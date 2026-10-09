"""Forecast-only EV filtering. Never alter metered LOAD or infer command power."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from math import isfinite
import re
from statistics import median

from .rce_history import LoadHistorySummary
from .load_history_store import merge_history

ENABLED = 'input_boolean.hoymiles_ev_load_filter_enabled'
POWER = 'input_number.hoymiles_ev_charge_power'
SENSOR = 'input_text.hoymiles_ev_power_sensor'
HELPERS = frozenset((ENABLED, POWER, SENSOR))
MAX_DAYS = 28
MAX_POWER_GAP_SECONDS = 600


@dataclass(frozen=True)
class Config:
    enabled: bool
    typical_kw: float
    entity_id: str

    @property
    def valid(self):
        return isfinite(self.typical_kw) and 1 <= self.typical_kw <= 50 and (
            not self.entity_id or bool(re.fullmatch(r'sensor\.[a-z0-9_]+', self.entity_id))
            and self.entity_id not in {
                'sensor.hoymiles_actual_load_power', 'sensor.hoymiles_actual_load_energy_today',
            }
        )


def power_kw(value, unit):
    """Only a nonnegative power measurement, never energy, nominal power or V2G."""
    if unit not in ('W', 'kW') or isinstance(value, bool):
        return None
    try:
        number = float(value) / (1000 if unit == 'W' else 1)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) and 0 <= number <= 100 else None


def suspected_ev(profile, typical_kw):
    """An hour of large excess is suspect, not proof of a particular appliance.

    Exclude the day; do not subtract a guessed nominal charging power. The
    low quartile is deliberately used only as a within-day detection baseline.
    Variable PV-following EV and continuous all-day charging need a real sensor.
    """
    baseline = median(sorted(profile)[:12]) * 2
    run = 0
    for energy in profile:
        excess = energy * 2 - baseline
        run = run + 1 if .65 * typical_kw <= excess <= 1.4 * typical_kw else 0
        if run >= 2:
            return True
    return False


def integrate_day(rows, start: datetime, unit: str):
    """Integrate left-held W/kW in UTC into 48 wall-clock buckets (DST safe).

    Positive observations expire after 10 min. A recorded zero can be held
    without subtracting energy. Explicit outages/conflicting duplicates reject
    the day. Missing history is not a zero. No resampling of power is allowed.
    """
    if start.tzinfo is None or unit not in ('W', 'kW'):
        return None
    end = (start + timedelta(days=1)).astimezone(timezone.utc)
    begin = start.astimezone(timezone.utc)
    samples = {}
    for stamp, value in rows:
        if not isinstance(stamp, datetime) or stamp.tzinfo is None:
            return None
        stamp = stamp.astimezone(timezone.utc)
        if stamp > end:
            continue
        parsed = power_kw(value, unit)
        if stamp in samples and samples[stamp] != parsed:
            return None
        samples[stamp] = parsed
    points = sorted(samples.items())
    if not points or points[0][0] > begin:
        return None
    buckets = [0.] * 48
    for index, (stamp, value) in enumerate(points):
        right = points[index+1][0] if index+1 < len(points) else end
        left, right = max(stamp, begin), min(right, end)
        if right <= left:
            continue
        if value is None or value > 0 and (right-stamp).total_seconds() > MAX_POWER_GAP_SECONDS:
            return None
        while left < right:
            # UTC half-hours align with Polish clock half-hours, including fold.
            boundary = left.replace(minute=30 if left.minute < 30 else 0, second=0, microsecond=0)
            if left.minute >= 30:
                boundary += timedelta(hours=1)
            stop = min(right, boundary)
            local = left.astimezone(start.tzinfo)
            buckets[local.hour*2 + local.minute//30] += value * (stop-left).total_seconds()/3600
            left = stop
    return tuple(round(v, 6) for v in buckets)


def project_history(raw: LoadHistorySummary, config: Config, corrections):
    """Build a derived learning view AFTER the raw qualified-history merge."""
    if not config.enabled or not config.valid:
        return raw, 0
    daily, profiles = {}, {}
    for key in sorted(raw.daily_energy_kwh)[-MAX_DAYS:]:
        profile = (raw.profile_kwh_by_date or {}).get(key)
        if not profile or len(profile) != 48:
            continue
        if config.entity_id:
            ev = corrections.get(key)
            if ev is None or len(ev) != 48 or any(
                not isfinite(e) or e < 0 or e > p + .005 for p,e in zip(profile, ev)
            ):
                continue
            profile = tuple(round(max(0., p-e), 6) for p,e in zip(profile, ev))
        elif suspected_ev(profile, config.typical_kw):
            continue
        profiles[key] = profile
        daily[key] = round(sum(profile), 6)
    # Raw astronomical nights mix two calendar days. Let the common optimizer
    # derive night demand from the household profile, not the unfiltered counter.
    view = replace(raw, daily_energy_kwh=daily, profile_kwh_by_date=profiles,
        night_energy_kwh={}, average_night_kwh=None, night_history_days=0,
        current_day_energy_kwh=None, current_day_observed_at=None)
    view = merge_history(view, view, limit=MAX_DAYS)
    return view, len(raw.daily_energy_kwh) - len(daily)
