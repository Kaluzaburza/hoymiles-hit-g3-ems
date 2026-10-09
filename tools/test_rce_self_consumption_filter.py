"""Deterministic R09 tests for the bounded active RCE export filter."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from test_rce_optimizer import RCE, base_input, slots


ROOT = Path(__file__).resolve().parents[1]


class VerifiedSchedule:
    """Small complete price source with the same contract as R07."""

    source_id = "test:verified-import"
    source_revision = "2026-09-13:test"
    quality = "official_verified"
    coverage_complete = True

    def __init__(self, default: float, later: float | None = None) -> None:
        self._default = default
        self._later = later

    def price_at(self, when: datetime) -> float:
        if self._later is not None and when.hour >= 2:
            return self._later
        return self._default


_UNSET = object()


def filtered_case(*, pv_by_slot_kwh=None, schedule=_UNSET, **changes):
    """Return one real input -> legacy plan -> active-filter result."""

    return RCE.optimize_rce(
        base_input(
            price_slots=slots(0, 0, 4, 0.5),
            pv_by_slot_kwh=pv_by_slot_kwh or {},
            average_daily_load_kwh=4.0,
            day3_pv_forecast_kwh=0.0,
            tariff_price_schedule=(
                VerifiedSchedule(1.2) if schedule is _UNSET else schedule
            ),
            self_consumption_filter_enabled=True,
            **changes,
        )
    )


def test_partial_preservation_is_a_subset_of_the_legacy_plan() -> None:
    result = filtered_case()
    legacy = {item.start: item.energy_kwh for item in result.legacy_planned_exports}
    filtered = {item.start: item.energy_kwh for item in result.planned_exports}
    assert result.self_consumption_filter_contract_version == (
        "rce_self_consumption_filter_v1"
    )
    assert result.self_consumption_filter_active is True
    assert result.self_consumption_filter_applied is True
    assert result.self_consumption_filter_reduced is True
    assert 0.0 < result.planned_export_kwh < result.legacy_planned_export_kwh
    assert set(filtered) <= set(legacy)
    assert all(filtered[start] <= legacy[start] + 1e-9 for start in filtered)
    assert result.self_consumption_filter_ui_reason == "sell_surplus"


def test_e01_shadow_variants_match_their_owned_physical_results() -> None:
    result = filtered_case()
    shadow = result.self_consumption_shadow
    assert result.base_reserve_energy_kwh == 4.0
    assert result.protected_home_energy_kwh >= 8.0 - 1e-9
    assert shadow.baseline is not None
    assert shadow.selected is not None
    assert abs(shadow.baseline.grid_import_kwh_ac) <= 1e-9
    assert abs(
        shadow.selected.ending_battery_kwh_dc - result.ending_battery_kwh
    ) <= 1e-9
    assert len(shadow.baseline.slot_grid_import_kwh_ac) == len(
        result.timeline_trace.points
    )
    assert len(shadow.baseline.slot_battery_after_kwh_dc) == len(
        result.timeline_trace.points
    )


def _limited_load_oracle(
    *, initial_kwh: float, sold_kwh: float, load_kwh: float, limit_kw: float
) -> tuple[float, float]:
    """Independent two-slot E02 calculation; no optimizer helper calls."""

    battery_after_sale = initial_kwh - sold_kwh
    delivered = min(load_kwh, battery_after_sale, limit_kw * 0.5)
    return load_kwh - delivered, battery_after_sale - delivered


def test_e02_power_limit_gives_both_variants_the_same_grid_import() -> None:
    settings = base_input(
        battery_capacity_kwh=2.0,
        battery_soc_percent=100.0,
        outage_reserve_soc_percent=0.0,
        manual_minimum_soc_percent=0.0,
        bms_max_discharge_current_a=20.0,
        battery_voltage_v=50.0,
    )
    imports = []
    endings = []
    for sold in (0.5, 0.0):
        first = RCE._simulate_physical_slot(
            settings,
            battery_kwh_dc=2.0,
            load_kwh_ac=0.0,
            pv_kwh_ac=0.0,
            controlled_export_kwh_ac=sold,
            hard_floor_kwh_dc=0.0,
            export_floor_kwh_dc=0.0,
            slot_fraction=1.0,
        )
        second = RCE._simulate_physical_slot(
            settings,
            battery_kwh_dc=first.battery_after_kwh_dc,
            load_kwh_ac=3.0,
            pv_kwh_ac=0.0,
            controlled_export_kwh_ac=0.0,
            hard_floor_kwh_dc=0.0,
            export_floor_kwh_dc=0.0,
            slot_fraction=1.0,
        )
        reference_import, reference_end = _limited_load_oracle(
            initial_kwh=2.0,
            sold_kwh=sold,
            load_kwh=3.0,
            limit_kw=1.0,
        )
        assert first.feasible and second.feasible
        assert abs(second.grid_import_kwh_ac - reference_import) <= 1e-9
        assert abs(second.battery_after_kwh_dc - reference_end) <= 1e-9
        imports.append(second.grid_import_kwh_ac)
        endings.append(second.battery_after_kwh_dc)
    assert imports == [2.5, 2.5]
    assert endings == [1.0, 1.5]


def test_e03_pv_charge_obeys_bms_and_bridge_limits() -> None:
    settings = base_input(
        battery_capacity_kwh=2.0,
        battery_soc_percent=0.0,
        inverter_ac_power_kw=2.0,
        bms_max_charge_current_a=20.0,
        battery_voltage_v=50.0,
    )
    physical = RCE._simulate_physical_slot(
        settings,
        battery_kwh_dc=0.0,
        load_kwh_ac=0.0,
        pv_kwh_ac=4.0,
        controlled_export_kwh_ac=0.0,
        hard_floor_kwh_dc=0.0,
        export_floor_kwh_dc=0.0,
        slot_fraction=1.0,
    )
    assert physical.feasible
    assert abs(physical.charge_input_kwh_ac - 0.5) <= 1e-9
    assert abs(physical.battery_after_kwh_dc - 0.5) <= 1e-9
    assert abs(physical.natural_export_kwh_ac - 0.5) <= 1e-9


def test_e04_house_and_export_share_bms_and_ac_bridge() -> None:
    settings = base_input(
        inverter_ac_power_kw=1.0,
        bms_max_discharge_current_a=20.0,
        battery_voltage_v=50.0,
    )
    feasible = RCE._simulate_physical_slot(
        settings,
        battery_kwh_dc=2.0,
        load_kwh_ac=0.25,
        pv_kwh_ac=0.0,
        controlled_export_kwh_ac=0.25,
        hard_floor_kwh_dc=0.0,
        export_floor_kwh_dc=0.0,
        slot_fraction=1.0,
    )
    over_limit = RCE._simulate_physical_slot(
        settings,
        battery_kwh_dc=2.0,
        load_kwh_ac=0.25,
        pv_kwh_ac=0.0,
        controlled_export_kwh_ac=0.30,
        hard_floor_kwh_dc=0.0,
        export_floor_kwh_dc=0.0,
        slot_fraction=1.0,
    )
    assert feasible.feasible
    assert abs(feasible.delivered_to_load_kwh_ac - 0.25) <= 1e-9
    assert abs(feasible.controlled_export_kwh_ac - 0.25) <= 1e-9
    assert not over_limit.feasible


def test_e05_partial_slot_and_efficiencies_keep_ac_dc_balance() -> None:
    settings = base_input(
        battery_capacity_kwh=2.0,
        battery_soc_percent=100.0,
        inverter_ac_power_kw=2.0,
        bms_max_discharge_current_a=80.0,
        battery_voltage_v=50.0,
        export_efficiency_percent=80.0,
        house_discharge_efficiency_percent=80.0,
    )
    physical = RCE._simulate_physical_slot(
        settings,
        battery_kwh_dc=2.0,
        load_kwh_ac=0.20,
        pv_kwh_ac=0.0,
        controlled_export_kwh_ac=0.20,
        hard_floor_kwh_dc=0.0,
        export_floor_kwh_dc=0.0,
        slot_fraction=0.5,
    )
    assert physical.feasible
    assert abs(physical.battery_after_kwh_dc - 1.5) <= 1e-9
    assert abs(physical.balance_error_kwh) <= 1e-9


def test_missing_bms_capability_is_not_unlimited_power() -> None:
    settings = base_input(
        bms_discharge_data_fresh=False,
        bms_discharge_data_available=False,
    )
    physical = RCE._simulate_physical_slot(
        settings,
        battery_kwh_dc=2.0,
        load_kwh_ac=0.0,
        pv_kwh_ac=0.0,
        controlled_export_kwh_ac=0.1,
        hard_floor_kwh_dc=0.0,
        export_floor_kwh_dc=0.0,
        slot_fraction=1.0,
    )
    assert not physical.feasible


def test_missing_or_unverified_price_blocks_a_new_export() -> None:
    missing = filtered_case(schedule=None)
    unverified = filtered_case(
        schedule=SimpleNamespace(
            source_id="test:estimate",
            source_revision="1",
            quality="estimated",
            coverage_complete=True,
            price_at=lambda _when: 1.2,
        )
    )
    stale = filtered_case(
        schedule=SimpleNamespace(
            source_id="test:stale",
            source_revision="1",
            quality="stale",
            coverage_complete=True,
            price_at=lambda _when: 1.2,
        )
    )
    for result in (missing, unverified, stale):
        assert result.legacy_planned_export_kwh > 0.0
        assert result.planned_export_kwh == 0.0
        assert result.current_slot_start_eligible is False
        assert result.current_slot_execution_power_percent == 0.0
        assert result.self_consumption_filter_status_code == "blocked"
        assert result.self_consumption_filter_ui_reason == "valuation_unavailable"


def test_no_future_need_allows_the_legacy_surplus_sale() -> None:
    result = RCE.optimize_rce(
        base_input(
            price_slots=slots(0, 0, 4, 0.5),
            tariff_price_schedule=VerifiedSchedule(1.2),
            self_consumption_filter_enabled=True,
        )
    )
    assert result.planned_export_kwh == result.legacy_planned_export_kwh
    assert result.self_consumption_filter_reduced is False
    assert result.self_consumption_filter_ui_reason == "sell_surplus"


def test_later_self_use_pv_can_release_more_sale_energy() -> None:
    no_pv = filtered_case()
    # Refill follows the four eligible SELL slots. PV inside a SELL slot
    # cannot charge the battery and does not imply monotonic battery sales.
    pv = {
        no_pv.timeline_trace.points[index].end - RCE.SLOT: 2.0
        for index in range(4, 6)
    }
    with_pv = filtered_case(pv_by_slot_kwh=pv)
    assert with_pv.planned_export_kwh >= no_pv.planned_export_kwh - 1e-9
    assert with_pv.planned_export_kwh <= with_pv.legacy_planned_export_kwh + 1e-9


def test_pv_during_sale_preserves_balance_and_improves_horizon_value() -> None:
    no_pv = filtered_case()
    pv = {no_pv.timeline_trace.points[index].end - RCE.SLOT: 2.0
          for index in range(2, 4)}
    with_pv = filtered_case(pv_by_slot_kwh=pv)
    chosen = with_pv.self_consumption_shadow.selected
    before = no_pv.self_consumption_shadow.selected
    assert chosen.feasible and chosen.balance_error_kwh <= 1e-6
    assert chosen.raw_objective_pln >= before.raw_objective_pln
    assert chosen.grid_import_kwh_ac <= before.grid_import_kwh_ac + 1e-9
    assert with_pv.ending_battery_kwh >= no_pv.ending_battery_kwh - 1e-9
    legacy = {p.start: p.energy_kwh for p in with_pv.legacy_planned_exports}
    assert all(p.energy_kwh <= legacy[p.start] for p in with_pv.planned_exports)
    # Independent energy bound: the 4 kWh PV input may be exported or kept
    # for home use; it must not also appear as battery refill during SELL.
    points = with_pv.timeline_trace.points
    for point in points:
        if point.policy.planned_export_kwh > 0:
            assert point.battery_delta_kwh <= 1e-9


def test_current_4306_target_never_increases_after_filtering() -> None:
    baseline = RCE.optimize_rce(
        base_input(price_slots=slots(0, 0, 4, 0.5))
    )
    filtered = filtered_case()
    assert filtered.current_slot_execution_power_percent <= (
        baseline.current_slot_execution_power_percent
    )
    assert filtered.current_slot_execution_discharge_power_kw <= (
        baseline.current_slot_execution_discharge_power_kw + 1e-9
    )
    assert filtered.current_slot_execution_export_power_kw <= (
        baseline.current_slot_execution_export_power_kw + 1e-9
    )
    assert set(item.start for item in filtered.planned_exports) <= set(
        item.start for item in baseline.planned_exports
    )
    if filtered.current_run_end is not None:
        assert baseline.current_run_end is not None
        assert filtered.current_run_end <= baseline.current_run_end


def test_sub_command_residue_does_not_create_an_execution_cycle() -> None:
    settings = base_input(
        price_slots=slots(0, 0, 4, 0.5),
        self_consumption_filter_enabled=True,
    )
    result = RCE.optimize_rce(replace(settings, self_consumption_filter_enabled=False))
    point_count = len(result.timeline_trace.points)
    result.self_consumption_shadow = SimpleNamespace(
        available=True,
        status_code="preserve_home",
        reason_code="retained_energy_value_exceeds_sale",
        selected=SimpleNamespace(
            export_scale=0.0,
            slot_exports_kwh_ac=tuple(0.009 for _ in range(point_count)),
        ),
    )
    result = RCE._apply_self_consumption_filter(settings, result)
    assert result.legacy_planned_export_kwh > 0.0
    assert result.planned_exports == []
    assert result.current_slot_start_eligible is False
    assert result.current_run_end is None


def test_dst_slot_identity_and_filter_contract_are_utc_stable() -> None:
    warsaw = ZoneInfo("Europe/Warsaw")
    now = datetime(2026, 10, 25, 2, 45, tzinfo=warsaw, fold=1)
    current = RCE.floor_half_hour(now)
    market = [
        RCE.PriceSlot(current + timedelta(minutes=30 * index), 0.5)
        for index in range(4)
    ]
    result = RCE.optimize_rce(
        base_input(
            now=now,
            price_slots=market,
            average_daily_load_kwh=4.0,
            day3_pv_forecast_kwh=0.0,
            tariff_price_schedule=VerifiedSchedule(1.2),
            self_consumption_filter_enabled=True,
        )
    )
    utc = ZoneInfo("UTC")
    legacy = {item.start.astimezone(utc) for item in result.legacy_planned_exports}
    filtered = {item.start.astimezone(utc) for item in result.planned_exports}
    assert filtered <= legacy
    assert len(filtered) == len(result.planned_exports)


def test_filter_never_turns_natural_pv_export_into_forced_export() -> None:
    pv = {slot.start: 3.0 for slot in slots(0, 0, 4, 0.5)}
    result = RCE.optimize_rce(
        base_input(
            price_slots=slots(0, 0, 4, 0.5),
            pv_by_slot_kwh=pv,
            battery_soc_percent=100.0,
            tariff_price_schedule=VerifiedSchedule(1.2),
            self_consumption_filter_enabled=True,
        )
    )
    assert result.planned_export_kwh <= result.legacy_planned_export_kwh + 1e-9
    assert result.natural_export_kwh >= 0.0
    assert result.self_consumption_shadow.baseline is not None
    for variant in result.self_consumption_shadow.variants:
        assert variant.natural_export_kwh_ac >= 0.0
        assert variant.natural_export_revenue_pln == (
            variant.natural_export_kwh_ac * 0.5
        )
        expected = (
            variant.export_revenue_pln
            + variant.natural_export_revenue_pln
            - variant.grid_import_cost_pln
            - variant.battery_wear_cost_pln
            + variant.terminal_value_pln
        )
        assert abs(variant.raw_objective_pln - expected) <= 1e-9
    assert all(
        proposed <= old + 1e-9
        for proposed, old in zip(
            result.self_consumption_shadow.selected.slot_exports_kwh_ac,
            result.self_consumption_shadow.baseline.slot_exports_kwh_ac,
        )
    )


def test_ui_publishes_only_the_three_bounded_reasons() -> None:
    canonical = (
        ROOT / "home_assistant" / "www" / "hoymiles-rce-chart-card.js"
    ).read_text(encoding="utf-8")
    bundled = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "resources"
        / "www"
        / "hoymiles-rce-chart-card.js"
    ).read_text(encoding="utf-8")
    assert canonical == bundled
    for text in (
        "Zachowuję energię na potrzeby domu",
        "Dopuszczam sprzedaż nadwyżki",
        "Brak wiarygodnej wyceny",
    ):
        assert text in canonical
    assert "existingRceChartCard.prototype" in canonical
    assert 'value: "rce_self_consumption_filter_v1"' in canonical
    sensor = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    assert '"self_consumption_filter_ui_reason"' in sensor
    assert "shadow_prediction_not_realized_saving" in (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "rce_self_consumption_shadow.py"
    ).read_text(encoding="utf-8")


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"RCE self-consumption filter: {len(tests)} groups passed")


if __name__ == "__main__":
    main()
