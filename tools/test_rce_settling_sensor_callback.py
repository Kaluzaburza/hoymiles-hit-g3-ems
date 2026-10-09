"""Real Supervisor adapter/controller callbacks for the 185 LOAD transient."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import test_supervisor_sensor_contract as h


async def scenario(outcome: str, *, verify_lease: bool = False) -> None:
    hass, entry, _runtime, sensor = h.environment()
    now = h.NOW
    deadline = now + timedelta(minutes=5)
    writes = []
    requests = []
    market = {"value": "a" * 64}
    solver_release = asyncio.Event()

    def eid(key):
        return h._source_entity_id(h.SENSOR._SOURCE_BY_KEY[key], entry.entry_id)

    def store(key, value, attrs=None, at=None):
        hass.states.values[eid(key)] = h.FakeState(
            str(value), attrs, at or h.CLOCK["now"],
        )

    plan = {
        **h._plan_attributes("rce_plan"),
        "system_power_kw": 16.0,
        "current_slot_planned": True,
        "current_slot_start_eligible": True,
        "current_slot_continue_eligible": True,
        "current_slot_end": deadline.isoformat(),
        "current_run_end": deadline.isoformat(),
        "current_slot_execution_discharge_power_kw": 8.0,
        "current_slot_planned_export_kwh": 1.736,
        "current_required_minimum_soc_percent": 49,
        "current_slot_suppression_reason": "eligible",
        "current_slot_load_exhausts_requested_discharge_budget": False,
        "post_command_settling_market_fingerprint": market["value"],
    }

    def publish_plan():
        store("rce_plan", "ready", plan.copy())

    async def dispatch(write):
        writes.append(write)

    async def drain():
        for _ in range(80):
            await asyncio.sleep(0)
            if sensor._controller_task is None and sensor._pending_active_frame is None:
                return
        raise AssertionError("Controller adapter did not drain")

    async def replan():
        requests.append(h.CLOCK["now"])
        plan.update(result_current=False, recalculation_pending=True)
        publish_plan()
        sensor._recompute()
        await solver_release.wait()
        if outcome == "success":
            plan.update(
                result_current=True, recalculation_pending=False,
                input_revision=3, current_slot_planned=True,
                current_slot_start_eligible=True, current_slot_continue_eligible=True,
                current_run_end=deadline.isoformat(),
                current_slot_execution_discharge_power_kw=6.4,
                current_slot_planned_export_kwh=1.2,
                current_slot_suppression_reason="eligible",
                current_slot_load_exhausts_requested_discharge_budget=False,
            )
            store("rce_effective_discharge_power", 40)
            store("rce_control_data_ready", "on")
            store("rce_reserve_ready", "on")
            publish_plan()
            sensor._recompute()

    sensor.attach_rce_settling_source(lambda: market["value"], replan)
    for key, value in {
        "supervisor_mode": "Active", "allow_rce": "on", "rce_enabled": "on",
        "rce_active": "off", "rce_control_data_ready": "on",
        "rce_price_above_threshold": "on", "rce_reserve_ready": "on",
        "sale_block_active": "off", "ems_execution_ready": "on",
        "rce_requested_discharge_power": 50, "rce_effective_discharge_power": 50,
        "rce_latched_minimum_soc": 49, "battery_soc": 74,
        "bms_voltage": 53, "bms_max_discharge_current": 390,
        "ems_mode_readback": 0, "ems_generation": 1,
        "self_use_soc_readback": 30, "backup_soc_readback": 90,
        "charge_soc_readback": 70, "charge_power_ems_readback": 50,
        "discharge_soc_readback": 30, "discharge_power_readback": 50,
        "gcf_enable_readback": 1, "gcf_export_limit_readback": 100,
        "battery_power": 748, "load_power": 748, "grid_power": 0, "pv_power": 0,
    }.items():
        store(key, value)
    publish_plan()
    store("rce_latched_slot_end", "ignored", {"timestamp": deadline.timestamp()})
    await sensor.add_to_platform_finish()
    controller = sensor._controller
    assert controller is not None
    controller._clock = lambda: h.CLOCK["now"]
    controller._dispatch = dispatch
    await drain()
    tx = controller.record.transaction
    assert tx is not None and tx.command_sent_at is not None
    sent_at = tx.command_sent_at
    original_snapshot = tx.command_snapshot
    original_deadline = tx.deadline
    # The fake HA fixture does not run ESP lease renewals. Model a current
    # accepted lease when testing the separate same-owner retarget callback.
    controller._control_lease_valid_until = lambda: min(
        original_deadline, h.CLOCK["now"] + timedelta(seconds=30)
    )
    assert len(writes) == 1 and writes[0].ems_block.mode.value == 5
    if verify_lease:
        client = sensor._control_lease_client
        request = client.prepare_arm(transaction_id=tx.transaction_id,
            hard_deadline=tx.deadline,
            block=h.SENSOR._control_lease_block(tx.intent.command.ems_block),
            # Match the adapter's clock. A zero epoch is already expired
            # under the unified candidate's conservative local TTL check.
            challenge_nonce="00000001", now_wall=sent_at,
            now_monotonic=asyncio.get_running_loop().time())
        assert client.accept_arm(request, {"schema_version":1, "protocol_version":2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
            "accepted":True, "reason":"accepted", "renew_nonce":"00000002"})

    def refresh(seconds, *, power=50, soc_floor=49, mode=5):
        h.CLOCK["now"] = now + timedelta(seconds=seconds)
        for key in (
            "battery_soc", "bms_voltage", "bms_max_discharge_current",
            "self_use_soc_readback", "backup_soc_readback", "charge_soc_readback",
            "charge_power_ems_readback", "gcf_enable_readback", "gcf_export_limit_readback",
            "topology_generation", "machine_type", "inverter_count",
        ):
            state = hass.states.values[eid(key)]
            store(key, state.state, state.attributes)
        for key, value in {
            "ems_mode_readback": mode, "ems_generation": int(seconds) + 2,
            "discharge_soc_readback": soc_floor, "discharge_power_readback": power,
            "gcf_generation": int(seconds) + 2, "battery_power": 8000,
            "load_power": 7860 if seconds < 149 else 800,
            "grid_power": 0 if seconds < 149 else 7200, "pv_power": 0,
        }.items():
            store(key, value)

    refresh(5)
    store("rce_active", "on")
    sensor._recompute()
    await drain()
    assert controller.record.state is h.SENSOR.ActiveState.EXECUTING

    # Exactly the recorded solver31 pattern: new40% with LOAD7.86kW>6.4kW.
    refresh(26)
    store("rce_requested_discharge_power", 40)
    store("rce_effective_discharge_power", 0)
    store("rce_control_data_ready", "off")
    store("rce_reserve_ready", "off")
    plan.update(
        input_revision=2, current_slot_planned=False,
        current_slot_start_eligible=False, current_slot_continue_eligible=False,
        current_run_end=None, current_slot_execution_discharge_power_kw=0,
        current_slot_planned_export_kwh=0, current_required_minimum_soc_percent=50,
        current_slot_suppression_reason="no_current_plan",
        current_slot_load_exhausts_requested_discharge_budget=True,
    )
    publish_plan()
    sensor._recompute()
    await drain()
    assert controller.record.state is h.SENSOR.ActiveState.EXECUTING
    assert len(writes) == 1, "Transient cannot write40 or restore without a valid plan"
    assert controller.post_command_settling_deadline == sent_at + timedelta(seconds=180)
    if verify_lease:
        assert sensor._control_lease_renewal_evidence() is not None, sensor._control_lease_gate
    replan_handle = next(
        item for item in hass.active_points()
        if item.when == sent_at + timedelta(seconds=150, microseconds=1)
    )
    refresh(149)
    sensor._recompute()
    await drain()
    assert sensor._settling_replan_key == (tx.transaction_id, sent_at)
    assert replan_handle in hass.active_points(), "Physical refresh renewed150"

    if outcome in {"off", "market", "unload"}:
        if outcome == "off":
            store("allow_rce", "off")
        elif outcome == "market":
            market["value"] = None
        else:
            sensor._cleanup_lifecycle()
        if outcome != "unload":
            sensor._recompute()
            await drain()
            assert controller.record.state is not h.SENSOR.ActiveState.EXECUTING
        h.CLOCK["now"] = sent_at + timedelta(seconds=150, microseconds=1)
        # Even an already queued canceled callback may not start the solver.
        replan_handle.callback(h.CLOCK["now"])
        await drain()
        assert requests == []
        if outcome != "unload":
            sensor._cleanup_lifecycle()
        return

    if outcome in {"build_error", "unready", "cardinality", "reconcile_error"}:
        if outcome == "build_error":
            def failed_build(*args):
                raise ValueError("injected source snapshot failure")
            sensor._build_snapshots = failed_build
        elif outcome == "unready":
            sensor._guard_ready = False
        elif outcome == "cardinality":
            h.SENSOR._loaded_entry_count = lambda _hass: 2
        else:
            async def failed_reconcile(frame):
                raise ValueError("injected reconcile failure")
            controller.async_reconcile = failed_reconcile
        h.CLOCK["now"] = sent_at + timedelta(seconds=150, microseconds=1)
        replan_handle.run()
        task = sensor._settling_replan_task
        assert task is not None
        await task
        await drain()
        assert requests == [], "Failed fresh frame must not borrow previous authority"
        assert len(writes) == 1
        sensor._cleanup_lifecycle()
        return

    h.CLOCK["now"] = sent_at + timedelta(seconds=150, microseconds=1)
    replan_handle.run()
    for _ in range(80):
        await asyncio.sleep(0)
        if requests:
            break
    assert len(requests) == 1, "The real150 callback did not request one full replan"
    replan_handle.callback(h.CLOCK["now"])
    await drain()
    assert len(requests) == 1, "Duplicate callback restarted the solver"

    if outcome == "success":
        refresh(151)
        solver_release.set()
        if sensor._settling_replan_task is not None:
            await sensor._settling_replan_task
        await drain()
        assert len(writes) == 2 and all(w.ems_block.mode.value == 5 for w in writes)
        assert writes[1].ems_block.maximum_discharge_power_percent_4306 == 40
        refresh(155, power=40, soc_floor=50)
        sensor._recompute()
        await drain()
        current = controller.record.transaction
        assert current is not None
        assert controller.record.state is h.SENSOR.ActiveState.EXECUTING
        assert current.transaction_id == tx.transaction_id
        assert current.command_snapshot == original_snapshot and current.deadline == original_deadline
        assert current.command_sent_at > sent_at
        assert current.readback_result.value == "confirmed"
        assert len(writes) == 2, "ACK must not repeat the retarget write"
    else:
        # A blocked solver cannot turn this180s hold into another180s pending hold.
        refresh(179)
        sensor._recompute()
        await drain()
        assert controller.record.state is h.SENSOR.ActiveState.EXECUTING
        assert controller.execution_watchdog[1] == sent_at + timedelta(seconds=180)
        expiry = next(
            item for item in hass.active_points()
            if item.when == sent_at + timedelta(seconds=180, microseconds=1)
        )
        h.CLOCK["now"] = expiry.when
        expiry.run()
        await drain()
        assert controller.record.state is not h.SENSOR.ActiveState.EXECUTING
        assert len(writes) == 2 and writes[-1].ems_block.mode.value == 0
        assert len(requests) == 1
        solver_release.set()
        if sensor._settling_replan_task is not None:
            await sensor._settling_replan_task
    sensor._cleanup_lifecycle()


if __name__ == "__main__":
    asyncio.run(scenario("success", verify_lease=True))
    print("PASS real adapter lease gate during LOAD settling")
    loaded_count = h.SENSOR._loaded_entry_count
    for result in ("success", "pending_timeout", "off", "market", "unload",
                   "build_error", "unready", "cardinality", "reconcile_error"):
        try:
            asyncio.run(scenario(result))
            print(f"PASS real settling callback: {result}")
        finally:
            h.SENSOR._loaded_entry_count = loaded_count
