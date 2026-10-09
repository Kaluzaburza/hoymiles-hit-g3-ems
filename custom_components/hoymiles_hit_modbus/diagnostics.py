"""Diagnostics support for EMS for Hoymiles HIT-(5–20)L-G3."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import datetime, timedelta
from functools import partial
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.components.recorder import history as recorder_history
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.recorder import get_instance as get_recorder_instance
from homeassistant.util import dt as dt_util

from .const import (
    CONF_RESOLVED_SOURCE_DEVICE_ID,
    CONF_SOURCE_DEVICE_ID,
    DOMAIN,
    VERSION,
)
from .diagnostic_redaction import REDACTED, sanitize_diagnostic_value
from .execution_history import recorded_stop_decisions, recorded_supervisor_attributes
from .installation_identity import async_get_or_create_installation_identity
from .models import RuntimeData


# Rich control-event attributes are an optional extension inside the existing
# control_history item mapping.  Readers of report schema 1 already ignore
# unknown item keys, so this additive evidence has its own version and does not
# require a report schema bump.
REPORT_SCHEMA_VERSION = 1
CONTROL_HISTORY_EVENT_SCHEMA_VERSION = 1
HISTORY_HOURS = 24
PROFILED_HISTORY_QUERY_WINDOW_HOURS = 1
MAX_HISTORY_EVENTS_PER_ENTITY = 500
MAX_HISTORY_EVENTS_TOTAL = 4_000
MAX_HISTORY_EVENT_BYTES = 64 * 1024
MAX_HISTORY_SERIALIZED_BYTES = 8 * 1024 * 1024
HISTORY_QUERY_TIMEOUT_SECONDS = 4
HISTORY_COLLECTION_TIMEOUT_SECONDS = 12
_ACTIVE_HISTORY_QUERY_TASKS: dict[int, asyncio.Future[Any]] = {}
ENTRY_REDACT_KEYS = {
    CONF_RESOLVED_SOURCE_DEVICE_ID,
    CONF_SOURCE_DEVICE_ID,
    "unique_id",
}
SNAPSHOT_DOMAINS = {
    "automation",
    "binary_sensor",
    "button",
    "input_boolean",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "number",
    "select",
    "sensor",
    "switch",
    "timer",
}
HISTORY_DOMAINS = {
    "automation",
    "binary_sensor",
    "input_boolean",
    "input_datetime",
    "input_number",
    "input_select",
    "input_text",
    "select",
    "switch",
    "timer",
}
HISTORY_SENSOR_PARTS = (
    "alarm",
    "ems_working_mode",
    "inverter_status",
    "online_status",
    "rce_optimized_plan",
    "rcm_voltage_plan",
    "setup_status",
    "system_work_state",
    "tariff_charge_plan",
)
AGGREGATE_RESPONSE_ENTITY_ID = (
    "sensor.hoymiles_parallel_aggregate_physical_response"
)
# Every rich-history profile below is an exact allowlist.  This original
# aggregate-response contract stays named separately because the offline
# analyzer and regression tests consume it directly.
AGGREGATE_RESPONSE_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "authoritative_expected_power",
        "baseline_generation",
        "candidate_generations",
        "collection_baseline_generation",
        "completed_at",
        "configuration_acknowledgement_scope",
        "detected_inverters",
        "evidence_scope",
        "expected_power_kw",
        "final_generation",
        "formula",
        "grid_samples_kw",
        "individual_inverter_acknowledgement",
        "latched_machine_type",
        "observed_median_power_kw",
        "observed_spread_kw",
        "owner",
        "pending_at",
        "phase",
        "reason",
        "required_stable_generations",
        "requires_parallel_proof",
        "sample_count",
        "sampled_transition_observed",
        "sampled_transition_peak_kw",
        "sampled_transition_scope",
        "samples_kw",
        "stable_window_start",
        "tolerance_kw",
        "topology_known",
        "transaction_id",
        "transaction_started_epoch",
        "transition_grace_seconds",
        "verification_horizon_seconds",
    }
)

PLANNER_LIFECYCLE_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "captured_input_revision",
        "control_input_block_reason",
        "execution_blocker_code",
        "full_plan_solver_calls",
        "input_revision",
        "plan_revision",
        "profile_revision",
        "price_provider",
        "joint_plan_revision",
        "joint_profile_revision",
        "pstryk_blocker",
        "load_profile_generated_at",
        "load_profile_age_seconds",
        "load_profile_snapshot_age_seconds",
        "load_profile_data_fresh",
        "load_profile_broker_fresh",
        "load_model_schema",
        "load_model_quality",
        "load_model_revision",
        "load_model_source",
        "load_history_source",
        "load_history_read_status",
        "load_history_read_error",
        "load_history_last_attempt_at",
        "load_history_last_success_at",
        "load_history_retry_count",
        "load_history_next_retry_at",
        "load_daily_coverage_ratio",
        "load_profile_coverage_ratio",
        "load_history_days",
        "load_profile_source",
        "last_full_plan_at",
        "last_full_plan_trigger",
        "pending_input_revision",
        "planner_event",
        "planner_event_schema_version",
        "planner_reason_code",
        "recalculation_pending",
        "result_current",
        "status_code",
    }
)

RCE_PLANNER_HISTORY_ATTRIBUTE_KEYS = frozenset(
    set(PLANNER_LIFECYCLE_HISTORY_ATTRIBUTE_KEYS)
    | {
        "automatic_price_floor_pln_kwh",
        "bms_discharge_data_available",
        "bms_discharge_data_fresh",
        "current_price_pln_kwh",
        "current_required_minimum_soc_percent",
        "current_run_end",
        "current_slot_continue_eligible",
        "current_slot_continue_reason",
        "current_slot_end",
        "current_slot_execution_discharge_power_kw",
        "current_slot_execution_export_power_kw",
        "current_slot_execution_power_percent",
        "current_slot_planned",
        "current_slot_planned_export_kwh",
        "current_slot_start_eligible",
        "current_slot_suppression_reason",
        "execution_input_valid",
        "forecast_day3_data_fresh",
        "forecast_remaining_today_data_fresh",
        "forecast_today_data_fresh",
        "forecast_tomorrow_data_fresh",
        "gcf_data_fresh",
        "gcf_execution_data_fresh",
        "gcf_limit_data_fresh",
        "inverter_count_data_fresh",
        "minimum_soc",
        "planned_export_kwh",
        "rce_today_data_fresh",
        "rce_tomorrow_data_fresh",
        "self_use_soc_data_fresh",
        "soc_data_fresh",
        "current_slot_execution_discharge_power_percent",
        "pv_charge_delay_current",
        "pv_charge_delay_execution_ready",
        "pv_charge_delay_start",
        "pv_charge_delay_end",
    }
)

TARIFF_PLANNER_HISTORY_ATTRIBUTE_KEYS = frozenset(
    set(PLANNER_LIFECYCLE_HISTORY_ATTRIBUTE_KEYS)
    | {
        "bms_charge_data_fresh",
        "bms_discharge_data_fresh",
        "command_charge_power_percent",
        "control_inputs_fresh",
        "current_action",
        "current_price_pln_kwh",
        "current_run_benefit_pln",
        "current_run_continue_eligible",
        "current_run_continue_reason",
        "current_run_grid_import_kwh",
        "current_run_remaining_minutes",
        "current_run_start_eligible",
        "current_run_stored_kwh",
        "current_run_suppression_reason",
        "current_slot_end",
        "current_slot_planned",
        "execution_input_valid",
        "forecast_data_fresh",
        "forecast_day_3_data_fresh",
        "forecast_remaining_today_data_fresh",
        "forecast_today_data_fresh",
        "forecast_tomorrow_data_fresh",
        "inverter_count_data_fresh",
        "live_power_data_fresh",
        "load_profile_broker_fresh",
        "load_profile_data_fresh",
        "planned_grid_import_kwh",
        "planned_stored_energy_kwh",
        "self_use_soc_data_fresh",
        "soc_data_fresh",
        "target_soc_percent",
        "tariff_operator",
        "tariff_type",
        "tariff_profile_data_version",
        "tariff_profile_valid_from",
        "tariff_profile_valid_until",
        "tariff_profile_low_windows",
        "tariff_profile_low_price",
        "tariff_profile_peak_price",
    }
)

RCM_PLANNER_HISTORY_ATTRIBUTE_KEYS = frozenset(
    set(PLANNER_LIFECYCLE_HISTORY_ATTRIBUTE_KEYS)
    | {
        "action",
        "actuator_data_fresh",
        "battery_capacity_data_fresh",
        "battery_soc_data_fresh",
        "bms_charge_data_fresh",
        "bms_discharge_data_fresh",
        "charge_actuator_data_fresh",
        "discharge_registers_data_fresh",
        "emergency_action_ready",
        "emergency_voltage_data_fresh",
        "ems_mode_data_fresh",
        "estimated_safe_export_reason",
        "execution_input_valid",
        "export_actuator_data_fresh",
        "export_register_data_fresh",
        "force_discharge_soc_data_fresh",
        "forecast_data_fresh",
        "gcf_data_fresh",
        "history_data_fresh",
        "live_control_cache_current",
        "live_control_eligible",
        "live_control_input_current",
        "live_control_status",
        "live_emergency",
        "live_power_data_fresh",
        "load_profile_broker_fresh",
        "load_profile_data_fresh",
        "maximum_discharge_power_data_fresh",
        "pre_discharge_continue_eligible",
        "pre_discharge_actuator_data_fresh",
        "pre_discharge_deadline",
        "pre_discharge_power_kw",
        "pre_discharge_power_percent",
        "pre_discharge_ready",
        "pre_discharge_start_eligible",
        "pre_discharge_target_soc_percent",
        "prediction_block_reason",
        "recommended_charge_limit_percent",
        "recommended_charge_power_kw",
        "recommended_export_limit_percent",
        "reason",
        "voltage_data_fresh",
    }
)

AUTOMATION_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "current",
        "last_triggered",
        "mode",
    }
)

TIMELINE_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "active_observed_at",
        "active_scope",
        "blocker_code",
        "current_actual",
        "generated_at",
        "horizon_end",
        "horizon_start",
        "input_revision",
        "pending_input_revision",
        "plan_revision",
        "point_count",
        "policy_id",
        "quality",
        "recalculation_pending",
        "result_current",
        "schema_version",
    }
)

CANONICAL_PLAN_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "adapter_error_stage",
        "adapter_error_type",
        "arbitration_revision",
        "authorization_final_soc_percent",
        "authorization_grid_export_kwh",
        "authorization_projection",
        "authorization_reserve_violation_count",
        "built_at",
        "blocker_code",
        "canonical_blocker_code",
        "canonical_status",
        "expected_final_soc_percent",
        "expected_grid_export_kwh",
        "expected_min_soc_percent",
        "expected_pv_curtailed_kwh",
        "expected_projection",
        "expected_projection_quality",
        "ledger_revision",
        "projection_contract",
        "result_current",
        "schema_version",
        "source_timeline_states",
    }
)

SUPERVISOR_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "active_state",
        "arbitration_revision",
        "automatic_policies_enabled",
        "candidate_summaries",
        "command_snapshot",
        "control_lease",
        "control_lease_renewal",
        "deadline",
        "execution_blocked_reason",
        "execution_context_evidence",
        "execution_phase",
        "execution_health",
        "expected_readback",
        "executor_revision",
        "legacy_execution_unchanged",
        "lifecycle_reason",
        "manual_proxy_readback_pending",
        "master_stop",
        "observed_owner",
        "owner",
        "owner_conflict",
        "physical_verification",
        "physical_verification_result",
        "profile",
        "reason",
        "rce_export_evidence",
        "pv_hold_retry",
        "tariff_decision",
        "recent_stop_decisions",
        "stop_decisions_available",
        "rejected_reasons",
        "rollback_result",
        "rollback_status",
        "schema_version",
        "selected_action",
        "selected_candidate_revision",
        "selected_policy",
        "selection_kind",
        "selection_reason",
        "started_at",
        "starts_allowed",
        "state",
        "supervisor_execution_authorized",
        "transaction_candidate_identity",
        "transaction_evidence",
        "transaction_evidence_schema_version",
        "transaction_evidence_scope",
        "transaction_id",
        "transaction_owner",
    }
)

BALANCING_TRANSACTION_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "cooldown_epoch_ms",
        "current_ems_generation",
        "current_physical_4303",
        "current_physical_4304",
        "current_physical_mode",
        "current_topology_generation",
        "cycle_id",
        "event_marker",
        "invalid_reason",
        "operational_started",
        "owner",
        "owner_generation",
        "reason_code",
        "schema_valid",
        "snapshot_4303",
        "snapshot_4304",
        "snapshot_cycle_id",
        "snapshot_ems_generation",
        "snapshot_epoch_ms",
        "snapshot_mode",
        "snapshot_topology_generation",
        "snapshot_valid",
        "transaction_state",
        "write_started",
    }
)

BALANCING_TIMING_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "cycle_id",
        "gap_deadline_epoch_ms",
        "gap_generation",
        "gap_start_epoch_ms",
        "hold_deadline_epoch_ms",
        "hold_generation",
        "timing_state",
    }
)

BALANCING_ABORT_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "cycle_id",
        "priority",
        "reason_code",
        "request_generation",
        "request_state",
        "requested_epoch_ms",
    }
)

BALANCING_OUTBOX_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "slot_1_attempt_count",
        "slot_1_cycle_id",
        "slot_1_delivery_state",
        "slot_1_event_id",
        "slot_1_kind",
        "slot_1_reason",
        "slot_2_attempt_count",
        "slot_2_cycle_id",
        "slot_2_delivery_state",
        "slot_2_event_id",
        "slot_2_kind",
        "slot_2_reason",
    }
)

OWNER_HISTORY_ATTRIBUTE_KEYS = frozenset(
    {
        "conflict",
        "ems_mode",
        "owner_code",
        "rce_policy_enabled",
        "rcm_policy_enabled",
        "tariff_policy_enabled",
    }
)

HISTORY_ATTRIBUTE_KEYS_BY_PROFILE = {
    "aggregate_response": AGGREGATE_RESPONSE_HISTORY_ATTRIBUTE_KEYS,
    "automation": AUTOMATION_HISTORY_ATTRIBUTE_KEYS,
    "balancing_abort": BALANCING_ABORT_HISTORY_ATTRIBUTE_KEYS,
    "balancing_outbox": BALANCING_OUTBOX_HISTORY_ATTRIBUTE_KEYS,
    "balancing_timing": BALANCING_TIMING_HISTORY_ATTRIBUTE_KEYS,
    "balancing_transaction": BALANCING_TRANSACTION_HISTORY_ATTRIBUTE_KEYS,
    "canonical_plan": CANONICAL_PLAN_HISTORY_ATTRIBUTE_KEYS,
    "owner": OWNER_HISTORY_ATTRIBUTE_KEYS,
    "rce_planner": RCE_PLANNER_HISTORY_ATTRIBUTE_KEYS,
    "rcm_planner": RCM_PLANNER_HISTORY_ATTRIBUTE_KEYS,
    "supervisor": SUPERVISOR_HISTORY_ATTRIBUTE_KEYS,
    "timeline": TIMELINE_HISTORY_ATTRIBUTE_KEYS,
    "tariff_planner": TARIFF_PLANNER_HISTORY_ATTRIBUTE_KEYS,
}

HISTORY_ATTRIBUTE_PROFILE_BY_ENTITY_ID = {
    AGGREGATE_RESPONSE_ENTITY_ID: "aggregate_response",
    "sensor.hoymiles_battery_balancing_abort_request": "balancing_abort",
    "sensor.hoymiles_battery_balancing_notification_outbox": "balancing_outbox",
    "sensor.hoymiles_battery_balancing_timing_transaction": "balancing_timing",
    "sensor.hoymiles_battery_balancing_transaction": "balancing_transaction",
    "sensor.hoymiles_ems_control_owner": "owner",
    "sensor.hoymiles_hit_ems_supervisor": "supervisor",
    "sensor.hoymiles_hit_ems_supervisor_canonical_plan": "canonical_plan",
    "sensor.hoymiles_hit_rce_automation_plan_timeline": "timeline",
    "sensor.hoymiles_hit_rce_optimized_plan": "rce_planner",
    "sensor.hoymiles_hit_rcm_automation_plan_timeline": "timeline",
    "sensor.hoymiles_hit_rcm_voltage_plan": "rcm_planner",
    "sensor.hoymiles_hit_tariff_automation_plan_timeline": "timeline",
    "sensor.hoymiles_hit_tariff_charge_plan": "tariff_planner",
}

# State-only context needed to interpret whether execution evidence was fresh
# and applicable to the physical topology.  Attribute retrieval stays disabled
# for these entities.
HISTORY_CONTEXT_SENSOR_ENTITY_IDS = frozenset(
    {
        "sensor.hoymiles_ems_hardware_mode",
        "sensor.hoymiles_hit_battery_charge_power_readback_generation",
        "sensor.hoymiles_hit_battery_max_charge_power_readback",
        "sensor.hoymiles_hit_ems_control_readback_generation",
        "sensor.hoymiles_hit_ems_backup_soc_readback",
        "sensor.hoymiles_hit_ems_force_charge_soc_readback",
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
        "sensor.hoymiles_hit_ems_maximum_charge_power_readback",
        "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "sensor.hoymiles_hit_ems_self_use_soc_readback",
        "sensor.hoymiles_hit_gcf_control_readback_generation",
        "sensor.hoymiles_hit_gcf_enable_readback_code",
        "sensor.hoymiles_hit_gcf_maximum_export_power_readback",
        "sensor.hoymiles_hit_machines_type",
        "sensor.hoymiles_hit_number_of_machines_master_and_slave",
        "sensor.hoymiles_hit_parallel_aggregate_power_readback_generation",
        "sensor.hoymiles_hit_parallel_topology_readback_generation",
    }
)

HISTORY_EXACT_SENSOR_ENTITY_IDS = frozenset(
    set(HISTORY_CONTEXT_SENSOR_ENTITY_IDS)
    | set(HISTORY_ATTRIBUTE_PROFILE_BY_ENTITY_ID)
)


def _supervisor_diagnostic_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Decode bounded Recorder evidence before privacy filtering.

    Encoded strings cannot be sanitized safely and are not useful to support.
    A corrupt/unknown compressed format must remain unknown, never an empty
    STOP journal. No Recorder writes or device reads are performed here.
    """
    result = dict(recorded_supervisor_attributes(attributes))
    result.update(attributes)  # Prefer fuller live/legacy evidence when present.
    frames = recorded_stop_decisions(attributes)
    result.pop("recorded_execution", None)
    result.pop("recorded_stop_decisions", None)
    result.pop("recent_stop_decisions", None)
    result["stop_decisions_available"] = frames is not None
    if frames is not None:
        result["recent_stop_decisions"] = frames
    return result


