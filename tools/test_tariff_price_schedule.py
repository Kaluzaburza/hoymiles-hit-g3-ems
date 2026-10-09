"""Deterministic R07 regression for the policy-neutral tariff price schedule."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import math
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))

from tariff_price_schedule import (  # noqa: E402
    TariffPriceConfig,
    TariffPriceScheduleCache,
    build_tariff_price_schedule,
    estimate_uniform_energy_cost,
    normalize_import_price,
    tariff_source_revision,
)
from tariff_profiles import MANUAL_OPERATOR, get_tariff_profile  # noqa: E402


UTC = timezone.utc
WARSAW = ZoneInfo("Europe/Warsaw")


def official(operator: str, tariff_type: str) -> TariffPriceConfig:
    profile = get_tariff_profile(operator, tariff_type)
    assert profile is not None
    return TariffPriceConfig(
        tariff_type=tariff_type,
        g11_price_pln_kwh=profile.g11_price_pln_kwh,
        low_price_pln_kwh=profile.low_price_pln_kwh,
        medium_price_pln_kwh=profile.medium_price_pln_kwh,
        peak_price_pln_kwh=profile.peak_price_pln_kwh,
        cheap_windows=((13 * 60, 15 * 60), (22 * 60, 6 * 60)),
        medium_windows=((7 * 60, 13 * 60),),
        weekend_low_price=profile.weekend_low_price,
        polish_holidays_low_price=profile.polish_holidays_low_price,
        operator=operator,
    )


def manual(**changes: object) -> TariffPriceConfig:
    values: dict[str, object] = {
        "tariff_type": "G12",
        "g11_price_pln_kwh": 0.9,
        "low_price_pln_kwh": 0.5,
        "medium_price_pln_kwh": 0.8,
        "peak_price_pln_kwh": 1.2,
        "cheap_windows": ((13 * 60, 15 * 60), (22 * 60, 6 * 60)),
        "medium_windows": ((7 * 60, 13 * 60),),
        "weekend_low_price": True,
        "polish_holidays_low_price": True,
        "operator": MANUAL_OPERATOR,
    }
    values.update(changes)
    return TariffPriceConfig(**values)  # type: ignore[arg-type]


def span(schedule: TariffPriceConfig, start: datetime, end: datetime):
    return build_tariff_price_schedule(
        schedule,
        start=start,
        end=end,
        local_zone=WARSAW,
        generated_at=start,
    )


def test_units_values_and_g11() -> None:
    assert normalize_import_price(1200, "PLN/MWh") == 1.2
    assert normalize_import_price(1.2, "PLN/kWh") == 1.2
    for invalid in (None, float("nan"), float("inf")):
        try:
            normalize_import_price(invalid, "PLN/kWh")
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid price accepted: {invalid}")

    start = datetime(2026, 7, 1, tzinfo=WARSAW)
    result = span(official("TAURON", "G11"), start, start + timedelta(days=1))
    assert result.quality == "official_verified"
    assert result.coverage_complete
    assert len(result.intervals) == 1
    assert result.intervals[0].price_pln_kwh_ac == 0.9741
    assert result.as_attributes()["energy_side"] == "ac_import"
    assert "fixed_fees" in result.price_basis


def test_boundary_partial_slot_and_honest_cost_bounds() -> None:
    start = datetime(2026, 7, 1, 12, 7, tzinfo=WARSAW)
    end = datetime(2026, 7, 1, 13, 7, tzinfo=WARSAW)
    result = span(official("TAURON", "G12"), start, end)
    assert result.intervals[0].start_utc == start.astimezone(UTC)
    assert result.intervals[0].end_utc == datetime(
        2026, 7, 1, 13, 0, tzinfo=WARSAW
    ).astimezone(UTC)
    assert [item.zone for item in result.intervals] == ["peak", "low"]
    cost = estimate_uniform_energy_cost(
        result,
        start=start,
        end=end,
        energy_kwh_ac=1.0,
    )
    expected = (53.0 / 60.0) * 1.0769 + (7.0 / 60.0) * 0.6362
    assert math.isclose(cost.estimated_cost_pln, expected, abs_tol=1e-12)
    assert cost.minimum_cost_pln == 0.6362
    assert cost.maximum_cost_pln == 1.0769
    assert not cost.exact
    assert cost.allocation_basis == "uniform_power_estimate_with_price_bounds"


def test_weekend_holiday_and_dst() -> None:
    schedule = official("TAURON", "G12w")
    saturday = datetime(2026, 8, 8, tzinfo=WARSAW)
    weekend = span(schedule, saturday, saturday + timedelta(days=1))
    assert len(weekend.intervals) == 1 and weekend.intervals[0].zone == "low"
    holiday = datetime(2026, 11, 11, tzinfo=WARSAW)
    holiday_result = span(schedule, holiday, holiday + timedelta(days=1))
    assert len(holiday_result.intervals) == 1
    assert holiday_result.intervals[0].zone == "low"

    spring_start = datetime(2026, 3, 29, tzinfo=WARSAW)
    spring_end = datetime(2026, 3, 30, tzinfo=WARSAW)
    spring = span(schedule, spring_start, spring_end)
    assert (spring.coverage_end_utc - spring.coverage_start_utc) == timedelta(hours=23)
    autumn_start = datetime(2026, 10, 25, tzinfo=WARSAW)
    autumn_end = datetime(2026, 10, 26, tzinfo=WARSAW)
    autumn = span(schedule, autumn_start, autumn_end)
    assert (autumn.coverage_end_utc - autumn.coverage_start_utc) == timedelta(hours=25)


def test_missing_tomorrow_and_cache() -> None:
    start = datetime(2026, 12, 31, 22, tzinfo=WARSAW)
    end = datetime(2027, 1, 1, 2, tzinfo=WARSAW)
    partial = span(official("TAURON", "G12w"), start, end)
    assert partial.quality == "partial"
    assert not partial.coverage_complete
    assert partial.coverage_end_utc == datetime(2027, 1, 1, tzinfo=WARSAW).astimezone(UTC)
    assert partial.missing_reasons

    valid_start = datetime(2026, 7, 1, tzinfo=WARSAW)
    valid = span(official("TAURON", "G12w"), valid_start, valid_start + timedelta(days=2))
    unavailable = span(
        manual(low_price_pln_kwh=None),
        valid_start,
        valid_start + timedelta(days=2),
    )
    cache = TariffPriceScheduleCache()
    assert cache.select(valid, now=valid_start) is valid
    cached = cache.select(unavailable, now=valid_start + timedelta(hours=1))
    assert cached.served_from_cache and cached.quality == "cached_verified"
    expired = cache.select(unavailable, now=valid_start + timedelta(days=3))
    assert not expired.available


def test_semantic_revision_and_policy_independence() -> None:
    schedule = manual()
    revision = tariff_source_revision(schedule)
    assert revision == tariff_source_revision(schedule)
    assert revision != tariff_source_revision(replace(schedule, peak_price_pln_kwh=1.21))
    assert revision != tariff_source_revision(
        replace(schedule, cheap_windows=((12 * 60, 14 * 60),))
    )

    sensor_source = (COMPONENT / "tariff_price_sensor.py").read_text(encoding="utf-8")
    config_block = sensor_source.split("PRICE_CONFIG_ENTITIES", 1)[1].split(")\n\n\n", 1)[0]
    assert "hoymiles_tariff_charge_enabled" not in config_block
    assert "result_current" not in sensor_source
    assert "recalculation_pending" not in sensor_source


def test_zero_negative_nan_and_missing_are_distinct() -> None:
    start = datetime(2026, 7, 1, 13, tzinfo=WARSAW)
    end = start + timedelta(hours=1)
    zero = span(manual(low_price_pln_kwh=0.0), start, end)
    assert zero.available and zero.price_at(start) == 0.0
    negative = span(manual(low_price_pln_kwh=-0.1), start, end)
    assert negative.available and negative.price_at(start) == -0.1
    missing = span(manual(low_price_pln_kwh=None), start, end)
    assert not missing.available
    assert "low_price_pln_kwh:missing" in missing.missing_reasons
    non_finite = span(manual(low_price_pln_kwh=float("nan")), start, end)
    assert not non_finite.available
    assert "low_price_pln_kwh:non_finite" in non_finite.missing_reasons
    missing_window = span(
        manual(cheap_windows=((None, 15 * 60),)),  # type: ignore[arg-type]
        start,
        end,
    )
    assert not missing_window.available
    assert "price_window_0:invalid" in missing_window.missing_reasons


def test_pge_g12e_price_feed() -> None:
    schedule = official("PGE", "G12e")
    start = datetime(2026, 10, 2, 10, 59, tzinfo=WARSAW)
    result = span(schedule, start, start.replace(hour=15, minute=1))
    assert result.quality == "official_verified" and result.coverage_complete
    assert [item.zone for item in result.intervals] == ["peak", "low", "peak"]
    assert [item.price_pln_kwh_ac for item in result.intervals] == [1.3635, 0.5969, 1.3635]
    assert result.price_at(start.replace(hour=11, minute=0)) == 0.5969
    assert result.price_at(start.replace(hour=15, minute=0)) == 1.3635
    # The rolling feed crosses a month boundary with different daytime zones.
    march = datetime(2026, 3, 31, 10, 30, tzinfo=WARSAW)
    rolling = span(schedule, march, march + timedelta(days=2))
    assert rolling.price_at(march) == 1.3635
    assert rolling.price_at(march + timedelta(days=1)) == 0.5969
    for day, hours in ((datetime(2026, 3, 29, tzinfo=WARSAW), 23),
                       (datetime(2026, 10, 25, tzinfo=WARSAW), 25),
                       (datetime(2026, 11, 11, tzinfo=WARSAW), 24)):
        feed = span(schedule, day, day + timedelta(days=1))
        assert feed.coverage_complete and len(feed.intervals) == 1
        assert feed.intervals[0].zone == "low"
        assert feed.coverage_end_utc - feed.coverage_start_utc == timedelta(hours=hours)
    # Unverified prices never fall back to the unrelated manual helper values.
    for day in (datetime(2026, 1, 15, tzinfo=WARSAW), datetime(2027, 1, 1, tzinfo=WARSAW)):
        feed = span(schedule, day, day + timedelta(days=1))
        assert not feed.available and not feed.coverage_complete
    unsupported = span(replace(schedule, operator="TAURON"), start, start + timedelta(hours=1))
    assert not unsupported.available


def main() -> None:
    tests = (
        test_units_values_and_g11,
        test_boundary_partial_slot_and_honest_cost_bounds,
        test_weekend_holiday_and_dst,
        test_missing_tomorrow_and_cache,
        test_semantic_revision_and_policy_independence,
        test_zero_negative_nan_and_missing_are_distinct,
        test_pge_g12e_price_feed,
    )
    for test in tests:
        test()
    print(f"Tariff price schedule R07: {len(tests)} scenario groups PASS")


if __name__ == "__main__":
    main()
