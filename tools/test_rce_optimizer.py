"""Deterministic safety and profitability tests for the RCE optimizer."""

from __future__ import annotations

import asyncio
import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
from itertools import product
import math
from pathlib import Path
import random
import sys
import time as monotonic_time
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))
MODULE_PATH = (
    ROOT
    / "custom_components"
    / "hoymiles_hit_modbus"
    / "rce_optimizer.py"
)
SPEC = importlib.util.spec_from_file_location("hoymiles_rce_optimizer", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load the RCE optimizer")
RCE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RCE
SPEC.loader.exec_module(RCE)

FORECAST_PATH = (
    ROOT
    / "custom_components"
    / "hoymiles_hit_modbus"
    / "forecast_model.py"
)
FORECAST_SPEC = importlib.util.spec_from_file_location(
    "hoymiles_forecast_model",
    FORECAST_PATH,
)
if FORECAST_SPEC is None or FORECAST_SPEC.loader is None:
    raise RuntimeError("Cannot load the shared forecast model")
FORECAST = importlib.util.module_from_spec(FORECAST_SPEC)
sys.modules[FORECAST_SPEC.name] = FORECAST
FORECAST_SPEC.loader.exec_module(FORECAST)

ENERGY_DATA_PATH = (
    ROOT
    / "custom_components"
    / "hoymiles_hit_modbus"
    / "energy_data.py"
)
ENERGY_DATA_SPEC = importlib.util.spec_from_file_location(
    "hoymiles_energy_data",
    ENERGY_DATA_PATH,
)
if ENERGY_DATA_SPEC is None or ENERGY_DATA_SPEC.loader is None:
    raise RuntimeError("Cannot load the shared energy-data validator")
ENERGY_DATA = importlib.util.module_from_spec(ENERGY_DATA_SPEC)
sys.modules[ENERGY_DATA_SPEC.name] = ENERGY_DATA
ENERGY_DATA_SPEC.loader.exec_module(ENERGY_DATA)

WARSAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 7, 28, 0, 0, tzinfo=WARSAW)
# Wall-clock timing on shared CI runners is noisy. Keep one conservative
# ceiling for the heavy 96/110-slot regressions; event-loop safety is proved by
# the separate executor/offload contract rather than by this benchmark.
SHARED_RUNNER_SOLVER_CEILING_SECONDS = 1.0
V156_RCE_OPTIMIZER_SHA256 = (
    "cf78c4a373be1c643f29e8933b873ba4fd2f787428e5845b6b555a9f4c0c40f8"
)


def test_v156_rce_optimizer_source_is_frozen() -> None:
    """Freeze the reviewed AP-1 RCE implementation including its sidecar."""

    assert hashlib.sha256(MODULE_PATH.read_bytes()).hexdigest() == (
        V156_RCE_OPTIMIZER_SHA256
    )


def slots(day_offset: int, hour: int, count: int, price: float):
    """Create consecutive half-hour price slots."""
    start = NOW.replace(hour=hour) + timedelta(days=day_offset)
    return [
        RCE.PriceSlot(start=start + timedelta(minutes=30 * index), price_pln_kwh=price)
        for index in range(count)
    ]


def base_input(**changes):
    """Return a complete deterministic optimizer input."""
    settings = RCE.OptimizerInput(
        now=NOW,
        price_slots=[],
        pv_by_slot_kwh={},
        battery_capacity_kwh=20.0,
        battery_soc_percent=100.0,
        outage_reserve_soc_percent=20.0,
        safety_margin_soc_percent=0.0,
        manual_minimum_soc_percent=20.0,
        dynamic_reserve_enabled=True,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
        night_start_minute=20 * 60,
        night_end_minute=8 * 60,
        inverter_power_kw=10.0,
        inverter_count=1,
        discharge_power_percent=100.0,
        export_efficiency_percent=100.0,
        bms_max_discharge_current_a=500.0,
        bms_max_charge_current_a=500.0,
        battery_voltage_v=50.0,
        bms_power_safety_percent=100.0,
        bms_discharge_data_fresh=True,
        bms_discharge_data_age_seconds=0.0,
        bms_discharge_data_available=True,
        bms_charge_data_fresh=True,
        bms_charge_data_age_seconds=0.0,
        bms_charge_data_available=True,
        charge_efficiency_percent=100.0,
        house_discharge_efficiency_percent=100.0,
    )
    return replace(settings, **changes)


def _independent_short_simulation(
    *,
    starts: list[datetime],
    settings,
    load_by_slot: dict[datetime, float],
    pv_by_slot: dict[datetime, float],
    exports: dict[datetime, float],
    floor_kwh: float,
    reserve_by_slot: dict[datetime, float],
    prices: dict[datetime, float],
) -> tuple[bool, float]:
    """Simulate a tiny oracle case without optimizer helper functions."""
    capacity = settings.battery_capacity_kwh
    battery = capacity * settings.battery_soc_percent / 100.0
    export_efficiency = settings.export_efficiency_percent / 100.0
    charge_efficiency = settings.charge_efficiency_percent / 100.0
    house_efficiency = settings.house_discharge_efficiency_percent / 100.0
    system_power = settings.inverter_power_kw * settings.inverter_count
    requested_power = (
        system_power * settings.discharge_power_percent / 100.0
    )
    revenue = 0.0
    exported_dc = 0.0
    for start in starts:
        pv = max(pv_by_slot.get(start, 0.0), 0.0)
        load = max(load_by_slot.get(start, 0.0), 0.0)
        export = max(exports.get(start, 0.0), 0.0)
        load_deficit = max(load - pv, 0.0)
        system_energy = system_power * 0.5
        export_caps = [
            max(requested_power * 0.5 - load_deficit, 0.0),
            max(system_energy - min(load, system_energy), 0.0),
        ]
        if settings.bms_discharge_data_fresh:
            discharge_dc = (
                max(settings.bms_max_discharge_current_a or 0.0, 0.0)
                * max(settings.battery_voltage_v or 0.0, 0.0)
                / 1000.0
                * settings.bms_power_safety_percent
                / 100.0
            )
            export_caps.append(
                max(discharge_dc * 0.5 - load_deficit / house_efficiency, 0.0)
                * export_efficiency
            )
        if settings.export_power_cap_kw is not None:
            export_caps.append(max(settings.export_power_cap_kw, 0.0) * 0.5)
        if settings.effective_export_power_kw is not None:
            export_caps.append(
                max(settings.effective_export_power_kw, 0.0) * 0.5
            )
        export_cap = min(export_caps)
        if export > export_cap + 1e-7:
            return False, -float("inf")
        if pv < load:
            battery -= (load - pv) / house_efficiency
        battery -= export / export_efficiency
        charge_input = 0.0
        if pv >= load:
            charge_dc_power = (
                max(settings.bms_max_charge_current_a or 0.0, 0.0)
                * max(settings.battery_voltage_v or 0.0, 0.0)
                / 1000.0
                * settings.bms_power_safety_percent
                / 100.0
                if settings.bms_charge_data_fresh
                and settings.bms_charge_data_available
                else 0.0
            )
            charge_input = min(
                max(pv - load, 0.0),
                max(system_energy - min(load, system_energy) - export, 0.0),
                charge_dc_power * 0.5 / charge_efficiency,
                max(capacity - battery, 0.0) / charge_efficiency,
            )
            battery += charge_input * charge_efficiency
        if battery < floor_kwh - 1e-7:
            return False, -float("inf")
        if export > 1e-9 and battery < reserve_by_slot[start] - 1e-7:
            return False, -float("inf")
        unallocated_pv = max(pv - load - charge_input, 0.0)
        remaining_system = max(
            system_energy
            - min(load, system_energy)
            - export
            - charge_input,
            0.0,
        )
        grid_caps = [
            max(cap, 0.0)
            for cap in (
                settings.export_power_cap_kw,
                settings.effective_export_power_kw,
            )
            if cap is not None
        ]
        remaining_grid = (
            max(min(grid_caps) * 0.5 - export, 0.0)
            if grid_caps
            else remaining_system
        )
        natural_export = min(unallocated_pv, remaining_system, remaining_grid)
        battery = min(battery, capacity)
        revenue += (export + natural_export) * prices.get(start, 0.0)
        exported_dc += export / export_efficiency
    objective = revenue - (
        exported_dc * settings.battery_wear_cost_pln_kwh
    )
    return True, objective


def _independent_grid_oracle(
    *,
    starts: list[datetime],
    settings,
    load_by_slot: dict[datetime, float],
    conservative_pv: dict[datetime, float],
    expected_pv: dict[datetime, float],
    floor_kwh: float,
    reserve_by_slot: dict[datetime, float],
    prices: dict[datetime, float],
) -> tuple[float, dict[datetime, float]]:
    """Enumerate an independent 0/0.5/1 kWh feasible lower bound."""
    best_value = -float("inf")
    best_plan: dict[datetime, float] = {}
    for actions in product((0.0, 0.5, 1.0), repeat=len(starts)):
        plan = {
            start: action
            for start, action in zip(starts, actions, strict=True)
            if action > 0.0
        }
        safe, _ = _independent_short_simulation(
            starts=starts,
            settings=settings,
            load_by_slot=load_by_slot,
            pv_by_slot=conservative_pv,
            exports=plan,
            floor_kwh=floor_kwh,
            reserve_by_slot=reserve_by_slot,
            prices=prices,
        )
        if not safe:
            continue
        expected_safe, value = _independent_short_simulation(
            starts=starts,
            settings=settings,
            load_by_slot=load_by_slot,
            pv_by_slot=expected_pv,
            exports=plan,
            floor_kwh=floor_kwh,
            reserve_by_slot=reserve_by_slot,
            prices=prices,
        )
        if expected_safe and value > best_value:
            best_value = value
            best_plan = plan
    return best_value, best_plan


def test_higher_tomorrow_price_wins() -> None:
    """Energy must be held for tomorrow when its price is higher."""
    today = slots(0, 18, 4, 0.70)
    tomorrow = slots(1, 6, 4, 0.90)
    result = RCE.optimize_rce(
        base_input(price_slots=[*today, *tomorrow])
    )
    assert result.ready
    assert result.planned_export_kwh > 15.9
    assert {
        item.start.date() for item in result.planned_exports
    } == {tomorrow[0].start.date()}
    assert result.ending_battery_kwh >= 4.0 - 1e-6
    assert abs(result.automatic_price_floor_pln_kwh - 0.9) < 1e-6


def test_home_energy_is_never_sold() -> None:
    """A weak PV forecast must keep enough energy for both protected nights."""
    price_slots = [
        *slots(0, 18, 8, 1.20),
        *slots(1, 6, 8, 1.50),
    ]
    tomorrow_noon = NOW.replace(hour=12) + timedelta(days=1)
    result = RCE.optimize_rce(
        base_input(
            price_slots=price_slots,
            pv_by_slot_kwh={tomorrow_noon: 2.0},
            average_daily_load_kwh=7.0,
            average_night_load_kwh=4.0,
        )
    )
    assert result.ready
    assert result.minimum_soc_percent > 20
    assert abs(result.base_reserve_energy_kwh - 4.0) < 1e-6
    assert result.additional_forecast_reserve_kwh > 0
    assert abs(
        result.protected_home_energy_kwh
        - (
            result.base_reserve_energy_kwh
            + result.additional_forecast_reserve_kwh
        )
    ) < 1e-6
    assert result.ending_battery_kwh >= 4.0 - 1e-6
    assert result.planned_export_kwh < 4.0


def test_upcoming_night_is_reserved_even_before_sunset() -> None:
    """A sunny daytime forecast must not hide the next night's house load."""
    result = RCE.optimize_rce(
        base_input(
            now=NOW.replace(hour=12),
            price_slots=slots(0, 18, 4, 1.0),
            battery_capacity_kwh=100.0,
            outage_reserve_soc_percent=30.0,
            safety_margin_soc_percent=2.0,
            average_daily_load_kwh=30.0,
            average_night_load_kwh=20.0,
            pv_by_slot_kwh={
                NOW.replace(hour=16): 60.0,
            },
        )
    )
    assert result.ready
    assert abs(result.base_reserve_energy_kwh - 32.0) < 1e-6
    assert abs(result.protected_night_energy_kwh - 20.0) < 1e-6
    # The single 60 kWh PV spike is now credited only up to the physical
    # 10 kW/30-minute charge bridge.  That exposes another 1.67 kWh of LOAD
    # which the former unlimited-refill model hid.
    assert abs(result.protected_home_energy_kwh - 53.6666666667) < 1e-6
    assert result.minimum_soc_percent == 54
    assert result.ending_battery_kwh >= 32.0 - 1e-6


def test_low_market_prices_still_use_the_best_48h_slots() -> None:
    """No manual threshold may prevent controlled export at the best price."""
    today = slots(0, 18, 4, 0.05)
    tomorrow = slots(1, 6, 4, 0.12)
    result = RCE.optimize_rce(
        base_input(price_slots=[*today, *tomorrow])
    )
    assert result.ready
    assert result.planned_exports
    assert {
        item.start.date() for item in result.planned_exports
    } == {tomorrow[0].start.date()}
    assert abs(result.automatic_price_floor_pln_kwh - 0.12) < 1e-6


def test_negative_prices_do_not_dump_stored_energy() -> None:
    """Stored surplus must be retained when every available sale loses money."""
    result = RCE.optimize_rce(
        base_input(price_slots=slots(0, 18, 4, -0.2))
    )
    assert result.ready
    assert not result.planned_exports
    assert result.status_code == "home_protected"
    assert result.automatic_price_floor_pln_kwh is None
    assert abs(result.optimization_gain_pln) < 1e-6


def test_discharge_creates_headroom_before_worse_pv_overflow() -> None:
    """An earlier sale is valid when it avoids a lower-priced PV spill."""
    earlier = RCE.PriceSlot(
        start=NOW.replace(hour=10),
        price_pln_kwh=0.30,
    )
    overflow = RCE.PriceSlot(
        start=NOW.replace(hour=12),
        price_pln_kwh=-0.20,
    )
    result = RCE.optimize_rce(
        base_input(
            price_slots=[earlier, overflow],
            pv_by_slot_kwh={overflow.start: 10.0},
        )
    )
    assert result.ready
    assert [item.start for item in result.planned_exports] == [earlier.start]
    assert result.total_revenue_pln > result.uncontrolled_revenue_pln
    assert result.optimization_gain_pln > 2.4


def test_shortage_blocks_export() -> None:
    """Export must fail closed when the home cannot reach the safety floor."""
    result = RCE.optimize_rce(
        base_input(
            price_slots=slots(0, 18, 4, 2.0),
            battery_soc_percent=35.0,
            average_daily_load_kwh=12.0,
            average_night_load_kwh=7.0,
        )
    )
    assert not result.ready
    assert result.status_code == "home_energy_shortage"
    assert not result.planned_exports


def test_parallel_power_scales_slot_energy() -> None:
    """Detected parallel inverters must multiply the physical export limit."""
    candidate = slots(0, 12, 1, 1.0)
    single = RCE.optimize_rce(
        base_input(
            price_slots=candidate,
            battery_capacity_kwh=100.0,
            outage_reserve_soc_percent=0.0,
            inverter_count=1,
            discharge_power_percent=50.0,
        )
    )
    parallel = RCE.optimize_rce(
        base_input(
            price_slots=candidate,
            battery_capacity_kwh=100.0,
            outage_reserve_soc_percent=0.0,
            inverter_count=2,
            discharge_power_percent=50.0,
        )
    )
    assert abs(single.maximum_export_power_kw - 5.0) < 1e-6
    assert abs(parallel.maximum_export_power_kw - 10.0) < 1e-6
    assert abs(single.planned_export_kwh - 2.5) < 0.02
    assert abs(parallel.planned_export_kwh - 5.0) < 0.02


def test_export_lockout_excludes_slots() -> None:
    """A blocked high price must not override the configured lockout."""
    blocked = RCE.PriceSlot(
        start=NOW.replace(hour=22),
        price_pln_kwh=2.0,
        blocked=True,
    )
    allowed = RCE.PriceSlot(
        start=NOW.replace(hour=20),
        price_pln_kwh=0.8,
    )
    result = RCE.optimize_rce(base_input(price_slots=[blocked, allowed]))
    assert [item.start for item in result.planned_exports] == [allowed.start]


def test_feasible_plan_is_not_broken_by_display_rounding() -> None:
    """A fractional final slot must not invalidate an otherwise safe plan."""
    result = RCE.optimize_rce(
        base_input(
            price_slots=slots(0, 6, 32, 0.8),
            battery_capacity_kwh=5.1,
            battery_soc_percent=53.0,
            export_efficiency_percent=93.0,
            discharge_power_percent=30.0,
        )
    )
    assert result.ready
    assert result.status_code != "optimizer_error"
    assert result.ending_battery_kwh >= 1.02 - 1e-6


def test_today_only_prices_produce_a_safe_plan() -> None:
    """Missing tomorrow prices must not block profitable slots today."""
    today = slots(0, 18, 6, 0.9)
    result = RCE.optimize_rce(
        base_input(
            now=NOW.replace(hour=6),
            price_slots=today,
            average_daily_load_kwh=4.0,
            average_night_load_kwh=2.0,
        )
    )
    assert result.ready
    assert result.planned_exports
    assert {item.start.date() for item in result.planned_exports} == {
        NOW.date()
    }
    assert result.ending_battery_kwh >= 4.0 - 1e-6


def test_export_and_revenue_totals_expose_their_sources() -> None:
    """The UI totals must equal battery export plus natural PV surplus."""
    planned = RCE.PriceSlot(
        start=NOW.replace(hour=10),
        price_pln_kwh=1.0,
    )
    overflow = RCE.PriceSlot(
        start=NOW.replace(hour=12),
        price_pln_kwh=0.5,
    )
    result = RCE.optimize_rce(
        base_input(
            price_slots=[planned, overflow],
            pv_by_slot_kwh={overflow.start: 10.0},
            bms_max_charge_current_a=0.0,
            bms_charge_data_fresh=True,
            bms_charge_data_available=False,
        )
    )
    assert result.ready
    assert result.planned_export_kwh > 4.9
    assert result.natural_export_kwh > 4.9
    assert abs(
        result.total_export_kwh
        - (result.planned_export_kwh + result.natural_export_kwh)
    ) < 1e-6
    assert abs(
        result.total_revenue_pln
        - (result.planned_revenue_pln + result.natural_revenue_pln)
    ) < 1e-6


