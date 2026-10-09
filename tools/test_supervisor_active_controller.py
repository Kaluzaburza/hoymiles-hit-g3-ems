#!/usr/bin/env python3
"""End-to-end pure adapter test for persist-first Active execution."""

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
    SupervisorMode,
    SupervisorProfile,
    arbitrate_supervisor,
)
import supervisor_active_controller as controller_module  # noqa: E402
from supervisor_active_controller import (  # noqa: E402
    ActiveFrame,
    SupervisorActiveController,
)
from supervisor_executor import (  # noqa: E402
    ActiveState,
    AtomicWriteNotQueued,
    EmsMode,
    ExecutionOwner,
    ExecutionReason,
    MasterStopStatus,
    RollbackStatus,
    VerificationStatus,
)
from supervisor_runtime import (  # noqa: E402
    ExecutionSourceSnapshot,
    RcePlanStatus,
    RceSourceSnapshot,
    RcmSourceSnapshot,
    TariffSourceSnapshot,
    build_rce_candidate,
)


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


class ScriptedClock:
    """Deterministic aware/invalid clock sequence for dispatch-boundary tests."""

    def __init__(self, *values: object) -> None:
        self._values = list(values)

    def __call__(self) -> datetime:
        if not self._values:
            raise AssertionError("runtime clock was read more often than expected")
        return self._values.pop(0)  # type: ignore[return-value]


def execution_source(at: datetime, **overrides: object) -> ExecutionSourceSnapshot:
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
        "gcf_enable_code": 0,
        "effective_export_limit_percent": 100,
        "gcf_generation": 11,
        "gcf_generation_at": at - timedelta(seconds=1),
        "gcf_cohort_coherent": True,
        "battery_charge_limit_generation": 12,
        "battery_charge_limit_generation_at": at - timedelta(seconds=1),
        "battery_charge_limit_percent": 80,
        "hardware_readback_supported": True,
    }
    values.update(overrides)
    return ExecutionSourceSnapshot(**values)


def rcm_candidate(at: datetime, *, enabled: bool = True) -> PolicyCandidate:
    return PolicyCandidate(
        schema_version=1,
        policy_id=PolicyId.RCM,
        observed_at=at - timedelta(seconds=1),
        allowed_by_user=True,
        enabled=enabled,
        available=enabled,
        result_current=enabled,
        recalculation_pending=False,
        input_revision=1,
        candidate_revision=13 if enabled else 0,
        start_eligible=enabled,
        continuation_eligible=enabled,
        active_latched=False,
        local_hard_stop=False,
        requested_action=(RequestedAction.RCM_ABSORB_PV if enabled else RequestedAction.NONE),
        actuator_scope=(ActuatorScope.DIRECT_306 if enabled else ActuatorScope.NONE),
        priority_class=(PriorityClass.PREVENTIVE_GRID if enabled else PriorityClass.NONE),
        need_class=(NeedClass.PREVENTIVE if enabled else NeedClass.NONE),
        reason_code=(ReasonCode.PREVENTIVE_VOLTAGE_ACTION if enabled else ReasonCode.NO_ACTION),
        blocked_reason=None,
        valid_from=None,
        valid_until=None,
        desired_actuator_fingerprint=HASH if enabled else None,
        economic_value_status=EconomicValueStatus.UNAVAILABLE,
        requested_mode=PhysicalMode.SELF_USE if enabled else None,
        requested_power_kw=2.0 if enabled else None,
    )


def empty_candidate(policy: PolicyId, at: datetime) -> PolicyCandidate:
    return PolicyCandidate(
        schema_version=1,
        policy_id=policy,
        observed_at=at - timedelta(seconds=1),
        allowed_by_user=True,
        enabled=False,
        available=True,
        result_current=True,
        recalculation_pending=False,
        input_revision=1,
        candidate_revision=0,
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


def frame(
    at: datetime,
    source: ExecutionSourceSnapshot,
    *,
    mode: SupervisorMode = SupervisorMode.ACTIVE,
    enabled: bool = True,
) -> ActiveFrame:
    candidates = (
        empty_candidate(PolicyId.RCE, at),
        empty_candidate(PolicyId.TARIFF, at),
        rcm_candidate(at, enabled=enabled),
    )
    context = ExecutionContext(
        observed_at=at,
        physical_mode={
            0: PhysicalMode.SELF_USE,
            3: PhysicalMode.OFF_GRID,
            4: PhysicalMode.GRID_CHARGE,
            5: PhysicalMode.GRID_DISCHARGE,
        }[int(source.physical_mode_code)],
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
        battery_soc_percent=float(source.battery_soc_percent),
        physical_protected_soc_floor_percent=float(source.self_use_soc_percent),
    )
    decision = arbitrate_supervisor(
        mode=mode,
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
        tariff=TariffSourceSnapshot(),
        rcm=RcmSourceSnapshot(
            recommended_charge_limit_percent=60,
            recommended_charge_power_kw=2.0,
        ),
        execution=source,
    )


def rce_frame(
    at: datetime,
    source: ExecutionSourceSnapshot,
    *,
    deadline: datetime,
    floor: float,
    power: float,
    active: bool,
    transaction_pending: bool = False,
    observed_at: datetime | None = None,
    result_current: bool = True,
    recalculation_pending: bool = False,
    control_data_ready: bool | None = True,
    reserve_ready: bool | None = True,
    sale_block_active: bool | None = False,
    slot_end: datetime | None = None,
    pending_no_action: bool = False,
    start_eligible: bool = True,
    continue_eligible: bool = True,
    export_state: ExportState = ExportState.VERIFIED_ALLOWED,
) -> ActiveFrame:
    rce = RceSourceSnapshot(
        observed_at=observed_at or at - timedelta(seconds=1),
        allowed_by_user=True,
        enabled=True,
        active_latched=active,
        status_code=RcePlanStatus.READY,
        result_current=result_current,
        recalculation_pending=recalculation_pending,
        input_revision=2,
        current_slot_planned=True,
        current_slot_start_eligible=start_eligible,
        current_slot_continue_eligible=continue_eligible,
        current_slot_end=slot_end or at + timedelta(minutes=5),
        current_run_end=deadline,
        requested_discharge_power_kw=8.0,
        planned_export_energy_kwh=4.0,
        protected_soc_floor_percent=floor,
        effective_discharge_power_percent=power,
        system_power_kw=10.0,
        current_soc_percent=float(source.battery_soc_percent),
        control_data_ready=control_data_ready,
        price_above_threshold=True,
        reserve_ready=reserve_ready,
        sale_block_active=sale_block_active,
        latched_slot_end=deadline if active else None,
        latched_minimum_soc_percent=floor if active else None,
        active_4305_readback_percent=(
            float(source.force_discharge_soc_percent) if active else None
        ),
        active_4306_readback_percent=(
            float(source.maximum_discharge_power_percent) if active else None
        ),
    )
    rce_candidate = build_rce_candidate(rce, now=at)
    if pending_no_action:
        rce_candidate = replace(
            empty_candidate(PolicyId.RCE, at),
            allowed_by_user=True,
            enabled=True,
            available=False,
            result_current=False,
            recalculation_pending=True,
            input_revision=2,
        )
    candidates = (
        rce_candidate,
        empty_candidate(PolicyId.TARIFF, at),
        empty_candidate(PolicyId.RCM, at),
    )
    context = ExecutionContext(
        observed_at=at,
        physical_mode={
            0: PhysicalMode.SELF_USE,
            3: PhysicalMode.OFF_GRID,
            4: PhysicalMode.GRID_CHARGE,
            5: PhysicalMode.GRID_DISCHARGE,
        }[int(source.physical_mode_code)],
        physical_mode_fresh=True,
        owner_kind=OwnerKind.RCE if active or transaction_pending else OwnerKind.NONE,
        owner_conflict=False,
        transaction_pending=transaction_pending,
        transaction_owner_kind=(
            OwnerKind.RCE if transaction_pending else OwnerKind.NONE
        ),
        full_block_execution_ready=True,
        direct_306_execution_ready=True,
        direct_259_execution_ready=True,
        topology_full_block_allowed=True,
        topology_direct_register_allowed=True,
        charge_direction_ready=True,
        discharge_direction_ready=True,
        critical_bms_ready=True,
        export_state=export_state,
        battery_soc_percent=float(source.battery_soc_percent),
        physical_protected_soc_floor_percent=float(source.self_use_soc_percent),
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
        tariff=TariffSourceSnapshot(),
        rcm=RcmSourceSnapshot(),
        execution=source,
    )


async def test_full_lifecycle() -> None:
    events: list[tuple[str, object]] = []
    publications: list[tuple[ActiveState, str]] = []

    async def persist(record) -> None:
        events.append(("persist", record.state))

    async def dispatch(write) -> None:
        events.append(("dispatch", write.family))

    def publish(record) -> None:
        events.append(("publish", record.state))
        publications.append(
            (record.state, controller.recorder_attributes()["execution_phase"])
        )

    controller = SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=publish,
        clock=ScriptedClock(NOW, NOW + timedelta(seconds=10)),
    )
    await controller.async_initialize()
    await controller.async_reconcile(frame(NOW, execution_source(NOW)))
    check(controller.record.state is ActiveState.WAITING_READBACK, "start must wait for readback")
    published_phase_by_state = dict(publications)
    check(
        all(
            published_phase_by_state.get(state) == phase
            for state, phase in (
                (ActiveState.IDLE, "idle"),
                (ActiveState.SELECTED, "selected"),
                (ActiveState.STARTING, "starting"),
                (ActiveState.WAITING_READBACK, "waiting_readback"),
            )
        ),
        "recorder lifecycle phases do not match the committed states",
    )
    dispatch_index = next(i for i, event in enumerate(events) if event[0] == "dispatch")
    starting_persist = max(i for i, event in enumerate(events[:dispatch_index]) if event == ("persist", ActiveState.STARTING))
    check(starting_persist < dispatch_index, "owner/command must persist before dispatch")

    readback_at = NOW + timedelta(seconds=5)
    await controller.async_reconcile(
        frame(
            readback_at,
            execution_source(
                readback_at,
                battery_charge_limit_generation=13,
                battery_charge_limit_percent=60,
                battery_charge_limit_generation_at=readback_at - timedelta(seconds=1),
                pv_power_w=4000,
                pv_power_observed_at=readback_at - timedelta(seconds=1),
                load_power_w=1000,
                load_power_observed_at=readback_at - timedelta(seconds=1),
                battery_power_w=-2000,
                battery_power_observed_at=readback_at - timedelta(seconds=1),
            ),
        )
    )
    check(controller.record.state is ActiveState.EXECUTING, "readback+physics must enter executing")
    attrs = controller.recorder_attributes()
    check(attrs["execution_phase"] == "executing", "executing phase was not published")
    check(
        attrs["selected_policy"] == "rcm"
        and attrs["transaction_candidate_identity"]
        == controller.record.transaction.intent.candidate_revision,
        "active transaction policy and stable identity were not published atomically",
    )
    check(
        "selected_candidate_revision" not in attrs,
        "internal transaction identity was exposed as the public candidate revision",
    )
    check(
        all(phase != "observed_active_latched" for _state, phase in publications),
        "recorder leaked the pure observed-active phase",
    )
    check(attrs["owner"] == "rcm", "A4 owner must be published")
    check(attrs["supervisor_execution_authorized"] is True, "authority only after physical proof")
    check(
        attrs["transaction_evidence_schema_version"] == 4
        and attrs["transaction_evidence_scope"] == "active"
        and attrs["transaction_evidence"]["transaction_id"]
        == controller.record.transaction.transaction_id,
        "active diagnostics must retain the complete versioned transaction",
    )
    check(
        attrs["transaction_evidence"]["command_snapshot"] is not None
        and attrs["transaction_evidence"]["expected_readback"] is not None
        and attrs["transaction_evidence"]["physical_verification"]["status"]
        == "confirmed",
        "active evidence must correlate command, readback and physical verification",
    )
    check(
        attrs["execution_context_evidence"]
        == {
            "observed_at": "2026-08-31T12:00:05.000000Z",
            "physical_mode": "self_use",
            "physical_mode_fresh": True,
            "owner_kind": "none",
            "owner_conflict": False,
            "transaction_pending": False,
            "transaction_owner_kind": "none",
            "full_block_execution_ready": True,
            "direct_306_execution_ready": True,
            "direct_259_execution_ready": True,
            "topology_full_block_allowed": True,
            "topology_direct_register_allowed": True,
            "charge_direction_ready": True,
            "discharge_direction_ready": True,
            "critical_bms_ready": True,
            "export_state": "verified_allowed",
        },
        "diagnostics must retain the exact bounded execution context",
    )

    stop_at = readback_at + timedelta(seconds=5)
    await controller.async_reconcile(
        frame(
            stop_at,
            execution_source(
                stop_at,
                battery_charge_limit_generation=14,
                battery_charge_limit_percent=60,
                pv_power_w=4000,
                pv_power_observed_at=stop_at - timedelta(seconds=1),
                load_power_w=1000,
                load_power_observed_at=stop_at - timedelta(seconds=1),
                battery_power_w=-1800,
                battery_power_observed_at=stop_at - timedelta(seconds=1),
            ),
            mode=SupervisorMode.OFF,
        )
    )
    check(controller.record.state is ActiveState.RESTORING, "Off must dispatch exact restore")
    restore_at = stop_at + timedelta(seconds=5)
    await controller.async_reconcile(
        frame(
            restore_at,
            execution_source(
                restore_at,
                battery_charge_limit_generation=15,
                battery_charge_limit_percent=80,
                battery_charge_limit_generation_at=restore_at - timedelta(seconds=1),
            ),
            mode=SupervisorMode.OFF,
        )
    )
    check(controller.record.state is ActiveState.IDLE, "restore readback must release owner")
    check(controller.record.owner is ExecutionOwner.NONE, "restore must release exact owner")
    terminal_attrs = controller.recorder_attributes()
    check(
        terminal_attrs["transaction_evidence_scope"] == "last"
        and terminal_attrs["transaction_evidence"]["transaction_id"]
        == attrs["transaction_evidence"]["transaction_id"],
        "terminal diagnostics must keep the completed transaction after IDLE",
    )
    check(
        terminal_attrs["transaction_evidence"]["rollback_status"] == "confirmed"
        and terminal_attrs["transaction_evidence"]["rollback_result"] == "confirmed"
        and terminal_attrs["transaction_evidence"]["restore_expected_readback"]
        is not None,
        "terminal evidence must keep the complete restore outcome",
    )


