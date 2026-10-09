"""Deterministic and mutation tests for the output-only canonical SOC ledger."""

from __future__ import annotations

import ast
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
SOURCE = COMPONENT / "supervisor_canonical_ledger.py"
FIXTURE = ROOT / "tools" / "fixtures" / "supervisor_canonical_ledger_v1.json"
sys.path.insert(0, str(COMPONENT))

import supervisor_canonical_ledger as LEDGER  # noqa: E402


NOW = datetime(2026, 8, 21, 12, 0, tzinfo=timezone.utc)
ARBITRATION_REVISION = "a" * 64


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _expect_value_error(callable_: Callable[[], Any], message: str) -> None:
    try:
        callable_()
    except ValueError:
        return
    raise AssertionError(message)


def _candidate(
    policy: Any,
    action: Any,
    *,
    eligible: bool,
    start: Any,
    revision: int,
    rejected: tuple[str, ...] = (),
) -> Any:
    return LEDGER.PolicyCandidate(
        policy_id=policy,
        requested_action=action,
        eligible=eligible,
        start_eligibility=start,
        input_revision=7,
        candidate_revision=revision,
        rejected_reasons=rejected,
    )


def _block_values(*, mode: int = 4) -> tuple[Any, ...]:
    # Intentionally non-canonical order. The module must canonicalize it.
    return (
        LEDGER.ExpectationValue("maximum_discharge_power", 50.0),
        LEDGER.ExpectationValue("mode_code", mode),
        LEDGER.ExpectationValue("backup_soc", 60.0),
        LEDGER.ExpectationValue("force_charge_soc", 95.0),
        LEDGER.ExpectationValue("self_use_soc", 20.0),
        LEDGER.ExpectationValue("force_discharge_soc", 20.0),
        LEDGER.ExpectationValue("maximum_charge_power", 50.0),
    )


def _expectations(
    target: Any,
    physical: Any,
    *,
    generation: int | None,
    values: tuple[Any, ...],
) -> tuple[Any, Any]:
    return (
        LEDGER.CommandExpectation(
            target=target,
            source_generation=generation,
            values=values,
        ),
        LEDGER.ReadbackExpectation(
            target=target,
            newer_than_source_generation=(generation is not None),
            values=tuple(reversed(values)),
            physical_expectation=physical,
        ),
    )


