"""Real Home Assistant registration coverage for AP-1 timeline sensors."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
import shutil
from types import SimpleNamespace
import traceback
from typing import Any
from unittest.mock import patch

import pytest

from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.const import EVENT_STATE_CHANGED, __version__ as HA_VERSION
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers.entity_registry import RegistryEntryDisabler

from custom_components.hoymiles_hit_modbus import (
    TimelineIdentityCollisionError,
    _async_prepare_timeline_entity_registry,
    _async_reconcile_entity_registry,
)
from custom_components.hoymiles_hit_modbus import automation_plan_timeline as timeline_contract
from custom_components.hoymiles_hit_modbus import rce_sensor
from custom_components.hoymiles_hit_modbus import rcm_sensor
from custom_components.hoymiles_hit_modbus import sensor as sensor_platform
from custom_components.hoymiles_hit_modbus import tariff_sensor
from custom_components.hoymiles_hit_modbus.const import DOMAIN
from custom_components.hoymiles_hit_modbus.models import MatchedEntity, RuntimeData
from custom_components.hoymiles_hit_modbus.automation_plan_timeline import (
    OptimizerTimelineTrace,
    RCEPolicyPoint,
    RCMPolicyPoint,
    TariffPolicyPoint,
    TimelineTracePoint,
    TimelineValidationError,
    validate_payload,
)
from custom_components.hoymiles_hit_modbus.rce_sensor import (
    HoymilesRCEOptimizerSensor,
)
from custom_components.hoymiles_hit_modbus.rcm_sensor import (
    HoymilesRCMOptimizerSensor,
)
from custom_components.hoymiles_hit_modbus.tariff_sensor import (
    HoymilesTariffOptimizerSensor,
)
from custom_components.hoymiles_hit_modbus.timeline_sensor import (
    HoymilesAutomationPlanTimelineSensor,
)


ENTRY_ID = "94396617d70b4001af95bed64904c802"
TIMELINE_IDENTITIES = {
    "rce": (
        "sensor.hoymiles_hit_rce_automation_plan_timeline",
        f"{ENTRY_ID}_rce_automation_plan_timeline",
        "rce_automation_plan_timeline",
    ),
    "tariff": (
        "sensor.hoymiles_hit_tariff_automation_plan_timeline",
        f"{ENTRY_ID}_tariff_automation_plan_timeline",
        "tariff_automation_plan_timeline",
    ),
    "rcm": (
        "sensor.hoymiles_hit_rcm_automation_plan_timeline",
        f"{ENTRY_ID}_rcm_automation_plan_timeline",
        "rcm_automation_plan_timeline",
    ),
}
LEGACY_ENTITY_IDS = {
    policy_id: entity_id.replace(
        "sensor.hoymiles_hit_",
        "sensor.hoymiles_inverter_hoymiles_hit_",
    )
    for policy_id, (entity_id, _, _) in TIMELINE_IDENTITIES.items()
}
ACTIVE_NATIVE_IDS = (
    "sensor.hoymiles_hit_ems_supervisor",
    "sensor.hoymiles_hit_rce_optimized_plan",
    "sensor.hoymiles_hit_tariff_charge_plan",
    "sensor.hoymiles_hit_rcm_voltage_plan",
    "sensor.hoymiles_hit_setup_status",
)
ACTIVE_PROXY_ID = "sensor.hoymiles_hit_backup_voltage_l1"
AP2R1J_RCE_PENDING_REVISION = 21
AP2R1J_TARIFF_PENDING_REVISION = 11
AP2R1J_RCM_PENDING_REVISION = 24
AP2R1J_TRACE_START = datetime(2026, 8, 26, 20, 0, tzinfo=timezone.utc)


def _ap2r1j_trace(policy_id: str) -> OptimizerTimelineTrace:
    """Return one deterministic production-shaped optimizer trace."""

    if policy_id == "rce":
        minutes = 30
        point = TimelineTracePoint(
            start=AP2R1J_TRACE_START,
            end=AP2R1J_TRACE_START + timedelta(minutes=minutes),
            pv_kwh=0.0,
            load_kwh=0.0,
            battery_delta_kwh=-1.6129032258064515,
            grid_import_kwh=0.0,
            grid_export_kwh=1.5,
            soc_percent=53.0,
            baseline_soc_percent=53.0,
            protected_soc_floor_percent=20.0,
            action_code="export",
            selected=True,
            quality="complete",
            policy=RCEPolicyPoint(
                sell_price_pln_kwh=0.8,
                planned_export_kwh=1.5,
                target_discharge_kw=3.0,
                target_tolerance_kw=0.05,
                expected_revenue_pln=1.2,
            ),
        )
    elif policy_id == "tariff":
        minutes = 30
        point = TimelineTracePoint(
            start=AP2R1J_TRACE_START,
            end=AP2R1J_TRACE_START + timedelta(minutes=minutes),
            pv_kwh=0.0,
            load_kwh=0.38461538461538464,
            battery_delta_kwh=-0.4048582995951415,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=61.0,
            baseline_soc_percent=61.0,
            protected_soc_floor_percent=20.0,
            action_code="idle",
            selected=False,
            quality="complete",
            policy=TariffPolicyPoint(
                buy_price_pln_kwh=0.6,
                tariff_zone="low",
                planned_import_kwh=0.0,
                planned_charge_kw=0.0,
                expected_cost_pln=0.0,
                expected_saving_pln=None,
            ),
        )
    else:
        assert policy_id == "rcm"
        minutes = 15
        point = TimelineTracePoint(
            start=AP2R1J_TRACE_START,
            end=AP2R1J_TRACE_START + timedelta(minutes=minutes),
            pv_kwh=0.25,
            load_kwh=0.375,
            battery_delta_kwh=-0.125,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=60.0,
            baseline_soc_percent=60.0,
            protected_soc_floor_percent=20.0,
            action_code="monitor",
            selected=False,
            quality="complete",
            policy=RCMPolicyPoint(
                voltage_risk_code="no_risk",
                planned_export_limit_percent=None,
                planned_pre_discharge_kw=None,
                headroom_shortfall_kwh=None,
                control_mode="monitor",
            ),
        )
    return OptimizerTimelineTrace(policy_id=policy_id, points=(point,))


AP2R1J_TRACES = {
    policy_id: _ap2r1j_trace(policy_id)
    for policy_id in TIMELINE_IDENTITIES
}


def _event_value(data: Any, name: str) -> Any:
    if isinstance(data, dict):
        return data.get(name)
    return getattr(data, name, None)


def _timeline_registry_events(events: list[Any]) -> list[Any]:
    relevant_ids = {
        *LEGACY_ENTITY_IDS.values(),
        *(identity[0] for identity in TIMELINE_IDENTITIES.values()),
    }
    return [
        data
        for data in events
        if _event_value(data, "entity_id") in relevant_ids
        or _event_value(data, "old_entity_id") in relevant_ids
    ]


def _assert_timeline_registry_events(
    events: list[Any],
    *,
    create_ids: set[str],
) -> None:
    timeline_events = _timeline_registry_events(events)
    created_ids = [
        _event_value(data, "entity_id")
        for data in timeline_events
        if _event_value(data, "action") == "create"
    ]
    post_add_renames = [
        data
        for data in timeline_events
        if _event_value(data, "action") == "update"
        and _event_value(data, "old_entity_id") is not None
    ]
    removals = [
        data
        for data in timeline_events
        if _event_value(data, "action") == "remove"
    ]
    assert sorted(created_ids) == sorted(create_ids)
    assert not post_add_renames
    assert not removals


def _listen_registry_events(hass: HomeAssistant) -> list[Any]:
    events: list[Any] = []
    hass.bus.async_listen(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        lambda event: events.append(event.data),
    )
    return events


def _entry(*, disable_new_entities: bool) -> ConfigEntry:
    return ConfigEntry(
        data={},
        discovery_keys={},
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        minor_version=1,
        options={},
        pref_disable_new_entities=disable_new_entities,
        pref_disable_polling=False,
        source="user",
        state=ConfigEntryState.LOADED,
        subentries_data={},
        title="Hoymiles HIT",
        unique_id="ap1e-real-platform",
        version=1,
    )


async def _new_hass(
    root: Path,
    *,
    disable_new_entities: bool,
) -> tuple[HomeAssistant, ConfigEntry, er.EntityRegistry, dr.DeviceRegistry]:
    config_dir = root / (
        "disabled" if disable_new_entities else "enabled"
    )
    component_source = (
        Path(__file__).resolve().parents[1]
        / "custom_components"
        / DOMAIN
    )
    shutil.copytree(
        component_source,
        config_dir / "custom_components" / DOMAIN,
    )
    hass = HomeAssistant(str(config_dir))
    hass.set_state(CoreState.running)
    hass.config.skip_pip = True
    dr.async_setup(hass)
    hass.data[er.DATA_REGISTRY] = er.EntityRegistry(hass)
    hass.config_entries = ConfigEntries(hass, {})
    entry = _entry(disable_new_entities=disable_new_entities)
    hass.config_entries._entries[entry.entry_id] = entry

    device_registry = dr.async_get(hass)
    entity_registry = er.async_get(hass)
    await device_registry.async_load()
    await entity_registry.async_load()
    source_device = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={("esphome", "ap1e-source-device")},
        manufacturer="Hoymiles",
        model="HIT xxL G3",
        name="Hoymiles Inverter",
        sw_version="1.5.7",
    )
    proxy_catalog = {
        "domain": "sensor",
        "source_component": "sensor",
        "translation_key": "backup_voltage_l1",
        "source_name": "Backup Voltage L1",
        "source_id": "backup_voltage_a_81",
        "source_object_id": "backup_voltage_l1",
        "entity_category": None,
        "name": {"en": "Backup Voltage L1", "pl": "Backup napięcie L1"},
        "description": {
            "en": "AP-1E active proxy registration probe.",
            "pl": "Sonda aktywnego proxy AP-1E.",
        },
        "options": [],
    }
    runtime = RuntimeData(
        source_device=source_device,
        entities={
            "sensor": [MatchedEntity(catalog=proxy_catalog, source=None)],
        },
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    return hass, entry, entity_registry, device_registry


async def _setup_platform(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> EntityPlatform:
    _async_prepare_timeline_entity_registry(hass, entry)
    platform = EntityPlatform(
        hass=hass,
        logger=logging.getLogger(f"{__name__}.entity_platform"),
        domain="sensor",
        platform_name=DOMAIN,
        platform=sensor_platform,
        scan_interval=timedelta(seconds=30),
        entity_namespace=None,
    )
    platform.config_entry = entry
    await platform.async_setup_entry(entry)
    await hass.async_block_till_done()
    timelines = (
        {}
        if entry.pref_disable_new_entities
        else _timeline_entities(platform)
    )
    sources = {} if entry.pref_disable_new_entities else _source_entities(platform)
    before_reconciliation = {
        policy_id: (
            id(timeline),
            timeline.entity_id,
            timeline._platform_added,
            id(timeline._source_sensor),
            id(timeline._source_proxy),
            _rows_for_unique_id(
                er.async_get(hass),
                TIMELINE_IDENTITIES[policy_id][1],
            )[0].id,
        )
        for policy_id, timeline in timelines.items()
    }
    for policy_id, timeline in timelines.items():
        assert timeline._platform_added
        assert timeline.entity_id == TIMELINE_IDENTITIES[policy_id][0]
        assert timeline._source_sensor is sources[policy_id]
        assert sources[policy_id]._timeline_sensor is timeline._source_proxy
    _async_reconcile_entity_registry(
        hass,
        entry,
        hass.data[DOMAIN][entry.entry_id],
    )
    await hass.async_block_till_done()
    for policy_id, timeline in timelines.items():
        assert timeline._platform_added
        assert timeline._source_sensor is sources[policy_id]
        assert sources[policy_id]._timeline_sensor is timeline._source_proxy
        assert (
            id(timeline),
            timeline.entity_id,
            timeline._platform_added,
            id(timeline._source_sensor),
            id(timeline._source_proxy),
            _rows_for_unique_id(
                er.async_get(hass),
                TIMELINE_IDENTITIES[policy_id][1],
            )[0].id,
        ) == before_reconciliation[policy_id], (
            "reconciliation_changed_active_timeline",
            policy_id,
        )
    return platform


def _rows_for_unique_id(
    registry: er.EntityRegistry,
    unique_id: str,
) -> list[er.RegistryEntry]:
    return [
        row
        for row in registry.entities.values()
        if row.platform == DOMAIN and row.unique_id == unique_id
    ]


def _deleted_key(unique_id: str) -> tuple[str, str, str]:
    return ("sensor", DOMAIN, unique_id)


def _create_legacy_rows(
    registry: er.EntityRegistry,
    entry: ConfigEntry,
) -> dict[str, er.RegistryEntry]:
    rows: dict[str, er.RegistryEntry] = {}
    for policy_id, (_, unique_id, translation_key) in TIMELINE_IDENTITIES.items():
        row = registry.async_get_or_create(
            "sensor",
            DOMAIN,
            unique_id,
            config_entry=entry,
            suggested_object_id=LEGACY_ENTITY_IDS[policy_id].split(".", 1)[1],
            translation_key=translation_key,
        )
        assert row.entity_id == LEGACY_ENTITY_IDS[policy_id]
        row = registry.async_update_entity(
            row.entity_id,
            icon="mdi:history",
            name=f"Legacy {policy_id} timeline",
        )
        rows[policy_id] = row
    return rows


def _deleted_for_unique_ids(
    registry: er.EntityRegistry,
    unique_ids: set[str],
) -> list[Any]:
    return [
        row
        for row in registry.deleted_entities.values()
        if row.unique_id in unique_ids
    ]


def _timeline_entities(
    platform: EntityPlatform,
) -> dict[str, HoymilesAutomationPlanTimelineSensor]:
    timelines = {
        entity._policy_id: entity
        for entity in platform.entities.values()
        if isinstance(entity, HoymilesAutomationPlanTimelineSensor)
    }
    assert set(timelines) == set(TIMELINE_IDENTITIES)
    return timelines


def _source_entities(platform: EntityPlatform) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    for entity in platform.entities.values():
        if isinstance(entity, HoymilesRCEOptimizerSensor):
            sources["rce"] = entity
        elif isinstance(entity, HoymilesTariffOptimizerSensor):
            sources["tariff"] = entity
        elif isinstance(entity, HoymilesRCMOptimizerSensor):
            sources["rcm"] = entity
    assert set(sources) == set(TIMELINE_IDENTITIES)
    return sources


def _assert_exact_timelines(
    hass: HomeAssistant,
    entry: ConfigEntry,
    entity_registry: er.EntityRegistry,
    device_registry: dr.DeviceRegistry,
    platform: EntityPlatform,
    *,
    disabled: bool,
) -> None:
    timeline_unique_ids = {
        identity[1] for identity in TIMELINE_IDENTITIES.values()
    }
    timeline_rows: list[er.RegistryEntry] = []
    for policy_id, (entity_id, unique_id, translation_key) in (
        TIMELINE_IDENTITIES.items()
    ):
        rows = _rows_for_unique_id(entity_registry, unique_id)
        assert len(rows) == 1, policy_id
        row = rows[0]
        timeline_rows.append(row)
        assert row.entity_id == entity_id
        assert row.unique_id == unique_id
        assert row.config_entry_id == entry.entry_id
        assert row.platform == DOMAIN
        assert row.translation_key == translation_key
        assert row.disabled_by == (
            RegistryEntryDisabler.INTEGRATION if disabled else None
        )
        assert row.hidden_by is None
        if disabled:
            assert hass.states.get(entity_id) is None
        else:
            assert hass.states.get(entity_id) is not None
            assert row.device_id is not None
            target_device = device_registry.async_get(row.device_id)
            assert target_device is not None
            assert (DOMAIN, ENTRY_ID) in target_device.identifiers
        assert not any(
            candidate.entity_id.startswith(f"{entity_id}_")
            for candidate in entity_registry.entities.values()
        )

    if not disabled:
        assert len({row.device_id for row in timeline_rows}) == 1
        timelines = _timeline_entities(platform)
        assert all(timeline._platform_added for timeline in timelines.values())
        for entity_id in (*ACTIVE_NATIVE_IDS, ACTIVE_PROXY_ID):
            assert entity_registry.async_get(entity_id) is not None
    assert not _deleted_for_unique_ids(entity_registry, timeline_unique_ids)
    assert entry.state is ConfigEntryState.LOADED


async def _shutdown(
    hass: HomeAssistant,
    platform: EntityPlatform,
) -> None:
    await platform.async_reset()
    await hass.async_block_till_done()
    await hass.async_stop(force=True)


async def _fresh_enabled_scenario(root: Path) -> None:
    hass, entry, entity_registry, device_registry = await _new_hass(
        root / "scenario-enabled",
        disable_new_entities=False,
    )
    events = _listen_registry_events(hass)
    platform = await _setup_platform(hass, entry)
    _assert_exact_timelines(
        hass,
        entry,
        entity_registry,
        device_registry,
        platform,
        disabled=False,
    )
    await hass.async_block_till_done()
    _assert_timeline_registry_events(
        events,
        create_ids={identity[0] for identity in TIMELINE_IDENTITIES.values()},
    )
    for legacy_entity_id in LEGACY_ENTITY_IDS.values():
        assert entity_registry.async_get(legacy_entity_id) is None
        assert hass.states.get(legacy_entity_id) is None
    await _shutdown(hass, platform)


async def _stale_scenario(root: Path) -> None:
    hass, entry, entity_registry, device_registry = await _new_hass(
        root / "scenario-stale",
        disable_new_entities=False,
    )
    events = _listen_registry_events(hass)
    platform = await _setup_platform(hass, entry)

    stale = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{ENTRY_ID}_inactive_ap1e_probe",
        config_entry=entry,
        suggested_object_id="hoymiles_hit_inactive_ap1e_probe",
        translation_key="inactive_ap1e_probe",
    )
    assert entity_registry.async_get(stale.entity_id) is stale
    _async_reconcile_entity_registry(
        hass,
        entry,
        hass.data[DOMAIN][entry.entry_id],
    )
    await hass.async_block_till_done()
    assert entity_registry.async_get(stale.entity_id) is None
    _assert_exact_timelines(
        hass,
        entry,
        entity_registry,
        device_registry,
        platform,
        disabled=False,
    )
    for entity_id in (*ACTIVE_NATIVE_IDS, ACTIVE_PROXY_ID):
        assert entity_registry.async_get(entity_id) is not None
    _assert_timeline_registry_events(
        events,
        create_ids={identity[0] for identity in TIMELINE_IDENTITIES.values()},
    )
    await _shutdown(hass, platform)


async def _prior_deleted_scenario(
    root: Path,
    schedule_calls: list[er.EntityRegistry],
) -> None:
    hass, entry, entity_registry, device_registry = await _new_hass(
        root / "scenario-prior-deleted",
        disable_new_entities=False,
    )
    events = _listen_registry_events(hass)
    legacy_rows = _create_legacy_rows(entity_registry, entry)
    foreign_deleted = entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{ENTRY_ID}_foreign_deleted_probe",
        config_entry=entry,
        suggested_object_id="hoymiles_hit_foreign_deleted_probe",
        translation_key="foreign_deleted_probe",
    )
    entity_registry.async_remove(foreign_deleted.entity_id)
    foreign_deleted_key = _deleted_key(foreign_deleted.unique_id)
    foreign_deleted_snapshot = entity_registry.deleted_entities[
        foreign_deleted_key
    ]
    deleted_before: dict[str, er.DeletedRegistryEntry] = {}
    for policy_id, row in legacy_rows.items():
        entity_registry.async_remove(row.entity_id)
        key = _deleted_key(TIMELINE_IDENTITIES[policy_id][1])
        deleted = entity_registry.deleted_entities[key]
        assert deleted.entity_id == LEGACY_ENTITY_IDS[policy_id]
        deleted_before[policy_id] = deleted

    calls_before = sum(call is entity_registry for call in schedule_calls)
    _async_prepare_timeline_entity_registry(hass, entry)
    calls_after_change = sum(call is entity_registry for call in schedule_calls)
    assert calls_after_change == calls_before + 1
    assert (
        entity_registry.deleted_entities[foreign_deleted_key]
        is foreign_deleted_snapshot
    )
    for policy_id, deleted in deleted_before.items():
        key = _deleted_key(TIMELINE_IDENTITIES[policy_id][1])
        normalized = entity_registry.deleted_entities[key]
        assert normalized == deleted.__replace__(
            entity_id=TIMELINE_IDENTITIES[policy_id][0]
        )
        assert normalized.id == deleted.id
        assert normalized.created_at == deleted.created_at
        assert normalized.modified_at == deleted.modified_at
        assert normalized.name == deleted.name
        assert normalized.icon == deleted.icon

    _async_prepare_timeline_entity_registry(hass, entry)
    assert sum(call is entity_registry for call in schedule_calls) == (
        calls_after_change
    )
    await hass.async_block_till_done()
    events.clear()
    platform = await _setup_platform(hass, entry)
    _assert_exact_timelines(
        hass,
        entry,
        entity_registry,
        device_registry,
        platform,
        disabled=False,
    )
    await hass.async_block_till_done()
    _assert_timeline_registry_events(
        events,
        create_ids={identity[0] for identity in TIMELINE_IDENTITIES.values()},
    )
    for policy_id, deleted in deleted_before.items():
        entity_id, unique_id, _ = TIMELINE_IDENTITIES[policy_id]
        assert _deleted_key(unique_id) not in entity_registry.deleted_entities
        row = entity_registry.async_get(entity_id)
        assert row is not None
        assert row.id == deleted.id
        assert row.name == deleted.name
        assert row.icon == deleted.icon
        assert entity_registry.async_get(LEGACY_ENTITY_IDS[policy_id]) is None
        assert hass.states.get(entity_id) is not None
    await _shutdown(hass, platform)


async def _active_legacy_scenario(root: Path) -> None:
    hass, entry, entity_registry, device_registry = await _new_hass(
        root / "scenario-active-legacy",
        disable_new_entities=False,
    )
    events = _listen_registry_events(hass)
    legacy_rows = _create_legacy_rows(entity_registry, entry)
    await hass.async_block_till_done()
    events.clear()
    _async_prepare_timeline_entity_registry(hass, entry)
    await hass.async_block_till_done()
    migration_events = _timeline_registry_events(events)
    assert {
        (
            _event_value(data, "old_entity_id"),
            _event_value(data, "entity_id"),
        )
        for data in migration_events
        if _event_value(data, "action") == "update"
        and _event_value(data, "old_entity_id") is not None
    } == {
        (LEGACY_ENTITY_IDS[policy_id], TIMELINE_IDENTITIES[policy_id][0])
        for policy_id in TIMELINE_IDENTITIES
    }
    events.clear()
    platform = await _setup_platform(hass, entry)
    _assert_exact_timelines(
        hass,
        entry,
        entity_registry,
        device_registry,
        platform,
        disabled=False,
    )
    await hass.async_block_till_done()
    _assert_timeline_registry_events(events, create_ids=set())
    for policy_id, legacy_row in legacy_rows.items():
        entity_id, unique_id, _ = TIMELINE_IDENTITIES[policy_id]
        row = entity_registry.async_get(entity_id)
        assert row is not None
        assert row.id == legacy_row.id
        assert row.unique_id == unique_id
        assert row.name == legacy_row.name
        assert row.icon == legacy_row.icon
        assert not _deleted_for_unique_ids(entity_registry, {unique_id})
    await _shutdown(hass, platform)


async def _collision_scenario(root: Path, policy_id: str) -> None:
    hass, entry, entity_registry, _ = await _new_hass(
        root / f"scenario-collision-{policy_id}",
        disable_new_entities=False,
    )
    legacy_rows = _create_legacy_rows(entity_registry, entry)
    collided_entity_id = TIMELINE_IDENTITIES[policy_id][0]
    foreign = entity_registry.async_get_or_create(
        "sensor",
        "foreign_ap1e_platform",
        f"foreign_{policy_id}_identity",
        suggested_object_id=collided_entity_id.split(".", 1)[1],
    )
    assert foreign.entity_id == collided_entity_id
    with pytest.raises(
        TimelineIdentityCollisionError,
        match=rf"{policy_id} timeline entity ID is already in use",
    ):
        await _setup_platform(hass, entry)
    assert entity_registry.async_get(collided_entity_id) is foreign
    assert foreign.unique_id == f"foreign_{policy_id}_identity"
    assert foreign.platform == "foreign_ap1e_platform"
    assert not any(
        row.entity_id.startswith(f"{collided_entity_id}_")
        for row in entity_registry.entities.values()
    )
    for candidate_policy, (entity_id, unique_id, _) in TIMELINE_IDENTITIES.items():
        rows = _rows_for_unique_id(entity_registry, unique_id)
        assert len(rows) == 1
        assert rows[0] is legacy_rows[candidate_policy]
        assert rows[0].entity_id == LEGACY_ENTITY_IDS[candidate_policy]
        assert hass.states.get(entity_id) is None
    await hass.async_stop(force=True)


async def _disabled_scenario(root: Path) -> None:
    hass, entry, entity_registry, device_registry = await _new_hass(
        root / "scenario-disabled",
        disable_new_entities=True,
    )
    events = _listen_registry_events(hass)
    platform = await _setup_platform(hass, entry)
    _assert_exact_timelines(
        hass,
        entry,
        entity_registry,
        device_registry,
        platform,
        disabled=True,
    )
    await hass.async_block_till_done()
    _assert_timeline_registry_events(
        events,
        create_ids={identity[0] for identity in TIMELINE_IDENTITIES.values()},
    )
    await _shutdown(hass, platform)


async def _reload_scenario(root: Path) -> None:
    hass, entry, entity_registry, device_registry = await _new_hass(
        root / "scenario-reload",
        disable_new_entities=False,
    )
    platform = await _setup_platform(hass, entry)
    old_proxies: list[Any] = []

    for reload_number in range(3):
        _assert_exact_timelines(
            hass,
            entry,
            entity_registry,
            device_registry,
            platform,
            disabled=False,
        )
        timelines = _timeline_entities(platform)
        sources = _source_entities(platform)
        assert len(timelines) == 3
        for policy_id in TIMELINE_IDENTITIES:
            timeline = timelines[policy_id]
            source = sources[policy_id]
            assert timeline._source_sensor is source
            assert source._timeline_sensor is timeline._source_proxy
            new_proxy = source._timeline_sensor
            revision_before_publish = timeline._plan_revision
            new_proxy.publish_unavailable(
                input_revision=1000 + reload_number,
                blocker_code=f"reload_probe_{reload_number}",
            )
            assert timeline._plan_revision > revision_before_publish
            state = hass.states.get(TIMELINE_IDENTITIES[policy_id][0])
            assert state is not None
            active_snapshot = (
                timeline._plan_revision,
                timeline.native_value,
                dict(timeline.extra_state_attributes),
            )
            for old_proxy in old_proxies:
                old_proxy.publish_unavailable(
                    input_revision=2000 + reload_number,
                    blocker_code="old_callback_must_be_inert",
                )
            assert (
                timeline._plan_revision,
                timeline.native_value,
                dict(timeline.extra_state_attributes),
            ) == active_snapshot

        if reload_number == 2:
            break
        current_timelines = tuple(timelines.values())
        current_sources = tuple(sources.values())
        old_proxies.extend(
            source._timeline_sensor for source in current_sources
        )
        await platform.async_reset()
        await hass.async_block_till_done()
        for timeline in current_timelines:
            assert timeline._source_sensor is None
        for source in current_sources:
            assert source._timeline_sensor is None
        platform = await _setup_platform(hass, entry)

    await _shutdown(hass, platform)


def _assert_malformed_policy_ids_rejected(payload: dict[str, Any]) -> None:
    """Reject copied policy IDs before downstream validation, without coercion."""

    original = deepcopy(payload)
    validate_payload(payload, expected_state="current")
    malformed_policy_ids = (
        [],
        {},
        None,
        0,
        1,
        1.5,
        True,
        False,
        "",
        "unknown",
        "RCE",
        ["rce"],
        {"value": "rce"},
    )
    with (
        patch.object(timeline_contract, "slot_minutes_for_policy") as slot_resolver,
        patch.object(timeline_contract, "_validate_points") as points_validator,
    ):
        for malformed in malformed_policy_ids:
            probe = deepcopy(payload)
            probe["policy_id"] = deepcopy(malformed)
            with pytest.raises(TimelineValidationError) as error:
                validate_payload(probe, expected_state="current")
            assert type(error.value) is TimelineValidationError
            assert str(error.value) == "unknown policy_id"
        assert len(malformed_policy_ids) == 13
        slot_resolver.assert_not_called()
        points_validator.assert_not_called()
    assert payload == original


def _assert_policy_battery_direction(
    payload: dict[str, Any],
) -> list[tuple[dict[str, Any], str]]:
    """Literal direction-only oracles, independent of the production helper."""

    original = deepcopy(payload)
    validate_payload(payload, expected_state="current")
    if payload["policy_id"] == "rcm":
        probe = deepcopy(payload)
        probe["points"][0]["load_kw"] += 1.0
        message = "power balance differs from canonical signs"
        with pytest.raises(TimelineValidationError) as error:
            validate_payload(probe, expected_state="current")
        assert type(error.value) is TimelineValidationError
        assert str(error.value) == message
        assert payload == original
        return [(probe, message)]

    discharge = "battery_kw direction conflicts with planned discharge"
    charge = "battery_kw direction conflicts with planned charge"
    # Each row is (group, planned kW, signed battery kW, exact error or None).
    cases = {
        "rce": (
            ("valid", 1.0, -1.0, None),
            ("contradictory", 1.0, 1.0, discharge),
            ("positive_zero", 1.0, 0.0, None),
            ("negative_zero", 1.0, -0.0, None),
            ("positive_boundary", 1.0, 1e-9, None),
            ("negative_boundary", 1.0, -1e-9, None),
            ("beyond_boundary", 1.0, 1e-9 + 1e-12, discharge),
            ("zero_plan_positive", 0.0, 1.0, None),
            ("zero_plan_negative", 0.0, -1.0, None),
            ("plan_boundary_positive", 1e-9, 1.0, None),
            ("plan_boundary_negative", 1e-9, -1.0, None),
            ("plan_beyond_boundary", 1e-9 + 1e-12, 1.0, discharge),
            ("null_battery", 1.0, None, None),
            ("null_plan_positive", None, 1.0, None),
            ("null_plan_negative", None, -1.0, None),
            ("small_magnitude", 10.0, -0.1, None),
            ("large_magnitude", 0.1, -10.0, None),
            ("non_first_point", 1.0, 1.0, discharge),
        ),
        "tariff": (
            ("valid", 0.95, 0.95, None),
            ("contradictory", 0.95, -0.95, charge),
            ("positive_zero", 0.95, 0.0, None),
            ("negative_zero", 0.95, -0.0, None),
            ("positive_boundary", 0.95, 1e-9, None),
            ("negative_boundary", 0.95, -1e-9, None),
            ("beyond_boundary", 0.95, -(1e-9 + 1e-12), charge),
            ("zero_plan_positive", 0.0, 1.0, None),
            ("zero_plan_negative", 0.0, -1.0, None),
            ("plan_boundary_positive", 1e-9, 1.0, None),
            ("plan_boundary_negative", 1e-9, -1.0, None),
            ("plan_beyond_boundary", 1e-9 + 1e-12, -0.95, charge),
            ("null_battery", 0.95, None, None),
            ("null_plan_positive", None, 1.0, None),
            ("null_plan_negative", None, -1.0, None),
            ("small_magnitude", 10.0, 0.1, None),
            ("large_magnitude", 0.1, 10.0, None),
            ("non_first_point", 0.95, -0.95, charge),
        ),
    }
    policy_id = payload["policy_id"]
    field = "target_discharge_kw" if policy_id == "rce" else "planned_charge_kw"
    rejected: list[tuple[dict[str, Any], str]] = []
    assert len(cases[policy_id]) == 18
    for group, planned_kw, battery_kw, message in cases[policy_id]:
        probe = deepcopy(payload)
        point = probe["points"][0]
        # No selected/action/active/grid precondition may hide a contradiction.
        point.update(selected=False, active=False, action_code="hold",
                     grid_kw=0.0, grid_import_kw=0.0, grid_export_kw=0.0)
        point["policy"][field] = planned_kw
        point["battery_kw"] = battery_kw
        if group == "non_first_point":
            second = deepcopy(point)
            point["battery_kw"] = -1.0 if policy_id == "rce" else 0.95
            second["start"] = point["end"]
            end = datetime.fromisoformat(second["start"].replace("Z", "+00:00"))
            second["end"] = timeline_contract.utc_iso(end + timedelta(minutes=30))
            probe["points"].append(second)
            probe["point_count"] = 2
            probe["horizon_end"] = second["end"]
        before = deepcopy(probe)
        if message is None:
            validate_payload(probe, expected_state="current")
        else:
            with pytest.raises(TimelineValidationError) as error:
                validate_payload(probe, expected_state="current")
            assert type(error.value) is TimelineValidationError, group
            assert str(error.value) == message, group
            rejected.append((probe, message))
        assert probe == before, group
    assert len(rejected) == 4
    assert payload == original
    return rejected


async def _convergence_scenario(
    root: Path,
    *,
    source_callback_counts: dict[str, int],
    proxy_current_counts: dict[str, int],
    current_publication_counts: dict[str, int],
    optimizer_calls: dict[str, int],
    fixture_results: dict[str, Any],
) -> dict[str, Any]:
    """Exercise the exact source-current-to-timeline publication order."""

    hass, entry, entity_registry, device_registry = await _new_hass(
        root / "scenario-ap2r1j-convergence",
        disable_new_entities=False,
    )
    platform = await _setup_platform(hass, entry)
    _assert_exact_timelines(
        hass,
        entry,
        entity_registry,
        device_registry,
        platform,
        disabled=False,
    )
    sources = _source_entities(platform)
    timelines = _timeline_entities(platform)
    state_machine_current_counts = {policy_id: 0 for policy_id in TIMELINE_IDENTITIES}

    for policy_id, source in sources.items():
        async def deterministic_result(
            *,
            fixture_policy_id: str = policy_id,
            fixture_source: Any = source,
        ) -> bool:
            optimizer_calls[fixture_policy_id] += 1
            trace = AP2R1J_TRACES[fixture_policy_id]
            if fixture_policy_id == "rcm":
                fixture_results[fixture_policy_id] = trace
                fixture_source._timeline_trace = trace
            else:
                result = SimpleNamespace(timeline_trace=trace)
                fixture_results[fixture_policy_id] = result
                fixture_source._result = result
            fixture_source._timeline_metadata = {}
            fixture_source._attributes = {
                **fixture_source._attributes,
                "status_code": "ready",
                "planned_slots": [],
            }
            return True

        source._recalculate_locked = deterministic_result

    def observe_state_changed(event: Any) -> None:
        new_state = event.data.get("new_state")
        if new_state is None or new_state.state != "current":
            return
        for policy_id, (entity_id, _, _) in TIMELINE_IDENTITIES.items():
            if new_state.entity_id == entity_id:
                state_machine_current_counts[policy_id] += 1

    hass.bus.async_listen(EVENT_STATE_CHANGED, observe_state_changed)
    expected_revisions = {
        "rce": AP2R1J_RCE_PENDING_REVISION,
        "tariff": AP2R1J_TARIFF_PENDING_REVISION,
        "rcm": AP2R1J_RCM_PENDING_REVISION,
    }
    results: dict[str, Any] = {}
    for policy_id in ("rce", "tariff", "rcm"):
        source = sources[policy_id]
        timeline = timelines[policy_id]
        expected_revision = expected_revisions[policy_id]
        assert source._input_revision.value <= expected_revision
        for _ in range(expected_revision - source._input_revision.value):
            source._input_revision.invalidate()
        source._mark_recalculation_pending()
        await hass.async_block_till_done()
        pending_state = hass.states.get(TIMELINE_IDENTITIES[policy_id][0])
        assert pending_state is not None
        pending_snapshot = {
            "state": pending_state.state,
            "pending_input_revision": pending_state.attributes.get(
                "pending_input_revision"
            ),
            "input_revision": pending_state.attributes.get("input_revision"),
            "blocker_code": pending_state.attributes.get("blocker_code"),
        }
        exception: str | None = None
        exception_traceback: str | None = None
        try:
            await source._recalculate_and_write()
        except TimelineValidationError as err:
            exception = f"{type(err).__name__}: {err}"
            exception_traceback = traceback.format_exc()
        await hass.async_block_till_done()
        source_state = hass.states.get(source.entity_id)
        timeline_state = hass.states.get(TIMELINE_IDENTITIES[policy_id][0])
        assert source_state is not None
        assert timeline_state is not None
        result_object = (
            source._timeline_trace
            if policy_id == "rcm"
            else source._result
        )
        results[policy_id] = {
            "exception": exception,
            "exception_traceback": exception_traceback,
            "pending": pending_snapshot,
            "source_internal_revision": source._input_revision.value,
            "source_public_state": source_state.state,
            "source_public_attributes": dict(source_state.attributes),
            "timeline_internal_state": timeline.native_value,
            "timeline_internal_attributes": deepcopy(
                timeline.extra_state_attributes
            ),
            "timeline_ha_state": timeline_state.state,
            "timeline_ha_attributes": dict(timeline_state.attributes),
            "source_callback_count": source_callback_counts[policy_id],
            "proxy_current_count": proxy_current_counts[policy_id],
            "current_publication_count": current_publication_counts[policy_id],
            "state_machine_current_count": state_machine_current_counts[policy_id],
            "optimizer_call_count": optimizer_calls[policy_id],
            "result_object_unchanged": result_object is fixture_results[policy_id],
            "trace_unchanged": (
                source._timeline_trace == AP2R1J_TRACES[policy_id]
                if policy_id == "rcm"
                else source._result.timeline_trace == AP2R1J_TRACES[policy_id]
            ),
        }

    for policy_id in ("rce", "tariff", "rcm"):
        timeline = timelines[policy_id]
        entity_id = TIMELINE_IDENTITIES[policy_id][0]
        state_before = hass.states.get(entity_id)
        attributes_before = deepcopy(timeline.extra_state_attributes)
        _assert_malformed_policy_ids_rejected(
            deepcopy(results[policy_id]["timeline_internal_attributes"])
        )
        revision_before = timeline._plan_revision
        result_before = fixture_results[policy_id]
        result_value_before = deepcopy(result_before)
        registry_row_before = entity_registry.async_get(entity_id)
        for rejected, message in _assert_policy_battery_direction(attributes_before):
            with pytest.raises(TimelineValidationError) as error:
                timeline._publish_candidate("current", deepcopy(rejected))
            assert type(error.value) is TimelineValidationError
            assert str(error.value) == message
            assert timeline._plan_revision == revision_before
            assert timeline.extra_state_attributes == attributes_before
            assert hass.states.get(entity_id) is state_before
        for refresh in ("timestamp", "age"):
            candidate = deepcopy(attributes_before)
            if refresh == "timestamp":
                generated = datetime.fromisoformat(candidate["generated_at"].replace("Z", "+00:00"))
                candidate["generated_at"] = timeline_contract.utc_iso(generated + timedelta(seconds=1))
            else:
                candidate["current_actual"]["source_ages_seconds"]["battery"] = 1.0
            assert candidate != attributes_before
            assert timeline_contract.semantic_fingerprint(candidate) == timeline_contract.semantic_fingerprint(attributes_before)
            assert timeline._publish_candidate("current", candidate) is False
        await hass.async_block_till_done()
        assert timeline._plan_revision == revision_before
        assert _timeline_entities(platform)[policy_id] is timeline
        assert _source_entities(platform)[policy_id] is sources[policy_id]
        assert entity_registry.async_get(entity_id) is registry_row_before
        result_after = sources[policy_id]._timeline_trace if policy_id == "rcm" else sources[policy_id]._result
        assert result_after is result_before
        assert result_after == result_value_before
        assert optimizer_calls[policy_id] == 1
        assert source_callback_counts[policy_id] == 1
        assert proxy_current_counts[policy_id] == 1
        assert timeline.native_value == "current"
        assert timeline.extra_state_attributes == attributes_before
        assert hass.states.get(entity_id) is state_before
        assert current_publication_counts[policy_id] == 1
        assert state_machine_current_counts[policy_id] == 1

    _assert_exact_timelines(
        hass, entry, entity_registry, device_registry, platform, disabled=False
    )
    rcm_payload = deepcopy(results["rcm"]["timeline_internal_attributes"])
    rcm_payload["points"][0]["load_kw"] += 1.0
    invalid_rcm_balance_rejected = False
    invalid_rcm_exception = None
    try:
        validate_payload(rcm_payload, expected_state="current")
    except TimelineValidationError as err:
        invalid_rcm_balance_rejected = True
        invalid_rcm_exception = f"{type(err).__name__}: {err}"
    results["rcm_invalid_balance"] = {
        "rejected": invalid_rcm_balance_rejected,
        "exception": invalid_rcm_exception,
    }
    await _shutdown(hass, platform)
    return results


def _assert_policy_convergence(
    result: dict[str, Any],
    *,
    expected_revision: int,
) -> None:
    """Require one exact source/proxy/timeline/State-Machine convergence."""

    assert result["pending"] == {
        "state": "pending",
        "pending_input_revision": expected_revision,
        "input_revision": 0,
        "blocker_code": "recalculation_pending",
    }
    assert result["source_internal_revision"] == expected_revision
    assert result["source_public_attributes"]["result_current"] is True
    assert result["source_public_attributes"]["recalculation_pending"] is False
    assert result["source_public_attributes"]["input_revision"] == expected_revision
    assert result["source_callback_count"] == 1
    assert result["proxy_current_count"] == 1
    assert result["exception"] is None
    assert result["timeline_internal_state"] == "current"
    assert result["timeline_internal_attributes"]["input_revision"] == expected_revision
    assert "pending_input_revision" not in result["timeline_internal_attributes"]
    assert result["timeline_ha_state"] == "current"
    assert result["timeline_ha_attributes"]["input_revision"] == expected_revision
    assert all(
        result["timeline_ha_attributes"].get(key) == value
        for key, value in result["timeline_internal_attributes"].items()
    )
    assert set(result["timeline_ha_attributes"]) - set(
        result["timeline_internal_attributes"]
    ) == {"friendly_name", "icon"}
    assert result["timeline_internal_attributes"]["point_count"] > 0
    assert result["timeline_internal_attributes"]["points"]
    assert result["timeline_internal_attributes"]["blocker_code"] != "recalculation_pending"
    assert result["timeline_internal_attributes"]["quality"] == "complete"
    assert result["current_publication_count"] == 1
    assert result["state_machine_current_count"] == 1
    assert result["optimizer_call_count"] == 1
    assert result["result_object_unchanged"]
    assert result["trace_unchanged"]


def test_timeline_platform_registration(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Exercise enabled, disabled, stale and two-reload real-HA lifecycles."""

    assert HA_VERSION == "2026.8.2"
    unexpected_optimizer_calls = {"rce": 0, "tariff": 0, "rcm": 0}
    optimizer_calls = {"rce": 0, "tariff": 0, "rcm": 0}
    source_callback_counts = {"rce": 0, "tariff": 0, "rcm": 0}
    proxy_current_counts = {"rce": 0, "tariff": 0, "rcm": 0}
    current_publication_counts = {"rce": 0, "tariff": 0, "rcm": 0}
    fixture_results: dict[str, Any] = {}
    schedule_calls: list[er.EntityRegistry] = []

    def unexpected_rce_call(*_args: Any, **_kwargs: Any) -> None:
        unexpected_optimizer_calls["rce"] += 1
        raise AssertionError("timeline registration called optimize_rce")

    def unexpected_tariff_call(*_args: Any, **_kwargs: Any) -> None:
        unexpected_optimizer_calls["tariff"] += 1
        raise AssertionError(
            "timeline registration called optimize_tariff_charging"
        )

    def unexpected_rcm_call(*_args: Any, **_kwargs: Any) -> None:
        unexpected_optimizer_calls["rcm"] += 1
        raise AssertionError("timeline registration called optimize_rcm")

    real_rce_publish = HoymilesRCEOptimizerSensor._publish_timeline_result
    real_tariff_publish = HoymilesTariffOptimizerSensor._publish_timeline_result
    real_rcm_publish = HoymilesRCMOptimizerSensor._publish_timeline_result

    def observed_rce_publish(source: HoymilesRCEOptimizerSensor) -> None:
        source_callback_counts["rce"] += 1
        real_rce_publish(source)

    def observed_tariff_publish(source: HoymilesTariffOptimizerSensor) -> None:
        source_callback_counts["tariff"] += 1
        real_tariff_publish(source)

    def observed_rcm_publish(source: HoymilesRCMOptimizerSensor) -> None:
        source_callback_counts["rcm"] += 1
        real_rcm_publish(source)

    real_timeline_publish_current = (
        HoymilesAutomationPlanTimelineSensor.publish_current
    )
    real_timeline_publish = HoymilesAutomationPlanTimelineSensor._publish

    def observed_timeline_publish_current(
        timeline: HoymilesAutomationPlanTimelineSensor,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        proxy_current_counts[timeline._policy_id] += 1
        real_timeline_publish_current(timeline, *args, **kwargs)

    def observed_timeline_publish(
        timeline: HoymilesAutomationPlanTimelineSensor,
        state: str,
        attributes: dict[str, Any],
    ) -> None:
        if state == "current":
            current_publication_counts[timeline._policy_id] += 1
        real_timeline_publish(timeline, state, attributes)

    monkeypatch.setattr(rce_sensor, "optimize_rce", unexpected_rce_call)
    monkeypatch.setattr(
        tariff_sensor,
        "optimize_tariff_charging",
        unexpected_tariff_call,
    )
    monkeypatch.setattr(rcm_sensor, "optimize_rcm", unexpected_rcm_call)
    monkeypatch.setattr(
        HoymilesRCEOptimizerSensor,
        "_publish_timeline_result",
        observed_rce_publish,
    )
    monkeypatch.setattr(
        HoymilesTariffOptimizerSensor,
        "_publish_timeline_result",
        observed_tariff_publish,
    )
    monkeypatch.setattr(
        HoymilesRCMOptimizerSensor,
        "_publish_timeline_result",
        observed_rcm_publish,
    )
    monkeypatch.setattr(
        HoymilesAutomationPlanTimelineSensor,
        "publish_current",
        observed_timeline_publish_current,
    )
    monkeypatch.setattr(
        HoymilesAutomationPlanTimelineSensor,
        "_publish",
        observed_timeline_publish,
    )
    monkeypatch.setattr(
        rce_sensor.HoymilesRCEOptimizerSensor,
        "_schedule_startup_warmup",
        lambda self: None,
    )
    monkeypatch.setattr(
        tariff_sensor.HoymilesTariffOptimizerSensor,
        "_schedule_startup_warmup",
        lambda self: None,
    )
    monkeypatch.setattr(
        rcm_sensor.HoymilesRCMOptimizerSensor,
        "_schedule_startup_warmup",
        lambda self: None,
    )
    real_schedule_save: Callable[..., Any] = (
        er.EntityRegistry.async_schedule_save
    )

    def observed_schedule_save(registry: er.EntityRegistry) -> None:
        schedule_calls.append(registry)
        real_schedule_save(registry)

    monkeypatch.setattr(
        er.EntityRegistry,
        "async_schedule_save",
        observed_schedule_save,
    )
    setup_calls = 0
    real_setup_entry: Callable[..., Any] = sensor_platform.async_setup_entry

    async def observed_setup_entry(*args: Any, **kwargs: Any) -> None:
        nonlocal setup_calls
        setup_calls += 1
        await real_setup_entry(*args, **kwargs)

    monkeypatch.setattr(
        sensor_platform,
        "async_setup_entry",
        observed_setup_entry,
    )

    convergence: dict[str, Any] = {}

    async def run_scenarios() -> None:
        nonlocal convergence
        await _fresh_enabled_scenario(tmp_path)
        convergence = await _convergence_scenario(
            tmp_path,
            source_callback_counts=source_callback_counts,
            proxy_current_counts=proxy_current_counts,
            current_publication_counts=current_publication_counts,
            optimizer_calls=optimizer_calls,
            fixture_results=fixture_results,
        )
        await _prior_deleted_scenario(tmp_path, schedule_calls)
        await _active_legacy_scenario(tmp_path)
        await _disabled_scenario(tmp_path)
        await _stale_scenario(tmp_path)
        await _reload_scenario(tmp_path)
        for policy_id in TIMELINE_IDENTITIES:
            await _collision_scenario(tmp_path, policy_id)

    asyncio.run(run_scenarios())
    callback_exceptions = {
        policy_id: result["exception_traceback"]
        for policy_id, result in convergence.items()
        if policy_id in {"rce", "tariff"} and result["exception"] is not None
    }
    assert callback_exceptions == {}, (
        "source_current_callback_exception_after_source_commit\n"
        + "\n".join(
            f"{policy_id}:\n{failure}"
            for policy_id, failure in callback_exceptions.items()
        )
    )
    _assert_policy_convergence(
        convergence["rce"],
        expected_revision=AP2R1J_RCE_PENDING_REVISION,
    )
    _assert_policy_convergence(
        convergence["tariff"],
        expected_revision=AP2R1J_TARIFF_PENDING_REVISION,
    )
    _assert_policy_convergence(
        convergence["rcm"],
        expected_revision=AP2R1J_RCM_PENDING_REVISION,
    )
    assert convergence["rcm_invalid_balance"] == {
        "rejected": True,
        "exception": (
            "TimelineValidationError: "
            "power balance differs from canonical signs"
        ),
    }
    assert setup_calls == 9
    assert unexpected_optimizer_calls == {"rce": 0, "tariff": 0, "rcm": 0}
    assert optimizer_calls == {"rce": 1, "tariff": 1, "rcm": 1}
