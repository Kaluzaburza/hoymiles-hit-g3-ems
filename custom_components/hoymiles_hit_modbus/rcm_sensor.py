"""Home Assistant sensor for RCEm 253 V+ voltage-aware PV buffering."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from collections import deque
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
import logging
from math import floor, isfinite
import re
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .bounded_history import async_get_bounded_state_reports
from .const import DOMAIN, NAME
from .energy_data import numeric_state_sample, state_age_seconds
from .ems_shared_inputs import (
    NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER,
    qualified_load_history_is_usable,
)
from .models import RuntimeData
from .optimizer_revision import (
    MAX_IMMEDIATE_RECALCULATIONS,
    OptimizerInputRevision,
    RCE_LOAD_BROKER_ATTRIBUTES,
    optimizer_input_fingerprint,
)
from .pv_forecast_usability import (
    SOLCAST_UPDATE_ENTITY_CANDIDATES,
    evaluate_pv_forecast_usefulness,
    resolve_solcast_update_state,
)
from .rcm_history import (
    GRID_VOLTAGE_ENTITIES,
    SLOTS_PER_DAY,
    VoltageHistorySummary,
    summarize_voltage_history,
)
from . import rcm_optimizer as rcm_optimizer_module
from .rcm_optimizer import (
    RCMOptimizerInput,
    RCMRiskWindowInput,
    optimize_rcm,
    shared_rcm_load_envelopes,
    select_rcm_pv_profile,
    stateful_natural_headroom_kwh,
    stateful_pre_risk_home_buffer_kwh,
)
from .rcm_timeline_model import (
    RCMTimelineEnergyPoint,
    RCMTimelineModelError,
    RCMTimelineModelInput,
    RCMTimelineRiskInterval,
    build_rcm_timeline_trace,
)
from .rce_optimizer import floor_half_hour
from .rce_sensor import (
    EMS_INVERTER_RATED_POWER_HELPER,
    EMS_TODAY_FORECAST_ENTITY_HELPER,
    EMS_TOMORROW_FORECAST_ENTITY_HELPER,
    LEGACY_INVERTER_RATED_POWER_HELPER,
    REMAINING_TODAY_CANDIDATES,
    TODAY_FORECAST_CANDIDATES,
    TODAY_FORECAST_ENTITY_HELPER,
    TOMORROW_FORECAST_CANDIDATES,
    TOMORROW_FORECAST_ENTITY_HELPER,
    _detailed_pv_map,
    _detailed_pv_expected_elapsed_kwh,
    _first_numeric_state,
    _forecast_total,
    _policy_numeric_sample,
    _preferred_select_number,
    _resolved_forecast_entity_id,
    _select_number,
    _shared_input_field,
    _shared_inputs_snapshot,
    _shared_sample_value,
    _SHARED_INPUT_MISSING,
    _state_number,
    _state_text,
)


_LOGGER = logging.getLogger(__name__)

LIVE_TELEMETRY_MAX_AGE_SECONDS = 90.0
SLOW_TELEMETRY_MAX_AGE_SECONDS = 300.0
ACTUATOR_MAX_AGE_SECONDS = 300.0
FORECAST_MAX_AGE_SECONDS = 12 * 60 * 60.0
RCE_PLAN_MAX_AGE_SECONDS = 300.0
HISTORY_MAX_AGE_SECONDS = 2 * 60 * 60.0
RCM_FULL_PLAN_INTERVAL = timedelta(minutes=10)
RCM_HISTORY_REFRESH_INTERVAL = timedelta(hours=1)
RCM_FULL_PLAN_RECALCULATION_DELAY_SECONDS = 1.0
RCM_FORECAST_BATCH_DELAY_SECONDS = 5.0
RCM_STALE_RESULT_RETRY_DELAY_SECONDS = 5.0
_LAST_COMPLETE_PLAN_GRACE_SECONDS = 10 * 60.0
RCM_CERTIFIED_CACHE_MAX_AGE_SECONDS = 10 * 60.0 + 30.0

BATTERY_CAPACITY_ENTITIES = (
    "sensor.hoymiles_hit_battery_capacity",
    "sensor.hoymiles_hit_total_capacity",
)
FORECAST_ENTITY_HELPERS = (
    EMS_TODAY_FORECAST_ENTITY_HELPER,
    EMS_TOMORROW_FORECAST_ENTITY_HELPER,
    TODAY_FORECAST_ENTITY_HELPER,
    TOMORROW_FORECAST_ENTITY_HELPER,
)
_FORECAST_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

WATCHED_RCM_ENTITIES = {
    *GRID_VOLTAGE_ENTITIES,
    "sensor.hoymiles_hit_overview_pv_total_power",
    "sensor.hoymiles_actual_load_power",
    "sensor.hoymiles_rce_grid_export_power",
    *BATTERY_CAPACITY_ENTITIES,
    "sensor.hoymiles_hit_overview_battery_soc",
    "sensor.hoymiles_hit_battery_voltage_bms",
    "sensor.hoymiles_hit_maximum_charge_current",
    "sensor.hoymiles_hit_maximum_discharge_current",
    "sensor.hoymiles_hit_number_of_machines_master_and_slave",
    *FORECAST_ENTITY_HELPERS,
    *SOLCAST_UPDATE_ENTITY_CANDIDATES,
    "sun.sun",
    *TODAY_FORECAST_CANDIDATES,
    *TOMORROW_FORECAST_CANDIDATES,
    *REMAINING_TODAY_CANDIDATES,
    "sensor.hoymiles_hit_ems_self_use_soc_readback",
    "sensor.hoymiles_hit_battery_max_charge_power_readback",
    "sensor.hoymiles_hit_gcf_maximum_export_power_readback",
    "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
    "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
    "sensor.hoymiles_hit_ems_mode_readback_code",
    "input_number.hoymiles_rcm_soc_safety_margin",
    "input_number.hoymiles_rcm_saved_battery_charge_power",
    "input_number.hoymiles_rcm_export_cap_percent",
    "input_number.hoymiles_rcm_saved_export_limit",
    "input_number.hoymiles_rcm_charge_efficiency",
    NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER,
    EMS_INVERTER_RATED_POWER_HELPER,
    LEGACY_INVERTER_RATED_POWER_HELPER,
    "input_boolean.hoymiles_rcm_enabled",
    "input_boolean.hoymiles_rcm_export_control_enabled",
    "input_boolean.hoymiles_rcm_export_control_active",
    "input_boolean.hoymiles_rcm_pre_discharge_active",
    "sensor.hoymiles_hit_gcf_enable_readback_code",
}

RCM_FORECAST_VALUE_ENTITIES = frozenset(
    {
        *TODAY_FORECAST_CANDIDATES,
        *TOMORROW_FORECAST_CANDIDATES,
        *REMAINING_TODAY_CANDIDATES,
    }
)
RCM_FULL_PLAN_COALESCED_ENTITIES = frozenset(
    {
        *BATTERY_CAPACITY_ENTITIES,
        "sensor.hoymiles_hit_number_of_machines_master_and_slave",
        "sensor.hoymiles_hit_gcf_enable_readback_code",
    }
)
RCM_EVENT_DRIVEN_ENTITIES = frozenset(
    WATCHED_RCM_ENTITIES
    - RCM_FORECAST_VALUE_ENTITIES
    - RCM_FULL_PLAN_COALESCED_ENTITIES
)

# These inputs keep the fast, physical RCEm safety loop alive.  They are not
# full 24/48-hour risk-plan inputs: the scheduler still consumes independent
# freshness/readback gates before an action can execute.  Slow plan inputs
# (settings, topology, forecast selection/batches and the exact RCE LOAD
# broker projection) are handled by the bounded full-plan cadence below.
RCM_LIVE_CONTROL_ENTITIES = frozenset(
    {
        *GRID_VOLTAGE_ENTITIES,
        "sensor.hoymiles_hit_overview_pv_total_power",
        "sensor.hoymiles_actual_load_power",
        "sensor.hoymiles_rce_grid_export_power",
        "sensor.hoymiles_hit_overview_battery_soc",
        "sensor.hoymiles_hit_battery_voltage_bms",
        "sensor.hoymiles_hit_maximum_charge_current",
        "sensor.hoymiles_hit_maximum_discharge_current",
        "sensor.hoymiles_hit_ems_self_use_soc_readback",
        "sensor.hoymiles_hit_battery_max_charge_power_readback",
        "sensor.hoymiles_hit_gcf_maximum_export_power_readback",
        "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "input_boolean.hoymiles_rcm_export_control_active",
        "input_boolean.hoymiles_rcm_pre_discharge_active",
    }
)
RCM_LIVE_CONTROL_CERTIFICATION_ENTITIES = frozenset(
    {
        *RCM_LIVE_CONTROL_ENTITIES,
        *BATTERY_CAPACITY_ENTITIES,
        "sensor.hoymiles_hit_number_of_machines_master_and_slave",
        "sensor.hoymiles_hit_gcf_enable_readback_code",
        EMS_INVERTER_RATED_POWER_HELPER,
        LEGACY_INVERTER_RATED_POWER_HELPER,
        "input_boolean.hoymiles_rcm_export_control_enabled",
        "input_number.hoymiles_rcm_saved_battery_charge_power",
        "input_number.hoymiles_rcm_export_cap_percent",
        "input_number.hoymiles_rcm_saved_export_limit",
    }
)


@dataclass(frozen=True, slots=True)
class RCMEnergyForecast:
    """Selected energy profiles and balances for the next risk horizon."""

    surplus_kwh: float
    horizon: str
    selected_forecast_kwh: float
    selected_forecast_p90_kwh: float | None
    selected_forecast_p10_kwh: float | None
    expected_load_kwh: float
    protected_home_energy_kwh: float
    stress_protected_home_energy_kwh: float
    absorbable_surplus_kwh: float
    natural_headroom_kwh: float
    pre_risk_surplus_kwh: float
    unavoidable_charge_input_kwh: float
    minutes_to_risk: int | None
    risk_day_offset: int
    timeline_target_date: date | None
    forecast_entity_id: str
    forecast_profile_source: str
    forecast_profile_confidence: float
    reserve_forecast_profile_source: str
    reserve_forecast_profile_confidence: float
    load_profile_source: str
    load_profile_confidence: float
    load_profile_data_fresh: bool
    headroom_load_profile_source: str
    reserve_load_profile_source: str
    window_forecasts: tuple[RCMRiskWindowInput, ...]
    timeline_energy_points: tuple[RCMTimelineEnergyPoint, ...]


def _shared_fallback_profile_ready(
    shared_load: Any,
    daily_load: float | None,
) -> bool:
    """Validate current fallback authority independently of history age."""

    fallback = getattr(shared_load, "fallback_daily_home_load_kwh", None)
    fallback_value = getattr(fallback, "value", None)
    return bool(
        getattr(shared_load, "fallback_currently_used", False) is True
        and getattr(shared_load, "ready", False) is True
        and getattr(fallback, "fresh", False) is True
        and type(fallback_value) in {int, float}
        and isfinite(float(fallback_value))
        and float(fallback_value) > 0.0
        and type(daily_load) in {int, float}
        and isfinite(float(daily_load))
        and abs(float(daily_load) - float(fallback_value)) <= 1e-6
    )


STATUS_TEXT = {
    "pl": {
        "ready": "Gotowa — napięcie bezpieczne",
        "learning": "Uczenie — trwa zbieranie historii napięcia",
        "preparing_headroom": "Przygotowanie miejsca w magazynie",
        "preparing_discharge": "Poranne przygotowanie miejsca — kontrolowane rozładowanie",
        "controlling": "Regulacja — ochrona eksportu przed 253 V",
        "battery_limited": "Ograniczenie baterii — brak dalszej mocy ładowania",
        "emergency": "Ochrona szybka — napięcie co najmniej 253 V",
        "emergency_actuator_unavailable": "Ochrona 253 V — brak świeżych aktuatorów",
        "battery_charge_unavailable": "Regulacja eksportu — BMS nie przyjmuje ładowania",
        "stale_voltage": "Brak świeżych napięć — sterowanie wstrzymane",
        "history_stale": "Historia napięcia nieaktualna — predykcja wstrzymana",
        "forecast_stale": "Prognoza nieaktualna — predykcja wstrzymana",
        "missing_data": "Brak wymaganych danych — sterowanie zablokowane",
        "optimizer_error": "Błąd obliczeń — sterowanie zablokowane",
    },
    "en": {
        "ready": "Ready — grid voltage safe",
        "learning": "Learning — collecting voltage history",
        "preparing_headroom": "Preparing battery headroom",
        "preparing_discharge": "Morning headroom preparation — controlled discharge",
        "controlling": "Regulating — protecting export below 253 V",
        "battery_limited": "Battery limited — no additional charge power",
        "emergency": "Fast protection — voltage at or above 253 V",
        "emergency_actuator_unavailable": "253 V protection — fresh actuators unavailable",
        "battery_charge_unavailable": "Export regulation — BMS cannot accept charge",
        "stale_voltage": "Live voltage stale — control held",
        "history_stale": "Voltage history stale — prediction suspended",
        "forecast_stale": "Forecast stale — prediction suspended",
        "missing_data": "Required data missing — control blocked",
        "optimizer_error": "Calculation error — control blocked",
    },
}


def _minutes_text(minutes: int | None) -> str | None:
    if minutes is None:
        return None
    day = "+1d " if minutes >= 24 * 60 else ""
    minute = minutes % (24 * 60)
    return f"{day}{minute // 60:02d}:{minute % 60:02d}"


def _window_text(window: tuple[int, int, float]) -> str:
    start, end, peak = window
    return f"{start // 60:02d}:{start % 60:02d}–{end // 60:02d}:{end % 60:02d} ({peak:.1f} V)"


def _state_age_seconds(state: Any, now: datetime) -> float | None:
    """Return signed age through the shared EMS data contract."""
    return state_age_seconds(state, now)


def _number_sample(
    hass: HomeAssistant,
    entity_id: str,
    *,
    now: datetime,
    max_age_seconds: float,
    minimum: float | None = None,
    maximum: float | None = None,
    runtime: RuntimeData | None = None,
    shared_section: str | None = None,
    shared_field: str | None = None,
    shared_scale: float = 1.0,
    legacy_scale: float = 1.0,
) -> tuple[float | None, bool, float | None]:
    """Return numeric value, freshness and age for one HA entity."""
    sample = (
        _policy_numeric_sample(
            runtime,
            shared_section,
            shared_field,
            hass.states.get(entity_id),
            now,
            max_age_seconds=max_age_seconds,
            shared_scale=shared_scale,
            legacy_scale=legacy_scale,
            minimum=minimum,
            maximum=maximum,
        )
        if runtime is not None
        and shared_section is not None
        and shared_field is not None
        else numeric_state_sample(
            hass.states.get(entity_id),
            now,
            max_age_seconds=max_age_seconds,
            scale=legacy_scale,
            minimum=minimum,
            maximum=maximum,
        )
    )
    return sample.value, sample.fresh, sample.age_seconds


def _rcm_shared_sample_signature(sample: Any) -> tuple[Any, ...] | None:
    """Fingerprint one broker value with its physical/source provenance."""

    if sample is None:
        return None
    reported_at = getattr(sample, "reported_at", None)
    return (
        getattr(sample, "value", None),
        getattr(sample, "entity_id", None),
        tuple(getattr(sample, "source_entity_ids", ()) or ()),
        getattr(sample, "selector_entity_id", None),
        (
            reported_at.isoformat()
            if isinstance(reported_at, datetime)
            else reported_at
        ),
        getattr(sample, "fresh", None),
        getattr(sample, "reason", None),
        getattr(sample, "quality", None),
        getattr(sample, "provenance", None),
    )


def _rcm_shared_optimizer_signature(
    runtime: RuntimeData,
) -> tuple[Any, ...] | None:
    """Fingerprint exactly the shared RCEm inputs consumed by one run.

    The coordinator revision is intentionally absent.  Values, source
    identity, report time/freshness and load-model provenance are inputs and
    must invalidate an in-flight result. ``captured_at`` is retained because
    the current LOAD freshness contract explicitly consumes it.
    """

    snapshot = _shared_inputs_snapshot(runtime)
    if snapshot is None:
        return None
    system = getattr(snapshot, "system", None)
    forecast = getattr(snapshot, "forecast", None)
    load = getattr(snapshot, "load", None)
    bms = getattr(snapshot, "bms", None)
    efficiency = getattr(snapshot, "efficiency", None)
    gcf = getattr(snapshot, "gcf", None)
    power = getattr(snapshot, "power", None)
    captured_at = getattr(snapshot, "captured_at", None)
    return (
        getattr(snapshot, "schema_version", None),
        getattr(snapshot, "config_entry_id", None),
        (
            captured_at.isoformat()
            if isinstance(captured_at, datetime)
            else captured_at
        ),
        tuple(
            _rcm_shared_sample_signature(getattr(system, field_name, None))
            for field_name in (
                "battery_capacity_kwh",
                "total_capacity_kwh",
                "battery_soc_percent",
                "self_use_reserve_soc_percent",
                "inverter_count",
                "inverter_rated_power_each_kw",
            )
        ),
        tuple(
            _rcm_shared_sample_signature(getattr(forecast, field_name, None))
            for field_name in ("today", "tomorrow", "remaining_today")
        ),
        (
            getattr(load, "average_daily_home_load_kwh", None),
            tuple(getattr(load, "daily_totals_kwh", ()) or ()),
            tuple(getattr(load, "average_profile_30m_kwh", ()) or ()),
            tuple(getattr(load, "weekday_profile_30m_kwh", ()) or ()),
            tuple(getattr(load, "weekend_profile_30m_kwh", ()) or ()),
            (
                getattr(load, "generated_at", None).isoformat()
                if isinstance(getattr(load, "generated_at", None), datetime)
                else getattr(load, "generated_at", None)
            ),
            getattr(load, "ready", None),
            getattr(load, "fallback_currently_used", None),
            getattr(load, "source", None),
        ),
        tuple(
            _rcm_shared_sample_signature(getattr(bms, field_name, None))
            for field_name in (
                "voltage_v",
                "maximum_charge_current_a",
                "maximum_discharge_current_a",
            )
        ),
        _rcm_shared_sample_signature(
            getattr(efficiency, "battery_to_home_efficiency", None)
        ),
        (
            _rcm_shared_sample_signature(getattr(gcf, "enable_code", None)),
            _rcm_shared_sample_signature(
                getattr(gcf, "maximum_export_power_percent", None)
            ),
            getattr(gcf, "gcf_readback_ready", None),
        ),
        tuple(
            _rcm_shared_sample_signature(getattr(power, field_name, None))
            for field_name in (
                "pv_power_kw",
                "home_load_power_kw",
                "grid_power_kw",
            )
        ),
    )


def _rcm_shared_live_control_signature(
    runtime: RuntimeData,
) -> tuple[Any, ...] | None:
    """Fingerprint only broker facts resampled by the 15-second path."""

    snapshot = _shared_inputs_snapshot(runtime)
    if snapshot is None:
        return None
    system = getattr(snapshot, "system", None)
    bms = getattr(snapshot, "bms", None)
    efficiency = getattr(snapshot, "efficiency", None)
    gcf = getattr(snapshot, "gcf", None)
    power = getattr(snapshot, "power", None)
    return (
        getattr(snapshot, "schema_version", None),
        getattr(snapshot, "config_entry_id", None),
        tuple(
            _rcm_shared_sample_signature(getattr(system, field_name, None))
            for field_name in (
                "battery_capacity_kwh",
                "total_capacity_kwh",
                "battery_soc_percent",
                "self_use_reserve_soc_percent",
                "inverter_count",
                "inverter_rated_power_each_kw",
            )
        ),
        tuple(
            _rcm_shared_sample_signature(getattr(bms, field_name, None))
            for field_name in (
                "voltage_v",
                "maximum_charge_current_a",
                "maximum_discharge_current_a",
            )
        ),
        _rcm_shared_sample_signature(
            getattr(efficiency, "battery_to_home_efficiency", None)
        ),
        (
            _rcm_shared_sample_signature(getattr(gcf, "enable_code", None)),
            _rcm_shared_sample_signature(
                getattr(gcf, "maximum_export_power_percent", None)
            ),
            getattr(gcf, "gcf_readback_ready", None),
        ),
        tuple(
            _rcm_shared_sample_signature(getattr(power, field_name, None))
            for field_name in (
                "pv_power_kw",
                "home_load_power_kw",
                "grid_power_kw",
            )
        ),
    )


def _rcm_voltage_samples_signature(
    samples: deque[tuple[datetime, float]],
) -> tuple[tuple[str, float], ...]:
    """Freeze the rolling voltage cohort consumed by the live evaluator."""

    return tuple(
        (timestamp.astimezone(timezone.utc).isoformat(), float(value))
        for timestamp, value in samples
    )


def _rcm_live_control_fingerprint(
    hass: HomeAssistant,
    runtime: RuntimeData,
    samples: deque[tuple[datetime, float]],
) -> tuple[Any, ...]:
    """Certify a live executor result without scheduling a full plan."""

    return (
        optimizer_input_fingerprint(
            hass,
            RCM_LIVE_CONTROL_CERTIFICATION_ENTITIES,
        ),
        _rcm_shared_live_control_signature(runtime),
        _rcm_voltage_samples_signature(samples),
    )


def _stable_battery_capacity(
    hass: HomeAssistant,
    runtime: RuntimeData | None = None,
) -> tuple[float | None, str]:
    """Return the positive configured/effective capacity without an age gate.

    Capacity is a stable installation property, unlike dynamic BMS voltage and
    current limits.  Prefer register 4102 and retain the published Total
    Capacity entity as the same 1907-backed fallback used by the firmware.
    Missing, non-finite and non-positive values remain fail-closed.
    """
    if runtime is not None:
        for field_name in ("battery_capacity_kwh", "total_capacity_kwh"):
            sample = _shared_input_field(runtime, "system", field_name)
            if sample is _SHARED_INPUT_MISSING:
                break
            value = getattr(sample, "value", None)
            if type(value) in {int, float} and isfinite(float(value)) and value > 0.0:
                source = getattr(sample, "entity_id", None)
                return float(value), source if isinstance(source, str) else field_name
    for entity_id in BATTERY_CAPACITY_ENTITIES:
        value = _state_number(hass, entity_id)
        if value is not None and isfinite(value) and value > 0.0:
            return value, entity_id
    return None, ""


def _utc_quarter_ceiling(value: datetime) -> datetime:
    """Return the next UTC quarter boundary without local wall-clock stepping."""

    utc_value = value.astimezone(timezone.utc)
    discarded = utc_value.minute % 15 or utc_value.second or utc_value.microsecond
    if discarded:
        utc_value += timedelta(minutes=15 - utc_value.minute % 15)
    return utc_value.replace(second=0, microsecond=0)


def _timeline_forecast_target_date(
    *,
    now: datetime,
    risk_day_offset: int,
) -> date:
    """Select the forecast day that can still provide a native timeline slot."""

    if risk_day_offset > 0:
        return now.date() + timedelta(days=1)
    if risk_day_offset == 0:
        return now.date()
    local_zone = now.tzinfo
    assert local_zone is not None
    current_day_end = datetime.combine(
        now.date() + timedelta(days=1),
        time.min,
        tzinfo=local_zone,
    ).astimezone(timezone.utc)
    if _utc_quarter_ceiling(now) >= current_day_end:
        return now.date() + timedelta(days=1)
    return now.date()


def _timeline_horizon_bounds(
    *,
    now: datetime,
    target_date: date,
    risk_day_offset: int,
) -> tuple[datetime, datetime]:
    """Return the exact requested UTC horizon without wall-clock stepping."""

    local_zone = now.tzinfo
    assert local_zone is not None
    local_midnight = datetime.combine(target_date, time.min, tzinfo=local_zone)
    local_end = datetime.combine(
        target_date + timedelta(days=1),
        time.min,
        tzinfo=local_zone,
    )
    current_day = target_date == now.date()
    start = (
        now.astimezone(timezone.utc)
        if current_day
        else local_midnight.astimezone(timezone.utc)
    )
    return start, local_end.astimezone(timezone.utc)


def _timeline_energy_points(
    *,
    now: datetime,
    target_date: date,
    risk_day_offset: int,
    pv_profile: tuple[float, ...],
    load_profile: tuple[float, ...],
    load_by_start_kwh: dict[datetime, float] | None = None,
) -> tuple[RCMTimelineEnergyPoint, ...]:
    """Project exact 30-minute source energy onto native UTC quarter slots."""

    local_zone = now.tzinfo
    assert local_zone is not None
    start, end = _timeline_horizon_bounds(
        now=now,
        target_date=target_date,
        risk_day_offset=risk_day_offset,
    )
    current_day = target_date == now.date()
    available_start_second = (
        now.hour * 3600
        + now.minute * 60
        + now.second
        + now.microsecond / 1_000_000.0
        if current_day
        else 0.0
    )
    points: list[RCMTimelineEnergyPoint] = []
    profile_occurrences: dict[
        tuple[date, int],
        tuple[timedelta | None, int],
    ] = {}
    cursor = start
    while cursor < end and len(points) < 192:
        quarter_end = _utc_quarter_ceiling(cursor)
        if quarter_end <= cursor:
            quarter_end = cursor + timedelta(minutes=15)
        point_end = min(quarter_end, end)
        overlap_seconds = (point_end - cursor).total_seconds()
        if overlap_seconds <= 0.0 or overlap_seconds > 15 * 60:
            break
        local_start = cursor.astimezone(local_zone)
        slot = local_start.hour * 2 + local_start.minute // 30
        source_slot = (local_start.date(), slot)
        occurrence = (local_start.utcoffset(), local_start.fold)
        selected_occurrence = profile_occurrences.setdefault(
            source_slot,
            occurrence,
        )
        source_covered = occurrence == selected_occurrence
        slot_start_minute = slot * 30
        available_seconds = 30 * 60.0
        if (
            current_day
            and slot_start_minute * 60
            <= available_start_second
            < (slot_start_minute + 30) * 60
        ):
            available_seconds = max(
                (slot_start_minute + 30) * 60 - available_start_second,
                1.0,
            )
        points.append(
            RCMTimelineEnergyPoint(
                start=cursor,
                end=point_end,
                pv_kwh=(
                    pv_profile[slot] * overlap_seconds / available_seconds
                    if source_covered
                    else None
                ),
                load_kwh=(
                    load_by_start_kwh.get(floor_half_hour(cursor), 0.)
                    * overlap_seconds / (30 * 60.0)
                    if load_by_start_kwh is not None else
                    load_profile[slot] * overlap_seconds / (30 * 60.0)
                    if source_covered else None
                ),
            )
        )
        cursor = point_end
    return tuple(points)


def _timeline_risk_intervals(
    now: datetime,
    result: Any,
) -> tuple[RCMTimelineRiskInterval, ...]:
    """Convert existing optimizer risk plans to absolute UTC intervals."""

    local_zone = now.tzinfo
    assert local_zone is not None
    intervals: list[RCMTimelineRiskInterval] = []
    for plan in result.risk_window_plans:
        target_date = now.date() + timedelta(days=plan.day_offset)
        midnight = datetime.combine(target_date, time.min, tzinfo=local_zone)
        start = midnight + timedelta(minutes=plan.start_minute)
        end = midnight + timedelta(minutes=plan.end_minute)
        if end <= start:
            end += timedelta(days=1)
        intervals.append(
            RCMTimelineRiskInterval(
                start=start.astimezone(timezone.utc),
                end=end.astimezone(timezone.utc),
                voltage_risk_code="historical_risk_window",
                required_headroom_kwh=plan.required_headroom_kwh,
                headroom_shortfall_kwh=(
                    plan.cumulative_headroom_shortfall_kwh
                ),
            )
        )
    return tuple(intervals)


class HoymilesRCMOptimizerSensor(SensorEntity):
    """Expose voltage history, battery headroom and a safe charge setpoint."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    # Retain states, actions, power limits and readiness flags. These detailed
    # inputs/profiles remain live; duplicating them at every control tick is costly.
    _unrecorded_attributes = frozenset({
        "data_freshness", "data_age_seconds", "history_profile_median_v",
        "history_profile_p90_v", "history_daily_peak_v", "risk_window_details",
        "risk_window_energy_plans", "risk_surplus_horizon",
        "forecast_source_age_seconds", "history_age_seconds",
        "live_control_cache_age_seconds", "live_control_ticks",
        "live_control_updated_at", "load_profile_broker_age_seconds",
        # Raw phase voltages have their own recorded sensors. Keep the live
        # values for automation/UI, and retain maximum/rolling voltage plus
        # status and limits in the historical plan.
        "voltage_l1_v", "voltage_l2_v", "voltage_l3_v",
        "filtered_voltage_v", "pv_surplus_power_kw",
    })
    _attr_translation_key = "rcm_voltage_plan"
    _attr_icon = "mdi:transmission-tower-export"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: RuntimeData,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_rcm_voltage_plan"
        self._history = VoltageHistorySummary(
            history_days=0,
            sample_count=0,
            profile_median_v=(0.0,) * SLOTS_PER_DAY,
            profile_p90_v=(0.0,) * SLOTS_PER_DAY,
            daily_peak_v={},
            risk_windows=(),
        )
        self._samples: deque[tuple[datetime, float]] = deque()
        self._history_refresh_running = False
        self._history_refreshed_at: datetime | None = None
        self._startup_warmup_task: asyncio.Task[None] | None = None
        self._optimizer_lock = asyncio.Lock()
        self._input_revision = OptimizerInputRevision()
        self._full_plan_recalculate_cancel: Callable[[], None] | None = None
        self._forecast_batch_recalculate_cancel: Callable[[], None] | None = None
        self._stale_result_retry_cancel: Callable[[], None] | None = None
        self._full_plan_rejected_for_input_drift = False
        self._required_input_recovery_pending = False
        self._full_plan_solver_calls = 0
        self._live_control_ticks = 0
        self._live_emergency_active = False
        self._last_full_optimizer_input: RCMOptimizerInput | None = None
        self._last_full_optimizer_result: Any | None = None
        self._last_full_plan_at: datetime | None = None
        self._full_plan_trigger = "startup"
        self._timeline_sensor: Any | None = None
        self._rce_plan_source: SensorEntity | None = None
        self._timeline_trace: Any | None = None
        self._last_full_timeline_input: RCMTimelineModelInput | None = None
        self._last_full_timeline_trace: Any | None = None
        self._last_full_timeline_metadata: dict[str, Any] = {}
        self._live_timeline_signature: tuple[Any, ...] | None = None
        self._timeline_blocker = "awaiting_first_calculation"
        self._timeline_metadata: dict[str, Any] = {}
        self._dynamic_forecast_listener_entities: frozenset[str] = frozenset()
        self._dynamic_forecast_listener_unsub: Callable[[], None] | None = None
        self._attributes: dict[str, Any] = {
            "status_code": "missing_data",
            "missing_entities": [],
            "risk_windows": [],
            "result_current": False,
            "recalculation_pending": True,
            "input_revision": 0,
            "full_plan_solver_calls": 0,
            "last_full_plan_at": None,
            "last_full_plan_trigger": "startup",
            "live_control_ticks": 0,
        }

    @property
    def suggested_object_id(self) -> str:
        return "hoymiles_hit_rcm_voltage_plan"

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
    def native_value(self) -> str:
        language = "pl" if self.hass.config.language.startswith("pl") else "en"
        code = str(self._attributes.get("status_code", "missing_data"))
        return STATUS_TEXT[language].get(code, STATUS_TEXT[language]["optimizer_error"])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attributes

    def attach_timeline_sensor(self, timeline_sensor: Any) -> None:
        """Bind the one entry-local observation-only RCEm publisher."""

        if self._timeline_sensor is not None and self._timeline_sensor is not timeline_sensor:
            raise RuntimeError("RCEm timeline sensor already attached")
        self._timeline_sensor = timeline_sensor

    def attach_rce_plan_source(self, rce_plan_source: SensorEntity) -> None:
        """Bind the exact same-entry legacy LOAD provider."""

        source_entry = getattr(rce_plan_source, "_entry", None)
        if source_entry is None or source_entry.entry_id != self._entry.entry_id:
            raise RuntimeError("RCEm LOAD broker belongs to another entry")
        self._rce_plan_source = rce_plan_source

    def _same_entry_rce_plan_state(self) -> State | None:
        """Return only the RCE entity explicitly wired for this entry."""

        source = self._rce_plan_source
        source_entry = getattr(source, "_entry", None)
        entity_id = getattr(source, "entity_id", None)
        if (
            source is None
            or source_entry is None
            or source_entry.entry_id != self._entry.entry_id
            or not isinstance(entity_id, str)
        ):
            return None
        return self.hass.states.get(entity_id)

    def _load_broker_entity_id(self) -> str | None:
        entity_id = getattr(self._rce_plan_source, "entity_id", None)
        return entity_id if isinstance(entity_id, str) else None

    @callback
    def _publish_timeline_result(self) -> None:
        """Publish only the model built from the committed input revision."""

        if self._timeline_sensor is None:
            return
        if self._timeline_trace is None:
            self._timeline_sensor.publish_unavailable(
                input_revision=self._input_revision.value,
                blocker_code=self._timeline_blocker,
            )
            return
        self._timeline_sensor.publish_current(
            self._timeline_trace,
            input_revision=self._input_revision.value,
            metadata=self._timeline_metadata,
        )

    def _refresh_live_observation_timeline(
        self,
        *,
        now: datetime,
        result: Any,
        current_soc_percent: float | None,
        discharge_power_limit_kw: float,
        pv_power_kw: float,
        load_power_kw: float,
    ) -> bool:
        """Record live observation identity without rewriting future geometry.

        The main RCEm sensor already publishes the current action, SOC, PV and
        LOAD observation.  Rebuilding the timeline from those 15-second values
        made observation time look like a new plan revision and shifted the
        common chart horizon by seconds.  A timeline is therefore committed
        only by the serialized full solver path.
        """

        del now, current_soc_percent, discharge_power_limit_kw, pv_power_kw, load_power_kw
        self._live_timeline_signature = (
            result.action,
            result.action_interval_start,
            result.action_interval_end,
        )
        return False

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                sorted(self._event_driven_rcm_entities()),
                self._async_input_changed,
            )
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                sorted(RCM_FORECAST_VALUE_ENTITIES),
                self._async_forecast_value_changed,
            )
        )
        refresh_dynamic_forecast_listener = getattr(
            self,
            "_refresh_dynamic_forecast_listener",
            None,
        )
        if callable(refresh_dynamic_forecast_listener):
            refresh_dynamic_forecast_listener()
        self.async_on_remove(lambda: self._remove_dynamic_forecast_listener())
        self.async_on_remove(self._cancel_full_plan_recalculation)
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_control_timer,
                timedelta(seconds=15),
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_full_plan_timer,
                RCM_FULL_PLAN_INTERVAL,
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_history_timer,
                RCM_HISTORY_REFRESH_INTERVAL,
            )
        )
        self.async_write_ha_state()
        self._schedule_startup_warmup()

    @callback
    def _configured_forecast_entities(self) -> frozenset[str]:
        """Resolve arbitrary forecast entity IDs selected by the helpers."""
        configured: set[str] = set()
        snapshot = getattr(
            getattr(getattr(self, "_runtime", None), "shared_inputs", None),
            "snapshot",
            None,
        )
        forecast = getattr(snapshot, "forecast", None)
        if forecast is not None:
            for field_name in ("today", "tomorrow", "day3"):
                entity_id = getattr(
                    getattr(forecast, field_name, None),
                    "entity_id",
                    None,
                )
                if isinstance(entity_id, str) and _FORECAST_ENTITY_ID.fullmatch(
                    entity_id.strip().lower()
                ):
                    configured.add(entity_id.strip().lower())
        else:
            for helper_entity_id in FORECAST_ENTITY_HELPERS:
                entity_id = _state_text(self.hass, helper_entity_id).strip().lower()
                if entity_id and _FORECAST_ENTITY_ID.fullmatch(entity_id):
                    configured.add(entity_id)
        own_entity_id = getattr(self, "entity_id", None)
        if isinstance(own_entity_id, str) and own_entity_id:
            configured.discard(own_entity_id)
        return frozenset(configured)

    @callback
    def _watched_rcm_entities(self) -> frozenset[str]:
        """Return static inputs plus the exact configured forecast sources."""
        watched = set(WATCHED_RCM_ENTITIES).union(
            self._configured_forecast_entities()
        )
        rce_entity_id = getattr(
            getattr(self, "_rce_plan_source", None),
            "entity_id",
            None,
        )
        if rce_entity_id is not None:
            watched.add(rce_entity_id)
        return frozenset(watched)

    @callback
    def _event_driven_rcm_entities(self) -> frozenset[str]:
        """Return immediate inputs without forecast value publications."""

        # The same-entry RCE entity is only a legacy LOAD broker.  Its normal
        # five-minute publication is not an immediate RCEm setting change;
        # the ten-minute full-plan pass samples it when Shared EMS LOAD is not
        # available.
        return RCM_EVENT_DRIVEN_ENTITIES

    @callback
    def _remove_dynamic_forecast_listener(self) -> None:
        """Remove the current custom-forecast listener, if one is installed."""
        if self._dynamic_forecast_listener_unsub is not None:
            self._dynamic_forecast_listener_unsub()
            self._dynamic_forecast_listener_unsub = None
        self._dynamic_forecast_listener_entities = frozenset()

    @callback
    def _refresh_dynamic_forecast_listener(self) -> None:
        """Rebind state tracking when a configured forecast entity changes."""
        desired = self._configured_forecast_entities().difference(
            WATCHED_RCM_ENTITIES
        )
        if desired == self._dynamic_forecast_listener_entities:
            return
        self._remove_dynamic_forecast_listener()
        self._dynamic_forecast_listener_entities = frozenset(desired)
        if desired:
            self._dynamic_forecast_listener_unsub = (
                async_track_state_change_event(
                    self.hass,
                    sorted(desired),
                    self._async_forecast_value_changed,
                )
            )

    @callback
    def _schedule_startup_warmup(self) -> None:
        """Start exactly one cancel-safe Recorder and optimizer warmup."""
        if (
            self._startup_warmup_task is not None
            and not self._startup_warmup_task.done()
        ):
            return
        task = self._entry.async_create_background_task(
            self.hass,
            self._async_startup_warmup(),
            "hoymiles RCEm optimizer startup warmup",
        )
        self._startup_warmup_task = task
        self.async_on_remove(task.cancel)

    async def _async_startup_warmup(self) -> None:
        """Warm Recorder models after the fail-closed entity is registered."""
        try:
            await self._async_refresh_voltage_history()
            self._full_plan_trigger = "startup"
            self._invalidate_internal_inputs()
            await self._recalculate_and_write()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - retain the fail-closed initial state
            _LOGGER.exception("Cannot complete the RCEm optimizer startup warmup")

    @callback
    def _current_input_fingerprint(self) -> tuple[Any, ...]:
        """Certify every value/provenance fact consumed by a full-plan run."""
        rce_entity_id = getattr(
            getattr(self, "_rce_plan_source", None),
            "entity_id",
            None,
        )
        shared_signature = globals().get("_rcm_shared_optimizer_signature")
        base_fingerprint = optimizer_input_fingerprint(
            self.hass,
            self._full_plan_input_entities(),
            attribute_projections=(
                {rce_entity_id: RCE_LOAD_BROKER_ATTRIBUTES}
                if rce_entity_id is not None
                else {}
            ),
        )
        # Live inputs do not schedule a full-plan rebuild, but a full run does
        # consume them.  Certify that awaited result against their exact
        # value, physical report time and source attributes as a separate
        # snapshot so cadence and publication safety remain independent.
        live_fingerprint = optimizer_input_fingerprint(
            self.hass,
            RCM_LIVE_CONTROL_ENTITIES,
        )
        return (
            *base_fingerprint,
            *live_fingerprint,
            (
                "shared_inputs",
                shared_signature(self._runtime)
                if callable(shared_signature) and hasattr(self, "_runtime")
                else None,
            ),
            ("voltage_samples", _rcm_voltage_samples_signature(self._samples)),
        )

    @callback
    def _invalidate_input_event(
        self,
        event: Event[EventStateChangedData],
    ) -> bool:
        """Invalidate only when a value consumed by this optimizer changed."""
        entity_id = event.data["entity_id"]
        counterpart = entity_id == self._load_broker_entity_id()
        changed = self._input_revision.invalidate_state_change(
            event.data.get("old_state"),
            event.data.get("new_state"),
            attributes=(RCE_LOAD_BROKER_ATTRIBUTES if counterpart else None),
            include_state=not counterpart,
            # Report cadence alone is not a risk-plan input.  This keeps
            # counterpart availability/consumed fields fail-closed without
            # turning timestamp churn into a solver restart loop.
            include_last_updated=False,
        )
        if changed:
            self._mark_recalculation_pending()
        return changed

    @callback
    def _invalidate_internal_inputs(self) -> None:
        """Invalidate after recorder/clock-backed inputs change."""
        self._input_revision.invalidate()
        self._mark_recalculation_pending()

    @callback
    def _mark_recalculation_pending(self) -> None:
        """Withdraw execution authority once while a replacement is pending."""
        if (
            self._attributes.get("result_current") is False
            and self._attributes.get("recalculation_pending") is True
        ):
            if self._timeline_sensor is not None:
                self._timeline_sensor.publish_pending(self._input_revision.value)
            return
        self._attributes = {
            **self._attributes,
            "result_current": False,
            "recalculation_pending": True,
        }
        self.async_write_ha_state()
        if self._timeline_sensor is not None:
            self._timeline_sensor.publish_pending(self._input_revision.value)

    @callback
    def _mark_result_current(self) -> None:
        """Mark the committed plan as matching the latest input revision."""
        self._attributes = {
            **self._attributes,
            "result_current": True,
            "recalculation_pending": False,
            "input_revision": self._input_revision.value,
        }

    def _full_plan_input_entities(self) -> frozenset[str]:
        """Return only values that rebuild the forecast/risk trajectory."""

        entities = set(self._watched_rcm_entities().difference(
            RCM_LIVE_CONTROL_ENTITIES
        ))
        shared_snapshot = _shared_inputs_snapshot(
            getattr(self, "_runtime", None)
        )
        if getattr(shared_snapshot, "load", None) is not None:
            broker_entity_id = self._load_broker_entity_id()
            if broker_entity_id is not None:
                entities.discard(broker_entity_id)
        return frozenset(entities)

    @callback
    def _schedule_full_plan_recalculation(self) -> None:
        """Debounce one explicit full risk-plan rebuild."""

        self._cancel_stale_result_retry()
        if self._full_plan_recalculate_cancel is not None:
            return
        self._full_plan_recalculate_cancel = async_call_later(
            self.hass,
            RCM_FULL_PLAN_RECALCULATION_DELAY_SECONDS,
            self._async_debounced_full_plan_recalculation,
        )

    @callback
    def _schedule_forecast_batch_recalculation(self) -> None:
        """Use one trailing-edge rebuild for a complete forecast cohort."""

        self._cancel_stale_result_retry()
        if self._forecast_batch_recalculate_cancel is not None:
            self._forecast_batch_recalculate_cancel()
        self._forecast_batch_recalculate_cancel = async_call_later(
            self.hass,
            RCM_FORECAST_BATCH_DELAY_SECONDS,
            self._async_debounced_forecast_batch_recalculation,
        )

    @callback
    def _schedule_stale_result_retry(self) -> None:
        """Queue one bounded retry after an in-flight input drift."""

        if self._stale_result_retry_cancel is not None:
            return
        self._stale_result_retry_cancel = async_call_later(
            self.hass,
            RCM_STALE_RESULT_RETRY_DELAY_SECONDS,
            self._async_stale_result_retry,
        )

    @callback
    def _cancel_stale_result_retry(self) -> None:
        """Cancel a retry superseded by a newer explicit plan trigger."""

        if self._stale_result_retry_cancel is not None:
            self._stale_result_retry_cancel()
            self._stale_result_retry_cancel = None

    @callback
    def _cancel_full_plan_recalculation(self) -> None:
        self._cancel_stale_result_retry()
        if self._full_plan_recalculate_cancel is not None:
            self._full_plan_recalculate_cancel()
            self._full_plan_recalculate_cancel = None
        if self._forecast_batch_recalculate_cancel is not None:
            self._forecast_batch_recalculate_cancel()
            self._forecast_batch_recalculate_cancel = None
        self._required_input_recovery_pending = False

    async def _async_debounced_full_plan_recalculation(
        self,
        _now: datetime,
    ) -> None:
        self._full_plan_recalculate_cancel = None
        try:
            await self._recalculate_and_write()
        finally:
            self._required_input_recovery_pending = False

    async def _async_debounced_forecast_batch_recalculation(
        self,
        _now: datetime,
    ) -> None:
        self._forecast_batch_recalculate_cancel = None
        await self._recalculate_and_write()

    async def _async_stale_result_retry(self, _now: datetime) -> None:
        """Run one delayed retry batch without re-arming on another drift."""

        self._stale_result_retry_cancel = None
        self._full_plan_trigger = "stale_result_retry"
        await self._recalculate_and_write(allow_deferred_retry=False)

    @callback
    def _async_forecast_value_changed(
        self,
        event: Event[EventStateChangedData],
    ) -> None:
        """Coalesce all fields of one forecast publication into one plan."""

        if not self._invalidate_input_event(event):
            return
        self._full_plan_trigger = "forecast_batch"
        self._schedule_forecast_batch_recalculation()

    async def _async_input_changed(self, event: Event[EventStateChangedData]) -> None:
        if event.data["entity_id"] in FORECAST_ENTITY_HELPERS:
            self._refresh_dynamic_forecast_listener()
        entity_id = event.data["entity_id"]
        if entity_id in RCM_LIVE_CONTROL_ENTITIES:
            if entity_id in GRID_VOLTAGE_ENTITIES:
                self._append_voltage_sample()
            # Threshold transitions are published immediately by the live
            # control path.  They must not rebuild the full 48-hour forecast;
            # the executor consumes the live safety/eligibility attributes
            # independently of the bounded risk-plan cadence.
            await self._async_live_control_refresh(dt_util.now())
            missing_entities = self._attributes.get("missing_entities", ())
            new_state = event.data.get("new_state")
            recovered = False
            if entity_id in missing_entities and new_state is not None:
                try:
                    recovered = isfinite(float(new_state.state))
                except (TypeError, ValueError):
                    recovered = False
            if recovered and not self._required_input_recovery_pending:
                # A source which made the previous full plan incomplete gets
                # one bounded recovery pass. Ordinary live telemetry remains
                # on the fast safety path and never rebuilds the risk plan.
                self._required_input_recovery_pending = True
                self._full_plan_trigger = "required_input_recovered"
                self._input_revision.invalidate()
                self._mark_recalculation_pending()
                self._schedule_full_plan_recalculation()
            return
        if not self._invalidate_input_event(event):
            return
        self._full_plan_trigger = "immediate_input"
        self._schedule_full_plan_recalculation()

    async def _async_control_timer(self, now: datetime) -> None:
        self._refresh_dynamic_forecast_listener()
        self._append_voltage_sample(now)
        # The 15-second loop remains immediate for status, eligibility and
        # safety, but never becomes a full forecast/risk-plan trigger.
        await self._async_live_control_refresh(now)

    async def _async_full_plan_timer(self, _now: datetime) -> None:
        """Rebuild the forecast/risk timeline no more than every ten minutes."""

        history_age_seconds: float | None = None
        if self._history_refreshed_at is not None:
            try:
                history_age_seconds = (
                    _now.astimezone(timezone.utc)
                    - self._history_refreshed_at.astimezone(timezone.utc)
                ).total_seconds()
            except (AttributeError, OverflowError, TypeError, ValueError):
                history_age_seconds = None
        if self._history_refresh_running or (
            history_age_seconds is not None
            and history_age_seconds
            >= RCM_HISTORY_REFRESH_INTERVAL.total_seconds() - 30.0
        ):
            # The hourly callback owns this boundary: it refreshes Recorder
            # first and then publishes one plan from the new history cohort.
            return
        self._cancel_full_plan_recalculation()
        self._full_plan_trigger = "periodic"
        self._invalidate_internal_inputs()
        await self._recalculate_and_write()

    async def _async_history_timer(self, now: datetime) -> None:
        self._cancel_full_plan_recalculation()
        await self._async_refresh_voltage_history()
        self._full_plan_trigger = "history_refresh"
        self._invalidate_internal_inputs()
        await self._recalculate_and_write()

    async def _async_live_control_refresh(self, now: datetime) -> bool:
        """Publish one coherent 15-second physical control decision.

        The expensive forecast/history model remains frozen in the last full
        optimizer input.  Only current physical measurements, permissions and
        actuator gates are resampled here.  A bounded feedback evaluator
        tightens the last certified prediction; it never invokes the full
        forecast/risk optimizer from a telemetry tick.
        """

        async with self._optimizer_lock:
            local_now = now.astimezone(ZoneInfo(self.hass.config.time_zone))
            captured_live_fingerprint = _rcm_live_control_fingerprint(
                self.hass,
                self._runtime,
                self._samples,
            )
            existing_freshness = self._attributes.get("data_freshness", {})
            existing_ages = self._attributes.get("data_age_seconds", {})
            freshness = (
                dict(existing_freshness)
                if isinstance(existing_freshness, dict)
                else {}
            )
            ages = dict(existing_ages) if isinstance(existing_ages, dict) else {}

            def sample(
                entity_id: str,
                max_age_seconds: float,
                *,
                minimum: float | None = None,
                maximum: float | None = None,
                shared_section: str | None = None,
                shared_field: str | None = None,
                shared_scale: float = 1.0,
                legacy_scale: float = 1.0,
            ) -> tuple[float | None, bool]:
                value, fresh, age = _number_sample(
                    self.hass,
                    entity_id,
                    now=local_now,
                    max_age_seconds=max_age_seconds,
                    minimum=minimum,
                    maximum=maximum,
                    runtime=(
                        self._runtime if shared_section is not None else None
                    ),
                    shared_section=shared_section,
                    shared_field=shared_field,
                    shared_scale=shared_scale,
                    legacy_scale=legacy_scale,
                )
                freshness[entity_id] = fresh
                ages[entity_id] = round(age, 1) if age is not None else None
                return value, fresh

            voltage_samples = {
                entity_id: sample(
                    entity_id,
                    LIVE_TELEMETRY_MAX_AGE_SECONDS,
                )
                for entity_id in GRID_VOLTAGE_ENTITIES
            }
            phase_values = [
                value
                if (
                    fresh
                    and value is not None
                    and isfinite(value)
                    and value > 0.0
                )
                else None
                for value, fresh in voltage_samples.values()
            ]
            fresh_phase_values = [value for value in phase_values if value is not None]
            predictive_phase_values = [
                value
                if value is not None and 180.0 <= value <= 280.0
                else None
                for value in phase_values
            ]
            voltage_data_fresh = (
                all(value is not None for value in predictive_phase_values)
            )
            emergency_voltage_data_fresh = bool(fresh_phase_values)
            live_phase_values = [
                value if value is not None else 0.0 for value in phase_values
            ]
            live_maximum_voltage = max(fresh_phase_values, default=0.0)
            recent = [
                value
                for timestamp, value in self._samples
                if timestamp >= local_now - timedelta(seconds=60)
            ]
            rolling = [value for _timestamp, value in self._samples]
            filtered_voltage = (
                median(recent) if recent else live_maximum_voltage
            )
            rolling_voltage = (
                sum(rolling) / len(rolling)
                if rolling
                else live_maximum_voltage
            )

            current_charge_limit, charge_actuator_fresh = sample(
                "sensor.hoymiles_hit_battery_max_charge_power_readback",
                ACTUATOR_MAX_AGE_SECONDS,
                minimum=10.0,
                maximum=100.0,
            )
            export_control_enabled = self.hass.states.is_state(
                "input_boolean.hoymiles_rcm_export_control_enabled",
                "on",
            )
            gcf_sample = _policy_numeric_sample(
                self._runtime,
                "gcf",
                "enable_code",
                self.hass.states.get(
                    "sensor.hoymiles_hit_gcf_enable_readback_code"
                ),
                local_now,
                max_age_seconds=ACTUATOR_MAX_AGE_SECONDS,
                minimum=0.0,
                maximum=1.0,
            )
            gcf_value = gcf_sample.value
            shared_gcf = getattr(
                _shared_inputs_snapshot(self._runtime),
                "gcf",
                None,
            )
            gcf_state_fresh = bool(
                gcf_sample.fresh
                and gcf_value in {0.0, 1.0}
                and (
                    shared_gcf is None
                    or getattr(shared_gcf, "gcf_readback_ready", False) is True
                )
            )
            gcf_active = bool(gcf_state_fresh and gcf_value == 1.0)
            freshness["sensor.hoymiles_hit_gcf_enable_readback_code"] = (
                gcf_state_fresh
            )
            ages["sensor.hoymiles_hit_gcf_enable_readback_code"] = (
                round(gcf_sample.age_seconds, 1)
                if gcf_sample.age_seconds is not None
                else None
            )
            current_export_limit, export_register_data_fresh = sample(
                "sensor.hoymiles_hit_gcf_maximum_export_power_readback",
                ACTUATOR_MAX_AGE_SECONDS,
                shared_section="gcf",
                shared_field="maximum_export_power_percent",
            )
            export_control_path_enabled = bool(
                export_control_enabled and gcf_active and gcf_state_fresh
            )
            export_actuator_fresh = bool(
                export_control_path_enabled and export_register_data_fresh
            )
            gcf_data_fresh = bool(
                gcf_state_fresh
                and (not gcf_active or export_register_data_fresh)
            )
            actuator_data_fresh = bool(
                charge_actuator_fresh
                and (
                    export_actuator_fresh
                    if export_control_path_enabled
                    else True
                )
            )

            shared_rated_power = _shared_sample_value(
                self._runtime,
                "system",
                "inverter_rated_power_each_kw",
            )
            rated_power = (
                _preferred_select_number(
                    self.hass,
                    EMS_INVERTER_RATED_POWER_HELPER,
                    LEGACY_INVERTER_RATED_POWER_HELPER,
                )
                if shared_rated_power is _SHARED_INPUT_MISSING
                else float(shared_rated_power)
                if type(shared_rated_power) in {int, float}
                and isfinite(float(shared_rated_power))
                else None
            )
            machine_count, machine_count_fresh = sample(
                "sensor.hoymiles_hit_number_of_machines_master_and_slave",
                SLOW_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="system",
                shared_field="inverter_count",
            )
            system_power_data_valid = bool(
                rated_power is not None
                and rated_power > 0.0
                and machine_count_fresh
                and machine_count is not None
                and 1.0 <= machine_count <= 10.0
            )
            inverter_count = (
                min(max(round(machine_count or 0.0), 1), 10)
                if system_power_data_valid
                else 0
            )
            # The pure optimizer requires a positive numerical scale.  Its
            # separate provenance flag blocks the charge path when topology is
            # absent while retaining an independent export-only emergency.
            system_power_kw = (
                (rated_power or 0.0) * inverter_count
                if system_power_data_valid
                else 1.0
            )

            battery_voltage, battery_voltage_fresh = sample(
                "sensor.hoymiles_hit_battery_voltage_bms",
                SLOW_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="bms",
                shared_field="voltage_v",
            )
            bms_charge_current, bms_charge_current_fresh = sample(
                "sensor.hoymiles_hit_maximum_charge_current",
                SLOW_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="bms",
                shared_field="maximum_charge_current_a",
            )
            bms_discharge_current, bms_discharge_current_fresh = sample(
                "sensor.hoymiles_hit_maximum_discharge_current",
                SLOW_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="bms",
                shared_field="maximum_discharge_current_a",
            )
            bms_charge_fresh = battery_voltage_fresh and bms_charge_current_fresh
            bms_discharge_fresh = (
                battery_voltage_fresh and bms_discharge_current_fresh
            )
            battery_soc, battery_soc_fresh = sample(
                "sensor.hoymiles_hit_overview_battery_soc",
                SLOW_TELEMETRY_MAX_AGE_SECONDS,
                minimum=0.0,
                maximum=100.0,
                shared_section="system",
                shared_field="battery_soc_percent",
            )
            self_use_soc, self_use_soc_fresh = sample(
                "sensor.hoymiles_hit_ems_self_use_soc_readback",
                ACTUATOR_MAX_AGE_SECONDS,
                minimum=10.0,
                maximum=100.0,
                shared_section="system",
                shared_field="self_use_reserve_soc_percent",
            )
            battery_capacity, battery_capacity_source = (
                _stable_battery_capacity(self.hass, self._runtime)
            )

            pv_power, pv_power_fresh = sample(
                "sensor.hoymiles_hit_overview_pv_total_power",
                LIVE_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="power",
                shared_field="pv_power_kw",
                shared_scale=1000.0,
            )
            load_power, load_power_fresh = sample(
                "sensor.hoymiles_actual_load_power",
                LIVE_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="power",
                shared_field="home_load_power_kw",
                shared_scale=1000.0,
            )
            grid_export_power, _grid_export_power_fresh = sample(
                "sensor.hoymiles_rce_grid_export_power",
                LIVE_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="power",
                shared_field="grid_power_kw",
            )
            live_power_data_fresh = bool(pv_power_fresh and load_power_fresh)

            _maximum_discharge, max_discharge_fresh = sample(
                "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
                ACTUATOR_MAX_AGE_SECONDS,
            )
            _force_discharge_soc, force_discharge_fresh = sample(
                "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
                ACTUATOR_MAX_AGE_SECONDS,
            )
            ems_mode, ems_mode_fresh = sample(
                "sensor.hoymiles_hit_ems_mode_readback_code",
                ACTUATOR_MAX_AGE_SECONDS,
            )
            ems_mode_fresh = bool(
                ems_mode_fresh and ems_mode in {0.0, 3.0, 4.0, 5.0}
            )
            discharge_registers_data_fresh = bool(
                max_discharge_fresh
                and force_discharge_fresh
                and ems_mode_fresh
            )
            pre_discharge_actuator_fresh = bool(
                discharge_registers_data_fresh
                and battery_soc_fresh
                and self_use_soc_fresh
                and battery_capacity is not None
            )

            base_input = self._last_full_optimizer_input
            result = self._last_full_optimizer_result
            completed_at = self._last_full_plan_at
            cache_age_seconds: float | None = None
            if isinstance(completed_at, datetime):
                try:
                    cache_age_seconds = (
                        local_now.astimezone(timezone.utc)
                        - completed_at.astimezone(timezone.utc)
                    ).total_seconds()
                except (AttributeError, OverflowError, TypeError, ValueError):
                    cache_age_seconds = None
            certified_cache_current = bool(
                base_input is not None
                and result is not None
                and cache_age_seconds is not None
                and -5.0 <= cache_age_seconds <= RCM_CERTIFIED_CACHE_MAX_AGE_SECONDS
                and self._attributes.get("result_current") is True
                and self._attributes.get("recalculation_pending") is False
            )
            if not certified_cache_current:
                result = None

            saved_charge_limit = _state_number(
                self.hass,
                "input_number.hoymiles_rcm_saved_battery_charge_power",
            )
            if saved_charge_limit is None or saved_charge_limit < 10.0:
                saved_charge_limit = (
                    current_charge_limit
                    if current_charge_limit is not None
                    else base_input.saved_charge_limit_percent
                    if base_input is not None
                    else 10.0
                )
            user_export_cap = _state_number(
                self.hass,
                "input_number.hoymiles_rcm_export_cap_percent",
            )
            saved_export_limit = _state_number(
                self.hass,
                "input_number.hoymiles_rcm_saved_export_limit",
            )
            if not self.hass.states.is_state(
                "input_boolean.hoymiles_rcm_export_control_active",
                "on",
            ):
                saved_export_limit = (
                    current_export_limit
                    if current_export_limit is not None
                    else base_input.current_export_limit_percent
                    if base_input is not None
                    else 0.0
                )
            elif saved_export_limit is None:
                saved_export_limit = (
                    base_input.saved_export_limit_percent
                    if base_input is not None
                    else 0.0
                )
            if user_export_cap is None:
                user_export_cap = (
                    base_input.user_export_cap_percent
                    if base_input is not None
                    else 0.0
                )

            bms_charge_telemetry_available = bool(
                system_power_data_valid
                and bms_charge_fresh
                and battery_voltage is not None
                and bms_charge_current is not None
                and battery_voltage > 0.0
                and bms_charge_current > 0.0
            )
            bms_charge_limit_kw = (
                min(
                    system_power_kw,
                    battery_voltage * bms_charge_current / 1000.0,
                )
                if bms_charge_telemetry_available
                else 0.0
            )
            minimum_charge_kw = system_power_kw * 0.10
            bms_charge_quantization_limited = bool(
                bms_charge_telemetry_available
                and bms_charge_limit_kw + 1e-6 < minimum_charge_kw
            )
            bms_charge_available = bool(
                bms_charge_telemetry_available
                and not bms_charge_quantization_limited
            )
            bms_discharge_available = bool(
                system_power_data_valid
                and bms_discharge_fresh
                and battery_voltage is not None
                and bms_discharge_current is not None
                and battery_voltage > 0.0
                and bms_discharge_current > 0.0
            )
            discharge_efficiency = (
                min(
                    max(
                        base_input.house_discharge_efficiency_percent,
                        1.0,
                    ),
                    100.0,
                )
                / 100.0
                if base_input is not None
                else 0.0
            )
            bms_discharge_dc_limit_kw = (
                battery_voltage * bms_discharge_current / 1000.0
                if bms_discharge_available
                else 0.0
            )
            bms_discharge_limit_kw = min(
                system_power_kw,
                bms_discharge_dc_limit_kw * discharge_efficiency,
            )
            maximum_charge_limit = (
                min(max(bms_charge_limit_kw * 100.0 / system_power_kw, 10.0), 100.0)
                if bms_charge_available
                else current_charge_limit
                if current_charge_limit is not None
                else 10.0
            )
            live_input_drifted = bool(
                captured_live_fingerprint
                != _rcm_live_control_fingerprint(
                    self.hass,
                    self._runtime,
                    self._samples,
                )
            )
            if live_input_drifted:
                result = None

            previous_live_emergency = getattr(
                self,
                "_live_emergency_active",
                False,
            )
            observed_emergency = bool(
                emergency_voltage_data_fresh
                and live_maximum_voltage >= 253.0
            )
            live_emergency = observed_emergency
            if result is not None and base_input is not None:
                current_charge = (
                    current_charge_limit
                    if current_charge_limit is not None
                    else base_input.current_charge_limit_percent
                )
                current_export = (
                    current_export_limit
                    if current_export_limit is not None
                    else base_input.current_export_limit_percent
                )
                control_voltage = max(
                    live_maximum_voltage,
                    filtered_voltage,
                    rolling_voltage + 0.25,
                )
                pv_surplus = (
                    max(
                        max(pv_power or 0.0, 0.0) / 1000.0
                        - max(load_power or 0.0, 0.0) / 1000.0,
                        0.0,
                    )
                    if live_power_data_fresh
                    else 0.0
                )
                emergency_charge_ready = bool(
                    charge_actuator_fresh and bms_charge_available
                )
                emergency_export_ready = bool(
                    export_control_enabled
                    and gcf_active
                    and gcf_data_fresh
                    and export_actuator_fresh
                )
                effective_export_cap = min(
                    max(
                        min(
                            current_export,
                            saved_export_limit,
                            user_export_cap,
                        )
                        if gcf_active and gcf_data_fresh
                        else user_export_cap,
                        0.0,
                    ),
                    100.0,
                )
                minute = local_now.hour * 60 + local_now.minute
                risk_window_active = bool(
                    base_input.history_data_fresh
                    and any(
                        start <= minute < end
                        if start <= end
                        else minute >= start or minute < end
                        for start, end, _peak in base_input.risk_windows
                    )
                )
                future_starts = sorted(
                    start if start > minute else start + 24 * 60
                    for start, _end, _peak in base_input.risk_windows
                )
                next_risk_start = (
                    future_starts[0]
                    if future_starts and base_input.history_data_fresh
                    else None
                )
                fast_score = min(
                    max(
                        (control_voltage - rcm_optimizer_module.P_U_START_V)
                        / (
                            rcm_optimizer_module.EMERGENCY_V
                            - rcm_optimizer_module.P_U_START_V
                        ),
                        0.0,
                    ),
                    1.0,
                )
                history_score = (
                    min(
                        max(
                            (
                                base_input.historical_p90_voltage_v
                                - rcm_optimizer_module.P_U_START_V
                            )
                            / (
                                rcm_optimizer_module.EMERGENCY_V
                                - rcm_optimizer_module.P_U_START_V
                            ),
                            0.0,
                        ),
                        1.0,
                    )
                    if base_input.history_days > 0
                    and base_input.history_data_fresh
                    else 0.0
                )
                risk_score = max(fast_score, history_score * 0.35)

                if observed_emergency:
                    status_code = (
                        "emergency"
                        if emergency_charge_ready or emergency_export_ready
                        else "emergency_actuator_unavailable"
                    )
                    action = (
                        "absorb_pv"
                        if emergency_charge_ready
                        else "limit_export"
                        if emergency_export_ready
                        else "monitor"
                    )
                    recommended_charge = (
                        maximum_charge_limit
                        if emergency_charge_ready
                        else current_charge
                    )
                    recommended_export = (
                        0.0 if emergency_export_ready else current_export
                    )
                elif not voltage_data_fresh:
                    status_code = "stale_voltage"
                    action = "hold"
                    recommended_charge = current_charge
                    recommended_export = current_export
                elif (
                    risk_window_active
                    or control_voltage >= rcm_optimizer_module.P_U_START_V
                ):
                    status_code = "controlling"
                    if not live_power_data_fresh:
                        action = "monitor"
                        recommended_charge = current_charge
                    elif not bms_charge_available:
                        status_code = "battery_charge_unavailable"
                        action = (
                            "limit_export" if emergency_export_ready else "monitor"
                        )
                        recommended_charge = current_charge
                    elif pv_surplus <= 0.05:
                        action = "monitor"
                        recommended_charge = min(
                            max(saved_charge_limit, 10.0),
                            maximum_charge_limit,
                        )
                    else:
                        desired_absorption_kw = min(
                            pv_surplus
                            * max(risk_score, 0.20 if risk_window_active else 0.0),
                            bms_charge_limit_kw,
                        )
                        target_charge = min(
                            max(
                                desired_absorption_kw / system_power_kw * 100.0,
                                10.0,
                            ),
                            maximum_charge_limit,
                        )
                        if control_voltage >= rcm_optimizer_module.WARNING_V:
                            target_charge = max(
                                target_charge,
                                min(75.0, maximum_charge_limit),
                            )
                        if control_voltage >= rcm_optimizer_module.EMERGENCY_V:
                            target_charge = maximum_charge_limit
                        recommended_charge = min(
                            max(target_charge, current_charge - 10.0),
                            current_charge + 10.0,
                            maximum_charge_limit,
                        )
                        action = (
                            "absorb_pv"
                            if recommended_charge >= current_charge
                            else "release_export"
                        )
                    if not emergency_export_ready:
                        recommended_export = current_export
                    elif current_export > effective_export_cap:
                        recommended_export = effective_export_cap
                    elif control_voltage >= rcm_optimizer_module.EMERGENCY_V:
                        recommended_export = current_export - 15.0
                    elif control_voltage >= rcm_optimizer_module.WARNING_V and (
                        result.saturated or result.available_headroom_kwh <= 0.25
                    ):
                        recommended_export = current_export - 5.0
                    else:
                        recommended_export = current_export
                    recommended_export = min(
                        max(recommended_export, 0.0),
                        max(min(saved_export_limit, user_export_cap), 0.0),
                    )
                else:
                    status_code = (
                        "ready" if result.live_emergency else result.status_code
                    )
                    action = "restore" if result.live_emergency else result.action
                    recommended_charge = (
                        min(
                            max(result.recommended_charge_limit_percent, 10.0),
                            maximum_charge_limit,
                        )
                        if charge_actuator_fresh and bms_charge_available
                        else current_charge
                    )
                    recommended_export = (
                        min(
                            max(result.recommended_export_limit_percent, 0.0),
                            max(min(saved_export_limit, user_export_cap), 0.0),
                        )
                        if emergency_export_ready
                        else current_export
                    )

                recommended_charge = rcm_optimizer_module._quantize_power_percent(recommended_charge)
                recommended_export = rcm_optimizer_module._quantize_power_percent(recommended_export)

                pre_discharge_safe = bool(
                    not observed_emergency
                    and voltage_data_fresh
                    and control_voltage < rcm_optimizer_module.P_U_START_V
                    and pre_discharge_actuator_fresh
                    and actuator_data_fresh
                    and gcf_data_fresh
                    and bms_charge_available
                    and bms_discharge_available
                    and system_power_data_valid
                    and battery_capacity is not None
                    and battery_soc_fresh
                    and battery_soc is not None
                    and battery_soc > result.protected_minimum_soc_percent + 0.5
                    and (
                        result.pre_discharge_deadline is None
                        or local_now <= result.pre_discharge_deadline
                    )
                )
                pre_discharge_start = bool(
                    pre_discharge_safe
                    and live_power_data_fresh
                    and result.pre_discharge_start_eligible
                    and battery_soc > result.pre_discharge_target_soc_percent + 0.5
                )
                pre_discharge_continue = bool(
                    pre_discharge_safe
                    and result.pre_discharge_continue_eligible
                )
                pre_discharge_power = (
                    min(result.pre_discharge_power_kw, bms_discharge_limit_kw)
                    if pre_discharge_start or pre_discharge_continue
                    else 0.0
                )
                pre_discharge_power_percent = (
                    min(max(pre_discharge_power * 100.0 / system_power_kw, 0.0), 100.0)
                    if system_power_data_valid
                    else 0.0
                )
                pre_discharge_power_percent = rcm_optimizer_module._quantize_power_percent(pre_discharge_power_percent)
                pre_discharge_power = min(
                    pre_discharge_power,
                    system_power_kw * pre_discharge_power_percent / 100.0,
                )
                recommended_charge_power = (
                    min(
                        system_power_kw * recommended_charge / 100.0,
                        bms_charge_limit_kw,
                    )
                    if bms_charge_available
                    else 0.0
                )
                action_interval_start: datetime | None = None
                action_interval_end: datetime | None = None
                if (
                    action == "grid_discharge_preparation"
                    and result.pre_discharge_deadline is not None
                    and result.pre_discharge_deadline > local_now
                ):
                    action_interval_start = local_now
                    action_interval_end = result.pre_discharge_deadline
                elif action == "preserve_headroom":
                    preserved_end = result.action_interval_end
                    if preserved_end is not None and preserved_end > local_now:
                        action_interval_start = local_now
                        action_interval_end = preserved_end
                elif action in {"absorb_pv", "limit_export"}:
                    action_interval_start = local_now
                    action_interval_end = local_now + timedelta(
                        seconds=rcm_optimizer_module.LIVE_ACTION_VALIDITY_SECONDS
                    )
                result = replace(
                    result,
                    status_code=status_code,
                    action=action,
                    action_interval_start=action_interval_start,
                    action_interval_end=action_interval_end,
                    maximum_voltage_v=live_maximum_voltage,
                    control_voltage_v=control_voltage,
                    voltage_risk_score=round(risk_score * 100.0, 1),
                    risk_window_active=risk_window_active,
                    next_risk_start_minute=next_risk_start,
                    pv_surplus_power_kw=round(pv_surplus, 3),
                    bms_charge_power_limit_kw=round(bms_charge_limit_kw, 3),
                    recommended_charge_limit_percent=round(recommended_charge, 1),
                    recommended_charge_power_kw=round(recommended_charge_power, 3),
                    effective_export_cap_percent=round(effective_export_cap, 1),
                    recommended_export_limit_percent=round(recommended_export, 1),
                    saturated=bool(
                        bms_charge_available
                        and recommended_charge >= maximum_charge_limit - 0.05
                        and control_voltage >= rcm_optimizer_module.WARNING_V
                    ),
                    live_emergency=observed_emergency,
                    emergency_action_ready=bool(
                        observed_emergency
                        and (emergency_charge_ready or emergency_export_ready)
                    ),
                    prediction_ready=bool(
                        result.prediction_ready and gcf_data_fresh
                    ),
                    prediction_block_reason=(
                        result.prediction_block_reason
                        if gcf_data_fresh
                        else "gcf_state_stale"
                    ),
                    system_power_data_valid=system_power_data_valid,
                    voltage_data_fresh=voltage_data_fresh,
                    emergency_voltage_data_fresh=emergency_voltage_data_fresh,
                    actuator_data_fresh=actuator_data_fresh,
                    charge_actuator_data_fresh=charge_actuator_fresh,
                    export_actuator_data_fresh=export_actuator_fresh,
                    live_power_data_fresh=live_power_data_fresh,
                    bms_charge_available=bms_charge_available,
                    bms_charge_quantization_limited=(
                        bms_charge_quantization_limited
                    ),
                    bms_discharge_available=bms_discharge_available,
                    bms_discharge_dc_power_limit_kw=round(
                        bms_discharge_dc_limit_kw,
                        3,
                    ),
                    bms_discharge_power_limit_kw=round(
                        bms_discharge_limit_kw,
                        3,
                    ),
                    pre_discharge_ready=bool(
                        pre_discharge_start or pre_discharge_continue
                    ),
                    pre_discharge_start_eligible=pre_discharge_start,
                    pre_discharge_continue_eligible=pre_discharge_continue,
                    pre_discharge_transaction_ready=bool(
                        pre_discharge_start and pre_discharge_actuator_fresh
                    ),
                    pre_discharge_power_kw=round(pre_discharge_power, 3),
                    pre_discharge_power_percent=round(
                        pre_discharge_power_percent,
                        1,
                    ),
                )
            timeline_changed = False
            if result is not None and certified_cache_current:
                timeline_changed = self._refresh_live_observation_timeline(
                    now=local_now,
                    result=result,
                    current_soc_percent=(
                        battery_soc if battery_soc_fresh else None
                    ),
                    discharge_power_limit_kw=bms_discharge_limit_kw,
                    pv_power_kw=max(pv_power or 0.0, 0.0) / 1000.0,
                    load_power_kw=max(load_power or 0.0, 0.0) / 1000.0,
                )
            self._live_emergency_active = live_emergency
            self._live_control_ticks += 1
            transition = (
                "entered_emergency"
                if live_emergency and not previous_live_emergency
                else "left_emergency"
                if previous_live_emergency and not live_emergency
                else "steady"
            )
            common_updates: dict[str, Any] = {
                "voltage_l1_v": (
                    round(phase_values[0], 2)
                    if phase_values[0] is not None
                    else None
                ),
                "voltage_l2_v": (
                    round(phase_values[1], 2)
                    if phase_values[1] is not None
                    else None
                ),
                "voltage_l3_v": (
                    round(phase_values[2], 2)
                    if phase_values[2] is not None
                    else None
                ),
                "maximum_voltage_v": (
                    round(live_maximum_voltage, 2)
                    if emergency_voltage_data_fresh
                    else None
                ),
                "filtered_voltage_v": round(filtered_voltage, 2),
                "rolling_10m_voltage_v": round(rolling_voltage, 2),
                "voltage_data_fresh": voltage_data_fresh,
                "emergency_voltage_data_fresh": (
                    emergency_voltage_data_fresh
                ),
                "live_emergency": live_emergency,
                "live_control_transition": transition,
                "live_control_ticks": self._live_control_ticks,
                "live_control_updated_at": local_now.astimezone(timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "live_control_eligible": bool(
                    result is not None and result.emergency_action_ready
                ),
                "live_control_input_current": bool(
                    result is not None and not live_input_drifted
                ),
                "live_control_cache_age_seconds": (
                    round(max(cache_age_seconds, 0.0), 1)
                    if cache_age_seconds is not None
                    else None
                ),
                "live_control_cache_current": certified_cache_current,
                "actuator_data_fresh": actuator_data_fresh,
                "charge_actuator_data_fresh": charge_actuator_fresh,
                "export_actuator_data_fresh": export_actuator_fresh,
                "export_register_data_fresh": export_register_data_fresh,
                "bms_charge_data_fresh": bms_charge_fresh,
                "bms_discharge_data_fresh": bms_discharge_fresh,
                "live_power_data_fresh": live_power_data_fresh,
                "battery_soc_data_fresh": battery_soc_fresh,
                "battery_capacity_data_available": (
                    battery_capacity is not None
                ),
                "battery_capacity_source_entity_id": (
                    battery_capacity_source or None
                ),
                "discharge_registers_data_fresh": (
                    discharge_registers_data_fresh
                ),
                "ems_mode_data_fresh": ems_mode_fresh,
                "maximum_discharge_power_data_fresh": (
                    max_discharge_fresh
                ),
                "force_discharge_soc_data_fresh": force_discharge_fresh,
                "pre_discharge_actuator_data_fresh": (
                    pre_discharge_actuator_fresh
                ),
                "system_power_data_valid": system_power_data_valid,
                "inverter_count": inverter_count,
                "system_power_kw": (
                    round(system_power_kw, 3)
                    if system_power_data_valid
                    else None
                ),
                "export_control_enabled": export_control_enabled,
                "gcf_active": gcf_active,
                "gcf_data_fresh": gcf_data_fresh,
                "export_control_path_enabled": export_control_path_enabled,
                "data_freshness": freshness,
                "data_age_seconds": ages,
            }
            if not certified_cache_current:
                common_updates.update(
                    {
                        "result_current": False,
                        "recalculation_pending": True,
                        "execution_input_valid": False,
                        "execution_blocker_code": "certified_cache_expired",
                    }
                )
            if result is None:
                updates = {
                    **common_updates,
                    "status_code": (
                        "emergency_actuator_unavailable"
                        if observed_emergency
                        else "stale_voltage"
                        if not voltage_data_fresh
                        else "missing_data"
                    ),
                    "action": "monitor" if observed_emergency else "hold",
                    "action_interval_start": None,
                    "action_interval_end": None,
                    "emergency_action_ready": False,
                    "recommended_charge_limit_percent": (
                        current_charge_limit
                        if current_charge_limit is not None
                        else None
                    ),
                    "recommended_charge_power_kw": 0.0,
                    "recommended_export_limit_percent": (
                        current_export_limit
                        if current_export_limit is not None
                        else None
                    ),
                    "prediction_ready": False,
                    "pre_discharge_ready": False,
                    "pre_discharge_start_eligible": False,
                    "pre_discharge_continue_eligible": False,
                    "pre_discharge_transaction_ready": False,
                    "pre_discharge_deadline": None,
                    "pre_discharge_power_kw": 0.0,
                    "pre_discharge_power_percent": 0.0,
                    "live_control_status": "awaiting_full_plan",
                }
            else:
                updates = {
                    **common_updates,
                    "status_code": result.status_code,
                    "action": result.action,
                    "action_interval_start": (
                        result.action_interval_start.isoformat()
                        if result.action_interval_start is not None
                        else None
                    ),
                    "action_interval_end": (
                        result.action_interval_end.isoformat()
                        if result.action_interval_end is not None
                        else None
                    ),
                    "maximum_voltage_v": (
                        result.maximum_voltage_v
                        if emergency_voltage_data_fresh
                        else None
                    ),
                    "voltage_risk_score_percent": result.voltage_risk_score,
                    "risk_window_active": result.risk_window_active,
                    "next_risk_start": _minutes_text(
                        result.next_risk_start_minute
                    ),
                    "pv_surplus_power_kw": result.pv_surplus_power_kw,
                    "bms_charge_power_limit_kw": (
                        result.bms_charge_power_limit_kw
                    ),
                    "bms_discharge_power_limit_kw": (
                        result.bms_discharge_power_limit_kw
                    ),
                    "bms_discharge_dc_power_limit_kw": (
                        result.bms_discharge_dc_power_limit_kw
                    ),
                    "bms_charge_available": result.bms_charge_available,
                    "bms_charge_quantization_limited": (
                        result.bms_charge_quantization_limited
                    ),
                    "bms_discharge_available": result.bms_discharge_available,
                    "recommended_charge_limit_percent": (
                        result.recommended_charge_limit_percent
                    ),
                    "recommended_charge_power_kw": (
                        result.recommended_charge_power_kw
                    ),
                    "effective_export_cap_percent": (
                        result.effective_export_cap_percent
                    ),
                    "recommended_export_limit_percent": (
                        result.recommended_export_limit_percent
                    ),
                    "estimated_safe_export_power_kw": (
                        result.estimated_safe_export_power_kw
                    ),
                    "estimated_safe_export_available": (
                        result.estimated_safe_export_power_kw is not None
                    ),
                    "battery_limit_saturated": result.saturated,
                    "emergency_action_ready": (
                        result.emergency_action_ready
                    ),
                    "prediction_ready": result.prediction_ready,
                    "prediction_block_reason": (
                        result.prediction_block_reason
                    ),
                    "pre_discharge_ready": result.pre_discharge_ready,
                    "pre_discharge_start_eligible": (
                        result.pre_discharge_start_eligible
                    ),
                    "pre_discharge_continue_eligible": (
                        result.pre_discharge_continue_eligible
                    ),
                    "pre_discharge_transaction_ready": (
                        result.pre_discharge_transaction_ready
                    ),
                    "pre_discharge_deadline": (
                        result.pre_discharge_deadline.isoformat()
                        if result.pre_discharge_deadline is not None
                        else None
                    ),
                    "pre_discharge_target_soc_percent": (
                        result.pre_discharge_target_soc_percent
                    ),
                    "pre_discharge_power_kw": result.pre_discharge_power_kw,
                    "pre_discharge_power_percent": (
                        result.pre_discharge_power_percent
                    ),
                    "planned_grid_discharge_kwh": (
                        result.planned_grid_discharge_kwh
                    ),
                    "live_control_status": result.status_code,
                }
            candidate = {**self._attributes, **updates}
            if candidate != self._attributes:
                self._attributes = candidate
                self.async_write_ha_state()
            if timeline_changed:
                self._publish_timeline_result()
            return live_emergency != previous_live_emergency

    async def _recalculate_and_write(
        self,
        *,
        allow_deferred_retry: bool = True,
    ) -> None:
        """Write only when the voltage plan materially changed."""
        async with self._optimizer_lock:
            previous_state = self.native_value
            previous_attributes = self._attributes
            committed = False
            for _attempt in range(MAX_IMMEDIATE_RECALCULATIONS):
                if await self._recalculate_locked():
                    committed = True
                    break
            if committed:
                self._mark_result_current()
                self._cancel_stale_result_retry()
            self._attributes = {
                **self._attributes,
                "full_plan_solver_calls": self._full_plan_solver_calls,
                "last_full_plan_at": (
                    self._last_full_plan_at.astimezone(timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                    if self._last_full_plan_at is not None
                    else None
                ),
                "last_full_plan_trigger": self._full_plan_trigger,
            }
            if (
                previous_state != self.native_value
                or previous_attributes != self._attributes
            ):
                self.async_write_ha_state()
            if committed:
                self._publish_timeline_result()
            elif (
                allow_deferred_retry
                and self._full_plan_rejected_for_input_drift
            ):
                self._schedule_stale_result_retry()

    async def _recalculate(self) -> None:
        """Serialize startup and event-driven optimizer runs."""
        async with self._optimizer_lock:
            for _attempt in range(MAX_IMMEDIATE_RECALCULATIONS):
                if await self._recalculate_locked():
                    self._mark_result_current()
                    self._publish_timeline_result()
                    return

    def _append_voltage_sample(self, now: datetime | None = None) -> None:
        timestamp = now or dt_util.now()
        samples = [
            _number_sample(
                self.hass,
                entity_id,
                now=timestamp,
                max_age_seconds=LIVE_TELEMETRY_MAX_AGE_SECONDS,
            )
            for entity_id in GRID_VOLTAGE_ENTITIES
        ]
        if any(value is None or not fresh for value, fresh, _age in samples):
            return
        self._samples.append(
            (timestamp, max(value or 0.0 for value, _fresh, _age in samples))
        )
        cutoff = timestamp - timedelta(minutes=10)
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()

    async def _async_refresh_voltage_history(self) -> None:
        if self._history_refresh_running:
            return
        self._history_refresh_running = True
        try:
            timezone = ZoneInfo(self.hass.config.time_zone)
            now = dt_util.now().astimezone(timezone)
            start = now - timedelta(days=5)
            raw = await async_get_bounded_state_reports(
                self.hass,
                dt_util.as_utc(start),
                dt_util.as_utc(now),
                GRID_VOLTAGE_ENTITIES,
            )
            normalized: dict[str, list[tuple[datetime, float]]] = {
                entity_id: [] for entity_id in GRID_VOLTAGE_ENTITIES
            }
            for entity_id in GRID_VOLTAGE_ENTITIES:
                for item in raw.get(entity_id, []):
                    updated = getattr(item, "last_updated", None)
                    value = getattr(item, "state", None)
                    if updated is None or value is None:
                        continue
                    try:
                        numeric = float(value)
                    except (TypeError, ValueError):
                        continue
                    normalized[entity_id].append((updated.astimezone(timezone), numeric))
            self._history = summarize_voltage_history(normalized, now=now)
            self._history_refreshed_at = now
        except Exception:  # noqa: BLE001 - remain fail-closed
            _LOGGER.exception("Cannot rebuild four-day RCEm voltage history")
        finally:
            self._history_refresh_running = False

    def _expected_risk_surplus_kwh(
        self,
        system_power_kw: float,
        battery_charge_power_kw: float,
        charge_efficiency: float,
        house_discharge_efficiency: float,
        battery_capacity_kwh: float,
        battery_soc_percent: float,
        protected_minimum_soc_percent: float,
    ) -> RCMEnergyForecast:
        """Estimate each risk window from Solcast and weekday/weekend LOAD."""
        missing = RCMEnergyForecast(
            surplus_kwh=0.0,
            horizon="missing",
            selected_forecast_kwh=0.0,
            selected_forecast_p90_kwh=None,
            selected_forecast_p10_kwh=None,
            expected_load_kwh=0.0,
            protected_home_energy_kwh=0.0,
            stress_protected_home_energy_kwh=0.0,
            absorbable_surplus_kwh=0.0,
            natural_headroom_kwh=0.0,
            pre_risk_surplus_kwh=0.0,
            unavoidable_charge_input_kwh=0.0,
            minutes_to_risk=None,
            risk_day_offset=-1,
            timeline_target_date=None,
            forecast_entity_id="",
            forecast_profile_source="missing",
            forecast_profile_confidence=0.0,
            reserve_forecast_profile_source="missing",
            reserve_forecast_profile_confidence=0.0,
            load_profile_source="missing",
            load_profile_confidence=0.0,
            load_profile_data_fresh=False,
            headroom_load_profile_source="missing",
            reserve_load_profile_source="missing",
            window_forecasts=(),
            timeline_energy_points=(),
        )
        timezone = ZoneInfo(self.hass.config.time_zone)
        now = dt_util.now().astimezone(timezone)
        shared_snapshot = _shared_inputs_snapshot(self._runtime)
        shared_load = getattr(shared_snapshot, "load", None)
        if shared_load is not None:
            captured_at = getattr(shared_snapshot, "captured_at", None)
            rce_profile_age = (
                (now - captured_at.astimezone(now.tzinfo)).total_seconds()
                if isinstance(captured_at, datetime)
                and captured_at.tzinfo is not None
                and captured_at.utcoffset() is not None
                else None
            )
            raw_generated_at = getattr(shared_load, "generated_at", None)
            load_profile_generated_at = (
                raw_generated_at
                if isinstance(raw_generated_at, datetime)
                else dt_util.parse_datetime(raw_generated_at)
                if isinstance(raw_generated_at, str)
                else None
            )
            load_profile_data_fresh = bool(
                getattr(shared_load, "ready", False) is True
                and rce_profile_age is not None
                and -5.0 <= rce_profile_age <= RCE_PLAN_MAX_AGE_SECONDS
            )
        else:
            rce = self._same_entry_rce_plan_state()
            rce_profile_age = _state_age_seconds(rce, now)
            load_profile_data_fresh = bool(
                rce is not None
                and rce_profile_age is not None
                and -5.0 <= rce_profile_age <= RCE_PLAN_MAX_AGE_SECONDS
            )
            load_profile_generated_at = None
            if rce is not None:
                raw_generated_at = rce.attributes.get(
                    "load_profile_generated_at"
                )
                if isinstance(raw_generated_at, str):
                    load_profile_generated_at = dt_util.parse_datetime(
                        raw_generated_at
                    )
        load_profile_snapshot_age = (
            (now - load_profile_generated_at.astimezone(now.tzinfo)).total_seconds()
            if load_profile_generated_at is not None
            else None
        )
        shared_fallback_profile_ready = bool(
            shared_load is not None
            and _shared_fallback_profile_ready(
                shared_load,
                getattr(shared_load, "average_daily_home_load_kwh", None),
            )
        )
        load_profile_data_fresh = bool(
            load_profile_data_fresh
            and (
                (
                    load_profile_snapshot_age is not None
                    and -5.0 <= load_profile_snapshot_age <= 30 * 60 * 60
                )
                or shared_fallback_profile_ready
                or (
                    shared_load is not None
                    and qualified_load_history_is_usable(
                        load_profile_generated_at,
                        getattr(shared_load, "daily_total_dates", ()),
                        now=now,
                    )
                )
            )
        )
        # Never consume RCE forecast totals/status/floor. The shared broker
        # carries only the exact-entry Recorder LOAD model; old runtimes fall
        # back solely to the RCE object explicitly wired by sensor.py.
        if shared_load is not None and load_profile_data_fresh:
            rce_attributes = {
                "selected_average_daily_load_kwh": getattr(
                    shared_load,
                    "average_daily_home_load_kwh",
                    None,
                ),
                "recorder_load_daily_kwh": tuple(
                    getattr(shared_load, "daily_totals_kwh", ()) or ()
                ),
                "recorder_load_profile_30m_kwh": tuple(
                    getattr(shared_load, "average_profile_30m_kwh", ()) or ()
                ),
                "recorder_load_weekday_profile_30m_kwh": tuple(
                    getattr(shared_load, "weekday_profile_30m_kwh", ()) or ()
                ),
                "recorder_load_weekend_profile_30m_kwh": tuple(
                    getattr(shared_load, "weekend_profile_30m_kwh", ()) or ()
                ),
            }
        elif shared_load is None and load_profile_data_fresh:
            rce_attributes = rce.attributes
        else:
            rce_attributes = {}
        minute = now.hour * 60 + now.minute
        current_slot = now.hour * 2 + now.minute // 30
        pending_today = sorted(
            (start, end, peak)
            for start, end, peak in self._history.risk_windows
            if end > minute
        )
        if pending_today:
            horizon = "today"
            first_slot = current_slot
            risk_day_offset = 0
            first_risk_start = pending_today[0][0]
            minutes_to_risk = max(first_risk_start - minute, 0)
            selected_windows = pending_today
        elif self._history.risk_windows:
            horizon = "tomorrow"
            first_slot = 0
            risk_day_offset = 1
            first_risk_start = min(
                start for start, _end, _peak in self._history.risk_windows
            )
            minutes_to_risk = 24 * 60 - minute + first_risk_start
            selected_windows = list(self._history.risk_windows)
        else:
            horizon = "none"
            first_slot = 0
            risk_day_offset = -1
            first_risk_start = 0
            minutes_to_risk = None
            selected_windows = []
        # With no learned risk window the observation-only timeline follows
        # today while a complete native 15-minute slot remains.  During the
        # final partial slot it rolls to tomorrow instead of publishing an
        # empty source-coverage failure that blocks the canonical EMS plan.
        target_date = _timeline_forecast_target_date(
            now=now,
            risk_day_offset=risk_day_offset,
        )
        current_day_forecast = target_date == now.date()
        configured_entity = _resolved_forecast_entity_id(
            self.hass,
            self._runtime,
            broker_field="today" if current_day_forecast else "tomorrow",
            new_helper=(
                EMS_TODAY_FORECAST_ENTITY_HELPER
                if current_day_forecast
                else EMS_TOMORROW_FORECAST_ENTITY_HELPER
            ),
            legacy_helper=(
                TODAY_FORECAST_ENTITY_HELPER
                if current_day_forecast
                else TOMORROW_FORECAST_ENTITY_HELPER
            ),
        )
        forecast_entity, forecast_state = _first_numeric_state(
            self.hass,
            TODAY_FORECAST_CANDIDATES
            if current_day_forecast
            else TOMORROW_FORECAST_CANDIDATES,
            configured_entity,
        )
        direct_p50_total = _forecast_total(forecast_state, "p50")
        if direct_p50_total is None:
            return missing
        if current_day_forecast:
            expected_elapsed = _detailed_pv_expected_elapsed_kwh(
                forecast_state,
                target_date,
                timezone,
                now,
            )
            if expected_elapsed is not None:
                forecast = max(direct_p50_total - expected_elapsed, 0.0)
            else:
                remaining_sample = _shared_input_field(
                    self._runtime,
                    "forecast",
                    "remaining_today",
                )
                remaining_configured = (
                    getattr(remaining_sample, "entity_id", None)
                    if remaining_sample is not _SHARED_INPUT_MISSING
                    else None
                )
                remaining_entity, remaining_state = _first_numeric_state(
                    self.hass,
                    REMAINING_TODAY_CANDIDATES,
                    remaining_configured,
                )
                remaining_age = _state_age_seconds(remaining_state, now)
                remaining_total = _forecast_total(remaining_state, "p50")
                if (
                    remaining_total is None
                    or remaining_age is None
                    or remaining_age < -5.0
                    or remaining_age > FORECAST_MAX_AGE_SECONDS
                ):
                    return missing
                forecast = remaining_total
                forecast_entity = remaining_entity
        else:
            forecast = direct_p50_total
        weekend = target_date.weekday() >= 5
        try:
            average_daily = max(
                float(rce_attributes.get("selected_average_daily_load_kwh", 0.0)),
                0.0,
            )
        except (TypeError, ValueError):
            average_daily = 0.0
        daily_loads_raw = rce_attributes.get("recorder_load_daily_kwh")
        if isinstance(daily_loads_raw, dict):
            daily_loads = tuple(daily_loads_raw.values())
        elif isinstance(daily_loads_raw, (list, tuple)):
            daily_loads = tuple(daily_loads_raw)
        else:
            daily_loads = ()
        load_envelopes = shared_rcm_load_envelopes(
            now=now, target_date=target_date,
            average_profile_30m_kwh=rce_attributes.get("recorder_load_profile_30m_kwh", ()),
            weekday_profile_30m_kwh=rce_attributes.get(
                "recorder_load_weekday_profile_30m_kwh"
            ) or (),
            weekend_profile_30m_kwh=rce_attributes.get(
                "recorder_load_weekend_profile_30m_kwh"
            ) or (),
            daily_energy_kwh=average_daily,
            daily_totals_kwh=daily_loads,
            night_energy_kwh=getattr(shared_load, "average_night_home_load_kwh", None),
            night_start_minute=getattr(shared_load, "night_start_minute", 22 * 60),
            night_end_minute=getattr(shared_load, "night_end_minute", 6 * 60),
            current_day_energy_kwh=getattr(shared_load, "current_day_energy_kwh", None),
            current_day_observed_at=getattr(shared_load, "current_day_observed_at", None),
            persistence_delta_kw=getattr(shared_load, "persistence_delta_kw", 0.),
            persistence_observed_at=getattr(shared_load, "persistence_observed_at", None),
        )
        load_selection = load_envelopes.nominal
        headroom_load_selection = load_envelopes.low
        reserve_load_selection = load_envelopes.high

        p90_raw = _forecast_total(forecast_state, "p90")
        p10_raw = _forecast_total(forecast_state, "p10")
        selected_p90 = (
            forecast
            * min(
                max(max(p90_raw, direct_p50_total) / direct_p50_total, 1.0),
                2.5,
            )
            if p90_raw is not None and direct_p50_total > 0.0
            else None
        )
        selected_p10 = (
            forecast
            * min(max(p10_raw / direct_p50_total, 0.0), 1.0)
            if p10_raw is not None and direct_p50_total > 0.0
            else None
        )

        now_slot = (
            floor_half_hour(now)
            if current_day_forecast
            else datetime.combine(target_date, datetime.min.time(), tzinfo=timezone)
        )
        p50_map = _detailed_pv_map(
            forecast_state,
            target_date,
            forecast,
            timezone,
            now_slot,
            percentile="p50",
        )
        p90_map = (
            _detailed_pv_map(
                forecast_state,
                target_date,
                selected_p90,
                timezone,
                now_slot,
                percentile="p90",
            )
            if selected_p90 is not None
            else {}
        )
        p10_map = (
            _detailed_pv_map(
                forecast_state,
                target_date,
                selected_p10,
                timezone,
                now_slot,
                percentile="p10",
            )
            if selected_p10 is not None
            else {}
        )
        p50_by_slot = {
            start.hour * 2 + start.minute // 30: energy
            for start, energy in p50_map.items()
        }
        p90_by_slot = {
            start.hour * 2 + start.minute // 30: energy
            for start, energy in p90_map.items()
        }
        p10_by_slot = {
            start.hour * 2 + start.minute // 30: energy
            for start, energy in p10_map.items()
        }
        risk_slots = tuple(
            sorted(
                {
                    slot
                    for start, end, _peak in selected_windows
                    for slot in range(start // 30, min((end + 29) // 30, 48))
                }
            )
        )
        pv_selection = select_rcm_pv_profile(
            forecast_total_kwh=forecast,
            forecast_p90_total_kwh=selected_p90,
            detailed_p50_by_slot=p50_by_slot,
            detailed_p90_by_slot=p90_by_slot,
            first_slot=first_slot,
            current_slot_fraction=(
                (30 - now.minute % 30) / 30.0
                if current_day_forecast
                else 1.0
            ),
            risk_slots=risk_slots,
        )
        nominal_pv_selection = select_rcm_pv_profile(
            forecast_total_kwh=forecast,
            forecast_p90_total_kwh=selected_p90,
            forecast_p10_total_kwh=selected_p10,
            detailed_p50_by_slot=p50_by_slot,
            detailed_p90_by_slot=p90_by_slot,
            detailed_p10_by_slot=p10_by_slot,
            first_slot=first_slot,
            current_slot_fraction=(
                (30 - now.minute % 30) / 30.0
                if current_day_forecast
                else 1.0
            ),
            risk_slots=risk_slots,
            scenario="nominal",
        )
        reserve_pv_selection = select_rcm_pv_profile(
            forecast_total_kwh=forecast,
            forecast_p90_total_kwh=selected_p90,
            forecast_p10_total_kwh=selected_p10,
            detailed_p50_by_slot=p50_by_slot,
            detailed_p90_by_slot=p90_by_slot,
            detailed_p10_by_slot=p10_by_slot,
            first_slot=first_slot,
            current_slot_fraction=(
                (30 - now.minute % 30) / 30.0
                if current_day_forecast
                else 1.0
            ),
            risk_slots=risk_slots,
            scenario="low",
        )
        horizon_start_minute = minute if current_day_forecast else 0
        minimum_charge_floor_kw = max(system_power_kw, 0.0) * 0.10
        capacity = max(float(battery_capacity_kwh), 0.0)
        current_soc = min(max(float(battery_soc_percent), 0.0), 100.0)
        protected_soc = min(
            max(float(protected_minimum_soc_percent), 0.0),
            100.0,
        )
        initial_headroom_kwh = capacity * (100.0 - current_soc) / 100.0
        maximum_headroom_kwh = max(
            initial_headroom_kwh,
            capacity * (100.0 - protected_soc) / 100.0,
        )

        def energy_balance(
            start_minute: int,
            end_minute: int,
            *,
            pv_profile: tuple[float, ...],
            load_selection,
            cap_absorption: bool,
        ) -> tuple[float, float, float, float, float, float, float]:
            pv_energy = 0.0
            load_energy = 0.0
            surplus_energy = 0.0
            absorbable_surplus = 0.0
            minimum_charge_input = 0.0
            chronological_pv: list[float] = []
            chronological_load: list[float] = []
            charge_input_limits: list[float] = []
            for stamp, slot_load_full in load_selection.by_start_kwh.items():
                local_stamp = stamp.astimezone(now.tzinfo)
                slot = local_stamp.hour * 2 + local_stamp.minute // 30
                slot_start = slot * 30
                slot_end = slot_start + 30
                elapsed = max((now.astimezone(dt_util.UTC) - stamp).total_seconds()/60., 0.)
                available_start = slot_start + min(elapsed, 30.) if current_day_forecast else slot_start
                overlap_start = max(start_minute, available_start)
                overlap_end = min(end_minute, slot_end)
                overlap_minutes = max(overlap_end - overlap_start, 0)
                if overlap_minutes <= 0:
                    continue
                available_minutes = max(slot_end - available_start, 1)
                slot_pv = (
                    pv_profile[slot]
                    * overlap_minutes
                    / available_minutes
                )
                slot_load = (
                    slot_load_full
                    * overlap_minutes
                    / 30.0
                )
                pv_energy += slot_pv
                load_energy += slot_load
                slot_surplus = max(slot_pv - slot_load, 0.0)
                surplus_energy += slot_surplus
                overlap_hours = overlap_minutes / 60.0
                load_power_kw = slot_load / max(overlap_hours, 1 / 60.0)
                shared_charge_power_kw = max(
                    system_power_kw - load_power_kw,
                    0.0,
                )
                physical_charge_power_kw = min(
                    max(battery_charge_power_kw, 0.0),
                    shared_charge_power_kw,
                )
                chronological_pv.append(slot_pv)
                chronological_load.append(slot_load)
                charge_input_limits.append(
                    physical_charge_power_kw * overlap_hours
                )
                if cap_absorption:
                    absorbable_surplus += min(
                        slot_surplus,
                        physical_charge_power_kw * overlap_hours,
                    )
                    minimum_charge_input += min(
                        slot_surplus,
                        minimum_charge_floor_kw * overlap_hours,
                        physical_charge_power_kw * overlap_hours,
                    )
                else:
                    absorbable_surplus += slot_surplus
            return (
                pv_energy,
                load_energy,
                surplus_energy,
                stateful_natural_headroom_kwh(
                    chronological_pv,
                    chronological_load,
                    initial_headroom_kwh=initial_headroom_kwh,
                    maximum_headroom_kwh=maximum_headroom_kwh,
                    charge_efficiency=charge_efficiency,
                    house_discharge_efficiency=house_discharge_efficiency,
                    charge_input_limits_kwh=charge_input_limits,
                ),
                absorbable_surplus,
                minimum_charge_input,
                stateful_pre_risk_home_buffer_kwh(
                    chronological_pv,
                    chronological_load,
                    charge_efficiency=charge_efficiency,
                    house_discharge_efficiency=house_discharge_efficiency,
                    charge_input_limits_kwh=charge_input_limits,
                ),
            )

        window_forecasts: list[RCMRiskWindowInput] = []
        for start, end, peak in selected_windows:
            window_start = max(start, horizon_start_minute)
            (
                pv_energy,
                load_energy,
                surplus_energy,
                _deficit,
                absorbable_surplus,
                _minimum_charge,
                _headroom_buffer,
            ) = energy_balance(
                window_start,
                end,
                pv_profile=pv_selection.slot_kwh,
                load_selection=headroom_load_selection,
                cap_absorption=True,
            )
            (
                _pre_pv,
                _pre_load,
                _pre_surplus,
                natural_before,
                _pre_absorbable,
                _pre_minimum_charge,
                _natural_buffer,
            ) = energy_balance(
                horizon_start_minute,
                max(start, horizon_start_minute),
                pv_profile=pv_selection.slot_kwh,
                load_selection=headroom_load_selection,
                cap_absorption=True,
            )
            nominal_before = energy_balance(
                horizon_start_minute,
                max(start, horizon_start_minute),
                pv_profile=nominal_pv_selection.slot_kwh,
                load_selection=load_selection,
                cap_absorption=False,
            )
            stress_before = energy_balance(
                horizon_start_minute,
                max(start, horizon_start_minute),
                pv_profile=reserve_pv_selection.slot_kwh,
                load_selection=reserve_load_selection,
                cap_absorption=False,
            )
            window_forecasts.append(
                RCMRiskWindowInput(
                    start_minute=start,
                    end_minute=end,
                    peak_voltage_v=peak,
                    day_offset=risk_day_offset,
                    expected_pv_kwh=pv_energy,
                    expected_load_kwh=load_energy,
                    expected_surplus_kwh=surplus_energy,
                    natural_headroom_before_kwh=natural_before,
                    absorbable_surplus_kwh=absorbable_surplus,
                    protected_home_energy_kwh=nominal_before[6],
                    stress_protected_home_energy_kwh=stress_before[6],
                    absorption_power_limited=(
                        absorbable_surplus < surplus_energy - 0.001
                    ),
                )
            )
        first_balance = energy_balance(
            horizon_start_minute,
            max(first_risk_start, horizon_start_minute),
            pv_profile=pv_selection.slot_kwh,
            load_selection=headroom_load_selection,
            cap_absorption=True,
        )
        first_nominal_balance = energy_balance(
            horizon_start_minute,
            max(first_risk_start, horizon_start_minute),
            pv_profile=nominal_pv_selection.slot_kwh,
            load_selection=load_selection,
            cap_absorption=False,
        )
        first_stress_balance = energy_balance(
            horizon_start_minute,
            max(first_risk_start, horizon_start_minute),
            pv_profile=reserve_pv_selection.slot_kwh,
            load_selection=reserve_load_selection,
            cap_absorption=False,
        )
        return RCMEnergyForecast(
            surplus_kwh=sum(item.expected_surplus_kwh for item in window_forecasts),
            horizon=horizon,
            selected_forecast_kwh=forecast,
            selected_forecast_p90_kwh=selected_p90,
            selected_forecast_p10_kwh=selected_p10,
            expected_load_kwh=sum(item.expected_load_kwh for item in window_forecasts),
            protected_home_energy_kwh=first_nominal_balance[6],
            stress_protected_home_energy_kwh=first_stress_balance[6],
            absorbable_surplus_kwh=sum(
                item.absorbable_surplus_kwh or 0.0
                for item in window_forecasts
            ),
            natural_headroom_kwh=(
                window_forecasts[0].natural_headroom_before_kwh
                if window_forecasts
                else 0.0
            ),
            pre_risk_surplus_kwh=first_balance[2],
            unavoidable_charge_input_kwh=first_balance[5],
            minutes_to_risk=minutes_to_risk,
            risk_day_offset=risk_day_offset,
            timeline_target_date=target_date,
            forecast_entity_id=forecast_entity,
            forecast_profile_source=pv_selection.source,
            forecast_profile_confidence=pv_selection.confidence,
            reserve_forecast_profile_source=reserve_pv_selection.source,
            reserve_forecast_profile_confidence=reserve_pv_selection.confidence,
            load_profile_source=load_selection.source,
            load_profile_confidence=load_selection.confidence,
            load_profile_data_fresh=load_profile_data_fresh,
            headroom_load_profile_source=headroom_load_selection.source,
            reserve_load_profile_source=reserve_load_selection.source,
            window_forecasts=tuple(window_forecasts),
            timeline_energy_points=_timeline_energy_points(
                now=now,
                target_date=target_date,
                risk_day_offset=risk_day_offset,
                pv_profile=pv_selection.slot_kwh,
                load_profile=headroom_load_selection.slot_kwh,
                load_by_start_kwh=dict(headroom_load_selection.by_start_kwh),
            ),
        )

    async def _recalculate_locked(self) -> bool:
        self._full_plan_rejected_for_input_drift = False
        captured_revision = self._input_revision.value
        captured_fingerprint = self._current_input_fingerprint()
        try:
            now = dt_util.now().astimezone(ZoneInfo(self.hass.config.time_zone))
            (
                self_use_soc_value,
                self_use_soc_fresh,
                self_use_soc_age,
            ) = _number_sample(
                self.hass,
                "sensor.hoymiles_hit_ems_self_use_soc_readback",
                now=now,
                max_age_seconds=ACTUATOR_MAX_AGE_SECONDS,
                minimum=10.0,
                maximum=100.0,
                runtime=self._runtime,
                shared_section="system",
                shared_field="self_use_reserve_soc_percent",
            )
            (
                battery_soc_value,
                battery_soc_fresh,
                battery_soc_age,
            ) = _number_sample(
                self.hass,
                "sensor.hoymiles_hit_overview_battery_soc",
                now=now,
                max_age_seconds=SLOW_TELEMETRY_MAX_AGE_SECONDS,
                minimum=0.0,
                maximum=100.0,
                runtime=self._runtime,
                shared_section="system",
                shared_field="battery_soc_percent",
            )
            machine_count_required, _, _ = _number_sample(
                self.hass,
                "sensor.hoymiles_hit_number_of_machines_master_and_slave",
                now=now,
                max_age_seconds=SLOW_TELEMETRY_MAX_AGE_SECONDS,
                runtime=self._runtime,
                shared_section="system",
                shared_field="inverter_count",
            )
            required = {
                entity_id: _state_number(self.hass, entity_id)
                for entity_id in (
                    *GRID_VOLTAGE_ENTITIES,
                    "sensor.hoymiles_hit_battery_max_charge_power_readback",
                    "input_number.hoymiles_rcm_soc_safety_margin",
                    "input_number.hoymiles_rcm_charge_efficiency",
                    "input_number.hoymiles_rcm_export_cap_percent",
                )
            }
            # Instantaneous PV and LOAD are live-control evidence, not inputs
            # required to rebuild the 48-hour risk trajectory. They are
            # sampled below into ``live_power_data_fresh`` so a stale report
            # still withholds write authority without discarding the plan.
            required[
                "sensor.hoymiles_hit_number_of_machines_master_and_slave"
            ] = machine_count_required
            battery_capacity, battery_capacity_source = (
                _stable_battery_capacity(self.hass, self._runtime)
            )
            required["sensor.hoymiles_hit_battery_capacity"] = battery_capacity
            required["sensor.hoymiles_hit_ems_self_use_soc_readback"] = (
                self_use_soc_value if self_use_soc_fresh else None
            )
            required["sensor.hoymiles_hit_overview_battery_soc"] = (
                battery_soc_value if battery_soc_fresh else None
            )
            export_control_enabled = self.hass.states.is_state(
                "input_boolean.hoymiles_rcm_export_control_enabled",
                "on",
            )
            gcf_sample = _policy_numeric_sample(
                self._runtime,
                "gcf",
                "enable_code",
                self.hass.states.get(
                    "sensor.hoymiles_hit_gcf_enable_readback_code"
                ),
                now,
                max_age_seconds=ACTUATOR_MAX_AGE_SECONDS,
                minimum=0.0,
                maximum=1.0,
            )
            gcf_value = gcf_sample.value
            gcf_age = gcf_sample.age_seconds
            shared_gcf = getattr(
                _shared_inputs_snapshot(self._runtime),
                "gcf",
                None,
            )
            gcf_state_fresh = bool(
                gcf_sample.fresh and gcf_value in {0.0, 1.0}
                and (
                    shared_gcf is None
                    or getattr(shared_gcf, "gcf_readback_ready", False) is True
                )
            )
            gcf_active = bool(gcf_state_fresh and gcf_value == 1.0)
            current_export_sample = _policy_numeric_sample(
                self._runtime,
                "gcf",
                "maximum_export_power_percent",
                self.hass.states.get(
                    "sensor.hoymiles_hit_gcf_maximum_export_power_readback"
                ),
                now,
                max_age_seconds=ACTUATOR_MAX_AGE_SECONDS,
            )
            current_export_limit = current_export_sample.value
            if gcf_active and current_export_limit is None:
                required[
                    "sensor.hoymiles_hit_gcf_maximum_export_power_readback"
                ] = None
            shared_rated_power = _shared_sample_value(
                self._runtime,
                "system",
                "inverter_rated_power_each_kw",
            )
            rated_power = (
                _preferred_select_number(
                    self.hass,
                    EMS_INVERTER_RATED_POWER_HELPER,
                    LEGACY_INVERTER_RATED_POWER_HELPER,
                )
                if shared_rated_power is _SHARED_INPUT_MISSING
                else float(shared_rated_power)
                if type(shared_rated_power) in {int, float}
                and isfinite(float(shared_rated_power))
                else None
            )
            if rated_power is None:
                required[EMS_INVERTER_RATED_POWER_HELPER] = None
            house_efficiency_sample = _shared_input_field(
                self._runtime,
                "efficiency",
                "battery_to_home_efficiency",
            )
            house_efficiency_raw = (
                getattr(house_efficiency_sample, "value", None)
                if house_efficiency_sample is not _SHARED_INPUT_MISSING
                else None
            )
            house_efficiency_fresh = bool(
                house_efficiency_sample is not _SHARED_INPUT_MISSING
                and getattr(house_efficiency_sample, "fresh", False) is True
                and type(house_efficiency_raw) in {int, float}
                and isfinite(float(house_efficiency_raw))
                and 0.0 < float(house_efficiency_raw) <= 100.0
            )
            house_efficiency_age = (
                getattr(house_efficiency_sample, "age_seconds", None)
                if house_efficiency_sample is not _SHARED_INPUT_MISSING
                else None
            )
            required[NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER] = (
                float(house_efficiency_raw)
                if house_efficiency_fresh
                else None
            )
            missing = sorted(key for key, value in required.items() if value is None)

            freshness: dict[str, bool] = {}
            ages: dict[str, float | None] = {}
            freshness[
                "sensor.hoymiles_hit_ems_self_use_soc_readback"
            ] = self_use_soc_fresh
            ages["sensor.hoymiles_hit_ems_self_use_soc_readback"] = (
                round(self_use_soc_age, 1)
                if self_use_soc_age is not None
                else None
            )
            freshness["sensor.hoymiles_hit_overview_battery_soc"] = (
                battery_soc_fresh
            )
            ages["sensor.hoymiles_hit_overview_battery_soc"] = (
                round(battery_soc_age, 1)
                if battery_soc_age is not None
                else None
            )
            freshness[NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER] = (
                house_efficiency_fresh
            )
            ages[NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER] = (
                round(float(house_efficiency_age), 1)
                if type(house_efficiency_age) in {int, float}
                and isfinite(float(house_efficiency_age))
                else None
            )

            def sample(
                entity_id: str,
                max_age_seconds: float,
                *,
                minimum: float | None = None,
                maximum: float | None = None,
                shared_section: str | None = None,
                shared_field: str | None = None,
                shared_scale: float = 1.0,
                legacy_scale: float = 1.0,
            ) -> tuple[float | None, bool]:
                value, fresh, age = _number_sample(
                    self.hass,
                    entity_id,
                    now=now,
                    max_age_seconds=max_age_seconds,
                    minimum=minimum,
                    maximum=maximum,
                    runtime=self._runtime if shared_section is not None else None,
                    shared_section=shared_section,
                    shared_field=shared_field,
                    shared_scale=shared_scale,
                    legacy_scale=legacy_scale,
                )
                freshness[entity_id] = fresh
                ages[entity_id] = round(age, 1) if age is not None else None
                return value, fresh

            for capacity_entity_id in BATTERY_CAPACITY_ENTITIES:
                capacity_state = self.hass.states.get(capacity_entity_id)
                capacity_age = _state_age_seconds(capacity_state, now)
                capacity_value = _state_number(self.hass, capacity_entity_id)
                capacity_report_fresh = bool(
                    capacity_value is not None
                    and isfinite(capacity_value)
                    and capacity_value > 0.0
                    and capacity_age is not None
                    and -5.0 <= capacity_age <= SLOW_TELEMETRY_MAX_AGE_SECONDS
                )
                freshness[capacity_entity_id] = capacity_report_fresh
                ages[capacity_entity_id] = (
                    round(capacity_age, 1)
                    if capacity_age is not None
                    else None
                )
            battery_capacity_report_fresh = bool(
                battery_capacity_source
                and freshness.get(battery_capacity_source, False)
            )

            voltage_samples = {
                entity_id: sample(entity_id, LIVE_TELEMETRY_MAX_AGE_SECONDS)
                for entity_id in GRID_VOLTAGE_ENTITIES
            }
            voltage_data_fresh = all(
                fresh
                and value is not None
                and isfinite(value)
                and 180.0 <= value <= 280.0
                for value, fresh in voltage_samples.values()
            )
            emergency_voltage_data_fresh = any(
                fresh
                and value is not None
                and isfinite(value)
                and value > 0.0
                for value, fresh in voltage_samples.values()
            )
            live_voltage_values = [
                value
                if (
                    fresh
                    and value is not None
                    and isfinite(value)
                    and value > 0.0
                )
                else 0.0
                for value, fresh in voltage_samples.values()
            ]
            live_emergency = bool(
                emergency_voltage_data_fresh
                and max(live_voltage_values, default=0.0) >= 253.0
            )
            machine_count_value, machine_count_fresh = sample(
                "sensor.hoymiles_hit_number_of_machines_master_and_slave",
                SLOW_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="system",
                shared_field="inverter_count",
            )
            system_power_data_valid = bool(
                rated_power is not None
                and rated_power > 0.0
                and machine_count_fresh
                and machine_count_value is not None
                and 1.0 <= machine_count_value <= 10.0
            )

            current_charge_limit, charge_actuator_fresh = sample(
                "sensor.hoymiles_hit_battery_max_charge_power_readback",
                ACTUATOR_MAX_AGE_SECONDS,
                minimum=10.0,
                maximum=100.0,
            )
            # Always sample the raw register. New export writes additionally
            # require active GCF + user permission, but rollback must retain a
            # readback path after either of those is switched off.
            current_export_limit, export_register_data_fresh = sample(
                "sensor.hoymiles_hit_gcf_maximum_export_power_readback",
                ACTUATOR_MAX_AGE_SECONDS,
                shared_section="gcf",
                shared_field="maximum_export_power_percent",
            )
            export_control_path_enabled = bool(
                export_control_enabled and gcf_active and gcf_state_fresh
            )
            export_actuator_fresh = bool(
                export_control_path_enabled and export_register_data_fresh
            )
            gcf_data_fresh = bool(
                gcf_state_fresh
                and (not gcf_active or export_register_data_fresh)
            )
            freshness["sensor.hoymiles_hit_gcf_enable_readback_code"] = (
                gcf_state_fresh
            )
            ages["sensor.hoymiles_hit_gcf_enable_readback_code"] = (
                round(gcf_age, 1) if gcf_age is not None else None
            )
            actuator_data_fresh = bool(
                charge_actuator_fresh
                and (
                    export_actuator_fresh
                    if export_control_path_enabled
                    else True
                )
            )

            # At 253 V the feedback safety path must not wait for Solcast,
            # LOAD, SOC or the four-day history. It still publishes an
            # explicit actuator-unavailable emergency when the write path is
            # stale, instead of silently degrading to missing_data/learning.
            if missing and not live_emergency:
                if self._retain_last_complete_plan(
                    blocker_code="missing_data",
                    missing_entities=missing,
                    voltage_data_fresh=voltage_data_fresh,
                    emergency_voltage_data_fresh=emergency_voltage_data_fresh,
                    actuator_data_fresh=actuator_data_fresh,
                    freshness=freshness,
                    ages=ages,
                ):
                    return False
                self._timeline_trace = None
                self._timeline_blocker = "missing_data"
                self._attributes = {
                    "status_code": "missing_data",
                    "missing_entities": missing,
                    "risk_windows": [_window_text(item) for item in self._history.risk_windows],
                    "history_days": self._history.history_days,
                    "live_emergency": False,
                    "voltage_data_fresh": voltage_data_fresh,
                    "emergency_voltage_data_fresh": (
                        emergency_voltage_data_fresh
                    ),
                    "actuator_data_fresh": actuator_data_fresh,
                    "data_freshness": freshness,
                    "data_age_seconds": ages,
                }
                if self._timeline_sensor is not None:
                    self._timeline_sensor.publish_unavailable(
                        input_revision=captured_revision,
                        blocker_code=self._timeline_blocker,
                    )
                return True

            # The 15-second/event-driven live path owns this rolling cohort.
            # A full-plan run must consume, but never mutate, its certified
            # snapshot or it would reject its own executor result as stale.
            recent = [value for timestamp, value in self._samples if timestamp >= now - timedelta(seconds=60)]
            rolling = [value for _timestamp, value in self._samples]
            live_max = max(live_voltage_values, default=0.0)
            filtered = median(recent) if recent else live_max
            rolling_10m = sum(rolling) / len(rolling) if rolling else live_max
            current_slot = now.hour * 4 + now.minute // 15
            historical_p90 = self._history.profile_p90_v[current_slot]
            inverter_count = (
                min(max(round(machine_count_value or 0.0), 1), 10)
                if system_power_data_valid
                else 0
            )
            saved_limit = _state_number(
                self.hass,
                "input_number.hoymiles_rcm_saved_battery_charge_power",
            )
            if saved_limit is None or saved_limit < 10.0:
                saved_limit = (
                    current_charge_limit
                    if current_charge_limit is not None
                    else 10.0
                )
            # The pure optimizer requires a positive numerical scale, while
            # `system_power_data_valid` independently blocks every charge or
            # predictive-discharge path when topology/rated power is unknown.
            # An export-only live emergency may still clamp its percentage.
            system_power_kw = (
                (rated_power or 0.0) * inverter_count
                if system_power_data_valid
                else 1.0
            )
            battery_voltage, battery_voltage_fresh = sample(
                "sensor.hoymiles_hit_battery_voltage_bms",
                SLOW_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="bms",
                shared_field="voltage_v",
            )
            bms_charge_current, bms_charge_current_fresh = sample(
                "sensor.hoymiles_hit_maximum_charge_current",
                SLOW_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="bms",
                shared_field="maximum_charge_current_a",
            )
            bms_discharge_current, bms_discharge_current_fresh = sample(
                "sensor.hoymiles_hit_maximum_discharge_current",
                SLOW_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="bms",
                shared_field="maximum_discharge_current_a",
            )
            bms_charge_fresh = battery_voltage_fresh and bms_charge_current_fresh
            bms_discharge_fresh = (
                battery_voltage_fresh and bms_discharge_current_fresh
            )
            physical_charge_power_kw = (
                min(
                    system_power_kw,
                    (battery_voltage or 0.0)
                    * (bms_charge_current or 0.0)
                    / 1000.0,
                )
                if bms_charge_fresh
                and (battery_voltage or 0.0) > 0.0
                and (bms_charge_current or 0.0) > 0.0
                else 0.0
            )
            charge_efficiency = min(
                max(
                    required["input_number.hoymiles_rcm_charge_efficiency"]
                    or 95.0,
                    1.0,
                ),
                100.0,
            ) / 100.0
            house_discharge_efficiency = (
                min(
                    max(
                        required[NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER],
                        1.0,
                    ),
                    100.0,
                )
                / 100.0
                if house_efficiency_fresh
                else 0.01
            )
            protected_minimum_soc = (
                (
                    required[
                        "sensor.hoymiles_hit_ems_self_use_soc_readback"
                    ]
                    or 0.0
                )
                + (required["input_number.hoymiles_rcm_soc_safety_margin"] or 0.0)
            )
            energy_forecast = self._expected_risk_surplus_kwh(
                system_power_kw,
                # Forecast surplus is AC-side input energy. Convert the BMS
                # DC storage limit to the matching AC-side ceiling; the pure
                # optimizer applies charging efficiency exactly once later.
                physical_charge_power_kw / charge_efficiency,
                charge_efficiency,
                house_discharge_efficiency,
                required["sensor.hoymiles_hit_battery_capacity"] or 0.0,
                required["sensor.hoymiles_hit_overview_battery_soc"] or 0.0,
                protected_minimum_soc,
            )
            shared_snapshot = _shared_inputs_snapshot(self._runtime)
            if shared_snapshot is not None:
                captured_at = getattr(shared_snapshot, "captured_at", None)
                rce_plan_age = (
                    (now - captured_at.astimezone(now.tzinfo)).total_seconds()
                    if isinstance(captured_at, datetime)
                    and captured_at.tzinfo is not None
                    and captured_at.utcoffset() is not None
                    else None
                )
                shared_load = getattr(shared_snapshot, "load", None)
                rce_plan_fresh = bool(
                    shared_load is not None
                    and getattr(shared_load, "ready", False) is True
                    and rce_plan_age is not None
                    and -5.0 <= rce_plan_age <= RCE_PLAN_MAX_AGE_SECONDS
                )
            else:
                rce_plan = self._same_entry_rce_plan_state()
                rce_plan_age = _state_age_seconds(rce_plan, now)
                rce_plan_fresh = bool(
                    rce_plan is not None
                    and rce_plan_age is not None
                    and -5.0 <= rce_plan_age <= RCE_PLAN_MAX_AGE_SECONDS
                )
            freshness["sensor.hoymiles_hit_rce_optimized_plan"] = rce_plan_fresh
            ages["sensor.hoymiles_hit_rce_optimized_plan"] = (
                round(rce_plan_age, 1) if rce_plan_age is not None else None
            )
            forecast_state = (
                self.hass.states.get(energy_forecast.forecast_entity_id)
                if energy_forecast.forecast_entity_id
                else None
            )
            forecast_target_date = (
                energy_forecast.timeline_target_date
                or now.date()
                + timedelta(days=max(energy_forecast.risk_day_offset, 0))
            )
            forecast_usefulness = evaluate_pv_forecast_usefulness(
                forecast_state,
                now,
                target_date=forecast_target_date,
                max_age_seconds=FORECAST_MAX_AGE_SECONDS,
                update_state=resolve_solcast_update_state(self.hass.states),
                sun_state=self.hass.states.get("sun.sun"),
            )
            forecast_age = forecast_usefulness.age_seconds
            source_forecast_fresh = forecast_usefulness.source_fresh
            if energy_forecast.forecast_entity_id:
                freshness[energy_forecast.forecast_entity_id] = source_forecast_fresh
                ages[energy_forecast.forecast_entity_id] = (
                    round(forecast_age, 1) if forecast_age is not None else None
                )
            forecast_data_fresh = bool(
                forecast_usefulness.usable
                and energy_forecast.horizon != "missing"
            )
            history_age = (
                max((now - self._history_refreshed_at).total_seconds(), 0.0)
                if self._history_refreshed_at is not None
                else None
            )
            history_data_fresh = bool(
                self._history.history_days > 0
                and history_age is not None
                and history_age <= HISTORY_MAX_AGE_SECONDS
            )
            # RCEm owns only its Self-Use+safety hard floor.  RCE may still
            # expose reusable raw forecast/profile diagnostics, but its plan
            # target must never leak into this independent control objective.
            saved_export_limit = _state_number(
                self.hass,
                "input_number.hoymiles_rcm_saved_export_limit",
            )
            user_export_cap = required[
                "input_number.hoymiles_rcm_export_cap_percent"
            ]
            if not self.hass.states.is_state(
                "input_boolean.hoymiles_rcm_export_control_active",
                "on",
            ):
                saved_export_limit = current_export_limit or 0.0
            elif saved_export_limit is None:
                saved_export_limit = current_export_limit or 0.0

            pv_power, pv_power_fresh = sample(
                "sensor.hoymiles_hit_overview_pv_total_power",
                LIVE_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="power",
                shared_field="pv_power_kw",
                shared_scale=1000.0,
            )
            load_power, load_power_fresh = sample(
                "sensor.hoymiles_actual_load_power",
                LIVE_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="power",
                shared_field="home_load_power_kw",
                shared_scale=1000.0,
            )
            grid_export_power, grid_export_fresh = sample(
                "sensor.hoymiles_rce_grid_export_power",
                LIVE_TELEMETRY_MAX_AGE_SECONDS,
                shared_section="power",
                shared_field="grid_power_kw",
            )
            live_power_data_fresh = pv_power_fresh and load_power_fresh
            battery_soc = battery_soc_value
            _max_discharge, max_discharge_fresh = sample(
                "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
                ACTUATOR_MAX_AGE_SECONDS,
            )
            _force_discharge_soc, force_discharge_fresh = sample(
                "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
                ACTUATOR_MAX_AGE_SECONDS,
            )
            ems_mode_value, ems_mode_fresh = sample(
                "sensor.hoymiles_hit_ems_mode_readback_code",
                ACTUATOR_MAX_AGE_SECONDS,
            )
            ems_mode_fresh = bool(
                ems_mode_fresh and ems_mode_value in {0.0, 3.0, 4.0, 5.0}
            )
            # These three readbacks are sufficient to restore the discharge
            # registers and release RCEm ownership.  Keep this contract
            # independent from SOC/capacity telemetry: a recorder or BMS
            # outage must block a new predictive start, but must not prevent a
            # safe best-effort restore of registers whose readbacks are fresh.
            discharge_registers_data_fresh = bool(
                max_discharge_fresh
                and force_discharge_fresh
                and ems_mode_fresh
            )
            # Starting/continuing predictive discharge still needs both the
            # writable-register readbacks and fresh battery-state evidence.
            pre_discharge_actuator_fresh = bool(
                discharge_registers_data_fresh
                and battery_soc_fresh
                and battery_capacity is not None
            )
            optimizer_input = RCMOptimizerInput(
                    now=now,
                    voltage_l1_v=voltage_samples[GRID_VOLTAGE_ENTITIES[0]][0] or 0.0,
                    voltage_l2_v=voltage_samples[GRID_VOLTAGE_ENTITIES[1]][0] or 0.0,
                    voltage_l3_v=voltage_samples[GRID_VOLTAGE_ENTITIES[2]][0] or 0.0,
                    filtered_voltage_v=filtered,
                    rolling_10m_voltage_v=rolling_10m,
                    historical_p90_voltage_v=historical_p90,
                    risk_windows=self._history.risk_windows,
                    history_days=self._history.history_days,
                    pv_power_kw=max(pv_power or 0.0, 0.0) / 1000.0,
                    load_power_kw=max(load_power or 0.0, 0.0) / 1000.0,
                    grid_export_power_kw=max(
                        grid_export_power or 0.0,
                        0.0,
                    ),
                    # Preserve an exact 0 kWh as unavailable capacity.  The
                    # pure optimizer owns the fail-closed decision; coercing
                    # zero to 1 kWh here could authorize a false discharge.
                    battery_capacity_kwh=max(
                        battery_capacity
                        if battery_capacity is not None
                        else 0.0,
                        0.0,
                    ),
                    battery_soc_percent=battery_soc or 0.0,
                    reserve_soc_percent=(
                        required[
                            "sensor.hoymiles_hit_ems_self_use_soc_readback"
                        ]
                        or 0.0
                    ),
                    safety_margin_soc_percent=required["input_number.hoymiles_rcm_soc_safety_margin"] or 0.0,
                    protected_minimum_soc_percent=protected_minimum_soc,
                    expected_risk_surplus_kwh=energy_forecast.surplus_kwh,
                    expected_natural_headroom_kwh=(
                        energy_forecast.natural_headroom_kwh
                    ),
                    minutes_to_risk=energy_forecast.minutes_to_risk,
                    risk_day_offset=energy_forecast.risk_day_offset,
                    system_power_kw=system_power_kw,
                    battery_voltage_v=battery_voltage,
                    bms_max_charge_current_a=bms_charge_current,
                    bms_max_discharge_current_a=bms_discharge_current,
                    current_charge_limit_percent=(
                        current_charge_limit
                        if current_charge_limit is not None
                        else 10.0
                    ),
                    saved_charge_limit_percent=saved_limit,
                    export_control_enabled=export_control_enabled,
                    current_export_limit_percent=current_export_limit or 0.0,
                    saved_export_limit_percent=saved_export_limit,
                    user_export_cap_percent=(
                        100.0
                        if user_export_cap is None
                        else user_export_cap
                    ),
                    gcf_active=gcf_active,
                    gcf_data_fresh=gcf_data_fresh,
                    charge_efficiency_percent=charge_efficiency * 100.0,
                    house_discharge_efficiency_percent=(
                        house_discharge_efficiency * 100.0
                    ),
                    expected_pre_risk_surplus_kwh=(
                        energy_forecast.pre_risk_surplus_kwh
                    ),
                    risk_window_forecasts=energy_forecast.window_forecasts,
                    expected_unavoidable_charge_input_kwh=(
                        energy_forecast.unavoidable_charge_input_kwh
                    ),
                    expected_absorbable_risk_surplus_kwh=(
                        energy_forecast.absorbable_surplus_kwh
                    ),
                    expected_protected_home_energy_kwh=(
                        energy_forecast.protected_home_energy_kwh
                    ),
                    expected_stress_home_energy_kwh=(
                        energy_forecast.stress_protected_home_energy_kwh
                    ),
                    voltage_data_fresh=voltage_data_fresh,
                    emergency_voltage_data_fresh=(
                        emergency_voltage_data_fresh
                    ),
                    actuator_data_fresh=actuator_data_fresh,
                    history_data_fresh=history_data_fresh,
                    forecast_data_fresh=forecast_data_fresh,
                    load_profile_data_fresh=(
                        energy_forecast.load_profile_data_fresh
                        and house_efficiency_fresh
                    ),
                    live_power_data_fresh=live_power_data_fresh,
                    charge_actuator_data_fresh=charge_actuator_fresh,
                    export_actuator_data_fresh=export_actuator_fresh,
                    bms_charge_data_fresh=bms_charge_fresh,
                    bms_discharge_data_fresh=bms_discharge_fresh,
                    pre_discharge_actuator_data_fresh=(
                        pre_discharge_actuator_fresh
                    ),
                    pre_discharge_active=self.hass.states.is_state(
                        "input_boolean.hoymiles_rcm_pre_discharge_active",
                        "on",
                    ),
                    system_power_data_valid=system_power_data_valid,
            )
            self._full_plan_solver_calls += 1
            result = await self.hass.async_add_executor_job(
                optimize_rcm,
                optimizer_input,
            )
            if (
                not self._input_revision.is_current(captured_revision)
                or captured_fingerprint != self._current_input_fingerprint()
            ):
                self._full_plan_rejected_for_input_drift = True
                self._mark_recalculation_pending()
                return False
            timeline_target_date = (
                energy_forecast.timeline_target_date
                or now.date()
                + timedelta(days=max(energy_forecast.risk_day_offset, 0))
            )
            timeline_horizon_start, timeline_horizon_end = (
                _timeline_horizon_bounds(
                    now=now,
                    target_date=timeline_target_date,
                    risk_day_offset=energy_forecast.risk_day_offset,
                )
            )
            try:
                timeline_input = RCMTimelineModelInput(
                        generated_at=now,
                        requested_horizon_start=timeline_horizon_start,
                        requested_horizon_end=timeline_horizon_end,
                        energy_points=energy_forecast.timeline_energy_points,
                        risk_intervals=_timeline_risk_intervals(now, result),
                        battery_capacity_kwh=(
                            battery_capacity if battery_capacity is not None else None
                        ),
                        current_soc_percent=battery_soc,
                        protected_soc_floor_percent=(
                            result.protected_minimum_soc_percent
                        ),
                        maximum_soc_percent=100.0,
                        charge_power_limit_kw=(
                            min(
                                system_power_kw,
                                physical_charge_power_kw / charge_efficiency,
                            )
                            if result.bms_charge_available
                            else None
                        ),
                        discharge_power_limit_kw=(
                            result.bms_discharge_power_limit_kw
                            if result.bms_discharge_available
                            else None
                        ),
                        charge_efficiency=charge_efficiency,
                        discharge_efficiency=house_discharge_efficiency,
                        system_power_kw=(
                            system_power_kw if system_power_data_valid else None
                        ),
                        result=result,
                        inputs_fresh=bool(
                            forecast_data_fresh
                            and energy_forecast.load_profile_data_fresh
                            and house_efficiency_fresh
                            and battery_soc_fresh
                            and battery_capacity is not None
                        ),
                )
                self._timeline_trace = build_rcm_timeline_trace(
                    timeline_input
                )
                self._timeline_blocker = ""
                self._timeline_metadata = {
                    "forecast_today_entity": (
                        energy_forecast.forecast_entity_id
                    ),
                    "load_profile_broker_entity_id": (
                        self._load_broker_entity_id()
                    ),
                    "load_profile_broker_source": "rce_recorder_broker",
                }
                self._last_full_timeline_input = timeline_input
                self._last_full_timeline_trace = self._timeline_trace
                self._last_full_timeline_metadata = dict(
                    self._timeline_metadata
                )
                self._live_timeline_signature = None
            except RCMTimelineModelError as err:
                self._timeline_trace = None
                self._timeline_blocker = err.blocker_code
            except Exception:  # noqa: BLE001 - never alter the optimizer result
                _LOGGER.exception("Cannot build the observation-only RCEm timeline")
                self._timeline_trace = None
                self._timeline_blocker = "timeline_model_error"
            risk_window_details = [
                {
                    "start": (
                        f"{item.start_minute // 60:02d}:"
                        f"{item.start_minute % 60:02d}"
                    ),
                    "end": (
                        f"{item.end_minute // 60:02d}:"
                        f"{item.end_minute % 60:02d}"
                    ),
                    "peak_voltage_v": item.peak_voltage_v,
                    "day_offset": item.day_offset,
                    "expected_pv_kwh": item.expected_pv_kwh,
                    "expected_load_kwh": item.expected_load_kwh,
                    "expected_surplus_kwh": item.expected_surplus_kwh,
                    "absorbable_surplus_kwh": item.absorbable_surplus_kwh,
                    "protected_home_energy_kwh": item.protected_home_energy_kwh,
                    "stress_protected_home_energy_kwh": (
                        item.stress_protected_home_energy_kwh
                    ),
                    "absorption_power_limited": item.absorption_power_limited,
                    "required_headroom_kwh": item.required_headroom_kwh,
                    "projected_headroom_before_kwh": (
                        item.projected_headroom_before_kwh
                    ),
                    "cumulative_headroom_shortfall_kwh": (
                        item.cumulative_headroom_shortfall_kwh
                    ),
                }
                for item in result.risk_window_plans
            ]
            self._attributes = {
                "status_code": result.status_code,
                "missing_entities": [],
                "enabled": self.hass.states.is_state("input_boolean.hoymiles_rcm_enabled", "on"),
                "action": result.action,
                "action_interval_start": (
                    result.action_interval_start.isoformat()
                    if result.action_interval_start is not None
                    else None
                ),
                "action_interval_end": (
                    result.action_interval_end.isoformat()
                    if result.action_interval_end is not None
                    else None
                ),
                "voltage_l1_v": round(voltage_samples[GRID_VOLTAGE_ENTITIES[0]][0] or 0.0, 2),
                "voltage_l2_v": round(voltage_samples[GRID_VOLTAGE_ENTITIES[1]][0] or 0.0, 2),
                "voltage_l3_v": round(voltage_samples[GRID_VOLTAGE_ENTITIES[2]][0] or 0.0, 2),
                "maximum_voltage_v": result.maximum_voltage_v,
                "filtered_voltage_v": round(filtered, 2),
                "rolling_10m_voltage_v": round(rolling_10m, 2),
                "historical_p90_voltage_v": (
                    round(historical_p90, 3)
                    if self._history.history_days > 0 and historical_p90 > 0
                    else None
                ),
                "historical_p90_available": (
                    self._history.history_days > 0 and historical_p90 > 0
                ),
                "historical_p90_slot_index": current_slot,
                "historical_p90_slot_start": (
                    f"{current_slot // 4:02d}:{(current_slot % 4) * 15:02d}"
                ),
                "historical_p90_source": "recorder_15m_four_day_profile",
                "voltage_risk_score_percent": result.voltage_risk_score,
                "risk_window_active": result.risk_window_active,
                "next_risk_start": _minutes_text(result.next_risk_start_minute),
                "risk_windows": [_window_text(item) for item in self._history.risk_windows],
                "history_days": self._history.history_days,
                "history_data_fresh": result.history_data_fresh,
                "history_age_seconds": (
                    round(history_age, 1) if history_age is not None else None
                ),
                "history_samples": self._history.sample_count,
                "history_daily_peak_v": self._history.daily_peak_v,
                "history_profile_median_v": list(self._history.profile_median_v),
                "history_profile_p90_v": [
                    round(value, 3) if value > 0 else None
                    for value in self._history.profile_p90_v
                ],
                "reserve_soc_percent": result.reserve_soc_percent,
                "protected_minimum_soc_percent": result.protected_minimum_soc_percent,
                "required_headroom_kwh": result.required_headroom_kwh,
                "available_headroom_kwh": result.available_headroom_kwh,
                "headroom_shortfall_kwh": result.headroom_shortfall_kwh,
                "unconstrained_required_headroom_kwh": (
                    result.unconstrained_required_headroom_kwh
                ),
                "creatable_headroom_kwh": result.creatable_headroom_kwh,
                "unabsorbed_surplus_due_floor_kwh": (
                    result.unabsorbed_surplus_due_floor_kwh
                ),
                "absorbable_risk_surplus_kwh": result.absorbable_risk_surplus_kwh,
                "protected_home_energy_kwh": result.protected_home_energy_kwh,
                "nominal_pre_risk_home_buffer_kwh": (
                    result.nominal_pre_risk_home_buffer_kwh
                ),
                "stress_protected_home_energy_kwh": (
                    result.stress_protected_home_energy_kwh
                ),
                "stress_reserve_energy_critical": (
                    result.stress_reserve_energy_critical
                ),
                "stress_discharge_limited": result.stress_discharge_limited,
                "charge_efficiency_percent": round(
                    charge_efficiency * 100.0,
                    1,
                ),
                "house_discharge_efficiency_percent": round(
                    house_discharge_efficiency * 100.0,
                    1,
                ),
                "headroom_power_limited": result.headroom_power_limited,
                "headroom_capacity_limited": result.headroom_capacity_limited,
                "expected_natural_headroom_kwh": result.expected_natural_headroom_kwh,
                "unavoidable_minimum_charge_kwh": (
                    result.unavoidable_minimum_charge_kwh
                ),
                "unavoidable_charge_before_risk_kwh": (
                    result.unavoidable_minimum_charge_kwh
                ),
                "planned_grid_discharge_kwh": result.planned_grid_discharge_kwh,
                "pre_discharge_target_soc_percent": result.pre_discharge_target_soc_percent,
                "pre_discharge_power_kw": result.pre_discharge_power_kw,
                "pre_discharge_power_percent": result.pre_discharge_power_percent,
                "pre_discharge_ready": result.pre_discharge_ready,
                "pre_discharge_start_eligible": (
                    result.pre_discharge_start_eligible
                ),
                "pre_discharge_continue_eligible": (
                    result.pre_discharge_continue_eligible
                ),
                "pre_discharge_transaction_ready": (
                    result.pre_discharge_transaction_ready
                ),
                "pre_discharge_deadline": (
                    result.pre_discharge_deadline.isoformat()
                    if result.pre_discharge_deadline is not None
                    else None
                ),
                "minutes_to_risk": energy_forecast.minutes_to_risk,
                "risk_day_offset": energy_forecast.risk_day_offset,
                "target_soc_before_risk_percent": result.target_soc_before_risk_percent,
                "expected_risk_surplus_kwh": round(
                    energy_forecast.surplus_kwh,
                    3,
                ),
                "risk_surplus_horizon": energy_forecast.horizon,
                "selected_forecast_kwh": round(
                    energy_forecast.selected_forecast_kwh,
                    3,
                ),
                "selected_forecast_p90_kwh": (
                    round(energy_forecast.selected_forecast_p90_kwh, 3)
                    if energy_forecast.selected_forecast_p90_kwh is not None
                    else None
                ),
                "selected_forecast_p10_kwh": (
                    round(energy_forecast.selected_forecast_p10_kwh, 3)
                    if energy_forecast.selected_forecast_p10_kwh is not None
                    else None
                ),
                "forecast_entity_id": energy_forecast.forecast_entity_id,
                "forecast_source_fresh": forecast_usefulness.source_fresh,
                "forecast_usable": forecast_usefulness.usable,
                "forecast_usability_mode": forecast_usefulness.mode,
                "forecast_usability_reason": forecast_usefulness.reason,
                "forecast_source_age_seconds": (
                    round(forecast_usefulness.age_seconds, 1)
                    if forecast_usefulness.age_seconds is not None
                    else None
                ),
                "forecast_last_success": (
                    forecast_usefulness.last_success_at.isoformat()
                    if forecast_usefulness.last_success_at is not None
                    else None
                ),
                "forecast_next_update": (
                    forecast_usefulness.next_update_at.isoformat()
                    if forecast_usefulness.next_update_at is not None
                    else None
                ),
                "forecast_valid_until": (
                    forecast_usefulness.valid_until.isoformat()
                    if forecast_usefulness.valid_until is not None
                    else None
                ),
                "forecast_target_date": forecast_usefulness.target_date.isoformat(),
                "forecast_coverage_start_date": (
                    forecast_usefulness.coverage_start_date.isoformat()
                    if forecast_usefulness.coverage_start_date is not None
                    else None
                ),
                "forecast_coverage_end_date": (
                    forecast_usefulness.coverage_end_date.isoformat()
                    if forecast_usefulness.coverage_end_date is not None
                    else None
                ),
                "forecast_coverage_complete": forecast_usefulness.coverage_complete,
                "forecast_profile_source": (
                    energy_forecast.forecast_profile_source
                ),
                "pv_profile_source": energy_forecast.forecast_profile_source,
                "headroom_pv_profile_source": (
                    energy_forecast.forecast_profile_source
                ),
                "reserve_pv_profile_source": (
                    energy_forecast.reserve_forecast_profile_source
                ),
                "forecast_profile_confidence_percent": round(
                    energy_forecast.forecast_profile_confidence * 100.0,
                    1,
                ),
                "pv_profile_confidence_percent": round(
                    energy_forecast.forecast_profile_confidence * 100.0,
                    1,
                ),
                "load_profile_source": energy_forecast.load_profile_source,
                "load_profile_mode": energy_forecast.load_profile_source,
                "load_profile_broker_entity_id": (
                    self._load_broker_entity_id()
                ),
                "load_profile_broker_source": "rce_recorder_broker",
                "load_profile_broker_fresh": (
                    energy_forecast.load_profile_data_fresh
                ),
                "load_profile_broker_age_seconds": (
                    round(rce_plan_age, 1)
                    if rce_plan_age is not None
                    else None
                ),
                "headroom_load_profile_source": (
                    energy_forecast.headroom_load_profile_source
                ),
                "reserve_load_profile_source": (
                    energy_forecast.reserve_load_profile_source
                ),
                "load_profile_confidence_percent": round(
                    energy_forecast.load_profile_confidence * 100.0,
                    1,
                ),
                "expected_risk_load_kwh": round(
                    energy_forecast.expected_load_kwh,
                    3,
                ),
                "expected_pre_risk_surplus_kwh": round(
                    energy_forecast.pre_risk_surplus_kwh,
                    3,
                ),
                "risk_window_details": risk_window_details,
                "risk_window_energy_plans": risk_window_details,
                "pv_surplus_power_kw": result.pv_surplus_power_kw,
                "bms_charge_power_limit_kw": result.bms_charge_power_limit_kw,
                "bms_discharge_power_limit_kw": (
                    result.bms_discharge_power_limit_kw
                ),
                "bms_discharge_dc_power_limit_kw": (
                    result.bms_discharge_dc_power_limit_kw
                ),
                "bms_charge_available": result.bms_charge_available,
                "bms_charge_quantization_limited": (
                    result.bms_charge_quantization_limited
                ),
                "bms_discharge_available": result.bms_discharge_available,
                "recommended_charge_limit_percent": result.recommended_charge_limit_percent,
                "recommended_charge_power_kw": result.recommended_charge_power_kw,
                "export_control_enabled": export_control_enabled,
                "gcf_active": gcf_active,
                "gcf_data_fresh": gcf_data_fresh,
                "export_control_path_enabled": export_control_path_enabled,
                "effective_export_cap_percent": result.effective_export_cap_percent,
                "recommended_export_limit_percent": result.recommended_export_limit_percent,
                "estimated_safe_export_power_kw": result.estimated_safe_export_power_kw,
                "estimated_safe_export_available": (
                    result.estimated_safe_export_power_kw is not None
                ),
                "estimated_safe_export_reason": (
                    "live_pv_surplus"
                    if result.estimated_safe_export_power_kw is not None
                    else "not_applicable_no_pv_surplus"
                ),
                "battery_limit_saturated": result.saturated,
                "live_emergency": result.live_emergency,
                "emergency_action_ready": result.emergency_action_ready,
                "prediction_ready": result.prediction_ready,
                "prediction_block_reason": result.prediction_block_reason,
                "system_power_data_valid": result.system_power_data_valid,
                "inverter_count": inverter_count,
                "system_power_kw": (
                    round(system_power_kw, 3)
                    if system_power_data_valid
                    else None
                ),
                "voltage_data_fresh": result.voltage_data_fresh,
                "emergency_voltage_data_fresh": (
                    result.emergency_voltage_data_fresh
                ),
                "actuator_data_fresh": result.actuator_data_fresh,
                "charge_actuator_data_fresh": (
                    result.charge_actuator_data_fresh
                ),
                "export_actuator_data_fresh": (
                    result.export_actuator_data_fresh
                ),
                "export_register_data_fresh": export_register_data_fresh,
                "forecast_data_fresh": result.forecast_data_fresh,
                "load_profile_data_fresh": result.load_profile_data_fresh,
                "live_power_data_fresh": result.live_power_data_fresh,
                "bms_charge_data_fresh": bms_charge_fresh,
                "bms_discharge_data_fresh": bms_discharge_fresh,
                "pre_discharge_actuator_data_fresh": (
                    pre_discharge_actuator_fresh
                ),
                "discharge_registers_data_fresh": (
                    discharge_registers_data_fresh
                ),
                "ems_mode_data_fresh": ems_mode_fresh,
                "maximum_discharge_power_data_fresh": max_discharge_fresh,
                "force_discharge_soc_data_fresh": force_discharge_fresh,
                "battery_soc_data_fresh": battery_soc_fresh,
                "battery_capacity_data_available": battery_capacity is not None,
                "battery_capacity_source_entity_id": (
                    battery_capacity_source or None
                ),
                "battery_capacity_data_fresh": battery_capacity_report_fresh,
                "data_freshness": freshness,
                "data_age_seconds": ages,
                "actuators": [
                    "sensor.hoymiles_hit_battery_max_charge_power_readback",
                    "sensor.hoymiles_hit_gcf_maximum_export_power_readback"
                    if export_control_path_enabled
                    else "export_control_inactive_or_gcf_disabled",
                ],
                "protected_entities": [
                    "sensor.hoymiles_hit_gcf_enable_readback_code",
                    "three_phase_unbalance",
                    "grid_protection_settings",
                ],
            }
            # Retain only an input whose solver result, timeline handling and
            # public attribute projection all completed after the stale guard.
            # The live loop freezes its forecast/history fields and replaces
            # every physical control field with a fresh sample.
            self._last_full_optimizer_input = optimizer_input
            self._last_full_optimizer_result = result
            self._live_emergency_active = result.live_emergency
            self._last_full_plan_at = dt_util.now()
            return True
        except Exception:  # noqa: BLE001 - automation must fail closed
            if (
                not self._input_revision.is_current(captured_revision)
                or captured_fingerprint != self._current_input_fingerprint()
            ):
                self._full_plan_rejected_for_input_drift = True
                self._mark_recalculation_pending()
                return False
            _LOGGER.exception("Cannot calculate the RCEm voltage plan")
            if self._retain_last_complete_plan(
                blocker_code="optimizer_error",
                missing_entities=(),
            ):
                return False
            self._timeline_trace = None
            self._timeline_blocker = "optimizer_error"
            self._attributes = {
                "status_code": "optimizer_error",
                "missing_entities": [],
                "risk_windows": [],
            }
            return True

    def _retain_last_complete_plan(
        self,
        *,
        blocker_code: str,
        missing_entities: Any,
        voltage_data_fresh: bool | None = None,
        emergency_voltage_data_fresh: bool | None = None,
        actuator_data_fresh: bool | None = None,
        freshness: Any | None = None,
        ages: Any | None = None,
    ) -> bool:
        """Keep one recent risk plan visible without execution authority."""

        completed_at = self._last_full_plan_at
        if self._timeline_trace is None or completed_at is None:
            return False
        try:
            age_seconds = (
                dt_util.now().astimezone(timezone.utc)
                - completed_at.astimezone(timezone.utc)
            ).total_seconds()
        except (AttributeError, OverflowError, TypeError, ValueError):
            return False
        if age_seconds < -5.0 or age_seconds > _LAST_COMPLETE_PLAN_GRACE_SECONDS:
            return False
        updates: dict[str, Any] = {
            "missing_entities": list(missing_entities),
            "result_current": False,
            "recalculation_pending": True,
            "plan_display_available": True,
            "plan_data_state": "last_complete_inputs_missing",
            "execution_input_valid": False,
            "execution_blocker_code": blocker_code,
            "last_complete_age_seconds": round(max(age_seconds, 0.0), 1),
        }
        if voltage_data_fresh is not None:
            updates["voltage_data_fresh"] = voltage_data_fresh
        if emergency_voltage_data_fresh is not None:
            updates["emergency_voltage_data_fresh"] = emergency_voltage_data_fresh
        if actuator_data_fresh is not None:
            updates["actuator_data_fresh"] = actuator_data_fresh
        if freshness is not None:
            updates["data_freshness"] = freshness
        if ages is not None:
            updates["data_age_seconds"] = ages
        self._attributes = {**self._attributes, **updates}
        if self._timeline_sensor is not None:
            self._timeline_sensor.publish_unavailable(
                input_revision=self._input_revision.value,
                blocker_code=blocker_code,
            )
        return True
