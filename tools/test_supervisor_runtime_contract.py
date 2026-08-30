"""Focused deterministic tests for Supervisor Phase 1B-1 normalization."""

from __future__ import annotations

import ast
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
from itertools import permutations
import json
from pathlib import Path
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
SOURCE_PATH = COMPONENT / "supervisor_runtime.py"
sys.path.insert(0, str(COMPONENT))

import supervisor_runtime as runtime_module  # noqa: E402

from ems_supervisor import (  # noqa: E402
    ActuatorScope,
    EconomicValueStatus,
    ExportState,
    MAX_CANDIDATE_SUMMARY_BYTES,
    MAX_SUPERVISOR_SUMMARY_BYTES,
    NeedClass,
    OwnerKind,
    PhysicalMode,
    PolicyId,
    PriorityClass,
    ReasonCode,
    RequestedAction,
    SupervisorMode,
    SupervisorProfile,
    SupervisorState,
    arbitrate_supervisor,
    serialize_supervisor_summary,
)
from supervisor_runtime import (  # noqa: E402
    ExecutionSourceSnapshot,
    NormalizedOwner,
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
    normalize_export_state,
    normalize_owner,
)


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
HUGE_INT = 10**10000
CHECK_COUNT = 0


def check(condition: bool, message: str) -> None:
    global CHECK_COUNT
    CHECK_COUNT += 1
    if not condition:
        raise AssertionError(message)


