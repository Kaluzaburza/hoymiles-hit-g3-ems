"""Real HA 2026.9.2 Store/registry/sensor tests; external HTTP is simulated."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import importlib
import json
from pathlib import Path
import sys
from types import MappingProxyType, ModuleType
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry, ConfigEntries
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import device_registry as dr

from test_rce_price_cache import NOW, DAY, rows_for

# Load the actual provider without starting the unrelated inverter integration.
ROOT = Path(__file__).resolve().parents[1]
package = ModuleType("rce_price_runtime_test")
package.__path__ = [str(ROOT / "custom_components/hoymiles_hit_modbus")]
sys.modules[package.__name__] = package
provider = importlib.import_module(package.__name__ + ".rce_price_sensor")


async def make_hass(tmp_path):
    hass = HomeAssistant(str(tmp_path))
    hass.config_entries = ConfigEntries(hass, {})
    dr.async_setup(hass)
    await dr.async_load(hass)
    await er.async_load(hass)
    return hass


def make_entry(hass, suffix="one"):
    entry = ConfigEntry(data={}, discovery_keys=MappingProxyType({}),
                        domain=provider.DOMAIN, minor_version=1, options={},
                        source="user", subentries_data=None, title=suffix,
                        unique_id=suffix, version=1)
    # Register the fixture without setting up ESPHome or physical devices.
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


@pytest.mark.asyncio
async def test_store_restart_dns_rotation_and_call_budget(tmp_path):
    hass = await make_hass(tmp_path)
    clock = NOW
    with patch.object(provider.dt_util, "utcnow", side_effect=lambda: clock):
        source = provider.RCEPriceCoordinator(hass)
        await source.async_initialize()
        source._fetch = AsyncMock(side_effect=lambda day: rows_for(day))
        await source.async_refresh()
        assert source._fetch.await_count == 2
        saved = source.cache.dump()
        for _ in range(120):
            clock += timedelta(seconds=30)
            await source.async_refresh()
        assert source._fetch.await_count == 2  # no 15-minute poll after success
        restarted = provider.RCEPriceCoordinator(hass)
        await restarted.async_initialize()
        assert restarted.cache.dump() == saved
        restarted._fetch = AsyncMock(side_effect=TimeoutError)
        await restarted.async_refresh()
        assert restarted._fetch.await_count == 0
        clock = NOW.replace(hour=0) + timedelta(days=1)
        await restarted.async_refresh()
        assert restarted._fetch.await_count == 2  # one correction, one missing day
        assert set(restarted.cache.days) == {(DAY + timedelta(days=1)).isoformat()}
        assert restarted.cache.get(DAY + timedelta(days=1), clock) is not None
        disk = json.loads(Path(restarted.store.path).read_text())
        assert set(disk["data"]["days"]) == set(restarted.cache.days)
        again = provider.RCEPriceCoordinator(hass)
        await again.async_initialize()
        again._fetch = AsyncMock(side_effect=TimeoutError)
        await again.async_refresh()
        assert again._fetch.await_count == 1  # no repeat correction after restart
        clock += timedelta(minutes=1)
        await again.async_refresh()
        assert again._fetch.await_count == 1
        clock += timedelta(minutes=14)
        await again.async_refresh()
        assert again._fetch.await_count == 2
        clock += timedelta(minutes=29)
        await again.async_refresh()
        assert again._fetch.await_count == 2
        clock += timedelta(minutes=1)
        await again.async_refresh()
        assert again._fetch.await_count == 3
        await source.async_stop()
        await restarted.async_stop()
        await again.async_stop()


@pytest.mark.asyncio
async def test_migration_exact_ids_customizations_and_single_owner(tmp_path):
    hass = await make_hass(tmp_path)
    registry = er.async_get(hass)
    entry = make_entry(hass)
    for object_id in provider.PRICE_ENTITIES:
        old = registry.async_get_or_create("sensor", "rest", object_id,
                                          suggested_object_id=object_id,
                                          get_initial_options=lambda: {"sensor": {"display_precision": 0}})
        registry.async_update_entity(old.entity_id, name="Custom price", icon="mdi:cash",
                                     disabled_by=er.RegistryEntryDisabler.USER)
    unrelated = registry.async_get_or_create("sensor", "rest", "keep_me", suggested_object_id="keep_me")
    assert provider.prepare_price_entities(hass, entry)
    assert provider.prepare_price_entities(hass, entry)  # idempotent reload
    assert not provider.prepare_price_entities(hass, make_entry(hass, "two"))
    assert len(registry.entities) == 3
    assert registry.async_get(unrelated.entity_id) == unrelated
    for object_id in provider.PRICE_ENTITIES:
        item = registry.async_get("sensor." + object_id)
        assert item.platform == provider.DOMAIN and item.config_entry_id == entry.entry_id
        assert item.name == "Custom price" and item.icon == "mdi:cash"
        assert item.disabled_by == er.RegistryEntryDisabler.USER
        assert item.options["sensor"]["display_precision"] == 0


@pytest.mark.asyncio
async def test_migration_unknown_collision_and_running_rest_are_atomic(tmp_path):
    hass = await make_hass(tmp_path)
    registry = er.async_get(hass)
    entry = make_entry(hass)
    first, second = provider.PRICE_ENTITIES
    registry.async_get_or_create("sensor", "rest", first, suggested_object_id=first)
    collision = registry.async_get_or_create("sensor", "mqtt", "operator_price", suggested_object_id=second)
    before = dict(registry.entities)
    with pytest.raises(ValueError, match="collision"):
        provider.prepare_price_entities(hass, entry)
    assert dict(registry.entities) == before
    registry.async_remove(collision.entity_id)
    registry.async_get_or_create("sensor", "rest", second, suggested_object_id=second)
    hass.set_state(CoreState.running)
    with pytest.raises(ValueError, match="restart required"):
        provider.prepare_price_entities(hass, entry)
    assert registry.async_get("sensor." + first).platform == "rest"


@pytest.mark.asyncio
async def test_single_task_and_unload_cancels_network(tmp_path):
    hass = await make_hass(tmp_path)
    source = provider.RCEPriceCoordinator(hass)
    started = asyncio.Event()
    completed = asyncio.Event()

    async def slow_fetch(day):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            completed.set()
    source._fetch = AsyncMock(side_effect=slow_fetch)
    source.start()
    await started.wait()
    for _ in range(5):
        source._schedule()
    assert source._fetch.await_count == 1
    await source.async_stop()
    assert completed.is_set() and source._task.done() and source._cancel_timer is None
    source._schedule()
    assert source._fetch.await_count == 1


@pytest.mark.asyncio
async def test_sensor_state_keeps_provenance_and_expires_by_date(tmp_path):
    hass = await make_hass(tmp_path)
    source = provider.RCEPriceCoordinator(hass)
    clock = NOW
    with patch.object(provider.dt_util, "utcnow", side_effect=lambda: clock):
        source.cache.accept(DAY, rows_for(), clock)
        sensor = provider.HoymilesRCEPriceSensor(source, "one", 0)
        assert sensor.available and sensor.native_value == 96
        fetched = sensor.extra_state_attributes["fetched_at"]
        clock += timedelta(hours=4)
        source.errors[DAY.isoformat()] = "TimeoutError"
        assert sensor.available and sensor.extra_state_attributes["fetched_at"] == fetched
        clock += timedelta(hours=3)
        assert not sensor.available and sensor.native_value is None


@pytest.mark.asyncio
async def test_http_contract_rejects_pagination_size_and_bad_json(tmp_path):
    hass = await make_hass(tmp_path)
    source = provider.RCEPriceCoordinator(hass)

    class Response:
        status = 200
        def __init__(self, data):
            self.data = data
            self.content = self
        def raise_for_status(self):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def iter_chunked(self, size):
            for offset in range(0, len(self.data), size):
                yield self.data[offset:offset+size]

    for body in (b'{"value":[],"nextLink":"https://invalid/"}', b'x' * 160001, b'no json'):
        with patch.object(provider, "async_get_clientsession") as session:
            session.return_value.get.return_value = Response(body)
            with pytest.raises(ValueError):
                await source._fetch(DAY)
            assert session.return_value.get.call_args.kwargs["allow_redirects"] is False
            assert "$top" not in session.return_value.get.call_args.kwargs["params"]
