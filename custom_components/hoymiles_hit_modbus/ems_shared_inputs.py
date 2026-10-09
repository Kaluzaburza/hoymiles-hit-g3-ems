"""Entry-local, policy-neutral EMS input broker.

The broker resolves integration-owned entities by their exact config-entry
identity and publishes immutable, bounded samples.  It deliberately contains
no optimizer policy, execution authority, Modbus write, or inferred
grid-to-battery flow.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from math import isfinite
import re
from typing import Any, TypeAlias
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
try:  # Real HA exports MATCH_ALL; minimal offline harnesses may not.
    from homeassistant.const import MATCH_ALL
except ImportError:  # pragma: no cover - exercised by lightweight test stubs.
    MATCH_ALL = "*"
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .const import DOMAIN, NAME
from .energy_data import numeric_state_sample, state_age_seconds, state_reported_at
from .pv_forecast_usability import (
    SOLCAST_UPDATE_ENTITY_CANDIDATES,
    evaluate_pv_forecast_usefulness,
    resolve_solcast_update_state,
)


SHARED_INPUTS_SCHEMA_VERSION = 1
SHARED_INPUTS_REFRESH_INTERVAL = timedelta(seconds=60)
SHARED_INPUTS_PHYSICAL_MAX_AGE_SECONDS = 300.0
SHARED_INPUTS_FORECAST_MAX_AGE_SECONDS = 18.0 * 60.0 * 60.0
SHARED_INPUTS_GCF_COHORT_MAX_SKEW_SECONDS = 5.0
SHARED_INPUTS_MIGRATION_VERSION = 2
LOAD_MODEL_MAX_AGE_SECONDS = 30.0 * 60.0 * 60.0

P1_PROVIDERLESS_GRID_TO_BATTERY_DEPENDENCY = (
    "P1_PROVIDERLESS_GRID_TO_BATTERY_DEPENDENCY"
)


def load_model_generated_at_is_fresh(
    generated_at: datetime | None,
    *,
    now: datetime,
) -> bool:
    """Apply the single 30-hour LOAD-model observation-age contract."""

    if (
        generated_at is None
        or generated_at.tzinfo is None
        or generated_at.utcoffset() is None
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        return False
    age_seconds = (
        now.astimezone(timezone.utc)
        - generated_at.astimezone(timezone.utc)
    ).total_seconds()
    return -5.0 <= age_seconds <= LOAD_MODEL_MAX_AGE_SECONDS


def _explicit_fallback_model_ready(
    load_model: "SharedLoadModelSnapshot",
    fallback: "SharedSample",
) -> bool:
    """Keep a valid explicit fallback usable without renewing history age."""

    fallback_value = fallback.value
    model_value = load_model.average_daily_home_load_kwh
    return bool(
        load_model.fallback_currently_used is True
        and fallback.fresh is True
        and type(fallback_value) in {int, float}
        and isfinite(float(fallback_value))
        and float(fallback_value) > 0.0
        and type(model_value) in {int, float}
        and isfinite(float(model_value))
        and abs(float(model_value) - float(fallback_value)) <= 1e-6
    )


def qualified_load_history_is_usable(
    generated_at: datetime | None,
    daily_dates: Sequence[str],
    *,
    now: datetime,
) -> bool:
    """Retain accepted statistical days inside the 28-day household window.

    This is not physical telemetry freshness or permission to execute. The
    provider must qualify and identity-bind the days before publishing them;
    the broker and consumers independently bound their calendar/observation age.
    """
    if (
        not isinstance(generated_at, datetime)
        or generated_at.tzinfo is None
        or generated_at.utcoffset() is None
        or now.tzinfo is None
        or now.utcoffset() is None
        or isinstance(daily_dates, (str, bytes))
        or not isinstance(daily_dates, Sequence)
        or not 3 <= len(daily_dates) <= 28
    ):
        return False
    age = (
        now.astimezone(timezone.utc) - generated_at.astimezone(timezone.utc)
    ).total_seconds()
    if not -5.0 <= age <= 28 * 86400:
        return False
    try:
        dates = tuple(date.fromisoformat(item) for item in daily_dates)
    except (TypeError, ValueError):
        return False
    return bool(
        len(set(dates)) == len(dates)
        and all(1 <= (now.date() - item).days <= 28 for item in dates)
    )

NEW_FORECAST_HELPERS = {
    "today": "input_text.hoymiles_ems_pv_forecast_today_entity",
    "tomorrow": "input_text.hoymiles_ems_pv_forecast_tomorrow_entity",
    "day3": "input_text.hoymiles_ems_pv_forecast_day_3_entity",
}
LEGACY_FORECAST_HELPERS = {
    "today": "input_text.hoymiles_solcast_forecast_today_entity",
    "tomorrow": "input_text.hoymiles_solcast_forecast_tomorrow_entity",
    "day3": "input_text.hoymiles_solcast_forecast_day_3_entity",
}
FORECAST_CANDIDATES = {
    "today": (
        "sensor.solcast_pv_forecast_forecast_today",
        "sensor.solcast_pv_forecast_prognoza_na_dzisiaj",
        "sensor.solcast_forecast_today",
    ),
    "tomorrow": (
        "sensor.solcast_pv_forecast_forecast_tomorrow",
        "sensor.solcast_pv_forecast_prognoza_na_jutro",
        "sensor.solcast_forecast_tomorrow",
    ),
    "day3": (
        "sensor.solcast_pv_forecast_forecast_day_3",
        "sensor.solcast_pv_forecast_forecast_d3",
        "sensor.solcast_pv_forecast_day_3",
        "sensor.solcast_pv_forecast_d3",
        "sensor.solcast_pv_forecast_prognoza_na_dzien_3",
        "sensor.solcast_pv_forecast_prognoza_d3",
        "sensor.solcast_forecast_day_3",
        "sensor.solcast_forecast_d3",
    ),
    "remaining_today": (
        "sensor.solcast_pv_forecast_forecast_remaining_today",
        "sensor.solcast_pv_forecast_pozostala_prognoza_na_dzis",
        "sensor.solcast_forecast_remaining_today",
    ),
}

NEW_FALLBACK_LOAD_HELPER = "input_number.hoymiles_ems_fallback_daily_home_load"
LEGACY_FALLBACK_LOAD_HELPER = "input_number.hoymiles_rce_fallback_daily_load"
NEW_RATED_POWER_HELPER = "input_select.hoymiles_ems_inverter_rated_power_each"
LEGACY_RATED_POWER_HELPER = "input_select.hoymiles_rce_inverter_rated_power"
NEW_PV_TO_BATTERY_EFFICIENCY_HELPER = (
    "input_number.hoymiles_ems_pv_to_battery_efficiency"
)
NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER = (
    "input_number.hoymiles_ems_battery_to_home_efficiency"
)
LEGACY_PV_TO_BATTERY_EFFICIENCY_HELPER = (
    "input_number.hoymiles_tariff_charge_efficiency"
)
LEGACY_BATTERY_TO_HOME_EFFICIENCY_HELPER = (
    "input_number.hoymiles_tariff_discharge_efficiency"
)

_ENTITY_ID_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
_STRICT_MODEL_RE = re.compile(r"HIT-(5|8|10|12|15|20)L-G3", re.IGNORECASE)
_RATED_POWER_RE = re.compile(
    r"(5|8|10|12|15|20)(?:\s*kW)?",
    re.IGNORECASE,
)
_AUTOMATIC_RATED_POWER_STATES = frozenset(
    {"auto", "automatic", "automatycznie"}
)
_ALLOWED_RATED_POWER_KW = frozenset({5.0, 8.0, 10.0, 12.0, 15.0, 20.0})
_UNAVAILABLE = frozenset({"", "unknown", "unavailable", "none", "niedostępne"})
_LEGACY_FALLBACK_OPEN_STATUSES = frozenset(
    {
        "not_run",
        "target_missing",
        "target_unavailable",
        "legacy_unavailable",
        "copy_pending",
        "write_failed",
        "verification_failed",
    }
)

_MAX_ENTITY_ID_LENGTH = 255
_MAX_PUBLIC_SOURCE_IDS = 4
_MAX_DAILY_TOTALS = 28
_PROFILE_SLOT_COUNT = 48
_MAX_ENERGY_KWH = 10_000.0
_MAX_POWER_KW = 1_000_000.0
# Per phase: tolerate the inverter's bounded idle/load-drop residual as zero.
_HOME_LOAD_NEGATIVE_RESIDUAL_MIN_KW = -0.300
_SIGNED_WORD_POWER_MAX_KW = 32.767

REQUIRED_FLAT_ATTRIBUTE_KEYS = frozenset(
    {
        "inverter_model",
        "inverter_model_source",
        "inverter_rated_power_each_kw",
        "inverter_rated_power_source",
        "inverter_count",
        "inverter_count_source",
        "system_rated_power_kw",
        "system_rated_power_source",
        "battery_capacity_kwh",
        "battery_capacity_source",
        "reserve_soc_floor_percent",
        "hardware_minimum_soc_percent",
        "hardware_maximum_soc_percent",
        "forecast_today_entity",
        "forecast_tomorrow_entity",
        "forecast_day_3_entity",
        "forecast_remaining_today_entity",
        "forecast_today_ready",
        "forecast_tomorrow_ready",
        "forecast_day_3_ready",
        "forecast_remaining_today_ready",
        "forecast_last_updated",
        "forecast_quality",
        "forecast_provenance",
        "forecast_source_ages_seconds",
        "forecast_freshness",
        "forecast_source_freshness",
        "forecast_usability_modes",
        "forecast_last_success",
        "forecast_next_update",
        "forecast_valid_until",
        "load_model_ready",
        "load_model_source",
        "load_history_days",
        "load_history_complete",
        "load_profile_history_days",
        "load_daily_coverage_ratio",
        "load_profile_coverage_ratio",
        "average_daily_home_load_kwh",
        "weekday_profile_ready",
        "weekend_profile_ready",
        "load_current_day_energy_kwh",
        "load_current_day_observed_at",
        "load_persistence_delta_kw",
        "load_persistence_observed_at",
        "load_persistence_sample_count",
        "load_model_schema",
        "load_model_quality",
        "fallback_daily_home_load_kwh",
        "fallback_currently_used",
        "load_model_generated_at",
        "bms_ready",
        "bms_provenance",
        "battery_voltage",
        "maximum_charge_current",
        "maximum_discharge_current",
        "maximum_charge_power_kw",
        "maximum_discharge_power_kw",
        "bms_source_ages_seconds",
        "bms_freshness",
        "gcf_readback_ready",
        "zero_export_confirmed",
        "export_allowed",
        "export_limit_percent",
        "gcf_readback_generation",
        "gcf_provenance",
        "gcf_source_ages_seconds",
        "gcf_freshness",
        "physical_power_ready",
        "physical_power_quality",
        "pv_power_entity",
        "home_load_power_entity",
        "home_load_power_source_entities",
        "system_load_power_entity",
        "battery_power_entity",
        "grid_power_entity",
        "grid_to_battery_power_entity",
        "grid_to_battery_ready",
        "grid_to_battery_quality",
        "physical_power_source_ages_seconds",
        "physical_power_freshness",
        "physical_power_provenance",
        "migration_version",
        "migration_result",
        "legacy_aliases",
        "pv_to_battery_efficiency",
        "pv_to_battery_efficiency_source",
        "battery_to_home_efficiency",
        "battery_to_home_efficiency_source",
    }
)

SharedScalar: TypeAlias = float | int | str | bool | None
LoadModelProvider: TypeAlias = Callable[
    [datetime], "SharedLoadModelSnapshot | Mapping[str, Any]"
]


@dataclass(frozen=True, slots=True)
class SharedSample:
    """One bounded value with freshness and source evidence."""

    value: SharedScalar
    entity_id: str | None
    source_entity_ids: tuple[str, ...]
    selector_entity_id: str | None
    reported_at: datetime | None
    age_seconds: float | None
    fresh: bool
    reason: str
    quality: str
    provenance: str
    usefulness: Mapping[str, Any] | None = None

    def as_public_dict(self) -> dict[str, Any]:
        """Return a fixed, JSON-safe public projection."""

        value = self.value
        if isinstance(value, float):
            value = round(value, 6)
        public = {
            "value": value,
            "entity_id": self.entity_id,
            "source_entity_ids": list(self.source_entity_ids),
            "selector_entity_id": self.selector_entity_id,
            "reported_at": (
                self.reported_at.isoformat() if self.reported_at is not None else None
            ),
            "age_seconds": (
                round(self.age_seconds, 3)
                if self.age_seconds is not None and isfinite(self.age_seconds)
                else None
            ),
            "fresh": self.fresh,
            "reason": self.reason,
            "quality": self.quality,
            "provenance": self.provenance,
        }
        if self.usefulness is not None:
            public["usefulness"] = dict(self.usefulness)
        return public


@dataclass(frozen=True, slots=True)
class SharedSystemInputs:
    """Installation-wide values resolved for one integration entry."""

    inverter_model: SharedSample
    battery_capacity_kwh: SharedSample
    total_capacity_kwh: SharedSample
    battery_soc_percent: SharedSample
    self_use_reserve_soc_percent: SharedSample
    hardware_minimum_soc_percent: SharedSample
    hardware_maximum_soc_percent: SharedSample
    inverter_count: SharedSample
    inverter_rated_power_each_kw: SharedSample
    system_rated_power_kw: SharedSample

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "inverter_model": self.inverter_model.as_public_dict(),
            "battery_capacity_kwh": self.battery_capacity_kwh.as_public_dict(),
            "total_capacity_kwh": self.total_capacity_kwh.as_public_dict(),
            "battery_soc_percent": self.battery_soc_percent.as_public_dict(),
            "self_use_reserve_soc_percent": (
                self.self_use_reserve_soc_percent.as_public_dict()
            ),
            "hardware_minimum_soc_percent": (
                self.hardware_minimum_soc_percent.as_public_dict()
            ),
            "hardware_maximum_soc_percent": (
                self.hardware_maximum_soc_percent.as_public_dict()
            ),
            "inverter_count": self.inverter_count.as_public_dict(),
            "inverter_rated_power_each_kw": (
                self.inverter_rated_power_each_kw.as_public_dict()
            ),
            "system_rated_power_kw": self.system_rated_power_kw.as_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class SharedForecastInputs:
    """Resolved forecast states without optimizer-specific interpretation."""

    today: SharedSample
    tomorrow: SharedSample
    day3: SharedSample
    remaining_today: SharedSample

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "today": self.today.as_public_dict(),
            "tomorrow": self.tomorrow.as_public_dict(),
            "day3": self.day3.as_public_dict(),
            "remaining_today": self.remaining_today.as_public_dict(),
        }


@dataclass(frozen=True, slots=True)
class SharedLoadModelSnapshot:
    """Bounded LOAD-model payload supplied by the current entry's RCE sensor."""

    average_daily_home_load_kwh: float | None = None
    average_night_home_load_kwh: float | None = None
    provisional_daily_load_projection_kwh: float | None = None
    daily_history_days: int = 0
    night_history_days: int = 0
    daily_totals_kwh: tuple[float, ...] = ()
    daily_total_dates: tuple[str, ...] = ()
    average_profile_30m_kwh: tuple[float, ...] = ()
    weekday_profile_30m_kwh: tuple[float, ...] = ()
    weekend_profile_30m_kwh: tuple[float, ...] = ()
    weekday_profile_days: int = 0
    weekend_profile_days: int = 0
    profile_history_days: int = 0
    daily_coverage_ratio: float = 0.0
    profile_coverage_ratio: float = 0.0
    current_day_energy_kwh: float | None = None
    current_day_observed_at: datetime | None = None
    persistence_delta_kw: float = 0.0
    persistence_observed_at: datetime | None = None
    persistence_sample_count: int = 0
    model_schema: str = "unavailable"
    model_quality: str = "unavailable"
    generated_at: datetime | None = None
    ready: bool = False
    fallback_currently_used: bool = False
    source: str = "unavailable"
    night_start_minute: int = 22 * 60
    night_end_minute: int = 6 * 60

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "night_start_minute": self.night_start_minute,
            "night_end_minute": self.night_end_minute,
            "average_daily_home_load_kwh": self.average_daily_home_load_kwh,
            "average_night_home_load_kwh": self.average_night_home_load_kwh,
            "provisional_daily_load_projection_kwh": (
                self.provisional_daily_load_projection_kwh
            ),
            "daily_history_days": self.daily_history_days,
            "night_history_days": self.night_history_days,
            "daily_totals_kwh": list(self.daily_totals_kwh),
            "daily_total_dates": list(self.daily_total_dates),
            "average_profile_30m_kwh": list(self.average_profile_30m_kwh),
            "weekday_profile_30m_kwh": list(self.weekday_profile_30m_kwh),
            "weekend_profile_30m_kwh": list(self.weekend_profile_30m_kwh),
            "weekday_profile_days": self.weekday_profile_days,
            "weekend_profile_days": self.weekend_profile_days,
            "profile_history_days": self.profile_history_days,
            "daily_coverage_ratio": self.daily_coverage_ratio,
            "profile_coverage_ratio": self.profile_coverage_ratio,
            "current_day_energy_kwh": self.current_day_energy_kwh,
            "current_day_observed_at": (
                self.current_day_observed_at.isoformat()
                if self.current_day_observed_at is not None
                else None
            ),
            "persistence_delta_kw": self.persistence_delta_kw,
            "persistence_observed_at": (
                self.persistence_observed_at.isoformat()
                if self.persistence_observed_at is not None
                else None
            ),
            "persistence_sample_count": self.persistence_sample_count,
            "model_schema": self.model_schema,
            "model_quality": self.model_quality,
            "generated_at": (
                self.generated_at.isoformat() if self.generated_at is not None else None
            ),
            "ready": self.ready,
            "fallback_currently_used": self.fallback_currently_used,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class SharedLoadInputs:
    """Shared LOAD helper and exact-entry Recorder model projection."""

    fallback_daily_home_load_kwh: SharedSample
    average_daily_home_load_kwh: float | None
    average_night_home_load_kwh: float | None
    provisional_daily_load_projection_kwh: float | None
    daily_history_days: int
    night_history_days: int
    daily_totals_kwh: tuple[float, ...]
    daily_total_dates: tuple[str, ...]
    average_profile_30m_kwh: tuple[float, ...]
    weekday_profile_30m_kwh: tuple[float, ...]
    weekend_profile_30m_kwh: tuple[float, ...]
    weekday_profile_days: int
    weekend_profile_days: int
    profile_history_days: int
    daily_coverage_ratio: float
    profile_coverage_ratio: float
    current_day_energy_kwh: float | None
    current_day_observed_at: datetime | None
    persistence_delta_kw: float
    persistence_observed_at: datetime | None
    persistence_sample_count: int
    model_schema: str
    model_quality: str
    generated_at: datetime | None
    ready: bool
    fallback_currently_used: bool
    source: str
    night_start_minute: int = 22 * 60
    night_end_minute: int = 6 * 60

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "night_start_minute": self.night_start_minute,
            "night_end_minute": self.night_end_minute,
            "fallback_daily_home_load_kwh": (
                self.fallback_daily_home_load_kwh.as_public_dict()
            ),
            "average_daily_home_load_kwh": self.average_daily_home_load_kwh,
            "average_night_home_load_kwh": self.average_night_home_load_kwh,
            "provisional_daily_load_projection_kwh": (
                self.provisional_daily_load_projection_kwh
            ),
            "daily_history_days": self.daily_history_days,
            "night_history_days": self.night_history_days,
            "daily_totals_kwh": list(self.daily_totals_kwh),
            "daily_total_dates": list(self.daily_total_dates),
            "average_profile_30m_kwh": list(self.average_profile_30m_kwh),
            "weekday_profile_30m_kwh": list(self.weekday_profile_30m_kwh),
            "weekend_profile_30m_kwh": list(self.weekend_profile_30m_kwh),
            "weekday_profile_days": self.weekday_profile_days,
            "weekend_profile_days": self.weekend_profile_days,
            "profile_history_days": self.profile_history_days,
            "daily_coverage_ratio": self.daily_coverage_ratio,
            "profile_coverage_ratio": self.profile_coverage_ratio,
            "current_day_energy_kwh": self.current_day_energy_kwh,
            "current_day_observed_at": (
                self.current_day_observed_at.isoformat()
                if self.current_day_observed_at is not None
                else None
            ),
            "persistence_delta_kw": self.persistence_delta_kw,
            "persistence_observed_at": (
                self.persistence_observed_at.isoformat()
                if self.persistence_observed_at is not None
                else None
            ),
            "persistence_sample_count": self.persistence_sample_count,
            "model_schema": self.model_schema,
            "model_quality": self.model_quality,
            "generated_at": (
                self.generated_at.isoformat() if self.generated_at is not None else None
            ),
            "ready": self.ready,
            "fallback_currently_used": self.fallback_currently_used,
            "source": self.source,
        }


