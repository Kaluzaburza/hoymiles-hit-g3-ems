"""Regression tests for the fast RCEm 253 V publication path.

The test executes the actual sensor callback body with small Home Assistant
fakes and the real pure optimizer.  It does not import Home Assistant or touch
an installation.
"""

from __future__ import annotations

import ast
import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from math import floor, isfinite
from pathlib import Path
from statistics import median
import sys
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))

import rcm_optimizer  # noqa: E402
from rcm_optimizer import RCMOptimizerInput  # noqa: E402


GRID_VOLTAGE_ENTITIES = (
    "sensor.hoymiles_hit_grid_voltage_l1",
    "sensor.hoymiles_hit_grid_voltage_l2",
    "sensor.hoymiles_hit_grid_voltage_l3",
)
LIVE_TELEMETRY_MAX_AGE_SECONDS = 90.0
SLOW_TELEMETRY_MAX_AGE_SECONDS = 300.0
ACTUATOR_MAX_AGE_SECONDS = 300.0
EMS_INVERTER_RATED_POWER_HELPER = "input_select.hoymiles_ems_inverter_power"
LEGACY_INVERTER_RATED_POWER_HELPER = "input_select.hoymiles_rce_inverter_power"
_SHARED_INPUT_MISSING = object()
NOW = datetime(2026, 9, 4, 12, 0, tzinfo=ZoneInfo("Europe/Warsaw"))


def _sensor_tree() -> ast.Module:
    path = COMPONENT / "rcm_sensor.py"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _sensor_method(name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    sensor = next(
        node
        for node in _sensor_tree().body
        if isinstance(node, ast.ClassDef)
        and node.name == "HoymilesRCMOptimizerSensor"
    )
    matches = [
        node
        for node in sensor.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    assert len(matches) == 1
    return matches[0]


def _compile_live_probe(
    samples: dict[str, tuple[float | None, bool]],
    *,
    rated_power_kw: float | None = 10.0,
    capacity_kwh: float | None = 21.0,
) -> type[Any]:
    method = deepcopy(_sensor_method("_async_live_control_refresh"))
    method.decorator_list = []

    def number_sample(
        _hass: Any,
        entity_id: str,
        **_kwargs: Any,
    ) -> tuple[float | None, bool, float | None]:
        value, fresh = samples.get(entity_id, (None, False))
        return (value if fresh else None), fresh, 0.0 if fresh else 999.0

    def policy_sample(
        _runtime: Any,
        _section: str,
        _field: str,
        _legacy_state: Any,
        _now: datetime,
        **_kwargs: Any,
    ) -> Any:
        value, fresh = samples.get(
            "sensor.hoymiles_hit_gcf_enable_readback_code",
            (None, False),
        )
        return SimpleNamespace(
            value=value if fresh else None,
            fresh=fresh,
            age_seconds=0.0 if fresh else 999.0,
        )

    namespace: dict[str, Any] = {
        "Any": Any,
        "datetime": datetime,
        "replace": replace,
        "ZoneInfo": ZoneInfo,
        "timezone": timezone,
        "timedelta": timedelta,
        "median": median,
        "floor": floor,
        "isfinite": isfinite,
        "GRID_VOLTAGE_ENTITIES": GRID_VOLTAGE_ENTITIES,
        "LIVE_TELEMETRY_MAX_AGE_SECONDS": LIVE_TELEMETRY_MAX_AGE_SECONDS,
        "SLOW_TELEMETRY_MAX_AGE_SECONDS": SLOW_TELEMETRY_MAX_AGE_SECONDS,
        "ACTUATOR_MAX_AGE_SECONDS": ACTUATOR_MAX_AGE_SECONDS,
        "RCM_CERTIFIED_CACHE_MAX_AGE_SECONDS": 630.0,
        "EMS_INVERTER_RATED_POWER_HELPER": EMS_INVERTER_RATED_POWER_HELPER,
        "LEGACY_INVERTER_RATED_POWER_HELPER": (
            LEGACY_INVERTER_RATED_POWER_HELPER
        ),
        "_SHARED_INPUT_MISSING": _SHARED_INPUT_MISSING,
        "_number_sample": number_sample,
        "_policy_numeric_sample": policy_sample,
        "_shared_inputs_snapshot": lambda _runtime: None,
        "_shared_sample_value": (
            lambda _runtime, _section, _field: _SHARED_INPUT_MISSING
        ),
        "_preferred_select_number": (
            lambda _hass, _new, _legacy: rated_power_kw
        ),
        "_stable_battery_capacity": (
            lambda _hass, _runtime: (
                capacity_kwh,
                "sensor.hoymiles_hit_battery_capacity"
                if capacity_kwh is not None
                else "",
            )
        ),
        "_state_number": lambda hass, entity_id: hass.states.number(entity_id),
        "_minutes_text": (
            lambda minutes: None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"
        ),
        "_rcm_live_control_fingerprint": (
            lambda _hass, _runtime, _samples: ("stable",)
        ),
        "rcm_optimizer_module": rcm_optimizer,
    }
    probe_class = ast.ClassDef(
        name="Probe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[method],
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[probe_class], type_ignores=[])
    )
    exec(compile(module, "<rcm-live-control-probe>", "exec"), namespace)
    return namespace["Probe"]


class States:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, entity_id: str) -> Any:
        value = self.values.get(entity_id)
        return None if value is None else SimpleNamespace(state=value)

    def is_state(self, entity_id: str, expected: str) -> bool:
        return self.values.get(entity_id) == expected

    def number(self, entity_id: str) -> float | None:
        value = self.values.get(entity_id)
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None


