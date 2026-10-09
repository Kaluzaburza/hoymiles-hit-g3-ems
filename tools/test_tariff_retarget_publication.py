"""Tariff command replacement across real HA callbacks and lease challenge.

Offline reproduction of the 2026-10-08 unsent replacement failure shape.
The field did not retain the exact callback occurring during its challenge.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import timedelta
from types import SimpleNamespace

import test_supervisor_sensor_contract as h
from test_rce_unsent_retarget_cohort import FirmwareServices

S = h.SENSOR


async def scenario(case: str) -> None:
    hass, entry, runtime, sensor = h.environment()
    deadline = h.NOW + timedelta(minutes=10)
    source = SimpleNamespace(
        entry_id="esphome-source", domain="esphome",
        data={"device_name": "source-node"},
    )
    runtime.source_device.config_entry_id = source.entry_id
    hass.config_entries = SimpleNamespace(
        async_get_entry=lambda entry_id: source if entry_id == source.entry_id else None,
    )
    names = {
        "source_node_ems_supervisor_control_lease_challenge",
        "source_node_ems_supervisor_write_complete_block_leased",
        "source_node_ems_supervisor_renew_control_lease",
        *(f"source_node_{family.value}" for family in S.AtomicWriteFamily),
    }
    services = FirmwareServices(names, reject_arm_calls=set())
    hass.services = services

    def eid(key):
        return h._source_entity_id(S._SOURCE_BY_KEY[key], entry.entry_id)

    def store(key, value, attrs=None):
        hass.states.values[eid(key)] = h.FakeState(str(value), attrs, h.CLOCK["now"])

    def event(key, value, attrs=None, *, report=False):
        emit = hass.fire_report if report else hass.fire_state
        emit(eid(key), h.FakeState(str(value), attrs, h.CLOCK["now"]))

    async def drain():
        for _ in range(700):
            await asyncio.sleep(0.01)
            if sensor._controller_task is None and sensor._pending_active_frame is None:
                return
        raise AssertionError("Controller callback queue did not drain")

    def publications():
        return [handle for handle in hass.active_delays()
                if any(name in handle.callback.__qualname__
                       for name in ("planner_callback", "cohort_callback"))]

    async def flush():
        for _ in range(16):
            for handle in publications():
                handle.run()
            await drain()
            if not publications():
                return
        raise AssertionError("Debounce queue did not drain")

    def physical(generation, *, target=58, battery=0):
        for key, value in (
            ("ems_mode_readback", 4), ("self_use_soc_readback", 20),
            ("backup_soc_readback", 80), ("charge_soc_readback", target),
            ("charge_power_ems_readback", 60), ("discharge_soc_readback", 20),
            ("discharge_power_readback", 50), ("ems_generation", generation),
            ("battery_soc", 60), ("bms_voltage", 51.2),
            ("bms_max_charge_current", 240), ("bms_max_discharge_current", 100),
            ("pv_power", 0), ("load_power", 1200), ("battery_power", battery),
            ("grid_power", battery - 1200), ("machine_type", 0),
            ("inverter_count", 1), ("topology_generation", generation),
            ("gcf_enable_readback", 0), ("gcf_export_limit_readback", 100),
            ("gcf_generation", generation),
        ):
            event(key, value, report=True)

    plan = {
        **h._plan_attributes("tariff_plan"), "status_code": "ready",
        "current_slot_planned": True, "current_action": "grid_support",
        "current_run_need_class": "economic", "current_run_start_eligible": True,
        "current_run_continue_eligible": True, "command_charge_power_percent": 60.,
        "requested_charge_power_kw": 6., "target_soc_percent": 60.,
        "current_slot_end": deadline.isoformat(),
        "current_grid_charge_run_end": deadline.isoformat(),
        "model_input_maximum_soc_percent": 100., "control_inputs_fresh": True,
        "forecast_data_fresh": True, "bms_charge_power_limit_kw": 3.,
    }
    for key, value in (
        ("supervisor_mode", "Active"), ("allow_tariff", "on"),
        ("tariff_enabled", "on"), ("tariff_active", "off"),
        ("tariff_control_data_ready", "on"), ("tariff_planned_charge_slot", "on"),
        ("ems_execution_ready", "on"), ("battery_soc", 60), ("bms_voltage", 51.2),
        ("bms_max_charge_current", 240), ("bms_max_discharge_current", 100),
        ("ems_mode_readback", 0), ("ems_generation", 1),
        ("self_use_soc_readback", 20), ("backup_soc_readback", 80),
        ("charge_soc_readback", 80), ("charge_power_ems_readback", 40),
        ("discharge_soc_readback", 20), ("discharge_power_readback", 50),
        ("gcf_enable_readback", 0), ("gcf_export_limit_readback", 100),
        ("gcf_generation", 1),
    ):
        store(key, value)
    store("tariff_plan", "ready", plan)
    await sensor.add_to_platform_finish()
    controller = sensor._controller
    controller._clock = lambda: h.CLOCK["now"]
    await drain()
    assert services.arm_calls == 1, controller.record
    assert controller.record.state is S.ActiveState.WAITING_READBACK, controller.record
    h.CLOCK["now"] += timedelta(seconds=1)
    physical(2)
    await flush()
    assert controller.record.state is S.ActiveState.EXECUTING, controller.record
    predecessor = controller.record.transaction
    baseline = predecessor.command_snapshot
    original_id = predecessor.transaction_id
    original_started_at = predecessor.started_at
    completed = []

    def publication_during_challenge(number):
        if number != 2:
            return
        assert controller.record.state is S.ActiveState.RETARGETING
        assert controller.record.transaction.command_sent_at is None
        assert services.arm_calls == 1, "Successor was sent before publication"
        if case == "planner":
            plan["input_revision"] += 1
            event("tariff_plan", "ready", plan.copy())
            assert sensor._planner_cancel is not None
        else:
            event("ems_generation", 3 if case == "generation" else 2, report=True)
            assert sensor._cohort_cancel is not None
        pending = publications()
        assert pending

        def complete_publication():
            if case == "bms_zero":
                store("bms_max_charge_current", 0)
            elif case == "permission":
                store("allow_tariff", "off")
            elif case == "pause":
                hass.states.values[S.EMS_PAUSED_ENTITY_ID] = h.FakeState("on", reported=h.CLOCK["now"])
            elif case == "off_grid":
                store("ems_mode_readback", 3)
            elif case == "deadline":
                h.CLOCK["now"] = deadline + timedelta(microseconds=1)
            elif case == "ineligible_plan":
                plan.update(current_run_start_eligible=False, current_run_continue_eligible=False)
                store("tariff_plan", "ready", plan.copy())
            elif case == "stale_fc03":
                h.CLOCK["now"] += timedelta(seconds=16)
            elif case == "master_stop":
                sensor.request_master_stop()
            for handle in pending:
                handle.run()
            completed.append(True)

        if case not in {"pending_timeout", "lease_budget"}:
            asyncio.get_running_loop().call_soon(complete_publication)

    services.challenge_hook = publication_during_challenge
    h.CLOCK["now"] += timedelta(seconds=1)
    if case == "lease_budget":
        # Include the real outer controller timeout: the adapter's 5-second
        # publication wait must not overrun an almost expired predecessor.
        lease_end = h.CLOCK["now"] + timedelta(seconds=0.65)
        controller._control_lease_valid_until = lambda: lease_end
    plan.update(input_revision=2, current_action="grid_support_and_charge", target_soc_percent=65.)
    started = asyncio.get_running_loop().time()
    event("tariff_plan", "ready", plan.copy())
    for handle in publications():
        handle.run()
    await drain()
    elapsed = asyncio.get_running_loop().time() - started
    successor_calls = [data for _domain, service, data in services.calls
                       if service.endswith("write_complete_block_leased")
                       and data["mode_code"] == 4 and data["force_charge_soc"] == 65]
    if case in {"cohort", "planner", "generation"}:
        assert completed, "Scheduled HA publication was not processed"
        assert len(successor_calls) == 1, (
            "Qualified unsent tariff replacement was abandoned",
            case, controller.record.state, controller.record.reason, services.arm_calls,
        )
        assert services.arm_calls == 2, "Unexpected restore or duplicate successor"
        assert services.challenge_calls == (3 if case == "generation" else 2)
        assert successor_calls[0]["snapshot_generation"] == (3 if case == "generation" else 2)
        assert controller.record.state is S.ActiveState.RETARGETING
        transaction = controller.record.transaction
        assert transaction.transaction_id == original_id
        assert transaction.command_snapshot == baseline
        assert transaction.started_at == original_started_at
        assert transaction.deadline == deadline
        assert not any(data.get("mode_code") == 0 for _, _, data in services.calls)
        h.CLOCK["now"] += timedelta(seconds=1)
        physical(4, target=65, battery=-4000)
        await flush()
        assert controller.record.state is S.ActiveState.EXECUTING, controller.record
        assert sensor._control_lease_client.handle.command_generation == 2
        assert sensor._control_lease_renewal_evidence() is not None
    else:
        assert not successor_calls, f"Revoked or pending replacement reached transport: {case}"
        assert sensor._control_lease_renewal_evidence() is None
        if case == "pending_timeout":
            assert 4.8 <= elapsed < 6.5, f"Publication wait was unbounded: {elapsed}"
        elif case == "lease_budget":
            assert 0.3 <= elapsed < 1.5, f"Wait exceeded predecessor lease budget: {elapsed}"
    sensor._cleanup_lifecycle()
    print(f"PASS tariff retarget publication: {case}")


if __name__ == "__main__":
    cases = sys.argv[1:] or [
        "cohort", "planner", "generation", "bms_zero", "permission", "pause",
        "off_grid", "deadline", "ineligible_plan", "stale_fc03", "master_stop",
        "pending_timeout", "lease_budget",
    ]
    for case in cases:
        asyncio.run(scenario(case))
    print(f"{len(cases)} production-adapter tariff scenarios PASS (offline only)")
