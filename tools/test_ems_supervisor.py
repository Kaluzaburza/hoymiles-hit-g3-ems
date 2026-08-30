"""Focused deterministic tests for EMS Supervisor V1 Phase 1A."""

from __future__ import annotations

import ast
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import inspect
from itertools import permutations, product
import json
from pathlib import Path
import sys
from typing import Any, Callable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
SOURCE_PATH = COMPONENT / "ems_supervisor.py"
sys.path.insert(0, str(COMPONENT))

import ems_supervisor as supervisor_module  # noqa: E402

from ems_supervisor import (  # noqa: E402
    ActuatorScope,
    CandidateRejection,
    CandidateSummary,
    EconomicValueStatus,
    ExecutionContext,
    ExecutionPhase,
    ExportState,
    MAX_CANDIDATE_SUMMARY_BYTES,
    MAX_SUPERVISOR_SUMMARY_BYTES,
    NeedClass,
    OwnerKind,
    PROFILE_TABLE_VERSION,
    PhysicalMode,
    PolicyCandidate,
    PolicyId,
    PriorityClass,
    ProfileEffect,
    ReasonCode,
    RequestedAction,
    SelectionKind,
    SupervisorDecision,
    SupervisorMode,
    SupervisorProfile,
    SupervisorState,
    TemporalStatus,
    arbitrate_supervisor,
    serialize_supervisor_summary,
)


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
CONTRACT_ID = "supervisor.marginal-run.v1"

CHECK_COUNT = 0


def check(condition: bool, message: str) -> None:
    """Count one deterministic contract check and fail with context."""
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
    """Count and verify one expected exception."""
    global CHECK_COUNT
    CHECK_COUNT += 1
    try:
        function(*args, **kwargs)
    except exception:
        return
    raise AssertionError(f"Expected {exception.__name__}")


def context(**overrides: Any) -> ExecutionContext:
    values: dict[str, Any] = {
        "observed_at": NOW - timedelta(seconds=1),
        "physical_mode": PhysicalMode.SELF_USE,
        "physical_mode_fresh": True,
        "owner_kind": OwnerKind.NONE,
        "owner_conflict": False,
        "transaction_pending": False,
        "transaction_owner_kind": OwnerKind.NONE,
        "full_block_execution_ready": True,
        "direct_306_execution_ready": True,
        "direct_259_execution_ready": True,
        "topology_full_block_allowed": True,
        "topology_direct_register_allowed": True,
        "charge_direction_ready": True,
        "discharge_direction_ready": True,
        "critical_bms_ready": True,
        "export_state": ExportState.VERIFIED_ALLOWED,
    }
    values.update(overrides)
    return ExecutionContext(**values)


DEFAULT_SHAPES = {
    PolicyId.RCE: (
        PriorityClass.ECONOMIC,
        NeedClass.OPTIONAL,
        RequestedAction.RCE_EXPORT,
        ActuatorScope.EMS_BLOCK_4300_4306,
        ReasonCode.ECONOMIC_CANDIDATE,
    ),
    PolicyId.TARIFF: (
        PriorityClass.ECONOMIC,
        NeedClass.OPTIONAL,
        RequestedAction.TARIFF_CHARGE,
        ActuatorScope.EMS_BLOCK_4300_4306,
        ReasonCode.ECONOMIC_CANDIDATE,
    ),
    PolicyId.RCM: (
        PriorityClass.PREVENTIVE_GRID,
        NeedClass.PREVENTIVE,
        RequestedAction.RCM_ABSORB_PV,
        ActuatorScope.DIRECT_306,
        ReasonCode.PREVENTIVE_VOLTAGE_ACTION,
    ),
}


def candidate(policy_id: PolicyId, **overrides: Any) -> PolicyCandidate:
    priority, need, action, scope, reason = DEFAULT_SHAPES[policy_id]
    values: dict[str, Any] = {
        "schema_version": 1,
        "policy_id": policy_id,
        "observed_at": NOW - timedelta(seconds=1),
        "allowed_by_user": True,
        "enabled": True,
        "available": True,
        "result_current": True,
        "recalculation_pending": False,
        "input_revision": 1,
        "candidate_revision": {
            PolicyId.RCE: 11,
            PolicyId.TARIFF: 12,
            PolicyId.RCM: 13,
        }[policy_id],
        "start_eligible": True,
        "continuation_eligible": True,
        "active_latched": False,
        "local_hard_stop": False,
        "requested_action": action,
        "actuator_scope": scope,
        "priority_class": priority,
        "need_class": need,
        "reason_code": reason,
        "blocked_reason": None,
        "valid_from": NOW - timedelta(hours=1),
        "valid_until": NOW + timedelta(hours=1),
        "desired_actuator_fingerprint": HASH_A,
        "economic_value_status": EconomicValueStatus.UNAVAILABLE,
    }
    values.update(overrides)
    if "desired_actuator_fingerprint" not in overrides:
        values["desired_actuator_fingerprint"] = (
            None
            if values["requested_action"] is RequestedAction.NONE
            else HASH_A
        )
    return PolicyCandidate(**values)


def no_action_candidate(policy_id: PolicyId, **overrides: Any) -> PolicyCandidate:
    return candidate(
        policy_id,
        priority_class=PriorityClass.NONE,
        need_class=NeedClass.NONE,
        requested_action=RequestedAction.NONE,
        actuator_scope=ActuatorScope.NONE,
        reason_code=ReasonCode.NO_ACTION,
        start_eligible=False,
        continuation_eligible=False,
        desired_actuator_fingerprint=None,
        **overrides,
    )


def comparable_candidate(
    policy_id: PolicyId,
    value: float,
    *,
    contract_id: str = CONTRACT_ID,
    basis: str = HASH_B,
    **overrides: Any,
) -> PolicyCandidate:
    return candidate(
        policy_id,
        economic_value_status=EconomicValueStatus.COMPARABLE,
        economic_contract_id=contract_id,
        economic_basis_fingerprint=basis,
        expected_marginal_net_benefit_pln=value,
        **overrides,
    )


def decide(
    candidates: Any,
    *,
    mode: SupervisorMode = SupervisorMode.SHADOW,
    profile: SupervisorProfile = SupervisorProfile.BALANCED,
    execution_context: ExecutionContext | None = None,
    now: datetime = NOW,
):
    return arbitrate_supervisor(
        mode=mode,
        profile=profile,
        context=execution_context or context(),
        candidates=candidates,
        now=now,
    )


