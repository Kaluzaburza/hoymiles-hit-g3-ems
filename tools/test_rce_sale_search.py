"""Regression for cheap early sales hiding feasible later two-day windows."""
from __future__ import annotations

import asyncio  # Import stdlib select before the integration's select.py.
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))
import rce_optimizer as rce


FIXTURE_PATH = ROOT / "tools" / "fixtures" / "rce_economics_81_point.json"


def two_day_case(load: float, pv_total: float):
    """Synthetic inputs; no installation telemetry or historical snapshot."""
    zone = ZoneInfo("Europe/Warsaw")
    start = datetime(2026, 7, 28, 16, tzinfo=zone)
    prices = [
        .39, .45, .56, .62, .56, .65, .60, .66,
        .61, .55, .54, .48, .47, .45, .44, .35,
    ] + [.3] * 12 + [
        .55, .73, .79, .78, .79, .6, .51, .36, .31, .15, .12, .06,
        .06, .03, .03, .03, .04, .14, .18, .29, .35, .5, .57, .78,
        .76, 1.02, 1, 1.12, 1.08, .93, .83, .63, .65, .67, .68, .69,
    ]
    market = [rce.PriceSlot(start + timedelta(minutes=30*i), price)
              for i, price in enumerate(prices)]
    starts = [(start + timedelta(minutes=30*i)).astimezone(timezone.utc)
              for i in range(81)]
    settings = rce.OptimizerInput(
        now=start + timedelta(minutes=18), price_slots=market,
        pv_by_slot_kwh={}, battery_capacity_kwh=15, battery_soc_percent=94,
        outage_reserve_soc_percent=10, safety_margin_soc_percent=0,
        manual_minimum_soc_percent=72, dynamic_reserve_enabled=False,
        average_daily_load_kwh=load*48, average_night_load_kwh=load*24,
        night_start_minute=18*60, night_end_minute=8*60,
        inverter_power_kw=10, inverter_count=1, discharge_power_percent=60,
        export_efficiency_percent=80, bms_max_discharge_current_a=16,
        bms_max_charge_current_a=16, battery_voltage_v=500,
        bms_discharge_data_fresh=True, bms_discharge_data_available=True,
        bms_charge_data_fresh=True, bms_charge_data_available=True,
    )
    loads = {stamp: load for stamp in starts}
    fractions = dict.fromkeys(starts, 1.0)
    fractions[starts[0]] = .4
    loads[starts[0]] = .33*.2
    weights = {}
    for stamp in starts:
        local = stamp.astimezone(zone)
        hour = local.hour + local.minute / 60
        weights[stamp] = (
            math.sin(math.pi*(hour-7)/12)
            if local.date() == (start+timedelta(days=1)).date() and 7 < hour < 19
            else 0.0
        )
    conservative = {stamp: weight*pv_total/sum(weights.values())
                    for stamp, weight in weights.items()}
    expected = {stamp: energy*2.2 for stamp, energy in conservative.items()}
    settings.pv_by_slot_kwh = expected
    reserves = dict.fromkeys(starts, 10.8)
    floor = 1.5
    price_map = {slot.start: slot.price_pln_kwh for slot in market}

    def objective(plan):
        safe, _, _ = rce._simulate(
            starts, settings, loads, plan, floor, reserves, conservative, fractions
        )
        expected_safe, end, natural = rce._simulate(
            starts, settings, loads, plan, floor, reserves, expected, fractions
        )
        assert safe and expected_safe, "Reference/selected plan must be feasible"
        return rce._economic_objective(
            exports=plan, natural_exports=natural, price_by_start=price_map,
            ending_battery_kwh=end, floor_kwh=floor, export_efficiency=.8,
            battery_wear_cost_pln_kwh=.08, terminal_energy_target_kwh=0,
            terminal_energy_value_pln_kwh=0,
        )[0]

    def solve(candidates):
        return rce._solve_joint_horizon_exports(
            starts=starts, settings=settings, candidates=candidates,
            load_by_slot=loads, floor_kwh=floor, export_reserve_by_slot=reserves,
            conservative_pv=conservative, expected_pv=expected,
            slot_fractions=fractions, price_by_start=price_map,
            baseline_objective=objective({}), export_efficiency=.8,
            terminal_energy_target=0, terminal_unit_value=0,
        )
    return starts, market, objective, solve


