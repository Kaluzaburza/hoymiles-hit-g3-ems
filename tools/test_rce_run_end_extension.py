"""Read-only fake-HA callback regression for covering RCE run ends.

Uses the actual runtime/controller/executor and replaces only HA/transport.
This is an execution regression, not an optimizer or physical inverter test.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import runpy


HERE = Path(__file__).resolve().parent
WORK = HERE.parent
CHECKS = 0


def check(condition, message):
    global CHECKS
    assert condition, message
    CHECKS += 1


async def scenario(f, *, shorter=False, pre_dispatch_pending=False):
    S, clock, now = f["SENSOR"], f["CLOCK"], f["NOW"]
    hass, entry, _runtime, sensor = f["environment"]()
    deadline = now + timedelta(seconds=12)
    raw_end = deadline + (timedelta(seconds=-1) if shorter else timedelta(minutes=30))
    writes = []

    def eid(key):
        return f["_source_entity_id"](S._SOURCE_BY_KEY[key], entry.entry_id)

    def state(value, attrs=None):
        return f["FakeState"](str(value), attrs, clock["now"])

    def store(key, value, attrs=None):
        hass.states.values[eid(key)] = state(value, attrs)

    async def dispatch(write):
        writes.append(write)

    async def drain():
        for _ in range(32):
            await asyncio.sleep(0)
            if sensor._controller_task is None and sensor._pending_active_frame is None:
                return
        raise AssertionError("adapter failed to drain")

    async def fire_pending_callback():
        pending = hass.active_delays()
        check(len(pending) == 1, "expected one actual HA callback")
        pending[0].run()
        await drain()

    async def report(generation, *, mode=5, floor=72.0, power=50.0):
        for key, value in (("ems_mode_readback", mode), ("self_use_soc_readback", 20),
                           ("backup_soc_readback", 80), ("charge_soc_readback", 80),
                           ("charge_power_ems_readback", 40), ("discharge_soc_readback", floor),
                           ("discharge_power_readback", power), ("ems_generation", generation)):
            hass.fire_report(eid(key), state(value))
        hass.fire_state(eid("rce_active"), state("on" if mode == 5 else "off"))
        hass.fire_state(eid("grid_power"), state(1200 if mode == 5 else 0))
        hass.fire_state(eid("battery_power"), state(900 if mode == 5 else 0))
        # Report all four physical channels. A partial GRID/BATTERY pair keeps
        # the separate collection timeout pending and cannot confirm a flow
        # cohort. In sale mode: PV 500 + battery 900 = load 200 + export 1200.
        hass.fire_state(eid("pv_power"), state(500 if mode == 5 else 300))
        hass.fire_state(eid("load_power"), state(200 if mode == 5 else 300))
        await fire_pending_callback()

    plan = {**f["_plan_attributes"]("rce_plan"), "input_revision": 1,
            "current_slot_planned": True, "current_slot_start_eligible": True,
            "current_slot_continue_eligible": True,
            "current_slot_end": (now + timedelta(minutes=10)).isoformat(),
            "current_run_end": deadline.isoformat(),
            "current_slot_execution_discharge_power_kw": 5.0,
            "current_slot_planned_export_kwh": 0.4,
            "current_required_minimum_soc_percent": 72.0}
    for key, value in (("supervisor_mode", "Active"), ("allow_rce", "on"),
                       ("rce_enabled", "on"), ("rce_active", "off"),
                       ("rce_control_data_ready", "on"), ("rce_price_above_threshold", "on"),
                       ("rce_reserve_ready", "on"), ("sale_block_active", "off"),
                       ("ems_execution_ready", "on"), ("rce_effective_discharge_power", 50),
                       ("rce_latched_minimum_soc", 72), ("battery_soc", 90),
                       ("bms_max_discharge_current", 240), ("ems_mode_readback", 0),
                       ("ems_generation", 1), ("self_use_soc_readback", 20),
                       ("backup_soc_readback", 80), ("charge_soc_readback", 80),
                       ("charge_power_ems_readback", 40), ("discharge_soc_readback", 20),
                       ("discharge_power_readback", 50)):
        store(key, value)
    store("rce_plan", "ready", plan)
    store("rce_latched_slot_end", "ignored", {"timestamp": deadline.timestamp()})
    await sensor.add_to_platform_finish()
    controller = sensor._controller
    assert controller is not None
    controller._clock = lambda: clock["now"]
    controller._dispatch = dispatch
    await drain()
    check(controller.record.state is S.ActiveState.WAITING_READBACK and len(writes) == 1,
          "initial 72/50 was not sent")
    original = controller.record.transaction
    assert original is not None
    baseline, txid = original.command_snapshot, original.transaction_id
    # This callback fixture does not run the ESP lease service. Supply its
    # accepted, hard-deadline-capped boundary for the retarget budget.
    controller._control_lease_valid_until = lambda: deadline
    from custom_components.hoymiles_hit_modbus import ems_notifications as N
    notifier = object.__new__(N.HoymilesEmsNotificationManager)
    notifier.hass = hass
    notifier._model = N.RangeNotificationModel()
    assert notifier._model.observe(
        notifier._observation(controller.record, controller.last_frame)
    ) == ()
    clock["now"] = now + timedelta(seconds=1)
    await report(2)
    check(controller.record.state is S.ActiveState.EXECUTING, "A was not physically confirmed")
    started = notifier._model.observe(
        notifier._observation(controller.record, controller.last_frame)
    )
    check(len(started) == 1 and started[0].kind == "start", "physical A did not produce one START")

    original_persist = controller._persist
    pending_injected = False
    async def persist_with_plan_debounce(record):
        nonlocal pending_injected
        await original_persist(record)
        if (pre_dispatch_pending and not pending_injected
                and record.state is S.ActiveState.RETARGETING
                and record.transaction.command_sent_at is None):
            pending_injected = True
            plan_state = hass.states.values[eid("rce_plan")]
            hass.fire_state(eid("rce_plan"), state("ready", {
                **plan_state.attributes, "input_revision": 3,
                "result_current": False, "recalculation_pending": True}))
            check(sensor._current_dispatch_frame() is None,
                  "actual raw-plan debounce exposed an old dispatch frame")
            check(sensor._retarget_replan_pending(),
                  "covering raw run lost the narrow pending predecessor gate")
            pending_state = hass.states.values[eid("rce_plan")]
            for end_value in (None, "invalid", (deadline-timedelta(seconds=1)).isoformat()):
                hass.states.values[eid("rce_plan")] = state("ready", {
                    **pending_state.attributes, "current_run_end": end_value})
                check(not sensor._retarget_replan_pending(),
                      "missing/invalid/shorter raw run gained predecessor authority")
            hass.states.values[eid("rce_plan")] = state("ready", {
                **pending_state.attributes, "current_run_end": deadline.isoformat()})
            check(sensor._retarget_replan_pending(), "equal raw run lost existing authority")
            hass.states.values[eid("rce_plan")] = state("ready", {
                **pending_state.attributes, "current_slot_start_eligible": False})
            check(not sensor._retarget_replan_pending(),
                  "raw run extension bypassed an existing eligibility gate")
            hass.states.values[eid("rce_plan")] = pending_state
            allow_state = hass.states.values[eid("allow_rce")]
            hass.states.values[eid("allow_rce")] = state("off")
            check(not sensor._retarget_replan_pending(), "raw run extension bypassed permission loss")
            hass.states.values[eid("allow_rce")] = allow_state
    controller._persist = persist_with_plan_debounce

    clock["now"] = now + timedelta(seconds=2)
    successor = {**plan, "input_revision": 2, "current_run_end": raw_end.isoformat(),
                 "current_required_minimum_soc_percent": 74.0,
                 "current_slot_execution_discharge_power_kw": 4.0}
    hass.fire_state(eid("rce_effective_discharge_power"), state(40))
    hass.fire_state(eid("rce_plan"), state("ready", successor))
    clock["now"] = now + timedelta(seconds=3)
    await fire_pending_callback()
    if pre_dispatch_pending:
        check(pending_injected and controller.record.state is S.ActiveState.EXECUTING
              and len(writes) == 1,
              "covering raw-plan debounce between prepare/send did not resume unsent predecessor")
        check(controller.record.transaction.command_sent_at == original.command_sent_at
              and controller.record.transaction.transaction_id == txid,
              "unsent predecessor resume replaced A's transaction/send evidence")
        await fire_pending_callback()
        check(controller.record.state is S.ActiveState.EXECUTING and len(writes) == 1,
              "pending callback failed to retain confirmed A")
        hass.fire_state(eid("rce_plan"), state("ready", {
            **successor, "input_revision": 3}))
        await fire_pending_callback()
    check(hass.states.values[eid("rce_plan")].attributes["current_run_end"] == raw_end.isoformat(),
          "raw published optimizer end was rewritten")
    current = controller.record.transaction
    assert current is not None
    check(current.transaction_id == txid and current.deadline == deadline
          and current.command_snapshot == baseline, "retarget changed the original lease/baseline")
    check(notifier._model.observe(
        notifier._observation(controller.record, controller.last_frame)
    ) == (), "retarget preparation produced a duplicate notification")
    check(controller.record.state is S.ActiveState.RETARGETING and len(writes) == 2
          and writes[-1].ems_block.force_discharge_soc_percent_4305 == 74
          and writes[-1].ems_block.maximum_discharge_power_percent_4306 == 40,
          "fresh 50->40 did not dispatch the bounded same-owner retarget")
    stop_at = min(raw_end, deadline)
    check(controller.last_frame.rce.current_run_end == stop_at,
          "execution end did not respect both the fresh plan and original lease")
    clock["now"] = now + timedelta(seconds=4)
    await report(3, floor=74, power=40)
    check(notifier._model.observe(
        notifier._observation(controller.record, controller.last_frame)
    ) == (), "confirmed same-owner retarget produced a duplicate START")
    current = controller.record.transaction
    assert current is not None
    check(controller.record.state is S.ActiveState.EXECUTING and len(writes) == 2
          and current.readback_result.value == "confirmed"
          and current.physical_verification.observed_at > current.command_sent_at,
          "B did not consume its own newer FC03 and physical proof")

    clock["now"] = now + timedelta(seconds=5)
    pending = {**successor, "result_current": False, "recalculation_pending": True}
    hass.fire_state(eid("rce_control_data_ready"), state("off"))
    hass.fire_state(eid("rce_plan"), state("ready", pending))
    await fire_pending_callback()
    check(controller.record.state is S.ActiveState.EXECUTING and len(writes) == 2
          and controller.last_frame.rce.current_run_end == stop_at,
          "pending plan lost the already-confirmed bounded hold")
    check(hass.states.values[eid("rce_plan")].attributes["current_run_end"] == raw_end.isoformat(),
          "pending normalization modified the raw plan")
    boundary = next(handle for handle in hass.active_points() if handle.when == stop_at)
    clock["now"] = stop_at + timedelta(microseconds=1)
    boundary.run()
    await drain()
    check(controller.record.state is S.ActiveState.RESTORING
          and [write.ems_block.mode.value for write in writes] == [5, 5, 0]
          and writes[-1].ems_block == baseline.ems_block,
          "the original deadline did not restore the original baseline")
    clock["now"] = deadline + timedelta(seconds=1)
    await report(4, mode=0, floor=20, power=50)
    check(controller.record.state is S.ActiveState.IDLE
          and controller.record.last_transaction.rollback_status is S.RollbackStatus.CONFIRMED,
          "baseline restore did not require its own newer FC03")
    ended = notifier._model.observe(
        notifier._observation(controller.record, controller.last_frame)
    )
    if shorter:
        check(len(ended) == 1 and ended[0].kind == "end"
              and ended[0].outcome == "interrupted",
              "shorter plan STOP was falsely reported as completed")
        check(started[0].range_id == ended[0].range_id,
              "shorter plan changed the notification range")
        return
    check(ended == (), "natural restore closed the range before the continuity window")
    later_frame = replace(
        controller.last_frame, now=deadline + timedelta(seconds=121)
    )
    ended = notifier._model.observe(notifier._observation(controller.record, later_frame))
    check(len(ended) == 1 and ended[0].kind == "end", "restore lost one terminal event")
    if not shorter:
        check(ended[0].outcome == "completed", "natural restore lost completion")
    check(started[0].range_id == ended[0].range_id,
          "retarget changed the logical notification range")


def main():
    f = runpy.run_path(str(WORK / "tools" / "test_supervisor_sensor_contract.py"), run_name="sensor_fixture")
    asyncio.run(scenario(f))
    asyncio.run(scenario(f, shorter=True))
    asyncio.run(scenario(f, pre_dispatch_pending=True))
    print(f"PASS ({CHECKS} checks): real sensor callbacks, own FC03, immutable deadline/baseline; offline only")


if __name__ == "__main__":
    main()
