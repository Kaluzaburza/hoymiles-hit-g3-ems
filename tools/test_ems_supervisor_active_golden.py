"""Frozen Shadow-to-Active pure-decision parity and mutation gate for A2."""

from __future__ import annotations

from dataclasses import fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
SOURCE_PATH = COMPONENT / "ems_supervisor.py"
FIXTURE_PATH = ROOT / "tools" / "fixtures" / "ems_supervisor_shadow_decisions_v1.json"
sys.path.insert(0, str(COMPONENT))

import ems_supervisor as CORE  # noqa: E402


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64
CONTRACT_ID = "supervisor.marginal-run.v1"
EXPECTED_FIXTURE_SHA256 = "d58219a267dae7101bd8e2eac1488b6bf3d487e1ef8f8a433f9a298a197fb21d"
PARITY_FIELDS = (
    "selected_policy",
    "selection_reason",
    "rejected_reasons",
)


def _context(**overrides: Any) -> Any:
    values: dict[str, Any] = {
        "observed_at": NOW - timedelta(seconds=1),
        "physical_mode": CORE.PhysicalMode.SELF_USE,
        "physical_mode_fresh": True,
        "owner_kind": CORE.OwnerKind.NONE,
        "owner_conflict": False,
        "transaction_pending": False,
        "transaction_owner_kind": CORE.OwnerKind.NONE,
        "full_block_execution_ready": True,
        "direct_306_execution_ready": True,
        "direct_259_execution_ready": True,
        "topology_full_block_allowed": True,
        "topology_direct_register_allowed": True,
        "charge_direction_ready": True,
        "discharge_direction_ready": True,
        "critical_bms_ready": True,
        "export_state": CORE.ExportState.VERIFIED_ALLOWED,
        "battery_soc_percent": 50.0,
        "physical_protected_soc_floor_percent": 10.0,
    }
    values.update(overrides)
    return CORE.ExecutionContext(**values)


_DEFAULT_SHAPES = {
    CORE.PolicyId.RCE: (
        CORE.PriorityClass.ECONOMIC,
        CORE.NeedClass.OPTIONAL,
        CORE.RequestedAction.RCE_EXPORT,
        CORE.ActuatorScope.EMS_BLOCK_4300_4306,
        CORE.ReasonCode.ECONOMIC_CANDIDATE,
    ),
    CORE.PolicyId.TARIFF: (
        CORE.PriorityClass.ECONOMIC,
        CORE.NeedClass.OPTIONAL,
        CORE.RequestedAction.TARIFF_BATTERY_CHARGE,
        CORE.ActuatorScope.EMS_BLOCK_4300_4306,
        CORE.ReasonCode.ECONOMIC_CANDIDATE,
    ),
    CORE.PolicyId.RCM: (
        CORE.PriorityClass.PREVENTIVE_GRID,
        CORE.NeedClass.PREVENTIVE,
        CORE.RequestedAction.RCM_ABSORB_PV,
        CORE.ActuatorScope.DIRECT_306,
        CORE.ReasonCode.PREVENTIVE_VOLTAGE_ACTION,
    ),
}


def _candidate(policy_id: Any, **overrides: Any) -> Any:
    priority, need, action, scope, reason = _DEFAULT_SHAPES[policy_id]
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
            CORE.PolicyId.RCE: 11,
            CORE.PolicyId.TARIFF: 12,
            CORE.PolicyId.RCM: 13,
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
        "economic_value_status": CORE.EconomicValueStatus.UNAVAILABLE,
    }
    values.update(overrides)
    if (
        values["requested_action"] is CORE.RequestedAction.RCE_EXPORT
        and "protected_soc_floor_percent" not in overrides
    ):
        values["protected_soc_floor_percent"] = 20.0
    if values["requested_action"] is CORE.RequestedAction.RCM_PRE_DISCHARGE:
        if "protected_soc_floor_percent" not in overrides:
            values["protected_soc_floor_percent"] = 20.0
        if "target_soc_percent" not in overrides:
            values["target_soc_percent"] = 30.0
    if "desired_actuator_fingerprint" not in overrides:
        values["desired_actuator_fingerprint"] = (
            None
            if values["requested_action"] is CORE.RequestedAction.NONE
            else HASH_A
        )
    return CORE.PolicyCandidate(**values)