def _state_snapshot(state: State | None, *, key_hint: str = "") -> Any:
    """Return a compact, redacted representation of a Home Assistant state."""
    if state is None:
        return None
    state_value: Any = state.state
    if "serial" in key_hint.casefold():
        state_value = REDACTED
    attributes = dict(state.attributes)
    if _history_attribute_profile(key_hint) == "supervisor":
        attributes = _supervisor_diagnostic_attributes(attributes)
    return {
        "state": sanitize_diagnostic_value(state_value, key_hint=key_hint),
        "attributes": sanitize_diagnostic_value(attributes),
        "last_changed": state.last_changed.isoformat(),
        "last_updated": state.last_updated.isoformat(),
    }


def _is_hoymiles_state(entity_id: str) -> bool:
    """Return whether an entity belongs to the managed Hoymiles setup."""
    domain, separator, object_id = entity_id.partition(".")
    return bool(
        separator
        and domain in SNAPSHOT_DOMAINS
        and (
            object_id.startswith("hoymiles_")
            or object_id.startswith("hoymiles_hit_")
        )
    )


def _needs_history(entity_id: str) -> bool:
    """Select slow-changing control entities without querying fast telemetry."""
    domain, _, object_id = entity_id.partition(".")
    if domain in HISTORY_DOMAINS:
        return True
    return entity_id in HISTORY_EXACT_SENSOR_ENTITY_IDS or (
        domain == "sensor"
        and any(part in object_id for part in HISTORY_SENSOR_PARTS)
    )