ALLOWED_SHAPES = frozenset(
    {
        (
            PolicyId.RCE,
            PriorityClass.ECONOMIC,
            NeedClass.OPTIONAL,
            RequestedAction.RCE_EXPORT,
            ActuatorScope.EMS_BLOCK_4300_4306,
        ),
        (
            PolicyId.TARIFF,
            PriorityClass.REQUIRED_ENERGY,
            NeedClass.MANDATORY,
            RequestedAction.TARIFF_CHARGE,
            ActuatorScope.EMS_BLOCK_4300_4306,
        ),
        (
            PolicyId.TARIFF,
            PriorityClass.ECONOMIC,
            NeedClass.OPTIONAL,
            RequestedAction.TARIFF_CHARGE,
            ActuatorScope.EMS_BLOCK_4300_4306,
        ),
        (
            PolicyId.RCM,
            PriorityClass.LIVE_EMERGENCY,
            NeedClass.MANDATORY,
            RequestedAction.RCM_ABSORB_PV,
            ActuatorScope.DIRECT_306,
        ),
        (
            PolicyId.RCM,
            PriorityClass.LIVE_EMERGENCY,
            NeedClass.MANDATORY,
            RequestedAction.RCM_LIMIT_EXPORT,
            ActuatorScope.DIRECT_259,
        ),
        (
            PolicyId.RCM,
            PriorityClass.PREVENTIVE_GRID,
            NeedClass.PREVENTIVE,
            RequestedAction.RCM_ABSORB_PV,
            ActuatorScope.DIRECT_306,
        ),
        (
            PolicyId.RCM,
            PriorityClass.PREVENTIVE_GRID,
            NeedClass.PREVENTIVE,
            RequestedAction.RCM_LIMIT_EXPORT,
            ActuatorScope.DIRECT_259,
        ),
        (
            PolicyId.RCM,
            PriorityClass.PREVENTIVE_GRID,
            NeedClass.PREVENTIVE,
            RequestedAction.RCM_PRE_DISCHARGE,
            ActuatorScope.EMS_BLOCK_4300_4306,
        ),
        *(
            (
                policy,
                PriorityClass.NONE,
                NeedClass.NONE,
                RequestedAction.NONE,
                ActuatorScope.NONE,
            )
            for policy in PolicyId
        ),
    }
)


def test_structure_and_static_safety() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SOURCE_PATH))
    disallowed_imports = {
        "asyncio",
        "homeassistant",
        "aiohttp",
        "http",
        "importlib",
        "os",
        "pathlib",
        "pymodbus",
        "random",
        "requests",
        "socket",
        "sqlite3",
        "subprocess",
        "time",
        "urllib",
        "uuid",
    }
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])
    check(not imported_roots & disallowed_imports, "Pure module imports I/O/runtime code")
    check(
        not any(isinstance(node, ast.AsyncFunctionDef) for node in ast.walk(tree)),
        "Pure arbiter contains async runtime code",
    )
    forbidden_calls = []
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
            "async_call",
            "call_service",
            "commit",
            "connect",
            "cursor",
            "execute",
            "executemany",
            "now",
            "utcnow",
            "today",
            "getenv",
            "monotonic",
            "save",
            "time",
            "write",
            "write_coil",
            "write_register",
            "write_registers",
        }:
            forbidden_calls.append(node.func.attr)
    check(not forbidden_calls, f"Pure arbiter performs forbidden calls: {forbidden_calls}")
    check(
        not any(
            isinstance(node, ast.Attribute) and node.attr == "environ"
            for node in ast.walk(tree)
        ),
        "Pure arbiter reads process environment",
    )
    dumps_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "json"
        and node.func.attr == "dumps"
    ]
    check(bool(dumps_calls), "Canonical JSON serializer is missing")
    for call in dumps_calls:
        allow_nan = next(
            (keyword.value for keyword in call.keywords if keyword.arg == "allow_nan"),
            None,
        )
        check(
            isinstance(allow_nan, ast.Constant) and allow_nan.value is False,
            "json.dumps does not independently forbid non-finite values",
        )
    mutable_globals = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, (ast.Dict, ast.List, ast.Set)):
                mutable_globals.append(getattr(node, "lineno", 0))
    check(not mutable_globals, "Pure arbiter exposes mutable global containers")
    check("homeassistant" not in source.lower(), "Home Assistant dependency found")
    for structure in (
        supervisor_module._ShadowProfileDefinition,
        ExecutionContext,
        PolicyCandidate,
        CandidateSummary,
        CandidateRejection,
        SupervisorDecision,
    ):
        check(
            structure.__dataclass_params__.frozen,
            f"{structure.__name__} is not frozen",
        )
        check(
            hasattr(structure, "__slots__"),
            f"{structure.__name__} is not slots-bounded",
        )
    signature = inspect.signature(arbitrate_supervisor)
    check(
        tuple(signature.parameters) == ("mode", "profile", "context", "candidates", "now"),
        "Arbiter API contains an unexpected input or retained-decision channel",
    )
    check(
        all(
            parameter.kind is inspect.Parameter.KEYWORD_ONLY
            for parameter in signature.parameters.values()
        ),
        "Arbiter API is not fully keyword-only",
    )
    expected_profile_effects = (
        "economic_minimum_advantage",
        "switching_advantage",
        "commitment_preference",
        "soft_reserve",
        "battery_wear_weight",
        "preferred_throughput",
        "minimum_hold",
    )
    check(
        tuple(effect.value for effect in ProfileEffect) == expected_profile_effects,
        "ProfileEffect schema differs from the literal Phase 1A contract",
    )
    expected_public_api = (
        "ActuatorScope",
        "CandidateRejection",
        "CandidateSummary",
        "EconomicValueStatus",
        "ExecutionContext",
        "ExecutionPhase",
        "ExportState",
        "MAX_CANDIDATES",
        "MAX_CANDIDATE_SUMMARY_BYTES",
        "MAX_SUPERVISOR_SUMMARY_BYTES",
        "NeedClass",
        "OwnerKind",
        "PROFILE_TABLE_VERSION",
        "PhysicalMode",
        "PolicyCandidate",
        "PolicyId",
        "PriorityClass",
        "ProfileEffect",
        "ReasonCode",
        "RequestedAction",
        "SCHEMA_VERSION",
        "SelectionKind",
        "SupervisorDecision",
        "SupervisorMode",
        "SupervisorProfile",
        "SupervisorState",
        "TemporalStatus",
        "arbitrate_supervisor",
        "serialize_supervisor_summary",
    )
    check(
        supervisor_module.__all__ == expected_public_api,
        "Public API differs from the exact 29-symbol Phase 1A contract",
    )
    expected_decision_fields = (
        "supervisor_mode",
        "profile",
        "state",
        "execution_phase",
        "selected_policy",
        "selected_candidate_revision",
        "selection_kind",
        "selection_reason",
        "execution_blocked_reason",
        "arbitration_revision",
        "supervisor_execution_authorized",
        "legacy_execution_unchanged",
        "candidate_summaries",
        "rejected_reasons",
        "profile_effects_applied",
        "profile_effects_not_applied",
    )
    check(
        tuple(field.name for field in fields(SupervisorDecision))
        == expected_decision_fields,
        "SupervisorDecision differs from the exact Phase 1A schema",
    )
    check(
        tuple(name for name, _semantic in supervisor_module._CANDIDATE_PROJECTION_FIELDS)
        == tuple(field.name for field in fields(CandidateSummary)),
        "Candidate projection whitelist and public summary schema diverged",
    )
    public_structures = (
        ExecutionContext,
        PolicyCandidate,
        CandidateSummary,
        CandidateRejection,
        SupervisorDecision,
    )
    unsupported_tokens = ("grant", "handover", "writer")
    check(
        not any(
            token in field.name.lower()
            for structure in public_structures
            for field in fields(structure)
            for token in unsupported_tokens
        ),
        "Public schema exposes a grant/handover/writer channel",
    )
    check(
        "ShadowProfileDefinition" not in supervisor_module.__all__,
        "Internal profile definition is publicly exported",
    )