async def test_rce_initial_physical_effect_uses_fixed_timeout() -> None:
    deadline = NOW + timedelta(minutes=5)
    initial_sent_at = NOW + timedelta(milliseconds=100)
    writes: list[object] = []

    async def persist(_record) -> None:
        return None

    async def dispatch(write) -> None:
        writes.append(write)

    controller = SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=ScriptedClock(
            initial_sent_at,
            initial_sent_at + timedelta(seconds=90, milliseconds=600),
        ),
    )
    await controller.async_initialize()
    await controller.async_reconcile(
        rce_frame(
            NOW,
            execution_source(NOW),
            deadline=deadline,
            floor=55.0,
            power=10.0,
            active=False,
            slot_end=deadline,
        )
    )
    check(
        controller.record.state is ActiveState.WAITING_READBACK and len(writes) == 1,
        "RCE start did not dispatch exactly one full block",
    )
    transaction = controller.record.transaction
    assert transaction is not None
    check(
        transaction.command_sent_at == initial_sent_at,
        "RCE command did not retain its physical dispatch timestamp",
    )
    initial_watchdog = (
        transaction.transaction_id,
        initial_sent_at
        + timedelta(seconds=controller_module.COMMAND_ACK_TIMEOUT_SECONDS),
    )
    check(
        controller.execution_watchdog == initial_watchdog,
        "RCE initial physical-settling watchdog was not anchored to dispatch",
    )

    for second in (2, 10, 30, 89):
        at = initial_sent_at + timedelta(seconds=second)
        observed = at - timedelta(milliseconds=100)
        await controller.async_reconcile(
            rce_frame(
                at,
                execution_source(
                    at,
                    physical_mode_code=5,
                    full_block_generation=10 + second,
                    full_block_generation_at=observed,
                    force_discharge_soc_percent=55.0,
                    maximum_discharge_power_percent=10.0,
                    grid_power_w=0.0,
                    grid_power_observed_at=observed,
                    battery_power_w=-7200.0,
                    battery_power_observed_at=observed,
                    pv_power_w=8700.0,
                    pv_power_observed_at=observed,
                    load_power_w=1500.0,
                    load_power_observed_at=observed,
                ),
                deadline=deadline,
                floor=55.0,
                power=10.0,
                active=False,
                transaction_pending=True,
                slot_end=deadline,
            )
        )
        check(
            controller.record.state is ActiveState.WAITING_READBACK
            and controller.record.reason is ExecutionReason.PHYSICAL_PENDING
            and controller.record.transaction is not None
            and controller.record.transaction.command_sent_at == initial_sent_at
            and controller.execution_watchdog
            == (
                transaction.transaction_id,
                min(
                    initial_sent_at + timedelta(seconds=180),
                    observed
                    + timedelta(seconds=controller_module.MAX_READBACK_AGE_SECONDS),
                ),
            )
            and len(writes) == 1,
            "old PV-charging flow did not retain fixed timeout and evidence expiry",
        )

    confirmed_at = initial_sent_at + timedelta(seconds=89, milliseconds=500)
    confirmed_observed = confirmed_at - timedelta(milliseconds=100)
    await controller.async_reconcile(
        rce_frame(
            confirmed_at,
            execution_source(
                confirmed_at,
                physical_mode_code=5,
                full_block_generation=100,
                full_block_generation_at=confirmed_observed,
                force_discharge_soc_percent=55.0,
                maximum_discharge_power_percent=10.0,
                battery_power_w=900.0,
                battery_power_observed_at=confirmed_observed,
            ),
            deadline=deadline,
            floor=55.0,
            power=10.0,
            active=False,
            transaction_pending=True,
            slot_end=deadline,
        )
    )
    check(
        controller.record.state is ActiveState.EXECUTING
        and controller.record.reason is ExecutionReason.PHYSICALLY_CONFIRMED
        and controller.record.transaction is not None
        and controller.record.transaction.command_sent_at == initial_sent_at
        and len(writes) == 1,
        "fresh RCE discharge did not confirm within the original 90-second boundary",
    )

    reversed_at = initial_sent_at + timedelta(seconds=90, milliseconds=500)
    reversed_observed = reversed_at - timedelta(milliseconds=100)
    await controller.async_reconcile(
        rce_frame(
            reversed_at,
            execution_source(
                reversed_at,
                physical_mode_code=5,
                full_block_generation=101,
                full_block_generation_at=reversed_observed,
                force_discharge_soc_percent=55.0,
                maximum_discharge_power_percent=10.0,
                battery_power_w=-900.0,
                battery_power_observed_at=reversed_observed,
            ),
            deadline=deadline,
            floor=55.0,
            power=10.0,
            active=True,
            slot_end=deadline,
        )
    )
    check(
        controller.record.state is ActiveState.RESTORING
        and controller.record.owner is ExecutionOwner.RCE
        and len(writes) == 2
        and writes[-1].ems_block.mode is EmsMode.SELF_USE,
        "RCE flow reversal after first physical confirmation did not fail closed",
    )

    timeout_writes: list[object] = []

    async def timeout_dispatch(write) -> None:
        timeout_writes.append(write)

    timeout_controller = SupervisorActiveController(
        persist=persist,
        dispatch=timeout_dispatch,
        publish=lambda _record: None,
        clock=ScriptedClock(
            initial_sent_at,
            initial_sent_at + timedelta(seconds=90, milliseconds=100),
        ),
    )
    await timeout_controller.async_initialize()
    await timeout_controller.async_reconcile(
        rce_frame(
            NOW,
            execution_source(NOW),
            deadline=deadline,
            floor=55.0,
            power=10.0,
            active=False,
            slot_end=deadline,
        )
    )
    timeout_at = initial_sent_at + timedelta(seconds=90)
    timeout_observed = timeout_at - timedelta(milliseconds=100)
    await timeout_controller.async_reconcile(
        rce_frame(
            timeout_at,
            execution_source(
                timeout_at,
                physical_mode_code=5,
                full_block_generation=110,
                full_block_generation_at=timeout_observed,
                force_discharge_soc_percent=55.0,
                maximum_discharge_power_percent=10.0,
                battery_power_w=-7200.0,
                battery_power_observed_at=timeout_observed,
            ),
            deadline=deadline,
            floor=55.0,
            power=10.0,
            active=False,
            transaction_pending=True,
            slot_end=deadline,
        )
    )
    check(
        timeout_controller.record.state is ActiveState.RESTORING
        and timeout_controller.record.owner is ExecutionOwner.RCE
        and len(timeout_writes) == 2
        and timeout_writes[-1].ems_block.mode is EmsMode.SELF_USE,
        "RCE physical settling exceeded the fixed 90-second boundary",
    )


async def test_rce_initial_pending_uses_real_builder() -> None:
    deadline = NOW + timedelta(minutes=5)
    sent_at = NOW + timedelta(milliseconds=100)
    for start_eligible in (False, True):
        writes: list[object] = []
        controller = SupervisorActiveController(
            persist=lambda _record: _done(),
            dispatch=lambda write: _append_async(writes, write),
            publish=lambda _record: None,
            clock=ScriptedClock(sent_at),
        )
        await controller.async_initialize()
        await controller.async_reconcile(
            rce_frame(
                NOW,
                execution_source(NOW),
                deadline=deadline,
                floor=55.0,
                power=10.0,
                active=False,
                slot_end=deadline,
            )
        )
        original = controller.record.transaction
        assert original is not None
        for seconds in (2, 30, 89):
            at = sent_at + timedelta(seconds=seconds)
            observed = at - timedelta(milliseconds=100)
            frame = rce_frame(
                at,
                execution_source(
                    at,
                    physical_mode_code=5,
                    full_block_generation=10 + seconds,
                    full_block_generation_at=observed,
                    force_discharge_soc_percent=55.0,
                    maximum_discharge_power_percent=10.0,
                    battery_power_w=-7200.0,
                    battery_power_observed_at=observed,
                    grid_power_w=0.0,
                    grid_power_observed_at=observed,
                    pv_power_w=8700.0,
                    pv_power_observed_at=observed,
                    load_power_w=1500.0,
                    load_power_observed_at=observed,
                ),
                deadline=deadline,
                floor=55.0,
                power=10.0,
                active=False,
                transaction_pending=True,
                observed_at=at,
                result_current=False,
                recalculation_pending=True,
                control_data_ready=False,
                reserve_ready=False,
                start_eligible=start_eligible,
                slot_end=deadline,
            )
            candidate = next(
                item for item in frame.candidates if item.policy_id is PolicyId.RCE
            )
            check(
                candidate.requested_action is RequestedAction.RCE_EXPORT
                and not candidate.result_current
                and candidate.recalculation_pending,
                "initial pending regression did not use the real RCE_EXPORT builder",
            )
            await controller.async_reconcile(frame)
            current = controller.record.transaction
            check(
                controller.record.state is ActiveState.WAITING_READBACK
                and controller.record.reason is ExecutionReason.PHYSICAL_PENDING
                and current is not None
                and current.transaction_id == original.transaction_id
                and current.command_sent_at == sent_at
                and current.deadline == deadline
                and current.command_snapshot == original.command_snapshot
                and current.readback_result is VerificationStatus.CONFIRMED
                and current.physical_verification is not None
                and current.physical_verification.status is VerificationStatus.PENDING
                and len(writes) == 1,
                f"real pending plan revoked or renewed initial WAITING (start={start_eligible})",
            )
            check(
                controller.execution_watchdog
                == (
                    original.transaction_id,
                    min(
                        sent_at + timedelta(seconds=180),
                        observed
                        + timedelta(seconds=controller_module.MAX_READBACK_AGE_SECONDS),
                    ),
                ),
                "pending plan publication renewed the original physical-settling watchdog",
            )


