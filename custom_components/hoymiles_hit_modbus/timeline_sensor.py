"""Entry-local, read-only Home Assistant sensors for optimizer timelines."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from math import isfinite
from typing import TYPE_CHECKING, Any, Mapping
import weakref

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
try:  # Real HA exports MATCH_ALL; minimal offline harnesses may not.
    from homeassistant.const import MATCH_ALL
except ImportError:  # pragma: no cover - deterministic integration stubs only
    MATCH_ALL = "*"
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from .automation_plan_timeline import (
    CurrentActualSnapshot,
    OptimizerTimelineTrace,
    TimelineValidationError,
    build_current_payload,
    build_pending_payload,
    build_unavailable_payload,
    semantic_fingerprint,
    validate_payload,
)
from .const import DOMAIN, NAME
from .energy_data import numeric_state_sample
from .models import RuntimeData

if TYPE_CHECKING:
    from .energy_data import NumericStateSample


_PHYSICAL_SOURCES: tuple[
    tuple[str, str, bool, float, float | None, float | None], ...
] = (
    ("pv", "overview_pv_total_power", True, 120.0, 0.0, None),
    # The Phase 0A source freeze identifies the managed, physically balanced
    # LOAD template as the exact public current-actual source.
    ("load", "sensor.hoymiles_actual_load_power", False, 120.0, 0.0, None),
    ("battery", "overview_battery_power", True, 120.0, None, None),
    ("grid", "overview_grid_total_active_power", True, 120.0, None, None),
    ("soc", "overview_battery_soc", True, 300.0, 0.0, 100.0),
)
_ACTIVE_MAX_AGE_SECONDS = 120.0


def _power_scale(state: Any) -> float:
    """Return the one bounded W/kW conversion used at publication."""

    unit = getattr(state, "attributes", {}).get("unit_of_measurement")
    return 1.0 if unit == "kW" else 0.001


def _source_candidates(
    registry: er.EntityRegistry,
    *,
    config_entry_id: str,
    unique_id: str,
) -> list[str]:
    """Return every exact registry candidate; callers require exactly one."""

    return [
        item.entity_id
        for item in registry.entities.values()
        if item.platform == DOMAIN
        and item.unique_id == unique_id
        and item.config_entry_id == config_entry_id
    ]


def _exact_registered_entity_id(
    registry: er.EntityRegistry,
    *,
    config_entry_id: str,
    unique_id: str,
) -> tuple[str | None, str | None]:
    """Resolve one exact same-entry source without a first-match fallback."""

    candidates = _source_candidates(
        registry,
        config_entry_id=config_entry_id,
        unique_id=unique_id,
    )
    if len(candidates) > 1:
        return None, "ambiguous_entry"
    if not candidates:
        return None, "source_unavailable"
    return candidates[0], None


def _sample_value(sample: NumericStateSample) -> float | None:
    return sample.value if sample.fresh else None


def capture_current_actual(
    hass: HomeAssistant,
    entry: ConfigEntry,
    now: datetime,
) -> tuple[CurrentActualSnapshot, tuple[dict[str, str], ...]]:
    """Capture one same-entry physical snapshot using existing freshness gates."""

    registry = er.async_get(hass)
    samples: dict[str, NumericStateSample] = {}
    source_records: list[dict[str, str]] = []
    for role, source_identity, entry_local, max_age, minimum, maximum in _PHYSICAL_SOURCES:
        if entry_local:
            entity_id, _ = _exact_registered_entity_id(
                registry,
                config_entry_id=entry.entry_id,
                unique_id=f"{entry.entry_id}_{source_identity}",
            )
        else:
            entity_id = source_identity
        state = hass.states.get(entity_id) if entity_id is not None else None
        scale = 1.0 if role == "soc" else _power_scale(state)
        sample = numeric_state_sample(
            state,
            now,
            max_age_seconds=max_age,
            scale=scale,
            minimum=minimum,
            maximum=maximum,
        )
        samples[role] = sample
        if entity_id is not None:
            source_records.append({"role": f"actual_{role}", "entity_id": entity_id})

    fresh_count = sum(sample.fresh for sample in samples.values())
    if fresh_count == len(samples):
        quality = "complete"
    elif fresh_count:
        quality = "partial"
    elif any(sample.reason in {"stale", "future_timestamp"} for sample in samples.values()):
        quality = "stale"
    else:
        quality = "unavailable"

    battery = _sample_value(samples["battery"])
    grid = _sample_value(samples["grid"])
    snapshot = CurrentActualSnapshot(
        observed_at=now,
        pv_kw=_sample_value(samples["pv"]),
        load_kw=_sample_value(samples["load"]),
        # Physical Overview signs are +discharge and +export. Convert each
        # exactly once to the public +charge and +import conventions.
        battery_kw=-battery if battery is not None else None,
        grid_kw=-grid if grid is not None else None,
        soc_percent=_sample_value(samples["soc"]),
        quality=quality,
        source_ages_seconds={
            role: (
                float(sample.age_seconds)
                if sample.age_seconds is not None and isfinite(sample.age_seconds)
                else None
            )
            for role, sample in samples.items()
        },
    )
    return snapshot, tuple(source_records)


class _TimelinePublisherProxy:
    """Keep source callbacks generation-bound without retaining the entity."""

    __slots__ = ("_generation", "_sensor_ref")

    def __init__(
        self,
        sensor: "HoymilesAutomationPlanTimelineSensor",
        generation: int,
    ) -> None:
        self._sensor_ref = weakref.ref(sensor)
        self._generation = generation

    def _sensor(self) -> "HoymilesAutomationPlanTimelineSensor | None":
        sensor = self._sensor_ref()
        if sensor is None or not sensor._accepts_generation(self._generation):
            return None
        return sensor

    def publish_pending(self, pending_input_revision: int) -> None:
        sensor = self._sensor()
        if sensor is not None:
            sensor.publish_pending(pending_input_revision)

    def publish_unavailable(self, *, input_revision: int, blocker_code: str) -> None:
        sensor = self._sensor()
        if sensor is not None:
            sensor.publish_unavailable(
                input_revision=input_revision,
                blocker_code=blocker_code,
            )

    def publish_current(
        self,
        trace: OptimizerTimelineTrace | None,
        *,
        input_revision: int,
        metadata: Mapping[str, Any] | None = None,
        quality: str | None = None,
    ) -> None:
        sensor = self._sensor()
        if sensor is not None:
            sensor.publish_current(
                trace,
                input_revision=input_revision,
                metadata=metadata,
                quality=quality,
            )


class HoymilesAutomationPlanTimelineSensor(SensorEntity):
    """Publish one immutable, bounded optimizer projection per source cycle."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:timeline-clock-outline"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: RuntimeData,
        *,
        policy_id: str,
        source_sensor: SensorEntity,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._policy_id = policy_id
        self._source_sensor: SensorEntity | None = source_sensor
        self._state = "unavailable"
        self._plan_revision = 0
        self._last_fingerprint: str | None = None
        self._last_complete: dict[str, Any] | None = None
        self._max_source_input_revision = 0
        self._latest_completed_input_revision = -1
        self._lifecycle_generation = 1
        self._platform_added = False
        self._attr_translation_key = f"{policy_id}_automation_plan_timeline"
        self._attr_unique_id = (
            f"{entry.entry_id}_{policy_id}_automation_plan_timeline"
        )
        self._attributes = build_unavailable_payload(
            policy_id=policy_id,
            config_entry_id=entry.entry_id,
            generated_at=dt_util.utcnow(),
            input_revision=0,
            plan_entity_id=None,
            blocker_code="awaiting_first_calculation",
        )
        attach = getattr(source_sensor, "attach_timeline_sensor", None)
        if not callable(attach):
            raise TimelineValidationError("source sensor cannot publish a timeline")
        self._source_proxy = _TimelinePublisherProxy(
            self,
            self._lifecycle_generation,
        )
        attach(self._source_proxy)

    @property
    def suggested_object_id(self) -> str:
        return f"hoymiles_hit_{self._policy_id}_automation_plan_timeline"

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

    def _plan_entity_id(self) -> tuple[str | None, str | None]:
        """Attest the exact source object against its same-entry registry row."""

        source_entry = getattr(self._source_sensor, "_entry", None)
        if source_entry is None or source_entry.entry_id != self._entry.entry_id:
            return None, "ambiguous_entry"
        registry = er.async_get(self.hass)
        expected_unique_id = f"{self._entry.entry_id}_{self._policy_id}_{'optimized_plan' if self._policy_id == 'rce' else 'charge_plan'}"
        return _exact_registered_entity_id(
            registry,
            config_entry_id=self._entry.entry_id,
            unique_id=expected_unique_id,
        )

    def _physical_active(
        self,
        observed_at: datetime,
    ) -> tuple[bool | None, datetime | None]:
        """Capture one publication-time lifecycle fact without fake false."""

        entity_id = (
            "input_boolean.hoymiles_rce_discharge_active"
            if self._policy_id == "rce"
            else "input_boolean.hoymiles_tariff_charge_active"
        )
        state = self.hass.states.get(entity_id)
        if state is None or state.state not in {"on", "off"}:
            return None, None
        reported = getattr(state, "last_reported", None) or getattr(
            state,
            "last_updated",
            None,
        )
        if not isinstance(reported, datetime) or reported.tzinfo is None:
            return None, None
        try:
            age_seconds = (
                observed_at.astimezone(timezone.utc)
                - reported.astimezone(timezone.utc)
            ).total_seconds()
        except (OverflowError, TypeError, ValueError):
            return None, None
        if age_seconds < -5.0 or age_seconds > _ACTIVE_MAX_AGE_SECONDS:
            return None, None
        return state.state == "on", observed_at

    def _provenance(
        self,
        plan_entity_id: str,
        physical_sources: tuple[dict[str, str], ...],
        metadata: Mapping[str, Any] | None,
    ) -> tuple[dict[str, str], ...]:
        records: list[dict[str, str]] = [
            {"role": "plan", "entity_id": plan_entity_id}
        ]
        records.extend(physical_sources)
        if metadata is not None:
            for key in (
                "forecast_today_entity",
                "forecast_tomorrow_entity",
                "forecast_remaining_today_entity",
                "forecast_day3_entity",
                "forecast_day_3_entity",
                "load_profile_broker_entity_id",
            ):
                value = metadata.get(key)
                if isinstance(value, str) and "." in value:
                    records.append({"role": key, "entity_id": value})
        deduplicated: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for record in records:
            identity = (record["role"], record["entity_id"])
            if identity not in seen:
                deduplicated.append(record)
                seen.add(identity)
        return tuple(deduplicated[:16])

    def _accepts_generation(self, generation: int) -> bool:
        return (
            generation == self._lifecycle_generation
            and self._source_sensor is not None
        )

    def _lifecycle_current(self) -> bool:
        return self._source_sensor is not None

    def _detach_source(self) -> None:
        """Break the exact entry-local callback relation without publishing."""

        source = self._source_sensor
        proxy = self._source_proxy
        if source is not None and getattr(source, "_timeline_sensor", None) is proxy:
            source._timeline_sensor = None
        self._source_sensor = None
        self._source_proxy = None

    @staticmethod
    def _valid_input_revision(input_revision: int) -> bool:
        return (
            not isinstance(input_revision, bool)
            and isinstance(input_revision, int)
            and input_revision >= 0
        )

    def _publish(self, state: str, attributes: dict[str, Any]) -> None:
        """Commit one already-validated atomic attributes/state publication."""

        self._state = state
        self._attributes = attributes
        if (
            self._platform_added
            and self.hass is not None
            and self.entity_id is not None
            and self._lifecycle_current()
        ):
            self.async_write_ha_state()

    def _publish_candidate(
        self,
        state: str,
        candidate: dict[str, Any],
        *,
        metadata_write: bool = False,
    ) -> bool:
        """Assign one runtime-local revision to a complete semantic transition."""

        fingerprint = semantic_fingerprint(candidate)
        semantic_changed = fingerprint != self._last_fingerprint
        revision = self._plan_revision + 1 if semantic_changed else self._plan_revision
        if revision < 1:
            revision = 1
        candidate["plan_revision"] = revision
        validate_payload(candidate, expected_state=state)
        if not semantic_changed and not metadata_write:
            return False
        self._plan_revision = revision
        self._last_fingerprint = fingerprint
        if state == "current":
            self._last_complete = candidate
        self._publish(state, candidate)
        return True

    def publish_pending(self, pending_input_revision: int) -> None:
        """Withdraw current authority while retaining only the old point set."""

        if (
            not self._lifecycle_current()
            or not self._valid_input_revision(pending_input_revision)
            or pending_input_revision <= self._latest_completed_input_revision
            or pending_input_revision < self._max_source_input_revision
        ):
            return
        if (
            self._state == "pending"
            and self._attributes.get("pending_input_revision")
            == pending_input_revision
        ):
            return
        plan_entity_id, _ = self._plan_entity_id()
        candidate = build_pending_payload(
            self._last_complete,
            policy_id=self._policy_id,
            config_entry_id=self._entry.entry_id,
            generated_at=dt_util.utcnow(),
            pending_input_revision=pending_input_revision,
            plan_entity_id=plan_entity_id,
            plan_revision=max(self._plan_revision, 1),
        )
        self._publish_candidate("pending", candidate, metadata_write=True)
        self._max_source_input_revision = max(
            self._max_source_input_revision,
            pending_input_revision,
        )

    def publish_unavailable(self, *, input_revision: int, blocker_code: str) -> None:
        """Publish no stale points when a complete trace cannot be proven."""

        if (
            not self._lifecycle_current()
            or not self._valid_input_revision(input_revision)
            or input_revision < self._max_source_input_revision
        ):
            return
        if (
            self._state == "unavailable"
            and self._attributes.get("input_revision") == input_revision
            and self._attributes.get("blocker_code") == blocker_code
        ):
            return
        plan_entity_id, source_blocker = self._plan_entity_id()
        candidate = build_unavailable_payload(
            policy_id=self._policy_id,
            config_entry_id=self._entry.entry_id,
            generated_at=dt_util.utcnow(),
            input_revision=input_revision,
            plan_entity_id=plan_entity_id,
            blocker_code=source_blocker or blocker_code,
            plan_revision=max(self._plan_revision, 1),
        )
        self._publish_candidate("unavailable", candidate)
        self._max_source_input_revision = max(
            self._max_source_input_revision,
            input_revision,
        )
        self._last_complete = None

    def publish_current(
        self,
        trace: OptimizerTimelineTrace | None,
        *,
        input_revision: int,
        metadata: Mapping[str, Any] | None = None,
        quality: str | None = None,
    ) -> None:
        """Validate a complete same-revision trace, then publish it once."""

        if (
            not self._lifecycle_current()
            or not self._valid_input_revision(input_revision)
            or input_revision < self._max_source_input_revision
            or input_revision < self._latest_completed_input_revision
        ):
            return
        if trace is None or trace.policy_id != self._policy_id:
            self.publish_unavailable(
                input_revision=input_revision,
                blocker_code="invalid_trace",
            )
            return
        plan_entity_id, source_blocker = self._plan_entity_id()
        if plan_entity_id is None:
            self.publish_unavailable(
                input_revision=input_revision,
                blocker_code=source_blocker or "source_unavailable",
            )
            return
        now = dt_util.utcnow().astimezone(timezone.utc)
        snapshot, physical_sources = capture_current_actual(
            self.hass,
            self._entry,
            now,
        )
        candidate_trace = replace(trace, quality=quality or trace.quality)
        sources = self._provenance(
            plan_entity_id,
            physical_sources,
            metadata,
        )
        physical_active, active_observed_at = self._physical_active(now)
        provisional_revision = max(self._plan_revision, 1)
        candidate = build_current_payload(
            candidate_trace,
            config_entry_id=self._entry.entry_id,
            generated_at=now,
            input_revision=input_revision,
            plan_revision=provisional_revision,
            plan_entity_id=plan_entity_id,
            current_actual=snapshot,
            sources=sources,
            physical_active=physical_active,
            active_observed_at=active_observed_at,
        )
        self._publish_candidate("current", candidate)
        self._max_source_input_revision = max(
            self._max_source_input_revision,
            input_revision,
        )
        self._latest_completed_input_revision = max(
            self._latest_completed_input_revision,
            input_revision,
        )


async def _timeline_async_added_to_hass(
    self: HoymilesAutomationPlanTimelineSensor,
) -> None:
    """Mark writes safe only after the base Entity add lifecycle completes."""

    try:
        await super(
            HoymilesAutomationPlanTimelineSensor,
            self,
        ).async_added_to_hass()
    except BaseException:
        self._lifecycle_generation += 1
        self._platform_added = False
        self._detach_source()
        raise
    if self._lifecycle_current():
        self._platform_added = True


async def _timeline_async_will_remove_from_hass(
    self: HoymilesAutomationPlanTimelineSensor,
) -> None:
    """Invalidate callbacks and explicitly detach the exact source relation."""

    self._lifecycle_generation += 1
    self._platform_added = False
    self._detach_source()
    await super(
        HoymilesAutomationPlanTimelineSensor,
        self,
    ).async_will_remove_from_hass()


# Keep the class declaration compatible with the frozen startup AST while the
# runtime still participates explicitly in the standard Entity lifecycle.
HoymilesAutomationPlanTimelineSensor.async_added_to_hass = (
    _timeline_async_added_to_hass
)
HoymilesAutomationPlanTimelineSensor.async_will_remove_from_hass = (
    _timeline_async_will_remove_from_hass
)