def check_raises(
    exception: type[BaseException],
    function: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> None:
    global CHECK_COUNT
    CHECK_COUNT += 1
    try:
        function(*args, **kwargs)
    except exception:
        return
    raise AssertionError(f"Expected {exception.__name__}")


def canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {
            str(key): canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (tuple, list)):
        return [canonical_value(item) for item in value]
    if isinstance(value, float) and value == 0.0:
        return 0.0
    return value


def payload_hash(value: Any) -> str:
    serialized = json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def rce_source(**overrides: Any) -> RceSourceSnapshot:
    values: dict[str, Any] = {
        "observed_at": NOW - timedelta(seconds=1),
        "allowed_by_user": True,
        "enabled": True,
        "active_latched": False,
        "status_code": RcePlanStatus.READY,
        "result_current": True,
        "recalculation_pending": False,
        "input_revision": 11,
        "current_slot_planned": True,
        "current_slot_start_eligible": True,
        "current_slot_continue_eligible": True,
        "current_slot_end": NOW + timedelta(minutes=30),
        "current_run_end": NOW + timedelta(hours=1),
        "requested_discharge_power_kw": 4.0,
        "planned_export_energy_kwh": 2.0,
        "protected_soc_floor_percent": 25.0,
        "effective_discharge_power_percent": 40.0,
        "current_soc_percent": 70.0,
        "control_data_ready": True,
        "price_above_threshold": True,
        "reserve_ready": True,
        "sale_block_active": False,
        "latched_slot_end": NOW + timedelta(minutes=45),
        "latched_minimum_soc_percent": 30.0,
        "active_4305_readback_percent": 30.0,
        "active_4306_readback_percent": 40.0,
    }
    values.update(overrides)
    return RceSourceSnapshot(**values)


def tariff_source(**overrides: Any) -> TariffSourceSnapshot:
    values: dict[str, Any] = {
        "observed_at": NOW - timedelta(seconds=1),
        "allowed_by_user": True,
        "enabled": True,
        "active_latched": False,
        "status_code": TariffPlanStatus.READY,
        "result_current": True,
        "recalculation_pending": False,
        "input_revision": 12,
        "current_slot_planned": True,
        "current_action": TariffAction.BATTERY_CHARGE,
        "current_run_need_class": TariffRunNeed.ECONOMIC,
        "current_run_start_eligible": True,
        "current_run_continue_eligible": True,
        "requested_charge_power_kw": 5.0,
        "command_charge_power_percent": 50.0,
        "current_run_grid_import_kwh": 4.0,
        "current_run_benefit_pln": 1.25,
        "target_soc_percent": 70.0,
        "base_reserve_soc_percent": 25.0,
        "current_slot_end": NOW + timedelta(hours=1),
        "active_action": TariffAction.GRID_SUPPORT_AND_CHARGE,
        "latched_slot_end": NOW + timedelta(minutes=50),
        "latched_target_soc_percent": 75.0,
        "control_data_ready": True,
        "planned_slot_ready": True,
        "active_4303_readback_percent": 75.0,
        "active_4304_readback_percent": 50.0,
    }
    values.update(overrides)
    return TariffSourceSnapshot(**values)


def rcm_source(**overrides: Any) -> RcmSourceSnapshot:
    values: dict[str, Any] = {
        "observed_at": NOW - timedelta(seconds=1),
        "allowed_by_user": True,
        "enabled": True,
        "result_current": True,
        "recalculation_pending": False,
        "input_revision": 13,
        "live_emergency": False,
        "emergency_action_ready": False,
        "prediction_ready": True,
        "action": RcmAction.ABSORB_PV,
        "risk_window_active": True,
        "voltage_risk_score_percent": 65.0,
        "recommended_charge_limit_percent": 55.0,
        "recommended_charge_power_kw": 5.5,
        "recommended_export_limit_percent": 35.0,
        "current_export_limit_percent": 60.0,
        "current_export_limit_fresh": True,
        "charge_path_locally_valid": True,
        "export_path_locally_valid": True,
        "direct_register_topology_allowed": True,
        "full_block_topology_allowed": True,
        "export_control_enabled": True,
        "pre_discharge_enabled": True,
        "absorb_active": False,
        "export_active": False,
        "pre_discharge_active": False,
        "pre_discharge_start_eligible": True,
        "pre_discharge_continue_eligible": False,
        "pre_discharge_deadline": NOW + timedelta(hours=2),
        "pre_discharge_target_soc_percent": 45.0,
        "pre_discharge_power_kw": 4.0,
        "pre_discharge_power_percent": 40.0,
        "planned_grid_discharge_kwh": 3.0,
        "target_soc_before_risk_percent": 60.0,
        "protected_minimum_soc_percent": 25.0,
        "latched_pre_discharge_deadline": NOW + timedelta(hours=3),
        "latched_pre_discharge_target_soc_percent": 42.0,
        "latched_pre_discharge_power_kw": 3.5,
        "latched_pre_discharge_power_percent": 35.0,
        "sale_block_active": False,
        "export_state": ExportState.VERIFIED_ALLOWED,
    }
    values.update(overrides)
    return RcmSourceSnapshot(**values)


def execution_source(**overrides: Any) -> ExecutionSourceSnapshot:
    values: dict[str, Any] = {
        "physical_mode_code": 0,
        "full_block_generation_at": NOW - timedelta(seconds=1),
        "full_block_execution_ready": True,
        "direct_306_execution_ready": True,
        "direct_259_execution_ready": True,
        "machine_type_code": 0,
        "inverter_count": 1,
        "topology_generation_at": NOW - timedelta(seconds=1),
        "battery_soc_percent": 50.0,
        "battery_soc_observed_at": NOW - timedelta(seconds=1),
        "bms_voltage_v": 50.0,
        "bms_voltage_observed_at": NOW - timedelta(seconds=1),
        "bms_max_charge_current_a": 100.0,
        "bms_charge_current_observed_at": NOW - timedelta(seconds=1),
        "bms_max_discharge_current_a": 100.0,
        "bms_discharge_current_observed_at": NOW - timedelta(seconds=1),
        "balancing_active": False,
        "manual_charge_active": False,
        "manual_discharge_active": False,
        "rce_active": False,
        "tariff_active": False,
        "rcm_active": False,
        "rcm_export_control_active": False,
        "rcm_pre_discharge_active": False,
        "charge_timer_active": False,
        "discharge_timer_active": False,
        "gcf_enable_code": 0,
        "effective_export_limit_percent": 100.0,
        "gcf_generation_at": NOW - timedelta(seconds=1),
        "gcf_cohort_coherent": True,
        "hardware_readback_supported": True,
    }
    values.update(overrides)
    return ExecutionSourceSnapshot(**values)


def context_for(owner: OwnerKind = OwnerKind.NONE):
    context = build_execution_context(execution_source(), now=NOW)
    return replace(context, owner_kind=owner)


def assert_core_accepts(candidate, *, owner: OwnerKind = OwnerKind.NONE) -> None:
    decision = arbitrate_supervisor(
        mode=SupervisorMode.SHADOW,
        profile=SupervisorProfile.BALANCED,
        context=context_for(owner),
        candidates=(candidate,),
        now=NOW,
    )
    check(
        decision.state is not SupervisorState.BLOCKED,
        f"Core rejected {candidate.policy_id.value} structurally",
    )
    check(
        len(decision.candidate_summaries) == 1,
        "Core did not serialize the normalized candidate",
    )


def test_structure_and_static_safety() -> None:
    expected_public = (
        "ExecutionSourceSnapshot",
        "NormalizedOwner",
        "RcePlanStatus",
        "RceSourceSnapshot",
        "RcmAction",
        "RcmSourceSnapshot",
        "TariffAction",
        "TariffPlanStatus",
        "TariffRunNeed",
        "TariffSourceSnapshot",
        "build_execution_context",
        "build_rce_candidate",
        "build_rcm_candidate",
        "build_tariff_candidate",
        "normalize_export_state",
        "normalize_owner",
    )
    check(runtime_module.__all__ == expected_public, "Unexpected public runtime API")
    expected_rce_source_fields = (
        "observed_at",
        "allowed_by_user",
        "enabled",
        "active_latched",
        "status_code",
        "result_current",
        "recalculation_pending",
        "input_revision",
        "current_slot_planned",
        "current_slot_start_eligible",
        "current_slot_continue_eligible",
        "current_slot_end",
        "current_run_end",
        "requested_discharge_power_kw",
        "planned_export_energy_kwh",
        "protected_soc_floor_percent",
        "effective_discharge_power_percent",
        "current_soc_percent",
        "control_data_ready",
        "price_above_threshold",
        "reserve_ready",
        "sale_block_active",
        "latched_slot_end",
        "latched_minimum_soc_percent",
        "active_4305_readback_percent",
        "active_4306_readback_percent",
    )
    check(
        tuple(item.name for item in fields(RceSourceSnapshot))
        == expected_rce_source_fields,
        "RCE source schema contains an unexpected dormant channel",
    )
    check(
        tuple(item.value for item in RcePlanStatus)
        == ("ready", "waiting_for_market", "home_protected"),
        "RCE plan status schema changed",
    )
    check(
        tuple(
            item.value
            for item in RequestedAction
            if item.name.startswith("RCE")
        )
        == ("rce_export",),
        "RCE action schema permits something other than export",
    )
    for snapshot_type in (
        RceSourceSnapshot,
        TariffSourceSnapshot,
        RcmSourceSnapshot,
        ExecutionSourceSnapshot,
        NormalizedOwner,
    ):
        check(snapshot_type.__dataclass_params__.frozen, "Snapshot is mutable")
        check(hasattr(snapshot_type, "__slots__"), "Snapshot is not slotted")

    source = SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SOURCE_PATH))
    disallowed_imports = {
        "aiohttp",
        "asyncio",
        "homeassistant",
        "http",
        "os",
        "pathlib",
        "pymodbus",
        "requests",
        "socket",
        "subprocess",
        "time",
        "urllib",
    }
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    check(not imported_roots & disallowed_imports, "Runtime imports external I/O")
    check(
        not any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree)),
        "Runtime contains asynchronous code",
    )
    forbidden_calls: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in {
            "__import__",
            "eval",
            "exec",
            "hash",
            "input",
            "open",
        }:
            forbidden_calls.append(node.func.id)
        if isinstance(node.func, ast.Attribute) and node.func.attr in {
            "now",
            "utcnow",
            "request",
            "send",
            "write_register",
            "write_registers",
        }:
            forbidden_calls.append(node.func.attr)
    check(not forbidden_calls, f"Runtime contains forbidden calls: {forbidden_calls}")
    lowered = source.casefold()
    for marker in (
        "homeassistant",
        "restoreentity",
        "pymodbus",
        "market_import",
        "rce_charge",
        "grant",
        "handover",
        "owner acquisition",
    ):
        check(marker not in lowered, f"Forbidden static marker present: {marker}")
    for marker in (
        "rce_market_charge",
        "rce_grid_charge",
        "dynamic_import_price",
        "market_import_price",
        "buy_low",
        "charge_from_rce",
        "rce_charge_target",
        "rce_charge_enabled",
    ):
        check(marker not in lowered, f"Dormant RCE charge surface exists: {marker}")
    rce_builder = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "build_rce_candidate"
    )
    rce_actions = {
        node.attr
        for node in ast.walk(rce_builder)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "RequestedAction"
    }
    check(
        rce_actions == {"RCE_EXPORT"},
        f"RCE builder action surface changed: {sorted(rce_actions)}",
    )
    check("tariff_optimizer" not in source, "Adapter contains schedule mathematics")
    check("planned_charges" not in source, "Adapter consumes an unbounded plan")
    check('digest[:8], byteorder="big", signed=False' in source, "Wrong revision extraction")
    snapshot_fields = {
        item.name
        for snapshot_type in (
            RceSourceSnapshot,
            TariffSourceSnapshot,
            RcmSourceSnapshot,
            ExecutionSourceSnapshot,
        )
        for item in fields(snapshot_type)
    }
    for unbounded in ("slots", "prices", "forecast", "weather", "history"):
        check(
            not any(unbounded in name for name in snapshot_fields),
            f"Unbounded snapshot field present: {unbounded}",
        )