async def test_master_stop_and_persist_failure() -> None:
    writes: list[object] = []
    grid_at = NOW + timedelta(minutes=1)

    async def persist(_record) -> None:
        return None

    async def dispatch(write) -> None:
        writes.append(write)

    controller = SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=ScriptedClock(grid_at),
    )
    await controller.async_initialize()
    current = execution_source(grid_at, physical_mode_code=4)
    await controller.async_master_stop(frame(grid_at, current, enabled=False))
    check(controller.record.state is ActiveState.RESTORING, "MASTER STOP must dispatch Self-Use restore")
    check(controller.record.starts_allowed is False, "MASTER STOP must latch starts off")
    done_at = grid_at + timedelta(seconds=5)
    await controller.async_reconcile(
        frame(
            done_at,
            execution_source(
                done_at,
                physical_mode_code=0,
                full_block_generation=11,
                full_block_generation_at=done_at - timedelta(seconds=1),
            ),
            mode=SupervisorMode.OFF,
            enabled=False,
        )
    )
    check(controller.record.master_stop_result.status is MasterStopStatus.COMPLETED, "MASTER STOP readback must complete")
    check(controller.record.owner is ExecutionOwner.NONE, "MASTER STOP must release owner")

    deferred_writes: list[object] = []
    deferred_dispatches = 0
    retry_at = grid_at + timedelta(seconds=1)

    async def defer_once(write) -> None:
        nonlocal deferred_dispatches
        deferred_dispatches += 1
        if deferred_dispatches == 1:
            raise AtomicWriteNotQueued("previous_write_pending")
        deferred_writes.append(write)

    deferred = SupervisorActiveController(
        persist=persist,
        dispatch=defer_once,
        publish=lambda _record: None,
        clock=ScriptedClock(retry_at),
    )
    await deferred.async_initialize()
    await deferred.async_master_stop(
        frame(grid_at, execution_source(grid_at, physical_mode_code=4), enabled=False)
    )
    deferred_transaction = deferred.record.transaction
    check(
        deferred.record.state is ActiveState.STOPPING
        and deferred_transaction is not None
        and deferred_transaction.rollback_status is RollbackStatus.PENDING
        and deferred_transaction.restore_attempts == 0
        and deferred_transaction.restore_sent_at is None
        and not deferred_writes,
        "proven pretransport restore rejection consumed a retry or claimed transport",
    )
    await deferred.async_reconcile(
        frame(
            retry_at,
            execution_source(
                retry_at,
                physical_mode_code=4,
                full_block_generation=11,
                full_block_generation_at=retry_at - timedelta(seconds=1),
            ),
            mode=SupervisorMode.OFF,
            enabled=False,
        )
    )
    deferred_transaction = deferred.record.transaction
    check(
        deferred.record.state is ActiveState.RESTORING
        and deferred_transaction is not None
        and deferred_transaction.restore_attempts == 1
        and deferred_transaction.restore_sent_at == retry_at
        and len(deferred_writes) == 1,
        "fresh FC03 did not retry exactly one deferred restore",
    )
    deferred_done_at = retry_at + timedelta(seconds=1)
    await deferred.async_reconcile(
        frame(
            deferred_done_at,
            execution_source(
                deferred_done_at,
                physical_mode_code=0,
                full_block_generation=12,
                full_block_generation_at=deferred_done_at - timedelta(milliseconds=100),
            ),
            mode=SupervisorMode.OFF,
            enabled=False,
        )
    )
    check(
        deferred.record.state is ActiveState.IDLE
        and deferred.record.owner is ExecutionOwner.NONE
        and deferred.record.master_stop_result.status is MasterStopStatus.COMPLETED,
        "deferred MASTER STOP restore did not finish after physical confirmation: "
        f"state={deferred.record.state.value} owner={deferred.record.owner.value} "
        f"stop={deferred.record.master_stop_result.status.value} "
        f"reason={deferred.record.reason.value}",
    )

    failed_writes: list[object] = []
    calls = 0

    async def fail_after_initialize(_record) -> None:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise OSError("store unavailable")

    failed = SupervisorActiveController(
        persist=fail_after_initialize,
        dispatch=lambda write: _append_async(failed_writes, write),
        publish=lambda _record: None,
    )
    await failed.async_initialize()
    try:
        await failed.async_reconcile(frame(NOW, execution_source(NOW)))
    except OSError:
        pass
    else:
        raise AssertionError("persistence failure must propagate")
    check(not failed_writes, "no write may occur after persistence failure")

    hung_writes: list[object] = []
    persist_entered = asyncio.Event()
    persist_release = asyncio.Event()
    persist_calls = 0

    async def hang_after_initialize(_record) -> None:
        nonlocal persist_calls
        persist_calls += 1
        if persist_calls > 1:
            persist_entered.set()
            await persist_release.wait()

    hung = SupervisorActiveController(
        persist=hang_after_initialize,
        dispatch=lambda write: _append_async(hung_writes, write),
        publish=lambda _record: None,
        clock=ScriptedClock(NOW + timedelta(milliseconds=100)),
    )
    await hung.async_initialize()
    before = hung.record
    original_timeout = controller_module.MASTER_STOP_PERSIST_TIMEOUT_SECONDS
    controller_module.MASTER_STOP_PERSIST_TIMEOUT_SECONDS = 0.01
    try:
        await hung.async_master_stop(
            frame(NOW, execution_source(NOW, physical_mode_code=4))
        )
    finally:
        controller_module.MASTER_STOP_PERSIST_TIMEOUT_SECONDS = original_timeout
        persist_release.set()
    check(persist_entered.is_set(), "hung MASTER STOP journal was not exercised")
    check(before.starts_allowed, "hung fixture did not begin from an armed record")
    check(
        len(hung_writes) == 1 and hung.record.state is ActiveState.RESTORING,
        "hung MASTER STOP journal blocked the one legal physical restore: "
        f"writes={len(hung_writes)} state={hung.record.state.value} "
        f"reason={hung.record.reason.value}",
    )
    check(not hung.record_persisted, "hung MASTER STOP journal claimed durability")
    check(
        hung.persistence_error == "master_stop_record_persist_timeout",
        "hung MASTER STOP journal did not expose its timeout",
    )

    recovered_at = NOW + timedelta(seconds=5)
    await hung.async_reconcile(
        frame(
            recovered_at,
            execution_source(
                recovered_at,
                physical_mode_code=0,
                full_block_generation=11,
                full_block_generation_at=recovered_at - timedelta(seconds=1),
            ),
            mode=SupervisorMode.OFF,
            enabled=False,
        )
    )
    check(
        hung.record.master_stop_result.status is MasterStopStatus.COMPLETED,
        "fresh FC03 did not complete STOP after Store recovery",
    )
    check(hung.record_persisted, "newest recovered STOP revision was not durable")
    check(hung.persistence_error is None, "Store recovery left a sticky error")

    exception_writes: list[object] = []
    exception_calls = 0
    store_failed = True

    async def fail_master_stop_store(_record) -> None:
        nonlocal exception_calls
        exception_calls += 1
        if exception_calls == 3 and store_failed:
            raise OSError("MASTER STOP store unavailable")

    exception_controller = SupervisorActiveController(
        persist=fail_master_stop_store,
        dispatch=lambda write: _append_async(exception_writes, write),
        publish=lambda _record: None,
        clock=ScriptedClock(grid_at),
    )
    await exception_controller.async_initialize()
    await exception_controller.async_master_stop(
        frame(grid_at, execution_source(grid_at, physical_mode_code=4), enabled=False)
    )
    check(
        len(exception_writes) == 1
        and exception_controller.record.state is ActiveState.RESTORING,
        "Store exception blocked physical MASTER STOP",
    )
    check(
        exception_controller.persistence_error == "master_stop_record_persist_failed",
        "Store exception was not reported separately from physical state",
    )
    exception_attrs = exception_controller.recorder_attributes()
    check(
        exception_attrs["execution_record_durable"] is False
        and exception_attrs["execution_persistence_error"]
        == "master_stop_record_persist_failed",
        "Recorder did not separate physical progress from durable state",
    )
    store_failed = False
    exception_done_at = grid_at + timedelta(seconds=5)
    await exception_controller.async_reconcile(
        frame(
            exception_done_at,
            execution_source(
                exception_done_at,
                physical_mode_code=0,
                full_block_generation=11,
                full_block_generation_at=exception_done_at - timedelta(seconds=1),
            ),
            mode=SupervisorMode.OFF,
            enabled=False,
        )
    )
    check(
        exception_controller.record_persisted
        and exception_controller.persistence_error is None,
        "Store recovery did not persist the newest terminal STOP revision",
    )
    recovered_attrs = exception_controller.recorder_attributes()
    check(
        recovered_attrs["execution_record_durable"] is True
        and recovered_attrs["execution_persisted_revision"]
        == recovered_attrs["executor_revision"],
        "Recorder did not publish the recovered durable revision",
    )


async def test_authorization_freshness_pending_and_block_retry() -> None:
    published: list[ActiveState] = []

    async def persist(_record) -> None:
        return None

    async def dispatch(_write) -> None:
        return None

    unauthorized = SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda record: published.append(record.state),
        clock=ScriptedClock(NOW, NOW + timedelta(seconds=5)),
    )
    await unauthorized.async_initialize()
    await unauthorized.async_reconcile(frame(NOW, execution_source(NOW)))
    readback_at = NOW + timedelta(seconds=5)
    await unauthorized.async_reconcile(
        frame(
            readback_at,
            execution_source(
                readback_at,
                battery_charge_limit_generation=13,
                battery_charge_limit_percent=60,
                battery_charge_limit_generation_at=readback_at - timedelta(seconds=1),
            ),
            enabled=False,
        )
    )
    check(
        unauthorized.record.state is ActiveState.RESTORING,
        "lost arbitration authorization must restore before execution",
    )
    check(
        ActiveState.EXECUTING not in published,
        "WAITING_READBACK may not cross into EXECUTING without authorization",
    )

    stale = SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=ScriptedClock(NOW, NOW + timedelta(seconds=21)),
    )
    await stale.async_initialize()
    await stale.async_reconcile(frame(NOW, execution_source(NOW)))
    await stale.async_reconcile(
        frame(
            readback_at,
            execution_source(
                readback_at,
                battery_charge_limit_generation=13,
                battery_charge_limit_percent=60,
                battery_charge_limit_generation_at=readback_at - timedelta(seconds=1),
                pv_power_w=4000,
                pv_power_observed_at=readback_at - timedelta(seconds=1),
                load_power_w=1000,
                load_power_observed_at=readback_at - timedelta(seconds=1),
                battery_power_w=-2000,
                battery_power_observed_at=readback_at - timedelta(seconds=1),
            ),
        )
    )
    check(stale.record.state is ActiveState.EXECUTING, "fixture must reach execution")
    stale_at = NOW + timedelta(seconds=21)
    stale_sample_at = readback_at - timedelta(seconds=1)
    await stale.async_reconcile(
        frame(
            stale_at,
            execution_source(
                stale_at,
                battery_charge_limit_generation=13,
                battery_charge_limit_percent=60,
                battery_charge_limit_generation_at=stale_at - timedelta(seconds=1),
                pv_power_w=4000,
                pv_power_observed_at=stale_sample_at,
                load_power_w=1000,
                load_power_observed_at=stale_sample_at,
                battery_power_w=-2000,
                battery_power_observed_at=stale_sample_at,
            ),
        )
    )
    check(
        stale.record.state is ActiveState.RESTORING,
        "execution authority must be revoked when no fresh physical sample arrives",
    )
    check(
        stale.recorder_attributes()["supervisor_execution_authorized"] is False,
        "stale physical evidence must remove the published authority bit",
    )

    persist_count = 0

    async def count_persist(_record) -> None:
        nonlocal persist_count
        persist_count += 1

    pending = SupervisorActiveController(
        persist=count_persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=ScriptedClock(NOW),
    )
    await pending.async_initialize()
    await pending.async_reconcile(frame(NOW, execution_source(NOW)))
    unchanged_source = execution_source(readback_at)
    await pending.async_reconcile(frame(readback_at, unchanged_source))
    after_first_pending = persist_count
    await pending.async_reconcile(
        frame(NOW + timedelta(seconds=6), unchanged_source)
    )
    check(
        persist_count == after_first_pending,
        "identical pending readback must not churn Store revisions",
    )

    blocked = SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=ScriptedClock(NOW + timedelta(seconds=3)),
    )
    await blocked.async_initialize()
    await blocked.async_reconcile(
        frame(
            NOW,
            execution_source(NOW, direct_306_execution_ready=False),
        )
    )
    check(blocked.record.state is ActiveState.BLOCKED, "closed gate must block start")
    blocked_revision = blocked.record.revision
    still_blocked_at = NOW + timedelta(seconds=1)
    await blocked.async_reconcile(
        frame(
            still_blocked_at,
            execution_source(
                still_blocked_at,
                direct_306_execution_ready=False,
            ),
        )
    )
    check(
        blocked.record.revision == blocked_revision,
        "new timestamps alone must not flap an unresolved block",
    )
    recovered_at = NOW + timedelta(seconds=2)
    await blocked.async_reconcile(
        frame(recovered_at, execution_source(recovered_at))
    )
    check(
        blocked.record.state is ActiveState.IDLE,
        "resolved gate evidence must release a transient block for reselection",
    )
    restart_at = NOW + timedelta(seconds=3)
    await blocked.async_reconcile(frame(restart_at, execution_source(restart_at)))
    check(
        blocked.record.state is ActiveState.WAITING_READBACK,
        "same candidate must retry after its physical gate recovers",
    )