class Hass:
    def __init__(self, states: States) -> None:
        self.states = states
        self.config = SimpleNamespace(time_zone="Europe/Warsaw")
        self.executor_calls: list[str] = []

    async def async_add_executor_job(self, function: Any, argument: Any) -> Any:
        self.executor_calls.append(function.__name__)
        return function(argument)


def _base_optimizer_input() -> RCMOptimizerInput:
    return RCMOptimizerInput(
        now=NOW,
        voltage_l1_v=240.0,
        voltage_l2_v=240.0,
        voltage_l3_v=240.0,
        filtered_voltage_v=240.0,
        rolling_10m_voltage_v=240.0,
        historical_p90_voltage_v=0.0,
        risk_windows=(),
        history_days=0,
        pv_power_kw=8.0,
        load_power_kw=2.0,
        grid_export_power_kw=5.0,
        battery_capacity_kwh=21.0,
        battery_soc_percent=70.0,
        reserve_soc_percent=20.0,
        safety_margin_soc_percent=2.0,
        protected_minimum_soc_percent=22.0,
        expected_risk_surplus_kwh=0.0,
        expected_natural_headroom_kwh=0.0,
        minutes_to_risk=None,
        risk_day_offset=-1,
        system_power_kw=10.0,
        battery_voltage_v=52.0,
        bms_max_charge_current_a=175.0,
        bms_max_discharge_current_a=175.0,
        current_charge_limit_percent=80.0,
        saved_charge_limit_percent=80.0,
        export_control_enabled=True,
        current_export_limit_percent=50.0,
        saved_export_limit_percent=50.0,
        user_export_cap_percent=60.0,
        charge_efficiency_percent=95.0,
        house_discharge_efficiency_percent=95.0,
    )


