"""Recorder-backed LOAD history with separate energy and shape quality."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import math
from typing import Any

LOAD_PHASE_ENERGY_ENTITIES = (
    "sensor.hoymiles_hit_load_energy_use_l1n_today",
    "sensor.hoymiles_hit_load_energy_use_l2n_today",
    "sensor.hoymiles_hit_load_energy_use_l3n_today",
)
LOAD_PROFILE_ENERGY_ENTITY = "sensor.hoymiles_actual_load_energy_today"
LOAD_HISTORY_ENTITIES = (*LOAD_PHASE_ENERGY_ENTITIES, LOAD_PROFILE_ENERGY_ENTITY)

# The firmware reports these sources every 150 s. Four missed reports are the
# limit for the dense aggregate continuity proof and profile edge coverage.
SOURCE_REPORT_INTERVAL = timedelta(seconds=150)
PROFILE_MAX_GAP = SOURCE_REPORT_INTERVAL * 4
PROFILE_EDGE_TOLERANCE = SOURCE_REPORT_INTERVAL * 4
# Unchanged low-load phase values are not all persisted. Their cumulative total
# is accepted near the edge only while the dense aggregate proves continuity.
PHASE_TOTAL_EDGE_TOLERANCE = timedelta(hours=2)
MIN_PROFILE_COVERAGE = 0.98
COUNTER_NOISE_KWH = 0.05
COUNTER_RESET_DROP_KWH = 0.50
PHASE_AGGREGATE_TOLERANCE_KWH = 2.0
PHASE_AGGREGATE_TOLERANCE_RATIO = 0.15
LOAD_HISTORY_QUALIFIER_VERSION = "load_history_v6_bounded_phase_windows"
AVAILABILITY_STATES = frozenset({"unknown", "unavailable"})


@dataclass(frozen=True, slots=True)
class LoadHistoryAvailability:
    """An explicit HA availability state, never a numeric observation."""

    category: str


@dataclass(frozen=True, slots=True)
class _NormalizedSeries:
    samples: list[tuple[datetime, float]]
    invalid_by_day: dict[date | None, set[str]]
    availability_by_day: dict[date, dict[str, Any]]
    strict_invalid_by_day: dict[date | None, set[str]] | None = None
    strict_gap_seconds_by_day: dict[date, float] | None = None


def parse_load_history_state(raw: Any) -> Any:
    """Preserve explicit HA availability while leaving corruption visible."""

    if isinstance(raw, str):
        category = raw.strip().casefold()
        if category in AVAILABILITY_STATES:
            return LoadHistoryAvailability(category)
    try:
        return float(raw)
    except (TypeError, ValueError, OverflowError):
        return raw


def is_load_history_observation(value: Any) -> bool:
    """Return whether a Recorder row exists without assuming it is numeric."""

    return value is not None or isinstance(value, LoadHistoryAvailability)


@dataclass(frozen=True, slots=True)
class LoadHistorySummary:
    """LOAD totals and profiles with independent quality diagnostics."""

    average_daily_kwh: float | None
    daily_history_days: int
    daily_energy_kwh: dict[str, float]
    average_night_kwh: float | None
    night_history_days: int
    night_energy_kwh: dict[str, float]
    current_day_energy_kwh: float | None = None
    current_day_observed_at: datetime | None = None
    average_profile_kwh: tuple[float, ...] = ()
    weekday_profile_kwh: tuple[float, ...] = ()
    weekend_profile_kwh: tuple[float, ...] = ()
    weekday_profile_days: int = 0
    weekend_profile_days: int = 0
    profile_history_days: int = 0
    partial_daily_energy_kwh: dict[str, float] | None = None
    daily_quality_by_date: dict[str, str] | None = None
    profile_quality_by_date: dict[str, str] | None = None
    phase_quality_by_date: dict[str, dict[str, str]] | None = None
    phase_diagnostics_by_date: dict[str, dict[str, dict[str, Any]]] | None = None
    availability_diagnostics_by_date: (
        dict[str, dict[str, dict[str, Any]]] | None
    ) = None
    night_quality_by_date: dict[str, str] | None = None
    profile_kwh_by_date: dict[str, tuple[float, ...]] | None = None
    daily_coverage_ratio: float = 0.0
    profile_coverage_ratio: float = 0.0
    cache_migrated_from_qualifier_version: str | None = None

    @property
    def daily_energy_total_kwh(self) -> float:
        return sum(self.daily_energy_kwh.values())

    @property
    def night_energy_total_kwh(self) -> float:
        return sum(self.night_energy_kwh.values())


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("LOAD history timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _normalize(
    samples: Sequence[tuple[Any, Any]],
    *,
    sparse_phase: bool = False,
) -> _NormalizedSeries:
    by_stamp: dict[datetime, tuple[datetime, float]] = {}
    invalid_days: dict[date | None, set[str]] = {}
    availability_events: list[tuple[datetime, datetime, str]] = []

    def invalid(day: date | None, category: str) -> None:
        invalid_days.setdefault(day, set()).add(category)

    for stamp, raw in samples:
        if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
            invalid(None, "invalid_timestamp")
            continue
        day = stamp.date()
        parsed = parse_load_history_state(raw)
        if isinstance(parsed, LoadHistoryAvailability):
            availability_events.append((_utc(stamp), stamp, parsed.category))
            continue
        try:
            numeric = float(parsed)
        except (TypeError, ValueError, OverflowError):
            invalid(day, "invalid_text")
            continue
        if not math.isfinite(numeric):
            invalid(day, "nonfinite_value")
            continue
        if numeric < 0.0:
            invalid(day, "negative_value")
            continue
        stamp_utc = _utc(stamp)
        previous = by_stamp.get(stamp_utc)
        if previous is not None and abs(previous[1] - numeric) > COUNTER_NOISE_KWH:
            invalid(day, "duplicate_conflict")
        by_stamp[stamp_utc] = (stamp, numeric)

    numeric_stamps = sorted(by_stamp)
    availability_by_day: dict[date, dict[str, Any]] = {}
    accepted_brackets: dict[date, set[tuple[datetime, datetime]]] = {}
    strict_invalid_days: dict[date | None, set[str]] = {}
    strict_gap_seconds: dict[date, float] = {}
    episodes: dict[tuple[datetime, datetime], list[tuple[datetime, datetime, str]]] = {}
    for stamp_utc, stamp, category in sorted(availability_events):
        day = stamp.date()
        row = availability_by_day.setdefault(
            day,
            {
                "unknown_count": 0,
                "unavailable_count": 0,
                "event_count": 0,
                "first_at": stamp.isoformat(),
                "last_at": stamp.isoformat(),
                "max_bracket_seconds": None,
                "max_numeric_bracket_seconds": None,
                "max_episode_seconds": 0.0,
                "episode_count": 0,
                "first_recovery_at": None,
                "total_unavailable_seconds": 0.0,
                "total_gap_seconds": 0.0,
                "decision": "accepted_short_availability_gap",
                "qualifier_version": LOAD_HISTORY_QUALIFIER_VERSION,
            },
        )
        row[f"{category}_count"] += 1
        row["event_count"] += 1
        row["last_at"] = stamp.isoformat()
        index = bisect_left(numeric_stamps, stamp_utc)
        if stamp_utc in by_stamp:
            invalid(day, "duplicate_conflict")
            row["decision"] = "rejected_duplicate_conflict"
            continue
        if index == 0 or index >= len(numeric_stamps):
            invalid(day, "availability_missing_bracket")
            row["decision"] = "rejected_missing_bracket"
            continue
        left_utc = numeric_stamps[index - 1]
        right_utc = numeric_stamps[index]
        episodes.setdefault((left_utc, right_utc), []).append((stamp_utc, stamp, category))

    for (left_utc, right_utc), markers in episodes.items():
        stamp_utc, stamp, _ = markers[0]
        day = stamp.date()
        row = availability_by_day[day]
        left_stamp, left_value = by_stamp[left_utc]
        right_stamp, right_value = by_stamp[right_utc]
        bracket_seconds = (right_utc - left_utc).total_seconds()
        episode_seconds = (right_utc - stamp_utc).total_seconds()
        row["max_bracket_seconds"] = max(
            float(row["max_bracket_seconds"] or 0.0),
            bracket_seconds,
        )
        row["max_numeric_bracket_seconds"] = row["max_bracket_seconds"]
        row["max_episode_seconds"] = max(row["max_episode_seconds"], episode_seconds)
        row["episode_count"] += 1
        row["first_recovery_at"] = row["first_recovery_at"] or right_stamp.isoformat()
        row["total_unavailable_seconds"] += episode_seconds
        if (
            left_stamp.date() != day
            or right_stamp.date() != day
            or any(marker_stamp.date() != day for _, marker_stamp, _ in markers)
        ):
            affected_days = {left_stamp.date(), right_stamp.date()}
            affected_days.update(marker_stamp.date() for _, marker_stamp, _ in markers)
            for affected_day in affected_days:
                invalid(affected_day, "availability_crosses_day_edge")
                details = availability_by_day.get(affected_day)
                if details:
                    details["decision"] = "rejected_day_edge"
            continue
        if bracket_seconds > PROFILE_MAX_GAP.total_seconds():
            strict_invalid_days.setdefault(day, set()).add("availability_bracket_too_long")
        if (episode_seconds if sparse_phase else bracket_seconds) > PROFILE_MAX_GAP.total_seconds():
            invalid(day, "availability_bracket_too_long")
            row["decision"] = "rejected_episode_too_long" if sparse_phase else "rejected_bracket_too_long"
            continue
        if right_value + COUNTER_NOISE_KWH < left_value:
            invalid(day, "availability_counter_drop")
            row["decision"] = "rejected_counter_drop"
            continue
        accepted_brackets.setdefault(day, set()).add((left_utc, right_utc))
        if sparse_phase:
            row["total_gap_seconds"] += episode_seconds
            strict_gap_seconds[day] = strict_gap_seconds.get(day, 0.0) + bracket_seconds

    for day, brackets in accepted_brackets.items():
        if not sparse_phase:
            availability_by_day[day]["total_gap_seconds"] = sum(
                (right - left).total_seconds() for left, right in brackets
            )
    return _NormalizedSeries(
        [by_stamp[key] for key in numeric_stamps],
        invalid_days,
        availability_by_day,
        strict_invalid_days if sparse_phase else None,
        strict_gap_seconds if sparse_phase else None,
    )


def _same_day_reset(samples: Sequence[tuple[datetime, float]]) -> bool:
    return any(
        later + COUNTER_RESET_DROP_KWH < earlier
        for (earlier_at, earlier), (later_at, later) in zip(samples, samples[1:])
        if earlier_at.date() == later_at.date()
    )


def _first_same_day_reset_at(
    samples: Sequence[tuple[datetime, float]],
) -> datetime | None:
    for (earlier_at, earlier), (later_at, later) in zip(samples, samples[1:]):
        if earlier_at.date() == later_at.date() and later + COUNTER_RESET_DROP_KWH < earlier:
            return later_at
    return None


def _strip_delayed_midnight_carryover(
    samples: Sequence[tuple[datetime, float]],
    *,
    day_start: datetime,
) -> tuple[list[tuple[datetime, float]], bool]:
    """Drop a proved prior-day carryover preceding the delayed daily reset."""

    day_samples = [sample for sample in samples if sample[0].date() == day_start.date()]
    previous = [sample for sample in samples if sample[0] < day_start]
    if len(day_samples) < 2 or not previous:
        return day_samples, False
    prior_at, prior_value = previous[-1]
    first_at, first_value = day_samples[0]
    if (
        day_start - prior_at > PHASE_TOTAL_EDGE_TOLERANCE
        or first_at - day_start > PROFILE_EDGE_TOLERANCE
        or abs(first_value - prior_value) > COUNTER_NOISE_KWH
    ):
        return day_samples, False
    for index, ((_, earlier), (later_at, later)) in enumerate(
        zip(day_samples, day_samples[1:])
    ):
        if later + COUNTER_RESET_DROP_KWH < earlier:
            if later_at - day_start <= PROFILE_EDGE_TOLERANCE:
                remaining = day_samples[index + 1 :]
                if remaining and not _same_day_reset(remaining):
                    return remaining, True
            break
    return day_samples, False


def _day_bounds(day: date, tzinfo: object) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=tzinfo)  # type: ignore[arg-type]
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tzinfo)  # type: ignore[arg-type]
    return start, end


def _invalid_categories(
    series: _NormalizedSeries,
    day: date,
) -> set[str]:
    return {
        *series.invalid_by_day.get(None, set()),
        *series.invalid_by_day.get(day, set()),
    }


def _availability_budget_ok(
    series: _NormalizedSeries,
    day: date,
    start: datetime,
    end: datetime,
    *,
    strict: bool = False,
) -> bool:
    details = series.availability_by_day.get(day)
    if not details:
        return True
    day_seconds = (_utc(end) - _utc(start)).total_seconds()
    allowed = day_seconds * (1.0 - MIN_PROFILE_COVERAGE)
    seconds = (
        (series.strict_gap_seconds_by_day or {}).get(day, 0.0)
        if strict and series.strict_gap_seconds_by_day is not None
        else float(details.get("total_gap_seconds") or 0.0)
    )
    if seconds <= allowed + 1e-9:
        return True
    if strict:
        return False
    details["decision"] = "rejected_availability_budget_exceeded"
    series.invalid_by_day.setdefault(day, set()).add(
        "availability_budget_exceeded"
    )
    return False


def _dense_day_quality(
    samples: Sequence[tuple[datetime, float]], start: datetime, end: datetime
) -> tuple[bool, float, str]:
    day_samples = [item for item in samples if start <= item[0] < end]
    if len(day_samples) < 2:
        return False, 0.0, "missing_dense_counter"
    if day_samples[0][0] - start > PROFILE_EDGE_TOLERANCE:
        return False, 0.0, "missing_start_edge"
    if end - day_samples[-1][0] > PROFILE_EDGE_TOLERANCE:
        return False, 0.0, "missing_end_edge"
    if _same_day_reset(day_samples):
        return False, 0.0, "midday_counter_reset"
    times = [_utc(item[0]) for item in day_samples]
    max_gap = max((b - a for a, b in zip(times, times[1:])), default=timedelta.max)
    day_seconds = (_utc(end) - _utc(start)).total_seconds()
    coverage = min((times[-1] - times[0]).total_seconds() / max(day_seconds, 1.0), 1.0)
    if max_gap > PROFILE_MAX_GAP:
        return False, coverage, "long_recorder_gap"
    if coverage < MIN_PROFILE_COVERAGE:
        return False, coverage, "insufficient_coverage"
    return True, coverage, "complete"


def _profile_from_counter(
    samples: Sequence[tuple[datetime, float]],
    start: datetime,
    end: datetime,
    target_total: float,
) -> tuple[float, ...]:
    day_samples = [item for item in samples if start <= item[0] < end]
    slots = [0.0] * 48
    for (left, before), (right, after) in zip(day_samples, day_samples[1:]):
        increase = after - before
        duration = (_utc(right) - _utc(left)).total_seconds()
        if increase <= 0.0 or not 0.0 < duration <= PROFILE_MAX_GAP.total_seconds():
            continue
        cursor = left
        while _utc(cursor) < _utc(right):
            boundary = cursor.replace(
                minute=30 if cursor.minute < 30 else 0, second=0, microsecond=0
            )
            if cursor.minute >= 30:
                boundary += timedelta(hours=1)
            boundary_utc = min(_utc(boundary), _utc(right))
            seconds = max((boundary_utc - _utc(cursor)).total_seconds(), 0.0)
            slots[cursor.hour * 2 + cursor.minute // 30] += increase * seconds / duration
            if boundary_utc >= _utc(right):
                break
            cursor = boundary_utc.astimezone(cursor.tzinfo)
    measured = sum(slots)
    if measured <= 0.0 or target_total <= 0.0:
        return ()
    scale = target_total / measured
    return tuple(max(value * scale, 0.0) for value in slots)


def _counter_increase(
    series: _NormalizedSeries, start: datetime, end: datetime,
    *, raw_samples: Sequence[tuple[Any, Any]] | None = None,
    dense_window_proved: bool = False,
) -> tuple[float | None, datetime | None]:
    samples = series.samples
    before = [sample for sample in samples if sample[0] <= start]
    inside = [sample for sample in samples if start < sample[0] <= end]
    if not before or not inside:
        return None, None
    start_sample, end_sample = before[-1], inside[-1]
    if raw_samples is not None:
        # A daytime outage cannot disqualify a later, complete night. Keep
        # the baseline observation and all explicit faults through the end,
        # including an outage that has not recovered by the window boundary.
        bounded = []
        for stamp, value in raw_samples:
            if (not isinstance(stamp, datetime) or stamp.tzinfo is None
                or stamp.utcoffset() is None):
                return None, None
            if _utc(start_sample[0]) <= _utc(stamp) <= _utc(end):
                bounded.append((stamp, value))
        series = _normalize(bounded, sparse_phase=dense_window_proved)
    cursor_day = start.date()
    while cursor_day <= end.date():
        day_start, day_end = _day_bounds(cursor_day, start.tzinfo)
        if (
            _invalid_categories(series, cursor_day)
            or (raw_samples is None and (series.strict_invalid_by_day or {}).get(cursor_day))
            or not _availability_budget_ok(
                series, cursor_day,
                max(start, day_start) if raw_samples is not None else day_start,
                min(end, day_end) if raw_samples is not None else day_end,
                strict=raw_samples is None,
            )
        ):
            return None, None
        cursor_day += timedelta(days=1)
    if start - start_sample[0] > PHASE_TOTAL_EDGE_TOLERANCE:
        return None, None
    if end - end_sample[0] > PHASE_TOTAL_EDGE_TOLERANCE:
        return None, None
    previous_time, previous = start_sample
    energy = 0.0
    delayed_rollover_used = False
    midnight_carryover_seen = False
    for stamp, value in inside:
        if value + COUNTER_NOISE_KWH >= previous:
            energy += max(value - previous, 0.0)
            if (
                stamp.date() != previous_time.date()
                and abs(value - previous) <= COUNTER_NOISE_KWH
                and stamp.time() <= (datetime.min + PROFILE_EDGE_TOLERANCE).time()
            ):
                midnight_carryover_seen = True
        elif stamp.date() != previous_time.date():
            energy += value
        elif (
            not delayed_rollover_used
            and stamp.time() <= (datetime.min + PROFILE_EDGE_TOLERANCE).time()
            and midnight_carryover_seen
        ):
            # The first report after midnight still repeated yesterday's
            # closing value; the following drop is the delayed daily reset.
            energy += value
            delayed_rollover_used = True
        else:
            return None, None
        previous_time, previous = stamp, value
    return max(energy, 0.0), end_sample[0]


def summarize_load_history(
    samples_by_entity: Mapping[str, Sequence[tuple[Any, Any]]],
    *,
    now: datetime,
    night_windows: Mapping[date, tuple[datetime, datetime]],
    current_day_window: tuple[datetime, datetime] | None = None,
    history_days: int = 4,
) -> LoadHistorySummary:
    """Build phase totals and continuity-proved half-hour profiles."""
    normalized: dict[str, _NormalizedSeries] = {}
    for entity_id, raw in samples_by_entity.items():
        normalized[entity_id] = _normalize(
            raw, sparse_phase=entity_id in LOAD_PHASE_ENERGY_ENTITIES
        )
    empty_series = _NormalizedSeries([], {}, {})
    dense_series = normalized.get(LOAD_PROFILE_ENERGY_ENTITY, empty_series)
    dense = dense_series.samples
    daily: dict[str, float] = {}
    partial: dict[str, float] = {}
    daily_quality: dict[str, str] = {}
    profile_quality: dict[str, str] = {}
    profiles: dict[date, tuple[float, ...]] = {}
    phase_quality: dict[str, dict[str, str]] = {}
    phase_diagnostics: dict[str, dict[str, dict[str, Any]]] = {}
    availability_diagnostics: dict[str, dict[str, dict[str, Any]]] = {}
    daily_coverages: list[float] = []
    profile_coverages: list[float] = []

    for candidate in [now.date() - timedelta(days=i) for i in range(history_days, 0, -1)]:
        key = candidate.isoformat()
        day_start, day_end = _day_bounds(candidate, now.tzinfo)
        availability_diagnostics[key] = {}
        for entity_id, series in normalized.items():
            _availability_budget_ok(series, candidate, day_start, day_end)
            details = series.availability_by_day.get(candidate)
            if details:
                availability_diagnostics[key][entity_id] = dict(details)
        dense_ok, coverage, reason = _dense_day_quality(dense, day_start, day_end)
        dense_invalid = _invalid_categories(dense_series, candidate)
        if dense_invalid:
            dense_ok = False
            reason = "invalid_dense_counter"
        for details in availability_diagnostics[key].values():
            details["dense_quality_reason"] = reason
            details["dense_coverage_ratio"] = round(coverage, 6)
        phase_totals: list[float] = []
        phase_ok = True
        phase_quality[key] = {}
        phase_diagnostics[key] = {}
        for entity_id in LOAD_PHASE_ENERGY_ENTITIES:
            phase_series = normalized.get(entity_id, empty_series)
            raw_phase = [
                sample
                for sample in phase_series.samples
                if sample[0].date() == candidate
            ]
            phase, delayed_rollover = _strip_delayed_midnight_carryover(
                phase_series.samples,
                day_start=day_start,
            )
            invalid_categories = _invalid_categories(phase_series, candidate)
            if not phase:
                phase_reason = (
                    "invalid_value"
                    if invalid_categories
                    else "missing"
                )
                phase_quality[key][entity_id] = phase_reason
                phase_diagnostics[key][entity_id] = {
                    "reason": phase_reason,
                    "first_at": None,
                    "last_at": None,
                    "sample_count": 0,
                    "minimum_kwh": None,
                    "maximum_kwh": None,
                    "reset_at": None,
                    "delayed_midnight_rollover": False,
                    "invalid_categories": ",".join(sorted(invalid_categories)),
                    "qualifier_version": LOAD_HISTORY_QUALIFIER_VERSION,
                }
                phase_ok = False
                continue
            phase_totals.append(max(value for _, value in phase))
            phase_reason = (
                "invalid_value"
                if invalid_categories
                else "midday_counter_reset"
                if _same_day_reset(phase)
                else "missing_start_edge"
                if phase[0][0] - day_start > PROFILE_EDGE_TOLERANCE
                else "missing_end_edge"
                if day_end - phase[-1][0] > PHASE_TOTAL_EDGE_TOLERANCE
                else "complete_delayed_midnight_rollover"
                if delayed_rollover
                else "complete"
            )
            phase_quality[key][entity_id] = phase_reason
            reset_at = _first_same_day_reset_at(raw_phase)
            phase_diagnostics[key][entity_id] = {
                "reason": phase_reason,
                "first_at": raw_phase[0][0].isoformat() if raw_phase else None,
                "last_at": raw_phase[-1][0].isoformat() if raw_phase else None,
                "accepted_first_at": phase[0][0].isoformat(),
                "accepted_last_at": phase[-1][0].isoformat(),
                "sample_count": len(raw_phase),
                "minimum_kwh": min((value for _, value in raw_phase), default=None),
                "maximum_kwh": max((value for _, value in raw_phase), default=None),
                "reset_at": reset_at.isoformat() if reset_at else None,
                "delayed_midnight_rollover": delayed_rollover,
                "invalid_categories": ",".join(sorted(invalid_categories)),
                "qualifier_version": LOAD_HISTORY_QUALIFIER_VERSION,
            }
            if phase_reason not in {"complete", "complete_delayed_midnight_rollover"}:
                phase_ok = False
            if (
                invalid_categories
                or _same_day_reset(phase)
                or phase[0][0] - day_start > PROFILE_EDGE_TOLERANCE
                or day_end - phase[-1][0] > PHASE_TOTAL_EDGE_TOLERANCE
            ):
                phase_ok = False
        if len(phase_totals) == len(LOAD_PHASE_ENERGY_ENTITIES):
            partial[key] = round(sum(phase_totals), 3)
        if not phase_ok:
            daily_quality[key] = "partial_phase_counter"
            profile_quality[key] = "unavailable"
            continue
        total = sum(phase_totals)
        dense_day = [item for item in dense if item[0].date() == candidate]
        dense_total = max((value for _, value in dense_day), default=0.0)
        tolerance = max(PHASE_AGGREGATE_TOLERANCE_KWH, total * PHASE_AGGREGATE_TOLERANCE_RATIO)
        if not dense_ok:
            daily_quality[key] = f"partial_{reason}"
            profile_quality[key] = reason
            continue
        if abs(total - dense_total) > tolerance:
            daily_quality[key] = "phase_aggregate_mismatch"
            profile_quality[key] = "phase_aggregate_mismatch"
            continue
        daily[key] = round(total, 3)
        daily_quality[key] = "complete"
        daily_coverages.append(coverage)
        profile = _profile_from_counter(dense, day_start, day_end, total)
        if profile:
            profiles[candidate] = profile
            profile_quality[key] = "complete"
            profile_coverages.append(coverage)
        else:
            profile_quality[key] = "empty_profile"

    nights: dict[str, float] = {}
    night_quality: dict[str, str] = {}
    for night_date, (start, end) in sorted(night_windows.items()):
        if end > now:
            continue
        values: list[float] = []
        dense_window_proved = _dense_day_quality(dense, start, end)[0]
        for entity_id in LOAD_PHASE_ENERGY_ENTITIES:
            increase, _ = _counter_increase(
                normalized.get(entity_id, empty_series), start, end,
                raw_samples=samples_by_entity.get(entity_id, ()),
                dense_window_proved=dense_window_proved,
            )
            if increase is None:
                values = []
                night_quality[night_date.isoformat()] = f"missing_or_reset:{entity_id}"
                break
            values.append(increase)
        if values:
            nights[night_date.isoformat()] = round(sum(values), 3)
            night_quality[night_date.isoformat()] = "complete"
    if len(nights) > history_days:
        nights = dict(list(nights.items())[-history_days:])

    current_energy: float | None = None
    current_observed_at: datetime | None = None
    if current_day_window is not None and current_day_window[1] > current_day_window[0]:
        values: list[float] = []
        observed: list[datetime] = []
        for entity_id in LOAD_PHASE_ENERGY_ENTITIES:
            increase, stamp = _counter_increase(
                normalized.get(entity_id, empty_series), *current_day_window
            )
            if increase is None or stamp is None:
                values = []
                break
            values.append(increase)
            observed.append(stamp)
        if values:
            current_energy = round(sum(values), 3)
            current_observed_at = min(observed)

    def averaged(days: Sequence[date]) -> tuple[float, ...]:
        selected = [profiles[day] for day in days if day in profiles]
        if not selected:
            return ()
        return tuple(round(sum(row[i] for row in selected) / len(selected), 4) for i in range(48))

    profile_days = sorted(profiles)
    weekdays = [day for day in profile_days if day.weekday() < 5]
    weekends = [day for day in profile_days if day.weekday() >= 5]
    return LoadHistorySummary(
        average_daily_kwh=round(sum(daily.values()) / len(daily), 3) if daily else None,
        daily_history_days=len(daily),
        daily_energy_kwh=daily,
        average_night_kwh=round(sum(nights.values()) / len(nights), 3) if nights else None,
        night_history_days=len(nights),
        night_energy_kwh=nights,
        current_day_energy_kwh=current_energy,
        current_day_observed_at=current_observed_at,
        average_profile_kwh=averaged(profile_days),
        weekday_profile_kwh=averaged(weekdays),
        weekend_profile_kwh=averaged(weekends),
        weekday_profile_days=len(weekdays),
        weekend_profile_days=len(weekends),
        profile_history_days=len(profile_days),
        partial_daily_energy_kwh=partial,
        daily_quality_by_date=daily_quality,
        profile_quality_by_date=profile_quality,
        phase_quality_by_date=phase_quality,
        phase_diagnostics_by_date=phase_diagnostics,
        availability_diagnostics_by_date=availability_diagnostics,
        night_quality_by_date=night_quality,
        profile_kwh_by_date={
            day.isoformat(): profile for day, profile in profiles.items()
        },
        daily_coverage_ratio=sum(daily_coverages) / len(daily_coverages) if daily_coverages else 0.0,
        profile_coverage_ratio=sum(profile_coverages) / len(profile_coverages) if profile_coverages else 0.0,
    )
