"""Cross-system simulation matrix for all automatic EMS planners.

The suite deliberately uses only the pure optimizer modules.  It therefore
cannot switch a real inverter and can be run before every public release.
Besides named household scenarios it performs deterministic parameter sweeps
for HIT 10/15/20 kW and a two-inverter 40 kW system.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil, sin, pi
from pathlib import Path
from random import Random
import sys
from zoneinfo import ZoneInfo

try:
    import yaml
except ImportError:  # Source-order checks below still run without PyYAML.
    yaml = None


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from rce_optimizer import OptimizerInput, PriceSlot, optimize_rce  # noqa: E402
from rcm_optimizer import RCMOptimizerInput, optimize_rcm  # noqa: E402
from tariff_optimizer import (  # noqa: E402
    TariffOptimizerInput,
    TariffSchedule,
    optimize_tariff_charging,
)


WARSAW = ZoneInfo("Europe/Warsaw")
EPSILON = 1e-5


@dataclass(frozen=True, slots=True)
class SystemModel:
    """Representative installation used by the release simulations."""

    name: str
    inverter_kw: float
    inverter_count: int
    battery_kwh: float
    pv_daily_kwh: float
    home_daily_kwh: float
    night_load_kwh: float
    battery_voltage_v: float
    bms_charge_a: float
    bms_discharge_a: float

    @property
    def system_kw(self) -> float:
        return self.inverter_kw * self.inverter_count

    @property
    def bms_charge_kw(self) -> float:
        return self.battery_voltage_v * self.bms_charge_a / 1000.0

    @property
    def bms_discharge_kw(self) -> float:
        return self.battery_voltage_v * self.bms_discharge_a / 1000.0


SYSTEMS = (
    SystemModel("HIT-10 + 10 kWh", 10.0, 1, 10.2, 12.0, 8.0, 3.2, 52.0, 100.0, 100.0),
    SystemModel("HIT-15 + 21 kWh", 15.0, 1, 21.0, 28.0, 16.0, 6.5, 52.5, 175.0, 175.0),
    SystemModel("HIT-20 + 40 kWh", 20.0, 1, 40.0, 55.0, 28.0, 11.0, 53.0, 250.0, 250.0),
    SystemModel("2x HIT-20 + 230 kWh", 20.0, 2, 230.0, 120.0, 48.0, 19.0, 53.0, 700.0, 700.0),
)


def _half_hours(start: datetime, end: datetime):
    cursor = start
    while cursor < end:
        yield cursor
        cursor += timedelta(minutes=30)


def pv_profile(
    first_day: datetime,
    today_kwh: float,
    tomorrow_kwh: float,
) -> dict[datetime, float]:
    """Create a smooth 06:00-18:00 two-day PV forecast."""

    result: dict[datetime, float] = {}
    for day_offset, total in ((0, today_kwh), (1, tomorrow_kwh)):
        starts = [
            first_day.replace(hour=6, minute=0) + timedelta(
                days=day_offset,
                minutes=30 * index,
            )
            for index in range(24)
        ]
        weights = [sin(pi * (index + 0.5) / len(starts)) for index in range(len(starts))]
        weight_sum = sum(weights)
        for start, weight in zip(starts, weights):
            result[start] = max(total, 0.0) * weight / weight_sum
    return result


def rce_prices(first_day: datetime, pattern: str) -> list[PriceSlot]:
    """Create two complete days of market prices and a 22:00-06:00 lockout."""

    slots: list[PriceSlot] = []
    end = first_day.replace(hour=0, minute=0) + timedelta(days=2)
    for start in _half_hours(first_day.replace(hour=0, minute=0), end):
        hour = start.hour + start.minute / 60.0
        if pattern == "tomorrow_peak":
            price = 1.15 if start.date() > first_day.date() and 6 <= hour < 9 else 0.32
            if start.date() == first_day.date() and 18 <= hour < 21:
                price = 0.78
        elif pattern == "today_peak":
            price = 1.25 if start.date() == first_day.date() and 18 <= hour < 21 else 0.55
        elif pattern == "volatile":
            if 10 <= hour < 15:
                price = -0.18
            elif 18 <= hour < 21:
                price = 1.40 if start.date() == first_day.date() else 1.05
            elif 6 <= hour < 9:
                price = 0.82
            else:
                price = 0.20
        elif pattern == "all_negative":
            price = -0.25
        else:
            price = 0.12
        blocked = hour >= 22 or hour < 6
        slots.append(PriceSlot(start=start, price_pln_kwh=price, blocked=blocked))
    return slots


def tariff_schedule(kind: str) -> TariffSchedule:
    """Return a stable synthetic schedule with realistic price ordering."""

    return TariffSchedule(
        tariff_type=kind,
        g11_price_pln_kwh=0.90,
        low_price_pln_kwh=0.55,
        medium_price_pln_kwh=0.82,
        peak_price_pln_kwh=1.18,
        cheap_windows=((22 * 60, 6 * 60), (13 * 60, 15 * 60)),
        medium_windows=((7 * 60, 13 * 60),),
        weekend_low_price=kind.casefold() in {"g12w", "g13"},
        polish_holidays_low_price=True,
    )


def assert_rce_invariants(settings: OptimizerInput, result) -> None:
    """Check safety, power, reserve and profitability invariants."""

    system_kw = settings.inverter_power_kw * settings.inverter_count
    requested_kw = system_kw * min(max(settings.discharge_power_percent, 0.0), 100.0) / 100.0
    assert 0.0 <= result.minimum_soc_percent <= 100.0
    assert 0.0 <= result.base_reserve_energy_kwh <= settings.battery_capacity_kwh + EPSILON
    assert 0.0 <= result.protected_home_energy_kwh <= settings.battery_capacity_kwh + EPSILON
    assert result.available_energy_now_kwh >= -EPSILON
    assert result.maximum_export_power_kw <= requested_kw + EPSILON
    assert abs(result.total_export_kwh - (result.planned_export_kwh + result.natural_export_kwh)) < EPSILON
    assert abs(result.total_revenue_pln - (result.planned_revenue_pln + result.natural_revenue_pln)) < EPSILON
    assert result.status_code != "optimizer_error"
    price_lookup = {slot.start: slot for slot in settings.price_slots}
    for item in result.planned_exports:
        assert item.start in price_lookup
        assert not price_lookup[item.start].blocked
        assert item.energy_kwh <= result.maximum_export_power_kw * 0.5 + 0.02
        assert item.energy_kwh >= 0.0
    if result.ready:
        assert result.ending_battery_kwh >= result.base_reserve_energy_kwh - EPSILON
        assert result.total_revenue_pln >= result.uncontrolled_revenue_pln - EPSILON
    else:
        assert not result.planned_exports
    if result.bms_discharge_power_limit_kw is not None:
        assert result.maximum_export_power_kw <= result.bms_discharge_power_limit_kw + EPSILON
    # With a full battery and unavoidable PV overflow the optimizer may choose
    # the least-bad negative-price slot.  With no natural overflow it must not
    # deliberately discharge the battery at a non-positive price.
    if (
        all(slot.price_pln_kwh <= 0 for slot in settings.price_slots)
        and result.uncontrolled_export_kwh <= EPSILON
    ):
        assert not result.planned_exports


def _record_rce_coverage(coverage: Counter[str], result) -> None:
    """Record non-vacuous evidence from one nominal RCE optimization."""

    if result.maximum_export_power_kw > EPSILON:
        coverage["positive_power"] += 1
    # ``solver_runtime_ms`` is populated only after the joint-horizon solver
    # returns.  Unlike ``solver_method`` (a result default), it therefore
    # proves that the solver path really executed.
    if result.solver_runtime_ms > 0.0:
        assert result.solver_method == "joint_horizon_bounded_active_set"
        coverage["joint_solver"] += 1
    if result.planned_export_kwh > EPSILON:
        assert result.planned_exports
        coverage["planned_export"] += 1


def _assert_rce_coverage(
    coverage: Counter[str],
    *,
    scope: str,
) -> None:
    """Reject nominal RCE matrices that only exercise fail-closed previews."""

    for marker in ("positive_power", "joint_solver", "planned_export"):
        assert coverage[marker] > 0, (
            f"Nominal RCE {scope} did not exercise {marker}; "
            "check the complete fresh BMS fixture contract"
        )


def run_rce_matrix(
    *,
    exhaustive: bool = True,
) -> tuple[int, Counter[str], dict[str, Counter[str]]]:
    """Exercise price selection, night protection, BMS limits and parallel power."""

    now = datetime(2026, 8, 10, 6, 0, tzinfo=WARSAW)
    statuses: Counter[str] = Counter()
    count = 0
    pv_factors = (0.0, 0.25, 1.0, 1.6) if exhaustive else (0.0, 1.6)
    tomorrow_factors = (0.1, 0.7, 1.3) if exhaustive else (0.1, 1.3)
    soc_values = (18.0, 55.0, 98.0) if exhaustive else (18.0, 98.0)
    coverage_by_model = {model.name: Counter() for model in SYSTEMS}
    for model in SYSTEMS:
        for pv_factor in pv_factors:
            for tomorrow_factor in tomorrow_factors:
                for soc in soc_values:
                    for price_pattern in ("tomorrow_peak", "today_peak", "volatile", "all_negative"):
                        settings = OptimizerInput(
                            now=now,
                            price_slots=rce_prices(now, price_pattern),
                            pv_by_slot_kwh=pv_profile(
                                now,
                                model.pv_daily_kwh * pv_factor,
                                model.pv_daily_kwh * tomorrow_factor,
                            ),
                            battery_capacity_kwh=model.battery_kwh,
                            battery_soc_percent=soc,
                            outage_reserve_soc_percent=20.0,
                            safety_margin_soc_percent=2.0,
                            manual_minimum_soc_percent=22.0,
                            dynamic_reserve_enabled=True,
                            average_daily_load_kwh=model.home_daily_kwh,
                            average_night_load_kwh=model.night_load_kwh,
                            night_start_minute=20 * 60,
                            night_end_minute=7 * 60,
                            inverter_power_kw=model.inverter_kw,
                            inverter_count=model.inverter_count,
                            discharge_power_percent=80.0,
                            export_efficiency_percent=94.0,
                            bms_max_discharge_current_a=model.bms_discharge_a,
                            bms_max_charge_current_a=model.bms_charge_a,
                            battery_voltage_v=model.battery_voltage_v,
                            bms_power_safety_percent=95.0,
                            bms_discharge_data_fresh=True,
                            bms_discharge_data_age_seconds=15.0,
                            bms_discharge_data_available=True,
                            bms_charge_data_fresh=True,
                            bms_charge_data_age_seconds=20.0,
                            bms_charge_data_available=True,
                        )
                        result = optimize_rce(settings)
                        assert_rce_invariants(settings, result)
                        _record_rce_coverage(
                            coverage_by_model[model.name],
                            result,
                        )
                        statuses[result.status_code] += 1
                        count += 1
    for model_name, coverage in coverage_by_model.items():
        _assert_rce_coverage(coverage, scope=f"model {model_name!r}")
    return count, statuses, coverage_by_model


def assert_tariff_invariants(settings: TariffOptimizerInput, result) -> None:
    """Check battery bounds, shared Grid Charge power and economic monotonicity."""

    assert -EPSILON <= result.ending_battery_kwh <= settings.battery_capacity_kwh + EPSILON
    assert 0.0 <= result.ending_battery_soc_percent <= 100.0 + EPSILON
    assert 0.0 <= result.target_soc_percent <= 100.0 + EPSILON
    assert result.remaining_shortage_kwh <= result.baseline_shortage_kwh + EPSILON
    assert result.planned_grid_import_kwh >= -EPSILON
    assert result.planned_stored_energy_kwh >= -EPSILON
    assert result.planned_direct_load_kwh >= -EPSILON
    reserve_gap = max(settings.reserve_soc_percent - settings.battery_soc_percent, 0.0) / 100.0 * settings.battery_capacity_kwh
    reserve_cost = reserve_gap / max(settings.charge_efficiency_percent / 100.0, 0.01) * settings.schedule.low_price_pln_kwh
    assert result.optimized_grid_cost_pln <= result.baseline_grid_cost_pln + reserve_cost + 0.02
    for item in result.planned_charges:
        assert item.grid_import_kwh <= result.charge_power_kw * 0.5 + EPSILON
        assert item.direct_load_kwh <= item.grid_import_kwh + EPSILON
        assert item.stored_energy_kwh <= max(item.grid_import_kwh - item.direct_load_kwh, 0.0) * settings.charge_efficiency_percent / 100.0 + EPSILON
        if settings.battery_charge_power_kw is not None:
            assert item.stored_energy_kwh <= settings.battery_charge_power_kw * 0.5 + EPSILON
        assert 0.0 <= item.target_soc_percent <= 100.0 + EPSILON
    if settings.schedule.tariff_type.casefold().replace(" ", "") == "g11":
        assert not result.planned_charges
        assert result.planned_grid_import_kwh == 0.0
    if result.next_charge_start is not None:
        assert any(item.start == result.next_charge_start for item in result.planned_charges)


def run_tariff_matrix(*, exhaustive: bool = True) -> tuple[int, Counter[str]]:
    """Exercise G11/G12/G12w/G13, weak PV, winter load and limited BMS power."""

    statuses: Counter[str] = Counter()
    count = 0
    all_moments = (
        datetime(2026, 8, 10, 5, 40, tzinfo=WARSAW),
        datetime(2026, 8, 10, 12, 20, tzinfo=WARSAW),
        datetime(2026, 8, 10, 14, 50, tzinfo=WARSAW),
        datetime(2026, 8, 10, 21, 10, tzinfo=WARSAW),
        datetime(2026, 8, 8, 9, 10, tzinfo=WARSAW),
    )
    moments = all_moments if exhaustive else (all_moments[0], all_moments[2])
    pv_factors = (0.0, 0.2, 1.2) if exhaustive else (0.0, 1.2)
    soc_values = (15.0, 45.0, 90.0) if exhaustive else (15.0, 90.0)
    for model in SYSTEMS:
        for kind in ("G11", "G12", "G12w", "G13"):
            for now in moments:
                for pv_factor in pv_factors:
                    for soc in soc_values:
                        charge_power = model.system_kw * 0.8
                        settings = TariffOptimizerInput(
                            now=now,
                            pv_by_slot_kwh=pv_profile(
                                now,
                                model.pv_daily_kwh * pv_factor,
                                model.pv_daily_kwh * (0.15 if pv_factor < 0.3 else 0.8),
                            ),
                            battery_capacity_kwh=model.battery_kwh,
                            battery_soc_percent=soc,
                            reserve_soc_percent=22.0,
                            maximum_soc_percent=95.0,
                            average_daily_load_kwh=model.home_daily_kwh * (1.6 if pv_factor == 0 else 1.0),
                            average_night_load_kwh=model.night_load_kwh,
                            night_start_minute=20 * 60,
                            night_end_minute=7 * 60,
                            charge_power_kw=charge_power,
                            charge_efficiency_percent=93.0,
                            discharge_efficiency_percent=94.0,
                            minimum_saving_pln_kwh=0.04,
                            schedule=tariff_schedule(kind),
                            battery_charge_power_kw=min(model.bms_charge_kw, model.system_kw),
                            battery_discharge_power_kw=min(model.bms_discharge_kw, model.system_kw),
                            pv_charge_power_kw=min(model.bms_charge_kw, model.system_kw),
                        )
                        result = optimize_tariff_charging(settings)
                        assert_tariff_invariants(settings, result)
                        statuses[result.status_code] += 1
                        count += 1
    return count, statuses


def assert_rcm_invariants(settings: RCMOptimizerInput, result) -> None:
    """Check SOC floors, BMS power, export cap and voltage emergency behavior."""

    assert result.maximum_voltage_v == max(settings.voltage_l1_v, settings.voltage_l2_v, settings.voltage_l3_v)
    assert 0.0 <= result.voltage_risk_score <= 100.0
    assert 0.0 <= result.required_headroom_kwh <= settings.battery_capacity_kwh + EPSILON
    assert result.available_headroom_kwh >= -EPSILON
    assert result.planned_grid_discharge_kwh >= -EPSILON
    energy_above_floor = max(settings.battery_capacity_kwh * (settings.battery_soc_percent - result.protected_minimum_soc_percent) / 100.0, 0.0)
    assert result.planned_grid_discharge_kwh <= energy_above_floor + EPSILON
    assert result.pre_discharge_target_soc_percent >= result.protected_minimum_soc_percent - EPSILON
    assert 10.0 <= result.recommended_charge_limit_percent <= 100.0
    assert result.recommended_charge_power_kw <= result.bms_charge_power_limit_kw + EPSILON
    assert 0.0 <= result.recommended_export_limit_percent <= result.effective_export_cap_percent + EPSILON
    assert result.pre_discharge_power_kw <= settings.system_power_kw + max(settings.load_power_kw, 0.0) + EPSILON
    if settings.battery_voltage_v and settings.bms_max_discharge_current_a:
        bms_discharge_kw = (
            settings.battery_voltage_v
            * settings.bms_max_discharge_current_a
            / 1000.0
        )
        # Result values are rounded for Home Assistant presentation.
        assert result.pre_discharge_power_kw <= bms_discharge_kw + 0.011, (
            result.pre_discharge_power_kw,
            bms_discharge_kw,
        )
    if result.maximum_voltage_v >= 253.0 and settings.export_control_enabled:
        assert result.status_code == "emergency"
        assert result.recommended_export_limit_percent == 0.0
    if result.pre_discharge_ready:
        assert result.action == "grid_discharge_preparation"
        assert (settings.minutes_to_risk or 0) > 30
        assert settings.risk_day_offset == 0


def run_rcm_matrix(*, exhaustive: bool = True) -> tuple[int, Counter[str]]:
    """Exercise stable, warning, emergency, full-battery and no-export states."""

    statuses: Counter[str] = Counter()
    count = 0
    now = datetime(2026, 8, 10, 11, 0, tzinfo=WARSAW)
    voltages = (
        (240.0, 248.6, 250.2, 251.2, 252.4, 253.2)
        if exhaustive
        else (240.0, 250.2, 252.4, 253.2)
    )
    soc_values = (18.0, 60.0, 98.0) if exhaustive else (18.0, 98.0)
    pv_factors = (0.0, 0.6, 1.2) if exhaustive else (0.0, 1.2)
    for model in SYSTEMS:
        for voltage in voltages:
            for soc in soc_values:
                for pv_factor in pv_factors:
                    for export_cap in (0.0, 50.0, 100.0):
                        pv_kw = model.system_kw * pv_factor
                        load_kw = min(model.home_daily_kwh / 12.0, model.system_kw * 0.6)
                        settings = RCMOptimizerInput(
                            now=now,
                            voltage_l1_v=voltage,
                            voltage_l2_v=voltage - 0.6,
                            voltage_l3_v=voltage - 1.1,
                            filtered_voltage_v=voltage - 0.2,
                            rolling_10m_voltage_v=voltage - 0.4,
                            historical_p90_voltage_v=max(voltage - 0.8, 240.0),
                            risk_windows=((12 * 60, 15 * 60, 254.0),),
                            history_days=4,
                            pv_power_kw=pv_kw,
                            load_power_kw=load_kw,
                            grid_export_power_kw=max(pv_kw - load_kw, 0.0),
                            battery_capacity_kwh=model.battery_kwh,
                            battery_soc_percent=soc,
                            reserve_soc_percent=20.0,
                            safety_margin_soc_percent=2.0,
                            protected_minimum_soc_percent=30.0,
                            expected_risk_surplus_kwh=model.pv_daily_kwh * 0.25,
                            expected_natural_headroom_kwh=model.home_daily_kwh * 0.05,
                            minutes_to_risk=60,
                            risk_day_offset=0,
                            system_power_kw=model.system_kw,
                            battery_voltage_v=model.battery_voltage_v,
                            bms_max_charge_current_a=model.bms_charge_a,
                            bms_max_discharge_current_a=model.bms_discharge_a,
                            current_charge_limit_percent=50.0,
                            saved_charge_limit_percent=90.0,
                            export_control_enabled=True,
                            current_export_limit_percent=export_cap,
                            saved_export_limit_percent=export_cap,
                            user_export_cap_percent=export_cap,
                            charge_efficiency_percent=94.0,
                        )
                        result = optimize_rcm(settings)
                        assert_rcm_invariants(settings, result)
                        statuses[result.status_code] += 1
                        count += 1
    return count, statuses


def run_randomized_boundary_sweep(
    *,
    samples: int = 120,
) -> tuple[int, Counter[str]]:
    """Add reproducible edge values not covered by the representative systems."""

    random = Random(20260808)
    # Keep the established scenario stream stable while adding independent
    # randomized values for the newly explicit BMS telemetry contract.
    bms_random = Random(20260809)
    now = datetime(2026, 8, 10, 6, 0, tzinfo=WARSAW)
    count = 0
    coverage: Counter[str] = Counter()
    for _ in range(samples):
        inverter_kw = random.choice((5.0, 8.0, 10.0, 12.0, 15.0, 20.0))
        inverter_count = random.randint(1, 3)
        capacity = random.uniform(5.0, 230.0)
        voltage = random.uniform(48.0, 58.0)
        discharge_a = random.uniform(50.0, 700.0)
        charge_a = bms_random.uniform(50.0, 700.0)
        settings = OptimizerInput(
            now=now,
            price_slots=rce_prices(now, random.choice(("tomorrow_peak", "volatile", "flat"))),
            pv_by_slot_kwh=pv_profile(now, random.uniform(0.0, 150.0), random.uniform(0.0, 150.0)),
            battery_capacity_kwh=capacity,
            battery_soc_percent=random.uniform(0.0, 100.0),
            outage_reserve_soc_percent=random.uniform(0.0, 45.0),
            safety_margin_soc_percent=random.uniform(0.0, 10.0),
            manual_minimum_soc_percent=random.uniform(0.0, 50.0),
            dynamic_reserve_enabled=random.choice((True, False)),
            average_daily_load_kwh=random.uniform(0.0, 90.0),
            average_night_load_kwh=random.uniform(0.0, 35.0),
            night_start_minute=20 * 60,
            night_end_minute=7 * 60,
            inverter_power_kw=inverter_kw,
            inverter_count=inverter_count,
            discharge_power_percent=random.uniform(0.0, 100.0),
            export_efficiency_percent=random.uniform(82.0, 99.0),
            bms_max_discharge_current_a=discharge_a,
            bms_max_charge_current_a=charge_a,
            battery_voltage_v=voltage,
            bms_power_safety_percent=random.uniform(80.0, 98.0),
            bms_discharge_data_fresh=True,
            bms_discharge_data_age_seconds=bms_random.uniform(-5.0, 300.0),
            bms_discharge_data_available=True,
            bms_charge_data_fresh=True,
            bms_charge_data_age_seconds=bms_random.uniform(-5.0, 300.0),
            bms_charge_data_available=True,
        )
        result = optimize_rce(settings)
        assert_rce_invariants(settings, result)
        _record_rce_coverage(coverage, result)
        count += 1
    _assert_rce_coverage(coverage, scope="randomized boundary family")
    return count, coverage


def assert_rce_bms_fail_closed_contracts() -> Counter[str]:
    """Keep invalid BMS telemetry explicit and outside nominal coverage."""

    now = datetime(2026, 8, 10, 6, 0, tzinfo=WARSAW)
    common = {
        "now": now,
        "price_slots": rce_prices(now, "today_peak"),
        "pv_by_slot_kwh": pv_profile(now, 0.0, 0.0),
        "battery_capacity_kwh": 100.0,
        "battery_soc_percent": 98.0,
        "outage_reserve_soc_percent": 0.0,
        "safety_margin_soc_percent": 0.0,
        "manual_minimum_soc_percent": 0.0,
        "dynamic_reserve_enabled": False,
        "average_daily_load_kwh": 0.0,
        "average_night_load_kwh": 0.0,
        "night_start_minute": 20 * 60,
        "night_end_minute": 7 * 60,
        "inverter_power_kw": 20.0,
        "inverter_count": 1,
        "discharge_power_percent": 80.0,
        "export_efficiency_percent": 94.0,
        "bms_max_charge_current_a": 250.0,
        "battery_voltage_v": 52.0,
        "bms_power_safety_percent": 95.0,
        "bms_charge_data_fresh": True,
        "bms_charge_data_age_seconds": 20.0,
        "bms_charge_data_available": True,
    }
    cases = (
        (
            "missing",
            {
                "bms_max_discharge_current_a": None,
                "bms_discharge_data_fresh": False,
                "bms_discharge_data_age_seconds": None,
                "bms_discharge_data_available": False,
            },
            "bms_discharge_data_unavailable",
        ),
        (
            "stale",
            {
                "bms_max_discharge_current_a": 250.0,
                "bms_discharge_data_fresh": False,
                "bms_discharge_data_age_seconds": 300.001,
                "bms_discharge_data_available": True,
            },
            "bms_discharge_data_stale",
        ),
        (
            "future",
            {
                "bms_max_discharge_current_a": 250.0,
                "bms_discharge_data_fresh": False,
                "bms_discharge_data_age_seconds": -5.001,
                "bms_discharge_data_available": True,
            },
            "bms_discharge_data_stale",
        ),
        (
            "zero",
            {
                "bms_max_discharge_current_a": 0.0,
                "bms_discharge_data_fresh": True,
                "bms_discharge_data_age_seconds": 0.0,
                "bms_discharge_data_available": False,
            },
            "bms_discharge_limit_zero",
        ),
    )
    checked: Counter[str] = Counter()
    for label, changes, expected_reason in cases:
        settings = OptimizerInput(**common, **changes)
        result = optimize_rce(settings)
        assert_rce_invariants(settings, result)
        assert result.bms_discharge_power_limit_kw == 0.0
        assert result.maximum_export_power_kw == 0.0
        assert result.bms_charge_power_limit_kw > 0.0
        assert result.solver_runtime_ms == 0.0
        assert not result.planned_exports
        assert not result.current_slot_start_eligible
        assert result.current_slot_suppression_reason == expected_reason
        checked[label] += 1
    assert checked == Counter({label: 1 for label, _, _ in cases})
    return checked


def assert_automation_interlocks() -> None:
    """Verify that the three controllers and service balancing cannot overlap."""

    source = (ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml").read_text(encoding="utf-8")
    required_markers = (
        "id: hoymiles_tariff_grid_charge_control",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_battery_balancing_active",
        "id: hoymiles_rce_grid_discharge_control",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "id: hoymiles_rcm_voltage_charge_control",
        "id: hoymiles_rcm_pre_discharge_control",
        "input_boolean.hoymiles_rcm_shadow_mode",
        "input_boolean.hoymiles_discharge_cycle_active",
        "input_boolean.hoymiles_charge_cycle_active",
        "is_state('timer.hoymiles_discharge', 'active')",
        "is_state('timer.hoymiles_charge', 'active')",
        "input_number.hoymiles_tariff_latched_target_soc",
        "input_datetime.hoymiles_tariff_latched_slot_end",
        "states.input_boolean.hoymiles_tariff_charge_active",
        "'current_slot_end'",
        'for: "00:00:45"',
        "input_datetime.hoymiles_rce_latched_slot_end",
        "binary_sensor.hoymiles_ems_execution_ready",
        "binary_sensor.hoymiles_rce_control_data_ready",
        "binary_sensor.hoymiles_tariff_control_data_ready",
        "binary_sensor.hoymiles_ems_export_allowed",
        "id: hoymiles_automatic_ems_control_failsafe",
        "states.sensor.hoymiles_hit_esp_uptime",
        "last_reported",
        "esp_age <= 180",
        "liveness_source: sensor.hoymiles_hit_esp_uptime",
        "plan_age >= -5",
        "plan_age <= 300",
        "rce_today_age_seconds",
        "forecast_today_age_seconds",
        "forecast_today_age_minutes",
        "forecast_tomorrow_age_minutes",
        "input_text.hoymiles_ems_last_push_fingerprint",
        "as_timestamp(now()) - last_push >= 300",
        "end - as_timestamp(now()) >= 300",
        "Falownik nie potwierdził limitu ładowania",
        "Falownik nie potwierdził nowego limitu ładowania",
        "Falownik nie potwierdził celu SOC",
    )
    for marker in required_markers:
        assert marker in source, f"Missing automation interlock marker: {marker}"

    # Day 3 remains an optional direct-source override.  A scheduler-side
    # minute-refresh proxy or auto-detection mutation would hide source age and
    # break the sensor package's signed-freshness provenance.
    day_3_helper = source.split(
        "hoymiles_solcast_forecast_day_3_entity:", 1
    )[1].split("hoymiles_rce_accounting_date:", 1)[0]
    assert "initial:" not in day_3_helper
    assert 'name: "Hoymiles Solcast Forecast Day 3"' not in source
    solcast_initializer = source.split(
        "id: hoymiles_initialize_solcast_forecast_entity", 1
    )[1].split("id: hoymiles_rce_sync_dynamic_minimum_soc", 1)[0]
    assert "hoymiles_solcast_forecast_day_3_entity" not in solcast_initializer

    safe_bms = source.split(
        'name: "Hoymiles RCE BMS Safe Discharge Power"', 1
    )[1].split(
        'name: "Hoymiles RCE Effective Discharge Power Percent"', 1
    )[0]
    for marker in (
        "sensor.hoymiles_hit_rce_optimized_plan",
        "'result_current') == true",
        "'bms_discharge_data_fresh') == true",
        "'bms_discharge_power_limit_kw'",
        "bms_data_age_seconds",
        "physical_limit_source",
    ):
        assert marker in safe_bms, f"Safe BMS helper lacks {marker}"
    assert "current * voltage" not in safe_bms, (
        "Safe BMS helper must not present stale raw telemetry as executable power"
    )
    rce_ready = source.split(
        'name: "Hoymiles RCE Control Data Ready"', 1
    )[1].split('name: "Hoymiles EMS Export Allowed"', 1)[0]
    for marker in (
        "'result_current') is sameas true",
        "'rce_today_data_fresh') is sameas true",
        "'rce_today_age_seconds'",
        "is_number(rce_age)",
        "(rce_age | float(-999)) >= -5",
        "'forecast_today_data_fresh') is sameas true",
        "'forecast_today_age_seconds'",
        "is_number(forecast_age)",
        "(forecast_age | float(-999)) >= -5",
        "plan_age >= -5 and plan_age <= 300",
    ):
        assert marker in rce_ready, f"RCE authoritative readiness lacks {marker}"
    for forbidden in (
        "price_reported",
        "source_reported",
        "price_age <= 300",
        "source_age <= 1200",
    ):
        assert forbidden not in rce_ready, (
            f"RCE readiness still trusts a derived proxy timestamp: {forbidden}"
        )
    transient_stop = (
        "or not is_state(\n"
        "                       'binary_sensor.hoymiles_tariff_planned_charge_slot', 'on')"
    )
    assert transient_stop not in source, (
        "Tariff charging still stops immediately when the live plan flickers"
    )
    assert (
        "is_state('binary_sensor.hoymiles_ems_export_allowed', 'on')" in source
    ), "RCE control does not enforce the zero-export/GCF guard"
    assert (
        "is_state('sensor.hoymiles_ems_hardware_mode', 'grid_discharge') }}"
        in source
    ), "RCE command acknowledgement is not based on the actual EMS readback"
    balancing_scripts = source.split(
        "  hoymiles_notify_battery_balancing_lifecycle:", 1
    )[1].split("\nautomation:", 1)[0]
    balancing_start = source.split(
        "hoymiles_start_battery_balancing:", 1
    )[1].split("hoymiles_stop_battery_balancing:", 1)[0]
    balancing_stop = source.split(
        "hoymiles_stop_battery_balancing:", 1
    )[1].split("automation:", 1)[0]
    balancing_abort = balancing_scripts.split(
        "hoymiles_abort_or_pause_battery_balancing:", 1
    )[1].split("hoymiles_battery_balancing_soft_gap_guard:", 1)[0]
    balancing_control = source.split(
        "- id: hoymiles_battery_balancing_control", 1
    )[1].split("- id: hoymiles_ems_push_status_notification", 1)[0]
    balancing_worker = source.split(
        "hoymiles_battery_balancing_transaction_worker:", 1
    )[1].split("\nautomation:", 1)[0]

    # The wrapper queues one worker. The worker reserves ownership first,
    # captures a provenance-bound b2 snapshot and revalidates it before the
    # first physical write.
    assert "mode: queued" in balancing_start
    assert "script.turn_on" in balancing_start
    assert "script.hoymiles_battery_balancing_transaction_worker" in balancing_start
    assert not any(
        helper in balancing_start
        for helper in (
            "script.hoymiles_verified_set_ems_maximum_charge_power",
            "script.hoymiles_verified_set_ems_force_charge_soc",
            "script.hoymiles_verified_set_ems_mode",
        )
    )
    for marker in (
        "next_sequence",
        "input_number.hoymiles_battery_balancing_cycle_sequence",
        'transaction_state: "REQUESTED"',
        'transaction_state: "OWNER_ACQUIRED"',
        "captured_mode",
        "sensor.hoymiles_hit_ems_maximum_charge_power_readback",
        "sensor.hoymiles_hit_ems_force_charge_soc_readback",
        "captured_ems_generation",
        "captured_topology_generation",
        'transaction_state: "SNAPSHOT_VALID"',
        "snapshot_valid: true",
        "snapshot_still_equal",
        "snapshot_changed_before_transaction",
        "write_started: true",
    ):
        assert marker in balancing_worker, f"Balancing transaction lacks {marker}"
    owner_ack = balancing_worker.index('transaction_state: "OWNER_ACQUIRED"')
    snapshot_capture = balancing_worker.index("captured_mode", owner_ack)
    snapshot_commit = balancing_worker.index('transaction_state: "SNAPSHOT_VALID"', snapshot_capture)
    snapshot_recheck = balancing_worker.index("snapshot_still_equal", snapshot_commit)
    write_started = balancing_worker.index("write_started: true", snapshot_recheck)
    first_apply_write = balancing_worker.index(
        "script.hoymiles_verified_set_ems_maximum_charge_power", write_started
    )
    assert owner_ack < snapshot_capture < snapshot_commit < snapshot_recheck
    assert snapshot_recheck < write_started < first_apply_write

    # Active foreign writers and manual timers are preserved by a hard start
    # gate, not cancelled and reconstructed from guessed state.
    for marker in (
        "input_boolean.hoymiles_discharge_cycle_active",
        "input_boolean.hoymiles_charge_cycle_active",
        "input_boolean.hoymiles_rce_discharge_active",
        "input_boolean.hoymiles_tariff_charge_active",
        "input_boolean.hoymiles_rcm_active",
        "input_boolean.hoymiles_rcm_pre_discharge_active",
        "binary_sensor.hoymiles_ems_control_conflict",
        "timer.hoymiles_discharge",
        "timer.hoymiles_charge",
    ):
        assert marker in balancing_worker
    start_transaction = balancing_worker.split(
        "# Only the worker creates a monotonic cycle", 1
    )[1].split("default:\n          # Routine reconcile", 1)[0]
    assert "action: timer.cancel" not in start_transaction

    # Mode-aware power uses one 400 W aggregate constant, exact topology,
    # downward 0.1% quantization and no post-cap minimum.
    assert source.count("BALANCING_SLOW_TARGET_KW = 0.4") == 1
    power_block = source.split(
        'name: "Hoymiles Battery Balancing BMS Safe Charge Power"', 1
    )[1].split(
        'name: "Hoymiles Battery Balancing Next Run"', 1
    )[0]
    for marker in (
        "self_use_percent",
        "grid_charge_percent",
        "round(0, 'floor')",
        "self_use_semantics: direct_battery_charge_cap",
        "grid_charge_semantics: common_ac_budget_including_load",
        "(machine_count | int(-1)) == 1",
        "(machine_count | int(-1)) >= 2",
        "(machine_count | int(-1)) <= 10",
    ):
        assert marker in power_block, f"Balancing power contract lacks {marker}"
    assert "[2 + home_power" not in power_block
    assert "[[desired_budget" not in power_block
    assert "| max | round(1)" not in power_block

    # Readiness exposes deterministic hard/soft provenance rather than treating
    # the combined binary sensor as an unconditional stop.
    readiness = source.split(
        'name: "Hoymiles Battery Balancing Control Data Ready"', 1
    )[1].split('name: "Hoymiles RCE Planned Export Slot"', 1)[0]
    for marker in (
        "failure_class:",
        "reason_code:",
        "soc_age >= -5 and soc_age <= 120",
        "load_age >= -5 and load_age <= 120",
        "current_age >= -5 and current_age <= 300",
        "voltage_age >= -5 and voltage_age <= 300",
        "topology_age >= -5 and topology_age <= 180",
        "bms_fault",
        "inverter_fault",
        "invalid_topology",
        "data_stale",
    ):
        assert marker in readiness, f"Balancing readiness lacks {marker}"

    # The accepted soft-gap deadline/generation is durable, restart-rearmed and
    # backward-clock safe. The guard only emits a generation-bound event; the
    # short controller classifies the single abort.
    soft_guard = balancing_scripts.split(
        "hoymiles_battery_balancing_soft_gap_guard:", 1
    )[1].split("hoymiles_apply_battery_balancing_target:", 1)[0]
    for marker in (
        "mode: restart",
        "guarded_start_ms",
        "guarded_deadline_ms",
        "guarded_start_ms | int(-1) + 60000",
        "remaining_seconds",
        "hoymiles_battery_balancing_soft_gap_deadline",
        "gap_generation",
    ):
        assert marker in soft_guard, f"Balancing soft-gap guard lacks {marker}"
    for marker in (
        "mode: queued",
        "timing_state == 'GAP'",
        "gap_generation",
        "gap_start_epoch_ms",
        "gap_deadline_epoch_ms",
        "new_gap_deadline_ms",
        "{{ (now_epoch_ms | int(0)) + 60000 }}",
        "clock_anomaly",
        "data_stale_timeout",
        "script.turn_on",
        "hoymiles_battery_balancing_soft_gap_guard",
    ):
        assert marker in balancing_control, (
            f"Restart-safe balancing soft-gap controller lacks {marker}"
        )
    assert balancing_control.count("timing_state == 'NONE'") == 2
    assert balancing_control.count(
        "script.hoymiles_battery_balancing_soft_gap_guard"
    ) == 2

    # A missed watchdog event is recovered from durable transaction state.
    for marker in (
        "timer.hoymiles_battery_balancing_watchdog",
        'state: "idle"',
        "transaction_state in [",
        "script.hoymiles_battery_balancing_request_abort",
        'reason_code: "watchdog_timeout"',
    ):
        assert marker in balancing_control, (
            f"Balancing watchdog recovery lacks {marker}"
        )

    # Slow remains latched from 95%; HOLD_ARMING and its absolute deadline are
    # durable before timer.start, then one acknowledged timer becomes HOLDING.
    for marker in (
        ">= 95",
        ">= 99.9",
        "entry_state in ['SLOW', 'HOLD_ARMING', 'HOLDING']",
        "HOLD_ARMING",
        "HOLDING",
        "hold_generation",
        "hold_deadline_epoch_ms",
    ):
        assert marker in balancing_worker
    hold_reset = balancing_worker.split(
        "# A drop below 99.9%", 1
    )[1].split("# Startup and periodic hold reconciliation", 1)[0]
    assert "< 99.9" in hold_reset
    assert "action: timer.cancel" in hold_reset
    assert 'transaction_state: "SLOW"' in hold_reset
    hold_completion = balancing_worker.split(
        "# Startup and periodic hold reconciliation", 1
    )[1].split("# Timer/deadline completion", 1)[0]
    for marker in (
        "hold_timing_cycle == entry_cycle",
        "hold_generation",
        "hold_deadline_ms",
        "timer.hoymiles_battery_balancing_hold",
        "duration: >-",
        'timing_state: "HOLDING"',
        'transaction_state: "HOLDING"',
    ):
        assert marker in hold_completion, (
            f"Balancing hold restart reconciliation lacks {marker}"
        )
    apply_target = balancing_worker.split("# Routine reconcile:", 1)[1]
    hold_arm = apply_target.split("# HOLD_ARMING and absolute deadline", 1)[1]
    hold_arm_start = apply_target.index("# HOLD_ARMING and absolute deadline")
    timer_start = hold_arm_start + hold_arm.index("action: timer.start")
    final_ack = apply_target.index("final_transaction_ack")
    assert final_ack < timer_start
    hold_arming = hold_arm_start + hold_arm.index('timing_state: "HOLD_ARMING"')
    holding = hold_arm_start + hold_arm.index('timing_state: "HOLDING"')
    assert hold_arming < timer_start < holding

    # Every target is re-read at its physical boundary; no positive fallback
    # or direct writable entity path exists.
    for marker in (
        "pre_target",
        "final_target",
        "corrected_target",
        "committed_target",
        "is_number(pre_target)",
        "is_number(final_target)",
        "is_number(committed_target)",
    ):
        assert marker in apply_target
    balancing_runtime = balancing_scripts + balancing_control
    for forbidden in (
        "| float(20)",
        "| float(10)",
        "| float(1)",
        "modbus.write_register",
        "modbus.write_registers",
        "number.hoymiles_hit_",
    ):
        assert forbidden not in balancing_runtime, (
            f"Balancing runtime contains unsafe/bypass marker {forbidden}"
        )
    for action in (
        "script.hoymiles_verified_set_ems_maximum_charge_power",
        "script.hoymiles_verified_set_ems_force_charge_soc",
        "script.hoymiles_verified_set_ems_mode",
    ):
        assert action in apply_target
        assert action in balancing_worker

    # Restore exact b2 snapshot values, retain Off-Grid, and release only after
    # a final live ACK/generation recheck. Notification enqueue follows physical
    # closeout and never calls the phone provider here.
    restore_path = balancing_worker.split(
        "# Restoration or provisional-owner release always wins", 1
    )[1].split("# Only the worker creates a monotonic cycle", 1)[0]
    for marker in (
        "trusted_snapshot",
        "snapshot_valid",
        "snapshot_cycle_id",
        "saved_mode",
        "saved_4303",
        "saved_4304",
        "off_grid_owner",
        "binary_sensor.hoymiles_battery_balancing_restore_authorized",
        "final_restore_ack",
        "release_abort_generation",
        "sensor.hoymiles_hit_ems_maximum_charge_power_readback",
        "sensor.hoymiles_hit_ems_force_charge_soc_readback",
    ):
        assert marker in restore_path, f"Balancing restore lacks {marker}"
    restore_ack = restore_path.index("final_restore_ack")
    release = restore_path.rindex("action: input_boolean.turn_off")
    terminal_push = restore_path.rindex(
        "script.hoymiles_notify_battery_balancing_lifecycle"
    )
    assert restore_ack < release < terminal_push

    # Lifecycle messages become durable stable-ID outbox records. Only the
    # independent dispatcher calls the phone, PENDING before service and
    # DELIVERED afterward. STARTED remains post-ACK.
    notify_script = source.split(
        "  hoymiles_notify_battery_balancing_lifecycle:", 1
    )[1].split("hoymiles_battery_balancing_update_outbox_delivery:", 1)[0]
    for marker in (
        "event_id",
        "stable_tag",
        "event_already_present",
        'slot_1_delivery_state: "PENDING"',
        'slot_2_delivery_state: "PENDING"',
    ):
        assert marker in notify_script
    assert "notify.send_message" not in notify_script
    dispatcher = balancing_scripts.split(
        "hoymiles_battery_balancing_notification_dispatcher:", 1
    )[1].split("hoymiles_battery_balancing_request_abort:", 1)[0]
    pending = dispatcher.rindex('next_delivery_state: "PENDING"')
    provider = dispatcher.index("action: notify.send_message", pending)
    delivered = dispatcher.index('next_delivery_state: "DELIVERED"', provider)
    assert pending < provider < delivered
    assert "tag: \"{{ selected_tag }}\"" in dispatcher
    assert not any(action in dispatcher for action in (
        "script.hoymiles_verified_set_ems_maximum_charge_power",
        "script.hoymiles_verified_set_ems_force_charge_soc",
        "script.hoymiles_verified_set_ems_mode",
        "input_boolean.turn_off",
    ))
    assert 'event: "started"' in apply_target
    assert "notify.send_message" not in balancing_control

    # Parser-level shape protects against valid block scalars swallowing an
    # action and proves all balancing writes use the three shared helpers.
    if yaml is not None:
        def nested_dicts(value):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from nested_dicts(child)
            elif isinstance(value, list):
                for child in value:
                    yield from nested_dicts(child)

        package = yaml.safe_load(source)
        scripts = package["script"]
        physical_by_script = {
            script_name: {
                item.get("action")
                for item in nested_dicts(script_body)
                if isinstance(item.get("action"), str)
                and item.get("action") in {
                    "script.hoymiles_verified_set_ems_maximum_charge_power",
                    "script.hoymiles_verified_set_ems_force_charge_soc",
                    "script.hoymiles_verified_set_ems_mode",
                }
            }
            for script_name, script_body in scripts.items()
            if "battery_balancing" in script_name
        }
        assert physical_by_script.pop(
            "hoymiles_battery_balancing_transaction_worker"
        ) == {
            "script.hoymiles_verified_set_ems_maximum_charge_power",
            "script.hoymiles_verified_set_ems_force_charge_soc",
            "script.hoymiles_verified_set_ems_mode",
        }
        assert all(not actions for actions in physical_by_script.values())

    # Model the dropped-trigger interleaving of a mode:single automation: the
    # first ACK returns after the inverter has moved to code3. The follow-up
    # write must fail closed even if the entry snapshot was fully ready.
    def followup_operational_write_allowed(
        owner_active: bool,
        policy_enabled: bool,
        data_ready: bool,
        control_conflict: bool,
        physical_mode: str,
        required_mode: str | None = None,
    ) -> bool:
        valid_mode = (
            physical_mode == required_mode
            if required_mode is not None
            else physical_mode
            not in {"off_grid", "unknown", "unavailable", "none", ""}
        )
        return (
            owner_active
            and policy_enabled
            and data_ready
            and not control_conflict
            and valid_mode
        )

    assert followup_operational_write_allowed(True, True, True, False, "self_use")
    assert followup_operational_write_allowed(
        True, True, True, False, "grid_charge", "grid_charge"
    )
    assert not followup_operational_write_allowed(
        True, True, True, False, "off_grid"
    )
    assert not followup_operational_write_allowed(
        True, True, True, False, "off_grid", "grid_charge"
    )
    assert not followup_operational_write_allowed(
        True, False, True, False, "self_use"
    ), "An in-flight user toggle-off must veto the next write/mode command"
    assert not followup_operational_write_allowed(
        True, True, True, True, "self_use"
    ), "A foreign owner appearing during ACK must veto the next command"
    assert not followup_operational_write_allowed(
        True, True, False, False, "self_use"
    )
    assert not followup_operational_write_allowed(
        False, True, True, False, "self_use"
    )


def assert_manual_cycle_finalization_contracts() -> None:
    """Protect manual owner recovery across timer expiry and HA restarts."""

    scheduler = (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
    ).read_text(encoding="utf-8")

    # A start owns the transaction before touching EMS. The watchdog grace
    # below is what makes that intentional owner=on/timer=idle phase safe.
    start_specs = (
        (
            "hoymiles_start_grid_discharge:",
            "hoymiles_start_grid_charge:",
            "input_boolean.hoymiles_discharge_cycle_active",
            'option: "grid_discharge"',
            "timer.hoymiles_discharge",
        ),
        (
            "hoymiles_start_grid_charge:",
            "hoymiles_stop_scheduled_cycle:",
            "input_boolean.hoymiles_charge_cycle_active",
            'option: "grid_charge"',
            "timer.hoymiles_charge",
        ),
    )
    for start_marker, end_marker, owner, mode_write, timer in start_specs:
        block = scheduler.split(start_marker, 1)[1].split(end_marker, 1)[0]
        owner_write = block.index("action: input_boolean.turn_on")
        assert owner in block[owner_write:]
        assert owner_write < block.index(mode_write) < block.index(
            "action: timer.start"
        )
        assert timer in block[block.index("action: timer.start") :]
        release_wait = block.index("wait_template", block.index(owner))
        mode_position = block.index(mode_write)
        readback_position = block.index(
            'state: "grid_discharge"'
            if "grid_discharge" in mode_write
            else 'state: "grid_charge"',
            mode_position,
        )
        timer_position = block.index("action: timer.start")
        for automatic_owner in (
            "input_boolean.hoymiles_rcm_active",
            "input_boolean.hoymiles_rcm_pre_discharge_active",
            "input_boolean.hoymiles_rcm_export_control_active",
            "input_boolean.hoymiles_tariff_charge_active",
            "input_boolean.hoymiles_rce_discharge_active",
        ):
            assert automatic_owner in block[release_wait:mode_position]
        final_guard = block.index(
            "# Final physical interlock after every ownership/rollback wait."
        )
        assert release_wait < final_guard < mode_position
        assert "'sensor.hoymiles_ems_hardware_mode', 'off_grid'" in block[
            final_guard:mode_position
        ]
        assert release_wait < mode_position < readback_position < timer_position
        if "grid_discharge" in mode_write:
            # The verified mode helper has already consumed fresh Master FC03.
            # Arm the exact timer immediately, without an extra wait that would
            # leave the claimed owner timer-idle across the minute watchdog.
            assert '- delay: "00:00:05"' not in block[mode_position:timer_position]
            assert "wait_template:" not in block[mode_position:timer_position]
            assert block.count("binary_sensor.hoymiles_sale_block_active") >= 3
            assert block.rindex(
                "binary_sensor.hoymiles_sale_block_active", 0, mode_position
            ) > release_wait, (
                "Manual Grid Discharge does not recheck sale lockout after waits"
            )
            neutral_comment = block.index(
                "RCEm pre-discharge retains ownership until its saved registers"
            )
            neutral_mode = block.index('option: "self_use"', neutral_comment)
            assert owner_write < neutral_comment < neutral_mode < release_wait
            neutral_guard = block[neutral_comment:neutral_mode]
            assert "input_boolean.hoymiles_rcm_active" in neutral_guard
            assert "input_boolean.hoymiles_rcm_pre_discharge_active" in neutral_guard
            assert "['self_use', 'off_grid', 'unknown', 'unavailable'" in neutral_guard
        else:
            assert '- delay: "00:00:05"' in block[mode_position:timer_position]
            assert "continue_on_timeout: false" in block[mode_position:timer_position]
            assert release_wait < mode_position, (
                "Manual Grid Discharge can race its final mode write with RCEm restore"
            )

    def rcm_manual_handover_can_release(
        rcm_owned: bool,
        manual_owner_active: bool,
        neutral_mode_acknowledged: bool,
    ) -> bool:
        if not rcm_owned:
            return True
        # RCEm intentionally does not force Self-Use over a manual owner. The
        # manual transaction must therefore provide the neutral ACK itself.
        return manual_owner_active and neutral_mode_acknowledged

    assert not rcm_manual_handover_can_release(True, True, False)
    assert rcm_manual_handover_can_release(True, True, True)
    assert rcm_manual_handover_can_release(False, True, False)

    for daily_marker, next_marker, start_script in (
        (
            "id: hoymiles_daily_grid_discharge",
            "id: hoymiles_daily_grid_charge",
            "script.hoymiles_start_grid_discharge",
        ),
        (
            "id: hoymiles_daily_grid_charge",
            "id: hoymiles_finish_grid_discharge",
            "script.hoymiles_start_grid_charge",
        ),
    ):
        daily = scheduler.split(daily_marker, 1)[1].split(next_marker, 1)[0]
        call_position = daily.index(start_script)
        assert (
            "'sensor.hoymiles_ems_hardware_mode', 'off_grid'"
            in daily[:call_position]
        ), f"Daily cycle {daily_marker} can start over physical Off-Grid"

    explicit_stop = scheduler.split(
        "hoymiles_stop_scheduled_cycle:", 1
    )[1].split("hoymiles_rollback_tariff_transaction:", 1)[0]
    cancel_position = explicit_stop.index("action: timer.cancel")
    mode_position = explicit_stop.index('option: "self_use"')
    readback_position = explicit_stop.rindex("in ['self_use', 'off_grid']")
    clear_position = explicit_stop.rindex("action: input_boolean.turn_off")
    assert cancel_position < mode_position < readback_position < clear_position
    assert "'sensor.hoymiles_ems_hardware_mode', 'off_grid'" in explicit_stop[
        cancel_position:mode_position
    ]
    assert "timer.hoymiles_discharge\n        state: \"idle\"" in explicit_stop
    assert "timer.hoymiles_charge\n        state: \"idle\"" in explicit_stop
    assert "continue_on_timeout: false" in explicit_stop

    # Balancing now preserves an already active manual owner/timer by refusing
    # to start over it. No timer is cancelled and reconstructed from a guess.
    balancing_worker = scheduler.split(
        "hoymiles_battery_balancing_transaction_worker:", 1
    )[1].split("\nautomation:", 1)[0]
    balancing = balancing_worker.split(
        "# Only the worker creates a monotonic cycle", 1
    )[1].split("default:\n          # Routine reconcile", 1)[0]
    for marker in (
        "timer.hoymiles_discharge",
        "timer.hoymiles_charge",
        "'idle'",
        "input_boolean.hoymiles_discharge_cycle_active",
        "input_boolean.hoymiles_charge_cycle_active",
    ):
        assert marker in balancing
    assert "action: timer.cancel" not in balancing
    assert "input_boolean.turn_off" not in balancing

    finish_specs = (
        (
            "id: hoymiles_finish_grid_discharge",
            "id: hoymiles_finish_grid_charge",
            "input_boolean.hoymiles_discharge_cycle_active",
            "timer.hoymiles_discharge",
        ),
        (
            "id: hoymiles_finish_grid_charge",
            "id: hoymiles_restore_ems_cycle_after_ha_restart",
            "input_boolean.hoymiles_charge_cycle_active",
            "timer.hoymiles_charge",
        ),
    )
    positive_safe_terminal_mode = "in ['self_use', 'off_grid']"
    for start_marker, end_marker, owner, timer in finish_specs:
        block = scheduler.split(start_marker, 1)[1].split(end_marker, 1)[0]
        for marker in (
            'minutes: "/1"',
            "id: timer_finished",
            "id: watchdog",
            "trigger.id == 'timer_finished'",
            ".last_changed",
            ">= 60",
            owner,
            timer,
            f"{{{{ is_state('{timer}', 'idle') }}}}",
            'timeout: "00:00:10"',
            'timeout: "00:00:35"',
            '- delay: "00:00:05"',
            positive_safe_terminal_mode,
            "'sensor.hoymiles_ems_hardware_mode', 'off_grid'",
            "# Clear ownership only after positive readback",
        ):
            assert marker in block, (
                f"Manual finalization block {start_marker} lacks {marker}"
            )
        assert block.count("continue_on_timeout: false") >= 2
        clear_position = block.rindex("action: input_boolean.turn_off")
        mode_write = block.index('option: "self_use"')
        assert block.rindex(
            "'sensor.hoymiles_ems_hardware_mode', 'off_grid'", 0, mode_write
        ) < mode_write
        assert block.rindex(positive_safe_terminal_mode) < clear_position
        assert block.rindex(
            f"{{{{ is_state('{timer}', 'idle') }}}}"
        ) < clear_position

    restart = scheduler.split(
        "id: hoymiles_restore_ems_cycle_after_ha_restart",
        1,
    )[1].split("id: hoymiles_rcm_voltage_charge_control", 1)[0]
    restore_choices = restart.split("      - choose:", 1)[1]
    discharge_restore, charge_restore = restore_choices.split(
        "      - choose:", 1
    )
    for block, owner, timer, active_mode in (
        (
            discharge_restore,
            "input_boolean.hoymiles_discharge_cycle_active",
            "timer.hoymiles_discharge",
            "grid_discharge",
        ),
        (
            charge_restore,
            "input_boolean.hoymiles_charge_cycle_active",
            "timer.hoymiles_charge",
            "grid_charge",
        ),
    ):
        for marker in (
            owner,
            timer,
            f"{{{{ is_state('sensor.hoymiles_ems_hardware_mode', '{active_mode}') }}}}",
            positive_safe_terminal_mode,
            f"{{{{ is_state('{timer}', 'idle') }}}}",
            '- delay: "00:00:05"',
            "'off_grid'",
            "continue_on_timeout: false",
        ):
            assert marker in block, (
                f"Manual restart recovery for {active_mode} lacks {marker}"
            )
        clear_position = block.rindex("action: input_boolean.turn_off")
        assert block.rindex(positive_safe_terminal_mode) < clear_position
        assert block.rindex(
            f"{{{{ is_state('{timer}', 'idle') }}}}"
        ) < clear_position

    # A restored discharge timer is cancelled and finalized if the export
    # lockout now forbids its continuation.
    assert "binary_sensor.hoymiles_sale_block_active" in discharge_restore
    assert "action: timer.cancel" in discharge_restore
    restored_mode_write = discharge_restore.index('option: "grid_discharge"')
    restored_wait = discharge_restore.index("wait_template:")
    assert discharge_restore.rindex(
        "binary_sensor.hoymiles_sale_block_active", 0, restored_mode_write
    ) > restored_wait, "Restart recovery can resume discharge after lockout"

    def watchdog_eligible(
        owner: bool,
        timer_state: str,
        owner_age_seconds: float,
        timer_finished: bool = False,
    ) -> bool:
        return (
            owner
            and timer_state == "idle"
            and (timer_finished or owner_age_seconds >= 60.0)
        )

    assert watchdog_eligible(True, "idle", 0.0, timer_finished=True)
    assert not watchdog_eligible(True, "idle", 59.9)
    assert watchdog_eligible(True, "idle", 60.0)
    for non_idle in ("active", "paused", "unknown", "unavailable"):
        assert not watchdog_eligible(True, non_idle, 600.0)
    assert not watchdog_eligible(False, "idle", 600.0)

    def ownership_can_clear(owner: bool, timer_state: str, mode: str) -> bool:
        return owner and timer_state == "idle" and mode in {"self_use", "off_grid"}

    assert ownership_can_clear(True, "idle", "self_use")
    assert ownership_can_clear(True, "idle", "off_grid")
    for unsafe_mode in (
        "unknown",
        "unavailable",
        "none",
        "grid_charge",
        "grid_discharge",
    ):
        assert not ownership_can_clear(True, "idle", unsafe_mode)
    for non_idle in ("active", "paused", "unknown", "unavailable"):
        assert not ownership_can_clear(True, non_idle, "self_use")
    assert not ownership_can_clear(False, "idle", "self_use")

    # Model the relevant TOCTOU interleaving: a caller may observe Self-Use,
    # wait for another owner, and only then see physical Off-Grid.  The final
    # guard must veto every non-Off-Grid command in that transition.
    def physical_mode_write_allowed(option: str, mode_at_write: str) -> bool:
        return option == "off_grid" or mode_at_write != "off_grid"

    assert physical_mode_write_allowed("off_grid", "off_grid")
    assert not physical_mode_write_allowed("self_use", "off_grid")
    assert not physical_mode_write_allowed("grid_charge", "off_grid")
    assert not physical_mode_write_allowed("grid_discharge", "off_grid")


def assert_tariff_startup_contracts() -> None:
    """Protect late-Solcast startup and fail-closed tariff readiness."""

    scheduler = (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
    ).read_text(encoding="utf-8")
    ready_block = scheduler.split(
        "unique_id: hoymiles_tariff_control_data_ready",
        1,
    )[1].split("attributes:", 1)[0]
    valid_statuses = {
        "ready",
        "insufficient_cheap_window",
        "no_charge_needed",
        "no_discount_window",
        "not_economically_beneficial",
        "shortage_in_low_period",
        "no_cheap_window",
    }
    blocked_statuses = {
        "missing_data",
        "optimizer_error",
        "unsupported_profile",
        "expired_profile",
        "soc_limits_conflict",
        "hard_reserve_unavailable",
    }
    for status in valid_statuses:
        assert f"'{status}'" in ready_block, (
            f"Valid tariff status {status} incorrectly blocks data readiness"
        )
    for status in blocked_statuses:
        assert f"'{status}'" not in ready_block, (
            f"Unsafe tariff status {status} incorrectly enables data readiness"
        )
    assert "'control_inputs_fresh') is sameas true" in ready_block, (
        "Tariff data readiness does not fail closed on stale SOC/BMS/LOAD"
    )
    for marker in (
        "'result_current') is sameas true",
        "'forecast_data_fresh') is sameas true",
        "'forecast_today_age_minutes'",
        "'forecast_tomorrow_age_minutes'",
        "is_number(forecast_today_age)",
        "is_number(forecast_tomorrow_age)",
        "plan_age >= -5 and plan_age <= 300",
    ):
        assert marker in ready_block, (
            f"Tariff authoritative readiness lacks {marker}"
        )
    for forbidden in (
        "states.sensor.hoymiles_solcast_forecast_today",
        "forecast_reported",
        "forecast_age <= 300",
    ):
        assert forbidden not in ready_block, (
            f"Tariff readiness still trusts a derived proxy: {forbidden}"
        )

    # Internal transaction helpers must be restored by HA. Supplying `initial`
    # would overwrite the frozen target/action during every restart.
    latched_target_helper = scheduler.split(
        "hoymiles_tariff_latched_target_soc:", 1
    )[1].split("hoymiles_rce_latched_minimum_soc:", 1)[0]
    active_action_helper = scheduler.split(
        "hoymiles_tariff_active_action:", 1
    )[1].split("hoymiles_ems_last_push_fingerprint:", 1)[0]
    assert "initial:" not in latched_target_helper
    assert "initial:" not in active_action_helper

    sensor_source = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "tariff_sensor.py"
    ).read_text(encoding="utf-8")
    retry_block = sensor_source.split(
        "def _async_input_changed",
        1,
    )[1].split("def _async_debounced_recalculate", 1)[0]
    assert "not self._forecast_retry_pending" in retry_block
    assert "self._forecast_accuracy_source_available()" in retry_block
    assert "self._forecast_retry_pending = True" in retry_block
    retry_method = sensor_source.split(
        "async def _async_retry_initial_forecast_accuracy",
        1,
    )[1].split("def _recalculate_and_write", 1)[0]
    assert "finally:" in retry_method
    assert "self._forecast_retry_pending = False" in retry_method

    for attribute in (
        "forecast_day_3_kwh",
        "forecast_day_3_raw_kwh",
    ):
        day_3_block = sensor_source.split(f'"{attribute}": (', 1)[1][:220]
        assert "if day_3_state is not None" in day_3_block
        assert "else None" in day_3_block, (
            f"Missing Day 3 must remain unknown for {attribute}, not 0 kWh"
        )


def assert_tariff_execution_contracts() -> None:
    """Keep optional grid support quiet without delaying required charging."""

    scheduler = (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
    ).read_text(encoding="utf-8")
    planned_slot_block = scheduler.split(
        "unique_id: hoymiles_tariff_planned_charge_slot",
        1,
    )[1].split(
        "unique_id: hoymiles_rce_reserve_ready",
        1,
    )[0]
    assert "not in ['ready', 'insufficient_cheap_window']" in planned_slot_block
    assert "current_run_remaining_minutes" in planned_slot_block
    assert "current_run_start_eligible" in planned_slot_block
    assert "(remaining | float(0)) < 7" in planned_slot_block
    start_block = scheduler.split(
        "# Start: plan i wszystkie dane są ważne",
        1,
    )[1].split(
        "# Aktualizacja ograniczeń BMS podczas bieżącego bloku",
        1,
    )[0]

    # Optional direct grid support must pass the optimizer's whole-run gate
    # and a separately tracked, stable intent. This must not be the old broad
    # plan fingerprint, which can drift with forecasts and target SOC.
    assert "'current_run_start_eligible'" in start_block
    assert "'current_run_intent_stable_seconds'" in start_block
    assert ">= 120" in start_block
    assert "'plan_stable_for_minutes'" not in start_block
    assert "'reserve_soc_percent'" in start_block
    assert "'input_number.hoymiles_tariff_maximum_soc'" in start_block
    assert "'current_run_remaining_minutes'" in scheduler

    # Required battery charging uses a capacity-scaled energy hysteresis. An
    # urgent reserve recovery bypasses only this headroom threshold.
    assert "['battery_charge', 'grid_support_and_charge']" in start_block
    assert "minimum_headroom_kwh" in start_block
    assert "(capacity | float(0)) * 0.005" in start_block
    assert "0.20] | max, 0.50]" in start_block
    assert "(soc | float(0)) < (reserve | float(0))" in start_block
    assert start_block.count(
        "'input_number.hoymiles_tariff_maximum_soc'"
    ) >= 2
    assert start_block.count(">= (reserve | float(0))") >= 1

    def eligible_required_charge(
        capacity_kwh: float,
        soc: float,
        target: float,
        reserve: float,
    ) -> bool:
        threshold = min(max(capacity_kwh * 0.005, 0.20), 0.50)
        headroom = (target - soc) * capacity_kwh / 100.0
        reserve_recovery = (
            soc < reserve
            and target - soc + EPSILON >= 0.2
            and headroom + EPSILON >= 0.10
        )
        return target > soc and (
            reserve_recovery or headroom + EPSILON >= threshold
        )

    for capacity_kwh in (10.0, 21.0, 230.0):
        threshold = min(max(capacity_kwh * 0.005, 0.20), 0.50)
        equality_target = 50.0 + threshold / capacity_kwh * 100.0
        assert eligible_required_charge(
            capacity_kwh, 50.0, equality_target, 25.0
        )
        assert not eligible_required_charge(
            capacity_kwh, 50.0, equality_target - 0.01, 25.0
        )
        assert eligible_required_charge(
            capacity_kwh, 24.9, 24.91, 25.0
        ) is False, "Tiny reserve deficits must not create a start/stop loop"
        assert eligible_required_charge(
            capacity_kwh,
            24.8,
            max(25.0, 24.8 + 0.10 / capacity_kwh * 100.0),
            25.0,
        ), "Material reserve recovery must bypass normal energy hysteresis"

    # Freeze the accepted intent before any Modbus acknowledgement wait. A
    # sensor refresh during the transaction may not change cycle ownership.
    assert "tariff_start_action:" in start_block
    assert "tariff_start_target_soc:" in start_block
    assert "tariff_start_reserve_soc:" in start_block
    assert "tariff_start_maximum_soc:" in start_block
    assert "tariff_start_power:" in start_block
    assert "tariff_start_slot_end:" in start_block
    assert "{{ tariff_start_action | default('none', true) }}" in start_block
    assert "{{ as_timestamp(tariff_start_slot_end) }}" in start_block
    assert start_block.count("tariff_start_power | float(0)") >= 5
    assert "Warunki bezpiecznego startu Grid Charge wygasły" in start_block
    assert "['off_grid', 'unknown', 'unavailable', 'none']" in start_block, (
        "Automatic tariff charging may override a physical Off-Grid mode"
    )
    assert "'current_zone') == 'low'" in start_block
    assert "- as_timestamp(now()) >= 300" in start_block
    assert "- as_timestamp(now())\n                       >= 420" in start_block
    assert '# The select entity may publish optimistically.' in start_block
    assert '- delay: "00:00:05"' in start_block
    final_guard = start_block.split(
        "# Setting readbacks may take up to two minutes",
        1,
    )[1].split(
        "# The select entity may publish optimistically",
        1,
    )[0]
    for marker in (
        "input_boolean.hoymiles_tariff_charge_enabled",
        "input_boolean.hoymiles_tariff_charge_active",
        "input_text.hoymiles_tariff_active_action",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_rcm_enabled",
        "input_boolean.hoymiles_battery_balancing_active",
        "input_boolean.hoymiles_discharge_cycle_active",
        "input_boolean.hoymiles_charge_cycle_active",
        "timer.hoymiles_discharge",
        "timer.hoymiles_charge",
        "binary_sensor.hoymiles_ems_execution_ready",
        "binary_sensor.hoymiles_tariff_control_data_ready",
        "binary_sensor.hoymiles_ems_control_conflict",
        "input_number.hoymiles_tariff_maximum_soc",
        "tariff_start_reserve_soc",
        "tariff_start_action",
        "control_inputs_fresh",
        "current_slot_planned",
        "current_action",
        "soc_age_seconds",
        "bms_charge_age_seconds",
        "plan_age >= 0 and plan_age <= 120",
    ):
        assert marker in final_guard, f"Final Grid Charge guard lacks {marker}"
    assert ") >= tariff_start_reserve_soc" in final_guard
    assert "tariff_start_maximum_soc" not in final_guard
    post_mode_start_guard = start_block.split(
        "# The select entity may publish optimistically", 1
    )[1]
    for marker in (
        "binary_sensor.hoymiles_ems_control_conflict",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "input_boolean.hoymiles_tariff_charge_active",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_rcm_enabled",
        "input_boolean.hoymiles_battery_balancing_active",
        "binary_sensor.hoymiles_ems_execution_ready",
        "binary_sensor.hoymiles_tariff_control_data_ready",
        "current_slot_planned",
        "current_action",
        "control_inputs_fresh",
        "plan_age >= 0 and plan_age <= 120",
        "sensor.hoymiles_hit_ems_maximum_charge_power_readback",
        "sensor.hoymiles_hit_ems_force_charge_soc_readback",
        "script.hoymiles_rollback_tariff_transaction",
    ):
        assert marker in post_mode_start_guard, (
            f"Tariff start post-mode ACK guard lacks {marker}"
        )

    # The automatic start is a durable transaction: snapshot and frozen action
    # precede ownership, and ownership precedes the first shared register write.
    snapshot_position = start_block.index(
        "input_number.hoymiles_tariff_saved_max_charge_power"
    )
    action_position = start_block.index(
        "entity_id: input_text.hoymiles_tariff_active_action"
    )
    owner_position = start_block.index(
        "entity_id: input_boolean.hoymiles_tariff_charge_active",
        action_position,
    )
    first_register_write = start_block.index(
        "action: script.hoymiles_verified_set_ems_maximum_charge_power"
    )
    assert snapshot_position < action_position < owner_position < first_register_write
    followup_guard = start_block.split(
        "# The 4304 ACK above may have taken a full minute.", 1
    )[1].split("- wait_template:", 1)[0]
    followup_write = followup_guard.index(
        "script.hoymiles_verified_set_ems_force_charge_soc"
    )
    for marker in (
        "input_boolean.hoymiles_tariff_charge_active",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "binary_sensor.hoymiles_ems_control_conflict",
        "binary_sensor.hoymiles_tariff_control_data_ready",
        "not in ['off_grid', 'unknown', 'unavailable'",
        "script.hoymiles_rollback_tariff_transaction",
    ):
        assert marker in followup_guard
    assert followup_guard.index(
        "binary_sensor.hoymiles_tariff_control_data_ready"
    ) < followup_write
    assert followup_guard.index(
        "binary_sensor.hoymiles_ems_control_conflict"
    ) < followup_write
    assert followup_guard.index(
        "not in ['off_grid', 'unknown', 'unavailable'"
    ) < followup_write
    for marker in (
        "input_number.hoymiles_tariff_saved_force_charge_soc",
        "script.hoymiles_rollback_tariff_transaction",
    ):
        assert marker in start_block

    rollback = scheduler.split(
        "hoymiles_rollback_tariff_transaction:", 1
    )[1].split("hoymiles_rollback_rce_transaction:", 1)[0]
    for marker in (
        "mode: single",
        "input_number.hoymiles_tariff_saved_max_charge_power",
        "input_number.hoymiles_tariff_saved_force_charge_soc",
        "sensor.hoymiles_hit_ems_control_readback_generation",
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "sensor.hoymiles_hit_ems_self_use_soc_readback",
        "sensor.hoymiles_hit_ems_backup_soc_readback",
        "sensor.hoymiles_hit_ems_maximum_charge_power_readback",
        "sensor.hoymiles_hit_ems_force_charge_soc_readback",
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
        "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
        "rollback_write_required",
        "rollback_generation_before_transaction",
        "rollback_generation_after_write",
        "number.hoymiles_hit_ems_complete_block_charge_rollback_command",
        "continue_on_timeout: false",
    ):
        assert marker in rollback, f"Tariff rollback lacks {marker}"
    assert "delay:" not in rollback
    assert "mode: queued" not in rollback
    assert "script.hoymiles_verified_set_ems_mode" not in rollback
    assert "script.hoymiles_verified_set_ems_maximum_charge_power" not in rollback
    assert "script.hoymiles_verified_set_ems_force_charge_soc" not in rollback
    assert rollback.count("action: number.set_value") == 1
    assert rollback.count(
        "number.hoymiles_hit_ems_complete_block_charge_rollback_command"
    ) == 1
    command_position = rollback.index("action: number.set_value")
    no_op_guard_position = rollback.index("rollback_write_required")
    no_op_generation_position = rollback.index(
        "rollback_generation_before_transaction"
    )
    generation_position = rollback.index(
        "rollback_generation_after_write", command_position
    )
    ack_position = rollback.index("- wait_template:", generation_position)
    no_op_comment_position = rollback.index("# A logical no-op emits no FC16")
    no_op_ack_position = rollback.index("- wait_template:", no_op_comment_position)
    owner_release_position = rollback.rindex("action: input_boolean.turn_off")
    assert no_op_guard_position < no_op_generation_position < command_position
    assert command_position < generation_position < ack_position
    assert ack_position < no_op_comment_position < no_op_ack_position
    assert no_op_ack_position < owner_release_position
    for ack_name, ack in (
        ("write", rollback[ack_position:no_op_comment_position]),
        ("no-op", rollback[no_op_ack_position:owner_release_position]),
    ):
        assert "continue_on_timeout: false" in ack
        assert "sensor.hoymiles_hit_ems_control_readback_generation" in ack
        for marker in (
            "rollback_expected_mode",
            "rollback_expected_4301",
            "rollback_expected_4302",
            "rollback_expected_4303",
            "rollback_expected_4304",
            "rollback_expected_4305",
            "rollback_expected_4306",
        ):
            assert marker in ack, f"Tariff rollback {ack_name} ACK lacks {marker}"
    final_guard = rollback[rollback.index(
        "# Off-Grid is a user-owned physical mode"
    ):owner_release_position]
    for marker in (
        "rollback_expected_mode",
        "rollback_expected_4301",
        "rollback_expected_4302",
        "rollback_expected_4303",
        "rollback_expected_4304",
        "rollback_expected_4305",
        "rollback_expected_4306",
    ):
        assert marker in final_guard, f"Tariff rollback final guard lacks {marker}"

    def tariff_rollback_payload(force_charge_soc: int, power_percent: float) -> int:
        return force_charge_soc * 1001 + round(power_percent * 10)

    def tariff_rollback_write_required(
        mode: int,
        force_charge_soc: float,
        power_percent: float,
        desired_mode: int,
        desired_force_charge_soc: float,
        desired_power_percent: float,
    ) -> bool:
        return (
            mode != desired_mode
            or abs(force_charge_soc - desired_force_charge_soc) >= 0.5
            or abs(power_percent - desired_power_percent) >= 0.05
        )

    assert tariff_rollback_payload(98, 50.0) == 98 * 1001 + 500
    assert not tariff_rollback_write_required(0, 98, 50, 0, 98, 50)
    assert tariff_rollback_write_required(0, 93, 50, 0, 98, 50)
    assert tariff_rollback_write_required(4, 93, 60, 0, 98, 50)

    if yaml is not None:
        package = yaml.safe_load(scheduler)
        rollback_script = package["script"]["hoymiles_rollback_tariff_transaction"]
        assert rollback_script["mode"] == "single"
        rollback_sequence = rollback_script["sequence"]
        rollback_if_index, rollback_if = next(
            (index, item)
            for index, item in enumerate(rollback_sequence)
            if isinstance(item, dict)
            and "if" in item
            and "number.hoymiles_hit_ems_complete_block_charge_rollback_command"
            in str(item)
        )
        assert rollback_if["if"] == [
            {
                "condition": "template",
                "value_template": "{{ rollback_write_required }}",
            }
        ]

        variable_maps = [
            item["variables"]
            for item in rollback_sequence[:rollback_if_index]
            if isinstance(item, dict) and "variables" in item
        ]
        transaction_variables = next(
            variables
            for variables in variable_maps
            if "rollback_payload" in variables
        )
        compact = lambda value: "".join(str(value).split())
        assert compact(transaction_variables["rollback_payload"]) == (
            "{{(rollback_expected_4303|int(-1))*1001"
            "+(((rollback_expected_4304|float(-1))*10)|round(0)|int(-1))}}"
        )
        assert compact(transaction_variables["rollback_write_required"]) == (
            "{{(states('sensor.hoymiles_hit_ems_mode_readback_code')|int(-1))"
            "!=(rollback_expected_mode|int(-2))"
            "or((states('sensor.hoymiles_hit_ems_force_charge_soc_readback')"
            "|float(-999))-(rollback_expected_4303|float(-998)))|abs>=0.5"
            "or((states('sensor.hoymiles_hit_ems_maximum_charge_power_readback')"
            "|float(-999))-(rollback_expected_4304|float(-998)))|abs>=0.05}}"
        )
        assert compact(
            transaction_variables["rollback_generation_before_transaction"]
        ) == (
            "{{states('sensor.hoymiles_hit_ems_control_readback_generation')}}"
        )

        then_sequence = rollback_if["then"]
        else_sequence = rollback_if.get("else")
        assert isinstance(else_sequence, list) and len(else_sequence) == 1
        action_items = [
            item for item in then_sequence if isinstance(item, dict) and "action" in item
        ]
        assert action_items == [
            {
                "action": "number.set_value",
                "target": {
                    "entity_id": (
                        "number.hoymiles_hit_ems_complete_block_charge_rollback_command"
                    )
                },
                "data": {"value": "{{ rollback_payload }}"},
            }
        ]
        assert len(then_sequence) == 3
        assert "variables" in then_sequence[1]
        assert set(then_sequence[1]["variables"]) == {
            "rollback_generation_after_write"
        }
        write_wait = then_sequence[2]
        no_op_wait = else_sequence[0]
        for wait in (write_wait, no_op_wait):
            assert set(wait) == {
                "wait_template",
                "timeout",
                "continue_on_timeout",
            }
            assert wait["timeout"] == "00:01:00"
            assert wait["continue_on_timeout"] is False

        def assert_full_tuple(template: str) -> None:
            normalized = compact(template)
            assert (
                "(states('sensor.hoymiles_hit_ems_verified_hardware_readback_supported')"
                "|float(0))>0.5"
            ) in normalized
            assert (
                "(states('sensor.hoymiles_hit_ems_mode_readback_code')|int(-1))"
                "==(rollback_expected_mode|int(-2))"
            ) in normalized
            for entity_id, expected, tolerance in (
                ("ems_self_use_soc_readback", "4301", "0.5"),
                ("ems_backup_soc_readback", "4302", "0.5"),
                ("ems_force_charge_soc_readback", "4303", "0.5"),
                ("ems_maximum_charge_power_readback", "4304", "0.05"),
                ("ems_force_discharge_soc_readback", "4305", "0.5"),
                ("ems_maximum_discharge_power_readback", "4306", "0.05"),
            ):
                assert (
                    f"((states('sensor.hoymiles_hit_{entity_id}')|float(-999))"
                    f"-(rollback_expected_{expected}|float(-998)))|abs<{tolerance}"
                ) in normalized

        write_template = write_wait["wait_template"]
        no_op_template = no_op_wait["wait_template"]
        generation_available = (
            "states('sensor.hoymiles_hit_ems_control_readback_generation')"
            "notin['unknown','unavailable','none','']"
        )
        assert generation_available in compact(write_template)
        assert generation_available in compact(no_op_template)
        assert (
            "states('sensor.hoymiles_hit_ems_control_readback_generation')"
            "!=rollback_generation_after_write"
        ) in compact(write_template)
        assert (
            "states('sensor.hoymiles_hit_ems_control_readback_generation')"
            "!=rollback_generation_before_transaction"
        ) in compact(no_op_template)
        assert_full_tuple(write_template)
        assert_full_tuple(no_op_template)

        final_condition = rollback_sequence[rollback_if_index + 1]
        assert final_condition["condition"] == "template"
        assert_full_tuple(final_condition["value_template"])

    update_block = scheduler.split(
        "# Aktualizacja ograniczeń BMS podczas bieżącego bloku",
        1,
    )[1].split(
        "# Stop: koniec zapamiętanego ciągłego okna",
        1,
    )[0]
    assert "binary_sensor.hoymiles_tariff_control_data_ready" in update_block, (
        "Active tariff updates are not gated by the committed fresh plan"
    )
    assert update_block.index(
        "binary_sensor.hoymiles_tariff_control_data_ready"
    ) < update_block.index(
        "script.hoymiles_verified_set_ems_maximum_charge_power"
    )
    assert "active == 'grid_support'" in update_block
    assert "current == 'grid_support'" in update_block
    assert update_block.count(
        "['battery_charge', 'grid_support_and_charge']"
    ) >= 2
    deadline_guard = update_block.split(
        "# A helper ACK may hold this mode:single automation for 60 s,", 1
    )[1].split(
        "# Re-check again after the optional latch action", 1
    )[0]
    latch_write = deadline_guard.index(
        "action: input_datetime.set_datetime"
    )
    for marker in (
        "input_boolean.hoymiles_tariff_charge_active",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "binary_sensor.hoymiles_ems_control_conflict",
        "binary_sensor.hoymiles_tariff_control_data_ready",
        "binary_sensor.hoymiles_tariff_planned_charge_slot",
        "entity_id: sensor.hoymiles_ems_hardware_mode",
        'state: "grid_charge"',
        "'status_code'",
        "'current_action'",
        "'current_slot_end'",
        "input_datetime.hoymiles_tariff_latched_slot_end",
        "'current_run_continue_eligible'",
        "'current_run_intent_stable_seconds'",
        "script.hoymiles_rollback_tariff_transaction",
    ):
        assert marker in deadline_guard
    assert deadline_guard.index(
        "binary_sensor.hoymiles_tariff_control_data_ready"
    ) < latch_write
    assert deadline_guard.index(
        "binary_sensor.hoymiles_ems_control_conflict"
    ) < latch_write
    assert deadline_guard.index(
        "entity_id: sensor.hoymiles_ems_hardware_mode"
    ) < latch_write
    active_followup_guard = update_block.split(
        "# Re-check again after the optional latch action", 1
    )[1].split("- wait_template:", 1)[0]
    active_followup_write = active_followup_guard.index(
        "script.hoymiles_verified_set_ems_force_charge_soc"
    )
    for marker in (
        "input_boolean.hoymiles_tariff_charge_active",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "binary_sensor.hoymiles_ems_control_conflict",
        "binary_sensor.hoymiles_tariff_control_data_ready",
        "binary_sensor.hoymiles_tariff_planned_charge_slot",
        "entity_id: sensor.hoymiles_ems_hardware_mode",
        'state: "grid_charge"',
        "'status_code'",
        "'current_action'",
        "'current_slot_end'",
        "input_datetime.hoymiles_tariff_latched_slot_end",
        "'current_run_continue_eligible'",
        "'current_run_intent_stable_seconds'",
        "script.hoymiles_rollback_tariff_transaction",
    ):
        assert marker in active_followup_guard
    assert active_followup_guard.index(
        "binary_sensor.hoymiles_tariff_control_data_ready"
    ) < active_followup_write
    assert active_followup_guard.index(
        "binary_sensor.hoymiles_ems_control_conflict"
    ) < active_followup_write
    assert active_followup_guard.index(
        "entity_id: sensor.hoymiles_ems_hardware_mode"
    ) < active_followup_write
    post_4303_guard = scheduler.split(
        "# The second 4303 ACK can also outlive", 1
    )[1].split("\n          - conditions:", 1)[0]
    for marker in (
        "input_boolean.hoymiles_tariff_charge_active",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "binary_sensor.hoymiles_ems_control_conflict",
        "binary_sensor.hoymiles_tariff_control_data_ready",
        "binary_sensor.hoymiles_tariff_planned_charge_slot",
        "sensor.hoymiles_ems_hardware_mode",
        'state: "grid_charge"',
        "current_action",
        "current_slot_end",
        "input_datetime.hoymiles_tariff_latched_slot_end",
        "current_run_continue_eligible",
        "script.hoymiles_rollback_tariff_transaction",
    ):
        assert marker in post_4303_guard, (
            f"Active tariff post-4303 ACK guard lacks {marker}"
        )

    def active_tariff_update_allowed(
        planned: bool,
        status: str,
        active_action: str,
        current_action: str,
        slot_seconds_remaining: float,
        latched_seconds_remaining: float,
        continue_eligible: bool,
        stable_seconds: float,
        active_seconds: float,
    ) -> bool:
        compatible = (
            active_action == current_action == "grid_support"
            or (
                active_action in {"battery_charge", "grid_support_and_charge"}
                and current_action
                in {"battery_charge", "grid_support_and_charge"}
            )
        )
        continuation_valid = continue_eligible or not (
            stable_seconds >= 120 and active_seconds >= 120
        )
        return (
            planned
            and status in {"ready", "insufficient_cheap_window"}
            and compatible
            and slot_seconds_remaining > 0
            and latched_seconds_remaining > 0
            and continuation_valid
        )

    tariff_live = dict(
        planned=True,
        status="ready",
        active_action="battery_charge",
        current_action="grid_support_and_charge",
        slot_seconds_remaining=300.0,
        latched_seconds_remaining=300.0,
        continue_eligible=True,
        stable_seconds=0.0,
        active_seconds=300.0,
    )
    assert active_tariff_update_allowed(**tariff_live)
    for changed in (
        {"planned": False},
        {"status": "no_charge_needed"},
        {"current_action": "grid_support"},
        {"slot_seconds_remaining": 0.0},
        {"latched_seconds_remaining": 0.0},
        {"continue_eligible": False, "stable_seconds": 120.0},
    ):
        scenario = tariff_live | changed
        assert not active_tariff_update_allowed(**scenario), (
            "A tariff run changed during ACK but could still extend/write"
        )
    transient_withdrawal = tariff_live | {
        "continue_eligible": False,
        "stable_seconds": 119.9,
    }
    assert active_tariff_update_allowed(**transient_withdrawal)
    assert "A lower maximum SOC chosen during an active" in scheduler
    assert "input_number.hoymiles_tariff_maximum_soc" in scheduler.split(
        "id: hoymiles_tariff_grid_charge_control", 1
    )[1].split("actions:", 1)[0]
    stop_block = scheduler.split(
        "# Stop: koniec zapamiętanego ciągłego okna",
        1,
    )[1].split("id: hoymiles_rce_grid_discharge_control", 1)[0]
    assert "'current_run_continue_eligible')" in stop_block
    assert "is not sameas true" in stop_block
    assert "'current_run_start_eligible') is not sameas true" not in stop_block
    assert "'current_run_intent_stable_seconds') | float(0))" in stop_block
    assert "'input_number.hoymiles_tariff_maximum_soc'" in stop_block
    for marker in (
        "'no_charge_needed'",
        "'soc_limits_conflict'",
        "'hard_reserve_unavailable'",
        "'bms_charge_data_fresh'",
        "'bms_charge_available'",
        "is not sameas true",
    ):
        assert marker in stop_block, f"Tariff hard stop lacks {marker}"
    stabilized_stop = stop_block.split(
        "current_run_intent_stable_seconds", 1
    )[1]
    assert "current_run_continue_eligible" in stabilized_stop
    assert "== 'grid_support'" not in stabilized_stop, (
        "Required-charge runs ignore a stable withdrawn plan"
    )
    tariff_choose = scheduler.split(
        "id: hoymiles_tariff_grid_charge_control", 1
    )[1].split("id: hoymiles_rce_grid_discharge_control", 1)[0]
    conflict_pos = tariff_choose.index(
        "A value below the protected reserve first"
    )
    clamp_pos = tariff_choose.index(
        "and (maximum | float(0)) < (latched | float(0))"
    )
    update_pos = tariff_choose.index(
        "# Aktualizacja ograniczeń BMS podczas bieżącego bloku"
    )
    assert conflict_pos < clamp_pos < update_pos, (
        "Maximum-SOC conflict handling is shadowed by a later choose branch"
    )
    assert "current_run_continue_eligible') is not sameas true" in update_block
    continue_gate = update_block.split(
        "current_run_continue_eligible", 1
    )[0].rsplit("{{ not (", 1)[-1]
    assert "== 'grid_support'" not in continue_gate
    assert "current_run_start_eligible') is not sameas true" not in update_block
    assert "current_run_intent_stable_seconds') | float(0)) >= 120" in update_block

    # Pure grid support must set an integer Force Charge target strictly below
    # the starting SOC, at/below maximum SOC and not below the protected floor.
    assert "tariff_start_soc | round(0, 'floor')) - 1" in start_block
    assert "tariff_start_maximum_soc" in start_block
    assert "tariff_start_reserve_soc" in start_block
    assert "[[[below_live, maximum] | min, reserve] | max, 0]" in start_block
    old_grid_support_target = (
        "states('sensor.hoymiles_hit_overview_battery_soc')\n"
        "                         | float(0) | round(0, 'floor')"
    )
    assert old_grid_support_target not in start_block

    sensor_source = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "tariff_sensor.py"
    ).read_text(encoding="utf-8")
    for attribute in (
        "current_run_start_eligible",
        "current_run_suppression_reason",
        "current_run_continue_eligible",
        "current_run_continue_reason",
        "current_run_grid_import_kwh",
        "current_run_direct_load_kwh",
        "current_run_stored_kwh",
        "current_run_benefit_pln",
        "current_run_remaining_minutes",
        "current_run_intent_stable_seconds",
        "command_charge_power_percent",
        "modeled_effective_charge_power_percent",
    ):
        assert f'"{attribute}"' in sensor_source, (
            f"Tariff scheduler/sensor execution contract lacks {attribute}"
        )
    assert "effective_charge_power_percent" in sensor_source, (
        "The public legacy command alias disappeared during v1.5.2"
    )
    tariff_control = scheduler.split(
        "id: hoymiles_tariff_grid_charge_control",
        1,
    )[1].split("id: hoymiles_rce_grid_discharge_control", 1)[0]
    assert "command_charge_power_percent" in tariff_control
    assert "effective_charge_power_percent" not in tariff_control, (
        "The scheduler must consume the explicit command, not the modeled "
        "delivered-power diagnostic"
    )

    intent_block = sensor_source.split(
        "current_run_intent = (",
        1,
    )[1].split(
        "if current_run_intent !=",
        1,
    )[0]
    assert "result.current_run_start_eligible" in intent_block, (
        "Grid-support eligibility changes do not reset intent stability"
    )
    assert "result.current_run_suppression_reason" in intent_block, (
        "Grid-support suppression changes do not reset intent stability"
    )

    optimizer_source = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "tariff_optimizer.py"
    ).read_text(encoding="utf-8")
    continue_block = optimizer_source.split(
        "# Continuing an already active support run",
        1,
    )[1].split(
        "        live_values = (",
        1,
    )[0]
    assert "settings.current_load_power_kw" in continue_block
    assert "settings.current_pv_power_kw" in continue_block
    assert "settings.current_battery_power_kw" not in continue_block, (
        "Zero battery discharge after entering Grid Charge must not abort support"
    )
    assert 'current_run_continue_reason = "live_data_missing"' in continue_block
    assert 'current_run_continue_reason = "pv_covers_load"' in continue_block
    assert 'current_run_continue_reason = "eligible"' in continue_block


def assert_rce_execution_contracts() -> None:
    """Protect atomic RCE start and a monotonic accepted discharge block."""

    scheduler = (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
    ).read_text(encoding="utf-8")
    block = scheduler.split(
        "id: hoymiles_rce_grid_discharge_control",
        1,
    )[1].split(
        "id: hoymiles_automatic_ems_control_failsafe",
        1,
    )[0]
    start = block.split("# Start RCE:", 1)[1].split("# Stop RCE:", 1)[0]
    stop = block.split("# Stop RCE:", 1)[1]

    for attribute in (
        "current_slot_planned",
        "current_slot_end",
        "current_run_end",
        "current_slot_remaining_minutes",
        "current_slot_start_eligible",
        "current_required_minimum_soc_percent",
    ):
        assert f"'{attribute}'" in block, (
            f"RCE scheduler does not consume {attribute}"
        )
    power_template = scheduler.split(
        'name: "Hoymiles RCE Effective Discharge Power Percent"', 1
    )[1].split('name: "Hoymiles RCE Revenue Rate"', 1)[0]
    for marker in (
        "current_slot_execution_power_percent",
        "current_slot_execution_export_power_kw",
        "current_slot_execution_discharge_power_kw",
        "[requested, bms_percent, planned, 100] | min",
    ):
        assert marker in power_template, (
            f"RCE execution power is not bounded by the plan: {marker}"
        )
    for marker in (
        "input_datetime.hoymiles_rce_latched_slot_end",
        "input_number.hoymiles_rce_latched_minimum_soc",
        "# The Force Discharge floor is monotonic",
        "[latched, register,",
        "| max | round(0, 'ceil')",
    ):
        assert marker in block, f"RCE monotonic latch lacks {marker}"
    assert "end > as_timestamp(states(" in block, (
        "A live refresh can shorten the accepted RCE deadline"
    )
    active_power_guard = block.split(
        "# Re-evaluate the complete active-run authorization before the first", 1
    )[1].split("# Latch the complete contiguous run", 1)[0]
    active_power_write = active_power_guard.index(
        "script.hoymiles_verified_set_ems_maximum_discharge_power"
    )
    for marker in (
        "input_boolean.hoymiles_rce_discharge_active",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "binary_sensor.hoymiles_sale_block_active",
        "binary_sensor.hoymiles_ems_export_allowed",
        "binary_sensor.hoymiles_rce_reserve_ready",
        "binary_sensor.hoymiles_rce_control_data_ready",
        "binary_sensor.hoymiles_ems_control_conflict",
        "sensor.hoymiles_ems_hardware_mode",
        "current_slot_planned",
        "current_slot_continue_eligible",
        "input_datetime.hoymiles_rce_latched_slot_end",
        "script.hoymiles_rollback_rce_transaction",
    ):
        assert marker in active_power_guard
        assert active_power_guard.index(marker) < active_power_write

    active_power_update = active_power_guard[active_power_write:]
    frozen_target = active_power_guard.index("rce_power_update_target")
    assert frozen_target < active_power_write
    for marker in (
        'value: "{{ rce_power_update_target }}"',
        "rce_power_update_generation_before",
        "rce_power_update_generation_after_helper",
        "rce_power_update_helper_saw_new_generation",
        "rce_power_update_exact_ack_after_helper",
        "rce_power_update_generation_newer_than_recovery",
        "rce_power_update_full_block_current",
        "rce_power_update_live_authorized",
        "rce_power_update_within_live_safe_cap",
        "rce_power_update_safe_under_command",
        "rce_power_update_target | float(-998)",
        "rce_power_update_live_safe_target | float(0)]",
        "| min + 0.5",
        "or newer or not authorized",
        "script.hoymiles_rollback_rce_transaction",
        "16000000",
    ):
        assert marker in active_power_update, (
            f"RCE active 4306 recovery lacks {marker}"
        )
    exact_ack = active_power_update.split(
        "rce_power_update_exact_ack_after_helper:", 1
    )[1].split("# The helper already spent", 1)[0]
    assert "sensor.hoymiles_rce_effective_discharge_power_percent" not in exact_ack, (
        "A moving live target can still redefine the frozen physical ACK"
    )
    assert "rce_power_update_current_generation" in exact_ack
    assert "rce_power_update_generation_before" in exact_ack
    assert "rce_power_update_full_block_current" in exact_ack
    assert "rce_power_update_target" in exact_ack

    def active_4306_outcome(
        *,
        exact_physical_ack: bool,
        helper_saw_new_generation: bool,
        newer_recovery_generation: bool,
        full_block_preserved: bool,
        live_authorized: bool,
        physical_4306: float,
        frozen_target: float,
        live_safe_target: float,
    ) -> str:
        """Model the reviewed asymmetric active-4306 decision contract."""

        within_live_cap = physical_4306 <= live_safe_target + 0.5
        if exact_physical_ack and live_authorized and within_live_cap:
            return "continue"
        safe_under_command = (
            not helper_saw_new_generation
            and newer_recovery_generation
            and full_block_preserved
            and live_authorized
            and physical_4306 > 0
            and physical_4306 <= min(frozen_target, live_safe_target) + 0.5
        )
        return "defer" if safe_under_command else "rollback"

    assert active_4306_outcome(
        exact_physical_ack=True,
        helper_saw_new_generation=True,
        newer_recovery_generation=False,
        full_block_preserved=True,
        live_authorized=True,
        physical_4306=17.4,
        frozen_target=17.4,
        live_safe_target=18.0,
    ) == "continue"
    assert active_4306_outcome(
        exact_physical_ack=False,
        helper_saw_new_generation=False,
        newer_recovery_generation=True,
        full_block_preserved=True,
        live_authorized=True,
        physical_4306=15.6,
        frozen_target=17.4,
        live_safe_target=18.0,
    ) == "defer"
    assert active_4306_outcome(
        exact_physical_ack=False,
        helper_saw_new_generation=False,
        newer_recovery_generation=False,
        full_block_preserved=True,
        live_authorized=True,
        physical_4306=15.6,
        frozen_target=17.4,
        live_safe_target=18.0,
    ) == "rollback"
    assert active_4306_outcome(
        exact_physical_ack=False,
        helper_saw_new_generation=False,
        newer_recovery_generation=True,
        full_block_preserved=True,
        live_authorized=True,
        physical_4306=15.6,
        frozen_target=12.0,
        live_safe_target=12.0,
    ) == "rollback"
    assert active_4306_outcome(
        exact_physical_ack=False,
        helper_saw_new_generation=False,
        newer_recovery_generation=True,
        full_block_preserved=True,
        live_authorized=True,
        physical_4306=11.8,
        frozen_target=12.0,
        live_safe_target=12.0,
    ) == "defer"
    for full_block_preserved, live_authorized in ((False, True), (True, False)):
        assert active_4306_outcome(
            exact_physical_ack=False,
            helper_saw_new_generation=False,
            newer_recovery_generation=True,
            full_block_preserved=full_block_preserved,
            live_authorized=live_authorized,
            physical_4306=15.6,
            frozen_target=17.4,
            live_safe_target=18.0,
        ) == "rollback"
    assert active_4306_outcome(
        exact_physical_ack=True,
        helper_saw_new_generation=True,
        newer_recovery_generation=True,
        full_block_preserved=True,
        live_authorized=True,
        physical_4306=15.6,
        frozen_target=15.6,
        live_safe_target=12.0,
    ) == "rollback"
    assert active_4306_outcome(
        exact_physical_ack=False,
        helper_saw_new_generation=True,
        newer_recovery_generation=True,
        full_block_preserved=True,
        live_authorized=True,
        physical_4306=15.6,
        frozen_target=17.4,
        live_safe_target=18.0,
    ) == "rollback"
    deadline_extension = block.split(
        "# Latch the complete contiguous run", 1
    )[1].split("# The Force Discharge floor", 1)[0]
    deadline_write = deadline_extension.index(
        "action: input_datetime.set_datetime"
    )
    for marker in (
        "input_boolean.hoymiles_rce_discharge_active",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "binary_sensor.hoymiles_ems_control_conflict",
        "binary_sensor.hoymiles_rce_control_data_ready",
        "binary_sensor.hoymiles_sale_block_active",
        "binary_sensor.hoymiles_ems_export_allowed",
        "binary_sensor.hoymiles_rce_reserve_ready",
        "current_slot_planned",
        "current_slot_continue_eligible",
        "input_datetime.hoymiles_rce_latched_slot_end",
        "entity_id: sensor.hoymiles_ems_hardware_mode",
        'state: "grid_discharge"',
    ):
        assert marker in deadline_extension
        assert deadline_extension.index(marker) < deadline_write
    monotonic_floor = block.split(
        "# The Force Discharge floor is monotonic", 1
    )[1].split("\n      - choose:", 1)[0]
    floor_write = monotonic_floor.index(
        "action: script.hoymiles_verified_set_ems_force_discharge_soc"
    )
    assert monotonic_floor.count(
        "entity_id: sensor.hoymiles_ems_hardware_mode"
    ) >= 2, "RCE 4305 floor lacks outer and final physical-mode guards"
    assert monotonic_floor.count(
        "entity_id: binary_sensor.hoymiles_rce_control_data_ready"
    ) >= 2, "RCE 4305 floor lacks outer and final readiness guards"
    final_floor_guard = monotonic_floor.index(
        "# Therefore re-read both physical mode and source readiness directly"
    )
    assert final_floor_guard < floor_write
    for marker in (
        "input_boolean.hoymiles_rce_discharge_enabled",
        "binary_sensor.hoymiles_ems_control_conflict",
        "binary_sensor.hoymiles_sale_block_active",
        "binary_sensor.hoymiles_ems_export_allowed",
        "binary_sensor.hoymiles_rce_reserve_ready",
        "current_slot_planned",
        "current_slot_continue_eligible",
        "input_datetime.hoymiles_rce_latched_slot_end",
    ):
        assert marker in monotonic_floor[final_floor_guard:floor_write]
    assert "Modbus 4305 floor write" in monotonic_floor
    assert "current_slot_planned" in start
    assert "current_slot_start_eligible" in start
    assert "current_slot_remaining_minutes" in start
    assert "# Final, fresh interlock" in start
    assert "last_reported" in start
    for marker in (
        "rce_start_run_end",
        "plan_age >= 0 and plan_age <= 120",
        "soc_age >= 0 and soc_age <= 120",
        "floor_age >= 0 and floor_age <= 120",
        "fresh_required",
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
        "sensor.hoymiles_hit_overview_battery_soc",
    ):
        assert marker in start, f"RCE fresh start interlock lacks {marker}"
    assert "current_run_end" in start
    rce_mode_command = start.split(
        "# Ownership is already active, so the first export sample",
        1,
    )[1].split("- wait_template:", 1)[0]
    assert "not is_state(" in rce_mode_command
    assert "'sensor.hoymiles_ems_hardware_mode', 'grid_discharge'" in rce_mode_command
    assert "hoymiles_hit_rcm_voltage_plan" not in rce_mode_command, (
        "RCE mode start accidentally depends on the RCEm pre-discharge plan"
    )
    assert "pre_discharge_" not in rce_mode_command
    assert "current_slot_end')" not in block.split(
        "# Latch the complete contiguous run", 1
    )[1].split("# The Force Discharge floor", 1)[0]
    assert "binary_sensor.hoymiles_ems_export_allowed" in start
    assert "['off_grid', 'unknown', 'unavailable', 'none']" in start, (
        "Automatic RCE discharge may override a physical Off-Grid mode"
    )
    assert "rce_start_minimum_soc" in start
    assert "[required, register] | max" in start
    start_followup_guard = start.split(
        "# The 4306 ACK above may outlive this start decision.", 1
    )[1].split("- wait_template:", 1)[0]
    start_followup_write = start_followup_guard.index(
        "script.hoymiles_verified_set_ems_force_discharge_soc"
    )
    for marker in (
        "input_boolean.hoymiles_rce_discharge_active",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "binary_sensor.hoymiles_ems_control_conflict",
        "binary_sensor.hoymiles_rce_control_data_ready",
        "binary_sensor.hoymiles_sale_block_active",
        "binary_sensor.hoymiles_ems_export_allowed",
        "binary_sensor.hoymiles_rce_reserve_ready",
        "current_slot_planned",
        "current_slot_start_eligible",
        "current_slot_end",
        "not in ['off_grid', 'unknown', 'unavailable'",
        "script.hoymiles_rollback_rce_transaction",
    ):
        assert marker in start_followup_guard
    assert start_followup_guard.index(
        "binary_sensor.hoymiles_rce_control_data_ready"
    ) < start_followup_write
    assert start_followup_guard.index(
        "binary_sensor.hoymiles_ems_control_conflict"
    ) < start_followup_write
    assert start_followup_guard.index(
        "not in ['off_grid', 'unknown', 'unavailable'"
    ) < start_followup_write
    final_start_guard = start.split(
        "# Final, fresh interlock after every owned Modbus setting", 1
    )[1].split(
        "# Ownership is already active, so the first export sample", 1
    )[0]
    assert "binary_sensor.hoymiles_ems_control_conflict" in final_start_guard
    frozen_target_drift_check = (
        "sensor.hoymiles_rce_effective_discharge_power_percent') "
        "| float(-999)) - (rce_start_power"
    )
    assert frozen_target_drift_check not in " ".join(final_start_guard.split())
    post_mode_start_guard = start.split(
        "# A successful mode ACK is not permission to accept an obsolete", 1
    )[1].split(
        "# A recalculation withdraws planning authority", 1
    )[0]
    for marker in (
        "binary_sensor.hoymiles_ems_control_conflict",
        "input_boolean.hoymiles_rce_discharge_active",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "binary_sensor.hoymiles_rce_control_data_ready",
        "binary_sensor.hoymiles_ems_export_allowed",
        "binary_sensor.hoymiles_sale_block_active",
        "binary_sensor.hoymiles_rce_reserve_ready",
        "current_slot_planned",
        "current_slot_continue_eligible",
        "current_run_end",
        "rce_start_power",
        "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
        "sensor.hoymiles_hit_overview_battery_soc",
        'state: "grid_discharge"',
        "script.hoymiles_rollback_rce_transaction",
    ):
        assert marker in post_mode_start_guard, (
            f"RCE post-mode ACK guard lacks {marker}"
        )
    assert "current_slot_start_eligible" not in post_mode_start_guard, (
        "A physically confirmed RCE cycle still depends on new-start eligibility"
    )
    post_mode_guard_normalized = " ".join(post_mode_start_guard.split())
    assert frozen_target_drift_check not in post_mode_guard_normalized
    assert (
        "'current_slot_continue_eligible') is sameas true"
        in post_mode_guard_normalized
    )
    assert "current_slot_continue_stable_seconds" not in post_mode_start_guard
    assert (
        "sensor.hoymiles_rce_effective_discharge_power_percent') | float(0)) > 0"
        in post_mode_guard_normalized
    )

    def rce_post_ack_slot_authorized(
        *,
        planned: bool,
        start_eligible: bool,
        continue_eligible: bool,
        slot_seconds_remaining: float,
    ) -> bool:
        del start_eligible
        return planned and continue_eligible and slot_seconds_remaining > 0

    assert rce_post_ack_slot_authorized(
        planned=True,
        start_eligible=False,
        continue_eligible=True,
        slot_seconds_remaining=249.0,
    ), "The under-five-minute new-start gate rolled back a confirmed RCE cycle"
    assert not rce_post_ack_slot_authorized(
        planned=True,
        start_eligible=True,
        continue_eligible=False,
        slot_seconds_remaining=249.0,
    ), "Loss of continuation eligibility did not stop an ACK-confirmed cycle"
    assert not rce_post_ack_slot_authorized(
        planned=False,
        start_eligible=True,
        continue_eligible=True,
        slot_seconds_remaining=249.0,
    ), "Loss of the planned slot did not stop an ACK-confirmed cycle"
    assert rce_post_ack_slot_authorized(
        planned=True,
        start_eligible=False,
        continue_eligible=True,
        slot_seconds_remaining=1800.0,
    ), "A consecutive planned slot boundary rolled back the active RCE run"
    assert not rce_post_ack_slot_authorized(
        planned=False,
        start_eligible=False,
        continue_eligible=True,
        slot_seconds_remaining=0.0,
    ), "The end of the last planned slot did not stop RCE"

    def active_rce_update_allowed(
        enabled: bool,
        sale_block: bool,
        export_allowed: bool,
        reserve_ready: bool,
        data_ready: bool,
        conflict: bool,
        physical_mode: str,
        planned: bool,
        latched_seconds_remaining: float,
        continue_eligible: bool,
    ) -> bool:
        return (
            enabled
            and not sale_block
            and export_allowed
            and reserve_ready
            and data_ready
            and not conflict
            and physical_mode == "grid_discharge"
            and planned
            and latched_seconds_remaining > 0
            and continue_eligible
        )

    rce_live = dict(
        enabled=True,
        sale_block=False,
        export_allowed=True,
        reserve_ready=True,
        data_ready=True,
        conflict=False,
        physical_mode="grid_discharge",
        planned=True,
        latched_seconds_remaining=300.0,
        continue_eligible=True,
    )
    assert active_rce_update_allowed(**rce_live)
    for changed in (
        {"enabled": False},
        {"sale_block": True},
        {"export_allowed": False},
        {"reserve_ready": False},
        {"data_ready": False},
        {"conflict": True},
        {"physical_mode": "off_grid"},
        {"planned": False},
        {"latched_seconds_remaining": 0.0},
        {"continue_eligible": False},
    ):
        assert not active_rce_update_allowed(**(rce_live | changed)), (
            "RCE interleaving can write or extend after authorization loss"
        )

    # Planning authority for a new start remains distinct from the execution
    # authority of an already accepted, latched cycle.  A pending calculation
    # may hold that cycle only while current physical safety inputs independently
    # remain valid; it must never authorize a write or hide a hard stop.
    def rce_lifecycle_action(
        *,
        active: bool,
        enabled: bool,
        result_current: bool,
        recalculation_pending: bool,
        control_data_ready: bool | None,
        plan_age_seconds: float | None,
        execution_ready: bool,
        export_allowed: bool,
        sale_block: bool,
        conflict: bool,
        owner_exclusive: bool,
        physical_mode: str,
        topology_valid: bool,
        latched_seconds_remaining: float,
        planned: bool,
        committed_power_percent: float | None,
        physical_power_percent: float | None,
        price_above_floor: bool,
        continue_eligible: bool | None,
        soc_percent: float | None,
        floor_percent: float | None,
        latched_floor_percent: float | None,
        soc_fresh: bool,
        bms_current_a: float | None,
        bms_voltage_v: float | None,
        bms_fresh: bool,
        readbacks_fresh: bool,
    ) -> str:
        live_safety = (
            enabled
            and execution_ready
            and export_allowed
            and not sale_block
            and not conflict
            and owner_exclusive
            and physical_mode == "grid_discharge"
            and topology_valid
            and soc_fresh
            and soc_percent is not None
            and floor_percent is not None
            and soc_percent > floor_percent
            and bms_fresh
            and bms_current_a is not None
            and bms_current_a > 0
            and bms_voltage_v is not None
            and bms_voltage_v > 0
            and readbacks_fresh
            and committed_power_percent is not None
            and physical_power_percent is not None
            and physical_power_percent <= committed_power_percent + 0.5
            and floor_percent is not None
            and latched_floor_percent is not None
            and floor_percent >= latched_floor_percent - 0.5
        )
        committed_execution = (
            latched_seconds_remaining > 0
            and planned
            and committed_power_percent is not None
            and committed_power_percent > 0
            and price_above_floor
            and continue_eligible is True
        )
        if not active:
            return "unowned"
        if not live_safety or not committed_execution:
            return "rollback"
        optimizer_pending = not result_current and recalculation_pending
        current_cohort_settling = (
            result_current
            and not recalculation_pending
            and control_data_ready is False
            and plan_age_seconds is not None
            and 0 <= plan_age_seconds <= 5
        )
        if optimizer_pending or current_cohort_settling:
            return "hold"
        if not result_current or not control_data_ready:
            return "rollback"
        return "continue"

    rce_latched = dict(
        active=True,
        enabled=True,
        result_current=True,
        recalculation_pending=False,
        control_data_ready=True,
        plan_age_seconds=0.0,
        execution_ready=True,
        export_allowed=True,
        sale_block=False,
        conflict=False,
        owner_exclusive=True,
        physical_mode="grid_discharge",
        topology_valid=True,
        latched_seconds_remaining=300.0,
        planned=True,
        committed_power_percent=50.0,
        physical_power_percent=50.0,
        price_above_floor=True,
        continue_eligible=True,
        soc_percent=70.0,
        floor_percent=30.0,
        latched_floor_percent=30.0,
        soc_fresh=True,
        bms_current_a=500.0,
        bms_voltage_v=53.0,
        bms_fresh=True,
        readbacks_fresh=True,
    )
    harmless_recalculation = rce_latched | {
        "result_current": False,
        "recalculation_pending": True,
        "control_data_ready": False,
    }
    current_cohort_settling = rce_latched | {
        "control_data_ready": False,
        "plan_age_seconds": 0.0,
    }
    harmless_event_orders = (
        harmless_recalculation,
        current_cohort_settling,
    )
    lifecycle_sequence = [rce_lifecycle_action(**rce_latched)] + [
        rce_lifecycle_action(**event) for event in harmless_event_orders
    ] + [rce_lifecycle_action(**rce_latched)]
    assert lifecycle_sequence == ["continue", "hold", "hold", "continue"], (
        "Plan/readiness event order changed the active pending-cycle decision"
    )
    assert "rollback" not in lifecycle_sequence
    assert rce_lifecycle_action(**harmless_recalculation) == "hold", (
        "A harmless price/PV/LOAD recalculation rolled back a latched RCE cycle"
    )
    assert rce_lifecycle_action(**current_cohort_settling) == "hold", (
        "A current plan rolled back before its derived readiness cohort settled"
    )
    assert rce_lifecycle_action(
        **(current_cohort_settling | {"plan_age_seconds": 5.1})
    ) == "rollback", "A missing derived readiness cohort was held without a bound"
    assert rce_lifecycle_action(
        **(current_cohort_settling | {"control_data_ready": None})
    ) == "rollback", "Unavailable derived readiness was treated as cohort settling"
    assert rce_lifecycle_action(**rce_latched) == "continue", (
        "A compatible committed replacement plan did not resume normal continuation"
    )
    assert rce_lifecycle_action(
        **(rce_latched | {"planned": False})
    ) == "rollback", "A current replacement plan requiring stop was ignored"
    assert rce_lifecycle_action(
        **(rce_latched | {"continue_eligible": False})
    ) == "rollback", "Lost continuation eligibility did not stop active RCE"
    assert rce_lifecycle_action(
        **(harmless_recalculation | {"bms_current_a": 0.0})
    ) == "rollback", "BMS zero was hidden by optimizer pending"
    assert rce_lifecycle_action(
        **(harmless_recalculation | {"bms_current_a": None})
    ) == "rollback", "Missing BMS capability was hidden by optimizer pending"
    assert rce_lifecycle_action(
        **(harmless_recalculation | {"bms_fresh": False})
    ) == "rollback", "Stale BMS capability was hidden by optimizer pending"
    assert rce_lifecycle_action(
        **(harmless_recalculation | {"export_allowed": False})
    ) == "rollback", "Physical export prohibition was hidden by optimizer pending"
    assert rce_lifecycle_action(
        **(
            harmless_recalculation
            | {"bms_current_a": 0.0, "export_allowed": False}
        )
    ) == "rollback", (
        "A simultaneous BMS/GCF hard stop waited for a replacement optimizer result"
    )
    for hard_stop in (
        {"conflict": True},
        {"owner_exclusive": False},
        {"physical_mode": "off_grid"},
        {"execution_ready": False},
        {"topology_valid": False},
        {"soc_percent": 30.0},
        {"soc_fresh": False},
        {"readbacks_fresh": False},
        {"physical_power_percent": 100.0},
        {"floor_percent": 29.0},
    ):
        assert rce_lifecycle_action(
            **(harmless_recalculation | hard_stop)
        ) == "rollback", f"Pending RCE masked hard stop {hard_stop}"
    assert rce_lifecycle_action(
        **(harmless_recalculation | {"physical_power_percent": 25.0})
    ) == "hold", "A safer downward physical power cap was treated as drift"
    assert rce_lifecycle_action(
        **(harmless_recalculation | {"active": False})
    ) == "unowned", (
        "An unowned physical Grid Discharge was misclassified as an RCE cycle"
    )

    def rce_new_start_allowed(
        *,
        active: bool,
        result_current: bool,
        recalculation_pending: bool,
        control_data_ready: bool,
        physical_mode: str,
        planned: bool,
        start_eligible: bool,
    ) -> bool:
        return (
            not active
            and result_current
            and not recalculation_pending
            and control_data_ready
            and physical_mode == "self_use"
            and planned
            and start_eligible
        )

    assert rce_new_start_allowed(
        active=False,
        result_current=True,
        recalculation_pending=False,
        control_data_ready=True,
        physical_mode="self_use",
        planned=True,
        start_eligible=True,
    )
    assert not rce_new_start_allowed(
        active=False,
        result_current=True,
        recalculation_pending=False,
        control_data_ready=True,
        physical_mode="self_use",
        planned=True,
        start_eligible=False,
    ), "start_eligible=false authorized a new pre-ACK cycle"
    assert not rce_new_start_allowed(
        active=False,
        result_current=False,
        recalculation_pending=True,
        control_data_ready=False,
        physical_mode="self_use",
        planned=True,
        start_eligible=True,
    ), "A pending optimizer result authorized a new RCE start"

    def cohort_transition(
        *,
        active: bool = True,
        ack_confirmed: bool = True,
        owner_rce: bool = True,
        conflict: bool = False,
        physical_mode: str = "grid_discharge",
        result_current: bool | None = True,
        recalculation_pending: bool = False,
        control_data_ready: bool | None = False,
        revision: int = 100,
        revision_started_at: float = 1000.0,
        now_epoch: float = 1000.0,
        hard_stop: bool = False,
        planned: bool = True,
        continue_eligible: bool = True,
        latched_target_kw: float = 32.0,
    ) -> tuple[str, int, float]:
        """Mirror the bounded, no-write cohort transition contract."""

        del revision
        if not active or not ack_confirmed:
            return "rollback", 0, latched_target_kw
        if not result_current and recalculation_pending:
            return "optimizer_pending", 0, latched_target_kw
        if control_data_ready is True and result_current is True:
            return "continue", 0, latched_target_kw
        age = now_epoch - revision_started_at
        bridge = (
            owner_rce
            and not conflict
            and physical_mode == "grid_discharge"
            and result_current is True
            and not recalculation_pending
            and control_data_ready is False
            and not hard_stop
            and planned
            and continue_eligible
            and 0 <= age <= 5
        )
        return (
            ("bridge" if bridge else "rollback"),
            0,
            latched_target_kw,
        )

    assert cohort_transition(control_data_ready=True)[0] == "continue"
    assert cohort_transition(now_epoch=1004.0)[0] == "bridge"
    assert cohort_transition(now_epoch=1004.0, control_data_ready=True)[0] == (
        "continue"
    )
    assert cohort_transition(now_epoch=1005.001)[0] == "rollback"
    assert cohort_transition(result_current=None)[0] == "rollback"
    assert cohort_transition(
        result_current=False,
        recalculation_pending=True,
    )[0] == "optimizer_pending"
    assert cohort_transition(hard_stop=True)[0] == "rollback"
    assert cohort_transition(conflict=True)[0] == "rollback"
    assert cohort_transition(physical_mode="self_use")[0] == "rollback"
    same_revision_after_toggle = cohort_transition(
        revision=100,
        revision_started_at=1000.0,
        now_epoch=1006.0,
        control_data_ready=False,
    )
    assert same_revision_after_toggle[0] == "rollback", (
        "The same source revision renewed its five-second bridge"
    )
    new_revision = cohort_transition(
        revision=101,
        revision_started_at=1006.0,
        now_epoch=1006.0,
    )
    assert new_revision[0] == "bridge"
    for transition in (
        cohort_transition(now_epoch=1004.0),
        same_revision_after_toggle,
        new_revision,
    ):
        assert transition[1] == 0, "Cohort bridge issued a service/write call"
        assert transition[2] == 32.0, "Cohort bridge changed the latched target"

    # Reproduce the accepted field chronology without wall-clock sleeps.  The
    # last transition is a normal end of the planned run, not a false rollback.
    field_lifecycle = [
        "start",
        "physical_ack",
        "verifier_confirmed",
        cohort_transition(now_epoch=1001.0)[0],
        cohort_transition(now_epoch=1002.0, control_data_ready=True)[0],
        (
            "continue"
            if rce_post_ack_slot_authorized(
                planned=True,
                start_eligible=False,
                continue_eligible=True,
                slot_seconds_remaining=120.0,
            )
            else "rollback"
        ),
        (
            "continue"
            if rce_post_ack_slot_authorized(
                planned=True,
                start_eligible=False,
                continue_eligible=True,
                slot_seconds_remaining=900.0,
            )
            else "rollback"
        ),
        "active_at_30_minutes",
        (
            "rollback"
            if rce_post_ack_slot_authorized(
                planned=False,
                start_eligible=False,
                continue_eligible=False,
                slot_seconds_remaining=0.0,
            )
            else "normal_stop"
        ),
    ]
    assert field_lifecycle == [
        "start",
        "physical_ack",
        "verifier_confirmed",
        "bridge",
        "continue",
        "continue",
        "continue",
        "active_at_30_minutes",
        "normal_stop",
    ]
    assert "rollback" not in field_lifecycle

    pending_marker = "rce_pending_latched_execution_safe"
    pending_stop = (
        "RCE synchronizuje kohortę planu; aktywny zatwierdzony cykl "
        "pozostaje bez zmian"
    )
    assert scheduler.count(f"&{pending_marker}") == 1
    assert scheduler.count(f"*{pending_marker}") == 5
    assert scheduler.count(pending_stop) == 4
    pending_source = scheduler.split(f"&{pending_marker}", 1)[1].split(
        "sequence:", 1
    )[0]
    pending_normalized = " ".join(pending_source.split())
    pending_compact = "".join(pending_source.split())
    assert "{% set plan_reported = plan.last_updated %}" in pending_source
    assert "rce_control_data_ready.last_updated" not in pending_source, (
        "Readiness toggles, rather than a source revision, renew the bridge"
    )
    assert "result_current is sameas false" in pending_normalized
    assert "recalculation_pending is sameas true" in pending_normalized
    assert "current_cohort_settling" in pending_normalized
    assert "plan_age >= 0 and plan_age <= 5" in pending_normalized
    assert "binary_sensor.hoymiles_rce_control_data_ready" in pending_normalized
    assert "<= (committed_power | float(0)) + 0.5" in pending_normalized
    assert ">= (latched_floor | float(100)) - 0.5" in pending_normalized
    for exact_contract in (
        "result_currentissameasfalse",
        "recalculation_pendingissameastrue",
        "result_currentissameastrue",
        "recalculation_pendingissameasfalse",
        "is_state('binary_sensor.hoymiles_rce_control_data_ready','off')",
        "plan_age>=0andplan_age<=5",
        "optimizer_pendingorcurrent_cohort_settling",
        "is_state('input_boolean.hoymiles_rce_discharge_active','on')",
        "is_state('input_boolean.hoymiles_rce_discharge_enabled','on')",
        "is_state('binary_sensor.hoymiles_ems_execution_ready','on')",
        "is_state('binary_sensor.hoymiles_ems_export_allowed','on')",
        "is_state('binary_sensor.hoymiles_sale_block_active','off')",
        "is_state('binary_sensor.hoymiles_ems_control_conflict','off')",
        "is_state('sensor.hoymiles_ems_hardware_mode','grid_discharge')",
        "is_state('input_boolean.hoymiles_tariff_charge_enabled','off')",
        "is_state('input_boolean.hoymiles_tariff_charge_active','off')",
        "is_state('input_boolean.hoymiles_rcm_active','off')",
        "is_state('input_boolean.hoymiles_rcm_pre_discharge_active','off')",
        "is_state('input_boolean.hoymiles_rcm_export_control_active','off')",
        "(is_state('input_boolean.hoymiles_rcm_enabled','off')"
        "oris_state('input_boolean.hoymiles_rcm_shadow_mode','on'))",
        "is_state('input_boolean.hoymiles_battery_balancing_active','off')",
        "is_state('input_boolean.hoymiles_discharge_cycle_active','off')",
        "is_state('input_boolean.hoymiles_charge_cycle_active','off')",
        "notis_state('timer.hoymiles_discharge','active')",
        "notis_state('timer.hoymiles_charge','active')",
        "(bms_current.state|float(0))>0",
        "(bms_voltage.state|float(0))>0",
        "current_age>=-5andcurrent_age<=300",
        "voltage_age>=-5andvoltage_age<=300",
        "soc_age>=-5andsoc_age<=120",
        "floor_age>=-5andfloor_age<=180",
        "power_age>=-5andpower_age<=180",
        "as_timestamp(states('input_datetime.hoymiles_rce_latched_slot_end'),0)"
        ">now_ts",
        "'current_slot_planned')|default(false,true)",
        "continue_eligibleissameastrue",
        "andnotprice_stop",
        "(power.state|float(999))<=(committed_power|float(0))+0.5",
        "(floor.state|float(-999))>=(latched_floor|float(100))-0.5",
        "(soc.state|float(0))>[latched_floor|float(100),"
        "floor.state|float(100)]|max",
    ):
        assert exact_contract in pending_compact, (
            f"RCE pending hard-stop polarity changed: {exact_contract}"
        )
    for marker in (
        "result_current",
        "recalculation_pending",
        "current_cohort_settling",
        "plan_age >= 0 and plan_age <= 5",
        "binary_sensor.hoymiles_rce_control_data_ready",
        "binary_sensor.hoymiles_ems_execution_ready",
        "binary_sensor.hoymiles_ems_export_allowed",
        "binary_sensor.hoymiles_sale_block_active",
        "binary_sensor.hoymiles_ems_control_conflict",
        "sensor.hoymiles_ems_hardware_mode",
        "sensor.hoymiles_hit_overview_battery_soc",
        "sensor.hoymiles_hit_maximum_discharge_current",
        "sensor.hoymiles_hit_battery_voltage_bms",
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
        "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
        "sensor.hoymiles_hit_machines_type",
        "sensor.hoymiles_hit_number_of_machines_master_and_slave",
        "input_datetime.hoymiles_rce_latched_slot_end",
        "input_number.hoymiles_rce_latched_minimum_soc",
        "current_slot_execution_power_percent",
        "current_slot_planned",
        "current_slot_continue_eligible",
        "current_price_pln_kwh",
        "automatic_price_floor_pln_kwh",
        "current_age >= -5 and current_age <= 300",
        "voltage_age >= -5 and voltage_age <= 300",
        "soc_age >= -5 and soc_age <= 120",
        "floor_age >= -5 and floor_age <= 180",
        "power_age >= -5 and power_age <= 180",
    ):
        assert marker in pending_source, (
            f"RCE pending execution safety predicate lacks {marker}"
        )
    assert "sensor.hoymiles_rce_effective_discharge_power_percent" not in pending_source
    for marker in (
        "binary_sensor.hoymiles_ems_execution_ready",
        "binary_sensor.hoymiles_ems_control_conflict",
    ):
        assert marker in block.split("actions:", 1)[0], (
            f"RCE hard-stop source is not a direct controller trigger: {marker}"
        )

    if yaml is not None:
        package = yaml.safe_load(scheduler)
        rce_automation = next(
            item
            for item in package["automation"]
            if item.get("id") == "hoymiles_rce_grid_discharge_control"
        )

        def tree_contains(value, needle: str) -> bool:
            if isinstance(value, str):
                return needle in value
            if isinstance(value, dict):
                return any(
                    needle in str(key) or tree_contains(item, needle)
                    for key, item in value.items()
                )
            if isinstance(value, list):
                return any(tree_contains(item, needle) for item in value)
            return False

        pending_parents: list[tuple[dict, int]] = []

        def collect_pending_branches(value) -> None:
            if isinstance(value, dict):
                branches = value.get("choose")
                if isinstance(branches, list):
                    for index, branch in enumerate(branches):
                        if tree_contains(branch.get("sequence", []), pending_stop):
                            pending_parents.append((value, index))
                for item in value.values():
                    collect_pending_branches(item)
            elif isinstance(value, list):
                for item in value:
                    collect_pending_branches(item)

        collect_pending_branches(rce_automation["actions"])
        assert len(pending_parents) == 4, (
            "Every active-cycle interleaving checkpoint needs the same pending hold"
        )
        for parent, index in pending_parents:
            branch = parent["choose"][index]
            sequence = branch.get("sequence", [])
            assert len(sequence) == 1 and set(sequence[0]) == {"stop"}
            assert tree_contains(branch, "result_current")
            assert tree_contains(branch, "recalculation_pending")
            assert tree_contains(
                branch, "binary_sensor.hoymiles_rce_control_data_ready"
            )
            assert not tree_contains(
                branch, "sensor.hoymiles_rce_effective_discharge_power_percent"
            )
            for forbidden in (
                "script.hoymiles_rollback_rce_transaction",
                "script.hoymiles_verified_set_ems_",
                "input_boolean.turn_off",
                "wait_template",
                "delay",
            ):
                assert not tree_contains(branch, forbidden), (
                    f"RCE pending hold performs forbidden operation {forbidden}"
                )
            later_fallback = any(
                tree_contains(candidate, "script.hoymiles_rollback_rce_transaction")
                for candidate in parent["choose"][index + 1 :]
            ) or tree_contains(
                parent.get("default", []),
                "script.hoymiles_rollback_rce_transaction",
            )
            assert later_fallback, (
                "RCE pending hold is not ordered before an existing hard rollback"
            )

        prewrite_pending_parents = [
            (parent, index)
            for parent, index in pending_parents
            if tree_contains(
                parent["choose"][0], "rce_live_authorization_checked"
            )
        ]
        assert len(prewrite_pending_parents) == 2
        for parent, index in prewrite_pending_parents:
            assert index == 1, (
                "Active RCE ordering must be current authority, pending hold, rollback"
            )
            current_branch = parent["choose"][0]
            rollback_branch = parent["choose"][2]
            assert tree_contains(
                current_branch, "binary_sensor.hoymiles_rce_control_data_ready"
            )
            assert tree_contains(current_branch, "current_slot_planned")
            current_conditions = current_branch.get("conditions", [])
            assert isinstance(current_conditions, list)
            assert any(
                isinstance(condition, dict)
                and condition.get("condition") == "template"
                and "current_slot_continue_eligible"
                in str(condition.get("value_template", ""))
                for condition in current_conditions
            ), (
                "Active RCE continuation authority was folded into another "
                "YAML scalar instead of remaining a separate condition"
            )
            assert tree_contains(
                rollback_branch, "input_boolean.hoymiles_rce_discharge_active"
            )
            assert tree_contains(
                rollback_branch, "script.hoymiles_rollback_rce_transaction"
            )

        trigger_entities: set[str] = set()
        for trigger in rce_automation["triggers"]:
            entity_ids = trigger.get("entity_id", [])
            if isinstance(entity_ids, str):
                trigger_entities.add(entity_ids)
            else:
                trigger_entities.update(entity_ids)
        for entity_id in (
            "binary_sensor.hoymiles_ems_execution_ready",
            "binary_sensor.hoymiles_ems_control_conflict",
        ):
            assert entity_id in trigger_entities

    failsafe = scheduler.split(
        "id: hoymiles_automatic_ems_control_failsafe", 1
    )[1].split("id: hoymiles_daily_grid_discharge", 1)[0]
    assert "binary_sensor.hoymiles_rce_control_data_ready" in failsafe
    assert ">= 120" in failsafe, (
        "Persistent RCE planning/readiness loss no longer reaches the failsafe"
    )
    assert "input_number.hoymiles_rce_latched_minimum_soc" in stop
    assert "sensor.hoymiles_rce_effective_minimum_soc" not in stop
    assert "as_timestamp(now()) >= as_timestamp(states(" in stop
    assert "binary_sensor.hoymiles_rce_price_above_threshold" not in stop
    for marker in (
        "current_slot_continue_eligible",
        "current_price_pln_kwh",
        "automatic_price_floor_pln_kwh",
    ):
        assert marker in stop, f"RCE active stop lacks {marker}"
    assert (
        "'current_slot_continue_eligible') is not sameas true"
        in " ".join(stop.split())
    ), "RCE does not stop immediately when continuation authority disappears"
    ready = scheduler.split(
        'name: "Hoymiles RCE Control Data Ready"', 1
    )[1].split('name: "Hoymiles EMS Export Allowed"', 1)[0]
    for marker in (
        "rce_today_data_fresh",
        "forecast_today_data_fresh",
        "gcf_execution_data_fresh",
        "is sameas true",
    ):
        assert marker in ready, f"RCE execution freshness lacks {marker}"
    assert "input_boolean.hoymiles_rcm_export_control_active" in start, (
        "RCE start ignores an outstanding RCEm export-register owner"
    )

    # Inactive RCE does not pre-write discharge power. Start snapshots both
    # shared registers and exposes ownership before its first Modbus write.
    before_start = block.split("# Start RCE:", 1)[0]
    assert "Inactive RCE never pre-writes a shared register" in before_start
    snapshot_position = start.index(
        "input_number.hoymiles_rce_saved_max_discharge_power"
    )
    owner_position = start.index(
        "entity_id: input_boolean.hoymiles_rce_discharge_active",
        snapshot_position,
    )
    first_register_write = start.index(
        "action: script.hoymiles_verified_set_ems_maximum_discharge_power"
    )
    assert snapshot_position < owner_position < first_register_write
    for marker in (
        "input_number.hoymiles_rce_saved_force_discharge_soc",
        "script.hoymiles_rollback_rce_transaction",
        "rce_start_power",
    ):
        assert marker in start

    rollback = scheduler.split(
        "hoymiles_rollback_rce_transaction:", 1
    )[1].split("hoymiles_start_battery_balancing:", 1)[0]
    for marker in (
        "is_state('sensor.hoymiles_ems_hardware_mode', 'self_use')",
        "rollback_preserve_off_grid",
        "'sensor.hoymiles_ems_hardware_mode', 'off_grid'",
        "input_number.hoymiles_rce_saved_max_discharge_power",
        "input_number.hoymiles_rce_saved_force_discharge_soc",
        "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
        "continue_on_timeout: false",
    ):
        assert marker in rollback, f"RCE rollback lacks {marker}"
    rollback_mode_write = rollback.index('option: "self_use"')
    assert rollback.rindex(
        "'sensor.hoymiles_ems_hardware_mode', 'off_grid'",
        0,
        rollback_mode_write,
    ) < rollback_mode_write
    assert rollback.rindex(
        "or is_state('sensor.hoymiles_ems_hardware_mode', 'off_grid')"
    ) < rollback.rindex("action: input_boolean.turn_off")
    assert rollback.rindex(
        "input_number.hoymiles_rce_saved_force_discharge_soc"
    ) < rollback.rindex("action: input_boolean.turn_off")
    sync = scheduler.split(
        "id: hoymiles_rce_sync_dynamic_minimum_soc",
        1,
    )[1].split(
        "id: hoymiles_enforce_sale_block",
        1,
    )[0]
    assert "input_number.hoymiles_rce_latched_minimum_soc" in sync
    assert sync.count("[dynamic, latched, current] | max") >= 2, (
        "Background dynamic-SOC sync can lower an active RCE floor"
    )

    # These are diagnostic parts of the same sensor contract even though the
    # actuator needs only eligibility, deadline and the protected SOC.
    sensor = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    for attribute in (
        "current_run_end",
        "current_slot_fraction",
        "current_slot_planned_export_kwh",
        "current_slot_suppression_reason",
    ):
        assert f'"{attribute}"' in sensor

    def monotonic_floor(latched: float, register: float, required: float) -> int:
        return ceil(max(latched, register, required))

    assert monotonic_floor(24.0, 25.0, 31.2) == 32
    assert monotonic_floor(32.0, 25.0, 21.0) == 32

    # A contiguous transaction deadline is monotonic even across the repeated
    # local hour at the end of daylight-saving time.
    first_end = datetime(2026, 10, 25, 2, 30, tzinfo=WARSAW, fold=0)
    second_end = datetime(2026, 10, 25, 2, 30, tzinfo=WARSAW, fold=1)
    assert second_end.timestamp() > first_end.timestamp()
    assert max(first_end.timestamp(), second_end.timestamp()) == second_end.timestamp()


def assert_rcm_execution_contracts() -> None:
    """Protect emergency priority and atomic/frozen RCEm pre-discharge."""

    scheduler = (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
    ).read_text(encoding="utf-8")
    main = scheduler.split(
        "id: hoymiles_rcm_voltage_charge_control",
        1,
    )[1].split(
        "id: hoymiles_rcm_pre_discharge_control",
        1,
    )[0]
    assert "mode: restart" in main.split("actions:", 1)[0], (
        "RCEm emergency cannot interrupt a normal Modbus readback wait"
    )
    helper_block = scheduler.split("script:", 1)[1].split(
        "hoymiles_start_grid_discharge:", 1
    )[0]
    for helper in (
        "hoymiles_verified_set_ems_mode",
        "hoymiles_verified_set_ems_maximum_charge_power",
        "hoymiles_verified_set_ems_force_charge_soc",
        "hoymiles_verified_set_ems_maximum_discharge_power",
        "hoymiles_verified_set_ems_force_discharge_soc",
        "hoymiles_verified_set_battery_max_charge_power",
        "hoymiles_verified_set_gcf_export_limit",
    ):
        body = helper_block.split(f"  {helper}:", 1)[1].split("\n  hoymiles_", 1)[0]
        assert "mode: restart" in body
        assert f"action: script.{helper}" not in body, f"{helper} calls itself"
        assert "readback_generation" in body
        assert "generation_after_write" in body
        assert body.index("action: number.set_value") < body.index(
            "generation_after_write"
        ) if helper != "hoymiles_verified_set_ems_mode" else (
            body.index("action: select.select_option")
            < body.index("generation_after_write")
        )
        capability = (
            "direct_register_verified_readback_supported"
            if helper
            in {
                "hoymiles_verified_set_battery_max_charge_power",
                "hoymiles_verified_set_gcf_export_limit",
            }
            else "ems_verified_hardware_readback_supported"
        )
        if helper in {
            "hoymiles_verified_set_ems_maximum_charge_power",
            "hoymiles_verified_set_ems_force_charge_soc",
            "hoymiles_verified_set_ems_maximum_discharge_power",
            "hoymiles_verified_set_ems_force_discharge_soc",
        }:
            assert "*ems_complete_physical_snapshot_ready" in body
            assert "*ems_complete_physical_block_ack" in body
        else:
            assert capability in body
    pre = scheduler.split(
        "id: hoymiles_rcm_pre_discharge_control",
        1,
    )[1]

    emergency_pos = main.index("# Napięcie >=253 V")
    release_pos = main.index("# Zwolnienie własności")
    assert emergency_pos < release_pos, "RCEm emergency is shadowed by release"
    emergency = main[emergency_pos:release_pos]
    for marker in (
        "live_emergency",
        "emergency_action_ready",
        "maximum_voltage >= 253.0",
        "emergency_voltage_data_fresh",
        "charge_actuator_data_fresh",
        "export_actuator_data_fresh",
        "emergency_charge_path",
        "emergency_export_path",
        "# Final emergency interlocks",
    ):
        assert marker in emergency, f"RCEm emergency path lacks {marker}"
    assert "'forecast_data_fresh'" not in emergency
    assert "'history_data_fresh'" not in emergency
    assert "'actuator_data_fresh'" not in emergency
    assert "plan_status" not in emergency
    assert "and voltage_data_fresh" not in emergency, (
        "A missing non-emergency phase still blocks the live 253 V path"
    )
    ownership_setup = emergency.split(
        "# Charge and export ownership are independent",
        1,
    )[1].split("# A zero/stale BMS charge limit", 1)[0]
    assert "emergency_charge_path" in ownership_setup
    assert "input_number.hoymiles_rcm_saved_battery_charge_power" in ownership_setup
    assert "input_boolean.hoymiles_rcm_active" in ownership_setup
    assert ownership_setup.index("emergency_charge_path") < ownership_setup.index(
        "input_number.hoymiles_rcm_saved_battery_charge_power"
    )
    charge_path = main.split("emergency_charge_path: >-", 1)[1].split(
        "emergency_export_path:", 1
    )[0]
    assert "bms_charge_available" in charge_path
    assert "bms_charge_data_fresh" in charge_path
    assert "ems_mode_data_fresh" in charge_path
    assert "recommended_limit >= 10" in charge_path
    export_available = main.split("export_control_available: >-", 1)[1].split(
        "rcm_blocked:", 1
    )[0]
    assert "| float(-1)) >= 0" in export_available
    assert "| float(0)) > 0" not in export_available, (
        "A held 0% export limit must remain an available emergency actuator"
    )
    restore_export = main.split(
        "Odtwórz limit eksportu także po restarcie", 1
    )[1].split("Normalny regulator zaczyna", 1)[0]
    assert "export_register_data_fresh" in restore_export
    assert "export_actuator_data_fresh" not in restore_export, (
        "RCEm export rollback still depends on the execution/GCF path"
    )

    normal_start = main.split(
        "# Normalny regulator zaczyna",
        1,
    )[1].split(
        "# Po przejęciu własności",
        1,
    )[0]
    for marker in (
        "prediction_ready",
        "voltage_data_fresh",
        "charge_actuator_data_fresh",
        "forecast_data_fresh",
        "history_data_fresh",
        "live_power_data_fresh",
        "bms_charge_data_fresh",
        "bms_charge_available",
        "system_power_data_valid",
        "binary_sensor.hoymiles_direct_register_execution_ready",
        "# Final normal-control interlock",
        "last_reported",
        "age >= 0 and age <= 60",
    ):
        assert marker in normal_start, f"Normal RCEm start lacks {marker}"
    assert normal_start.count("wait_template:") >= 2
    start_pre_export = normal_start.split(
        "# The charge ACK may consume the whole restart-mode run",
        1,
    )[1].split(
        "action: script.hoymiles_verified_set_gcf_export_limit",
        1,
    )[0]
    for marker in (
        "last_reported",
        "age >= 0 and age <= 60",
        "result_current",
        "input_boolean.hoymiles_rcm_enabled",
        "input_boolean.hoymiles_rcm_shadow_mode",
        "input_boolean.hoymiles_rcm_active",
        "input_boolean.hoymiles_rcm_pre_discharge_active",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "input_boolean.hoymiles_battery_balancing_active",
        "input_boolean.hoymiles_discharge_cycle_active",
        "input_boolean.hoymiles_charge_cycle_active",
        "binary_sensor.hoymiles_direct_register_execution_ready",
        "sensor.hoymiles_ems_hardware_mode",
        "system_power_data_valid",
        "prediction_ready",
        "voltage_data_fresh",
        "charge_actuator_data_fresh",
        "forecast_data_fresh",
        "history_data_fresh",
        "live_power_data_fresh",
        "bms_charge_data_fresh",
        "bms_charge_available",
        "recommended_charge_limit_percent",
        "recommended_export_limit_percent",
        "export_actuator_data_fresh",
        "gcf_data_fresh",
    ):
        assert marker in start_pre_export, (
            f"Normal RCEm start pre-export post-ACK guard lacks {marker}"
        )
    assert "not rcm_blocked" not in start_pre_export

    start_final = normal_start.split(
        "# Final normal-control interlock",
        1,
    )[1].split("                then:", 1)[0]
    for marker in (
        "result_current",
        "input_boolean.hoymiles_rcm_active",
        "input_boolean.hoymiles_rcm_pre_discharge_active",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "input_boolean.hoymiles_rce_discharge_active",
        "input_boolean.hoymiles_tariff_charge_active",
        "input_boolean.hoymiles_battery_balancing_active",
        "input_boolean.hoymiles_discharge_cycle_active",
        "input_boolean.hoymiles_charge_cycle_active",
        "binary_sensor.hoymiles_direct_register_execution_ready",
        "recommended_charge_limit_percent",
        "recommended_export_limit_percent",
    ):
        assert marker in start_final, f"Normal RCEm final guard lacks {marker}"
    assert "not rcm_blocked" not in start_final, (
        "Normal RCEm final guard still trusts a frozen blocker snapshot"
    )
    start_rollback = normal_start.split(
        "# Final normal-control interlock",
        1,
    )[1].split("                else:", 1)[1]
    assert "script.hoymiles_verified_set_battery_max_charge_power" in start_rollback
    assert "input_boolean.hoymiles_rcm_active" in start_rollback
    continuous = main.split("# Po przejęciu własności", 1)[1]
    pre_export_guard = continuous.split(
        "action: script.hoymiles_verified_set_battery_max_charge_power", 1
    )[1].split(
        "action: script.hoymiles_verified_set_gcf_export_limit", 1
    )[0]
    for marker in (
        "last_reported",
        "age >= 0 and age <= 60",
        "input_boolean.hoymiles_rcm_enabled",
        "input_boolean.hoymiles_rcm_shadow_mode",
        "input_boolean.hoymiles_rcm_active",
        "input_boolean.hoymiles_rcm_pre_discharge_active",
        "binary_sensor.hoymiles_direct_register_execution_ready",
        "sensor.hoymiles_ems_hardware_mode",
        "result_current",
        "system_power_data_valid",
        "prediction_ready",
        "voltage_data_fresh",
        "charge_actuator_data_fresh",
        "forecast_data_fresh",
        "history_data_fresh",
        "live_power_data_fresh",
        "bms_charge_data_fresh",
        "bms_charge_available",
        "recommended_charge_limit_percent",
        "recommended_export_limit_percent",
        "export_actuator_data_fresh",
        "gcf_data_fresh",
    ):
        assert marker in pre_export_guard, (
            f"Continuous RCEm pre-export post-ACK guard lacks {marker}"
        )
    continuous_final = continuous.split(
        'value: "{{ recommended_export_limit | round(1) }}"', 1
    )[1].split("                else:", 1)[0]
    for marker in (
        "last_reported",
        "age >= 0 and age <= 60",
        "input_boolean.hoymiles_rcm_enabled",
        "input_boolean.hoymiles_rcm_shadow_mode",
        "not rcm_blocked",
        "binary_sensor.hoymiles_direct_register_execution_ready",
        "result_current",
        "system_power_data_valid",
        "prediction_ready",
        "forecast_data_fresh",
        "history_data_fresh",
        "live_power_data_fresh",
        "bms_charge_data_fresh",
        "recommended_charge_limit_percent",
        "recommended_export_limit_percent",
        "sensor.hoymiles_ems_hardware_mode",
    ):
        assert marker in continuous_final, (
            f"Continuous RCEm final post-ACK guard lacks {marker}"
        )
    assert "Each transaction retains only its own durable owner" in emergency
    assert "above: 252.99" in main, (
        "RCEm emergency trigger must cross at the same 253 V policy boundary"
    )
    charge_path = main.split("emergency_charge_path: >-", 1)[1].split(
        "emergency_export_path: >-", 1
    )[0]
    assert "system_power_data_valid" in charge_path
    assert "direct_register_write_ready" in charge_path
    export_path = main.split("emergency_export_path: >-", 1)[1].split(
        "normal_export_path: >-", 1
    )[0]
    assert "ems_mode_data_fresh" not in export_path, (
        "Export-only emergency still depends on EMS mode telemetry"
    )
    assert "system_power_data_valid" not in export_path, (
        "A live export clamp must remain independent of kW-to-percent topology"
    )
    assert "direct_register_write_ready" in export_path
    emergency_final = emergency.split("# Final emergency interlocks", 1)[1]
    charge_final, export_final = emergency_final.split(
        "# Export emergency finalization (independent actuator path).", 1
    )
    assert "# Charge emergency finalization (independent actuator path)." in charge_final
    assert "'sensor.hoymiles_ems_hardware_mode', 'self_use'" in charge_final
    assert "binary_sensor.hoymiles_direct_register_execution_ready" in charge_final
    assert "script.hoymiles_verified_set_battery_max_charge_power" in charge_final
    assert "input_boolean.hoymiles_rcm_active" in charge_final
    assert "script.hoymiles_verified_set_gcf_export_limit" not in charge_final
    assert "input_boolean.hoymiles_rcm_export_control_active" not in charge_final
    assert "script.hoymiles_verified_set_gcf_export_limit" in export_final
    assert "input_boolean.hoymiles_rcm_export_control_active" in export_final
    assert "script.hoymiles_verified_set_battery_max_charge_power" not in export_final
    assert "input_boolean.hoymiles_rcm_active" not in export_final
    charge_final_guard = charge_final.split("                then:", 1)[0]
    assert "binary_sensor.hoymiles_direct_register_execution_ready" in (
        charge_final_guard
    )
    charge_final_rollback = charge_final.split("                else:", 1)[1]
    assert "script.hoymiles_verified_set_battery_max_charge_power" in (
        charge_final_rollback
    )
    for actuator_final in (charge_final, export_final):
        assert "continue_on_error: true" in actuator_final
        assert "continue_on_timeout: true" in actuator_final
    self_use_write = emergency.split(
        "# Charge and export ownership are independent", 1
    )[1].split("# A zero/stale BMS charge limit", 1)[0]
    assert "{{ emergency_charge_path" in self_use_write

    # The initial emergency_*_path variables are only entry snapshots. Every
    # actuator has a separate full live predicate immediately before its own
    # helper, after all earlier mode/actuator ACK waits have returned.
    charge_prewrite = emergency.split(
        "# Final live charge-write interlock after every prior mode ACK.",
        1,
    )[1].split(
        "action: script.hoymiles_verified_set_battery_max_charge_power",
        1,
    )[0]
    for marker in (
        "last_reported",
        "age >= 0 and age <= 60",
        "result_current",
        "input_boolean.hoymiles_rcm_enabled",
        "input_boolean.hoymiles_rcm_shadow_mode",
        "input_boolean.hoymiles_rcm_active",
        "binary_sensor.hoymiles_ems_control_conflict",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "input_boolean.hoymiles_battery_balancing_active",
        "input_boolean.hoymiles_discharge_cycle_active",
        "input_boolean.hoymiles_charge_cycle_active",
        "binary_sensor.hoymiles_direct_register_execution_ready",
        "sensor.hoymiles_ems_hardware_mode",
        "live_emergency",
        "emergency_action_ready",
        "maximum_voltage_v",
        "emergency_voltage_data_fresh",
        "system_power_data_valid",
        "ems_mode_data_fresh",
        "charge_actuator_data_fresh",
        "bms_charge_data_fresh",
        "bms_charge_available",
        "recommended_charge_limit_percent",
    ):
        assert marker in charge_prewrite, (
            f"Emergency charge pre-write live guard lacks {marker}"
        )
    assert "wait_template:" not in charge_prewrite

    export_prewrite = emergency.split(
        "# Final live export-write interlock after the optional charge",
        1,
    )[1].split(
        "action: script.hoymiles_verified_set_gcf_export_limit",
        1,
    )[0]
    for marker in (
        "last_reported",
        "age >= 0 and age <= 60",
        "result_current",
        "input_boolean.hoymiles_rcm_enabled",
        "input_boolean.hoymiles_rcm_shadow_mode",
        "input_boolean.hoymiles_rcm_export_control_enabled",
        "input_boolean.hoymiles_rcm_export_control_active",
        "binary_sensor.hoymiles_ems_control_conflict",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "input_boolean.hoymiles_battery_balancing_active",
        "input_boolean.hoymiles_discharge_cycle_active",
        "input_boolean.hoymiles_charge_cycle_active",
        "binary_sensor.hoymiles_direct_register_execution_ready",
        "sensor.hoymiles_ems_hardware_mode",
        "live_emergency",
        "emergency_action_ready",
        "maximum_voltage_v",
        "emergency_voltage_data_fresh",
        "export_actuator_data_fresh",
        "gcf_data_fresh",
        "recommended_export_limit_percent",
    ):
        assert marker in export_prewrite, (
            f"Emergency export pre-write live guard lacks {marker}"
        )
    assert "wait_template:" not in export_prewrite
    assert emergency.index(
        "# Final live charge-write interlock after every prior mode ACK."
    ) < emergency.index(
        "action: script.hoymiles_verified_set_battery_max_charge_power"
    ) < emergency.index(
        "# Final live export-write interlock after the optional charge"
    ) < emergency.index("action: script.hoymiles_verified_set_gcf_export_limit")

    if yaml is not None:
        package = yaml.safe_load(scheduler)
        rcm_automation = next(
            item
            for item in package["automation"]
            if item.get("id") == "hoymiles_rcm_voltage_charge_control"
        )
        rcm_choose = next(
            item["choose"]
            for item in rcm_automation["actions"]
            if isinstance(item, dict) and "choose" in item
        )
        emergency_sequence = rcm_choose[0]["sequence"]

        def tree_contains(value, needle: str) -> bool:
            if isinstance(value, str):
                return needle in value
            if isinstance(value, dict):
                return any(tree_contains(item, needle) for item in value.values())
            if isinstance(value, list):
                return any(tree_contains(item, needle) for item in value)
            return False

        charge_item_index = next(
            index
            for index, item in enumerate(emergency_sequence)
            if tree_contains(
                item,
                "script.hoymiles_verified_set_battery_max_charge_power",
            )
        )
        export_item_index = next(
            index
            for index, item in enumerate(emergency_sequence)
            if tree_contains(item, "script.hoymiles_verified_set_gcf_export_limit")
        )
        assert charge_item_index < export_item_index, (
            "Emergency export write is nested in the charge-only path"
        )
        charge_item = emergency_sequence[charge_item_index]
        export_item = emergency_sequence[export_item_index]
        for item, actuator in (
            (charge_item, "charge"),
            (export_item, "export"),
        ):
            assert tree_contains(
                item,
                "binary_sensor.hoymiles_direct_register_execution_ready",
            ), f"Emergency {actuator} helper lacks a structural live gate"
            assert tree_contains(item, "emergency_voltage_data_fresh")
            assert tree_contains(item, "binary_sensor.hoymiles_ems_control_conflict")

    for marker in (
        "pre_discharge_start_eligible",
        "pre_discharge_continue_eligible",
        "pre_discharge_transaction_ready",
        "pre_discharge_deadline",
        "bms_discharge_available",
        "bms_discharge_data_fresh",
        "pre_discharge_actuator_data_fresh",
        "discharge_registers_data_fresh",
        "input_datetime.hoymiles_rcm_latched_pre_discharge_deadline",
        "input_number.hoymiles_rcm_latched_pre_discharge_target_soc",
        "input_number.hoymiles_rcm_latched_pre_discharge_power",
        "# Ownership is already active here",
    ):
        assert marker in pre, f"RCEm pre-discharge lacks {marker}"
    stop = pre.split("# Każde naruszenie warunków", 1)[1].split(
        "# Rozpoczęcie jest dozwolone",
        1,
    )[0]
    assert "not pre_discharge_continue_eligible" in stop
    assert "not voltage_data_fresh" in stop
    assert "not pre_discharge_actuator_data_fresh" in stop
    assert "not bms_discharge_data_fresh" in stop
    assert "not bms_discharge_available" in stop
    assert "maximum_voltage >= 248.4" in stop
    assert "forecast_data_fresh" not in stop
    assert "history_data_fresh" not in stop
    assert "Keep ownership until mode and both Modbus values" in stop
    assert stop.count("discharge_registers_data_fresh") >= 3

    start = pre.split("# Rozpoczęcie jest dozwolone", 1)[1]
    for marker in (
        "pre_discharge_start_eligible",
        "pre_discharge_transaction_ready",
        "binary_sensor.hoymiles_ems_execution_ready",
        "prediction_ready",
        "forecast_data_fresh",
        "history_data_fresh",
        "live_power_data_fresh",
        "pre_discharge_actuator_data_fresh",
        "bms_discharge_available",
        "as_timestamp(pre_discharge_deadline, 0)",
    ):
        assert marker in start, f"RCEm pre-discharge start lacks {marker}"
    start_entry = start.split("            sequence:", 1)[0]
    assert "binary_sensor.hoymiles_ems_execution_ready" in start_entry
    assert start.count("wait_template:") >= 3
    assert start.index("Ownership begins before the first Modbus write") < start.index(
        "action: script.hoymiles_verified_set_ems_maximum_discharge_power"
    )
    assert "A failed final interlock or mode readback is a pending restore" in start
    owned_phase = start.split("# Ownership begins before the first Modbus write", 1)[1]
    between_register_guard = owned_phase.split(
        "# The 4306 acknowledgement can consume the whole single-mode", 1
    )[1].split("# Ownership is already active here", 1)[0]
    paired_write = between_register_guard.index(
        "script.hoymiles_verified_set_ems_force_discharge_soc"
    )
    for marker in (
        "result_current",
        "input_boolean.hoymiles_rcm_enabled",
        "input_boolean.hoymiles_rcm_shadow_mode",
        "input_boolean.hoymiles_rcm_pre_discharge_enabled",
        "input_boolean.hoymiles_rcm_active",
        "input_boolean.hoymiles_rcm_pre_discharge_active",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "input_boolean.hoymiles_battery_balancing_active",
        "binary_sensor.hoymiles_sale_block_active",
        "binary_sensor.hoymiles_ems_execution_ready",
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "pre_discharge_continue_eligible",
        "prediction_ready",
        "forecast_data_fresh",
        "history_data_fresh",
        "live_power_data_fresh",
        "bms_discharge_data_fresh",
        "gcf_data_fresh",
        "binary_sensor.hoymiles_ems_export_allowed",
        "maximum_voltage_v",
        "input_datetime.hoymiles_rcm_latched_pre_discharge_deadline",
    ):
        assert marker in between_register_guard
        assert between_register_guard.index(marker) < paired_write
    assert "and not pre_discharge_blocked" not in owned_phase
    final_interlock = owned_phase.split(
        "# Ownership is already active here",
        1,
    )[1].split(
        "# A failed final interlock or mode readback",
        1,
    )[0]
    assert "pre_discharge_continue_eligible" in final_interlock
    assert "pre_discharge_start_eligible" not in final_interlock
    assert "pre_discharge_transaction_ready" not in final_interlock
    assert "prediction_ready" in final_interlock
    assert "forecast_data_fresh" in final_interlock
    assert "history_data_fresh" in final_interlock
    for marker in (
        "input_datetime.hoymiles_rcm_latched_pre_discharge_deadline",
        "input_number.hoymiles_rcm_latched_pre_discharge_target_soc",
        "input_number.hoymiles_rcm_latched_pre_discharge_power",
        "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "input_boolean.hoymiles_battery_balancing_active",
        "binary_sensor.hoymiles_sale_block_active",
        "binary_sensor.hoymiles_ems_execution_ready",
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "live_power_data_fresh",
        "gcf_data_fresh",
        "binary_sensor.hoymiles_ems_export_allowed",
    ):
        assert marker in final_interlock, (
            f"Owned RCEm final interlock lacks frozen/readback guard {marker}"
        )
    post_mode_guard = owned_phase.split(
        "# A failed final interlock or mode readback",
        1,
    )[1].split("                then:", 1)[0]
    assert "pre_discharge_continue_eligible" in post_mode_guard
    assert "pre_discharge_start_eligible" not in post_mode_guard
    assert "pre_discharge_transaction_ready" not in post_mode_guard
    assert "'sensor.hoymiles_ems_hardware_mode', 'grid_discharge'" in post_mode_guard
    for marker in (
        "result_current",
        "input_boolean.hoymiles_rce_discharge_enabled",
        "input_boolean.hoymiles_tariff_charge_enabled",
        "input_boolean.hoymiles_battery_balancing_active",
        "binary_sensor.hoymiles_sale_block_active",
        "binary_sensor.hoymiles_ems_execution_ready",
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "prediction_ready",
        "forecast_data_fresh",
        "history_data_fresh",
        "live_power_data_fresh",
        "gcf_data_fresh",
        "binary_sensor.hoymiles_ems_export_allowed",
        "bms_discharge_data_fresh",
        "bms_discharge_available",
    ):
        assert marker in post_mode_guard, (
            f"RCEm pre-discharge post-mode guard lacks {marker}"
        )

    # Every wait boundary re-reads the shared EMS execution gate. Model the
    # mode:single interleaving where readiness was true at entry but drops
    # before any one of the four protected stages resumes.
    pre_discharge_stages = {
        "start": start_entry,
        "inter_write": between_register_guard,
        "pre_mode": final_interlock,
        "post_mode": post_mode_guard,
    }
    def pre_discharge_stage_authorized(
        entry_execution_ready: bool,
        live_execution_ready: bool,
    ) -> bool:
        return entry_execution_ready and live_execution_ready

    for stage_name, stage_block in pre_discharge_stages.items():
        assert "binary_sensor.hoymiles_ems_execution_ready" in stage_block, (
            f"RCEm pre-discharge {stage_name} does not recheck EMS readiness"
        )
        assert pre_discharge_stage_authorized(True, True)
        assert not pre_discharge_stage_authorized(True, False), (
            f"RCEm pre-discharge {stage_name} accepted stale entry readiness"
        )

    def rcm_post_ack_authorized(
        enabled: bool,
        shadow: bool,
        owner_active: bool,
        result_current: bool,
        plan_fresh: bool,
        inputs_fresh: bool,
        direct_ready: bool,
        blocked: bool,
        physical_mode: str,
        recommendation_same: bool,
    ) -> bool:
        return (
            enabled
            and not shadow
            and owner_active
            and result_current
            and plan_fresh
            and inputs_fresh
            and direct_ready
            and not blocked
            and physical_mode == "self_use"
            and recommendation_same
        )

    rcm_live = dict(
        enabled=True,
        shadow=False,
        owner_active=True,
        result_current=True,
        plan_fresh=True,
        inputs_fresh=True,
        direct_ready=True,
        blocked=False,
        physical_mode="self_use",
        recommendation_same=True,
    )
    assert rcm_post_ack_authorized(**rcm_live)
    for changed in (
        {"enabled": False},
        {"shadow": True},
        {"owner_active": False},
        {"result_current": False},
        {"plan_fresh": False},
        {"inputs_fresh": False},
        {"direct_ready": False},
        {"blocked": True},
        {"physical_mode": "off_grid"},
        {"recommendation_same": False},
    ):
        assert not rcm_post_ack_authorized(**(rcm_live | changed)), (
            "RCEm can continue after an in-flight authorization change"
        )
    assert "# Podczas cyklu aktualizuj moc i cel SOC" not in pre, (
        "Active RCEm target still follows a moving forecast"
    )

    def emergency_paths(
        charge_fresh: bool,
        bms_charge_ok: bool,
        export_requested: bool,
        export_fresh: bool,
    ) -> tuple[bool, bool]:
        return (
            charge_fresh and bms_charge_ok,
            export_requested and export_fresh,
        )

    assert emergency_paths(False, True, True, True) == (False, True)
    assert emergency_paths(True, True, True, False) == (True, False)
    assert emergency_paths(False, True, True, False) == (False, False)

    def emergency_ack_outcome(
        charge_path: bool,
        charge_ack: bool,
        export_path: bool,
        export_ack: bool,
    ) -> tuple[bool, bool]:
        """Successful emergency actuators survive failure of the other path."""

        return charge_path and charge_ack, export_path and export_ack

    assert emergency_ack_outcome(True, False, True, True) == (False, True)
    assert emergency_ack_outcome(True, True, True, False) == (True, False)
    assert emergency_ack_outcome(True, True, True, True) == (True, True)

    def emergency_prewrite_outcome(
        charge_entry_path: bool,
        charge_live_ready: bool,
        export_entry_path: bool,
        export_live_ready: bool,
    ) -> tuple[bool, bool]:
        """A dropped gate vetoes only the affected actuator's next write."""

        return (
            charge_entry_path and charge_live_ready,
            export_entry_path and export_live_ready,
        )

    assert emergency_prewrite_outcome(True, True, True, True) == (True, True)
    assert emergency_prewrite_outcome(True, False, True, True) == (False, True), (
        "Lost readiness before charge helper incorrectly blocks export too"
    )
    assert emergency_prewrite_outcome(True, True, True, False) == (True, False), (
        "Lost readiness before export helper incorrectly undoes charge"
    )
    assert emergency_prewrite_outcome(True, False, True, False) == (False, False)

    def emergency_charge_final_authorized(
        charge_path: bool,
        direct_execution_ready: bool,
        charge_ack: bool,
    ) -> bool:
        return charge_path and direct_execution_ready and charge_ack

    assert emergency_charge_final_authorized(True, True, True)
    assert not emergency_charge_final_authorized(True, False, True), (
        "Emergency charge finalizer accepted a dropped direct execution gate"
    )

    def charge_ownership_transition(
        charge_path: bool,
        ownership_preexisting: bool,
    ) -> tuple[bool, bool]:
        snapshot_user_value = charge_path and not ownership_preexisting
        ownership_after_setup = ownership_preexisting or charge_path
        return snapshot_user_value, ownership_after_setup

    assert charge_ownership_transition(False, False) == (False, False)
    assert charge_ownership_transition(True, False) == (True, True)
    assert charge_ownership_transition(False, True) == (False, True)

    def export_actuator_available(
        numeric_limit: bool,
        current_limit_percent: float,
        generation_control_available: bool,
    ) -> bool:
        return (
            numeric_limit
            and current_limit_percent >= 0.0
            and generation_control_available
        )

    assert export_actuator_available(True, 0.0, True)
    assert export_actuator_available(True, 100.0, True)
    assert not export_actuator_available(False, 0.0, True)

    def pre_discharge_phase_eligible(
        ownership_active: bool,
        start_eligible: bool,
        continue_eligible: bool,
    ) -> bool:
        return continue_eligible if ownership_active else start_eligible

    assert pre_discharge_phase_eligible(False, True, False)
    assert pre_discharge_phase_eligible(True, False, True)
    assert not pre_discharge_phase_eligible(True, True, False)

    def ownership_can_clear(actuator_fresh: bool, readback_ok: bool) -> bool:
        return actuator_fresh and readback_ok

    assert ownership_can_clear(True, True)
    assert not ownership_can_clear(False, True)
    assert not ownership_can_clear(True, False)


