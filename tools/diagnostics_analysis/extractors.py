"""Normalize planner and control evidence from diagnostic reports."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping, Sequence

from .privacy import sanitize_diagnostic_value

from .models import (
    Confidence,
    Controller,
    ControllerObservation,
    LoadedDiagnosticArchive,
    ValueStatus,
)


UNKNOWN_MARKERS = frozenset(
    {
        "[redacted]",
        "[truncated]",
        "[max_depth_reached]",
        "[depth_limit]",
        "unknown",
        "unavailable",
        "none",
        "null",
    }
)

CONTROLLER_SUFFIXES: Mapping[Controller, tuple[str, ...]] = {
    Controller.RCE: ("hoymiles_hit_rce_optimized_plan", "rce_optimized_plan"),
    Controller.RCEM: ("hoymiles_hit_rcm_voltage_plan", "rcm_voltage_plan"),
    Controller.TARIFF: (
        "hoymiles_hit_tariff_charge_plan",
        "tariff_charge_plan",
    ),
}

CONTROLLER_TRANSLATION_KEYS: Mapping[Controller, str] = {
    Controller.RCE: "rce_optimized_plan",
    Controller.RCEM: "rcm_voltage_plan",
    Controller.TARIFF: "tariff_charge_plan",
}

SIGNATURE_KEYS: Mapping[Controller, frozenset[str]] = {
    Controller.RCE: frozenset(
        {"planned_export_kwh", "maximum_export_power_kw"}
    ),
    Controller.RCEM: frozenset({"maximum_voltage_v", "prediction_ready"}),
    Controller.TARIFF: frozenset(
        {"planned_grid_import_kwh", "current_action"}
    ),
}

DETAIL_LIST_KEYS = frozenset(
    {
        "data_quality_issues",
        "missing_entities",
        "planned_slots",
        "risk_windows",
        "risk_window_details",
    }
)

COMMON_KEYS = frozenset(
    {
        "status_code",
        "result_current",
        "recalculation_pending",
        "input_revision",
        "captured_input_revision",
        "pending_input_revision",
        "plan_revision",
        "profile_revision",
        "joint_plan_revision",
        "joint_profile_revision",
        "price_provider",
        "pstryk_blocker",
        "full_plan_solver_calls",
        "last_full_plan_at",
        "last_full_plan_trigger",
        "missing_entities",
        "plan_is_preview",
        "automatic_charge_enabled",
        "automatic_discharge_enabled",
        "enabled",
        "shadow_mode",
        "action",
        "current_action",
        "current_slot_planned",
        "current_run_start_eligible",
        "current_run_continue_eligible",
        "current_slot_start_eligible",
        "current_slot_continue_eligible",
    }
)

METRIC_PREFIXES: Mapping[Controller, tuple[str, ...]] = {
    Controller.RCE: (
        "available_",
        "base_reserve_",
        "bms_",
        "control_reserve_",
        "current_",
        "data_quality_",
        "day3_",
        "effective_",
        "ending_",
        "export_",
        "forecast_",
        "gcf_",
        "gross_",
        "history_",
        "load_",
        "pv_charge_delay_",
        "maximum_",
        "minimum_",
        "net_",
        "physical_",
        "planned_",
        "protected_",
        "rce_",
        "requested_",
        "system_power_",
        "soc_",
        "terminal_",
    ),
    Controller.RCEM: (
        "absorbable_",
        "actuator_",
        "available_",
        "battery_",
        "bms_",
        "charge_",
        "creatable_",
        "data_",
        "effective_",
        "emergency_",
        "estimated_",
        "expected_",
        "export_",
        "filtered_",
        "forecast_",
        "gcf_",
        "headroom_",
        "historical_",
        "history_",
        "live_",
        "load_",
        "maximum_",
        "minutes_",
        "next_",
        "planned_",
        "pre_discharge_",
        "prediction_",
        "protected_",
        "pv_",
        "recommended_",
        "required_",
        "reserve_",
        "risk_",
        "rolling_",
        "selected_",
        "stress_",
        "system_power_",
        "target_",
        "unabsorbed_",
        "unavoidable_",
        "voltage_",
    ),
    Controller.TARIFF: (
        "automation_",
        "automatic_",
        "base_reserve_",
        "battery_",
        "baseline_",
        "bms_",
        "charge_power_",
        "control_",
        "current_",
        "effective_",
        "estimated_",
        "ending_",
        "feedback_",
        "forecast_",
        "hard_reserve_",
        "history_",
        "load_",
        "model_input_",
        "optimized_",
        "planned_",
        "planning_",
        "price_",
        "requested_",
        "reserve_",
        "self_use_",
        "soc_",
        "savings_",
        "target_",
        "tariff_",
        "terminal_",
    ),
}

RELEVANT_HISTORY_PARTS = (
    "rce",
    "rcm",
    "tariff",
    "ems",
    "balanc",
    "sale_block",
    "discharge_cycle",
    "charge_cycle",
)

# These entities are current execution evidence even though their object IDs
# do not contain one of the planner/history family names above.  Keep this an
# explicit allowlist so arbitrary Hoymiles telemetry is not copied into the
# normalized output by accident.
CONTEXT_ENTITY_SUFFIXES = (
    "hoymiles_direct_register_execution_ready",
    "hoymiles_ems_hardware_mode",
    "hoymiles_hit_battery_max_charge_power_readback",
    "hoymiles_hit_gcf_control_readback_generation",
    "hoymiles_hit_gcf_enable_readback_code",
    "hoymiles_hit_gcf_maximum_export_power_readback",
    "hoymiles_hit_machines_type",
    "hoymiles_hit_number_of_machines_master_and_slave",
    "hoymiles_hit_parallel_aggregate_power_readback_generation",
    "hoymiles_parallel_aggregate_physical_response",
)

AGGREGATE_RESPONSE_ENTITY_ID = (
    "sensor.hoymiles_parallel_aggregate_physical_response"
)
CONTROL_HISTORY_PROFILES = frozenset({
    "automation", "balancing_abort", "balancing_outbox", "balancing_timing",
    "balancing_transaction", "canonical_plan", "owner", "rce_planner",
    "rcm_planner", "supervisor", "timeline", "tariff_planner",
})
AGGREGATE_RESPONSE_EVENT_ATTRIBUTE_KEYS = frozenset(
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


def is_unknown(value: Any) -> bool:
    """Return whether a value is absent/redacted rather than evidence."""
    return value is None or (
        isinstance(value, str) and (
            value.strip().casefold() in UNKNOWN_MARKERS
            or value.strip().casefold().startswith("[redacted")
        )
    )


def strict_bool(value: Any) -> bool | None:
    """Return only genuine JSON booleans."""
    return value if type(value) is bool else None


def finite_number(value: Any) -> float | None:
    """Return a finite number without coercing strings or booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def text_value(value: Any) -> str | None:
    """Return a usable bounded text scalar."""
    if is_unknown(value) or not isinstance(value, str):
        return None
    return value[:2048]


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _time_rank(value: Any) -> float:
    parsed = _parse_time(value)
    if parsed is None:
        return float("-inf")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _json_digest(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _compact_value(value: Any, *, depth: int = 0, max_depth: int = 3) -> Any:
    """Keep diagnostic evidence useful while bounding output size."""
    if depth >= max_depth:
        return "[DEPTH_LIMIT]"
    if value is None or type(value) is bool:
        return value
    if isinstance(value, (int, float)):
        return value if finite_number(value) is not None else None
    if isinstance(value, str):
        return value[:2048]
    if isinstance(value, list):
        return [
            _compact_value(item, depth=depth + 1, max_depth=max_depth)
            for item in value[:200]
        ]
    if isinstance(value, Mapping):
        return {
            str(key)[:128]: _compact_value(child, depth=depth + 1, max_depth=max_depth)
            for key, child in list(value.items())[:200]
        }
    return str(value)[:256]


def _selected_attributes(
    controller: Controller,
    attributes: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, bool], dict[str, float], dict[str, Any]]:
    metrics: dict[str, Any] = {}
    flags: dict[str, bool] = {}
    ages: dict[str, float] = {}
    details: dict[str, Any] = {}
    prefixes = METRIC_PREFIXES[controller]
    for raw_key, raw_value in attributes.items():
        key = str(raw_key)
        if key in DETAIL_LIST_KEYS and isinstance(raw_value, list):
            details[key] = _compact_value(raw_value)
            details[f"{key}_count"] = len(raw_value)
            continue
        if key not in COMMON_KEYS and not key.startswith(prefixes):
            continue
        boolean = strict_bool(raw_value)
        numeric = finite_number(raw_value)
        if boolean is not None:
            flags[key] = boolean
        elif numeric is not None:
            metrics[key] = numeric
            if key.endswith("age_seconds"):
                ages[key] = numeric
        elif not is_unknown(raw_value) and isinstance(raw_value, str):
            metrics[key] = raw_value[:2048]
    return metrics, flags, ages, details


