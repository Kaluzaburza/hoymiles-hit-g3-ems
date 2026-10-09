"""Live-cap regressions: real tariff bridge/controller and rendered HA YAML.

All telemetry is synthetic. No Home Assistant connection or inverter writes.
Requires the existing test dependencies Jinja2 and PyYAML on PYTHONPATH.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from math import isfinite
from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, StrictUndefined
import yaml

import test_tariff_active_controller as fixture
from supervisor_active_bridge import (
    settings_from_execution_source,
    same_run_tariff_wait_authorized,
    tariff_command_within_live_bms_limit,
)

ROOT = Path(__file__).resolve().parents[1]
NOW = fixture.NOW
CHECKS = 0


def check(condition, message):
    global CHECKS
    CHECKS += 1
    assert condition, message


async def controller_with_command(power, *, confirm=False, resample_changes=None):
    sent, saved, clock = [], [], [NOW]

    async def persist(record):
        saved.append(record)

    async def dispatch(write):
        sent.append(write)

    controller = fixture.SupervisorActiveController(
        persist=persist, dispatch=dispatch, publish=lambda record: None,
        clock=lambda: clock[0],
    )
    await controller.async_initialize()
    source = fixture.execution_source(NOW, bms_voltage_v=54, bms_max_charge_current_a=240)
    if resample_changes is not None:
        controller._frame_resampler = lambda: fixture.frame(
            clock[0], replace(source, **resample_changes), controller.record, power=power,
        )
    await controller.async_reconcile(fixture.frame(NOW, source, power=power))
    if resample_changes is not None:
        return controller, sent, clock
    check(controller.record.state is fixture.ActiveState.WAITING_READBACK, "fixture sends exactly one command")
    check(len(sent) == 1, "fixture command count")
    if confirm:
        clock[0] += timedelta(seconds=2)
        source = replace(fixture.physical(clock[0], power=power), bms_voltage_v=54)
        await controller.async_reconcile(fixture.frame(clock[0], source, controller.record, power=power))
        check(controller.record.state is fixture.ActiveState.EXECUTING, "fixture gets separate physical ACK")
    return controller, sent, clock


def rendered_readiness(source, tariff):
    """Render the actual canonical template with HA-compatible value helpers."""
    class States:
        def __init__(self):
            self.items = {}
            self.sensor = SimpleNamespace()

        def put(self, entity_id, value, *, attributes=None, reported=NOW):
            state = SimpleNamespace(state=str(value), attributes=attributes or {},
                                    last_reported=reported, last_updated=reported)
            self.items[entity_id] = state
            if entity_id.startswith("sensor."):
                setattr(self.sensor, entity_id.split(".", 1)[1], state)

        def __call__(self, entity_id):
            state = self.items.get(entity_id)
            return state.state if state else "unknown"

    states = States()
    states.put("sensor.hoymiles_hit_tariff_charge_plan", "ready", reported=tariff.observed_at,
               attributes={
                   "status_code": "ready", "result_current": True, "recalculation_pending": False,
                   "forecast_data_fresh": True, "forecast_today_age_minutes": 0,
                   "forecast_tomorrow_age_minutes": 0, "control_inputs_fresh": True,
                   "system_power_kw": tariff.system_power_kw,
                   "command_charge_power_percent": tariff.command_charge_power_percent,
                   # Deliberately larger than the current cap: this is model data.
                   "bms_charge_power_limit_kw": 12.96,
               })
    for entity_id, value, stamp in (
        ("sensor.hoymiles_hit_maximum_charge_current", source.bms_max_charge_current_a, source.bms_charge_current_observed_at),
        ("sensor.hoymiles_hit_battery_voltage_bms", source.bms_voltage_v, source.bms_voltage_observed_at),
        ("sensor.hoymiles_hit_overview_battery_soc", 60, NOW),
        ("sensor.hoymiles_tariff_target_soc", 58, NOW),
        ("sensor.hoymiles_hit_ems_force_charge_soc_readback", 58, NOW),
        ("sensor.hoymiles_hit_ems_maximum_charge_power_readback", 30, NOW),
    ):
        states.put(entity_id, value, reported=stamp)
    states.put("binary_sensor.hoymiles_ems_execution_ready", "on")

    def numeric(value):
        try:
            return not isinstance(value, bool) and isfinite(float(value))
        except (ValueError, TypeError, OverflowError):
            return False

    env = Environment(undefined=StrictUndefined)
    env.globals.update(
        states=states, now=lambda: NOW, is_number=numeric,
        is_state=lambda entity, value: states(entity) == value,
        state_attr=lambda entity, name: states.items[entity].attributes.get(name),
        as_timestamp=lambda value, default=0: value.timestamp() if hasattr(value, "timestamp") else default,
    )
    return (
        env.from_string(READY_TEMPLATE["state"]).render().strip() == "True",
        env.from_string(READY_TEMPLATE["attributes"]["reason"]).render().strip(),
    )


async def test_cap_and_yaml():
    controller, _sent, _clock = await controller_with_command(30)
    intent = controller.record.transaction.intent
    src = fixture.execution_source(NOW, bms_voltage_v=53, bms_max_charge_current_a=240)
    tariff = fixture.frame(NOW, src, power=30).tariff
    mutations = [
        ("voltage relaxation", {}, {}, True),
        ("sufficient reduced positive CCL", {"bms_max_charge_current_a": 100}, {}, True),
        ("insufficient positive CCL", {"bms_max_charge_current_a": 50}, {}, False),
        ("zero CCL", {"bms_max_charge_current_a": 0}, {}, False),
        ("missing CCL", {"bms_max_charge_current_a": None}, {}, False),
        ("NaN CCL", {"bms_max_charge_current_a": float("nan")}, {}, False),
        ("infinite voltage", {"bms_voltage_v": float("inf")}, {}, False),
        ("zero voltage", {"bms_voltage_v": 0}, {}, False),
        ("old CCL", {"bms_charge_current_observed_at": NOW-timedelta(seconds=301)}, {}, False),
        ("old voltage", {"bms_voltage_observed_at": NOW-timedelta(seconds=301)}, {}, False),
        ("future voltage", {"bms_voltage_observed_at": NOW+timedelta(seconds=6)}, {}, False),
        ("missing system power", {}, {"system_power_kw": None}, False),
        ("zero system power", {}, {"system_power_kw": 0}, False),
        ("NaN system power", {}, {"system_power_kw": float("nan")}, False),
        ("infinite system power", {}, {"system_power_kw": float("inf")}, False),
        ("old plan", {}, {"observed_at": NOW-timedelta(seconds=301)}, False),
        ("capacity tolerance boundary", {"bms_voltage_v": 50, "bms_max_charge_current_a": 59}, {}, True),
        ("capacity outside tolerance", {"bms_voltage_v": 50, "bms_max_charge_current_a": 58.98}, {}, False),
    ]
    for name, source_changes, plan_changes, expected in mutations:
        source = replace(src, **source_changes)
        plan = replace(tariff, **plan_changes)
        actual = tariff_command_within_live_bms_limit(intent, source, tariff=plan, now=NOW)
        rendered, reason = rendered_readiness(source, plan)
        check(actual is expected, f"bridge: {name}")
        check(rendered is expected, f"actual YAML: {name}: {reason}")
        if name == "voltage relaxation":
            check(reason == "Gotowe", "diagnostic reason must match relaxed voltage readiness")
        if name == "insufficient positive CCL":
            check("mocy polecenia" in reason, "diagnostic reason must identify command power")
    for bad in (
        {"hardware_readback_supported": False}, {"machine_type_code": 2},
        {"machine_type_code": 1, "inverter_count": None},
        {"topology_generation_at": NOW-timedelta(seconds=181)},
    ):
        check(not tariff_command_within_live_bms_limit(intent, replace(src, **bad), tariff=tariff, now=NOW),
              f"invalid hardware/topology must reject: {bad}")
    check(tariff_command_within_live_bms_limit(
        intent, replace(src, battery_power_w=-4600, pv_power_w=5800, load_power_w=1200), tariff=tariff, now=NOW),
        "PV charging above 4304 is not a command-cap violation")


async def test_executing_and_waiting():
    controller, sent, _clock = await controller_with_command(
        30, resample_changes={"bms_max_charge_current_a": 50},
    )
    check(not sent, "pre-dispatch resampling rejects a newly insufficient BMS without writing")
    for power in (30, 100):
        controller, sent, clock = await controller_with_command(power, confirm=True)
        baseline = controller.record.transaction.command_snapshot
        for generation, voltage in enumerate((53.3, 52.9, 54, 53), 20):
            clock[0] += timedelta(seconds=2)
            source = replace(fixture.physical(clock[0], power=power, generation=generation),
                             bms_voltage_v=voltage, bms_max_charge_current_a=240)
            await controller.async_reconcile(fixture.frame(clock[0], source, controller.record, power=power,
                                                           bms_charge_power_limit_kw=12.96))
            check(controller.record.state is fixture.ActiveState.EXECUTING and len(sent) == 1,
                  f"{power/10} kW holds through V={voltage}, CCL=240")
        clock[0] += timedelta(seconds=1)
        source = replace(fixture.physical(clock[0], power=power, generation=30), bms_voltage_v=53)
        pending = fixture.frame(clock[0], source, controller.record, power=power,
                                result_current=False, recalculation_pending=True, control_data_ready=False)
        await controller.async_reconcile(pending)
        check(controller.record.state is fixture.ActiveState.EXECUTING and len(sent) == 1,
              "pending still compares the command, not old BMS capacity")
        check(controller.record.transaction.command_snapshot == baseline, "hold preserves restore baseline")
        clock[0] += timedelta(seconds=1)
        source = replace(fixture.physical(clock[0], power=power, generation=31),
                         bms_voltage_v=53, bms_max_charge_current_a=50)
        # A fully 'ready' helper must not hide loss of raw execution authority.
        await controller.async_reconcile(fixture.frame(clock[0], source, controller.record, power=power))
        check(controller.record.state is fixture.ActiveState.RESTORING and len(sent) == 2,
              "ordinary EXECUTING stops on a real positive-cap shortfall")

    for confirmed in (False, True):
        controller, sent, clock = await controller_with_command(100, confirm=confirmed)
        clock[0] += timedelta(seconds=1)
        source = replace(fixture.physical(clock[0], power=100, generation=40),
                         bms_voltage_v=53, bms_max_charge_current_a=100)
        pending = fixture.frame(clock[0], source, controller.record, power=30,
                                result_current=False, recalculation_pending=True, control_data_ready=False)
        check(not same_run_tariff_wait_authorized(
            controller.record.transaction.intent, pending.decision, pending.candidates,
            settings_from_execution_source(source), tariff=pending.tariff,
            execution_source=source, now=clock[0]),
            "new 3 kW plan cannot authorize already-sent 10 kW")
        await controller.async_reconcile(pending)
        check(controller.record.state is fixture.ActiveState.RESTORING and len(sent) == 2,
              f"{'EXECUTING' if confirmed else 'WAITING'} checks old sent command")

    controller, sent, clock = await controller_with_command(30, confirm=True)
    clock[0] += timedelta(seconds=1)
    source = replace(fixture.physical(clock[0], power=30, generation=50),
                     bms_voltage_v=53, bms_max_charge_current_a=100)
    await controller.async_reconcile(fixture.frame(clock[0], source, controller.record, power=100))
    check(len(sent) == 2 and sent[-1].ems_block.mode.value == 0,
          "a ready successor above CCL never dispatches its larger Mode4 command")

    controller, sent, clock = await controller_with_command(30, confirm=True)
    clock[0] += timedelta(seconds=1)
    source = replace(fixture.physical(clock[0], power=30, generation=60), bms_voltage_v=53)
    controller._frame_resampler = lambda: fixture.frame(
        clock[0], replace(source, bms_max_charge_current_a=100), controller.record, power=100,
    )
    await controller.async_reconcile(fixture.frame(clock[0], source, controller.record, power=100))
    check(len(sent) == 1, "pre-dispatch BMS loss prevents the 10 kW successor from being sent")
    check(controller.record.state is fixture.ActiveState.EXECUTING
          and controller.record.transaction.intent.command.ems_block.maximum_charge_power_percent_4304 == 30,
          "unsent successor cancellation resumes only the 3 kW predecessor that still fits CCL")


async def main():
    await test_cap_and_yaml()
    await test_executing_and_waiting()


package = yaml.safe_load((ROOT / "home_assistant/hoymiles_ems_scheduler.yaml").read_text(encoding="utf-8"))
READY_TEMPLATE = next(sensor for block in package["template"]
                      for sensor in block.get("binary_sensor", [])
                      if sensor.get("unique_id") == "hoymiles_tariff_control_data_ready")

if __name__ == "__main__":
    asyncio.run(main())
    print(f"Tariff BMS guard: PASS ({CHECKS} checks, actual YAML + production bridge/controller; offline only)")