def assert_physical_hardware_readback_contracts() -> None:
    """Forbid optimistic HA echoes from acknowledging a Modbus transaction."""

    scheduler = (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
    ).read_text(encoding="utf-8")
    helpers = scheduler.split("script:", 1)[1].split(
        "  hoymiles_start_grid_discharge:", 1
    )[0]
    ems_helper_specs = {
        "hoymiles_verified_set_ems_mode": (
            "action: select.select_option",
            "expected_mode_code",
            "option",
        ),
        "hoymiles_verified_set_ems_maximum_charge_power": (
            "action: number.set_value",
            "expected_maximum_charge_power",
            "value | float(0) | round(1)",
        ),
        "hoymiles_verified_set_ems_force_charge_soc": (
            "action: number.set_value",
            "expected_force_charge_soc",
            "value | float(0) | round(0)",
        ),
        "hoymiles_verified_set_ems_maximum_discharge_power": (
            "action: number.set_value",
            "expected_maximum_discharge_power",
            "value | float(0) | round(1)",
        ),
        "hoymiles_verified_set_ems_force_discharge_soc": (
            "action: number.set_value",
            "expected_force_discharge_soc",
            "value | float(0) | round(0)",
        ),
    }
    direct_helper_specs = {
        "hoymiles_verified_set_battery_max_charge_power": (
            "action: number.set_value",
            "battery_charge_power_readback_generation",
            "battery_max_charge_power_readback",
        ),
        "hoymiles_verified_set_gcf_export_limit": (
            "action: number.set_value",
            "gcf_control_readback_generation",
            "gcf_maximum_export_power_readback",
        ),
    }
    full_block_mirrors = (
        "ems_mode_readback_code",
        "ems_self_use_soc_readback",
        "ems_backup_soc_readback",
        "ems_force_charge_soc_readback",
        "ems_maximum_charge_power_readback",
        "ems_force_discharge_soc_readback",
        "ems_maximum_discharge_power_readback",
    )
    full_block_expected = (
        "expected_mode_code",
        "expected_self_use_soc",
        "expected_backup_soc",
        "expected_force_charge_soc",
        "expected_maximum_charge_power",
        "expected_force_discharge_soc",
        "expected_maximum_discharge_power",
    )
    expected_to_mirror = {
        "expected_mode_code": "ems_mode_readback_code",
        "expected_self_use_soc": "ems_self_use_soc_readback",
        "expected_backup_soc": "ems_backup_soc_readback",
        "expected_force_charge_soc": "ems_force_charge_soc_readback",
        "expected_maximum_charge_power": "ems_maximum_charge_power_readback",
        "expected_force_discharge_soc": "ems_force_discharge_soc_readback",
        "expected_maximum_discharge_power": "ems_maximum_discharge_power_readback",
    }
    snapshot_contract = scheduler.split(
        "value_template: &ems_complete_physical_snapshot_ready", 1
    )[1].split("      - variables:", 1)[0]
    full_ack_contract = scheduler.split(
        "wait_template: &ems_complete_physical_block_ack", 1
    )[1].split('        timeout: "00:01:00"', 1)[0]
    assert scheduler.count("&ems_complete_physical_snapshot_ready") == 1
    assert scheduler.count("*ems_complete_physical_snapshot_ready") == 4
    assert scheduler.count("&ems_complete_physical_block_ack") == 1
    assert scheduler.count("*ems_complete_physical_block_ack") == 4
    for mirror in full_block_mirrors:
        assert mirror in snapshot_contract, f"Snapshot omits {mirror}"
        assert mirror in full_ack_contract, f"Full-block ACK omits {mirror}"
    for expected in full_block_expected:
        assert expected in full_ack_contract, f"Full-block ACK omits {expected}"
    for marker in (
        "ems_generation_before_write",
        "ems_generation_after_write",
        "generation_after_not_older",
        "newer_than_after",
        "generation_before >= 16000000",
        "generation_after >= 16000000",
        "is_number(states(",
        "sensor.hoymiles_hit_ems_verified_hardware_readback_supported",
    ):
        assert marker in full_ack_contract, marker

    compact_full_ack = " ".join(full_ack_contract.split()).replace(
        "states( '", "states('"
    )
    exact_full_ack_pairs = (
        "and (states('sensor.hoymiles_hit_ems_mode_readback_code') | int(-1)) "
        "== (expected_mode_code | int(-2))",
        "and ((states('sensor.hoymiles_hit_ems_self_use_soc_readback') "
        "| float(-999)) - (expected_self_use_soc | float(-998))) | abs < 0.5",
        "and ((states('sensor.hoymiles_hit_ems_backup_soc_readback') "
        "| float(-999)) - (expected_backup_soc | float(-998))) | abs < 0.5",
        "and ((states('sensor.hoymiles_hit_ems_force_charge_soc_readback') "
        "| float(-999)) - (expected_force_charge_soc | float(-998))) | abs < 0.5",
        "and ((states('sensor.hoymiles_hit_ems_maximum_charge_power_readback') "
        "| float(-999)) - (expected_maximum_charge_power | float(-998))) "
        "| abs < 0.05",
        "and ((states('sensor.hoymiles_hit_ems_force_discharge_soc_readback') "
        "| float(-999)) - (expected_force_discharge_soc | float(-998))) "
        "| abs < 0.5",
        "and ((states('sensor.hoymiles_hit_ems_maximum_discharge_power_readback') "
        "| float(-999)) - (expected_maximum_discharge_power | float(-998))) "
        "| abs < 0.05",
    )
    for exact_pair in exact_full_ack_pairs:
        assert compact_full_ack.count(exact_pair) == 1, (
            f"Full-block ACK no longer binds the exact physical/expected pair: "
            f"{exact_pair}"
        )
    for exact_generation_clause in (
        "{% set generation_after_not_older = generation_after == generation_before "
        "or generation_after > generation_before or (generation_before >= 16000000 "
        "and generation_after >= 1 and generation_after < generation_before) %}",
        "{% set newer_than_after = current_generation > generation_after "
        "or (generation_after >= 16000000 and current_generation >= 1 "
        "and current_generation < generation_after) %}",
        "and generation_after_not_older",
        "and newer_than_after",
    ):
        assert compact_full_ack.count(exact_generation_clause) == 1, (
            f"Full-block ACK generation polarity changed: {exact_generation_clause}"
        )

    for helper, (service, target_expected, target_source) in ems_helper_specs.items():
        body = helpers.split(f"  {helper}:", 1)[1].split("\n  hoymiles_", 1)[0]
        frozen_pos = body.index("ems_generation_before_write")
        service_pos = body.index(service)
        capture_pos = body.index("ems_generation_after_write")
        wait_pos = body.index("wait_template:", capture_pos)
        assert frozen_pos < service_pos < capture_pos < wait_pos
        frozen = body[frozen_pos:service_pos]
        assert target_expected in frozen
        target_assignment = frozen.split(f"{target_expected}:", 1)[1].split(
            "\n          expected_", 1
        )[0]
        assert target_source in target_assignment, (
            f"{helper} no longer substitutes its normalized requested field"
        )
        for expected, mirror in expected_to_mirror.items():
            if expected == target_expected:
                continue
            assignment = frozen.split(f"{expected}:", 1)[1].split(
                "\n          expected_", 1
            )[0]
            assert mirror in assignment, (
                f"{helper} no longer freezes {expected} from physical {mirror}"
            )
        assert "mode: restart" in body
        if helper == "hoymiles_verified_set_ems_mode":
            assert "sensor.hoymiles_hit_ems_verified_hardware_readback_supported" in body
            assert "&ems_complete_physical_snapshot_ready" in body
            assert "&ems_complete_physical_block_ack" in body
        else:
            assert "*ems_complete_physical_snapshot_ready" in body
            assert "*ems_complete_physical_block_ack" in body
        assert f"action: script.{helper}" not in body

    if yaml is not None:
        package = yaml.safe_load(scheduler)
        scripts = package["script"]
        parsed_waits: list[str] = []
        for helper in ems_helper_specs:
            sequence = scripts[helper]["sequence"]
            waits = [
                item["wait_template"]
                for item in sequence
                if isinstance(item, dict) and "wait_template" in item
            ]
            assert len(waits) == 1, f"{helper} has an ambiguous ACK wait"
            parsed_waits.extend(waits)
        assert len(set(parsed_waits)) == 1, (
            "EMS helpers no longer share one parsed full-block ACK definition"
        )

    for helper, (service, generation, mirror) in direct_helper_specs.items():
        body = helpers.split(f"  {helper}:", 1)[1].split("\n  hoymiles_", 1)[0]
        service_pos = body.index(service)
        capture_pos = body.index("generation_after_write")
        wait_pos = body.index("wait_template:", capture_pos)
        assert service_pos < capture_pos < wait_pos
        assert generation in body[capture_pos:]
        assert mirror in body[wait_pos:]
        assert "mode: restart" in body
        capability = "sensor.hoymiles_hit_direct_register_verified_readback_supported"
        assert capability in body
        assert "| float(0) > 0.5" in body[wait_pos:]
        assert f"action: script.{helper}" not in body

    mode = helpers.split("  hoymiles_verified_set_ems_mode:", 1)[1].split(
        "\n  hoymiles_verified_set_ems_maximum_charge_power:", 1
    )[0]
    for mirror in full_block_mirrors:
        assert mirror in mode
    assert "'self_use': 0" in mode and "'off_grid': 3" in mode
    assert "'grid_charge': 4" in mode
    assert "'grid_discharge': 5" in mode
    physical_guard = mode.index("option == 'off_grid'")
    select_write = mode.index("action: select.select_option")
    assert physical_guard < select_write
    assert (
        "'sensor.hoymiles_ems_hardware_mode', 'off_grid'"
        in mode[physical_guard:select_write]
    ), "Verified mode helper can overwrite a late physical Off-Grid transition"
    late_guard = mode[physical_guard:select_write]
    for marker in (
        "sensor.hoymiles_hit_ems_verified_hardware_readback_supported",
        "is_number(states(",
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "| int(-1)) != 3",
    ):
        assert marker in late_guard, (
            "Verified mode helper lacks a direct raw-code3 late interlock"
        )

    def generation_not_older(current: int, baseline: int) -> bool:
        return (
            current == baseline
            or current > baseline
            or (baseline >= 16_000_000 and 1 <= current < baseline)
        )

    def generation_newer(current: int, baseline: int) -> bool:
        return current > baseline or (
            baseline >= 16_000_000 and 1 <= current < baseline
        )

    def full_block_helper_ack(
        *,
        supported: bool,
        generation_before: int,
        generation_after: int,
        generation_current: int,
        expected: tuple[float, ...],
        physical: tuple[float | None, ...] | None,
    ) -> bool:
        """Model the one shared HA completion predicate for EMS 4300-4306."""

        if (
            not supported
            or physical is None
            or len(expected) != 7
            or len(physical) != 7
            or any(value is None for value in physical)
            or not generation_not_older(generation_after, generation_before)
            or not generation_newer(generation_current, generation_after)
        ):
            return False
        actual = tuple(float(value) for value in physical if value is not None)
        return (
            int(actual[0]) == int(expected[0])
            and all(abs(actual[index] - expected[index]) < 0.5 for index in (1, 2, 3, 5))
            and all(abs(actual[index] - expected[index]) < 0.05 for index in (4, 6))
        )

    first_expected = (0.0, 25.0, 90.0, 98.0, 60.0, 0.0, 100.0)
    target_only_match = (0.0, 25.0, 90.0, 98.0, 60.0, 0.0, 90.0)
    assert not full_block_helper_ack(
        supported=True,
        generation_before=10,
        generation_after=10,
        generation_current=11,
        expected=first_expected,
        physical=target_only_match,
    ), "A matching target field cannot hide a mismatching sibling"
    assert full_block_helper_ack(
        supported=True,
        generation_before=10,
        generation_after=10,
        generation_current=11,
        expected=first_expected,
        physical=first_expected,
    )
    assert not full_block_helper_ack(
        supported=True,
        generation_before=10,
        generation_after=10,
        generation_current=10,
        expected=first_expected,
        physical=first_expected,
    ), "Cached matching values are not a physical acknowledgement"
    assert not full_block_helper_ack(
        supported=True,
        generation_before=10,
        generation_after=9,
        generation_current=11,
        expected=first_expected,
        physical=first_expected,
    ), "A stale post-service generation capture must fail closed"
    assert not full_block_helper_ack(
        supported=True,
        generation_before=10,
        generation_after=10,
        generation_current=11,
        expected=first_expected,
        physical=None,
    )
    assert not full_block_helper_ack(
        supported=False,
        generation_before=10,
        generation_after=10,
        generation_current=11,
        expected=first_expected,
        physical=first_expected,
    ), "Missing or stale verified readback must fail closed"

    # A family sequence cannot advance to its next helper on a target-only
    # match. After the complete ACK, the next ordinary single-field helper can
    # run and complete against its own newly frozen tuple.
    first_complete = full_block_helper_ack(
        supported=True,
        generation_before=20,
        generation_after=20,
        generation_current=21,
        expected=first_expected,
        physical=first_expected,
    )
    assert first_complete
    second_expected = first_expected[:5] + (30.0, first_expected[6])
    assert full_block_helper_ack(
        supported=True,
        generation_before=21,
        generation_after=21,
        generation_current=22,
        expected=second_expected,
        physical=second_expected,
    ), "A normal helper must work after the prior full-block ACK"
    assert full_block_helper_ack(
        supported=True,
        generation_before=16_000_000,
        generation_after=16_000_000,
        generation_current=1,
        expected=first_expected,
        physical=first_expected,
    ), "Generation wrap still needs the exact full block"
    assert full_block_helper_ack(
        supported=True,
        generation_before=16_000_000,
        generation_after=1,
        generation_current=2,
        expected=first_expected,
        physical=first_expected,
    ), "A service-time wrap must not deadlock the following full-block ACK"

    # Minimal family markers: every previously affected path still routes
    # through the corrected common helper layer; no family gets a duplicate
    # acknowledgement implementation.
    family_markers = {
        "RCE": (
            "script.hoymiles_verified_set_ems_maximum_discharge_power",
            "script.hoymiles_verified_set_ems_force_discharge_soc",
        ),
        "tariff": (
            "script.hoymiles_verified_set_ems_maximum_charge_power",
            "script.hoymiles_verified_set_ems_force_charge_soc",
        ),
        "balancing": (
            "hoymiles_stop_battery_balancing:",
            "script.hoymiles_verified_set_ems_maximum_charge_power",
        ),
        "RCEm": (
            "id: hoymiles_rcm_pre_discharge_control",
            "script.hoymiles_verified_set_ems_force_discharge_soc",
        ),
    }
    for family, markers in family_markers.items():
        for marker in markers:
            assert marker in scheduler, f"{family} bypasses the common EMS helper layer"

    tariff_rollback = scheduler.split(
        "  hoymiles_rollback_tariff_transaction:", 1
    )[1].split("\n  hoymiles_rollback_rce_transaction:", 1)[0]
    assert tariff_rollback.count(
        "number.hoymiles_hit_ems_complete_block_charge_rollback_command"
    ) == 1
    assert "script.hoymiles_verified_set_ems_maximum_charge_power" not in tariff_rollback
    assert "script.hoymiles_verified_set_ems_force_charge_soc" not in tariff_rollback
    assert "system_broadcast_with_master_fc03" in scheduler
    assert "individual_inverter_acknowledgement: unavailable" in scheduler

    def late_mode_write_allowed(
        option: str,
        capability: bool,
        raw_code_numeric: bool,
        raw_code: int,
        derived_mode: str,
    ) -> bool:
        return option == "off_grid" or (
            capability
            and raw_code_numeric
            and raw_code != 3
            and derived_mode != "off_grid"
        )

    assert late_mode_write_allowed("off_grid", False, False, 3, "off_grid")
    assert late_mode_write_allowed("self_use", True, True, 0, "self_use")
    assert not late_mode_write_allowed(
        "grid_charge", True, True, 3, "self_use"
    ), "Raw code3 must veto a non-OffGrid write even with a stale derived mode"
    assert not late_mode_write_allowed(
        "grid_discharge", True, False, -1, "self_use"
    )

    aggregate_sensor = scheduler.split(
        "# The event-driven sensor reports a system-level physical response", 1
    )[1].split("# Shared execution gate for the complete EMS block", 1)[0]
    for marker in (
        "event_type: hoymiles_parallel_aggregate_physical_response",
        'name: "Hoymiles Parallel Aggregate Physical Response"',
        "aggregate_system_power",
        "master_fc03",
        "individual_inverter_acknowledgement: unavailable",
        'formula: "P_battery = P_grid + P_load - P_pv"',
        "transition_grace_seconds: 20",
        "candidate_generations: 6",
        "required_stable_generations: 3",
        "transaction_started_epoch",
        "latched_esp_uptime_seconds",
        "latched_machine_type",
        "topology_known",
        "requires_parallel_proof",
        "authoritative_expected_power",
        "observed_median_power_kw",
        "sampled_transition_peak_kw",
        "sampled_transition_observed",
        "sampled_transition_scope",
        "best_effort_post_master_ack_boundaries_and_complete_candidates",
        "verification_horizon_seconds",
        "baseline_generation",
        "collection_baseline_generation",
        "final_generation",
    ):
        assert marker in aggregate_sensor, marker
    assert "per_slave" not in aggregate_sensor.lower()

    aggregate_helper = helpers.split(
        "  hoymiles_verify_parallel_aggregate_discharge_response:", 1
    )[1].split("\n  hoymiles_start_grid_discharge:", 1)[0]
    for marker in (
        "mode: parallel",
        "max: 4",
        "transaction_id:",
        "transaction_started_epoch:",
        "baseline_generation:",
        "latched_esp_uptime_seconds:",
        "latched_machine_type:",
        "latched_inverter_count:",
        "topology_known:",
        "requires_parallel_proof:",
        "response_started_epoch",
        "sensor.hoymiles_hit_parallel_aggregate_power_readback_generation",
        "sensor.hoymiles_hit_overview_battery_power",
        "sensor.hoymiles_hit_overview_grid_total_active_power",
        "last_reported",
        "response_sample_1_valid",
        "response_sample_2_valid",
        "response_sample_3_valid",
        "response_sample_4_valid",
        "response_sample_5_valid",
        "response_sample_6_valid",
        "response_sample_count",
        "response_window_1_stable",
        "response_window_2_stable",
        "response_window_3_stable",
        "response_window_4_stable",
        "response_window_1_target_compatible",
        "response_window_2_target_compatible",
        "response_window_3_target_compatible",
        "response_window_4_target_compatible",
        "response_stable_window_start",
        "response_sampled_transition_peak_kw",
        "response_sampled_transition_observed",
        "response_collection_baseline_generation",
        "response_esp_uptime_after_grace",
        "response_esp_uptime_still_monotonic",
        "response_median_kw",
        "response_spread_kw",
        "authoritative_target_mismatch_after_transition_grace",
        "aggregate_generation_reset_during_transition",
        "esp_restart_during_response_verification",
        "fresh_sample_timeout",
        "missing_required_direction_after_transition_grace",
        "stable_sample_window_timeout",
        "result: pending",
        "result: not_evaluable",
        "confirmed",
    ):
        assert marker in aggregate_helper, marker
    # One 20-second transition-grace wait plus six candidate generations.
    assert aggregate_helper.count('timeout: "00:00:20"') == 7
    assert aggregate_helper.count("verification_horizon_seconds: 155") == 7
    assert (
        aggregate_helper.count(
            "or not is_number(states('sensor.hoymiles_hit_esp_uptime'))"
        )
        == 6
    )
    for sample in range(1, 7):
        assert aggregate_helper.count(f"response_esp_uptime_{sample}") >= 2
    assert "* 0.15" in aggregate_helper
    assert "* 0.10" in aggregate_helper
    assert "response_expected_kw | float(0)) >= 1.0" in aggregate_helper
    assert "action: select.select_option" not in aggregate_helper
    assert "action: number.set_value" not in aggregate_helper
    assert "per_slave" not in aggregate_helper.lower()
    assert "mode: restart" not in aggregate_helper.split("fields:", 1)[0]
    assert "transition_peak_kw:" not in aggregate_helper.replace(
        "sampled_transition_peak_kw:", ""
    )
    grace = aggregate_helper.index("transition grace")
    collection_baseline = aggregate_helper.index(
        "response_collection_baseline_generation", grace
    )
    first_candidate = aggregate_helper.index("response_generation_1", collection_baseline)
    assert grace < collection_baseline < first_candidate
    assert "64.0" not in aggregate_helper
    assert "< (response_baseline_generation | float(-1))" in aggregate_helper
    assert "> (response_collection_baseline_generation | float(-1))" in aggregate_helper
    assert (
        aggregate_helper.count(
            "> (response_collection_baseline_generation | float(-1))"
        )
        == 12
    ), "Every wait and candidate must stay above the post-grace boot floor"
    for generation in range(1, 6):
        assert (
            f"> (response_generation_{generation} | float(-1))"
            in aggregate_helper
        )
        assert (
            f"!= (response_generation_{generation} | float(-1))"
            not in aggregate_helper
        )

    def topology_contract(machine_type: int, count: int) -> tuple[bool, bool]:
        known = (machine_type == 0 and count == 1) or (
            machine_type == 1 and 2 <= count <= 10
        )
        requires_parallel_proof = machine_type == 1 and 2 <= count <= 10
        return known, requires_parallel_proof

    assert topology_contract(0, 1) == (True, False)
    assert topology_contract(1, 2) == (True, True)
    assert topology_contract(-1, -1) == (False, False)
    assert topology_contract(1, 1) == (False, False)

    def collection_generation_valid(precommand: float, after_grace: float) -> bool:
        return after_grace >= precommand

    assert collection_generation_valid(100, 102)
    assert not collection_generation_valid(100, 2), (
        "An ESP generation reset during grace must fail before sample collection"
    )
    assert 103 > 102
    assert not 2 > 100

    def generation_sequence_valid(baseline: float, generations: list[float]) -> bool:
        previous = baseline
        for generation in generations:
            if generation <= baseline or generation <= previous:
                return False
            previous = generation
        return True

    assert generation_sequence_valid(102, [103, 104, 105, 106, 107, 108])
    assert not generation_sequence_valid(102, [1, 2, 3, 4, 5])
    assert not generation_sequence_valid(102, [103, 104, 1, 2, 3]), (
        "A reset after collection begins must not create a later valid window"
    )

    def uptime_remains_in_same_boot(
        latched_uptime: float, observed_uptimes: list[float]
    ) -> bool:
        return latched_uptime >= 180 and all(
            uptime >= latched_uptime for uptime in observed_uptimes
        )

    assert uptime_remains_in_same_boot(1000, [1000, 1000, 1060, 1060, 1120])
    assert not uptime_remains_in_same_boot(1000, [1000, 1, 2, 3, 4]), (
        "Generation 2 catching up after reboot cannot cross the uptime boot latch"
    )
    assert not uptime_remains_in_same_boot(179, [180, 181, 182])

    def stable_response_window(
        battery_kw: list[float],
        grid_kw: list[float],
        expected_kw: float,
        *,
        authoritative: bool,
    ) -> tuple[int, float | None]:
        """Mirror the four overlapping windows encoded in the HA script."""

        assert len(battery_kw) == 6
        assert len(grid_kw) == 6
        for start in range(4):
            battery_window = battery_kw[start : start + 3]
            grid_window = grid_kw[start : start + 3]
            median = sorted(battery_window)[1]
            spread_reference = expected_kw if authoritative else median
            if (
                min(battery_window) >= 0.5
                and (not authoritative or min(grid_window) >= 0.25)
                and max(battery_window) - min(battery_window)
                <= max(1.0, spread_reference * 0.10)
                and (
                    not authoritative
                    or abs(median - expected_kw)
                    <= max(1.0, expected_kw * 0.15)
                )
            ):
                return start + 1, median
        return 0, None

    # Six clean generations expose the first of four exact three-sample windows.
    expected_kw = 33.75
    window, median_kw = stable_response_window(
        [33.65, 33.75, 33.86, 33.70, 33.80, 33.72],
        [29.10, 29.20, 29.23, 29.16, 29.25, 29.18],
        expected_kw,
        authoritative=True,
    )
    assert window == 1 and median_kw is not None
    assert abs(median_kw - expected_kw) <= max(1.0, expected_kw * 0.15)

    # One isolated constructional peak at every position remains diagnostic;
    # confirmation must come only from a clean, separate window.
    normal_battery = [33.65, 33.75, 33.86, 33.70, 33.80, 33.72]
    normal_grid = [29.10, 29.20, 29.23, 29.16, 29.25, 29.18]
    for peak_position in range(6):
        battery_samples = list(normal_battery)
        grid_samples = list(normal_grid)
        battery_samples[peak_position] = 62.0
        grid_samples[peak_position] = 57.0
        window, median_kw = stable_response_window(
            battery_samples,
            grid_samples,
            expected_kw,
            authoritative=True,
        )
        assert window > 0 and median_kw is not None, peak_position + 1
        selected_positions = set(range(window - 1, window + 2))
        assert peak_position not in selected_positions, (
            "An isolated sampled peak was accepted as stable evidence"
        )

    # Two consecutive central peaks intersect every candidate window. They
    # cannot be mistaken for stable target response.
    consecutive_peak_battery = list(normal_battery)
    consecutive_peak_grid = list(normal_grid)
    for peak_position in (2, 3):
        consecutive_peak_battery[peak_position] = 62.0
        consecutive_peak_grid[peak_position] = 57.0
    assert stable_response_window(
        consecutive_peak_battery,
        consecutive_peak_grid,
        expected_kw,
        authoritative=True,
    ) == (0, None)

    # Missing export and persistent low/high plateaux all fail closed after
    # all four windows have been exhausted.
    assert stable_response_window(
        [-2.0, -1.5, -1.0, -0.8, -0.5, -0.4],
        [-2.0, -1.5, -1.0, -0.8, -0.5, -0.4],
        expected_kw,
        authoritative=True,
    ) == (0, None)
    assert stable_response_window(
        [50.0, 50.1, 49.9, 50.0, 50.1, 49.9],
        [45.0, 45.1, 44.9, 45.0, 45.1, 44.9],
        expected_kw,
        authoritative=True,
    ) == (0, None)
    assert stable_response_window(
        [20.0, 20.1, 19.9, 20.0, 20.1, 19.9],
        [15.0, 15.1, 14.9, 15.0, 15.1, 14.9],
        expected_kw,
        authoritative=True,
    ) == (0, None)
    assert "response_sampled_transition_peak_kw" in aggregate_helper
    assert "best-effort sampled peak" in aggregate_helper
    assert "verification_horizon_seconds: 155" in aggregate_helper
    assert "stable_sample_window_timeout" in aggregate_helper
    assert "script.hoymiles_rollback_rce_transaction" in scheduler

    # Under high local load the battery can discharge while the site still
    # imports. Manual/RCEm need battery direction only; authoritative RCE also
    # requires physical grid export and therefore rejects the same samples.
    high_load_battery = [20.0, 20.1, 19.9, 20.0, 20.1, 19.9]
    high_load_grid = [-5.0, -4.9, -5.1, -5.0, -4.9, -5.1]
    assert stable_response_window(
        high_load_battery,
        high_load_grid,
        20.0,
        authoritative=False,
    )[0] == 1
    assert stable_response_window(
        high_load_battery,
        high_load_grid,
        20.0,
        authoritative=True,
    ) == (0, None)

    manual_start = scheduler.split(
        "  hoymiles_start_grid_discharge:", 1
    )[1].split("\n  hoymiles_start_grid_charge:", 1)[0]
    manual_baseline = manual_start.index(
        "manual_parallel_generation_before_mode"
    )
    manual_topology_gate = manual_start.index(
        'value_template: "{{ manual_topology_known | bool(false) }}"',
        manual_baseline,
    )
    manual_uptime_gate = manual_start.index(
        'value_template: "{{ manual_esp_uptime_ready | bool(false) }}"',
        manual_topology_gate,
    )
    manual_mode = manual_start.index('option: "grid_discharge"', manual_baseline)
    manual_master_ack = manual_start.index(
        "sensor.hoymiles_ems_hardware_mode", manual_mode
    )
    manual_timer = manual_start.index("action: timer.start", manual_master_ack)
    manual_aggregate = manual_start.index(
        "script.hoymiles_verify_parallel_aggregate_discharge_response",
        manual_timer,
    )
    assert (
        manual_baseline
        < manual_topology_gate
        < manual_uptime_gate
        < manual_mode
        < manual_master_ack
        < manual_timer
        < manual_aggregate
    )
    mode_ack_to_timer = manual_start[manual_mode:manual_timer]
    assert "wait_template:" not in mode_ack_to_timer
    assert "delay:" not in mode_ack_to_timer
    assert "state: \"grid_discharge\"" in mode_ack_to_timer
    assert manual_start.count("action: timer.start") == 1, (
        "Manual verification must not restart or extend the requested timer"
    )
    timer_block = manual_start[manual_timer:manual_aggregate]
    assert (
        "{{ (states('input_number.hoymiles_discharge_duration') | int(90)) * 60 }}"
        in timer_block
    )
    # The watchdog runs every minute, while nominal verification can consume
    # 20 s grace + six 13 s generations. Starting the exact timer first means
    # it never observes a claimed manual owner with an idle timer in that gap.
    helper_nominal_seconds = 20 + 6 * 13
    watchdog_period_seconds = 60
    assert helper_nominal_seconds > watchdog_period_seconds
    assert manual_timer < manual_aggregate
    manual_policy = manual_start[manual_aggregate:]
    assert "authoritative_expected_power: false" in manual_policy
    assert "expected_power_kw:" not in manual_policy
    assert "sensor.hoymiles_parallel_aggregate_physical_response" in manual_policy
    assert "transaction_id: \"{{ manual_response_transaction_id }}\"" in manual_policy
    assert "{{ manual_latched_esp_uptime_seconds }}" in manual_policy
    assert "continue_on_error: true" in manual_policy
    assert "'owner') == 'manual'" in manual_policy
    assert "'completed_at'" in manual_policy
    assert 'option: "self_use"' in manual_policy
    assert "action: timer.cancel" in manual_policy
    assert "tolerance_kw" not in manual_policy
    manual_postcheck = manual_policy.split("# Manual mode has no authoritative", 1)[1]
    assert "manual_requires_parallel_proof" in manual_postcheck
    assert "manual_topology_known" in manual_postcheck
    assert "states('sensor.hoymiles_hit_machines_type')" not in manual_postcheck

    manual_failure = manual_postcheck.split(
        "# Stop the requested-duration clock immediately.", 1
    )[1].split("- stop:", 1)[0]
    failure_timer_cancel = manual_failure.index("action: timer.cancel")
    failure_neutral_write = manual_failure.index('option: "self_use"')
    guarded_release = manual_failure.index(
        "# Release ownership only after the same physical neutral readback"
    )
    owner_release = manual_failure.index(
        "action: input_boolean.turn_off", guarded_release
    )
    assert failure_timer_cancel < failure_neutral_write < guarded_release < owner_release
    assert "continue_on_error: true" in manual_failure[
        failure_neutral_write - 120 : guarded_release
    ]
    assert "in ['self_use', 'off_grid']" in manual_failure[
        guarded_release:owner_release
    ]
    assert "is_state('timer.hoymiles_discharge', 'idle')" in manual_failure[
        guarded_release:owner_release
    ]
    assert manual_failure.count("action: input_boolean.turn_off") == 1

    def failed_manual_response_cleanup(
        *, rollback_readback_confirmed: bool
    ) -> tuple[bool, bool]:
        """Return (owner_active, timer_active) after aggregate-failure cleanup."""

        timer_active = False
        owner_active = not rollback_readback_confirmed
        return owner_active, timer_active

    assert failed_manual_response_cleanup(
        rollback_readback_confirmed=True
    ) == (False, False)
    assert failed_manual_response_cleanup(
        rollback_readback_confirmed=False
    ) == (True, False), (
        "Unavailable rollback must retain owner + idle timer for watchdog retry"
    )
    finish_watchdog = scheduler.split("id: hoymiles_finish_grid_discharge", 1)[1]
    finish_watchdog = finish_watchdog.split("\n  - id:", 1)[0]
    assert 'minutes: "/1"' in finish_watchdog
    assert "input_boolean.hoymiles_discharge_cycle_active" in finish_watchdog
    assert "is_state('timer.hoymiles_discharge', 'idle')" in finish_watchdog

    rce_control = scheduler.split(
        "id: hoymiles_rce_grid_discharge_control", 1
    )[1].split("\n  - id:", 1)[0]
    frozen_target = rce_control.index("rce_start_expected_power_kw")
    first_owned_write = rce_control.index(
        "script.hoymiles_verified_set_ems_maximum_discharge_power",
        frozen_target,
    )
    rce_baseline = rce_control.index(
        "rce_parallel_generation_before_mode", first_owned_write
    )
    rce_topology_gate = rce_control.index(
        'value_template: "{{ rce_topology_known | bool(false) }}"', rce_baseline
    )
    rce_uptime_gate = rce_control.index(
        'value_template: "{{ rce_esp_uptime_ready | bool(false) }}"',
        rce_topology_gate,
    )
    rce_mode = rce_control.index('option: "grid_discharge"', rce_baseline)
    rce_master_ack = rce_control.index("wait_template:", rce_mode)
    rce_aggregate = rce_control.index(
        "script.hoymiles_verify_parallel_aggregate_discharge_response",
        rce_master_ack,
    )
    assert (
        frozen_target
        < first_owned_write
        < rce_baseline
        < rce_topology_gate
        < rce_uptime_gate
        < rce_mode
    )
    assert rce_mode < rce_master_ack < rce_aggregate
    rce_physical = rce_control[rce_baseline:]
    assert "current_slot_execution_discharge_power_kw" not in (
        rce_physical.split("script.hoymiles_verify_parallel_aggregate", 1)[1]
    )
    for marker in (
        "expected_power_kw:",
        "{{ rce_start_expected_power_kw }}",
        "authoritative_expected_power: true",
        "sensor.hoymiles_parallel_aggregate_physical_response",
        "rce_response_transaction_id",
        "rce_response_transaction_started_epoch",
        "rce_latched_machine_type",
        "rce_latched_inverter_count",
        "rce_latched_esp_uptime_seconds",
        "rce_esp_uptime_ready",
        "rce_topology_known",
        "rce_requires_parallel_proof",
        "'transaction_id') == rce_response_transaction_id",
        "'owner') == 'rce'",
        "'completed_at'",
        "script.hoymiles_rollback_rce_transaction",
    ):
        assert marker in rce_physical, marker
    fail_closed = rce_physical.split(
        "script.hoymiles_verify_parallel_aggregate_discharge_response", 1
    )[1].split(
        "# A successful mode ACK is not permission", 1
    )[0]
    assert fail_closed.index(
        "sensor.hoymiles_parallel_aggregate_physical_response"
    ) < fail_closed.index("script.hoymiles_rollback_rce_transaction")
    assert "rce_requires_parallel_proof" in fail_closed
    assert "rce_topology_known" in fail_closed
    assert "continue_on_error: true" in fail_closed
    assert "states('sensor.hoymiles_hit_machines_type')" not in fail_closed

    # Every caller supplies an independently generated transaction plus its
    # frozen topology. This prevents a parallel helper or HA restart from
    # satisfying a different owner's post-command decision.
    recovery_control = scheduler.split(
        "id: hoymiles_restore_ems_cycle_after_ha_restart", 1
    )[1].split("\n  - id:", 1)[0]
    rcm_pre_discharge = scheduler.split(
        "id: hoymiles_rcm_pre_discharge_control", 1
    )[1].split("\n  - id:", 1)[0]
    caller_contracts = (
        (manual_start, "manual_response", "manual_latched"),
        (rce_control, "rce_response", "rce_latched"),
        (recovery_control, "recovery_response", "recovery_latched"),
        (rcm_pre_discharge, "rcm_response", "rcm_latched"),
    )
    for caller, transaction_prefix, topology_prefix in caller_contracts:
        mode_position = caller.index('option: "grid_discharge"')
        precommand = caller[:mode_position]
        assert (
            f"({topology_prefix}_esp_uptime_seconds | float(-1)) >= 180"
            in precommand
        )
        assert "age >= -5 and age <= 180" in precommand
        action = caller.split(
            "script.hoymiles_verify_parallel_aggregate_discharge_response", 1
        )[1]
        for marker in (
            f'transaction_id: "{{{{ {transaction_prefix}_transaction_id }}}}"',
            f"{{{{ {transaction_prefix}_transaction_started_epoch }}}}",
            f'latched_machine_type: "{{{{ {topology_prefix}_machine_type }}}}"',
            f'latched_inverter_count: "{{{{ {topology_prefix}_inverter_count }}}}"',
            f"{{{{ {topology_prefix}_esp_uptime_seconds }}}}",
            "topology_known:",
            "requires_parallel_proof:",
        ):
            assert marker in action, marker

    for caller, gate, uptime_gate in (
        (
            recovery_control,
            "recovery_topology_known",
            "recovery_esp_uptime_ready",
        ),
        (rcm_pre_discharge, "rcm_topology_known", "rcm_esp_uptime_ready"),
    ):
        latch = caller.index(f"{gate}:")
        gate_position = caller.index(
            f'value_template: "{{{{ {gate} | bool(false) }}}}"', latch
        )
        uptime_gate_position = caller.index(
            f'value_template: "{{{{ {uptime_gate} | bool(false) }}}}"',
            gate_position,
        )
        mode_position = caller.index('option: "grid_discharge"', uptime_gate_position)
        assert latch < gate_position < uptime_gate_position < mode_position

    # A failed discharge-response transaction may request the existing neutral
    # rollback, but the rollback itself must never wait on aggregate power.
    for rollback_name, next_name in (
        (
            "hoymiles_rollback_tariff_transaction:",
            "hoymiles_rollback_rce_transaction:",
        ),
        (
            "hoymiles_rollback_rce_transaction:",
            "hoymiles_start_battery_balancing:",
        ),
    ):
        rollback = scheduler.split(rollback_name, 1)[1].split(next_name, 1)[0]
        assert "parallel_aggregate_physical_response" not in rollback

    hardware_mode = scheduler.split(
        '- name: "Hoymiles EMS Hardware Mode"', 1
    )[1].split('- name: "Hoymiles Actual Load Power"', 1)[0]
    for marker in (
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "sensor.hoymiles_hit_ems_control_readback_generation",
        "sensor.hoymiles_hit_ems_verified_hardware_readback_supported",
        "{0: 'self_use', 3: 'off_grid', 4: 'grid_charge'",
        "5: 'grid_discharge'",
    ):
        assert marker in hardware_mode

    # The writable select is a command transport, never an acknowledgement.
    # It may occur only as the verified mode helper's service target; all
    # policy reads and triggers consume the physical-code-derived alias.
    writable_mode = "select.hoymiles_hit_ems_mode"
    assert scheduler.count(writable_mode) == 1
    assert f"entity_id: {writable_mode}" in mode

    owner = scheduler.split(
        '- name: "Hoymiles EMS Control Owner"', 1
    )[1].split('- name: "Hoymiles RCEm Maximum Grid Voltage"', 1)[0]
    owner_state = owner.split("state: >-", 1)[1].split("attributes:", 1)[0]
    assert "hoymiles_rce_discharge_active" in owner_state
    assert "hoymiles_tariff_charge_active" in owner_state
    assert "hoymiles_rcm_active" in owner_state
    assert "hoymiles_rcm_export_control_active" in owner_state
    assert "hoymiles_rcm_pre_discharge_active" in owner_state
    assert "hoymiles_rce_discharge_enabled" not in owner_state
    assert "hoymiles_tariff_charge_enabled" not in owner_state
    assert "hoymiles_rcm_enabled" not in owner_state
    for visible_owner in (
        "Balansowanie",
        "Sterowanie ręczne",
        "RCE",
        "Tanie ładowanie",
        "RCEm",
        "Brak aktywnej automatyki",
    ):
        assert visible_owner in owner_state
    fallback_owner = owner_state.rsplit("{% else %}", 1)[1]
    assert "Brak aktywnej automatyki" in fallback_owner
    assert "Sterowanie ręczne" not in fallback_owner
    owner_attributes = owner.split("attributes:", 1)[1]
    assert "owner_code: >-" in owner_attributes
    assert "manual" in owner_attributes

    conflict = scheduler.split(
        '- name: "Hoymiles EMS Control Conflict"', 1
    )[1].split('- name: "Hoymiles RCEm Risk Window Active"', 1)[0]
    running = conflict.split("{% set running =", 1)[1].split(
        "{% set export_owner =", 1
    )[0]
    foreign_running = conflict.split("{% set foreign_running =", 1)[1].split(
        "{{ configured > 1", 1
    )[0]
    assert "hoymiles_rcm_active" in running
    assert "hoymiles_rcm_pre_discharge_active" in running
    assert " or is_state(" in running
    assert "hoymiles_rcm_active" not in foreign_running
    assert "hoymiles_rcm_pre_discharge_active" not in foreign_running

    tariff_rollback = scheduler.split(
        "hoymiles_rollback_tariff_transaction:", 1
    )[1].split("hoymiles_rollback_rce_transaction:", 1)[0]
    assert "rollback_expected_mode" in tariff_rollback
    assert "== 3 else 0" in tariff_rollback
    assert "sensor.hoymiles_hit_ems_mode_readback_code" in tariff_rollback
    rce_rollback = scheduler.split(
        "hoymiles_rollback_rce_transaction:", 1
    )[1].split("hoymiles_start_battery_balancing:", 1)[0]
    assert "rollback_preserve_off_grid" in rce_rollback
    assert "'sensor.hoymiles_ems_hardware_mode', 'off_grid'" in rce_rollback

    balancing_stop_wrapper = scheduler.split(
        "hoymiles_stop_battery_balancing:", 1
    )[1].split("hoymiles_battery_balancing_transaction_worker:", 1)[0]
    assert "mode: queued" in balancing_stop_wrapper
    assert "script.hoymiles_battery_balancing_request_abort" in balancing_stop_wrapper
    for forbidden in (
        "script.hoymiles_verified_set_ems_maximum_charge_power",
        "script.hoymiles_verified_set_ems_force_charge_soc",
        "script.hoymiles_verified_set_ems_mode",
        "script.hoymiles_notify_battery_balancing_lifecycle",
    ):
        assert forbidden not in balancing_stop_wrapper

    balancing_worker = scheduler.split(
        "hoymiles_battery_balancing_transaction_worker:", 1
    )[1].split("\nautomation:", 1)[0]
    balancing_restore = balancing_worker.split(
        "# Restoration or provisional-owner release always wins", 1
    )[1].split("# Only the worker creates a monotonic cycle", 1)[0]
    for marker in (
        "trusted_snapshot",
        "saved_mode",
        "saved_4303",
        "saved_4304",
        "off_grid_owner",
        "binary_sensor.hoymiles_battery_balancing_restore_authorized",
        "final_restore_ack",
        "release_abort_generation",
    ):
        assert marker in balancing_restore
    charge_restore = balancing_restore.index(
        "action: script.hoymiles_verified_set_ems_maximum_charge_power"
    )
    soc_restore = balancing_restore.index(
        "action: script.hoymiles_verified_set_ems_force_charge_soc"
    )
    mode_restore = balancing_restore.index(
        "action: script.hoymiles_verified_set_ems_mode"
    )
    assert charge_restore < soc_restore < mode_restore
    assert balancing_restore.index("final_restore_ack:") > mode_restore
    final_release = balancing_restore.split(
        "# This is the final live boundary", 1
    )[1].split("                    default:", 1)[0]
    owner_release = final_release.index("action: input_boolean.turn_off")
    terminal_record = final_release.index('transaction_state: "NOTIFICATION_PENDING"')
    terminal_enqueue = final_release.index(
        "script.hoymiles_notify_battery_balancing_lifecycle"
    )
    assert owner_release < terminal_record < terminal_enqueue
    assert "action: notify." not in balancing_restore
    assert "notify.mobile_app_" not in balancing_restore

    rcm_main = scheduler.split(
        "id: hoymiles_rcm_voltage_charge_control", 1
    )[1].split("id: hoymiles_rcm_pre_discharge_control", 1)[0]
    rcm_pre = scheduler.split(
        "id: hoymiles_rcm_pre_discharge_control", 1
    )[1]
    for controller in (rcm_main, rcm_pre):
        assert "'sensor.hoymiles_ems_hardware_mode', 'off_grid'" in controller

    # No production path may call a writable HIT entity directly. The only
    # exception is the tariff rollback's single explicit full-block command,
    # already proven above to await the complete physical ACK.
    production = scheduler.split("  hoymiles_start_grid_discharge:", 1)[1]
    production_without_tariff_rollback = production.split(
        "  hoymiles_rollback_tariff_transaction:", 1
    )[0] + production.split("  hoymiles_rollback_rce_transaction:", 1)[1]
    assert "action: number.set_value" not in production_without_tariff_rollback
    assert "action: select.select_option" not in production_without_tariff_rollback
    assert writable_mode not in production_without_tariff_rollback
    assert "sensor.hoymiles_ems_hardware_mode" in production_without_tariff_rollback

    execution_ready = scheduler.split(
        '- name: "Hoymiles EMS Execution Ready"', 1
    )[1].split('- name: "Hoymiles RCE Control Data Ready"', 1)[0]
    for marker in (
        "ems_verified_hardware_readback_supported",
        "direct_register_verified_readback_supported",
        "Hoymiles Direct Register Execution Ready",
        "system_broadcast_with_master_fc03",
        "ems_mode_readback_code",
        "ems_control_readback_generation",
        "parallel_topology_readback_generation",
        "ems_age >= -5",
        "topology_age >= -5",
    ):
        assert marker in execution_ready
    assert "parallel_ems_control_status" not in execution_ready

    export_allowed = scheduler.split(
        '- name: "Hoymiles EMS Export Allowed"', 1
    )[1].split('- name: "Hoymiles Tariff Control Data Ready"', 1)[0]
    assert "gcf_enable_readback_code" in export_allowed
    assert "gcf_maximum_export_power_readback" in export_allowed
    assert "gcf_control_readback_generation" in export_allowed
    assert "select.hoymiles_hit_generation_control_function" not in export_allowed
    assert "number.hoymiles_hit_maximum_export_power_limit" not in export_allowed

    assert "number.hoymiles_hit_self_use_soc" not in scheduler
    tariff_snapshot = scheduler.split(
        "# Snapshot both shared registers, freeze the accepted action", 1
    )[1].split("# Idempotent setting writes", 1)[0]
    assert "ems_maximum_charge_power_readback" in tariff_snapshot
    assert "ems_force_charge_soc_readback" in tariff_snapshot
    rce_snapshot = scheduler.split(
        "# Snapshot both shared registers and claim durable RCE ownership", 1
    )[1].split("entity_id: input_boolean.hoymiles_rce_discharge_active", 1)[0]
    assert "ems_maximum_discharge_power_readback" in rce_snapshot
    assert "ems_force_discharge_soc_readback" in rce_snapshot
    balancing_snapshot = balancing_worker.split(
        "# Only the worker creates a monotonic cycle", 1
    )[1].split("default:\n          # Routine reconcile", 1)[0]
    assert "ems_maximum_charge_power_readback" in balancing_snapshot
    assert "ems_force_charge_soc_readback" in balancing_snapshot
    assert "captured_ems_generation" in balancing_snapshot
    assert "captured_topology_generation" in balancing_snapshot
    owner_acquired = balancing_snapshot.index('transaction_state: "OWNER_ACQUIRED"')
    snapshot_capture = balancing_snapshot.index("captured_mode:")
    snapshot_valid = balancing_snapshot.index('transaction_state: "SNAPSHOT_VALID"')
    assert owner_acquired < snapshot_capture < snapshot_valid

    saved_gcf = scheduler.split("hoymiles_rcm_saved_export_limit:", 1)[1].split(
        "hoymiles_rcm_saved_max_discharge_power:", 1
    )[0]
    assert "min: -10" in saved_gcf and "max: 200" in saved_gcf
    gcf_helper = helpers.split("hoymiles_verified_set_gcf_export_limit:", 1)[1]
    assert "min: -10" in gcf_helper and "max: 200" in gcf_helper

    main = scheduler.split("id: hoymiles_rcm_voltage_charge_control", 1)[1].split(
        "id: hoymiles_rcm_pre_discharge_control", 1
    )[0]
    emergency = main.split(
        "# Charge and export ownership are independent", 1
    )[1].split("# Final emergency interlock", 1)[0]
    charge_call = emergency.index(
        "action: script.hoymiles_verified_set_battery_max_charge_power"
    )
    export_call = emergency.index(
        "action: script.hoymiles_verified_set_gcf_export_limit"
    )
    assert "continue_on_error: true" in emergency[charge_call:export_call]
    assert "continue_on_error: true" in emergency[export_call:]
    assert "battery_max_charge_power_readback" in emergency
    assert "gcf_maximum_export_power_readback" in emergency

    # A syntactically valid block scalar can still swallow an accidentally
    # indented ``then``. Parse the structure when PyYAML is available and prove
    # that balancing restores both owned 4304 and 4303 registers as actions.
    if yaml is not None:
        package = yaml.safe_load(scheduler)
        worker_sequence = package["script"][
            "hoymiles_battery_balancing_transaction_worker"
        ][
            "sequence"
        ]
        def nested_dicts(value: object):
            if isinstance(value, dict):
                yield value
                for child in value.values():
                    yield from nested_dicts(child)
            elif isinstance(value, list):
                for child in value:
                    yield from nested_dicts(child)

        worker_actions = {
            item.get("action")
            for item in nested_dicts(worker_sequence)
            if isinstance(item.get("action"), str)
        }
        physical_helpers = {
            "script.hoymiles_verified_set_ems_maximum_charge_power",
            "script.hoymiles_verified_set_ems_force_charge_soc",
            "script.hoymiles_verified_set_ems_mode",
        }
        assert physical_helpers <= worker_actions
        for script_name, script_body in package["script"].items():
            if (
                "battery_balancing" in script_name
                and script_name != "hoymiles_battery_balancing_transaction_worker"
            ):
                other_actions = {
                    item.get("action")
                    for item in nested_dicts(script_body)
                    if isinstance(item.get("action"), str)
                }
                assert not physical_helpers.intersection(other_actions), script_name


