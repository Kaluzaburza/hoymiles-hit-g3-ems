"""Live BMS limit must bind the encoded RCE command and its continuation."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import test_supervisor_active_controller as t
from supervisor_active_bridge import rce_sent_command_within_live_bms_limit


async def scenario() -> None:
    now = t.NOW
    clock = {"now": now + timedelta(milliseconds=100)}
    writes = []

    async def persist(_record):
        return None

    async def dispatch(write):
        writes.append(write)

    controller = t.SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: clock["now"],
    )
    await controller.async_initialize()
    deadline = now + timedelta(minutes=30)

    def execution(at, *, dcl=100.0, generation=11):
        observed = at - timedelta(milliseconds=500)
        return t.execution_source(
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
            bms_max_discharge_current_a=dcl,
            bms_discharge_current_observed_at=observed,
        )

    def frame(at, source, *, power=25.0, floor=41.0, active=False):
        return t.rce_frame(
            at, source, deadline=deadline, floor=floor, power=power,
            active=active, slot_end=now + timedelta(minutes=5),
        )

    await controller.async_reconcile(frame(now, t.execution_source(now)))
    assert controller.record.state is t.ActiveState.WAITING_READBACK
    at = now + timedelta(seconds=2)
    await controller.async_reconcile(frame(
        at, execution(at), active=False,
    ))
    assert controller.record.state is t.ActiveState.EXECUTING
    assert len(writes) == 1

    # 25% of 10 kW is above 51.2 V * 10 A * .95 = 0.4864 kW.
    at = now + timedelta(seconds=4)
    clock["now"] = at
    low = frame(at, execution(at, dcl=10, generation=12), active=True)
    assert not rce_sent_command_within_live_bms_limit(
        controller.record.transaction.intent, low.execution, rce=low.rce, now=at,
    )
    before = len(writes)
    await controller.async_reconcile(low)
    assert controller.record.state is not t.ActiveState.EXECUTING, (
        "a command above the live BMS limit remained authorized"
    )
    assert all(
        write.ems_block is None or write.ems_block.mode is not t.EmsMode.GRID_DISCHARGE
        for write in writes[before:]
    ), "BMS loss dispatched another RCE export block"

    # The current 25% block exceeds the cap, but a fresh 4% successor is
    # 0.4 kW and remains below the unchanged 0.4864 kW limit.
    writes.clear()
    clock["now"] = now + timedelta(milliseconds=100)
    controller = t.SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: clock["now"],
    )
    await controller.async_initialize()
    await controller.async_reconcile(frame(now, t.execution_source(now)))
    at = now + timedelta(seconds=2)
    await controller.async_reconcile(frame(at, execution(at), active=False))
    assert controller.record.state is t.ActiveState.EXECUTING
    at = now + timedelta(seconds=4)
    clock["now"] = at
    await controller.async_reconcile(frame(
        at, execution(at, dcl=10, generation=12), power=4.0,
        floor=42.0, active=True,
    ))
    assert controller.record.state is t.ActiveState.RETARGETING
    assert len(writes) == 2
    assert writes[-1].ems_block.maximum_discharge_power_percent_4306 == 4.0

    writes.clear()
    clock["now"] = now + timedelta(milliseconds=100)
    controller = t.SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: clock["now"],
    )
    await controller.async_initialize()
    await controller.async_reconcile(frame(now, t.execution_source(now)))
    at = now + timedelta(seconds=2)
    await controller.async_reconcile(frame(at, execution(at), active=False))
    at = now + timedelta(seconds=4)
    clock["now"] = at
    await controller.async_reconcile(frame(
        at, execution(at, dcl=10, generation=12), power=30.0,
        floor=42.0, active=True,
    ))
    assert all(
        write.ems_block is None
        or write.ems_block.mode is not t.EmsMode.GRID_DISCHARGE
        or write.ems_block.maximum_discharge_power_percent_4306 != 30.0
        for write in writes[1:]
    ), "3 kW RCE successor bypassed the 0.4864 kW live cap"


if __name__ == "__main__":
    asyncio.run(scenario())
    print("RCE live BMS continuation: PASS")
