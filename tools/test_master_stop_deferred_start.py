"""Regression tests for deferred MASTER STOP controller admission."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import test_supervisor_active_controller as fixtures
from supervisor_active_bridge import ActiveBridgeError
from supervisor_executor import MasterStopStatus


def _adapter_method():
    source = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "supervisor_sensor.py"
    )
    tree = ast.parse(source.read_text(encoding="utf-8"))
    sensor = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "HoymilesSupervisorSensor"
    )
    method = next(
        node
        for node in sensor.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_async_advance_master_stop"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            method,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace = {
        "ActiveBridgeError": ActiveBridgeError,
        "MasterStopStatus": MasterStopStatus,
    }
    exec(compile(module, str(source), "exec"), namespace)
    return namespace["_async_advance_master_stop"]


ADVANCE = _adapter_method()


async def _scenario(kind: str) -> tuple[object, object, list[object], str | None]:
    writes: list[object] = []
    clock = [fixtures.NOW]

    async def persist(_record: object) -> None:
        return None

    async def dispatch(write: object) -> None:
        writes.append(write)

    controller = fixtures.SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: clock[0],
    )
    await controller.async_initialize()
    sensor = SimpleNamespace(_master_stop_controller_start_pending=True)
    valid = fixtures.frame(
        clock[0],
        fixtures.execution_source(clock[0], physical_mode_code=4),
        enabled=False,
    )
    if kind == "invalid_first_settings":
        invalid = replace(
            valid,
            execution=replace(valid.execution, physical_mode_code=None),
        )
        await ADVANCE(sensor, controller, invalid, retry_blocked=True)
    elif kind == "manual_write_readback_pending":
        readback = {"ready": False}

        async def manual_dispatch() -> None:
            return None

        assert await controller.async_manual_proxy_dispatch(
            authority_valid=lambda: True,
            dispatch=manual_dispatch,
            readback_resolved=lambda: readback["ready"],
        )
        await ADVANCE(sensor, controller, valid, retry_blocked=True)
        readback["ready"] = True
    else:
        raise AssertionError(kind)

    assert sensor._master_stop_controller_start_pending is True
    assert not writes

    transaction_id = None
    for seconds in (1, 2, 5, 20):
        clock[0] = fixtures.NOW + timedelta(seconds=seconds)
        physical_mode = 0 if writes else 4
        recovered = fixtures.frame(
            clock[0],
            fixtures.execution_source(
                clock[0],
                physical_mode_code=physical_mode,
                full_block_generation=10 + seconds,
            ),
            enabled=False,
        )
        await ADVANCE(sensor, controller, recovered)
        current = controller.record.transaction
        if current is not None:
            transaction_id = transaction_id or current.transaction_id
            assert current.transaction_id == transaction_id

    # A repeated explicit click is idempotent and may not create a second write.
    await ADVANCE(sensor, controller, recovered, retry_blocked=True)
    return sensor, controller, writes, transaction_id


async def _permanent_invalid() -> None:
    writes: list[object] = []

    async def persist(_record: object) -> None:
        return None

    async def dispatch(write: object) -> None:
        writes.append(write)

    controller = fixtures.SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: fixtures.NOW,
    )
    await controller.async_initialize()
    sensor = SimpleNamespace(_master_stop_controller_start_pending=True)
    valid = fixtures.frame(
        fixtures.NOW,
        fixtures.execution_source(fixtures.NOW, physical_mode_code=4),
        enabled=False,
    )
    invalid = replace(valid, execution=replace(valid.execution, physical_mode_code=None))
    for _index in range(3):
        await ADVANCE(sensor, controller, invalid, retry_blocked=True)
    assert sensor._master_stop_controller_start_pending is True
    assert controller.record.master_stop_result.status is MasterStopStatus.NOT_REQUESTED
    assert not writes


async def main() -> None:
    for kind in ("invalid_first_settings", "manual_write_readback_pending"):
        sensor, controller, writes, transaction_id = await _scenario(kind)
        assert sensor._master_stop_controller_start_pending is False
        assert controller.record.master_stop_result.status is not MasterStopStatus.NOT_REQUESTED
        assert transaction_id is not None
        assert len(writes) == 1
    await _permanent_invalid()
    print("MASTER STOP deferred admission: 3 scenarios passed")


if __name__ == "__main__":
    asyncio.run(main())