async def test_bounded_dispatch_timeout() -> None:
    original_timeout = controller_module.COMMAND_DISPATCH_TIMEOUT_SECONDS
    original_restore_timeout = controller_module.RESTORE_DISPATCH_TIMEOUT_SECONDS
    check(original_timeout == 30.0, "command dispatch timeout contract must be 30 s")
    check(
        controller_module.COMMAND_ACK_TIMEOUT_SECONDS == 90.0,
        "command FC03 ACK timeout contract must be 90 s",
    )
    check(
        controller_module.RESTORE_DISPATCH_TIMEOUT_SECONDS == 30.0,
        "restore dispatch timeout contract must be 30 s",
    )
    check(
        controller_module.RESTORE_ACK_TIMEOUT_SECONDS == 90.0,
        "restore FC03 ACK timeout contract must be 90 s",
    )
    controller_module.COMMAND_DISPATCH_TIMEOUT_SECONDS = 0.001
    controller_module.RESTORE_DISPATCH_TIMEOUT_SECONDS = 0.001

    async def slow_dispatch(_write) -> None:
        await asyncio.sleep(0.02)

    try:
        controller = SupervisorActiveController(
            persist=lambda _record: _append_async([], None),
            dispatch=slow_dispatch,
            publish=lambda _record: None,
        )
        await controller.async_initialize()
        await controller.async_reconcile(frame(NOW, execution_source(NOW)))
        check(
            controller.record.state is ActiveState.FAULT,
            "timed-out dispatch must retain ownership for recovery",
        )
        check(
            controller.record.reason is ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
            "dispatch timeout must never be treated as a successful command",
        )

        dispatch_count = 0

        async def slow_restore_dispatch(_write) -> None:
            nonlocal dispatch_count
            dispatch_count += 1
            if dispatch_count > 1:
                await asyncio.sleep(0.02)

        restore_controller = SupervisorActiveController(
            persist=lambda _record: _done(),
            dispatch=slow_restore_dispatch,
            publish=lambda _record: None,
            clock=ScriptedClock(NOW),
        )
        await restore_controller.async_initialize()
        await restore_controller.async_reconcile(frame(NOW, execution_source(NOW)))
        readback_at = NOW + timedelta(seconds=5)
        await restore_controller.async_reconcile(
            frame(
                readback_at,
                execution_source(
                    readback_at,
                    battery_charge_limit_generation=13,
                    battery_charge_limit_percent=60,
                    battery_charge_limit_generation_at=(
                        readback_at - timedelta(seconds=1)
                    ),
                    pv_power_w=4000,
                    pv_power_observed_at=readback_at - timedelta(seconds=1),
                    load_power_w=1000,
                    load_power_observed_at=readback_at - timedelta(seconds=1),
                    battery_power_w=-2000,
                    battery_power_observed_at=readback_at - timedelta(seconds=1),
                ),
            )
        )
        check(
            restore_controller.record.state is ActiveState.EXECUTING,
            "restore-timeout fixture did not reach execution",
        )
        stop_at = NOW + timedelta(seconds=10)
        await restore_controller.async_reconcile(
            frame(
                stop_at,
                execution_source(
                    stop_at,
                    battery_charge_limit_generation=14,
                    battery_charge_limit_percent=60,
                    battery_charge_limit_generation_at=stop_at - timedelta(seconds=1),
                ),
                mode=SupervisorMode.OFF,
            )
        )
        check(
            restore_controller.record.state is ActiveState.FAULT
            and restore_controller.record.reason
            is ExecutionReason.COMMAND_OUTCOME_UNKNOWN
            and restore_controller.record.transaction is not None
            and restore_controller.record.transaction.rollback_status
            is RollbackStatus.PENDING,
            "restore dispatch timeout did not retain pending rollback ownership",
        )
    finally:
        controller_module.COMMAND_DISPATCH_TIMEOUT_SECONDS = original_timeout
        controller_module.RESTORE_DISPATCH_TIMEOUT_SECONDS = original_restore_timeout


async def test_actual_dispatch_completion_is_readback_boundary() -> None:
    command_completed_at = NOW + timedelta(seconds=2)
    restore_frame_at = NOW + timedelta(seconds=10)
    restore_completed_at = NOW + timedelta(seconds=12)
    completions = (command_completed_at, restore_completed_at)
    completed_dispatches = 0
    clock_reads = 0

    async def dispatch(_write) -> None:
        nonlocal completed_dispatches
        # Yield once to model a transport call which completes after frame.now.
        await asyncio.sleep(0)
        completed_dispatches += 1

    def completion_clock() -> datetime:
        nonlocal clock_reads
        if completed_dispatches <= clock_reads:
            raise AssertionError("clock was sampled before dispatch completed")
        value = completions[clock_reads]
        clock_reads += 1
        return value

    controller = SupervisorActiveController(
        persist=lambda _record: _done(),
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=completion_clock,
    )
    await controller.async_initialize()
    await controller.async_reconcile(frame(NOW, execution_source(NOW)))
    transaction = controller.record.transaction
    check(transaction is not None, "command transaction must exist")
    check(
        transaction.command_sent_at == command_completed_at,
        "command boundary must be captured after transport completion",
    )

    command_between = NOW + timedelta(seconds=1)
    check(
        NOW < command_between <= command_completed_at,
        "command fixture must lie inside the dispatch interval",
    )
    await controller.async_reconcile(
        frame(
            NOW + timedelta(seconds=3),
            execution_source(
                NOW + timedelta(seconds=3),
                battery_charge_limit_generation=13,
                battery_charge_limit_percent=60,
                battery_charge_limit_generation_at=command_between,
            ),
        )
    )
    check(
        controller.record.state is ActiveState.WAITING_READBACK,
        "a sample captured before command completion must not acknowledge it",
    )
    check(
        controller.record.reason is ExecutionReason.READBACK_PENDING,
        "pre-completion command evidence must remain pending",
    )

    command_after = command_completed_at + timedelta(seconds=1)
    await controller.async_reconcile(
        frame(
            NOW + timedelta(seconds=4),
            execution_source(
                NOW + timedelta(seconds=4),
                battery_charge_limit_generation=14,
                battery_charge_limit_percent=60,
                battery_charge_limit_generation_at=command_after,
                pv_power_w=4000,
                pv_power_observed_at=command_after,
                load_power_w=1000,
                load_power_observed_at=command_after,
                battery_power_w=-2000,
                battery_power_observed_at=command_after,
            ),
        )
    )
    check(
        controller.record.state is ActiveState.EXECUTING,
        "fresh post-completion command evidence must still execute",
    )

    await controller.async_reconcile(
        frame(
            restore_frame_at,
            execution_source(
                restore_frame_at,
                battery_charge_limit_generation=14,
                battery_charge_limit_percent=60,
                battery_charge_limit_generation_at=(
                    restore_frame_at - timedelta(seconds=1)
                ),
            ),
            mode=SupervisorMode.OFF,
        )
    )
    transaction = controller.record.transaction
    check(transaction is not None, "restore transaction must exist")
    check(
        transaction.restore_sent_at == restore_completed_at,
        "restore boundary must be captured after transport completion",
    )

    restore_between = restore_frame_at + timedelta(seconds=1)
    check(
        restore_frame_at < restore_between <= restore_completed_at,
        "restore fixture must lie inside the dispatch interval",
    )
    await controller.async_reconcile(
        frame(
            NOW + timedelta(seconds=13),
            execution_source(
                NOW + timedelta(seconds=13),
                battery_charge_limit_generation=15,
                battery_charge_limit_percent=80,
                battery_charge_limit_generation_at=restore_between,
            ),
            mode=SupervisorMode.OFF,
        )
    )
    check(
        controller.record.state is ActiveState.RESTORING,
        "a sample captured before restore completion must not acknowledge it",
    )

    restore_after = restore_completed_at + timedelta(seconds=1)
    await controller.async_reconcile(
        frame(
            NOW + timedelta(seconds=14),
            execution_source(
                NOW + timedelta(seconds=14),
                battery_charge_limit_generation=16,
                battery_charge_limit_percent=80,
                battery_charge_limit_generation_at=restore_after,
            ),
            mode=SupervisorMode.OFF,
        )
    )
    check(
        controller.record.state is ActiveState.IDLE,
        "fresh post-completion restore evidence must release ownership",
    )
    check(clock_reads == 2, "clock must be sampled once per completed dispatch")


async def test_invalid_dispatch_clock_fails_closed() -> None:
    command_dispatches = 0

    async def command_dispatch(_write) -> None:
        nonlocal command_dispatches
        command_dispatches += 1

    invalid_command = SupervisorActiveController(
        persist=lambda _record: _done(),
        dispatch=command_dispatch,
        publish=lambda _record: None,
        clock=ScriptedClock(object()),
    )
    await invalid_command.async_initialize()
    await invalid_command.async_reconcile(frame(NOW, execution_source(NOW)))
    check(command_dispatches == 1, "invalid clock fixture must dispatch first")
    check(
        invalid_command.record.state is ActiveState.FAULT,
        "invalid command clock must fail closed",
    )
    check(
        invalid_command.record.reason is ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
        "invalid command clock must not publish a false ACK",
    )
    transaction = invalid_command.record.transaction
    check(
        transaction is not None and transaction.command_sent_at is None,
        "invalid command clock must not invent command_sent_at",
    )

    naive_restore_time = datetime(2026, 8, 31, 12, 0)
    invalid_restore = SupervisorActiveController(
        persist=lambda _record: _done(),
        dispatch=lambda _write: _done(),
        publish=lambda _record: None,
        clock=ScriptedClock(NOW, naive_restore_time),
    )
    await invalid_restore.async_initialize()
    await invalid_restore.async_reconcile(frame(NOW, execution_source(NOW)))
    readback_at = NOW + timedelta(seconds=5)
    await invalid_restore.async_reconcile(
        frame(
            readback_at,
            execution_source(
                readback_at,
                battery_charge_limit_generation=13,
                battery_charge_limit_percent=60,
                battery_charge_limit_generation_at=(
                    readback_at - timedelta(seconds=1)
                ),
                pv_power_w=4000,
                pv_power_observed_at=readback_at - timedelta(seconds=1),
                load_power_w=1000,
                load_power_observed_at=readback_at - timedelta(seconds=1),
                battery_power_w=-2000,
                battery_power_observed_at=readback_at - timedelta(seconds=1),
            ),
        )
    )
    check(
        invalid_restore.record.state is ActiveState.EXECUTING,
        "invalid restore fixture must first reach execution",
    )
    stop_at = NOW + timedelta(seconds=10)
    await invalid_restore.async_reconcile(
        frame(
            stop_at,
            execution_source(
                stop_at,
                battery_charge_limit_generation=14,
                battery_charge_limit_percent=60,
                battery_charge_limit_generation_at=stop_at - timedelta(seconds=1),
            ),
            mode=SupervisorMode.OFF,
        )
    )
    check(
        invalid_restore.record.state is ActiveState.FAULT,
        "naive restore clock must fail closed",
    )
    check(
        invalid_restore.record.reason is ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
        "naive restore clock must retain unknown transport outcome",
    )
    transaction = invalid_restore.record.transaction
    check(
        transaction is not None and transaction.restore_sent_at is None,
        "naive restore clock must not invent restore_sent_at",
    )
    check(
        invalid_restore.record.owner is ExecutionOwner.RCM,
        "unknown restore outcome must retain ownership",
    )


