"""LOAD-HISTORY-RECOVERY-01 contract tests for the production refresh path."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from functools import partial
import logging
import math
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from unittest.mock import AsyncMock


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))
import rce_history as history
import load_history_store as history_store

WARSAW = ZoneInfo("Europe/Warsaw")
CLOCK = {"now": datetime(2026, 9, 21, 12, tzinfo=WARSAW)}


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return CLOCK["now"].astimezone(tz or timezone.utc)


class RecorderHistoryQueryTimeout(RuntimeError):
    pass


class RecorderHistoryLimitExceeded(RuntimeError):
    pass


def _method(namespace: dict[str, object]):
    source = COMPONENT / "rce_sensor.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_async_refresh_load_history"
    )
    method.decorator_list = []
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
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["_async_refresh_load_history"]


def _bounded_query_method(namespace: dict[str, object]):
    source = COMPONENT / "bounded_history.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_get_bounded_state_reports"
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
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["async_get_bounded_state_reports"]


def _fixture(
    now: datetime,
    *,
    days: int = 4,
) -> dict[str, list[SimpleNamespace]]:
    rows = {key: [] for key in history.LOAD_HISTORY_ENTITIES}
    for offset in range(days, 0, -1):
        day = now.date() - timedelta(days=offset)
        start = datetime.combine(day, time.min, tzinfo=WARSAW)
        for entity_id in history.LOAD_PHASE_ENERGY_ENTITIES:
            rows[entity_id].extend(
                [
                    SimpleNamespace(state="0", last_updated=start),
                    SimpleNamespace(
                        state="8",
                        last_updated=start + timedelta(hours=23, minutes=55),
                    ),
                ]
            )
        rows[history.LOAD_PROFILE_ENERGY_ENTITY].extend(
            SimpleNamespace(
                state=str(24 * index / 288),
                last_updated=start + timedelta(minutes=5 * index),
            )
            for index in range(288)
        )
    return rows


def _namespace(query):
    return {
        "asyncio": asyncio,
        "datetime": FrozenDatetime,
        "ZoneInfo": ZoneInfo,
        "timedelta": timedelta,
        "time": time,
        "date": date,
        "dt_util": SimpleNamespace(
            as_utc=lambda value: value.astimezone(timezone.utc)
        ),
        "async_get_bounded_state_reports": query,
        "RecorderHistoryQueryTimeout": RecorderHistoryQueryTimeout,
        "RecorderHistoryLimitExceeded": RecorderHistoryLimitExceeded,
        "LOAD_PHASE_ENERGY_ENTITIES": history.LOAD_PHASE_ENERGY_ENTITIES,
        "LOAD_PROFILE_ENERGY_ENTITY": history.LOAD_PROFILE_ENERGY_ENTITY,
        "LOAD_HISTORY_ENTITIES": history.LOAD_HISTORY_ENTITIES,
        "isfinite": math.isfinite,
        "get_astral_event_date": lambda _hass, event, day: datetime.combine(
            day,
            time(6 if event == "sunrise" else 18),
            tzinfo=WARSAW,
        ),
        "summarize_load_history": history.summarize_load_history,
        "parse_load_history_state": history.parse_load_history_state,
        "is_load_history_observation": history.is_load_history_observation,
        "merge_history": history_store.merge_history,
        "replace": replace,
        "LOAD_SHORT_LOOKBACK_DAYS": 5,
        "LOAD_EXTENDED_LOOKBACK_DAYS": 31,
        "LOAD_EXTENDED_CHUNK_DAYS": 7,
        "LOAD_EXTENDED_TOTAL_BUDGET_SECONDS": 30.0,
        "LOAD_HISTORY_RETRY_LIMIT": 3,
        "LOAD_HISTORY_RETRY_BACKOFF": timedelta(hours=1),
        "LOAD_HISTORY_RETRY_COOLDOWN": timedelta(hours=6),
        "_LOGGER": logging.getLogger("test_load_history_recovery"),
    }


def _probe() -> SimpleNamespace:
    return SimpleNamespace(
        _ev_filter=lambda: SimpleNamespace(refresh=AsyncMock(return_value=None)),
        _charge_forecast=lambda: SimpleNamespace(refresh=AsyncMock(return_value=None)),
        _history_refresh_running=False,
        _full_history_refresh_date=None,
        _load_profile_generated_at=None,
        _load_history_last_attempt_at=None,
        _load_history_last_success_at=None,
        _load_history_retry_count=0,
        _load_history_next_retry_at=None,
        _load_history_retry_epoch_date=None,
        _load_history_read_status="not_started",
        _load_history_read_error=None,
        _load_history_short_range=None,
        _load_history_extended_range=None,
        _load_history_extended_partial=False,
        _load_history=history.LoadHistorySummary(None, 0, {}, None, 0, {}),
        _extended_load_history=history.LoadHistorySummary(None, 0, {}, None, 0, {}),
        hass=SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Warsaw")),
        _runtime=SimpleNamespace(),
    )


def _slice(rows, start, end, entities):
    local_start = start.astimezone(WARSAW)
    local_end = end.astimezone(WARSAW)
    return {
        entity_id: [
            item
            for item in rows.get(entity_id, [])
            if local_start <= item.last_updated <= local_end
        ]
        for entity_id in entities
    }


async def _wide_timeout_keeps_short_model() -> None:
    rows = _fixture(CLOCK["now"])
    calls: list[float] = []
    fail_wide = True

    async def query(_hass, start, end, entities, **_kwargs):
        hours = (end - start).total_seconds() / 3600
        calls.append(hours)
        if (
            fail_wide
            and hours > 24 * 6
            and tuple(entities) == (history.LOAD_PROFILE_ENERGY_ENTITY,)
        ):
            raise RecorderHistoryQueryTimeout("wide profile timed out")
        return _slice(rows, start, end, entities)

    probe = _probe()
    await _method(_namespace(query))(probe, force_full=True)
    assert probe._load_history.daily_history_days == 4
    assert probe._load_history.profile_history_days == 4
    assert probe._extended_load_history.daily_history_days >= 4
    assert probe._load_history_extended_partial is True
    assert any(hours <= 24 * 6 for hours in calls), calls

    # The optional extension gets another bounded chance on the next daily
    # refresh; its earlier timeout does not poison the required short model.
    fail_wide = False
    CLOCK["now"] += timedelta(days=1)
    await _method(_namespace(query))(probe)
    assert probe._load_history_extended_partial is False
    assert probe._load_history_read_status == "extended_complete"


async def _successful_empty_refresh_does_not_renew_model_age() -> None:
    rows = _fixture(CLOCK["now"])
    return_empty = False

    async def query(_hass, start, end, entities, **_kwargs):
        if return_empty:
            return {entity_id: [] for entity_id in entities}
        return _slice(rows, start, end, entities)

    probe = _probe()
    refresh = _method(_namespace(query))
    await refresh(probe, force_full=True)
    generated_at = probe._load_profile_generated_at
    short_before = probe._load_history
    extended_before = probe._extended_load_history
    CLOCK["now"] += timedelta(days=1)
    return_empty = True
    await refresh(probe)
    assert probe._load_profile_generated_at == generated_at
    assert probe._load_history == short_before
    assert probe._extended_load_history == extended_before
    assert probe._load_history_read_status == "extended_no_new_data"
    assert probe._full_history_refresh_date == CLOCK["now"].date()


async def _same_day_recovery_without_restart() -> None:
    rows = _fixture(CLOCK["now"])
    profile_calls = 0
    events: list[str] = []

    async def query(_hass, start, end, entities, **_kwargs):
        nonlocal profile_calls
        hours = (end - start).total_seconds() / 3600
        events.append(f"query:{','.join(entities)}:{hours:.0f}")
        if tuple(entities) == (history.LOAD_PROFILE_ENERGY_ENTITY,):
            profile_calls += 1
            if profile_calls == 1:
                raise RecorderHistoryQueryTimeout("transient short failure")
        return _slice(rows, start, end, entities)

    probe = _probe()
    async def publish() -> None:
        events.append("publish")

    probe._runtime = SimpleNamespace(
        shared_inputs=SimpleNamespace(async_refresh=publish)
    )
    refresh = _method(_namespace(query))
    await refresh(probe, force_full=True)
    assert probe._full_history_refresh_date is None
    assert probe._load_history.daily_history_days == 0
    assert probe._load_history.profile_history_days == 0
    assert len(probe._load_history.partial_daily_energy_kwh) == 4
    assert set(probe._load_history.daily_quality_by_date.values()) == {
        "partial_missing_dense_counter"
    }
    assert probe._load_history_read_status == "short_phase_complete_dense_unavailable"
    assert probe._load_history_read_error == "RecorderHistoryQueryTimeout"
    assert probe._load_history_retry_count == 1
    assert probe._load_history_extended_partial is True
    assert events[-1] == "publish"
    assert len([event for event in events if event.startswith("query:")]) == 2
    CLOCK["now"] += timedelta(hours=1)
    await refresh(probe)
    assert probe._full_history_refresh_date == CLOCK["now"].date()
    assert probe._load_history.daily_history_days == 4
    assert probe._load_history.profile_history_days == 4
    assert profile_calls >= 2


async def _next_day_timeout_preserves_verified_days() -> None:
    rows = _fixture(CLOCK["now"])
    fail_short = False

    async def query(_hass, start, end, entities, **_kwargs):
        if fail_short and tuple(entities) == (history.LOAD_PROFILE_ENERGY_ENTITY,):
            raise RecorderHistoryQueryTimeout("next-day transient failure")
        return _slice(rows, start, end, entities)

    probe = _probe()
    refresh = _method(_namespace(query))
    await refresh(probe, force_full=True)
    assert probe._load_history.daily_history_days == 4
    before = dict(probe._load_history.daily_energy_kwh)
    generated_at = probe._load_profile_generated_at
    changed_day = CLOCK["now"].date() - timedelta(days=2)
    for item in rows[history.LOAD_PHASE_ENERGY_ENTITIES[0]]:
        if item.last_updated.date() == changed_day and item.state == "8":
            item.state = "7"
    CLOCK["now"] += timedelta(days=1)
    fail_short = True
    await refresh(probe)
    assert probe._load_history.daily_energy_kwh == before
    assert changed_day.isoformat() in before
    assert probe._load_profile_generated_at == generated_at
    observed_quality = set(probe._load_history.daily_quality_by_date.values())
    assert observed_quality == {
        "complete",
        "retained_complete_dense_timeout",
        "partial_phase_counter",
    }, observed_quality
    assert probe._full_history_refresh_date != CLOCK["now"].date()


async def _hourly_adds_finished_night_without_refreshing_daily_age() -> None:
    now = CLOCK["now"]
    night_date = now.date() - timedelta(days=1)
    night_start = datetime.combine(night_date, time(16, 30), tzinfo=WARSAW)
    midnight = datetime.combine(now.date(), time.min, tzinfo=WARSAW)
    rows = {key: [] for key in history.LOAD_PHASE_ENERGY_ENTITIES}
    for entity_id in history.LOAD_PHASE_ENERGY_ENTITIES:
        rows[entity_id] = [
            SimpleNamespace(state="2", last_updated=night_start - timedelta(minutes=5)),
            SimpleNamespace(state="5", last_updated=midnight - timedelta(minutes=5)),
            SimpleNamespace(state="5", last_updated=midnight + timedelta(minutes=2)),
            SimpleNamespace(state="0", last_updated=midnight + timedelta(minutes=5)),
            SimpleNamespace(state="1", last_updated=midnight + timedelta(hours=7, minutes=25)),
        ]
    calls: list[tuple[datetime, datetime, tuple[str, ...]]] = []

    async def query(_hass, start, end, entities, **_kwargs):
        calls.append((start, end, tuple(entities)))
        return _slice(rows, start, end, entities)

    probe = _probe()
    probe._full_history_refresh_date = now.date()
    generated_at = now - timedelta(days=1)
    probe._load_profile_generated_at = generated_at
    refresh = _method(_namespace(query))
    await refresh(probe)
    assert len(calls) == 2
    assert all(call[2] == history.LOAD_PHASE_ENERGY_ENTITIES for call in calls)
    assert probe._extended_load_history.night_energy_kwh == {
        night_date.isoformat(): 12.0
    }
    assert probe._load_history_read_status == "completed_night_added"
    assert probe._load_profile_generated_at == generated_at


async def _retry_budget_opens_a_new_bounded_epoch() -> None:
    rows = _fixture(CLOCK["now"])
    profile_calls = 0

    failing = True

    async def query(_hass, start, end, entities, **_kwargs):
        nonlocal profile_calls
        if tuple(entities) == (history.LOAD_PROFILE_ENERGY_ENTITY,):
            profile_calls += 1
            if failing:
                raise RecorderHistoryQueryTimeout("persistent short failure")
        return _slice(rows, start, end, entities)

    probe = _probe()
    refresh = _method(_namespace(query))
    for attempt in range(3):
        await refresh(probe, force_full=attempt == 0)
        assert probe._load_history_retry_count == attempt + 1
        CLOCK["now"] += timedelta(hours=1)

    cooldown_end = probe._load_history_next_retry_at
    assert cooldown_end == CLOCK["now"] - timedelta(hours=1) + timedelta(hours=6)
    await refresh(probe)
    assert profile_calls == 3
    assert probe._load_history_retry_count == 3
    assert probe._full_history_refresh_date is None
    assert probe._load_history_read_status == (
        "short_phase_complete_dense_unavailable"
    )
    assert len(probe._load_history.partial_daily_energy_kwh) == 4

    failing = False
    CLOCK["now"] = cooldown_end - timedelta(seconds=1)
    await refresh(probe, force_full=True)
    assert profile_calls == 3
    CLOCK["now"] = cooldown_end
    await refresh(probe, force_full=True)
    assert profile_calls >= 4
    assert probe._load_history_retry_count == 0
    assert probe._load_history_retry_epoch_date is None
    assert probe._full_history_refresh_date == CLOCK["now"].date()


async def _chunked_extension_matches_full_reference() -> None:
    rows = _fixture(CLOCK["now"], days=10)

    async def query(_hass, start, end, entities, **_kwargs):
        return _slice(rows, start, end, entities)

    probe = _probe()
    await _method(_namespace(query))(probe, force_full=True)

    samples = {
        entity_id: [
            (item.last_updated, float(item.state))
            for item in rows[entity_id]
        ]
        for entity_id in history.LOAD_HISTORY_ENTITIES
    }
    night_windows = {
        CLOCK["now"].date() - timedelta(days=offset): (
            datetime.combine(
                CLOCK["now"].date() - timedelta(days=offset),
                time(16, 30),
                tzinfo=WARSAW,
            ),
            datetime.combine(
                CLOCK["now"].date() - timedelta(days=offset - 1),
                time(7, 30),
                tzinfo=WARSAW,
            ),
        )
        for offset in range(29, 0, -1)
    }
    reference = history.summarize_load_history(
        samples,
        now=CLOCK["now"],
        night_windows=night_windows,
        current_day_window=None,
        history_days=28,
    )
    assert probe._load_history_read_status == "extended_complete"
    assert probe._load_history_extended_partial is False
    assert probe._extended_load_history.daily_energy_kwh == reference.daily_energy_kwh
    assert (
        probe._extended_load_history.daily_quality_by_date
        == reference.daily_quality_by_date
    )
    assert probe._extended_load_history.average_daily_kwh == reference.average_daily_kwh


async def _concurrent_consumers_do_not_block_startup() -> None:
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()
    active = 0
    maximum_active = 0
    call_count = 0

    class FakeRecorder:
        async def async_add_executor_job(self, _query):
            nonlocal active, maximum_active, call_count
            call_count += 1
            current_call = call_count
            active += 1
            maximum_active = max(maximum_active, active)
            try:
                if current_call == 1:
                    first_started.set()
                    await release_first.wait()
                else:
                    second_started.set()
                return [], False
            finally:
                active -= 1

    recorder = FakeRecorder()
    query = _bounded_query_method(
        {
            "asyncio": asyncio,
            "partial": partial,
            "get_recorder_instance": lambda _hass: recorder,
            "_query_state_reports": lambda *_args, **_kwargs: ([], False),
            "RECORDER_STATES_PER_ENTITY_LIMIT": 100,
            "RECORDER_QUERY_TIMEOUT_SECONDS": 1.0,
            "RecorderHistoryQueryTimeout": RecorderHistoryQueryTimeout,
            "RecorderHistoryLimitExceeded": RecorderHistoryLimitExceeded,
        }
    )
    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    first = asyncio.create_task(query(object(), start, end, ("sensor.first",)))
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    second = asyncio.create_task(query(object(), start, end, ("sensor.second",)))
    await asyncio.wait_for(second_started.wait(), timeout=1.0)
    assert maximum_active == 2
    release_first.set()
    await asyncio.gather(first, second)
    assert maximum_active == 2


async def _all_timed_out_workers_remain_guarded() -> None:
    started = [asyncio.Event() for _ in range(4)]
    release = [asyncio.Event() for _ in range(4)]
    active: set[int] = set()

    class FakeRecorder:
        def __init__(self, *, late_error: int | None = None) -> None:
            self.late_error = late_error
            self.calls = 0

        async def async_add_executor_job(self, _query):
            index = self.calls
            self.calls += 1
            active.add(index)
            started[index].set()
            try:
                await release[index].wait()
                if index == self.late_error:
                    raise RuntimeError("late Recorder worker failure")
                return [], False
            finally:
                active.discard(index)

    recorder = FakeRecorder(late_error=1)
    other = FakeRecorder()
    query = _bounded_query_method(
        {
            "asyncio": asyncio,
            "partial": partial,
            "get_recorder_instance": lambda hass: hass,
            "_query_state_reports": lambda *_args, **_kwargs: ([], False),
            "RECORDER_STATES_PER_ENTITY_LIMIT": 100,
            "RECORDER_QUERY_TIMEOUT_SECONDS": 0.01,
            "RecorderHistoryQueryTimeout": RecorderHistoryQueryTimeout,
            "RecorderHistoryLimitExceeded": RecorderHistoryLimitExceeded,
        }
    )
    start = datetime(2026, 9, 20, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)
    one = asyncio.create_task(
        query(recorder, start, end, ("sensor.one",), timeout_seconds=0.1)
    )
    await started[0].wait()
    two = asyncio.create_task(
        query(recorder, start, end, ("sensor.two",), timeout_seconds=0.2)
    )
    await started[1].wait()
    results = await asyncio.gather(one, two, return_exceptions=True)
    assert all(isinstance(item, RecorderHistoryQueryTimeout) for item in results)
    tracked = query._inflight_by_recorder[id(recorder)]
    assert len(tracked) == 2 and active == {0, 1}

    release[0].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert active == {1}
    assert len(query._inflight_by_recorder[id(recorder)]) == 1
    try:
        await query(recorder, start, end, ("sensor.blocked",))
    except RecorderHistoryQueryTimeout:
        pass
    else:
        raise AssertionError("surviving timed-out worker did not guard Recorder")
    assert recorder.calls == 2

    # A different Recorder instance remains independent.
    independent = asyncio.create_task(
        query(other, start, end, ("sensor.other",), timeout_seconds=0.1)
    )
    await started[0].wait()
    release[0].set()
    await independent

    release[1].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert id(recorder) not in query._inflight_by_recorder

    # Repeat with the later-timed-out worker finishing first.  The first
    # survivor must continue to guard its Recorder until it also finishes.
    started = [asyncio.Event() for _ in range(4)]
    release = [asyncio.Event() for _ in range(4)]
    active = set()
    reverse = FakeRecorder(late_error=0)
    one = asyncio.create_task(
        query(reverse, start, end, ("sensor.reverse_one",), timeout_seconds=0.1)
    )
    await started[0].wait()
    two = asyncio.create_task(
        query(reverse, start, end, ("sensor.reverse_two",), timeout_seconds=0.2)
    )
    await started[1].wait()
    results = await asyncio.gather(one, two, return_exceptions=True)
    assert all(isinstance(item, RecorderHistoryQueryTimeout) for item in results)
    release[1].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert active == {0}
    assert len(query._inflight_by_recorder[id(reverse)]) == 1
    try:
        await query(reverse, start, end, ("sensor.reverse_blocked",))
    except RecorderHistoryQueryTimeout:
        pass
    else:
        raise AssertionError("first timed-out worker stopped guarding too early")
    release[0].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert id(reverse) not in query._inflight_by_recorder

    # Cancelling the waiter must also retain its still-running worker.
    cancelled_waiter = asyncio.create_task(
        query(recorder, start, end, ("sensor.cancel",), timeout_seconds=1.0)
    )
    await started[2].wait()
    cancelled_waiter.cancel()
    try:
        await cancelled_waiter
    except asyncio.CancelledError:
        pass
    assert len(query._inflight_by_recorder[id(recorder)]) == 1
    release[2].set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert id(recorder) not in query._inflight_by_recorder


async def main() -> None:
    original_now = CLOCK["now"]
    try:
        await _wide_timeout_keeps_short_model()
        CLOCK["now"] = original_now
        await _successful_empty_refresh_does_not_renew_model_age()
        CLOCK["now"] = original_now
        await _same_day_recovery_without_restart()
        CLOCK["now"] = original_now
        await _next_day_timeout_preserves_verified_days()
        CLOCK["now"] = original_now
        await _hourly_adds_finished_night_without_refreshing_daily_age()
        CLOCK["now"] = original_now
        await _retry_budget_opens_a_new_bounded_epoch()
        CLOCK["now"] = original_now
        await _chunked_extension_matches_full_reference()
        CLOCK["now"] = original_now
        await _concurrent_consumers_do_not_block_startup()
        await _all_timed_out_workers_remain_guarded()
    finally:
        CLOCK["now"] = original_now
    print("LOAD history recovery: 10 contract paths passed")


if __name__ == "__main__":
    asyncio.run(main())
