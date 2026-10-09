"""A04 regression through bounded Recorder -> production LOAD loader."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
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
NOW = datetime(2026, 9, 20, 12, tzinfo=WARSAW)
DAY = NOW.date() - timedelta(days=1)
START = datetime.combine(DAY, time.min, tzinfo=WARSAW)


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW.astimezone(tz or timezone.utc)


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


def _raw(
    injected: str, *, dense_phases: bool = False
) -> dict[str, list[SimpleNamespace]]:
    rows = {key: [] for key in history.LOAD_HISTORY_ENTITIES}
    for entity_id in history.LOAD_PHASE_ENERGY_ENTITIES:
        if dense_phases:
            rows[entity_id] = [
                SimpleNamespace(
                    state=str(8 * index / 287),
                    last_updated=START + timedelta(minutes=5 * index),
                )
                for index in range(288)
            ]
        else:
            rows[entity_id] = [
                SimpleNamespace(state="0", last_updated=START),
                SimpleNamespace(
                    state="8",
                    last_updated=START + timedelta(hours=23, minutes=55),
                ),
            ]
    target_rows = rows[history.LOAD_PHASE_ENERGY_ENTITIES[0]]
    target_rows[:] = [
        item for item in target_rows if item.last_updated != START + timedelta(hours=12)
    ]
    target_rows.append(
        SimpleNamespace(state=injected, last_updated=START + timedelta(hours=12))
    )
    target_rows.sort(key=lambda item: item.last_updated)
    rows[history.LOAD_PROFILE_ENERGY_ENTITY] = [
        SimpleNamespace(
            state=str(24 * index / 288),
            last_updated=START + timedelta(minutes=5 * index),
        )
        for index in range(288)
    ]
    return rows


async def _run(
    injected: str, *, dense_phases: bool = False
) -> tuple[object, list[float | None]]:
    rows = _raw(injected, dense_phases=dense_phases)
    sample_intervals: list[float | None] = []

    async def query(_hass, _start, _end, entities, **kwargs):
        sample_intervals.append(kwargs.get("sample_interval_seconds"))
        return {key: rows.get(key, []) for key in entities}

    namespace = {
        "datetime": FrozenDatetime,
        "ZoneInfo": ZoneInfo,
        "timedelta": timedelta,
        "time": time,
        "date": date,
        "dt_util": SimpleNamespace(
            as_utc=lambda value: value.astimezone(timezone.utc)
        ),
        "async_get_bounded_state_reports": query,
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
        "asyncio": asyncio,
        "LOAD_SHORT_LOOKBACK_DAYS": 5,
        "LOAD_EXTENDED_LOOKBACK_DAYS": 31,
        "LOAD_EXTENDED_CHUNK_DAYS": 7,
        "LOAD_EXTENDED_TOTAL_BUDGET_SECONDS": 30.0,
        "LOAD_HISTORY_RETRY_LIMIT": 3,
        "LOAD_HISTORY_RETRY_BACKOFF": timedelta(hours=1),
        "LOAD_HISTORY_RETRY_COOLDOWN": timedelta(hours=6),
        "RecorderHistoryQueryTimeout": RecorderHistoryQueryTimeout,
        "RecorderHistoryLimitExceeded": RecorderHistoryLimitExceeded,
        "_LOGGER": logging.getLogger("test_load_history_runtime_quality"),
    }
    refresh = _method(namespace)
    probe = SimpleNamespace(
        _ev_filter=lambda: SimpleNamespace(refresh=AsyncMock(return_value=None)),
        _charge_forecast=lambda: SimpleNamespace(refresh=AsyncMock(return_value=None)),
        _history_refresh_running=False,
        _full_history_refresh_date=None,
        _load_profile_generated_at=None,
        _load_history=history.LoadHistorySummary(None, 0, {}, None, 0, {}),
        _extended_load_history=history.LoadHistorySummary(None, 0, {}, None, 0, {}),
        hass=SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Warsaw")),
        _runtime=SimpleNamespace(),
    )
    await refresh(probe, force_full=True)
    return probe._load_history, sample_intervals


async def _run_query_failure(
    failure: type[RuntimeError], *, fail_profile_only: bool
) -> tuple[SimpleNamespace, list[tuple[str, ...]]]:
    rows = _raw("0")
    calls: list[tuple[str, ...]] = []

    async def query(_hass, _start, _end, entities, **_kwargs):
        requested = tuple(entities)
        calls.append(requested)
        if not fail_profile_only or requested == (history.LOAD_PROFILE_ENERGY_ENTITY,):
            raise failure("bounded query failed")
        return {key: rows.get(key, []) for key in entities}

    namespace = {
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
        "asyncio": asyncio,
        "LOAD_SHORT_LOOKBACK_DAYS": 5,
        "LOAD_EXTENDED_LOOKBACK_DAYS": 31,
        "LOAD_EXTENDED_CHUNK_DAYS": 7,
        "LOAD_EXTENDED_TOTAL_BUDGET_SECONDS": 30.0,
        "LOAD_HISTORY_RETRY_LIMIT": 3,
        "LOAD_HISTORY_RETRY_BACKOFF": timedelta(hours=1),
        "LOAD_HISTORY_RETRY_COOLDOWN": timedelta(hours=6),
        "_LOGGER": logging.getLogger("test_load_history_runtime_quality"),
    }
    refresh = _method(namespace)
    probe = SimpleNamespace(
        _ev_filter=lambda: SimpleNamespace(refresh=AsyncMock(return_value=None)),
        _charge_forecast=lambda: SimpleNamespace(refresh=AsyncMock(return_value=None)),
        _history_refresh_running=False,
        _full_history_refresh_date=None,
        _load_profile_generated_at=None,
        _load_history=history.LoadHistorySummary(None, 0, {}, None, 0, {}),
        _extended_load_history=history.LoadHistorySummary(None, 0, {}, None, 0, {}),
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
        hass=SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Warsaw")),
        _runtime=SimpleNamespace(),
    )
    await refresh(probe, force_full=True)
    return probe, calls


async def _run_hourly_rejected_night() -> tuple[SimpleNamespace, list[tuple[str, ...]]]:
    calls: list[tuple[str, ...]] = []

    async def query(_hass, start, end, entities, **_kwargs):
        requested = tuple(entities)
        calls.append(requested)
        if len(calls) == 1:
            return {key: [] for key in requested}
        midpoint = start.astimezone(WARSAW) + timedelta(hours=4)
        return {
            entity_id: [
                SimpleNamespace(state="2", last_updated=midpoint),
                SimpleNamespace(state="4", last_updated=end.astimezone(WARSAW)),
            ]
            for entity_id in requested
        }

    namespace = {
        "datetime": FrozenDatetime,
        "ZoneInfo": ZoneInfo,
        "timedelta": timedelta,
        "time": time,
        "date": date,
        "dt_util": SimpleNamespace(as_utc=lambda value: value.astimezone(timezone.utc)),
        "async_get_bounded_state_reports": query,
        "RecorderHistoryQueryTimeout": RecorderHistoryQueryTimeout,
        "RecorderHistoryLimitExceeded": RecorderHistoryLimitExceeded,
        "LOAD_PHASE_ENERGY_ENTITIES": history.LOAD_PHASE_ENERGY_ENTITIES,
        "LOAD_PROFILE_ENERGY_ENTITY": history.LOAD_PROFILE_ENERGY_ENTITY,
        "LOAD_HISTORY_ENTITIES": history.LOAD_HISTORY_ENTITIES,
        "isfinite": math.isfinite,
        "get_astral_event_date": lambda _hass, event, day: datetime.combine(
            day, time(6 if event == "sunrise" else 18), tzinfo=WARSAW
        ),
        "summarize_load_history": history.summarize_load_history,
        "parse_load_history_state": history.parse_load_history_state,
        "is_load_history_observation": history.is_load_history_observation,
        "merge_history": history_store.merge_history,
        "replace": replace,
        "asyncio": asyncio,
        "LOAD_SHORT_LOOKBACK_DAYS": 5,
        "LOAD_EXTENDED_LOOKBACK_DAYS": 31,
        "LOAD_EXTENDED_CHUNK_DAYS": 7,
        "LOAD_EXTENDED_TOTAL_BUDGET_SECONDS": 30.0,
        "LOAD_HISTORY_RETRY_LIMIT": 3,
        "LOAD_HISTORY_RETRY_BACKOFF": timedelta(hours=1),
        "LOAD_HISTORY_RETRY_COOLDOWN": timedelta(hours=6),
        "_LOGGER": logging.getLogger("test_load_history_runtime_quality"),
    }
    refresh = _method(namespace)
    empty = history.LoadHistorySummary(None, 0, {}, None, 0, {})
    probe = SimpleNamespace(
        _ev_filter=lambda: SimpleNamespace(refresh=AsyncMock(return_value=None)),
        _charge_forecast=lambda: SimpleNamespace(refresh=AsyncMock(return_value=None)),
        _history_refresh_running=False,
        _full_history_refresh_date=NOW.date(),
        _load_profile_generated_at=None,
        _load_history=empty,
        _extended_load_history=empty,
        _load_history_last_attempt_at=None,
        _load_history_last_success_at=None,
        _load_history_retry_count=0,
        _load_history_next_retry_at=None,
        _load_history_retry_epoch_date=None,
        _load_history_read_status="extended_complete",
        _load_history_read_error=None,
        _load_history_short_range=None,
        _load_history_extended_range=None,
        _load_history_extended_partial=False,
        hass=SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Warsaw")),
        _runtime=SimpleNamespace(),
    )
    await refresh(probe)
    return probe, calls


async def main() -> None:
    for invalid in ("nan", "inf", "unknown", "unavailable", "-1"):
        result, intervals = await _run(invalid)
        assert result.daily_history_days == 0, (invalid, result.daily_quality_by_date)
        assert result.profile_history_days == 0, (invalid, result.profile_quality_by_date)
        assert intervals == [None] * 10
    valid, intervals = await _run("0")
    assert valid.daily_history_days == 1
    assert valid.profile_history_days == 1
    assert abs(valid.average_daily_kwh - 24.0) < 0.01
    assert intervals == [None] * 10

    # This passes through the real Recorder adapter method, production
    # qualifier, merge, and versioned cache codec. A single explicit HA
    # availability state is bracketed by measured phase values 600 s apart;
    # it must not become a number and must not discard the complete day.
    available, intervals = await _run("unknown", dense_phases=True)
    assert available.daily_history_days == 1, available.daily_quality_by_date
    assert available.profile_history_days == 1
    assert abs(available.average_daily_kwh - 24.0) < 0.01
    identity = history_store.source_identity(
        entry_id="test-entry",
        entry_unique_id="test-unique",
        source_device_id="test-source",
        resolved_source_device_id="test-resolved",
        timezone="Europe/Warsaw",
    )
    encoded = history_store.encode_cache(
        available,
        identity=identity,
        generated_at=NOW,
    )
    restored, generated_at = history_store.decode_cache(
        encoded,
        expected_identity=identity,
        empty=history.LoadHistorySummary(None, 0, {}, None, 0, {}),
    )
    assert restored.daily_energy_kwh == available.daily_energy_kwh
    assert restored.profile_kwh_by_date == available.profile_kwh_by_date
    assert generated_at == NOW
    assert intervals == [None] * 10

    for failure in (RecorderHistoryQueryTimeout, RecorderHistoryLimitExceeded):
        degraded, calls = await _run_query_failure(failure, fail_profile_only=True)
        assert calls == [
            history.LOAD_PHASE_ENERGY_ENTITIES,
            (history.LOAD_PROFILE_ENERGY_ENTITY,),
        ]
        assert degraded._full_history_refresh_date is None
        assert degraded._load_profile_generated_at is not None
        assert degraded._load_history.daily_history_days == 0
        assert degraded._load_history.profile_history_days == 0
        assert len(degraded._load_history.partial_daily_energy_kwh) == 1
        assert (
            degraded._load_history_read_status
            == "short_phase_complete_dense_unavailable"
        )
        assert degraded._load_history_read_error == failure.__name__
        assert degraded._load_history_retry_count == 1

    required, calls = await _run_query_failure(
        RecorderHistoryQueryTimeout, fail_profile_only=False
    )
    assert calls == [history.LOAD_PHASE_ENERGY_ENTITIES]
    assert required._full_history_refresh_date is None
    assert required._load_profile_generated_at is None
    assert required._load_history.daily_history_days == 0
    assert required._load_history_read_status == "short_io_failed"

    rejected_night, calls = await _run_hourly_rejected_night()
    expected_key = (NOW.date() - timedelta(days=1)).isoformat()
    assert calls == [
        history.LOAD_PHASE_ENERGY_ENTITIES,
        history.LOAD_PHASE_ENERGY_ENTITIES,
    ]
    assert rejected_night._load_history.night_history_days == 0
    assert rejected_night._extended_load_history.night_history_days == 0
    assert rejected_night._load_history.night_quality_by_date[expected_key].startswith(
        "missing_or_reset:"
    )
    assert rejected_night._extended_load_history.night_quality_by_date == (
        rejected_night._load_history.night_quality_by_date
    )
    assert rejected_night._load_history_read_status == "extended_complete"
    assert rejected_night._load_profile_generated_at is None
    print("LOAD runtime quality: 11 Recorder paths passed")


if __name__ == "__main__":
    asyncio.run(main())