def test_modes_permissions_and_basic_selection() -> None:
    rce = candidate(PolicyId.RCE)
    off = decide((rce,), mode=SupervisorMode.OFF)
    check(off.state is SupervisorState.OFF, "Off did not return off state")
    check(off.selected_policy is None, "Off selected a policy")
    check(off.selection_kind is SelectionKind.OFF, "Off selection kind is wrong")
    check(off.legacy_execution_unchanged, "Off did not preserve legacy execution")
    check(not off.supervisor_execution_authorized, "Off authorized execution")
    check(off.execution_blocked_reason is None, "Off reported an execution blocker")
    off_blocker_contexts = (
        context(owner_kind=OwnerKind.MANUAL),
        context(physical_mode=PhysicalMode.OFF_GRID),
        context(owner_kind=OwnerKind.FOREIGN),
        context(
            transaction_pending=True,
            transaction_owner_kind=OwnerKind.UNKNOWN,
        ),
    )
    for execution_context in off_blocker_contexts:
        blocked_off = decide(
            (rce,),
            mode=SupervisorMode.OFF,
            execution_context=execution_context,
        )
        check(blocked_off.state is SupervisorState.OFF, "Valid Off context was blocked")
        check(blocked_off.selected_policy is None, "Off selected under a global blocker")
        check(blocked_off.selection_kind is SelectionKind.OFF, "Off kind changed")
        check(blocked_off.execution_blocked_reason is None, "Off leaked a blocker")
        check(blocked_off.legacy_execution_unchanged, "Off changed legacy execution")

    shadow = decide((rce,))
    check(shadow.state is SupervisorState.SHADOW_SELECTED, "Shadow did not select")
    check(shadow.selected_policy is PolicyId.RCE, "Shadow selected wrong policy")
    check(shadow.selection_kind is SelectionKind.SHADOW, "Shadow kind is wrong")
    check(shadow.legacy_execution_unchanged, "Shadow changed legacy execution")
    check(not shadow.supervisor_execution_authorized, "Shadow authorized execution")

    active = decide((rce,), mode=SupervisorMode.ACTIVE)
    check(active.state is SupervisorState.BLOCKED, "Active was not blocked")
    check(active.selected_policy is None, "Active selected a policy")
    check(
        active.selection_kind is SelectionKind.ACTIVE_NOT_IMPLEMENTED,
        "Active did not report active_not_implemented",
    )
    check(
        active.execution_blocked_reason is ReasonCode.ACTIVE_NOT_IMPLEMENTED,
        "Active blocker is wrong",
    )
    check(not active.supervisor_execution_authorized, "Active authorized execution")
    check(active.legacy_execution_unchanged, "Phase 1A Active changed legacy execution")
    for result in (off, shadow, active):
        check(
            json.loads(serialize_supervisor_summary(result))[
                "supervisor_execution_authorized"
            ]
            is False,
            "A public mode decision failed safe serialization",
        )

    cases = (
        (replace(rce, allowed_by_user=False), ReasonCode.NOT_ALLOWED),
        (replace(rce, enabled=False), ReasonCode.POLICY_DISABLED),
        (replace(rce, available=False), ReasonCode.UNAVAILABLE),
    )
    for item, reason in cases:
        result = decide((item,))
        check(result.selected_policy is None, f"Rejected candidate selected: {reason}")
        check(
            result.rejected_reasons[0].reason is reason,
            f"Wrong rejection reason for {reason}",
        )
    check(
        decide(()).state is SupervisorState.SHADOW_IDLE,
        "No-candidate Shadow was not idle",
    )
    check(
        decide((rce, candidate(PolicyId.TARIFF), candidate(PolicyId.RCM))).selected_policy
        is PolicyId.RCM,
        "All permitted policies did not follow class priority",
    )


def test_complete_policy_shape_matrix() -> None:
    for policy, priority, need, action, scope in product(
        tuple(PolicyId),
        tuple(PriorityClass),
        tuple(NeedClass),
        tuple(RequestedAction),
        tuple(ActuatorScope),
    ):
        desired = None if action is RequestedAction.NONE else HASH_A
        item = candidate(
            policy,
            priority_class=priority,
            need_class=need,
            requested_action=action,
            actuator_scope=scope,
            desired_actuator_fingerprint=desired,
        )
        result = decide((item,))
        shape = (policy, priority, need, action, scope)
        check(
            (result.state is not SupervisorState.BLOCKED)
            == (shape in ALLOWED_SHAPES),
            f"Policy shape validation mismatch: {shape}",
        )
    for policy in PolicyId:
        result = decide((no_action_candidate(policy),))
        check(result.state is SupervisorState.SHADOW_IDLE, f"No-action failed: {policy}")
        check(result.selected_policy is None, f"No-action selected: {policy}")
    invalid_no_action = candidate(
        PolicyId.RCE,
        priority_class=PriorityClass.NONE,
        need_class=NeedClass.NONE,
        requested_action=RequestedAction.NONE,
        actuator_scope=ActuatorScope.EMS_BLOCK_4300_4306,
        desired_actuator_fingerprint=None,
    )
    check(
        decide((invalid_no_action,)).state is SupervisorState.BLOCKED,
        "No-action with physical scope was accepted",
    )


def test_input_validation_and_temporal_boundaries() -> None:
    rce = candidate(PolicyId.RCE)
    check(
        decide((rce, rce)).state is SupervisorState.BLOCKED,
        "Duplicate PolicyId was accepted",
    )
    check(
        decide((rce, candidate(PolicyId.TARIFF), candidate(PolicyId.RCM), rce)).state
        is SupervisorState.BLOCKED,
        "More than three candidates was accepted",
    )
    check(
        decide(item for item in (rce,)).state is SupervisorState.BLOCKED,
        "Unbounded candidate iterable was accepted",
    )
    check_raises(
        ValueError,
        arbitrate_supervisor,
        mode="shadow",
        profile=SupervisorProfile.BALANCED,
        context=context(),
        candidates=(rce,),
        now=NOW,
    )
    check_raises(
        ValueError,
        arbitrate_supervisor,
        mode=SupervisorMode.SHADOW,
        profile=SupervisorProfile.BALANCED,
        context=context(),
        candidates=(rce,),
        now=NOW.replace(tzinfo=None),
    )
    check(
        decide((replace(rce, observed_at=NOW.replace(tzinfo=None)),)).state
        is SupervisorState.BLOCKED,
        "Naive candidate timestamp was accepted",
    )
    future = replace(rce, observed_at=NOW + timedelta(microseconds=1))
    future_result = decide((future,))
    check(future_result.selected_policy is None, "Future candidate selected")
    check(
        future_result.rejected_reasons[0].reason is ReasonCode.FUTURE_CANDIDATE,
        "Future candidate reason is wrong",
    )
    not_started = replace(rce, valid_from=NOW + timedelta(microseconds=1))
    check(
        decide((not_started,)).rejected_reasons[0].reason is ReasonCode.NOT_STARTED,
        "Future valid_from did not block start",
    )
    expires_now = replace(rce, valid_until=NOW)
    check(
        decide((expires_now,)).rejected_reasons[0].reason is ReasonCode.EXPIRED,
        "valid_until == now was not expired",
    )
    expired = replace(rce, valid_until=NOW - timedelta(microseconds=1))
    check(
        decide((expired,)).rejected_reasons[0].reason is ReasonCode.EXPIRED,
        "Past valid_until was not expired",
    )
    invalid_cases = (
        replace(rce, schema_version=2),
        replace(rce, schema_version=1.0),
        replace(rce, input_revision=True),
        replace(rce, input_revision=-1),
        replace(rce, candidate_revision=True),
        replace(rce, desired_actuator_fingerprint="a" * 63),
        replace(rce, desired_actuator_fingerprint="A" * 64),
        replace(rce, desired_actuator_fingerprint=None),
        replace(rce, requested_power_kw=float("nan")),
        replace(rce, requested_energy_kwh=float("inf")),
        replace(rce, requested_power_kw=10**1000),
        replace(rce, requested_power_kw=-0.1),
        replace(rce, target_soc_percent=100.1),
        replace(rce, valid_from=NOW + timedelta(hours=2), valid_until=NOW + timedelta(hours=1)),
        replace(rce, reason_code="candidate_ready"),
    )
    for invalid in invalid_cases:
        check(
            decide((invalid,)).state is SupervisorState.BLOCKED,
            f"Invalid candidate field was accepted: {invalid}",
        )
    invalid_contract = comparable_candidate(
        PolicyId.RCE,
        1.0,
        contract_id="UPPERCASE",
    )
    check(
        decide((invalid_contract,)).state is SupervisorState.BLOCKED,
        "Invalid economic contract ID was accepted",
    )
    invalid_basis = comparable_candidate(PolicyId.RCE, 1.0, basis="b" * 63)
    check(
        decide((invalid_basis,)).state is SupervisorState.BLOCKED,
        "Invalid economic basis fingerprint was accepted",
    )
    negative_zero = replace(rce, requested_power_kw=-0.0)
    positive_zero = replace(rce, requested_power_kw=0.0)
    negative_result = decide((negative_zero,))
    positive_result = decide((positive_zero,))
    check(
        negative_result.arbitration_revision == positive_result.arbitration_revision,
        "-0.0 changed arbitration revision",
    )
    check(
        serialize_supervisor_summary(negative_result)
        == serialize_supervisor_summary(positive_result),
        "-0.0 did not canonicalize in summary",
    )


