"""Home Assistant sensor exposing the optimized two-day RCE plan."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import fields, is_dataclass, replace
from datetime import date, datetime, time, timedelta
import logging
from math import isfinite
import re
from types import SimpleNamespace
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
    async_track_time_interval,
)
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .bounded_history import (
    RecorderHistoryLimitExceeded,
    RecorderHistoryQueryTimeout,
    async_get_bounded_state_reports,
)
from .const import (
    CONF_RESOLVED_SOURCE_DEVICE_ID,
    CONF_SOURCE_DEVICE_ID,
    DOMAIN,
    NAME,
)
from .energy_data import numeric_state_sample, state_age_seconds, state_reported_at
from .ems_shared_inputs import load_model_generated_at_is_fresh
from .forecast_model import (
    ForecastLearningPolicy,
    adaptive_forecast_factor,
    blend_low_expected,
    forecast_factor_for_policy,
    forecast_policy_for_source,
    forecast_learning_history_day_eligible,
    qualified_cumulative_energy_day,
    resolve_forecast_learning_policy,
    robust_weighted_factor,
    uncertainty_risk_weight,
)
from .models import RuntimeData
from .load_model import (
    current_day_profile_correction,
    daily_ages_days,
    expected_load_by_slot,
    persistent_load_delta_kw,
    robust_weighted_estimate,
)
from .load_history_store import (
    decode_cache,
    encode_cache,
    merge_history,
    qualification_diagnostics,
    source_identity,
)
from .optimizer_revision import (
    INPUT_RECALCULATION_DELAY_SECONDS,
    MAX_IMMEDIATE_RECALCULATIONS,
    OptimizerInputRevision,
    TARIFF_PRICE_BROKER_ATTRIBUTES,
    optimizer_input_fingerprint,
)
from .pv_forecast_usability import (
    SCHEDULED_PAUSE_MAX_SOURCE_AGE_SECONDS,
    SOLCAST_UPDATE_ENTITY_CANDIDATES,
    evaluate_pv_forecast_usefulness,
    resolve_solcast_update_state,
)
from .rce_history import (
    LOAD_HISTORY_ENTITIES,
    LOAD_PHASE_ENERGY_ENTITIES,
    LOAD_PROFILE_ENERGY_ENTITY,
    LoadHistorySummary,
    is_load_history_observation,
    parse_load_history_state,
    summarize_load_history,
)
from .rce_optimizer import (
    OptimizerInput,
    OptimizerResult,
    RceActiveCommitment,
    floor_half_hour,
    optimize_rce,
    parse_rce_rows,
    post_command_settling_market_fingerprint,
    revalidate_rce_plan,
    retain_active_rce_slot,
    robust_weighted_upper_estimate,
)
from .tariff_optimizer import TariffSchedule, tariff_rate
from .tariff_profiles import MANUAL_OPERATOR, get_tariff_profile, profile_is_valid


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
_LAST_COMPLETE_PLAN_GRACE_SECONDS = 15 * 60.0
RCE_STALE_RESULT_RETRY_DELAY_SECONDS = 5.0
LOAD_SHORT_LOOKBACK_DAYS = 5
LOAD_EXTENDED_LOOKBACK_DAYS = 31
LOAD_EXTENDED_CHUNK_DAYS = 7
LOAD_EXTENDED_TOTAL_BUDGET_SECONDS = 30.0
LOAD_HISTORY_RETRY_LIMIT = 3
LOAD_HISTORY_RETRY_BACKOFF = timedelta(hours=1)
LOAD_HISTORY_RETRY_COOLDOWN = timedelta(hours=6)
LOAD_HISTORY_STORE_VERSION = 1
LOAD_HISTORY_STORE_KEY_PREFIX = f"{DOMAIN}.qualified_load_history_v1"

# The HIT-20L-G3 nameplate describes total inverter AC capability.  Its
# battery-only Grid Discharge base used by RCE is 16 kW per inverter; PV,
# LOAD, charging, and every other configured inverter profile stay separate.
_HIT_20L_BATTERY_DISCHARGE_POWER_KW = 16.0


def _rce_inverter_discharge_power_kw(rated_power_kw: float) -> float:
    """Return the model-specific battery-only discharge base for RCE."""

    if abs(rated_power_kw - 20.0) < 0.01:
        return _HIT_20L_BATTERY_DISCHARGE_POWER_KW
    return rated_power_kw


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
    FORECAST_GCF_SUPPORT_ENTITY,
)

EMS_TODAY_FORECAST_ENTITY_HELPER = (
    "input_text.hoymiles_ems_pv_forecast_today_entity"
)
EMS_TOMORROW_FORECAST_ENTITY_HELPER = (
    "input_text.hoymiles_ems_pv_forecast_tomorrow_entity"
)
EMS_DAY3_FORECAST_ENTITY_HELPER = (
    "input_text.hoymiles_ems_pv_forecast_day_3_entity"
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
    EMS_TODAY_FORECAST_ENTITY_HELPER,
    EMS_TOMORROW_FORECAST_ENTITY_HELPER,
    EMS_DAY3_FORECAST_ENTITY_HELPER,
    TODAY_FORECAST_ENTITY_HELPER,
    TOMORROW_FORECAST_ENTITY_HELPER,
    DAY3_FORECAST_ENTITY_HELPER,
)

EMS_FALLBACK_DAILY_LOAD_HELPER = (
    "input_number.hoymiles_ems_fallback_daily_home_load"
)
LEGACY_FALLBACK_DAILY_LOAD_HELPER = (
    "input_number.hoymiles_rce_fallback_daily_load"
)
EMS_INVERTER_RATED_POWER_HELPER = (
    "input_select.hoymiles_ems_inverter_rated_power_each"
)
LEGACY_INVERTER_RATED_POWER_HELPER = (
    "input_select.hoymiles_rce_inverter_rated_power"
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
    EMS_FALLBACK_DAILY_LOAD_HELPER,
    LEGACY_FALLBACK_DAILY_LOAD_HELPER,
    "input_number.hoymiles_rce_requested_discharge_power",
    "input_number.hoymiles_rce_battery_wear_cost",
    "input_number.hoymiles_tariff_g11_price",
    "input_number.hoymiles_tariff_charge_efficiency",
    "input_number.hoymiles_tariff_discharge_efficiency",
    EMS_INVERTER_RATED_POWER_HELPER,
    LEGACY_INVERTER_RATED_POWER_HELPER,
    *FORECAST_ENTITY_HELPERS,
    *SOLCAST_UPDATE_ENTITY_CANDIDATES,
    "sun.sun",
    *TODAY_FORECAST_CANDIDATES,
    *TOMORROW_FORECAST_CANDIDATES,
    *DAY3_FORECAST_CANDIDATES,
    *REMAINING_TODAY_CANDIDATES,
}

# These continuously changing physical values are sampled by the bounded
# full-plan timer instead of withdrawing plan authority on every
# ESPHome state change.  The scheduler keeps its independent live SOC, BMS,
# export-permission and hardware-readback gates, so a real safety loss still
# stops execution immediately.  In particular, the Force Discharge readback
# is written by the RCE transaction itself and must not invalidate that same
# transaction before its mode write can be acknowledged.
RCE_FIVE_MINUTE_COALESCED_ENTITIES = {
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
    "sensor.hoymiles_hit_ems_self_use_soc_readback",
    "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
    "sensor.hoymiles_rce_effective_export_power",
    "sensor.hoymiles_rce_learned_export_power",
    "sun.sun",
    *TODAY_FORECAST_CANDIDATES,
    *TOMORROW_FORECAST_CANDIDATES,
    *DAY3_FORECAST_CANDIDATES,
    *REMAINING_TODAY_CANDIDATES,
}
# The full export planner is deliberately slower than the physical safety
# gates in the scheduler.  Live telemetry is sampled as one snapshot at this
# cadence; explicit user/market/forecast events remain on the existing short
# debounce path below.
RCE_FULL_OPTIMIZER_INTERVAL = timedelta(seconds=120)
RCE_GCF_OPTIMIZER_ENTITIES = frozenset(
    {
        FORECAST_GCF_ENABLE_ENTITY,
        FORECAST_GCF_LIMIT_ENTITY,
    }
)
RCE_EVENT_DRIVEN_ENTITIES = (
    WATCHED_ENTITIES
    - RCE_FIVE_MINUTE_COALESCED_ENTITIES
    - RCE_GCF_OPTIMIZER_ENTITIES
)

# These sampled numeric inputs may move while the optimizer is working. Their
# source, attributes and freshness remain guarded; their newest values must
# pass synchronous fixed-plan revalidation before publication. This is never
# permission to publish the original solver result after telemetry drift.
_RCE_REVALIDATED_NUMERIC_INPUTS = {
    "sensor.hoymiles_hit_overview_battery_soc": (120.0, 0.0, 100.0),
    "sensor.hoymiles_hit_battery_voltage_bms": (300.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_maximum_charge_current": (300.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_maximum_discharge_current": (300.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_ems_self_use_soc_readback": (300.0, 10.0, 100.0),
    "sensor.hoymiles_hit_ems_force_discharge_soc_readback": (180.0, 0.0, 100.0),
    "sensor.hoymiles_hit_number_of_machines_master_and_slave": (300.0, 1.0, 10.0),
    "sensor.hoymiles_hit_gcf_enable_readback_code": (180.0, 0.0, 1.0),
    "sensor.hoymiles_hit_gcf_maximum_export_power_readback": (180.0, -10.0, 200.0),
    "sensor.hoymiles_actual_load_power": (120.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_load_power_l1n": (120.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_load_power_l2n": (120.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_load_power_l3n": (120.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_overview_load_active_power": (120.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_overview_pv_total_power": (120.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_load_from_pv_power": (120.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_actual_load_energy_today": (300.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_pv_total_energy_today": (300.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_pv_to_load_energy_today": (300.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_energy_from_battery_today": (300.0, 0.0, 1_000_000_000.0),
    "sensor.hoymiles_hit_energy_from_grid_today": (300.0, 0.0, 1_000_000_000.0),
    **{entity_id: (300.0, 0.0, 1_000_000_000.0) for entity_id in LOAD_PHASE_ENERGY_ENTITIES},
}


def _rce_report_fingerprint(
    hass: HomeAssistant,
    fingerprint: tuple[Any, ...],
    now: datetime,
) -> tuple[Any, ...]:
    """Keep report provenance and capture whether its sample was usable."""

    result = []
    for entity_id, value in fingerprint:
        bounds = _RCE_REVALIDATED_NUMERIC_INPUTS.get(entity_id)
        if bounds is not None and value is not None:
            sample = numeric_state_sample(
                hass.states.get(entity_id), now,
                max_age_seconds=bounds[0], minimum=bounds[1], maximum=bounds[2],
            )
            value = (*value, sample.fresh)
        result.append((entity_id, value))
    return tuple(result)


def _rce_publication_fingerprints_match(
    captured: tuple[Any, ...], current: tuple[Any, ...], *, now: datetime,
) -> bool:
    """Guard identity/quality while allowing fresh inputs for revalidation."""

    if len(captured) != len(current):
        return False
    for (old_id, old), (new_id, new) in zip(captured, current, strict=True):
        if old_id != new_id:
            return False
        if old_id == "__rce_execution_gcf__":
            if not _rce_publication_fingerprints_match(old, new, now=now):
                return False
            continue
        if old_id == "__shared_ems_inputs__" and old is not None and new is not None:
            if old[0] != "revalidated_samples_v1" or new[0] != "revalidated_samples_v1":
                if old != new:
                    return False
                continue
            if old[1] != new[1] or len(old[2]) != len(new[2]):
                return False
            for before, after in zip(old[2], new[2], strict=True):
                path, old_report, fresh, ttl, timestamp_required = before
                if (path, fresh, ttl, timestamp_required) != (after[0], *after[2:]):
                    return False
                new_report = after[1]
                if not fresh:
                    if old_report != new_report:
                        return False
                    continue
                if old_report is None or new_report is None:
                    if timestamp_required or old_report != new_report:
                        return False
                    continue
                try:
                    if (
                        old_report.utcoffset() is None or new_report.utcoffset() is None
                        or new_report < old_report
                        or not all(
                            (now - report).total_seconds() >= -5.0
                            and (ttl is None or (now - report).total_seconds() <= ttl)
                            for report in (old_report, new_report)
                        )
                    ):
                        return False
                except (AttributeError, TypeError, ValueError):
                    return False
            continue
        bounds = _RCE_REVALIDATED_NUMERIC_INPUTS.get(old_id)
        if bounds is None or old is None or new is None:
            if old != new:
                return False
            continue
        if old == new and old[3] is not True:
            # Unusable/missing optional inputs keep their existing fallback
            # semantics. They cannot enter the equivalent-refresh exception.
            continue
        if old[1] != new[1] or old[3] is not True or new[3] is not True:
            return False
        if (
            old_id in {
                "sensor.hoymiles_hit_ems_self_use_soc_readback",
                "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
                "sensor.hoymiles_hit_number_of_machines_master_and_slave",
                "sensor.hoymiles_hit_gcf_enable_readback_code",
                "sensor.hoymiles_hit_gcf_maximum_export_power_readback",
            }
            and old[0] != new[0]
        ):
            return False
        try:
            before = datetime.fromisoformat(old[2])
            after = datetime.fromisoformat(new[2])
            if (
                before.tzinfo is None or after.tzinfo is None
                or before.utcoffset() is None or after.utcoffset() is None
                or after < before
            ):
                return False
            for raw, reported in ((old[0], before), (new[0], after)):
                sample = numeric_state_sample(
                    SimpleNamespace(state=raw, last_reported=reported), now,
                    max_age_seconds=bounds[0], minimum=bounds[1], maximum=bounds[2],
                )
                if not sample.fresh:
                    return False
        except (TypeError, ValueError, IndexError):
            return False
    return True


def _forecast_learning_policy_snapshot(
    hass: HomeAssistant,
    now: datetime,
    runtime: RuntimeData | None = None,
) -> tuple[ForecastLearningPolicy, dict[str, Any]]:
    """Read one coherent, physically verified GCF learning policy."""

    def sample(
        field_name: str,
        entity_id: str,
        max_age_seconds: float,
        minimum: float,
        maximum: float,
    ) -> Any:
        if runtime is None:
            return numeric_state_sample(
                hass.states.get(entity_id),
                now,
                max_age_seconds=max_age_seconds,
                minimum=minimum,
                maximum=maximum,
            )
        return _policy_numeric_sample(
            runtime,
            "gcf",
            field_name,
            hass.states.get(entity_id),
            now,
            max_age_seconds=max_age_seconds,
            minimum=minimum,
            maximum=maximum,
        )

    support = sample(
        "hardware_readback_supported",
        FORECAST_GCF_SUPPORT_ENTITY,
        _FORECAST_GCF_SUPPORT_MAX_AGE_SECONDS,
        0.0,
        1.0,
    )
    generation = sample(
        "generation",
        FORECAST_GCF_GENERATION_ENTITY,
        _FORECAST_GCF_READBACK_MAX_AGE_SECONDS,
        1.0,
        16_000_000.0,
    )
    enable = sample(
        "enable_code",
        FORECAST_GCF_ENABLE_ENTITY,
        _FORECAST_GCF_READBACK_MAX_AGE_SECONDS,
        0.0,
        1.0,
    )
    export_limit = sample(
        "maximum_export_power_percent",
        FORECAST_GCF_LIMIT_ENTITY,
        _FORECAST_GCF_READBACK_MAX_AGE_SECONDS,
        -10.0,
        200.0,
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
    """Return the stable physical GCF values consumed by RCE planning."""

    return (
        *_forecast_learning_policy_signature(policy),
        diagnostics.get("forecast_learning_gcf_enable_code"),
        diagnostics.get("forecast_learning_gcf_export_limit_percent"),
    )


def _forecast_gcf_publication_signature(
    policy: ForecastLearningPolicy,
    diagnostics: Mapping[str, Any],
) -> tuple[
    bool,
    str,
    str | None,
    float | None,
    float | None,
    float | None,
    float | None,
]:
    """Bind solver publication to one coherent physical FC03 generation."""

    return (
        *_forecast_gcf_optimizer_signature(policy, diagnostics),
        diagnostics.get("forecast_learning_gcf_generation"),
    )


def _live_forecast_gcf_optimizer_signature(
    hass: HomeAssistant,
    runtime: RuntimeData | None,
) -> tuple[
    bool,
    str,
    str | None,
    float | None,
    float | None,
    float | None,
    float | None,
]:
    """Read one coherent GCF signature for an executor publication guard."""

    policy, diagnostics = _forecast_learning_policy_snapshot(
        hass,
        dt_util.now(),
        runtime,
    )
    return _forecast_gcf_publication_signature(policy, diagnostics)


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


_SHARED_INPUT_MISSING = object()


def _shared_inputs_snapshot(runtime: RuntimeData) -> Any | None:
    """Return the immutable same-entry broker snapshot when it is installed."""
    return getattr(getattr(runtime, "shared_inputs", None), "snapshot", None)


def _shared_input_field(
    runtime: RuntimeData,
    section_name: str,
    field_name: str,
) -> Any:
    """Read one broker field without treating an explicit ``None`` as absent."""

    return getattr(
        getattr(
            _shared_inputs_snapshot(runtime),
            section_name,
            _SHARED_INPUT_MISSING,
        ),
        field_name,
        _SHARED_INPUT_MISSING,
    )


def _shared_sample_value(
    runtime: RuntimeData,
    section_name: str,
    field_name: str,
) -> Any:
    """Return a validated broker sample value or the absent-field sentinel."""

    sample = _shared_input_field(runtime, section_name, field_name)
    if sample is _SHARED_INPUT_MISSING:
        return _SHARED_INPUT_MISSING
    return getattr(sample, "value", sample)


def _policy_numeric_sample(
    runtime: RuntimeData,
    section_name: str,
    field_name: str,
    legacy_state: State | None,
    now: datetime,
    *,
    max_age_seconds: float,
    shared_scale: float = 1.0,
    legacy_scale: float = 1.0,
    minimum: float | None = None,
    maximum: float | None = None,
    future_tolerance_seconds: float = 5.0,
) -> Any:
    """Apply one policy's legacy bounds to an entry-local broker sample."""

    shared = _shared_input_field(runtime, section_name, field_name)
    if shared is _SHARED_INPUT_MISSING:
        return numeric_state_sample(
            legacy_state,
            now,
            max_age_seconds=max_age_seconds,
            scale=legacy_scale,
            minimum=minimum,
            maximum=maximum,
            future_tolerance_seconds=future_tolerance_seconds,
        )
    reported_at = getattr(shared, "reported_at", None)
    proxy = SimpleNamespace(
        state=getattr(shared, "value", None),
        last_reported=reported_at,
        last_updated=reported_at,
    )
    return numeric_state_sample(
        proxy,
        now,
        max_age_seconds=max_age_seconds,
        scale=shared_scale,
        minimum=minimum,
        maximum=maximum,
        future_tolerance_seconds=future_tolerance_seconds,
    )


