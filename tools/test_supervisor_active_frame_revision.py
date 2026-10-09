"""Real adapter queue races across persisted executor lifecycle changes.

The Home Assistant fixture supplies synthetic sources; real runtime builders,
controller, persistence callbacks and adapter drain remain in the path.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import test_supervisor_sensor_contract as h


async def scenario(outcome: str) -> None:
    hass, entry, _runtime, sensor = h.environment()
    now = h.NOW
    deadline = now + timedelta(minutes=5)
    writes, transitions, queued = [], [], []

    def eid(key):
        return h._source_entity_id(h.SENSOR._SOURCE_BY_KEY[key], entry.entry_id)

    def store(key, value, attrs=None):
        hass.states.values[eid(key)] = h.FakeState(str(value), attrs, h.CLOCK["now"])

    plan = {
        **h._plan_attributes("rce_plan"), "system_power_kw": 16.0,
        "current_slot_planned": True, "current_slot_start_eligible": True,
        "current_slot_continue_eligible": True,
        "current_slot_end": deadline.isoformat(), "current_run_end": deadline.isoformat(),
        "current_slot_execution_discharge_power_kw": 7.84,
        "current_slot_planned_export_kwh": 1.82,
        "current_required_minimum_soc_percent": 46,
    }

    def publish_plan():
        store("rce_plan", "ready", plan.copy())

    for key, value in {
        "supervisor_mode": "Active", "allow_rce": "on", "rce_enabled": "on",
        "rce_active": "off", "rce_control_data_ready": "on",
        "rce_price_above_threshold": "on", "rce_reserve_ready": "on",
        "sale_block_active": "off", "ems_execution_ready": "on",
        "rce_requested_discharge_power": 50, "rce_effective_discharge_power": 49,
        "rce_latched_minimum_soc": 46, "battery_soc": 70,
        "bms_voltage": 53, "bms_max_discharge_current": 390,
        "ems_mode_readback": 0, "ems_generation": 2941,
        "self_use_soc_readback": 30, "backup_soc_readback": 90,
        "charge_soc_readback": 70, "charge_power_ems_readback": 50,
        "discharge_soc_readback": 30, "discharge_power_readback": 50,
        "gcf_enable_readback": 1, "gcf_export_limit_readback": 100,
        "battery_power": 579, "load_power": 579, "grid_power": 0, "pv_power": 0,
    }.items():
        store(key, value)
    publish_plan()
    store("rce_latched_slot_end", "ignored", {"timestamp": deadline.timestamp()})

    async def drain():
        for _ in range(120):
            await asyncio.sleep(0)
            if sensor._controller_task is None and sensor._pending_active_frame is None:
                return
        raise AssertionError("adapter drain did not terminate")

    async def dispatch(write):
        writes.append(write)

    await sensor.add_to_platform_finish()
    controller = sensor._controller
    assert controller is not None
    controller._clock = lambda: h.CLOCK["now"]
    controller._dispatch = dispatch
    # This test replaces the real leased ESP adapter with a local collector.
    controller._control_lease_valid_until = None
    await drain()
    initial = controller.record.transaction
    assert initial is not None and initial.command_sent_at is not None
    assert len(writes) == 1 and writes[0].ems_block.maximum_discharge_power_percent_4306 == 49
    original = initial.transaction_id, initial.started_at, initial.deadline, initial.command_snapshot
    original_persist = controller._persist
    original_build = sensor._build_snapshots
    original_recompute = sensor._recompute
    original_read = sensor._read_source_states
    original_count = h.SENSOR._loaded_entry_count
    original_accounting = sensor._async_publish_accounting
    cohort = []

    def change_inputs(kind):
        if kind == "changed_target":
            plan["current_slot_execution_discharge_power_kw"] = 6.4
            store("rce_effective_discharge_power", 40)
            store("rce_requested_discharge_power", 40)
            publish_plan()
        elif kind == "off":
            store("supervisor_mode", "Off")
        elif kind == "allow_off":
            store("allow_rce", "off")
        elif kind == "bms_zero":
            store("bms_max_discharge_current", 0)
        elif kind == "bms_missing":
            store("bms_voltage", "unavailable")
        elif kind == "off_grid":
            store("ems_mode_readback", 3)

    async def persist(record):
        transitions.append((record.state.value, record.reason.value))
        if (not queued and record.state is h.SENSOR.ActiveState.WAITING_READBACK
                and record.transaction.readback_result.value == "confirmed"):
            h.CLOCK["now"] = now + timedelta(seconds=5, milliseconds=220)
            if not outcome.startswith("cohort_"):
                change_inputs(outcome)
            sensor._recompute()
            queued.append(sensor._pending_active_frame)
            assert queued[0].rce.active_latched is False
            assert queued[0].context.transaction_pending is True
            if outcome.startswith("cohort_"):
                sensor._schedule_cohort_callback(source="state_reported")
                cohort.append((sensor._cohort_cancel, hass.delay_handles[-1]))
                # This source may be only part of the newer physical group.
                change_inputs(outcome.removeprefix("cohort_"))
                sensor._read_source_states = lambda: (_ for _ in ()).throw(
                    AssertionError("partial cohort was read before its trailing edge"))
            elif outcome == "rebuild_error":
                sensor._build_snapshots = lambda *_: (_ for _ in ()).throw(ValueError("rebuild failed"))
            elif outcome == "cardinality":
                h.SENSOR._loaded_entry_count = lambda _: 2
            elif outcome in {"rebuild_unavailable_pending", "rebuild_raises_pending"}:
                def incomplete_rebuild(**kwargs):
                    original_recompute(**kwargs)
                    assert sensor._pending_active_frame is not None
                    if outcome == "rebuild_raises_pending":
                        raise ValueError("failure after queuing replacement")
                    sensor._available = False
                sensor._recompute = incomplete_rebuild
        await original_persist(record)

    async def accounting(record, frame):
        if outcome == "accounting_off" and queued and len(queued) == 1:
            queued.append("new OFF during accounting")
            store("supervisor_mode", "Off")
            sensor._recompute()
        if outcome == "finally_reschedule" and queued and len(queued) == 1:
            queued.append("accounting failure")
            raise RuntimeError("force drain finally with an old pending revision")
        await original_accounting(record, frame)

    try:
        controller._persist = persist
        sensor._async_publish_accounting = accounting
        h.CLOCK["now"] = now + timedelta(seconds=5)
        for key in (
            "battery_soc", "bms_voltage", "bms_max_discharge_current", "self_use_soc_readback",
            "backup_soc_readback", "charge_soc_readback", "charge_power_ems_readback",
            "gcf_enable_readback", "gcf_export_limit_readback", "topology_generation",
            "machine_type", "inverter_count", "battery_power", "load_power", "grid_power", "pv_power",
        ):
            state = hass.states.values[eid(key)]
            store(key, state.state, state.attributes)
        for key, value in {"ems_mode_readback": 5, "ems_generation": 2942,
                           "discharge_soc_readback": 46, "discharge_power_readback": 49}.items():
            store(key, value)
        sensor._recompute()
        await drain()
        if cohort:
            assert len(writes) == 1 and controller.record.state is h.SENSOR.ActiveState.EXECUTING
            assert sensor._cohort_cancel is cohort[0][0], "cohort timer was replaced or cancelled"
            sensor._read_source_states = original_read
            # Exercise the registered real callback, rather than manual reconcile.
            handle = cohort[0][1]
            assert not handle.cancelled and handle.cancel_calls == 0
            h.CLOCK["now"] += timedelta(seconds=handle.when)
            handle.run()
            await drain()
        if outcome in {"healthy", "finally_reschedule", "cohort_healthy"}:
            assert controller.record.state is h.SENSOR.ActiveState.EXECUTING, transitions
            assert len(writes) == 1, "healthy lifecycle race sent a second command"
            assert sensor._latest_active_frame.rce.active_latched is True
            assert sensor._latest_active_frame.candidates[0].continuation_eligible is True
            for _ in range(3):
                h.CLOCK["now"] += timedelta(seconds=1)
                store("battery_power", 579)
                sensor._recompute()
                await drain()
                assert len(writes) == 1 and controller.record.state is h.SENSOR.ActiveState.EXECUTING
        elif outcome == "changed_target":
            assert controller.record.state is h.SENSOR.ActiveState.RETARGETING, transitions
            assert len(writes) == 2 and all(write.ems_block.mode.value == 5 for write in writes)
            assert writes[-1].ems_block.maximum_discharge_power_percent_4306 == 40
            assert controller.record.transaction.readback_result.value == "pending"
        elif outcome in {"rebuild_error", "cardinality", "rebuild_unavailable_pending", "rebuild_raises_pending"}:
            assert sensor._execution_adapter_error == "active_reconcile_failed"
            assert len(writes) == 1 and controller.record.state is h.SENSOR.ActiveState.EXECUTING
            assert not any(reason == "authorization_lost" for _, reason in transitions)
            assert sensor._pending_active_frame is None, "old frame reused after failed rebuild"
        else:
            assert controller.record.state is not h.SENSOR.ActiveState.EXECUTING, transitions
            assert all(write.ems_block.mode.value != 5 for write in writes[1:])
            if outcome.endswith("off_grid"):
                assert all(write.ems_block.mode.value == 3 for write in writes[1:]), "Off-Grid must not receive automatic Self-Use"
        tx = controller.record.transaction
        if tx is not None:
            assert (tx.transaction_id, tx.started_at, tx.deadline, tx.command_snapshot) == original
        assert hass.states.values[eid("rce_active")].state == "off", "test must not fake legacy ownership"
    finally:
        sensor._build_snapshots = original_build
        sensor._recompute = original_recompute
        sensor._read_source_states = original_read
        h.SENSOR._loaded_entry_count = original_count
        sensor._cleanup_lifecycle()


if __name__ == "__main__":
    for outcome in ("healthy", "changed_target", "off", "allow_off", "bms_zero", "bms_missing",
                    "off_grid", "cohort_healthy", "cohort_off", "cohort_bms_zero",
                    "cohort_off_grid", "rebuild_error", "cardinality", "finally_reschedule",
                    "rebuild_unavailable_pending", "rebuild_raises_pending", "accounting_off"):
        asyncio.run(scenario(outcome))
        print("PASS lifecycle frame revision:", outcome)
