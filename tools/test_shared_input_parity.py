"""Offline parity matrix for the Shared EMS consumer boundary.

This test deliberately imports only the pure optimizers.  Home Assistant
adapter wiring is checked through AST/source contracts, while identical
legacy/shared fixtures are passed through the same optimizer dataclasses and
their complete deterministic results are compared.
"""

from __future__ import annotations

import ast
from dataclasses import asdict
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
FIXTURE = ROOT / "tools" / "fixtures" / "shared_input_parity_v1.json"
sys.path.insert(0, str(COMPONENT))

import rce_optimizer as rce  # noqa: E402
import rcm_optimizer as rcm  # noqa: E402
import tariff_optimizer as tariff  # noqa: E402


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))
SCENARIOS = (
    "baseline",
    "stale_solcast",
    "missing_day3",
    "stale_bms",
    "missing_count",
    "capacity_total_fallback",
    "missing_capacity",
    "zero_export_incoherent",
    "stale_load",
    "grid_to_battery_unknown",
)


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _fresh(age_seconds: float | None, maximum: float) -> bool:
    return age_seconds is not None and -5.0 <= age_seconds <= maximum


def _adapt(fixture: dict[str, Any], scenario: str, *, shared: bool) -> dict[str, Any]:
    """Normalize legacy and broker representations at the optimizer boundary."""

    old = fixture["legacy"]
    new = fixture["shared"]
    ages = {
        "forecast": 60.0,
        "bms": 20.0,
        "system": 20.0,
        "load": 60.0,
        "live": 5.0,
    }
    if scenario == "stale_solcast":
        ages["forecast"] = 64_801.0
    elif scenario == "stale_bms":
        ages["bms"] = 301.0
    elif scenario == "stale_load":
        ages["load"] = 301.0

    if shared:
        system = new["system"]
        bms = new["bms"]
        power = new["power"]
        forecast = new["forecast"]
        gcf = new["gcf"]
        load = new["load"]
        capacity = system["battery_capacity_kwh"]
        total_capacity = system["total_capacity_kwh"]
        count = system["inverter_count"]
        day3 = forecast["day3_kwh"]
        average_profile = tuple(load["average_profile_30m_kwh"])
        weekday_profile = tuple(load["weekday_profile_30m_kwh"])
        weekend_profile = tuple(load["weekend_profile_30m_kwh"])
        values = {
            "battery_soc": system["battery_soc_percent"],
            "reserve_soc": system["self_use_reserve_soc_percent"],
            "rated_each": system["inverter_rated_power_each_kw"],
            "voltage": bms["voltage_v"],
            "charge_current": bms["maximum_charge_current_a"],
            "discharge_current": bms["maximum_discharge_current_a"],
            "pv_kw": power["pv_power_kw"],
            "load_kw": power["home_load_power_kw"],
            "battery_kw": power["battery_power_kw"],
            "grid_export_kw": power["grid_power_kw"],
            "forecast_today": forecast["today_kwh"],
            "forecast_tomorrow": forecast["tomorrow_kwh"],
            "average_daily_load": load["average_daily_home_load_kwh"],
            "average_night_load": load["average_night_home_load_kwh"],
            "load_history_days": load["daily_history_days"],
            "load_source": load["source"],
            "fallback_used": load["fallback_currently_used"],
            "weekday_profile_days": load["weekday_profile_days"],
            "weekend_profile_days": load["weekend_profile_days"],
            "gcf_enable": gcf["enable_code"],
            "gcf_limit": gcf["maximum_export_power_percent"],
            "gcf_ready": gcf["gcf_readback_ready"],
            "grid_to_battery_ready": power["grid_to_battery_ready"],
        }
    else:
        capacity = old["battery_capacity_kwh"]
        total_capacity = old["total_capacity_kwh"]
        count = old["inverter_count"]
        day3 = old["forecast_day3_kwh"]
        average_profile = (old["load_average_profile_slot_kwh"],) * 48
        weekday_profile = (old["load_weekday_profile_slot_kwh"],) * 48
        weekend_profile = (old["load_weekend_profile_slot_kwh"],) * 48
        values = {
            "battery_soc": old["battery_soc_percent"],
            "reserve_soc": old["self_use_reserve_soc_percent"],
            "rated_each": old["inverter_rated_power_each_kw"],
            "voltage": old["battery_voltage_v"],
            "charge_current": old["maximum_charge_current_a"],
            "discharge_current": old["maximum_discharge_current_a"],
            "pv_kw": old["pv_power_w"] / 1000.0,
            "load_kw": old["home_load_power_w"] / 1000.0,
            "battery_kw": old["battery_power_w"] / 1000.0,
            "grid_export_kw": old["grid_export_power_kw"],
            "forecast_today": old["forecast_today_kwh"],
            "forecast_tomorrow": old["forecast_tomorrow_kwh"],
            "average_daily_load": old["average_daily_home_load_kwh"],
            "average_night_load": old["average_night_home_load_kwh"],
            "load_history_days": old["load_history_days"],
            "load_source": old["load_model_source"],
            "fallback_used": old["fallback_currently_used"],
            "weekday_profile_days": old["load_weekday_profile_days"],
            "weekend_profile_days": old["load_weekend_profile_days"],
            "gcf_enable": old["gcf_enable_code"],
            "gcf_limit": old["gcf_export_limit_percent"],
            "gcf_ready": True,
            "grid_to_battery_ready": False,
        }

    if scenario == "missing_day3":
        day3 = None
    if scenario == "missing_count":
        count = None
    if scenario == "capacity_total_fallback":
        capacity = None
    if scenario == "missing_capacity":
        capacity = total_capacity = None
    if scenario == "zero_export_incoherent":
        values["gcf_ready"] = False

    values.update(
        {
            # RCEm alone retains this ordered policy fallback.
            "capacity": capacity if capacity is not None else total_capacity,
            "inverter_count": count,
            "day3": day3,
            "average_profile": average_profile,
            "weekday_profile": weekday_profile,
            "weekend_profile": weekend_profile,
            "forecast_rce_fresh": _fresh(ages["forecast"], 64_800.0),
            "forecast_rcm_fresh": _fresh(ages["forecast"], 43_200.0),
            "bms_fresh": _fresh(ages["bms"], 300.0),
            "system_fresh": _fresh(ages["system"], 300.0),
            "load_fresh": _fresh(ages["load"], 300.0),
            "live_rce_tariff_fresh": _fresh(ages["live"], 120.0),
            "live_rcm_fresh": _fresh(ages["live"], 90.0),
        }
    )
    return values


