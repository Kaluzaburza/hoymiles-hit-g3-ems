"""Bounded usefulness policy for scheduled Solcast forecast pauses.

The raw Home Assistant state age remains observable and strict.  This module
adds a separate, finite usability decision for the normal overnight pause in
Solcast API updates.  Missing, malformed, future-dated, wrong-horizon and
overdue data never become usable through this policy.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from typing import Any

from .energy_data import numeric_state_sample, state_reported_at


SOLCAST_UPDATE_ENTITY_CANDIDATES = (
    "sensor.solcast_pv_forecast_last_updated",
    "sensor.solcast_pv_forecast_api_last_updated",
    "sensor.solcast_pv_forecast_ostatnia_aktualizacja_api",
)

SCHEDULED_PAUSE_GRACE_SECONDS = 90.0 * 60.0
SCHEDULED_PAUSE_MAX_SOURCE_AGE_SECONDS = 30.0 * 60.0 * 60.0
SCHEDULED_PAUSE_MAX_LOOKAHEAD_SECONDS = 36.0 * 60.0 * 60.0


@dataclass(frozen=True, slots=True)
class PVForecastUsefulness:
    """Forecast value plus independent freshness and usefulness evidence."""

    value: float | None
    age_seconds: float | None
    source_fresh: bool
    source_reason: str
    usable: bool
    mode: str
    reason: str
    source_reported_at: datetime | None
    last_success_at: datetime | None
    target_date: date
    coverage_start_date: date | None
    coverage_end_date: date | None
    coverage_complete: bool
    next_update_at: datetime | None
    valid_until: datetime | None

    @property
    def fresh(self) -> bool:
        """Compatibility readiness flag; source freshness stays separate."""

        return self.usable

    @property
    def reported_at(self) -> datetime | None:
        """Compatibility alias for numeric-sample consumers."""

        return self.source_reported_at

    def as_public_dict(self) -> dict[str, Any]:
        """Return a stable JSON-safe diagnostic projection."""

        return {
            "value": round(self.value, 6) if self.value is not None else None,
            "age_seconds": (
                round(self.age_seconds, 3)
                if self.age_seconds is not None and isfinite(self.age_seconds)
                else None
            ),
            "source_fresh": self.source_fresh,
            "source_reason": self.source_reason,
            "usable": self.usable,
            "mode": self.mode,
            "reason": self.reason,
            "source_reported_at": _iso(self.source_reported_at),
            "last_success_at": _iso(self.last_success_at),
            "target_date": self.target_date.isoformat(),
            "coverage_start_date": (
                self.coverage_start_date.isoformat()
                if self.coverage_start_date is not None
                else None
            ),
            "coverage_end_date": (
                self.coverage_end_date.isoformat()
                if self.coverage_end_date is not None
                else None
            ),
            "coverage_complete": self.coverage_complete,
            "next_update_at": _iso(self.next_update_at),
            "valid_until": _iso(self.valid_until),
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _aware(value: datetime, now: datetime) -> datetime | None:
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(now.tzinfo or timezone.utc)


def _parse_datetime(value: Any, now: datetime) -> datetime | None:
    if isinstance(value, datetime):
        return _aware(value, now)
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return _aware(parsed, now)


def _iter_schedule_values(value: Any) -> Iterable[Any]:
    if isinstance(value, str):
        yield from (item.strip() for item in value.split(",") if item.strip())
    elif isinstance(value, (list, tuple)):
        yield from value


def _coverage_evidence(
    state: Any,
    now: datetime,
) -> tuple[tuple[date, ...], frozenset[date]]:
    attributes = getattr(state, "attributes", {})
    if not isinstance(attributes, Mapping):
        return (), frozenset()
    rows = attributes.get("detailedForecast")
    if not isinstance(rows, list):
        rows = attributes.get("detailed_forecast")
    if not isinstance(rows, list):
        analysis = attributes.get("analysis")
        rows = analysis.get("intervals") if isinstance(analysis, Mapping) else None
    if not isinstance(rows, list):
        return (), frozenset()
    dates: set[date] = set()
    starts_by_date: dict[date, list[datetime]] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        start = _parse_datetime(
            row.get("period_start") or row.get("period_start_local"),
            now,
        )
        if start is not None:
            dates.add(start.date())
            starts_by_date.setdefault(start.date(), []).append(start)
    complete: set[date] = set()
    for target, starts in starts_by_date.items():
        unique_by_utc = {
            value.astimezone(timezone.utc): value
            for value in starts
        }
        ordered = [unique_by_utc[key] for key in sorted(unique_by_utc)]
        if len(ordered) < 24:
            continue
        first = ordered[0]
        last = ordered[-1]
        if first.hour != 0 or first.minute > 30 or last.hour < 23:
            continue
        gaps = [
            (
                later.astimezone(timezone.utc)
                - earlier.astimezone(timezone.utc)
            ).total_seconds()
            for earlier, later in zip(ordered, ordered[1:])
        ]
        if gaps and max(gaps) <= 60.0 * 60.0:
            complete.add(target)
    return tuple(sorted(dates)), frozenset(complete)


def resolve_solcast_update_state(states: Any) -> Any | None:
    """Resolve the integration's API-success sensor without registry writes."""

    getter = getattr(states, "get", None)
    if not callable(getter):
        return None
    for entity_id in SOLCAST_UPDATE_ENTITY_CANDIDATES:
        state = getter(entity_id)
        if state is not None:
            return state
    return None


