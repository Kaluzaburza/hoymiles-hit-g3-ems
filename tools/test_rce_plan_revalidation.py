"""Fixed RCE schedule publication uses fresh physics without another search."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from time import perf_counter
import math

from test_rce_optimizer import RCE, NOW, base_input, slots
from test_rce_self_consumption_filter import VerifiedSchedule


def seed(*, count=4, filtered=False):
    now = NOW.replace(hour=6 if count == 100 else 19, minute=2)
    start = RCE.floor_half_hour(now)
    settings = base_input(
        now=now,
        # This fixture exercises an active current schedule, including on the
        # long horizon. A strict first-slot premium prevents the separate
        # equal-price consolidation policy from legitimately deferring it.
        price_slots=[RCE.PriceSlot(start + RCE.SLOT * i,
            2.01 if count == 100 and i == 0 else 2.0) for i in range(count)],
        battery_capacity_kwh=100.0,
        battery_soc_percent=90.0,
        discharge_power_percent=50.0,
        current_load_power_kw=0.6,
        current_pv_power_kw=0.0,
        tariff_price_schedule=VerifiedSchedule(0.2) if filtered else None,
        self_consumption_filter_enabled=filtered,
        battery_wear_cost_pln_kwh=0.08,
    )
    result = RCE.optimize_rce(settings)
    assert result.ready and result.current_slot_start_eligible
    assert result.planned_exports
    return settings, result


def assert_subset(result, captured):
    original = {item.start: item.energy_kwh for item in captured.planned_exports}
    assert all(item.start in original and 0 <= item.energy_kwh <= original[item.start] + 1e-9 for item in result.planned_exports)
    assert result.planned_export_kwh <= captured.planned_export_kwh + 1e-9
    if result.current_run_end is not None:
        assert result.current_run_end <= captured.current_run_end


def test_full_horizon_tail_economics():
    # Real public path: only slot 0 sells; identical daily LOAD is covered by
    # PV on days 1/2, while slot 99 consumes 1.5 kWh without PV on day 3.
    # A 48 h diagnostic trace misses that consumption and overvalues energy
    # retained by the selling candidate (4.75 vs 4.50 PLN). The full horizon
    # values selling around 0.25 PLN vs 1.50 PLN for preserving the battery.
    now = NOW.replace(hour=6, minute=0)
    starts = [now + RCE.SLOT * i for i in range(100)]
    profile = tuple(1.5 if i == 15 else 0.0 for i in range(48))
    pv = {starts[3]: 1.5, starts[51]: 1.5}
    settings = base_input(
        now=now,
        price_slots=[RCE.PriceSlot(t, 0.5, i != 0) for i, t in enumerate(starts)],
        pv_by_slot_kwh=pv, conservative_pv_by_slot_kwh=pv,
        battery_capacity_kwh=2.0, battery_soc_percent=100.0,
        manual_minimum_soc_percent=0.0, outage_reserve_soc_percent=0.0,
        dynamic_reserve_enabled=False,
        average_daily_load_kwh=1.5, load_profile_30m_kwh=profile,
        current_load_power_kw=0.0, current_pv_power_kw=0.0,
        tariff_price_schedule=VerifiedSchedule(3.0),
        self_consumption_filter_enabled=True, battery_wear_cost_pln_kwh=0.0,
        day3_pv_forecast_kwh=0.0, avoided_import_price_pln_kwh=3.0,
    )
    captured = RCE.optimize_rce(settings)
    assert captured.ready and captured.current_slot_start_eligible
    assert captured.planned_export_kwh > 0.45
    assert len(captured.timeline_trace.points) == 96
    assert RCE.revalidate_rce_plan(settings, captured, captured_settings=settings) is None, (
        "final 4 slots must participate in the fresh economic gate"
    )


def test_inactive_current_results():
    settings, _ = seed()
    cases = {
        "zero_bms": replace(settings, bms_max_discharge_current_a=0.0, current_load_power_kw=0.0),
        "known_zero_bms_unavailable": replace(settings, bms_max_discharge_current_a=0.0,
            bms_discharge_data_available=False, current_load_power_kw=0.0),
        "zero_export": replace(settings, export_power_cap_kw=0.0),
        "empty_market": replace(settings, price_slots=[]),
    }
    for label, captured_settings in cases.items():
        captured = RCE.optimize_rce(captured_settings)
        assert captured.ready and not captured.planned_exports, label
        latest = replace(captured_settings, now=captured_settings.now + timedelta(seconds=4),
            bms_discharge_data_age_seconds=4.0, bms_charge_data_age_seconds=4.0)
        result = RCE.revalidate_rce_plan(latest, captured, captured_settings=captured_settings)
        assert result is not None, label
        assert not result.planned_exports and not result.current_slot_start_eligible
        assert result.current_slot_execution_discharge_power_kw == 0.0
        assert result.current_slot_execution_export_power_kw == 0.0
        assert result.current_slot_execution_power_percent == 0.0
        assert result.current_slot_planned_export_kwh == 0.0
        assert result.current_run_end is None
        assert result.status_code == captured.status_code
        if label == "known_zero_bms_unavailable":
            for invalid in (
                {"bms_max_discharge_current_a": None},
                {"bms_max_discharge_current_a": float("nan")},
                {"bms_max_discharge_current_a": 1.0},
                {"bms_discharge_data_fresh": False},
                {"bms_discharge_data_age_seconds": 301.0},
                {"bms_discharge_data_available": None},
            ):
                assert RCE.revalidate_rce_plan(replace(latest, **invalid), captured,
                    captured_settings=captured_settings) is None, invalid
        moving = RCE.revalidate_rce_plan(
            replace(latest, current_load_power_kw=latest.current_load_power_kw + 0.1,
                current_pv_power_kw=0.2),
            captured, captured_settings=captured_settings,
        )
        assert moving is not None, "idle plan must absorb fresh live telemetry"
        assert not moving.planned_exports and not moving.current_slot_start_eligible
        assert moving.current_slot_execution_power_percent == 0.0
        assert moving.current_slot_execution_discharge_power_kw == 0.0
        assert moving.current_run_end is None
        assert RCE.revalidate_rce_plan(
            replace(latest, current_battery_soc_fresh=False),
            captured, captured_settings=captured_settings,
        ) is None, "inactive path must not accept changed safety quality"


def test_constant_power_time_steps():
    settings, captured = seed()
    for requested in (16, 26, 33, 50, 99, 100):
        fixed_settings = replace(settings, discharge_power_percent=requested)
        fixed_result = RCE.optimize_rce(fixed_settings)
        mismatches = []
        for seconds in range(121):
            latest = replace(fixed_settings, now=settings.now + timedelta(seconds=seconds))
            result = RCE.revalidate_rce_plan(latest, fixed_result, captured_settings=fixed_settings)
            assert result is not None
            if result.current_slot_execution_power_percent != fixed_result.current_slot_execution_power_percent:
                mismatches.append((seconds, result.current_slot_execution_power_percent))
            assert_subset(result, fixed_result)
        assert not mismatches, f"time-only revalidation changed 4306 ({requested}): {mismatches}"
    for seconds in (1, 8, 13, 24, 120):
        for bms_cap in (math.nextafter(5.0, 0.0), 4.95, 4.9, 0.486):
            latest = replace(settings, now=settings.now + timedelta(seconds=seconds),
                bms_max_discharge_current_a=bms_cap * 1000.0 / settings.battery_voltage_v)
            result = RCE.revalidate_rce_plan(latest, captured, captured_settings=settings)
            actual_cap = latest.bms_max_discharge_current_a * latest.battery_voltage_v / 1000.0
            assert result is None or result.current_slot_execution_discharge_power_kw <= actual_cap
        for export_cap in (math.nextafter(4.4, 0.0), 4.399, 3.9, 0.01):
            latest = replace(settings, now=settings.now + timedelta(seconds=seconds),
                export_power_cap_kw=export_cap)
            result = RCE.revalidate_rce_plan(latest, captured, captured_settings=settings)
            assert result is None or result.current_slot_execution_export_power_kw <= export_cap


def test_optional_power_fallback():
    settings, captured = seed()
    for changes in (
        {"current_load_power_kw": None}, {"current_pv_power_kw": None},
        {"current_load_power_kw": None, "current_pv_power_kw": None},
    ):
        fallback = replace(settings, **changes)
        result = RCE.optimize_rce(fallback)
        assert result.ready and result.planned_exports and not result.current_slot_start_eligible
        refreshed = RCE.revalidate_rce_plan(
            replace(fallback, now=fallback.now + timedelta(seconds=4)), result,
            captured_settings=fallback,
        )
        assert refreshed is not None, "unchanged optional-power fallback was rejected"
        assert not refreshed.current_slot_start_eligible
        assert_subset(refreshed, result)
        assert RCE.revalidate_rce_plan(replace(settings, **changes), captured,
            captured_settings=settings) is None, "valid live power becoming missing was accepted"


def main():
    assert hasattr(RCE, "revalidate_rce_plan"), "missing fresh fixed-schedule publication contract"
    revalidate = RCE.revalidate_rce_plan
    test_constant_power_time_steps()
    test_optional_power_fallback()
    test_full_horizon_tail_economics()
    test_inactive_current_results()
    settings, captured = seed()
    charge_zero = replace(settings, bms_max_charge_current_a=0.0)
    charge_zero_captured = RCE.optimize_rce(charge_zero)
    charge_zero_fresh = revalidate(charge_zero, charge_zero_captured, captured_settings=charge_zero)
    assert charge_zero_fresh is not None and charge_zero_fresh.current_slot_start_eligible
    overflow_settings, overflow_captured = seed(count=1)
    settings_before, result_before = deepcopy(settings), deepcopy(captured)
    latest = replace(settings, now=settings.now + timedelta(seconds=4), current_load_power_kw=1.1, current_pv_power_kw=0.2)
    original_search, original_shadow = RCE._solve_joint_horizon_exports, RCE.evaluate_sale_vs_preserve

    def forbidden(*args, **kwargs):
        raise AssertionError("publication must not run optimizer/shadow search")

    RCE._solve_joint_horizon_exports = forbidden
    RCE.evaluate_sale_vs_preserve = forbidden
    try:
        refreshed = revalidate(latest, captured, captured_settings=settings)
        assert refreshed is not None and refreshed.current_slot_start_eligible
        assert_subset(refreshed, captured)
        assert refreshed.current_slot_load_source == "shared_forecast"
        assert math.isclose(refreshed.current_slot_load_kwh,
            captured.current_slot_load_kwh * refreshed.current_slot_remaining_minutes
            / captured.current_slot_remaining_minutes, abs_tol=1e-8)
        assert refreshed.solver_method == "fixed_schedule_revalidation"
        assert refreshed.optimality_verified is False
        assert refreshed.self_consumption_shadow.available is False
        assert settings == settings_before and captured == result_before
        refreshed.planned_exports.clear()
        assert captured.planned_exports == result_before.planned_exports

        unchanged = revalidate(replace(settings, now=settings.now + timedelta(seconds=10)), captured, captured_settings=settings)
        assert unchanged is not None
        expected_ratio = (RCE.SLOT.total_seconds() - 130) / (RCE.SLOT.total_seconds() - 120)
        assert unchanged.current_slot_planned_export_kwh <= captured.current_slot_planned_export_kwh * expected_ratio + 1e-8
        assert_subset(unchanged, captured)

        rejected = {
            "backwards": replace(settings, now=settings.now - timedelta(seconds=1)),
            "slot_boundary": replace(settings, now=RCE.floor_half_hour(settings.now) + RCE.SLOT),
            "soc_floor": replace(settings, battery_soc_percent=19.0),
            "soc_stale": replace(settings, current_battery_soc_fresh=False),
            "bms_stale": replace(settings, bms_discharge_data_fresh=False),
            "bms_expired": replace(settings, bms_discharge_data_age_seconds=301),
            "bms_future": replace(settings, bms_discharge_data_age_seconds=-6),
            "bms_missing": replace(settings, bms_discharge_data_available=False),
            "bms_zero": replace(settings, bms_max_discharge_current_a=0),
            "bms_nan": replace(settings, battery_voltage_v=float("nan")),
            "charge_bms_nan": replace(settings, bms_max_charge_current_a=float("nan")),
            "charge_bms_negative": replace(settings, bms_max_charge_current_a=-1.0),
            "soc_nan": replace(settings, battery_soc_percent=float("nan")),
            "load_unknown": replace(settings, current_load_power_kw=None),
            "pv_invalid": replace(settings, current_pv_power_kw=float("inf")),
            "load_counter_nan": replace(settings, actual_day_load_today_kwh=float("nan")),
            "persistence_nan": replace(settings, persistence_delta_kw=float("nan")),
            "zero_export": replace(settings, export_power_cap_kw=0),
            "reserve_change": replace(settings, outage_reserve_soc_percent=80),
            "topology_change": replace(settings, inverter_count=2),
            "wear_change": replace(settings, battery_wear_cost_pln_kwh=3),
            "market_change": replace(settings, price_slots=[replace(settings.price_slots[0], price_pln_kwh=0.01)]),
        }
        for label, value in rejected.items():
            assert revalidate(value, captured, captured_settings=settings) is None, label
        # A newer BMS limit never inherits the old 5 kW command.
        lower_bms = replace(settings, bms_max_discharge_current_a=20)
        limited = revalidate(lower_bms, captured, captured_settings=settings)
        assert limited is None or limited.current_slot_execution_discharge_power_kw <= 1.0 + 1e-9
        # PV overflow can make a former battery sale lose money versus fresh self-use.
        overflow = revalidate(replace(overflow_settings, current_pv_power_kw=30.0, battery_soc_percent=100.0), overflow_captured, captured_settings=overflow_settings)
        assert overflow is None, "fixed export displaced free natural PV export"
    finally:
        RCE._solve_joint_horizon_exports, RCE.evaluate_sale_vs_preserve = original_search, original_shadow

    # Import prices are contract inputs, not live telemetry: changed pricing
    # requires another solver result even if the old trade stays feasible.
    filtered_settings, filtered = seed(filtered=True)
    assert revalidate(
        replace(filtered_settings, tariff_price_schedule=VerifiedSchedule(5.0)),
        filtered, captured_settings=filtered_settings,
    ) is None

    # A lower fresh SOC can keep the captured trade physically feasible while
    # making it worse than preserving energy valued at the verified import price.
    economic_settings = base_input(
        price_slots=slots(0, 0, 4, 0.5), average_daily_load_kwh=4.0,
        day3_pv_forecast_kwh=0.0, tariff_price_schedule=VerifiedSchedule(1.2),
        self_consumption_filter_enabled=True,
        current_load_power_kw=0.0, current_pv_power_kw=0.0,
    )
    economic_captured = RCE.optimize_rce(economic_settings)
    assert economic_captured.planned_exports
    variants = []
    original_variant = RCE._shadow_variant_from_shared_physics

    def record_variant(*args, **kwargs):
        variant = original_variant(*args, **kwargs)
        variants.append(variant)
        return variant

    RCE._shadow_variant_from_shared_physics = record_variant
    RCE._solve_joint_horizon_exports = forbidden
    RCE.evaluate_sale_vs_preserve = forbidden
    try:
        assert revalidate(
            replace(economic_settings, battery_soc_percent=80.0),
            economic_captured, captured_settings=economic_settings,
        ) is None
        assert len(variants) == 2 and all(item.feasible for item in variants)
        sale, preserve = variants
        assert sale.raw_objective_pln < preserve.raw_objective_pln
        assert sale.battery_wear_cost_pln > preserve.battery_wear_cost_pln
        assert sale.terminal_value_pln < preserve.terminal_value_pln
        # Both observed costs are included in the economic comparison; this
        # case has zero immediate imports and a worse retained-energy value.
        for item in variants:
            assert abs(item.raw_objective_pln - (
                item.export_revenue_pln + item.natural_export_revenue_pln
                - item.grid_import_cost_pln - item.battery_wear_cost_pln
                + item.terminal_value_pln
            )) < 1e-9
    finally:
        RCE._shadow_variant_from_shared_physics = original_variant
        RCE._solve_joint_horizon_exports, RCE.evaluate_sale_vs_preserve = original_search, original_shadow

    # Independent two-slot import-cost oracle for the final economic gate:
    # initial 2 kWh, sell 0.5 kWh for 0.25 PLN, then consume 2 kWh.
    # Selling causes 0.5 kWh of imports at 3 PLN/kWh: net -1.25 PLN;
    # preserving the battery has no imports and net 0 PLN.
    import_settings = replace(
        economic_settings, battery_capacity_kwh=2.0,
        battery_soc_percent=100.0, battery_wear_cost_pln_kwh=0.0,
        tariff_price_schedule=VerifiedSchedule(3.0),
    )
    import_candidate = deepcopy(economic_captured)
    first, second = import_candidate.timeline_trace.points[:2]
    import_candidate.timeline_trace = replace(
        import_candidate.timeline_trace,
        points=(
            replace(first, load_kwh=0.0, pv_kwh=0.0,
                protected_soc_floor_percent=0.0,
                policy=replace(first.policy, planned_export_kwh=0.5)),
            replace(second, load_kwh=2.0, pv_kwh=0.0,
                protected_soc_floor_percent=0.0,
                policy=replace(second.policy, planned_export_kwh=0.0)),
        ),
    )
    import_candidate.base_reserve_energy_kwh = 0.0
    import_candidate.terminal_energy_target_kwh = 0.0
    import_candidate.net_objective_pln = 0.25
    import_candidate.baseline_net_objective_pln = 0.0
    variants.clear()
    RCE._shadow_variant_from_shared_physics = record_variant
    try:
        assert not RCE._fixed_schedule_is_economic(import_settings, import_candidate)
        sale, preserve = variants
        assert sale.feasible and preserve.feasible
        assert abs(sale.grid_import_cost_pln - 1.5) < 1e-9
        assert abs(sale.raw_objective_pln - (-1.25)) < 1e-9
        assert preserve.grid_import_cost_pln == preserve.raw_objective_pln == 0.0
    finally:
        RCE._shadow_variant_from_shared_physics = original_variant

    RCE._solve_joint_horizon_exports = forbidden
    RCE.evaluate_sale_vs_preserve = forbidden
    try:
        fresh_filtered = revalidate(replace(filtered_settings, now=filtered_settings.now + timedelta(seconds=4), current_load_power_kw=0.7), filtered, captured_settings=filtered_settings)
        assert fresh_filtered is not None
        assert_subset(fresh_filtered, filtered)
        assert fresh_filtered.self_consumption_filter_active
        assert fresh_filtered.self_consumption_filter_reason_code == "accepted_selection_revalidated"
    finally:
        RCE._solve_joint_horizon_exports, RCE.evaluate_sale_vs_preserve = original_search, original_shadow

    # Full supported market-day horizon, with the common fixed physics path timed.
    long_settings, long_result = seed(count=100, filtered=True)
    physical_end = RCE._horizon_end(replace(long_settings, price_slots=RCE._supported_price_slots(long_settings)))
    assert (physical_end - RCE.floor_half_hour(long_settings.now)) / RCE.SLOT == 100
    times = []
    RCE._solve_joint_horizon_exports = forbidden
    RCE.evaluate_sale_vs_preserve = forbidden
    try:
        for _ in range(10):
            started = perf_counter()
            value = revalidate(long_settings, long_result, captured_settings=long_settings)
            times.append((perf_counter() - started) * 1000)
            assert value is not None
            assert len(value.timeline_trace.points) == 96, "private full trace leaked publicly"
        assert max(times) < 200.0, times
    finally:
        RCE._solve_joint_horizon_exports, RCE.evaluate_sale_vs_preserve = original_search, original_shadow
    print(f"PASS fixed-plan revalidation: {len(rejected)} safety negatives, changing telemetry, time/cap subset, isolation, filter/import provenance/economic baseline, full-horizon tail veto, idle churn/optional fallback, stable time-only integer command, no searches; 100 physical slots (trace capped at 96) median_ms={sorted(times)[len(times)//2]:.3f} max_ms={max(times):.3f}")


if __name__ == "__main__":
    main()