def test_bms_current_limit_caps_export_power() -> None:
    """A small battery must cap a larger inverter before planning export."""
    candidate = slots(0, 18, 1, 1.0)
    result = RCE.optimize_rce(
        base_input(
            price_slots=candidate,
            battery_capacity_kwh=21.0,
            outage_reserve_soc_percent=0.0,
            inverter_power_kw=20.0,
            discharge_power_percent=100.0,
            export_efficiency_percent=95.0,
            bms_max_discharge_current_a=170.0,
            battery_voltage_v=53.0,
            bms_power_safety_percent=95.0,
        )
    )
    expected_limit = 170.0 * 53.0 / 1000.0 * 0.95 * 0.95
    assert result.ready
    assert result.bms_limit_active
    assert abs(result.requested_export_power_kw - 20.0) < 1e-6
    assert abs(result.bms_discharge_power_limit_kw - expected_limit) < 1e-6
    assert abs(result.maximum_export_power_kw - expected_limit) < 1e-6
    assert abs(
        result.bms_discharge_limit_percent
        - expected_limit / 20.0 * 100.0
    ) < 1e-6
    assert abs(result.planned_export_kwh - expected_limit * 0.5) < 0.02


def test_actual_day_load_corrects_day_load_projection() -> None:
    """Actual phase LOAD energy must correct an understated daytime profile."""
    result = RCE.optimize_rce(
        base_input(
            now=NOW.replace(hour=14),
            price_slots=slots(0, 14, 12, 1.0),
            battery_capacity_kwh=100.0,
            average_daily_load_kwh=10.0,
            average_night_load_kwh=8.0,
            actual_day_load_today_kwh=4.0,
            pv_to_load_power_kw=2.0,
        )
    )
    assert result.ready
    assert abs(result.daylight_progress_percent - 50.0) < 1e-6
    assert abs(result.historical_day_load_kwh - 2.0) < 1e-6
    assert abs(result.live_projected_day_load_kwh - 8.0) < 1e-6
    assert abs(result.modeled_day_load_kwh - 8.0) < 1e-6


def test_recorder_profile_is_used_instead_of_flat_load() -> None:
    """A valid 48-slot weekday profile must drive the physical simulation."""
    profile = [0.0] * 48
    profile[12] = 2.0
    profile[36] = 6.0
    profile[44] = 2.0
    result = RCE.optimize_rce(
        base_input(
            now=NOW.replace(hour=6),
            price_slots=slots(0, 18, 4, 1.0),
            average_daily_load_kwh=10.0,
            average_night_load_kwh=2.0,
            weekday_load_profile_30m_kwh=tuple(profile),
            weekend_load_profile_30m_kwh=tuple(profile),
        )
    )
    assert result.ready
    assert result.load_profile_mode == "weekday_48_slot"


def test_conservative_pv_band_blocks_unsafe_export() -> None:
    """P50 optimism must not make a plan feasible when P10 cannot feed home."""
    noon = NOW.replace(hour=12)
    result = RCE.optimize_rce(
        base_input(
            now=NOW.replace(hour=8),
            price_slots=slots(0, 18, 4, 1.0),
            battery_soc_percent=20.0,
            average_daily_load_kwh=8.0,
            average_night_load_kwh=4.0,
            pv_by_slot_kwh={noon: 20.0},
            conservative_pv_by_slot_kwh={noon: 0.0},
        )
    )
    assert not result.ready
    assert result.status_code == "home_energy_shortage"


def test_gcf_zero_export_is_a_hard_cap_with_clear_status() -> None:
    """An enabled 0% GCF installation must never receive an export plan."""
    result = RCE.optimize_rce(
        base_input(
            price_slots=slots(0, 18, 4, 2.0),
            export_power_cap_kw=0.0,
            pv_by_slot_kwh={NOW.replace(hour=12): 30.0},
        )
    )
    assert result.ready
    assert result.status_code == "zero_export"
    assert result.maximum_export_power_kw == 0.0
    assert not result.planned_exports
    assert result.natural_export_kwh == 0.0
    assert result.uncontrolled_export_kwh == 0.0
    assert result.physical_limit_source == "gcf_export_cap"


def test_effective_power_input_caps_slot_energy() -> None:
    """A trusted learned limit must cap the catalogue inverter power."""
    result = RCE.optimize_rce(
        base_input(
            price_slots=slots(0, 18, 1, 1.0),
            battery_capacity_kwh=100.0,
            outage_reserve_soc_percent=0.0,
            effective_export_power_kw=3.2,
        )
    )
    assert abs(result.maximum_export_power_kw - 3.2) < 1e-6
    assert abs(result.planned_export_kwh - 1.6) < 0.02
    assert result.physical_limit_source == "effective_export_power"


def test_gcf_caps_natural_pv_export_per_slot() -> None:
    """PV overflow must respect the same enabled GCF AC export ceiling."""
    result = RCE.optimize_rce(
        base_input(
            price_slots=[],
            pv_by_slot_kwh={NOW.replace(hour=12): 10.0},
            export_power_cap_kw=2.0,
        )
    )
    assert result.ready
    assert result.natural_export_kwh <= 1.0 + 1e-6


def test_battery_wear_rejects_gross_but_unprofitable_sale() -> None:
    """Gross positive RCE price below throughput wear must retain energy."""
    result = RCE.optimize_rce(
        base_input(
            price_slots=slots(0, 18, 2, 0.05),
            battery_wear_cost_pln_kwh=0.08,
        )
    )
    assert not result.planned_exports
    assert result.net_optimization_gain_pln >= -1e-6


def test_day3_terminal_value_is_diagnostic_only() -> None:
    """Day-3 value must not veto a sale-profit-maximizing RCE plan."""
    pv = {
        NOW.replace(hour=12): 5.0,
        NOW.replace(hour=12) + timedelta(days=1): 5.0,
    }
    price = slots(1, 18, 4, 0.50)
    without_day3 = RCE.optimize_rce(
        base_input(
            price_slots=price,
            pv_by_slot_kwh=pv,
            average_daily_load_kwh=5.0,
            average_night_load_kwh=0.0,
            battery_wear_cost_pln_kwh=0.0,
        )
    )
    with_day3 = RCE.optimize_rce(
        base_input(
            price_slots=price,
            pv_by_slot_kwh=pv,
            average_daily_load_kwh=5.0,
            average_night_load_kwh=0.0,
            battery_wear_cost_pln_kwh=0.0,
            avoided_import_price_pln_kwh=1.0,
            day3_pv_forecast_kwh=0.0,
        )
    )
    assert with_day3.terminal_energy_target_kwh == 5.0
    assert not with_day3.terminal_energy_value_applied_to_objective
    assert abs(
        with_day3.planned_export_kwh - without_day3.planned_export_kwh
    ) < 0.02
    assert abs(
        with_day3.ending_battery_kwh - without_day3.ending_battery_kwh
    ) < 0.02


def test_high_sale_price_is_not_blocked_by_default_terminal_value() -> None:
    """A 1 PLN/kWh Day-3 diagnostic cannot block profitable RCE export."""
    pv = {
        NOW.replace(hour=12): 10.0,
        NOW.replace(hour=12) + timedelta(days=1): 10.0,
    }
    result = RCE.optimize_rce(
        base_input(
            price_slots=slots(0, 18, 8, 1.10),
            pv_by_slot_kwh=pv,
            average_daily_load_kwh=10.0,
            day3_pv_forecast_kwh=5.0,
            battery_wear_cost_pln_kwh=0.0,
            avoided_import_price_pln_kwh=1.0,
            dynamic_reserve_enabled=False,
        )
    )

    assert result.terminal_energy_target_kwh == 5.0
    assert result.terminal_energy_value_pln_kwh == 1.0
    assert not result.terminal_energy_value_applied_to_objective
    assert result.planned_export_kwh > 9.0
    assert result.ending_battery_kwh < 5.0


def test_terminal_value_boundary_uses_house_discharge_efficiency() -> None:
    """Day-3 diagnostics retain their DC-to-house efficiency conversion."""
    pv = {
        NOW.replace(hour=12): 10.0,
        NOW.replace(hour=12) + timedelta(days=1): 10.0,
    }
    common = {
        "pv_by_slot_kwh": pv,
        "average_daily_load_kwh": 10.0,
        "day3_pv_forecast_kwh": 5.0,
        "battery_wear_cost_pln_kwh": 0.0,
        "avoided_import_price_pln_kwh": 1.0,
        "dynamic_reserve_enabled": False,
        "export_efficiency_percent": 95.0,
        "house_discharge_efficiency_percent": 80.0,
    }
    below = RCE.optimize_rce(
        base_input(price_slots=slots(0, 18, 8, 0.84), **common)
    )
    above = RCE.optimize_rce(
        base_input(price_slots=slots(0, 18, 8, 0.85), **common)
    )

    assert below.terminal_energy_value_pln_kwh == 0.8
    assert below.terminal_energy_target_kwh == 6.25
    assert not below.terminal_energy_value_applied_to_objective
    assert abs(above.planned_export_kwh - below.planned_export_kwh) < 0.02
    assert abs(above.ending_battery_kwh - below.ending_battery_kwh) < 0.02


def test_terminal_reserve_reports_day3_availability_and_reason() -> None:
    """A zero target must distinguish missing Day-3 data from sufficient PV."""
    common = {
        "price_slots": slots(1, 18, 4, 0.50),
        "average_daily_load_kwh": 5.0,
        "average_night_load_kwh": 0.0,
        "battery_wear_cost_pln_kwh": 0.0,
    }
    missing = RCE.optimize_rce(base_input(**common))
    covered = RCE.optimize_rce(
        base_input(**common, day3_pv_forecast_kwh=6.0)
    )
    deficit = RCE.optimize_rce(
        base_input(**common, day3_pv_forecast_kwh=1.0)
    )

    assert not missing.day3_forecast_available
    assert missing.day3_forecast_kwh is None
    assert missing.day3_energy_shortfall_kwh == 0.0
    assert missing.terminal_energy_target_kwh == 0.0
    assert missing.terminal_reserve_reason == "day3_forecast_missing"

    assert covered.day3_forecast_available
    assert covered.day3_forecast_kwh == 6.0
    assert covered.day3_load_requirement_kwh == 5.0
    assert covered.day3_energy_shortfall_kwh == 0.0
    assert covered.terminal_energy_target_kwh == 0.0
    assert covered.terminal_reserve_reason == "day3_pv_covers_load"

    assert deficit.day3_forecast_available
    assert deficit.day3_energy_shortfall_kwh == 4.0
    assert deficit.terminal_energy_target_kwh == 4.0
    assert deficit.terminal_reserve_reason == "day3_pv_deficit"


def test_whole_soc_control_reserve_does_not_overstate_export() -> None:
    """A fractional protected reserve must be rounded up before planning."""
    result = RCE.optimize_rce(
        base_input(
            now=NOW.replace(hour=12),
            price_slots=slots(0, 18, 5, 2.0),
            battery_capacity_kwh=230.0,
            battery_soc_percent=55.0,
            outage_reserve_soc_percent=20.0,
            safety_margin_soc_percent=2.0,
            average_daily_load_kwh=7.79,
            average_night_load_kwh=7.79,
            inverter_power_kw=30.0,
            export_efficiency_percent=95.0,
            battery_wear_cost_pln_kwh=0.0,
        )
    )

    assert result.ready
    assert result.minimum_soc_percent == 26
    assert abs(result.base_reserve_energy_kwh - 50.6) < 1e-6
    assert abs(result.protected_home_energy_kwh - 58.39) < 1e-6
    assert abs(result.control_reserve_energy_kwh - 59.8) < 1e-6
    assert abs(result.soc_quantization_reserve_kwh - 1.41) < 1e-6
    assert abs(result.available_energy_now_kwh - 66.7) < 1e-6
    assert (
        result.planned_export_kwh
        <= result.available_energy_now_kwh * 0.95 + 1e-6
    )
    # The control threshold protects the upcoming night at export time.  LOAD
    # then legitimately consumes that protected energy, so horizon-end SOC may
    # be below 26% but must remain above the 22% outage reserve.
    assert result.ending_battery_kwh < result.control_reserve_energy_kwh
    assert result.ending_battery_kwh >= result.base_reserve_energy_kwh - 1e-6


def test_gross_and_net_optimization_gain_are_unambiguous() -> None:
    """Legacy gain stays gross while net gain subtracts only battery wear."""
    result = RCE.optimize_rce(
        base_input(
            price_slots=slots(0, 18, 4, 1.0),
            battery_wear_cost_pln_kwh=0.08,
        )
    )
    assert result.planned_export_kwh > 0.0
    assert abs(
        result.optimization_gain_pln - result.gross_optimization_gain_pln
    ) < 1e-9
    assert result.net_optimization_gain_pln < result.gross_optimization_gain_pln
    assert abs(
        result.net_optimization_gain_pln
        - (
            result.gross_optimization_gain_pln
            - result.battery_wear_cost_pln
        )
    ) < 1e-6
    assert not result.terminal_energy_value_applied_to_objective


def test_sensor_exposes_terminal_and_gain_contract() -> None:
    """The HA sensor must publish the explicit optimizer diagnostics."""
    sensor_source = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    for attribute in (
        '"day3_forecast_available"',
        '"day3_energy_shortfall_kwh"',
        '"terminal_reserve_reason"',
        '"terminal_energy_target_reason"',
        '"gross_optimization_gain_pln"',
        '"net_optimization_gain_pln"',
        '"terminal_energy_value_delta_pln"',
        '"terminal_energy_value_applied_to_objective"',
        '"current_slot_end"',
        '"current_run_end"',
        '"current_slot_start_eligible"',
        '"current_slot_suppression_reason"',
        '"current_required_minimum_soc_percent"',
        '"critical_zero_pv_guard_active"',
        '"load_risk_buffer_kwh"',
        '"load_risk_mode"',
        '"solver_method"',
        '"optimality_verified"',
        '"solver_runtime_ms"',
        '"bms_discharge_data_fresh"',
        '"bms_discharge_data_age_seconds"',
        '"bms_discharge_data_available"',
    ):
        assert attribute in sensor_source
    assert "from .energy_data import numeric_state_sample, state_age_seconds" in sensor_source
    assert "sample = numeric_state_sample(" in sensor_source
    assert "return age / 60.0 if age is not None else None" in sensor_source


def test_dynamic_solcast_sources_and_day3_freshness_contract() -> None:
    """Configured sources invalidate plans and optional Day 3 fails soft."""
    sensor_source = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    for marker in (
        '"input_text.hoymiles_solcast_forecast_day_3_entity"',
        '"sensor.solcast_pv_forecast_prognoza_na_dzien_3"',
        '"sensor.solcast_pv_forecast_forecast_d3"',
        "def _configured_forecast_entity_ids(",
        "def _configured_forecast_source_ids(",
        "def _refresh_dynamic_forecast_listener(",
        "WATCHED_ENTITIES | self._configured_forecast_source_ids()",
        'if event.data["entity_id"] in FORECAST_ENTITY_HELPERS:',
        "day3_forecast_sample = numeric_state_sample(",
        "max_age_seconds=_DAY3_FORECAST_MAX_AGE_SECONDS",
        "day3_forecast_state if forecast_day3_data_fresh else None",
        '"forecast_day3_data_fresh"',
        '"forecast_day3_data_complete"',
        '"forecast_day3_age_seconds"',
        '"forecast_day3_data_reason"',
    ):
        assert marker in sensor_source
    # Day 3 improves the terminal diagnostic when fresh, but can never block
    # an otherwise safe today/tomorrow plan merely because it is disabled.
    assert 'required["Solcast Forecast Day 3"]' not in sensor_source


def test_shared_forecast_model_is_conservative_and_robust() -> None:
    """Live underproduction and an outlier cannot make PV optimistic."""
    factor, ratio, confidence = FORECAST.adaptive_forecast_factor(
        0.9,
        2.0,
        5.0,
        eligible=True,
    )
    assert ratio == 0.4
    assert 0.4 < factor < 0.9
    assert confidence > 0.8
    learned, uncertainty, count = FORECAST.robust_weighted_factor(
        [(1.0, 0.82), (2.0, 0.80), (3.0, 1.10), (8.0, 0.78)]
    )
    assert count == 4
    assert 0.78 <= learned <= 0.83
    assert uncertainty >= 0.0


def _load_forecast_learning_policy_snapshot():
    """Execute the production GCF snapshot resolver without importing HA."""

    source = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_forecast_learning_policy_snapshot"
    )
    max_skew = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_FORECAST_GCF_READBACK_MAX_SKEW_SECONDS"
            for target in node.targets
        )
    )
    namespace = {
        "Any": object,
        "HomeAssistant": object,
        "datetime": datetime,
        "ForecastLearningPolicy": FORECAST.ForecastLearningPolicy,
        "numeric_state_sample": ENERGY_DATA.numeric_state_sample,
        "resolve_forecast_learning_policy": (
            FORECAST.resolve_forecast_learning_policy
        ),
        "_FORECAST_GCF_SUPPORT_MAX_AGE_SECONDS": 10.0,
        "_FORECAST_GCF_READBACK_MAX_AGE_SECONDS": 180.0,
        "_FORECAST_GCF_READBACK_MAX_SKEW_SECONDS": max_skew,
        "FORECAST_GCF_ENABLE_ENTITY": (
            "sensor.hoymiles_hit_gcf_enable_readback_code"
        ),
        "FORECAST_GCF_LIMIT_ENTITY": (
            "sensor.hoymiles_hit_gcf_maximum_export_power_readback"
        ),
        "FORECAST_GCF_GENERATION_ENTITY": (
            "sensor.hoymiles_hit_gcf_control_readback_generation"
        ),
        "FORECAST_GCF_SUPPORT_ENTITY": (
            "sensor.hoymiles_hit_ems_verified_hardware_readback_supported"
        ),
    }
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    exec(compile(module, "<forecast-gcf-snapshot>", "exec"), namespace)
    return namespace["_forecast_learning_policy_snapshot"], namespace