def _base_samples() -> dict[str, tuple[float | None, bool]]:
    return {
        GRID_VOLTAGE_ENTITIES[0]: (240.0, True),
        GRID_VOLTAGE_ENTITIES[1]: (240.0, True),
        GRID_VOLTAGE_ENTITIES[2]: (240.0, True),
        "sensor.hoymiles_hit_battery_max_charge_power_readback": (80.0, True),
        "sensor.hoymiles_hit_gcf_enable_readback_code": (1.0, True),
        "sensor.hoymiles_hit_gcf_maximum_export_power_readback": (50.0, True),
        "sensor.hoymiles_hit_number_of_machines_master_and_slave": (1.0, True),
        "sensor.hoymiles_hit_battery_voltage_bms": (52.0, True),
        "sensor.hoymiles_hit_maximum_charge_current": (175.0, True),
        "sensor.hoymiles_hit_maximum_discharge_current": (175.0, True),
        "sensor.hoymiles_hit_overview_battery_soc": (70.0, True),
        "sensor.hoymiles_hit_ems_self_use_soc_readback": (20.0, True),
        "sensor.hoymiles_hit_overview_pv_total_power": (8000.0, True),
        "sensor.hoymiles_actual_load_power": (2000.0, True),
        "sensor.hoymiles_rce_grid_export_power": (5.0, True),
        "sensor.hoymiles_hit_ems_maximum_discharge_power_readback": (50.0, True),
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback": (20.0, True),
        "sensor.hoymiles_hit_ems_mode_readback_code": (0.0, True),
    }


def _new_probe(
    samples: dict[str, tuple[float | None, bool]],
    *,
    rated_power_kw: float | None = 10.0,
) -> Any:
    values = {
        "input_boolean.hoymiles_rcm_export_control_enabled": "on",
        "input_boolean.hoymiles_rcm_export_control_active": "off",
        "input_boolean.hoymiles_rcm_pre_discharge_active": "off",
        "input_number.hoymiles_rcm_saved_battery_charge_power": "80",
        "input_number.hoymiles_rcm_export_cap_percent": "60",
        "input_number.hoymiles_rcm_saved_export_limit": "50",
    }
    probe = _compile_live_probe(
        samples,
        rated_power_kw=rated_power_kw,
    )()
    probe.hass = Hass(States(values))
    probe._runtime = object()
    probe._optimizer_lock = asyncio.Lock()
    probe._attributes = {
        "action": "hold",
        "recommended_charge_limit_percent": 80.0,
        "recommended_export_limit_percent": 50.0,
        "data_freshness": {},
        "data_age_seconds": {},
        "result_current": True,
        "recalculation_pending": False,
    }
    probe._samples = deque()
    probe._last_full_optimizer_input = _base_optimizer_input()
    probe._last_full_optimizer_result = rcm_optimizer.optimize_rcm(
        probe._last_full_optimizer_input
    )
    probe._last_full_plan_at = NOW
    probe._live_emergency_active = False
    probe._live_control_ticks = 0
    probe._full_plan_solver_calls = 7
    probe.timeline_refresh_calls = []
    probe.timeline_publish_calls = 0
    probe._refresh_live_observation_timeline = lambda **kwargs: (
        probe.timeline_refresh_calls.append(kwargs) or True
    )
    probe._publish_timeline_result = lambda: setattr(
        probe,
        "timeline_publish_calls",
        probe.timeline_publish_calls + 1,
    )
    probe.write_calls = 0
    probe.async_write_ha_state = lambda: setattr(
        probe,
        "write_calls",
        probe.write_calls + 1,
    )
    return probe


