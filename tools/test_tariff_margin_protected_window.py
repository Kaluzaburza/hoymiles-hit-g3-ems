"""Regression for protected-period demand and useful consumable margin."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [
    str(ROOT / "tools"),
    str(ROOT / "custom_components" / "hoymiles_hit_modbus"),
]

from test_tariff_consumable_margin import _case  # noqa: E402
from tariff_optimizer import optimize_tariff_charging  # noqa: E402


ZONE = ZoneInfo("Europe/Warsaw")


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 22, hour, minute, tzinfo=ZONE)


def test_pre_window_pv_does_not_reduce_protected_demand() -> None:
    setup = replace(
        _case(protected_load_kwh=0.0, margin=10.0),
        now=at(15),
        schedule=replace(_case().schedule, operator="PGE"),
        charge_power_kw=6.0,
        system_ac_power_kw=10.0,
        battery_charge_power_kw=10.0,
        load_by_slot_kwh={at(18): 5.0, at(18, 30): 5.0},
        pv_by_slot_kwh={at(15): 2.0, at(15, 30): 2.0},
        current_load_power_kw=0.0,
        current_pv_power_kw=4.0,
        current_battery_power_kw=-4.0,
    )
    results = {
        margin: optimize_tariff_charging(
            replace(setup, demand_margin_percent=margin)
        )
        for margin in (0.0, 5.0, 10.0)
    }
    assert all(result.protected_demand_kwh == 10.0 for result in results.values())
    assert results[0.0].planned_stored_energy_kwh == 6.0
    assert abs(results[5.0].planned_stored_energy_kwh - 6.5) <= 1e-4
    assert abs(results[10.0].planned_stored_energy_kwh - 7.0) <= 1e-4
    assert results[10.0].demand_margin_requested_kwh == 1.0
    assert abs(results[10.0].demand_margin_feasible_kwh - 1.0) <= 1e-4
    assert results[10.0].demand_margin_unserved_kwh <= 1e-4
    assert results[10.0].next_charge_start == at(15, 30)
    assert results[10.0].latest_feasible_start is None

    after_half_hour = optimize_tariff_charging(
        replace(
            setup,
            now=at(15, 30),
            battery_soc_percent=8.5 / 26.0 * 100.0,
        )
    )
    assert after_half_hour.protected_demand_kwh == 10.0
    assert after_half_hour.demand_margin_requested_kwh == 1.0
    assert abs(after_half_hour.planned_stored_energy_kwh - 7.0) <= 1e-4
    assert after_half_hour.demand_margin_unserved_kwh <= 1e-4


def test_margin_buys_only_demand_usable_under_dcl() -> None:
    zero = optimize_tariff_charging(
        replace(
            _case(protected_load_kwh=10.0, margin=10.0),
            now=at(5),
            battery_discharge_power_kw=0.0,
            current_load_power_kw=0.0,
            current_pv_power_kw=0.0,
            current_battery_power_kw=0.0,
        )
    )
    assert zero.protected_demand_kwh == 0.0
    assert zero.demand_margin_requested_kwh == 0.0
    assert zero.planned_stored_energy_kwh == 0.0
    assert not zero.current_slot_planned
    assert abs(zero.optimized_grid_cost_pln - zero.baseline_grid_cost_pln) <= 1e-9

    limited = optimize_tariff_charging(
        replace(
            _case(protected_load_kwh=10.0, margin=10.0),
            now=at(5),
            battery_discharge_power_kw=0.1,
            current_load_power_kw=0.0,
            current_pv_power_kw=0.0,
            current_battery_power_kw=0.0,
        )
    )
    assert abs(limited.protected_demand_kwh - 0.6) <= 1e-9
    assert abs(limited.demand_margin_requested_kwh - 0.06) <= 1e-9
    assert limited.planned_stored_energy_kwh <= 0.661
    assert limited.optimized_grid_cost_pln < limited.baseline_grid_cost_pln

    usable = optimize_tariff_charging(
        replace(
            _case(protected_load_kwh=10.0, margin=10.0),
            now=at(5),
            battery_discharge_power_kw=2.0,
            current_load_power_kw=0.0,
            current_pv_power_kw=0.0,
            current_battery_power_kw=0.0,
        )
    )
    assert usable.protected_demand_kwh == 10.0
    assert usable.demand_margin_requested_kwh == 1.0
    assert abs(usable.planned_stored_energy_kwh - 11.0) <= 1e-3


def main() -> None:
    test_pre_window_pv_does_not_reduce_protected_demand()
    test_margin_buys_only_demand_usable_under_dcl()
    print("Tariff protected-period margin: PASS")


if __name__ == "__main__":
    main()
