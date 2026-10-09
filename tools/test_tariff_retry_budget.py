"""Closed-loop tariff retry guard after a physical execution failure."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import test_tariff_active_controller as f


async def main() -> None:
    sent = []
    clock = [f.NOW]

    async def persist(_record):
        return None

    async def dispatch(write):
        sent.append(write)

    controller = f.SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: clock[0],
    )
    await controller.async_initialize()

    def frame(source):
        return f.frame(
            clock[0],
            source,
            controller.record,
            target=65,
            action=f.TariffAction.GRID_SUPPORT_AND_CHARGE,
            input_revision=1,
        )

    await controller.async_reconcile(frame(f.execution_source(clock[0])))
    clock[0] += timedelta(seconds=2)
    await controller.async_reconcile(
        frame(f.physical(clock[0], target=65, battery=-5000))
    )
    assert controller.record.state is f.ActiveState.EXECUTING

    clock[0] += timedelta(seconds=5)
    await controller.async_reconcile(
        frame(f.physical(clock[0], target=65, battery=500, generation=12))
    )
    assert controller.record.state is f.ActiveState.RESTORING

    clock[0] += timedelta(seconds=5)
    neutral = f.execution_source(
        clock[0],
        full_block_generation=13,
        full_block_generation_at=clock[0] - timedelta(milliseconds=100),
    )
    await controller.async_reconcile(frame(neutral))
    assert controller.record.state is f.ActiveState.IDLE

    # A new generation one second later is not enough evidence that the cause
    # of the physical contradiction disappeared.
    clock[0] += timedelta(seconds=1)
    neutral = f.execution_source(
        clock[0],
        full_block_generation=14,
        full_block_generation_at=clock[0] - timedelta(milliseconds=100),
    )
    await controller.async_reconcile(frame(neutral))
    assert controller.record.state is f.ActiveState.IDLE
    assert [write.ems_block.mode.value for write in sent] == [4, 0]

    # Once spacing and a fresh neutral cohort exist, exactly one retry is
    # allowed and durably marked in its transaction id.
    clock[0] += timedelta(seconds=10)
    neutral = f.execution_source(
        clock[0],
        full_block_generation=15,
        full_block_generation_at=clock[0] - timedelta(milliseconds=100),
    )
    await controller.async_reconcile(frame(neutral))
    assert controller.record.state is f.ActiveState.WAITING_READBACK
    assert controller.record.transaction is not None
    assert controller.record.transaction.transaction_id.startswith(
        "tariff_retry1:"
    )

    clock[0] += timedelta(seconds=2)
    await controller.async_reconcile(
        frame(f.physical(clock[0], target=65, battery=-5000, generation=16))
    )
    assert controller.record.state is f.ActiveState.EXECUTING
    clock[0] += timedelta(seconds=5)
    await controller.async_reconcile(
        frame(f.physical(clock[0], target=65, battery=500, generation=17))
    )
    assert controller.record.state is f.ActiveState.RESTORING
    clock[0] += timedelta(seconds=5)
    neutral = f.execution_source(
        clock[0],
        full_block_generation=18,
        full_block_generation_at=clock[0] - timedelta(milliseconds=100),
    )
    await controller.async_reconcile(frame(neutral))
    assert controller.record.state is f.ActiveState.IDLE

    # The retry marker survives persistence in the normal transaction record,
    # so a later fresh cohort cannot create an unbounded third attempt.
    clock[0] += timedelta(seconds=16)
    neutral = f.execution_source(
        clock[0],
        full_block_generation=19,
        full_block_generation_at=clock[0] - timedelta(milliseconds=100),
    )
    recovered = f.SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        persisted_record=controller.record,
        clock=lambda: clock[0],
    )
    await recovered.async_initialize()
    await recovered.async_reconcile(
        f.frame(
            clock[0],
            neutral,
            recovered.record,
            target=65,
            action=f.TariffAction.GRID_SUPPORT_AND_CHARGE,
            input_revision=1,
        )
    )
    assert recovered.record.state is f.ActiveState.IDLE
    assert recovered.tariff_retry_status["reason"] == "retry_budget_exhausted"
    assert [write.ems_block.mode.value for write in sent] == [4, 0, 4, 0]

    print("Tariff retry budget: spacing and one-attempt ceiling passed")


if __name__ == "__main__":
    asyncio.run(main())