def test_rce_mapping() -> None:
    source = rce_source()
    candidate = build_rce_candidate(source, now=NOW)
    check(candidate.policy_id is PolicyId.RCE, "Wrong RCE identity")
    check(candidate.priority_class is PriorityClass.ECONOMIC, "Wrong RCE priority")
    check(candidate.need_class is NeedClass.OPTIONAL, "Wrong RCE need")
    check(candidate.requested_action is RequestedAction.RCE_EXPORT, "Wrong RCE action")
    check(
        candidate.actuator_scope is ActuatorScope.EMS_BLOCK_4300_4306,
        "Wrong RCE scope",
    )
    check(candidate.requested_mode is PhysicalMode.GRID_DISCHARGE, "Wrong RCE mode")
    check(candidate.start_eligible, "Valid RCE start was rejected")
    check(candidate.valid_until == source.current_run_end, "RCE run end was lost")
    expected = payload_hash(
        {
            "policy": PolicyId.RCE,
            "action": RequestedAction.RCE_EXPORT,
            "actuator_scope": ActuatorScope.EMS_BLOCK_4300_4306,
            "mode": 5,
            "target_4305": 25.0,
            "target_4306_percent": 40.0,
            "valid_until": source.current_run_end,
        }
    )
    check(candidate.desired_actuator_fingerprint == expected, "Wrong RCE fingerprint")
    check(
        (
            candidate.policy_id,
            candidate.requested_action,
            candidate.actuator_scope,
            candidate.requested_mode,
            candidate.economic_value_status,
        )
        == (
            PolicyId.RCE,
            RequestedAction.RCE_EXPORT,
            ActuatorScope.EMS_BLOCK_4300_4306,
            PhysicalMode.GRID_DISCHARGE,
            EconomicValueStatus.UNAVAILABLE,
        ),
        "RCE builder output shape changed",
    )
    check(
        candidate.economic_value_status is EconomicValueStatus.UNAVAILABLE
        and candidate.economic_contract_id is None
        and candidate.economic_basis_fingerprint is None
        and candidate.expected_marginal_net_benefit_pln is None,
        "RCE economics were invented",
    )
    assert_core_accepts(candidate)

    no_action = build_rce_candidate(
        rce_source(current_slot_planned=False),
        now=NOW,
    )
    check(no_action.requested_action is RequestedAction.NONE, "RCE no-action failed")
    check(no_action.actuator_scope is ActuatorScope.NONE, "RCE no-action has scope")
    check(no_action.available, "Valid empty RCE plan became unavailable")
    assert_core_accepts(no_action)
    short_slot = build_rce_candidate(
        rce_source(
            current_slot_end=NOW + timedelta(seconds=299),
            current_run_end=NOW + timedelta(hours=1),
        ),
        now=NOW,
    )
    check(not short_slot.start_eligible, "RCE ignored its minimum slot time")
    check(
        short_slot.requested_action is RequestedAction.RCE_EXPORT,
        "RCE timing gate erased a truthful action",
    )
    for invalid in (
        rce_source(observed_at=None),
        rce_source(observed_at=NOW - timedelta(seconds=301)),
        rce_source(observed_at=NOW + timedelta(microseconds=1)),
        rce_source(current_slot_planned=1),
        rce_source(requested_discharge_power_kw="4"),
    ):
        rejected = build_rce_candidate(invalid, now=NOW)
        check(rejected.requested_action is RequestedAction.NONE, "Invalid RCE acted")
        check(not rejected.available, "Invalid RCE remained available")
        assert_core_accepts(rejected)

    latched_end = NOW + timedelta(minutes=42)
    active_source = rce_source(
        active_latched=True,
        result_current=False,
        recalculation_pending=True,
        current_run_end=NOW + timedelta(hours=4),
        protected_soc_floor_percent=10.0,
        latched_slot_end=latched_end,
        latched_minimum_soc_percent=30.0,
        active_4305_readback_percent=30.0,
    )
    active = build_rce_candidate(active_source, now=NOW)
    check(active.valid_until == latched_end, "RCE did not prefer frozen end")
    check(active.protected_soc_floor_percent == 30.0, "RCE did not prefer frozen floor")
    check(active.continuation_eligible, "Benign pending stopped RCE continuation")
    assert_core_accepts(active, owner=OwnerKind.RCE)
    stopped = build_rce_candidate(
        replace(active_source, sale_block_active=True),
        now=NOW,
    )
    check(stopped.local_hard_stop, "RCE local hard stop was lost")
    check(not stopped.continuation_eligible, "RCE continued through hard stop")
    assert_core_accepts(stopped, owner=OwnerKind.RCE)

    refreshed = build_rce_candidate(
        replace(source, observed_at=NOW - timedelta(seconds=2)),
        now=NOW,
    )
    changed = build_rce_candidate(
        replace(source, effective_discharge_power_percent=41.0),
        now=NOW,
    )
    check(
        refreshed.candidate_revision == candidate.candidate_revision,
        "RCE observed_at churned candidate revision",
    )
    check(
        changed.candidate_revision != candidate.candidate_revision
        and changed.desired_actuator_fingerprint
        != candidate.desired_actuator_fingerprint,
        "RCE target change did not alter revision and fingerprint",
    )
    check(
        not any("charge" in member.value for member in RequestedAction if member.name.startswith("RCE")),
        "RCE import action exists",
    )


def test_tariff_mapping() -> None:
    economic_source = tariff_source()
    economic = build_tariff_candidate(economic_source, now=NOW)
    check(economic.priority_class is PriorityClass.ECONOMIC, "Wrong economic priority")
    check(economic.need_class is NeedClass.OPTIONAL, "Wrong economic need")
    check(economic.requested_action is RequestedAction.TARIFF_CHARGE, "Wrong tariff action")
    check(
        economic.economic_value_status is EconomicValueStatus.PROVISIONAL,
        "Tariff value is not provisional",
    )
    check(economic.expected_marginal_net_benefit_pln == 1.25, "Tariff benefit lost")
    finite_negative = build_tariff_candidate(
        tariff_source(current_run_benefit_pln=-0.25),
        now=NOW,
    )
    check(
        finite_negative.expected_marginal_net_benefit_pln == -0.25,
        "Finite tariff benefit was discarded",
    )
    check(
        economic.economic_contract_id is None
        and economic.economic_basis_fingerprint is None,
        "Tariff comparison contract was invented",
    )
    expected = payload_hash(
        {
            "policy": PolicyId.TARIFF,
            "action": RequestedAction.TARIFF_CHARGE,
            "actuator_scope": ActuatorScope.EMS_BLOCK_4300_4306,
            "mode": 4,
            "active_action": TariffAction.BATTERY_CHARGE,
            "target_4303": 70.0,
            "target_4304_percent": 50.0,
            "valid_until": economic_source.current_slot_end,
        }
    )
    check(economic.desired_actuator_fingerprint == expected, "Wrong tariff fingerprint")
    assert_core_accepts(economic)

    required = build_tariff_candidate(
        tariff_source(current_run_need_class=TariffRunNeed.REQUIRED_ENERGY),
        now=NOW,
    )
    check(required.priority_class is PriorityClass.REQUIRED_ENERGY, "Wrong required priority")
    check(required.need_class is NeedClass.MANDATORY, "Wrong required need")
    check(
        required.economic_value_status is EconomicValueStatus.UNAVAILABLE
        and required.expected_marginal_net_benefit_pln is None,
        "Required tariff candidate gained economics",
    )
    assert_core_accepts(required)

    for invalid_need in (TariffRunNeed.MIXED, None, "invalid"):
        rejected = build_tariff_candidate(
            tariff_source(current_run_need_class=invalid_need),
            now=NOW,
        )
        check(rejected.requested_action is RequestedAction.NONE, "Ambiguous tariff acted")
        check(not rejected.available, "Ambiguous tariff remained available")
        assert_core_accepts(rejected)
    none = build_tariff_candidate(
        tariff_source(
            current_slot_planned=False,
            current_action=TariffAction.NONE,
            current_run_need_class=TariffRunNeed.NONE,
        ),
        now=NOW,
    )
    check(none.requested_action is RequestedAction.NONE, "Tariff none was not no-action")
    check(none.available, "Valid tariff none became unavailable")
    assert_core_accepts(none)

    active_source = tariff_source(
        active_latched=True,
        active_action=TariffAction.GRID_SUPPORT_AND_CHARGE,
        target_soc_percent=55.0,
        current_slot_end=NOW + timedelta(hours=3),
        latched_target_soc_percent=75.0,
        latched_slot_end=NOW + timedelta(minutes=50),
        active_4303_readback_percent=75.0,
    )
    active = build_tariff_candidate(active_source, now=NOW)
    check(active.target_soc_percent == 75.0, "Tariff frozen SOC was not preferred")
    check(
        active.valid_until == active_source.latched_slot_end,
        "Tariff frozen end was not preferred",
    )
    check(active.continuation_eligible, "Valid tariff continuation was blocked")
    assert_core_accepts(active, owner=OwnerKind.TARIFF)

    for invalid in (
        tariff_source(observed_at=NOW - timedelta(seconds=301)),
        tariff_source(observed_at=NOW + timedelta(microseconds=1)),
    ):
        rejected = build_tariff_candidate(invalid, now=NOW)
        check(rejected.requested_action is RequestedAction.NONE, "Stale tariff acted")
        check(not rejected.available, "Stale tariff remained available")