def test_forecast_gcf_snapshot_accepts_one_cycle_in_either_event_order() -> None:
    """HA delivery order cannot invalidate one completed physical 258/259 cycle."""

    snapshot, names = _load_forecast_learning_policy_snapshot()

    def state(value: object, reported_at: datetime):
        return type(
            "FakeState",
            (),
            {
                "state": str(value),
                "last_reported": reported_at,
                "last_updated": reported_at,
            },
        )()

    class FakeStates(dict):
        pass

    support = names["FORECAST_GCF_SUPPORT_ENTITY"]
    enable = names["FORECAST_GCF_ENABLE_ENTITY"]
    limit = names["FORECAST_GCF_LIMIT_ENTITY"]
    generation = names["FORECAST_GCF_GENERATION_ENTITY"]

    def evaluate(
        *,
        support_at: datetime | None = NOW,
        support_value: object = 1.0,
        enable_at: datetime | None = NOW - timedelta(seconds=2),
        enable_value: object = 1.0,
        limit_at: datetime | None = NOW - timedelta(seconds=1),
        limit_value: object = 10.0,
        generation_at: datetime | None = NOW,
        generation_value: object = 7.0,
    ):
        states = FakeStates()
        if support_at is not None:
            states[support] = state(support_value, support_at)
        if enable_at is not None:
            states[enable] = state(enable_value, enable_at)
        if limit_at is not None:
            states[limit] = state(limit_value, limit_at)
        if generation_at is not None:
            states[generation] = state(generation_value, generation_at)
        hass = type("FakeHass", (), {"states": states})()
        return snapshot(hass, NOW)

    # Normal ordering: generation is observed after both physical mirrors.
    policy, diagnostics = evaluate()
    assert diagnostics["forecast_learning_gcf_readback_verified"] is True
    assert diagnostics["forecast_learning_gcf_readback_reason"] is None
    assert policy.enabled and policy.mode == "adaptive"
    assert FORECAST.forecast_factor_for_policy(policy, 0.93) == 0.93

    # Native ESPHome order: generation is emitted from 259.on_value before the
    # outer 259 state report.  The old 259 is rejected, then the same-cycle
    # catch-up closes the cohort without guessing or weakening freshness.
    early_policy, early_diagnostics = evaluate(
        generation_at=NOW - timedelta(seconds=1),
        limit_at=NOW - timedelta(seconds=20),
    )
    assert not early_policy.enabled
    assert (
        early_diagnostics["forecast_learning_gcf_readback_reason"]
        == "gcf_readback_incoherent"
    )
    caught_up, caught_up_diagnostics = evaluate(
        generation_at=NOW - timedelta(seconds=1),
        limit_at=NOW,
    )
    assert caught_up_diagnostics["forecast_learning_gcf_readback_verified"] is True
    assert caught_up.enabled and caught_up.mode == "adaptive"

    stale_limit, stale_limit_diagnostics = evaluate(
        limit_at=NOW - timedelta(seconds=181),
    )
    assert not stale_limit.enabled
    assert stale_limit_diagnostics["forecast_learning_gcf_readback_reason"] == (
        "gcf_limit_stale"
    )
    stale_enable, stale_enable_diagnostics = evaluate(
        enable_at=NOW - timedelta(seconds=181),
    )
    assert not stale_enable.enabled
    assert stale_enable_diagnostics["forecast_learning_gcf_readback_reason"] == (
        "gcf_enable_stale"
    )
    missing_generation, missing_generation_diagnostics = evaluate(
        generation_at=None,
    )
    assert not missing_generation.enabled
    assert missing_generation_diagnostics[
        "forecast_learning_gcf_readback_reason"
    ] == "gcf_generation_missing"
    old_cycle, old_cycle_diagnostics = evaluate(
        enable_at=NOW,
        limit_at=NOW,
        generation_at=NOW - timedelta(seconds=6),
    )
    assert not old_cycle.enabled
    assert old_cycle_diagnostics["forecast_learning_gcf_readback_reason"] == (
        "gcf_readback_incoherent"
    )

    for span_seconds in (4.999, 5.0):
        boundary, boundary_diagnostics = evaluate(
            enable_at=NOW - timedelta(seconds=span_seconds),
            limit_at=NOW,
            generation_at=NOW,
        )
        assert boundary.enabled
        assert boundary_diagnostics[
            "forecast_learning_gcf_readback_verified"
        ] is True
    over_boundary, over_boundary_diagnostics = evaluate(
        enable_at=NOW - timedelta(seconds=5.001),
        limit_at=NOW,
        generation_at=NOW,
    )
    assert not over_boundary.enabled
    assert over_boundary_diagnostics[
        "forecast_learning_gcf_readback_reason"
    ] == "gcf_readback_incoherent"

    exact_future_tolerance, exact_future_diagnostics = evaluate(
        generation_at=NOW + timedelta(seconds=5.0),
        enable_at=NOW,
        limit_at=NOW,
    )
    assert exact_future_tolerance.enabled
    assert exact_future_diagnostics[
        "forecast_learning_gcf_readback_verified"
    ] is True
    future_generation, future_generation_diagnostics = evaluate(
        generation_at=NOW + timedelta(seconds=5.001),
    )
    assert not future_generation.enabled
    assert future_generation_diagnostics[
        "forecast_learning_gcf_readback_reason"
    ] == "gcf_generation_future_timestamp"

    fail_closed_cases = (
        ({"support_value": 0.0}, "hardware_readback_unverified"),
        ({"support_at": None}, "hardware_readback_missing"),
        ({"enable_value": "unknown"}, "gcf_enable_unavailable"),
        ({"enable_value": 2.0}, "gcf_enable_above_maximum"),
        ({"limit_at": None}, "gcf_limit_missing"),
        ({"limit_value": "unavailable"}, "gcf_limit_unavailable"),
        ({"limit_value": "nan"}, "gcf_limit_not_finite"),
    )
    for kwargs, expected_reason in fail_closed_cases:
        rejected, rejected_diagnostics = evaluate(**kwargs)
        assert not rejected.enabled
        assert rejected.mode == "conservative_gcf_unverified"
        assert rejected_diagnostics[
            "forecast_learning_gcf_readback_reason"
        ] == expected_reason

    zero_export, zero_diagnostics = evaluate(limit_value=0.0)
    assert zero_diagnostics["forecast_learning_gcf_readback_verified"] is True
    assert not zero_export.enabled
    assert zero_export.mode == "fixed_zero_export"
    assert zero_export.excluded_reason == "zero_export"
    assert FORECAST.forecast_factor_for_policy(zero_export, 0.93) == 0.80

    disabled, disabled_diagnostics = evaluate(enable_value=0.0)
    assert disabled_diagnostics["forecast_learning_gcf_readback_verified"] is True
    assert disabled.enabled and disabled.mode == "adaptive"
    positive, positive_diagnostics = evaluate(limit_value=25.0)
    assert positive_diagnostics["forecast_learning_gcf_readback_verified"] is True
    assert positive.enabled and positive.mode == "adaptive"


def _gcf_policy(
    *,
    enabled: bool = True,
    mode: str = "adaptive",
    excluded_reason: str | None = None,
    factor_override: float | None = None,
):
    return FORECAST.ForecastLearningPolicy(
        enabled,
        mode,
        excluded_reason,
        factor_override,
    )


def _gcf_diagnostics(
    *,
    enable_code: float | None = 1.0,
    export_limit: float | None = 10.0,
    generation: float | None = 7.0,
) -> dict[str, float | None]:
    return {
        "forecast_learning_gcf_enable_code": enable_code,
        "forecast_learning_gcf_export_limit_percent": export_limit,
        "forecast_learning_gcf_generation": generation,
    }


def _load_forecast_gcf_event_probe():
    """Execute the production cohort/lifecycle methods without importing HA."""

    source = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    max_skew = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "_FORECAST_GCF_READBACK_MAX_SKEW_SECONDS"
            for target in node.targets
        )
    )
    sensor_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "HoymilesRCEOptimizerSensor"
    )
    optimizer_signature_function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_forecast_gcf_optimizer_signature"
    )
    method_names = {
        "_async_forecast_gcf_policy_changed",
        "_cancel_forecast_gcf_policy_evaluation",
        "_clear_forecast_gcf_cohort_state",
        "_async_evaluate_forecast_gcf_policy",
        "_apply_forecast_gcf_policy",
        "_async_debounced_recalculate",
        "_cancel_delayed_recalculation",
    }
    methods = [
        node
        for node in sensor_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in method_names
    ]
    assert {method.name for method in methods} == method_names

    initial_policy = _gcf_policy()
    initial_diagnostics = _gcf_diagnostics()
    policy_slot = {
        "policy": initial_policy,
        "diagnostics": initial_diagnostics,
    }
    scheduled: list[dict[str, object]] = []
    counters = {
        "input_revision": 0,
        "optimizer_calls": 0,
        "publications": 0,
        "policy_refreshes": 0,
        "result_current": True,
    }

    def policy_signature(policy):
        return (
            policy.enabled,
            policy.mode,
            policy.excluded_reason,
            policy.factor_override,
        )

    def call_later(_hass, delay, callback):
        token = {"delay": delay, "callback": callback, "cancelled": False}
        scheduled.append(token)

        def cancel() -> None:
            token["cancelled"] = True

        return cancel

    probe_class = ast.ClassDef(
        name="ForecastGCFEventProbe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=methods,
    )
    module = ast.fix_missing_locations(
        ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__",
                    names=[ast.alias(name="annotations")],
                    level=0,
                ),
                optimizer_signature_function,
                probe_class,
            ],
            type_ignores=[],
        )
    )
    namespace = {
        "asyncio": asyncio,
        "callback": lambda method: method,
        "async_call_later": call_later,
        "dt_util": type("DTUtil", (), {"now": staticmethod(lambda: NOW)}),
        "_FORECAST_GCF_READBACK_MAX_SKEW_SECONDS": max_skew,
        "INPUT_RECALCULATION_DELAY_SECONDS": 1.0,
        "_forecast_learning_policy_signature": policy_signature,
        "_forecast_learning_policy_snapshot": (
            lambda _hass, _now: (
                policy_slot["policy"],
                policy_slot["diagnostics"],
            )
        ),
    }
    exec(compile(module, "<forecast-gcf-event-probe>", "exec"), namespace)
    optimizer_signature = namespace["_forecast_gcf_optimizer_signature"]
    probe = namespace["ForecastGCFEventProbe"]()
    probe.hass = object()
    probe._forecast_gcf_policy_evaluation_cancel = None
    probe._forecast_gcf_policy_signature = policy_signature(initial_policy)
    probe._forecast_gcf_optimizer_signature = optimizer_signature(
        initial_policy,
        initial_diagnostics,
    )
    probe._recalculate_cancel = None
    probe._delayed_recalculate_tasks = set()
    probe._lifecycle_stopped = False

    def invalidate() -> None:
        counters["input_revision"] += 1
        counters["result_current"] = False

    async def recalculate_and_write() -> None:
        counters["optimizer_calls"] += 1
        counters["publications"] += 1
        counters["result_current"] = True

    def refresh_policy() -> None:
        counters["policy_refreshes"] += 1

    probe._invalidate_internal_inputs = invalidate
    probe._recalculate_and_write = recalculate_and_write
    probe._schedule_forecast_policy_refresh = refresh_policy
    return probe, policy_slot, scheduled, counters, source, sensor_class


def _fire_gcf_callback(token: dict[str, object], now: datetime = NOW) -> None:
    assert token["cancelled"] is False
    callback = token["callback"]
    assert callable(callback)
    asyncio.run(callback(now))


def test_forecast_gcf_event_closure_is_bounded_and_never_transient() -> None:
    """A three-part report creates one semantic optimizer calculation."""

    (
        probe,
        policy_slot,
        scheduled,
        counters,
        source,
        sensor_class,
    ) = _load_forecast_gcf_event_probe()

    event_assignment = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "RCE_EVENT_DRIVEN_ENTITIES"
            for target in node.targets
        )
    )
    event_source = ast.get_source_segment(source, event_assignment)
    assert event_source is not None
    assert "RCE_GCF_OPTIMIZER_ENTITIES" in event_source
    fingerprint = next(
        node
        for node in sensor_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_current_input_fingerprint"
    )
    fingerprint_source = ast.get_source_segment(source, fingerprint)
    assert fingerprint_source is not None
    assert "WATCHED_ENTITIES | self._configured_forecast_source_ids()" in (
        fingerprint_source
    )
    assert "sensor.hoymiles_hit_gcf_enable_readback_code" in fingerprint_source
    assert (
        "sensor.hoymiles_hit_gcf_maximum_export_power_readback"
        in fingerprint_source
    )
    assert 'getattr(self, "_forecast_gcf_optimizer_signature", None)' in (
        fingerprint_source
    )
    watched_assignment = next(
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "WATCHED_ENTITIES"
            for target in node.targets
        )
    )
    watched_source = ast.get_source_segment(source, watched_assignment)
    assert watched_source is not None
    assert "sensor.hoymiles_hit_rce_optimized_plan" not in watched_source

    # The first two parts of a changed physical report are mixed. They arm one
    # fixed deadline without withdrawing the previous plan or changing its
    # input revision, even if the mixed event repeats rapidly.
    policy_slot["policy"] = _gcf_policy(
        enabled=False,
        mode="conservative_gcf_unverified",
        excluded_reason="gcf_readback_incoherent",
    )
    policy_slot["diagnostics"] = _gcf_diagnostics(
        export_limit=20.0,
        generation=8.0,
    )
    probe._async_forecast_gcf_policy_changed(object())
    first_deadline = scheduled[0]
    assert first_deadline["delay"] == 5.0
    assert counters["input_revision"] == 0
    assert counters["result_current"] is True
    probe._async_forecast_gcf_policy_changed(object())
    assert len(scheduled) == 1
    assert probe._forecast_gcf_policy_evaluation_cancel is not None
    assert counters["input_revision"] == 0

    # The last report part closes a coherent changed cohort. Multiple HA
    # callbacks for that one completion create one revision and one solver run.
    policy_slot["policy"] = _gcf_policy()
    probe._async_forecast_gcf_policy_changed(object())
    probe._async_forecast_gcf_policy_changed(object())
    assert first_deadline["cancelled"] is True
    assert probe._forecast_gcf_policy_evaluation_cancel is None
    assert counters["input_revision"] == 1
    assert counters["result_current"] is False
    assert len(scheduled) == 2
    assert scheduled[1]["delay"] == 1.0
    _fire_gcf_callback(scheduled[1])
    assert counters["optimizer_calls"] == 1
    assert counters["publications"] == 1
    assert counters["result_current"] is True

    # Exact duplicate completion and a newer provenance-only generation do not
    # change optimizer mathematics and therefore cannot manufacture churn.
    probe._async_forecast_gcf_policy_changed(object())
    policy_slot["diagnostics"] = _gcf_diagnostics(
        export_limit=20.0,
        generation=9.0,
    )
    probe._async_forecast_gcf_policy_changed(object())
    assert counters["input_revision"] == 1
    assert counters["optimizer_calls"] == 1
    assert len(scheduled) == 2

    # A consumed value changing under the same generation is not deduplicated.
    policy_slot["diagnostics"] = _gcf_diagnostics(
        export_limit=25.0,
        generation=9.0,
    )
    probe._async_forecast_gcf_policy_changed(object())
    probe._async_forecast_gcf_policy_changed(object())
    assert counters["input_revision"] == 2
    assert len(scheduled) == 3
    _fire_gcf_callback(scheduled[2])
    assert counters["optimizer_calls"] == 2
    assert counters["publications"] == 2

    # A genuinely new coherent semantic cohort is also consumed exactly once.
    policy_slot["diagnostics"] = _gcf_diagnostics(
        export_limit=30.0,
        generation=10.0,
    )
    for _part in range(3):
        probe._async_forecast_gcf_policy_changed(object())
    assert counters["input_revision"] == 3
    assert len(scheduled) == 4
    _fire_gcf_callback(scheduled[3])
    assert counters["optimizer_calls"] == 3
    assert counters["publications"] == 3
    assert counters["policy_refreshes"] == 0


def test_forecast_gcf_persistent_incoherence_and_unload_fail_closed() -> None:
    """The 5-second deadline is final and no retained state survives unload."""

    probe, policy_slot, scheduled, counters, source, sensor_class = (
        _load_forecast_gcf_event_probe()
    )
    policy_slot["policy"] = _gcf_policy(
        enabled=False,
        mode="conservative_gcf_unverified",
        excluded_reason="gcf_readback_incoherent",
    )
    policy_slot["diagnostics"] = _gcf_diagnostics(generation=8.0)
    probe._async_forecast_gcf_policy_changed(object())
    deadline = scheduled[0]
    assert deadline["delay"] == 5.0
    assert counters["input_revision"] == 0
    _fire_gcf_callback(deadline, NOW + timedelta(seconds=5.0))
    assert probe._forecast_gcf_policy_evaluation_cancel is None
    assert counters["input_revision"] == 1
    assert counters["result_current"] is False
    assert len(scheduled) == 2
    _fire_gcf_callback(scheduled[1])
    assert counters["optimizer_calls"] == 1
    assert counters["publications"] == 1

    # Once fail-closed has been applied, repeated persistent incoherence cannot
    # arm another deadline or create an optimizer loop.
    for _report in range(4):
        probe._async_forecast_gcf_policy_changed(object())
    assert len(scheduled) == 2
    assert counters["input_revision"] == 1
    assert counters["optimizer_calls"] == 1

    # A pending retained cohort is explicitly cancelled and cleared on unload.
    unload_probe, unload_slot, unload_scheduled, unload_counters, _, _ = (
        _load_forecast_gcf_event_probe()
    )
    unload_slot["policy"] = policy_slot["policy"]
    unload_slot["diagnostics"] = policy_slot["diagnostics"]
    unload_probe._async_forecast_gcf_policy_changed(object())
    unload_deadline = unload_scheduled[0]
    unload_probe._clear_forecast_gcf_cohort_state()
    unload_probe._cancel_delayed_recalculation()
    assert unload_deadline["cancelled"] is True
    assert unload_probe._forecast_gcf_policy_evaluation_cancel is None
    assert unload_probe._forecast_gcf_policy_signature is None
    assert unload_probe._forecast_gcf_optimizer_signature is None
    assert unload_probe._lifecycle_stopped is True
    assert unload_counters["optimizer_calls"] == 0
    assert unload_counters["publications"] == 0

    # A completion-scheduled calculation is also cancelled, and even a forced
    # late callback cannot publish through the stopped lifecycle.
    late_probe, late_slot, late_scheduled, late_counters, _, _ = (
        _load_forecast_gcf_event_probe()
    )
    late_slot["diagnostics"] = _gcf_diagnostics(export_limit=20.0)
    late_probe._async_forecast_gcf_policy_changed(object())
    queued_recalculation = late_scheduled[0]
    late_probe._clear_forecast_gcf_cohort_state()
    late_probe._cancel_delayed_recalculation()
    assert queued_recalculation["cancelled"] is True
    callback = queued_recalculation["callback"]
    assert callable(callback)
    asyncio.run(callback(NOW))
    assert late_counters["optimizer_calls"] == 0
    assert late_counters["publications"] == 0

    class FakeTask:
        def __init__(self) -> None:
            self.cancelled = False

        def done(self) -> bool:
            return False

        def cancel(self) -> None:
            self.cancelled = True

    running = FakeTask()
    late_probe._delayed_recalculate_tasks.add(running)
    late_probe._cancel_delayed_recalculation()
    assert running.cancelled is True

    added_to_hass = next(
        node
        for node in sensor_class.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_added_to_hass"
    )
    added_source = ast.get_source_segment(source, added_to_hass)
    assert added_source is not None
    assert "self._clear_forecast_gcf_cohort_state()" in added_source
    assert "self.async_on_remove(self._cancel_delayed_recalculation)" in (
        added_source
    )
    assert "RestoreEntity" not in source

    recalculate_locked = next(
        node
        for node in sensor_class.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_recalculate_locked"
    )
    recalculate_source = ast.get_source_segment(source, recalculate_locked)
    assert recalculate_source is not None
    grace_prefix = recalculate_source[
        : recalculate_source.index("captured_revision")
    ]
    assert "_forecast_gcf_policy_evaluation_cancel" in grace_prefix
    assert "_mark_recalculation_pending" not in grace_prefix


