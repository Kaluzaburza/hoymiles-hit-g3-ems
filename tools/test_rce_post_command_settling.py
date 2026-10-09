"""Offline real-builder/controller regression for the measured Mode 5 LOAD lag."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import test_supervisor_active_controller as t
from supervisor_active_bridge import (
    post_command_rce_measurement_hold_authorized,
    settings_from_execution_source,
)

CHECKS = 0
MARKET = "b" * 64
END = t.NOW + timedelta(minutes=30)


def check(value, message):
    global CHECKS
    CHECKS += 1
    assert value, message


def source(at, *, power=50, floor=49, generation=11, mode=5, **extra):
    return t.execution_source(
        at, physical_mode_code=mode, full_block_generation=generation,
        force_discharge_soc_percent=floor, maximum_discharge_power_percent=power,
        battery_soc_percent=74, bms_voltage_v=53, bms_max_discharge_current_a=390,
        battery_power_w=7000, battery_power_observed_at=at-timedelta(milliseconds=200),
        grid_power_w=0, grid_power_observed_at=at-timedelta(milliseconds=200),
        pv_power_w=0, pv_power_observed_at=at-timedelta(milliseconds=200),
        load_power_w=7860, load_power_observed_at=at-timedelta(milliseconds=200),
        **extra,
    )


def rebuild(frame, *, rce=None, context=None):
    rce = rce or frame.rce
    context = context or frame.context
    candidates = (t.build_rce_candidate(rce, now=frame.now), *frame.candidates[1:])
    decision = t.arbitrate_supervisor(
        mode=frame.decision.supervisor_mode, profile=t.SupervisorProfile.BALANCED,
        context=context, candidates=candidates, now=frame.now,
    )
    return replace(frame, rce=rce, context=context, candidates=candidates, decision=decision)


def frame(at, physical, *, active=True, power=50, floor=49, pending_ack=False, **extra):
    frame = t.rce_frame(
        at, physical, deadline=END, floor=floor, power=power,
        active=active, transaction_pending=pending_ack,
    )
    return rebuild(frame, rce=replace(
        frame.rce, system_power_kw=16, requested_discharge_power_kw=power*.16,
        current_slot_suppression_reason="eligible",
        current_slot_load_exhausts_requested_discharge_budget=False,
        post_command_settling_market_fingerprint=MARKET,
        requested_discharge_power_percent=power, **extra,
    ))


def load_block(at, physical=None, *, pending=False, **extra):
    f = frame(at, physical or source(at))
    values = dict(
        current_slot_planned=False, current_slot_start_eligible=False,
        current_slot_continue_eligible=False, current_run_end=None,
        planned_export_energy_kwh=0, requested_discharge_power_kw=0,
        protected_soc_floor_percent=50, control_data_ready=False,
        # Active commitment retains the physical command, not raw plan power 0.
        effective_discharge_power_percent=50,
        current_slot_suppression_reason="no_current_plan",
        current_slot_load_exhausts_requested_discharge_budget=True,
        requested_discharge_power_percent=40,
        result_current=not pending, recalculation_pending=pending,
    )
    values.update(extra)
    return rebuild(f, rce=replace(f.rce, **values))


async def running(*, market=MARKET, ack_seconds=2):
    clock = {"now": t.NOW}
    writes = []
    async def persist(_record):
        pass
    async def dispatch(write):
        writes.append(write)
    controller = t.SupervisorActiveController(
        persist=persist, dispatch=dispatch, publish=lambda _record: None,
        clock=lambda: clock["now"],
    )
    await controller.async_initialize()
    initial = frame(t.NOW, t.execution_source(t.NOW, battery_soc_percent=74,
        bms_voltage_v=53, bms_max_discharge_current_a=390), active=False)
    initial = rebuild(initial, rce=replace(initial.rce,
        post_command_settling_market_fingerprint=market))
    await controller.async_reconcile(initial)
    check(controller.record.state is t.ActiveState.WAITING_READBACK, "start needs FC03")
    check(controller.post_command_settling_replan is None, "transport success is not settling proof")
    clock["now"] = t.NOW + timedelta(seconds=ack_seconds)
    await controller.async_reconcile(frame(clock["now"], source(clock["now"]),
        active=False, pending_ack=True))
    check(controller.record.state is t.ActiveState.EXECUTING, "own FC03 + BAT confirms start")
    check(controller.post_command_settling_replan is None, "healthy command requests no extra solver")
    return controller, clock, writes


async def reconcile(controller, clock, f):
    clock["now"] = f.now
    await controller.async_reconcile(f)


async def test_fixed_budget_and_restore():
    c, clock, writes = await running()
    tx = c.record.transaction
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=27)))
    check(c.record.state is t.ActiveState.EXECUTING, "measured live LOAD suppression retains Mode5")
    check(len(writes) == 1, "LOAD grace must not dispatch a replacement")
    key = (tx.transaction_id, tx.command_sent_at)
    check(c.post_command_settling_replan == (key, t.NOW+timedelta(seconds=150)), "fixed replan150")
    check(c.post_command_settling_deadline == t.NOW+timedelta(seconds=180), "fixed end180")
    check(c.record.transaction.command_snapshot == tx.command_snapshot, "baseline unchanged")
    for second in (35, 150, 179.999):
        f = load_block(t.NOW+timedelta(seconds=second), pending=second >= 45)
        await reconcile(c, clock, f)
        check(c.record.state is t.ActiveState.EXECUTING, "fresh/pending remains bounded")
        check(c.post_command_settling_deadline == t.NOW+timedelta(seconds=180), "reports never renew")
        check(c.execution_watchdog[1] <= t.NOW+timedelta(seconds=180), "watchdog cannot slide past180")
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=180), pending=True))
    check(c.record.state is t.ActiveState.RESTORING, "pending cannot inherit another hold at180")
    check(len(writes) == 2 and writes[-1].ems_block.mode == 0, "expiry uses ordinary restore")
    check(c.post_command_settling_replan is None, "restore cancels replan")
    at = t.NOW+timedelta(seconds=182)
    baseline = t.execution_source(at, full_block_generation=20, battery_soc_percent=74,
        bms_voltage_v=53, bms_max_discharge_current_a=390)
    await reconcile(c, clock, frame(at, baseline))
    check(c.record.state is t.ActiveState.IDLE and c.record.owner is t.ExecutionOwner.NONE,
          "restore needs its own FC03 before release")


async def test_real_successor_and_command_identity():
    c, clock, writes = await running()
    original = c.record.transaction
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=27)))
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=38)))
    at = t.NOW+timedelta(seconds=45)
    await reconcile(c, clock, frame(at, source(at, generation=12), power=40, floor=50))
    check(c.record.state is t.ActiveState.RETARGETING, "fresh valid plan sends successor")
    check(len(writes) == 2 and writes[-1].ems_block.maximum_discharge_power_percent_4306 == 40, "only valid40 dispatched")
    check(c.post_command_settling_replan is None, "unacknowledged successor has no grace")
    at += timedelta(seconds=2)
    await reconcile(c, clock, frame(at, source(at, power=40, floor=50, generation=13),
        power=40, floor=50, pending_ack=True))
    check(c.record.state is t.ActiveState.EXECUTING, "successor needs own newer FC03")
    tx = c.record.transaction
    check(tx.transaction_id == original.transaction_id and tx.deadline == original.deadline,
          "retarget preserves transaction and original deadline")
    check(tx.command_snapshot == original.command_snapshot, "retarget preserves original baseline")
    at = t.NOW+timedelta(seconds=50)
    bad = load_block(at, source(at, power=40, floor=50, generation=14),
        effective_discharge_power_percent=40)
    await reconcile(c, clock, bad)
    check(c.record.state is t.ActiveState.EXECUTING, "confirmed retarget has its own settling")
    check(c.post_command_settling_deadline == t.NOW+timedelta(seconds=225), "retarget anchored at sent45")
    check(c.post_command_settling_replan[0] != (original.transaction_id, original.command_sent_at),
          "same transaction is not same command key")
    check(all(w.ems_block.mode == 5 for w in writes), "no Mode0 during accepted retarget")


async def test_bridge_negative_matrix():
    c, clock, writes = await running()
    tx = c.record.transaction
    at = t.NOW+timedelta(seconds=27)
    good = load_block(at)
    def authorized(f, *, recognized=False):
        return post_command_rce_measurement_hold_authorized(
            tx.intent, f.decision, f.candidates, settings_from_execution_source(f.execution),
            execution_source=f.execution, export_state=f.context.export_state,
            rce=f.rce, market_fingerprint=MARKET, recognized_pending=recognized, now=f.now,
        )
    check(authorized(good), "real no-action builder is accepted only by narrow hold")
    for field, value in (
        ("current_slot_load_exhausts_requested_discharge_budget", False),
        ("current_slot_load_exhausts_requested_discharge_budget", None),
        ("current_slot_suppression_reason", "pv_or_grid_balance_unsafe"),
        ("current_slot_suppression_reason", "insufficient_runtime"),
        ("post_command_settling_market_fingerprint", "c"*64),
        ("post_command_settling_market_fingerprint", None),
        ("requested_discharge_power_percent", 0),
        ("requested_discharge_power_percent", float("nan")),
        ("requested_discharge_power_percent", False),
        ("protected_soc_floor_percent", 74),
        ("protected_soc_floor_percent", None),
        ("reserve_ready", None), ("control_data_ready", None),
        ("input_revision", None), ("input_revision", True),
        ("sale_block_active", True), ("enabled", False), ("allowed_by_user", False),
        ("latched_slot_end", END+timedelta(minutes=30)),
        ("status_code", t.RcePlanStatus.HOME_ENERGY_SHORTAGE),
    ):
        check(not authorized(rebuild(good, rce=replace(good.rce, **{field:value}))), field)
    check(not authorized(load_block(at, pending=True)), "unrecognized pending does not acquire grace")
    check(authorized(load_block(at, pending=True), recognized=True), "recognized pending can finish bounded replan")
    for field, value in (("bms_max_discharge_current_a", 0), ("bms_max_discharge_current_a", 20),
                         ("bms_voltage_v", float("nan")),
                         ("bms_discharge_current_observed_at", at-timedelta(seconds=301))):
        check(not authorized(replace(good, execution=replace(good.execution, **{field:value}))), field)
    for prefix in ("load", "pv", "grid"):
        for value in (None, float("nan"), float("inf"), True):
            check(not authorized(replace(good, execution=replace(good.execution,
                **{prefix+"_power_w":value}))), prefix+" rejects invalid live value")
        for stamp in (None, at-timedelta(seconds=16), at+timedelta(seconds=6)):
            check(not authorized(replace(good, execution=replace(good.execution,
                **{prefix+"_power_observed_at":stamp}))), prefix+" rejects missing/stale/future")
    for field, value in (("battery_soc_percent", 50), ("battery_soc_percent", None),
                         ("battery_soc_observed_at", at-timedelta(seconds=121)),
                         ("battery_soc_observed_at", at+timedelta(seconds=6))):
        check(not authorized(replace(good, execution=replace(good.execution, **{field:value}))), field)


async def test_immediate_stops_and_no_late_basis():
    cases = (
        ("BAT stale", lambda f: replace(f, execution=replace(f.execution,
            battery_power_observed_at=f.now-timedelta(seconds=16)))),
        ("BAT opposite", lambda f: replace(f, execution=replace(f.execution, battery_power_w=-800))),
        ("EMS stale", lambda f: replace(f, execution=replace(f.execution,
            full_block_generation_at=f.now-timedelta(seconds=31)))),
        ("GCF stale", lambda f: replace(f, execution=replace(f.execution,
            gcf_generation_at=f.now-timedelta(seconds=31)))),
        ("owner conflict", lambda f: rebuild(f, context=replace(f.context, owner_conflict=True))),
        ("GCF blocked", lambda f: rebuild(f, context=replace(f.context, export_state=t.ExportState.CONFIRMED_ZERO_EXPORT))),
        ("physical mismatch", lambda f: replace(f, execution=replace(f.execution, maximum_discharge_power_percent=40))),
        ("Off Grid", lambda f: rebuild(replace(f, execution=replace(f.execution, physical_mode_code=3)),
            context=replace(f.context, physical_mode=t.PhysicalMode.OFF_GRID))),
        ("Slave topology", lambda f: replace(f, execution=replace(f.execution, machine_type_code=2))),
    )
    for label, mutate in cases:
        c, clock, writes = await running()
        f = mutate(load_block(t.NOW+timedelta(seconds=27)))
        await reconcile(c, clock, f)
        check(c.record.state is not t.ActiveState.EXECUTING, label+" cannot be hidden by grace")
    c, clock, writes = await running(market=None)
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=27)))
    check(c.record.state is not t.ActiveState.EXECUTING, "no late capture from bad plan")


async def test_healthy_interlude_cannot_renew_budget():
    c, clock, writes = await running()
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=27)))
    key = c.post_command_settling_replan[0]
    at = t.NOW+timedelta(seconds=38)
    await reconcile(c, clock, frame(at, source(at)))
    check(c.post_command_settling_replan is None, "healthy current plan suppresses unnecessary replan")
    check(c.post_command_settling_deadline == t.NOW+timedelta(seconds=180), "healthy interlude preserves anchor")
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=45), pending=True))
    check(c.post_command_settling_replan[0] == key, "pending after healthy plan keeps original key")
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=58), pending=True))
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=180), pending=True))
    check(c.record.state is t.ActiveState.RESTORING, "intermittent healthy plan cannot gain a new settling budget")


async def test_unsent_successor_preserves_old_anchor():
    c, clock, writes = await running()
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=27)))
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=37)))
    old = c.record.transaction
    at = t.NOW+timedelta(seconds=40)
    latest_target = frame(at, source(at, generation=12), power=30)
    async def persist(record):
        if record.state is t.ActiveState.RETARGETING:
            c._frame_resampler = lambda: latest_target
    c._persist = persist
    await reconcile(c, clock, frame(at, source(at, generation=12), power=40))
    check(c.record.state is t.ActiveState.EXECUTING, "newest valid target preserves unsent predecessor")
    check(len(writes) == 1, "rejected pre-dispatch successor was never sent")
    check(c.record.transaction.command_sent_at == old.command_sent_at, "unsent successor cannot renew sent time")
    c._frame_resampler = None
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=45)))
    check(c.post_command_settling_deadline == t.NOW+timedelta(seconds=180), "unsent successor cannot renew grace")


async def test_restart_has_no_grace_context():
    c, clock, writes = await running()
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=27)))
    async def persist(record):
        pass
    async def dispatch(write):
        writes.append(write)
    recovered = t.SupervisorActiveController(persist=persist, dispatch=dispatch,
        publish=lambda record: None, clock=lambda: clock["now"], persisted_record=c.record)
    await recovered.async_initialize()
    check(recovered.post_command_settling_replan is None, "restart cannot borrow volatile market basis")
    check(recovered.post_command_settling_deadline is None, "restart uses existing recovery, not grace")


async def test_late_ack_keeps_send_time_anchor():
    c, clock, writes = await running(ack_seconds=59)
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=59.5)))
    check(c.record.state is t.ActiveState.EXECUTING, "late genuine ACK permits only remaining budget")
    check(c.post_command_settling_deadline == t.NOW+timedelta(seconds=180), "ACK59 does not restart180")
    check(c.post_command_settling_replan[1] == t.NOW+timedelta(seconds=150), "late delivery cannot shift requested replan")
    await reconcile(c, clock, load_block(t.NOW+timedelta(seconds=180), pending=True))
    check(c.record.state is t.ActiveState.RESTORING, "late ACK still expires at original180")


async def main():
    await test_fixed_budget_and_restore()
    await test_real_successor_and_command_identity()
    await test_bridge_negative_matrix()
    await test_immediate_stops_and_no_late_basis()
    await test_healthy_interlude_cannot_renew_budget()
    await test_unsent_successor_preserves_old_anchor()
    await test_restart_has_no_grace_context()
    await test_late_ack_keeps_send_time_anchor()
    print(f"RCE post-command settling: PASS ({CHECKS} checks)")


if __name__ == "__main__":
    asyncio.run(main())