def test_numeric_totality_and_revision_semantics() -> None:
    huge_rce = build_rce_candidate(
        rce_source(requested_discharge_power_kw=HUGE_INT),
        now=NOW,
    )
    check(
        not huge_rce.available
        and huge_rce.requested_action is RequestedAction.NONE,
        "Huge RCE power did not fail closed",
    )
    huge_tariff_energy = build_tariff_candidate(
        tariff_source(current_run_grid_import_kwh=HUGE_INT),
        now=NOW,
    )
    check(
        not huge_tariff_energy.available
        and huge_tariff_energy.requested_action is RequestedAction.NONE,
        "Huge tariff energy did not fail closed",
    )
    huge_tariff_benefit = build_tariff_candidate(
        tariff_source(current_run_benefit_pln=HUGE_INT),
        now=NOW,
    )
    check(
        huge_tariff_benefit.requested_action is RequestedAction.TARIFF_CHARGE
        and huge_tariff_benefit.expected_marginal_net_benefit_pln is None,
        "Huge diagnostic tariff benefit was not safely discarded",
    )
    for invalid_rcm in (
        rcm_source(recommended_charge_limit_percent=HUGE_INT),
        rcm_source(recommended_charge_power_kw=HUGE_INT),
    ):
        candidate = build_rcm_candidate(invalid_rcm, now=NOW)
        check(
            not candidate.available
            and candidate.requested_action is RequestedAction.NONE,
            "Huge RCEm target or power did not fail closed",
        )
    huge_bms = build_execution_context(
        execution_source(
            battery_soc_percent=HUGE_INT,
            bms_voltage_v=HUGE_INT,
            bms_max_charge_current_a=HUGE_INT,
            bms_max_discharge_current_a=HUGE_INT,
        ),
        now=NOW,
    )
    check(
        not huge_bms.charge_direction_ready
        and not huge_bms.discharge_direction_ready
        and not huge_bms.critical_bms_ready,
        "Huge BMS input retained readiness",
    )
    huge_mode = build_execution_context(
        execution_source(physical_mode_code=HUGE_INT),
        now=NOW,
    )
    check(
        huge_mode.physical_mode is PhysicalMode.UNKNOWN
        and not huge_mode.physical_mode_fresh,
        "Huge physical mode did not become unknown",
    )
    for invalid_export in (
        execution_source(gcf_enable_code=HUGE_INT),
        execution_source(effective_export_limit_percent=HUGE_INT),
    ):
        check(
            normalize_export_state(invalid_export, now=NOW)
            is ExportState.UNVERIFIED,
            "Huge GCF/export input was trusted",
        )

    tariff_one = build_tariff_candidate(
        tariff_source(current_run_benefit_pln=1.0),
        now=NOW,
    )
    tariff_two = build_tariff_candidate(
        tariff_source(current_run_benefit_pln=2.0),
        now=NOW,
    )
    check(
        tariff_one.desired_actuator_fingerprint
        == tariff_two.desired_actuator_fingerprint
        and tariff_one.candidate_revision == tariff_two.candidate_revision,
        "Provisional benefit churned candidate revision",
    )
    incompatible_one = runtime_module._finalize(
        replace(
            tariff_one,
            candidate_revision=0,
            economic_value_status=EconomicValueStatus.INCOMPATIBLE,
            expected_marginal_net_benefit_pln=1.0,
        )
    )
    incompatible_two = runtime_module._finalize(
        replace(
            incompatible_one,
            candidate_revision=0,
            expected_marginal_net_benefit_pln=2.0,
        )
    )
    check(
        incompatible_one.candidate_revision
        == incompatible_two.candidate_revision,
        "Incompatible benefit churned candidate revision",
    )

    baseline = build_rce_candidate(rce_source(), now=NOW)
    power_changed = build_rce_candidate(
        rce_source(requested_discharge_power_kw=5.0),
        now=NOW,
    )
    energy_changed = build_rce_candidate(
        rce_source(planned_export_energy_kwh=3.0),
        now=NOW,
    )
    for presentation_changed in (power_changed, energy_changed):
        check(
            presentation_changed.desired_actuator_fingerprint
            == baseline.desired_actuator_fingerprint
            and presentation_changed.candidate_revision
            == baseline.candidate_revision,
            "Presentation-only RCE metric churned candidate revision",
        )

    target_changed = build_rce_candidate(
        rce_source(effective_discharge_power_percent=41.0),
        now=NOW,
    )
    eligibility_changed = build_rce_candidate(
        rce_source(current_slot_start_eligible=False),
        now=NOW,
    )
    validity_changed = build_rce_candidate(
        rce_source(current_run_end=NOW + timedelta(hours=2)),
        now=NOW,
    )
    source_revision_changed = build_rce_candidate(
        rce_source(input_revision=99),
        now=NOW,
    )
    check(
        target_changed.desired_actuator_fingerprint
        != baseline.desired_actuator_fingerprint
        and target_changed.candidate_revision != baseline.candidate_revision,
        "Physical intent change did not change revision",
    )
    for semantic_changed in (
        eligibility_changed,
        validity_changed,
        source_revision_changed,
    ):
        check(
            semantic_changed.candidate_revision != baseline.candidate_revision,
            "Consumed RCE semantic fact did not change revision",
        )

    active_source = rce_source(
        active_latched=True,
        latched_slot_end=NOW + timedelta(minutes=45),
        latched_minimum_soc_percent=30.0,
        active_4305_readback_percent=30.0,
    )
    active = build_rce_candidate(active_source, now=NOW)
    hard_stopped = build_rce_candidate(
        replace(active_source, sale_block_active=True),
        now=NOW,
    )
    check(
        active.local_hard_stop is False
        and hard_stopped.local_hard_stop is True
        and active.candidate_revision != hard_stopped.candidate_revision,
        "Local hard stop did not change revision",
    )

    comparable_one = runtime_module._finalize(
        replace(
            tariff_one,
            candidate_revision=0,
            economic_value_status=EconomicValueStatus.COMPARABLE,
            economic_contract_id="supervisor.marginal-run.v1",
            economic_basis_fingerprint="b" * 64,
            expected_marginal_net_benefit_pln=1.0,
        )
    )
    comparable_two = runtime_module._finalize(
        replace(
            comparable_one,
            candidate_revision=0,
            expected_marginal_net_benefit_pln=2.0,
        )
    )
    check(
        comparable_one.candidate_revision != comparable_two.candidate_revision,
        "Comparable marginal value did not change revision",
    )
    semantic_projection = runtime_module._semantic_revision_projection(
        baseline
    )
    check(
        tuple(semantic_projection)
        == (
            "schema_version",
            "policy_id",
            "allowed_by_user",
            "enabled",
            "available",
            "result_current",
            "recalculation_pending",
            "input_revision",
            "start_eligible",
            "continuation_eligible",
            "active_latched",
            "local_hard_stop",
            "requested_action",
            "actuator_scope",
            "priority_class",
            "need_class",
            "reason_code",
            "blocked_reason",
            "valid_from",
            "valid_until",
            "economic_value_status",
            "economic_contract_id",
            "economic_basis_fingerprint",
            "expected_marginal_net_benefit_pln",
            "desired_actuator_fingerprint",
        ),
        "Semantic revision projection changed",
    )
    for excluded in (
        "observed_at",
        "requested_mode",
        "requested_power_kw",
        "requested_energy_kwh",
        "target_soc_percent",
        "protected_soc_floor_percent",
        "urgency",
        "severity",
    ):
        check(
            excluded not in semantic_projection,
            f"Presentation field entered revision: {excluded}",
        )