def _baseline_slots() -> tuple[Any, ...]:
    command_0, readback_0 = _expectations(
        LEDGER.CommandTarget.EMS_BLOCK_4300_4306,
        LEDGER.PhysicalExpectation.GRID_IMPORT_AND_BATTERY_CHARGE,
        generation=71,
        values=_block_values(mode=4),
    )
    command_1, readback_1 = _expectations(
        LEDGER.CommandTarget.BATTERY_CHARGE_LIMIT_306,
        LEDGER.PhysicalExpectation.PV_SURPLUS_AND_BATTERY_CHARGE,
        generation=72,
        values=(
            LEDGER.ExpectationValue("battery_charge_limit_percent", 45.0),
        ),
    )
    command_2, readback_2 = _expectations(
        LEDGER.CommandTarget.NONE,
        LEDGER.PhysicalExpectation.NONE,
        generation=None,
        values=(),
    )
    rce_blocked = _candidate(
        LEDGER.PolicyId.RCE,
        LEDGER.RequestedAction.RCE_EXPORT,
        eligible=False,
        start=LEDGER.StartEligibility.BLOCKED,
        revision=11,
        rejected=("confirmed_zero_export",),
    )
    tariff_selected = _candidate(
        LEDGER.PolicyId.TARIFF,
        LEDGER.RequestedAction.TARIFF_BATTERY_CHARGE,
        eligible=True,
        start=LEDGER.StartEligibility.ELIGIBLE,
        revision=12,
    )
    rcm_lower = _candidate(
        LEDGER.PolicyId.RCM,
        LEDGER.RequestedAction.RCM_ABSORB_PV,
        eligible=True,
        start=LEDGER.StartEligibility.ELIGIBLE,
        revision=13,
        rejected=("lower_priority",),
    )
    slot_0 = LEDGER.CanonicalSlotInput(
        slot_id="2026-08-21t12:00z",
        starts_at=NOW,
        ends_at=NOW + timedelta(minutes=30),
        candidates=(tariff_selected, rce_blocked, rcm_lower),
        selected_policy=LEDGER.PolicyId.TARIFF,
        selected_action=LEDGER.RequestedAction.TARIFF_BATTERY_CHARGE,
        start_eligibility=LEDGER.StartEligibility.ELIGIBLE,
        owner=LEDGER.OwnerKind.SUPERVISOR,
        planned=LEDGER.PlannedEnergy(
            pv_kwh=0.5,
            load_kwh=0.4,
            battery_kwh=1.05,
            grid_kwh=1.0,
        ),
        flows=LEDGER.BatteryEnergyFlows(
            pv_to_battery_kwh=0.5,
            grid_to_battery_kwh=0.6,
            battery_to_load_kwh=0.0,
            battery_to_grid_kwh=0.0,
            losses_kwh=0.05,
        ),
        protected_reserve_percent=20.0,
        command_expectation=command_0,
        readback_expectation=readback_0,
    )

    tariff_outside = replace(
        tariff_selected,
        rejected_reasons=("outside_tariff_window",),
    )
    rcm_selected = replace(rcm_lower, rejected_reasons=())
    slot_1 = LEDGER.CanonicalSlotInput(
        slot_id="2026-08-21t12:30z",
        starts_at=NOW + timedelta(minutes=30),
        ends_at=NOW + timedelta(hours=1),
        candidates=(rcm_selected, tariff_outside, rce_blocked),
        selected_policy=LEDGER.PolicyId.RCM,
        selected_action=LEDGER.RequestedAction.RCM_ABSORB_PV,
        start_eligibility=LEDGER.StartEligibility.ELIGIBLE,
        owner=LEDGER.OwnerKind.SUPERVISOR,
        planned=LEDGER.PlannedEnergy(
            pv_kwh=1.5,
            load_kwh=0.4,
            battery_kwh=0.75,
            grid_kwh=-0.3,
        ),
        flows=LEDGER.BatteryEnergyFlows(
            pv_to_battery_kwh=0.8,
            grid_to_battery_kwh=0.0,
            battery_to_load_kwh=0.0,
            battery_to_grid_kwh=0.0,
            losses_kwh=0.05,
        ),
        protected_reserve_percent=20.0,
        command_expectation=command_1,
        readback_expectation=readback_1,
    )

    no_action_candidates = (
        replace(
            rce_blocked,
            requested_action=LEDGER.RequestedAction.NONE,
            rejected_reasons=("no_action",),
        ),
        replace(
            tariff_selected,
            requested_action=LEDGER.RequestedAction.NONE,
            eligible=False,
            start_eligibility=LEDGER.StartEligibility.BLOCKED,
            rejected_reasons=("no_action",),
        ),
        replace(
            rcm_lower,
            requested_action=LEDGER.RequestedAction.NONE,
            eligible=False,
            start_eligibility=LEDGER.StartEligibility.BLOCKED,
            rejected_reasons=("no_action",),
        ),
    )
    slot_2 = LEDGER.CanonicalSlotInput(
        slot_id="2026-08-21t13:00z",
        starts_at=NOW + timedelta(hours=1),
        ends_at=NOW + timedelta(hours=1, minutes=30),
        candidates=no_action_candidates,
        selected_policy=LEDGER.PolicyId.NONE,
        selected_action=LEDGER.RequestedAction.NONE,
        start_eligibility=LEDGER.StartEligibility.NOT_APPLICABLE,
        owner=LEDGER.OwnerKind.NONE,
        planned=LEDGER.PlannedEnergy(
            pv_kwh=0.0,
            load_kwh=0.45,
            battery_kwh=-0.5,
            grid_kwh=0.0,
        ),
        flows=LEDGER.BatteryEnergyFlows(
            pv_to_battery_kwh=0.0,
            grid_to_battery_kwh=0.0,
            battery_to_load_kwh=0.45,
            battery_to_grid_kwh=0.0,
            losses_kwh=0.05,
        ),
        protected_reserve_percent=20.0,
        command_expectation=command_2,
        readback_expectation=readback_2,
    )
    return (slot_0, slot_1, slot_2)