async def _test_live_decisions() -> None:
    samples = _base_samples()
    samples[GRID_VOLTAGE_ENTITIES[0]] = (253.0, True)
    samples[GRID_VOLTAGE_ENTITIES[1]] = (None, False)
    samples[GRID_VOLTAGE_ENTITIES[2]] = (None, False)
    probe = _new_probe(samples)
    frozen = probe._last_full_optimizer_input

    assert await probe._async_live_control_refresh(NOW)
    attrs = probe._attributes
    assert attrs["live_emergency"] is True
    assert attrs["emergency_voltage_data_fresh"] is True
    assert attrs["voltage_data_fresh"] is False
    assert attrs["action"] == "absorb_pv"
    assert attrs["emergency_action_ready"] is True
    assert attrs["recommended_charge_limit_percent"] == 91.0
    assert attrs["recommended_export_limit_percent"] == 0.0
    assert attrs["pre_discharge_start_eligible"] is False
    assert attrs["pre_discharge_continue_eligible"] is False
    assert attrs["live_control_transition"] == "entered_emergency"
    assert probe._full_plan_solver_calls == 7
    assert probe._last_full_optimizer_input is frozen
    assert len(probe.timeline_refresh_calls) == 1
    assert probe.timeline_refresh_calls[0]["result"].action == "absorb_pv"
    assert (
        probe.timeline_refresh_calls[0]["result"].action_interval_start
        == NOW
    )
    assert (
        probe.timeline_refresh_calls[0]["result"].action_interval_end
        == NOW + timedelta(seconds=60)
    )
    assert probe.timeline_publish_calls == 1

    # Losing every phase immediately withdraws the live authority, action and
    # emergency targets instead of retaining the previous recommendation.
    for entity_id in GRID_VOLTAGE_ENTITIES:
        samples[entity_id] = (None, False)
    assert await probe._async_live_control_refresh(NOW + timedelta(seconds=15))
    attrs = probe._attributes
    assert attrs["live_emergency"] is False
    assert attrs["emergency_action_ready"] is False
    assert attrs["status_code"] == "stale_voltage"
    assert attrs["action"] == "hold"
    assert attrs["recommended_charge_limit_percent"] == 80.0
    assert attrs["recommended_export_limit_percent"] == 50.0
    assert attrs["maximum_voltage_v"] is None
    assert attrs["pre_discharge_start_eligible"] is False
    assert attrs["pre_discharge_continue_eligible"] is False
    assert attrs["live_control_transition"] == "left_emergency"
    assert probe._full_plan_solver_calls == 7
    assert probe.hass.executor_calls == []

    # 252.99 V is not the live threshold; exactly 253.0 V is.
    exact_samples = _base_samples()
    exact_probe = _new_probe(exact_samples)
    exact_samples[GRID_VOLTAGE_ENTITIES[0]] = (252.99, True)
    await exact_probe._async_live_control_refresh(NOW)
    assert exact_probe._attributes["live_emergency"] is False
    assert exact_probe._attributes["emergency_action_ready"] is False
    exact_samples[GRID_VOLTAGE_ENTITIES[0]] = (253.0, True)
    await exact_probe._async_live_control_refresh(NOW + timedelta(seconds=15))
    assert exact_probe._attributes["live_emergency"] is True


async def _test_bms_and_export_gates() -> None:
    for bms_sample in ((0.0, True), (None, False)):
        samples = _base_samples()
        samples[GRID_VOLTAGE_ENTITIES[0]] = (253.2, True)
        samples["sensor.hoymiles_hit_maximum_charge_current"] = bms_sample
        probe = _new_probe(samples)
        await probe._async_live_control_refresh(NOW)
        attrs = probe._attributes
        assert attrs["live_emergency"] is True
        assert attrs["bms_charge_available"] is False
        assert attrs["action"] == "limit_export"
        assert attrs["emergency_action_ready"] is True
        assert attrs["recommended_charge_limit_percent"] == 80.0
        assert attrs["recommended_export_limit_percent"] == 0.0

    # Missing topology blocks charge/BMS conversion, but it cannot erase the
    # independently verified GCF export clamp path.
    samples = _base_samples()
    samples[GRID_VOLTAGE_ENTITIES[2]] = (253.4, True)
    samples["sensor.hoymiles_hit_number_of_machines_master_and_slave"] = (
        None,
        False,
    )
    probe = _new_probe(samples, rated_power_kw=None)
    await probe._async_live_control_refresh(NOW)
    assert probe._attributes["system_power_data_valid"] is False
    assert probe._attributes["action"] == "limit_export"
    assert probe._attributes["emergency_action_ready"] is True
    assert probe._attributes["recommended_export_limit_percent"] == 0.0

    # With both physical paths unavailable the high-voltage fact remains
    # visible, but no command authority or stale emergency target survives.
    probe.hass.states.values[
        "input_boolean.hoymiles_rcm_export_control_enabled"
    ] = "off"
    await probe._async_live_control_refresh(NOW + timedelta(seconds=15))
    assert probe._attributes["live_emergency"] is True
    assert probe._attributes["action"] == "monitor"
    assert probe._attributes["emergency_action_ready"] is False
    assert probe._attributes["recommended_export_limit_percent"] == 50.0

    # Charge remains independently available when GCF/export provenance is
    # missing; no export command is invented from that missing path.
    samples = _base_samples()
    samples[GRID_VOLTAGE_ENTITIES[1]] = (253.1, True)
    samples["sensor.hoymiles_hit_gcf_enable_readback_code"] = (None, False)
    charge_only = _new_probe(samples)
    await charge_only._async_live_control_refresh(NOW)
    assert charge_only._attributes["action"] == "absorb_pv"
    assert charge_only._attributes["emergency_action_ready"] is True
    assert charge_only._attributes["recommended_charge_limit_percent"] == 91.0
    assert charge_only._attributes["recommended_export_limit_percent"] == 50.0