def test_candidate_order_is_irrelevant() -> None:
    items = (
        candidate(PolicyId.RCE),
        candidate(PolicyId.TARIFF),
        candidate(PolicyId.RCM),
    )
    reference = decide(items)
    reference_json = serialize_supervisor_summary(reference)
    for order in permutations(items):
        result = decide(order)
        check(result == reference, "Candidate permutation changed decision")
        check(
            serialize_supervisor_summary(result) == reference_json,
            "Candidate permutation changed serialization",
        )
    invalid_schema = replace(items[0], schema_version=2)
    invalid_shape = replace(
        items[1],
        priority_class=PriorityClass.LIVE_EMERGENCY,
        need_class=NeedClass.MANDATORY,
    )
    invalid_reference = decide((invalid_schema, invalid_shape))
    check(
        decide((invalid_shape, invalid_schema)) == invalid_reference,
        "Invalid candidate ordering changed deterministic failure",
    )


def test_public_boundary_validation() -> None:
    max_uint64 = 18_446_744_073_709_551_615
    max_int64 = 9_223_372_036_854_775_807
    max_binary64 = float.fromhex("0x1.fffffffffffffp+1023")
    bounded = comparable_candidate(
        PolicyId.RCE,
        -max_binary64,
        input_revision=max_uint64,
        candidate_revision=max_uint64,
        requested_power_kw=max_binary64,
        requested_energy_kwh=max_binary64,
        target_soc_percent=100,
        protected_soc_floor_percent=100,
        urgency=max_binary64,
        severity=max_binary64,
    )
    bounded_decision = decide((bounded,))
    check(
        bounded_decision.selected_candidate_revision == max_uint64,
        "Maximum uint64 revision was not preserved exactly",
    )
    bounded_json = serialize_supervisor_summary(bounded_decision)
    check("NaN" not in bounded_json, "Valid serialized output contains NaN")
    check("Infinity" not in bounded_json, "Valid serialized output contains Infinity")
    check("-Infinity" not in bounded_json, "Valid serialized output contains -Infinity")
    bounded_integer = replace(
        bounded,
        requested_power_kw=max_int64,
        requested_energy_kwh=max_int64,
        expected_marginal_net_benefit_pln=-max_int64 - 1,
        urgency=max_int64,
        severity=max_int64,
    )
    check(
        json.loads(serialize_supervisor_summary(decide((bounded_integer,))))[
            "candidate_summaries"
        ][0]["requested_power_kw"]
        == max_int64,
        "Signed int64 numeric boundary was not preserved exactly",
    )

    invalid_candidates = (
        replace(bounded, input_revision=max_uint64 + 1),
        replace(bounded, candidate_revision=max_uint64 + 1),
        replace(bounded, requested_power_kw=max_int64 + 1),
        replace(bounded, expected_marginal_net_benefit_pln=-max_int64 - 2),
        replace(bounded, requested_power_kw=True),
    )
    for invalid in invalid_candidates:
        check(
            decide((invalid,)).state is SupervisorState.BLOCKED,
            "Out-of-bound public candidate was accepted",
        )

    summary = bounded_decision.candidate_summaries[0]
    forged_cases = (
        replace(bounded_decision, supervisor_execution_authorized=True),
        replace(
            bounded_decision,
            selected_candidate_revision=max_uint64 + 1,
        ),
        replace(
            bounded_decision,
            candidate_summaries=(replace(summary, reason_code="forged_reason"),),
        ),
        replace(
            bounded_decision,
            candidate_summaries=(replace(summary, requested_power_kw=float("nan")),),
        ),
        replace(
            bounded_decision,
            candidate_summaries=(replace(summary, requested_power_kw=max_int64 + 1),),
        ),
        replace(
            bounded_decision,
            candidate_summaries=(summary, summary),
        ),
        replace(
            bounded_decision,
            profile_effects_not_applied=("battery_wear_weight",),
        ),
        replace(
            bounded_decision,
            supervisor_mode=SupervisorMode.OFF,
        ),
    )
    for forged in forged_cases:
        check_raises(ValueError, serialize_supervisor_summary, forged)
    invalid_decision = decide((replace(bounded, input_revision=max_uint64 + 1),))
    check(
        json.loads(serialize_supervisor_summary(invalid_decision))["state"] == "blocked",
        "Legitimate structurally blocked decision did not serialize",
    )