def _build(*, slots: tuple[Any, ...] | None = None, **overrides: Any) -> Any:
    values: dict[str, Any] = {
        "built_at": NOW,
        "arbitration_revision": ARBITRATION_REVISION,
        "usable_capacity_kwh": 10.0,
        "initial_soc_percent": 50.0,
        "slots": _baseline_slots() if slots is None else slots,
    }
    values.update(overrides)
    return LEDGER.build_canonical_execution_ledger(**values)


def _fixture() -> dict[str, Any]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    _assert(
        payload.get("schema")
        == "hoymiles.supervisor-canonical-ledger.golden.v1",
        "unexpected golden schema",
    )
    return payload


def test_golden_and_required_projection() -> str:
    fixture = _fixture()
    ledger = _build()
    serialized = LEDGER.serialize_canonical_execution_ledger(ledger)
    payload = json.loads(serialized)
    revision = LEDGER.canonical_execution_ledger_revision(ledger)
    serialized_sha = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    _assert(
        len(ledger.slots) == fixture["expected_slot_count"],
        "golden slot count drift",
    )
    _assert(
        ledger.final_soc_percent == fixture["expected_final_soc_percent"],
        "golden final SOC drift",
    )
    _assert(
        ledger.reserve_violation_count
        == fixture["expected_reserve_violation_count"],
        "golden reserve verdict drift",
    )
    _assert(
        revision == fixture["expected_ledger_revision"],
        f"ledger revision drift: {revision}",
    )
    _assert(
        serialized_sha == fixture["expected_serialized_sha256"],
        f"serialized golden drift: {serialized_sha}",
    )
    _assert(payload["output_only"] is True, "ledger is not explicitly output-only")
    _assert("authority" not in serialized, "ledger projection contains authority")
    required_slot_keys = {
        "policy_candidates",
        "selected_policy",
        "selected_action",
        "rejected_reasons",
        "start_eligibility",
        "owner",
        "planned",
        "soc_equation",
        "protected_reserve",
        "command_expectation",
        "readback_expectation",
    }
    _assert(
        required_slot_keys <= payload["slots"][0].keys(),
        "canonical slot projection is incomplete",
    )
    return serialized_sha


def test_independent_soc_equation_and_continuity() -> None:
    ledger = _build()
    previous_end = ledger.initial_soc_percent
    max_energy_residual = 0.0
    for slot in ledger.slots:
        _assert(
            slot.soc_start_percent == previous_end,
            "slot did not start at the previous canonical end",
        )
        flow_delta = (
            slot.flows.pv_to_battery_kwh
            + slot.flows.grid_to_battery_kwh
            - slot.flows.battery_to_load_kwh
            - slot.flows.battery_to_grid_kwh
            - slot.flows.losses_kwh
        )
        expected_end = slot.soc_start_percent + 100.0 * flow_delta / 10.0
        max_energy_residual = max(
            max_energy_residual,
            abs(expected_end - slot.soc_end_percent),
        )
        previous_end = slot.soc_end_percent
    _assert(max_energy_residual <= 1e-12, "independent SOC residual is non-zero")
    audit = LEDGER.audit_canonical_execution_ledger(ledger)
    _assert(audit.valid, f"baseline audit failed: {audit.failures}")
    _assert(audit.max_continuity_residual_percent == 0.0, "continuity drift")
    _assert(audit.max_energy_balance_residual_kwh == 0.0, "energy drift")
    _assert(audit.max_system_balance_residual_kwh == 0.0, "system drift")


