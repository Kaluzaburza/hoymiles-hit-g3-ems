"""Causal contracts for the consumable tariff energy-demand margin."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from tariff_optimizer import (  # noqa: E402
    ExportState,
    TariffOptimizerInput,
    TariffSchedule,
    optimize_tariff_charging,
)


ZONE = ZoneInfo("Europe/Warsaw")


def _schedule() -> TariffSchedule:
    return TariffSchedule(
        tariff_type="G12",
        g11_price_pln_kwh=0.90,
        low_price_pln_kwh=0.50,
        medium_price_pln_kwh=0.80,
        peak_price_pln_kwh=1.10,
        cheap_windows=((22 * 60, 6 * 60), (12 * 60, 13 * 60)),
    )


def _case(
    *,
    maximum_soc: float = 100.0,
    margin: float = 10.0,
    capacity: float = 26.0,
    battery_soc: float = 25.0,
    protected_load_kwh: float = 18.0,
    protected_pv_kwh: float = 0.0,
    charge_power_kw: float = 20.0,
    discharge_efficiency: float = 100.0,
) -> TariffOptimizerInput:
    now = datetime(2026, 9, 21, 22, 0, tzinfo=ZONE)
    # The protected 06:00-12:00 period needs exactly 18 kWh on the battery
    # axis when discharge efficiency is 100%. The battery starts on its 25%
    # physical reserve, so existing stock above reserve is zero.
    load = {
        now + timedelta(minutes=30 * index): protected_load_kwh / 12.0
        for index in range(16, 28)
    }
    pv = {
        now + timedelta(minutes=30 * index): protected_pv_kwh / 12.0
        for index in range(16, 28)
        if protected_pv_kwh > 0.0
    }
    return TariffOptimizerInput(
        now=now,
        pv_by_slot_kwh=pv,
        battery_capacity_kwh=capacity,
        battery_soc_percent=battery_soc,
        reserve_soc_percent=25.0,
        base_reserve_soc_percent=25.0,
        maximum_soc_percent=maximum_soc,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
        night_start_minute=22 * 60,
        night_end_minute=6 * 60,
        charge_power_kw=charge_power_kw,
        system_ac_power_kw=20.0,
        battery_charge_power_kw=charge_power_kw,
        charge_efficiency_percent=100.0,
        discharge_efficiency_percent=discharge_efficiency,
        minimum_saving_pln_kwh=0.0,
        schedule=_schedule(),
        load_by_slot_kwh=load,
        terminal_reserve_soc_percent=25.0,
        demand_margin_percent=margin,
        export_state=ExportState.VERIFIED_ALLOWED,
    )


def main() -> None:
    full = optimize_tariff_charging(_case())
    assert full.base_reserve_soc_percent == 25.0
    assert full.protected_demand_kwh == 18.0
    assert full.demand_margin_requested_kwh == 1.8
    assert full.demand_margin_feasible_kwh == 1.5
    assert abs(full.demand_margin_unserved_kwh - 0.3) <= 1e-9
    assert full.requested_target_energy_kwh == 26.3
    assert full.feasible_target_energy_kwh == 26.0
    assert full.target_soc_percent <= 100.0
    assert full.protected_period_start == datetime(2026, 9, 22, 6, 0, tzinfo=ZONE)
    assert full.protected_period_end == datetime(2026, 9, 22, 12, 0, tzinfo=ZONE)

    capped = optimize_tariff_charging(_case(maximum_soc=90.0))
    assert capped.protected_demand_kwh == 18.0
    assert capped.base_energy_shortfall_kwh >= 1.1 - 1e-6
    assert capped.demand_margin_feasible_kwh == 0.0
    assert capped.demand_margin_unserved_kwh == 1.8
    assert capped.feasible_target_energy_kwh == 23.4
    assert capped.target_soc_percent <= 90.0

    # The setting is a percentage of D, not battery capacity. Its base is
    # stable while SOC rises during equivalent replans.
    for margin, expected in ((0.0, 0.0), (5.0, 0.5), (10.0, 1.0)):
        result = optimize_tariff_charging(
            _case(margin=margin, protected_load_kwh=10.0)
        )
        assert abs(result.protected_demand_kwh - 10.0) <= 1e-9
        assert abs(result.demand_margin_requested_kwh - expected) <= 1e-9
        assert result.base_reserve_soc_percent == 25.0
    low_soc = optimize_tariff_charging(
        _case(protected_load_kwh=10.0, battery_soc=25.0)
    )
    higher_soc = optimize_tariff_charging(
        _case(protected_load_kwh=10.0, battery_soc=40.0)
    )
    assert low_soc.protected_demand_kwh == higher_soc.protected_demand_kwh
    assert low_soc.requested_target_energy_kwh == higher_soc.requested_target_energy_kwh
    assert higher_soc.planned_stored_energy_kwh < low_soc.planned_stored_energy_kwh

    # D and B use the battery axis: useful home energy is divided by discharge
    # efficiency exactly once, while PV within the protected period reduces D.
    lossy = optimize_tariff_charging(
        _case(
            protected_load_kwh=8.0,
            discharge_efficiency=80.0,
            margin=10.0,
        )
    )
    assert abs(lossy.protected_demand_kwh - 10.0) <= 1e-9
    assert abs(lossy.demand_margin_requested_kwh - 1.0) <= 1e-9
    pv_reduced = optimize_tariff_charging(
        _case(protected_load_kwh=10.0, protected_pv_kwh=2.0, margin=10.0)
    )
    assert abs(pv_reduced.protected_demand_kwh - 8.0) <= 1e-9
    assert abs(pv_reduced.demand_margin_requested_kwh - 0.8) <= 1e-9

    different_capacity = optimize_tariff_charging(
        _case(capacity=20.0, protected_load_kwh=10.0, margin=10.0)
    )
    assert different_capacity.requested_target_energy_kwh == 16.0
    power_limited = optimize_tariff_charging(
        _case(
            protected_load_kwh=10.0,
            margin=10.0,
            charge_power_kw=1.0,
        )
    )
    assert power_limited.base_energy_shortfall_kwh > 0.0
    assert power_limited.demand_margin_feasible_kwh == 0.0
    assert power_limited.demand_margin_constraint_reason == (
        "base_energy_capacity_time_or_power"
    )

    zero = optimize_tariff_charging(replace(_case(margin=10.0), load_by_slot_kwh={}))
    assert zero.protected_demand_kwh == 0.0
    assert zero.demand_margin_requested_kwh == 0.0
    assert zero.demand_margin_feasible_kwh == 0.0
    assert not zero.planned_charges

    # The same protected period remains stable as time advances through the
    # cheap window; equal-cost charging is packed at its latest feasible end.
    starts = [item.start for item in full.planned_charges if item.stored_energy_kwh > 0]
    assert starts
    assert max(starts) == datetime(2026, 9, 22, 5, 30, tzinfo=ZONE)
    assert min(starts) >= datetime(2026, 9, 22, 4, 30, tzinfo=ZONE)

    for bad_capacity in (0.0, float("nan")):
        try:
            optimize_tariff_charging(replace(_case(), battery_capacity_kwh=bad_capacity))
        except ValueError:
            pass
        else:
            raise AssertionError("invalid battery capacity was treated as usable")

    print("Tariff consumable margin: 9 causal groups passed")


if __name__ == "__main__":
    main()