@dataclass(frozen=True, slots=True)
class SharedBMSInputs:
    """Physical BMS capabilities with no optimizer fallback."""

    voltage_v: SharedSample
    maximum_charge_current_a: SharedSample
    maximum_discharge_current_a: SharedSample
    maximum_charge_power_kw: SharedSample
    maximum_discharge_power_kw: SharedSample

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "voltage_v": self.voltage_v.as_public_dict(),
            "maximum_charge_current_a": (
                self.maximum_charge_current_a.as_public_dict()
            ),
            "maximum_discharge_current_a": (
                self.maximum_discharge_current_a.as_public_dict()
            ),
            "maximum_charge_power_kw": (
                self.maximum_charge_power_kw.as_public_dict()
            ),
            "maximum_discharge_power_kw": (
                self.maximum_discharge_power_kw.as_public_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class SharedEfficiencyInputs:
    """Two distinct baseline efficiencies; no cross-flow equivalence claim."""

    pv_to_battery_efficiency: SharedSample
    battery_to_home_efficiency: SharedSample

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "pv_to_battery_efficiency": (
                self.pv_to_battery_efficiency.as_public_dict()
            ),
            "battery_to_home_efficiency": (
                self.battery_to_home_efficiency.as_public_dict()
            ),
        }


@dataclass(frozen=True, slots=True)
class SharedGCFInputs:
    """Generation-control readback cohort and verified export meaning."""

    hardware_readback_supported: SharedSample
    generation: SharedSample
    enable_code: SharedSample
    maximum_export_power_percent: SharedSample
    cohort_skew_seconds: float | None
    gcf_readback_ready: bool
    zero_export_confirmed: bool
    export_allowed: bool | None

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "hardware_readback_supported": (
                self.hardware_readback_supported.as_public_dict()
            ),
            "generation": self.generation.as_public_dict(),
            "enable_code": self.enable_code.as_public_dict(),
            "maximum_export_power_percent": (
                self.maximum_export_power_percent.as_public_dict()
            ),
            "cohort_skew_seconds": (
                round(self.cohort_skew_seconds, 3)
                if self.cohort_skew_seconds is not None
                else None
            ),
            "gcf_readback_ready": self.gcf_readback_ready,
            "zero_export_confirmed": self.zero_export_confirmed,
            "export_allowed": self.export_allowed,
        }


