"""Regression tests for small but coherent Mode 4 charging."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from supervisor_active_controller import SupervisorActiveController
from supervisor_executor import ActiveState
from supervisor_runtime import TariffAction
from test_tariff_active_controller import NOW, execution_source, frame, physical


async def _closed_loop(
    power_percent: int,
    battery_sequence: tuple[float, ...],
    *,
    load_w: float = 500.0,
    soc: float = 60.0,
    target: float = 65.0,
    system_power_kw: float = 10.0,
    grid_override_w: float | None = None,
) -> tuple[list[str], list[int], list[str | None]]:
    writes: list[object] = []
    clock = [NOW]

    async def persist(_record: object) -> None:
        return None

    async def dispatch(write: object) -> None:
        writes.append(write)

    controller = SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: clock[0],
    )
    await controller.async_initialize()

    def make_frame(source: object):
        return frame(
            clock[0],
            source,
            controller.record,
            power=power_percent,
            target=target,
            action=TariffAction.GRID_SUPPORT_AND_CHARGE,
            current_run_grid_import_kwh=0.3,
            system_power_kw=system_power_kw,
            requested_charge_power_kw=(power_percent * system_power_kw / 100.0),
        )

    initial = make_frame(execution_source(NOW))
    assert initial.candidates[1].start_eligible
    await controller.async_reconcile(initial)
    states: list[str] = []
    proofs: list[str | None] = []
    for index, battery_w in enumerate(battery_sequence):
        clock[0] = NOW + timedelta(seconds=2 + index * 2)
        source = physical(
            clock[0],
            target=target,
            soc=soc,
            battery=battery_w,
            power=power_percent,
            generation=11 + index,
        )
        source = replace(
            source,
            load_power_w=load_w,
            grid_power_w=(
                battery_w - load_w
                if grid_override_w is None
                else grid_override_w
            ),
        )
        await controller.async_reconcile(make_frame(source))
        states.append(controller.record.state.value)
        transaction = controller.record.transaction
        proofs.append(
            transaction.physical_verification.status.value
            if transaction is not None and transaction.physical_verification is not None
            else None
        )
    return states, [int(write.ems_block.mode) for write in writes], proofs


async def main() -> None:
    for battery_w in (-100.0, -199.0, -200.0, -201.0):
        states, modes, proofs = await _closed_loop(1, (battery_w,))
        assert states == [ActiveState.EXECUTING.value], (battery_w, states, proofs)
        assert modes == [4]
        assert proofs == ["confirmed"]

    states, modes, proofs = await _closed_loop(30, (-500.0, -100.0))
    assert states == [ActiveState.EXECUTING.value, ActiveState.EXECUTING.value]
    assert modes == [4]
    assert proofs == ["confirmed", "confirmed"]

    states, modes, proofs = await _closed_loop(
        1, (-400.0,), system_power_kw=40.0
    )
    assert states == [ActiveState.EXECUTING.value]
    assert modes == [4]
    assert proofs == ["confirmed"]

    for total_import_w, expected in (
        (199.0, "pending"),
        (200.0, "pending"),
        (201.0, "confirmed"),
    ):
        states, _modes, proofs = await _closed_loop(
            1,
            (-100.0,),
            load_w=total_import_w - 100.0,
        )
        assert proofs == [expected], (total_import_w, states, proofs)

    # A real zero below target is not evidence of battery charging.
    states, _modes, proofs = await _closed_loop(1, (0.0,))
    assert states == [ActiveState.WAITING_READBACK.value]
    assert proofs == ["pending"]

    # Once SOC reached the requested target, holding in Mode 4 is sufficient.
    states, modes, proofs = await _closed_loop(1, (0.0,), soc=65.0)
    assert states == [ActiveState.EXECUTING.value]
    assert modes == [4]
    assert proofs == ["confirmed"]

    states, modes, proofs = await _closed_loop(1, (-100.0, 201.0))
    assert states == [ActiveState.EXECUTING.value, ActiveState.RESTORING.value]
    assert modes == [4, 0]
    assert proofs[0] == "confirmed"
    states, _modes, proofs = await _closed_loop(
        1, (-100.0,), grid_override_w=-300.0
    )
    assert proofs == ["contradicted"]
    print("Tariff low-power confirmation: 13 closed-loop scenarios passed")


if __name__ == "__main__":
    asyncio.run(main())