def _no_action(policy_id: Any, **overrides: Any) -> Any:
    return _candidate(
        policy_id,
        priority_class=CORE.PriorityClass.NONE,
        need_class=CORE.NeedClass.NONE,
        requested_action=CORE.RequestedAction.NONE,
        actuator_scope=CORE.ActuatorScope.NONE,
        reason_code=CORE.ReasonCode.NO_ACTION,
        start_eligible=False,
        continuation_eligible=False,
        desired_actuator_fingerprint=None,
        **overrides,
    )


def _comparable(
    policy_id: Any,
    value: float,
    *,
    contract_id: str = CONTRACT_ID,
    basis: str = HASH_B,
    **overrides: Any,
) -> Any:
    return _candidate(
        policy_id,
        economic_value_status=CORE.EconomicValueStatus.COMPARABLE,
        economic_contract_id=contract_id,
        economic_basis_fingerprint=basis,
        expected_marginal_net_benefit_pln=value,
        **overrides,
    )


def _required_tariff(**overrides: Any) -> Any:
    return _candidate(
        CORE.PolicyId.TARIFF,
        priority_class=CORE.PriorityClass.REQUIRED_ENERGY,
        need_class=CORE.NeedClass.MANDATORY,
        reason_code=CORE.ReasonCode.REQUIRED_ENERGY_RESTORE,
        **overrides,
    )


def _rcm(
    priority: Any,
    action: Any,
    scope: Any,
    **overrides: Any,
) -> Any:
    mandatory = priority is CORE.PriorityClass.LIVE_EMERGENCY
    return _candidate(
        CORE.PolicyId.RCM,
        priority_class=priority,
        need_class=(
            CORE.NeedClass.MANDATORY if mandatory else CORE.NeedClass.PREVENTIVE
        ),
        requested_action=action,
        actuator_scope=scope,
        reason_code=(
            CORE.ReasonCode.LIVE_EMERGENCY
            if mandatory
            else CORE.ReasonCode.PREVENTIVE_VOLTAGE_ACTION
        ),
        **overrides,
    )


def _active(policy_id: Any, **overrides: Any) -> Any:
    return _candidate(
        policy_id,
        active_latched=True,
        start_eligible=False,
        continuation_eligible=True,
        **overrides,
    )


