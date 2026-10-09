"""Deterministic tests for recorder-backed RCE LOAD history."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "custom_components"
    / "hoymiles_hit_modbus"
    / "rce_history.py"
)
SPEC = importlib.util.spec_from_file_location("hoymiles_rce_history", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load the RCE history helpers")
HISTORY = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = HISTORY
SPEC.loader.exec_module(HISTORY)

WARSAW = ZoneInfo("Europe/Warsaw")


def add_dense_aggregate(
    samples: dict[str, list[tuple[datetime, float]]],
    day: date,
    total: float,
    *,
    reset_at: datetime | None = None,
) -> None:
    """Add five-minute reports by real elapsed time, including DST days."""
    start = datetime.combine(day, time.min, tzinfo=WARSAW)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=WARSAW)
    duration = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
    cursor = start.astimezone(timezone.utc)
    rows = samples.setdefault(HISTORY.LOAD_PROFILE_ENERGY_ENTITY, [])
    while cursor < end.astimezone(timezone.utc):
        local = cursor.astimezone(WARSAW)
        elapsed = (cursor - start.astimezone(timezone.utc)).total_seconds()
        value = total * elapsed / duration
        if reset_at is not None and local >= reset_at:
            value = max(value - total * 0.5, 0.0)
        rows.append((local, value))
        cursor += timedelta(minutes=5)


def complete_dense_day(day: date, total: float = 24.0):
    samples = {entity_id: [] for entity_id in HISTORY.LOAD_HISTORY_ENTITIES}
    start = datetime.combine(day, time.min, tzinfo=WARSAW)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=WARSAW)
    start_utc = start.astimezone(timezone.utc)
    duration = (end.astimezone(timezone.utc) - start_utc).total_seconds()
    cursor = start_utc
    while cursor < end.astimezone(timezone.utc):
        local = cursor.astimezone(WARSAW)
        fraction = (cursor - start_utc).total_seconds() / duration
        samples[HISTORY.LOAD_PROFILE_ENERGY_ENTITY].append(
            (local, total * fraction)
        )
        for entity_id in HISTORY.LOAD_PHASE_ENERGY_ENTITIES:
            samples[entity_id].append((local, total * fraction / 3.0))
        cursor += timedelta(seconds=150)
    return samples


def replace_with_availability_gap(
    rows: list[tuple[datetime, object]],
    left: datetime,
    bracket_seconds: int,
    *,
    category: str = "unknown",
) -> None:
    right_utc = left.astimezone(timezone.utc) + timedelta(
        seconds=bracket_seconds
    )
    right = right_utc.astimezone(WARSAW)
    before_value = max(
        value
        for stamp, value in rows
        if isinstance(value, (int, float))
        and stamp.astimezone(timezone.utc) <= left.astimezone(timezone.utc)
    )
    after_value = max(
        value
        for stamp, value in rows
        if isinstance(value, (int, float))
        and stamp.astimezone(timezone.utc) <= right_utc
    )
    rows[:] = [
        (stamp, value)
        for stamp, value in rows
        if not left.astimezone(timezone.utc)
        < stamp.astimezone(timezone.utc)
        <= right_utc
    ]
    rows.append((right, after_value))
    marker = (
        left.astimezone(timezone.utc) + timedelta(seconds=bracket_seconds / 2)
    ).astimezone(WARSAW)
    rows.append((marker, category))
    rows.sort(key=lambda item: item[0].astimezone(timezone.utc))
    assert before_value <= after_value


def test_daily_and_night_history_uses_phase_counters() -> None:
    """Four complete days and nights must be restored across daily resets."""
    now = datetime(2026, 8, 1, 12, 0, tzinfo=WARSAW)
    samples = {entity_id: [] for entity_id in HISTORY.LOAD_HISTORY_ENTITIES}
    totals = {
        date(2026, 7, 28): (10.0, 12.0, 2.0),
        date(2026, 7, 29): (11.0, 13.0, 3.0),
        date(2026, 7, 30): (12.0, 14.0, 4.0),
        date(2026, 7, 31): (13.0, 15.0, 5.0),
        date(2026, 8, 1): (3.0, 3.0, 1.0),
    }
    for day, phase_totals in totals.items():
        add_dense_aggregate(samples, day, sum(phase_totals))
        for entity_id, total in zip(
            HISTORY.LOAD_PHASE_ENERGY_ENTITIES,
            phase_totals,
            strict=True,
        ):
            samples[entity_id].extend(
                [
                    (
                        datetime.combine(day, time(0, 5), tzinfo=WARSAW),
                        0.0,
                    ),
                    (
                        datetime.combine(day, time(6, 25), tzinfo=WARSAW),
                        min(total, 1.0),
                    ),
                    (
                        datetime.combine(day, time(18, 55), tzinfo=WARSAW),
                        max(total - 2.0, min(total, 1.0)),
                    ),
                    (
                        datetime.combine(day, time(23, 55), tzinfo=WARSAW),
                        total,
                    ),
                ]
            )

    windows = {
        day: (
            datetime.combine(day, time(19, 0), tzinfo=WARSAW),
            datetime.combine(
                day + timedelta(days=1),
                time(6, 30),
                tzinfo=WARSAW,
            ),
        )
        for day in (
            date(2026, 7, 28),
            date(2026, 7, 29),
            date(2026, 7, 30),
            date(2026, 7, 31),
        )
    }
    result = HISTORY.summarize_load_history(
        samples,
        now=now,
        night_windows=windows,
    )

    assert result.daily_history_days == 4
    assert result.daily_energy_kwh == {
        "2026-07-28": 24.0,
        "2026-07-29": 27.0,
        "2026-07-30": 30.0,
        "2026-07-31": 33.0,
    }
    assert abs(result.average_daily_kwh - 28.5) < 1e-6
    assert result.night_history_days == 4
    assert abs(result.average_night_kwh - 8.75) < 1e-6
    assert len(result.average_profile_kwh) == 48
    assert abs(sum(result.average_profile_kwh) - 28.5) < 0.01
    assert result.weekday_profile_days == 4
    assert result.weekend_profile_days == 0
    assert len(result.weekday_profile_kwh) == 48


def test_current_day_window_uses_actual_phase_load() -> None:
    """Live daytime LOAD must be reconstructed from the phase counters."""
    now = datetime(2026, 8, 1, 12, 0, tzinfo=WARSAW)
    start = datetime(2026, 8, 1, 7, 30, tzinfo=WARSAW)
    samples = {entity_id: [] for entity_id in HISTORY.LOAD_HISTORY_ENTITIES}
    for entity_id, before, current in zip(
        HISTORY.LOAD_PHASE_ENERGY_ENTITIES,
        (2.0, 3.0, 1.0),
        (5.0, 7.0, 2.5),
        strict=True,
    ):
        samples[entity_id] = [
            (start - timedelta(minutes=5), before),
            (now - timedelta(minutes=5), current),
        ]

    result = HISTORY.summarize_load_history(
        samples,
        now=now,
        night_windows={},
        current_day_window=(start, now),
    )

    assert abs(result.current_day_energy_kwh - 8.5) < 1e-6


def test_extended_history_exposes_28_complete_days_and_profiles() -> None:
    """The daily cache may retain 28 days without changing profile shape."""
    now = datetime(2026, 8, 31, 12, 0, tzinfo=WARSAW)
    samples = {entity_id: [] for entity_id in HISTORY.LOAD_HISTORY_ENTITIES}
    for offset in range(28, 0, -1):
        day = now.date() - timedelta(days=offset)
        add_dense_aggregate(samples, day, 12.0)
        for phase, entity_id in enumerate(
            HISTORY.LOAD_PHASE_ENERGY_ENTITIES,
            start=1,
        ):
            samples[entity_id].extend(
                [
                    (datetime.combine(day, time(0, 5), tzinfo=WARSAW), 0.0),
                    (
                        datetime.combine(day, time(12, 5), tzinfo=WARSAW),
                        float(phase),
                    ),
                    (
                        datetime.combine(day, time(23, 55), tzinfo=WARSAW),
                        float(phase * 2),
                    ),
                ]
            )
    result = HISTORY.summarize_load_history(
        samples,
        now=now,
        night_windows={},
        history_days=28,
    )
    assert result.daily_history_days == 28
    assert len(result.daily_energy_kwh) == 28
    assert result.weekday_profile_days + result.weekend_profile_days == 28
    assert len(result.average_profile_kwh) == 48
    assert abs(sum(result.average_profile_kwh) - 12.0) < 0.01


def test_sparse_or_reset_data_keeps_partial_total_without_fake_profile() -> None:
    day = date(2026, 8, 1)
    now = datetime(2026, 8, 2, 12, 0, tzinfo=WARSAW)
    samples = {entity_id: [] for entity_id in HISTORY.LOAD_HISTORY_ENTITIES}
    for entity_id in HISTORY.LOAD_PHASE_ENERGY_ENTITIES:
        samples[entity_id] = [
            (datetime.combine(day, time.min, tzinfo=WARSAW), 0.0),
            (datetime.combine(day, time(23, 55), tzinfo=WARSAW), 8.0),
        ]
    sparse = HISTORY.summarize_load_history(
        samples, now=now, night_windows={}, history_days=1
    )
    assert sparse.daily_history_days == 0
    assert sparse.partial_daily_energy_kwh == {day.isoformat(): 24.0}
    assert sparse.profile_history_days == 0
    assert sparse.average_profile_kwh == ()

    add_dense_aggregate(
        samples,
        day,
        24.0,
        reset_at=datetime.combine(day, time(12), tzinfo=WARSAW),
    )
    reset = HISTORY.summarize_load_history(
        samples, now=now, night_windows={}, history_days=1
    )
    assert reset.daily_history_days == 0
    assert reset.profile_history_days == 0
    assert "reset" in reset.profile_quality_by_date[day.isoformat()]


def test_delayed_midnight_reset_excludes_carryover_and_keeps_night() -> None:
    previous = date(2026, 7, 31)
    day = date(2026, 8, 1)
    now = datetime(2026, 8, 2, 12, 0, tzinfo=WARSAW)
    samples = {entity_id: [] for entity_id in HISTORY.LOAD_HISTORY_ENTITIES}
    add_dense_aggregate(samples, day, 18.0)
    for entity_id in HISTORY.LOAD_PHASE_ENERGY_ENTITIES:
        samples[entity_id] = [
            (datetime.combine(previous, time(18, 55), tzinfo=WARSAW), 5.0),
            (datetime.combine(previous, time(23, 55), tzinfo=WARSAW), 8.0),
            (datetime.combine(day, time(0, 2), tzinfo=WARSAW), 8.0),
            (datetime.combine(day, time(0, 5), tzinfo=WARSAW), 0.0),
            (datetime.combine(day, time(6, 25), tzinfo=WARSAW), 1.0),
            (datetime.combine(day, time(23, 55), tzinfo=WARSAW), 6.0),
        ]
    result = HISTORY.summarize_load_history(
        samples,
        now=now,
        night_windows={
            previous: (
                datetime.combine(previous, time(19), tzinfo=WARSAW),
                datetime.combine(day, time(6, 30), tzinfo=WARSAW),
            )
        },
        history_days=1,
    )
    assert result.daily_energy_kwh == {day.isoformat(): 18.0}
    assert result.night_energy_kwh == {previous.isoformat(): 12.0}
    assert all(
        reason == "complete_delayed_midnight_rollover"
        for reason in result.phase_quality_by_date[day.isoformat()].values()
    )
    diagnostics = result.phase_diagnostics_by_date[day.isoformat()]
    assert all(row["delayed_midnight_rollover"] for row in diagnostics.values())
    assert all(row["reset_at"].endswith("00:05:00+02:00") for row in diagnostics.values())
    assert all(row["accepted_first_at"].endswith("00:05:00+02:00") for row in diagnostics.values())
    assert all(row["minimum_kwh"] == 0.0 and row["maximum_kwh"] == 8.0 for row in diagnostics.values())


def test_out_of_order_samples_and_dst_preserve_energy() -> None:
    for day in (date(2026, 3, 29), date(2026, 10, 25)):
        now = datetime.combine(day + timedelta(days=1), time(12), tzinfo=WARSAW)
        samples = {entity_id: [] for entity_id in HISTORY.LOAD_HISTORY_ENTITIES}
        add_dense_aggregate(samples, day, 24.0)
        for entity_id in HISTORY.LOAD_PHASE_ENERGY_ENTITIES:
            samples[entity_id] = [
                (datetime.combine(day, time(23, 55), tzinfo=WARSAW), 8.0),
                (datetime.combine(day, time.min, tzinfo=WARSAW), 0.0),
            ]
        result = HISTORY.summarize_load_history(
            samples, now=now, night_windows={}, history_days=1
        )
        assert result.daily_history_days == 1
        assert result.profile_history_days == 1
        assert abs(sum(result.average_profile_kwh) - 24.0) < 0.01


def test_nonfinite_phase_invalidates_complete_day() -> None:
    day = date(2026, 8, 1)
    samples = {entity_id: [] for entity_id in HISTORY.LOAD_HISTORY_ENTITIES}
    add_dense_aggregate(samples, day, 24.0)
    for entity_id in HISTORY.LOAD_PHASE_ENERGY_ENTITIES:
        samples[entity_id] = [
            (datetime.combine(day, time.min, tzinfo=WARSAW), 0.0),
            (datetime.combine(day, time(23, 55), tzinfo=WARSAW), 8.0),
        ]
    samples[HISTORY.LOAD_PHASE_ENERGY_ENTITIES[0]].append(
        (datetime.combine(day, time(12), tzinfo=WARSAW), float("nan"))
    )
    result = HISTORY.summarize_load_history(
        samples,
        now=datetime.combine(day + timedelta(days=1), time(12), tzinfo=WARSAW),
        night_windows={},
        history_days=1,
    )
    assert result.daily_history_days == 0


def test_nonfinite_dense_counter_invalidates_profile() -> None:
    day = date(2026, 8, 1)
    samples = {entity_id: [] for entity_id in HISTORY.LOAD_HISTORY_ENTITIES}
    add_dense_aggregate(samples, day, 24.0)
    samples[HISTORY.LOAD_PROFILE_ENERGY_ENTITY].append(
        (datetime.combine(day, time(12), tzinfo=WARSAW), float("nan"))
    )
    for entity_id in HISTORY.LOAD_PHASE_ENERGY_ENTITIES:
        samples[entity_id] = [
            (datetime.combine(day, time.min, tzinfo=WARSAW), 0.0),
            (datetime.combine(day, time(23, 55), tzinfo=WARSAW), 8.0),
        ]
    result = HISTORY.summarize_load_history(
        samples,
        now=datetime.combine(day + timedelta(days=1), time(12), tzinfo=WARSAW),
        night_windows={},
        history_days=1,
    )
    assert result.daily_history_days == 0
    assert result.profile_history_days == 0
    assert result.profile_quality_by_date[day.isoformat()] == "invalid_dense_counter"


def test_bounded_availability_gap_preserves_measured_energy_and_profile() -> None:
    day = date(2026, 8, 1)
    now = datetime.combine(day + timedelta(days=1), time(12), tzinfo=WARSAW)
    baseline_samples = complete_dense_day(day)
    baseline = HISTORY.summarize_load_history(
        baseline_samples, now=now, night_windows={}, history_days=1
    )
    assert baseline.daily_history_days == baseline.profile_history_days == 1

    for entity_id, category in (
        (HISTORY.LOAD_PHASE_ENERGY_ENTITIES[0], "unknown"),
        (HISTORY.LOAD_PROFILE_ENERGY_ENTITY, "unavailable"),
    ):
        samples = complete_dense_day(day)
        replace_with_availability_gap(
            samples[entity_id],
            datetime.combine(day, time(12), tzinfo=WARSAW),
            600,
            category=category,
        )
        result = HISTORY.summarize_load_history(
            samples, now=now, night_windows={}, history_days=1
        )
        assert result.daily_energy_kwh == baseline.daily_energy_kwh
        assert result.profile_kwh_by_date == baseline.profile_kwh_by_date
        details = result.availability_diagnostics_by_date[day.isoformat()][
            entity_id
        ]
        assert details["decision"] == "accepted_short_availability_gap"
        assert details[f"{category}_count"] == 1
        assert details["max_bracket_seconds"] == 600.0
        assert details["qualifier_version"] == (
            HISTORY.LOAD_HISTORY_QUALIFIER_VERSION
        )


def test_availability_gap_boundaries_budget_and_corruption_fail_closed() -> None:
    day = date(2026, 8, 1)
    now = datetime.combine(day + timedelta(days=1), time(12), tzinfo=WARSAW)
    # A phase's numeric bracket can be long while the explicit outage is short.
    # The dense aggregate still applies its original bracket limit.
    dense_entity = HISTORY.LOAD_PROFILE_ENERGY_ENTITY
    for seconds, accepted in ((599, True), (600, True), (601, False)):
        samples = complete_dense_day(day)
        replace_with_availability_gap(
            samples[dense_entity],
            datetime.combine(day, time(12), tzinfo=WARSAW),
            seconds,
        )
        result = HISTORY.summarize_load_history(
            samples, now=now, night_windows={}, history_days=1
        )
        assert (result.daily_history_days == 1) is accepted, (
            seconds,
            result.phase_diagnostics_by_date,
        )

    entity_id = HISTORY.LOAD_PHASE_ENERGY_ENTITIES[0]
    samples = complete_dense_day(day)
    for hour in (4, 8, 12, 16):
        left = datetime.combine(day, time(hour), tzinfo=WARSAW)
        replace_with_availability_gap(
            samples[entity_id],
            left,
            600,
        )
        rows = samples[entity_id]
        old_marker = left + timedelta(seconds=300)
        rows[:] = [
            (left + timedelta(seconds=1), value) if stamp == old_marker else (stamp, value)
            for stamp, value in rows
        ]
    excessive = HISTORY.summarize_load_history(
        samples, now=now, night_windows={}, history_days=1
    )
    assert excessive.daily_history_days == 0
    assert (
        excessive.availability_diagnostics_by_date[day.isoformat()][entity_id][
            "decision"
        ]
        == "rejected_availability_budget_exceeded"
    )

    samples = complete_dense_day(day)
    first_stamp = samples[entity_id][0][0]
    samples[entity_id][0] = (first_stamp, "unknown")
    open_edge = HISTORY.summarize_load_history(
        samples, now=now, night_windows={}, history_days=1
    )
    assert open_edge.daily_history_days == 0
    assert (
        open_edge.availability_diagnostics_by_date[day.isoformat()][entity_id][
            "decision"
        ]
        == "rejected_missing_bracket"
    )

    for corrupt in (float("nan"), float("inf"), -1.0, "bad-state"):
        samples = complete_dense_day(day)
        rows = samples[entity_id]
        stamp = datetime.combine(day, time(12), tzinfo=WARSAW)
        rows[:] = [row for row in rows if row[0] != stamp]
        rows.append((stamp, corrupt))
        rejected = HISTORY.summarize_load_history(
            samples, now=now, night_windows={}, history_days=1
        )
        assert rejected.daily_history_days == 0, corrupt
        categories = rejected.phase_diagnostics_by_date[day.isoformat()][
            entity_id
        ]["invalid_categories"]
        assert categories

    samples = complete_dense_day(day)
    rows = samples[entity_id]
    stamp = datetime.combine(day, time(12), tzinfo=WARSAW)
    rows.append((stamp, 99.0))
    duplicate = HISTORY.summarize_load_history(
        samples, now=now, night_windows={}, history_days=1
    )
    assert duplicate.daily_history_days == 0
    assert "duplicate_conflict" in duplicate.phase_diagnostics_by_date[
        day.isoformat()
    ][entity_id]["invalid_categories"]

    samples = complete_dense_day(day)
    samples[entity_id].append((None, 3.0))  # type: ignore[arg-type]
    invalid_timestamp = HISTORY.summarize_load_history(
        samples, now=now, night_windows={}, history_days=1
    )
    assert invalid_timestamp.daily_history_days == 0
    assert "invalid_timestamp" in invalid_timestamp.phase_diagnostics_by_date[
        day.isoformat()
    ][entity_id]["invalid_categories"]


def test_sparse_phase_outage_uses_episode_duration_with_dense_proof() -> None:
    day = date(2026, 8, 1)
    now = datetime.combine(day + timedelta(days=1), time(12), tzinfo=WARSAW)
    phase = HISTORY.LOAD_PHASE_ENERGY_ENTITIES[0]
    baseline_samples = complete_dense_day(day)
    baseline = HISTORY.summarize_load_history(
        baseline_samples, now=now, night_windows={}, history_days=1
    )

    def scenario(seconds: int, *, repeated: bool = False):
        samples = complete_dense_day(day)
        rows = samples[phase]
        left = datetime.combine(day, time(10), tzinfo=WARSAW)
        marker = datetime.combine(day, time(12), tzinfo=WARSAW)
        recovery = marker + timedelta(seconds=seconds)
        # Retain measured values at the edges and the dense aggregate. This
        # isolates the effect of sparse phase reporting from LOAD shape.
        rows[:] = [
            row for row in rows
            if row[0] <= left or row[0] >= recovery
        ]
        rows.append((marker, "unknown"))
        if repeated:
            rows.append((marker + timedelta(seconds=15), "unavailable"))
        rows.append((recovery, 8.0 * (recovery - datetime.combine(day, time.min, tzinfo=WARSAW)).total_seconds() / 86400.0))
        rows.sort(key=lambda row: row[0])
        return HISTORY.summarize_load_history(
            samples, now=now, night_windows={}, history_days=1
        )

    for seconds, accepted in ((599, True), (600, True), (601, False)):
        result = scenario(seconds, repeated=True)
        assert (result.daily_history_days == 1) is accepted, seconds
        details = result.availability_diagnostics_by_date[day.isoformat()][phase]
        assert details["max_episode_seconds"] == seconds
        assert details["episode_count"] == 1
        assert details["max_bracket_seconds"] > 600
        if accepted:
            assert result.daily_energy_kwh == baseline.daily_energy_kwh
            assert result.profile_kwh_by_date == baseline.profile_kwh_by_date

    short = scenario(60)
    assert short.daily_history_days == short.profile_history_days == 1
    # Qualify the actual window with its own dense proof; a daily decision
    # alone still cannot authorize a phase-only night.
    samples = complete_dense_day(day)
    left = datetime.combine(day, time(10), tzinfo=WARSAW)
    marker = datetime.combine(day, time(12), tzinfo=WARSAW)
    recovery = marker + timedelta(seconds=60)
    rows = samples[phase]
    rows[:] = [row for row in rows if row[0] <= left or row[0] >= recovery]
    rows.extend(((marker, "unknown"), (recovery, 8.0 * 12.0166666667 / 24.0)))
    phase_only = HISTORY.summarize_load_history(
        samples,
        now=now,
        night_windows={day: (left, datetime.combine(day, time(13), tzinfo=WARSAW))},
        history_days=1,
    )
    assert phase_only.daily_history_days == 1
    assert phase_only.night_history_days == 1
    samples.pop(HISTORY.LOAD_PROFILE_ENERGY_ENTITY)
    without_dense = HISTORY.summarize_load_history(
        samples, now=now,
        night_windows={day: (left, datetime.combine(day, time(13), tzinfo=WARSAW))},
        history_days=1,
    )
    assert without_dense.night_history_days == 0


def test_sparse_unchanged_phase_needs_no_pre_outage_report() -> None:
    day = date(2026, 8, 1)
    now = datetime.combine(day + timedelta(days=1), time(12), tzinfo=WARSAW)
    phase = HISTORY.LOAD_PHASE_ENERGY_ENTITIES[0]
    samples = complete_dense_day(day)
    at = lambda hour, minute: datetime.combine(day, time(hour, minute), tzinfo=WARSAW)
    samples[phase] = [
        (at(0, 0), 0.0),
        (at(10, 0), 3.0),
        (at(12, 1), 3.0),
        (at(14, 0), 3.0),
        (at(18, 0), 5.0),
        (at(23, 57), 7.979166666666666),
    ]
    baseline = HISTORY.summarize_load_history(
        samples, now=now, night_windows={}, history_days=1
    )
    assert baseline.daily_history_days == baseline.profile_history_days == 1
    samples[phase].insert(2, (at(12, 0), "unavailable"))
    short = HISTORY.summarize_load_history(
        samples, now=now, night_windows={}, history_days=1
    )
    assert short.daily_energy_kwh == baseline.daily_energy_kwh
    assert short.profile_kwh_by_date == baseline.profile_kwh_by_date
    details = short.availability_diagnostics_by_date[day.isoformat()][phase]
    assert details["max_bracket_seconds"] == 7260.0
    assert details["max_episode_seconds"] == 60.0
    assert details["total_gap_seconds"] == 60.0
    assert details["episode_count"] == 1


def test_sparse_phase_budget_uses_real_dst_day_seconds() -> None:
    phase = HISTORY.LOAD_PHASE_ENERGY_ENTITIES[0]
    for day, boundary in (
        (date(2026, 3, 29), 414),
        (date(2026, 8, 1), 432),
        (date(2026, 10, 25), 450),
    ):
        start = datetime.combine(day, time.min, tzinfo=WARSAW)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=WARSAW)
        duration = (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
        now = end + timedelta(hours=12)
        for adjustment, accepted in ((-1, True), (0, True), (1, False)):
            samples = complete_dense_day(day)
            rows = samples[phase]
            for hour in (4, 8, 12, 16):
                left = datetime.combine(day, time(hour), tzinfo=WARSAW)
                marker = left + timedelta(seconds=1)
                recovery = marker + timedelta(
                    seconds=boundary + (adjustment if hour == 16 else 0)
                )
                rows[:] = [
                    row for row in rows
                    if row[0] <= left or row[0] >= recovery
                ]
                elapsed = (
                    recovery.astimezone(timezone.utc)
                    - start.astimezone(timezone.utc)
                ).total_seconds()
                rows.extend(((marker, "unknown"), (recovery, 8.0 * elapsed / duration)))
            rows.sort(key=lambda row: row[0].astimezone(timezone.utc))
            result = HISTORY.summarize_load_history(
                samples, now=now, night_windows={}, history_days=1
            )
            assert (result.daily_history_days == 1) is accepted, (day, adjustment)
            details = result.availability_diagnostics_by_date[day.isoformat()][phase]
            assert details["episode_count"] == 4
            assert details["total_unavailable_seconds"] == 4 * boundary + adjustment


def test_sparse_episode_crossing_midnight_invalidates_both_days() -> None:
    first = datetime(2026, 8, 1, 23, 59, tzinfo=WARSAW)
    second = datetime(2026, 8, 2, 0, 0, tzinfo=WARSAW)
    normalized = HISTORY._normalize(
        (
            (first, 3.0),
            (first + timedelta(seconds=30), "unknown"),
            (second + timedelta(seconds=30), "unavailable"),
            (second + timedelta(minutes=1), 3.0),
        ),
        sparse_phase=True,
    )
    for day in (first.date(), second.date()):
        assert "availability_crosses_day_edge" in normalized.invalid_by_day[day]
        assert normalized.availability_by_day[day]["decision"] == "rejected_day_edge"


def test_availability_with_counter_drop_reports_reset_and_rejects() -> None:
    day = date(2026, 8, 1)
    now = datetime.combine(day + timedelta(days=1), time(12), tzinfo=WARSAW)
    samples = complete_dense_day(day)
    entity_id = HISTORY.LOAD_PHASE_ENERGY_ENTITIES[0]
    rows = samples[entity_id]
    left = datetime.combine(day, time(12), tzinfo=WARSAW)
    right = left + timedelta(minutes=10)
    rows[:] = [
        row for row in rows if not left < row[0] <= right
    ]
    left_index = next(index for index, row in enumerate(rows) if row[0] == left)
    rows[left_index] = (left, 7.0)
    rows.append((left + timedelta(minutes=5), "unavailable"))
    rows.append((right, 1.0))
    rows.sort(key=lambda item: item[0])
    result = HISTORY.summarize_load_history(
        samples, now=now, night_windows={}, history_days=1
    )
    assert result.daily_history_days == 0
    diagnostics = result.phase_diagnostics_by_date[day.isoformat()][entity_id]
    assert diagnostics["reset_at"] is not None
    assert "availability_counter_drop" in diagnostics["invalid_categories"]


def test_short_availability_gap_respects_dst_and_current_window() -> None:
    for day in (date(2026, 3, 29), date(2026, 10, 25)):
        now = datetime.combine(day + timedelta(days=1), time(12), tzinfo=WARSAW)
        samples = complete_dense_day(day)
        replace_with_availability_gap(
            samples[HISTORY.LOAD_PROFILE_ENERGY_ENTITY],
            datetime.combine(day, time(12), tzinfo=WARSAW),
            600,
        )
        result = HISTORY.summarize_load_history(
            samples, now=now, night_windows={}, history_days=1
        )
        assert result.daily_history_days == result.profile_history_days == 1

    current_day = date(2026, 8, 2)
    current_start = datetime.combine(current_day, time(7, 30), tzinfo=WARSAW)
    current_end = datetime.combine(current_day, time(12), tzinfo=WARSAW)
    samples = {entity_id: [] for entity_id in HISTORY.LOAD_HISTORY_ENTITIES}
    for entity_id in HISTORY.LOAD_PHASE_ENERGY_ENTITIES:
        samples[entity_id] = [
            (current_start - timedelta(minutes=5), 1.0),
            (current_start + timedelta(hours=2), 2.0),
            (current_start + timedelta(hours=2, minutes=5), "unknown"),
            (current_start + timedelta(hours=2, minutes=10), 2.2),
            (current_end - timedelta(minutes=5), 3.0),
        ]
    current = HISTORY.summarize_load_history(
        samples,
        now=current_end,
        night_windows={},
        current_day_window=(current_start, current_end),
        history_days=0,
    )
    assert current.current_day_energy_kwh == 6.0


if __name__ == "__main__":
    test_daily_and_night_history_uses_phase_counters()
    test_current_day_window_uses_actual_phase_load()
    test_extended_history_exposes_28_complete_days_and_profiles()
    test_sparse_or_reset_data_keeps_partial_total_without_fake_profile()
    test_delayed_midnight_reset_excludes_carryover_and_keeps_night()
    test_out_of_order_samples_and_dst_preserve_energy()
    test_nonfinite_phase_invalidates_complete_day()
    test_nonfinite_dense_counter_invalidates_profile()
    test_bounded_availability_gap_preserves_measured_energy_and_profile()
    test_availability_gap_boundaries_budget_and_corruption_fail_closed()
    test_sparse_phase_outage_uses_episode_duration_with_dense_proof()
    test_sparse_unchanged_phase_needs_no_pre_outage_report()
    test_sparse_phase_budget_uses_real_dst_day_seconds()
    test_sparse_episode_crossing_midnight_invalidates_both_days()
    test_availability_with_counter_drop_reports_reset_and_rejects()
    test_short_availability_gap_respects_dst_and_current_window()
    print("RCE history: 16 recorder quality groups passed")
