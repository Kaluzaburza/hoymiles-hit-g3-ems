"""Data models for EMS for Hoymiles HIT-(5–20)L-G3."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

if TYPE_CHECKING:
    from .ems_notifications import HoymilesEmsNotificationManager
    from .ems_shared_inputs import EMSSharedInputsCoordinator
    from .rce_price_sensor import RCEPriceCoordinator
    from .supervisor_control_lease import ControlLeaseClient


@dataclass(slots=True)
class MatchedEntity:
    """A catalog entry and its optional ESPHome source entity."""

    catalog: dict[str, Any]
    source: er.RegistryEntry | None


@dataclass(slots=True)
class RuntimeData:
    """Runtime data shared by all entity platforms."""

    source_device: dr.DeviceEntry
    entities: dict[str, list[MatchedEntity]]
    shared_inputs: EMSSharedInputsCoordinator | None = None
    rce_prices: RCEPriceCoordinator | None = None
    notifications: HoymilesEmsNotificationManager | None = None
    profits: Any = None
    control_lease: ControlLeaseClient | None = None