def _policy_stable_number(
    runtime: RuntimeData,
    section_name: str,
    field_name: str,
    hass: HomeAssistant,
    legacy_entity_id: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    shared_scale: float = 1.0,
    legacy_scale: float = 1.0,
) -> float | None:
    """Prefer the broker while preserving a policy's no-age stable setting."""

    shared = _shared_sample_value(runtime, section_name, field_name)
    candidates = ((shared, shared_scale),) if shared is not _SHARED_INPUT_MISSING else ()
    candidates += ((_state_number(hass, legacy_entity_id), legacy_scale),)
    for raw_value, scale in candidates:
        if type(raw_value) not in {int, float}:
            continue
        value = float(raw_value) * float(scale)
        if not isfinite(value):
            continue
        if minimum is not None and value < minimum:
            continue
        if maximum is not None and value > maximum:
            continue
        return value
    return None


def _shared_optimizer_signature(runtime: RuntimeData) -> tuple[Any, ...] | None:
    """Guard Shared identity/configuration/quality, revalidate live values."""

    snapshot = _shared_inputs_snapshot(runtime)
    if snapshot is None:
        return None
    if not is_dataclass(snapshot):
        # Unknown broker contracts cannot receive the sampled-value exception.
        return (
            getattr(snapshot, "schema_version", None),
            getattr(snapshot, "config_entry_id", None),
            getattr(snapshot, "revision", None),
        )

    samples = []

    def freeze(value: Any, path: tuple[str, ...] = ()) -> Any:
        if is_dataclass(value):
            result = []
            sampled = hasattr(value, "reported_at") and hasattr(value, "fresh")
            if sampled:
                # Broker samples carry entry-local source IDs. Guard their
                # actual report clocks as well as canonical compatibility IDs.
                ttl = 300.0
                if path[:1] == ("forecast",):
                    ttl = 18.0 * 3600.0
                    usefulness = getattr(value, "usefulness", None)
                    if (
                        isinstance(usefulness, Mapping)
                        and usefulness.get("mode") == "scheduled_pause"
                        and usefulness.get("usable") is True
                        and usefulness.get("source_fresh") is False
                    ):
                        # Broker freshness means bounded usefulness overnight,
                        # not that an old forecast acquired a new report clock.
                        try:
                            deadline = datetime.fromisoformat(usefulness["valid_until"])
                            if deadline.utcoffset() is None:
                                raise ValueError("naive forecast deadline")
                            ttl = min(
                                SCHEDULED_PAUSE_MAX_SOURCE_AGE_SECONDS,
                                (deadline - value.reported_at).total_seconds(),
                            )
                        except (KeyError, TypeError, ValueError):
                            ttl = 0.0
                elif path[:1] == ("power",) or path == ("system", "battery_soc_percent"):
                    ttl = 120.0
                elif path[:1] == ("gcf",):
                    ttl = 10.0 if path[-1] == "hardware_readback_supported" else 180.0
                elif path in {
                    ("system", "inverter_rated_power_each_kw"),
                    ("system", "system_rated_power_kw"),
                    ("load", "fallback_daily_home_load_kwh"),
                    ("efficiency", "pv_to_battery_efficiency"),
                    ("efficiency", "battery_to_home_efficiency"),
                }:
                    # Shared _setting_sample keeps valid user settings fresh
                    # indefinitely. The derived system rating includes that
                    # helper clock; its physical count is guarded separately.
                    ttl = None
                samples.append((
                    path, value.reported_at, value.fresh, ttl,
                    ttl is not None and bool(
                        getattr(value, "entity_id", None)
                        or getattr(value, "source_entity_ids", ())
                    ),
                ))
            for field in fields(value):
                key = field.name
                if sampled and key in {"reported_at", "age_seconds"}:
                    continue
                if not path and key in {"captured_at", "revision"}:
                    continue
                if key == "value" and (
                    path[:1] in {("bms",), ("power",)}
                    or path == ("system", "battery_soc_percent")
                ):
                    continue
                if path == ("load",) and key in {
                    "current_day_energy_kwh", "current_day_observed_at",
                    "persistence_delta_kw", "persistence_observed_at",
                    "persistence_sample_count",
                }:
                    continue
                result.append((key, freeze(getattr(value, key), (*path, key))))
            return tuple(result)
        if isinstance(value, Mapping):
            return tuple(sorted(
                (key, freeze(item, (*path, str(key))))
                for key, item in value.items()
                if not (path[:1] == ("forecast",) and path[-1:] == ("usefulness",)
                        and key == "age_seconds")
            ))
        if isinstance(value, (list, tuple)):
            return tuple(freeze(item, path) for item in value)
        return value

    semantic = freeze(snapshot)
    return ("revalidated_samples_v1", semantic, tuple(samples))


def _preferred_text_helper(
    hass: HomeAssistant,
    new_entity_id: str,
    legacy_entity_id: str,
) -> str:
    """Use the neutral helper when set, otherwise retain the legacy fallback."""

    value = _state_text(hass, new_entity_id).strip()
    return value if value else _state_text(hass, legacy_entity_id).strip()


def _preferred_number_helper(
    hass: HomeAssistant,
    new_entity_id: str,
    legacy_entity_id: str,
) -> float | None:
    """Resolve a numeric neutral helper before its legacy compatibility alias."""

    state = hass.states.get(new_entity_id)
    if state is not None and state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
        return _state_number(hass, new_entity_id)
    return _state_number(hass, legacy_entity_id)


def _preferred_select_number(
    hass: HomeAssistant,
    new_entity_id: str,
    legacy_entity_id: str,
) -> float | None:
    """Resolve a neutral select, preserving ``Automatycznie`` as no override."""

    state = hass.states.get(new_entity_id)
    if state is not None and state.state not in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
        return _select_number(hass, new_entity_id)
    return _select_number(hass, legacy_entity_id)


def _resolved_forecast_entity_id(
    hass: HomeAssistant,
    runtime: RuntimeData,
    *,
    broker_field: str,
    new_helper: str,
    legacy_helper: str,
) -> str:
    """Resolve a forecast source from the same-entry broker or helper fallback."""

    sample = _shared_input_field(runtime, "forecast", broker_field)
    if sample is not _SHARED_INPUT_MISSING:
        entity_id = getattr(sample, "entity_id", None)
        return (
            entity_id.strip().lower()
            if isinstance(entity_id, str) and _ENTITY_ID.fullmatch(entity_id.strip().lower())
            else ""
        )
    configured = _preferred_text_helper(hass, new_helper, legacy_helper).lower()
    return configured if _ENTITY_ID.fullmatch(configured) else ""


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


def _configured_forecast_entity_ids(
    hass: HomeAssistant,
    runtime: RuntimeData | None = None,
) -> frozenset[str]:
    """Return valid source entity IDs selected through forecast helpers.

    The helpers may point at user-created template sensors whose IDs cannot be
    known when the integration is loaded.  Keeping these IDs in both the event
    subscription and optimizer fingerprint prevents a published plan from
    remaining execution-current after its real source changes.
    """
    if runtime is not None and _shared_inputs_snapshot(runtime) is not None:
        return frozenset(
            entity_id
            for field_name in ("today", "tomorrow", "day3")
            if (
                isinstance(
                    entity_id := getattr(
                        _shared_input_field(runtime, "forecast", field_name),
                        "entity_id",
                        None,
                    ),
                    str,
                )
                and _ENTITY_ID.fullmatch(entity_id)
            )
        )
    return frozenset(
        entity_id
        for new_helper, legacy_helper in (
            (EMS_TODAY_FORECAST_ENTITY_HELPER, TODAY_FORECAST_ENTITY_HELPER),
            (EMS_TOMORROW_FORECAST_ENTITY_HELPER, TOMORROW_FORECAST_ENTITY_HELPER),
            (EMS_DAY3_FORECAST_ENTITY_HELPER, DAY3_FORECAST_ENTITY_HELPER),
        )
        if (
            (
                entity_id := _preferred_text_helper(
                    hass,
                    new_helper,
                    legacy_helper,
                ).strip().lower()
            )
            and _ENTITY_ID.fullmatch(entity_id)
        )
    )


def _parse_datetime(value: Any, timezone: ZoneInfo) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = dt_util.parse_datetime(value)
    else:
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone)
    return parsed.astimezone(timezone)


def _rce_rows_for_local_date(
    rows: Any,
    target_date: date,
    timezone: ZoneInfo,
) -> list[Mapping[str, Any]]:
    """Return only market quarters that belong to one local business day."""

    if not isinstance(rows, list):
        return []
    matched: list[Mapping[str, Any]] = []
    for item in rows:
        if not isinstance(item, Mapping):
            continue
        raw_business_date = str(item.get("business_date", "")).strip()
        if raw_business_date == target_date.isoformat():
            matched.append(item)
            continue
        if raw_business_date:
            continue
        # PSE ``dtime_utc`` is the end of the 15-minute settlement interval.
        # Resolve the absolute quarter start before assigning a local date;
        # ``period_utc`` alone is only a clock range and cannot prove a day.
        raw_interval_end = item.get("dtime_utc")
        if isinstance(raw_interval_end, datetime):
            interval_end = raw_interval_end
        elif isinstance(raw_interval_end, str) and raw_interval_end.strip():
            interval_end = dt_util.parse_datetime(raw_interval_end.strip())
        else:
            interval_end = None
        if interval_end is None:
            continue
        if interval_end.tzinfo is None:
            interval_end = interval_end.replace(tzinfo=dt_util.UTC)
        quarter_start = (
            interval_end.astimezone(dt_util.UTC) - timedelta(minutes=15)
        ).astimezone(timezone)
        if quarter_start.date() == target_date:
            matched.append(item)
    return matched


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


def _select_current_rce_price_rows(
    *,
    primary_rows: Any,
    primary_age_seconds: float | None,
    rollover_rows: Any,
    rollover_age_seconds: float | None,
    target_date: date,
    timezone: ZoneInfo,
) -> tuple[
    list[Mapping[str, Any]],
    bool,
    int,
    int,
    bool,
    float | None,
    str,
]:
    """Select a complete current day, including the prior tomorrow payload."""

    candidates = []
    for source_role, rows, age_seconds in (
        ("today", primary_rows, primary_age_seconds),
        ("previous_tomorrow_rollover", rollover_rows, rollover_age_seconds),
    ):
        matched = _rce_rows_for_local_date(rows, target_date, timezone)
        complete, half_hours, expected_half_hours = (
            _complete_rce_half_hours_for_local_date(
                matched,
                target_date,
                timezone,
            )
        )
        fresh = bool(
            complete
            and age_seconds is not None
            and -5.0 <= age_seconds <= _RCE_PRICE_MAX_AGE_SECONDS
        )
        candidates.append(
            (
                matched,
                complete,
                half_hours,
                expected_half_hours,
                fresh,
                age_seconds,
                source_role,
            )
        )
    return next(
        (candidate for candidate in candidates if candidate[4]),
        next(
            (candidate for candidate in candidates if candidate[0]),
            candidates[0],
        ),
    )