def test_zero_export_forecast_learning_policy_is_fixed_and_reversible() -> None:
    """A verified 0% cap never consumes clipped actual PV."""

    learned_factor = 0.93
    zero_export = FORECAST.resolve_forecast_learning_policy(
        readback_verified=True,
        gcf_enable_code=1.0,
        export_limit_percent=0.0,
    )
    assert not zero_export.enabled
    assert zero_export.mode == "fixed_zero_export"
    assert zero_export.excluded_reason == "zero_export"
    assert FORECAST.forecast_factor_for_policy(
        zero_export,
        learned_factor,
    ) == 0.80
    live_factor, live_ratio, confidence = FORECAST.adaptive_forecast_factor(
        FORECAST.forecast_factor_for_policy(zero_export, learned_factor),
        actual_energy_kwh=1.0,
        expected_elapsed_kwh=10.0,
        eligible=zero_export.enabled,
    )
    assert (live_factor, live_ratio, confidence) == (0.80, None, 0.0)

    export_restored = FORECAST.resolve_forecast_learning_policy(
        readback_verified=True,
        gcf_enable_code=1.0,
        export_limit_percent=0.1,
    )
    assert export_restored.enabled
    assert export_restored.mode == "adaptive"
    assert FORECAST.forecast_factor_for_policy(
        export_restored,
        learned_factor,
    ) == learned_factor

    gcf_disabled = FORECAST.resolve_forecast_learning_policy(
        readback_verified=True,
        gcf_enable_code=0.0,
        export_limit_percent=0.0,
    )
    assert gcf_disabled.enabled
    assert FORECAST.forecast_factor_for_policy(
        gcf_disabled,
        learned_factor,
    ) == learned_factor


def test_unverified_gcf_pauses_learning_without_guessing_zero_export() -> None:
    """Missing/stale evidence is conservative but distinct from 0% GCF."""

    for reason in (
        "gcf_generation_stale",
        "gcf_enable_missing",
        "gcf_limit_future_timestamp",
        "gcf_readback_incoherent",
        "hardware_readback_unverified",
    ):
        policy = FORECAST.resolve_forecast_learning_policy(
            readback_verified=False,
            gcf_enable_code=None,
            export_limit_percent=None,
            unverified_reason=reason,
        )
        assert not policy.enabled
        assert policy.mode == "conservative_gcf_unverified"
        assert policy.excluded_reason == reason
        assert policy.factor_override is None
        assert FORECAST.forecast_factor_for_policy(policy, 0.93) == 0.80
        assert FORECAST.forecast_factor_for_policy(policy, 0.72) == 0.72

    invalid_enable = FORECAST.resolve_forecast_learning_policy(
        readback_verified=True,
        gcf_enable_code=2.0,
        export_limit_percent=50.0,
    )
    assert invalid_enable.excluded_reason == "gcf_enable_invalid"
    invalid_negative_cap = FORECAST.resolve_forecast_learning_policy(
        readback_verified=True,
        gcf_enable_code=1.0,
        export_limit_percent=-10.0,
    )
    assert invalid_negative_cap.mode == "conservative_gcf_unverified"
    assert invalid_negative_cap.excluded_reason == "gcf_limit_invalid"
    maximum_positive_cap = FORECAST.resolve_forecast_learning_policy(
        readback_verified=True,
        gcf_enable_code=1.0,
        export_limit_percent=200.0,
    )
    assert maximum_positive_cap.enabled
    assert maximum_positive_cap.mode == "adaptive"
    assert maximum_positive_cap.excluded_reason is None
    assert maximum_positive_cap.factor_override is None
    invalid_excess_cap = FORECAST.resolve_forecast_learning_policy(
        readback_verified=True,
        gcf_enable_code=1.0,
        export_limit_percent=200.1,
    )
    assert invalid_excess_cap.excluded_reason == "gcf_limit_invalid"


def test_zero_export_days_never_reenter_forecast_learning_history() -> None:
    """Any unverified/blocked interval excludes the cumulative-PV day."""

    day = datetime(2026, 8, 10, tzinfo=WARSAW)
    always_allowed = [
        (day - timedelta(minutes=1), "on"),
        (day + timedelta(hours=12), "on"),
    ]
    physical_package = [
        (day - timedelta(minutes=1), "1.5.5"),
    ]
    assert FORECAST.forecast_learning_history_day_eligible(
        always_allowed,
        physical_package,
        day_start=day,
        day_end=day + timedelta(days=1),
    )

    zero_then_restored = [
        (day - timedelta(minutes=1), "on"),
        (day + timedelta(hours=10), "off"),
        (day + timedelta(hours=11), "on"),
    ]
    assert not FORECAST.forecast_learning_history_day_eligible(
        zero_then_restored,
        physical_package,
        day_start=day,
        day_end=day + timedelta(days=1),
    )
    for unknown_state in ("unknown", "unavailable", "off"):
        assert not FORECAST.forecast_learning_history_day_eligible(
            [
                (day, "on"),
                (day + timedelta(hours=6), unknown_state),
                (day + timedelta(hours=7), "on"),
            ],
            physical_package,
            day_start=day,
            day_end=day + timedelta(days=1),
        )
    assert FORECAST.forecast_learning_history_day_eligible(
        [(day, "on"), (day + timedelta(days=1), "off")],
        [(day, "1.5.2")],
        day_start=day,
        day_end=day + timedelta(days=1),
    )
    assert not FORECAST.forecast_learning_history_day_eligible(
        [(day + timedelta(hours=1), "on")],
        physical_package,
        day_start=day,
        day_end=day + timedelta(days=1),
    )

    # The v1.5.6 daily evidence attribute creates a Recorder row even when
    # both semantic states stay constant. The first retained/installation day
    # has no midnight boundary and is rejected; the following complete day can
    # use that heartbeat as its proven starting state.
    heartbeat_at = day + timedelta(minutes=1)
    retained_export = [(heartbeat_at, "on")]
    retained_version = [(heartbeat_at, "1.5.6")]
    assert not FORECAST.forecast_learning_history_day_eligible(
        retained_export,
        retained_version,
        day_start=day,
        day_end=day + timedelta(days=1),
    )
    next_day = day + timedelta(days=1)
    assert FORECAST.forecast_learning_history_day_eligible(
        retained_export,
        retained_version,
        day_start=next_day,
        day_end=next_day + timedelta(days=1),
    )

    legacy_ui_derived = [(day - timedelta(minutes=1), "1.5.1")]
    assert not FORECAST.forecast_learning_history_day_eligible(
        always_allowed,
        legacy_ui_derived,
        day_start=day,
        day_end=day + timedelta(days=1),
    )
    upgraded_midday = [
        (day - timedelta(minutes=1), "1.5.1"),
        (day + timedelta(hours=8), "1.5.5"),
    ]
    assert not FORECAST.forecast_learning_history_day_eligible(
        always_allowed,
        upgraded_midday,
        day_start=day,
        day_end=day + timedelta(days=1),
    )
    assert not FORECAST.forecast_learning_history_day_eligible(
        always_allowed,
        [],
        day_start=day,
        day_end=day + timedelta(days=1),
    )
    assert not FORECAST.forecast_learning_history_day_eligible(
        always_allowed,
        [(day - timedelta(minutes=1), "9" * 5000 + ".0.0")],
        day_start=day,
        day_end=day + timedelta(days=1),
    )

    # Recorder timestamps are UTC-aware, while day boundaries are local. Both
    # the 23-hour spring day and 25-hour autumn day must reject an intraday
    # fail-closed interval without inventing a fixed 24-hour window.
    for dst_day in (
        datetime(2026, 3, 29, tzinfo=WARSAW),
        datetime(2026, 10, 25, tzinfo=WARSAW),
    ):
        dst_end = dst_day + timedelta(days=1)
        assert (
            dst_end.astimezone(timezone.utc)
            - dst_day.astimezone(timezone.utc)
        ).total_seconds() in {23 * 3600, 25 * 3600}
        midpoint_utc = dst_day.astimezone(timezone.utc) + timedelta(hours=12)
        assert not FORECAST.forecast_learning_history_day_eligible(
            [
                (dst_day - timedelta(minutes=1), "on"),
                (midpoint_utc, "off"),
                (midpoint_utc + timedelta(minutes=1), "on"),
            ],
            [(dst_day - timedelta(minutes=1), "1.5.5")],
            day_start=dst_day,
            day_end=dst_end,
        )
        assert FORECAST.forecast_learning_history_day_eligible(
            [(dst_day - timedelta(minutes=1), "on")],
            [(dst_day - timedelta(minutes=1), "1.5.6")],
            day_start=dst_day,
            day_end=dst_end,
        )

    # A clipped 1/10 actual-vs-Solcast day must not depress an otherwise
    # stable 0.93 model, even when the model is rebuilt after export returns.
    candidate_days = [
        (always_allowed, 0.93),
        (zero_then_restored, 0.10),
    ]
    eligible_ratios = [
        ratio
        for history, ratio in candidate_days
        if FORECAST.forecast_learning_history_day_eligible(
            history,
            physical_package,
            day_start=day,
            day_end=day + timedelta(days=1),
        )
    ]
    learned, _, count = FORECAST.robust_weighted_factor(
        [(1.0, ratio) for ratio in eligible_ratios]
    )
    assert count == 1
    assert learned == 0.93


def test_forecast_learning_wiring_covers_rce_and_tariff() -> None:
    """Both independent HA models consume the same physical GCF contract."""

    rce_source = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    tariff_source = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "tariff_sensor.py"
    ).read_text(encoding="utf-8")
    for source in (rce_source, tariff_source):
        for marker in (
            "_forecast_learning_policy_snapshot",
            "FORECAST_EXPORT_ALLOWED_ENTITY",
            "FORECAST_EMS_PACKAGE_VERSION_ENTITY",
            "raw.get(FORECAST_EMS_PACKAGE_VERSION_ENTITY, [])",
            "package_version_history,",
            '"forecast_learning_enabled"',
            '"forecast_learning_mode"',
            '"forecast_learning_excluded_reason"',
            '"forecast_factor_used"',
            "forecast_learning_history_day_eligible(",
            "retained_last_qualified_factor_no_current_history",
        ):
            assert marker in source
        assert source.index('"forecast_learning_enabled"') < source.index(
            "if missing:"
        )
    assert "FORECAST_GCF_POLICY_TRIGGER_ENTITIES" in tariff_source
    assert """FORECAST_GCF_POLICY_TRIGGER_ENTITIES = (
    FORECAST_GCF_ENABLE_ENTITY,
    FORECAST_GCF_LIMIT_ENTITY,
    FORECAST_GCF_GENERATION_ENTITY,
    FORECAST_GCF_SUPPORT_ENTITY,
)""" in rce_source
    assert """FORECAST_GCF_COHORT_REPORT_ENTITIES = (
    FORECAST_GCF_ENABLE_ENTITY,
    FORECAST_GCF_LIMIT_ENTITY,
    FORECAST_GCF_GENERATION_ENTITY,
)""" in rce_source
    assert "FORECAST_GCF_COHORT_REPORT_ENTITIES" in tariff_source
    for marker in (
        "async_track_state_report_event",
        "_forecast_gcf_policy_evaluation_cancel",
    ):
        assert marker in rce_source
        assert marker in tariff_source
    for source, pending_during_grace in (
        (rce_source, False),
        (tariff_source, True),
    ):
        assert "previous_signature" in source
        assert "self._entry.async_create_background_task(" in source
        assert "self.hass.async_create_task(" not in source
        assert "_forecast_policy_refresh_task" in source
        assert "self.async_on_remove(task.cancel)" in source
        assert "_async_refresh_forecast_after_policy_restore" in source
        sensor_tree = ast.parse(source)
        sensor_class = next(
            node
            for node in sensor_tree.body
            if isinstance(node, ast.ClassDef)
            and node.name
            in {"HoymilesRCEOptimizerSensor", "HoymilesTariffOptimizerSensor"}
        )
        recalculate = next(
            node
            for node in sensor_class.body
            if isinstance(node, ast.AsyncFunctionDef)
            and node.name == "_recalculate_locked"
        )
        recalculate_source = ast.get_source_segment(source, recalculate)
        assert recalculate_source is not None
        # Normal timer/input recalculations cannot bypass the bounded GCF
        # cohort barrier and publish a transient unverified policy.
        assert recalculate_source.index(
            "_forecast_gcf_policy_evaluation_cancel"
        ) < recalculate_source.index("self._optimizer_input()")
        pending_prefix = recalculate_source[
            : recalculate_source.index("self._optimizer_input()")
        ]
        if pending_during_grace:
            assert "self._mark_recalculation_pending()" in pending_prefix
        else:
            # RCE retains its last coherent plan only until the exact bounded
            # deadline; the deadline then performs the fail-closed invalidation.
            assert "self._mark_recalculation_pending()" not in pending_prefix
        assert "return False" in pending_prefix

    tariff_tree = ast.parse(tariff_source)
    forecast_imports = {
        alias.name
        for node in tariff_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "forecast_model"
        for alias in node.names
    }
    assert "ForecastLearningPolicy" in forecast_imports


def test_house_energy_model_applies_charge_and_discharge_losses() -> None:
    """PV surplus and battery-fed LOAD must use their own efficiencies."""
    start = NOW.replace(hour=12)
    settings = base_input(
        battery_soc_percent=50.0,
        charge_efficiency_percent=80.0,
        house_discharge_efficiency_percent=80.0,
        pv_by_slot_kwh={start: 4.0},
    )
    feasible, ending, _ = RCE._simulate(
        [start],
        settings,
        {start: 2.0},
        {},
        0.0,
    )
    assert feasible
    assert abs(ending - 11.6) < 1e-6
    settings = replace(settings, pv_by_slot_kwh={})
    feasible, ending, _ = RCE._simulate(
        [start],
        settings,
        {start: 2.0},
        {},
        0.0,
    )
    assert feasible
    assert abs(ending - 7.5) < 1e-6


def test_load_and_grid_share_the_same_bms_discharge_budget() -> None:
    """House LOAD must reduce the BMS power left for grid export."""
    profile = [0.0] * 48
    profile[36] = 2.0  # 18:00-18:30 household energy
    candidate = RCE.PriceSlot(NOW.replace(hour=18), 2.0)
    result = RCE.optimize_rce(
        base_input(
            price_slots=[candidate],
            battery_capacity_kwh=100.0,
            outage_reserve_soc_percent=0.0,
            average_daily_load_kwh=2.0,
            average_night_load_kwh=0.0,
            weekday_load_profile_30m_kwh=tuple(profile),
            weekend_load_profile_30m_kwh=tuple(profile),
            bms_max_discharge_current_a=100.0,
            battery_voltage_v=50.0,
            bms_power_safety_percent=100.0,
            battery_wear_cost_pln_kwh=0.0,
        )
    )
    assert abs(result.maximum_export_power_kw - 5.0) < 1e-6
    assert result.planned_export_kwh <= 0.5 + 0.02
    assert result.planned_export_kwh + 2.0 <= 2.5 + 0.02


def test_current_slot_uses_only_real_remaining_fraction_and_live_power() -> None:
    """At 18:20 a 5 kW export cannot be planned as a full 30-minute block."""
    now = NOW.replace(hour=18, minute=20)
    result = RCE.optimize_rce(
        base_input(
            now=now,
            price_slots=[RCE.PriceSlot(now.replace(minute=0), 2.0)],
            battery_capacity_kwh=100.0,
            outage_reserve_soc_percent=0.0,
            discharge_power_percent=50.0,
            current_load_power_kw=0.0,
            current_pv_power_kw=0.0,
            current_battery_soc_fresh=True,
            battery_wear_cost_pln_kwh=0.0,
        )
    )
    assert abs(result.current_slot_fraction - 1.0 / 3.0) < 1e-9
    assert abs(result.current_slot_remaining_minutes - 10.0) < 1e-9
    assert result.current_slot_planned_export_kwh <= 5.0 / 6.0 + 0.01
    assert result.current_slot_start_eligible
    assert result.current_slot_suppression_reason == "eligible"
    assert result.current_slot_load_source == "live"
    assert result.current_slot_pv_source == "live"


def test_partial_slot_plan_exposes_energy_bounded_execution_power() -> None:
    """Scheduler power must reproduce partial kWh, including live LOAD."""
    now = NOW.replace(hour=18, minute=10)
    result = RCE.optimize_rce(
        base_input(
            now=now,
            price_slots=[RCE.PriceSlot(now.replace(minute=0), 2.0)],
            battery_capacity_kwh=10.0,
            battery_soc_percent=30.0,
            dynamic_reserve_enabled=False,
            manual_minimum_soc_percent=20.0,
            outage_reserve_soc_percent=0.0,
            inverter_power_kw=10.0,
            inverter_count=1,
            current_load_power_kw=1.0,
            current_pv_power_kw=0.0,
            current_battery_soc_fresh=True,
            charge_efficiency_percent=100.0,
            house_discharge_efficiency_percent=100.0,
            export_efficiency_percent=100.0,
            battery_wear_cost_pln_kwh=0.0,
        )
    )
    hours = result.current_slot_remaining_minutes / 60.0
    assert result.current_slot_start_eligible
    assert 0.65 < result.current_slot_planned_export_kwh < 0.68
    assert abs(
        result.current_slot_execution_export_power_kw * hours
        - result.current_slot_planned_export_kwh
    ) < 1e-6
    assert abs(result.current_slot_execution_discharge_power_kw - 3.0) < 0.02
    assert abs(result.current_slot_execution_power_percent - 30.0) < 0.2
    delivered_export = max(
        result.current_slot_execution_discharge_power_kw
        - max(
            result.current_slot_load_kwh - result.current_slot_pv_kwh,
            0.0,
        )
        / hours,
        0.0,
    ) * hours
    assert delivered_export <= result.current_slot_planned_export_kwh + 1e-6


def test_current_slot_start_fails_closed_without_fresh_live_inputs() -> None:
    """A preview may exist, but scheduler readiness must fail closed."""
    now = NOW.replace(hour=18, minute=10)
    result = RCE.optimize_rce(
        base_input(
            now=now,
            price_slots=[RCE.PriceSlot(now.replace(minute=0), 2.0)],
            battery_capacity_kwh=100.0,
            outage_reserve_soc_percent=0.0,
            battery_wear_cost_pln_kwh=0.0,
        )
    )
    assert result.current_slot_planned_export_kwh > 0.0
    assert not result.current_slot_start_eligible
    assert result.current_slot_suppression_reason == "live_data_missing"


