#!/usr/bin/env python3
"""Virtual-time RCE cadence regression through real sensor callbacks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

import test_supervisor_sensor_contract as fixture


SENSOR = fixture.SENSOR
NOW = fixture.NOW
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


async def scenario() -> None:
    hass, entry, _runtime, sensor = fixture.environment()
    deadline = NOW + timedelta(seconds=600)
    writes: list[Any] = []
    observed_ems_ages: list[float] = []

    def entity_id(key: str) -> str:
        return fixture._source_entity_id(SENSOR._SOURCE_BY_KEY[key], entry.entry_id)

    def store(
        key: str,
        value: object,
        *,
        attributes: dict[str, Any] | None = None,
        reported: datetime = NOW,
    ) -> None:
        hass.states.values[entity_id(key)] = fixture.FakeState(
            str(value), attributes, reported
        )

    async def dispatch(write: Any) -> None:
        writes.append(write)

    async def drain_adapter() -> None:
        for _attempt in range(128):
            await asyncio.sleep(0)
            if (
                sensor._controller_task is None
                and sensor._pending_active_frame is None
            ):
                return
        raise AssertionError("sensor/controller callback queue did not drain")

    async def run_delays() -> None:
        for _attempt in range(16):
            pending = hass.active_delays()
            if not pending:
                await drain_adapter()
                return
            for handle in pending:
                handle.run()
            await drain_adapter()
        raise AssertionError("sensor debounce callbacks did not drain")

    async def run_due_points(at: datetime) -> None:
        for _attempt in range(64):
            due = sorted(
                (handle for handle in hass.active_points() if handle.when <= at),
                key=lambda handle: handle.when,
            )
            if not due:
                return
            for handle in due:
                handle.run()
                await drain_adapter()
        raise AssertionError("sensor point callbacks did not drain")

    def report_ems_block(at: datetime, generation: int, *, mode: int) -> None:
        for key, value in (
            ("ems_mode_readback", mode),
            ("self_use_soc_readback", 20),
            ("backup_soc_readback", 80),
            ("charge_soc_readback", 80),
            ("charge_power_ems_readback", 40),
            ("discharge_soc_readback", 41 if mode == 5 else 20),
            ("discharge_power_readback", 25.0 if mode == 5 else 50),
            ("ems_generation", generation),
        ):
            hass.fire_report(entity_id(key), fixture.FakeState(str(value), reported=at))

    def report_battery(at: datetime, *, discharging: bool = True) -> None:
        for key, value in (
            ("battery_soc", 90),
            ("bms_voltage", 51.2),
            ("bms_max_charge_current", 100),
            ("bms_max_discharge_current", 100),
            ("grid_power", 1200 if discharging else 0),
            ("battery_power", 900 if discharging else 0),
        ):
            hass.fire_state(entity_id(key), fixture.FakeState(str(value), reported=at))

    def report_settings(at: datetime, generation: int) -> None:
        for key, value in (
            ("gcf_enable_readback", 0),
            ("gcf_export_limit_readback", 100),
            ("gcf_generation", generation),
            ("machine_type", 0),
            ("inverter_count", 1),
            ("topology_generation", generation),
            ("charge_power_readback", 80),
            ("battery_charge_generation", generation),
        ):
            hass.fire_report(entity_id(key), fixture.FakeState(str(value), reported=at))

    plan = {
        **fixture._plan_attributes("rce_plan"),
        "input_revision": 7,
        "current_slot_planned": True,
        "current_slot_start_eligible": True,
        "current_slot_continue_eligible": True,
        "current_slot_end": deadline.isoformat(),
        "current_run_end": deadline.isoformat(),
        "current_slot_execution_discharge_power_kw": 2.50,
        "current_slot_planned_export_kwh": 10.0,
        "current_required_minimum_soc_percent": 41.0,
        "system_power_kw": 10.0,
    }
    for key, value in (
        ("supervisor_mode", "Active"),
        ("allow_rce", "on"),
        ("rce_enabled", "on"),
        ("rce_active", "off"),
        ("rce_control_data_ready", "on"),
        ("rce_price_above_threshold", "on"),
        ("rce_reserve_ready", "on"),
        ("sale_block_active", "off"),
        ("ems_execution_ready", "on"),
        ("rce_effective_discharge_power", "25.0"),
        ("rce_latched_minimum_soc", "41"),
        ("battery_soc", "90"),
        ("bms_voltage", "51.2"),
        ("bms_max_charge_current", "100"),
        ("bms_max_discharge_current", "100"),
        ("ems_mode_readback", "0"),
        ("ems_generation", "1"),
        ("self_use_soc_readback", "20"),
        ("backup_soc_readback", "80"),
        ("charge_soc_readback", "80"),
        ("charge_power_ems_readback", "40"),
        ("discharge_soc_readback", "20"),
        ("discharge_power_readback", "50"),
        ("gcf_enable_readback", "0"),
        ("gcf_export_limit_readback", "100"),
        ("gcf_generation", "1"),
        ("charge_power_readback", "80"),
        ("battery_charge_generation", "1"),
    ):
        store(key, value)
    store("rce_plan", "ready", attributes=plan)
    store(
        "rce_latched_slot_end",
        "ignored",
        attributes={"timestamp": deadline.timestamp()},
    )

    await sensor.add_to_platform_finish()
    controller = sensor._controller
    assert controller is not None
    controller._clock = lambda: fixture.CLOCK["now"]
    controller._dispatch = dispatch
    await drain_adapter()
    check(
        controller.record.state is SENSOR.ActiveState.WAITING_READBACK
        and len(writes) == 1
        and writes[0].ems_block.mode.value == 5,
        "real callback stack did not dispatch the initial RCE command",
    )

    original_reconcile = controller.async_reconcile

    async def traced_reconcile(frame: Any) -> Any:
        observed_at = frame.execution.full_block_generation_at
        if observed_at is not None:
            observed_ems_ages.append((frame.now - observed_at).total_seconds())
        return await original_reconcile(frame)

    controller.async_reconcile = traced_reconcile

    fixture.CLOCK["now"] = NOW + timedelta(seconds=1)
    report_ems_block(fixture.CLOCK["now"], 2, mode=5)
    hass.fire_state(
        entity_id("rce_active"),
        fixture.FakeState("on", reported=fixture.CLOCK["now"]),
    )
    report_battery(fixture.CLOCK["now"])
    await run_delays()
    transaction = controller.record.transaction
    assert transaction is not None
    transaction_id = transaction.transaction_id
    baseline = transaction.command_snapshot
    check(
        controller.record.state is SENSOR.ActiveState.EXECUTING
        and controller.record.owner is SENSOR.ExecutionOwner.RCE,
        "newer physical FC03 did not confirm the initial RCE command",
    )

    ems_times = [float(second) for second in range(6, 102, 5)]
    ems_times += [128.934]
    ems_times += [133.934 + 5.0 * index for index in range(26)]
    ems_times = [value for value in ems_times if value <= 260.0]
    battery_times = [3.0 + 13.0 * index for index in range(20)] + [125.318]
    battery_times = [value for value in battery_times if 1.0 < value <= 260.0]
    settings_times = [7.0 + 20.0 * index for index in range(13)]
    events = sorted(
        [(value, "ems") for value in ems_times]
        + [(value, "battery") for value in battery_times]
        + [(value, "settings") for value in settings_times],
        key=lambda item: (item[0], item[1]),
    )
    ems_generation = 2
    settings_generation = 1
    last_ems_at = NOW + timedelta(seconds=1)
    for offset, family in events:
        at = NOW + timedelta(seconds=offset)
        fixture.CLOCK["now"] = at
        await run_due_points(at)
        if family == "ems":
            ems_generation += 1
            report_ems_block(at, ems_generation, mode=5)
            last_ems_at = at
        elif family == "battery":
            report_battery(at)
        else:
            settings_generation += 1
            report_settings(at, settings_generation)
        await run_delays()
        check(
            controller.record.state is SENSOR.ActiveState.EXECUTING,
            f"healthy {family} callback stopped RCE at virtual t={offset:.3f}s: "
            f"{controller.record.state.value}/{controller.record.reason.value}",
        )

    transaction = controller.record.transaction
    assert transaction is not None
    check(
        (NOW + timedelta(seconds=128.934))
        - (NOW + timedelta(seconds=101.0))
        == timedelta(seconds=27.934)
        and any(abs(age - 24.318) < 0.001 for age in observed_ems_ages)
        and max(observed_ems_ages) < 30.0,
        "measured 24.318/27.934-second EMS gap was not exercised",
    )
    check(
        transaction.transaction_id == transaction_id
        and transaction.deadline == deadline
        and transaction.command_snapshot == baseline
        and controller.record.owner is SENSOR.ExecutionOwner.RCE
        and len(writes) == 1,
        "260-second callback cadence changed the lease, owner, baseline, or command",
    )

    # Keep the independent BAT/GCF evidence fresh while EMS falls silent.  The
    # sensor's real immutable watchdog must revoke continuation at EMS+30 s.
    for offset, family in ((263.0, "battery"), (267.0, "settings"), (276.0, "battery"), (287.0, "settings")):
        at = NOW + timedelta(seconds=offset)
        fixture.CLOCK["now"] = at
        await run_due_points(at)
        if family == "battery":
            report_battery(at)
        else:
            settings_generation += 1
            report_settings(at, settings_generation)
        await run_delays()
        check(
            controller.record.state is SENSOR.ActiveState.EXECUTING,
            "independent fresh evidence hid or prematurely triggered EMS expiry",
        )

    expiry = last_ems_at + timedelta(seconds=30, microseconds=1)
    fixture.CLOCK["now"] = expiry
    await run_due_points(expiry)
    check(
        controller.record.state is SENSOR.ActiveState.STOPPING
        and controller.record.reason is SENSOR.ExecutionReason.STALE_INPUTS
        and controller.record.owner is SENSOR.ExecutionOwner.RCE
        and len(writes) == 1,
        "no-event EMS watchdog did not fail closed without writing from stale FC03",
    )

    fresh_at = expiry + timedelta(seconds=1)
    fixture.CLOCK["now"] = fresh_at
    ems_generation += 1
    report_ems_block(fresh_at, ems_generation, mode=5)
    report_battery(fresh_at)
    await run_delays()
    check(
        controller.record.state is SENSOR.ActiveState.RESTORING
        and len(writes) == 2
        and writes[-1].ems_block.mode.value == 0
        and writes[-1].ems_block == baseline.ems_block,
        "fresh FC03 did not dispatch the exact preserved Self-Use baseline",
    )

    ack_at = fresh_at + timedelta(seconds=2)
    fixture.CLOCK["now"] = ack_at
    ems_generation += 1
    report_ems_block(ack_at, ems_generation, mode=0)
    hass.fire_state(
        entity_id("rce_active"),
        fixture.FakeState("off", reported=ack_at),
    )
    report_battery(ack_at, discharging=False)
    await run_delays()
    check(
        controller.record.state is SENSOR.ActiveState.IDLE
        and controller.record.owner is SENSOR.ExecutionOwner.NONE
        and controller.record.transaction is None,
        "newer physical baseline FC03 did not release the RCE owner",
    )


def main() -> None:
    asyncio.run(scenario())
    print(f"Supervisor RCE callback cadence: PASS ({CHECKS} checks, 260 s virtual)")


if __name__ == "__main__":
    main()