def test_semantic_order_and_timezone_determinism() -> None:
    baseline = _build()
    slots = _baseline_slots()
    shuffled: list[Any] = []
    for slot in slots:
        shuffled_candidates = tuple(
            replace(
                candidate,
                rejected_reasons=tuple(reversed(candidate.rejected_reasons)),
            )
            for candidate in reversed(slot.candidates)
        )
        shifted = replace(
            slot,
            starts_at=slot.starts_at.astimezone(timezone(timedelta(hours=2))),
            ends_at=slot.ends_at.astimezone(timezone(timedelta(hours=2))),
            candidates=shuffled_candidates,
            command_expectation=replace(
                slot.command_expectation,
                values=tuple(reversed(slot.command_expectation.values)),
            ),
            readback_expectation=replace(
                slot.readback_expectation,
                values=tuple(reversed(slot.readback_expectation.values)),
            ),
        )
        shuffled.append(shifted)
    equivalent = _build(
        built_at=NOW.astimezone(timezone(timedelta(hours=-5))),
        slots=tuple(shuffled),
    )
    _assert(
        LEDGER.serialize_canonical_execution_ledger(baseline)
        == LEDGER.serialize_canonical_execution_ledger(equivalent),
        "semantic ordering or equivalent timezone changed serialization",
    )


def test_fail_closed_inputs() -> None:
    slots = _baseline_slots()
    first = slots[0]
    second = slots[1]
    selected = next(
        candidate
        for candidate in first.candidates
        if candidate.policy_id is LEDGER.PolicyId.TARIFF
    )
    rejected = next(
        candidate
        for candidate in first.candidates
        if candidate.policy_id is LEDGER.PolicyId.RCE
    )
    cases: tuple[tuple[str, Callable[[], Any]], ...] = (
        ("missing_plan", lambda: _build(slots=(replace(first, planned=None),))),
        (
            "nonfinite_pv",
            lambda: _build(
                slots=(replace(first, planned=replace(first.planned, pv_kwh=math.nan)),)
            ),
        ),
        (
            "nonfinite_flow",
            lambda: _build(
                slots=(replace(first, flows=replace(first.flows, losses_kwh=math.inf)),)
            ),
        ),
        (
            "battery_path_mismatch",
            lambda: _build(
                slots=(replace(first, flows=replace(first.flows, grid_to_battery_kwh=0.5)),)
            ),
        ),
        (
            "system_balance_mismatch",
            lambda: _build(
                slots=(replace(first, planned=replace(first.planned, grid_kwh=0.9)),)
            ),
        ),
        (
            "pv_allocation_overflow",
            lambda: _build(
                slots=(
                    replace(
                        first,
                        planned=LEDGER.PlannedEnergy(0.4, 0.4, 1.05, 1.1),
                    ),
                )
            ),
        ),
        (
            "selected_rejected",
            lambda: _build(
                slots=(
                    replace(
                        first,
                        candidates=tuple(
                            replace(candidate, rejected_reasons=("forged",))
                            if candidate is selected
                            else candidate
                            for candidate in first.candidates
                        ),
                    ),
                )
            ),
        ),
        (
            "unselected_without_reason",
            lambda: _build(
                slots=(
                    replace(
                        first,
                        candidates=tuple(
                            replace(candidate, rejected_reasons=())
                            if candidate is rejected
                            else candidate
                            for candidate in first.candidates
                        ),
                    ),
                )
            ),
        ),
        (
            "duplicate_candidate",
            lambda: _build(
                slots=(replace(first, candidates=(selected, selected)),)
            ),
        ),
        (
            "wrong_action_policy",
            lambda: _build(
                slots=(replace(first, selected_action=LEDGER.RequestedAction.RCE_EXPORT),)
            ),
        ),
        (
            "missing_block_field",
            lambda: _build(
                slots=(
                    replace(
                        first,
                        command_expectation=replace(
                            first.command_expectation,
                            values=first.command_expectation.values[:-1],
                        ),
                    ),
                )
            ),
        ),
        (
            "wrong_readback_value",
            lambda: _build(
                slots=(
                    replace(
                        first,
                        readback_expectation=replace(
                            first.readback_expectation,
                            values=_block_values(mode=5),
                        ),
                    ),
                )
            ),
        ),
        (
            "no_new_generation",
            lambda: _build(
                slots=(
                    replace(
                        first,
                        readback_expectation=replace(
                            first.readback_expectation,
                            newer_than_source_generation=False,
                        ),
                    ),
                )
            ),
        ),
        (
            "wrong_physical_predicate",
            lambda: _build(
                slots=(
                    replace(
                        first,
                        readback_expectation=replace(
                            first.readback_expectation,
                            physical_expectation=LEDGER.PhysicalExpectation.NONE,
                        ),
                    ),
                )
            ),
        ),
        (
            "time_gap",
            lambda: _build(
                slots=(
                    first,
                    replace(second, starts_at=second.starts_at + timedelta(minutes=1)),
                )
            ),
        ),
        ("zero_capacity", lambda: _build(usable_capacity_kwh=0.0)),
        ("nonfinite_initial_soc", lambda: _build(initial_soc_percent=math.nan)),
        (
            "soc_overflow",
            lambda: _build(initial_soc_percent=99.0, slots=(first,)),
        ),
    )
    for case_id, callable_ in cases:
        _expect_value_error(callable_, f"fail-closed input survived: {case_id}")