def test_later_window_survives_expensive_tomorrow() -> None:
    for load, pv_total, reference_energy in ((.20, 8, 1.9), (.24, 10, 1.7)):
        starts, market, objective, solve = two_day_case(load, pv_total)
        candidates = [(slot, slot.start.astimezone(timezone.utc)) for slot in market]
        plan = solve(candidates)
        # Both early exports must be abandoned together to expose this better
        # independent plan. Tomorrow's expensive rows cannot hide today's 17:30.
        reference = {starts[3]: reference_energy}
        assert objective(plan) >= objective(reference) - .001, (
            objective(plan), objective(reference), plan
        )


def test_candidate_order_and_unavailable_slots() -> None:
    starts, market, objective, solve = two_day_case(.20, 8)
    candidates = [(slot, slot.start.astimezone(timezone.utc)) for slot in market]
    forward = solve(candidates)
    reversed_plan = solve(list(reversed(candidates)))
    assert forward == reversed_plan
    assert objective(forward) >= objective({})
    allowed = [(slot, stamp) for slot, stamp in candidates if stamp != starts[3]]
    restricted = solve(allowed)
    assert starts[3] not in restricted
    assert set(restricted) <= {stamp for _, stamp in allowed}
    assert objective(restricted) >= objective({})


def economics_counterexample_case():
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    zone = ZoneInfo("Europe/Warsaw")
    now_utc = datetime.fromisoformat(fixture["now"].replace("Z", "+00:00"))
    first_end = datetime.fromisoformat(
        fixture["first_end"].replace("Z", "+00:00")
    )
    starts = [
        first_end - timedelta(minutes=30) + timedelta(minutes=30 * index)
        for index in range(len(fixture["points"]))
    ]
    fractions = dict.fromkeys(starts, 1.0)
    fractions[starts[0]] = (first_end - now_utc).total_seconds() / 1800.0
    expected: dict[datetime, float] = {}
    conservative: dict[datetime, float] = {}
    load: dict[datetime, float] = {}
    reserves: dict[datetime, float] = {}
    prices: dict[datetime, float] = {}
    capacity = float(fixture["settings"]["battery_capacity_kwh"])
    for start, row in zip(starts, fixture["points"], strict=True):
        hours = 0.5 * fractions[start]
        expected[start] = float(row["pv_kw"]) * hours
        conservative[start] = float(row["conservative_pv_kw"]) * hours
        load[start] = float(row["load_kw"]) * hours
        reserves[start] = capacity * float(row["protected_soc_percent"]) / 100.0
        if row["price_pln_kwh"] is not None:
            prices[start] = float(row["price_pln_kwh"])

    configured = fixture["settings"]
    settings = rce.OptimizerInput(
        now=now_utc.astimezone(zone),
        price_slots=[],
        pv_by_slot_kwh=expected,
        battery_capacity_kwh=capacity,
        battery_soc_percent=float(configured["battery_soc_percent"]),
        outage_reserve_soc_percent=float(configured["outage_reserve_soc_percent"]),
        safety_margin_soc_percent=float(configured["safety_margin_soc_percent"]),
        manual_minimum_soc_percent=30.0,
        dynamic_reserve_enabled=bool(configured["dynamic_reserve_enabled"]),
        average_daily_load_kwh=40.0,
        average_night_load_kwh=None,
        night_start_minute=17 * 60,
        night_end_minute=7 * 60 + 57,
        inverter_power_kw=float(configured["inverter_power_kw"]),
        inverter_ac_power_kw=float(configured["inverter_ac_power_kw"]),
        inverter_count=int(configured["inverter_count"]),
        discharge_power_percent=float(configured["discharge_power_percent"]),
        export_efficiency_percent=float(configured["export_efficiency_percent"]),
        battery_voltage_v=100.0,
        bms_max_discharge_current_a=8.099 / 0.095,
        bms_max_charge_current_a=8.099 / 0.095,
        bms_discharge_data_fresh=True,
        bms_discharge_data_available=True,
        bms_charge_data_fresh=True,
        bms_charge_data_available=True,
        charge_efficiency_percent=float(configured["charge_efficiency_percent"]),
        house_discharge_efficiency_percent=float(
            configured["house_discharge_efficiency_percent"]
        ),
        battery_wear_cost_pln_kwh=float(
            configured["battery_wear_cost_pln_kwh"]
        ),
    )
    candidates = [
        (rce.PriceSlot(start.astimezone(zone), price), start)
        for start, price in prices.items()
        if not (start.astimezone(zone).hour >= 23 or start.astimezone(zone).hour < 5)
    ]
    settings.price_slots = [slot for slot, _start in candidates]
    floor = float(fixture["floor_kwh"])

    def metrics(plan: dict[datetime, float]) -> dict[str, float | bool]:
        safe, safe_end, _safe_natural = rce._simulate(
            starts,
            settings,
            load,
            plan,
            floor,
            reserves,
            conservative,
            fractions,
        )
        expected_safe, expected_end, natural = rce._simulate(
            starts,
            settings,
            load,
            plan,
            floor,
            reserves,
            expected,
            fractions,
        )
        net, _wear, _terminal = rce._economic_objective(
            exports=plan,
            natural_exports=natural,
            price_by_start=prices,
            ending_battery_kwh=expected_end,
            floor_kwh=floor,
            export_efficiency=0.95,
            battery_wear_cost_pln_kwh=0.08,
            terminal_energy_target_kwh=0.0,
            terminal_energy_value_pln_kwh=0.0,
        )
        safe_trace: list[object] = []
        expected_trace: list[object] = []
        rce._simulate(
            starts,
            settings,
            load,
            plan,
            floor,
            reserves,
            conservative,
            fractions,
            trace_collector=safe_trace,
        )
        rce._simulate(
            starts,
            settings,
            load,
            plan,
            floor,
            reserves,
            expected,
            fractions,
            trace_collector=expected_trace,
        )
        return {
            "net_pln": net,
            "safe": safe,
            "expected_safe": expected_safe,
            "safe_import_kwh": sum(point.grid_import_kwh for point in safe_trace),
            "expected_import_kwh": sum(
                point.grid_import_kwh for point in expected_trace
            ),
            "safe_end_kwh": safe_end,
        }

    def solve(candidate_rows):
        return rce._solve_joint_horizon_exports(
            starts=starts,
            settings=settings,
            candidates=candidate_rows,
            load_by_slot=load,
            floor_kwh=floor,
            export_reserve_by_slot=reserves,
            conservative_pv=conservative,
            expected_pv=expected,
            slot_fractions=fractions,
            price_by_start=prices,
            baseline_objective=float(metrics({})["net_pln"]),
            export_efficiency=0.95,
            terminal_energy_target=0.0,
            terminal_unit_value=0.0,
        )

    reference = {
        datetime.fromisoformat(row["start"]).astimezone(timezone.utc): float(
            row["energy_kwh"]
        )
        for row in fixture["reference_plan"]
    }
    return fixture, starts, candidates, prices, settings, metrics, solve, reference