async def _test_predictive_voltage_validity() -> None:
    base = replace(
        _base_optimizer_input(),
        battery_soc_percent=100.0,
        pv_power_kw=0.0,
        load_power_kw=2.0,
        historical_p90_voltage_v=250.0,
        history_days=4,
        expected_risk_surplus_kwh=8.0,
        expected_natural_headroom_kwh=0.0,
        minutes_to_risk=90,
        risk_day_offset=0,
        risk_windows=((810, 855, 254.0),),
    )

    valid_samples = _base_samples()
    valid_samples["sensor.hoymiles_hit_overview_battery_soc"] = (100.0, True)
    valid_samples["sensor.hoymiles_hit_overview_pv_total_power"] = (0.0, True)
    valid = _new_probe(valid_samples)
    valid._last_full_optimizer_input = base
    valid._last_full_optimizer_result = rcm_optimizer.optimize_rcm(base)
    await valid._async_live_control_refresh(NOW)
    assert valid._attributes["voltage_data_fresh"] is True
    assert valid._attributes["pre_discharge_start_eligible"] is True

    invalid_sets = (
        (0.0, 0.0, 0.0),
        (0.0, 240.0, 240.0),
        (-1.0, 240.0, 240.0),
        (float("nan"), 240.0, 240.0),
        (float("inf"), 240.0, 240.0),
    )
    for values in invalid_sets:
        samples = dict(valid_samples)
        for entity_id, value in zip(GRID_VOLTAGE_ENTITIES, values, strict=True):
            samples[entity_id] = (value, True)
        probe = _new_probe(samples)
        probe._last_full_optimizer_input = base
        probe._last_full_optimizer_result = rcm_optimizer.optimize_rcm(base)
        await probe._async_live_control_refresh(NOW)
        assert probe._attributes["voltage_data_fresh"] is False
        assert probe._attributes["pre_discharge_start_eligible"] is False
        assert probe._attributes["pre_discharge_continue_eligible"] is False

    stale = dict(valid_samples)
    stale[GRID_VOLTAGE_ENTITIES[1]] = (240.0, False)
    stale_probe = _new_probe(stale)
    stale_probe._last_full_optimizer_input = base
    stale_probe._last_full_optimizer_result = rcm_optimizer.optimize_rcm(base)
    await stale_probe._async_live_control_refresh(NOW)
    assert stale_probe._attributes["voltage_data_fresh"] is False
    assert stale_probe._attributes["pre_discharge_start_eligible"] is False

    high = dict(valid_samples)
    high[GRID_VOLTAGE_ENTITIES[0]] = (253.2, True)
    high[GRID_VOLTAGE_ENTITIES[1]] = (None, False)
    high[GRID_VOLTAGE_ENTITIES[2]] = (0.0, True)
    high_probe = _new_probe(high)
    high_probe._last_full_optimizer_input = base
    high_probe._last_full_optimizer_result = rcm_optimizer.optimize_rcm(base)
    await high_probe._async_live_control_refresh(NOW)
    assert high_probe._attributes["voltage_data_fresh"] is False
    assert high_probe._attributes["emergency_voltage_data_fresh"] is True
    assert high_probe._attributes["live_emergency"] is True
    assert high_probe._attributes["pre_discharge_start_eligible"] is False