def test_reserve_violation_is_visible_not_authority() -> None:
    slot = _baseline_slots()[2]
    ledger = _build(initial_soc_percent=21.0, slots=(slot,))
    _assert(ledger.final_soc_percent == 16.0, "unexpected reserve test SOC")
    _assert(ledger.reserve_violation_count == 1, "reserve breach was hidden")
    _assert(not ledger.slots[0].reserve_respected, "reserve verdict was forged")
    payload = LEDGER.canonical_execution_ledger_to_dict(ledger)
    _assert(payload["output_only"] is True, "reserve output granted authority")


def test_complete_action_identity_matrix() -> None:
    cases = (
        (
            LEDGER.PolicyId.RCE,
            LEDGER.RequestedAction.RCE_EXPORT,
            LEDGER.CommandTarget.EMS_BLOCK_4300_4306,
            LEDGER.PhysicalExpectation.GRID_EXPORT_AND_BATTERY_DISCHARGE,
            _block_values(mode=5),
        ),
        (
            LEDGER.PolicyId.TARIFF,
            LEDGER.RequestedAction.TARIFF_BATTERY_CHARGE,
            LEDGER.CommandTarget.EMS_BLOCK_4300_4306,
            LEDGER.PhysicalExpectation.GRID_IMPORT_AND_BATTERY_CHARGE,
            _block_values(mode=4),
        ),
        (
            LEDGER.PolicyId.TARIFF,
            LEDGER.RequestedAction.TARIFF_GRID_SUPPORT,
            LEDGER.CommandTarget.EMS_BLOCK_4300_4306,
            LEDGER.PhysicalExpectation.GRID_IMPORT_AND_NO_BATTERY_DISCHARGE,
            _block_values(mode=4),
        ),
        (
            LEDGER.PolicyId.TARIFF,
            LEDGER.RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
            LEDGER.CommandTarget.EMS_BLOCK_4300_4306,
            LEDGER.PhysicalExpectation.GRID_IMPORT_AND_BATTERY_CHARGE,
            _block_values(mode=4),
        ),
        (
            LEDGER.PolicyId.RCM,
            LEDGER.RequestedAction.RCM_ABSORB_PV,
            LEDGER.CommandTarget.BATTERY_CHARGE_LIMIT_306,
            LEDGER.PhysicalExpectation.PV_SURPLUS_AND_BATTERY_CHARGE,
            (
                LEDGER.ExpectationValue(
                    "battery_charge_limit_percent",
                    45.0,
                ),
            ),
        ),
        (
            LEDGER.PolicyId.RCM,
            LEDGER.RequestedAction.RCM_LIMIT_EXPORT,
            LEDGER.CommandTarget.GCF_EXPORT_LIMIT_259,
            LEDGER.PhysicalExpectation.EXPORT_NOT_ABOVE_SNAPSHOT,
            (LEDGER.ExpectationValue("export_limit_percent", 0.0),),
        ),
        (
            LEDGER.PolicyId.RCM,
            LEDGER.RequestedAction.RCM_PRE_DISCHARGE,
            LEDGER.CommandTarget.EMS_BLOCK_4300_4306,
            LEDGER.PhysicalExpectation.GRID_EXPORT_AND_BATTERY_DISCHARGE,
            _block_values(mode=5),
        ),
    )
    for index, (policy, action, target, physical, values) in enumerate(cases):
        command, readback = _expectations(
            target,
            physical,
            generation=100 + index,
            values=values,
        )
        slot = LEDGER.CanonicalSlotInput(
            slot_id=f"action-{index:02d}",
            starts_at=NOW,
            ends_at=NOW + timedelta(minutes=30),
            candidates=(
                _candidate(
                    policy,
                    action,
                    eligible=True,
                    start=LEDGER.StartEligibility.ELIGIBLE,
                    revision=100 + index,
                ),
            ),
            selected_policy=policy,
            selected_action=action,
            start_eligibility=LEDGER.StartEligibility.ELIGIBLE,
            owner=LEDGER.OwnerKind.SUPERVISOR,
            planned=LEDGER.PlannedEnergy(0.0, 0.0, 0.0, 0.0),
            flows=LEDGER.BatteryEnergyFlows(0.0, 0.0, 0.0, 0.0, 0.0),
            protected_reserve_percent=20.0,
            command_expectation=command,
            readback_expectation=readback,
        )
        ledger = _build(slots=(slot,))
        payload = LEDGER.canonical_execution_ledger_to_dict(ledger)["slots"][0]
        _assert(payload["selected_action"] == action.value, "action identity drift")
        _assert(
            payload["command_expectation"]["target"] == target.value,
            "action command target drift",
        )
        _assert(
            payload["readback_expectation"]["physical_expectation"]
            == physical.value,
            "action physical expectation drift",
        )