def _catalog_ids(
    reports: Sequence[Any],
) -> dict[Controller, set[str]]:
    result = {controller: set() for controller in CONTROLLER_SUFFIXES}
    for report in reports:
        for row in report.catalog_entities:
            if not isinstance(row, Mapping):
                continue
            translation_key = row.get("translation_key")
            entity_id = row.get("proxy_entity_id")
            if not isinstance(entity_id, str):
                continue
            for controller, expected in CONTROLLER_TRANSLATION_KEYS.items():
                if translation_key == expected:
                    result[controller].add(entity_id)
    return result


def _candidate_confidence(
    controller: Controller,
    entity_id: str,
    attributes: Mapping[str, Any],
    catalog_ids: Mapping[Controller, set[str]],
) -> Confidence | None:
    if entity_id in catalog_ids.get(controller, set()):
        return Confidence.HIGH
    object_id = entity_id.partition(".")[2]
    if any(object_id.endswith(suffix) for suffix in CONTROLLER_SUFFIXES[controller]):
        return Confidence.HIGH
    if SIGNATURE_KEYS[controller].issubset(attributes):
        return Confidence.MEDIUM
    return None


def _planner_candidate_priority(
    controller: Controller,
    entity_id: str,
    catalog_ids: Mapping[Controller, set[str]],
) -> int:
    """Prefer the unsuffixed planner consumed by the managed automations.

    Home Assistant may create ``*_2`` planner entities for a second config
    entry.  They are useful evidence, but the packaged scheduler references the
    canonical unsuffixed entity.  A newer ``*_2`` must therefore never replace
    the actual controlling snapshot.
    """

    canonical = f"sensor.{CONTROLLER_SUFFIXES[controller][0]}"
    if entity_id == canonical:
        return 3
    object_id = entity_id.partition(".")[2]
    if object_id in CONTROLLER_SUFFIXES[controller]:
        return 2
    if entity_id in catalog_ids.get(controller, set()):
        return 1
    return 0


