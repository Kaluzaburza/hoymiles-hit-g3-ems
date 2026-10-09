"""Event-driven Off/Active Home Assistant adapter for EMS Supervisor V1."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
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
    async_track_state_report_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME
from .ems_supervisor import (
    ExportState,
    OwnerKind,
    PolicyCandidate,
    RequestedAction,
    SupervisorMode,
    SupervisorProfile,
    SupervisorState,
    apply_minimum_grid_power,
    apply_minimum_tariff_power,
    arbitrate_supervisor,
    serialize_supervisor_summary,
)
from .models import RuntimeData
from .supervisor_active_bridge import (
    ActiveBridgeError,
    authorization_matches,
    correlated_observed_owner,
    execution_gates,
    physical_verification,
    rcm_charge_command_within_live_bms_limit,
    rcm_pre_discharge_command_within_live_bms_limit,
    rce_sent_command_within_live_bms_limit,
    same_window_rce_wait_authorized,
    same_run_tariff_desired,
    settings_from_execution_source,
    tariff_command_within_live_bms_limit,
)
from .supervisor_active_controller import (
    ActiveFrame,
    RetargetPendingContext,
    SupervisorActiveController,
)
from .supervisor_executor import (
    ActiveState,
    AtomicWrite,
    AtomicWriteFamily,
    AtomicWriteNotQueued,
    COMMAND_ACK_TIMEOUT_SECONDS,
    COMMAND_DISPATCH_TIMEOUT_SECONDS,
    EmsBlock,
    EmsMode,
    ExecutionAction,
    ExecutionGates,
    ExecutionOwner,
    ExecutionReason,
    ExecutorRecord,
    MAX_READBACK_AGE_SECONDS,
    MasterStopStatus,
    RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS,
    TARIFF_PHYSICAL_MAX_AGE_SECONDS,
    RollbackStatus,
    SettingsSnapshot,
    TransactionRecord,
    VerificationStatus,
)
from .supervisor_executor_codec import record_from_dict, record_to_dict
from .supervisor_control_lease import (
    CONTROL_LEASE_PROTOCOL_VERSION,
    CONTROL_LEASE_RENEW_SECONDS,
    CONTROL_LEASE_TTL_SECONDS,
    ControlLeaseClient,
    ControlLeaseRenewStatus,
)
from .source_device import linked_config_entry_ids
from .execution_history import (
    RECORDED_EXECUTION_ATTRIBUTE,
    SUPERVISOR_UNRECORDED_ATTRIBUTES,
    supervisor_recorder_projection,
    RECORDED_STOP_ATTRIBUTE, stop_recorder_projection,
)
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
from .tariff_optimizer import TariffActiveCommitment
from .rce_optimizer import RceActiveCommitment


_LOGGER = logging.getLogger(__name__)
_GUARD_KEY = f"{DOMAIN}_ems_supervisor_guard"
_PLANNER_DELAY_SECONDS = 0.100
_READBACK_COHORT_DELAY_SECONDS = 0.100
# Native PV -> BAT -> GRID -> LOAD reports take about 260 ms in the captured
# field sequences.  This window is anchored at the first report (not rearmed
# at every edge), so a stalled channel cannot postpone the decision forever.
_POWER_COHORT_COLLECTION_SECONDS = 0.350
_COHORT_SPAN_SECONDS = 5.0
_GENERATION_MAX = 16_000_000.0
_READBACK_ACTIVE_TOLERANCE = 1.0
_MICROSECOND = timedelta(microseconds=1)
_EXECUTOR_STORAGE_VERSION = 1
_EXECUTOR_STORAGE_KEY_PREFIX = f"{DOMAIN}.supervisor_executor_v1"
_MASTER_STOP_STORAGE_VERSION = 1
_MASTER_STOP_STORAGE_KEY_PREFIX = f"{DOMAIN}.supervisor_master_stop_v1"
_MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS = 10.0
_MASTER_STOP_CONTINUATION_DELAY_SECONDS = 5.0
_CONTROL_LEASE_PROTOCOL_VERSION = CONTROL_LEASE_PROTOCOL_VERSION
_EXECUTION_HEALTH_SCHEMA_VERSION = 1
_EXECUTION_HEALTH_MAX_READ_AGE_SECONDS = 180.0
_ESPHOME_DOMAIN = "esphome"
_ESPHOME_DEVICE_NAME_KEY = "device_name"
_EMS_PRETRANSPORT_REJECTIONS = frozenset({
    "previous_write_pending",
    "hardware_not_ready",
    "generation_unavailable",
    "stale_snapshot_generation",
    "mode_unavailable",
    "invalid_block",
    "stale_snapshot",
    "topology_unavailable",
    "topology_invalid",
    "topology_nonintegral",
    "slave_topology",
    "topology_unsupported",
    "machine_count_unavailable",
    "machine_count_invalid",
    "machine_count_nonintegral",
    "machine_count_out_of_range",
    "control_lease_required",
    "lease_store_unavailable",
    "lease_already_active",
    "stale_or_invalid_nonce",
    "invalid_lease_identity",
    "invalid_hard_deadline",
    "fallback_not_self_use",
    "retarget_not_authorized",
    "lease_store_failed",
})
SUPERVISOR_ENTITY_ID = "sensor.hoymiles_hit_ems_supervisor"
EMS_PAUSED_ENTITY_ID = "input_boolean.hoymiles_ems_paused"
_SUPERVISOR_MODE_ENTITY_ID = "input_select.hoymiles_ems_supervisor_mode"
_POLICY_HELPERS = {
    "rce": (
        "input_boolean.hoymiles_ems_policy_preference_rce",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_ems_supervisor_allow_rce",
    ),
    "tariff": (
        "input_boolean.hoymiles_ems_policy_preference_tariff",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "input_boolean.hoymiles_ems_supervisor_allow_tariff",
    ),
    "rcm": (
        "input_boolean.hoymiles_ems_policy_preference_rcm",
        "input_boolean.hoymiles_rcm_enabled",
        "input_boolean.hoymiles_ems_supervisor_allow_rcm",
    ),
    "balancing": (
        "input_boolean.hoymiles_ems_policy_preference_balancing",
        "input_boolean.hoymiles_battery_balancing_enabled",
        None,
    ),
}
_MASTER_STOP_INPUT_BOOLEANS = (
    "input_boolean.hoymiles_ems_supervisor_allow_rce",
    "input_boolean.hoymiles_ems_supervisor_allow_tariff",
    "input_boolean.hoymiles_ems_supervisor_allow_rcm",
    "input_boolean.hoymiles_discharge_schedule_enabled",
    "input_boolean.hoymiles_charge_schedule_enabled",
    "input_boolean.hoymiles_discharge_cycle_active",
    "input_boolean.hoymiles_charge_cycle_active",
    "input_boolean.hoymiles_rce_discharge_enabled",
    "input_boolean.hoymiles_rce_discharge_active",
    "input_boolean.hoymiles_tariff_charge_enabled",
    "input_boolean.hoymiles_tariff_charge_active",
    "input_boolean.hoymiles_rcm_enabled",
    "input_boolean.hoymiles_rcm_export_control_enabled",
    "input_boolean.hoymiles_rcm_pre_discharge_enabled",
    "input_boolean.hoymiles_rcm_active",
    "input_boolean.hoymiles_rcm_export_control_active",
    "input_boolean.hoymiles_rcm_pre_discharge_active",
    "input_boolean.hoymiles_battery_balancing_enabled",
    "input_boolean.hoymiles_battery_balancing_active",
)
_MASTER_STOP_TIMERS = (
    "timer.hoymiles_charge",
    "timer.hoymiles_discharge",
)


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
    (24, "rcm_export_control_enabled", "input_boolean.hoymiles_rcm_export_control_enabled", False, True, False),
    (25, "rcm_pre_discharge_enabled", "input_boolean.hoymiles_rcm_pre_discharge_enabled", False, True, False),
    (26, "rcm_active", "input_boolean.hoymiles_rcm_active", False, False, False),
    (27, "rcm_export_control_active", "input_boolean.hoymiles_rcm_export_control_active", False, False, False),
    (28, "rcm_pre_discharge_active", "input_boolean.hoymiles_rcm_pre_discharge_active", False, False, False),
    (29, "rcm_latched_pre_discharge_deadline", "input_datetime.hoymiles_rcm_latched_pre_discharge_deadline", False, False, False),
    (30, "rcm_latched_pre_discharge_target_soc", "input_number.hoymiles_rcm_latched_pre_discharge_target_soc", False, False, False),
    (31, "rcm_latched_pre_discharge_power", "input_number.hoymiles_rcm_latched_pre_discharge_power", False, False, False),
    (32, "sun", "sun.sun", False, False, False),
    (33, "manual_discharge_active", "input_boolean.hoymiles_discharge_cycle_active", False, False, False),
    (34, "manual_charge_active", "input_boolean.hoymiles_charge_cycle_active", False, False, False),
    (35, "balancing_active", "input_boolean.hoymiles_battery_balancing_active", False, False, False),
    (36, "discharge_timer", "timer.hoymiles_discharge", False, False, False),
    (37, "charge_timer", "timer.hoymiles_charge", False, False, False),
    (38, "sale_block_active", "binary_sensor.hoymiles_sale_block_active", False, False, False),
    (39, "ems_mode_readback", "ems_mode_readback_code", True, False, False),
    (40, "ems_generation", "ems_control_readback_generation", True, False, False),
    (41, "ems_execution_ready", "binary_sensor.hoymiles_ems_execution_ready", False, False, False),
    (42, "direct_execution_ready", "binary_sensor.hoymiles_direct_register_execution_ready", False, False, False),
    (43, "gcf_enable_readback", "gcf_enable_readback_code", True, False, False),
    (44, "gcf_export_limit_readback", "gcf_maximum_export_power_readback", True, False, False),
    (45, "gcf_generation", "gcf_control_readback_generation", True, False, False),
    (46, "hardware_readback_supported", "ems_verified_hardware_readback_supported", True, False, False),
    (47, "battery_soc", "overview_battery_soc", True, False, False),
    (48, "bms_voltage", "battery_voltage_bms", True, False, False),
    (49, "bms_max_charge_current", "maximum_charge_current", True, False, False),
    (50, "bms_max_discharge_current", "maximum_discharge_current", True, False, False),
    (51, "machine_type", "machines_type", True, False, False),
    (52, "inverter_count", "number_of_machines_master_and_slave", True, False, False),
    (53, "topology_generation", "parallel_topology_readback_generation", True, False, False),
    (54, "charge_power_readback", "battery_max_charge_power_readback", True, False, False),
    (55, "discharge_power_readback", "ems_maximum_discharge_power_readback", True, False, False),
    (56, "discharge_soc_readback", "ems_force_discharge_soc_readback", True, False, False),
    (57, "charge_power_ems_readback", "ems_maximum_charge_power_readback", True, False, False),
    (58, "charge_soc_readback", "ems_force_charge_soc_readback", True, False, False),
    (59, "rce_effective_discharge_power", "sensor.hoymiles_rce_effective_discharge_power_percent", False, False, False),
    (60, "self_use_soc_readback", "ems_self_use_soc_readback", True, False, False),
    (61, "backup_soc_readback", "ems_backup_soc_readback", True, False, False),
    (62, "battery_charge_generation", "battery_charge_power_readback_generation", True, False, False),
    (63, "grid_to_battery_power", "sensor.hoymiles_hit_grid_to_battery_power", False, False, False),
    (64, "grid_power", "overview_grid_total_active_power", True, False, False),
    (65, "battery_power", "overview_battery_power", True, False, False),
    (66, "pv_power", "overview_pv_total_power", True, False, False),
    (67, "load_power", "overview_load_active_power", True, False, False),
    (68, "tariff_maximum_soc", "input_number.hoymiles_tariff_maximum_soc", False, False, False),
    (69, "rce_requested_discharge_power", "input_number.hoymiles_rce_requested_discharge_power", False, False, False),
    (70, "pv_charge_delay_enabled", "input_boolean.hoymiles_pv_charge_delay_enabled", False, True, False),
    (71, "bms_battery_power", "battery_power_bms", True, False, False),
)

SUPERVISOR_SOURCE_SPECS = tuple(
    SupervisorSourceSpec(*row) for row in _SOURCE_ROWS
)
_SOURCE_BY_KEY = {spec.key: spec for spec in SUPERVISOR_SOURCE_SPECS}
_PLANNER_KEYS = frozenset(
    spec.key for spec in SUPERVISOR_SOURCE_SPECS if spec.planner_event
)
_READBACK_COHORT_KEYS = frozenset(
    {
        "ems_mode_readback",
        "ems_generation",
        "self_use_soc_readback",
        "backup_soc_readback",
        "discharge_power_readback",
        "discharge_soc_readback",
        "charge_power_ems_readback",
        "charge_soc_readback",
        "gcf_enable_readback",
        "gcf_export_limit_readback",
        "gcf_generation",
        "charge_power_readback",
        "battery_charge_generation",
        "machine_type",
        "inverter_count",
        "topology_generation",
    }
)
_POWER_FLOW_COHORT_KEYS = frozenset(
    {"grid_power", "battery_power", "pv_power", "load_power"}
)
_ENTRY_LOCAL_SPECS = tuple(
    spec for spec in SUPERVISOR_SOURCE_SPECS if spec.entry_local
)

RCE_PLAN_ATTRIBUTES = (
    "pv_charge_delay_execution_ready", "pv_charge_delay_end",
    "rce_today_data_fresh", "forecast_today_data_fresh", "soc_data_fresh", "gcf_execution_data_fresh",
    "price_provider", "joint_plan_revision", "joint_profile_revision",
    "current_slot_execution_export_power_kw",
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
    "system_power_kw",
    "current_slot_suppression_reason",
    "current_slot_load_exhausts_requested_discharge_budget",
    "current_slot_load_only_export_suppressed",
    "post_command_settling_market_fingerprint",
)
TARIFF_PLAN_ATTRIBUTES = (
    "price_provider", "joint_plan_revision", "joint_profile_revision",
    "current_slot_planned_import_power_kw",
    "current_slot_planned_charge_power_kw",
    "status_code",
    "result_current",
    "recalculation_pending",
    "input_revision",
    "current_slot_planned",
    "current_action",
    "current_run_need_class",
    "current_run_start_eligible",
    "current_run_continue_eligible",
    "current_run_suppression_reason",
    "current_run_continue_reason",
    "requested_charge_power_kw",
    "system_power_kw",
    "command_charge_power_percent",
    "current_run_grid_import_kwh",
    "requested_target_energy_kwh",
    "demand_margin_requested_kwh",
    "demand_margin_unserved_kwh",
    "base_energy_shortfall_kwh",
    "current_run_benefit_pln",
    "target_soc_percent",
    "base_reserve_soc_percent",
    "current_slot_end",
    "current_grid_charge_run_end",
    "control_inputs_fresh",
    "forecast_data_fresh",
    "control_input_block_reason",
    "load_profile_source",
    "configured_daily_fallback_kwh",
    "input_change_reason",
    "input_change_previous_value",
    "input_change_new_value",
    "bms_charge_power_limit_kw",
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
        "rce_today_data_fresh", "forecast_today_data_fresh", "soc_data_fresh", "gcf_execution_data_fresh",
        "pv_charge_delay_execution_ready",
        "result_current",
        "recalculation_pending",
        "current_slot_planned",
        "current_slot_start_eligible",
        "current_slot_continue_eligible",
        "current_slot_load_exhausts_requested_discharge_budget",
        "current_slot_load_only_export_suppressed",
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
        "pv_charge_delay_end",
        "current_slot_end",
        "current_run_end",
        "current_grid_charge_run_end",
        "pre_discharge_deadline",
    }
)
_PLAN_TEXT_ATTRIBUTES = frozenset(
    {
        "current_run_suppression_reason",
        "current_run_continue_reason",
        "control_input_block_reason",
        "load_profile_source",
        "input_change_reason",
        "input_change_previous_value",
        "input_change_new_value",
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
        "current_slot_execution_export_power_kw",
        "current_slot_planned_import_power_kw",
        "current_slot_planned_charge_power_kw",
        "current_slot_execution_discharge_power_kw",
        "current_slot_planned_export_kwh",
        "requested_charge_power_kw",
        "current_run_grid_import_kwh",
        "requested_target_energy_kwh",
        "demand_margin_requested_kwh",
        "demand_margin_unserved_kwh",
        "base_energy_shortfall_kwh",
        "configured_daily_fallback_kwh",
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
    control_lock: asyncio.Lock | None = None
    control_generation: int = 0


_SAFE_OFF_LEGACY_PROXY_OWNERS = frozenset(
    {
        OwnerKind.NONE,
        OwnerKind.MANUAL,
        OwnerKind.BALANCING,
        OwnerKind.RCE,
        OwnerKind.TARIFF,
        OwnerKind.RCM,
    }
)


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


@callback
def manual_ems_proxy_write_allowed(
    hass: HomeAssistant,
    entry_id: str,
) -> bool:
    """Release one known legacy writer after the Supervisor safe-Off handoff.

    The Active executor uses separate generation-bound ESPHome actions and
    therefore never relies on this manual/proxy authority path.  A single
    known legacy owner must remain latched until its physical write or rollback
    is acknowledged; foreign, unknown, and conflicting ownership stays closed.
    """

    guard = hass.data.get(_GUARD_KEY)
    if (
        type(entry_id) is not str
        or not entry_id
        or not isinstance(guard, _SupervisorGuard)
        or guard.loaded_entry_count != 1
        or _loaded_entry_count(hass) != 1
        or len(guard.sensors) != 1
    ):
        return False
    sensor = guard.sensors.get(entry_id)
    if (
        sensor is None
        or sensor._removed
        or sensor._unloading
        or not sensor._guard_ready
        or sensor._master_stop_latched
        or sensor._controller is None
        or getattr(sensor._controller, "manual_proxy_readback_pending", True)
        or sensor.latest_active_frame is None
    ):
        return False
    helper = hass.states.get("input_select.hoymiles_ems_supervisor_mode")
    paused = hass.states.get(EMS_PAUSED_ENTITY_ID)
    frame = sensor.latest_active_frame
    record = sensor._controller.record
    return bool(
        helper is not None
        and helper.state == "Off"
        and paused is not None
        and paused.state == "off"
        and frame.decision.supervisor_mode is SupervisorMode.OFF
        and frame.decision.state is SupervisorState.OFF
        and frame.decision.supervisor_execution_authorized is False
        and frame.context.owner_kind in _SAFE_OFF_LEGACY_PROXY_OWNERS
        and frame.context.owner_conflict is False
        and frame.context.transaction_pending is False
        and frame.context.transaction_owner_kind is OwnerKind.NONE
        and record.state is ActiveState.IDLE
        and record.owner is ExecutionOwner.NONE
        and record.transaction is None
    )


def _manual_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _manual_generation(value: object) -> int | None:
    number = _manual_number(value)
    if number is None:
        return None
    generation = int(round(number))
    if abs(number - generation) > 0.001 or not 1 <= generation <= 16_000_000:
        return None
    return generation


def _manual_generation_newer(current: int, baseline: int) -> bool:
    return current > baseline or (
        baseline >= 16_000_000 and 1 <= current < baseline
    )


@dataclass(slots=True)
class _ManualProxyReadbackLease:
    family: str
    base_generation: int
    expected: tuple[float, ...] | float
    first_mismatch_generation: int | None = None

    def resolved(self, frame: ActiveFrame | None) -> bool:
        if frame is None:
            return False
        source = frame.execution
        if self.family == "ems":
            generation = _manual_generation(source.full_block_generation)
            values = tuple(
                _manual_number(value)
                for value in (
                    source.physical_mode_code,
                    source.self_use_soc_percent,
                    source.backup_soc_percent,
                    source.force_charge_soc_percent,
                    source.maximum_charge_power_percent,
                    source.force_discharge_soc_percent,
                    source.maximum_discharge_power_percent,
                )
            )
            coherent = (
                generation is not None
                and source.full_block_generation_at is not None
                and all(value is not None for value in values)
                and float(values[0]).is_integer()
                and int(float(values[0])) in {0, 3, 4, 5}
            )
            actual = tuple(float(value) for value in values if value is not None)
            expected = self.expected
            matches = bool(
                coherent
                and isinstance(expected, tuple)
                and actual[0] == expected[0]
                and all(abs(actual[index] - expected[index]) < 0.5 for index in (1, 2, 3, 5))
                and all(abs(actual[index] - expected[index]) < 0.05 for index in (4, 6))
            )
        elif self.family == "gcf":
            generation = _manual_generation(source.gcf_generation)
            actual_value = _manual_number(source.effective_export_limit_percent)
            coherent = bool(
                generation is not None
                and source.gcf_generation_at is not None
                and source.gcf_cohort_coherent is True
                and actual_value is not None
            )
            matches = bool(
                coherent
                and isinstance(self.expected, float)
                and abs(actual_value - self.expected) < 0.05
            )
        else:
            generation = _manual_generation(source.battery_charge_limit_generation)
            actual_value = _manual_number(source.battery_charge_limit_percent)
            coherent = bool(
                generation is not None
                and source.battery_charge_limit_generation_at is not None
                and actual_value is not None
            )
            matches = bool(
                coherent
                and isinstance(self.expected, float)
                and abs(actual_value - self.expected) < 0.05
            )
        if (
            not coherent
            or generation is None
            or not _manual_generation_newer(generation, self.base_generation)
        ):
            return False
        if matches:
            return True
        mismatch = self.first_mismatch_generation
        if mismatch is None:
            self.first_mismatch_generation = generation
            return False
        return _manual_generation_newer(generation, mismatch)


def _manual_proxy_readback_lease(
    source_id: str,
    requested_value: object,
    frame: ActiveFrame,
) -> _ManualProxyReadbackLease | None:
    source = frame.execution
    if source_id == "gcf_export_soft_limit_ratio_259":
        generation = _manual_generation(source.gcf_generation)
        target = _manual_number(requested_value)
        if generation is None or target is None or not target.is_integer() or not -10.0 <= target <= 200.0:
            return None
        return _ManualProxyReadbackLease("gcf", generation, target)
    if source_id == "battery_max_charge_power_306":
        generation = _manual_generation(source.battery_charge_limit_generation)
        target = _manual_number(requested_value)
        if generation is None or target is None or not target.is_integer() or not 10.0 <= target <= 100.0:
            return None
        return _ManualProxyReadbackLease("battery", generation, target)

    try:
        settings = settings_from_execution_source(source)
    except (ActiveBridgeError, TypeError, ValueError):
        return None
    generation = settings.ems_generation
    block = settings.ems_block
    expected = [
        float(block.mode),
        block.self_use_soc_percent_4301,
        block.backup_soc_percent_4302,
        block.force_charge_soc_percent_4303,
        block.maximum_charge_power_percent_4304,
        block.force_discharge_soc_percent_4305,
        block.maximum_discharge_power_percent_4306,
    ]
    physical_mode = expected[0]
    physical_mode_code = int(physical_mode)
    if (
        not physical_mode.is_integer()
        or physical_mode_code not in {0, 3, 4, 5}
    ):
        return None
    expected[0] = float(physical_mode_code)
    target = _manual_number(requested_value)
    if source_id == "ems_mode_4300":
        mode = {
            "self_use": 0.0,
            "off_grid": 3.0,
            "grid_charge": 4.0,
            "grid_discharge": 5.0,
        }.get(requested_value)
        if mode is None:
            return None
        expected[0] = mode
    elif source_id == "ems_complete_block_charge_rollback_command":
        if target is None:
            return None
        payload = int(round(target))
        if abs(target - payload) > 0.01 or not 10_010 <= payload <= 101_100:
            return None
        charge_soc, charge_power_raw = divmod(payload, 1001)
        if not 10 <= charge_soc <= 100 or not 0 <= charge_power_raw <= 1000:
            return None
        expected[0] = 3.0 if int(round(expected[0])) == 3 else 0.0
        expected[3] = float(charge_soc)
        expected[4] = charge_power_raw * 0.1
    else:
        field = {
            "self_used_soc_4301": 1,
            "force_charge_soc_4303": 3,
            "maximum_charge_power_4304": 4,
            "force_discharge_soc_4305": 5,
            "maximum_discharge_power_4306": 6,
        }.get(source_id)
        if field is None or target is None or not target.is_integer():
            return None
        expected[field] = target
    return _ManualProxyReadbackLease("ems", generation, tuple(expected))


def _manual_proxy_atomic_write(
    lease: _ManualProxyReadbackLease,
) -> AtomicWrite:
    """Translate one safe-Off lease to the shared generation-bound transport."""

    if lease.family == "gcf":
        if not isinstance(lease.expected, float):
            raise ValueError("manual GCF lease has no numeric target")
        return AtomicWrite(
            AtomicWriteFamily.GCF_EXPORT_LIMIT,
            lease.base_generation,
            target_percent=lease.expected,
        )
    if lease.family == "battery":
        if not isinstance(lease.expected, float):
            raise ValueError("manual battery lease has no numeric target")
        return AtomicWrite(
            AtomicWriteFamily.BATTERY_CHARGE_LIMIT,
            lease.base_generation,
            target_percent=lease.expected,
        )
    if not isinstance(lease.expected, tuple) or len(lease.expected) != 7:
        raise ValueError("manual EMS lease has no complete block")
    values = lease.expected
    return AtomicWrite(
        AtomicWriteFamily.EMS_COMPLETE_BLOCK,
        lease.base_generation,
        ems_block=EmsBlock(
            mode=EmsMode(int(values[0])),
            self_use_soc_percent_4301=values[1],
            backup_soc_percent_4302=values[2],
            force_charge_soc_percent_4303=values[3],
            maximum_charge_power_percent_4304=values[4],
            force_discharge_soc_percent_4305=values[5],
            maximum_discharge_power_percent_4306=values[6],
        ),
    )


async def async_dispatch_manual_ems_proxy_write(
    hass: HomeAssistant,
    entry_id: str,
    source_id: str,
    requested_value: object,
) -> bool:
    """Dispatch one safe-Off proxy write through the shared guarded transport."""

    guard = hass.data.get(_GUARD_KEY)
    if not isinstance(guard, _SupervisorGuard):
        return False
    sensor = guard.sensors.get(entry_id)
    if (
        sensor is None
        or sensor._controller is None
        or sensor.latest_active_frame is None
    ):
        return False
    controller = sensor._controller
    lease = _manual_proxy_readback_lease(
        source_id,
        requested_value,
        sensor.latest_active_frame,
    )
    if lease is None:
        return False
    try:
        write = _manual_proxy_atomic_write(lease)
    except (TypeError, ValueError):
        return False
    return await controller.async_manual_proxy_dispatch(
        authority_valid=lambda: (
            sensor._controller is controller
            and manual_ems_proxy_write_allowed(hass, entry_id)
        ),
        dispatch=lambda: sensor._async_dispatch_atomic_write(write),
        readback_resolved=lambda: lease.resolved(sensor.latest_active_frame),
    )


def _single_supervisor_guard(hass: HomeAssistant) -> _SupervisorGuard:
    """Return the one loaded Supervisor guard or fail closed."""

    guard = hass.data.get(_GUARD_KEY)
    if (
        not isinstance(guard, _SupervisorGuard)
        or guard.loaded_entry_count != 1
        or len(guard.sensors) != 1
    ):
        raise RuntimeError("EMS Supervisor control requires exactly one instance")
    return guard


def _supervisor_control_lock(guard: _SupervisorGuard) -> asyncio.Lock:
    """Create the installation-wide user-control serializer lazily."""

    if guard.control_lock is None:
        guard.control_lock = asyncio.Lock()
    return guard.control_lock


def _control_sensor(guard: _SupervisorGuard) -> "HoymilesSupervisorSensor":
    """Return the sole sensor already validated by the installation guard."""

    return next(iter(guard.sensors.values()))


def _control_generation_current(
    guard: _SupervisorGuard,
    sensor: "HoymilesSupervisorSensor",
    generation: int,
) -> bool:
    """Reject a late ordinary control operation after MASTER STOP was requested."""

    return bool(
        guard.control_generation == generation
        and not sensor._master_stop_latched
    )


def _require_control_generation(
    guard: _SupervisorGuard,
    sensor: "HoymilesSupervisorSensor",
    generation: int,
) -> None:
    if not _control_generation_current(guard, sensor, generation):
        raise RuntimeError("EMS control operation was superseded by MASTER STOP")


async def _async_set_boolean_helper(
    hass: HomeAssistant,
    entity_id: str,
    enabled: bool,
) -> None:
    """Set and verify one required input_boolean helper."""

    state = hass.states.get(entity_id)
    if state is None:
        raise RuntimeError(f"required EMS helper is missing: {entity_id}")
    expected = "on" if enabled else "off"
    if _state_text(state) != expected:
        await hass.services.async_call(
            "input_boolean",
            "turn_on" if enabled else "turn_off",
            {"entity_id": entity_id},
            blocking=True,
        )
    if _state_text(hass.states.get(entity_id)) != expected:
        raise RuntimeError(f"EMS helper did not confirm {expected}: {entity_id}")


async def _async_set_supervisor_mode(
    hass: HomeAssistant,
    option: str,
) -> None:
    """Set and verify the physical Supervisor authority helper."""

    state = hass.states.get(_SUPERVISOR_MODE_ENTITY_ID)
    options = state.attributes.get("options") if state is not None else None
    if state is None or not isinstance(options, (list, tuple)) or option not in options:
        raise RuntimeError("EMS Supervisor mode helper is missing or invalid")
    if _state_text(state) != option:
        await hass.services.async_call(
            "input_select",
            "select_option",
            {"entity_id": _SUPERVISOR_MODE_ENTITY_ID, "option": option},
            blocking=True,
        )
    if _state_text(hass.states.get(_SUPERVISOR_MODE_ENTITY_ID)) != option:
        raise RuntimeError(f"EMS Supervisor mode did not confirm {option}")


def _controls_permit_policy_change(hass: HomeAssistant) -> bool:
    """Allow paired policy edits only under Active or the explicit pause latch."""

    return bool(
        _state_text(hass.states.get(EMS_PAUSED_ENTITY_ID)) == "on"
        or _state_text(hass.states.get(_SUPERVISOR_MODE_ENTITY_ID)) == "Active"
    )


def _recompute_user_control_state(guard: _SupervisorGuard) -> None:
    """Publish a fresh frame after a helper sequence which has no source row."""

    for sensor in tuple(guard.sensors.values()):
        sensor._recompute(raise_on_error=True)


async def _async_set_policy_helpers(
    hass: HomeAssistant,
    policy_id: str,
    enabled: bool,
    *,
    update_preference: bool,
) -> None:
    """Apply one policy request in the only fail-closed helper order."""

    helpers = _POLICY_HELPERS.get(policy_id)
    if helpers is None:
        raise RuntimeError("unsupported EMS policy")
    preference, policy_enabled, allowed = helpers
    if update_preference:
        await _async_set_boolean_helper(hass, preference, enabled)
    if enabled:
        if not _controls_permit_policy_change(hass):
            if allowed is not None and hass.states.get(allowed) is not None:
                await _async_set_boolean_helper(hass, allowed, False)
            raise RuntimeError("EMS policy can be enabled only while Active or paused")
        await _async_set_boolean_helper(hass, policy_enabled, True)
        if not _controls_permit_policy_change(hass):
            if allowed is not None:
                await _async_set_boolean_helper(hass, allowed, False)
            raise RuntimeError("EMS mode changed during policy update")
        if allowed is not None:
            try:
                await _async_set_boolean_helper(hass, allowed, True)
            except Exception:
                await _async_set_boolean_helper(hass, allowed, False)
                raise
        return
    if allowed is not None:
        await _async_set_boolean_helper(hass, allowed, False)
    await _async_set_boolean_helper(hass, policy_enabled, False)


async def async_set_supervisor_paused(
    hass: HomeAssistant,
    paused: bool,
) -> None:
    """Pause or resume EMS while preserving policy selection."""

    if type(paused) is not bool:
        raise RuntimeError("paused must be a boolean")
    guard = _single_supervisor_guard(hass)
    sensor = _control_sensor(guard)
    async with _supervisor_control_lock(guard):
        generation = guard.control_generation
        _require_control_generation(guard, sensor, generation)
        try:
            if paused:
                # The latch closes manual/proxy authority before Active is released.
                await _async_set_boolean_helper(hass, EMS_PAUSED_ENTITY_ID, True)
                _require_control_generation(guard, sensor, generation)
                await _async_set_supervisor_mode(hass, "Off")
                _require_control_generation(guard, sensor, generation)
            else:
                if _state_text(hass.states.get(EMS_PAUSED_ENTITY_ID)) != "on":
                    return
                # Active is selected while the pause latch still blocks all writers.
                await _async_set_supervisor_mode(hass, "Active")
                _require_control_generation(guard, sensor, generation)
                await _async_set_boolean_helper(hass, EMS_PAUSED_ENTITY_ID, False)
                _require_control_generation(guard, sensor, generation)
        finally:
            _recompute_user_control_state(guard)


async def async_set_supervisor_policy_enabled(
    hass: HomeAssistant,
    policy_id: str,
    enabled: bool,
) -> None:
    """Apply one idempotent enabled+allow policy operation."""

    if type(policy_id) is not str or type(enabled) is not bool:
        raise RuntimeError("invalid EMS policy request")
    guard = _single_supervisor_guard(hass)
    sensor = _control_sensor(guard)
    async with _supervisor_control_lock(guard):
        generation = guard.control_generation
        _require_control_generation(guard, sensor, generation)
        try:
            await _async_set_policy_helpers(
                hass,
                policy_id,
                enabled,
                update_preference=True,
            )
            _require_control_generation(guard, sensor, generation)
        except Exception:
            if not _control_generation_current(guard, sensor, generation):
                raise
            # A partially applied paired operation must not retain execution
            # authority.  The pause latch closes legacy paths before Active is
            # released; recompute below stops/restores an active transaction.
            try:
                await _async_set_boolean_helper(hass, EMS_PAUSED_ENTITY_ID, True)
            finally:
                await _async_set_supervisor_mode(hass, "Off")
            raise
        finally:
            _recompute_user_control_state(guard)


async def async_resume_supervisor_after_master_stop(hass: HomeAssistant) -> None:
    """Consciously restore saved policy choices after a completed MASTER STOP."""

    guard = _single_supervisor_guard(hass)
    sensor = _control_sensor(guard)
    async with _supervisor_control_lock(guard):
        generation = guard.control_generation
        _require_control_generation(guard, sensor, generation)
        record = sensor._controller.record if sensor._controller is not None else None
        if (
            sensor._master_stop_latched
            or record is None
            or record.master_stop_result.status is not MasterStopStatus.COMPLETED
            or record.owner is not ExecutionOwner.NONE
            or record.transaction is not None
        ):
            raise RuntimeError("MASTER STOP has not completed safely")
        await _async_set_boolean_helper(hass, EMS_PAUSED_ENTITY_ID, True)
        _require_control_generation(guard, sensor, generation)
        await _async_set_supervisor_mode(hass, "Off")
        _require_control_generation(guard, sensor, generation)
        try:
            await sensor._controller.async_rearm_after_master_stop()
            _require_control_generation(guard, sensor, generation)
            for policy_id, (preference, _enabled, _allowed) in _POLICY_HELPERS.items():
                selected = _state_text(hass.states.get(preference)) == "on"
                await _async_set_policy_helpers(
                    hass,
                    policy_id,
                    selected,
                    update_preference=False,
                )
                _require_control_generation(guard, sensor, generation)
            await _async_set_supervisor_mode(hass, "Active")
            _require_control_generation(guard, sensor, generation)
            await _async_set_boolean_helper(hass, EMS_PAUSED_ENTITY_ID, False)
            _require_control_generation(guard, sensor, generation)
        except Exception:
            # Keeping the explicit pause on is the final fail-closed boundary.
            raise
        finally:
            _recompute_user_control_state(guard)


async def async_request_supervisor_master_stop(hass: HomeAssistant) -> None:
    """Run MASTER STOP only for one unambiguous loaded Supervisor instance."""

    guard = _single_supervisor_guard(hass)
    sensor = _control_sensor(guard)
    guard.control_generation += 1
    sensor.request_master_stop()
    preferences = {
        preference: (
            _state_text(hass.states.get(policy_enabled)) == "on"
            and (allowed is None or _state_text(hass.states.get(allowed)) == "on")
        )
        for preference, policy_enabled, allowed in _POLICY_HELPERS.values()
    }

    # Start the serialized physical STOP before waiting for any ordinary UI
    # operation. The controller lock remains the only Modbus writer boundary.
    await sensor.async_master_stop()

    lock = _supervisor_control_lock(guard)
    try:
        await asyncio.wait_for(
            lock.acquire(),
            timeout=_MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS,
        )
    except TimeoutError as err:
        raise RuntimeError(
            "MASTER STOP accepted; ordinary controls remain latched while helper cleanup is pending"
        ) from err
    try:
        await asyncio.wait_for(
            sensor.async_finish_master_stop_controls(),
            timeout=_MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS,
        )
        try:
            await asyncio.wait_for(
                _async_store_policy_preferences(hass, preferences),
                timeout=_MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS,
            )
        except Exception as err:  # noqa: BLE001 - STOP must win over preference I/O
            _LOGGER.warning(
                "MASTER STOP continues without a complete policy-preference snapshot: %s",
                err,
            )
    finally:
        lock.release()


async def _async_store_policy_preferences(
    hass: HomeAssistant,
    preferences: Mapping[str, bool],
) -> None:
    """Persist already captured choices outside the physical STOP path."""

    for preference, selected in preferences.items():
        await _async_set_boolean_helper(hass, preference, selected)


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


def _health_text(value: Any) -> str | None:
    """Return one bounded scalar health value without truthy coercion."""

    if not isinstance(value, (str, int, float, bool)):
        return None
    text = str(value).strip()
    return text[:160] if text else None


def _execution_health_contract(
    attributes: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    """Build the single fail-closed health projection consumed by Aurora."""

    normalized_now = _aware_utc(now)
    phase = _health_text(attributes.get("execution_phase")) or "unknown"
    context = attributes.get("execution_context_evidence")
    context = context if isinstance(context, Mapping) else {}
    last_read = _iso_datetime(attributes.get("execution_last_valid_read_at"))
    read_age = (
        max(0.0, (normalized_now - last_read).total_seconds())
        if normalized_now is not None
        and last_read is not None
        and last_read <= normalized_now
        else None
    )
    physical_fresh = bool(
        (
            context.get("physical_mode_fresh") is True
            or attributes.get("execution_physical_mode_fresh") is True
        )
        and read_age is not None
        and read_age <= _EXECUTION_HEALTH_MAX_READ_AGE_SECONDS
    )
    connectivity = (
        "connected"
        if physical_fresh
        else "stale"
        if last_read is not None
        else "unknown"
    )

    transaction_owner = _health_text(attributes.get("transaction_owner"))
    owner = _health_text(attributes.get("owner")) or "unknown"
    if transaction_owner not in {None, "none", "unknown"}:
        owner = transaction_owner
    observed_owner = _health_text(attributes.get("observed_owner")) or "unknown"
    owner_conflict = attributes.get("owner_conflict") is True

    master_stop = attributes.get("master_stop")
    master_stop = master_stop if isinstance(master_stop, Mapping) else {}
    master_stop_status = _health_text(master_stop.get("status")) or "unknown"
    if (
        attributes.get("master_stop_adapter_latched") is True
        and master_stop_status in {"not_requested", "completed", "unknown"}
    ):
        master_stop_status = "requested"
    if master_stop_status == "completed":
        master_stop_confirmation = "confirmed"
    elif master_stop_status in {"requested", "in_progress"}:
        master_stop_confirmation = "unconfirmed"
    elif master_stop_status in {"blocked", "failed"}:
        master_stop_confirmation = "blocked"
    elif master_stop_status == "not_requested":
        master_stop_confirmation = "not_requested"
    else:
        master_stop_confirmation = "unknown"

    rollback_status = _health_text(attributes.get("rollback_status")) or "unknown"
    verification = (
        _health_text(attributes.get("physical_verification_result")) or "unknown"
    )
    lifecycle_reason = _health_text(attributes.get("lifecycle_reason"))
    execution_error = _health_text(attributes.get("execution_adapter_error"))
    accounting_error = _health_text(attributes.get("accounting_adapter_error"))
    notification_error = _health_text(attributes.get("notification_adapter_error"))
    support_filter = attributes.get("tariff_support_flow_filter")

    control_status = "healthy"
    reason = "healthy"
    action = "none"
    if master_stop_status in {"blocked", "failed"}:
        control_status = "unhealthy"
        reason = f"master_stop_{master_stop_status}"
        action = "verify_master_stop"
    elif master_stop_status in {"requested", "in_progress"}:
        if execution_error is not None:
            control_status = "unhealthy"
            reason = "master_stop_unconfirmed_persistence_failed"
            action = "verify_master_stop"
        else:
            control_status = "recovering"
            reason = "master_stop_unconfirmed"
            action = "wait_for_master_stop"
    elif master_stop_status == "completed" and execution_error is not None:
        control_status = "unhealthy"
        reason = "master_stop_state_save_failed"
        action = "inspect_recovery"
    elif execution_error is not None:
        control_status = "unhealthy"
        reason = "execution_adapter_error"
        action = "restart_ems"
    elif owner_conflict:
        control_status = "unhealthy"
        reason = "owner_conflict"
        action = "resolve_owner_conflict"
    elif rollback_status == "failed":
        control_status = "unhealthy"
        reason = "rollback_failed"
        action = "inspect_recovery"
    elif phase in {"fault", "active_fault", "blocked", "active_blocked"}:
        control_status = "unhealthy"
        reason = "execution_fault" if "fault" in phase else "execution_blocked"
        action = "check_current_state"
    elif not physical_fresh:
        control_status = "unknown"
        reason = (
            "physical_readback_stale"
            if last_read is not None
            else "physical_readback_missing"
        )
        action = "wait_for_fresh_read"
    elif (
        (
            rollback_status == "pending"
            and phase not in {"executing", "active_executing"}
        )
        or phase in {"stopping", "active_stopping", "restoring", "active_restoring"}
        or lifecycle_reason == "restart_recovery"
    ):
        control_status = "recovering"
        reason = "recovery_in_progress"
        action = "wait_for_recovery"
    elif verification == "contradicted":
        control_status = "unhealthy"
        reason = "physical_verification_contradicted"
        action = "check_current_state"
    elif verification == "unavailable":
        control_status = "unknown"
        reason = "physical_verification_unavailable"
        action = "check_current_state"
    elif isinstance(support_filter, Mapping) and support_filter.get("active") is True:
        control_status = "recovering"
        reason = "tariff_support_flow_filter"
        action = "wait_for_readback"
    elif (
        verification == "pending"
        and (
            _health_text(attributes.get("transaction_id")) is not None
            or phase in {"waiting_readback", "active_waiting_readback"}
        )
    ):
        control_status = "recovering"
        reason = "physical_verification_pending"
        action = "wait_for_readback"

    degradations = {
        key: value
        for key, value in (
            ("accounting", accounting_error),
            ("notifications", notification_error),
        )
        if value is not None
    }
    status = control_status
    if control_status == "healthy" and degradations:
        status = "degraded"
        reason = (
            "accounting_and_notifications_degraded"
            if len(degradations) == 2
            else f"{next(iter(degradations))}_degraded"
        )
        action = "inspect_degradations"

    return {
        "schema_version": _EXECUTION_HEALTH_SCHEMA_VERSION,
        "status": status,
        "control_status": control_status,
        "reason": reason,
        "action": action,
        "phase": phase,
        "control_error": execution_error,
        "connectivity": connectivity,
        "last_valid_read_at": (
            last_read.isoformat() if last_read is not None else None
        ),
        "last_valid_read_age_seconds": (
            round(read_age, 3) if read_age is not None else None
        ),
        "owner": owner,
        "observed_owner": observed_owner,
        "owner_conflict": owner_conflict,
        "master_stop_status": master_stop_status,
        "master_stop_confirmation": master_stop_confirmation,
        "rollback_status": rollback_status,
        "physical_verification": verification,
        "degradations": degradations,
    }


def _master_stop_latch_payload(
    *,
    latched: bool,
    requested_at: datetime | None,
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    """Encode the small adapter-owned starts-off safety latch."""

    return {
        "schema_version": 1,
        "latched": latched,
        "requested_at": (
            requested_at.astimezone(timezone.utc).isoformat()
            if requested_at is not None
            else None
        ),
        "completed_at": (
            completed_at.astimezone(timezone.utc).isoformat()
            if completed_at is not None
            else None
        ),
    }


def _decode_master_stop_latch(payload: object) -> tuple[bool, datetime | None]:
    """Decode only the exact schema-v1 latch; malformed data fails closed."""

    if type(payload) is not dict or set(payload) != {
        "schema_version",
        "latched",
        "requested_at",
        "completed_at",
    }:
        raise ValueError("MASTER STOP latch is not an exact schema-v1 object")
    if payload["schema_version"] != 1 or type(payload["latched"]) is not bool:
        raise ValueError("MASTER STOP latch schema or flag is invalid")
    requested_raw = payload["requested_at"]
    completed_raw = payload["completed_at"]
    requested_at = (
        _iso_datetime(requested_raw) if requested_raw is not None else None
    )
    completed_at = (
        _iso_datetime(completed_raw) if completed_raw is not None else None
    )
    if (
        (requested_raw is not None and requested_at is None)
        or (completed_raw is not None and completed_at is None)
        or (payload["latched"] and (requested_at is None or completed_at is not None))
        or (
            not payload["latched"]
            and (requested_at is None) != (completed_at is None)
        )
        or (
            requested_at is not None
            and completed_at is not None
            and completed_at < requested_at
        )
    ):
        raise ValueError("MASTER STOP latch timestamps are incoherent")
    return payload["latched"], requested_at


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


def _tariff_live_power_input_evidence(attrs: Mapping[str, Any]) -> dict[str, Any] | None:
    """Bound three consumed channels for a missing-input STOP, never for control."""
    if attrs.get("current_run_continue_reason") != "live_data_missing":
        return None
    sampled = _iso_datetime(attrs.get("current_live_power_sampled_at"))
    limit = _plan_number(attrs.get("current_live_power_max_age_seconds"), minimum=0.0)
    channels = {}
    for name in ("load", "pv", "battery"):
        prefix = f"current_live_{name}_power_"
        reason = attrs.get(prefix + "source")
        channels[name] = {
            "value_kw": _plan_number(attrs.get(prefix + "kw")),
            "age_seconds": _plan_number(attrs.get(prefix + "age_seconds")),
            "maximum_age_seconds": limit,
            "reason": reason if type(reason) is str and len(reason) <= 64 else None,
        }
    return {
        "stage": "tariff_optimizer_input",
        "sampled_at": sampled.isoformat() if sampled else None,
        "shared_revision": _revision(attrs.get("current_live_power_shared_revision")),
        "channels": channels,
    }


def _bounded_plan_attribute(plan_key: str, attribute: str, value: Any) -> Any:
    if plan_key == "rce_plan" and attribute in {
        "current_slot_suppression_reason",
        "post_command_settling_market_fingerprint",
    }:
        return value if type(value) is str and len(value) <= 128 else None
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
    if attribute in _PLAN_TEXT_ATTRIBUTES:
        return value if type(value) is str and len(value) <= 128 else None
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


def _control_lease_block(block: EmsBlock) -> tuple[float, ...]:
    """Encode the exact seven-register block carried by lease protocol v1."""

    return (
        float(int(block.mode)),
        float(block.self_use_soc_percent_4301),
        float(block.backup_soc_percent_4302),
        float(block.force_charge_soc_percent_4303),
        float(block.maximum_charge_power_percent_4304),
        float(block.force_discharge_soc_percent_4305),
        float(block.maximum_discharge_power_percent_4306),
    )


def _control_lease_frame_block(frame: ActiveFrame) -> tuple[float, ...] | None:
    values = (
        frame.execution.physical_mode_code,
        frame.execution.self_use_soc_percent,
        frame.execution.backup_soc_percent,
        frame.execution.force_charge_soc_percent,
        frame.execution.maximum_charge_power_percent,
        frame.execution.force_discharge_soc_percent,
        frame.execution.maximum_discharge_power_percent,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        for value in values
    ):
        return None
    return tuple(float(value) for value in values)  # type: ignore[arg-type]


def _control_lease_blocks_match(
    left: tuple[float, ...] | None,
    right: tuple[float, ...],
) -> bool:
    return bool(
        left is not None
        and len(left) == len(right) == 7
        and int(round(left[0])) == int(round(right[0]))
        and all(abs(a - b) <= 0.05 for a, b in zip(left[1:], right[1:]))
    )


def _control_lease_direction_ready(
    *,
    owner: ExecutionOwner,
    action: ExecutionAction,
    mode: EmsMode,
    gates: ExecutionGates,
) -> bool:
    """Apply only the transport gates used by the leased EMS command.

    RCE export writes the complete 4300-4306 block, which is supported for a
    standalone inverter and for a verified parallel Master.  Direct register
    259 remains required for other discharge owners that depend on that
    transport, but it must not suppress an otherwise valid RCE lease renewal.
    """

    if action is ExecutionAction.PV_CHARGE_HOLD:
        return owner is ExecutionOwner.RCE and mode is EmsMode.GRID_DISCHARGE and gates.pv_charge_hold_ready
    if mode is EmsMode.GRID_CHARGE:
        return gates.charge_direction_ready
    if mode is not EmsMode.GRID_DISCHARGE or not gates.discharge_direction_ready:
        return False
    return bool(
        (
            owner is ExecutionOwner.RCE
            and action is ExecutionAction.RCE_EXPORT
        )
        or gates.direct_259_ready
    )


class HoymilesSupervisorSensor(SensorEntity):
    """Publish one deterministic Off/Active Supervisor decision."""

    _attr_should_poll = False
    _unrecorded_attributes = SUPERVISOR_UNRECORDED_ATTRIBUTES | frozenset({
        "recent_stop_decisions", "tariff_support_flow_filter",
    })
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
        # Aurora and the managed scheduler consume this stable identity.  Do
        # not let a site's optional entity-id formatting turn it into a
        # device-prefixed runtime-only state.
        self.entity_id = SUPERVISOR_ENTITY_ID
        self._attr_unique_id = f"{entry.entry_id}_ems_supervisor"
        self._available = False
        self._native_value: str | None = None
        self._attributes: dict[str, Any] = {}
        self._serialized_summary: str | None = None
        self._recorded_execution_previous: dict[str, Any] | None = None
        self._source_entity_ids: dict[str, str | None] = {}
        self._keys_by_entity_id: dict[str, tuple[str, ...]] = {}
        self._state_unsub: Callable[[], None] | None = None
        self._cohort_report_unsub: Callable[[], None] | None = None
        self._registry_unsub: Callable[[], None] | None = None
        self._config_unsub: Callable[[], None] | None = None
        self._planner_cancel: Callable[[], None] | None = None
        self._cohort_cancel: Callable[[], None] | None = None
        self._cohort_pending_source: str | None = None
        self._power_cohort_cancel: Callable[[], None] | None = None
        self._power_cohort_pending: dict[str, State] = {}
        self._power_cohort_states: dict[str, State] = {}
        self._power_cohort_generation = 0
        self._temporal_cancel: Callable[[], None] | None = None
        self._execution_watchdog_cancel: Callable[[], None] | None = None
        self._control_lease_cancel: Callable[[], None] | None = None
        self._control_lease_task: asyncio.Task[None] | None = None
        self._control_lease_client = ControlLeaseClient()
        self._runtime.control_lease = self._control_lease_client
        self._control_lease_error: str | None = None
        self._control_lease_last_result: dict[str, Any] | None = None
        self._control_lease_gate: dict[str, Any] | None = None
        self._control_lease_authorization_deadline: datetime | None = None
        self._master_stop_continuation_cancel: Callable[[], None] | None = None
        self._planner_pending_keys: set[str] = set()
        self._planner_generation = 0
        self._cohort_generation = 0
        self._temporal_generation = 0
        self._execution_watchdog_generation = 0
        self._execution_watchdog_key: tuple[str, datetime] | None = None
        self._settling_replan_cancel: Callable[[], None] | None = None
        self._settling_replan_key: tuple[str, datetime] | None = None
        self._settling_replan_attempted: tuple[str, datetime] | None = None
        self._settling_replan_task: asyncio.Task[None] | None = None
        self._settling_market_source: Callable[[], str | None] | None = None
        self._settling_replan_request: Callable[[], Awaitable[None]] | None = None
        self._master_stop_continuation_generation = 0
        self._removed = False
        self._guard_ready = False
        self._warning_categories: set[str] = set()
        self._error_categories: set[str] = set()
        self._executor_store: Store[dict[str, Any]] = Store(
            hass,
            _EXECUTOR_STORAGE_VERSION,
            f"{_EXECUTOR_STORAGE_KEY_PREFIX}.{entry.entry_id}",
        )
        self._master_stop_store: Store[dict[str, Any]] = Store(
            hass,
            _MASTER_STOP_STORAGE_VERSION,
            f"{_MASTER_STOP_STORAGE_KEY_PREFIX}.{entry.entry_id}",
        )
        self._controller: SupervisorActiveController | None = None
        self._controller_task: asyncio.Task[None] | None = None
        self._safety_task: asyncio.Task[None] | None = None
        self._pending_active_frame: ActiveFrame | None = None
        self._pending_active_frame_revision: int | None = None
        self._latest_active_frame: ActiveFrame | None = None
        self._master_stop_latched = False
        self._master_stop_requested_at: datetime | None = None
        self._master_stop_latch_persisted = False
        self._master_stop_latch_persist_error: str | None = None
        self._master_stop_controller_start_pending = False
        self._master_stop_controls_fenced = False
        self._unloading = False
        self._decision_state: str | None = None
        self._decision_attributes: dict[str, Any] = {}
        self._execution_last_valid_read_at: datetime | None = None
        self._execution_adapter_error: str | None = None
        self._accounting_adapter_error: str | None = None
        self._notification_adapter_error: str | None = None
        self._accounting_sink: Callable[
            [ExecutorRecord, ActiveFrame, Mapping[str, str | None]],
            Awaitable[None],
        ] | None = None
        self._notification_sink: Callable[
            [ExecutorRecord, ActiveFrame, Mapping[str, str | None]],
            None,
        ] | None = None

    def attach_accounting_sink(
        self,
        sink: Callable[
            [ExecutorRecord, ActiveFrame, Mapping[str, str | None]],
            Awaitable[None],
        ],
    ) -> None:
        """Attach the one output-only accounting-v2 consumer before HA add."""

        if self._accounting_sink is not None:
            raise RuntimeError("Supervisor accounting sink is already attached")
        self._accounting_sink = sink

    def attach_notification_sink(
        self,
        sink: Callable[
            [ExecutorRecord, ActiveFrame, Mapping[str, str | None]],
            None,
        ],
    ) -> None:
        """Attach the one output-only range notification consumer."""

        if self._notification_sink is not None:
            raise RuntimeError("Supervisor notification sink is already attached")
        self._notification_sink = sink

    @callback
    def update_notification_degradation(self, reason: str | None) -> None:
        """Publish notifier delivery truth without granting control authority."""

        if reason == self._notification_adapter_error:
            return
        self._notification_adapter_error = reason
        if not self._removed and not self._unloading:
            self._publish_composite_state()

    def attach_rce_settling_source(
        self,
        market_source: Callable[[], str | None],
        replan_request: Callable[[], Awaitable[None]],
    ) -> None:
        """Attach the entry's existing RCE planner before HA registration."""
        if self._settling_market_source is not None:
            raise RuntimeError("RCE settling source is already attached")
        self._settling_market_source = market_source
        self._settling_replan_request = replan_request

    @property
    def latest_active_frame(self) -> ActiveFrame | None:
        """Expose the newest immutable frame to output-only local consumers."""

        return self._latest_active_frame

    @callback
    def rce_commitment_cohort_pending(self) -> bool:
        """Tell the same-entry planner to defer attestation during FC03 delivery."""
        return self._cohort_cancel is not None

    @callback
    def current_rce_active_commitment(self, now: datetime) -> RceActiveCommitment | None:
        """Expose only a live leased export with fresh physical confirmation."""
        return self._current_rce_family_commitment(now, ExecutionAction.RCE_EXPORT)

    @callback
    def current_pv_delay_commitment(self, now):
        """Bind the PV plan after ACK; only the controller certifies execution."""
        return self._current_rce_family_commitment(now, ExecutionAction.PV_CHARGE_HOLD)

    def _current_rce_family_commitment(self, now, action):
        now_utc = _aware_utc(now)
        controller = self._controller
        if (now_utc is None or controller is None or self._master_stop_latched
            or self._pause_state != "off" or self._removed or self._unloading
            or self._cohort_cancel is not None):
            return None
        record = controller.record
        transaction = record.transaction
        lease = self._control_lease_client.handle
        valid_until = self._control_lease_valid_until()
        pv_starting = (action is ExecutionAction.PV_CHARGE_HOLD
                       and record.state is ActiveState.WAITING_READBACK)
        if (record.state is not ActiveState.EXECUTING and not pv_starting
            or record.owner is not ExecutionOwner.RCE
            or not record.starts_allowed or not record.automatic_policies_enabled
            or transaction is None or transaction.owner is not ExecutionOwner.RCE
            or transaction.intent.action is not action
            or lease is None or lease.transaction_id != transaction.transaction_id
            or lease.hard_deadline != transaction.deadline
            or valid_until is None or now_utc >= valid_until
            or now_utc >= transaction.deadline):
            return None
        block, proof = transaction.intent.command.ems_block, transaction.physical_verification
        if (block is None or block.mode is not EmsMode.GRID_DISCHARGE
            or transaction.readback_result is not VerificationStatus.CONFIRMED
            or not pv_starting and (proof is None or proof.transaction_id != transaction.transaction_id
            or proof.action is not transaction.intent.action
            or proof.status is not VerificationStatus.CONFIRMED
            or not 0 <= (now_utc - proof.observed_at).total_seconds()
            <= RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS)):
            return None
        frame = self._fresh_active_frame()
        if (frame is None or frame.decision.supervisor_mode is not SupervisorMode.ACTIVE
            or frame.rce.allowed_by_user is not True or frame.rce.enabled is not True
            or frame.rce.sale_block_active is not False or frame.context.owner_conflict
            or not frame.context.full_block_execution_ready
            or not frame.context.critical_bms_ready
            or not frame.context.discharge_direction_ready
            or frame.context.export_state is not ExportState.VERIFIED_ALLOWED):
            return None
        try:
            settings = settings_from_execution_source(frame.execution)
        except (ActiveBridgeError, TypeError, ValueError):
            return None
        if settings.ems_block != block or lease.block != _control_lease_block(block):
            return None
        if action is ExecutionAction.PV_CHARGE_HOLD:
            from .pv_charge_delay import PvDelayCommitment, PLANNER_SETTLING_SECONDS
            from .pv_charge_delay_control import sent_command_ready
            if (frame.rce.pv_charge_hold_qualified is not True
                or not sent_command_ready(transaction.intent, frame.execution, rce=frame.rce, now=now_utc)):
                return None
            if pv_starting:
                sent = transaction.command_sent_at
                generation_at = frame.execution.full_block_generation_at
                if (sent is None or generation_at is None
                    or not sent < generation_at <= now_utc
                    or not sent < settings.ems_observed_at <= now_utc
                    or not 0 <= (now_utc - sent).total_seconds() < PLANNER_SETTLING_SECONDS):
                    return None
            return PvDelayCommitment(transaction.transaction_id, transaction.started_at,
                transaction.deadline, transaction.command_sent_at, not pv_starting)
        return RceActiveCommitment(
            transaction_id=transaction.transaction_id, started_at=transaction.started_at,
            hard_deadline=transaction.deadline, physical_verified_at=proof.observed_at,
            maximum_discharge_power_percent=block.maximum_discharge_power_percent_4306,
            minimum_soc_percent=block.force_discharge_soc_percent_4305,
        )

    @callback
    def current_tariff_active_commitment(
        self,
        now: datetime,
    ) -> TariffActiveCommitment | None:
        """Return the live Mode 4 commitment, including direct house supply."""

        now_utc = _aware_utc(now)
        controller = self._controller
        if (
            now_utc is None
            or controller is None
            or self._master_stop_latched
            or self._pause_state != "off"
            or self._cohort_cancel is not None
            or self._removed
            or self._unloading
        ):
            return None
        record = controller.record
        transaction = record.transaction
        lease = self._control_lease_client.handle
        valid_until = self._control_lease_valid_until()
        if (
            record.state is not ActiveState.EXECUTING
            or record.owner is not ExecutionOwner.TARIFF
            or not record.starts_allowed
            or not record.automatic_policies_enabled
            or transaction is None
            or transaction.owner is not ExecutionOwner.TARIFF
            or lease is None
            or lease.transaction_id != transaction.transaction_id
            or lease.hard_deadline != transaction.deadline
            or valid_until is None or now_utc >= valid_until
            or now_utc >= transaction.deadline
        ):
            return None
        action = {
            ExecutionAction.TARIFF_GRID_SUPPORT: "grid_support",
            ExecutionAction.TARIFF_BATTERY_CHARGE: "battery_charge",
            ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE: (
                "grid_support_and_charge"
            ),
        }.get(transaction.intent.action)
        block = transaction.intent.command.ems_block
        proof = transaction.physical_verification
        if (
            action is None
            or block is None
            or block.mode is not EmsMode.GRID_CHARGE
            or transaction.readback_result is not VerificationStatus.CONFIRMED
            or proof is None
            or proof.transaction_id != transaction.transaction_id
            or proof.action is not transaction.intent.action
            or proof.status is not VerificationStatus.CONFIRMED
            or not 0.0
            <= (now_utc - proof.observed_at).total_seconds()
            <= TARIFF_PHYSICAL_MAX_AGE_SECONDS
        ):
            return None
        frame = self._fresh_active_frame()
        if (frame is None or frame.decision.supervisor_mode is not SupervisorMode.ACTIVE
            or frame.tariff.allowed_by_user is not True or frame.tariff.enabled is not True
            or frame.context.owner_conflict or not frame.context.full_block_execution_ready
            or not frame.context.critical_bms_ready):
            return None
        try:
            settings = settings_from_execution_source(frame.execution)
        except (ActiveBridgeError, TypeError, ValueError):
            return None
        if (settings.ems_block != block or lease.block != _control_lease_block(block)
            or not tariff_command_within_live_bms_limit(
                transaction.intent, frame.execution, tariff=frame.tariff, now=now_utc)):
            return None
        return TariffActiveCommitment(
            transaction_id=transaction.transaction_id,
            action=action,
            started_at=transaction.started_at,
            hard_deadline=transaction.deadline,
            target_soc_percent=block.force_charge_soc_percent_4303,
            maximum_charge_power_percent=(
                block.maximum_charge_power_percent_4304
            ),
            physical_verified_at=proof.observed_at,
        )

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

    async def _async_persist_executor_record(
        self,
        record: ExecutorRecord,
    ) -> None:
        """Persist one exact executor revision before it can reach transport."""

        await self._executor_store.async_save(record_to_dict(record))

    async def _async_latch_master_stop(self) -> bool:
        """Latch starts in RAM and attempt bounded, independently reported Store I/O."""

        newly_latched = not self._master_stop_latched
        self.request_master_stop()
        if self._master_stop_latch_persisted:
            return newly_latched
        requested_at = self._master_stop_requested_at
        if requested_at is None:  # pragma: no cover - request_master_stop sets it
            raise RuntimeError("MASTER STOP request timestamp is unavailable")
        try:
            await asyncio.wait_for(
                self._master_stop_store.async_save(
                    _master_stop_latch_payload(
                        latched=True,
                        requested_at=requested_at,
                    )
                ),
                timeout=_MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS,
            )
        except TimeoutError as err:
            self._master_stop_latch_persist_error = (
                "master_stop_latch_persist_timeout"
            )
            self._publish_composite_state()
            _LOGGER.error(
                "MASTER STOP accepted in RAM but its durable adapter latch timed out: %s",
                err,
            )
            return newly_latched
        except Exception as err:  # noqa: BLE001 - physical STOP retains priority
            self._master_stop_latch_persist_error = (
                "master_stop_latch_persist_failed"
            )
            self._publish_composite_state()
            _LOGGER.error(
                "MASTER STOP accepted in RAM but its durable adapter latch failed: %s",
                err,
            )
            return newly_latched
        self._master_stop_latch_persisted = True
        self._master_stop_latch_persist_error = None
        return newly_latched

    def request_master_stop(self) -> None:
        """Synchronously invalidate starts before any lock or helper await."""

        if self._master_stop_latched:
            return
        requested_at = _aware_utc(dt_util.utcnow())
        if requested_at is None:
            raise RuntimeError("UTC clock returned a naive MASTER STOP timestamp")
        self._master_stop_latched = True
        self._master_stop_requested_at = requested_at
        self._master_stop_controller_start_pending = True
        self._master_stop_controls_fenced = False
        if not self._unloading:
            self._publish_composite_state()

    async def _async_clear_master_stop_latch(self) -> None:
        """Close the adapter latch only after restore ACK and owner release."""

        requested_at = self._master_stop_requested_at
        if not self._master_stop_latched or requested_at is None:
            return
        completed_at = _aware_utc(dt_util.utcnow())
        if completed_at is None:
            raise RuntimeError("UTC clock returned a naive MASTER STOP timestamp")
        try:
            await asyncio.wait_for(
                self._master_stop_store.async_save(
                    _master_stop_latch_payload(
                        latched=False,
                        requested_at=requested_at,
                        completed_at=completed_at,
                    )
                ),
                timeout=_MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS,
            )
        except TimeoutError as err:
            self._master_stop_latch_persist_error = (
                "master_stop_latch_clear_persist_timeout"
            )
            self._publish_composite_state()
            raise RuntimeError(
                "Inverter STOP is confirmed but clearing its durable latch timed out"
            ) from err
        except Exception as err:  # noqa: BLE001 - preserve confirmed physical truth
            self._master_stop_latch_persist_error = (
                "master_stop_latch_clear_persist_failed"
            )
            self._publish_composite_state()
            raise RuntimeError(
                "Inverter STOP is confirmed but clearing its durable latch failed"
            ) from err
        self._master_stop_latched = False
        self._master_stop_latch_persisted = False
        self._master_stop_latch_persist_error = None
        self._master_stop_controller_start_pending = False
        self._master_stop_controls_fenced = False
        if not self._unloading:
            self._publish_composite_state()

    def _assert_single_transport_instance(self) -> None:
        """Re-check the exact single-entry guard at the transport boundary."""

        guard = self.hass.data.get(_GUARD_KEY)
        if (
            _loaded_entry_count(self.hass) != 1
            or not isinstance(guard, _SupervisorGuard)
            or guard.loaded_entry_count != 1
            or len(guard.sensors) != 1
            or guard.sensors.get(self._entry.entry_id) is not self
        ):
            raise RuntimeError(
                "EMS Supervisor transport requires exactly one loaded instance"
            )

    def _esphome_named_action_service(self, action: str) -> str:
        """Resolve one exact action owned by this source device's ESPHome node."""

        source_entry_ids = linked_config_entry_ids(self._runtime.source_device)
        if len(source_entry_ids) != 1:
            raise RuntimeError(
                "source device must belong to exactly one ESPHome config entry"
            )
        source_entry_id = next(iter(source_entry_ids))
        source_entry = self.hass.config_entries.async_get_entry(source_entry_id)
        if source_entry is None or getattr(source_entry, "domain", None) != _ESPHOME_DOMAIN:
            raise RuntimeError("source device owner is not a loaded ESPHome entry")
        source_data = getattr(source_entry, "data", None)
        node_name = (
            source_data.get(_ESPHOME_DEVICE_NAME_KEY)
            if isinstance(source_data, Mapping)
            else None
        )
        if (
            type(node_name) is not str
            or not node_name
            or node_name.strip() != node_name
        ):
            raise RuntimeError("source ESPHome entry has no exact device_name")
        expected_service = f"{node_name.replace('-', '_')}_{action}"
        services = self.hass.services.async_services().get(_ESPHOME_DOMAIN, {})
        if expected_service not in services:
            raise RuntimeError(
                f"source ESPHome action {expected_service} is not registered"
            )
        return expected_service

    def _esphome_action_service(self, family: AtomicWriteFamily) -> str:
        """Resolve one atomic writer action for the source ESPHome node."""

        return self._esphome_named_action_service(family.value)

    async def _async_dispatch_atomic_write(self, write: AtomicWrite) -> None:
        """Dispatch only the three generation-bound Supervisor API actions."""

        self._assert_single_transport_instance()
        if write.family is AtomicWriteFamily.EMS_COMPLETE_BLOCK:
            block = write.ems_block
            if block is None:
                raise RuntimeError("complete-block action has no EMS payload")
            if block.mode in {EmsMode.GRID_CHARGE, EmsMode.GRID_DISCHARGE}:
                try:
                    await self._async_dispatch_leased_ems(write, block)
                except AtomicWriteNotQueued as err:
                    _LOGGER.warning(
                        "EMS leased write refused before transport: %s",
                        err.reason,
                    )
                    raise
                except Exception:
                    _LOGGER.exception(
                        "EMS Supervisor leased transport failed closed"
                    )
                    raise
                return
            data: dict[str, Any] = {
                "mode_code": int(block.mode),
                "self_use_soc": block.self_use_soc_percent_4301,
                "backup_soc": block.backup_soc_percent_4302,
                "force_charge_soc": block.force_charge_soc_percent_4303,
                "maximum_charge_power": block.maximum_charge_power_percent_4304,
                "force_discharge_soc": block.force_discharge_soc_percent_4305,
                "maximum_discharge_power": block.maximum_discharge_power_percent_4306,
                "snapshot_generation": write.snapshot_generation,
            }
        else:
            if write.target_percent is None:
                raise RuntimeError("direct-register action has no target")
            data = {
                "target_percent": write.target_percent,
                "snapshot_generation": write.snapshot_generation,
            }
        service = self._esphome_action_service(write.family)
        # There is no await between this second cardinality guard and dispatch,
        # so another config entry cannot slip into the call.
        self._assert_single_transport_instance()
        try:
            response_options = (
                {"return_response": True}
                if write.family is AtomicWriteFamily.EMS_COMPLETE_BLOCK
                else {}
            )
            response = await self.hass.services.async_call(
                _ESPHOME_DOMAIN,
                service,
                data,
                blocking=True,
                context=self._context,
                **response_options,
            )
            if write.family is AtomicWriteFamily.EMS_COMPLETE_BLOCK:
                # This is the native response correlated to this API call.
                # Acceptance means the writer entered transport, not FC03 ACK.
                if (not isinstance(response, Mapping)
                    or type(response.get("schema_version")) is not int
                    or response.get("schema_version") != 1):
                    raise RuntimeError("EMS firmware dispatch result is unknown")
                accepted = response.get("accepted")
                reason = response.get("reason")
                if (accepted is False and type(reason) is str
                    and reason in _EMS_PRETRANSPORT_REJECTIONS):
                    raise AtomicWriteNotQueued(reason)
                if accepted is not True or reason != "accepted":
                    raise RuntimeError("EMS firmware dispatch result is unknown")
        except AtomicWriteNotQueued as err:
            _LOGGER.warning("EMS firmware refused before transport: %s", err.reason)
            raise
        except Exception:
            _LOGGER.exception(
                "EMS Supervisor transport action %s failed closed",
                service,
            )
            raise

    async def _async_dispatch_leased_ems(
        self,
        write: AtomicWrite,
        block: EmsBlock,
    ) -> None:
        """Persist an ESP recovery record before one forced 4/5 transport."""

        now = _aware_utc(dt_util.utcnow())
        if now is None:
            raise RuntimeError("UTC clock returned a naive control lease timestamp")
        transaction_id = f"manual:{self._control_lease_client.session_id}"
        hard_deadline = now + timedelta(seconds=CONTROL_LEASE_TTL_SECONDS)
        controller = self._controller
        transaction = controller.record.transaction if controller is not None else None
        if (
            transaction is not None
            and transaction.intent.command.ems_block == block
            and transaction.deadline > now
        ):
            transaction_id = transaction.transaction_id
            hard_deadline = transaction.deadline

        challenge_service = self._esphome_named_action_service(
            "ems_supervisor_control_lease_challenge"
        )
        self._assert_single_transport_instance()
        challenge = await self.hass.services.async_call(
            _ESPHOME_DOMAIN,
            challenge_service,
            {},
            blocking=True,
            context=self._context,
            return_response=True,
        )
        if (
            not isinstance(challenge, Mapping)
            or challenge.get("schema_version") != 1
            or challenge.get("protocol_version") !=
                _CONTROL_LEASE_PROTOCOL_VERSION
            or type(challenge.get("nonce")) is not str
            or challenge.get("maximum_lease_seconds") != CONTROL_LEASE_TTL_SECONDS
            or challenge.get("renew_interval_seconds") != CONTROL_LEASE_RENEW_SECONDS
        ):
            raise RuntimeError("ESP control lease capability is absent or incompatible")
        if (
            controller is not None
            and transaction is not None
            and transaction.owner in {ExecutionOwner.RCE, ExecutionOwner.TARIFF}
            and controller.record.state in {ActiveState.STARTING, ActiveState.RETARGETING}
        ):
            # The challenge yields to HA. A trailing planner/FC03 publication
            # is not a permanent denial of a command that was never sent.
            # Wait only inside this attempt; the fresh authorization below
            # still owns every write. Never retry an unknown ESP outcome.
            loop = asyncio.get_running_loop()
            attempt_state = controller.record.state
            now = _aware_utc(dt_util.utcnow())
            if now is None:
                raise AtomicWriteNotQueued("retarget_not_authorized")
            remaining = min(
                (transaction.deadline - now).total_seconds(),
                5.0,
            )
            if attempt_state is ActiveState.STARTING:
                remaining = min(remaining, COMMAND_DISPATCH_TIMEOUT_SECONDS
                                - (now - transaction.started_at).total_seconds() - 1.0)
            # RETARGETING is already enclosed by the controller's immutable
            # attempt timeout (persist + challenge + transport + handling).
            # This wait cannot renew that budget, lease or hard deadline.
            wait_until = loop.time() + max(remaining, 0.0)
            while self._planner_cancel is not None or self._cohort_cancel is not None:
                if (
                    loop.time() >= wait_until
                    or self._master_stop_latched or self._removed or self._unloading
                    or controller.record.transaction is not transaction
                    or controller.record.state is not attempt_state
                ):
                    raise AtomicWriteNotQueued("retarget_not_authorized")
                await asyncio.sleep(min(0.05, wait_until - loop.time()))
            now = _aware_utc(dt_util.utcnow())
            if now is None:
                raise AtomicWriteNotQueued("retarget_not_authorized")
            frame = self._fresh_active_frame()
            if frame is not None:
                try:
                    settings = settings_from_execution_source(frame.execution)
                except (ActiveBridgeError, TypeError, ValueError) as err:
                    raise AtomicWriteNotQueued("retarget_not_authorized") from err
                if settings.ems_generation != write.snapshot_generation:
                    # A complete newer FC03 arrived while waiting. Nothing
                    # was sent: use the existing single fresh reprepare,
                    # including its persistence and authorization checks.
                    raise AtomicWriteNotQueued("stale_snapshot_generation")
        now = _aware_utc(dt_util.utcnow())
        if now is None or now >= hard_deadline:
            raise AtomicWriteNotQueued("retarget_not_authorized")
        try:
            request = self._control_lease_client.prepare_arm(
                transaction_id=transaction_id,
                hard_deadline=hard_deadline,
                block=_control_lease_block(block),
                challenge_nonce=challenge["nonce"],
                now_wall=now,
                now_monotonic=asyncio.get_running_loop().time(),
            )
        except ValueError as err:
            if str(err) == "control lease retarget changed transaction":
                raise AtomicWriteNotQueued("retarget_not_authorized") from err
            raise
        data = {
            key: value for key, value in request.items() if not key.startswith("_")
        }
        data.update(
            {
                "mode_code": int(block.mode),
                "self_use_soc": block.self_use_soc_percent_4301,
                "backup_soc": block.backup_soc_percent_4302,
                "force_charge_soc": block.force_charge_soc_percent_4303,
                "maximum_charge_power": block.maximum_charge_power_percent_4304,
                "force_discharge_soc": block.force_discharge_soc_percent_4305,
                "maximum_discharge_power": block.maximum_discharge_power_percent_4306,
                "snapshot_generation": write.snapshot_generation,
            }
        )
        service = self._esphome_named_action_service(
            "ems_supervisor_write_complete_block_leased"
        )
        self._assert_single_transport_instance()
        # The challenge above yields control to HA. A BMS or policy update
        # during that await invalidates the prepared block even if its helper
        # has not caught up. This is the final authorization before transport.
        if not self._leased_dispatch_frame_authorized(
            write, block, transaction_id=transaction_id,
        ):
            raise AtomicWriteNotQueued("retarget_not_authorized")
        response = await self.hass.services.async_call(
            _ESPHOME_DOMAIN,
            service,
            data,
            blocking=True,
            context=self._context,
            return_response=True,
        )
        if not self._control_lease_client.accept_arm(request, response, now_monotonic=asyncio.get_running_loop().time()):
            reason = (
                response.get("reason")
                if isinstance(response, Mapping)
                and type(response.get("reason")) is str
                else "lease_dispatch_rejected"
            )
            if reason in _EMS_PRETRANSPORT_REJECTIONS:
                raise AtomicWriteNotQueued(reason)
            raise RuntimeError(f"ESP control lease rejected before transport: {reason}")
        self._control_lease_error = None
        self._control_lease_last_result = None
        self._control_lease_gate = None
        self._sync_control_lease()

    def _leased_dispatch_frame_authorized(
        self,
        write: AtomicWrite,
        block: EmsBlock,
        *,
        transaction_id: str,
    ) -> bool:
        """Bind the exact leased write to a fresh frame after the challenge."""

        def deny(reason: str) -> bool:
            _LOGGER.warning(
                "EMS pretransport authorization refused: transaction_id=%s predicate=%s",
                transaction_id, reason,
            )
            return False

        if self._master_stop_latched or self._removed or self._unloading or self._pause_state != "off":
            return deny("stop_pause_or_unloading")
        if self._planner_cancel is not None or self._cohort_cancel is not None:
            return deny("publication_pending")
        controller = self._controller
        transaction = (
            controller.record.transaction if controller is not None else None
        )
        frame = (
            self._fresh_active_frame()
            if transaction is not None and transaction.owner in {ExecutionOwner.RCE, ExecutionOwner.TARIFF}
            else self._current_dispatch_frame()
        )
        if frame is None or transaction is None or controller is None:
            return deny("frame_or_transaction_missing")
        if (
            transaction.transaction_id != transaction_id
            or transaction.intent.command.ems_block != block
            or frame.now >= transaction.deadline
        ):
            return deny("transaction_block_or_deadline_changed")
        try:
            settings = settings_from_execution_source(frame.execution)
        except (ActiveBridgeError, TypeError, ValueError):
            return deny("settings_invalid")
        if settings.ems_generation != write.snapshot_generation:
            return deny("fc03_generation_changed")
        if controller.record.state is ActiveState.STARTING:
            return controller._command_frame_authorized(frame, now=frame.now) or deny("start_authorization_changed")
        if controller.record.state is ActiveState.RETARGETING:
            return controller._retarget_frame_authorized(frame) or deny("retarget_authorization_changed")
        return deny("execution_phase_changed")

    @callback
    def _executor_record_updated(self, record: ExecutorRecord) -> None:
        """Republish persisted lifecycle evidence without starting new work."""

        if self._removed:
            return
        self._release_completed_control_lease(record)
        self._publish_composite_state()
        self._schedule_master_stop_continuation_callback()
        self._sync_control_lease()
        if self._controller is not None:
            self._sync_settling_replan(self._controller)

    def _publish_composite_state(self, *, now: datetime | None = None) -> None:
        """Merge the pure decision with the persisted Active lifecycle."""

        if self._decision_state is None:
            return
        attributes = dict(self._decision_attributes)
        state = self._decision_state
        if self._controller is not None:
            selected_policy = attributes.get("selected_policy")
            selected_candidate_revision = attributes.get(
                "selected_candidate_revision"
            )
            observed_owner = attributes.get("observed_owner")
            observed_owner_conflict = attributes.get("owner_conflict")
            runtime = self._controller.recorder_attributes()
            transaction_policy = runtime.get("selected_policy")
            candidate_summaries = attributes.get("candidate_summaries")
            if not isinstance(candidate_summaries, (list, tuple)):
                candidate_summaries = ()
            transaction_candidate_revisions = [
                candidate.get("candidate_revision")
                for candidate in candidate_summaries
                if isinstance(candidate, Mapping)
                and candidate.get("policy_id") == transaction_policy
                and type(candidate.get("candidate_revision")) is int
            ]
            transaction_candidate_revision = (
                transaction_candidate_revisions[0]
                if len(transaction_candidate_revisions) == 1
                else None
            )
            attributes.update(runtime)
            lease = self._control_lease_client.handle
            attributes["control_lease"] = {
                "protocol_version": _CONTROL_LEASE_PROTOCOL_VERSION,
                "active": lease is not None,
                "transaction_id": lease.transaction_id if lease is not None else None,
                "command_generation": (
                    lease.command_generation if lease is not None else None
                ),
                "hard_deadline": (
                    lease.hard_deadline.isoformat() if lease is not None else None
                ),
                "sequence": lease.sequence if lease is not None else None,
                "renew_pending": (
                    lease.pending_sequence is not None if lease is not None else False
                ),
            }
            record = self._controller.record
            transaction_owner = (
                record.owner.value
                if record.transaction is not None
                else ExecutionOwner.NONE.value
            )
            attributes["observed_owner"] = observed_owner
            attributes["transaction_owner"] = transaction_owner
            attributes["owner_conflict"] = observed_owner_conflict
            # Public execution ownership exists only for a live transaction.
            # Keep any physically observed legacy writer separately for
            # diagnostics and conflict gating; it must not make normal IDLE
            # look like an acquired Supervisor owner.
            attributes["owner"] = transaction_owner
            if record.state is ActiveState.IDLE:
                if self._decision_state == SupervisorState.ACTIVE_SELECTED.value:
                    state = "selected"
                    attributes["execution_phase"] = "selected"
                    attributes["selected_policy"] = selected_policy
                    attributes["selected_candidate_revision"] = (
                        selected_candidate_revision
                    )
            else:
                state = runtime["execution_phase"]
                if record.transaction is not None:
                    attributes["selected_candidate_revision"] = (
                        transaction_candidate_revision
                    )
                else:
                    attributes["selected_policy"] = selected_policy
                    attributes["selected_candidate_revision"] = (
                        selected_candidate_revision
                    )
        if self._master_stop_latched:
            # This adapter-owned Store is the earlier boundary used when no
            # valid physical frame exists yet.  It may only make the core
            # executor stricter, never grant authority.
            attributes["starts_allowed"] = False
            attributes["automatic_policies_enabled"] = False
            attributes["master_stop_adapter_latched"] = True
            attributes["master_stop_adapter_requested_at"] = (
                self._master_stop_requested_at.isoformat()
                if self._master_stop_requested_at is not None
                else None
            )
            attributes["master_stop_adapter_persisted"] = (
                self._master_stop_latch_persisted
            )
        else:
            attributes["master_stop_adapter_latched"] = False
            attributes["master_stop_adapter_requested_at"] = None
            attributes["master_stop_adapter_persisted"] = False
        pause_state = getattr(self, "_pause_state", None)
        attributes["ems_paused"] = pause_state == "on"
        if pause_state not in {"on", "off"}:
            attributes["starts_allowed"] = False
            attributes["automatic_policies_enabled"] = False
            attributes["ems_paused"] = True
        execution_error = (
            self._master_stop_latch_persist_error
            or _health_text(attributes.get("execution_persistence_error"))
            or self._control_lease_error
            or self._execution_adapter_error
        )
        if execution_error is not None:
            attributes["execution_adapter_error"] = execution_error
        if self._control_lease_last_result is not None:
            attributes["control_lease_renewal"] = dict(
                self._control_lease_last_result
            )
        if self._control_lease_gate is not None:
            attributes["control_lease_gate"] = dict(self._control_lease_gate)
        if self._accounting_adapter_error is not None:
            attributes["accounting_adapter_error"] = self._accounting_adapter_error
        if self._notification_adapter_error is not None:
            attributes["notification_adapter_error"] = self._notification_adapter_error
        health_now = _aware_utc(now) if now is not None else _aware_utc(dt_util.utcnow())
        attributes["execution_health"] = _execution_health_contract(
            attributes,
            now=(
                health_now
                if health_now is not None
                else datetime.min.replace(tzinfo=timezone.utc)
            ),
        )
        recorded_execution = supervisor_recorder_projection(
            attributes, previous=self._recorded_execution_previous
        )
        attributes[RECORDED_EXECUTION_ATTRIBUTE] = recorded_execution
        if "recent_stop_decisions" in attributes:
            attributes[RECORDED_STOP_ATTRIBUTE] = stop_recorder_projection(attributes["recent_stop_decisions"])
        self._recorded_execution_previous = recorded_execution
        serialized = json.dumps(
            attributes,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
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

    async def async_added_to_hass(self) -> None:
        """Resolve sources, subscribe once and compute a fresh decision."""
        await super().async_added_to_hass()
        try:
            stored_master_stop = await self._master_stop_store.async_load()
            if stored_master_stop is not None:
                try:
                    (
                        self._master_stop_latched,
                        self._master_stop_requested_at,
                    ) = _decode_master_stop_latch(stored_master_stop)
                    self._master_stop_latch_persisted = self._master_stop_latched
                    self._master_stop_controller_start_pending = (
                        self._master_stop_latched
                    )
                except (TypeError, ValueError, OverflowError):
                    requested_at = _aware_utc(dt_util.utcnow())
                    if requested_at is None:
                        raise RuntimeError(
                            "UTC clock returned a naive MASTER STOP timestamp"
                        )
                    self._master_stop_latched = True
                    self._master_stop_requested_at = requested_at
                    await self._master_stop_store.async_save(
                        _master_stop_latch_payload(
                            latched=True,
                            requested_at=requested_at,
                        )
                    )
                    self._master_stop_latch_persisted = True
                    self._master_stop_controller_start_pending = True
                    _LOGGER.error(
                        "Stored EMS Supervisor MASTER STOP latch is invalid; "
                        "automatic starts remain blocked"
                    )
            stored_record = await self._executor_store.async_load()
            persisted: ExecutorRecord | None = None
            if stored_record is not None:
                try:
                    persisted = record_from_dict(stored_record)
                except (TypeError, ValueError, OverflowError):
                    _LOGGER.error(
                        "Stored EMS Supervisor executor record is invalid; "
                        "automatic starts remain blocked until MASTER STOP"
                    )
                    persisted = ExecutorRecord(
                        state=ActiveState.BLOCKED,
                        owner=ExecutionOwner.NONE,
                        starts_allowed=False,
                        automatic_policies_enabled=False,
                        reason=ExecutionReason.SNAPSHOT_INVALID,
                    )
            self._controller = SupervisorActiveController(
                persist=self._async_persist_executor_record,
                dispatch=self._async_dispatch_atomic_write,
                publish=self._executor_record_updated,
                persisted_record=persisted,
                frame_resampler=self._current_dispatch_frame,
                retarget_replan_pending=self._retarget_replan_pending,
                retarget_resume_frame=self._retarget_resume_frame,
                retarget_pending_context=self._retarget_pending_context,
                control_lease_valid_until=self._control_lease_valid_until,
                control_lease_identity=self._control_lease_identity,
                terminal_proof=self._async_control_lease_terminal_proof,
            )
            await self._controller.async_initialize()
            if (
                self._master_stop_latched
                and self._controller.record.master_stop_result.status
                is not MasterStopStatus.NOT_REQUESTED
            ):
                # A recovered requested/in-progress/completed generation must
                # be continued, never replaced by a fresh STOP generation.
                self._master_stop_controller_start_pending = False
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
            if _loaded_entry_count(self.hass) != 1:
                self._schedule_cardinality_safety_boundary()
            if self._master_stop_latched:
                try:
                    await self._async_disable_master_stop_helpers()
                except Exception:  # noqa: BLE001 - retain the recovered latch
                    self._execution_adapter_error = (
                        "master_stop_helper_cleanup_failed"
                    )
                    _LOGGER.exception(
                        "Recovered MASTER STOP could not finish helper cleanup"
                    )
                else:
                    self._master_stop_controls_fenced = True
            self._recompute(raise_on_error=True)
            self._schedule_master_stop_continuation_callback()
        except Exception:
            self._cleanup_lifecycle()
            raise

    @callback
    def _current_dispatch_frame(self) -> ActiveFrame | None:
        """Return a frame only when it still matches the live mode helper."""

        if self._planner_cancel is not None or self._cohort_cancel is not None:
            return None
        frame = self._latest_active_frame
        helper = _state_text(
            self.hass.states.get(_SUPERVISOR_MODE_ENTITY_ID)
        )
        paused = _state_text(self.hass.states.get(EMS_PAUSED_ENTITY_ID))
        expected = (
            SupervisorMode.OFF
            if paused is not None and paused != "off"
            else
            SupervisorMode.ACTIVE
            if helper == "Active"
            else SupervisorMode.OFF
            if helper == "Off"
            else None
        )
        if frame is None or expected is None or frame.decision.supervisor_mode is not expected:
            return None
        return frame

    def _retarget_pending_context(self) -> RetargetPendingContext:
        """Describe only the already scheduled bounded adapter callbacks."""

        return RetargetPendingContext(
            planner_pending=self._planner_cancel is not None,
            cohort_pending=self._cohort_cancel is not None,
            source=(
                self._cohort_pending_source
                if self._cohort_cancel is not None
                else ",".join(sorted(self._planner_pending_keys)) or None
            ),
            generation=(
                self._cohort_generation
                if self._cohort_cancel is not None
                else self._planner_generation
                if self._planner_cancel is not None
                else None
            ),
        )

    def _retarget_resume_frame(self) -> ActiveFrame | None:
        """Build a fresh frame solely for cancelling an unsent RCE successor.

        The normal dispatch resampler remains fail-closed while the cohort
        callback is pending.  This private snapshot never reaches transport;
        the executor still requires a coherent, fresh full FC03 readback,
        unchanged owner/command/deadline and every live safety gate before it
        may retain the already physical predecessor.
        """

        if (
            self._master_stop_latched or self._removed or self._unloading
            or self._planner_cancel is not None
        ):
            return None
        return self._fresh_active_frame()

    def _fresh_active_frame(self) -> ActiveFrame | None:
        """Rebuild one complete decision from live HA states without publishing it."""

        now = _aware_utc(dt_util.utcnow())
        if now is None:
            return None
        try:
            states = self._read_source_states()
            mode, profile, rce, tariff, rcm, execution = self._build_snapshots(
                states,
                now,
            )
            context = build_execution_context(execution, now=now)
            rcm = replace(
                rcm,
                export_state=context.export_state,
                direct_register_topology_allowed=(
                    context.topology_direct_register_allowed
                ),
                full_block_topology_allowed=context.topology_full_block_allowed,
            )
            rce, tariff, rcm, context = self._apply_executor_commitment(
                rce,
                tariff,
                rcm,
                context,
                execution,
                now=now,
            )
            candidates = (
                apply_minimum_grid_power(
                    build_rce_candidate(rce, now=now),
                    self._attrs(states, "rce_plan").get(
                        "current_slot_execution_export_power_kw"
                    ),
                ),
                apply_minimum_tariff_power(
                    build_tariff_candidate(tariff, now=now),
                    self._attrs(states, "tariff_plan"),
                ),
                build_rcm_candidate(rcm, now=now),
            )
            decision = arbitrate_supervisor(
                mode=mode,
                profile=profile,
                context=context,
                candidates=candidates,
                now=now,
            )
            return ActiveFrame(
                now=now,
                decision=decision,
                candidates=candidates,
                context=context,
                rce=rce,
                tariff=tariff,
                rcm=rcm,
                execution=execution,
            )
        except (ActiveBridgeError, TypeError, ValueError, OverflowError):
            return None

    def _retarget_replan_pending(self) -> bool:
        """Recognize only narrow planner gaps that can retain an unsent predecessor."""

        if (
            self._planner_cancel is None
            or self._cohort_cancel is not None
        ):
            return False
        if self._planner_pending_keys == {"rcm_plan"}:
            return (
                self._tariff_retarget_rcm_pending()
                or self._rce_retarget_rcm_pending()
            )
        if self._planner_pending_keys != {"rce_plan"}:
            return False

        def current(key: str) -> State | None:
            entity_id = self._source_entity_ids.get(key)
            return self.hass.states.get(entity_id) if entity_id is not None else None

        controller = self._controller
        if controller is None:
            return False
        record = controller.record
        transaction = record.transaction
        plan = current("rce_plan")
        plan_attrs = plan.attributes if plan is not None else {}
        plan_phase = (
            _exact_bool(plan_attrs.get("result_current")),
            _exact_bool(plan_attrs.get("recalculation_pending")),
        )
        plan_run_end = _iso_datetime(plan_attrs.get("current_run_end"))
        return bool(
            record.state is ActiveState.RETARGETING
            and record.owner is ExecutionOwner.RCE
            and transaction is not None
            and transaction.command_sent_at is None
            and transaction.intent.policy is ExecutionOwner.RCE
            and transaction.intent.action is ExecutionAction.RCE_EXPORT
            and _state_text(current("supervisor_mode")) == "Active"
            and _flag(current("allow_rce"))
            and _flag(current("rce_enabled"))
            and _tri_state(current("rce_active")) is True
            and _tri_state(current("sale_block_active")) is False
            and _enum_value(RcePlanStatus, plan_attrs.get("status_code"))
            is RcePlanStatus.READY
            and plan_phase in {(True, False), (False, True)}
            and _exact_bool(plan_attrs.get("current_slot_planned")) is True
            and _exact_bool(plan_attrs.get("current_slot_start_eligible")) is True
            and _exact_bool(plan_attrs.get("current_slot_continue_eligible"))
            is True
            and plan_run_end is not None
            and plan_run_end >= transaction.deadline
            and _input_datetime(current("rce_latched_slot_end"))
            == transaction.deadline
        )

    def _tariff_retarget_rcm_pending(self) -> bool:
        """Attest an idle RCEm update for cancellation only, never for a write."""

        controller = self._controller
        frame = self._latest_active_frame
        now = _aware_utc(dt_util.utcnow())
        if controller is None or frame is None or now is None:
            return False
        record = controller.record
        transaction = record.transaction
        if (
            record.state is not ActiveState.RETARGETING
            or record.owner is not ExecutionOwner.TARIFF
            or transaction is None
            or transaction.intent.policy is not ExecutionOwner.TARIFF
            or transaction.command_sent_at is not None
            or transaction.prewrite_snapshot is None
            or now >= transaction.deadline
        ):
            return False
        states = self._read_source_states()
        try:
            mode, profile, _rce, tariff, rcm, execution = self._build_snapshots(states, now)
            settings = settings_from_execution_source(execution)
        except (ActiveBridgeError, TypeError, ValueError):
            return False
        if (
            mode is not SupervisorMode.ACTIVE
            or profile is not frame.decision.profile
            or tariff.allowed_by_user is not True
            or tariff.enabled is not True
            or tariff.control_data_ready is not True
            or tariff.control_inputs_fresh is not True
            or tariff.forecast_data_fresh is not True
            or tariff.system_power_kw != frame.tariff.system_power_kw
            or tariff.maximum_soc_percent is None
            or transaction.prewrite_snapshot.ems_block.force_charge_soc_percent_4303
            > tariff.maximum_soc_percent
            or settings.freshness_errors(now, required_families=frozenset())
            or settings.ems_generation != transaction.prewrite_snapshot.ems_generation
            or not settings.values_match(transaction.prewrite_snapshot, required_families=frozenset())
            or rcm.live_emergency is not False
            or any(_tri_state(states.get(key)) is not False for key in (
                "rcm_active", "rcm_export_control_active", "rcm_pre_discharge_active",
            ))
            or any(value is not False for value in (
                execution.rce_active, execution.balancing_active,
                execution.manual_charge_active, execution.manual_discharge_active,
                execution.charge_timer_active, execution.discharge_timer_active,
            ))
        ):
            return False
        context = build_execution_context(execution, now=now)
        # An owned Mode4 can have every legacy active helper Off. Preserve the
        # persisted owner, as the normal commitment projection does, only after
        # rejecting other writers and matching its physical prewrite snapshot.
        context = replace(context, owner_kind=OwnerKind.TARIFF,
                          transaction_pending=True, transaction_owner_kind=OwnerKind.TARIFF)
        rcm = replace(
            rcm, export_state=context.export_state,
            direct_register_topology_allowed=context.topology_direct_register_allowed,
            full_block_topology_allowed=context.topology_full_block_allowed,
        )
        disabled = all(_tri_state(states.get(key)) is False for key in (
            "rcm_enabled", "rcm_export_control_enabled", "rcm_pre_discharge_enabled",
        ))
        if not disabled:
            candidate = build_rcm_candidate(rcm, now=now)
            if (
                any(_tri_state(states.get(key)) is None for key in (
                    "rcm_enabled", "rcm_export_control_enabled", "rcm_pre_discharge_enabled",
                    "allow_rcm",
                ))
                or rcm.result_current is not True
                or rcm.recalculation_pending is not False
                or rcm.action not in {RcmAction.MONITOR, RcmAction.HOLD}
                or candidate.available is not True
                or candidate.local_hard_stop
                or candidate.requested_action is not RequestedAction.NONE
            ):
                return False
        # The latest frame retains the committed plan. The normalizer above
        # supplies current physical and helper facts.
        proposal = same_run_tariff_desired(
            transaction.intent, frame.decision, frame.candidates,
            transaction.prewrite_snapshot, tariff=frame.tariff, now=now,
        )
        if proposal is None or proposal.intent != transaction.intent:
            return False
        gates = execution_gates(
            execution, owner_kind=context.owner_kind,
            owner_conflict=context.owner_conflict, now=now, candidate=proposal.candidate,
        )
        predecessor_intent = replace(transaction.intent, command=replace(
            transaction.intent.command, ems_block=transaction.prewrite_snapshot.ems_block,
        ))
        return bool(
            gates.inputs_fresh and gates.bms_fresh and gates.full_block_ready
            and gates.full_block_topology_ready and gates.charge_direction_ready
            and not gates.owner_conflict
            and gates.observed_owner in {None, ExecutionOwner.TARIFF}
            and tariff_command_within_live_bms_limit(
                predecessor_intent, execution, tariff=frame.tariff, now=now,
            )
        )

    def _rce_retarget_rcm_pending(self) -> bool:
        """Attest a fresh non-acting RCEm update while retaining RCE A."""

        controller = self._controller
        now = _aware_utc(dt_util.utcnow())
        if controller is None or now is None:
            return False
        record = controller.record
        transaction = record.transaction
        if (
            record.state is not ActiveState.RETARGETING
            or record.owner is not ExecutionOwner.RCE
            or transaction is None
            or transaction.intent.policy is not ExecutionOwner.RCE
            or transaction.command_sent_at is not None
            or transaction.prewrite_snapshot is None
            or now >= transaction.deadline
        ):
            return False
        states = self._read_source_states()
        enable_flags = tuple(_tri_state(states.get(key)) for key in (
            "allow_rcm", "rcm_enabled", "rcm_export_control_enabled",
            "rcm_pre_discharge_enabled",
        ))
        active_flags = tuple(_tri_state(states.get(key)) for key in (
            "rcm_active", "rcm_export_control_active", "rcm_pre_discharge_active",
        ))
        if (
            any(value is None for value in (*enable_flags, *active_flags))
            or any(value is True for value in active_flags)
        ):
            return False
        try:
            mode, profile, rce, tariff, rcm, execution = self._build_snapshots(
                states,
                now,
            )
            settings = settings_from_execution_source(execution)
        except (ActiveBridgeError, TypeError, ValueError):
            return False
        if mode is not SupervisorMode.ACTIVE or rcm.live_emergency is not False:
            return False
        context = build_execution_context(execution, now=now)
        rcm = replace(
            rcm,
            export_state=context.export_state,
            direct_register_topology_allowed=(
                context.topology_direct_register_allowed
            ),
            full_block_topology_allowed=context.topology_full_block_allowed,
        )
        rce, tariff, rcm, context = self._apply_executor_commitment(
            rce,
            tariff,
            rcm,
            context,
            execution,
            now=now,
        )
        candidates = (
            apply_minimum_grid_power(
                build_rce_candidate(rce, now=now),
                self._attrs(states, "rce_plan").get(
                    "current_slot_execution_export_power_kw"
                ),
            ),
            apply_minimum_tariff_power(
                build_tariff_candidate(tariff, now=now),
                self._attrs(states, "tariff_plan"),
            ),
            build_rcm_candidate(rcm, now=now),
        )
        if any(value is True for value in enable_flags) and not (
            rcm.result_current is True
            and rcm.recalculation_pending is False
            and rcm.action in {
                RcmAction.MONITOR, RcmAction.HOLD,
                RcmAction.RESTORE, RcmAction.RELEASE_EXPORT,
            }
            and candidates[2].available is True
        ):
            return False
        # Enabled RCEm can publish RESTORE after voltage returns to normal
        # without owning an actuator. Its label alone does not revoke RCE A.
        # The attestation below still requires fresh GCF/306 equal to A's
        # immutable baseline and rejects any unresolved RCEm cleanup.
        inactive_rcm_attested = self._inactive_rcm_predecessor_attested(
            record=record,
            transaction=transaction,
            action=rcm.action,
            settings=settings,
            now=now,
        )
        decision = arbitrate_supervisor(
            mode=mode,
            profile=profile,
            context=context,
            candidates=candidates,
            now=now,
        )
        rce_candidate = candidates[0]
        gates = execution_gates(
            execution,
            owner_kind=context.owner_kind,
            owner_conflict=context.owner_conflict,
            now=now,
            candidate=rce_candidate,
        )
        return bool(
            inactive_rcm_attested
            and candidates[2].requested_action is RequestedAction.NONE
            and not candidates[2].local_hard_stop
            and same_window_rce_wait_authorized(
                transaction.intent,
                decision,
                candidates,
                settings,
                export_state=context.export_state,
                rce=rce,
                now=now,
            )
            and gates.inputs_fresh
            and gates.bms_fresh
            and gates.full_block_ready
            and gates.full_block_topology_ready
            and gates.discharge_direction_ready
            and not gates.owner_conflict
            and gates.observed_owner in {None, ExecutionOwner.RCE}
            and rce_sent_command_within_live_bms_limit(
                transaction.intent,
                execution,
                rce=rce,
                now=now,
            )
        )

    def _inactive_rcm_predecessor_attested(
        self,
        *,
        record: ExecutorRecord,
        transaction: TransactionRecord,
        action: RcmAction | None,
        settings: SettingsSnapshot,
        now: datetime,
    ) -> bool:
        """Prove a non-acting RCEm restore/release has no unsettled authority.

        ``restore`` and ``release_export`` are optimizer descriptions, not by
        themselves proof that an RCEm cleanup is still owed.  They may retain
        only the already-confirmed RCE predecessor, and only when the current
        physical GCF/306 cohorts still match that transaction's immutable
        rollback baseline.  This helper never authorizes the unsent successor.
        """

        last = record.last_transaction
        if (
            last is not None
            and last.owner is ExecutionOwner.RCM
            and (
                last.rollback_status in {RollbackStatus.PENDING, RollbackStatus.FAILED}
                or last.state
                in {
                    ActiveState.STOPPING,
                    ActiveState.RESTORING,
                    ActiveState.FAULT,
                }
            )
        ):
            _LOGGER.debug(
                "RCE predecessor retention rejected: inactive RCEm cleanup "
                "is unsettled action=%s rollback=%s state=%s",
                action.value if action is not None else None,
                last.rollback_status.value,
                last.state.value,
            )
            return False

        if action in {RcmAction.MONITOR, RcmAction.HOLD}:
            return True
        if action not in {RcmAction.RESTORE, RcmAction.RELEASE_EXPORT}:
            return False

        baseline = transaction.command_snapshot
        required = frozenset(
            {
                AtomicWriteFamily.EMS_COMPLETE_BLOCK,
                AtomicWriteFamily.GCF_EXPORT_LIMIT,
                AtomicWriteFamily.BATTERY_CHARGE_LIMIT,
            }
        )
        if (
            baseline is None
            or not baseline.gcf_available
            or not baseline.gcf_coherent
            or not baseline.battery_306_available
            or not baseline.battery_coherent
            or not settings.gcf_available
            or not settings.gcf_coherent
            or not settings.battery_306_available
            or not settings.battery_coherent
            or settings.freshness_errors(now, required_families=required)
            or settings.gcf_enabled_258 != baseline.gcf_enabled_258
            or settings.export_limit_percent_259
            != baseline.export_limit_percent_259
            or settings.battery_max_charge_power_percent_306
            != baseline.battery_max_charge_power_percent_306
        ):
            _LOGGER.debug(
                "RCE predecessor retention rejected: inactive RCEm baseline "
                "is not freshly attested action=%s",
                action.value,
            )
            return False
        return True

    async def async_will_remove_from_hass(self) -> None:
        """Persist a stop/recovery boundary before detaching this instance."""

        self._unloading = True
        try:
            await self._async_cancel_controller_task()
            await self._async_wait_safety_task()
            controller = self._controller
            if controller is not None and (
                controller.record.owner is not ExecutionOwner.NONE
                or controller.record.transaction is not None
            ):
                await self._async_latch_master_stop()
                frame = self._latest_active_frame
                if frame is not None and self._available:
                    await self._async_advance_master_stop(controller, frame)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - persisted recovery remains authoritative
            _LOGGER.exception(
                "EMS Supervisor unload retained a fail-closed recovery boundary"
            )
        finally:
            self._cleanup_lifecycle()
            await super().async_will_remove_from_hass()

    async def _async_cancel_controller_task(self) -> None:
        """Cancel and join the executor task without swallowing caller cancellation."""

        task = self._controller_task
        if task is None or task.done():
            self._controller_task = None
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        finally:
            self._controller_task = None

    async def _async_wait_safety_task(self) -> None:
        """Join the small Store write used by a cardinality transition."""

        task = self._safety_task
        if task is None or task.done():
            self._safety_task = None
            return
        try:
            await task
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
        finally:
            self._safety_task = None

    def _cleanup_lifecycle(self) -> None:
        """Idempotently detach this exact lifecycle and clear its decision."""
        self._removed = True
        self._guard_ready = False
        self._planner_generation += 1
        self._cohort_generation += 1
        self._temporal_generation += 1
        self._cancel_planner_callback()
        self._cancel_cohort_callback()
        self._cancel_power_cohort_callback(clear_pending=True)
        self._cancel_temporal_callback()
        self._cancel_execution_watchdog()
        self._cancel_control_lease_callback()
        if self._control_lease_task is not None:
            self._control_lease_task.cancel()
            self._control_lease_task = None
        self._control_lease_client.invalidate()
        self._cancel_settling_replan()
        if self._settling_replan_task is not None:
            self._settling_replan_task.cancel()
            self._settling_replan_task = None
        self._cancel_master_stop_continuation_callback()
        if self._controller_task is not None:
            self._controller_task.cancel()
            self._controller_task = None
        if self._safety_task is not None:
            self._safety_task.cancel()
            self._safety_task = None
        self._pending_active_frame = None
        self._pending_active_frame_revision = None
        self._latest_active_frame = None
        self._unsubscribe("_state_unsub")
        self._unsubscribe("_cohort_report_unsub")
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
        self._decision_state = None
        self._decision_attributes = {}
        self._controller = None
        self._accounting_sink = None
        self._notification_sink = None
        self._settling_market_source = None
        self._settling_replan_request = None

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
            self._cancel_cohort_callback()
            self._cancel_power_cohort_callback(clear_pending=True)
            self._cancel_temporal_callback()
            self._publish_unavailable()
            self._schedule_cardinality_safety_boundary()
            return
        if self._cohort_cancel is not None:
            # The bounded cohort pass will observe the restored cardinality.
            return
        self._recompute()

    @callback
    def _schedule_cardinality_safety_boundary(self) -> None:
        """Persist starts-off when instance cardinality becomes ambiguous."""

        if self._unloading or (
            self._safety_task is not None and not self._safety_task.done()
        ):
            return
        self._safety_task = self._entry.async_create_background_task(
            self.hass,
            self._async_cardinality_safety_boundary(),
            "Hoymiles EMS Supervisor cardinality safety boundary",
        )

    async def _async_cardinality_safety_boundary(self) -> None:
        """Store the boundary; restoration resumes only after fresh uniqueness."""

        try:
            await self._async_latch_master_stop()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - cardinality remains transport-blocked
            self._execution_adapter_error = "cardinality_latch_failed"
            _LOGGER.exception(
                "EMS Supervisor could not persist the cardinality safety latch"
            )
        finally:
            self._safety_task = None

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
        self._unsubscribe("_cohort_report_unsub")
        watched_ids = tuple(sorted(self._keys_by_entity_id))
        self._state_unsub = async_track_state_change_event(
            self.hass,
            watched_ids,
            self._async_source_state_changed,
        )
        cohort_ids = tuple(
            entity_id
            for entity_id in watched_ids
            if any(
                key in _READBACK_COHORT_KEYS or key in _POWER_FLOW_COHORT_KEYS
                for key in self._keys_by_entity_id[entity_id]
            )
        )
        if cohort_ids:
            self._cohort_report_unsub = async_track_state_report_event(
                self.hass,
                cohort_ids,
                self._async_cohort_source_reported,
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
        self._cancel_power_cohort_callback(clear_pending=True)
        self._power_cohort_states.clear()
        self._power_cohort_generation = 0
        if self._cohort_cancel is not None:
            # Keep the physical trailing edge; it reads the newly resolved map.
            return
        self._cancel_cohort_callback()
        self._recompute()

    @callback
    def _async_core_config_updated(self, event: Event) -> None:
        if self._removed or "time_zone" not in event.data:
            return
        self._cancel_planner_callback()
        if self._cohort_cancel is not None:
            # The pending pass reads the current Home Assistant time zone.
            return
        self._cancel_cohort_callback()
        self._recompute()

    @callback
    def _async_source_state_changed(self, event: Event) -> None:
        if self._removed:
            return
        entity_id = event.data.get("entity_id")
        keys = self._keys_by_entity_id.get(entity_id, ())
        if not keys:
            return
        if keys == ("rce_price_above_threshold",):
            # Legacy display helper; authoritative slot gates arrive with the
            # plan. Its pending/unavailable transition must not invalidate a
            # prepared command or open another planner callback during retarget.
            return
        plan_keys = tuple(key for key in keys if key in _PLAN_ATTRIBUTES_BY_KEY)
        if plan_keys and all(
            self._plan_projection(event.data.get("old_state"), key)
            == self._plan_projection(event.data.get("new_state"), key)
            for key in plan_keys
        ):
            return
        if any(key in _POWER_FLOW_COHORT_KEYS for key in keys):
            # HA sends changed values/attributes through state_changed and
            # unchanged writes through state_reported. Both feed one collector.
            generation = self._power_cohort_generation
            self._collect_power_cohort_report(keys, event.data.get("new_state"))
            transaction = (
                self._controller.record.transaction
                if self._controller is not None else None
            )
            if (
                (transaction is None or transaction.owner is not ExecutionOwner.TARIFF)
                and "grid_power" not in keys
                and self._power_cohort_generation == generation
            ):
                self._recompute()
            return
        if any(key in _READBACK_COHORT_KEYS for key in keys):
            self._cancel_planner_callback()
            self._schedule_cohort_callback(source="state_changed")
            return
        if any(key not in _PLANNER_KEYS for key in keys):
            self._cancel_planner_callback()
            if self._cohort_cancel is not None:
                # Consume this hard event in the already bounded trailing-edge
                # readback pass.  Publishing now could expose a mixed physical
                # generation before the final unchanged cohort report arrives.
                return
            self._recompute()
            return
        if self._cohort_cancel is not None:
            # The pending readback pass reads the latest planner state too.
            # A separate planner timer could otherwise publish the mixed
            # physical generation before its final unchanged report arrives.
            return
        self._schedule_planner_callback(
            frozenset(key for key in keys if key in _PLANNER_KEYS)
        )

    @callback
    def _async_cohort_source_reported(self, event: Event) -> None:
        """Coalesce only physical cohort reports into one complete snapshot."""

        if self._removed:
            return
        entity_id = event.data.get("entity_id")
        keys = self._keys_by_entity_id.get(entity_id, ())
        if any(key in _POWER_FLOW_COHORT_KEYS for key in keys):
            self._collect_power_cohort_report(keys, event.data.get("new_state"))
        if any(key in _READBACK_COHORT_KEYS for key in keys):
            self._cancel_planner_callback()
            self._schedule_cohort_callback(source="state_reported")

    @callback
    def _schedule_active_frame(self, frame: ActiveFrame) -> None:
        """Coalesce physical updates so stale frames never queue behind writes."""

        self._latest_active_frame = frame
        if self._unloading:
            return
        self._pending_active_frame = frame
        self._pending_active_frame_revision = (
            self._controller.record.revision if self._controller is not None else None
        )
        self._ensure_active_frame_drain()

    @callback
    def _ensure_active_frame_drain(self) -> None:
        """Start the drain without changing an already queued frame's revision."""

        if self._controller_task is not None and not self._controller_task.done():
            return
        task = self._entry.async_create_background_task(
            self.hass,
            self._async_drain_active_frames(),
            "Hoymiles EMS Supervisor Active executor",
        )
        self._controller_task = task

    async def _async_drain_active_frames(self) -> None:
        """Run only the newest frame through the controller's single lock."""

        try:
            while (
                not self._removed
                and not self._unloading
                and self._pending_active_frame is not None
            ):
                frame = self._pending_active_frame
                frame_revision = self._pending_active_frame_revision
                self._pending_active_frame = None
                self._pending_active_frame_revision = None
                controller = self._controller
                if controller is None:
                    return
                if frame_revision != controller.record.revision:
                    # A physical update can queue while lifecycle persistence
                    # awaits I/O. Rebuild its ownership/candidates against the
                    # current record; never consume an old WAITING projection
                    # after the controller has already entered EXECUTING.
                    if self._cohort_cancel is not None:
                        # The existing trailing edge owns the next complete
                        # physical snapshot. Do not read a partial cohort or
                        # cancel/renew its timer merely for this rebuild.
                        return
                    try:
                        self._recompute(raise_on_error=True)
                    except Exception:
                        self._pending_active_frame = None
                        self._pending_active_frame_revision = None
                        raise
                    if (
                        not self._guard_ready
                        or not self._available
                        or _loaded_entry_count(self.hass) != 1
                        or self._pending_active_frame is None
                        or self._pending_active_frame is frame
                        or self._pending_active_frame_revision != controller.record.revision
                    ):
                        self._pending_active_frame = None
                        self._pending_active_frame_revision = None
                        raise RuntimeError("Active lifecycle projection rebuild unavailable")
                    continue
                if self._master_stop_latched:
                    await self._async_advance_master_stop(controller, frame)
                else:
                    await controller.async_reconcile(frame)
                self._sync_execution_watchdog(controller)
                self._sync_settling_replan(controller)
                self._execution_adapter_error = None
                await self._async_publish_accounting(controller.record, frame)
                self._publish_notifications(controller.record, frame)
                await self._async_finalize_master_stop_if_safe()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - execution must remain fail closed
            self._execution_adapter_error = "active_reconcile_failed"
            self._publish_composite_state()
            _LOGGER.exception("EMS Supervisor Active reconciliation failed closed")
        finally:
            self._controller_task = None
            if (
                not self._removed
                and not self._unloading
                and self._pending_active_frame is not None
            ):
                self._ensure_active_frame_drain()

    async def _async_publish_accounting(
        self,
        record: ExecutorRecord,
        frame: ActiveFrame,
    ) -> None:
        """Publish physical evidence without coupling it to execution success."""

        sink = self._accounting_sink
        if sink is None:
            return
        try:
            await sink(record, frame, dict(self._source_entity_ids))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - accounting remains independently closed
            self._accounting_adapter_error = "accounting_v2_update_failed"
            self._publish_composite_state()
            _LOGGER.exception("EMS Supervisor accounting v2 update failed closed")
        else:
            self._accounting_adapter_error = None

    def _publish_notifications(
        self,
        record: ExecutorRecord,
        frame: ActiveFrame,
    ) -> None:
        """Enqueue immutable evidence without awaiting notifier I/O."""

        sink = self._notification_sink
        if sink is None:
            return
        try:
            sink(record, frame, dict(self._source_entity_ids))
        except Exception:  # noqa: BLE001 - notifications cannot stop execution
            self._notification_adapter_error = "notification_update_failed"
            self._publish_composite_state()
            _LOGGER.exception("EMS range notification update failed closed")

    async def async_master_stop(self) -> None:
        """Latch, execute and publish the one installation-wide MASTER STOP."""

        # The synchronous fence must exist before either concurrent branch can
        # run. Adapter Store I/O is then observed without gating the controller.
        self.request_master_stop()
        latch_task = asyncio.create_task(self._async_latch_master_stop())
        try:
            controller = self._controller
            frame = self._latest_active_frame
            if controller is not None and frame is not None and self._available:
                await self._async_advance_master_stop(
                    controller,
                    frame,
                    retry_blocked=True,
                )
                self._sync_execution_watchdog(controller)
                await self._async_publish_accounting(controller.record, frame)
                self._publish_notifications(controller.record, frame)
                await self._async_finalize_master_stop_if_safe()
        finally:
            await latch_task

    async def async_finish_master_stop_controls(self) -> None:
        """Fence late UI work, then finish bounded helper cleanup."""

        await self._async_disable_master_stop_helpers()
        self._master_stop_controls_fenced = True
        await self._async_finalize_master_stop_if_safe()

    async def _async_advance_master_stop(
        self,
        controller: SupervisorActiveController,
        frame: ActiveFrame,
        *,
        retry_blocked: bool = False,
    ) -> None:
        """Start MASTER STOP once, then continue only its persisted lifecycle."""

        record = controller.record
        transaction = record.transaction
        initial_request = (
            record.master_stop_result.status
            in {MasterStopStatus.NOT_REQUESTED, MasterStopStatus.COMPLETED}
            and (transaction is None or not transaction.master_stop_requested)
        )
        explicit_retry = (
            retry_blocked
            and record.master_stop_result.status
            in {MasterStopStatus.BLOCKED, MasterStopStatus.FAILED}
            and (
                transaction is None
                or transaction.master_stop_requested
            )
        )
        start_pending = getattr(
            self,
            "_master_stop_controller_start_pending",
            initial_request and retry_blocked,
        )
        if (initial_request and start_pending) or explicit_retry:
            self._master_stop_controller_start_pending = False
            try:
                await controller.async_master_stop(frame)
            except ActiveBridgeError:
                # The adapter Store already forbids starts.  Wait for a newer
                # coherent settings frame instead of weakening the latch.
                self._master_stop_controller_start_pending = True
                return
            except BaseException:
                self._master_stop_controller_start_pending = True
                raise
            transaction = controller.record.transaction
            accepted = bool(
                controller.record.master_stop_result.status
                is not MasterStopStatus.NOT_REQUESTED
                or (
                    transaction is not None
                    and transaction.master_stop_requested
                )
            )
            if initial_request and not accepted:
                # A pending manual-proxy readback deliberately makes the
                # controller return the unchanged IDLE record.  The adapter
                # fence is already active, but admission has not happened, so
                # retain the one start token for the next coherent frame.
                self._master_stop_controller_start_pending = True
            return
        if (
            record.master_stop_result.status is MasterStopStatus.COMPLETED
            and not getattr(controller, "record_persisted", True)
        ):
            await controller.async_retry_master_stop_persistence()
            return
        if (
            record.master_stop_result.status
            in {MasterStopStatus.REQUESTED, MasterStopStatus.IN_PROGRESS}
            or (transaction is not None and transaction.master_stop_requested)
        ):
            await controller.async_reconcile(frame)
            if not getattr(controller, "record_persisted", True):
                # Physical reconciliation always wins this turn. Once it has
                # run, retry only the newest in-memory safety revision.
                await controller.async_retry_master_stop_persistence()

    async def _async_disable_master_stop_helpers(self) -> None:
        """Disable policy requests/timers after the synchronous starts-off fence."""

        present_booleans = tuple(
            entity_id
            for entity_id in _MASTER_STOP_INPUT_BOOLEANS
            if self.hass.states.get(entity_id) is not None
        )
        if present_booleans:
            await asyncio.wait_for(
                self.hass.services.async_call(
                    "input_boolean",
                    "turn_off",
                    {"entity_id": present_booleans},
                    blocking=True,
                    context=self._context,
                ),
                timeout=_MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS,
            )
        present_timers = tuple(
            entity_id
            for entity_id in _MASTER_STOP_TIMERS
            if self.hass.states.get(entity_id) is not None
        )
        if present_timers:
            await asyncio.wait_for(
                self.hass.services.async_call(
                    "timer",
                    "cancel",
                    {"entity_id": present_timers},
                    blocking=True,
                    context=self._context,
                ),
                timeout=_MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS,
            )

    async def _async_finalize_master_stop_if_safe(self) -> None:
        """Expose Off only after restore ACK and persisted owner release."""

        controller = self._controller
        if (
            controller is None
            or not self._master_stop_latched
            or not self._master_stop_controls_fenced
            or not getattr(controller, "record_persisted", True)
        ):
            return
        record = controller.record
        if (
            record.master_stop_result.status is not MasterStopStatus.COMPLETED
            or record.owner is not ExecutionOwner.NONE
            or record.transaction is not None
        ):
            return
        # Repeat idempotent helper cleanup here: a restart may have happened
        # after the durable latch write but before the first cleanup call.
        await self._async_disable_master_stop_helpers()
        mode_entity = "input_select.hoymiles_ems_supervisor_mode"
        if self.hass.states.get(mode_entity) is None:
            return
        await asyncio.wait_for(
            self.hass.services.async_call(
                "input_select",
                "select_option",
                {"entity_id": mode_entity, "option": "Off"},
                blocking=True,
                context=self._context,
            ),
            timeout=_MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS,
        )
        await self._async_clear_master_stop_latch()

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
        self._planner_pending_keys.clear()
        if cancel is not None:
            cancel()

    def _master_stop_continuation_pending(self) -> bool:
        """Return whether only a persisted pending stop may request a fresh frame."""

        if (
            self._removed
            or self._unloading
            or not self._guard_ready
            or not self._master_stop_latched
            or _loaded_entry_count(self.hass) != 1
        ):
            return False
        controller = self._controller
        if controller is None:
            return False
        record = controller.record
        transaction = record.transaction
        return bool(
            (
                transaction is not None
                and transaction.master_stop_requested
                and transaction.rollback_status is RollbackStatus.PENDING
                and record.master_stop_result.status
                in {MasterStopStatus.REQUESTED, MasterStopStatus.IN_PROGRESS}
            )
            or (
                record.master_stop_result.status is MasterStopStatus.COMPLETED
                and (
                    not getattr(controller, "record_persisted", True)
                    or not self._master_stop_latch_persisted
                )
            )
        )

    def _cancel_master_stop_continuation_callback(self) -> None:
        """Cancel the private recovery frame request without changing execution."""

        self._master_stop_continuation_generation += 1
        cancel = self._master_stop_continuation_cancel
        self._master_stop_continuation_cancel = None
        if cancel is not None:
            cancel()

    def _schedule_master_stop_continuation_callback(self) -> None:
        """Request a later fresh frame only for a durable pending MASTER STOP."""

        if not self._master_stop_continuation_pending():
            self._cancel_master_stop_continuation_callback()
            return
        if self._master_stop_continuation_cancel is not None:
            return
        self._master_stop_continuation_generation += 1
        generation = self._master_stop_continuation_generation

        @callback
        def master_stop_continuation_callback(_now: datetime) -> None:
            if generation != self._master_stop_continuation_generation:
                return
            self._master_stop_continuation_cancel = None
            if not self._master_stop_continuation_pending():
                return
            # This never invokes a policy or transport directly.  The existing
            # MASTER STOP state machine decides whether a newer physical frame
            # is safe enough to continue its already-persisted rollback.
            self._recompute()
            self._schedule_master_stop_continuation_callback()

        self._master_stop_continuation_cancel = async_call_later(
            self.hass,
            _MASTER_STOP_CONTINUATION_DELAY_SECONDS,
            master_stop_continuation_callback,
        )

    def _schedule_planner_callback(self, keys: frozenset[str]) -> None:
        pending_keys = self._planner_pending_keys | set(keys)
        self._cancel_planner_callback()
        self._planner_pending_keys = pending_keys
        generation = self._planner_generation

        @callback
        def planner_callback(_now: datetime) -> None:
            if (
                self._removed
                or generation != self._planner_generation
            ):
                return
            self._planner_cancel = None
            self._planner_pending_keys.clear()
            self._recompute()

        self._planner_cancel = async_call_later(
            self.hass,
            _PLANNER_DELAY_SECONDS,
            planner_callback,
        )

    def _cancel_cohort_callback(self) -> None:
        self._cohort_generation += 1
        cancel = self._cohort_cancel
        self._cohort_cancel = None
        self._cohort_pending_source = None
        if cancel is not None:
            cancel()

    def _schedule_cohort_callback(self, *, source: str) -> None:
        # The coherent trailing-edge pass will calculate a fresh semantic
        # boundary.  Retaining the old one could publish a mixed generation
        # while the physical cohort is still arriving.
        self._cancel_temporal_callback()
        self._cancel_cohort_callback()
        generation = self._cohort_generation
        self._cohort_pending_source = source

        @callback
        def cohort_callback(_now: datetime) -> None:
            if self._removed or generation != self._cohort_generation:
                return
            self._cohort_cancel = None
            self._cohort_pending_source = None
            self._recompute()

        self._cohort_cancel = async_call_later(
            self.hass,
            _READBACK_COHORT_DELAY_SECONDS,
            cohort_callback,
        )

    def _cancel_power_cohort_callback(self, *, clear_pending: bool) -> None:
        cancel = self._power_cohort_cancel
        self._power_cohort_cancel = None
        if cancel is not None:
            cancel()
        if clear_pending:
            self._power_cohort_pending.clear()

    @callback
    def _collect_power_cohort_report(
        self,
        keys: tuple[str, ...],
        new_state: State | None,
    ) -> None:
        """Accept only one complete, bounded four-channel power generation."""

        if self._removed or new_state is None:
            return
        power_keys = tuple(key for key in keys if key in _POWER_FLOW_COHORT_KEYS)
        if not power_keys:
            return
        for key in power_keys:
            self._power_cohort_pending[key] = new_state
        if self._power_cohort_cancel is None:
            @callback
            def collection_deadline(_now: datetime) -> None:
                self._power_cohort_cancel = None
                # A partial set is deliberately discarded.  The previous
                # complete proof remains frozen with its original timestamps.
                self._power_cohort_pending.clear()

            self._power_cohort_cancel = async_call_later(
                self.hass,
                _POWER_COHORT_COLLECTION_SECONDS,
                collection_deadline,
            )
        cohort_complete = _POWER_FLOW_COHORT_KEYS.issubset(
            self._power_cohort_pending
        )
        if "grid_power" in power_keys and not cohort_complete:
            # A fresh GRID export must reach the physical contradiction gate
            # without waiting for LOAD. The frozen cohort still supplies all
            # values used for balance/confirmation, so this cannot authorize
            # a new action or rejuvenate the accepted proof.
            self._recompute()
        if cohort_complete:
            self._cancel_power_cohort_callback(clear_pending=False)
            self._power_cohort_states = {
                key: self._power_cohort_pending[key]
                for key in _POWER_FLOW_COHORT_KEYS
            }
            self._power_cohort_pending.clear()
            self._power_cohort_generation += 1
            self._recompute()

    def _cancel_temporal_callback(self) -> None:
        self._temporal_generation += 1
        cancel = self._temporal_cancel
        self._temporal_cancel = None
        if cancel is not None:
            cancel()

    def _cancel_execution_watchdog(self) -> None:
        self._execution_watchdog_generation += 1
        cancel = self._execution_watchdog_cancel
        self._execution_watchdog_cancel = None
        self._execution_watchdog_key = None
        if cancel is not None:
            cancel()

    def _cancel_control_lease_callback(self) -> None:
        cancel = self._control_lease_cancel
        self._control_lease_cancel = None
        if cancel is not None:
            cancel()

    def _release_completed_control_lease(self, record: ExecutorRecord) -> None:
        """Drop only the right whose confirmed terminal record was published."""

        lease = self._control_lease_client.handle
        completed = record.last_transaction
        if (
            record.state is not ActiveState.IDLE
            or record.transaction is not None
            or completed is None
            or (lease is not None and completed.transaction_id != lease.transaction_id)
            or completed.owner is not ExecutionOwner.NONE
            or completed.rollback_status is not RollbackStatus.CONFIRMED
        ):
            return
        # Expiry invalidates the handle before restoration finishes. Match its
        # bounded diagnostic to the confirmed terminal transaction as well;
        # never let an old terminal record clear a newer transaction's fault.
        gate = self._control_lease_gate
        if (self._control_lease_error == "control_lease_lost:local_soft_deadline"
            and gate is not None
            and gate.get("transaction_id") == completed.transaction_id):
            self._control_lease_error = None
        if lease is None:
            return
        self._cancel_control_lease_callback()
        task = self._control_lease_task
        if task is not None:
            current = asyncio.current_task()
            if task is not current:
                task.cancel()
            self._control_lease_task = None
        self._control_lease_client.invalidate()

    def _record_control_lease_gate(
        self, *, status: str, reason: str, transaction_id: str
    ) -> None:
        """Publish only transitions in local renewal eligibility."""

        previous = self._control_lease_gate
        if (
            previous is not None
            and previous["status"] == status
            and previous["reason"] == reason
            and previous["transaction_id"] == transaction_id
        ):
            return
        now = _aware_utc(dt_util.utcnow())
        if now is None:
            return
        last_success_at = (
            previous.get("last_success_at")
            if previous is not None and previous["transaction_id"] == transaction_id
            else None
        )
        lease = self._control_lease_client.handle
        soft_valid_until = self._control_lease_valid_until()
        frame = self._latest_active_frame
        self._control_lease_gate = {
            "status": status,
            "reason": reason,
            "transaction_id": transaction_id,
            "first_at": now.isoformat(),
            "last_success_at": last_success_at,
            "soft_valid_until": (
                soft_valid_until.isoformat() if soft_valid_until is not None else None
            ),
            "hard_deadline": (
                lease.hard_deadline.isoformat() if lease is not None else None
            ),
            "rce_input_revision": frame.rce.input_revision if frame is not None else None,
            "rce_recalculation_pending": (
                frame.rce.recalculation_pending if frame is not None else None
            ),
        }
        # One bounded transition, not a history growing on every renewal.
        if (reason == "local_soft_deadline" and previous is not None
            and previous.get("transaction_id") == transaction_id
            and previous.get("status") == "denied"):
            self._control_lease_gate["preceding_denial"] = {
                key: previous.get(key) for key in ("reason", "first_at", "last_success_at")
            }
        self._publish_composite_state(now=now)

    def _control_lease_renewal_evidence(
        self,
    ) -> tuple[int, tuple[float, ...]] | None:
        """Return a fresh authorized physical frame, never a heartbeat."""

        client = self._control_lease_client
        self._control_lease_authorization_deadline = None
        lease = client.handle
        controller = self._controller
        frame = self._latest_active_frame
        if lease is None:
            return None
        def deny(reason: str) -> None:
            self._record_control_lease_gate(
                status="denied",
                reason=reason,
                transaction_id=lease.transaction_id,
            )
            return None

        if controller is None or frame is None:
            return deny("active_frame_missing")
        if self._master_stop_latched:
            return deny("master_stop")
        if self._pause_state != "off":
            return deny("paused")
        if self._removed or self._unloading:
            return deny("sensor_unloading")
        waiting_pv = bool(
            controller.record.state is ActiveState.WAITING_READBACK
            and controller.record.transaction is not None
            and controller.record.transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD
        )
        if getattr(controller, '_telemetry_hold_until', None) is not None and not waiting_pv:
            return deny('waiting_for_telemetry_within_existing_lease')
        waiting_rce = bool(controller.record.state in {ActiveState.WAITING_READBACK, ActiveState.RETARGETING}
            and controller.record.transaction is not None
            and controller.record.transaction.intent.action is ExecutionAction.RCE_EXPORT
            and controller.record.transaction.readback_result is VerificationStatus.CONFIRMED)
        transaction = controller.record.transaction
        if transaction is None or transaction.transaction_id != lease.transaction_id:
            return deny("transaction_mismatch")
        settling = (
            controller.record.state in {ActiveState.WAITING_READBACK, ActiveState.RETARGETING}
            and transaction.owner is ExecutionOwner.TARIFF
            and transaction.intent.action in {
                ExecutionAction.TARIFF_BATTERY_CHARGE,
                ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
                ExecutionAction.TARIFF_GRID_SUPPORT,
            }
            and transaction.readback_result is VerificationStatus.CONFIRMED
        )
        if controller.record.state is not ActiveState.EXECUTING and not settling and not waiting_pv and not waiting_rce:
            return deny("execution_not_confirmed")
        if transaction.deadline != lease.hard_deadline:
            return deny("hard_deadline_mismatch")
        if transaction.command_sent_at is None:
            return deny("command_not_sent")
        if transaction.intent.command.ems_block is None or (
            _control_lease_block(transaction.intent.command.ems_block) != lease.block
        ):
            return deny("command_block_mismatch")
        now = _aware_utc(dt_util.utcnow())
        if now is None:
            return deny("clock_unavailable")
        if now < transaction.command_sent_at or now >= transaction.deadline:
            return deny("transaction_deadline")
        if settling and now + timedelta(seconds=1) >= controller.lease_policy_deadline(now=now):
            return deny("tariff_settling_budget")
        if waiting_pv:
            fresh = self._fresh_active_frame()
            if fresh is None or not controller.pv_hold_settling_lease_authorized(
                fresh, now=now, lease_ttl_seconds=1):
                return deny("pv_hold_settling_unqualified")
            frame = fresh
        if waiting_rce:
            fresh = self._fresh_active_frame()
            if fresh is None or not controller.rce_settling_lease_authorized(fresh, now=now, lease_ttl_seconds=1):
                return deny('rce_settling_unqualified')
            frame = fresh
        renewal_authorized = authorization_matches(
            transaction.intent,
            frame.decision,
            frame.candidates,
        )
        if transaction.owner is ExecutionOwner.TARIFF and (
            frame.tariff.result_current is not True or frame.tariff.recalculation_pending is not False
        ):
            renewal_authorized = False
        if waiting_pv or waiting_rce:
            renewal_authorized = True  # exact pending candidate and fresh ramp checked above
        if not renewal_authorized and transaction.owner is ExecutionOwner.RCE:
            fresh = self._fresh_active_frame()
            if fresh is not None and controller.rce_replan_lease_authorized(
                fresh, now=now, lease_ttl_seconds=1,
            ):
                frame = fresh
                renewal_authorized = True
        tariff_replanning = False
        if not renewal_authorized and transaction.owner is ExecutionOwner.TARIFF:
            fresh = self._fresh_active_frame()
            if fresh is not None and controller.tariff_replan_lease_authorized(fresh, now=now, lease_ttl_seconds=1):
                frame = fresh
                renewal_authorized = tariff_replanning = True
        if not renewal_authorized:
            return deny("authorization_mismatch")
        try:
            gates = execution_gates(
                frame.execution,
                owner_kind=frame.context.owner_kind,
                owner_conflict=frame.context.owner_conflict,
                now=now,
            )
        except (ActiveBridgeError, TypeError, ValueError):
            return deny("execution_gates_invalid")
        mode = transaction.intent.command.ems_block.mode
        if not gates.inputs_fresh:
            return deny("inputs_stale")
        if not gates.bms_fresh:
            return deny("bms_stale")
        if not gates.full_block_ready or not gates.full_block_topology_ready:
            return deny("fc03_incomplete")
        if gates.owner_conflict or gates.observed_owner not in {None, transaction.owner}:
            return deny("owner_conflict")
        if not waiting_pv and not _control_lease_direction_ready(
            owner=transaction.owner,
            action=transaction.intent.action,
            mode=mode,
            gates=gates,
        ):
            return deny("export_direction")
        if (
            transaction.owner is ExecutionOwner.RCE
            and not waiting_pv
            and not rce_sent_command_within_live_bms_limit(
                transaction.intent,
                frame.execution,
                rce=frame.rce,
                now=now,
            )
        ):
            return deny("rce_bms_limit")
        if (
            transaction.owner is ExecutionOwner.TARIFF
            and (
                (not tariff_replanning and (frame.tariff.result_current is not True
                 or frame.tariff.recalculation_pending is not False))
                or not tariff_command_within_live_bms_limit(
                    transaction.intent,
                    frame.execution,
                    tariff=frame.tariff,
                    now=now,
                )
            )
        ):
            return deny("tariff_plan_or_bms")
        proof = transaction.physical_verification
        if (controller.record.state is ActiveState.EXECUTING
            and transaction.intent.action is ExecutionAction.TARIFF_GRID_SUPPORT):
            # A cached confirmed cohort may predate a battery excursion. The
            # bounded controller wait never grants fresh lease authority, even
            # when the HA source event arrives before controller reconciliation.
            current_proof = physical_verification(
                transaction.transaction_id, transaction.intent.action,
                frame.execution, command_sent_at=transaction.command_sent_at, now=now,
            )
            if current_proof.status is not VerificationStatus.CONFIRMED:
                return deny("tariff_support_flow_unconfirmed")
        if waiting_rce:
            proof = physical_verification(transaction.transaction_id, transaction.intent.action,
                frame.execution, command_sent_at=transaction.command_sent_at, now=now,
                allow_rce_settling=controller.record.state is ActiveState.WAITING_READBACK)
        if settling:
            # Mode 4 has already acknowledged the exact charging command. A
            # coherent, newer house-supply cohort may bridge charging latency
            # only inside the original ACK budget. This proof is local to the
            # lease gate: it never confirms charging or grants accounting.
            if frame.execution.power_cohort_complete is not True:
                return deny("tariff_settling_cohort")
            proof = physical_verification(
                transaction.transaction_id,
                ExecutionAction.TARIFF_GRID_SUPPORT,
                frame.execution,
                command_sent_at=transaction.command_sent_at,
                now=now,
                allow_tariff_settling=(transaction.intent.action is ExecutionAction.TARIFF_GRID_SUPPORT),
            )
        support_ramp = bool(settling and transaction.intent.action is ExecutionAction.TARIFF_GRID_SUPPORT
            and proof is not None and proof.status is VerificationStatus.PENDING
            and "tariff_phase=waiting_for_grid_charge_effect" in proof.evidence)
        generation_at = frame.execution.full_block_generation_at
        generation = frame.execution.full_block_generation
        if (
            (not waiting_pv and proof is None)
            or (not waiting_pv and proof.status is not VerificationStatus.CONFIRMED and not waiting_rce and not support_ramp)
            or generation_at is None
            or generation_at.tzinfo is None
            or generation_at.utcoffset() is None
            or (not waiting_pv and not 0.0 <= (now - proof.observed_at).total_seconds() <= (
                RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS
                if (
                    transaction.owner is ExecutionOwner.RCE
                    and transaction.intent.action is ExecutionAction.RCE_EXPORT
                )
                else MAX_READBACK_AGE_SECONDS
            ))
            or not 0.0 <= (
                now - generation_at.astimezone(timezone.utc)
            ).total_seconds() <= MAX_READBACK_AGE_SECONDS
            or isinstance(generation, bool)
            or not isinstance(generation, (int, float))
            or not math.isfinite(float(generation))
            or abs(float(generation) - round(float(generation))) > 0.01
            or int(round(float(generation))) < 1
            or not _control_lease_blocks_match(
                _control_lease_frame_block(frame),
                lease.block,
            )
        ):
            return deny("physical_or_fc03_proof")
        self._record_control_lease_gate(
            status="eligible",
            reason=("pv_hold_initial_stabilization" if waiting_pv else
                    "tariff_settling_house_supply" if settling else "fresh_authorization"),
            transaction_id=lease.transaction_id,
        )
        self._control_lease_authorization_deadline = controller.lease_policy_deadline(now=now)
        run_end = _aware_utc(
            frame.rce.current_run_end if transaction.owner is ExecutionOwner.RCE
            else frame.tariff.current_grid_charge_run_end if transaction.owner is ExecutionOwner.TARIFF
            else frame.rcm.pre_discharge_deadline
        )
        if run_end is not None:
            self._control_lease_authorization_deadline = min(
                self._control_lease_authorization_deadline, run_end)
        if self._control_lease_authorization_deadline <= now:
            return deny("authorization_deadline")
        return int(round(float(generation))), lease.block

    def _control_lease_identity(self) -> tuple[str, str, str, int] | None:
        lease = self._control_lease_client.handle
        if lease is None:
            return None
        return (
            self._control_lease_client.session_id,
            lease.lease_id,
            lease.transaction_id,
            lease.command_generation,
        )

    async def _async_control_lease_terminal_proof(self) -> Mapping[str, object] | None:
        """Read ESP's durable terminal snapshot; never issue a control write."""

        service = self._esphome_named_action_service(
            "ems_supervisor_control_lease_terminal_proof"
        )
        self._assert_single_transport_instance()
        response = await self.hass.services.async_call(
            _ESPHOME_DOMAIN,
            service,
            {},
            blocking=True,
            context=self._context,
            return_response=True,
        )
        return response if isinstance(response, Mapping) else None

    def _control_lease_valid_until(self) -> datetime | None:
        """Return the conservative ESP soft-lease boundary for hold capping."""

        lease = self._control_lease_client.handle
        if lease is None:
            return None
        now = _aware_utc(dt_util.utcnow())
        if now is None:
            return None
        loop_now = asyncio.get_running_loop().time()
        if loop_now < lease.last_accepted_monotonic:
            return None
        remaining = (
            lease.last_accepted_monotonic
            + lease.granted_seconds
            - loop_now
        )
        return min(lease.hard_deadline, now + timedelta(seconds=max(0.0, remaining)))

    def _sync_control_lease(self) -> None:
        lease = self._control_lease_client.handle
        if lease is None:
            self._cancel_control_lease_callback()
            return
        task = self._control_lease_task
        if task is not None and not task.done():
            # A state publication may call this method while the ESP response
            # is still pending.  The owning task alone schedules its successor.
            self._cancel_control_lease_callback()
            return
        loop = asyncio.get_running_loop()
        loop_now = loop.time()
        wall_now = _aware_utc(dt_util.utcnow())
        if (
            wall_now is None
            or wall_now < lease.last_wall
            or wall_now >= lease.hard_deadline
            or loop_now < lease.last_monotonic
        ):
            self._control_lease_client.invalidate()
            self._cancel_control_lease_callback()
            return
        if self._control_lease_client.expire_if_due(now_monotonic=loop_now):
            record = self._controller.record if self._controller is not None else None
            tx = record.transaction if record is not None else None
            planned_finish = bool(
                record is not None and record.state is ActiveState.STOPPING
                and record.reason is ExecutionReason.DEADLINE_REACHED
                and tx is not None and tx.transaction_id == lease.transaction_id
                and tx.owner is ExecutionOwner.TARIFF
                and wall_now >= self._controller._tariff_planned_boundary(tx)
            )
            if not planned_finish:
                self._control_lease_error = "control_lease_lost:local_soft_deadline"
            self._cancel_control_lease_callback()
            self._record_control_lease_gate(
                status="ended" if planned_finish else "denied",
                reason="planned_tariff_end" if planned_finish else "local_soft_deadline",
                transaction_id=lease.transaction_id,
            )
            return
        evidence = self._control_lease_renewal_evidence()
        delay = (
            max(0.0, lease.next_renew_monotonic - loop_now)
            if evidence is not None
            else CONTROL_LEASE_RENEW_SECONDS
        )
        scheduled_identity = (
            lease.lease_id,
            lease.transaction_id,
            lease.command_generation,
        )
        if self._control_lease_cancel is not None:
            if evidence is None or delay > 0.0:
                return
            # Authorization can arrive near the end of the ESP TTL while a
            # twenty-second unauthorised recheck is already pending. Replace it
            # with the single renewal which is now due.
            self._cancel_control_lease_callback()

        @callback
        def renew_callback(_now: datetime) -> None:
            self._control_lease_cancel = None
            if self._removed or self._unloading:
                return
            current_lease = self._control_lease_client.handle
            if (
                current_lease is None
                or (
                    current_lease.lease_id,
                    current_lease.transaction_id,
                    current_lease.command_generation,
                ) != scheduled_identity
            ):
                self._sync_control_lease()
                return
            current = self._control_lease_task
            if current is not None and not current.done():
                return
            task = self.hass.async_create_task(
                self._async_renew_control_lease(),
                "hoymiles EMS control lease renewal",
            )
            self._control_lease_task = task
            task.add_done_callback(self._control_lease_renewal_done)

        self._control_lease_cancel = async_call_later(
            self.hass,
            delay,
            renew_callback,
        )

    @callback
    def _control_lease_renewal_done(self, task: asyncio.Task[None]) -> None:
        """Let only the exact completed renewal task release its slot."""

        if self._control_lease_task is not task:
            return
        self._control_lease_task = None
        if not self._removed and not self._unloading:
            self._sync_control_lease()

    def _record_control_lease_result(
        self,
        *,
        status: str,
        reason: str,
        request: Mapping[str, Any],
        requested_at: datetime,
        lost: bool | None = None,
        first_send_monotonic: float | None = None,
        observed_monotonic: float | None = None,
    ) -> None:
        """Publish correlated renewal evidence without protocol secrets."""

        if lost is not None:
            self._control_lease_error = (
                f"control_lease_lost:{reason}" if lost else None
            )
        result = {
            "status": status,
            "reason": reason,
            "transaction_id": request["transaction_id"],
            "command_generation": request["command_generation"],
            "sequence": request["sequence"],
            "snapshot_generation": request["snapshot_generation"],
            "requested_at": requested_at.isoformat(),
        }
        if (
            first_send_monotonic is not None
            and observed_monotonic is not None
            and math.isfinite(first_send_monotonic)
            and math.isfinite(observed_monotonic)
            and observed_monotonic >= first_send_monotonic
        ):
            elapsed = observed_monotonic - first_send_monotonic
            handle = self._control_lease_client.handle
            remaining = 0.0
            if (
                handle is not None
                and request.get("lease_id") == handle.lease_id
                and request.get("transaction_id") == handle.transaction_id
                and request.get("command_generation") == handle.command_generation
            ):
                remaining = max(
                    0.0,
                    min(
                        handle.last_accepted_monotonic + handle.granted_seconds
                        - observed_monotonic,
                        (handle.hard_deadline - requested_at).total_seconds() - elapsed,
                    ),
                )
            result.update(
                {
                    "first_send_monotonic": round(first_send_monotonic, 6),
                    "response_received_monotonic": round(observed_monotonic, 6),
                    "response_latency_seconds": round(elapsed, 6),
                    "soft_lease_remaining_seconds": round(
                        remaining,
                        6,
                    ),
                }
            )
            _LOGGER.debug(
                "ESP control lease renewal timing status=%s reason=%s "
                "transaction_id=%s command_generation=%s sequence=%s "
                "first_send_monotonic=%.6f response_received_monotonic=%.6f "
                "response_latency_s=%.6f soft_remaining_s=%.6f",
                status,
                reason,
                request["transaction_id"],
                request["command_generation"],
                request["sequence"],
                first_send_monotonic,
                observed_monotonic,
                elapsed,
                remaining,
            )
        self._control_lease_last_result = result
        if (
            status == ControlLeaseRenewStatus.ACCEPTED.value
            and self._control_lease_gate is not None
            and self._control_lease_gate["transaction_id"] == request["transaction_id"]
        ):
            self._control_lease_gate["last_success_at"] = requested_at.isoformat()
        self._publish_composite_state()

    async def _async_renew_control_lease(self) -> None:
        """Renew once; every absent/malformed/rejected response fails closed."""

        request: dict[str, Any] | None = None
        requested_at: datetime | None = None
        first_send_monotonic: float | None = None
        response_received_monotonic: float | None = None
        loop: asyncio.AbstractEventLoop | None = None
        try:
            evidence = self._control_lease_renewal_evidence()
            if evidence is None:
                return
            generation, block = evidence
            loop = asyncio.get_running_loop()
            now = _aware_utc(dt_util.utcnow())
            if now is None:
                self._control_lease_client.invalidate()
                return
            requested_at = now
            request = self._control_lease_client.prepare_renew(
                authorized=True,
                snapshot_generation=generation,
                now_wall=now,
                now_monotonic=loop.time(),
                authorization_deadline=self._control_lease_authorization_deadline,
            )
            if request is None:
                return
            handle = self._control_lease_client.handle
            if (
                handle is not None
                and handle.pending_sequence == request["sequence"]
            ):
                first_send_monotonic = handle.pending_first_send_monotonic
            request.update(
                {
                    "mode_code": int(round(block[0])),
                    "self_use_soc": block[1],
                    "backup_soc": block[2],
                    "force_charge_soc": block[3],
                    "maximum_charge_power": block[4],
                    "force_discharge_soc": block[5],
                    "maximum_discharge_power": block[6],
                }
            )
            service = self._esphome_named_action_service(
                "ems_supervisor_renew_control_lease"
            )
            self._assert_single_transport_instance()
            retry_used = False
            while True:
                response = await self.hass.services.async_call(
                    _ESPHOME_DOMAIN,
                    service,
                    request,
                    blocking=True,
                    context=self._context,
                    return_response=True,
                )
                response_received_monotonic = loop.time()
                result = self._control_lease_client.process_renew_response(
                    response,
                    request=request,
                    now_monotonic=response_received_monotonic,
                )
                if (
                    result.status
                    is not ControlLeaseRenewStatus.RETRYABLE_STALE_SNAPSHOT
                ):
                    break
                fresh = self._control_lease_renewal_evidence()
                if retry_used or fresh is None:
                    self._control_lease_client.invalidate()
                    result = type(result)(
                        ControlLeaseRenewStatus.REJECTED,
                        result.reason,
                    )
                    break
                fresh_generation, fresh_block = fresh
                if (
                    fresh_generation <= request["snapshot_generation"]
                    or not _control_lease_blocks_match(fresh_block, block)
                ):
                    self._control_lease_client.invalidate()
                    result = type(result)(
                        ControlLeaseRenewStatus.REJECTED,
                        result.reason,
                    )
                    break
                # Same identity, nonce and sequence; only the newly verified
                # complete FC03 generation is updated. No deadline moves.
                request["snapshot_generation"] = fresh_generation
                retry_used = True
                _LOGGER.debug(
                    "Retrying ESP control lease renewal once after a newer "
                    "identical FC03 snapshot: transaction_id=%s "
                    "command_generation=%s sequence=%s snapshot_generation=%s",
                    request["transaction_id"],
                    request["command_generation"],
                    request["sequence"],
                    fresh_generation,
                )
            metadata = (
                request["transaction_id"],
                request["command_generation"],
                request["sequence"],
                request["snapshot_generation"],
                now.isoformat(),
            )
            if result.status is ControlLeaseRenewStatus.IGNORED_STALE_RESPONSE:
                self._record_control_lease_result(
                    status=result.status.value,
                    reason=result.reason,
                    request=request,
                    requested_at=now,
                    first_send_monotonic=first_send_monotonic,
                    observed_monotonic=response_received_monotonic,
                )
                _LOGGER.debug(
                    "Ignored stale ESP control lease renewal response: "
                    "reason=%s transaction_id=%s command_generation=%s "
                    "sequence=%s snapshot_generation=%s requested_at=%s",
                    result.reason,
                    *metadata,
                )
                return
            if result.status is ControlLeaseRenewStatus.REJECTED:
                self._record_control_lease_result(
                    status=result.status.value,
                    reason=result.reason,
                    request=request,
                    requested_at=now,
                    lost=True,
                    first_send_monotonic=first_send_monotonic,
                    observed_monotonic=response_received_monotonic,
                )
                _LOGGER.error(
                    "ESP control lease renewal was rejected: reason=%s "
                    "transaction_id=%s command_generation=%s sequence=%s "
                    "snapshot_generation=%s requested_at=%s",
                    result.reason,
                    *metadata,
                )
                return
            self._record_control_lease_result(
                status=result.status.value,
                reason=result.reason,
                request=request,
                requested_at=now,
                lost=False,
                first_send_monotonic=first_send_monotonic,
                observed_monotonic=response_received_monotonic,
            )
        except asyncio.CancelledError:
            raise
        except ValueError:
            if request is not None and requested_at is not None:
                self._record_control_lease_result(
                    status="malformed_response",
                    reason="protocol_response_invalid",
                    request=request,
                    requested_at=requested_at,
                    lost=True,
                    first_send_monotonic=first_send_monotonic,
                    observed_monotonic=response_received_monotonic,
                )
            _LOGGER.error(
                "ESP control lease renewal response was malformed",
                exc_info=True,
            )
        except Exception:  # noqa: BLE001 - local ESP expiry is authoritative
            observed_monotonic = (
                loop.time() if loop is not None else None
            )
            if observed_monotonic is not None:
                self._control_lease_client.defer_renew_retry(
                    now_monotonic=observed_monotonic
                )
            if request is not None and requested_at is not None:
                self._record_control_lease_result(
                    status="unavailable",
                    reason="transport_response_unavailable",
                    request=request,
                    requested_at=requested_at,
                    first_send_monotonic=first_send_monotonic,
                    observed_monotonic=observed_monotonic,
                )
            _LOGGER.warning(
                "ESP control lease renewal response was unavailable; retrying the "
                "same sequence within the local ESP TTL",
                exc_info=True,
            )

    def _sync_execution_watchdog(
        self,
        controller: SupervisorActiveController,
    ) -> None:
        """Schedule one nonrenewable frame at the executor's earliest boundary."""

        evidence = controller.execution_watchdog
        if evidence is None or self._removed or self._unloading:
            self._cancel_execution_watchdog()
            return
        transaction_id, boundary = evidence
        key = (transaction_id, boundary)
        if (
            self._execution_watchdog_key == key
            and self._execution_watchdog_cancel is not None
        ):
            return
        generation = self._execution_watchdog_generation + 1

        @callback
        def execution_watchdog_callback(_now: datetime) -> None:
            if (
                self._removed
                or generation != self._execution_watchdog_generation
                or self._execution_watchdog_key != key
            ):
                return
            self._execution_watchdog_cancel = None
            self._execution_watchdog_key = None
            if _loaded_entry_count(self.hass) != 1:
                self._publish_unavailable()
                return
            self._recompute()

        new_cancel = async_track_point_in_utc_time(
            self.hass,
            execution_watchdog_callback,
            boundary + _MICROSECOND,
        )
        old_cancel = self._execution_watchdog_cancel
        self._execution_watchdog_generation = generation
        self._execution_watchdog_key = key
        self._execution_watchdog_cancel = new_cancel
        if old_cancel is not None:
            old_cancel()

    def _cancel_settling_replan(self) -> None:
        cancel = self._settling_replan_cancel
        self._settling_replan_cancel = None
        self._settling_replan_key = None
        if cancel is not None:
            cancel()

    def _sync_settling_replan(
        self, controller: SupervisorActiveController,
    ) -> None:
        """Arm one +150 s recalculation; the controller owns the +180 s limit."""
        evidence = controller.post_command_settling_replan
        if (
            evidence is None
            or self._removed
            or self._unloading
            or self._master_stop_latched
            or self._settling_replan_request is None
        ):
            self._cancel_settling_replan()
            return
        key, boundary = evidence
        if self._settling_replan_attempted == key:
            self._cancel_settling_replan()
            return
        if self._settling_replan_key == key and self._settling_replan_cancel is not None:
            return
        self._cancel_settling_replan()

        @callback
        def settling_replan_callback(_now: datetime) -> None:
            if (
                self._removed
                or self._unloading
                or self._master_stop_latched
                or self._settling_replan_key != key
                or self._settling_replan_attempted == key
                or self._controller is not controller
                or controller.post_command_settling_replan != evidence
            ):
                return
            # Persist deduplication in this lifecycle before starting work;
            # a fresh FC03 or another pending publication cannot re-arm it.
            self._settling_replan_attempted = key
            self._settling_replan_cancel = None
            self._settling_replan_key = None
            self._settling_replan_task = self._entry.async_create_background_task(
                self.hass,
                self._async_run_settling_replan(controller, key),
                "Hoymiles RCE post-command settling recalculation",
            )

        self._settling_replan_key = key
        self._settling_replan_cancel = async_track_point_in_utc_time(
            self.hass, settling_replan_callback,
            max(boundary, dt_util.utcnow()) + _MICROSECOND,
        )

    async def _async_run_settling_replan(
        self,
        controller: SupervisorActiveController,
        key: tuple[str, datetime],
    ) -> None:
        """Recheck live authority before requesting the existing full solver."""
        task = asyncio.current_task()
        try:
            if self._removed or self._unloading or self._master_stop_latched:
                return
            # The ordinary frame path checks OFF, owner, BMS, SOC and physical
            # freshness. Never run the solver inside the controller's lock.
            previous_frame = self._latest_active_frame
            self._recompute(raise_on_error=True)
            if self._latest_active_frame is previous_frame:
                return
            controller_task = self._controller_task
            if controller_task is not None:
                await asyncio.shield(controller_task)
            evidence = controller.post_command_settling_replan
            deadline = controller.post_command_settling_deadline
            request = self._settling_replan_request
            if (
                self._removed
                or self._unloading
                or self._master_stop_latched
                or not self._guard_ready
                or not self._available
                or _loaded_entry_count(self.hass) != 1
                or self._execution_adapter_error is not None
                or self._controller is not controller
                or evidence is None
                or evidence[0] != key
                or deadline is None
                or dt_util.utcnow() >= deadline
                or request is None
            ):
                return
            await request()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Failure does not move the controller's fixed expiry or create
            # another retry loop. Its independent watchdog ends the hold.
            _LOGGER.exception("RCE post-command settling recalculation failed")
        finally:
            if self._settling_replan_task is task:
                self._settling_replan_task = None

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
        paused = _state_text(self.hass.states.get(EMS_PAUSED_ENTITY_ID))
        self._pause_state = paused
        if paused != "off":
            return SupervisorMode.OFF
        if state is None:
            return SupervisorMode.OFF
        value = _state_text(state)
        if value == "Off":
            return SupervisorMode.OFF
        if value in {"Active", "active"}:
            return SupervisorMode.ACTIVE
        if value in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
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
        live_power = {
            key: states.get(key)
            for key in _POWER_FLOW_COHORT_KEYS
        }
        # GRID direction is a one-way safety signal. Keep its newest native
        # report separate from the accepted four-channel balance cohort: it
        # may revoke tariff authority, but can never grant or refresh it.
        states["critical_grid_power"] = live_power.get("grid_power")
        if not self._power_cohort_states and all(
            isinstance(value, State) for value in live_power.values()
        ):
            self._power_cohort_states = {
                key: value for key, value in live_power.items()
                if isinstance(value, State)
            }
            self._power_cohort_generation = 1
        transaction = (
            self._controller.record.transaction
            if self._controller is not None else None
        )
        if (
            self._power_cohort_states
            and transaction is not None
            and transaction.owner is ExecutionOwner.TARIFF
        ):
            states.update(self._power_cohort_states)
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
        self_use_soc = _percent_state(states.get("self_use_soc_readback"))
        backup_soc = _percent_state(states.get("backup_soc_readback"))
        ems_block_values = (
            ems_values[0],
            self_use_soc,
            backup_soc,
            ems_values[4],
            ems_values[3],
            ems_values[2],
            ems_values[1],
        )
        ems_states = (
            states.get("ems_mode_readback"),
            states.get("ems_generation"),
            states.get("self_use_soc_readback"),
            states.get("backup_soc_readback"),
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
            if all(value is not None for value in ems_block_values)
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

        battery_charge_generation = _state_number(
            states.get("battery_charge_generation"),
            minimum=1.0,
            maximum=_GENERATION_MAX,
        )
        battery_charge_generation_at = (
            _cohort_generation_time(
                (
                    states.get("charge_power_readback"),
                    states.get("battery_charge_generation"),
                ),
                states.get("battery_charge_generation"),
                battery_charge_generation,
                now,
            )
            if _percent_state(states.get("charge_power_readback")) is not None
            else None
        )

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
            full_block_generation=(ems_generation if ems_coherent else None),
            full_block_generation_at=ems_generation_at,
            self_use_soc_percent=(self_use_soc if ems_coherent else None),
            backup_soc_percent=(backup_soc if ems_coherent else None),
            force_charge_soc_percent=(ems_values[4] if ems_coherent else None),
            maximum_charge_power_percent=(ems_values[3] if ems_coherent else None),
            force_discharge_soc_percent=(ems_values[2] if ems_coherent else None),
            maximum_discharge_power_percent=(ems_values[1] if ems_coherent else None),
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
            gcf_generation=(gcf_generation if gcf_coherent else None),
            gcf_generation_at=gcf_generation_at,
            gcf_cohort_coherent=gcf_coherent,
            battery_charge_limit_generation=(
                battery_charge_generation
                if battery_charge_generation_at is not None
                else None
            ),
            battery_charge_limit_generation_at=battery_charge_generation_at,
            battery_charge_limit_percent=(
                _percent_state(states.get("charge_power_readback"))
                if battery_charge_generation_at is not None
                else None
            ),
            # No entry-local physical Grid-to-Battery provider exists in this
            # release.  The legacy global entity id remains in the frozen
            # source map only for compatibility, but it must never acquire
            # execution, accounting or feedback authority.
            grid_to_battery_power_w=None,
            grid_to_battery_power_observed_at=None,
            grid_power_w=_state_number(states.get("grid_power")),
            grid_power_observed_at=_reported(states.get("grid_power"), now),
            critical_grid_power_w=_state_number(
                states.get("critical_grid_power")
            ),
            critical_grid_power_observed_at=_reported(
                states.get("critical_grid_power"),
                now,
            ),
            bms_battery_power_w=_state_number(states.get("bms_battery_power")),
            bms_battery_power_observed_at=_reported(states.get("bms_battery_power"), now),
            battery_power_w=_state_number(states.get("battery_power")),
            battery_power_observed_at=_reported(states.get("battery_power"), now),
            pv_power_w=_state_number(states.get("pv_power")),
            pv_power_observed_at=_reported(states.get("pv_power"), now),
            load_power_w=_state_number(states.get("load_power")),
            load_power_observed_at=_reported(states.get("load_power"), now),
            power_cohort_complete=(
                _POWER_FLOW_COHORT_KEYS.issubset(self._power_cohort_states)
            ),
            power_cohort_generation=(
                self._power_cohort_generation
                if self._power_cohort_states
                else None
            ),
            hardware_readback_supported=(
                (_state_number(states.get("hardware_readback_supported")) or 0.0)
                > 0.5
            ),
        )

        rce_attrs = self._attrs(states, "rce_plan")
        tariff_attrs = self._attrs(states, "tariff_plan")
        from .dynamic_price_profile import paired_plan_current
        providers = tuple(self.hass.states.get(entity) for entity in (
            "input_select.hoymiles_dynamic_sale_provider", "input_select.hoymiles_tariff_operator"))
        pstryk_selected = any(state is not None and state.state == "Pstryk" for state in providers)
        pstryk_bound = all(state is not None and state.state == "Pstryk" for state in providers)
        if not paired_plan_current(rce_attrs, tariff_attrs, pstryk_selected=pstryk_selected, pstryk_bound=pstryk_bound):
            rce_attrs = {**rce_attrs, "result_current": False, "recalculation_pending": True}
            tariff_attrs = {**tariff_attrs, "result_current": False, "recalculation_pending": True}
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
            system_power_kw=_plan_number(rce_attrs.get("system_power_kw"), minimum=0.0),
            current_soc_percent=execution.battery_soc_percent,
            control_data_ready=_tri_state(states.get("rce_control_data_ready")),
            price_above_threshold=_tri_state(states.get("rce_price_above_threshold")),
            reserve_ready=_tri_state(states.get("rce_reserve_ready")),
            sale_block_active=_tri_state(states.get("sale_block_active")),
            latched_slot_end=_input_datetime(states.get("rce_latched_slot_end")),
            latched_minimum_soc_percent=_percent_state(states.get("rce_latched_minimum_soc")),
            active_4305_readback_percent=(ems_values[2] if ems_coherent else None),
            active_4306_readback_percent=(ems_values[1] if ems_coherent else None),
            current_slot_suppression_reason=(
                rce_attrs.get("current_slot_suppression_reason")
                if type(rce_attrs.get("current_slot_suppression_reason")) is str
                else None
            ),
            current_slot_load_only_export_suppressed=_exact_bool(
                rce_attrs.get("current_slot_load_only_export_suppressed")
            ),
            current_slot_load_exhausts_requested_discharge_budget=_exact_bool(
                rce_attrs.get("current_slot_load_exhausts_requested_discharge_budget")
            ),
            post_command_settling_market_fingerprint=(
                self._settling_market_source()
                if self._settling_market_source is not None
                else None
            ),
            requested_discharge_power_percent=_percent_state(
                states.get("rce_requested_discharge_power")
            ),
        )

        if (_flag(states.get("pv_charge_delay_enabled"))
                and rce_attrs.get("pv_charge_delay_execution_ready") is True):
            qualified = all(rce_attrs.get(key) is True for key in (
                "rce_today_data_fresh", "forecast_today_data_fresh", "soc_data_fresh", "gcf_execution_data_fresh"))
            # Pending can retain physics, but cannot bridge a profile/price or
            # user-setting change. A fresh result must authorize that change.
            if (pstryk_selected != (rce_attrs.get('price_provider') == 'Pstryk')
                or pstryk_selected and not pstryk_bound
                or rce_attrs.get('pstryk_blocker') in {
                    'prices_changed', 'profile_changing', 'profile_not_ready', 'settings_changed',
                    'helper_missing', 'helper_invalid', 'permission_missing', 'unloaded'}):
                qualified = False
            rce = replace(rce, pv_charge_hold=True, pv_charge_hold_qualified=qualified,
                current_slot_planned=True, current_run_end=_iso_datetime(rce_attrs.get("pv_charge_delay_end")),
                effective_discharge_power_percent=1.)

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
            current_run_suppression_reason=(
                tariff_attrs.get("current_run_suppression_reason")
                if type(tariff_attrs.get("current_run_suppression_reason")) is str else None
            ),
            current_run_continue_reason=(
                tariff_attrs.get("current_run_continue_reason")
                if type(tariff_attrs.get("current_run_continue_reason")) is str else None
            ),
            live_power_input_evidence=_tariff_live_power_input_evidence(tariff_attrs),
            requested_charge_power_kw=_plan_number(tariff_attrs.get("requested_charge_power_kw"), minimum=0.0),
            system_power_kw=_plan_number(tariff_attrs.get("system_power_kw"), minimum=0.0),
            command_charge_power_percent=_percent_attr(tariff_attrs.get("command_charge_power_percent")),
            current_run_grid_import_kwh=_plan_number(tariff_attrs.get("current_run_grid_import_kwh"), minimum=0.0),
            requested_target_energy_kwh=_plan_number(tariff_attrs.get("requested_target_energy_kwh"), minimum=0.0),
            demand_margin_requested_kwh=_plan_number(tariff_attrs.get("demand_margin_requested_kwh"), minimum=0.0),
            demand_margin_unserved_kwh=_plan_number(tariff_attrs.get("demand_margin_unserved_kwh"), minimum=0.0),
            base_energy_shortfall_kwh=_plan_number(tariff_attrs.get("base_energy_shortfall_kwh"), minimum=0.0),
            current_run_benefit_pln=_plan_number(tariff_attrs.get("current_run_benefit_pln")),
            target_soc_percent=_percent_attr(tariff_attrs.get("target_soc_percent")),
            current_soc_percent=execution.battery_soc_percent,
            current_soc_observed_at=execution.battery_soc_observed_at,
            maximum_soc_percent=_percent_state(states.get("tariff_maximum_soc")),
            base_reserve_soc_percent=_percent_attr(tariff_attrs.get("base_reserve_soc_percent")),
            current_slot_end=_iso_datetime(tariff_attrs.get("current_slot_end")),
            current_grid_charge_run_end=_iso_datetime(tariff_attrs.get("current_grid_charge_run_end")),
            control_inputs_fresh=_exact_bool(tariff_attrs.get("control_inputs_fresh")),
            forecast_data_fresh=_exact_bool(tariff_attrs.get("forecast_data_fresh")),
            control_input_block_reason=(
                tariff_attrs.get("control_input_block_reason")
                if type(tariff_attrs.get("control_input_block_reason")) is str else None
            ),
            load_profile_source=(
                tariff_attrs.get("load_profile_source")
                if type(tariff_attrs.get("load_profile_source")) is str else None
            ),
            configured_daily_fallback_kwh=_plan_number(tariff_attrs.get("configured_daily_fallback_kwh"), minimum=0.0),
            input_change_reason=(
                tariff_attrs.get("input_change_reason")
                if type(tariff_attrs.get("input_change_reason")) is str else None
            ),
            input_change_previous_value=(
                tariff_attrs.get("input_change_previous_value")
                if type(tariff_attrs.get("input_change_previous_value")) is str else None
            ),
            input_change_new_value=(
                tariff_attrs.get("input_change_new_value")
                if type(tariff_attrs.get("input_change_new_value")) is str else None
            ),
            bms_charge_power_limit_kw=_plan_number(tariff_attrs.get("bms_charge_power_limit_kw"), minimum=0.0),
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
            enabled=_flag(states.get("rcm_enabled")),
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

    def _apply_executor_commitment(
        self,
        rce: RceSourceSnapshot,
        tariff: TariffSourceSnapshot,
        rcm: RcmSourceSnapshot,
        context,
        execution: ExecutionSourceSnapshot,
        *,
        now: datetime,
    ):
        """Expose the persisted owner/commitment to arbitration, not helpers."""

        controller = self._controller
        if controller is None:
            return rce, tariff, rcm, context
        record = controller.record
        transaction = record.transaction
        owner_kind = {
            ExecutionOwner.RCE: OwnerKind.RCE,
            ExecutionOwner.TARIFF: OwnerKind.TARIFF,
            ExecutionOwner.RCM: OwnerKind.RCM,
            ExecutionOwner.BALANCING: OwnerKind.BALANCING,
            ExecutionOwner.MANUAL: OwnerKind.MANUAL,
        }.get(record.owner)
        if transaction is None or owner_kind is None:
            return rce, tariff, rcm, context
        pending = record.state in {
            ActiveState.STARTING,
            ActiveState.WAITING_READBACK,
            ActiveState.RETARGETING,
            ActiveState.STOPPING,
            ActiveState.RESTORING,
            ActiveState.FAULT,
        }
        context = replace(
            context,
            owner_kind=owner_kind,
            transaction_pending=pending,
            transaction_owner_kind=(
                owner_kind if pending else OwnerKind.NONE
            ),
        )
        if record.state not in {ActiveState.EXECUTING, ActiveState.RETARGETING}:
            if not (
                record.state is ActiveState.WAITING_READBACK
                and (transaction.owner is ExecutionOwner.TARIFF
                     or transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD)
            ):
                return rce, tariff, rcm, context

        command = transaction.intent.command
        block = command.ems_block
        action = transaction.intent.action
        if action is ExecutionAction.PV_CHARGE_HOLD and block is not None:
            # Keep the original target/deadline. Loss of plan/permission stops;
            # no SOC retarget heartbeat, no extension and no export fallback.
            end = rce.current_run_end
            rce = replace(rce, pv_charge_hold=True,
                pv_charge_hold_target_from_transaction=True,
                active_latched=record.state is ActiveState.EXECUTING,
                current_run_end=min(end, transaction.deadline) if end is not None else None,
                latched_slot_end=transaction.deadline,
                latched_minimum_soc_percent=block.force_discharge_soc_percent_4305,
                active_4305_readback_percent=execution.force_discharge_soc_percent,
                active_4306_readback_percent=execution.maximum_discharge_power_percent)
        elif action is ExecutionAction.RCE_EXPORT and block is not None:
            desired_floor = block.force_discharge_soc_percent_4305
            desired_power = block.maximum_discharge_power_percent_4306
            observed_at = _aware_utc(rce.observed_at)
            current_run_end = _aware_utc(rce.current_run_end)
            deadline = _aware_utc(transaction.deadline)
            run_covers_deadline = (
                deadline is not None
                and current_run_end is not None
                and current_run_end >= deadline
            )
            plan_age = (
                math.inf
                if observed_at is None
                else (now - observed_at).total_seconds()
            )
            # A fully committed, fresh plan continuing the existing run is
            # the desired target.  Keep physical readback separate below so
            # runtime may recognize the sole old-target incoherence and issue
            # a direct same-owner retarget.  Never renew the transaction lease.
            if (
                current_run_end is not None
                and now < current_run_end
                and deadline is not None
                and now < deadline
                and observed_at is not None
                and -5.0 <= plan_age <= 300.0
                and rce.allowed_by_user is True
                and rce.enabled is True
                and rce.status_code is RcePlanStatus.READY
                and rce.result_current is True
                and rce.recalculation_pending is False
                and rce.current_slot_planned is True
                and rce.current_slot_continue_eligible is True
                and rce.sale_block_active is False
                and rce.protected_soc_floor_percent is not None
                and rce.effective_discharge_power_percent is not None
                and rce.effective_discharge_power_percent > 0.0
            ):
                # Do not lower 4305 inside an already confirmed Mode 5 run.
                # A lower planner floor only expands discharge authority and
                # can safely wait for the next natural transaction.  Keeping
                # the physically confirmed higher floor avoids a redundant
                # full-block write (and, on a parallel plant, a stop/restart
                # fan-out) while an increased safety floor still retargets
                # immediately.
                desired_floor = max(
                    block.force_discharge_soc_percent_4305,
                    rce.protected_soc_floor_percent,
                )
                desired_power = rce.effective_discharge_power_percent
            rce = replace(
                rce,
                # This execution-only projection clips a covering raw plan
                # to the existing lease. The optimizer's published run end
                # remains unchanged; a shorter/unknown run is not normalized.
                current_run_end=(
                    transaction.deadline
                    if run_covers_deadline
                    else rce.current_run_end
                ),
                active_latched=True,
                latched_slot_end=transaction.deadline,
                latched_minimum_soc_percent=desired_floor,
                effective_discharge_power_percent=desired_power,
                active_4305_readback_percent=execution.force_discharge_soc_percent,
                active_4306_readback_percent=(
                    execution.maximum_discharge_power_percent
                ),
            )
        elif action in {
            ExecutionAction.TARIFF_BATTERY_CHARGE,
            ExecutionAction.TARIFF_GRID_SUPPORT,
            ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        } and block is not None:
            active_action = {
                ExecutionAction.TARIFF_BATTERY_CHARGE: TariffAction.BATTERY_CHARGE,
                ExecutionAction.TARIFF_GRID_SUPPORT: TariffAction.GRID_SUPPORT,
                ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE: (
                    TariffAction.GRID_SUPPORT_AND_CHARGE
                ),
            }[action]
            tariff = replace(
                tariff,
                active_latched=True,
                active_action=active_action,
                latched_slot_end=transaction.deadline,
                latched_target_soc_percent=block.force_charge_soc_percent_4303,
                active_4303_readback_percent=execution.force_charge_soc_percent,
                active_4304_readback_percent=(
                    execution.maximum_charge_power_percent
                ),
            )
        elif action is ExecutionAction.RCM_ABSORB_PV:
            active_charge_limit = getattr(
                command,
                "battery_max_charge_power_percent_306",
                None,
            )
            rcm = replace(
                rcm,
                absorb_active=True,
                recommended_charge_limit_percent=(
                    active_charge_limit
                    if rcm_charge_command_within_live_bms_limit(
                        transaction.intent,
                        execution,
                        rcm=rcm,
                        now=now,
                    )
                    else rcm.recommended_charge_limit_percent
                ),
            )
        elif action is ExecutionAction.RCM_LIMIT_EXPORT:
            rcm = replace(
                rcm,
                export_active=True,
                recommended_export_limit_percent=getattr(
                    command,
                    "export_limit_percent_259",
                    None,
                ),
            )
        elif action is ExecutionAction.RCM_ABSORB_AND_LIMIT:
            active_charge_limit = getattr(
                command,
                "battery_max_charge_power_percent_306",
                None,
            )
            rcm = replace(
                rcm,
                absorb_active=True,
                export_active=True,
                recommended_charge_limit_percent=(
                    active_charge_limit
                    if rcm_charge_command_within_live_bms_limit(
                        transaction.intent,
                        execution,
                        rcm=rcm,
                        now=now,
                    )
                    else rcm.recommended_charge_limit_percent
                ),
                recommended_export_limit_percent=getattr(
                    command,
                    "export_limit_percent_259",
                    None,
                ),
            )
        elif action is ExecutionAction.RCM_PRE_DISCHARGE and block is not None:
            active_power_still_authorized = (
                rcm_pre_discharge_command_within_live_bms_limit(
                    transaction.intent,
                    execution,
                    rcm=rcm,
                    now=now,
                )
            )
            rcm = replace(
                rcm,
                pre_discharge_active=True,
                latched_pre_discharge_deadline=transaction.deadline,
                latched_pre_discharge_target_soc_percent=(
                    block.force_discharge_soc_percent_4305
                ),
                latched_pre_discharge_power_percent=(
                    block.maximum_discharge_power_percent_4306
                    if active_power_still_authorized
                    else rcm.pre_discharge_power_percent
                ),
                latched_pre_discharge_power_kw=(
                    (
                        rcm.latched_pre_discharge_power_kw
                        if rcm.latched_pre_discharge_power_kw is not None
                        else rcm.pre_discharge_power_kw
                    )
                    if active_power_still_authorized
                    else rcm.pre_discharge_power_kw
                ),
            )
        return rce, tariff, rcm, context

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
        if rce.pv_charge_hold:
            freshness(execution.bms_battery_power_observed_at, 15.0)
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
            consumed_deadlines += (rce.latched_slot_end, rce.current_run_end)
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
        *,
        now: datetime | None = None,
    ) -> None:
        del serialized
        self._decision_state = state
        self._decision_attributes = attributes
        self._publish_composite_state(now=now)

    def _publish_unavailable(self) -> None:
        changed = self._available
        self._available = False
        self._native_value = None
        self._attributes = {}
        self._serialized_summary = None
        self._decision_state = None
        self._decision_attributes = {}
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
            self._cancel_cohort_callback()
            self._cancel_power_cohort_callback(clear_pending=True)
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
            observed_owner = context.owner_kind
            observed_owner_conflict = context.owner_conflict
            rcm = replace(
                rcm,
                export_state=context.export_state,
                direct_register_topology_allowed=(
                    context.topology_direct_register_allowed
                ),
                full_block_topology_allowed=context.topology_full_block_allowed,
            )
            rce, tariff, rcm, context = self._apply_executor_commitment(
                rce,
                tariff,
                rcm,
                context,
                execution,
                now=now,
            )
            controller = self._controller
            transaction = (
                controller.record.transaction if controller is not None else None
            )
            if (
                transaction is not None
                and transaction.command_sent_at is not None
                and transaction.intent.command.ems_block is not None
                and transaction.physical_verification is not None
                and transaction.physical_verification.status
                is VerificationStatus.CONFIRMED
            ):
                observed_owner = correlated_observed_owner(
                    observed_owner,
                    owner_conflict=observed_owner_conflict,
                    transaction_id=transaction.transaction_id,
                    transaction_owner=transaction.owner,
                    action=transaction.intent.action,
                    expected_block=transaction.intent.command.ems_block,
                    command_sent_at=transaction.command_sent_at,
                    source=execution,
                    now=now,
                )
            stage = "candidate_builders"
            candidates = (
                apply_minimum_grid_power(
                    build_rce_candidate(rce, now=now),
                    self._attrs(states, "rce_plan").get("current_slot_execution_export_power_kw"),
                ),
                apply_minimum_tariff_power(
                    build_tariff_candidate(tariff, now=now),
                    self._attrs(states, "tariff_plan"),
                ),
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
            attributes["observed_owner"] = observed_owner.value
            attributes["transaction_owner"] = OwnerKind.NONE.value
            attributes["owner"] = OwnerKind.NONE.value
            attributes["owner_conflict"] = observed_owner_conflict
            attributes["execution_physical_mode_fresh"] = context.physical_mode_fresh
            attributes["tariff_decision"] = {
                "schema_version": 1,
                "source_frame": {
                    "observed_at": tariff.observed_at.isoformat() if tariff.observed_at else None,
                    "input_revision": tariff.input_revision,
                    "power_cohort_generation": execution.power_cohort_generation,
                },
                "result_current": tariff.result_current,
                "recalculation_pending": tariff.recalculation_pending,
                "action": tariff.current_action.value if tariff.current_action else None,
                "slot_planned": tariff.current_slot_planned,
                "need_class": tariff.current_run_need_class.value if tariff.current_run_need_class else None,
                "start_eligible": tariff.current_run_start_eligible,
                "start_reason": tariff.current_run_suppression_reason,
                "continue_eligible": tariff.current_run_continue_eligible,
                "continue_reason": tariff.current_run_continue_reason,
                "target_soc_percent": tariff.target_soc_percent,
                "requested_charge_power_kw": tariff.requested_charge_power_kw,
                "current_run_grid_import_kwh": tariff.current_run_grid_import_kwh,
                "requested_target_energy_kwh": tariff.requested_target_energy_kwh,
                "demand_margin_requested_kwh": tariff.demand_margin_requested_kwh,
                "demand_margin_unserved_kwh": tariff.demand_margin_unserved_kwh,
                "base_energy_shortfall_kwh": tariff.base_energy_shortfall_kwh,
                "run_end": tariff.current_grid_charge_run_end.isoformat() if tariff.current_grid_charge_run_end else None,
                "slot_end": tariff.current_slot_end.isoformat() if tariff.current_slot_end else None,
                "control_inputs_fresh": tariff.control_inputs_fresh,
                "forecast_data_fresh": tariff.forecast_data_fresh,
                "blocker": tariff.control_input_block_reason,
                "load_profile_source": tariff.load_profile_source,
                "configured_daily_fallback_kwh": tariff.configured_daily_fallback_kwh,
                "input_change_reason": tariff.input_change_reason,
                "input_change_previous_value": tariff.input_change_previous_value,
                "input_change_new_value": tariff.input_change_new_value,
            }
            physical_read_at = _aware_utc(execution.full_block_generation_at)
            physical_mode_code = execution.physical_mode_code
            if (
                physical_read_at is not None
                and physical_read_at <= now
                and type(physical_mode_code) in {int, float}
                and float(physical_mode_code) in {0.0, 3.0, 4.0, 5.0}
            ):
                self._execution_last_valid_read_at = physical_read_at
            if self._execution_last_valid_read_at is not None:
                attributes["execution_last_valid_read_at"] = (
                    self._execution_last_valid_read_at.isoformat()
                )
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
                now=now,
            )
            self._schedule_active_frame(
                ActiveFrame(
                    now=now,
                    decision=decision,
                    candidates=candidates,
                    context=context,
                    rce=rce,
                    tariff=tariff,
                    rcm=rcm,
                    execution=execution,
                )
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
    "async_request_supervisor_master_stop",
    "notify_supervisor_guard",
)