async def _test_dc_to_ac_live_discharge_limit() -> None:
    samples = _base_samples()
    samples["sensor.hoymiles_hit_maximum_discharge_current"] = (20.0, True)
    probe = _new_probe(samples)
    probe._last_full_optimizer_input = replace(
        probe._last_full_optimizer_input,
        charge_efficiency_percent=95.0,
        house_discharge_efficiency_percent=80.0,
    )
    probe._last_full_optimizer_result = rcm_optimizer.optimize_rcm(
        probe._last_full_optimizer_input
    )
    await probe._async_live_control_refresh(NOW)
    assert probe._attributes["bms_discharge_available"] is True
    assert probe._attributes["bms_discharge_dc_power_limit_kw"] == 1.04
    assert probe._attributes["bms_discharge_power_limit_kw"] == 0.832

    for discharge_sample in ((0.0, True), (None, False)):
        failed_samples = _base_samples()
        failed_samples[
            "sensor.hoymiles_hit_maximum_discharge_current"
        ] = discharge_sample
        failed = _new_probe(failed_samples)
        await failed._async_live_control_refresh(NOW)
        assert failed._attributes["bms_discharge_available"] is False
        assert failed._attributes["bms_discharge_dc_power_limit_kw"] == 0.0
        assert failed._attributes["bms_discharge_power_limit_kw"] == 0.0


async def _test_no_cached_plan_is_fail_closed() -> None:
    samples = _base_samples()
    for entity_id in GRID_VOLTAGE_ENTITIES:
        samples[entity_id] = (None, False)
    probe = _new_probe(samples)
    probe._last_full_optimizer_input = None
    probe._attributes.update(
        {
            "action": "absorb_pv",
            "emergency_action_ready": True,
            "recommended_charge_limit_percent": 100.0,
            "recommended_export_limit_percent": 0.0,
            "pre_discharge_start_eligible": True,
        }
    )
    await probe._async_live_control_refresh(NOW)
    attrs = probe._attributes
    assert attrs["action"] == "hold"
    assert attrs["emergency_action_ready"] is False
    assert attrs["recommended_charge_limit_percent"] == 80.0
    assert attrs["recommended_export_limit_percent"] == 50.0
    assert attrs["pre_discharge_start_eligible"] is False
    assert probe.hass.executor_calls == []
    assert probe._full_plan_solver_calls == 7


async def _test_expired_cached_plan_withdraws_authority() -> None:
    samples = _base_samples()
    probe = _new_probe(samples)
    probe._last_full_plan_at = NOW - timedelta(seconds=631)
    probe._attributes.update(
        {
            "action": "grid_discharge_preparation",
            "pre_discharge_start_eligible": True,
            "emergency_action_ready": True,
        }
    )
    await probe._async_live_control_refresh(NOW)
    attrs = probe._attributes
    assert attrs["live_control_cache_current"] is False
    assert attrs["result_current"] is False
    assert attrs["recalculation_pending"] is True
    assert attrs["execution_input_valid"] is False
    assert attrs["execution_blocker_code"] == "certified_cache_expired"
    assert attrs["action"] == "hold"
    assert attrs["pre_discharge_start_eligible"] is False
    assert attrs["emergency_action_ready"] is False
    assert probe.hass.executor_calls == []


async def _test_live_executor_drift_is_fail_closed() -> None:
    samples = _base_samples()
    samples[GRID_VOLTAGE_ENTITIES[0]] = (253.3, True)
    probe = _new_probe(samples)
    fingerprints = iter((("before",), ("after",)))
    probe._async_live_control_refresh.__func__.__globals__[
        "_rcm_live_control_fingerprint"
    ] = lambda _hass, _runtime, _samples: next(fingerprints)
    await probe._async_live_control_refresh(NOW)
    attrs = probe._attributes
    assert attrs["live_emergency"] is True
    assert attrs["emergency_action_ready"] is False
    assert attrs["action"] == "monitor"
    assert attrs["recommended_charge_limit_percent"] == 80.0
    assert attrs["recommended_export_limit_percent"] == 50.0
    assert attrs["live_control_input_current"] is False
    assert probe._full_plan_solver_calls == 7


