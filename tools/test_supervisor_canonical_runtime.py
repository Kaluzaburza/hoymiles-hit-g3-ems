#!/usr/bin/env python3
"""Dependency-free post-arbitration canonical-plan contracts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))

import automation_plan_timeline as TL  # noqa: E402
from ems_supervisor import (  # noqa: E402
    ActuatorScope,
    EconomicValueStatus,
    ExecutionContext,
    ExportState,
    NeedClass,
    OwnerKind,
    PhysicalMode,
    PolicyCandidate,
    PolicyId,
    PriorityClass,
    ReasonCode,
    RequestedAction,
    SupervisorMode,
    SupervisorProfile,
    arbitrate_supervisor,
)
from supervisor_canonical_ledger import (  # noqa: E402
    BatteryEnergyFlows,
    OwnerKind as LedgerOwner,
    PlannedEnergy,
    PolicyId as LedgerPolicy,
    RequestedAction as LedgerAction,
    StartEligibility,
    audit_canonical_execution_ledger,
    canonical_execution_ledger_to_dict,
)
from supervisor_canonical_runtime import (  # noqa: E402
    CanonicalRuntimeError,
    _limit_segment_to_physical_soc,
    build_supervisor_canonical_ledger,
)
from supervisor_runtime import (  # noqa: E402
    ExecutionSourceSnapshot,
    RcmAction,
    RceSourceSnapshot,
    RcmSourceSnapshot,
    TariffSourceSnapshot,
    build_rcm_candidate,
)


UTC = timezone.utc
NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
CAPACITY = 10.0
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def no_action(policy: PolicyId, revision: int) -> PolicyCandidate:
    return PolicyCandidate(
        schema_version=1,
        policy_id=policy,
        observed_at=NOW,
        allowed_by_user=True,
        enabled=True,
        available=True,
        result_current=True,
        recalculation_pending=False,
        input_revision=1,
        candidate_revision=revision,
        start_eligible=False,
        continuation_eligible=False,
        active_latched=False,
        local_hard_stop=False,
        requested_action=RequestedAction.NONE,
        actuator_scope=ActuatorScope.NONE,
        priority_class=PriorityClass.NONE,
        need_class=NeedClass.NONE,
        reason_code=ReasonCode.NO_ACTION,
        blocked_reason=None,
        valid_from=None,
        valid_until=None,
        desired_actuator_fingerprint=None,
        economic_value_status=EconomicValueStatus.UNAVAILABLE,
    )


def execution_context(export_state: ExportState) -> ExecutionContext:
    return ExecutionContext(
        observed_at=NOW,
        physical_mode=PhysicalMode.SELF_USE,
        physical_mode_fresh=True,
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        transaction_pending=False,
        transaction_owner_kind=OwnerKind.NONE,
        full_block_execution_ready=True,
        direct_306_execution_ready=True,
        direct_259_execution_ready=True,
        topology_full_block_allowed=True,
        topology_direct_register_allowed=True,
        charge_direction_ready=True,
        discharge_direction_ready=True,
        critical_bms_ready=True,
        export_state=export_state,
        battery_soc_percent=50.0,
        physical_protected_soc_floor_percent=10.0,
    )


def execution_source() -> ExecutionSourceSnapshot:
    fresh = NOW - timedelta(seconds=1)
    return ExecutionSourceSnapshot(
        physical_mode_code=0,
        full_block_generation=10,
        full_block_generation_at=fresh,
        self_use_soc_percent=20.0,
        backup_soc_percent=80.0,
        force_charge_soc_percent=90.0,
        maximum_charge_power_percent=50.0,
        force_discharge_soc_percent=25.0,
        maximum_discharge_power_percent=40.0,
        full_block_execution_ready=True,
        direct_306_execution_ready=True,
        direct_259_execution_ready=True,
        machine_type_code=0,
        inverter_count=1,
        topology_generation_at=fresh,
        battery_soc_percent=60.0,
        battery_soc_observed_at=fresh,
        bms_voltage_v=51.2,
        bms_voltage_observed_at=fresh,
        bms_max_charge_current_a=100.0,
        bms_charge_current_observed_at=fresh,
        bms_max_discharge_current_a=100.0,
        bms_discharge_current_observed_at=fresh,
        gcf_enable_code=1,
        effective_export_limit_percent=100.0,
        gcf_generation=11,
        gcf_generation_at=fresh,
        gcf_cohort_coherent=True,
        battery_charge_limit_generation=12,
        battery_charge_limit_generation_at=fresh,
        battery_charge_limit_percent=80.0,
        hardware_readback_supported=True,
    )


def frame(export_state: ExportState = ExportState.VERIFIED_ALLOWED) -> SimpleNamespace:
    candidates = tuple(no_action(policy, index + 1) for index, policy in enumerate(PolicyId))
    context = execution_context(export_state)
    decision = arbitrate_supervisor(
        mode=SupervisorMode.ACTIVE,
        profile=SupervisorProfile.BALANCED,
        context=context,
        candidates=candidates,
        now=NOW,
    )
    return SimpleNamespace(
        now=NOW,
        decision=decision,
        candidates=candidates,
        context=context,
        rce=RceSourceSnapshot(
            effective_discharge_power_percent=40.0,
        ),
        tariff=TariffSourceSnapshot(
            requested_charge_power_kw=2.0,
            command_charge_power_percent=50.0,
        ),
        rcm=RcmSourceSnapshot(
            recommended_charge_limit_percent=60.0,
            recommended_charge_power_kw=2.0,
            pre_discharge_power_percent=20.0,
        ),
        execution=execution_source(),
    )


def actual() -> TL.CurrentActualSnapshot:
    return TL.CurrentActualSnapshot(
        observed_at=NOW,
        pv_kw=2.0,
        load_kw=1.0,
        battery_kw=0.0,
        grid_kw=-1.0,
        soc_percent=60.0,
        quality="complete",
        source_ages_seconds={key: 0.0 for key in ("pv", "load", "battery", "grid", "soc")},
    )


def _point(
    policy_id: str,
    *,
    index: int,
    selected: bool,
    action: str,
    soc_end: float,
    pv_kwh: float,
    load_kwh: float,
    grid_kwh: float,
) -> TL.TimelineTracePoint:
    minutes = 15 if policy_id == "rcm" else 30
    start = NOW + timedelta(minutes=minutes * index)
    if policy_id == "rce":
        policy: TL.RCEPolicyPoint | TL.TariffPolicyPoint | TL.RCMPolicyPoint = TL.RCEPolicyPoint(
            sell_price_pln_kwh=0.8,
            planned_export_kwh=max(-grid_kwh, 0.0),
            planned_battery_withdrawal_kwh=max(-grid_kwh, 0.0),
            target_discharge_kw=max(-grid_kwh / (minutes / 60.0), 0.0),
            command_discharge_power_percent=40.0 if selected else 0.0,
            target_tolerance_kw=0.05,
            expected_revenue_pln=max(-grid_kwh, 0.0) * 0.8,
        )
        target = None
        required_headroom = None
    elif policy_id == "tariff":
        policy = TL.TariffPolicyPoint(
            buy_price_pln_kwh=0.4,
            tariff_zone="low",
            planned_import_kwh=max(grid_kwh, 0.0),
            stored_energy_kwh=max((soc_end - 60.0) * CAPACITY / 100.0, 0.0),
            direct_load_kwh=0.0,
            planned_charge_kw=max((soc_end - 60.0) * CAPACITY / 100.0, 0.0),
            expected_cost_pln=max(grid_kwh, 0.0) * 0.4,
            expected_saving_pln=None,
            need_class="required_energy" if selected else "none",
        )
        target = soc_end if selected else None
        required_headroom = None
    else:
        policy = TL.RCMPolicyPoint(
            voltage_risk_code="no_risk",
            planned_export_limit_percent=None,
            planned_export_limit_kw=None,
            recommended_charge_power_kw=(
                2.0 if selected and action == "absorb_pv" else None
            ),
            planned_pre_discharge_kw=None,
            planned_pre_discharge_kwh=None,
            planned_pre_discharge_stored_kwh=None,
            action_start_offset_seconds=(
                0.0 if selected and action == "absorb_pv" else None
            ),
            action_end_offset_seconds=(
                900.0 if selected and action == "absorb_pv" else None
            ),
            headroom_shortfall_kwh=None,
            control_mode=action,
        )
        target = None
        required_headroom = None
    duration = minutes / 60.0
    stored_delta = (soc_end - (60.0 if index == 0 else 65.0 if policy_id == "tariff" else soc_end)) * CAPACITY / 100.0
    return TL.TimelineTracePoint(
        start=start,
        end=start + timedelta(minutes=minutes),
        pv_kwh=pv_kwh,
        load_kwh=load_kwh,
        battery_delta_kwh=stored_delta,
        grid_import_kwh=max(grid_kwh, 0.0),
        grid_export_kwh=max(-grid_kwh, 0.0),
        soc_percent=soc_end,
        baseline_soc_percent=60.0,
        protected_soc_floor_percent=20.0,
        action_code=action,
        selected=selected,
        quality="complete",
        policy=policy,
        target_soc_percent=target,
        required_headroom_kwh=required_headroom,
    )


def timelines(
    *,
    rce_selected: bool = False,
    rce_future_selected: bool = False,
    tariff_selected: bool = True,
    rcm_absorb_selected: bool = False,
) -> dict[str, dict[str, object]]:
    # Each source is physically balanced with 0.05 kWh loss in a native slot.
    rce_points = (
        _point(
            "rce",
            index=0,
            selected=rce_selected,
            action="export" if rce_selected else "idle",
            soc_end=55.0 if rce_selected else 60.0,
            pv_kwh=0.5,
            load_kwh=0.5,
            grid_kwh=-0.45 if rce_selected else 0.0,
        ),
        _point(
            "rce",
            index=1,
            selected=rce_future_selected,
            action="export" if rce_future_selected else "idle",
            soc_end=55.0 if rce_selected or rce_future_selected else 60.0,
            pv_kwh=0.5,
            load_kwh=0.5,
            grid_kwh=-0.45 if rce_future_selected else 0.0,
        ),
    )
    tariff_points = (
        _point(
            "tariff",
            index=0,
            selected=tariff_selected,
            action="battery_charge" if tariff_selected else "idle",
            soc_end=65.0 if tariff_selected else 60.0,
            pv_kwh=0.5,
            load_kwh=0.5,
            grid_kwh=0.55 if tariff_selected else 0.0,
        ),
        _point(
            "tariff",
            index=1,
            selected=False,
            action="idle",
            soc_end=65.0 if tariff_selected else 60.0,
            pv_kwh=0.5,
            load_kwh=0.5,
            grid_kwh=0.0,
        ),
    )
    rcm_points = tuple(
        _point(
            "rcm",
            index=index,
            selected=rcm_absorb_selected and index == 0,
            action="absorb_pv" if rcm_absorb_selected and index == 0 else "idle",
            soc_end=65.0 if rcm_absorb_selected else 60.0,
            pv_kwh=0.70 if rcm_absorb_selected and index == 0 else 0.25,
            load_kwh=0.20 if rcm_absorb_selected and index == 0 else 0.25,
            grid_kwh=0.0,
        )
        for index in range(4)
    )
    traces = {
        "rce": TL.OptimizerTimelineTrace("rce", rce_points),
        "tariff": TL.OptimizerTimelineTrace("tariff", tariff_points),
        "rcm": TL.OptimizerTimelineTrace("rcm", rcm_points),
    }
    return {
        policy_id: TL.build_current_payload(
            trace,
            config_entry_id="entry-a",
            generated_at=NOW,
            input_revision=7,
            plan_revision=2,
            plan_entity_id=f"sensor.{policy_id}_plan",
            current_actual=actual(),
            sources=[{"role": "plan", "entity_id": f"sensor.{policy_id}_plan"}],
            physical_active=False,
        )
        for policy_id, trace in traces.items()
    }


def test_required_tariff_wins_and_chain_is_canonical() -> None:
    ledger = build_supervisor_canonical_ledger(
        frame=frame(),
        timelines=timelines(),
        usable_capacity_kwh=CAPACITY,
    )
    audit = audit_canonical_execution_ledger(ledger)
    check(audit.valid, f"ledger audit failed: {audit.failures}")
    check(len(ledger.slots) == 4, "15-minute common grid must produce four slots")
    first = ledger.slots[0]
    check(first.selected_policy is LedgerPolicy.TARIFF, "required tariff must win")
    check(first.selected_action is LedgerAction.TARIFF_BATTERY_CHARGE, "action drift")
    check(first.owner is LedgerOwner.TARIFF, "planned owner must follow winner")
    check(first.start_eligibility is StartEligibility.ELIGIBLE, "current start must be eligible")
    check(ledger.slots[1].start_eligibility is StartEligibility.UNVERIFIED, "future authority must not be invented")
    check(abs(first.soc_end_percent - 62.5) < 1e-8, "SOC must come from slot energy, not raw endpoint")
    check(ledger.slots[1].soc_start_percent == first.soc_end_percent, "SOC chain broke")
    check(first.command_expectation.source_generation == 10, "command provenance drift")
    check(dict((item.name, item.value) for item in first.command_expectation.values)["mode_code"] == 4, "tariff must expect Grid Charge")
    payload = canonical_execution_ledger_to_dict(ledger)
    check(payload["output_only"] is True, "canonical plan must remain output-only")
    check(payload["audit"]["max_system_balance_residual_kwh"] == 0.0, "system residual must be zero")


def test_selected_rce_export_lowers_soc_through_battery_to_grid() -> None:
    ledger = build_supervisor_canonical_ledger(
        frame=frame(),
        timelines=timelines(rce_selected=True, tariff_selected=False),
        usable_capacity_kwh=CAPACITY,
    )
    audit = audit_canonical_execution_ledger(ledger)
    check(audit.valid, f"RCE export ledger audit failed: {audit.failures}")
    first = ledger.slots[0]
    check(first.selected_policy is LedgerPolicy.RCE, "RCE export was not selected")
    check(first.selected_action is LedgerAction.RCE_EXPORT, "RCE action drift")
    check(first.soc_end_percent < first.soc_start_percent, "RCE export did not lower SOC")
    check(first.flows.battery_to_grid_kwh > 0.0, "RCE export has no battery-to-grid energy")


def test_future_rce_uses_slot_command_while_current_supervisor_stays_idle() -> None:
    idle_frame = frame()
    idle_frame.rce = replace(
        idle_frame.rce,
        effective_discharge_power_percent=0.0,
    )
    check(
        idle_frame.decision.selected_policy is None,
        "current supervisor must remain idle without a current RCE action",
    )
    check(
        idle_frame.decision.state.value == "active_idle",
        "current supervisor state must remain active-idle",
    )

    ledger = build_supervisor_canonical_ledger(
        frame=idle_frame,
        timelines=timelines(
            rce_future_selected=True,
            tariff_selected=False,
        ),
        usable_capacity_kwh=CAPACITY,
    )
    future = [
        slot
        for slot in ledger.slots
        if slot.selected_policy is LedgerPolicy.RCE
    ]
    check(bool(future), "future RCE action was rejected by current-slot idle power")
    check(
        all(slot.starts_at >= NOW + timedelta(minutes=30) for slot in future),
        "future RCE action leaked into the current interval",
    )
    check(
        all(slot.soc_end_percent < slot.soc_start_percent for slot in future),
        "future RCE action did not lower canonical SOC",
    )
    expected = next(
        value.value
        for value in future[0].command_expectation.values
        if value.name == "maximum_discharge_power"
    )
    check(
        expected == 40.0,
        "future RCE command did not come from its timeline slot",
    )

    zero_command = timelines(
        rce_future_selected=True,
        tariff_selected=False,
    )
    zero_command["rce"]["points"][1]["policy"][
        "command_discharge_power_percent"
    ] = 0.0
    blocked = build_supervisor_canonical_ledger(
        frame=idle_frame,
        timelines=zero_command,
        usable_capacity_kwh=CAPACITY,
    )
    check(
        all(slot.selected_policy is not LedgerPolicy.RCE for slot in blocked.slots),
        "zero per-slot RCE command acquired a fallback execution power",
    )


def test_selected_rcm_absorb_pv_raises_soc_through_pv_to_battery() -> None:
    ledger = build_supervisor_canonical_ledger(
        frame=frame(),
        timelines=timelines(tariff_selected=False, rcm_absorb_selected=True),
        usable_capacity_kwh=CAPACITY,
    )
    audit = audit_canonical_execution_ledger(ledger)
    check(audit.valid, f"RCEm absorb-PV ledger audit failed: {audit.failures}")
    first = ledger.slots[0]
    check(first.selected_policy is LedgerPolicy.RCM, "RCEm absorb-PV was not selected")
    check(first.selected_action is LedgerAction.RCM_ABSORB_PV, "RCEm action drift")
    check(first.soc_end_percent > first.soc_start_percent, "RCEm absorb-PV did not raise SOC")
    check(first.flows.pv_to_battery_kwh > 0.0, "RCEm absorb-PV has no PV-to-battery energy")


def test_confirmed_zero_export_rejects_rce() -> None:
    ledger = build_supervisor_canonical_ledger(
        frame=frame(ExportState.CONFIRMED_ZERO_EXPORT),
        timelines=timelines(rce_selected=True, tariff_selected=False),
        usable_capacity_kwh=CAPACITY,
    )
    first = ledger.slots[0]
    check(first.selected_policy is LedgerPolicy.NONE, "zero export must leave RCE unselected")
    rce = next(item for item in first.candidates if item.policy_id is LedgerPolicy.RCE)
    check("confirmed_zero_export" in rce.rejected_reasons, "hard zero-export reason missing")
    check(first.selected_action is LedgerAction.NONE, "blocked export must not become an intent")


def test_mixed_tariff_provenance_preserves_required_priority() -> None:
    mixed = timelines()
    mixed["tariff"]["points"][0]["policy"]["need_class"] = "mixed"
    ledger = build_supervisor_canonical_ledger(
        frame=frame(),
        timelines=mixed,
        usable_capacity_kwh=CAPACITY,
    )
    first = ledger.slots[0]
    check(first.selected_policy is LedgerPolicy.TARIFF, "mixed tariff need must remain selectable")
    check(first.start_eligibility is StartEligibility.ELIGIBLE, "mixed current tariff need must remain eligible")


def test_partial_tariff_horizon_keeps_only_complete_slots_actionable() -> None:
    partial_horizon = timelines()
    partial_horizon["tariff"]["quality"] = "partial"
    partial_horizon["tariff"]["blocker_code"] = "insufficient_cheap_window"
    ledger = build_supervisor_canonical_ledger(
        frame=frame(),
        timelines=partial_horizon,
        usable_capacity_kwh=CAPACITY,
    )
    first = ledger.slots[0]
    check(
        first.selected_policy is LedgerPolicy.TARIFF,
        "a partial horizon must not invalidate its complete feasible tariff slot",
    )
    check(
        first.selected_action is LedgerAction.TARIFF_BATTERY_CHARGE,
        "a complete feasible tariff slot lost its action",
    )

    unverified_slot = timelines()
    unverified_slot["tariff"]["quality"] = "partial"
    unverified_slot["tariff"]["blocker_code"] = "insufficient_cheap_window"
    unverified_slot["tariff"]["points"][0]["quality"] = "partial"
    blocked = build_supervisor_canonical_ledger(
        frame=frame(),
        timelines=unverified_slot,
        usable_capacity_kwh=CAPACITY,
    )
    blocked_first = blocked.slots[0]
    check(
        blocked_first.selected_policy is LedgerPolicy.NONE,
        "a genuinely partial selected point must remain fail closed",
    )
    tariff_candidate = next(
        item
        for item in blocked_first.candidates
        if item.policy_id is LedgerPolicy.TARIFF
    )
    check(
        tariff_candidate.start_eligibility is StartEligibility.UNVERIFIED,
        "a genuinely partial tariff command must be marked unverified",
    )


def test_union_horizon_preserves_actions_beyond_shorter_policy_coverage() -> None:
    unequal = timelines()
    rce = unequal["rce"]
    rce["points"] = rce["points"][:1]
    rce["point_count"] = 1
    rce["horizon_end"] = rce["points"][0]["end"]

    later_tariff = unequal["tariff"]["points"][1]
    later_tariff.update(
        {
            "battery_kw": 1.0,
            "grid_kw": 1.1,
            "grid_import_kw": 1.1,
            "grid_export_kw": 0.0,
            "soc_percent": 70.0,
            "action_code": "battery_charge",
            "selected": True,
            "target_soc_percent": 70.0,
        }
    )
    later_tariff["policy"].update(
        {
            "planned_import_kwh": 0.55,
            "stored_energy_kwh": 0.5,
            "planned_charge_kw": 1.0,
            "expected_cost_pln": 0.22,
            "need_class": "required_energy",
        }
    )

    ledger = build_supervisor_canonical_ledger(
        frame=frame(),
        timelines=unequal,
        usable_capacity_kwh=CAPACITY,
    )
    check(len(ledger.slots) == 4, "union horizon was truncated by the shorter RCE source")
    later = ledger.slots[2]
    check(
        later.selected_policy is LedgerPolicy.TARIFF,
        "a valid later tariff action disappeared outside the RCE horizon",
    )
    check(
        later.selected_action is LedgerAction.TARIFF_BATTERY_CHARGE,
        "the later tariff action changed in the union horizon",
    )
    missing_rce = next(
        item for item in later.candidates if item.policy_id is LedgerPolicy.RCE
    )
    check(not missing_rce.eligible, "missing RCE coverage acquired eligibility")
    check(
        "unavailable" in missing_rce.rejected_reasons,
        "missing RCE coverage is not explicitly unavailable",
    )

    missing_current = timelines()
    current_rce = missing_current["rce"]
    current_rce["points"] = current_rce["points"][1:]
    current_rce["point_count"] = 1
    current_rce["horizon_start"] = current_rce["points"][0]["start"]
    current_rce["forecast_from"] = current_rce["points"][0]["start"]
    current = build_supervisor_canonical_ledger(
        frame=frame(),
        timelines=missing_current,
        usable_capacity_kwh=CAPACITY,
    ).slots[0]
    check(
        current.selected_policy is LedgerPolicy.NONE,
        "incomplete current policy coverage must not project a selected owner",
    )
    current_tariff = next(
        item for item in current.candidates if item.policy_id is LedgerPolicy.TARIFF
    )
    check(
        current_tariff.start_eligibility is StartEligibility.UNVERIFIED,
        "incomplete current policy coverage must remain fail closed",
    )


def test_tomorrow_rcm_horizon_does_not_hide_current_sale() -> None:
    # installation_2 2026-10-01: RCE/Pstryk covers the active sale, while RCEm's
    # headroom forecast has moved to tomorrow. Only a fresh, explicitly idle
    # live RCEm source can resolve that CURRENT display-coverage gap.
    sources = timelines(rce_selected=True, tariff_selected=False)
    rcm = sources["rcm"]
    for key in ("horizon_start", "horizon_end", "forecast_from"):
        value = TL.parse_utc_iso(rcm[key], name=key)
        rcm[key] = (value + timedelta(days=1)).isoformat()
    for point in rcm["points"]:
        for key in ("start", "end"):
            value = TL.parse_utc_iso(point[key], name=key)
            point[key] = (value + timedelta(days=1)).isoformat()
    quiet = RcmSourceSnapshot(
        observed_at=NOW,
        allowed_by_user=True,
        enabled=True,
        result_current=True,
        recalculation_pending=False,
        input_revision=1,
        live_emergency=False,
        emergency_action_ready=False,
        prediction_ready=True,
        action=RcmAction.PRESERVE_HEADROOM,
        risk_window_active=False,
        absorb_active=False,
        export_active=False,
        pre_discharge_active=False,
    )

    def project(
        source=quiet, *, export_state=ExportState.VERIFIED_ALLOWED,
        freshness_now=NOW, template_blocked=False,
    ):
        live = frame(export_state)
        live.rcm = source
        live.candidates = tuple(
            build_rcm_candidate(source, now=NOW)
            if candidate.policy_id is PolicyId.RCM else candidate
            for candidate in live.candidates
        )
        if template_blocked:
            live.candidates = tuple(
                replace(candidate, available=False, blocked_reason=ReasonCode.UNAVAILABLE)
                if candidate.policy_id is PolicyId.RCM else candidate
                for candidate in live.candidates
            )
        return build_supervisor_canonical_ledger(
            frame=live, timelines=sources, usable_capacity_kwh=CAPACITY,
            freshness_now=freshness_now,
        )

    ledger = project()
    first = ledger.slots[0]
    check(first.selected_policy is LedgerPolicy.RCE,
          "tomorrow-only RCEm forecast hid the current RCE/Pstryk sale")
    check(first.selected_action is LedgerAction.RCE_EXPORT,
          "current sale action disappeared from the canonical chart")
    check(first.soc_end_percent < first.soc_start_percent,
          "current sale did not lower the canonical SOC")
    check(first.flows.battery_to_grid_kwh > 0.0,
          "current sale lost its battery-to-grid energy")
    check(audit_canonical_execution_ledger(ledger).valid,
          "current sale coverage repair broke energy conservation")
    missing = next(c for c in first.candidates if c.policy_id is LedgerPolicy.RCM)
    check(not missing.eligible and "unavailable" in missing.rejected_reasons,
          "missing RCEm forecast acquired invented energy or eligibility")
    # Complete RCE/BUY sources and all existing physical gates still apply.
    check(project(export_state=ExportState.CONFIRMED_ZERO_EXPORT).slots[0].selected_policy
          is LedgerPolicy.NONE, "RCEm coverage repair bypassed zero export")
    check(project(freshness_now=NOW + timedelta(seconds=61)).slots[0].selected_policy
          is LedgerPolicy.NONE, "publication reused an expired live RCEm attestation")
    check(project(template_blocked=True).slots[0].selected_policy is LedgerPolicy.NONE,
          "fresh RCEm source overrode a blocked runtime candidate")
    unsafe = (
        replace(quiet, observed_at=None),
        replace(quiet, observed_at=NOW - timedelta(seconds=61)),
        replace(quiet, observed_at=NOW + timedelta(seconds=1)),
        replace(quiet, result_current=False),
        replace(quiet, recalculation_pending=True),
        replace(quiet, live_emergency=True),
        replace(quiet, absorb_active=True),
        replace(quiet, export_active=True),
        replace(quiet, pre_discharge_active=True),
        replace(quiet, action=None),
        replace(quiet, action=RcmAction.ABSORB_PV,
                charge_path_locally_valid=True, direct_register_topology_allowed=True,
                recommended_charge_limit_percent=60.0, recommended_charge_power_kw=2.0),
    )
    for source in unsafe:
        check(project(source).slots[0].selected_policy is LedgerPolicy.NONE,
              f"unknown/active/stale RCEm bypassed current coverage: {source}")


def test_rejected_tariff_action_cannot_become_the_idle_energy_backbone() -> None:
    disabled_frame = frame()
    disabled_frame.candidates = tuple(
        replace(candidate, enabled=False)
        if candidate.policy_id is PolicyId.TARIFF
        else candidate
        for candidate in disabled_frame.candidates
    )
    ledger = build_supervisor_canonical_ledger(
        frame=disabled_frame,
        timelines=timelines(),
        usable_capacity_kwh=CAPACITY,
    )
    first = ledger.slots[0]
    check(first.selected_policy is LedgerPolicy.NONE, "disabled tariff unexpectedly won")
    check(
        abs(first.soc_end_percent - 60.0) < 1e-8,
        "a rejected tariff charge still raised the canonical SOC projection",
    )
    check(
        abs(first.planned.battery_kwh) < 1e-8,
        "a rejected tariff charge still contributed planned battery energy",
    )


def test_partial_current_interval_respects_physical_soc_headroom() -> None:
    """A late canonical start must not push a valid source trace above 100%."""

    live_now = NOW + timedelta(seconds=23)
    source = timelines(tariff_selected=False)
    generated_at = live_now.isoformat().replace("+00:00", "Z")
    for payload in source.values():
        payload["generated_at"] = generated_at
        payload["current_actual"] = {
            **payload["current_actual"],
            "observed_at": generated_at,
            "soc_percent": 99.0,
        }

    # Both source points are independently physical: 99 -> 98 -> 100%.
    # Starting the canonical chain 23 seconds late retains the physical 99%
    # readback and applies the source power only for the remaining interval.
    # The resulting 0.01278 pp offset must be saturated, not invalidate the
    # complete projection or invent an export under confirmed zero export.
    first, second = source["tariff"]["points"]
    first.update(
        {
            "pv_kw": 0.0,
            "load_kw": 0.19,
            "battery_kw": -0.2,
            "grid_kw": 0.0,
            "grid_import_kw": 0.0,
            "grid_export_kw": 0.0,
            "soc_percent": 98.0,
            "baseline_soc_percent": 98.0,
        }
    )
    second.update(
        {
            "pv_kw": 0.42,
            "load_kw": 0.0,
            "battery_kw": 0.4,
            "grid_kw": 0.0,
            "grid_import_kw": 0.0,
            "grid_export_kw": 0.0,
            "soc_percent": 100.0,
            "baseline_soc_percent": 100.0,
        }
    )

    live_frame = frame(ExportState.CONFIRMED_ZERO_EXPORT)
    live_frame.now = live_now
    live_frame.context = replace(
        live_frame.context,
        observed_at=live_now,
        battery_soc_percent=99.0,
    )
    live_frame.execution = replace(
        live_frame.execution,
        battery_soc_percent=99.0,
        battery_soc_observed_at=live_now - timedelta(seconds=1),
        effective_export_limit_percent=0.0,
    )

    ledger = build_supervisor_canonical_ledger(
        frame=live_frame,
        timelines=source,
        usable_capacity_kwh=CAPACITY,
        freshness_now=live_now,
    )
    audit = audit_canonical_execution_ledger(ledger)
    check(audit.valid, f"headroom-saturated ledger failed: {audit.failures}")
    check(abs(ledger.final_soc_percent - 100.0) < 1e-8, "SOC was not capped at 100%")
    check(
        all(slot.selected_policy is LedgerPolicy.NONE for slot in ledger.slots),
        "headroom saturation invented a policy selection",
    )
    check(
        all(slot.selected_action is LedgerAction.NONE for slot in ledger.slots),
        "headroom saturation invented an EMS action",
    )
    check(
        ledger.slots[-1].planned.grid_kwh >= -1e-9,
        "confirmed zero export acquired a synthetic export",
    )
    check(
        ledger.slots[-1].planned.pv_kwh < 0.105,
        "surplus PV was not curtailed at the physical headroom boundary",
    )
    check(abs(audit.max_energy_balance_residual_kwh) <= 1e-9, "battery balance drifted")
    check(abs(audit.max_system_balance_residual_kwh) <= 1e-9, "system balance drifted")

    unverified_export_frame = frame(ExportState.UNVERIFIED)
    unverified_export_frame.now = live_now
    unverified_export_frame.context = replace(
        unverified_export_frame.context,
        observed_at=live_now,
        battery_soc_percent=99.0,
    )
    unverified_export_frame.execution = replace(
        unverified_export_frame.execution,
        battery_soc_percent=99.0,
        battery_soc_observed_at=live_now - timedelta(seconds=1),
    )
    try:
        build_supervisor_canonical_ledger(
            frame=unverified_export_frame,
            timelines=source,
            usable_capacity_kwh=CAPACITY,
            freshness_now=live_now,
        )
    except CanonicalRuntimeError as err:
        check(
            err.code == "canonical_export_disposition_unverified",
            "canonical adapter guessed how to dispose of surplus PV",
        )
    else:
        raise AssertionError("unverified export state acquired a projected disposition")

    lower = timelines(tariff_selected=False)
    for payload in lower.values():
        payload["generated_at"] = generated_at
        payload["current_actual"] = {
            **payload["current_actual"],
            "observed_at": generated_at,
            "soc_percent": 21.0,
        }
    lower_first, lower_second = lower["tariff"]["points"]
    lower_first.update(
        {
            "pv_kw": 0.42,
            "load_kw": 0.0,
            "battery_kw": 0.4,
            "grid_kw": 0.0,
            "grid_import_kw": 0.0,
            "grid_export_kw": 0.0,
            "soc_percent": 22.0,
            "baseline_soc_percent": 22.0,
        }
    )
    lower_second.update(
        {
            "pv_kw": 0.0,
            "load_kw": 0.38,
            "battery_kw": -0.4,
            "grid_kw": 0.0,
            "grid_import_kw": 0.0,
            "grid_export_kw": 0.0,
            "soc_percent": 18.0,
            "baseline_soc_percent": 18.0,
        }
    )
    low_frame = frame()
    low_frame.now = live_now
    low_frame.context = replace(
        low_frame.context,
        observed_at=live_now,
        battery_soc_percent=21.0,
    )
    low_frame.execution = replace(
        low_frame.execution,
        battery_soc_percent=21.0,
        battery_soc_observed_at=live_now - timedelta(seconds=1),
    )
    low_ledger = build_supervisor_canonical_ledger(
        frame=low_frame,
        timelines=lower,
        usable_capacity_kwh=CAPACITY,
        freshness_now=live_now,
    )
    low_audit = audit_canonical_execution_ledger(low_ledger)
    check(low_audit.valid, f"floor-saturated ledger failed: {low_audit.failures}")
    protected_floor = max(
        slot.protected_reserve_percent for slot in low_ledger.slots
    )
    check(
        abs(low_ledger.final_soc_percent - protected_floor) < 1e-8,
        "SOC was not capped at the highest protected floor",
    )
    check(
        low_ledger.slots[-1].planned.grid_kwh > 0.0,
        "missing battery discharge was not replaced by grid import",
    )
    check(abs(low_audit.max_energy_balance_residual_kwh) <= 1e-9, "floor battery balance drifted")
    check(abs(low_audit.max_system_balance_residual_kwh) <= 1e-9, "floor system balance drifted")

    for physical_mode, mode_fresh in (
        (PhysicalMode.OFF_GRID, True),
        (PhysicalMode.UNKNOWN, False),
    ):
        blocked_grid_frame = frame()
        blocked_grid_frame.now = live_now
        blocked_grid_frame.context = replace(
            blocked_grid_frame.context,
            observed_at=live_now,
            physical_mode=physical_mode,
            physical_mode_fresh=mode_fresh,
            battery_soc_percent=21.0,
        )
        blocked_grid_frame.execution = replace(
            blocked_grid_frame.execution,
            battery_soc_percent=21.0,
            battery_soc_observed_at=live_now - timedelta(seconds=1),
        )
        try:
            build_supervisor_canonical_ledger(
                frame=blocked_grid_frame,
                timelines=lower,
                usable_capacity_kwh=CAPACITY,
                freshness_now=live_now,
            )
        except CanonicalRuntimeError as err:
            check(
                err.code == "canonical_grid_import_unavailable",
                f"wrong {physical_mode.value} grid-import blocker",
            )
        else:
            raise AssertionError(
                f"SOC floor invented grid import in {physical_mode.value} mode"
            )


def test_physical_soc_limit_reroutes_energy_without_inventing_paths() -> None:
    """Saturation must re-route physical energy instead of scaling paths."""

    def assert_balanced(plan: PlannedEnergy, paths: BatteryEnergyFlows) -> None:
        battery_net = (
            paths.pv_to_battery_kwh
            + paths.grid_to_battery_kwh
            - paths.battery_to_load_kwh
            - paths.battery_to_grid_kwh
            - paths.losses_kwh
        )
        system_net = (
            plan.pv_kwh
            + plan.grid_kwh
            - plan.load_kwh
            - plan.battery_kwh
            - paths.losses_kwh
        )
        check(abs(plan.battery_kwh - battery_net) <= 1e-9, "battery paths lost balance")
        check(abs(system_net) <= 1e-9, "system paths lost balance")
        check(paths.pv_to_battery_kwh <= plan.pv_kwh + 1e-9, "PV path exceeds PV")
        check(
            paths.grid_to_battery_kwh <= max(plan.grid_kwh, 0.0) + 1e-9,
            "grid charge path exceeds import",
        )
        check(paths.battery_to_load_kwh <= plan.load_kwh + 1e-9, "load path exceeds load")
        check(
            paths.battery_to_grid_kwh <= max(-plan.grid_kwh, 0.0) + 1e-9,
            "battery export path exceeds export",
        )

    mixed_charge = PlannedEnergy(
        pv_kwh=0.5,
        load_kwh=0.0,
        battery_kwh=0.9,
        grid_kwh=0.5,
    )
    mixed_charge_paths = BatteryEnergyFlows(
        pv_to_battery_kwh=0.5,
        grid_to_battery_kwh=0.5,
        battery_to_load_kwh=0.0,
        battery_to_grid_kwh=0.0,
        losses_kwh=0.1,
    )
    charge_plan, charge_paths, charge_cursor = _limit_segment_to_physical_soc(
        mixed_charge,
        mixed_charge_paths,
        stored_energy_kwh=0.55,
        capacity_kwh=1.0,
        export_state=ExportState.VERIFIED_ALLOWED,
        grid_import_verified=True,
    )
    assert_balanced(charge_plan, charge_paths)
    check(abs(charge_cursor - 1.0) <= 1e-9, "charge cursor did not reach its ceiling")
    check(abs(charge_plan.pv_kwh - 0.5) <= 1e-9, "available PV was invented away")
    check(abs(charge_plan.grid_kwh) <= 1e-9, "grid import survived despite enough PV")
    check(abs(charge_paths.pv_to_battery_kwh - 0.5) <= 1e-9, "PV lost charge priority")
    check(abs(charge_paths.grid_to_battery_kwh) <= 1e-9, "grid charge was not removed first")

    mixed_discharge = PlannedEnergy(
        pv_kwh=0.0,
        load_kwh=0.8,
        battery_kwh=-1.1,
        grid_kwh=-0.2,
    )
    mixed_discharge_paths = BatteryEnergyFlows(
        pv_to_battery_kwh=0.0,
        grid_to_battery_kwh=0.0,
        battery_to_load_kwh=0.8,
        battery_to_grid_kwh=0.2,
        losses_kwh=0.1,
    )
    discharge_plan, discharge_paths, discharge_cursor = _limit_segment_to_physical_soc(
        mixed_discharge,
        mixed_discharge_paths,
        stored_energy_kwh=0.55,
        capacity_kwh=1.0,
        export_state=ExportState.VERIFIED_ALLOWED,
        grid_import_verified=True,
    )
    assert_balanced(discharge_plan, discharge_paths)
    check(abs(discharge_cursor) <= 1e-9, "discharge cursor did not reach its floor")
    check(abs(discharge_paths.battery_to_load_kwh - 0.5) <= 1e-9, "load lost discharge priority")
    check(abs(discharge_paths.battery_to_grid_kwh) <= 1e-9, "battery exported while grid imported")
    check(abs(discharge_plan.grid_kwh - 0.3) <= 1e-9, "load deficit was not imported")

    try:
        _limit_segment_to_physical_soc(
            mixed_discharge,
            mixed_discharge_paths,
            stored_energy_kwh=0.55,
            capacity_kwh=1.0,
            export_state=ExportState.VERIFIED_ALLOWED,
            grid_import_verified=False,
        )
    except CanonicalRuntimeError as err:
        check(err.code == "canonical_grid_import_unavailable", "wrong off-grid blocker")
    else:
        raise AssertionError("SOC saturation invented grid import in off-grid/unknown mode")

    pv_surplus = PlannedEnergy(
        pv_kwh=1.0,
        load_kwh=0.0,
        battery_kwh=0.9,
        grid_kwh=0.0,
    )
    pv_surplus_paths = BatteryEnergyFlows(
        pv_to_battery_kwh=1.0,
        grid_to_battery_kwh=0.0,
        battery_to_load_kwh=0.0,
        battery_to_grid_kwh=0.0,
        losses_kwh=0.1,
    )
    zero_plan, zero_paths, _ = _limit_segment_to_physical_soc(
        pv_surplus,
        pv_surplus_paths,
        stored_energy_kwh=0.55,
        capacity_kwh=1.0,
        export_state=ExportState.CONFIRMED_ZERO_EXPORT,
        grid_import_verified=True,
    )
    assert_balanced(zero_plan, zero_paths)
    check(abs(zero_plan.pv_kwh - 0.5) <= 1e-9, "zero export did not curtail residual PV")
    check(abs(zero_plan.grid_kwh) <= 1e-9, "zero export produced grid energy")

    discharge_with_pv_surplus = PlannedEnergy(
        pv_kwh=1.0,
        load_kwh=0.5,
        battery_kwh=-0.4,
        grid_kwh=-0.86,
    )
    discharge_with_pv_paths = BatteryEnergyFlows(
        pv_to_battery_kwh=0.0,
        grid_to_battery_kwh=0.0,
        battery_to_load_kwh=0.0,
        battery_to_grid_kwh=0.36,
        losses_kwh=0.04,
    )
    discharge_zero_plan, discharge_zero_paths, _ = _limit_segment_to_physical_soc(
        discharge_with_pv_surplus,
        discharge_with_pv_paths,
        stored_energy_kwh=0.2,
        capacity_kwh=1.0,
        export_state=ExportState.CONFIRMED_ZERO_EXPORT,
        grid_import_verified=True,
    )
    assert_balanced(discharge_zero_plan, discharge_zero_paths)
    check(
        abs(discharge_zero_plan.pv_kwh - 0.32) <= 1e-9,
        "zero export did not curtail the discharge-side PV surplus",
    )
    check(
        abs(discharge_zero_paths.battery_to_load_kwh - 0.18) <= 1e-9,
        "curtailment did not route bounded discharge to load first",
    )
    check(
        abs(discharge_zero_paths.battery_to_grid_kwh) <= 1e-9,
        "zero export retained a battery-to-grid path",
    )
    check(
        abs(discharge_zero_plan.grid_kwh) <= 1e-9,
        "discharge-side zero export produced grid energy",
    )

    unavoidable_battery_export = replace(
        discharge_with_pv_surplus,
        load_kwh=0.1,
        grid_kwh=-1.26,
    )
    unavoidable_paths = replace(
        discharge_with_pv_paths,
        battery_to_grid_kwh=0.36,
    )
    try:
        _limit_segment_to_physical_soc(
            unavoidable_battery_export,
            unavoidable_paths,
            stored_energy_kwh=0.2,
            capacity_kwh=1.0,
            export_state=ExportState.PROHIBITED,
            grid_import_verified=True,
        )
    except CanonicalRuntimeError as err:
        check(
            err.code == "canonical_export_prohibited",
            "wrong unavoidable battery-export blocker",
        )
    else:
        raise AssertionError("prohibited export retained unavoidable battery export")

    try:
        _limit_segment_to_physical_soc(
            pv_surplus,
            pv_surplus_paths,
            stored_energy_kwh=0.55,
            capacity_kwh=1.0,
            export_state=ExportState.UNVERIFIED,
            grid_import_verified=True,
        )
    except CanonicalRuntimeError as err:
        check(
            err.code == "canonical_export_disposition_unverified",
            "wrong unverified-export blocker",
        )
    else:
        raise AssertionError("unverified export was silently treated as export or curtailment")


def test_negative_loss_and_stale_sources_fail_closed() -> None:
    broken = timelines()
    broken["tariff"]["points"][0]["grid_kw"] = 0.0
    broken["tariff"]["points"][0]["grid_import_kw"] = 0.0
    try:
        build_supervisor_canonical_ledger(
            frame=frame(), timelines=broken, usable_capacity_kwh=CAPACITY
        )
    except CanonicalRuntimeError as err:
        check(err.code == "canonical_negative_losses", "wrong balance blocker")
    else:
        raise AssertionError("negative conversion loss was accepted")

    within_rcm_cadence = timelines()
    within_rcm_cadence["rcm"]["generated_at"] = (
        NOW - timedelta(seconds=61)
    ).isoformat().replace("+00:00", "Z")
    build_supervisor_canonical_ledger(
        frame=frame(), timelines=within_rcm_cadence, usable_capacity_kwh=CAPACITY
    )
    check(True, "RCM timeline was rejected inside its ten-minute cadence")

    stale = timelines()
    stale["rcm"]["generated_at"] = (
        NOW - timedelta(seconds=631)
    ).isoformat().replace("+00:00", "Z")
    try:
        build_supervisor_canonical_ledger(
            frame=frame(), timelines=stale, usable_capacity_kwh=CAPACITY
        )
    except CanonicalRuntimeError as err:
        check(err.code == "rcm_timeline_stale", "wrong freshness blocker")
    else:
        raise AssertionError("stale RCM timeline was accepted")

    wall_clock_stale = timelines()
    wall_clock_frame = frame()
    wall_clock_frame.execution = replace(
        wall_clock_frame.execution,
        battery_soc_observed_at=NOW + timedelta(seconds=150),
    )
    try:
        build_supervisor_canonical_ledger(
            frame=wall_clock_frame,
            timelines=wall_clock_stale,
            usable_capacity_kwh=CAPACITY,
            freshness_now=NOW + timedelta(seconds=151),
        )
    except CanonicalRuntimeError as err:
        check(
            err.code == "rce_timeline_stale",
            "wall-clock freshness did not override the older planning frame",
        )
    else:
        raise AssertionError("wall-clock stale timelines were accepted")


def test_canonical_entity_publication_is_thread_safe() -> None:
    source = (COMPONENT / "supervisor_canonical_sensor.py").read_text(
        encoding="utf-8"
    )
    check(
        "self.schedule_update_ha_state()" in source,
        "canonical publication must be queued through Home Assistant",
    )
    check(
        "self.async_write_ha_state()" not in source,
        "canonical publication must not write directly from worker callbacks",
    )


def main() -> None:
    test_required_tariff_wins_and_chain_is_canonical()
    test_selected_rce_export_lowers_soc_through_battery_to_grid()
    test_future_rce_uses_slot_command_while_current_supervisor_stays_idle()
    test_selected_rcm_absorb_pv_raises_soc_through_pv_to_battery()
    test_confirmed_zero_export_rejects_rce()
    test_mixed_tariff_provenance_preserves_required_priority()
    test_partial_tariff_horizon_keeps_only_complete_slots_actionable()
    test_union_horizon_preserves_actions_beyond_shorter_policy_coverage()
    test_tomorrow_rcm_horizon_does_not_hide_current_sale()
    test_rejected_tariff_action_cannot_become_the_idle_energy_backbone()
    test_partial_current_interval_respects_physical_soc_headroom()
    test_physical_soc_limit_reroutes_energy_without_inventing_paths()
    test_negative_loss_and_stale_sources_fail_closed()
    test_canonical_entity_publication_is_thread_safe()
    print(f"Supervisor canonical runtime: PASS ({CHECKS} checks)")


if __name__ == "__main__":
    main()
