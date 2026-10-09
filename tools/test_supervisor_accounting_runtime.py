#!/usr/bin/env python3
"""Deterministic Active-to-accounting-v2 runtime contract."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))

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
    SupervisorState,
    SupervisorMode,
    SupervisorProfile,
    arbitrate_supervisor,
)
from supervisor_accounting_runtime import build_active_accounting_entry  # noqa: E402
from supervisor_accounting_v2 import (  # noqa: E402
    accumulate_execution_entry,
    new_accounting_state,
)
from supervisor_active_controller import ActiveFrame, SupervisorActiveController  # noqa: E402
from supervisor_executor import (  # noqa: E402
    ActiveState,
    AtomicWriteFamily,
    EmsMode,
    ExecutionOwner,
    VerificationStatus,
)
from supervisor_runtime import (  # noqa: E402
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


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
FINGERPRINT = "b" * 64
SOURCES = {
    "ems_mode_readback": "sensor.hoymiles_hit_ems_mode_readback_code",
    "ems_generation": "sensor.hoymiles_hit_ems_control_readback_generation",
    "grid_to_battery_power": "sensor.hoymiles_hit_grid_to_battery_power",
    "grid_power": "sensor.hoymiles_hit_overview_grid_total_active_power",
}
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def source(at: datetime, **overrides: object) -> ExecutionSourceSnapshot:
    values: dict[str, object] = {
        "physical_mode_code": 0,
        "full_block_generation": 10,
        "full_block_generation_at": at - timedelta(seconds=1),
        "self_use_soc_percent": 20,
        "backup_soc_percent": 80,
        "force_charge_soc_percent": 90,
        "maximum_charge_power_percent": 50,
        "force_discharge_soc_percent": 25,
        "maximum_discharge_power_percent": 40,
        "full_block_execution_ready": True,
        "direct_306_execution_ready": True,
        "direct_259_execution_ready": True,
        "machine_type_code": 0,
        "inverter_count": 1,
        "topology_generation_at": at - timedelta(seconds=1),
        "battery_soc_percent": 60,
        "battery_soc_observed_at": at - timedelta(seconds=1),
        "bms_voltage_v": 51.2,
        "bms_voltage_observed_at": at - timedelta(seconds=1),
        "bms_max_charge_current_a": 100,
        "bms_charge_current_observed_at": at - timedelta(seconds=1),
        "bms_max_discharge_current_a": 100,
        "bms_discharge_current_observed_at": at - timedelta(seconds=1),
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
        "effective_export_limit_percent": 100,
        "gcf_generation": 11,
        "gcf_generation_at": at - timedelta(seconds=1),
        "gcf_cohort_coherent": True,
        "battery_charge_limit_generation": 12,
        "battery_charge_limit_generation_at": at - timedelta(seconds=1),
        "battery_charge_limit_percent": 80,
        "battery_power_w": None,
        "battery_power_observed_at": None,
        "pv_power_w": None,
        "pv_power_observed_at": None,
        "load_power_w": None,
        "load_power_observed_at": None,
        "grid_to_battery_power_w": None,
        "grid_to_battery_power_observed_at": None,
        "grid_power_w": None,
        "grid_power_observed_at": None,
        "hardware_readback_supported": True,
    }
    values.update(overrides)
    return ExecutionSourceSnapshot(**values)


def candidate(policy: PolicyId, at: datetime, *, active: bool) -> PolicyCandidate:
    if policy is PolicyId.TARIFF:
        action = RequestedAction.TARIFF_BATTERY_CHARGE
        scope = ActuatorScope.EMS_BLOCK_4300_4306
        priority = PriorityClass.REQUIRED_ENERGY
        need = NeedClass.MANDATORY
        reason = ReasonCode.REQUIRED_ENERGY_RESTORE
    else:
        action = RequestedAction.NONE
        scope = ActuatorScope.NONE
        priority = PriorityClass.NONE
        need = NeedClass.NONE
        reason = ReasonCode.NO_ACTION
    return PolicyCandidate(
        schema_version=1,
        policy_id=policy,
        observed_at=at - timedelta(seconds=1),
        allowed_by_user=True,
        enabled=active,
        available=True,
        result_current=True,
        recalculation_pending=False,
        input_revision=1,
        candidate_revision=12 if active else 0,
        start_eligible=active,
        continuation_eligible=active,
        active_latched=False,
        local_hard_stop=False,
        requested_action=action,
        actuator_scope=scope,
        priority_class=priority,
        need_class=need,
        reason_code=reason,
        blocked_reason=None,
        valid_from=None,
        valid_until=NOW + timedelta(minutes=20) if active else None,
        desired_actuator_fingerprint=FINGERPRINT if active else None,
        economic_value_status=EconomicValueStatus.UNAVAILABLE,
        requested_mode=PhysicalMode.GRID_CHARGE if active else None,
        requested_power_kw=3.0 if active else None,
        target_soc_percent=85.0 if active else None,
    )


def frame(at: datetime, execution: ExecutionSourceSnapshot) -> ActiveFrame:
    candidates = tuple(
        candidate(policy, at, active=policy is PolicyId.TARIFF)
        for policy in PolicyId
    )
    context = ExecutionContext(
        observed_at=at,
        physical_mode={0: PhysicalMode.SELF_USE, 4: PhysicalMode.GRID_CHARGE}[
            int(execution.physical_mode_code)
        ],
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
        export_state=ExportState.VERIFIED_ALLOWED,
        battery_soc_percent=float(execution.battery_soc_percent),
        physical_protected_soc_floor_percent=float(
            execution.self_use_soc_percent
        ),
    )
    decision = arbitrate_supervisor(
        mode=SupervisorMode.ACTIVE,
        profile=SupervisorProfile.BALANCED,
        context=context,
        candidates=candidates,
        now=at,
    )
    return ActiveFrame(
        now=at,
        decision=decision,
        candidates=candidates,
        context=context,
        rce=RceSourceSnapshot(),
        tariff=TariffSourceSnapshot(
            observed_at=at - timedelta(seconds=1),
            allowed_by_user=True,
            enabled=True,
            result_current=True,
            recalculation_pending=False,
            current_slot_planned=True,
            current_run_continue_eligible=True,
            system_power_kw=10.0,
            target_soc_percent=85,
            maximum_soc_percent=100,
            current_grid_charge_run_end=NOW + timedelta(minutes=20),
            control_data_ready=True,
            command_charge_power_percent=50,
        ),
        rcm=RcmSourceSnapshot(),
        execution=execution,
    )


POLICY_RUN_END = NOW + timedelta(minutes=30)


def policy_local_sources(
    at: datetime,
    *,
    tariff_active: bool,
) -> tuple[RceSourceSnapshot, TariffSourceSnapshot, RcmSourceSnapshot]:
    """Return the exact low-SOC policy-local regression inputs."""

    rce = RceSourceSnapshot(
        observed_at=at - timedelta(seconds=1),
        allowed_by_user=True,
        enabled=True,
        active_latched=False,
        status_code=RcePlanStatus.READY,
        result_current=True,
        recalculation_pending=False,
        input_revision=41,
        current_slot_planned=True,
        current_slot_start_eligible=True,
        current_slot_continue_eligible=True,
        current_slot_end=POLICY_RUN_END,
        current_run_end=POLICY_RUN_END,
        requested_discharge_power_kw=3.0,
        planned_export_energy_kwh=1.0,
        protected_soc_floor_percent=25.0,
        effective_discharge_power_percent=40.0,
        current_soc_percent=24.0,
        control_data_ready=True,
        price_above_threshold=True,
        reserve_ready=True,
        sale_block_active=False,
    )
    tariff = TariffSourceSnapshot(
        observed_at=at - timedelta(seconds=1),
        allowed_by_user=True,
        enabled=True,
        active_latched=tariff_active,
        status_code=TariffPlanStatus.READY,
        result_current=True,
        recalculation_pending=False,
        input_revision=42,
        current_slot_planned=True,
        current_action=TariffAction.BATTERY_CHARGE,
        current_run_need_class=TariffRunNeed.REQUIRED_ENERGY,
        current_run_start_eligible=True,
        current_run_continue_eligible=True,
        requested_charge_power_kw=3.0,
        system_power_kw=10.0,
        command_charge_power_percent=50.0,
        current_run_grid_import_kwh=2.0,
        current_run_benefit_pln=0.0,
        target_soc_percent=85.0,
        maximum_soc_percent=100.0,
        base_reserve_soc_percent=25.0,
        current_slot_end=POLICY_RUN_END,
        current_grid_charge_run_end=POLICY_RUN_END,
        active_action=TariffAction.BATTERY_CHARGE,
        latched_slot_end=POLICY_RUN_END,
        latched_target_soc_percent=85.0,
        control_data_ready=True,
        planned_slot_ready=True,
        active_4303_readback_percent=85.0,
        active_4304_readback_percent=50.0,
    )
    # The unavailable RCEm timeline carries the same 25% operating floor.  Its
    # local failure must not become tariff authority or a global hard stop.
    rcm = RcmSourceSnapshot(
        observed_at=None,
        allowed_by_user=True,
        enabled=True,
        result_current=False,
        recalculation_pending=False,
        input_revision=43,
        action=RcmAction.GRID_DISCHARGE_PREPARATION,
        protected_minimum_soc_percent=25.0,
    )
    return rce, tariff, rcm


def policy_local_frame(
    at: datetime,
    execution: ExecutionSourceSnapshot,
    *,
    tariff_active: bool,
    transaction_pending: bool,
) -> ActiveFrame:
    """Build one runtime decision frame without any canonical input."""

    rce, tariff, rcm = policy_local_sources(at, tariff_active=tariff_active)
    candidates = (
        build_rce_candidate(rce, now=at),
        build_tariff_candidate(tariff, now=at),
        build_rcm_candidate(rcm, now=at),
    )
    context = build_execution_context(execution, now=at)
    context = replace(
        context,
        owner_conflict=False,
        transaction_pending=transaction_pending,
        transaction_owner_kind=(
            OwnerKind.TARIFF if transaction_pending else OwnerKind.NONE
        ),
    )
    decision = arbitrate_supervisor(
        mode=SupervisorMode.ACTIVE,
        profile=SupervisorProfile.BALANCED,
        context=context,
        candidates=candidates,
        now=at,
    )
    return ActiveFrame(
        now=at,
        decision=decision,
        candidates=candidates,
        context=context,
        rce=rce,
        tariff=tariff,
        rcm=rcm,
        execution=execution,
    )


async def test_policy_local_tariff_end_to_end() -> None:
    """Prove low-SOC tariff selection, dispatch, proof and accounting."""

    persisted: list[tuple[ActiveState, ExecutionOwner]] = []
    writes: list[object] = []
    command_completed_at = NOW + timedelta(seconds=2)

    async def persist(record) -> None:
        persisted.append((record.state, record.owner))

    async def dispatch(write) -> None:
        writes.append(write)

    initial_source = source(
        NOW,
        battery_soc_percent=24.0,
        self_use_soc_percent=25.0,
        gcf_enable_code=1,
        effective_export_limit_percent=0.0,
        direct_259_execution_ready=False,
        direct_306_execution_ready=False,
        battery_charge_limit_generation=None,
        battery_charge_limit_generation_at=None,
        battery_charge_limit_percent=None,
    )
    initial = policy_local_frame(
        NOW,
        initial_source,
        tariff_active=False,
        transaction_pending=False,
    )
    rejected = {
        item.policy_id: item.reason for item in initial.decision.rejected_reasons
    }
    check(
        initial.decision.supervisor_mode is SupervisorMode.ACTIVE
        and initial.decision.state is SupervisorState.ACTIVE_SELECTED,
        "policy-local rejection turned Supervisor Active into a global hold",
    )
    check(
        initial.context.owner_kind is OwnerKind.NONE,
        "idle policy selection acquired an execution owner",
    )
    check(
        initial.context.charge_direction_ready
        and not initial.context.discharge_direction_ready,
        "SOC 24/floor 25 did not split charge and discharge direction gates",
    )
    check(
        initial.decision.selected_policy is PolicyId.TARIFF,
        "current eligible tariff charge was not selected",
    )
    check(
        rejected.get(PolicyId.RCE) is ReasonCode.CONFIRMED_ZERO_EXPORT,
        "RCE did not retain its confirmed_zero_export rejection",
    )
    check(
        rejected.get(PolicyId.RCM) is ReasonCode.UNAVAILABLE,
        "unavailable RCEm did not remain an individual rejection",
    )
    check(
        initial.rcm.protected_minimum_soc_percent == 25.0
        and initial.execution.battery_soc_percent == 24.0,
        "RCEm unavailable/out-of-bounds fixture drifted",
    )
    check(
        not hasattr(initial, "canonical"),
        "controller frame unexpectedly gained canonical execution authority",
    )

    controller = SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: command_completed_at,
    )
    await controller.async_initialize()
    await controller.async_reconcile(initial)
    check(
        controller.record.state is ActiveState.WAITING_READBACK,
        "tariff start did not reach the physical readback boundary",
    )
    first_owned = next(
        index
        for index, (_state, owner) in enumerate(persisted)
        if owner is not ExecutionOwner.NONE
    )
    check(
        all(owner is ExecutionOwner.NONE for _state, owner in persisted[:first_owned]),
        "owner was acquired before transaction start",
    )
    check(
        persisted[first_owned]
        == (ActiveState.STARTING, ExecutionOwner.TARIFF),
        "tariff owner was not acquired exactly at STARTING",
    )
    check(len(writes) == 1, "tariff start dispatched more than one atomic write")
    write = writes[0]
    check(
        getattr(write, "family", None) is AtomicWriteFamily.EMS_COMPLETE_BLOCK,
        "tariff charge did not use the complete EMS 4300-4306 action",
    )
    block = getattr(write, "ems_block", None)
    check(
        block is not None
        and (
            block.mode,
            block.self_use_soc_percent_4301,
            block.backup_soc_percent_4302,
            block.force_charge_soc_percent_4303,
            block.maximum_charge_power_percent_4304,
            block.force_discharge_soc_percent_4305,
            block.maximum_discharge_power_percent_4306,
        )
        == (EmsMode.GRID_CHARGE, 25.0, 80.0, 85.0, 50.0, 25.0, 40.0),
        "tariff command was not the exact atomic Grid Charge 4300-4306 block",
    )
    transaction = controller.record.transaction
    check(transaction is not None, "tariff transaction disappeared")
    check(
        transaction.command_sent_at == command_completed_at,
        "command boundary was not captured after physical dispatch completion",
    )
    check(
        transaction.expected_readback is not None
        and transaction.expected_readback.written_families
        == frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK}),
        "tariff EMS-only transaction acquired GCF/306 readback authority",
    )

    readback_at = NOW + timedelta(seconds=5)
    fc03_at = NOW + timedelta(seconds=4)
    readback_source = source(
        readback_at,
        physical_mode_code=4,
        full_block_generation=11,
        full_block_generation_at=fc03_at,
        self_use_soc_percent=25.0,
        force_charge_soc_percent=85.0,
        maximum_charge_power_percent=50.0,
        battery_soc_percent=24.0,
        tariff_active=True,
        gcf_enable_code=1,
        effective_export_limit_percent=0.0,
        gcf_generation=11,
        gcf_generation_at=NOW - timedelta(seconds=1),
        direct_259_execution_ready=False,
        direct_306_execution_ready=False,
        battery_charge_limit_generation=None,
        battery_charge_limit_generation_at=None,
        battery_charge_limit_percent=None,
        grid_to_battery_power_w=None,
        grid_to_battery_power_observed_at=None,
        grid_power_w=-3000,
        grid_power_observed_at=fc03_at,
        battery_power_w=-5000,
        battery_power_observed_at=fc03_at,
        pv_power_w=3000,
        pv_power_observed_at=fc03_at,
        load_power_w=1000,
        load_power_observed_at=fc03_at,
    )
    waiting_frame = policy_local_frame(
        readback_at,
        readback_source,
        tariff_active=False,
        transaction_pending=True,
    )
    # STARTING ownership is an executor commitment even before the optimizer
    # exposes its active latch; this mirrors the sensor commitment overlay.
    check(
        waiting_frame.context.owner_kind is OwnerKind.TARIFF
        and waiting_frame.context.transaction_pending,
        "pending sensor frame did not expose the committed tariff owner",
    )
    await controller.async_reconcile(waiting_frame)
    check(
        fc03_at > command_completed_at,
        "FC03 readback fixture did not cross the actual dispatch boundary",
    )
    check(
        controller.record.state is ActiveState.EXECUTING
        and controller.record.owner is ExecutionOwner.TARIFF,
        "new FC03 plus physical import did not commit tariff EXECUTING: "
        f"{controller.record.state.value}/{controller.record.reason.value}/"
        f"{controller.record.owner.value}",
    )
    transaction = controller.record.transaction
    check(
        transaction is not None
        and transaction.readback_result is VerificationStatus.CONFIRMED
        and transaction.physical_verification is not None
        and transaction.physical_verification.status
        is VerificationStatus.CONFIRMED,
        "tariff execution lacks readback or physical grid-to-battery proof",
    )
    inferred_entry = build_active_accounting_entry(
        controller.record,
        waiting_frame,
        SOURCES,
    )
    check(
        not inferred_entry.qualified
        and inferred_entry.attributed_power_w == 0.0,
        "inferred execution proof leaked into grid-to-battery accounting",
    )
    inferred_update = accumulate_execution_entry(
        new_accounting_state(),
        inferred_entry,
    )
    check(
        not inferred_update.accepted
        and inferred_update.state.accumulated_energy_kwh == 0.0
        and inferred_update.state.delivered_power_feedback_w == 0.0,
        "inferred execution proof produced energy or delivered-power feedback",
    )

    continuation_at = NOW + timedelta(seconds=6)
    physical_at = NOW + timedelta(seconds=5, milliseconds=500)
    continuation_source = replace(
        readback_source,
        full_block_generation_at=fc03_at,
        battery_soc_observed_at=physical_at,
        bms_voltage_observed_at=physical_at,
        bms_charge_current_observed_at=physical_at,
        bms_discharge_current_observed_at=physical_at,
        grid_to_battery_power_w=1500,
        grid_to_battery_power_observed_at=physical_at,
        grid_power_w=-3000,
        grid_power_observed_at=physical_at,
        battery_power_w=-5000,
        battery_power_observed_at=physical_at,
        pv_power_w=3000,
        pv_power_observed_at=physical_at,
        load_power_w=1000,
        load_power_observed_at=physical_at,
    )
    continuation = policy_local_frame(
        continuation_at,
        continuation_source,
        tariff_active=True,
        transaction_pending=False,
    )
    continuation_rejected = {
        item.policy_id: item.reason
        for item in continuation.decision.rejected_reasons
    }
    check(
        continuation.decision.selected_policy is PolicyId.TARIFF
        and continuation_rejected.get(PolicyId.RCE)
        is ReasonCode.CONFIRMED_ZERO_EXPORT
        and continuation_rejected.get(PolicyId.RCM) is ReasonCode.UNAVAILABLE,
        "policy-local rejections changed after tariff ownership was committed",
    )
    await controller.async_reconcile(continuation)
    check(
        controller.record.state is ActiveState.EXECUTING
        and controller.record.owner is ExecutionOwner.TARIFF,
        "fresh policy-local continuation lost tariff execution",
    )

    entry = build_active_accounting_entry(
        controller.record,
        continuation,
        SOURCES,
    )
    selected_tariff = next(
        candidate
        for candidate in continuation.candidates
        if candidate.policy_id is PolicyId.TARIFF
    )
    check(
        not entry.qualified,
        "providerless runtime promoted a global Grid-to-Battery entity",
    )
    check(
        entry.attributed_power_w == 0.0
        and entry.reason_code.value == "grid_to_battery_missing",
        "providerless runtime did not remain explicit zero/unverified",
    )
    check(
        entry.intent_fingerprint == selected_tariff.desired_actuator_fingerprint,
        "accounting lost the exact selected tariff intent",
    )
    missing_channel = policy_local_frame(
        continuation_at + timedelta(seconds=1),
        replace(
            continuation_source,
            grid_to_battery_power_w=None,
            grid_to_battery_power_observed_at=None,
        ),
        tariff_active=True,
        transaction_pending=False,
    )
    missing_entry = build_active_accounting_entry(
        controller.record,
        missing_channel,
        SOURCES,
    )
    check(
        not missing_entry.qualified and missing_entry.attributed_power_w == 0.0,
        "battery charge power became a grid-to-battery accounting fallback",
    )


async def main_async() -> None:
    controller = SupervisorActiveController(
        persist=lambda _record: _done(),
        dispatch=lambda _write: _done(),
        publish=lambda _record: None,
        clock=lambda: NOW,
    )
    await controller.async_initialize()
    await controller.async_reconcile(frame(NOW, source(NOW)))
    check(
        controller.record.state is ActiveState.WAITING_READBACK,
        "tariff start must wait for complete readback",
    )

    observed = NOW + timedelta(seconds=5)
    executing_frame = frame(
        observed,
        source(
            observed,
            physical_mode_code=4,
            full_block_generation=11,
            full_block_generation_at=observed - timedelta(seconds=1),
            force_charge_soc_percent=85,
            maximum_charge_power_percent=50,
            grid_to_battery_power_w=1500,
            grid_to_battery_power_observed_at=observed - timedelta(seconds=1),
            grid_power_w=-3000,
            grid_power_observed_at=observed - timedelta(seconds=1),
            battery_power_w=-2000,
            battery_power_observed_at=observed - timedelta(seconds=1),
            pv_power_w=500,
            pv_power_observed_at=observed - timedelta(seconds=1),
            load_power_w=1500,
            load_power_observed_at=observed - timedelta(seconds=1),
        ),
    )
    await controller.async_reconcile(executing_frame)
    check(controller.record.state is ActiveState.EXECUTING, "tariff must execute")
    entry = build_active_accounting_entry(
        controller.record,
        executing_frame,
        SOURCES,
    )
    check(
        not entry.qualified,
        "foreign global Grid-to-Battery entity acquired accounting authority",
    )
    check(
        entry.attributed_power_w == 0.0
        and entry.reason_code.value == "grid_to_battery_missing",
        "provider absence did not remain zero/unverified",
    )
    check(entry.intent_fingerprint == FINGERPRINT, "full candidate SHA is required")
    check(entry.readback_confirmed, "executor readback proof was lost")

    missing_frame = frame(
        observed + timedelta(seconds=5),
        source(
            observed + timedelta(seconds=5),
            physical_mode_code=4,
            full_block_generation=12,
            grid_to_battery_power_w=None,
            grid_to_battery_power_observed_at=None,
            grid_power_w=-3000,
            grid_power_observed_at=observed + timedelta(seconds=4),
            battery_power_w=-2000,
            battery_power_observed_at=observed + timedelta(seconds=4),
        ),
    )
    missing = build_active_accounting_entry(
        controller.record,
        missing_frame,
        SOURCES,
    )
    check(not missing.qualified, "missing physical channel must fail closed")
    check(missing.attributed_power_w == 0.0, "battery power became a fallback")

    await test_policy_local_tariff_end_to_end()


async def _done() -> None:
    return None


def main() -> None:
    asyncio.run(main_async())
    print(f"Supervisor accounting runtime: PASS ({CHECKS} checks)")


if __name__ == "__main__":
    main()