def _compile_signature_functions() -> dict[str, Any]:
    wanted = {
        "_rcm_shared_sample_signature",
        "_rcm_shared_optimizer_signature",
    }
    functions = [
        deepcopy(node)
        for node in _sensor_tree().body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    assert {function.name for function in functions} == wanted
    namespace: dict[str, Any] = {
        "Any": Any,
        "RuntimeData": Any,
        "datetime": datetime,
        "_shared_inputs_snapshot": lambda runtime: runtime.snapshot,
    }
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    exec(compile(module, "<rcm-shared-signature>", "exec"), namespace)
    return namespace


def _sample(
    value: float,
    *,
    entity_id: str,
    reported_at: datetime = NOW,
    provenance: str = "physical_readback",
) -> Any:
    return SimpleNamespace(
        value=value,
        entity_id=entity_id,
        source_entity_ids=(entity_id,),
        selector_entity_id=None,
        reported_at=reported_at,
        fresh=True,
        reason="fresh",
        quality="physical",
        provenance=provenance,
    )


def _shared_snapshot(
    *,
    pv_value: float = 8.0,
    battery_to_home_efficiency: float = 94.0,
    revision: int = 1,
) -> Any:
    system = SimpleNamespace(
        battery_capacity_kwh=_sample(21.0, entity_id="sensor.capacity"),
        total_capacity_kwh=_sample(21.0, entity_id="sensor.total_capacity"),
        battery_soc_percent=_sample(70.0, entity_id="sensor.soc"),
        self_use_reserve_soc_percent=_sample(20.0, entity_id="sensor.reserve"),
        inverter_count=_sample(1.0, entity_id="sensor.count"),
        inverter_rated_power_each_kw=_sample(10.0, entity_id="input_select.power"),
    )
    forecast = SimpleNamespace(
        today=_sample(20.0, entity_id="sensor.forecast_today"),
        tomorrow=_sample(22.0, entity_id="sensor.forecast_tomorrow"),
        remaining_today=_sample(9.0, entity_id="sensor.forecast_remaining"),
    )
    load = SimpleNamespace(
        average_daily_home_load_kwh=16.0,
        daily_totals_kwh=(15.0, 16.0),
        average_profile_30m_kwh=(0.3,) * 48,
        weekday_profile_30m_kwh=(0.3,) * 48,
        weekend_profile_30m_kwh=(0.4,) * 48,
        generated_at=NOW,
        ready=True,
        fallback_currently_used=False,
        source="same_entry_recorder",
    )
    bms = SimpleNamespace(
        voltage_v=_sample(52.0, entity_id="sensor.bms_voltage"),
        maximum_charge_current_a=_sample(175.0, entity_id="sensor.bms_charge"),
        maximum_discharge_current_a=_sample(175.0, entity_id="sensor.bms_discharge"),
    )
    efficiency = SimpleNamespace(
        battery_to_home_efficiency=_sample(
            battery_to_home_efficiency,
            entity_id="input_number.hoymiles_ems_battery_to_home_efficiency",
            provenance="neutral_model_helper",
        )
    )
    gcf = SimpleNamespace(
        enable_code=_sample(1.0, entity_id="sensor.gcf_enable"),
        maximum_export_power_percent=_sample(50.0, entity_id="sensor.gcf_limit"),
        gcf_readback_ready=True,
    )
    power = SimpleNamespace(
        pv_power_kw=_sample(pv_value, entity_id="sensor.pv"),
        home_load_power_kw=_sample(2.0, entity_id="sensor.load"),
        grid_power_kw=_sample(5.0, entity_id="sensor.grid"),
    )
    return SimpleNamespace(
        schema_version=1,
        config_entry_id="entry-1",
        captured_at=NOW,
        revision=revision,
        system=system,
        forecast=forecast,
        load=load,
        bms=bms,
        efficiency=efficiency,
        gcf=gcf,
        power=power,
    )


def _test_full_run_fingerprint() -> None:
    namespace = _compile_signature_functions()
    signature = namespace["_rcm_shared_optimizer_signature"]
    first_snapshot = _shared_snapshot(revision=1)
    first = signature(SimpleNamespace(snapshot=first_snapshot))
    # A coordinator counter alone is not a consumed value.
    assert first == signature(
        SimpleNamespace(snapshot=_shared_snapshot(revision=999))
    )
    # Values, physical source/report provenance and capture time used for LOAD
    # freshness each invalidate an in-flight result.
    assert first != signature(
        SimpleNamespace(snapshot=_shared_snapshot(pv_value=8.1))
    )
    assert first != signature(
        SimpleNamespace(
            snapshot=_shared_snapshot(battery_to_home_efficiency=93.0)
        )
    )
    source_changed = _shared_snapshot()
    source_changed.system.battery_soc_percent.entity_id = "sensor.soc_rebound"
    assert first != signature(SimpleNamespace(snapshot=source_changed))
    report_changed = _shared_snapshot()
    report_changed.bms.maximum_charge_current_a.reported_at = NOW + timedelta(
        seconds=1
    )
    assert first != signature(SimpleNamespace(snapshot=report_changed))
    capture_changed = _shared_snapshot()
    capture_changed.captured_at = NOW + timedelta(seconds=1)
    assert first != signature(SimpleNamespace(snapshot=capture_changed))

    method = deepcopy(_sensor_method("_current_input_fingerprint"))
    method.decorator_list = []
    method.returns = None
    probe_class = ast.ClassDef(
        name="FingerprintProbe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[method],
    )

    def entity_fingerprint(
        hass: Any,
        entity_ids: Any,
        **_kwargs: Any,
    ) -> tuple[Any, ...]:
        return tuple(
            (entity_id, hass.states.get(entity_id))
            for entity_id in sorted(entity_ids)
        )

    fingerprint_namespace = {
        "Any": Any,
        "RCE_LOAD_BROKER_ATTRIBUTES": (),
        "RCM_LIVE_CONTROL_ENTITIES": frozenset({"sensor.live"}),
        "optimizer_input_fingerprint": entity_fingerprint,
        "_rcm_shared_optimizer_signature": lambda runtime: runtime.signature,
        "_rcm_voltage_samples_signature": lambda samples: tuple(samples),
    }
    module = ast.fix_missing_locations(
        ast.Module(body=[probe_class], type_ignores=[])
    )
    exec(compile(module, "<rcm-full-fingerprint>", "exec"), fingerprint_namespace)
    probe = fingerprint_namespace["FingerprintProbe"]()
    probe.hass = SimpleNamespace(states={"sensor.slow": 1, "sensor.live": 10})
    probe._runtime = SimpleNamespace(signature=("shared", 1))
    probe._rce_plan_source = None
    probe._samples = deque([(NOW, 240.0)])
    probe._full_plan_input_entities = lambda: frozenset({"sensor.slow"})
    initial = probe._current_input_fingerprint()
    probe._samples.append((NOW + timedelta(seconds=15), 241.0))
    assert initial != probe._current_input_fingerprint()
    probe._samples.pop()
    probe.hass.states["sensor.live"] = 11
    assert initial != probe._current_input_fingerprint()
    probe.hass.states["sensor.live"] = 10
    probe._runtime.signature = ("shared", 2)
    assert initial != probe._current_input_fingerprint()


async def main() -> None:
    live_source = ast.get_source_segment(
        (COMPONENT / "rcm_sensor.py").read_text(encoding="utf-8"),
        _sensor_method("_async_live_control_refresh"),
    ) or ""
    assert "async_add_executor_job" not in live_source
    assert "optimize_rcm(" not in live_source
    await _test_live_decisions()
    await _test_bms_and_export_gates()
    await _test_predictive_voltage_validity()
    await _test_dc_to_ac_live_discharge_limit()
    await _test_no_cached_plan_is_fail_closed()
    await _test_expired_cached_plan_withdraws_authority()
    await _test_live_executor_drift_is_fail_closed()
    _test_full_run_fingerprint()
    print("RCEm live control: threshold, fail-closed gates and cadence passed")


if __name__ == "__main__":
    asyncio.run(main())
