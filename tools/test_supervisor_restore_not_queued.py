"""Correlated restore admission failures do not consume physical writes."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import runpy
from pathlib import Path


fixture = runpy.run_path(
    str(Path(__file__).with_name("test_supervisor_active_controller.py")),
    run_name="restore_fixture",
)


async def scenario(reason: str, *, late: bool = False) -> None:
    now = fixture["NOW"]
    writes: list[int] = []
    saves: list[int] = []

    async def persist(record) -> None:
        saves.append(record.revision)

    async def dispatch(write) -> None:
        writes.append(write.snapshot_generation)
        if reason == "timeout":
            raise TimeoutError("unknown transport outcome")
        if reason in {"malformed_response", "sent_partial"}:
            raise RuntimeError(reason)
        raise fixture["AtomicWriteNotQueued"](reason)

    controller = fixture["SupervisorActiveController"](
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: now,
    )
    await controller.async_initialize()

    def frame(generation: int, mode: int):
        source = fixture["execution_source"](
            now,
            physical_mode_code=mode,
            full_block_generation=generation,
            full_block_generation_at=now - timedelta(milliseconds=10),
        )
        return fixture["frame"](
            now,
            source,
            mode=fixture["SupervisorMode"].OFF,
            enabled=False,
        )

    await controller.async_master_stop(frame(10, 4))
    transaction = controller.record.transaction
    assert transaction is not None
    if reason in {
        "timeout", "malformed_response", "sent_partial",
        "lease_already_active", "uncorrelated_stale",
    }:
        assert controller.record.state is fixture["ActiveState"].FAULT
        assert controller.record.reason.value == "command_outcome_unknown"
        assert transaction.restore_attempts == 1
        assert transaction.restore_not_queued_count == 0
        assert writes == [10]
        return
    if reason == "previous_write_pending":
        assert controller.record.state is fixture["ActiveState"].STOPPING
        assert transaction.restore_attempts == 0
        return

    assert controller.record.state is fixture["ActiveState"].STOPPING
    assert transaction.restore_attempts == 0
    assert transaction.restore_not_queued_count == 1
    now += timedelta(milliseconds=100)
    await controller.async_reconcile(frame(10, 4))
    assert writes == [10], "same FC03 generation retried a rejected restore"
    now += timedelta(seconds=31 if late else 1)
    await controller.async_reconcile(frame(11, 4))
    transaction = controller.record.transaction
    assert transaction is not None
    assert controller.record.state is fixture["ActiveState"].FAULT
    assert controller.record.reason.value == "command_not_queued"
    assert transaction.restore_attempts == 0
    assert transaction.restore_not_queued_count == (1 if late else 2)
    if late:
        assert writes == [10], "admission timeout allowed a late retry"
        return
    now += timedelta(seconds=1)
    await controller.async_reconcile(frame(12, 4))
    assert writes == [10, 11], "admission retries were not bounded"
    frozen_revision = controller.record.revision
    frozen_saves = len(saves)
    for generation in (13, 14, 15):
        now += timedelta(seconds=1)
        await controller.async_reconcile(frame(generation, 4))
    assert controller.record.revision == frozen_revision
    assert len(saves) == frozen_saves


async def main() -> None:
    for reason in (
        "stale_snapshot_generation",
        "stale_snapshot",
        "previous_write_pending",
        "timeout",
        "malformed_response",
        "sent_partial",
        "lease_already_active",
        "uncorrelated_stale",
    ):
        await scenario(reason)
        print("PASS restore admission", reason)
    await scenario("stale_snapshot_generation", late=True)
    print("PASS restore admission fixed deadline")
    await previous_write_pending_budget(expire=True)
    await previous_write_pending_budget(expire=False)
    print("PASS previous writer has a fixed admission budget")


async def previous_write_pending_budget(*, expire: bool) -> None:
    now = fixture["NOW"]
    writes: list[int] = []

    async def persist(_record) -> None:
        return None

    async def dispatch(write) -> None:
        writes.append(write.snapshot_generation)
        raise fixture["AtomicWriteNotQueued"]("previous_write_pending")

    controller = fixture["SupervisorActiveController"](
        persist=persist, dispatch=dispatch, publish=lambda _record: None,
        clock=lambda: now,
    )
    await controller.async_initialize()

    def frame(generation: int):
        source = fixture["execution_source"](
            now, physical_mode_code=4, full_block_generation=generation,
            full_block_generation_at=now - timedelta(milliseconds=10),
        )
        return fixture["frame"](
            now, source, mode=fixture["SupervisorMode"].OFF, enabled=False,
        )

    await controller.async_master_stop(frame(10))
    assert controller.record.state is fixture["ActiveState"].STOPPING
    assert controller.record.transaction.restore_not_queued_count == 1
    now += timedelta(seconds=31 if expire else 1)
    await controller.async_reconcile(frame(10 if expire else 11))
    assert controller.record.state is fixture["ActiveState"].FAULT
    assert controller.record.reason.value == "command_not_queued"
    assert controller.record.transaction.restore_attempts == 0
    assert writes == ([10] if expire else [10, 11]), (
        "pending-writer admission exceeded its fixed time/count budget"
    )


if __name__ == "__main__":
    asyncio.run(main())
