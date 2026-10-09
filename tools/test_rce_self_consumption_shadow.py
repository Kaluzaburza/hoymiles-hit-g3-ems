#!/usr/bin/env python3
"""Deterministic R08 shadow-value regressions; no Home Assistant required."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from time import perf_counter


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(MODULE))

from rce_self_consumption_shadow import (  # noqa: E402
    COST_TOLERANCE_PLN,
    ENERGY_TOLERANCE_KWH,
    LEGACY_SALE_CONTRACT,
    SHADOW_VALUE_CONTRACT,
    ShadowSlot,
    evaluate_sale_vs_preserve,
)


START = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
HALF_HOUR = timedelta(minutes=30)


def slot(
    index: int,
    *,
    load: float = 0.0,
    pv: float = 0.0,
    export: float = 0.0,
    sell: float | None = 0.5,
    buy: float | None = 1.2,
    floor: float = 0.0,
    hard_floor: float | None = None,
) -> ShadowSlot:
    start = START + index * HALF_HOUR
    return ShadowSlot(
        start,
        start + HALF_HOUR,
        load,
        pv,
        export,
        sell,
        buy,
        floor,
        hard_floor,
    )


def evaluate(slots: list[ShadowSlot], *, initial: float, capacity: float, **kwargs):
    kwargs.setdefault("export_efficiency_percent", 95.0)
    kwargs.setdefault("charge_efficiency_percent", 95.0)
    kwargs.setdefault("house_discharge_efficiency_percent", 95.0)
    kwargs.setdefault("battery_wear_cost_pln_kwh_dc", 0.0)
    return evaluate_sale_vs_preserve(
        slots,
        initial_battery_kwh_dc=initial,
        battery_capacity_kwh_dc=capacity,
        uncertainty_fraction=0.0,
        switch_cost_pln=0.0,
        minimum_advantage_pln=0.0,
        scale_steps=20,
        price_source_id="official:test",
        price_source_revision="fixture-v1",
        price_quality="official_verified",
        **kwargs,
    )


def assert_close(actual: float, expected: float, tolerance: float = 1e-9) -> None:
    assert abs(actual - expected) <= tolerance, (actual, expected)


def test_required_fixture() -> None:
    result = evaluate(
        [slot(0, export=0.95), slot(1, load=0.95)],
        initial=1.0,
        capacity=1.0,
    )
    assert result.available and result.status_code == "preserve_home", result
    assert result.baseline is not None and result.selected is not None
    assert_close(result.baseline.export_revenue_pln, 0.475)
    assert_close(result.baseline.grid_import_cost_pln, 1.14)
    assert_close(result.selected.forced_export_kwh_ac, 0.0)
    assert_close(result.selected.avoided_import_value_pln, 1.14)


def test_e01_export_reserve_is_not_the_house_hard_floor() -> None:
    result = evaluate(
        [
            slot(0, export=12.0, floor=8.0, hard_floor=4.0),
            slot(1, load=4.0, floor=8.0, hard_floor=4.0),
        ],
        initial=20.0,
        capacity=20.0,
        export_efficiency_percent=100.0,
        charge_efficiency_percent=100.0,
        house_discharge_efficiency_percent=100.0,
    )
    assert result.baseline is not None
    assert_close(result.baseline.grid_import_kwh_ac, 0.0)
    assert_close(result.baseline.ending_battery_kwh_dc, 4.0)


def test_pv_before_need_reverses_choice() -> None:
    result = evaluate(
        [
            slot(0, export=0.95),
            slot(1, pv=1.0),
            slot(2, load=0.95),
        ],
        initial=2.0,
        capacity=2.0,
    )
    assert result.status_code == "legacy_sale", result
    assert result.selected is result.baseline


def test_partial_need_keeps_surplus_sale() -> None:
    result = evaluate(
        [slot(0, export=1.9), slot(1, load=0.95)],
        initial=2.0,
        capacity=2.0,
    )
    assert result.status_code == "sell_surplus", result
    assert result.selected is not None
    assert_close(result.selected.forced_export_kwh_ac, 0.95)
    assert_close(result.selected.preserved_export_kwh_ac, 0.95)


def test_low_buy_or_no_load_keeps_sale() -> None:
    low_buy = [slot(0, export=0.95, buy=0.2), slot(1, load=0.95, buy=0.2)]
    result = evaluate(low_buy, initial=1.0, capacity=1.0)
    assert result.status_code == "legacy_sale"
    result = evaluate([slot(0, export=0.95)], initial=1.0, capacity=1.0)
    assert result.status_code == "legacy_sale"


def test_missing_price_is_unavailable_not_zero() -> None:
    result = evaluate(
        [slot(0, export=0.95), slot(1, load=0.95, buy=None)],
        initial=1.0,
        capacity=1.0,
    )
    assert not result.available
    assert result.reason_code == "import_price_missing"
    assert result.maximum_justified_export_kwh_ac is None


def test_e06_zero_negative_and_zero_import_prices_are_not_missing() -> None:
    for sell in (0.0, -0.25):
        result = evaluate(
            [slot(0, export=0.95, sell=sell), slot(1, load=0.95, buy=1.2)],
            initial=1.0,
            capacity=1.0,
        )
        assert result.available
        assert result.status_code == "preserve_home", result
        assert result.reason_code == "retained_energy_avoids_higher_import"

    free_import = evaluate(
        [slot(0, export=0.95, sell=0.5), slot(1, load=0.95, buy=0.0)],
        initial=1.0,
        capacity=1.0,
    )
    assert free_import.available
    assert free_import.status_code == "legacy_sale", free_import
    assert free_import.reason_code == "sale_value_exceeds_preservation"


def test_full_battery_and_terminal_bound() -> None:
    result = evaluate(
        [slot(0, export=0.95), slot(1, pv=2.0)],
        initial=2.0,
        capacity=2.0,
        terminal_target_kwh_dc=0.5,
        terminal_value_pln_kwh_dc=0.3,
    )
    assert result.baseline is not None and result.selected is not None
    assert result.baseline.terminal_value_pln <= 0.15 + COST_TOLERANCE_PLN
    assert result.selected.ending_battery_kwh_dc <= 2.0 + ENERGY_TOLERANCE_KWH


def test_e07_objective_components_and_terminal_value_are_not_double_counted() -> None:
    avoided_import = evaluate(
        [slot(0, export=0.95), slot(1, load=0.95)],
        initial=1.0,
        capacity=1.0,
        terminal_target_kwh_dc=1.0,
        terminal_value_pln_kwh_dc=2.0,
    )
    assert avoided_import.selected is not None
    # Energy consumed inside the horizon can avoid import, but it cannot also
    # receive terminal value because none of it remains at the horizon end.
    assert_close(avoided_import.selected.grid_import_cost_pln, 0.0)
    assert_close(avoided_import.selected.terminal_energy_kwh_dc, 0.0)
    assert_close(avoided_import.selected.terminal_value_pln, 0.0)

    bounded_terminal = evaluate(
        [slot(0, export=0.5, sell=0.1, floor=1.0, hard_floor=0.5)],
        initial=2.0,
        capacity=2.0,
        export_efficiency_percent=100.0,
        charge_efficiency_percent=100.0,
        house_discharge_efficiency_percent=100.0,
        battery_wear_cost_pln_kwh_dc=0.2,
        terminal_target_kwh_dc=0.4,
        terminal_value_pln_kwh_dc=3.0,
    )
    assert bounded_terminal.baseline is not None
    for variant in bounded_terminal.variants:
        assert variant.terminal_energy_kwh_dc <= 0.4 + ENERGY_TOLERANCE_KWH
        assert variant.terminal_energy_kwh_dc <= (
            variant.ending_battery_kwh_dc - 0.5 + ENERGY_TOLERANCE_KWH
        )
        expected = (
            variant.export_revenue_pln
            + variant.natural_export_revenue_pln
            - variant.grid_import_cost_pln
            - variant.battery_wear_cost_pln
            + variant.terminal_value_pln
        )
        assert_close(variant.raw_objective_pln, expected)


def test_zero_export_and_horizon_end() -> None:
    zero = evaluate([slot(0)], initial=1.0, capacity=2.0)
    assert zero.available and zero.status_code == "no_existing_export"
    assert zero.maximum_justified_export_kwh_ac == 0.0
    end = evaluate([slot(0, export=0.95)], initial=1.0, capacity=1.0)
    assert end.status_code == "legacy_sale"


def test_all_variants_obey_old_candidate_and_balance() -> None:
    slots = [
        slot(0, export=0.475),
        slot(1, pv=0.3, export=0.475),
        slot(2, load=0.95),
        slot(3, load=0.4, buy=0.6),
    ]
    started = perf_counter()
    result = evaluate(slots, initial=2.0, capacity=2.5)
    elapsed_ms = (perf_counter() - started) * 1000.0
    assert len(result.variants) == 21
    assert elapsed_ms < 100.0, elapsed_ms
    for variant in result.variants:
        assert abs(variant.balance_error_kwh) <= ENERGY_TOLERANCE_KWH
        assert len(variant.slot_exports_kwh_ac) == len(slots)
        assert len(variant.slot_grid_import_kwh_ac) == len(slots)
        assert len(variant.slot_natural_exports_kwh_ac) == len(slots)
        assert len(variant.slot_battery_after_kwh_dc) == len(slots)
        for proposed, old in zip(variant.slot_exports_kwh_ac, slots):
            assert proposed <= old.baseline_export_kwh_ac + ENERGY_TOLERANCE_KWH
            if old.baseline_export_kwh_ac == 0.0:
                assert proposed == 0.0
    attrs = result.as_attributes()
    assert attrs["shadow_contract_version"] == SHADOW_VALUE_CONTRACT
    assert attrs["legacy_sale_contract_version"] == LEGACY_SALE_CONTRACT
    assert attrs["shadow_control_applied"] is False
    assert attrs["shadow_new_export_slots"] == 0
    assert attrs["shadow_new_grid_charge_kwh"] == 0.0


def test_source_has_no_command_surface() -> None:
    source = (MODULE / "rce_self_consumption_shadow.py").read_text(encoding="utf-8")
    for forbidden in (
        "async_register",
        "services.async_call",
        "write_register",
        "force_charge",
        "set_policy_enabled",
    ):
        assert forbidden not in source, forbidden


if __name__ == "__main__":
    tests = [
        test_required_fixture,
        test_e01_export_reserve_is_not_the_house_hard_floor,
        test_pv_before_need_reverses_choice,
        test_partial_need_keeps_surplus_sale,
        test_low_buy_or_no_load_keeps_sale,
        test_missing_price_is_unavailable_not_zero,
        test_e06_zero_negative_and_zero_import_prices_are_not_missing,
        test_full_battery_and_terminal_bound,
        test_e07_objective_components_and_terminal_value_are_not_double_counted,
        test_zero_export_and_horizon_end,
        test_all_variants_obey_old_candidate_and_balance,
        test_source_has_no_command_surface,
    ]
    for test in tests:
        test()
    print(f"RCE self-consumption shadow: {len(tests)} groups passed")