@dataclass(frozen=True, slots=True)
class SharedPhysicalFlowInputs:
    """Distinct physical flows using exact entry-local proxy identities."""

    pv_power_kw: SharedSample
    home_load_power_kw: SharedSample
    system_load_power_kw: SharedSample
    battery_power_kw: SharedSample
    grid_power_kw: SharedSample
    grid_to_battery_power: SharedSample
    grid_to_battery_ready: bool

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "pv_power_kw": self.pv_power_kw.as_public_dict(),
            "home_load_power_kw": self.home_load_power_kw.as_public_dict(),
            "system_load_power_kw": self.system_load_power_kw.as_public_dict(),
            "battery_power_kw": self.battery_power_kw.as_public_dict(),
            "grid_power_kw": self.grid_power_kw.as_public_dict(),
            "grid_to_battery_power": self.grid_to_battery_power.as_public_dict(),
            "grid_to_battery_ready": self.grid_to_battery_ready,
        }


@dataclass(frozen=True, slots=True)
class EMSSharedInputsSnapshot:
    """One immutable, bounded shared-input revision."""

    schema_version: int
    config_entry_id: str
    captured_at: datetime
    revision: int
    quality: str
    issues: tuple[str, ...]
    system: SharedSystemInputs
    forecast: SharedForecastInputs
    load: SharedLoadInputs
    bms: SharedBMSInputs
    efficiency: SharedEfficiencyInputs
    gcf: SharedGCFInputs
    power: SharedPhysicalFlowInputs
    migration: Mapping[str, Any]

    @staticmethod
    def _fresh_value(sample: SharedSample) -> SharedScalar:
        return sample.value if sample.fresh else None

    def flat_public_attributes(self) -> dict[str, Any]:
        """Return the exact flat A0/UI compatibility projection."""

        forecast_samples = (
            self.forecast.today,
            self.forecast.tomorrow,
            self.forecast.day3,
            self.forecast.remaining_today,
        )
        reported = [
            sample.reported_at
            for sample in forecast_samples
            if sample.reported_at is not None
        ]
        fresh_forecasts = sum(sample.fresh for sample in forecast_samples)
        if fresh_forecasts == len(forecast_samples):
            forecast_quality = "complete"
        elif fresh_forecasts:
            forecast_quality = "partial"
        elif any(sample.quality == "stale" for sample in forecast_samples):
            forecast_quality = "stale"
        else:
            forecast_quality = "unavailable"
        forecast_provenances = {
            sample.provenance
            for sample in forecast_samples
            if sample.provenance != "unavailable"
        }
        if forecast_provenances == {"new_helper"}:
            forecast_provenance = "shared_forecast_helpers"
        elif forecast_provenances == {"legacy_helper"}:
            forecast_provenance = "legacy_forecast_helpers"
        elif forecast_provenances == {"automatic_candidate"}:
            forecast_provenance = "automatic_candidates"
        elif forecast_provenances:
            forecast_provenance = "mixed_sources"
        else:
            forecast_provenance = "unavailable"

        bms_samples = {
            "battery_voltage": self.bms.voltage_v,
            "maximum_charge_current": self.bms.maximum_charge_current_a,
            "maximum_discharge_current": self.bms.maximum_discharge_current_a,
            "maximum_charge_power": self.bms.maximum_charge_power_kw,
            "maximum_discharge_power": self.bms.maximum_discharge_power_kw,
        }
        physical_samples = {
            "pv": self.power.pv_power_kw,
            "home_load": self.power.home_load_power_kw,
            "system_load": self.power.system_load_power_kw,
            "battery": self.power.battery_power_kw,
            "grid": self.power.grid_power_kw,
        }
        physical_fresh_count = sum(
            sample.fresh for sample in physical_samples.values()
        )
        if physical_fresh_count == len(physical_samples):
            physical_quality = "current"
        elif physical_fresh_count:
            physical_quality = "partial"
        elif any(sample.quality == "stale" for sample in physical_samples.values()):
            physical_quality = "stale"
        else:
            physical_quality = "unavailable"

        rated_source = {
            "device_model": "source_device_model",
            "new_helper": "configured_shared_helper",
            "legacy_helper": "legacy_helper",
        }.get(
            self.system.inverter_rated_power_each_kw.provenance,
            self.system.inverter_rated_power_each_kw.provenance,
        )
        migration = dict(self.migration)
        flat = {
            "inverter_model": self._fresh_value(self.system.inverter_model),
            "inverter_model_source": (
                self.system.inverter_model.provenance
                if self.system.inverter_model.fresh
                else "unavailable"
            ),
            "inverter_rated_power_each_kw": self._fresh_value(
                self.system.inverter_rated_power_each_kw
            ),
            "inverter_rated_power_source": rated_source,
            "inverter_count": self._fresh_value(self.system.inverter_count),
            "inverter_count_source": (
                "physical_fc03"
                if self.system.inverter_count.entity_id is not None
                else "unavailable"
            ),
            "system_rated_power_kw": self._fresh_value(
                self.system.system_rated_power_kw
            ),
            "system_rated_power_source": (
                "rated_power_each_x_inverter_count"
                if self.system.system_rated_power_kw.fresh
                else "unavailable"
            ),
            "battery_capacity_kwh": self._fresh_value(
                self.system.battery_capacity_kwh
            ),
            "battery_capacity_source": (
                "physical_capacity_register"
                if self.system.battery_capacity_kwh.entity_id is not None
                else "unavailable"
            ),
            "reserve_soc_floor_percent": self._fresh_value(
                self.system.self_use_reserve_soc_percent
            ),
            "hardware_minimum_soc_percent": self._fresh_value(
                self.system.hardware_minimum_soc_percent
            ),
            "hardware_maximum_soc_percent": self._fresh_value(
                self.system.hardware_maximum_soc_percent
            ),
            "forecast_today_entity": self.forecast.today.entity_id,
            "forecast_tomorrow_entity": self.forecast.tomorrow.entity_id,
            "forecast_day_3_entity": self.forecast.day3.entity_id,
            "forecast_remaining_today_entity": (
                self.forecast.remaining_today.entity_id
            ),
            "forecast_today_ready": self.forecast.today.fresh,
            "forecast_tomorrow_ready": self.forecast.tomorrow.fresh,
            "forecast_day_3_ready": self.forecast.day3.fresh,
            "forecast_remaining_today_ready": self.forecast.remaining_today.fresh,
            "forecast_last_updated": (
                max(reported).isoformat() if reported else None
            ),
            "forecast_quality": forecast_quality,
            "forecast_provenance": forecast_provenance,
            "forecast_source_ages_seconds": {
                "today": self.forecast.today.age_seconds,
                "tomorrow": self.forecast.tomorrow.age_seconds,
                "day_3": self.forecast.day3.age_seconds,
                "remaining_today": self.forecast.remaining_today.age_seconds,
            },
            "forecast_freshness": {
                "today": self.forecast.today.fresh,
                "tomorrow": self.forecast.tomorrow.fresh,
                "day_3": self.forecast.day3.fresh,
                "remaining_today": self.forecast.remaining_today.fresh,
            },
            "forecast_source_freshness": {
                "today": _usefulness_field(self.forecast.today, "source_fresh"),
                "tomorrow": _usefulness_field(
                    self.forecast.tomorrow,
                    "source_fresh",
                ),
                "day_3": _usefulness_field(self.forecast.day3, "source_fresh"),
                "remaining_today": _usefulness_field(
                    self.forecast.remaining_today,
                    "source_fresh",
                ),
            },
            "forecast_usability_modes": {
                "today": _usefulness_field(self.forecast.today, "mode"),
                "tomorrow": _usefulness_field(self.forecast.tomorrow, "mode"),
                "day_3": _usefulness_field(self.forecast.day3, "mode"),
                "remaining_today": _usefulness_field(
                    self.forecast.remaining_today,
                    "mode",
                ),
            },
            "forecast_last_success": _usefulness_field(
                self.forecast.today,
                "last_success_at",
            ),
            "forecast_next_update": _usefulness_field(
                self.forecast.today,
                "next_update_at",
            ),
            "forecast_valid_until": _usefulness_field(
                self.forecast.today,
                "valid_until",
            ),
            "load_model_ready": self.load.ready,
            "load_model_source": self.load.source,
            "load_history_days": self.load.daily_history_days,
            "load_history_complete": bool(
                self.load.ready and self.load.daily_history_days >= 3
            ),
            "load_profile_history_days": self.load.profile_history_days,
            "load_daily_coverage_ratio": self.load.daily_coverage_ratio,
            "load_profile_coverage_ratio": self.load.profile_coverage_ratio,
            "average_daily_home_load_kwh": self.load.average_daily_home_load_kwh,
            "weekday_profile_ready": bool(
                len(self.load.weekday_profile_30m_kwh) == _PROFILE_SLOT_COUNT
                and self.load.weekday_profile_days > 0
            ),
            "weekend_profile_ready": bool(
                len(self.load.weekend_profile_30m_kwh) == _PROFILE_SLOT_COUNT
                and self.load.weekend_profile_days > 0
            ),
            "load_current_day_energy_kwh": self.load.current_day_energy_kwh,
            "load_current_day_observed_at": (
                self.load.current_day_observed_at.isoformat()
                if self.load.current_day_observed_at is not None
                else None
            ),
            "load_persistence_delta_kw": self.load.persistence_delta_kw,
            "load_persistence_observed_at": (
                self.load.persistence_observed_at.isoformat()
                if self.load.persistence_observed_at is not None
                else None
            ),
            "load_persistence_sample_count": self.load.persistence_sample_count,
            "load_model_schema": self.load.model_schema,
            "load_model_quality": self.load.model_quality,
            "fallback_daily_home_load_kwh": self._fresh_value(
                self.load.fallback_daily_home_load_kwh
            ),
            "fallback_currently_used": self.load.fallback_currently_used,
            "load_model_generated_at": (
                self.load.generated_at.isoformat()
                if self.load.generated_at is not None
                else None
            ),
            "bms_ready": all(sample.fresh for sample in bms_samples.values()),
            "bms_provenance": (
                "physical_bms"
                if any(sample.entity_id is not None for sample in bms_samples.values())
                else "unavailable"
            ),
            "battery_voltage": self._fresh_value(self.bms.voltage_v),
            "maximum_charge_current": self._fresh_value(
                self.bms.maximum_charge_current_a
            ),
            "maximum_discharge_current": self._fresh_value(
                self.bms.maximum_discharge_current_a
            ),
            "maximum_charge_power_kw": self._fresh_value(
                self.bms.maximum_charge_power_kw
            ),
            "maximum_discharge_power_kw": self._fresh_value(
                self.bms.maximum_discharge_power_kw
            ),
            "bms_source_ages_seconds": {
                key: sample.age_seconds for key, sample in bms_samples.items()
            },
            "bms_freshness": {
                key: sample.fresh for key, sample in bms_samples.items()
            },
            "pv_to_battery_efficiency": self._fresh_value(
                self.efficiency.pv_to_battery_efficiency
            ),
            "pv_to_battery_efficiency_source": (
                self.efficiency.pv_to_battery_efficiency.provenance
            ),
            "battery_to_home_efficiency": self._fresh_value(
                self.efficiency.battery_to_home_efficiency
            ),
            "battery_to_home_efficiency_source": (
                self.efficiency.battery_to_home_efficiency.provenance
            ),
            "gcf_readback_ready": self.gcf.gcf_readback_ready,
            "zero_export_confirmed": self.gcf.zero_export_confirmed,
            "export_allowed": self.gcf.export_allowed,
            "export_limit_percent": self._fresh_value(
                self.gcf.maximum_export_power_percent
            ),
            "gcf_readback_generation": self._fresh_value(self.gcf.generation),
            "gcf_provenance": (
                "physical_fc03"
                if any(
                    sample.entity_id is not None
                    for sample in (
                        self.gcf.hardware_readback_supported,
                        self.gcf.generation,
                        self.gcf.enable_code,
                        self.gcf.maximum_export_power_percent,
                    )
                )
                else "unavailable"
            ),
            "gcf_source_ages_seconds": {
                "supported": self.gcf.hardware_readback_supported.age_seconds,
                "generation": self.gcf.generation.age_seconds,
                "enable": self.gcf.enable_code.age_seconds,
                "limit": self.gcf.maximum_export_power_percent.age_seconds,
            },
            "gcf_freshness": {
                "supported": self.gcf.hardware_readback_supported.fresh,
                "generation": self.gcf.generation.fresh,
                "enable": self.gcf.enable_code.fresh,
                "limit": self.gcf.maximum_export_power_percent.fresh,
            },
            "physical_power_ready": physical_fresh_count == len(physical_samples),
            "physical_power_quality": physical_quality,
            "pv_power_entity": self.power.pv_power_kw.entity_id,
            "home_load_power_entity": self.power.home_load_power_kw.entity_id,
            "home_load_power_source_entities": list(
                self.power.home_load_power_kw.source_entity_ids
            ),
            "system_load_power_entity": self.power.system_load_power_kw.entity_id,
            "battery_power_entity": self.power.battery_power_kw.entity_id,
            "grid_power_entity": self.power.grid_power_kw.entity_id,
            "grid_to_battery_power_entity": (
                self.power.grid_to_battery_power.entity_id
            ),
            "grid_to_battery_ready": self.power.grid_to_battery_ready,
            "grid_to_battery_quality": self.power.grid_to_battery_power.quality,
            "physical_power_source_ages_seconds": {
                key: sample.age_seconds for key, sample in physical_samples.items()
            },
            "physical_power_freshness": {
                key: sample.fresh for key, sample in physical_samples.items()
            },
            "physical_power_provenance": {
                key: sample.provenance for key, sample in physical_samples.items()
            },
            "migration_version": migration.get(
                "version", SHARED_INPUTS_MIGRATION_VERSION
            ),
            "migration_result": migration.get("result", "unknown"),
            "legacy_aliases": migration.get("legacy_aliases"),
        }
        if set(flat) != REQUIRED_FLAT_ATTRIBUTE_KEYS:
            raise RuntimeError("shared EMS flat schema drift")
        return flat

    def as_public_dict(self) -> dict[str, Any]:
        """Serialize only the fixed public schema."""

        nested = {
            "schema_version": self.schema_version,
            "config_entry_id": self.config_entry_id,
            "captured_at": self.captured_at.isoformat(),
            "revision": self.revision,
            "quality": self.quality,
            "issues": list(self.issues),
            "system": self.system.as_public_dict(),
            "forecast": self.forecast.as_public_dict(),
            "load": self.load.as_public_dict(),
            "bms": self.bms.as_public_dict(),
            "efficiency": self.efficiency.as_public_dict(),
            "gcf": self.gcf.as_public_dict(),
            "power": self.power.as_public_dict(),
            "migration": dict(self.migration),
        }
        nested.update(self.flat_public_attributes())
        return nested