def _rce_result(values: dict[str, Any]) -> Any:
    slots = [
        rce.PriceSlot(NOW + timedelta(minutes=30 * index), price)
        for index, price in enumerate((0.35, 0.45, 1.20, 1.10, 0.50, 0.40))
    ]
    pv = (
        {slot.start: 0.6 for slot in slots}
        if values["forecast_rce_fresh"]
        else {}
    )
    bms_fresh = values["bms_fresh"]
    settings = rce.OptimizerInput(
        now=NOW,
        price_slots=slots,
        pv_by_slot_kwh=pv,
        battery_capacity_kwh=values["capacity"] or 0.0,
        battery_soc_percent=values["battery_soc"],
        outage_reserve_soc_percent=values["reserve_soc"],
        safety_margin_soc_percent=2.0,
        manual_minimum_soc_percent=20.0,
        dynamic_reserve_enabled=True,
        average_daily_load_kwh=values["average_daily_load"],
        average_night_load_kwh=values["average_night_load"],
        night_start_minute=20 * 60,
        night_end_minute=7 * 60,
        inverter_power_kw=values["rated_each"],
        inverter_count=values["inverter_count"] or 0,
        discharge_power_percent=80.0,
        export_efficiency_percent=88.0,
        bms_max_discharge_current_a=values["discharge_current"] if bms_fresh else None,
        bms_max_charge_current_a=values["charge_current"] if bms_fresh else None,
        battery_voltage_v=values["voltage"] if bms_fresh else None,
        bms_discharge_data_fresh=bms_fresh,
        bms_discharge_data_age_seconds=20.0 if bms_fresh else 301.0,
        bms_discharge_data_available=bms_fresh,
        bms_charge_data_fresh=bms_fresh,
        bms_charge_data_age_seconds=20.0 if bms_fresh else 301.0,
        bms_charge_data_available=bms_fresh,
        actual_day_load_today_kwh=7.0,
        pv_to_load_power_kw=values["pv_kw"],
        load_profile_30m_kwh=values["average_profile"] if values["load_fresh"] else (),
        weekday_load_profile_30m_kwh=values["weekday_profile"] if values["load_fresh"] else (),
        weekend_load_profile_30m_kwh=values["weekend_profile"] if values["load_fresh"] else (),
        day3_pv_forecast_kwh=values["day3"],
        charge_efficiency_percent=94.0,
        house_discharge_efficiency_percent=91.0,
        current_load_power_kw=values["load_kw"] if values["live_rce_tariff_fresh"] else None,
        current_pv_power_kw=values["pv_kw"] if values["live_rce_tariff_fresh"] else None,
    )
    return rce.optimize_rce(settings)


