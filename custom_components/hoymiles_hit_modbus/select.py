"""Localized writable Hoymiles select entities."""

from __future__ import annotations

import re
import unicodedata

from homeassistant.components.select import (
    ATTR_OPTION,
    DOMAIN as SELECT_DOMAIN,
    SERVICE_SELECT_OPTION,
    SelectEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .entity import HoymilesProxyEntity
from .models import RuntimeData
from .supervisor_sensor import async_dispatch_manual_ems_proxy_write


def _slugify(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^a-z0-9]+", "_", value.casefold()).strip("_")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up writable localized selects."""
    runtime: RuntimeData = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        HoymilesSelect(hass, entry, runtime, matched)
        for matched in runtime.entities["select"]
    )


class HoymilesSelect(HoymilesProxyEntity, SelectEntity):
    """A select mirrored from the ESPHome Modbus bridge."""

    @property
    def _raw_to_key(self) -> dict[str, str]:
        return {
            option["raw"]: option["key"]
            for option in self._catalog.get("options", [])
        }

    @property
    def _key_to_raw(self) -> dict[str, str]:
        return {
            option["key"]: option["raw"]
            for option in self._catalog.get("options", [])
        }

    @property
    def current_option(self) -> str | None:
        """Return the stable canonical option key."""
        source = self.source_state
        if source is None or not self.available:
            return None
        return self._raw_to_key.get(source.state, _slugify(source.state))

    @property
    def options(self) -> list[str]:
        """Return canonical option keys translated by Home Assistant."""
        configured = list(self._key_to_raw)
        if configured:
            return configured
        source = self.source_state
        if source is None:
            return []
        return [_slugify(value) for value in source.attributes.get("options", [])]

    async def async_select_option(self, option: str) -> None:
        """Forward a selected canonical option to ESPHome."""
        if self._source_entity_id is None:
            raise HomeAssistantError(
                "This setting requires a newer Hoymiles ESPHome firmware"
            )
        raw_option = self._key_to_raw.get(option, option)

        async def dispatch() -> None:
            await self.hass.services.async_call(
                SELECT_DOMAIN,
                SERVICE_SELECT_OPTION,
                {
                    ATTR_ENTITY_ID: self._source_entity_id,
                    ATTR_OPTION: raw_option,
                },
                blocking=True,
                context=self._context,
            )

        if self._catalog.get("source_id") == "ems_mode_4300":
            if not await async_dispatch_manual_ems_proxy_write(
                self.hass,
                self._entry.entry_id,
                "ems_mode_4300",
                option,
            ):
                raise HomeAssistantError(
                    "This inverter actuator is controlled by EMS Supervisor; "
                    "set it Off and wait for confirmed idle ownership release"
                )
            return
        await dispatch()