def test_bms_discharge_limit_fails_closed_on_invalid_freshness() -> None:
    """Missing, stale, future and exact-zero BMS limits cannot start RCE."""
    now = NOW.replace(hour=18, minute=5)

    def calculate(**changes):
        return RCE.optimize_rce(
            base_input(
                now=now,
                price_slots=[RCE.PriceSlot(now.replace(minute=0), 2.0)],
                battery_capacity_kwh=100.0,
                outage_reserve_soc_percent=0.0,
                current_load_power_kw=0.0,
                current_pv_power_kw=0.0,
                current_battery_soc_fresh=True,
                battery_wear_cost_pln_kwh=0.0,
                **changes,
            )
        )

    missing = calculate(
        bms_max_discharge_current_a=None,
        battery_voltage_v=None,
        bms_discharge_data_fresh=False,
        bms_discharge_data_age_seconds=None,
        bms_discharge_data_available=False,
    )
    stale = calculate(
        bms_max_discharge_current_a=None,
        battery_voltage_v=None,
        bms_discharge_data_fresh=False,
        bms_discharge_data_age_seconds=300.001,
        bms_discharge_data_available=False,
    )
    future = calculate(
        bms_max_discharge_current_a=None,
        battery_voltage_v=None,
        bms_discharge_data_fresh=False,
        bms_discharge_data_age_seconds=-5.001,
        bms_discharge_data_available=False,
    )
    exact_zero = calculate(
        bms_max_discharge_current_a=0.0,
        battery_voltage_v=50.0,
        bms_discharge_data_fresh=True,
        bms_discharge_data_age_seconds=0.0,
        bms_discharge_data_available=False,
    )
    for result in (missing, stale, future, exact_zero):
        assert not result.current_slot_start_eligible
        assert result.bms_discharge_power_limit_kw == 0.0
        assert result.maximum_export_power_kw == 0.0
        assert not result.planned_exports
    assert missing.current_slot_suppression_reason == (
        "bms_discharge_data_unavailable"
    )
    assert stale.current_slot_suppression_reason == "bms_discharge_data_stale"
    assert future.current_slot_suppression_reason == "bms_discharge_data_stale"
    assert exact_zero.current_slot_suppression_reason == (
        "bms_discharge_limit_zero"
    )
    assert exact_zero.bms_discharge_data_fresh
    assert not exact_zero.bms_discharge_data_available

    fresh = calculate(
        bms_max_discharge_current_a=100.0,
        battery_voltage_v=50.0,
        bms_power_safety_percent=100.0,
        bms_discharge_data_fresh=True,
        bms_discharge_data_age_seconds=300.0,
        bms_discharge_data_available=True,
    )
    future_boundary = calculate(
        bms_max_discharge_current_a=100.0,
        battery_voltage_v=50.0,
        bms_power_safety_percent=100.0,
        bms_discharge_data_fresh=True,
        bms_discharge_data_age_seconds=-5.0,
        bms_discharge_data_available=True,
    )
    for result in (fresh, future_boundary):
        assert result.bms_discharge_data_fresh
        assert result.bms_discharge_data_available
        assert result.bms_discharge_power_limit_kw == 5.0
        assert result.current_slot_start_eligible
        assert result.current_slot_suppression_reason == "eligible"


def test_bms_charge_limit_fails_closed_and_caps_future_refill() -> None:
    """Only a fresh positive BMS charge limit may finance later export."""
    now = NOW.replace(hour=12, minute=0)
    sale = RCE.PriceSlot(now + timedelta(minutes=30), 2.0)
    pv = {now: 10.0}

    def calculate(**changes):
        return RCE.optimize_rce(
            base_input(
                now=now,
                price_slots=[sale],
                pv_by_slot_kwh=pv,
                conservative_pv_by_slot_kwh=pv,
                battery_capacity_kwh=10.0,
                battery_soc_percent=20.0,
                dynamic_reserve_enabled=False,
                manual_minimum_soc_percent=20.0,
                outage_reserve_soc_percent=0.0,
                inverter_power_kw=10.0,
                battery_voltage_v=50.0,
                bms_power_safety_percent=100.0,
                charge_efficiency_percent=100.0,
                export_efficiency_percent=100.0,
                battery_wear_cost_pln_kwh=0.0,
                **changes,
            )
        )

    limited = calculate(
        bms_max_charge_current_a=10.0,
        bms_charge_data_fresh=True,
        bms_charge_data_age_seconds=300.0,
        bms_charge_data_available=True,
    )
    assert abs(limited.bms_charge_power_limit_kw - 0.5) < 1e-9
    assert 0.24 < limited.planned_export_kwh < 0.26

    cases = (
        calculate(
            bms_max_charge_current_a=None,
            bms_charge_data_fresh=False,
            bms_charge_data_age_seconds=None,
            bms_charge_data_available=False,
        ),
        calculate(
            bms_max_charge_current_a=10.0,
            bms_charge_data_fresh=False,
            bms_charge_data_age_seconds=300.001,
            bms_charge_data_available=False,
        ),
        calculate(
            bms_max_charge_current_a=10.0,
            bms_charge_data_fresh=False,
            bms_charge_data_age_seconds=-5.001,
            bms_charge_data_available=False,
        ),
        calculate(
            bms_max_charge_current_a=0.0,
            bms_charge_data_fresh=True,
            bms_charge_data_age_seconds=0.0,
            bms_charge_data_available=False,
        ),
    )
    for result in cases:
        assert result.bms_charge_power_limit_kw == 0.0
        assert not result.bms_charge_data_available
        assert result.planned_export_kwh == 0.0

    future_boundary = calculate(
        bms_max_charge_current_a=10.0,
        bms_charge_data_fresh=True,
        bms_charge_data_age_seconds=-5.0,
        bms_charge_data_available=True,
    )
    assert future_boundary.bms_charge_data_fresh
    assert future_boundary.bms_charge_data_available
    assert abs(future_boundary.bms_charge_power_limit_kw - 0.5) < 1e-9
    assert 0.24 < future_boundary.planned_export_kwh < 0.26


def test_pv_charge_and_both_export_paths_share_one_ac_bridge() -> None:
    """LOAD, charging, natural export and controlled export cannot overlap."""
    start = NOW.replace(hour=12)
    settings = base_input(
        now=start,
        battery_capacity_kwh=10.0,
        battery_soc_percent=50.0,
        inverter_power_kw=2.0,
        inverter_count=1,
        bms_max_charge_current_a=10.0,
        battery_voltage_v=50.0,
        bms_power_safety_percent=100.0,
        charge_efficiency_percent=100.0,
        export_efficiency_percent=100.0,
    )
    safe, ending, natural = RCE._simulate(
        [start],
        settings,
        {start: 0.2},
        {start: 0.3},
        0.0,
        {start: 0.0},
        {start: 3.0},
        {start: 1.0},
    )
    assert safe
    # One half-hour supplies exactly 1.0 kWh of AC conversion:
    # 0.2 LOAD + 0.25 battery charge + 0.3 controlled + 0.25 natural.
    assert abs(ending - 4.95) < 1e-9
    assert abs(natural[start] - 0.25) < 1e-9
    assert abs(0.2 + 0.25 + 0.3 + natural[start] - 1.0) < 1e-9


def test_signed_age_contract_is_shared_with_rce_sensor() -> None:
    """RCE uses the common -5..300 s BMS rule and signed SOC freshness."""
    assert ENERGY_DATA.numeric_sample_is_fresh(50.0, -5.0, 300.0)
    assert ENERGY_DATA.numeric_sample_is_fresh(50.0, 300.0, 300.0)
    assert not ENERGY_DATA.numeric_sample_is_fresh(50.0, -5.001, 300.0)
    assert not ENERGY_DATA.numeric_sample_is_fresh(50.0, 300.001, 300.0)
    sensor_source = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    assert 'max_age_seconds=300.0' in sensor_source
    assert '"bms_discharge_data_fresh"' in sensor_source
    assert '"bms_discharge_data_age_seconds"' in sensor_source
    assert '"bms_discharge_data_available"' in sensor_source
    assert 'sensor.hoymiles_hit_maximum_charge_current' in sensor_source
    assert '"bms_charge_data_fresh"' in sensor_source
    assert '"bms_charge_data_age_seconds"' in sensor_source
    assert '"bms_charge_data_available"' in sensor_source
    assert '"bms_charge_power_limit_kw"' in sensor_source
    assert 'metadata["current_price_pln_kwh"]' in sensor_source
    assert "slot.start.astimezone(dt_util.UTC) == current_slot_utc" in sensor_source
    assert "current_battery_soc_fresh=_age_minutes_is_fresh(" in sensor_source
    assert '"soc_data_fresh": battery_soc_sample.fresh' in sensor_source
    assert '"soc_data_age_seconds"' in sensor_source
    assert '"forecast_remaining_today_data_fresh"' in sensor_source
    assert '"forecast_minus_actual_fallback"' in sensor_source
    for marker in (
        "def _complete_rce_half_hours_for_local_date(",
        "actual == expected",
        "today_rows_complete",
        "tomorrow_rows_structurally_complete",
        '"rce_today_expected_half_hours"',
        '"rce_tomorrow_expected_half_hours"',
        '"gcf_execution_data_fresh"',
        '"Generation Control Function"',
        "gcf_limit_sample = numeric_state_sample(",
    ):
        assert marker in sensor_source, f"RCE day completeness lacks {marker}"
    assert "_MIN_COMPLETE_RCE_DAY_PERIODS" not in sensor_source


def test_rce_sensor_dates_dtime_only_rows_by_quarter_start() -> None:
    """The no-business-date fallback retains PSE's final 24:00 row."""
    sensor_source = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    assert 'raw_interval_end = item.get("dtime_utc")' in sensor_source
    assert (
        'item.get("dtime_utc") or item.get("period_utc")'
        not in sensor_source
    )
    assert (
        "interval_end = interval_end.replace(tzinfo=dt_util.UTC)"
        in sensor_source
    )
    assert (
        "interval_end.astimezone(dt_util.UTC)\n"
        "                    - timedelta(minutes=15)"
        in sensor_source
    )
    assert "if quarter_start.date() == target_date:" in sensor_source


def test_current_run_end_covers_the_whole_consecutive_export_window() -> None:
    """The scheduler latch must cover adjacent planned half-hour slots."""
    now = NOW.replace(hour=18, minute=5)
    result = RCE.optimize_rce(
        base_input(
            now=now,
            price_slots=[
                RCE.PriceSlot(now.replace(minute=0), 2.0),
                RCE.PriceSlot(now.replace(minute=30), 2.0),
            ],
            battery_capacity_kwh=100.0,
            outage_reserve_soc_percent=0.0,
            current_load_power_kw=0.0,
            current_pv_power_kw=0.0,
            current_battery_soc_fresh=True,
            battery_wear_cost_pln_kwh=0.0,
        )
    )
    assert len(result.planned_exports) == 2
    assert result.current_slot_end == now.replace(minute=30)
    assert result.current_run_end == now.replace(hour=19, minute=0)
    assert result.current_run_end > result.current_slot_end


def test_current_run_end_steps_across_autumn_dst_in_utc() -> None:
    """A repeated 02:xx hour must extend, not collapse, the accepted run."""
    utc = ZoneInfo("UTC")
    now = datetime(2026, 10, 25, 1, 40, tzinfo=WARSAW)
    first = now.replace(minute=30).astimezone(utc)
    price_slots = [
        RCE.PriceSlot(
            (first + timedelta(minutes=30 * index)).astimezone(WARSAW),
            2.0,
        )
        for index in range(6)
    ]
    result = RCE.optimize_rce(
        base_input(
            now=now,
            price_slots=price_slots,
            battery_capacity_kwh=100.0,
            outage_reserve_soc_percent=0.0,
            current_load_power_kw=0.0,
            current_pv_power_kw=0.0,
            current_battery_soc_fresh=True,
            battery_wear_cost_pln_kwh=0.0,
        )
    )
    assert len(result.planned_exports) == 6
    assert result.current_run_end is not None
    assert result.current_run_end == datetime(
        2026, 10, 25, 3, 30, tzinfo=WARSAW
    )
    assert (
        result.current_run_end.astimezone(utc) - first
    ) == timedelta(hours=3)


def test_p90_load_is_an_alternative_scenario_not_double_counted() -> None:
    """Tariff P90 remains diagnostic and cannot suppress RCE revenue."""
    price = slots(0, 18, 8, 2.0)
    expected = RCE.optimize_rce(
        base_input(
            price_slots=price,
            battery_capacity_kwh=100.0,
            average_daily_load_kwh=20.0,
            average_night_load_kwh=8.0,
            battery_wear_cost_pln_kwh=0.0,
        )
    )
    conservative = RCE.optimize_rce(
        base_input(
            price_slots=price,
            battery_capacity_kwh=100.0,
            average_daily_load_kwh=20.0,
            average_night_load_kwh=8.0,
            conservative_daily_load_kwh=27.0,
            conservative_night_load_kwh=10.8,
            load_history_days=28,
            battery_wear_cost_pln_kwh=0.0,
        )
    )
    assert conservative.load_risk_multiplier == 1.35
    assert conservative.load_risk_buffer_kwh > 0.0
    assert conservative.load_risk_mode == "diagnostic_only"
    assert abs(
        conservative.planned_export_kwh - expected.planned_export_kwh
    ) < 0.02
    assert abs(
        conservative.protected_home_energy_kwh
        - expected.protected_home_energy_kwh
    ) < 1e-6


def test_meter_scale_high_pv_forecast_stays_revenue_first() -> None:
    """The live-scale 230 kWh site keeps its exact hard reserve and export."""
    now = NOW.replace(hour=8)
    market = [
        RCE.PriceSlot(
            now.replace(hour=16) + timedelta(minutes=30 * index),
            1.20 if index < 4 else 0.75,
        )
        for index in range(32)
    ]
    pv: dict[datetime, float] = {}
    for day_offset, energy in ((0, 100.0), (1, 67.0)):
        day_start = now.replace(hour=10) + timedelta(days=day_offset)
        for index in range(12):
            pv[day_start + timedelta(minutes=30 * index)] = energy / 12.0
    result = RCE.optimize_rce(
        base_input(
            now=now,
            price_slots=market,
            pv_by_slot_kwh=pv,
            conservative_pv_by_slot_kwh=pv,
            battery_capacity_kwh=230.0,
            battery_soc_percent=58.0,
            outage_reserve_soc_percent=11.5,
            safety_margin_soc_percent=2.0,
            average_daily_load_kwh=37.4,
            average_night_load_kwh=11.4,
            conservative_daily_load_kwh=50.49,
            conservative_night_load_kwh=15.39,
            load_history_days=28,
            inverter_power_kw=10.0,
            inverter_count=2,
            discharge_power_percent=100.0,
            export_efficiency_percent=95.0,
            critical_zero_pv_guard=True,
            critical_zero_pv_guard_reason="p10_missing",
            avoided_import_price_pln_kwh=1.0,
            day3_pv_forecast_kwh=0.0,
            battery_wear_cost_pln_kwh=0.08,
        )
    )
    assert result.ready
    assert not result.critical_zero_pv_guard_active
    assert result.critical_zero_pv_guard_reason == "risk_not_energy_critical"
    assert result.load_risk_mode == "diagnostic_only"
    assert abs(result.base_reserve_energy_kwh - 31.05) < 1e-6
    assert abs(result.protected_night_energy_kwh - 11.4) < 1e-6
    assert result.minimum_soc_percent == 19
    assert abs(result.control_reserve_energy_kwh - 43.7) < 1e-6
    assert not result.terminal_energy_value_applied_to_objective
    assert result.planned_export_kwh > 100.0
    assert result.automatic_price_floor_pln_kwh == 0.75


def test_critical_zero_pv_guard_blocks_optimistic_missing_p10() -> None:
    """Missing/high-risk P10 must not finance export from an optimistic P50."""
    noon = NOW.replace(hour=12)
    result = RCE.optimize_rce(
        base_input(
            now=NOW.replace(hour=8),
            price_slots=slots(0, 18, 4, 2.0),
            battery_soc_percent=20.0,
            average_daily_load_kwh=8.0,
            average_night_load_kwh=4.0,
            pv_by_slot_kwh={noon: 20.0},
            critical_zero_pv_guard=True,
            critical_zero_pv_guard_reason="p10_missing",
        )
    )
    assert result.critical_zero_pv_guard_active
    assert result.critical_zero_pv_guard_reason == "p10_missing"
    assert result.critical_zero_pv_guarded_kwh >= 19.9
    assert result.critical_zero_pv_guard_until is not None
    assert not result.ready
    assert not result.planned_exports


def test_joint_solver_beats_greedy_headroom_counterexample() -> None:
    """Joint exports move a later capped PV spill from -1.00 to 1.40."""
    start = NOW.replace(hour=12)
    market = [
        RCE.PriceSlot(start + timedelta(minutes=30 * index), price)
        for index, price in enumerate((1.40, 1.40, 1.40, -1.00))
    ]
    pv = {
        slot.start: energy
        for slot, energy in zip(market, (1.0, 2.0, 2.0, 2.0), strict=True)
    }
    result = RCE.optimize_rce(
        base_input(
            now=start,
            price_slots=market,
            pv_by_slot_kwh=pv,
            conservative_pv_by_slot_kwh=pv,
            battery_capacity_kwh=4.0,
            battery_soc_percent=25.0,
            dynamic_reserve_enabled=False,
            manual_minimum_soc_percent=0.0,
            outage_reserve_soc_percent=0.0,
            inverter_power_kw=2.0,
            battery_wear_cost_pln_kwh=0.08,
        )
    )
    # With the shared 1 kWh AC bridge, exporting in the first and third slots
    # frees enough headroom to avoid the later negative-price natural export.
    assert result.planned_export_kwh > 1.99
    assert result.natural_export_kwh < 0.01
    assert abs(result.net_objective_pln - 2.64) < 0.02
    assert result.net_optimization_gain_pln > 3.63