def _tariff_result(values: dict[str, Any]) -> Any:
    schedule = tariff.TariffSchedule(
        tariff_type="G12",
        g11_price_pln_kwh=0.85,
        low_price_pln_kwh=0.55,
        medium_price_pln_kwh=0.80,
        peak_price_pln_kwh=1.05,
        cheap_windows=((22 * 60, 6 * 60), (13 * 60, 15 * 60)),
        medium_windows=((7 * 60, 13 * 60),),
        weekend_low_price=False,
        polish_holidays_low_price=True,
    )
    control_fresh = bool(
        values["bms_fresh"]
        and values["load_fresh"]
        and values["capacity"] is not None
        and values["inverter_count"] is not None
    )
    return tariff.optimize_tariff_charging(
        tariff.TariffOptimizerInput(
            now=NOW,
            pv_by_slot_kwh={},
            battery_capacity_kwh=values["capacity"] or 0.0,
            battery_soc_percent=values["battery_soc"],
            reserve_soc_percent=values["reserve_soc"] + 2.0,
            maximum_soc_percent=95.0,
            average_daily_load_kwh=values["average_daily_load"],
            average_night_load_kwh=values["average_night_load"],
            night_start_minute=20 * 60,
            night_end_minute=7 * 60,
            charge_power_kw=5.0,
            system_ac_power_kw=(
                values["rated_each"] * values["inverter_count"]
                if values["rated_each"] is not None
                and values["inverter_count"] is not None
                else 0.0
            ),
            # AC/grid -> battery remains distinct from PV -> battery.
            charge_efficiency_percent=93.0,
            discharge_efficiency_percent=91.0,
            minimum_saving_pln_kwh=0.05,
            schedule=schedule,
            load_by_slot_kwh={
                NOW + timedelta(minutes=30 * index): value
                for index, value in enumerate(values["average_profile"])
            } if values["load_fresh"] else None,
            battery_charge_power_kw=(
                values["voltage"] * values["charge_current"] / 1000.0
                if values["bms_fresh"] else 0.0
            ),
            battery_discharge_power_kw=(
                values["voltage"] * values["discharge_current"] / 1000.0
                if values["bms_fresh"] else 0.0
            ),
            current_load_power_kw=values["load_kw"],
            current_pv_power_kw=values["pv_kw"],
            current_battery_power_kw=values["battery_kw"],
            control_inputs_fresh=control_fresh,
            control_input_block_reason="none" if control_fresh else "shared_input_unavailable",
        )
    )


def _rcm_result(values: dict[str, Any]) -> Any:
    bms_fresh = values["bms_fresh"]
    system_valid = values["inverter_count"] is not None
    return rcm.optimize_rcm(
        rcm.RCMOptimizerInput(
            now=NOW,
            voltage_l1_v=249.0,
            voltage_l2_v=250.0,
            voltage_l3_v=251.0,
            filtered_voltage_v=250.0,
            rolling_10m_voltage_v=249.5,
            historical_p90_voltage_v=252.0,
            risk_windows=((13 * 60, 15 * 60, 254.0),),
            history_days=4,
            pv_power_kw=values["pv_kw"],
            load_power_kw=values["load_kw"],
            grid_export_power_kw=max(values["grid_export_kw"], 0.0),
            battery_capacity_kwh=values["capacity"] or 0.0,
            battery_soc_percent=values["battery_soc"],
            reserve_soc_percent=values["reserve_soc"],
            safety_margin_soc_percent=2.0,
            protected_minimum_soc_percent=22.0,
            expected_risk_surplus_kwh=7.0,
            expected_natural_headroom_kwh=0.5,
            minutes_to_risk=60,
            risk_day_offset=0,
            system_power_kw=(values["rated_each"] * (values["inverter_count"] or 0)) or 1.0,
            battery_voltage_v=values["voltage"] if bms_fresh else None,
            bms_max_charge_current_a=values["charge_current"] if bms_fresh else None,
            bms_max_discharge_current_a=values["discharge_current"] if bms_fresh else None,
            current_charge_limit_percent=80.0,
            saved_charge_limit_percent=100.0,
            export_control_enabled=True,
            current_export_limit_percent=values["gcf_limit"],
            saved_export_limit_percent=values["gcf_limit"],
            user_export_cap_percent=60.0,
            gcf_active=values["gcf_enable"] == 1.0,
            gcf_data_fresh=values["gcf_ready"],
            charge_efficiency_percent=95.0,
            voltage_data_fresh=True,
            actuator_data_fresh=True,
            history_data_fresh=True,
            forecast_data_fresh=values["forecast_rcm_fresh"],
            load_profile_data_fresh=values["load_fresh"],
            live_power_data_fresh=values["live_rcm_fresh"],
            bms_charge_data_fresh=bms_fresh,
            bms_discharge_data_fresh=bms_fresh,
            pre_discharge_actuator_data_fresh=(
                bms_fresh and values["capacity"] is not None
            ),
            system_power_data_valid=system_valid,
        )
    )