def test_execution_context_and_global_blockers() -> None:
    rce = candidate(PolicyId.RCE)
    no_owner = decide((rce,), execution_context=context(owner_kind=OwnerKind.NONE))
    manual = decide((rce,), execution_context=context(owner_kind=OwnerKind.MANUAL))
    check(no_owner.execution_blocked_reason is None, "Owner none became manual")
    check(manual.selected_policy is PolicyId.RCE, "Manual hid Shadow selection")
    check(
        manual.execution_blocked_reason is ReasonCode.MANUAL_AUTHORITY,
        "Manual blocker missing",
    )
    blockers = (
        (context(physical_mode=PhysicalMode.OFF_GRID), ReasonCode.OFF_GRID),
        (context(owner_kind=OwnerKind.BALANCING), ReasonCode.BALANCING_ACTIVE),
        (context(owner_kind=OwnerKind.FOREIGN), ReasonCode.FOREIGN_OWNER),
        (context(owner_conflict=True), ReasonCode.OWNER_CONFLICT),
        (
            context(
                transaction_pending=True,
                transaction_owner_kind=OwnerKind.MANUAL,
                owner_kind=OwnerKind.MANUAL,
            ),
            ReasonCode.MANUAL_AUTHORITY,
        ),
        (context(physical_mode_fresh=False), ReasonCode.PHYSICAL_MODE_STALE),
        (context(physical_mode=PhysicalMode.UNKNOWN), ReasonCode.PHYSICAL_MODE_UNKNOWN),
        (context(critical_bms_ready=False), ReasonCode.CRITICAL_BMS_UNAVAILABLE),
    )
    for execution_context, reason in blockers:
        result = decide((rce,), execution_context=execution_context)
        check(result.selected_policy is PolicyId.RCE, f"Blocker hid Shadow: {reason}")
        check(result.state is SupervisorState.SHADOW_SELECTED, f"Wrong Shadow state: {reason}")
        check(result.execution_blocked_reason is reason, f"Wrong blocker: {reason}")
    invalid_contexts = (
        context(transaction_pending=False, transaction_owner_kind=OwnerKind.MANUAL),
        context(transaction_pending=True, transaction_owner_kind=OwnerKind.NONE),
        context(observed_at=NOW + timedelta(seconds=1)),
        context(physical_mode_fresh=1),
        replace(context(), export_state="verified_allowed"),
    )
    for invalid in invalid_contexts:
        check(
            decide((rce,), execution_context=invalid).state is SupervisorState.BLOCKED,
            "Structurally inconsistent ExecutionContext was accepted",
        )
    context_fields = {field.name for field in fields(ExecutionContext)}
    check("export_state" in context_fields, "ExportState is missing")
    check("confirmed_zero_export" not in context_fields, "Contradictory export bool exists")
    check("export_allowed_verified" not in context_fields, "Contradictory export bool exists")


def test_scope_topology_and_direction_readiness() -> None:
    rce = candidate(PolicyId.RCE)
    tariff = candidate(PolicyId.TARIFF)
    absorb = candidate(PolicyId.RCM)
    limit_export = candidate(
        PolicyId.RCM,
        priority_class=PriorityClass.PREVENTIVE_GRID,
        need_class=NeedClass.PREVENTIVE,
        requested_action=RequestedAction.RCM_LIMIT_EXPORT,
        actuator_scope=ActuatorScope.DIRECT_259,
    )
    pre_discharge = candidate(
        PolicyId.RCM,
        priority_class=PriorityClass.PREVENTIVE_GRID,
        need_class=NeedClass.PREVENTIVE,
        requested_action=RequestedAction.RCM_PRE_DISCHARGE,
        actuator_scope=ActuatorScope.EMS_BLOCK_4300_4306,
    )
    cases = (
        (rce, context(full_block_execution_ready=False)),
        (rce, context(topology_full_block_allowed=False)),
        (absorb, context(direct_306_execution_ready=False)),
        (absorb, context(topology_direct_register_allowed=False)),
        (limit_export, context(direct_259_execution_ready=False)),
    )
    for item, execution_context in cases:
        result = decide((item,), execution_context=execution_context)
        check(result.selected_policy is None, "Unavailable actuator was selected")
        check(
            result.rejected_reasons[0].reason is ReasonCode.ACTUATOR_UNAVAILABLE,
            "Wrong unavailable actuator reason",
        )
    direction_cases = (
        (rce, context(discharge_direction_ready=False)),
        (pre_discharge, context(discharge_direction_ready=False)),
        (tariff, context(charge_direction_ready=False)),
        (absorb, context(charge_direction_ready=False)),
    )
    for item, execution_context in direction_cases:
        result = decide((item,), execution_context=execution_context)
        check(result.selected_policy is None, "Direction-unready action was selected")
        check(
            result.rejected_reasons[0].reason is ReasonCode.DIRECTION_UNAVAILABLE,
            "Wrong direction rejection",
        )
    neutral = decide(
        (limit_export,),
        execution_context=context(
            charge_direction_ready=False,
            discharge_direction_ready=False,
        ),
    )
    check(
        neutral.selected_policy is PolicyId.RCM,
        "RCEm limit-export was not direction-neutral",
    )
    direct_only = context(
        full_block_execution_ready=False,
        direct_306_execution_ready=True,
        direct_259_execution_ready=True,
    )
    check(
        decide((rce,), execution_context=direct_only).selected_policy is None,
        "Direct readiness authorized full block",
    )
    full_only = context(
        full_block_execution_ready=True,
        direct_306_execution_ready=False,
        direct_259_execution_ready=False,
    )
    check(
        decide((absorb,), execution_context=full_only).selected_policy is None,
        "Full-block readiness authorized direct register",
    )


def test_priority_order() -> None:
    rce = candidate(PolicyId.RCE)
    required_tariff = candidate(
        PolicyId.TARIFF,
        priority_class=PriorityClass.REQUIRED_ENERGY,
        need_class=NeedClass.MANDATORY,
        reason_code=ReasonCode.REQUIRED_ENERGY_RESTORE,
    )
    emergency = candidate(
        PolicyId.RCM,
        priority_class=PriorityClass.LIVE_EMERGENCY,
        need_class=NeedClass.MANDATORY,
        requested_action=RequestedAction.RCM_ABSORB_PV,
        actuator_scope=ActuatorScope.DIRECT_306,
        reason_code=ReasonCode.LIVE_EMERGENCY,
    )
    preventive = candidate(PolicyId.RCM)
    check(
        decide((rce, required_tariff, emergency)).selected_policy is PolicyId.RCM,
        "RCEm live emergency did not win",
    )
    check(
        decide((rce, required_tariff, preventive)).selected_policy is PolicyId.TARIFF,
        "Mandatory tariff did not beat preventive/economic",
    )
    check(
        decide((rce, preventive)).selected_policy is PolicyId.RCM,
        "Preventive RCEm did not beat economic RCE",
    )
    for profile in SupervisorProfile:
        result = decide((rce, required_tariff), profile=profile)
        check(result.selected_policy is PolicyId.TARIFF, "Profile changed mandatory priority")
    check(
        "external_override" not in {item.value for item in PriorityClass},
        "External authority leaked into policy priority",
    )
    check(
        "optional" not in {item.value for item in PriorityClass},
        "Optional duplicated NeedClass in PriorityClass",
    )
    impossible_peer = replace(
        rce,
        priority_class=PriorityClass.REQUIRED_ENERGY,
        need_class=NeedClass.MANDATORY,
    )
    winner, reason = supervisor_module._select_shadow_winner(
        (impossible_peer, required_tariff)
    )
    check(winner is None, "Impossible non-economic tie selected lexically")
    check(
        reason is ReasonCode.INCONSISTENT_PRIORITY_TIE,
        "Impossible non-economic tie did not fail closed structurally",
    )


