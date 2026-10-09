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
# Recorder startup proof, native cached-price entities and Pstryk source.
EXPECTED_CHECK_COUNT = 682
EXPECTED_TASK_BRANCH = "integration/v1.5.8-active-shared-aurora"
EXPECTED_TASK_BASE_SHA = "3112cde86d2969e39b2e7f29e0b9fef380d06716"
EXPECTED_TASK_COMMIT_SUBJECT = (
    "feat(ems): integrate Active supervisor, shared inputs and unified Aurora"
)
EXPECTED_TASK_PATHS = {
    "custom_components/hoymiles_hit_modbus/__init__.py",
    "custom_components/hoymiles_hit_modbus/const.py",
    "custom_components/hoymiles_hit_modbus/ems_supervisor.py",
    "custom_components/hoymiles_hit_modbus/services.yaml",
    "custom_components/hoymiles_hit_modbus/supervisor_active_bridge.py",
    "custom_components/hoymiles_hit_modbus/supervisor_active_controller.py",
    "custom_components/hoymiles_hit_modbus/supervisor_executor.py",
    "custom_components/hoymiles_hit_modbus/supervisor_executor_codec.py",
    "custom_components/hoymiles_hit_modbus/supervisor_sensor.py",
    "home_assistant/hoymiles_ems_scheduler.yaml",
    "packages/settings.yaml",
    "tools/test_supervisor_active_bridge.py",
    "tools/test_supervisor_active_controller.py",
    "tools/test_supervisor_executor.py",
    "tools/test_supervisor_executor_codec.py",
    "tools/test_supervisor_master_stop_service_contract.py",
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

    def is_state(self, entity_id: str, state: str) -> bool:
        value = self.get(entity_id)
        return value is not None and value.state == state

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
        self.report_listeners: list[dict[str, Any]] = []
        self.delay_handles: list[FakeHandle] = []
        self.point_handles: list[FakeHandle] = []
        self.fail_point_creation = False
        self.point_install_observer: Callable[[FakeHandle], None] | None = None
        self.services = SimpleNamespace(
            async_services=lambda: {},
            async_call=self._async_service_call,
        )
        self.storage: dict[str, Any] = {}

    async def _async_service_call(
        self, *_args: Any, **kwargs: Any
    ) -> dict[str, object] | None:
        if kwargs.get("return_response") is True:
            return {
                "schema_version": 1,
                "accepted": True,
                "reason": "accepted",
            }
        return None

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

    def track_reports(
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
        self.report_listeners.append(record)

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

    def fire_report(self, entity_id: str, new_state: FakeState) -> None:
        self.states.values[entity_id] = new_state
        event = FakeEvent({"entity_id": entity_id, "new_state": new_state})
        for registration in tuple(self.report_listeners):
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
    unload_callbacks: list[Callable[[], None]] | None = None

    def async_on_unload(self, callback: Callable[[], None]) -> None:
        if self.unload_callbacks is None:
            self.unload_callbacks = []
        self.unload_callbacks.append(callback)

    def async_create_background_task(
        self,
        _hass: FakeHass,
        coroutine,
        _name: str,
    ) -> asyncio.Task[Any]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            coroutine.close()
            return SimpleNamespace(done=lambda: True, cancel=lambda: None)  # type: ignore[return-value]
        return asyncio.create_task(coroutine)


@dataclass
class FakeRuntimeData:
    source_device: Any
    entities: dict[str, Any]
    shared_inputs: Any = None


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
        self._context = None

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

    def schedule_update_ha_state(self, force_refresh: bool = False) -> None:
        del force_refresh
        self.async_write_ha_state()


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
    sensor = _module(
        "homeassistant.components.sensor",
        SensorDeviceClass=SimpleNamespace(ENERGY="energy"),
        SensorEntity=FakeSensorEntity,
        SensorStateClass=SimpleNamespace(TOTAL_INCREASING="total_increasing"),
    )
    components.sensor = sensor
    homeassistant.components = components
    _module("homeassistant.config_entries", ConfigEntry=FakeConfigEntry)
    _module(
        "homeassistant.const",
        ATTR_ENTITY_ID="entity_id",
        EVENT_CORE_CONFIG_UPDATE="core_config_updated",
        STATE_UNAVAILABLE="unavailable",
        STATE_UNKNOWN="unknown",
        UnitOfEnergy=SimpleNamespace(KILO_WATT_HOUR="kWh"),
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

    class FakeStore:
        def __init__(self, hass: FakeHass, _version: int, key: str) -> None:
            self.hass = hass
            self.key = key

        async def async_load(self) -> Any:
            return deepcopy(self.hass.storage.get(self.key))

        async def async_save(self, value: Any) -> None:
            self.hass.storage[self.key] = deepcopy(value)

    _module("homeassistant.helpers.storage", Store=FakeStore)
    _module(
        "homeassistant.helpers.event",
        async_call_later=lambda hass, delay, callback: hass.call_later(delay, callback),
        async_track_point_in_utc_time=lambda hass, callback, when: hass.track_point(callback, when),
        async_track_state_change_event=lambda hass, ids, callback: hass.track_states(tuple(ids), callback),
        async_track_state_report_event=lambda hass, ids, callback: hass.track_reports(tuple(ids), callback),
        async_track_time_interval=lambda _hass, _callback, _interval: (lambda: None),
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
        EMS_BASELINE_TIMELINE_ENTITY_ID="sensor.hoymiles_ems_baseline_energy_timeline",
        EMS_BASELINE_TIMELINE_TRANSLATION_KEY="ems_baseline_energy_timeline",
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
    _module(
        "homeassistant.exceptions",
        HomeAssistantError=type("HomeAssistantError", (Exception,), {}),
    )
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
        Required=lambda key: key,
        In=lambda values: values,
        Schema=lambda value: value,
    )
    const_module = sys.modules["custom_components.hoymiles_hit_modbus.const"]
    for name, value in {
        "ATTR_ENABLED": "enabled",
        "ATTR_OVERWRITE": "overwrite",
        "ATTR_PAUSED": "paused",
        "ATTR_POLICY": "policy",
        "CONF_RESOLVED_SOURCE_DEVICE_ID": "resolved_source_device_id",
        "CONF_SOURCE_DEVICE_ID": "source_device_id",
        "EMS_PACKAGE_SENTINEL": "binary_sensor.ems_package",
        "EMS_PACKAGE_VERSION": "1.5.7",
        "EMS_PACKAGE_VERSION_ENTITY": "sensor.ems_package_version",
        "PLATFORMS": ("sensor",),
        "SERVICE_INSTALL_ASSETS": "install_assets",
        "SERVICE_MASTER_STOP": "master_stop",
        "SERVICE_RESUME_AFTER_MASTER_STOP": "resume_after_master_stop",
        "SERVICE_SET_EMS_PAUSED": "set_ems_paused",
        "SERVICE_SET_POLICY_ENABLED": "set_policy_enabled",
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
    _module(
        "custom_components.hoymiles_hit_modbus.execution_history_http",
        HoymilesExecutionHistoryView=type("HoymilesExecutionHistoryView", (), {}),
    )
    _module(
        "custom_components.hoymiles_hit_modbus.profit_http",
        HoymilesProfitView=type("HoymilesProfitView", (), {}),
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

    def attach_rce_plan_source(self, _source: Any) -> None:
        return None

    def attach_tariff_plan_source(self, _source: Any) -> None:
        return None

    def attach_tariff_price_source(self, _source: Any) -> None:
        return None

    def attach_supervisor_commitment_source(self, _source: Any) -> None:
        return None

    def observe_supervisor_accounting_feedback(self, **_kwargs: Any) -> None:
        return None

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
    def current_post_command_settling_market_fingerprint(self) -> str | None:
        return None

    async def async_recalculate_post_command_settling(self) -> None:
        return None

    plan_locator = "rce_optimized_plan"


class FakeTariffOptimizerSensor(FakeOptimizerSensor):
    plan_locator = "tariff_charge_plan"


class FakeRCMOptimizerSensor(FakeOptimizerSensor):
    plan_locator = "rcm_voltage_plan"


class FakePstrykPriceSensor(FakeOptimizerSensor):
    plan_locator = "pstryk_public_net_price"


class FakePstrykRuntime:
    active = False

    def __init__(self, hass, entry, runtime, rce, tariff):
        self.sensor = FakePstrykPriceSensor(hass, entry, runtime)

    async def initialize(self):
        return None


class FakeProfitRuntime:
    """No archive I/O in control tests; economics has independent regressions."""
    def __init__(self, *_args):
        pass

    async def initialize(self):
        return None

    async def close(self):
        return None


class FakeTimelineSensor(FakeSensorEntity):
    def __init__(
        self,
        hass: FakeHass,
        entry: FakeConfigEntry,
        runtime: FakeRuntimeData,
        *,
        policy_id: str,
        source_sensor: Any,
    ) -> None:
        self._init_fake_lifecycle()
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self.policy_id = policy_id
        self.source_sensor = source_sensor


class FakePriceCoordinator:
    """Only platform ordering is tested here; real HA I/O has its own suite."""
    def __init__(self, hass):
        self.hass = hass
        self.initialized = self.started = False

    async def async_initialize(self):
        self.initialized = True

    def start(self):
        assert self.initialized
        self.started = True


class FakePriceSensor(FakeSensorEntity):
    def __init__(self, coordinator, entry_id, offset):
        self._init_fake_lifecycle()
        self.coordinator = coordinator
        self.entry_id = entry_id
        self.offset = offset


def _install_sensor_platform_stubs() -> None:
    """Install only imports needed to execute production sensor.async_setup_entry."""
    _module(
        "custom_components.hoymiles_hit_modbus.pstryk_runtime",
        PstrykRuntime=FakePstrykRuntime,
        prepare_price_entity=lambda _hass, _entry: True,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.profit_runtime",
        ProfitRuntime=FakeProfitRuntime,
    )
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
        state_age_seconds=lambda _state, _now: None,
        state_reported_at=lambda _state: None,
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
        "custom_components.hoymiles_hit_modbus.rce_price_sensor",
        HoymilesRCEPriceSensor=FakePriceSensor,
        RCEPriceCoordinator=FakePriceCoordinator,
        prepare_price_entities=lambda _hass, _entry: True,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.tariff_sensor",
        HoymilesTariffOptimizerSensor=FakeTariffOptimizerSensor,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.rcm_sensor",
        HoymilesRCMOptimizerSensor=FakeRCMOptimizerSensor,
    )
    _module(
        "custom_components.hoymiles_hit_modbus.timeline_sensor",
        HoymilesAutomationPlanTimelineSensor=FakeTimelineSensor,
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
            "current_slot_execution_export_power_kw": 4.0,
            "current_slot_planned_export_kwh": 2.0,
            "current_required_minimum_soc_percent": 20.0,
            "system_power_kw": 10.0,
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
            "current_slot_planned_import_power_kw": 5.0,
            "current_slot_planned_charge_power_kw": 4.0,
            "system_power_kw": 10.0,
            "command_charge_power_percent": 40.0,
            "current_run_grid_import_kwh": 2.0,
            "current_run_benefit_pln": 1.0,
            "target_soc_percent": 80.0,
            "model_input_maximum_soc_percent": 100.0,
            "current_grid_charge_run_end": future,
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
        "battery_charge_generation",
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
    if spec.key == "self_use_soc_readback":
        return FakeState("20")
    if spec.key == "backup_soc_readback":
        return FakeState("80")
    if spec.key == "tariff_maximum_soc":
        return FakeState("100")
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
        "grid_to_battery_power",
        "grid_power",
        "battery_power",
        "pv_power",
        "load_power",
    }:
        return FakeState("0")
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
    hass.states.values[SENSOR.EMS_PAUSED_ENTITY_ID] = FakeState("off")
    sensor = SENSOR.HoymilesSupervisorSensor(hass, entry, runtime)
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


def _active_report_registration(hass: FakeHass) -> dict[str, Any]:
    active = [registration for registration in hass.report_listeners if registration["active"]]
    check(len(active) == 1, "Expected exactly one active cohort report listener")
    return active[0]


def _git_paths(*args: str) -> set[str]:
    output = subprocess.check_output(["git", *args], cwd=ROOT, text=True)
    return {line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()}


def _git_text(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _current_task_paths() -> set[str]:
    """Return the integrated task manifest plus any reviewed overlay."""

    changed = _git_paths("diff", "--name-only", "HEAD") | _git_paths(
        "ls-files", "--others", "--exclude-standard"
    )
    committed_paths = _git_paths(
        "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
    )
    if _is_expected_task_commit(committed_paths):
        return committed_paths | changed
    return changed


def _is_expected_task_commit(committed_paths: set[str] | None = None) -> bool:
    """Recognize the frozen integrated candidate below an additional overlay."""

    if committed_paths is None:
        committed_paths = _git_paths(
            "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
        )
    parents = tuple(_git_text("show", "-s", "--format=%P", "HEAD").split())
    return (
        _git_text("branch", "--show-current") == EXPECTED_TASK_BRANCH
        and parents == (EXPECTED_TASK_BASE_SHA,)
        and _git_text("show", "-s", "--format=%s", "HEAD")
        == EXPECTED_TASK_COMMIT_SUBJECT
        and len(committed_paths) == 105
        and EXPECTED_TASK_PATHS <= committed_paths
    )


def _task_baseline_revision() -> str:
    """Use the integrated parent even while a later overlay is under test."""

    return "HEAD^" if _is_expected_task_commit() else "HEAD"


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
    check(
        len(entities) == len(matched_proxies) + 16,
        "Native Supervisor/shared-input entity count changed",
    )
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
        SENSOR_PLATFORM.HoymilesSupervisorAccountingV2Sensor,
        SENSOR_PLATFORM.HoymilesRCEOptimizerSensor,
        SENSOR_PLATFORM.HoymilesTariffOptimizerSensor,
        SENSOR_PLATFORM.HoymilesAutomationPlanTimelineSensor,
        SENSOR_PLATFORM.HoymilesAutomationPlanTimelineSensor,
        SENSOR_PLATFORM.HoymilesRCMOptimizerSensor,
        SENSOR_PLATFORM.HoymilesAutomationPlanTimelineSensor,
        SENSOR_PLATFORM.HoymilesSupervisorCanonicalPlanSensor,
        SENSOR_PLATFORM.HoymilesSetupStatusSensor,
        SENSOR_PLATFORM.HoymilesEMSSharedInputsSensor,
        SENSOR_PLATFORM.HoymilesBaselineEnergyTimelineSensor,
        SENSOR_PLATFORM.HoymilesTariffPriceScheduleSensor,
        SENSOR_PLATFORM.HoymilesRCEPriceSensor,
        SENSOR_PLATFORM.HoymilesRCEPriceSensor,
        FakePstrykPriceSensor,
    )
    actual_native_types = tuple(
        type(entity) for entity in entities[supervisor_position + 1 :]
    )
    check(actual_native_types == expected_native_types, "Native sensor relative order changed")
    for native_type in tuple(dict.fromkeys(expected_native_types)):
        check(
            supervisor_position
            < next(index for index, entity in enumerate(entities) if type(entity) is native_type),
            f"Supervisor is not before {native_type.__name__}",
        )
        check(
            sum(type(entity) is native_type for entity in entities)
            == (3 if native_type is SENSOR_PLATFORM.HoymilesAutomationPlanTimelineSensor
                else 2 if native_type is SENSOR_PLATFORM.HoymilesRCEPriceSensor else 1),
            f"Native entity count differs for {native_type.__name__}",
        )
    check(supervisor._entry is entry, "Supervisor received a different ConfigEntry")
    check(supervisor._runtime is runtime, "Supervisor received a different RuntimeData")
    check(supervisor.hass is hass, "Supervisor received a different HomeAssistant")

    canonical = next(
        entity
        for entity in entities
        if type(entity) is SENSOR_PLATFORM.HoymilesSupervisorCanonicalPlanSensor
    )
    baseline = next(
        entity
        for entity in entities
        if type(entity) is SENSOR_PLATFORM.HoymilesBaselineEnergyTimelineSensor
    )
    canonical_module = sys.modules[canonical.__class__.__module__]
    check(
        canonical._expected_timeline is baseline,
        "Canonical sensor did not receive the prebuilt neutral baseline",
    )
    check(
        set(canonical._source_states()) == {"rce", "tariff", "rcm"},
        "Neutral baseline leaked into canonical authorization source states",
    )

    canonical._subscribe_sources()
    canonical_registration = _supervisor_state_registrations(hass, canonical)
    check(
        len(canonical_registration) == 1
        and baseline.entity_id in canonical_registration[0]["ids"],
        "Canonical sensor did not subscribe to the neutral baseline entity",
    )

    initial_attributes = {
        "canonical_status": "current",
        "result_current": True,
        "recalculation_pending": False,
        "authorization_marker": "unchanged",
    }
    canonical._state = "current"
    canonical._attributes = deepcopy(initial_attributes)
    common_baseline = {
        "schema_version": 1,
        "config_entry_id": entry.entry_id,
        "shared_inputs_revision": 7,
        "quality": "current",
        "source": {"pv": "sensor.solcast"},
        "points": [{"start": NOW.isoformat(), "soc_end_percent": 60.0}],
    }
    canonical._handle_source_event(
        FakeEvent(
            {
                "entity_id": baseline.entity_id,
                "old_state": FakeState(
                    "current",
                    {**common_baseline, "generated_at": NOW.isoformat()},
                ),
                "new_state": FakeState(
                    "current",
                    {
                        **common_baseline,
                        "generated_at": (NOW + timedelta(minutes=5)).isoformat(),
                    },
                ),
            }
        )
    )
    check(
        not hass.active_delays()
        and canonical.native_value == "current"
        and canonical.extra_state_attributes == initial_attributes,
        "Diagnostic-only baseline timestamp churned canonical publication",
    )

    changed_baseline = {
        **common_baseline,
        "shared_inputs_revision": 8,
        "points": [{"start": NOW.isoformat(), "soc_end_percent": 74.0}],
    }
    semantic_event = FakeEvent(
        {
            "entity_id": baseline.entity_id,
            "old_state": FakeState("current", common_baseline),
            "new_state": FakeState("partial", changed_baseline),
        }
    )
    canonical._handle_source_event(semantic_event)
    first_delay = hass.active_delays()
    canonical._handle_source_event(semantic_event)
    check(
        len(first_delay) == 1
        and first_delay[0].when
        == canonical_module.EXPECTED_TIMELINE_REFRESH_DELAY_SECONDS
        and hass.active_delays() == first_delay
        and canonical.native_value == "current"
        and canonical.extra_state_attributes == initial_attributes,
        "Baseline change published pending or escaped canonical coalescing",
    )

    recomputes: list[bool] = []
    canonical._recompute = lambda: recomputes.append(True)
    first_delay[0].run()
    check(
        recomputes == [True]
        and canonical.native_value == "current"
        and canonical.extra_state_attributes == initial_attributes,
        "Coalesced baseline refresh changed authorization before recompute",
    )

    canonical._handle_source_event(semantic_event)
    expected_delay = hass.active_delays()[0]
    canonical._schedule_recompute()
    policy_delays = hass.active_delays()
    check(
        expected_delay.cancelled
        and len(policy_delays) == 1
        and policy_delays[0].when
        == canonical_module.CANONICAL_COHORT_DELAY_SECONDS
        and canonical.native_value == "current"
        and canonical.extra_state_attributes == initial_attributes,
        "Policy refresh did not supersede an expected-only timer silently",
    )
    policy_delays[0].run()

    del canonical._recompute
    for timeline in canonical._timelines.values():
        timeline.native_value = "current"
        timeline.extra_state_attributes = {}
    canonical._supervisor._latest_active_frame = object()
    canonical._capacity_entity_id = "sensor.test_battery_capacity"
    hass.states.values[canonical._capacity_entity_id] = FakeState(
        "230",
        {"unit_of_measurement": "kWh"},
    )
    canonical._schedule_freshness_expiry = lambda: None
    original_builder = canonical_module.build_supervisor_canonical_ledger
    original_serializer = canonical_module.canonical_execution_ledger_to_dict
    original_augment = canonical_module.augment_canonical_projection_payload
    builder_calls: list[dict[str, Any]] = []
    expected_payloads: list[Any] = []

    def fake_builder(**kwargs: Any) -> object:
        builder_calls.append(kwargs)
        return object()

    def fake_augment(payload: Any, **kwargs: Any) -> dict[str, Any]:
        del payload
        expected_payloads.append(kwargs.get("expected_timeline"))
        return {
            "canonical_status": "current",
            "result_current": True,
            "recalculation_pending": False,
            "authorization_marker": "unchanged",
        }

    canonical_module.build_supervisor_canonical_ledger = fake_builder
    canonical_module.canonical_execution_ledger_to_dict = lambda _ledger: {}
    canonical_module.augment_canonical_projection_payload = fake_augment
    baseline._attributes = {
        "schema_version": 1,
        "quality": "partial",
        "shared_inputs_revision": 8,
        "points": changed_baseline["points"],
    }
    baseline._state = "partial"
    try:
        canonical._recompute()
        baseline._attributes = ["malformed"]
        canonical._recompute()
    finally:
        canonical_module.build_supervisor_canonical_ledger = original_builder
        canonical_module.canonical_execution_ledger_to_dict = original_serializer
        canonical_module.augment_canonical_projection_payload = original_augment
    check(
        len(builder_calls) == 2
        and all("expected_timeline" not in call for call in builder_calls)
        and expected_payloads
        == [
            {
                "schema_version": 1,
                "quality": "partial",
                "shared_inputs_revision": 8,
                "points": changed_baseline["points"],
                "state": "partial",
            },
            None,
        ]
        and canonical.native_value == "current"
        and canonical.extra_state_attributes["authorization_marker"]
        == "unchanged",
        "Canonical adapter blocked authorization or leaked baseline into the ledger",
    )


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
    initial_publication = deepcopy(supervisor.visible_write_history[-1])
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
    optimizer_publication_counts: list[int] = []
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
        optimizer_publication_counts.append(supervisor.visible_writes)

    check(
        all(supervisor._source_entity_ids[key] == plan_ids[key] for key in plan_keys),
        "Final plan source mapping is incomplete",
    )
    check(
        all(supervisor._read_source_states()[key] is None for key in plan_keys),
        "Missing plan state became available after registry creation",
    )
    check(supervisor.available and supervisor.native_value == "off", "Fresh sequence did not remain safely Off")
    publications = supervisor.visible_write_history
    final_publication = deepcopy(publications[-1])
    context_evidence = final_publication["attributes"].get(
        "execution_context_evidence"
    )
    final_publication["attributes"]["execution_context_evidence"] = (
        initial_publication["attributes"].get("execution_context_evidence")
    )
    # The first complete frame now proves why export remains unavailable.
    # Keep that material transition, then compare the rest of the startup
    # publication with the pre-frame snapshot.
    first_export = initial_publication["attributes"].get("rce_export_evidence")
    final_export = final_publication["attributes"].get("rce_export_evidence")
    first_recorded = initial_publication["attributes"].get("recorded_execution")
    final_recorded = final_publication["attributes"].get("recorded_execution")
    check(
        isinstance(first_export, dict)
        and first_export.get("reason") == "no_frame"
        and isinstance(final_export, dict)
        and final_export.get("status") == "pending"
        and final_export.get("reason") == "no_net_export"
        and final_export.get("control_authority") is False
        and isinstance(first_recorded, dict)
        and isinstance(final_recorded, dict)
        and first_recorded.get("rce_export_evidence") == {
            "status": "pending", "reason": "no_frame", "control_authority": None,
        }
        and final_recorded.get("rce_export_evidence") == {
            "status": "pending", "reason": "no_net_export",
            "control_authority": False,
        },
        "First complete frame lost its bounded export evidence transition",
    )
    final_publication["attributes"]["rce_export_evidence"] = first_export
    final_recorded["rce_export_evidence"] = first_recorded["rce_export_evidence"]
    check(
        len(publications) == 2
        and optimizer_publication_counts == [2, 2, 2]
        and initial_publication["attributes"].get("execution_context_evidence")
        is None
        and isinstance(context_evidence, dict)
        and final_publication == initial_publication,
        "Registry creation published more than the first completed execution-context frame",
    )
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
    check(
        "retry_blocked: bool = False" in source,
        "MASTER STOP retry must remain explicit and disabled by default",
    )
    check(
        source.count("retry_blocked=True") == 1,
        "Only the explicit MASTER STOP request may retry a blocked attempt",
    )
    check(
        "in {MasterStopStatus.BLOCKED, MasterStopStatus.FAILED}" in source,
        "Explicit blocked/failed MASTER STOP retry gate is missing",
    )
    changed = _current_task_paths()
    check(
        all((ROOT / path).is_file() for path in EXPECTED_TASK_PATHS),
        "Integrated Active task paths are missing from the worktree",
    )
    if _is_expected_task_commit():
        check(
            EXPECTED_TASK_PATHS <= changed,
            f"Active task paths missing: {sorted(EXPECTED_TASK_PATHS - changed)}",
        )
    check(not _git_paths("diff", "--cached", "--name-only"), "Staged files exist")
    check(
        not any(path.startswith(".git/") for path in changed),
        "Task manifest escaped the worktree",
    )
    blocked_by_tool_policy = {
        ROOT / "tools" / "__pycache__" / "build_hacs_assets.cpython-312.pyc",
        ROOT
        / "tools"
        / "__pycache__"
        / "test_optimizer_executor_contract.cpython-312.pyc",
    }
    artifacts = [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and (path.suffix in {".pyc", ".pyo"} or path.name.endswith(".tmp"))
        and path not in blocked_by_tool_policy
    ]
    check(not artifacts, f"Temporary artifacts found: {artifacts[:3]}")


def test_entity_identity() -> None:
    hass, entry, _runtime, sensor = environment()
    check(sensor._attr_should_poll is False, "Polling enabled")
    check(sensor._attr_has_entity_name is True, "Entity name contract changed")
    check(sensor._attr_translation_key == "ems_supervisor", "Translation key changed")
    check(sensor._attr_unique_id == f"{entry.entry_id}_ems_supervisor", "Unique ID changed")
    check(
        sensor.entity_id == "sensor.hoymiles_hit_ems_supervisor",
        "Supervisor entity ID must remain canonical across site entity-ID formats",
    )
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
    check(
        {key: sensor.extra_state_attributes[key] for key in expected} == expected,
        "Pure decision projection differs from exact serializer",
    )
    check(
        {
            "active_state",
            "selected_action",
            "owner",
            "transaction_id",
            "started_at",
            "deadline",
            "command_snapshot",
            "expected_readback",
            "physical_verification_result",
            "rollback_result",
            "master_stop",
        }
        <= set(sensor.extra_state_attributes),
        "A4 lifecycle evidence is incomplete",
    )
    mode_entity_id = sensor._source_entity_ids["supervisor_mode"]
    assert mode_entity_id is not None
    hass.states.values[mode_entity_id] = FakeState("Active")
    sensor._recompute()
    check(
        sensor._controller is not None
        and sensor._controller.record.state is SENSOR.ActiveState.IDLE,
        "Active no-action publication did not retain executor IDLE",
    )
    check(
        sensor.extra_state_attributes["active_state"]
        == SENSOR.ActiveState.IDLE.value,
        "Active no-action publication lost lifecycle IDLE evidence",
    )
    check(
        sensor.native_value == "active_idle",
        "Executor IDLE overwrote the pure Active idle state",
    )
    check(
        sensor.extra_state_attributes["execution_phase"] == "idle"
        and sensor.extra_state_attributes["lifecycle_reason"] == "idle",
        "Executor IDLE did not replace the pure execution phase",
    )
    check(
        sensor.extra_state_attributes["observed_owner"] == "none"
        and sensor.extra_state_attributes["transaction_owner"] == "none"
        and sensor.extra_state_attributes["owner"] == "none"
        and sensor.extra_state_attributes["owner_conflict"] is False,
        "Normal Active idle ownership projection differs",
    )
    check(set(sensor.__dict__) >= {"_attributes", "_serialized_summary"}, "Publication cache missing")
    serialized = sensor._serialized_summary.encode("utf-8")
    check(len(serialized) <= 65_536, "Composite Supervisor summary bound exceeded")
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
    check(
        "*" not in SENSOR.HoymilesSupervisorSensor._unrecorded_attributes
        and "recorded_execution" not in SENSOR.HoymilesSupervisorSensor._unrecorded_attributes
        and sensor.extra_state_attributes["recorded_execution"]
        == SENSOR.supervisor_recorder_projection(sensor.extra_state_attributes),
        "Recorder removed compact execution proof or added a wildcard",
    )


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
        hass.states.values[mode_id] = FakeState("Active")
        hass.states.values[profile_id] = FakeState(label)
        sensor._recompute()
        check(sensor.extra_state_attributes["supervisor_mode"] == "active", "Active rejected")
        check(sensor.extra_state_attributes["profile"] == expected, f"Profile {label} rejected")
    observed_modes: list[Any] = []
    original = SENSOR.arbitrate_supervisor

    def observe(**kwargs: Any) -> Any:
        observed_modes.append(kwargs["mode"])
        return original(**kwargs)

    SENSOR.arbitrate_supervisor = observe
    try:
        for invalid in ("Shadow", "shadow", "unsupported", "unavailable"):
            hass.states.values[mode_id] = FakeState(invalid)
            sensor._recompute()
            check(sensor.extra_state_attributes["supervisor_mode"] == "off", f"{invalid} escaped Off")
        hass.states.values[profile_id] = FakeState("invalid profile text")
        sensor._recompute()
        sensor._recompute()
    finally:
        SENSOR.arbitrate_supervisor = original
    check(all(mode is CORE.SupervisorMode.OFF for mode in observed_modes), "Invalid mode reached core")
    check(
        sensor._warning_categories
        == {
            "unknown_or_unavailable_mode",
            "malformed_or_unsupported_mode",
            "invalid_profile",
        },
        "Warning categories are not finite/exact",
    )
    rcm_enabled = SENSOR._SOURCE_BY_KEY["rcm_enabled"].locator
    hass.states.values[rcm_enabled] = FakeState("on")
    sensor._recompute()
    rcm = next(item for item in sensor.extra_state_attributes["candidate_summaries"] if item["policy_id"] == "rcm")
    check(rcm["enabled"] is True, "RCEm planner enable was not preserved")

    hass.states.values[mode_id] = FakeState("Active")
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
    check(sensor.extra_state_attributes["supervisor_mode"] == "active", "Healthy Active frame was rejected")
    rce_summary = next(
        item
        for item in sensor.extra_state_attributes["candidate_summaries"]
        if item["policy_id"] == "rce"
    )
    check(
        rce_summary["enabled"] is True
        and rce_summary["requested_action"] == "rce_export",
        "Active RCE candidate identity was not preserved",
    )
    check(
        sensor._controller is not None
        and sensor._controller.record.state is SENSOR.ActiveState.IDLE
        and sensor.native_value == "selected",
        "Pure selection did not normalize to the stable public selected state",
    )
    check(
        sensor.extra_state_attributes["selected_policy"] == "rce"
        and sensor.extra_state_attributes["selected_candidate_revision"]
        == rce_summary["candidate_revision"],
        "IDLE recorder erased the pure selected policy identity",
    )
    check(
        sensor.extra_state_attributes["execution_phase"] == "selected"
        and sensor.extra_state_attributes["observed_owner"] == "none"
        and sensor.extra_state_attributes["transaction_owner"] == "none"
        and sensor.extra_state_attributes["owner"] == "none"
        and sensor.extra_state_attributes["transaction_id"] is None,
        "Logical selection invented a transaction or execution owner",
    )
    del hass.states.values[
        SENSOR._SOURCE_BY_KEY["rce_price_above_threshold"].locator
    ]
    sensor._recompute()
    check(
        sensor.extra_state_attributes["selected_policy"] == "rce"
        and sensor.native_value == "selected",
        "Fresh-install RCE selection still depends on the legacy slot-helper entity ID",
    )

    decision_attributes = deepcopy(sensor._decision_attributes)
    candidate_revision = decision_attributes["selected_candidate_revision"]
    phase_by_state = {
        SENSOR.ActiveState.SELECTED: "selected",
        SENSOR.ActiveState.STARTING: "starting",
        SENSOR.ActiveState.WAITING_READBACK: "waiting_readback",
        SENSOR.ActiveState.EXECUTING: "executing",
        SENSOR.ActiveState.STOPPING: "stopping",
        SENSOR.ActiveState.RESTORING: "restoring",
        SENSOR.ActiveState.BLOCKED: "blocked",
        SENSOR.ActiveState.FAULT: "fault",
    }
    reason_by_state = {
        SENSOR.ActiveState.SELECTED: "candidate_selected",
        SENSOR.ActiveState.STARTING: "owner_acquired",
        SENSOR.ActiveState.WAITING_READBACK: "command_ready",
        SENSOR.ActiveState.EXECUTING: "physically_confirmed",
        SENSOR.ActiveState.STOPPING: "stop_requested",
        SENSOR.ActiveState.RESTORING: "restoring",
        SENSOR.ActiveState.BLOCKED: "owner_conflict",
        SENSOR.ActiveState.FAULT: "command_outcome_unknown",
    }

    class CompositeController:
        def __init__(self, lifecycle_state: Any) -> None:
            owner = (
                SENSOR.ExecutionOwner.NONE
                if lifecycle_state
                in {SENSOR.ActiveState.SELECTED, SENSOR.ActiveState.BLOCKED}
                else SENSOR.ExecutionOwner.RCE
            )
            self.record = SimpleNamespace(
                state=lifecycle_state,
                owner=owner,
                transaction=(
                    None
                    if lifecycle_state is SENSOR.ActiveState.BLOCKED
                    else SimpleNamespace(transaction_id="rce:test-transaction")
                ),
            )

        def recorder_attributes(self) -> dict[str, Any]:
            phase = phase_by_state[self.record.state]
            return {
                "active_state": self.record.state.value,
                "execution_phase": phase,
                "selected_policy": (
                    "rce" if self.record.transaction is not None else None
                ),
                "transaction_candidate_identity": (
                    "rce:rce_export:" + "a" * 64
                    if self.record.transaction is not None
                    else None
                ),
                "selected_action": "rce_export",
                "lifecycle_reason": reason_by_state[self.record.state],
                "owner": self.record.owner.value,
                "owner_conflict": False,
                "transaction_id": "rce:test-transaction",
                "supervisor_execution_authorized": (
                    self.record.state is SENSOR.ActiveState.EXECUTING
                ),
            }

    for lifecycle_state, expected_phase in phase_by_state.items():
        sensor._controller = CompositeController(lifecycle_state)
        published_decision_attributes = deepcopy(decision_attributes)
        if lifecycle_state in {
            SENSOR.ActiveState.STOPPING,
            SENSOR.ActiveState.RESTORING,
        }:
            published_decision_attributes["selected_policy"] = "tariff"
            published_decision_attributes["selected_candidate_revision"] = None
        sensor._publish_decision(
            CORE.SupervisorState.ACTIVE_SELECTED.value,
            "",
            published_decision_attributes,
        )
        check(
            sensor.native_value == expected_phase
            and sensor.extra_state_attributes["execution_phase"]
            == expected_phase
            and sensor.extra_state_attributes["lifecycle_reason"]
            == reason_by_state[lifecycle_state],
            f"{expected_phase} lifecycle was not published as a public state/phase",
        )
        check(
            sensor.extra_state_attributes["selected_policy"] == "rce"
            and sensor.extra_state_attributes["selected_candidate_revision"]
            == candidate_revision,
            f"{expected_phase} lifecycle lost selected policy identity",
        )
        expected_owner = (
            "none"
            if lifecycle_state
            in {SENSOR.ActiveState.SELECTED, SENSOR.ActiveState.BLOCKED}
            else "rce"
        )
        check(
            sensor.extra_state_attributes["observed_owner"] == "none"
            and sensor.extra_state_attributes["transaction_owner"]
            == expected_owner
            and sensor.extra_state_attributes["owner"] == expected_owner,
            f"{expected_phase} lifecycle merged ownership incorrectly",
        )
        check(
            sensor.extra_state_attributes["execution_phase"]
            != "observed_active_latched",
            f"{expected_phase} leaked the pure observed-active phase",
        )
        if lifecycle_state in {
            SENSOR.ActiveState.BLOCKED,
            SENSOR.ActiveState.FAULT,
        }:
            check(
                sensor.extra_state_attributes["selection_reason"]
                == decision_attributes["selection_reason"]
                and sensor.extra_state_attributes["execution_blocked_reason"]
                == decision_attributes["execution_blocked_reason"]
                and sensor.extra_state_attributes["lifecycle_reason"]
                == reason_by_state[lifecycle_state],
                f"{expected_phase} conflated lifecycle and policy reasons",
            )


def test_idle_owner_publication() -> None:
    scenarios = (
        ("normal", {}, "none", False),
        ("manual", {"manual_charge_active": "on"}, "manual", False),
        ("balancing", {"balancing_active": "on"}, "balancing", False),
        ("foreign", {"ems_mode_readback": "4"}, "foreign", False),
        (
            "conflict",
            {"manual_charge_active": "on", "balancing_active": "on"},
            "unknown",
            True,
        ),
    )
    for label, overrides, expected_owner, expected_conflict in scenarios:
        hass, entry, _runtime, sensor = environment()
        for key, value in overrides.items():
            spec = SENSOR._SOURCE_BY_KEY[key]
            hass.states.values[_source_entity_id(spec, entry.entry_id)] = (
                FakeState(value)
            )
        add(sensor)
        check(
            sensor._controller is not None
            and sensor._controller.record.state is SENSOR.ActiveState.IDLE,
            f"{label} fixture did not remain executor IDLE",
        )
        check(
            sensor.extra_state_attributes["observed_owner"] == expected_owner
            and sensor.extra_state_attributes["transaction_owner"] == "none"
            and sensor.extra_state_attributes["owner"] == "none",
            f"{label} executor IDLE exposed a public transaction owner",
        )
        check(
            sensor.extra_state_attributes["owner_conflict"]
            is expected_conflict,
            f"{label} owner conflict projection differs",
        )
        check(
            sensor.extra_state_attributes["transaction_id"] is None
            and sensor.extra_state_attributes["execution_phase"] == "idle",
            f"{label} idle ownership invented a transaction lifecycle",
        )


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
            frozen_rows[:67],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    check(
        source_map_hash
        == "3759b2f9b85bfabec8b65e2ddd560d607d7cc16f7f444d75a7afa09d57f5839a",
        "Exact frozen source map differs",
    )
    check(frozen_rows[67:] == (
        (68,"tariff_maximum_soc","input_number.hoymiles_tariff_maximum_soc",False,False,False),
        (69,"rce_requested_discharge_power","input_number.hoymiles_rce_requested_discharge_power",False,False,False),
        (70,"pv_charge_delay_enabled","input_boolean.hoymiles_pv_charge_delay_enabled",False,True,False),
        (71,"bms_battery_power","battery_power_bms",True,False,False),
    ), "Only the tariff maximum and raw RCE requested power extend the source map")
    check(len(specs) == 71, "Logical source count differs")
    check(tuple(spec.number for spec in specs) == tuple(range(1, 72)), "Source numbers differ")
    check(sum(not spec.future_helper for spec in specs) == 71, "Existing source count differs")
    check(sum(spec.future_helper for spec in specs) == 0, "Future source count differs")
    check(all(not specs[index].future_helper for index in range(5)), "Persistent helper sources remain future")
    check(sum(spec.entry_local for spec in specs) == 29, "Entry-local count differs")
    check(sum(not spec.entry_local for spec in specs) == 42, "Global count differs")
    check(sum(not spec.planner_event for spec in specs) == 56, "H count differs")
    check(sum(spec.planner_event for spec in specs) == 15, "P count differs")
    check({spec.number for spec in specs if spec.planner_event} == {2, 3, 4, 5, 6, 7, 12, 14, 15, 21, 22, 23, 24, 25, 70}, "P set differs")
    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    ids = _active_state_registration(hass)["ids"]
    check(len(ids) == len(set(ids)) == 71, "Healthy watched IDs are not exact/unique")
    forbidden = {
        "sensor.hoymiles_hit_ems_supervisor",
        "sensor.hoymiles_ems_control_owner",
        "binary_sensor.hoymiles_ems_control_conflict",
        "sensor.hoymiles_parallel_aggregate_physical_response",
    }
    check(not forbidden.intersection(ids), "Forbidden source watched")


def test_plan_whitelists() -> None:
    expected_rce = (
        "pv_charge_delay_execution_ready", "pv_charge_delay_end",
    "rce_today_data_fresh", "forecast_today_data_fresh", "soc_data_fresh", "gcf_execution_data_fresh",
        "price_provider", "joint_plan_revision", "joint_profile_revision",
        "current_slot_execution_export_power_kw",
        "status_code", "result_current", "recalculation_pending",
        "input_revision", "current_slot_planned",
        "current_slot_start_eligible", "current_slot_continue_eligible",
        "current_slot_end", "current_run_end",
        "current_slot_execution_discharge_power_kw",
        "current_slot_planned_export_kwh",
        "current_required_minimum_soc_percent",
        "system_power_kw",
        "current_slot_suppression_reason",
        "current_slot_load_exhausts_requested_discharge_budget",
        "current_slot_load_only_export_suppressed",
        "post_command_settling_market_fingerprint",
    )
    expected_tariff = (
        "price_provider", "joint_plan_revision", "joint_profile_revision",
        "current_slot_planned_import_power_kw",
        "current_slot_planned_charge_power_kw",
        "status_code", "result_current", "recalculation_pending",
        "input_revision", "current_slot_planned", "current_action",
        "current_run_need_class", "current_run_start_eligible",
        "current_run_continue_eligible", "current_run_suppression_reason",
        "current_run_continue_reason", "requested_charge_power_kw", "system_power_kw",
        "command_charge_power_percent", "current_run_grid_import_kwh",
        "requested_target_energy_kwh", "demand_margin_requested_kwh",
        "demand_margin_unserved_kwh", "base_energy_shortfall_kwh",
        "current_run_benefit_pln", "target_soc_percent",
        "base_reserve_soc_percent", "current_slot_end",
        "current_grid_charge_run_end",
        "control_inputs_fresh", "forecast_data_fresh", "control_input_block_reason",
        "load_profile_source", "configured_daily_fallback_kwh",
        "input_change_reason", "input_change_previous_value", "input_change_new_value",
        "bms_charge_power_limit_kw",
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
    check(rce.system_power_kw == 10.0, "RCE system power projection differs")
    check(tariff.current_run_need_class is RUNTIME.TariffRunNeed.NONE, "Tariff need projection differs")
    check(tariff.current_action is RUNTIME.TariffAction.NONE, "Tariff action projection differs")
    check(tariff.system_power_kw == 10.0, "Tariff system power projection differs")
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
    rce_plan = states["rce_plan"]
    tariff_plan = states["tariff_plan"]
    assert rce_plan is not None and tariff_plan is not None
    rce_plan.attributes["status_code"] = "home_energy_shortage"
    rce_plan.attributes["current_slot_planned"] = False
    tariff_plan.attributes["status_code"] = "no_charge_needed"
    tariff_plan.attributes["current_slot_planned"] = False
    tariff_plan.attributes["current_action"] = "none"
    tariff_plan.attributes["current_run_need_class"] = "none"
    live_rce, live_tariff = sensor._build_snapshots(states, NOW)[2:4]
    check(
        live_rce.status_code is RUNTIME.RcePlanStatus.HOME_ENERGY_SHORTAGE,
        "Live RCE no-action status was discarded",
    )
    check(
        live_tariff.status_code is RUNTIME.TariffPlanStatus.NO_CHARGE_NEEDED,
        "Live tariff no-action status was discarded",
    )
    for candidate in (
        RUNTIME.build_rce_candidate(live_rce, now=NOW),
        RUNTIME.build_tariff_candidate(live_tariff, now=NOW),
    ):
        check(
            candidate.available
            and candidate.requested_action is CORE.RequestedAction.NONE
            and candidate.blocked_reason is None,
            "Live current no-action plan became unavailable",
        )
    rce_plan.attributes["status_code"] = "ready"
    rce_plan.attributes["current_slot_planned"] = rce.current_slot_planned
    tariff_plan.attributes["status_code"] = "ready"
    tariff_plan.attributes["current_slot_planned"] = tariff.current_slot_planned
    tariff_plan.attributes["current_action"] = tariff.current_action.value
    tariff_plan.attributes["current_run_need_class"] = (
        tariff.current_run_need_class.value
    )
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
    duplicate_reads = {
        entity_id: count for entity_id, count in hass.states.reads.items() if count > 1
    }
    check(not duplicate_reads, f"A source was read more than once: {duplicate_reads}")
    check(sum(hass.states.reads.values()) == 74, "Atomic pass must read 71 sources, pause latch and two price-provider selectors exactly once")
    check(counts == {name: 1 for name in counts}, f"Pipeline call counts differ: {counts}")


def test_executor_commitment_recompute() -> None:
    """Keep one tariff transaction valid across every persisted executor state."""

    _hass, _entry, _runtime, sensor = environment()
    sensor._resolve_source_entity_ids()
    states = sensor._read_source_states()
    _mode, _profile, rce, tariff, rcm, execution = sensor._build_snapshots(
        states,
        NOW,
    )
    execution = replace(execution, full_block_execution_ready=True)
    tariff = replace(
        tariff,
        allowed_by_user=True,
        enabled=True,
        current_slot_planned=True,
        current_action=RUNTIME.TariffAction.BATTERY_CHARGE,
        current_run_need_class=RUNTIME.TariffRunNeed.REQUIRED_ENERGY,
        current_run_start_eligible=True,
        current_run_continue_eligible=True,
        command_charge_power_percent=55.0,
        control_data_ready=True,
        planned_slot_ready=True,
    )
    transaction = SimpleNamespace(
        owner=SENSOR.ExecutionOwner.TARIFF,
        deadline=NOW + timedelta(hours=1),
        intent=SimpleNamespace(
            action=SENSOR.ExecutionAction.TARIFF_BATTERY_CHARGE,
            command=SimpleNamespace(
                ems_block=SimpleNamespace(
                    force_charge_soc_percent_4303=80.0,
                    maximum_charge_power_percent_4304=40.0,
                )
            ),
        ),
    )

    def committed(
        state: Any,
        owner: Any,
        *,
        active_execution: bool = False,
    ) -> tuple[Any, Any, Any]:
        current_execution = (
            replace(
                execution,
                physical_mode_code=4,
                force_charge_soc_percent=80.0,
                maximum_charge_power_percent=40.0,
            )
            if active_execution
            else execution
        )
        context = RUNTIME.build_execution_context(current_execution, now=NOW)
        sensor._controller = SimpleNamespace(
            record=SimpleNamespace(
                state=state,
                owner=owner,
                transaction=(None if state is SENSOR.ActiveState.IDLE else transaction),
            )
        )
        _rce, current_tariff, _rcm, current_context = (
            sensor._apply_executor_commitment(
                rce,
                tariff,
                rcm,
                context,
                current_execution,
                now=NOW,
            )
        )
        candidates = (
            RUNTIME.build_rce_candidate(_rce, now=NOW),
            RUNTIME.build_tariff_candidate(current_tariff, now=NOW),
            RUNTIME.build_rcm_candidate(_rcm, now=NOW),
        )
        decision = CORE.arbitrate_supervisor(
            mode=CORE.SupervisorMode.ACTIVE,
            profile=CORE.SupervisorProfile.BALANCED,
            context=current_context,
            candidates=candidates,
            now=NOW,
        )
        return current_context, current_tariff, decision

    idle_context, _idle_tariff, idle_decision = committed(
        SENSOR.ActiveState.IDLE,
        SENSOR.ExecutionOwner.NONE,
    )
    check(
        idle_context.owner_kind is CORE.OwnerKind.NONE
        and not idle_context.transaction_pending
        and idle_context.transaction_owner_kind is CORE.OwnerKind.NONE,
        "IDLE recompute invented an owner or pending transaction",
    )
    check(
        idle_decision.selected_policy is CORE.PolicyId.TARIFF,
        "IDLE recompute did not select the eligible tariff candidate",
    )

    selected_context, _selected_tariff, selected_decision = committed(
        SENSOR.ActiveState.SELECTED,
        SENSOR.ExecutionOwner.NONE,
    )
    check(
        selected_context.owner_kind is CORE.OwnerKind.NONE
        and not selected_context.transaction_pending,
        "SELECTED acquired an owner before transaction start",
    )
    check(
        selected_decision.selected_policy is CORE.PolicyId.TARIFF,
        "SELECTED recompute lost the tariff candidate",
    )

    for state in (
        SENSOR.ActiveState.STARTING,
        SENSOR.ActiveState.WAITING_READBACK,
    ):
        pending_context, pending_tariff, pending_decision = committed(
            state,
            SENSOR.ExecutionOwner.TARIFF,
        )
        check(
            pending_context.owner_kind is CORE.OwnerKind.TARIFF
            and pending_context.transaction_pending
            and pending_context.transaction_owner_kind is CORE.OwnerKind.TARIFF,
            f"{state.value} recompute lost the exact pending-owner relation",
        )
        check(
            pending_tariff.active_latched is (state is SENSOR.ActiveState.WAITING_READBACK),
            f"{state.value} tariff capture must follow the dispatched transaction",
        )

    executing_context, executing_tariff, executing_decision = committed(
        SENSOR.ActiveState.EXECUTING,
        SENSOR.ExecutionOwner.TARIFF,
        active_execution=True,
    )
    check(
        executing_context.owner_kind is CORE.OwnerKind.TARIFF
        and not executing_context.transaction_pending
        and executing_context.transaction_owner_kind is CORE.OwnerKind.NONE,
        "EXECUTING recompute exposed a non-pending transaction owner",
    )
    check(
        executing_tariff.active_latched is True,
        "EXECUTING recompute did not expose the persisted tariff latch",
    )
    check(
        executing_tariff.command_charge_power_percent == 55.0,
        "EXECUTING tariff recompute hid the fresh desired 4304",
    )
    check(
        executing_tariff.latched_target_soc_percent == 80.0,
        "EXECUTING recompute lost the sent target",
    )
    check(
        executing_decision.selected_policy is None,
        "changed tariff target requires explicit retarget proof instead of ordinary selection",
    )

    rce_deadline = NOW + timedelta(hours=1)
    start_rce = replace(
        rce,
        allowed_by_user=True,
        enabled=True,
        active_latched=False,
        status_code=RUNTIME.RcePlanStatus.READY,
        result_current=True,
        recalculation_pending=False,
        input_revision=1,
        current_slot_planned=True,
        current_slot_start_eligible=True,
        current_slot_continue_eligible=True,
        current_slot_end=rce_deadline,
        current_run_end=rce_deadline,
        requested_discharge_power_kw=32.0,
        planned_export_energy_kwh=16.0,
        protected_soc_floor_percent=48.0,
        effective_discharge_power_percent=100.0,
        current_soc_percent=90.0,
        control_data_ready=True,
        price_above_threshold=True,
        reserve_ready=True,
        sale_block_active=False,
    )
    start_rce_candidate = RUNTIME.build_rce_candidate(start_rce, now=NOW)
    recalculated_rce = replace(
        start_rce,
        input_revision=2,
        protected_soc_floor_percent=49.0,
        effective_discharge_power_percent=55.0,
        requested_discharge_power_kw=17.6,
        planned_export_energy_kwh=8.8,
    )
    rce_execution = replace(
        execution,
        physical_mode_code=5,
        force_discharge_soc_percent=48.0,
        maximum_discharge_power_percent=100.0,
    )
    rce_context = RUNTIME.build_execution_context(rce_execution, now=NOW)
    sensor._controller = SimpleNamespace(
        record=SimpleNamespace(
            state=SENSOR.ActiveState.EXECUTING,
            owner=SENSOR.ExecutionOwner.RCE,
            transaction=SimpleNamespace(
                deadline=rce_deadline,
                intent=SimpleNamespace(
                    action=SENSOR.ExecutionAction.RCE_EXPORT,
                    command=SimpleNamespace(
                        ems_block=SimpleNamespace(
                            force_discharge_soc_percent_4305=48.0,
                            maximum_discharge_power_percent_4306=100.0,
                        )
                    ),
                ),
            ),
        )
    )
    committed_rce, _tariff, _rcm, _context = sensor._apply_executor_commitment(
        recalculated_rce,
        tariff,
        rcm,
        rce_context,
        rce_execution,
        now=NOW,
    )
    continued_rce_candidate = RUNTIME.build_rce_candidate(committed_rce, now=NOW)
    check(
        committed_rce.latched_minimum_soc_percent == 49.0
        and committed_rce.effective_discharge_power_percent == 55.0,
        "EXECUTING RCE recompute hid the latest same-run 4305/4306 target",
    )
    check(
        continued_rce_candidate.local_hard_stop
        and not continued_rce_candidate.continuation_eligible
        and continued_rce_candidate.desired_actuator_fingerprint
        != start_rce_candidate.desired_actuator_fingerprint,
        "RCE target change was not exposed as sole old-readback incoherence",
    )

    threshold_only_rce, _tariff, _rcm, _context = (
        sensor._apply_executor_commitment(
            replace(
                recalculated_rce,
                effective_discharge_power_percent=100.0,
                requested_discharge_power_kw=32.0,
            ),
            tariff,
            rcm,
            rce_context,
            rce_execution,
            now=NOW,
        )
    )
    threshold_only_candidate = RUNTIME.build_rce_candidate(
        threshold_only_rce,
        now=NOW,
    )
    check(
        threshold_only_rce.latched_minimum_soc_percent == 49.0
        and threshold_only_rce.effective_discharge_power_percent == 100.0
        and threshold_only_candidate.local_hard_stop
        and threshold_only_candidate.desired_actuator_fingerprint
        != start_rce_candidate.desired_actuator_fingerprint,
        "threshold-only RCE retarget remained latched to old 4305",
    )

    # Field M01 chronology (parallel Master/Slave) showed that lowering the
    # protected floor 53 -> 52 during an otherwise unchanged Mode 5 run caused
    # a full stop/restore/restart and a transient aggregate-power overshoot.
    # A lower floor is never safety-critical inside the already leased run: keep
    # the physically confirmed higher floor until the next natural transaction.
    # The rule is topology-independent so the single-inverter path must retain
    # the same command identity as well.
    original_rce_controller = sensor._controller
    for machine_type_code, inverter_count, direct_259_ready in (
        (0.0, 1.0, True),
        (1.0, 3.0, False),
    ):
        downward_start = replace(
            start_rce,
            input_revision=3,
            protected_soc_floor_percent=53.0,
        )
        downward_start_candidate = RUNTIME.build_rce_candidate(
            downward_start,
            now=NOW,
        )
        downward_execution = replace(
            rce_execution,
            machine_type_code=machine_type_code,
            inverter_count=inverter_count,
            direct_259_execution_ready=direct_259_ready,
            force_discharge_soc_percent=53.0,
            maximum_discharge_power_percent=100.0,
        )
        downward_context = RUNTIME.build_execution_context(
            downward_execution,
            now=NOW,
        )
        sensor._controller = SimpleNamespace(
            record=SimpleNamespace(
                state=SENSOR.ActiveState.EXECUTING,
                owner=SENSOR.ExecutionOwner.RCE,
                transaction=SimpleNamespace(
                    deadline=rce_deadline,
                    intent=SimpleNamespace(
                        action=SENSOR.ExecutionAction.RCE_EXPORT,
                        command=SimpleNamespace(
                            ems_block=SimpleNamespace(
                                force_discharge_soc_percent_4305=53.0,
                                maximum_discharge_power_percent_4306=100.0,
                            )
                        ),
                    ),
                ),
            )
        )
        retained_rce, _tariff, _rcm, _context = (
            sensor._apply_executor_commitment(
                replace(
                    downward_start,
                    input_revision=4,
                    protected_soc_floor_percent=52.0,
                ),
                tariff,
                rcm,
                downward_context,
                downward_execution,
                now=NOW,
            )
        )
        retained_candidate = RUNTIME.build_rce_candidate(
            retained_rce,
            now=NOW,
        )
        check(
            retained_rce.protected_soc_floor_percent == 52.0
            and retained_rce.latched_minimum_soc_percent == 53.0
            and retained_rce.active_4305_readback_percent == 53.0
            and retained_candidate.continuation_eligible
            and not retained_candidate.local_hard_stop
            and retained_candidate.desired_actuator_fingerprint
            == downward_start_candidate.desired_actuator_fingerprint,
            "lower RCE floor changed the active command identity for topology "
            f"{machine_type_code}/{inverter_count}",
        )
    sensor._controller = original_rce_controller

    split_event_rce, _tariff, _rcm, _context = (
        sensor._apply_executor_commitment(
            replace(
                recalculated_rce,
                control_data_ready=False,
                reserve_ready=None,
            ),
            tariff,
            rcm,
            rce_context,
            rce_execution,
            now=NOW,
        )
    )
    check(
        split_event_rce.latched_minimum_soc_percent == 49.0
        and split_event_rce.effective_discharge_power_percent == 55.0
        and split_event_rce.active_4305_readback_percent == 48.0
        and split_event_rce.active_4306_readback_percent == 100.0,
        "split-event RCE readiness hid the latest desired target or old FC03 proof",
    )

    rcm_context = RUNTIME.build_execution_context(execution, now=NOW)
    recalculated_rcm = replace(
        rcm,
        recommended_charge_limit_percent=25.0,
        recommended_export_limit_percent=80.0,
    )
    sensor._controller = SimpleNamespace(
        record=SimpleNamespace(
            state=SENSOR.ActiveState.EXECUTING,
            owner=SENSOR.ExecutionOwner.RCM,
            transaction=SimpleNamespace(
                deadline=NOW + timedelta(minutes=5),
                intent=SimpleNamespace(
                    action=SENSOR.ExecutionAction.RCM_ABSORB_AND_LIMIT,
                    command=SimpleNamespace(
                        ems_block=None,
                        battery_max_charge_power_percent_306=60.0,
                        export_limit_percent_259=35.0,
                    ),
                ),
            ),
        )
    )
    _rce, _tariff, committed_rcm, _context = sensor._apply_executor_commitment(
        rce,
        tariff,
        recalculated_rcm,
        rcm_context,
        execution,
        now=NOW,
    )
    check(
        committed_rcm.absorb_active is True
        and committed_rcm.export_active is True
        and committed_rcm.recommended_charge_limit_percent == 25.0
        and committed_rcm.recommended_export_limit_percent == 35.0,
        "EXECUTING RCEm recompute hid the safer live 306 target or lost 259",
    )


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

    for key, value in (
        ("supervisor_mode", "Active"),
        ("allow_rce", "on"),
        ("rce_enabled", "on"),
        ("rce_active", "on"),
        ("rce_control_data_ready", "on"),
        ("rce_price_above_threshold", "on"),
        ("rce_reserve_ready", "on"),
        ("sale_block_active", "off"),
    ):
        entity_id = sensor._source_entity_ids[key]
        assert entity_id is not None
        hass.states.values[entity_id] = FakeState(value)
    deadline = NOW + timedelta(hours=1)
    current_plan = hass.states.values[plan_id]
    ready_plan_attributes = {
        **current_plan.attributes,
        "current_slot_planned": True,
        "current_slot_start_eligible": True,
        "current_slot_continue_eligible": True,
        "current_run_end": deadline.isoformat(),
    }
    hass.states.values[plan_id] = FakeState(
        current_plan.state,
        ready_plan_attributes,
        current_plan.last_reported,
    )
    sensor._recompute()
    check(
        sensor._current_dispatch_frame() is not None,
        "Settled Active frame was unavailable to the predispatch resampler",
    )
    real_controller = sensor._controller
    sensor._controller = SimpleNamespace(
        record=SimpleNamespace(
            state=SENSOR.ActiveState.RETARGETING,
            owner=SENSOR.ExecutionOwner.RCE,
            transaction=SimpleNamespace(
                command_sent_at=None,
                deadline=deadline,
                intent=SimpleNamespace(
                    policy=SENSOR.ExecutionOwner.RCE,
                    action=SENSOR.ExecutionAction.RCE_EXPORT,
                ),
            ),
        )
    )
    current_plan = hass.states.values[plan_id]
    hass.fire_state(
        plan_id,
        FakeState(
            current_plan.state,
            current_plan.attributes,
            NOW + timedelta(microseconds=1),
        ),
    )
    check(
        sensor._planner_pending_keys == {"rce_plan"}
        and sensor._current_dispatch_frame() is None
        and sensor._retarget_replan_pending(),
        "raw RCE target change exposed stale B during its debounce window",
    )
    current_plan = hass.states.values[plan_id]
    hass.states.values[plan_id] = FakeState(
        current_plan.state,
        {
            **current_plan.attributes,
            "current_run_end": (deadline + timedelta(minutes=1)).isoformat(),
        },
        current_plan.last_reported,
    )
    check(
        sensor._retarget_replan_pending(),
        "covering raw run deadline lost predecessor-resume authority",
    )
    hass.states.values[plan_id] = FakeState(
        current_plan.state,
        {
            **current_plan.attributes,
            "current_run_end": (deadline - timedelta(microseconds=1)).isoformat(),
        },
        current_plan.last_reported,
    )
    check(
        not sensor._retarget_replan_pending(),
        "shorter raw run deadline gained predecessor-resume authority",
    )
    hass.states.values[plan_id] = FakeState(
        current_plan.state,
        {**current_plan.attributes, "current_run_end": "invalid"},
        current_plan.last_reported,
    )
    check(
        not sensor._retarget_replan_pending(),
        "invalid raw run deadline gained predecessor-resume authority",
    )
    hass.states.values[plan_id] = current_plan
    hass.fire_state(profile_id, FakeState("High Reserve — Winter"))
    check(
        sensor._planner_pending_keys == {"rce_plan", "supervisor_profile"}
        and not sensor._retarget_replan_pending(),
        "non-target planner churn gained the narrow predecessor-resume authority",
    )
    sensor._controller = real_controller
    hass.active_delays()[0].run()
    before_writes = sensor.write_count
    sensor._recompute()
    check(sensor.write_count == before_writes, "Identical summary was republished")
    hass.fire_state("sensor.hoymiles_hit_ems_supervisor", FakeState("shadow_selected"))
    check(sensor.write_count == before_writes, "Output self-triggered")


def test_rce_retarget_callback_lifecycle() -> None:
    """Drive pre-ACK A -> latest C -> FC03 -> restore through HA callbacks."""

    async def scenario() -> None:
        hass, entry, _runtime, sensor = environment()
        deadline = NOW + timedelta(seconds=12)
        writes: list[Any] = []

        def entity_id(key: str) -> str:
            return _source_entity_id(SENSOR._SOURCE_BY_KEY[key], entry.entry_id)

        def store(
            key: str,
            value: object,
            *,
            attributes: dict[str, Any] | None = None,
            reported: datetime = NOW,
        ) -> None:
            hass.states.values[entity_id(key)] = FakeState(
                str(value),
                attributes,
                reported,
            )

        async def dispatch(write: Any) -> None:
            writes.append(write)

        async def drain_adapter() -> None:
            for _attempt in range(32):
                await asyncio.sleep(0)
                if (
                    sensor._controller_task is None
                    and sensor._pending_active_frame is None
                ):
                    return
            raise AssertionError("Sensor adapter did not drain its latest frame")

        def report_ems_block(
            *,
            at: datetime,
            generation: int,
            mode: int,
            self_use_soc: float,
            backup_soc: float,
            force_charge_soc: float,
            maximum_charge_power: float,
            force_discharge_soc: float,
            maximum_discharge_power: float,
        ) -> None:
            values = (
                ("ems_mode_readback", mode),
                ("self_use_soc_readback", self_use_soc),
                ("backup_soc_readback", backup_soc),
                ("charge_soc_readback", force_charge_soc),
                ("charge_power_ems_readback", maximum_charge_power),
                ("discharge_soc_readback", force_discharge_soc),
                ("discharge_power_readback", maximum_discharge_power),
                ("ems_generation", generation),
            )
            for key, value in values:
                hass.fire_report(entity_id(key), FakeState(str(value), reported=at))

        plan_a = {
            **_plan_attributes("rce_plan"),
            "input_revision": 1,
            "current_slot_planned": True,
            "current_slot_start_eligible": True,
            "current_slot_continue_eligible": True,
            "current_slot_end": (NOW + timedelta(minutes=10)).isoformat(),
            "current_run_end": deadline.isoformat(),
            "current_slot_execution_discharge_power_kw": 2.50,
            "current_slot_planned_export_kwh": 0.4,
            "current_required_minimum_soc_percent": 41.0,
        }
        for key, value in (
            ("supervisor_mode", "Active"),
            ("allow_rce", "on"),
            ("rce_enabled", "on"),
            ("rce_active", "off"),
            ("rce_control_data_ready", "on"),
            ("rce_price_above_threshold", "on"),
            ("rce_reserve_ready", "on"),
            ("sale_block_active", "off"),
            ("ems_execution_ready", "on"),
            ("rce_effective_discharge_power", "25.0"),
            ("rce_latched_minimum_soc", "41"),
            ("battery_soc", "90"),
            # 50 V × 60 A × 0.95 = 2.85 kW, so the 2.50 kW command
            # remains inside the live BMS discharge limit during this test.
            ("bms_max_discharge_current", "60"),
            ("ems_mode_readback", "0"),
            ("ems_generation", "1"),
            ("self_use_soc_readback", "20"),
            ("backup_soc_readback", "80"),
            ("charge_soc_readback", "80"),
            ("charge_power_ems_readback", "40"),
            ("discharge_soc_readback", "20"),
            ("discharge_power_readback", "50"),
        ):
            store(key, value)
        store("rce_plan", "ready", attributes=plan_a)
        store(
            "rce_latched_slot_end",
            "ignored",
            attributes={"timestamp": deadline.timestamp()},
        )

        # Keep the real adapter/controller but replace only the external I/O
        # boundary and its wall clock with deterministic test doubles.
        await sensor.add_to_platform_finish()
        controller = sensor._controller
        assert controller is not None
        controller._clock = lambda: CLOCK["now"]
        controller._dispatch = dispatch
        controller._control_lease_valid_until = None
        await drain_adapter()
        first = controller.record.transaction
        assert first is not None and first.command_snapshot is not None
        first_block = writes[0].ems_block
        check(
            controller.record.state is SENSOR.ActiveState.WAITING_READBACK
            and len(writes) == 1
            and first_block is not None
            and first_block.mode.value == 5
            and first_block.force_discharge_soc_percent_4305 == 41.0
            and first_block.maximum_discharge_power_percent_4306 == 25.0,
            "Real adapter did not dispatch exact initial RCE target A",
        )

        baseline = first.command_snapshot
        original_transaction_id = first.transaction_id
        check(
            baseline.ems_generation == 1
            and baseline.ems_block.mode.value == 0
            and baseline.ems_block.force_discharge_soc_percent_4305 == 20.0
            and baseline.ems_block.maximum_discharge_power_percent_4306 == 50.0,
            "Initial transaction did not preserve the exact neutral baseline N",
        )

        # Both successors arrive before A is physically acknowledged.  B is a
        # real 30/50 target, but C supersedes it inside the plan debounce and
        # is the only successor that may reach transport after FC03 proves A.
        plan_b = {
            **plan_a,
            "input_revision": 2,
            "current_required_minimum_soc_percent": 30.0,
        }
        plan_c = {
            **plan_a,
            "input_revision": 3,
            "current_required_minimum_soc_percent": 30.0,
        }
        plan_id = entity_id("rce_plan")
        CLOCK["now"] = NOW + timedelta(milliseconds=200)
        hass.fire_state(
            entity_id("rce_latched_minimum_soc"),
            FakeState("30", reported=CLOCK["now"]),
        )
        hass.fire_state(
            entity_id("rce_effective_discharge_power"),
            FakeState("50", reported=CLOCK["now"]),
        )
        hass.fire_state(plan_id, FakeState("ready", plan_b, CLOCK["now"]))
        timer_b = hass.active_delays()[0]
        check(
            hass.states.values[plan_id].attributes[
                "current_required_minimum_soc_percent"
            ]
            == 30.0
            and hass.states.values[
                entity_id("rce_effective_discharge_power")
            ].state
            == "50",
            "Pre-ACK target B was not the intended 30/50 command",
        )
        CLOCK["now"] = NOW + timedelta(milliseconds=210)
        hass.fire_state(
            entity_id("rce_effective_discharge_power"),
            FakeState("25.0", reported=CLOCK["now"]),
        )
        hass.fire_state(plan_id, FakeState("ready", plan_c, CLOCK["now"]))
        timer_c = hass.active_delays()[0]
        check(
            timer_b.cancelled
            and timer_c is not timer_b
            and hass.active_delays() == [timer_c]
            and controller.record.state is SENSOR.ActiveState.WAITING_READBACK
            and len(writes) == 1,
            "Pre-ACK plan callbacks did not retain A and collapse B into latest C",
        )

        CLOCK["now"] = NOW + timedelta(seconds=1)
        report_ems_block(
            at=CLOCK["now"],
            generation=2,
            mode=5,
            self_use_soc=20.0,
            backup_soc=80.0,
            force_charge_soc=80.0,
            maximum_charge_power=40.0,
            force_discharge_soc=41.0,
            maximum_discharge_power=25.0,
        )
        hass.fire_state(entity_id("rce_active"), FakeState("on", reported=CLOCK["now"]))
        hass.fire_state(
            entity_id("grid_power"),
            FakeState("1200", reported=CLOCK["now"]),
        )
        hass.fire_state(
            entity_id("battery_power"),
            FakeState("900", reported=CLOCK["now"]),
        )
        pending = hass.active_delays()
        check(
            len(pending) == 2
            and sorted(handle.when for handle in pending) == [
                SENSOR._READBACK_COHORT_DELAY_SECONDS,
                SENSOR._POWER_COHORT_COLLECTION_SECONDS,
            ],
            "Initial FC03 did not replace the plan timer with one cohort callback",
        )
        # The independent incomplete power collector only expires its partial
        # set; the 100 ms FC03 callback alone drives this readback scenario.
        for handle in sorted(pending, key=lambda handle: handle.when):
            handle.run()
        await drain_adapter()
        retarget = controller.record.transaction
        assert retarget is not None
        check(
            timer_c.cancelled
            and controller.record.state is SENSOR.ActiveState.RETARGETING
            and controller.record.owner is SENSOR.ExecutionOwner.RCE
            and len(writes) == 2
            and [write.ems_block.mode.value for write in writes] == [5, 5]
            and all(
                not (
                    write.ems_block.force_discharge_soc_percent_4305 == 30.0
                    and write.ems_block.maximum_discharge_power_percent_4306
                    == 50.0
                )
                for write in writes
            )
            and writes[-1].ems_block.force_discharge_soc_percent_4305 == 30.0
            and writes[-1].ems_block.maximum_discharge_power_percent_4306 == 25.0,
            "Generation-2 ACK of A did not autonomously dispatch only latest C",
        )
        check(
            retarget.transaction_id == original_transaction_id
            and retarget.deadline == deadline
            and retarget.command_snapshot == baseline
            and writes[-1].snapshot_generation == 2,
            "Retarget replaced the transaction lease, baseline, or FC03 generation",
        )

        # Even an exact-value replay of generation 2 after C was sent is old
        # evidence.  It must not acknowledge C or release RCE ownership.
        CLOCK["now"] = NOW + timedelta(seconds=1, milliseconds=500)
        report_ems_block(
            at=CLOCK["now"],
            generation=2,
            mode=5,
            self_use_soc=20.0,
            backup_soc=80.0,
            force_charge_soc=80.0,
            maximum_charge_power=40.0,
            force_discharge_soc=30.0,
            maximum_discharge_power=25.0,
        )
        hass.fire_state(
            entity_id("rce_latched_minimum_soc"),
            FakeState("30", reported=CLOCK["now"]),
        )
        hass.fire_state(
            entity_id("grid_power"),
            FakeState("1250", reported=CLOCK["now"]),
        )
        hass.fire_state(
            entity_id("battery_power"),
            FakeState("950", reported=CLOCK["now"]),
        )
        pending = hass.active_delays()
        check(
            len(pending) == 2
            and sorted(handle.when for handle in pending) == [
                SENSOR._READBACK_COHORT_DELAY_SECONDS,
                SENSOR._POWER_COHORT_COLLECTION_SECONDS,
            ],
            "Replayed generation-2 reports did not coalesce into one callback",
        )
        # The independent incomplete power collector only expires its partial
        # set; the 100 ms FC03 callback alone drives this readback scenario.
        for handle in sorted(pending, key=lambda handle: handle.when):
            handle.run()
        await drain_adapter()
        stale = controller.record.transaction
        assert stale is not None
        check(
            controller.record.state is SENSOR.ActiveState.RETARGETING
            and controller.record.owner is SENSOR.ExecutionOwner.RCE
            and stale.readback_result.value == "pending"
            and stale.physical_verification is None
            and len(writes) == 2,
            "Replayed generation 2 acknowledged C or lost RCE ownership",
        )

        # D arrives while C still waits for its own newer FC03.  The adapter
        # must retain only D, but it may not write D until generation 3 proves
        # that C really reached the inverter.
        plan_d = {
            **plan_c,
            "input_revision": 4,
            "current_required_minimum_soc_percent": 35.0,
        }
        CLOCK["now"] = NOW + timedelta(seconds=2)
        hass.fire_state(
            entity_id("rce_latched_minimum_soc"),
            FakeState("35", reported=CLOCK["now"]),
        )
        hass.fire_state(
            entity_id("rce_effective_discharge_power"),
            FakeState("20", reported=CLOCK["now"]),
        )
        hass.fire_state(plan_id, FakeState("ready", plan_d, CLOCK["now"]))
        timer_d = hass.active_delays()[0]
        check(
            controller.record.state is SENSOR.ActiveState.RETARGETING
            and controller.record.owner is SENSOR.ExecutionOwner.RCE
            and len(writes) == 2,
            "D was written before C received its newer physical FC03",
        )

        CLOCK["now"] = NOW + timedelta(seconds=3)
        report_ems_block(
            at=CLOCK["now"],
            generation=3,
            mode=5,
            self_use_soc=20.0,
            backup_soc=80.0,
            force_charge_soc=80.0,
            maximum_charge_power=40.0,
            force_discharge_soc=30.0,
            maximum_discharge_power=25.0,
        )
        hass.fire_state(
            entity_id("grid_power"),
            FakeState("1250", reported=CLOCK["now"]),
        )
        hass.fire_state(
            entity_id("battery_power"),
            FakeState("950", reported=CLOCK["now"]),
        )
        pending = hass.active_delays()
        check(
            len(pending) == 2
            and sorted(handle.when for handle in pending) == [
                SENSOR._READBACK_COHORT_DELAY_SECONDS,
                SENSOR._POWER_COHORT_COLLECTION_SECONDS,
            ],
            "Fresh retarget FC03 reports did not coalesce into one callback",
        )
        # The independent incomplete power collector only expires its partial
        # set; the 100 ms FC03 callback alone drives this readback scenario.
        for handle in sorted(pending, key=lambda handle: handle.when):
            handle.run()
        await drain_adapter()
        latest = controller.record.transaction
        assert latest is not None
        check(
            timer_d.cancelled
            and controller.record.state is SENSOR.ActiveState.RETARGETING
            and controller.record.owner is SENSOR.ExecutionOwner.RCE
            and len(writes) == 3
            and [write.ems_block.mode.value for write in writes] == [5, 5, 5]
            and writes[-1].ems_block.force_discharge_soc_percent_4305 == 35.0
            and writes[-1].ems_block.maximum_discharge_power_percent_4306 == 20.0
            and writes[-1].snapshot_generation == 3
            and latest.transaction_id == original_transaction_id
            and latest.command_snapshot == baseline
            and latest.deadline == deadline,
            "Fresh generation-3 ACK of C did not autonomously dispatch only D",
        )

        CLOCK["now"] = NOW + timedelta(seconds=4)
        report_ems_block(
            at=CLOCK["now"],
            generation=4,
            mode=5,
            self_use_soc=20.0,
            backup_soc=80.0,
            force_charge_soc=80.0,
            maximum_charge_power=40.0,
            force_discharge_soc=35.0,
            maximum_discharge_power=20.0,
        )
        hass.fire_state(
            entity_id("grid_power"),
            FakeState("1000", reported=CLOCK["now"]),
        )
        hass.fire_state(
            entity_id("battery_power"),
            FakeState("700", reported=CLOCK["now"]),
        )
        pending = hass.active_delays()
        check(
            len(pending) == 2
            and sorted(handle.when for handle in pending) == [
                SENSOR._READBACK_COHORT_DELAY_SECONDS,
                SENSOR._POWER_COHORT_COLLECTION_SECONDS,
            ],
            "Fresh D FC03 reports did not coalesce into one callback",
        )
        # The independent incomplete power collector only expires its partial
        # set; the 100 ms FC03 callback alone drives this readback scenario.
        for handle in sorted(pending, key=lambda handle: handle.when):
            handle.run()
        await drain_adapter()
        confirmed = controller.record.transaction
        assert confirmed is not None
        check(
            controller.record.state is SENSOR.ActiveState.EXECUTING
            and controller.record.owner is SENSOR.ExecutionOwner.RCE
            and len(writes) == 3
            and confirmed.transaction_id == original_transaction_id
            and confirmed.command_snapshot == baseline
            and confirmed.deadline == deadline
            and confirmed.readback_result.value == "confirmed"
            and confirmed.physical_verification is not None
            and confirmed.command_sent_at is not None
            and confirmed.physical_verification.observed_at
            > confirmed.command_sent_at,
            "D did not require and consume its separate generation-4 FC03",
        )

        check(
            controller.execution_watchdog == (original_transaction_id, deadline)
            and any(
                handle.when == deadline and not handle.cancelled
                for handle in hass.active_points()
            ),
            "Adapter did not retain an autonomous callback at the original deadline",
        )
        deadline_callback = next(
            handle
            for handle in hass.active_points()
            if handle.when == deadline and not handle.cancelled
        )
        CLOCK["now"] = deadline + timedelta(microseconds=1)
        deadline_callback.run()
        await drain_adapter()
        restoring = controller.record.transaction
        assert restoring is not None
        check(
            controller.record.state is SENSOR.ActiveState.RESTORING
            and controller.record.owner is SENSOR.ExecutionOwner.RCE
            and len(writes) == 4
            and [write.ems_block.mode.value for write in writes]
            == [5, 5, 5, 0]
            and writes[-1].ems_block == baseline.ems_block
            and writes[-1].snapshot_generation == 4
            and restoring.command_snapshot == baseline
            and restoring.deadline == deadline,
            "Original deadline did not autonomously restore exact baseline N",
        )

        CLOCK["now"] = deadline + timedelta(seconds=1)
        baseline_block = baseline.ems_block
        report_ems_block(
            at=CLOCK["now"],
            generation=5,
            mode=baseline_block.mode.value,
            self_use_soc=baseline_block.self_use_soc_percent_4301,
            backup_soc=baseline_block.backup_soc_percent_4302,
            force_charge_soc=baseline_block.force_charge_soc_percent_4303,
            maximum_charge_power=baseline_block.maximum_charge_power_percent_4304,
            force_discharge_soc=baseline_block.force_discharge_soc_percent_4305,
            maximum_discharge_power=baseline_block.maximum_discharge_power_percent_4306,
        )
        hass.fire_state(
            entity_id("rce_active"),
            FakeState("off", reported=CLOCK["now"]),
        )
        hass.fire_state(
            entity_id("grid_power"),
            FakeState("0", reported=CLOCK["now"]),
        )
        hass.fire_state(
            entity_id("battery_power"),
            FakeState("0", reported=CLOCK["now"]),
        )
        pending = hass.active_delays()
        check(
            len(pending) == 2
            and sorted(handle.when for handle in pending) == [
                SENSOR._READBACK_COHORT_DELAY_SECONDS,
                SENSOR._POWER_COHORT_COLLECTION_SECONDS,
            ],
            "Baseline FC03 reports did not coalesce into one adapter callback",
        )
        # The independent incomplete power collector only expires its partial
        # set; the 100 ms FC03 callback alone drives this readback scenario.
        for handle in sorted(pending, key=lambda handle: handle.when):
            handle.run()
        await drain_adapter()
        terminal = controller.record.last_transaction
        assert terminal is not None
        check(
            controller.record.state is SENSOR.ActiveState.IDLE
            and controller.record.owner is SENSOR.ExecutionOwner.NONE
            and controller.record.transaction is None
            and terminal.transaction_id == original_transaction_id
            and terminal.command_snapshot == baseline
            and terminal.rollback_status is SENSOR.RollbackStatus.CONFIRMED
            and terminal.rollback_result.value == "confirmed",
            "Fresh generation-5 baseline FC03 did not finish the autonomous rollback",
        )

    asyncio.run(scenario())


def test_rce_settling_watchdog_callback() -> None:
    """Acknowledged RCE settling is bounded by 180 s and live EMS/GCF callbacks."""

    async def scenario(stale_family: str) -> None:
        hass, entry, _runtime, sensor = environment()
        deadline = NOW + timedelta(minutes=5)
        writes: list[Any] = []

        def entity_id(key: str) -> str:
            return _source_entity_id(SENSOR._SOURCE_BY_KEY[key], entry.entry_id)

        def store(
            key: str,
            value: object,
            *,
            attributes: dict[str, Any] | None = None,
            reported: datetime = NOW,
        ) -> None:
            hass.states.values[entity_id(key)] = FakeState(
                str(value),
                attributes,
                reported,
            )

        async def dispatch(write: Any) -> None:
            writes.append(write)

        async def drain_adapter() -> None:
            for _attempt in range(32):
                await asyncio.sleep(0)
                if (
                    sensor._controller_task is None
                    and sensor._pending_active_frame is None
                ):
                    return
            raise AssertionError("Sensor adapter did not drain its latest frame")

        plan = {
            **_plan_attributes("rce_plan"),
            "input_revision": 1,
            "current_slot_planned": True,
            "current_slot_start_eligible": True,
            "current_slot_continue_eligible": True,
            "current_slot_end": deadline.isoformat(),
            "current_run_end": deadline.isoformat(),
            "current_slot_execution_discharge_power_kw": 1.0,
            "current_slot_planned_export_kwh": 0.4,
            "current_required_minimum_soc_percent": 55.0,
        }
        for key, value in (
            ("supervisor_mode", "Active"),
            ("allow_rce", "on"),
            ("rce_enabled", "on"),
            ("rce_active", "off"),
            ("rce_control_data_ready", "on"),
            ("rce_price_above_threshold", "on"),
            ("rce_reserve_ready", "on"),
            ("sale_block_active", "off"),
            ("ems_execution_ready", "on"),
            ("rce_effective_discharge_power", "10"),
            ("rce_latched_minimum_soc", "55"),
            ("battery_soc", "90"),
            ("bms_voltage", "50"),
            ("bms_max_discharge_current", "200"),
            ("ems_mode_readback", "0"),
            ("ems_generation", "1"),
            ("self_use_soc_readback", "20"),
            ("backup_soc_readback", "80"),
            ("charge_soc_readback", "80"),
            ("charge_power_ems_readback", "40"),
            ("discharge_soc_readback", "20"),
            ("discharge_power_readback", "50"),
        ):
            store(key, value)
        store("rce_plan", "ready", attributes=plan)
        store(
            "rce_latched_slot_end",
            "ignored",
            attributes={"timestamp": deadline.timestamp()},
        )

        await sensor.add_to_platform_finish()
        controller = sensor._controller
        assert controller is not None
        controller._clock = lambda: CLOCK["now"]
        controller._dispatch = dispatch
        controller._control_lease_valid_until = None
        await drain_adapter()
        transaction = controller.record.transaction
        assert transaction is not None and transaction.command_sent_at is not None
        check(
            controller.record.state is SENSOR.ActiveState.WAITING_READBACK
            and controller.record.owner is SENSOR.ExecutionOwner.RCE
            and len(writes) == 1
            and writes[0].ems_block.mode.value == 5,
            "Real adapter did not start the RCE settling fixture",
        )

        battery_at = NOW + timedelta(seconds=1)
        ack_at = NOW + timedelta(seconds=5)
        CLOCK["now"] = ack_at
        for key in (
            "battery_soc",
            "bms_voltage",
            "bms_max_discharge_current",
        ):
            current = hass.states.values[entity_id(key)]
            store(key, current.state, attributes=current.attributes, reported=ack_at)
        for key, value in (
            ("ems_mode_readback", 5),
            ("self_use_soc_readback", 20),
            ("backup_soc_readback", 80),
            ("charge_soc_readback", 80),
            ("charge_power_ems_readback", 40),
            ("discharge_soc_readback", 55),
            ("discharge_power_readback", 10),
            ("ems_generation", 2),
            ("gcf_enable_readback", 0),
            ("gcf_export_limit_readback", 50),
            ("gcf_generation", 2),
        ):
            hass.fire_report(
                entity_id(key),
                FakeState(str(value), reported=ack_at),
            )
        hass.fire_state(
            entity_id("rce_active"),
            FakeState("on", reported=ack_at),
        )
        hass.fire_state(
            entity_id("battery_power"),
            FakeState("-7200", reported=battery_at),
        )
        cohort_callbacks = hass.active_delays()
        check(
            len(cohort_callbacks) == 2
            and sorted(handle.when for handle in cohort_callbacks) == [
                SENSOR._READBACK_COHORT_DELAY_SECONDS,
                SENSOR._POWER_COHORT_COLLECTION_SECONDS,
            ],
            "Post-ACK reports did not coalesce into one sensor callback",
        )
        for handle in sorted(cohort_callbacks, key=lambda handle: handle.when):
            handle.run()
        await drain_adapter()
        pending = controller.record.transaction
        assert pending is not None and pending.physical_verification is not None
        controller_module = sys.modules[
            "custom_components.hoymiles_hit_modbus.supervisor_active_controller"
        ]
        battery_boundary = battery_at + timedelta(
            seconds=controller_module.MAX_READBACK_AGE_SECONDS
        )
        command_boundary = transaction.command_sent_at + timedelta(seconds=180)
        check(
            controller.record.state is SENSOR.ActiveState.WAITING_READBACK
            and controller.record.reason is SENSOR.ExecutionReason.PHYSICAL_PENDING
            and pending.readback_result.value == "confirmed"
            and pending.physical_verification.status.value == "pending"
            and pending.command_sent_at == transaction.command_sent_at
            and len(writes) == 1,
            "Matching FC03 plus old charging flow did not enter bounded settling",
        )
        check(
            controller.execution_watchdog
            == (transaction.transaction_id, ack_at + timedelta(seconds=15))
            and not any(
                handle.when == battery_boundary + timedelta(microseconds=1)
                for handle in hass.active_points()
            ),
            "Initial settling must retain EMS expiry without a BAT expiry timer",
        )

        async def refresh_readback(at: datetime, *, include_gcf: bool) -> None:
            """Deliver actual cohort reports; leave the BAT timestamp untouched."""
            CLOCK["now"] = at
            keys = (
                "ems_mode_readback", "self_use_soc_readback",
                "backup_soc_readback", "charge_soc_readback",
                "charge_power_ems_readback", "discharge_soc_readback",
                "discharge_power_readback", "ems_generation", "battery_soc",
                "machine_type", "inverter_count", "topology_generation",
            )
            if include_gcf:
                keys += (
                    "gcf_enable_readback", "gcf_export_limit_readback",
                    "gcf_generation",
                )
            for key in keys:
                current = hass.states.values[entity_id(key)]
                value = (
                    int(current.state) + 1
                    if key in {"ems_generation", "gcf_generation", "topology_generation"}
                    else current.state
                )
                hass.fire_report(
                    entity_id(key),
                    FakeState(str(value), current.attributes, reported=at),
                )
            callbacks = hass.active_delays()
            assert len(callbacks) == 1
            callbacks[0].run()
            await drain_adapter()

        async def run_due_points(at: datetime) -> None:
            """Run only registered callbacks; never call reconcile directly."""
            CLOCK["now"] = at
            for _attempt in range(32):
                due = [handle for handle in hass.active_points() if handle.when <= at]
                if not due:
                    return
                min(due, key=lambda handle: handle.when).run()
                await drain_adapter()
            raise AssertionError("Sensor point callbacks did not drain")

        if stale_family == "battery":
            for second in (*range(10, 180, 10), 175):
                at = NOW + timedelta(seconds=second)
                await refresh_readback(at, include_gcf=True)
                await run_due_points(at)
                current = controller.record.transaction
                assert current is not None and current.physical_verification is not None
                check(
                    controller.record.state is SENSOR.ActiveState.WAITING_READBACK
                    and controller.record.reason is SENSOR.ExecutionReason.PHYSICAL_PENDING
                    and controller.record.owner is SENSOR.ExecutionOwner.RCE
                    and current.transaction_id == transaction.transaction_id
                    and current.command_sent_at == transaction.command_sent_at
                    and current.command_snapshot == transaction.command_snapshot
                    and current.deadline == transaction.deadline
                    and current.readback_result.value == "confirmed"
                    and current.physical_verification.status.value == "pending"
                    and current.physical_verification.observed_at == battery_at
                    and hass.states.values[entity_id("battery_power")].last_reported == battery_at
                    and len(writes) == 1,
                    "Fresh EMS/GCF renewed the command timeout or stale BAT ended initial settling",
                )
            boundary = command_boundary
            expected_state = SENSOR.ActiveState.RESTORING
            expected_modes = [5, 0]
        elif stale_family == "ems":
            await refresh_readback(NOW + timedelta(seconds=10), include_gcf=True)
            boundary = NOW + timedelta(seconds=25)
            expected_state = SENSOR.ActiveState.STOPPING
            expected_modes = [5]
        else:
            assert stale_family == "gcf"
            for second in (10, 20, 30, 34):
                await refresh_readback(NOW + timedelta(seconds=second), include_gcf=False)
            boundary = ack_at + timedelta(seconds=30)
            expected_state = SENSOR.ActiveState.RESTORING
            expected_modes = [5, 0]

        watchdog = next(
            (
                handle for handle in hass.active_points()
                if handle.when == boundary + timedelta(microseconds=1)
            ),
            None,
        )
        check(
            controller.execution_watchdog == (transaction.transaction_id, boundary)
            and watchdog is not None
            and (stale_family == "battery" or boundary < command_boundary),
            f"Sensor did not schedule the fixed {stale_family} boundary",
        )
        await run_due_points(boundary - timedelta(microseconds=1))
        check(
            controller.record.state is SENSOR.ActiveState.WAITING_READBACK
            and controller.record.reason is SENSOR.ExecutionReason.PHYSICAL_PENDING
            and len(writes) == 1,
            f"Initial settling ended before the {stale_family} boundary",
        )
        await run_due_points(boundary + timedelta(microseconds=1))
        stopped = controller.record.transaction
        assert stopped is not None
        check(
            controller.record.state is expected_state
            and controller.record.owner is SENSOR.ExecutionOwner.RCE
            and stopped.transaction_id == transaction.transaction_id
            and stopped.command_snapshot == transaction.command_snapshot
            and (
                len(writes) == 1
                or writes[-1].ems_block == transaction.command_snapshot.ems_block
            )
            and controller.recorder_attributes()[
                "supervisor_execution_authorized"
            ]
            is False
            and [write.ems_block.mode.value for write in writes] == expected_modes,
            f"Silent {stale_family} expiry did not invoke protection with owner retained",
        )

    for stale_family in ("battery", "ems", "gcf"):
        asyncio.run(scenario(stale_family))


def test_readback_cohort_eventing() -> None:
    hass, entry, _runtime, sensor = environment()
    add(sensor)
    expected_keys = {
        "ems_mode_readback",
        "ems_generation",
        "self_use_soc_readback",
        "backup_soc_readback",
        "discharge_power_readback",
        "discharge_soc_readback",
        "charge_power_ems_readback",
        "charge_soc_readback",
        "gcf_enable_readback",
        "gcf_export_limit_readback",
        "gcf_generation",
        "charge_power_readback",
        "battery_charge_generation",
        "machine_type",
        "inverter_count",
        "topology_generation",
        "grid_power",
        "battery_power",
        "pv_power",
        "load_power",
    }
    expected_ids = {
        _source_entity_id(SENSOR._SOURCE_BY_KEY[key], entry.entry_id)
        for key in expected_keys
    }
    report_registration = _active_report_registration(hass)
    check(
        set(report_registration["ids"]) == expected_ids
        and len(report_registration["ids"]) == 20,
        "Cohort report listener is not the exact readback/power source union",
    )
    check(
        set(report_registration["ids"])
        < set(_active_state_registration(hass)["ids"]),
        "Cohort reports accidentally subscribed all Supervisor sources",
    )

    representative_keys = (
        "ems_mode_readback",
        "gcf_enable_readback",
        "charge_power_readback",
        "machine_type",
    )
    for key in representative_keys:
        entity_id = _source_entity_id(SENSOR._SOURCE_BY_KEY[key], entry.entry_id)
        current = hass.states.values[entity_id]
        calls = CLOCK["calls"]
        hass.fire_report(
            entity_id,
            FakeState(current.state, current.attributes, CLOCK["now"]),
        )
        pending = hass.active_delays()
        check(
            len(pending) == 1 and abs(pending[0].when - 0.100) < 1e-12,
            f"{key} report did not schedule one bounded trailing edge",
        )
        pending[0].run()
        check(
            CLOCK["calls"] == calls + 1,
            f"{key} report did not finish with one recompute",
        )

    battery_soc_id = _source_entity_id(
        SENSOR._SOURCE_BY_KEY["battery_soc"], entry.entry_id
    )
    calls = CLOCK["calls"]
    battery_soc = hass.states.values[battery_soc_id]
    hass.fire_report(
        battery_soc_id,
        FakeState(battery_soc.state, battery_soc.attributes, CLOCK["now"]),
    )
    check(
        CLOCK["calls"] == calls and not hass.active_delays(),
        "Non-cohort unchanged report entered the recompute surface",
    )

    CLOCK["now"] = NOW + timedelta(seconds=10)
    calls = CLOCK["calls"]
    writes = sensor.visible_writes
    history_start = len(sensor.visible_write_history)
    self_use_id = _source_entity_id(
        SENSOR._SOURCE_BY_KEY["self_use_soc_readback"], entry.entry_id
    )
    old_temporal = hass.active_points()[0]
    hass.fire_state(self_use_id, FakeState("25", reported=CLOCK["now"]))
    pending = hass.active_delays()
    check(
        len(pending) == 1
        and abs(pending[0].when - 0.100) < 1e-12
        and old_temporal.cancelled
        and not hass.active_points()
        and CLOCK["calls"] == calls
        and sensor.visible_writes == writes,
        "First changed cohort member retained a temporal partial-publication path",
    )
    old_temporal.run()
    old_temporal.callback(CLOCK["now"])
    check(
        CLOCK["calls"] == calls
        and sensor.visible_writes == writes
        and hass.active_delays() == pending,
        "Cancelled temporal callback escaped the pending cohort generation guard",
    )
    partial = sensor._build_snapshots(sensor._read_source_states(), CLOCK["now"])[5]
    check(
        partial.full_block_generation_at is None,
        "Regression fixture did not create a genuinely partial EMS cohort",
    )

    profile_id = SENSOR._SOURCE_BY_KEY["supervisor_profile"].locator
    cohort_pending = pending[0]
    hass.fire_state(profile_id, FakeState("Maximum Profit"))
    check(
        hass.active_delays() == [cohort_pending]
        and sensor._planner_cancel is None
        and CLOCK["calls"] == calls
        and sensor.visible_writes == writes,
        "Planner event escaped the pending cohort trailing edge",
    )

    sensor._async_loaded_entry_count_changed(1)
    check(
        hass.active_delays() == [cohort_pending]
        and CLOCK["calls"] == calls
        and sensor.visible_writes == writes,
        "Single-entry notification escaped the pending cohort trailing edge",
    )

    hass.config.time_zone = "Europe/Warsaw"
    hass.bus.fire("core_config_updated", {"time_zone": "Europe/Warsaw"})
    check(
        hass.active_delays() == [cohort_pending]
        and CLOCK["calls"] == calls
        and sensor.visible_writes == writes,
        "Core-config update escaped the pending cohort trailing edge",
    )

    plan_spec = SENSOR._SOURCE_BY_KEY["rce_plan"]
    plan_unique_id = f"{entry.entry_id}_{plan_spec.locator}"
    old_plan_id, new_plan_id = hass.registry.rename(
        plan_unique_id,
        "sensor.renamed_cohort_rce_plan",
    )
    hass.states.values[new_plan_id] = hass.states.values.pop(old_plan_id)
    old_state_registration = _active_state_registration(hass)
    old_report_registration = _active_report_registration(hass)
    hass.bus.fire(
        "entity_registry_updated",
        {"action": "update", "entity_id": new_plan_id},
    )
    latest_state_registration = _active_state_registration(hass)
    latest_report_registration = _active_report_registration(hass)
    check(
        hass.active_delays() == [cohort_pending]
        and CLOCK["calls"] == calls
        and sensor.visible_writes == writes
        and not old_state_registration["active"]
        and not old_report_registration["active"]
        and new_plan_id in latest_state_registration["ids"]
        and old_plan_id not in latest_state_registration["ids"],
        "Registry rebind escaped or lost the pending cohort trailing edge",
    )

    final_key = "discharge_power_readback"
    for key in (
        "ems_mode_readback",
        "backup_soc_readback",
        "discharge_soc_readback",
        "charge_power_ems_readback",
        "charge_soc_readback",
    ):
        entity_id = _source_entity_id(SENSOR._SOURCE_BY_KEY[key], entry.entry_id)
        current = hass.states.values[entity_id]
        hass.fire_report(
            entity_id,
            FakeState(current.state, current.attributes, CLOCK["now"]),
        )
    generation_id = _source_entity_id(
        SENSOR._SOURCE_BY_KEY["ems_generation"], entry.entry_id
    )
    hass.fire_state(generation_id, FakeState("2", reported=CLOCK["now"]))

    # A concurrent hard source must be consumed by the already pending bounded
    # pass, not publish the mixed EMS generation.
    hass.fire_state(battery_soc_id, FakeState("51", reported=CLOCK["now"]))
    check(
        CLOCK["calls"] == calls and sensor.visible_writes == writes,
        "Concurrent hard event escaped the pending cohort trailing edge",
    )
    before_final = sensor._build_snapshots(
        sensor._read_source_states(), CLOCK["now"]
    )[5]
    check(
        before_final.full_block_generation_at is None,
        "Generation-before-4306 fixture became coherent too early",
    )

    final_id = _source_entity_id(
        SENSOR._SOURCE_BY_KEY[final_key], entry.entry_id
    )
    final_state = hass.states.values[final_id]
    hass.fire_report(
        final_id,
        FakeState(final_state.state, final_state.attributes, CLOCK["now"]),
    )
    pending = hass.active_delays()
    check(
        len(pending) == 1
        and abs(pending[0].when - 0.100) < 1e-12
        and CLOCK["calls"] == calls
        and sensor.visible_writes == writes,
        "Final unchanged 4306 report did not retain one trailing-edge pass",
    )
    pending[0].run()
    coherent = sensor._build_snapshots(
        sensor._read_source_states(), CLOCK["now"]
    )[5]
    check(
        CLOCK["calls"] == calls + 1
        and coherent.full_block_generation_at == CLOCK["now"]
        and len(hass.active_points()) == 1
        and hass.active_points()[0] is not old_temporal
        and sensor.extra_state_attributes["profile"] == "maximum_profit"
        and sensor.extra_state_attributes["observed_owner"] == "none"
        and sensor.extra_state_attributes["owner_conflict"] is False,
        "Trailing edge did not consume planner state with the complete Self-Use cohort",
    )
    check(
        all(
            item["state"] != "blocked"
            and item["attributes"].get("execution_blocked_reason")
            != "structurally_inconsistent_context"
            for item in sensor.visible_write_history[history_start:]
        ),
        "Partial cohort leaked a structural block to the public sensor",
    )
    remove(sensor)
    check(
        not report_registration["active"]
        and report_registration["unsubscribe_calls"] == 1
        and not latest_report_registration["active"]
        and latest_report_registration["unsubscribe_calls"] == 1
        and sensor._cohort_report_unsub is None
        and sensor._cohort_cancel is None,
        "Cohort report lifecycle did not unsubscribe exactly once",
    )


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
    settling_rce = replace(
        rce,
        active_latched=True,
        result_current=True,
        recalculation_pending=False,
        current_slot_planned=True,
        current_slot_continue_eligible=True,
        control_data_ready=False,
    )
    settling_candidates = (
        SENSOR.build_rce_candidate(settling_rce, now=NOW),
        candidates[1],
        candidates[2],
    )
    settling_boundaries = set(
        sensor._semantic_boundaries(
            states,
            NOW,
            settling_rce,
            tariff,
            rcm,
            execution,
            settling_candidates,
        )
    )
    check(
        settling_rce.observed_at
        + timedelta(seconds=SENSOR._COHORT_SPAN_SECONDS, microseconds=1)
        not in settling_boundaries,
        "Renewable RCE source observation still drives the execution hold timer",
    )
    for no_settling_rce in (
        replace(settling_rce, control_data_ready=True),
        replace(settling_rce, active_latched=False),
    ):
        no_settling_boundaries = set(
            sensor._semantic_boundaries(
                states,
                NOW,
                no_settling_rce,
                tariff,
                rcm,
                execution,
                candidates,
            )
        )
        check(
            no_settling_rce.observed_at
            + timedelta(seconds=SENSOR._COHORT_SPAN_SECONDS, microseconds=1)
            not in no_settling_boundaries,
            "Inactive or coherent RCE plan gained a cohort-settling timer",
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

    recomputes: list[bool] = []
    sensor._recompute = lambda **_kwargs: recomputes.append(True)
    watchdog_at = CLOCK["now"] + timedelta(seconds=30)
    watchdog_controller = SimpleNamespace(
        execution_watchdog=("tx:rce:watchdog1", watchdog_at)
    )
    sensor._sync_execution_watchdog(watchdog_controller)
    watchdog = hass.active_points()[0]
    check(
        watchdog.when == watchdog_at + timedelta(microseconds=1),
        "Executor ACK/hold boundary was not scheduled at its exact expiry edge",
    )
    sensor._sync_execution_watchdog(watchdog_controller)
    check(
        hass.active_points() == [watchdog]
        and watchdog.cancel_calls == 0,
        "Unchanged transaction watchdog was renewed by source churn",
    )
    watchdog.run()
    check(
        recomputes == [True]
        and sensor._execution_watchdog_cancel is None
        and sensor._execution_watchdog_key is None,
        "Executor watchdog did not request exactly one fresh controller frame",
    )
    source = (COMPONENT / "supervisor_sensor.py").read_text(encoding="utf-8")
    check("async_track_point_in_utc_time" in source, "Point scheduler absent")
    check("async_track_time_interval" not in source and "EVENT_TIME_CHANGED" not in source, "Polling clock path added")


def test_master_stop_continuation() -> None:
    """A pending persisted rollback requests frames, never a policy restart."""

    hass, _entry, _runtime, sensor = environment()
    add(sensor)
    recomputes = 0

    def record(*, status: Any, rollback: Any) -> Any:
        return SimpleNamespace(
            master_stop_result=SimpleNamespace(status=status),
            transaction=SimpleNamespace(
                master_stop_requested=True,
                rollback_status=rollback,
            ),
        )

    sensor._controller = SimpleNamespace(
        record=record(
            status=SENSOR.MasterStopStatus.IN_PROGRESS,
            rollback=SENSOR.RollbackStatus.PENDING,
        )
    )
    sensor._master_stop_latched = True

    def recompute(*_args: Any, **_kwargs: Any) -> None:
        nonlocal recomputes
        recomputes += 1

    sensor._recompute = recompute
    sensor._schedule_master_stop_continuation_callback()
    pending = hass.active_delays()
    check(
        len(pending) == 1
        and pending[0].when == SENSOR._MASTER_STOP_CONTINUATION_DELAY_SECONDS,
        "Pending MASTER STOP did not schedule exactly one bounded frame request",
    )
    first = pending[0]
    first.run()
    pending = hass.active_delays()
    check(
        recomputes == 1 and len(pending) == 1 and pending[0] is not first,
        "Pending MASTER STOP did not re-arm through a fresh frame request",
    )
    sensor._cancel_master_stop_continuation_callback()
    sensor._controller.record = record(
        status=SENSOR.MasterStopStatus.BLOCKED,
        rollback=SENSOR.RollbackStatus.PENDING,
    )
    sensor._schedule_master_stop_continuation_callback()
    check(not hass.active_delays(), "Blocked MASTER STOP gained an automatic retry")
    sensor._controller.record = record(
        status=SENSOR.MasterStopStatus.IN_PROGRESS,
        rollback=SENSOR.RollbackStatus.FAILED,
    )
    sensor._schedule_master_stop_continuation_callback()
    check(not hass.active_delays(), "Failed rollback gained an automatic continuation")
    sensor._controller.record = record(
        status=SENSOR.MasterStopStatus.IN_PROGRESS,
        rollback=SENSOR.RollbackStatus.PENDING,
    )
    sensor._schedule_master_stop_continuation_callback()
    pending = hass.active_delays()
    check(len(pending) == 1, "Restored pending transaction did not schedule")
    sensor._cleanup_lifecycle()
    check(
        pending[0].cancelled and sensor._master_stop_continuation_cancel is None,
        "Lifecycle cleanup retained a MASTER STOP continuation callback",
    )
    source = (COMPONENT / "supervisor_sensor.py").read_text(encoding="utf-8")
    start = source.index("    def _schedule_master_stop_continuation_callback")
    end = source.index("    def _schedule_planner_callback", start)
    continuation = source[start:end]
    check(
        "self._recompute()" in continuation
        and "async_master_stop(" not in continuation
        and "async_reconcile(" not in continuation
        and "_async_dispatch" not in continuation,
        "MASTER STOP continuation bypassed the existing guarded state machine",
    )


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
        "async_track_time_interval",
        "event_time_changed",
        "restoreentity",
        "owner_code",
        "market_charg",
        "os.environ",
        "pathlib",
        "requests.",
        "aiohttp",
        "open(",
    )
    for token in forbidden:
        check(token not in lowered, f"Forbidden static token present: {token}")
    check("supervisormode.active" in lowered, "Active is not passed to the executor")
    check(
        "number.set_value" not in lowered
        and '"select", "select_option"' not in lowered,
        "Legacy actuator service escaped the single executor",
    )
    check(
        source.count("self.hass.services.async_call(") == 8
        and "ems_supervisor_control_lease_challenge" in source
        and "ems_supervisor_control_lease_terminal_proof" in source
        and "ems_supervisor_write_complete_block_leased" in source
        and "ems_supervisor_renew_control_lease" in source,
        "Active transport/MASTER STOP service surface changed",
    )
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
    check(
        'active_translation_keys.add("tariff_import_price_schedule")' in init_source,
        "Tariff price registry reconciliation key missing",
    )
    check(
        'active_translation_keys.add("ems_supervisor_canonical_plan")' in init_source,
        "Canonical-plan registry reconciliation key missing",
    )
    check(sensor_source.count("HoymilesSupervisorSensor(hass, entry, runtime)") == 1, "Entity registration differs")
    supervisor_position = sensor_source.index("HoymilesSupervisorSensor(hass, entry, runtime)")
    rce_position = sensor_source.index("HoymilesRCEOptimizerSensor(hass, entry, runtime)")
    tariff_position = sensor_source.index("HoymilesTariffOptimizerSensor(hass, entry, runtime)")
    rcm_position = sensor_source.index("HoymilesRCMOptimizerSensor(hass, entry, runtime)")
    canonical_position = sensor_source.index("HoymilesSupervisorCanonicalPlanSensor(")
    setup_status_position = sensor_source.index("HoymilesSetupStatusSensor(hass, entry, runtime)")
    add_position = sensor_source.index("async_add_entities(entities)", setup_status_position)
    check(
        supervisor_position
        < rce_position
        < tariff_position
        < rcm_position
        < canonical_position
        < setup_status_position
        < add_position,
        "Native sensor source order differs",
    )


def _translation_count(payload: dict[str, Any]) -> int:
    return sum(len(entries) for entries in payload["entity"].values())


def test_execution_health_contract() -> None:
    """One backend projection must classify the full R03 fault matrix."""

    base = {
        "execution_phase": "executing",
        "execution_context_evidence": {"physical_mode_fresh": True},
        "execution_physical_mode_fresh": True,
        "execution_last_valid_read_at": (NOW - timedelta(seconds=10)).isoformat(),
        "owner": "rce",
        "transaction_owner": "rce",
        "observed_owner": "rce",
        "owner_conflict": False,
        "master_stop": {"status": "not_requested"},
        "rollback_status": "not_required",
        "physical_verification_result": "confirmed",
    }

    def health(**overrides: Any) -> dict[str, Any]:
        return SENSOR._execution_health_contract({**base, **overrides}, now=NOW)

    healthy = health()
    check(healthy["status"] == "healthy", "Fresh executing state is not healthy")
    check(healthy["control_status"] == "healthy", "Fresh control status differs")
    check(healthy["connectivity"] == "connected", "Fresh connectivity differs")

    reconcile = health(execution_adapter_error="active_reconcile_failed")
    check(reconcile["status"] == "unhealthy", "Active reconcile failure is green")
    check(reconcile["reason"] == "execution_adapter_error", "Reconcile reason differs")
    check(reconcile["control_error"] == "active_reconcile_failed", "Reconcile detail lost")

    other_adapter = health(execution_adapter_error="write_timeout")
    check(other_adapter["control_status"] == "unhealthy", "Other adapter error is green")
    check(other_adapter["action"] == "restart_ems", "Adapter recovery action differs")

    stale = health(execution_last_valid_read_at=(NOW - timedelta(seconds=181)).isoformat())
    check(stale["status"] == "unknown", "Stale physical evidence is not unknown")
    check(stale["reason"] == "physical_readback_stale", "Stale reason differs")
    check(stale["connectivity"] == "stale", "Stale connectivity differs")

    missing = health(execution_last_valid_read_at=None)
    check(missing["status"] == "unknown", "Missing physical evidence is not unknown")
    check(missing["reason"] == "physical_readback_missing", "Missing reason differs")

    accounting = health(accounting_adapter_error="ledger_unavailable")
    check(accounting["status"] == "degraded", "Accounting error is not degraded")
    check(accounting["control_status"] == "healthy", "Accounting error poisons control")
    check(accounting["reason"] == "accounting_degraded", "Accounting reason differs")
    check(accounting["degradations"] == {"accounting": "ledger_unavailable"}, "Accounting detail lost")

    notifications = health(notification_adapter_error="phone_unavailable")
    check(notifications["status"] == "degraded", "Phone unavailable is not degraded")
    check(notifications["control_status"] == "healthy", "Phone error poisons control")
    check(notifications["degradations"] == {"notifications": "phone_unavailable"}, "Phone detail lost")

    pending = health(execution_phase="pending", physical_verification_result="pending")
    check(pending["status"] == "healthy", "Ordinary pending state is a false fault")
    check(pending["reason"] == "healthy", "Ordinary pending reason differs")

    conflict = health(owner_conflict=True, observed_owner="manual")
    check(conflict["status"] == "unhealthy", "Owner conflict is green")
    check(conflict["reason"] == "owner_conflict", "Owner conflict reason differs")

    recovery = health(execution_phase="restoring", rollback_status="pending")
    check(recovery["status"] == "recovering", "Recovery is not recovering")
    check(recovery["action"] == "wait_for_recovery", "Recovery action differs")
    for phase in ("stopping", "idle"):
        check(
            health(execution_phase=phase, rollback_status="pending")["status"]
            == "recovering",
            f"Pending rollback in {phase} lost recovery status",
        )
    check(
        health(lifecycle_reason="restart_recovery")["status"] == "recovering",
        "Restart recovery was marked healthy",
    )
    check(
        health(rollback_status="failed")["reason"] == "rollback_failed",
        "Failed rollback lost priority",
    )
    check(
        health(rollback_status="pending", physical_verification_result="contradicted")
        ["reason"] == "physical_verification_contradicted",
        "Pending rollback hid contradicted physical proof",
    )
    check(
        health(rollback_status="pending", physical_verification_result="unavailable")
        ["status"] == "unknown",
        "Pending rollback hid unavailable physical proof",
    )
    check(
        health(rollback_status="pending", owner_conflict=True)["reason"]
        == "owner_conflict",
        "Pending rollback hid owner conflict",
    )
    check(
        health(rollback_status="pending", accounting_adapter_error="ledger_unavailable")
        ["status"] == "degraded",
        "Pending rollback hid accounting degradation during confirmed execution",
    )

    stop_requested = health(master_stop={"status": "requested"})
    check(stop_requested["status"] == "recovering", "Requested STOP is not recovering")
    check(stop_requested["master_stop_confirmation"] == "unconfirmed", "Requested STOP confirmation differs")
    check(stop_requested["action"] == "wait_for_master_stop", "Requested STOP action differs")

    stop_unconfirmed_store = health(
        master_stop={"status": "in_progress"},
        execution_adapter_error="master_stop_record_persist_timeout",
    )
    check(
        stop_unconfirmed_store["status"] == "unhealthy"
        and stop_unconfirmed_store["reason"]
        == "master_stop_unconfirmed_persistence_failed"
        and stop_unconfirmed_store["master_stop_confirmation"] == "unconfirmed",
        "Unconfirmed physical STOP hid its persistence failure",
    )

    stop_completed = health(master_stop={"status": "completed"})
    check(stop_completed["status"] == "healthy", "Confirmed STOP is not healthy")
    check(stop_completed["master_stop_confirmation"] == "confirmed", "Completed STOP confirmation differs")

    stop_completed_store = health(
        master_stop={"status": "completed"},
        execution_adapter_error="master_stop_record_persist_failed",
    )
    check(
        stop_completed_store["status"] == "unhealthy"
        and stop_completed_store["reason"] == "master_stop_state_save_failed"
        and stop_completed_store["master_stop_confirmation"] == "confirmed",
        "Confirmed physical STOP did not retain its separate Store failure",
    )

    stop_blocked = health(master_stop={"status": "blocked"})
    check(stop_blocked["status"] == "unhealthy", "Blocked STOP is green")
    check(stop_blocked["master_stop_confirmation"] == "blocked", "Blocked STOP confirmation differs")

    returned = health(execution_last_valid_read_at=(NOW - timedelta(seconds=1)).isoformat())
    check(returned["status"] == "healthy", "Fresh data does not restore health")
    check(returned["last_valid_read_age_seconds"] == 1.0, "Fresh data age differs")

    still_broken = health(
        execution_last_valid_read_at=(NOW - timedelta(seconds=1)).isoformat(),
        execution_adapter_error="write_timeout",
    )
    check(still_broken["status"] == "unhealthy", "Fresh data hides uncleared adapter error")

    latched = health(master_stop_adapter_latched=True)
    check(latched["master_stop_status"] == "requested", "STOP latch is not projected")
    check(latched["status"] == "recovering", "Unconfirmed STOP latch is green")


def test_translations() -> None:
    en_path = COMPONENT / "translations" / "en.json"
    pl_path = COMPONENT / "translations" / "pl.json"
    en = json.loads(en_path.read_text(encoding="utf-8"))
    pl = json.loads(pl_path.read_text(encoding="utf-8"))
    check(en["entity"]["sensor"]["ems_supervisor"] == {"name": "EMS"}, "EN translation differs")
    check(pl["entity"]["sensor"]["ems_supervisor"] == {"name": "EMS"}, "PL translation differs")
    generator = (ROOT / "tools" / "build_hacs_assets.py").read_text(encoding="utf-8")
    check(generator.count('["ems_supervisor"]') == 2, "Generator native key differs")
    check(
        generator.count('["ems_supervisor_canonical_plan"]') == 2,
        "Generator canonical-plan key differs",
    )
    check(
        generator.count('["ems_shared_inputs"]') == 2,
        "Generator shared-input key differs",
    )
    check(
        generator.count('["ems_baseline_energy_timeline"]') == 2,
        "Generator baseline-timeline key differs",
    )
    check(
        generator.count('["tariff_import_price_schedule"]') == 2,
        "Generator tariff-price-schedule key differs",
    )
    baseline = _task_baseline_revision()
    before = json.loads(
        subprocess.check_output(
            ["git", "show", f"{baseline}:custom_components/hoymiles_hit_modbus/translations/en.json"],
            cwd=ROOT,
            text=True,
        )
    )
    baseline_count = _translation_count(before)
    check(
        baseline_count in {302, 306, 307, 309},
        "Baseline localized entity count differs",
    )
    current_count = _translation_count(en)
    check(current_count == 309, "Current localized entity count differs")
    expected_delta = {302: 7, 306: 3, 307: 2, 309: 0}[baseline_count]
    check(
        current_count == baseline_count + expected_delta,
        "Canonical plan, accounting v2, shared inputs, baseline timeline, tariff price, and lease translation delta differs",
    )


TEST_GROUPS = (
    ("STRUCTURE_AND_MANIFEST", test_structure_and_manifest),
    ("PLATFORM_ENTITY_ORDER", test_platform_entity_order),
    ("FRESH_INSTALL_SEQUENTIAL_ADD", test_fresh_install_sequential_add),
    ("EXISTING_INSTALL_ORDER", test_existing_install_order),
    ("ENTITY_IDENTITY", test_entity_identity),
    ("PUBLICATION", test_publication),
    ("MODE_PROFILE_PERMISSIONS", test_mode_profile_permissions),
    ("IDLE_OWNER_PUBLICATION", test_idle_owner_publication),
    ("ENTRY_RESOLUTION", test_entry_resolution),
    ("MULTI_ENTRY", test_multi_entry),
    ("SOURCE_MAP", test_source_map),
    ("PLAN_WHITELISTS", test_plan_whitelists),
    ("ATOMIC_SNAPSHOT", test_atomic_snapshot),
    ("EXECUTOR_COMMITMENT_RECOMPUTE", test_executor_commitment_recompute),
    ("EVENTING", test_eventing),
    ("RCE_RETARGET_CALLBACK_LIFECYCLE", test_rce_retarget_callback_lifecycle),
    ("RCE_SETTLING_WATCHDOG_CALLBACK", test_rce_settling_watchdog_callback),
    ("READBACK_COHORT_EVENTING", test_readback_cohort_eventing),
    ("TEMPORAL_SCHEDULING", test_temporal_scheduling),
    ("MASTER_STOP_CONTINUATION", test_master_stop_continuation),
    ("HA_LIFECYCLE_MODEL", test_ha_lifecycle_model),
    ("TRANSACTIONAL_SETUP", test_transactional_setup),
    ("ATOMIC_TIMER_PUBLICATION", test_atomic_timer_publication),
    ("CONFIG_ENTRY_UNLOAD", test_config_entry_unload),
    ("LIFECYCLE", test_lifecycle),
    ("FAILURE_HANDLING", test_failure_handling),
    ("STATIC_SAFETY", test_static_safety),
    ("EXECUTION_HEALTH_CONTRACT", test_execution_health_contract),
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
        "sources=70 existing=70 future=0 entry_local=28 global=42 H=55 P=15"
    )


if __name__ == "__main__":
    main()