def _canonical_projection(results: dict[str, Any]) -> dict[str, Any]:
    """Project the policy outputs consumed by canonical/start gates."""

    rce_result = results["rce"]
    tariff_result = results["tariff"]
    rcm_result = results["rcm"]
    return {
        "rce_status": rce_result.status_code,
        "rce_ready": rce_result.ready,
        "rce_actions": asdict(rce_result).get("planned_exports", []),
        "tariff_status": tariff_result.status_code,
        "tariff_action": tariff_result.current_action,
        "tariff_current_slot_planned": tariff_result.current_slot_planned,
        "tariff_actions": asdict(tariff_result)["planned_charges"],
        "rcm_status": rcm_result.status_code,
        "rcm_action": rcm_result.action,
        "rcm_blocker": rcm_result.prediction_block_reason,
        "rcm_start_eligible": rcm_result.pre_discharge_start_eligible,
        "rcm_continue_eligible": rcm_result.pre_discharge_continue_eligible,
        "rcm_transaction_ready": rcm_result.pre_discharge_transaction_ready,
    }


def test_fixture_and_optimizer_parity() -> int:
    fixture = _load_fixture()
    assert fixture["schema_version"] == 1
    assert fixture["shared"]["load"]["daily_history_days"] == 9
    assert fixture["shared"]["load"]["fallback_currently_used"] is False
    assert fixture["shared"]["load"]["source"] == "rce_recorder_broker"
    assert fixture["shared"]["power"]["grid_to_battery_ready"] is False

    comparisons = 0
    for scenario in SCENARIOS:
        before = _adapt(fixture, scenario, shared=False)
        after = _adapt(fixture, scenario, shared=True)
        assert before == after, f"SHARED_INPUT_PARITY_FAILURE: adapter:{scenario}"
        before_results: dict[str, Any] = {}
        after_results: dict[str, Any] = {}
        calculation_errors: dict[str, tuple[type[Exception], str]] = {}
        for policy, calculate in (
            ("rce", _rce_result),
            ("tariff", _tariff_result),
            ("rcm", _rcm_result),
        ):
            try:
                before_result = calculate(before)
            except ValueError as before_error:
                try:
                    calculate(after)
                except ValueError as after_error:
                    assert type(before_error) is type(after_error)
                    assert str(before_error) == str(after_error), (
                        f"SHARED_INPUT_PARITY_FAILURE: error:{policy}:{scenario}"
                    )
                    calculation_errors[policy] = (
                        type(before_error),
                        str(before_error),
                    )
                    comparisons += 1
                    continue
                raise AssertionError(
                    f"SHARED_INPUT_PARITY_FAILURE: one-sided-error:{policy}:{scenario}"
                ) from before_error
            try:
                after_result = calculate(after)
            except ValueError as after_error:
                raise AssertionError(
                    f"SHARED_INPUT_PARITY_FAILURE: one-sided-error:{policy}:{scenario}"
                ) from after_error
            before_results[policy] = before_result
            after_results[policy] = after_result
            assert asdict(before_result) == asdict(after_result), (
                f"SHARED_INPUT_PARITY_FAILURE: {policy}:{scenario}"
            )
            comparisons += 1
        if calculation_errors:
            assert scenario == "missing_capacity"
            assert calculation_errors == {
                "tariff": (ValueError, "battery capacity must be finite and positive")
            }
        else:
            assert _canonical_projection(before_results) == _canonical_projection(
                after_results
            ), f"SHARED_INPUT_PARITY_FAILURE: canonical:{scenario}"
            comparisons += 1
        rcm_result = after_results["rcm"]
        assert hasattr(rcm_result, "pre_discharge_start_eligible")
        assert hasattr(rcm_result, "pre_discharge_continue_eligible")
    return comparisons