def test_audit_counterexample_full_candidate_set() -> None:
    fixture, _starts, candidates, prices, settings, metrics, solve, reference = (
        economics_counterexample_case()
    )
    assert Path(rce.__file__).resolve() == (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "rce_optimizer.py"
    ).resolve()
    assert len(candidates) == 51
    expected_reference = float(fixture["expected_reference_net_pln"])
    independent_reference = sum(
        energy * prices[start] - energy / 0.95 * 0.08
        for start, energy in reference.items()
    )
    assert abs(independent_reference - expected_reference) < 1e-9
    reference_metrics = metrics(reference)
    assert reference_metrics["safe"] and reference_metrics["expected_safe"]
    assert reference_metrics["safe_import_kwh"] == 0.0
    assert reference_metrics["expected_import_kwh"] == 0.0

    # The archived continuous reference contains a 0.3845 kW tail, below
    # the existing 0.52 kW start gate (before integer-register rounding).
    # Keep its historical amount/value intact. Construct an independently
    # feasible executable reference by topping up that tail from its earlier
    # adjacent sale, rather than treating the unexecutable command as profit.
    def executable(stamp, energy):
        index = _starts.index(stamp)
        row = fixture['points'][index]
        hours = ((datetime.fromisoformat(fixture['first_end'].replace('Z','+00:00'))
                  - settings.now).total_seconds() / 3600 if index == 0 else .5)
        return rce._rce_export_is_executable(settings, energy, hours,
            row['load_kw']*hours, row['conservative_pv_kw']*hours, new_run=True)
    tails = [stamp for stamp, energy in reference.items() if not executable(stamp, energy)]
    assert len(tails) == 1
    tail = tails[0]
    row = fixture['points'][_starts.index(tail)]
    minimum = rce._minimum_executable_export(settings, .5,
        row['load_kw']*.5, row['conservative_pv_kw']*.5)
    qualified_reference = dict(reference)
    delta = minimum - qualified_reference[tail]
    assert delta > 0 and tail - rce.SLOT in qualified_reference
    qualified_reference[tail] = minimum
    qualified_reference[tail - rce.SLOT] -= delta
    assert all(executable(stamp, energy) for stamp, energy in qualified_reference.items())
    qualified_metrics = metrics(qualified_reference)
    assert qualified_metrics['safe'] and qualified_metrics['expected_safe']
    assert qualified_metrics['safe_import_kwh'] == qualified_metrics['expected_import_kwh'] == 0.
    qualified_value = sum(energy*prices[stamp] - energy/.95*.08
                          for stamp, energy in qualified_reference.items())
    assert abs(qualified_value - qualified_metrics['net_pln']) < 1e-9
    assert qualified_value > 17.81

    selected = solve(candidates)
    selected_metrics = metrics(selected)
    assert selected_metrics["safe"] and selected_metrics["expected_safe"]
    assert selected_metrics["safe_import_kwh"] == 0.0
    assert selected_metrics["expected_import_kwh"] == 0.0
    assert set(selected) <= {start for _slot, start in candidates}
    assert all(executable(stamp, energy) for stamp, energy in selected.items())
    assert selected_metrics["net_pln"] >= qualified_value - 0.001, (
        selected_metrics["net_pln"],
        qualified_value,
        selected,
    )

    reversed_selected = solve(list(reversed(candidates)))
    assert all(executable(stamp, energy) for stamp, energy in reversed_selected.items())
    assert metrics(reversed_selected)["net_pln"] >= qualified_value - 0.001
    assert settings.battery_soc_percent == 85.0


