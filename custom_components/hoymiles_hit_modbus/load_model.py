"""Policy-neutral robust estimates for household energy demand.

The EMS engines have different objectives, but they must derive the same
P50-like estimate and upper-load envelope from an identical recorder sample.
This pure module owns only sample validation, recency weighting and robust
quantiles. It deliberately contains no tariff, RCE or voltage-control rules.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from statistics import median


_EPSILON = 1e-6
LOAD_MODEL_SCHEMA = "load_forecast_v3"
DAY_CORRECTION_MIN_EXPECTED_KWH = 1.0
DAY_CORRECTION_MIN_ELAPSED = timedelta(hours=2)
DAY_CORRECTION_MIN_RATIO = 0.80
DAY_CORRECTION_MAX_RATIO = 1.25
PERSISTENCE_WINDOW = timedelta(minutes=20)
PERSISTENCE_MIN_SPAN = timedelta(minutes=12)
PERSISTENCE_MAX_GAP = timedelta(minutes=5)
PERSISTENCE_MIN_DELTA_KW = 0.25
PERSISTENCE_MIN_RELATIVE_DELTA = 0.20
PERSISTENCE_MAX_DELTA_KW = 3.0
PERSISTENCE_GAIN = 0.25
PERSISTENCE_DECAY = timedelta(minutes=60)
PERSISTENCE_HORIZON = timedelta(hours=2)


@dataclass(frozen=True, slots=True)
class LoadForecastResult:
    """Policy-neutral expected LOAD plus bounded model diagnostics."""

    by_slot_kwh: Mapping[datetime, float]
    profile_mode: str
    current_day_correction_ratio: float
    current_day_correction_kwh: float
    persistence_delta_kw: float
    content_revision: str


def _clean_and_winsorise(
    values: Sequence[float],
    *,
    max_points: int,
    ages_days: Sequence[float] | None = None,
) -> tuple[list[float], list[float], list[float]]:
    """Return recent valid values and a common robust clipping envelope."""

    if ages_days is not None and len(ages_days) != len(values):
        raise ValueError("ages_days must align with values")
    valid: list[tuple[float, float]] = []
    for index, raw in enumerate(values):
        try:
            value = float(raw)
            age = float(ages_days[index]) if ages_days is not None else float(len(values) - 1 - index)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and 0.0 < value < 1_000.0 and math.isfinite(age) and age >= 0.0:
            valid.append((value, age))
    valid = valid[-max(max_points, 1) :]
    clean = [item[0] for item in valid]
    clean_ages = [item[1] for item in valid]
    if not clean:
        return [], [], []
    ordered = sorted(clean)
    middle = len(ordered) // 2
    centre = (
        ordered[middle]
        if len(ordered) % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )
    lower = max(centre * 0.45, 0.05)
    upper = max(centre * 1.75, lower)
    return clean, [min(max(value, lower), upper) for value in clean], clean_ages


def robust_weighted_estimate(
    values: Sequence[float],
    *,
    max_points: int = 28,
    ages_days: Sequence[float] | None = None,
) -> tuple[float | None, float, int]:
    """Return recency-weighted demand, relative uncertainty and sample count."""

    clean, clipped, ages = _clean_and_winsorise(
        values, max_points=max_points, ages_days=ages_days
    )
    if not clean:
        return None, 0.0, 0
    weights = [0.5 ** (age / 7.0) for age in ages]
    weight_sum = sum(weights)
    estimate = sum(
        value * weight for value, weight in zip(clipped, weights)
    ) / weight_sum
    uncertainty = sum(
        abs(value - estimate) / max(estimate, _EPSILON) * weight
        for value, weight in zip(clipped, weights)
    ) / weight_sum
    return estimate, min(max(uncertainty, 0.0), 1.0), len(clean)


def robust_weighted_upper_estimate(
    values: Sequence[float],
    *,
    max_points: int = 28,
    quantile: float = 0.90,
    ages_days: Sequence[float] | None = None,
) -> tuple[float | None, int]:
    """Return a recency-weighted, winsorised upper demand quantile."""

    clean, clipped, ages = _clean_and_winsorise(
        values, max_points=max_points, ages_days=ages_days
    )
    if not clean:
        return None, 0
    weighted = sorted(
        (
            value,
            0.5 ** (ages[index] / 7.0),
        )
        for index, value in enumerate(clipped)
    )
    threshold = sum(weight for _, weight in weighted) * min(
        max(quantile, 0.0),
        1.0,
    )
    cumulative = 0.0
    for value, weight in weighted:
        cumulative += weight
        if cumulative + _EPSILON >= threshold:
            return value, len(clean)
    return weighted[-1][0], len(clean)


def daily_ages_days(keys: Sequence[str], *, as_of: date) -> tuple[float, ...]:
    """Return actual calendar ages, preserving gaps in Recorder history."""
    result: list[float] = []
    for key in keys:
        try:
            parsed = date.fromisoformat(str(key))
        except ValueError:
            result.append(float("nan"))
        else:
            result.append(float(max((as_of - parsed).days, 0)))
    return tuple(result)


def _valid_profile(values: Sequence[float]) -> tuple[float, ...]:
    if len(values) != 48:
        return ()
    try:
        parsed = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError):
        return ()
    if any(not math.isfinite(value) or value < 0.0 for value in parsed):
        return ()
    return parsed if sum(parsed) > _EPSILON else ()


def _profile_for_date(
    day: date,
    average: Sequence[float],
    weekday: Sequence[float],
    weekend: Sequence[float],
) -> tuple[tuple[float, ...], str]:
    average_profile = _valid_profile(average)
    weekday_profile = _valid_profile(weekday)
    weekend_profile = _valid_profile(weekend)
    selected = weekend_profile if day.weekday() >= 5 else weekday_profile
    if selected:
        return selected, "weekend_48_slot" if day.weekday() >= 5 else "weekday_48_slot"
    if average_profile:
        return average_profile, "average_48_slot"
    if weekday_profile or weekend_profile:
        return weekday_profile or weekend_profile, "cross_day_48_slot_fallback"
    return tuple([1.0] * 48), "flat_fallback"


def _day_slots(day: date, tzinfo: object) -> list[datetime]:
    """Return 46/48/50 real half-hours for a local 23/24/25-hour day."""
    local_start = datetime.combine(day, time.min, tzinfo=tzinfo)  # type: ignore[arg-type]
    local_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tzinfo)  # type: ignore[arg-type]
    cursor = local_start.astimezone(timezone.utc)
    end = local_end.astimezone(timezone.utc)
    result: list[datetime] = []
    while cursor < end:
        result.append(cursor.astimezone(local_start.tzinfo))
        cursor += timedelta(minutes=30)
    return result


def _base_day_loads(
    day: date,
    tzinfo: object,
    daily_energy_kwh: float,
    average: Sequence[float],
    weekday: Sequence[float],
    weekend: Sequence[float],
    night_energy_kwh: float | None = None,
    night_start_minute: int = 22 * 60,
    night_end_minute: int = 6 * 60,
) -> tuple[dict[datetime, float], str]:
    profile, mode = _profile_for_date(day, average, weekday, weekend)
    slots = _day_slots(day, tzinfo)
    weights = [profile[slot.hour * 2 + slot.minute // 30] for slot in slots]
    if mode == "flat_fallback" and night_energy_kwh is not None:
        night_flags = []
        for slot in slots:
            minute = slot.hour * 60 + slot.minute
            is_night = (
                night_start_minute <= minute < night_end_minute
                if night_start_minute < night_end_minute
                else minute >= night_start_minute or minute < night_end_minute
            )
            night_flags.append(is_night)
        night_count = max(sum(night_flags), 1)
        day_count = max(len(slots) - sum(night_flags), 1)
        night_energy = min(max(float(night_energy_kwh), 0.0), max(float(daily_energy_kwh), 0.0))
        day_energy = max(float(daily_energy_kwh) - night_energy, 0.0)
        return {
            slot.astimezone(timezone.utc): (
                night_energy / night_count if flag else day_energy / day_count
            )
            for slot, flag in zip(slots, night_flags)
        }, "flat_day_night_fallback"
    total_weight = sum(weights)
    scale = max(float(daily_energy_kwh), 0.0) / max(total_weight, _EPSILON)
    return {
        slot.astimezone(timezone.utc): max(weight * scale, 0.0)
        for slot, weight in zip(slots, weights)
    }, mode


def expected_energy_between(
    *,
    start: datetime,
    end: datetime,
    daily_energy_kwh: float,
    average_profile_30m_kwh: Sequence[float] = (),
    weekday_profile_30m_kwh: Sequence[float] = (),
    weekend_profile_30m_kwh: Sequence[float] = (),
    night_energy_kwh: float | None = None,
    night_start_minute: int = 22 * 60,
    night_end_minute: int = 6 * 60,
) -> float:
    """Integrate expected energy over the exact real-time interval."""
    if end <= start or start.tzinfo is None or end.tzinfo is None:
        return 0.0
    total = 0.0
    day = start.date()
    while day <= end.astimezone(start.tzinfo).date():
        loads, _ = _base_day_loads(
            day,
            start.tzinfo,
            daily_energy_kwh,
            average_profile_30m_kwh,
            weekday_profile_30m_kwh,
            weekend_profile_30m_kwh,
            night_energy_kwh,
            night_start_minute,
            night_end_minute,
        )
        for slot_utc, energy in loads.items():
            slot_end = slot_utc + timedelta(minutes=30)
            overlap = max(
                (min(slot_end, end.astimezone(timezone.utc)) - max(slot_utc, start.astimezone(timezone.utc))).total_seconds(),
                0.0,
            )
            total += energy * overlap / 1800.0
        day += timedelta(days=1)
    return max(total, 0.0)


def current_day_profile_correction(
    *,
    now: datetime,
    observed_energy_kwh: float | None,
    observed_at: datetime | None,
    daily_energy_kwh: float,
    average_profile_30m_kwh: Sequence[float] = (),
    weekday_profile_30m_kwh: Sequence[float] = (),
    weekend_profile_30m_kwh: Sequence[float] = (),
    night_energy_kwh: float | None = None,
    night_start_minute: int = 22 * 60,
    night_end_minute: int = 6 * 60,
    persistence_delta_kw: float = 0.0,
    persistence_observed_at: datetime | None = None,
) -> tuple[float, float, float]:
    """Return a correction only while dense power evidence confirms the residual.

    The cumulative counter proves energy already consumed, but cannot by itself
    distinguish a one-off event from a durable change in future demand.  The
    bounded power-deviation window supplies that causal distinction.
    """
    if (
        observed_energy_kwh is None
        or observed_at is None
        or not math.isfinite(float(observed_energy_kwh))
        or float(observed_energy_kwh) < 0.0
        or observed_at.tzinfo is None
        or abs((now.astimezone(timezone.utc) - observed_at.astimezone(timezone.utc)).total_seconds()) > 600.0
    ):
        return 1.0, 0.0, 0.0
    local_observed = observed_at.astimezone(now.tzinfo)
    if local_observed.date() != now.astimezone(now.tzinfo).date():
        return 1.0, 0.0, 0.0
    start = datetime.combine(local_observed.date(), time.min, tzinfo=now.tzinfo)
    if observed_at - start < DAY_CORRECTION_MIN_ELAPSED:
        return 1.0, 0.0, 0.0
    expected = expected_energy_between(
        start=start,
        end=observed_at,
        daily_energy_kwh=daily_energy_kwh,
        average_profile_30m_kwh=average_profile_30m_kwh,
        weekday_profile_30m_kwh=weekday_profile_30m_kwh,
        weekend_profile_30m_kwh=weekend_profile_30m_kwh,
        night_energy_kwh=night_energy_kwh,
        night_start_minute=night_start_minute,
        night_end_minute=night_end_minute,
    )
    if expected < DAY_CORRECTION_MIN_EXPECTED_KWH:
        return 1.0, 0.0, expected
    residual = float(observed_energy_kwh) - expected
    raw_ratio = min(
        max(float(observed_energy_kwh) / expected, DAY_CORRECTION_MIN_RATIO),
        DAY_CORRECTION_MAX_RATIO,
    )
    try:
        persistence = float(persistence_delta_kw)
        persistence_at_utc = persistence_observed_at.astimezone(timezone.utc)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 1.0, residual, expected
    persistence_age = (
        now.astimezone(timezone.utc) - persistence_at_utc
    ).total_seconds()
    if (
        not math.isfinite(persistence)
        or abs(persistence) < PERSISTENCE_MIN_DELTA_KW
        or persistence_age < -5.0
        or persistence_age > PERSISTENCE_MAX_GAP.total_seconds()
        or residual * persistence <= 0.0
    ):
        return 1.0, residual, expected

    # A qualified persistence value already represents at least 12 minutes of
    # dense samples with no gap over five minutes.  Scale the daily correction
    # by whether it can explain the average residual rate and gently age it;
    # do not project the counter residual when current power returned to normal.
    elapsed_hours = max(
        (local_observed.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()
        / 3600.0,
        0.5,
    )
    required_delta_kw = max(
        PERSISTENCE_MIN_DELTA_KW,
        abs(residual) / elapsed_hours,
    )
    evidence_strength = min(abs(persistence) / required_delta_kw, 1.0)
    freshness = max(
        1.0 - persistence_age / PERSISTENCE_DECAY.total_seconds(),
        0.0,
    )
    ratio = 1.0 + (raw_ratio - 1.0) * evidence_strength * freshness
    return ratio, residual, expected


def persistent_load_delta_kw(
    observations: Sequence[tuple[datetime, float, float]], *, now: datetime
) -> tuple[float, datetime | None, int]:
    """Return causal median deviation after 12 minutes of dense evidence."""
    valid: list[tuple[datetime, float, float]] = []
    lower = now.astimezone(timezone.utc) - PERSISTENCE_WINDOW
    for stamp, actual, expected in observations:
        try:
            stamp_utc = stamp.astimezone(timezone.utc)
            actual_value = float(actual)
            expected_value = float(expected)
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if (
            lower <= stamp_utc <= now.astimezone(timezone.utc)
            and math.isfinite(actual_value)
            and math.isfinite(expected_value)
            and actual_value >= 0.0
            and expected_value >= 0.0
        ):
            valid.append((stamp_utc, actual_value, expected_value))
    valid.sort(key=lambda item: item[0])
    if len(valid) < 4 or valid[-1][0] - valid[0][0] < PERSISTENCE_MIN_SPAN:
        return 0.0, valid[-1][0] if valid else None, len(valid)
    if max((b[0] - a[0] for a, b in zip(valid, valid[1:])), default=timedelta.max) > PERSISTENCE_MAX_GAP:
        return 0.0, valid[-1][0], len(valid)
    actual_median = median(item[1] for item in valid)
    expected_median = median(item[2] for item in valid)
    delta = actual_median - expected_median
    threshold = max(PERSISTENCE_MIN_DELTA_KW, expected_median * PERSISTENCE_MIN_RELATIVE_DELTA)
    if abs(delta) < threshold:
        delta = 0.0
    delta = min(max(delta, -PERSISTENCE_MAX_DELTA_KW), PERSISTENCE_MAX_DELTA_KW)
    return delta, valid[-1][0], len(valid)


def expected_load_by_slot(
    starts: Sequence[datetime],
    *,
    now: datetime,
    daily_energy_kwh: float,
    average_profile_30m_kwh: Sequence[float] = (),
    weekday_profile_30m_kwh: Sequence[float] = (),
    weekend_profile_30m_kwh: Sequence[float] = (),
    night_energy_kwh: float | None = None,
    night_start_minute: int = 22 * 60,
    night_end_minute: int = 6 * 60,
    current_day_energy_kwh: float | None = None,
    current_day_observed_at: datetime | None = None,
    persistence_delta_kw: float = 0.0,
    persistence_observed_at: datetime | None = None,
) -> LoadForecastResult:
    """Build shared full-slot energy; consumers clip the current slot once.

    Instantaneous power belongs to execution caps, not a remaining-slot energy
    prediction. Only the existing dense persistence evidence adjusts the model.
    """
    if now.tzinfo is None or any(start.tzinfo is None for start in starts):
        raise ValueError("LOAD forecast timestamps must be timezone-aware")
    ratio, residual, expected_elapsed = current_day_profile_correction(
        now=now,
        observed_energy_kwh=current_day_energy_kwh,
        observed_at=current_day_observed_at,
        daily_energy_kwh=daily_energy_kwh,
        average_profile_30m_kwh=average_profile_30m_kwh,
        weekday_profile_30m_kwh=weekday_profile_30m_kwh,
        weekend_profile_30m_kwh=weekend_profile_30m_kwh,
        night_energy_kwh=night_energy_kwh,
        night_start_minute=night_start_minute,
        night_end_minute=night_end_minute,
        persistence_delta_kw=persistence_delta_kw,
        persistence_observed_at=persistence_observed_at,
    )
    result: dict[datetime, float] = {}
    modes: set[str] = set()
    by_day: dict[date, tuple[dict[datetime, float], str]] = {}
    for start in starts:
        effective_start = max(start.astimezone(timezone.utc), now.astimezone(timezone.utc))
        slot_end = start.astimezone(timezone.utc) + timedelta(minutes=30)
        local = start.astimezone(now.tzinfo)
        if local.date() not in by_day:
            by_day[local.date()] = _base_day_loads(
                local.date(), now.tzinfo, daily_energy_kwh,
                average_profile_30m_kwh, weekday_profile_30m_kwh, weekend_profile_30m_kwh,
                night_energy_kwh, night_start_minute, night_end_minute,
            )
        loads, mode = by_day[local.date()]
        modes.add(mode)
        value = loads.get(start.astimezone(timezone.utc), 0.0)
        if (
            current_day_observed_at is not None
            and local.date() == now.date()
            and slot_end > now.astimezone(timezone.utc)
            and effective_start >= current_day_observed_at.astimezone(timezone.utc)
        ):
            value *= ratio
        if (
            persistence_observed_at is not None
            and math.isfinite(persistence_delta_kw)
            and abs(ratio - 1.0) <= _EPSILON
            and slot_end > now.astimezone(timezone.utc)
        ):
            age = (now.astimezone(timezone.utc) - persistence_observed_at.astimezone(timezone.utc)).total_seconds()
            horizon = (effective_start - now.astimezone(timezone.utc)).total_seconds()
            if 0.0 <= age <= PERSISTENCE_MAX_GAP.total_seconds() and horizon <= PERSISTENCE_HORIZON.total_seconds():
                decay = (1.0 - age / PERSISTENCE_DECAY.total_seconds()) * (1.0 - horizon / PERSISTENCE_HORIZON.total_seconds())
                value += persistence_delta_kw * PERSISTENCE_GAIN * 0.5 * max(decay, 0.0)
        result[start] = max(value, 0.0)
    mode = "+".join(sorted(modes)) if modes else "no_slots"
    payload = {
        "schema": LOAD_MODEL_SCHEMA,
        "daily": round(float(daily_energy_kwh), 6),
        "ratio": round(ratio, 6),
        "residual": round(residual, 6),
        "applied_day_correction": round(
            expected_elapsed * (ratio - 1.0), 6
        ),
        "persistence": round(float(persistence_delta_kw), 6),
        "persistence_at": persistence_observed_at.isoformat() if persistence_observed_at else None,
        "loads": [(start.isoformat(), round(value, 6)) for start, value in result.items()],
    }
    revision = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    # ``residual`` remains part of the content revision as diagnostic evidence;
    # the public correction value reports only what was actually projected.
    applied_correction = expected_elapsed * (ratio - 1.0)
    return LoadForecastResult(
        result,
        mode,
        ratio,
        applied_correction,
        persistence_delta_kw,
        revision,
    )