def test_economic_value_status_and_comparison() -> None:
    unavailable_with_value = replace(
        candidate(PolicyId.RCE),
        expected_marginal_net_benefit_pln=1.0,
    )
    check(
        decide((unavailable_with_value,)).state is SupervisorState.BLOCKED,
        "Unavailable economic status accepted a value",
    )
    provisional = replace(
        candidate(PolicyId.RCE),
        economic_value_status=EconomicValueStatus.PROVISIONAL,
        expected_marginal_net_benefit_pln=1.0,
    )
    check(
        decide((provisional,)).selected_policy is PolicyId.RCE,
        "Single provisional economic candidate was hidden",
    )
    provisional_with_contract = replace(
        provisional,
        economic_contract_id=CONTRACT_ID,
    )
    check(
        decide((provisional_with_contract,)).state is SupervisorState.BLOCKED,
        "Provisional value accepted comparison contract metadata",
    )
    incompatible = replace(
        candidate(PolicyId.RCE),
        economic_value_status=EconomicValueStatus.INCOMPATIBLE,
        expected_marginal_net_benefit_pln=1.0,
    )
    comparable_missing = replace(
        candidate(PolicyId.RCE),
        economic_value_status=EconomicValueStatus.COMPARABLE,
    )
    check(
        decide((comparable_missing,)).state is SupervisorState.BLOCKED,
        "Comparable status accepted missing comparison fields",
    )

    rce_high = comparable_candidate(PolicyId.RCE, 2.0)
    tariff_low = comparable_candidate(PolicyId.TARIFF, 1.0)
    check(
        decide((rce_high, tariff_low)).selected_policy is PolicyId.RCE,
        "Higher comparable RCE value did not win",
    )
    tariff_high = comparable_candidate(PolicyId.TARIFF, 3.0)
    check(
        decide((rce_high, tariff_high)).selected_policy is PolicyId.TARIFF,
        "Higher comparable tariff value did not win",
    )
    different_contract = comparable_candidate(
        PolicyId.TARIFF,
        3.0,
        contract_id="supervisor.other.v1",
    )
    different_basis = comparable_candidate(PolicyId.TARIFF, 3.0, basis=HASH_C)
    for other in (different_contract, different_basis, candidate(PolicyId.TARIFF)):
        result = decide((rce_high, other))
        check(result.selected_policy is None, "Non-comparable economics chose winner")
        check(
            result.selection_reason
            is ReasonCode.ECONOMIC_CANDIDATES_NOT_COMPARABLE,
            "Non-comparable economics has wrong reason",
        )
    provisional_pair = decide((provisional, tariff_low))
    check(provisional_pair.selected_policy is None, "Provisional value entered comparison")
    incompatible_pair = decide((incompatible, tariff_low))
    check(incompatible_pair.selected_policy is None, "Incompatible value entered comparison")
    tie = decide((comparable_candidate(PolicyId.RCE, 1.0), tariff_low))
    check(tie.selected_policy is None, "Economic tie invented a winner")
    check(tie.selection_reason is ReasonCode.ECONOMIC_TIE, "Tie reason is wrong")


def test_commitment_and_pending_contract() -> None:
    active_rce = candidate(
        PolicyId.RCE,
        active_latched=True,
        start_eligible=False,
        continuation_eligible=True,
        result_current=False,
        recalculation_pending=True,
        economic_value_status=EconomicValueStatus.UNAVAILABLE,
    )
    owner_context = context(owner_kind=OwnerKind.RCE)
    retained = decide(
        (active_rce, candidate(PolicyId.TARIFF)),
        execution_context=owner_context,
    )
    check(retained.selected_policy is PolicyId.RCE, "Pending commitment was dropped")
    check(
        retained.selection_kind is SelectionKind.PRESERVED_COMMITMENT,
        "Commitment selection kind is wrong",
    )
    check(
        retained.execution_phase is ExecutionPhase.OBSERVED_ACTIVE_LATCHED,
        "Active commitment phase is missing",
    )
    comparable_active = comparable_candidate(
        PolicyId.RCE,
        1.0,
        active_latched=True,
        continuation_eligible=True,
    )
    much_better_tariff = comparable_candidate(PolicyId.TARIFF, 100.0)
    retained_without_threshold = decide(
        (comparable_active, much_better_tariff),
        execution_context=owner_context,
    )
    check(
        retained_without_threshold.selected_policy is PolicyId.RCE,
        "Unproven switch threshold replaced active commitment",
    )
    new_pending = replace(
        candidate(PolicyId.RCE),
        recalculation_pending=True,
        result_current=False,
    )
    new_pending_result = decide((new_pending,))
    check(new_pending_result.selected_policy is None, "Pending result started new run")
    check(
        new_pending_result.rejected_reasons[0].reason
        is ReasonCode.RECALCULATION_PENDING_NEW_START,
        "Pending new-start reason is wrong",
    )
    stale_active = replace(
        active_rce,
        recalculation_pending=False,
        result_current=False,
    )
    check(
        decide((stale_active,), execution_context=owner_context).selected_policy is None,
        "Non-pending stale commitment was retained",
    )
    hard_stop = replace(active_rce, local_hard_stop=True)
    stopped = decide((hard_stop,), execution_context=owner_context)
    check(stopped.selected_policy is None, "Local hard stop retained commitment")
    check(
        stopped.rejected_reasons[0].reason is ReasonCode.LOCAL_HARD_STOP,
        "Hard-stop reason is wrong",
    )
    expired = replace(active_rce, valid_until=NOW)
    check(
        decide((expired,), execution_context=owner_context).selected_policy is None,
        "Expired commitment was retained",
    )
    benign_pending_context = context(
        owner_kind=OwnerKind.RCE,
        transaction_pending=True,
        transaction_owner_kind=OwnerKind.RCE,
    )
    benign = decide((active_rce,), execution_context=benign_pending_context)
    check(benign.selected_policy is PolicyId.RCE, "Benign same-policy pending lost commitment")
    check(
        benign.execution_blocked_reason is ReasonCode.TRANSACTION_PENDING,
        "Pending execution blocker is missing",
    )
    unknown_pending_context = context(
        owner_kind=OwnerKind.RCE,
        transaction_pending=True,
        transaction_owner_kind=OwnerKind.UNKNOWN,
    )
    unknown = decide((active_rce,), execution_context=unknown_pending_context)
    check(unknown.state is SupervisorState.SHADOW_IDLE, "Unknown pending owner was benign")
    check(
        unknown.rejected_reasons[0].reason is ReasonCode.TRANSACTION_PENDING,
        "Unknown pending owner reason is wrong",
    )


def test_owner_and_pending_consistency() -> None:
    active_rce = candidate(PolicyId.RCE, active_latched=True)
    active_tariff = candidate(PolicyId.TARIFF, active_latched=True)
    invalid_cases = (
        (
            (active_rce, active_tariff),
            context(owner_kind=OwnerKind.RCE),
            ReasonCode.MULTIPLE_ACTIVE_COMMITMENTS,
        ),
        (
            (active_rce,),
            context(owner_kind=OwnerKind.TARIFF),
            ReasonCode.OWNER_COMMITMENT_MISMATCH,
        ),
        (
            (active_rce,),
            context(owner_kind=OwnerKind.NONE),
            ReasonCode.OWNER_COMMITMENT_MISMATCH,
        ),
        (
            (candidate(PolicyId.RCE),),
            context(owner_kind=OwnerKind.RCE),
            ReasonCode.OWNER_COMMITMENT_MISMATCH,
        ),
        (
            (active_rce,),
            context(
                owner_kind=OwnerKind.RCE,
                transaction_pending=True,
                transaction_owner_kind=OwnerKind.TARIFF,
            ),
            ReasonCode.INVALID_PENDING_OWNER_RELATIONSHIP,
        ),
    )
    for items, execution_context, reason in invalid_cases:
        result = decide(items, execution_context=execution_context)
        check(result.state is SupervisorState.BLOCKED, "Owner inconsistency was accepted")
        check(result.execution_blocked_reason is reason, "Wrong owner inconsistency reason")