def test_rcm_mapping() -> None:
    live_absorb = build_rcm_candidate(
        rcm_source(
            live_emergency=True,
            emergency_action_ready=True,
            action=RcmAction.ABSORB_PV,
        ),
        now=NOW,
    )
    check(live_absorb.priority_class is PriorityClass.LIVE_EMERGENCY, "Wrong live priority")
    check(live_absorb.need_class is NeedClass.MANDATORY, "Wrong live need")
    check(live_absorb.requested_action is RequestedAction.RCM_ABSORB_PV, "Wrong live absorb")
    check(live_absorb.actuator_scope is ActuatorScope.DIRECT_306, "Wrong 306 scope")
    expected_absorb_fingerprint = payload_hash(
        {
            "policy": PolicyId.RCM,
            "action": RequestedAction.RCM_ABSORB_PV,
            "actuator_scope": ActuatorScope.DIRECT_306,
            "target_306_percent": 55.0,
        }
    )
    check(
        live_absorb.desired_actuator_fingerprint
        == expected_absorb_fingerprint,
        "306 fingerprint is not the exact independent intent",
    )
    assert_core_accepts(live_absorb)

    live_limit = build_rcm_candidate(
        rcm_source(
            live_emergency=True,
            emergency_action_ready=True,
            action=RcmAction.LIMIT_EXPORT,
        ),
        now=NOW,
    )
    check(live_limit.requested_action is RequestedAction.RCM_LIMIT_EXPORT, "Wrong live limit")
    check(live_limit.actuator_scope is ActuatorScope.DIRECT_259, "Wrong 259 scope")
    expected_limit_fingerprint = payload_hash(
        {
            "policy": PolicyId.RCM,
            "action": RequestedAction.RCM_LIMIT_EXPORT,
            "actuator_scope": ActuatorScope.DIRECT_259,
            "target_259_percent": 35.0,
        }
    )
    check(
        live_limit.desired_actuator_fingerprint
        == expected_limit_fingerprint,
        "259 fingerprint is not the exact independent intent",
    )
    assert_core_accepts(live_limit)

    fallback_limit = build_rcm_candidate(
        rcm_source(
            live_emergency=True,
            emergency_action_ready=True,
            action=RcmAction.ABSORB_PV,
            charge_path_locally_valid=False,
        ),
        now=NOW,
    )
    check(
        fallback_limit.requested_action is RequestedAction.RCM_LIMIT_EXPORT,
        "Live fallback did not choose a valid lower export target",
    )
    no_actuator = build_rcm_candidate(
        rcm_source(
            live_emergency=True,
            emergency_action_ready=False,
            action=RcmAction.ABSORB_PV,
            charge_path_locally_valid=False,
            export_path_locally_valid=False,
        ),
        now=NOW,
    )
    check(no_actuator.requested_action is RequestedAction.NONE, "Unavailable live RCEm acted")
    check(not no_actuator.available, "Unavailable live RCEm remained available")
    for observed_at in (
        NOW - timedelta(seconds=61),
        NOW + timedelta(microseconds=1),
    ):
        stale = build_rcm_candidate(
            rcm_source(observed_at=observed_at),
            now=NOW,
        )
        check(stale.requested_action is RequestedAction.NONE, "Stale RCEm acted")
        check(not stale.available, "Stale RCEm remained available")

    pre = build_rcm_candidate(
        rcm_source(action=RcmAction.GRID_DISCHARGE_PREPARATION),
        now=NOW,
    )
    check(pre.requested_action is RequestedAction.RCM_PRE_DISCHARGE, "Pre-discharge lost")
    check(
        pre.actuator_scope is ActuatorScope.EMS_BLOCK_4300_4306,
        "Wrong pre-discharge scope",
    )
    check(pre.priority_class is PriorityClass.PREVENTIVE_GRID, "Wrong preventive priority")
    expected_pre_discharge_fingerprint = payload_hash(
        {
            "policy": PolicyId.RCM,
            "action": RequestedAction.RCM_PRE_DISCHARGE,
            "actuator_scope": ActuatorScope.EMS_BLOCK_4300_4306,
            "mode": 5,
            "target_4305": 45.0,
            "target_4306_percent": 40.0,
            "valid_until": NOW + timedelta(hours=2),
        }
    )
    check(
        pre.desired_actuator_fingerprint
        == expected_pre_discharge_fingerprint,
        "Pre-discharge fingerprint is not the exact independent intent",
    )
    assert_core_accepts(pre)
    active_pre_source = rcm_source(
        action=RcmAction.GRID_DISCHARGE_PREPARATION,
        pre_discharge_active=True,
        pre_discharge_start_eligible=False,
        pre_discharge_continue_eligible=True,
        pre_discharge_deadline=NOW + timedelta(minutes=20),
        pre_discharge_target_soc_percent=55.0,
        pre_discharge_power_kw=7.0,
        pre_discharge_power_percent=70.0,
        latched_pre_discharge_deadline=NOW + timedelta(hours=3),
        latched_pre_discharge_target_soc_percent=42.0,
        latched_pre_discharge_power_kw=3.5,
        latched_pre_discharge_power_percent=35.0,
    )
    active_pre = build_rcm_candidate(active_pre_source, now=NOW)
    check(active_pre.active_latched, "RCEm active latch was lost")
    check(active_pre.continuation_eligible, "RCEm active continuation was blocked")
    check(active_pre.valid_until == NOW + timedelta(hours=3), "RCEm frozen end lost")
    check(active_pre.target_soc_percent == 42.0, "RCEm frozen target lost")
    check(active_pre.requested_power_kw == 3.5, "RCEm frozen power lost")
    assert_core_accepts(active_pre, owner=OwnerKind.RCM)

    preventive_absorb = build_rcm_candidate(rcm_source(), now=NOW)
    preventive_limit = build_rcm_candidate(
        rcm_source(action=RcmAction.LIMIT_EXPORT),
        now=NOW,
    )
    check(
        preventive_absorb.requested_action is RequestedAction.RCM_ABSORB_PV,
        "Preventive absorb lost",
    )
    check(
        preventive_limit.requested_action is RequestedAction.RCM_LIMIT_EXPORT,
        "Preventive limit lost",
    )
    for action in (
        RcmAction.MONITOR,
        RcmAction.HOLD,
        RcmAction.RESTORE,
        RcmAction.RELEASE_EXPORT,
        RcmAction.PRESERVE_HEADROOM,
        RcmAction.UNKNOWN,
    ):
        candidate = build_rcm_candidate(rcm_source(action=action), now=NOW)
        check(candidate.requested_action is RequestedAction.NONE, f"{action} was mapped")
        assert_core_accepts(candidate)

    master_context = build_execution_context(
        execution_source(
            machine_type_code=1,
            inverter_count=4,
            full_block_execution_ready=True,
            direct_306_execution_ready=True,
            direct_259_execution_ready=True,
        ),
        now=NOW,
    )
    check(
        master_context.topology_full_block_allowed
        and not master_context.topology_direct_register_allowed
        and master_context.direct_259_execution_ready,
        "Master topology fixture is not independently ready for the 259 gate",
    )
    parallel_absorb = build_rcm_candidate(
        rcm_source(
            action=RcmAction.ABSORB_PV,
            direct_register_topology_allowed=(
                master_context.topology_direct_register_allowed
            ),
            full_block_topology_allowed=(
                master_context.topology_full_block_allowed
            ),
        ),
        now=NOW,
    )
    parallel_limit = build_rcm_candidate(
        rcm_source(
            action=RcmAction.LIMIT_EXPORT,
            direct_register_topology_allowed=(
                master_context.topology_direct_register_allowed
            ),
            full_block_topology_allowed=(
                master_context.topology_full_block_allowed
            ),
        ),
        now=NOW,
    )
    for direct_candidate in (parallel_absorb, parallel_limit):
        check(
            direct_candidate.requested_action is RequestedAction.NONE
            and direct_candidate.actuator_scope is ActuatorScope.NONE
            and not direct_candidate.available,
            "Parallel direct-register candidate survived",
        )
        blocked_decision = arbitrate_supervisor(
            mode=SupervisorMode.SHADOW,
            profile=SupervisorProfile.BALANCED,
            context=master_context,
            candidates=(direct_candidate,),
            now=NOW,
        )
        check(
            blocked_decision.selected_policy is None,
            "Parallel direct-register path produced a physical selection",
        )
    single_context = build_execution_context(execution_source(), now=NOW)
    single_limit = build_rcm_candidate(
        rcm_source(
            action=RcmAction.LIMIT_EXPORT,
            direct_register_topology_allowed=(
                single_context.topology_direct_register_allowed
            ),
        ),
        now=NOW,
    )
    check(
        single_context.topology_direct_register_allowed
        and single_limit.requested_action is RequestedAction.RCM_LIMIT_EXPORT
        and single_limit.actuator_scope is ActuatorScope.DIRECT_259,
        "Verified single inverter lost its valid direct-259 candidate",
    )
    single_limit_decision = arbitrate_supervisor(
        mode=SupervisorMode.SHADOW,
        profile=SupervisorProfile.BALANCED,
        context=single_context,
        candidates=(single_limit,),
        now=NOW,
    )
    check(
        single_limit_decision.selected_policy is PolicyId.RCM,
        "Verified single direct-259 candidate was not selectable in Shadow",
    )
    master_pre = build_rcm_candidate(
        rcm_source(
            action=RcmAction.GRID_DISCHARGE_PREPARATION,
            direct_register_topology_allowed=False,
            full_block_topology_allowed=True,
        ),
        now=NOW,
    )
    check(
        master_pre.requested_action is RequestedAction.RCM_PRE_DISCHARGE,
        "Verified Master full-block pre-discharge was lost",
    )
    dual_condition = build_rcm_candidate(
        rcm_source(
            live_emergency=True,
            emergency_action_ready=True,
            action=RcmAction.ABSORB_PV,
            charge_path_locally_valid=True,
            export_path_locally_valid=True,
            recommended_charge_limit_percent=55.0,
            recommended_export_limit_percent=35.0,
            current_export_limit_percent=60.0,
        ),
        now=NOW,
    )
    check(
        dual_condition.requested_action is RequestedAction.RCM_ABSORB_PV
        and dual_condition.actuator_scope is ActuatorScope.DIRECT_306,
        "Dual-condition live emergency violated deterministic primary ordering",
    )
    check(
        dual_condition.desired_actuator_fingerprint
        == expected_absorb_fingerprint,
        "Dual-condition 306 intent contains a hidden 259 target",
    )
    dual_decision = arbitrate_supervisor(
        mode=SupervisorMode.SHADOW,
        profile=SupervisorProfile.BALANCED,
        context=single_context,
        candidates=(dual_condition,),
        now=NOW,
    )
    check(
        len(dual_decision.candidate_summaries) == 1
        and dual_decision.selected_policy is PolicyId.RCM,
        "Dual-condition snapshot did not produce exactly one primary candidate",
    )
    check(
        len(
            {
                expected_absorb_fingerprint,
                expected_limit_fingerprint,
                expected_pre_discharge_fingerprint,
            }
        )
        == 3,
        "Independent RCEm physical fingerprints collided",
    )
    for candidate in (
        live_absorb,
        live_limit,
        fallback_limit,
        pre,
        preventive_absorb,
        preventive_limit,
        master_pre,
    ):
        check(
            candidate.economic_value_status is EconomicValueStatus.UNAVAILABLE
            and candidate.economic_contract_id is None
            and candidate.economic_basis_fingerprint is None
            and candidate.expected_marginal_net_benefit_pln is None,
            "RCEm economics were invented",
        )