def _matrix_inputs() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []

    def add(
        case_id: str,
        candidates: tuple[Any, ...],
        *,
        execution_context: Any | None = None,
        profile: Any = CORE.SupervisorProfile.BALANCED,
        now: datetime = NOW,
    ) -> None:
        if any(case["id"] == case_id for case in cases):
            raise AssertionError(f"duplicate golden case id: {case_id}")
        cases.append(
            {
                "id": case_id,
                "profile": profile,
                "context": execution_context or _context(),
                "candidates": candidates,
                "now": now,
            }
        )

    add("empty", ())
    for policy in CORE.PolicyId:
        add(f"no_action_{policy.value}", (_no_action(policy),))

    rce = _candidate(CORE.PolicyId.RCE)
    tariff = _candidate(CORE.PolicyId.TARIFF)
    preventive = _candidate(CORE.PolicyId.RCM)
    emergency_absorb = _rcm(
        CORE.PriorityClass.LIVE_EMERGENCY,
        CORE.RequestedAction.RCM_ABSORB_PV,
        CORE.ActuatorScope.DIRECT_306,
    )
    emergency_limit = _rcm(
        CORE.PriorityClass.LIVE_EMERGENCY,
        CORE.RequestedAction.RCM_LIMIT_EXPORT,
        CORE.ActuatorScope.DIRECT_259,
    )
    preventive_limit = _rcm(
        CORE.PriorityClass.PREVENTIVE_GRID,
        CORE.RequestedAction.RCM_LIMIT_EXPORT,
        CORE.ActuatorScope.DIRECT_259,
    )
    preventive_discharge = _rcm(
        CORE.PriorityClass.PREVENTIVE_GRID,
        CORE.RequestedAction.RCM_PRE_DISCHARGE,
        CORE.ActuatorScope.EMS_BLOCK_4300_4306,
    )
    legal = (
        ("legal_rce_economic", rce),
        ("legal_tariff_economic", tariff),
        ("legal_tariff_required", _required_tariff()),
        ("legal_rcm_emergency_absorb", emergency_absorb),
        ("legal_rcm_emergency_limit", emergency_limit),
        ("legal_rcm_preventive_absorb", preventive),
        ("legal_rcm_preventive_limit", preventive_limit),
        ("legal_rcm_preventive_discharge", preventive_discharge),
    )
    for case_id, item in legal:
        add(case_id, (item,))

    add("priority_emergency", (rce, _required_tariff(), emergency_absorb))
    add("priority_required", (rce, _required_tariff(), preventive))
    add("priority_preventive", (rce, preventive))
    add("priority_all_default", (rce, tariff, preventive))

    add("economic_rce_wins", (_comparable(CORE.PolicyId.RCE, 3.0), _comparable(CORE.PolicyId.TARIFF, 2.0)))
    add("economic_tariff_wins", (_comparable(CORE.PolicyId.RCE, 1.0), _comparable(CORE.PolicyId.TARIFF, 2.0)))
    add("economic_tie", (_comparable(CORE.PolicyId.RCE, 2.0), _comparable(CORE.PolicyId.TARIFF, 2.0)))
    add("economic_contract_mismatch", (_comparable(CORE.PolicyId.RCE, 3.0), _comparable(CORE.PolicyId.TARIFF, 2.0, contract_id="other.contract.v1")))
    add("economic_basis_mismatch", (_comparable(CORE.PolicyId.RCE, 3.0), _comparable(CORE.PolicyId.TARIFF, 2.0, basis=HASH_C)))
    add("economic_provisional_pair", (replace(rce, economic_value_status=CORE.EconomicValueStatus.PROVISIONAL, expected_marginal_net_benefit_pln=3.0), replace(tariff, economic_value_status=CORE.EconomicValueStatus.PROVISIONAL, expected_marginal_net_benefit_pln=2.0)))
    add("economic_incompatible_pair", (replace(rce, economic_value_status=CORE.EconomicValueStatus.INCOMPATIBLE, expected_marginal_net_benefit_pln=3.0), replace(tariff, economic_value_status=CORE.EconomicValueStatus.INCOMPATIBLE, expected_marginal_net_benefit_pln=2.0)))

    gates = (
        ("not_allowed", {"allowed_by_user": False}),
        ("policy_disabled", {"enabled": False}),
        ("unavailable", {"available": False}),
    )
    for policy in CORE.PolicyId:
        for suffix, overrides in gates:
            add(f"gate_{policy.value}_{suffix}", (_candidate(policy, **overrides),))
    add("gate_future_candidate", (replace(rce, observed_at=NOW + timedelta(microseconds=1)),))
    add("gate_not_started", (replace(rce, valid_from=NOW + timedelta(microseconds=1)),))
    add("gate_expired", (replace(rce, valid_until=NOW),))
    add("gate_local_hard_stop", (replace(rce, local_hard_stop=True),))
    add("gate_result_not_current", (replace(rce, result_current=False),))
    add("gate_recalculation_pending", (replace(rce, result_current=False, recalculation_pending=True),))
    add("gate_not_start_eligible", (replace(rce, start_eligible=False),))

    scope_cases = (
        ("scope_rce_actuator", rce, _context(full_block_execution_ready=False)),
        ("scope_rce_topology", rce, _context(topology_full_block_allowed=False)),
        ("scope_rce_direction", rce, _context(discharge_direction_ready=False)),
        ("scope_tariff_direction", tariff, _context(charge_direction_ready=False)),
        ("scope_rcm_306_actuator", preventive, _context(direct_306_execution_ready=False)),
        ("scope_rcm_direct_topology", preventive, _context(topology_direct_register_allowed=False)),
        ("scope_rcm_259_actuator", preventive_limit, _context(direct_259_execution_ready=False)),
        ("scope_rcm_predischarge_direction", preventive_discharge, _context(discharge_direction_ready=False)),
    )
    for case_id, item, execution_context in scope_cases:
        add(case_id, (item,), execution_context=execution_context)

    export_cases = (
        ("export_verified", CORE.ExportState.VERIFIED_ALLOWED),
        ("export_confirmed_zero", CORE.ExportState.CONFIRMED_ZERO_EXPORT),
        ("export_prohibited", CORE.ExportState.PROHIBITED),
        ("export_unverified", CORE.ExportState.UNVERIFIED),
    )
    for case_id, export_state in export_cases:
        add(case_id, (rce,), execution_context=_context(export_state=export_state))
    add("zero_export_tariff_allowed", (tariff,), execution_context=_context(export_state=CORE.ExportState.CONFIRMED_ZERO_EXPORT))
    add("zero_export_rcm_predischarge_policy_only", (preventive_discharge,), execution_context=_context(export_state=CORE.ExportState.CONFIRMED_ZERO_EXPORT))

    blockers = (
        ("blocker_manual", _context(owner_kind=CORE.OwnerKind.MANUAL)),
        ("blocker_balancing", _context(owner_kind=CORE.OwnerKind.BALANCING)),
        ("blocker_foreign", _context(owner_kind=CORE.OwnerKind.FOREIGN)),
        ("blocker_unknown_owner", _context(owner_kind=CORE.OwnerKind.UNKNOWN)),
        ("blocker_off_grid", _context(physical_mode=CORE.PhysicalMode.OFF_GRID)),
        ("blocker_owner_conflict", _context(owner_conflict=True)),
        ("blocker_physical_stale", _context(physical_mode_fresh=False)),
        ("blocker_physical_unknown", _context(physical_mode=CORE.PhysicalMode.UNKNOWN)),
        ("blocker_bms", _context(critical_bms_ready=False)),
        ("blocker_manual_transaction", _context(owner_kind=CORE.OwnerKind.MANUAL, transaction_pending=True, transaction_owner_kind=CORE.OwnerKind.MANUAL)),
    )
    for case_id, execution_context in blockers:
        add(case_id, (rce,), execution_context=execution_context)

    active_rce = _active(CORE.PolicyId.RCE)
    active_tariff = _active(CORE.PolicyId.TARIFF)
    active_rcm = _active(CORE.PolicyId.RCM)
    add("commitment_rce", (active_rce, tariff), execution_context=_context(owner_kind=CORE.OwnerKind.RCE))
    add("commitment_rce_pending_result", (replace(active_rce, result_current=False, recalculation_pending=True), tariff), execution_context=_context(owner_kind=CORE.OwnerKind.RCE))
    add("commitment_rce_beats_value", (_comparable(CORE.PolicyId.RCE, 1.0, active_latched=True, start_eligible=False), _comparable(CORE.PolicyId.TARIFF, 100.0)), execution_context=_context(owner_kind=CORE.OwnerKind.RCE))
    add("commitment_tariff", (rce, active_tariff), execution_context=_context(owner_kind=CORE.OwnerKind.TARIFF))
    add("commitment_rcm", (rce, active_rcm), execution_context=_context(owner_kind=CORE.OwnerKind.RCM))
    add("commitment_not_continuation_eligible", (replace(active_rce, continuation_eligible=False),), execution_context=_context(owner_kind=CORE.OwnerKind.RCE))
    add("commitment_result_stale", (replace(active_rce, result_current=False),), execution_context=_context(owner_kind=CORE.OwnerKind.RCE))
    add("commitment_local_hard_stop", (replace(active_rce, local_hard_stop=True),), execution_context=_context(owner_kind=CORE.OwnerKind.RCE))
    add("commitment_expired", (replace(active_rce, valid_until=NOW),), execution_context=_context(owner_kind=CORE.OwnerKind.RCE))
    add("commitment_transaction_same_owner", (active_rce,), execution_context=_context(owner_kind=CORE.OwnerKind.RCE, transaction_pending=True, transaction_owner_kind=CORE.OwnerKind.RCE))
    add("commitment_transaction_unknown", (active_rce,), execution_context=_context(owner_kind=CORE.OwnerKind.RCE, transaction_pending=True, transaction_owner_kind=CORE.OwnerKind.UNKNOWN))

    add("structural_duplicate_policy", (rce, replace(rce, candidate_revision=99)))
    add("structural_invalid_shape", (replace(rce, priority_class=CORE.PriorityClass.REQUIRED_ENERGY, need_class=CORE.NeedClass.MANDATORY),))
    add("structural_multiple_commitments", (active_rce, active_tariff), execution_context=_context(owner_kind=CORE.OwnerKind.RCE))
    add("structural_owner_commitment_mismatch", (active_rce,), execution_context=_context(owner_kind=CORE.OwnerKind.TARIFF))
    add("structural_owner_without_commitment", (rce,), execution_context=_context(owner_kind=CORE.OwnerKind.RCE))
    add("structural_pending_owner_mismatch", (active_rce,), execution_context=_context(owner_kind=CORE.OwnerKind.RCE, transaction_pending=True, transaction_owner_kind=CORE.OwnerKind.TARIFF))
    add("structural_pending_without_commitment", (rce,), execution_context=_context(transaction_pending=True, transaction_owner_kind=CORE.OwnerKind.RCE))

    for profile in CORE.SupervisorProfile:
        add(f"profile_neutral_{profile.value}", (rce, tariff), profile=profile)
    return cases