def test_zero_export_contract() -> None:
    rce = candidate(PolicyId.RCE)
    tariff = candidate(PolicyId.TARIFF)
    states = (
        (ExportState.CONFIRMED_ZERO_EXPORT, ReasonCode.CONFIRMED_ZERO_EXPORT),
        (ExportState.PROHIBITED, ReasonCode.EXPORT_PROHIBITED),
        (ExportState.UNVERIFIED, ReasonCode.EXPORT_UNVERIFIED),
    )
    for export_state, reason in states:
        result = decide(
            (rce,),
            execution_context=context(export_state=export_state),
        )
        check(result.selected_policy is None, f"RCE selected under {export_state}")
        check(result.rejected_reasons[0].reason is reason, f"Wrong {export_state} reason")
    check(
        decide(
            (rce,),
            execution_context=context(export_state=ExportState.VERIFIED_ALLOWED),
        ).selected_policy
        is PolicyId.RCE,
        "Verified export did not allow RCE",
    )
    tariff_zero = decide(
        (tariff,),
        execution_context=context(export_state=ExportState.CONFIRMED_ZERO_EXPORT),
    )
    check(tariff_zero.selected_policy is PolicyId.TARIFF, "Zero export blocked tariff")
    pre_discharge = candidate(
        PolicyId.RCM,
        priority_class=PriorityClass.PREVENTIVE_GRID,
        need_class=NeedClass.PREVENTIVE,
        requested_action=RequestedAction.RCM_PRE_DISCHARGE,
        actuator_scope=ActuatorScope.EMS_BLOCK_4300_4306,
    )
    check(
        decide(
            (pre_discharge,),
            execution_context=context(export_state=ExportState.UNVERIFIED),
        ).selected_policy
        is PolicyId.RCM,
        "Pure arbiter invented RCEm pre-discharge export logic",
    )


def test_profiles_are_neutral_and_bounded() -> None:
    rce = candidate(PolicyId.RCE)
    tariff = candidate(PolicyId.TARIFF)
    results = [decide((rce, tariff), profile=profile) for profile in SupervisorProfile]
    for result in results:
        check(result.selected_policy is None, "Profile invented economic winner")
        serialized = json.loads(serialize_supervisor_summary(result))
        check(
            serialized["profile_table_version"] == PROFILE_TABLE_VERSION == 1,
            "Profile table is not explicitly versioned",
        )
        check(not result.profile_effects_applied, "Phase 1A applied profile tuning")
        check(
            tuple(effect.value for effect in result.profile_effects_not_applied)
            == (
                "economic_minimum_advantage",
                "switching_advantage",
                "commitment_preference",
                "soft_reserve",
                "battery_wear_weight",
                "preferred_throughput",
                "minimum_hold",
            ),
            "Profile not_applied schema is incomplete or unstable",
        )
        check(
            len(set(result.profile_effects_not_applied))
            == len(result.profile_effects_not_applied),
            "Profile effects contain duplicates",
        )
    mandatory = candidate(
        PolicyId.TARIFF,
        priority_class=PriorityClass.REQUIRED_ENERGY,
        need_class=NeedClass.MANDATORY,
        reason_code=ReasonCode.REQUIRED_ENERGY_RESTORE,
    )
    for profile in SupervisorProfile:
        check(
            decide((rce, mandatory), profile=profile).selected_policy
            is PolicyId.TARIFF,
            "Profile altered mandatory tariff priority",
        )


def test_temporal_and_semantic_fingerprint() -> None:
    rce = candidate(PolicyId.RCE)
    first = decide((rce,))
    second = decide((rce,))
    check(first == second, "Identical semantic input changed decision")
    check(len(first.arbitration_revision) == 64, "Revision is not SHA-256 hex")
    check(
        all(character in "0123456789abcdef" for character in first.arbitration_revision),
        "Revision is not lowercase hexadecimal",
    )
    inside_interval = decide((rce,), now=NOW + timedelta(minutes=10))
    check(
        first.arbitration_revision == inside_interval.arbitration_revision,
        "Moving now inside validity interval caused fingerprint churn",
    )
    refreshed_candidate = replace(rce, observed_at=NOW - timedelta(milliseconds=1))
    refreshed_context = replace(context(), observed_at=NOW - timedelta(milliseconds=2))
    refreshed = decide((refreshed_candidate,), execution_context=refreshed_context)
    check(
        first.arbitration_revision == refreshed.arbitration_revision,
        "Observed-at refresh changed semantic revision",
    )
    starts_later = replace(rce, valid_from=NOW + timedelta(minutes=5))
    before_start = decide((starts_later,), now=NOW)
    after_start = decide((starts_later,), now=NOW + timedelta(minutes=5))
    check(
        before_start.arbitration_revision != after_start.arbitration_revision,
        "Crossing valid_from did not change revision",
    )
    check(
        before_start.candidate_summaries[0].temporal_status
        is TemporalStatus.NOT_STARTED,
        "Before-start temporal status is wrong",
    )
    check(
        after_start.candidate_summaries[0].temporal_status is TemporalStatus.VALID,
        "valid_from boundary did not become valid",
    )
    expires = replace(rce, valid_until=NOW + timedelta(minutes=5))
    before_expiry = decide((expires,), now=NOW + timedelta(minutes=4, seconds=59))
    at_expiry = decide((expires,), now=NOW + timedelta(minutes=5))
    check(
        before_expiry.arbitration_revision != at_expiry.arbitration_revision,
        "valid_until boundary did not change revision",
    )
    check(
        at_expiry.candidate_summaries[0].temporal_status is TemporalStatus.EXPIRED,
        "valid_until == now is not expired",
    )
    presentation_change = decide((replace(rce, requested_power_kw=50.0),))
    check(
        first.arbitration_revision == presentation_change.arbitration_revision,
        "Presentation-only requested power changed revision",
    )
    provisional_one = replace(
        rce,
        economic_value_status=EconomicValueStatus.PROVISIONAL,
        expected_marginal_net_benefit_pln=1.0,
    )
    provisional_two = replace(
        provisional_one,
        expected_marginal_net_benefit_pln=2.0,
    )
    provisional_results = (decide((provisional_one,)), decide((provisional_two,)))
    check(
        provisional_results[0].selected_policy
        is provisional_results[1].selected_policy,
        "Provisional diagnostic value changed selection",
    )
    check(
        provisional_results[0].arbitration_revision
        == provisional_results[1].arbitration_revision,
        "Provisional diagnostic value churned semantic revision",
    )
    incompatible_one = replace(
        rce,
        economic_value_status=EconomicValueStatus.INCOMPATIBLE,
        expected_marginal_net_benefit_pln=1.0,
    )
    incompatible_two = replace(
        incompatible_one,
        expected_marginal_net_benefit_pln=2.0,
    )
    incompatible_results = (
        decide((incompatible_one,)),
        decide((incompatible_two,)),
    )
    check(
        incompatible_results[0].selected_policy
        is incompatible_results[1].selected_policy,
        "Incompatible diagnostic value changed selection",
    )
    check(
        incompatible_results[0].arbitration_revision
        == incompatible_results[1].arbitration_revision,
        "Incompatible diagnostic value churned semantic revision",
    )
    comparable_tariff = comparable_candidate(PolicyId.TARIFF, 2.0)
    comparable_low = decide((comparable_candidate(PolicyId.RCE, 1.0), comparable_tariff))
    comparable_high = decide((comparable_candidate(PolicyId.RCE, 3.0), comparable_tariff))
    check(
        comparable_low.arbitration_revision != comparable_high.arbitration_revision,
        "Consumed comparable value did not change semantic revision",
    )
    changed_revision = decide((replace(rce, candidate_revision=99),))
    check(
        first.arbitration_revision != changed_revision.arbitration_revision,
        "Candidate revision did not change arbitration revision",
    )
    changed_target = decide((replace(rce, desired_actuator_fingerprint=HASH_C),))
    check(
        first.arbitration_revision != changed_target.arbitration_revision,
        "Actuator target did not change arbitration revision",
    )
    warsaw = ZoneInfo("Europe/Warsaw")
    same_instant = replace(
        rce,
        observed_at=rce.observed_at.astimezone(warsaw),
        valid_from=rce.valid_from.astimezone(warsaw),
        valid_until=rce.valid_until.astimezone(warsaw),
    )
    same_instant_result = decide((same_instant,))
    check(
        first.arbitration_revision == same_instant_result.arbitration_revision,
        "Equivalent timezone instants changed revision",
    )
    invalid_before = decide((replace(rce, input_revision=-1),))
    original_profile_version = supervisor_module.PROFILE_TABLE_VERSION
    try:
        supervisor_module.PROFILE_TABLE_VERSION = original_profile_version + 1
        valid_after_version = decide((rce,))
        invalid_after_version = decide((replace(rce, input_revision=-1),))
    finally:
        supervisor_module.PROFILE_TABLE_VERSION = original_profile_version
    check(
        first.arbitration_revision != valid_after_version.arbitration_revision,
        "Profile-table version did not change valid semantic revision",
    )
    check(
        invalid_before.arbitration_revision
        != invalid_after_version.arbitration_revision,
        "Profile-table version did not change invalid semantic revision",
    )