def test_owner_normalization() -> None:
    check(
        "owner_code" not in {item.name for item in fields(ExecutionSourceSnapshot)},
        "Compatibility owner fallback entered the API",
    )
    base = execution_source()
    check(normalize_owner(base, now=NOW) == NormalizedOwner(OwnerKind.NONE, False), "Self-Use owner")
    check(
        normalize_owner(replace(base, physical_mode_code=3), now=NOW)
        == NormalizedOwner(OwnerKind.NONE, False),
        "Off-Grid owner",
    )
    cases = (
        (replace(base, manual_charge_active=True), OwnerKind.MANUAL),
        (replace(base, manual_discharge_active=True), OwnerKind.MANUAL),
        (replace(base, balancing_active=True), OwnerKind.BALANCING),
        (replace(base, rce_active=True), OwnerKind.RCE),
        (replace(base, tariff_active=True), OwnerKind.TARIFF),
        (replace(base, rcm_active=True), OwnerKind.RCM),
        (replace(base, rcm_export_control_active=True), OwnerKind.RCM),
        (replace(base, rcm_pre_discharge_active=True), OwnerKind.RCM),
        (
            replace(base, rcm_active=True, rcm_export_control_active=True),
            OwnerKind.RCM,
        ),
    )
    for source, expected in cases:
        owner = normalize_owner(source, now=NOW)
        check(owner == NormalizedOwner(expected, False), f"Wrong owner: {expected.value}")
    for conflict in (
        replace(base, rce_active=True, tariff_active=True),
        replace(base, manual_charge_active=True, manual_discharge_active=True),
        replace(base, charge_timer_active=True),
        replace(base, discharge_timer_active=True),
        replace(base, rce_active=None),
    ):
        owner = normalize_owner(conflict, now=NOW)
        check(owner == NormalizedOwner(OwnerKind.UNKNOWN, True), "Conflict did not fail closed")
    for mode in (4, 5):
        check(
            normalize_owner(replace(base, physical_mode_code=mode), now=NOW)
            == NormalizedOwner(OwnerKind.FOREIGN, False),
            "Unowned execution mode is not foreign",
        )
    stale = normalize_owner(
        replace(base, full_block_generation_at=NOW - timedelta(seconds=181)),
        now=NOW,
    )
    check(stale == NormalizedOwner(OwnerKind.UNKNOWN, False), "Stale mode did not become unknown")


