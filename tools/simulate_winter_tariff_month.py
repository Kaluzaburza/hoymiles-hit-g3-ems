"""Deterministic January closed-loop simulation for the tariff EMS.

This is a development tool, not production logic.  It builds a reproducible
heat-pump household profile, feeds the real tariff optimizer with the same
rolling horizon used by Home Assistant and executes only the current planned
half-hour.  Actual PV can intentionally differ from Solcast to stress the
replanning behaviour.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from math import cos, exp, pi
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from tariff_optimizer import (  # noqa: E402
    TariffOptimizerInput,
    TariffSchedule,
    horizon_gap_expensive_load_reserve_kwh,
    optimize_tariff_charging,
    robust_weighted_estimate,
    robust_weighted_upper_estimate,
    tariff_rate,
)


ZONE = ZoneInfo("Europe/Warsaw")
SLOT_HOURS = 0.5
CAPACITY_KWH = 21.0
RESERVE_SOC = 27.0
BASE_RESERVE_SOC = 25.0
MAXIMUM_SOC = 100.0
CHARGE_POWER_KW = 5.0
CHARGE_EFFICIENCY = 0.95
DISCHARGE_EFFICIENCY = 0.95
WEAR_COST = 0.06


# Hand-built, deterministic winter sequence.  It deliberately contains four
# near-zero days and six 18--20 kWh days.  The total is scaled to exactly
# 350 kWh, matching the user's January range and capped near 20 kWh/day.
PV_DAILY_SHAPE = (
    0.2, 0.5, 1.2, 8.0, 13.0, 20.0, 19.0, 18.0, 14.0, 5.0,
    0.4, 1.0, 9.0, 16.0, 20.0, 19.0, 11.0, 3.0, 0.7, 7.0,
    17.0, 20.0, 20.0, 15.0, 8.0, 2.0, 0.3, 12.0, 19.0, 20.0, 13.0,
)


@dataclass(slots=True)
class MonthResult:
    mode: str
    cost_pln: float = 0.0
    grid_import_kwh: float = 0.0
    low_import_kwh: float = 0.0
    peak_import_kwh: float = 0.0
    pv_used_kwh: float = 0.0
    pv_export_kwh: float = 0.0
    battery_grid_stored_kwh: float = 0.0
    battery_pv_stored_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0
    battery_wear_pln: float = 0.0
    mode_starts: int = 0
    grid_charge_slots: int = 0
    min_soc_percent: float = 100.0
    ending_soc_percent: float = 0.0
    reserve_violations: int = 0
    unserved_kwh: float = 0.0
    forecast_miss_kwh: float = 0.0
    forecast_overestimate_kwh: float = 0.0
    max_planned_shortage_kwh: float = 0.0
    insufficient_plan_slots: int = 0

    @property
    def total_cost_with_wear(self) -> float:
        return self.cost_pln + self.battery_wear_pln

    def rounded(self) -> dict[str, float | int | str]:
        data: dict[str, float | int | str] = {"mode": self.mode}
        for name in (
            "cost_pln", "total_cost_with_wear", "grid_import_kwh",
            "low_import_kwh", "peak_import_kwh", "pv_used_kwh",
            "pv_export_kwh", "battery_grid_stored_kwh",
            "battery_pv_stored_kwh", "battery_discharge_kwh",
            "battery_wear_pln", "min_soc_percent", "ending_soc_percent",
            "unserved_kwh", "forecast_miss_kwh", "forecast_overestimate_kwh",
            "max_planned_shortage_kwh",
        ):
            data[name] = round(float(getattr(self, name)), 3)
        for name in (
            "mode_starts", "grid_charge_slots", "reserve_violations",
            "insufficient_plan_slots",
        ):
            data[name] = int(getattr(self, name))
        return data


def tauron_g12w() -> TariffSchedule:
    return TariffSchedule(
        tariff_type="G12w",
        g11_price_pln_kwh=0.9741,
        low_price_pln_kwh=0.6306,
        medium_price_pln_kwh=0.6306,
        peak_price_pln_kwh=1.2304,
        cheap_windows=((13 * 60, 15 * 60), (22 * 60, 6 * 60)),
        weekend_low_price=True,
        polish_holidays_low_price=True,
        operator="TAURON",
    )


def half_hours(begin: datetime, end: datetime) -> list[datetime]:
    result: list[datetime] = []
    cursor = begin
    while cursor < end:
        result.append(cursor)
        cursor += timedelta(minutes=30)
    return result


def heat_pump_load_profile() -> tuple[dict[datetime, float], list[float]]:
    """Return a 950 kWh month with cold spells and two HP demand peaks."""
    begin = datetime(2026, 1, 1, tzinfo=ZONE)
    end = datetime(2026, 2, 1, tzinfo=ZONE)
    slots = half_hours(begin, end)
    daily_factors = [
        1.22, 1.20, 1.15, 1.08, 0.98, 0.92, 0.88, 0.91,
        1.02, 1.13, 1.24, 1.28, 1.20, 1.08, 0.98, 0.92,
        0.90, 0.96, 1.05, 1.14, 1.18, 1.12, 1.03, 0.95,
        0.90, 0.94, 1.02, 1.10, 1.17, 1.21, 1.14,
    ]

    def slot_weight(value: datetime) -> float:
        hour = value.hour + value.minute / 60.0
        # Continuous space-heating demand, plus DHW/temperature-recovery peaks.
        base = 0.78
        overnight = 0.25 if hour >= 22.0 or hour < 6.0 else 0.0
        morning = 0.85 * exp(-((hour - 7.5) / 1.15) ** 2)
        evening = 0.92 * exp(-((hour - 19.3) / 1.55) ** 2)
        daytime = 0.15 * exp(-((hour - 12.0) / 3.2) ** 2)
        return (base + overnight + morning + evening + daytime) * (
            daily_factors[value.day - 1]
        )

    raw = [slot_weight(slot) for slot in slots]
    scale = 950.0 / sum(raw)
    values = [value * scale for value in raw]
    profile = dict(zip(slots, values))
    daily = [
        sum(profile[slot] for slot in slots if slot.day == day)
        for day in range(1, 32)
    ]
    return profile, daily


def pv_profiles(*, forecast_bias: str = "normal") -> tuple[
    dict[datetime, float], dict[datetime, float], list[float], list[float]
]:
    """Return actual and forecast PV maps, both with realistic daylight shape."""
    begin = datetime(2026, 1, 1, tzinfo=ZONE)
    slots = half_hours(begin, datetime(2026, 2, 1, tzinfo=ZONE))
    base_scale = 350.0 / sum(PV_DAILY_SHAPE)
    actual_daily = [value * base_scale for value in PV_DAILY_SHAPE]
    if forecast_bias == "normal":
        multipliers = [
            1.18, 1.55, 1.35, 1.08, 0.97, 0.96, 1.04, 1.00,
            0.94, 1.15, 1.45, 1.30, 1.08, 1.02, 0.97, 1.00,
            1.06, 1.18, 1.42, 1.12, 0.98, 1.02, 0.96, 1.04,
            1.12, 1.30, 1.65, 1.08, 1.01, 0.98, 1.05,
        ]
    elif forecast_bias == "failed_string":
        # Five-day persistent failure: Solcast remains sunny while actual PV is
        # reduced to 20%. The sensor's live correction is represented by a
        # one-day recognition lag, after which the forecast becomes cautious.
        multipliers = [1.03] * 31
        for day in range(10, 15):
            actual_daily[day] *= 0.20
            multipliers[day] = 5.0 if day == 10 else 0.30
    else:
        raise ValueError(f"unknown forecast bias: {forecast_bias}")
    forecast_daily = [
        min(actual * multiplier, 20.5)
        for actual, multiplier in zip(actual_daily, multipliers)
    ]

    def allocate(daily: list[float]) -> dict[datetime, float]:
        result: dict[datetime, float] = {}
        for day, total in enumerate(daily, start=1):
            day_slots = [slot for slot in slots if slot.day == day]
            weights = []
            for slot in day_slots:
                hour = slot.hour + slot.minute / 60.0
                # January daylight in Poland, approximately 07:45--16:05.
                if 7.5 <= hour < 16.5:
                    phase = (hour - 7.5) / 9.0
                    weights.append(max(0.0, 1.0 - cos(2.0 * pi * phase)) / 2.0)
                else:
                    weights.append(0.0)
            weight_sum = sum(weights)
            for slot, weight in zip(day_slots, weights):
                result[slot] = total * weight / weight_sum if weight_sum else 0.0
        return result

    return allocate(actual_daily), allocate(forecast_daily), actual_daily, forecast_daily


def extended_maps(
    load_map: dict[datetime, float],
    pv_map: dict[datetime, float],
) -> tuple[dict[datetime, float], dict[datetime, float]]:
    """Repeat the last representative days into Feb 3 for a full horizon."""
    load = dict(load_map)
    pv = dict(pv_map)
    for offset in range(1, 4):
        source_date = datetime(2026, 1, 31 - (3 - offset), tzinfo=ZONE).date()
        target_date = datetime(2026, 2, offset, tzinfo=ZONE).date()
        for source, value in list(load_map.items()):
            if source.date() == source_date:
                target = source.replace(
                    year=target_date.year, month=target_date.month, day=target_date.day
                )
                load[target] = value
        for source, value in list(pv_map.items()):
            if source.date() == source_date:
                target = source.replace(
                    year=target_date.year, month=target_date.month, day=target_date.day
                )
                pv[target] = value
    return load, pv


def night_energy(profile: dict[datetime, float]) -> float:
    days: list[float] = []
    for day in range(1, 32):
        days.append(sum(
            energy for slot, energy in profile.items()
            if slot.day == day and (slot.hour >= 20 or slot.hour < 7)
        ))
    return sum(days) / len(days)


def execute_slot(
    *,
    battery: float,
    load: float,
    pv: float,
    grid_charge: bool,
    requested_battery_ac: float,
    reserve: float,
    maximum: float,
) -> tuple[float, dict[str, float]]:
    """Execute one physical slot using the optimizer's shared-power semantics."""
    flows = {
        "grid": 0.0, "pv_used": 0.0, "pv_export": 0.0,
        "grid_stored": 0.0, "pv_stored": 0.0, "discharged": 0.0,
        "unserved": 0.0,
    }
    direct_pv = min(load, pv)
    flows["pv_used"] += direct_pv
    remaining_load = load - direct_pv
    surplus_pv = pv - direct_pv
    grid_budget = CHARGE_POWER_KW * SLOT_HOURS if grid_charge else 0.0
    direct_grid = min(remaining_load, grid_budget)
    flows["grid"] += direct_grid
    remaining_load -= direct_grid
    grid_budget -= direct_grid
    if remaining_load > 1e-9:
        required_dc = remaining_load / DISCHARGE_EFFICIENCY
        discharged = min(required_dc, max(battery - reserve, 0.0), 10.0 * SLOT_HOURS)
        battery -= discharged
        flows["discharged"] += discharged
        covered = discharged * DISCHARGE_EFFICIENCY
        remaining_load -= covered
        if remaining_load > 1e-9:
            flows["grid"] += remaining_load
    if surplus_pv > 1e-9:
        stored = min(
            surplus_pv * CHARGE_EFFICIENCY,
            10.0 * SLOT_HOURS,
            max(maximum - battery, 0.0),
        )
        battery += stored
        flows["pv_stored"] += stored
        flows["pv_used"] += stored / CHARGE_EFFICIENCY
        flows["pv_export"] += max(surplus_pv - stored / CHARGE_EFFICIENCY, 0.0)
    if grid_charge and requested_battery_ac > 1e-9:
        accepted_ac = min(
            requested_battery_ac,
            grid_budget,
            max(maximum - battery, 0.0) / CHARGE_EFFICIENCY,
            10.0 * SLOT_HOURS / CHARGE_EFFICIENCY,
        )
        stored = accepted_ac * CHARGE_EFFICIENCY
        battery += stored
        flows["grid"] += accepted_ac
        flows["grid_stored"] += stored
    return battery, flows


