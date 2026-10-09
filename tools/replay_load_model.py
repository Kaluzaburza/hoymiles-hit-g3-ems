"""Causal old/new LOAD forecast replay over a bounded HA Recorder export."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import statistics
import sys
from time import perf_counter
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from load_model import (  # noqa: E402
    daily_ages_days,
    expected_load_by_slot,
    persistent_load_delta_kw,
    robust_weighted_estimate,
)
from rce_history import LOAD_HISTORY_ENTITIES, summarize_load_history  # noqa: E402

WARSAW = ZoneInfo("Europe/Warsaw")
HORIZONS = (1, 4, 12)  # 30 min, 2 h, 6 h
# Declared before candidate replay: no more than 10% MAE regression, 0.10 kWh
# additional absolute bias, five percentage points more underestimation, or the
# previously recorded 4.5654265 kWh maximum six-hour underestimate.
TOLERANCES = {
    "mae_ratio": 1.10,
    "absolute_bias_kwh": 0.10,
    "underestimate_frequency_points": 0.05,
    "max_6h_underestimate_kwh": 4.565426542852509,
    "p95_compute_ms": 50.0,
}
_ROW_KEYS: dict[int, list[datetime]] = {}


def floor_half_hour(value: datetime) -> datetime:
    return value.replace(minute=(value.minute // 30) * 30, second=0, microsecond=0)


def numeric_rows(raw: list[list[object]]) -> list[tuple[datetime, float]]:
    result = []
    for stamp, state in raw:
        try:
            value = float(state)
            moment = datetime.fromtimestamp(float(stamp), timezone.utc).astimezone(WARSAW)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value):
            result.append((moment, value))
    return result


def latest_at(rows: list[tuple[datetime, float]], stamp: datetime) -> tuple[datetime, float] | None:
    keys = _ROW_KEYS.setdefault(
        id(rows), [item[0].astimezone(timezone.utc) for item in rows]
    )
    index = bisect_right(keys, stamp.astimezone(timezone.utc)) - 1
    return rows[index] if index >= 0 else None


def energy_from_power(
    rows: list[tuple[datetime, float]], start: datetime, end: datetime
) -> float | None:
    keys = _ROW_KEYS.setdefault(
        id(rows), [item[0].astimezone(timezone.utc) for item in rows]
    )
    left = max(bisect_right(keys, start.astimezone(timezone.utc)) - 1, 0)
    right = bisect_right(keys, end.astimezone(timezone.utc))
    selected = rows[left:right]
    before = latest_at(rows, start)
    if before is not None and (not selected or selected[0][0] > start):
        selected.insert(0, (start, before[1]))
    if len(selected) < 2 or selected[-1][0] < end - timedelta(minutes=2):
        return None
    total = 0.0
    for (left, power_w), (right, _) in zip(selected, selected[1:]):
        duration = (min(right, end) - max(left, start)).total_seconds()
        if duration > 0.0:
            total += max(power_w, 0.0) / 1000.0 * duration / 3600.0
    if selected[-1][0] < end:
        total += max(selected[-1][1], 0.0) / 1000.0 * (end - selected[-1][0]).total_seconds() / 3600.0
    return total


def metrics(errors: list[float]) -> dict[str, float | int]:
    under = [-value for value in errors if value < 0.0]
    return {
        "count": len(errors),
        "mae_kwh": statistics.fmean(abs(value) for value in errors) if errors else 0.0,
        "signed_error_kwh": statistics.fmean(errors) if errors else 0.0,
        "underestimate_frequency": len(under) / len(errors) if errors else 0.0,
        "underestimate_mean_kwh": statistics.fmean(under) if under else 0.0,
        "underestimate_max_kwh": max(under, default=0.0),
    }


def replay(source: Path) -> dict[str, object]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    rows = {key: numeric_rows(value) for key, value in payload["entities"].items()}
    history_samples = {key: rows.get(key, []) for key in LOAD_HISTORY_ENTITIES}
    power_rows = rows["sensor.hoymiles_actual_load_power"]
    energy_rows = rows["sensor.hoymiles_actual_load_energy_today"]
    first_day = min(item[0].date() for item in power_rows)
    last_day = max(item[0].date() for item in power_rows)
    all_errors: dict[str, dict[int, list[float]]] = {
        "legacy": defaultdict(list),
        "candidate": defaultdict(list),
    }
    category_errors: dict[str, dict[int, list[float]]] = defaultdict(lambda: defaultdict(list))
    compute_ms: list[float] = []
    forecast_points = 0

    day = first_day + timedelta(days=1)
    while day <= last_day:
        day_start = datetime.combine(day, time.min, tzinfo=WARSAW)
        history = summarize_load_history(
            history_samples,
            now=day_start,
            night_windows={},
            history_days=28,
        )
        if history.daily_history_days < 3 or not history.average_profile_kwh:
            day += timedelta(days=1)
            continue
        keys = tuple(history.daily_energy_kwh)
        values = tuple(history.daily_energy_kwh.values())
        baseline, _, _ = robust_weighted_estimate(
            values, ages_days=daily_ages_days(keys, as_of=day)
        )
        if baseline is None:
            day += timedelta(days=1)
            continue
        point = day_start
        while point + timedelta(hours=6) <= min(
            datetime.combine(day + timedelta(days=1), time.min, tzinfo=WARSAW),
            power_rows[-1][0],
        ):
            observed = latest_at(energy_rows, point)
            live = latest_at(power_rows, point)
            if observed is None or live is None or observed[0].date() != day:
                point += timedelta(minutes=30)
                continue
            slots = [point.astimezone(timezone.utc) + timedelta(minutes=30 * i) for i in range(12)]
            base_current = expected_load_by_slot(
                (slots[0],),
                now=point,
                daily_energy_kwh=baseline,
                average_profile_30m_kwh=history.average_profile_kwh,
                weekday_profile_30m_kwh=history.weekday_profile_kwh,
                weekend_profile_30m_kwh=history.weekend_profile_kwh,
            ).by_slot_kwh[slots[0]] * 2.0
            power_keys = _ROW_KEYS.setdefault(
                id(power_rows), [item[0].astimezone(timezone.utc) for item in power_rows]
            )
            recent_left = bisect_right(
                power_keys, (point - timedelta(minutes=20)).astimezone(timezone.utc)
            )
            recent_right = bisect_right(power_keys, point.astimezone(timezone.utc))
            recent_raw = power_rows[recent_left:recent_right]
            recent = [(stamp, max(value, 0.0) / 1000.0, base_current) for stamp, value in recent_raw]
            persistence, persistence_at, _ = persistent_load_delta_kw(recent, now=point)
            started = perf_counter()
            candidate = expected_load_by_slot(
                slots,
                now=point,
                daily_energy_kwh=baseline,
                average_profile_30m_kwh=history.average_profile_kwh,
                weekday_profile_30m_kwh=history.weekday_profile_kwh,
                weekend_profile_30m_kwh=history.weekend_profile_kwh,
                current_day_energy_kwh=observed[1],
                current_day_observed_at=observed[0],
                persistence_delta_kw=persistence,
                persistence_observed_at=persistence_at,
            ).by_slot_kwh
            elapsed_fraction = max((point.hour * 60 + point.minute) / 1440.0, 0.25)
            legacy_daily = max(baseline, max(observed[1], 0.0) / elapsed_fraction)
            legacy = expected_load_by_slot(
                slots,
                now=point,
                daily_energy_kwh=legacy_daily,
                average_profile_30m_kwh=history.average_profile_kwh,
                weekday_profile_30m_kwh=history.weekday_profile_kwh,
                weekend_profile_30m_kwh=history.weekend_profile_kwh,
            ).by_slot_kwh
            compute_ms.append((perf_counter() - started) * 1000.0)
            current_energy = max(live[1], 0.0) / 1000.0 * 0.5
            candidate[slots[0]] = current_energy
            legacy[slots[0]] = current_energy
            instantaneous_delta = max(live[1], 0.0) / 1000.0 - base_current
            if persistence >= 0.25:
                category = "persistent_high"
            elif persistence <= -0.25:
                category = "persistent_low"
            elif abs(instantaneous_delta) >= 0.50:
                category = "impulse"
            else:
                category = "ordinary"
            for horizon in HORIZONS:
                actual = energy_from_power(power_rows, point, point + timedelta(minutes=30 * horizon))
                if actual is None:
                    continue
                old_value = sum(legacy[slot] for slot in slots[:horizon])
                new_value = sum(candidate[slot] for slot in slots[:horizon])
                all_errors["legacy"][horizon].append(old_value - actual)
                all_errors["candidate"][horizon].append(new_value - actual)
                category_errors[category][horizon].append(new_value - actual)
            forecast_points += 1
            point += timedelta(minutes=30)
        day += timedelta(days=1)

    result_metrics = {
        model: {str(h * 30): metrics(values) for h, values in horizons.items()}
        for model, horizons in all_errors.items()
    }
    category_metrics = {
        category: {str(h * 30): metrics(values) for h, values in horizons.items()}
        for category, horizons in category_errors.items()
    }
    checks = {}
    for horizon in HORIZONS:
        key = str(horizon * 30)
        old = result_metrics["legacy"][key]
        new = result_metrics["candidate"][key]
        checks[key] = {
            "mae": new["mae_kwh"] <= old["mae_kwh"] * TOLERANCES["mae_ratio"],
            "bias": abs(new["signed_error_kwh"]) <= abs(old["signed_error_kwh"]) + TOLERANCES["absolute_bias_kwh"],
            "underestimate_frequency": new["underestimate_frequency"] <= old["underestimate_frequency"] + TOLERANCES["underestimate_frequency_points"],
        }
        if horizon == 12:
            checks[key]["max_underestimate"] = (
                new["underestimate_max_kwh"]
                <= TOLERANCES["max_6h_underestimate_kwh"]
            )
    p95 = sorted(compute_ms)[max(math.ceil(len(compute_ms) * 0.95) - 1, 0)] if compute_ms else 0.0
    return {
        "schema": "ems_load_replay_v1",
        "source": str(source),
        "source_range": [payload["range_start"], payload["range_end"]],
        "retained_calendar_days": (last_day - first_day).days + 1,
        "complete_training_days_at_end": summarize_load_history(
            history_samples,
            now=datetime.combine(last_day + timedelta(days=1), time.min, tzinfo=WARSAW),
            night_windows={},
            history_days=28,
        ).daily_history_days,
        "forecast_points": forecast_points,
        "metrics": result_metrics,
        "candidate_categories": category_metrics,
        "compute_ms": {
            "mean": statistics.fmean(compute_ms) if compute_ms else 0.0,
            "p95": p95,
            "max": max(compute_ms, default=0.0),
        },
        "tolerances": TOLERANCES,
        "checks": checks,
        "pass": bool(checks) and all(all(group.values()) for group in checks.values()) and p95 <= TOLERANCES["p95_compute_ms"],
        "limitations": [
            "Recorder retained LOAD only from 2026-09-02; fewer than 14 calendar days are available.",
            "Cost/import/export counterfactuals are not evaluated because historical point-in-time price and PV forecast vintages are unavailable.",
        ],
    }


def main() -> None:
    if len(sys.argv) not in {2, 3}:
        raise SystemExit("usage: replay_load_model.py SOURCE.json [RESULT.json]")
    result = replay(Path(sys.argv[1]))
    rendered = json.dumps(result, indent=2, ensure_ascii=False)
    if len(sys.argv) == 3:
        Path(sys.argv[2]).write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    raise SystemExit(0 if result["pass"] else 1)


if __name__ == "__main__":
    main()
