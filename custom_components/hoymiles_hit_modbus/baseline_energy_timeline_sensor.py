"""Entry-local publisher for the neutral baseline energy timeline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from math import isfinite
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
try:
    from homeassistant.const import MATCH_ALL
except ImportError:  # pragma: no cover - offline integration stubs only
    MATCH_ALL = "*"
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .baseline_energy_timeline import (
    BaselineEnergyInputs,
    BaselineForecastSlot,
    build_baseline_energy_timeline,
)
from .const import (
    DOMAIN,
    EMS_BASELINE_TIMELINE_ENTITY_ID,
    EMS_BASELINE_TIMELINE_TRANSLATION_KEY,
    NAME,
)
from .ems_shared_inputs import EMSSharedInputsCoordinator, EMSSharedInputsSnapshot
from .models import RuntimeData

BASELINE_ENTITY_ID = EMS_BASELINE_TIMELINE_ENTITY_ID
BASELINE_REFRESH_INTERVAL = timedelta(minutes=5)
BASELINE_POINT_COUNT = 96


def baseline_timeline_unique_id(entry_id: str) -> str:
    """Return the stable entry-local registry identity."""

    return f"{entry_id}_ems_baseline_energy_timeline"


def _finite_number(value: object, *, minimum: float = 0.0) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number >= minimum else None


def _parse_aware_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and len(value) <= 80:
        normalized = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _utc_half_hour(value: datetime) -> datetime:
    utc = value.astimezone(timezone.utc)
    return utc.replace(minute=(utc.minute // 30) * 30, second=0, microsecond=0)


def _detailed_forecast_map(
    state: State | None,
    *,
    target_local_date: object,
    local_zone: Any,
) -> dict[datetime, float]:
    """Return exact P50 powers keyed by absolute half-hour start."""

    if state is None:
        return {}
    details = state.attributes.get("detailedForecast")
    if not isinstance(details, list):
        details = state.attributes.get("detailed_forecast")
    if not isinstance(details, list) or len(details) > 200:
        return {}
    values: dict[datetime, float] = {}
    ambiguous: set[datetime] = set()
    for item in details:
        if not isinstance(item, Mapping):
            continue
        start = _parse_aware_datetime(
            item.get("period_start") or item.get("period_start_local")
        )
        if start is None or start.astimezone(local_zone).date() != target_local_date:
            continue
        key = _utc_half_hour(start)
        if start.astimezone(timezone.utc) != key:
            # Provider rows are exact interval starts.  Flooring an off-grid
            # timestamp would move measured provenance into a synthetic slot.
            continue
        raw = (
            item.get("pv_estimate")
            if item.get("pv_estimate") is not None
            else item.get("estimate")
        )
        power_kw = _finite_number(raw)
        if power_kw is None or power_kw > 1_000.0:
            continue
        # Duplicate timestamps are unverifiable.  Removing the point is safer
        # than summing two provider rows into a fabricated peak.
        if key in values or key in ambiguous:
            values.pop(key, None)
            ambiguous.add(key)
            continue
        values[key] = power_kw
    return values


def _fresh_value(sample: Any) -> float | None:
    if sample is None or getattr(sample, "fresh", False) is not True:
        return None
    return _finite_number(getattr(sample, "value", None))


def _fresh_efficiency_fraction(sample: Any) -> float | None:
    """Convert the existing percentage helper to a model fraction once."""

    value = _fresh_value(sample)
    if value is None:
        return None
    # Shared EMS helpers are percentages across their entire 0..100 domain;
    # therefore 1 means one percent, never an already-normalized fraction.
    fraction = value / 100.0
    return fraction if 0.01 <= fraction <= 1.0 else None


def _profile_for_date(
    snapshot: EMSSharedInputsSnapshot,
    local_date: object,
) -> tuple[tuple[float, ...] | None, str | None]:
    load = snapshot.load
    if getattr(load, "ready", False) is not True:
        return None, None
    is_weekend = getattr(local_date, "weekday")() >= 5
    preferred = (
        load.weekend_profile_30m_kwh
        if is_weekend
        else load.weekday_profile_30m_kwh
    )
    preferred_days = load.weekend_profile_days if is_weekend else load.weekday_profile_days
    preferred_name = (
        "weekend_profile_30m_kwh" if is_weekend else "weekday_profile_30m_kwh"
    )
    if preferred_days > 0 and len(preferred) == 48:
        return tuple(preferred), preferred_name
    if len(load.average_profile_30m_kwh) == 48:
        return tuple(load.average_profile_30m_kwh), "average_profile_30m_kwh"
    return None, None


def _load_power(
    profile: tuple[float, ...] | None,
    local_start: datetime,
) -> float | None:
    if profile is None:
        return None
    index = local_start.hour * 2 + (1 if local_start.minute >= 30 else 0)
    value = _finite_number(profile[index])
    # Profiles store kWh per nominal half-hour.  The point publishes kW; the
    # model applies the actual (possibly partial first) interval duration.
    return value * 2.0 if value is not None else None


def _slot_boundaries(now: datetime) -> list[tuple[datetime, datetime]]:
    now_utc = now.astimezone(timezone.utc)
    floor = _utc_half_hour(now_utc)
    next_boundary = floor + timedelta(minutes=30)
    first_end = next_boundary if now_utc < next_boundary else next_boundary + timedelta(minutes=30)
    boundaries = [(now_utc, first_end)]
    cursor = first_end
    for _ in range(BASELINE_POINT_COUNT - 1):
        end = cursor + timedelta(minutes=30)
        boundaries.append((cursor, end))
        cursor = end
    return boundaries


def _sample_source_label(*samples: Any) -> str | None:
    """Return bounded, de-duplicated physical/helper source identities.

    Derived broker samples (notably BMS V×A power limits) intentionally have
    no single ``entity_id``.  Their evidentiary inputs remain available in
    ``source_entity_ids`` and must not disappear from the baseline timeline.
    """

    source_ids: list[str] = []
    for sample in samples:
        if sample is None:
            continue
        candidates = [getattr(sample, "entity_id", None)]
        related = getattr(sample, "source_entity_ids", ())
        if isinstance(related, (list, tuple)):
            candidates.extend(related)
        for candidate in candidates:
            if (
                isinstance(candidate, str)
                and candidate
                and candidate not in source_ids
            ):
                source_ids.append(candidate)
    return "|".join(source_ids)[:256] or None


def _sample_source(sample: Any) -> str | None:
    return _sample_source_label(sample)


def _sample_provenance_label(*samples: Any) -> str | None:
    values: list[str] = []
    for sample in samples:
        value = getattr(sample, "provenance", None)
        if isinstance(value, str) and value and value not in values:
            values.append(value)
    return "|".join(values)[:256] or None


def build_slots_from_shared_inputs(
    hass: HomeAssistant,
    snapshot: EMSSharedInputsSnapshot,
    *,
    generated_at: datetime,
) -> list[BaselineForecastSlot]:
    """Project exact forecast/provider rows and LOAD model buckets to slots."""

    try:
        local_zone = ZoneInfo(hass.config.time_zone)
    except (AttributeError, TypeError, ZoneInfoNotFoundError):
        local_zone = timezone.utc

    forecast_by_date = {
        generated_at.astimezone(local_zone).date(): snapshot.forecast.today,
        (generated_at.astimezone(local_zone) + timedelta(days=1)).date(): (
            snapshot.forecast.tomorrow
        ),
    }
    pv_maps: dict[object, dict[datetime, float]] = {}
    for local_date, sample in forecast_by_date.items():
        if getattr(sample, "fresh", False) is not True:
            pv_maps[local_date] = {}
            continue
        entity_id = _sample_source(sample)
        state = hass.states.get(entity_id) if entity_id is not None else None
        mapping = _detailed_forecast_map(
            state,
            target_local_date=local_date,
            local_zone=local_zone,
        )
        # A fresh, exactly zero daily total proves all non-negative detailed
        # slots are zero even if the provider omitted its verbose attribute.
        if not mapping and _fresh_value(sample) == 0.0:
            mapping = {
                _utc_half_hour(start): 0.0
                for start, _ in _slot_boundaries(generated_at)
                if start.astimezone(local_zone).date() == local_date
            }
        pv_maps[local_date] = mapping

    result: list[BaselineForecastSlot] = []
    for start_utc, end_utc in _slot_boundaries(generated_at):
        local_start = start_utc.astimezone(local_zone)
        local_date = local_start.date()
        forecast_sample = forecast_by_date.get(local_date)
        pv_map = pv_maps.get(local_date, {})
        profile, profile_name = _profile_for_date(snapshot, local_date)
        result.append(
            BaselineForecastSlot(
                start=start_utc,
                end=end_utc,
                pv_kw=pv_map.get(_utc_half_hour(start_utc)),
                load_kw=_load_power(profile, local_start),
                pv_source=_sample_source(forecast_sample),
                load_source=snapshot.load.source,
                pv_provenance=(
                    "detailed_forecast_p50"
                    if pv_map
                    else getattr(forecast_sample, "provenance", None)
                ),
                load_provenance=profile_name,
            )
        )
    return result


def build_payload_from_shared_inputs(
    hass: HomeAssistant,
    snapshot: EMSSharedInputsSnapshot,
    *,
    generated_at: datetime,
) -> dict[str, Any]:
    """Build one timeline payload from one immutable broker revision."""

    capacity_sample = (
        snapshot.system.total_capacity_kwh
        if snapshot.system.total_capacity_kwh.fresh
        else snapshot.system.battery_capacity_kwh
    )
    gcf_samples = (
        snapshot.gcf.hardware_readback_supported,
        snapshot.gcf.generation,
        snapshot.gcf.enable_code,
        snapshot.gcf.maximum_export_power_percent,
    )
    gcf_state_provenance = (
        "confirmed_zero_export"
        if snapshot.gcf.zero_export_confirmed
        else "physical_gcf_readback"
        if snapshot.gcf.gcf_readback_ready
        else "unverified"
    )
    gcf_sample_provenance = _sample_provenance_label(*gcf_samples)
    gcf_provenance = "|".join(
        value
        for value in (gcf_state_provenance, gcf_sample_provenance)
        if value
    )[:256]
    sources = {
        "shared_inputs": "sensor.hoymiles_hit_ems_shared_inputs",
        "soc": _sample_source(snapshot.system.battery_soc_percent),
        "capacity": _sample_source(capacity_sample),
        "reserve": _sample_source(snapshot.system.self_use_reserve_soc_percent),
        "hardware_minimum_soc": _sample_source(
            snapshot.system.hardware_minimum_soc_percent
        ),
        "hardware_maximum_soc": _sample_source(
            snapshot.system.hardware_maximum_soc_percent
        ),
        "pv_to_battery_efficiency": _sample_source(
            snapshot.efficiency.pv_to_battery_efficiency
        ),
        "battery_to_home_efficiency": _sample_source(
            snapshot.efficiency.battery_to_home_efficiency
        ),
        "bms_charge_limit": _sample_source_label(
            snapshot.bms.maximum_charge_power_kw
        ),
        "bms_discharge_limit": _sample_source_label(
            snapshot.bms.maximum_discharge_power_kw
        ),
        "system_ac_power": _sample_source(
            snapshot.system.system_rated_power_kw
        ),
        "gcf_readback": _sample_source_label(*gcf_samples),
    }
    provenance = {
        "shared_inputs": "entry_local_snapshot",
        "soc": snapshot.system.battery_soc_percent.provenance,
        "capacity": capacity_sample.provenance,
        "reserve": snapshot.system.self_use_reserve_soc_percent.provenance,
        "hardware_minimum_soc": (
            snapshot.system.hardware_minimum_soc_percent.provenance
        ),
        "hardware_maximum_soc": (
            snapshot.system.hardware_maximum_soc_percent.provenance
            if snapshot.system.hardware_maximum_soc_percent.fresh
            else "mathematical_soc_domain_100_percent"
        ),
        "pv_to_battery_efficiency": (
            snapshot.efficiency.pv_to_battery_efficiency.provenance
        ),
        "battery_to_home_efficiency": (
            snapshot.efficiency.battery_to_home_efficiency.provenance
        ),
        "bms_charge_limit": snapshot.bms.maximum_charge_power_kw.provenance,
        "bms_discharge_limit": snapshot.bms.maximum_discharge_power_kw.provenance,
        "system_ac_power": snapshot.system.system_rated_power_kw.provenance,
        "gcf_readback": gcf_provenance,
    }
    inputs = BaselineEnergyInputs(
        generated_at=generated_at,
        config_entry_id=snapshot.config_entry_id,
        shared_inputs_revision=snapshot.revision,
        current_soc_percent=_fresh_value(snapshot.system.battery_soc_percent),
        battery_capacity_kwh=_fresh_value(capacity_sample),
        reserve_soc_percent=_fresh_value(
            snapshot.system.self_use_reserve_soc_percent
        ),
        hardware_minimum_soc_percent=_fresh_value(
            snapshot.system.hardware_minimum_soc_percent
        ),
        hardware_maximum_soc_percent=_fresh_value(
            snapshot.system.hardware_maximum_soc_percent
        ),
        pv_to_battery_efficiency=_fresh_efficiency_fraction(
            snapshot.efficiency.pv_to_battery_efficiency
        ),
        battery_to_home_efficiency=_fresh_efficiency_fraction(
            snapshot.efficiency.battery_to_home_efficiency
        ),
        maximum_charge_power_kw=_fresh_value(
            snapshot.bms.maximum_charge_power_kw
        ),
        maximum_discharge_power_kw=_fresh_value(
            snapshot.bms.maximum_discharge_power_kw
        ),
        system_ac_power_kw=_fresh_value(
            snapshot.system.system_rated_power_kw
        ),
        zero_export_confirmed=snapshot.gcf.zero_export_confirmed is True,
        export_allowed=(
            snapshot.gcf.export_allowed
            if isinstance(snapshot.gcf.export_allowed, bool)
            else None
        ),
        sources=sources,
        provenance=provenance,
    )
    return build_baseline_energy_timeline(
        inputs,
        build_slots_from_shared_inputs(
            hass,
            snapshot,
            generated_at=generated_at,
        ),
    )


class HoymilesBaselineEnergyTimelineSensor(SensorEntity):
    """Publish the always-on baseline without any control authority."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:chart-timeline-variant-shimmer"
    _attr_translation_key = EMS_BASELINE_TIMELINE_TRANSLATION_KEY
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: RuntimeData,
        coordinator: EMSSharedInputsCoordinator,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._coordinator = coordinator
        self.entity_id = BASELINE_ENTITY_ID
        self._attr_unique_id = baseline_timeline_unique_id(entry.entry_id)
        self._state = "unavailable"
        self._attributes: dict[str, Any] = {}
        self._rebuild()

    @property
    def native_value(self) -> str:
        return self._state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attributes

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

    def _rebuild(self) -> None:
        payload = build_payload_from_shared_inputs(
            self.hass,
            self._coordinator.snapshot,
            generated_at=dt_util.utcnow(),
        )
        self._state = payload.pop("state")
        self._attributes = payload

    @callback
    def _async_shared_inputs_updated(self) -> None:
        self._rebuild()
        if self.hass is not None:
            self.async_write_ha_state()

    @callback
    def _async_interval_refresh(self, _now: datetime) -> None:
        self._async_shared_inputs_updated()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self._coordinator.async_listen(self._async_shared_inputs_updated)
        )
        self._rebuild()
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_interval_refresh,
                BASELINE_REFRESH_INTERVAL,
            )
        )
