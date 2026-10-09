"""Offline behavioral probes of the real RCE settling sensor methods."""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import logging
from math import isfinite
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
from typing import Any
from zoneinfo import ZoneInfo

import test_rce_optimizer as fixtures
import test_optimizer_executor_contract as executor_fixtures
from tariff_optimizer import TariffSchedule, tariff_rate
from tariff_profiles import MANUAL_OPERATOR, SUPPORTED_GROUPS, get_tariff_profile, profile_is_valid
from rce_price_cache import SOURCE as RCE_CACHE_SOURCE, cached_state_valid, utc_time


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "custom_components/hoymiles_hit_modbus/rce_sensor.py"
RCE = fixtures.RCE
NOW = fixtures.NOW.replace(hour=18)


def load_probe():
    """Execute production methods with only HA scheduling/publication replaced."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    function_names = {
        "_state_number", "_state_text", "_shared_inputs_snapshot",
        "_shared_input_field", "_preferred_text_helper", "_helper_minutes",
        "_first_numeric_state", "_rce_rows_for_local_date",
        "_complete_rce_half_hours_for_local_date",
        "_select_current_rce_price_rows", "_rce_state_rows_and_age",
        "_rce_publication_fingerprints_match",
    }
    constant_names = {
        "_SHARED_INPUT_MISSING", "_RCE_PRICE_MAX_AGE_SECONDS",
        "_TOMORROW_FORECAST_MAX_AGE_SECONDS", "EMS_TOMORROW_FORECAST_ENTITY_HELPER",
        "TOMORROW_FORECAST_ENTITY_HELPER", "TOMORROW_FORECAST_CANDIDATES",
        "_RCE_REVALIDATED_NUMERIC_INPUTS",
    }
    methods = {
        "current_post_command_settling_market_fingerprint",
        "_same_entry_tariff_plan_state",
        "_post_command_settling_avoided_import_price",
        "async_recalculate_post_command_settling", "_recalculate_and_write",
        "_recalculate_locked", "_reject_stale_executor_result",
        "_invalidate_internal_inputs", "_mark_recalculation_pending",
        "_mark_result_current", "_cancel_delayed_recalculation",
        "_cancel_stale_result_retry",
        "_active_rce_commitment",
        "_async_wait_active_rce_commitment_cohort",
    }
    nodes = [deepcopy(node) for node in tree.body if (
        isinstance(node, ast.FunctionDef) and node.name in function_names
    ) or (
        isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id in constant_names for t in node.targets)
    )]
    source_class = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                        and node.name == "HoymilesRCEOptimizerSensor")
    body = [deepcopy(node) for node in source_class.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in methods]
    assert {node.name for node in body} == methods
    for node in body:
        node.decorator_list = []
    nodes.append(ast.ClassDef(name="Probe", bases=[], keywords=[], body=body,
                              decorator_list=[]))
    clock = SimpleNamespace(now_value=NOW)
    # Production methods now import the PV adapter relatively. Load that
    # real adapter in a small package without importing Home Assistant.
    package_name = "_rce_settling_probe_modules"
    if package_name not in sys.modules:
        package = ModuleType(package_name)
        package.__path__ = [str(SOURCE.parent)]
        sys.modules[package_name] = package
    namespace = {
        "RCE_CACHE_SOURCE": RCE_CACHE_SOURCE,
        "cached_state_valid": cached_state_valid,
        "utc_time": utc_time,
        "__name__": package_name + ".rce_sensor_probe",
        "__package__": package_name,
        "asyncio": asyncio, "replace": replace, "Mapping": Mapping, "Any": Any,
        "deepcopy": deepcopy, "SimpleNamespace": SimpleNamespace,
        "LOAD_PHASE_ENERGY_ENTITIES": (),
        "revalidate_rce_plan": RCE.revalidate_rce_plan,
        "retain_active_rce_slot": RCE.retain_active_rce_slot,
        "active_load_only_export_suppressed": RCE.active_load_only_export_suppressed,
        "RceActiveCommitment": RCE.RceActiveCommitment,
        "date": date, "datetime": datetime, "time": time, "timedelta": timedelta,
        "ZoneInfo": ZoneInfo, "STATE_UNKNOWN": "unknown",
        "STATE_UNAVAILABLE": "unavailable", "parse_rce_rows": RCE.parse_rce_rows,
        "post_command_settling_market_fingerprint": RCE.post_command_settling_market_fingerprint,
        "numeric_state_sample": fixtures.ENERGY_DATA.numeric_state_sample,
        "state_age_seconds": fixtures.ENERGY_DATA.state_age_seconds,
        "optimize_rce": RCE.optimize_rce, "_LOGGER": logging.getLogger(__name__),
        "TariffSchedule": TariffSchedule, "tariff_rate": tariff_rate,
        "MANUAL_OPERATOR": MANUAL_OPERATOR, "get_tariff_profile": get_tariff_profile,
        "SUPPORTED_GROUPS": SUPPORTED_GROUPS,
        "profile_is_valid": profile_is_valid, "isfinite": isfinite,
        "MAX_IMMEDIATE_RECALCULATIONS": 3,
        "dt_util": SimpleNamespace(
            UTC=timezone.utc, parse_datetime=datetime.fromisoformat,
            now=lambda: clock.now_value,
            utcnow=lambda: clock.now_value.astimezone(timezone.utc)),
    }
    module = ast.fix_missing_locations(ast.Module(
        body=[ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes],
        type_ignores=[]))
    exec(compile(module, str(SOURCE), "exec"), namespace)
    return namespace["Probe"], clock


class States(dict):
    def is_state(self, entity_id, value):
        item = self.get(entity_id)
        return item is not None and item.state == value


def state(raw_value, **attrs):
    return SimpleNamespace(state=str(raw_value), attributes=attrs,
                           last_updated=NOW, last_reported=NOW)


def probe():
    probe_type, clock = load_probe()
    item = probe_type()
    rows = fixtures._official_pse_rows_for_local_day(NOW.replace(hour=0))
    prices = RCE.parse_rce_rows(rows, fixtures.WARSAW, block_enabled=False,
                               block_start_minute=0, block_end_minute=0)
    settings = fixtures.base_input(
        now=NOW, price_slots=prices, battery_wear_cost_pln_kwh=.08,
        avoided_import_price_pln_kwh=1.0, export_efficiency_percent=95.0,
        house_discharge_efficiency_percent=95.0,
        current_load_power_kw=0.0, current_pv_power_kw=0.0)
    states = States({
        "sensor.hoymiles_rce_day": state("ok", value=rows),
        "input_boolean.hoymiles_sale_block_enabled": state("off"),
        "input_datetime.hoymiles_sale_block_start": state("00:00:00"),
        "input_datetime.hoymiles_sale_block_end": state("00:00:00"),
        "input_number.hoymiles_rce_export_efficiency": state(95),
        "input_number.hoymiles_rce_minimum_net_export_power": state(settings.minimum_net_export_power_kw),
        "input_number.hoymiles_tariff_discharge_efficiency": state(95),
        "input_number.hoymiles_tariff_g11_price": state(1),
        "input_select.hoymiles_tariff_operator": state("Manual"),
        "input_select.hoymiles_tariff_type": state("G11"),
    })
    item.hass = SimpleNamespace(states=states, config=SimpleNamespace(time_zone="Europe/Warsaw"))
    item._runtime = SimpleNamespace(shared_inputs=None)
    item._entry = SimpleNamespace(entry_id="entry")
    item._tariff_plan_source = None
    item._post_command_settling_market_settings = settings
    item._lifecycle_stopped = False
    item._optimizer_lock = asyncio.Lock()
    item._delayed_recalculate_tasks = set()
    item._input_revision = executor_fixtures._load_revision_module().OptimizerInputRevision()
    item._attributes = {"result_current": True, "recalculation_pending": False}
    item._result = None
    item._timeline_sensor = None
    item._stale_result_retry_cancel = None
    item._recalculate_cancel = None
    item._forecast_gcf_policy_evaluation_cancel = None
    item._full_plan_solver_calls = 0
    item._last_full_plan_at = None
    item._full_plan_trigger = "periodic"
    item._current_slot_continue_eligible = None
    item._current_slot_continue_changed_at = None
    item._shared_inputs_dirty = True
    item.native_value = "ready"
    item.async_write_ha_state = lambda: None
    item._publish_timeline_result = lambda: None
    item._schedule_stale_result_retry = lambda: None
    item._retain_last_complete_plan = lambda **_kw: False
    item._current_input_fingerprint = lambda: (("fixture_revision", item._input_revision.value),)
    return item, clock, settings


def test_market_basis_is_current_before_callbacks():
    item, clock, settings = probe()
    expected = RCE.post_command_settling_market_fingerprint(settings)
    assert item.current_post_command_settling_market_fingerprint() == expected
    item._attributes.update(result_current=False, recalculation_pending=True)
    item.hass.states["input_number.hoymiles_rce_requested_discharge_power"] = state(40)
    item.hass.states["sensor.hoymiles_actual_load_power"] = state(7860)
    assert item.current_post_command_settling_market_fingerprint() == expected
    clock.now_value += timedelta(seconds=45)
    item.hass.states["sensor.hoymiles_rce_day"].last_reported = clock.now_value
    assert item.current_post_command_settling_market_fingerprint() == expected
    rows = item.hass.states["sensor.hoymiles_rce_day"].attributes["value"]
    rows[75]["rce_pln"] += 10
    assert item.current_post_command_settling_market_fingerprint() is None
    rows[75]["rce_pln"] -= 10
    assert item.current_post_command_settling_market_fingerprint() == expected
    item.hass.states["input_boolean.hoymiles_sale_block_enabled"] = state("on")
    item.hass.states["input_datetime.hoymiles_sale_block_end"] = state("19:00")
    assert item.current_post_command_settling_market_fingerprint() is None


def test_missing_stale_economics_and_sources_fail_closed():
    for entity_id in (
        "sensor.hoymiles_rce_day", "input_boolean.hoymiles_sale_block_enabled",
        "input_datetime.hoymiles_sale_block_start",
        "input_number.hoymiles_rce_export_efficiency",
        "input_number.hoymiles_tariff_discharge_efficiency",
        "input_number.hoymiles_tariff_g11_price",
    ):
        item, _, _ = probe()
        item.hass.states.pop(entity_id)
        assert item.current_post_command_settling_market_fingerprint() is None, entity_id
    for seconds in (1201, -6):
        item, _, _ = probe()
        price = item.hass.states["sensor.hoymiles_rce_day"]
        price.last_reported = NOW - timedelta(seconds=seconds)
        assert item.current_post_command_settling_market_fingerprint() is None
    for entity_id in (
        "input_number.hoymiles_rce_battery_wear_cost",
        "input_number.hoymiles_rce_minimum_net_export_power",
        "input_number.hoymiles_rce_export_efficiency",
        "input_number.hoymiles_tariff_discharge_efficiency",
        "input_number.hoymiles_tariff_g11_price",
    ):
        for invalid in ("nan", "inf", "-inf", "unavailable", "unknown", "invalid", -0.01):
            item, _, _ = probe()
            item.hass.states[entity_id] = state(invalid)
            assert item.current_post_command_settling_market_fingerprint() is None


def test_optional_legacy_wear_default_and_live_changes():
    item, _, settings = probe()
    entity_id = "input_number.hoymiles_rce_battery_wear_cost"
    assert entity_id not in item.hass.states  # Actual managed installation.
    expected = RCE.post_command_settling_market_fingerprint(settings)
    assert item.current_post_command_settling_market_fingerprint() == expected
    # The synchronous getter sees a newly added override before any callback.
    item.hass.states[entity_id] = state(.09)
    assert item._post_command_settling_market_settings is settings
    assert item.current_post_command_settling_market_fingerprint() is None
    # An explicit equal default does not change the accepted economics.
    item.hass.states[entity_id] = state(.08)
    assert item.current_post_command_settling_market_fingerprint() == expected
    item.hass.states[entity_id] = state(0)
    assert item.current_post_command_settling_market_fingerprint() is None
    # A valid explicit zero is preserved, never replaced with the default.
    item._post_command_settling_market_settings = replace(settings, battery_wear_cost_pln_kwh=0)
    assert item.current_post_command_settling_market_fingerprint() == RCE.post_command_settling_market_fingerprint(
        item._post_command_settling_market_settings)
    item.hass.states.pop(entity_id)
    assert item.current_post_command_settling_market_fingerprint() is None


def test_minimum_export_default_and_changes():
    item, _, settings = probe()
    entity_id = "input_number.hoymiles_rce_minimum_net_export_power"
    accepted = replace(settings, minimum_net_export_power_kw=2.0)
    item._post_command_settling_market_settings = accepted
    item.hass.states.pop(entity_id)
    expected = RCE.post_command_settling_market_fingerprint(accepted)
    assert item.current_post_command_settling_market_fingerprint() == expected
    item.hass.states[entity_id] = state(2)
    assert item.current_post_command_settling_market_fingerprint() == expected
    item.hass.states[entity_id] = state(3)
    assert item.current_post_command_settling_market_fingerprint() is None


def test_shared_efficiency_and_same_entry_tariff_are_live():
    item, _, settings = probe()
    expected = RCE.post_command_settling_market_fingerprint(settings)
    item._runtime.shared_inputs = SimpleNamespace(snapshot=SimpleNamespace(
        efficiency=SimpleNamespace(battery_to_home_efficiency=SimpleNamespace(value=95))))
    item.hass.states["input_number.hoymiles_ems_battery_to_home_efficiency"] = state(95)
    assert item.current_post_command_settling_market_fingerprint() == expected
    # Actual helper changes before the broker callback: its old 95 is irrelevant.
    item.hass.states["input_number.hoymiles_ems_battery_to_home_efficiency"] = state(90)
    assert item.current_post_command_settling_market_fingerprint() is None
    item.hass.states["input_number.hoymiles_ems_battery_to_home_efficiency"] = state(95)
    item._tariff_plan_source = SimpleNamespace(_entry=item._entry, entity_id="sensor.exact_tariff")
    item.hass.states["sensor.exact_tariff"] = state(
        "ready", g11_reference_price_pln_kwh=1,
        result_current=True, recalculation_pending=False)
    assert item.current_post_command_settling_market_fingerprint() == expected
    item.hass.states["sensor.exact_tariff"].attributes["g11_reference_price_pln_kwh"] = 1.1
    assert item.current_post_command_settling_market_fingerprint() is None


def test_tariff_pricing_drift_before_broker_callback():
    item, _, settings = probe()
    item._tariff_plan_source = SimpleNamespace(_entry=item._entry, entity_id="sensor.exact_tariff")
    published = state("ready", current_price_pln_kwh=1,
                      result_current=True, recalculation_pending=False)
    item.hass.states["sensor.exact_tariff"] = published
    for name in ("low", "medium", "peak"):
        item.hass.states[f"input_number.hoymiles_tariff_{name}_price"] = state(1)
    for name in ("cheap_1_start", "cheap_1_end", "cheap_2_start", "cheap_2_end",
                 "medium_start", "medium_end"):
        item.hass.states[f"input_datetime.hoymiles_tariff_{name}"] = state("00:00")
    for name in ("weekend", "polish_holidays"):
        item.hass.states[f"input_boolean.hoymiles_tariff_{name}_low_price"] = state("off")
    expected = RCE.post_command_settling_market_fingerprint(settings)
    assert item.current_post_command_settling_market_fingerprint() == expected
    item.hass.states["input_number.hoymiles_tariff_g11_price"] = state(1.1)
    assert published.attributes["result_current"] is True  # callback has not run
    assert item.current_post_command_settling_market_fingerprint() is None
    item.hass.states["input_number.hoymiles_tariff_g11_price"] = state(1)
    for attribute, changed in (("result_current", False), ("recalculation_pending", True)):
        original = published.attributes[attribute]
        published.attributes[attribute] = changed
        assert item.current_post_command_settling_market_fingerprint() is None
        published.attributes[attribute] = original
    # Official G12 uses its actual profile reference, not an unused Manual helper.
    profile = get_tariff_profile("PGE", "G12")
    item.hass.states["input_select.hoymiles_tariff_operator"] = state("PGE")
    item.hass.states["input_select.hoymiles_tariff_type"] = state("G12")
    published.attributes["tariff_profile_g11_price"] = profile.g11_price_pln_kwh
    item._post_command_settling_market_settings = replace(
        settings, avoided_import_price_pln_kwh=profile.g11_price_pln_kwh)
    assert item.current_post_command_settling_market_fingerprint() is not None
    item.hass.states["input_number.hoymiles_tariff_g11_price"] = state(5)
    assert item.current_post_command_settling_market_fingerprint() is not None
    item.hass.states["input_select.hoymiles_tariff_type"] = state("G12e")
    assert item.current_post_command_settling_market_fingerprint() is not None
    item.hass.states["input_select.hoymiles_tariff_operator"] = state("TAURON")
    assert item.current_post_command_settling_market_fingerprint() is None
    item.hass.states["input_select.hoymiles_tariff_operator"] = state("Manual")
    assert item.current_post_command_settling_market_fingerprint() is None
    item.hass.states.pop("sensor.exact_tariff")
    assert item.current_post_command_settling_market_fingerprint() is None


def test_tomorrow_scope_and_day_rollover():
    item, clock, settings = probe()
    rows = fixtures._official_pse_rows_for_local_day(NOW.replace(hour=0) + timedelta(days=1))
    item.hass.states["sensor.hoymiles_rce_day_tomorrow"] = state("ok", value=rows)
    assert item.current_post_command_settling_market_fingerprint() is not None
    item.hass.states["input_text.hoymiles_ems_pv_forecast_tomorrow_entity"] = state("sensor.tomorrow")
    item.hass.states["sensor.tomorrow"] = state(10)
    assert item.current_post_command_settling_market_fingerprint() is None
    tomorrow = RCE.parse_rce_rows(rows, fixtures.WARSAW, block_enabled=False,
                                 block_start_minute=0, block_end_minute=0)
    item._post_command_settling_market_settings = replace(settings, price_slots=[*settings.price_slots, *tomorrow])
    assert item.current_post_command_settling_market_fingerprint() is not None
    item.hass.states["sensor.hoymiles_rce_day_tomorrow"].last_reported -= timedelta(seconds=1201)
    assert item.current_post_command_settling_market_fingerprint() is None
    clock.now_value += timedelta(days=1)
    assert item.current_post_command_settling_market_fingerprint() is None


async def test_recalculation_uses_revision_and_commit_path():
    item, _, settings = probe()
    item._post_command_settling_market_settings = None
    current = [settings]
    item._optimizer_input = lambda: (current[0], {
        "rce_today_data_fresh": True, "forecast_today_data_fresh": True,
        "soc_data_fresh": True, "gcf_execution_data_fresh": True,
    })
    entered, release = asyncio.Event(), asyncio.Event()
    seen = []

    async def solve(function, captured):
        seen.append(captured)
        if len(seen) == 1:
            entered.set()
            await release.wait()
        return function(captured)

    item.hass.async_add_executor_job = solve
    task = asyncio.create_task(item.async_recalculate_post_command_settling())
    await entered.wait()
    assert item._attributes["result_current"] is False
    assert item._optimizer_lock.locked()
    current[0] = replace(settings, discharge_power_percent=40)
    item._invalidate_internal_inputs()
    assert item._post_command_settling_market_settings is None
    release.set()
    await task
    assert seen == [settings, current[0]]
    assert item._post_command_settling_market_settings is current[0]
    assert item._attributes["post_command_settling_market_fingerprint"] == RCE.post_command_settling_market_fingerprint(settings)
    assert type(item._attributes["current_slot_load_exhausts_requested_discharge_budget"]) is bool
    assert item._attributes["result_current"] is True
    assert item._attributes["last_full_plan_trigger"] == "post_command_settling"
    assert item._full_plan_solver_calls == 2
    assert not item._delayed_recalculate_tasks
    item._optimizer_input = lambda: (None, {"missing_entities": ["physical_cap"]})
    await item.async_recalculate_post_command_settling()
    assert item.current_post_command_settling_market_fingerprint() is None
    assert item._attributes["current_slot_load_exhausts_requested_discharge_budget"] is False


async def test_unload_cancels_waiting_and_running_refresh():
    for waiting_on_lock in (True, False):
        item, _, settings = probe()
        item._optimizer_input = lambda: (settings, {})
        entered = asyncio.Event()

        async def solve(_function, _settings):
            entered.set()
            await asyncio.Event().wait()

        item.hass.async_add_executor_job = solve
        if waiting_on_lock:
            await item._optimizer_lock.acquire()
        task = asyncio.create_task(item.async_recalculate_post_command_settling())
        if waiting_on_lock:
            await asyncio.sleep(0)
        else:
            await entered.wait()
        item._cancel_delayed_recalculation()
        result = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(result[0], asyncio.CancelledError)
        assert item.current_post_command_settling_market_fingerprint() is None
        assert not item._delayed_recalculate_tasks
        assert item._result is None
        calls = item._full_plan_solver_calls
        await item.async_recalculate_post_command_settling()
        assert item._full_plan_solver_calls == calls
        if waiting_on_lock:
            item._optimizer_lock.release()

    # An executor wrapper can swallow cancellation while finishing a thread
    # result. Unloaded entities still must not commit or start another solve.
    item, _, settings = probe()
    item._optimizer_input = lambda: (settings, {
        "rce_today_data_fresh": True, "forecast_today_data_fresh": True,
        "soc_data_fresh": True, "gcf_execution_data_fresh": True,
    })
    entered = asyncio.Event()

    async def ignores_cancellation(function, captured):
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return function(captured)

    item.hass.async_add_executor_job = ignores_cancellation
    task = asyncio.create_task(item.async_recalculate_post_command_settling())
    await entered.wait()
    item._cancel_delayed_recalculation()
    await task
    assert item._result is None
    assert item._attributes["result_current"] is False
    assert item._full_plan_solver_calls == 1


def main():
    tests = (
        test_market_basis_is_current_before_callbacks,
        test_missing_stale_economics_and_sources_fail_closed,
        test_optional_legacy_wear_default_and_live_changes,
        test_minimum_export_default_and_changes,
        test_shared_efficiency_and_same_entry_tariff_are_live,
        test_tariff_pricing_drift_before_broker_callback,
        test_tomorrow_scope_and_day_rollover,
    )
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    for test in (test_recalculation_uses_revision_and_commit_path,
                 test_unload_cancels_waiting_and_running_refresh):
        asyncio.run(test())
        print(f"PASS {test.__name__}")
    print("PASS 9 RCE post-command settling sensor groups")


if __name__ == "__main__":
    main()