def test_solve_local_cache_preserves_separate_forecasts_and_exact_plan():
    original_simulate = rce._simulate
    original_slot = rce._simulate_physical_slot
    def uncached(*args, **kwargs):
        kwargs.pop('slot_cache', None)
        return original_simulate(*args, **kwargs)
    # Different LOAD and PV inputs must each start with a new cache. These
    # cases also have distinct conservative/expected forecast trajectories.
    for load, pv in ((.20, 8), (.24, 10)):
        _, market, objective, solve = two_day_case(load, pv)
        candidates = [(slot, slot.start.astimezone(timezone.utc)) for slot in market]
        with patch.object(rce, '_simulate_physical_slot', wraps=original_slot) as calls:
            cached = solve(candidates)
            cached_calls = calls.call_count
        with patch.object(rce, '_simulate', side_effect=uncached):
            with patch.object(rce, '_simulate_physical_slot', wraps=original_slot) as calls:
                reference = solve(candidates)
                reference_calls = calls.call_count
        assert cached == reference
        assert objective(cached) == objective(reference)
        assert cached_calls < reference_calls


if __name__ == "__main__":
    test_later_window_survives_expensive_tomorrow()
    test_candidate_order_and_unavailable_slots()
    test_audit_counterexample_full_candidate_set()
    test_solve_local_cache_preserves_separate_forecasts_and_exact_plan()
    print("RCE sale search: 4 groups PASS (audit, two-day, order, constraints, exact cache parity)")