def _class_fields(tree: ast.Module, class_name: str) -> set[str]:
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.ClassDef) and item.name == class_name
    )
    return {
        item.target.id
        for item in node.body
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name)
    }


def test_schema_freshness_migration_and_wiring_contracts() -> None:
    shared_path = COMPONENT / "ems_shared_inputs.py"
    shared_source = shared_path.read_text(encoding="utf-8")
    shared_tree = ast.parse(shared_source, filename=str(shared_path))
    assert {
        "value", "entity_id", "reported_at", "age_seconds", "fresh",
        "reason", "quality", "provenance",
    } <= _class_fields(shared_tree, "SharedSample")
    assert {"schema_version", "config_entry_id", "captured_at", "migration"} <= (
        _class_fields(shared_tree, "EMSSharedInputsSnapshot")
    )
    assert {"pv_to_battery_efficiency", "battery_to_home_efficiency"} == (
        _class_fields(shared_tree, "SharedEfficiencyInputs")
    )
    assert "P1_PROVIDERLESS_GRID_TO_BATTERY_DEPENDENCY" in shared_source
    assert "grid_to_battery_ready=False" in shared_source
    assert "attach_load_model_provider" in shared_source

    migration_source = (COMPONENT / "ems_shared_input_migration.py").read_text(
        encoding="utf-8"
    )
    assert "MIGRATION_VERSION" in migration_source
    assert "copy_once" in migration_source
    assert "legacy" in migration_source

    rce_source = (COMPONENT / "rce_sensor.py").read_text(encoding="utf-8")
    tariff_source = (COMPONENT / "tariff_sensor.py").read_text(encoding="utf-8")
    rcm_source = (COMPONENT / "rcm_sensor.py").read_text(encoding="utf-8")
    sensor_source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    assert "def shared_load_model_snapshot(" in rce_source
    assert '"source": "recorder_phase_counters_and_actual_load"' in rce_source
    assert '"fallback_currently_used"' in rce_source
    assert "expected_load_by_slot(" in rce_source
    assert "expected_load_by_slot(" in tariff_source
    assert rce_source.index(
        'night_load = load_model_values["average_night_load"]'
    ) < rce_source.index(
        ") = current_day_profile_correction("
    )
    tariff_input_start = tariff_source.index("    def _optimizer_input(")
    assert tariff_source.index(
        "        night_start = (", tariff_input_start
    ) < tariff_source.index(
        "            load_forecast = expected_load_by_slot(", tariff_input_start
    )
    assert {
        "daily_total_dates",
        "profile_history_days",
        "current_day_observed_at",
        "persistence_delta_kw",
        "model_quality",
    } <= _class_fields(shared_tree, "SharedLoadModelSnapshot")
    assert "shared_inputs.attach_load_model_provider" in rce_source
    assert "rcm_plan.attach_rce_plan_source(rce_plan)" in sensor_source
    assert "def _same_entry_rce_plan_state(" in rcm_source
    assert not re.search(
        r'hass\.states\.get\(\s*["\']sensor\.hoymiles_hit_rce_optimized_plan',
        rcm_source,
    )
    gate = tariff_source.index("grid_to_battery_ready")
    feedback_append = tariff_source.index("self._delivered_power_ratios.append", gate)
    assert gate < feedback_append
    assert "_policy_numeric_sample(" in rce_source
    assert "_policy_numeric_sample(" in tariff_source
    assert "_policy_numeric_sample(" in rcm_source

    # The four flows remain distinct: only the two explicitly shared
    # efficiencies are broker fields. Grid charging and battery export retain
    # their policy helpers and equations.
    assert '"pv_to_battery_efficiency"' in rce_source
    assert '"battery_to_home_efficiency"' in rce_source
    assert '"input_number.hoymiles_tariff_charge_efficiency"' in tariff_source
    assert '"input_number.hoymiles_rce_export_efficiency"' in rce_source
    assert '"input_number.hoymiles_rcm_charge_efficiency"' in rcm_source


def main() -> None:
    comparisons = test_fixture_and_optimizer_parity()
    test_schema_freshness_migration_and_wiring_contracts()
    print(
        "Shared EMS parity: PASS "
        f"({comparisons} optimizer/canonical comparisons; "
        f"{len(SCENARIOS)} freshness/capacity/GCF scenarios)"
    )


if __name__ == "__main__":
    main()