def _newer_snapshot(
    current: tuple[int, str, Mapping[str, Any], Confidence] | None,
    candidate: tuple[int, str, Mapping[str, Any], Confidence],
) -> tuple[int, str, Mapping[str, Any], Confidence]:
    if current is None:
        return candidate
    current_rank = (
        _time_rank(current[2].get("last_updated")),
        current[1],
        _json_digest(current[2]),
    )
    candidate_rank = (
        _time_rank(candidate[2].get("last_updated")),
        candidate[1],
        _json_digest(candidate[2]),
    )
    return candidate if candidate_rank > current_rank else current


def extract_archive_evidence(
    archive: LoadedDiagnosticArchive,
) -> tuple[
    tuple[ControllerObservation, ...],
    Mapping[str, Mapping[str, Any]],
    tuple[Mapping[str, Any], ...],
    tuple[Mapping[str, Any], ...],
]:
    """Return deduplicated planner snapshots, context, history and issues."""
    catalog_ids = _catalog_ids(archive.reports)
    selected: dict[
        Controller, tuple[int, str, Mapping[str, Any], Confidence] | None
    ] = {controller: None for controller in CONTROLLER_SUFFIXES}
    candidate_digests: dict[Controller, set[str]] = {
        controller: set() for controller in CONTROLLER_SUFFIXES
    }
    context: dict[str, Mapping[str, Any]] = {}
    events_by_key: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    issues: list[Mapping[str, Any]] = []

    for report in archive.reports:
        for entity_id, raw_snapshot in report.managed_state_snapshot.items():
            if not isinstance(entity_id, str) or not isinstance(raw_snapshot, Mapping):
                continue
            attributes = raw_snapshot.get("attributes")
            attributes = attributes if isinstance(attributes, Mapping) else {}
            for controller in CONTROLLER_SUFFIXES:
                confidence = _candidate_confidence(
                    controller,
                    entity_id,
                    attributes,
                    catalog_ids,
                )
                if confidence is None:
                    continue
                candidate = (
                    report.report_index,
                    entity_id,
                    raw_snapshot,
                    confidence,
                )
                current = selected[controller]
                candidate_priority = _planner_candidate_priority(
                    controller, entity_id, catalog_ids
                )
                current_priority = (
                    _planner_candidate_priority(
                        controller, current[1], catalog_ids
                    )
                    if current is not None
                    else -1
                )
                if candidate_priority > current_priority:
                    selected[controller] = candidate
                elif candidate_priority == current_priority:
                    selected[controller] = _newer_snapshot(current, candidate)
                candidate_digests[controller].add(_json_digest(raw_snapshot))

            if "hoymiles" in entity_id and (
                any(part in entity_id for part in RELEVANT_HISTORY_PARTS)
                or entity_id.endswith(CONTEXT_ENTITY_SUFFIXES)
            ):
                current = context.get(entity_id)
                if current is None:
                    context[entity_id] = raw_snapshot
                else:
                    chosen = _newer_snapshot(
                        (0, entity_id, current, Confidence.HIGH),
                        (0, entity_id, raw_snapshot, Confidence.HIGH),
                    )
                    context[entity_id] = chosen[2]

        history = report.control_history
        entities = history.get("entities") if isinstance(history, Mapping) else None
        if isinstance(entities, Mapping):
            history_start = _parse_time(history.get("start"))
            history_end = _parse_time(history.get("end"))
            if history_start is not None and history_start.tzinfo is None:
                history_start = history_start.replace(tzinfo=timezone.utc)
            if history_end is not None and history_end.tzinfo is None:
                history_end = history_end.replace(tzinfo=timezone.utc)
            bounded_history = bool(
                history_start is not None
                and history_end is not None
                and history_end > history_start
            )
            for entity_id, raw_items in entities.items():
                if not isinstance(entity_id, str) or not isinstance(raw_items, list):
                    continue
                if not (
                    any(part in entity_id for part in RELEVANT_HISTORY_PARTS)
                    or entity_id.endswith(CONTEXT_ENTITY_SUFFIXES)
                ):
                    continue
                normalized_items: list[
                    tuple[datetime, Mapping[str, Any], Any]
                ] = []
                for item in raw_items:
                    if not isinstance(item, Mapping):
                        continue
                    timestamp = item.get("last_updated") or item.get("last_changed")
                    state = item.get("state")
                    if not isinstance(timestamp, str) or is_unknown(state):
                        continue
                    parsed = _parse_time(timestamp)
                    if parsed is None:
                        continue
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    normalized_items.append((parsed, item, state))

                selected_items: list[
                    tuple[datetime, Mapping[str, Any], Any, bool]
                ] = []
                if bounded_history:
                    assert history_start is not None
                    assert history_end is not None
                    before = [
                        candidate
                        for candidate in normalized_items
                        if candidate[0] < history_start
                    ]
                    if before:
                        _source_at, item, state = max(
                            before, key=lambda candidate: candidate[0]
                        )
                        # Recorder's include-start state may retain its old
                        # last_changed timestamp.  Clamp it to the exact query
                        # boundary so it initializes this window without
                        # inventing activity in the unobserved gap.
                        selected_items.append(
                            (history_start, item, state, True)
                        )
                    selected_items.extend(
                        (parsed, item, state, False)
                        for parsed, item, state in normalized_items
                        if history_start <= parsed <= history_end
                    )
                else:
                    selected_items.extend(
                        (parsed, item, state, False)
                        for parsed, item, state in normalized_items
                    )

                for parsed, item, state, boundary_seed in selected_items:
                    event_timestamp = parsed.isoformat()
                    key = (entity_id, event_timestamp, str(state))
                    event: dict[str, Any] = {
                        "installation_key": archive.metadata.installation_key,
                        "archive_key": archive.metadata.archive_key,
                        "integration_version": report.integration_version,
                        "entity_id": entity_id,
                        "state": _compact_value(state),
                        "last_changed": (
                            event_timestamp
                            if boundary_seed
                            else item.get("last_changed")
                        ),
                        "last_updated": (
                            event_timestamp
                            if boundary_seed
                            else item.get("last_updated")
                        ),
                        "history_boundary_seed": boundary_seed,
                    }
                    if entity_id == AGGREGATE_RESPONSE_ENTITY_ID:
                        raw_attributes = item.get("attributes")
                        if isinstance(raw_attributes, Mapping):
                            event["attributes"] = {
                                str(attribute): _compact_value(value)
                                for attribute, value in raw_attributes.items()
                                if str(attribute)
                                in AGGREGATE_RESPONSE_EVENT_ATTRIBUTE_KEYS
                            }
                    elif (isinstance(item.get("attribute_profile"), str)
                          and item["attribute_profile"] in CONTROL_HISTORY_PROFILES):
                        # Preserve bounded event evidence from the exporter,
                        # including frozen STOP inputs. Unknown schemas remain
                        # state-only; current snapshots cannot fill their gaps.
                        event["event_schema_version"] = item.get("event_schema_version")
                        event["attribute_profile"] = item["attribute_profile"]
                        raw_attributes = item.get("attributes")
                        available = (
                            type(item.get("event_schema_version")) is int
                            and item["event_schema_version"] == 1
                            and item.get("attributes_available") is True
                            and isinstance(raw_attributes, Mapping)
                        )
                        event["attributes_available"] = available
                        if available:
                            event["attributes"] = _compact_value(
                                sanitize_diagnostic_value(raw_attributes), max_depth=10
                            )
                    if key not in events_by_key or prefer_control_event(event, events_by_key[key]):
                        events_by_key[key] = event

    observations: list[ControllerObservation] = []
    owner_snapshot = next(
        (
            snapshot
            for entity_id, snapshot in context.items()
            if entity_id.endswith("hoymiles_ems_control_owner")
        ),
        {},
    )
    owner_attributes = owner_snapshot.get("attributes", {})
    owner_code = (
        text_value(owner_attributes.get("owner_code"))
        if isinstance(owner_attributes, Mapping)
        else None
    )
    active_by_controller = _active_controller_states(context)

    for controller, candidate in selected.items():
        if candidate is None:
            continue
        report_index, entity_id, snapshot, confidence = candidate
        attributes = snapshot.get("attributes")
        attributes = attributes if isinstance(attributes, Mapping) else {}
        metrics, flags, ages, details = _selected_attributes(
            controller,
            attributes,
        )
        status_code = text_value(attributes.get("status_code"))
        action = text_value(
            attributes.get("current_action")
            if controller is Controller.TARIFF
            else attributes.get("action")
        )
        planned = strict_bool(
            attributes.get("current_slot_planned")
            if controller is not Controller.RCEM
            else attributes.get("pre_discharge_ready")
        )
        enabled = strict_bool(
            attributes.get("automatic_discharge_enabled")
            if controller is Controller.RCE
            else attributes.get("automatic_charge_enabled")
            if controller is Controller.TARIFF
            else attributes.get("enabled")
        )
        block_reason = text_value(
            attributes.get("prediction_block_reason")
            if controller is Controller.RCEM
            else attributes.get("control_input_block_reason")
        )
        suppression_reason = text_value(
            attributes.get("current_slot_suppression_reason")
            if controller is Controller.RCE
            else attributes.get("current_run_suppression_reason")
        )
        continue_reason = text_value(
            attributes.get("current_slot_continue_reason")
            if controller is Controller.RCE
            else attributes.get("current_run_continue_reason")
        )
        observations.append(
            ControllerObservation(
                archive_key=archive.metadata.archive_key,
                installation_key=archive.metadata.installation_key,
                report_index=report_index,
                controller=controller,
                observed_at=archive.metadata.generated_at,
                entity_id=entity_id,
                state=text_value(snapshot.get("state")),
                last_changed=_parse_time(snapshot.get("last_changed")),
                last_updated=_parse_time(snapshot.get("last_updated")),
                status_code=status_code,
                action=action,
                result_current=strict_bool(attributes.get("result_current")),
                planned=planned,
                enabled=enabled,
                active=active_by_controller.get(controller),
                owner_code=owner_code,
                suppression_reason=suppression_reason,
                continue_reason=continue_reason,
                block_reason=block_reason,
                freshness={
                    key: value
                    for key, value in flags.items()
                    if "fresh" in key or key.endswith("available")
                },
                ages_seconds=ages,
                metrics=metrics,
                flags=flags,
                coverage=_controller_coverage(
                    controller,
                    context,
                    attributes,
                ),
                details={**details, "source_confidence": confidence.value},
            )
        )
        if len(candidate_digests[controller]) > 1:
            issues.append(
                {
                    "code": "ARCHIVE_MULTI_ENTRY_SNAPSHOT_DIVERGENCE",
                    "controller": controller.value,
                    "entity_id": entity_id,
                    "candidate_count": len(candidate_digests[controller]),
                    "selected_report_index": report_index,
                }
            )

    events = tuple(
        events_by_key[key]
        for key in sorted(
            events_by_key,
            key=lambda item: (_time_rank(item[1]), item[0], item[2]),
        )
    )
    return tuple(observations), context, events, tuple(issues)


