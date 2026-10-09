"""Recorder Future ownership, timeout and cancellation without a database."""
from __future__ import annotations

import ast
import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]


async def main():
    source = ROOT / "custom_components/hoymiles_hit_modbus/diagnostics.py"
    names = {"DiagnosticHistoryQueryTimeoutError", "DiagnosticHistoryQueryBusyError",
             "_history_query_done", "_async_bounded_recorder_query"}
    parts = [node for node in ast.parse(source.read_text(encoding="utf8")).body
             if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and node.name in names]
    assert len(parts) == len(names)
    ns = {"asyncio": asyncio, "_ACTIVE_HISTORY_QUERY_TASKS": {}}
    exec(compile("from __future__ import annotations\n" +
                 "\n\n".join(ast.unparse(node) for node in parts), str(source), "exec"), ns)
    query = ns["_async_bounded_recorder_query"]
    active = ns["_ACTIVE_HISTORY_QUERY_TASKS"]
    loop = asyncio.get_running_loop()
    hass = object()
    queued = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        def enqueue(job):
            queued.append(job)
            # Same API contract as Recorder: synchronous enqueue, bare Future.
            result = loop.run_in_executor(pool, job)
            assert isinstance(result, asyncio.Future) and not isinstance(result, asyncio.Task)
            return result

        recorder = SimpleNamespace(async_add_executor_job=enqueue)
        assert await query(hass, recorder, lambda: 42, timeout_seconds=1) == 42
        await asyncio.sleep(0)
        assert not active

        async def coroutine_double(job):
            return job()
        assert await query(hass, SimpleNamespace(async_add_executor_job=coroutine_double),
                           lambda: 43, timeout_seconds=1) == 43

        release, started = threading.Event(), threading.Event()
        def slow():
            started.set()
            assert release.wait(3), "test failed to release worker"
            return 44

        try:
            try:
                await query(hass, recorder, slow, timeout_seconds=.02)
                raise AssertionError("timeout required")
            except ns["DiagnosticHistoryQueryTimeoutError"]:
                pass
            assert started.is_set() and id(hass) in active
            worker = active[id(hass)]
            count = len(queued)
            try:
                await query(hass, recorder, lambda: 45, timeout_seconds=.02)
                raise AssertionError("busy required")
            except ns["DiagnosticHistoryQueryBusyError"]:
                pass
            assert len(queued) == count and not worker.cancelled()
        finally:
            release.set()
        assert await worker == 44
        await asyncio.sleep(0)
        assert not active

        def broken():
            raise ValueError("worker failure")
        try:
            await query(hass, recorder, broken, timeout_seconds=1)
            raise AssertionError("exception must propagate")
        except ValueError:
            pass
        await asyncio.sleep(0)
        assert not active

        release.clear()
        started.clear()
        outer = asyncio.create_task(query(hass, recorder, slow, timeout_seconds=2))
        try:
            for _ in range(100):
                if id(hass) in active:
                    break
                await asyncio.sleep(.001)
            worker = active[id(hass)]
            outer.cancel()
            try:
                await outer
            except asyncio.CancelledError:
                pass
            assert active[id(hass)] is worker and not worker.cancelled()
        finally:
            release.set()
        assert await worker == 44
        await asyncio.sleep(0)
        assert not active
    print("PASS: bare Future, coroutine, timeout/single-flight, cleanup, exception, cancellation")


if __name__ == "__main__":
    asyncio.run(main())
