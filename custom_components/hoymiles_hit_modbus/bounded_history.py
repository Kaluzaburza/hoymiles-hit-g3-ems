"""Bounded Recorder reads used by background optimizer warmups."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
import sqlite3
import time

from sqlalchemy import Integer, cast, func, select

from homeassistant.components.recorder import get_instance as get_recorder_instance
from homeassistant.components.recorder.db_schema import States
from homeassistant.components.recorder.util import session_scope
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


# The shipped firmware reports fast voltages every 13 s (about 33k reports in
# five days) and standard energy counters every 150 s (about 18k in 31 days).
# This budget preserves those intended horizons while rejecting pathological
# Recorder growth before a query can materialize an unbounded result.
RECORDER_STATES_PER_ENTITY_LIMIT = 50_000
RECORDER_QUERY_TIMEOUT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class RecorderStateSample:
    """Minimal Recorder row consumed by the optimizer history builders."""

    state: str
    last_updated: datetime


class RecorderHistoryLimitExceeded(RuntimeError):
    """Raised when a Recorder result reaches the explicit safety budget."""


class RecorderHistoryQueryTimeout(RuntimeError):
    """Raised when a bounded Recorder query does not finish in time."""


@contextmanager
def recorder_query_budget(session, deadline: float):
    """Stop SQLite work itself, not only the coroutine awaiting its thread.

    The read session owns this connection until the guard exits. Remove the
    handler before returning it to Recorder's pool. Other database backends
    retain their existing row limits and guarded asynchronous timeout.
    """
    if time.monotonic() >= deadline:
        raise TimeoutError("recorder_query_deadline")
    connection = None
    expired = False

    def progress():
        nonlocal expired
        expired = time.monotonic() >= deadline
        return int(expired)

    if session.get_bind().dialect.name == "sqlite":
        raw = session.connection().connection.driver_connection
        if isinstance(raw, sqlite3.Connection):
            connection = raw
            connection.set_progress_handler(progress, 1000)
    try:
        yield
    except Exception as err:
        # SQLAlchemy wraps sqlite3's interruption. Do not relabel unrelated
        # SQL errors or manufacture a successful partial history.
        original = getattr(err, "orig", err)
        if expired and isinstance(original, sqlite3.OperationalError):
            raise TimeoutError("recorder_query_deadline") from err
        raise
    finally:
        if connection is not None:
            connection.set_progress_handler(None, 0)


def _query_state_reports(
    hass: HomeAssistant,
    start_time: datetime,
    end_time: datetime,
    entity_id: str,
    limit: int,
    sample_interval_seconds: float | None,
    *,
    query_timeout_seconds: float = RECORDER_QUERY_TIMEOUT_SECONDS,
) -> tuple[list[RecorderStateSample], bool]:
    """Read all stored reports for one entity with an SQL-level limit."""
    start_timestamp = start_time.timestamp()
    end_timestamp = end_time.timestamp()
    deadline = time.monotonic() + query_timeout_seconds
    with session_scope(hass=hass, read_only=True) as session, recorder_query_budget(session, deadline):
        recorder = get_recorder_instance(hass)
        metadata_id = recorder.states_meta_manager.get(
            entity_id,
            session,
            False,
        )
        if metadata_id is None:
            return [], False

        columns = (States.state, States.last_updated_ts)
        previous = session.execute(
            select(*columns)
            .where(
                States.metadata_id == metadata_id,
                States.last_updated_ts <= start_timestamp,
            )
            .order_by(States.last_updated_ts.desc())
            .limit(1)
        ).first()
        if sample_interval_seconds is None:
            statement = select(*columns).where(
                States.metadata_id == metadata_id,
                States.last_updated_ts > start_timestamp,
                States.last_updated_ts < end_timestamp,
            )
        else:
            # Cumulative energy needs the last value in each real-time bucket.
            # The subquery keeps the row budget at SQL level instead of first
            # materialising every fast state report in the executor worker.
            bucket = cast(
                States.last_updated_ts / sample_interval_seconds,
                Integer,
            )
            latest = (
                select(func.max(States.last_updated_ts).label("sample_ts"))
                .where(
                    States.metadata_id == metadata_id,
                    States.last_updated_ts > start_timestamp,
                    States.last_updated_ts < end_timestamp,
                )
                .group_by(bucket)
            )
            statement = (
                select(*columns)
                .where(
                    States.metadata_id == metadata_id,
                    States.last_updated_ts.in_(latest),
                )
            )
        rows = list(
            session.execute(
                statement.order_by(States.last_updated_ts).limit(limit + 1)
            )
        )
        exceeded = len(rows) > limit
        if exceeded:
            return [], True
        if previous is not None:
            rows.insert(0, previous)
        return [
            RecorderStateSample(
                state=row.state,
                last_updated=dt_util.utc_from_timestamp(row.last_updated_ts),
            )
            for row in rows
            if row.state is not None and row.last_updated_ts is not None
        ], False


async def async_get_bounded_state_reports(
    hass: HomeAssistant,
    start_time: datetime,
    end_time: datetime,
    entity_ids: Sequence[str],
    *,
    limit_per_entity: int = RECORDER_STATES_PER_ENTITY_LIMIT,
    timeout_seconds: float = RECORDER_QUERY_TIMEOUT_SECONDS,
    sample_interval_seconds: float | None = None,
) -> dict[str, list[RecorderStateSample]]:
    """Return all stored reports with explicit time and row budgets.

    Repeated reports are intentionally preserved.  RCE uses them to prove
    counter coverage at time-window boundaries, while RCEm uses their presence
    in each 15-minute slot.  Asking SQL for one extra row makes a truncated
    result distinguishable from complete history; incomplete history is
    rejected rather than silently used by a controller.

    Queries are deliberately sequential. SQLite execution has its own progress
    deadline. Queued workers and other backends may still outlive the awaiting
    warmup, so the existing guard also tracks those unfinished workers.
    """
    if end_time <= start_time:
        raise ValueError("end_time must be later than start_time")
    if limit_per_entity < 1:
        raise ValueError("limit_per_entity must be positive")
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be positive")
    if sample_interval_seconds is not None and sample_interval_seconds <= 0:
        raise ValueError("sample_interval_seconds must be positive")

    recorder = get_recorder_instance(hass)
    # asyncio timeouts cannot stop the executor thread that is already inside
    # SQLite.  Track only a worker which actually outlived its timeout, then
    # refuse new bounded queries until that worker has really finished.
    # Ordinary RCE LOAD and RCEm voltage warmups are allowed to overlap: the
    # Recorder executor/SQLite coordinates those reads and blocking them here
    # can turn normal HA startup concurrency into a long serial queue.
    inflight_by_recorder = getattr(
        async_get_bounded_state_reports,
        "_inflight_by_recorder",
        None,
    )
    if inflight_by_recorder is None:
        inflight_by_recorder = {}
        setattr(
            async_get_bounded_state_reports,
            "_inflight_by_recorder",
            inflight_by_recorder,
        )
    recorder_key = id(recorder)

    def _consume_finished_workers() -> set[asyncio.Future]:
        tracked = inflight_by_recorder.get(recorder_key, set())
        for completed in tuple(tracked):
            if not completed.done():
                continue
            tracked.discard(completed)
            if not completed.cancelled():
                completed.exception()
        if tracked:
            inflight_by_recorder[recorder_key] = tracked
        else:
            inflight_by_recorder.pop(recorder_key, None)
        return tracked

    def _track_surviving_worker(worker: asyncio.Future) -> None:
        tracked = inflight_by_recorder.setdefault(recorder_key, set())
        tracked.add(worker)

        def _surviving_worker_done(completed: asyncio.Future) -> None:
            current = inflight_by_recorder.get(recorder_key)
            if current is not None:
                current.discard(completed)
                if not current:
                    inflight_by_recorder.pop(recorder_key, None)
            if not completed.cancelled():
                completed.exception()

        worker.add_done_callback(_surviving_worker_done)

    result: dict[str, list[RecorderStateSample]] = {}
    for entity_id in entity_ids:
        if any(not worker.done() for worker in _consume_finished_workers()):
            raise RecorderHistoryQueryTimeout(
                "Previous Recorder history worker is still running"
            )
        query = partial(
            _query_state_reports,
            hass,
            start_time,
            end_time,
            entity_id,
            limit_per_entity,
            sample_interval_seconds,
            query_timeout_seconds=timeout_seconds,
        )
        worker = asyncio.ensure_future(recorder.async_add_executor_job(query))

        try:
            states, exceeded = await asyncio.wait_for(
                asyncio.shield(worker),
                timeout=timeout_seconds,
            )
        except asyncio.CancelledError:
            _track_surviving_worker(worker)
            raise
        except TimeoutError as err:
            _track_surviving_worker(worker)
            raise RecorderHistoryQueryTimeout(
                f"Recorder history query timed out for {entity_id}"
            ) from err
        if exceeded:
            raise RecorderHistoryLimitExceeded(
                f"Recorder history exceeded {limit_per_entity} reports for "
                f"{entity_id}"
            )
        result[entity_id] = states
    return result