def test_execution_context() -> None:
    for code, expected in (
        (0, PhysicalMode.SELF_USE),
        (3, PhysicalMode.OFF_GRID),
        (4, PhysicalMode.GRID_CHARGE),
        (5, PhysicalMode.GRID_DISCHARGE),
        (2, PhysicalMode.UNKNOWN),
        (None, PhysicalMode.UNKNOWN),
        (float("nan"), PhysicalMode.UNKNOWN),
    ):
        context = build_execution_context(
            execution_source(physical_mode_code=code),
            now=NOW,
        )
        check(context.physical_mode is expected, f"Wrong physical mode for {code}")
        check(
            context.physical_mode_fresh is (expected is not PhysicalMode.UNKNOWN),
            f"Wrong mode freshness for {code}",
        )
    for timestamp in (
        NOW - timedelta(seconds=181),
        NOW + timedelta(microseconds=1),
        None,
    ):
        context = build_execution_context(
            execution_source(full_block_generation_at=timestamp),
            now=NOW,
        )
        check(not context.physical_mode_fresh, "Invalid mode generation was fresh")

    independent = build_execution_context(
        execution_source(
            full_block_execution_ready=False,
            direct_306_execution_ready=True,
            direct_259_execution_ready=False,
        ),
        now=NOW,
    )
    check(not independent.full_block_execution_ready, "Full readiness coupled")
    check(independent.direct_306_execution_ready, "306 readiness coupled")
    check(not independent.direct_259_execution_ready, "259 readiness coupled")
    master = build_execution_context(
        execution_source(machine_type_code=1, inverter_count=4),
        now=NOW,
    )
    check(master.topology_full_block_allowed, "Master full block rejected")
    check(not master.topology_direct_register_allowed, "Master direct register allowed")
    stale_topology = build_execution_context(
        execution_source(topology_generation_at=NOW - timedelta(seconds=181)),
        now=NOW,
    )
    check(
        not stale_topology.topology_full_block_allowed
        and not stale_topology.topology_direct_register_allowed,
        "Stale topology retained authority",
    )

    no_charge = build_execution_context(
        execution_source(bms_max_charge_current_a=0.0),
        now=NOW,
    )
    check(not no_charge.charge_direction_ready, "Zero charge capability accepted")
    check(no_charge.discharge_direction_ready, "Charge capability affected discharge")
    no_discharge = build_execution_context(
        execution_source(bms_max_discharge_current_a=0.0),
        now=NOW,
    )
    check(no_discharge.charge_direction_ready, "Discharge capability affected charge")
    check(not no_discharge.discharge_direction_ready, "Zero discharge capability accepted")
    stale_soc = build_execution_context(
        execution_source(battery_soc_observed_at=NOW - timedelta(seconds=121)),
        now=NOW,
    )
    check(not stale_soc.critical_bms_ready, "Stale SOC was critical-ready")
    context = build_execution_context(execution_source(), now=NOW)
    check(context.critical_bms_ready, "Fresh BMS was rejected")
    check(
        context.transaction_pending is False
        and context.transaction_owner_kind is OwnerKind.NONE,
        "Supervisor transaction namespace is not idle",
    )
    transaction_sources = (
        ("self_use", execution_source(physical_mode_code=0)),
        ("off_grid", execution_source(physical_mode_code=3)),
        ("grid_charge", execution_source(physical_mode_code=4)),
        ("grid_discharge", execution_source(physical_mode_code=5)),
        ("foreign_owner", execution_source(physical_mode_code=4)),
        ("rce_writer", execution_source(physical_mode_code=5, rce_active=True)),
        (
            "tariff_writer",
            execution_source(physical_mode_code=4, tariff_active=True),
        ),
        ("rcm_writer", execution_source(rcm_active=True)),
    )
    for label, source in transaction_sources:
        transaction_context = build_execution_context(source, now=NOW)
        check(
            transaction_context.transaction_pending is False
            and transaction_context.transaction_owner_kind is OwnerKind.NONE,
            f"Supervisor transaction namespace derived pending for {label}",
        )
    source_text = SOURCE_PATH.read_text(encoding="utf-8")
    source_tree = ast.parse(source_text, filename=str(SOURCE_PATH))
    context_builder = next(
        node
        for node in source_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "build_execution_context"
    )
    context_returns = [
        node.value
        for node in ast.walk(context_builder)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "ExecutionContext"
    ]
    check(len(context_returns) == 1, "ExecutionContext construction path changed")
    transaction_keywords = {
        keyword.arg: keyword.value
        for keyword in context_returns[0].keywords
        if keyword.arg in {"transaction_pending", "transaction_owner_kind"}
    }
    pending_node = transaction_keywords.get("transaction_pending")
    owner_node = transaction_keywords.get("transaction_owner_kind")
    check(
        isinstance(pending_node, ast.Constant)
        and pending_node.value is False,
        "transaction_pending is derived instead of literal false",
    )
    check(
        isinstance(owner_node, ast.Attribute)
        and isinstance(owner_node.value, ast.Name)
        and owner_node.value.id == "OwnerKind"
        and owner_node.attr == "NONE",
        "transaction_owner_kind is derived instead of literal NONE",
    )
    check(
        not any("legacy" in item.name or "pending" in item.name for item in fields(ExecutionSourceSnapshot)),
        "Legacy pending was inferred into the snapshot",
    )
    check_raises(
        ValueError,
        build_execution_context,
        execution_source(),
        now=NOW.replace(tzinfo=None),
    )