def _encode(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _encode(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, tuple):
        return [_encode(item) for item in value]
    if isinstance(value, list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {key: _encode(item) for key, item in value.items()}
    return value


def _decision_projection(decision: Any) -> dict[str, Any]:
    return {
        "selected_policy": (
            decision.selected_policy.value if decision.selected_policy is not None else None
        ),
        "selection_reason": decision.selection_reason.value,
        "rejected_reasons": [
            {"policy_id": rejection.policy_id.value, "reason": rejection.reason.value}
            for rejection in decision.rejected_reasons
        ],
    }


def _frozen_projection(case: dict[str, Any]) -> dict[str, Any]:
    expected = case["shadow_expected"]
    return {field: expected[field] for field in PARITY_FIELDS}


def _emit_shadow_golden() -> None:
    shadow = getattr(CORE.SupervisorMode, "SHADOW", None)
    if shadow is None:
        raise AssertionError("Shadow source no longer exists; fixture is already frozen")
    cases = []
    for item in _matrix_inputs():
        decision = CORE.arbitrate_supervisor(
            mode=shadow,
            profile=item["profile"],
            context=item["context"],
            candidates=item["candidates"],
            now=item["now"],
        )
        cases.append(
            {
                "id": item["id"],
                "input": {
                    "profile": _encode(item["profile"]),
                    "context": _encode(item["context"]),
                    "candidates": _encode(item["candidates"]),
                    "now": _encode(item["now"]),
                },
                "shadow_expected": _decision_projection(decision),
            }
        )
    payload = {
        "schema_version": 1,
        "frozen_from": {
            "mode": "shadow",
            "source_sha256": hashlib.sha256(SOURCE_PATH.read_bytes()).hexdigest(),
            "base_head": "3112cde86d2969e39b2e7f29e0b9fef380d06716",
        },
        "coverage": {
            "case_count": len(cases),
            "domains": [
                "legal_candidate_shapes",
                "candidate_gates",
                "temporal_boundaries",
                "actuator_topology_direction",
                "zero_export",
                "priority_and_economics",
                "global_execution_blockers",
                "owner_commitments",
                "structural_fail_closed",
                "neutral_profiles",
            ],
        },
        "cases": cases,
    }
    print(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )


_CONTEXT_ENUMS = {
    "physical_mode": CORE.PhysicalMode,
    "owner_kind": CORE.OwnerKind,
    "transaction_owner_kind": CORE.OwnerKind,
    "export_state": CORE.ExportState,
}
_CANDIDATE_ENUMS = {
    "policy_id": CORE.PolicyId,
    "requested_action": CORE.RequestedAction,
    "actuator_scope": CORE.ActuatorScope,
    "priority_class": CORE.PriorityClass,
    "need_class": CORE.NeedClass,
    "reason_code": CORE.ReasonCode,
    "blocked_reason": CORE.ReasonCode,
    "economic_value_status": CORE.EconomicValueStatus,
    "requested_mode": CORE.PhysicalMode,
}


def _instant(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _decode_context(payload: dict[str, Any]) -> Any:
    values = dict(payload)
    values.setdefault("battery_soc_percent", 50.0)
    values.setdefault("physical_protected_soc_floor_percent", 10.0)
    values["observed_at"] = _instant(values["observed_at"])
    for key, enum_type in _CONTEXT_ENUMS.items():
        values[key] = enum_type(values[key])
    return CORE.ExecutionContext(**values)


def _decode_candidate(payload: dict[str, Any]) -> Any:
    values = dict(payload)
    if values["requested_action"] == "tariff_charge":
        # The immutable fixture records the pre-A2 merged identity.  A2 maps it
        # to the most specific equivalent while separately testing all three
        # exact tariff actions below.
        values["requested_action"] = "tariff_battery_charge"
    if (
        values["requested_action"] == "rce_export"
        and values.get("protected_soc_floor_percent") is None
    ):
        # The frozen pre-Active fixture predates the explicit direction floor.
        # Add the prior valid physical premise without changing its decisions.
        values["protected_soc_floor_percent"] = 20.0
    if values["requested_action"] == "rcm_pre_discharge":
        if values.get("protected_soc_floor_percent") is None:
            values["protected_soc_floor_percent"] = 20.0
        if values.get("target_soc_percent") is None:
            values["target_soc_percent"] = 30.0
    for key in ("observed_at", "valid_from", "valid_until"):
        if values[key] is not None:
            values[key] = _instant(values[key])
    for key, enum_type in _CANDIDATE_ENUMS.items():
        if values[key] is not None:
            values[key] = enum_type(values[key])
    return CORE.PolicyCandidate(**values)


def _load_fixture() -> dict[str, Any]:
    raw = FIXTURE_PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_FIXTURE_SHA256:
        raise AssertionError(
            f"golden fixture drift: expected {EXPECTED_FIXTURE_SHA256}, got {digest}"
        )
    payload = json.loads(raw)
    if payload.get("schema_version") != 1:
        raise AssertionError("unsupported golden fixture schema")
    if payload.get("frozen_from") != {
        "base_head": "3112cde86d2969e39b2e7f29e0b9fef380d06716",
        "mode": "shadow",
        "source_sha256": "031d0abf8948d24708b960deb6ee71c7fe952fdebecd915ac6fc766596429ca7",
    }:
        raise AssertionError("golden provenance drift")
    return payload


def _evaluate(case: dict[str, Any]) -> dict[str, Any]:
    inputs = case["input"]
    decision = CORE.arbitrate_supervisor(
        mode=CORE.SupervisorMode.ACTIVE,
        profile=CORE.SupervisorProfile(inputs["profile"]),
        context=_decode_context(inputs["context"]),
        candidates=tuple(_decode_candidate(item) for item in inputs["candidates"]),
        now=_instant(inputs["now"]),
    )
    if decision.supervisor_execution_authorized is not False:
        raise AssertionError(f"{case['id']}: pure decision granted execution authority")
    return _decision_projection(decision)


def _mismatch_count(cases: list[dict[str, Any]]) -> int:
    return sum(_evaluate(case) != _frozen_projection(case) for case in cases)


def test_frozen_shadow_active_parity() -> int:
    fixture = _load_fixture()
    cases = fixture["cases"]
    if fixture["coverage"]["case_count"] != len(cases) or len(cases) < 70:
        raise AssertionError("golden matrix is incomplete")
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)):
        raise AssertionError("golden case IDs are not unique")
    for case in cases:
        actual = _evaluate(case)
        expected = _frozen_projection(case)
        if actual != expected:
            raise AssertionError(
                f"{case['id']}: Active drifted from frozen Shadow: "
                f"expected={expected!r} actual={actual!r}"
            )
    return len(cases)


def test_tariff_action_identity_parity() -> int:
    fixture = _load_fixture()
    case = next(item for item in fixture["cases"] if item["id"] == "legal_tariff_economic")
    inputs = case["input"]
    base = _decode_candidate(inputs["candidates"][0])
    expected = _frozen_projection(case)
    actions = (
        CORE.RequestedAction.TARIFF_BATTERY_CHARGE,
        CORE.RequestedAction.TARIFF_GRID_SUPPORT,
        CORE.RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
    )
    revisions: set[str] = set()
    for action in actions:
        decision = CORE.arbitrate_supervisor(
            mode=CORE.SupervisorMode.ACTIVE,
            profile=CORE.SupervisorProfile(inputs["profile"]),
            context=_decode_context(inputs["context"]),
            candidates=(replace(base, requested_action=action),),
            now=_instant(inputs["now"]),
        )
        actual = _decision_projection(decision)
        if actual != expected:
            raise AssertionError(
                f"{action.value}: exact tariff identity changed selection parity: "
                f"expected={expected!r} actual={actual!r}"
            )
        if decision.candidate_summaries[0].requested_action is not action:
            raise AssertionError(f"{action.value}: identity collapsed in decision summary")
        revisions.add(decision.arbitration_revision)
    if len(revisions) != len(actions):
        raise AssertionError("exact tariff actions collapsed in arbitration revision")
    return len(actions)


def test_golden_mutation_gate() -> int:
    fixture = _load_fixture()
    cases = fixture["cases"]
    mutations = 0

    original_select = CORE._select_winner
    try:
        def wrong_winner(eligible: tuple[Any, ...]) -> tuple[Any | None, Any]:
            if not eligible:
                return None, CORE.ReasonCode.NO_ELIGIBLE_CANDIDATE
            return eligible[-1], CORE.ReasonCode.ECONOMIC_CANDIDATE

        CORE._select_winner = wrong_winner
        if _mismatch_count(cases) == 0:
            raise AssertionError("winner mutation survived the golden gate")
        mutations += 1
    finally:
        CORE._select_winner = original_select

    original_reason = CORE._selection_reason
    try:
        CORE._selection_reason = lambda _candidate: CORE.ReasonCode.NO_ACTION
        if _mismatch_count(cases) == 0:
            raise AssertionError("selection-reason mutation survived the golden gate")
        mutations += 1
    finally:
        CORE._selection_reason = original_reason

    original_rejection = CORE._candidate_rejection
    try:
        CORE._candidate_rejection = lambda _candidate, _context, _temporal: None
        if _mismatch_count(cases) == 0:
            raise AssertionError("rejection mutation survived the golden gate")
        mutations += 1
    finally:
        CORE._candidate_rejection = original_rejection
    return mutations


def test_shadow_semantics_absent_from_pure_decision() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8").lower()
    forbidden = (
        "shadow",
        "active_not_implemented",
        "tariff_charge",
    )
    present = [token for token in forbidden if token in source]
    if present:
        raise AssertionError(f"pure decision retains Shadow semantics: {present}")
    if {mode.value for mode in CORE.SupervisorMode} != {"off", "active"}:
        raise AssertionError("SupervisorMode is not exactly Off/Active")
    if {state.value for state in CORE.SupervisorState} != {
        "off",
        "active_idle",
        "active_selected",
        "blocked",
    }:
        raise AssertionError("SupervisorState retains a non-Active selection state")


def main() -> None:
    cases = test_frozen_shadow_active_parity()
    tariff_actions = test_tariff_action_identity_parity()
    mutations = test_golden_mutation_gate()
    test_shadow_semantics_absent_from_pure_decision()
    print(
        "EMS Supervisor A2 golden:",
        f"{cases} frozen decisions matched;",
        f"{tariff_actions} tariff identities preserved;",
        f"{mutations}/3 mutations killed",
    )


if __name__ == "__main__":
    if "--emit-shadow-golden" in sys.argv:
        _emit_shadow_golden()
    else:
        main()
