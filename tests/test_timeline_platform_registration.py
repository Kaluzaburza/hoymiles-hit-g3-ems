"""Real Home Assistant registration coverage for AP-1 timeline sensors."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from pathlib import Path
import shutil
from typing import Any

import pytest

from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.const import __version__ as HA_VERSION
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
from custom_components.hoymiles_hit_modbus import rce_sensor
from custom_components.hoymiles_hit_modbus import rcm_sensor
from custom_components.hoymiles_hit_modbus import sensor as sensor_platform
from custom_components.hoymiles_hit_modbus import tariff_sensor
from custom_components.hoymiles_hit_modbus.const import DOMAIN
from custom_components.hoymiles_hit_modbus.models import MatchedEntity, RuntimeData
from custom_components.hoymiles_hit_modbus.rce_sensor import (
    HoymilesRCEOptimizerSensor,
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
        assert timeline_rows[0].device_id == timeline_rows[1].device_id
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


async def _collision_scenario(root: Path) -> None:
    hass, entry, entity_registry, _ = await _new_hass(
        root / "scenario-collision",
        disable_new_entities=False,
    )
    rce_entity_id = TIMELINE_IDENTITIES["rce"][0]
    foreign = entity_registry.async_get_or_create(
        "sensor",
        "foreign_ap1e_platform",
        "foreign_rce_identity",
        suggested_object_id=rce_entity_id.split(".", 1)[1],
    )
    assert foreign.entity_id == rce_entity_id
    with pytest.raises(
        TimelineIdentityCollisionError,
        match="rce timeline entity ID is already in use",
    ):
        await _setup_platform(hass, entry)
    assert entity_registry.async_get(rce_entity_id) is foreign
    assert foreign.unique_id == "foreign_rce_identity"
    assert foreign.platform == "foreign_ap1e_platform"
    assert not any(
        row.entity_id.startswith(f"{rce_entity_id}_")
        for row in entity_registry.entities.values()
    )
    for entity_id, unique_id, _ in TIMELINE_IDENTITIES.values():
        assert not _rows_for_unique_id(entity_registry, unique_id)
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
        assert len(timelines) == 2
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


def test_timeline_platform_registration(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """Exercise enabled, disabled, stale and two-reload real-HA lifecycles."""

    assert HA_VERSION == "2026.8.2"
    optimizer_calls = {"rce": 0, "tariff": 0}
    schedule_calls: list[er.EntityRegistry] = []

    def unexpected_rce_call(*_args: Any, **_kwargs: Any) -> None:
        optimizer_calls["rce"] += 1
        raise AssertionError("timeline registration called optimize_rce")

    def unexpected_tariff_call(*_args: Any, **_kwargs: Any) -> None:
        optimizer_calls["tariff"] += 1
        raise AssertionError(
            "timeline registration called optimize_tariff_charging"
        )

    monkeypatch.setattr(rce_sensor, "optimize_rce", unexpected_rce_call)
    monkeypatch.setattr(
        tariff_sensor,
        "optimize_tariff_charging",
        unexpected_tariff_call,
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

    async def run_scenarios() -> None:
        await _fresh_enabled_scenario(tmp_path)
        await _prior_deleted_scenario(tmp_path, schedule_calls)
        await _active_legacy_scenario(tmp_path)
        await _disabled_scenario(tmp_path)
        await _stale_scenario(tmp_path)
        await _reload_scenario(tmp_path)
        await _collision_scenario(tmp_path)

    asyncio.run(run_scenarios())
    assert setup_calls == 8
    assert optimizer_calls == {"rce": 0, "tariff": 0}