def test_export_normalization() -> None:
    check(
        normalize_export_state(execution_source(gcf_enable_code=0), now=NOW)
        is ExportState.VERIFIED_ALLOWED,
        "Disabled GCF was not verified allowed",
    )
    check(
        normalize_export_state(
            execution_source(gcf_enable_code=1, effective_export_limit_percent=10.0),
            now=NOW,
        )
        is ExportState.VERIFIED_ALLOWED,
        "Positive export cap was not verified allowed",
    )
    check(
        normalize_export_state(
            execution_source(gcf_enable_code=1, effective_export_limit_percent=0.0),
            now=NOW,
        )
        is ExportState.CONFIRMED_ZERO_EXPORT,
        "Exact zero export was not confirmed",
    )
    invalid = (
        execution_source(hardware_readback_supported=False),
        execution_source(gcf_generation_at=NOW - timedelta(seconds=181)),
        execution_source(gcf_generation_at=NOW + timedelta(microseconds=1)),
        execution_source(gcf_cohort_coherent=False),
        execution_source(effective_export_limit_percent=-0.1),
        execution_source(effective_export_limit_percent=float("nan")),
        execution_source(gcf_enable_code=2),
    )
    for source in invalid:
        result = normalize_export_state(source, now=NOW)
        check(result is ExportState.UNVERIFIED, "Invalid export cohort was trusted")
        check(result is not ExportState.PROHIBITED, "Prohibited was synthesized")


def test_determinism_bounds_and_core() -> tuple[int, int]:
    rce = build_rce_candidate(rce_source(), now=NOW)
    tariff = build_tariff_candidate(
        tariff_source(current_run_need_class=TariffRunNeed.REQUIRED_ENERGY),
        now=NOW,
    )
    rcm = build_rcm_candidate(rcm_source(), now=NOW)
    candidates = (rce, tariff, rcm)
    reference: str | None = None
    for ordered in permutations(candidates):
        decision = arbitrate_supervisor(
            mode=SupervisorMode.SHADOW,
            profile=SupervisorProfile.BALANCED,
            context=build_execution_context(execution_source(), now=NOW),
            candidates=ordered,
            now=NOW,
        )
        check(decision.state is not SupervisorState.BLOCKED, "Builder produced invalid candidate")
        serialized = serialize_supervisor_summary(decision)
        if reference is None:
            reference = serialized
        else:
            check(serialized == reference, "Candidate input order changed the decision")
    check(reference is not None, "No serialized decision was produced")
    far_future = datetime.max.replace(tzinfo=timezone.utc)
    max_revision = 18_446_744_073_709_551_615
    stress_candidates = (
        build_rce_candidate(
            rce_source(
                observed_at=NOW - timedelta(microseconds=1),
                input_revision=max_revision,
                current_run_end=far_future,
                requested_discharge_power_kw=1_000_000_000.0,
                planned_export_energy_kwh=1_000_000_000.0,
                protected_soc_floor_percent=0.0,
                effective_discharge_power_percent=100.0,
                current_soc_percent=100.0,
            ),
            now=NOW,
        ),
        build_tariff_candidate(
            tariff_source(
                observed_at=NOW - timedelta(microseconds=1),
                input_revision=max_revision,
                current_action=TariffAction.GRID_SUPPORT_AND_CHARGE,
                current_slot_end=far_future,
                requested_charge_power_kw=1_000_000_000.0,
                command_charge_power_percent=100.0,
                current_run_grid_import_kwh=1_000_000_000.0,
                current_run_benefit_pln=1_000_000_000.0,
                target_soc_percent=100.0,
                base_reserve_soc_percent=0.0,
            ),
            now=NOW,
        ),
        build_rcm_candidate(
            rcm_source(
                observed_at=NOW - timedelta(microseconds=1),
                input_revision=max_revision,
                action=RcmAction.GRID_DISCHARGE_PREPARATION,
                pre_discharge_deadline=far_future,
                pre_discharge_target_soc_percent=100.0,
                pre_discharge_power_kw=1_000_000_000.0,
                pre_discharge_power_percent=100.0,
                planned_grid_discharge_kwh=1_000_000_000.0,
                protected_minimum_soc_percent=0.0,
                voltage_risk_score_percent=100.0,
            ),
            now=NOW,
        ),
    )
    stress_context = replace(
        build_execution_context(execution_source(), now=NOW),
        full_block_execution_ready=False,
        direct_306_execution_ready=False,
        direct_259_execution_ready=False,
    )
    stress_decision = arbitrate_supervisor(
        mode=SupervisorMode.SHADOW,
        profile=SupervisorProfile.HIGH_RESERVE_WINTER,
        context=stress_context,
        candidates=stress_candidates,
        now=NOW,
    )
    stress_serialized = serialize_supervisor_summary(stress_decision)
    serialized_cases = (reference, stress_serialized)
    total_bytes = max(len(item.encode("utf-8")) for item in serialized_cases)
    candidate_sizes = []
    for serialized in serialized_cases:
        payload = json.loads(serialized)
        candidate_sizes.extend(
            len(
                json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
            for item in payload["candidate_summaries"]
        )
    check(total_bytes <= MAX_SUPERVISOR_SUMMARY_BYTES, "Total summary bound exceeded")
    check(
        max(candidate_sizes) <= MAX_CANDIDATE_SUMMARY_BYTES,
        "Candidate summary bound exceeded",
    )
    for forbidden in ("planned_slots", "price_history", "forecast", "weather", "provenance"):
        check(
            all(forbidden not in item for item in serialized_cases),
            f"Unbounded payload leaked: {forbidden}",
        )
    return total_bytes, max(candidate_sizes)


def main() -> None:
    test_structure_and_static_safety()
    test_rce_mapping()
    test_tariff_mapping()
    test_numeric_totality_and_revision_semantics()
    test_rcm_mapping()
    test_owner_normalization()
    test_execution_context()
    test_export_normalization()
    total_bytes, candidate_bytes = test_determinism_bounds_and_core()
    print(
        "Supervisor runtime: deterministic contract passed "
        f"checks={CHECK_COUNT} worst_total_bytes={total_bytes} "
        f"max_candidate_bytes={candidate_bytes}"
    )


if __name__ == "__main__":
    main()
