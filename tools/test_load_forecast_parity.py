"""End-to-end parity at the RCE/tariff expected-LOAD boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

import load_model  # noqa: E402
import rce_optimizer  # noqa: E402
import tariff_optimizer  # noqa: E402


def main() -> None:
    warsaw = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 9, 8, 14, 0, tzinfo=warsaw)
    starts = [now.astimezone(timezone.utc) + timedelta(minutes=30 * i) for i in range(68)]
    profile = tuple([0.75] * 12 + [0.0] * 16 + [0.15] * 20)
    shared = load_model.expected_load_by_slot(
        starts,
        now=now,
        daily_energy_kwh=12.0,
        average_profile_30m_kwh=profile,
        current_day_energy_kwh=9.0,
        current_day_observed_at=now,
        persistence_delta_kw=1.0,
        persistence_observed_at=now,
    )
    rce_settings = SimpleNamespace(
        now=now,
        average_daily_load_kwh=12.0,
        average_night_load_kwh=None,
        night_start_minute=22 * 60,
        night_end_minute=6 * 60,
        actual_day_load_today_kwh=9.0,
        actual_day_load_observed_at=now,
        persistence_delta_kw=1.0,
        persistence_observed_at=now,
        load_profile_30m_kwh=profile,
        weekday_load_profile_30m_kwh=(),
        weekend_load_profile_30m_kwh=(),
    )
    rce_load, _ = rce_optimizer._load_by_slot(
        rce_settings,
        starts,
        historical_day_energy=12.0,
        live_projected_day_energy=12.0,
        modeled_day_energy=12.0,
        daylight_progress=0.75,
    )
    tariff_settings = SimpleNamespace(
        load_by_slot_kwh=dict(shared.by_slot_kwh),
        average_daily_load_kwh=12.0,
        average_night_load_kwh=None,
        night_start_minute=22 * 60,
        night_end_minute=6 * 60,
    )
    tariff_load = tariff_optimizer._slot_loads(
        tariff_settings, [stamp.astimezone(warsaw) for stamp in starts]
    )
    assert all(abs(rce_load[stamp] - shared.by_slot_kwh[stamp]) < 1e-9 for stamp in starts)
    assert all(
        abs(value - shared.by_slot_kwh[stamp]) < 1e-9
        for stamp, value in zip(starts, tariff_load, strict=True)
    )
    tomorrow = now.date() + timedelta(days=1)
    assert abs(
        sum(value for stamp, value in shared.by_slot_kwh.items() if stamp.astimezone(warsaw).date() == tomorrow)
        - 12.0
    ) < 1e-6
    print("Shared LOAD forecast parity: RCE/tariff policy boundary passed")


if __name__ == "__main__":
    main()
