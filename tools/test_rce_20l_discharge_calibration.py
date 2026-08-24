"""Deterministic contract for the HIT-20L battery-only RCE calibration."""

from __future__ import annotations

import ast
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
INTEGRATION = ROOT / "custom_components" / "hoymiles_hit_modbus"
SENSOR_PATH = INTEGRATION / "rce_sensor.py"
OPTIMIZER_PATH = INTEGRATION / "rce_optimizer.py"
SCHEDULER_PATH = ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"

sys.path.insert(0, str(INTEGRATION))
SPEC = importlib.util.spec_from_file_location("rce_20l_optimizer", OPTIMIZER_PATH)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError("Cannot load the RCE optimizer")
RCE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RCE
SPEC.loader.exec_module(RCE)


def _load_calibration():
    """Load only the pure calibration constant and function, without HA."""

    source = SENSOR_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SENSOR_PATH))
    selected: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name)
            and target.id == "_HIT_20L_BATTERY_DISCHARGE_POWER_KW"
            for target in node.targets
        ):
            selected.append(node)
        elif (
            isinstance(node, ast.FunctionDef)
            and node.name == "_rce_inverter_discharge_power_kw"
        ):
            selected.append(node)
    namespace: dict[str, object] = {}
    exec(compile(ast.Module(selected, []), str(SENSOR_PATH), "exec"), namespace)
    calibration = namespace.get("_rce_inverter_discharge_power_kw")
    if not callable(calibration):
        raise AssertionError("Battery discharge calibration is unavailable")
    return calibration


CALIBRATE = _load_calibration()
NOW = datetime(2026, 8, 24, 18, 0, tzinfo=ZoneInfo("Europe/Warsaw"))


def _result(
    rated_power_kw: float,
    inverter_count: int,
    discharge_percent: float = 100.0,
    *,
    pv_power_kw: float = 0.0,
    load_power_kw: float = 0.0,
):
    effective_power_kw = CALIBRATE(rated_power_kw)
    settings = RCE.OptimizerInput(
        now=NOW,
        price_slots=[],
        pv_by_slot_kwh={},
        battery_capacity_kwh=200.0,
        battery_soc_percent=90.0,
        outage_reserve_soc_percent=10.0,
        safety_margin_soc_percent=0.0,
        manual_minimum_soc_percent=10.0,
        dynamic_reserve_enabled=False,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
        night_start_minute=20 * 60,
        night_end_minute=6 * 60,
        inverter_power_kw=effective_power_kw,
        inverter_ac_power_kw=rated_power_kw,
        inverter_count=inverter_count,
        discharge_power_percent=discharge_percent,
        export_efficiency_percent=100.0,
        bms_max_discharge_current_a=4000.0,
        bms_max_charge_current_a=4000.0,
        battery_voltage_v=50.0,
        bms_power_safety_percent=100.0,
        bms_discharge_data_fresh=True,
        bms_discharge_data_age_seconds=0.0,
        bms_discharge_data_available=True,
        bms_charge_data_fresh=True,
        bms_charge_data_age_seconds=0.0,
        bms_charge_data_available=True,
        current_load_power_kw=load_power_kw,
        current_pv_power_kw=pv_power_kw,
        current_battery_soc_fresh=True,
    )
    result = RCE.optimize_rce(settings)
    assert result.ready
    return settings, result


def test_one_hit20_at_100_percent() -> None:
    _, result = _result(20.0, 1)
    assert result.system_power_kw == 16.0
    assert result.requested_export_power_kw == 16.0


def test_two_hit20_at_100_percent() -> None:
    _, result = _result(20.0, 2)
    assert result.system_power_kw == 32.0
    assert result.requested_export_power_kw == 32.0


def test_two_hit20_at_80_percent() -> None:
    _, result = _result(20.0, 2, 80.0)
    assert result.system_power_kw == 32.0
    assert result.requested_export_power_kw == 25.6


def test_one_15kw_profile_is_unchanged() -> None:
    _, result = _result(15.0, 1)
    assert result.system_power_kw == 15.0
    assert result.requested_export_power_kw == 15.0


def test_two_15kw_profiles_are_unchanged() -> None:
    _, result = _result(15.0, 2)
    assert result.system_power_kw == 30.0
    assert result.requested_export_power_kw == 30.0


def test_all_non_20kw_profiles_are_unchanged() -> None:
    for rated_power_kw in (5.0, 10.0, 12.0, 15.0):
        assert CALIBRATE(rated_power_kw) == rated_power_kw


def test_pv_does_not_change_the_battery_verifier_base() -> None:
    settings, result = _result(20.0, 2, pv_power_kw=36.0)
    assert result.system_power_kw == 32.0
    assert result.requested_export_power_kw == 32.0
    assert RCE._inverter_ac_power_kw(settings) * 2 == 40.0


def test_load_does_not_change_the_battery_verifier_base() -> None:
    settings, result = _result(20.0, 2, load_power_kw=12.0)
    assert result.system_power_kw == 32.0
    assert result.requested_export_power_kw == 32.0
    assert RCE._slot_export_limit_kwh(settings, 6.0, 0.0, 1.0) == 10.0


def test_nameplate_and_effective_diagnostics_are_truthful() -> None:
    source = SENSOR_PATH.read_text(encoding="utf-8")
    assert '"inverter_nameplate_power_each_kw": rated_power' in source
    assert '"inverter_power_each_kw": rce_inverter_power' in source
    assert '"battery_discharge_calibration_applied"' in source
    assert "inverter_power_kw=rce_inverter_power" in source
    assert "inverter_ac_power_kw=rated_power" in source


def test_calibration_adds_no_physical_register_write_or_power_cap() -> None:
    sensor_source = SENSOR_PATH.read_text(encoding="utf-8")
    scheduler_source = SCHEDULER_PATH.read_text(encoding="utf-8")
    helper_start = sensor_source.index("def _rce_inverter_discharge_power_kw")
    helper_end = sensor_source.index("\n\nFORECAST_GCF_ENABLE_ENTITY", helper_start)
    helper_source = sensor_source[helper_start:helper_end]
    assert "430" not in helper_source
    assert "service" not in helper_source
    assert "_HIT_20L_BATTERY_DISCHARGE_POWER_KW" not in scheduler_source
    assert "script.hoymiles_verified_set_ems_maximum_discharge_power" in (
        scheduler_source
    )
    assert 'value: "{{ rce_start_power | float(0) | round(1) }}"' in (
        scheduler_source
    )


def main() -> None:
    tests = [
        test_one_hit20_at_100_percent,
        test_two_hit20_at_100_percent,
        test_two_hit20_at_80_percent,
        test_one_15kw_profile_is_unchanged,
        test_two_15kw_profiles_are_unchanged,
        test_all_non_20kw_profiles_are_unchanged,
        test_pv_does_not_change_the_battery_verifier_base,
        test_load_does_not_change_the_battery_verifier_base,
        test_nameplate_and_effective_diagnostics_are_truthful,
        test_calibration_adds_no_physical_register_write_or_power_cap,
    ]
    for test in tests:
        test()
    print(f"RCE HIT-20L discharge calibration: {len(tests)}/10 PASS")


if __name__ == "__main__":
    main()
