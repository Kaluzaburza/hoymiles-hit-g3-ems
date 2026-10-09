"""Deterministic offline contract for the entry-local EMS input broker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)
CLOCK = {"now": NOW}


class FakeSensorEntity:
    def __init_subclass__(cls, **_kwargs: Any) -> None:
        return None

    async def async_added_to_hass(self) -> None:
        return None

    async def async_will_remove_from_hass(self) -> None:
        for callback_func in tuple(getattr(self, "_remove_callbacks", ())):
            callback_func()
        return None

    def async_on_remove(self, callback_func: Any) -> None:
        self.__dict__.setdefault("_remove_callbacks", []).append(callback_func)

    def async_write_ha_state(self) -> None:
        return None


class FakeDeviceInfo(dict):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(kwargs)


def _install_stubs() -> None:
    custom_components = ModuleType("custom_components")
    custom_components.__path__ = [str(ROOT / "custom_components")]  # type: ignore[attr-defined]
    package = ModuleType("custom_components.hoymiles_hit_modbus")
    package.__path__ = [str(COMPONENT)]  # type: ignore[attr-defined]
    sys.modules["custom_components"] = custom_components
    sys.modules["custom_components.hoymiles_hit_modbus"] = package

    homeassistant = ModuleType("homeassistant")
    components = ModuleType("homeassistant.components")
    sensor = ModuleType("homeassistant.components.sensor")
    sensor.SensorEntity = FakeSensorEntity
    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    const = ModuleType("homeassistant.const")
    const.EntityCategory = SimpleNamespace(DIAGNOSTIC="diagnostic")
    const.STATE_UNAVAILABLE = "unavailable"
    const.STATE_UNKNOWN = "unknown"
    core = ModuleType("homeassistant.core")
    core.Event = object
    core.HomeAssistant = object
    core.callback = lambda function: function
    helpers = ModuleType("homeassistant.helpers")
    entity_registry = ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.EVENT_ENTITY_REGISTRY_UPDATED = "entity_registry_updated"
    entity_registry.async_get = lambda hass: hass.registry
    device_registry = ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = FakeDeviceInfo
    event = ModuleType("homeassistant.helpers.event")
    event.async_track_state_change_event = (
        lambda hass, entity_ids, callback: hass.track("change", entity_ids, callback)
    )
    event.async_track_state_report_event = (
        lambda hass, entity_ids, callback: hass.track("report", entity_ids, callback)
    )
    event.async_track_time_interval = (
        lambda hass, callback, interval: hass.track("interval", (str(interval),), callback)
    )
    util = ModuleType("homeassistant.util")
    dt = ModuleType("homeassistant.util.dt")
    dt.utcnow = lambda: CLOCK["now"]
    util.dt = dt

    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.components": components,
            "homeassistant.components.sensor": sensor,
            "homeassistant.config_entries": config_entries,
            "homeassistant.const": const,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.entity_registry": entity_registry,
            "homeassistant.helpers.device_registry": device_registry,
            "homeassistant.helpers.event": event,
            "homeassistant.util": util,
            "homeassistant.util.dt": dt,
        }
    )

    const_module = ModuleType("custom_components.hoymiles_hit_modbus.const")
    const_module.DOMAIN = "hoymiles_hit_modbus"
    const_module.NAME = "EMS for Hoymiles"
    sys.modules[const_module.__name__] = const_module


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()
_load_module(
    "custom_components.hoymiles_hit_modbus.energy_data",
    COMPONENT / "energy_data.py",
)
M = _load_module(
    "custom_components.hoymiles_hit_modbus.ems_shared_inputs",
    COMPONENT / "ems_shared_inputs.py",
)


@dataclass
class FakeState:
    state: str
    reported: datetime = NOW
    unit: str | None = None

    @property
    def last_reported(self) -> datetime:
        return self.reported

    @property
    def last_updated(self) -> datetime:
        return self.reported

    @property
    def attributes(self) -> dict[str, Any]:
        return {"unit_of_measurement": self.unit} if self.unit is not None else {}


@dataclass
class FakeRegistryEntry:
    entity_id: str
    unique_id: str
    config_entry_id: str
    translation_key: str
    platform: str = "hoymiles_hit_modbus"

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]


class FakeRegistry:
    def __init__(self) -> None:
        self.entities: dict[str, FakeRegistryEntry] = {}

    def add(self, entry: FakeRegistryEntry) -> None:
        self.entities[entry.entity_id] = entry


class FakeStates:
    def __init__(self) -> None:
        self.values: dict[str, FakeState] = {}

    def get(self, entity_id: str | None) -> FakeState | None:
        return self.values.get(entity_id) if entity_id is not None else None


class FakeBus:
    def __init__(self, hass: "FakeHass") -> None:
        self.hass = hass

    def async_listen(self, event_type: str, callback: Any) -> Any:
        return self.hass.track(event_type, (), callback)


class FakeHass:
    def __init__(self) -> None:
        self.registry = FakeRegistry()
        self.states = FakeStates()
        self.bus = FakeBus(self)
        self.subscriptions: list[list[Any]] = []
        self.service_calls: list[Any] = []

    def track(self, kind: str, entity_ids: Any, callback: Any) -> Any:
        record = [kind, tuple(entity_ids), callback, False]
        self.subscriptions.append(record)

        def unsubscribe() -> None:
            record[3] = True

        return unsubscribe


def _proxy(hass: FakeHass, key: str, value: str, *, unit: str | None = None) -> str:
    spec = M._NUMERIC_SOURCES[key]
    entity_id = f"{spec.domain}.entry_a_{spec.translation_key}"
    hass.registry.add(
        FakeRegistryEntry(
            entity_id=entity_id,
            unique_id=f"entry-a_{spec.translation_key}",
            config_entry_id="entry-a",
            translation_key=spec.translation_key,
        )
    )
    hass.states.values[entity_id] = FakeState(value, NOW, unit)
    return entity_id


def _coordinator() -> tuple[FakeHass, Any]:
    hass = FakeHass()
    values = {
        "battery_capacity": "26",
        "total_capacity": "25.5",
        "battery_soc": "55",
        "self_use_reserve": "20",
        "hardware_minimum_soc": "10",
        "hardware_maximum_soc": "90",
        "inverter_count": "1",
        "bms_voltage": "52",
        "bms_charge_current": "100",
        "bms_discharge_current": "120",
        "gcf_supported": "1",
        "gcf_generation": "8",
        "gcf_enable": "1",
        "gcf_limit": "0",
        "pv_power": "5000",
        "system_load_power": "4500",
        "battery_power": "-1000",
        "grid_power": "500",
        "home_load_l1": "1000",
        "home_load_l2": "1500",
        "home_load_l3": "500",
    }
    for key, value in values.items():
        _proxy(
            hass,
            key,
            value,
            unit="W" if M._NUMERIC_SOURCES[key].power_unit else None,
        )

    # A second entry with the same translation keys must never be selected.
    hass.registry.add(
        FakeRegistryEntry(
            entity_id="sensor.entry_b_overview_pv_total_power",
            unique_id="entry-b_overview_pv_total_power",
            config_entry_id="entry-b",
            translation_key="overview_pv_total_power",
        )
    )
    hass.states.values["sensor.entry_b_overview_pv_total_power"] = FakeState(
        "999999", NOW, "W"
    )
    for key, value in {
        "hardware_minimum_soc": "35",
        "hardware_maximum_soc": "45",
    }.items():
        spec = M._NUMERIC_SOURCES[key]
        entity_id = f"{spec.domain}.entry_b_{spec.translation_key}"
        hass.registry.add(
            FakeRegistryEntry(
                entity_id=entity_id,
                unique_id=f"entry-b_{spec.translation_key}",
                config_entry_id="entry-b",
                translation_key=spec.translation_key,
            )
        )
        hass.states.values[entity_id] = FakeState(value)

    helpers = {
        M.NEW_FORECAST_HELPERS["today"]: "sensor.shared_today",
        M.NEW_FORECAST_HELPERS["tomorrow"]: "sensor.shared_tomorrow",
        M.NEW_FORECAST_HELPERS["day3"]: "",
        M.LEGACY_FORECAST_HELPERS["day3"]: "sensor.legacy_day3",
        M.NEW_FALLBACK_LOAD_HELPER: "0",
        M.LEGACY_FALLBACK_LOAD_HELPER: "12.5",
        M.NEW_RATED_POWER_HELPER: "Automatycznie",
        M.LEGACY_RATED_POWER_HELPER: "20 kW",
        M.NEW_PV_TO_BATTERY_EFFICIENCY_HELPER: "95",
        M.NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER: "94",
        M.LEGACY_PV_TO_BATTERY_EFFICIENCY_HELPER: "95",
        M.LEGACY_BATTERY_TO_HOME_EFFICIENCY_HELPER: "94",
    }
    for entity_id, value in helpers.items():
        hass.states.values[entity_id] = FakeState(value)
    for entity_id, value in {
        "sensor.shared_today": "20",
        "sensor.shared_tomorrow": "18",
        "sensor.legacy_day3": "16",
        M.FORECAST_CANDIDATES["remaining_today"][0]: "7",
    }.items():
        hass.states.values[entity_id] = FakeState(value)

    coordinator = M.EMSSharedInputsCoordinator(
        hass,
        config_entry_id="entry-a",
        source_device_model="HIT-10L-G3",
        now_provider=lambda: CLOCK["now"],
    )
    return hass, coordinator


def test_contract_and_identity() -> None:
    hass, coordinator = _coordinator()
    asyncio.run(coordinator.async_refresh())
    snapshot = coordinator.snapshot
    assert snapshot.schema_version == 1
    assert snapshot.config_entry_id == "entry-a"
    assert snapshot.system.inverter_model.value == "HIT-10L-G3"
    assert snapshot.system.inverter_rated_power_each_kw.value == 10.0
    assert snapshot.system.inverter_rated_power_each_kw.provenance == "device_model"
    assert snapshot.system.inverter_count.value == 1
    assert snapshot.system.system_rated_power_kw.value == 10.0
    assert snapshot.system.hardware_minimum_soc_percent.value == 10.0
    assert snapshot.system.hardware_minimum_soc_percent.entity_id == (
        "number.entry_a_minimum_soc"
    )
    assert snapshot.system.hardware_minimum_soc_percent.provenance == (
        "entry_local_registry"
    )
    assert snapshot.system.hardware_maximum_soc_percent.value == 90.0
    assert snapshot.system.hardware_maximum_soc_percent.entity_id == (
        "number.entry_a_maximum_soc"
    )
    assert snapshot.power.pv_power_kw.value == 5.0
    assert snapshot.power.pv_power_kw.entity_id == "sensor.entry_a_overview_pv_total_power"
    assert snapshot.power.home_load_power_kw.value == 3.0
    assert snapshot.power.home_load_power_kw.entity_id is None
    assert len(snapshot.power.home_load_power_kw.source_entity_ids) == 3
    assert snapshot.power.system_load_power_kw.value == 4.5
    assert snapshot.power.home_load_power_kw.value != snapshot.power.system_load_power_kw.value
    assert snapshot.forecast.today.entity_id == "sensor.shared_today"
    assert snapshot.forecast.today.provenance == "new_helper"
    assert snapshot.forecast.day3.entity_id == "sensor.legacy_day3"
    assert snapshot.forecast.day3.provenance == "legacy_helper"
    assert snapshot.forecast.remaining_today.fresh
    assert snapshot.bms.maximum_charge_power_kw.value == 5.2
    assert snapshot.gcf.gcf_readback_ready
    assert snapshot.gcf.zero_export_confirmed
    assert snapshot.gcf.export_allowed is False
    assert snapshot.efficiency.pv_to_battery_efficiency.value == 95.0
    assert snapshot.efficiency.battery_to_home_efficiency.value == 94.0
    assert snapshot.efficiency.pv_to_battery_efficiency.provenance == (
        "neutral_model_helper"
    )
    assert snapshot.power.grid_to_battery_power.value is None
    assert not snapshot.power.grid_to_battery_ready
    assert (
        M.P1_PROVIDERLESS_GRID_TO_BATTERY_DEPENDENCY in snapshot.issues
    )
    flat = snapshot.flat_public_attributes()
    assert set(flat) == M.REQUIRED_FLAT_ATTRIBUTE_KEYS
    assert flat["reserve_soc_floor_percent"] == 20.0
    assert flat["hardware_minimum_soc_percent"] == 10.0
    assert flat["hardware_maximum_soc_percent"] == 90.0
    assert flat["forecast_remaining_today_ready"] is True
    assert flat["grid_to_battery_power_entity"] is None
    assert flat["migration_version"] == 2
    assert hass.service_calls == []


def test_home_load_signed_phases_match_canonical_non_negative_sum() -> None:
    hass, coordinator = _coordinator()
    phase_ids = {
        key: f"sensor.entry_a_{M._NUMERIC_SOURCES[key].translation_key}"
        for key in ("home_load_l1", "home_load_l2", "home_load_l3")
    }
    for key, value in {
        "home_load_l1": "223",
        "home_load_l2": "-10",
        "home_load_l3": "791",
    }.items():
        hass.states.values[phase_ids[key]].state = value

    asyncio.run(coordinator.async_refresh())
    sample = coordinator.snapshot.power.home_load_power_kw
    assert sample.fresh
    assert sample.value == 1.014
    assert sample.reason == "derived_exact_phase_sum"
    assert sample.provenance == "entry_local_exact_phase_sum"
    assert sample.source_entity_ids == tuple(phase_ids.values())

    # Idle/disappearing load can leave a bounded negative phase residual.
    # It contributes zero, never subtracting from another phase's real load.
    for residual_w in (-100, -143, -200, -300):
        hass.states.values[phase_ids["home_load_l2"]].state = str(residual_w)
        asyncio.run(coordinator.async_refresh())
        sample = coordinator.snapshot.power.home_load_power_kw
        assert sample.fresh, (residual_w, sample.reason)
        assert sample.value == 1.014

    # Loss of freshness on any exact phase still withholds the aggregate.
    hass.states.values[phase_ids["home_load_l2"]].reported = NOW - timedelta(
        seconds=M.SHARED_INPUTS_PHYSICAL_MAX_AGE_SECONDS + 1
    )
    asyncio.run(coordinator.async_refresh())
    sample = coordinator.snapshot.power.home_load_power_kw
    assert sample.value is None
    assert not sample.fresh
    assert sample.reason == "incomplete_exact_phase_set"

    # INT16_MIN is a common invalid/sentinel-shaped value and must never be
    # converted into a fresh zero load.
    hass.states.values[phase_ids["home_load_l2"]].reported = NOW
    hass.states.values[phase_ids["home_load_l2"]].state = "-32768"
    asyncio.run(coordinator.async_refresh())
    sample = coordinator.snapshot.power.home_load_power_kw
    assert sample.value is None
    assert not sample.fresh
    assert sample.reason == "incomplete_exact_phase_set"

    # Only the bounded idle-phase residual is normalized. A materially
    # negative value remains incoherent and withholds the aggregate instead
    # of being silently converted into a healthy zero.
    hass.states.values[phase_ids["home_load_l2"]].reported = NOW
    hass.states.values[phase_ids["home_load_l2"]].state = "-301"
    asyncio.run(coordinator.async_refresh())
    sample = coordinator.snapshot.power.home_load_power_kw
    assert sample.value is None
    assert not sample.fresh
    assert sample.reason == "incomplete_exact_phase_set"

    # The upper physical limit is still the signed 16-bit register ceiling.
    hass.states.values[phase_ids["home_load_l2"]].state = "32768"
    asyncio.run(coordinator.async_refresh())
    sample = coordinator.snapshot.power.home_load_power_kw
    assert sample.value is None
    assert not sample.fresh
    assert sample.reason == "incomplete_exact_phase_set"


def test_home_load_night_residual_replay_and_all_idle_phases() -> None:
    hass, coordinator = _coordinator()
    phase_ids = tuple(
        f"sensor.entry_a_{M._NUMERIC_SOURCES[key].translation_key}"
        for key in ("home_load_l1", "home_load_l2", "home_load_l3")
    )
    for values_w, expected_kw in (
        ((921, 1056, -143), 1.977),  # 7 Oct, 22:26 local, recorded input
        ((-143, 921, 1056), 1.977),
        ((921, -143, 1056), 1.977),
        ((-300, -200, -143), 0.0),
        ((921, 1056, -301), None),
        ((-1732, -1787, -1627), None),  # Later transition: outside tolerance
    ):
        for entity_id, value in zip(phase_ids, values_w):
            hass.states.values[entity_id].state = str(value)
        asyncio.run(coordinator.async_refresh())
        sample = coordinator.snapshot.power.home_load_power_kw
        assert sample.value == expected_kw, (values_w, sample)
        assert sample.fresh is (expected_kw is not None)
        assert sample.source_entity_ids == phase_ids
        assert sample.provenance == "entry_local_exact_phase_sum"


def test_fractional_gcf_generation_cannot_confirm_zero_export() -> None:
    hass, coordinator = _coordinator()
    generation_id = (
        f"sensor.entry_a_{M._NUMERIC_SOURCES['gcf_generation'].translation_key}"
    )
    hass.states.values[generation_id].state = "7.5"
    asyncio.run(coordinator.async_refresh())
    snapshot = coordinator.snapshot
    assert snapshot.gcf.generation.value is None
    assert snapshot.gcf.generation.reason == "not_integer"
    assert not snapshot.gcf.gcf_readback_ready
    assert not snapshot.gcf.zero_export_confirmed
    assert snapshot.gcf.export_allowed is None


def test_hardware_soc_number_proxies_fail_closed() -> None:
    minimum_spec = M._NUMERIC_SOURCES["hardware_minimum_soc"]
    maximum_spec = M._NUMERIC_SOURCES["hardware_maximum_soc"]
    assert (minimum_spec.domain, minimum_spec.minimum, minimum_spec.maximum) == (
        "number",
        10.0,
        90.0,
    )
    assert (maximum_spec.domain, maximum_spec.minimum, maximum_spec.maximum) == (
        "number",
        10.0,
        100.0,
    )
    hass, coordinator = _coordinator()
    asyncio.run(coordinator.async_refresh())
    minimum_id = coordinator.snapshot.system.hardware_minimum_soc_percent.entity_id
    maximum_id = coordinator.snapshot.system.hardware_maximum_soc_percent.entity_id
    assert minimum_id == "number.entry_a_minimum_soc"
    assert maximum_id == "number.entry_a_maximum_soc"

    asyncio.run(coordinator.async_start())
    active_state_subscriptions = [
        record
        for record in hass.subscriptions
        if record[0] in {"change", "report"} and not record[3]
    ]
    assert len(active_state_subscriptions) == 2
    assert all(minimum_id in record[1] for record in active_state_subscriptions)
    assert all(maximum_id in record[1] for record in active_state_subscriptions)
    asyncio.run(coordinator.async_stop())

    hass.states.values[maximum_id].reported = NOW - timedelta(
        seconds=M.SHARED_INPUTS_PHYSICAL_MAX_AGE_SECONDS + 1
    )
    asyncio.run(coordinator.async_refresh())
    maximum = coordinator.snapshot.system.hardware_maximum_soc_percent
    assert maximum.value is None
    assert not maximum.fresh
    assert maximum.reason == "stale"
    assert maximum.entity_id == maximum_id
    assert maximum.provenance == "entry_local_registry"

    minimum_entry = hass.registry.entities.pop(minimum_id)
    hass.registry.add(
        FakeRegistryEntry(
            entity_id="sensor.wrong_domain_minimum_soc",
            unique_id="entry-a_minimum_soc",
            config_entry_id="entry-a",
            translation_key="minimum_soc",
        )
    )
    hass.states.values["sensor.wrong_domain_minimum_soc"] = FakeState("77")
    asyncio.run(coordinator.async_refresh())
    minimum = coordinator.snapshot.system.hardware_minimum_soc_percent
    assert minimum.value is None
    assert minimum.entity_id is None
    assert minimum.reason == "source_unavailable"
    assert minimum.provenance == "entry_local_registry"

    hass.registry.add(minimum_entry)
    hass.registry.add(
        FakeRegistryEntry(
            entity_id="number.duplicate_minimum_soc",
            unique_id="entry-a_minimum_soc",
            config_entry_id="entry-a",
            translation_key="minimum_soc",
        )
    )
    hass.states.values["number.duplicate_minimum_soc"] = FakeState("12")
    asyncio.run(coordinator.async_refresh())
    minimum = coordinator.snapshot.system.hardware_minimum_soc_percent
    assert minimum.value is None
    assert minimum.reason == "ambiguous_entry"
    assert minimum.provenance == "entry_local_registry"


def test_removing_diagnostic_client_does_not_stop_entry_coordinator() -> None:
    hass, coordinator = _coordinator()
    asyncio.run(coordinator.async_start())
    updates: list[int] = []
    coordinator.async_listen(lambda: updates.append(coordinator.snapshot.revision))
    entry = SimpleNamespace(entry_id="entry-a")
    source = SimpleNamespace(
        name_by_user=None,
        name="Source",
        manufacturer="Hoymiles",
        model="HIT-10L-G3",
        sw_version="test",
    )
    entity = M.HoymilesEMSSharedInputsSensor(hass, entry, coordinator, source)
    assert entity._unrecorded_attributes == frozenset({M.MATCH_ALL})
    asyncio.run(entity.async_added_to_hass())
    asyncio.run(entity.async_will_remove_from_hass())
    assert coordinator._started
    assert any(not record[3] for record in hass.subscriptions)

    capacity_id = coordinator.snapshot.system.battery_capacity_kwh.entity_id
    assert capacity_id is not None
    hass.states.values[capacity_id].state = "27"
    asyncio.run(coordinator.async_refresh())
    assert coordinator.snapshot.system.battery_capacity_kwh.value == 27.0
    assert updates
    asyncio.run(coordinator.async_stop())


def test_semantic_revision_ignores_diagnostic_time_churn() -> None:
    hass, coordinator = _coordinator()
    try:
        asyncio.run(coordinator.async_refresh())
        initial_revision = coordinator.snapshot.revision
        updates: list[int] = []
        coordinator.async_listen(lambda: updates.append(coordinator.snapshot.revision))

        # Advancing only the observation clock changes diagnostic ages, not the
        # optimizer inputs or their freshness classification.
        CLOCK["now"] = NOW + timedelta(seconds=1)
        asyncio.run(coordinator.async_refresh())
        assert coordinator.snapshot.revision == initial_revision
        assert updates == []

        # A repeated physical report with the same value refreshes provenance,
        # but must not starve slower consumers with a new semantic revision.
        capacity_id = coordinator.snapshot.system.battery_capacity_kwh.entity_id
        assert capacity_id is not None
        hass.states.values[capacity_id].reported = CLOCK["now"]
        asyncio.run(coordinator.async_refresh())
        assert coordinator.snapshot.revision == initial_revision
        assert updates == []

        hass.states.values[capacity_id].state = "27"
        asyncio.run(coordinator.async_refresh())
        assert coordinator.snapshot.revision == initial_revision + 1
        assert updates == [initial_revision + 1]

        # Diagnostic time is ignored only while the semantic freshness state
        # is unchanged. Crossing the physical stale boundary still notifies.
        CLOCK["now"] = NOW + timedelta(
            seconds=M.SHARED_INPUTS_PHYSICAL_MAX_AGE_SECONDS + 1
        )
        asyncio.run(coordinator.async_refresh())
        assert coordinator.snapshot.revision == initial_revision + 2
        assert updates == [initial_revision + 1, initial_revision + 2]
    finally:
        CLOCK["now"] = NOW


def test_semantic_revision_ignores_identical_forecast_report_timestamp() -> None:
    hass, coordinator = _coordinator()
    try:
        asyncio.run(coordinator.async_refresh())
        initial_revision = coordinator.snapshot.revision
        initial_last_updated = coordinator.snapshot.flat_public_attributes()[
            "forecast_last_updated"
        ]
        updates: list[int] = []
        coordinator.async_listen(lambda: updates.append(coordinator.snapshot.revision))

        forecast_entity_id = coordinator.snapshot.forecast.today.entity_id
        assert forecast_entity_id is not None
        CLOCK["now"] = NOW + timedelta(seconds=1)
        hass.states.values[forecast_entity_id].reported = CLOCK["now"]
        asyncio.run(coordinator.async_refresh())

        assert coordinator.snapshot.forecast.today.value == 20.0
        assert coordinator.snapshot.forecast.today.fresh
        assert coordinator.snapshot.flat_public_attributes()[
            "forecast_last_updated"
        ] != initial_last_updated
        assert coordinator.snapshot.revision == initial_revision
        assert updates == []
    finally:
        CLOCK["now"] = NOW


def test_semantic_revision_tracks_forecast_fresh_to_stale() -> None:
    hass, coordinator = _coordinator()
    try:
        asyncio.run(coordinator.async_refresh())
        initial_revision = coordinator.snapshot.revision
        forecast_entity_id = coordinator.snapshot.forecast.today.entity_id
        assert forecast_entity_id is not None
        assert coordinator.snapshot.forecast.today.fresh
        updates: list[int] = []
        coordinator.async_listen(lambda: updates.append(coordinator.snapshot.revision))

        CLOCK["now"] = NOW + timedelta(
            seconds=M.SHARED_INPUTS_FORECAST_MAX_AGE_SECONDS + 1
        )
        for state in hass.states.values.values():
            state.reported = CLOCK["now"]
        hass.states.values[forecast_entity_id].reported = NOW
        asyncio.run(coordinator.async_refresh())

        assert not coordinator.snapshot.forecast.today.fresh
        assert coordinator.snapshot.forecast.today.reason == "stale"
        assert coordinator.snapshot.revision == initial_revision + 1
        assert updates == [initial_revision + 1]
    finally:
        CLOCK["now"] = NOW


def test_config_entry_owns_shared_coordinator_lifecycle() -> None:
    source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    migration = source.index("await migration.async_run_copy_once")
    start = source.index("await shared_inputs.async_start()")
    forward = source.index("await hass.config_entries.async_forward_entry_setups")
    stop = source.index("await shared_inputs.async_stop()")
    assert forward < migration < start < stop
    assert "shared_inputs.set_migration_status(migration_ledger.as_public_dict())" in source
    assert "await self._coordinator.async_stop()" not in (
        COMPONENT / "ems_shared_inputs.py"
    ).read_text(encoding="utf-8")
    assert "await self._coordinator.async_start()" not in (
        COMPONENT / "baseline_energy_timeline_sensor.py"
    ).read_text(encoding="utf-8")


def test_load_provider_bounds_and_timestamps() -> None:
    _hass, coordinator = _coordinator()
    profile = tuple(0.25 for _ in range(48))
    coordinator.attach_load_model_provider(
        lambda _now: M.SharedLoadModelSnapshot(
            average_daily_home_load_kwh=12.0,
            average_night_home_load_kwh=3.0,
            provisional_daily_load_projection_kwh=11.5,
            daily_history_days=9,
            night_history_days=4,
            daily_totals_kwh=tuple(float(index) for index in range(1, 10)),
            average_profile_30m_kwh=profile,
            weekday_profile_30m_kwh=profile,
            weekend_profile_30m_kwh=profile,
            weekday_profile_days=7,
            weekend_profile_days=2,
            profile_history_days=9,
            daily_coverage_ratio=0.995,
            profile_coverage_ratio=0.991,
            current_day_energy_kwh=4.25,
            current_day_observed_at=NOW,
            persistence_delta_kw=0.4,
            persistence_observed_at=NOW,
            persistence_sample_count=5,
            model_schema="load_forecast_v2",
            model_quality="complete",
            generated_at=NOW,
            ready=True,
            fallback_currently_used=False,
            source="rce_recorder_broker",
        )
    )
    asyncio.run(coordinator.async_refresh())
    load = coordinator.snapshot.load
    assert load.ready and load.daily_history_days == 9
    assert not load.fallback_currently_used
    assert len(load.average_profile_30m_kwh) == 48
    flat = coordinator.snapshot.flat_public_attributes()
    assert flat["load_history_complete"]
    assert flat["load_profile_history_days"] == 9
    assert flat["load_daily_coverage_ratio"] == 0.995
    assert flat["load_profile_coverage_ratio"] == 0.991
    assert flat["load_current_day_energy_kwh"] == 4.25
    assert flat["load_persistence_delta_kw"] == 0.4
    assert flat["load_persistence_sample_count"] == 5
    assert flat["load_model_schema"] == "load_forecast_v2"
    assert flat["load_model_quality"] == "complete"

    coordinator.detach_load_model_provider(coordinator._load_model_provider)
    coordinator.attach_load_model_provider(
        lambda _now: {
            "daily_history_days": True,
            "generated_at": NOW,
            "ready": True,
            "source": "invalid_bool",
        }
    )
    asyncio.run(coordinator.async_refresh())
    assert coordinator.snapshot.load.source == "invalid_provider"
    assert not coordinator.snapshot.load.ready

    coordinator.detach_load_model_provider(coordinator._load_model_provider)
    coordinator.attach_load_model_provider(
        lambda _now: {
            "average_daily_home_load_kwh": float("nan"),
            "daily_history_days": 1,
            "generated_at": NOW,
            "ready": True,
            "source": "nonfinite",
        }
    )
    asyncio.run(coordinator.async_refresh())
    assert coordinator.snapshot.load.source == "invalid_provider"

    coordinator.detach_load_model_provider(coordinator._load_model_provider)
    coordinator.attach_load_model_provider(
        lambda _now: {
            "daily_history_days": 1,
            "generated_at": NOW.replace(tzinfo=None),
            "ready": True,
            "source": "naive",
        }
    )
    asyncio.run(coordinator.async_refresh())
    assert coordinator.snapshot.load.source == "invalid_provider"

    coordinator.detach_load_model_provider(coordinator._load_model_provider)
    coordinator.attach_load_model_provider(
        lambda _now: {
            "daily_history_days": 1,
            "generated_at": NOW + timedelta(seconds=6),
            "ready": True,
            "source": "future",
        }
    )
    asyncio.run(coordinator.async_refresh())
    assert not coordinator.snapshot.load.ready


def test_explicit_fallback_remains_ready_with_stale_history_timestamp() -> None:
    hass, coordinator = _coordinator()
    stale_history_at = NOW - timedelta(hours=31)
    hass.states.values[M.NEW_FALLBACK_LOAD_HELPER] = FakeState("17", NOW)
    coordinator.attach_load_model_provider(
        lambda _now: {
            "average_daily_home_load_kwh": 17.0,
            "daily_history_days": 0,
            "night_history_days": 0,
            "generated_at": stale_history_at,
            "ready": True,
            "fallback_currently_used": True,
            "model_quality": "fallback",
            "source": "recorder_phase_counters_and_actual_load",
        }
    )
    asyncio.run(coordinator.async_refresh())
    load = coordinator.snapshot.load
    assert load.generated_at == stale_history_at
    assert load.fallback_daily_home_load_kwh.fresh
    assert load.fallback_daily_home_load_kwh.value == 17.0
    assert load.fallback_currently_used
    assert load.ready

    hass.states.values[M.NEW_FALLBACK_LOAD_HELPER] = FakeState("0", NOW)
    asyncio.run(coordinator.async_refresh())
    assert not coordinator.snapshot.load.ready


def test_helper_precedence_and_legacy_fallback() -> None:
    hass, coordinator = _coordinator()
    hass.states.values[M.NEW_RATED_POWER_HELPER].state = "12 kW"
    asyncio.run(coordinator.async_refresh())
    rated = coordinator.snapshot.system.inverter_rated_power_each_kw
    assert rated.value == 12.0 and rated.provenance == "new_helper"

    hass.states.values[M.NEW_RATED_POWER_HELPER].state = "unavailable"
    coordinator.source_device_model = "HIT xxL G3"
    asyncio.run(coordinator.async_refresh())
    rated = coordinator.snapshot.system.inverter_rated_power_each_kw
    assert rated.value == 20.0 and rated.provenance == "legacy_helper"

    # A non-empty invalid new source remains authoritative and fail-closed;
    # it never silently resurrects the legacy selector.
    hass.states.values[M.NEW_FORECAST_HELPERS["today"]].state = "not an entity"
    asyncio.run(coordinator.async_refresh())
    today = coordinator.snapshot.forecast.today
    assert today.value is None
    assert today.reason == "invalid_entity_id"
    assert today.provenance == "new_helper"


def test_terminal_migration_closes_each_legacy_fallback_on_restart() -> None:
    hass, coordinator = _coordinator()
    # Before the lifecycle migration runs, compatibility aliases remain open.
    asyncio.run(coordinator.async_refresh())
    assert coordinator.snapshot.forecast.day3.provenance == "legacy_helper"
    assert coordinator.snapshot.load.fallback_daily_home_load_kwh.value == 12.5

    coordinator.set_migration_status(
        {
            "version": 2,
            "result": "complete",
            "fields": {
                "forecast_today": "preserved_new",
                "forecast_tomorrow": "preserved_new",
                "forecast_day3": "copied_legacy",
                "fallback_daily_home_load": "copied_legacy",
                "inverter_rated_power_each": "preserved_new",
            },
            "seeded_values": {},
        }
    )
    # Simulate a restart where neutral states are temporarily unavailable.
    for entity_id in (
        M.NEW_FORECAST_HELPERS["today"],
        M.NEW_FORECAST_HELPERS["day3"],
        M.NEW_FALLBACK_LOAD_HELPER,
        M.NEW_RATED_POWER_HELPER,
    ):
        hass.states.values[entity_id].state = "unavailable"
    hass.states.values[M.LEGACY_FORECAST_HELPERS["today"]] = FakeState(
        "sensor.legacy_today"
    )
    hass.states.values["sensor.legacy_today"] = FakeState("99")
    hass.states.values[M.LEGACY_FORECAST_HELPERS["day3"]].state = (
        "sensor.legacy_day3"
    )
    hass.states.values[M.LEGACY_FALLBACK_LOAD_HELPER].state = "199"
    hass.states.values[M.LEGACY_RATED_POWER_HELPER].state = "20 kW"

    asyncio.run(coordinator.async_start())
    snapshot = coordinator.snapshot
    assert snapshot.forecast.today.value is None
    assert snapshot.forecast.today.provenance != "legacy_helper"
    assert snapshot.forecast.day3.value is None
    assert snapshot.forecast.day3.provenance != "legacy_helper"
    assert snapshot.load.fallback_daily_home_load_kwh.value is None
    assert snapshot.system.inverter_rated_power_each_kw.value is None
    assert not any(
        entity_id in coordinator._watched_entity_ids
        for entity_id in (
            *M.LEGACY_FORECAST_HELPERS.values(),
            M.LEGACY_FALLBACK_LOAD_HELPER,
            M.LEGACY_RATED_POWER_HELPER,
        )
    )

    # Further legacy changes cannot alter the terminal neutral result.
    hass.states.values[M.LEGACY_FALLBACK_LOAD_HELPER].state = "17"
    hass.states.values[M.LEGACY_RATED_POWER_HELPER].state = "5 kW"
    asyncio.run(coordinator.async_refresh())
    assert coordinator.snapshot.load.fallback_daily_home_load_kwh.value is None
    assert coordinator.snapshot.system.inverter_rated_power_each_kw.value is None
    asyncio.run(coordinator.async_stop())


def test_neutral_efficiency_copy_once_provenance_and_no_fallback() -> None:
    hass, coordinator = _coordinator()
    coordinator.set_migration_status(
        {
            "version": 2,
            "result": "complete",
            "fields": {
                "pv_to_battery_efficiency": "copied_legacy",
                "battery_to_home_efficiency": "copied_legacy",
            },
            "seeded_values": {
                "pv_to_battery_efficiency": 95.0,
                "battery_to_home_efficiency": 94.0,
            },
        }
    )
    asyncio.run(coordinator.async_refresh())
    efficiency = coordinator.snapshot.efficiency
    assert efficiency.pv_to_battery_efficiency.provenance == (
        "migration_seed_from_legacy"
    )
    assert efficiency.battery_to_home_efficiency.provenance == (
        "migration_seed_from_legacy"
    )

    # A later neutral edit changes only that direction's provenance/value.
    hass.states.values[M.NEW_PV_TO_BATTERY_EFFICIENCY_HELPER].state = "92"
    asyncio.run(coordinator.async_refresh())
    efficiency = coordinator.snapshot.efficiency
    assert efficiency.pv_to_battery_efficiency.value == 92.0
    assert efficiency.pv_to_battery_efficiency.provenance == "neutral_model_helper"
    assert efficiency.battery_to_home_efficiency.value == 94.0
    assert efficiency.battery_to_home_efficiency.provenance == (
        "migration_seed_from_legacy"
    )

    # The legacy tariff helper remains populated but is never a live fallback.
    hass.states.values.pop(M.NEW_PV_TO_BATTERY_EFFICIENCY_HELPER)
    hass.states.values[M.LEGACY_PV_TO_BATTERY_EFFICIENCY_HELPER].state = "99"
    asyncio.run(coordinator.async_refresh())
    sample = coordinator.snapshot.efficiency.pv_to_battery_efficiency
    assert sample.value is None
    assert sample.provenance == "unavailable"
    assert sample.selector_entity_id == M.NEW_PV_TO_BATTERY_EFFICIENCY_HELPER


def test_freshness_nonfinite_ambiguity_and_lifecycle() -> None:
    hass, coordinator = _coordinator()
    asyncio.run(coordinator.async_refresh())

    today = hass.states.values["sensor.shared_today"]
    today.reported = NOW - timedelta(hours=19)
    voltage_id = coordinator.snapshot.bms.voltage_v.entity_id
    assert voltage_id is not None
    hass.states.values[voltage_id].state = "nan"
    discharge_id = coordinator.snapshot.bms.maximum_discharge_current_a.entity_id
    assert discharge_id is not None
    hass.states.values[discharge_id].state = "inf"
    count_id = coordinator.snapshot.system.inverter_count.entity_id
    assert count_id is not None
    hass.states.values[count_id].reported = NOW + timedelta(seconds=6)
    asyncio.run(coordinator.async_refresh())
    snapshot = coordinator.snapshot
    assert not snapshot.forecast.today.fresh
    assert snapshot.forecast.today.reason == "stale"
    assert not snapshot.bms.voltage_v.fresh
    assert snapshot.bms.voltage_v.reason == "not_finite"
    assert not snapshot.bms.maximum_discharge_current_a.fresh
    assert snapshot.bms.maximum_discharge_current_a.reason == "not_finite"
    assert not snapshot.system.inverter_count.fresh
    assert snapshot.system.inverter_count.reason == "future_timestamp"

    # A duplicate exact identity is ambiguity, never a first-match fallback.
    hass.registry.add(
        FakeRegistryEntry(
            entity_id="sensor.duplicate_capacity",
            unique_id="entry-a_battery_capacity",
            config_entry_id="entry-a",
            translation_key="battery_capacity",
        )
    )
    hass.states.values["sensor.duplicate_capacity"] = FakeState("99")
    asyncio.run(coordinator.async_refresh())
    assert coordinator.snapshot.system.battery_capacity_kwh.value is None
    assert coordinator.snapshot.system.battery_capacity_kwh.reason == "ambiguous_entry"

    asyncio.run(coordinator.async_start())
    assert hass.subscriptions
    asyncio.run(coordinator.async_stop())
    assert all(record[3] for record in hass.subscriptions)

    try:
        M.EMSSharedInputsCoordinator(
            hass,
            config_entry_id="entry-a",
            source_device_model="HIT-10L-G3",
            now_provider=lambda: NOW.replace(tzinfo=None),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("naive coordinator clock must be rejected")
    assert M.strict_rated_power_from_model("prefix HIT-10L-G3") is None
    assert M.strict_rated_power_from_model("HIT-20L-G3") == 20.0


def main() -> None:
    test_contract_and_identity()
    test_home_load_signed_phases_match_canonical_non_negative_sum()
    test_home_load_night_residual_replay_and_all_idle_phases()
    test_fractional_gcf_generation_cannot_confirm_zero_export()
    test_hardware_soc_number_proxies_fail_closed()
    test_removing_diagnostic_client_does_not_stop_entry_coordinator()
    test_semantic_revision_ignores_diagnostic_time_churn()
    test_semantic_revision_ignores_identical_forecast_report_timestamp()
    test_semantic_revision_tracks_forecast_fresh_to_stale()
    test_config_entry_owns_shared_coordinator_lifecycle()
    test_load_provider_bounds_and_timestamps()
    test_explicit_fallback_remains_ready_with_stale_history_timestamp()
    test_helper_precedence_and_legacy_fallback()
    test_terminal_migration_closes_each_legacy_fallback_on_restart()
    test_neutral_efficiency_copy_once_provenance_and_no_fallback()
    test_freshness_nonfinite_ambiguity_and_lifecycle()
    print("Shared EMS inputs: 16 deterministic groups passed")


if __name__ == "__main__":
    main()