def _rce_state_rows_and_age(
    state: State | None,
    now: datetime,
) -> tuple[Any, float | None]:
    """Return REST rows only while their HA source is available."""

    if state is None or state.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}:
        return [], None
    return state.attributes.get("value", []), state_age_seconds(state, now)


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
    runtime: RuntimeData | None = None,
    shared_field: str | None = None,
) -> tuple[float | None, float | None, str]:
    """Return a fresh non-negative live power in kW and diagnostics."""
    sample = (
        _policy_numeric_sample(
            runtime,
            "power",
            shared_field,
            hass.states.get(entity_id),
            now,
            max_age_seconds=max_age_seconds,
            shared_scale=1.0,
            legacy_scale=0.001,
            minimum=0.0,
        )
        if runtime is not None and shared_field is not None
        else numeric_state_sample(
            hass.states.get(entity_id),
            now,
            max_age_seconds=max_age_seconds,
            scale=0.001,
            minimum=0.0,
        )
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
    # The plan/profile arrays are available live. Re-recording them can exceed
    # HA's attribute-size limit and discard even the small lifecycle evidence.
    _unrecorded_attributes = frozenset({
        "planned_slots", "recorder_load_daily_kwh", "recorder_load_recent_4d_kwh",
        "recorder_load_profile_30m_kwh", "recorder_load_average_profile_30m_kwh",
        "recorder_load_weekday_profile_30m_kwh", "recorder_load_weekend_profile_30m_kwh",
        "recorder_load_daily_quality", "recorder_load_profile_quality",
        "recorder_load_phase_quality", "recorder_load_phase_diagnostics",
        "recorder_load_availability_diagnostics",
        "recorder_night_quality",
        "recorder_night_daily_kwh", "recorder_night_daily_kwh_28d",
        "bms_discharge_data_age_seconds", "bms_charge_data_age_seconds",
        "current_live_load_power_age_seconds", "current_live_pv_power_age_seconds",
        "inverter_count_age_seconds", "self_use_soc_age_seconds", "soc_data_age_seconds",
        "rce_today_age_seconds", "rce_today_age_minutes",
        "rce_tomorrow_age_seconds", "rce_tomorrow_age_minutes",
        "battery_soc_age_minutes", "solver_runtime_ms", "shadow_runtime_ms",
    })
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
        self._post_command_settling_market_settings: OptimizerInput | None = None
        self._load_history = _empty_load_summary()
        self._extended_load_history = _empty_load_summary()
        self._load_history_store: Store[dict[str, Any]] = Store(
            hass,
            LOAD_HISTORY_STORE_VERSION,
            f"{LOAD_HISTORY_STORE_KEY_PREFIX}.{entry.entry_id}",
        )
        self._load_history_store_payload: dict[str, Any] | None = None
        self._full_history_refresh_date: date | None = None
        self._load_profile_generated_at: datetime | None = None
        self._load_history_last_attempt_at: datetime | None = None
        self._load_history_last_success_at: datetime | None = None
        self._load_history_retry_count = 0
        self._load_history_next_retry_at: datetime | None = None
        self._load_history_retry_epoch_date: date | None = None
        self._load_history_read_status = "not_started"
        self._load_history_read_error: str | None = None
        self._load_history_short_range: tuple[datetime, datetime] | None = None
        self._load_history_extended_range: tuple[datetime, datetime] | None = None
        self._load_history_extended_partial = False
        self._load_power_observations: deque[
            tuple[datetime, float, float]
        ] = deque(maxlen=240)
        self._load_persistence_delta_kw = 0.0
        self._load_persistence_observed_at: datetime | None = None
        self._load_persistence_sample_count = 0
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
        self._stale_result_retry_cancel = None
        self._shared_inputs_dirty = False
        self._shared_inputs_seen = False
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
        self._full_plan_rejected_for_input_drift = False
        self._full_plan_solver_calls = 0
        self._last_full_plan_at: datetime | None = None
        self._full_plan_trigger = "startup"
        self._current_slot_continue_eligible: bool | None = None
        self._current_slot_continue_changed_at: datetime | None = None
        self._tariff_plan_source: SensorEntity | None = None
        self._active_commitment_source = None
        self._tariff_price_source: SensorEntity | None = None
        self._timeline_sensor: Any | None = None
        self._timeline_metadata: dict[str, Any] = {}
        self._attributes: dict[str, Any] = {
            "status_code": "missing_data",
            "missing_entities": [],
            "planned_slots": [],
            "result_current": False,
            "recalculation_pending": True,
            "input_revision": 0,
            "full_plan_solver_calls": 0,
            "current_slot_load_exhausts_requested_discharge_budget": False,
            "post_command_settling_market_fingerprint": None,
            "last_full_plan_at": None,
            "last_full_plan_trigger": "startup",
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

    def attach_supervisor_commitment_source(self, supervisor: SensorEntity) -> None:
        """Bind the authoritative same-entry execution source, as tariff does."""
        source_entry = getattr(supervisor, "_entry", None)
        reader = getattr(supervisor, "current_rce_active_commitment", None)
        if (source_entry is None or source_entry.entry_id != self._entry.entry_id
            or not callable(reader) or self._active_commitment_source is not None):
            raise RuntimeError("invalid or duplicate RCE commitment source")
        self._active_commitment_source = reader

    def _active_rce_commitment(self, now: datetime) -> RceActiveCommitment | None:
        reader = getattr(self, "_active_commitment_source", None)
        try:
            value = reader(now) if reader is not None else None
            return value if isinstance(value, RceActiveCommitment) else None
        except (AttributeError, TypeError, ValueError):
            return None

    def attach_tariff_price_source(self, tariff_price_source: SensorEntity) -> None:
        """Bind the exact same-entry R07 price publication for R08 only."""

        source_entry = getattr(tariff_price_source, "_entry", None)
        if source_entry is None or source_entry.entry_id != self._entry.entry_id:
            raise RuntimeError("RCE tariff price source belongs to another entry")
        self._tariff_price_source = tariff_price_source

    def _load_model_values(
        self,
        now: datetime,
        *,
        use_shared_inputs: bool = True,
    ) -> dict[str, Any]:
        """Return one 28-day, gap-aware LOAD baseline without Recorder I/O."""
        recorded_profile_history = (
            self._extended_load_history
            if self._extended_load_history.daily_history_days
            else self._load_history
        )
        daily_keys = tuple(recorded_profile_history.daily_energy_kwh)
        daily_values = tuple(recorded_profile_history.daily_energy_kwh.values())
        ages = daily_ages_days(daily_keys, as_of=now.date())
        history_load, uncertainty, history_days = robust_weighted_estimate(
            daily_values,
            ages_days=ages,
        )
        load_history_source = "recorder_phase_counters_28d"
        if history_load is None:
            # A statistics sensor using ``sum_differences_nonnegative`` on a
            # daily-reset counter can count restored/startup jumps as energy.
            # It is useful for diagnostics, but it is not safe control input.
            history_days = 0
            load_history_source = "no_valid_recorder_history"

        shared_fallback = (
            _shared_sample_value(
                getattr(self, "_runtime", None),
                "load",
                "fallback_daily_home_load_kwh",
            )
            if use_shared_inputs
            else _SHARED_INPUT_MISSING
        )
        if shared_fallback is _SHARED_INPUT_MISSING:
            fallback_load = _preferred_number_helper(
                self.hass,
                EMS_FALLBACK_DAILY_LOAD_HELPER,
                LEGACY_FALLBACK_DAILY_LOAD_HELPER,
            )
        else:
            fallback_load = (
                float(shared_fallback)
                if type(shared_fallback) in {int, float}
                and isfinite(float(shared_fallback))
                else None
            )
        history_data_fresh = load_model_generated_at_is_fresh(
            self._load_profile_generated_at,
            now=now,
        )
        history_complete = history_days >= 3 and history_data_fresh
        if history_load is not None and history_complete:
            average_load = history_load
            source = "history_28d_gap_weighted"
        elif history_load is None and fallback_load is not None:
            average_load = fallback_load
            source = "configured_daily_fallback"
        else:
            candidates = [
                value
                for value in (history_load, fallback_load)
                if value is not None and isfinite(value) and value >= 0.0
            ]
            average_load = max(candidates) if candidates else None
            source = (
                "stale_history_or_fallback"
                if history_load is not None and not history_data_fresh
                else "provisional_history_or_fallback"
            )

        if (
            history_data_fresh
            and self._load_history.average_night_kwh is not None
        ):
            night_load = self._load_history.average_night_kwh
            night_days = self._load_history.night_history_days
        else:
            # Do not import the same reset-sensitive statistics aggregate for
            # the protected night window. The optimizer will derive its safe
            # provisional night demand from the configured daily fallback.
            night_load = None
            night_days = 0

        actual_state = self.hass.states.get(
            "sensor.hoymiles_actual_load_energy_today"
        )
        actual_energy: float | None = None
        actual_observed_at: datetime | None = None
        if actual_state is not None:
            try:
                parsed = float(actual_state.state)
            except (TypeError, ValueError, OverflowError):
                parsed = float("nan")
            if isfinite(parsed) and parsed >= 0.0:
                actual_energy = parsed
                actual_observed_at = state_reported_at(actual_state)
                if actual_observed_at is not None:
                    actual_observed_at = actual_observed_at.astimezone(now.tzinfo)

        profile_history = (
            recorded_profile_history
            if history_data_fresh
            else _empty_load_summary()
        )
        diagnostic_history = merge_history(
            self._extended_load_history,
            self._load_history,
            limit=LOAD_EXTENDED_LOOKBACK_DAYS,
        )
        return {
            "history_load": history_load,
            "load_history_days": float(history_days),
            "load_history_source": load_history_source,
            "load_uncertainty_ratio": uncertainty,
            "average_night_load": night_load,
            "night_history_days": float(night_days),
            "fallback_load": fallback_load,
            "actual_load_today": actual_energy,
            "actual_load_observed_at": actual_observed_at,
            "live_daily_projection": None,
            "history_complete": history_complete,
            "history_data_fresh": history_data_fresh,
            "average_load": average_load,
            "load_model_source": source,
            "profile_history": profile_history,
            "diagnostic_history": diagnostic_history,
            "daily_total_dates": daily_keys,
        }

    def _update_load_persistence(
        self,
        now: datetime,
        values: Mapping[str, Any],
    ) -> None:
        """Update a bounded causal power-deviation window from fresh telemetry."""
        average_load = values.get("average_load")
        if average_load is None:
            return
        state = self.hass.states.get("sensor.hoymiles_actual_load_power")
        if state is None:
            return
        try:
            actual_kw = float(state.state) / 1000.0
        except (TypeError, ValueError, OverflowError):
            return
        observed_at = state_reported_at(state)
        if (
            observed_at is None
            or not isfinite(actual_kw)
            or actual_kw < 0.0
            or abs((now - observed_at.astimezone(now.tzinfo)).total_seconds()) > 180.0
        ):
            return
        profile: LoadHistorySummary = values["profile_history"]
        slot = floor_half_hour(observed_at.astimezone(now.tzinfo))
        base = expected_load_by_slot(
            (slot,),
            now=now,
            daily_energy_kwh=float(average_load),
            average_profile_30m_kwh=profile.average_profile_kwh,
            weekday_profile_30m_kwh=profile.weekday_profile_kwh,
            weekend_profile_30m_kwh=profile.weekend_profile_kwh,
        ).by_slot_kwh.get(slot, 0.0) * 2.0
        stamp = observed_at.astimezone(now.tzinfo)
        if not self._load_power_observations or self._load_power_observations[-1][0] != stamp:
            self._load_power_observations.append((stamp, actual_kw, base))
        cutoff = now - timedelta(minutes=20)
        while self._load_power_observations and self._load_power_observations[0][0] < cutoff:
            self._load_power_observations.popleft()
        (
            self._load_persistence_delta_kw,
            self._load_persistence_observed_at,
            self._load_persistence_sample_count,
        ) = persistent_load_delta_kw(tuple(self._load_power_observations), now=now)

    def shared_load_model_snapshot(self, now: datetime) -> Mapping[str, Any]:
        """Publish the single policy-neutral LOAD model through the broker."""
        timezone = ZoneInfo(self.hass.config.time_zone)
        local_now = now.astimezone(timezone)
        values = self._load_model_values(local_now, use_shared_inputs=False)
        self._update_load_persistence(local_now, values)
        profile: LoadHistorySummary = values["profile_history"]
        generated_at = self._load_profile_generated_at
        return {
            "average_daily_home_load_kwh": values["average_load"],
            "average_night_home_load_kwh": values["average_night_load"],
            "provisional_daily_load_projection_kwh": None,
            "daily_history_days": profile.daily_history_days,
            "night_history_days": self._load_history.night_history_days,
            "daily_totals_kwh": tuple(profile.daily_energy_kwh.values()),
            "daily_total_dates": tuple(profile.daily_energy_kwh),
            "average_profile_30m_kwh": tuple(profile.average_profile_kwh),
            "weekday_profile_30m_kwh": tuple(profile.weekday_profile_kwh),
            "weekend_profile_30m_kwh": tuple(profile.weekend_profile_kwh),
            "weekday_profile_days": profile.weekday_profile_days,
            "weekend_profile_days": profile.weekend_profile_days,
            "profile_history_days": profile.profile_history_days,
            "daily_coverage_ratio": profile.daily_coverage_ratio,
            "profile_coverage_ratio": profile.profile_coverage_ratio,
            "current_day_energy_kwh": values["actual_load_today"],
            "current_day_observed_at": values["actual_load_observed_at"],
            "persistence_delta_kw": self._load_persistence_delta_kw,
            "persistence_observed_at": self._load_persistence_observed_at,
            "persistence_sample_count": self._load_persistence_sample_count,
            "model_schema": "load_forecast_v2",
            "model_quality": (
                "complete" if values["history_complete"] and profile.profile_history_days else "fallback"
            ),
            "fallback_currently_used": bool(
                not values["history_complete"]
                and values["fallback_load"] is not None
                and values["average_load"] == values["fallback_load"]
            ),
            "generated_at": generated_at,
            "ready": bool(values["average_load"] is not None and generated_at is not None),
            "source": "recorder_phase_counters_and_actual_load",
        }

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

    def current_post_command_settling_market_fingerprint(self) -> str | None:
        """Recheck the accepted market basis against actual HA sources.

        A desired-power replan may be pending without changing this basis.
        This hash is only evidence of unchanged economics, never authority.
        Missing required policy sources do not inherit display defaults.
        """

        accepted = self._post_command_settling_market_settings
        if self._lifecycle_stopped or accepted is None:
            return None
        accepted_hash = post_command_settling_market_fingerprint(accepted)
        if accepted_hash is None:
            return None
        timezone = ZoneInfo(self.hass.config.time_zone)
        now = dt_util.now().astimezone(timezone)
        if now.date() != accepted.now.date():
            return None
        today_payload, today_age = _rce_state_rows_and_age(
            self.hass.states.get("sensor.hoymiles_rce_day"), now
        )
        tomorrow_payload, tomorrow_age = _rce_state_rows_and_age(
            self.hass.states.get("sensor.hoymiles_rce_day_tomorrow"), now
        )
        selected = _select_current_rce_price_rows(
            primary_rows=today_payload,
            primary_age_seconds=today_age,
            rollover_rows=tomorrow_payload,
            rollover_age_seconds=tomorrow_age,
            target_date=now.date(),
            timezone=timezone,
        )
        if not selected[4]:
            return None
        tomorrow_rows = _rce_rows_for_local_date(
            tomorrow_payload, now.date() + timedelta(days=1), timezone
        )
        tomorrow_complete = _complete_rce_half_hours_for_local_date(
            tomorrow_rows, now.date() + timedelta(days=1), timezone
        )[0]
        # Read the selector itself, not a broker snapshot awaiting its callback.
        tomorrow_configured = _preferred_text_helper(
            self.hass,
            EMS_TOMORROW_FORECAST_ENTITY_HELPER,
            TOMORROW_FORECAST_ENTITY_HELPER,
        ).lower()
        _, tomorrow_forecast = _first_numeric_state(
            self.hass, TOMORROW_FORECAST_CANDIDATES, tomorrow_configured
        )
        use_tomorrow = bool(
            tomorrow_complete
            and tomorrow_age is not None
            and -5.0 <= tomorrow_age <= _RCE_PRICE_MAX_AGE_SECONDS
            and numeric_state_sample(
                tomorrow_forecast,
                now,
                max_age_seconds=_TOMORROW_FORECAST_MAX_AGE_SECONDS,
                minimum=0.0,
            ).fresh
        )
        block_state = self.hass.states.get(
            "input_boolean.hoymiles_sale_block_enabled"
        )
        block_start = _helper_minutes(
            self.hass, "input_datetime.hoymiles_sale_block_start"
        )
        block_end = _helper_minutes(
            self.hass, "input_datetime.hoymiles_sale_block_end"
        )
        if (
            block_state is None
            or block_state.state not in {"on", "off"}
            or block_start is None
            or block_end is None
            or not 0 <= block_start < 1440
            or not 0 <= block_end < 1440
        ):
            return None
        prices = parse_rce_rows(
            [*selected[0], *(tomorrow_rows if use_tomorrow else [])],
            timezone,
            block_enabled=block_state.state == "on",
            block_start_minute=block_start,
            block_end_minute=block_end,
        )
        avoided_import_price = self._post_command_settling_avoided_import_price(now)
        efficiency_sample = _shared_input_field(
            self._runtime, "efficiency", "battery_to_home_efficiency"
        )
        discharge_efficiency_entity = (
            "input_number.hoymiles_tariff_discharge_efficiency"
            if efficiency_sample is _SHARED_INPUT_MISSING
            else "input_number.hoymiles_ems_battery_to_home_efficiency"
        )
        current_hash = post_command_settling_market_fingerprint(replace(
            accepted,
            now=now,
            price_slots=prices,
            battery_wear_cost_pln_kwh=(
                # This optional legacy helper is absent in the managed package.
                # Match the optimizer's 0.08 default only for actual absence.
                0.08
                if self.hass.states.get("input_number.hoymiles_rce_battery_wear_cost") is None
                else _state_number(self.hass, "input_number.hoymiles_rce_battery_wear_cost")
            ),
            export_efficiency_percent=_state_number(
                self.hass, "input_number.hoymiles_rce_export_efficiency"
            ),
            house_discharge_efficiency_percent=_state_number(
                self.hass, discharge_efficiency_entity
            ),
            avoided_import_price_pln_kwh=avoided_import_price,
        ))
        return accepted_hash if current_hash == accepted_hash else None

    def _post_command_settling_avoided_import_price(self, now: datetime) -> float | None:
        """Verify the tariff broker's price against its current pricing policy."""

        tariff_plan = self._same_entry_tariff_plan_state()
        if tariff_plan is None:
            if self._tariff_plan_source is not None:
                return None
            return _state_number(self.hass, "input_number.hoymiles_tariff_g11_price")
        if (
            tariff_plan.state in {STATE_UNKNOWN, STATE_UNAVAILABLE}
            or tariff_plan.attributes.get("result_current") is not True
            or tariff_plan.attributes.get("recalculation_pending") is not False
        ):
            return None
        published = None
        price_attribute = ""
        for attribute in (
            "tariff_profile_g11_price", "g11_reference_price_pln_kwh",
            "current_price_pln_kwh",
        ):
            try:
                published = float(tariff_plan.attributes[attribute])
                price_attribute = attribute
                break
            except (KeyError, TypeError, ValueError):
                pass
        if published is None or not isfinite(published) or published <= 0.0:
            return None
        operator = _state_text(self.hass, "input_select.hoymiles_tariff_operator")
        tariff_type = _state_text(self.hass, "input_select.hoymiles_tariff_type")
        if tariff_type not in {"G11", "G12", "G12w", "G13"}:
            return None
        if operator != MANUAL_OPERATOR:
            profile = get_tariff_profile(operator, tariff_type)
            if (
                profile is None or not profile_is_valid(profile, now.date())
                or price_attribute != "tariff_profile_g11_price"
            ):
                return None
            actual_price = profile.g11_price_pln_kwh
        elif price_attribute == "g11_reference_price_pln_kwh":
            actual_price = _state_number(self.hass, "input_number.hoymiles_tariff_g11_price")
        elif price_attribute == "current_price_pln_kwh":
            rates = {
                name: _state_number(self.hass, f"input_number.hoymiles_tariff_{name}_price")
                for name in ("g11", "low", "medium", "peak")
            }
            windows = {
                name: _helper_minutes(self.hass, f"input_datetime.hoymiles_tariff_{name}")
                for name in ("cheap_1_start", "cheap_1_end", "cheap_2_start",
                             "cheap_2_end", "medium_start", "medium_end")
            }
            weekend = _state_text(self.hass, "input_boolean.hoymiles_tariff_weekend_low_price")
            holidays = _state_text(self.hass, "input_boolean.hoymiles_tariff_polish_holidays_low_price")
            if (
                any(value is None or not isfinite(value) or value < 0 for value in rates.values())
                or any(value is None or not 0 <= value < 1440 for value in windows.values())
                or weekend not in {"on", "off"} or holidays not in {"on", "off"}
            ):
                return None
            schedule = TariffSchedule(
                tariff_type=tariff_type,
                g11_price_pln_kwh=rates["g11"], low_price_pln_kwh=rates["low"],
                medium_price_pln_kwh=rates["medium"], peak_price_pln_kwh=rates["peak"],
                cheap_windows=((windows["cheap_1_start"], windows["cheap_1_end"]),
                               (windows["cheap_2_start"], windows["cheap_2_end"])),
                medium_windows=((windows["medium_start"], windows["medium_end"]),),
                weekend_low_price=tariff_type in {"G12w", "G13"} and weekend == "on",
                polish_holidays_low_price=holidays == "on",
                operator=operator,
            )
            actual_price = round(tariff_rate(now, schedule)[0], 4)
        else:
            return None
        return published if actual_price == published else None

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
        restore_cache = getattr(self, "_async_restore_load_history_cache", None)
        if callable(restore_cache):
            await restore_cache()
        shared_inputs = getattr(self._runtime, "shared_inputs", None)
        if shared_inputs is not None:
            # Bind only after the entity is genuinely on the platform. The
            # removal callback releases this bound-method owner before a
            # sensor-only reload attaches its replacement.
            self.async_on_remove(
                shared_inputs.attach_load_model_provider(
                    self.shared_load_model_snapshot
                )
            )
        listen_shared = getattr(
            getattr(self._runtime, "shared_inputs", None),
            "async_listen",
            None,
        )
        if callable(listen_shared):
            self.async_on_remove(listen_shared(self._async_shared_inputs_changed))
        listen_tariff_price = getattr(self._tariff_price_source, "async_listen", None)
        if callable(listen_tariff_price):
            self.async_on_remove(listen_tariff_price(self._async_tariff_price_changed))
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
            getattr(self, "_runtime", None),
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
                RCE_FULL_OPTIMIZER_INTERVAL,
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

    def _load_history_identity(self) -> dict[str, Any]:
        return source_identity(
            entry_id=self._entry.entry_id,
            entry_unique_id=self._entry.unique_id,
            source_device_id=self._entry.data.get(CONF_SOURCE_DEVICE_ID),
            resolved_source_device_id=self._entry.data.get(
                CONF_RESOLVED_SOURCE_DEVICE_ID
            ),
            timezone=self.hass.config.time_zone,
        )

    async def _async_restore_load_history_cache(self) -> None:
        """Restore accepted dates without pretending they were read now."""

        try:
            raw = await self._load_history_store.async_load()
            if raw is None:
                return
            restored, generated_at = decode_cache(
                raw,
                expected_identity=self._load_history_identity(),
                empty=_empty_load_summary(),
            )
        except (TypeError, ValueError):
            _LOGGER.warning("Ignoring foreign or corrupt qualified LOAD history cache")
            return
        self._extended_load_history = restored
        self._load_history = merge_history(
            _empty_load_summary(), restored, limit=4
        )
        self._load_profile_generated_at = generated_at
        self._load_history_store_payload = dict(raw)

    async def _async_save_load_history_cache(self) -> None:
        summary = merge_history(
            self._extended_load_history,
            self._load_history,
            limit=LOAD_EXTENDED_LOOKBACK_DAYS,
        )
        payload = encode_cache(
            summary,
            identity=self._load_history_identity(),
            generated_at=self._load_profile_generated_at,
        )
        if payload == self._load_history_store_payload:
            return
        await self._load_history_store.async_save(payload)
        self._load_history_store_payload = payload

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
            self._full_plan_trigger = "startup"
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
        self._full_plan_trigger = "history_refresh"
        self._invalidate_internal_inputs()
        await self._recalculate_and_write()

    async def _async_refresh_load_history(self, *, force_full: bool = False) -> None:
        """Refresh the mandatory recent LOAD model before optional history."""
        if self._history_refresh_running:
            return
        self._history_refresh_running = True
        refresh_shared_inputs = False
        try:
            timezone = ZoneInfo(self.hass.config.time_zone)
            now = datetime.now(timezone)
            self._load_history_last_attempt_at = now
            retry_count = getattr(self, "_load_history_retry_count", 0)
            next_retry_at = getattr(self, "_load_history_next_retry_at", None)
            retry_epoch_date = getattr(
                self,
                "_load_history_retry_epoch_date",
                None,
            )
            if retry_count >= LOAD_HISTORY_RETRY_LIMIT and (
                retry_epoch_date != now.date()
                or (next_retry_at is not None and now >= next_retry_at)
            ):
                retry_count = 0
                next_retry_at = None
                retry_epoch_date = now.date()
                self._load_history_retry_count = 0
                self._load_history_next_retry_at = None
                self._load_history_retry_epoch_date = now.date()
            full_refresh_needed = (
                force_full or self._full_history_refresh_date != now.date()
            )
            retry_due = next_retry_at is None or now >= next_retry_at
            retry_allowed = retry_count < LOAD_HISTORY_RETRY_LIMIT
            full_refresh = full_refresh_needed and retry_allowed and (
                retry_due or (force_full and retry_count == 0)
            )

            if full_refresh:
                short_start = datetime.combine(
                    now.date() - timedelta(days=LOAD_SHORT_LOOKBACK_DAYS),
                    time.min,
                    tzinfo=timezone,
                )
                profile_history_error: (
                    RecorderHistoryQueryTimeout
                    | RecorderHistoryLimitExceeded
                    | None
                ) = None
                try:
                    raw_history = await async_get_bounded_state_reports(
                        self.hass,
                        dt_util.as_utc(short_start),
                        dt_util.as_utc(now),
                        LOAD_PHASE_ENERGY_ENTITIES,
                    )
                except (RecorderHistoryQueryTimeout, RecorderHistoryLimitExceeded) as err:
                    self._load_history_retry_count = min(
                        retry_count + 1,
                        LOAD_HISTORY_RETRY_LIMIT,
                    )
                    self._load_history_retry_epoch_date = (
                        retry_epoch_date or now.date()
                    )
                    self._load_history_next_retry_at = (
                        now + LOAD_HISTORY_RETRY_BACKOFF
                        if self._load_history_retry_count < LOAD_HISTORY_RETRY_LIMIT
                        else now + LOAD_HISTORY_RETRY_COOLDOWN
                    )
                    self._load_history_read_status = "short_io_failed"
                    self._load_history_read_error = type(err).__name__
                    self._load_history_extended_partial = True
                    _LOGGER.warning(
                        "Recent LOAD phase history unavailable; preserving the last "
                        "verified model (attempt %s/%s)",
                        self._load_history_retry_count,
                        LOAD_HISTORY_RETRY_LIMIT,
                        exc_info=True,
                    )
                    return
                try:
                    profile_history = await async_get_bounded_state_reports(
                        self.hass,
                        dt_util.as_utc(short_start),
                        dt_util.as_utc(now),
                        (LOAD_PROFILE_ENERGY_ENTITY,),
                    )
                except (RecorderHistoryQueryTimeout, RecorderHistoryLimitExceeded) as err:
                    profile_history_error = err
                    profile_history = {LOAD_PROFILE_ENERGY_ENTITY: []}
                    self._load_history_retry_count = min(
                        retry_count + 1,
                        LOAD_HISTORY_RETRY_LIMIT,
                    )
                    self._load_history_retry_epoch_date = (
                        retry_epoch_date or now.date()
                    )
                    self._load_history_next_retry_at = (
                        now + LOAD_HISTORY_RETRY_BACKOFF
                        if self._load_history_retry_count < LOAD_HISTORY_RETRY_LIMIT
                        else now + LOAD_HISTORY_RETRY_COOLDOWN
                    )
                    self._load_history_read_status = (
                        "short_phase_complete_dense_unavailable"
                    )
                    self._load_history_read_error = type(err).__name__
                    self._load_history_extended_partial = True
                    _LOGGER.warning(
                        "Recent dense LOAD history unavailable; publishing the "
                        "phase-backed partial model (attempt %s/%s)",
                        self._load_history_retry_count,
                        LOAD_HISTORY_RETRY_LIMIT,
                        exc_info=True,
                    )
                raw_history.update(profile_history)
                self._load_history_short_range = (short_start, now)
            else:
                local_start = datetime.combine(
                    now.date(),
                    time.min,
                    tzinfo=timezone,
                )
                raw_history = await async_get_bounded_state_reports(
                    self.hass,
                    dt_util.as_utc(local_start),
                    dt_util.as_utc(now),
                    LOAD_PHASE_ENERGY_ENTITIES,
                )

            samples: dict[str, list[tuple[Any, Any]]] = {
                entity_id: [] for entity_id in LOAD_HISTORY_ENTITIES
            }
            for entity_id in LOAD_HISTORY_ENTITIES:
                for item in raw_history.get(entity_id, []):
                    state_value = getattr(item, "state", None)
                    updated = getattr(item, "last_updated", None)
                    observed_at = (
                        updated.astimezone(timezone)
                        if isinstance(updated, datetime)
                        and updated.tzinfo is not None
                        and updated.utcoffset() is not None
                        else updated
                    )
                    samples[entity_id].append(
                        (observed_at, parse_load_history_state(state_value))
                    )

            night_windows: dict[date, tuple[datetime, datetime]] = {}
            for offset in range(LOAD_SHORT_LOOKBACK_DAYS if full_refresh else 0, 0, -1):
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
                previous_short_summary = self._load_history
                previous_extended_summary = self._extended_load_history
                previous_profile_generated_at = self._load_profile_generated_at
                short_summary = summarize_load_history(
                    samples,
                    now=now,
                    night_windows=night_windows,
                    current_day_window=current_day_window,
                    history_days=4,
                )
                retained_daily: dict[str, float] = {}
                if profile_history_error is not None:
                    partial_daily = short_summary.partial_daily_energy_kwh or {}
                    retained_daily = {
                        day: value
                        for day, value in previous_short_summary.daily_energy_kwh.items()
                        if day in partial_daily and partial_daily[day] == value
                    }
                    daily_quality = dict(short_summary.daily_quality_by_date or {})
                    daily_quality.update(
                        {
                            day: "retained_complete_dense_timeout"
                            for day in retained_daily
                        }
                    )
                    preserve_profile = set(retained_daily) == set(
                        previous_short_summary.daily_energy_kwh
                    )
                    profile_quality = dict(short_summary.profile_quality_by_date or {})
                    if preserve_profile:
                        profile_quality.update(
                            {
                                day: "retained_complete_dense_timeout"
                                for day in retained_daily
                            }
                        )
                    short_summary = replace(
                        short_summary,
                        average_daily_kwh=(
                            round(sum(retained_daily.values()) / len(retained_daily), 3)
                            if retained_daily
                            else None
                        ),
                        daily_history_days=len(retained_daily),
                        daily_energy_kwh=retained_daily,
                        average_profile_kwh=(
                            previous_short_summary.average_profile_kwh
                            if preserve_profile
                            else ()
                        ),
                        weekday_profile_kwh=(
                            previous_short_summary.weekday_profile_kwh
                            if preserve_profile
                            else ()
                        ),
                        weekend_profile_kwh=(
                            previous_short_summary.weekend_profile_kwh
                            if preserve_profile
                            else ()
                        ),
                        weekday_profile_days=(
                            previous_short_summary.weekday_profile_days
                            if preserve_profile
                            else 0
                        ),
                        weekend_profile_days=(
                            previous_short_summary.weekend_profile_days
                            if preserve_profile
                            else 0
                        ),
                        profile_history_days=(
                            previous_short_summary.profile_history_days
                            if preserve_profile
                            else 0
                        ),
                        daily_quality_by_date=daily_quality,
                        profile_quality_by_date=profile_quality,
                        daily_coverage_ratio=(
                            previous_short_summary.daily_coverage_ratio
                            if preserve_profile
                            else 0.0
                        ),
                        profile_coverage_ratio=(
                            previous_short_summary.profile_coverage_ratio
                            if preserve_profile
                            else 0.0
                        ),
                    )
                short_has_observations = any(
                    is_load_history_observation(value)
                    for entity_samples in samples.values()
                    for _observed_at, value in entity_samples
                )
                if short_has_observations:
                    merged_short = merge_history(
                        previous_short_summary,
                        short_summary,
                        limit=4,
                    )
                    accepted_changed = (
                        merged_short.daily_energy_kwh
                        != previous_short_summary.daily_energy_kwh
                        or merged_short.profile_kwh_by_date
                        != previous_short_summary.profile_kwh_by_date
                    )
                    self._load_history = merged_short
                    self._load_profile_generated_at = (
                        now if accepted_changed else previous_profile_generated_at
                    )
                else:
                    # A successful empty Recorder response is not a new model
                    # observation.  Keep the last verified model and its true
                    # acquisition timestamp so freshness cannot be renewed by
                    # republishing it.
                    self._load_history = previous_short_summary
                    self._load_profile_generated_at = previous_profile_generated_at
                self._load_history_last_success_at = now
                if profile_history_error is None:
                    self._full_history_refresh_date = now.date()
                    self._load_history_retry_count = 0
                    self._load_history_next_retry_at = None
                    self._load_history_retry_epoch_date = None
                    self._load_history_read_status = (
                        "short_complete"
                        if short_has_observations
                        else "short_no_new_data"
                    )
                    self._load_history_read_error = None
                refresh_shared_inputs = True

                # Publish the useful recent result before optional old history.
                coordinator = getattr(self._runtime, "shared_inputs", None)
                refresh = getattr(coordinator, "async_refresh", None)
                if callable(refresh):
                    try:
                        await refresh()
                        refresh_shared_inputs = False
                    except Exception:  # noqa: BLE001 - recent model stays valid
                        _LOGGER.exception("Cannot publish the recent EMS LOAD model")

                # A timed-out Recorder worker may still be executing SQL. Do not
                # start optional extension reads until the bounded query guard
                # confirms that worker has really finished on a later refresh.
                if profile_history_error is None:
                    extended_samples = {
                        entity_id: list(values)
                        for entity_id, values in samples.items()
                    }
                    extended_start = datetime.combine(
                        now.date() - timedelta(days=LOAD_EXTENDED_LOOKBACK_DAYS),
                        time.min,
                        tzinfo=timezone,
                    )
                    cursor = extended_start
                    extension_end = self._load_history_short_range[0]
                    extension_complete = True
                    try:
                        async with asyncio.timeout(LOAD_EXTENDED_TOTAL_BUDGET_SECONDS):
                            while cursor < extension_end:
                                chunk_end = min(
                                    cursor + timedelta(days=LOAD_EXTENDED_CHUNK_DAYS),
                                    extension_end,
                                )
                                phase_chunk = await async_get_bounded_state_reports(
                                    self.hass,
                                    dt_util.as_utc(cursor),
                                    dt_util.as_utc(chunk_end),
                                    LOAD_PHASE_ENERGY_ENTITIES,
                                )
                                dense_chunk = await async_get_bounded_state_reports(
                                    self.hass,
                                    dt_util.as_utc(cursor),
                                    dt_util.as_utc(chunk_end),
                                    (LOAD_PROFILE_ENERGY_ENTITY,),
                                )
                                phase_chunk.update(dense_chunk)
                                for entity_id in LOAD_HISTORY_ENTITIES:
                                    for item in phase_chunk.get(entity_id, []):
                                        state_value = getattr(item, "state", None)
                                        updated = getattr(item, "last_updated", None)
                                        observed_at = (
                                            updated.astimezone(timezone)
                                            if isinstance(updated, datetime)
                                            and updated.tzinfo is not None
                                            and updated.utcoffset() is not None
                                            else updated
                                        )
                                        extended_samples[entity_id].append(
                                            (
                                                observed_at,
                                                parse_load_history_state(state_value),
                                            )
                                        )
                                cursor = chunk_end
                    except (
                        TimeoutError,
                        RecorderHistoryQueryTimeout,
                        RecorderHistoryLimitExceeded,
                    ) as err:
                        extension_complete = False
                        self._load_history_read_error = type(err).__name__
                        _LOGGER.warning(
                            "Extended LOAD history is partial; keeping the recent model",
                            exc_info=True,
                        )

                    extended_night_windows = dict(night_windows)
                    for offset in range(29, LOAD_SHORT_LOOKBACK_DAYS, -1):
                        night_date = now.date() - timedelta(days=offset)
                        sunset = get_astral_event_date(self.hass, "sunset", night_date)
                        sunrise = get_astral_event_date(
                            self.hass,
                            "sunrise",
                            night_date + timedelta(days=1),
                        )
                        if sunset is None or sunrise is None:
                            continue
                        extended_night_windows[night_date] = (
                            sunset.astimezone(timezone) - timedelta(minutes=90),
                            sunrise.astimezone(timezone) + timedelta(minutes=90),
                        )
                    extended_summary = summarize_load_history(
                        extended_samples,
                        now=now,
                        night_windows=extended_night_windows,
                        current_day_window=current_day_window,
                        history_days=28,
                    )
                    extended_has_observations = any(
                        is_load_history_observation(value)
                        for entity_samples in extended_samples.values()
                        for _observed_at, value in entity_samples
                    )
                    if extended_has_observations:
                        merged_extended = merge_history(
                            previous_extended_summary,
                            extended_summary,
                            limit=LOAD_EXTENDED_LOOKBACK_DAYS,
                        )
                        accepted_changed = (
                            merged_extended.daily_energy_kwh
                            != previous_extended_summary.daily_energy_kwh
                            or merged_extended.profile_kwh_by_date
                            != previous_extended_summary.profile_kwh_by_date
                        )
                        self._extended_load_history = merged_extended
                        if accepted_changed:
                            self._load_profile_generated_at = now
                    else:
                        self._extended_load_history = previous_extended_summary
                    self._load_history_extended_range = (extended_start, now)
                    self._load_history_extended_partial = not extension_complete
                    self._load_history_read_status = (
                        "extended_complete"
                        if extension_complete and extended_has_observations
                        else "extended_no_new_data"
                        if extension_complete
                        else "extended_partial"
                    )
                    refresh_shared_inputs = True
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
                    current_day_observed_at=current.current_day_observed_at,
                )
                self._extended_load_history = replace(
                    self._extended_load_history,
                    current_day_energy_kwh=current.current_day_energy_kwh,
                    current_day_observed_at=current.current_day_observed_at,
                )
                # The hourly pass may add exactly one just-finished night.
                # This is a bounded phase-only query and does not refresh the
                # age of the daily/profile model.
                for night_date in (now.date() - timedelta(days=1), now.date()):
                    key = night_date.isoformat()
                    if key in self._extended_load_history.night_energy_kwh:
                        continue
                    sunset = get_astral_event_date(self.hass, "sunset", night_date)
                    sunrise = get_astral_event_date(
                        self.hass, "sunrise", night_date + timedelta(days=1)
                    )
                    if sunset is None or sunrise is None:
                        continue
                    night_start = sunset.astimezone(timezone) - timedelta(minutes=90)
                    night_end = sunrise.astimezone(timezone) + timedelta(minutes=90)
                    if night_end > now:
                        continue
                    night_raw = await async_get_bounded_state_reports(
                        self.hass,
                        dt_util.as_utc(night_start - timedelta(hours=2)),
                        dt_util.as_utc(night_end),
                        LOAD_PHASE_ENERGY_ENTITIES,
                    )
                    night_samples: dict[str, list[tuple[Any, Any]]] = {}
                    for entity_id in LOAD_PHASE_ENERGY_ENTITIES:
                        rows: list[tuple[Any, Any]] = []
                        for item in night_raw.get(entity_id, []):
                            observed_at = getattr(item, "last_updated", None)
                            local_observed_at = (
                                observed_at.astimezone(timezone)
                                if isinstance(observed_at, datetime)
                                and observed_at.tzinfo is not None
                                and observed_at.utcoffset() is not None
                                else observed_at
                            )
                            rows.append(
                                (
                                    local_observed_at,
                                    parse_load_history_state(
                                        getattr(item, "state", None)
                                    ),
                                )
                            )
                        night_samples[entity_id] = rows
                    night_summary = summarize_load_history(
                        night_samples,
                        now=now,
                        night_windows={night_date: (night_start, night_end)},
                        history_days=0,
                    )
                    if key in night_summary.night_energy_kwh:
                        self._load_history = merge_history(
                            self._load_history, night_summary, limit=4
                        )
                        self._extended_load_history = merge_history(
                            self._extended_load_history,
                            night_summary,
                            limit=LOAD_EXTENDED_LOOKBACK_DAYS,
                        )
                        self._load_history_read_status = "completed_night_added"
                    elif key in (night_summary.night_quality_by_date or {}):
                        previous_short = self._load_history
                        previous_extended = self._extended_load_history
                        merged_short = merge_history(
                            previous_short, night_summary, limit=4
                        )
                        merged_extended = merge_history(
                            previous_extended,
                            night_summary,
                            limit=LOAD_EXTENDED_LOOKBACK_DAYS,
                        )
                        self._load_history = replace(
                            merged_short,
                            current_day_energy_kwh=(
                                previous_short.current_day_energy_kwh
                            ),
                            current_day_observed_at=(
                                previous_short.current_day_observed_at
                            ),
                            partial_daily_energy_kwh=(
                                previous_short.partial_daily_energy_kwh
                            ),
                        )
                        self._extended_load_history = replace(
                            merged_extended,
                            current_day_energy_kwh=(
                                previous_extended.current_day_energy_kwh
                            ),
                            current_day_observed_at=(
                                previous_extended.current_day_observed_at
                            ),
                            partial_daily_energy_kwh=(
                                previous_extended.partial_daily_energy_kwh
                            ),
                        )
                    break
                refresh_shared_inputs = True
            save_cache = getattr(self, "_async_save_load_history_cache", None)
            if callable(save_cache):
                await save_cache()
        except Exception:  # noqa: BLE001 - recorder outages need a safe fallback
            _LOGGER.exception("Cannot rebuild recorder-backed LOAD history")
        finally:
            self._history_refresh_running = False
        if refresh_shared_inputs:
            coordinator = getattr(self._runtime, "shared_inputs", None)
            refresh = getattr(coordinator, "async_refresh", None)
            if callable(refresh):
                try:
                    await refresh()
                except Exception:  # noqa: BLE001 - the local model stays valid
                    _LOGGER.exception("Cannot publish the shared EMS LOAD model")

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
                getattr(self, "_runtime", None),
            )
            policy_signature = _forecast_learning_policy_signature(
                learning_policy
            )
            if not learning_policy.enabled:
                if learning_policy.mode == "fixed_zero_export":
                    self._forecast_refresh_date = now.date()
                return
            configured = _resolved_forecast_entity_id(
                self.hass,
                getattr(self, "_runtime", None),
                broker_field="today",
                new_helper=EMS_TODAY_FORECAST_ENTITY_HELPER,
                legacy_helper=TODAY_FORECAST_ENTITY_HELPER,
            )
            forecast_entity, forecast_state = _first_numeric_state(
                self.hass,
                TODAY_FORECAST_CANDIDATES,
                configured,
            )
            if not forecast_entity or forecast_state is None:
                return
            source_policy = forecast_policy_for_source(
                learning_policy, getattr(forecast_state, "attributes", {}),
            )
            if source_policy.mode == "solcast_adaptive":
                # Solcast owns production-based calibration for this source.
                # Retain the dormant local model, but perform no Recorder scan.
                self._forecast_refresh_date = now.date()
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
            actual_by_day: dict[date, list[tuple[datetime, object]]] = {}
            for item in raw.get(forecast_entity, []):
                updated = getattr(item, "last_updated", None)
                value = getattr(item, "state", None)
                if updated is None or value is None:
                    continue
                try:
                    numeric = float(value)
                except (TypeError, ValueError, OverflowError):
                    continue
                if not isfinite(numeric) or numeric < 0.0:
                    continue
                local_day = updated.astimezone(timezone).date()
                if local_day < now.date():
                    forecast_by_day.setdefault(local_day, []).append(numeric)
            for item in raw.get(actual_entity, []):
                updated = getattr(item, "last_updated", None)
                if updated is None:
                    continue
                local_updated = updated.astimezone(timezone)
                if local_updated.date() < now.date():
                    actual_by_day.setdefault(local_updated.date(), []).append(
                        (local_updated, getattr(item, "state", None))
                    )

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
                sunset = get_astral_event_date(self.hass, "sunset", day)
                if sunset is None:
                    continue
                actual = qualified_cumulative_energy_day(
                    actual_by_day[day],
                    day_start=day_start,
                    day_end=day_end,
                    production_end=sunset.astimezone(timezone),
                )
                if not forecasts or actual is None:
                    continue
                # The median is robust to several intraday Solcast refreshes.
                ordered = sorted(forecasts)
                forecast = ordered[len(ordered) // 2]
                age_days = float((now.date() - day).days)
                samples.append((age_days, actual / forecast))

            current_policy, _ = _forecast_learning_policy_snapshot(
                self.hass,
                dt_util.now().astimezone(timezone),
                getattr(self, "_runtime", None),
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
        if _shared_inputs_snapshot(self._runtime) is not None:
            targets = _configured_forecast_entity_ids(self.hass, self._runtime)
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
                self._async_forecast_value_changed,
            )

    @callback
    def _async_forecast_value_changed(
        self,
        _event: Event[EventStateChangedData],
    ) -> None:
        """Collect forecast value churn for the next 120-second snapshot."""

        if not self._lifecycle_stopped:
            self._shared_inputs_dirty = True

    @callback
    def _remove_dynamic_forecast_listener(self) -> None:
        """Release the current dynamic forecast subscription."""
        if self._dynamic_forecast_unsub is not None:
            self._dynamic_forecast_unsub()
            self._dynamic_forecast_unsub = None
        self._dynamic_forecast_entities = frozenset()

    @callback
    def _current_input_fingerprint(self) -> tuple[Any, ...]:
        """Certify every value and provenance consumed by this solver run."""
        tariff_entity_id = getattr(
            getattr(self, "_tariff_plan_source", None),
            "entity_id",
            None,
        )
        # Fast telemetry does not start another solver. Publication retains its
        # identity/quality evidence and revalidates the fixed selection against
        # newly captured physical inputs after the executor returns.
        watched_entities = set(
            WATCHED_ENTITIES | self._configured_forecast_source_ids()
        )
        watched_entities -= {"sensor.hoymiles_hit_tariff_charge_plan"}
        if isinstance(tariff_entity_id, str):
            watched_entities.add(tariff_entity_id)
        # These two raw states arrive as one physical FC03 cohort.  Fingerprint
        # the coherent projection below rather than a transient HA delivery
        # order.
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
        fingerprint = _rce_report_fingerprint(
            self.hass, fingerprint, dt_util.now(),
        )
        # RCE still consumes the raw execution enable/limit states below when
        # applying the export cap.  Keep them separate from the coherent GCF
        # learning cohort so a raw-state drift cannot certify stale executor
        # work even when Shared EMS is active.
        raw_execution_gcf_signature = optimizer_input_fingerprint(
            self.hass,
            RCE_GCF_OPTIMIZER_ENTITIES,
        )
        raw_execution_gcf_signature = _rce_report_fingerprint(
            self.hass, raw_execution_gcf_signature, dt_util.now(),
        )
        fingerprint = (
            *fingerprint,
            ("__rce_execution_gcf__", raw_execution_gcf_signature),
        )
        shared_signature_func = globals().get("_shared_optimizer_signature")
        shared_signature = (
            shared_signature_func(self._runtime)
            if callable(shared_signature_func) and hasattr(self, "_runtime")
            else None
        )
        if shared_signature is not None:
            fingerprint = (
                *fingerprint,
                ("__shared_ems_inputs__", shared_signature),
            )
        gcf_signature = _live_forecast_gcf_optimizer_signature(
            self.hass,
            getattr(self, "_runtime", None),
        )
        schedule = getattr(
            getattr(self, "_tariff_price_source", None),
            "current_price_schedule",
            None,
        )
        semantic_signature = getattr(
            getattr(self, "_tariff_price_source", None),
            "_semantic_signature",
            None,
        )
        tariff_price_signature = (
            semantic_signature(schedule)
            if callable(semantic_signature)
            else None
        )
        return (
            *fingerprint,
            ("__rce_gcf_optimizer__", gcf_signature),
            ("__r07_tariff_price__", tariff_price_signature),
            ("__rce_publication_source__", (
                getattr(getattr(self, "_entry", None), "entry_id", None),
                getattr(getattr(self, "_entry", None), "data", {}).get(CONF_SOURCE_DEVICE_ID),
                getattr(getattr(self, "_entry", None), "data", {}).get(CONF_RESOLVED_SOURCE_DEVICE_ID),
                getattr(getattr(getattr(self, "_runtime", None), "source_device", None), "id", None),
            )),
        )

    @callback
    def _async_tariff_price_changed(self) -> None:
        """Refresh the shadow sidecar when its independent R07 source changes."""

        if self._lifecycle_stopped:
            return
        self._invalidate_internal_inputs()
        self._full_plan_trigger = "tariff_price_update"
        if self._recalculate_cancel is None:
            self._recalculate_cancel = async_call_later(
                self.hass,
                INPUT_RECALCULATION_DELAY_SECONDS,
                self._async_debounced_recalculate,
            )

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
    def _reject_stale_executor_result(
        self,
        captured_revision: int,
        captured_fingerprint: tuple[Any, ...],
    ) -> bool:
        """Reject stale work and account for unreported fingerprint drift."""

        revision_current = self._input_revision.is_current(captured_revision)
        fingerprint_current = _rce_publication_fingerprints_match(
            captured_fingerprint, self._current_input_fingerprint(),
            now=dt_util.now(),
        )
        if revision_current and fingerprint_current:
            return False
        self._full_plan_rejected_for_input_drift = True
        if revision_current:
            # A consumed source changed before its HA callback advanced the
            # revision.  Convert that drift into a real revision so the next
            # attempt can certify a fresh snapshot.
            self._invalidate_internal_inputs()
        else:
            self._mark_recalculation_pending()
        return True

    @callback
    def _mark_recalculation_pending(self) -> None:
        """Withdraw execution authority once, without publication ping-pong."""
        self._cancel_stale_result_retry()
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
        self._full_plan_trigger = "immediate_input"
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
    def _async_shared_inputs_changed(self) -> None:
        """Collect broker churn for the next bounded full-plan snapshot."""

        if self._lifecycle_stopped:
            return
        bootstrap = not self._shared_inputs_seen
        self._shared_inputs_seen = True
        if not bootstrap:
            # Shared live values update frequently.  They do not withdraw an
            # accepted RCE plan; the periodic five-minute pass reads their
            # newest coherent snapshot.  The independent execution gates
            # remain fail-closed for live SOC/BMS/export data.
            self._shared_inputs_dirty = True
            return
        self._invalidate_internal_inputs()
        self._full_plan_trigger = "shared_bootstrap"
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
        runtime = getattr(self, "_runtime", None)
        policy, diagnostics = (
            _forecast_learning_policy_snapshot(self.hass, dt_util.now(), runtime)
            if runtime is not None
            else _forecast_learning_policy_snapshot(self.hass, dt_util.now())
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
        runtime = getattr(self, "_runtime", None)
        policy, diagnostics = (
            _forecast_learning_policy_snapshot(self.hass, now, runtime)
            if runtime is not None
            else _forecast_learning_policy_snapshot(self.hass, now)
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
        self._full_plan_trigger = "forecast_gcf_policy"
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
        self._full_plan_trigger = "forecast_policy_recovery"
        self._invalidate_internal_inputs()
        await self._recalculate_and_write()

    async def _async_debounced_recalculate(self, now: datetime) -> None:
        self._recalculate_cancel = None
        if self._lifecycle_stopped:
            return
        # Small contract probes and migration-era restored entities may reach
        # this callback before the normal constructor has installed the
        # optional broker flag.  Treat that as a clean snapshot rather than
        # creating a second invalidation path.
        if getattr(self, "_shared_inputs_dirty", False):
            self._shared_inputs_dirty = False
            self._invalidate_internal_inputs()
        task = asyncio.current_task()
        if task is None:
            return
        self._delayed_recalculate_tasks.add(task)
        try:
            await self._recalculate_and_write()
        finally:
            self._delayed_recalculate_tasks.discard(task)

    @callback
    def _schedule_stale_result_retry(self) -> None:
        """Queue one bounded retry after three in-flight input drifts."""

        if (
            self._lifecycle_stopped
            or self._stale_result_retry_cancel is not None
        ):
            return
        self._stale_result_retry_cancel = async_call_later(
            self.hass,
            RCE_STALE_RESULT_RETRY_DELAY_SECONDS,
            self._async_stale_result_retry,
        )

    @callback
    def _cancel_stale_result_retry(self) -> None:
        """Cancel a retry superseded by a newer RCE-plan trigger."""

        if self._stale_result_retry_cancel is not None:
            self._stale_result_retry_cancel()
            self._stale_result_retry_cancel = None

    async def _async_stale_result_retry(self, _now: datetime) -> None:
        """Run one delayed retry batch without re-arming on another drift."""

        self._stale_result_retry_cancel = None
        if self._lifecycle_stopped:
            return
        self._full_plan_trigger = "stale_result_retry"
        await self._recalculate_and_write(allow_deferred_retry=False)

    @callback
    def _cancel_delayed_recalculation(self) -> None:
        """Cancel queued or running delayed work when the entity is removed."""

        self._lifecycle_stopped = True
        self._cancel_stale_result_retry()
        if self._recalculate_cancel is not None:
            self._recalculate_cancel()
            self._recalculate_cancel = None
        tasks = tuple(self._delayed_recalculate_tasks)
        self._delayed_recalculate_tasks.clear()
        for task in tasks:
            if not task.done():
                task.cancel()

    async def _async_timer(self, now: datetime) -> None:
        """Refresh the full RCE forecast on its bounded five-minute cadence."""
        if self._recalculate_cancel is not None:
            self._recalculate_cancel()
            self._recalculate_cancel = None
        self._shared_inputs_dirty = False
        self._full_plan_trigger = "periodic"
        self._invalidate_internal_inputs()
        await self._recalculate_and_write()

    async def async_recalculate_post_command_settling(self) -> None:
        """Run the supervisor's one extra refresh through normal revision gates."""

        if self._lifecycle_stopped:
            return
        task = asyncio.current_task()
        if task is None:
            return
        self._delayed_recalculate_tasks.add(task)
        try:
            self._shared_inputs_dirty = False
            self._full_plan_trigger = "post_command_settling"
            self._invalidate_internal_inputs()
            await self._recalculate_and_write()
        finally:
            self._delayed_recalculate_tasks.discard(task)

    async def _recalculate_and_write(
        self,
        *,
        allow_deferred_retry: bool = True,
    ) -> None:
        """Write to HA only when the material plan state changed."""
        async with self._optimizer_lock:
            previous_state = self.native_value
            previous_attributes = self._attributes
            committed = False
            stale_rejections = 0
            for _attempt in range(MAX_IMMEDIATE_RECALCULATIONS):
                self._full_plan_rejected_for_input_drift = False
                if await self._recalculate_locked():
                    committed = True
                    break
                if self._full_plan_rejected_for_input_drift:
                    stale_rejections += 1
            if committed:
                self._mark_result_current()
                self._cancel_stale_result_retry()
                if self._recalculate_cancel is not None:
                    self._recalculate_cancel()
                    self._recalculate_cancel = None
            self._attributes = {
                **self._attributes,
                "full_plan_solver_calls": self._full_plan_solver_calls,
                "last_full_plan_at": (
                    self._last_full_plan_at.isoformat().replace("+00:00", "Z")
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
                and stale_rejections == MAX_IMMEDIATE_RECALCULATIONS
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

    async def _recalculate_locked(self) -> bool:
        if self._lifecycle_stopped:
            return False
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
                self._post_command_settling_market_settings = None
                if self._retain_last_complete_plan(
                    blocker_code="missing_data",
                    missing_entities=metadata["missing_entities"],
                ):
                    return False
                self._result = None
                self._timeline_metadata = dict(metadata)
                self._attributes = {
                    "status_code": "missing_data",
                    "missing_entities": metadata["missing_entities"],
                    "planned_slots": [],
                    "current_slot_load_exhausts_requested_discharge_budget": False,
                    "post_command_settling_market_fingerprint": None,
                    **metadata,
                }
                return True
            # Freeze both the original evidence and the worker input. The HA
            # loop may replace or mutate provider-owned maps during this await.
            captured_settings = deepcopy(settings)
            self._full_plan_solver_calls += 1
            result = await self.hass.async_add_executor_job(
                optimize_rce, deepcopy(captured_settings),
            )
            if self._lifecycle_stopped:
                return False
            settings, metadata = self._optimizer_input()
            if self._reject_stale_executor_result(
                captured_revision,
                captured_fingerprint,
            ):
                return False
            fresh_inputs = settings is not None and all(
                metadata.get(key) is True for key in (
                    "rce_today_data_fresh", "forecast_today_data_fresh",
                    "soc_data_fresh", "gcf_execution_data_fresh",
                )
            )
            solver_status, solver_ready = result.status_code, result.ready
            result = (
                revalidate_rce_plan(
                    settings, result, captured_settings=captured_settings,
                )
                if fresh_inputs else None
            )
            if result is None:
                # No await separates latest input capture, revalidation and
                # publication. A failed proof cannot inherit currentness from
                # the worker's old snapshot or become a successful empty run.
                self._post_command_settling_market_settings = None
                blocker = "plan_revalidation_failed" if fresh_inputs else "missing_data"
                failure_diagnostics = {
                    "plan_revalidation_solver_status": solver_status,
                    "plan_revalidation_solver_ready": solver_ready,
                }
                if not self._retain_last_complete_plan(
                    blocker_code=blocker,
                    missing_entities=metadata.get("missing_entities", ()),
                ):
                    self._result = None
                    self._timeline_metadata = dict(metadata)
                    self._attributes = {
                        **metadata,
                        **failure_diagnostics,
                        "status_code": "missing_data",
                        "result_current": False,
                        "recalculation_pending": True,
                        "execution_input_valid": False,
                        "execution_blocker_code": blocker,
                        "planned_slots": [],
                        "post_command_settling_market_fingerprint": None,
                    }
                else:
                    self._attributes = {**self._attributes, **failure_diagnostics}
                return False
            if result.ready is False and result.status_code == "home_energy_shortage":
                # A current diagnostic result is not execution readiness.
                # Preserve the actual physical blocker instead of leaving a
                # valid negative plan pending forever as generic missing data.
                metadata = {
                    **metadata,
                    "execution_input_valid": False,
                    "execution_blocker_code": "home_energy_shortage",
                    "plan_revalidation_reason": "current_home_energy_shortage",
                }
            result = retain_active_rce_slot(
                settings, result,
                accepted_settings=self._post_command_settling_market_settings,
                accepted_result=self._result,
                commitment=self._active_rce_commitment(settings.now),
            )
            metadata = {**metadata, "active_slot_commitment_applied":
                        result.active_slot_commitment_applied}
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
                "legacy_planned_export_kwh": round(
                    result.legacy_planned_export_kwh,
                    2,
                ),
                "legacy_planned_revenue_pln": round(
                    result.legacy_planned_revenue_pln,
                    2,
                ),
                "self_consumption_filter_contract_version": (
                    result.self_consumption_filter_contract_version
                ),
                "self_consumption_filter_active": (
                    result.self_consumption_filter_active
                ),
                "self_consumption_filter_applied": (
                    result.self_consumption_filter_applied
                ),
                "self_consumption_filter_reduced": (
                    result.self_consumption_filter_reduced
                ),
                "self_consumption_filter_status_code": (
                    result.self_consumption_filter_status_code
                ),
                "self_consumption_filter_reason_code": (
                    result.self_consumption_filter_reason_code
                ),
                "self_consumption_filter_ui_reason": (
                    result.self_consumption_filter_ui_reason
                ),
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
                # Control input: rounding 199.9 W to 200 W changes eligibility.
                "current_slot_execution_export_power_kw": (
                    result.current_slot_execution_export_power_kw
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
                "current_slot_load_exhausts_requested_discharge_budget": (
                    result.current_slot_load_exhausts_requested_discharge_budget
                ),
                "post_command_settling_market_fingerprint": (
                    result.post_command_settling_market_fingerprint
                ),
                "planned_slots": planned_slots,
                **(
                    result.self_consumption_shadow.as_attributes()
                    if result.self_consumption_shadow is not None
                    else {
                        "shadow_contract_version": (
                            "rce_self_consumption_shadow_v3"
                        ),
                        "legacy_sale_contract_version": "rce_sale_profit_v1",
                        "shadow_control_applied": False,
                        "shadow_available": False,
                        "shadow_status_code": "unavailable",
                        "shadow_reason_code": "not_calculated",
                    }
                ),
                **metadata,
            }
            self._post_command_settling_market_settings = settings
            self._last_full_plan_at = dt_util.utcnow()
            return True
        except Exception:  # noqa: BLE001 - fail closed in the automation entity
            if self._reject_stale_executor_result(
                captured_revision,
                captured_fingerprint,
            ):
                return False
            _LOGGER.exception("Cannot calculate the optimized RCE plan")
            self._post_command_settling_market_settings = None
            if self._retain_last_complete_plan(
                blocker_code="optimizer_error",
                missing_entities=(),
            ):
                return False
            self._result = None
            self._timeline_metadata = {}
            self._attributes = {
                "status_code": "optimizer_error",
                "missing_entities": [],
                "planned_slots": [],
                "current_slot_load_exhausts_requested_discharge_budget": False,
                "post_command_settling_market_fingerprint": None,
            }
            return True

    def _retain_last_complete_plan(
        self,
        *,
        blocker_code: str,
        missing_entities: Any,
    ) -> bool:
        """Keep one recent display plan while withdrawing all authority."""

        completed_at = self._last_full_plan_at
        if self._result is None or completed_at is None:
            return False
        try:
            age_seconds = (dt_util.utcnow() - completed_at).total_seconds()
        except (AttributeError, OverflowError, TypeError, ValueError):
            return False
        if age_seconds < -5.0 or age_seconds > _LAST_COMPLETE_PLAN_GRACE_SECONDS:
            return False
        self._attributes = {
            **self._attributes,
            "missing_entities": list(missing_entities),
            "result_current": False,
            "recalculation_pending": True,
            "plan_display_available": True,
            "plan_data_state": "last_complete_inputs_missing",
            "execution_input_valid": False,
            "execution_blocker_code": blocker_code,
            "last_complete_age_seconds": round(max(age_seconds, 0.0), 1),
        }
        if self._timeline_sensor is not None:
            self._timeline_sensor.publish_unavailable(
                input_revision=self._input_revision.value,
                blocker_code=blocker_code,
            )
        return True

    def _optimizer_input(
        self,
    ) -> tuple[OptimizerInput | None, dict[str, Any]]:
        timezone = ZoneInfo(self.hass.config.time_zone)
        now = dt_util.now().astimezone(timezone)
        now_slot = floor_half_hour(now)

        bms_current_sample = _policy_numeric_sample(
            self._runtime,
            "bms",
            "maximum_discharge_current_a",
            self.hass.states.get("sensor.hoymiles_hit_maximum_discharge_current"),
            now,
            max_age_seconds=300.0,
            minimum=0.0,
        )
        bms_charge_current_sample = _policy_numeric_sample(
            self._runtime,
            "bms",
            "maximum_charge_current_a",
            self.hass.states.get("sensor.hoymiles_hit_maximum_charge_current"),
            now,
            max_age_seconds=300.0,
            minimum=0.0,
        )
        bms_voltage_sample = _policy_numeric_sample(
            self._runtime,
            "bms",
            "voltage_v",
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

        self_use_soc_sample = _policy_numeric_sample(
            self._runtime,
            "system",
            "self_use_reserve_soc_percent",
            self.hass.states.get(
                "sensor.hoymiles_hit_ems_self_use_soc_readback"
            ),
            now,
            max_age_seconds=300.0,
            minimum=10.0,
            maximum=100.0,
        )
        battery_soc_sample = _policy_numeric_sample(
            self._runtime,
            "system",
            "battery_soc_percent",
            self.hass.states.get(
                "sensor.hoymiles_hit_overview_battery_soc"
            ),
            now,
            max_age_seconds=120.0,
            minimum=0.0,
            maximum=100.0,
        )
        inverter_count_sample = _policy_numeric_sample(
            self._runtime,
            "system",
            "inverter_count",
            self.hass.states.get(
                "sensor.hoymiles_hit_number_of_machines_master_and_slave"
            ),
            now,
            max_age_seconds=300.0,
            minimum=1.0,
            maximum=10.0,
        )
        battery_capacity = _policy_stable_number(
            self._runtime,
            "system",
            "battery_capacity_kwh",
            self.hass,
            "sensor.hoymiles_hit_battery_capacity",
            minimum=0.001,
        )
        required = {
            "sensor.hoymiles_hit_battery_capacity": battery_capacity,
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
        shared_rated_power = _shared_sample_value(
            self._runtime,
            "system",
            "inverter_rated_power_each_kw",
        )
        if shared_rated_power is _SHARED_INPUT_MISSING:
            rated_power = _preferred_select_number(
                self.hass,
                EMS_INVERTER_RATED_POWER_HELPER,
                LEGACY_INVERTER_RATED_POWER_HELPER,
            )
        else:
            rated_power = (
                float(shared_rated_power)
                if type(shared_rated_power) in {int, float}
                else None
            )
        if rated_power is None:
            required[EMS_INVERTER_RATED_POWER_HELPER] = None
        rce_inverter_power = (
            _rce_inverter_discharge_power_kw(rated_power)
            if rated_power is not None
            else None
        )

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
        (
            today_rows_payload,
            primary_today_rows_age_seconds,
        ) = _rce_state_rows_and_age(
            today_rows_state,
            now,
        )
        (
            tomorrow_rows_payload,
            tomorrow_rows_age_seconds,
        ) = _rce_state_rows_and_age(tomorrow_rows_state, now)
        (
            today_rows,
            today_rows_complete,
            today_half_hours,
            today_expected_half_hours,
            today_rows_data_fresh,
            today_rows_age_seconds,
            today_rows_source_role,
        ) = _select_current_rce_price_rows(
            primary_rows=today_rows_payload,
            primary_age_seconds=primary_today_rows_age_seconds,
            rollover_rows=tomorrow_rows_payload,
            rollover_age_seconds=tomorrow_rows_age_seconds,
            target_date=now.date(),
            timezone=timezone,
        )
        tomorrow_rows = _rce_rows_for_local_date(
            tomorrow_rows_payload,
            now.date() + timedelta(days=1),
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

        today_configured = _resolved_forecast_entity_id(
            self.hass,
            self._runtime,
            broker_field="today",
            new_helper=EMS_TODAY_FORECAST_ENTITY_HELPER,
            legacy_helper=TODAY_FORECAST_ENTITY_HELPER,
        )
        tomorrow_configured = _resolved_forecast_entity_id(
            self.hass,
            self._runtime,
            broker_field="tomorrow",
            new_helper=EMS_TOMORROW_FORECAST_ENTITY_HELPER,
            legacy_helper=TOMORROW_FORECAST_ENTITY_HELPER,
        )
        day3_configured = _resolved_forecast_entity_id(
            self.hass,
            self._runtime,
            broker_field="day3",
            new_helper=EMS_DAY3_FORECAST_ENTITY_HELPER,
            legacy_helper=DAY3_FORECAST_ENTITY_HELPER,
        )
        remaining_shared = _shared_input_field(
            self._runtime,
            "forecast",
            "remaining_today",
        )
        remaining_configured = (
            getattr(remaining_shared, "entity_id", None)
            if remaining_shared is not _SHARED_INPUT_MISSING
            else None
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
            remaining_configured,
        )
        day3_entity, day3_forecast_state = _first_numeric_state(
            self.hass,
            DAY3_FORECAST_CANDIDATES,
            day3_configured,
        )
        forecast_update_state = resolve_solcast_update_state(self.hass.states)
        forecast_sun_state = self.hass.states.get("sun.sun")
        today_forecast_sample = evaluate_pv_forecast_usefulness(
            today_forecast_state,
            now,
            target_date=now.date(),
            max_age_seconds=_TODAY_FORECAST_MAX_AGE_SECONDS,
            update_state=forecast_update_state,
            sun_state=forecast_sun_state,
        )
        tomorrow_forecast_sample = evaluate_pv_forecast_usefulness(
            tomorrow_forecast_state,
            now,
            target_date=now.date() + timedelta(days=1),
            max_age_seconds=_TOMORROW_FORECAST_MAX_AGE_SECONDS,
            update_state=forecast_update_state,
            sun_state=forecast_sun_state,
        )
        day3_forecast_sample = evaluate_pv_forecast_usefulness(
            day3_forecast_state,
            now,
            target_date=now.date() + timedelta(days=2),
            max_age_seconds=_DAY3_FORECAST_MAX_AGE_SECONDS,
            update_state=forecast_update_state,
            sun_state=forecast_sun_state,
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
        # Tomorrow's sale slots require both fresh prices and fresh PV. Its
        # independently fresh PV/P10 still informs the protected home horizon
        # while prices are pending. Missing prices must not manufacture a
        # missing P10 and erase today's valid production via the zero-PV guard.
        tomorrow_rows_complete = bool(
            tomorrow_price_rows_complete and forecast_tomorrow_data_fresh
        )
        usable_tomorrow_rows = tomorrow_rows if tomorrow_rows_complete else []
        usable_tomorrow_forecast_state = (
            tomorrow_forecast_state if forecast_tomorrow_data_fresh else None
        )

        load_model_values = self._load_model_values(now)
        history_load = load_model_values["history_load"]
        load_history_days = load_model_values["load_history_days"]
        load_history_source = load_model_values["load_history_source"]
        night_load = load_model_values["average_night_load"]
        night_history_days = load_model_values["night_history_days"]
        fallback_load = load_model_values["fallback_load"]
        actual_load_today = load_model_values["actual_load_today"]
        # The direct daily counter is retained with its own observation time;
        # it is compared with the historical profile over the same elapsed
        # interval instead of being extrapolated by clock time.
        live_daily_projection = load_model_values["live_daily_projection"]
        history_complete = load_model_values["history_complete"]
        average_load = load_model_values["average_load"]
        load_model_source = load_model_values["load_model_source"]
        if average_load is None:
            required["sensor.hoymiles_load_average_4_days"] = None

        missing = sorted(
            entity_id for entity_id, value in required.items() if value is None
        )
        profile_history = load_model_values["profile_history"]
        diagnostic_history = load_model_values["diagnostic_history"]
        (
            conservative_daily_load,
            conservative_load_days,
        ) = robust_weighted_upper_estimate(
            tuple(profile_history.daily_energy_kwh.values()),
            ages_days=daily_ages_days(
                tuple(profile_history.daily_energy_kwh), as_of=now.date()
            ),
        )
        (
            conservative_night_load,
            conservative_night_days,
        ) = robust_weighted_upper_estimate(
            tuple(profile_history.night_energy_kwh.values()),
            ages_days=daily_ages_days(
                tuple(profile_history.night_energy_kwh), as_of=now.date()
            ),
        )
        learning_policy, learning_diagnostics = (
            _forecast_learning_policy_snapshot(
                self.hass,
                now,
                getattr(self, "_runtime", None),
            )
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
        today_learning_policy, tomorrow_learning_policy, day3_learning_policy = (
            forecast_policy_for_source(
                learning_policy, getattr(state, "attributes", {}),
            )
            for state in (
                today_forecast_state, usable_tomorrow_forecast_state,
                usable_day3_forecast_state,
            )
        )
        forecast_factor_used = forecast_factor_for_policy(
            today_learning_policy, self._forecast_accuracy_factor,
        )
        tomorrow_forecast_factor = forecast_factor_for_policy(
            tomorrow_learning_policy, self._forecast_accuracy_factor,
        )
        day3_forecast_factor = forecast_factor_for_policy(
            day3_learning_policy, self._forecast_accuracy_factor,
        )
        forecast_history_days_used = (
            self._forecast_accuracy_days if today_learning_policy.enabled else 0
        )
        forecast_uncertainty_used = (
            self._forecast_accuracy_uncertainty
            if today_learning_policy.enabled
            else 0.15
        )
        metadata: dict[str, Any] = {
            **learning_diagnostics,
            "forecast_learning_enabled": today_learning_policy.enabled,
            "forecast_learning_mode": today_learning_policy.mode,
            "forecast_learning_excluded_reason": (
                today_learning_policy.excluded_reason
            ),
            "forecast_factor_used": round(forecast_factor_used, 3),
            "forecast_tomorrow_factor_used": round(tomorrow_forecast_factor, 3),
            "forecast_day3_factor_used": round(day3_forecast_factor, 3),
            "forecast_tomorrow_learning_mode": tomorrow_learning_policy.mode,
            "forecast_day3_learning_mode": day3_learning_policy.mode,
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
            "forecast_day3_source_fresh": day3_forecast_sample.source_fresh,
            "forecast_day3_usability_mode": day3_forecast_sample.mode,
            "forecast_day3_usable": day3_forecast_sample.usable,
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
            "rce_today_source_entity": (
                "sensor.hoymiles_rce_day_tomorrow"
                if today_rows_source_role == "previous_tomorrow_rollover"
                else "sensor.hoymiles_rce_day"
            ),
            "rce_today_source_role": today_rows_source_role,
            "rce_today_rollover_used": (
                today_rows_data_fresh
                and today_rows_source_role == "previous_tomorrow_rollover"
            ),
            "rce_today_business_date": now.date().isoformat(),
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
            "forecast_today_source_fresh": today_forecast_sample.source_fresh,
            "forecast_today_usability_mode": today_forecast_sample.mode,
            "forecast_today_usable": today_forecast_sample.usable,
            "forecast_last_success": (
                today_forecast_sample.last_success_at.isoformat()
                if today_forecast_sample.last_success_at is not None
                else None
            ),
            "forecast_next_update": (
                today_forecast_sample.next_update_at.isoformat()
                if today_forecast_sample.next_update_at is not None
                else None
            ),
            "forecast_valid_until": (
                today_forecast_sample.valid_until.isoformat()
                if today_forecast_sample.valid_until is not None
                else None
            ),
            "forecast_target_date": today_forecast_sample.target_date.isoformat(),
            "forecast_coverage_start_date": (
                today_forecast_sample.coverage_start_date.isoformat()
                if today_forecast_sample.coverage_start_date is not None
                else None
            ),
            "forecast_coverage_end_date": (
                today_forecast_sample.coverage_end_date.isoformat()
                if today_forecast_sample.coverage_end_date is not None
                else None
            ),
            "forecast_coverage_complete": today_forecast_sample.coverage_complete,
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
            "forecast_tomorrow_source_fresh": (
                tomorrow_forecast_sample.source_fresh
            ),
            "forecast_tomorrow_usability_mode": tomorrow_forecast_sample.mode,
            "forecast_tomorrow_usable": tomorrow_forecast_sample.usable,
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
            "recorder_load_accepted_energy_days": profile_history.daily_history_days,
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
                profile_history.profile_history_days
            ),
            "recorder_load_accepted_profile_days": profile_history.profile_history_days,
            "recorder_load_daily_coverage_ratio": round(
                profile_history.daily_coverage_ratio, 4
            ),
            "recorder_load_profile_coverage_ratio": round(
                profile_history.profile_coverage_ratio, 4
            ),
            "recorder_load_daily_quality": diagnostic_history.daily_quality_by_date,
            "recorder_load_profile_quality": diagnostic_history.profile_quality_by_date,
            "recorder_load_phase_quality": diagnostic_history.phase_quality_by_date,
            "recorder_load_phase_diagnostics": (
                diagnostic_history.phase_diagnostics_by_date
            ),
            "recorder_load_availability_diagnostics": (
                diagnostic_history.availability_diagnostics_by_date
            ),
            **qualification_diagnostics(diagnostic_history),
            "recorder_load_data_retained_after_outage": bool(
                self._load_history_store_payload is not None
                and profile_history.daily_history_days
                and self._load_history_read_status
                in {
                    "short_io_failed", "short_phase_complete_dense_unavailable",
                    "short_no_new_data", "extended_no_new_data", "extended_partial",
                }
            ),
            "recorder_load_current_sample_count": self._load_persistence_sample_count,
            "load_profile_generated_at": (
                self._load_profile_generated_at.isoformat()
                if self._load_profile_generated_at is not None
                else None
            ),
            "load_history_read_status": self._load_history_read_status,
            "load_history_read_error": self._load_history_read_error,
            "load_history_last_attempt_at": (
                self._load_history_last_attempt_at.isoformat()
                if self._load_history_last_attempt_at is not None
                else None
            ),
            "load_history_last_success_at": (
                self._load_history_last_success_at.isoformat()
                if self._load_history_last_success_at is not None
                else None
            ),
            "load_history_retry_count": self._load_history_retry_count,
            "load_history_next_retry_at": (
                self._load_history_next_retry_at.isoformat()
                if self._load_history_next_retry_at is not None
                else None
            ),
            "load_history_short_range": (
                [stamp.isoformat() for stamp in self._load_history_short_range]
                if self._load_history_short_range is not None
                else None
            ),
            "load_history_extended_range": (
                [stamp.isoformat() for stamp in self._load_history_extended_range]
                if self._load_history_extended_range is not None
                else None
            ),
            "load_history_extended_partial": self._load_history_extended_partial,
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
            "recorder_night_completed_windows": self._load_history.night_history_days,
            "recorder_night_quality": diagnostic_history.night_quality_by_date,
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
            "actual_load_energy_today_observed_at": (
                load_model_values["actual_load_observed_at"].isoformat()
                if load_model_values["actual_load_observed_at"] is not None
                else None
            ),
            "load_persistence_delta_kw": round(
                self._load_persistence_delta_kw, 3
            ),
            "load_persistence_observed_at": (
                self._load_persistence_observed_at.isoformat()
                if self._load_persistence_observed_at is not None
                else None
            ),
            "load_persistence_sample_count": self._load_persistence_sample_count,
            "load_model_schema": "load_forecast_v2",
            "load_model_quality": (
                "complete"
                if history_complete and profile_history.profile_history_days
                else "fallback"
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
        assert rce_inverter_power is not None
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
        ) if forecast_tomorrow_data_fresh else 0.0
        actual_pv_today = _state_number(
            self.hass,
            "sensor.hoymiles_hit_pv_total_energy_today",
        ) or 0.0
        remaining_sample = evaluate_pv_forecast_usefulness(
            remaining_state,
            now,
            target_date=now.date(),
            max_age_seconds=18 * 3600.0,
            update_state=forecast_update_state,
            sun_state=forecast_sun_state,
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
                "forecast_remaining_today_source_fresh": (
                    remaining_sample.source_fresh
                ),
                "forecast_remaining_today_usability_mode": remaining_sample.mode,
                "forecast_remaining_today_usable": remaining_sample.usable,
            }
        )
        sunrise_minute = rising.hour * 60 + rising.minute
        sunset_minute = setting.hour * 60 + setting.minute
        night_start = (sunset_minute - 90) % (24 * 60)
        night_end = (sunrise_minute + 90) % (24 * 60)
        (
            load_current_day_correction_ratio,
            load_current_day_residual_kwh,
            load_expected_to_observation_kwh,
        ) = current_day_profile_correction(
            now=now,
            observed_energy_kwh=actual_load_today,
            observed_at=load_model_values["actual_load_observed_at"],
            daily_energy_kwh=average_load,
            average_profile_30m_kwh=profile_history.average_profile_kwh,
            weekday_profile_30m_kwh=profile_history.weekday_profile_kwh,
            weekend_profile_30m_kwh=profile_history.weekend_profile_kwh,
            night_energy_kwh=night_load,
            night_start_minute=night_start,
            night_end_minute=night_end,
            persistence_delta_kw=self._load_persistence_delta_kw,
            persistence_observed_at=self._load_persistence_observed_at,
        )
        load_current_day_correction_kwh = (
            load_expected_to_observation_kwh
            * (load_current_day_correction_ratio - 1.0)
        )
        shared_snapshot = _shared_inputs_snapshot(self._runtime)
        metadata.update(
            {
                "load_model_revision": getattr(shared_snapshot, "revision", None),
                "load_current_day_correction_ratio": round(
                    load_current_day_correction_ratio, 4
                ),
                "load_current_day_correction_kwh": round(
                    load_current_day_correction_kwh, 3
                ),
                "load_current_day_residual_kwh": round(
                    load_current_day_residual_kwh, 3
                ),
                "load_expected_to_observation_kwh": round(
                    load_expected_to_observation_kwh, 3
                ),
            }
        )

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
        # Compare cumulative production and forecast at the same observation
        # time. Advancing wall time across the solver await must not change
        # immutable PV maps when no input report has changed.
        actual_pv_sample = numeric_state_sample(
            self.hass.states.get("sensor.hoymiles_hit_pv_total_energy_today"),
            now, max_age_seconds=300.0, minimum=0.0,
            future_tolerance_seconds=0.0,
        )
        pv_observed_at = actual_pv_sample.reported_at
        pv_observation_current = bool(
            actual_pv_sample.fresh
            and pv_observed_at is not None
            and pv_observed_at.tzinfo is not None
            and pv_observed_at.utcoffset() is not None
            and pv_observed_at.astimezone(timezone).date() == now.date()
        )
        expected_elapsed_raw = (
            _detailed_pv_expected_elapsed_kwh(
                today_forecast_state, now.date(), timezone,
                pv_observed_at.astimezone(timezone),
            )
            if pv_observation_current else None
        )
        live_eligible = (
            today_learning_policy.enabled
            and pv_observation_current
            and sunrise_today is not None
            and sunset_today is not None
            and pv_observed_at >= sunrise_today.astimezone(timezone) + timedelta(minutes=90)
            and pv_observed_at <= sunset_today.astimezone(timezone) + timedelta(minutes=30)
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
            forecast_tomorrow_raw * tomorrow_forecast_factor
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
        # A wider P10â€“P90 band moves reserve planning further toward P10.
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
            day3_raw * day3_forecast_factor
            if day3_raw is not None
            else None
        )
        day3_low = (
            day3_p10_raw * day3_forecast_factor
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
        shared_charge_efficiency = _shared_sample_value(
            self._runtime,
            "efficiency",
            "pv_to_battery_efficiency",
        )
        shared_discharge_efficiency = _shared_sample_value(
            self._runtime,
            "efficiency",
            "battery_to_home_efficiency",
        )
        charge_efficiency = (
            _state_number(
                self.hass,
                "input_number.hoymiles_tariff_charge_efficiency",
            )
            if shared_charge_efficiency is _SHARED_INPUT_MISSING
            else float(shared_charge_efficiency)
            if type(shared_charge_efficiency) in {int, float}
            and isfinite(float(shared_charge_efficiency))
            else None
        )
        discharge_efficiency = (
            _state_number(
                self.hass,
                "input_number.hoymiles_tariff_discharge_efficiency",
            )
            if shared_discharge_efficiency is _SHARED_INPUT_MISSING
            else float(shared_discharge_efficiency)
            if type(shared_discharge_efficiency) in {int, float}
            and isfinite(float(shared_discharge_efficiency))
            else None
        )
        charge_efficiency = charge_efficiency or 95.0
        discharge_efficiency = discharge_efficiency or 95.0
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
        pv_total_power_w = _policy_stable_number(
            self._runtime,
            "power",
            "pv_power_kw",
            self.hass,
            "sensor.hoymiles_hit_overview_pv_total_power",
            minimum=0.0,
            shared_scale=1000.0,
        ) or 0.0
        (
            current_load_power_kw,
            current_load_power_age_seconds,
            current_load_power_source,
        ) = _fresh_power_sample(
            self.hass,
            "sensor.hoymiles_actual_load_power",
            now,
            runtime=self._runtime,
            shared_field="home_load_power_kw",
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
            runtime=self._runtime,
            shared_field="pv_power_kw",
        )
        load_power_w = _policy_stable_number(
            self._runtime,
            "power",
            "home_load_power_kw",
            self.hass,
            "sensor.hoymiles_actual_load_power",
            minimum=0.0,
            shared_scale=1000.0,
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
        # Rejestr 2180 potrafi zawieraÄ‡ straty przetwarzania. Dla udziaĹ‚u
        # autokonsumpcji ograniczamy go do rzeczywistego obciÄ…ĹĽenia domu.
        pv_to_load_power_w = min(
            max(pv_to_load_power_w, 0.0),
            load_power_w,
        )
        battery_age = (
            battery_soc_sample.age_seconds / 60.0
            if battery_soc_sample.age_seconds is not None
            else None
        )
        rce_today_age = (
            today_rows_age_seconds / 60.0
            if today_rows_age_seconds is not None
            else None
        )
        rce_tomorrow_age = (
            tomorrow_rows_age_seconds / 60.0
            if tomorrow_rows_age_seconds is not None
            else None
        )
        forecast_today_age = _state_age_minutes(today_forecast_state, now)
        forecast_tomorrow_age = _state_age_minutes(
            tomorrow_forecast_state,
            now,
        )
        p10_missing = today_p10_raw is None or tomorrow_p10_raw is None
        p10_stale = not (
            today_forecast_sample.usable
            and tomorrow_forecast_sample.usable
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
        if not today_forecast_sample.usable:
            quality_score -= 15
            quality_issues.append("forecast_today_stale")
        if not tomorrow_forecast_sample.usable:
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
        if today_learning_policy.enabled and self._forecast_accuracy_days < 2:
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
                "inverter_power_each_kw": rce_inverter_power,
                "inverter_nameplate_power_each_kw": rated_power,
                "battery_discharge_calibration_applied": (
                    abs(rce_inverter_power - rated_power) >= 0.01
                ),
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
                inverter_power_kw=rce_inverter_power,
                inverter_ac_power_kw=rated_power,
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
                    actual_load_today
                ),
                actual_day_load_observed_at=load_model_values[
                    "actual_load_observed_at"
                ],
                persistence_delta_kw=self._load_persistence_delta_kw,
                persistence_observed_at=self._load_persistence_observed_at,
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
                tariff_price_schedule=getattr(
                    self._tariff_price_source,
                    "current_price_schedule",
                    None,
                ),
                self_consumption_filter_enabled=True,
            ),
            metadata,
        )
