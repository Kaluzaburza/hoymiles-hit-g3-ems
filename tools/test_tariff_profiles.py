"""Deterministic checks for official 2026 Polish tariff profiles."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from tariff_profiles import (  # noqa: E402
    PROFILE_DATA_VERSION,
    get_tariff_profile,
    profile_is_valid,
    profile_rate,
    profile_summary,
    configured_tariff_rate,
    is_polish_public_holiday,
)


ZONE = ZoneInfo("Europe/Warsaw")


def rate(operator: str, group: str, when: datetime) -> tuple[float, str]:
    profile = get_tariff_profile(operator, group)
    assert profile is not None
    return profile_rate(when, profile, is_public_holiday=False)


def main() -> None:
    # G12e is monthly, not the two-season G12 schedule. Check every boundary
    # on working days, including the month and DST changes in the price feed.
    g12e = get_tariff_profile("PGE", "G12e")
    assert g12e is not None
    for month, (begin, end) in enumerate(
        [(13, 15), (13, 15), (11, 15), (10, 17), (9, 17), (9, 17),
         (9, 17), (9, 17), (10, 17), (11, 15), (13, 15), (13, 15)], 1
    ):
        day = datetime(2026, month, 7, tzinfo=ZONE)
        while day.weekday() >= 5 or is_polish_public_holiday(day.date()):
            day += timedelta(days=1)
        for hour, minute, expected in (
            (5, 59, "low"), (6, 0, "peak"), (begin - 1, 59, "peak"),
            (begin, 0, "low"), (end - 1, 59, "low"), (end, 0, "peak"),
            (21, 59, "peak"), (22, 0, "low"),
        ):
            assert rate("PGE", "G12e", day.replace(hour=hour, minute=minute))[1] == expected
    for day in (datetime(2026, 10, 3, 8, tzinfo=ZONE),
                datetime(2026, 11, 11, 8, tzinfo=ZONE)):
        assert profile_rate(day, g12e, is_public_holiday=is_polish_public_holiday(day.date()))[1] == "low"
    for fold in (0, 1):
        assert rate("PGE", "G12e", datetime(2026, 10, 25, 2, 30, tzinfo=ZONE, fold=fold))[1] == "low"
    assert rate("PGE", "G12e", datetime(2026, 3, 31, 10, 30, tzinfo=ZONE))[1] == "peak"
    assert rate("PGE", "G12e", datetime(2026, 4, 1, 10, 30, tzinfo=ZONE))[1] == "low"
    assert g12e.low_price_pln_kwh == round(0.5005 + (0.0349 + 0.0332 + 0.0073 + 0.0030) * 1.23, 4)
    assert g12e.peak_price_pln_kwh == round(0.8363 + (0.3851 + 0.0332 + 0.0073 + 0.0030) * 1.23, 4)
    assert not profile_is_valid(g12e, datetime(2026, 1, 31).date())
    assert profile_is_valid(g12e, datetime(2026, 2, 1).date())
    assert not profile_is_valid(g12e, datetime(2027, 1, 1).date())
    for operator in ("TAURON", "ENEA", "ENERGA", "STOEN"):
        assert get_tariff_profile(operator, "G12e") is None
        try:
            configured_tariff_rate(datetime(2026, 10, 2, 12, tzinfo=ZONE),
                operator=operator, tariff_type="G12e", g11_price_pln_kwh=1,
                low_price_pln_kwh=0.1, medium_price_pln_kwh=1, peak_price_pln_kwh=1,
                cheap_windows=((0, 1439),))
        except ValueError:
            pass
        else:
            raise AssertionError("Unsupported G12e must not silently use manual prices")

    # PGE changes the afternoon low window between summer and winter.
    assert rate("PGE", "G12", datetime(2026, 7, 1, 16, tzinfo=ZONE))[1] == "low"
    assert rate("PGE", "G12", datetime(2026, 1, 2, 16, tzinfo=ZONE))[1] == "peak"
    assert rate("PGE", "G12", datetime(2026, 1, 2, 14, tzinfo=ZONE))[1] == "low"

    # TAURON G13 uses seasonal afternoon peaks and a cheaper remainder.
    assert rate("TAURON", "G13", datetime(2026, 7, 1, 8, tzinfo=ZONE))[1] == "medium"
    assert rate("TAURON", "G13", datetime(2026, 7, 1, 14, tzinfo=ZONE))[1] == "low"
    assert rate("TAURON", "G13", datetime(2026, 7, 1, 20, tzinfo=ZONE))[1] == "peak"
    assert rate("TAURON", "G13", datetime(2026, 1, 2, 17, tzinfo=ZONE))[1] == "peak"

    # Operator-specific G12w schedules must not be replaced by one generic rule.
    assert rate("ENEA", "G12w", datetime(2026, 3, 2, 20, 30, tzinfo=ZONE))[1] == "peak"
    assert rate("ENEA", "G12w", datetime(2026, 3, 2, 21, 0, tzinfo=ZONE))[1] == "low"
    assert rate("STOEN", "G12w", datetime(2026, 3, 2, 13, 30, tzinfo=ZONE))[1] == "peak"
    assert rate("ENERGA", "G12w", datetime(2026, 3, 2, 13, 30, tzinfo=ZONE))[1] == "low"

    # Weekends and statutory holidays use the low period where specified.
    tauron = get_tariff_profile("TAURON", "G12w")
    assert tauron is not None
    saturday = datetime(2026, 8, 8, 12, tzinfo=ZONE)
    assert profile_rate(saturday, tauron, is_public_holiday=False)[1] == "low"
    weekday_holiday = datetime(2026, 8, 15, 12, tzinfo=ZONE)
    assert profile_rate(weekday_holiday, tauron, is_public_holiday=True)[1] == "low"

    # G13 is intentionally supported only where an official profile is known.
    assert get_tariff_profile("TAURON", "G13") is not None
    assert get_tariff_profile("PGE", "G13") is None

    # Versioned tables remain valid through the complete declared year and
    # fail closed immediately outside it; annual maintenance cannot be missed
    # silently by a future Home Assistant update.
    pge = get_tariff_profile("PGE", "G12")
    assert pge is not None
    assert profile_is_valid(pge, datetime(2026, 1, 1).date())
    assert profile_is_valid(pge, datetime(2026, 12, 31).date())
    assert not profile_is_valid(pge, datetime(2025, 12, 31).date())
    assert not profile_is_valid(pge, datetime(2027, 1, 1).date())
    summary = profile_summary(
        pge,
        datetime(2026, 12, 31, 23, 30, tzinfo=ZONE),
        is_public_holiday=False,
    )
    assert summary["tariff_profile_data_version"] == PROFILE_DATA_VERSION
    assert summary["tariff_profile_valid_now"] is True

    print("Tariff profiles: official 2026 schedules and prices passed")


if __name__ == "__main__":
    main()