def _context_state(
    context: Mapping[str, Mapping[str, Any]],
    suffix: str,
) -> str | None:
    for entity_id, snapshot in context.items():
        if entity_id.endswith(suffix):
            return text_value(snapshot.get("state"))
    return None


def _active_controller_states(
    context: Mapping[str, Mapping[str, Any]],
) -> dict[Controller, bool | None]:
    rce = _context_state(context, "hoymiles_rce_discharge_active")
    tariff = _context_state(context, "hoymiles_tariff_charge_active")
    rcm_values = [
        _context_state(context, suffix)
        for suffix in (
            "hoymiles_rcm_active",
            "hoymiles_rcm_export_control_active",
            "hoymiles_rcm_pre_discharge_active",
        )
    ]
    return {
        Controller.RCE: None if rce is None else rce == "on",
        Controller.TARIFF: None if tariff is None else tariff == "on",
        Controller.RCEM: (
            None
            if all(value is None for value in rcm_values)
            else any(value == "on" for value in rcm_values)
        ),
    }


def _context_value_status(
    context: Mapping[str, Mapping[str, Any]],
    suffix: str,
) -> ValueStatus:
    for entity_id, snapshot in context.items():
        if entity_id.endswith(suffix):
            return (
                ValueStatus.PRESENT
                if text_value(snapshot.get("state")) is not None
                else ValueStatus.UNKNOWN
            )
    return ValueStatus.MISSING


