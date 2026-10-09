"""X03: one Active controller across RCE, RCEm, tariff and fault recovery."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

import test_supervisor_active_controller as a
import test_supervisor_accounting_runtime as b


async def main() -> None:
    now = a.NOW
    writes = []
    records = []
    clock_at = now
    fail_restore = False

    async def persist(record):
        records.append(record)

    async def dispatch(write):
        writes.append(write)
        if (fail_restore and write.ems_block is not None
            and write.ems_block.mode is a.EmsMode.SELF_USE):
            raise TimeoutError("restore outcome unknown")

    controller = a.SupervisorActiveController(
        persist=persist, dispatch=dispatch, publish=lambda _record: None,
        clock=lambda: clock_at,
    )
    await controller.async_initialize()
    deadline = now + timedelta(minutes=30)

    def rce(at, source, *, active, run_end=None, slot_end=None):
        return a.rce_frame(
            at, source, deadline=run_end or deadline, floor=41.0, power=25.0,
            active=active, slot_end=slot_end or now + timedelta(minutes=5),
        )

    await controller.async_reconcile(rce(now, a.execution_source(now), active=False))
    assert controller.record.state is a.ActiveState.WAITING_READBACK
    rce_tx = controller.record.transaction.transaction_id
    at = now + timedelta(seconds=2)
    observed = at - timedelta(milliseconds=500)
    rce_physical = a.execution_source(
        at, physical_mode_code=5, full_block_generation=11,
        full_block_generation_at=observed,
        force_discharge_soc_percent=41.0,
        maximum_discharge_power_percent=25.0,
        grid_power_w=1200.0, grid_power_observed_at=observed,
        battery_power_w=900.0, battery_power_observed_at=observed,
        bms_max_discharge_current_a=200,
        bms_discharge_current_observed_at=observed,
    )
    await controller.async_reconcile(rce(at, rce_physical, active=False))
    assert controller.record.state is a.ActiveState.EXECUTING
    assert controller.record.owner is a.ExecutionOwner.RCE

    def emergency_frame(at, source, *, active, pending=False):
        base = a.rce_frame(
            at, source, deadline=deadline, floor=41.0, power=25.0,
            active=active, transaction_pending=pending,
            slot_end=now + timedelta(minutes=5),
        )
        rcm = b.RcmSourceSnapshot(
            observed_at=at - timedelta(milliseconds=500),
            allowed_by_user=True, enabled=True,
            result_current=True, recalculation_pending=False,
            input_revision=7, live_emergency=True,
            emergency_action_ready=True, prediction_ready=True,
            action=b.RcmAction.ABSORB_PV, risk_window_active=True,
            absorb_active=False, export_active=False,
            pre_discharge_active=False,
            charge_path_locally_valid=True,
            direct_register_topology_allowed=True,
            recommended_charge_limit_percent=60,
            recommended_charge_power_kw=2.0,
        )
        emergency = b.build_rcm_candidate(rcm, now=at)
        candidates = (base.candidates[0], base.candidates[1], emergency)
        decision = a.arbitrate_supervisor(
            mode=a.SupervisorMode.ACTIVE,
            profile=a.SupervisorProfile.BALANCED,
            context=base.context, candidates=candidates, now=at,
        )
        return replace(base, candidates=candidates, decision=decision, rcm=rcm)

    def rcm_frame(at, source, *, active=False, enabled=True):
        base = a.frame(at, source, enabled=False)
        context = replace(
            base.context,
            owner_kind=a.OwnerKind.RCM if active else a.OwnerKind.NONE,
        )
        rcm = b.RcmSourceSnapshot(
            observed_at=at - timedelta(milliseconds=500),
            allowed_by_user=True, enabled=enabled,
            result_current=True, recalculation_pending=False,
            input_revision=7, live_emergency=enabled,
            emergency_action_ready=True, prediction_ready=True,
            action=b.RcmAction.ABSORB_PV, risk_window_active=enabled,
            absorb_active=active, export_active=False,
            pre_discharge_active=False,
            charge_path_locally_valid=True,
            direct_register_topology_allowed=True,
            recommended_charge_limit_percent=60,
            recommended_charge_power_kw=2.0,
        )
        candidates = (
            base.candidates[0], base.candidates[1],
            b.build_rcm_candidate(rcm, now=at),
        )
        decision = a.arbitrate_supervisor(
            mode=a.SupervisorMode.ACTIVE,
            profile=a.SupervisorProfile.BALANCED,
            context=context, candidates=candidates, now=at,
        )
        return replace(base, candidates=candidates, decision=decision,
            context=context, rcm=rcm)

    at = now + timedelta(seconds=3)
    intervening = emergency_frame(at, replace(rce_physical,
        full_block_generation_at=at - timedelta(milliseconds=500),
        bms_discharge_current_observed_at=at - timedelta(milliseconds=500),
    ), active=True)
    decision = intervening.decision
    assert decision.selected_policy is a.PolicyId.RCM, decision
    clock_at = at + timedelta(milliseconds=100)
    await controller.async_reconcile(intervening)
    assert controller.record.state is a.ActiveState.RESTORING
    assert controller.record.owner is a.ExecutionOwner.RCE
    assert len(writes) == 2 and writes[-1].ems_block.mode is a.EmsMode.SELF_USE
    assert controller.record.transaction.transaction_id == rce_tx

    at = now + timedelta(seconds=4)
    neutral = a.execution_source(
        at, full_block_generation=12,
        full_block_generation_at=at - timedelta(milliseconds=500),
    )
    await controller.async_reconcile(emergency_frame(
        at, neutral, active=False, pending=True,
    ))
    assert controller.record.state is a.ActiveState.IDLE
    assert controller.record.owner is a.ExecutionOwner.NONE
    assert controller.record.last_transaction.transaction_id == rce_tx

    at = now + timedelta(seconds=5)
    clock_at = at + timedelta(milliseconds=100)
    await controller.async_reconcile(rcm_frame(at, a.execution_source(at)))
    assert controller.record.state is a.ActiveState.WAITING_READBACK
    assert controller.record.owner is a.ExecutionOwner.RCM
    rcm_tx = controller.record.transaction.transaction_id
    assert len(writes) == 3 and writes[-1].family is b.AtomicWriteFamily.BATTERY_CHARGE_LIMIT

    at = now + timedelta(seconds=7)
    observed = at - timedelta(milliseconds=500)
    rcm_physical = a.execution_source(
        at, battery_charge_limit_generation=13,
        battery_charge_limit_percent=60,
        battery_charge_limit_generation_at=observed,
        pv_power_w=4000, pv_power_observed_at=observed,
        load_power_w=1000, load_power_observed_at=observed,
        battery_power_w=-2000, battery_power_observed_at=observed,
    )
    await controller.async_reconcile(rcm_frame(at, rcm_physical, active=True))
    assert controller.record.state is a.ActiveState.EXECUTING
    assert controller.record.owner is a.ExecutionOwner.RCM

    at = now + timedelta(seconds=8)
    clock_at = at + timedelta(milliseconds=100)
    await controller.async_reconcile(rcm_frame(
        at, replace(rcm_physical,
            battery_charge_limit_generation_at=at - timedelta(milliseconds=500),
        ), enabled=False,
    ))
    assert controller.record.state is a.ActiveState.RESTORING
    assert controller.record.owner is a.ExecutionOwner.RCM
    assert len(writes) == 4 and writes[-1].family is b.AtomicWriteFamily.BATTERY_CHARGE_LIMIT
    at = now + timedelta(seconds=10)
    await controller.async_reconcile(rcm_frame(
        at, a.execution_source(
            at, battery_charge_limit_generation=14,
            battery_charge_limit_percent=80,
            battery_charge_limit_generation_at=at - timedelta(milliseconds=500),
        ), enabled=False,
    ))
    assert controller.record.state is a.ActiveState.IDLE
    assert controller.record.owner is a.ExecutionOwner.NONE
    assert controller.record.last_transaction.transaction_id == rcm_tx

    def tariff_source(at, **overrides):
        values = dict(
            battery_soc_percent=24.0, self_use_soc_percent=25.0,
            gcf_enable_code=1, effective_export_limit_percent=0.0,
            direct_259_execution_ready=False,
            direct_306_execution_ready=False,
            battery_charge_limit_generation=None,
            battery_charge_limit_generation_at=None,
            battery_charge_limit_percent=None,
        )
        values.update(overrides)
        return b.source(at, **values)

    at = now + timedelta(seconds=11)
    clock_at = at + timedelta(milliseconds=100)
    tariff_start = b.policy_local_frame(
        at, tariff_source(at), tariff_active=False,
        transaction_pending=False,
    )
    assert tariff_start.decision.selected_policy is a.PolicyId.TARIFF
    await controller.async_reconcile(tariff_start)
    assert controller.record.state is a.ActiveState.WAITING_READBACK
    assert controller.record.owner is a.ExecutionOwner.TARIFF
    assert len(writes) == 5 and writes[-1].ems_block.mode is a.EmsMode.GRID_CHARGE
    tariff_tx = controller.record.transaction.transaction_id
    assert tariff_tx not in {rce_tx, rcm_tx}

    at = now + timedelta(seconds=13)
    observed = at - timedelta(milliseconds=500)
    charged = tariff_source(
        at, physical_mode_code=4, full_block_generation=13,
        full_block_generation_at=observed,
        force_charge_soc_percent=85.0,
        maximum_charge_power_percent=50.0,
        tariff_active=True,
        grid_power_w=-3000, grid_power_observed_at=observed,
        battery_power_w=-5000, battery_power_observed_at=observed,
        pv_power_w=3000, pv_power_observed_at=observed,
        load_power_w=1000, load_power_observed_at=observed,
    )
    await controller.async_reconcile(b.policy_local_frame(
        at, charged, tariff_active=False, transaction_pending=True,
    ))
    assert controller.record.state is a.ActiveState.EXECUTING
    assert controller.record.owner is a.ExecutionOwner.TARIFF
    assert controller.record.transaction.transaction_id == tariff_tx

    at = now + timedelta(seconds=14)
    charged = replace(charged,
        full_block_generation_at=at - timedelta(milliseconds=500),
        grid_power_observed_at=at - timedelta(milliseconds=500),
        battery_power_observed_at=at - timedelta(milliseconds=500),
        pv_power_observed_at=at - timedelta(milliseconds=500),
        load_power_observed_at=at - timedelta(milliseconds=500),
    )
    tariff_running = b.policy_local_frame(
        at, charged, tariff_active=True, transaction_pending=False,
    )
    off_decision = a.arbitrate_supervisor(
        mode=a.SupervisorMode.OFF,
        profile=a.SupervisorProfile.BALANCED,
        context=tariff_running.context,
        candidates=tariff_running.candidates, now=at,
    )
    fail_restore = True
    clock_at = at + timedelta(milliseconds=100)
    await controller.async_reconcile(replace(
        tariff_running, decision=off_decision,
    ))
    assert controller.record.state is a.ActiveState.FAULT
    assert controller.record.reason is a.ExecutionReason.COMMAND_OUTCOME_UNKNOWN
    assert controller.record.owner is a.ExecutionOwner.TARIFF
    assert controller.record.transaction.transaction_id == tariff_tx
    assert len(writes) == 6
    fault = records[-1]
    assert fault == controller.record
    fail_restore = False
    restarted = a.SupervisorActiveController(
        persist=persist, dispatch=dispatch, publish=lambda _record: None,
        persisted_record=fault, clock=lambda: clock_at,
    )
    await restarted.async_initialize()
    assert restarted.record.state is a.ActiveState.STOPPING
    assert restarted.record.reason is a.ExecutionReason.RESTART_RECOVERY
    assert restarted.record.owner is a.ExecutionOwner.TARIFF
    assert restarted.record.transaction.transaction_id == tariff_tx
    assert len(writes) == 6

    at = now + timedelta(seconds=40)
    stale = b.policy_local_frame(
        at, charged, tariff_active=True, transaction_pending=True,
    )
    await restarted.async_reconcile(stale)
    assert restarted.record.owner is a.ExecutionOwner.TARIFF
    assert restarted.record.transaction.transaction_id == tariff_tx
    assert len(writes) == 6, "stale FC03 produced a cached restore or new start"

    at = now + timedelta(seconds=41)
    neutral_tariff = tariff_source(
        at, physical_mode_code=0, full_block_generation=14,
        full_block_generation_at=at - timedelta(milliseconds=500),
        self_use_soc_percent=25.0,
    )
    await restarted.async_reconcile(b.policy_local_frame(
        at, neutral_tariff, tariff_active=False,
        transaction_pending=True,
    ))
    assert restarted.record.state is a.ActiveState.IDLE
    assert restarted.record.owner is a.ExecutionOwner.NONE
    assert restarted.record.last_transaction.transaction_id == tariff_tx
    assert len(writes) == 6

    at = now + timedelta(seconds=42)
    clock_at = at + timedelta(milliseconds=100)
    second_start = a.execution_source(
        at, full_block_generation=15,
        full_block_generation_at=at - timedelta(milliseconds=500),
    )
    second_run_end = now + timedelta(minutes=60)
    second_slot_end = now + timedelta(minutes=35)
    second_frame = rce(
        at, second_start, active=False,
        run_end=second_run_end, slot_end=second_slot_end,
    )
    await restarted.async_reconcile(second_frame)
    assert restarted.record.state is a.ActiveState.WAITING_READBACK
    assert restarted.record.owner is a.ExecutionOwner.RCE
    second_rce_tx = restarted.record.transaction.transaction_id
    assert second_rce_tx not in {rce_tx, rcm_tx, tariff_tx}
    assert len(writes) == 7 and writes[-1].ems_block.mode is a.EmsMode.GRID_DISCHARGE

    at = now + timedelta(seconds=44)
    observed = at - timedelta(milliseconds=500)
    second_physical = a.execution_source(
        at, physical_mode_code=5, full_block_generation=16,
        full_block_generation_at=observed,
        force_discharge_soc_percent=41.0,
        maximum_discharge_power_percent=25.0,
        grid_power_w=1200, grid_power_observed_at=observed,
        battery_power_w=900, battery_power_observed_at=observed,
        bms_max_discharge_current_a=200,
        bms_discharge_current_observed_at=observed,
    )
    await restarted.async_reconcile(rce(
        at, second_physical, active=False,
        run_end=second_run_end, slot_end=second_slot_end,
    ))
    assert restarted.record.state is a.ActiveState.EXECUTING
    assert restarted.record.transaction.transaction_id == second_rce_tx

    at = now + timedelta(seconds=45)
    clock_at = at + timedelta(milliseconds=100)
    running = rce(at, replace(
        second_physical,
        full_block_generation_at=at - timedelta(milliseconds=500),
        bms_discharge_current_observed_at=at - timedelta(milliseconds=500),
    ), active=True, run_end=second_run_end, slot_end=second_slot_end)
    off_decision = a.arbitrate_supervisor(
        mode=a.SupervisorMode.OFF,
        profile=a.SupervisorProfile.BALANCED,
        context=running.context, candidates=running.candidates, now=at,
    )
    await restarted.async_reconcile(replace(running, decision=off_decision))
    assert restarted.record.state is a.ActiveState.RESTORING
    assert restarted.record.owner is a.ExecutionOwner.RCE
    assert len(writes) == 8 and writes[-1].ems_block.mode is a.EmsMode.SELF_USE
    at = now + timedelta(seconds=47)
    await restarted.async_reconcile(rce(
        at, a.execution_source(
            at, full_block_generation=17,
            full_block_generation_at=at - timedelta(milliseconds=500),
        ), active=False, run_end=second_run_end, slot_end=second_slot_end,
    ))
    assert restarted.record.state is a.ActiveState.IDLE
    assert restarted.record.owner is a.ExecutionOwner.NONE
    assert restarted.record.last_transaction.transaction_id == second_rce_tx
    assert len(writes) == 8
    print("PASS X03 RCE -> RCEm intervention -> tariff fault/restart -> RCE")


if __name__ == "__main__":
    asyncio.run(main())