def assert_human_control_status_contracts() -> None:
    """Keep enabled policy, planned work and active ownership distinct in UI."""

    scheduler = (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
    ).read_text(encoding="utf-8")
    tariff = scheduler.split(
        '- name: "Hoymiles Tariff Charge Status"', 1
    )[1].split(
        '- name: "Hoymiles Battery Balancing BMS Safe Charge Power"', 1
    )[0]
    tariff_state = tariff.split("state: >-", 1)[1]

    # A running transaction is the primary truth.  The frozen action describes
    # what the owner is actually doing even if the live optimizer republishes.
    assert "input_text.hoymiles_tariff_active_action" in tariff_state
    assert "'current_action'" not in tariff_state
    active_branch = tariff_state.index(
        "is_state('input_boolean.hoymiles_tariff_charge_active', 'on')"
    )
    disabled_branch = tariff_state.index(
        "is_state('input_boolean.hoymiles_tariff_charge_enabled', 'off')",
        active_branch + 1,
    )
    rce_block = tariff_state.index(
        "is_state('input_boolean.hoymiles_rce_discharge_enabled', 'on')"
    )
    assert active_branch < disabled_branch < rce_block

    for human_state in (
        "Wyłączone — kończenie aktywnego bloku",
        "Aktywne — dom zasilany z taniej sieci",
        "Aktywne — ładowanie z sieci",
        "Aktywne — sterowanie taryfowe",
        "Wyłączone",
        "Niedostępne — trwa inicjalizacja",
        "Włączone — zablokowane: włączona polityka RCE",
        "Włączone — zablokowane: plan niedostępny",
        "Włączone — oczekuje na aktualny plan",
        "Włączone — brak potrzeby ładowania",
        "Włączone — wybrany blok oczekuje na rozpoczęcie",
        "Włączone — oczekuje na wybrany blok",
    ):
        assert human_state in tariff_state
    for blocking_status in (
        "missing_data",
        "optimizer_error",
        "unsupported_profile",
        "expired_profile",
        "soc_limits_conflict",
        "hard_reserve_unavailable",
    ):
        assert blocking_status in tariff_state
    assert "recalculation_pending" in tariff_state
    assert "result_current" in tariff_state
    assert "current_slot_planned" in tariff_state
    assert "planned_slots" in tariff_state
    assert "hoymiles_tariff_control_data_ready" in tariff_state
    no_need_branch = tariff_state.split(
        "{% elif status == 'no_charge_needed' %}", 1
    )[1].split("{% elif current_slot %}", 1)[0]
    assert "Włączone — brak potrzeby ładowania" in no_need_branch
    for distinct_result in (
        "no_discount_window",
        "no_cheap_window",
        "not_economically_beneficial",
        "shortage_in_low_period",
    ):
        assert distinct_result not in no_need_branch
    final_result = tariff_state.split("{% elif planned_slots | count > 0 %}", 1)[
        1
    ].split("{% endif %}", 1)[0]
    assert "Włączone — oczekuje na wybrany blok" in final_result
    assert "Włączone — {{ plan_state }}" in final_result

    dashboard = (ROOT / "dashboard_hoymiles.yaml").read_text(encoding="utf-8")
    main_ems = dashboard.split("title: Sterowanie EMS", 1)[1].split(
        "\n      - type:", 1
    )[0]
    main_owner = main_ems.index("entity: sensor.hoymiles_ems_control_owner")
    main_tariff = main_ems.index("entity: sensor.hoymiles_tariff_charge_status")
    main_conflict = main_ems.index("entity: binary_sensor.hoymiles_ems_control_conflict")
    assert main_owner < main_tariff < main_conflict

    tariff_view = dashboard.split("path: ladowanie-taryfowe", 1)[1].split(
        "  - title: RCEm 253 V+", 1
    )[0]
    assert tariff_view.count("entity: sensor.hoymiles_ems_control_owner") == 1
    assert tariff_view.count("entity: sensor.hoymiles_tariff_charge_status") == 1
    assert tariff_view.count("entity: binary_sensor.hoymiles_ems_control_conflict") == 1
    tariff_owner = tariff_view.index("entity: sensor.hoymiles_ems_control_owner")
    tariff_policy = tariff_view.index("entity: sensor.hoymiles_tariff_charge_status")
    tariff_conflict = tariff_view.index(
        "entity: binary_sensor.hoymiles_ems_control_conflict"
    )
    tariff_toggle = tariff_view.index(
        "entity: input_boolean.hoymiles_tariff_charge_enabled"
    )
    assert tariff_owner < tariff_policy < tariff_conflict < tariff_toggle


