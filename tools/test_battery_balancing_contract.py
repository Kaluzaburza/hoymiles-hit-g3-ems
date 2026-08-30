"""Production-derived offline contract for v1.5.8 battery balancing.

Lifecycle, restart, concurrency and notification checks inspect the exact
scheduler YAML.  There is intentionally no duplicate lifecycle model here.
Only power and SOC arithmetic remain as small pure functions.  The exact
Home Assistant 2026.8.2 behavior is covered by the companion runtime suite.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import math
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
PL_SCHEDULER = (ROOT / "custom_components" / "hoymiles_hit_modbus" /
                "resources" / "home_assistant" / "pl" /
                "hoymiles_ems_scheduler.yaml")
EN_SCHEDULER = PL_SCHEDULER.parents[1] / "en" / PL_SCHEDULER.name
VALIDATOR = ROOT / "tools" / "validate_release.py"
RUNTIME = ROOT / "tools" / "test_battery_balancing_ha_runtime.py"
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"
CATALOG = ROOT / "custom_components" / "hoymiles_hit_modbus" / "entity_catalog.json"
README = ROOT / "README.md"
README_PL = ROOT / "README.pl.md"
RELEASING = ROOT / "RELEASING.md"

TARGET_KW = 0.4
SLOW_SOC = 95.0
HOLD_SOC = 99.9
RATED_VALUES = {5.0, 8.0, 10.0, 12.0, 15.0, 20.0}
PHYSICAL_HELPERS = {
    "script.hoymiles_verified_set_ems_maximum_charge_power",
    "script.hoymiles_verified_set_ems_force_charge_soc",
    "script.hoymiles_verified_set_ems_mode",
}
TRANSACTION_STATES = {
    "REQUESTED", "OWNER_ACQUIRED", "SNAPSHOT_VALID", "APPLYING",
    "OPERATIONAL", "SLOW", "HOLD_ARMING", "HOLDING", "RESTORING",
    "RESTORED", "TERMINAL", "ABORT_REQUESTED", "RESTORE_FAILED",
    "RECOVERY_REQUIRED", "NOTIFICATION_PENDING",
}


@dataclass(frozen=True, slots=True)
class PowerResult:
    percent: float
    system_kw: float
    requested_budget_kw: float
    bms_budget_kw: float
    net_battery_kw: float


@dataclass(slots=True)
class Audit:
    checks: int = 0

    def require(self, condition: bool, message: str) -> None:
        self.checks += 1
        if not condition:
            raise AssertionError(message)

    def contains(self, text: str, markers: tuple[str, ...], scope: str) -> None:
        for marker in markers:
            self.require(marker in text, f"{scope}: missing {marker!r}")


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _topology_valid(machine_type: Any, count: Any) -> bool:
    kind, machines = _number(machine_type), _number(count)
    if kind is None or machines is None or kind != int(kind) or machines != int(machines):
        return False
    return (int(kind), int(machines)) == (0, 1) or (
        int(kind) == 1 and 2 <= int(machines) <= 10
    )


def calculate_slow_power(
    *, rated_each_kw: Any, machine_type: Any, machine_count: Any,
    load_kw: Any, bms_battery_kw: Any, mode: str,
    load_fresh: bool = True, bms_fresh: bool = True,
    topology_fresh: bool = True, readback_fresh: bool = True,
) -> PowerResult | None:
    """Return one safe 4304 percentage, or fail closed with ``None``."""
    rated, count = _number(rated_each_kw), _number(machine_count)
    load, bms = _number(load_kw), _number(bms_battery_kw)
    if (rated not in RATED_VALUES or not _topology_valid(machine_type, machine_count)
            or count is None or load is None or load < 0 or bms is None or bms <= 0
            or mode not in {"self_use", "grid_charge"} or not load_fresh
            or not bms_fresh or not topology_fresh or not readback_fresh):
        return None
    system_kw = rated * count
    requested = TARGET_KW + (load if mode == "grid_charge" else 0.0)
    bms_budget = bms + (load if mode == "grid_charge" else 0.0)
    raw = min(requested / system_kw * 100.0, bms_budget / system_kw * 100.0, 100.0)
    percent = math.floor(raw * 10.0 + 1e-12) / 10.0
    if percent < 0.1:
        return None
    net = percent / 100.0 * system_kw - (load if mode == "grid_charge" else 0.0)
    return PowerResult(round(percent, 1), system_kw, requested, bms_budget, net)


def phase_after_soc(phase: str, soc: float, *, ack: bool = True) -> tuple[str, bool, bool]:
    """Return phase, hold-started and hold-cancelled for SOC boundaries."""
    latched = phase in {"slow", "holding"}
    if phase == "holding" and soc < HOLD_SOC:
        return "slow", False, True
    if phase == "holding":
        return "holding", False, False
    if soc >= HOLD_SOC and (latched or soc >= SLOW_SOC):
        return ("holding", True, False) if ack else ("slow", False, False)
    return ("slow", False, False) if latched or soc >= SLOW_SOC else ("pv", False, False)


def _load_package(source: str) -> dict[str, Any]:
    package = yaml.safe_load(source)
    if not isinstance(package, dict):
        raise AssertionError("Scheduler YAML is not a mapping")
    return package


def _block(source: str, start: str, end: str | None = None) -> str:
    begin = source.find(start)
    if begin < 0:
        raise AssertionError(f"Missing block start: {start!r}")
    if end is None:
        return source[begin:]
    finish = source.find(end, begin + len(start))
    if finish < 0:
        raise AssertionError(f"Missing block end: {end!r}")
    return source[begin:finish]


def _script_block(source: str, script_id: str) -> str:
    match = re.search(
        rf"(?ms)^  {re.escape(script_id)}:\n.*?(?=^  [a-zA-Z0-9_]+:\n|^automation:\n)",
        source,
    )
    if match is None:
        raise AssertionError(f"Missing script block: {script_id}")
    return match.group(0)


def _automation_block(source: str, automation_id: str, next_id: str) -> str:
    return _block(source, f"  - id: {automation_id}", f"  - id: {next_id}")


def _walk(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def _action_counts(value: Any) -> Counter[str]:
    return Counter(item["action"] for item in _walk(value)
                   if isinstance(item.get("action"), str))


def _find_unique_id(package: dict[str, Any], unique_id: str) -> dict[str, Any]:
    for item in _walk(package.get("template", [])):
        if item.get("unique_id") == unique_id:
            return item
    raise AssertionError(f"Missing template object: {unique_id}")


def _assert_order(audit: Audit, text: str, tokens: tuple[str, ...], scope: str) -> None:
    cursor = -1
    for token in tokens:
        found = text.find(token, cursor + 1)
        audit.require(found >= 0, f"{scope}: missing ordered token {token!r}")
        audit.require(found > cursor, f"{scope}: bad order at {token!r}")
        cursor = found


def _maximum_lifecycle_length() -> int:
    reasons = (
        "none", "completed", "user_disabled", "off_grid", "bms_limit_invalid",
        "bms_data_invalid", "load_data_invalid", "bms_fault", "inverter_fault",
        "invalid_topology", "control_conflict", "ownership_lost",
        "data_stale_timeout", "clock_anomaly", "communication_failure",
        "max_charge_power_ack_failed", "force_soc_ack_failed", "mode_ack_failed",
        "watchdog_timeout", "snapshot_changed_before_transaction",
        "lifecycle_invalid", "legacy_untrusted", "restore_ack_failed",
        "outbox_write_failed", "physical_transaction_failure",
        "unknown_internal_error",
    )
    events = ("NONE", "STARTED", "COMPLETED", "ABORTED", "FAILED",
              "RESTORE_FAILED", "RECOVERY_REQUIRED")
    records = (
        "b2|c2147483647|" + state
        + "|balancing|2147483647|1|c2147483647|grid_discharge|100.0|100.0|"
          "16000000|16000000|4102444800000|1|" + reason
        + "|1|4102444800000|" + event
        for state in TRANSACTION_STATES | {"IDLE"} for reason in reasons for event in events
    )
    return max(map(len, records))


def _assert_guarded_apply_helpers(audit: Audit, apply_path: str) -> None:
    pattern = re.compile(
        r"- action: (?:>-\n\s+)?(script\.hoymiles_verified_set_ems_(?:maximum_charge_power|force_charge_soc|mode))"
    )
    matches = list(pattern.finditer(apply_path))
    audit.require(len(matches) == 10, "Apply path must retain ten bounded helper sites")
    for match in matches:
        before = apply_path[max(0, match.start() - 2500):match.start()]
        after = apply_path[match.end():match.end() + 5000]
        audit.require("binary_sensor.hoymiles_battery_balancing_apply_authorized" in before,
                      f"Missing pre-helper gate for {match.group(1)}")
        audit.require("hoymiles_battery_balancing_request_abort" in after
                      or "final_transaction_ack" in after,
                      f"Missing post-helper/tail gate for {match.group(1)}")


def assert_production_contract(source: str) -> int:
    """Derive lifecycle, restart and physical-order facts from exact YAML."""
    audit = Audit()
    package = _load_package(source)
    scripts = package.get("script", {})
    automations = {item.get("id"): item for item in package.get("automation", [])
                   if isinstance(item, dict)}
    audit.require(isinstance(scripts, dict), "Scheduler scripts are not a mapping")
    audit.require({
        "hoymiles_battery_balancing_write_record",
        "hoymiles_battery_balancing_write_timing",
        "hoymiles_battery_balancing_write_abort_request",
        "hoymiles_battery_balancing_write_outbox",
        "hoymiles_battery_balancing_transaction_worker",
        "hoymiles_battery_balancing_request_abort",
        "hoymiles_battery_balancing_soft_gap_guard",
        "hoymiles_battery_balancing_hold_guard",
        "hoymiles_battery_balancing_enter_recovery",
        "hoymiles_battery_balancing_notification_dispatcher",
        "hoymiles_battery_balancing_notification_provider_attempt",
        "hoymiles_battery_balancing_notification_attempt_timeout",
        "hoymiles_battery_balancing_recover_notification_leases",
    } <= scripts.keys(), "Transactional scripts missing")
    audit.require({
        "hoymiles_battery_balancing_hard_stop_capture",
        "hoymiles_battery_balancing_off_grid_hard_stop_capture",
        "hoymiles_battery_balancing_p95_hard_stop_capture",
        "hoymiles_battery_balancing_p90_hard_stop_capture",
        "hoymiles_battery_balancing_p80_hard_stop_capture",
        "hoymiles_battery_balancing_p75_hard_stop_capture",
        "hoymiles_battery_balancing_p60_hard_stop_capture",
        "hoymiles_battery_balancing_recovery_hard_stop_capture",
        "hoymiles_battery_balancing_control",
        "hoymiles_battery_balancing_notification_delivery",
    } <= automations.keys(), "Transactional automations missing")

    transaction = _find_unique_id(package, "hoymiles_battery_balancing_transaction")
    parser_state = str(transaction.get("state", ""))
    parser_attrs = yaml.safe_dump(transaction.get("attributes", {}), sort_keys=True)
    audit.contains(parser_state, (
        "p | count != 18", ") | length) > 255", "ascii.valid", "p[0] == 'b2'",
        "p[5] == '1' and p[6] == p[1]", "p[12] | int(0)) > 0",
        "p[13] in ['0', '1']", "p[14] in reasons", "p[17] in events",
        "conditional_states", "conditional_commit_current",
        "effective_state", "'101': 'OPERATIONAL'", "'402': 'HOLDING'",
        "binary_sensor.hoymiles_battery_balancing_control_data_ready",
        "else 'INVALID'",
    ), "b2 parser")
    audit.require(
        "if conditional_commit_current else p[2]" in parser_state,
        "Conditional steady token bypasses the live parser guard",
    )
    audit.require(
        "and conditional_timing_ok" in parser_state,
        "Conditional HOLD token bypasses the matching timing record",
    )
    for state in TRANSACTION_STATES:
        audit.require(state in parser_state, f"b2 parser omits {state}")
    audit.contains(parser_attrs, (
        "p | count == 7", "legacy_untrusted", "lifecycle_invalid",
        "current_physical_mode", "current_physical_4303", "current_physical_4304",
        "current_ems_generation", "current_topology_generation",
    ), "b2 attributes")
    parser_region = _block(source,
        "# Canonical parser for the exact 18-field b2 lifecycle schema.",
        "# Canonical parser for restart-durable soft-gap")
    audit.require(source.count("battery_balancing_lifecycle').split('|')")
                  == parser_region.count("battery_balancing_lifecycle').split('|')"),
                  "Lifecycle reader bypasses canonical parser")

    serializer = _script_block(source, "hoymiles_battery_balancing_write_record")
    audit.contains(serializer, (
        "b2|{{ record_cycle }}|{{ record_persisted_state }}",
        "serialized_record | length <= 255",
        "serialized_ascii_valid", "record_snapshot_cycle == record_cycle",
        "snapshot_trusted", "record_write_started", "record_state in allowed_states",
        "record_guard_steady_commit", "record_guarded_owner_generation",
        "record_current_required_mode", "record_steady_commit_authorized",
        "record_persisted_state", "record_commit_token",
        "'OPERATIONAL': 100", "'HOLDING': 400",
        "if record_guard_steady_commit and phase_code > 0",
        "binary_sensor.hoymiles_battery_balancing_apply_authorized",
        "record_valid and record_steady_commit_authorized",
        "continue_on_timeout: false",
    ), "b2 serializer")
    audit.require(
        "'APPLYING' if record_guard_steady_commit else record_state"
        in serializer,
        "Guarded steady writer can persist a raw accepted phase",
    )
    audit.require(
        'value: "{{ record_persisted_state }}"' in serializer,
        "Compatibility mirror can persist a stale accepted phase",
    )
    _assert_order(audit, serializer, (
        "record_current_required_mode", "record_persisted_state",
        "record_commit_token", "record_steady_commit_authorized",
        "record_valid and record_steady_commit_authorized",
        "action: input_text.set_value",
    ), "inner steady lifecycle-write boundary")
    audit.require(source.count(
        "entity_id: input_text.hoymiles_battery_balancing_lifecycle") == 1,
        "b2 serializer is not the sole lifecycle writer")
    audit.require(_maximum_lifecycle_length() == 201, "Unexpected max b2 fixture")
    audit.require(_maximum_lifecycle_length() <= 255, "b2 may exceed input_text")

    power_region = _block(source,
        '# Single runtime source of truth for the complete parallel system.',
        '# Canonical parser for the exact 18-field b2 lifecycle schema.')
    audit.require(source.count("BALANCING_SLOW_TARGET_KW = 0.4") == 1,
                  "0.4 kW aggregate target is not singular")
    audit.contains(power_region, (
        "target / system_power * 100", "(load_power + target) / system_power * 100",
        "direct_safe, 100] | min", "grid_safe, 100] | min",
        "round(0, 'floor')", "if direct_raw >= 0.1 else 0",
        "if grid_raw >= 0.1 else 0", "scope: aggregate_parallel_system",
        "self_use_semantics: direct_battery_charge_cap",
        "grid_charge_semantics: common_ac_budget_including_load",
    ), "power source")
    audit.require("round(0, 'ceil')" not in power_region,
                  "Power is rounded above a safe limit")
    audit.require("target * machines" not in power_region,
                  "0.4 kW target is multiplied by machine count")
    audit.require("| float(20)" not in power_region,
                  "Positive fallback grants power authority")
    audit.require(power_region.count("target / system_power * 100") == 2,
                  "Self-Use slow target formula count changed")
    audit.require(power_region.count("(load_power + target) / system_power") == 2,
                  "Grid Charge LOAD compensation count changed")
    audit.require(power_region.count("if direct_raw >= 0.1 else 0 %}") == 2,
                  "Sub-step no-write floor changed")

    timing = _script_block(source, "hoymiles_battery_balancing_write_timing")
    abort_writer = _script_block(source, "hoymiles_battery_balancing_write_abort_request")
    outbox_writer = _script_block(source, "hoymiles_battery_balancing_write_outbox")
    audit.contains(timing, (
        "t1|{{ timing_cycle }}|{{ timing_gap_generation }}",
        "timing_gap_start | int(0)) + 60000",
        "timing_kind in ['NONE', 'GAP', 'HOLD_ARMING', 'HOLDING']",
        "serialized_timing | length <= 160",
        "hold_soc_at_serializer", "hold_soc_age_at_serializer",
        "hold_mode_at_serializer", "hold_arm_authorized",
        "(hold_soc_at_serializer | float(101)) <= 100",
        "(hold_soc_age_at_serializer | float(999)) >= -5",
        "(hold_soc_age_at_serializer | float(999)) <= 120",
        "'sensor.hoymiles_battery_balancing_transaction', 'SLOW'",
        "input_boolean.hoymiles_battery_balancing_active",
        "input_boolean.hoymiles_battery_balancing_enabled",
        "sensor.hoymiles_ems_control_owner", "owner_code",
        "sensor.hoymiles_ems_hardware_mode",
        "binary_sensor.hoymiles_battery_balancing_apply_authorized",
        "== (timing_hold_generation | int(-1)) - 1",
        "sensor.hoymiles_battery_balancing_abort_request",
        "and hold_arm_authorized",
    ), "t1 serializer")
    _assert_order(audit, timing, (
        "hold_soc_at_serializer", "hold_arm_authorized",
        "and hold_arm_authorized", "action: input_text.set_value",
    ), "HOLD_ARMING serializer boundary")
    audit.require(timing.count(">= 99.9") == 1,
                  "Serializer-local 99.9% HOLD_ARMING boundary changed")
    audit.require(
        "(hold_soc_age_at_serializer | float(999)) <= 120\n" in timing,
        "Serializer-local SOC freshness ceiling changed",
    )
    audit.contains(abort_writer, (
        "a1|{{ abort_cycle }}|{{ abort_generation }}",
        "abort_state == 'NONE'", "abort_state in ['PENDING', 'CONSUMED']",
        "serialized_abort | length <= 160",
    ), "a1 serializer")
    audit.contains(outbox_writer, (
        "o1|{{ slot_1_event_id }}", "serialized_outbox | length <= 255",
        "'PENDING', 'DELIVERING', 'DELIVERED'",
        "'bb.' ~ slot_1_cycle_id ~ '.' ~ slot_1_kind",
    ), "o1 serializer")
    for suffix in ("timing", "abort_request", "notification_outbox"):
        audit.require(source.count(
            f"entity_id: input_text.hoymiles_battery_balancing_{suffix}") == 1,
            f"Canonical {suffix} writer count changed")

    worker = scripts["hoymiles_battery_balancing_transaction_worker"]
    worker_source = _script_block(source, "hoymiles_battery_balancing_transaction_worker")
    worker_actions = _action_counts(worker)
    audit.require(worker.get("mode") == "queued", "Physical worker is not queued")
    audit.require(worker.get("max") == 100, "Physical worker bound changed")
    audit.require(worker.get("max_exceeded") == "error", "Worker may drop work silently")
    audit.require("modbus." not in worker_source, "Balancing gained direct Modbus")
    expected_physical = {
        "script.hoymiles_verified_set_ems_maximum_charge_power": 5,
        "script.hoymiles_verified_set_ems_force_charge_soc": 2,
        "script.hoymiles_verified_set_ems_mode": 6,
    }
    audit.require({key: worker_actions[key] for key in PHYSICAL_HELPERS}
                  == expected_physical, "Physical helper sites changed")
    for script_id, script in scripts.items():
        if "battery_balancing" not in script_id and script_id != "hoymiles_notify_battery_balancing_lifecycle":
            continue
        if script_id != "hoymiles_battery_balancing_transaction_worker":
            audit.require(not (PHYSICAL_HELPERS & _action_counts(script).keys()),
                          f"Second physical writer: {script_id}")

    recovery = _script_block(source, "hoymiles_battery_balancing_enter_recovery")
    audit.contains(recovery, (
        'transaction_state: "RECOVERY_REQUIRED"', 'snapshot_valid: false',
        'snapshot_cycle_id: "none"', 'snapshot_mode: "unknown"',
        'write_started: true', 'event: "recovery_required"',
    ), "legacy recovery")
    audit.require(not any(helper in recovery for helper in PHYSICAL_HELPERS),
                  "RECOVERY_REQUIRED writes physical state")
    audit.require("input_boolean.turn_off" not in recovery, "Recovery releases owner")

    restore = _block(worker_source,
        "# Restoration or provisional-owner release always wins",
        "# Only the worker creates a monotonic cycle")
    audit.contains(restore, (
        "snapshot_valid') | bool(false)", "snapshot_cycle_id') == entry_cycle",
        "not any_write_started", "entry_state != 'RECOVERY_REQUIRED'",
        "binary_sensor.hoymiles_battery_balancing_restore_authorized",
        "final_restore_ack", "release_abort_generation",
        'transaction_state: "RESTORE_FAILED"', 'reason_code: "restore_ack_failed"',
    ), "restore path")
    trusted_definition = _block(restore, "trusted_snapshot: >-", "any_write_started: >-")
    audit.require("snapshot_valid') | bool(false)" in trusted_definition,
                  "Restore can trust a snapshot without snapshot_valid=true")
    audit.require("if false else 'STARTED'" not in restore,
                  "Restore resurrects a pre-commit notice state")
    audit.require("input_number.hoymiles_battery_balancing_saved_charge_power" not in restore
                  and "input_number.hoymiles_battery_balancing_saved_force_charge_soc" not in restore,
                  "Restore trusts legacy saved input_number values")
    trusted_restore = _block(restore, "# Restore 4304.",
        "default:\n                      - variables:\n                          restore_failure_reason")
    _assert_order(audit, trusted_restore, (
        "script.hoymiles_verified_set_ems_maximum_charge_power",
        "script.hoymiles_verified_set_ems_force_charge_soc",
        "script.hoymiles_verified_set_ems_mode", "final_restore_ack",
        'transaction_state: "RESTORED"', "release_abort_generation",
        "input_boolean.turn_off", 'transaction_state: "NOTIFICATION_PENDING"',
    ), "trusted restore and owner release")

    start = _block(worker_source, "# Only the worker creates a monotonic cycle",
                   "default:\n          # Routine reconcile")
    _assert_order(audit, start, (
        "next_sequence", "input_number.set_value", "next_cycle",
        'transaction_state: "REQUESTED"', "input_boolean.turn_on",
        'transaction_state: "OWNER_ACQUIRED"', "captured_mode", "captured_4303",
        "captured_4304", "captured_ems_generation", "captured_topology_generation",
        'transaction_state: "SNAPSHOT_VALID"', "snapshot_valid: true",
    ), "cycle and snapshot order")
    audit.contains(start, (
        "next_cycle: \"{{ 'c' ~ next_sequence }}\"", "continue_on_timeout: false",
        "owner_code') == 'balancing'", "is_number(captured_4303)",
        "is_number(captured_4304)",
    ), "monotonic start")
    audit.require("as_timestamp(now()) | int }}|pending" not in start,
                  "Wall-clock second used as cycle ID")

    apply_path = _block(worker_source, "# Routine reconcile:")
    _assert_order(audit, apply_path, (
        "first_write_pending", "snapshot_still_equal",
        "snapshot_changed_before_transaction",
        "binary_sensor.hoymiles_battery_balancing_apply_authorized",
        'transaction_state: "APPLYING"', "write_started: true", "delay: 0",
        "snapshot_equal_after_apply_arm",
        "Physical snapshot changed at the APPLYING arm boundary",
        "required_mode_before_apply", "first_mode_request",
        "script.hoymiles_verified_set_ems_mode", "required_mode_after_ack",
        "required_mode_before_power", "required_mode_after_power", "committed_mode",
        "final_transaction_ack",
    ), "apply transaction")
    audit.contains(apply_path, (
        "entry_state in ['SLOW', 'HOLD_ARMING', 'HOLDING']",
        "(soc_now | float(0)) >= 95", "mode_correction_used: false",
        "if slow_latched else", "not mode_correction_used",
        "state_attr(target_sensor, committed_mode ~ '_percent')",
        "not final_transaction_ack", "hoymiles_battery_balancing_request_abort",
        "first_write_pending and not snapshot_still_equal",
        "required_mode_after_ack != first_mode_request",
        "snapshot_ems_generation') | int(16000001)",
        "snapshot_topology_generation') | int(16000001)",
        "== (entry_owner_generation | int(-2))",
        "first_write_pending\n                     and not snapshot_equal_after_apply_arm",
    ), "apply invariants")
    audit.require(
        "# Yield once so every callback fired by the inner input_text service\n"
        "          # boundary becomes visible before the post-arm physical snapshot read.\n"
        "          - delay: 0" in apply_path,
        "Post-APPLYING service boundary no longer yields before physical helpers",
    )
    snapshot_guard = _block(
        apply_path, "snapshot_still_equal: >-", "first_write_pending and not snapshot_still_equal"
    )
    audit.contains(snapshot_guard, (
        "sensor.hoymiles_hit_ems_control_readback_generation",
        "snapshot_ems_generation') | int(16000001)",
        "sensor.hoymiles_hit_parallel_topology_readback_generation",
        "snapshot_topology_generation') | int(16000001)",
        "== (entry_owner_generation | int(-2))",
    ), "exact snapshot generations")
    audit.require(snapshot_guard.count("| int(-1)) == (state_attr(") == 2,
                  "Snapshot generations are not exact equality checks")
    audit.require("| int(-1)) >= (state_attr(" not in snapshot_guard,
                  "Snapshot generation drift is accepted")
    post_arm_snapshot_guard = _block(
        apply_path,
        "snapshot_equal_after_apply_arm: >-",
        "Physical snapshot changed at the APPLYING arm boundary",
    )
    audit.contains(post_arm_snapshot_guard, (
        "snapshot_valid", "snapshot_cycle_id", "snapshot_mode",
        "snapshot_4303", "snapshot_4304",
        "sensor.hoymiles_hit_ems_control_readback_generation",
        "snapshot_ems_generation') | int(16000001)",
        "sensor.hoymiles_hit_parallel_topology_readback_generation",
        "snapshot_topology_generation') | int(16000001)",
        "== (entry_owner_generation | int(-2))",
        "transaction_state: \"SNAPSHOT_VALID\"", "write_started: false",
        'reason_code: "snapshot_changed_before_transaction"',
    ), "post-APPLYING exact snapshot boundary")
    audit.require(post_arm_snapshot_guard.count("| int(-1)) == (state_attr(") == 2,
                  "Post-APPLYING generations are not exact equality checks")
    audit.require("| int(-1)) >= (state_attr(" not in post_arm_snapshot_guard,
                  "Post-APPLYING generation drift is accepted")
    audit.require("| float(20)" not in apply_path,
                  "Apply path gained a positive authority fallback")
    target_selection = _block(apply_path, "target_sensor: >-", "required_mode_before_apply: >-")
    audit.require("if slow_latched else" in target_selection,
                  "Slow target may be selected below the 95% latch")
    audit.require(source.count("'self_use' if is_state('sun.sun', 'above_horizon')") == 16,
                  "Current-sun mode derivation sites changed")
    audit.require(source.count(">= 99.9") == 4,
                  "99.9% hold/completion boundary changed")
    _assert_guarded_apply_helpers(audit, apply_path)
    transition = _script_block(source, "hoymiles_battery_balancing_transition")
    audit.contains(transition, (
        "steady_commit_guard", "expected_owner_generation",
        "current_commit_required_mode",
        "{{ 'self_use' if is_state('sun.sun', 'above_horizon')",
        "else 'grid_charge' }}",
        "transaction_state in [",
        "'OPERATIONAL', 'SLOW', 'HOLD_ARMING', 'HOLDING'",
        "owner_generation') | int(-1)",
        "sensor.hoymiles_battery_balancing_abort_request",
        "sensor.hoymiles_ems_hardware_mode",
        'steady_commit_guard: "{{ guard_steady_commit }}"',
        'expected_owner_generation: "{{ guarded_owner_generation }}"',
        "{{ not guard_steady_commit }}",
    ), "final steady-phase transition guard")
    final_commit = _block(
        apply_path,
        "# The final steady-phase service re-reads sun",
        "# The physical operational fact",
    )
    _assert_order(audit, final_commit, (
        "final_steady_state", "steady_commit_guard: true",
        "final_phase_commit_valid", "not final_phase_commit_valid",
        "final_correction_required_mode", "final_correction_eligible",
        "script.hoymiles_verified_set_ems_mode",
        "final_sun_mode_after_correction",
        "Durable hard-stop won during final mode correction",
        "final_correction_ack", "steady_commit_guard: true",
        "final_phase_commit_valid_after_attempt",
        "not final_phase_commit_valid_after_attempt",
    ), "bounded final sun correction")
    audit.contains(final_commit, (
        "final_phase_required_mode_now", "final_phase_commit_valid",
        "final_phase_required_mode_after_attempt",
        "final_phase_commit_valid_after_attempt",
        "sensor.hoymiles_ems_hardware_mode", "owner_generation",
        "sensor.hoymiles_battery_balancing_abort_request",
        "Single final correction was exhausted",
    ), "post-serializer sun/mode commit guards")
    audit.require(final_commit.count("{{ not final_phase_commit_valid }}") == 1,
                  "Initial final-phase post-serializer guard changed")
    audit.require(final_commit.count(
        "{{ not final_phase_commit_valid_after_attempt }}") == 1,
        "Post-correction final-phase guard changed")
    audit.require(final_commit.count(
        "script.hoymiles_verified_set_ems_mode") == 1,
        "Final sun correction is no longer bounded to one helper site")
    audit.require("false and is_state" not in final_commit,
                  "Final sun correction no longer honors a durable hard-stop")

    gap_guard = _script_block(source, "hoymiles_battery_balancing_soft_gap_guard")
    audit.contains(gap_guard, (
        "guarded_deadline_ms", "guarded_start_ms | int(-1) + 60000",
        "remaining_seconds", "hoymiles_battery_balancing_soft_gap_deadline",
        "gap_generation",
    ), "gap guard")
    controller_source = _automation_block(source, "hoymiles_battery_balancing_control",
                                          "hoymiles_battery_balancing_notification_delivery")
    controller = automations["hoymiles_battery_balancing_control"]
    audit.require(controller.get("mode") == "queued", "Controller may drop busy triggers")
    audit.require(controller.get("max") == 100, "Controller bound changed")
    audit.contains(controller_source, (
        "gap_start_epoch_ms", "gap_deadline_epoch_ms", "clock_anomaly",
        "data_stale_timeout", "new_gap_deadline_ms",
        "{{ (now_epoch_ms | int(0)) + 60000 }}",
        "script.hoymiles_battery_balancing_soft_gap_guard", "timing_state == 'GAP'",
    ), "restart-safe gap")

    hold_guard = _script_block(source, "hoymiles_battery_balancing_hold_guard")
    audit.contains(hold_guard, (
        "guarded_cycle", "guarded_generation", "guarded_deadline_ms",
        "hoymiles_battery_balancing_hold_deadline",
    ), "hold guard")
    hold_arm = _block(apply_path, "# HOLD_ARMING and absolute deadline",
                      "# The physical operational fact")
    _assert_order(audit, hold_arm, (
        'timing_state: "HOLD_ARMING"', 'transaction_state: "HOLD_ARMING"',
        "timer.start", 'timing_state: "HOLDING"', 'transaction_state: "HOLDING"',
    ), "two-phase hold")
    audit.contains(worker_source, (
        "entry_state == 'HOLD_ARMING'", "timer.hoymiles_battery_balancing_hold",
        "hold_deadline_ms", "hold_generation", "hold_timing_cycle == entry_cycle",
        "(hold_now_ms | int(0))", "< 99.9", "timer.cancel",
    ), "restart-atomic hold")
    fresh_hold = _block(apply_path, "hold_soc_value: >-", "# HOLD_ARMING and absolute deadline")
    audit.contains(fresh_hold, (
        "states('sensor.hoymiles_hit_overview_battery_soc')",
        "hold_soc_age_seconds", "fresh_full_soc", ">= 99.9", "<= 120",
        "hold_generation_before_arm", "hold_required_mode", "entry_owner_generation",
        "sensor.hoymiles_battery_balancing_abort_request",
    ), "fresh SOC before HOLD_ARMING")
    hold_arm = _block(apply_path, "# HOLD_ARMING and absolute deadline",
                      "# The physical operational fact")
    audit.contains(hold_arm, (
        'timing_state: "HOLD_ARMING"', "hold_soc_after_timing_write",
        "hold_soc_age_after_timing_write", "hold_mode_after_timing_write",
        "delay: 0", "hold_arm_after_timing_valid",
        "(hold_soc_after_timing_write | float(-1)) >= 99.9",
        "binary_sensor.hoymiles_battery_balancing_apply_authorized",
        "sensor.hoymiles_battery_balancing_abort_request",
        "not hold_arm_after_timing_valid", 'timing_state: "NONE"',
        'transaction_state: "HOLD_ARMING"', "timer.start",
    ), "post-timing HOLD_ARMING boundary")
    hold_reconcile = _block(worker_source, "# Startup and periodic hold reconciliation",
                            "# Timer/deadline completion")
    audit.require(hold_reconcile.count(
        "- (as_timestamp(now()) * 1000)) / 1000, 1]") == 1,
        "Restart does not use only the remaining hold duration")
    drop_hold = _block(worker_source, "# A drop below 99.9%", "# Startup and periodic hold")
    audit.require(drop_hold.count("action: timer.cancel") == 1,
                  "SOC drop no longer cancels the active hold once")
    audit.require(restore.count("+ 900000") == 2,
                  "Abort cooldown boundary changed")

    priority_hard_source = _automation_block(source,
        "hoymiles_battery_balancing_off_grid_hard_stop_capture",
        "hoymiles_battery_balancing_hard_stop_capture")
    hard_source = _automation_block(source,
        "hoymiles_battery_balancing_hard_stop_capture",
        "hoymiles_battery_balancing_control")
    priority_lane_ids = (
        "hoymiles_battery_balancing_off_grid_hard_stop_capture",
        "hoymiles_battery_balancing_p95_hard_stop_capture",
        "hoymiles_battery_balancing_p90_hard_stop_capture",
        "hoymiles_battery_balancing_p80_hard_stop_capture",
        "hoymiles_battery_balancing_p75_hard_stop_capture",
        "hoymiles_battery_balancing_p60_hard_stop_capture",
    )
    priority_lane_trigger_ids = {
        "hoymiles_battery_balancing_off_grid_hard_stop_capture": {
            "ems_off_grid_priority",
        },
        "hoymiles_battery_balancing_p95_hard_stop_capture": {
            "owner_lost_priority", "control_conflict_priority",
        },
        "hoymiles_battery_balancing_p90_hard_stop_capture": {
            "bms_fault_code_priority", "bms_overview_fault_priority",
            "inverter_fault_priority", "bms_current_invalid_priority",
            "bms_voltage_invalid_priority", "bms_safe_limit_invalid_priority",
            "topology_invalid_priority",
        },
        "hoymiles_battery_balancing_p80_hard_stop_capture": {
            "user_disabled_priority", "watchdog_finished_priority",
        },
        "hoymiles_battery_balancing_p75_hard_stop_capture": {
            "control_data_p75_priority",
        },
        "hoymiles_battery_balancing_p60_hard_stop_capture": {
            "control_data_p60_priority",
        },
    }
    hard = automations["hoymiles_battery_balancing_hard_stop_capture"]
    for lane_id in priority_lane_ids:
        lane = automations[lane_id]
        audit.require(lane.get("mode") == "queued",
                      f"Priority lane is not FIFO: {lane_id}")
        audit.require(lane.get("max") == 100,
                      f"Priority lane bound changed: {lane_id}")
        lane_source = yaml.safe_dump(lane, sort_keys=False)
        trigger_ids = {
            str(item.get("id")) for item in lane.get("triggers", [])
            if isinstance(item, dict)
        }
        audit.require(
            trigger_ids == priority_lane_trigger_ids[lane_id],
            f"Priority classes were merged, omitted or duplicated: {lane_id}",
        )
        audit.require("wake_worker: false" in lane_source,
                      f"Priority lane wakes before merge drain: {lane_id}")
        audit.require("hoymiles_battery_balancing_request_abort" in lane_source,
                      f"Priority lane does not merge abort: {lane_id}")
        audit.require("hoymiles_battery_balancing_transaction_worker" in lane_source,
                      f"Priority lane does not perform final wake: {lane_id}")
        _assert_order(audit, lane_source, (
            "wake_worker: false", "wait_template:",
            "state_attr(this.entity_id, 'current')", "<= 1",
            "hoymiles_battery_balancing_transaction_worker",
        ), f"priority lane merge/drain/wake: {lane_id}")
        audit.require("notify." not in lane_source and "timer.start" not in lane_source,
                      f"Priority lane gained notification/timer effects: {lane_id}")
        audit.require(not any(helper in lane_source for helper in PHYSICAL_HELPERS),
                      f"Priority lane became a physical writer: {lane_id}")
    audit.require(hard.get("mode") == "queued", "Hard-stop may drop triggers")
    audit.require(hard.get("max") == 100, "Hard-stop bound changed")
    audit.require("modbus." not in priority_hard_source + hard_source,
                  "Hard-stop writes Modbus")
    audit.require(not any(helper in priority_hard_source for helper in PHYSICAL_HELPERS),
                  "Off-Grid priority lane became a physical writer")
    audit.contains(priority_hard_source, (
        'to: "off_grid"', "trigger.id == 'ems_off_grid_priority'",
        "trigger.from_state is defined", "trigger.to_state.state == 'off_grid'",
        "frozen_transaction_state", "frozen_transaction_cycle",
        "frozen_off_grid_reason",
        "owner_lost_priority", "control_conflict_priority",
        "bms_fault_code_priority", "bms_current_invalid_priority",
        "user_disabled_priority", "watchdog_finished_priority",
        "control_data_p75_priority", "control_data_p60_priority",
        "lifecycle_invalid_priority",
        "ownership_lost", "control_conflict", "bms_fault",
        "communication_failure", "unknown_internal_error",
        "script.hoymiles_battery_balancing_request_abort",
        "wake_worker: false", "wait_template",
        "script.hoymiles_battery_balancing_transaction_worker",
    ), "priority hard-stop lanes")
    recovery_lane = automations[
        "hoymiles_battery_balancing_recovery_hard_stop_capture"
    ]
    recovery_lane_source = yaml.safe_dump(recovery_lane, sort_keys=False)
    audit.require(recovery_lane.get("mode") == "restart",
                  "Malformed-lifecycle recovery admission is not bounded")
    audit.contains(recovery_lane_source, (
        "lifecycle_invalid_priority", "trigger.to_state",
        "frozen_invalid_reason", "frozen_owner_active",
        "hoymiles_battery_balancing_enter_recovery",
    ), "dedicated malformed-lifecycle recovery lane")
    audit.require(not any(helper in recovery_lane_source for helper in PHYSICAL_HELPERS),
                  "Malformed-lifecycle recovery lane became a physical writer")
    audit.contains(hard_source, (
        "off_grid", "user_disabled", "bms_fault", "bms_limit_invalid",
        "ownership_lost", "control_conflict", "watchdog_timeout",
        "script.hoymiles_battery_balancing_request_abort",
        "frozen_transaction_state", "frozen_transaction_cycle",
        "frozen_hard_reason", "trigger.to_state", "trigger.from_state",
        "wake_worker: false",
        "(state_attr(this.entity_id, 'current') | int(1)) <= 1",
        "script.hoymiles_battery_balancing_transaction_worker",
    ), "hard-stop capture")
    _assert_order(audit, hard_source, (
        "wake_worker: false", "state_attr(this.entity_id, 'current')",
        "'PENDING'", "script.hoymiles_battery_balancing_transaction_worker",
        'intent: "abort"',
    ), "hard-stop queue-drain wake")
    audit.require(
        "trigger.id == 'ems_off_grid_priority'" in priority_hard_source
        and "trigger.to_state.state == 'off_grid'" in priority_hard_source,
        "Physical Off-Grid is no longer frozen from trigger evidence")
    audit.require(
        "trigger.id in ['bms_fault_code', 'bms_overview_fault']" in hard_source
        and "trigger.from_state is defined" in hard_source,
        "BMS fault reason is no longer frozen from trigger evidence")
    audit.require(
        "not_to: \"Brak błędu\"" in hard_source
        and "not_to: \"Brak błędów\"" in hard_source,
        "Fault recovery transitions can fill the hard-stop queue")
    request_abort = _script_block(source, "hoymiles_battery_balancing_request_abort")
    audit.require(not any(helper in request_abort for helper in PHYSICAL_HELPERS),
                  "Hard-stop became physical writer")
    audit.contains(request_abort, (
        "current_priority", "requested_priority | int(-1) > current_priority",
        'request_state: "PENDING"', "queue_restore_worker", "script.turn_on",
        'intent: "abort"', "physical_transaction_failure",
        "requested_wake_worker", "wake_worker | default(true) | bool",
        "requested_wake_worker and queue_restore_worker",
    ), "durable abort")
    audit.require("and not (\n               is_state(\n                 'sensor.hoymiles_battery_balancing_abort_request', 'PENDING')" in source,
                  "Apply authorization does not block pending abort")
    audit.contains(apply_path, (
        "# Final tail authorization", "not final_transaction_ack",
        "Durable hard-stop observed at the final tail",
    ), "final hard-stop tail")
    operational_fact = _block(
        apply_path,
        "# The physical operational fact",
        "# Final tail check",
    )
    _assert_order(audit, operational_fact, (
        "committed_state", "started_already",
        "operational_started: true", 'event_marker: "STARTED"',
        "started_commit_required_mode", "started_commit_valid",
        "not started_commit_valid",
        "hoymiles_notify_battery_balancing_lifecycle",
    ), "operational fact independent of outbox capacity")
    audit.contains(operational_fact, (
        "steady_commit_guard: true", "expected_owner_generation",
        "started_commit_valid", "sensor.hoymiles_ems_hardware_mode",
        "sensor.hoymiles_battery_balancing_abort_request",
        "STARTED fact boundary lost the current sun/mode invariant",
    ), "STARTED post-serializer sun/mode guard")
    operational_commit = _block(
        operational_fact, "committed_state: >-", "started_fact_committed: >-"
    )
    audit.require("notification_outbox" not in operational_commit,
                  "Operational physical fact depends on free outbox capacity")
    audit.require("{% elif terminal_was_started %}aborted" in restore,
                  "Operational abort is no longer classified from the physical fact")
    apply_wrapper = scripts["hoymiles_apply_battery_balancing_target"]
    audit.require(_action_counts(apply_wrapper)["script.turn_on"] == 1,
                  "Apply wrapper no longer queues the physical worker")
    audit.require("script.hoymiles_battery_balancing_transaction_worker" in
                  _script_block(source, "hoymiles_apply_battery_balancing_target"),
                  "Apply wrapper targets another path")
    audit.require(controller_source.count("timing_state == 'NONE'") == 2,
                  "Soft-gap initial deadline logic changed")
    audit.require(controller_source.count(
        "script.hoymiles_battery_balancing_soft_gap_guard") == 2,
        "Soft-gap restart/recovery guard re-arm changed")
    return audit.checks


def assert_notification_contract(source: str) -> int:
    """Derive outbox ordering and scheduler-side deduplication from YAML."""
    audit = Audit()
    package = _load_package(source)
    scripts = package["script"]
    producer = _script_block(source, "hoymiles_notify_battery_balancing_lifecycle")
    updater = _script_block(source, "hoymiles_battery_balancing_update_outbox_delivery")
    provider = _script_block(
        source, "hoymiles_battery_balancing_notification_provider_attempt"
    )
    timeout = _script_block(
        source, "hoymiles_battery_balancing_notification_attempt_timeout"
    )
    recovery = _script_block(
        source, "hoymiles_battery_balancing_recover_notification_leases"
    )
    dispatcher = _script_block(source, "hoymiles_battery_balancing_notification_dispatcher")
    delivery = _automation_block(source,
        "hoymiles_battery_balancing_notification_delivery",
        "hoymiles_ems_push_status_notification")
    worker = _script_block(source, "hoymiles_battery_balancing_transaction_worker")
    generic = _automation_block(source, "hoymiles_ems_push_status_notification",
                                "hoymiles_initialize_solcast_forecast_entity")
    audit.contains(producer, (
        "event_kind", "event_id: \"{{ requested_cycle ~ '.' ~ event_kind }}\"",
        "stable_tag: \"{{ 'bb.' ~ requested_cycle ~ '.' ~ event_kind }}\"",
        "event_already_present", "selected_slot",
        'slot_1_delivery_state: "PENDING"', 'slot_2_delivery_state: "PENDING"',
        "continue_on_timeout: false",
    ), "event producer")
    audit.require('event_already_present: "{{ event_id in [slot_1_id, slot_2_id] }}"'
                  in producer, "Strict event-ID dedupe was removed")
    audit.require("notify.send_message" not in producer, "Producer calls phone")
    audit.require(_action_counts(scripts["hoymiles_notify_battery_balancing_lifecycle"])
                  ["script.hoymiles_battery_balancing_write_outbox"] == 3,
                  "Outbox producer topology changed")
    audit.contains(updater, (
        "expected_delivery_state", "expected_attempt_count",
        "slot_1_selected", "slot_2_selected",
        "expected_state in ['PENDING', 'DELIVERING']",
        "'PENDING', 'DELIVERING', 'DELIVERED'",
    ), "outbox compare-and-set")
    audit.contains(provider, (
        "mode: parallel", "max: 6", "lease_is_current",
        "slot_1_delivery_state') == 'DELIVERING'",
        "notify.send_message", 'tag: "{{ leased_tag }}"',
        "provider_return_before_deadline", "< leased_deadline_ms",
        'expected_delivery_state: "DELIVERING"',
        'expected_attempt_count: "{{ leased_generation }}"',
        'next_delivery_state: "DELIVERED"',
    ), "bounded provider attempt")
    audit.require(
        scripts["hoymiles_battery_balancing_notification_provider_attempt"].get("mode")
        == "parallel"
        and scripts["hoymiles_battery_balancing_notification_provider_attempt"].get("max")
        == 6,
        "Provider attempt worker bound changed",
    )
    audit.contains(timeout, (
        "mode: parallel", "max: 6", "remaining_seconds",
        "continue_on_timeout: true", "deadline_reached",
        ">= guarded_deadline_ms", "lease_still_current",
        "'PERMANENT_FAILURE' if guarded_generation >= 3 else 'PENDING'",
    ), "absolute provider timeout")
    audit.require(
        scripts["hoymiles_battery_balancing_notification_attempt_timeout"].get("mode")
        == "parallel"
        and scripts["hoymiles_battery_balancing_notification_attempt_timeout"].get("max")
        == 6,
        "Provider timeout worker bound changed",
    )
    audit.contains(recovery, (
        "slot_1_state == 'DELIVERING' or slot_2_state == 'DELIVERING'",
        "'PENDING' if slot_1_state == 'DELIVERING'",
        "'PENDING' if slot_2_state == 'DELIVERING'",
        "slot_1_attempt_count", "slot_2_attempt_count",
    ), "restart lease recovery")
    audit.require(recovery.count("'slot_1_attempt_count') }}") == 1
                  and recovery.count("'slot_2_attempt_count') }}") == 1,
                  "Restart resets an active delivery lease generation")
    audit.contains(dispatcher, (
        "selected_attempt | int(0) >= 3", 'next_delivery_state: "PERMANENT_FAILURE"',
        'next_delivery_state: "SUPPRESSED_DISABLED"', "next_attempt",
        "+ 15000", 'next_delivery_state: "DELIVERING"',
        "script.hoymiles_battery_balancing_notification_attempt_timeout",
        "script.hoymiles_battery_balancing_notification_provider_attempt",
    ), "dispatcher")
    audit.require("notify.send_message" not in dispatcher,
                  "Short dispatcher can still hang in the provider")
    for name in (
        "hoymiles_battery_balancing_notification_dispatcher",
        "hoymiles_battery_balancing_notification_provider_attempt",
        "hoymiles_battery_balancing_notification_attempt_timeout",
        "hoymiles_battery_balancing_recover_notification_leases",
    ):
        block = _script_block(source, name)
        audit.require(not any(helper in block for helper in PHYSICAL_HELPERS),
                      f"Notification path gained physical authority: {name}")
        audit.require("input_boolean.turn_off" not in block,
                      f"Notification path releases owner: {name}")
        audit.require("timer.start" not in block and "timer.cancel" not in block,
                      f"Notification path changes transaction timers: {name}")
    audit.contains(delivery, (
        "event: start", "id: startup", "time_pattern",
        "script.hoymiles_battery_balancing_recover_notification_leases",
        "script.hoymiles_battery_balancing_notification_dispatcher",
    ), "outbox retry automation")
    audit.require("continue-on-error" not in delivery, "Delivery setup failure is hidden")
    audit.require(_action_counts(scripts["hoymiles_battery_balancing_transaction_worker"])
                  ["script.hoymiles_notify_battery_balancing_lifecycle"] == 6,
                  "Terminal event generation sites changed")
    audit.contains(worker, (
        'transaction_state: "NOTIFICATION_PENDING"',
        'transaction_state: "TERMINAL"', "prior_event_marker",
        "terminal_event", "terminal_marker",
    ), "single terminal closeout")
    audit.contains(generic, (
        "raw_status in ['Czuwanie', 'Test sieci', 'Praca z siecią']",
        "exact_terminal_fault_coverage", "raw_status == 'Awaria'",
        "raw_status not in [", "expected_balancing_pair or expected_safe_restore",
    ), "closed generic suppression")
    expected_pair = _block(generic, "expected_balancing_pair: >-", "expected_safe_restore: >-")
    audit.require("raw_status in ['Czuwanie', 'Test sieci', 'Praca z siecią']"
                  in expected_pair, "Routine balancing allowlist was widened")
    audit.require(generic.count("or exact_terminal_fault_coverage") == 2,
                  "Generic/terminal exact-fault suppression changed")
    return audit.checks


def _load_generator():
    path = ROOT / "tools" / "build_hacs_assets.py"
    spec = importlib.util.spec_from_file_location("balancing_asset_generator", path)
    if spec is None or spec.loader is None:
        raise AssertionError("Cannot load asset generator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_validator():
    path = ROOT / "tools" / "validate_release.py"
    spec = importlib.util.spec_from_file_location("balancing_release_validator", path)
    if spec is None or spec.loader is None:
        raise AssertionError("Cannot load release validator")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assert_generated_assets_fresh(canonical_override: str | None = None) -> int:
    audit = Audit()
    generator = _load_generator()
    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    first, second = generator.render_managed_assets(catalog), generator.render_managed_assets(catalog)
    audit.require(first == second, "In-memory generation is not deterministic")
    if canonical_override is None:
        for destination, expected in first.items():
            audit.require(destination.is_file(), f"Missing asset {destination}")
            audit.require(destination.read_bytes().replace(b"\r\n", b"\n")
                          == expected.encode("utf-8"),
                          f"Stale asset {destination.relative_to(ROOT)}")
    else:
        package = generator.transform_entity_ids(canonical_override, catalog)
        expected_pl = generator.canonicalize_proxy_select_options(package).encode()
        expected_en = generator.translate_asset_to_english(package).encode()
        audit.require(PL_SCHEDULER.read_bytes().replace(b"\r\n", b"\n") == expected_pl,
                      "Ungenerated canonical mutation accepted by PL copy")
        audit.require(EN_SCHEDULER.read_bytes().replace(b"\r\n", b"\n") == expected_en,
                      "Ungenerated canonical mutation accepted by EN copy")
    return audit.checks


def assert_validator_source_contract(validator_source: str, workflow_source: str) -> int:
    audit = Audit()
    tree = ast.parse(validator_source)
    functions = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    audit.contains(validator_source, (
        "test_path.is_file()", "EXPECTED_BALANCING_TEST_SHA256",
        "EXPECTED_BALANCING_TEST_AST_SHA256",
        "EXPECTED_BALANCING_RUNTIME_SHA256",
        "EXPECTED_BALANCING_RUNTIME_AST_SHA256",
        "EXPECTED_BALANCING_SCHEDULER_SHA256",
        "EXPECTED_BALANCING_SCRIPT_SEMANTIC_SHA256",
        "EXPECTED_BALANCING_CONTROL_SEMANTIC_SHA256",
        "require(runtime_path.is_file()",
        "hashlib.sha256(runtime_bytes).hexdigest()",
        "== EXPECTED_BALANCING_RUNTIME_SHA256",
        "reviewed_ast_sha256(runtime_tree)",
        "== EXPECTED_BALANCING_RUNTIME_AST_SHA256",
        "REQUIRED_BALANCING_TEST_FUNCTIONS <= functions",
        "REQUIRED_BALANCING_RUNTIME_FUNCTIONS <= runtime_functions",
        'validate_runtime_runner(runtime_tree, "async_main")',
        'validate_runtime_runner(runtime_tree, "main")',
        "assignments.get(result_name) == function_name",
        "annotate_fields=True", "include_attributes=False", "indent=None",
        "validate_balancing_object_freeze(ems_package)",
        "validate_balancing_scheduler_freeze(ems_package)",
        "validate_lifecycle_parser_boundary(lifecycle_boundary_objects(package))",
        "raw_readers == EXPECTED_LIFECYCLE_RAW_READERS",
        'direct_writers == ["script:hoymiles_battery_balancing_write_record"]',
        "tainted = {", "mentions_alias", "parses_alias",
        "parses_alias(text, alias) for text in texts for alias in tainted",
        "for alias in tainted", "mapping.get(\"action\", mapping.get(\"service\"))",
        "if isinstance(entity_id, list)", "if helper in entity_ids",
        're.search(r"\\|\\s*split\\s*\\(", text)',
        "validate_managed_asset_freshness(catalog)",
        'actual == expected.encode("utf-8")', "first == second",
        "actual == EXPECTED_BALANCING_OBJECTS[domain]",
        "constant_disabled", "require_unconditional",
        'require_unconditional(job, f"job {job_name}")',
        'require_unconditional(step, f"step {job_name}/{command}")',
        "Mandatory workflow {label} has an unauthorized if condition",
        "len(matching_steps) == 1",
    ), "release validator")
    for name in (
        "validate_balancing_test_identity", "validate_managed_asset_freshness",
        "validate_balancing_object_freeze", "validate_balancing_scheduler_freeze",
        "lifecycle_boundary_objects", "validate_lifecycle_parser_boundary",
        "validate_runtime_runner",
        "validate_mandatory_workflow_step",
    ):
        audit.require(name in functions, f"Validator function missing: {name}")
    for constant in (
        "EXPECTED_BALANCING_TEST_SHA256",
        "EXPECTED_BALANCING_TEST_AST_SHA256",
        "EXPECTED_BALANCING_RUNTIME_SHA256",
        "EXPECTED_BALANCING_RUNTIME_AST_SHA256",
        "EXPECTED_BALANCING_SCHEDULER_SHA256",
        "EXPECTED_BALANCING_SCRIPT_SEMANTIC_SHA256",
        "EXPECTED_BALANCING_CONTROL_SEMANTIC_SHA256",
    ):
        audit.require(re.search(rf'{constant} = "[0-9a-f]{{64}}"',
                                validator_source) is not None,
                      f"Reviewed hash is not frozen: {constant}")
    workflow = yaml.safe_load(workflow_source)
    for job_name, command in (
        ("project-checks", "python tools/test_battery_balancing_contract.py"),
        ("battery-balancing-ha-runtime", "python tools/test_battery_balancing_ha_runtime.py"),
    ):
        steps = workflow.get("jobs", {}).get(job_name, {}).get("steps", [])
        matches = [step for step in steps if str(step.get("run", "")).strip() == command]
        job = workflow.get("jobs", {}).get(job_name, {})
        audit.require(len(matches) == 1, f"Workflow omits or duplicates {command}")
        audit.require("if" not in job and "if" not in matches[0],
                      f"Workflow conditionally skips {command}")
        audit.require("continue-on-error" not in matches[0]
                      or matches[0].get("continue-on-error") is False,
                      f"Workflow makes {command} optional")
    audit.require('"homeassistant==2026.8.2"' in workflow_source,
                  "Exact HA 2026.8.2 runtime is not pinned")
    return audit.checks


def assert_runtime_source_contract(runtime_source: str) -> int:
    """Independently require real race calls in both exact-version runners."""
    audit = Audit()
    tree = ast.parse(runtime_source)
    for runner_name in ("async_main", "main"):
        runner = next(
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == runner_name
        )
        race_assignments = [
            node for node in runner.body
            if isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "races"
        ]
        audit.require(len(race_assignments) == 1,
                      f"Runtime race result missing in {runner_name}")
        audit.require(
            "test_hard_stop_race_matrix" in ast.dump(race_assignments[0]),
            f"Runtime race result is not produced by the race group in {runner_name}",
        )
    audit.require(runtime_source == RUNTIME.read_text(encoding="utf-8"),
                  "Runtime source differs from the exact reviewed file")
    return audit.checks


def assert_documentation_contract(
    readme: str, readme_pl: str, releasing: str
) -> int:
    """Keep hard-stop limits and split Python validation paths truthful."""
    audit = Audit()
    audit.contains(readme, (
        "Hard-stop evidence is frozen when its trigger is admitted",
        "Independent bounded FIFO admission lanes",
        "An already running verified helper call cannot be cancelled",
        "durable record remains raw `APPLYING`",
    ), "English lifecycle documentation")
    audit.contains(readme_pl, (
        "Dowód hard-stop jest zamrażany w chwili przyjęcia wyzwalacza",
        "Niezależne, ograniczone kolejki FIFO",
        "Już uruchomionego wywołania zweryfikowanego helpera nie można anulować",
        "Trwały rekord pozostaje surowym `APPLYING`",
    ), "Polish lifecycle documentation")
    audit.contains(releasing, (
        "Python 3.12 — structural/offline gate",
        "Python 3.14.7 + Home Assistant 2026.8.2 — isolated runtime gate",
        "100 lower-priority triggers followed by a",
        "the durable record remains raw `APPLYING`",
    ), "split battery-balancing release gates")
    forbidden = (
        "immediately cancels an in-flight helper",
        "natychmiast anuluje trwające wywołanie helpera",
    )
    audit.require(not any(marker in readme or marker in readme_pl for marker in forbidden),
                  "Documentation overstates hard-stop cancellation")
    return audit.checks


def test_power_contract() -> int:
    cases = 0
    for system_kw, expected in ((5.0, 8.0), (10.0, 4.0), (20.0, 2.0)):
        result = calculate_slow_power(rated_each_kw=system_kw, machine_type=0,
            machine_count=1, load_kw=0, bms_battery_kw=system_kw, mode="self_use")
        assert result is not None and result.percent == expected
        assert result.net_battery_kw <= TARGET_KW + 1e-9
        cases += 1
    parallel = calculate_slow_power(rated_each_kw=20, machine_type=1,
        machine_count=2, load_kw=0, bms_battery_kw=40, mode="self_use")
    assert parallel is not None and parallel.percent == 1.0
    assert abs(parallel.net_battery_kw - TARGET_KW) < 1e-9
    assert parallel.requested_budget_kw == TARGET_KW
    cases += 1
    for system_kw in (5.0, 10.0, 20.0):
        for load_kw in (0.0, 0.5, 2.0, system_kw - 0.2):
            for mode in ("self_use", "grid_charge"):
                result = calculate_slow_power(rated_each_kw=system_kw, machine_type=0,
                    machine_count=1, load_kw=load_kw, bms_battery_kw=system_kw, mode=mode)
                assert result is not None and result.percent <= 100.0
                assert result.net_battery_kw <= TARGET_KW + 1e-9
                safe = (system_kw + load_kw if mode == "grid_charge" else system_kw)
                assert result.percent <= safe / system_kw * 100.0 + 1e-9
                if mode == "grid_charge" and load_kw <= system_kw - TARGET_KW:
                    assert result.net_battery_kw > TARGET_KW - 0.011 * system_kw
                    assert result.requested_budget_kw == load_kw + TARGET_KW
                cases += 1
    parallel_grid = calculate_slow_power(rated_each_kw=20, machine_type=1,
        machine_count=2, load_kw=2, bms_battery_kw=40, mode="grid_charge")
    assert parallel_grid is not None and parallel_grid.percent == 6.0
    assert abs(parallel_grid.net_battery_kw - TARGET_KW) < 1e-9
    cases += 1
    for rated in (5, 10, 12, 15, 20):
        assert calculate_slow_power(rated_each_kw=rated, machine_type=0,
            machine_count=1, load_kw=0, bms_battery_kw=rated, mode="self_use") is not None
        cases += 1
    for invalid in (None, "unknown", "unavailable", "10-ish", 0, -5, math.nan, math.inf):
        assert calculate_slow_power(rated_each_kw=invalid, machine_type=0,
            machine_count=1, load_kw=0, bms_battery_kw=5, mode="self_use") is None
        cases += 1
    for kind, count in ((None, 1), (0, 0), (0, 2), (1, 1), (1, 0),
                        (2, 2), (1, 11), (1, 2.5)):
        assert calculate_slow_power(rated_each_kw=10, machine_type=kind,
            machine_count=count, load_kw=0, bms_battery_kw=10, mode="self_use") is None
        cases += 1
    for invalid in (None, "unavailable", "bad", -0.1, math.nan, math.inf):
        assert calculate_slow_power(rated_each_kw=10, machine_type=0,
            machine_count=1, load_kw=invalid, bms_battery_kw=10, mode="grid_charge") is None
        cases += 1
    for invalid in (None, "unavailable", "bad", 0, -1, math.nan, math.inf):
        assert calculate_slow_power(rated_each_kw=10, machine_type=0,
            machine_count=1, load_kw=0, bms_battery_kw=invalid, mode="self_use") is None
        cases += 1
    for bms_kw in (1.0, 0.4, 0.31):
        result = calculate_slow_power(rated_each_kw=10, machine_type=0,
            machine_count=1, load_kw=0, bms_battery_kw=bms_kw, mode="self_use")
        assert result is not None and result.net_battery_kw <= min(TARGET_KW, bms_kw) + 1e-9
        cases += 1
    assert calculate_slow_power(rated_each_kw=20, machine_type=0,
        machine_count=1, load_kw=0, bms_battery_kw=0.01, mode="self_use") is None
    cases += 1
    for freshness in ({"load_fresh": False}, {"bms_fresh": False},
                      {"topology_fresh": False}, {"readback_fresh": False}):
        assert calculate_slow_power(rated_each_kw=10, machine_type=0,
            machine_count=1, load_kw=0.5, bms_battery_kw=10,
            mode="grid_charge", **freshness) is None
        cases += 1
    assert cases == 71
    return cases


def test_soc_contract() -> int:
    cases = 0
    for soc in (94.8, 94.9):
        assert phase_after_soc("pv", soc) == ("pv", False, False); cases += 1
    for soc in (95.0, 95.1, 97.0, 99.0, 99.8):
        assert phase_after_soc("pv", soc) == ("slow", False, False); cases += 1
    assert phase_after_soc("pv", 99.9, ack=False) == ("slow", False, False); cases += 1
    for soc in (99.9, 100.0):
        assert phase_after_soc("slow", soc) == ("holding", True, False); cases += 1
    assert phase_after_soc("slow", 94.9) == ("slow", False, False); cases += 1
    assert phase_after_soc("holding", 99.8) == ("slow", False, True); cases += 1
    assert cases == 12
    return cases


def test_production_lifecycle_contract() -> int:
    checks = assert_production_contract(SCHEDULER.read_text(encoding="utf-8"))
    assert checks >= 100
    return checks


def test_notification_contract() -> int:
    checks = assert_notification_contract(SCHEDULER.read_text(encoding="utf-8"))
    assert checks >= 30
    return checks


def test_validator_contract() -> int:
    checks = assert_validator_source_contract(VALIDATOR.read_text(encoding="utf-8"),
                                               WORKFLOW.read_text(encoding="utf-8"))
    validator = _load_validator()
    package = yaml.safe_load(SCHEDULER.read_text(encoding="utf-8"))
    validator.validate_lifecycle_parser_boundary(
        validator.lifecycle_boundary_objects(package)
    )
    checks += 1
    parser_mutation = yaml.safe_load(SCHEDULER.read_text(encoding="utf-8"))
    parser_mutation["script"]["unexpected_lifecycle_reader"] = {
        "sequence": [{
            "variables": {
                "raw": "{{ states('input_text.hoymiles_battery_balancing_lifecycle') }}",
                "copy": "{{ raw }}",
                "field": "{{ copy | split('|') | list }}",
            }
        }]
    }
    try:
        validator.validate_lifecycle_parser_boundary(
            validator.lifecycle_boundary_objects(parser_mutation)
        )
    except RuntimeError:
        checks += 1
    else:
        raise AssertionError("Unknown lifecycle parser survived the package-wide boundary")
    writer_mutation = yaml.safe_load(SCHEDULER.read_text(encoding="utf-8"))
    writer_mutation["script"]["unexpected_lifecycle_writer"] = {
        "sequence": [{
            "service": "input_text.set_value",
            "target": {"entity_id": [
                "input_text.hoymiles_battery_balancing_lifecycle"
            ]},
            "data": {"value": "unexpected"},
        }]
    }
    try:
        validator.validate_lifecycle_parser_boundary(
            validator.lifecycle_boundary_objects(writer_mutation)
        )
    except RuntimeError:
        checks += 1
    else:
        raise AssertionError("Unknown lifecycle writer survived the package-wide boundary")
    workflow_source = WORKFLOW.read_text(encoding="utf-8")
    workflow_contracts = (
        ("project-checks", "python tools/test_battery_balancing_contract.py"),
        ("battery-balancing-ha-runtime", "python tools/test_battery_balancing_ha_runtime.py"),
    )
    for job_name, command in workflow_contracts:
        validator.validate_mandatory_workflow_step(
            yaml.safe_load(workflow_source), command, job_name=job_name
        )
        checks += 1
        for disabled in (
            False, 0, "false", "False", "  FALSE  ",
            "${{ false }}", "${{ 0 }}", " ${{   FaLsE   }} ",
        ):
            for scope in ("step", "job"):
                mutated = yaml.safe_load(workflow_source)
                job = mutated["jobs"][job_name]
                if scope == "job":
                    job["if"] = disabled
                else:
                    step = next(
                        item for item in job["steps"]
                        if str(item.get("run", "")).strip() == command
                    )
                    step["if"] = disabled
                try:
                    validator.validate_mandatory_workflow_step(
                        mutated, command, job_name=job_name
                    )
                except RuntimeError:
                    checks += 1
                else:
                    raise AssertionError(
                        f"Disabled mandatory workflow {scope} survived: {disabled!r}"
                    )
        for mutation in ("remove", "continue"):
            mutated = yaml.safe_load(workflow_source)
            job = mutated["jobs"][job_name]
            step = next(
                item for item in job["steps"]
                if str(item.get("run", "")).strip() == command
            )
            if mutation == "remove":
                step["run"] = "python -m compileall tools"
            else:
                step["continue-on-error"] = True
            try:
                validator.validate_mandatory_workflow_step(
                    mutated, command, job_name=job_name
                )
            except RuntimeError:
                checks += 1
            else:
                raise AssertionError(f"Workflow mutation survived: {job_name}/{mutation}")
    checks += _assert_generated_assets_fresh()
    canonical = SCHEDULER.read_text(encoding="utf-8")
    mutation = canonical.replace(">= 99.9", ">= 99.8", 1)
    assert mutation != canonical
    try:
        _assert_generated_assets_fresh(mutation)
    except AssertionError:
        return checks + 1
    raise AssertionError("Ungenerated canonical 99.9 -> 99.8 mutation survived")


def _replace_once(source: str, old: str, new: str) -> str:
    if old not in source:
        raise AssertionError(f"Mutation anchor missing: {old!r}")
    return source.replace(old, new, 1)


def _replace_after(source: str, anchor: str, old: str, new: str) -> str:
    index = source.find(anchor)
    if index < 0 or old not in source[index:]:
        raise AssertionError(f"Mutation anchor missing after {anchor!r}: {old!r}")
    return source[:index] + source[index:].replace(old, new, 1)


def _source_mutation_detected(mutated: str) -> bool:
    try:
        assert_production_contract(mutated)
        assert_notification_contract(mutated)
    except (AssertionError, yaml.YAMLError):
        return True
    return False


def _run_source_mutations(source: str, mutations: dict[str, Callable[[str], str]]) -> tuple[int, list[str]]:
    detected: list[str] = []
    for name, mutate in mutations.items():
        mutated = mutate(source)
        assert mutated != source, f"Mutation did not alter source: {name}"
        if _source_mutation_detected(mutated):
            detected.append(name)
    return len(detected), sorted(set(mutations) - set(detected))


def test_existing_mutations() -> tuple[int, int]:
    source = SCHEDULER.read_text(encoding="utf-8")
    mutations: dict[str, Callable[[str], str]] = {
        "restore_2kw": lambda s: _replace_once(s,
            "BALANCING_SLOW_TARGET_KW = 0.4", "BALANCING_SLOW_TARGET_KW = 2.0"),
        "restore_99pct_threshold": lambda s: _replace_after(s, "# Routine reconcile:",
            "(soc_now | float(0)) >= 95", "(soc_now | float(0)) >= 99"),
        "slow_below_95": lambda s: _replace_after(s, "# Routine reconcile:",
            "if slow_latched else", "if true else"),
        "multiply_target_by_count": lambda s: _replace_after(s,
            'name: "Hoymiles Battery Balancing Slow Charge Power"',
            "target / system_power * 100", "(target * machines) / system_power * 100"),
        "omit_load_compensation": lambda s: _replace_after(s,
            'name: "Hoymiles Battery Balancing Slow Charge Power"',
            "(load_power + target) / system_power", "target / system_power"),
        "double_load_compensation": lambda s: _replace_after(s,
            'name: "Hoymiles Battery Balancing Slow Charge Power"',
            "(load_power + target) / system_power",
            "(load_power + load_power + target) / system_power"),
        "round_above_bms": lambda s: _replace_after(s,
            'name: "Hoymiles Battery Balancing Slow Charge Power"',
            "round(0, 'floor')", "round(0, 'ceil')"),
        "positive_fallback": lambda s: _replace_after(s, "# Routine reconcile:",
            "pre_target | float | round(1)", "pre_target | float(20) | round(1)"),
        "minimum_over_bms": lambda s: _replace_after(s,
            'name: "Hoymiles Battery Balancing Slow Charge Power"',
            "if direct_raw >= 0.1 else 0", "if direct_raw >= 0.1 else 0.1"),
        "clear_latch_94_9": lambda s: _replace_after(s, "# Routine reconcile:",
            "entry_state in ['SLOW', 'HOLD_ARMING', 'HOLDING']", "entry_state == 'HOLDING'"),
        "hold_at_95": lambda s: _replace_after(s, "# Routine reconcile:", ">= 99.9", ">= 95"),
        "retain_hold_after_drop": lambda s: _replace_after(s, "# A drop below 99.9%",
            "action: timer.cancel", "action: timer.start"),
        "daylight_grid_charge": lambda s: _replace_after(s, "# Routine reconcile:",
            "'self_use' if is_state('sun.sun', 'above_horizon')",
            "'grid_charge' if is_state('sun.sun', 'above_horizon')"),
        "stop_on_5s_soft_gap": lambda s: _replace_after(s,
            "hoymiles_battery_balancing_write_timing:", "+ 60000", "+ 5000"),
        "reset_soft_gap_deadline": lambda s: _replace_after(s, "hoymiles_battery_balancing_control",
            "timing_state == 'NONE'", "timing_state != 'NONE'"),
        "delay_off_grid": lambda s: _replace_after(s,
            "hoymiles_battery_balancing_off_grid_hard_stop_capture",
            "trigger.to_state.state == 'off_grid'",
            "trigger.to_state.state == 'self_use'"),
        "immediate_restart": lambda s: _replace_after(s,
            "# Restoration or provisional-owner", "+ 900000", "+ 0"),
        "push_every_phase": lambda s: _replace_after(s,
            "hoymiles_apply_battery_balancing_target:", "script.turn_on",
            "script.hoymiles_notify_battery_balancing_lifecycle"),
        "suppress_real_fault": lambda s: _replace_after(s, "expected_balancing_pair: >-",
            "['Czuwanie', 'Test sieci', 'Praca z siecią']",
            "['Czuwanie', 'Test sieci', 'Praca z siecią', 'Awaria']"),
        "duplicate_started": lambda s: _replace_after(s,
            "hoymiles_notify_battery_balancing_lifecycle:",
            "event_id: \"{{ requested_cycle ~ '.' ~ event_kind }}\"",
            "event_id: \"{{ requested_cycle ~ '.' ~ event_kind ~ '.' ~ now() }}\""),
        "duplicate_completed": lambda s: _replace_after(s,
            "hoymiles_notify_battery_balancing_lifecycle:",
            "event_already_present: \"{{ event_id in [slot_1_id, slot_2_id] }}\"",
            "event_already_present: false"),
        "generic_plus_balancing_fault": lambda s: _replace_after(s,
            "expected_balancing_pair: >-", "or exact_terminal_fault_coverage", "or false"),
        "notify_failure_changes_physical": lambda s: _replace_after(s,
            "hoymiles_battery_balancing_notification_provider_attempt:",
            "notify.send_message", "script.hoymiles_verified_set_ems_mode"),
        "skip_previous_restore": lambda s: _replace_after(s, "# Restore 4304.",
            "script.hoymiles_verified_set_ems_maximum_charge_power",
            "script.hoymiles_verified_set_ems_force_charge_soc"),
        "direct_modbus_write": lambda s: _replace_after(s, "# Routine reconcile:",
            "script.hoymiles_verified_set_ems_maximum_charge_power", "modbus.write_register"),
    }
    assert len(mutations) == 25
    detected, survivors = _run_source_mutations(source, mutations)
    assert not survivors, f"Existing mutation survivors: {survivors}"
    return detected, len(survivors)


def test_transactional_mutations() -> tuple[int, int]:
    source = SCHEDULER.read_text(encoding="utf-8")
    validator_source = VALIDATOR.read_text(encoding="utf-8")
    workflow_source = WORKFLOW.read_text(encoding="utf-8")
    mutations: dict[str, Callable[[str], str]] = {
        "permit_legacy_restore": lambda s: _replace_after(s,
            "# Restoration or provisional-owner", "snapshot_valid') | bool(false)",
            "snapshot_valid') | bool(true)"),
        "release_owner_recovery": lambda s: _replace_after(s,
            "hoymiles_battery_balancing_enter_recovery:",
            "action: timer.cancel", "action: input_boolean.turn_off"),
        "trust_old_saved_numbers": lambda s: _replace_after(s, "saved_4304: >-",
            "snapshot_4304') | float(-1)",
            "states('input_number.hoymiles_battery_balancing_saved_charge_power') | float(-1)"),
        "suppress_gap_rearm": lambda s: _replace_after(s, "hoymiles_battery_balancing_control",
            "script.hoymiles_battery_balancing_soft_gap_guard",
            "script.hoymiles_battery_balancing_hold_guard"),
        "reset_gap_deadline": lambda s: _replace_after(s, "hoymiles_battery_balancing_control",
            "{{ (now_epoch_ms | int(0)) + 60000 }}",
            "{{ (as_timestamp(now()) * 1000) | int + 60000 }}"),
        "ignore_backward_clock": lambda s: _replace_after(s, "hoymiles_battery_balancing_control",
            'reason_code: "clock_anomaly"', 'reason_code: "communication_failure"'),
        "duplicate_terminal_notifier": lambda s: _replace_after(s,
            "# Restoration or provisional-owner",
            "action: script.hoymiles_notify_battery_balancing_lifecycle",
            "action: script.hoymiles_notify_battery_balancing_lifecycle\n"
            "                      - action: script.hoymiles_notify_battery_balancing_lifecycle"),
        "restore_old_notice_state": lambda s: _replace_after(s, "prior_event_marker:",
            "'event_marker')", "'event_marker') if false else 'STARTED'"),
        "delivered_before_service": lambda s: _replace_after(s, "next_attempt:",
            'next_delivery_state: "DELIVERING"', 'next_delivery_state: "DELIVERED"'),
        "drop_pending_on_startup": lambda s: _replace_after(s,
            "hoymiles_battery_balancing_notification_delivery", "event: start", "event: shutdown"),
        "notify_before_restore": lambda s: _replace_after(s, "# Restore 4304.",
            "script.hoymiles_verified_set_ems_maximum_charge_power",
            "script.hoymiles_notify_battery_balancing_lifecycle"),
        "single_silent_controller": lambda s: _replace_after(s,
            "  - id: hoymiles_battery_balancing_control", "mode: queued", "mode: single"),
        "remove_tail_recheck": lambda s: _replace_after(s,
            "# Final tail authorization", "not final_transaction_ack", "false"),
        "apply_after_abort": lambda s: _replace_once(s,
            "and not (\n               is_state(\n                 "
            "'sensor.hoymiles_battery_balancing_abort_request', 'PENDING')",
            "and (\n               is_state(\n                 "
            "'sensor.hoymiles_battery_balancing_abort_request', 'PENDING')"),
        "snapshot_before_owner": lambda s: _replace_after(s, "# Routine reconcile:",
            "first_write_pending and not snapshot_still_equal", "false"),
        "accept_bad_field_count": lambda s: _replace_after(s,
            "# Canonical parser for the exact 18-field", "p | count != 18", "p | count != 17"),
        "allow_lifecycle_over_255": lambda s: _replace_after(s,
            "hoymiles_battery_balancing_write_record:",
            "serialized_record | length <= 255", "serialized_record | length <= 300"),
        "second_resolution_cycle_id": lambda s: _replace_after(s,
            "# Only the worker creates a monotonic cycle",
            "next_cycle: \"{{ 'c' ~ next_sequence }}\"",
            "next_cycle: \"{{ as_timestamp(now()) | int }}\""),
        "stale_sun_mode": lambda s: _replace_after(s, "required_mode_after_ack:",
            "required_mode_after_ack != first_mode_request", "false"),
        "timer_before_hold_arming": lambda s: _replace_after(s,
            "# HOLD_ARMING and absolute deadline", 'timing_state: "HOLD_ARMING"',
            'timing_state: "HOLDING"'),
        "restart_full_hold": lambda s: _replace_after(s, "worker_intent == 'hold_reconcile'",
            "- (as_timestamp(now()) * 1000)) / 1000, 1]",
            "- (as_timestamp(now()) * 1000)) / 1000 + 14400, 1]"),
        "notification_affects_closeout": lambda s: _replace_after(s,
            "hoymiles_battery_balancing_notification_provider_attempt:",
            "notify.send_message", "script.hoymiles_verified_set_ems_mode"),
        "release_before_restore_ack": lambda s: _replace_after(s, "# Restore 4304.",
            "final_restore_ack", "input_boolean.turn_off\n                      final_restore_ack"),
        "direct_modbus": lambda s: _replace_after(s, "# Routine reconcile:",
            "script.hoymiles_verified_set_ems_force_charge_soc", "modbus.write_register"),
        "second_physical_worker": lambda s: _replace_after(s,
            "hoymiles_apply_battery_balancing_target:", "script.turn_on",
            "script.hoymiles_verified_set_ems_mode"),
        "reuse_stale_generation": lambda s: _replace_after(s,
            "worker_intent == 'hold_reconcile'", "hold_timing_cycle == entry_cycle",
            "hold_timing_cycle != entry_cycle"),
        "reset_recovery_to_idle": lambda s: _replace_after(s,
            "hoymiles_battery_balancing_enter_recovery:",
            'transaction_state: "RECOVERY_REQUIRED"', 'transaction_state: "IDLE"'),
    }
    assert len(mutations) == 27
    detected, survivors = _run_source_mutations(source, mutations)
    validator_mutations = {
        "remove_focused_existence_check": validator_source.replace(
            'require(test_path.is_file(), "Focused battery-balancing test is missing")',
            'require(True, "Focused battery-balancing test is missing")', 1),
        "accept_stale_packaged_pl": validator_source.replace(
            'actual == expected.encode("utf-8")', "True", 1),
        "accept_ungenerated_semantic": validator_source.replace(
            "validate_managed_asset_freshness(catalog)", "pass", 1),
    }
    for name, mutated in validator_mutations.items():
        assert mutated != validator_source, f"Validator mutation did not alter: {name}"
        try:
            assert_validator_source_contract(mutated, workflow_source)
        except (AssertionError, SyntaxError):
            detected += 1
        else:
            survivors.append(name)
    assert detected + len(survivors) == 30
    assert not survivors, f"Transactional mutation survivors: {sorted(survivors)}"
    return detected, len(survivors)


def test_correction_mutations() -> tuple[int, int]:
    """Detect the complete BAL-R2-F1 plus superseding R2 correction campaign."""
    source = SCHEDULER.read_text(encoding="utf-8")
    validator_source = VALIDATOR.read_text(encoding="utf-8")
    workflow_source = WORKFLOW.read_text(encoding="utf-8")
    runtime_source = RUNTIME.read_text(encoding="utf-8")

    final_mode_action = (
        "- action: script.hoymiles_verified_set_ems_mode\n"
        "                        continue_on_error: true\n"
        "                        data:\n"
        "                          option: \"{{ final_correction_required_mode }}\""
    )
    scheduler_mutations: dict[str, Callable[[str], str]] = {
        "m01_ems_generation_ge": lambda s: _replace_after(
            s, "sensor.hoymiles_hit_ems_control_readback_generation",
            "| int(-1)) == (state_attr(", "| int(-1)) >= (state_attr("),
        "m02_topology_generation_ge": lambda s: _replace_after(
            s, "sensor.hoymiles_hit_parallel_topology_readback_generation",
            "| int(-1)) == (state_attr(", "| int(-1)) >= (state_attr("),
        "m03_remove_ems_generation": lambda s: _replace_after(
            s, "snapshot_still_equal: >-", "snapshot_ems_generation')",
            "snapshot_topology_generation')"),
        "m04_remove_topology_generation": lambda s: _replace_after(
            s, "snapshot_still_equal: >-", "snapshot_topology_generation')",
            "snapshot_ems_generation')"),
        "m05_bms_reason_from_current_state": lambda s: _replace_after(
            s, "frozen_hard_reason: >-",
            "trigger.id in ['bms_fault_code', 'bms_overview_fault']",
            "is_state('sensor.hoymiles_hit_battery_fault_code_bms', 'Brak błędu')"),
        "m06_offgrid_reason_from_current_state": lambda s: _replace_after(
            s, "frozen_off_grid_reason: >-",
            "trigger.id == 'ems_off_grid_priority'",
            "is_state('sensor.hoymiles_ems_hardware_mode', 'off_grid')"),
        "m07_recovery_clears_abort": lambda s: _replace_after(
            s, "hoymiles_battery_balancing_hard_stop_capture",
            'not_to: "Brak błędu"', 'to: "Brak błędu"'),
        "m08_only_fault_dropped_at_saturation": lambda s: _replace_after(
            s, "hoymiles_battery_balancing_hard_stop_capture",
            "max: 100", "max: 1"),
        "m09_remove_provider_timeout": lambda s: _replace_after(
            s, "hoymiles_battery_balancing_notification_dispatcher:",
            "script.hoymiles_battery_balancing_notification_attempt_timeout",
            "script.hoymiles_battery_balancing_notification_provider_attempt"),
        "m10_reset_lease_on_restart": lambda s: _replace_after(
            s, "hoymiles_battery_balancing_recover_notification_leases:",
            "'slot_1_attempt_count') }}", "'slot_1_attempt_count') if false else 0 }}"),
        "m11_accept_stale_provider_completion": lambda s: _replace_after(
            s, "hoymiles_battery_balancing_notification_provider_attempt:",
            'expected_attempt_count: "{{ leased_generation }}"',
            'expected_attempt_count: 0'),
        "m12_unbounded_provider_workers": lambda s: _replace_after(
            s, "hoymiles_battery_balancing_notification_provider_attempt:",
            "max: 6", "max: 600"),
        "m13_reuse_old_soc_before_hold": lambda s: _replace_after(
            s, "hold_soc_value: >-",
            "{{ states('sensor.hoymiles_hit_overview_battery_soc') }}",
            "{{ soc_now }}"),
        "m14_arm_hold_at_99_8": lambda s: _replace_after(
            s, "hold_soc_value: >-", ">= 99.9", ">= 99.8"),
        "m15_operational_fact_needs_free_outbox": lambda s: _replace_after(
            s, "# The physical operational fact",
            "and not started_already\n                     and not (is_state(",
            "and not started_already\n"
            "                     and states('sensor.hoymiles_battery_balancing_notification_outbox') == 'VALID'\n"
            "                     and not (is_state("),
        "m16_full_outbox_abort_becomes_failed": lambda s: _replace_after(
            s, "terminal_event: >-", "{% elif terminal_was_started %}aborted",
            "{% elif prior_event_marker == 'STARTED' %}aborted"),
        "m22_add_raw_parser_bypass": lambda s: _replace_once(
            s, "worker_intent: \"{{ intent | default('reconcile', true) | string }}\"",
            "worker_intent: \"{{ states('input_text.hoymiles_battery_balancing_lifecycle').split('|')[2] }}\""),
        "m27_remove_final_current_sun_read": lambda s: _replace_after(
            s, "current_commit_required_mode: >-",
            "'self_use' if is_state('sun.sun', 'above_horizon')\n               else 'grid_charge'",
            "'grid_charge'"),
        "m28_reuse_desired_mode_at_commit": lambda s: _replace_after(
            s, "current_commit_required_mode: >-",
            "'self_use' if is_state('sun.sun', 'above_horizon')\n               else 'grid_charge'",
            "desired_mode"),
        "m29_allow_daylight_grid_charge": lambda s: _replace_after(
            s, "current_commit_required_mode: >-", "'self_use' if is_state(",
            "'grid_charge' if is_state("),
        "m30_allow_night_self_use": lambda s: _replace_after(
            s, "current_commit_required_mode: >-", "else 'grid_charge' }}",
            "else 'self_use' }}"),
        "m31_second_sun_correction_loop": lambda s: _replace_after(
            s, "# The final steady-phase service re-reads sun",
            final_mode_action, final_mode_action + "\n                      " + final_mode_action),
        "m32_skip_hardstop_during_correction": lambda s: _replace_after(
            s, "final_mode_after_correction: >-", "{{ is_state(\n",
            "{{ false and is_state(\n"),
        "m37_bypass_post_apply_snapshot_guard": lambda s: _replace_after(
            s, "snapshot_equal_after_apply_arm: >-",
            "and not snapshot_equal_after_apply_arm", "and false"),
        "m38_remove_post_apply_ems_generation": lambda s: _replace_after(
            s, "snapshot_equal_after_apply_arm: >-",
            "snapshot_ems_generation')", "snapshot_topology_generation')"),
        "m39_wake_worker_before_fault_queue_drains": lambda s: _replace_after(
            s, "  - id: hoymiles_battery_balancing_hard_stop_capture",
            "wake_worker: false", "wake_worker: true"),
        "m40_ignore_general_fault_queue_depth": lambda s: _replace_after(
            s, "  - id: hoymiles_battery_balancing_hard_stop_capture",
            "state_attr(this.entity_id, 'current')", "0"),
        "m41_bypass_serializer_hold_guard": lambda s: _replace_after(
            s, "hoymiles_battery_balancing_write_timing:",
            "and hold_arm_authorized", "and true"),
        "m42_serializer_arms_at_99_8": lambda s: _replace_after(
            s, "hold_soc_at_serializer: >-", ">= 99.9", ">= 99.8"),
        "m43_bypass_initial_post_commit_guard": lambda s: _replace_after(
            s, "final_phase_commit_valid: >-",
            "{{ not final_phase_commit_valid }}", "{{ false }}"),
        "m44_bypass_post_correction_commit_guard": lambda s: _replace_after(
            s, "final_phase_commit_valid_after_attempt: >-",
            "{{ not final_phase_commit_valid_after_attempt }}", "{{ false }}"),
        "m45_bypass_started_post_commit_guard": lambda s: _replace_after(
            s, "started_commit_valid: >-",
            "{{ not started_commit_valid }}", "{{ false }}"),
        "m49_bypass_inner_steady_writer_guard": lambda s: _replace_after(
            s, "record_steady_commit_authorized: >-",
            "record_valid and record_steady_commit_authorized",
            "record_valid and true"),
        "m50_accept_stale_soc_at_serializer": lambda s: _replace_after(
            s, "hold_soc_age_at_serializer: >-", "<= 120", "<= 1200"),
        "m52_persist_raw_steady_phase": lambda s: _replace_after(
            s, "record_persisted_state: >-",
            "'APPLYING' if record_guard_steady_commit else record_state",
            "record_state"),
        "m53_bypass_conditional_commit_parser": lambda s: _replace_after(
            s, "conditional_commit_current = conditional_phase is not none",
            "if conditional_commit_current else p[2]",
            "if conditional_phase is not none else p[2]"),
        "m54_remove_post_apply_service_yield": lambda s: _replace_after(
            s, "# Yield once so every callback fired by the inner input_text service",
            "- delay: 0", "- delay: 1"),
        "m55_bypass_post_timing_hold_guard": lambda s: _replace_after(
            s, "hold_arm_after_timing_valid: >-",
            "{{ not hold_arm_after_timing_valid }}", "{{ false }}"),
        "m56_remove_p95_priority_lane": lambda s: s.replace(
            "  - id: hoymiles_battery_balancing_p95_hard_stop_capture",
            "  - id: hoymiles_battery_balancing_p95_hard_stop_capture_removed",
            1),
        "m57_reduce_p90_priority_lane_bound": lambda s: _replace_after(
            s, "  - id: hoymiles_battery_balancing_p90_hard_stop_capture",
            "max: 100", "max: 1"),
        "m58_equal_priority_last_reason_wins": lambda s: _replace_after(
            s, "current_priority: >-",
            "requested_priority | int(-1) > current_priority",
            "requested_priority | int(-1) >= current_priority"),
        "m59_mirror_stale_steady_phase": lambda s: _replace_after(
            s, "# Compatibility mirror only; every authority decision uses the b2 parser.",
            'value: "{{ record_persisted_state }}"',
            'value: "{{ record_state }}"'),
        "m60_bypass_conditional_hold_timing": lambda s: _replace_after(
            s, "conditional_timing_ok =",
            "and conditional_timing_ok", "and true"),
    }
    assert len(scheduler_mutations) == 43
    detected, survivors = _run_source_mutations(source, scheduler_mutations)

    validator_mutations = {
        "m17_remove_runtime_raw_hash": validator_source.replace(
            "hashlib.sha256(runtime_bytes).hexdigest()\n"
            "        == EXPECTED_BALANCING_RUNTIME_SHA256", "True", 1),
        "m18_remove_runtime_ast_hash": validator_source.replace(
            "reviewed_ast_sha256(runtime_tree)\n"
            "        == EXPECTED_BALANCING_RUNTIME_AST_SHA256", "True", 1),
        "m19_remove_runtime_race_origin": validator_source.replace(
            "assignments.get(result_name) == function_name", "True", 1),
        "m21_remove_runtime_existence": validator_source.replace(
            'require(runtime_path.is_file(), "Exact HA battery-balancing runtime test is missing")',
            'require(True, "Exact HA battery-balancing runtime test is missing")', 1),
        "m23_remove_scheduler_semantic_freeze": validator_source.replace(
            "validate_balancing_scheduler_freeze(ems_package)", "pass", 1),
        "m24_accept_stale_pl_scheduler": validator_source.replace(
            'actual == expected.encode("utf-8")', "True", 1),
        "m36_command_text_only": validator_source.replace(
            'require_unconditional(step, f"step {job_name}/{command}")', "pass", 1),
        "m46_ignore_legacy_service_writer": validator_source.replace(
            'mapping.get("action", mapping.get("service"))',
            'mapping.get("action")', 1),
        "m47_disable_tainted_alias_parser": validator_source.replace(
            "parses_alias(text, alias) for text in texts for alias in tainted",
            "parses_alias(text, alias) for text in texts for alias in ()", 1),
        "m48_ignore_direct_split_filter": validator_source.replace(
            'or re.search(r"\\|\\s*split\\s*\\(", text)',
            "or False", 1),
        "m51_ignore_list_form_lifecycle_writer": validator_source.replace(
            "if helper in entity_ids", "if entity_id == helper", 1),
    }
    for name, mutated in validator_mutations.items():
        assert mutated != validator_source, f"Validator correction mutation did not alter: {name}"
        try:
            assert_validator_source_contract(mutated, workflow_source)
        except (AssertionError, SyntaxError):
            detected += 1
        else:
            survivors.append(name)

    literal_race = runtime_source.replace(
        "races = asyncio.run(test_hard_stop_race_matrix(package))",
        "races = 15",
        1,
    )
    assert literal_race != runtime_source
    try:
        assert_runtime_source_contract(literal_race)
    except (AssertionError, SyntaxError):
        detected += 1
    else:
        survivors.append("m20_literal_runtime_race_result")

    readme = README.read_text(encoding="utf-8")
    readme_pl = README_PL.read_text(encoding="utf-8")
    releasing = RELEASING.read_text(encoding="utf-8")
    documentation_mutations = {
        "m25_false_immediate_hardstop_claim": (
            readme.replace(
                "An already running verified helper call cannot be cancelled",
                "A hard-stop immediately cancels an in-flight helper",
                1,
            ),
            readme_pl,
            releasing,
        ),
        "m26_merge_python_paths": (
            readme,
            readme_pl,
            releasing.replace(
                "Python 3.12 — structural/offline gate",
                "Python 3.12/3.14.7 — combined gate",
                1,
            ),
        ),
    }
    for name, (mutated_readme, mutated_pl, mutated_releasing) in documentation_mutations.items():
        assert (mutated_readme, mutated_pl, mutated_releasing) != (
            readme, readme_pl, releasing
        ), f"Documentation mutation did not alter: {name}"
        try:
            assert_documentation_contract(mutated_readme, mutated_pl, mutated_releasing)
        except AssertionError:
            detected += 1
        else:
            survivors.append(name)

    validator = _load_validator()
    workflow_mutations = {
        "m33_focused_step_if_false": (
            "project-checks", "python tools/test_battery_balancing_contract.py", "step", False
        ),
        "m34_runtime_step_expression_false": (
            "battery-balancing-ha-runtime",
            "python tools/test_battery_balancing_ha_runtime.py",
            "step",
            "${{ false }}",
        ),
        "m35_containing_job_false": (
            "project-checks", "python tools/test_battery_balancing_contract.py", "job", 0
        ),
    }
    for name, (job_name, command, scope, condition) in workflow_mutations.items():
        mutated = yaml.safe_load(workflow_source)
        job = mutated["jobs"][job_name]
        if scope == "job":
            job["if"] = condition
        else:
            step = next(
                item for item in job["steps"]
                if str(item.get("run", "")).strip() == command
            )
            step["if"] = condition
        try:
            validator.validate_mandatory_workflow_step(
                mutated, command, job_name=job_name
            )
        except RuntimeError:
            detected += 1
        else:
            survivors.append(name)

    assert detected + len(survivors) == 60
    assert not survivors, f"Correction mutation survivors: {sorted(survivors)}"
    return detected, len(survivors)


def main() -> None:
    power = test_power_contract()
    soc = test_soc_contract()
    lifecycle = test_production_lifecycle_contract()
    notifications = test_notification_contract()
    validator = test_validator_contract()
    existing, old_survivors = test_existing_mutations()
    transactional, new_survivors = test_transactional_mutations()
    correction, correction_survivors = test_correction_mutations()
    survivors = old_survivors + new_survivors + correction_survivors
    print("Battery-balancing production contract: PASS "
          f"(power={power}, soc={soc}, lifecycle={lifecycle}, "
          f"notifications={notifications}, validator={validator}, "
          f"lifecycle_max_serialized_length={_maximum_lifecycle_length()})")
    print(f"Existing implementation mutations: {existing}/25 detected; "
          f"transactional mutations: {transactional}/30 detected; "
          f"correction mutations: {correction}/60 detected; survivors={survivors}")


if __name__ == "__main__":
    main()
