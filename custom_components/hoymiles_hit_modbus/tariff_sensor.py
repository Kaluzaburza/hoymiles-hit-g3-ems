"""Home Assistant sensor exposing the automatic tariff charging plan."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import date, datetime, time, timedelta
import logging
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import MATCH_ALL, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, State, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
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
    robust_weighted_factor,
    uncertainty_risk_weight,
)
from .models import RuntimeData
from .optimizer_revision import (
    MAX_IMMEDIATE_RECALCULATIONS,
    OptimizerInputRevision,
    RCE_LOAD_BROKER_ATTRIBUTES,
    optimizer_input_fingerprint,
)
from .rce_sensor import (
    DAY3_FORECAST_CANDIDATES,
    DAY3_FORECAST_ENTITY_HELPER,
    FORECAST_EMS_PACKAGE_VERSION_ENTITY,
    FORECAST_EXPORT_ALLOWED_ENTITY,
    FORECAST_ENTITY_HELPERS,
    FORECAST_GCF_POLICY_TRIGGER_ENTITIES,
    FORECAST_GCF_COHORT_REPORT_ENTITIES,
    REMAINING_TODAY_CANDIDATES,
    TODAY_FORECAST_CANDIDATES,
    TODAY_FORECAST_ENTITY_HELPER,
    TOMORROW_FORECAST_CANDIDATES,
    TOMORROW_FORECAST_ENTITY_HELPER,
    _configured_forecast_entity_ids,
    _detailed_pv_map,
    _fallback_pv_map,
    _first_numeric_state,
    _FORECAST_GCF_READBACK_MAX_SKEW_SECONDS,
    _forecast_learning_policy_signature,
    _forecast_learning_policy_snapshot,
    _helper_minutes,
    _select_number,
    _state_number,
    _state_text,
)
from .tariff_optimizer import (
    TariffOptimizerInput,
    TariffOptimizerResult,
    TariffSchedule,
    floor_half_hour,
    horizon_gap_expensive_load_reserve_kwh,
    is_polish_public_holiday,
    optimize_tariff_charging,
    resolve_planning_horizon,
    robust_weighted_estimate,
    robust_weighted_upper_estimate,
)
from .tariff_profiles import (
    MANUAL_OPERATOR,
    PROFILE_YEAR,
    SUPPORTED_OPERATORS,
    get_tariff_profile,
    profile_is_valid,
    profile_summary,
)


_LOGGER = logging.getLogger(__name__)
CHARGE_POWER_FEEDBACK_MIN_SAMPLES = 5
PLANNING_HORIZON_TARGET_HOURS = 48.0
LIVE_PV_SURPLUS_MIN_KW = 0.20
LIVE_PV_SURPLUS_STABLE_SECONDS = 5 * 60.0
LIVE_TELEMETRY_MAX_AGE_SECONDS = 120.0
SLOW_TELEMETRY_MAX_AGE_SECONDS = 300.0
LOAD_BROKER_MAX_AGE_SECONDS = 300.0
FORECAST_MAX_AGE_SECONDS = 18 * 60 * 60.0
TARIFF_INPUT_RECALCULATION_DELAY_SECONDS = 5 * 60.0

WATCHED_TARIFF_ENTITIES = {
    "sensor.hoymiles_hit_rce_optimized_plan",
    "sensor.hoymiles_hit_battery_capacity",
    "sensor.hoymiles_hit_overview_battery_soc",
    "sensor.hoymiles_hit_battery_voltage_bms",
    "sensor.hoymiles_hit_maximum_charge_current",
    "sensor.hoymiles_hit_maximum_discharge_current",
    "sensor.hoymiles_hit_number_of_machines_master_and_slave",
    "sensor.hoymiles_hit_pv_total_energy_today",
    "sensor.hoymiles_hit_overview_battery_power",
    "sensor.hoymiles_actual_load_power",
    "sensor.hoymiles_hit_overview_pv_total_power",
    "sensor.hoymiles_hit_ems_self_use_soc_readback",
    "input_boolean.hoymiles_tariff_charge_enabled",
    "input_boolean.hoymiles_tariff_weekend_low_price",
    "input_boolean.hoymiles_tariff_polish_holidays_low_price",
    "input_select.hoymiles_tariff_operator",
    "input_select.hoymiles_tariff_type",
    "input_select.hoymiles_rce_inverter_rated_power",
    "input_number.hoymiles_tariff_g11_price",
    "input_number.hoymiles_tariff_low_price",
    "input_number.hoymiles_tariff_medium_price",
    "input_number.hoymiles_tariff_peak_price",
    "input_number.hoymiles_tariff_requested_charge_power",
    "input_number.hoymiles_tariff_charge_efficiency",
    "input_number.hoymiles_tariff_discharge_efficiency",
    "input_number.hoymiles_tariff_minimum_saving",
    "input_number.hoymiles_tariff_soc_safety_margin",
    "input_number.hoymiles_tariff_maximum_soc",
    "input_number.hoymiles_rce_fallback_daily_load",
    "input_datetime.hoymiles_tariff_cheap_1_start",
    "input_datetime.hoymiles_tariff_cheap_1_end",
    "input_datetime.hoymiles_tariff_cheap_2_start",
    "input_datetime.hoymiles_tariff_cheap_2_end",
    "input_datetime.hoymiles_tariff_medium_start",
    "input_datetime.hoymiles_tariff_medium_end",
    *FORECAST_ENTITY_HELPERS,
    "sun.sun",
    *TODAY_FORECAST_CANDIDATES,
    *TOMORROW_FORECAST_CANDIDATES,
    *DAY3_FORECAST_CANDIDATES,
    *REMAINING_TODAY_CANDIDATES,
}

# These planning inputs change continuously or arrive in forecast batches.
# The five-minute optimizer timer samples them as one coherent snapshot instead
# of withdrawing an accepted plan on every HA state event.  User settings,
# topology and BMS capability inputs remain event-driven and fail closed.
TARIFF_FIVE_MINUTE_COALESCED_ENTITIES = {
    "sensor.hoymiles_hit_rce_optimized_plan",
    "sensor.hoymiles_hit_overview_battery_soc",
    "sensor.hoymiles_hit_battery_voltage_bms",
    "sensor.hoymiles_hit_pv_total_energy_today",
    "sensor.hoymiles_hit_overview_battery_power",
    "sensor.hoymiles_actual_load_power",
    "sensor.hoymiles_hit_overview_pv_total_power",
    *FORECAST_ENTITY_HELPERS,
    "sun.sun",
    *TODAY_FORECAST_CANDIDATES,
    *TOMORROW_FORECAST_CANDIDATES,
    *DAY3_FORECAST_CANDIDATES,
    *REMAINING_TODAY_CANDIDATES,
}
TARIFF_EVENT_DRIVEN_ENTITIES = (
    WATCHED_TARIFF_ENTITIES - TARIFF_FIVE_MINUTE_COALESCED_ENTITIES
)

# These execution states are sampled only by the one-minute delivered-power
# feedback pass below. They do not participate in optimizer mathematics and
# therefore must not withdraw planning authority when an accepted cycle starts.
TARIFF_EXECUTION_FEEDBACK_ENTITIES = frozenset(
    {
        "input_boolean.hoymiles_tariff_charge_active",
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "sensor.hoymiles_hit_grid_to_battery_power",
        "sensor.hoymiles_hit_overview_load_active_power",
        "sensor.hoymiles_tariff_grid_charge_power",
    }
)

STATUS_TEXT = {
    "pl": {
        "soc_limits_conflict": (
            "Konflikt limit\u00f3w SOC \u2014 \u0142adowanie zablokowane"
        ),
        "hard_reserve_unavailable": (
            "Nie mo\u017cna odbudowa\u0107 rezerwy Self-Use \u2014 "
            "\u0142adowanie zablokowane przez limit mocy lub BMS"
        ),
        "ready": "Gotowa — zaplanowano tanie ładowanie",
        "no_charge_needed": "Brak potrzeby doładowania — PV i bateria wystarczą",
        "no_discount_window": "G11 — brak tańszej strefy do wykorzystania",
        "no_cheap_window": "Brak taniego okna przed prognozowanym deficytem",
        "not_economically_beneficial": (
            "Ładowanie pominięte — różnica cen nie pokrywa strat, zużycia "
            "baterii i wymaganego marginesu"
        ),
        "shortage_in_low_period": (
            "Deficyt przypada w taniej strefie — bezpośredni pobór bez strat baterii"
        ),
        "insufficient_cheap_window": "Tanie okna nie pokryją całego deficytu",
        "missing_data": "Brak wymaganych danych — ładowanie zablokowane",
        "optimizer_error": "Błąd obliczeń — ładowanie zablokowane",
        "unsupported_profile": "Ta grupa nie jest dostępna u wybranego operatora",
        "expired_profile": "Cennik wygasł — automatyczne ładowanie zablokowane",
    },
    "en": {
        "ready": "Ready — low-cost charging planned",
        "no_charge_needed": "No charging needed — PV and battery are sufficient",
        "no_discount_window": "G11 — no lower-cost period available",
        "no_cheap_window": "No low-cost period before the forecast shortage",
        "not_economically_beneficial": (
            "Charging skipped — the price spread does not cover losses, "
            "battery wear and the required margin"
        ),
        "shortage_in_low_period": (
            "Shortage occurs in the low-cost period — direct import avoids battery losses"
        ),
        "insufficient_cheap_window": "Low-cost periods cannot cover the full shortage",
        "hard_reserve_unavailable": (
            "Self-Use reserve cannot be restored — a hardware or charging "
            "limit blocks Grid Charge"
        ),
        "missing_data": "Required data missing — charging blocked",
        "optimizer_error": "Calculation error — charging blocked",
        "unsupported_profile": "This tariff group is unavailable for the selected DSO",
        "soc_limits_conflict": "SOC limits conflict - charging blocked",
        "expired_profile": "Tariff prices expired — automatic charging blocked",
    },
}


def _state_attribute_number(
    state: State | None,
    attribute: str,
) -> float | None:
    if state is None:
        return None
    try:
        return float(state.attributes[attribute])
    except (KeyError, TypeError, ValueError):
        return None


def _state_age_seconds(
    state: State | None,
    *,
    now: datetime | None = None,
) -> float | None:
    """Return signed entity age through the shared EMS data contract."""
    return state_age_seconds(state, now or dt_util.utcnow())


def _number_sample(
    hass: HomeAssistant,
    entity_id: str,
    *,
    now: datetime,
    max_age_seconds: float,
    minimum: float | None = None,
    maximum: float | None = None,
) -> tuple[float | None, bool, float | None]:
    """Return numeric value, freshness and age without treating zero as absent."""
    sample = numeric_state_sample(
        hass.states.get(entity_id),
        now,
        max_age_seconds=max_age_seconds,
        minimum=minimum,
        maximum=maximum,
    )
    return (
        sample.value,
        sample.fresh,
        sample.age_seconds,
    )


def _fresh_power_kw(
    hass: HomeAssistant,
    entity_id: str,
    *,
    max_age_seconds: float = 120.0,
    non_negative: bool = True,
) -> float | None:
    """Return a fresh non-negative live power in kW, or a safe fallback flag."""
    state = hass.states.get(entity_id)
    value = _state_number(hass, entity_id)
    if state is None or value is None or (non_negative and value < 0.0):
        return None
    reported = getattr(state, "last_reported", None) or state.last_updated
    age = (dt_util.utcnow() - reported).total_seconds()
    if age < -5.0 or age > max(max_age_seconds, 0.0):
        return None
    return value / 1000.0


def _fresh_power_sample(
    hass: HomeAssistant,
    entity_id: str,
    *,
    max_age_seconds: float = 120.0,
    non_negative: bool = True,
) -> tuple[float | None, float | None, str]:
    """Return live kW, age and a stable source reason for diagnostics."""
    state = hass.states.get(entity_id)
    if state is None:
        return None, None, "missing"
    reported = getattr(state, "last_reported", None) or state.last_updated
    age = max((dt_util.utcnow() - reported).total_seconds(), 0.0)
    value = _fresh_power_kw(
        hass,
        entity_id,
        max_age_seconds=max_age_seconds,
        non_negative=non_negative,
    )
    if value is None:
        raw = _state_number(hass, entity_id)
        if raw is None:
            return None, age, "not_numeric"
        if non_negative and raw < 0.0:
            return None, age, "invalid_negative"
        return None, age, "stale"
    return value, age, "live"


def _state_attribute_profile(
    state: State | None,
    attribute: str,
) -> tuple[float, ...]:
    """Return one validated 48-slot recorder profile from an entity attribute."""
    if state is None:
        return ()
    raw = state.attributes.get(attribute)
    if not isinstance(raw, (list, tuple)) or len(raw) != 48:
        return ()
    try:
        values = tuple(max(float(value), 0.0) for value in raw)
    except (TypeError, ValueError):
        return ()
    return values if sum(values) > 0 else ()


def _state_attribute_daily_values(
    state: State | None,
    attribute: str,
) -> tuple[float, ...]:
    """Return chronological, valid complete-day values from an attribute."""
    if state is None:
        return ()
    raw = state.attributes.get(attribute)
    if not isinstance(raw, dict):
        return ()
    values: list[float] = []
    for key in sorted(raw):
        try:
            value = float(raw[key])
        except (TypeError, ValueError):
            continue
        if 0.0 < value < 1_000.0:
            values.append(value)
    return tuple(values[-28:])


def _detailed_pv_expected_elapsed_kwh(
    state: State | None,
    target_date: date,
    timezone: ZoneInfo,
    now: datetime,
) -> float | None:
    """Return raw Solcast energy expected from midnight until ``now``.

    The integration exposes half-hour power estimates.  The current interval
    is counted proportionally so a recalculation shortly after sunrise does
    not compare a few minutes of real production with a complete 30-minute
    forecast block.
    """
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
        if not isinstance(item, dict):
            continue
        raw_start = item.get("period_start") or item.get("period_start_local")
        if not isinstance(raw_start, str):
            continue
        start = dt_util.parse_datetime(raw_start)
        if start is None:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone)
        start = start.astimezone(timezone)
        if start.date() != target_date or start >= now:
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
        elapsed_fraction = min(
            max((now - start).total_seconds() / (30 * 60), 0.0),
            1.0,
        )
        expected += power_kw * 0.5 * elapsed_fraction
        found = True
    return expected if found else None


def _forecast_interval_kwh(
    state: State | None,
) -> tuple[float | None, float | None, float | None]:
    """Read Solcast P10/P50/P90 totals without depending on one version."""
    if state is None:
        return None, None, None
    analysis = state.attributes.get("analysis")
    analysis = analysis if isinstance(analysis, dict) else {}

    def number(*keys: str) -> float | None:
        for source in (state.attributes, analysis):
            for key in keys:
                try:
                    value = float(source[key])
                except (KeyError, TypeError, ValueError):
                    continue
                if value >= 0.0:
                    return value
        return None

    return (
        number("estimate10", "estimate10_kwh"),
        number("estimate", "estimate_kwh"),
        number("estimate90", "estimate90_kwh"),
    )


class HoymilesTariffOptimizerSensor(SensorEntity, RestoreEntity):
    """Calculate a home-first two- or three-day low-tariff charging plan."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "tariff_charge_plan"
    _attr_icon = "mdi:battery-clock-outline"
    # The complete current plan remains available to automations, diagnostics,
    # and RestoreEntity. Recorder intentionally stores no custom attributes for
    # this entity because the bounded plan can still exceed its 16 KiB column.
    _unrecorded_attributes = frozenset({MATCH_ALL})

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        runtime: RuntimeData,
        rce_plan_source: SensorEntity | None = None,
    ) -> None:
        self.hass = hass
        self._entry = entry
        self._runtime = runtime
        self._rce_plan_source = rce_plan_source
        self._attr_unique_id = f"{entry.entry_id}_tariff_charge_plan"
        self._result: TariffOptimizerResult | None = None
        self._timeline_sensor: Any | None = None
        self._timeline_metadata: dict[str, Any] = {}
        self._attributes: dict[str, Any] = {
            "status_code": "missing_data",
            "missing_entities": [],
            "planned_slots": [],
            "current_run_need_class": "none",
            "current_run_start_eligible": False,
            "current_run_suppression_reason": "live_data_missing",
            "current_run_continue_eligible": False,
            "current_run_continue_reason": "live_data_missing",
            "control_inputs_fresh": False,
            "control_input_block_reason": "missing_data",
            "soc_data_fresh": False,
            "bms_charge_data_fresh": False,
            "bms_charge_available": False,
            "bms_discharge_data_fresh": False,
            "bms_discharge_available": False,
            "load_profile_data_fresh": False,
            "live_power_data_fresh": False,
        }
        self._forecast_accuracy_factor = 0.90
        self._forecast_accuracy_days = 0
        self._forecast_accuracy_source = "automatic_conservative_fallback"
        self._forecast_accuracy_uncertainty = 0.10
        self._forecast_accuracy_refreshed_at: datetime | None = None
        self._forecast_refresh_running = False
        self._forecast_retry_pending = False
        self._delivered_power_ratios: deque[float] = deque(maxlen=24)
        self._effective_charge_power_factor = 1.0
        self._effective_charge_power_source = "configured"
        self._charge_power_feedback_last_ratio: float | None = None
        self._charge_power_feedback_last_sample_at: datetime | None = None
        self._plan_signature: tuple[Any, ...] | None = None
        self._plan_changed_at: datetime | None = None
        self._current_run_intent_signature: tuple[Any, ...] | None = None
        self._current_run_intent_changed_at: datetime | None = None
        self._live_pv_surplus_started_at: datetime | None = None
        self._startup_warmup_task: asyncio.Task[None] | None = None
        self._forecast_policy_refresh_task: asyncio.Task[None] | None = None
        self._recalculate_cancel = None
        self._delayed_recalculate_tasks: set[asyncio.Task[None]] = set()
        self._lifecycle_stopped = False
        self._dynamic_forecast_entities: frozenset[str] = frozenset()
        self._dynamic_forecast_unsub = None
        self._forecast_gcf_policy_signature: (
            tuple[bool, str, str | None, float | None] | None
        ) = None
        self._forecast_gcf_policy_evaluation_cancel = None
        self._optimizer_lock = asyncio.Lock()
        self._input_revision = OptimizerInputRevision()
        self._attributes.update(
            {
                "result_current": False,
                "recalculation_pending": True,
                "input_revision": 0,
            }
        )

    def attach_timeline_sensor(self, timeline_sensor: Any) -> None:
        """Bind the one entry-local observation-only timeline publisher."""

        if self._timeline_sensor is not None and self._timeline_sensor is not timeline_sensor:
            raise RuntimeError("tariff timeline sensor already attached")
        self._timeline_sensor = timeline_sensor

    def attach_rce_plan_source(self, rce_plan_source: SensorEntity) -> None:
        """Bind the exact same-entry LOAD broker before entity registration."""

        source_entry = getattr(rce_plan_source, "_entry", None)
        if source_entry is None or source_entry.entry_id != self._entry.entry_id:
            raise RuntimeError("tariff LOAD broker belongs to another entry")
        self._rce_plan_source = rce_plan_source

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
        self._timeline_sensor.publish_current(
            self._result.timeline_trace,
            input_revision=self._input_revision.value,
            metadata=self._timeline_metadata,
        )

    def _same_entry_rce_plan_state(self) -> State | None:
        """Return only the exact RCE broker object wired for this config entry."""

        source = getattr(self, "_rce_plan_source", None)
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

    @property
    def suggested_object_id(self) -> str:
        return "hoymiles_hit_tariff_charge_plan"

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
        return STATUS_TEXT[language].get(
            code,
            STATUS_TEXT[language]["optimizer_error"],
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._attributes

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        attach_tariff_listener = getattr(
            getattr(self, "_rce_plan_source", None),
            "attach_tariff_plan_listener",
            None,
        )
        if callable(attach_tariff_listener):
            attach_tariff_listener()
        restored = await self.async_get_last_state()
        if (
            restored is not None
            and restored.attributes.get("charge_power_feedback_version") == 1
        ):
            try:
                factor = float(
                    restored.attributes["effective_charge_power_factor"]
                )
                sample_count = int(
                    restored.attributes.get(
                        "effective_charge_power_feedback_samples", 0
                    )
                )
            except (KeyError, TypeError, ValueError):
                factor = 1.0
                sample_count = 0
            if (
                0.50 <= factor <= 1.0
                and sample_count >= CHARGE_POWER_FEEDBACK_MIN_SAMPLES
            ):
                self._effective_charge_power_factor = factor
                self._effective_charge_power_source = (
                    "restored_grid_charge_feedback"
                )
                for _ in range(min(sample_count, self._delivered_power_ratios.maxlen)):
                    self._delivered_power_ratios.append(factor)
                try:
                    last_ratio = float(
                        restored.attributes.get(
                            "charge_power_feedback_last_ratio",
                            factor,
                        )
                    )
                except (TypeError, ValueError):
                    last_ratio = factor
                self._charge_power_feedback_last_ratio = min(
                    max(last_ratio, 0.35),
                    1.10,
                )
                last_sample_at = restored.attributes.get(
                    "charge_power_feedback_last_sample_at"
                )
                if isinstance(last_sample_at, str):
                    parsed = dt_util.parse_datetime(last_sample_at)
                    if parsed is not None:
                        self._charge_power_feedback_last_sample_at = parsed
        self.async_on_remove(
            async_track_state_change_event(
                self.hass,
                sorted(TARIFF_EVENT_DRIVEN_ENTITIES),
                self._async_input_changed,
            )
        )
        current_policy, _ = _forecast_learning_policy_snapshot(
            self.hass,
            dt_util.now(),
        )
        self._forecast_gcf_policy_signature = (
            _forecast_learning_policy_signature(current_policy)
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
        self.async_on_remove(
            lambda: self._cancel_forecast_gcf_policy_evaluation()
        )
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
                timedelta(minutes=5),
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass,
                self._async_forecast_accuracy_timer,
                timedelta(hours=12),
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
            "hoymiles tariff optimizer startup warmup",
        )
        self._startup_warmup_task = task
        self.async_on_remove(task.cancel)

    async def _async_startup_warmup(self) -> None:
        """Warm Recorder models after the fail-closed entity is registered."""
        try:
            await self._async_refresh_forecast_accuracy()
            self._invalidate_internal_inputs()
            await self._recalculate_and_write()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - retain the fail-closed initial state
            _LOGGER.exception("Cannot complete the tariff optimizer startup warmup")

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
            self._configured_forecast_source_ids() - WATCHED_TARIFF_ENTITIES
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

        rce_entity_id = getattr(
            getattr(self, "_rce_plan_source", None),
            "entity_id",
            None,
        )
        watched_entities = (
            WATCHED_TARIFF_ENTITIES
            | self._configured_forecast_source_ids()
        ) - {"sensor.hoymiles_hit_rce_optimized_plan"}
        if isinstance(rce_entity_id, str):
            watched_entities.add(rce_entity_id)
        return optimizer_input_fingerprint(
            self.hass,
            watched_entities,
            attribute_projections=(
                {rce_entity_id: RCE_LOAD_BROKER_ATTRIBUTES}
                if isinstance(rce_entity_id, str)
                else {}
            ),
        )

    @callback
    def _invalidate_input_event(
        self,
        event: Event[EventStateChangedData],
    ) -> bool:
        """Invalidate only when a value consumed by this optimizer changed."""
        entity_id = event.data["entity_id"]
        counterpart = entity_id == getattr(
            getattr(self, "_rce_plan_source", None),
            "entity_id",
            None,
        )
        changed = self._input_revision.invalidate_state_change(
            event.data.get("old_state"),
            event.data.get("new_state"),
            attributes=(RCE_LOAD_BROKER_ATTRIBUTES if counterpart else None),
            include_state=not counterpart,
            # A fresh report with the same value extends provenance freshness;
            # it does not change optimizer mathematics.  Availability, state
            # and consumed attributes remain immediate fail-closed inputs.
            include_last_updated=False,
        )
        if changed:
            self._mark_recalculation_pending()
        return changed

    @callback
    def _invalidate_internal_inputs(self) -> None:
        """Invalidate after recorder/clock/feedback inputs change."""
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

    @callback
    def _async_input_changed(self, event: Event[EventStateChangedData]) -> None:
        if event.data["entity_id"] in FORECAST_ENTITY_HELPERS:
            self._refresh_dynamic_forecast_listener()
        # Solcast may finish loading a few seconds after this integration.
        # When the first startup calibration found no forecast entity, retry
        # as soon as any watched source becomes available instead of keeping
        # the conservative fallback until the 12-hour timer fires.
        if (
            self._forecast_accuracy_refreshed_at is None
            and not self._forecast_refresh_running
            and not self._forecast_retry_pending
            and (
                self._startup_warmup_task is None
                or self._startup_warmup_task.done()
            )
            and self._forecast_accuracy_source_available()
        ):
            self._forecast_retry_pending = True
            task = self._entry.async_create_background_task(
                self.hass,
                self._async_retry_initial_forecast_accuracy(),
                "hoymiles tariff initial forecast calibration",
            )
            self.async_on_remove(task.cancel)
        if not self._invalidate_input_event(event):
            return
        if not self._lifecycle_stopped and self._recalculate_cancel is None:
            self._recalculate_cancel = async_call_later(
                self.hass,
                TARIFF_INPUT_RECALCULATION_DELAY_SECONDS,
                self._async_debounced_recalculate,
            )

    @callback
    def _async_forecast_gcf_policy_changed(
        self,
        event: Event[Any],
    ) -> None:
        """Accept a complete cohort or arm one bounded fail-closed deadline."""

        policy, _ = _forecast_learning_policy_snapshot(
            self.hass,
            dt_util.now(),
        )
        if policy.excluded_reason != "gcf_readback_incoherent":
            self._cancel_forecast_gcf_policy_evaluation()
            # A completed HA delivery cohort with the same physical policy is
            # not a new optimizer input.  A changed signature still invalidates
            # immediately in _apply_forecast_gcf_policy().
            self._apply_forecast_gcf_policy(policy)
            return
        if self._forecast_gcf_policy_evaluation_cancel is None:
            # Keep the last coherent, fresh generation while HA finishes
            # delivering this physical readback cohort.  The bounded deadline
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

    async def _async_evaluate_forecast_gcf_policy(
        self,
        now: datetime,
    ) -> None:
        """Replan only when a completed GCF read changes learning policy."""

        self._forecast_gcf_policy_evaluation_cancel = None
        policy, _ = _forecast_learning_policy_snapshot(
            self.hass,
            now,
        )
        self._apply_forecast_gcf_policy(policy, force_recalculate=True)

    @callback
    def _apply_forecast_gcf_policy(
        self,
        policy: ForecastLearningPolicy,
        *,
        force_recalculate: bool = False,
    ) -> None:
        """Apply one complete or deadline-expired physical GCF policy."""

        signature = _forecast_learning_policy_signature(policy)
        previous_signature = self._forecast_gcf_policy_signature
        signature_changed = signature != previous_signature
        if not signature_changed and not force_recalculate:
            return
        self._forecast_gcf_policy_signature = signature
        if signature_changed and policy.enabled and (
            previous_signature is None or not previous_signature[0]
        ):
            self._schedule_forecast_policy_refresh()
        self._invalidate_internal_inputs()
        if not self._lifecycle_stopped and self._recalculate_cancel is None:
            self._recalculate_cancel = async_call_later(
                self.hass,
                TARIFF_INPUT_RECALCULATION_DELAY_SECONDS,
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
            "hoymiles tariff forecast learning policy refresh",
        )
        self._forecast_policy_refresh_task = task
        self.async_on_remove(task.cancel)

    async def _async_refresh_forecast_after_policy_restore(self) -> None:
        """Rebuild and publish the adaptive model after GCF becomes usable."""

        await self._async_refresh_forecast_accuracy()
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
        if self._recalculate_cancel is not None:
            self._recalculate_cancel()
            self._recalculate_cancel = None
        self._update_delivered_power_feedback()
        self._invalidate_internal_inputs()
        await self._recalculate_and_write()

    def _update_delivered_power_feedback(self) -> None:
        """Learn a conservative real Grid Charge budget during active runs."""
        now = dt_util.utcnow()
        active_state = self.hass.states.get(
            "input_boolean.hoymiles_tariff_charge_active"
        )
        if active_state is None or active_state.state != "on":
            return
        if now - active_state.last_changed < timedelta(minutes=2):
            # Ignore inverter ramp-up and Modbus propagation after a command.
            return
        mode_sample = numeric_state_sample(
            self.hass.states.get("sensor.hoymiles_hit_ems_mode_readback_code"),
            now,
            max_age_seconds=LIVE_TELEMETRY_MAX_AGE_SECONDS,
            minimum=0.0,
            maximum=5.0,
        )
        if not mode_sample.fresh or mode_sample.value != 4.0:
            return
        if self._attributes.get("current_action") not in {
            "battery_charge",
            "grid_support_and_charge",
        }:
            return
        soc_sample = numeric_state_sample(
            self.hass.states.get("sensor.hoymiles_hit_overview_battery_soc"),
            now,
            max_age_seconds=SLOW_TELEMETRY_MAX_AGE_SECONDS,
            minimum=0.0,
            maximum=100.0,
        )
        soc = soc_sample.value if soc_sample.fresh else None
        maximum_soc = _state_number(
            self.hass, "input_number.hoymiles_tariff_maximum_soc"
        )
        if soc is None or maximum_soc is None or soc >= maximum_soc - 5.0:
            # Exclude the normal high-SOC taper from the power calibration.
            return
        battery_power_sample = numeric_state_sample(
            self.hass.states.get("sensor.hoymiles_hit_grid_to_battery_power"),
            now,
            max_age_seconds=LIVE_TELEMETRY_MAX_AGE_SECONDS,
        )
        battery_power_w = (
            battery_power_sample.value if battery_power_sample.fresh else None
        )
        battery_power_is_ac = battery_power_w is not None
        if battery_power_w is None:
            battery_power_sample = numeric_state_sample(
                self.hass.states.get("sensor.hoymiles_tariff_grid_charge_power"),
                now,
                max_age_seconds=LIVE_TELEMETRY_MAX_AGE_SECONDS,
            )
            battery_power_w = (
                battery_power_sample.value if battery_power_sample.fresh else None
            )
        requested_kw = self._attributes.get("requested_charge_power_kw")
        if battery_power_w is None or not isinstance(requested_kw, (int, float)):
            return
        battery_power_kw = max(battery_power_w, 0.0) / 1000.0
        if battery_power_kw < 0.25 or requested_kw < 0.5:
            return
        load_sample = numeric_state_sample(
            self.hass.states.get("sensor.hoymiles_hit_overview_load_active_power"),
            now,
            max_age_seconds=LIVE_TELEMETRY_MAX_AGE_SECONDS,
        )
        if not load_sample.fresh or load_sample.value is None:
            return
        load_kw = max(load_sample.value, 0.0) / 1000.0
        efficiency_value = _state_number(
            self.hass,
            "input_number.hoymiles_tariff_charge_efficiency",
        )
        efficiency = (
            90.0 if efficiency_value is None else efficiency_value
        ) / 100.0
        battery_ac_kw = (
            battery_power_kw
            if battery_power_is_ac
            else battery_power_kw / max(efficiency, 0.5)
        )
        delivered_grid_budget = battery_ac_kw + load_kw
        ratio = min(max(delivered_grid_budget / requested_kw, 0.35), 1.10)
        self._delivered_power_ratios.append(ratio)
        self._charge_power_feedback_last_ratio = ratio
        self._charge_power_feedback_last_sample_at = now
        if len(self._delivered_power_ratios) < CHARGE_POWER_FEEDBACK_MIN_SAMPLES:
            return
        target = min(max(median(self._delivered_power_ratios), 0.50), 1.0)
        # Smooth the estimate so one ramp-up minute cannot move a complete
        # charging window.  Recovery is allowed as later runs prove it.
        self._effective_charge_power_factor = (
            self._effective_charge_power_factor * 0.75 + target * 0.25
        )
        self._effective_charge_power_source = "live_grid_charge_feedback"

    async def _async_forecast_accuracy_timer(self, now: datetime) -> None:
        """Refresh complete-day Solcast calibration twice per day."""
        await self._async_refresh_forecast_accuracy()
        self._invalidate_internal_inputs()
        await self._recalculate_and_write()

    def _forecast_accuracy_source_available(self) -> bool:
        """Return whether the configured Solcast today source is numeric."""
        configured = _state_text(
            self.hass,
            TODAY_FORECAST_ENTITY_HELPER,
        )
        _, forecast_state = _first_numeric_state(
            self.hass,
            TODAY_FORECAST_CANDIDATES,
            configured,
        )
        return forecast_state is not None

    async def _async_retry_initial_forecast_accuracy(self) -> None:
        """Retry the startup calibration after late forecast discovery."""
        try:
            await self._async_refresh_forecast_accuracy()
            if self._forecast_accuracy_refreshed_at is not None:
                self._invalidate_internal_inputs()
                await self._recalculate_and_write()
        finally:
            self._forecast_retry_pending = False

    async def _recalculate_and_write(self) -> None:
        """Write only when the plan or its diagnostics actually changed."""
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

    async def _async_refresh_forecast_accuracy(self) -> None:
        """Learn a conservative PV factor from complete local days."""
        if self._forecast_refresh_running:
            return
        self._forecast_refresh_running = True
        try:
            timezone = ZoneInfo(self.hass.config.time_zone)
            now = dt_util.now().astimezone(timezone)
            learning_policy, _ = _forecast_learning_policy_snapshot(
                self.hass,
                now,
            )
            policy_signature = _forecast_learning_policy_signature(
                learning_policy
            )
            if not learning_policy.enabled:
                self._forecast_accuracy_refreshed_at = now
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
            start = now - timedelta(days=29)
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
            forecast_by_day: dict[Any, list[float]] = {}
            actual_by_day: dict[Any, list[float]] = {}
            for entity_id, destination in (
                (forecast_entity, forecast_by_day),
                (actual_entity, actual_by_day),
            ):
                for item in raw.get(entity_id, []):
                    updated = getattr(item, "last_updated", None)
                    state_value = getattr(item, "state", None)
                    if updated is None or state_value is None:
                        continue
                    try:
                        numeric = max(float(state_value), 0.0)
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

            dated_ratios: list[tuple[int, float]] = []
            for day in sorted(set(forecast_by_day) & set(actual_by_day))[-28:]:
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
                forecast = median(forecasts)
                actual = max(actuals)
                if actual <= 0.5:
                    continue
                age_days = max((now.date() - day).days, 1)
                dated_ratios.append(
                    (age_days, min(max(actual / forecast, 0.50), 1.10))
                )
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
            if dated_ratios:
                # Never increase a forecast automatically; optimism could cause
                # the home reserve to be undersized.  A weighted lower-middle
                # quantile is robust to one cloudless or failed-string day and
                # gives recent observations more authority.
                (
                    self._forecast_accuracy_factor,
                    self._forecast_accuracy_uncertainty,
                    self._forecast_accuracy_days,
                ) = robust_weighted_factor(
                    [(float(age), ratio) for age, ratio in dated_ratios]
                )
                self._forecast_accuracy_source = (
                    "recorder_28d_weighted_actual_vs_solcast"
                )
            else:
                # Preserve only a factor already learned from qualified days;
                # report zero currently retained evidence instead of leaving a
                # stale history count/source visible after Recorder purging.
                self._forecast_accuracy_days = 0
                if self._forecast_accuracy_source in {
                    "recorder_28d_weighted_actual_vs_solcast",
                    "retained_last_qualified_factor_no_current_history",
                }:
                    self._forecast_accuracy_source = (
                        "retained_last_qualified_factor_no_current_history"
                    )
            self._forecast_accuracy_refreshed_at = now
        except Exception:  # noqa: BLE001 - retain the conservative safe fallback
            _LOGGER.exception("Cannot learn Solcast forecast accuracy")
        finally:
            self._forecast_refresh_running = False

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
            # delivery cohort.  Keep the previous plan non-current until the
            # late report closes it or the bounded deadline fails closed.
            self._mark_recalculation_pending()
            return False
        captured_revision = self._input_revision.value
        captured_fingerprint = self._current_input_fingerprint()
        try:
            settings, metadata = self._optimizer_input()
            if settings is None:
                # A gap in required inputs breaks proof of a continuous live
                # PV charging surplus; it must earn the five-minute window
                # again after telemetry recovers.
                self._live_pv_surplus_started_at = None
                status_code = str(metadata.pop("_status_code", "missing_data"))
                self._result = None
                self._timeline_metadata = dict(metadata)
                self._attributes = {
                    "status_code": status_code,
                    "planned_slots": [],
                    "current_slot_planned": False,
                    "current_action": "none",
                    "current_run_need_class": "none",
                    "current_run_start_eligible": False,
                    "current_run_suppression_reason": "live_data_missing",
                    "current_run_continue_eligible": False,
                    "current_run_continue_reason": "live_data_missing",
                    "current_run_intent_stable_seconds": 0.0,
                    **metadata,
                }
                return True
            result = await self.hass.async_add_executor_job(
                optimize_tariff_charging,
                settings,
            )
            if (
                not self._input_revision.is_current(captured_revision)
                or captured_fingerprint != self._current_input_fingerprint()
            ):
                self._mark_recalculation_pending()
                return False
            self._result = result
            self._timeline_metadata = dict(metadata)
            broker_entity_id = getattr(
                getattr(self, "_rce_plan_source", None),
                "entity_id",
                None,
            )
            if isinstance(broker_entity_id, str):
                self._timeline_metadata["load_profile_broker_entity_id"] = (
                    broker_entity_id
                )
            planned_slots = [
                {
                    "date": item.start.date().isoformat(),
                    "start": item.start.strftime("%H:%M"),
                    "end": (
                        dt_util.as_utc(item.start) + timedelta(minutes=30)
                    ).astimezone(item.start.tzinfo).strftime("%H:%M"),
                    "zone": item.zone,
                    "price": round(item.price_pln_kwh, 4),
                    "action": item.action,
                    "grid_import_kwh": round(item.grid_import_kwh, 3),
                    "stored_energy_kwh": round(item.stored_energy_kwh, 3),
                    "direct_load_kwh": round(item.direct_load_kwh, 3),
                    "target_soc_percent": round(item.target_soc_percent, 1),
                }
                for item in result.planned_charges
            ]
            signature = tuple(
                (
                    item["date"],
                    item["start"],
                    item["action"],
                    round(float(item["grid_import_kwh"]), 1),
                )
                for item in planned_slots
            ) + ((round(result.target_soc_percent), result.status_code),)
            now = dt_util.now()
            if signature != self._plan_signature:
                self._plan_signature = signature
                self._plan_changed_at = now
            stable_seconds = (
                max((now - self._plan_changed_at).total_seconds(), 0.0)
                if self._plan_changed_at is not None
                else 0.0
            )
            current_run_action_family = (
                "support_only"
                if result.current_action == "grid_support"
                else "required_charge"
                if result.current_action in {
                    "battery_charge",
                    "grid_support_and_charge",
                }
                else "none"
            )
            current_run_intent = (
                current_run_action_family,
                result.current_slot_end,
                result.current_run_start_eligible,
                result.current_run_suppression_reason,
            )
            if current_run_intent != self._current_run_intent_signature:
                self._current_run_intent_signature = current_run_intent
                self._current_run_intent_changed_at = now
            current_run_intent_stable_seconds = (
                max(
                    (now - self._current_run_intent_changed_at).total_seconds(),
                    0.0,
                )
                if self._current_run_intent_changed_at is not None
                else 0.0
            )
            current_run_start_eligible = result.current_run_start_eligible
            current_run_suppression_reason = result.current_run_suppression_reason
            if (
                result.current_action == "grid_support"
                and result.current_run_start_eligible
                and current_run_intent_stable_seconds < 120.0
            ):
                current_run_start_eligible = False
                current_run_suppression_reason = "intent_not_stable"
            feedback_samples = len(self._delivered_power_ratios)
            feedback_ready = (
                feedback_samples >= CHARGE_POWER_FEEDBACK_MIN_SAMPLES
            )
            if self._effective_charge_power_source == "restored_grid_charge_feedback":
                feedback_state = "learned_restored"
            elif feedback_ready:
                feedback_state = "learned_live"
            elif feedback_samples:
                feedback_state = "collecting"
            else:
                feedback_state = "not_observed"
            feedback_median = (
                median(self._delivered_power_ratios)
                if self._delivered_power_ratios
                else None
            )
            self._attributes = {
                "status_code": result.status_code,
                "missing_entities": [],
                "planned_slots": planned_slots,
                "current_slot_planned": result.current_slot_planned,
                "current_action": result.current_action,
                "current_run_need_class": result.current_run_need_class,
                "current_run_start_eligible": current_run_start_eligible,
                "current_run_suppression_reason": (
                    current_run_suppression_reason
                ),
                "current_run_continue_eligible": (
                    result.current_run_continue_eligible
                ),
                "current_run_continue_reason": (
                    result.current_run_continue_reason
                ),
                "current_run_grid_import_kwh": round(
                    result.current_run_grid_import_kwh,
                    3,
                ),
                "current_run_direct_load_kwh": round(
                    result.current_run_direct_load_kwh,
                    3,
                ),
                "current_run_stored_kwh": round(
                    result.current_run_stored_kwh,
                    3,
                ),
                "current_run_benefit_pln": round(
                    result.current_run_benefit_pln,
                    3,
                ),
                "current_run_remaining_minutes": round(
                    result.current_run_duration_seconds / 60.0,
                    2,
                ),
                "current_run_intent_stable_seconds": round(
                    current_run_intent_stable_seconds,
                    1,
                ),
                "current_run_intent_stable_minutes": round(
                    current_run_intent_stable_seconds / 60.0,
                    2,
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
                "current_battery_power_kw": (
                    round(result.current_battery_power_kw, 3)
                    if result.current_battery_power_kw is not None
                    else None
                ),
                "base_reserve_soc_percent": round(
                    result.base_reserve_soc_percent,
                    1,
                ),
                "hard_reserve_deficit_kwh": round(
                    result.hard_reserve_deficit_kwh,
                    3,
                ),
                "hard_reserve_restoration_required": (
                    result.hard_reserve_restoration_required
                ),
                "hard_reserve_restored_by_near_term_pv": (
                    result.hard_reserve_restored_by_near_term_pv
                ),
                "hard_reserve_unavailable": result.hard_reserve_unavailable,
                "hard_reserve_shortfall_kwh": round(
                    result.hard_reserve_shortfall_kwh,
                    3,
                ),
                "hard_reserve_deferral_source": (
                    result.hard_reserve_deferral_source
                ),
                "live_pv_surplus_stable": result.live_pv_surplus_stable,
                "live_pv_surplus_stable_seconds": round(
                    result.live_pv_surplus_stable_seconds,
                    1,
                ),
                "load_risk_multiplier": round(
                    result.load_risk_multiplier,
                    3,
                ),
                "load_risk_buffer_kwh": round(
                    result.load_risk_buffer_kwh,
                    3,
                ),
                "expensive_window_load_buffers": [
                    {
                        "start": item.start.isoformat(),
                        "end": item.end.isoformat(),
                        "expected_load_kwh": round(
                            item.expected_load_kwh,
                            3,
                        ),
                        "conservative_load_kwh": round(
                            item.conservative_load_kwh,
                            3,
                        ),
                        "buffer_kwh": round(item.buffer_kwh, 3),
                    }
                    for item in result.expensive_window_load_buffers
                ],
                "morning_protection_active": (
                    result.morning_protection_active
                ),
                "morning_protection_mode": result.morning_protection_mode,
                "morning_protection_window_start": (
                    result.morning_protection_window_start.isoformat()
                    if result.morning_protection_window_start is not None
                    else None
                ),
                "morning_protection_window_end": (
                    result.morning_protection_window_end.isoformat()
                    if result.morning_protection_window_end is not None
                    else None
                ),
                "morning_protection_expected_pv_kwh": round(
                    result.morning_protection_expected_pv_kwh,
                    3,
                ),
                "morning_protection_conservative_pv_kwh": round(
                    result.morning_protection_conservative_pv_kwh,
                    3,
                ),
                "remaining_low_direct_import_kwh": round(
                    result.remaining_low_direct_import_kwh,
                    3,
                ),
                "remaining_expensive_import_kwh": round(
                    result.remaining_expensive_import_kwh,
                    3,
                ),
                "capacity_or_power_shortfall_kwh": round(
                    result.capacity_or_power_shortfall_kwh,
                    3,
                ),
                "current_slot_end": (
                    result.current_slot_end.isoformat()
                    if result.current_slot_end is not None
                    else None
                ),
                "current_price_pln_kwh": round(result.current_price_pln_kwh, 4),
                "current_zone": result.current_zone,
                "next_charge_start": (
                    result.next_charge_start.isoformat()
                    if result.next_charge_start is not None
                    else None
                ),
                "target_soc_percent": round(result.target_soc_percent, 1),
                "baseline_shortage_kwh": round(result.baseline_shortage_kwh, 2),
                "remaining_shortage_kwh": round(result.remaining_shortage_kwh, 2),
                "planned_grid_import_kwh": round(result.planned_grid_import_kwh, 2),
                "planned_stored_energy_kwh": round(
                    result.planned_stored_energy_kwh,
                    2,
                ),
                "planned_direct_load_kwh": round(
                    result.planned_direct_load_kwh,
                    2,
                ),
                "planned_cost_pln": round(result.planned_cost_pln, 2),
                "planned_battery_wear_cost_pln": round(
                    result.planned_battery_wear_cost_pln,
                    2,
                ),
                "battery_wear_cost_pln_kwh": round(
                    settings.battery_wear_cost_pln_kwh,
                    3,
                ),
                "baseline_grid_cost_pln": round(
                    result.baseline_grid_cost_pln,
                    2,
                ),
                "optimized_grid_cost_pln": round(
                    result.optimized_grid_cost_pln,
                    2,
                ),
                "baseline_optimization_cost_pln": round(
                    result.baseline_optimization_cost_pln,
                    2,
                ),
                "optimized_optimization_cost_pln": round(
                    result.optimized_optimization_cost_pln,
                    2,
                ),
                "automation_savings_pln": round(
                    result.automation_savings_pln,
                    2,
                ),
                "baseline_grid_import_kwh": round(
                    result.baseline_grid_import_kwh,
                    2,
                ),
                "optimized_grid_import_kwh": round(
                    result.optimized_grid_import_kwh,
                    2,
                ),
                "g11_reference_cost_pln": round(result.g11_reference_cost_pln, 2),
                "estimated_savings_pln": round(result.estimated_savings_pln, 2),
                "ending_battery_kwh": round(result.ending_battery_kwh, 2),
                "ending_battery_soc_percent": round(
                    result.ending_battery_soc_percent,
                    1,
                ),
                "effective_charge_power_kw": round(result.charge_power_kw, 2),
                "requested_charge_power_kw": round(
                    result.requested_charge_power_kw, 2
                ),
                "effective_charge_power_factor": round(
                    result.effective_power_factor, 3
                ),
                "effective_charge_power_source": (
                    self._effective_charge_power_source
                ),
                "charge_power_feedback_version": 1,
                "effective_charge_power_feedback_samples": feedback_samples,
                "charge_power_feedback_state": feedback_state,
                "charge_power_feedback_ready": feedback_ready,
                "charge_power_feedback_samples_required": (
                    CHARGE_POWER_FEEDBACK_MIN_SAMPLES
                ),
                "charge_power_feedback_samples_remaining": max(
                    CHARGE_POWER_FEEDBACK_MIN_SAMPLES - feedback_samples,
                    0,
                ),
                "charge_power_feedback_observed_median_ratio": (
                    round(feedback_median, 3)
                    if feedback_median is not None
                    else None
                ),
                "charge_power_feedback_last_ratio": (
                    round(self._charge_power_feedback_last_ratio, 3)
                    if self._charge_power_feedback_last_ratio is not None
                    else None
                ),
                "charge_power_feedback_last_sample_at": (
                    self._charge_power_feedback_last_sample_at.isoformat()
                    if self._charge_power_feedback_last_sample_at is not None
                    else None
                ),
                "charge_power_feedback_applied_factor": round(
                    result.effective_power_factor,
                    3,
                ),
                "planning_horizon_days": result.horizon_days,
                "planning_horizon_end": result.horizon_end.isoformat(),
                "planning_horizon_hours": round(
                    result.planning_horizon_hours,
                    2,
                ),
                "planning_horizon_extended_to_minimum": (
                    result.planning_horizon_extended_to_minimum
                ),
                "planning_slot_count": result.planning_slot_count,
                "terminal_reserve_soc_percent": round(
                    result.terminal_reserve_soc_percent, 1
                ),
                "terminal_reserve_soc_percent_effective": round(
                    result.effective_terminal_reserve_soc_percent,
                    1,
                ),
                "terminal_shortfall_kwh": round(
                    result.terminal_shortfall_kwh, 2
                ),
                "model_input_horizon_start": settings.now.isoformat(),
                "model_input_horizon_end": result.horizon_end.isoformat(),
                "model_input_horizon_hours": round(
                    result.planning_horizon_hours,
                    2,
                ),
                "model_input_modeled_pv_kwh": round(
                    result.modeled_pv_kwh,
                    2,
                ),
                "model_input_modeled_load_kwh": round(
                    result.modeled_load_kwh,
                    2,
                ),
                "model_input_battery_capacity_kwh": round(
                    settings.battery_capacity_kwh,
                    2,
                ),
                "model_input_battery_soc_percent": round(
                    settings.battery_soc_percent,
                    1,
                ),
                "model_input_reserve_soc_percent": round(
                    settings.reserve_soc_percent,
                    1,
                ),
                "model_input_maximum_soc_percent": round(
                    settings.maximum_soc_percent,
                    1,
                ),
                "model_input_terminal_reserve_soc_percent": round(
                    result.effective_terminal_reserve_soc_percent,
                    1,
                ),
                "model_input_requested_charge_power_kw": round(
                    result.requested_charge_power_kw,
                    2,
                ),
                "model_input_effective_charge_power_kw": round(
                    result.charge_power_kw,
                    2,
                ),
                "model_input_bms_charge_power_limit_kw": (
                    round(settings.battery_charge_power_kw, 2)
                    if settings.battery_charge_power_kw is not None
                    else None
                ),
                "model_input_bms_discharge_power_limit_kw": (
                    round(settings.battery_discharge_power_kw, 2)
                    if settings.battery_discharge_power_kw is not None
                    else None
                ),
                "model_input_charge_efficiency_percent": round(
                    settings.charge_efficiency_percent,
                    1,
                ),
                "model_input_discharge_efficiency_percent": round(
                    settings.discharge_efficiency_percent,
                    1,
                ),
                "model_input_minimum_saving_pln_kwh": round(
                    settings.minimum_saving_pln_kwh,
                    4,
                ),
                "model_input_battery_wear_cost_pln_kwh": round(
                    settings.battery_wear_cost_pln_kwh,
                    4,
                ),
                "model_input_tariff_operator": settings.schedule.operator,
                "model_input_tariff_type": settings.schedule.tariff_type,
                "model_input_tariff_g11_price_pln_kwh": round(
                    settings.schedule.g11_price_pln_kwh,
                    4,
                ),
                "model_input_tariff_low_price_pln_kwh": round(
                    settings.schedule.low_price_pln_kwh,
                    4,
                ),
                "model_input_tariff_medium_price_pln_kwh": round(
                    settings.schedule.medium_price_pln_kwh,
                    4,
                ),
                "model_input_tariff_peak_price_pln_kwh": round(
                    settings.schedule.peak_price_pln_kwh,
                    4,
                ),
                "plan_changed_at": (
                    self._plan_changed_at.isoformat()
                    if self._plan_changed_at is not None
                    else None
                ),
                "plan_stable_for_minutes": round(stable_seconds / 60.0, 1),
                **metadata,
            }
            return True
        except Exception:  # noqa: BLE001 - automation must fail closed
            if (
                not self._input_revision.is_current(captured_revision)
                or captured_fingerprint != self._current_input_fingerprint()
            ):
                self._mark_recalculation_pending()
                return False
            _LOGGER.exception("Cannot calculate the tariff charging plan")
            self._result = None
            self._timeline_metadata = {}
            self._attributes = {
                "status_code": "optimizer_error",
                "missing_entities": [],
                "planned_slots": [],
                "current_slot_planned": False,
                "current_action": "none",
                "current_run_need_class": "none",
                "current_slot_end": None,
                "current_run_start_eligible": False,
                "current_run_suppression_reason": "live_data_missing",
                "current_run_continue_eligible": False,
                "current_run_continue_reason": "live_data_missing",
                "current_run_intent_stable_seconds": 0.0,
                "control_inputs_fresh": False,
                "control_input_block_reason": "optimizer_error",
                "soc_data_fresh": False,
                "bms_charge_data_fresh": False,
                "bms_charge_available": False,
                "bms_discharge_data_fresh": False,
                "bms_discharge_available": False,
                "load_profile_data_fresh": False,
                "live_power_data_fresh": False,
            }
            return True

    def _optimizer_input(
        self,
    ) -> tuple[TariffOptimizerInput | None, dict[str, Any]]:
        timezone = ZoneInfo(self.hass.config.time_zone)
        now = dt_util.now().astimezone(timezone)
        now_slot = floor_half_hour(now)
        rce_state_raw = self._same_entry_rce_plan_state()
        load_profile_age_seconds = _state_age_seconds(rce_state_raw, now=now)
        load_profile_broker_fresh = bool(
            rce_state_raw is not None
            and rce_state_raw.state
            not in {STATE_UNKNOWN, STATE_UNAVAILABLE, "none"}
            and load_profile_age_seconds is not None
            and -5.0 <= load_profile_age_seconds <= LOAD_BROKER_MAX_AGE_SECONDS
        )
        load_profile_generated_at = None
        if rce_state_raw is not None:
            raw_generated_at = rce_state_raw.attributes.get(
                "load_profile_generated_at"
            )
            if isinstance(raw_generated_at, str):
                load_profile_generated_at = dt_util.parse_datetime(
                    raw_generated_at
                )
        load_profile_snapshot_age_seconds = (
            (now - load_profile_generated_at.astimezone(now.tzinfo)).total_seconds()
            if load_profile_generated_at is not None
            else None
        )
        load_profile_snapshot_fresh = bool(
            load_profile_snapshot_age_seconds is not None
            and -5.0 <= load_profile_snapshot_age_seconds <= 30 * 60 * 60
        )
        load_profile_broker_fresh = bool(
            load_profile_broker_fresh and load_profile_snapshot_fresh
        )
        # Never consume stale recorder-derived LOAD attributes. A configured
        # static fallback may still produce a preview, but stale broker data
        # cannot authorize the current Grid Charge transaction.
        rce_state = rce_state_raw if load_profile_broker_fresh else None

        (
            battery_soc_value,
            soc_data_fresh,
            soc_age_seconds,
        ) = _number_sample(
            self.hass,
            "sensor.hoymiles_hit_overview_battery_soc",
            now=now,
            max_age_seconds=SLOW_TELEMETRY_MAX_AGE_SECONDS,
            minimum=0.0,
            maximum=100.0,
        )
        (
            self_use_soc_value,
            self_use_soc_data_fresh,
            self_use_soc_age_seconds,
        ) = _number_sample(
            self.hass,
            "sensor.hoymiles_hit_ems_self_use_soc_readback",
            now=now,
            max_age_seconds=SLOW_TELEMETRY_MAX_AGE_SECONDS,
            minimum=10.0,
            maximum=100.0,
        )
        (
            inverter_count_value,
            inverter_count_data_fresh,
            inverter_count_age_seconds,
        ) = _number_sample(
            self.hass,
            "sensor.hoymiles_hit_number_of_machines_master_and_slave",
            now=now,
            max_age_seconds=SLOW_TELEMETRY_MAX_AGE_SECONDS,
        )
        inverter_count_data_fresh = bool(
            inverter_count_data_fresh
            and inverter_count_value is not None
            and 1.0 <= inverter_count_value <= 10.0
        )
        battery_voltage, battery_voltage_fresh, battery_voltage_age = (
            _number_sample(
                self.hass,
                "sensor.hoymiles_hit_battery_voltage_bms",
                now=now,
                max_age_seconds=SLOW_TELEMETRY_MAX_AGE_SECONDS,
            )
        )
        bms_charge_current, bms_charge_current_fresh, bms_charge_current_age = (
            _number_sample(
                self.hass,
                "sensor.hoymiles_hit_maximum_charge_current",
                now=now,
                max_age_seconds=SLOW_TELEMETRY_MAX_AGE_SECONDS,
            )
        )
        (
            bms_discharge_current,
            bms_discharge_current_fresh,
            bms_discharge_current_age,
        ) = _number_sample(
            self.hass,
            "sensor.hoymiles_hit_maximum_discharge_current",
            now=now,
            max_age_seconds=SLOW_TELEMETRY_MAX_AGE_SECONDS,
        )
        bms_charge_data_fresh = bool(
            battery_voltage_fresh and bms_charge_current_fresh
        )
        bms_discharge_data_fresh = bool(
            battery_voltage_fresh and bms_discharge_current_fresh
        )
        bms_charge_age_seconds = (
            max(battery_voltage_age, bms_charge_current_age)
            if battery_voltage_age is not None
            and bms_charge_current_age is not None
            else None
        )
        bms_discharge_age_seconds = (
            max(battery_voltage_age, bms_discharge_current_age)
            if battery_voltage_age is not None
            and bms_discharge_current_age is not None
            else None
        )
        # Missing or stale BMS limits are zero-throughput, never "unlimited".
        # A fresh exact zero is equally significant and remains zero.
        bms_power_kw = (
            max(
                battery_voltage if battery_voltage is not None else 0.0,
                0.0,
            )
            * max(
                bms_charge_current
                if bms_charge_current is not None
                else 0.0,
                0.0,
            )
            / 1000.0
            if bms_charge_data_fresh
            else 0.0
        )
        bms_discharge_power_kw = (
            max(
                battery_voltage if battery_voltage is not None else 0.0,
                0.0,
            )
            * max(
                bms_discharge_current
                if bms_discharge_current is not None
                else 0.0,
                0.0,
            )
            / 1000.0
            if bms_discharge_data_fresh
            else 0.0
        )
        bms_charge_available = bms_charge_data_fresh and bms_power_kw > 0.0
        bms_discharge_available = (
            bms_discharge_data_fresh and bms_discharge_power_kw > 0.0
        )

        (
            current_load_power_kw,
            current_load_power_age_seconds,
            current_load_power_source,
        ) = _fresh_power_sample(
            self.hass,
            "sensor.hoymiles_actual_load_power",
            max_age_seconds=LIVE_TELEMETRY_MAX_AGE_SECONDS,
        )
        (
            current_pv_power_kw,
            current_pv_power_age_seconds,
            current_pv_power_source,
        ) = _fresh_power_sample(
            self.hass,
            "sensor.hoymiles_hit_overview_pv_total_power",
            max_age_seconds=LIVE_TELEMETRY_MAX_AGE_SECONDS,
        )
        (
            current_battery_power_kw,
            current_battery_power_age_seconds,
            current_battery_power_source,
        ) = _fresh_power_sample(
            self.hass,
            "sensor.hoymiles_hit_overview_battery_power",
            max_age_seconds=LIVE_TELEMETRY_MAX_AGE_SECONDS,
            non_negative=False,
        )
        live_power_data_fresh = all(
            source == "live"
            for source in (
                current_load_power_source,
                current_pv_power_source,
                current_battery_power_source,
            )
        )

        required: dict[str, float | None] = {
            "sensor.hoymiles_hit_battery_capacity": _state_number(
                self.hass,
                "sensor.hoymiles_hit_battery_capacity",
            ),
            "sensor.hoymiles_hit_overview_battery_soc": battery_soc_value,
            "sensor.hoymiles_hit_ems_self_use_soc_readback": (
                self_use_soc_value if self_use_soc_data_fresh else None
            ),
            "sensor.hoymiles_hit_number_of_machines_master_and_slave": (
                inverter_count_value if inverter_count_data_fresh else None
            ),
            "input_number.hoymiles_tariff_soc_safety_margin": _state_number(
                self.hass,
                "input_number.hoymiles_tariff_soc_safety_margin",
            ),
            "input_number.hoymiles_tariff_maximum_soc": _state_number(
                self.hass,
                "input_number.hoymiles_tariff_maximum_soc",
            ),
            "input_number.hoymiles_tariff_requested_charge_power": _state_number(
                self.hass,
                "input_number.hoymiles_tariff_requested_charge_power",
            ),
            "input_number.hoymiles_tariff_charge_efficiency": _state_number(
                self.hass,
                "input_number.hoymiles_tariff_charge_efficiency",
            ),
            "input_number.hoymiles_tariff_discharge_efficiency": _state_number(
                self.hass,
                "input_number.hoymiles_tariff_discharge_efficiency",
            ),
            "input_number.hoymiles_tariff_minimum_saving": _state_number(
                self.hass,
                "input_number.hoymiles_tariff_minimum_saving",
            ),
            "input_number.hoymiles_tariff_g11_price": _state_number(
                self.hass,
                "input_number.hoymiles_tariff_g11_price",
            ),
            "input_number.hoymiles_tariff_low_price": _state_number(
                self.hass,
                "input_number.hoymiles_tariff_low_price",
            ),
            "input_number.hoymiles_tariff_medium_price": _state_number(
                self.hass,
                "input_number.hoymiles_tariff_medium_price",
            ),
            "input_number.hoymiles_tariff_peak_price": _state_number(
                self.hass,
                "input_number.hoymiles_tariff_peak_price",
            ),
        }
        daily_load_from_broker = _state_attribute_number(
            rce_state,
            "selected_average_daily_load_kwh",
        )
        daily_load = daily_load_from_broker
        night_load = _state_attribute_number(
            rce_state,
            "average_night_load_4d_kwh",
        )
        load_profile_source = "rce_recorder_broker"
        if daily_load is None:
            daily_load = _state_number(
                self.hass,
                "input_number.hoymiles_rce_fallback_daily_load",
            )
            load_profile_source = "configured_daily_fallback"
        load_profile_data_fresh = bool(
            (
                daily_load_from_broker is not None
                and load_profile_broker_fresh
            )
            or (
                rce_state_raw is None
                and daily_load is not None
                and daily_load > 0.0
            )
        )
        if daily_load is None:
            required["sensor.hoymiles_load_average_4_days"] = None
        complete_daily_loads = _state_attribute_daily_values(
            rce_state,
            "recorder_load_daily_kwh",
        )
        (
            robust_daily_load,
            load_uncertainty_ratio,
            robust_load_days,
        ) = robust_weighted_estimate(complete_daily_loads)
        (
            conservative_daily_load,
            conservative_load_days,
        ) = robust_weighted_upper_estimate(complete_daily_loads)
        provisional_load = _state_attribute_number(
            rce_state,
            "provisional_daily_load_projection_kwh",
        )
        if robust_daily_load is not None and robust_load_days >= 5:
            # Complete days provide the stable baseline.  Today's live
            # projection may only raise it, which reacts to exceptional demand
            # without allowing an incomplete morning to understate the home.
            daily_load = max(robust_daily_load, provisional_load or 0.0)
        average_load_profile = _state_attribute_profile(
            rce_state,
            "recorder_load_profile_30m_kwh",
        )
        if not average_load_profile:
            average_load_profile = _state_attribute_profile(
                rce_state,
                "recorder_load_average_profile_30m_kwh",
            )
        weekday_load_profile = _state_attribute_profile(
            rce_state,
            "recorder_load_weekday_profile_30m_kwh",
        )
        weekend_load_profile = _state_attribute_profile(
            rce_state,
            "recorder_load_weekend_profile_30m_kwh",
        )

        rated_power = _select_number(
            self.hass,
            "input_select.hoymiles_rce_inverter_rated_power",
        )
        if rated_power is None:
            required["input_select.hoymiles_rce_inverter_rated_power"] = None

        window_entities = (
            "input_datetime.hoymiles_tariff_cheap_1_start",
            "input_datetime.hoymiles_tariff_cheap_1_end",
            "input_datetime.hoymiles_tariff_cheap_2_start",
            "input_datetime.hoymiles_tariff_cheap_2_end",
            "input_datetime.hoymiles_tariff_medium_start",
            "input_datetime.hoymiles_tariff_medium_end",
        )
        window_values = {
            entity_id: _helper_minutes(self.hass, entity_id)
            for entity_id in window_entities
        }
        required.update(window_values)

        tariff_type = _state_text(self.hass, "input_select.hoymiles_tariff_type")
        if tariff_type not in {"G11", "G12", "G12w", "G13"}:
            required["input_select.hoymiles_tariff_type"] = None
        operator = _state_text(
            self.hass,
            "input_select.hoymiles_tariff_operator",
        )
        if operator not in {*SUPPORTED_OPERATORS, MANUAL_OPERATOR}:
            required["input_select.hoymiles_tariff_operator"] = None
            operator = MANUAL_OPERATOR
        profile = (
            get_tariff_profile(operator, tariff_type)
            if operator != MANUAL_OPERATOR and tariff_type is not None
            else None
        )
        unsupported_profile = (
            operator != MANUAL_OPERATOR
            and tariff_type in {"G11", "G12", "G12w", "G13"}
            and profile is None
        )

        today_configured = _state_text(
            self.hass,
            TODAY_FORECAST_ENTITY_HELPER,
        )
        tomorrow_configured = _state_text(
            self.hass,
            TOMORROW_FORECAST_ENTITY_HELPER,
        )
        day_3_configured = _state_text(
            self.hass,
            DAY3_FORECAST_ENTITY_HELPER,
        )
        today_entity, today_state = _first_numeric_state(
            self.hass,
            TODAY_FORECAST_CANDIDATES,
            today_configured,
        )
        tomorrow_entity, tomorrow_state = _first_numeric_state(
            self.hass,
            TOMORROW_FORECAST_CANDIDATES,
            tomorrow_configured,
        )
        remaining_entity, remaining_state = _first_numeric_state(
            self.hass,
            REMAINING_TODAY_CANDIDATES,
        )
        day_3_entity, day_3_state = _first_numeric_state(
            self.hass,
            DAY3_FORECAST_CANDIDATES,
            day_3_configured,
        )
        today_forecast_sample = numeric_state_sample(
            today_state,
            now,
            max_age_seconds=FORECAST_MAX_AGE_SECONDS,
            minimum=0.0,
        )
        tomorrow_forecast_sample = numeric_state_sample(
            tomorrow_state,
            now,
            max_age_seconds=FORECAST_MAX_AGE_SECONDS,
            minimum=0.0,
        )
        remaining_forecast_sample = numeric_state_sample(
            remaining_state,
            now,
            max_age_seconds=FORECAST_MAX_AGE_SECONDS,
            minimum=0.0,
        )
        day_3_forecast_sample = numeric_state_sample(
            day_3_state,
            now,
            max_age_seconds=FORECAST_MAX_AGE_SECONDS,
            minimum=0.0,
        )
        forecast_fresh = bool(
            today_forecast_sample.fresh
            and tomorrow_forecast_sample.fresh
        )
        remaining_forecast_fresh = remaining_forecast_sample.fresh
        today_forecast_age = (
            today_forecast_sample.age_seconds / 60.0
            if today_forecast_sample.age_seconds is not None
            else None
        )
        tomorrow_forecast_age = (
            tomorrow_forecast_sample.age_seconds / 60.0
            if tomorrow_forecast_sample.age_seconds is not None
            else None
        )
        remaining_forecast_age = (
            remaining_forecast_sample.age_seconds / 60.0
            if remaining_forecast_sample.age_seconds is not None
            else None
        )
        day_3_forecast_age = (
            day_3_forecast_sample.age_seconds / 60.0
            if day_3_forecast_sample.age_seconds is not None
            else None
        )
        if not forecast_fresh:
            required["Solcast forecast freshness"] = None
        if day_3_state is None:
            day_3_status = "missing"
        elif day_3_forecast_sample.fresh:
            day_3_status = "fresh"
        else:
            day_3_status = "stale"
        day_3_data_available = day_3_state is not None
        if day_3_status != "fresh":
            # Day 3 is optional.  Missing, stale, invalid or implausibly future
            # data reduces the horizon to two days instead of blocking an
            # otherwise safe tariff plan.
            day_3_state = None
        selected_horizon_days = 3 if day_3_status == "fresh" else 2
        (
            _,
            selected_horizon_end,
            selected_horizon_hours,
            selected_horizon_extended,
        ) = resolve_planning_horizon(
            now,
            selected_horizon_days,
            minimum_hours=(
                PLANNING_HORIZON_TARGET_HOURS
                if day_3_status == "fresh"
                else 0.0
            ),
        )
        horizon_gap_hours = max(
            PLANNING_HORIZON_TARGET_HOURS - selected_horizon_hours,
            0.0,
        )
        fallback_reason = (
            "none"
            if day_3_status == "fresh"
            else f"day_3_forecast_{day_3_status}"
        )
        if today_state is None:
            required["Solcast Forecast Today"] = None
        if tomorrow_state is None:
            required["Solcast Forecast Tomorrow"] = None

        sunrise_today = get_astral_event_date(self.hass, "sunrise", now.date())
        sunset_today = get_astral_event_date(self.hass, "sunset", now.date())
        sunrise_tomorrow = get_astral_event_date(
            self.hass,
            "sunrise",
            now.date() + timedelta(days=1),
        )
        sunset_tomorrow = get_astral_event_date(
            self.hass,
            "sunset",
            now.date() + timedelta(days=1),
        )
        sunrise_day_3 = get_astral_event_date(
            self.hass,
            "sunrise",
            now.date() + timedelta(days=2),
        )
        sunset_day_3 = get_astral_event_date(
            self.hass,
            "sunset",
            now.date() + timedelta(days=2),
        )
        if any(
            value is None
            for value in (
                sunrise_today,
                sunset_today,
                sunrise_tomorrow,
                sunset_tomorrow,
            )
        ):
            required["sun.sun"] = None

        control_input_block_reason = "none"
        if not self_use_soc_data_fresh:
            control_input_block_reason = (
                "self_use_soc_data_missing"
                if self_use_soc_value is None
                else "self_use_soc_data_stale"
            )
        elif not inverter_count_data_fresh:
            control_input_block_reason = (
                "inverter_count_data_missing"
                if inverter_count_value is None
                else "inverter_count_data_stale"
            )
        elif not soc_data_fresh:
            control_input_block_reason = (
                "soc_data_missing"
                if battery_soc_value is None
                else "soc_data_stale"
            )
        elif not bms_charge_data_fresh:
            control_input_block_reason = (
                "bms_charge_data_missing"
                if battery_voltage is None or bms_charge_current is None
                else "bms_charge_data_stale"
            )
        elif not bms_discharge_data_fresh:
            control_input_block_reason = (
                "bms_discharge_data_missing"
                if battery_voltage is None or bms_discharge_current is None
                else "bms_discharge_data_stale"
            )
        elif not load_profile_data_fresh:
            control_input_block_reason = (
                "load_profile_data_missing"
                if daily_load is None
                else "load_profile_data_stale"
            )
        control_inputs_fresh = control_input_block_reason == "none"

        missing = sorted(key for key, value in required.items() if value is None)
        learning_policy, learning_diagnostics = (
            _forecast_learning_policy_snapshot(self.hass, now)
        )
        self._forecast_gcf_policy_signature = (
            _forecast_learning_policy_signature(learning_policy)
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
            else 0.10
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
            "control_inputs_fresh": control_inputs_fresh,
            "control_input_block_reason": control_input_block_reason,
            "soc_data_fresh": soc_data_fresh,
            "soc_age_seconds": (
                round(soc_age_seconds, 1)
                if soc_age_seconds is not None
                else None
            ),
            "self_use_soc_data_fresh": self_use_soc_data_fresh,
            "self_use_soc_age_seconds": (
                round(self_use_soc_age_seconds, 1)
                if self_use_soc_age_seconds is not None
                else None
            ),
            "inverter_count_data_fresh": inverter_count_data_fresh,
            "inverter_count_age_seconds": (
                round(inverter_count_age_seconds, 1)
                if inverter_count_age_seconds is not None
                else None
            ),
            "bms_charge_data_fresh": bms_charge_data_fresh,
            "bms_charge_age_seconds": (
                round(bms_charge_age_seconds, 1)
                if bms_charge_age_seconds is not None
                else None
            ),
            "bms_charge_available": bms_charge_available,
            "bms_discharge_data_fresh": bms_discharge_data_fresh,
            "bms_discharge_age_seconds": (
                round(bms_discharge_age_seconds, 1)
                if bms_discharge_age_seconds is not None
                else None
            ),
            "bms_discharge_available": bms_discharge_available,
            "load_profile_data_fresh": load_profile_data_fresh,
            "load_profile_source": load_profile_source,
            "load_profile_broker_fresh": load_profile_broker_fresh,
            "load_profile_age_seconds": (
                round(max(load_profile_age_seconds, 0.0), 1)
                if load_profile_age_seconds is not None
                else None
            ),
            "load_profile_snapshot_age_seconds": (
                round(load_profile_snapshot_age_seconds, 1)
                if load_profile_snapshot_age_seconds is not None
                else None
            ),
            "live_power_data_fresh": live_power_data_fresh,
            "current_load_power_age_seconds": (
                round(current_load_power_age_seconds, 1)
                if current_load_power_age_seconds is not None
                else None
            ),
            "current_pv_power_age_seconds": (
                round(current_pv_power_age_seconds, 1)
                if current_pv_power_age_seconds is not None
                else None
            ),
            "current_battery_power_age_seconds": (
                round(current_battery_power_age_seconds, 1)
                if current_battery_power_age_seconds is not None
                else None
            ),
            "automatic_charge_enabled": self.hass.states.is_state(
                "input_boolean.hoymiles_tariff_charge_enabled",
                "on",
            ),
            "plan_is_preview": not self.hass.states.is_state(
                "input_boolean.hoymiles_tariff_charge_enabled",
                "on",
            ),
            "tariff_type": tariff_type or "none",
            "tariff_operator": operator,
            "forecast_today_entity": today_entity or "none",
            "forecast_tomorrow_entity": tomorrow_entity or "none",
            "forecast_remaining_today_entity": remaining_entity or "fallback",
            "forecast_day_3_entity": day_3_entity or "not_available",
            "forecast_day_3_configured_entity": (
                day_3_configured or "automatic"
            ),
            "forecast_day_3_available": day_3_state is not None,
            "forecast_day_3_source_available": day_3_data_available,
            "forecast_day_3_status": day_3_status,
            "forecast_day_3_data_fresh": day_3_forecast_sample.fresh,
            "forecast_day_3_data_complete": day_3_status == "fresh",
            "forecast_day_3_data_reason": day_3_forecast_sample.reason,
            "forecast_day_3_age_seconds": (
                round(day_3_forecast_sample.age_seconds, 1)
                if day_3_forecast_sample.age_seconds is not None
                else None
            ),
            "forecast_data_fresh": forecast_fresh,
            "forecast_today_data_fresh": today_forecast_sample.fresh,
            "forecast_today_data_reason": today_forecast_sample.reason,
            "forecast_today_age_seconds": (
                round(today_forecast_sample.age_seconds, 1)
                if today_forecast_sample.age_seconds is not None
                else None
            ),
            "forecast_tomorrow_data_fresh": tomorrow_forecast_sample.fresh,
            "forecast_tomorrow_data_reason": tomorrow_forecast_sample.reason,
            "forecast_tomorrow_age_seconds": (
                round(tomorrow_forecast_sample.age_seconds, 1)
                if tomorrow_forecast_sample.age_seconds is not None
                else None
            ),
            "forecast_remaining_today_data_fresh": (
                remaining_forecast_sample.fresh
            ),
            "forecast_remaining_today_data_reason": (
                remaining_forecast_sample.reason
            ),
            "forecast_remaining_today_age_seconds": (
                round(remaining_forecast_sample.age_seconds, 1)
                if remaining_forecast_sample.age_seconds is not None
                else None
            ),
            "forecast_today_age_minutes": (
                round(today_forecast_age, 1)
                if today_forecast_age is not None
                else None
            ),
            "forecast_tomorrow_age_minutes": (
                round(tomorrow_forecast_age, 1)
                if tomorrow_forecast_age is not None
                else None
            ),
            "forecast_day_3_age_minutes": (
                round(day_3_forecast_age, 1)
                if day_3_forecast_age is not None
                else None
            ),
            "planning_horizon_target_hours": PLANNING_HORIZON_TARGET_HOURS,
            "planning_horizon_selected_days": selected_horizon_days,
            "planning_horizon_selected_end": selected_horizon_end.isoformat(),
            "planning_horizon_selected_hours": round(
                selected_horizon_hours,
                2,
            ),
            "planning_horizon_fallback_active": day_3_status != "fresh",
            "planning_horizon_fallback_reason": fallback_reason,
            "planning_horizon_limited": horizon_gap_hours > 0.01,
            "planning_horizon_limitation_reason": (
                fallback_reason if horizon_gap_hours > 0.01 else "none"
            ),
            "planning_horizon_gap_to_target_hours": round(
                horizon_gap_hours,
                2,
            ),
            "planning_horizon_extended_for_dst": selected_horizon_extended,
            "average_daily_load_kwh": (
                round(daily_load, 2) if daily_load is not None else None
            ),
            "average_night_load_kwh": (
                round(night_load, 2) if night_load is not None else None
            ),
            "history_complete": (
                bool(rce_state.attributes.get("history_complete"))
                if rce_state is not None
                else False
            ),
            "robust_load_history_days": robust_load_days,
            "robust_average_daily_load_kwh": (
                round(robust_daily_load, 2)
                if robust_daily_load is not None
                else None
            ),
            "conservative_daily_load_p90_kwh": (
                round(conservative_daily_load, 2)
                if conservative_daily_load is not None
                else None
            ),
            "conservative_load_history_days": conservative_load_days,
            "load_uncertainty_percent": round(
                load_uncertainty_ratio * 100.0,
                1,
            ),
            "load_profile_mode": (
                "recorder_30m_weekday_weekend"
                if weekday_load_profile or weekend_load_profile
                else (
                    "recorder_30m_average"
                    if average_load_profile
                    else "flat_day_night_fallback"
                )
            ),
            "price_model": (
                "manual marginal all-in prices; fixed monthly fees excluded"
                if operator == MANUAL_OPERATOR
                else (
                    f"official {PROFILE_YEAR} regional profile; "
                    "fixed monthly fees excluded"
                )
            ),
            "default_price_reference": (
                "manual user invoice"
                if operator == MANUAL_OPERATOR
                else (
                    f"{PROFILE_YEAR} incumbent supplier and selected DSO tariffs"
                )
            ),
            "tariff_profile_expired": (
                operator != MANUAL_OPERATOR
                and profile is not None
                and (
                    not profile_is_valid(profile, now.date())
                    or not profile_is_valid(
                        profile,
                        now.date()
                        + timedelta(days=(3 if day_3_state is not None else 2) - 1),
                    )
                )
            ),
            "forecast_accuracy_factor": round(
                self._forecast_accuracy_factor,
                3,
            ),
            "forecast_accuracy_history_days": self._forecast_accuracy_days,
            "forecast_accuracy_source": self._forecast_accuracy_source,
            "forecast_accuracy_refreshed_at": (
                self._forecast_accuracy_refreshed_at.isoformat()
                if self._forecast_accuracy_refreshed_at is not None
                else None
            ),
            "forecast_accuracy_uncertainty": round(
                self._forecast_accuracy_uncertainty,
                3,
            ),
        }
        if profile is not None:
            metadata.update(
                profile_summary(
                    profile,
                    now,
                    is_public_holiday=is_polish_public_holiday(now.date()),
                )
            )
        elif unsupported_profile:
            metadata.update(
                {
                    "_status_code": "unsupported_profile",
                    "tariff_profile_supported": False,
                    "missing_entities": [],
                }
            )
            return None, metadata
        if metadata["tariff_profile_expired"]:
            metadata.update(
                {
                    "_status_code": "expired_profile",
                    "missing_entities": [],
                }
            )
            return None, metadata
        if missing:
            return None, metadata

        assert daily_load is not None
        assert rated_power is not None
        assert today_state is not None
        assert tomorrow_state is not None
        assert sunrise_today is not None
        assert sunset_today is not None
        assert sunrise_tomorrow is not None
        assert sunset_tomorrow is not None

        forecast_today = max(float(today_state.state), 0.0)
        forecast_tomorrow_raw = max(float(tomorrow_state.state), 0.0)
        forecast_day_3_raw = (
            max(float(day_3_state.state), 0.0)
            if day_3_state is not None
            else 0.0
        )
        actual_pv_today = _state_number(
            self.hass,
            "sensor.hoymiles_hit_pv_total_energy_today",
        ) or 0.0
        sunrise_today_local = sunrise_today.astimezone(timezone)
        sunset_today_local = sunset_today.astimezone(timezone)
        sunrise_tomorrow_local = sunrise_tomorrow.astimezone(timezone)
        sunset_tomorrow_local = sunset_tomorrow.astimezone(timezone)
        expected_elapsed_raw = _detailed_pv_expected_elapsed_kwh(
            today_state,
            now.date(),
            timezone,
            now,
        )
        live_adjustment_eligible = (
            learning_policy.enabled
            and now >= sunrise_today_local + timedelta(minutes=90)
            and now <= sunset_today_local + timedelta(minutes=30)
        )
        (
            today_forecast_factor,
            live_forecast_ratio,
            live_forecast_confidence,
        ) = adaptive_forecast_factor(
            forecast_factor_used,
            actual_pv_today,
            expected_elapsed_raw,
            eligible=live_adjustment_eligible,
        )
        today_p10, _, today_p90 = _forecast_interval_kwh(today_state)
        tomorrow_p10, _, tomorrow_p90 = _forecast_interval_kwh(tomorrow_state)
        day_3_p10, _, day_3_p90 = _forecast_interval_kwh(day_3_state)
        risk_weight = uncertainty_risk_weight(
            history_days=forecast_history_days_used,
            live_confidence=live_forecast_confidence,
            uncertainty_available=(
                today_p10 is not None
                or tomorrow_p10 is not None
                or day_3_p10 is not None
            ),
        )
        forecast_tomorrow_conservative_raw = blend_low_expected(
            tomorrow_p10
            if tomorrow_p10 is not None
            else forecast_tomorrow_raw,
            forecast_tomorrow_raw,
            risk_weight,
        )
        forecast_tomorrow = (
            forecast_tomorrow_conservative_raw * forecast_factor_used
        )
        forecast_day_3_conservative_raw = blend_low_expected(
            day_3_p10 if day_3_p10 is not None else forecast_day_3_raw,
            forecast_day_3_raw,
            risk_weight,
        )
        forecast_day_3 = (
            forecast_day_3_conservative_raw * forecast_factor_used
        )
        remaining_today_raw = (
            max(float(remaining_state.state), 0.0)
            if remaining_state is not None and remaining_forecast_fresh
            else max(forecast_today - actual_pv_today, 0.0)
        )
        today_interval_factor = (
            blend_low_expected(
                today_p10 if today_p10 is not None else forecast_today,
                forecast_today,
                risk_weight,
            )
            / max(forecast_today, 0.001)
            if forecast_today > 0.0
            else 1.0
        )
        remaining_today = (
            remaining_today_raw
            * today_forecast_factor
            * min(max(today_interval_factor, 0.0), 1.0)
        )

        pv_today = _detailed_pv_map(
            today_state,
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
                sunrise_today_local.hour * 60 + sunrise_today_local.minute,
                sunset_today_local.hour * 60 + sunset_today_local.minute,
            )
        pv_today_p10: dict[datetime, float] = {}
        if today_p10 is not None:
            remaining_today_p10 = min(
                remaining_today,
                max(today_p10 - actual_pv_today, 0.0)
                * today_forecast_factor,
            )
            pv_today_p10 = _detailed_pv_map(
                today_state,
                now.date(),
                remaining_today_p10,
                timezone,
                now_slot,
                percentile="p10",
            )
            if not pv_today_p10:
                pv_today_p10 = _fallback_pv_map(
                    now.date(),
                    remaining_today_p10,
                    timezone,
                    now_slot,
                    sunrise_today_local.hour * 60 + sunrise_today_local.minute,
                    sunset_today_local.hour * 60 + sunset_today_local.minute,
                )
        tomorrow_date = now.date() + timedelta(days=1)
        pv_tomorrow = _detailed_pv_map(
            tomorrow_state,
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
                sunrise_tomorrow_local.hour * 60 + sunrise_tomorrow_local.minute,
                sunset_tomorrow_local.hour * 60 + sunset_tomorrow_local.minute,
            )
        pv_tomorrow_p10: dict[datetime, float] = {}
        if tomorrow_p10 is not None:
            tomorrow_p10_effective = (
                min(tomorrow_p10, forecast_tomorrow_raw)
                * forecast_factor_used
            )
            pv_tomorrow_p10 = _detailed_pv_map(
                tomorrow_state,
                tomorrow_date,
                tomorrow_p10_effective,
                timezone,
                now_slot,
                percentile="p10",
            )
            if not pv_tomorrow_p10:
                pv_tomorrow_p10 = _fallback_pv_map(
                    tomorrow_date,
                    tomorrow_p10_effective,
                    timezone,
                    now_slot,
                    sunrise_tomorrow_local.hour * 60 + sunrise_tomorrow_local.minute,
                    sunset_tomorrow_local.hour * 60 + sunset_tomorrow_local.minute,
                )
        pv_by_slot = dict(pv_today)
        pv_p10_by_slot = dict(pv_today_p10)
        pv_p10_available_dates: set[date] = (
            {now.date()} if today_p10 is not None else set()
        )
        for start, energy in pv_tomorrow.items():
            pv_by_slot[start] = pv_by_slot.get(start, 0.0) + energy
        for start, energy in pv_tomorrow_p10.items():
            pv_p10_by_slot[start] = pv_p10_by_slot.get(start, 0.0) + energy
        if tomorrow_p10 is not None:
            pv_p10_available_dates.add(tomorrow_date)
        horizon_days = selected_horizon_days
        if day_3_state is not None:
            day_3_date = now.date() + timedelta(days=2)
            sunrise_day_3_local = (
                sunrise_day_3.astimezone(timezone)
                if sunrise_day_3 is not None
                else sunrise_tomorrow_local + timedelta(days=1)
            )
            sunset_day_3_local = (
                sunset_day_3.astimezone(timezone)
                if sunset_day_3 is not None
                else sunset_tomorrow_local + timedelta(days=1)
            )
            pv_day_3 = _detailed_pv_map(
                day_3_state,
                day_3_date,
                forecast_day_3,
                timezone,
                now_slot,
            )
            if not pv_day_3:
                pv_day_3 = _fallback_pv_map(
                    day_3_date,
                    forecast_day_3,
                    timezone,
                    now_slot,
                    sunrise_day_3_local.hour * 60 + sunrise_day_3_local.minute,
                    sunset_day_3_local.hour * 60 + sunset_day_3_local.minute,
                )
            for start, energy in pv_day_3.items():
                pv_by_slot[start] = pv_by_slot.get(start, 0.0) + energy
            if day_3_p10 is not None:
                day_3_p10_effective = (
                    min(day_3_p10, forecast_day_3_raw)
                    * forecast_factor_used
                )
                pv_day_3_p10 = _detailed_pv_map(
                    day_3_state,
                    day_3_date,
                    day_3_p10_effective,
                    timezone,
                    now_slot,
                    percentile="p10",
                )
                if not pv_day_3_p10:
                    pv_day_3_p10 = _fallback_pv_map(
                        day_3_date,
                        day_3_p10_effective,
                        timezone,
                        now_slot,
                        sunrise_day_3_local.hour * 60 + sunrise_day_3_local.minute,
                        sunset_day_3_local.hour * 60 + sunset_day_3_local.minute,
                    )
                for start, energy in pv_day_3_p10.items():
                    pv_p10_by_slot[start] = (
                        pv_p10_by_slot.get(start, 0.0) + energy
                    )
                pv_p10_available_dates.add(day_3_date)

        load_by_slot: dict[datetime, float] | None = None
        if average_load_profile or weekday_load_profile or weekend_load_profile:
            load_by_slot = {}
            horizon_end = selected_horizon_end
            cursor_utc = dt_util.as_utc(now_slot)
            horizon_end_utc = dt_util.as_utc(horizon_end)
            while cursor_utc < horizon_end_utc:
                cursor = cursor_utc.astimezone(timezone)
                category_profile = (
                    weekend_load_profile
                    if cursor.weekday() >= 5
                    else weekday_load_profile
                )
                # Keep the selected tariff profile intact.  Reusing the name
                # ``profile`` here replaced TariffProfile with a 48-slot load
                # tuple and broke every automatic (non-Manual) operator after
                # recorder history became available.
                slot_profile = category_profile or average_load_profile
                if slot_profile:
                    profile_total = sum(slot_profile)
                    scale = daily_load / profile_total if profile_total > 0 else 1.0
                    slot = cursor.hour * 2 + cursor.minute // 30
                    load_by_slot[cursor] = slot_profile[slot] * scale
                cursor_utc += timedelta(minutes=30)

        # Only the unfinished current interval is corrected from the fresh
        # power samples captured together with the control-input contract.
        # Future slots retain the robust recorder profile.
        live_pv_surplus_now = (
            current_load_power_kw is not None
            and current_pv_power_kw is not None
            and current_battery_power_kw is not None
            and current_pv_power_kw
            > current_load_power_kw + LIVE_PV_SURPLUS_MIN_KW
            # Overview convention is positive discharge, negative charge.
            and current_battery_power_kw < -LIVE_PV_SURPLUS_MIN_KW
        )
        if live_pv_surplus_now:
            if self._live_pv_surplus_started_at is None:
                self._live_pv_surplus_started_at = now
        else:
            self._live_pv_surplus_started_at = None
        live_pv_surplus_stable_seconds = (
            max(
                (
                    now - self._live_pv_surplus_started_at
                ).total_seconds(),
                0.0,
            )
            if self._live_pv_surplus_started_at is not None
            else 0.0
        )
        live_pv_surplus_stable = (
            live_pv_surplus_stable_seconds
            >= LIVE_PV_SURPLUS_STABLE_SECONDS
        )

        assert inverter_count_value is not None
        inverter_count = min(max(round(inverter_count_value), 1), 10)
        system_power_kw = rated_power * inverter_count
        requested_percent = required[
            "input_number.hoymiles_tariff_requested_charge_power"
        ]
        assert requested_percent is not None
        requested_power_kw = system_power_kw * requested_percent / 100.0
        # Maximum Charge Power is the complete AC budget used by Grid Charge.
        # The inverter supplies LOAD first and directs only the remainder to
        # the battery.  The BMS value therefore limits the battery branch, not
        # the complete grid input; combining them here would subtract LOAD
        # twice and systematically undercharge the storage.
        effective_power_kw = requested_power_kw * min(
            max(self._effective_charge_power_factor, 0.50),
            1.0,
        )

        cheap_windows = (
            (
                window_values["input_datetime.hoymiles_tariff_cheap_1_start"],
                window_values["input_datetime.hoymiles_tariff_cheap_1_end"],
            ),
            (
                window_values["input_datetime.hoymiles_tariff_cheap_2_start"],
                window_values["input_datetime.hoymiles_tariff_cheap_2_end"],
            ),
        )
        medium_windows = (
            (
                window_values["input_datetime.hoymiles_tariff_medium_start"],
                window_values["input_datetime.hoymiles_tariff_medium_end"],
            ),
        )
        assert all(
            value is not None
            for pair in (*cheap_windows, *medium_windows)
            for value in pair
        )
        selected_schedule = TariffSchedule(
            tariff_type=tariff_type,
            g11_price_pln_kwh=(
                profile.g11_price_pln_kwh
                if profile is not None
                else required["input_number.hoymiles_tariff_g11_price"]
            ),
            low_price_pln_kwh=(
                profile.low_price_pln_kwh
                if profile is not None
                else required["input_number.hoymiles_tariff_low_price"]
            ),
            medium_price_pln_kwh=(
                profile.medium_price_pln_kwh
                if profile is not None
                else required["input_number.hoymiles_tariff_medium_price"]
            ),
            peak_price_pln_kwh=(
                profile.peak_price_pln_kwh
                if profile is not None
                else required["input_number.hoymiles_tariff_peak_price"]
            ),
            cheap_windows=cheap_windows,  # type: ignore[arg-type]
            medium_windows=medium_windows,  # type: ignore[arg-type]
            weekend_low_price=(
                tariff_type in {"G12w", "G13"}
                and self.hass.states.is_state(
                    "input_boolean.hoymiles_tariff_weekend_low_price",
                    "on",
                )
            ),
            polish_holidays_low_price=self.hass.states.is_state(
                "input_boolean.hoymiles_tariff_polish_holidays_low_price",
                "on",
            ),
            operator=operator,
        )

        self_use_reserve_soc = required[
            "sensor.hoymiles_hit_ems_self_use_soc_readback"
        ]
        safety_margin_soc = required[
            "input_number.hoymiles_tariff_soc_safety_margin"
        ]
        assert self_use_reserve_soc is not None
        assert safety_margin_soc is not None
        reserve_soc = min(
            max(self_use_reserve_soc + safety_margin_soc, 0.0),
            100.0,
        )
        capacity_kwh = required["sensor.hoymiles_hit_battery_capacity"]
        assert capacity_kwh is not None
        # With at least five complete days the P90 scenario is already applied
        # slot-by-slot to every expensive window. Do not charge the same LOAD
        # uncertainty again as a terminal lump. Sparse history keeps the old
        # small fallback until an upper scenario is trustworthy.
        load_risk_kwh = (
            0.0
            if conservative_daily_load is not None
            and conservative_load_days >= 5
            else daily_load
            * min(max(load_uncertainty_ratio * 0.25, 0.02), 0.08)
        )
        forecast_risk_kwh = (
            (forecast_tomorrow + forecast_day_3)
            * min(max(forecast_uncertainty_used, 0.0), 0.50)
            * 0.05
        )
        uncertainty_margin_kwh = min(
            load_risk_kwh + forecast_risk_kwh,
            capacity_kwh * 0.08,
        )
        maximum_soc = required["input_number.hoymiles_tariff_maximum_soc"]
        assert maximum_soc is not None
        terminal_headroom_kwh = capacity_kwh * max(
            maximum_soc - reserve_soc,
            0.0,
        ) / 100.0
        (
            fallback_gap_load_kwh,
            fallback_gap_protected_hours,
        ) = (
            horizon_gap_expensive_load_reserve_kwh(
                daily_load,
                selected_horizon_end,
                horizon_gap_hours,
                selected_schedule,
                charge_power_kw=effective_power_kw,
                battery_charge_power_kw=bms_power_kw,
                charge_efficiency_percent=required[
                    "input_number.hoymiles_tariff_charge_efficiency"
                ],
                discharge_efficiency_percent=required[
                    "input_number.hoymiles_tariff_discharge_efficiency"
                ],
                maximum_stored_energy_kwh=terminal_headroom_kwh,
            )
            if day_3_status != "fresh"
            else (0.0, 0.0)
        )
        uncertainty_margin_applied_kwh = min(
            uncertainty_margin_kwh,
            terminal_headroom_kwh,
        )
        fallback_gap_load_applied_kwh = min(
            fallback_gap_load_kwh,
            max(terminal_headroom_kwh - uncertainty_margin_applied_kwh, 0.0),
        )
        terminal_margin_kwh = (
            uncertainty_margin_applied_kwh + fallback_gap_load_applied_kwh
        )
        terminal_reserve_soc = min(
            reserve_soc + terminal_margin_kwh / max(capacity_kwh, 0.001) * 100.0,
            maximum_soc,
        )
        night_start = (
            sunset_today_local.hour * 60 + sunset_today_local.minute - 90
        ) % (24 * 60)
        night_end = (
            sunrise_tomorrow_local.hour * 60 + sunrise_tomorrow_local.minute + 90
        ) % (24 * 60)
        metadata.update(
            {
                "forecast_remaining_today_kwh": round(remaining_today, 2),
                "forecast_tomorrow_kwh": round(forecast_tomorrow, 2),
                "forecast_day_3_kwh": (
                    round(forecast_day_3, 2)
                    if day_3_state is not None
                    else None
                ),
                "model_input_forecast_remaining_today_kwh": round(
                    remaining_today,
                    2,
                ),
                "model_input_forecast_tomorrow_kwh": round(
                    forecast_tomorrow,
                    2,
                ),
                "model_input_forecast_day_3_kwh": (
                    round(forecast_day_3, 2)
                    if day_3_state is not None
                    else 0.0
                ),
                "model_input_forecast_day_3_included": day_3_state is not None,
                "model_input_average_daily_load_kwh": round(daily_load, 2),
                "model_input_average_night_load_kwh": (
                    round(night_load, 2) if night_load is not None else None
                ),
                "model_input_load_profile_mode": metadata["load_profile_mode"],
                "current_live_load_power_kw": (
                    round(current_load_power_kw, 3)
                    if current_load_power_kw is not None
                    else None
                ),
                "current_live_pv_power_kw": (
                    round(current_pv_power_kw, 3)
                    if current_pv_power_kw is not None
                    else None
                ),
                "current_live_battery_power_kw": (
                    round(current_battery_power_kw, 3)
                    if current_battery_power_kw is not None
                    else None
                ),
                "current_live_power_fresh": all(
                    value is not None
                    for value in (
                        current_load_power_kw,
                        current_pv_power_kw,
                        current_battery_power_kw,
                    )
                ),
                "live_pv_surplus_now": live_pv_surplus_now,
                "live_pv_surplus_stable": live_pv_surplus_stable,
                "live_pv_surplus_stable_seconds": round(
                    live_pv_surplus_stable_seconds,
                    1,
                ),
                "live_pv_surplus_required_stable_seconds": (
                    LIVE_PV_SURPLUS_STABLE_SECONDS
                ),
                "current_live_load_power_age_seconds": (
                    round(current_load_power_age_seconds, 1)
                    if current_load_power_age_seconds is not None
                    else None
                ),
                "current_live_pv_power_age_seconds": (
                    round(current_pv_power_age_seconds, 1)
                    if current_pv_power_age_seconds is not None
                    else None
                ),
                "current_live_battery_power_age_seconds": (
                    round(current_battery_power_age_seconds, 1)
                    if current_battery_power_age_seconds is not None
                    else None
                ),
                "current_live_load_power_source": current_load_power_source,
                "current_live_pv_power_source": current_pv_power_source,
                "current_live_battery_power_source": (
                    current_battery_power_source
                ),
                "model_input_fallback_zero_pv_hours": round(
                    horizon_gap_hours,
                    2,
                ),
                "forecast_remaining_today_raw_kwh": round(
                    remaining_today_raw,
                    2,
                ),
                "forecast_tomorrow_raw_kwh": round(
                    forecast_tomorrow_raw,
                    2,
                ),
                "forecast_day_3_raw_kwh": (
                    round(forecast_day_3_raw, 2)
                    if day_3_state is not None
                    else None
                ),
                "forecast_uncertainty_risk_weight": round(risk_weight, 3),
                "forecast_today_p10_kwh": (
                    round(today_p10, 2) if today_p10 is not None else None
                ),
                "forecast_today_p90_kwh": (
                    round(today_p90, 2) if today_p90 is not None else None
                ),
                "forecast_tomorrow_p10_kwh": (
                    round(tomorrow_p10, 2)
                    if tomorrow_p10 is not None
                    else None
                ),
                "forecast_tomorrow_p90_kwh": (
                    round(tomorrow_p90, 2)
                    if tomorrow_p90 is not None
                    else None
                ),
                "forecast_day_3_p10_kwh": (
                    round(day_3_p10, 2) if day_3_p10 is not None else None
                ),
                "forecast_day_3_p90_kwh": (
                    round(day_3_p90, 2) if day_3_p90 is not None else None
                ),
                "forecast_today_effective_factor": round(
                    today_forecast_factor,
                    3,
                ),
                "forecast_live_expected_elapsed_kwh": (
                    round(expected_elapsed_raw, 2)
                    if expected_elapsed_raw is not None
                    else None
                ),
                "forecast_live_actual_elapsed_kwh": round(actual_pv_today, 2),
                "forecast_live_ratio": (
                    round(live_forecast_ratio, 3)
                    if live_forecast_ratio is not None
                    else None
                ),
                "forecast_live_confidence": round(
                    live_forecast_confidence,
                    3,
                ),
                "forecast_live_adjustment_active": (
                    learning_policy.enabled
                    and today_forecast_factor < forecast_factor_used - 0.001
                ),
                "battery_capacity_kwh": round(
                    required["sensor.hoymiles_hit_battery_capacity"],
                    2,
                ),
                "battery_soc_percent": round(
                    required["sensor.hoymiles_hit_overview_battery_soc"],
                    1,
                ),
                "reserve_soc_percent": round(reserve_soc, 1),
                "self_use_reserve_soc_percent": round(
                    self_use_reserve_soc,
                    1,
                ),
                "safety_margin_soc_percentage_points": round(
                    safety_margin_soc,
                    1,
                ),
                "inverter_count": inverter_count,
                "inverter_power_each_kw": rated_power,
                "system_power_kw": round(system_power_kw, 2),
                "requested_charge_power_kw": round(requested_power_kw, 2),
                "measured_charge_power_factor": round(
                    self._effective_charge_power_factor,
                    3,
                ),
                "terminal_uncertainty_margin_kwh": round(
                    terminal_margin_kwh,
                    2,
                ),
                "terminal_statistical_margin_kwh": round(
                    uncertainty_margin_applied_kwh,
                    2,
                ),
                "terminal_load_uncertainty_margin_kwh": round(
                    load_risk_kwh,
                    2,
                ),
                "fallback_zero_pv_load_reserve_kwh": round(
                    fallback_gap_load_applied_kwh,
                    2,
                ),
                "fallback_zero_pv_load_reserve_requested_kwh": round(
                    fallback_gap_load_kwh,
                    2,
                ),
                "fallback_zero_pv_protected_expensive_hours": round(
                    fallback_gap_protected_hours,
                    2,
                ),
                "model_input_fallback_zero_pv_load_reserve_kwh": round(
                    fallback_gap_load_applied_kwh,
                    2,
                ),
                "fallback_reserve_capped_by_maximum_soc": (
                    fallback_gap_load_applied_kwh
                    + 0.01
                    < fallback_gap_load_kwh
                ),
                "terminal_reserve_soc_percent": round(
                    terminal_reserve_soc,
                    1,
                ),
                "bms_charge_power_limit_kw": (
                    round(bms_power_kw, 2) if bms_power_kw is not None else None
                ),
                "bms_discharge_power_limit_kw": (
                    round(bms_discharge_power_kw, 2)
                    if bms_discharge_power_kw is not None
                    else None
                ),
                "bms_limit_active": (
                    bms_charge_data_fresh
                    and bms_power_kw + 0.05 < requested_power_kw
                ),
                "effective_charge_power_percent": round(
                    requested_percent,
                    1,
                ),
                # The register command stays at the requested percentage.
                # The learned factor predicts what that command physically
                # delivers; applying it to the command again would square the
                # derating. Keep the legacy alias above for older dashboards.
                "command_charge_power_percent": round(
                    requested_percent,
                    1,
                ),
                "modeled_effective_charge_power_percent": round(
                    requested_percent
                    * min(max(self._effective_charge_power_factor, 0.50), 1.0),
                    1,
                ),
                "night_window": (
                    f"{night_start // 60:02d}:{night_start % 60:02d}–"
                    f"{night_end // 60:02d}:{night_end % 60:02d}"
                ),
            }
        )
        return (
            TariffOptimizerInput(
                now=now,
                pv_by_slot_kwh=pv_by_slot,
                battery_capacity_kwh=required[
                    "sensor.hoymiles_hit_battery_capacity"
                ],
                battery_soc_percent=required[
                    "sensor.hoymiles_hit_overview_battery_soc"
                ],
                reserve_soc_percent=reserve_soc,
                maximum_soc_percent=required[
                    "input_number.hoymiles_tariff_maximum_soc"
                ],
                average_daily_load_kwh=daily_load,
                average_night_load_kwh=night_load,
                night_start_minute=night_start,
                night_end_minute=night_end,
                charge_power_kw=effective_power_kw,
                requested_charge_power_kw=requested_power_kw,
                battery_charge_power_kw=bms_power_kw,
                battery_discharge_power_kw=bms_discharge_power_kw,
                charge_efficiency_percent=required[
                    "input_number.hoymiles_tariff_charge_efficiency"
                ],
                discharge_efficiency_percent=required[
                    "input_number.hoymiles_tariff_discharge_efficiency"
                ],
                minimum_saving_pln_kwh=required[
                    "input_number.hoymiles_tariff_minimum_saving"
                ],
                schedule=selected_schedule,
                load_by_slot_kwh=load_by_slot,
                pv_charge_power_kw=bms_power_kw,
                horizon_days=horizon_days,
                terminal_reserve_soc_percent=terminal_reserve_soc,
                base_reserve_soc_percent=self_use_reserve_soc,
                conservative_daily_load_kwh=conservative_daily_load,
                load_uncertainty_ratio=load_uncertainty_ratio,
                load_history_days=robust_load_days,
                pv_p10_by_slot_kwh=pv_p10_by_slot,
                pv_p10_available_dates=tuple(sorted(pv_p10_available_dates)),
                forecast_uncertainty_ratio=forecast_uncertainty_used,
                live_pv_surplus_stable=live_pv_surplus_stable,
                live_pv_surplus_stable_seconds=(
                    live_pv_surplus_stable_seconds
                ),
                current_load_power_kw=current_load_power_kw,
                current_pv_power_kw=current_pv_power_kw,
                current_battery_power_kw=current_battery_power_kw,
                control_inputs_fresh=control_inputs_fresh,
                control_input_block_reason=control_input_block_reason,
            ),
            metadata,
        )
