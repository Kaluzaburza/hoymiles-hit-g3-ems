"""Home Assistant sensor exposing the optimized two-day RCE plan."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from datetime import date, datetime, time, timedelta
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .bounded_history import async_get_bounded_state_reports
from .const import DOMAIN, NAME
from .energy_data import numeric_state_sample, state_age_seconds
from .forecast_model import (
    ForecastLearningPolicy,
    adaptive_forecast_factor,
    blend_low_expected,
    forecast_factor_for_policy,
    forecast_learning_history_day_eligible,
    resolve_forecast_learning_policy,
    robust_weighted_factor,
    uncertainty_risk_weight,
)
from .models import RuntimeData
from .optimizer_revision import (
    INPUT_RECALCULATION_DELAY_SECONDS,
    MAX_IMMEDIATE_RECALCULATIONS,
    OptimizerInputRevision,
    TARIFF_PRICE_BROKER_ATTRIBUTES,
    optimizer_input_fingerprint,
)
from .rce_history import (
    LOAD_PHASE_ENERGY_ENTITIES,
    LoadHistorySummary,
    summarize_load_history,
)
from .rce_optimizer import (
    OptimizerInput,
    OptimizerResult,
    floor_half_hour,
    optimize_rce,
    parse_rce_rows,
    robust_weighted_upper_estimate,
)


_LOGGER = logging.getLogger(__name__)
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")
_RCE_PRICE_MAX_AGE_SECONDS = 20 * 60.0
_TODAY_FORECAST_MAX_AGE_SECONDS = 6 * 60 * 60.0
_TOMORROW_FORECAST_MAX_AGE_SECONDS = 12 * 60 * 60.0
_DAY3_FORECAST_MAX_AGE_SECONDS = 18 * 60 * 60.0
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_FORECAST_GCF_SUPPORT_MAX_AGE_SECONDS = 10.0
_FORECAST_GCF_READBACK_MAX_AGE_SECONDS = 180.0
_FORECAST_GCF_READBACK_MAX_SKEW_SECONDS = 5.0

FORECAST_GCF_ENABLE_ENTITY = (
    "sensor.hoymiles_hit_gcf_enable_readback_code"
)
FORECAST_GCF_LIMIT_ENTITY = (
    "sensor.hoymiles_hit_gcf_maximum_export_power_readback"
)
FORECAST_GCF_GENERATION_ENTITY = (
    "sensor.hoymiles_hit_gcf_control_readback_generation"
)
FORECAST_GCF_SUPPORT_ENTITY = (
    "sensor.hoymiles_hit_ems_verified_hardware_readback_supported"
)
FORECAST_EXPORT_ALLOWED_ENTITY = "binary_sensor.hoymiles_ems_export_allowed"
FORECAST_EMS_PACKAGE_VERSION_ENTITY = "sensor.hoymiles_ems_package_version"
FORECAST_GCF_POLICY_TRIGGER_ENTITIES = (
    FORECAST_GCF_ENABLE_ENTITY,
    FORECAST_GCF_LIMIT_ENTITY,
    FORECAST_GCF_GENERATION_ENTITY,
    FORECAST_GCF_SUPPORT_ENTITY,
)
FORECAST_GCF_COHORT_REPORT_ENTITIES = (
    FORECAST_GCF_ENABLE_ENTITY,
    FORECAST_GCF_LIMIT_ENTITY,
    FORECAST_GCF_GENERATION_ENTITY,
)

TODAY_FORECAST_ENTITY_HELPER = (
    "input_text.hoymiles_solcast_forecast_today_entity"
)
TOMORROW_FORECAST_ENTITY_HELPER = (
    "input_text.hoymiles_solcast_forecast_tomorrow_entity"
)
DAY3_FORECAST_ENTITY_HELPER = (
    "input_text.hoymiles_solcast_forecast_day_3_entity"
)
FORECAST_ENTITY_HELPERS = (
    TODAY_FORECAST_ENTITY_HELPER,
    TOMORROW_FORECAST_ENTITY_HELPER,
    DAY3_FORECAST_ENTITY_HELPER,
)

TODAY_FORECAST_CANDIDATES = (
    "sensor.solcast_pv_forecast_forecast_today",
    "sensor.solcast_pv_forecast_prognoza_na_dzisiaj",
    "sensor.solcast_forecast_today",
)
TOMORROW_FORECAST_CANDIDATES = (
    "sensor.solcast_pv_forecast_forecast_tomorrow",
    "sensor.solcast_pv_forecast_prognoza_na_jutro",
    "sensor.solcast_forecast_tomorrow",
)
DAY3_FORECAST_CANDIDATES = (
    "sensor.solcast_pv_forecast_forecast_day_3",
    "sensor.solcast_pv_forecast_forecast_d3",
    "sensor.solcast_pv_forecast_day_3",
    "sensor.solcast_pv_forecast_d3",
    "sensor.solcast_pv_forecast_prognoza_na_dzien_3",
    "sensor.solcast_pv_forecast_prognoza_d3",
    "sensor.solcast_forecast_day_3",
    "sensor.solcast_forecast_d3",
)
REMAINING_TODAY_CANDIDATES = (
    "sensor.solcast_pv_forecast_forecast_remaining_today",
    "sensor.solcast_pv_forecast_pozostala_prognoza_na_dzis",
    "sensor.solcast_forecast_remaining_today",
)

WATCHED_ENTITIES = {
    "sensor.hoymiles_rce_day",
    "sensor.hoymiles_rce_day_tomorrow",
    "sensor.hoymiles_hit_battery_capacity",
    "sensor.hoymiles_hit_overview_battery_soc",
    "sensor.hoymiles_hit_number_of_machines_master_and_slave",
    "sensor.hoymiles_hit_pv_total_energy_today",
    "sensor.hoymiles_hit_pv_to_load_energy_today",
    "sensor.hoymiles_hit_energy_from_battery_today",
    "sensor.hoymiles_hit_energy_from_grid_today",
    "sensor.hoymiles_hit_load_from_pv_power",
    "sensor.hoymiles_hit_overview_pv_total_power",
    "sensor.hoymiles_actual_load_power",
    "sensor.hoymiles_actual_load_energy_today",
    *LOAD_PHASE_ENERGY_ENTITIES,
    "sensor.hoymiles_hit_load_power_l1n",
    "sensor.hoymiles_hit_load_power_l2n",
    "sensor.hoymiles_hit_load_power_l3n",
    "sensor.hoymiles_hit_overview_load_active_power",
    "sensor.hoymiles_load_average_4_days",
    "sensor.hoymiles_night_load_average_4_days",
    "sensor.hoymiles_hit_battery_voltage_bms",
    "sensor.hoymiles_hit_maximum_charge_current",
    "sensor.hoymiles_hit_maximum_discharge_current",
    "sensor.hoymiles_hit_ems_self_use_soc_readback",
    "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
    "sensor.hoymiles_hit_gcf_maximum_export_power_readback",
    "sensor.hoymiles_hit_gcf_enable_readback_code",
    "sensor.hoymiles_rce_effective_export_power",
    "sensor.hoymiles_rce_learned_export_power",
    "sensor.hoymiles_hit_tariff_charge_plan",
    "input_boolean.hoymiles_rce_discharge_enabled",
    "input_boolean.hoymiles_rce_dynamic_soc_enabled",
    "input_boolean.hoymiles_sale_block_enabled",
    "input_datetime.hoymiles_sale_block_start",
    "input_datetime.hoymiles_sale_block_end",
    "input_number.hoymiles_rce_soc_safety_margin",
    "input_number.hoymiles_rce_export_efficiency",
    "input_number.hoymiles_rce_fallback_daily_load",
    "input_number.hoymiles_rce_requested_discharge_power",
    "input_number.hoymiles_rce_battery_wear_cost",
    "input_number.hoymiles_tariff_g11_price",
    "input_number.hoymiles_tariff_charge_efficiency",
    "input_number.hoymiles_tariff_discharge_efficiency",
    "input_select.hoymiles_rce_inverter_rated_power",
    *FORECAST_ENTITY_HELPERS,
    "sun.sun",
    *TODAY_FORECAST_CANDIDATES,
    *TOMORROW_FORECAST_CANDIDATES,
    *DAY3_FORECAST_CANDIDATES,
    *REMAINING_TODAY_CANDIDATES,
}

# These continuously changing physical values are sampled by the existing
# one-minute optimizer timer instead of withdrawing plan authority on every
# ESPHome state change.  The scheduler keeps its independent live SOC, BMS,
# export-permission and hardware-readback gates, so a real safety loss still
# stops execution immediately.  In particular, the Force Discharge readback
# is written by the RCE transaction itself and must not invalidate that same
# transaction before its mode write can be acknowledged.
RCE_MINUTE_COALESCED_ENTITIES = {
    "sensor.hoymiles_hit_overview_battery_soc",
    "sensor.hoymiles_hit_pv_total_energy_today",
    "sensor.hoymiles_hit_pv_to_load_energy_today",
    "sensor.hoymiles_hit_energy_from_battery_today",
    "sensor.hoymiles_hit_energy_from_grid_today",
    "sensor.hoymiles_hit_load_from_pv_power",
    "sensor.hoymiles_hit_overview_pv_total_power",
    "sensor.hoymiles_actual_load_power",
    "sensor.hoymiles_actual_load_energy_today",
    *LOAD_PHASE_ENERGY_ENTITIES,
    "sensor.hoymiles_hit_load_power_l1n",
    "sensor.hoymiles_hit_load_power_l2n",
    "sensor.hoymiles_hit_load_power_l3n",
    "sensor.hoymiles_hit_overview_load_active_power",
    "sensor.hoymiles_hit_battery_voltage_bms",
    "sensor.hoymiles_hit_maximum_charge_current",
    "sensor.hoymiles_hit_maximum_discharge_current",
    "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
    "sensor.hoymiles_rce_effective_export_power",
    "sensor.hoymiles_rce_learned_export_power",
    "sun.sun",
}
RCE_GCF_OPTIMIZER_ENTITIES = frozenset(
    {
        FORECAST_GCF_ENABLE_ENTITY,
        FORECAST_GCF_LIMIT_ENTITY,
    }
)
RCE_EVENT_DRIVEN_ENTITIES = (
    WATCHED_ENTITIES
    - RCE_MINUTE_COALESCED_ENTITIES
    - RCE_GCF_OPTIMIZER_ENTITIES
)


def _forecast_learning_policy_snapshot(
    hass: HomeAssistant,
    now: datetime,
) -> tuple[ForecastLearningPolicy, dict[str, Any]]:
    """Read one coherent, physically verified GCF learning policy."""

    support = numeric_state_sample(
        hass.states.get(FORECAST_GCF_SUPPORT_ENTITY),
        now,
        max_age_seconds=_FORECAST_GCF_SUPPORT_MAX_AGE_SECONDS,
        minimum=0.0,
        maximum=1.0,
    )
    generation = numeric_state_sample(
        hass.states.get(FORECAST_GCF_GENERATION_ENTITY),
        now,
        max_age_seconds=_FORECAST_GCF_READBACK_MAX_AGE_SECONDS,
        minimum=1.0,
        maximum=16_000_000.0,
    )
    enable = numeric_state_sample(
        hass.states.get(FORECAST_GCF_ENABLE_ENTITY),
        now,
        max_age_seconds=_FORECAST_GCF_READBACK_MAX_AGE_SECONDS,
        minimum=0.0,
        maximum=1.0,
    )
    export_limit = numeric_state_sample(
        hass.states.get(FORECAST_GCF_LIMIT_ENTITY),
        now,
        max_age_seconds=_FORECAST_GCF_READBACK_MAX_AGE_SECONDS,
        minimum=-10.0,
        maximum=200.0,
    )

    reason: str | None = None
    if not support.fresh:
        reason = f"hardware_readback_{support.reason}"
    elif support.value != 1.0:
        reason = "hardware_readback_unverified"
    elif not generation.fresh:
        reason = f"gcf_generation_{generation.reason}"
    elif not enable.fresh:
        reason = f"gcf_enable_{enable.reason}"
    elif not export_limit.fresh:
        reason = f"gcf_limit_{export_limit.reason}"

    coherent = False
    if reason is None:
        reported = (
            generation.reported_at,
            enable.reported_at,
            export_limit.reported_at,
        )
        if all(item is not None for item in reported):
            assert generation.reported_at is not None
            assert enable.reported_at is not None
            assert export_limit.reported_at is not None
            try:
                cohort_span = (
                    max(reported) - min(reported)
                ).total_seconds()
                coherent = bool(
                    0.0
                    <= cohort_span
                    <= _FORECAST_GCF_READBACK_MAX_SKEW_SECONDS
                )
            except (TypeError, ValueError):
                coherent = False
        if not coherent:
            reason = "gcf_readback_incoherent"

    verified = reason is None
    policy = resolve_forecast_learning_policy(
        readback_verified=verified,
        gcf_enable_code=enable.value,
        export_limit_percent=export_limit.value,
        unverified_reason=reason,
    )
    diagnostics = {
        "forecast_learning_gcf_readback_verified": verified,
        "forecast_learning_gcf_readback_reason": reason,
        "forecast_learning_gcf_enable_code": enable.value,
        "forecast_learning_gcf_export_limit_percent": export_limit.value,
        "forecast_learning_gcf_generation": generation.value,
        "forecast_learning_gcf_generation_age_seconds": (
            round(generation.age_seconds, 1)
            if generation.age_seconds is not None
            else None
        ),
    }
    return policy, diagnostics


def _forecast_learning_policy_signature(
    policy: ForecastLearningPolicy,
) -> tuple[bool, str, str | None, float | None]:
    """Return only semantic fields, excluding the 20-second generation."""

    return (
        policy.enabled,
        policy.mode,
        policy.excluded_reason,
        policy.factor_override,
    )


def _forecast_gcf_optimizer_signature(
    policy: ForecastLearningPolicy,
    diagnostics: Mapping[str, Any],
) -> tuple[bool, str, str | None, float | None, float | None, float | None]:
    """Return the stable physical GCF values consumed by RCE planning.

    The FC03 generation proves freshness and cohort coherence, but advances on
    every unchanged physical report. Excluding it here prevents a periodic
    provenance heartbeat from becoming a new mathematical optimizer input.
    """

    return (
        *_forecast_learning_policy_signature(policy),
        diagnostics.get("forecast_learning_gcf_enable_code"),
        diagnostics.get("forecast_learning_gcf_export_limit_percent"),
    )


STATUS_TEXT = {
    "pl": {
        "ready": "Gotowa — plan zoptymalizowany",
        "waiting_for_market": "Oczekiwanie — brak dostępnego okna rynkowego",
        "home_protected": "Zasilanie domu zabezpieczone — brak energii na sprzedaż",
        "home_energy_shortage": "Za mało energii na potrzeby domu — sprzedaż zablokowana",
        "missing_data": "Brak wymaganych danych — sprzedaż zablokowana",
        "optimizer_error": "Błąd obliczeń — sprzedaż zablokowana",
        "zero_export": "Eksport zablokowany — aktywny limit GCF 0%",
    },
    "en": {
        "ready": "Ready — optimized plan",
        "waiting_for_market": "Waiting — no available market window",
        "home_protected": "Home supply protected — no energy available for export",
        "home_energy_shortage": "Insufficient home energy — export blocked",
        "missing_data": "Required data missing — export blocked",
        "optimizer_error": "Calculation error — export blocked",
        "zero_export": "Export blocked — active GCF limit is 0%",
    },
}

TODAY_ONLY_SUFFIX = {
    "pl": "plan tylko na dziś; jutro zostanie przeliczone automatycznie",
    "en": "today-only plan; tomorrow will be recalculated automatically",
}


def _state_number(hass: HomeAssistant, entity_id: str) -> float | None:
    state = hass.states.get(entity_id)
    if state is None or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


def _state_attribute_number(
    hass: HomeAssistant,
    entity_id: str,
    attribute: str,
) -> float | None:
    state = hass.states.get(entity_id)
    if state is None:
        return None
    try:
        return float(state.attributes[attribute])
    except (KeyError, TypeError, ValueError):
        return None


def _state_text(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    if state is None or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
        return ""
    return state.state.strip()


def _helper_minutes(hass: HomeAssistant, entity_id: str) -> int | None:
    value = _state_text(hass, entity_id)
    parts = value.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except ValueError:
        return None


def _select_number(hass: HomeAssistant, entity_id: str) -> float | None:
    match = _NUMBER.search(_state_text(hass, entity_id).replace(",", "."))
    return float(match.group(0)) if match else None


def _first_numeric_state(
    hass: HomeAssistant,
    candidates: tuple[str, ...],
    configured: str = "",
) -> tuple[str, State | None]:
    entity_ids = ((configured,) if configured else ()) + candidates
    seen: set[str] = set()
    for entity_id in entity_ids:
        if not entity_id or entity_id in seen:
            continue
        seen.add(entity_id)
        state = hass.states.get(entity_id)
        if state is None:
            continue
        try:
            float(state.state)
        except (TypeError, ValueError):
            continue
        return entity_id, state
    return "", None


def _configured_forecast_entity_ids(hass: HomeAssistant) -> frozenset[str]:
    """Return valid source entity IDs selected through forecast helpers.

    The helpers may point at user-created template sensors whose IDs cannot be
    known when the integration is loaded.  Keeping these IDs in both the event
    subscription and optimizer fingerprint prevents a published plan from
    remaining execution-current after its real source changes.
    """
    return frozenset(
        entity_id
        for helper in FORECAST_ENTITY_HELPERS
        if (
            (entity_id := _state_text(hass, helper).strip().lower())
            and _ENTITY_ID.fullmatch(entity_id)
        )
    )


def _parse_datetime(value: Any, timezone: ZoneInfo) -> datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _complete_rce_half_hours_for_local_date(
    rows: list[Mapping[str, Any]],
    target_date: date,
    timezone: ZoneInfo,
) -> tuple[bool, int, int]:
    """Validate every real half-hour between two local midnights.

    Warsaw has 46, 48 or 50 real half-hours on DST transition/normal days.
    Counting raw rows with a fixed threshold can therefore accept a damaged
    normal/autumn payload or reject valid spring data.  Reuse the production
    parser and compare its absolute UTC instants with the exact expected set.
    """

    local_start = datetime.combine(target_date, time.min, tzinfo=timezone)
    local_end = datetime.combine(
        target_date + timedelta(days=1),
        time.min,
        tzinfo=timezone,
    )
    start_utc = local_start.astimezone(dt_util.UTC)
    end_utc = local_end.astimezone(dt_util.UTC)
    expected: set[datetime] = set()
    cursor = start_utc
    while cursor < end_utc:
        expected.add(cursor)
        cursor += timedelta(minutes=30)
    parsed = parse_rce_rows(
        rows,
        timezone,
        block_enabled=False,
        block_start_minute=0,
        block_end_minute=0,
    )
    actual = {
        slot.start.astimezone(dt_util.UTC)
        for slot in parsed
        if start_utc <= slot.start.astimezone(dt_util.UTC) < end_utc
    }
    return actual == expected, len(actual), len(expected)


def _state_age_minutes(state: State | None, now: datetime) -> float | None:
    """Return age of the latest HA state report without assuming its version."""
    age = state_age_seconds(state, now)
    return age / 60.0 if age is not None else None


def _age_minutes_is_fresh(age: float | None, maximum: float) -> bool:
    """Accept a small clock skew, but never hide future-dated telemetry."""

    return bool(age is not None and -(5.0 / 60.0) <= age <= maximum)


def _fresh_power_sample(
    hass: HomeAssistant,
    entity_id: str,
    now: datetime,
    *,
    max_age_seconds: float = 120.0,
) -> tuple[float | None, float | None, str]:
    """Return a fresh non-negative live power in kW and diagnostics."""
    sample = numeric_state_sample(
        hass.states.get(entity_id),
        now,
        max_age_seconds=max_age_seconds,
        scale=0.001,
        minimum=0.0,
    )
    if not sample.fresh:
        reason = {
            "below_minimum": "invalid_negative",
            "unavailable": "not_numeric",
        }.get(sample.reason, sample.reason)
        return None, sample.age_seconds, reason
    return sample.value, max(sample.age_seconds or 0.0, 0.0), "live"


def _forecast_total(state: State | None, percentile: str) -> float | None:
    """Read Solcast P10/P50/P90 totals across old and new attribute layouts."""
    if state is None:
        return None
    if percentile == "p50":
        try:
            return max(float(state.state), 0.0)
        except (TypeError, ValueError):
            return None
    suffix = "10" if percentile == "p10" else "90"
    candidates = (f"estimate{suffix}", f"estimate{suffix}_kwh")
    for key in candidates:
        try:
            return max(float(state.attributes[key]), 0.0)
        except (KeyError, TypeError, ValueError):
            pass
    analysis = state.attributes.get("analysis")
    if isinstance(analysis, Mapping):
        for key in candidates:
            try:
                return max(float(analysis[key]), 0.0)
            except (KeyError, TypeError, ValueError):
                pass
    return None


def _detailed_pv_expected_elapsed_kwh(
    state: State | None,
    target_date: date,
    timezone: ZoneInfo,
    now: datetime,
) -> float | None:
    """Return raw P50 energy expected from midnight through ``now``."""
    if state is None:
        return None
    details = state.attributes.get("detailedForecast")
    if not isinstance(details, list):
        details = state.attributes.get("detailed_forecast")
    if not isinstance(details, list):
        return None
    expected = 0.0
    found = False
    for item in details:
        if not isinstance(item, Mapping):
            continue
        start = _parse_datetime(
            item.get("period_start") or item.get("period_start_local"),
            timezone,
        )
        if start is None or start.date() != target_date or start >= now:
            continue
        raw_power = (
            item.get("pv_estimate")
            if item.get("pv_estimate") is not None
            else item.get("estimate")
        )
        try:
            power_kw = max(float(raw_power), 0.0)
        except (TypeError, ValueError):
            continue
        fraction = min(
            max((now - start).total_seconds() / (30 * 60), 0.0),
            1.0,
        )
        expected += power_kw * 0.5 * fraction
        found = True
    return expected if found else None


def _detailed_pv_map(
    state: State | None,
    target_date: date,
    target_kwh: float,
    timezone: ZoneInfo,
    now_slot: datetime,
    *,
    percentile: str = "p50",
) -> dict[datetime, float]:
    if state is None or target_kwh <= 0:
        return {}
    details = state.attributes.get("detailedForecast")
    if not isinstance(details, list):
        details = state.attributes.get("detailed_forecast")
    if not isinstance(details, list):
        return {}
    values: dict[datetime, float] = {}
    for item in details:
        if not isinstance(item, Mapping):
            continue
        start = _parse_datetime(
            item.get("period_start") or item.get("period_start_local"),
            timezone,
        )
        if start is None or start.date() != target_date:
            continue
        start = floor_half_hour(start)
        if start < now_slot:
            continue
        if percentile == "p10":
            raw_power = (
                item.get("pv_estimate10")
                if item.get("pv_estimate10") is not None
                else item.get("estimate10")
            )
        elif percentile == "p90":
            raw_power = (
                item.get("pv_estimate90")
                if item.get("pv_estimate90") is not None
                else item.get("estimate90")
            )
        else:
            raw_power = (
                item.get("pv_estimate")
                if item.get("pv_estimate") is not None
                else item.get("estimate")
            )
        try:
            energy = max(float(raw_power), 0.0) * 0.5
        except (TypeError, ValueError):
            continue
        values[start] = values.get(start, 0.0) + energy
    total = sum(values.values())
    if total <= 0:
        return {}
    scale = target_kwh / total
    return {start: energy * scale for start, energy in values.items()}


def _blend_pv_maps(
    expected: Mapping[datetime, float],
    low: Mapping[datetime, float],
    risk_weight: float,
) -> dict[datetime, float]:
    """Blend P10/P50 maps while retaining slots absent from either series."""
    return {
        start: blend_low_expected(
            float(low.get(start, expected.get(start, 0.0))),
            float(expected.get(start, 0.0)),
            risk_weight,
        )
        for start in set(expected) | set(low)
    }


def _fallback_pv_map(
    target_date: date,
    target_kwh: float,
    timezone: ZoneInfo,
    now_slot: datetime,
    sunrise_minute: int,
    sunset_minute: int,
) -> dict[datetime, float]:
    if target_kwh <= 0:
        return {}
    starts: list[datetime] = []
    cursor = datetime.combine(target_date, time.min, tzinfo=timezone)
    for _ in range(48):
        minute = cursor.hour * 60 + cursor.minute
        if (
            cursor >= now_slot
            and sunrise_minute <= minute < sunset_minute
        ):
            starts.append(cursor)
        cursor += timedelta(minutes=30)
    if not starts:
        return {}
    per_slot = target_kwh / len(starts)
    return {start: per_slot for start in starts}


def _empty_load_summary() -> LoadHistorySummary:
    return LoadHistorySummary(
        average_daily_kwh=None,
        daily_history_days=0,
        daily_energy_kwh={},
        average_night_kwh=None,
        night_history_days=0,
        night_energy_kwh={},
        current_day_energy_kwh=None,
    )


class HoymilesRCEOptimizerSensor(SensorEntity):
    """Calculate a two-day, home-first RCE export plan."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "rce_optimized_plan"
    _attr_icon = "mdi:chart-timeline-variant"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: RuntimeData,
    ) -> None:
        """Initialize the optimizer sensor."""
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._attr_unique_id = f"{entry.entry_id}_rce_optimized_plan"
        self._result: OptimizerResult | None = None
        self._load_history = _empty_load_summary()
        self._extended_load_history = _empty_load_summary()
        self._full_history_refresh_date: date | None = None
        self._load_profile_generated_at: datetime | None = None
        self._history_refresh_running = False
        self._forecast_accuracy_factor = 0.90
        self._forecast_accuracy_uncertainty = 0.15
        self._forecast_accuracy_days = 0
        self._forecast_accuracy_source = "automatic_conservative_fallback"
        self._forecast_refresh_running = False
        self._forecast_refresh_date: date | None = None
        self._startup_warmup_task: asyncio.Task[None] | None = None
        self._forecast_policy_refresh_task: asyncio.Task[None] | None = None
        self._recalculate_cancel = None
        self._dynamic_forecast_entities: frozenset[str] = frozenset()
        self._dynamic_forecast_unsub = None
        self._forecast_gcf_policy_signature: (
            tuple[bool, str, str | None, float | None] | None
        ) = None
        self._forecast_gcf_optimizer_signature: (
            tuple[
                bool,
                str,
                str | None,
                float | None,
                float | None,
                float | None,
            ]
            | None
        ) = None
        self._forecast_gcf_policy_evaluation_cancel = None
        self._delayed_recalculate_tasks: set[asyncio.Task[None]] = set()
        self._lifecycle_stopped = False
        self._optimizer_lock = asyncio.Lock()
        self._input_revision = OptimizerInputRevision()
        self._current_slot_continue_eligible: bool | None = None
        self._current_slot_continue_changed_at: datetime | None = None
        self._tariff_plan_source: SensorEntity | None = None
        self._timeline_sensor: Any | None = None
        self._timeline_metadata: dict[str, Any] = {}
        self._attributes: dict[str, Any] = {
            "status_code": "missing_data",
            "missing_entities": [],
            "planned_slots": [],
            "result_current": False,
            "recalculation_pending": True,
            "input_revision": 0,
        }

    def attach_timeline_sensor(self, timeline_sensor: Any) -> None:
        """Bind the one entry-local observation-only timeline publisher."""

        if self._timeline_sensor is not None and self._timeline_sensor is not timeline_sensor:
            raise RuntimeError("RCE timeline sensor already attached")
        self._timeline_sensor = timeline_sensor

    def attach_tariff_plan_source(self, tariff_plan_source: SensorEntity) -> None:
        """Bind the exact same-entry tariff price broker."""

        source_entry = getattr(tariff_plan_source, "_entry", None)
        if source_entry is None or source_entry.entry_id != self._entry.entry_id:
            raise RuntimeError("RCE tariff broker belongs to another entry")
        self._tariff_plan_source = tariff_plan_source

    def attach_tariff_plan_listener(self) -> None:
        """Observe a suffixed same-entry tariff entity without guessing its ID."""

        source = getattr(self, "_tariff_plan_source", None)
        entity_id = getattr(source, "entity_id", None)
        if not isinstance(entity_id, str) or entity_id in RCE_EVENT_DRIVEN_ENTITIES:
            return
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                (entity_id,),
                self._async_input_changed,
            )
        )

    def _same_entry_tariff_plan_state(self) -> State | None:
        """Return only the exact tariff broker object wired for this entry."""

        source = getattr(self, "_tariff_plan_source", None)
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

    @callback
    def _publish_timeline_result(self) -> None:
        """Publish only the result committed for this exact input revision."""

        if self._timeline_sensor is None:
            return
        if self._result is None:
            self._timeline_sensor.publish_unavailable(
                input_revision=self._input_revision.value,
                blocker_code=str(
                    self._attributes.get("status_code", "missing_data")
                ),
            )
            return
        quality = (
            "partial"
            if self._timeline_metadata.get("planning_scope") == "today_only"
            else "complete"
        )
        self._timeline_sensor.publish_current(
            self._result.timeline_trace,
            input_revision=self._input_revision.value,
            metadata=self._timeline_metadata,
            quality=quality,
        )

    @property
    def suggested_object_id(self) -> str:
        """Return a stable entity id."""
        return "hoymiles_hit_rce_optimized_plan"

    @property
    def device_info(self) -> DeviceInfo:
        """Attach the optimizer to the localized inverter device."""
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
        """Return a localized plan state."""
        language = "pl" if self.hass.config.language.startswith("pl") else "en"
        code = str(self._attributes.get("status_code", "missing_data"))
        text = STATUS_TEXT[language].get(
            code,
            STATUS_TEXT[language]["optimizer_error"],
        )
        if (
            self._attributes.get("planning_scope") == "today_only"
            and code in {"ready", "waiting_for_market", "home_protected"}
        ):
            return f"{text} — {TODAY_ONLY_SUFFIX[language]}"
        return text

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return plan diagnostics used by the dashboard and automations."""
        return self._attributes

    async def async_added_to_hass(self) -> None:
        """Track every input that can change the plan."""
        await super().async_added_to_hass()
        self._lifecycle_stopped = False
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                sorted(RCE_EVENT_DRIVEN_ENTITIES),
                self._async_input_changed,
            )
        )
        current_policy, current_diagnostics = _forecast_learning_policy_snapshot(
            self.hass,
            dt_util.now(),
        )
        self._forecast_gcf_policy_signature = (
            _forecast_learning_policy_signature(current_policy)
        )
        self._forecast_gcf_optimizer_signature = (
            *_forecast_learning_policy_signature(current_policy),
            current_diagnostics.get("forecast_learning_gcf_enable_code"),
            current_diagnostics.get(
                "forecast_learning_gcf_export_limit_percent"
            ),
        )
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                FORECAST_GCF_POLICY_TRIGGER_ENTITIES,
                self._async_forecast_gcf_policy_changed,
            )
        )
        self.async_on_remove(
            async_track_state_report_event(
                self.hass,
                FORECAST_GCF_COHORT_REPORT_ENTITIES,
                self._async_forecast_gcf_policy_changed,
            )
        )
        self.async_on_remove(lambda: self._clear_forecast_gcf_cohort_state())
        self.async_on_remove(self._cancel_delayed_recalculation)
        refresh_dynamic_forecast_listener = getattr(
            self,
            "_refresh_dynamic_forecast_listener",
            None,
        )
        if callable(refresh_dynamic_forecast_listener):
            refresh_dynamic_forecast_listener()
        self.async_on_remove(
            lambda: self._remove_dynamic_forecast_listener()
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_timer,
                timedelta(minutes=1),
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_history_timer,
                timedelta(hours=1),
            )
        )
        self.async_write_ha_state()
        self._schedule_startup_warmup()

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
            "hoymiles RCE optimizer startup warmup",
        )
        self._startup_warmup_task = task
        self.async_on_remove(task.cancel)

    async def _async_startup_warmup(self) -> None:
        """Warm Recorder models after the fail-closed entity is registered."""
        try:
            await self._async_refresh_load_history(force_full=True)
            await self._async_refresh_forecast_accuracy(force=True)
            self._invalidate_internal_inputs()
            await self._recalculate_and_write()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - retain the fail-closed initial state
            _LOGGER.exception("Cannot complete the RCE optimizer startup warmup")

    async def _async_history_timer(self, now: datetime) -> None:
        """Refresh recorder-backed LOAD history once per hour."""
        await self._async_refresh_load_history()
        await self._async_refresh_forecast_accuracy()
        self._invalidate_internal_inputs()
        await self._recalculate_and_write()

    async def _async_refresh_load_history(self, *, force_full: bool = False) -> None:
        """Refresh LOAD history without scanning 28 raw days every hour."""
        if self._history_refresh_running:
            return
        self._history_refresh_running = True
        try:
            timezone = ZoneInfo(self.hass.config.time_zone)
            now = datetime.now(timezone)
            full_refresh = (
                force_full or self._full_history_refresh_date != now.date()
            )
            local_start = datetime.combine(
                now.date() - timedelta(days=31 if full_refresh else 0),
                time.min,
                tzinfo=timezone,
            )
            raw_history = await async_get_bounded_state_reports(
                self.hass,
                dt_util.as_utc(local_start),
                dt_util.as_utc(now),
                LOAD_PHASE_ENERGY_ENTITIES,
            )

            samples: dict[str, list[tuple[datetime, float]]] = {
                entity_id: [] for entity_id in LOAD_PHASE_ENERGY_ENTITIES
            }
            for entity_id in LOAD_PHASE_ENERGY_ENTITIES:
                for item in raw_history.get(entity_id, []):
                    state_value = getattr(item, "state", None)
                    updated = getattr(item, "last_updated", None)
                    if state_value is None or updated is None:
                        continue
                    try:
                        numeric = float(state_value)
                    except (TypeError, ValueError):
                        continue
                    samples[entity_id].append(
                        (updated.astimezone(timezone), max(numeric, 0.0))
                    )

            night_windows: dict[date, tuple[datetime, datetime]] = {}
            for offset in range(29 if full_refresh else 0, 0, -1):
                night_date = now.date() - timedelta(days=offset)
                sunset = get_astral_event_date(
                    self.hass,
                    "sunset",
                    night_date,
                )
                sunrise = get_astral_event_date(
                    self.hass,
                    "sunrise",
                    night_date + timedelta(days=1),
                )
                if sunset is None or sunrise is None:
                    continue
                night_windows[night_date] = (
                    sunset.astimezone(timezone) - timedelta(minutes=90),
                    sunrise.astimezone(timezone) + timedelta(minutes=90),
                )

            current_day_window: tuple[datetime, datetime] | None = None
            today_sunrise = get_astral_event_date(
                self.hass,
                "sunrise",
                now.date(),
            )
            today_sunset = get_astral_event_date(
                self.hass,
                "sunset",
                now.date(),
            )
            if today_sunrise is not None and today_sunset is not None:
                current_day_start = (
                    today_sunrise.astimezone(timezone)
                    + timedelta(minutes=90)
                )
                current_day_end = min(
                    now,
                    today_sunset.astimezone(timezone)
                    - timedelta(minutes=90),
                )
                if current_day_end > current_day_start:
                    current_day_window = (
                        current_day_start,
                        current_day_end,
                    )

            if full_refresh:
                self._load_history = summarize_load_history(
                    samples,
                    now=now,
                    night_windows=night_windows,
                    current_day_window=current_day_window,
                    history_days=4,
                )
                self._extended_load_history = summarize_load_history(
                    samples,
                    now=now,
                    night_windows=night_windows,
                    current_day_window=current_day_window,
                    history_days=28,
                )
                self._full_history_refresh_date = now.date()
                self._load_profile_generated_at = now
            else:
                current = summarize_load_history(
                    samples,
                    now=now,
                    night_windows={},
                    current_day_window=current_day_window,
                    history_days=0,
                )
                self._load_history = replace(
                    self._load_history,
                    current_day_energy_kwh=current.current_day_energy_kwh,
                )
                self._extended_load_history = replace(
                    self._extended_load_history,
                    current_day_energy_kwh=current.current_day_energy_kwh,
                )
        except Exception:  # noqa: BLE001 - recorder outages need a safe fallback
            _LOGGER.exception("Cannot rebuild recorder-backed LOAD history")
        finally:
            self._history_refresh_running = False

    async def _async_refresh_forecast_accuracy(
        self,
        *,
        force: bool = False,
    ) -> None:
        """Learn a robust conservative Solcast factor once per local day."""
        if self._forecast_refresh_running:
            return
        timezone = ZoneInfo(self.hass.config.time_zone)
        now = dt_util.now().astimezone(timezone)
        if not force and self._forecast_refresh_date == now.date():
            return
        self._forecast_refresh_running = True
        try:
            learning_policy, _ = _forecast_learning_policy_snapshot(
                self.hass,
                now,
            )
            policy_signature = _forecast_learning_policy_signature(
                learning_policy
            )
            if not learning_policy.enabled:
                if learning_policy.mode == "fixed_zero_export":
                    self._forecast_refresh_date = now.date()
                return
            configured = _state_text(
                self.hass,
                TODAY_FORECAST_ENTITY_HELPER,
            )
            forecast_entity, forecast_state = _first_numeric_state(
                self.hass,
                TODAY_FORECAST_CANDIDATES,
                configured,
            )
            if not forecast_entity or forecast_state is None:
                return
            actual_entity = "sensor.hoymiles_hit_pv_total_energy_today"
            start = now - timedelta(days=15)
            raw = await async_get_bounded_state_reports(
                self.hass,
                dt_util.as_utc(start),
                dt_util.as_utc(now),
                (
                    forecast_entity,
                    actual_entity,
                    FORECAST_EXPORT_ALLOWED_ENTITY,
                    FORECAST_EMS_PACKAGE_VERSION_ENTITY,
                ),
            )
            forecast_by_day: dict[date, list[float]] = {}
            actual_by_day: dict[date, list[float]] = {}
            for entity_id, destination in (
                (forecast_entity, forecast_by_day),
                (actual_entity, actual_by_day),
            ):
                for item in raw.get(entity_id, []):
                    updated = getattr(item, "last_updated", None)
                    value = getattr(item, "state", None)
                    if updated is None or value is None:
                        continue
                    try:
                        numeric = max(float(value), 0.0)
                    except (TypeError, ValueError):
                        continue
                    local_day = updated.astimezone(timezone).date()
                    if local_day >= now.date():
                        continue
                    destination.setdefault(local_day, []).append(numeric)

            export_allowed_history = [
                (item.last_updated, item.state)
                for item in raw.get(FORECAST_EXPORT_ALLOWED_ENTITY, [])
                if getattr(item, "last_updated", None) is not None
            ]
            package_version_history = [
                (item.last_updated, item.state)
                for item in raw.get(FORECAST_EMS_PACKAGE_VERSION_ENTITY, [])
                if getattr(item, "last_updated", None) is not None
            ]

            samples: list[tuple[float, float]] = []
            for day in sorted(set(forecast_by_day) & set(actual_by_day)):
                day_start = datetime.combine(day, time.min, tzinfo=timezone)
                day_end = datetime.combine(
                    day + timedelta(days=1),
                    time.min,
                    tzinfo=timezone,
                )
                if not forecast_learning_history_day_eligible(
                    export_allowed_history,
                    package_version_history,
                    day_start=day_start,
                    day_end=day_end,
                ):
                    continue
                forecasts = [value for value in forecast_by_day[day] if value > 0.5]
                actuals = actual_by_day[day]
                if not forecasts or not actuals:
                    continue
                # The median is robust to several intraday Solcast refreshes.
                ordered = sorted(forecasts)
                forecast = ordered[len(ordered) // 2]
                actual = max(actuals)
                if actual <= 0.5:
                    continue
                age_days = float((now.date() - day).days)
                samples.append((age_days, actual / forecast))

            current_policy, _ = _forecast_learning_policy_snapshot(
                self.hass,
                dt_util.now().astimezone(timezone),
            )
            if (
                not current_policy.enabled
                or _forecast_learning_policy_signature(current_policy)
                != policy_signature
            ):
                return
            if samples:
                factor, uncertainty, count = robust_weighted_factor(samples)
                self._forecast_accuracy_factor = factor
                self._forecast_accuracy_uncertainty = uncertainty
                self._forecast_accuracy_days = count
                self._forecast_accuracy_source = (
                    "recorder_actual_vs_solcast_robust"
                )
            else:
                # Keep the last factor that was learned only from qualified
                # days, but do not claim that currently retained Recorder rows
                # still support it. A fresh process remains on its 0.90
                # conservative fallback until a complete day is provable.
                self._forecast_accuracy_days = 0
                if self._forecast_accuracy_source in {
                    "recorder_actual_vs_solcast_robust",
                    "retained_last_qualified_factor_no_current_history",
                }:
                    self._forecast_accuracy_source = (
                        "retained_last_qualified_factor_no_current_history"
                    )
            self._forecast_refresh_date = now.date()
        except Exception:  # noqa: BLE001 - safe fallback remains active
            _LOGGER.exception("Cannot learn RCE Solcast forecast accuracy")
        finally:
            self._forecast_refresh_running = False

    @callback
    def _configured_forecast_source_ids(self) -> frozenset[str]:
        """Return configured sources without allowing a self-reference."""
        targets = _configured_forecast_entity_ids(self.hass)
        own_entity_id = getattr(self, "entity_id", None)
        return (
            targets - {own_entity_id}
            if isinstance(own_entity_id, str) and own_entity_id
            else targets
        )

    @callback
    def _refresh_dynamic_forecast_listener(self) -> None:
        """Follow configured forecast sources outside the built-in candidates."""
        targets = frozenset(
            self._configured_forecast_source_ids() - WATCHED_ENTITIES
        )
        if targets == self._dynamic_forecast_entities:
            return
        if self._dynamic_forecast_unsub is not None:
            self._dynamic_forecast_unsub()
            self._dynamic_forecast_unsub = None
        self._dynamic_forecast_entities = targets
        if targets:
            self._dynamic_forecast_unsub = async_track_state_change_event(
                self.hass,
                sorted(targets),
                self._async_input_changed,
            )

    @callback
    def _remove_dynamic_forecast_listener(self) -> None:
        """Release the current dynamic forecast subscription."""
        if self._dynamic_forecast_unsub is not None:
            self._dynamic_forecast_unsub()
            self._dynamic_forecast_unsub = None
        self._dynamic_forecast_entities = frozenset()

    @callback
    def _current_input_fingerprint(self) -> tuple[Any, ...]:
        """Return the exact watched snapshot used to certify publication."""
        tariff_entity_id = getattr(
            getattr(self, "_tariff_plan_source", None),
            "entity_id",
            None,
        )
        watched_entities = WATCHED_ENTITIES | self._configured_forecast_source_ids()
        watched_entities -= {"sensor.hoymiles_hit_tariff_charge_plan"}
        if isinstance(tariff_entity_id, str):
            watched_entities.add(tariff_entity_id)
        watched_entities -= {
            "sensor.hoymiles_hit_gcf_enable_readback_code",
            "sensor.hoymiles_hit_gcf_maximum_export_power_readback",
        }
        fingerprint = optimizer_input_fingerprint(
            self.hass,
            watched_entities,
            attribute_projections=(
                {tariff_entity_id: TARIFF_PRICE_BROKER_ATTRIBUTES}
                if isinstance(tariff_entity_id, str)
                else {}
            ),
        )
        gcf_signature = getattr(self, "_forecast_gcf_optimizer_signature", None)
        if gcf_signature is None:
            return fingerprint
        return (*fingerprint, ("__rce_gcf_optimizer__", gcf_signature))

    @callback
    def _invalidate_input_event(
        self,
        event: Event[EventStateChangedData],
    ) -> bool:
        """Invalidate only when a value consumed by this optimizer changed."""
        entity_id = event.data["entity_id"]
        counterpart = entity_id == getattr(
            getattr(self, "_tariff_plan_source", None),
            "entity_id",
            None,
        )
        changed = self._input_revision.invalidate_state_change(
            event.data.get("old_state"),
            event.data.get("new_state"),
            attributes=(TARIFF_PRICE_BROKER_ATTRIBUTES if counterpart else None),
            include_state=not counterpart,
            include_last_updated=not counterpart,
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
        """Withdraw execution authority once, without publication ping-pong."""
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

    @callback
    def _async_input_changed(
        self,
        event: Event[EventStateChangedData],
    ) -> None:
        """Coalesce fast ESPHome updates into one optimizer refresh."""
        if event.data["entity_id"] in FORECAST_ENTITY_HELPERS:
            self._refresh_dynamic_forecast_listener()
        if not self._invalidate_input_event(event):
            return
        # Leading-edge coalescing bounds the fail-closed pending interval even
        # when ESPHome telemetry changes continuously. A solver already in
        # flight observes the revision and immediately retries the latest
        # snapshot under the same single-flight lock.
        if self._recalculate_cancel is None:
            self._recalculate_cancel = async_call_later(
                self.hass,
                INPUT_RECALCULATION_DELAY_SECONDS,
                self._async_debounced_recalculate,
            )

    @callback
    def _async_forecast_gcf_policy_changed(
        self,
        event: Event[Any],
    ) -> None:
        """Accept a complete cohort or arm one bounded fail-closed deadline."""

        if self._lifecycle_stopped:
            return
        policy, diagnostics = _forecast_learning_policy_snapshot(
            self.hass,
            dt_util.now(),
        )
        if policy.excluded_reason != "gcf_readback_incoherent":
            self._cancel_forecast_gcf_policy_evaluation()
            # A completed HA delivery cohort with the same physical policy is
            # not a new optimizer input. A changed signature still invalidates
            # immediately in _apply_forecast_gcf_policy().
            self._apply_forecast_gcf_policy(policy, diagnostics)
            return
        if (
            self._forecast_gcf_policy_signature
            == _forecast_learning_policy_signature(policy)
        ):
            # The previous bounded deadline already failed closed for this
            # semantic condition. Repeated incoherent reports cannot create a
            # new timer/recalculation loop while the source remains invalid.
            return
        if self._forecast_gcf_policy_evaluation_cancel is None:
            # Keep the last coherent, fresh generation while HA finishes
            # delivering this physical readback cohort. The bounded deadline
            # below still fails closed if the cohort remains incoherent.
            self._forecast_gcf_policy_evaluation_cancel = async_call_later(
                self.hass,
                _FORECAST_GCF_READBACK_MAX_SKEW_SECONDS,
                self._async_evaluate_forecast_gcf_policy,
            )

    @callback
    def _cancel_forecast_gcf_policy_evaluation(self) -> None:
        """Cancel one pending event-order coalescing callback."""

        if self._forecast_gcf_policy_evaluation_cancel is not None:
            self._forecast_gcf_policy_evaluation_cancel()
            self._forecast_gcf_policy_evaluation_cancel = None

    @callback
    def _clear_forecast_gcf_cohort_state(self) -> None:
        """Drop all retained cohort authority when this entity is removed."""

        self._lifecycle_stopped = True
        self._cancel_forecast_gcf_policy_evaluation()
        self._forecast_gcf_policy_signature = None
        self._forecast_gcf_optimizer_signature = None

    async def _async_evaluate_forecast_gcf_policy(
        self,
        now: datetime,
    ) -> None:
        """Replan only when a completed GCF read changes learning policy."""

        self._forecast_gcf_policy_evaluation_cancel = None
        if self._lifecycle_stopped:
            return
        policy, diagnostics = _forecast_learning_policy_snapshot(
            self.hass,
            now,
        )
        self._apply_forecast_gcf_policy(
            policy,
            diagnostics,
            force_recalculate=True,
        )

    @callback
    def _apply_forecast_gcf_policy(
        self,
        policy: ForecastLearningPolicy,
        diagnostics: Mapping[str, Any],
        *,
        force_recalculate: bool = False,
    ) -> None:
        """Apply one complete or deadline-expired physical GCF policy."""

        if self._lifecycle_stopped:
            return
        policy_signature = _forecast_learning_policy_signature(policy)
        optimizer_signature = _forecast_gcf_optimizer_signature(
            policy,
            diagnostics,
        )
        previous_signature = self._forecast_gcf_policy_signature
        previous_optimizer_signature = self._forecast_gcf_optimizer_signature
        policy_changed = policy_signature != previous_signature
        optimizer_input_changed = (
            optimizer_signature != previous_optimizer_signature
        )
        if not optimizer_input_changed and not force_recalculate:
            return
        self._forecast_gcf_policy_signature = policy_signature
        self._forecast_gcf_optimizer_signature = optimizer_signature
        if policy_changed and policy.enabled and (
            previous_signature is None
            or not previous_signature[0]
        ):
            self._schedule_forecast_policy_refresh()
        self._invalidate_internal_inputs()
        if not self._lifecycle_stopped and self._recalculate_cancel is None:
            self._recalculate_cancel = async_call_later(
                self.hass,
                INPUT_RECALCULATION_DELAY_SECONDS,
                self._async_debounced_recalculate,
            )

    @callback
    def _schedule_forecast_policy_refresh(self) -> None:
        """Start one cancel-safe Recorder rebuild after GCF policy recovery."""

        if (
            self._forecast_policy_refresh_task is not None
            and not self._forecast_policy_refresh_task.done()
        ):
            return
        task = self._entry.async_create_background_task(
            self.hass,
            self._async_refresh_forecast_after_policy_restore(),
            "hoymiles RCE forecast learning policy refresh",
        )
        self._forecast_policy_refresh_task = task
        self.async_on_remove(task.cancel)

    async def _async_refresh_forecast_after_policy_restore(self) -> None:
        """Rebuild and publish the adaptive model after GCF becomes usable."""

        await self._async_refresh_forecast_accuracy(force=True)
        self._invalidate_internal_inputs()
        await self._recalculate_and_write()

    async def _async_debounced_recalculate(self, now: datetime) -> None:
        self._recalculate_cancel = None
        if self._lifecycle_stopped:
            return
        task = asyncio.current_task()
        if task is None:
            return
        self._delayed_recalculate_tasks.add(task)
        try:
            await self._recalculate_and_write()
        finally:
            self._delayed_recalculate_tasks.discard(task)

    @callback
    def _cancel_delayed_recalculation(self) -> None:
        """Cancel queued or running delayed work when the entity is removed."""

        self._lifecycle_stopped = True
        if self._recalculate_cancel is not None:
            self._recalculate_cancel()
            self._recalculate_cancel = None
        tasks = tuple(self._delayed_recalculate_tasks)
        self._delayed_recalculate_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

    async def _async_timer(self, now: datetime) -> None:
        """Refresh the active slot and rolling forecast every minute."""
        if self._recalculate_cancel is not None:
            self._recalculate_cancel()
            self._recalculate_cancel = None
        self._invalidate_internal_inputs()
        await self._recalculate_and_write()

    async def _recalculate_and_write(self) -> None:
        """Write to HA only when the material plan state changed."""
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
                if self._recalculate_cancel is not None:
                    self._recalculate_cancel()
                    self._recalculate_cancel = None
            if (
                previous_state != self.native_value
                or previous_attributes != self._attributes
            ):
                self.async_write_ha_state()
            if committed:
                self._publish_timeline_result()

    async def _recalculate(self) -> None:
        """Serialize startup and event-driven optimizer runs."""
        async with self._optimizer_lock:
            for _attempt in range(MAX_IMMEDIATE_RECALCULATIONS):
                if await self._recalculate_locked():
                    self._mark_result_current()
                    self._publish_timeline_result()
                    return

    async def _recalculate_locked(self) -> bool:
        if self._forecast_gcf_policy_evaluation_cancel is not None:
            # The current 258/259/generation reports are still a mixed HA
            # delivery cohort. Keep the previous coherent plan current until
            # the late report closes it or the bounded deadline fails closed.
            return False
        captured_revision = self._input_revision.value
        captured_fingerprint = self._current_input_fingerprint()
        try:
            settings, metadata = self._optimizer_input()
            if settings is None:
                self._result = None
                self._timeline_metadata = dict(metadata)
                self._attributes = {
                    "status_code": "missing_data",
                    "missing_entities": metadata["missing_entities"],
                    "planned_slots": [],
                    **metadata,
                }
                return True
            result = await self.hass.async_add_executor_job(optimize_rce, settings)
            if (
                not self._input_revision.is_current(captured_revision)
                or captured_fingerprint != self._current_input_fingerprint()
            ):
                self._mark_recalculation_pending()
                return False
            self._result = result
            self._timeline_metadata = dict(metadata)
            now = dt_util.now().astimezone(ZoneInfo(self.hass.config.time_zone))
            current_slot_continue_eligible = bool(
                result.current_slot_planned_export_kwh >= 0.01
                and result.current_slot_execution_power_percent > 0.0
            )
            if (
                self._current_slot_continue_eligible
                is not current_slot_continue_eligible
            ):
                self._current_slot_continue_eligible = (
                    current_slot_continue_eligible
                )
                self._current_slot_continue_changed_at = now
            current_slot_continue_stable_seconds = max(
                (
                    now - self._current_slot_continue_changed_at
                ).total_seconds(),
                0.0,
            ) if self._current_slot_continue_changed_at is not None else 0.0
            if current_slot_continue_eligible:
                current_slot_continue_reason = "eligible"
            elif result.current_slot_planned_export_kwh < 0.01:
                current_slot_continue_reason = "slot_no_longer_selected"
            else:
                current_slot_continue_reason = "execution_power_unavailable"
            planned_slots = [
                {
                    "date": item.start.date().isoformat(),
                    "start": item.start.strftime("%H:%M"),
                    "end": (item.start + timedelta(minutes=30)).strftime("%H:%M"),
                    "price": round(item.price_pln_kwh, 4),
                    "energy": round(item.energy_kwh, 2),
                    "revenue": round(item.revenue_pln, 2),
                }
                for item in result.planned_exports
            ]
            self._attributes = {
                "status_code": result.status_code,
                "missing_entities": [],
                "minimum_soc": result.minimum_soc_percent,
                "base_reserve_energy_kwh": round(
                    result.base_reserve_energy_kwh,
                    2,
                ),
                "protected_night_energy_kwh": round(
                    result.protected_night_energy_kwh,
                    2,
                ),
                "additional_forecast_reserve_kwh": round(
                    result.additional_forecast_reserve_kwh,
                    2,
                ),
                "protected_home_energy_kwh": round(
                    result.protected_home_energy_kwh,
                    2,
                ),
                "available_energy_now_kwh": round(
                    result.available_energy_now_kwh,
                    2,
                ),
                "current_battery_energy_kwh": round(
                    settings.battery_capacity_kwh
                    * settings.battery_soc_percent
                    / 100.0,
                    2,
                ),
                "planned_export_kwh": round(result.planned_export_kwh, 2),
                "planned_revenue_pln": round(result.planned_revenue_pln, 2),
                "automatic_price_floor_pln_kwh": (
                    round(result.automatic_price_floor_pln_kwh, 4)
                    if result.automatic_price_floor_pln_kwh is not None
                    else None
                ),
                "highest_planned_price_pln_kwh": (
                    round(
                        max(
                            item.price_pln_kwh
                            for item in result.planned_exports
                        ),
                        4,
                    )
                    if result.planned_exports
                    else None
                ),
                "natural_pv_export_kwh": round(result.natural_export_kwh, 2),
                "natural_pv_revenue_pln": round(
                    result.natural_revenue_pln,
                    2,
                ),
                "expected_total_export_kwh": round(result.total_export_kwh, 2),
                "estimated_revenue_pln": round(result.total_revenue_pln, 2),
                "uncontrolled_export_kwh": round(
                    result.uncontrolled_export_kwh,
                    2,
                ),
                "uncontrolled_revenue_pln": round(
                    result.uncontrolled_revenue_pln,
                    2,
                ),
                "optimization_gain_pln": round(
                    result.optimization_gain_pln,
                    2,
                ),
                "gross_optimization_gain_pln": round(
                    result.gross_optimization_gain_pln,
                    2,
                ),
                "gross_optimization_gain_basis": (
                    "optimized_market_revenue_minus_uncontrolled_market_revenue"
                ),
                "optimization_gain_basis": "gross_revenue_uplift",
                "optimization_gain_legacy_alias": (
                    "gross_optimization_gain_pln"
                ),
                "ending_battery_kwh": round(result.ending_battery_kwh, 2),
                "ending_battery_soc": round(
                    result.ending_battery_kwh
                    / max(settings.battery_capacity_kwh, 0.001)
                    * 100.0,
                    1,
                ),
                "system_power_kw": round(result.system_power_kw, 2),
                "requested_export_power_kw": round(
                    result.requested_export_power_kw,
                    2,
                ),
                "bms_discharge_power_limit_kw": (
                    round(result.bms_discharge_power_limit_kw, 2)
                    if result.bms_discharge_power_limit_kw is not None
                    else None
                ),
                "bms_discharge_limit_percent": (
                    round(result.bms_discharge_limit_percent, 1)
                    if result.bms_discharge_limit_percent is not None
                    else None
                ),
                "bms_limit_active": result.bms_limit_active,
                "bms_discharge_data_fresh": (
                    result.bms_discharge_data_fresh
                ),
                "bms_discharge_data_age_seconds": (
                    round(result.bms_discharge_data_age_seconds, 1)
                    if result.bms_discharge_data_age_seconds is not None
                    else None
                ),
                "bms_discharge_data_available": (
                    result.bms_discharge_data_available
                ),
                "bms_charge_power_limit_kw": round(
                    result.bms_charge_power_limit_kw,
                    3,
                ),
                "bms_charge_data_fresh": result.bms_charge_data_fresh,
                "bms_charge_data_age_seconds": (
                    round(result.bms_charge_data_age_seconds, 1)
                    if result.bms_charge_data_age_seconds is not None
                    else None
                ),
                "bms_charge_data_available": (
                    result.bms_charge_data_available
                ),
                "maximum_export_power_kw": round(
                    result.maximum_export_power_kw,
                    2,
                ),
                "export_power_cap_kw": (
                    round(result.export_power_cap_kw, 2)
                    if result.export_power_cap_kw is not None
                    else None
                ),
                "effective_export_power_kw": (
                    round(result.effective_export_power_kw, 2)
                    if result.effective_export_power_kw is not None
                    else None
                ),
                "physical_limit_source": result.physical_limit_source,
                "load_profile_mode": result.load_profile_mode,
                "conservative_daily_load_kwh": (
                    round(result.conservative_daily_load_kwh, 2)
                    if result.conservative_daily_load_kwh is not None
                    else None
                ),
                "conservative_night_load_kwh": (
                    round(result.conservative_night_load_kwh, 2)
                    if result.conservative_night_load_kwh is not None
                    else None
                ),
                "load_risk_multiplier": round(
                    result.load_risk_multiplier,
                    4,
                ),
                "load_risk_buffer_kwh": round(
                    result.load_risk_buffer_kwh,
                    2,
                ),
                "load_risk_mode": result.load_risk_mode,
                "critical_zero_pv_guard_active": (
                    result.critical_zero_pv_guard_active
                ),
                "critical_zero_pv_guard_reason": (
                    result.critical_zero_pv_guard_reason
                ),
                "critical_zero_pv_guard_until": (
                    result.critical_zero_pv_guard_until.isoformat()
                    if result.critical_zero_pv_guard_until is not None
                    else None
                ),
                "critical_zero_pv_guarded_kwh": round(
                    result.critical_zero_pv_guarded_kwh,
                    2,
                ),
                "forecast_confidence_percent": round(
                    result.forecast_confidence_percent,
                    1,
                ),
                "battery_wear_cost_pln": round(
                    result.battery_wear_cost_pln,
                    2,
                ),
                "control_reserve_energy_kwh": round(
                    result.control_reserve_energy_kwh,
                    2,
                ),
                "soc_quantization_reserve_kwh": round(
                    result.soc_quantization_reserve_kwh,
                    2,
                ),
                "day3_forecast_available": result.day3_forecast_available,
                "day3_forecast_kwh": (
                    round(result.day3_forecast_kwh, 2)
                    if result.day3_forecast_kwh is not None
                    else None
                ),
                "day3_load_requirement_kwh": round(
                    result.day3_load_requirement_kwh,
                    2,
                ),
                "day3_energy_shortfall_kwh": round(
                    result.day3_energy_shortfall_kwh,
                    2,
                ),
                "terminal_reserve_reason": result.terminal_reserve_reason,
                "terminal_energy_target_reason": result.terminal_reserve_reason,
                "terminal_energy_target_kwh": round(
                    result.terminal_energy_target_kwh,
                    2,
                ),
                "terminal_energy_value_pln_kwh": round(
                    result.terminal_energy_value_pln_kwh,
                    4,
                ),
                "terminal_energy_value_pln": round(
                    result.terminal_energy_value_pln,
                    2,
                ),
                "baseline_terminal_energy_value_pln": round(
                    result.baseline_terminal_energy_value_pln,
                    2,
                ),
                "terminal_energy_value_delta_pln": round(
                    result.terminal_energy_value_delta_pln,
                    2,
                ),
                "terminal_energy_value_applied_to_objective": (
                    result.terminal_energy_value_applied_to_objective
                ),
                "net_objective_pln": round(result.net_objective_pln, 2),
                "solver_method": result.solver_method,
                "optimality_verified": result.optimality_verified,
                "solver_runtime_ms": round(result.solver_runtime_ms, 2),
                "net_optimization_gain_pln": round(
                    result.net_optimization_gain_pln,
                    2,
                ),
                "net_optimization_gain_basis": (
                    "gross_gain_minus_battery_wear"
                ),
                "historical_day_load_kwh": round(
                    result.historical_day_load_kwh,
                    2,
                ),
                "live_projected_day_load_kwh": round(
                    result.live_projected_day_load_kwh,
                    2,
                ),
                "modeled_day_load_kwh": round(
                    result.modeled_day_load_kwh,
                    2,
                ),
                "daylight_progress_percent": round(
                    result.daylight_progress_percent,
                    1,
                ),
                "current_slot_planned": (
                    result.current_slot_planned_export_kwh >= 0.01
                ),
                "current_slot_end": (
                    result.current_slot_end.isoformat()
                    if result.current_slot_end is not None
                    else None
                ),
                "current_run_end": (
                    result.current_run_end.isoformat()
                    if result.current_run_end is not None
                    else None
                ),
                "current_slot_remaining_minutes": round(
                    result.current_slot_remaining_minutes,
                    2,
                ),
                "current_slot_fraction": round(
                    result.current_slot_fraction,
                    6,
                ),
                "current_slot_planned_export_kwh": round(
                    result.current_slot_planned_export_kwh,
                    3,
                ),
                "current_slot_execution_export_power_kw": round(
                    result.current_slot_execution_export_power_kw,
                    3,
                ),
                "current_slot_execution_discharge_power_kw": round(
                    result.current_slot_execution_discharge_power_kw,
                    3,
                ),
                "current_slot_execution_power_percent": round(
                    result.current_slot_execution_power_percent,
                    3,
                ),
                "current_slot_start_eligible": (
                    result.current_slot_start_eligible
                ),
                "current_slot_continue_eligible": (
                    current_slot_continue_eligible
                ),
                "current_slot_continue_reason": (
                    current_slot_continue_reason
                ),
                "current_slot_continue_stable_seconds": round(
                    current_slot_continue_stable_seconds,
                    1,
                ),
                "current_slot_suppression_reason": (
                    result.current_slot_suppression_reason
                ),
                "current_required_minimum_soc_percent": (
                    result.current_required_minimum_soc_percent
                ),
                "current_slot_load_kwh": round(
                    result.current_slot_load_kwh,
                    3,
                ),
                "current_slot_pv_kwh": round(
                    result.current_slot_pv_kwh,
                    3,
                ),
                "current_slot_load_source": result.current_slot_load_source,
                "current_slot_pv_source": result.current_slot_pv_source,
                "current_slot_shared_discharge_limit_kwh": round(
                    result.current_slot_shared_discharge_limit_kwh,
                    3,
                ),
                "planned_slots": planned_slots,
                **metadata,
            }
            return True
        except Exception:  # noqa: BLE001 - fail closed in the automation entity
            if (
                not self._input_revision.is_current(captured_revision)
                or captured_fingerprint != self._current_input_fingerprint()
            ):
                self._mark_recalculation_pending()
                return False
            _LOGGER.exception("Cannot calculate the optimized RCE plan")
            self._result = None
            self._timeline_metadata = {}
            self._attributes = {
                "status_code": "optimizer_error",
                "missing_entities": [],
                "planned_slots": [],
            }
            return True

    def _optimizer_input(
        self,
    ) -> tuple[OptimizerInput | None, dict[str, Any]]:
        timezone = ZoneInfo(self.hass.config.time_zone)
        now = dt_util.now().astimezone(timezone)
        now_slot = floor_half_hour(now)

        bms_current_sample = numeric_state_sample(
            self.hass.states.get(
                "sensor.hoymiles_hit_maximum_discharge_current"
            ),
            now,
            max_age_seconds=300.0,
            minimum=0.0,
        )
        bms_charge_current_sample = numeric_state_sample(
            self.hass.states.get("sensor.hoymiles_hit_maximum_charge_current"),
            now,
            max_age_seconds=300.0,
            minimum=0.0,
        )
        bms_voltage_sample = numeric_state_sample(
            self.hass.states.get("sensor.hoymiles_hit_battery_voltage_bms"),
            now,
            max_age_seconds=300.0,
            minimum=0.0,
        )
        bms_discharge_data_fresh = (
            bms_current_sample.fresh and bms_voltage_sample.fresh
        )
        bms_discharge_data_available = bool(
            bms_discharge_data_fresh
            and bms_current_sample.value is not None
            and bms_current_sample.value > 0.0
            and bms_voltage_sample.value is not None
            and bms_voltage_sample.value > 0.0
        )
        bms_charge_data_fresh = (
            bms_charge_current_sample.fresh and bms_voltage_sample.fresh
        )
        bms_charge_data_available = bool(
            bms_charge_data_fresh
            and bms_charge_current_sample.value is not None
            and bms_charge_current_sample.value > 0.0
            and bms_voltage_sample.value is not None
            and bms_voltage_sample.value > 0.0
        )
        bms_ages = (
            bms_current_sample.age_seconds,
            bms_voltage_sample.age_seconds,
        )
        if bms_discharge_data_fresh:
            bms_discharge_data_age_seconds = max(
                age for age in bms_ages if age is not None
            )
        else:
            failed_ages = (
                sample.age_seconds
                for sample in (bms_current_sample, bms_voltage_sample)
                if not sample.fresh and sample.age_seconds is not None
            )
            bms_discharge_data_age_seconds = next(failed_ages, None)
        bms_charge_ages = (
            bms_charge_current_sample.age_seconds,
            bms_voltage_sample.age_seconds,
        )
        if bms_charge_data_fresh:
            bms_charge_data_age_seconds = max(
                age for age in bms_charge_ages if age is not None
            )
        else:
            failed_charge_ages = (
                sample.age_seconds
                for sample in (bms_charge_current_sample, bms_voltage_sample)
                if not sample.fresh and sample.age_seconds is not None
            )
            bms_charge_data_age_seconds = next(failed_charge_ages, None)

        self_use_soc_sample = numeric_state_sample(
            self.hass.states.get(
                "sensor.hoymiles_hit_ems_self_use_soc_readback"
            ),
            now,
            max_age_seconds=300.0,
            minimum=10.0,
            maximum=100.0,
        )
        battery_soc_sample = numeric_state_sample(
            self.hass.states.get(
                "sensor.hoymiles_hit_overview_battery_soc"
            ),
            now,
            max_age_seconds=120.0,
            minimum=0.0,
            maximum=100.0,
        )
        inverter_count_sample = numeric_state_sample(
            self.hass.states.get(
                "sensor.hoymiles_hit_number_of_machines_master_and_slave"
            ),
            now,
            max_age_seconds=300.0,
            minimum=1.0,
            maximum=10.0,
        )
        required = {
            "sensor.hoymiles_hit_battery_capacity": _state_number(
                self.hass,
                "sensor.hoymiles_hit_battery_capacity",
            ),
            "sensor.hoymiles_hit_overview_battery_soc": (
                battery_soc_sample.value
                if battery_soc_sample.fresh
                else None
            ),
            "sensor.hoymiles_hit_ems_self_use_soc_readback": (
                self_use_soc_sample.value
                if self_use_soc_sample.fresh
                else None
            ),
            "sensor.hoymiles_hit_number_of_machines_master_and_slave": (
                inverter_count_sample.value
                if inverter_count_sample.fresh
                else None
            ),
            "sensor.hoymiles_hit_ems_force_discharge_soc_readback": _state_number(
                self.hass,
                "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
            ),
            "input_number.hoymiles_rce_requested_discharge_power": _state_number(
                self.hass,
                "input_number.hoymiles_rce_requested_discharge_power",
            ),
            "input_number.hoymiles_rce_soc_safety_margin": _state_number(
                self.hass,
                "input_number.hoymiles_rce_soc_safety_margin",
            ),
            "input_number.hoymiles_rce_export_efficiency": _state_number(
                self.hass,
                "input_number.hoymiles_rce_export_efficiency",
            ),
        }
        rated_power = _select_number(
            self.hass,
            "input_select.hoymiles_rce_inverter_rated_power",
        )
        if rated_power is None:
            required["input_select.hoymiles_rce_inverter_rated_power"] = None

        sun = self.hass.states.get("sun.sun")
        rising = (
            _parse_datetime(sun.attributes.get("next_rising"), timezone)
            if sun
            else None
        )
        setting = (
            _parse_datetime(sun.attributes.get("next_setting"), timezone)
            if sun
            else None
        )
        if rising is None or setting is None:
            required["sun.sun"] = None

        today_rows_state = self.hass.states.get("sensor.hoymiles_rce_day")
        tomorrow_rows_state = self.hass.states.get(
            "sensor.hoymiles_rce_day_tomorrow"
        )
        today_rows = (
            today_rows_state.attributes.get("value", [])
            if today_rows_state
            else []
        )
        tomorrow_rows = (
            tomorrow_rows_state.attributes.get("value", [])
            if tomorrow_rows_state
            else []
        )
        def rows_for_local_date(
            rows: Any,
            target_date: date,
        ) -> list[Mapping[str, Any]]:
            if not isinstance(rows, list):
                return []
            matched: list[Mapping[str, Any]] = []
            for item in rows:
                if not isinstance(item, Mapping):
                    continue
                raw_business_date = str(
                    item.get("business_date", "")
                ).strip()
                if raw_business_date == target_date.isoformat():
                    matched.append(item)
                    continue
                if raw_business_date:
                    continue
                # PSE ``dtime_utc`` is the end of the 15-minute settlement
                # interval.  Resolve the market quarter on the absolute UTC
                # timeline before assigning a local business date.  This is
                # especially important for the 24:00 endpoint and both folds
                # of the repeated autumn hour.  ``period_utc`` is a clock
                # range, not an absolute datetime, so it is not a safe date
                # fallback.
                raw_interval_end = item.get("dtime_utc")
                if isinstance(raw_interval_end, datetime):
                    interval_end = raw_interval_end
                elif (
                    isinstance(raw_interval_end, str)
                    and raw_interval_end.strip()
                ):
                    interval_end = dt_util.parse_datetime(
                        raw_interval_end.strip()
                    )
                else:
                    interval_end = None
                if interval_end is None:
                    continue
                if interval_end.tzinfo is None:
                    interval_end = interval_end.replace(tzinfo=dt_util.UTC)
                quarter_start = (
                    interval_end.astimezone(dt_util.UTC)
                    - timedelta(minutes=15)
                ).astimezone(timezone)
                if quarter_start.date() == target_date:
                    matched.append(item)
            return matched

        today_rows_age_seconds = state_age_seconds(today_rows_state, now)
        tomorrow_rows_age_seconds = state_age_seconds(tomorrow_rows_state, now)
        today_rows = rows_for_local_date(today_rows, now.date())
        tomorrow_rows = rows_for_local_date(
            tomorrow_rows,
            now.date() + timedelta(days=1),
        )
        (
            today_rows_complete,
            today_half_hours,
            today_expected_half_hours,
        ) = _complete_rce_half_hours_for_local_date(
            today_rows,
            now.date(),
            timezone,
        )
        (
            tomorrow_rows_structurally_complete,
            tomorrow_half_hours,
            tomorrow_expected_half_hours,
        ) = _complete_rce_half_hours_for_local_date(
            tomorrow_rows,
            now.date() + timedelta(days=1),
            timezone,
        )
        today_rows_data_fresh = bool(
            today_rows_complete
            and today_rows_age_seconds is not None
            and -5.0 <= today_rows_age_seconds <= _RCE_PRICE_MAX_AGE_SECONDS
        )
        tomorrow_price_rows_complete = bool(
            tomorrow_rows_structurally_complete
            and tomorrow_rows_age_seconds is not None
            and -5.0
            <= tomorrow_rows_age_seconds
            <= _RCE_PRICE_MAX_AGE_SECONDS
        )
        if not today_rows_data_fresh:
            required["sensor.hoymiles_rce_day"] = None

        block_start = _helper_minutes(
            self.hass,
            "input_datetime.hoymiles_sale_block_start",
        )
        block_end = _helper_minutes(
            self.hass,
            "input_datetime.hoymiles_sale_block_end",
        )
        if block_start is None:
            required["input_datetime.hoymiles_sale_block_start"] = None
        if block_end is None:
            required["input_datetime.hoymiles_sale_block_end"] = None

        today_configured = _state_text(
            self.hass,
            TODAY_FORECAST_ENTITY_HELPER,
        )
        tomorrow_configured = _state_text(
            self.hass,
            TOMORROW_FORECAST_ENTITY_HELPER,
        )
        day3_configured = _state_text(
            self.hass,
            DAY3_FORECAST_ENTITY_HELPER,
        )
        today_entity, today_forecast_state = _first_numeric_state(
            self.hass,
            TODAY_FORECAST_CANDIDATES,
            today_configured,
        )
        tomorrow_entity, tomorrow_forecast_state = _first_numeric_state(
            self.hass,
            TOMORROW_FORECAST_CANDIDATES,
            tomorrow_configured,
        )
        remaining_entity, remaining_state = _first_numeric_state(
            self.hass,
            REMAINING_TODAY_CANDIDATES,
        )
        day3_entity, day3_forecast_state = _first_numeric_state(
            self.hass,
            DAY3_FORECAST_CANDIDATES,
            day3_configured,
        )
        today_forecast_sample = numeric_state_sample(
            today_forecast_state,
            now,
            max_age_seconds=_TODAY_FORECAST_MAX_AGE_SECONDS,
            minimum=0.0,
        )
        tomorrow_forecast_sample = numeric_state_sample(
            tomorrow_forecast_state,
            now,
            max_age_seconds=_TOMORROW_FORECAST_MAX_AGE_SECONDS,
            minimum=0.0,
        )
        day3_forecast_sample = numeric_state_sample(
            day3_forecast_state,
            now,
            max_age_seconds=_DAY3_FORECAST_MAX_AGE_SECONDS,
            minimum=0.0,
        )
        forecast_today_data_fresh = today_forecast_sample.fresh
        forecast_tomorrow_data_fresh = tomorrow_forecast_sample.fresh
        forecast_day3_data_fresh = day3_forecast_sample.fresh
        if day3_forecast_state is None:
            forecast_day3_status = "missing"
        elif forecast_day3_data_fresh:
            forecast_day3_status = "fresh"
        else:
            forecast_day3_status = "stale"
        usable_day3_forecast_state = (
            day3_forecast_state if forecast_day3_data_fresh else None
        )
        if not forecast_today_data_fresh:
            required["Solcast Forecast Today"] = None
        # Tomorrow is optional. Use it only when both the price day and its
        # matching forecast are positively fresh; otherwise solve today only.
        tomorrow_rows_complete = bool(
            tomorrow_price_rows_complete and forecast_tomorrow_data_fresh
        )
        usable_tomorrow_rows = tomorrow_rows if tomorrow_rows_complete else []
        usable_tomorrow_forecast_state = (
            tomorrow_forecast_state if tomorrow_rows_complete else None
        )

        if self._load_history.average_daily_kwh is not None:
            history_load = self._load_history.average_daily_kwh
            load_history_days = float(self._load_history.daily_history_days)
            load_history_source = "recorder_phase_energy_counters"
        else:
            history_load = _state_number(
                self.hass,
                "sensor.hoymiles_load_average_4_days",
            )
            load_history_days = _state_attribute_number(
                self.hass,
                "sensor.hoymiles_load_average_4_days",
                "history_days",
            )
            load_history_source = "statistics_fallback"
        if self._load_history.average_night_kwh is not None:
            night_history_days = float(self._load_history.night_history_days)
        else:
            night_history_days = _state_attribute_number(
                self.hass,
                "sensor.hoymiles_night_load_average_4_days",
                "history_days",
            )
        fallback_load = _state_number(
            self.hass,
            "input_number.hoymiles_rce_fallback_daily_load",
        )
        actual_load_today = _state_number(
            self.hass,
            "sensor.hoymiles_actual_load_energy_today",
        )
        # A newly installed/migrated actual-load meter needs four complete days
        # before the long-term statistic is representative.  During that
        # transition, never reserve less than either the user fallback or a
        # conservative projection of today's measured house energy.  The
        # quarter-day denominator prevents one early counter step from creating
        # an unrealistically large projection just after midnight.
        elapsed_day_fraction = max(
            (now.hour * 60 + now.minute) / (24 * 60),
            0.25,
        )
        live_daily_projection = (
            max(actual_load_today, 0.0) / elapsed_day_fraction
            if actual_load_today is not None
            else None
        )
        history_complete = (load_history_days or 0.0) >= 3.95
        if history_load is not None and history_complete:
            average_load = history_load
            load_model_source = "history_4_days"
        else:
            provisional_candidates = (
                history_load,
                fallback_load,
                live_daily_projection,
            )
            numeric_candidates = [
                value for value in provisional_candidates if value is not None
            ]
            average_load = max(numeric_candidates) if numeric_candidates else None
            load_model_source = "provisional_safe_max"
        if average_load is None:
            required["sensor.hoymiles_load_average_4_days"] = None

        missing = sorted(
            entity_id for entity_id, value in required.items() if value is None
        )
        profile_history = (
            self._extended_load_history
            if self._extended_load_history.daily_history_days
            else self._load_history
        )
        (
            conservative_daily_load,
            conservative_load_days,
        ) = robust_weighted_upper_estimate(
            tuple(profile_history.daily_energy_kwh.values())
        )
        (
            conservative_night_load,
            conservative_night_days,
        ) = robust_weighted_upper_estimate(
            tuple(profile_history.night_energy_kwh.values())
        )
        learning_policy, learning_diagnostics = (
            _forecast_learning_policy_snapshot(self.hass, now)
        )
        self._forecast_gcf_policy_signature = (
            _forecast_learning_policy_signature(learning_policy)
        )
        self._forecast_gcf_optimizer_signature = (
            _forecast_gcf_optimizer_signature(
                learning_policy,
                learning_diagnostics,
            )
        )
        forecast_factor_used = forecast_factor_for_policy(
            learning_policy,
            self._forecast_accuracy_factor,
        )
        forecast_history_days_used = (
            self._forecast_accuracy_days if learning_policy.enabled else 0
        )
        forecast_uncertainty_used = (
            self._forecast_accuracy_uncertainty
            if learning_policy.enabled
            else 0.15
        )
        metadata: dict[str, Any] = {
            **learning_diagnostics,
            "forecast_learning_enabled": learning_policy.enabled,
            "forecast_learning_mode": learning_policy.mode,
            "forecast_learning_excluded_reason": (
                learning_policy.excluded_reason
            ),
            "forecast_factor_used": round(forecast_factor_used, 3),
            "forecast_learning_history_days_used": forecast_history_days_used,
            "missing_entities": missing,
            "forecast_today_entity": today_entity or "none",
            "forecast_tomorrow_entity": tomorrow_entity or "none",
            "forecast_remaining_today_entity": remaining_entity or "fallback",
            "forecast_day3_entity": day3_entity or "not_enabled",
            "forecast_day3_configured_entity": day3_configured or "automatic",
            "forecast_day3_status": forecast_day3_status,
            "forecast_day3_data_available": day3_forecast_state is not None,
            "forecast_day3_data_fresh": forecast_day3_data_fresh,
            "forecast_day3_data_complete": forecast_day3_data_fresh,
            "forecast_day3_age_seconds": (
                round(day3_forecast_sample.age_seconds, 1)
                if day3_forecast_sample.age_seconds is not None
                else None
            ),
            "forecast_day3_data_reason": day3_forecast_sample.reason,
            "rce_today_periods": len(today_rows) if isinstance(today_rows, list) else 0,
            "rce_today_half_hours": today_half_hours,
            "rce_today_expected_half_hours": today_expected_half_hours,
            "rce_tomorrow_periods": (
                len(tomorrow_rows) if isinstance(tomorrow_rows, list) else 0
            ),
            "rce_tomorrow_half_hours": tomorrow_half_hours,
            "rce_tomorrow_expected_half_hours": tomorrow_expected_half_hours,
            "planning_scope": (
                "today_and_tomorrow"
                if tomorrow_rows_complete
                else "today_only"
            ),
            "tomorrow_data_pending": not tomorrow_rows_complete,
            "rce_today_data_fresh": today_rows_data_fresh,
            "rce_today_age_seconds": (
                round(today_rows_age_seconds, 1)
                if today_rows_age_seconds is not None
                else None
            ),
            "rce_tomorrow_data_fresh": tomorrow_price_rows_complete,
            "rce_tomorrow_age_seconds": (
                round(tomorrow_rows_age_seconds, 1)
                if tomorrow_rows_age_seconds is not None
                else None
            ),
            "forecast_today_data_fresh": forecast_today_data_fresh,
            "forecast_today_age_seconds": (
                round(today_forecast_sample.age_seconds, 1)
                if today_forecast_sample.age_seconds is not None
                else None
            ),
            "forecast_today_data_reason": today_forecast_sample.reason,
            "self_use_soc_data_fresh": self_use_soc_sample.fresh,
            "self_use_soc_age_seconds": (
                round(self_use_soc_sample.age_seconds, 1)
                if self_use_soc_sample.age_seconds is not None
                else None
            ),
            "self_use_soc_data_reason": self_use_soc_sample.reason,
            "inverter_count_data_fresh": inverter_count_sample.fresh,
            "inverter_count_age_seconds": (
                round(inverter_count_sample.age_seconds, 1)
                if inverter_count_sample.age_seconds is not None
                else None
            ),
            "inverter_count_data_reason": inverter_count_sample.reason,
            "forecast_tomorrow_data_fresh": (
                forecast_tomorrow_data_fresh
            ),
            "forecast_tomorrow_age_seconds": (
                round(tomorrow_forecast_sample.age_seconds, 1)
                if tomorrow_forecast_sample.age_seconds is not None
                else None
            ),
            "forecast_tomorrow_data_reason": (
                tomorrow_forecast_sample.reason
            ),
            "automatic_replan": True,
            "automatic_discharge_enabled": self.hass.states.is_state(
                "input_boolean.hoymiles_rce_discharge_enabled",
                "on",
            ),
            "plan_is_preview": not self.hass.states.is_state(
                "input_boolean.hoymiles_rce_discharge_enabled",
                "on",
            ),
            "bms_discharge_data_fresh": bms_discharge_data_fresh,
            "bms_discharge_data_age_seconds": (
                round(bms_discharge_data_age_seconds, 1)
                if bms_discharge_data_age_seconds is not None
                else None
            ),
            "bms_discharge_data_available": bms_discharge_data_available,
            "bms_discharge_current_data_reason": bms_current_sample.reason,
            "bms_voltage_data_reason": bms_voltage_sample.reason,
            "bms_charge_data_fresh": bms_charge_data_fresh,
            "bms_charge_data_age_seconds": (
                round(bms_charge_data_age_seconds, 1)
                if bms_charge_data_age_seconds is not None
                else None
            ),
            "bms_charge_data_available": bms_charge_data_available,
            "bms_charge_current_data_reason": (
                bms_charge_current_sample.reason
            ),
            "load_model_source": load_model_source,
            "load_history_source": load_history_source,
            "recorder_load_average_4d_kwh": (
                round(self._load_history.average_daily_kwh, 2)
                if self._load_history.average_daily_kwh is not None
                else None
            ),
            "recorder_load_history_days": profile_history.daily_history_days,
            "recorder_load_history_energy_kwh": round(
                profile_history.daily_energy_total_kwh,
                2,
            ),
            "recorder_load_daily_kwh": profile_history.daily_energy_kwh,
            "recorder_load_recent_4d_kwh": self._load_history.daily_energy_kwh,
            "recorder_load_average_28d_kwh": (
                round(profile_history.average_daily_kwh, 2)
                if profile_history.average_daily_kwh is not None
                else None
            ),
            "recorder_load_profile_30m_kwh": list(
                profile_history.average_profile_kwh
            ),
            "recorder_load_average_profile_30m_kwh": list(
                profile_history.average_profile_kwh
            ),
            "recorder_load_profile_history_days": (
                profile_history.daily_history_days
            ),
            "load_profile_generated_at": (
                self._load_profile_generated_at.isoformat()
                if self._load_profile_generated_at is not None
                else None
            ),
            "recorder_load_weekday_profile_30m_kwh": list(
                profile_history.weekday_profile_kwh
            ),
            "recorder_load_weekend_profile_30m_kwh": list(
                profile_history.weekend_profile_kwh
            ),
            "recorder_load_weekday_profile_days": (
                profile_history.weekday_profile_days
            ),
            "recorder_load_weekend_profile_days": (
                profile_history.weekend_profile_days
            ),
            "recorder_night_load_average_4d_kwh": (
                round(self._load_history.average_night_kwh, 2)
                if self._load_history.average_night_kwh is not None
                else None
            ),
            "recorder_night_history_days": self._load_history.night_history_days,
            "recorder_night_history_energy_kwh": round(
                self._load_history.night_energy_total_kwh,
                2,
            ),
            "recorder_night_daily_kwh": self._load_history.night_energy_kwh,
            "recorder_night_daily_kwh_28d": (
                profile_history.night_energy_kwh
            ),
            "recorder_night_load_average_28d_kwh": (
                round(profile_history.average_night_kwh, 2)
                if profile_history.average_night_kwh is not None
                else None
            ),
            "actual_load_energy_today_kwh": (
                round(actual_load_today, 2)
                if actual_load_today is not None
                else None
            ),
            "actual_day_window_load_today_kwh": (
                round(self._load_history.current_day_energy_kwh, 2)
                if self._load_history.current_day_energy_kwh is not None
                else None
            ),
            "day_load_live_source": (
                "recorder_actual_phase_load"
                if self._load_history.current_day_energy_kwh is not None
                else "history_only"
            ),
            "provisional_daily_load_projection_kwh": (
                round(live_daily_projection, 2)
                if live_daily_projection is not None
                else None
            ),
            "selected_average_daily_load_kwh": (
                round(average_load, 2) if average_load is not None else None
            ),
            "load_history_days": (
                round(load_history_days, 2)
                if load_history_days is not None
                else 0.0
            ),
            "night_history_days": (
                round(night_history_days, 2)
                if night_history_days is not None
                else 0.0
            ),
            "history_complete": (
                history_complete and (night_history_days or 0.0) >= 3.95
            ),
            "conservative_daily_load_p90_kwh": (
                round(conservative_daily_load, 2)
                if conservative_daily_load is not None
                else None
            ),
            "conservative_night_load_p90_kwh": (
                round(conservative_night_load, 2)
                if conservative_night_load is not None
                else None
            ),
            "conservative_load_history_days": conservative_load_days,
            "conservative_night_history_days": conservative_night_days,
        }
        if missing:
            return None, metadata

        assert rising is not None
        assert setting is not None
        assert block_start is not None
        assert block_end is not None
        assert average_load is not None
        assert rated_power is not None
        price_slots = parse_rce_rows(
            [*today_rows, *usable_tomorrow_rows],
            timezone,
            block_enabled=self.hass.states.is_state(
                "input_boolean.hoymiles_sale_block_enabled",
                "on",
            ),
            block_start_minute=block_start,
            block_end_minute=block_end,
        )
        current_slot_utc = now_slot.astimezone(dt_util.UTC)
        current_price = next(
            (
                slot.price_pln_kwh
                for slot in price_slots
                if slot.start.astimezone(dt_util.UTC) == current_slot_utc
            ),
            None,
        )
        metadata["current_price_pln_kwh"] = (
            round(current_price, 6) if current_price is not None else None
        )

        forecast_today_raw = max(today_forecast_sample.value or 0.0, 0.0)
        forecast_tomorrow_raw = max(
            tomorrow_forecast_sample.value or 0.0,
            0.0,
        ) if tomorrow_rows_complete else 0.0
        actual_pv_today = _state_number(
            self.hass,
            "sensor.hoymiles_hit_pv_total_energy_today",
        ) or 0.0
        remaining_sample = numeric_state_sample(
            remaining_state,
            now,
            max_age_seconds=18 * 3600.0,
            minimum=0.0,
        )
        remaining_today_raw = (
            remaining_sample.value
            if remaining_sample.fresh and remaining_sample.value is not None
            else max(forecast_today_raw - actual_pv_today, 0.0)
        )
        metadata.update(
            {
                "forecast_remaining_today_entity": (
                    remaining_entity
                    if remaining_sample.fresh
                    else "forecast_minus_actual_fallback"
                ),
                "forecast_remaining_today_data_fresh": remaining_sample.fresh,
                "forecast_remaining_today_age_seconds": (
                    round(remaining_sample.age_seconds, 1)
                    if remaining_sample.age_seconds is not None
                    else None
                ),
                "forecast_remaining_today_data_reason": remaining_sample.reason,
            }
        )
        sunrise_minute = rising.hour * 60 + rising.minute
        sunset_minute = setting.hour * 60 + setting.minute
        night_start = (sunset_minute - 90) % (24 * 60)
        night_end = (sunrise_minute + 90) % (24 * 60)

        sunrise_today = get_astral_event_date(
            self.hass,
            "sunrise",
            now.date(),
        )
        sunset_today = get_astral_event_date(
            self.hass,
            "sunset",
            now.date(),
        )
        expected_elapsed_raw = _detailed_pv_expected_elapsed_kwh(
            today_forecast_state,
            now.date(),
            timezone,
            now,
        )
        live_eligible = (
            learning_policy.enabled
            and sunrise_today is not None
            and sunset_today is not None
            and now >= sunrise_today.astimezone(timezone) + timedelta(minutes=90)
            and now <= sunset_today.astimezone(timezone) + timedelta(minutes=30)
        )
        (
            today_forecast_factor,
            live_forecast_ratio,
            live_forecast_confidence,
        ) = adaptive_forecast_factor(
            forecast_factor_used,
            actual_pv_today,
            expected_elapsed_raw,
            eligible=live_eligible,
        )
        forecast_today = forecast_today_raw * today_forecast_factor
        forecast_tomorrow = (
            forecast_tomorrow_raw * forecast_factor_used
        )
        remaining_today = remaining_today_raw * today_forecast_factor

        today_p10_raw = _forecast_total(today_forecast_state, "p10")
        today_p90_raw = _forecast_total(today_forecast_state, "p90")
        tomorrow_p10_raw = _forecast_total(
            usable_tomorrow_forecast_state,
            "p10",
        )
        tomorrow_p90_raw = _forecast_total(
            usable_tomorrow_forecast_state,
            "p90",
        )
        uncertainty_available = (
            today_p10_raw is not None
            and tomorrow_p10_raw is not None
            and forecast_today_raw > 0
            and forecast_tomorrow_raw > 0
        )
        risk_weight = uncertainty_risk_weight(
            history_days=forecast_history_days_used,
            live_confidence=live_forecast_confidence,
            uncertainty_available=uncertainty_available,
        )
        uncertainty_spread_ratio = (
            max(tomorrow_p90_raw - tomorrow_p10_raw, 0.0)
            / max(forecast_tomorrow_raw, 0.001)
            if tomorrow_p90_raw is not None
            and tomorrow_p10_raw is not None
            and forecast_tomorrow_raw > 0
            else 0.0
        )
        # A wider P10–P90 band moves reserve planning further toward P10.
        risk_weight = min(
            risk_weight + min(uncertainty_spread_ratio, 1.0) * 0.15,
            0.90,
        )
        analysis = (
            usable_tomorrow_forecast_state.attributes.get("analysis")
            if usable_tomorrow_forecast_state is not None
            else None
        )
        solcast_band_confidence: float | None = None
        if isinstance(analysis, Mapping):
            try:
                solcast_band_confidence = min(
                    max(float(analysis["confidence"]), 0.0),
                    1.0,
                )
            except (KeyError, TypeError, ValueError):
                pass
        history_forecast_confidence = min(
            forecast_history_days_used / 4.0,
            1.0,
        )
        forecast_confidence = 100.0 * (
            0.50 * history_forecast_confidence
            + 0.30 * (solcast_band_confidence or 0.0)
            + 0.20 * live_forecast_confidence
        )
        today_p10_remaining = (
            remaining_today
            * min(max(today_p10_raw / forecast_today_raw, 0.0), 1.0)
            if today_p10_raw is not None and forecast_today_raw > 0
            else remaining_today
        )
        tomorrow_p10 = (
            forecast_tomorrow
            * min(max(tomorrow_p10_raw / forecast_tomorrow_raw, 0.0), 1.0)
            if tomorrow_p10_raw is not None and forecast_tomorrow_raw > 0
            else forecast_tomorrow
        )

        pv_today = _detailed_pv_map(
            today_forecast_state,
            now.date(),
            remaining_today,
            timezone,
            now_slot,
        )
        if not pv_today:
            pv_today = _fallback_pv_map(
                now.date(),
                remaining_today,
                timezone,
                now_slot,
                sunrise_minute,
                sunset_minute,
            )
        pv_today_low = _detailed_pv_map(
            today_forecast_state,
            now.date(),
            today_p10_remaining,
            timezone,
            now_slot,
            percentile="p10",
        )
        if not pv_today_low:
            pv_today_low = _fallback_pv_map(
                now.date(),
                today_p10_remaining,
                timezone,
                now_slot,
                sunrise_minute,
                sunset_minute,
            )
        tomorrow_date = now.date() + timedelta(days=1)
        pv_tomorrow = _detailed_pv_map(
            usable_tomorrow_forecast_state,
            tomorrow_date,
            forecast_tomorrow,
            timezone,
            now_slot,
        )
        if not pv_tomorrow:
            pv_tomorrow = _fallback_pv_map(
                tomorrow_date,
                forecast_tomorrow,
                timezone,
                now_slot,
                sunrise_minute,
                sunset_minute,
            )
        pv_tomorrow_low = _detailed_pv_map(
            usable_tomorrow_forecast_state,
            tomorrow_date,
            tomorrow_p10,
            timezone,
            now_slot,
            percentile="p10",
        )
        if not pv_tomorrow_low:
            pv_tomorrow_low = _fallback_pv_map(
                tomorrow_date,
                tomorrow_p10,
                timezone,
                now_slot,
                sunrise_minute,
                sunset_minute,
            )
        pv_by_slot = dict(pv_today)
        for start, energy in pv_tomorrow.items():
            pv_by_slot[start] = pv_by_slot.get(start, 0.0) + energy
        low_pv_by_slot = dict(pv_today_low)
        for start, energy in pv_tomorrow_low.items():
            low_pv_by_slot[start] = low_pv_by_slot.get(start, 0.0) + energy
        conservative_pv_by_slot = _blend_pv_maps(
            pv_by_slot,
            low_pv_by_slot,
            risk_weight,
        )

        day3_raw = _forecast_total(usable_day3_forecast_state, "p50")
        day3_p10_raw = _forecast_total(usable_day3_forecast_state, "p10")
        day3_expected = (
            day3_raw * forecast_factor_used
            if day3_raw is not None
            else None
        )
        day3_low = (
            day3_p10_raw * forecast_factor_used
            if day3_p10_raw is not None
            else day3_expected
        )
        day3_conservative = (
            blend_low_expected(day3_low or 0.0, day3_expected, risk_weight)
            if day3_expected is not None
            else None
        )

        inverter_count = round(inverter_count_sample.value or 0.0)
        if not inverter_count_sample.fresh or not 1 <= inverter_count <= 10:
            metadata["missing_entities"] = sorted(
                {
                    *metadata.get("missing_entities", []),
                    "sensor.hoymiles_hit_number_of_machines_master_and_slave",
                }
            )
            return None, metadata
        system_power_kw = rated_power * inverter_count
        gcf_state = self.hass.states.get(
            "sensor.hoymiles_hit_gcf_enable_readback_code"
        )
        gcf_sample = numeric_state_sample(
            gcf_state,
            now,
            max_age_seconds=300.0,
            minimum=0.0,
            maximum=1.0,
        )
        gcf_age_seconds = gcf_sample.age_seconds
        gcf_data_fresh = bool(
            gcf_sample.fresh and gcf_sample.value in {0.0, 1.0}
        )
        gcf_enabled = bool(gcf_data_fresh and gcf_sample.value == 1.0)
        gcf_limit_sample = numeric_state_sample(
            self.hass.states.get(
                "sensor.hoymiles_hit_gcf_maximum_export_power_readback"
            ),
            now,
            max_age_seconds=300.0,
            minimum=-10.0,
            maximum=200.0,
        )
        gcf_limit_percent = gcf_limit_sample.value
        gcf_limit_data_fresh = bool(
            gcf_limit_sample.fresh if gcf_enabled else True
        )
        gcf_execution_data_fresh = bool(
            gcf_data_fresh and gcf_limit_data_fresh
        )
        if not gcf_execution_data_fresh:
            metadata["missing_entities"] = sorted(
                {
                    *metadata.get("missing_entities", []),
                    "Generation Control Function",
                }
            )
            metadata.update(
                {
                    "gcf_enabled": gcf_enabled,
                    "gcf_data_fresh": gcf_data_fresh,
                    "gcf_age_seconds": (
                        round(gcf_age_seconds, 1)
                        if gcf_age_seconds is not None
                        else None
                    ),
                    "gcf_limit_data_fresh": gcf_limit_data_fresh,
                    "gcf_limit_age_seconds": (
                        round(gcf_limit_sample.age_seconds, 1)
                        if gcf_limit_sample.age_seconds is not None
                        else None
                    ),
                    "gcf_execution_data_fresh": False,
                }
            )
            return None, metadata
        export_power_cap_kw = (
            system_power_kw
            * min(max(gcf_limit_percent or 0.0, 0.0), 100.0)
            / 100.0
            if gcf_enabled and gcf_limit_percent is not None
            else None
        )
        effective_export_power_kw: float | None = None
        effective_export_source = "not_available"
        # Never interpret live Grid=0 in Self-Use as an inverter limit.  Only
        # an explicit learned/effective entity may constrain future planning.
        for entity_id in (
            "sensor.hoymiles_rce_learned_export_power",
            "sensor.hoymiles_rce_effective_export_power",
        ):
            learned = _state_number(self.hass, entity_id)
            if learned is not None and learned > 0:
                effective_export_power_kw = learned
                effective_export_source = entity_id
                break

        tariff_plan = self._same_entry_tariff_plan_state()
        avoided_import_price = None
        if tariff_plan is not None:
            for attribute in (
                "tariff_profile_g11_price",
                "g11_reference_price_pln_kwh",
                "current_price_pln_kwh",
            ):
                try:
                    avoided_import_price = float(
                        tariff_plan.attributes[attribute]
                    )
                    break
                except (KeyError, TypeError, ValueError):
                    pass
        if avoided_import_price is None:
            avoided_import_price = _state_number(
                self.hass,
                "input_number.hoymiles_tariff_g11_price",
            )
        if avoided_import_price is None or avoided_import_price <= 0:
            avoided_import_price = 1.0
        battery_wear_cost = _state_number(
            self.hass,
            "input_number.hoymiles_rce_battery_wear_cost",
        )
        if battery_wear_cost is None:
            battery_wear_cost = 0.08
        charge_efficiency = _state_number(
            self.hass,
            "input_number.hoymiles_tariff_charge_efficiency",
        )
        discharge_efficiency = _state_number(
            self.hass,
            "input_number.hoymiles_tariff_discharge_efficiency",
        )
        charge_efficiency = charge_efficiency or 95.0
        discharge_efficiency = discharge_efficiency or 95.0
        night_load = self._load_history.average_night_kwh
        if night_load is None:
            night_load = _state_number(
                self.hass,
                "sensor.hoymiles_night_load_average_4_days",
            )
        pv_to_load_today = _state_number(
            self.hass,
            "sensor.hoymiles_hit_pv_to_load_energy_today",
        ) or 0.0
        battery_to_load_today = _state_number(
            self.hass,
            "sensor.hoymiles_hit_energy_from_battery_today",
        ) or 0.0
        grid_to_load_today = _state_number(
            self.hass,
            "sensor.hoymiles_hit_energy_from_grid_today",
        ) or 0.0
        pv_to_load_power_w = _state_number(
            self.hass,
            "sensor.hoymiles_hit_load_from_pv_power",
        ) or 0.0
        pv_total_power_w = _state_number(
            self.hass,
            "sensor.hoymiles_hit_overview_pv_total_power",
        ) or 0.0
        (
            current_load_power_kw,
            current_load_power_age_seconds,
            current_load_power_source,
        ) = _fresh_power_sample(
            self.hass,
            "sensor.hoymiles_actual_load_power",
            now,
        )
        if current_load_power_kw is None:
            phase_samples = tuple(
                _fresh_power_sample(self.hass, entity_id, now)
                for entity_id in (
                    "sensor.hoymiles_hit_load_power_l1n",
                    "sensor.hoymiles_hit_load_power_l2n",
                    "sensor.hoymiles_hit_load_power_l3n",
                )
            )
            if all(sample[0] is not None for sample in phase_samples):
                current_load_power_kw = sum(
                    sample[0] or 0.0 for sample in phase_samples
                )
                current_load_power_age_seconds = max(
                    sample[1] or 0.0 for sample in phase_samples
                )
                current_load_power_source = "live_phase_sum"
            else:
                (
                    current_load_power_kw,
                    current_load_power_age_seconds,
                    current_load_power_source,
                ) = _fresh_power_sample(
                    self.hass,
                    "sensor.hoymiles_hit_overview_load_active_power",
                    now,
                )
                if current_load_power_source == "live":
                    current_load_power_source = "live_overview_fallback"
        (
            current_pv_power_kw,
            current_pv_power_age_seconds,
            current_pv_power_source,
        ) = _fresh_power_sample(
            self.hass,
            "sensor.hoymiles_hit_overview_pv_total_power",
            now,
        )
        load_power_w = _state_number(
            self.hass,
            "sensor.hoymiles_actual_load_power",
        )
        if load_power_w is None:
            phase_loads = tuple(
                _state_number(self.hass, entity_id)
                for entity_id in (
                    "sensor.hoymiles_hit_load_power_l1n",
                    "sensor.hoymiles_hit_load_power_l2n",
                    "sensor.hoymiles_hit_load_power_l3n",
                )
            )
            if all(value is not None for value in phase_loads):
                load_power_w = sum(
                    max(value or 0.0, 0.0) for value in phase_loads
                )
            else:
                load_power_w = _state_number(
                    self.hass,
                    "sensor.hoymiles_hit_overview_load_active_power",
                )
        load_power_w = max(load_power_w or 0.0, 0.0)
        # Rejestr 2180 potrafi zawierać straty przetwarzania. Dla udziału
        # autokonsumpcji ograniczamy go do rzeczywistego obciążenia domu.
        pv_to_load_power_w = min(
            max(pv_to_load_power_w, 0.0),
            load_power_w,
        )
        battery_age = (
            battery_soc_sample.age_seconds / 60.0
            if battery_soc_sample.age_seconds is not None
            else None
        )
        rce_today_age = _state_age_minutes(today_rows_state, now)
        rce_tomorrow_age = _state_age_minutes(tomorrow_rows_state, now)
        forecast_today_age = _state_age_minutes(today_forecast_state, now)
        forecast_tomorrow_age = _state_age_minutes(
            tomorrow_forecast_state,
            now,
        )
        p10_missing = today_p10_raw is None or tomorrow_p10_raw is None
        p10_stale = not (
            _age_minutes_is_fresh(forecast_today_age, 360)
            and _age_minutes_is_fresh(forecast_tomorrow_age, 720)
        )
        p10_high_risk = (
            forecast_uncertainty_used >= 0.18
            or uncertainty_spread_ratio >= 0.75
        )
        critical_zero_pv_guard = p10_missing or p10_stale or p10_high_risk
        if p10_missing:
            critical_zero_pv_reason = "p10_missing"
        elif p10_stale:
            critical_zero_pv_reason = "p10_stale"
        elif p10_high_risk:
            critical_zero_pv_reason = "p10_high_risk"
        else:
            critical_zero_pv_reason = "not_required"
        quality_issues: list[str] = []
        quality_score = 100
        if not _age_minutes_is_fresh(battery_age, 10):
            quality_score -= 25
            quality_issues.append("battery_soc_stale")
        if not _age_minutes_is_fresh(forecast_today_age, 360):
            quality_score -= 15
            quality_issues.append("forecast_today_stale")
        if not _age_minutes_is_fresh(forecast_tomorrow_age, 720):
            quality_score -= 15
            quality_issues.append("forecast_tomorrow_stale")
        if not _age_minutes_is_fresh(rce_today_age, 24 * 60):
            quality_score -= 20
            quality_issues.append("rce_today_stale")
        if not tomorrow_rows_complete:
            quality_score -= 10
            quality_issues.append("rce_tomorrow_pending")
        if not uncertainty_available:
            quality_score -= 10
            quality_issues.append("solcast_uncertainty_unavailable")
        if learning_policy.enabled and self._forecast_accuracy_days < 2:
            quality_score -= 5
            quality_issues.append("forecast_history_short")
        if profile_history.daily_history_days < 4:
            quality_score -= 10
            quality_issues.append("load_history_short")
        quality_score = max(quality_score, 0)
        quality_level = (
            "high"
            if quality_score >= 80
            else "medium"
            if quality_score >= 55
            else "low"
        )
        metadata.update(
            {
                "forecast_today_raw_kwh": round(forecast_today_raw, 2),
                "forecast_today_kwh": round(forecast_today, 2),
                "forecast_remaining_today_kwh": round(remaining_today, 2),
                "forecast_remaining_today_raw_kwh": round(
                    remaining_today_raw,
                    2,
                ),
                "forecast_tomorrow_raw_kwh": round(
                    forecast_tomorrow_raw,
                    2,
                ),
                "forecast_tomorrow_kwh": round(forecast_tomorrow, 2),
                "forecast_day3_kwh": (
                    round(day3_conservative, 2)
                    if day3_conservative is not None
                    else None
                ),
                "forecast_today_p10_kwh": (
                    round(today_p10_raw, 2)
                    if today_p10_raw is not None
                    else None
                ),
                "forecast_today_p90_kwh": (
                    round(today_p90_raw, 2)
                    if today_p90_raw is not None
                    else None
                ),
                "forecast_tomorrow_p10_kwh": (
                    round(tomorrow_p10_raw, 2)
                    if tomorrow_p10_raw is not None
                    else None
                ),
                "forecast_tomorrow_p90_kwh": (
                    round(tomorrow_p90_raw, 2)
                    if tomorrow_p90_raw is not None
                    else None
                ),
                "forecast_accuracy_factor": round(
                    self._forecast_accuracy_factor,
                    4,
                ),
                "forecast_accuracy_uncertainty": round(
                    self._forecast_accuracy_uncertainty,
                    4,
                ),
                "forecast_accuracy_history_days": (
                    self._forecast_accuracy_days
                ),
                "forecast_accuracy_source": self._forecast_accuracy_source,
                "forecast_today_effective_factor": round(
                    today_forecast_factor,
                    4,
                ),
                "forecast_live_ratio": (
                    round(live_forecast_ratio, 4)
                    if live_forecast_ratio is not None
                    else None
                ),
                "forecast_live_confidence": round(
                    live_forecast_confidence,
                    4,
                ),
                "forecast_live_expected_elapsed_kwh": (
                    round(expected_elapsed_raw, 2)
                    if expected_elapsed_raw is not None
                    else None
                ),
                "forecast_live_actual_elapsed_kwh": round(
                    actual_pv_today,
                    2,
                ),
                "forecast_uncertainty_available": uncertainty_available,
                "forecast_uncertainty_risk_weight": round(risk_weight, 4),
                "forecast_uncertainty_spread_ratio": round(
                    uncertainty_spread_ratio,
                    4,
                ),
                "critical_zero_pv_guard_requested": critical_zero_pv_guard,
                "critical_zero_pv_guard_request_reason": (
                    critical_zero_pv_reason
                ),
                "forecast_conservative_horizon_kwh": round(
                    sum(conservative_pv_by_slot.values()),
                    2,
                ),
                "forecast_expected_horizon_kwh": round(
                    sum(pv_by_slot.values()),
                    2,
                ),
                "data_quality_score": quality_score,
                "data_quality_level": quality_level,
                "data_quality_issues": quality_issues,
                "battery_soc_age_minutes": (
                    round(battery_age, 1) if battery_age is not None else None
                ),
                "soc_data_fresh": battery_soc_sample.fresh,
                "soc_data_age_seconds": (
                    round(battery_age * 60.0, 1)
                    if battery_age is not None
                    else None
                ),
                "rce_today_age_minutes": (
                    round(rce_today_age, 1)
                    if rce_today_age is not None
                    else None
                ),
                "rce_tomorrow_age_minutes": (
                    round(rce_tomorrow_age, 1)
                    if rce_tomorrow_age is not None
                    else None
                ),
                "forecast_today_age_minutes": (
                    round(forecast_today_age, 1)
                    if forecast_today_age is not None
                    else None
                ),
                "forecast_tomorrow_age_minutes": (
                    round(forecast_tomorrow_age, 1)
                    if forecast_tomorrow_age is not None
                    else None
                ),
                "current_live_load_power_kw": (
                    round(current_load_power_kw, 3)
                    if current_load_power_kw is not None
                    else None
                ),
                "current_live_load_power_age_seconds": (
                    round(current_load_power_age_seconds, 1)
                    if current_load_power_age_seconds is not None
                    else None
                ),
                "current_live_load_power_source": current_load_power_source,
                "current_live_pv_power_kw": (
                    round(current_pv_power_kw, 3)
                    if current_pv_power_kw is not None
                    else None
                ),
                "current_live_pv_power_age_seconds": (
                    round(current_pv_power_age_seconds, 1)
                    if current_pv_power_age_seconds is not None
                    else None
                ),
                "current_live_pv_power_source": current_pv_power_source,
                "gcf_enabled": gcf_enabled,
                "gcf_data_fresh": gcf_data_fresh,
                "gcf_age_seconds": (
                    round(gcf_age_seconds, 1)
                    if gcf_age_seconds is not None
                    else None
                ),
                "gcf_limit_data_fresh": gcf_limit_data_fresh,
                "gcf_limit_age_seconds": (
                    round(gcf_limit_sample.age_seconds, 1)
                    if gcf_limit_sample.age_seconds is not None
                    else None
                ),
                "gcf_execution_data_fresh": gcf_execution_data_fresh,
                "gcf_export_limit_percent": gcf_limit_percent,
                "gcf_export_power_cap_kw": (
                    round(export_power_cap_kw, 2)
                    if export_power_cap_kw is not None
                    else None
                ),
                "effective_export_power_source": effective_export_source,
                "avoided_import_price_pln_kwh": round(
                    avoided_import_price,
                    4,
                ),
                "battery_wear_cost_pln_kwh": round(
                    battery_wear_cost,
                    4,
                ),
                "charge_efficiency_percent": round(charge_efficiency, 1),
                "house_discharge_efficiency_percent": round(
                    discharge_efficiency,
                    1,
                ),
                "average_load_4d_kwh": round(average_load, 2),
                "average_night_load_4d_kwh": (
                    round(night_load, 2) if night_load is not None else None
                ),
                "pv_to_load_today_kwh": round(pv_to_load_today, 2),
                "battery_to_load_today_kwh": round(
                    battery_to_load_today,
                    2,
                ),
                "grid_to_load_today_kwh": round(grid_to_load_today, 2),
                "pv_to_load_power_kw": round(
                    max(pv_to_load_power_w, 0.0) / 1000.0,
                    3,
                ),
                "pv_self_consumption_share_percent": round(
                    min(
                        max(pv_to_load_power_w, 0.0)
                        / max(pv_total_power_w, 0.001)
                        * 100.0,
                        100.0,
                    ),
                    1,
                ),
                "home_pv_coverage_percent": round(
                    min(
                        max(pv_to_load_power_w, 0.0)
                        / max(load_power_w, 0.001)
                        * 100.0,
                        100.0,
                    ),
                    1,
                ),
                "inverter_count": inverter_count,
                "inverter_power_each_kw": rated_power,
                "market_slots": len(price_slots),
                "night_window": (
                    f"{night_start // 60:02d}:{night_start % 60:02d}"
                    f"–{night_end // 60:02d}:{night_end % 60:02d}"
                ),
            }
        )
        return (
            OptimizerInput(
                now=now,
                price_slots=price_slots,
                pv_by_slot_kwh=pv_by_slot,
                battery_capacity_kwh=required[
                    "sensor.hoymiles_hit_battery_capacity"
                ],
                battery_soc_percent=required[
                    "sensor.hoymiles_hit_overview_battery_soc"
                ],
                outage_reserve_soc_percent=required[
                    "sensor.hoymiles_hit_ems_self_use_soc_readback"
                ],
                safety_margin_soc_percent=required[
                    "input_number.hoymiles_rce_soc_safety_margin"
                ],
                manual_minimum_soc_percent=required[
                    "sensor.hoymiles_hit_ems_force_discharge_soc_readback"
                ],
                dynamic_reserve_enabled=self.hass.states.is_state(
                    "input_boolean.hoymiles_rce_dynamic_soc_enabled",
                    "on",
                ),
                average_daily_load_kwh=average_load,
                average_night_load_kwh=night_load,
                night_start_minute=night_start,
                night_end_minute=night_end,
                inverter_power_kw=rated_power,
                inverter_count=inverter_count,
                discharge_power_percent=required[
                    "input_number.hoymiles_rce_requested_discharge_power"
                ],
                export_efficiency_percent=required[
                    "input_number.hoymiles_rce_export_efficiency"
                ],
                bms_max_discharge_current_a=bms_current_sample.value,
                bms_max_charge_current_a=bms_charge_current_sample.value,
                battery_voltage_v=bms_voltage_sample.value,
                bms_discharge_data_fresh=bms_discharge_data_fresh,
                bms_discharge_data_age_seconds=(
                    bms_discharge_data_age_seconds
                ),
                bms_discharge_data_available=(
                    bms_discharge_data_available
                ),
                bms_charge_data_fresh=bms_charge_data_fresh,
                bms_charge_data_age_seconds=bms_charge_data_age_seconds,
                bms_charge_data_available=bms_charge_data_available,
                actual_day_load_today_kwh=(
                    self._load_history.current_day_energy_kwh
                ),
                pv_to_load_power_kw=max(pv_to_load_power_w, 0.0) / 1000.0,
                load_profile_30m_kwh=profile_history.average_profile_kwh,
                weekday_load_profile_30m_kwh=(
                    profile_history.weekday_profile_kwh
                ),
                weekend_load_profile_30m_kwh=(
                    profile_history.weekend_profile_kwh
                ),
                conservative_pv_by_slot_kwh=conservative_pv_by_slot,
                forecast_confidence_percent=forecast_confidence,
                export_power_cap_kw=export_power_cap_kw,
                effective_export_power_kw=effective_export_power_kw,
                avoided_import_price_pln_kwh=avoided_import_price,
                battery_wear_cost_pln_kwh=battery_wear_cost,
                day3_pv_forecast_kwh=day3_conservative,
                charge_efficiency_percent=charge_efficiency,
                house_discharge_efficiency_percent=discharge_efficiency,
                conservative_daily_load_kwh=conservative_daily_load,
                conservative_night_load_kwh=conservative_night_load,
                load_history_days=conservative_load_days,
                current_load_power_kw=current_load_power_kw,
                current_pv_power_kw=current_pv_power_kw,
                current_battery_soc_fresh=_age_minutes_is_fresh(
                    battery_age,
                    2.0,
                ),
                critical_zero_pv_guard=critical_zero_pv_guard,
                critical_zero_pv_guard_reason=critical_zero_pv_reason,
            ),
            metadata,
        )
