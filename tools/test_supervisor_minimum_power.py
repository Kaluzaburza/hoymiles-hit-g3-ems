"""200 W battery/export minimum; positive house support has no 200 W floor."""
from dataclasses import replace
import math
import asyncio

import test_ems_supervisor as base
import ems_supervisor as ems


def test_minimum_power():
    for policy, action in (
        (ems.PolicyId.RCE, ems.RequestedAction.RCE_EXPORT),
        (ems.PolicyId.TARIFF, ems.RequestedAction.TARIFF_BATTERY_CHARGE),
        (ems.PolicyId.TARIFF, ems.RequestedAction.TARIFF_GRID_SUPPORT),
        (ems.PolicyId.TARIFF, ems.RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE),
    ):
        for power in (0.0, 0.001, 0.199, 0.199999, 0.2, 0.200001, 5.0):
            original = base.candidate(policy, requested_action=action)
            filtered = ems.apply_minimum_grid_power(original, power)
            decision = base.decide((filtered,))
            eligible = power > 0 if action is ems.RequestedAction.TARIFF_GRID_SUPPORT else power >= 0.2
            assert (decision.selected_policy == policy) == eligible, (action, power, decision)
            if not eligible:
                assert filtered.blocked_reason.value == "below_minimum_grid_power"
                assert filtered.requested_action == original.requested_action
                assert not filtered.start_eligible and not filtered.continuation_eligible
            else:
                assert filtered is original
        for missing in (None, True, "0.3", float("nan"), float("inf"), -0.1):
            filtered = ems.apply_minimum_grid_power(original, missing)
            assert not filtered.available
            assert base.decide((filtered,)).selected_policy is None

    # House supply cannot mask a subminimum battery allocation.
    mixed = base.candidate(ems.PolicyId.TARIFF,
        requested_action=ems.RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        requested_power_kw=0.1)
    assert not ems.apply_minimum_tariff_power(mixed, {
        'current_slot_planned_import_power_kw': .6,
        'current_slot_planned_charge_power_kw': .1}).available
    assert ems.apply_minimum_tariff_power(mixed, {
        'current_slot_planned_import_power_kw': .7,
        'current_slot_planned_charge_power_kw': .2}).available
    assert not ems.apply_minimum_tariff_power(mixed, {
        'current_slot_planned_import_power_kw': .7}).available
    # Active commitments cannot retain a freshly rejected subminimum plan.
    active = replace(mixed, active_latched=True)
    filtered = ems.apply_minimum_grid_power(active, 0.1)
    ctx = base.context(owner_kind=ems.OwnerKind.TARIFF, physical_mode=ems.PhysicalMode.GRID_CHARGE)
    assert base.decide((filtered,), execution_context=ctx).selected_policy is None
    # RCEm and no-action are outside this economic block filter.
    rcm = base.candidate(ems.PolicyId.RCM)
    assert ems.apply_minimum_grid_power(rcm, 0.01) is rcm
    idle = base.no_action_candidate(ems.PolicyId.TARIFF)
    assert ems.apply_minimum_grid_power(idle, None) is idle
    # Partial intervals preserve power, not a fixed kWh threshold.
    for seconds in (60.0, 300.0, 900.0, 1800.0):
        for power in (0.199, 0.2, 0.201):
            actual = ems.planned_grid_power_kw(power * seconds / 3600.0, seconds)
            assert math.isclose(actual, power)
            assert (ems.apply_minimum_grid_power(mixed, actual).available) == (power >= 0.2)
    print("200 W boundary, mixed import, active rejection, RCEm and partial intervals: PASS")


async def test_controller_stop_and_meter_noise():
    import test_tariff_active_controller as tc
    controller, sent, saved, clock = await tc.confirmed_controller()
    transaction_id = controller.record.transaction.transaction_id
    for power in (0.2, 0.8, 0.199):
        clock[0] += tc.timedelta(seconds=1)
        # Battery is at zero in a valid grid-support execution. The decision
        # uses the plan and cannot stop just because battery charging is small.
        current = tc.frame(clock[0], tc.physical(clock[0], generation=20 + len(saved)), controller.record)
        filtered = tuple(ems.apply_minimum_grid_power(c, power) for c in current.candidates)
        decision = ems.arbitrate_supervisor(mode=current.decision.supervisor_mode,
            profile=ems.SupervisorProfile.BALANCED, context=current.context,
            candidates=filtered, now=current.now)
        await controller.async_reconcile(replace(current, candidates=filtered, decision=decision))
        if power > 0:
            assert controller.record.state is tc.ActiveState.EXECUTING
            assert controller.record.transaction.transaction_id == transaction_id
            assert len(sent) == 1
        else:
            assert controller.record.state is tc.ActiveState.RESTORING, controller.record
            assert sent[-1].ems_block.mode.value == 0
            evidence = controller.recorder_attributes()["recent_stop_decisions"][-1]
            assert evidence["transaction_id"] == transaction_id
            assert evidence["reason"] == "authorization_lost"
            assert evidence["candidate_blocker"] == "below_minimum_grid_power"
            assert evidence["continuation_eligible"] is False
    print("Confirmed same-transaction house support also below 200 W: PASS")


def test_canonical_rejection_visible():
    import test_supervisor_canonical_runtime as cc
    source = cc.timelines()
    # The policy's requested grid energy is authoritative for the filter.
    # Other forecast flow (PV/load) is not a reason to keep a tiny action.
    source["tariff"]["points"][0]["policy"]["planned_import_kwh"] = 0.05
    source["tariff"]["points"][0]["policy"]["direct_load_kwh"] = 0.
    frame = cc.frame()
    frame.tariff = replace(frame.tariff, requested_charge_power_kw=.1)
    ledger = cc.build_supervisor_canonical_ledger(frame=frame, timelines=source,
        usable_capacity_kwh=cc.CAPACITY)
    assert ledger.slots[0].selected_policy is cc.LedgerPolicy.NONE
    payload = cc.canonical_execution_ledger_to_dict(ledger)
    assert "below_minimum_grid_power" in str(payload)
    assert cc.audit_canonical_execution_ledger(ledger).valid
    print("Rejected block retained with reason in balanced canonical ledger: PASS")


if __name__ == "__main__":
    test_minimum_power()
    asyncio.run(test_controller_stop_and_meter_noise())
    test_canonical_rejection_visible()
