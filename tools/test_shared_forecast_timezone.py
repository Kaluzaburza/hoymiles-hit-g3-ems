"""A05 broker-level tests for HA-local calendar forecast evaluation."""

from __future__ import annotations

import asyncio  # Load stdlib select before the integration's select.py is visible.
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import test_ems_shared_inputs as fixtures


def _rows(day: date, zone: ZoneInfo) -> list[dict[str, object]]:
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(timezone.utc)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(
        timezone.utc
    )
    slots = int((end - start).total_seconds() / 1800)
    return [
        {
            "period_start": (start + timedelta(minutes=30 * index)).astimezone(zone),
            "pv_estimate": 1.0,
        }
        for index in range(slots)
    ]


def _state(value: float, reported: datetime, day: date, zone: ZoneInfo):
    return SimpleNamespace(
        state=str(value),
        last_reported=reported,
        last_updated=reported,
        attributes={"dataCorrect": True, "detailedForecast": _rows(day, zone)},
    )


def main() -> None:
    for zone_name, local_now in (
        ("Europe/Warsaw", datetime(2026, 9, 20, 0, 30)),
        ("America/New_York", datetime(2026, 9, 20, 23, 30)),
    ):
        zone = ZoneInfo(zone_name)
        local = local_now.replace(tzinfo=zone)
        hass, broker = fixtures._coordinator()
        hass.config = SimpleNamespace(time_zone=zone_name)
        hass.states.values["sensor.shared_today"] = _state(
            30.0,
            local - timedelta(hours=8),
            local.date(),
            zone,
        )
        sample = broker._forecast_sample("today", local.astimezone(timezone.utc))
        assert sample.fresh, (zone_name, sample.reason, sample.usefulness)
        assert sample.usefulness["target_date"] == local.date().isoformat()

    # The normal overnight Solcast pause remains usable at 06:30 local even
    # when the last successful update is 19 hours old and the next is at 07:00.
    zone = ZoneInfo("Europe/Warsaw")
    local = datetime(2026, 9, 20, 6, 30, tzinfo=zone)
    hass, broker = fixtures._coordinator()
    hass.config = SimpleNamespace(time_zone="Europe/Warsaw")
    reported = local - timedelta(hours=19)
    hass.states.values["sensor.shared_today"] = _state(
        30.0, reported, local.date(), zone
    )
    hass.states.values[fixtures.M.SOLCAST_UPDATE_ENTITY_CANDIDATES[0]] = (
        SimpleNamespace(
            state=reported.isoformat(),
            last_reported=reported,
            last_updated=reported,
            attributes={
                "next_auto_update": (local + timedelta(minutes=30)).isoformat()
            },
        )
    )
    sample = broker._forecast_sample("today", local.astimezone(timezone.utc))
    assert sample.fresh, (sample.reason, sample.usefulness)
    assert sample.usefulness["mode"] == "scheduled_pause"

    for dst_day in (date(2026, 3, 29), date(2026, 10, 25)):
        local = datetime.combine(dst_day, time(12), tzinfo=zone)
        hass, broker = fixtures._coordinator()
        hass.config = SimpleNamespace(time_zone="Europe/Warsaw")
        hass.states.values["sensor.shared_today"] = _state(
            30.0, local - timedelta(hours=1), dst_day, zone
        )
        sample = broker._forecast_sample("today", local.astimezone(timezone.utc))
        assert sample.fresh, (dst_day, sample.reason, sample.usefulness)
        assert sample.usefulness["coverage_complete"] is True
    print("Shared forecast timezone: 5 local-calendar scenarios passed")


if __name__ == "__main__":
    main()