def assert_rcm_timeline_observation_boundary() -> None:
    """Keep AP-2R1 outside every scheduler, helper and inverter write path."""

    component = ROOT / "custom_components" / "hoymiles_hit_modbus"
    model = (component / "rcm_timeline_model.py").read_text(encoding="utf-8")
    source = (component / "rcm_sensor.py").read_text(encoding="utf-8")
    platform = (component / "sensor.py").read_text(encoding="utf-8")
    timeline = (component / "timeline_sensor.py").read_text(encoding="utf-8")
    forbidden = (
        "services.async_call",
        "write_register",
        "modbus.write",
        "owner_acquire",
        "grant_execution",
        "handover_execution",
        "scheduler_command",
        "executor_command",
    )
    assert not any(token in model for token in forbidden)
    assert "optimize_rcm" not in model
    assert sum(
        isinstance(node, ast.Name) and node.id == "optimize_rcm"
        for node in ast.walk(ast.parse(source))
    ) == 1
    assert source.index(
        "optimize_rcm,", source.index("async_add_executor_job(")
    ) < source.index(
        "build_rcm_timeline_trace("
    )
    assert platform.count("entities.append(rcm_plan)") == 1
    assert platform.count('policy_id="rcm"') == 1
    assert 'source_sensor=rcm_plan' in platform
    assert "_unrecorded_attributes = frozenset({MATCH_ALL})" in timeline


