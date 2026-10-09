"""Virtual RCE solver/YAML/Supervisor/ESP-lease continuity for >240 seconds."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jinja2
import yaml

import test_rce_optimizer as optimizer_fixture
import test_supervisor_sensor_contract as h
from test_rce_plan_churn import production_rce_module
from test_supervisor_control_lease import FirmwareLeaseModel


NOW = h.NOW
YAML = h.ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"


class Domain:
    def __init__(self, states, prefix):
        self.states = states
        self.prefix = prefix

    def __getattr__(self, name):
        value = self.states.values.get(f"{self.prefix}.{name}")
        if value is None:
            return h.FakeState("unknown", reported=None)
        return value


class States:
    def __init__(self, registry):
        self.registry = registry

    def __call__(self, entity_id):
        value = self.registry.values.get(entity_id)
        return value.state if value is not None else "unknown"

    def __getattr__(self, name):
        return Domain(self.registry, name)


class RealHelperRenderer:
    """Render the actual YAML Jinja bodies against reported entity states."""

    IDS = (
        "hoymiles_ems_execution_ready",
        "hoymiles_ems_export_allowed",
        "hoymiles_rce_current_price",
        "hoymiles_rce_bms_safe_discharge_power",
        "hoymiles_rce_effective_discharge_power_percent",
        "hoymiles_rce_effective_minimum_soc",
        "hoymiles_rce_control_data_ready",
        "hoymiles_rce_reserve_ready",
        "hoymiles_rce_price_above_threshold",
    )

    def __init__(self, hass):
        self.hass = hass
        root = yaml.safe_load(YAML.read_text(encoding="utf-8"))
        self.templates = {}
        for group in root["template"]:
            for domain in ("sensor", "binary_sensor"):
                for item in group.get(domain, ()):
                    uid = item.get("unique_id")
                    if uid in self.IDS:
                        self.templates[uid] = (domain, item)
        assert set(self.templates) == set(self.IDS)
        states = States(hass.states)

        def state_attr(entity_id, name):
            value = hass.states.values.get(entity_id)
            return None if value is None else value.attributes.get(name)

        def is_number(value):
            if value is None or isinstance(value, bool):
                return False
            try:
                number = float(value)
            except (TypeError, ValueError):
                return False
            return number == number and abs(number) != float("inf")

        def as_timestamp(value, default=0):
            if isinstance(value, datetime):
                return value.timestamp()
            return default

        self.env = jinja2.Environment(autoescape=False)
        self.env.globals.update(
            states=states, state_attr=state_attr,
            is_state=hass.states.is_state, is_number=is_number,
            as_timestamp=as_timestamp, now=lambda: h.CLOCK["now"],
            true=True, false=False,
        )

    def publish(self, uid, at):
        domain, item = self.templates[uid]
        if "availability" in item:
            available = self.env.from_string(item["availability"]).render().strip()
            if available != "True":
                value = "unavailable"
            else:
                value = self.env.from_string(item["state"]).render().strip()
        else:
            value = self.env.from_string(item["state"]).render().strip()
        if domain == "binary_sensor" and value in {"True", "False"}:
            value = "on" if value == "True" else "off"
        entity_id = f"{domain}.{uid}"
        self.hass.fire_state(entity_id, h.FakeState(value, reported=at))
        return value

    def publish_all(self, at):
        return {uid: self.publish(uid, at) for uid in self.IDS}


async def solver_probe(case="bms"):
    hass, entry, runtime, supervisor = h.environment()
    module = production_rce_module()
    module.Store = lambda *_args, **_kwargs: object()
    module.dt_util.now = lambda: h.CLOCK["now"]
    module._live_forecast_gcf_optimizer_signature = lambda *_args: (
        False, "disabled", None, None, None, None, None,
    )

    def eid(key):
        return h._source_entity_id(h.SENSOR._SOURCE_BY_KEY[key], entry.entry_id)

    plan_id = eid("rce_plan")
    source = module.HoymilesRCEOptimizerSensor(hass, entry, runtime)
    renderer = RealHelperRenderer(hass)
    prices = [
        optimizer_fixture.RCE.PriceSlot(
            start=NOW + timedelta(minutes=30 * index), price_pln_kwh=2.0
        )
        for index in range(4)
    ]
    solves = []

    def provider():
        now = h.CLOCK["now"]
        requested = hass.states.values.get(
            "input_number.hoymiles_rce_requested_discharge_power"
        )
        try:
            requested_percent = float(requested.state)
        except (AttributeError, TypeError, ValueError):
            requested_percent = 100.0
        settings = optimizer_fixture.base_input(
            now=now, price_slots=prices, battery_soc_percent=90.0,
            battery_capacity_kwh=(100.0 if case in {"retarget", "natural"} else 20.0),
            discharge_power_percent=requested_percent,
            current_load_power_kw=0.0, current_pv_power_kw=0.0,
        )
        metadata = {
            "rce_today_data_fresh": True,
            "rce_today_age_seconds": 0.0,
            "forecast_today_data_fresh": True,
            "forecast_today_age_seconds": 0.0,
            "gcf_execution_data_fresh": True,
            "soc_data_fresh": True,
            "soc_data_age_seconds": 0.0,
            "current_price_pln_kwh": 2.0,
        }
        return settings, metadata

    source._optimizer_input = provider
    async def executor_job(func, *args):
        solves.append(h.CLOCK["now"])
        return func(*args)
    hass.async_add_executor_job = executor_job
    def publish_plan():
        plan = h.FakeState(
            "ready" if source._attributes.get("status_code") == "ready" else "unknown",
            dict(source._attributes), h.CLOCK["now"],
        )
        hass.states.values[plan_id] = plan
        renderer.publish_all(h.CLOCK["now"])
        hass.fire_state(plan_id, plan)

    source.async_write_ha_state = publish_plan
    await source._recalculate_and_write()
    assert source._attributes["result_current"] is True
    assert source._attributes.get("current_slot_start_eligible") is True, source._attributes
    assert len(solves) == 1
    return hass, entry, runtime, supervisor, source, module, renderer, solves


async def main(case="bms"):
    h.CLOCK["now"] = NOW
    hass, entry, runtime, supervisor, source, module, renderer, solves = await solver_probe(case)
    assert source._attributes["current_slot_execution_power_percent"] > 0
    basic = {
        "sensor.hoymiles_hit_ems_mode_readback_code": 0,
        "sensor.hoymiles_hit_ems_control_readback_generation": 1,
        "sensor.hoymiles_hit_parallel_topology_readback_generation": 1,
        "sensor.hoymiles_hit_esp_uptime": 1000,
        "sensor.hoymiles_hit_ems_verified_hardware_readback_supported": 1,
        "sensor.hoymiles_hit_overview_battery_soc": 90,
        "sensor.hoymiles_hit_maximum_discharge_current": 500,
        "sensor.hoymiles_hit_battery_voltage_bms": 51.2,
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback": 20,
        "sensor.hoymiles_hit_ems_maximum_discharge_power_readback": 50,
        "sensor.hoymiles_hit_gcf_enable_readback_code": 0,
        "sensor.hoymiles_hit_gcf_maximum_export_power_readback": 100,
        "sensor.hoymiles_hit_gcf_control_readback_generation": 1,
        "input_number.hoymiles_rce_requested_discharge_power": 100,
        "input_boolean.hoymiles_rce_dynamic_soc_enabled": "off",
    }
    for entity_id, value in basic.items():
        hass.states.values[entity_id] = h.FakeState(str(value), reported=NOW)
    rendered = renderer.publish_all(NOW)
    assert rendered["hoymiles_ems_execution_ready"] == "on", rendered
    assert rendered["hoymiles_ems_export_allowed"] == "on", rendered
    assert rendered["hoymiles_rce_control_data_ready"] == "on", (
        rendered,
        renderer.env.from_string(renderer.templates[
            "hoymiles_rce_control_data_ready"
        ][1]["attributes"]["reason"]).render().strip(),
    )
    assert rendered["hoymiles_rce_reserve_ready"] == "on", rendered
    print("PASS production listener/solver startup", len(solves))
    print("PASS actual YAML/Jinja helper publication", rendered)

    def eid(key):
        return h._source_entity_id(h.SENSOR._SOURCE_BY_KEY[key], entry.entry_id)

    def store(key, value, at=NOW, attributes=None):
        hass.states.values[eid(key)] = h.FakeState(
            str(value), attributes, reported=at
        )

    for key, value in {
        "supervisor_mode": "Active",
        "allow_rce": "on",
        "rce_enabled": "on",
        "rce_active": "off",
        "sale_block_active": "off",
        "rce_price_above_threshold": "on",
        "rce_latched_minimum_soc": 20,
        "battery_soc": 90,
        "bms_voltage": 51.2,
        "bms_max_charge_current": 500,
        "bms_max_discharge_current": 500,
        "ems_mode_readback": 0,
        "ems_generation": 1,
        "self_use_soc_readback": 20,
        "backup_soc_readback": 80,
        "charge_soc_readback": 80,
        "charge_power_ems_readback": 40,
        "discharge_soc_readback": 20,
        "discharge_power_readback": 50,
        "gcf_enable_readback": 0,
        "gcf_export_limit_readback": 100,
        "gcf_generation": 1,
        "machine_type": 0,
        "inverter_count": 1,
        "topology_generation": 1,
        "charge_power_readback": 80,
        "battery_charge_generation": 1,
        "grid_power": 0,
        "battery_power": 0,
        "load_power": 0,
    }.items():
        store(key, value)
    # The actual renderer is authoritative for all named readiness helpers.
    renderer.publish_all(NOW)
    store(
        "rce_latched_slot_end", "ignored", attributes={
            "timestamp": (
                min(source._result.current_slot_end, NOW + timedelta(minutes=30))
                if case in {"retarget", "natural"} else source._result.current_slot_end
            ).timestamp()
        },
    )
    services = LeaseServices(
        hass, entry, runtime,
        restore_refusals=(1 if case == "bms_notqueued" else 0),
    )
    hass.services = services
    loop = asyncio.get_running_loop()
    real_loop_time = loop.time
    loop.time = lambda: 1000.0 + (h.CLOCK["now"] - NOW).total_seconds()

    async_callbacks = []

    def virtual_call_later(delay, callback):
        def invoke(at):
            result = callback(at)
            if inspect.isawaitable(result):
                async_callbacks.append(asyncio.create_task(result))

        handle = h.FakeHandle(
            invoke, h.CLOCK["now"] + timedelta(seconds=delay)
        )
        hass.delay_handles.append(handle)
        return handle.cancel

    hass.call_later = virtual_call_later
    hass.async_create_task = lambda coro, name=None: asyncio.create_task(
        coro, name=name
    )
    await supervisor.add_to_platform_finish()
    controller = supervisor._controller
    assert controller is not None
    controller._clock = lambda: h.CLOCK["now"]
    await drain(supervisor)
    assert services.arms == 1, (
        controller.record.state, controller.record.reason,
        supervisor._decision_attributes,
    )
    original_tx = controller.record.transaction.transaction_id
    original_deadline = controller.record.transaction.deadline
    original_hard = services.oracle.deadline
    if case in {"retarget", "natural"}:
        hass.track_states(
            tuple(module.RCE_EVENT_DRIVEN_ENTITIES), source._async_input_changed
        )
    print("PASS production Supervisor leased start", controller.record.state)
    async def due():
        for _attempt in range(30):
            handles = [
                handle for handle in (*hass.active_delays(), *hass.active_points())
                if isinstance(handle.when, datetime)
                and handle.when <= h.CLOCK["now"]
            ]
            if not handles:
                break
            for handle in sorted(handles, key=lambda item: item.when):
                handle.run()
            if async_callbacks:
                current = tuple(async_callbacks)
                async_callbacks.clear()
                await asyncio.gather(*current)
            await drain(supervisor)
            await asyncio.sleep(0)
        else:
            raise AssertionError("virtual callbacks did not settle")

    def report(key, value):
        hass.fire_report(eid(key), h.FakeState(
            str(value), reported=h.CLOCK["now"]
        ))

    def ems(generation, block=None):
        block = services.oracle.active if block is None else block
        assert block is not None
        hass.states.values["sensor.hoymiles_hit_esp_uptime"] = h.FakeState(
            str(1000 + generation), reported=h.CLOCK["now"]
        )
        for key, value in (
            ("ems_mode_readback", int(block[0])),
            ("self_use_soc_readback", block[1]),
            ("backup_soc_readback", block[2]),
            ("charge_soc_readback", block[3]),
            ("charge_power_ems_readback", block[4]),
            ("discharge_soc_readback", block[5]),
            ("discharge_power_readback", block[6]),
            ("ems_generation", generation),
        ):
            report(key, value)
        for entity_id, value in (
            ("sensor.hoymiles_hit_ems_mode_readback_code", int(block[0])),
            ("sensor.hoymiles_hit_ems_control_readback_generation", generation),
            ("sensor.hoymiles_hit_ems_force_discharge_soc_readback", block[5]),
            ("sensor.hoymiles_hit_ems_maximum_discharge_power_readback", block[6]),
        ):
            hass.states.values[entity_id] = h.FakeState(
                str(value), reported=h.CLOCK["now"]
            )

    def battery(active=True):
        for key, value in (
            ("battery_soc", 90), ("bms_voltage", 51.2),
            ("bms_max_charge_current", 500),
            ("bms_max_discharge_current", 500),
            ("grid_power", 9000 if active else 0),
            ("battery_power", 10000 if active else 0),
            ("load_power", 1000),
        ):
            hass.fire_state(eid(key), h.FakeState(
                str(value), reported=h.CLOCK["now"]
            ))
        for entity_id, value in (
            ("sensor.hoymiles_hit_overview_battery_soc", 90),
            ("sensor.hoymiles_hit_maximum_discharge_current", 500),
            ("sensor.hoymiles_hit_battery_voltage_bms", 51.2),
        ):
            hass.states.values[entity_id] = h.FakeState(
                str(value), reported=h.CLOCK["now"]
            )

    def settings(generation):
        for key, value in (
            ("gcf_enable_readback", 0),
            ("gcf_export_limit_readback", 100),
            ("gcf_generation", generation),
            ("machine_type", 0), ("inverter_count", 1),
            ("topology_generation", generation),
            ("charge_power_readback", 80),
            ("battery_charge_generation", generation),
        ):
            report(key, value)
        for entity_id in (
            "sensor.hoymiles_hit_parallel_topology_readback_generation",
            "sensor.hoymiles_hit_gcf_control_readback_generation",
        ):
            hass.states.values[entity_id] = h.FakeState(
                str(generation), reported=h.CLOCK["now"]
            )

    generation = 1
    settings_generation = 1
    try:
        duration = 7200 if case == "natural" else 260 if case in {"bms", "bms_notqueued", "retarget"} else 30
        seconds = list(range(1, min(duration, 260) + 1))
        if case == "natural":
            seconds.extend(range(265, duration + 1, 5))
        previous_second = 0
        first_confirmed_record = None
        for second in seconds:
            h.CLOCK["now"] = NOW + timedelta(seconds=second)
            services.oracle.advance(float(second - previous_second))
            previous_second = second
            await due()
            if second == 1 or second % 5 == 1 or case == "natural" and second > 260:
                generation += 1
                ems(generation)
                services.oracle.observe(services.oracle.active)
            if second == 1 or second % 13 == 1 or case == "natural" and second > 260:
                battery()
                if second == 1:
                    hass.fire_state(
                        eid("rce_active"), h.FakeState(
                            "on", reported=h.CLOCK["now"]
                        ),
                    )
            if second % 20 == 7 or case == "natural" and second > 260:
                settings_generation += 1
                settings(settings_generation)
            if second in {120, 240} or case == "natural" and second > 260 and second % 120 == 0:
                before = source._full_plan_solver_calls
                await source._async_timer(h.CLOCK["now"])
                assert source._full_plan_solver_calls == before + 1
                assert source._attributes["result_current"] is True
            if second in {60, 180}:
                old = h.FakeState("15", {
                    "statistics_energy_kwh": 15.0,
                }, h.CLOCK["now"])
                new = h.FakeState("16", {
                    "statistics_energy_kwh": 16.0,
                }, h.CLOCK["now"])
                before = source._input_revision.value
                diagnostic_id = "sensor.hoymiles_load_average_4_days"
                if diagnostic_id in module.RCE_EVENT_DRIVEN_ENTITIES:
                    source._async_input_changed(SimpleNamespace(data={
                        "entity_id": diagnostic_id,
                        "old_state": old, "new_state": new,
                    }))
                assert source._input_revision.value == before
            if case in {"retarget", "natural"} and second == 160:
                hass.fire_state(
                    "input_number.hoymiles_rce_requested_discharge_power",
                    h.FakeState("50", reported=h.CLOCK["now"]),
                )
                renderer.publish_all(h.CLOCK["now"])
                await due()
            if case in {"retarget", "natural"} and second in {80, 200}:
                enabled = second == 80
                for key in ("allow_rcm", "rcm_enabled"):
                    hass.fire_state(eid(key), h.FakeState(
                        "on" if enabled else "off", reported=h.CLOCK["now"]
                    ))
                hass.fire_state(eid("rcm_active"), h.FakeState(
                    "off", reported=h.CLOCK["now"]
                ))
                if enabled:
                    rcm = h._plan_attributes("rcm_plan")
                    rcm.update(action="monitor", live_emergency=False)
                    hass.fire_state(eid("rcm_plan"), h.FakeState(
                        "monitoring", rcm, h.CLOCK["now"]
                    ))
                await due()
            if second % 5 == 1 or second % 13 == 1 or second % 20 == 7 or second in {120, 240} or case == "natural" and second > 260:
                rendered = renderer.publish_all(h.CLOCK["now"])
                if (case not in {"retarget", "natural"} or second < 160 or second > 165) and not (
                    case == "natural" and second >= 7200
                ):
                    assert rendered["hoymiles_rce_control_data_ready"] == "on", (
                        second, rendered,
                        renderer.env.from_string(renderer.templates[
                            "hoymiles_rce_control_data_ready"
                        ][1]["attributes"]["reason"]).render().strip(),
                    )
                await due()
            await drain(supervisor)
            if case == "natural" and second >= 7200:
                break
            assert (
                controller.record.state is h.SENSOR.ActiveState.EXECUTING
                or (
                    case in {"retarget", "natural"} and 161 <= second <= 172
                    # The periodic shorter plan must now apply its 85% target,
                    # rather than silently retaining the old 100% command.
                    or case in {"bms", "bms_notqueued"}
                    and (120 <= second <= 132 or 240 <= second <= 252)
                )
                and controller.record.state is h.SENSOR.ActiveState.RETARGETING
            ), (
                second, controller.record.state, controller.record.reason,
                supervisor._control_lease_gate,
                services.arms, services.oracle.state,
            )
            assert services.oracle.state == "confirmed" or (
                controller.record.state is h.SENSOR.ActiveState.RETARGETING
                and services.oracle.state == "pending"
                and controller.record.transaction.command_sent_at is not None
                and 0 <= (h.CLOCK["now"] - controller.record.transaction.command_sent_at).total_seconds() <= 12
            ), (
                second, services.oracle.state, services.renewals[-3:]
            )
            assert controller.record.transaction.transaction_id == original_tx
            assert controller.record.transaction.deadline == original_deadline
            assert not services.restore_calls
            if case in {"natural", "bms_notqueued", "export", "master_stop"} and second == 1:
                first_confirmed_record = controller.record
        if case == "natural":
            assert len(solves) == 62
            assert services.arms == 2
            assert all(accepted for _, _, accepted in services.renewals)
            assert controller.record.transaction.transaction_id == original_tx
            assert controller.record.transaction.deadline == original_deadline
            assert services.oracle.deadline == original_hard
            assert controller.record.state is h.SENSOR.ActiveState.RESTORING
            assert controller.record.owner is h.SENSOR.ExecutionOwner.RCE
            assert services.oracle.state == "restoring"
            assert controller.record.transaction.restore_sent_at is not None
            h.CLOCK["now"] = NOW + timedelta(seconds=7201)
            services.oracle.advance(1.0)
            services.oracle.observe(services.oracle.fallback)
            generation += 1
            ems(generation, services.oracle.fallback)
            battery(active=False)
            settings_generation += 1
            settings(settings_generation)
            hass.fire_state(eid("rce_active"), h.FakeState(
                "off", reported=h.CLOCK["now"]
            ))
            renderer.publish_all(h.CLOCK["now"])
            await due()
            await drain(supervisor)
            assert controller.record.state is h.SENSOR.ActiveState.IDLE, (
                controller.record.state, controller.record.reason,
                controller.record.transaction,
            )
            assert controller.record.owner is h.SENSOR.ExecutionOwner.NONE
            assert controller.record.last_transaction.transaction_id == original_tx
            health = supervisor.extra_state_attributes["execution_health"]
            assert health["control_status"] == "healthy", health
            from custom_components.hoymiles_hit_modbus.ems_notifications import (
                HoymilesEmsNotificationManager, RangeNotificationModel,
            )
            manager = object.__new__(HoymilesEmsNotificationManager)
            manager.hass = SimpleNamespace(states=SimpleNamespace(
                is_state=lambda *_args: False
            ))
            model = RangeNotificationModel()
            manager._model = model
            def observation_frame(at):
                return SimpleNamespace(
                    now=at,
                    rce=SimpleNamespace(current_run_end=original_deadline),
                    tariff=SimpleNamespace(current_grid_charge_run_end=None),
                    rcm=SimpleNamespace(
                        latched_pre_discharge_deadline=None,
                        pre_discharge_deadline=None,
                    ),
                )
            model.observe(manager._observation(
                h.SENSOR.ExecutorRecord(), observation_frame(NOW)
            ))
            assert first_confirmed_record is not None
            started = model.observe(manager._observation(
                first_confirmed_record,
                observation_frame(NOW + timedelta(seconds=1)),
            ))
            assert len(started) == 1 and started[0].kind == "start", started
            terminal = manager._observation(
                controller.record, observation_frame(h.CLOCK["now"])
            )
            assert not terminal.interrupted, terminal
            model.observe(terminal)
            finished = model.observe(replace(
                terminal, observed_at=h.CLOCK["now"] + timedelta(minutes=3)
            ))
            assert len(finished) == 1 and finished[0].outcome == "completed", finished
            print("PASS X01 slot boundary, planned finish and exact fallback", len(services.renewals))
            return
        assert len(solves) == (4 if case == "retarget" else 3 if case in {"bms", "bms_notqueued"} else 1)
        assert len(services.renewals) >= (10 if case in {"bms", "bms_notqueued", "retarget"} else 1)
        assert all(accepted for _, _, accepted in services.renewals)
        assert controller.record.transaction.transaction_id == original_tx
        assert controller.record.transaction.deadline == original_deadline
        assert services.oracle.deadline == original_hard
        expected_arms = 3 if case in {"bms", "bms_notqueued"} else 2 if case in {"retarget", "natural"} else 1
        assert services.arms == expected_arms, (
            case, services.arms, controller.record.state, controller.record.reason,
            services.arm_requests,
        )
        if case == "retarget":
            assert [request["command_generation"] for request in services.arm_requests] == [1, 2]
            assert all(request["transaction_id"] == original_tx for request in services.arm_requests)
            print("PASS L03 retarget preserves tx and hard deadline", len(services.renewals))
            return
        if case in {"bms", "bms_notqueued"}:
            print("PASS L02 260s solver/helper/listener/renew/ESP TTL", len(services.renewals))
        h.CLOCK["now"] = NOW + timedelta(seconds=duration + 1)
        if case in {"bms", "bms_notqueued"}:
            # A real lower DCL revokes the previously sent 10 kW command.
            hass.fire_report(eid("bms_max_discharge_current"), h.FakeState(
                "10", reported=h.CLOCK["now"]
            ))
            hass.states.values["sensor.hoymiles_hit_maximum_discharge_current"] = (
                h.FakeState("10", reported=h.CLOCK["now"])
            )
        elif case == "export":
            hass.fire_report(eid("gcf_enable_readback"), h.FakeState(
                "1", reported=h.CLOCK["now"]
            ))
            hass.fire_report(eid("gcf_export_limit_readback"), h.FakeState(
                "0", reported=h.CLOCK["now"]
            ))
            hass.states.values["sensor.hoymiles_hit_gcf_enable_readback_code"] = (
                h.FakeState("1", reported=h.CLOCK["now"])
            )
            hass.states.values["sensor.hoymiles_hit_gcf_maximum_export_power_readback"] = (
                h.FakeState("0", reported=h.CLOCK["now"])
            )
        elif case == "master_stop":
            supervisor.request_master_stop()
        else:
            raise AssertionError(case)
        rendered = renderer.publish_all(h.CLOCK["now"])
        if case in {"bms", "bms_notqueued"}:
            assert rendered["hoymiles_rce_control_data_ready"] == "off", rendered
        if case == "export":
            assert rendered["hoymiles_ems_export_allowed"] == "off", rendered
        h.CLOCK["now"] = NOW + timedelta(seconds=duration + 4)
        await due()
        await drain(supervisor)
        renewals_before = len(services.renewals)
        evidence = supervisor._control_lease_renewal_evidence()
        assert evidence is None, (
            case, rendered, evidence, supervisor._control_lease_gate,
            supervisor._latest_active_frame.execution,
        )
        assert supervisor._control_lease_gate["reason"] in {
            "rce_bms_limit", "execution_not_confirmed", "authorization_mismatch",
            "export_direction", "master_stop"
        }, supervisor._control_lease_gate
        await due()
        assert len(services.renewals) == renewals_before
        if case == "bms_notqueued":
            tx = controller.record.transaction
            assert controller.record.state is h.SENSOR.ActiveState.STOPPING
            assert tx.restore_not_queued_count == 1
            assert tx.restore_attempts == 0
            assert tx.restore_sent_at is None
            assert len(services.restore_calls) == 1
            h.CLOCK["now"] = NOW + timedelta(seconds=265)
            generation += 1
            ems(generation)
            battery()
            settings_generation += 1
            settings(settings_generation)
            renderer.publish_all(h.CLOCK["now"])
            h.CLOCK["now"] = NOW + timedelta(seconds=267)
            await due()
            await drain(supervisor)
            tx = controller.record.transaction
            assert controller.record.state is h.SENSOR.ActiveState.RESTORING, (
                controller.record.state, controller.record.reason, tx,
            )
            assert tx.restore_not_queued_count == 1
            assert tx.restore_attempts == 1
            assert tx.restore_sent_at is not None
            assert len(services.restore_calls) == 2
            h.CLOCK["now"] = NOW + timedelta(seconds=268)
            services.oracle.advance(8.0)
            services.oracle.observe(services.oracle.fallback)
            generation += 1
            ems(generation, services.oracle.fallback)
            battery(active=False)
            settings_generation += 1
            settings(settings_generation)
            hass.fire_state(eid("rce_active"), h.FakeState(
                "off", reported=h.CLOCK["now"]
            ))
            renderer.publish_all(h.CLOCK["now"])
            await due()
            await drain(supervisor)
            assert controller.record.state is h.SENSOR.ActiveState.IDLE
            assert controller.record.owner is h.SENSOR.ExecutionOwner.NONE
            assert controller.record.last_transaction.transaction_id == original_tx
            assert controller.record.last_transaction.restore_not_queued_count == 1
            assert controller.record.last_transaction.interruption_reason is h.SENSOR.ExecutionReason.BMS_UNAVAILABLE
            from custom_components.hoymiles_hit_modbus.ems_notifications import (
                HoymilesEmsNotificationManager, RangeNotificationModel,
            )
            manager = object.__new__(HoymilesEmsNotificationManager)
            manager.hass = SimpleNamespace(states=SimpleNamespace(
                is_state=lambda *_args: False
            ))
            model = RangeNotificationModel()
            manager._model = model
            def notification_frame(at):
                return SimpleNamespace(
                    now=at,
                    rce=SimpleNamespace(current_run_end=original_deadline),
                    tariff=SimpleNamespace(current_grid_charge_run_end=None),
                    rcm=SimpleNamespace(
                        latched_pre_discharge_deadline=None,
                        pre_discharge_deadline=None,
                    ),
                )
            model.observe(manager._observation(
                h.SENSOR.ExecutorRecord(), notification_frame(NOW)
            ))
            assert first_confirmed_record is not None
            starts = model.observe(manager._observation(
                first_confirmed_record,
                notification_frame(NOW + timedelta(seconds=1)),
            ))
            assert len(starts) == 1 and starts[0].kind == "start", starts
            terminal = manager._observation(
                controller.record, notification_frame(h.CLOCK["now"])
            )
            assert terminal.interrupted and terminal.restoration_confirmed, terminal
            events = model.observe(terminal)
            assert len(events) == 1 and events[0].outcome == "interrupted", events
            print("PASS X02 BMS refusal, fresh FC03 retry and interrupted restore")
            return
        if case in {"export", "master_stop"}:
            assert controller.record.state is h.SENSOR.ActiveState.RESTORING
            assert controller.record.owner is h.SENSOR.ExecutionOwner.RCE
            assert len(services.restore_calls) == 1
            h.CLOCK["now"] = NOW + timedelta(seconds=duration + 5)
            services.oracle.advance(5.0)
            services.oracle.observe(services.oracle.fallback)
            generation += 1
            ems(generation, services.oracle.fallback)
            battery(active=False)
            settings_generation += 1
            settings(settings_generation)
            hass.fire_state(eid("rce_active"), h.FakeState(
                "off", reported=h.CLOCK["now"]
            ))
            renderer.publish_all(h.CLOCK["now"])
            await due()
            await drain(supervisor)
            assert controller.record.state is h.SENSOR.ActiveState.IDLE
            assert controller.record.owner is h.SENSOR.ExecutionOwner.NONE
            assert controller.record.last_transaction.transaction_id == original_tx
            from custom_components.hoymiles_hit_modbus.ems_notifications import (
                HoymilesEmsNotificationManager, RangeNotificationModel,
            )
            manager = object.__new__(HoymilesEmsNotificationManager)
            manager.hass = SimpleNamespace(states=SimpleNamespace(
                is_state=lambda *_args: False
            ))
            model = RangeNotificationModel()
            manager._model = model
            def notification_frame(at):
                return SimpleNamespace(
                    now=at,
                    rce=SimpleNamespace(current_run_end=original_deadline),
                    tariff=SimpleNamespace(current_grid_charge_run_end=None),
                    rcm=SimpleNamespace(
                        latched_pre_discharge_deadline=None,
                        pre_discharge_deadline=None,
                    ),
                )
            model.observe(manager._observation(
                h.SENSOR.ExecutorRecord(), notification_frame(NOW)
            ))
            assert first_confirmed_record is not None
            starts = model.observe(manager._observation(
                first_confirmed_record,
                notification_frame(NOW + timedelta(seconds=1)),
            ))
            assert len(starts) == 1 and starts[0].kind == "start", starts
            terminal = manager._observation(
                controller.record, notification_frame(h.CLOCK["now"])
            )
            assert terminal.interrupted and terminal.restoration_confirmed, terminal
            ended = model.observe(terminal)
            assert len(ended) == 1 and ended[0].outcome == "interrupted", ended
            print("PASS X02", case, "safe restore and interrupted outcome")
            return
        print("PASS L04", case, "revokes renewal on same production path")
    finally:
        loop.time = real_loop_time


async def drain(sensor):
    for _ in range(150):
        await asyncio.sleep(0)
        if sensor._controller_task is None and sensor._pending_active_frame is None:
            return
    raise AssertionError("Supervisor callback queue did not drain")


class LeaseServices:
    def __init__(self, hass, entry, runtime, *, restore_refusals=0):
        source_entry = SimpleNamespace(
            entry_id="esphome-source", domain="esphome",
            data={"device_name": "source-node"},
        )
        runtime.source_device.config_entry_id = source_entry.entry_id
        hass.config_entries = SimpleNamespace(
            async_get_entry=lambda entry_id: (
                source_entry if entry_id == source_entry.entry_id else None
            )
        )
        self.names = {
            "source_node_ems_supervisor_control_lease_challenge",
            "source_node_ems_supervisor_write_complete_block_leased",
            "source_node_ems_supervisor_renew_control_lease",
            "source_node_ems_supervisor_control_lease_terminal_proof",
        }
        self.names.update(
            f"source_node_{family.value}"
            for family in h.SENSOR.AtomicWriteFamily
        )
        self.oracle = FirmwareLeaseModel()
        self.arms = 0
        self.arm_requests = []
        self.renewals = []
        self.restore_refusals = restore_refusals
        self.restore_calls = []

    def async_services(self):
        return {"esphome": {name: object() for name in self.names}}

    async def async_call(self, _domain, service, data, **kwargs):
        if not kwargs.get("return_response"):
            return None
        if service.endswith("ems_supervisor_control_lease_challenge"):
            return {"schema_version": 1, "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20, "nonce": "00000001"}
        if service.endswith("ems_supervisor_write_complete_block_leased"):
            self.arms += 1
            self.arm_requests.append(dict(data))
            block = (
                float(data["mode_code"]), float(data["self_use_soc"]),
                float(data["backup_soc"]), float(data["force_charge_soc"]),
                float(data["maximum_charge_power"]),
                float(data["force_discharge_soc"]),
                float(data["maximum_discharge_power"]),
            )
            if self.arms == 1:
                self.oracle.fallback = (0.0, 20.0, 80.0, 80.0, 40.0, 20.0, 50.0)
                self.oracle.physical = self.oracle.fallback
            accepted = self.oracle.arm(
                block, transaction=data["transaction_id"],
                generation=data["command_generation"],
                hard_seconds=data["hard_deadline_seconds"],
            )
            return {
                "schema_version": 1, "protocol_version": 2, "soft_remaining_ms": int((self.oracle.expiry-self.oracle.now)*1000),
                "accepted": accepted,
                "reason": "accepted" if accepted else "lease_already_active",
                "renew_nonce": "00000002",
            }
        if service.endswith("ems_supervisor_renew_control_lease"):
            accepted = self.oracle.renew(
                authorized=True, sequence=data["sequence"], authorization_seconds=data["authorization_seconds"]
            )
            self.renewals.append((self.oracle.now, data["sequence"], accepted))
            return {
                "schema_version": 1, "protocol_version": 2, "soft_remaining_ms": int((self.oracle.expiry-self.oracle.now)*1000),
                "accepted": accepted,
                "reason": "renewed" if accepted else "lease_not_confirmed",
                "renew_nonce": f"{data['sequence'] + 2:08x}",
            }
        if service.endswith("ems_supervisor_write_complete_block"):
            self.restore_calls.append(dict(data))
            if len(self.restore_calls) <= self.restore_refusals:
                return {
                    "schema_version": 1, "accepted": False,
                    "reason": "stale_snapshot_generation",
                }
        return {"schema_version": 1, "accepted": True, "reason": "accepted"}


if __name__ == "__main__":
    async def all_cases():
        for name in ("bms", "retarget", "natural", "bms_notqueued", "export", "master_stop"):
            await main(name)

    asyncio.run(all_cases())