def test_seven_slot_middle_price_threshold_is_retained() -> None:
    """A profitable 0.80 band must survive between 1.40 and negative prices."""
    start = NOW.replace(hour=10)
    starts = [start + timedelta(minutes=30 * index) for index in range(7)]
    price_values = (0.8, 0.8, -1.0, 1.4, 0.5, -0.4, -0.4)
    conservative_values = (1.5, 0.5, 0.0, 1.5, 2.0, 0.5, 2.0)
    expected_values = (1.5, 1.0, 1.0, 1.5, 2.0, 1.0, 2.0)
    load_values = (0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.0)
    prices = dict(zip(starts, price_values, strict=True))
    conservative_pv = dict(zip(starts, conservative_values, strict=True))
    expected_pv = dict(zip(starts, expected_values, strict=True))
    load = dict(zip(starts, load_values, strict=True))
    settings = base_input(
        now=start,
        battery_capacity_kwh=3.0,
        battery_soc_percent=50.0,
        dynamic_reserve_enabled=False,
        manual_minimum_soc_percent=0.0,
        outage_reserve_soc_percent=0.0,
        inverter_power_kw=2.0,
        battery_wear_cost_pln_kwh=0.08,
    )
    reserve = {slot: 0.0 for slot in starts}
    _, baseline_end, baseline_natural = RCE._simulate(
        starts,
        settings,
        load,
        {},
        0.0,
        reserve,
        expected_pv,
    )
    baseline_objective = RCE._economic_objective(
        exports={},
        natural_exports=baseline_natural,
        price_by_start=prices,
        ending_battery_kwh=baseline_end,
        floor_kwh=0.0,
        export_efficiency=1.0,
        battery_wear_cost_pln_kwh=0.08,
        terminal_energy_target_kwh=0.0,
        terminal_energy_value_pln_kwh=0.0,
    )[0]
    plan = RCE._solve_joint_horizon_exports(
        starts=starts,
        settings=settings,
        candidates=[
            (RCE.PriceSlot(slot, prices[slot]), slot) for slot in starts
        ],
        load_by_slot=load,
        floor_kwh=0.0,
        export_reserve_by_slot=reserve,
        conservative_pv=conservative_pv,
        expected_pv=expected_pv,
        slot_fractions={slot: 1.0 for slot in starts},
        price_by_start=prices,
        baseline_objective=baseline_objective,
        export_efficiency=1.0,
        terminal_energy_target=0.0,
        terminal_unit_value=0.0,
    )
    safe, value = _independent_short_simulation(
        starts=starts,
        settings=settings,
        load_by_slot=load,
        pv_by_slot=conservative_pv,
        exports=plan,
        floor_kwh=0.0,
        reserve_by_slot=reserve,
        prices=prices,
    )
    expected_safe, expected_value = _independent_short_simulation(
        starts=starts,
        settings=settings,
        load_by_slot=load,
        pv_by_slot=expected_pv,
        exports=plan,
        floor_kwh=0.0,
        reserve_by_slot=reserve,
        prices=prices,
    )
    assert safe and expected_safe
    oracle_value, _ = _independent_grid_oracle(
        starts=starts,
        settings=settings,
        load_by_slot=load,
        conservative_pv=conservative_pv,
        expected_pv=expected_pv,
        floor_kwh=0.0,
        reserve_by_slot=reserve,
        prices=prices,
    )
    assert abs(baseline_objective - 1.0) < 1e-9
    assert expected_value >= oracle_value - 0.02, (
        expected_value,
        oracle_value,
        plan,
    )
    assert 0.8 in {prices[slot] for slot in plan}


def test_large_candidate_set_drains_the_complete_available_budget() -> None:
    """Eleven slots must not strand energy at a coarse bisection boundary."""
    start = NOW.replace(hour=12)
    market = [
        RCE.PriceSlot(
            start + timedelta(minutes=30 * index),
            2.0 - index * 0.05,
        )
        for index in range(11)
    ]
    result = RCE.optimize_rce(
        base_input(
            now=start,
            price_slots=market,
            battery_capacity_kwh=100.0,
            battery_soc_percent=43.0,
            dynamic_reserve_enabled=False,
            manual_minimum_soc_percent=20.0,
            outage_reserve_soc_percent=0.0,
            inverter_power_kw=100.0,
            discharge_power_percent=100.0,
            bms_max_discharge_current_a=5_000.0,
            battery_voltage_v=50.0,
            bms_power_safety_percent=100.0,
            battery_wear_cost_pln_kwh=0.0,
        )
    )
    assert result.ready
    assert abs(result.available_energy_now_kwh - 23.0) < 1e-9
    assert abs(result.planned_export_kwh - 23.0) < 0.011
    assert abs(result.planned_revenue_pln - 46.0) < 0.022
    assert len(result.planned_exports) == 1
    assert result.planned_exports[0].start == market[0].start


def test_neutral_slots_do_not_disable_partial_headroom_refinement() -> None:
    """Adding negative-price rows cannot force a below-wear full discharge."""
    start = NOW.replace(hour=12)
    all_market = [
        RCE.PriceSlot(
            start + timedelta(minutes=30 * index),
            0.05 if index == 0 else -1.0,
        )
        for index in range(11)
    ]
    pv = {all_market[4].start: 2.0}

    def calculate(market):
        return RCE.optimize_rce(
            base_input(
                now=start,
                price_slots=market,
                pv_by_slot_kwh=pv,
                conservative_pv_by_slot_kwh=pv,
                battery_capacity_kwh=20.0,
                battery_soc_percent=100.0,
                dynamic_reserve_enabled=False,
                manual_minimum_soc_percent=0.0,
                outage_reserve_soc_percent=0.0,
                inverter_power_kw=10.0,
                inverter_count=1,
                charge_efficiency_percent=100.0,
                export_efficiency_percent=100.0,
                battery_wear_cost_pln_kwh=0.08,
            )
        )

    short = calculate([all_market[0], all_market[4]])
    long = calculate(all_market)
    assert 1.98 < short.planned_export_kwh < 2.03
    assert abs(long.planned_export_kwh - short.planned_export_kwh) < 0.03
    assert abs(long.net_optimization_gain_pln - 1.94) < 0.03
    assert abs(
        long.net_optimization_gain_pln - short.net_optimization_gain_pln
    ) < 0.03


