"""One PSE fetcher and a durable two-day cache, exposed under legacy entity IDs."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
from typing import Any

from aiohttp import ClientError, ClientTimeout
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .rce_price_cache import RCEPriceCache, SCHEMA, SOURCE, WARSAW, public_entry

_LOGGER = logging.getLogger(__name__)
STORE_KEY = f"{DOMAIN}.rce_daily_prices_v1"
API_URL = "https://api.raporty.pse.pl/api/rce-pln"
PRICE_ENTITIES = ("hoymiles_rce_day", "hoymiles_rce_day_tomorrow")
_MAX_RESPONSE_BYTES = 160_000


class RCEPriceCoordinator:
    """Network failures do not replace an already verified business day."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.cache = RCEPriceCache()
        self.store = Store(hass, SCHEMA, STORE_KEY)
        self.listeners: set[Callable[[], None]] = set()
        self.errors: dict[str, str] = {}
        self.attempts: dict[str, int] = {}
        self.retry_at: dict[str, datetime] = {}
        self._task: asyncio.Task | None = None
        self._cancel_timer: Callable[[], None] | None = None
        self._stopped = False

    async def async_initialize(self) -> None:
        try:
            payload = await self.store.async_load()
        except (OSError, ValueError) as err:
            _LOGGER.warning("Cannot load RCE daily cache: %s", type(err).__name__)
            self.errors["storage"] = type(err).__name__
            payload = None
        self.cache.load(payload, dt_util.utcnow())
        # Persist pruning even when both network requests will fail.
        await self._save()

    async def _save(self) -> None:
        try:
            await self.store.async_save(self.cache.dump())
        except (OSError, ValueError) as err:
            self.errors["storage"] = type(err).__name__
            _LOGGER.warning("Cannot persist RCE daily cache: %s", type(err).__name__)
        else:
            self.errors.pop("storage", None)

    @callback
    def start(self) -> None:
        if self._cancel_timer is None:
            self._cancel_timer = async_track_time_interval(
                self.hass, self._schedule, timedelta(minutes=1))
        self._schedule()

    @callback
    def _schedule(self, _now=None) -> None:
        if not self._stopped and (self._task is None or self._task.done()):
            self._task = self.hass.async_create_background_task(
                self.async_refresh(), "hoymiles_rce_daily_prices")

    async def async_stop(self) -> None:
        self._stopped = True
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.listeners.clear()

    async def _fetch(self, day) -> list[dict[str, Any]]:
        params = {
            "$select": "rce_pln,period,business_date,dtime_utc,period_utc,publication_ts,publication_ts_utc",
            "$filter": f"business_date eq '{day.isoformat()}'",
            "$orderby": "dtime_utc asc",
        }
        session = async_get_clientsession(self.hass)
        async with session.get(API_URL, params=params, timeout=ClientTimeout(total=30),
                               headers={"Accept": "application/json"}, allow_redirects=False) as response:
            response.raise_for_status()
            if response.status != 200:
                raise ValueError("unexpected_http_status")
            body = bytearray()
            async for chunk in response.content.iter_chunked(16_384):
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ValueError("oversized_response")
            import json  # noqa: PLC0415
            payload = json.loads(body)
        # A continuation means we did not receive a complete day; never follow
        # arbitrary server-provided URLs. PSE does not accept OData $top.
        if not isinstance(payload, dict) or payload.get("@odata.nextLink") or payload.get("nextLink"):
            raise ValueError("incomplete_paginated_response")
        return payload.get("value")

    async def async_refresh(self) -> None:
        now = dt_util.utcnow()
        today = now.astimezone(WARSAW).date()
        changed = self.cache.prune(now)
        keep = {today.isoformat(), (today + timedelta(days=1)).isoformat()}
        self.attempts = {k: v for k, v in self.attempts.items() if k in keep}
        self.retry_at = {k: v for k, v in self.retry_at.items() if k in keep}
        self.errors = {k: v for k, v in self.errors.items() if k in keep or k == "storage"}
        if changed:
            await self._save()
        self._notify()
        for day in (today, today + timedelta(days=1)):
            if self._stopped:
                return
            key = day.isoformat()
            cached = self.cache.get(day, now)
            check_revision = bool(cached and day == today and self.cache.revision_checked_on != key)
            if cached and not check_revision:
                continue
            if not cached and now < self.retry_at.get(key, now):
                continue
            if check_revision:
                # One correction check per local day, persisted before the
                # network await so restarts/timeouts cannot become a poll loop.
                self.cache.revision_checked_on = key
                await self._save()
            try:
                rows = await self._fetch(day)
                self.cache.accept(day, rows, dt_util.utcnow())
            except (TimeoutError, ClientError, OSError, ValueError, TypeError, KeyError, OverflowError) as err:
                self.errors[key] = str(err)[:100] if isinstance(err, ValueError) else type(err).__name__
                count = min(self.attempts.get(key, 0) + 1, 3)
                self.attempts[key] = count
                self.retry_at[key] = dt_util.utcnow() + timedelta(minutes=min(15 * 2 ** (count - 1), 60))
            else:
                self.errors.pop(key, None)
                self.attempts.pop(key, None)
                self.retry_at.pop(key, None)
                if day == today:
                    self.cache.revision_checked_on = key
                await self._save()
            self._notify()

    @callback
    def _notify(self) -> None:
        for listener in tuple(self.listeners):
            listener()


