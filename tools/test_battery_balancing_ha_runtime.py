"""Exact Home Assistant 2026.8.2 battery-balancing runtime regressions.

This suite loads the production scheduler objects into an isolated temporary
Home Assistant instance.  The only substituted services are the three already
verified EMS helpers and the phone provider; no network or inverter is used.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib.metadata import version
import logging
from pathlib import Path
import tempfile
from typing import Any
from unittest.mock import patch

import yaml

from homeassistant import config_entries, loader
from homeassistant.const import EVENT_CALL_SERVICE
from homeassistant.core import Event, HomeAssistant, ServiceCall, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import trigger
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
EXPECTED_HOME_ASSISTANT = "2026.8.2"

PHYSICAL_SERVICES = (
    "hoymiles_verified_set_ems_maximum_charge_power",
    "hoymiles_verified_set_ems_force_charge_soc",
    "hoymiles_verified_set_ems_mode",
)

HARD_STOP_AUTOMATIONS = (
    "hoymiles_battery_balancing_hard_stop_capture",
    "hoymiles_battery_balancing_off_grid_hard_stop_capture",
    "hoymiles_battery_balancing_p95_hard_stop_capture",
    "hoymiles_battery_balancing_p90_hard_stop_capture",
    "hoymiles_battery_balancing_p80_hard_stop_capture",
    "hoymiles_battery_balancing_p75_hard_stop_capture",
    "hoymiles_battery_balancing_p60_hard_stop_capture",
)

BALANCING_TEXTS = {
    "hoymiles_battery_balancing_phase",
    "hoymiles_battery_balancing_lifecycle",
    "hoymiles_battery_balancing_timing",
    "hoymiles_battery_balancing_abort_request",
    "hoymiles_battery_balancing_notification_outbox",
    "hoymiles_tariff_active_action",
    "hoymiles_ems_push_notify_target",
}

BALANCING_NUMBERS = {
    "hoymiles_battery_balancing_cycle_sequence",
    "hoymiles_battery_balancing_hold_hours",
    "hoymiles_battery_balancing_interval_days",
}

OWNER_BOOLEANS = {
    "hoymiles_battery_balancing_active",
    "hoymiles_battery_balancing_enabled",
    "hoymiles_ems_push_notifications_enabled",
    "hoymiles_discharge_cycle_active",
    "hoymiles_charge_cycle_active",
    "hoymiles_rce_discharge_active",
    "hoymiles_tariff_charge_active",
    "hoymiles_rcm_active",
    "hoymiles_rcm_pre_discharge_active",
    "hoymiles_rcm_export_control_active",
    "hoymiles_rce_discharge_enabled",
    "hoymiles_tariff_charge_enabled",
    "hoymiles_rcm_enabled",
    "hoymiles_rcm_shadow_mode",
}

TEMPLATE_IDS = {
    "hoymiles_battery_balancing_transaction",
    "hoymiles_battery_balancing_timing_transaction",
    "hoymiles_battery_balancing_abort_request",
    "hoymiles_battery_balancing_notification_outbox",
    "hoymiles_ems_control_owner",
    "hoymiles_ems_control_conflict",
    "hoymiles_battery_balancing_apply_authorized",
    "hoymiles_battery_balancing_restore_authorized",
}


async def test_all_jinja_templates_compile(package: dict[str, Any]) -> int:
    """Compile every production scheduler template with exact-version HA."""

    def iter_templates(value: object):
        if isinstance(value, dict):
            for child in value.values():
                yield from iter_templates(child)
        elif isinstance(value, list):
            for child in value:
                yield from iter_templates(child)
        elif isinstance(value, str) and ("{{" in value or "{%" in value):
            yield value

    with tempfile.TemporaryDirectory(prefix="hoymiles-jinja-ha-2026.8.2-") as config:
        hass = HomeAssistant(config)
        templates = list(iter_templates(package))
        assert templates, "scheduler contains no Jinja templates"
        for source in templates:
            Template(source, hass).ensure_valid()
    return len(templates)


async def test_soft_gap_exact_boundaries(package: dict[str, Any]) -> int:
    """Render the exact production deadline conditions at required edges."""

    def iter_strings(value: object):
        if isinstance(value, dict):
            for child in value.values():
                yield from iter_strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from iter_strings(child)
        elif isinstance(value, str):
            yield value

    strings = list(iter_strings(package))
    backward_matches = [
        source
        for source in strings
        if "< (gap_start_ms | int(0)) - 5000" in source
    ]
    expiry_matches = [
        source
        for source in strings
        if ">= (gap_deadline_ms | int(1))" in source
    ]
    assert len(backward_matches) == 1, backward_matches
    assert len(expiry_matches) == 1, expiry_matches

    start_ms = 1_000_000
    deadline_ms = start_ms + 60_000
    fixtures = (
        ("elapsed_5s", start_ms + 5_000, "guard"),
        ("elapsed_30s", start_ms + 30_000, "guard"),
        ("elapsed_59_999s", start_ms + 59_999, "guard"),
        ("elapsed_60_000s", deadline_ms, "data_stale_timeout"),
        ("elapsed_60s_plus_epsilon", deadline_ms + 1, "data_stale_timeout"),
        ("backward_tolerance_edge", start_ms - 5_000, "guard"),
        ("backward_clock", start_ms - 5_001, "clock_anomaly"),
        ("forward_clock", deadline_ms + 60_000, "data_stale_timeout"),
    )
    with tempfile.TemporaryDirectory(
        prefix="hoymiles-gap-boundaries-ha-2026.8.2-"
    ) as config:
        hass = HomeAssistant(config)
        backward = Template(backward_matches[0], hass)
        expired = Template(expiry_matches[0], hass)
        for label, now_ms, expected in fixtures:
            variables = {
                "now_epoch_ms": now_ms,
                "gap_start_ms": start_ms,
                "gap_deadline_ms": deadline_ms,
            }
            backward_result = bool(backward.async_render(variables))
            expiry_result = bool(expired.async_render(variables))
            result = (
                "clock_anomaly"
                if backward_result
                else "data_stale_timeout"
                if expiry_result
                else "guard"
            )
            assert result == expected, (label, result, expected)
    return len(fixtures)


@dataclass(frozen=True, slots=True)
class PhysicalCall:
    service: str
    data: dict[str, Any]
    transaction_state: str
    abort_state: str


class RuntimeHarness:
    """Minimal exact-production HA package harness."""

    def __init__(self, package: dict[str, Any]) -> None:
        self.package = package
        self.tempdir = tempfile.TemporaryDirectory(
            prefix="hoymiles-balancing-ha-2026.8.2-"
        )
        self.hass = HomeAssistant(self.tempdir.name)
        self.physical_calls: list[PhysicalCall] = []
        self.service_events: list[tuple[str, str, dict[str, Any]]] = []
        self.notify_calls: list[dict[str, Any]] = []
        self.notify_observations: list[tuple[str, int]] = []
        self.block_service: str | None = None
        self.block_entered = asyncio.Event()
        self.block_release = asyncio.Event()
        self.block_notify = False
        self.notify_entered = asyncio.Event()
        self.notify_release = asyncio.Event()
        self.notify_raise = False
        self.notify_delay_seconds = 0.0
        self.notify_active = 0
        self.notify_peak = 0
        self.service_event_hook: (
            Callable[[str, str, dict[str, Any]], None] | None
        ) = None
        self._trigger_helper_ready = False

    async def setup(self, *, automations: tuple[str, ...] = ()) -> None:
        hass = self.hass
        loader.async_setup(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)

        template_block = self.package["template"][4]
        sensors = [
            item
            for item in template_block.get("sensor", [])
            if item.get("unique_id") in TEMPLATE_IDS
        ]
        binary_sensors = [
            item
            for item in template_block.get("binary_sensor", [])
            if item.get("unique_id") in TEMPLATE_IDS
        ]
        scripts = {
            name: body
            for name, body in self.package["script"].items()
            if "battery_balancing" in name
        }
        config = {
            "input_text": {
                name: self.package["input_text"][name]
                for name in BALANCING_TEXTS
            },
            "input_number": {
                name: self.package["input_number"][name]
                for name in BALANCING_NUMBERS
            },
            "input_boolean": {
                name: self.package["input_boolean"][name]
                for name in OWNER_BOOLEANS
            },
            "timer": {
                name: body
                for name, body in self.package["timer"].items()
                if "battery_balancing" in name
            },
            "input_datetime": {
                "hoymiles_battery_balancing_last_completed": self.package[
                    "input_datetime"
                ]["hoymiles_battery_balancing_last_completed"]
            },
            "template": [
                {"sensor": sensors, "binary_sensor": binary_sensors}
            ],
            "script": scripts,
        }

        for domain in (
            "input_text",
            "input_number",
            "input_boolean",
            "input_datetime",
            "timer",
        ):
            assert await async_setup_component(hass, domain, config), domain

        for name in OWNER_BOOLEANS:
            await self.call(
                "input_boolean", "turn_off", {"entity_id": f"input_boolean.{name}"}
            )
        await self.call(
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.hoymiles_battery_balancing_enabled"},
        )
        await self.call(
            "input_number",
            "set_value",
            {
                "entity_id": "input_number.hoymiles_battery_balancing_cycle_sequence",
                "value": 0,
            },
        )
        await self.call(
            "input_text",
            "set_value",
            {
                "entity_id": "input_text.hoymiles_tariff_active_action",
                "value": "none",
            },
        )
        await self.call(
            "input_text",
            "set_value",
            {
                "entity_id": "input_text.hoymiles_ems_push_notify_target",
                "value": "notify.offline_phone",
            },
        )
        await self.call(
            "input_datetime",
            "set_datetime",
            {
                "entity_id": "input_datetime.hoymiles_battery_balancing_last_completed",
                "datetime": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
        )
        await self.call(
            "input_number",
            "set_value",
            {
                "entity_id": "input_number.hoymiles_battery_balancing_hold_hours",
                "value": 2,
            },
        )
        self.set_external_defaults()

        assert await async_setup_component(hass, "template", config)
        for service in PHYSICAL_SERVICES:
            hass.services.async_register("script", service, self._physical_service)
        hass.services.async_register("notify", "send_message", self._notify_service)
        assert await async_setup_component(hass, "script", config)
        hass.bus.async_listen(EVENT_CALL_SERVICE, self._record_service_event)
        await hass.async_start()
        await hass.async_block_till_done()
        await self.initialize_records()

        if automations:
            await self.setup_automations(*automations)

    async def setup_automations(self, *automation_ids: str) -> None:
        selected = [
            item
            for item in self.package["automation"]
            if item.get("id") in automation_ids
        ]
        assert len(selected) == len(automation_ids)
        if not self._trigger_helper_ready:
            await trigger.async_setup(self.hass)
            self._trigger_helper_ready = True
        assert await async_setup_component(
            self.hass, "automation", {"automation": selected}
        )
        await self.hass.async_block_till_done()

    def set_state(
        self, entity_id: str, state: Any, attributes: dict[str, Any] | None = None
    ) -> None:
        self.hass.states.async_set(entity_id, str(state), attributes or {})

    def set_external_defaults(self) -> None:
        self.set_state("sun.sun", "above_horizon")
        self.set_state("timer.hoymiles_discharge", "idle")
        self.set_state("timer.hoymiles_charge", "idle")
        self.set_state("binary_sensor.hoymiles_battery_balancing_due", "on")
        self.set_state(
            "binary_sensor.hoymiles_battery_balancing_control_data_ready",
            "on",
            {"failure_class": "none", "reason_code": "none"},
        )
        self.set_state("sensor.hoymiles_ems_hardware_mode", "self_use")
        self.set_state("sensor.hoymiles_hit_ems_force_charge_soc_readback", 80)
        self.set_state(
            "sensor.hoymiles_hit_ems_maximum_charge_power_readback", 50
        )
        self.set_state(
            "sensor.hoymiles_hit_ems_control_readback_generation", 10
        )
        self.set_state(
            "sensor.hoymiles_hit_parallel_topology_readback_generation", 20
        )
        self.set_state("sensor.hoymiles_hit_overview_battery_soc", 50)
        self.set_state("sensor.hoymiles_hit_maximum_charge_current", 20)
        self.set_state("sensor.hoymiles_hit_battery_voltage_bms", 400)
        self.set_state("sensor.hoymiles_hit_battery_fault_code_bms", "Brak błędu")
        self.set_state("sensor.hoymiles_hit_overview_battery_faults", "Brak błędów")
        self.set_state("sensor.hoymiles_hit_inverter_work_status", "Praca z siecią")
        self.set_state("sensor.hoymiles_hit_machines_type", 0)
        self.set_state("sensor.hoymiles_hit_number_of_machines_master_and_slave", 1)
        self.set_state(
            "sensor.hoymiles_battery_balancing_bms_safe_charge_power",
            2.5,
            {"self_use_percent": 8.0, "grid_charge_percent": 12.0},
        )
        self.set_state(
            "sensor.hoymiles_battery_balancing_slow_charge_power",
            0.4,
            {"self_use_percent": 8.0, "grid_charge_percent": 12.0},
        )
        self.set_state("notify.offline_phone", "unknown")

    async def initialize_records(self) -> None:
        await self.call_script(
            "hoymiles_battery_balancing_write_record",
            cycle_id="idle",
            transaction_state="IDLE",
            owner="none",
            owner_generation=0,
            snapshot_valid=False,
            snapshot_cycle_id="none",
            snapshot_mode="unknown",
            snapshot_4303=0,
            snapshot_4304=0,
            snapshot_ems_generation=0,
            snapshot_topology_generation=0,
            snapshot_epoch_ms=0,
            write_started=False,
            reason_code="none",
            operational_started=False,
            cooldown_epoch_ms=0,
            event_marker="NONE",
        )
        await self.call_script(
            "hoymiles_battery_balancing_write_timing",
            cycle_id="idle",
            gap_generation=0,
            gap_start_epoch_ms=0,
            gap_deadline_epoch_ms=0,
            hold_generation=0,
            hold_deadline_epoch_ms=0,
            timing_state="NONE",
        )
        await self.call_script(
            "hoymiles_battery_balancing_write_abort_request",
            cycle_id="idle",
            request_generation=0,
            priority=0,
            reason_code="none",
            request_state="NONE",
            requested_epoch_ms=0,
        )
        await self.call_script(
            "hoymiles_battery_balancing_write_outbox",
            slot_1_event_id="-",
            slot_1_cycle_id="idle",
            slot_1_kind="NA",
            slot_1_reason="none",
            slot_1_delivery_state="EMPTY",
            slot_1_attempt_count=0,
            slot_1_stable_tag="-",
            slot_2_event_id="-",
            slot_2_cycle_id="idle",
            slot_2_kind="NA",
            slot_2_reason="none",
            slot_2_delivery_state="EMPTY",
            slot_2_attempt_count=0,
            slot_2_stable_tag="-",
        )
        await self.hass.async_block_till_done()
        assert self.state("sensor.hoymiles_battery_balancing_transaction") == "IDLE"
        assert self.state("sensor.hoymiles_battery_balancing_timing_transaction") == "NONE"
        assert self.state("sensor.hoymiles_battery_balancing_abort_request") == "NONE"
        assert self.state("sensor.hoymiles_battery_balancing_notification_outbox") == "VALID"

    async def seed_cycle(
        self,
        *,
        cycle_number: int = 1,
        state: str = "SNAPSHOT_VALID",
        mode: str = "self_use",
        register_4303: float = 80,
        register_4304: float = 50,
        write_started: bool | None = None,
        operational_started: bool = False,
    ) -> str:
        cycle = f"c{cycle_number}"
        if write_started is None:
            write_started = state not in {
                "REQUESTED",
                "OWNER_ACQUIRED",
                "SNAPSHOT_VALID",
            }
        self.set_state("sensor.hoymiles_ems_hardware_mode", mode)
        self.set_state(
            "sensor.hoymiles_hit_ems_force_charge_soc_readback", register_4303
        )
        self.set_state(
            "sensor.hoymiles_hit_ems_maximum_charge_power_readback", register_4304
        )
        await self.call(
            "input_number",
            "set_value",
            {
                "entity_id": "input_number.hoymiles_battery_balancing_cycle_sequence",
                "value": cycle_number,
            },
        )
        await self.call(
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.hoymiles_battery_balancing_active"},
        )
        await self.call_script(
            "hoymiles_battery_balancing_write_record",
            cycle_id=cycle,
            transaction_state=state,
            owner="balancing",
            owner_generation=cycle_number,
            snapshot_valid=True,
            snapshot_cycle_id=cycle,
            snapshot_mode=mode,
            snapshot_4303=register_4303,
            snapshot_4304=register_4304,
            snapshot_ems_generation=10,
            snapshot_topology_generation=20,
            snapshot_epoch_ms=self.now_ms(),
            write_started=write_started,
            reason_code="none",
            operational_started=operational_started,
            cooldown_epoch_ms=0,
            event_marker="STARTED" if operational_started else "NONE",
        )
        await self.call_script(
            "hoymiles_battery_balancing_write_timing",
            cycle_id=cycle,
            gap_generation=1,
            gap_start_epoch_ms=0,
            gap_deadline_epoch_ms=0,
            hold_generation=1,
            hold_deadline_epoch_ms=0,
            timing_state="NONE",
        )
        await self.call_script(
            "hoymiles_battery_balancing_write_abort_request",
            cycle_id=cycle,
            request_generation=1,
            priority=0,
            reason_code="none",
            request_state="NONE",
            requested_epoch_ms=0,
        )
        await self.hass.async_block_till_done()
        assert self.state("sensor.hoymiles_battery_balancing_transaction") == state
        return cycle

    async def call(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        await self.hass.services.async_call(domain, service, data, blocking=True)

    async def call_script(self, service: str, **data: Any) -> None:
        await self.call("script", service, data)

    def state(self, entity_id: str) -> str:
        item = self.hass.states.get(entity_id)
        return "missing" if item is None else item.state

    def attr(self, entity_id: str, attribute: str) -> Any:
        item = self.hass.states.get(entity_id)
        return None if item is None else item.attributes.get(attribute)

    @staticmethod
    def now_ms() -> int:
        from time import time

        return int(time() * 1000)

    async def wait_until(
        self,
        predicate: Callable[[], bool],
        *,
        timeout: float = 8.0,
        message: str,
    ) -> None:
        try:
            async with asyncio.timeout(timeout):
                while not predicate():
                    await asyncio.sleep(0.01)
        except TimeoutError as error:
            raise AssertionError(message) from error
        assert predicate(), message

    async def wait_worker_idle(self) -> None:
        await self.wait_until(
            lambda: self.state(
                "script.hoymiles_battery_balancing_transaction_worker"
            )
            != "on",
            timeout=12,
            message="transaction worker remained active",
        )
        await asyncio.sleep(0.05)

    async def _physical_service(self, call: ServiceCall) -> None:
        tx_state = self.state("sensor.hoymiles_battery_balancing_transaction")
        self.physical_calls.append(
            PhysicalCall(
                service=call.service,
                data=dict(call.data),
                transaction_state=tx_state,
                abort_state=self.state(
                    "sensor.hoymiles_battery_balancing_abort_request"
                ),
            )
        )
        if self.block_service == call.service and not self.block_entered.is_set():
            self.block_entered.set()
            await self.block_release.wait()
        if call.service.endswith("maximum_charge_power"):
            self.set_state(
                "sensor.hoymiles_hit_ems_maximum_charge_power_readback",
                call.data["value"],
            )
        elif call.service.endswith("force_charge_soc"):
            self.set_state(
                "sensor.hoymiles_hit_ems_force_charge_soc_readback",
                call.data["value"],
            )
        elif call.service.endswith("ems_mode"):
            self.set_state(
                "sensor.hoymiles_ems_hardware_mode", call.data["option"]
            )

    async def _notify_service(self, call: ServiceCall) -> None:
        self.notify_calls.append(dict(call.data))
        self.notify_observations.append(
            (
                str(
                    self.attr(
                        "sensor.hoymiles_battery_balancing_notification_outbox",
                        "slot_1_delivery_state",
                    )
                ),
                int(
                    self.attr(
                        "sensor.hoymiles_battery_balancing_notification_outbox",
                        "slot_1_attempt_count",
                    )
                ),
            )
        )
        self.notify_active += 1
        self.notify_peak = max(self.notify_peak, self.notify_active)
        try:
            if self.notify_delay_seconds > 0:
                await asyncio.sleep(self.notify_delay_seconds)
            if self.block_notify:
                self.notify_entered.set()
                await self.notify_release.wait()
            if self.notify_raise:
                raise RuntimeError("mock notification provider failure")
        finally:
            self.notify_active -= 1

    def _record_service_event(self, event: Event) -> None:
        domain = str(event.data.get("domain"))
        service = str(event.data.get("service"))
        data = dict(event.data.get("service_data", {}))
        self.service_events.append((domain, service, data))
        if self.service_event_hook is not None:
            self.service_event_hook(domain, service, data)

    async def close(self) -> None:
        if not self.block_release.is_set():
            self.block_release.set()
        if not self.notify_release.is_set():
            self.notify_release.set()
        await self.hass.async_stop()
        self.tempdir.cleanup()


async def with_harness(
    package: dict[str, Any],
    test: Callable[[RuntimeHarness], Awaitable[None]],
    *,
    automations: tuple[str, ...] = (),
) -> None:
    harness = RuntimeHarness(package)
    try:
        await harness.setup(automations=automations)
        await test(harness)
    finally:
        await harness.close()


def committed_lifecycle_fields(harness: RuntimeHarness) -> list[list[str]]:
    """Return only b2 values that reached the lifecycle input_text service."""
    records: list[list[str]] = []
    for domain, service, data in harness.service_events:
        entity_ids = data.get("entity_id", [])
        if isinstance(entity_ids, str):
            entity_ids = [entity_ids]
        if (
            domain == "input_text"
            and service == "set_value"
            and "input_text.hoymiles_battery_balancing_lifecycle" in entity_ids
        ):
            fields = str(data.get("value", "")).split("|")
            if len(fields) == 18 and fields[0] == "b2":
                records.append(fields)
    return records


def install_boundary_hook(
    harness: RuntimeHarness,
    hook: Callable[[str, str, dict[str, Any]], None],
) -> None:
    """Run one test hook at the exact Home Assistant service-call boundary."""

    @callback
    def boundary_listener(event: Event) -> None:
        hook(
            str(event.data.get("domain")),
            str(event.data.get("service")),
            dict(event.data.get("service_data", {})),
        )

    harness.hass.bus.async_listen(EVENT_CALL_SERVICE, boundary_listener)


async def test_exact_start(package: dict[str, Any]) -> None:
    async def scenario(harness: RuntimeHarness) -> None:
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="start",
            expected_cycle_id="",
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            in {"OPERATIONAL", "SLOW", "HOLDING"},
            timeout=10,
            message="exact start never reached an operational phase",
        )
        await harness.wait_worker_idle()
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "cycle_id"
        ) == "c1"
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "snapshot_cycle_id"
        ) == "c1"
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "write_started"
        ) is True
        assert [item.service for item in harness.physical_calls] == [
            "hoymiles_verified_set_ems_maximum_charge_power",
            "hoymiles_verified_set_ems_force_charge_soc",
        ]

    await with_harness(package, scenario)


async def test_hard_stop_race_matrix(package: dict[str, Any]) -> int:
    helpers = {
        "maximum_charge_power": {
            "service": "hoymiles_verified_set_ems_maximum_charge_power",
            "mode": "self_use",
            "register_4303": 80,
            "register_4304": 50,
        },
        "force_charge_soc": {
            "service": "hoymiles_verified_set_ems_force_charge_soc",
            "mode": "self_use",
            "register_4303": 80,
            "register_4304": 8,
        },
        "ems_mode": {
            "service": "hoymiles_verified_set_ems_mode",
            "mode": "grid_charge",
            "register_4303": 100,
            "register_4304": 8,
        },
    }

    async def off_grid(harness: RuntimeHarness) -> None:
        harness.set_state("sensor.hoymiles_ems_hardware_mode", "off_grid")

    async def bms_fault(harness: RuntimeHarness) -> None:
        harness.set_state("sensor.hoymiles_hit_battery_fault_code_bms", "Alarm")

    async def conflict(harness: RuntimeHarness) -> None:
        await harness.call(
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.hoymiles_rce_discharge_active"},
        )

    async def ownership_lost(harness: RuntimeHarness) -> None:
        await harness.call(
            "input_boolean",
            "turn_off",
            {"entity_id": "input_boolean.hoymiles_battery_balancing_active"},
        )

    async def user_disabled(harness: RuntimeHarness) -> None:
        await harness.call(
            "input_boolean",
            "turn_off",
            {"entity_id": "input_boolean.hoymiles_battery_balancing_enabled"},
        )

    hard_stops: dict[
        str, tuple[str, Callable[[RuntimeHarness], Awaitable[None]]]
    ] = {
        "off_grid": ("off_grid", off_grid),
        "bms_fault": ("bms_fault", bms_fault),
        "conflict": ("control_conflict", conflict),
        "ownership_lost": ("ownership_lost", ownership_lost),
        "user_disabled": ("user_disabled", user_disabled),
    }

    cases = 0
    for helper_name, helper in helpers.items():
        for stop_name, (expected_reason, mutate) in hard_stops.items():
            async def scenario(
                harness: RuntimeHarness,
                *,
                helper: dict[str, Any] = helper,
                helper_name: str = helper_name,
                stop_name: str = stop_name,
                expected_reason: str = expected_reason,
                mutate: Callable[[RuntimeHarness], Awaitable[None]] = mutate,
            ) -> None:
                cycle = await harness.seed_cycle(
                    mode=str(helper["mode"]),
                    register_4303=float(helper["register_4303"]),
                    register_4304=float(helper["register_4304"]),
                )
                assert harness.state(
                    "binary_sensor.hoymiles_battery_balancing_apply_authorized"
                ) == "on"
                harness.block_service = str(helper["service"])
                await harness.call(
                    "script",
                    "turn_on",
                    {
                        "entity_id": (
                            "script.hoymiles_battery_balancing_transaction_worker"
                        ),
                        "variables": {
                            "intent": "reconcile",
                            "expected_cycle_id": cycle,
                        },
                    },
                )
                await asyncio.wait_for(harness.block_entered.wait(), timeout=5)
                await mutate(harness)
                await harness.wait_until(
                    lambda: harness.state(
                        "sensor.hoymiles_battery_balancing_abort_request"
                    )
                    == "PENDING",
                    timeout=5,
                    message=(
                        f"{stop_name} was lost during {helper_name} helper wait"
                    ),
                )
                assert harness.attr(
                    "sensor.hoymiles_battery_balancing_abort_request",
                    "reason_code",
                ) == expected_reason
                harness.block_release.set()
                try:
                    await harness.wait_worker_idle()
                except TimeoutError:
                    raise AssertionError(
                        f"worker timeout for {helper_name}/{stop_name}: "
                        f"tx={harness.state('sensor.hoymiles_battery_balancing_transaction')}, "
                        f"abort={harness.state('sensor.hoymiles_battery_balancing_abort_request')}, "
                        f"reason={harness.attr('sensor.hoymiles_battery_balancing_abort_request', 'reason_code')}, "
                        f"worker={harness.hass.states.get('script.hoymiles_battery_balancing_transaction_worker')}, "
                        f"calls={harness.physical_calls}, "
                        f"events={harness.service_events[-20:]}"
                    ) from None
                nonrestore_after_abort = [
                    item
                    for item in harness.physical_calls[1:]
                    if item.transaction_state
                    in {
                        "APPLYING",
                        "OPERATIONAL",
                        "SLOW",
                        "HOLD_ARMING",
                        "HOLDING",
                    }
                ]
                assert not nonrestore_after_abort, (
                    helper_name,
                    stop_name,
                    nonrestore_after_abort,
                )
                assert harness.state(
                    "sensor.hoymiles_battery_balancing_transaction"
                ) in {"TERMINAL", "RESTORE_FAILED"}

            print(f"  race {helper_name}/{stop_name}")
            await with_harness(
                package,
                scenario,
                automations=HARD_STOP_AUTOMATIONS,
            )
            cases += 1
    return cases


async def test_transient_hard_stop_capture(package: dict[str, Any]) -> int:
    """Retain trigger-time evidence after every tested fault has recovered."""

    def bms_fault(harness: RuntimeHarness) -> None:
        harness.set_state("sensor.hoymiles_hit_battery_fault_code_bms", "Alarm")
        harness.set_state("sensor.hoymiles_hit_battery_fault_code_bms", "Brak błędu")

    def invalid_current(harness: RuntimeHarness) -> None:
        harness.set_state("sensor.hoymiles_hit_maximum_charge_current", "unavailable")
        harness.set_state("sensor.hoymiles_hit_maximum_charge_current", 20)

    def invalid_voltage(harness: RuntimeHarness) -> None:
        harness.set_state("sensor.hoymiles_hit_battery_voltage_bms", "unavailable")
        harness.set_state("sensor.hoymiles_hit_battery_voltage_bms", 400)

    def invalid_limit(harness: RuntimeHarness) -> None:
        harness.set_state(
            "sensor.hoymiles_battery_balancing_bms_safe_charge_power", 0
        )
        harness.set_state(
            "sensor.hoymiles_battery_balancing_bms_safe_charge_power",
            2.5,
            {"self_use_percent": 8.0, "grid_charge_percent": 12.0},
        )

    def off_grid(harness: RuntimeHarness) -> None:
        harness.set_state("sensor.hoymiles_ems_hardware_mode", "off_grid")
        harness.set_state("sensor.hoymiles_ems_hardware_mode", "self_use")

    def user_disabled(harness: RuntimeHarness) -> None:
        harness.set_state("input_boolean.hoymiles_battery_balancing_enabled", "off")
        harness.set_state("input_boolean.hoymiles_battery_balancing_enabled", "on")

    def conflict(harness: RuntimeHarness) -> None:
        harness.set_state("binary_sensor.hoymiles_ems_control_conflict", "on")
        harness.set_state("binary_sensor.hoymiles_ems_control_conflict", "off")

    def owner_lost(harness: RuntimeHarness) -> None:
        harness.set_state("input_boolean.hoymiles_battery_balancing_active", "off")
        harness.set_state("input_boolean.hoymiles_battery_balancing_active", "on")

    fixtures: tuple[
        tuple[str, str, Callable[[RuntimeHarness], None]], ...
    ] = (
        ("bms_fault", "bms_fault", bms_fault),
        ("invalid_current", "bms_data_invalid", invalid_current),
        ("invalid_voltage", "bms_data_invalid", invalid_voltage),
        ("invalid_limit", "bms_limit_invalid", invalid_limit),
        ("off_grid", "off_grid", off_grid),
        ("user_disabled", "user_disabled", user_disabled),
        ("conflict", "control_conflict", conflict),
        ("owner_lost", "ownership_lost", owner_lost),
    )

    for label, expected_reason, fire_and_recover in fixtures:
        async def scenario(
            harness: RuntimeHarness,
            label: str = label,
            expected_reason: str = expected_reason,
            fire_and_recover: Callable[[RuntimeHarness], None] = fire_and_recover,
        ) -> None:
            cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
            harness.block_service = (
                "hoymiles_verified_set_ems_maximum_charge_power"
            )
            await harness.call(
                "script",
                "turn_on",
                {
                    "entity_id": (
                        "script.hoymiles_battery_balancing_transaction_worker"
                    ),
                    "variables": {
                        "intent": "reconcile",
                        "expected_cycle_id": cycle,
                    },
                },
            )
            await asyncio.wait_for(harness.block_entered.wait(), timeout=5)
            physical_before = len(harness.physical_calls)
            fire_and_recover(harness)
            await harness.wait_until(
                lambda: harness.state(
                    "sensor.hoymiles_battery_balancing_abort_request"
                )
                == "PENDING",
                timeout=5,
                message=f"transient hard-stop was lost: {label}",
            )
            assert harness.attr(
                "sensor.hoymiles_battery_balancing_abort_request", "reason_code"
            ) == expected_reason
            assert len(harness.physical_calls) == physical_before
            assert not harness.notify_calls
            harness.block_release.set()
            await harness.wait_worker_idle()

        await with_harness(
            package,
            scenario,
            automations=HARD_STOP_AUTOMATIONS,
        )

    async def burst(harness: RuntimeHarness) -> None:
        cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
        harness.block_service = "hoymiles_verified_set_ems_maximum_charge_power"
        await harness.call(
            "script",
            "turn_on",
            {
                "entity_id": (
                    "script.hoymiles_battery_balancing_transaction_worker"
                ),
                "variables": {
                    "intent": "reconcile",
                    "expected_cycle_id": cycle,
                },
            },
        )
        await asyncio.wait_for(harness.block_entered.wait(), timeout=5)
        physical_before = len(harness.physical_calls)
        for index in range(101):
            harness.set_state(
                "sensor.hoymiles_hit_battery_fault_code_bms", f"Alarm{index}"
            )
            harness.set_state(
                "sensor.hoymiles_hit_battery_fault_code_bms", "Brak błędu"
            )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_abort_request"
            )
            == "PENDING",
            timeout=8,
            message="101-event hard-stop burst lost the only durable fault",
        )
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_abort_request", "reason_code"
        ) == "bms_fault"
        await harness.wait_until(
            lambda: sum(
                1
                for domain, service, data in harness.service_events
                if domain == "script"
                and service == "hoymiles_battery_balancing_write_abort_request"
                and data.get("request_state") == "PENDING"
            )
            == 1,
            timeout=5,
            message="durable abort service event was not observed after its state",
        )
        durable_writes = [
            data
            for domain, service, data in harness.service_events
            if domain == "script"
            and service == "hoymiles_battery_balancing_write_abort_request"
            and data.get("request_state") == "PENDING"
        ]
        assert len(durable_writes) == 1, durable_writes
        assert len(harness.physical_calls) == physical_before
        assert not harness.notify_calls
        harness.block_release.set()
        await harness.wait_worker_idle()

    await with_harness(
        package,
        burst,
        automations=("hoymiles_battery_balancing_hard_stop_capture",),
    )

    async def saturated_priority_lane(harness: RuntimeHarness) -> None:
        await harness.seed_cycle(state="SNAPSHOT_VALID")
        for index in range(100):
            harness.set_state(
                "sensor.hoymiles_hit_battery_fault_code_bms", f"Alarm{index}"
            )
            harness.set_state(
                "sensor.hoymiles_hit_battery_fault_code_bms", "Brak błędu"
            )
        harness.set_state("sensor.hoymiles_ems_hardware_mode", "off_grid")
        await harness.wait_until(
            lambda: any(
                domain == "script"
                and service
                == "hoymiles_battery_balancing_write_abort_request"
                and data.get("request_state") == "PENDING"
                and data.get("reason_code") == "off_grid"
                and data.get("priority") == 100
                for domain, service, data in harness.service_events
            ),
            timeout=8,
            message="the 101st, higher-priority Off-Grid event was dropped",
        )
        await harness.wait_until(
            lambda: (
                harness.attr(
                    "sensor.hoymiles_battery_balancing_abort_request", "reason_code"
                )
                == "off_grid"
                or harness.attr(
                    "sensor.hoymiles_battery_balancing_transaction", "reason_code"
                )
                == "off_grid"
            ),
            timeout=5,
            message="the persisted Off-Grid reason was not retained",
        )
        assert not harness.physical_calls
        assert not harness.notify_calls

    await with_harness(
        package,
        saturated_priority_lane,
        automations=HARD_STOP_AUTOMATIONS,
    )

    saturation_escalations = (
        (
            "ownership_lost",
            95,
            lambda harness: harness.set_state(
                "input_boolean.hoymiles_battery_balancing_active", "off"
            ),
        ),
        (
            "control_conflict",
            95,
            lambda harness: harness.set_state(
                "binary_sensor.hoymiles_ems_control_conflict", "on"
            ),
        ),
    )
    for expected_reason, expected_priority, fire_higher in saturation_escalations:
        async def saturated_escalation(
            harness: RuntimeHarness,
            *,
            expected_reason: str = expected_reason,
            expected_priority: int = expected_priority,
            fire_higher: Callable[[RuntimeHarness], None] = fire_higher,
        ) -> None:
            await harness.seed_cycle(state="SNAPSHOT_VALID")
            for index in range(100):
                harness.set_state(
                    "sensor.hoymiles_hit_battery_fault_code_bms", f"Alarm{index}"
                )
                harness.set_state(
                    "sensor.hoymiles_hit_battery_fault_code_bms", "Brak błędu"
                )
            fire_higher(harness)
            await harness.wait_until(
                lambda: any(
                    domain == "script"
                    and service
                    == "hoymiles_battery_balancing_write_abort_request"
                    and data.get("request_state") == "PENDING"
                    and data.get("reason_code") == expected_reason
                    and data.get("priority") == expected_priority
                    for domain, service, data in harness.service_events
                ),
                timeout=8,
                message=(
                    "the 101st higher-priority event was dropped: "
                    f"{expected_reason}"
                ),
            )
            await harness.wait_until(
                lambda: (
                    harness.attr(
                        "sensor.hoymiles_battery_balancing_abort_request",
                        "reason_code",
                    )
                    == expected_reason
                    or harness.attr(
                        "sensor.hoymiles_battery_balancing_transaction",
                        "reason_code",
                    )
                    == expected_reason
                ),
                timeout=5,
                message=f"priority reason was not retained: {expected_reason}",
            )
            assert not harness.physical_calls
            assert not harness.notify_calls

        await with_harness(
            package,
            saturated_escalation,
            automations=HARD_STOP_AUTOMATIONS,
        )

    def burst_control_data(harness: RuntimeHarness, reason: str) -> None:
        for _ in range(100):
            harness.set_state(
                "binary_sensor.hoymiles_battery_balancing_control_data_ready",
                "off",
                {"failure_class": "hard", "reason_code": reason},
            )
            harness.set_state(
                "binary_sensor.hoymiles_battery_balancing_control_data_ready",
                "on",
                {"failure_class": "none", "reason_code": "none"},
            )

    def burst_user_disabled(harness: RuntimeHarness) -> None:
        for _ in range(100):
            harness.set_state(
                "input_boolean.hoymiles_battery_balancing_enabled", "off"
            )
            harness.set_state(
                "input_boolean.hoymiles_battery_balancing_enabled", "on"
            )

    def burst_conflict(harness: RuntimeHarness) -> None:
        for _ in range(100):
            harness.set_state("binary_sensor.hoymiles_ems_control_conflict", "on")
            harness.set_state("binary_sensor.hoymiles_ems_control_conflict", "off")

    adjacent_priority_escalations: tuple[
        tuple[
            str,
            int,
            Callable[[RuntimeHarness], None],
            Callable[[RuntimeHarness], None],
        ], ...
    ] = (
        (
            "communication_failure",
            75,
            lambda harness: burst_control_data(harness, "unknown_internal_error"),
            lambda harness: harness.set_state(
                "binary_sensor.hoymiles_battery_balancing_control_data_ready",
                "off",
                {"failure_class": "hard", "reason_code": "communication_failure"},
            ),
        ),
        (
            "user_disabled",
            80,
            lambda harness: burst_control_data(harness, "communication_failure"),
            lambda harness: harness.set_state(
                "input_boolean.hoymiles_battery_balancing_enabled", "off"
            ),
        ),
        (
            "bms_fault",
            90,
            burst_user_disabled,
            lambda harness: harness.set_state(
                "sensor.hoymiles_hit_battery_fault_code_bms", "Alarm"
            ),
        ),
        (
            "off_grid",
            100,
            burst_conflict,
            lambda harness: harness.set_state(
                "sensor.hoymiles_ems_hardware_mode", "off_grid"
            ),
        ),
    )
    for (
        expected_reason,
        expected_priority,
        fire_lower_burst,
        fire_higher,
    ) in adjacent_priority_escalations:
        async def adjacent_escalation(
            harness: RuntimeHarness,
            *,
            expected_reason: str = expected_reason,
            expected_priority: int = expected_priority,
            fire_lower_burst: Callable[[RuntimeHarness], None] = fire_lower_burst,
            fire_higher: Callable[[RuntimeHarness], None] = fire_higher,
        ) -> None:
            await harness.seed_cycle(state="SNAPSHOT_VALID")
            fire_lower_burst(harness)
            fire_higher(harness)
            await harness.wait_until(
                lambda: any(
                    domain == "script"
                    and service
                    == "hoymiles_battery_balancing_write_abort_request"
                    and data.get("request_state") == "PENDING"
                    and data.get("reason_code") == expected_reason
                    and data.get("priority") == expected_priority
                    for domain, service, data in harness.service_events
                ),
                timeout=10,
                message=(
                    "adjacent priority lane dropped the 101st higher event: "
                    f"{expected_reason}"
                ),
            )
            await harness.wait_until(
                lambda: (
                    harness.attr(
                        "sensor.hoymiles_battery_balancing_abort_request",
                        "reason_code",
                    )
                    == expected_reason
                    or harness.attr(
                        "sensor.hoymiles_battery_balancing_transaction",
                        "reason_code",
                    )
                    == expected_reason
                ),
                timeout=5,
                message=(
                    "adjacent priority merge did not retain the exact reason: "
                    f"{expected_reason}"
                ),
            )
            assert not harness.physical_calls

        await with_harness(
            package,
            adjacent_escalation,
            automations=HARD_STOP_AUTOMATIONS,
        )

    priority_fixtures = (
        ("user_disabled", "bms_fault", "bms_fault"),
        ("bms_fault", "user_disabled", "bms_fault"),
        ("bms_data_invalid", "bms_fault", "bms_data_invalid"),
    )
    for first, second, expected in priority_fixtures:
        async def priority_scenario(
            harness: RuntimeHarness,
            first: str = first,
            second: str = second,
            expected: str = expected,
        ) -> None:
            cycle = await harness.seed_cycle(state="APPLYING")
            harness.set_state(
                "sensor.hoymiles_hit_ems_maximum_charge_power_readback", 8
            )
            harness.block_service = (
                "hoymiles_verified_set_ems_maximum_charge_power"
            )
            await harness.call_script(
                "hoymiles_battery_balancing_request_abort",
                reason_code=first,
                cycle_id=cycle,
            )
            await asyncio.wait_for(harness.block_entered.wait(), timeout=5)
            await harness.call_script(
                "hoymiles_battery_balancing_request_abort",
                reason_code=second,
                cycle_id=cycle,
            )
            assert harness.attr(
                "sensor.hoymiles_battery_balancing_abort_request", "reason_code"
            ) == expected
            harness.block_release.set()
            await harness.wait_worker_idle()

        await with_harness(package, priority_scenario)

    return (
        len(fixtures)
        + 2
        + len(saturation_escalations)
        + len(adjacent_priority_escalations)
        + len(priority_fixtures)
    )


async def test_legacy_and_malformed_recovery(package: dict[str, Any]) -> int:
    cases = {
        "recognized legacy": (
            "123|started|0|0|none|slow|grid_charge",
            "legacy_untrusted",
        ),
        "truncated b2": ("b2|c1", "lifecycle_invalid"),
        "legal 255-byte garbage": ("x" * 255, "lifecycle_invalid"),
    }
    for label, (raw_record, expected_reason) in cases.items():
        async def scenario(
            harness: RuntimeHarness,
            *,
            label: str = label,
            raw_record: str = raw_record,
            expected_reason: str = expected_reason,
        ) -> None:
            await harness.call(
                "input_boolean",
                "turn_on",
                {"entity_id": "input_boolean.hoymiles_battery_balancing_active"},
            )
            await harness.call(
                "input_text",
                "set_value",
                {
                    "entity_id": "input_text.hoymiles_battery_balancing_phase",
                    "value": "SLOW",
                },
            )
            service_count = len(harness.service_events)
            await harness.call(
                "input_text",
                "set_value",
                {
                    "entity_id": "input_text.hoymiles_battery_balancing_lifecycle",
                    "value": raw_record,
                },
            )
            await harness.wait_until(
                lambda: harness.state(
                    "sensor.hoymiles_battery_balancing_transaction"
                )
                == "RECOVERY_REQUIRED",
                timeout=7,
                message=f"{label} did not enter RECOVERY_REQUIRED",
            )
            assert harness.attr(
                "sensor.hoymiles_battery_balancing_transaction", "reason_code"
            ) == expected_reason
            assert harness.state(
                "input_boolean.hoymiles_battery_balancing_active"
            ) == "on"
            assert harness.attr(
                "sensor.hoymiles_ems_control_owner", "owner_code"
            ) == "balancing"
            assert not harness.physical_calls
            later_events = harness.service_events[service_count:]
            assert not any(
                domain == "input_boolean"
                and service == "turn_off"
                and "input_boolean.hoymiles_battery_balancing_active"
                in data.get("entity_id", [])
                for domain, service, data in later_events
            )
            await harness.wait_until(
                lambda: "legacy.RQ"
                in {
                    harness.attr(
                        "sensor.hoymiles_battery_balancing_notification_outbox",
                        "slot_1_event_id",
                    ),
                    harness.attr(
                        "sensor.hoymiles_battery_balancing_notification_outbox",
                        "slot_2_event_id",
                    ),
                },
                timeout=7,
                message=f"{label} did not persist one recovery outbox event",
            )
            event_ids = {
                harness.attr(
                    "sensor.hoymiles_battery_balancing_notification_outbox",
                    "slot_1_event_id",
                ),
                harness.attr(
                    "sensor.hoymiles_battery_balancing_notification_outbox",
                    "slot_2_event_id",
                ),
            }
            assert "legacy.RQ" in event_ids
            assert len([item for item in event_ids if item == "legacy.RQ"]) == 1
            await harness.call_script(
                "hoymiles_battery_balancing_transaction_worker",
                intent="reconcile",
                expected_cycle_id="legacy",
            )
            assert not harness.physical_calls
            assert harness.state(
                "input_boolean.hoymiles_battery_balancing_active"
            ) == "on"

        await with_harness(
            package,
            scenario,
            automations=("hoymiles_battery_balancing_control",),
        )

    async def dedicated_recovery_lane(harness: RuntimeHarness) -> None:
        await harness.call(
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.hoymiles_battery_balancing_active"},
        )
        await harness.call(
            "input_text",
            "set_value",
            {
                "entity_id": "input_text.hoymiles_battery_balancing_phase",
                "value": "SLOW",
            },
        )
        await harness.call(
            "input_text",
            "set_value",
            {
                "entity_id": "input_text.hoymiles_battery_balancing_lifecycle",
                "value": "b2|broken",
            },
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "RECOVERY_REQUIRED",
            timeout=7,
            message="dedicated malformed-lifecycle admission was suppressed",
        )
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "reason_code"
        ) == "lifecycle_invalid"
        assert harness.state(
            "input_boolean.hoymiles_battery_balancing_active"
        ) == "on"
        assert not harness.physical_calls

    await with_harness(
        package,
        dedicated_recovery_lane,
        automations=(
            "hoymiles_battery_balancing_recovery_hard_stop_capture",
        ),
    )
    return len(cases) + 1


async def test_soft_gap_restart_and_clock(package: dict[str, Any]) -> int:
    cases = (
        ("elapsed_5s", -5_000, "guard"),
        ("restart_at_10s", -10_000, "guard"),
        ("elapsed_30s", -30_000, "guard"),
        # Exact 59.999/60.000 s production-template boundaries are covered by
        # test_soft_gap_exact_boundaries. Keep enough host-setup margin here;
        # this scenario verifies restart recovery of the absolute deadline.
        ("restart_at_50s", -50_000, "guard"),
        ("expired", -60_001, "data_stale_timeout"),
        ("forward_clock", -120_000, "data_stale_timeout"),
        ("backward_clock", 10_000, "clock_anomaly"),
    )
    for label, start_delta_ms, expected in cases:
        async def scenario(
            harness: RuntimeHarness,
            *,
            label: str = label,
            start_delta_ms: int = start_delta_ms,
            expected: str = expected,
        ) -> None:
            cycle = await harness.seed_cycle(
                state="OPERATIONAL",
                register_4303=100,
                register_4304=8,
                operational_started=True,
            )
            await harness.call(
                "timer",
                "start",
                {"entity_id": "timer.hoymiles_battery_balancing_watchdog"},
            )
            gap_start = harness.now_ms() + start_delta_ms
            gap_deadline = gap_start + 60_000
            await harness.call_script(
                "hoymiles_battery_balancing_write_timing",
                cycle_id=cycle,
                gap_generation=7,
                gap_start_epoch_ms=gap_start,
                gap_deadline_epoch_ms=gap_deadline,
                hold_generation=1,
                hold_deadline_epoch_ms=0,
                timing_state="GAP",
            )
            harness.set_state(
                "binary_sensor.hoymiles_battery_balancing_control_data_ready",
                "off",
                {"failure_class": "soft", "reason_code": "communication_failure"},
            )
            await harness.setup_automations("hoymiles_battery_balancing_control")
            harness.set_state("sensor.hoymiles_actual_load_power", label)
            if expected == "guard":
                await harness.wait_until(
                    lambda: harness.state(
                        "script.hoymiles_battery_balancing_soft_gap_guard"
                    )
                    == "on",
                    timeout=5,
                    message=f"{label} did not re-arm the durable gap guard",
                )
                assert harness.attr(
                    "sensor.hoymiles_battery_balancing_timing_transaction",
                    "gap_deadline_epoch_ms",
                ) == gap_deadline
                # Repeated stale updates cannot move the accepted deadline.
                harness.set_state("sensor.hoymiles_actual_load_power", label + "-2")
                await asyncio.sleep(0.1)
                assert harness.attr(
                    "sensor.hoymiles_battery_balancing_timing_transaction",
                    "gap_deadline_epoch_ms",
                ) == gap_deadline
                await harness.call(
                    "script",
                    "turn_off",
                    {
                        "entity_id": (
                            "script.hoymiles_battery_balancing_soft_gap_guard"
                        )
                    },
                )
            else:
                await harness.wait_until(
                    lambda: harness.state(
                        "sensor.hoymiles_battery_balancing_abort_request"
                    )
                    == "PENDING"
                    and harness.attr(
                        "sensor.hoymiles_battery_balancing_abort_request",
                        "reason_code",
                    )
                    == expected,
                    timeout=7,
                    message=f"{label} did not classify {expected}",
                )
                assert harness.attr(
                    "sensor.hoymiles_battery_balancing_timing_transaction",
                    "gap_deadline_epoch_ms",
                ) in {gap_deadline, 0}

        await with_harness(package, scenario)

    return len(cases)


async def test_hold_restart_protocol(package: dict[str, Any]) -> int:
    async def prepare_hold(
        harness: RuntimeHarness,
        *,
        state: str,
        timer_active: bool,
        deadline_delta_ms: int = 3_600_000,
        soc: float = 99.9,
    ) -> tuple[str, int]:
        seed_state = "SLOW" if state == "HOLD_ARMING" else state
        cycle = await harness.seed_cycle(
            state=seed_state,
            register_4303=100,
            register_4304=8,
            operational_started=True,
        )
        harness.set_state("sensor.hoymiles_hit_overview_battery_soc", soc)
        deadline = harness.now_ms() + deadline_delta_ms
        next_hold_generation = int(
            harness.attr(
                "sensor.hoymiles_battery_balancing_timing_transaction",
                "hold_generation",
            )
        ) + 1
        await harness.call_script(
            "hoymiles_battery_balancing_write_timing",
            cycle_id=cycle,
            gap_generation=2,
            gap_start_epoch_ms=0,
            gap_deadline_epoch_ms=0,
            hold_generation=next_hold_generation,
            hold_deadline_epoch_ms=deadline,
            timing_state=state,
        )
        if state == "HOLD_ARMING":
            await harness.call_script(
                "hoymiles_battery_balancing_transition",
                expected_cycle_id=cycle,
                transaction_state="HOLD_ARMING",
                steady_commit_guard=True,
                expected_owner_generation=harness.attr(
                    "sensor.hoymiles_battery_balancing_transaction",
                    "owner_generation",
                ),
            )
        if timer_active:
            await harness.call(
                "timer",
                "start",
                {
                    "entity_id": "timer.hoymiles_battery_balancing_hold",
                    "duration": 3600,
                },
            )
        await harness.hass.async_block_till_done()
        return cycle, deadline

    async def active_arming(harness: RuntimeHarness) -> None:
        cycle, _ = await prepare_hold(
            harness, state="HOLD_ARMING", timer_active=True
        )
        start_at = len(harness.service_events)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="hold_reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "HOLDING",
            timeout=5,
            message="HOLD_ARMING with active timer was not adopted",
        )
        await asyncio.sleep(0.1)
        unexpected_starts = [
            data
            for domain, service, data in harness.service_events[start_at:]
            if domain == "timer" and service == "start"
        ]
        assert not unexpected_starts, unexpected_starts

    async def idle_arming(harness: RuntimeHarness) -> None:
        cycle, deadline = await prepare_hold(
            harness, state="HOLD_ARMING", timer_active=False
        )
        start_at = len(harness.service_events)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="hold_reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "HOLDING",
            timeout=5,
            message="future HOLD_ARMING did not resume remaining duration",
        )
        await asyncio.sleep(0.1)
        timer_starts = [
            data
            for domain, service, data in harness.service_events[start_at:]
            if domain == "timer" and service == "start"
        ]
        assert len(timer_starts) == 1, timer_starts
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_timing_transaction",
            "hold_deadline_epoch_ms",
        ) == deadline

    async def holding_active(harness: RuntimeHarness) -> None:
        cycle, _ = await prepare_hold(
            harness, state="HOLDING", timer_active=True
        )
        start_at = len(harness.service_events)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="hold_reconcile",
            expected_cycle_id=cycle,
        )
        await asyncio.sleep(0.1)
        assert harness.state(
            "sensor.hoymiles_battery_balancing_transaction"
        ) == "HOLDING"
        assert not any(
            domain == "timer" and service == "start"
            for domain, service, _ in harness.service_events[start_at:]
        )

    async def expired_once(harness: RuntimeHarness) -> None:
        cycle, _ = await prepare_hold(
            harness,
            state="HOLDING",
            timer_active=False,
            deadline_delta_ms=-1,
        )
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="hold_reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=8,
            message="expired hold did not complete exactly once",
        )
        terminal_ids = {
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_1_event_id",
            ),
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_2_event_id",
            ),
        }
        assert "c1.CO" in terminal_ids

    async def soc_drop(harness: RuntimeHarness) -> None:
        cycle, _ = await prepare_hold(
            harness,
            state="HOLDING",
            timer_active=True,
            soc=99.8,
        )
        start_at = len(harness.service_events)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="hold_reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "SLOW",
            timeout=5,
            message="SOC drop did not invalidate the hold",
        )
        await asyncio.sleep(0.1)
        cancels = [
            data
            for domain, service, data in harness.service_events[start_at:]
            if domain == "timer" and service == "cancel"
        ]
        assert len(cancels) == 1, cancels
        assert harness.state(
            "sensor.hoymiles_battery_balancing_timing_transaction"
        ) == "NONE"

    async def stale_event(harness: RuntimeHarness) -> None:
        _, deadline = await prepare_hold(
            harness, state="HOLDING", timer_active=True
        )
        await harness.call(
            "timer",
            "start",
            {"entity_id": "timer.hoymiles_battery_balancing_watchdog"},
        )
        await harness.setup_automations("hoymiles_battery_balancing_control")
        start_at = len(harness.service_events)
        harness.hass.bus.async_fire(
            "hoymiles_battery_balancing_hold_deadline",
            {
                "cycle_id": "c1",
                "hold_generation": 8,
                "hold_deadline_epoch_ms": deadline,
            },
        )
        await asyncio.sleep(0.15)
        assert harness.state(
            "sensor.hoymiles_battery_balancing_transaction"
        ) == "HOLDING"
        assert not any(
            domain == "script"
            and service == "hoymiles_battery_balancing_request_abort"
            for domain, service, _ in harness.service_events[start_at:]
        )

    scenarios = (
        active_arming,
        idle_arming,
        holding_active,
        expired_once,
        soc_drop,
        stale_event,
    )
    for scenario in scenarios:
        await with_harness(package, scenario)
    return len(scenarios)


async def test_fresh_soc_before_hold(package: dict[str, Any]) -> int:
    """A helper-time SOC drop must prevent HOLD_ARMING and timer start."""

    async def scenario(harness: RuntimeHarness) -> None:
        harness.set_state("sensor.hoymiles_hit_overview_battery_soc", 99.9)
        cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
        harness.block_service = "hoymiles_verified_set_ems_maximum_charge_power"
        await harness.call(
            "script",
            "turn_on",
            {
                "entity_id": (
                    "script.hoymiles_battery_balancing_transaction_worker"
                ),
                "variables": {
                    "intent": "reconcile",
                    "expected_cycle_id": cycle,
                },
            },
        )
        await asyncio.wait_for(harness.block_entered.wait(), timeout=5)
        harness.set_state("sensor.hoymiles_hit_overview_battery_soc", 99.8)
        harness.block_release.set()
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "SLOW",
            timeout=8,
            message="stale 99.9% SOC armed the hold after a 99.8% reread",
        )
        await harness.wait_worker_idle()
        assert harness.state("timer.hoymiles_battery_balancing_hold") == "idle"
        assert harness.state(
            "sensor.hoymiles_battery_balancing_timing_transaction"
        ) == "NONE"
        assert not any(
            data.get("transaction_state") in {"HOLD_ARMING", "HOLDING"}
            for domain, service, data in harness.service_events
            if domain == "script"
            and service == "hoymiles_battery_balancing_transition"
        )

    await with_harness(package, scenario)

    async def serializer_boundary(harness: RuntimeHarness) -> None:
        harness.set_state("sensor.hoymiles_hit_overview_battery_soc", 99.9)
        cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
        flipped = False

        @callback
        def boundary_listener(event: Event) -> None:
            nonlocal flipped
            if (
                event.data.get("domain") == "script"
                and event.data.get("service")
                == "hoymiles_battery_balancing_write_timing"
                and event.data.get("service_data", {}).get("timing_state")
                == "HOLD_ARMING"
                and not flipped
            ):
                flipped = True
                harness.set_state("sensor.hoymiles_hit_overview_battery_soc", 99.8)

        harness.hass.bus.async_listen(EVENT_CALL_SERVICE, boundary_listener)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_worker_idle()
        assert flipped
        assert harness.state("sensor.hoymiles_battery_balancing_transaction") == "SLOW"
        assert harness.state("sensor.hoymiles_battery_balancing_timing_transaction") == "NONE"
        assert harness.state("timer.hoymiles_battery_balancing_hold") == "idle"
        assert not any(
            data.get("transaction_state") in {"HOLD_ARMING", "HOLDING"}
            for domain, service, data in harness.service_events
            if domain == "script"
            and service == "hoymiles_battery_balancing_transition"
        )

    await with_harness(package, serializer_boundary)

    async def inner_input_text_boundary(harness: RuntimeHarness) -> None:
        harness.set_state("sensor.hoymiles_hit_overview_battery_soc", 99.9)
        cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
        flipped = False

        def hook(domain: str, service: str, data: dict[str, Any]) -> None:
            nonlocal flipped
            entity_ids = data.get("entity_id", [])
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]
            fields = str(data.get("value", "")).split("|")
            if (
                domain == "input_text"
                and service == "set_value"
                and "input_text.hoymiles_battery_balancing_timing" in entity_ids
                and len(fields) == 8
                and fields[1] == cycle
                and fields[7] == "HOLD_ARMING"
                and not flipped
            ):
                flipped = True
                harness.set_state(
                    "sensor.hoymiles_hit_overview_battery_soc", 99.8
                )

        install_boundary_hook(harness, hook)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_worker_idle()
        assert flipped
        assert harness.state(
            "sensor.hoymiles_battery_balancing_transaction"
        ) == "SLOW"
        assert harness.state(
            "sensor.hoymiles_battery_balancing_timing_transaction"
        ) == "NONE"
        assert harness.state("timer.hoymiles_battery_balancing_hold") == "idle"
        assert not any(
            data.get("transaction_state") in {"HOLD_ARMING", "HOLDING"}
            for domain, service, data in harness.service_events
            if domain == "script"
            and service == "hoymiles_battery_balancing_transition"
        )

    await with_harness(package, inner_input_text_boundary)
    return 3


async def test_terminal_and_notification_outbox(package: dict[str, Any]) -> int:
    async def started_failure(harness: RuntimeHarness) -> None:
        cycle = await harness.seed_cycle(
            state="OPERATIONAL", operational_started=True
        )
        await harness.call_script(
            "hoymiles_notify_battery_balancing_lifecycle",
            event="started",
            reason_code="none",
            cycle_id=cycle,
        )
        await harness.call_script(
            "hoymiles_battery_balancing_request_abort",
            reason_code="user_disabled",
            cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=8,
            message="started failure did not close",
        )
        await harness.call_script(
            "hoymiles_battery_balancing_request_abort",
            reason_code="user_disabled",
            cycle_id=cycle,
        )
        await harness.call_script(
            "hoymiles_stop_battery_balancing", reason_code="user_disabled"
        )
        ids = [
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_1_event_id",
            ),
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_2_event_id",
            ),
        ]
        assert ids.count("c1.ST") == 1, ids
        assert ids.count("c1.AB") == 1, ids
        assert not any(item == "c1.FA" for item in ids)

    async def preoperational_failure(harness: RuntimeHarness) -> None:
        cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
        await harness.call_script(
            "hoymiles_battery_balancing_request_abort",
            reason_code="communication_failure",
            cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=8,
            message="pre-operational failure did not close",
        )
        ids = {
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_1_event_id",
            ),
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_2_event_id",
            ),
        }
        assert "c1.FA" in ids
        assert "c1.ST" not in ids and "c1.AB" not in ids

    for scenario in (started_failure, preoperational_failure):
        await with_harness(package, scenario)

    pending_harness = RuntimeHarness(package)
    try:
        await pending_harness.setup()
        await pending_harness.call_script(
            "hoymiles_notify_battery_balancing_lifecycle",
            event="failed",
            reason_code="communication_failure",
            cycle_id="c41",
        )
        pending_raw = pending_harness.state(
            "input_text.hoymiles_battery_balancing_notification_outbox"
        )
        assert pending_harness.attr(
            "sensor.hoymiles_battery_balancing_notification_outbox",
            "slot_1_delivery_state",
        ) == "PENDING"
    finally:
        await pending_harness.close()

    delivered_tags: list[str] = []
    for _ in range(2):
        retry_harness = RuntimeHarness(package)
        try:
            await retry_harness.setup()
            await retry_harness.call(
                "input_text",
                "set_value",
                {
                    "entity_id": (
                        "input_text.hoymiles_battery_balancing_notification_outbox"
                    ),
                    "value": pending_raw,
                },
            )
            await retry_harness.call(
                "input_boolean",
                "turn_on",
                {
                    "entity_id": (
                        "input_boolean.hoymiles_ems_push_notifications_enabled"
                    )
                },
            )
            await retry_harness.call_script(
                "hoymiles_battery_balancing_notification_dispatcher"
            )
            await asyncio.sleep(0.1)
            assert len(retry_harness.notify_calls) == 1
            tag = retry_harness.notify_calls[0]["data"]["tag"]
            delivered_tags.append(tag)
            assert retry_harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_1_delivery_state",
            ) == "DELIVERED"
            assert retry_harness.notify_observations == [("DELIVERING", 1)]
            await retry_harness.call_script(
                "hoymiles_battery_balancing_notification_dispatcher"
            )
            assert len(retry_harness.notify_calls) == 1
        finally:
            await retry_harness.close()
    # This second run models restart after provider return but before the
    # DELIVERED ledger write: a retry is possible, but the visible tag is stable.
    assert delivered_tags == ["bb.c41.FA", "bb.c41.FA"]

    async def slow_phone_does_not_delay_restore(
        harness: RuntimeHarness,
    ) -> None:
        await harness.call_script(
            "hoymiles_notify_battery_balancing_lifecycle",
            event="failed",
            reason_code="communication_failure",
            cycle_id="c77",
        )
        await harness.call(
            "input_boolean",
            "turn_on",
            {
                "entity_id": (
                    "input_boolean.hoymiles_ems_push_notifications_enabled"
                )
            },
        )
        cycle = await harness.seed_cycle(state="APPLYING")
        harness.block_notify = True
        await harness.call(
            "script",
            "turn_on",
            {
                "entity_id": (
                    "script.hoymiles_battery_balancing_notification_dispatcher"
                )
            },
        )
        await asyncio.wait_for(harness.notify_entered.wait(), timeout=5)
        await harness.call_script(
            "hoymiles_battery_balancing_request_abort",
            reason_code="user_disabled",
            cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=8,
            message="blocked phone provider delayed physical closeout",
        )
        assert harness.state(
            "input_boolean.hoymiles_battery_balancing_active"
        ) == "off"
        assert harness.physical_calls
        assert not harness.notify_release.is_set()
        harness.notify_release.set()

    await with_harness(package, slow_phone_does_not_delay_restore)
    return 5


async def test_notification_provider_timeout(package: dict[str, Any]) -> int:
    """Exercise the exact 15-second lease, restart and stale-result guards."""

    provider_script = package["script"][
        "hoymiles_battery_balancing_notification_provider_attempt"
    ]
    timeout_script = package["script"][
        "hoymiles_battery_balancing_notification_attempt_timeout"
    ]
    provider_boundary = next(
        str(step["variables"]["provider_return_before_deadline"])
        for step in provider_script["sequence"]
        if isinstance(step, dict)
        and "provider_return_before_deadline" in step.get("variables", {})
    )
    timeout_boundary = next(
        str(step["variables"]["deadline_reached"])
        for step in timeout_script["sequence"]
        if isinstance(step, dict)
        and "deadline_reached" in step.get("variables", {})
    )
    base_ms = 2_000_000_000_000
    deadline_ms = base_ms + 15_000
    boundary_fixtures = (
        (14_999, True, False),
        (15_000, False, True),
    )
    with tempfile.TemporaryDirectory(
        prefix="hoymiles-notify-boundary-ha-2026.8.2-"
    ) as config:
        hass = HomeAssistant(config)
        provider_template = Template(provider_boundary, hass)
        timeout_template = Template(timeout_boundary, hass)
        for elapsed_ms, provider_expected, timeout_expected in boundary_fixtures:
            frozen_now = datetime.fromtimestamp(
                (base_ms + elapsed_ms) / 1000, tz=timezone.utc
            )
            with patch("homeassistant.util.dt.now", return_value=frozen_now):
                provider_result = bool(
                    provider_template.async_render(
                        {"leased_deadline_ms": deadline_ms}
                    )
                )
                timeout_result = bool(
                    timeout_template.async_render(
                        {"guarded_deadline_ms": deadline_ms}
                    )
                )
            assert provider_result is provider_expected
            assert timeout_result is timeout_expected

    hung = RuntimeHarness(package)
    try:
        await hung.setup()
        owner_before = hung.state("input_boolean.hoymiles_battery_balancing_active")
        for cycle, event, reason in (
            ("c80", "failed", "communication_failure"),
            ("c81", "aborted", "user_disabled"),
        ):
            await hung.call_script(
                "hoymiles_notify_battery_balancing_lifecycle",
                event=event,
                reason_code=reason,
                cycle_id=cycle,
            )
        await hung.call(
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.hoymiles_ems_push_notifications_enabled"},
        )
        hung.block_notify = True
        from time import monotonic

        started = monotonic()
        await hung.call(
            "script",
            "turn_on",
            {
                "entity_id": (
                    "script.hoymiles_battery_balancing_notification_dispatcher"
                )
            },
        )
        await hung.wait_until(
            lambda: len(hung.notify_calls) >= 2,
            timeout=5,
            message="a hung provider starved the second occupied outbox slot",
        )
        assert hung.state(
            "script.hoymiles_battery_balancing_notification_dispatcher"
        ) != "on"
        assert {
            hung.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_1_delivery_state",
            ),
            hung.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_2_delivery_state",
            ),
        } == {"DELIVERING"}
        await hung.wait_until(
            lambda: len(hung.notify_calls) >= 4,
            timeout=18,
            message="15-second provider leases did not expire and retry",
        )
        elapsed = monotonic() - started
        assert elapsed >= 15.0
        assert hung.attr(
            "sensor.hoymiles_battery_balancing_notification_outbox",
            "slot_1_attempt_count",
        ) == 2
        assert hung.attr(
            "sensor.hoymiles_battery_balancing_notification_outbox",
            "slot_2_attempt_count",
        ) == 2
        assert hung.notify_peak <= 4
        hung.notify_release.set()
        await hung.wait_until(
            lambda: {
                hung.attr(
                    "sensor.hoymiles_battery_balancing_notification_outbox",
                    "slot_1_delivery_state",
                ),
                hung.attr(
                    "sensor.hoymiles_battery_balancing_notification_outbox",
                    "slot_2_delivery_state",
                ),
            }
            == {"DELIVERED"},
            timeout=5,
            message="current retry leases were not delivered",
        )
        tags = [str(call["data"]["tag"]) for call in hung.notify_calls]
        assert tags.count("bb.c80.FA") == 2
        assert tags.count("bb.c81.AB") == 2
        assert hung.state(
            "input_boolean.hoymiles_battery_balancing_active"
        ) == owner_before
        assert not hung.physical_calls
    finally:
        await hung.close()

    async def provider_exception(harness: RuntimeHarness) -> None:
        await harness.call_script(
            "hoymiles_notify_battery_balancing_lifecycle",
            event="failed",
            reason_code="communication_failure",
            cycle_id="c82",
        )
        await harness.call_script(
            "hoymiles_battery_balancing_update_outbox_delivery",
            event_id="c82.FA",
            expected_delivery_state="PENDING",
            expected_attempt_count=0,
            next_delivery_state="DELIVERING",
            next_attempt_count=3,
        )
        harness.notify_raise = True
        deadline = harness.now_ms() + 250
        for service in (
            "hoymiles_battery_balancing_notification_attempt_timeout",
            "hoymiles_battery_balancing_notification_provider_attempt",
        ):
            variables: dict[str, Any] = {
                "event_id": "c82.FA",
                "lease_generation": 3,
                "attempt_deadline_epoch_ms": deadline,
            }
            if service.endswith("provider_attempt"):
                variables.update(
                    {
                        "target_entity": "notify.offline_phone",
                        "selected_cycle": "c82",
                        "selected_kind": "FA",
                        "selected_reason": "communication_failure",
                        "selected_tag": "bb.c82.FA",
                    }
                )
            await harness.call(
                "script",
                "turn_on",
                {"entity_id": f"script.{service}", "variables": variables},
            )
        await harness.wait_until(
            lambda: harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_1_delivery_state",
            )
            == "PERMANENT_FAILURE",
            timeout=3,
            message="provider exception escaped the bounded attempt policy",
        )
        assert len(harness.notify_calls) == 1

    await with_harness(package, provider_exception)

    first = RuntimeHarness(package)
    try:
        await first.setup()
        await first.call_script(
            "hoymiles_notify_battery_balancing_lifecycle",
            event="completed",
            reason_code="completed",
            cycle_id="c83",
        )
        await first.call_script(
            "hoymiles_battery_balancing_update_outbox_delivery",
            event_id="c83.CO",
            expected_delivery_state="PENDING",
            expected_attempt_count=0,
            next_delivery_state="DELIVERING",
            next_attempt_count=1,
        )
        restart_raw = first.state(
            "input_text.hoymiles_battery_balancing_notification_outbox"
        )
    finally:
        await first.close()

    second = RuntimeHarness(package)
    try:
        await second.setup()
        await second.call(
            "input_text",
            "set_value",
            {
                "entity_id": (
                    "input_text.hoymiles_battery_balancing_notification_outbox"
                ),
                "value": restart_raw,
            },
        )
        await second.call_script(
            "hoymiles_battery_balancing_recover_notification_leases"
        )
        assert second.attr(
            "sensor.hoymiles_battery_balancing_notification_outbox",
            "slot_1_delivery_state",
        ) == "PENDING"
        assert second.attr(
            "sensor.hoymiles_battery_balancing_notification_outbox",
            "slot_1_attempt_count",
        ) == 1
        await second.call(
            "input_boolean",
            "turn_on",
            {"entity_id": "input_boolean.hoymiles_ems_push_notifications_enabled"},
        )
        await second.call_script(
            "hoymiles_battery_balancing_notification_dispatcher"
        )
        await second.wait_until(
            lambda: second.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_1_delivery_state",
            )
            == "DELIVERED",
            timeout=5,
            message="restart recovery did not grant a new delivery lease",
        )
        assert second.attr(
            "sensor.hoymiles_battery_balancing_notification_outbox",
            "slot_1_attempt_count",
        ) == 2
        assert second.notify_calls[0]["data"]["tag"] == "bb.c83.CO"
    finally:
        await second.close()

    return 2 + 5 + 1 + 2


async def test_exact_generation_drift(package: dict[str, Any]) -> int:
    """Reject any post-snapshot EMS or topology generation drift."""

    async def scenario(harness: RuntimeHarness, entity_id: str, value: int) -> None:
        cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
        harness.set_state(entity_id, value)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=8,
            message=f"generation drift was not closed: {entity_id}",
        )
        assert not harness.physical_calls
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "write_started"
        ) is False
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "reason_code"
        ) == "snapshot_changed_before_transaction"

    fixtures = (
        ("sensor.hoymiles_hit_ems_control_readback_generation", 11),
        ("sensor.hoymiles_hit_parallel_topology_readback_generation", 21),
    )
    for entity_id, value in fixtures:
        async def run(
            harness: RuntimeHarness,
            entity_id: str = entity_id,
            value: int = value,
        ) -> None:
            await scenario(harness, entity_id, value)

        await with_harness(package, run)

    for entity_id, value in fixtures:
        async def boundary_run(
            harness: RuntimeHarness,
            entity_id: str = entity_id,
            value: int = value,
        ) -> None:
            cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
            changed = False

            @callback
            def boundary_listener(event: Event) -> None:
                nonlocal changed
                if (
                    event.data.get("domain") == "script"
                    and event.data.get("service")
                    == "hoymiles_battery_balancing_write_record"
                    and event.data.get("service_data", {}).get("transaction_state")
                    == "APPLYING"
                    and not changed
                ):
                    changed = True
                    harness.set_state(entity_id, value)

            harness.hass.bus.async_listen(EVENT_CALL_SERVICE, boundary_listener)
            await harness.call_script(
                "hoymiles_battery_balancing_transaction_worker",
                intent="reconcile",
                expected_cycle_id=cycle,
            )
            await harness.wait_until(
                lambda: harness.state(
                    "sensor.hoymiles_battery_balancing_transaction"
                )
                == "TERMINAL",
                timeout=8,
                message=f"generation drift at APPLYING was not closed: {entity_id}",
            )
            assert changed
            assert not harness.physical_calls
            assert harness.attr(
                "sensor.hoymiles_battery_balancing_transaction", "write_started"
            ) is False
            assert harness.attr(
                "sensor.hoymiles_battery_balancing_transaction", "reason_code"
            ) == "snapshot_changed_before_transaction"

        await with_harness(package, boundary_run)

    for entity_id, value in fixtures:
        async def inner_input_text_boundary_run(
            harness: RuntimeHarness,
            entity_id: str = entity_id,
            value: int = value,
        ) -> None:
            cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
            changed = False

            def hook(domain: str, service: str, data: dict[str, Any]) -> None:
                nonlocal changed
                entity_ids = data.get("entity_id", [])
                if isinstance(entity_ids, str):
                    entity_ids = [entity_ids]
                fields = str(data.get("value", "")).split("|")
                if (
                    domain == "input_text"
                    and service == "set_value"
                    and "input_text.hoymiles_battery_balancing_lifecycle"
                    in entity_ids
                    and len(fields) == 18
                    and fields[1] == cycle
                    and fields[2] == "APPLYING"
                    and fields[13] == "1"
                    and fields[16] == "0"
                    and not changed
                ):
                    changed = True
                    harness.set_state(entity_id, value)

            install_boundary_hook(harness, hook)
            await harness.call_script(
                "hoymiles_battery_balancing_transaction_worker",
                intent="reconcile",
                expected_cycle_id=cycle,
            )
            await harness.wait_until(
                lambda: harness.state(
                    "sensor.hoymiles_battery_balancing_transaction"
                )
                == "TERMINAL",
                timeout=8,
                message=(
                    "generation drift at the inner input_text APPLYING boundary "
                    f"was not closed: {entity_id}"
                ),
            )
            assert changed
            assert not harness.physical_calls
            assert harness.attr(
                "sensor.hoymiles_battery_balancing_transaction", "write_started"
            ) is False
            assert harness.attr(
                "sensor.hoymiles_battery_balancing_transaction", "reason_code"
            ) == "snapshot_changed_before_transaction"

        await with_harness(package, inner_input_text_boundary_run)
    return len(fixtures) * 3


async def test_snapshot_and_sun_transactions(package: dict[str, Any]) -> int:
    async def changed_snapshot(harness: RuntimeHarness) -> None:
        cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
        harness.set_state(
            "sensor.hoymiles_hit_ems_maximum_charge_power_readback", 49
        )
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=8,
            message="changed snapshot did not fail before first write",
        )
        assert not harness.physical_calls
        assert harness.state(
            "input_boolean.hoymiles_battery_balancing_active"
        ) == "off"
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "reason_code"
        ) == "snapshot_changed_before_transaction"

    async def stale_snapshot(harness: RuntimeHarness) -> None:
        cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
        await harness.call_script(
            "hoymiles_battery_balancing_write_record",
            cycle_id=cycle,
            transaction_state="SNAPSHOT_VALID",
            owner="balancing",
            owner_generation=1,
            snapshot_valid=True,
            snapshot_cycle_id=cycle,
            snapshot_mode="self_use",
            snapshot_4303=80,
            snapshot_4304=50,
            snapshot_ems_generation=10,
            snapshot_topology_generation=20,
            snapshot_epoch_ms=harness.now_ms() - 60_001,
            write_started=False,
            reason_code="none",
            operational_started=False,
            cooldown_epoch_ms=0,
            event_marker="NONE",
        )
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=8,
            message="stale snapshot was accepted for a first write",
        )
        assert not harness.physical_calls

    async def sun_during_mode(
        harness: RuntimeHarness,
        *,
        initial_sun: str,
        initial_mode: str,
        changed_sun: str,
        expected_modes: list[str],
        expected_power: float,
    ) -> None:
        harness.set_state("sun.sun", initial_sun)
        cycle = await harness.seed_cycle(
            state="SNAPSHOT_VALID",
            mode=initial_mode,
            register_4303=100,
            register_4304=8,
        )
        harness.block_service = "hoymiles_verified_set_ems_mode"
        await harness.call(
            "script",
            "turn_on",
            {
                "entity_id": (
                    "script.hoymiles_battery_balancing_transaction_worker"
                ),
                "variables": {
                    "intent": "reconcile",
                    "expected_cycle_id": cycle,
                },
            },
        )
        await asyncio.wait_for(harness.block_entered.wait(), timeout=5)
        harness.set_state("sun.sun", changed_sun)
        harness.block_release.set()
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            in {"OPERATIONAL", "SLOW"},
            timeout=8,
            message="sun transition did not commit the current mode",
        )
        mode_calls = [
            str(item.data["option"])
            for item in harness.physical_calls
            if item.service == "hoymiles_verified_set_ems_mode"
        ]
        assert mode_calls == expected_modes, mode_calls
        assert harness.state("sensor.hoymiles_ems_hardware_mode") == expected_modes[-1]
        assert abs(
            float(
                harness.state(
                    "sensor.hoymiles_hit_ems_maximum_charge_power_readback"
                )
            )
            - expected_power
        ) < 0.05

    await with_harness(package, changed_snapshot)
    await with_harness(package, stale_snapshot)

    async def sunrise(harness: RuntimeHarness) -> None:
        await sun_during_mode(
            harness,
            initial_sun="below_horizon",
            initial_mode="self_use",
            changed_sun="above_horizon",
            expected_modes=["grid_charge", "self_use"],
            expected_power=8,
        )

    async def sunset(harness: RuntimeHarness) -> None:
        await sun_during_mode(
            harness,
            initial_sun="above_horizon",
            initial_mode="grid_charge",
            changed_sun="below_horizon",
            expected_modes=["self_use", "grid_charge"],
            expected_power=12,
        )

    await with_harness(package, sunrise)
    await with_harness(package, sunset)

    async def inner_one_flip(
        harness: RuntimeHarness,
        *,
        initial_sun: str,
        initial_mode: str,
        boundary_sun: str,
        expected_mode: str,
    ) -> None:
        harness.set_state("sun.sun", initial_sun)
        cycle = await harness.seed_cycle(
            state="SNAPSHOT_VALID", mode=initial_mode
        )
        flipped = False

        def hook(domain: str, service: str, data: dict[str, Any]) -> None:
            nonlocal flipped
            if (
                domain == "script"
                and service == "hoymiles_battery_balancing_write_record"
                and data.get("transaction_state") == "OPERATIONAL"
                and not flipped
            ):
                flipped = True
                harness.set_state("sun.sun", boundary_sun)

        install_boundary_hook(harness, hook)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "OPERATIONAL",
            timeout=10,
            message="inner steady serializer flip was not corrected",
        )
        await harness.wait_worker_idle()
        assert flipped
        assert harness.state("sun.sun") == boundary_sun
        assert harness.state("sensor.hoymiles_ems_hardware_mode") == expected_mode
        mode_calls = [
            call.data.get("option")
            for call in harness.physical_calls
            if call.service == "hoymiles_verified_set_ems_mode"
        ]
        assert mode_calls == [
            "grid_charge" if initial_sun == "below_horizon" else "self_use",
            expected_mode,
        ]
        operational_records = [
            fields
            for fields in committed_lifecycle_fields(harness)
            if fields[1] == cycle
            and fields[2] == "APPLYING"
            and fields[16] in {"101", "102"}
        ]
        assert len(operational_records) == 2, operational_records
        assert {
            (fields[15], fields[17]) for fields in operational_records
        } == {("0", "NONE"), ("1", "STARTED")}, operational_records

    async def inner_sunrise(harness: RuntimeHarness) -> None:
        await inner_one_flip(
            harness,
            initial_sun="below_horizon",
            initial_mode="self_use",
            boundary_sun="above_horizon",
            expected_mode="self_use",
        )

    async def inner_sunset(harness: RuntimeHarness) -> None:
        await inner_one_flip(
            harness,
            initial_sun="above_horizon",
            initial_mode="grid_charge",
            boundary_sun="below_horizon",
            expected_mode="grid_charge",
        )

    await with_harness(package, inner_sunrise)
    await with_harness(package, inner_sunset)
    return 4


async def test_final_sun_phase_commit_races(package: dict[str, Any]) -> int:
    """Re-read sun/mode at the exact final transition service boundary."""

    async def one_flip(
        harness: RuntimeHarness,
        *,
        initial_sun: str,
        initial_mode: str,
        boundary_sun: str,
        expected_mode: str,
    ) -> None:
        harness.set_state("sun.sun", initial_sun)
        cycle = await harness.seed_cycle(
            state="SNAPSHOT_VALID", mode=initial_mode
        )
        flips = 0

        def hook(domain: str, service: str, data: dict[str, Any]) -> None:
            nonlocal flips
            if (
                domain == "script"
                and service == "hoymiles_battery_balancing_transition"
                and data.get("steady_commit_guard") is True
                and data.get("transaction_state") == "OPERATIONAL"
                and flips == 0
            ):
                flips += 1
                harness.set_state("sun.sun", boundary_sun)

        install_boundary_hook(harness, hook)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "OPERATIONAL",
            timeout=10,
            message="final sun flip did not reach a valid corrected commit",
        )
        assert flips == 1
        assert harness.state("sun.sun") == boundary_sun
        assert harness.state("sensor.hoymiles_ems_hardware_mode") == expected_mode
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction",
            "operational_started",
        ) is True
        mode_calls = [
            call.data.get("option")
            for call in harness.physical_calls
            if call.service == "hoymiles_verified_set_ems_mode"
        ]
        assert mode_calls == [
            "grid_charge" if initial_sun == "below_horizon" else "self_use",
            expected_mode,
        ]

    async def sunrise(harness: RuntimeHarness) -> None:
        await one_flip(
            harness,
            initial_sun="below_horizon",
            initial_mode="self_use",
            boundary_sun="above_horizon",
            expected_mode="self_use",
        )

    async def sunset(harness: RuntimeHarness) -> None:
        await one_flip(
            harness,
            initial_sun="above_horizon",
            initial_mode="grid_charge",
            boundary_sun="below_horizon",
            expected_mode="grid_charge",
        )

    await with_harness(package, sunrise)
    await with_harness(package, sunset)

    async def second_flip(harness: RuntimeHarness) -> None:
        harness.set_state("sun.sun", "below_horizon")
        cycle = await harness.seed_cycle(
            state="SNAPSHOT_VALID", mode="self_use"
        )
        boundary_flipped = False
        correction_seen = False

        def hook(domain: str, service: str, data: dict[str, Any]) -> None:
            nonlocal boundary_flipped, correction_seen
            if (
                domain == "script"
                and service == "hoymiles_battery_balancing_transition"
                and data.get("steady_commit_guard") is True
                and data.get("transaction_state") == "OPERATIONAL"
                and not boundary_flipped
            ):
                boundary_flipped = True
                harness.set_state("sun.sun", "above_horizon")
            elif (
                domain == "script"
                and service == "hoymiles_verified_set_ems_mode"
                and boundary_flipped
                and not correction_seen
            ):
                correction_seen = True
                harness.set_state("sun.sun", "below_horizon")

        install_boundary_hook(harness, hook)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=10,
            message="second sun flip did not fail closed",
        )
        assert boundary_flipped and correction_seen
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "reason_code"
        ) == "mode_ack_failed"
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction",
            "operational_started",
        ) is False

    await with_harness(package, second_flip)

    async def second_inner_flip(harness: RuntimeHarness) -> None:
        harness.set_state("sun.sun", "below_horizon")
        cycle = await harness.seed_cycle(
            state="SNAPSHOT_VALID", mode="self_use"
        )
        write_count = 0

        def hook(domain: str, service: str, data: dict[str, Any]) -> None:
            nonlocal write_count
            if (
                domain == "script"
                and service == "hoymiles_battery_balancing_write_record"
                and data.get("transaction_state") == "OPERATIONAL"
            ):
                write_count += 1
                harness.set_state(
                    "sun.sun",
                    "above_horizon" if write_count == 1 else "below_horizon",
                )

        install_boundary_hook(harness, hook)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=10,
            message="second inner sun flip did not fail closed",
        )
        assert write_count == 2
        assert not [
            fields
            for fields in committed_lifecycle_fields(harness)
            if fields[1] == cycle
            and fields[2] == "APPLYING"
            and fields[16] in {"101", "102"}
        ]
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "reason_code"
        ) == "mode_ack_failed"

    await with_harness(package, second_inner_flip)

    async def hard_stop_during_correction(harness: RuntimeHarness) -> None:
        harness.set_state("sun.sun", "below_horizon")
        cycle = await harness.seed_cycle(
            state="SNAPSHOT_VALID", mode="self_use"
        )
        boundary_flipped = False
        correction_blocked = False

        def hook(domain: str, service: str, data: dict[str, Any]) -> None:
            nonlocal boundary_flipped, correction_blocked
            if (
                domain == "script"
                and service == "hoymiles_battery_balancing_transition"
                and data.get("steady_commit_guard") is True
                and data.get("transaction_state") == "OPERATIONAL"
                and not boundary_flipped
            ):
                boundary_flipped = True
                harness.set_state("sun.sun", "above_horizon")
            elif (
                domain == "script"
                and service == "hoymiles_verified_set_ems_mode"
                and boundary_flipped
                and not correction_blocked
            ):
                correction_blocked = True
                harness.block_service = "hoymiles_verified_set_ems_mode"
                harness.set_state(
                    "binary_sensor.hoymiles_ems_control_conflict", "on"
                )

        install_boundary_hook(harness, hook)
        await harness.call(
            "script",
            "turn_on",
            {
                "entity_id": (
                    "script.hoymiles_battery_balancing_transaction_worker"
                ),
                "variables": {
                    "intent": "reconcile",
                    "expected_cycle_id": cycle,
                },
            },
        )
        await asyncio.wait_for(harness.block_entered.wait(), timeout=8)
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_abort_request"
            )
            == "PENDING",
            timeout=5,
            message="hard-stop during final correction was not retained",
        )
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_abort_request", "reason_code"
        ) == "control_conflict"
        harness.block_release.set()
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            in {"TERMINAL", "RESTORE_FAILED"},
            timeout=10,
            message="hard-stop correction race did not close safely",
        )
        assert boundary_flipped and correction_blocked
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "reason_code"
        ) == "control_conflict"
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction",
            "operational_started",
        ) is False

    await with_harness(
        package,
        hard_stop_during_correction,
        automations=HARD_STOP_AUTOMATIONS,
    )

    async def started_fact_inner_flip(harness: RuntimeHarness) -> None:
        harness.set_state("sun.sun", "below_horizon")
        cycle = await harness.seed_cycle(
            state="SNAPSHOT_VALID", mode="self_use"
        )
        flipped = False

        def hook(domain: str, service: str, data: dict[str, Any]) -> None:
            nonlocal flipped
            operational_started = str(data.get("operational_started", "")).lower()
            if (
                domain == "script"
                and service == "hoymiles_battery_balancing_write_record"
                and data.get("transaction_state") == "OPERATIONAL"
                and operational_started in {"true", "1", "on"}
                and not flipped
            ):
                flipped = True
                harness.set_state("sun.sun", "above_horizon")

        install_boundary_hook(harness, hook)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=10,
            message="STARTED inner sun flip did not fail closed",
        )
        assert flipped
        operational_records = [
            fields
            for fields in committed_lifecycle_fields(harness)
            if fields[1] == cycle
            and fields[2] == "APPLYING"
            and fields[16] in {"101", "102"}
        ]
        assert [(fields[15], fields[17]) for fields in operational_records] == [
            ("0", "NONE")
        ], operational_records
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "reason_code"
        ) == "mode_ack_failed"

    await with_harness(package, started_fact_inner_flip)

    async def deepest_input_text_sun_flip(harness: RuntimeHarness) -> None:
        harness.set_state("sun.sun", "below_horizon")
        cycle = await harness.seed_cycle(
            state="SNAPSHOT_VALID", mode="self_use"
        )
        flipped = False
        semantic_commits: list[tuple[str, str]] = []

        @callback
        def observe_transaction(event: Event) -> None:
            if event.data.get("entity_id") != (
                "sensor.hoymiles_battery_balancing_transaction"
            ):
                return
            new_state = event.data.get("new_state")
            if new_state is not None and new_state.state == "OPERATIONAL":
                semantic_commits.append(
                    (
                        harness.state("sun.sun"),
                        harness.state("sensor.hoymiles_ems_hardware_mode"),
                    )
                )

        harness.hass.bus.async_listen("state_changed", observe_transaction)

        def hook(domain: str, service: str, data: dict[str, Any]) -> None:
            nonlocal flipped
            entity_ids = data.get("entity_id", [])
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]
            fields = str(data.get("value", "")).split("|")
            if (
                domain == "input_text"
                and service == "set_value"
                and "input_text.hoymiles_battery_balancing_lifecycle"
                in entity_ids
                and len(fields) == 18
                and fields[1] == cycle
                and fields[2] == "APPLYING"
                and fields[16] == "102"
                and not flipped
            ):
                flipped = True
                harness.set_state("sun.sun", "above_horizon")

        install_boundary_hook(harness, hook)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "OPERATIONAL",
            timeout=10,
            message="deepest input_text sun flip was not corrected",
        )
        await harness.wait_worker_idle()
        assert flipped
        assert semantic_commits
        assert all(
            sun == "above_horizon" and mode == "self_use"
            for sun, mode in semantic_commits
        ), semantic_commits
        assert not [
            fields
            for fields in committed_lifecycle_fields(harness)
            if fields[1] == cycle and fields[2] == "OPERATIONAL"
        ]
        assert not [
            data
            for domain, service, data in harness.service_events
            if domain == "input_text"
            and service == "set_value"
            and "input_text.hoymiles_battery_balancing_phase"
            in (
                [data.get("entity_id")]
                if isinstance(data.get("entity_id"), str)
                else data.get("entity_id", [])
            )
            and data.get("value") in {
                "OPERATIONAL", "SLOW", "HOLD_ARMING", "HOLDING"
            }
        ]

    await with_harness(package, deepest_input_text_sun_flip)

    async def deepest_input_text_owner_loss(harness: RuntimeHarness) -> None:
        harness.set_state("sun.sun", "below_horizon")
        cycle = await harness.seed_cycle(
            state="SNAPSHOT_VALID", mode="self_use"
        )
        owner_dropped = False
        semantic_commits_after_drop: list[str] = []

        @callback
        def observe_transaction(event: Event) -> None:
            if event.data.get("entity_id") != (
                "sensor.hoymiles_battery_balancing_transaction"
            ):
                return
            new_state = event.data.get("new_state")
            if (
                owner_dropped
                and new_state is not None
                and new_state.state
                in {"OPERATIONAL", "SLOW", "HOLD_ARMING", "HOLDING"}
            ):
                semantic_commits_after_drop.append(new_state.state)

        harness.hass.bus.async_listen("state_changed", observe_transaction)

        def hook(domain: str, service: str, data: dict[str, Any]) -> None:
            nonlocal owner_dropped
            entity_ids = data.get("entity_id", [])
            if isinstance(entity_ids, str):
                entity_ids = [entity_ids]
            fields = str(data.get("value", "")).split("|")
            if (
                domain == "input_text"
                and service == "set_value"
                and "input_text.hoymiles_battery_balancing_lifecycle"
                in entity_ids
                and len(fields) == 18
                and fields[1] == cycle
                and fields[2] == "APPLYING"
                and fields[16] == "102"
                and not owner_dropped
            ):
                owner_dropped = True
                harness.set_state(
                    "input_boolean.hoymiles_battery_balancing_active", "off"
                )

        install_boundary_hook(harness, hook)
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            in {"TERMINAL", "RESTORE_FAILED"},
            timeout=10,
            message="deepest owner-loss commit race did not restore",
        )
        assert owner_dropped
        assert not semantic_commits_after_drop, semantic_commits_after_drop
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "reason_code"
        ) == "ownership_lost"
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction",
            "operational_started",
        ) is False
        assert not [
            fields
            for fields in committed_lifecycle_fields(harness)
            if fields[1] == cycle and fields[2] == "OPERATIONAL"
        ]
        assert not [
            data
            for domain, service, data in harness.service_events
            if domain == "input_text"
            and service == "set_value"
            and "input_text.hoymiles_battery_balancing_phase"
            in (
                [data.get("entity_id")]
                if isinstance(data.get("entity_id"), str)
                else data.get("entity_id", [])
            )
            and data.get("value") in {
                "OPERATIONAL", "SLOW", "HOLD_ARMING", "HOLDING"
            }
        ]

    await with_harness(
        package,
        deepest_input_text_owner_loss,
        automations=HARD_STOP_AUTOMATIONS,
    )
    return 10


async def test_operational_full_outbox(package: dict[str, Any]) -> int:
    """Physical operational truth and ABORTED classification ignore capacity."""

    async def scenario(harness: RuntimeHarness) -> None:
        for cycle in ("c90", "c91"):
            await harness.call_script(
                "hoymiles_notify_battery_balancing_lifecycle",
                event="failed",
                reason_code="communication_failure",
                cycle_id=cycle,
            )
        original_ids = {
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_1_event_id",
            ),
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_2_event_id",
            ),
        }
        assert original_ids == {"c90.FA", "c91.FA"}
        cycle = await harness.seed_cycle(state="SNAPSHOT_VALID")
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="reconcile",
            expected_cycle_id=cycle,
        )
        assert harness.state(
            "sensor.hoymiles_battery_balancing_transaction"
        ) == "OPERATIONAL"
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction",
            "operational_started",
        ) is True
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "event_marker"
        ) == "STARTED"
        assert {
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_1_event_id",
            ),
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_2_event_id",
            ),
        } == original_ids
        await harness.call_script(
            "hoymiles_battery_balancing_request_abort",
            reason_code="user_disabled",
            cycle_id=cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "NOTIFICATION_PENDING",
            timeout=10,
            message="full outbox did not retain the terminal intent",
        )
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "event_marker"
        ) == "ABORTED"
        assert harness.attr(
            "sensor.hoymiles_battery_balancing_transaction",
            "operational_started",
        ) is True
        await harness.call_script(
            "hoymiles_battery_balancing_update_outbox_delivery",
            event_id="c90.FA",
            expected_delivery_state="PENDING",
            expected_attempt_count=0,
            next_delivery_state="DELIVERED",
            next_attempt_count=0,
        )
        await harness.setup_automations("hoymiles_battery_balancing_control")
        harness.set_state("sensor.hoymiles_hit_overview_battery_soc", 51)
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=8,
            message="deferred ABORTED event was not represented after a slot freed",
        )
        ids = [
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_1_event_id",
            ),
            harness.attr(
                "sensor.hoymiles_battery_balancing_notification_outbox",
                "slot_2_event_id",
            ),
        ]
        assert ids.count("c1.AB") == 1
        assert "c1.FA" not in ids
        assert ids.count("c91.FA") == 1
        assert not harness.notify_calls

    await with_harness(package, scenario)
    return 1


async def test_monotonic_cycle_identity(package: dict[str, Any]) -> int:
    async def scenario(harness: RuntimeHarness) -> None:
        from time import time

        harness.set_state("sensor.hoymiles_hit_ems_force_charge_soc_readback", 100)
        harness.set_state(
            "sensor.hoymiles_hit_ems_maximum_charge_power_readback", 8
        )
        await harness.call(
            "input_number",
            "set_value",
            {
                "entity_id": "input_number.hoymiles_battery_balancing_cycle_sequence",
                "value": 41,
            },
        )
        # Align both complete transactions inside one wall-clock second. Their
        # identities still come only from the durable sequence.
        fraction = time() % 1
        if fraction > 0.05:
            await asyncio.sleep(1.02 - fraction)
        first_second = int(time())
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="start",
            expected_cycle_id="",
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            in {"OPERATIONAL", "SLOW"},
            timeout=5,
            message="first monotonic cycle did not start",
        )
        first_cycle = harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "cycle_id"
        )
        await harness.call_script(
            "hoymiles_battery_balancing_request_abort",
            reason_code="completed",
            cycle_id=first_cycle,
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            == "TERMINAL",
            timeout=5,
            message="first monotonic cycle did not close",
        )
        await harness.call_script(
            "hoymiles_battery_balancing_transaction_worker",
            intent="start",
            expected_cycle_id="",
        )
        await harness.wait_until(
            lambda: harness.state(
                "sensor.hoymiles_battery_balancing_transaction"
            )
            in {"OPERATIONAL", "SLOW"},
            timeout=5,
            message="second monotonic cycle did not start",
        )
        second_cycle = harness.attr(
            "sensor.hoymiles_battery_balancing_transaction", "cycle_id"
        )
        second_second = int(time())
        assert (first_cycle, second_cycle) == ("c42", "c43")
        assert first_second == second_second, (
            "test host was too slow to exercise same-second creation",
            first_second,
            second_second,
        )

    await with_harness(package, scenario)
    return 1


async def async_main() -> None:
    assert version("homeassistant") == EXPECTED_HOME_ASSISTANT
    package = yaml.safe_load(SCHEDULER.read_text(encoding="utf-8"))
    templates = await test_all_jinja_templates_compile(package)
    gap_boundaries = await test_soft_gap_exact_boundaries(package)
    await test_exact_start(package)
    races = await test_hard_stop_race_matrix(package)
    transient_stops = await test_transient_hard_stop_capture(package)
    recovery = await test_legacy_and_malformed_recovery(package)
    gaps = await test_soft_gap_restart_and_clock(package)
    holds = await test_hold_restart_protocol(package)
    fresh_soc = await test_fresh_soc_before_hold(package)
    notifications = await test_terminal_and_notification_outbox(package)
    provider_timeouts = await test_notification_provider_timeout(package)
    generation_drifts = await test_exact_generation_drift(package)
    transaction_edges = await test_snapshot_and_sun_transactions(package)
    sun_commit_races = await test_final_sun_phase_commit_races(package)
    full_outbox = await test_operational_full_outbox(package)
    identities = await test_monotonic_cycle_identity(package)
    print(
        "Battery-balancing isolated HA runtime: PASS "
        f"(Home Assistant {EXPECTED_HOME_ASSISTANT}; exact start=1; "
        f"races={races}; transient-stops={transient_stops}; recovery={recovery}; "
        f"soft-gap={gaps}; holds={holds}; fresh-soc={fresh_soc}; "
        f"notifications={notifications}; provider-timeouts={provider_timeouts}; "
        f"generation-drifts={generation_drifts}; "
        f"transaction-edges={transaction_edges}; sun-commit={sun_commit_races}; "
        f"full-outbox={full_outbox}; "
        f"cycle-identity={identities}; gap-boundaries={gap_boundaries}; "
        f"jinja={templates})"
    )


def main() -> None:
    logging.basicConfig(level=logging.ERROR)
    # Home Assistant scripts may leave cancelled timer callbacks scheduled
    # until their owning event loop is closed.  Give every major scenario
    # family a fresh loop so an earlier disposable HA instance cannot inject
    # callbacks into a later one during the complete CI run.
    assert version("homeassistant") == EXPECTED_HOME_ASSISTANT
    package = yaml.safe_load(SCHEDULER.read_text(encoding="utf-8"))
    templates = asyncio.run(test_all_jinja_templates_compile(package))
    gap_boundaries = asyncio.run(test_soft_gap_exact_boundaries(package))
    asyncio.run(test_exact_start(package))
    races = asyncio.run(test_hard_stop_race_matrix(package))
    transient_stops = asyncio.run(test_transient_hard_stop_capture(package))
    recovery = asyncio.run(test_legacy_and_malformed_recovery(package))
    gaps = asyncio.run(test_soft_gap_restart_and_clock(package))
    holds = asyncio.run(test_hold_restart_protocol(package))
    fresh_soc = asyncio.run(test_fresh_soc_before_hold(package))
    notifications = asyncio.run(test_terminal_and_notification_outbox(package))
    provider_timeouts = asyncio.run(test_notification_provider_timeout(package))
    generation_drifts = asyncio.run(test_exact_generation_drift(package))
    transaction_edges = asyncio.run(test_snapshot_and_sun_transactions(package))
    sun_commit_races = asyncio.run(test_final_sun_phase_commit_races(package))
    full_outbox = asyncio.run(test_operational_full_outbox(package))
    identities = asyncio.run(test_monotonic_cycle_identity(package))
    print(
        "Battery-balancing isolated HA runtime: PASS "
        f"(Home Assistant {EXPECTED_HOME_ASSISTANT}; exact start=1; "
        f"races={races}; transient-stops={transient_stops}; recovery={recovery}; "
        f"soft-gap={gaps}; holds={holds}; fresh-soc={fresh_soc}; "
        f"notifications={notifications}; provider-timeouts={provider_timeouts}; "
        f"generation-drifts={generation_drifts}; "
        f"transaction-edges={transaction_edges}; sun-commit={sun_commit_races}; "
        f"full-outbox={full_outbox}; "
        f"cycle-identity={identities}; gap-boundaries={gap_boundaries}; "
        f"jinja={templates})"
    )


if __name__ == "__main__":
    main()