def _history_attribute_profile(entity_id: str) -> str | None:
    """Return the exact bounded attribute profile for one entity."""

    if entity_id.startswith("automation.hoymiles_"):
        return "automation"
    return HISTORY_ATTRIBUTE_PROFILE_BY_ENTITY_ID.get(entity_id)


def _history_attributes(entity_id: str, attributes: Any) -> dict[str, Any]:
    """Return only explicitly approved attributes for one control entity."""

    if not isinstance(attributes, Mapping):
        return {}
    profile = _history_attribute_profile(entity_id)
    if profile is None:
        return {}
    allowed_keys = HISTORY_ATTRIBUTE_KEYS_BY_PROFILE[profile]
    if profile == "supervisor":
        # Recorded rows provide compact proof; live/legacy snapshots retain
        # their original details whenever those attributes are available.
        attributes = _supervisor_diagnostic_attributes(attributes)
    return {
        key: sanitize_diagnostic_value(attributes[key], key_hint=key)
        for key in sorted(allowed_keys)
        if key in attributes
    }


def _serialize_history_item(
    item: Any,
    *,
    entity_id: str,
) -> dict[str, Any]:
    """Serialize recorder State objects and tolerate compressed dictionaries."""
    profile = _history_attribute_profile(entity_id)
    if isinstance(item, State):
        serialized = {
            "state": sanitize_diagnostic_value(item.state),
            "last_changed": item.last_changed.isoformat(),
            "last_updated": item.last_updated.isoformat(),
        }
        if profile is not None:
            serialized["event_schema_version"] = (
                CONTROL_HISTORY_EVENT_SCHEMA_VERSION
            )
            serialized["attribute_profile"] = profile
            serialized["attributes_available"] = True
            serialized["attributes"] = _history_attributes(
                entity_id,
                dict(item.attributes),
            )
        return serialized
    if isinstance(item, dict):
        serialized = {
            str(key): sanitize_diagnostic_value(value, key_hint=str(key))
            for key, value in item.items()
            if key != "attributes"
        }
        if profile is not None:
            serialized["event_schema_version"] = (
                CONTROL_HISTORY_EVENT_SCHEMA_VERSION
            )
            serialized["attribute_profile"] = profile
            serialized["attributes_available"] = isinstance(item.get("attributes"), Mapping)
            serialized["attributes"] = _history_attributes(entity_id, item.get("attributes"))
        return serialized
    return {"value": sanitize_diagnostic_value(item)}