@callback
def prepare_price_entities(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate only the two known YAML REST identities, after a HA restart.

    No private registry-file writes. Unknown collisions fail before mutations.
    The managed scheduler must be updated together with this provider.
    """
    registry = er.async_get(hass)
    entry_id = entry.entry_id
    legacy = []
    owners = set()
    for object_id in PRICE_ENTITIES:
        entity_id = f"sensor.{object_id}"
        existing = registry.async_get(entity_id)
        if existing is None:
            continue
        if (existing.platform == DOMAIN and existing.config_entry_id
                and existing.unique_id == f"{existing.config_entry_id}_{object_id}"):
            owners.add(existing.config_entry_id)
            continue
        if existing.platform != "rest" or existing.unique_id != object_id or hass.is_running:
            raise ValueError(f"RCE price identity collision/restart required: {entity_id}")
        legacy.append(existing)
    if len(owners) > 1 or (owners and legacy):
        raise ValueError("RCE price identities have conflicting owners")
    if owners and entry_id not in owners:
        # The same global PSE source is shared by multiple inverter entries.
        # Only its registered owner publishes it and schedules network I/O.
        return False
    for old in legacy:
        registry.async_remove(old.entity_id)
        new = registry.async_get_or_create(
            "sensor", DOMAIN, f"{entry_id}_{old.unique_id}",
            suggested_object_id=old.entity_id.split(".", 1)[1], config_entry=entry,
            get_initial_options=lambda old=old: dict(old.options))
        registry.async_update_entity(
            new.entity_id, name=old.name, icon=old.icon, area_id=old.area_id,
            disabled_by=old.disabled_by, hidden_by=old.hidden_by,
            aliases=old.aliases, labels=old.labels, categories=old.categories,
        )
    # Reserve both identities before the first await, including a fresh install.
    for object_id in PRICE_ENTITIES:
        registry.async_get_or_create(
            "sensor", DOMAIN, f"{entry_id}_{object_id}",
            suggested_object_id=object_id, config_entry=entry)
    return True


class HoymilesRCEPriceSensor(SensorEntity):
    """Stable public day payload; publication age remains visible."""

    _attr_should_poll = False
    _attr_icon = "mdi:chart-areaspline"

    def __init__(self, coordinator: RCEPriceCoordinator, entry_id: str, offset: int) -> None:
        self.coordinator = coordinator
        self.offset = offset
        object_id = PRICE_ENTITIES[offset]
        self._attr_unique_id = f"{entry_id}_{object_id}"
        self.entity_id = f"sensor.{object_id}"
        self._attr_name = "Hoymiles RCE Day" + (" Tomorrow" if offset else "")
        self._last_publication = None

    def _entry(self):
        now = dt_util.utcnow()
        day = now.astimezone(WARSAW).date() + timedelta(days=self.offset)
        return self.coordinator.cache.get(day, now)

    @property
    def available(self) -> bool:
        return self._entry() is not None

    @property
    def native_value(self) -> int | None:
        entry = self._entry()
        return len(entry["value"]) if entry else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        now = dt_util.utcnow()
        key = (now.astimezone(WARSAW).date() + timedelta(days=self.offset)).isoformat()
        entry = self._entry()
        return {**(public_entry(entry) if entry else {"source": SOURCE, "schema_version": SCHEMA, "business_date": key, "value": []}),
                "fetch_error": self.coordinator.errors.get(key),
                "storage_error": self.coordinator.errors.get("storage"),
                "validity_contract": "complete_warsaw_business_day"}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.coordinator.listeners.add(self._publish)
        self.async_on_remove(lambda: self.coordinator.listeners.discard(self._publish))

    @callback
    def _publish(self) -> None:
        current = (self.available, self.native_value, self.extra_state_attributes)
        if current != self._last_publication:
            self._last_publication = current
            self.async_write_ha_state()