def test_output_mutation_gate() -> int:
    ledger = _build()
    first, second, third = ledger.slots
    selected_index = next(
        index
        for index, candidate in enumerate(first.candidates)
        if candidate.policy_id is LEDGER.PolicyId.TARIFF
    )

    def replace_first(slot: Any) -> Any:
        return replace(ledger, slots=(slot, second, third))

    forged_candidates = list(first.candidates)
    forged_candidates[selected_index] = replace(
        forged_candidates[selected_index],
        rejected_reasons=("forged",),
    )
    mutations: tuple[tuple[str, Any], ...] = (
        ("soc_end", replace_first(replace(first, soc_end_percent=60.6))),
        ("soc_start", replace_first(replace(first, soc_start_percent=49.9))),
        (
            "flow",
            replace_first(
                replace(
                    first,
                    flows=replace(first.flows, grid_to_battery_kwh=0.5),
                )
            ),
        ),
        (
            "planned_battery",
            replace_first(
                replace(first, planned=replace(first.planned, battery_kwh=1.0))
            ),
        ),
        (
            "stored_continuity",
            replace_first(replace(first, continuity_residual_percent=0.1)),
        ),
        (
            "stored_energy",
            replace_first(replace(first, energy_balance_residual_kwh=0.1)),
        ),
        (
            "stored_system",
            replace_first(replace(first, system_balance_residual_kwh=0.1)),
        ),
        (
            "reserve_margin",
            replace_first(replace(first, reserve_margin_end_percent=999.0)),
        ),
        (
            "reserve_verdict",
            replace_first(replace(first, reserve_respected=False)),
        ),
        (
            "rejection_projection",
            replace_first(replace(first, rejected_reasons=())),
        ),
        (
            "candidate_projection",
            replace_first(replace(first, candidates=tuple(forged_candidates))),
        ),
        (
            "candidate_order",
            replace_first(replace(first, candidates=tuple(reversed(first.candidates)))),
        ),
        (
            "expectation_order",
            replace_first(
                replace(
                    first,
                    command_expectation=replace(
                        first.command_expectation,
                        values=tuple(reversed(first.command_expectation.values)),
                    ),
                    readback_expectation=replace(
                        first.readback_expectation,
                        values=tuple(reversed(first.readback_expectation.values)),
                    ),
                )
            ),
        ),
        (
            "readback",
            replace_first(
                replace(
                    first,
                    readback_expectation=replace(
                        first.readback_expectation,
                        newer_than_source_generation=False,
                    ),
                )
            ),
        ),
        (
            "time_continuity",
            replace(
                ledger,
                slots=(
                    first,
                    replace(second, starts_at=second.starts_at + timedelta(minutes=1)),
                    third,
                ),
            ),
        ),
        (
            "soc_continuity",
            replace(
                ledger,
                slots=(first, replace(second, soc_start_percent=60.4), third),
            ),
        ),
        ("final_soc", replace(ledger, final_soc_percent=63.1)),
        (
            "max_continuity",
            replace(ledger, max_continuity_residual_percent=0.1),
        ),
        (
            "max_energy",
            replace(ledger, max_energy_balance_residual_kwh=0.1),
        ),
        (
            "max_system",
            replace(ledger, max_system_balance_residual_kwh=0.1),
        ),
        ("reserve_count", replace(ledger, reserve_violation_count=1)),
        (
            "duplicate_slot",
            replace(ledger, slots=(first, replace(second, slot_id=first.slot_id), third)),
        ),
    )
    detected = 0
    for mutation_id, forged in mutations:
        audit = LEDGER.audit_canonical_execution_ledger(forged)
        _assert(not audit.valid, f"output mutation survived audit: {mutation_id}")
        _expect_value_error(
            lambda forged=forged: LEDGER.serialize_canonical_execution_ledger(forged),
            f"output mutation reached serialization: {mutation_id}",
        )
        detected += 1
    return detected