@dataclass(frozen=True, slots=True)
class _NumericSourceSpec:
    translation_key: str
    minimum: float | None
    maximum: float | None
    power_unit: bool = False
    domain: str = "sensor"


_NUMERIC_SOURCES = {
    "battery_capacity": _NumericSourceSpec("battery_capacity", 0.001, 10_000.0),
    "total_capacity": _NumericSourceSpec("total_capacity", 0.001, 10_000.0),
    "battery_soc": _NumericSourceSpec("overview_battery_soc", 0.0, 100.0),
    "self_use_reserve": _NumericSourceSpec(
        "ems_self_use_soc_readback", 10.0, 100.0
    ),
    "hardware_minimum_soc": _NumericSourceSpec(
        "minimum_soc", 10.0, 90.0, domain="number"
    ),
    "hardware_maximum_soc": _NumericSourceSpec(
        "maximum_soc", 10.0, 100.0, domain="number"
    ),
    "inverter_count": _NumericSourceSpec(
        "number_of_machines_master_and_slave", 1.0, 10.0
    ),
    "bms_voltage": _NumericSourceSpec("battery_voltage_bms", 0.0, 2_000.0),
    "bms_charge_current": _NumericSourceSpec(
        "maximum_charge_current", 0.0, 10_000.0
    ),
    "bms_discharge_current": _NumericSourceSpec(
        "maximum_discharge_current", 0.0, 10_000.0
    ),
    "gcf_supported": _NumericSourceSpec(
        "ems_verified_hardware_readback_supported", 0.0, 1.0
    ),
    "gcf_generation": _NumericSourceSpec(
        "gcf_control_readback_generation", 1.0, 16_000_000.0
    ),
    "gcf_enable": _NumericSourceSpec("gcf_enable_readback_code", 0.0, 1.0),
    "gcf_limit": _NumericSourceSpec(
        "gcf_maximum_export_power_readback", 0.0, 200.0
    ),
    "pv_power": _NumericSourceSpec(
        "overview_pv_total_power", 0.0, _MAX_POWER_KW, True
    ),
    "system_load_power": _NumericSourceSpec(
        "overview_load_active_power", 0.0, _MAX_POWER_KW, True
    ),
    "battery_power": _NumericSourceSpec(
        "overview_battery_power", -_MAX_POWER_KW, _MAX_POWER_KW, True
    ),
    "grid_power": _NumericSourceSpec(
        "overview_grid_total_active_power", -_MAX_POWER_KW, _MAX_POWER_KW, True
    ),
    "home_load_l1": _NumericSourceSpec(
        "load_power_l1n",
        _HOME_LOAD_NEGATIVE_RESIDUAL_MIN_KW,
        _SIGNED_WORD_POWER_MAX_KW,
        True,
    ),
    "home_load_l2": _NumericSourceSpec(
        "load_power_l2n",
        _HOME_LOAD_NEGATIVE_RESIDUAL_MIN_KW,
        _SIGNED_WORD_POWER_MAX_KW,
        True,
    ),
    "home_load_l3": _NumericSourceSpec(
        "load_power_l3n",
        _HOME_LOAD_NEGATIVE_RESIDUAL_MIN_KW,
        _SIGNED_WORD_POWER_MAX_KW,
        True,
    ),
}


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("shared EMS timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _bounded_entity_id(value: str | None) -> str | None:
    if value is None or len(value) > _MAX_ENTITY_ID_LENGTH:
        return None
    normalized = value.strip().casefold()
    return normalized if _ENTITY_ID_RE.fullmatch(normalized) else None


def _quality_for_reason(reason: str, fresh: bool) -> str:
    if fresh:
        return "complete"
    if reason in {"stale", "future_timestamp"}:
        return "stale"
    if reason == "provider_missing":
        return "provider_missing"
    if reason.startswith("incomplete_"):
        return "partial"
    return "unavailable"


def _usefulness_field(sample: SharedSample, key: str) -> Any:
    """Read one JSON-safe forecast-usefulness field."""

    if not isinstance(sample.usefulness, Mapping):
        return None
    return sample.usefulness.get(key)


def _empty_sample(
    reason: str,
    *,
    provenance: str = "unavailable",
    entity_id: str | None = None,
    source_entity_ids: Sequence[str] = (),
    selector_entity_id: str | None = None,
) -> SharedSample:
    bounded_sources = tuple(
        source
        for source in source_entity_ids[:_MAX_PUBLIC_SOURCE_IDS]
        if _bounded_entity_id(source) is not None
    )
    return SharedSample(
        value=None,
        entity_id=_bounded_entity_id(entity_id),
        source_entity_ids=bounded_sources,
        selector_entity_id=_bounded_entity_id(selector_entity_id),
        reported_at=None,
        age_seconds=None,
        fresh=False,
        reason=reason,
        quality=_quality_for_reason(reason, False),
        provenance=provenance,
    )


def _setting_sample(
    value: float,
    *,
    now: datetime,
    state: Any,
    entity_id: str,
    provenance: str,
) -> SharedSample:
    return SharedSample(
        value=value,
        entity_id=entity_id,
        source_entity_ids=(entity_id,),
        selector_entity_id=entity_id,
        reported_at=state_reported_at(state),
        age_seconds=state_age_seconds(state, now),
        fresh=True,
        reason="configured",
        quality="complete",
        provenance=provenance,
    )


def _power_scale(state: Any) -> float:
    unit = str(getattr(state, "attributes", {}).get("unit_of_measurement", ""))
    return 1.0 if unit.casefold() == "kw" else 0.001


def _exact_registry_entity_id(
    registry: Any,
    *,
    config_entry_id: str,
    translation_key: str,
    domain: str,
) -> tuple[str | None, str | None]:
    """Resolve exactly one integration-owned proxy for this config entry."""

    unique_id = f"{config_entry_id}_{translation_key}"
    entities = getattr(registry, "entities", None)
    if entities is not None:
        candidates = [
            item.entity_id
            for item in entities.values()
            if getattr(item, "platform", None) == DOMAIN
            and getattr(item, "config_entry_id", None) == config_entry_id
            and getattr(item, "unique_id", None) == unique_id
            and getattr(item, "translation_key", None) == translation_key
            and getattr(item, "domain", None) == domain
        ]
        if len(candidates) > 1:
            return None, "ambiguous_entry"
        if len(candidates) == 1:
            return candidates[0], None
        return None, "source_unavailable"

    entity_id = registry.async_get_entity_id(domain, DOMAIN, unique_id)
    item = registry.async_get(entity_id) if entity_id is not None else None
    if (
        item is None
        or getattr(item, "domain", None) != domain
        or getattr(item, "platform", None) != DOMAIN
        or getattr(item, "config_entry_id", None) != config_entry_id
        or getattr(item, "unique_id", None) != unique_id
        or getattr(item, "translation_key", None) != translation_key
    ):
        return None, "source_unavailable"
    return entity_id, None


def strict_rated_power_from_model(model: Any) -> float | None:
    """Return kW only for an exact supported HIT model identifier."""

    if not isinstance(model, str):
        return None
    match = _STRICT_MODEL_RE.fullmatch(model.strip())
    return float(match.group(1)) if match is not None else None


def _rated_power_from_helper_state(state: Any) -> tuple[float | None, str]:
    if state is None:
        return None, "missing"
    raw = str(getattr(state, "state", "")).strip()
    normalized = raw.casefold()
    if normalized in _UNAVAILABLE:
        return None, "unavailable"
    if normalized in _AUTOMATIC_RATED_POWER_STATES:
        return None, "automatic"
    match = _RATED_POWER_RE.fullmatch(raw)
    if match is None:
        return None, "invalid_option"
    value = float(match.group(1))
    return (value, "configured") if value in _ALLOWED_RATED_POWER_KW else (None, "invalid_option")


def _bounded_number(value: Any, *, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) and minimum <= parsed <= maximum else None


def _bounded_count(value: Any, maximum: int) -> int | None:
    parsed = _bounded_number(value, minimum=0.0, maximum=float(maximum))
    if parsed is None or abs(parsed - round(parsed)) > 1e-9:
        return None
    return int(round(parsed))


def _bounded_profile(value: Any) -> tuple[float, ...] | None:
    if value is None or value == ():
        return ()
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return None
    if len(value) != _PROFILE_SLOT_COUNT:
        return None
    parsed = tuple(
        _bounded_number(item, minimum=0.0, maximum=_MAX_ENERGY_KWH)
        for item in value
    )
    return None if any(item is None for item in parsed) else tuple(parsed)  # type: ignore[arg-type]


def _bounded_load_model_snapshot(
    value: Any,
    *,
    now: datetime,
) -> SharedLoadModelSnapshot:
    """Validate an entry-local provider without silently clamping it."""

    if isinstance(value, SharedLoadModelSnapshot):
        raw: Mapping[str, Any] = {
            field: getattr(value, field)
            for field in SharedLoadModelSnapshot.__dataclass_fields__
        }
    elif isinstance(value, Mapping):
        raw = value
    else:
        return SharedLoadModelSnapshot(source="invalid_provider")

    daily_raw = raw.get("average_daily_home_load_kwh")
    daily = (
        _bounded_number(daily_raw, minimum=0.0, maximum=_MAX_ENERGY_KWH)
        if daily_raw is not None
        else None
    )
    night_raw = raw.get("average_night_home_load_kwh")
    night = (
        _bounded_number(night_raw, minimum=0.0, maximum=_MAX_ENERGY_KWH)
        if night_raw is not None
        else None
    )
    provisional_raw = raw.get("provisional_daily_load_projection_kwh")
    provisional = (
        _bounded_number(
            provisional_raw,
            minimum=0.0,
            maximum=_MAX_ENERGY_KWH,
        )
        if provisional_raw is not None
        else None
    )
    optional_number_invalid = bool(
        (daily_raw is not None and daily is None)
        or (night_raw is not None and night is None)
        or (provisional_raw is not None and provisional is None)
    )
    daily_days = _bounded_count(raw.get("daily_history_days", 0), _MAX_DAILY_TOTALS)
    night_days = _bounded_count(raw.get("night_history_days", 0), _MAX_DAILY_TOTALS)
    weekday_days = _bounded_count(raw.get("weekday_profile_days", 0), _MAX_DAILY_TOTALS)
    weekend_days = _bounded_count(raw.get("weekend_profile_days", 0), _MAX_DAILY_TOTALS)

    raw_totals = raw.get("daily_totals_kwh", ())
    if isinstance(raw_totals, Mapping):
        raw_totals = tuple(value for _, value in sorted(raw_totals.items()))
    if isinstance(raw_totals, (str, bytes)) or not isinstance(raw_totals, Sequence):
        totals: tuple[float, ...] | None = None
    elif len(raw_totals) > _MAX_DAILY_TOTALS:
        totals = None
    else:
        parsed_totals = tuple(
            _bounded_number(item, minimum=0.0, maximum=_MAX_ENERGY_KWH)
            for item in raw_totals
        )
        totals = (
            None
            if any(item is None for item in parsed_totals)
            else tuple(parsed_totals)  # type: ignore[arg-type]
        )

    raw_dates = raw.get("daily_total_dates", ())
    if (
        isinstance(raw_dates, (str, bytes))
        or not isinstance(raw_dates, Sequence)
        or len(raw_dates) > _MAX_DAILY_TOTALS
    ):
        total_dates: tuple[str, ...] | None = None
    else:
        total_dates = tuple(str(item) for item in raw_dates)
        try:
            for item in total_dates:
                datetime.fromisoformat(item)
        except ValueError:
            total_dates = None
        if total_dates and totals is not None and len(total_dates) != len(totals):
            total_dates = None

    average_profile = _bounded_profile(raw.get("average_profile_30m_kwh", ()))
    weekday_profile = _bounded_profile(raw.get("weekday_profile_30m_kwh", ()))
    weekend_profile = _bounded_profile(raw.get("weekend_profile_30m_kwh", ()))
    profile_days = _bounded_count(raw.get("profile_history_days", 0), _MAX_DAILY_TOTALS)
    daily_coverage = _bounded_number(
        raw.get("daily_coverage_ratio", 0.0), minimum=0.0, maximum=1.0
    )
    profile_coverage = _bounded_number(
        raw.get("profile_coverage_ratio", 0.0), minimum=0.0, maximum=1.0
    )
    current_energy_raw = raw.get("current_day_energy_kwh")
    current_energy = (
        _bounded_number(current_energy_raw, minimum=0.0, maximum=_MAX_ENERGY_KWH)
        if current_energy_raw is not None
        else None
    )
    persistence_delta = _bounded_number(
        raw.get("persistence_delta_kw", 0.0), minimum=-3.0, maximum=3.0
    )
    persistence_samples = _bounded_count(
        raw.get("persistence_sample_count", 0), 1_000
    )

    def optional_timestamp(field: str) -> tuple[datetime | None, bool]:
        value = raw.get(field)
        if value is None:
            return None, False
        if not isinstance(value, datetime):
            return None, True
        try:
            return _aware_utc(value), False
        except ValueError:
            return None, True

    current_observed_at, current_observed_invalid = optional_timestamp(
        "current_day_observed_at"
    )
    persistence_observed_at, persistence_observed_invalid = optional_timestamp(
        "persistence_observed_at"
    )
    generated_at_raw = raw.get("generated_at")
    generated_at_invalid = False
    if isinstance(generated_at_raw, datetime):
        try:
            generated_at = _aware_utc(generated_at_raw)
        except ValueError:
            generated_at = None
            generated_at_invalid = True
    elif generated_at_raw is not None:
        generated_at = None
        generated_at_invalid = True
    else:
        generated_at = None

    source = str(raw.get("source", "unavailable"))[:64]
    night_start = _bounded_count(raw.get("night_start_minute", 22 * 60), 1439)
    night_end = _bounded_count(raw.get("night_end_minute", 6 * 60), 1439)
    valid = not generated_at_invalid and not optional_number_invalid and all(
        item is not None
        for item in (
            daily_days,
            night_days,
            weekday_days,
            weekend_days,
            totals,
            total_dates,
            average_profile,
            weekday_profile,
            weekend_profile,
            profile_days,
            daily_coverage,
            profile_coverage,
            persistence_delta,
            persistence_samples,
            night_start,
            night_end,
        )
    ) and not current_observed_invalid and not persistence_observed_invalid
    if current_energy_raw is not None and current_energy is None:
        valid = False
    if not valid:
        return SharedLoadModelSnapshot(source="invalid_provider")
    ready_raw = raw.get("ready", False)
    if type(ready_raw) is not bool:
        return SharedLoadModelSnapshot(source="invalid_provider")
    generated_fresh = load_model_generated_at_is_fresh(
        generated_at,
        now=now,
    )
    retained_history_usable = bool(
        daily_days == len(total_dates or ())
        and qualified_load_history_is_usable(
            generated_at, total_dates or (), now=now
        )
    )
    fallback_raw = raw.get("fallback_currently_used", False)
    if type(fallback_raw) is not bool:
        return SharedLoadModelSnapshot(source="invalid_provider")
    return SharedLoadModelSnapshot(
        average_daily_home_load_kwh=daily,
        average_night_home_load_kwh=night,
        provisional_daily_load_projection_kwh=provisional,
        daily_history_days=daily_days or 0,
        night_history_days=night_days or 0,
        daily_totals_kwh=totals or (),
        daily_total_dates=total_dates or (),
        average_profile_30m_kwh=average_profile or (),
        weekday_profile_30m_kwh=weekday_profile or (),
        weekend_profile_30m_kwh=weekend_profile or (),
        weekday_profile_days=weekday_days or 0,
        weekend_profile_days=weekend_days or 0,
        profile_history_days=profile_days or 0,
        daily_coverage_ratio=daily_coverage or 0.0,
        profile_coverage_ratio=profile_coverage or 0.0,
        current_day_energy_kwh=current_energy,
        current_day_observed_at=current_observed_at,
        persistence_delta_kw=persistence_delta or 0.0,
        persistence_observed_at=persistence_observed_at,
        persistence_sample_count=persistence_samples or 0,
        model_schema=str(raw.get("model_schema", "unavailable"))[:64],
        night_start_minute=night_start,
        night_end_minute=night_end,
        model_quality=str(raw.get("model_quality", "unavailable"))[:64],
        generated_at=generated_at,
        ready=bool(ready_raw and (generated_fresh or retained_history_usable)),
        fallback_currently_used=fallback_raw,
        source=source,
    )


def _migration_not_run() -> dict[str, Any]:
    return {
        "version": SHARED_INPUTS_MIGRATION_VERSION,
        "result": "not_run",
        "legacy_aliases": {
            NEW_FORECAST_HELPERS["today"]: LEGACY_FORECAST_HELPERS["today"],
            NEW_FORECAST_HELPERS["tomorrow"]: LEGACY_FORECAST_HELPERS["tomorrow"],
            NEW_FORECAST_HELPERS["day3"]: LEGACY_FORECAST_HELPERS["day3"],
            NEW_FALLBACK_LOAD_HELPER: LEGACY_FALLBACK_LOAD_HELPER,
            NEW_RATED_POWER_HELPER: LEGACY_RATED_POWER_HELPER,
            NEW_PV_TO_BATTERY_EFFICIENCY_HELPER: (
                LEGACY_PV_TO_BATTERY_EFFICIENCY_HELPER
            ),
            NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER: (
                LEGACY_BATTERY_TO_HOME_EFFICIENCY_HELPER
            ),
        },
        "fields": {},
        "seeded_values": {},
        "provenance": {},
    }


class EMSSharedInputsCoordinator:
    """Resolve and refresh one config entry's neutral EMS inputs."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        config_entry_id: str,
        source_device_model: str | None,
        now_provider: Callable[[], datetime] | None = None,
    ) -> None:
        if (
            not isinstance(config_entry_id, str)
            or not config_entry_id
            or len(config_entry_id) > 64
        ):
            raise ValueError("shared EMS config_entry_id must contain 1..64 characters")
        self.hass = hass
        self.config_entry_id = config_entry_id
        self.source_device_model = source_device_model
        self._now_provider = now_provider or dt_util.utcnow
        self._callbacks: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []
        self._state_unsubs: list[Callable[[], None]] = []
        self._watched_entity_ids: tuple[str, ...] = ()
        self._resolved_entity_ids: dict[str, tuple[str | None, str | None]] = {}
        self._load_model_provider: LoadModelProvider | None = None
        self._started = False
        self._signature: tuple[Any, ...] | None = None
        self._migration_status: dict[str, Any] = _migration_not_run()
        self._snapshot = self._build_snapshot(_aware_utc(self._now_provider()), 0)

    @property
    def snapshot(self) -> EMSSharedInputsSnapshot:
        """Return the latest immutable snapshot without performing I/O."""

        return self._snapshot

    def async_listen(self, callback_func: Callable[[], None]) -> Callable[[], None]:
        """Listen for semantic snapshot revisions."""

        self._callbacks.append(callback_func)

        def unsubscribe() -> None:
            if callback_func in self._callbacks:
                self._callbacks.remove(callback_func)

        return unsubscribe

    def attach_load_model_provider(
        self,
        provider: LoadModelProvider,
    ) -> Callable[[], None]:
        """Attach the exact-entry RCE LOAD snapshot provider once."""

        if self._load_model_provider is not None and self._load_model_provider != provider:
            raise RuntimeError("EMS shared LOAD provider is already attached")
        self._load_model_provider = provider
        if self._started:
            self._refresh(rebind=False)

        def detach() -> None:
            self.detach_load_model_provider(provider)

        return detach

    def detach_load_model_provider(self, provider: LoadModelProvider) -> None:
        """Detach only the provider that was previously attached."""

        if self._load_model_provider != provider:
            return
        self._load_model_provider = None
        if self._started:
            self._refresh(rebind=False)

    def set_migration_status(self, status: Mapping[str, Any]) -> None:
        """Publish the bounded lifecycle migration summary in the shared sensor."""

        version = _bounded_count(status.get("version"), 1_000_000)
        result = str(status.get("result", "unknown"))[:64]
        known_fields = {
            "forecast_today",
            "forecast_tomorrow",
            "forecast_day3",
            "fallback_daily_home_load",
            "inverter_rated_power_each",
            "pv_to_battery_efficiency",
            "battery_to_home_efficiency",
        }
        raw_fields = status.get("fields")
        fields = {
            key: str(value)[:64]
            for key, value in raw_fields.items()
            if key in known_fields
        } if isinstance(raw_fields, Mapping) else {}
        raw_seeded = status.get("seeded_values")
        seeded_values = {
            key: value
            for key, raw_value in raw_seeded.items()
            if key in {
                "pv_to_battery_efficiency",
                "battery_to_home_efficiency",
            }
            and (
                value := _bounded_number(
                    raw_value,
                    minimum=0.001,
                    maximum=100.0,
                )
            ) is not None
        } if isinstance(raw_seeded, Mapping) else {}
        self._migration_status = {
            "version": version or SHARED_INPUTS_MIGRATION_VERSION,
            "result": result,
            "legacy_aliases": _migration_not_run()["legacy_aliases"],
            "fields": fields,
            "seeded_values": seeded_values,
            "provenance": {
                key: (
                    "migration_seed_from_legacy"
                    if value == "copied_legacy"
                    else "neutral_helper"
                    if value in {"preserved_new", "preserved_invalid_new"}
                    else "unavailable"
                )
                for key, value in fields.items()
            },
        }
        if self._started:
            # A terminal field decision closes its legacy-listener window.
            self._refresh(rebind=True)

    async def async_start(self) -> None:
        """Start entry-local listeners; safe to call repeatedly."""

        if self._started:
            return
        self._started = True
        self._unsubs.append(
            self.hass.bus.async_listen(
                er.EVENT_ENTITY_REGISTRY_UPDATED,
                self._async_registry_updated,
            )
        )
        self._unsubs.append(
            async_track_time_interval(
                self.hass,
                self._async_interval_refresh,
                SHARED_INPUTS_REFRESH_INTERVAL,
            )
        )
        self._refresh(rebind=True)

    async def async_stop(self) -> None:
        """Release every listener; safe during partial setup and repeated unload."""

        if not self._started and not self._unsubs and not self._state_unsubs:
            return
        self._started = False
        for unsubscribe in (*self._state_unsubs, *self._unsubs):
            unsubscribe()
        self._state_unsubs.clear()
        self._unsubs.clear()
        self._watched_entity_ids = ()

    async def async_refresh(self) -> None:
        """Refresh synchronously available HA state without external I/O."""

        self._refresh(rebind=True)

    @callback
    def _async_registry_updated(self, _event: Event) -> None:
        if self._started:
            self._refresh(rebind=True)

    @callback
    def _async_source_updated(self, _event: Event) -> None:
        if self._started:
            self._refresh(rebind=True)

    @callback
    def _async_interval_refresh(self, _now: datetime) -> None:
        if self._started:
            self._refresh(rebind=False)

    def _resolve_registry_sources(self) -> None:
        registry = er.async_get(self.hass)
        self._resolved_entity_ids = {
            key: _exact_registry_entity_id(
                registry,
                config_entry_id=self.config_entry_id,
                translation_key=spec.translation_key,
                domain=spec.domain,
            )
            for key, spec in _NUMERIC_SOURCES.items()
        }

    def _legacy_fallback_open(self, field_key: str) -> bool:
        """Allow a legacy alias only while that exact migration is pending."""

        fields = self._migration_status.get("fields")
        status = fields.get(field_key) if isinstance(fields, Mapping) else None
        if status is None:
            return self._migration_status.get("result") == "not_run"
        return status in _LEGACY_FALLBACK_OPEN_STATUSES

    def _replace_state_listeners(self) -> None:
        watched: set[str] = {
            *NEW_FORECAST_HELPERS.values(),
            *SOLCAST_UPDATE_ENTITY_CANDIDATES,
            "sun.sun",
            NEW_FALLBACK_LOAD_HELPER,
            NEW_RATED_POWER_HELPER,
            NEW_PV_TO_BATTERY_EFFICIENCY_HELPER,
            NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER,
        }
        watched.update(
            legacy_entity_id
            for period, legacy_entity_id in LEGACY_FORECAST_HELPERS.items()
            if self._legacy_fallback_open(f"forecast_{period}")
        )
        if self._legacy_fallback_open("fallback_daily_home_load"):
            watched.add(LEGACY_FALLBACK_LOAD_HELPER)
        if self._legacy_fallback_open("inverter_rated_power_each"):
            watched.add(LEGACY_RATED_POWER_HELPER)
        watched.update(
            entity_id
            for entity_id, _reason in self._resolved_entity_ids.values()
            if entity_id is not None
        )
        for period in ("today", "tomorrow", "day3", "remaining_today"):
            selected, _selector, _provenance, _reason = self._forecast_selection(period)
            if selected is not None:
                watched.add(selected)
            watched.update(FORECAST_CANDIDATES[period])
        bounded = tuple(sorted(filter(_bounded_entity_id, watched)))
        if bounded == self._watched_entity_ids:
            return
        for unsubscribe in self._state_unsubs:
            unsubscribe()
        self._state_unsubs.clear()
        self._watched_entity_ids = bounded
        if not bounded:
            return
        self._state_unsubs.extend(
            (
                async_track_state_change_event(
                    self.hass,
                    bounded,
                    self._async_source_updated,
                ),
                async_track_state_report_event(
                    self.hass,
                    bounded,
                    self._async_source_updated,
                ),
            )
        )

    def _refresh(self, *, rebind: bool) -> None:
        if rebind or not self._resolved_entity_ids:
            self._resolve_registry_sources()
            if self._started:
                self._replace_state_listeners()
        now = _aware_utc(self._now_provider())
        candidate = self._build_snapshot(now, self._snapshot.revision)
        signature = self._semantic_signature(candidate)
        changed = signature != self._signature
        if changed:
            candidate = replace(candidate, revision=self._snapshot.revision + 1)
            self._signature = signature
        self._snapshot = candidate
        if changed:
            for callback_func in tuple(self._callbacks):
                callback_func()

    def _numeric_source(self, key: str, now: datetime) -> SharedSample:
        spec = _NUMERIC_SOURCES[key]
        entity_id, resolution_reason = self._resolved_entity_ids.get(
            key,
            (None, "source_unavailable"),
        )
        if entity_id is None:
            return _empty_sample(
                resolution_reason or "source_unavailable",
                provenance="entry_local_registry",
            )
        state = self.hass.states.get(entity_id)
        sample = numeric_state_sample(
            state,
            now,
            max_age_seconds=SHARED_INPUTS_PHYSICAL_MAX_AGE_SECONDS,
            scale=_power_scale(state) if spec.power_unit else 1.0,
            minimum=spec.minimum,
            maximum=spec.maximum,
        )
        value: SharedScalar = sample.value
        if key in {"inverter_count", "gcf_generation", "gcf_enable", "gcf_supported"} and sample.value is not None:
            rounded = round(sample.value)
            if abs(sample.value - rounded) > 1e-9:
                return _empty_sample(
                    "not_integer",
                    provenance="entry_local_registry",
                    entity_id=entity_id,
                    source_entity_ids=(entity_id,),
                )
            value = int(rounded)
        return SharedSample(
            value=value,
            entity_id=entity_id,
            source_entity_ids=(entity_id,),
            selector_entity_id=None,
            reported_at=sample.reported_at,
            age_seconds=sample.age_seconds,
            fresh=sample.fresh,
            reason=sample.reason,
            quality=_quality_for_reason(sample.reason, sample.fresh),
            provenance="entry_local_registry",
        )

    def _forecast_selection(
        self,
        period: str,
    ) -> tuple[str | None, str | None, str, str | None]:
        if period == "remaining_today":
            for candidate in FORECAST_CANDIDATES[period]:
                state = self.hass.states.get(candidate)
                if state is None:
                    continue
                try:
                    value = float(state.state)
                except (TypeError, ValueError):
                    continue
                if isfinite(value):
                    return candidate, None, "automatic_candidate", None
            return None, None, "unavailable", "source_unavailable"

        new_helper = NEW_FORECAST_HELPERS[period]
        legacy_helper = LEGACY_FORECAST_HELPERS[period]
        new_state = self.hass.states.get(new_helper)
        new_raw = (
            str(new_state.state).strip().casefold()
            if new_state is not None
            else ""
        )
        if new_raw not in _UNAVAILABLE:
            selected = _bounded_entity_id(new_raw)
            if selected is None:
                return None, new_helper, "new_helper", "invalid_entity_id"
            return selected, new_helper, "new_helper", None

        if self._legacy_fallback_open(f"forecast_{period}"):
            legacy_state = self.hass.states.get(legacy_helper)
            legacy_raw = (
                str(legacy_state.state).strip().casefold()
                if legacy_state is not None
                else ""
            )
            if legacy_raw not in _UNAVAILABLE:
                selected = _bounded_entity_id(legacy_raw)
                if selected is None:
                    return None, legacy_helper, "legacy_helper", "invalid_entity_id"
                return selected, legacy_helper, "legacy_helper", None

        for candidate in FORECAST_CANDIDATES[period]:
            state = self.hass.states.get(candidate)
            if state is None:
                continue
            try:
                value = float(state.state)
            except (TypeError, ValueError):
                continue
            if isfinite(value):
                return candidate, None, "automatic_candidate", None
        return None, None, "unavailable", "source_unavailable"

    def _forecast_sample(self, period: str, now: datetime) -> SharedSample:
        entity_id, selector, provenance, selection_reason = self._forecast_selection(period)
        if entity_id is None:
            return _empty_sample(
                selection_reason or "source_unavailable",
                provenance=provenance,
                selector_entity_id=selector,
            )
        state = self.hass.states.get(entity_id)
        time_zone = getattr(getattr(self.hass, "config", None), "time_zone", "UTC")
        local_now = now.astimezone(ZoneInfo(time_zone))
        target_offset = {"today": 0, "remaining_today": 0, "tomorrow": 1, "day3": 2}[
            period
        ]
        usefulness = evaluate_pv_forecast_usefulness(
            state,
            local_now,
            target_date=(local_now + timedelta(days=target_offset)).date(),
            max_age_seconds=SHARED_INPUTS_FORECAST_MAX_AGE_SECONDS,
            update_state=resolve_solcast_update_state(self.hass.states),
            sun_state=self.hass.states.get("sun.sun"),
            maximum=_MAX_ENERGY_KWH,
        )
        return SharedSample(
            value=usefulness.value,
            entity_id=entity_id,
            source_entity_ids=(entity_id,),
            selector_entity_id=selector,
            reported_at=usefulness.reported_at,
            age_seconds=usefulness.age_seconds,
            fresh=usefulness.usable,
            reason=usefulness.source_reason,
            quality=_quality_for_reason(
                usefulness.source_reason,
                usefulness.usable,
            ),
            provenance=provenance,
            usefulness=usefulness.as_public_dict(),
        )

    def _rated_power_sample(self, now: datetime) -> SharedSample:
        new_state = self.hass.states.get(NEW_RATED_POWER_HELPER)
        new_value, new_reason = _rated_power_from_helper_state(new_state)
        if new_value is not None:
            return _setting_sample(
                new_value,
                now=now,
                state=new_state,
                entity_id=NEW_RATED_POWER_HELPER,
                provenance="new_helper",
            )
        if new_reason == "automatic":
            detected = strict_rated_power_from_model(self.source_device_model)
            if detected is not None:
                return SharedSample(
                    value=detected,
                    entity_id=None,
                    source_entity_ids=(),
                    selector_entity_id=NEW_RATED_POWER_HELPER,
                    reported_at=None,
                    age_seconds=None,
                    fresh=True,
                    reason="strict_model_match",
                    quality="complete",
                    provenance="device_model",
                )

        if self._legacy_fallback_open("inverter_rated_power_each"):
            legacy_state = self.hass.states.get(LEGACY_RATED_POWER_HELPER)
            legacy_value, _legacy_reason = _rated_power_from_helper_state(legacy_state)
            if legacy_value is not None:
                return _setting_sample(
                    legacy_value,
                    now=now,
                    state=legacy_state,
                    entity_id=LEGACY_RATED_POWER_HELPER,
                    provenance="legacy_helper",
                )
        return _empty_sample(
            "rated_power_unavailable",
            provenance=("new_helper" if new_state is not None else "unavailable"),
            selector_entity_id=(
                NEW_RATED_POWER_HELPER if new_state is not None else None
            ),
        )

    def _inverter_model_sample(self) -> SharedSample:
        raw = self.source_device_model
        if not isinstance(raw, str):
            return _empty_sample(
                "model_unavailable",
                provenance="source_device_model",
            )
        model = raw.strip()
        if not model or len(model) > 64:
            return _empty_sample(
                "model_unavailable",
                provenance="source_device_model",
            )
        return SharedSample(
            value=model,
            entity_id=None,
            source_entity_ids=(),
            selector_entity_id=None,
            reported_at=None,
            age_seconds=None,
            fresh=True,
            reason="source_device_model",
            quality="complete",
            provenance="source_device_model",
        )

    def _fallback_load_sample(self, now: datetime) -> SharedSample:
        new_state = self.hass.states.get(NEW_FALLBACK_LOAD_HELPER)
        if new_state is not None:
            new_value = _bounded_number(
                getattr(new_state, "state", None), minimum=0.0, maximum=200.0
            )
            if new_value is not None and new_value > 0.0:
                return _setting_sample(
                    new_value,
                    now=now,
                    state=new_state,
                    entity_id=NEW_FALLBACK_LOAD_HELPER,
                    provenance="new_helper",
                )
            raw = str(getattr(new_state, "state", "")).strip().casefold()
            if new_value is None and raw not in _UNAVAILABLE:
                return _empty_sample(
                    "invalid_configured_value",
                    provenance="new_helper",
                    entity_id=NEW_FALLBACK_LOAD_HELPER,
                    source_entity_ids=(NEW_FALLBACK_LOAD_HELPER,),
                    selector_entity_id=NEW_FALLBACK_LOAD_HELPER,
                )

        if self._legacy_fallback_open("fallback_daily_home_load"):
            legacy_state = self.hass.states.get(LEGACY_FALLBACK_LOAD_HELPER)
            legacy_value = _bounded_number(
                getattr(legacy_state, "state", None), minimum=0.001, maximum=200.0
            ) if legacy_state is not None else None
            if legacy_value is not None:
                return _setting_sample(
                    legacy_value,
                    now=now,
                    state=legacy_state,
                    entity_id=LEGACY_FALLBACK_LOAD_HELPER,
                    provenance="legacy_helper",
                )
        return _empty_sample(
            "fallback_load_unavailable",
            provenance="unavailable",
            selector_entity_id=(
                NEW_FALLBACK_LOAD_HELPER if new_state is not None else None
            ),
        )

    def _baseline_efficiency_sample(
        self,
        entity_id: str,
        *,
        now: datetime,
        field_key: str,
    ) -> SharedSample:
        """Read one neutral model helper without a live legacy fallback."""

        state = self.hass.states.get(entity_id)
        value = _bounded_number(
            getattr(state, "state", None),
            minimum=0.001,
            maximum=100.0,
        ) if state is not None else None
        if value is None:
            return _empty_sample(
                "baseline_efficiency_unavailable",
                provenance="unavailable",
                entity_id=entity_id if state is not None else None,
                source_entity_ids=(entity_id,) if state is not None else (),
                selector_entity_id=entity_id,
            )
        seeded_value = self._migration_status.get("seeded_values", {}).get(
            field_key
        )
        provenance = (
            "migration_seed_from_legacy"
            if type(seeded_value) in {int, float}
            and abs(float(seeded_value) - value) <= 1e-9
            and self._migration_status.get("fields", {}).get(field_key)
            == "copied_legacy"
            else "neutral_model_helper"
        )
        return _setting_sample(
            value,
            now=now,
            state=state,
            entity_id=entity_id,
            provenance=provenance,
        )

    def _derived_sample(
        self,
        value: float | int,
        sources: Sequence[SharedSample],
        *,
        reason: str,
        provenance: str,
    ) -> SharedSample:
        source_ids = tuple(
            entity_id
            for source in sources
            for entity_id in source.source_entity_ids
        )[:_MAX_PUBLIC_SOURCE_IDS]
        reported = [source.reported_at for source in sources if source.reported_at is not None]
        ages = [source.age_seconds for source in sources if source.age_seconds is not None]
        return SharedSample(
            value=value,
            entity_id=None,
            source_entity_ids=source_ids,
            selector_entity_id=None,
            reported_at=min(reported) if reported else None,
            age_seconds=max(ages) if ages else None,
            fresh=True,
            reason=reason,
            quality="complete",
            provenance=provenance,
        )

    def _incomplete_derived_sample(
        self,
        sources: Sequence[SharedSample],
        *,
        reason: str,
        provenance: str,
    ) -> SharedSample:
        source_ids = tuple(
            entity_id
            for source in sources
            for entity_id in source.source_entity_ids
        )[:_MAX_PUBLIC_SOURCE_IDS]
        reported = [source.reported_at for source in sources if source.reported_at is not None]
        ages = [source.age_seconds for source in sources if source.age_seconds is not None]
        if any(source.fresh for source in sources):
            quality = "partial"
        elif any(source.quality == "stale" for source in sources):
            quality = "stale"
        else:
            quality = "unavailable"
        return SharedSample(
            value=None,
            entity_id=None,
            source_entity_ids=source_ids,
            selector_entity_id=None,
            reported_at=min(reported) if reported else None,
            age_seconds=max(ages) if ages else None,
            fresh=False,
            reason=reason,
            quality=quality,
            provenance=provenance,
        )

    def _load_model(self, now: datetime) -> SharedLoadModelSnapshot:
        if self._load_model_provider is None:
            return SharedLoadModelSnapshot()
        try:
            return _bounded_load_model_snapshot(
                self._load_model_provider(now),
                now=now.astimezone(ZoneInfo(
                    getattr(getattr(self.hass, "config", None), "time_zone", "UTC")
                )),
            )
        except Exception:  # noqa: BLE001 - provider failure remains unavailable
            return SharedLoadModelSnapshot(source="provider_error")

    def _build_snapshot(self, now: datetime, revision: int) -> EMSSharedInputsSnapshot:
        samples = {
            key: self._numeric_source(key, now)
            for key in _NUMERIC_SOURCES
        }
        rated = self._rated_power_sample(now)
        count = samples["inverter_count"]
        if rated.fresh and count.fresh and rated.value is not None and count.value is not None:
            system_rated = self._derived_sample(
                float(rated.value) * int(count.value),
                (rated, count),
                reason="derived_product",
                provenance="rated_each_times_inverter_count",
            )
        else:
            system_rated = self._incomplete_derived_sample(
                (rated, count),
                reason="incomplete_rated_power_inputs",
                provenance="rated_each_times_inverter_count",
            )

        system = SharedSystemInputs(
            inverter_model=self._inverter_model_sample(),
            battery_capacity_kwh=samples["battery_capacity"],
            total_capacity_kwh=samples["total_capacity"],
            battery_soc_percent=samples["battery_soc"],
            self_use_reserve_soc_percent=samples["self_use_reserve"],
            hardware_minimum_soc_percent=samples["hardware_minimum_soc"],
            hardware_maximum_soc_percent=samples["hardware_maximum_soc"],
            inverter_count=count,
            inverter_rated_power_each_kw=rated,
            system_rated_power_kw=system_rated,
        )

        forecast = SharedForecastInputs(
            today=self._forecast_sample("today", now),
            tomorrow=self._forecast_sample("tomorrow", now),
            day3=self._forecast_sample("day3", now),
            remaining_today=self._forecast_sample("remaining_today", now),
        )

        load_model = self._load_model(now)
        fallback_load = self._fallback_load_sample(now)
        fallback_model_ready = _explicit_fallback_model_ready(
            load_model,
            fallback_load,
        )
        load = SharedLoadInputs(
            fallback_daily_home_load_kwh=fallback_load,
            average_daily_home_load_kwh=load_model.average_daily_home_load_kwh,
            average_night_home_load_kwh=load_model.average_night_home_load_kwh,
            provisional_daily_load_projection_kwh=(
                load_model.provisional_daily_load_projection_kwh
            ),
            daily_history_days=load_model.daily_history_days,
            night_history_days=load_model.night_history_days,
            daily_totals_kwh=load_model.daily_totals_kwh,
            daily_total_dates=load_model.daily_total_dates,
            average_profile_30m_kwh=load_model.average_profile_30m_kwh,
            weekday_profile_30m_kwh=load_model.weekday_profile_30m_kwh,
            weekend_profile_30m_kwh=load_model.weekend_profile_30m_kwh,
            weekday_profile_days=load_model.weekday_profile_days,
            weekend_profile_days=load_model.weekend_profile_days,
            profile_history_days=load_model.profile_history_days,
            daily_coverage_ratio=load_model.daily_coverage_ratio,
            profile_coverage_ratio=load_model.profile_coverage_ratio,
            current_day_energy_kwh=load_model.current_day_energy_kwh,
            current_day_observed_at=load_model.current_day_observed_at,
            persistence_delta_kw=load_model.persistence_delta_kw,
            persistence_observed_at=load_model.persistence_observed_at,
            persistence_sample_count=load_model.persistence_sample_count,
            model_schema=load_model.model_schema,
            night_start_minute=load_model.night_start_minute,
            night_end_minute=load_model.night_end_minute,
            model_quality=load_model.model_quality,
            generated_at=load_model.generated_at,
            ready=bool(load_model.ready or fallback_model_ready),
            fallback_currently_used=load_model.fallback_currently_used,
            source=load_model.source,
        )

        voltage = samples["bms_voltage"]
        charge_current = samples["bms_charge_current"]
        discharge_current = samples["bms_discharge_current"]
        charge_power = (
            self._derived_sample(
                float(voltage.value) * float(charge_current.value) / 1000.0,
                (voltage, charge_current),
                reason="derived_voltage_current",
                provenance="physical_bms_product",
            )
            if voltage.fresh and charge_current.fresh
            else self._incomplete_derived_sample(
                (voltage, charge_current),
                reason="incomplete_bms_charge_inputs",
                provenance="physical_bms_product",
            )
        )
        discharge_power = (
            self._derived_sample(
                float(voltage.value) * float(discharge_current.value) / 1000.0,
                (voltage, discharge_current),
                reason="derived_voltage_current",
                provenance="physical_bms_product",
            )
            if voltage.fresh and discharge_current.fresh
            else self._incomplete_derived_sample(
                (voltage, discharge_current),
                reason="incomplete_bms_discharge_inputs",
                provenance="physical_bms_product",
            )
        )
        bms = SharedBMSInputs(
            voltage_v=voltage,
            maximum_charge_current_a=charge_current,
            maximum_discharge_current_a=discharge_current,
            maximum_charge_power_kw=charge_power,
            maximum_discharge_power_kw=discharge_power,
        )
        efficiency = SharedEfficiencyInputs(
            pv_to_battery_efficiency=self._baseline_efficiency_sample(
                NEW_PV_TO_BATTERY_EFFICIENCY_HELPER,
                now=now,
                field_key="pv_to_battery_efficiency",
            ),
            battery_to_home_efficiency=self._baseline_efficiency_sample(
                NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER,
                now=now,
                field_key="battery_to_home_efficiency",
            ),
        )

        support = samples["gcf_supported"]
        generation = samples["gcf_generation"]
        enable = samples["gcf_enable"]
        limit = samples["gcf_limit"]
        cohort_reported = [
            sample.reported_at
            for sample in (generation, enable, limit)
            if sample.reported_at is not None
        ]
        cohort_skew = (
            (max(cohort_reported) - min(cohort_reported)).total_seconds()
            if len(cohort_reported) == 3
            else None
        )
        gcf_ready = bool(
            support.fresh
            and support.value == 1
            and generation.fresh
            and isinstance(generation.value, int)
            and not isinstance(generation.value, bool)
            and enable.fresh
            and enable.value in {0, 1}
            and limit.fresh
            and cohort_skew is not None
            and cohort_skew <= SHARED_INPUTS_GCF_COHORT_MAX_SKEW_SECONDS
        )
        zero_export = bool(gcf_ready and enable.value == 1 and limit.value == 0.0)
        export_allowed: bool | None = None
        if gcf_ready:
            export_allowed = bool(enable.value == 0 or float(limit.value) > 0.0)
        gcf = SharedGCFInputs(
            hardware_readback_supported=support,
            generation=generation,
            enable_code=enable,
            maximum_export_power_percent=limit,
            cohort_skew_seconds=cohort_skew,
            gcf_readback_ready=gcf_ready,
            zero_export_confirmed=zero_export,
            export_allowed=export_allowed,
        )

        home_phases = (
            samples["home_load_l1"],
            samples["home_load_l2"],
            samples["home_load_l3"],
        )
        if all(sample.fresh and sample.value is not None for sample in home_phases):
            # Registers 2170-2172 can report a small negative measurement
            # residual on an idle phase. Keep the exact entry-local sources,
            # but normalize an accepted residual exactly like the canonical
            # ``sensor.hoymiles_actual_load_power`` template. A materially
            # negative value, missing/stale sample, non-finite number or value
            # outside the physical signed-word ceiling remains fail-closed.
            home_load = self._derived_sample(
                sum(max(float(sample.value), 0.0) for sample in home_phases),
                home_phases,
                reason="derived_exact_phase_sum",
                provenance="entry_local_exact_phase_sum",
            )
        else:
            home_load = self._incomplete_derived_sample(
                home_phases,
                reason="incomplete_exact_phase_set",
                provenance="entry_local_exact_phase_sum",
            )
        providerless = _empty_sample(
            "provider_missing",
            provenance="no_confirmed_repo_provider",
        )
        power = SharedPhysicalFlowInputs(
            pv_power_kw=samples["pv_power"],
            home_load_power_kw=home_load,
            system_load_power_kw=samples["system_load_power"],
            battery_power_kw=samples["battery_power"],
            grid_power_kw=samples["grid_power"],
            grid_to_battery_power=providerless,
            grid_to_battery_ready=False,
        )

        all_samples = (
            *system.as_public_dict().values(),
            *forecast.as_public_dict().values(),
            *bms.as_public_dict().values(),
            power.pv_power_kw.as_public_dict(),
            power.home_load_power_kw.as_public_dict(),
            power.system_load_power_kw.as_public_dict(),
            power.battery_power_kw.as_public_dict(),
            power.grid_power_kw.as_public_dict(),
        )
        any_fresh = any(
            isinstance(item, Mapping) and item.get("fresh") is True
            for item in all_samples
        )
        quality = "partial" if any_fresh else "unavailable"
        return EMSSharedInputsSnapshot(
            schema_version=SHARED_INPUTS_SCHEMA_VERSION,
            config_entry_id=self.config_entry_id,
            captured_at=now,
            revision=revision,
            quality=quality,
            issues=(P1_PROVIDERLESS_GRID_TO_BATTERY_DEPENDENCY,),
            system=system,
            forecast=forecast,
            load=load,
            bms=bms,
            efficiency=efficiency,
            gcf=gcf,
            power=power,
            migration=dict(self._migration_status),
        )

    @staticmethod
    def _semantic_signature(snapshot: EMSSharedInputsSnapshot) -> tuple[Any, ...]:
        public = snapshot.as_public_dict()
        public.pop("captured_at", None)
        public.pop("revision", None)
        public.pop("forecast_last_updated", None)
        public.pop("forecast_last_success", None)
        public.pop("forecast_next_update", None)
        public.pop("forecast_valid_until", None)

        def freeze(value: Any) -> Any:
            if isinstance(value, Mapping):
                return tuple(
                    (key, freeze(item))
                    for key, item in sorted(value.items())
                    if key not in {
                        "age_seconds",
                        "reported_at",
                        "source_reported_at",
                        "last_success_at",
                        "next_update_at",
                        "valid_until",
                    }
                    and not key.endswith("_source_ages_seconds")
                )
            if isinstance(value, list):
                return tuple(freeze(item) for item in value)
            return value

        return freeze(public)


class HoymilesEMSSharedInputsSensor(SensorEntity):
    """Publish the bounded shared-input contract for one config entry."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "ems_shared_inputs"
    _attr_icon = "mdi:database-sync-outline"
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        coordinator: EMSSharedInputsCoordinator,
        source_device: Any,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._coordinator = coordinator
        self._source_device = source_device
        self._attr_unique_id = f"{entry.entry_id}_ems_shared_inputs"

    @property
    def suggested_object_id(self) -> str:
        return "hoymiles_hit_ems_shared_inputs"

    @property
    def device_info(self) -> DeviceInfo:
        source = self._source_device
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=source.name_by_user or source.name or NAME,
            manufacturer=source.manufacturer or "Hoymiles",
            model=source.model or "HIT xxL G3",
            sw_version=source.sw_version,
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> str:
        return self._coordinator.snapshot.quality

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._coordinator.snapshot.as_public_dict()

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(self._coordinator.async_listen(self._async_updated))

    @callback
    def _async_updated(self) -> None:
        if self.hass is not None:
            self.async_write_ha_state()