def _context_entity_exists(
    context: Mapping[str, Mapping[str, Any]],
    suffix: str,
) -> bool:
    return any(entity_id.endswith(suffix) for entity_id in context)


def _attribute_value_status(
    attributes: Mapping[str, Any],
    key: str,
) -> ValueStatus:
    if key not in attributes:
        return ValueStatus.MISSING
    value = attributes.get(key)
    if isinstance(value, str) and value.strip().casefold() == "[redacted]":
        return ValueStatus.REDACTED
    return ValueStatus.PRESENT if not is_unknown(value) else ValueStatus.UNKNOWN


def _controller_coverage(
    controller: Controller,
    context: Mapping[str, Mapping[str, Any]],
    attributes: Mapping[str, Any],
) -> Mapping[str, ValueStatus]:
    active_suffixes = {
        Controller.RCE: ("hoymiles_rce_discharge_active",),
        Controller.TARIFF: ("hoymiles_tariff_charge_active",),
        Controller.RCEM: (
            "hoymiles_rcm_active",
            "hoymiles_rcm_export_control_active",
            "hoymiles_rcm_pre_discharge_active",
        ),
    }
    active_statuses = [
        _context_value_status(context, suffix)
        for suffix in active_suffixes[controller]
    ]
    if any(status is ValueStatus.PRESENT for status in active_statuses):
        active_status = ValueStatus.PRESENT
    elif any(status is ValueStatus.UNKNOWN for status in active_statuses):
        active_status = ValueStatus.UNKNOWN
    else:
        active_status = ValueStatus.MISSING
    coverage: dict[str, ValueStatus] = {
        "active_helper": active_status,
        "owner": (
            ValueStatus.PRESENT
            if text_value(
                context_attributes(
                    context, "hoymiles_ems_control_owner"
                ).get("owner_code")
            )
            is not None
            else (
                ValueStatus.UNKNOWN
                if _context_entity_exists(
                    context, "hoymiles_ems_control_owner"
                )
                else ValueStatus.MISSING
            )
        ),
        "result_current": _attribute_value_status(
            attributes, "result_current"
        ),
    }
    if controller in {Controller.RCE, Controller.TARIFF}:
        coverage["ems_execution_ready"] = _context_value_status(
            context, "hoymiles_ems_execution_ready"
        )
    else:
        coverage["direct_register_execution_ready"] = _context_value_status(
            context, "hoymiles_direct_register_execution_ready"
        )
        coverage["ems_execution_ready"] = _context_value_status(
            context, "hoymiles_ems_execution_ready"
        )
    return coverage