def test_solver_matches_independent_random_oracle() -> None:
    """120 random 4--7 slot plans match an independent exhaustive oracle."""
    rng = random.Random(20260813)
    worst_absolute_gap = 0.0
    worst_relative_gap = 0.0
    absolute_gaps: list[float] = []
    for case_index in range(120):
        slot_count = 4 + case_index % 4
        start = NOW.replace(hour=10) + timedelta(days=case_index)
        starts = [start + timedelta(minutes=30 * index) for index in range(slot_count)]
        prices = {
            slot: rng.choice((-0.40, 0.10, 0.80, 1.40))
            for slot in starts
        }
        conservative_pv = {
            slot: rng.choice((0.0, 0.5, 1.0, 1.5))
            for slot in starts
        }
        expected_pv = {
            slot: conservative_pv[slot] + rng.choice((0.0, 0.5))
            for slot in starts
        }
        load = {
            slot: rng.choice((0.0, 0.5))
            for slot in starts
        }
        settings = base_input(
            now=start,
            battery_capacity_kwh=4.0,
            battery_soc_percent=rng.choice((37.5, 50.0, 62.5)),
            dynamic_reserve_enabled=False,
            manual_minimum_soc_percent=12.5,
            outage_reserve_soc_percent=0.0,
            inverter_power_kw=2.0,
            battery_wear_cost_pln_kwh=0.08,
            charge_efficiency_percent=100.0,
            house_discharge_efficiency_percent=100.0,
            price_slots=[
                RCE.PriceSlot(slot, prices[slot]) for slot in starts
            ],
        )
        floor = 0.5
        reserve = {slot: floor for slot in starts}
        oracle_value, _ = _independent_grid_oracle(
            starts=starts,
            settings=settings,
            load_by_slot=load,
            conservative_pv=conservative_pv,
            expected_pv=expected_pv,
            floor_kwh=floor,
            reserve_by_slot=reserve,
            prices=prices,
        )
        _, baseline_end, baseline_natural = RCE._simulate(
            starts,
            settings,
            load,
            {},
            floor,
            reserve,
            expected_pv,
        )
        baseline_objective = RCE._economic_objective(
            exports={},
            natural_exports=baseline_natural,
            price_by_start=prices,
            ending_battery_kwh=baseline_end,
            floor_kwh=floor,
            export_efficiency=1.0,
            battery_wear_cost_pln_kwh=0.08,
            terminal_energy_target_kwh=0.0,
            terminal_energy_value_pln_kwh=0.0,
        )[0]
        plan = RCE._solve_joint_horizon_exports(
            starts=starts,
            settings=settings,
            candidates=[
                (RCE.PriceSlot(slot, prices[slot]), slot) for slot in starts
            ],
            load_by_slot=load,
            floor_kwh=floor,
            export_reserve_by_slot=reserve,
            conservative_pv=conservative_pv,
            expected_pv=expected_pv,
            slot_fractions={slot: 1.0 for slot in starts},
            price_by_start=prices,
            baseline_objective=baseline_objective,
            export_efficiency=1.0,
            terminal_energy_target=0.0,
            terminal_unit_value=0.0,
        )
        conservative_safe, _ = _independent_short_simulation(
            starts=starts,
            settings=settings,
            load_by_slot=load,
            pv_by_slot=conservative_pv,
            exports=plan,
            floor_kwh=floor,
            reserve_by_slot=reserve,
            prices=prices,
        )
        expected_safe, solver_value = _independent_short_simulation(
            starts=starts,
            settings=settings,
            load_by_slot=load,
            pv_by_slot=expected_pv,
            exports=plan,
            floor_kwh=floor,
            reserve_by_slot=reserve,
            prices=prices,
        )
        assert conservative_safe and expected_safe
        # The independent exhaustive grid is a certified feasible lower bound;
        # the continuous bounded solver is allowed to improve between grid
        # points.  Only actual suboptimality counts as a gap.
        absolute_gap = max(oracle_value - solver_value, 0.0)
        absolute_gaps.append(absolute_gap)
        relative_gap = absolute_gap / max(abs(oracle_value), 1.0)
        worst_absolute_gap = max(worst_absolute_gap, absolute_gap)
        worst_relative_gap = max(worst_relative_gap, relative_gap)
        assert absolute_gap < 0.025, (
            case_index,
            solver_value,
            oracle_value,
            plan,
        )
    assert worst_absolute_gap < 0.025, worst_absolute_gap
    assert worst_relative_gap < 0.003, (
        worst_absolute_gap,
        worst_relative_gap,
    )
    median_gap = sorted(absolute_gaps)[len(absolute_gaps) // 2]
    assert median_gap < 0.001


def test_missing_quarter_is_not_paired_with_the_next_half_hour() -> None:
    """A missing 00:30 row drops that block instead of shifting all pairs."""
    rows = [
        {"business_date": "2026-07-28", "period": clock, "rce_pln": value}
        for clock, value in (
            ("00:00", 100.0),
            ("00:15", 200.0),
            ("00:45", 900.0),
            ("01:00", 300.0),
            ("01:15", 500.0),
        )
    ]
    parsed = RCE.parse_rce_rows(
        rows,
        WARSAW,
        block_enabled=False,
        block_start_minute=0,
        block_end_minute=0,
    )
    assert [item.start.strftime("%H:%M") for item in parsed] == ["00:00", "01:00"]
    assert abs(parsed[0].price_pln_kwh - 0.15) < 1e-12
    assert abs(parsed[1].price_pln_kwh - 0.4) < 1e-12


def test_rce_row_parser_is_independent_of_payload_order() -> None:
    """Forward, reversed and shuffled OData rows produce identical slots."""
    rows = [
        {
            "business_date": "2026-07-28",
            "period": f"{hour:02d}:{minute:02d}",
            "rce_pln": 100.0 + hour * 100.0 + minute,
        }
        for hour in range(2)
        for minute in (0, 15, 30, 45)
    ]

    def parsed(payload):
        return [
            (
                item.start.astimezone(ZoneInfo("UTC")),
                item.price_pln_kwh,
            )
            for item in RCE.parse_rce_rows(
                payload,
                WARSAW,
                block_enabled=False,
                block_start_minute=0,
                block_end_minute=0,
            )
        ]

    forward = parsed(rows)
    reversed_rows = parsed(list(reversed(rows)))
    shuffled_rows = list(rows)
    random.Random(42).shuffle(shuffled_rows)
    shuffled = parsed(shuffled_rows)
    swapped_rows = list(rows)
    swapped_rows[1], swapped_rows[2] = swapped_rows[2], swapped_rows[1]
    swapped = parsed(swapped_rows)
    assert len(forward) == 4
    assert reversed_rows == forward
    assert shuffled == forward
    assert swapped == forward


def test_autumn_duplicate_rows_split_folds_deterministically() -> None:
    """Ambiguous local-only folds retain both instants at the safe price."""
    rows = []
    for repeat, base in (("a", 100.0), ("b", 500.0)):
        rows.extend(
            {
                "business_date": "2026-10-25",
                "period": f"02:{minute:02d}",
                "rce_pln": base + minute,
                "source_id": repeat,
            }
            for minute in (0, 15, 30, 45)
        )

    def parsed(payload):
        return [
            (
                item.start.astimezone(ZoneInfo("UTC")),
                item.price_pln_kwh,
            )
            for item in RCE.parse_rce_rows(
                payload,
                WARSAW,
                block_enabled=False,
                block_start_minute=0,
                block_end_minute=0,
            )
        ]

    forward = parsed(rows)
    reversed_rows = parsed(list(reversed(rows)))
    shuffled_rows = list(rows)
    random.Random(99).shuffle(shuffled_rows)
    assert len(forward) == 4
    assert forward == reversed_rows == parsed(shuffled_rows)
    absolute = [start for start, _ in forward]
    assert absolute == sorted(absolute)
    assert len(set(absolute)) == 4
    # Which real fold owns 100 vs 500 cannot be recovered without dtime_utc.
    # Both real half-hours therefore use the lower pair, independently of how
    # the source happened to order the two physical folds.
    assert [round(price, 6) for _, price in forward] == [
        0.1075,
        0.1375,
        0.1075,
        0.1375,
    ]


def test_local_only_dst_fold_prices_cannot_be_swapped_into_false_profit() -> None:
    """Swapped real-fold provenance has the same conservative fallback."""
    def payload(first_base: float, second_base: float) -> list[dict[str, object]]:
        return [
            {
                "business_date": "2026-10-25",
                "period": f"02:{minute:02d}",
                "rce_pln": base + minute,
                "source_id": source,
            }
            for source, base in (("first", first_base), ("second", second_base))
            for minute in (0, 15, 30, 45)
        ]

    def parsed(rows: list[dict[str, object]]) -> list[tuple[datetime, float]]:
        return [
            (item.start.astimezone(ZoneInfo("UTC")), item.price_pln_kwh)
            for item in RCE.parse_rce_rows(
                rows,
                WARSAW,
                block_enabled=False,
                block_start_minute=0,
                block_end_minute=0,
            )
        ]

    low_then_high = parsed(payload(100.0, 10_000.0))
    high_then_low = parsed(payload(10_000.0, 100.0))
    assert low_then_high == high_then_low
    assert len(low_then_high) == 4
    assert max(price for _, price in low_then_high) < 0.15

    # One local record per ambiguous wall-clock quarter cannot identify a
    # fold, so the incomplete hour is discarded instead of guessing.
    incomplete = payload(100.0, 10_000.0)[::2]
    assert parsed(incomplete) == []


def _official_pse_rows_for_local_day(
    business_day: datetime,
) -> list[dict[str, object]]:
    """Build authoritative PSE UTC fields for one Warsaw market day.

    The local ``period`` label is illustrative on DST transition days; the
    public feed's nonstandard ``02a``/gap labels are intentionally ignored
    whenever the authoritative absolute interval end is present.
    """
    utc = ZoneInfo("UTC")
    utc_start = business_day.astimezone(utc)
    utc_end = (business_day + timedelta(days=1)).astimezone(utc)
    quarter_count = int((utc_end - utc_start).total_seconds() // (15 * 60))
    rows: list[dict[str, object]] = []
    for quarter_index in range(quarter_count):
        interval_start = utc_start + timedelta(minutes=15 * quarter_index)
        interval_end = interval_start + timedelta(minutes=15)
        local_start = interval_start.astimezone(WARSAW)
        local_end = interval_end.astimezone(WARSAW)
        local_end_clock = (
            "24:00"
            if local_end.date() != business_day.date()
            else local_end.strftime("%H:%M")
        )
        utc_end_clock = (
            "24:00"
            if interval_end.hour == 0 and interval_end.minute == 0
            else interval_end.strftime("%H:%M")
        )
        rows.append(
            {
                "business_date": business_day.date().isoformat(),
                "period": f"{local_start:%H:%M} - {local_end_clock}",
                "period_utc": f"{interval_start:%H:%M} - {utc_end_clock}",
                "dtime_utc": interval_end.strftime("%Y-%m-%d %H:%M:%S"),
                "rce_pln": float(quarter_index),
            }
        )
    return rows


def _parsed_pse_rows(rows):
    return RCE.parse_rce_rows(
        rows,
        WARSAW,
        block_enabled=False,
        block_start_minute=0,
        block_end_minute=0,
    )


def test_live_pse_interval_end_payload_builds_complete_local_day() -> None:
    """The exact live 96-row shape yields all 48 local half-hours."""
    business_day = datetime(2026, 8, 13, tzinfo=WARSAW)
    rows = _official_pse_rows_for_local_day(business_day)
    assert len(rows) == 96
    assert rows[0] == {
        "business_date": "2026-08-13",
        "period": "00:00 - 00:15",
        "period_utc": "22:00 - 22:15",
        "dtime_utc": "2026-08-12 22:15:00",
        "rce_pln": 0.0,
    }
    assert rows[7]["period"] == "01:45 - 02:00"
    assert rows[7]["period_utc"] == "23:45 - 24:00"
    assert rows[7]["dtime_utc"] == "2026-08-13 00:00:00"
    assert rows[-1]["period"] == "23:45 - 24:00"
    assert rows[-1]["dtime_utc"] == "2026-08-13 22:00:00"
    assert rows[50]["period"] == "12:30 - 12:45"
    assert rows[51]["period"] == "12:45 - 13:00"

    expected = _parsed_pse_rows(rows)
    shuffled = list(rows)
    random.Random(813).shuffle(shuffled)
    assert expected == _parsed_pse_rows(list(reversed(rows)))
    assert expected == _parsed_pse_rows(shuffled)
    assert len(expected) == 48
    assert expected[0].start == business_day
    assert expected[-1].start == business_day.replace(hour=23, minute=30)
    assert all(slot.start.date() == business_day.date() for slot in expected)
    assert abs(expected[0].price_pln_kwh - 0.0005) < 1e-12
    assert expected[25].start == business_day.replace(hour=12, minute=30)
    assert abs(expected[25].price_pln_kwh - 0.0505) < 1e-12
    assert abs(expected[-1].price_pln_kwh - 0.0945) < 1e-12


def test_official_pse_interval_ends_cover_both_dst_day_lengths() -> None:
    """Absolute ends preserve every real spring and autumn half-hour."""
    for business_day, quarter_count, half_hour_count in (
        (datetime(2026, 3, 29, tzinfo=WARSAW), 92, 46),
        (datetime(2026, 10, 25, tzinfo=WARSAW), 100, 50),
    ):
        rows = _official_pse_rows_for_local_day(business_day)
        assert len(rows) == quarter_count
        expected = _parsed_pse_rows(rows)
        shuffled = list(rows)
        random.Random(quarter_count).shuffle(shuffled)
        assert expected == _parsed_pse_rows(list(reversed(rows)))
        assert expected == _parsed_pse_rows(shuffled)
        assert len(expected) == half_hour_count
        assert all(
            slot.start.date() == business_day.date() for slot in expected
        )
        absolute = [slot.start.astimezone(ZoneInfo("UTC")) for slot in expected]
        assert absolute == sorted(absolute)
        assert len(set(absolute)) == half_hour_count

    spring = _parsed_pse_rows(
        _official_pse_rows_for_local_day(
            datetime(2026, 3, 29, tzinfo=WARSAW)
        )
    )
    assert all(slot.start.hour != 2 for slot in spring)
    autumn = _parsed_pse_rows(
        _official_pse_rows_for_local_day(
            datetime(2026, 10, 25, tzinfo=WARSAW)
        )
    )
    assert len([slot for slot in autumn if slot.start.hour == 2]) == 4


def test_absolute_interval_end_accepts_naive_z_and_offset_dtime_only() -> None:
    """Dtime-only cache rows retain all supported UTC encodings."""
    utc = ZoneInfo("UTC")
    starts = [
        datetime(2026, 8, 12, 22, 0, tzinfo=utc),
        datetime(2026, 8, 12, 22, 15, tzinfo=utc),
    ]

    def payload(formatter):
        return [
            {
                "dtime_utc": formatter(start + timedelta(minutes=15)),
                "rce_pln": 100.0 + 100.0 * index,
            }
            for index, start in enumerate(starts)
        ]

    variants = (
        payload(lambda value: value.strftime("%Y-%m-%d %H:%M:%S")),
        payload(lambda value: value.isoformat().replace("+00:00", "Z")),
        payload(lambda value: value.isoformat()),
    )
    parsed = [_parsed_pse_rows(rows) for rows in variants]
    assert parsed[0] == parsed[1] == parsed[2]
    assert len(parsed[0]) == 1
    assert parsed[0][0].start == datetime(2026, 8, 13, tzinfo=WARSAW)
    assert abs(parsed[0][0].price_pln_kwh - 0.15) < 1e-12


def test_absolute_interval_metadata_mismatch_fails_closed() -> None:
    """Malformed absolute metadata cannot fall back to local wall time."""
    valid = _official_pse_rows_for_local_day(
        datetime(2026, 8, 13, tzinfo=WARSAW)
    )[:2]
    corruptions = (
        {"period_utc": "22:00 - 22:30"},
        {"period_utc": "22:15 - 22:30"},
        {"dtime_utc": "2026-08-12 22:14:00"},
        {"dtime_utc": "2026-08-12 22:15:01"},
        {"dtime_utc": "not-a-timestamp"},
        {"business_date": "2026-08-14"},
    )
    for corruption in corruptions:
        rows = [dict(item) for item in valid]
        rows[0].update(corruption)
        assert _parsed_pse_rows(rows) == [], corruption

    for invalid_price in (float("nan"), float("inf"), float("-inf")):
        rows = [dict(item) for item in valid]
        rows[0]["rce_pln"] = invalid_price
        assert _parsed_pse_rows(rows) == []


def test_pse_absolute_utc_preserves_price_to_dst_fold_relationship() -> None:
    """PSE interval ends, not payload order, assign repeated-hour prices."""
    utc = ZoneInfo("UTC")
    rows = []
    # Fold 0 deliberately has the larger price.  The wall-time fallback's
    # stable content sort would assign the smaller price first, so this proves
    # that the official absolute timestamp is authoritative.
    for fold_start, base_price in (
        (datetime(2026, 10, 25, 0, 0, tzinfo=utc), 900.0),
        (datetime(2026, 10, 25, 1, 0, tzinfo=utc), 100.0),
    ):
        for minute in (0, 15, 30, 45):
            instant = fold_start + timedelta(minutes=minute)
            interval_end = instant + timedelta(minutes=15)
            rows.append(
                {
                    "business_date": "2026-10-25",
                    "period": instant.astimezone(WARSAW).strftime("%H:%M"),
                    "period_utc": (
                        f"{instant:%H:%M} - {interval_end:%H:%M}"
                    ),
                    "dtime_utc": interval_end.isoformat().replace(
                        "+00:00", "Z"
                    ),
                    "rce_pln": base_price + minute,
                }
            )

    def parsed(payload):
        return [
            (
                item.start.astimezone(utc),
                item.price_pln_kwh,
            )
            for item in RCE.parse_rce_rows(
                payload,
                WARSAW,
                block_enabled=False,
                block_start_minute=0,
                block_end_minute=0,
            )
        ]

    expected = parsed(rows)
    shuffled = list(rows)
    random.Random(253).shuffle(shuffled)
    assert expected == parsed(list(reversed(rows))) == parsed(shuffled)
    assert len(expected) == 4
    assert expected[0][1] > 0.9 and expected[1][1] > 0.9
    assert expected[2][1] < 0.2 and expected[3][1] < 0.2
    assert [start for start, _ in expected] == sorted(
        start for start, _ in expected
    )


def test_conflicting_absolute_duplicate_uses_conservative_price_end_to_end() -> None:
    """One UTC quarter cannot manufacture a high-price export signal."""
    first_quarter_utc = NOW.astimezone(ZoneInfo("UTC"))
    second_quarter_utc = first_quarter_utc + timedelta(minutes=15)
    rows = [
        {
            "dtime_utc": (first_quarter_utc + timedelta(minutes=15)).isoformat(),
            "rce_pln": 10_000.0,
            "source": "inflated_duplicate",
        },
        {
            "dtime_utc": (first_quarter_utc + timedelta(minutes=15)).isoformat(),
            "rce_pln": 900.0,
            "source": "conservative_duplicate",
        },
        {
            "dtime_utc": (second_quarter_utc + timedelta(minutes=15)).isoformat(),
            "rce_pln": 900.0,
        },
    ]
    parsed = RCE.parse_rce_rows(
        rows,
        WARSAW,
        block_enabled=False,
        block_start_minute=0,
        block_end_minute=0,
    )
    reversed_parsed = RCE.parse_rce_rows(
        reversed(rows),
        WARSAW,
        block_enabled=False,
        block_start_minute=0,
        block_end_minute=0,
    )
    assert parsed == reversed_parsed
    assert len(parsed) == 1
    assert abs(parsed[0].price_pln_kwh - 0.9) < 1e-9

    result = RCE.optimize_rce(base_input(price_slots=parsed))
    assert result.planned_exports
    assert all(
        abs(item.price_pln_kwh - 0.9) < 1e-9
        for item in result.planned_exports
    )

    # The post-parser UTC deduplicator also keeps the stricter block flag.
    normalized = RCE._supported_price_slots(
        base_input(
            price_slots=[
                RCE.PriceSlot(NOW, 1.0, blocked=False),
                RCE.PriceSlot(NOW, 0.9, blocked=True),
            ]
        )
    )
    assert len(normalized) == 1
    assert normalized[0].blocked
    assert abs(normalized[0].price_pln_kwh - 0.9) < 1e-9


def test_scheduler_requests_absolute_ordered_pse_rows() -> None:
    """Every shipped scheduler asks PSE for UTC fields in UTC order."""
    scheduler_paths = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "resources"
        / "home_assistant"
        / "en"
        / "hoymiles_ems_scheduler.yaml",
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "resources"
        / "home_assistant"
        / "pl"
        / "hoymiles_ems_scheduler.yaml",
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml",
    )
    for path in scheduler_paths:
        source = path.read_text(encoding="utf-8")
        assert source.count("%2Cdtime_utc%2Cperiod_utc") == 2
        assert source.count("%24orderby=dtime_utc%20asc") == 2
        assert source.count("forecast_learning_evidence_date:") == 2
        assert source.count("now().date().isoformat()") == 2


def test_real_horizon_solver_runtime_is_bounded() -> None:
    """A 110-slot market horizon must stay below a multi-second solve."""
    start = NOW.replace(hour=0)
    market = [
        RCE.PriceSlot(
            start + timedelta(minutes=30 * index),
            (0.15, 0.38, 0.72, 1.18, 0.55)[index % 5],
        )
        for index in range(110)
    ]
    pv = {
        slot.start: (
            5.0
            if 10 <= slot.start.astimezone(WARSAW).hour < 16
            else 0.0
        )
        for slot in market
    }
    settings = base_input(
        now=start,
        price_slots=market,
        pv_by_slot_kwh=pv,
        conservative_pv_by_slot_kwh=pv,
        battery_capacity_kwh=230.0,
        battery_soc_percent=58.0,
        outage_reserve_soc_percent=11.5,
        safety_margin_soc_percent=2.0,
        average_daily_load_kwh=37.4,
        average_night_load_kwh=11.4,
        inverter_power_kw=10.0,
        inverter_count=2,
        battery_wear_cost_pln_kwh=0.08,
    )
    started = monotonic_time.perf_counter()
    result = RCE.optimize_rce(settings)
    elapsed = monotonic_time.perf_counter() - started
    assert result.ready
    assert result.planned_exports
    # Optimizer work is executor-offloaded and that event-loop contract has a
    # dedicated test.  Keep enough shared-runner headroom here while still
    # catching a material, multi-second algorithmic regression.
    assert elapsed < SHARED_RUNNER_SOLVER_CEILING_SECONDS, elapsed
    assert (
        result.solver_runtime_ms
        < SHARED_RUNNER_SOLVER_CEILING_SECONDS * 1000.0
    )
    assert result.solver_method == "joint_horizon_bounded_active_set"
    assert not result.optimality_verified


def test_medium_horizon_pair_swap_crosses_pv_headroom_valley() -> None:
    """An 8-slot plan may require removing and adding exports together."""
    starts = [
        NOW.replace(hour=10) + timedelta(minutes=30 * index)
        for index in range(8)
    ]
    price_values = (0.8, 0.8, 0.8, 0.8, 0.8, -0.4, 1.4, 0.8)
    pv_values = (1.5, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0)
    load_values = (0.0, 0.0, 0.5, 0.0, 0.5, 0.5, 0.0, 0.0)
    prices = dict(zip(starts, price_values, strict=True))
    pv = dict(zip(starts, pv_values, strict=True))
    load = dict(zip(starts, load_values, strict=True))
    settings = base_input(
        now=starts[0],
        price_slots=[RCE.PriceSlot(start, prices[start]) for start in starts],
        battery_capacity_kwh=4.0,
        battery_soc_percent=62.5,
        dynamic_reserve_enabled=False,
        manual_minimum_soc_percent=12.5,
        outage_reserve_soc_percent=0.0,
        inverter_power_kw=2.0,
        battery_wear_cost_pln_kwh=0.08,
        charge_efficiency_percent=100.0,
        house_discharge_efficiency_percent=100.0,
    )
    floor = 0.5
    reserve = {start: floor for start in starts}
    oracle_value, _ = _independent_grid_oracle(
        starts=starts,
        settings=settings,
        load_by_slot=load,
        conservative_pv=pv,
        expected_pv=pv,
        floor_kwh=floor,
        reserve_by_slot=reserve,
        prices=prices,
    )
    _, baseline_end, baseline_natural = RCE._simulate(
        starts,
        settings,
        load,
        {},
        floor,
        reserve,
        pv,
    )
    baseline_objective = RCE._economic_objective(
        exports={},
        natural_exports=baseline_natural,
        price_by_start=prices,
        ending_battery_kwh=baseline_end,
        floor_kwh=floor,
        export_efficiency=1.0,
        battery_wear_cost_pln_kwh=0.08,
        terminal_energy_target_kwh=0.0,
        terminal_energy_value_pln_kwh=0.0,
    )[0]
    started = monotonic_time.perf_counter()
    plan = RCE._solve_joint_horizon_exports(
        starts=starts,
        settings=settings,
        candidates=[
            (RCE.PriceSlot(start, prices[start]), start) for start in starts
        ],
        load_by_slot=load,
        floor_kwh=floor,
        export_reserve_by_slot=reserve,
        conservative_pv=pv,
        expected_pv=pv,
        slot_fractions={start: 1.0 for start in starts},
        price_by_start=prices,
        baseline_objective=baseline_objective,
        export_efficiency=1.0,
        terminal_energy_target=0.0,
        terminal_unit_value=0.0,
    )
    elapsed = monotonic_time.perf_counter() - started
    safe, ending, natural = RCE._simulate(
        starts,
        settings,
        load,
        plan,
        floor,
        reserve,
        pv,
    )
    solver_value = RCE._economic_objective(
        exports=plan,
        natural_exports=natural,
        price_by_start=prices,
        ending_battery_kwh=ending,
        floor_kwh=floor,
        export_efficiency=1.0,
        battery_wear_cost_pln_kwh=0.08,
        terminal_energy_target_kwh=0.0,
        terminal_energy_value_pln_kwh=0.0,
    )[0]
    assert safe
    assert abs(oracle_value - 3.12) < 1e-9
    assert solver_value >= oracle_value - 0.02, (solver_value, oracle_value)
    assert elapsed < 1.0, elapsed


def test_irrelevant_padding_keeps_pair_refinement_on_relevant_slots() -> None:
    """Negative, zero or below-wear rows cannot hide the 8-slot exchange."""
    starts = [
        NOW.replace(hour=0) + timedelta(minutes=30 * index)
        for index in range(96)
    ]
    price_values = (0.8, 0.8, 0.8, 0.8, 0.8, -0.4, 1.4, 0.8)
    pv_values = (1.5, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 0.0)
    load_values = (0.0, 0.0, 0.5, 0.0, 0.5, 0.5, 0.0, 0.0)
    pv = {
        start: pv_values[index]
        for index, start in enumerate(starts[: len(pv_values)])
    }
    load = {
        start: load_values[index]
        for index, start in enumerate(starts[: len(load_values)])
    }
    floor = 0.5
    reserve = {start: floor for start in starts}
    for padding_price in (-1.0, 0.0, 0.02):
        prices = {
            start: (
                price_values[index]
                if index < len(price_values)
                else padding_price
            )
            for index, start in enumerate(starts)
        }
        settings = base_input(
            now=starts[0],
            price_slots=[
                RCE.PriceSlot(start, prices[start]) for start in starts
            ],
            battery_capacity_kwh=4.0,
            battery_soc_percent=62.5,
            dynamic_reserve_enabled=False,
            manual_minimum_soc_percent=12.5,
            outage_reserve_soc_percent=0.0,
            inverter_power_kw=2.0,
            battery_wear_cost_pln_kwh=0.08,
            charge_efficiency_percent=100.0,
            house_discharge_efficiency_percent=100.0,
        )
        _, baseline_end, baseline_natural = RCE._simulate(
            starts,
            settings,
            load,
            {},
            floor,
            reserve,
            pv,
        )
        baseline_objective = RCE._economic_objective(
            exports={},
            natural_exports=baseline_natural,
            price_by_start=prices,
            ending_battery_kwh=baseline_end,
            floor_kwh=floor,
            export_efficiency=1.0,
            battery_wear_cost_pln_kwh=0.08,
            terminal_energy_target_kwh=0.0,
            terminal_energy_value_pln_kwh=0.0,
        )[0]
        started = monotonic_time.perf_counter()
        plan = RCE._solve_joint_horizon_exports(
            starts=starts,
            settings=settings,
            candidates=[
                (RCE.PriceSlot(start, prices[start]), start)
                for start in starts
            ],
            load_by_slot=load,
            floor_kwh=floor,
            export_reserve_by_slot=reserve,
            conservative_pv=pv,
            expected_pv=pv,
            slot_fractions={start: 1.0 for start in starts},
            price_by_start=prices,
            baseline_objective=baseline_objective,
            export_efficiency=1.0,
            terminal_energy_target=0.0,
            terminal_unit_value=0.0,
        )
        elapsed = monotonic_time.perf_counter() - started
        safe, ending, natural = RCE._simulate(
            starts,
            settings,
            load,
            plan,
            floor,
            reserve,
            pv,
        )
        solver_value = RCE._economic_objective(
            exports=plan,
            natural_exports=natural,
            price_by_start=prices,
            ending_battery_kwh=ending,
            floor_kwh=floor,
            export_efficiency=1.0,
            battery_wear_cost_pln_kwh=0.08,
            terminal_energy_target_kwh=0.0,
            terminal_energy_value_pln_kwh=0.0,
        )[0]
        assert safe
        assert solver_value >= 3.12 - 1e-9, (
            padding_price,
            solver_value,
            plan,
        )
        assert elapsed < SHARED_RUNNER_SOLVER_CEILING_SECONDS, (
            padding_price,
            elapsed,
        )


def _solve_padded_fuzz_fixture(
    *,
    prices_first_eight: tuple[float, ...],
    conservative_pv_first_eight: tuple[float, ...],
    expected_pv_first_eight: tuple[float, ...],
    load_first_eight: tuple[float, ...],
    padding_price: float,
    initial_soc_percent: float,
) -> tuple[float, dict[datetime, float], float, object, list[datetime]]:
    """Solve one deterministic eight-slot fuzz case in a 96-row payload."""
    starts = [
        NOW.replace(hour=0) + timedelta(minutes=30 * index)
        for index in range(96)
    ]
    prices = {
        start: (
            prices_first_eight[index]
            if index < len(prices_first_eight)
            else padding_price
        )
        for index, start in enumerate(starts)
    }
    conservative_pv = {
        start: conservative_pv_first_eight[index]
        for index, start in enumerate(starts[:8])
    }
    expected_pv = {
        start: expected_pv_first_eight[index]
        for index, start in enumerate(starts[:8])
    }
    load = {
        start: load_first_eight[index]
        for index, start in enumerate(starts[:8])
    }
    settings = base_input(
        now=starts[0],
        price_slots=[RCE.PriceSlot(start, prices[start]) for start in starts],
        battery_capacity_kwh=4.0,
        battery_soc_percent=initial_soc_percent,
        dynamic_reserve_enabled=False,
        manual_minimum_soc_percent=12.5,
        outage_reserve_soc_percent=0.0,
        inverter_power_kw=2.0,
        battery_wear_cost_pln_kwh=0.08,
        charge_efficiency_percent=100.0,
        house_discharge_efficiency_percent=100.0,
    )
    floor = 0.5
    reserve = {start: floor for start in starts}
    _, baseline_end, baseline_natural = RCE._simulate(
        starts,
        settings,
        load,
        {},
        floor,
        reserve,
        expected_pv,
    )
    baseline_objective = RCE._economic_objective(
        exports={},
        natural_exports=baseline_natural,
        price_by_start=prices,
        ending_battery_kwh=baseline_end,
        floor_kwh=floor,
        export_efficiency=1.0,
        battery_wear_cost_pln_kwh=0.08,
        terminal_energy_target_kwh=0.0,
        terminal_energy_value_pln_kwh=0.0,
    )[0]
    started = monotonic_time.perf_counter()
    plan = RCE._solve_joint_horizon_exports(
        starts=starts,
        settings=settings,
        candidates=[
            (RCE.PriceSlot(start, prices[start]), start) for start in starts
        ],
        load_by_slot=load,
        floor_kwh=floor,
        export_reserve_by_slot=reserve,
        conservative_pv=conservative_pv,
        expected_pv=expected_pv,
        slot_fractions={start: 1.0 for start in starts},
        price_by_start=prices,
        baseline_objective=baseline_objective,
        export_efficiency=1.0,
        terminal_energy_target=0.0,
        terminal_unit_value=0.0,
    )
    elapsed = monotonic_time.perf_counter() - started
    conservative_safe, _ = _independent_short_simulation(
        starts=starts,
        settings=settings,
        load_by_slot=load,
        pv_by_slot=conservative_pv,
        exports=plan,
        floor_kwh=floor,
        reserve_by_slot=reserve,
        prices=prices,
    )
    conservative_baseline_safe, _ = _independent_short_simulation(
        starts=starts,
        settings=settings,
        load_by_slot=load,
        pv_by_slot=conservative_pv,
        exports={},
        floor_kwh=floor,
        reserve_by_slot=reserve,
        prices=prices,
    )
    expected_safe, solver_value = _independent_short_simulation(
        starts=starts,
        settings=settings,
        load_by_slot=load,
        pv_by_slot=expected_pv,
        exports=plan,
        floor_kwh=floor,
        reserve_by_slot=reserve,
        prices=prices,
    )
    # A forecast can describe an unavoidable reserve shortage even with no
    # controllable export.  The fail-safe result is then the empty plan; no
    # export schedule can repair that exogenous deficit.
    assert conservative_safe or (not conservative_baseline_safe and not plan)
    assert expected_safe
    return solver_value, plan, elapsed, settings, starts


def test_padded_fuzz_case_4_rejects_expected_value_destroying_exports() -> None:
    """Above-wear padding cannot hide the independent feasible lower bound."""
    prices = (-0.4, -0.4, 0.1, 0.8, -0.4, 1.4, 0.8, 0.8)
    conservative_pv = (1.5, 1.0, 1.5, 0.5, 1.5, 1.0, 0.5, 1.5)
    expected_pv = (1.5, 1.5, 1.5, 1.0, 1.5, 1.5, 1.0, 2.0)
    load = (0.0, 0.5, 0.5, 0.0, 0.0, 0.0, 0.0, 0.0)
    for padding_price in (0.081, 0.09, 0.10):
        solver_value, plan, elapsed, settings, starts = (
            _solve_padded_fuzz_fixture(
                prices_first_eight=prices,
                conservative_pv_first_eight=conservative_pv,
                expected_pv_first_eight=expected_pv,
                load_first_eight=load,
                padding_price=padding_price,
                initial_soc_percent=62.5,
            )
        )
        oracle_plan = {starts[3]: 1.0}
        load_by_slot = dict(zip(starts[:8], load, strict=True))
        reserve_by_slot = {start: 0.5 for start in starts}
        price_by_slot = {
            start: prices[index] if index < 8 else padding_price
            for index, start in enumerate(starts)
        }
        conservative_safe, _ = _independent_short_simulation(
            starts=starts,
            settings=settings,
            load_by_slot=load_by_slot,
            pv_by_slot=dict(zip(starts[:8], conservative_pv, strict=True)),
            exports=oracle_plan,
            floor_kwh=0.5,
            reserve_by_slot=reserve_by_slot,
            prices=price_by_slot,
        )
        expected_safe, oracle_value = _independent_short_simulation(
            starts=starts,
            settings=settings,
            load_by_slot=load_by_slot,
            pv_by_slot=dict(zip(starts[:8], expected_pv, strict=True)),
            exports=oracle_plan,
            floor_kwh=0.5,
            reserve_by_slot=reserve_by_slot,
            prices=price_by_slot,
        )
        assert conservative_safe and expected_safe, padding_price
        assert abs(oracle_value - 3.77) < 1e-9, padding_price
        assert solver_value >= oracle_value - 1e-9, (
            padding_price,
            solver_value,
            plan,
        )
        # Optimizer work is executor-offloaded.  This heavy 96-row regression
        # uses the same CI-stable wall guard as the dedicated real-horizon
        # benchmark; result correctness remains asserted independently above.
        assert elapsed < SHARED_RUNNER_SOLVER_CEILING_SECONDS, (
            padding_price,
            elapsed,
        )


def test_padded_fuzz_case_35_crosses_three_coordinate_valley() -> None:
    """Profitable padding cannot conceal a multi-coordinate PV valley."""
    prices = (-0.4, -0.4, 0.8, 1.4, 0.1, 1.4, 0.1, 0.8)
    conservative_pv = (1.0, 1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 0.5)
    expected_pv = (1.0, 1.5, 1.5, 1.0, 1.0, 0.5, 1.5, 0.5)
    load = (0.0, 0.0, 0.5, 0.0, 0.0, 0.0, 0.0, 0.5)
    for padding_price in (0.081, 0.09, 0.10):
        solver_value, plan, elapsed, settings, starts = (
            _solve_padded_fuzz_fixture(
                prices_first_eight=prices,
                conservative_pv_first_eight=conservative_pv,
                expected_pv_first_eight=expected_pv,
                load_first_eight=load,
                padding_price=padding_price,
                initial_soc_percent=50.0,
            )
        )
        oracle_plan = {
            starts[5]: 1.0,
            starts[6]: 1.0,
            starts[7]: 0.5,
        }
        load_by_slot = dict(zip(starts[:8], load, strict=True))
        reserve_by_slot = {start: 0.5 for start in starts}
        price_by_slot = {
            start: prices[index] if index < 8 else padding_price
            for index, start in enumerate(starts)
        }
        conservative_safe, _ = _independent_short_simulation(
            starts=starts,
            settings=settings,
            load_by_slot=load_by_slot,
            pv_by_slot=dict(zip(starts[:8], conservative_pv, strict=True)),
            exports=oracle_plan,
            floor_kwh=0.5,
            reserve_by_slot=reserve_by_slot,
            prices=price_by_slot,
        )
        expected_safe, oracle_value = _independent_short_simulation(
            starts=starts,
            settings=settings,
            load_by_slot=load_by_slot,
            pv_by_slot=dict(zip(starts[:8], expected_pv, strict=True)),
            exports=oracle_plan,
            floor_kwh=0.5,
            reserve_by_slot=reserve_by_slot,
            prices=price_by_slot,
        )
        assert conservative_safe and expected_safe, padding_price
        assert abs(oracle_value - 3.60) < 1e-9, padding_price
        assert solver_value >= oracle_value - 1e-9, (
            padding_price,
            solver_value,
            plan,
        )
        assert elapsed < SHARED_RUNNER_SOLVER_CEILING_SECONDS, (
            padding_price,
            elapsed,
        )


def test_additional_padded_long_horizon_fuzz_has_bounded_gap() -> None:
    """The audited 40-case 96-row fuzz stays close to a grid oracle."""
    rng = random.Random(20260813)
    absolute_gaps: list[float] = []
    relative_gaps: list[float] = []
    for case_index in range(40):
        prices = tuple(
            rng.choice((-0.4, 0.1, 0.8, 1.4)) for _ in range(8)
        )
        conservative_pv = tuple(
            rng.choice((0.0, 0.5, 1.0, 1.5)) for _ in range(8)
        )
        expected_pv = tuple(
            value + rng.choice((0.0, 0.5)) for value in conservative_pv
        )
        load = tuple(rng.choice((0.0, 0.5)) for _ in range(8))
        initial_soc = rng.choice((37.5, 50.0, 62.5))
        padding_price = (0.081, 0.09, 0.10)[case_index % 3]

        solver_value, plan, elapsed, settings, all_starts = (
            _solve_padded_fuzz_fixture(
                prices_first_eight=prices,
                conservative_pv_first_eight=conservative_pv,
                expected_pv_first_eight=expected_pv,
                load_first_eight=load,
                padding_price=padding_price,
                initial_soc_percent=initial_soc,
            )
        )
        starts = all_starts[:8]
        oracle_value, _ = _independent_grid_oracle(
            starts=starts,
            settings=settings,
            load_by_slot=dict(zip(starts, load, strict=True)),
            conservative_pv=dict(zip(starts, conservative_pv, strict=True)),
            expected_pv=dict(zip(starts, expected_pv, strict=True)),
            floor_kwh=0.5,
            reserve_by_slot={start: 0.5 for start in starts},
            prices=dict(zip(starts, prices, strict=True)),
        )
        assert elapsed < SHARED_RUNNER_SOLVER_CEILING_SECONDS, (
            case_index,
            elapsed,
            plan,
        )
        if not math.isfinite(oracle_value):
            # Case 10 has an exogenous conservative reserve shortage even for
            # the empty plan; the helper above verifies that the solver does
            # not schedule any controllable export in that state.
            continue
        gap = max(oracle_value - solver_value, 0.0)
        absolute_gaps.append(gap)
        relative_gaps.append(gap / max(abs(oracle_value), 1.0))

    # This is a feasible lower bound on a 0/0.5/1 kWh grid, not a claim of
    # global optimality.  Of 39 cases with any feasible grid point, the
    # continuous solver beats or matches it in 38; case 12 trails by only
    # 0.0045 PLN (0.1661%).  Case 10 is the unavoidable shortage above.
    assert len(absolute_gaps) == 39
    assert max(absolute_gaps) <= 0.004501, max(absolute_gaps)
    assert max(relative_gaps) <= 0.001661, max(relative_gaps)
    assert sorted(absolute_gaps)[len(absolute_gaps) // 2] < 1e-9


def test_far_future_and_duplicate_prices_cannot_expand_horizon() -> None:
    """Only today/tomorrow, one conservative value per UTC slot, is used."""
    current = RCE.PriceSlot(NOW, 1.0)
    duplicate = RCE.PriceSlot(NOW, 0.2)
    far_future = RCE.PriceSlot(NOW + timedelta(days=3650), 1000.0)
    settings = base_input(
        price_slots=[far_future, current, duplicate],
        battery_capacity_kwh=20.0,
        battery_soc_percent=100.0,
    )
    started = monotonic_time.perf_counter()
    result = RCE.optimize_rce(settings)
    elapsed = monotonic_time.perf_counter() - started
    assert result.ready
    assert result.planned_exports
    assert all(
        item.start.date() <= NOW.date() + timedelta(days=1)
        for item in result.planned_exports
    )
    assert all(abs(item.price_pln_kwh - 0.2) < 1e-9 for item in result.planned_exports)
    assert elapsed < 0.5, elapsed


def test_dst_rows_are_resolved_on_absolute_utc_timeline() -> None:
    """Spring gaps disappear and both autumn 02:xx folds remain distinct."""
    def row(day: str, clock: str) -> dict[str, object]:
        return {"business_date": day, "period": clock, "rce_pln": 1000.0}

    spring_rows = [
        row("2026-03-29", f"{hour:02d}:{minute:02d}")
        for hour in range(4)
        for minute in (0, 15, 30, 45)
    ]
    spring = RCE.parse_rce_rows(
        spring_rows,
        WARSAW,
        block_enabled=False,
        block_start_minute=0,
        block_end_minute=0,
    )
    assert len(spring) == 6
    assert len({item.start.astimezone(ZoneInfo("UTC")) for item in spring}) == 6

    autumn_rows = [
        *[
            row("2026-10-25", f"{hour:02d}:{minute:02d}")
            for hour in range(2)
            for minute in (0, 15, 30, 45)
        ],
        *[row("2026-10-25", f"02:{minute:02d}") for minute in (0, 15, 30, 45)],
        *[row("2026-10-25", f"02:{minute:02d}") for minute in (0, 15, 30, 45)],
        *[row("2026-10-25", f"03:{minute:02d}") for minute in (0, 15, 30, 45)],
    ]
    autumn = RCE.parse_rce_rows(
        autumn_rows,
        WARSAW,
        block_enabled=False,
        block_start_minute=0,
        block_end_minute=0,
    )
    absolute = [item.start.astimezone(ZoneInfo("UTC")) for item in autumn]
    assert len(autumn) == 10
    assert len(set(absolute)) == 10
    assert absolute == sorted(absolute)


def test_self_use_reserve_uses_fresh_physical_readback() -> None:
    sensor_source = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    assert '"sensor.hoymiles_hit_ems_self_use_soc_readback"' in sensor_source
    assert '"number.hoymiles_hit_self_use_soc"' not in sensor_source
    assert '"self_use_soc_data_fresh"' in sensor_source
    assert '"self_use_soc_age_seconds"' in sensor_source
    assert "minimum=10.0,\n            maximum=100.0" in sensor_source
    assert "minimum=0.0,\n            maximum=100.0" in sensor_source
    assert '"soc_data_fresh": battery_soc_sample.fresh' in sensor_source
    assert '"inverter_count_data_fresh"' in sensor_source
    assert '"inverter_count_age_seconds"' in sensor_source
    assert "inverter_count_raw or 1.0" not in sensor_source


def main() -> None:
    """Run without pytest so the release validator has no extra dependency."""
    tests = [
        test_v156_rce_optimizer_source_is_frozen,
        test_higher_tomorrow_price_wins,
        test_low_market_prices_still_use_the_best_48h_slots,
        test_negative_prices_do_not_dump_stored_energy,
        test_discharge_creates_headroom_before_worse_pv_overflow,
        test_home_energy_is_never_sold,
        test_upcoming_night_is_reserved_even_before_sunset,
        test_shortage_blocks_export,
        test_parallel_power_scales_slot_energy,
        test_export_lockout_excludes_slots,
        test_feasible_plan_is_not_broken_by_display_rounding,
        test_today_only_prices_produce_a_safe_plan,
        test_export_and_revenue_totals_expose_their_sources,
        test_bms_current_limit_caps_export_power,
        test_actual_day_load_corrects_day_load_projection,
        test_recorder_profile_is_used_instead_of_flat_load,
        test_conservative_pv_band_blocks_unsafe_export,
        test_gcf_zero_export_is_a_hard_cap_with_clear_status,
        test_effective_power_input_caps_slot_energy,
        test_gcf_caps_natural_pv_export_per_slot,
        test_battery_wear_rejects_gross_but_unprofitable_sale,
        test_day3_terminal_value_is_diagnostic_only,
        test_high_sale_price_is_not_blocked_by_default_terminal_value,
        test_terminal_value_boundary_uses_house_discharge_efficiency,
        test_terminal_reserve_reports_day3_availability_and_reason,
        test_whole_soc_control_reserve_does_not_overstate_export,
        test_gross_and_net_optimization_gain_are_unambiguous,
        test_sensor_exposes_terminal_and_gain_contract,
        test_dynamic_solcast_sources_and_day3_freshness_contract,
        test_shared_forecast_model_is_conservative_and_robust,
        test_forecast_gcf_snapshot_accepts_one_cycle_in_either_event_order,
        test_forecast_gcf_event_closure_is_bounded_and_never_transient,
        test_forecast_gcf_persistent_incoherence_and_unload_fail_closed,
        test_zero_export_forecast_learning_policy_is_fixed_and_reversible,
        test_unverified_gcf_pauses_learning_without_guessing_zero_export,
        test_zero_export_days_never_reenter_forecast_learning_history,
        test_forecast_learning_wiring_covers_rce_and_tariff,
        test_house_energy_model_applies_charge_and_discharge_losses,
        test_load_and_grid_share_the_same_bms_discharge_budget,
        test_current_slot_uses_only_real_remaining_fraction_and_live_power,
        test_partial_slot_plan_exposes_energy_bounded_execution_power,
        test_current_slot_start_fails_closed_without_fresh_live_inputs,
        test_bms_discharge_limit_fails_closed_on_invalid_freshness,
        test_bms_charge_limit_fails_closed_and_caps_future_refill,
        test_pv_charge_and_both_export_paths_share_one_ac_bridge,
        test_signed_age_contract_is_shared_with_rce_sensor,
        test_rce_sensor_dates_dtime_only_rows_by_quarter_start,
        test_current_run_end_covers_the_whole_consecutive_export_window,
        test_current_run_end_steps_across_autumn_dst_in_utc,
        test_p90_load_is_an_alternative_scenario_not_double_counted,
        test_meter_scale_high_pv_forecast_stays_revenue_first,
        test_critical_zero_pv_guard_blocks_optimistic_missing_p10,
        test_joint_solver_beats_greedy_headroom_counterexample,
        test_seven_slot_middle_price_threshold_is_retained,
        test_large_candidate_set_drains_the_complete_available_budget,
        test_neutral_slots_do_not_disable_partial_headroom_refinement,
        test_solver_matches_independent_random_oracle,
        test_missing_quarter_is_not_paired_with_the_next_half_hour,
        test_rce_row_parser_is_independent_of_payload_order,
        test_autumn_duplicate_rows_split_folds_deterministically,
        test_local_only_dst_fold_prices_cannot_be_swapped_into_false_profit,
        test_live_pse_interval_end_payload_builds_complete_local_day,
        test_official_pse_interval_ends_cover_both_dst_day_lengths,
        test_absolute_interval_end_accepts_naive_z_and_offset_dtime_only,
        test_absolute_interval_metadata_mismatch_fails_closed,
        test_pse_absolute_utc_preserves_price_to_dst_fold_relationship,
        test_conflicting_absolute_duplicate_uses_conservative_price_end_to_end,
        test_scheduler_requests_absolute_ordered_pse_rows,
        test_real_horizon_solver_runtime_is_bounded,
        test_medium_horizon_pair_swap_crosses_pv_headroom_valley,
        test_irrelevant_padding_keeps_pair_refinement_on_relevant_slots,
        test_padded_fuzz_case_4_rejects_expected_value_destroying_exports,
        test_padded_fuzz_case_35_crosses_three_coordinate_valley,
        test_additional_padded_long_horizon_fuzz_has_bounded_gap,
        test_far_future_and_duplicate_prices_cannot_expand_horizon,
        test_dst_rows_are_resolved_on_absolute_utc_timeline,
        test_self_use_reserve_uses_fresh_physical_readback,
    ]
    for test in tests:
        test()
    print(f"RCE optimizer: {len(tests)} deterministic scenarios passed")


if __name__ == "__main__":
    main()
