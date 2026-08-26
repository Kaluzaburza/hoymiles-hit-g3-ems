"""Localized Hoymiles sensor entities."""

from __future__ import annotations

from math import isfinite
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    EMS_PACKAGE_SENTINEL,
    EMS_PACKAGE_VERSION,
    EMS_PACKAGE_VERSION_ENTITY,
    VERSION,
)
from .entity import HoymilesProxyEntity
from .energy_data import numeric_state_sample
from .localization import localized_text_state
from .models import MatchedEntity, RuntimeData
from .power_balance import (
    OVERVIEW_BATTERY_POWER,
    OVERVIEW_INVERTER_ACTIVE_POWER,
    PARALLEL_POWER_SOURCE_KEYS_BY_TARGET,
    PARALLEL_POWER_TARGETS,
    calculate_parallel_power_balance,
    calculate_parallel_inverter_power,
    is_parallel_master,
    is_known_machine_type,
    select_overview_power,
)
from .rcm_sensor import HoymilesRCMOptimizerSensor
from .rce_sensor import HoymilesRCEOptimizerSensor
from .supervisor_sensor import HoymilesSupervisorSensor
from .tariff_sensor import HoymilesTariffOptimizerSensor
from .timeline_sensor import HoymilesAutomationPlanTimelineSensor


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up localized sensors."""
    runtime: RuntimeData = hass.data[DOMAIN][entry.entry_id]
    entities = [
        HoymilesSensor(hass, entry, runtime, matched)
        for matched in runtime.entities["sensor"]
    ]
    entities.append(HoymilesSupervisorSensor(hass, entry, runtime))
    rce_plan = HoymilesRCEOptimizerSensor(hass, entry, runtime)
    tariff_plan = HoymilesTariffOptimizerSensor(hass, entry, runtime)
    rcm_plan = HoymilesRCMOptimizerSensor(hass, entry, runtime)
    tariff_plan.attach_rce_plan_source(rce_plan)
    rce_plan.attach_tariff_plan_source(tariff_plan)
    # Both source plans are registered before either observation-only timeline.
    entities.append(rce_plan)
    entities.append(tariff_plan)
    entities.append(
        HoymilesAutomationPlanTimelineSensor(
            hass,
            entry,
            runtime,
            policy_id="rce",
            source_sensor=rce_plan,
        )
    )
    entities.append(
        HoymilesAutomationPlanTimelineSensor(
            hass,
            entry,
            runtime,
            policy_id="tariff",
            source_sensor=tariff_plan,
        )
    )
    entities.append(rcm_plan)
    rcm_timeline_sensor = HoymilesAutomationPlanTimelineSensor
    entities.append(
        rcm_timeline_sensor(
            hass,
            entry,
            runtime,
            policy_id="rcm",
            source_sensor=rcm_plan,
        )
    )
    entities.append(HoymilesSetupStatusSensor(hass, entry, runtime))
    async_add_entities(entities)


class HoymilesSetupStatusSensor(SensorEntity):
    """Summarize installation readiness and the next user action."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_has_entity_name = True
    _attr_translation_key = "setup_status"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: RuntimeData,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_setup_status"
        self._source_entity_ids = tuple(
            matched.source.entity_id
            for matches in runtime.entities.values()
            for matched in matches
            if matched.source is not None
        )
        self._expected_entity_count = sum(
            len(matches) for matches in runtime.entities.values()
        )

    @property
    def _language_is_polish(self) -> bool:
        return self.hass.config.language.casefold().startswith("pl")

    @property
    def _ems_loaded(self) -> bool:
        return self.hass.states.get(EMS_PACKAGE_SENTINEL) is not None

    @property
    def _ems_version(self) -> str | None:
        state = self.hass.states.get(EMS_PACKAGE_VERSION_ENTITY)
        return state.state if state is not None else None

    @property
    def _ems_restart_required(self) -> bool:
        return self._ems_loaded and self._ems_version != EMS_PACKAGE_VERSION

    @property
    def _esp_online(self) -> bool:
        return any(
            (state := self.hass.states.get(entity_id)) is not None
            and state.state not in {STATE_UNAVAILABLE, STATE_UNKNOWN}
            for entity_id in self._source_entity_ids
        )

    @property
    def native_value(self) -> str:
        """Return a human-readable setup state."""
        if not self._esp_online:
            return "ESP32 niedostępne" if self._language_is_polish else "ESP32 offline"
        if len(self._source_entity_ids) < self._expected_entity_count:
            return (
                "Wymagana aktualizacja ESP32"
                if self._language_is_polish
                else "ESP32 update required"
            )
        if not self._ems_loaded:
            return (
                "Wymagane włączenie pakietów i restart"
                if self._language_is_polish
                else "Enable packages and restart"
            )
        if self._ems_restart_required:
            return (
                "Wymagany ponowny restart"
                if self._language_is_polish
                else "Restart required to finish update"
            )
        return "Gotowe" if self._language_is_polish else "Ready"

    @property
    def icon(self) -> str:
        """Return an icon matching readiness."""
        return "mdi:check-circle" if self.native_value in {"Gotowe", "Ready"} else "mdi:alert-circle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose a compact installation checklist."""
        source_count = len(self._source_entity_ids)
        coverage = (
            round(source_count / self._expected_entity_count * 100.0, 1)
            if self._expected_entity_count
            else 0.0
        )
        if not self._esp_online:
            next_step = (
                "Sprawdź zasilanie, Wi-Fi i integrację ESPHome."
                if self._language_is_polish
                else "Check power, Wi-Fi and the ESPHome integration."
            )
        elif source_count < self._expected_entity_count:
            next_step = (
                "Przebuduj i wgraj aktualny firmware ESPHome, następnie przeładuj integrację."
                if self._language_is_polish
                else "Build and upload the current ESPHome firmware, then reload the integration."
            )
        elif not self._ems_loaded:
            next_step = (
                "Włącz homeassistant: packages: !include_dir_named packages i uruchom HA ponownie."
                if self._language_is_polish
                else "Enable homeassistant: packages: !include_dir_named packages and restart HA."
            )
        elif self._ems_restart_required:
            next_step = (
                "Sprawdź konfigurację i uruchom Home Assistant ponownie jeszcze raz."
                if self._language_is_polish
                else "Validate the configuration and restart Home Assistant once more."
            )
        else:
            next_step = (
                "Instalacja jest gotowa."
                if self._language_is_polish
                else "The installation is ready."
            )
        return {
            "esp32_online": self._esp_online,
            "ems_package_loaded": self._ems_loaded,
            "ems_package_version": self._ems_version,
            "expected_ems_package_version": EMS_PACKAGE_VERSION,
            "integration_version": VERSION,
            "restart_required": self._ems_restart_required,
            "source_entities": source_count,
            "expected_entities": self._expected_entity_count,
            "firmware_coverage_percent": coverage,
            "next_step": next_step,
        }

    async def async_added_to_hass(self) -> None:
        """Refresh readiness when ESPHome or the EMS package changes."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                (
                    *self._source_entity_ids,
                    EMS_PACKAGE_SENTINEL,
                    EMS_PACKAGE_VERSION_ENTITY,
                ),
                self._async_handle_state_change,
            )
        )

    @callback
    def _async_handle_state_change(self, _event: Event) -> None:
        self.async_write_ha_state()


class HoymilesSensor(HoymilesProxyEntity, SensorEntity):
    """A sensor mirrored from the ESPHome Modbus bridge."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: RuntimeData,
        matched: MatchedEntity,
    ) -> None:
        """Initialize a mirrored sensor and its optional balance sources."""
        super().__init__(hass, entry, runtime, matched)
        self._parallel_power_sources: dict[str, str] = {}
        if self._catalog["translation_key"] not in PARALLEL_POWER_TARGETS:
            return

        dependency_keys = set(
            PARALLEL_POWER_SOURCE_KEYS_BY_TARGET[
                self._catalog["translation_key"]
            ]
        )
        self._parallel_power_sources = {
            candidate.catalog["translation_key"]: candidate.source.entity_id
            for candidate in runtime.entities["sensor"]
            if candidate.catalog["translation_key"] in dependency_keys
            and candidate.source is not None
        }

    def _parallel_source_state(self, translation_key: str) -> State | None:
        """Return a native source state used by the parallel power balance."""
        entity_id = self._parallel_power_sources.get(translation_key)
        return self.hass.states.get(entity_id) if entity_id is not None else None

    def _parallel_source_value(self, translation_key: str) -> float | None:
        """Return a fresh finite source value, converted to watts."""
        source = self._parallel_source_state(translation_key)
        if source is None:
            return None
        unit = source.attributes.get("unit_of_measurement")
        if unit not in {None, "W"}:
            if unit != "kW":
                return None
        sample = numeric_state_sample(
            source,
            dt_util.utcnow(),
            max_age_seconds=120.0,
            scale=1000.0 if unit == "kW" else 1.0,
        )
        return sample.value if sample.fresh else None

    @property
    def _parallel_master_declared(self) -> bool:
        """Return whether the last topology value identifies this as Master."""
        topology = self._parallel_source_state("machines_type")
        return topology is not None and is_parallel_master(topology.state)

    @property
    def _parallel_topology_known(self) -> bool:
        """Require a fresh explicit topology before any native fallback."""
        topology = self._parallel_source_state("machines_type")
        sample = numeric_state_sample(
            topology,
            dt_util.utcnow(),
            max_age_seconds=300.0,
            minimum=0.0,
            maximum=2.0,
        )
        return sample.fresh and is_known_machine_type(sample.value)

    @property
    def _is_parallel_master(self) -> bool:
        """Require a fresh topology sample before using the derived balance."""
        topology = self._parallel_source_state("machines_type")
        sample = numeric_state_sample(
            topology,
            dt_util.utcnow(),
            max_age_seconds=300.0,
            minimum=0.0,
        )
        return sample.fresh and is_parallel_master(sample.value)

    def _parallel_power_value(self) -> float | None:
        """Return system-wide power derived from the required balance sources."""
        if not self._is_parallel_master:
            return None
        grid_power = self._parallel_source_value(
            "overview_grid_total_active_power"
        )
        load_power = self._parallel_source_value("overview_load_active_power")
        if grid_power is None or load_power is None:
            return None

        translation_key = self._catalog["translation_key"]
        if translation_key == OVERVIEW_INVERTER_ACTIVE_POWER:
            return calculate_parallel_inverter_power(
                grid_power=grid_power,
                load_power=load_power,
            )

        pv_power = self._parallel_source_value("overview_pv_total_power")
        if pv_power is None:
            return None
        balance = calculate_parallel_power_balance(
            pv_power=pv_power,
            grid_power=grid_power,
            load_power=load_power,
        )
        return balance.battery_power if balance is not None else None

    @property
    def available(self) -> bool:
        """Mirror ordinary sources; fail closed for parallel power balances."""
        if self._catalog["translation_key"] not in PARALLEL_POWER_TARGETS:
            return super().available
        if not self._parallel_topology_known:
            return False
        if self._parallel_master_declared:
            return self._is_parallel_master and self._parallel_power_value() is not None
        return super().available

    def _mirrored_native_value(self) -> Any:
        """Return the source value without invoking the derived availability."""
        source = self.source_state
        if source is None or not super().available:
            return None
        if self._catalog.get("source_component") == "text_sensor":
            return localized_text_state(source.state, self.hass.config.language)
        try:
            return float(source.state)
        except (TypeError, ValueError):
            return source.state

    @property
    def native_value(self) -> Any:
        """Return a numeric value or localized text state."""
        source_value = self._mirrored_native_value()
        translation_key = self._catalog["translation_key"]
        if translation_key not in PARALLEL_POWER_TARGETS:
            return source_value

        topology = self._parallel_source_state("machines_type")
        return select_overview_power(
            translation_key,
            machine_type=topology.state if topology is not None else None,
            source_power=(
                source_value if isinstance(source_value, (float, int)) else None
            ),
            derived_power=self._parallel_power_value(),
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Expose whether a parallel power value uses the AC system balance."""
        attributes = super().extra_state_attributes
        if self._catalog["translation_key"] not in PARALLEL_POWER_TARGETS:
            return attributes
        derived_power = self._parallel_power_value()
        attributes.update(
            {
                "parallel_balance_active": (
                    self._is_parallel_master and derived_power is not None
                ),
                "power_value_source": (
                    "parallel_ac_balance"
                    if self._is_parallel_master and derived_power is not None
                    else (
                        "parallel_balance_unavailable"
                        if self._parallel_master_declared
                        or not self._parallel_topology_known
                        else "esphome_source"
                    )
                ),
                "power_sign_convention": (
                    "battery_positive_discharge_grid_positive_export"
                ),
            }
        )
        return attributes

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Mirror the source unit."""
        source = self.source_state
        return source.attributes.get("unit_of_measurement") if source else None

    @property
    def device_class(self) -> str | None:
        """Mirror the source device class."""
        source = self.source_state
        return source.attributes.get("device_class") if source else None

    @property
    def state_class(self) -> str | None:
        """Mirror the source state class."""
        source = self.source_state
        return source.attributes.get("state_class") if source else None

    @property
    def suggested_display_precision(self) -> int | None:
        """Mirror the source display precision when available."""
        source = self.source_state
        if source is None:
            return None
        precision = source.attributes.get("suggested_display_precision")
        return int(precision) if precision is not None else None

    async def async_added_to_hass(self) -> None:
        """Subscribe balance proxies to topology, PV, grid and LOAD changes."""
        await super().async_added_to_hass()
        if not self._parallel_power_sources:
            return
        dependency_entity_ids = tuple(
            dict.fromkeys(self._parallel_power_sources.values())
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                dependency_entity_ids,
                self._async_source_state_changed,
            )
        )
