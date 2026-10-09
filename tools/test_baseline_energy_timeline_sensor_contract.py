"""Offline adapter and registration contracts for the baseline publisher."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
PACKAGE = "custom_components.hoymiles_hit_modbus"


class FakeSensorEntity:
    def __init_subclass__(cls, **_kwargs: Any) -> None:
        return None

    async def async_added_to_hass(self) -> None:
        return None

    def async_on_remove(self, _callback: Any) -> None:
        return None

    def async_write_ha_state(self) -> None:
        return None


class FakeState:
    def __init__(self, state: str, attributes: dict[str, Any]) -> None:
        self.state = state
        self.attributes = attributes


class FakeStates(dict[str, FakeState]):
    def get(self, entity_id: str | None, default: Any = None) -> Any:
        if entity_id is None:
            return default
        return super().get(entity_id, default)


def _install_stubs() -> None:
    custom_components = ModuleType("custom_components")
    custom_components.__path__ = [str(ROOT / "custom_components")]  # type: ignore[attr-defined]
    package = ModuleType(PACKAGE)
    package.__path__ = [str(COMPONENT)]  # type: ignore[attr-defined]
    sys.modules["custom_components"] = custom_components
    sys.modules[PACKAGE] = package

    homeassistant = ModuleType("homeassistant")
    components = ModuleType("homeassistant.components")
    sensor = ModuleType("homeassistant.components.sensor")
    sensor.DOMAIN = "sensor"
    sensor.SensorEntity = FakeSensorEntity
    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    const = ModuleType("homeassistant.const")
    const.MATCH_ALL = "*"
    core = ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.State = FakeState
    core.callback = lambda function: function
    helpers = ModuleType("homeassistant.helpers")
    device_registry = ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = lambda **kwargs: kwargs
    event = ModuleType("homeassistant.helpers.event")
    event.async_track_time_interval = lambda *_args, **_kwargs: lambda: None
    util = ModuleType("homeassistant.util")
    dt = ModuleType("homeassistant.util.dt")
    dt.utcnow = lambda: datetime(2026, 9, 1, 10, 10, tzinfo=timezone.utc)
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
            "homeassistant.helpers.device_registry": device_registry,
            "homeassistant.helpers.event": event,
            "homeassistant.util": util,
            "homeassistant.util.dt": dt,
        }
    )

    component_const = ModuleType(f"{PACKAGE}.const")
    component_const.DOMAIN = "hoymiles_hit_modbus"
    component_const.NAME = "Hoymiles HIT Modbus"
    component_const.EMS_BASELINE_TIMELINE_ENTITY_ID = (
        "sensor.hoymiles_ems_baseline_energy_timeline"
    )
    component_const.EMS_BASELINE_TIMELINE_TRANSLATION_KEY = (
        "ems_baseline_energy_timeline"
    )
    component_models = ModuleType(f"{PACKAGE}.models")
    component_models.RuntimeData = object
    shared = ModuleType(f"{PACKAGE}.ems_shared_inputs")
    shared.EMSSharedInputsCoordinator = object
    shared.EMSSharedInputsSnapshot = object
    sys.modules[f"{PACKAGE}.const"] = component_const
    sys.modules[f"{PACKAGE}.models"] = component_models
    sys.modules[f"{PACKAGE}.ems_shared_inputs"] = shared


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_install_stubs()
BASE = _load(
    f"{PACKAGE}.baseline_energy_timeline",
    COMPONENT / "baseline_energy_timeline.py",
)
SENSOR = _load(
    f"{PACKAGE}.baseline_energy_timeline_sensor",
    COMPONENT / "baseline_energy_timeline_sensor.py",
)


NOW = datetime(2026, 9, 1, 10, 10, tzinfo=timezone.utc)
WARSAW = ZoneInfo("Europe/Warsaw")


def sample(
    value: float | None,
    entity_id: str | None,
    *,
    fresh: bool = True,
    provenance: str = "physical_fc03",
    source_entity_ids: tuple[str, ...] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        value=value,
        entity_id=entity_id,
        source_entity_ids=(
            source_entity_ids
            if source_entity_ids is not None
            else (entity_id,) if entity_id is not None else ()
        ),
        fresh=fresh,
        provenance=provenance,
    )


def forecast_state(local_date: object, base: float) -> FakeState:
    midnight_local = datetime.combine(
        local_date,
        datetime.min.time(),
        tzinfo=WARSAW,
    )
    details: list[dict[str, Any]] = []
    for index in range(48):
        start = midnight_local + timedelta(minutes=30 * index)
        details.append(
            {
                "period_start": start.astimezone(timezone.utc).isoformat(),
                "pv_estimate": base + index / 100.0,
            }
        )
    return FakeState("10.0", {"detailed_forecast": details})


def snapshot(*, soc: float | None = 55.0) -> SimpleNamespace:
    today = NOW.astimezone(WARSAW).date()
    return SimpleNamespace(
        config_entry_id="entry-a0",
        revision=9,
        system=SimpleNamespace(
            total_capacity_kwh=sample(20.0, "sensor.total_capacity"),
            battery_capacity_kwh=sample(10.0, "sensor.capacity"),
            battery_soc_percent=sample(
                soc,
                "sensor.soc" if soc is not None else None,
                fresh=soc is not None,
            ),
            self_use_reserve_soc_percent=sample(20.0, "sensor.reserve"),
            hardware_minimum_soc_percent=sample(10.0, "number.minimum_soc"),
            hardware_maximum_soc_percent=sample(100.0, "number.maximum_soc"),
            system_rated_power_kw=sample(
                10.0,
                "input_select.system_rated_power",
                provenance="rated_each_times_inverter_count",
            ),
        ),
        forecast=SimpleNamespace(
            today=sample(10.0, "sensor.forecast_today", provenance="new_helper"),
            tomorrow=sample(
                11.0,
                "sensor.forecast_tomorrow",
                provenance="new_helper",
            ),
        ),
        load=SimpleNamespace(
            average_profile_30m_kwh=tuple(0.5 + index / 1000 for index in range(48)),
            weekday_profile_30m_kwh=(),
            weekend_profile_30m_kwh=(),
            weekday_profile_days=0,
            weekend_profile_days=0,
            ready=True,
            source="recorder_load_model",
        ),
        bms=SimpleNamespace(
            maximum_charge_power_kw=sample(
                5.0,
                None,
                provenance="physical_bms_product",
                source_entity_ids=("sensor.battery_voltage", "sensor.charge_current"),
            ),
            maximum_discharge_power_kw=sample(
                5.0,
                None,
                provenance="physical_bms_product",
                source_entity_ids=(
                    "sensor.battery_voltage",
                    "sensor.discharge_current",
                ),
            ),
        ),
        efficiency=SimpleNamespace(
            pv_to_battery_efficiency=sample(
                95.0,
                "input_number.charge_efficiency",
                provenance="shared_helper",
            ),
            battery_to_home_efficiency=sample(
                90.0,
                "input_number.discharge_efficiency",
                provenance="shared_helper",
            ),
        ),
        gcf=SimpleNamespace(
            hardware_readback_supported=sample(
                1.0,
                "sensor.gcf_supported",
            ),
            generation=sample(7.0, "sensor.gcf_generation"),
            enable_code=sample(0.0, "sensor.gcf_enable"),
            maximum_export_power_percent=sample(100.0, "sensor.gcf_limit"),
            zero_export_confirmed=False,
            gcf_readback_ready=True,
            export_allowed=True,
        ),
        _today=today,
    )


def hass() -> SimpleNamespace:
    today = NOW.astimezone(WARSAW).date()
    states = FakeStates(
        {
            "sensor.forecast_today": forecast_state(today, 1.0),
            "sensor.forecast_tomorrow": forecast_state(
                today + timedelta(days=1),
                2.0,
            ),
        }
    )
    return SimpleNamespace(
        config=SimpleNamespace(time_zone="Europe/Warsaw"),
        states=states,
    )


def test_exact_identity_and_no_authority_registration() -> None:
    assert SENSOR.BASELINE_ENTITY_ID == "sensor.hoymiles_ems_baseline_energy_timeline"
    assert SENSOR.baseline_timeline_unique_id("abc") == (
        "abc_ems_baseline_energy_timeline"
    )
    platform = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    assert "HoymilesBaselineEnergyTimelineSensor" in platform
    init_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    assert "_async_prepare_baseline_entity_registry(hass, entry)" in init_source
    assert '_BASELINE_TRANSLATION_KEY = "ems_baseline_energy_timeline"' in init_source
    generator = (ROOT / "tools" / "build_hacs_assets.py").read_text(encoding="utf-8")
    assert generator.count('["ems_baseline_energy_timeline"]') == 2


def test_adapter_publishes_exact_provider_and_load_buckets() -> None:
    payload = SENSOR.build_payload_from_shared_inputs(
        hass(),
        snapshot(),
        generated_at=NOW,
    )
    # A rolling 48-hour horizon reaches into local day 3.  Only today's and
    # tomorrow's helpers are authoritative here, so the day-3 tail remains
    # explicitly partial instead of receiving invented PV.
    assert payload["schema_version"] == BASE.BASELINE_TIMELINE_SCHEMA_VERSION == "2.0"
    assert payload["state"] == "partial"
    assert payload["blocker_code"] == "series_partial"
    assert payload["point_count"] == 96
    assert payload["system_ac_power_kw"] == 10.0
    first = payload["points"][0]
    local = NOW.astimezone(WARSAW)
    index = local.hour * 2 + (1 if local.minute >= 30 else 0)
    assert first["start"] == NOW.isoformat()
    assert first["pv_kw"] == round(1.0 + index / 100.0, 4)
    assert first["load_kw"] == round((0.5 + index / 1000) * 2.0, 4)
    assert first["source"]["pv"] == "sensor.forecast_today"
    assert first["provenance"]["pv"] == "detailed_forecast_p50"
    expected_source_roles = {
        "pv",
        "load",
        "shared_inputs",
        "soc",
        "capacity",
        "reserve",
        "hardware_minimum_soc",
        "hardware_maximum_soc",
        "pv_to_battery_efficiency",
        "battery_to_home_efficiency",
        "bms_charge_limit",
        "bms_discharge_limit",
        "system_ac_power",
        "gcf_readback",
    }
    assert set(first["source"]) == expected_source_roles
    assert "sensor.battery_voltage" in first["source"]["bms_charge_limit"]
    assert "sensor.charge_current" in first["source"]["bms_charge_limit"]
    assert "sensor.discharge_current" in first["source"]["bms_discharge_limit"]
    assert "sensor.gcf_generation" in first["source"]["gcf_readback"]
    assert len(first["source"]) <= 16
    assert len(first["provenance"]) <= 16
    assert first["provenance"]["reserve"] == "physical_fc03"
    assert first["provenance"]["bms_charge_limit"] == "physical_bms_product"
    assert first["source"]["system_ac_power"] == (
        "input_select.system_rated_power"
    )
    assert first["provenance"]["system_ac_power"] == (
        "rated_each_times_inverter_count"
    )
    assert "physical_gcf_readback" in first["provenance"]["gcf_readback"]


def test_48h_horizon_uses_tomorrow_p50_without_fabricating_day_3() -> None:
    slots = SENSOR.build_slots_from_shared_inputs(
        hass(),
        snapshot(),
        generated_at=NOW,
    )
    today = NOW.astimezone(WARSAW).date()
    tomorrow = today + timedelta(days=1)
    day_3 = today + timedelta(days=2)

    assert len(slots) == 96

    tomorrow_slots = [
        slot for slot in slots if slot.start.astimezone(WARSAW).date() == tomorrow
    ]
    assert len(tomorrow_slots) == 48
    assert tomorrow_slots[0].start.astimezone(WARSAW).hour == 0
    assert tomorrow_slots[0].pv_kw == 2.0
    assert tomorrow_slots[0].pv_source == "sensor.forecast_tomorrow"
    assert tomorrow_slots[0].pv_provenance == "detailed_forecast_p50"
    assert abs(tomorrow_slots[-1].pv_kw - 2.47) < 1e-9

    day_3_slots = [
        slot for slot in slots if slot.start.astimezone(WARSAW).date() == day_3
    ]
    assert day_3_slots
    assert all(slot.pv_kw is None for slot in day_3_slots)
    assert all(slot.pv_source is None for slot in day_3_slots)
    assert all(slot.pv_provenance is None for slot in day_3_slots)
    assert all(slot.load_kw is not None for slot in day_3_slots)
    assert all(
        slot.load_provenance == "average_profile_30m_kwh"
        for slot in day_3_slots
    )


def test_adapter_missing_soc_keeps_pv_and_load_points() -> None:
    payload = SENSOR.build_payload_from_shared_inputs(
        hass(),
        snapshot(soc=None),
        generated_at=NOW,
    )
    assert payload["state"] == "partial"
    assert payload["current_actual_soc_percent"] is None
    assert any(point["pv_kw"] is not None for point in payload["points"])
    assert all(point["load_kw"] is not None for point in payload["points"])
    assert all(point["soc_end_percent"] is None for point in payload["points"])


def test_adapter_rejects_naive_detailed_timestamps_without_filling() -> None:
    fake_hass = hass()
    fake_hass.states["sensor.forecast_today"] = FakeState(
        "10.0",
        {
            "detailed_forecast": [
                {"period_start": "2026-09-01T12:00:00", "pv_estimate": 7.0}
            ]
        },
    )
    result = SENSOR.build_slots_from_shared_inputs(
        fake_hass,
        snapshot(),
        generated_at=NOW,
    )
    assert result[0].pv_kw is None
    assert result[0].load_kw is not None


def test_adapter_rejects_off_boundary_provider_row_without_shifting() -> None:
    fake_hass = hass()
    fake_hass.states["sensor.forecast_today"] = FakeState(
        "10.0",
        {
            "detailed_forecast": [
                {
                    "period_start": "2026-09-01T12:29:00+02:00",
                    "pv_estimate": 7.0,
                }
            ]
        },
    )
    result = SENSOR.build_slots_from_shared_inputs(
        fake_hass,
        snapshot(),
        generated_at=NOW,
    )
    assert result[0].pv_kw is None
    assert result[0].load_kw is not None


def test_unready_load_profile_degrades_only_load_and_dependent_series() -> None:
    current = snapshot()
    current.load.ready = False
    payload = SENSOR.build_payload_from_shared_inputs(
        hass(),
        current,
        generated_at=NOW,
    )
    assert any(point["pv_kw"] is not None for point in payload["points"])
    assert all(point["load_kw"] is None for point in payload["points"])
    assert all(point["soc_end_percent"] is None for point in payload["points"])


def test_efficiency_helper_percent_boundaries_are_always_divided_by_100() -> None:
    assert SENSOR._fresh_efficiency_fraction(sample(1.0, "input_number.one")) == 0.01
    assert SENSOR._fresh_efficiency_fraction(sample(2.0, "input_number.two")) == 0.02
    assert SENSOR._fresh_efficiency_fraction(sample(100.0, "input_number.full")) == 1.0


def test_standard_gcf_cohort_keeps_complete_enable_and_limit_sources() -> None:
    current = snapshot()
    source_ids = (
        "sensor.hoymiles_hit_ems_verified_hardware_readback_supported",
        "sensor.hoymiles_hit_gcf_control_readback_generation",
        "sensor.hoymiles_hit_gcf_enable_readback_code",
        "sensor.hoymiles_hit_gcf_maximum_export_power_readback",
    )
    current.gcf.hardware_readback_supported = sample(1.0, source_ids[0])
    current.gcf.generation = sample(7.0, source_ids[1])
    current.gcf.enable_code = sample(1.0, source_ids[2])
    current.gcf.maximum_export_power_percent = sample(0.0, source_ids[3])
    current.gcf.zero_export_confirmed = True
    current.gcf.export_allowed = False
    payload = SENSOR.build_payload_from_shared_inputs(
        hass(),
        current,
        generated_at=NOW,
    )
    expected = "|".join(source_ids)
    assert len(expected) <= 256
    assert payload["source"]["gcf_readback"] == expected
    assert payload["points"][0]["source"]["gcf_readback"] == expected
    assert source_ids[2] in expected and source_ids[3] in expected


def test_sensor_uses_entry_started_coordinator_and_rebuilds_immediately() -> None:
    class Coordinator:
        def __init__(self) -> None:
            self.snapshot = snapshot()

        def async_listen(self, _callback: Any) -> Any:
            return lambda: None

    coordinator = Coordinator()
    fake_hass = hass()
    entry = SimpleNamespace(entry_id="entry-a0")
    runtime = SimpleNamespace(source_device=SimpleNamespace())
    entity = SENSOR.HoymilesBaselineEnergyTimelineSensor(
        fake_hass,
        entry,
        runtime,
        coordinator,
    )
    asyncio.run(entity.async_added_to_hass())
    assert entity.extra_state_attributes["shared_inputs_revision"] == 9


def main() -> None:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"Baseline timeline sensor: {len(tests)} offline checks passed.")


if __name__ == "__main__":
    main()
