"""Home Assistant adapter for the output-only canonical Supervisor plan."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import math
from typing import Any, Mapping

try:  # Real HA exports the platform domain; small contract stubs may not.
    from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN, SensorEntity
except ImportError:  # pragma: no cover - dependency-free contract harnesses
    from homeassistant.components.sensor import SensorEntity

    SENSOR_DOMAIN = "sensor"
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
try:
    from homeassistant.helpers.entity_registry import EVENT_ENTITY_REGISTRY_UPDATED
except ImportError:  # pragma: no cover - dependency-free contract harnesses
    EVENT_ENTITY_REGISTRY_UPDATED = "entity_registry_updated"
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME
from .models import RuntimeData
from .supervisor_canonical_ledger import canonical_execution_ledger_to_dict
from .supervisor_canonical_runtime import (
    CanonicalRuntimeError,
    TIMELINE_MAX_AGE_SECONDS,
    augment_canonical_projection_payload,
    build_supervisor_canonical_ledger,
    canonical_timeline_dependencies,
)


CANONICAL_ENTITY_ID = f"{SENSOR_DOMAIN}.hoymiles_hit_ems_supervisor_canonical_plan"
CAPACITY_UNIQUE_ID_SUFFIX = "battery_capacity"
CANONICAL_COHORT_DELAY_SECONDS = 3.0
EXPECTED_TIMELINE_REFRESH_DELAY_SECONDS = 30.0
FRESHNESS_EXPIRY_EPSILON_SECONDS = 0.1
_LOGGER = logging.getLogger(__name__)


class HoymilesSupervisorCanonicalPlanSensor(SensorEntity):
    """Publish one fail-closed canonical trajectory for Aurora."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "ems_supervisor_canonical_plan"
    _attr_icon = "mdi:chart-timeline-variant-shimmer"
    # The 48-hour slot array is deliberately excluded from Recorder, while the
    # small revision/final-SOC summary remains available to the 24-hour support
    # history.  This makes expected-vs-authorization drift observable without
    # multiplying the database by 192 nested records per plan refresh.
    _unrecorded_attributes = frozenset({"slots"})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: RuntimeData,
        *,
        supervisor: SensorEntity,
        timelines: Mapping[str, SensorEntity],
        expected_timeline: SensorEntity,
    ) -> None:
        if set(timelines) != {"rce", "tariff", "rcm"}:
            raise ValueError("canonical plan requires all three timelines")
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._supervisor = supervisor
        self._timelines = dict(timelines)
        self._expected_timeline = expected_timeline
        self.entity_id = CANONICAL_ENTITY_ID
        self._attr_unique_id = f"{entry.entry_id}_ems_supervisor_canonical_plan"
        self._state = "unavailable"
        self._attributes = self._empty_attributes("awaiting_current_sources")
        self._source_unsub: Any = None
        self._registry_unsub: Any = None
        self._capacity_entity_id: str | None = None
        self._platform_added = False
        self._removed = False
        self._recompute_cancel: Any = None
        self._expected_recompute_cancel: Any = None
        self._freshness_expiry_cancel: Any = None
        self._freshness_expiry_generation = 0
        self._last_complete_attributes: dict[str, Any] | None = None
        self._last_adapter_error: tuple[str, str, str] | None = None
        self._timeline_dependencies = ("rce", "tariff", "rcm")

    @property
    def suggested_object_id(self) -> str:
        return "hoymiles_hit_ems_supervisor_canonical_plan"

    @property
    def native_value(self) -> str:
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attributes

    @property
    def device_info(self) -> DeviceInfo:
        source = self._runtime.source_device
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=source.name_by_user or source.name or NAME,
            manufacturer=source.manufacturer or "Hoymiles",
            model=source.model or "HIT xxL G3",
            sw_version=source.sw_version,
        )

    @staticmethod
    def _timestamp(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _empty_attributes(
        self, blocker_code: str, *, adapter_error_type: str | None = None,
        adapter_error_stage: str | None = None,
    ) -> dict[str, Any]:
        now = dt_util.utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        return {
            "schema_version": 1,
            "output_only": True,
            "built_at": self._timestamp(now),
            "blocker_code": blocker_code,
            "adapter_error_type": adapter_error_type,
            "adapter_error_stage": adapter_error_stage,
            "slots": [],
        }

    def _publish(self, state: str, attributes: dict[str, Any]) -> None:
        self._state = state
        self._attributes = attributes
        if self._platform_added and not self._removed:
            self.schedule_update_ha_state()

    def _retained_attributes(
        self,
        *,
        state: str,
        blocker_code: str,
        source_states: Mapping[str, str],
    ) -> dict[str, Any] | None:
        """Return a diagnostic-only retained canonical projection.

        The canonical ledger has no execution authority.  During a cohort
        refresh, retaining its last complete geometry prevents Aurora from
        briefly rendering an empty SOC/action view while the source policies
        publish their individual pending snapshots.
        """

        if self._last_complete_attributes is None:
            return None
        return {
            **self._last_complete_attributes,
            "canonical_status": state,
            "canonical_blocker_code": blocker_code,
            "source_timeline_states": dict(source_states),
            "result_current": False,
            "recalculation_pending": True,
        }

    def _resolve_capacity_entity(self) -> str | None:
        registry = er.async_get(self.hass)
        expected_unique_id = f"{self._entry.entry_id}_{CAPACITY_UNIQUE_ID_SUFFIX}"
        registry_entities = getattr(
            registry,
            "entities",
            getattr(registry, "entries", {}),
        )
        candidates = tuple(
            item.entity_id
            for item in registry_entities.values()
            if item.platform == DOMAIN
            and item.config_entry_id == self._entry.entry_id
            and item.unique_id == expected_unique_id
        )
        return candidates[0] if len(candidates) == 1 else None

    def _capacity_kwh(self) -> float:
        entity_id = self._capacity_entity_id
        state = self.hass.states.get(entity_id) if entity_id is not None else None
        if state is None or state.attributes.get("unit_of_measurement") != "kWh":
            raise CanonicalRuntimeError("battery_capacity_unavailable")
        try:
            value = float(state.state)
        except (TypeError, ValueError) as err:
            raise CanonicalRuntimeError("battery_capacity_unavailable") from err
        if not math.isfinite(value) or not 0.001 <= value <= 10_000.0:
            raise CanonicalRuntimeError("battery_capacity_unavailable")
        return value

    def _source_states(self) -> dict[str, str]:
        return {
            policy_id: str(getattr(sensor, "native_value", "unavailable"))
            for policy_id, sensor in self._timelines.items()
        }

    @staticmethod
    def _expected_semantic_value(state: Any) -> tuple[Any, Mapping[str, Any]]:
        """Return baseline content that can alter the expected projection.

        ``generated_at`` and ``built_at`` are publication diagnostics.  A
        refresh that changes only either timestamp must not churn the chart,
        while lifecycle, revision, provenance and point changes remain
        projection-relevant.
        """

        attributes = getattr(state, "attributes", {}) if state is not None else {}
        if not isinstance(attributes, Mapping):
            attributes = {}
        semantic_attributes = {
            key: value
            for key, value in attributes.items()
            if key not in {"generated_at", "built_at"}
        }
        return getattr(state, "state", None), semantic_attributes

    def _expected_event_changes_projection(self, event: Event) -> bool:
        """Return whether the neutral baseline changes expected SOC geometry."""

        data = getattr(event, "data", {})
        if not isinstance(data, Mapping):
            return False
        if data.get("entity_id") != getattr(
            self._expected_timeline,
            "entity_id",
            None,
        ):
            return False
        return self._expected_semantic_value(
            data.get("old_state")
        ) != self._expected_semantic_value(data.get("new_state"))

    @staticmethod
    def _state_semantic_value(state: Any, *keys: str) -> tuple[Any, ...]:
        """Project a HA state to fields that can change the displayed plan.

        State-change events also carry report timestamps and live physical
        diagnostics.  Neither changes a future trajectory.  Consuming either
        here would make the canonical display rebuild at the ESPHome cadence,
        even when every source plan revision remained identical.
        """

        attributes = getattr(state, "attributes", {}) if state is not None else {}
        if not isinstance(attributes, Mapping):
            attributes = {}
        return (
            getattr(state, "state", None),
            *(attributes.get(key) for key in keys),
        )

    def _source_event_changes_plan(self, event: Event) -> bool:
        """Return whether this event changes canonical plan semantics.

        The output-only ledger follows source timeline lifecycle and revisions,
        not source ``generated_at`` values or 15-second RCEm diagnostics.  A
        Supervisor event can still refresh the ledger for a deliberate mode,
        profile or selected-candidate change, while live owner/readback/SOC
        updates remain in the execution view instead of churning the chart.
        """

        data = getattr(event, "data", {})
        if not isinstance(data, Mapping):
            return False
        entity_id = data.get("entity_id")
        old_state = data.get("old_state")
        new_state = data.get("new_state")
        if not isinstance(entity_id, str):
            return False
        if entity_id == getattr(self._supervisor, "entity_id", None):
            if canonical_timeline_dependencies(getattr(
                self._supervisor, "latest_active_frame", None
            )) != self._timeline_dependencies:
                return True
            keys = (
                "supervisor_mode",
                "profile",
                "selected_policy",
                "selected_candidate_revision",
                "execution_phase",
            )
            return self._state_semantic_value(
                old_state, *keys
            ) != self._state_semantic_value(new_state, *keys)
        if entity_id == self._capacity_entity_id:
            return self._state_semantic_value(
                old_state, "unit_of_measurement"
            ) != self._state_semantic_value(new_state, "unit_of_measurement")
        for policy_id, sensor in self._timelines.items():
            if entity_id != getattr(sensor, "entity_id", None):
                continue
            keys = (
                "schema_version",
                "policy_id",
                "config_entry_id",
                "input_revision",
                "plan_revision",
                "result_current",
                "recalculation_pending",
                "quality",
                "blocker_code",
            )
            return self._state_semantic_value(
                old_state, *keys
            ) != self._state_semantic_value(new_state, *keys)
        return False

    def _source_event_refreshes_freshness(self, event: Event) -> bool:
        """Recognize one successful, semantically unchanged full-plan commit."""

        data = getattr(event, "data", {})
        if not isinstance(data, Mapping):
            return False
        entity_id = data.get("entity_id")
        old_state = data.get("old_state")
        new_state = data.get("new_state")
        if not isinstance(entity_id, str):
            return False
        if not any(
            entity_id == getattr(sensor, "entity_id", None)
            for sensor in self._timelines.values()
        ):
            return False
        old_attributes = getattr(old_state, "attributes", {})
        new_attributes = getattr(new_state, "attributes", {})
        if not isinstance(old_attributes, Mapping) or not isinstance(
            new_attributes, Mapping
        ):
            return False
        return bool(
            getattr(old_state, "state", None) == "current"
            and getattr(new_state, "state", None) == "current"
            and old_attributes.get("generated_at")
            != new_attributes.get("generated_at")
            and not self._source_event_changes_plan(event)
        )

    def _cancel_freshness_expiry(self) -> None:
        self._freshness_expiry_generation += 1
        cancel = self._freshness_expiry_cancel
        self._freshness_expiry_cancel = None
        if cancel is not None:
            cancel()

    def _schedule_freshness_expiry(self) -> None:
        """Recheck current sources immediately after their first age limit."""

        self._cancel_freshness_expiry()
        if self._removed or self._state != "current":
            return
        deadlines: list[datetime] = []
        for policy_id, sensor in self._timelines.items():
            if policy_id not in self._timeline_dependencies:
                continue
            attributes = getattr(sensor, "extra_state_attributes", {})
            value = attributes.get("generated_at") if isinstance(attributes, Mapping) else None
            generated = dt_util.parse_datetime(value) if isinstance(value, str) else None
            if generated is None or generated.tzinfo is None:
                return
            deadlines.append(
                generated.astimezone(timezone.utc)
                + timedelta(seconds=TIMELINE_MAX_AGE_SECONDS[policy_id])
            )
        if len(deadlines) != len(self._timeline_dependencies):
            return
        now = dt_util.utcnow()
        if now.tzinfo is None or now.utcoffset() is None:
            now = now.replace(tzinfo=timezone.utc)
        delay = max(
            (min(deadlines) - now.astimezone(timezone.utc)).total_seconds()
            + FRESHNESS_EXPIRY_EPSILON_SECONDS,
            FRESHNESS_EXPIRY_EPSILON_SECONDS,
        )
        generation = self._freshness_expiry_generation

        @callback
        def expire(_now: datetime) -> None:
            if self._removed or generation != self._freshness_expiry_generation:
                return
            self._freshness_expiry_cancel = None
            self._recompute()

        self._freshness_expiry_cancel = async_call_later(
            self.hass,
            delay,
            expire,
        )

    @callback
    def _recompute(self) -> None:
        self._cancel_expected_recompute()
        self._cancel_freshness_expiry()
        if self._removed:
            return
        source_states = self._source_states()
        frame = getattr(self._supervisor, "latest_active_frame", None)
        self._timeline_dependencies = canonical_timeline_dependencies(frame)
        required_states = {key: source_states[key] for key in self._timeline_dependencies}
        if any(state == "pending" for state in required_states.values()):
            retained = self._retained_attributes(
                state="pending",
                blocker_code="source_recalculation_pending",
                source_states=source_states,
            )
            self._publish(
                "pending",
                retained
                or self._empty_attributes("source_recalculation_pending"),
            )
            return
        if any(state != "current" for state in required_states.values()):
            blocker = next(
                f"{policy_id}_timeline_{state}"
                for policy_id, state in required_states.items()
                if state != "current"
            )
            retained = self._retained_attributes(
                state="partial",
                blocker_code=blocker,
                source_states=source_states,
            )
            self._publish("partial", retained or self._empty_attributes(blocker))
            return
        if frame is None:
            self._publish("unavailable", self._empty_attributes("active_frame_missing"))
            return
        stage = "source_snapshot"
        try:
            capacity = self._capacity_kwh()
            calculation_now = dt_util.utcnow()
            timeline_payloads = {
                policy_id: dict(sensor.extra_state_attributes)
                for policy_id, sensor in self._timelines.items()
                if policy_id in self._timeline_dependencies
            }
            expected_attributes = getattr(
                self._expected_timeline,
                "extra_state_attributes",
                None,
            )
            expected_payload = (
                dict(expected_attributes)
                if isinstance(expected_attributes, Mapping)
                else None
            )
            if expected_payload is not None:
                # Home Assistant stores an entity's state separately from its
                # attributes.  Reconstruct the exact neutral-baseline payload
                # expected by the pure validator at this adapter boundary.
                expected_payload["state"] = str(
                    getattr(
                        self._expected_timeline,
                        "native_value",
                        "unavailable",
                    )
                )
            stage = "canonical_ledger"
            ledger = build_supervisor_canonical_ledger(
                frame=frame,
                timelines=timeline_payloads,
                usable_capacity_kwh=capacity,
                freshness_now=calculation_now,
            )
            stage = "expected_projection"
            attributes = augment_canonical_projection_payload(
                canonical_execution_ledger_to_dict(ledger),
                frame=frame,
                timelines=timeline_payloads,
                usable_capacity_kwh=capacity,
                freshness_now=calculation_now,
                expected_timeline=expected_payload,
            )
            attributes["excluded_timeline_policies"] = {
                policy: "disabled_by_user_and_inactive"
                for policy in self._timelines if policy not in self._timeline_dependencies
            }
        except CanonicalRuntimeError as err:
            if err.code.endswith("_timeline_stale"):
                retained = self._retained_attributes(
                    state="partial",
                    blocker_code=err.code,
                    source_states=source_states,
                )
                if retained is not None:
                    self._publish("partial", retained)
                    return
            self._publish("unavailable", self._empty_attributes(err.code))
            return
        except (AttributeError, TypeError, ValueError) as err:
            signature = (stage, type(err).__name__, str(err))
            if signature != self._last_adapter_error:
                # Keep the traceback for diagnosis, once per unchanged failure.
                _LOGGER.exception("Canonical plan adapter failed at %s", stage)
                self._last_adapter_error = signature
            self._publish("unavailable", self._empty_attributes(
                "canonical_adapter_error", adapter_error_type=type(err).__name__,
                adapter_error_stage=stage))
            return
        self._last_adapter_error = None
        self._last_complete_attributes = dict(attributes)
        self._publish("current", attributes)
        self._schedule_freshness_expiry()

    @callback
    def _schedule_recompute(self) -> None:
        """Coalesce source transitions into one canonical publication."""

        self._cancel_expected_recompute()
        if self._removed or self._recompute_cancel is not None:
            return

        @callback
        def recompute_callback(_now: datetime) -> None:
            self._recompute_cancel = None
            self._recompute()

        self._recompute_cancel = async_call_later(
            self.hass,
            CANONICAL_COHORT_DELAY_SECONDS,
            recompute_callback,
        )

    def _cancel_expected_recompute(self) -> None:
        cancel = self._expected_recompute_cancel
        self._expected_recompute_cancel = None
        if cancel is not None:
            cancel()

    @callback
    def _schedule_expected_recompute(self) -> None:
        """Bound and coalesce display-only baseline refreshes.

        The shared-input broker may publish physical SOC/power revisions every
        few seconds.  Rebuilding Aurora at that cadence can disturb mobile
        scrolling, so expected-only changes use one bounded refresh window.
        A pending policy rebuild already consumes the latest baseline.
        """

        if (
            self._removed
            or self._recompute_cancel is not None
            or self._expected_recompute_cancel is not None
        ):
            return

        @callback
        def recompute_callback(_now: datetime) -> None:
            self._expected_recompute_cancel = None
            self._recompute()

        self._expected_recompute_cancel = async_call_later(
            self.hass,
            EXPECTED_TIMELINE_REFRESH_DELAY_SECONDS,
            recompute_callback,
        )

    def _publish_pending_cohort(self) -> None:
        """Withdraw current SOC/action authority before the delayed rebuild."""

        source_states = self._source_states()
        retained = self._retained_attributes(
            state="pending",
            blocker_code="source_recalculation_pending",
            source_states=source_states,
        )
        self._publish(
            "pending",
            retained or self._empty_attributes("source_recalculation_pending"),
        )

    @callback
    def _handle_source_event(self, event: Event) -> None:
        if self._expected_event_changes_projection(event):
            # The neutral baseline is display-only.  Its refresh is coalesced
            # into a new expected projection without withdrawing or changing
            # canonical authorization while the refresh is in flight.
            self._schedule_expected_recompute()
            return
        if self._source_event_changes_plan(event):
            self._publish_pending_cohort()
            self._schedule_recompute()
            return
        if self._source_event_refreshes_freshness(event):
            # A new full-run certificate with the same plan revision must not
            # flash the chart through pending; only refresh its age contract.
            self._schedule_recompute()

    @callback
    def _handle_registry_event(self, event: Event) -> None:
        data = getattr(event, "data", {})
        if data.get("action") not in {"create", "remove", "update"}:
            return
        resolved = self._resolve_capacity_entity()
        if resolved == self._capacity_entity_id:
            return
        self._capacity_entity_id = resolved
        self._subscribe_sources()
        self._publish_pending_cohort()
        self._schedule_recompute()

    def _subscribe_sources(self) -> None:
        if self._source_unsub is not None:
            self._source_unsub()
            self._source_unsub = None
        entity_ids = {
            entity_id
            for entity_id in (
                getattr(self._supervisor, "entity_id", None),
                self._capacity_entity_id,
                *(getattr(sensor, "entity_id", None) for sensor in self._timelines.values()),
                getattr(self._expected_timeline, "entity_id", None),
            )
            if isinstance(entity_id, str)
        }
        if entity_ids:
            self._source_unsub = async_track_state_change_event(
                self.hass,
                tuple(sorted(entity_ids)),
                self._handle_source_event,
            )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._removed = False
        self._platform_added = True
        self._capacity_entity_id = self._resolve_capacity_entity()
        self._subscribe_sources()
        self._registry_unsub = self.hass.bus.async_listen(
            EVENT_ENTITY_REGISTRY_UPDATED,
            self._handle_registry_event,
        )
        self._recompute()

    async def async_will_remove_from_hass(self) -> None:
        self._removed = True
        self._platform_added = False
        if self._recompute_cancel is not None:
            self._recompute_cancel()
            self._recompute_cancel = None
        self._cancel_expected_recompute()
        self._cancel_freshness_expiry()
        for unsubscribe_name in ("_source_unsub", "_registry_unsub"):
            unsubscribe = getattr(self, unsubscribe_name)
            setattr(self, unsubscribe_name, None)
            if unsubscribe is not None:
                unsubscribe()
        await super().async_will_remove_from_hass()


__all__ = (
    "CANONICAL_ENTITY_ID",
    "HoymilesSupervisorCanonicalPlanSensor",
)
