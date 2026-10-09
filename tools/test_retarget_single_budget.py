"""Shared retarget attempt budget regressions for RCE and tariff owners."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
import runpy
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))
sys.path.insert(0, str(ROOT / "tools"))
fixture = runpy.run_path(
    str(ROOT / "tools" / "test_supervisor_active_controller.py"),
    run_name="retarget_budget_fixture",
)

C = fixture["controller_module"]
E = sys.modules["supervisor_executor"]
NOW = fixture["NOW"]

import test_tariff_active_controller as tariff_fixture  # noqa: E402


def old_rce_source(at, generation):
    observed = at - timedelta(milliseconds=100)
    return fixture["execution_source"](
        at,
        physical_mode_code=5,
        full_block_generation=generation,
        full_block_generation_at=observed,
        force_discharge_soc_percent=41,
        maximum_discharge_power_percent=99,
        grid_power_w=-2000,
        grid_power_observed_at=observed,
        battery_power_w=4000,
        battery_power_observed_at=observed,
        bms_max_discharge_current_a=1000,
    )


async def rce_case(
    kind: str,
    *,
    lease_seconds: float,
    persist_advance: float = 0.0,
    dispatch_advance: float = 0.0,
) -> tuple[object, list[object], object]:
    deadline = NOW + timedelta(minutes=30)
    now = [NOW]
    writes = []
    current = [None]
    lease_end = [NOW + timedelta(hours=1)]
    delay_retarget_persist = [False]
    reject_once = [kind == "stale_retry_expiry"]

    async def persist(record):
        if (
            delay_retarget_persist[0]
            and record.state is E.ActiveState.RETARGETING
            and record.transaction is not None
            and record.transaction.command_sent_at is None
        ):
            delay_retarget_persist[0] = False
            now[0] += timedelta(seconds=persist_advance)
            current[0] = fixture["rce_frame"](
                now[0],
                old_rce_source(now[0], 12),
                deadline=deadline,
                floor=42,
                power=100,
                active=True,
                slot_end=deadline,
            )

    async def dispatch(write):
        writes.append(write)
        if reject_once[0] and len(writes) == 2:
            reject_once[0] = False
            now[0] += timedelta(seconds=lease_seconds)
            current[0] = fixture["rce_frame"](
                now[0],
                old_rce_source(now[0], 14),
                deadline=deadline,
                floor=42,
                power=100,
                active=True,
                slot_end=deadline,
            )
            raise E.AtomicWriteNotQueued("stale_snapshot_generation")
        if dispatch_advance:
            now[0] += timedelta(seconds=dispatch_advance)
            current[0] = fixture["rce_frame"](
                now[0],
                old_rce_source(now[0], 14),
                deadline=deadline,
                floor=42,
                power=100,
                active=True,
                slot_end=deadline,
            )

    controller = C.SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: now[0],
        frame_resampler=lambda: current[0],
        control_lease_valid_until=lambda: lease_end[0],
    )
    await controller.async_initialize()
    current[0] = fixture["rce_frame"](
        now[0],
        # Match the high-power predecessor used below. The common START BMS
        # gate must pass before this fixture can exercise a retarget budget.
        fixture["execution_source"](now[0], bms_max_discharge_current_a=1000),
        deadline=deadline,
        floor=41,
        power=99,
        active=False,
        slot_end=deadline,
    )
    await controller.async_reconcile(current[0])
    now[0] += timedelta(seconds=2)
    current[0] = fixture["rce_frame"](
        now[0],
        old_rce_source(now[0], 11),
        deadline=deadline,
        floor=41,
        power=99,
        active=True,
        transaction_pending=True,
        slot_end=deadline,
    )
    await controller.async_reconcile(current[0])
    assert controller.record.state is E.ActiveState.EXECUTING
    predecessor = controller.record.transaction
    assert predecessor is not None

    now[0] += timedelta(seconds=1)
    retarget_started = now[0]
    lease_end[0] = retarget_started + timedelta(seconds=lease_seconds)
    current[0] = fixture["rce_frame"](
        now[0],
        old_rce_source(now[0], 12),
        deadline=deadline,
        floor=42,
        power=100,
        active=True,
        slot_end=deadline,
    )
    delay_retarget_persist[0] = bool(persist_advance)
    await controller.async_reconcile(current[0])
    return controller, writes, predecessor


async def tariff_case(*, lease_seconds: float, persist_advance: float):
    now = [NOW]
    writes = []
    current = [None]
    lease_end = [NOW + timedelta(hours=1)]
    delay_retarget_persist = [False]

    async def persist(record):
        if (
            delay_retarget_persist[0]
            and record.state is E.ActiveState.RETARGETING
            and record.transaction is not None
            and record.transaction.command_sent_at is None
        ):
            delay_retarget_persist[0] = False
            now[0] += timedelta(seconds=persist_advance)
            current[0] = tariff_fixture.frame(
                now[0],
                tariff_fixture.physical(now[0], target=58, generation=12),
                controller.record,
                target=65,
                action=tariff_fixture.TariffAction.GRID_SUPPORT_AND_CHARGE,
            )

    async def dispatch(write):
        writes.append(write)

    controller = C.SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: now[0],
        frame_resampler=lambda: current[0],
        control_lease_valid_until=lambda: lease_end[0],
    )
    await controller.async_initialize()
    current[0] = tariff_fixture.frame(
        now[0], tariff_fixture.execution_source(now[0])
    )
    await controller.async_reconcile(current[0])
    # Support enters at SOC60 minus two points. The predecessor readback must
    # match that real command before this test can exercise the retarget budget.
    assert writes[0].ems_block.force_charge_soc_percent_4303 == 58
    now[0] += timedelta(seconds=2)
    current[0] = tariff_fixture.frame(
        now[0],
        tariff_fixture.physical(now[0], target=58, generation=11),
        controller.record,
    )
    await controller.async_reconcile(current[0])
    assert controller.record.state is E.ActiveState.EXECUTING
    predecessor = controller.record.transaction
    assert predecessor is not None

    now[0] += timedelta(seconds=1)
    lease_end[0] = now[0] + timedelta(seconds=lease_seconds)
    current[0] = tariff_fixture.frame(
        now[0],
        tariff_fixture.physical(now[0], target=58, generation=12),
        controller.record,
        target=65,
        action=tariff_fixture.TariffAction.GRID_SUPPORT_AND_CHARGE,
    )
    delay_retarget_persist[0] = bool(persist_advance)
    await controller.async_reconcile(current[0])
    return controller, writes, predecessor


async def main() -> None:
    controller, writes, predecessor = await rce_case(
        "persist_expiry",
        lease_seconds=2.0,
        persist_advance=3.0,
    )
    assert len(writes) == 1, "retarget dispatched after persist consumed the lease budget"
    assert controller.record.state is E.ActiveState.EXECUTING
    assert controller.record.transaction.transaction_id == predecessor.transaction_id
    assert controller.record.transaction.command_snapshot == predecessor.command_snapshot
    assert controller.record.transaction.intent == predecessor.intent

    controller, writes, predecessor = await rce_case(
        "enough_budget",
        lease_seconds=10.0,
        persist_advance=1.0,
    )
    assert len(writes) == 2
    assert controller.record.state is E.ActiveState.RETARGETING
    assert controller.record.transaction.transaction_id == predecessor.transaction_id
    assert controller.record.transaction.command_snapshot == predecessor.command_snapshot

    controller, writes, _predecessor = await rce_case(
        "slow_transport",
        lease_seconds=2.0,
        dispatch_advance=3.0,
    )
    assert len(writes) == 2
    assert controller.record.state is E.ActiveState.FAULT
    assert controller.record.transaction.command_sent_at is None

    controller, writes, predecessor = await rce_case(
        "stale_retry_expiry",
        lease_seconds=2.0,
    )
    assert len(writes) == 2, "expired stale-snapshot attempt started a second transport"
    assert controller.record.state is E.ActiveState.EXECUTING
    assert controller.record.transaction.transaction_id == predecessor.transaction_id
    assert controller.record.transaction.command_snapshot == predecessor.command_snapshot
    assert controller.record.transaction.intent == predecessor.intent

    controller, writes, predecessor = await tariff_case(
        lease_seconds=2.0,
        persist_advance=3.0,
    )
    assert len(writes) == 1, "tariff retarget escaped the shared lease budget"
    assert controller.record.state is E.ActiveState.EXECUTING
    assert controller.record.transaction.transaction_id == predecessor.transaction_id
    assert controller.record.transaction.command_snapshot == predecessor.command_snapshot
    assert controller.record.transaction.intent == predecessor.intent

    controller, writes, predecessor = await tariff_case(
        lease_seconds=10.0,
        persist_advance=1.0,
    )
    assert len(writes) == 2
    assert controller.record.state is E.ActiveState.RETARGETING
    assert controller.record.transaction.transaction_id == predecessor.transaction_id
    assert controller.record.transaction.command_snapshot == predecessor.command_snapshot

    print(
        "PASS: shared RCE/tariff retarget budget "
        "(persist, transport, retry, positive)"
    )


if __name__ == "__main__":
    asyncio.run(main())