def _serialized_history_size(value: Mapping[str, Any]) -> int:
    """Return deterministic UTF-8 JSON size for one sanitized event."""

    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    )


def _bounded_history_item(
    item: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    """Omit an oversized attribute block atomically, never partially."""

    if _serialized_history_size(item) <= MAX_HISTORY_EVENT_BYTES:
        return item, False
    bounded = {
        key: value
        for key, value in item.items()
        if key
        in {
            "attribute_profile",
            "event_schema_version",
            "last_changed",
            "last_updated",
            "state",
            "value",
        }
    }
    bounded["attributes_available"] = False
    bounded["omission_reason"] = "event_size_limit"
    return bounded, True


def _history_item_timestamp(item: Mapping[str, Any]) -> float:
    """Return a sortable event timestamp without trusting recorder mappings."""

    for key in ("last_updated", "last_changed"):
        value = item.get(key)
        if not isinstance(value, str):
            continue
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.timestamp()
        except (OSError, OverflowError, ValueError):
            continue
    return float("-inf")


def _history_semantic_signature(item: Mapping[str, Any]) -> str:
    """Return a stable signature which ignores Recorder update timestamps."""

    semantic = {
        key: value
        for key, value in item.items()
        if key not in {"last_changed", "last_updated"}
    }
    return json.dumps(
        semantic,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _history_limits() -> dict[str, int]:
    """Publish the exact bounded export contract alongside its evidence."""

    return {
        "max_events_per_entity": MAX_HISTORY_EVENTS_PER_ENTITY,
        "max_events_total": MAX_HISTORY_EVENTS_TOTAL,
        "max_event_bytes": MAX_HISTORY_EVENT_BYTES,
        "max_encoded_event_bytes_total": MAX_HISTORY_SERIALIZED_BYTES,
        "profiled_query_window_hours": PROFILED_HISTORY_QUERY_WINDOW_HOURS,
        "query_timeout_seconds": HISTORY_QUERY_TIMEOUT_SECONDS,
        "collection_timeout_seconds": HISTORY_COLLECTION_TIMEOUT_SECONDS,
    }


class DiagnosticHistoryQueryTimeoutError(TimeoutError):
    """A Recorder query exceeded the diagnostics-only time budget."""


class DiagnosticHistoryQueryBusyError(RuntimeError):
    """A timed-out Recorder worker from an earlier export is still active."""


def _history_query_done(
    hass_key: int,
    task: asyncio.Future[Any],
) -> None:
    """Consume a detached worker result and release its single-flight slot."""

    if _ACTIVE_HISTORY_QUERY_TASKS.get(hass_key) is task:
        _ACTIVE_HISTORY_QUERY_TASKS.pop(hass_key, None)
    try:
        task.exception()
    except asyncio.CancelledError:
        pass


async def _async_bounded_recorder_query(
    hass: HomeAssistant,
    recorder: Any,
    query: Any,
    *,
    timeout_seconds: float,
) -> Mapping[str, Any]:
    """Run one Recorder query without accumulating timed-out workers.

    Cancelling an asyncio wrapper cannot stop work already running in the
    Recorder executor.  Keep that surviving task registered until it really
    finishes and fail subsequent exports fast in the meantime.
    """

    hass_key = id(hass)
    active = _ACTIVE_HISTORY_QUERY_TASKS.get(hass_key)
    if active is not None and not active.done():
        raise DiagnosticHistoryQueryBusyError(
            "previous diagnostics Recorder query is still running"
        )
    if active is not None:
        _ACTIVE_HISTORY_QUERY_TASKS.pop(hass_key, None)

    # Recorder returns an already enqueued Future; create_task only accepts
    # coroutines and would lose ownership of the running worker on TypeError.
    task = asyncio.ensure_future(recorder.async_add_executor_job(query))
    _ACTIVE_HISTORY_QUERY_TASKS[hass_key] = task
    task.add_done_callback(
        lambda finished: _history_query_done(hass_key, finished)
    )
    try:
        return await asyncio.wait_for(
            asyncio.shield(task),
            timeout=max(float(timeout_seconds), 0.001),
        )
    except TimeoutError as err:
        raise DiagnosticHistoryQueryTimeoutError(
            "diagnostics Recorder query timed out"
        ) from err


async def _async_history(
    hass: HomeAssistant,
    entity_ids: list[str],
) -> dict[str, Any]:
    """Return 24 hours of significant control changes from Recorder."""
    if not entity_ids:
        return {
            "available": True,
            "complete": True,
            "event_schema_version": CONTROL_HISTORY_EVENT_SCHEMA_VERSION,
            "hours": HISTORY_HOURS,
            "entities": {},
            "entity_coverage": {},
            "limits": _history_limits(),
            "returned_event_count": 0,
            "queried_event_count": 0,
            "semantic_duplicate_count": 0,
            "dropped_event_count": 0,
            "attribute_omission_count": 0,
            "encoded_event_bytes": 0,
            "profiled_query_window_count": 0,
            "incomplete_entities": [],
            "truncated_entities": [],
        }
    end = dt_util.utcnow()
    start = end - timedelta(hours=HISTORY_HOURS)
    candidates: dict[str, list[dict[str, Any]]] = {
        entity_id: [] for entity_id in entity_ids
    }
    coverage: dict[str, dict[str, Any]] = {
        entity_id: {
            "profile": _history_attribute_profile(entity_id) or "state_only",
            "queried_count": 0,
            "semantic_event_count": 0,
            "semantic_duplicate_count": 0,
            "returned_count": 0,
            "dropped_count": 0,
            "attribute_omission_count": 0,
        }
        for entity_id in entity_ids
    }
    last_signatures: dict[str, str] = {}

    def ingest_batch(raw_batch: Mapping[str, Any], selected_ids: list[str]) -> None:
        """Sanitize, semantically compact and bound one Recorder batch."""

        for entity_id in selected_ids:
            raw_items = list(raw_batch.get(entity_id, []))
            coverage[entity_id]["queried_count"] += len(raw_items)
            serialized_batch: list[dict[str, Any]] = []
            for item in raw_items:
                serialized_item, attributes_omitted = _bounded_history_item(
                    _serialize_history_item(item, entity_id=entity_id)
                )
                serialized_batch.append(serialized_item)
                if attributes_omitted:
                    coverage[entity_id]["attribute_omission_count"] += 1
            serialized_batch.sort(key=_history_item_timestamp)
            for serialized_item in serialized_batch:
                signature = _history_semantic_signature(serialized_item)
                if last_signatures.get(entity_id) == signature:
                    coverage[entity_id]["semantic_duplicate_count"] += 1
                    continue
                last_signatures[entity_id] = signature
                coverage[entity_id]["semantic_event_count"] += 1
                candidates[entity_id].append(serialized_item)
                if len(candidates[entity_id]) > MAX_HISTORY_EVENTS_PER_ENTITY:
                    del candidates[entity_id][0]
                    coverage[entity_id]["dropped_count"] += 1

    profiled_query_window_count = 0
    collection_error: Exception | None = None
    try:
        recorder = get_recorder_instance(hass)
        loop = asyncio.get_running_loop()
        collection_deadline = loop.time() + HISTORY_COLLECTION_TIMEOUT_SECONDS

        async def run_query(query: Any) -> Mapping[str, Any]:
            remaining = collection_deadline - loop.time()
            if remaining <= 0:
                raise DiagnosticHistoryQueryTimeoutError(
                    "diagnostics history collection timed out"
                )
            return await _async_bounded_recorder_query(
                hass,
                recorder,
                query,
                timeout_seconds=min(HISTORY_QUERY_TIMEOUT_SECONDS, remaining),
            )

        profiled_entity_ids = [
            entity_id
            for entity_id in entity_ids
            if _history_attribute_profile(entity_id) is not None
        ]
        regular_entity_ids = [
            entity_id
            for entity_id in entity_ids
            if _history_attribute_profile(entity_id) is None
        ]
        if regular_entity_ids:
            regular_query = partial(
                recorder_history.get_significant_states,
                hass,
                start,
                end,
                regular_entity_ids,
                None,
                True,
                True,
                False,
                True,
            )
            regular_raw = await run_query(regular_query)
            ingest_batch(regular_raw, regular_entity_ids)
        if profiled_entity_ids:
            # Attribute retrieval is isolated to strictly allowlisted control
            # entities.  Read it in small windows and compact every batch
            # immediately so a fast RCEm attribute cannot materialize a full
            # day of large Recorder states in memory or crowd out older
            # semantic decisions.
            query_window = timedelta(hours=PROFILED_HISTORY_QUERY_WINDOW_HOURS)
            window_start = start
            while window_start < end:
                window_end = min(window_start + query_window, end)
                profiled_query = partial(
                    recorder_history.get_significant_states,
                    hass,
                    window_start,
                    window_end,
                    profiled_entity_ids,
                    None,
                    True,
                    False,
                    False,
                    False,
                )
                profiled_raw = await run_query(profiled_query)
                ingest_batch(profiled_raw, profiled_entity_ids)
                profiled_query_window_count += 1
                window_start = window_end
    except Exception as err:  # noqa: BLE001 - diagnostics must still download
        collection_error = err

    for entity_id in entity_ids:
        evidence_present = coverage[entity_id]["queried_count"] > 0
        coverage[entity_id]["evidence_present"] = evidence_present
        coverage[entity_id]["evidence_status"] = (
            "available" if evidence_present else "unknown_no_recorder_rows"
        )

    # Retain one deterministic newest suffix across all entities.  Once a
    # global limit is reached, older events are not allowed to displace newer
    # evidence merely because they serialize to fewer bytes.
    ordered_events: list[tuple[float, str, int, dict[str, Any], int]] = []
    for entity_id, items in candidates.items():
        for index, item in enumerate(items):
            ordered_events.append(
                (
                    _history_item_timestamp(item),
                    entity_id,
                    index,
                    item,
                    _serialized_history_size(item),
                )
            )
    ordered_events.sort(
        key=lambda event: (event[0], event[1], event[2]),
        reverse=True,
    )
    retained: set[tuple[str, int]] = set()
    encoded_event_bytes = 0
    for _timestamp, entity_id, index, _item, event_bytes in ordered_events:
        if len(retained) >= MAX_HISTORY_EVENTS_TOTAL:
            break
        if encoded_event_bytes + event_bytes > MAX_HISTORY_SERIALIZED_BYTES:
            break
        retained.add((entity_id, index))
        encoded_event_bytes += event_bytes

    serialized: dict[str, list[dict[str, Any]]] = {}
    for entity_id, items in candidates.items():
        retained_items = [
            item
            for index, item in enumerate(items)
            if (entity_id, index) in retained
        ]
        globally_dropped = len(items) - len(retained_items)
        coverage[entity_id]["returned_count"] = len(retained_items)
        coverage[entity_id]["dropped_count"] += globally_dropped
        coverage[entity_id]["complete"] = bool(
            coverage[entity_id]["evidence_present"]
            and coverage[entity_id]["dropped_count"] == 0
            and coverage[entity_id]["attribute_omission_count"] == 0
        )
        if retained_items:
            coverage[entity_id]["oldest_retained_at"] = retained_items[0].get(
                "last_updated",
                retained_items[0].get("last_changed"),
            )
            coverage[entity_id]["newest_retained_at"] = retained_items[-1].get(
                "last_updated",
                retained_items[-1].get("last_changed"),
            )
        else:
            coverage[entity_id]["oldest_retained_at"] = None
            coverage[entity_id]["newest_retained_at"] = None
        serialized[entity_id] = retained_items

    incomplete = sorted(
        entity_id
        for entity_id, item in coverage.items()
        if not item["complete"]
    )
    truncated = sorted(
        entity_id
        for entity_id, item in coverage.items()
        if item["dropped_count"] > 0
        or item["attribute_omission_count"] > 0
    )
    queried_event_count = sum(
        item["queried_count"] for item in coverage.values()
    )
    semantic_duplicate_count = sum(
        item["semantic_duplicate_count"] for item in coverage.values()
    )
    dropped_event_count = sum(
        item["dropped_count"] for item in coverage.values()
    )
    attribute_omission_count = sum(
        item["attribute_omission_count"] for item in coverage.values()
    )
    omission_reason = None
    if isinstance(collection_error, DiagnosticHistoryQueryTimeoutError):
        omission_reason = "history_query_timeout"
    elif isinstance(collection_error, DiagnosticHistoryQueryBusyError):
        omission_reason = "history_query_busy"
    elif collection_error is not None:
        omission_reason = "history_query_error"
    any_query_completed = any(
        item["queried_count"] > 0 for item in coverage.values()
    )
    available = collection_error is None or any_query_completed
    result = {
        "available": available,
        "complete": collection_error is None and not incomplete,
        "event_schema_version": CONTROL_HISTORY_EVENT_SCHEMA_VERSION,
        "hours": HISTORY_HOURS,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "entities": serialized,
        "entity_coverage": coverage,
        "limits": _history_limits(),
        "queried_event_count": queried_event_count,
        "returned_event_count": len(retained),
        "semantic_duplicate_count": semantic_duplicate_count,
        "dropped_event_count": dropped_event_count,
        "attribute_omission_count": attribute_omission_count,
        "encoded_event_bytes": encoded_event_bytes,
        "profiled_query_window_count": profiled_query_window_count,
        "incomplete_entities": incomplete,
        "truncated_entities": truncated,
    }
    if collection_error is not None:
        result.update(
            {
                "collection_status": (
                    "partial" if any_query_completed else "unavailable"
                ),
                "omission_reason": omission_reason,
                "error_type": type(collection_error).__name__,
            }
        )
    return result


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return a privacy-safe report downloadable from Home Assistant."""
    installation_identity = await async_get_or_create_installation_identity(
        hass
    )
    runtime: RuntimeData | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    entity_registry = er.async_get(hass)
    proxy_entries = er.async_entries_for_config_entry(entity_registry, entry.entry_id)
    proxies_by_key = {
        (registry_entry.domain, registry_entry.translation_key): registry_entry
        for registry_entry in proxy_entries
        if registry_entry.translation_key
    }

    catalog_rows: list[dict[str, Any]] = []
    expected_by_domain: dict[str, int] = {}
    present_by_domain: dict[str, int] = {}
    missing_translation_keys: list[str] = []
    if runtime is not None:
        for domain, matched_entities in runtime.entities.items():
            expected_by_domain[domain] = len(matched_entities)
            present_by_domain[domain] = sum(
                matched.source is not None for matched in matched_entities
            )
            for matched in matched_entities:
                translation_key = str(matched.catalog["translation_key"])
                proxy = proxies_by_key.get((domain, translation_key))
                source_state = (
                    hass.states.get(matched.source.entity_id)
                    if matched.source is not None
                    else None
                )
                if matched.source is None:
                    missing_translation_keys.append(translation_key)
                catalog_rows.append(
                    {
                        "domain": domain,
                        "translation_key": translation_key,
                        "source_present": matched.source is not None,
                        "source_state": _state_snapshot(
                            source_state,
                            key_hint=translation_key,
                        ),
                        "proxy_entity_id": proxy.entity_id if proxy else None,
                        "proxy_state": _state_snapshot(
                            hass.states.get(proxy.entity_id) if proxy else None,
                            key_hint=translation_key,
                        ),
                    }
                )

    managed_states = sorted(
        (
            state
            for state in hass.states.async_all()
            if _is_hoymiles_state(state.entity_id)
        ),
        key=lambda state: state.entity_id,
    )
    state_snapshot = {
        state.entity_id: _state_snapshot(state, key_hint=state.entity_id)
        for state in managed_states
    }
    history_entity_ids = [
        state.entity_id for state in managed_states if _needs_history(state.entity_id)
    ]

    source_device: dict[str, Any] | None = None
    if runtime is not None:
        source_device = {
            "manufacturer": sanitize_diagnostic_value(
                getattr(runtime.source_device, "manufacturer", None)
            ),
            "model": sanitize_diagnostic_value(
                getattr(runtime.source_device, "model", None)
            ),
            "hardware_version": sanitize_diagnostic_value(
                getattr(runtime.source_device, "hw_version", None)
            ),
            "firmware_version": sanitize_diagnostic_value(
                getattr(runtime.source_device, "sw_version", None)
            ),
        }

    lease_client = getattr(runtime, "control_lease", None)
    lease_journal = (
        lease_client.journal_snapshot() if lease_client is not None
        else {"available": False, "reason": "control_lease_client_unavailable"}
    )
    return {
        "report_schema": REPORT_SCHEMA_VERSION,
        **installation_identity.as_dict(),
        "generated_at": dt_util.utcnow().isoformat(),
        "integration_version": VERSION,
        "config_entry": {
            "title": "EMS for Hoymiles HIT-(5–20)L-G3",
            "data": async_redact_data(dict(entry.data), ENTRY_REDACT_KEYS),
            "options": async_redact_data(dict(entry.options), ENTRY_REDACT_KEYS),
        },
        "source_device": source_device,
        "catalog_coverage": {
            "runtime_loaded": runtime is not None,
            "expected_by_domain": expected_by_domain,
            "present_by_domain": present_by_domain,
            "missing_count": len(missing_translation_keys),
            "missing_translation_keys": sorted(missing_translation_keys),
        },
        "catalog_entities": catalog_rows,
        "managed_state_snapshot": state_snapshot,
        "notification_history": sanitize_diagnostic_value(
            runtime.notifications.diagnostic_snapshot()
            if runtime is not None and getattr(runtime, "notifications", None) is not None
            else {"available": False, "reason": "notification_manager_unavailable"}
        ),
        "accepted_lease_journal": sanitize_diagnostic_value(lease_journal),
        "evidence_contract": {
            "schema_version": 1,
            "current_state_scope": "export_time_only",
            "history_scope": "bounded_recorded_state_changes",
            "recorder_excluded_attributes": "unavailable_in_history_unless_compactly_recorded",
            "accepted_lease_renewal_journal_available": lease_journal["available"],
            "lease_continuity": "bounded_process_local_ACK_journal_requires_cycle_range_gap_and_overflow_checks",
            "physical_scope": "master_and_aggregate_are_not_individual_slave_fc03",
            "notification_delivery_scope": "ha_provider_result_not_phone_delivery",
            "deployment_hashes_available": False,
        },
        "control_history": await _async_history(hass, history_entity_ids),
    }