async def test_prewrite_resample_blocks_stale_start_and_restore() -> None:
    """Store awaits may not let a stale frame overwrite physical Off-Grid."""

    command_ready = asyncio.Event()
    release_command = asyncio.Event()
    command_writes: list[object] = []
    latest = frame(NOW, execution_source(NOW))

    async def command_persist(record) -> None:
        if (
            record.state is ActiveState.STARTING
            and record.reason is ExecutionReason.COMMAND_READY
            and not command_ready.is_set()
        ):
            command_ready.set()
            await release_command.wait()

    command_controller = SupervisorActiveController(
        persist=command_persist,
        dispatch=lambda write: _append_async(command_writes, write),
        publish=lambda _record: None,
        frame_resampler=lambda: latest,
    )
    await command_controller.async_initialize()
    start_task = asyncio.create_task(command_controller.async_reconcile(latest))
    await command_ready.wait()
    changed_at = NOW + timedelta(seconds=1)
    latest = frame(
        changed_at,
        execution_source(
            changed_at,
            physical_mode_code=3,
            full_block_generation=11,
            full_block_generation_at=changed_at - timedelta(milliseconds=100),
        ),
        mode=SupervisorMode.OFF,
        enabled=False,
    )
    release_command.set()
    await start_task
    check(not command_writes, "Stale start frame overwrote physical Off-Grid")
    check(
        command_controller.record.state is ActiveState.BLOCKED
        and command_controller.record.owner is ExecutionOwner.NONE,
        "Revoked prewrite did not release ownership without rollback",
    )
    reauthorized_at = changed_at + timedelta(seconds=1)
    latest = frame(reauthorized_at, execution_source(reauthorized_at))
    await command_controller.async_reconcile(latest)
    check(
        command_controller.record.state is ActiveState.IDLE,
        "restored authorization must release an owner-free blocked transaction",
    )

    restore_ready = asyncio.Event()
    release_restore = asyncio.Event()
    restore_barrier_enabled = False
    restore_writes: list[object] = []
    latest_restore = frame(NOW, execution_source(NOW))

    async def restore_persist(record) -> None:
        if (
            restore_barrier_enabled
            and record.state is ActiveState.STOPPING
            and record.reason is ExecutionReason.RESTORING
            and not restore_ready.is_set()
        ):
            restore_ready.set()
            await release_restore.wait()

    restore_controller = SupervisorActiveController(
        persist=restore_persist,
        dispatch=lambda write: _append_async(restore_writes, write),
        publish=lambda _record: None,
        clock=ScriptedClock(NOW),
        frame_resampler=lambda: latest_restore,
    )
    await restore_controller.async_initialize()
    await restore_controller.async_reconcile(latest_restore)
    readback_at = NOW + timedelta(seconds=5)
    latest_restore = frame(
        readback_at,
        execution_source(
            readback_at,
            battery_charge_limit_generation=13,
            battery_charge_limit_percent=60,
            battery_charge_limit_generation_at=readback_at - timedelta(seconds=1),
            pv_power_w=4000,
            pv_power_observed_at=readback_at - timedelta(seconds=1),
            load_power_w=1000,
            load_power_observed_at=readback_at - timedelta(seconds=1),
            battery_power_w=-2000,
            battery_power_observed_at=readback_at - timedelta(seconds=1),
        ),
    )
    await restore_controller.async_reconcile(latest_restore)
    check(
        restore_controller.record.state is ActiveState.EXECUTING,
        "Stale restore fixture did not reach execution",
    )
    check(len(restore_writes) == 1, "Stale restore fixture command count differs")
    restore_barrier_enabled = True
    stop_at = readback_at + timedelta(seconds=5)
    latest_restore = frame(
        stop_at,
        execution_source(
            stop_at,
            battery_charge_limit_generation=14,
            battery_charge_limit_percent=60,
            battery_charge_limit_generation_at=stop_at - timedelta(seconds=1),
        ),
        mode=SupervisorMode.OFF,
        enabled=False,
    )
    stop_task = asyncio.create_task(
        restore_controller.async_reconcile(latest_restore)
    )
    await restore_ready.wait()
    off_grid_at = stop_at + timedelta(seconds=1)
    latest_restore = frame(
        off_grid_at,
        execution_source(
            off_grid_at,
            physical_mode_code=3,
            full_block_generation=11,
            full_block_generation_at=off_grid_at - timedelta(milliseconds=100),
            battery_charge_limit_generation=15,
            battery_charge_limit_percent=60,
            battery_charge_limit_generation_at=off_grid_at - timedelta(milliseconds=100),
        ),
        mode=SupervisorMode.OFF,
        enabled=False,
    )
    release_restore.set()
    await stop_task
    check(
        len(restore_writes) == 1,
        "Stale restore frame overwrote a late physical Off-Grid transition",
    )
    check(
        restore_controller.record.state is ActiveState.STOPPING,
        "Deferred restore did not retain recoverable ownership",
    )


async def test_manual_proxy_dispatch_is_serialized_with_active_reconcile() -> None:
    """A proxy call authorized while Off must finish before Active can write."""

    sequence: list[str] = []
    manual_entered = asyncio.Event()
    release_manual = asyncio.Event()
    authority = True
    readback_ready = False

    async def persist(_record) -> None:
        return None

    async def automatic_dispatch(_write) -> None:
        sequence.append("automatic")

    async def manual_dispatch() -> None:
        sequence.append("manual_started")
        manual_entered.set()
        await release_manual.wait()
        sequence.append("manual_finished")

    controller = SupervisorActiveController(
        persist=persist,
        dispatch=automatic_dispatch,
        publish=lambda _record: None,
        clock=ScriptedClock(NOW),
    )
    await controller.async_initialize()
    manual_task = asyncio.create_task(
        controller.async_manual_proxy_dispatch(
            authority_valid=lambda: authority,
            dispatch=manual_dispatch,
            readback_resolved=lambda: readback_ready,
        )
    )
    await manual_entered.wait()
    authority = False  # Models the helper changing Off -> Active.
    reconcile_task = asyncio.create_task(
        controller.async_reconcile(frame(NOW, execution_source(NOW)))
    )
    await asyncio.sleep(0)
    check(not reconcile_task.done(), "Active reconcile bypassed the manual proxy lock")
    check(
        controller.record.owner is ExecutionOwner.NONE,
        "Active acquired ownership before the serialized manual write completed",
    )
    release_manual.set()
    check(await manual_task, "Safe-Off manual proxy dispatch was rejected")
    await reconcile_task
    check(
        sequence == ["manual_started", "manual_finished"],
        "Active transport started before manual physical readback",
    )
    check(
        controller.record.owner is ExecutionOwner.NONE,
        "Active acquired ownership while manual readback was pending",
    )
    readback_ready = True
    await controller.async_reconcile(frame(NOW, execution_source(NOW)))
    check(
        sequence == ["manual_started", "manual_finished", "automatic"],
        "Automatic transport overlapped or preceded the manual proxy write",
    )
    check(
        controller.record.owner is ExecutionOwner.RCM,
        "Active did not acquire ownership after the manual write completed",
    )
    denied: list[str] = []
    check(
        not await controller.async_manual_proxy_dispatch(
            authority_valid=lambda: False,
            dispatch=lambda: _append_async(denied, "manual"),
            readback_resolved=lambda: True,
        ),
        "Unsafe manual proxy dispatch was accepted",
    )
    check(not denied, "Rejected manual proxy dispatch reached transport")


