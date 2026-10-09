"""Focused A03 tests for cumulative PV learning evidence."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "hoymiles_hit_modbus" / "forecast_model.py"
SPEC = importlib.util.spec_from_file_location("forecast_history_quality", MODULE)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load forecast_model")
FORECAST = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = FORECAST
SPEC.loader.exec_module(FORECAST)

WARSAW = ZoneInfo("Europe/Warsaw")


def _stamp(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=WARSAW)


def main() -> None:
    day = date(2026, 9, 19)
    start = _stamp(day, 0)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=WARSAW)
    production_end = _stamp(day, 19)
    qualify = FORECAST.qualified_cumulative_energy_day

    assert qualify(
        [(start, 0.0), (_stamp(day, 12), 6.0)],
        day_start=start,
        day_end=end,
        production_end=production_end,
    ) is None
    assert qualify(
        [(_stamp(day, 12), 0.0), (_stamp(day, 19, 5), 6.0)],
        day_start=start,
        day_end=end,
        production_end=production_end,
    ) is None
    assert qualify(
        [(start, 0.0), (_stamp(day, 12), 6.0), (_stamp(day, 19, 5), 5.0)],
        day_start=start,
        day_end=end,
        production_end=production_end,
    ) is None
    assert qualify(
        [(start, 0.0), (_stamp(day, 12), float("nan")), (_stamp(day, 19, 5), 6.0)],
        day_start=start,
        day_end=end,
        production_end=production_end,
    ) is None
    assert qualify(
        [(start, 0.0), (_stamp(day, 19, 5), 6.0)],
        day_start=start,
        day_end=end,
        production_end=production_end,
    ) == 6.0
    assert qualify(
        [(start, 0.0), (_stamp(day, 19, 5), 0.0)],
        day_start=start,
        day_end=end,
        production_end=production_end,
    ) == 0.0

    for dst_day in (date(2026, 3, 29), date(2026, 10, 25)):
        dst_start = datetime.combine(dst_day, time.min, tzinfo=WARSAW)
        dst_end = datetime.combine(
            dst_day + timedelta(days=1), time.min, tzinfo=WARSAW
        )
        assert qualify(
            [(dst_start, 0.0), (_stamp(dst_day, 19), 8.0)],
            day_start=dst_start,
            day_end=dst_end,
            production_end=_stamp(dst_day, 18, 30),
        ) == 8.0
    print("PV forecast history quality: 8 scenarios passed")


if __name__ == "__main__":
    main()