def main() -> None:
    """Run all matrices without external test dependencies."""

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--exhaustive",
        action="store_true",
        help="run the full 2064-scenario pre-release matrix",
    )
    args = parser.parse_args()
    exhaustive = args.exhaustive
    rce_count, rce_statuses, rce_coverage = run_rce_matrix(
        exhaustive=exhaustive,
    )
    tariff_count, tariff_statuses = run_tariff_matrix(exhaustive=exhaustive)
    rcm_count, rcm_statuses = run_rcm_matrix(exhaustive=exhaustive)
    random_count, random_coverage = run_randomized_boundary_sweep(
        samples=120 if exhaustive else 40,
    )
    bms_fail_closed = assert_rce_bms_fail_closed_contracts()
    assert_automation_interlocks()
    assert_manual_cycle_finalization_contracts()
    assert_tariff_startup_contracts()
    assert_tariff_execution_contracts()
    assert_rce_execution_contracts()
    assert_rcm_execution_contracts()
    assert_physical_hardware_readback_contracts()
    assert_human_control_status_contracts()
    assert_rcm_timeline_observation_boundary()
    total = rce_count + tariff_count + rcm_count + random_count
    profile = "exhaustive" if exhaustive else "quick"
    print(f"Automation matrix ({profile}): {total} scenarios passed")
    print(f"  RCE: {rce_count} {dict(sorted(rce_statuses.items()))}")
    print(
        "  RCE nominal evidence by model: "
        + "; ".join(
            f"{model}={dict(sorted(coverage.items()))}"
            for model, coverage in rce_coverage.items()
        )
    )
    print(f"  Tariff: {tariff_count} {dict(sorted(tariff_statuses.items()))}")
    print(f"  RCEm: {rcm_count} {dict(sorted(rcm_statuses.items()))}")
    print(
        f"  Random RCE boundaries: {random_count} "
        f"{dict(sorted(random_coverage.items()))}"
    )
    print(
        "  RCE BMS fail-closed contracts (outside scenario total): "
        f"{sum(bms_fail_closed.values())}/4 "
        f"{dict(sorted(bms_fail_closed.items()))}"
    )
    print("  HA interlocks: RCE / tariff / RCEm / balancing / manual timers present")


if __name__ == "__main__":
    main()