async def test_same_window_rce_retarget_and_bounded_replan_holds() -> None:
    deadline = NOW + timedelta(minutes=30)

    def physical_old(at: datetime, *, generation: int = 11) -> ExecutionSourceSnapshot:
        observed = at - timedelta(milliseconds=500)
        return execution_source(
            at,
            physical_mode_code=5,
            full_block_generation=generation,
            full_block_generation_at=observed,
            force_discharge_soc_percent=41.0,
            maximum_discharge_power_percent=25.0,
            grid_power_w=1200.0,
            grid_power_observed_at=observed,
            battery_power_w=900.0,
            battery_power_observed_at=observed,
            # Positive successors reach 60% at 10 kW nameplate. A 100 A
            # fixture permits only 4.864 kW and would test an unsafe write.
            bms_max_discharge_current_a=200,
            bms_discharge_current_observed_at=observed,
        )

    async def running_controller(
        *clock_values: datetime,
    ) -> tuple[SupervisorActiveController, list[object]]:
        writes: list[object] = []

        async def persist(_record) -> None:
            return None

        async def dispatch(write) -> None:
            writes.append(write)

        controller = SupervisorActiveController(
            persist=persist,
            dispatch=dispatch,
            publish=lambda _record: None,
            clock=ScriptedClock(*clock_values),
        )
        await controller.async_initialize()
        await controller.async_reconcile(
            rce_frame(
                NOW,
                execution_source(NOW),
                deadline=deadline,
                floor=41.0,
                power=25.0,
                active=False,
                slot_end=NOW + timedelta(minutes=5),
            )
        )
        check(
            controller.record.state is ActiveState.WAITING_READBACK,
            "RCE start did not wait for physical FC03",
        )
        readback_at = NOW + timedelta(seconds=2)
        await controller.async_reconcile(
            rce_frame(
                readback_at,
                physical_old(readback_at),
                deadline=deadline,
                floor=41.0,
                power=25.0,
                active=False,
                transaction_pending=True,
                slot_end=NOW + timedelta(minutes=5),
            )
        )
        check(
            controller.record.state is ActiveState.EXECUTING,
            "old RCE target was not physically confirmed",
        )
        return controller, writes

    steady, steady_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
    )
    split_at = NOW + timedelta(seconds=4)
    split_source = physical_old(split_at)
    split_source = replace(
        split_source,
        grid_power_w=0.0,
        grid_power_observed_at=NOW - timedelta(seconds=10),
    )
    await steady.async_reconcile(
        rce_frame(
            split_at,
            split_source,
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        steady.record.state is ActiveState.EXECUTING
        and steady.record.reason is ExecutionReason.CONTINUATION_CONFIRMED
        and len(steady_writes) == 1,
        "an asynchronous GRID sample caused proven RCE discharge to roll back",
    )

    # A confirmed discharge may bridge one ordinary Wi-Fi telemetry drop, but
    # only until the immutable BAT-flow proof is 25 seconds old.  The raw EMS
    # generation is deliberately not refreshed in these replay frames.
    for age_seconds, should_continue in (
        (15.0, True),
        (16.0, True),
        (24.9, True),
        (25.0, True),
        (25.1, False),
        (30.0, False),
    ):
        replay, replay_writes = await running_controller(
            NOW + timedelta(milliseconds=100),
            NOW + timedelta(seconds=40),
        )
        replay_tx = replay.record.transaction
        assert replay_tx is not None
        proof_at = replay_tx.physical_verification.observed_at  # type: ignore[union-attr]
        replay_at = proof_at + timedelta(seconds=age_seconds)
        stale_source = replace(
            physical_old(replay_at),
            full_block_generation=11,
            full_block_generation_at=proof_at,
            battery_power_observed_at=proof_at,
        )
        replay_frame = rce_frame(
            replay_at,
            stale_source,
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
        replay._frame_resampler = lambda: replay_frame
        await replay.async_reconcile(replay_frame)
        if should_continue:
            check(
                replay.record.state is ActiveState.EXECUTING
                and len(replay_writes) == 1,
                f"confirmed RCE flow did not survive a {age_seconds:.1f}s Wi-Fi gap",
            )
            check(
                replay.execution_watchdog
                == (
                    replay_tx.transaction_id,
                    proof_at + timedelta(seconds=25),
                ),
                "same generation or timer frame renewed the physical watchdog: "
                f"{replay.execution_watchdog!r}",
            )
        else:
            check(
                replay.record.state is ActiveState.STOPPING
                and replay.record.reason
                is (
                    ExecutionReason.STALE_INPUTS
                    if age_seconds == 30.0
                    else ExecutionReason.PHYSICAL_UNAVAILABLE
                )
                and len(replay_writes) == 1,
                f"RCE did not issue exactly one STOP after a {age_seconds:.1f}s gap: "
                f"state={replay.record.state!r}, writes={len(replay_writes)}, "
                f"reason={replay.record.reason!r}",
            )

    simultaneous, simultaneous_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
    )
    simultaneous_tx = simultaneous.record.transaction
    assert simultaneous_tx is not None
    old_proof_at = simultaneous_tx.physical_verification.observed_at  # type: ignore[union-attr]
    simultaneous_at = old_proof_at + timedelta(seconds=25)
    await simultaneous.async_reconcile(
        rce_frame(
            simultaneous_at,
            replace(
                physical_old(simultaneous_at),
                full_block_generation=12,
                full_block_generation_at=simultaneous_at,
                battery_power_observed_at=simultaneous_at,
            ),
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        simultaneous.record.state is ActiveState.EXECUTING
        and len(simultaneous_writes) == 1
        and simultaneous.record.transaction is not None
        and simultaneous.record.transaction.physical_verification is not None
        and simultaneous.record.transaction.physical_verification.observed_at
        == simultaneous_at,
        "fresh sample simultaneous with the timer boundary did not win safely",
    )

    # Reproduce the measured 16247->16248 gap: only an already confirmed RCE
    # may retain the same physical FC03 block for less than 30 seconds.
    confirmed_ems_at = NOW + timedelta(seconds=1, milliseconds=500)
    delayed, delayed_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
        NOW + timedelta(seconds=33),
    )
    delayed_tx = delayed.record.transaction
    assert delayed_tx is not None
    delayed_tx_id = delayed_tx.transaction_id
    delayed_at = confirmed_ems_at + timedelta(seconds=24, milliseconds=318)
    delayed_source = replace(
        physical_old(delayed_at),
        full_block_generation=11,
        full_block_generation_at=confirmed_ems_at,
    )
    await delayed.async_reconcile(
        rce_frame(
            delayed_at,
            delayed_source,
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        delayed.record.state is ActiveState.EXECUTING
        and delayed.record.transaction is not None
        and delayed.record.transaction.transaction_id == delayed_tx_id
        and len(delayed_writes) == 1,
        "measured 24.318-second EMS gap caused a duplicate start or rollback",
    )
    check(
        delayed.execution_watchdog
        == (
            delayed_tx_id,
            confirmed_ems_at + timedelta(seconds=30),
        ),
        "EXECUTING RCE watchdog did not use the real EMS observation boundary",
    )
    boundary_at = confirmed_ems_at + timedelta(seconds=30)
    boundary_source = replace(
        physical_old(boundary_at),
        full_block_generation=11,
        full_block_generation_at=confirmed_ems_at,
    )
    await delayed.async_reconcile(
        rce_frame(
            boundary_at,
            boundary_source,
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        delayed.record.state is ActiveState.STOPPING
        and delayed.record.reason is ExecutionReason.STALE_INPUTS
        and len(delayed_writes) == 1,
        "exclusive 30-second boundary retained RCE or restored from stale FC03",
    )
    restore_at = boundary_at + timedelta(seconds=1)
    await delayed.async_reconcile(
        rce_frame(
            restore_at,
            physical_old(restore_at, generation=12),
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        delayed.record.state is ActiveState.RESTORING
        and len(delayed_writes) == 2
        and delayed_writes[-1].ems_block.mode is EmsMode.SELF_USE,
        "fresh FC03 did not resume exact safe restoration after the age stop",
    )
    restored_at = restore_at + timedelta(seconds=2)
    restored_source = execution_source(
        restored_at,
        physical_mode_code=0,
        full_block_generation=13,
        full_block_generation_at=restored_at - timedelta(milliseconds=500),
    )
    await delayed.async_reconcile(
        rce_frame(
            restored_at,
            restored_source,
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=False,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        delayed.record.state is ActiveState.IDLE
        and delayed.record.owner is ExecutionOwner.NONE,
        "safe restoration after an EMS-age stop lacked physical FC03 confirmation",
    )

    # A changed target may wait for a <=15-second write snapshot, but no target
    # is queued: the first fresh FC03 must dispatch only the latest successor.
    deferred, deferred_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
        NOW + timedelta(seconds=23),
    )
    deferred_at = confirmed_ems_at + timedelta(seconds=20)
    deferred_old_source = replace(
        physical_old(deferred_at),
        full_block_generation=11,
        full_block_generation_at=confirmed_ems_at,
    )
    await deferred.async_reconcile(
        rce_frame(
            deferred_at,
            deferred_old_source,
            deadline=deadline,
            floor=30.0,
            power=50.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        deferred.record.state is ActiveState.EXECUTING
        and len(deferred_writes) == 1,
        "retarget wrote or rolled back while the EMS snapshot was 15-30 seconds old",
    )
    latest_at = deferred_at + timedelta(seconds=1)
    latest_old_source = physical_old(latest_at, generation=12)
    await deferred.async_reconcile(
        rce_frame(
            latest_at,
            latest_old_source,
            deadline=deadline,
            floor=28.0,
            power=40.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        deferred.record.state is ActiveState.RETARGETING
        and len(deferred_writes) == 2
        and deferred_writes[-1].ems_block.mode is EmsMode.GRID_DISCHARGE
        and deferred_writes[-1].ems_block.force_discharge_soc_percent_4305 == 28.0
        and deferred_writes[-1].ems_block.maximum_discharge_power_percent_4306
        == 40.0,
        "fresh FC03 did not dispatch only the latest deferred RCE target",
    )

    controller, writes = await running_controller(
        NOW + timedelta(milliseconds=100),
        NOW + timedelta(seconds=3, milliseconds=100),
        NOW + timedelta(seconds=6, milliseconds=100),
    )
    original = controller.record.transaction
    assert original is not None and original.command_snapshot is not None
    original_baseline = original.command_snapshot
    retarget_at = NOW + timedelta(seconds=3)
    await controller.async_reconcile(
        rce_frame(
            retarget_at,
            physical_old(retarget_at),
            deadline=deadline,
            floor=30.0,
            power=50.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        controller.record.state is ActiveState.RETARGETING,
        "same-window target did not enter explicit RETARGETING",
    )
    retarget_transaction = controller.record.transaction
    assert retarget_transaction is not None
    check(
        controller.execution_watchdog
        == (
            retarget_transaction.transaction_id,
            NOW + timedelta(seconds=93, milliseconds=100),
        ),
        "retarget ACK watchdog did not retain its exact dispatch-time bound",
    )
    retarget_attrs = controller.recorder_attributes()
    check(
        retarget_attrs["active_state"] == "active_retargeting"
        and retarget_attrs["execution_phase"] == "waiting_readback",
        "retarget exposed an unsupported Aurora public phase",
    )
    check(
        len(writes) == 2
        and writes[0].ems_block.mode is EmsMode.GRID_DISCHARGE
        and writes[0].ems_block.force_discharge_soc_percent_4305 == 41.0
        and writes[0].ems_block.maximum_discharge_power_percent_4306 == 25.0
        and writes[1].ems_block.mode is EmsMode.GRID_DISCHARGE
        and writes[1].ems_block.force_discharge_soc_percent_4305 == 30.0
        and writes[1].ems_block.maximum_discharge_power_percent_4306 == 50.0,
        "RCE retarget dispatched anything other than direct old→latest full blocks",
    )
    new_at = NOW + timedelta(seconds=5)
    new_observed = new_at - timedelta(milliseconds=500)
    new_source = execution_source(
        new_at,
        physical_mode_code=5,
        full_block_generation=12,
        full_block_generation_at=new_observed,
        force_discharge_soc_percent=30.0,
        maximum_discharge_power_percent=50.0,
        bms_max_discharge_current_a=110.0,
        grid_power_w=1500.0,
        grid_power_observed_at=new_observed,
        battery_power_w=1100.0,
        battery_power_observed_at=new_observed,
    )
    await controller.async_reconcile(
        rce_frame(
            new_at,
            new_source,
            deadline=deadline,
            floor=30.0,
            power=50.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        controller.record.state is ActiveState.EXECUTING
        and controller.record.transaction is not None
        and controller.record.transaction.command_snapshot == original_baseline,
        "confirmed retarget lost the original rollback baseline",
    )
    await controller.async_reconcile(
        rce_frame(
            new_at + timedelta(milliseconds=100),
            new_source,
            deadline=deadline,
            floor=30.0,
            power=50.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        len(writes) == 2,
        "identical confirmed RCE target/revision dispatched a duplicate FC16",
    )
    reversed_at = NOW + timedelta(seconds=6)
    reversed_observed = reversed_at - timedelta(milliseconds=500)
    await controller.async_reconcile(
        rce_frame(
            reversed_at,
            replace(
                new_source,
                full_block_generation=13,
                full_block_generation_at=reversed_observed,
                battery_power_w=-900.0,
                battery_power_observed_at=reversed_observed,
            ),
            deadline=deadline,
            floor=30.0,
            power=50.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        controller.record.state is ActiveState.RESTORING
        and controller.record.owner is ExecutionOwner.RCE
        and len(writes) == 3
        and writes[-1].ems_block.mode is EmsMode.SELF_USE,
        "opposite battery flow after a confirmed retarget gained a second grace",
    )

    strict_retarget, strict_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
        NOW + timedelta(seconds=3, milliseconds=100),
        NOW + timedelta(seconds=5, milliseconds=100),
    )
    strict_target = rce_frame(
        retarget_at,
        physical_old(retarget_at),
        deadline=deadline,
        floor=30.0,
        power=50.0,
        active=True,
        slot_end=NOW + timedelta(minutes=5),
    )
    strict_retarget._frame_resampler = lambda: strict_target
    await strict_retarget.async_reconcile(strict_target)
    check(
        strict_retarget.record.state is ActiveState.RETARGETING
        and len(strict_writes) == 2,
        "strict retarget fixture did not dispatch the direct Mode 5 successor",
    )
    strict_at = NOW + timedelta(seconds=5)
    strict_observed = strict_at - timedelta(milliseconds=500)
    strict_negative = rce_frame(
        strict_at,
        execution_source(
            strict_at,
            physical_mode_code=5,
            full_block_generation=12,
            full_block_generation_at=strict_observed,
            force_discharge_soc_percent=30.0,
            maximum_discharge_power_percent=50.0,
            bms_max_discharge_current_a=110.0,
            battery_power_w=-900.0,
            battery_power_observed_at=strict_observed,
        ),
        deadline=deadline,
        floor=30.0,
        power=50.0,
        active=True,
        slot_end=NOW + timedelta(minutes=5),
    )
    strict_retarget._frame_resampler = lambda: strict_negative
    await strict_retarget.async_reconcile(strict_negative)
    check(
        strict_retarget.record.state is ActiveState.RESTORING
        and strict_retarget.record.owner is ExecutionOwner.RCE
        and len(strict_writes) == 3
        and strict_writes[-1].ems_block.mode is EmsMode.SELF_USE,
        "RETARGETING with matching FC03 and opposite BAT gained initial grace",
    )

    threshold_only, threshold_only_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
        NOW + timedelta(seconds=3, milliseconds=100),
    )
    threshold_only_original = threshold_only.record.transaction
    assert (
        threshold_only_original is not None
        and threshold_only_original.command_snapshot is not None
    )
    threshold_only_baseline = threshold_only_original.command_snapshot
    threshold_only_at = NOW + timedelta(seconds=3)
    threshold_only_frame = rce_frame(
        threshold_only_at,
        physical_old(threshold_only_at),
        deadline=deadline,
        floor=30.0,
        power=25.0,
        active=True,
        slot_end=NOW + timedelta(minutes=5),
    )
    await threshold_only.async_reconcile(threshold_only_frame)
    check(
        threshold_only.record.state is ActiveState.RETARGETING
        and len(threshold_only_writes) == 2
        and all(
            write.ems_block.mode is EmsMode.GRID_DISCHARGE
            for write in threshold_only_writes
        )
        and threshold_only_writes[-1].ems_block.force_discharge_soc_percent_4305
        == 30.0
        and threshold_only_writes[-1].ems_block.maximum_discharge_power_percent_4306
        == 25.0,
        "threshold-only RCE retarget did not dispatch one direct full Mode 5 block",
    )
    threshold_only_confirmed_at = NOW + timedelta(seconds=5)
    threshold_only_observed_at = threshold_only_confirmed_at - timedelta(
        milliseconds=500
    )
    threshold_only_source = execution_source(
        threshold_only_confirmed_at,
        physical_mode_code=5,
        full_block_generation=12,
        full_block_generation_at=threshold_only_observed_at,
        force_discharge_soc_percent=30.0,
        maximum_discharge_power_percent=25.0,
        grid_power_w=1200.0,
        grid_power_observed_at=threshold_only_observed_at,
        battery_power_w=900.0,
        battery_power_observed_at=threshold_only_observed_at,
    )
    threshold_only_confirmed_frame = rce_frame(
        threshold_only_confirmed_at,
        threshold_only_source,
        deadline=deadline,
        floor=30.0,
        power=25.0,
        active=True,
        slot_end=NOW + timedelta(minutes=5),
    )
    await threshold_only.async_reconcile(threshold_only_confirmed_frame)
    check(
        threshold_only.record.state is ActiveState.EXECUTING
        and threshold_only.record.transaction is not None
        and threshold_only.record.transaction.command_snapshot
        == threshold_only_baseline,
        "confirmed threshold-only retarget lost the original rollback baseline",
    )
    await threshold_only.async_reconcile(threshold_only_confirmed_frame)
    check(
        len(threshold_only_writes) == 2,
        "identical confirmed threshold-only target dispatched a duplicate FC16",
    )

    changed, changed_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
        NOW + timedelta(seconds=3, milliseconds=200),
    )
    target_b = rce_frame(
        retarget_at,
        physical_old(retarget_at),
        deadline=deadline,
        floor=30.0,
        power=50.0,
        active=True,
        slot_end=NOW + timedelta(minutes=5),
    )
    target_c = rce_frame(
        retarget_at + timedelta(milliseconds=10),
        physical_old(retarget_at + timedelta(milliseconds=10)),
        deadline=deadline,
        floor=28.0,
        power=60.0,
        active=True,
        slot_end=NOW + timedelta(minutes=5),
    )
    # Model the real adapter race: raw C has arrived while its 100 ms planner
    # projection is still pending, so stale computed B must not be returned.
    changed._frame_resampler = lambda: None
    changed._retarget_replan_pending = lambda: True
    await changed.async_reconcile(target_b)
    check(
        len(changed_writes) == 1
        and changed.record.state is ActiveState.EXECUTING
        and changed.record.owner is ExecutionOwner.RCE
        and changed.record.transaction is not None
        and changed.record.transaction.intent.command.ems_block is not None
        and changed.record.transaction.intent.command.ems_block.force_discharge_soc_percent_4305
        == 41.0
        and changed.record.transaction.intent.command.ems_block.maximum_discharge_power_percent_4306
        == 25.0,
        "zero-write target churn did not resume the confirmed predecessor",
    )
    changed._retarget_replan_pending = lambda: False
    changed._frame_resampler = lambda: target_c
    await changed.async_reconcile(target_c)
    check(
        len(changed_writes) == 2
        and changed.record.state is ActiveState.RETARGETING
        and changed_writes[-1].ems_block.mode is EmsMode.GRID_DISCHARGE
        and changed_writes[-1].ems_block.force_discharge_soc_percent_4305 == 28.0
        and changed_writes[-1].ems_block.maximum_discharge_power_percent_4306 == 60.0
        and all(
            write.ems_block.force_discharge_soc_percent_4305 != 30.0
            for write in changed_writes
        ),
        "pre-dispatch B→C churn did not dispatch only the latest successor",
    )
    changed_confirm_at = NOW + timedelta(seconds=5)
    changed_confirm_observed = changed_confirm_at - timedelta(milliseconds=500)
    changed_confirm_source = execution_source(
        changed_confirm_at,
        physical_mode_code=5,
        full_block_generation=12,
        full_block_generation_at=changed_confirm_observed,
        force_discharge_soc_percent=28.0,
        maximum_discharge_power_percent=60.0,
        bms_max_discharge_current_a=130.0,
        grid_power_w=1600.0,
        grid_power_observed_at=changed_confirm_observed,
        battery_power_w=1200.0,
        battery_power_observed_at=changed_confirm_observed,
    )
    changed_confirm_frame = rce_frame(
        changed_confirm_at,
        changed_confirm_source,
        deadline=deadline,
        floor=28.0,
        power=60.0,
        active=True,
        slot_end=NOW + timedelta(minutes=5),
    )
    changed._frame_resampler = lambda: changed_confirm_frame
    await changed.async_reconcile(changed_confirm_frame)
    await changed.async_reconcile(changed_confirm_frame)
    check(
        changed.record.state is ActiveState.EXECUTING
        and len(changed_writes) == 2,
        "confirmed latest successor or identical refresh caused a duplicate FC16",
    )

    def without_candidate_latch(current: ActiveFrame) -> ActiveFrame:
        return replace(
            current,
            candidates=tuple(
                replace(item, active_latched=False)
                if item.policy_id is PolicyId.RCE
                else item
                for item in current.candidates
            ),
        )

    for label, unsafe in (
        (
            "source",
            replace(target_b, rce=replace(target_b.rce, active_latched=False)),
        ),
        ("candidate", without_candidate_latch(target_b)),
    ):
        rejected, rejected_writes = await running_controller(
            NOW + timedelta(milliseconds=100),
        )
        rejected._frame_resampler = lambda unsafe=unsafe: unsafe
        await rejected.async_reconcile(target_b)
        check(
            len(rejected_writes) == 1
            and rejected.record.state is ActiveState.STOPPING
            and rejected.record.owner is ExecutionOwner.RCE,
            f"predispatch {label}-latch loss retained retarget authority",
        )

    post_send_at = NOW + timedelta(seconds=4)
    post_send_frame = rce_frame(
        post_send_at,
        physical_old(post_send_at),
        deadline=deadline,
        floor=30.0,
        power=50.0,
        active=True,
        slot_end=NOW + timedelta(minutes=5),
    )
    for label, unsafe in (
        (
            "source",
            replace(
                post_send_frame,
                rce=replace(post_send_frame.rce, active_latched=False),
            ),
        ),
        ("candidate", without_candidate_latch(post_send_frame)),
    ):
        rejected, rejected_writes = await running_controller(
            NOW + timedelta(milliseconds=100),
            NOW + timedelta(seconds=3, milliseconds=100),
            NOW + timedelta(seconds=4, milliseconds=100),
        )
        rejected._frame_resampler = lambda: target_b
        await rejected.async_reconcile(target_b)
        check(
            rejected.record.state is ActiveState.RETARGETING
            and len(rejected_writes) == 2,
            f"post-send {label}-latch fixture did not enter RETARGETING",
        )
        rejected._frame_resampler = lambda unsafe=unsafe: unsafe
        await rejected.async_reconcile(unsafe)
        check(
            rejected.record.state is ActiveState.RESTORING
            and rejected.record.owner is ExecutionOwner.RCE
            and len(rejected_writes) == 3
            and rejected_writes[-1].ems_block.mode is EmsMode.SELF_USE,
            f"post-send {label}-latch loss did not begin fail-closed restore",
        )

    churn, churn_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
        NOW + timedelta(seconds=184, milliseconds=100),
    )
    hold_anchor: tuple[str, datetime, float] | None = None
    for offset, control_ready, reserve_ready in (
        (3, True, True),
        (30, False, True),
        (60, True, False),
        (182, False, False),
    ):
        at = NOW + timedelta(seconds=offset)
        pending_frame = rce_frame(
            at,
            physical_old(at),
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=True,
            observed_at=at,
            result_current=False,
            recalculation_pending=True,
            control_data_ready=control_ready,
            reserve_ready=reserve_ready,
            slot_end=NOW + timedelta(minutes=5),
            start_eligible=False,
        )
        pending_candidate = next(
            item
            for item in pending_frame.candidates
            if item.policy_id is PolicyId.RCE
        )
        check(
            pending_candidate.requested_action is RequestedAction.RCE_EXPORT
            and pending_candidate.local_hard_stop == (not (control_ready and reserve_ready))
            and not pending_candidate.result_current
            and pending_candidate.recalculation_pending,
            "real pending builder fixture did not retain active RCE_EXPORT shape",
        )
        await churn.async_reconcile(pending_frame)
        check(
            churn.record.state is ActiveState.EXECUTING
            and len(churn_writes) == 1,
            f"pending helper order revoked RCE hold ({control_ready}/{reserve_ready})",
        )
        if hold_anchor is None:
            hold_anchor = churn._rce_execution_hold
            check(
                hold_anchor is not None
                and hold_anchor[1] == NOW + timedelta(seconds=3)
                and hold_anchor[2] == 180.0,
                "first explicit recalculation did not anchor its 180-second hold",
            )
        else:
            check(
                churn._rce_execution_hold == hold_anchor,
                "planner churn renewed the controller recalculation hold",
            )
    expired_at = NOW + timedelta(seconds=183, microseconds=1)
    await churn.async_reconcile(
        rce_frame(
            expired_at,
            physical_old(expired_at),
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=True,
            observed_at=expired_at,
            result_current=False,
            recalculation_pending=True,
            control_data_ready=False,
            reserve_ready=False,
            slot_end=NOW + timedelta(minutes=5),
            start_eligible=False,
        )
    )
    check(
        churn.record.state is ActiveState.RESTORING
        and len(churn_writes) == 2
        and churn_writes[-1].ems_block.mode is EmsMode.SELF_USE,
        "planner churn renewed the 180-second recalculation hold indefinitely",
    )

    cohort, cohort_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
        NOW + timedelta(seconds=65, milliseconds=100),
    )
    cohort_original = cohort.record.transaction
    assert cohort_original is not None
    cohort_anchor: tuple[str, datetime, float] | None = None
    # A pending snapshot may again project the acknowledged predecessor.  It
    # must not expand the short cohort already opened by a committed successor.
    for offset, result_current in ((3, False), (4, True), (5, False), (63, False)):
        at = NOW + timedelta(seconds=offset)
        await cohort.async_reconcile(
            rce_frame(
                at,
                physical_old(at),
                deadline=deadline,
                floor=30.0 if result_current else 41.0,
                power=50.0 if result_current else 25.0,
                active=True,
                observed_at=at,
                result_current=result_current,
                recalculation_pending=not result_current,
                control_data_ready=False,
                reserve_ready=False,
                start_eligible=False,
                slot_end=NOW + timedelta(minutes=5),
            )
        )
        current = cohort.record.transaction
        check(
            cohort.record.state is ActiveState.EXECUTING
            and current is not None
            and current.transaction_id == cohort_original.transaction_id
            and current.command_sent_at == cohort_original.command_sent_at
            and current.deadline == deadline
            and current.command_snapshot == cohort_original.command_snapshot
            and len(cohort_writes) == 1,
            "180/60-second hold transition wrote a successor or renewed the original lease",
        )
        if offset == 3:
            check(
                cohort._rce_execution_hold
                == (cohort_original.transaction_id, at, 180.0),
                "explicit replan did not start the 180-second hold",
            )
        elif offset == 4:
            cohort_anchor = (cohort_original.transaction_id, at, 60.0)
            check(
                cohort._rce_execution_hold == cohort_anchor,
                "new committed successor did not start its separate 60-second cohort",
            )
        else:
            check(
                cohort._rce_execution_hold == cohort_anchor,
                "return to pending renewed or expanded the 60-second cohort",
            )
    cohort_expired_at = NOW + timedelta(seconds=64, microseconds=1)
    await cohort.async_reconcile(
        rce_frame(
            cohort_expired_at,
            physical_old(cohort_expired_at),
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=True,
            observed_at=cohort_expired_at,
            result_current=False,
            recalculation_pending=True,
            control_data_ready=False,
            reserve_ready=False,
            start_eligible=False,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        cohort.record.state is ActiveState.RESTORING
        and cohort.record.owner is ExecutionOwner.RCE
        and len(cohort_writes) == 2
        and cohort_writes[-1].ems_block.mode is EmsMode.SELF_USE,
        "pending after a committed successor outlived the anchored 60-second cohort",
    )

    limited, limited_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
        NOW + timedelta(seconds=4, milliseconds=100),
    )
    limited_at = NOW + timedelta(seconds=3)
    limited_source = replace(
        physical_old(limited_at),
        bms_max_discharge_current_a=10.0,
    )
    await limited.async_reconcile(
        rce_frame(
            limited_at,
            limited_source,
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=True,
            observed_at=limited_at,
            result_current=False,
            recalculation_pending=True,
            control_data_ready=False,
            reserve_ready=True,
            slot_end=NOW + timedelta(minutes=5),
            start_eligible=False,
        )
    )
    check(
        limited.record.state is ActiveState.RESTORING
        and len(limited_writes) == 2
        and limited_writes[-1].ems_block.mode is EmsMode.SELF_USE,
        "positive but insufficient live BMS cap retained the old RCE command",
    )

    # Recreate the true WAITING chronology on a separate controller because
    # running_controller confirms its first command by design.
    pending_writes: list[object] = []

    async def persist_pending(_record) -> None:
        return None

    pending = SupervisorActiveController(
        persist=persist_pending,
        dispatch=lambda write: _append_async(pending_writes, write),
        publish=lambda _record: None,
        clock=ScriptedClock(
            NOW + timedelta(milliseconds=100),
            NOW + timedelta(seconds=11, milliseconds=100),
            NOW + timedelta(seconds=13, milliseconds=100),
        ),
    )
    await pending.async_initialize()
    await pending.async_reconcile(
        rce_frame(
            NOW,
            execution_source(NOW),
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=False,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    pending_tx = pending.record.transaction
    assert pending_tx is not None
    sent_at = pending_tx.command_sent_at
    assert sent_at is not None
    initial_ack_watchdog = pending.execution_watchdog
    check(
        initial_ack_watchdog
        == (
            pending_tx.transaction_id,
            sent_at + timedelta(seconds=controller_module.COMMAND_ACK_TIMEOUT_SECONDS),
        ),
        "initial FC03 ACK gained no exact no-event watchdog",
    )
    pending_at = NOW + timedelta(seconds=7)
    await pending.async_reconcile(
        rce_frame(
            pending_at,
            execution_source(
                pending_at,
                full_block_generation=10,
                full_block_generation_at=pending_at - timedelta(milliseconds=500),
            ),
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=False,
            transaction_pending=True,
            observed_at=pending_at,
            result_current=False,
            recalculation_pending=True,
            control_data_ready=None,
            reserve_ready=None,
            slot_end=NOW + timedelta(minutes=5),
            pending_no_action=True,
        )
    )
    check(
        pending.record.state is ActiveState.WAITING_READBACK
        and pending.record.transaction is not None
        and pending.record.transaction.command_sent_at == sent_at
        and pending.record.transaction.deadline == deadline,
        "pending RCE recalculation stopped or renewed the in-flight ACK lease",
    )
    check(
        pending.execution_watchdog == initial_ack_watchdog,
        "pending RCE recalculation renewed the initial ACK watchdog",
    )
    for offset, floor, power in (
        (8, 30.0, 50.0),
        (9, 29.0, 55.0),
        (10, 28.0, 60.0),
    ):
        at = NOW + timedelta(seconds=offset)
        await pending.async_reconcile(
            rce_frame(
                at,
                execution_source(
                    at,
                    full_block_generation=10,
                    full_block_generation_at=at - timedelta(milliseconds=500),
                ),
                deadline=deadline,
                floor=floor,
                power=power,
                active=False,
                transaction_pending=True,
                slot_end=NOW + timedelta(minutes=5),
            )
        )
        check(
            pending.record.state is ActiveState.WAITING_READBACK
            and len(pending_writes) == 1
            and pending.execution_watchdog == initial_ack_watchdog,
            "a successor target was written before the original FC03 acknowledgement",
        )
    latest_at = NOW + timedelta(seconds=11)
    latest_frame = rce_frame(
        latest_at,
        physical_old(latest_at),
        deadline=deadline,
        floor=28.0,
        power=60.0,
        active=True,
        transaction_pending=True,
        slot_end=NOW + timedelta(minutes=5),
    )
    await pending.async_reconcile(latest_frame)
    check(
        pending.record.state is ActiveState.RETARGETING
        and pending.record.owner is ExecutionOwner.RCE
        and len(pending_writes) == 2
        and [write.ems_block.mode for write in pending_writes]
        == [EmsMode.GRID_DISCHARGE, EmsMode.GRID_DISCHARGE],
        "old target acknowledgement did not autonomously dispatch latest RCE target",
    )
    pending_retarget_tx = pending.record.transaction
    assert pending_retarget_tx is not None
    pending_retarget_sent_at = pending_retarget_tx.command_sent_at
    assert pending_retarget_sent_at is not None
    check(
        pending.record.state is ActiveState.RETARGETING
        and len(pending_writes) == 2
        and pending_writes[-1].ems_block.mode is EmsMode.GRID_DISCHARGE
        and pending_writes[-1].ems_block.force_discharge_soc_percent_4305 == 28.0
        and pending_writes[-1].ems_block.maximum_discharge_power_percent_4306 == 60.0,
        "WAITING latest-wins dispatched an intermediate target or neutral restore",
    )
    check(
        pending.execution_watchdog
        == (
            pending_retarget_tx.transaction_id,
            pending_retarget_sent_at
            + timedelta(seconds=controller_module.COMMAND_ACK_TIMEOUT_SECONDS),
        )
        and pending_retarget_tx.deadline == deadline,
        "retarget ACK watchdog extended or replaced the original run deadline",
    )
    pending_new_at = NOW + timedelta(seconds=13)
    pending_new_observed = pending_new_at - timedelta(milliseconds=500)
    pending_new_source = execution_source(
        pending_new_at,
        physical_mode_code=5,
        full_block_generation=12,
        full_block_generation_at=pending_new_observed,
        force_discharge_soc_percent=28.0,
        maximum_discharge_power_percent=60.0,
        bms_max_discharge_current_a=130.0,
        grid_power_w=1600.0,
        grid_power_observed_at=pending_new_observed,
        battery_power_w=1200.0,
        battery_power_observed_at=pending_new_observed,
    )
    await pending.async_reconcile(
        rce_frame(
            pending_new_at,
            pending_new_source,
            deadline=deadline,
            floor=30.0,
            power=25.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    pending_latest_tx = pending.record.transaction
    assert pending_latest_tx is not None
    check(
        pending.record.state is ActiveState.RETARGETING
        and pending.record.owner is ExecutionOwner.RCE
        and len(pending_writes) == 3
        and [write.ems_block.mode for write in pending_writes]
        == [
            EmsMode.GRID_DISCHARGE,
            EmsMode.GRID_DISCHARGE,
            EmsMode.GRID_DISCHARGE,
        ]
        and pending_writes[-1].ems_block.force_discharge_soc_percent_4305 == 30.0
        and pending_writes[-1].ems_block.maximum_discharge_power_percent_4306
        == 25.0
        and pending_writes[-1].snapshot_generation == 12
        and pending_latest_tx.transaction_id == pending_retarget_tx.transaction_id
        and pending_latest_tx.deadline == deadline
        and pending_latest_tx.command_snapshot
        == pending_retarget_tx.command_snapshot,
        "FC03 confirmation of C did not autonomously dispatch exact latest D",
    )
    pending_latest_at = NOW + timedelta(seconds=15)
    pending_latest_observed = pending_latest_at - timedelta(milliseconds=500)
    await pending.async_reconcile(
        rce_frame(
            pending_latest_at,
            execution_source(
                pending_latest_at,
                physical_mode_code=5,
                full_block_generation=13,
                full_block_generation_at=pending_latest_observed,
                force_discharge_soc_percent=30.0,
                maximum_discharge_power_percent=25.0,
                grid_power_w=1400.0,
                grid_power_observed_at=pending_latest_observed,
                battery_power_w=1000.0,
                battery_power_observed_at=pending_latest_observed,
            ),
            deadline=deadline,
            floor=30.0,
            power=25.0,
            active=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        pending.record.state is ActiveState.EXECUTING
        and pending.record.owner is ExecutionOwner.RCE
        and len(pending_writes) == 3
        and pending.record.transaction is not None
        and pending.record.transaction.transaction_id
        == pending_retarget_tx.transaction_id
        and pending.record.transaction.deadline == deadline
        and pending.record.transaction.command_snapshot
        == pending_retarget_tx.command_snapshot,
        "D did not require and consume its separate newer FC03 confirmation",
    )

    initial_timeout_writes: list[object] = []
    initial_timeout = SupervisorActiveController(
        persist=lambda _record: _done(),
        dispatch=lambda write: _append_async(initial_timeout_writes, write),
        publish=lambda _record: None,
        clock=ScriptedClock(
            NOW + timedelta(milliseconds=100),
            NOW + timedelta(seconds=93, milliseconds=200),
        ),
    )
    await initial_timeout.async_initialize()
    initial_timeout_frame = rce_frame(
        NOW,
        execution_source(NOW),
        deadline=deadline,
        floor=41.0,
        power=25.0,
        active=False,
        slot_end=NOW + timedelta(minutes=5),
    )
    await initial_timeout.async_reconcile(initial_timeout_frame)
    initial_boundary = initial_timeout.execution_watchdog
    assert initial_boundary is not None
    initial_expired_at = initial_boundary[1] + timedelta(microseconds=1)
    await initial_timeout.async_reconcile(
        rce_frame(
            initial_expired_at,
            execution_source(
                initial_expired_at,
                full_block_generation_at=NOW,
            ),
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=False,
            transaction_pending=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        initial_timeout.record.state is ActiveState.FAULT
        and initial_timeout.record.owner is ExecutionOwner.RCE
        and initial_timeout.record.reason is ExecutionReason.DEADLINE_REACHED
        and initial_timeout.record.transaction is not None
        and initial_timeout.record.transaction.rollback_status
        is RollbackStatus.PENDING
        and len(initial_timeout_writes) == 1,
        "stale timeout frame overwrote the original fault or dispatched restore",
    )
    initial_fresh_at = initial_expired_at + timedelta(seconds=1)
    await initial_timeout.async_reconcile(
        rce_frame(
            initial_fresh_at,
            execution_source(initial_fresh_at, full_block_generation=11),
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=False,
            transaction_pending=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        initial_timeout.record.owner is ExecutionOwner.RCE
        and len(initial_timeout_writes) == 1,
        "first still-neutral FC03 released the owner or bypassed firmware barrier",
    )
    second_fresh_at = initial_fresh_at + timedelta(seconds=1)
    await initial_timeout.async_reconcile(
        rce_frame(
            second_fresh_at,
            execution_source(second_fresh_at, full_block_generation=12),
            deadline=deadline,
            floor=41.0,
            power=25.0,
            active=False,
            transaction_pending=True,
            slot_end=NOW + timedelta(minutes=5),
        )
    )
    check(
        initial_timeout.record.state is ActiveState.RESTORING
        and initial_timeout.record.owner is ExecutionOwner.RCE
        and initial_timeout.record.transaction is not None
        and initial_timeout.record.transaction.rollback_status
        is RollbackStatus.PENDING
        and len(initial_timeout_writes) == 2
        and initial_timeout_writes[-1].ems_block.mode is EmsMode.SELF_USE,
        "fresh FC03 did not resume the pending restore after command timeout",
    )

    retarget_timeout, retarget_timeout_writes = await running_controller(
        NOW + timedelta(milliseconds=100),
        NOW + timedelta(seconds=3, milliseconds=100),
        NOW + timedelta(seconds=93, milliseconds=200),
    )
    retarget_timeout._frame_resampler = lambda: target_b
    await retarget_timeout.async_reconcile(target_b)
    retarget_boundary = retarget_timeout.execution_watchdog
    assert retarget_boundary is not None
    retarget_expired_at = retarget_boundary[1] + timedelta(microseconds=1)
    retarget_timeout_frame = rce_frame(
        retarget_expired_at,
        physical_old(retarget_expired_at),
        deadline=deadline,
        floor=30.0,
        power=50.0,
        active=True,
        slot_end=NOW + timedelta(minutes=5),
    )
    retarget_timeout._frame_resampler = lambda: retarget_timeout_frame
    await retarget_timeout.async_reconcile(retarget_timeout_frame)
    check(
        retarget_timeout.record.state is ActiveState.RESTORING
        and retarget_timeout.record.owner is ExecutionOwner.RCE
        and len(retarget_timeout_writes) == 3
        and retarget_timeout_writes[-1].ems_block.mode is EmsMode.SELF_USE,
        "retarget ACK timeout required an external event or skipped restore",
    )


async def _append_async(target: list[object], value: object) -> None:
    target.append(value)


async def _done() -> None:
    return None


async def main_async() -> None:
    await test_full_lifecycle()
    await test_rce_initial_physical_effect_uses_fixed_timeout()
    await test_rce_initial_pending_uses_real_builder()
    await test_master_stop_and_persist_failure()
    await test_authorization_freshness_pending_and_block_retry()
    await test_bounded_dispatch_timeout()
    await test_actual_dispatch_completion_is_readback_boundary()
    await test_invalid_dispatch_clock_fails_closed()
    await test_prewrite_resample_blocks_stale_start_and_restore()
    await test_manual_proxy_dispatch_is_serialized_with_active_reconcile()
    await test_same_window_rce_retarget_and_bounded_replan_holds()


def main() -> None:
    asyncio.run(main_async())
    print(f"Supervisor Active controller: PASS ({CHECKS} checks)")


if __name__ == "__main__":
    main()