def test_pure_output_only_source_contract() -> None:
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
        _assert(not isinstance(node, ast.AsyncFunctionDef), "ledger defines async runtime code")
    disallowed_imports = {
        "aiohttp",
        "asyncio",
        "homeassistant",
        "os",
        "pathlib",
        "requests",
        "socket",
        "subprocess",
        "urllib",
    }
    _assert(
        not imported_roots.intersection(disallowed_imports),
        "pure ledger imports I/O/runtime dependencies",
    )
    for marker in (
        "async_call",
        "dispatch_command",
        "grant_execution",
        "hass.services",
        "modbus.write",
        "owner_acquire",
    ):
        _assert(marker not in source, f"pure ledger contains actuator marker {marker}")
    input_fields = {field.name for field in fields(LEDGER.CanonicalSlotInput)}
    _assert(
        "soc_start_percent" not in input_fields and "soc_end_percent" not in input_fields,
        "caller can splice a SOC segment into the canonical chain",
    )
    _assert(
        not any("author" in field_name for field_name in input_fields),
        "slot input contains an authority field",
    )


def main() -> None:
    test_independent_soc_equation_and_continuity()
    test_semantic_order_and_timezone_determinism()
    test_fail_closed_inputs()
    test_reserve_violation_is_visible_not_authority()
    test_complete_action_identity_matrix()
    detected = test_output_mutation_gate()
    test_pure_output_only_source_contract()
    digest = test_golden_and_required_projection()
    print(f"Canonical Supervisor SOC ledger mutation gate: detected {detected}/{detected}")
    print("Canonical Supervisor SOC ledger tests: PASS (3 slots, residual 0)")
    print(f"Canonical Supervisor SOC ledger serialized SHA-256: {digest}")


if __name__ == "__main__":
    main()