def test_bounded_serialization() -> None:
    long_contract = "a" + "x" * 63
    max_uint64 = 18_446_744_073_709_551_615
    max_binary64 = float.fromhex("0x1.fffffffffffffp+1023")
    common = {
        "available": False,
        "input_revision": max_uint64,
        "candidate_revision": max_uint64,
        "requested_power_kw": max_binary64,
        "requested_energy_kwh": max_binary64,
        "target_soc_percent": 100,
        "protected_soc_floor_percent": 100,
        "economic_value_status": EconomicValueStatus.COMPARABLE,
        "economic_contract_id": long_contract,
        "economic_basis_fingerprint": HASH_B,
        "expected_marginal_net_benefit_pln": -max_binary64,
        "urgency": max_binary64,
        "severity": max_binary64,
        "blocked_reason": ReasonCode.ECONOMIC_CANDIDATES_NOT_COMPARABLE,
        "reason_code": ReasonCode.ECONOMIC_CANDIDATES_NOT_COMPARABLE,
    }
    rce = candidate(
        PolicyId.RCE,
        requested_mode=PhysicalMode.GRID_DISCHARGE,
        **common,
    )
    tariff = candidate(
        PolicyId.TARIFF,
        requested_mode=PhysicalMode.GRID_CHARGE,
        **common,
    )
    rcm = candidate(
        PolicyId.RCM,
        requested_mode=PhysicalMode.GRID_DISCHARGE,
        requested_action=RequestedAction.RCM_PRE_DISCHARGE,
        actuator_scope=ActuatorScope.EMS_BLOCK_4300_4306,
        **common,
    )
    decision = decide((rce, tariff, rcm), profile=SupervisorProfile.HIGH_RESERVE_WINTER)
    serialized = serialize_supervisor_summary(decision)
    encoded = serialized.encode("utf-8")
    check(
        len(encoded) <= MAX_SUPERVISOR_SUMMARY_BYTES,
        f"Worst Supervisor summary is {len(encoded)} bytes",
    )
    payload = json.loads(serialized)
    check(len(payload["candidate_summaries"]) == 3, "Summary lost candidates")
    candidate_sizes = []
    for summary in payload["candidate_summaries"]:
        size = len(
            json.dumps(
                summary,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        candidate_sizes.append(size)
        check(
            size <= MAX_CANDIDATE_SUMMARY_BYTES,
            f"Worst candidate summary is {size} bytes",
        )
    forbidden_fragments = ("planned_charges", "price_slots", "history", "forecast")
    check(
        not any(fragment in serialized for fragment in forbidden_fragments),
        "Unbounded plan/history data leaked into summary",
    )
    check(
        payload["supervisor_execution_authorized"] is False,
        "Serialized Phase 1A result authorized execution",
    )
    print(
        "BOUNDED_SUMMARY",
        f"total_bytes={len(encoded)}",
        f"max_candidate_bytes={max(candidate_sizes)}",
    )


def test_rce_market_charging_is_absent() -> None:
    check(
        {item.value for item in PolicyId} == {"rce", "tariff", "rcm"},
        "Unexpected policy identity exists",
    )
    actions = {item.value for item in RequestedAction}
    check("rce_charge" not in actions, "RCE charge action exists")
    check("dynamic_import_price" not in actions, "Dynamic import action exists")
    candidate_fields = {field.name for field in fields(PolicyCandidate)}
    check(
        not any("market_import" in name or "rce_charge" in name for name in candidate_fields),
        "RCE market-charge field exists",
    )
    decision_fields = {field.name for field in fields(type(decide(())))}
    check("grant_policy" not in decision_fields, "Phase 1A decision contains grant policy")
    check("grant_token" not in decision_fields, "Phase 1A decision contains grant token")
    source = SOURCE_PATH.read_text(encoding="utf-8").lower()
    check("rce_charge" not in source, "Dormant RCE charge code exists")
    check("dynamic_import" not in source, "Dormant dynamic import code exists")


TESTS = (
    test_structure_and_static_safety,
    test_modes_permissions_and_basic_selection,
    test_complete_policy_shape_matrix,
    test_input_validation_and_temporal_boundaries,
    test_candidate_order_is_irrelevant,
    test_public_boundary_validation,
    test_execution_context_and_global_blockers,
    test_scope_topology_and_direction_readiness,
    test_priority_order,
    test_economic_value_status_and_comparison,
    test_commitment_and_pending_contract,
    test_owner_and_pending_consistency,
    test_zero_export_contract,
    test_profiles_are_neutral_and_bounded,
    test_temporal_and_semantic_fingerprint,
    test_bounded_serialization,
    test_rce_market_charging_is_absent,
)


def main() -> None:
    for test in TESTS:
        test()
    print(
        "EMS Supervisor Phase 1A:",
        f"{len(TESTS)}/{len(TESTS)} groups passed;",
        f"{CHECK_COUNT} contract checks passed",
    )


if __name__ == "__main__":
    main()
