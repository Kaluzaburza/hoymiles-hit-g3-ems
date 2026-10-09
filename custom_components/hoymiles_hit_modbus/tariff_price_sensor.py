"""Home Assistant publisher for the policy-neutral tariff price schedule."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timedelta, timezone
import math
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME
from .models import RuntimeData
from .tariff_price_schedule import (
    TariffPriceConfig,
    TariffPriceScheduleCache,
    TariffPriceScheduleSnapshot,
    build_tariff_price_schedule,
)
from .tariff_profiles import MANUAL_OPERATOR, get_tariff_profile


PRICE_CONFIG_ENTITIES = frozenset(
    {
        "input_select.hoymiles_tariff_operator",
        "input_select.hoymiles_tariff_type",
        "input_number.hoymiles_tariff_g11_price",
        "input_number.hoymiles_tariff_low_price",
        "input_number.hoymiles_tariff_medium_price",
        "input_number.hoymiles_tariff_peak_price",
        "input_datetime.hoymiles_tariff_cheap_1_start",
        "input_datetime.hoymiles_tariff_cheap_1_end",
        "input_datetime.hoymiles_tariff_cheap_2_start",
        "input_datetime.hoymiles_tariff_cheap_2_end",
        "input_datetime.hoymiles_tariff_medium_start",
        "input_datetime.hoymiles_tariff_medium_end",
        "input_boolean.hoymiles_tariff_weekend_low_price",
        "input_boolean.hoymiles_tariff_polish_holidays_low_price",
    }
)


def _text(hass: HomeAssistant, entity_id: str) -> str | None:
    state = hass.states.get(entity_id)
    if state is None or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE, ""}:
        return None
    return state.state.strip()


def _number(hass: HomeAssistant, entity_id: str) -> float | None:
    value = _text(hass, entity_id)
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric if math.isfinite(numeric) else None


def _minute(hass: HomeAssistant, entity_id: str) -> int | None:
    value = _text(hass, entity_id)
    if value is None:
        return None
    try:
        hour_text, minute_text, *_ = value.split(":")
        hour = int(hour_text)
        minute = int(minute_text)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _tariff_type(value: str | None) -> str | None:
    normalized = (value or "").casefold().replace(" ", "")
    return {"g11": "G11", "g12": "G12", "g12w": "G12w", "g12e": "G12e", "g13": "G13"}.get(
        normalized
    )


class HoymilesTariffPriceScheduleSensor(SensorEntity):
    """Expose current tariff prices independently from charging policy state."""

    _pstryk = None
    _unrecorded_attributes = frozenset({
        "intervals", "generated_at_utc", "requested_start_utc", "requested_end_utc",
        "coverage_start_utc", "coverage_end_utc", "coverage_hours",
    })
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "tariff_import_price_schedule"
    _attr_icon = "mdi:cash-clock"
    _attr_native_unit_of_measurement = "PLN/kWh"
    _attr_suggested_display_precision = 4

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: RuntimeData,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_tariff_import_price_schedule"
        self._cache = TariffPriceScheduleCache()
        self._snapshot: TariffPriceScheduleSnapshot | None = None
        self._listeners: set[Callable[[], None]] = set()

    @property
    def suggested_object_id(self) -> str:
        return "hoymiles_hit_tariff_import_price_schedule"

    @property
    def device_info(self) -> DeviceInfo:
        source = self._runtime.source_device
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=source.name_by_user or source.name or NAME,
            manufacturer=source.manufacturer or "Hoymiles",
            model=source.model or "HIT xxL G3",
            sw_version=source.sw_version,
        )

    @property
    def current_price_schedule(self) -> TariffPriceScheduleSnapshot | None:
        """Return the immutable publication for same-entry downstream readers."""

        return self._snapshot

    @callback
    def async_listen(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Subscribe an entry-local consumer to semantic schedule updates."""

        self._listeners.add(listener)
        return lambda: self._listeners.discard(listener)

    @property
    def available(self) -> bool:
        return self._snapshot is not None and self._snapshot.available

    @property
    def native_value(self) -> float | None:
        if self._snapshot is None:
            return None
        return self._snapshot.price_at(dt_util.utcnow())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self._snapshot is None:
            return {
                "quality": "unavailable",
                "missing_reasons": ["not_initialized"],
                "policy_independent": True,
            }
        return self._snapshot.as_attributes()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                tuple(sorted(PRICE_CONFIG_ENTITIES)),
                self._async_input_changed,
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_minute_tick,
                timedelta(minutes=1),
            )
        )
        self._refresh()
        self.async_write_ha_state()

    @callback
    def _async_input_changed(self, _event: Event) -> None:
        self._refresh()
        self.async_write_ha_state()

    @callback
    def _async_minute_tick(self, _now: datetime) -> None:
        self._refresh()
        self.async_write_ha_state()

    def _configured_schedule(self) -> TariffPriceConfig:
        operator = _text(self.hass, "input_select.hoymiles_tariff_operator")
        tariff_type = _tariff_type(
            _text(self.hass, "input_select.hoymiles_tariff_type")
        )
        operator = operator or "missing"
        tariff_type = tariff_type or "missing"
        profile = (
            get_tariff_profile(operator, tariff_type)
            if operator != MANUAL_OPERATOR
            else None
        )
        configured_prices = {
            name: _number(self.hass, f"input_number.hoymiles_tariff_{name}_price")
            for name in ("g11", "low", "medium", "peak")
        }

        def price(name: str) -> float | None:
            return (
                float(getattr(profile, f"{name}_price_pln_kwh"))
                if profile is not None
                else configured_prices[name]
            )

        def window(name: str) -> tuple[int | None, int | None]:
            return (
                _minute(self.hass, f"input_datetime.hoymiles_tariff_{name}_start"),
                _minute(self.hass, f"input_datetime.hoymiles_tariff_{name}_end"),
            )

        cheap_windows = (window("cheap_1"), window("cheap_2"))
        medium_windows = (window("medium"),)
        return TariffPriceConfig(
            tariff_type=tariff_type,
            g11_price_pln_kwh=price("g11"),  # type: ignore[arg-type]
            low_price_pln_kwh=price("low"),  # type: ignore[arg-type]
            medium_price_pln_kwh=price("medium"),  # type: ignore[arg-type]
            peak_price_pln_kwh=price("peak"),  # type: ignore[arg-type]
            cheap_windows=cheap_windows,  # type: ignore[arg-type]
            medium_windows=medium_windows,  # type: ignore[arg-type]
            weekend_low_price=self.hass.states.is_state(
                "input_boolean.hoymiles_tariff_weekend_low_price", "on"
            ),
            polish_holidays_low_price=self.hass.states.is_state(
                "input_boolean.hoymiles_tariff_polish_holidays_low_price", "on"
            ),
            operator=operator,
        )

    def _refresh(self) -> None:
        if self._pstryk is not None and self._pstryk.active:
            before = self._semantic_signature(self._snapshot)
            self._snapshot = self._pstryk.price_schedule()
            if before != self._semantic_signature(self._snapshot):
                for listener in tuple(self._listeners):
                    listener()
            return
        now_utc = dt_util.utcnow().replace(microsecond=0)
        zone_name = self.hass.config.time_zone or "UTC"
        local_zone = ZoneInfo(zone_name)
        local_now = now_utc.astimezone(local_zone)
        end_local = max(
            datetime.combine(
                local_now.date() + timedelta(days=2),
                time.min,
                tzinfo=local_zone,
            ),
            (now_utc + timedelta(hours=48)).astimezone(local_zone),
        )
        candidate = build_tariff_price_schedule(
            self._configured_schedule(),
            start=now_utc,
            end=end_local.astimezone(timezone.utc),
            local_zone=local_zone,
            generated_at=now_utc,
        )
        before = self._semantic_signature(self._snapshot)
        self._snapshot = self._cache.select(candidate, now=now_utc)
        if self._semantic_signature(self._snapshot) != before:
            for listener in tuple(self._listeners):
                listener()

    @staticmethod
    def _semantic_signature(
        snapshot: TariffPriceScheduleSnapshot | None,
    ) -> tuple[object, ...] | None:
        if snapshot is None:
            return None
        return (
            snapshot.source_revision,
            snapshot.quality,
            snapshot.coverage_complete,
            tuple(
                (
                    item.start_utc if index else None,
                    item.end_utc if index < len(snapshot.intervals) - 1 else None,
                    item.price_pln_kwh_ac,
                    item.zone,
                )
                for index, item in enumerate(snapshot.intervals)
            ),
        )
