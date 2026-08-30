"""Deterministic offline contract for the Phase 1B-2 Supervisor sensor."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
from typing import Any, Callable


ROOT = Path(os.environ.get("SUPERVISOR_CONTRACT_ROOT", Path(__file__).resolve().parents[1]))
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
DOMAIN = "hoymiles_hit_modbus"
NOW = datetime(2026, 8, 22, 10, 0, tzinfo=timezone.utc)
CLOCK = {"now": NOW, "calls": 0}
CHECKS = 0
GROUPS = 0
EXPECTED_CHECK_COUNT = 405
EXPECTED_TASK_PATHS = {
    "custom_components/hoymiles_hit_modbus/assets.py",
    "custom_components/hoymiles_hit_modbus/const.py",
    "custom_components/hoymiles_hit_modbus/resources/home_assistant/en/hoymiles_ems_scheduler.yaml",
    "custom_components/hoymiles_hit_modbus/resources/home_assistant/pl/hoymiles_ems_scheduler.yaml",
    "custom_components/hoymiles_hit_modbus/supervisor_sensor.py",
    "home_assistant/hoymiles_ems_scheduler.yaml",
    "tools/build_hacs_assets.py",
    "tools/test_supervisor_helpers_contract.py",
    "tools/test_supervisor_sensor_contract.py",
}


def check(condition: bool, message: str) -> None:
    """Count and enforce one bounded contract assertion."""
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def group(name: str, function: Callable[[], None]) -> None:
    """Run one named deterministic group."""
    global GROUPS
    function()
    GROUPS += 1
    print(f"PASS {name}")


class FakeState:
    """Small immutable-enough State projection used by the adapter."""

    def __init__(
        self,
        state: str,
        attributes: dict[str, Any] | None = None,
        reported: datetime | None = NOW,
    ) -> None:
        self.state = state
        self.attributes = dict(attributes or {})
        self.last_reported = reported
        self.last_updated = reported


class FakeEvent:
    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.data = dict(data or {})


class FakeHandle:
    def __init__(self, callback: Callable[[datetime], None], when: Any) -> None:
        self.callback = callback
        self.when = when
        self.cancelled = False
        self.cancel_calls = 0
        self.runs = 0

    def cancel(self) -> None:
        self.cancel_calls += 1
        self.cancelled = True

    def run(self) -> None:
        if self.cancelled:
            return
        self.runs += 1
        self.callback(CLOCK["now"])


class FakeBus:
    def __init__(self) -> None:
        self.listeners: dict[str, list[tuple[Callable[[FakeEvent], None], bool]]] = {}

    def async_listen(self, event_type: str, callback: Callable[[FakeEvent], None]) -> Callable[[], None]:
        record = [callback, True, 0]
        self.listeners.setdefault(event_type, []).append(record)  # type: ignore[arg-type]

        def unsubscribe() -> None:
            record[2] += 1
            record[1] = False

        return unsubscribe

    def fire(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        for callback, active, _unsubscribe_calls in tuple(self.listeners.get(event_type, ())):
            if active:
                callback(FakeEvent(data))


class FakeStates:
    def __init__(self) -> None:
        self.values: dict[str, FakeState] = {}
        self.reads: dict[str, int] = {}

    def get(self, entity_id: str) -> FakeState | None:
        self.reads[entity_id] = self.reads.get(entity_id, 0) + 1
        return self.values.get(entity_id)

    def reset_reads(self) -> None:
        self.reads.clear()


@dataclass
class FakeRegistryEntry:
    entity_id: str
    platform: str
    unique_id: str
    config_entry_id: str
    translation_key: str

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


class FakeRegistry:
    def __init__(self) -> None:
        self.entries: dict[str, FakeRegistryEntry] = {}

    def add(self, entry: FakeRegistryEntry) -> None:
        self.entries[entry.entity_id] = entry

    def async_get_entity_id(self, domain: str, platform: str, unique_id: str) -> str | None:
        for entry in self.entries.values():
            if entry.domain == domain and entry.platform == platform and entry.unique_id == unique_id:
                return entry.entity_id
        return None

    def async_get(self, entity_id: str | None) -> FakeRegistryEntry | None:
        return self.entries.get(entity_id) if entity_id is not None else None

    def rename(self, unique_id: str, new_entity_id: str) -> tuple[str, str]:
        old_id, entry = next(
            (entity_id, item)
            for entity_id, item in self.entries.items()
            if item.unique_id == unique_id
        )
        self.entries.pop(old_id)
        entry.entity_id = new_entity_id
        self.entries[new_entity_id] = entry
        return old_id, new_entity_id


class FakeHass:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.states = FakeStates()
        self.registry = FakeRegistry()
        self.bus = FakeBus()
        self.config = SimpleNamespace(time_zone="UTC", language="en")
        self.state_listeners: list[dict[str, Any]] = []
        self.delay_handles: list[FakeHandle] = []
        self.point_handles: list[FakeHandle] = []
        self.fail_point_creation = False
        self.point_install_observer: Callable[[FakeHandle], None] | None = None

    def track_states(
        self,
        entity_ids: tuple[str, ...],
        callback: Callable[[FakeEvent], None],
    ) -> Callable[[], None]:
        record = {
            "ids": tuple(entity_ids),
            "callback": callback,
            "active": True,
            "unsubscribe_calls": 0,
        }
        self.state_listeners.append(record)

        def unsubscribe() -> None:
            record["unsubscribe_calls"] += 1
            record["active"] = False

        return unsubscribe

    def fire_state(self, entity_id: str, new_state: FakeState | None) -> None:
        old_state = self.states.values.get(entity_id)
        if new_state is None:
            self.states.values.pop(entity_id, None)
        else:
            self.states.values[entity_id] = new_state
        event = FakeEvent(
            {"entity_id": entity_id, "old_state": old_state, "new_state": new_state}
        )
        for registration in tuple(self.state_listeners):
            if registration["active"] and entity_id in registration["ids"]:
                registration["callback"](event)

    def call_later(self, delay: float, callback: Callable[[datetime], None]) -> Callable[[], None]:
        handle = FakeHandle(callback, delay)
        self.delay_handles.append(handle)
        return handle.cancel

    def track_point(self, callback: Callable[[datetime], None], when: datetime) -> Callable[[], None]:
        if self.fail_point_creation:
            raise RuntimeError("injected point-timer creation failure")
        handle = FakeHandle(callback, when)
        self.point_handles.append(handle)
        if self.point_install_observer is not None:
            self.point_install_observer(handle)
        return handle.cancel

    def active_delays(self) -> list[FakeHandle]:
        return [handle for handle in self.delay_handles if not handle.cancelled and handle.runs == 0]

    def active_points(self) -> list[FakeHandle]:
        return [handle for handle in self.point_handles if not handle.cancelled and handle.runs == 0]


@dataclass
class FakeConfigEntry:
    entry_id: str


@dataclass
class FakeRuntimeData:
    source_device: Any
    entities: dict[str, Any]


class FakeSensorEntity:
    NOT_ADDED = "NOT_ADDED"
    ADDING = "ADDING"
    ADDED = "ADDED"
    REMOVED = "REMOVED"

    def _init_fake_lifecycle(self) -> None:
        self._fake_platform_state = self.NOT_ADDED
        self._fake_remove_callbacks: list[Callable[[], None]] = []
        self.attempted_writes = 0
        self.suppressed_adding_writes = 0
        self.visible_writes = 0
        self.forbidden_removed_writes = 0
        self.write_count = 0
        self.visible_write_history: list[dict[str, Any]] = []

    async def async_added_to_hass(self) -> None:
        return None

    async def async_will_remove_from_hass(self) -> None:
        return None

    def async_on_remove(self, callback: Callable[[], None]) -> None:
        self._fake_remove_callbacks.append(callback)

    async def add_to_platform_finish(self) -> None:
        """Model the relevant Home Assistant 2026.7 add lifecycle."""
        self._fake_platform_state = self.ADDING
        await self.async_added_to_hass()
        self._fake_platform_state = self.ADDED
        self.async_write_ha_state()

    async def remove_from_platform(self) -> None:
        """Mark removed before invoking the integration removal callback."""
        self._fake_platform_state = self.REMOVED
        await self.async_will_remove_from_hass()

    def async_write_ha_state(self) -> None:
        self.attempted_writes += 1
        if self._fake_platform_state == self.ADDING:
            self.suppressed_adding_writes += 1
            return
        if self._fake_platform_state == self.ADDED:
            self.visible_writes += 1
            self.write_count = self.visible_writes
            self.visible_write_history.append(
                {
                    "available": bool(getattr(self, "available", False)),
                    "state": getattr(self, "native_value", None),
                    "attributes": deepcopy(
                        getattr(self, "extra_state_attributes", {})
                    ),
                }
            )
            return
        if self._fake_platform_state == self.REMOVED:
            self.forbidden_removed_writes += 1
            raise AssertionError("Visible write attempted after entity removal")


class FakeDeviceInfo(dict):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.__dict__.update(kwargs)


def _module(name: str, **attributes: Any) -> ModuleType:
    module = ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _install_stubs() -> None:
    homeassistant = _module("homeassistant")
    components = _module("homeassistant.components")
    sensor = _module("homeassistant.components.sensor", SensorEntity=FakeSensorEntity)
    components.sensor = sensor
    homeassistant.components = components
    _module("homeassistant.config_entries", ConfigEntry=FakeConfigEntry)
    _module(
        "homeassistant.const",
        EVENT_CORE_CONFIG_UPDATE="core_config_updated",
        STATE_UNAVAILABLE="unavailable",
        STATE_UNKNOWN="unknown",
    )
    _module(
        "homeassistant.core",
        Event=FakeEvent,
        HomeAssistant=FakeHass,
        State=FakeState,
        callback=lambda function: function,
    )
    helpers = _module("homeassistant.helpers")
    entity_registry = _module(
        "homeassistant.helpers.entity_registry",
        EVENT_ENTITY_REGISTRY_UPDATED="entity_registry_updated",
        async_get=lambda hass: hass.registry,
    )
    helpers.entity_registry = entity_registry
    _module("homeassistant.helpers.device_registry", DeviceInfo=FakeDeviceInfo)
    _module(
        "homeassistant.helpers.event",
        async_call_later=lambda hass, delay, callback: hass.call_later(delay, callback),
        async_track_point_in_utc_time=lambda hass, callback, when: hass.track_point(callback, when),
        async_track_state_change_event=lambda hass, ids, callback: hass.track_states(tuple(ids), callback),
    )
    util = _module("homeassistant.util")

    def utcnow() -> datetime:
        CLOCK["calls"] += 1
        return CLOCK["now"]

    dt_module = _module("homeassistant.util.dt", utcnow=utcnow)
    util.dt = dt_module

    custom_components = _module("custom_components")
    custom_components.__path__ = [str(ROOT / "custom_components")]
    package = _module("custom_components.hoymiles_hit_modbus")
    package.__path__ = [str(COMPONENT)]
    custom_components.hoymiles_hit_modbus = package
    _module(
        "custom_components.hoymiles_hit_modbus.const",
        DOMAIN=DOMAIN,
        NAME="EMS for Hoymiles HIT-(5–20)L-G3",
    )
    _module(
        "custom_components.hoymiles_hit_modbus.models",
        RuntimeData=FakeRuntimeData,
    )


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _load_nonpackage(name: str, path: Path, package: str) -> ModuleType:
    """Load an __init__.py as a review module with its real relative imports."""
    module = ModuleType(name)
    module.__file__ = str(path)
    module.__package__ = package
    sys.modules[name] = module
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), module.__dict__)
    return module


_install_stubs()
CORE = _load(
    "custom_components.hoymiles_hit_modbus.ems_supervisor",
    COMPONENT / "ems_supervisor.py",
)
RUNTIME = _load(
    "custom_components.hoymiles_hit_modbus.supervisor_runtime",
    COMPONENT / "supervisor_runtime.py",
)
SENSOR = _load(
    "custom_components.hoymiles_hit_modbus.supervisor_sensor",
    COMPONENT / "supervisor_sensor.py",
)


def _install_integration_init_stubs() -> None:
    """Install only the import surface needed to load production __init__.py."""
    const = sys.modules["homeassistant.const"]
    const.EVENT_HOMEASSISTANT_STARTED = "homeassistant_started"
    core = sys.modules["homeassistant.core"]
    core.ServiceCall = SimpleNamespace
    components = sys.modules["homeassistant.components"]
    components.frontend = _module(
        "homeassistant.components.frontend",
        add_extra_js_url=lambda *_args, **_kwargs: None,
    )
    components.http = _module(
        "homeassistant.components.http",
        StaticPathConfig=lambda *args, **kwargs: (args, kwargs),
    )
    helpers = sys.modules["homeassistant.helpers"]
    helpers.config_validation = _module(
        "homeassistant.helpers.config_validation",
        boolean=lambda value: bool(value),
        config_entry_only_config_schema=lambda domain: {"domain": domain},
    )
    helpers.issue_registry = _module(
        "homeassistant.helpers.issue_registry",
        IssueSeverity=SimpleNamespace(WARNING="warning"),
        async_create_issue=lambda *_args, **_kwargs: None,
        async_delete_issue=lambda *_args, **_kwargs: None,
    )
    _module(
        "voluptuous",
        Optional=lambda key, default=None: key,
        Schema=lambda value: value,
    )
    const_module = sys.modules["custom_components.hoymiles_hit_modbus.const"]
    for name, value in {
        "ATTR_OVERWRITE": "overwrite",
        "CONF_RESOLVED_SOURCE_DEVICE_ID": "resolved_source_device_id",
        "CONF_SOURCE_DEVICE_ID": "source_device_id",
        "EMS_PACKAGE_SENTINEL": "binary_sensor.ems_package",
        "EMS_PACKAGE_VERSION": "1.5.7",
        "EMS_PACKAGE_VERSION_ENTITY": "sensor.ems_package_version",
        "PLATFORMS": ("sensor",),
        "SERVICE_INSTALL_ASSETS": "install_assets",
        "VERSION": "1.5.7",
    }.items():
        setattr(const_module, name, value)

    async def async_noop(*_args: Any, **_kwargs: Any) -> Any:
        return []

    _module(
        "custom_components.hoymiles_hit_modbus.assets",
        FRONTEND_BOOTSTRAP_URL="/local/bootstrap.js",
        FRONTEND_RESOURCE_URL="/local/resource.js",
        FRONTEND_STATIC_ROUTE="static",
        RESOURCE_ROOT=ROOT,
        async_install_assets=async_noop,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.catalog",
        async_match_entities=async_noop,
        matched_source_count=lambda _matched: 0,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.installation_identity",
        async_get_or_create_installation_identity=async_noop,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.source_device",
        async_resolve_source_device=async_noop,
        persist_resolved_source_entry=lambda *_args, **_kwargs: None,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.support_http",
        HoymilesSupportBundleView=type("HoymilesSupportBundleView", (), {}),
    )


_install_integration_init_stubs()
INTEGRATION = _load_nonpackage(
    "custom_components.hoymiles_hit_modbus.integration_init_under_test",
    COMPONENT / "__init__.py",
    "custom_components.hoymiles_hit_modbus",
)


@dataclass
class FakeMatchedEntity:
    """Exact constructor projection consumed by production sensor.py."""

    catalog: dict[str, Any]
    source: Any


class FakeHoymilesProxyEntity(FakeSensorEntity):
    """Minimal proxy base needed to instantiate the real HoymilesSensor class."""

    def __init__(
        self,
        hass: FakeHass,
        entry: FakeConfigEntry,
        runtime: FakeRuntimeData,
        matched: FakeMatchedEntity,
    ) -> None:
        self._init_fake_lifecycle()
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._matched = matched
        self._catalog = matched.catalog
        self._attr_unique_id = f"{entry.entry_id}_{matched.catalog['translation_key']}"
        self.entity_id = f"sensor.hoymiles_hit_{matched.catalog['translation_key']}"

    @property
    def source_state(self) -> FakeState | None:
        source = self._matched.source
        return self.hass.states.get(source.entity_id) if source is not None else None

    @property
    def available(self) -> bool:
        state = self.source_state
        return state is not None and state.state not in {"unknown", "unavailable"}

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()


class FakeOptimizerSensor(FakeSensorEntity):
    """Register one real optimizer-plan identity when sequentially added."""

    plan_locator = ""

    def __init__(
        self,
        hass: FakeHass,
        entry: FakeConfigEntry,
        runtime: FakeRuntimeData,
    ) -> None:
        self._init_fake_lifecycle()
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_{self.plan_locator}"
        self.entity_id = f"sensor.hoymiles_hit_{self.plan_locator}"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        unique_id = f"{self._entry.entry_id}_{self.plan_locator}"
        entity_id = self.hass.registry.async_get_entity_id(
            "sensor",
            DOMAIN,
            unique_id,
        )
        action = "update"
        if entity_id is None:
            entity_id = self.entity_id
            self.hass.registry.add(
                FakeRegistryEntry(
                    entity_id=entity_id,
                    platform=DOMAIN,
                    unique_id=unique_id,
                    config_entry_id=self._entry.entry_id,
                    translation_key=self.plan_locator,
                )
            )
            action = "create"
        self.hass.bus.fire(
            "entity_registry_updated",
            {"action": action, "entity_id": entity_id},
        )


class FakeRCEOptimizerSensor(FakeOptimizerSensor):
    plan_locator = "rce_optimized_plan"


class FakeTariffOptimizerSensor(FakeOptimizerSensor):
    plan_locator = "tariff_charge_plan"


class FakeRCMOptimizerSensor(FakeOptimizerSensor):
    plan_locator = "rcm_voltage_plan"


def _install_sensor_platform_stubs() -> None:
    """Install only imports needed to execute production sensor.async_setup_entry."""
    const = sys.modules["homeassistant.const"]
    const.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
    helpers = sys.modules["homeassistant.helpers"]
    helpers.entity_platform = _module(
        "homeassistant.helpers.entity_platform",
        AddConfigEntryEntitiesCallback=Callable[..., None],
    )
    models = sys.modules["custom_components.hoymiles_hit_modbus.models"]
    models.MatchedEntity = FakeMatchedEntity
    _module(
        "custom_components.hoymiles_hit_modbus.entity",
        HoymilesProxyEntity=FakeHoymilesProxyEntity,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.energy_data",
        numeric_state_sample=lambda *_args, **_kwargs: SimpleNamespace(
            fresh=False,
            value=None,
        ),
    )
    _module(
        "custom_components.hoymiles_hit_modbus.localization",
        localized_text_state=lambda value, _language: value,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.power_balance",
        OVERVIEW_BATTERY_POWER="overview_battery_power",
        OVERVIEW_INVERTER_ACTIVE_POWER="overview_inverter_active_power",
        PARALLEL_POWER_SOURCE_KEYS_BY_TARGET={},
        PARALLEL_POWER_TARGETS=set(),
        calculate_parallel_power_balance=lambda **_kwargs: None,
        calculate_parallel_inverter_power=lambda **_kwargs: None,
        is_parallel_master=lambda _value: False,
        is_known_machine_type=lambda _value: True,
        select_overview_power=lambda _key, **kwargs: kwargs.get("source_power"),
    )
    _module(
        "custom_components.hoymiles_hit_modbus.rce_sensor",
        HoymilesRCEOptimizerSensor=FakeRCEOptimizerSensor,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.tariff_sensor",
        HoymilesTariffOptimizerSensor=FakeTariffOptimizerSensor,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.rcm_sensor",
        HoymilesRCMOptimizerSensor=FakeRCMOptimizerSensor,
    )


_install_sensor_platform_stubs()
SENSOR_PLATFORM = _load(
    "custom_components.hoymiles_hit_modbus.sensor_order_under_test",
    COMPONENT / "sensor.py",
)


def _source_entity_id(spec: Any, entry_id: str) -> str:
    if not spec.entry_local:
        return spec.locator
    return f"sensor.hoymiles_hit_{spec.locator}"


def _plan_attributes(kind: str) -> dict[str, Any]:
    future = (NOW + timedelta(hours=1)).isoformat()
    if kind == "rce_plan":
        return {
            "status_code": "ready",
            "result_current": True,
            "recalculation_pending": False,
            "input_revision": 1,
            "current_slot_planned": False,
            "current_slot_start_eligible": False,
            "current_slot_continue_eligible": False,
            "current_slot_end": future,
            "current_run_end": future,
            "current_slot_execution_discharge_power_kw": 5.0,
            "current_slot_planned_export_kwh": 2.0,
            "current_required_minimum_soc_percent": 20.0,
        }
    if kind == "tariff_plan":
        return {
            "status_code": "ready",
            "result_current": True,
            "recalculation_pending": False,
            "input_revision": 1,
            "current_slot_planned": False,
            "current_action": "none",
            "current_run_need_class": "none",
            "current_run_start_eligible": False,
            "current_run_continue_eligible": False,
            "requested_charge_power_kw": 5.0,
            "command_charge_power_percent": 40.0,
            "current_run_grid_import_kwh": 2.0,
            "current_run_benefit_pln": 1.0,
            "target_soc_percent": 80.0,
            "base_reserve_soc_percent": 20.0,
            "current_slot_end": future,
        }
    return {
        "result_current": True,
        "recalculation_pending": False,
        "input_revision": 1,
        "live_emergency": False,
        "emergency_action_ready": False,
        "prediction_ready": True,
        "action": "monitor",
        "risk_window_active": False,
        "voltage_risk_score_percent": 0.0,
        "recommended_charge_limit_percent": 50.0,
        "recommended_charge_power_kw": 5.0,
        "recommended_export_limit_percent": 50.0,
        "charge_actuator_data_fresh": True,
        "export_actuator_data_fresh": True,
        "gcf_data_fresh": True,
        "bms_charge_data_fresh": True,
        "bms_charge_available": True,
        "system_power_data_valid": True,
        "pre_discharge_start_eligible": False,
        "pre_discharge_continue_eligible": False,
        "pre_discharge_transaction_ready": False,
        "pre_discharge_deadline": future,
        "pre_discharge_target_soc_percent": 30.0,
        "pre_discharge_power_kw": 4.0,
        "pre_discharge_power_percent": 40.0,
        "planned_grid_discharge_kwh": 2.0,
        "target_soc_before_risk_percent": 50.0,
        "protected_minimum_soc_percent": 20.0,
        "system_power_kw": 10.0,
    }


def _default_state(spec: Any) -> FakeState:
    if spec.key in {"rce_plan", "tariff_plan", "rcm_plan"}:
        return FakeState("translated presentation", _plan_attributes(spec.key))
    if spec.key == "supervisor_mode":
        return FakeState("Off")
    if spec.key == "supervisor_profile":
        return FakeState("Balanced")
    if spec.key == "sun":
        return FakeState("below_horizon")
    if spec.key in {"charge_timer", "discharge_timer"}:
        return FakeState("idle")
    if spec.key in {
        "ems_generation",
        "gcf_generation",
        "topology_generation",
        "hardware_readback_supported",
    }:
        return FakeState("1")
    if spec.key == "machine_type":
        return FakeState("0")
    if spec.key == "inverter_count":
        return FakeState("1")
    if spec.key == "ems_mode_readback":
        return FakeState("0")
    if spec.key == "gcf_enable_readback":
        return FakeState("0")
    if spec.key in {
        "battery_soc",
        "bms_voltage",
        "bms_max_charge_current",
        "bms_max_discharge_current",
        "charge_power_readback",
        "gcf_export_limit_readback",
        "rce_effective_discharge_power",
    }:
        return FakeState("50")
    if spec.key in {
        "discharge_power_readback",
        "discharge_soc_readback",
        "charge_power_ems_readback",
        "charge_soc_readback",
    }:
        return FakeState("0")
    if "latched" in spec.key:
        if "slot_end" in spec.key or "deadline" in spec.key:
            return FakeState("ignored", {"timestamp": (NOW + timedelta(hours=1)).timestamp()})
        return FakeState("40")
    if spec.key == "tariff_active_action":
        return FakeState("none")
    return FakeState("off")


def environment(entry_id: str = "entry-a") -> tuple[FakeHass, FakeConfigEntry, FakeRuntimeData, Any]:
    CLOCK["now"] = NOW
    CLOCK["calls"] = 0
    hass = FakeHass()
    entry = FakeConfigEntry(entry_id)
    source_device = SimpleNamespace(
        name_by_user=None,
        name="HIT inverter",
        manufacturer="Hoymiles",
        model="HIT-10L-G3",
        sw_version="1",
    )
    runtime = FakeRuntimeData(source_device=source_device, entities={})
    hass.data[DOMAIN] = {entry_id: runtime}
    for spec in SENSOR.SUPERVISOR_SOURCE_SPECS:
        entity_id = _source_entity_id(spec, entry_id)
        if spec.entry_local:
            hass.registry.add(
                FakeRegistryEntry(
                    entity_id=entity_id,
                    platform=DOMAIN,
                    unique_id=f"{entry_id}_{spec.locator}",
                    config_entry_id=entry_id,
                    translation_key=spec.locator,
                )
            )
        hass.states.values[entity_id] = _default_state(spec)
    sensor = SENSOR.HoymilesSupervisorSensor(hass, entry, runtime)
    sensor.entity_id = "sensor.hoymiles_hit_ems_supervisor"
    sensor._init_fake_lifecycle()
    return hass, entry, runtime, sensor


def add(sensor: Any) -> None:
    asyncio.run(sensor.add_to_platform_finish())


def remove(sensor: Any) -> None:
    asyncio.run(sensor.remove_from_platform())


def _active_state_registration(hass: FakeHass) -> dict[str, Any]:
    active = [registration for registration in hass.state_listeners if registration["active"]]
    check(len(active) == 1, "Expected exactly one active state listener")
    return active[0]


def _git_paths(*args: str) -> set[str]:
    output = subprocess.check_output(["git", *args], cwd=ROOT, text=True)
    return {line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()}


def _platform_environment(
    *,
    plan_registry_present: bool,
) -> tuple[FakeHass, FakeConfigEntry, FakeRuntimeData, tuple[FakeMatchedEntity, ...]]:
    """Build one production sensor-platform input with literal proxy identities."""
    hass, entry, runtime, _unused_sensor = environment("entry-order")
    proxies = (
        FakeMatchedEntity(
            catalog={"translation_key": "order_proxy_one"},
            source=SimpleNamespace(entity_id="sensor.order_source_one"),
        ),
        FakeMatchedEntity(
            catalog={"translation_key": "order_proxy_two"},
            source=SimpleNamespace(entity_id="sensor.order_source_two"),
        ),
    )
    runtime.entities = {"sensor": list(proxies)}
    for index, matched in enumerate(proxies, start=1):
        target_id = f"sensor.hoymiles_hit_{matched.catalog['translation_key']}"
        hass.registry.add(
            FakeRegistryEntry(
                entity_id=target_id,
                platform=DOMAIN,
                unique_id=f"{entry.entry_id}_{matched.catalog['translation_key']}",
                config_entry_id=entry.entry_id,
                translation_key=matched.catalog["translation_key"],
            )
        )
        hass.states.values[matched.source.entity_id] = FakeState(str(index))
    if not plan_registry_present:
        for key in ("rce_plan", "tariff_plan", "rcm_plan"):
            spec = SENSOR._SOURCE_BY_KEY[key]
            hass.registry.entries.pop(_source_entity_id(spec, entry.entry_id), None)
    return hass, entry, runtime, proxies


def _capture_platform_entities(
    hass: FakeHass,
    entry: FakeConfigEntry,
) -> tuple[list[Any], list[tuple[tuple[Any, ...], dict[str, Any]]]]:
    """Run actual production async_setup_entry and capture its one add call."""
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def capture(*args: Any, **kwargs: Any) -> None:
        calls.append((args, kwargs))

    asyncio.run(SENSOR_PLATFORM.async_setup_entry(hass, entry, capture))
    if not calls or not calls[0][0]:
        return [], calls
    return list(calls[0][0][0]), calls


def _add_platform_entity(entity: Any) -> None:
    """Perform the relevant sequential fake-platform add for one real object."""
    if not hasattr(entity, "_fake_platform_state"):
        entity._init_fake_lifecycle()
    asyncio.run(entity.add_to_platform_finish())


def _supervisor_state_registrations(
    hass: FakeHass,
    sensor: Any,
) -> list[dict[str, Any]]:
    return [
        registration
        for registration in hass.state_listeners
        if getattr(registration["callback"], "__self__", None) is sensor
    ]


def test_platform_entity_order() -> None:
    hass, entry, runtime, matched_proxies = _platform_environment(
        plan_registry_present=True
    )
    entities, calls = _capture_platform_entities(hass, entry)
    check(len(calls) == 1, "Production used more than one platform add operation")
    check(len(calls[0][0]) == 1, "Platform add positional arguments changed")
    check(calls[0][1] == {}, "update_before_add semantics changed")
    check(isinstance(calls[0][0][0], list), "Production entity collection is no longer one list")
    check(len(entities) == len(matched_proxies) + 5, "An existing production entity disappeared")
    check(len({id(entity) for entity in entities}) == len(entities), "A duplicate entity object was introduced")

    supervisors = [
        entity
        for entity in entities
        if type(entity) is SENSOR.HoymilesSupervisorSensor
    ]
    check(len(supervisors) == 1, "Expected exactly one Supervisor entity")
    supervisor = supervisors[0]
    supervisor_position = entities.index(supervisor)
    check(supervisor_position == len(matched_proxies), "Supervisor does not immediately follow every proxy")
    check(
        all(type(entity) is SENSOR_PLATFORM.HoymilesSensor for entity in entities[:supervisor_position]),
        "A non-proxy entity appears before Supervisor",
    )
    check(
        tuple(entity._matched for entity in entities[:supervisor_position])
        == matched_proxies,
        "Proxy identity or relative order changed",
    )

    expected_native_types = (
        SENSOR_PLATFORM.HoymilesRCEOptimizerSensor,
        SENSOR_PLATFORM.HoymilesTariffOptimizerSensor,
        SENSOR_PLATFORM.HoymilesRCMOptimizerSensor,
        SENSOR_PLATFORM.HoymilesSetupStatusSensor,
    )
    actual_native_types = tuple(
        type(entity) for entity in entities[supervisor_position + 1 :]
    )
    check(actual_native_types == expected_native_types, "Native sensor relative order changed")
    for native_type in expected_native_types:
        check(
            supervisor_position
            < next(index for index, entity in enumerate(entities) if type(entity) is native_type),
            f"Supervisor is not before {native_type.__name__}",
        )
        check(
            sum(type(entity) is native_type for entity in entities) == 1,
            f"Native entity count differs for {native_type.__name__}",
        )
    check(supervisor._entry is entry, "Supervisor received a different ConfigEntry")
    check(supervisor._runtime is runtime, "Supervisor received a different RuntimeData")
    check(supervisor.hass is hass, "Supervisor received a different HomeAssistant")


def test_fresh_install_sequential_add() -> None:
    hass, entry, _runtime, matched_proxies = _platform_environment(
        plan_registry_present=False
    )
    entities, calls = _capture_platform_entities(hass, entry)
    check(len(calls) == 1, "Fresh install used more than one add operation")
    supervisor = next(
        entity for entity in entities if type(entity) is SENSOR.HoymilesSupervisorSensor
    )
    supervisor_position = entities.index(supervisor)
    check(supervisor_position == len(matched_proxies), "Fresh install did not add Supervisor after proxies")
    plan_keys = ("rce_plan", "tariff_plan", "rcm_plan")
    plan_ids = {
        key: _source_entity_id(SENSOR._SOURCE_BY_KEY[key], entry.entry_id)
        for key in plan_keys
    }
    check(
        all(hass.registry.async_get(plan_ids[key]) is None for key in plan_keys),
        "Fresh install unexpectedly had an optimizer-plan registry entry",
    )
    check(
        all(
            hass.registry.async_get(
                f"sensor.hoymiles_hit_{matched.catalog['translation_key']}"
            )
            is not None
            for matched in matched_proxies
        ),
        "Proxy registry entries were not present before Supervisor",
    )

    for entity in entities[:supervisor_position]:
        _add_platform_entity(entity)
    _add_platform_entity(supervisor)
    check(supervisor.available and supervisor.native_value == "off", "Fresh Supervisor did not publish bounded Off")
    check(supervisor.visible_writes == 1, "Fresh Supervisor initial write count differs")
    check(
        supervisor.extra_state_attributes["supervisor_execution_authorized"] is False
        and supervisor.extra_state_attributes["legacy_execution_unchanged"] is True,
        "Fresh Supervisor gained physical authority",
    )
    check(
        all(supervisor._source_entity_ids[key] is None for key in plan_keys),
        "Missing optimizer registry source used a canonical fallback",
    )
    check(
        all(supervisor._read_source_states()[key] is None for key in plan_keys),
        "Old canonical plan State became authority without registry ownership",
    )
    check(
        len(hass.data[SENSOR._GUARD_KEY].sensors) == 1
        and hass.data[SENSOR._GUARD_KEY].sensors[entry.entry_id] is supervisor,
        "Fresh install registered a duplicate Supervisor",
    )

    for entity_id in plan_ids.values():
        hass.states.values.pop(entity_id, None)
    optimizer_types = (
        SENSOR_PLATFORM.HoymilesRCEOptimizerSensor,
        SENSOR_PLATFORM.HoymilesTariffOptimizerSensor,
        SENSOR_PLATFORM.HoymilesRCMOptimizerSensor,
    )
    optimizer_keys = dict(zip(optimizer_types, plan_keys, strict=True))
    for entity in entities[supervisor_position + 1 :]:
        _add_platform_entity(entity)
        entity_type = type(entity)
        if entity_type not in optimizer_keys:
            continue
        key = optimizer_keys[entity_type]
        registry_entry = hass.registry.async_get(plan_ids[key])
        check(supervisor._source_entity_ids[key] == plan_ids[key], f"{key} did not re-resolve")
        check(registry_entry is not None, f"{key} registry entry was not created")
        check(
            registry_entry.config_entry_id == entry.entry_id
            and registry_entry.unique_id
            == f"{entry.entry_id}_{SENSOR._SOURCE_BY_KEY[key].locator}",
            f"{key} resolved outside the current config entry",
        )
        check(hass.states.get(plan_ids[key]) is None, f"{key} became healthy without real state data")

    check(
        all(supervisor._source_entity_ids[key] == plan_ids[key] for key in plan_keys),
        "Final plan source mapping is incomplete",
    )
    check(
        all(supervisor._read_source_states()[key] is None for key in plan_keys),
        "Missing plan state became available after registry creation",
    )
    check(supervisor.available and supervisor.native_value == "off", "Fresh sequence did not remain safely Off")
    check(supervisor.visible_writes == 1, "Registry creation duplicated an unchanged Off publication")
    registrations = _supervisor_state_registrations(hass, supervisor)
    check(sum(item["active"] for item in registrations) == 1, "Supervisor state listener leaked during re-resolution")
    check(
        all(item["active"] or item["unsubscribe_calls"] == 1 for item in registrations),
        "Replaced Supervisor listener was not unsubscribed exactly once",
    )
    check(
        sum(
            type(entity) is SENSOR.HoymilesSupervisorSensor
            for entity in entities
        )
        == 1,
        "Fresh sequence introduced a duplicate Supervisor",
    )
    remove(supervisor)
    check(
        not any(item["active"] for item in _supervisor_state_registrations(hass, supervisor)),
        "Fresh sequence left a Supervisor state listener",
    )
    check(
        not any(
            active
            for records in hass.bus.listeners.values()
            for callback, active, _unsubscribe_calls in records
            if getattr(callback, "__self__", None) is supervisor
        ),
        "Fresh sequence left a Supervisor bus listener",
    )


def test_existing_install_order() -> None:
    hass, entry, _runtime, matched_proxies = _platform_environment(
        plan_registry_present=True
    )
    entities, calls = _capture_platform_entities(hass, entry)
    check(len(calls) == 1, "Existing install used more than one add operation")
    supervisor = next(
        entity for entity in entities if type(entity) is SENSOR.HoymilesSupervisorSensor
    )
    supervisor_position = entities.index(supervisor)
    check(supervisor_position == len(matched_proxies), "Existing install Supervisor order differs")
    for entity in entities[: supervisor_position + 1]:
        _add_platform_entity(entity)
    plan_keys = ("rce_plan", "tariff_plan", "rcm_plan")
    expected_ids = {
        key: _source_entity_id(SENSOR._SOURCE_BY_KEY[key], entry.entry_id)
        for key in plan_keys
    }
    check(
        all(
            supervisor._source_entity_ids[key] == expected_ids[key]
            for key in plan_keys
        ),
        "Existing plan mappings were not resolved before optimizer addition",
    )
    check(supervisor.available and supervisor.native_value == "off", "Existing install initial Off failed")
    check(supervisor.visible_writes == 1, "Existing install initial write count differs")
    initial_registration = _supervisor_state_registrations(hass, supervisor)[0]
    for entity in entities[supervisor_position + 1 :]:
        _add_platform_entity(entity)
    check(supervisor.visible_writes == 1, "Later optimizer addition duplicated Off publication")
    registrations = _supervisor_state_registrations(hass, supervisor)
    check(registrations == [initial_registration], "Unchanged registry events duplicated source resolution listener")
    check(initial_registration["active"], "Existing-install state listener became inactive")
    check(
        len(hass.data[SENSOR._GUARD_KEY].sensors) == 1
        and hass.data[SENSOR._GUARD_KEY].sensors[entry.entry_id] is supervisor,
        "Existing install duplicated Supervisor guard ownership",
    )
    check(supervisor._attr_unique_id == f"{entry.entry_id}_ems_supervisor", "Supervisor unique ID changed")
    for key in plan_keys:
        registry_entry = hass.registry.async_get(expected_ids[key])
        check(registry_entry is not None, f"Existing {key} registry entry disappeared")
        check(registry_entry.config_entry_id == entry.entry_id, f"Existing {key} changed config-entry owner")
        check(
            registry_entry.unique_id
            == f"{entry.entry_id}_{SENSOR._SOURCE_BY_KEY[key].locator}",
            f"Existing {key} unique ID changed",
        )
    remove(supervisor)


def test_structure_and_manifest() -> None:
    source = (COMPONENT / "supervisor_sensor.py").read_text(encoding="utf-8")
    check((COMPONENT / "supervisor_sensor.py").is_file(), "Supervisor module missing")
    check((ROOT / "tools" / "test_supervisor_sensor_contract.py").is_file(), "Dedicated test missing")
    check("class HoymilesSupervisorSensor(SensorEntity)" in source, "Wrong entity class")
    changed = _git_paths("diff", "--name-only", "HEAD") | _git_paths(
        "ls-files", "--others", "--exclude-standard"
    )
    check(changed == EXPECTED_TASK_PATHS, f"Task manifest differs: {sorted(changed)}")
    check(not _git_paths("diff", "--cached", "--name-only"), "Staged files exist")
    branch = _git_paths("diff", "--name-only", "v1.5.7") | _git_paths(
        "ls-files", "--others", "--exclude-standard"
    )
    check(len(branch) == 20, f"Branch manifest must have 20 paths, got {len(branch)}")
    check(
        not _git_paths(
            "diff",
            "--name-only",
            "HEAD",
            "--",
            "custom_components/hoymiles_hit_modbus/ems_supervisor.py",
            "custom_components/hoymiles_hit_modbus/supervisor_runtime.py",
        ),
        "Pure Supervisor files changed",
    )
    artifacts = [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and (path.suffix in {".pyc", ".pyo"} or path.name.endswith(".tmp"))
    ]
    check(not artifacts, f"Temporary artifacts found: {artifacts[:3]}")


def test_entity_identity() -> None:
    hass, entry, _runtime, sensor = environment()
    check(sensor._attr_should_poll is False, "Polling enabled")
    check(sensor._attr_has_entity_name is True, "Entity name contract changed")
    check(sensor._attr_translation_key == "ems_supervisor", "Translation key changed")
    check(sensor._attr_unique_id == f"{entry.entry_id}_ems_supervisor", "Unique ID changed")
    check(sensor.suggested_object_id == "hoymiles_hit_ems_supervisor", "Object ID changed")
    check(sensor._attr_icon == "mdi:source-branch-check", "Icon changed")
    check(getattr(sensor, "_attr_entity_category", None) is None, "Entity category must be absent")
    check(sensor.device_info.identifiers == {(DOMAIN, entry.entry_id)}, "Wrong device identity")
    check(sensor.available is False and sensor.native_value is None, "Constructor must be unavailable")
    check(sensor.extra_state_attributes == {}, "Constructor attrs must be empty")
    check(hass.states.reads == {}, "Constructor read HA states")
    check(not hass.state_listeners and not hass.delay_handles and not hass.point_handles, "Constructor registered work")
    check("RestoreEntity" not in (COMPONENT / "supervisor_sensor.py").read_text(encoding="utf-8"), "RestoreEntity used")


def test_publication() -> None:
    hass, _entry, _runtime, sensor = environment()
    captured: list[Any] = []
    original_arbiter = SENSOR.arbitrate_supervisor

    def capturing_arbiter(**kwargs: Any) -> Any:
        decision = original_arbiter(**kwargs)
        captured.append(decision)
        return decision

    SENSOR.arbitrate_supervisor = capturing_arbiter
    try:
        add(sensor)
    finally:
        SENSOR.arbitrate_supervisor = original_arbiter
    check(sensor.available, "Valid decision unavailable")
    check(sensor.native_value == captured[-1].state.value, "State is not exact decision state")
    expected = json.loads(CORE.serialize_supervisor_summary(captured[-1]))
    check(sensor.extra_state_attributes == expected, "Attributes differ from exact serializer")
    check(set(sensor.__dict__) >= {"_attributes", "_serialized_summary"}, "Publication cache missing")
    serialized = sensor._serialized_summary.encode("utf-8")
    check(len(serialized) <= CORE.MAX_SUPERVISOR_SUMMARY_BYTES, "Summary bound exceeded")
    for candidate in sensor.extra_state_attributes["candidate_summaries"]:
        size = len(json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        check(size <= CORE.MAX_CANDIDATE_SUMMARY_BYTES, "Candidate bound exceeded")
    forged = replace(captured[-1], supervisor_execution_authorized=True)
    try:
        CORE.serialize_supervisor_summary(forged)
    except ValueError:
        pass
    else:
        raise AssertionError("Forged execution authorization serialized")
    check("_unrecorded_attributes" not in SENSOR.HoymilesSupervisorSensor.__dict__, "Recorder wildcard added")


def test_mode_profile_permissions() -> None:
    hass, entry, _runtime, sensor = environment()
    for key in ("supervisor_mode", "supervisor_profile", "allow_rce", "allow_tariff", "allow_rcm"):
        spec = SENSOR._SOURCE_BY_KEY[key]
        hass.states.values.pop(_source_entity_id(spec, entry.entry_id), None)
    add(sensor)
    check(sensor.extra_state_attributes["supervisor_mode"] == "off", "Missing mode did not default Off")
    check(sensor.extra_state_attributes["profile"] == "balanced", "Missing profile did not default Balanced")
    check(all(not item["allowed_by_user"] for item in sensor.extra_state_attributes["candidate_summaries"]), "Missing permission became true")
    check(not sensor._warning_categories, "Missing future helpers emitted a warning")
    mode_id = SENSOR._SOURCE_BY_KEY["supervisor_mode"].locator
    profile_id = SENSOR._SOURCE_BY_KEY["supervisor_profile"].locator
    hass.states.values[mode_id] = FakeState(None)  # type: ignore[arg-type]
    hass.states.values[profile_id] = FakeState(123)  # type: ignore[arg-type]
    sensor._recompute()
    check(sensor.extra_state_attributes["supervisor_mode"] == "off", "Malformed mode type escaped Off")
    check(sensor.extra_state_attributes["profile"] == "balanced", "Malformed profile type escaped Balanced")
    for label, expected in (
        ("Balanced", "balanced"),
        ("Maximum Profit", "maximum_profit"),
        ("High Reserve — Winter", "high_reserve_winter"),
    ):
        hass.states.values[mode_id] = FakeState("Shadow")
        hass.states.values[profile_id] = FakeState(label)
        sensor._recompute()
        check(sensor.extra_state_attributes["supervisor_mode"] == "shadow", "Shadow rejected")
        check(sensor.extra_state_attributes["profile"] == expected, f"Profile {label} rejected")
    observed_modes: list[Any] = []
    original = SENSOR.arbitrate_supervisor

    def observe(**kwargs: Any) -> Any:
        observed_modes.append(kwargs["mode"])
        return original(**kwargs)

    SENSOR.arbitrate_supervisor = observe
    try:
        for invalid in ("Active", "active", "unsupported", "unavailable"):
            hass.states.values[mode_id] = FakeState(invalid)
            sensor._recompute()
            check(sensor.extra_state_attributes["supervisor_mode"] == "off", f"{invalid} escaped Off")
        hass.states.values[profile_id] = FakeState("invalid profile text")
        sensor._recompute()
        sensor._recompute()
    finally:
        SENSOR.arbitrate_supervisor = original
    check(all(mode is not CORE.SupervisorMode.ACTIVE for mode in observed_modes), "Core received Active")
    check(
        sensor._warning_categories
        == {
            "active",
            "unknown_or_unavailable_mode",
            "malformed_or_unsupported_mode",
            "invalid_profile",
        },
        "Warning categories are not finite/exact",
    )
    rcm_enabled = SENSOR._SOURCE_BY_KEY["rcm_enabled"].locator
    rcm_shadow = SENSOR._SOURCE_BY_KEY["rcm_shadow_mode"].locator
    hass.states.values[rcm_enabled] = FakeState("on")
    hass.states.values.pop(rcm_shadow, None)
    sensor._recompute()
    rcm = next(item for item in sensor.extra_state_attributes["candidate_summaries"] if item["policy_id"] == "rcm")
    check(rcm["enabled"] is False, "Missing RCM shadow flag enabled RCEm")

    hass.states.values[mode_id] = FakeState("Shadow")
    hass.states.values[profile_id] = FakeState("Balanced")
    hass.states.values[SENSOR._SOURCE_BY_KEY["allow_rce"].locator] = FakeState("on")
    hass.states.values[SENSOR._SOURCE_BY_KEY["rce_enabled"].locator] = FakeState("on")
    for key in (
        "rce_control_data_ready",
        "rce_price_above_threshold",
        "rce_reserve_ready",
        "ems_execution_ready",
    ):
        hass.states.values[SENSOR._SOURCE_BY_KEY[key].locator] = FakeState("on")
    rce_plan_id = sensor._source_entity_ids["rce_plan"]
    assert rce_plan_id is not None
    selected_plan = _plan_attributes("rce_plan")
    selected_plan["current_slot_planned"] = True
    selected_plan["current_slot_start_eligible"] = True
    hass.states.values[rce_plan_id] = FakeState("ignored", selected_plan)
    sensor._recompute()
    check(sensor.native_value == "shadow_selected", "Healthy Shadow candidate was not selected")
    check(sensor.extra_state_attributes["selected_policy"] == "rce", "Wrong Shadow policy selected")


def test_entry_resolution() -> None:
    hass, entry, _runtime, sensor = environment()
    add(sensor)
    plan_spec = SENSOR._SOURCE_BY_KEY["rce_plan"]
    unique_id = f"{entry.entry_id}_{plan_spec.locator}"
    old_id, new_id = hass.registry.rename(unique_id, "sensor.renamed_rce_plan")
    hass.states.values[new_id] = hass.states.values.pop(old_id)
    old_registration = _active_state_registration(hass)
    hass.bus.fire("entity_registry_updated", {"action": "update", "entity_id": new_id})
    new_registration = _active_state_registration(hass)
    check(old_registration["active"] is False, "Old listener survived registry rename")
    check(new_id in new_registration["ids"] and old_id not in new_registration["ids"], "Rename did not rebind")
    entry_record = hass.registry.async_get(new_id)
    assert entry_record is not None
    entry_record.config_entry_id = "other-entry"
    hass.bus.fire("entity_registry_updated", {"action": "update", "entity_id": new_id})
    check(sensor._source_entity_ids["rce_plan"] is None, "Cross-entry plan accepted")
    check(old_id not in _active_state_registration(hass)["ids"], "Canonical fallback used")
    check(sensor.available, "Missing registry source should produce bounded decision")


def test_multi_entry() -> None:
    hass, _entry, _runtime, first = environment()
    add(first)
    check(first.available, "Single entry unavailable")
    hass.states.reset_reads()
    calls = {name: 0 for name in ("context", "rce", "tariff", "rcm", "arbiter")}
    originals = {
        "context": SENSOR.build_execution_context,
        "rce": SENSOR.build_rce_candidate,
        "tariff": SENSOR.build_tariff_candidate,
        "rcm": SENSOR.build_rcm_candidate,
        "arbiter": SENSOR.arbitrate_supervisor,
    }

    def counted(name: str) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            calls[name] += 1
            return originals[name](*args, **kwargs)

        return wrapper

    SENSOR.build_execution_context = counted("context")
    SENSOR.build_rce_candidate = counted("rce")
    SENSOR.build_tariff_candidate = counted("tariff")
    SENSOR.build_rcm_candidate = counted("rcm")
    SENSOR.arbitrate_supervisor = counted("arbiter")
    try:
        second_runtime = FakeRuntimeData(first._runtime.source_device, {})
        hass.data[DOMAIN]["entry-b"] = second_runtime
        SENSOR.notify_supervisor_guard(hass)
        check(not first.available, "First sensor stayed available with two entries")
        check(not hass.states.reads and all(value == 0 for value in calls.values()), "Multi-entry read sources or called pure runtime")
        second = SENSOR.HoymilesSupervisorSensor(hass, FakeConfigEntry("entry-b"), second_runtime)
        second.entity_id = "sensor.second_supervisor"
        second._init_fake_lifecycle()
        add(second)
        check(not second.available and all(value == 0 for value in calls.values()), "Second sensor selected an entry")
        remove(second)
        hass.data[DOMAIN].pop("entry-b")
        SENSOR.notify_supervisor_guard(hass)
        check(first.available and all(value == 1 for value in calls.values()), "2→1 did not make exactly one fresh pipeline")
        guard = hass.data[SENSOR._GUARD_KEY]
        replacement = SENSOR.HoymilesSupervisorSensor(hass, first._entry, first._runtime)
        guard.sensors[first._entry.entry_id] = replacement
        remove(first)
        check(guard.sensors[first._entry.entry_id] is replacement, "Old reload object removed replacement")
    finally:
        SENSOR.build_execution_context = originals["context"]
        SENSOR.build_rce_candidate = originals["rce"]
        SENSOR.build_tariff_candidate = originals["tariff"]
        SENSOR.build_rcm_candidate = originals["rcm"]
        SENSOR.arbitrate_supervisor = originals["arbiter"]


def test_source_map() -> None:
    specs = SENSOR.SUPERVISOR_SOURCE_SPECS
    frozen_rows = tuple(
        (
            spec.number,
            spec.key,
            spec.locator,
            spec.entry_local,
            spec.planner_event,
            spec.future_helper,
        )
        for spec in specs
    )
    source_map_hash = hashlib.sha256(
        json.dumps(
            frozen_rows,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    check(
        source_map_hash
        == "9b135496b122e5bc95eac9352b95192901d46a8cea7de1aa4a1c948367d37ccc",
        "Exact frozen source map differs",
    )
    check(len(specs) == 60, "Logical source count differs")
    check(tuple(spec.number for spec in specs) == tuple(range(1, 61)), "Source numbers differ or a 61st source exists")
    check(sum(not spec.future_helper for spec in specs) == 60, "Existing source count differs")
    check(sum(spec.future_helper for spec in specs) == 0, "Future source count differs")
    check(all(not specs[index].future_helper for index in range(5)), "Persistent helper sources remain future")
    check(sum(spec.entry_local for spec in specs) == 21, "Entry-local count differs")
    check(sum(not spec.entry_local for spec in specs) == 39, "Global count differs")
    check(sum(not spec.planner_event for spec in specs) == 46, "H count differs")
    check(sum(spec.planner_event for spec in specs) == 14, "P count differs")
    check({spec.number for spec in specs if spec.planner_event} == {2, 3, 4, 5, 6, 7, 12, 14, 15, 21, 22, 23, 25, 26}, "P set differs")
    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    ids = _active_state_registration(hass)["ids"]
    check(len(ids) == len(set(ids)) == 60, "Healthy watched IDs are not exact/unique")
    forbidden = {
        "sensor.hoymiles_hit_ems_supervisor",
        "sensor.hoymiles_ems_control_owner",
        "binary_sensor.hoymiles_ems_control_conflict",
        "sensor.hoymiles_parallel_aggregate_physical_response",
    }
    check(not forbidden.intersection(ids), "Forbidden source watched")


def test_plan_whitelists() -> None:
    expected_rce = (
        "status_code", "result_current", "recalculation_pending",
        "input_revision", "current_slot_planned",
        "current_slot_start_eligible", "current_slot_continue_eligible",
        "current_slot_end", "current_run_end",
        "current_slot_execution_discharge_power_kw",
        "current_slot_planned_export_kwh",
        "current_required_minimum_soc_percent",
    )
    expected_tariff = (
        "status_code", "result_current", "recalculation_pending",
        "input_revision", "current_slot_planned", "current_action",
        "current_run_need_class", "current_run_start_eligible",
        "current_run_continue_eligible", "requested_charge_power_kw",
        "command_charge_power_percent", "current_run_grid_import_kwh",
        "current_run_benefit_pln", "target_soc_percent",
        "base_reserve_soc_percent", "current_slot_end",
    )
    expected_rcm = (
        "result_current", "recalculation_pending", "input_revision",
        "live_emergency", "emergency_action_ready", "prediction_ready",
        "action", "risk_window_active", "voltage_risk_score_percent",
        "recommended_charge_limit_percent", "recommended_charge_power_kw",
        "recommended_export_limit_percent", "charge_actuator_data_fresh",
        "export_actuator_data_fresh", "gcf_data_fresh",
        "bms_charge_data_fresh", "bms_charge_available",
        "system_power_data_valid", "pre_discharge_start_eligible",
        "pre_discharge_continue_eligible",
        "pre_discharge_transaction_ready", "pre_discharge_deadline",
        "pre_discharge_target_soc_percent", "pre_discharge_power_kw",
        "pre_discharge_power_percent", "planned_grid_discharge_kwh",
        "target_soc_before_risk_percent", "protected_minimum_soc_percent",
        "system_power_kw",
    )
    check(SENSOR.RCE_PLAN_ATTRIBUTES == expected_rce, "RCE whitelist differs")
    check(SENSOR.TARIFF_PLAN_ATTRIBUTES == expected_tariff, "Tariff whitelist differs")
    check(SENSOR.RCM_PLAN_ATTRIBUTES == expected_rcm, "RCM whitelist differs")
    check("current_run_need_class" in SENSOR.TARIFF_PLAN_ATTRIBUTES, "Tariff need missing")
    check("planned_slots" not in SENSOR.RCE_PLAN_ATTRIBUTES + SENSOR.TARIFF_PLAN_ATTRIBUTES + SENSOR.RCM_PLAN_ATTRIBUTES, "Full plan arrays consumed")
    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    states = sensor._read_source_states()
    _mode, _profile, rce, tariff, rcm, execution = sensor._build_snapshots(states, NOW)
    check(rce.status_code is RUNTIME.RcePlanStatus.READY, "RCE status projection differs")
    check(rce.input_revision == 1 and rce.observed_at == NOW, "RCE revision/time projection differs")
    check(tariff.current_run_need_class is RUNTIME.TariffRunNeed.NONE, "Tariff need projection differs")
    check(tariff.current_action is RUNTIME.TariffAction.NONE, "Tariff action projection differs")
    check(rcm.action is RUNTIME.RcmAction.MONITOR, "RCM action projection differs")
    check(rcm.charge_path_locally_valid is True, "RCM 306 local path projection differs")
    check(rcm.export_path_locally_valid is True and rcm.current_export_limit_fresh is True, "RCM 259 local path projection differs")
    check(execution.full_block_generation_at == NOW, "EMS cohort projection differs")
    check(execution.gcf_generation_at == NOW and execution.gcf_cohort_coherent is True, "GCF cohort projection differs")
    check(execution.topology_generation_at == NOW, "Topology cohort projection differs")
    context = SENSOR.build_execution_context(execution, now=NOW)
    check(context.transaction_pending is False and context.transaction_owner_kind is CORE.OwnerKind.NONE, "Transaction namespace changed")
    check(context.topology_full_block_allowed and context.topology_direct_register_allowed, "Single topology projection differs")
    baseline = (rce, tariff, rcm)
    for key in ("rce_plan", "tariff_plan", "rcm_plan"):
        plan_state = states[key]
        assert plan_state is not None
        plan_state.state = "other translated native state"
        plan_state.attributes["planned_slots"] = [{"unbounded": "ignored"}]
    projected = sensor._build_snapshots(states, NOW)[2:5]
    check(projected == baseline, "Native/presentation-only plan data crossed whitelist")
    rcm_plan = states["rcm_plan"]
    assert rcm_plan is not None
    rcm_plan.attributes["recommended_charge_power_kw"] = "5.0"
    malformed_rcm = sensor._build_snapshots(states, NOW)[4]
    check(malformed_rcm.recommended_charge_power_kw is None, "String plan numeric was accepted")


def _contains_fake_state(value: Any) -> bool:
    if isinstance(value, FakeState):
        return True
    if is_dataclass(value):
        return any(_contains_fake_state(getattr(value, field.name)) for field in fields(value))
    if isinstance(value, (tuple, list)):
        return any(_contains_fake_state(item) for item in value)
    if isinstance(value, dict):
        return any(_contains_fake_state(item) for item in value.values())
    return False


def test_atomic_snapshot() -> None:
    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    hass.states.reset_reads()
    CLOCK["calls"] = 0
    counts = {name: 0 for name in ("context", "rce", "tariff", "rcm", "arbiter", "serializer", "loads")}
    originals = {
        "context": SENSOR.build_execution_context,
        "rce": SENSOR.build_rce_candidate,
        "tariff": SENSOR.build_tariff_candidate,
        "rcm": SENSOR.build_rcm_candidate,
        "arbiter": SENSOR.arbitrate_supervisor,
        "serializer": SENSOR.serialize_supervisor_summary,
        "loads": SENSOR.json.loads,
    }

    def wrap(name: str) -> Callable[..., Any]:
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            counts[name] += 1
            check(not _contains_fake_state(args) and not _contains_fake_state(kwargs), f"Raw State crossed into {name}")
            return originals[name](*args, **kwargs)

        return wrapped

    SENSOR.build_execution_context = wrap("context")
    SENSOR.build_rce_candidate = wrap("rce")
    SENSOR.build_tariff_candidate = wrap("tariff")
    SENSOR.build_rcm_candidate = wrap("rcm")
    SENSOR.arbitrate_supervisor = wrap("arbiter")
    SENSOR.serialize_supervisor_summary = wrap("serializer")
    SENSOR.json.loads = wrap("loads")
    try:
        sensor._recompute()
    finally:
        SENSOR.build_execution_context = originals["context"]
        SENSOR.build_rce_candidate = originals["rce"]
        SENSOR.build_tariff_candidate = originals["tariff"]
        SENSOR.build_rcm_candidate = originals["rcm"]
        SENSOR.arbitrate_supervisor = originals["arbiter"]
        SENSOR.serialize_supervisor_summary = originals["serializer"]
        SENSOR.json.loads = originals["loads"]
    check(CLOCK["calls"] == 1, "Recompute used more than one now")
    check(all(value <= 1 for value in hass.states.reads.values()), "A source was read more than once")
    check(sum(hass.states.reads.values()) == 60, "Atomic pass did not read exact healthy 60")
    check(counts == {name: 1 for name in counts}, f"Pipeline call counts differ: {counts}")


def test_eventing() -> None:
    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    profile_id = SENSOR._SOURCE_BY_KEY["supervisor_profile"].locator
    hass.fire_state(profile_id, FakeState("Maximum Profit"))
    check(len(hass.active_delays()) == 1, "P event did not schedule one callback")
    first = hass.active_delays()[0]
    hass.fire_state(profile_id, FakeState("High Reserve — Winter"))
    check(first.cancelled and len(hass.active_delays()) == 1, "P event did not rearm")
    pending = hass.active_delays()[0]
    check(abs(pending.when - 0.100) < 1e-12, "P delay is not 100 ms")
    writes = sensor.write_count
    rce_active_id = SENSOR._SOURCE_BY_KEY["rce_active"].locator
    hass.fire_state(rce_active_id, FakeState("off"))
    check(pending.cancelled, "H event did not cancel P")
    check(sensor.write_count == writes + 1, "H event did not recompute immediately")
    check(sensor.extra_state_attributes["profile"] == "high_reserve_winter", "H did not consume latest P state")
    hass.fire_state(profile_id, FakeState("Balanced"))
    hass.fire_state(profile_id, FakeState("Maximum Profit"))
    latest = hass.active_delays()[0]
    latest.run()
    check(sensor.extra_state_attributes["profile"] == "maximum_profit", "Latest P state did not win")
    plan_id = sensor._source_entity_ids["rce_plan"]
    assert plan_id is not None
    plan = hass.states.values[plan_id]
    ignored = FakeState("different translated text", {**plan.attributes, "planned_slots": [1]}, plan.last_reported)
    before_delays = len(hass.delay_handles)
    hass.fire_state(plan_id, ignored)
    check(len(hass.delay_handles) == before_delays, "Ignored plan projection triggered work")
    before_writes = sensor.write_count
    sensor._recompute()
    check(sensor.write_count == before_writes, "Identical summary was republished")
    hass.fire_state("sensor.hoymiles_hit_ems_supervisor", FakeState("shadow_selected"))
    check(sensor.write_count == before_writes, "Output self-triggered")


def test_temporal_scheduling() -> None:
    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    points = hass.active_points()
    check(len(points) == 1, "Expected one point-in-time callback")
    check(points[0].when == NOW + timedelta(seconds=60, microseconds=1), "Nearest freshness boundary differs")
    states = sensor._read_source_states()
    _mode, _profile, rce, tariff, rcm, execution = sensor._build_snapshots(states, NOW)
    context = SENSOR.build_execution_context(execution, now=NOW)
    rcm = replace(
        rcm,
        export_state=context.export_state,
        direct_register_topology_allowed=context.topology_direct_register_allowed,
        full_block_topology_allowed=context.topology_full_block_allowed,
    )
    candidates = (
        SENSOR.build_rce_candidate(rce, now=NOW),
        SENSOR.build_tariff_candidate(tariff, now=NOW),
        SENSOR.build_rcm_candidate(rcm, now=NOW),
    )
    boundaries = set(
        sensor._semantic_boundaries(
            states,
            NOW,
            rce,
            tariff,
            rcm,
            execution,
            candidates,
        )
    )
    check(NOW + timedelta(seconds=60, microseconds=1) in boundaries, "RCM freshness boundary differs")
    check(NOW + timedelta(seconds=120, microseconds=1) in boundaries, "SOC freshness boundary differs")
    check(NOW + timedelta(seconds=180, microseconds=1) in boundaries, "Physical/GCF/topology boundary differs")
    check(NOW + timedelta(seconds=300, microseconds=1) in boundaries, "Plan/BMS/306 boundary differs")
    planned_rce = replace(rce, current_slot_planned=True)
    planned_tariff = replace(tariff, current_slot_planned=True)
    threshold_boundaries = set(
        sensor._semantic_boundaries(
            states,
            NOW,
            planned_rce,
            planned_tariff,
            rcm,
            execution,
            candidates,
        )
    )
    assert planned_rce.current_slot_end is not None
    assert planned_tariff.current_slot_end is not None
    check(
        planned_rce.current_slot_end - timedelta(seconds=300) + timedelta(microseconds=1)
        in threshold_boundaries,
        "RCE start threshold differs",
    )
    check(
        planned_tariff.current_slot_end - timedelta(seconds=420) + timedelta(microseconds=1)
        in threshold_boundaries,
        "Tariff start threshold differs",
    )
    sensor._schedule_temporal_callback(NOW, (NOW,))
    check(not hass.active_points(), "Boundary at now created an immediate loop")
    sensor._schedule_temporal_callback(NOW, tuple(boundaries))
    first = hass.active_points()[0]
    check(first.when == NOW + timedelta(seconds=60, microseconds=1), "Absolute deadline changed")
    CLOCK["now"] = first.when
    first.run()
    check(len(hass.active_points()) == 1, "Timer pass did not reselect one future boundary")
    replacement = hass.active_points()[0]
    hass.bus.fire("core_config_updated", {"time_zone": "Europe/Warsaw"})
    check(replacement.cancelled and len(hass.active_points()) == 1, "Timezone change did not reschedule")
    CLOCK["now"] = NOW + timedelta(days=2)
    for state in hass.states.values.values():
        state.last_reported = NOW - timedelta(days=2)
        state.last_updated = NOW - timedelta(days=2)
        if "timestamp" in state.attributes:
            state.attributes["timestamp"] = (NOW - timedelta(days=1)).timestamp()
        for key in ("current_slot_end", "current_run_end", "pre_discharge_deadline"):
            if key in state.attributes:
                state.attributes[key] = (NOW - timedelta(days=1)).isoformat()
    sensor._recompute()
    check(not hass.active_points(), "No-future state retained a temporal callback")
    source = (COMPONENT / "supervisor_sensor.py").read_text(encoding="utf-8")
    check("async_track_point_in_utc_time" in source, "Point scheduler absent")
    check("async_track_time_interval" not in source and "EVENT_TIME_CHANGED" not in source, "Polling clock path added")


def test_ha_lifecycle_model() -> None:
    class LifecycleProbe(FakeSensorEntity):
        def __init__(self, *, fail: bool) -> None:
            self._init_fake_lifecycle()
            self.fail = fail
            self.remove_callbacks = 0

        async def async_added_to_hass(self) -> None:
            self.async_write_ha_state()
            if self.fail:
                raise RuntimeError("injected add failure")

        async def async_will_remove_from_hass(self) -> None:
            self.remove_callbacks += 1

    healthy = LifecycleProbe(fail=False)
    asyncio.run(healthy.add_to_platform_finish())
    check(healthy._fake_platform_state == healthy.ADDED, "Successful fake add did not reach ADDED")
    check(healthy.attempted_writes == 2, "Successful fake add write attempts differ")
    check(healthy.suppressed_adding_writes == 1, "ADDING write was not suppressed")
    check(healthy.visible_writes == 1, "Automatic initial write is not exact")
    check(healthy.forbidden_removed_writes == 0, "Healthy add wrote after removal")

    failed = LifecycleProbe(fail=True)
    try:
        asyncio.run(failed.add_to_platform_finish())
    except RuntimeError:
        pass
    else:
        raise AssertionError("Fake add swallowed async_added_to_hass failure")
    check(failed._fake_platform_state == failed.ADDING, "Failed fake add performed a magic lifecycle transition")
    check(failed.attempted_writes == 1, "Failed fake add write attempts differ")
    check(failed.suppressed_adding_writes == 1, "Failed ADDING write was visible")
    check(failed.visible_writes == 0, "Failed add performed an automatic initial write")
    check(failed.remove_callbacks == 0, "Failed add automatically invoked integration cleanup")
    asyncio.run(failed.remove_from_platform())
    check(failed._fake_platform_state == failed.REMOVED, "Explicit fake remove did not reach REMOVED")
    check(failed.remove_callbacks == 1, "Explicit fake remove callback count differs")


def test_transactional_setup() -> None:
    stages = (
        "after_guard_registration",
        "after_entry_resolution",
        "after_state_listener",
        "after_registry_listener",
        "during_config_listener",
        "during_first_recompute",
        "during_first_temporal_creation",
    )

    for stage in stages:
        hass, entry, runtime, sensor = environment()

        class Peer:
            def __init__(self) -> None:
                self.counts: list[int] = []

            def _async_loaded_entry_count_changed(self, count: int) -> None:
                self.counts.append(count)

        peer = Peer()
        hass.data[SENSOR._GUARD_KEY] = SENSOR._SupervisorGuard(
            sensors={"peer-entry": peer},
            loaded_entry_count=1,
        )

        if stage == "during_config_listener":
            class ConfigAssignmentFailureSensor(SENSOR.HoymilesSupervisorSensor):
                def __setattr__(self, name: str, value: Any) -> None:
                    super().__setattr__(name, value)
                    if (
                        name == "_config_unsub"
                        and value is not None
                        and getattr(self, "_fail_config_assignment", False)
                    ):
                        self._fail_config_assignment = False
                        raise RuntimeError("injected config-listener assignment failure")

            sensor = ConfigAssignmentFailureSensor(hass, entry, runtime)
            sensor.entity_id = "sensor.hoymiles_hit_ems_supervisor"
            sensor._init_fake_lifecycle()
            sensor._fail_config_assignment = True

        original_notify = SENSOR.notify_supervisor_guard
        original_bus_listen = hass.bus.async_listen
        if stage == "after_guard_registration":
            first_notify = {"pending": True}

            def fail_after_guard(current_hass: FakeHass) -> None:
                original_notify(current_hass)
                if first_notify["pending"]:
                    first_notify["pending"] = False
                    raise RuntimeError("injected post-guard failure")

            SENSOR.notify_supervisor_guard = fail_after_guard
        elif stage == "after_entry_resolution":
            original_resolve = sensor._resolve_source_entity_ids

            def fail_after_resolution() -> None:
                original_resolve()
                raise RuntimeError("injected post-resolution failure")

            sensor._resolve_source_entity_ids = fail_after_resolution
        elif stage == "after_state_listener":
            original_replace = sensor._replace_state_listener

            def fail_after_state_listener() -> None:
                original_replace()
                raise RuntimeError("injected post-state-listener failure")

            sensor._replace_state_listener = fail_after_state_listener
        elif stage == "after_registry_listener":
            def fail_before_config_listener(
                event_type: str,
                callback: Callable[[FakeEvent], None],
            ) -> Callable[[], None]:
                if event_type == "core_config_updated":
                    raise RuntimeError("injected pre-config-listener failure")
                return original_bus_listen(event_type, callback)

            hass.bus.async_listen = fail_before_config_listener
        elif stage == "during_first_recompute":
            def fail_source_read() -> dict[str, FakeState | None]:
                raise RuntimeError("injected first-recompute failure")

            sensor._read_source_states = fail_source_read
        elif stage == "during_first_temporal_creation":
            hass.fail_point_creation = True

        try:
            add(sensor)
        except RuntimeError:
            pass
        else:
            raise AssertionError(f"Setup failure was not propagated for {stage}")
        finally:
            SENSOR.notify_supervisor_guard = original_notify
            hass.bus.async_listen = original_bus_listen
            hass.fail_point_creation = False

        guard = hass.data[SENSOR._GUARD_KEY]
        check(guard.sensors.get(entry.entry_id) is not sensor, f"Guard retained failed sensor at {stage}")
        check(not any(item["active"] for item in hass.state_listeners), f"State listener leaked at {stage}")
        check(
            not any(
                active
                for records in hass.bus.listeners.values()
                for _callback, active, _unsubscribe_calls in records
            ),
            f"Bus listener leaked at {stage}",
        )
        check(not hass.active_delays(), f"Planner callback leaked at {stage}")
        check(not hass.active_points(), f"Temporal callback leaked at {stage}")
        check(sensor._planner_cancel is None and sensor._temporal_cancel is None, f"Callback handle retained at {stage}")
        check(sensor._state_unsub is None and sensor._registry_unsub is None and sensor._config_unsub is None, f"Unsubscribe handle retained at {stage}")
        check(not sensor.available and sensor.native_value is None and sensor.extra_state_attributes == {}, f"Failed setup retained decision at {stage}")
        check(sensor.visible_writes == 0, f"Failed ADDING setup wrote visible state at {stage}")
        check(sensor._removed and not sensor._guard_ready, f"Failed setup remained registered at {stage}")
        check(peer.counts == [1, 1], f"Peer notification sequence differs at {stage}: {peer.counts}")
        unsubscribe_counts = [
            item["unsubscribe_calls"] for item in hass.state_listeners
        ] + [
            unsubscribe_calls
            for records in hass.bus.listeners.values()
            for _callback, _active, unsubscribe_calls in records
        ]
        check(
            all(count == 1 for count in unsubscribe_counts),
            f"Installed resource was not unsubscribed exactly once at {stage}",
        )

        peer_notifications = len(peer.counts)
        remove(sensor)
        check(len(peer.counts) == peer_notifications, f"Idempotent cleanup renotified peers at {stage}")
        check(
            unsubscribe_counts
            == [item["unsubscribe_calls"] for item in hass.state_listeners]
            + [
                unsubscribe_calls
                for records in hass.bus.listeners.values()
                for _callback, _active, unsubscribe_calls in records
            ],
            f"Idempotent cleanup unsubscribed a handle twice at {stage}",
        )

        fresh = SENSOR.HoymilesSupervisorSensor(hass, entry, runtime)
        fresh.entity_id = "sensor.hoymiles_hit_ems_supervisor"
        fresh._init_fake_lifecycle()
        add(fresh)
        check(guard.sensors.get(entry.entry_id) is fresh, f"Fresh setup did not replace failed lifecycle at {stage}")
        check(fresh.available and fresh.visible_writes == 1, f"Fresh setup failed after injected {stage}")
        remove(fresh)


def test_atomic_timer_publication() -> None:
    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    old_point = hass.active_points()[0]
    before = sensor.visible_writes
    profile_id = SENSOR._SOURCE_BY_KEY["supervisor_profile"].locator
    hass.states.values[profile_id] = FakeState("Maximum Profit")
    hass.fail_point_creation = True
    sensor._recompute()
    hass.fail_point_creation = False
    check(sensor.visible_writes == before + 1, "Timer failure did not produce exactly one visible transition")
    check(sensor.visible_write_history[-1] == {"available": False, "state": None, "attributes": {}}, "Timer failure did not publish only unavailable")
    check(not any(item["attributes"].get("profile") == "maximum_profit" for item in sensor.visible_write_history[before:]), "New valid decision was visible before timer failure")
    check(old_point.cancel_calls == 1 and not hass.active_points(), "Timer failure did not clear prior callback")

    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    old_point = hass.active_points()[0]
    before = sensor.visible_writes
    installation_observations: list[tuple[bool, str | None, bool]] = []
    hass.point_install_observer = lambda _handle: installation_observations.append(
        (sensor.available, sensor.native_value, old_point.cancelled)
    )
    hass.states.values[SENSOR._SOURCE_BY_KEY["supervisor_profile"].locator] = FakeState("Maximum Profit")
    sensor._recompute()
    hass.point_install_observer = None
    check(installation_observations == [(True, "off", False)], "Timer was not installed before replacement/publication")
    check(old_point.cancel_calls == 1 and len(hass.active_points()) == 1, "Successful timer replacement differs")
    check(sensor.visible_writes == before + 1, "Successful replacement did not publish changed decision")
    check(sensor.visible_write_history[-1]["attributes"]["profile"] == "maximum_profit", "Replacement published wrong decision")

    old_point = hass.active_points()[0]
    before = sensor.visible_writes
    original_boundaries = sensor._semantic_boundaries
    sensor._semantic_boundaries = lambda *_args, **_kwargs: ()
    hass.states.values[SENSOR._SOURCE_BY_KEY["supervisor_profile"].locator] = FakeState("Balanced")
    try:
        sensor._recompute()
    finally:
        sensor._semantic_boundaries = original_boundaries
    check(old_point.cancel_calls == 1 and not hass.active_points(), "No-boundary result retained a callback")
    check(sensor.available and sensor.visible_writes == before + 1, "No-boundary result did not publish normally")
    check(sensor.visible_write_history[-1]["attributes"]["profile"] == "balanced", "No-boundary publication differs")

    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    old_point = hass.active_points()[0]
    before = sensor.visible_writes
    original_boundaries = sensor._semantic_boundaries

    def fail_boundaries(*_args: Any, **_kwargs: Any) -> tuple[datetime, ...]:
        raise ValueError("injected semantic-boundary failure")

    sensor._semantic_boundaries = fail_boundaries
    hass.states.values[SENSOR._SOURCE_BY_KEY["supervisor_profile"].locator] = FakeState("Maximum Profit")
    try:
        sensor._recompute()
    finally:
        sensor._semantic_boundaries = original_boundaries
    check(sensor.visible_writes == before + 1, "Boundary failure did not produce one unavailable transition")
    check(sensor.visible_write_history[-1] == {"available": False, "state": None, "attributes": {}}, "Boundary failure retained stale decision")
    check(not any(item["attributes"].get("profile") == "maximum_profit" for item in sensor.visible_write_history[before:]), "Boundary failure exposed a new valid decision")
    check(old_point.cancel_calls == 1 and not hass.active_points(), "Boundary failure retained temporal state")
    check(sensor._error_categories == {"temporal_scheduler"}, "Temporal failure category is not bounded")


def test_config_entry_unload() -> None:
    class FakeConfigEntries:
        def __init__(self, result: bool) -> None:
            self.result = result
            self.calls: list[tuple[str, tuple[str, ...]]] = []

        async def async_unload_platforms(
            self,
            entry: FakeConfigEntry,
            platforms: tuple[str, ...],
        ) -> bool:
            self.calls.append((entry.entry_id, tuple(platforms)))
            return self.result

    def two_entry_environment() -> tuple[FakeHass, FakeConfigEntry, Any]:
        hass, _entry, runtime, peer = environment("entry-a")
        hass.data[DOMAIN]["entry-b"] = FakeRuntimeData(runtime.source_device, {})
        entry_b = FakeConfigEntry("entry-b")
        add(peer)
        return hass, entry_b, peer

    hass, entry_b, peer = two_entry_environment()
    hass.config_entries = FakeConfigEntries(False)
    notifications = 0
    original_notify = INTEGRATION.notify_supervisor_guard

    def count_failed_notify(current_hass: FakeHass) -> None:
        nonlocal notifications
        notifications += 1
        original_notify(current_hass)

    INTEGRATION.notify_supervisor_guard = count_failed_notify
    failed_arbiter_calls = 0
    original_arbiter = SENSOR.arbitrate_supervisor

    def count_failed_arbiter(*args: Any, **kwargs: Any) -> Any:
        nonlocal failed_arbiter_calls
        failed_arbiter_calls += 1
        return original_arbiter(*args, **kwargs)

    SENSOR.arbitrate_supervisor = count_failed_arbiter
    before_writes = peer.visible_writes
    try:
        result = asyncio.run(INTEGRATION.async_unload_entry(hass, entry_b))
    finally:
        INTEGRATION.notify_supervisor_guard = original_notify
        SENSOR.arbitrate_supervisor = original_arbiter
    check(result is False, "Failed platform unload did not return False")
    check(entry_b.entry_id in hass.data[DOMAIN], "Failed unload removed RuntimeData")
    check(SENSOR._loaded_entry_count(hass) == 2, "Failed unload lowered loaded-entry count")
    check(notifications == 0, "Failed unload notified Supervisor guard")
    check(not peer.available and peer.visible_writes == before_writes, "Failed unload reactivated peer sensor")
    check(failed_arbiter_calls == 0, "Failed unload ran a fresh single-entry recompute")
    check(hass.config_entries.calls == [("entry-b", tuple(INTEGRATION.PLATFORMS))], "Failed unload platform call differs")

    hass, entry_b, peer = two_entry_environment()
    hass.config_entries = FakeConfigEntries(True)
    notifications = 0
    original_notify = INTEGRATION.notify_supervisor_guard
    arbiter_calls = 0
    original_arbiter = SENSOR.arbitrate_supervisor

    def count_success_notify(current_hass: FakeHass) -> None:
        nonlocal notifications
        notifications += 1
        original_notify(current_hass)

    def count_arbiter(*args: Any, **kwargs: Any) -> Any:
        nonlocal arbiter_calls
        arbiter_calls += 1
        return original_arbiter(*args, **kwargs)

    INTEGRATION.notify_supervisor_guard = count_success_notify
    SENSOR.arbitrate_supervisor = count_arbiter
    before_writes = peer.visible_writes
    try:
        result = asyncio.run(INTEGRATION.async_unload_entry(hass, entry_b))
    finally:
        INTEGRATION.notify_supervisor_guard = original_notify
        SENSOR.arbitrate_supervisor = original_arbiter
    check(result is True, "Successful platform unload did not return True")
    check(entry_b.entry_id not in hass.data[DOMAIN], "Successful unload retained RuntimeData")
    check(SENSOR._loaded_entry_count(hass) == 1, "Successful unload did not lower loaded-entry count")
    check(notifications == 1, "Successful unload guard notification count differs")
    check(peer.available and peer.visible_writes == before_writes + 1, "Successful unload did not freshly reactivate peer")
    check(arbiter_calls == 1, "Successful unload did not run exactly one fresh arbiter pass")
    check(peer.native_value is not None and peer.visible_write_history[-1]["available"] is True, "Successful unload reused stale unavailable state")
    check(hass.config_entries.calls == [("entry-b", tuple(INTEGRATION.PLATFORMS))], "Successful unload platform call differs")


def test_lifecycle() -> None:
    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    plan_id = sensor._source_entity_ids["rce_plan"]
    assert plan_id is not None
    before = sensor.write_count
    hass.fire_state(plan_id, None)
    hass.active_delays()[0].run()
    check(sensor.available, "Missing source made whole entity unavailable")
    hass.fire_state(plan_id, FakeState("ignored", _plan_attributes("rce_plan")))
    hass.active_delays()[0].run()
    check(sensor.available and sensor.write_count >= before, "Returning source did not recompute")
    hass.fire_state(SENSOR._SOURCE_BY_KEY["supervisor_profile"].locator, FakeState("Maximum Profit"))
    planner = hass.active_delays()[0]
    temporal = hass.active_points()[0]
    writes = sensor.write_count
    remove(sensor)
    check(planner.cancelled and temporal.cancelled, "Unload did not cancel callbacks")
    check(not any(item["active"] for item in hass.state_listeners), "State listener survived unload")
    check(
        not any(
            active
            for records in hass.bus.listeners.values()
            for _callback, active, _unsubscribe_calls in records
        ),
        "Bus listener survived unload",
    )
    planner.run()
    temporal.run()
    sensor._publish_decision("off", "{}", {})
    check(sensor.write_count == writes, "Late callback/write survived unload")
    hass.data[DOMAIN].pop(sensor._entry.entry_id)
    SENSOR.notify_supervisor_guard(hass)
    check(SENSOR._GUARD_KEY not in hass.data, "Empty guard was not cleaned")


def test_failure_handling() -> None:
    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    plan_id = sensor._source_entity_ids["rce_plan"]
    assert plan_id is not None
    malformed = _plan_attributes("rce_plan")
    malformed["input_revision"] = "1"
    hass.states.values[plan_id] = FakeState("ignored", malformed)
    sensor._recompute()
    check(sensor.available, "Malformed plan field made whole sensor unavailable")
    hass.states.values[plan_id] = FakeState(
        "ignored",
        _plan_attributes("rce_plan"),
        NOW + timedelta(seconds=1),
    )
    sensor._recompute()
    check(sensor.available, "Future plan timestamp made whole sensor unavailable")
    naive = FakeState("ignored", _plan_attributes("rce_plan"), NOW)
    naive.last_reported = NOW.replace(tzinfo=None)
    naive.last_updated = NOW
    hass.states.values[plan_id] = naive
    snapshots = sensor._build_snapshots(sensor._read_source_states(), NOW)
    check(snapshots[2].observed_at is None, "Naive last_reported fell back to authority")
    sensor._recompute()
    check(sensor.available, "Naive timestamp made whole sensor unavailable")
    original_serializer = SENSOR.serialize_supervisor_summary
    SENSOR.serialize_supervisor_summary = lambda _decision: "[]"
    try:
        sensor._recompute()
    finally:
        SENSOR.serialize_supervisor_summary = original_serializer
    check(not sensor.available and sensor.native_value is None and sensor.extra_state_attributes == {}, "Malformed serializer retained stale decision")
    sensor._recompute()
    check(sensor.available, "Sensor did not recover from serializer failure")
    original_context = SENSOR.build_execution_context

    def invalid(*_args: Any, **_kwargs: Any) -> Any:
        raise ValueError("validation detail must stay private")

    SENSOR.build_execution_context = invalid
    try:
        sensor._recompute()
    finally:
        SENSOR.build_execution_context = original_context
    check(not sensor.available and sensor.extra_state_attributes == {}, "Adapter validation error retained data")
    sensor._recompute()

    def explode(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("secret exception text")

    SENSOR.build_execution_context = explode
    try:
        sensor._recompute()
    finally:
        SENSOR.build_execution_context = original_context
    check(not sensor.available and sensor.extra_state_attributes == {}, "Unexpected error retained data")
    check(
        sensor._error_categories
        == {"serializer", "execution_context", "unexpected_execution_context"},
        "Failure categories are not bounded/exact",
    )


def test_static_safety() -> None:
    source = (COMPONENT / "supervisor_sensor.py").read_text(encoding="utf-8")
    lowered = source.casefold()
    forbidden = (
        "hass.services",
        "services.async_call",
        "async_track_time_interval",
        "event_time_changed",
        "restoreentity",
        "owner_code",
        "transaction_pending",
        "market_charg",
        "modbus",
        "os.environ",
        "pathlib",
        "requests.",
        "aiohttp",
        "open(",
    )
    for token in forbidden:
        check(token not in lowered, f"Forbidden static token present: {token}")
    check("supervisormode.active" not in lowered, "Active passed through adapter")
    check(source.count("async_write_ha_state()") == 2, "Unexpected HA publication paths")
    init_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    sensor_source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    check(init_source.count("notify_supervisor_guard(hass)") == 2, "Guard notifications differ")
    runtime_insert = init_source.index("hass.data.setdefault(DOMAIN, {})[entry.entry_id]")
    setup_notify = init_source.index("notify_supervisor_guard(hass)", runtime_insert)
    platform_forward = init_source.index("async_forward_entry_setups", setup_notify)
    check(runtime_insert < setup_notify < platform_forward, "Setup guard notification order differs")
    runtime_pop = init_source.index("hass.data[DOMAIN].pop(entry.entry_id, None)")
    unload_notify = init_source.index("notify_supervisor_guard(hass)", runtime_pop)
    check(runtime_pop < unload_notify, "Unload guard notification precedes RuntimeData pop")
    check('active_translation_keys.add("ems_supervisor")' in init_source, "Registry reconciliation key missing")
    check(sensor_source.count("HoymilesSupervisorSensor(hass, entry, runtime)") == 1, "Entity registration differs")
    supervisor_position = sensor_source.index("HoymilesSupervisorSensor(hass, entry, runtime)")
    rce_position = sensor_source.index("HoymilesRCEOptimizerSensor(hass, entry, runtime)")
    tariff_position = sensor_source.index("HoymilesTariffOptimizerSensor(hass, entry, runtime)")
    rcm_position = sensor_source.index("HoymilesRCMOptimizerSensor(hass, entry, runtime)")
    setup_status_position = sensor_source.index("HoymilesSetupStatusSensor(hass, entry, runtime)")
    add_position = sensor_source.index("async_add_entities(entities)", setup_status_position)
    check(
        supervisor_position
        < rce_position
        < tariff_position
        < rcm_position
        < setup_status_position
        < add_position,
        "Native sensor source order differs",
    )


def _translation_count(payload: dict[str, Any]) -> int:
    return sum(len(entries) for entries in payload["entity"].values())


def test_translations() -> None:
    en_path = COMPONENT / "translations" / "en.json"
    pl_path = COMPONENT / "translations" / "pl.json"
    en = json.loads(en_path.read_text(encoding="utf-8"))
    pl = json.loads(pl_path.read_text(encoding="utf-8"))
    check(en["entity"]["sensor"]["ems_supervisor"] == {"name": "EMS Supervisor"}, "EN translation differs")
    check(pl["entity"]["sensor"]["ems_supervisor"] == {"name": "Nadzorca EMS"}, "PL translation differs")
    generator = (ROOT / "tools" / "build_hacs_assets.py").read_text(encoding="utf-8")
    check(generator.count('["ems_supervisor"]') == 2, "Generator native key differs")
    before = json.loads(subprocess.check_output(["git", "show", "HEAD:custom_components/hoymiles_hit_modbus/translations/en.json"], cwd=ROOT, text=True))
    check(_translation_count(before) == 299, "Baseline localized entity count differs")
    check(_translation_count(en) == 299, "Current localized entity count differs")
    check(_translation_count(en) == _translation_count(before), "Entity-order task changed entity count")


TEST_GROUPS = (
    ("STRUCTURE_AND_MANIFEST", test_structure_and_manifest),
    ("PLATFORM_ENTITY_ORDER", test_platform_entity_order),
    ("FRESH_INSTALL_SEQUENTIAL_ADD", test_fresh_install_sequential_add),
    ("EXISTING_INSTALL_ORDER", test_existing_install_order),
    ("ENTITY_IDENTITY", test_entity_identity),
    ("PUBLICATION", test_publication),
    ("MODE_PROFILE_PERMISSIONS", test_mode_profile_permissions),
    ("ENTRY_RESOLUTION", test_entry_resolution),
    ("MULTI_ENTRY", test_multi_entry),
    ("SOURCE_MAP", test_source_map),
    ("PLAN_WHITELISTS", test_plan_whitelists),
    ("ATOMIC_SNAPSHOT", test_atomic_snapshot),
    ("EVENTING", test_eventing),
    ("TEMPORAL_SCHEDULING", test_temporal_scheduling),
    ("HA_LIFECYCLE_MODEL", test_ha_lifecycle_model),
    ("TRANSACTIONAL_SETUP", test_transactional_setup),
    ("ATOMIC_TIMER_PUBLICATION", test_atomic_timer_publication),
    ("CONFIG_ENTRY_UNLOAD", test_config_entry_unload),
    ("LIFECYCLE", test_lifecycle),
    ("FAILURE_HANDLING", test_failure_handling),
    ("STATIC_SAFETY", test_static_safety),
    ("TRANSLATION_GENERATOR", test_translations),
)


def main() -> None:
    for name, function in TEST_GROUPS:
        group(name, function)
    if CHECKS != EXPECTED_CHECK_COUNT:
        raise AssertionError(
            f"Executed check count differs: expected {EXPECTED_CHECK_COUNT}, got {CHECKS}"
        )
    print(
        "Supervisor sensor contract: PASS "
        f"groups={GROUPS}/{len(TEST_GROUPS)} checks={CHECKS} "
        "sources=60 existing=60 future=0 entry_local=21 global=39 H=46 P=14"
    )


if __name__ == "__main__":
    main()