def context_state(
    context: Mapping[str, Mapping[str, Any]],
    suffix: str,
) -> str | None:
    """Public suffix lookup used by cross-controller rules."""
    return _context_state(context, suffix)


def context_attributes(
    context: Mapping[str, Mapping[str, Any]],
    suffix: str,
) -> Mapping[str, Any]:
    """Return attributes for a current context entity."""
    for entity_id, snapshot in context.items():
        if entity_id.endswith(suffix):
            value = snapshot.get("attributes")
            return value if isinstance(value, Mapping) else {}
    return {}


def prefer_control_event(candidate: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Do not replace a rich Recorder event with its state-only duplicate."""
    def rank(event: Mapping[str, Any]) -> int:
        attrs = event.get("attributes")
        return len(attrs) if isinstance(attrs, Mapping) else 0

    return rank(candidate) > rank(current) or (
        rank(candidate) == rank(current)
        and str(candidate.get("archive_key", "")) < str(current.get("archive_key", ""))
    )


def merge_events(
    archives_events: Iterable[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    """Deduplicate overlapping 24-hour histories across archives."""
    merged: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for event in archives_events:
        key = (
            str(event.get("installation_key", "")),
            str(event.get("entity_id", "")),
            str(event.get("last_updated") or event.get("last_changed") or ""),
            str(event.get("state", "")),
        )
        current = merged.get(key)
        if current is None or prefer_control_event(event, current):
            merged[key] = event
    return tuple(
        sorted(
            merged.values(),
            key=lambda event: (
                str(event.get("installation_key", "")),
                _time_rank(
                    event.get("last_updated") or event.get("last_changed")
                ),
                str(event.get("entity_id", "")),
                str(event.get("state", "")),
            ),
        )
    )