def run_month(
    *,
    mode: str,
    day3: bool,
    forecast_bias: str = "normal",
    load_forecast_mode: str = "perfect",
    start_soc: float = 50.0,
) -> MonthResult:
    load_map, daily_loads = heat_pump_load_profile()
    actual_pv, forecast_pv, actual_daily_pv, forecast_daily_pv = pv_profiles(
        forecast_bias=forecast_bias,
    )
    if load_forecast_mode == "perfect":
        forecast_load = load_map
    elif load_forecast_mode == "average_profile":
        by_slot = {
            index: sum(
                energy
                for when, energy in load_map.items()
                if when.hour * 2 + when.minute // 30 == index
            ) / 31.0
            for index in range(48)
        }
        forecast_load = {
            when: by_slot[when.hour * 2 + when.minute // 30]
            for when in load_map
        }
    else:
        raise ValueError(f"unknown load forecast: {load_forecast_mode}")
    extended_load, extended_forecast_pv = extended_maps(
        forecast_load, forecast_pv
    )
    _, extended_actual_pv = extended_maps(load_map, actual_pv)
    schedule = tauron_g12w()
    begin = datetime(2026, 1, 1, tzinfo=ZONE)
    end = datetime(2026, 2, 1, tzinfo=ZONE)
    slots = half_hours(begin, end)
    reserve = CAPACITY_KWH * RESERVE_SOC / 100.0
    maximum = CAPACITY_KWH * MAXIMUM_SOC / 100.0
    battery = CAPACITY_KWH * start_soc / 100.0
    result = MonthResult(
        mode=(
            f"{mode}_{'day3' if day3 else 'fallback2'}_"
            f"{forecast_bias}_{load_forecast_mode}"
        )
    )
    previous_grid_charge = False
    cached_plan: dict[datetime, tuple[bool, float]] = {}

    for slot in slots:
        load = load_map[slot]
        pv = actual_pv[slot]
        grid_charge = False
        requested_battery_ac = 0.0
        if mode == "optimizer":
            # The production sensor replans continuously.  Replanning at each
            # half-hour boundary is sufficient for deterministic monthly
            # energy/cost results; start stability affects only seconds.
            horizon_days = 3 if day3 else 2
            horizon_end = slot.replace(hour=0, minute=0) + timedelta(days=horizon_days)
            pv_input = {
                when: energy for when, energy in extended_forecast_pv.items()
                if slot <= when < horizon_end
            }
            load_input = {
                when: energy for when, energy in extended_load.items()
                if slot <= when < horizon_end
            }
            terminal_soc = RESERVE_SOC
            if not day3:
                # Exact production fallback: missing Day 3 assumes zero PV,
                # but protects only the expensive unseen tail until its next
                # cheap opportunity. Cheap direct import must not inflate the
                # terminal target to 100%.
                planning_hours = max(
                    (horizon_end - slot).total_seconds() / 3600.0,
                    0.0,
                )
                fallback, _ = horizon_gap_expensive_load_reserve_kwh(
                    sum(daily_loads) / 31.0,
                    horizon_end,
                    max(48.0 - planning_hours, 0.0),
                    schedule,
                    charge_power_kw=CHARGE_POWER_KW,
                    battery_charge_power_kw=10.0,
                    charge_efficiency_percent=CHARGE_EFFICIENCY * 100.0,
                    discharge_efficiency_percent=(
                        DISCHARGE_EFFICIENCY * 100.0
                    ),
                    maximum_stored_energy_kwh=(
                        CAPACITY_KWH
                        * (MAXIMUM_SOC - RESERVE_SOC)
                        / 100.0
                    ),
                )
                terminal_soc = min(
                    RESERVE_SOC + fallback / CAPACITY_KWH * 100.0,
                    MAXIMUM_SOC,
                )
            history = daily_loads[max(slot.day - 29, 0) : slot.day - 1]
            _, load_uncertainty, load_history_days = robust_weighted_estimate(
                history
            )
            conservative_daily_load, _ = robust_weighted_upper_estimate(history)
            p10_input = {
                when: energy * 0.55
                for when, energy in pv_input.items()
            }
            p10_dates = tuple(sorted({when.date() for when in p10_input}))
            net_power_kw = (load - pv) / SLOT_HOURS
            current_battery_power = (
                net_power_kw
                if net_power_kw >= 0.0
                else -min(-net_power_kw, 10.0)
            )
            settings = TariffOptimizerInput(
                now=slot,
                pv_by_slot_kwh=pv_input,
                battery_capacity_kwh=CAPACITY_KWH,
                battery_soc_percent=battery / CAPACITY_KWH * 100.0,
                reserve_soc_percent=RESERVE_SOC,
                base_reserve_soc_percent=BASE_RESERVE_SOC,
                maximum_soc_percent=MAXIMUM_SOC,
                terminal_reserve_soc_percent=terminal_soc,
                average_daily_load_kwh=sum(daily_loads) / 31.0,
                average_night_load_kwh=night_energy(load_map),
                night_start_minute=20 * 60,
                night_end_minute=7 * 60,
                charge_power_kw=CHARGE_POWER_KW,
                system_ac_power_kw=10.0,
                requested_charge_power_kw=CHARGE_POWER_KW,
                battery_charge_power_kw=10.0,
                battery_discharge_power_kw=10.0,
                pv_charge_power_kw=10.0,
                charge_efficiency_percent=CHARGE_EFFICIENCY * 100.0,
                discharge_efficiency_percent=DISCHARGE_EFFICIENCY * 100.0,
                minimum_saving_pln_kwh=0.05,
                battery_wear_cost_pln_kwh=WEAR_COST,
                schedule=schedule,
                load_by_slot_kwh=load_input,
                horizon_days=horizon_days,
                conservative_daily_load_kwh=conservative_daily_load,
                load_uncertainty_ratio=load_uncertainty,
                load_history_days=load_history_days,
                pv_p10_by_slot_kwh=p10_input,
                pv_p10_available_dates=p10_dates,
                forecast_uncertainty_ratio=0.20,
                current_load_power_kw=load / SLOT_HOURS,
                current_pv_power_kw=pv / SLOT_HOURS,
                current_battery_power_kw=current_battery_power,
            )
            # Replan every two hours and at tariff boundaries. Between those
            # moments execute the cached slot decision, analogous to a stable
            # plan that is not materially changed by every sensor refresh.
            should_replan = (slot.hour, slot.minute) in {
                (0, 0), (5, 30), (12, 30), (14, 30), (21, 30),
            }
            if should_replan:
                plan = optimize_tariff_charging(settings)
                result.max_planned_shortage_kwh = max(
                    result.max_planned_shortage_kwh, plan.remaining_shortage_kwh,
                )
                if plan.status_code == "insufficient_cheap_window":
                    result.insufficient_plan_slots += 1
                cached_plan.clear()
                for item in plan.planned_charges:
                    allowed = item.action != "grid_support"
                    if item.start == slot and item.action == "grid_support":
                        allowed = plan.current_run_start_eligible
                    cached_plan[item.start] = (
                        allowed,
                        item.stored_energy_kwh / CHARGE_EFFICIENCY,
                    )
            grid_charge, requested_battery_ac = cached_plan.get(slot, (False, 0.0))
        elif mode == "self_use":
            pass
        elif mode == "ideal":
            # The ideal benchmark uses the same optimizer but perfect 3-day PV
            # and no forecast error. It remains physically causal and feasible.
            horizon_end = slot.replace(hour=0, minute=0) + timedelta(days=3)
            settings = TariffOptimizerInput(
                now=slot,
                pv_by_slot_kwh={k: v for k, v in extended_actual_pv.items() if slot <= k < horizon_end},
                battery_capacity_kwh=CAPACITY_KWH,
                battery_soc_percent=battery / CAPACITY_KWH * 100.0,
                reserve_soc_percent=RESERVE_SOC,
                base_reserve_soc_percent=BASE_RESERVE_SOC,
                maximum_soc_percent=MAXIMUM_SOC,
                terminal_reserve_soc_percent=RESERVE_SOC,
                average_daily_load_kwh=sum(daily_loads) / 31.0,
                average_night_load_kwh=night_energy(load_map),
                night_start_minute=1200,
                night_end_minute=420,
                charge_power_kw=CHARGE_POWER_KW,
                system_ac_power_kw=10.0,
                requested_charge_power_kw=CHARGE_POWER_KW,
                battery_charge_power_kw=10.0,
                battery_discharge_power_kw=10.0,
                pv_charge_power_kw=10.0,
                charge_efficiency_percent=95.0,
                discharge_efficiency_percent=95.0,
                minimum_saving_pln_kwh=0.05,
                schedule=schedule,
                load_by_slot_kwh={k: v for k, v in extended_load.items() if slot <= k < horizon_end},
                horizon_days=3,
                current_load_power_kw=load / SLOT_HOURS,
                current_pv_power_kw=pv / SLOT_HOURS,
                current_battery_power_kw=max(load - pv, 0.0) / SLOT_HOURS,
            )
            should_replan = (slot.hour, slot.minute) in {
                (0, 0), (5, 30), (12, 30), (14, 30), (21, 30),
            }
            if should_replan:
                plan = optimize_tariff_charging(settings)
                cached_plan.clear()
                for item in plan.planned_charges:
                    allowed = item.action != "grid_support"
                    if item.start == slot and item.action == "grid_support":
                        allowed = plan.current_run_start_eligible
                    cached_plan[item.start] = (
                        allowed, item.stored_energy_kwh / CHARGE_EFFICIENCY,
                    )
            grid_charge, requested_battery_ac = cached_plan.get(slot, (False, 0.0))
        else:
            raise ValueError(mode)

        if grid_charge and not previous_grid_charge:
            result.mode_starts += 1
        if grid_charge:
            result.grid_charge_slots += 1
        previous_grid_charge = grid_charge
        battery, flows = execute_slot(
            battery=battery,
            load=load,
            pv=pv,
            grid_charge=grid_charge,
            requested_battery_ac=requested_battery_ac,
            reserve=reserve,
            maximum=maximum,
        )
        price, zone = tariff_rate(slot, schedule)
        result.cost_pln += flows["grid"] * price
        result.grid_import_kwh += flows["grid"]
        if zone == "low":
            result.low_import_kwh += flows["grid"]
        else:
            result.peak_import_kwh += flows["grid"]
        result.pv_used_kwh += flows["pv_used"]
        result.pv_export_kwh += flows["pv_export"]
        result.battery_grid_stored_kwh += flows["grid_stored"]
        result.battery_pv_stored_kwh += flows["pv_stored"]
        result.battery_discharge_kwh += flows["discharged"]
        result.unserved_kwh += flows["unserved"]
        result.min_soc_percent = min(result.min_soc_percent, battery / CAPACITY_KWH * 100.0)
        if battery + 1e-7 < reserve:
            result.reserve_violations += 1

    result.battery_wear_pln = result.battery_grid_stored_kwh * WEAR_COST
    result.ending_soc_percent = battery / CAPACITY_KWH * 100.0
    result.forecast_miss_kwh = sum(forecast_daily_pv) - sum(actual_daily_pv)
    result.forecast_overestimate_kwh = sum(
        max(forecast - actual, 0.0)
        for forecast, actual in zip(forecast_daily_pv, actual_daily_pv)
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--case",
        choices=(
            "self-use", "optimizer-average", "optimizer-perfect",
            "optimizer-fallback", "failed-string", "all",
        ),
        default="optimizer-average",
        help="scenario to run; optimizer cases take roughly 2--3 minutes",
    )
    selected = parser.parse_args().case
    load_map, daily_loads = heat_pump_load_profile()
    _, _, actual_daily, forecast_daily = pv_profiles()
    factories = {
        "self-use": lambda: run_month(mode="self_use", day3=True),
        "optimizer-average": lambda: run_month(
            mode="optimizer", day3=True, load_forecast_mode="average_profile"
        ),
        "optimizer-perfect": lambda: run_month(mode="optimizer", day3=True),
        "optimizer-fallback": lambda: run_month(mode="optimizer", day3=False),
        "failed-string": lambda: run_month(
            mode="optimizer", day3=True, forecast_bias="failed_string"
        ),
    }
    cases = (
        [factory() for factory in factories.values()]
        if selected == "all"
        else [factories[selected]()]
    )
    payload = {
        "inputs": {
            "load_kwh": round(sum(load_map.values()), 3),
            "average_daily_load_kwh": round(sum(daily_loads) / 31.0, 3),
            "average_night_load_kwh": round(night_energy(load_map), 3),
            "pv_actual_kwh": round(sum(actual_daily), 3),
            "pv_forecast_kwh": round(sum(forecast_daily), 3),
            "near_zero_pv_days": sum(value < 1.0 for value in actual_daily),
            "high_pv_days": sum(value >= 18.0 for value in actual_daily),
            "maximum_pv_day_kwh": round(max(actual_daily), 3),
            "battery_kwh": CAPACITY_KWH,
            "charge_power_kw": CHARGE_POWER_KW,
            "reserve_soc_percent": RESERVE_SOC,
        },
        "cases": [case.rounded() for case in cases],
    }
    print(json.dumps(payload, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