def _schedule_evidence(
    update_state: Any,
    sun_state: Any,
    now: datetime,
) -> tuple[datetime | None, datetime | None, str]:
    last_success = _parse_datetime(getattr(update_state, "state", None), now)
    update_attributes = getattr(update_state, "attributes", {})
    if not isinstance(update_attributes, Mapping):
        update_attributes = {}

    next_update = _parse_datetime(update_attributes.get("next_auto_update"), now)
    if next_update is None:
        candidates = sorted(
            parsed
            for raw in _iter_schedule_values(
                update_attributes.get("auto_update_queue")
            )
            if (parsed := _parse_datetime(raw, now)) is not None
            and parsed >= now - timedelta(seconds=SCHEDULED_PAUSE_GRACE_SECONDS)
        )
        next_update = candidates[0] if candidates else None
    if next_update is not None:
        return last_success, next_update, "solcast_schedule"

    sun_attributes = getattr(sun_state, "attributes", {})
    if (
        getattr(sun_state, "state", None) == "below_horizon"
        and isinstance(sun_attributes, Mapping)
    ):
        rising = _parse_datetime(sun_attributes.get("next_rising"), now)
        if rising is not None:
            return last_success, rising, "sunrise_fallback"
    return last_success, None, "schedule_unavailable"


def evaluate_pv_forecast_usefulness(
    state: Any,
    now: datetime,
    *,
    target_date: date,
    max_age_seconds: float,
    update_state: Any = None,
    sun_state: Any = None,
    minimum: float = 0.0,
    maximum: float = 100_000.0,
) -> PVForecastUsefulness:
    """Evaluate a forecast without redefining the source freshness TTL."""

    strict = numeric_state_sample(
        state,
        now,
        max_age_seconds=max_age_seconds,
        minimum=minimum,
        maximum=maximum,
    )
    bounded = numeric_state_sample(
        state,
        now,
        max_age_seconds=SCHEDULED_PAUSE_MAX_SOURCE_AGE_SECONDS,
        minimum=minimum,
        maximum=maximum,
    )
    dates, complete_dates = _coverage_evidence(state, now)
    coverage_start = dates[0] if dates else None
    coverage_end = dates[-1] if dates else None
    attributes = getattr(state, "attributes", {})
    data_correct = (
        attributes.get("dataCorrect")
        if isinstance(attributes, Mapping)
        else None
    )
    horizon_mismatch = bool(dates and target_date not in dates)
    source_reported = state_reported_at(state)
    last_success, next_update, schedule_source = _schedule_evidence(
        update_state,
        sun_state,
        now,
    )
    if last_success is None:
        last_success = source_reported

    common = {
        "age_seconds": strict.age_seconds,
        "source_fresh": strict.fresh,
        "source_reason": strict.reason,
        "source_reported_at": source_reported,
        "last_success_at": last_success,
        "target_date": target_date,
        "coverage_start_date": coverage_start,
        "coverage_end_date": coverage_end,
        "coverage_complete": target_date in complete_dates,
        "next_update_at": next_update,
    }

    if data_correct is False:
        return PVForecastUsefulness(
            value=None,
            usable=False,
            mode="expired",
            reason="source_data_incorrect",
            valid_until=None,
            **common,
        )
    if horizon_mismatch:
        return PVForecastUsefulness(
            value=None,
            usable=False,
            mode="expired",
            reason="forecast_horizon_mismatch",
            valid_until=None,
            **common,
        )
    if strict.fresh:
        return PVForecastUsefulness(
            value=strict.value,
            usable=True,
            mode="fresh",
            reason="fresh",
            valid_until=None,
            **common,
        )
    if strict.reason != "stale" or not bounded.fresh:
        return PVForecastUsefulness(
            value=None,
            usable=False,
            mode="expired",
            reason=strict.reason if strict.reason != "stale" else bounded.reason,
            valid_until=None,
            **common,
        )
    if not dates:
        return PVForecastUsefulness(
            value=None,
            usable=False,
            mode="expired",
            reason="forecast_coverage_unavailable",
            valid_until=None,
            **common,
        )
    if target_date not in complete_dates:
        return PVForecastUsefulness(
            value=None,
            usable=False,
            mode="expired",
            reason="forecast_coverage_incomplete",
            valid_until=None,
            **common,
        )
    if next_update is None:
        return PVForecastUsefulness(
            value=None,
            usable=False,
            mode="expired",
            reason=schedule_source,
            valid_until=None,
            **common,
        )
    now_utc = now.astimezone(timezone.utc)
    next_update_utc = next_update.astimezone(timezone.utc)
    if (
        next_update_utc - now_utc
    ).total_seconds() > SCHEDULED_PAUSE_MAX_LOOKAHEAD_SECONDS:
        return PVForecastUsefulness(
            value=None,
            usable=False,
            mode="expired",
            reason="next_update_implausibly_distant",
            valid_until=None,
            **common,
        )
    deadlines = [
        next_update + timedelta(seconds=SCHEDULED_PAUSE_GRACE_SECONDS),
    ]
    if last_success is not None:
        if last_success.astimezone(timezone.utc) > now_utc + timedelta(seconds=5):
            return PVForecastUsefulness(
                value=None,
                usable=False,
                mode="expired",
                reason="last_success_in_future",
                valid_until=None,
                **common,
            )
        deadlines.append(
            last_success
            + timedelta(seconds=SCHEDULED_PAUSE_MAX_SOURCE_AGE_SECONDS)
        )
    valid_until = min(deadlines)
    if now_utc > valid_until.astimezone(timezone.utc):
        return PVForecastUsefulness(
            value=None,
            usable=False,
            mode="expired",
            reason="scheduled_pause_deadline_expired",
            valid_until=valid_until,
            **common,
        )
    return PVForecastUsefulness(
        value=bounded.value,
        usable=True,
        mode="scheduled_pause",
        reason=f"scheduled_pause_{schedule_source}",
        valid_until=valid_until,
        **common,
    )
