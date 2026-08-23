"""Event-driven Off/Shadow Home Assistant adapter for EMS Supervisor V1."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import logging
import math
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    EVENT_CORE_CONFIG_UPDATE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME
from .ems_supervisor import (
    PolicyCandidate,
    SupervisorMode,
    SupervisorProfile,
    arbitrate_supervisor,
    serialize_supervisor_summary,
)
from .models import RuntimeData
from .supervisor_runtime import (
    ExecutionSourceSnapshot,
    RcePlanStatus,
    RceSourceSnapshot,
    RcmAction,
    RcmSourceSnapshot,
    TariffAction,
    TariffPlanStatus,
    TariffRunNeed,
    TariffSourceSnapshot,
    build_execution_context,
    build_rce_candidate,
    build_rcm_candidate,
    build_tariff_candidate,
)


_LOGGER = logging.getLogger(__name__)
_GUARD_KEY = f"{DOMAIN}_ems_supervisor_guard"
_PLANNER_DELAY_SECONDS = 0.100
_COHORT_SPAN_SECONDS = 5.0
_GENERATION_MAX = 16_000_000.0
_READBACK_ACTIVE_TOLERANCE = 1.0
_MICROSECOND = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class SupervisorSourceSpec:
    """One frozen logical HA source used by the adapter."""

    number: int
    key: str
    locator: str
    entry_local: bool
    planner_event: bool
    future_helper: bool = False


_SOURCE_ROWS = (
    (1, "supervisor_mode", "input_select.hoymiles_ems_supervisor_mode", False, False, False),
    (2, "supervisor_profile", "input_select.hoymiles_ems_supervisor_profile", False, True, False),
    (3, "allow_rce", "input_boolean.hoymiles_ems_supervisor_allow_rce", False, True, False),
    (4, "allow_tariff", "input_boolean.hoymiles_ems_supervisor_allow_tariff", False, True, False),
    (5, "allow_rcm", "input_boolean.hoymiles_ems_supervisor_allow_rcm", False, True, False),
    (6, "rce_plan", "rce_optimized_plan", True, True, False),
    (7, "rce_enabled", "input_boolean.hoymiles_rce_discharge_enabled", False, True, False),
    (8, "rce_active", "input_boolean.hoymiles_rce_discharge_active", False, False, False),
    (9, "rce_latched_slot_end", "input_datetime.hoymiles_rce_latched_slot_end", False, False, False),
    (10, "rce_latched_minimum_soc", "input_number.hoymiles_rce_latched_minimum_soc", False, False, False),
    (11, "rce_control_data_ready", "binary_sensor.hoymiles_rce_control_data_ready", False, False, False),
    (12, "rce_price_above_threshold", "binary_sensor.hoymiles_rce_price_above_threshold", False, True, False),
    (13, "rce_reserve_ready", "binary_sensor.hoymiles_rce_reserve_ready", False, False, False),
    (14, "tariff_plan", "tariff_charge_plan", True, True, False),
    (15, "tariff_enabled", "input_boolean.hoymiles_tariff_charge_enabled", False, True, False),
    (16, "tariff_active", "input_boolean.hoymiles_tariff_charge_active", False, False, False),
    (17, "tariff_active_action", "input_text.hoymiles_tariff_active_action", False, False, False),
    (18, "tariff_latched_slot_end", "input_datetime.hoymiles_tariff_latched_slot_end", False, False, False),
    (19, "tariff_latched_target_soc", "input_number.hoymiles_tariff_latched_target_soc", False, False, False),
    (20, "tariff_control_data_ready", "binary_sensor.hoymiles_tariff_control_data_ready", False, False, False),
    (21, "tariff_planned_charge_slot", "binary_sensor.hoymiles_tariff_planned_charge_slot", False, True, False),
    (22, "rcm_plan", "rcm_voltage_plan", True, True, False),
    (23, "rcm_enabled", "input_boolean.hoymiles_rcm_enabled", False, True, False),
    (24, "rcm_shadow_mode", "input_boolean.hoymiles_rcm_shadow_mode", False, False, False),
    (25, "rcm_export_control_enabled", "input_boolean.hoymiles_rcm_export_control_enabled", False, True, False),
    (26, "rcm_pre_discharge_enabled", "input_boolean.hoymiles_rcm_pre_discharge_enabled", False, True, False),
    (27, "rcm_active", "input_boolean.hoymiles_rcm_active", False, False, False),
    (28, "rcm_export_control_active", "input_boolean.hoymiles_rcm_export_control_active", False, False, False),
    (29, "rcm_pre_discharge_active", "input_boolean.hoymiles_rcm_pre_discharge_active", False, False, False),
    (30, "rcm_latched_pre_discharge_deadline", "input_datetime.hoymiles_rcm_latched_pre_discharge_deadline", False, False, False),
    (31, "rcm_latched_pre_discharge_target_soc", "input_number.hoymiles_rcm_latched_pre_discharge_target_soc", False, False, False),
    (32, "rcm_latched_pre_discharge_power", "input_number.hoymiles_rcm_latched_pre_discharge_power", False, False, False),
    (33, "sun", "sun.sun", False, False, False),
    (34, "manual_discharge_active", "input_boolean.hoymiles_discharge_cycle_active", False, False, False),
    (35, "manual_charge_active", "input_boolean.hoymiles_charge_cycle_active", False, False, False),
    (36, "balancing_active", "input_boolean.hoymiles_battery_balancing_active", False, False, False),
    (37, "discharge_timer", "timer.hoymiles_discharge", False, False, False),
    (38, "charge_timer", "timer.hoymiles_charge", False, False, False),
    (39, "sale_block_active", "binary_sensor.hoymiles_sale_block_active", False, False, False),
    (40, "ems_mode_readback", "ems_mode_readback_code", True, False, False),
    (41, "ems_generation", "ems_control_readback_generation", True, False, False),
    (42, "ems_execution_ready", "binary_sensor.hoymiles_ems_execution_ready", False, False, False),
    (43, "direct_execution_ready", "binary_sensor.hoymiles_direct_register_execution_ready", False, False, False),
    (44, "gcf_enable_readback", "gcf_enable_readback_code", True, False, False),
    (45, "gcf_export_limit_readback", "gcf_maximum_export_power_readback", True, False, False),
    (46, "gcf_generation", "gcf_control_readback_generation", True, False, False),
    (47, "hardware_readback_supported", "ems_verified_hardware_readback_supported", True, False, False),
    (48, "battery_soc", "overview_battery_soc", True, False, False),
    (49, "bms_voltage", "battery_voltage_bms", True, False, False),
    (50, "bms_max_charge_current", "maximum_charge_current", True, False, False),
    (51, "bms_max_discharge_current", "maximum_discharge_current", True, False, False),
    (52, "machine_type", "machines_type", True, False, False),
    (53, "inverter_count", "number_of_machines_master_and_slave", True, False, False),
    (54, "topology_generation", "parallel_topology_readback_generation", True, False, False),
    (55, "charge_power_readback", "battery_max_charge_power_readback", True, False, False),
    (56, "discharge_power_readback", "ems_maximum_discharge_power_readback", True, False, False),
    (57, "discharge_soc_readback", "ems_force_discharge_soc_readback", True, False, False),
    (58, "charge_power_ems_readback", "ems_maximum_charge_power_readback", True, False, False),
    (59, "charge_soc_readback", "ems_force_charge_soc_readback", True, False, False),
    (60, "rce_effective_discharge_power", "sensor.hoymiles_rce_effective_discharge_power_percent", False, False, False),
)

SUPERVISOR_SOURCE_SPECS = tuple(
    SupervisorSourceSpec(*row) for row in _SOURCE_ROWS
)
_SOURCE_BY_KEY = {spec.key: spec for spec in SUPERVISOR_SOURCE_SPECS}
_PLANNER_KEYS = frozenset(
    spec.key for spec in SUPERVISOR_SOURCE_SPECS if spec.planner_event
)
_ENTRY_LOCAL_SPECS = tuple(
    spec for spec in SUPERVISOR_SOURCE_SPECS if spec.entry_local
)

RCE_PLAN_ATTRIBUTES = (
    "status_code",
    "result_current",
    "recalculation_pending",
    "input_revision",
    "current_slot_planned",
    "current_slot_start_eligible",
    "current_slot_continue_eligible",
    "current_slot_end",
    "current_run_end",
    "current_slot_execution_discharge_power_kw",
    "current_slot_planned_export_kwh",
    "current_required_minimum_soc_percent",
)
TARIFF_PLAN_ATTRIBUTES = (
    "status_code",
    "result_current",
    "recalculation_pending",
    "input_revision",
    "current_slot_planned",
    "current_action",
    "current_run_need_class",
    "current_run_start_eligible",
    "current_run_continue_eligible",
    "requested_charge_power_kw",
    "command_charge_power_percent",
    "current_run_grid_import_kwh",
    "current_run_benefit_pln",
    "target_soc_percent",
    "base_reserve_soc_percent",
    "current_slot_end",
)
RCM_PLAN_ATTRIBUTES = (
    "result_current",
    "recalculation_pending",
    "input_revision",
    "live_emergency",
    "emergency_action_ready",
    "prediction_ready",
    "action",
    "risk_window_active",
    "voltage_risk_score_percent",
    "recommended_charge_limit_percent",
    "recommended_charge_power_kw",
    "recommended_export_limit_percent",
    "charge_actuator_data_fresh",
    "export_actuator_data_fresh",
    "gcf_data_fresh",
    "bms_charge_data_fresh",
    "bms_charge_available",
    "system_power_data_valid",
    "pre_discharge_start_eligible",
    "pre_discharge_continue_eligible",
    "pre_discharge_transaction_ready",
    "pre_discharge_deadline",
    "pre_discharge_target_soc_percent",
    "pre_discharge_power_kw",
    "pre_discharge_power_percent",
    "planned_grid_discharge_kwh",
    "target_soc_before_risk_percent",
    "protected_minimum_soc_percent",
    "system_power_kw",
)
_PLAN_ATTRIBUTES_BY_KEY = {
    "rce_plan": RCE_PLAN_ATTRIBUTES,
    "tariff_plan": TARIFF_PLAN_ATTRIBUTES,
    "rcm_plan": RCM_PLAN_ATTRIBUTES,
}
_PLAN_BOOL_ATTRIBUTES = frozenset(
    {
        "result_current",
        "recalculation_pending",
        "current_slot_planned",
        "current_slot_start_eligible",
        "current_slot_continue_eligible",
        "current_run_start_eligible",
        "current_run_continue_eligible",
        "live_emergency",
        "emergency_action_ready",
        "prediction_ready",
        "risk_window_active",
        "charge_actuator_data_fresh",
        "export_actuator_data_fresh",
        "gcf_data_fresh",
        "bms_charge_data_fresh",
        "bms_charge_available",
        "system_power_data_valid",
        "pre_discharge_start_eligible",
        "pre_discharge_continue_eligible",
        "pre_discharge_transaction_ready",
    }
)
_PLAN_DATETIME_ATTRIBUTES = frozenset(
    {
        "current_slot_end",
        "current_run_end",
        "pre_discharge_deadline",
    }
)
_PLAN_PERCENT_ATTRIBUTES = frozenset(
    {
        "current_required_minimum_soc_percent",
        "command_charge_power_percent",
        "target_soc_percent",
        "base_reserve_soc_percent",
        "voltage_risk_score_percent",
        "recommended_charge_limit_percent",
        "recommended_export_limit_percent",
        "pre_discharge_target_soc_percent",
        "pre_discharge_power_percent",
        "target_soc_before_risk_percent",
        "protected_minimum_soc_percent",
    }
)
_PLAN_NONNEGATIVE_NUMBER_ATTRIBUTES = frozenset(
    {
        "current_slot_execution_discharge_power_kw",
        "current_slot_planned_export_kwh",
        "requested_charge_power_kw",
        "current_run_grid_import_kwh",
        "recommended_charge_power_kw",
        "pre_discharge_power_kw",
        "planned_grid_discharge_kwh",
        "system_power_kw",
    }
)


@dataclass(slots=True)
class _SupervisorGuard:
    sensors: dict[str, "HoymilesSupervisorSensor"]
    loaded_entry_count: int | None = None


def _loaded_entry_count(hass: HomeAssistant) -> int:
    domain_data = hass.data.get(DOMAIN, {})
    if not isinstance(domain_data, Mapping):
        return 0
    return sum(isinstance(value, RuntimeData) for value in domain_data.values())


@callback
def notify_supervisor_guard(hass: HomeAssistant) -> None:
    """Notify bounded sensor peers after a RuntimeData count transition."""
    guard = hass.data.get(_GUARD_KEY)
    if not isinstance(guard, _SupervisorGuard):
        guard = _SupervisorGuard(sensors={})
        hass.data[_GUARD_KEY] = guard
    count = _loaded_entry_count(hass)
    changed = guard.loaded_entry_count != count
    guard.loaded_entry_count = count
    if changed and count > 1:
        _LOGGER.warning(
            "EMS Supervisor is unavailable because %s config entries are loaded",
            count,
        )
    for sensor in tuple(guard.sensors.values()):
        sensor._async_loaded_entry_count_changed(count)
    if count == 0 and not guard.sensors:
        hass.data.pop(_GUARD_KEY, None)


def _state_text(state: State | None) -> str | None:
    return state.state if state is not None and type(state.state) is str else None


def _flag(state: State | None) -> bool:
    return _state_text(state) == "on"


def _tri_state(state: State | None) -> bool | None:
    value = _state_text(state)
    if value == "on":
        return True
    if value == "off":
        return False
    return None


def _timer_state(state: State | None) -> bool | None:
    value = _state_text(state)
    if value == "active":
        return True
    if value in {"idle", "paused"}:
        return False
    return None


def _state_number(
    state: State | None,
    *,
    minimum: float = -1_000_000_000.0,
    maximum: float = 1_000_000_000.0,
) -> float | None:
    value = _state_text(state)
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return 0.0 if number == 0.0 else number


def _plan_number(
    value: Any,
    *,
    minimum: float = -1_000_000_000.0,
    maximum: float = 1_000_000_000.0,
) -> float | None:
    if type(value) not in {int, float}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or not minimum <= number <= maximum:
        return None
    return 0.0 if number == 0.0 else number


def _percent_state(state: State | None) -> float | None:
    return _state_number(state, minimum=0.0, maximum=100.0)


def _percent_attr(value: Any) -> float | None:
    return _plan_number(value, minimum=0.0, maximum=100.0)


def _exact_bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _revision(value: Any) -> int | None:
    return (
        value
        if type(value) is int and 0 <= value <= 18_446_744_073_709_551_615
        else None
    )


def _aware_utc(value: Any) -> datetime | None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        return None
    return value.astimezone(timezone.utc)


def _raw_reported(state: State | None) -> datetime | None:
    if state is None:
        return None
    reported_value = getattr(state, "last_reported", None)
    if reported_value is not None:
        return _aware_utc(reported_value)
    return _aware_utc(getattr(state, "last_updated", None))


def _reported(state: State | None, now: datetime) -> datetime | None:
    reported = _raw_reported(state)
    return reported if reported is not None and reported <= now else None


def _iso_datetime(value: Any) -> datetime | None:
    if type(value) is not str:
        return None
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError, OverflowError):
        return None
    return _aware_utc(parsed)


def _input_datetime(state: State | None) -> datetime | None:
    if state is None:
        return None
    value = state.attributes.get("timestamp")
    if type(value) not in {int, float}:
        return None
    try:
        timestamp = float(value)
        if not math.isfinite(timestamp):
            return None
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except (OSError, OverflowError, TypeError, ValueError):
        return None


def _enum_value(enum_type: type[Any], value: Any) -> Any:
    if type(value) is not str:
        return None
    try:
        return enum_type(value)
    except ValueError:
        return None


def _bounded_plan_attribute(plan_key: str, attribute: str, value: Any) -> Any:
    if attribute in _PLAN_BOOL_ATTRIBUTES:
        return _exact_bool(value)
    if attribute == "input_revision":
        return _revision(value)
    if attribute in _PLAN_DATETIME_ATTRIBUTES:
        return _iso_datetime(value)
    if attribute in _PLAN_PERCENT_ATTRIBUTES:
        return _percent_attr(value)
    if attribute in _PLAN_NONNEGATIVE_NUMBER_ATTRIBUTES:
        return _plan_number(value, minimum=0.0)
    if attribute == "current_run_benefit_pln":
        return _plan_number(value)
    enum_type = {
        ("rce_plan", "status_code"): RcePlanStatus,
        ("tariff_plan", "status_code"): TariffPlanStatus,
        ("tariff_plan", "current_action"): TariffAction,
        ("tariff_plan", "current_run_need_class"): TariffRunNeed,
        ("rcm_plan", "action"): RcmAction,
    }.get((plan_key, attribute))
    return _enum_value(enum_type, value) if enum_type is not None else None


def _cohort_generation_time(
    states: tuple[State | None, ...],
    generation_state: State | None,
    generation_value: float | None,
    now: datetime,
) -> datetime | None:
    if (
        generation_value is None
        or not generation_value.is_integer()
        or not 1.0 <= generation_value <= _GENERATION_MAX
    ):
        return None
    timestamps = tuple(_reported(state, now) for state in states)
    if any(timestamp is None for timestamp in timestamps):
        return None
    concrete = tuple(timestamp for timestamp in timestamps if timestamp is not None)
    if (max(concrete) - min(concrete)).total_seconds() > _COHORT_SPAN_SECONDS:
        return None
    return _reported(generation_state, now)


def _is_fresh(
    observed_at: datetime | None,
    now: datetime,
    maximum_age_seconds: float,
) -> bool:
    return bool(
        observed_at is not None
        and 0.0 <= (now - observed_at).total_seconds() <= maximum_age_seconds
    )


def _close_active(left: float | None, right: float | None) -> bool:
    return bool(
        left is not None
        and right is not None
        and abs(left - right) < _READBACK_ACTIVE_TOLERANCE
    )


class HoymilesSupervisorSensor(SensorEntity):
    """Publish one deterministic, read-only Off/Shadow Supervisor decision."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_translation_key = "ems_supervisor"
    _attr_icon = "mdi:source-branch-check"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: RuntimeData,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_ems_supervisor"
        self._available = False
        self._native_value: str | None = None
        self._attributes: dict[str, Any] = {}
        self._serialized_summary: str | None = None
        self._source_entity_ids: dict[str, str | None] = {}
        self._keys_by_entity_id: dict[str, tuple[str, ...]] = {}
        self._state_unsub: Callable[[], None] | None = None
        self._registry_unsub: Callable[[], None] | None = None
        self._config_unsub: Callable[[], None] | None = None
        self._planner_cancel: Callable[[], None] | None = None
        self._temporal_cancel: Callable[[], None] | None = None
        self._planner_generation = 0
        self._temporal_generation = 0
        self._removed = False
        self._guard_ready = False
        self._warning_categories: set[str] = set()
        self._error_categories: set[str] = set()

    @property
    def suggested_object_id(self) -> str:
        """Return the frozen canonical object ID suggestion."""
        return "hoymiles_hit_ems_supervisor"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the Supervisor to this config entry's inverter device."""
        source = self._runtime.source_device
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=source.name_by_user or source.name or NAME,
            manufacturer=source.manufacturer or "Hoymiles",
            model=source.model or "HIT xxL G3",
            sw_version=source.sw_version,
        )

    @property
    def available(self) -> bool:
        """Return whether a fresh deterministic decision is published."""
        return self._available

    @property
    def native_value(self) -> str | None:
        """Return the exact core decision state."""
        return self._native_value

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return only the exact bounded core serializer projection."""
        return self._attributes

    async def async_added_to_hass(self) -> None:
        """Resolve sources, subscribe once and compute a fresh decision."""
        await super().async_added_to_hass()
        try:
            guard = self.hass.data.get(_GUARD_KEY)
            if not isinstance(guard, _SupervisorGuard):
                guard = _SupervisorGuard(sensors={})
                self.hass.data[_GUARD_KEY] = guard
            guard.sensors[self._entry.entry_id] = self
            notify_supervisor_guard(self.hass)
            self._resolve_source_entity_ids()
            self._replace_state_listener()
            self._registry_unsub = self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED,
                self._async_registry_updated,
            )
            self._config_unsub = self.hass.bus.async_listen(
                EVENT_CORE_CONFIG_UPDATE,
                self._async_core_config_updated,
            )
            self._guard_ready = True
            self._recompute(raise_on_error=True)
        except Exception:
            self._cleanup_lifecycle()
            raise

    async def async_will_remove_from_hass(self) -> None:
        """Cancel every callback before detaching this exact guard object."""
        self._cleanup_lifecycle()
        await super().async_will_remove_from_hass()

    def _cleanup_lifecycle(self) -> None:
        """Idempotently detach this exact lifecycle and clear its decision."""
        self._removed = True
        self._guard_ready = False
        self._planner_generation += 1
        self._temporal_generation += 1
        self._cancel_planner_callback()
        self._cancel_temporal_callback()
        self._unsubscribe("_state_unsub")
        self._unsubscribe("_registry_unsub")
        self._unsubscribe("_config_unsub")
        guard = self.hass.data.get(_GUARD_KEY)
        if (
            isinstance(guard, _SupervisorGuard)
            and guard.sensors.get(self._entry.entry_id) is self
        ):
            guard.sensors.pop(self._entry.entry_id, None)
            notify_supervisor_guard(self.hass)
        self._source_entity_ids = {}
        self._keys_by_entity_id = {}
        self._available = False
        self._native_value = None
        self._attributes = {}
        self._serialized_summary = None

    def _unsubscribe(self, attribute: str) -> None:
        unsubscribe = getattr(self, attribute)
        setattr(self, attribute, None)
        if unsubscribe is not None:
            unsubscribe()

    @callback
    def _async_loaded_entry_count_changed(self, count: int) -> None:
        if self._removed or not self._guard_ready:
            return
        if count != 1:
            self._cancel_planner_callback()
            self._cancel_temporal_callback()
            self._publish_unavailable()
            return
        self._recompute()

    def _resolve_source_entity_ids(self) -> None:
        registry = er.async_get(self.hass)
        resolved: dict[str, str | None] = {}
        for spec in SUPERVISOR_SOURCE_SPECS:
            if not spec.entry_local:
                resolved[spec.key] = spec.locator
                continue
            unique_id = f"{self._entry.entry_id}_{spec.locator}"
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, unique_id)
            registry_entry = (
                registry.async_get(entity_id) if entity_id is not None else None
            )
            if (
                registry_entry is None
                or registry_entry.domain != "sensor"
                or registry_entry.platform != DOMAIN
                or registry_entry.unique_id != unique_id
                or registry_entry.config_entry_id != self._entry.entry_id
                or registry_entry.translation_key != spec.locator
            ):
                entity_id = None
            resolved[spec.key] = entity_id
        self._source_entity_ids = resolved
        keys_by_id: dict[str, list[str]] = {}
        for key, entity_id in resolved.items():
            if entity_id is not None:
                keys_by_id.setdefault(entity_id, []).append(key)
        self._keys_by_entity_id = {
            entity_id: tuple(keys) for entity_id, keys in keys_by_id.items()
        }

    def _replace_state_listener(self) -> None:
        self._unsubscribe("_state_unsub")
        watched_ids = tuple(sorted(self._keys_by_entity_id))
        self._state_unsub = async_track_state_change_event(
            self.hass,
            watched_ids,
            self._async_source_state_changed,
        )

    @callback
    def _async_registry_updated(self, _event: Event) -> None:
        if self._removed:
            return
        previous = self._source_entity_ids.copy()
        self._resolve_source_entity_ids()
        if self._source_entity_ids == previous:
            return
        self._cancel_planner_callback()
        self._replace_state_listener()
        self._recompute()

    @callback
    def _async_core_config_updated(self, event: Event) -> None:
        if self._removed or "time_zone" not in event.data:
            return
        self._cancel_planner_callback()
        self._recompute()

    @callback
    def _async_source_state_changed(self, event: Event) -> None:
        if self._removed:
            return
        entity_id = event.data.get("entity_id")
        keys = self._keys_by_entity_id.get(entity_id, ())
        if not keys:
            return
        plan_keys = tuple(key for key in keys if key in _PLAN_ATTRIBUTES_BY_KEY)
        if plan_keys and all(
            self._plan_projection(event.data.get("old_state"), key)
            == self._plan_projection(event.data.get("new_state"), key)
            for key in plan_keys
        ):
            return
        if any(key not in _PLANNER_KEYS for key in keys):
            self._cancel_planner_callback()
            self._recompute()
            return
        self._schedule_planner_callback()

    @staticmethod
    def _plan_projection(state: State | None, key: str) -> tuple[Any, ...]:
        if state is None:
            return (None,)
        return (
            *(
                _bounded_plan_attribute(
                    key,
                    attribute,
                    state.attributes.get(attribute),
                )
                for attribute in _PLAN_ATTRIBUTES_BY_KEY[key]
            ),
            _raw_reported(state),
        )

    def _cancel_planner_callback(self) -> None:
        self._planner_generation += 1
        cancel = self._planner_cancel
        self._planner_cancel = None
        if cancel is not None:
            cancel()

    def _schedule_planner_callback(self) -> None:
        self._cancel_planner_callback()
        generation = self._planner_generation

        @callback
        def planner_callback(_now: datetime) -> None:
            if (
                self._removed
                or generation != self._planner_generation
            ):
                return
            self._planner_cancel = None
            self._recompute()

        self._planner_cancel = async_call_later(
            self.hass,
            _PLANNER_DELAY_SECONDS,
            planner_callback,
        )

    def _cancel_temporal_callback(self) -> None:
        self._temporal_generation += 1
        cancel = self._temporal_cancel
        self._temporal_cancel = None
        if cancel is not None:
            cancel()

    def _schedule_temporal_callback(
        self,
        now: datetime,
        boundaries: tuple[datetime, ...],
    ) -> None:
        future = tuple(boundary for boundary in boundaries if boundary > now)
        if not future or self._removed:
            self._cancel_temporal_callback()
            return
        nearest = min(future)
        generation = self._temporal_generation + 1

        @callback
        def temporal_callback(_now: datetime) -> None:
            if (
                self._removed
                or generation != self._temporal_generation
            ):
                return
            self._temporal_cancel = None
            if _loaded_entry_count(self.hass) != 1:
                self._publish_unavailable()
                return
            self._recompute()

        new_cancel = async_track_point_in_utc_time(
            self.hass,
            temporal_callback,
            nearest,
        )
        old_cancel = self._temporal_cancel
        self._temporal_generation = generation
        self._temporal_cancel = new_cancel
        if old_cancel is not None:
            old_cancel()

    def _warn_once(self, category: str, message: str) -> None:
        if category in self._warning_categories:
            return
        self._warning_categories.add(category)
        _LOGGER.warning(message)

    def _effective_mode(self, state: State | None) -> SupervisorMode:
        if state is None:
            return SupervisorMode.OFF
        value = _state_text(state)
        if value == "Off":
            return SupervisorMode.OFF
        if value == "Shadow":
            return SupervisorMode.SHADOW
        if value in {"Active", "active"}:
            self._warn_once(
                "active",
                "EMS Supervisor Active mode is not available; using Off",
            )
        elif value in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
            self._warn_once(
                "unknown_or_unavailable_mode",
                "EMS Supervisor mode is unknown or unavailable; using Off",
            )
        else:
            self._warn_once(
                "malformed_or_unsupported_mode",
                "EMS Supervisor mode is malformed or unsupported; using Off",
            )
        return SupervisorMode.OFF

    def _effective_profile(self, state: State | None) -> SupervisorProfile:
        if state is None:
            return SupervisorProfile.BALANCED
        value = _state_text(state)
        profiles = {
            "Balanced": SupervisorProfile.BALANCED,
            "Maximum Profit": SupervisorProfile.MAXIMUM_PROFIT,
            "High Reserve — Winter": SupervisorProfile.HIGH_RESERVE_WINTER,
        }
        profile = profiles.get(value)
        if profile is None:
            self._warn_once(
                "invalid_profile",
                "EMS Supervisor profile is invalid; using Balanced",
            )
            return SupervisorProfile.BALANCED
        return profile

    def _read_source_states(self) -> dict[str, State | None]:
        by_entity_id: dict[str, State | None] = {}
        states: dict[str, State | None] = {}
        for spec in SUPERVISOR_SOURCE_SPECS:
            entity_id = self._source_entity_ids.get(spec.key)
            if entity_id is None:
                states[spec.key] = None
                continue
            if entity_id not in by_entity_id:
                by_entity_id[entity_id] = self.hass.states.get(entity_id)
            states[spec.key] = by_entity_id[entity_id]
        return states

    @staticmethod
    def _attrs(states: Mapping[str, State | None], key: str) -> Mapping[str, Any]:
        state = states.get(key)
        return state.attributes if state is not None else {}

    def _build_snapshots(
        self,
        states: Mapping[str, State | None],
        now: datetime,
    ) -> tuple[
        SupervisorMode,
        SupervisorProfile,
        RceSourceSnapshot,
        TariffSourceSnapshot,
        RcmSourceSnapshot,
        ExecutionSourceSnapshot,
    ]:
        mode = self._effective_mode(states.get("supervisor_mode"))
        profile = self._effective_profile(states.get("supervisor_profile"))

        ems_generation = _state_number(
            states.get("ems_generation"),
            minimum=1.0,
            maximum=_GENERATION_MAX,
        )
        ems_values = (
            _state_number(states.get("ems_mode_readback"), minimum=0.0, maximum=65_535.0),
            _percent_state(states.get("discharge_power_readback")),
            _percent_state(states.get("discharge_soc_readback")),
            _percent_state(states.get("charge_power_ems_readback")),
            _percent_state(states.get("charge_soc_readback")),
        )
        ems_states = (
            states.get("ems_mode_readback"),
            states.get("ems_generation"),
            states.get("discharge_power_readback"),
            states.get("discharge_soc_readback"),
            states.get("charge_power_ems_readback"),
            states.get("charge_soc_readback"),
        )
        ems_generation_at = (
            _cohort_generation_time(
                ems_states,
                states.get("ems_generation"),
                ems_generation,
                now,
            )
            if all(value is not None for value in ems_values)
            else None
        )
        ems_coherent = ems_generation_at is not None

        gcf_enable = _state_number(
            states.get("gcf_enable_readback"), minimum=0.0, maximum=1.0
        )
        gcf_limit = _percent_state(states.get("gcf_export_limit_readback"))
        gcf_generation = _state_number(
            states.get("gcf_generation"),
            minimum=1.0,
            maximum=_GENERATION_MAX,
        )
        gcf_generation_at = (
            _cohort_generation_time(
                (
                    states.get("gcf_enable_readback"),
                    states.get("gcf_export_limit_readback"),
                    states.get("gcf_generation"),
                ),
                states.get("gcf_generation"),
                gcf_generation,
                now,
            )
            if gcf_enable is not None and gcf_limit is not None
            else None
        )
        gcf_coherent = gcf_generation_at is not None

        machine_type = _state_number(
            states.get("machine_type"), minimum=0.0, maximum=255.0
        )
        inverter_count = _state_number(
            states.get("inverter_count"), minimum=0.0, maximum=255.0
        )
        topology_generation = _state_number(
            states.get("topology_generation"),
            minimum=1.0,
            maximum=_GENERATION_MAX,
        )
        topology_generation_at = (
            _cohort_generation_time(
                (
                    states.get("machine_type"),
                    states.get("inverter_count"),
                    states.get("topology_generation"),
                ),
                states.get("topology_generation"),
                topology_generation,
                now,
            )
            if machine_type is not None and inverter_count is not None
            else None
        )

        execution = ExecutionSourceSnapshot(
            physical_mode_code=ems_values[0],
            full_block_generation_at=ems_generation_at,
            full_block_execution_ready=_flag(states.get("ems_execution_ready")),
            direct_306_execution_ready=_flag(states.get("direct_execution_ready")),
            direct_259_execution_ready=_flag(states.get("direct_execution_ready")),
            machine_type_code=machine_type,
            inverter_count=inverter_count,
            topology_generation_at=topology_generation_at,
            battery_soc_percent=_percent_state(states.get("battery_soc")),
            battery_soc_observed_at=_reported(states.get("battery_soc"), now),
            bms_voltage_v=_state_number(states.get("bms_voltage")),
            bms_voltage_observed_at=_reported(states.get("bms_voltage"), now),
            bms_max_charge_current_a=_state_number(states.get("bms_max_charge_current")),
            bms_charge_current_observed_at=_reported(states.get("bms_max_charge_current"), now),
            bms_max_discharge_current_a=_state_number(states.get("bms_max_discharge_current")),
            bms_discharge_current_observed_at=_reported(states.get("bms_max_discharge_current"), now),
            balancing_active=_tri_state(states.get("balancing_active")),
            manual_charge_active=_tri_state(states.get("manual_charge_active")),
            manual_discharge_active=_tri_state(states.get("manual_discharge_active")),
            rce_active=_tri_state(states.get("rce_active")),
            tariff_active=_tri_state(states.get("tariff_active")),
            rcm_active=_tri_state(states.get("rcm_active")),
            rcm_export_control_active=_tri_state(states.get("rcm_export_control_active")),
            rcm_pre_discharge_active=_tri_state(states.get("rcm_pre_discharge_active")),
            charge_timer_active=_timer_state(states.get("charge_timer")),
            discharge_timer_active=_timer_state(states.get("discharge_timer")),
            gcf_enable_code=gcf_enable,
            effective_export_limit_percent=gcf_limit,
            gcf_generation_at=gcf_generation_at,
            gcf_cohort_coherent=gcf_coherent,
            hardware_readback_supported=(
                (_state_number(states.get("hardware_readback_supported")) or 0.0)
                > 0.5
            ),
        )

        rce_attrs = self._attrs(states, "rce_plan")
        rce = RceSourceSnapshot(
            observed_at=_reported(states.get("rce_plan"), now),
            allowed_by_user=_flag(states.get("allow_rce")),
            enabled=_flag(states.get("rce_enabled")),
            active_latched=_tri_state(states.get("rce_active")),
            status_code=_enum_value(RcePlanStatus, rce_attrs.get("status_code")),
            result_current=_exact_bool(rce_attrs.get("result_current")),
            recalculation_pending=_exact_bool(rce_attrs.get("recalculation_pending")),
            input_revision=_revision(rce_attrs.get("input_revision")),
            current_slot_planned=_exact_bool(rce_attrs.get("current_slot_planned")),
            current_slot_start_eligible=_exact_bool(rce_attrs.get("current_slot_start_eligible")),
            current_slot_continue_eligible=_exact_bool(rce_attrs.get("current_slot_continue_eligible")),
            current_slot_end=_iso_datetime(rce_attrs.get("current_slot_end")),
            current_run_end=_iso_datetime(rce_attrs.get("current_run_end")),
            requested_discharge_power_kw=_plan_number(rce_attrs.get("current_slot_execution_discharge_power_kw"), minimum=0.0),
            planned_export_energy_kwh=_plan_number(rce_attrs.get("current_slot_planned_export_kwh"), minimum=0.0),
            protected_soc_floor_percent=_percent_attr(rce_attrs.get("current_required_minimum_soc_percent")),
            effective_discharge_power_percent=_percent_state(states.get("rce_effective_discharge_power")),
            current_soc_percent=execution.battery_soc_percent,
            control_data_ready=_tri_state(states.get("rce_control_data_ready")),
            price_above_threshold=_tri_state(states.get("rce_price_above_threshold")),
            reserve_ready=_tri_state(states.get("rce_reserve_ready")),
            sale_block_active=_tri_state(states.get("sale_block_active")),
            latched_slot_end=_input_datetime(states.get("rce_latched_slot_end")),
            latched_minimum_soc_percent=_percent_state(states.get("rce_latched_minimum_soc")),
            active_4305_readback_percent=(ems_values[2] if ems_coherent else None),
            active_4306_readback_percent=(ems_values[1] if ems_coherent else None),
        )

        tariff_attrs = self._attrs(states, "tariff_plan")
        tariff = TariffSourceSnapshot(
            observed_at=_reported(states.get("tariff_plan"), now),
            allowed_by_user=_flag(states.get("allow_tariff")),
            enabled=_flag(states.get("tariff_enabled")),
            active_latched=_tri_state(states.get("tariff_active")),
            status_code=_enum_value(TariffPlanStatus, tariff_attrs.get("status_code")),
            result_current=_exact_bool(tariff_attrs.get("result_current")),
            recalculation_pending=_exact_bool(tariff_attrs.get("recalculation_pending")),
            input_revision=_revision(tariff_attrs.get("input_revision")),
            current_slot_planned=_exact_bool(tariff_attrs.get("current_slot_planned")),
            current_action=_enum_value(TariffAction, tariff_attrs.get("current_action")),
            current_run_need_class=_enum_value(TariffRunNeed, tariff_attrs.get("current_run_need_class")),
            current_run_start_eligible=_exact_bool(tariff_attrs.get("current_run_start_eligible")),
            current_run_continue_eligible=_exact_bool(tariff_attrs.get("current_run_continue_eligible")),
            requested_charge_power_kw=_plan_number(tariff_attrs.get("requested_charge_power_kw"), minimum=0.0),
            command_charge_power_percent=_percent_attr(tariff_attrs.get("command_charge_power_percent")),
            current_run_grid_import_kwh=_plan_number(tariff_attrs.get("current_run_grid_import_kwh"), minimum=0.0),
            current_run_benefit_pln=_plan_number(tariff_attrs.get("current_run_benefit_pln")),
            target_soc_percent=_percent_attr(tariff_attrs.get("target_soc_percent")),
            base_reserve_soc_percent=_percent_attr(tariff_attrs.get("base_reserve_soc_percent")),
            current_slot_end=_iso_datetime(tariff_attrs.get("current_slot_end")),
            active_action=_enum_value(TariffAction, _state_text(states.get("tariff_active_action"))),
            latched_slot_end=_input_datetime(states.get("tariff_latched_slot_end")),
            latched_target_soc_percent=_percent_state(states.get("tariff_latched_target_soc")),
            control_data_ready=_tri_state(states.get("tariff_control_data_ready")),
            planned_slot_ready=_tri_state(states.get("tariff_planned_charge_slot")),
            active_4303_readback_percent=(ems_values[4] if ems_coherent else None),
            active_4304_readback_percent=(ems_values[3] if ems_coherent else None),
        )

        rcm_attrs = self._attrs(states, "rcm_plan")
        charge_readback = _percent_state(states.get("charge_power_readback"))
        charge_reported = _reported(states.get("charge_power_readback"), now)
        recommended_charge = _percent_attr(rcm_attrs.get("recommended_charge_limit_percent"))
        absorb_active = _tri_state(states.get("rcm_active"))
        charge_path_valid = bool(
            _exact_bool(rcm_attrs.get("charge_actuator_data_fresh")) is True
            and _exact_bool(rcm_attrs.get("bms_charge_data_fresh")) is True
            and _exact_bool(rcm_attrs.get("bms_charge_available")) is True
            and _exact_bool(rcm_attrs.get("system_power_data_valid")) is True
            and charge_readback is not None
            and _is_fresh(charge_reported, now, 300.0)
            and (
                absorb_active is not True
                or _close_active(charge_readback, recommended_charge)
            )
        )
        current_export_fresh = bool(
            gcf_coherent and _is_fresh(gcf_generation_at, now, 180.0)
        )
        export_path_valid = bool(
            _exact_bool(rcm_attrs.get("export_actuator_data_fresh")) is True
            and _exact_bool(rcm_attrs.get("gcf_data_fresh")) is True
            and current_export_fresh
        )
        pre_active = _tri_state(states.get("rcm_pre_discharge_active"))
        latched_target = _percent_state(states.get("rcm_latched_pre_discharge_target_soc"))
        latched_power_percent = _percent_state(states.get("rcm_latched_pre_discharge_power"))
        system_power_kw = _plan_number(rcm_attrs.get("system_power_kw"), minimum=0.0)
        latched_power_kw = (
            latched_power_percent * system_power_kw / 100.0
            if latched_power_percent is not None and system_power_kw is not None
            else None
        )
        pre_readback_coherent = bool(
            ems_coherent
            and _close_active(ems_values[2], latched_target)
            and _close_active(ems_values[1], latched_power_percent)
        )
        pre_start = _exact_bool(rcm_attrs.get("pre_discharge_start_eligible"))
        pre_transaction = _exact_bool(rcm_attrs.get("pre_discharge_transaction_ready"))
        sun_above = _state_text(states.get("sun")) == "above_horizon"
        pre_continue = _exact_bool(rcm_attrs.get("pre_discharge_continue_eligible"))

        rcm = RcmSourceSnapshot(
            observed_at=_reported(states.get("rcm_plan"), now),
            allowed_by_user=_flag(states.get("allow_rcm")),
            enabled=(
                _flag(states.get("rcm_enabled"))
                and _tri_state(states.get("rcm_shadow_mode")) is False
            ),
            result_current=_exact_bool(rcm_attrs.get("result_current")),
            recalculation_pending=_exact_bool(rcm_attrs.get("recalculation_pending")),
            input_revision=_revision(rcm_attrs.get("input_revision")),
            live_emergency=_exact_bool(rcm_attrs.get("live_emergency")),
            emergency_action_ready=_exact_bool(rcm_attrs.get("emergency_action_ready")),
            prediction_ready=_exact_bool(rcm_attrs.get("prediction_ready")),
            action=_enum_value(RcmAction, rcm_attrs.get("action")),
            risk_window_active=_exact_bool(rcm_attrs.get("risk_window_active")),
            voltage_risk_score_percent=_percent_attr(rcm_attrs.get("voltage_risk_score_percent")),
            recommended_charge_limit_percent=recommended_charge,
            recommended_charge_power_kw=_plan_number(rcm_attrs.get("recommended_charge_power_kw"), minimum=0.0),
            recommended_export_limit_percent=_percent_attr(rcm_attrs.get("recommended_export_limit_percent")),
            current_export_limit_percent=(gcf_limit if gcf_coherent else None),
            current_export_limit_fresh=current_export_fresh,
            charge_path_locally_valid=charge_path_valid,
            export_path_locally_valid=export_path_valid,
            direct_register_topology_allowed=None,
            full_block_topology_allowed=None,
            export_control_enabled=_tri_state(states.get("rcm_export_control_enabled")),
            pre_discharge_enabled=_tri_state(states.get("rcm_pre_discharge_enabled")),
            absorb_active=absorb_active,
            export_active=_tri_state(states.get("rcm_export_control_active")),
            pre_discharge_active=pre_active,
            pre_discharge_start_eligible=bool(
                pre_start is True and pre_transaction is True and sun_above
            ),
            pre_discharge_continue_eligible=bool(
                pre_continue is True
                and (pre_active is not True or pre_readback_coherent)
            ),
            pre_discharge_deadline=_iso_datetime(rcm_attrs.get("pre_discharge_deadline")),
            pre_discharge_target_soc_percent=_percent_attr(rcm_attrs.get("pre_discharge_target_soc_percent")),
            pre_discharge_power_kw=_plan_number(rcm_attrs.get("pre_discharge_power_kw"), minimum=0.0),
            pre_discharge_power_percent=_percent_attr(rcm_attrs.get("pre_discharge_power_percent")),
            planned_grid_discharge_kwh=_plan_number(rcm_attrs.get("planned_grid_discharge_kwh"), minimum=0.0),
            target_soc_before_risk_percent=_percent_attr(rcm_attrs.get("target_soc_before_risk_percent")),
            protected_minimum_soc_percent=_percent_attr(rcm_attrs.get("protected_minimum_soc_percent")),
            latched_pre_discharge_deadline=_input_datetime(states.get("rcm_latched_pre_discharge_deadline")),
            latched_pre_discharge_target_soc_percent=latched_target,
            latched_pre_discharge_power_kw=latched_power_kw,
            latched_pre_discharge_power_percent=latched_power_percent,
            sale_block_active=_tri_state(states.get("sale_block_active")),
            export_state=None,
        )
        return mode, profile, rce, tariff, rcm, execution

    def _semantic_boundaries(
        self,
        states: Mapping[str, State | None],
        now: datetime,
        rce: RceSourceSnapshot,
        tariff: TariffSourceSnapshot,
        rcm: RcmSourceSnapshot,
        execution: ExecutionSourceSnapshot,
        candidates: tuple[PolicyCandidate, ...],
    ) -> tuple[datetime, ...]:
        boundaries: set[datetime] = set()

        def freshness(observed_at: datetime | None, seconds: float) -> None:
            if observed_at is not None:
                boundaries.add(observed_at + timedelta(seconds=seconds) + _MICROSECOND)

        freshness(rce.observed_at, 300.0)
        freshness(tariff.observed_at, 300.0)
        freshness(rcm.observed_at, 60.0)
        freshness(execution.full_block_generation_at, 180.0)
        freshness(execution.gcf_generation_at, 180.0)
        freshness(execution.topology_generation_at, 180.0)
        freshness(execution.battery_soc_observed_at, 120.0)
        freshness(execution.bms_voltage_observed_at, 300.0)
        freshness(execution.bms_charge_current_observed_at, 300.0)
        freshness(execution.bms_discharge_current_observed_at, 300.0)
        freshness(_reported(states.get("charge_power_readback"), now), 300.0)

        for state in states.values():
            reported = _raw_reported(state)
            if reported is not None and reported > now:
                boundaries.add(reported)
        for candidate in candidates:
            if candidate.observed_at > now:
                boundaries.add(candidate.observed_at)
            if candidate.valid_from is not None:
                boundaries.add(candidate.valid_from)
            if candidate.valid_until is not None:
                boundaries.add(candidate.valid_until)
        consumed_deadlines: tuple[datetime | None, ...] = ()
        if rce.active_latched is True:
            consumed_deadlines += (rce.latched_slot_end,)
        elif rce.current_slot_planned is True:
            consumed_deadlines += (rce.current_slot_end, rce.current_run_end)
        if tariff.active_latched is True:
            consumed_deadlines += (tariff.latched_slot_end,)
        elif tariff.current_slot_planned is True:
            consumed_deadlines += (tariff.current_slot_end,)
        if rcm.action is RcmAction.GRID_DISCHARGE_PREPARATION:
            consumed_deadlines += (
                rcm.latched_pre_discharge_deadline
                if rcm.pre_discharge_active is True
                else rcm.pre_discharge_deadline,
            )
        for boundary in consumed_deadlines:
            if boundary is not None:
                boundaries.add(boundary)
        if (
            rce.active_latched is not True
            and rce.current_slot_planned is True
            and rce.current_slot_end is not None
        ):
            boundaries.add(
                rce.current_slot_end - timedelta(seconds=300.0) + _MICROSECOND
            )
        if (
            tariff.active_latched is not True
            and tariff.current_slot_planned is True
            and tariff.current_slot_end is not None
        ):
            boundaries.add(
                tariff.current_slot_end - timedelta(seconds=420.0) + _MICROSECOND
            )
        return tuple(boundaries)

    def _publish_decision(
        self,
        state: str,
        serialized: str,
        attributes: dict[str, Any],
    ) -> None:
        changed = (
            not self._available
            or self._native_value != state
            or self._serialized_summary != serialized
        )
        self._available = True
        self._native_value = state
        self._attributes = attributes
        self._serialized_summary = serialized
        if changed and not self._removed:
            self.async_write_ha_state()

    def _publish_unavailable(self) -> None:
        changed = self._available
        self._available = False
        self._native_value = None
        self._attributes = {}
        self._serialized_summary = None
        if changed and not self._removed:
            self.async_write_ha_state()

    def _adapter_failed(self, category: str) -> None:
        self._cancel_temporal_callback()
        self._publish_unavailable()
        if category not in self._error_categories:
            self._error_categories.add(category)
            _LOGGER.error("EMS Supervisor adapter failed closed (%s)", category)

    def _recompute(self, *, raise_on_error: bool = False) -> None:
        if self._removed or not self._guard_ready:
            return
        if _loaded_entry_count(self.hass) != 1:
            self._cancel_planner_callback()
            self._cancel_temporal_callback()
            self._publish_unavailable()
            return
        stage = "source_snapshot"
        try:
            now = _aware_utc(dt_util.utcnow())
            if now is None:
                raise ValueError("UTC clock returned a naive value")
            states = self._read_source_states()
            mode, profile, rce, tariff, rcm, execution = self._build_snapshots(
                states,
                now,
            )
            stage = "execution_context"
            context = build_execution_context(execution, now=now)
            rcm = replace(
                rcm,
                export_state=context.export_state,
                direct_register_topology_allowed=(
                    context.topology_direct_register_allowed
                ),
                full_block_topology_allowed=context.topology_full_block_allowed,
            )
            stage = "candidate_builders"
            candidates = (
                build_rce_candidate(rce, now=now),
                build_tariff_candidate(tariff, now=now),
                build_rcm_candidate(rcm, now=now),
            )
            stage = "arbiter"
            decision = arbitrate_supervisor(
                mode=mode,
                profile=profile,
                context=context,
                candidates=candidates,
                now=now,
            )
            stage = "serializer"
            serialized = serialize_supervisor_summary(decision)
            if type(serialized) is not str:
                raise ValueError("serialized summary must be text")
            attributes = json.loads(serialized)
            if type(attributes) is not dict:
                raise ValueError("serialized summary must be an object")
            stage = "temporal_scheduler"
            boundaries = self._semantic_boundaries(
                states,
                now,
                rce,
                tariff,
                rcm,
                execution,
                candidates,
            )
            self._schedule_temporal_callback(now, boundaries)
            self._publish_decision(
                decision.state.value,
                serialized,
                attributes,
            )
        except (TypeError, ValueError, OverflowError, json.JSONDecodeError):
            self._adapter_failed(stage)
            if raise_on_error:
                raise
        except Exception:  # noqa: BLE001 - all adapter errors fail closed
            category = (
                "temporal_scheduler"
                if stage == "temporal_scheduler"
                else f"unexpected_{stage}"
            )
            self._adapter_failed(category)
            if raise_on_error:
                raise


__all__ = (
    "HoymilesSupervisorSensor",
    "RCM_PLAN_ATTRIBUTES",
    "RCE_PLAN_ATTRIBUTES",
    "SUPERVISOR_SOURCE_SPECS",
    "TARIFF_PLAN_ATTRIBUTES",
    "notify_supervisor_guard",
)
