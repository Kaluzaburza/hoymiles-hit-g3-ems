"""Pure RCE energy-planning helpers.

The optimizer is intentionally independent from Home Assistant so the energy
model can be tested without importing Home Assistant.  It maximizes expected
RCE revenue while preserving enough battery energy for the house, including
the protected night after the second market day.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field, fields, replace
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
import hashlib
import json
import math
import re
from time import perf_counter
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

try:  # Package import in Home Assistant; direct import in deterministic tests.
    from .automation_plan_timeline import (
        OptimizerTimelineTrace,
        RCEPolicyPoint,
        TimelineTracePoint,
    )
    from .load_model import (
        current_day_profile_correction,
        expected_load_by_slot,
        robust_weighted_upper_estimate,
    )
    from .rce_self_consumption_shadow import (
        ShadowSlot,
        ShadowVariant,
        evaluate_sale_vs_preserve,
        unavailable_shadow_evaluation,
    )
except ImportError:  # pragma: no cover - exercised by tools/test_rce_optimizer.py
    from automation_plan_timeline import (
        OptimizerTimelineTrace,
        RCEPolicyPoint,
        TimelineTracePoint,
    )
    from load_model import (
        current_day_profile_correction,
        expected_load_by_slot,
        robust_weighted_upper_estimate,
    )
    from rce_self_consumption_shadow import (
        ShadowSlot,
        ShadowVariant,
        evaluate_sale_vs_preserve,
        unavailable_shadow_evaluation,
    )


SLOT = timedelta(minutes=30)
EXACT_TIE_ROUNDOFF_PLN = 1e-12
SELF_CONSUMPTION_FILTER_CONTRACT = "rce_self_consumption_filter_v1"
_PERIOD_START = re.compile(r"(?P<hour>\d{1,2}):(?P<minute>\d{2})")
_PERIOD_RANGE = re.compile(
    r"\A\s*(?P<start_hour>\d{1,2}):(?P<start_minute>\d{2})\s*-\s*"
    r"(?P<end_hour>\d{1,2}):(?P<end_minute>\d{2})\s*\Z"
)


@dataclass(frozen=True, slots=True)
class PriceSlot:
    """One half-hour market slot."""

    start: datetime
    price_pln_kwh: float
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class PlannedExport:
    """Scheduled AC export for one market slot."""

    start: datetime
    price_pln_kwh: float
    energy_kwh: float

    @property
    def revenue_pln(self) -> float:
        """Return expected revenue for the slot."""
        return self.price_pln_kwh * self.energy_kwh


@dataclass(slots=True)
class OptimizerInput:
    """Inputs required by the RCE optimizer."""

    now: datetime
    price_slots: list[PriceSlot]
    pv_by_slot_kwh: Mapping[datetime, float]
    battery_capacity_kwh: float
    battery_soc_percent: float
    outage_reserve_soc_percent: float
    safety_margin_soc_percent: float
    manual_minimum_soc_percent: float
    dynamic_reserve_enabled: bool
    average_daily_load_kwh: float
    average_night_load_kwh: float | None
    night_start_minute: int
    night_end_minute: int
    inverter_power_kw: float
    inverter_count: int
    discharge_power_percent: float
    export_efficiency_percent: float
    # Optional nameplate AC bridge power.  ``inverter_power_kw`` remains the
    # battery-only RCE base so discharge percentages and verification targets
    # are not conflated with PV/LOAD/charging conversion headroom.
    inverter_ac_power_kw: float | None = None
    bms_max_discharge_current_a: float | None = None
    bms_max_charge_current_a: float | None = None
    battery_voltage_v: float | None = None
    bms_power_safety_percent: float = 95.0
    bms_discharge_data_fresh: bool = False
    bms_discharge_data_age_seconds: float | None = None
    bms_discharge_data_available: bool = False
    bms_charge_data_fresh: bool = False
    bms_charge_data_age_seconds: float | None = None
    bms_charge_data_available: bool = False
    actual_day_load_today_kwh: float | None = None
    actual_day_load_observed_at: datetime | None = None
    persistence_delta_kw: float = 0.0
    persistence_observed_at: datetime | None = None
    pv_to_load_power_kw: float = 0.0
    # Optional recorder profiles contain one kWh value for every half-hour.
    # They keep household peaks instead of spreading day/night energy flat.
    load_profile_30m_kwh: tuple[float, ...] = ()
    weekday_load_profile_30m_kwh: tuple[float, ...] = ()
    weekend_load_profile_30m_kwh: tuple[float, ...] = ()
    # P50 drives expected revenue; this risk-adjusted P10/P50 blend is used
    # for physical PV feasibility checks.
    conservative_pv_by_slot_kwh: Mapping[datetime, float] | None = None
    forecast_confidence_percent: float = 0.0
    # Additional physical caps.  GCF is an installation/DSO limit, while the
    # effective value may be learned from delivered power or another sensor.
    export_power_cap_kw: float | None = None
    effective_export_power_kw: float | None = None
    # Economic defaults require no new helper.  Day-3 retained-energy value is
    # diagnostic only; battery wear applies to DC export throughput.
    avoided_import_price_pln_kwh: float = 1.0
    battery_wear_cost_pln_kwh: float = 0.08
    day3_pv_forecast_kwh: float | None = None
    charge_efficiency_percent: float = 95.0
    house_discharge_efficiency_percent: float = 95.0
    # A recorder-derived upper LOAD scenario is exposed for diagnostics.  The
    # expected profile remains the single physical/economic LOAD model, which
    # prevents a tariff-style P90 buffer from suppressing RCE export.
    conservative_daily_load_kwh: float | None = None
    conservative_night_load_kwh: float | None = None
    load_history_days: int = 0
    # Fresh live powers describe only the unfinished current half-hour.  They
    # are optional so old callers retain their profile/forecast behaviour.
    current_load_power_kw: float | None = None
    current_pv_power_kw: float | None = None
    current_battery_soc_fresh: bool = True
    # Fail-closed PV scenario for the critical interval through the end of the
    # upcoming protected night when P10 is missing, stale or high-risk.
    critical_zero_pv_guard: bool = False
    critical_zero_pv_guard_reason: str = "not_required"
    # R07 policy-neutral publication.  It is used only by the R08 shadow
    # comparison; the legacy sale optimizer remains unchanged.
    tariff_price_schedule: Any | None = None
    # Internal version gate, enabled by the 1.5.8 HA adapter.  This is not a
    # user-facing policy toggle; direct optimizer callers can still exercise
    # the legacy sale-profit contract unchanged.
    self_consumption_filter_enabled: bool = False


@dataclass(frozen=True, slots=True)
class RceActiveCommitment:
    """Same-entry Supervisor evidence of an already confirmed export."""

    transaction_id: str
    started_at: datetime
    hard_deadline: datetime
    physical_verified_at: datetime
    maximum_discharge_power_percent: float
    minimum_soc_percent: float


@dataclass(slots=True)
class OptimizerResult:
    """Calculated RCE plan and diagnostics."""

    ready: bool
    status_code: str
    minimum_soc_percent: int
    base_reserve_energy_kwh: float
    protected_night_energy_kwh: float
    additional_forecast_reserve_kwh: float
    protected_home_energy_kwh: float
    available_energy_now_kwh: float
    active_slot_commitment_applied: bool = False
    planned_exports: list[PlannedExport] = field(default_factory=list)
    natural_export_kwh: float = 0.0
    natural_revenue_pln: float = 0.0
    uncontrolled_export_kwh: float = 0.0
    uncontrolled_revenue_pln: float = 0.0
    ending_battery_kwh: float = 0.0
    system_power_kw: float = 0.0
    requested_export_power_kw: float = 0.0
    bms_discharge_power_limit_kw: float | None = None
    bms_discharge_limit_percent: float | None = None
    bms_limit_active: bool = False
    bms_discharge_data_fresh: bool = False
    bms_discharge_data_age_seconds: float | None = None
    bms_discharge_data_available: bool = False
    bms_charge_power_limit_kw: float = 0.0
    bms_charge_data_fresh: bool = False
    bms_charge_data_age_seconds: float | None = None
    bms_charge_data_available: bool = False
    maximum_export_power_kw: float = 0.0
    historical_day_load_kwh: float = 0.0
    live_projected_day_load_kwh: float = 0.0
    modeled_day_load_kwh: float = 0.0
    daylight_progress_percent: float = 0.0
    load_profile_mode: str = "flat_day_night_fallback"
    forecast_confidence_percent: float = 0.0
    export_power_cap_kw: float | None = None
    effective_export_power_kw: float | None = None
    physical_limit_source: str = "requested_power"
    battery_wear_cost_pln: float = 0.0
    control_reserve_energy_kwh: float = 0.0
    soc_quantization_reserve_kwh: float = 0.0
    day3_forecast_available: bool = False
    day3_forecast_kwh: float | None = None
    day3_load_requirement_kwh: float = 0.0
    day3_energy_shortfall_kwh: float = 0.0
    terminal_reserve_reason: str = "day3_forecast_missing"
    terminal_energy_target_kwh: float = 0.0
    terminal_energy_value_pln_kwh: float = 0.0
    terminal_energy_value_pln: float = 0.0
    baseline_terminal_energy_value_pln: float = 0.0
    terminal_energy_value_applied_to_objective: bool = False
    net_objective_pln: float = 0.0
    baseline_net_objective_pln: float = 0.0
    conservative_daily_load_kwh: float | None = None
    conservative_night_load_kwh: float | None = None
    load_risk_multiplier: float = 1.0
    load_risk_buffer_kwh: float = 0.0
    load_risk_mode: str = "diagnostic_only"
    critical_zero_pv_guard_active: bool = False
    critical_zero_pv_guard_reason: str = "not_required"
    critical_zero_pv_guard_until: datetime | None = None
    critical_zero_pv_guarded_kwh: float = 0.0
    current_slot_end: datetime | None = None
    current_run_end: datetime | None = None
    current_slot_remaining_minutes: float = 0.0
    current_slot_fraction: float = 0.0
    current_slot_planned_export_kwh: float = 0.0
    current_slot_execution_export_power_kw: float = 0.0
    current_slot_execution_discharge_power_kw: float = 0.0
    current_slot_execution_power_percent: float = 0.0
    current_slot_start_eligible: bool = False
    current_slot_suppression_reason: str = "no_current_plan"
    current_required_minimum_soc_percent: int = 100
    current_slot_load_kwh: float = 0.0
    current_slot_pv_kwh: float = 0.0
    current_slot_load_source: str = "profile"
    current_slot_pv_source: str = "forecast"
    current_slot_shared_discharge_limit_kwh: float = 0.0
    # Observation only; neither field grants execution authority or changes a plan.
    current_slot_load_exhausts_requested_discharge_budget: bool = field(
        default=False, compare=False
    )
    post_command_settling_market_fingerprint: str | None = field(
        default=None, compare=False
    )
    solver_method: str = "joint_horizon_bounded_active_set"
    optimality_verified: bool = False
    solver_runtime_ms: float = 0.0
    # Observation-only sidecar. It is excluded from equality/repr so every
    # pre-AP-1 result contract remains backward compatible.
    timeline_trace: OptimizerTimelineTrace | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # R08 observation-only sidecar.  R09 may consume it explicitly, but R08
    # never mutates planned_exports or any execution field.
    self_consumption_shadow: Any | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    # R09 is a fail-closed reduction of the legacy RCE sale candidate.  Keep
    # both contracts visible so the old optimizer remains directly testable.
    legacy_planned_exports: tuple[PlannedExport, ...] = field(
        default=(),
        repr=False,
        compare=False,
    )
    self_consumption_filter_contract_version: str = field(
        default=SELF_CONSUMPTION_FILTER_CONTRACT,
        compare=False,
    )
    self_consumption_filter_active: bool = field(default=False, compare=False)
    self_consumption_filter_applied: bool = field(default=False, compare=False)
    self_consumption_filter_reduced: bool = field(default=False, compare=False)
    self_consumption_filter_status_code: str = field(
        default="not_evaluated",
        compare=False,
    )
    self_consumption_filter_reason_code: str = field(
        default="not_evaluated",
        compare=False,
    )
    self_consumption_filter_ui_reason: str = field(
        default="valuation_unavailable",
        compare=False,
    )

    @property
    def planned_export_kwh(self) -> float:
        """Return scheduled export energy."""
        return sum(item.energy_kwh for item in self.planned_exports)

    @property
    def planned_revenue_pln(self) -> float:
        """Return scheduled export revenue."""
        return sum(item.revenue_pln for item in self.planned_exports)

    @property
    def legacy_planned_export_kwh(self) -> float:
        """Return the unchanged sale-profit baseline export."""

        return sum(item.energy_kwh for item in self.legacy_planned_exports)

    @property
    def legacy_planned_revenue_pln(self) -> float:
        """Return revenue of the unchanged sale-profit baseline."""

        return sum(item.revenue_pln for item in self.legacy_planned_exports)

    @property
    def total_export_kwh(self) -> float:
        """Return scheduled plus unavoidable PV export."""
        return self.planned_export_kwh + self.natural_export_kwh

    @property
    def total_revenue_pln(self) -> float:
        """Return scheduled plus unavoidable PV-export revenue."""
        return self.planned_revenue_pln + self.natural_revenue_pln

    @property
    def automatic_price_floor_pln_kwh(self) -> float | None:
        """Return the lowest price selected by the optimized plan."""
        if not self.planned_exports:
            return None
        return min(item.price_pln_kwh for item in self.planned_exports)

    @property
    def optimization_gain_pln(self) -> float:
        """Return the legacy gross-revenue gain.

        Keep this property for dashboard and package compatibility.  New
        consumers should use :attr:`gross_optimization_gain_pln` or
        :attr:`net_optimization_gain_pln`, whose names state whether battery
        wear is included.  Terminal stored-energy value is diagnostic only.
        """
        return self.gross_optimization_gain_pln

    @property
    def gross_optimization_gain_pln(self) -> float:
        """Return gross revenue gained over uncontrolled PV export."""
        return self.total_revenue_pln - self.uncontrolled_revenue_pln

    @property
    def net_optimization_gain_pln(self) -> float:
        """Return market-revenue gain after battery wear."""
        return self.net_objective_pln - self.baseline_net_objective_pln

    @property
    def terminal_energy_value_delta_pln(self) -> float:
        """Return retained-energy value added relative to no RCE control."""
        return (
            self.terminal_energy_value_pln
            - self.baseline_terminal_energy_value_pln
        )


@dataclass(frozen=True, slots=True)
class _RCESimulationSlot:
    """Exact per-slot values retained by an already-required simulation."""

    slot_start: datetime
    start: datetime
    end: datetime
    pv_kwh: float
    load_kwh: float
    battery_delta_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_after_kwh: float


@dataclass(frozen=True, slots=True)
class _RCEPhysicalSlotResult:
    """One physical slot shared by the planner and sale/preserve evaluator."""

    feasible: bool
    home_energy_shortage: bool
    battery_after_kwh_dc: float
    delivered_to_load_kwh_ac: float
    grid_import_kwh_ac: float
    controlled_export_kwh_ac: float
    natural_export_kwh_ac: float
    charge_input_kwh_ac: float
    balance_error_kwh: float


@dataclass(frozen=True, slots=True)
class _RCEPhysicalConstants:
    """Snapshot-invariant limits reused across a complete trajectory."""

    battery_capacity_kwh: float
    battery_system_power_kw: float
    inverter_ac_total_kw: float
    bms_discharge_dc_power_kw: float
    bms_charge_dc_power_kw: float
    export_efficiency: float
    charge_efficiency: float
    house_efficiency: float


def floor_half_hour(value: datetime) -> datetime:
    """Floor an aware datetime to a half-hour boundary."""
    return value.replace(
        minute=0 if value.minute < 30 else 30,
        second=0,
        microsecond=0,
    )


def blocked_minute(
    minute: int,
    start_minute: int,
    end_minute: int,
    enabled: bool,
) -> bool:
    """Return whether a minute of day is in a configured lockout."""
    if not enabled or start_minute == end_minute:
        return False
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    return minute >= start_minute or minute < end_minute


def parse_rce_rows(
    rows: Iterable[Mapping[str, Any]],
    timezone: ZoneInfo,
    *,
    block_enabled: bool,
    block_start_minute: int,
    block_end_minute: int,
) -> list[PriceSlot]:
    """Convert PSE 15-minute rows to half-hour market slots.

    ``dtime_utc`` is the authoritative interval end when supplied by the
    official PSE API.  Deriving the quarter start on the absolute UTC timeline
    preserves the real price-to-fold relationship during the repeated autumn
    hour.  Older cached payloads may contain only local wall time; those rows
    use the deterministic fold fallback below.  Payload order is never used.
    """

    def valid_instants(naive: datetime) -> list[datetime]:
        candidates: dict[datetime, datetime] = {}
        for fold in (0, 1):
            local = naive.replace(tzinfo=timezone, fold=fold)
            utc_value = local.astimezone(dt_timezone.utc)
            round_trip = utc_value.astimezone(timezone)
            if round_trip.replace(tzinfo=None) != naive:
                continue
            candidates[utc_value] = local
        return [candidates[key] for key in sorted(candidates)]

    def absolute_start_utc(row: Mapping[str, Any]) -> datetime | None:
        value = row.get("dtime_utc")
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str) and value.strip():
            text = value.strip()
            if text.endswith(("Z", "z")):
                text = f"{text[:-1]}+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                return None
        else:
            return None
        # The field is explicitly UTC.  Be tolerant of an API/cache that omits
        # the suffix while retaining the absolute-field name.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_timezone.utc)
        interval_end = parsed.astimezone(dt_timezone.utc)
        if (
            interval_end.minute % 15 != 0
            or interval_end.second != 0
            or interval_end.microsecond != 0
        ):
            return None

        if "period_utc" in row:
            period_utc = row.get("period_utc")
            if not isinstance(period_utc, str):
                return None
            match = _PERIOD_RANGE.fullmatch(period_utc)
            if match is None:
                return None

            def clock_minute(hour_name: str, minute_name: str) -> int | None:
                hour = int(match.group(hour_name))
                minute = int(match.group(minute_name))
                if hour == 24 and minute == 0:
                    return 24 * 60
                if 0 <= hour < 24 and 0 <= minute < 60:
                    return hour * 60 + minute
                return None

            period_start = clock_minute("start_hour", "start_minute")
            period_end = clock_minute("end_hour", "end_minute")
            if period_start is None or period_end is None:
                return None
            if (period_end - period_start) % (24 * 60) != 15:
                return None
            interval_end_minute = interval_end.hour * 60 + interval_end.minute
            if period_end % (24 * 60) != interval_end_minute:
                return None

        # PSE publishes the settlement interval end in ``dtime_utc``.  Work
        # backwards on the UTC timeline so DST gaps/folds cannot distort the
        # fixed 15-minute market interval.
        interval_start = interval_end - timedelta(minutes=15)
        if "business_date" in row:
            try:
                business_day = date.fromisoformat(
                    str(row.get("business_date", "")).strip()
                )
            except ValueError:
                return None
            if interval_start.astimezone(timezone).date() != business_day:
                return None
        return interval_start

    absolute_grouped: dict[
        datetime,
        list[tuple[tuple[tuple[str, str], ...], float]],
    ] = {}
    grouped: dict[datetime, list[tuple[tuple[tuple[str, str], ...], float]]] = {}
    for row in rows:
        try:
            price = float(row["rce_pln"]) / 1000.0
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(price):
            continue
        stable_key = tuple(
            sorted((str(key), repr(value)) for key, value in row.items())
        )
        if "dtime_utc" in row:
            utc_start = absolute_start_utc(row)
            if utc_start is None:
                continue
            absolute_grouped.setdefault(utc_start, []).append(
                (stable_key, price)
            )
            continue

        business_date = str(row.get("business_date", ""))
        period = str(row.get("period", ""))
        match = _PERIOD_START.search(period)
        if not business_date or match is None:
            continue
        try:
            day = date.fromisoformat(business_date)
            hour = int(match.group("hour"))
            minute = int(match.group("minute"))
            if hour == 24:
                day += timedelta(days=1)
            naive = datetime.combine(
                day,
                time(hour=hour % 24, minute=minute),
            )
        except (TypeError, ValueError):
            continue
        # Stable content ordering makes reversed/shuffled payloads equivalent.
        # Price is part of this key, so two distinct fold values keep a stable
        # assignment even when OData returns the repeated hour in reverse.
        grouped.setdefault(naive, []).append((stable_key, price))

    parsed_by_utc: dict[datetime, tuple[datetime, float]] = {}
    for utc_start in sorted(absolute_grouped):
        records = absolute_grouped[utc_start]
        # Conflicting rows for one authoritative UTC instant are ambiguous.
        # Use the lower price so a duplicated/corrupted payload can only make
        # export more conservative, never manufacture an inflated sale value.
        price = min(item[1] for item in records)
        parsed_by_utc[utc_start] = (
            utc_start.astimezone(timezone),
            price,
        )

    for naive in sorted(grouped):
        candidates = valid_instants(naive)
        if not candidates:
            # A nonexistent spring-forward wall-clock interval is not a real
            # market interval and must not enter either the plan or reserve.
            continue
        records = sorted(grouped[naive], key=lambda item: item[0])
        # Non-ambiguous accidental duplicates collapse to one deterministic
        # row.  A local-only autumn repeated time has no fold provenance: its
        # two prices cannot safely be mapped to the two real instants.  Require
        # both rows and assign their minimum price to both folds.  This may
        # understate revenue but cannot manufacture a profitable export from
        # a high price which actually belonged to the other fold.
        if len(candidates) > 1:
            if len(records) < len(candidates):
                continue
            conservative_price = min(item[1] for item in records)
            selected = [
                (start, conservative_price) for start in candidates
            ]
        else:
            selected = [(candidates[0], min(item[1] for item in records))]
        for start, price in selected:
            utc_start = start.astimezone(dt_timezone.utc)
            # Absolute PSE telemetry wins when a mixed old/new cache contains
            # both representations of the same quarter.
            parsed_by_utc.setdefault(utc_start, (start, price))

    quarters_by_half_hour: dict[datetime, dict[int, float]] = {}
    for start_utc in sorted(parsed_by_utc):
        _, price = parsed_by_utc[start_utc]
        minute_in_half_hour = start_utc.minute % 30
        if minute_in_half_hour not in (0, 15):
            continue
        half_hour_utc = start_utc.replace(
            minute=0 if start_utc.minute < 30 else 30,
            second=0,
            microsecond=0,
        )
        quarters_by_half_hour.setdefault(half_hour_utc, {})[
            minute_in_half_hour
        ] = price

    result: list[PriceSlot] = []
    for half_hour_utc in sorted(quarters_by_half_hour):
        quarters = quarters_by_half_hour[half_hour_utc]
        if 0 not in quarters or 15 not in quarters:
            continue
        local_start = half_hour_utc.astimezone(timezone)
        minute = local_start.hour * 60 + local_start.minute
        result.append(
            PriceSlot(
                start=local_start,
                price_pln_kwh=(quarters[0] + quarters[15]) / 2.0,
                blocked=blocked_minute(
                    minute,
                    block_start_minute,
                    block_end_minute,
                    block_enabled,
                ),
            )
        )
    return result


def _is_night(
    moment: datetime,
    start_minute: int,
    end_minute: int,
) -> bool:
    minute = moment.hour * 60 + moment.minute
    if start_minute == end_minute:
        return False
    if start_minute < end_minute:
        return start_minute <= minute < end_minute
    return minute >= start_minute or minute < end_minute


def _horizon_end(settings: OptimizerInput) -> datetime:
    """Extend the two-day market horizon through the following protected night."""
    if settings.price_slots:
        last_market_day = max(slot.start.date() for slot in settings.price_slots)
    else:
        last_market_day = settings.now.date() + timedelta(days=1)
    end_day = last_market_day + timedelta(days=1)
    hour, minute = divmod(settings.night_end_minute, 60)
    return datetime.combine(
        end_day,
        time(hour=hour % 24, minute=minute),
        tzinfo=settings.now.tzinfo,
    )


def _supported_price_slots(settings: OptimizerInput) -> list[PriceSlot]:
    """Return deterministic market slots inside the supported two-day scope.

    The public RCE feed is a today/tomorrow product.  Treating an accidental
    far-future timestamp as the end of the optimization horizon makes every
    battery simulation scale with that malformed row.  Normalize on absolute
    UTC instants, reject dates outside today/tomorrow, and collapse duplicate
    instants conservatively so input ordering cannot change the plan.
    """

    first_day = settings.now.date()
    last_day = first_day + timedelta(days=1)
    timezone = settings.now.tzinfo
    if timezone is None:
        return []

    by_utc: dict[datetime, PriceSlot] = {}
    for slot in settings.price_slots:
        try:
            if slot.start.tzinfo is None or slot.start.utcoffset() is None:
                continue
            price = float(slot.price_pln_kwh)
            if not math.isfinite(price):
                continue
            utc_start = slot.start.astimezone(dt_timezone.utc)
            local_start = utc_start.astimezone(timezone)
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if not first_day <= local_start.date() <= last_day:
            continue
        if (
            local_start.minute not in (0, 30)
            or local_start.second != 0
            or local_start.microsecond != 0
        ):
            continue
        candidate = PriceSlot(
            start=local_start,
            price_pln_kwh=price,
            blocked=bool(slot.blocked),
        )
        previous = by_utc.get(utc_start)
        if previous is None:
            by_utc[utc_start] = candidate
        else:
            # Conflicting duplicates are ambiguous.  The lower price and the
            # stricter block flag are the fail-safe, order-independent view.
            by_utc[utc_start] = PriceSlot(
                start=local_start,
                price_pln_kwh=min(previous.price_pln_kwh, price),
                blocked=previous.blocked or bool(slot.blocked),
            )
    return [by_utc[start] for start in sorted(by_utc)]


def post_command_settling_market_fingerprint(settings: OptimizerInput) -> str | None:
    """Identify supported market/economic inputs without telemetry or commands.

    The date scope is the same local today/tomorrow scope as the optimizer.
    A nonempty normalized schedule may lack the current slot; the separate
    LOAD diagnostic requires that current slot to exist and be unblocked.
    This cheap identity check never runs the planner or grants authority.
    """
    try:
        if (
            not isinstance(settings.now, datetime)
            or settings.now.tzinfo is None
            or settings.now.utcoffset() is None
        ):
            return None
        economics = {}
        for name in (
            "battery_wear_cost_pln_kwh",
            "export_efficiency_percent",
            "house_discharge_efficiency_percent",
            "avoided_import_price_pln_kwh",
        ):
            value = getattr(settings, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return None
            value = float(value)
            if not math.isfinite(value) or value < 0.0:
                return None
            if name.endswith("efficiency_percent") and not 0.0 < value <= 100.0:
                return None
            economics[name] = value if value else 0.0
        slots = _supported_price_slots(settings)
        if not slots:
            return None
        basis = {
            "slots": [
                (
                    slot.start.astimezone(dt_timezone.utc).isoformat(),
                    slot.price_pln_kwh if slot.price_pln_kwh else 0.0,
                    slot.blocked,
                )
                for slot in slots
            ],
            "economics": economics,
        }
        encoded = json.dumps(
            basis, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _current_slot_load_exhausts_requested_discharge_budget(
    settings: OptimizerInput, result: OptimizerResult
) -> bool:
    """Diagnose LOAD consuming the requested budget, excluding other zero caps.

    LIVE power provenance and freshness are supplied by the input builder.
    Optional absent caps mean no additional limit; GCF authority/freshness still
    belongs to the execution gates, not to this observation-only flag.
    """
    if (
        not result.ready
        or result.status_code in {"missing_data", "home_energy_shortage", "optimizer_error"}
        or result.post_command_settling_market_fingerprint is None
        or result.current_slot_load_source != "live"
        or result.current_slot_pv_source != "live"
        or settings.current_battery_soc_fresh is not True
        or settings.bms_discharge_data_fresh is not True
        or settings.bms_discharge_data_available is not True
        or result.current_slot_shared_discharge_limit_kwh != 0.0
    ):
        return False
    values = (
        settings.current_load_power_kw, settings.current_pv_power_kw,
        settings.battery_soc_percent, settings.bms_max_discharge_current_a,
        settings.battery_voltage_v, settings.bms_power_safety_percent,
        settings.bms_discharge_data_age_seconds,
        result.requested_export_power_kw,
    )
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float))
        or not math.isfinite(value)
        for value in values
    ):
        return False
    if (
        not 0.0 <= settings.battery_soc_percent <= 100.0
        or settings.current_load_power_kw < 0.0
        or settings.current_pv_power_kw < 0.0
        or settings.bms_max_discharge_current_a <= 0.0
        or settings.battery_voltage_v <= 0.0
        or not 0.0 < settings.bms_power_safety_percent <= 100.0
        or settings.bms_discharge_data_age_seconds < 0.0
        or result.requested_export_power_kw <= 0.0
    ):
        return False
    for cap in (settings.export_power_cap_kw, settings.effective_export_power_kw):
        if cap is not None and (
            isinstance(cap, bool) or not isinstance(cap, (int, float))
            or not math.isfinite(cap) or cap <= 0.0
        ):
            return False
    current_start = floor_half_hour(settings.now).astimezone(dt_timezone.utc)
    if not any(
        slot.start.astimezone(dt_timezone.utc) == current_start and not slot.blocked
        for slot in settings.price_slots
    ):
        return False
    deficit = max(settings.current_load_power_kw - settings.current_pv_power_kw, 0.0)
    ac_headroom = (
        _inverter_ac_power_kw(settings) * settings.inverter_count
        - settings.current_load_power_kw
    )
    bms_headroom = _bms_total_ac_discharge_power_limit_kw(settings, deficit) - deficit
    return (
        deficit >= result.requested_export_power_kw
        and math.isfinite(ac_headroom) and ac_headroom > 0.0
        and math.isfinite(bms_headroom) and bms_headroom > 0.0
    )


def _local_slot(start: datetime, settings: OptimizerInput) -> datetime:
    """Return an internal UTC slot in the installation timezone."""
    return start.astimezone(settings.now.tzinfo)


def _utc_energy_map(values: Mapping[datetime, float]) -> dict[datetime, float]:
    """Normalize energy keys to absolute UTC instants.

    Python treats the two ``fold`` values of the same ZoneInfo wall time as
    equal dictionary keys.  UTC keys keep the repeated autumn hour distinct.
    """
    normalized: dict[datetime, float] = {}
    for start, energy in values.items():
        key = start.astimezone(dt_timezone.utc)
        normalized[key] = normalized.get(key, 0.0) + max(float(energy), 0.0)
    return normalized


def _current_slot_fraction(now: datetime) -> float:
    seconds_into_slot = (
        (now.minute % 30) * 60
        + now.second
        + now.microsecond / 1_000_000.0
    )
    return min(max((30 * 60 - seconds_into_slot) / (30 * 60), 0.0), 1.0)


def _day_load_projection(
    settings: OptimizerInput,
) -> tuple[float, float, float, float]:
    """Return historical, live and selected daytime-load estimates.

    ``PV to Load Energy Today`` is a direct inverter counter.  It is used only
    for the elapsed part of the current daylight window, so energy already
    consumed by the house is never subtracted from the future PV forecast a
    second time.  The live projection may increase the historical estimate but
    never reduce it; a cloudy morning must not weaken the home reserve.
    """

    night_minutes = (
        settings.night_end_minute - settings.night_start_minute
    ) % (24 * 60)
    night_hours = max(night_minutes / 60.0, 0.5)
    daily = max(settings.average_daily_load_kwh, 0.0)
    if settings.average_night_load_kwh is None:
        night_energy = daily * night_hours / 24.0
    else:
        night_energy = min(max(settings.average_night_load_kwh, 0.0), daily)
    historical_day_energy = max(daily - night_energy, 0.0)

    ratio, _, expected_elapsed = current_day_profile_correction(
        now=settings.now,
        observed_energy_kwh=settings.actual_day_load_today_kwh,
        observed_at=settings.actual_day_load_observed_at,
        daily_energy_kwh=daily,
        average_profile_30m_kwh=settings.load_profile_30m_kwh,
        weekday_profile_30m_kwh=settings.weekday_load_profile_30m_kwh,
        weekend_profile_30m_kwh=settings.weekend_load_profile_30m_kwh,
        night_energy_kwh=settings.average_night_load_kwh,
        night_start_minute=settings.night_start_minute,
        night_end_minute=settings.night_end_minute,
        persistence_delta_kw=settings.persistence_delta_kw,
        persistence_observed_at=settings.persistence_observed_at,
    )
    live_projection = historical_day_energy * ratio
    modeled_day_energy = live_projection
    progress = min(max(expected_elapsed / max(daily, 1e-6), 0.0), 1.0)
    return (
        historical_day_energy,
        live_projection,
        modeled_day_energy,
        progress,
    )


def _load_by_slot(
    settings: OptimizerInput,
    starts: list[datetime],
    *,
    historical_day_energy: float,
    live_projected_day_energy: float,
    modeled_day_energy: float,
    daylight_progress: float,
) -> tuple[dict[datetime, float], str]:
    forecast = expected_load_by_slot(
        starts,
        now=settings.now,
        daily_energy_kwh=max(settings.average_daily_load_kwh, 0.0),
        average_profile_30m_kwh=settings.load_profile_30m_kwh,
        weekday_profile_30m_kwh=settings.weekday_load_profile_30m_kwh,
        weekend_profile_30m_kwh=settings.weekend_load_profile_30m_kwh,
        night_energy_kwh=settings.average_night_load_kwh,
        night_start_minute=settings.night_start_minute,
        night_end_minute=settings.night_end_minute,
        current_day_energy_kwh=settings.actual_day_load_today_kwh,
        current_day_observed_at=settings.actual_day_load_observed_at,
        persistence_delta_kw=settings.persistence_delta_kw,
        persistence_observed_at=settings.persistence_observed_at,
    )
    return dict(forecast.by_slot_kwh), forecast.profile_mode



def _quantize_reserve_to_soc_percent(
    energy_kwh: float,
    capacity_kwh: float,
) -> float:
    """Round a protected DC-energy reserve up to a whole inverter SOC step.

    Hoymiles Force Discharge SOC accepts complete percentage points.  A
    continuous optimizer that retained 25.39% while commanding 26% would
    overstate exportable energy by 0.61% of the battery.  Quantizing every
    per-slot control reserve upward models the register that will actually be
    written and can only make the plan more conservative.
    """
    if capacity_kwh <= 0:
        return max(energy_kwh, 0.0)
    percent = math.ceil(
        min(max(energy_kwh / capacity_kwh * 100.0, 0.0), 100.0) - 1e-9
    )
    return capacity_kwh * percent / 100.0


def _conservative_load_by_slot(
    starts: list[datetime],
    settings: OptimizerInput,
    expected: Mapping[datetime, float],
    *,
    modeled_day_energy: float,
    current_slot_is_live: bool,
) -> tuple[dict[datetime, float], float, float]:
    """Return an alternative P90 LOAD scenario, never an additive reserve."""
    conservative = {
        start: max(float(expected.get(start, 0.0)), 0.0) for start in starts
    }
    if (
        settings.load_history_days < 5
        or settings.conservative_daily_load_kwh is None
        or settings.average_daily_load_kwh <= 1e-9
    ):
        return conservative, 1.0, 0.0

    expected_daily = max(settings.average_daily_load_kwh, 1e-9)
    daily_upper = max(settings.conservative_daily_load_kwh, expected_daily)
    daily_factor = min(max(daily_upper / expected_daily, 1.0), 1.35)
    day_factor = daily_factor
    night_factor = daily_factor
    if (
        settings.conservative_night_load_kwh is not None
        and settings.average_night_load_kwh is not None
    ):
        expected_night = max(settings.average_night_load_kwh, 0.0)
        upper_night = max(settings.conservative_night_load_kwh, expected_night)
        if expected_night > 1e-9:
            night_factor = min(max(upper_night / expected_night, 1.0), 1.35)
        expected_day = max(modeled_day_energy, 0.0)
        upper_day = max(daily_upper - upper_night, expected_day)
        if expected_day > 1e-9:
            day_factor = min(max(upper_day / expected_day, 1.0), 1.35)

    for index, start in enumerate(starts):
        # The unfinished live interval is already measured; multiplying it by
        # a historical upper percentile would count the same cold spike twice.
        if index == 0 and current_slot_is_live:
            continue
        local_start = _local_slot(start, settings)
        factor = (
            night_factor
            if _is_night(
                local_start,
                settings.night_start_minute,
                settings.night_end_minute,
            )
            else day_factor
        )
        conservative[start] *= factor
    buffer = sum(conservative.values()) - sum(
        max(float(expected.get(start, 0.0)), 0.0) for start in starts
    )
    return conservative, max(day_factor, night_factor), max(buffer, 0.0)


def _critical_zero_pv_scenario(
    starts: list[datetime],
    settings: OptimizerInput,
    conservative_pv: Mapping[datetime, float],
    *,
    preserve_live_current: bool,
) -> tuple[dict[datetime, float], datetime | None, float]:
    """Zero uncertain PV through the end of the upcoming protected night."""
    guarded = {
        start: max(float(conservative_pv.get(start, 0.0)), 0.0)
        for start in starts
    }
    if not settings.critical_zero_pv_guard or not starts:
        return guarded, None, 0.0

    entered_night = False
    guard_until: datetime | None = None
    for start in starts:
        is_night = _is_night(
            _local_slot(start, settings),
            settings.night_start_minute,
            settings.night_end_minute,
        )
        if not entered_night and is_night:
            entered_night = True
        elif entered_night and not is_night:
            guard_until = start
            break
    if guard_until is None:
        guard_until = starts[-1] + SLOT

    removed = 0.0
    for index, start in enumerate(starts):
        if start >= guard_until or (index == 0 and preserve_live_current):
            continue
        removed += guarded[start]
        guarded[start] = 0.0
    return guarded, guard_until, removed


def _bms_dc_power_limit_kw(settings: OptimizerInput) -> float | None:
    # RCE may display a diagnostic plan while telemetry is unavailable, but
    # every physical calculation must fail closed to 0 kW.  In particular, a
    # fresh 0 A register is a contractual stop, never an unlimited fallback.
    if (
        not settings.bms_discharge_data_fresh
        or not settings.bms_discharge_data_available
    ):
        return 0.0
    if (
        settings.bms_max_discharge_current_a is None
        or settings.battery_voltage_v is None
        or settings.battery_voltage_v <= 0
    ):
        return 0.0
    return (
        max(settings.bms_max_discharge_current_a, 0.0)
        * settings.battery_voltage_v
        / 1000.0
        * min(max(settings.bms_power_safety_percent, 0.0), 100.0)
        / 100.0
    )


def _bms_charge_dc_power_limit_kw(settings: OptimizerInput) -> float:
    """Return fresh BMS charge power, preserving a contractual zero limit."""
    if (
        not settings.bms_charge_data_fresh
        or not settings.bms_charge_data_available
        or settings.bms_max_charge_current_a is None
        or settings.battery_voltage_v is None
        or settings.battery_voltage_v <= 0.0
    ):
        return 0.0
    return (
        max(settings.bms_max_charge_current_a, 0.0)
        * settings.battery_voltage_v
        / 1000.0
        * min(max(settings.bms_power_safety_percent, 0.0), 100.0)
        / 100.0
    )


def _bms_start_suppression_reason(settings: OptimizerInput) -> str | None:
    """Return the fail-closed scheduler reason for BMS discharge telemetry."""
    if not settings.bms_discharge_data_fresh:
        return (
            "bms_discharge_data_unavailable"
            if settings.bms_discharge_data_age_seconds is None
            else "bms_discharge_data_stale"
        )
    if not settings.bms_discharge_data_available:
        return "bms_discharge_limit_zero"
    return None


def _inverter_ac_power_kw(settings: OptimizerInput) -> float:
    """Return nameplate AC bridge power, falling back for old callers."""

    if settings.inverter_ac_power_kw is None:
        return settings.inverter_power_kw
    return settings.inverter_ac_power_kw


def _physical_constants(settings: OptimizerInput) -> _RCEPhysicalConstants:
    """Calculate immutable physical inputs once per compared trajectory."""

    return _RCEPhysicalConstants(
        battery_capacity_kwh=settings.battery_capacity_kwh,
        battery_system_power_kw=(
            settings.inverter_power_kw * settings.inverter_count
        ),
        inverter_ac_total_kw=(
            _inverter_ac_power_kw(settings) * settings.inverter_count
        ),
        bms_discharge_dc_power_kw=max(
            _bms_dc_power_limit_kw(settings) or 0.0,
            0.0,
        ),
        bms_charge_dc_power_kw=max(
            _bms_charge_dc_power_limit_kw(settings),
            0.0,
        ),
        export_efficiency=max(
            min(settings.export_efficiency_percent / 100.0, 1.0),
            0.01,
        ),
        charge_efficiency=max(
            min(settings.charge_efficiency_percent / 100.0, 1.0),
            0.01,
        ),
        house_efficiency=max(
            min(settings.house_discharge_efficiency_percent / 100.0, 1.0),
            0.01,
        ),
    )


def _bms_total_ac_discharge_power_limit_kw(
    settings: OptimizerInput,
    load_deficit_power_kw: float,
) -> float:
    """Return the total AC command allowed by one shared BMS DC budget.

    Battery-fed household load and grid export can have different conversion
    efficiencies.  The house consumes its DC share first; only the remaining
    DC power is converted through the export path.  Missing, stale, or exact
    zero BMS capability remains a contractual 0 kW command.
    """

    bms_dc_power = _bms_dc_power_limit_kw(settings)
    if bms_dc_power is None or bms_dc_power <= 0.0:
        return 0.0
    load_deficit = max(load_deficit_power_kw, 0.0)
    house_efficiency = max(
        min(settings.house_discharge_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    export_efficiency = max(
        min(settings.export_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    house_ac_power = min(
        load_deficit,
        bms_dc_power * house_efficiency,
    )
    remaining_dc_power = max(
        bms_dc_power - house_ac_power / house_efficiency,
        0.0,
    )
    return house_ac_power + remaining_dc_power * export_efficiency


def _slot_export_limit_kwh(
    settings: OptimizerInput,
    load_kwh: float,
    pv_kwh: float,
    slot_fraction: float,
    *,
    physical_constants: _RCEPhysicalConstants | None = None,
) -> float:
    """Return grid-export energy after sharing discharge power with LOAD."""
    constants = physical_constants or _physical_constants(settings)
    fraction = min(max(slot_fraction, 0.0), 1.0)
    hours = 0.5 * fraction
    if hours <= 0.0:
        return 0.0
    battery_system_power = constants.battery_system_power_kw
    if battery_system_power <= 0.0:
        return 0.0
    ac_system_power = constants.inverter_ac_total_kw
    requested_power = battery_system_power * _quantize_4306_percent(
        settings.discharge_power_percent
    ) / 100.0
    load_deficit_ac = max(load_kwh - pv_kwh, 0.0)
    # Battery discharge power is shared with battery-fed LOAD, while the whole
    # inverter AC bridge is shared by PV/LOAD and every grid-export branch.
    shared_limits = [max(requested_power * hours - load_deficit_ac, 0.0)]
    shared_limits.append(
        max(
            ac_system_power * hours
            - min(load_kwh, ac_system_power * hours),
            0.0,
        )
    )

    load_deficit_power = load_deficit_ac / hours
    house_ac_power = min(
        load_deficit_power,
        constants.bms_discharge_dc_power_kw * constants.house_efficiency,
    )
    remaining_dc_power = max(
        constants.bms_discharge_dc_power_kw
        - house_ac_power / constants.house_efficiency,
        0.0,
    )
    bms_total_ac_power = (
        house_ac_power
        + remaining_dc_power * constants.export_efficiency
    )
    shared_limits.append(
        max(bms_total_ac_power * hours - load_deficit_ac, 0.0)
    )

    # GCF and learned delivered power limit only the grid branch.  They do not
    # reduce the separate AC power needed to keep the house supplied.
    if settings.export_power_cap_kw is not None:
        shared_limits.append(max(settings.export_power_cap_kw, 0.0) * hours)
    if settings.effective_export_power_kw is not None:
        shared_limits.append(
            max(settings.effective_export_power_kw, 0.0) * hours
        )
    export_limit = max(min(shared_limits), 0.0)
    command_percent = _quantize_4306_percent(
        (export_limit + load_deficit_ac) / hours * 100.0 / battery_system_power
    )
    # Division can put an exactly feasible integer command one ULP below
    # its step. Recover only that next step by testing the original energy
    # constraints and fresh power caps directly; never widen a cap by epsilon.
    next_percent = min(command_percent + 1.0, 100.0)
    next_power = battery_system_power * next_percent / 100.0
    next_export_power = max(next_power - load_deficit_power, 0.0)
    if (
        next_power <= min(requested_power, battery_system_power, bms_total_ac_power)
        and max(next_power * hours - load_deficit_ac, 0.0) <= export_limit
        and all(next_export_power <= cap for cap in (
            settings.export_power_cap_kw, settings.effective_export_power_kw,
        ) if cap is not None)
    ):
        command_percent = next_percent
    return min(
        export_limit,
        max(battery_system_power * command_percent / 100.0 * hours - load_deficit_ac, 0.0),
    )


def _slot_charge_input_limit_kwh(
    settings: OptimizerInput,
    load_kwh: float,
    pv_kwh: float,
    slot_fraction: float,
    controlled_export_kwh: float = 0.0,
    *,
    physical_constants: _RCEPhysicalConstants | None = None,
) -> float:
    """Return PV AC energy that can physically charge the battery this slot."""
    constants = physical_constants or _physical_constants(settings)
    fraction = min(max(slot_fraction, 0.0), 1.0)
    hours = 0.5 * fraction
    if hours <= 0.0:
        return 0.0
    system_energy = constants.inverter_ac_total_kw * hours
    pv_to_load = min(pv_kwh, load_kwh, system_energy)
    remaining_pv = max(pv_kwh - pv_to_load, 0.0)
    if remaining_pv <= 0.0:
        return 0.0
    remaining_conversion = max(
        system_energy
        - pv_to_load
        - max(controlled_export_kwh, 0.0),
        0.0,
    )
    bms_ac_input = (
        constants.bms_charge_dc_power_kw
        * hours
        / constants.charge_efficiency
    )
    return max(min(remaining_pv, remaining_conversion, bms_ac_input), 0.0)


def _simulate_physical_slot(
    settings: OptimizerInput,
    *,
    battery_kwh_dc: float,
    load_kwh_ac: float,
    pv_kwh_ac: float,
    controlled_export_kwh_ac: float,
    hard_floor_kwh_dc: float,
    export_floor_kwh_dc: float,
    slot_fraction: float,
    physical_constants: _RCEPhysicalConstants | None = None,
) -> _RCEPhysicalSlotResult:
    """Run the shared AC/DC balance for one possibly partial market slot.

    ``home_energy_shortage`` is a planner policy signal, not a violation of
    physics: the remaining load is supplied by grid import.  The legacy RCE
    planner can still reject such a candidate, while the economic comparison
    must retain the import in both complete trajectories.
    """

    constants = physical_constants or _physical_constants(settings)
    capacity = constants.battery_capacity_kwh
    battery_before = min(max(float(battery_kwh_dc), 0.0), capacity)
    battery = battery_before
    load = max(float(load_kwh_ac), 0.0)
    pv = max(float(pv_kwh_ac), 0.0)
    export = max(float(controlled_export_kwh_ac), 0.0)
    fraction = min(max(float(slot_fraction), 0.0), 1.0)
    duration_hours = 0.5 * fraction
    export_efficiency = constants.export_efficiency
    charge_efficiency = constants.charge_efficiency
    house_efficiency = constants.house_efficiency
    if duration_hours <= 0.0 or export > _slot_export_limit_kwh(
        settings,
        load,
        pv,
        fraction,
        physical_constants=constants,
    ) + 1e-6:
        return _RCEPhysicalSlotResult(
            False, False, battery, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        )

    system_energy = (
        constants.inverter_ac_total_kw * duration_hours
    )
    pv_to_load = min(pv, load, system_energy)
    remaining_pv = max(pv - pv_to_load, 0.0)
    remaining_load = max(load - pv_to_load, 0.0)
    remaining_bridge = max(system_energy - pv_to_load, 0.0)
    bms_dc_power = constants.bms_discharge_dc_power_kw
    floor_deliverable = (
        max(battery - hard_floor_kwh_dc, 0.0) * house_efficiency
    )
    home_energy_shortage = remaining_load > floor_deliverable + 1e-6
    delivered_to_load = min(
        remaining_load,
        remaining_bridge,
        max(bms_dc_power or 0.0, 0.0)
        * duration_hours
        * house_efficiency,
        floor_deliverable,
    )
    battery -= delivered_to_load / house_efficiency
    grid_import = max(remaining_load - delivered_to_load, 0.0)

    battery -= export / export_efficiency
    if remaining_pv > 0.0:
        charge_input = _slot_charge_input_limit_kwh(
            settings,
            load,
            pv,
            fraction,
            export,
            physical_constants=constants,
        )
        charge_input = min(
            charge_input,
            max(capacity - battery, 0.0) / charge_efficiency,
        )
    else:
        charge_input = 0.0
    battery += charge_input * charge_efficiency

    natural_export = 0.0
    if remaining_pv > 0.0:
        unallocated_pv = max(remaining_pv - charge_input, 0.0)
        remaining_system_headroom = max(
            system_energy
            - pv_to_load
            - delivered_to_load
            - export
            - charge_input,
            0.0,
        )
        grid_caps = [
            max(cap, 0.0)
            for cap in (
                settings.export_power_cap_kw,
                settings.effective_export_power_kw,
            )
            if cap is not None
        ]
        remaining_grid_headroom = (
            max(min(grid_caps) * duration_hours - export, 0.0)
            if grid_caps
            else remaining_system_headroom
        )
        natural_export = min(
            unallocated_pv,
            remaining_system_headroom,
            remaining_grid_headroom,
        )

    battery = min(battery, capacity)
    expected_battery = (
        battery_before
        - delivered_to_load / house_efficiency
        - export / export_efficiency
        + charge_input * charge_efficiency
    )
    balance_error = battery - expected_battery
    feasible = (
        battery >= hard_floor_kwh_dc - 1e-6
        and (
            export <= 1e-9
            or battery >= max(export_floor_kwh_dc, hard_floor_kwh_dc) - 1e-6
        )
        and abs(balance_error) <= 1e-6
    )
    return _RCEPhysicalSlotResult(
        feasible=feasible,
        home_energy_shortage=home_energy_shortage,
        battery_after_kwh_dc=battery,
        delivered_to_load_kwh_ac=delivered_to_load,
        grid_import_kwh_ac=grid_import,
        controlled_export_kwh_ac=export,
        natural_export_kwh_ac=natural_export,
        charge_input_kwh_ac=charge_input,
        balance_error_kwh=balance_error,
    )


def _simulate(
    starts: list[datetime],
    settings: OptimizerInput,
    load_by_slot: Mapping[datetime, float],
    exports: Mapping[datetime, float],
    floor_kwh: float,
    export_reserve_by_slot: Mapping[datetime, float] | None = None,
    pv_by_slot_kwh: Mapping[datetime, float] | None = None,
    slot_fractions: Mapping[datetime, float] | None = None,
    trace_collector: list[_RCESimulationSlot] | None = None,
) -> tuple[bool, float, dict[datetime, float]]:
    physical_constants = _physical_constants(settings)
    capacity = physical_constants.battery_capacity_kwh
    battery = capacity * settings.battery_soc_percent / 100.0
    natural_exports: dict[datetime, float] = {}
    pv_map = pv_by_slot_kwh or settings.pv_by_slot_kwh
    for start in starts:
        battery_before = battery
        pv = max(float(pv_map.get(start, 0.0)), 0.0)
        load = max(float(load_by_slot.get(start, 0.0)), 0.0)
        export = max(float(exports.get(start, 0.0)), 0.0)
        fraction = (
            min(max(float(slot_fractions.get(start, 1.0)), 0.0), 1.0)
            if slot_fractions is not None
            else 1.0
        )
        export_floor = (
            max(float(export_reserve_by_slot.get(start, floor_kwh)), floor_kwh)
            if export_reserve_by_slot is not None
            else floor_kwh
        )
        physical = _simulate_physical_slot(
            settings,
            battery_kwh_dc=battery,
            load_kwh_ac=load,
            pv_kwh_ac=pv,
            controlled_export_kwh_ac=export,
            hard_floor_kwh_dc=floor_kwh,
            export_floor_kwh_dc=export_floor,
            slot_fraction=fraction,
            physical_constants=physical_constants,
        )
        battery = physical.battery_after_kwh_dc
        if physical.natural_export_kwh_ac > 0.0:
            natural_exports[start] = physical.natural_export_kwh_ac
        if trace_collector is not None:
            end = start + SLOT
            effective_start = end - SLOT * fraction
            trace_collector.append(
                _RCESimulationSlot(
                    slot_start=start,
                    start=effective_start,
                    end=end,
                    pv_kwh=pv,
                    load_kwh=load,
                    battery_delta_kwh=battery - battery_before,
                    grid_import_kwh=physical.grid_import_kwh_ac,
                    grid_export_kwh=(
                        physical.controlled_export_kwh_ac
                        + physical.natural_export_kwh_ac
                    ),
                    battery_after_kwh=battery,
                )
            )
        if physical.home_energy_shortage:
            return False, battery, {}
        if not physical.feasible:
            return False, battery, {}
    return True, battery, natural_exports


def _quantize_4306_percent(value: float) -> float:
    """Return a fail-closed value exactly representable by register 4306."""

    if not math.isfinite(value) or value < 1.0:
        return 0.0
    bounded = min(value, 100.0)
    # New inverter commands use whole percentage points. Keep physical
    # readback/restore precision separate; never raise a BMS or planning cap.
    return float(math.floor(bounded))


def _rce_timeline_trace(
    *,
    settings: OptimizerInput,
    selected: list[_RCESimulationSlot],
    baseline: list[_RCESimulationSlot],
    exports: Mapping[datetime, float],
    price_by_start: Mapping[datetime, float],
    floor_kwh: float,
    export_reserve_by_slot: Mapping[datetime, float],
    quality: str = "complete",
    horizon_hours: float | None = 48.0,
) -> OptimizerTimelineTrace | None:
    """Merge two already-executed simulations into one immutable RCE trace."""

    if not selected or len(selected) != len(baseline):
        return None
    capacity = max(settings.battery_capacity_kwh, 0.001)
    baseline_by_start = {item.slot_start: item for item in baseline}
    points: list[TimelineTracePoint] = []
    horizon_limit = (
        selected[0].start + timedelta(hours=horizon_hours)
        if horizon_hours is not None else None
    )
    system_power = settings.inverter_power_kw * settings.inverter_count
    ac_system_power = _inverter_ac_power_kw(settings) * settings.inverter_count
    requested_power = system_power * _quantize_4306_percent(
        settings.discharge_power_percent
    ) / 100.0
    export_efficiency = max(
        min(settings.export_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    for item in selected:
        if horizon_limit is not None and item.end > horizon_limit:
            break
        baseline_item = baseline_by_start.get(item.slot_start)
        if baseline_item is None:
            return None
        duration_hours = max(
            (item.end - item.start).total_seconds() / 3600.0,
            1e-9,
        )
        planned_export = max(float(exports.get(item.slot_start, 0.0)), 0.0)
        sell_price = price_by_start.get(item.slot_start)
        system_energy = ac_system_power * duration_hours
        pv_to_load = min(item.pv_kwh, item.load_kwh, system_energy)
        load_deficit_power = max(
            item.load_kwh - pv_to_load,
            0.0,
        ) / duration_hours
        bridge_discharge_power = max(
            system_energy - pv_to_load,
            0.0,
        ) / duration_hours
        planned_discharge_power = (
            planned_export / duration_hours + load_deficit_power
        )
        bms_total_ac_power = _bms_total_ac_discharge_power_limit_kw(
            settings,
            load_deficit_power,
        )
        command_discharge_power = min(
            planned_discharge_power,
            requested_power,
            system_power,
            bridge_discharge_power,
            bms_total_ac_power,
        )
        command_discharge_power_percent = _quantize_4306_percent(
            command_discharge_power / system_power * 100.0
        )
        points.append(
            TimelineTracePoint(
                start=item.start,
                end=item.end,
                pv_kwh=item.pv_kwh,
                load_kwh=item.load_kwh,
                battery_delta_kwh=item.battery_delta_kwh,
                grid_import_kwh=item.grid_import_kwh,
                grid_export_kwh=item.grid_export_kwh,
                soc_percent=min(
                    max(item.battery_after_kwh / capacity * 100.0, 0.0),
                    100.0,
                ),
                baseline_soc_percent=min(
                    max(baseline_item.battery_after_kwh / capacity * 100.0, 0.0),
                    100.0,
                ),
                protected_soc_floor_percent=min(
                    max(
                        max(
                            export_reserve_by_slot.get(item.slot_start, floor_kwh),
                            floor_kwh,
                        )
                        / capacity
                        * 100.0,
                        0.0,
                    ),
                    100.0,
                ),
                action_code="export" if planned_export >= 0.001 else "idle",
                selected=planned_export >= 0.001,
                quality=quality,
                policy=RCEPolicyPoint(
                    sell_price_pln_kwh=sell_price,
                    planned_export_kwh=planned_export,
                    planned_battery_withdrawal_kwh=(
                        planned_export / export_efficiency
                    ),
                    target_discharge_kw=planned_export / duration_hours,
                    command_discharge_power_percent=(
                        command_discharge_power_percent
                        if planned_export >= 0.001
                        else 0.0
                    ),
                    target_tolerance_kw=0.05,
                    expected_revenue_pln=(
                        planned_export * sell_price
                        if sell_price is not None
                        else None
                    ),
                ),
            )
        )
    return OptimizerTimelineTrace(
        policy_id="rce",
        points=tuple(points),
        quality=quality,
    )


def _market_revenue(
    exports: Mapping[datetime, float],
    natural_exports: Mapping[datetime, float],
    price_by_start: Mapping[datetime, float],
) -> float:
    """Return revenue from scheduled and natural exports."""
    return sum(
        energy * price_by_start.get(start, 0.0)
        for start, energy in exports.items()
    ) + sum(
        energy * price_by_start.get(start, 0.0)
        for start, energy in natural_exports.items()
    )


def _required_energy_now(
    starts: list[datetime],
    settings: OptimizerInput,
    load_by_slot: Mapping[datetime, float],
    floor_kwh: float,
    pv_by_slot_kwh: Mapping[datetime, float] | None = None,
    slot_fractions: Mapping[datetime, float] | None = None,
) -> float:
    required = floor_kwh
    capacity = settings.battery_capacity_kwh
    pv_map = pv_by_slot_kwh or settings.pv_by_slot_kwh
    charge_efficiency = max(
        min(settings.charge_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    discharge_efficiency = max(
        min(settings.house_discharge_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    for start in reversed(starts):
        load = max(float(load_by_slot.get(start, 0.0)), 0.0)
        pv = max(float(pv_map.get(start, 0.0)), 0.0)
        fraction = (
            min(max(float(slot_fractions.get(start, 1.0)), 0.0), 1.0)
            if slot_fractions is not None
            else 1.0
        )
        if pv >= load:
            charge_input_ac = _slot_charge_input_limit_kwh(
                settings,
                load,
                pv,
                fraction,
            )
            required -= charge_input_ac * charge_efficiency
        else:
            required += (load - pv) / discharge_efficiency
        required = min(max(required, floor_kwh), capacity)
    return required


def _economic_objective(
    *,
    exports: Mapping[datetime, float],
    natural_exports: Mapping[datetime, float],
    price_by_start: Mapping[datetime, float],
    ending_battery_kwh: float,
    floor_kwh: float,
    export_efficiency: float,
    battery_wear_cost_pln_kwh: float,
    terminal_energy_target_kwh: float,
    terminal_energy_value_pln_kwh: float,
) -> tuple[float, float, float]:
    """Return RCE objective, wear cost and diagnostic terminal value.

    RCE is explicitly a sale-profit optimizer.  Day-3 avoided-import value is
    still calculated for dashboards and diagnostics, but it cannot veto an
    otherwise profitable export.  Household safety remains a hard physical
    constraint through the base reserve, protected night and LOAD/PV horizon.
    """

    revenue = _market_revenue(exports, natural_exports, price_by_start)
    dc_throughput = sum(max(value, 0.0) for value in exports.values()) / max(
        export_efficiency,
        0.01,
    )
    wear = dc_throughput * max(battery_wear_cost_pln_kwh, 0.0)
    retained = min(
        max(ending_battery_kwh - floor_kwh, 0.0),
        max(terminal_energy_target_kwh, 0.0),
    )
    terminal_value = retained * max(terminal_energy_value_pln_kwh, 0.0)
    return revenue - wear, wear, terminal_value


def _protected_night_reserve_by_slot(
    starts: list[datetime],
    settings: OptimizerInput,
    load_by_slot: Mapping[datetime, float],
    floor_kwh: float,
) -> tuple[float, dict[datetime, float]]:
    """Return current and per-export reserves for the next protected night.

    PV expected before sunset must not erase the explicit night reserve.  For
    every possible export slot we therefore retain the base outage reserve plus
    all forecast house load still remaining in the current or next protected
    night window.  The reserve decreases only while that night is actually
    consumed and is rebuilt for the following night after sunrise.
    """

    capacity = settings.battery_capacity_kwh
    discharge_efficiency = max(
        min(settings.house_discharge_efficiency_percent / 100.0, 1.0),
        0.01,
    )

    def remaining_night_energy(after_index: int) -> float:
        entered_night = False
        energy = 0.0
        for later in starts[after_index:]:
            is_night = _is_night(
                _local_slot(later, settings),
                settings.night_start_minute,
                settings.night_end_minute,
            )
            if not entered_night:
                if not is_night:
                    continue
                entered_night = True
            elif not is_night:
                break
            energy += (
                max(float(load_by_slot.get(later, 0.0)), 0.0)
                / discharge_efficiency
            )
        return energy

    current_night_energy = remaining_night_energy(0)
    reserve_by_slot = {
        start: _quantize_reserve_to_soc_percent(
            min(
                floor_kwh + remaining_night_energy(index + 1),
                capacity,
            ),
            capacity,
        )
        for index, start in enumerate(starts)
    }
    return current_night_energy, reserve_by_slot


def _solve_joint_horizon_exports(
    *,
    starts: list[datetime],
    settings: OptimizerInput,
    candidates: list[tuple[PriceSlot, datetime]],
    load_by_slot: Mapping[datetime, float],
    floor_kwh: float,
    export_reserve_by_slot: Mapping[datetime, float],
    conservative_pv: Mapping[datetime, float],
    expected_pv: Mapping[datetime, float],
    slot_fractions: Mapping[datetime, float],
    price_by_start: Mapping[datetime, float],
    baseline_objective: float,
    export_efficiency: float,
    terminal_energy_target: float,
    terminal_unit_value: float,
) -> dict[datetime, float]:
    """Return a bounded joint-horizon plan across deterministic active sets.

    A one-slot-at-a-time economic acceptance rule is not globally valid for a
    battery with finite headroom.  Several individually neutral exports can
    jointly prevent a later low-price PV spill.  This solver therefore builds
    complete feasible active sets first and only then optimizes every continuous
    slot amount against the *whole* horizon objective.

    For short horizons every active set is enumerated.  Normal 48-hour horizons
    use economically distinct price thresholds plus chronological and reverse
    chronological bases.  Only the strongest complete plans receive a bounded
    coordinate refinement; this keeps runtime predictable for HA while still
    optimizing the coupled headroom effect which defeated the former greedy
    planner.  Every trial is checked by ``_simulate``; the hard home reserve,
    protected night, both PV trajectories, shared LOAD/export power, BMS/GCF
    caps and current-slot fraction remain authoritative.
    """

    if not candidates:
        return {}

    candidate_by_start = {start: slot for slot, start in candidates}
    price_order = [
        start
        for _, start in sorted(
            candidates,
            key=lambda item: (-item[0].price_pln_kwh, item[1]),
        )
    ]
    reverse_tie_price_order = [
        start
        for _, start in sorted(
            candidates,
            key=lambda item: (
                -item[0].price_pln_kwh,
                -item[1].timestamp(),
            ),
        )
    ]
    short_horizon = len(candidates) <= 7
    # Padding rows which cannot cover battery wear are common in a complete
    # 48-hour PSE response.  Keep every row in seeds and physical simulations,
    # but rank the bounded exchange set by direct marginal net value.  Lower
    # priced rows which are active in a strong seed are added back below; they
    # can still be valuable indirectly by creating later PV headroom.
    direct_break_even_price = (
        max(settings.battery_wear_cost_pln_kwh, 0.0)
        / max(export_efficiency, 0.01)
    )
    profitable_price_order = [
        start
        for start in price_order
        if candidate_by_start[start].price_pln_kwh
        > direct_break_even_price + 1e-9
    ]
    chronological = sorted(candidate_by_start)

    def normalized(plan: Mapping[datetime, float]) -> dict[datetime, float]:
        return {
            start: max(float(energy), 0.0)
            for start, energy in plan.items()
            if energy >= 0.001
        }

    def signature(plan: Mapping[datetime, float]) -> tuple[tuple[int, int], ...]:
        return tuple(
            (int(start.timestamp()), round(energy * 10000.0))
            for start, energy in sorted(normalized(plan).items())
        )

    feasibility_cache: dict[tuple[tuple[int, int], ...], bool] = {}
    objective_cache: dict[tuple[tuple[int, int], ...], float] = {}

    def feasible(plan: Mapping[datetime, float]) -> bool:
        key = signature(plan)
        cached = feasibility_cache.get(key)
        if cached is not None:
            return cached
        value = _simulate(
            starts,
            settings,
            load_by_slot,
            plan,
            floor_kwh,
            export_reserve_by_slot,
            conservative_pv,
            slot_fractions,
        )[0]
        feasibility_cache[key] = value
        return value

    def objective(plan: Mapping[datetime, float]) -> float:
        trial = normalized(plan)
        key = signature(trial)
        cached = objective_cache.get(key)
        if cached is not None:
            return cached
        if not feasible(trial):
            return -math.inf
        _, expected_end, natural = _simulate(
            starts,
            settings,
            load_by_slot,
            trial,
            floor_kwh,
            export_reserve_by_slot,
            expected_pv,
            slot_fractions,
        )
        value = _economic_objective(
            exports=trial,
            natural_exports=natural,
            price_by_start=price_by_start,
            ending_battery_kwh=expected_end,
            floor_kwh=floor_kwh,
            export_efficiency=export_efficiency,
            battery_wear_cost_pln_kwh=settings.battery_wear_cost_pln_kwh,
            terminal_energy_target_kwh=terminal_energy_target,
            terminal_energy_value_pln_kwh=terminal_unit_value,
        )[0]
        objective_cache[key] = value
        return value

    def slot_physical_cap(start: datetime) -> float:
        return _slot_export_limit_kwh(
            settings,
            load_by_slot.get(start, 0.0),
            conservative_pv.get(start, 0.0),
            slot_fractions.get(start, 1.0),
        )

    def maximum_feasible(
        base: Mapping[datetime, float],
        start: datetime,
    ) -> float:
        low = 0.0
        high = slot_physical_cap(start)
        if high <= 0.0:
            return 0.0
        full_trial = dict(base)
        full_trial[start] = high
        if feasible(full_trial):
            return high
        minimum_trial = dict(base)
        minimum_trial[start] = min(0.01, high)
        if not feasible(minimum_trial):
            return 0.0
        # A coarse fixed iteration count left material residual energy whenever
        # the physical slot cap was large (for example 1.125 kWh from a 23 kWh
        # budget).  Resolve every boundary below 0.01 kWh.  Twelve probes are
        # the minimum for long horizons; exceptionally large caps receive only
        # the few additional probes mathematically required by their range.
        precision_iterations = max(
            12,
            math.ceil(math.log2(max(high / 0.01, 1.0))),
        )
        for _ in range(precision_iterations):
            middle = (low + high) / 2.0
            trial = dict(base)
            trial[start] = middle
            if feasible(trial):
                low = middle
            else:
                high = middle
        return low

    def grow(
        order: Iterable[datetime],
        active: set[datetime] | None = None,
    ) -> dict[datetime, float]:
        plan: dict[datetime, float] = {}
        for start in order:
            if active is not None and start not in active:
                continue
            energy = maximum_feasible(plan, start)
            if energy >= 0.01:
                plan[start] = energy
        return plan

    # Complete active-set enumeration is practical for short horizons and is
    # also the release-test oracle path.  Longer horizons receive deterministic
    # bases at every distinct market-price boundary.
    seeds: dict[tuple[tuple[int, int], ...], tuple[float, dict[datetime, float]]] = {}

    def remember(plan: Mapping[datetime, float]) -> None:
        clean = normalized(plan)
        value = objective(clean)
        key = signature(clean)
        previous = seeds.get(key)
        if previous is None or value > previous[0]:
            seeds[key] = (value, clean)

    remember({})
    remember(grow(chronological))
    if short_horizon:
        remember(grow(price_order))
        remember(grow(reversed(chronological)))
        for mask in range(1, 1 << len(price_order)):
            active = {
                start
                for index, start in enumerate(price_order)
                if mask & (1 << index)
            }
            remember(grow(price_order, active))
            remember(grow(chronological, active))
    else:
        # One price-ordered pass produces every threshold prefix.  Remembering
        # the plan whenever the price changes costs no extra feasibility
        # searches and prevents a profitable middle price band from vanishing
        # between only the maximum/minimum thresholds.
        price_prefix: dict[datetime, float] = {}
        for index, start in enumerate(price_order):
            energy = maximum_feasible(price_prefix, start)
            if energy >= 0.01:
                price_prefix[start] = energy
            current_price = candidate_by_start[start].price_pln_kwh
            next_price = (
                candidate_by_start[price_order[index + 1]].price_pln_kwh
                if index + 1 < len(price_order)
                else None
            )
            if next_price != current_price:
                remember(price_prefix)

        # Equal-price slots are not interchangeable when an early export can
        # consume battery headroom which later PV would otherwise refill.  A
        # second threshold pass with reversed chronological tie-breaking is a
        # linear, deterministic hedge against that coupling on real 48-hour
        # horizons; it does not enumerate active sets.
        reverse_price_prefix: dict[datetime, float] = {}
        for index, start in enumerate(reverse_tie_price_order):
            energy = maximum_feasible(reverse_price_prefix, start)
            if energy >= 0.01:
                reverse_price_prefix[start] = energy
            current_price = candidate_by_start[start].price_pln_kwh
            next_price = (
                candidate_by_start[
                    reverse_tie_price_order[index + 1]
                ].price_pln_kwh
                if index + 1 < len(reverse_tie_price_order)
                else None
            )
            if next_price != current_price:
                remember(reverse_price_prefix)

    def optimize_coordinate(
        original: Mapping[datetime, float],
        order: Iterable[datetime],
        *,
        passes: int = 2,
    ) -> tuple[float, dict[datetime, float]]:
        plan = normalized(original)
        current_value = objective(plan)
        for _ in range(max(passes, 1)):
            changed = False
            for start in order:
                base = dict(plan)
                old_energy = base.pop(start, 0.0)
                high = maximum_feasible(base, start)
                if high < 0.001:
                    candidate_energy = 0.0
                    candidate_value = objective(base)
                else:
                    # Natural PV spill creates non-concave one-dimensional
                    # sections.  Scan the complete bounded interval first,
                    # then refine around its best section.
                    grid = [high * index / 8.0 for index in range(9)]
                    # Never worsen a seed solely because its existing
                    # continuous amount lies between coarse grid points.
                    if 0.0 < old_energy < high:
                        grid.append(old_energy)
                    values = []
                    for energy in grid:
                        trial = dict(base)
                        if energy >= 0.001:
                            trial[start] = energy
                        values.append(objective(trial))
                    best_index = max(range(len(grid)), key=values.__getitem__)
                    left = grid[max(best_index - 1, 0)]
                    right = grid[min(best_index + 1, len(grid) - 1)]
                    for _ in range(12):
                        first = left + (right - left) / 3.0
                        second = right - (right - left) / 3.0
                        first_trial = dict(base)
                        second_trial = dict(base)
                        if first >= 0.001:
                            first_trial[start] = first
                        if second >= 0.001:
                            second_trial[start] = second
                        if objective(first_trial) < objective(second_trial):
                            left = first
                        else:
                            right = second
                    choices = (
                        0.0,
                        old_energy,
                        high,
                        left,
                        (left + right) / 2.0,
                        right,
                    )
                    candidate_energy = 0.0
                    candidate_value = -math.inf
                    for energy in choices:
                        trial = dict(base)
                        if energy >= 0.001:
                            trial[start] = energy
                        value = objective(trial)
                        if value > candidate_value:
                            candidate_value = value
                            candidate_energy = energy
                if candidate_energy >= 0.01:
                    base[start] = candidate_energy
                plan = base
                if abs(candidate_energy - old_energy) >= 0.005:
                    changed = True
                current_value = candidate_value
            if not changed:
                break
        return current_value, normalized(plan)

    # Refine only the strongest distinct bases; this bounds HA update latency
    # independently of the number of PSE rows.
    # Short/medium horizons have only a handful of deterministic seeds.  Keep
    # enough of them for refinement so a middle-price active set is not
    # discarded merely because its unrefined boundary plan ranks below two
    # extreme-price seeds.  Real 48-hour horizons stay capped at two, which is
    # the part that controls Home Assistant event-loop latency.
    strongest_count = (
        len(seeds) if short_horizon else (8 if len(candidates) <= 10 else 2)
    )
    strongest = sorted(seeds.values(), key=lambda item: item[0], reverse=True)[
        :strongest_count
    ]
    best_value = baseline_objective
    best_plan: dict[datetime, float] = {}
    for seed_value, seed in strongest:
        if seed_value > best_value + 0.0001:
            best_value = seed_value
            best_plan = seed
        # On a real 48-hour horizon, optimizing every empty coordinate would
        # make runtime scale with all PSE rows.  Threshold seeds already choose
        # the active set; refine only their active amounts in a single pass.
        # This is enough to avoid a full-slot, below-wear discharge when only a
        # partial export is needed to create PV headroom.
        if len(candidates) <= 10:
            coordinate_order = price_order
            passes = 2
        else:
            # Bound long-horizon work independently of market-row count.  The
            # economically relevant partial-headroom correction is on the top
            # active sale branches; lower-price active amounts remain at their
            # already feasible seed boundaries.
            coordinate_order = [
                start for start in price_order if start in seed
            ][:2]
            passes = 1
        orders: tuple[Iterable[datetime], ...] = (coordinate_order,)
        for order in orders:
            value, plan = optimize_coordinate(seed, order, passes=passes)
            if value > best_value + 0.0001:
                best_value = value
                best_plan = plan

    # Build a genuinely bounded active/relevant exchange set.  At most six
    # low-value active coordinates are candidates for removal; the remaining
    # places go first to profitable inactive rows, then to rows active in an
    # alternative strong seed and immediate temporal neighbours.  The final
    # highest-price fallback also covers a below-wear headroom opportunity
    # without letting dozens of zero/small-positive padding rows disable the
    # exchange.  Six active places are intentional: a few profitable tail
    # rows can otherwise consume all low-price ranks and hide the earlier
    # export whose removal restores expected PV value.  Four inactive places
    # remain available while the total search set stays capped at ten.
    active_order = sorted(
        best_plan,
        key=lambda start: (
            candidate_by_start[start].price_pln_kwh,
            start,
        ),
    )
    inactive_priority: list[datetime] = []
    inactive_seen: set[datetime] = set()

    def add_inactive(start: datetime) -> None:
        if start in best_plan or start in inactive_seen:
            return
        inactive_seen.add(start)
        inactive_priority.append(start)

    for start in profitable_price_order:
        add_inactive(start)
    for _, seed in strongest:
        for start in price_order:
            if start in seed:
                add_inactive(start)
    chronological_index = {
        start: index for index, start in enumerate(chronological)
    }
    for active in active_order:
        index = chronological_index[active]
        if index > 0:
            add_inactive(chronological[index - 1])
        if index + 1 < len(chronological):
            add_inactive(chronological[index + 1])
    for start in price_order:
        add_inactive(start)

    active_limit = min(len(active_order), 6)
    selected_pair_starts = active_order[:active_limit]
    selected_pair_starts.extend(
        inactive_priority[: 10 - len(selected_pair_starts)]
    )
    pair_starts = sorted(selected_pair_starts)
    pair_price_spread = (
        max(candidate_by_start[start].price_pln_kwh for start in pair_starts)
        - min(candidate_by_start[start].price_pln_kwh for start in pair_starts)
        if pair_starts
        else 0.0
    )
    # Runtime is bounded by ``pair_starts`` itself, not by the number of rows
    # whose price happens to clear the wear threshold.  A normal 48-hour PSE
    # payload may contain dozens of barely-above-wear padding rows; they must
    # not switch off refinement of the genuinely relevant active/inactive
    # coordinates selected above.
    sparse_exchange = not short_horizon and 2 <= len(pair_starts) <= 10

    # A threshold seed is grown to every feasible boundary.  Under different
    # conservative/expected PV trajectories, that can retain an export which
    # is physically safe but destroys expected natural-export value.  A swap
    # search cannot remove it unless a useful inactive coordinate exists.
    # Prune at most the six bounded low-value active rows first, accepting
    # only strict whole-horizon improvements.  This is a tiny linear local
    # search (at most 6 + 5 + ... + 1 objective evaluations), not an active-set
    # enumeration.  If pruning changed the plan, refine only the surviving
    # coordinates from the same bounded set so newly released PV/battery
    # headroom can be assigned continuously.
    pruned = False
    if sparse_exchange:
        for _ in range(active_limit):
            removal_value = best_value
            removal_plan: dict[datetime, float] | None = None
            for start in pair_starts:
                if start not in best_plan:
                    continue
                trial = dict(best_plan)
                trial.pop(start, None)
                value = objective(trial)
                if value > removal_value + 0.0001:
                    removal_value = value
                    removal_plan = trial
            if removal_plan is None:
                break
            best_value = removal_value
            best_plan = removal_plan
            pruned = True
        if pruned:
            refinement_order = [
                start for start in pair_starts if start in best_plan
            ]
            if refinement_order:
                refined_value, refined_plan = optimize_coordinate(
                    best_plan,
                    refinement_order,
                    passes=1,
                )
                if refined_value > best_value + 0.0001:
                    best_value = refined_value
                    best_plan = refined_plan

    # The exchange search remains bounded even when the complete market input
    # contains many directly profitable rows: only ``pair_starts`` (at most
    # ten relevant coordinates) participates.
    if (
        sparse_exchange
        # Equal-price timing is already covered by both chronological tie
        # orders above.  Repeating every active/inactive exchange in that case
        # cannot improve direct sale value and consumed most of the 48-hour
        # runtime budget in flat price bands.
        and pair_price_spread > 1e-9
        and any(start in best_plan for start in pair_starts)
        and any(start not in best_plan for start in pair_starts)
    ):
        # Coordinate descent cannot cross a valley where one early export must
        # be removed at the same time as a later slot is added.  Search only
        # active/inactive exchanges inside the bounded relevant set.  Each
        # trial grows both coordinates to their exact feasible boundary; the
        # single winning active set is then continuously refined.  This avoids
        # running the expensive one-dimensional scan for every padded market
        # row while preserving a runtime linear in the simulated horizon.
        # Medium horizons can require two consecutive exchanges to cross a
        # three-coordinate valley.  Keep that exhaustive-on-the-bounded-set
        # behaviour for <=10 rows; real 48-hour inputs get one pass.
        exchange_passes = (
            2
            if len(candidates) <= 10
            or any(
                candidate_by_start[start].price_pln_kwh
                <= direct_break_even_price + 1e-9
                for start in pair_starts
            )
            else 1
        )
        for _ in range(exchange_passes):
            pass_value = best_value
            pass_plan = best_plan
            pass_order: tuple[datetime, datetime] | None = None
            active_starts = [
                start for start in pair_starts if start in best_plan
            ]
            inactive_starts = [
                start for start in pair_starts if start not in best_plan
            ]
            for first in active_starts:
                for second in inactive_starts:
                    base = dict(best_plan)
                    base.pop(first, None)
                    base.pop(second, None)
                    for order in ((first, second), (second, first)):
                        plan = dict(base)
                        for start in order:
                            energy = maximum_feasible(plan, start)
                            if energy >= 0.01:
                                plan[start] = energy
                        value = objective(plan)
                        if value > pass_value + 0.0001:
                            pass_value = value
                            pass_plan = plan
                            pass_order = order
            if pass_value <= best_value + 0.0001:
                break
            refined_value, refined_plan = optimize_coordinate(
                pass_plan,
                pass_order or pair_starts,
                passes=1,
            )
            if refined_value > pass_value + 0.0001:
                pass_value = refined_value
                pass_plan = refined_plan
            best_value = pass_value
            best_plan = pass_plan
    if not short_horizon:
        # Keep the complete incumbent above, including coordinate refinement,
        # pruning and bounded exchanges.  Independent local-day prefixes are
        # additional whole-horizon alternatives only: they cannot displace a
        # stronger legacy result and they never combine separately-budgeted
        # daily plans.  This covers a search gap where a small expensive sale
        # on a later day could anchor every global price prefix and hide the
        # better active set on an earlier day.
        planning_timezone = settings.now.tzinfo or dt_timezone.utc
        local_days = sorted(
            {start.astimezone(planning_timezone).date() for start in price_order}
        )
        daily_orders_seen: set[tuple[datetime, ...]] = set()
        for local_day in local_days:
            # One deterministic price/UTC order is sufficient here.  The
            # legacy global path above already keeps its reverse tie hedge;
            # duplicating it per day would make the added work scale twice
            # with every market row without expanding price-prefix coverage.
            for complete_order in (price_order,):
                daily_order = tuple(
                    start
                    for start in complete_order
                    if start.astimezone(planning_timezone).date() == local_day
                )
                if not daily_order or daily_order in daily_orders_seen:
                    continue
                daily_orders_seen.add(daily_order)
                daily_prefix: dict[datetime, float] = {}
                for index, start in enumerate(daily_order):
                    energy = maximum_feasible(daily_prefix, start)
                    if energy >= 0.01:
                        daily_prefix[start] = energy
                    current_price = candidate_by_start[start].price_pln_kwh
                    next_price = (
                        candidate_by_start[daily_order[index + 1]].price_pln_kwh
                        if index + 1 < len(daily_order)
                        else None
                    )
                    if next_price == current_price:
                        continue
                    value = objective(daily_prefix)
                    if value > best_value + 0.0001:
                        best_value = value
                        best_plan = dict(daily_prefix)
    if not short_horizon:
        # Price prefixes retain earlier choices, and the bounded exchange set
        # can omit a feasible later window (or fail to replace two early sales
        # together). Compare independent single-slot plans after refinement so
        # adding alternatives never discards the incumbent's refined result.
        # Keep the same whole-horizon reserve, PV scenarios and objective.
        for start in chronological:
            candidate = grow((start,))
            value = objective(candidate)
            if value > best_value + 0.0001:
                best_value = value
                best_plan = candidate
    # Equal-price timing is a deterministic tie, not an economic reason to
    # discard the current half-hour. A later-only seed can win the bounded
    # search even when shifting its energy into the current slot is feasible
    # and has exactly the same whole-horizon objective. Check one such shift
    # on the same fresh physics and prices; never accept any objective loss.
    current_start = floor_half_hour(settings.now).astimezone(dt_timezone.utc)
    if current_start in candidate_by_start and best_plan.get(current_start, 0.0) < 0.01:
        current_cap = slot_physical_cap(current_start)
        if current_cap >= 0.01:
            def exact_objective(plan: Mapping[datetime, float]) -> float:
                # The search cache rounds energy to 0.0001 kWh. Its key can
                # alias neighbouring plans near a physical boundary; the
                # final equivalence check must use the actual amounts.
                if not _simulate(
                    starts, settings, load_by_slot, plan, floor_kwh,
                    export_reserve_by_slot, conservative_pv, slot_fractions,
                )[0]:
                    return -math.inf
                expected_feasible, ending, natural = _simulate(
                    starts, settings, load_by_slot, plan, floor_kwh,
                    export_reserve_by_slot, expected_pv, slot_fractions,
                )
                if not expected_feasible:
                    return -math.inf
                return _economic_objective(
                    exports=plan,
                    natural_exports=natural,
                    price_by_start=price_by_start,
                    ending_battery_kwh=ending,
                    floor_kwh=floor_kwh,
                    export_efficiency=export_efficiency,
                    battery_wear_cost_pln_kwh=settings.battery_wear_cost_pln_kwh,
                    terminal_energy_target_kwh=terminal_energy_target,
                    terminal_energy_value_pln_kwh=terminal_unit_value,
                )[0]

            for later_start in sorted(best_plan):
                if (
                    later_start <= current_start
                    or candidate_by_start[later_start].price_pln_kwh
                    != candidate_by_start[current_start].price_pln_kwh
                ):
                    continue
                moved = min(current_cap, best_plan[later_start])
                if moved < 0.01:
                    continue
                trial = dict(best_plan)
                trial[current_start] = moved
                trial[later_start] -= moved
                if trial[later_start] < 0.001:
                    trial.pop(later_start)
                trial_value = exact_objective(trial)
                if (
                    trial_value != -math.inf
                    and trial_value + EXACT_TIE_ROUNDOFF_PLN >= exact_objective(best_plan)
                ):
                    best_plan = trial
                break
    return best_plan


def _filtered_timeline(
    settings: OptimizerInput,
    result: OptimizerResult,
    exports: Mapping[datetime, float],
) -> None:
    """Re-simulate the already-bounded horizon after an R09 reduction."""

    trace = result.timeline_trace
    if trace is None or not trace.points:
        return
    points = tuple(trace.points)
    starts = [
        (point.end - SLOT).astimezone(dt_timezone.utc)
        for point in points
    ]
    load_by_slot = {
        start: max(float(point.load_kwh or 0.0), 0.0)
        for start, point in zip(starts, points, strict=True)
    }
    expected_pv = {
        start: max(float(point.pv_kwh or 0.0), 0.0)
        for start, point in zip(starts, points, strict=True)
    }
    slot_fractions = {
        start: min(
            max(
                (point.end - point.start).total_seconds()
                / SLOT.total_seconds(),
                0.0,
            ),
            1.0,
        )
        for start, point in zip(starts, points, strict=True)
    }
    conservative_source = (
        settings.conservative_pv_by_slot_kwh
        if settings.conservative_pv_by_slot_kwh is not None
        else settings.pv_by_slot_kwh
    )
    conservative_source_utc = _utc_energy_map(conservative_source)
    conservative_pv = {
        start: max(float(conservative_source_utc.get(start, 0.0)), 0.0)
        * slot_fractions[start]
        for start in starts
    }
    if (
        starts
        and settings.current_pv_power_kw is not None
        and math.isfinite(settings.current_pv_power_kw)
        and settings.current_pv_power_kw >= 0.0
    ):
        conservative_pv[starts[0]] = expected_pv[starts[0]]
    price_by_start: dict[datetime, float] = {}
    export_reserve_by_slot: dict[datetime, float] = {}
    for start, point in zip(starts, points, strict=True):
        if isinstance(point.policy, RCEPolicyPoint):
            price = point.policy.sell_price_pln_kwh
            if price is not None and math.isfinite(float(price)):
                price_by_start[start] = float(price)
        export_reserve_by_slot[start] = max(
            result.base_reserve_energy_kwh,
            settings.battery_capacity_kwh
            * float(point.protected_soc_floor_percent or 0.0)
            / 100.0,
        )

    baseline_trace: list[_RCESimulationSlot] = []
    _simulate(
        starts,
        settings,
        load_by_slot,
        {},
        result.base_reserve_energy_kwh,
        export_reserve_by_slot,
        expected_pv,
        slot_fractions,
        trace_collector=baseline_trace,
    )
    selected_trace: list[_RCESimulationSlot] = []
    feasible, expected_ending, natural_exports = _simulate(
        starts,
        settings,
        load_by_slot,
        exports,
        result.base_reserve_energy_kwh,
        export_reserve_by_slot,
        expected_pv,
        slot_fractions,
        trace_collector=selected_trace,
    )
    if not feasible:
        # A proportional reduction of a feasible legacy plan should remain
        # feasible.  If an invariant is ever violated, withdraw the plan.
        result.planned_exports = []
        exports = {}
        selected_trace.clear()
        _, expected_ending, natural_exports = _simulate(
            starts,
            settings,
            load_by_slot,
            {},
            result.base_reserve_energy_kwh,
            export_reserve_by_slot,
            expected_pv,
            slot_fractions,
            trace_collector=selected_trace,
        )
        result.self_consumption_filter_status_code = "fail_closed"
        result.self_consumption_filter_reason_code = "filtered_plan_not_feasible"
        result.self_consumption_filter_ui_reason = "valuation_unavailable"

    conservative_feasible, conservative_ending, _ = _simulate(
        starts,
        settings,
        load_by_slot,
        exports,
        result.base_reserve_energy_kwh,
        export_reserve_by_slot,
        conservative_pv,
        slot_fractions,
    )
    if not conservative_feasible:
        # The R09 filter may only reduce an already feasible legacy export.
        # Preserve fail-closed behaviour if that invariant is ever violated.
        result.planned_exports = []
        exports = {}
        selected_trace.clear()
        _, expected_ending, natural_exports = _simulate(
            starts,
            settings,
            load_by_slot,
            {},
            result.base_reserve_energy_kwh,
            export_reserve_by_slot,
            expected_pv,
            slot_fractions,
            trace_collector=selected_trace,
        )
        _, conservative_ending, _ = _simulate(
            starts,
            settings,
            load_by_slot,
            {},
            result.base_reserve_energy_kwh,
            export_reserve_by_slot,
            conservative_pv,
            slot_fractions,
        )
        result.self_consumption_filter_status_code = "fail_closed"
        result.self_consumption_filter_reason_code = "filtered_plan_not_feasible"
        result.self_consumption_filter_ui_reason = "valuation_unavailable"

    result.timeline_trace = _rce_timeline_trace(
        settings=settings,
        selected=selected_trace,
        baseline=baseline_trace,
        exports=exports,
        price_by_start=price_by_start,
        floor_kwh=result.base_reserve_energy_kwh,
        export_reserve_by_slot=export_reserve_by_slot,
    )
    result.natural_export_kwh = sum(natural_exports.values())
    result.natural_revenue_pln = _market_revenue(
        {}, natural_exports, price_by_start
    )
    result.ending_battery_kwh = conservative_ending
    export_efficiency = max(
        min(settings.export_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    (
        result.net_objective_pln,
        result.battery_wear_cost_pln,
        result.terminal_energy_value_pln,
    ) = _economic_objective(
        exports=exports,
        natural_exports=natural_exports,
        price_by_start=price_by_start,
        ending_battery_kwh=expected_ending,
        floor_kwh=result.base_reserve_energy_kwh,
        export_efficiency=export_efficiency,
        battery_wear_cost_pln_kwh=settings.battery_wear_cost_pln_kwh,
        terminal_energy_target_kwh=result.terminal_energy_target_kwh,
        terminal_energy_value_pln_kwh=result.terminal_energy_value_pln_kwh,
    )


def _filter_current_execution(
    settings: OptimizerInput,
    result: OptimizerResult,
) -> None:
    """Reduce current execution without creating a larger 4306 command."""

    legacy_energy = 0.0
    filtered_energy = 0.0
    current_start = floor_half_hour(settings.now).astimezone(dt_timezone.utc)
    for item in result.legacy_planned_exports:
        if item.start.astimezone(dt_timezone.utc) == current_start:
            legacy_energy = item.energy_kwh
            break
    for item in result.planned_exports:
        if item.start.astimezone(dt_timezone.utc) == current_start:
            filtered_energy = item.energy_kwh
            break

    legacy_percent = result.current_slot_execution_power_percent
    legacy_discharge = result.current_slot_execution_discharge_power_kw
    legacy_export_power = result.current_slot_execution_export_power_kw
    legacy_start_eligible = result.current_slot_start_eligible
    legacy_suppression = result.current_slot_suppression_reason

    result.current_slot_planned_export_kwh = filtered_energy
    result.current_slot_execution_power_percent = 0.0
    result.current_slot_execution_discharge_power_kw = 0.0
    result.current_slot_execution_export_power_kw = 0.0
    result.current_slot_start_eligible = False
    result.current_run_end = None

    if filtered_energy >= 0.01 and legacy_energy >= 0.01:
        remaining_hours = result.current_slot_remaining_minutes / 60.0
        current_system_energy = (
            _inverter_ac_power_kw(settings)
            * settings.inverter_count
            * remaining_hours
        )
        pv_to_load = min(
            result.current_slot_pv_kwh,
            result.current_slot_load_kwh,
            current_system_energy,
        )
        load_deficit_power = (
            max(result.current_slot_load_kwh - pv_to_load, 0.0)
            / max(remaining_hours, 1e-9)
        )
        desired_export_power = filtered_energy / max(remaining_hours, 1e-9)
        filtered_percent = _quantize_4306_percent(
            (desired_export_power + load_deficit_power)
            / max(result.system_power_kw, 0.001)
            * 100.0
        )
        filtered_percent = min(filtered_percent, legacy_percent)
        result.current_slot_execution_power_percent = filtered_percent
        result.current_slot_execution_discharge_power_kw = min(
            legacy_discharge,
            result.system_power_kw * filtered_percent / 100.0,
        )
        result.current_slot_execution_export_power_kw = min(
            desired_export_power,
            max(
                result.current_slot_execution_discharge_power_kw
                - load_deficit_power,
                0.0,
            ),
            legacy_export_power,
        )
        cursor = current_start
        filtered_by_start = {
            item.start.astimezone(dt_timezone.utc): item.energy_kwh
            for item in result.planned_exports
        }
        while filtered_by_start.get(cursor, 0.0) >= 0.01:
            cursor += SLOT
        result.current_run_end = cursor.astimezone(settings.now.tzinfo)
        if filtered_percent > 0.0:
            result.current_slot_start_eligible = legacy_start_eligible
            result.current_slot_suppression_reason = legacy_suppression
        else:
            result.current_slot_suppression_reason = "execution_power_unavailable"
    else:
        result.current_slot_suppression_reason = "no_current_plan"

    result.current_slot_load_exhausts_requested_discharge_budget = (
        _current_slot_load_exhausts_requested_discharge_budget(settings, result)
    )


def _apply_self_consumption_filter(
    settings: OptimizerInput,
    result: OptimizerResult,
) -> OptimizerResult:
    """Apply R09 solely as a reduction of the legacy RCE export candidate."""

    result.legacy_planned_exports = tuple(result.planned_exports)
    if not settings.self_consumption_filter_enabled:
        result.self_consumption_filter_status_code = "inactive"
        result.self_consumption_filter_reason_code = "filter_not_enabled"
        result.self_consumption_filter_ui_reason = "sell_surplus"
        return result
    shadow = result.self_consumption_shadow
    if not result.ready:
        result.self_consumption_filter_status_code = "inactive"
        result.self_consumption_filter_reason_code = "legacy_plan_not_ready"
        result.self_consumption_filter_ui_reason = "valuation_unavailable"
        return result

    result.self_consumption_filter_active = True
    selected_by_start: dict[datetime, float] = {}
    if shadow is None or not shadow.available or shadow.selected is None:
        result.self_consumption_filter_applied = bool(result.planned_exports)
        result.self_consumption_filter_status_code = "blocked"
        result.self_consumption_filter_reason_code = (
            getattr(shadow, "reason_code", None) or "valuation_unavailable"
        )
        result.self_consumption_filter_ui_reason = "valuation_unavailable"
    else:
        trace = result.timeline_trace
        points = tuple(trace.points) if trace is not None else ()
        selected = tuple(shadow.selected.slot_exports_kwh_ac)
        if len(points) != len(selected):
            result.self_consumption_filter_applied = bool(result.planned_exports)
            result.self_consumption_filter_status_code = "blocked"
            result.self_consumption_filter_reason_code = "valuation_horizon_mismatch"
            result.self_consumption_filter_ui_reason = "valuation_unavailable"
        else:
            for point, energy in zip(points, selected, strict=True):
                if energy >= 0.01:
                    selected_by_start[
                        (point.end - SLOT).astimezone(dt_timezone.utc)
                    ] = float(energy)
            result.self_consumption_filter_applied = True
            result.self_consumption_filter_status_code = "applied"
            result.self_consumption_filter_reason_code = shadow.reason_code
            result.self_consumption_filter_ui_reason = (
                "preserve_home"
                if shadow.status_code == "preserve_home"
                else "sell_surplus"
            )

    filtered: list[PlannedExport] = []
    for legacy in result.legacy_planned_exports:
        start_utc = legacy.start.astimezone(dt_timezone.utc)
        energy = min(
            legacy.energy_kwh,
            max(selected_by_start.get(start_utc, 0.0), 0.0),
        )
        # A sub-command residue must not create a new execution cycle.
        if energy < 0.01:
            continue
        filtered.append(replace(legacy, energy_kwh=energy))

    legacy_total = result.legacy_planned_export_kwh
    result.planned_exports = filtered
    result.self_consumption_filter_reduced = (
        result.planned_export_kwh < legacy_total - 1e-6
    )
    export_map = {
        item.start.astimezone(dt_timezone.utc): item.energy_kwh
        for item in result.planned_exports
    }
    _filtered_timeline(settings, result, export_map)
    _filter_current_execution(settings, result)
    if result.legacy_planned_exports and not result.planned_exports:
        result.status_code = "home_protected"
    return result


def _shadow_variant_from_shared_physics(
    settings: OptimizerInput,
    result: OptimizerResult,
    slots: tuple[ShadowSlot, ...],
    points: tuple[TimelineTracePoint, ...],
    export_scale: float,
) -> ShadowVariant:
    """Build one complete shadow trajectory with the planner's slot physics."""

    physical_constants = _physical_constants(settings)
    export_efficiency = physical_constants.export_efficiency
    charge_efficiency = physical_constants.charge_efficiency
    house_efficiency = physical_constants.house_efficiency
    battery = (
        settings.battery_capacity_kwh * settings.battery_soc_percent / 100.0
    )
    initial = battery
    baseline_export = sum(slot.baseline_export_kwh_ac for slot in slots)
    forced_export = 0.0
    natural_export = 0.0
    export_revenue = 0.0
    natural_export_revenue = 0.0
    grid_import = 0.0
    grid_import_cost = 0.0
    battery_to_load = 0.0
    avoided_import_value = 0.0
    throughput = 0.0
    charged_dc = 0.0
    maximum_balance_error = 0.0
    feasible = len(slots) == len(points)
    slot_exports: list[float] = []
    slot_imports: list[float] = []
    slot_natural_exports: list[float] = []
    slot_battery_after: list[float] = []

    for slot, point in zip(slots, points, strict=True):
        before = battery
        desired_export = max(slot.baseline_export_kwh_ac * export_scale, 0.0)
        fraction = min(
            max(
                (point.end - point.start).total_seconds()
                / SLOT.total_seconds(),
                0.0,
            ),
            1.0,
        )
        hard_floor = (
            result.base_reserve_energy_kwh
            if slot.hard_floor_kwh_dc is None
            else slot.hard_floor_kwh_dc
        )
        physical = _simulate_physical_slot(
            settings,
            battery_kwh_dc=battery,
            load_kwh_ac=slot.load_kwh_ac,
            pv_kwh_ac=slot.pv_kwh_ac,
            controlled_export_kwh_ac=desired_export,
            hard_floor_kwh_dc=hard_floor,
            export_floor_kwh_dc=slot.protected_floor_kwh_dc,
            slot_fraction=fraction,
            physical_constants=physical_constants,
        )
        battery = physical.battery_after_kwh_dc
        feasible = feasible and physical.feasible
        slot_exports.append(physical.controlled_export_kwh_ac)
        slot_imports.append(physical.grid_import_kwh_ac)
        slot_natural_exports.append(physical.natural_export_kwh_ac)
        slot_battery_after.append(battery)

        forced_export += physical.controlled_export_kwh_ac
        natural_export += physical.natural_export_kwh_ac
        if (
            physical.natural_export_kwh_ac > 1e-6
            and slot.sell_price_pln_kwh_ac is None
        ):
            feasible = False
        export_revenue += (
            physical.controlled_export_kwh_ac
            * float(slot.sell_price_pln_kwh_ac or 0.0)
        )
        natural_export_revenue += (
            physical.natural_export_kwh_ac
            * float(slot.sell_price_pln_kwh_ac or 0.0)
        )
        grid_import += physical.grid_import_kwh_ac
        import_price = float(slot.import_price_pln_kwh_ac or 0.0)
        grid_import_cost += physical.grid_import_kwh_ac * import_price
        battery_to_load += physical.delivered_to_load_kwh_ac
        avoided_import_value += (
            physical.delivered_to_load_kwh_ac * import_price
        )
        discharged_dc = (
            physical.delivered_to_load_kwh_ac / house_efficiency
            + physical.controlled_export_kwh_ac / export_efficiency
        )
        slot_charged_dc = physical.charge_input_kwh_ac * charge_efficiency
        throughput += discharged_dc
        charged_dc += slot_charged_dc
        expected_after = before - discharged_dc + slot_charged_dc
        maximum_balance_error = max(
            maximum_balance_error,
            abs(battery - expected_after),
            abs(physical.balance_error_kwh),
        )

    final_hard_floor = (
        (
            result.base_reserve_energy_kwh
            if slots[-1].hard_floor_kwh_dc is None
            else slots[-1].hard_floor_kwh_dc
        )
        if slots
        else result.base_reserve_energy_kwh
    )
    terminal_eligible = min(
        max(battery - final_hard_floor, 0.0),
        max(result.terminal_energy_target_kwh, 0.0),
    )
    terminal_value = terminal_eligible * max(
        result.terminal_energy_value_pln_kwh,
        0.0,
    )
    wear = throughput * settings.battery_wear_cost_pln_kwh
    objective = (
        export_revenue
        + natural_export_revenue
        - grid_import_cost
        - wear
        + terminal_value
    )
    trajectory_balance_error = battery - (initial + charged_dc - throughput)
    maximum_balance_error = max(
        maximum_balance_error,
        abs(trajectory_balance_error),
    )
    feasible = feasible and maximum_balance_error <= 1e-6
    return ShadowVariant(
        export_scale=export_scale,
        feasible=feasible,
        forced_export_kwh_ac=forced_export,
        preserved_export_kwh_ac=max(baseline_export - forced_export, 0.0),
        export_revenue_pln=export_revenue,
        natural_export_revenue_pln=natural_export_revenue,
        grid_import_kwh_ac=grid_import,
        grid_import_cost_pln=grid_import_cost,
        battery_to_load_kwh_ac=battery_to_load,
        avoided_import_value_pln=avoided_import_value,
        battery_throughput_kwh_dc=throughput,
        battery_wear_cost_pln=wear,
        ending_battery_kwh_dc=battery,
        terminal_energy_kwh_dc=terminal_eligible,
        terminal_value_pln=terminal_value,
        raw_objective_pln=objective,
        balance_error_kwh=maximum_balance_error,
        slot_exports_kwh_ac=tuple(slot_exports),
        natural_export_kwh_ac=natural_export,
        slot_grid_import_kwh_ac=tuple(slot_imports),
        slot_natural_exports_kwh_ac=tuple(slot_natural_exports),
        slot_battery_after_kwh_dc=tuple(slot_battery_after),
    )


def _attach_self_consumption_shadow(
    settings: OptimizerInput,
    result: OptimizerResult,
) -> OptimizerResult:
    """Evaluate R08 on the accepted legacy trace without changing that plan."""

    schedule = settings.tariff_price_schedule
    source_id = getattr(schedule, "source_id", None)
    source_revision = getattr(schedule, "source_revision", None)
    quality = getattr(schedule, "quality", None)
    trace = result.timeline_trace
    if not result.ready:
        result.self_consumption_shadow = unavailable_shadow_evaluation(
            "legacy_plan_not_ready",
            price_source_id=source_id,
            price_source_revision=source_revision,
            price_quality=quality,
        )
        return _apply_self_consumption_filter(settings, result)
    if trace is None or not trace.points:
        result.self_consumption_shadow = unavailable_shadow_evaluation(
            "legacy_trace_missing",
            price_source_id=source_id,
            price_source_revision=source_revision,
            price_quality=quality,
        )
        return _apply_self_consumption_filter(settings, result)
    if quality not in {"official_verified", "cached_verified"}:
        result.self_consumption_shadow = unavailable_shadow_evaluation(
            "tariff_price_not_verified",
            price_source_id=source_id,
            price_source_revision=source_revision,
            price_quality=quality,
        )
        return _apply_self_consumption_filter(settings, result)
    if getattr(schedule, "coverage_complete", False) is not True:
        result.self_consumption_shadow = unavailable_shadow_evaluation(
            "tariff_price_coverage_incomplete",
            price_source_id=source_id,
            price_source_revision=source_revision,
            price_quality=quality,
        )
        return _apply_self_consumption_filter(settings, result)

    slots: list[ShadowSlot] = []
    for point in trace.points:
        policy = point.policy
        if not isinstance(policy, RCEPolicyPoint):
            result.self_consumption_shadow = unavailable_shadow_evaluation(
                "legacy_trace_policy_invalid",
                price_source_id=source_id,
                price_source_revision=source_revision,
                price_quality=quality,
            )
            return _apply_self_consumption_filter(settings, result)
        import_price = schedule.price_at(point.start)
        floor_percent = point.protected_soc_floor_percent
        slots.append(
            ShadowSlot(
                start=point.start,
                end=point.end,
                load_kwh_ac=float(point.load_kwh or 0.0),
                pv_kwh_ac=float(point.pv_kwh or 0.0),
                baseline_export_kwh_ac=max(
                    float(policy.planned_export_kwh), 0.0
                ),
                sell_price_pln_kwh_ac=policy.sell_price_pln_kwh,
                import_price_pln_kwh_ac=import_price,
                protected_floor_kwh_dc=(
                    settings.battery_capacity_kwh
                    * float(floor_percent or 0.0)
                    / 100.0
                ),
                hard_floor_kwh_dc=result.base_reserve_energy_kwh,
            )
        )
    points = tuple(trace.points)
    result.self_consumption_shadow = evaluate_sale_vs_preserve(
        slots,
        initial_battery_kwh_dc=(
            settings.battery_capacity_kwh * settings.battery_soc_percent / 100.0
        ),
        battery_capacity_kwh_dc=settings.battery_capacity_kwh,
        export_efficiency_percent=settings.export_efficiency_percent,
        charge_efficiency_percent=settings.charge_efficiency_percent,
        house_discharge_efficiency_percent=(
            settings.house_discharge_efficiency_percent
        ),
        battery_wear_cost_pln_kwh_dc=settings.battery_wear_cost_pln_kwh,
        terminal_target_kwh_dc=result.terminal_energy_target_kwh,
        terminal_value_pln_kwh_dc=result.terminal_energy_value_pln_kwh,
        price_source_id=source_id,
        price_source_revision=source_revision,
        price_quality=quality,
        variant_simulator=lambda export_scale: _shadow_variant_from_shared_physics(
            settings,
            result,
            tuple(slots),
            points,
            export_scale,
        ),
    )
    return _apply_self_consumption_filter(settings, result)


RCE_REVALIDATION_LIVE_FIELDS = frozenset({
    "now", "battery_soc_percent", "battery_voltage_v",
    "bms_max_discharge_current_a", "bms_max_charge_current_a",
    "bms_discharge_data_fresh", "bms_discharge_data_age_seconds",
    "bms_discharge_data_available", "bms_charge_data_fresh",
    "bms_charge_data_age_seconds", "bms_charge_data_available",
    "actual_day_load_today_kwh", "actual_day_load_observed_at",
    "persistence_delta_kw", "persistence_observed_at", "pv_to_load_power_kw",
    "current_load_power_kw", "current_pv_power_kw", "current_battery_soc_fresh",
    "export_power_cap_kw", "effective_export_power_kw",
})


def _revalidation_live_inputs_ready(
    settings: OptimizerInput, *, allow_zero_authority: bool = False,
    allow_profile_fallback: bool = False,
) -> bool:
    """Validate supplied sample freshness; the HA adapter owns provenance."""
    def numeric(value: Any) -> bool:
        return (
            isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value)
        )

    if (
        settings.current_battery_soc_fresh is not True
        or settings.bms_discharge_data_fresh is not True
        or not (
            settings.bms_discharge_data_available is True
            or (
                allow_zero_authority
                and settings.bms_discharge_data_available is False
                and numeric(settings.bms_max_discharge_current_a)
                and settings.bms_max_discharge_current_a == 0.0
            )
        )
        or not numeric(settings.battery_soc_percent)
        or not 0.0 <= settings.battery_soc_percent <= 100.0
        or not numeric(settings.bms_discharge_data_age_seconds)
        or not -5.0 <= settings.bms_discharge_data_age_seconds <= 300.0
        or not numeric(settings.battery_voltage_v) or settings.battery_voltage_v <= 0.0
        or not numeric(settings.bms_max_discharge_current_a)
        or settings.bms_max_discharge_current_a < 0.0
        or (not allow_zero_authority and settings.bms_max_discharge_current_a == 0.0)
        or any(
            not (allow_profile_fallback and value is None)
            and (not numeric(value) or value < 0.0)
            for value in (settings.current_load_power_kw, settings.current_pv_power_kw)
        )
    ):
        return False
    for value in (settings.export_power_cap_kw, settings.effective_export_power_kw):
        if value is not None and (
            not numeric(value) or value < 0.0
            or (not allow_zero_authority and value == 0.0)
        ):
            return False
    for value in (
        settings.bms_max_charge_current_a, settings.actual_day_load_today_kwh,
        settings.pv_to_load_power_kw,
    ):
        if value is not None and (not numeric(value) or value < 0.0):
            return False
    if not numeric(settings.persistence_delta_kw):
        return False
    if settings.bms_charge_data_available:
        if (
            settings.bms_charge_data_fresh is not True
            or not numeric(settings.bms_max_charge_current_a)
            or settings.bms_max_charge_current_a < 0.0
            or not numeric(settings.bms_charge_data_age_seconds)
            or not -5.0 <= settings.bms_charge_data_age_seconds <= 300.0
        ):
            return False
    return True


def _finish_fixed_schedule(
    settings: OptimizerInput, result: OptimizerResult,
) -> OptimizerResult:
    """The fixed candidate already passed R09; do not rerun its search."""
    result.solver_method = "fixed_schedule_revalidation"
    result.optimality_verified = False
    result.legacy_planned_exports = tuple(result.planned_exports)
    result.self_consumption_filter_active = settings.self_consumption_filter_enabled
    result.self_consumption_filter_applied = settings.self_consumption_filter_enabled
    result.self_consumption_filter_status_code = (
        "applied" if settings.self_consumption_filter_enabled else "inactive"
    )
    result.self_consumption_filter_reason_code = "accepted_selection_revalidated"
    result.self_consumption_filter_ui_reason = (
        "sell_surplus" if result.planned_exports else "preserve_home"
    )
    schedule = settings.tariff_price_schedule
    result.self_consumption_shadow = unavailable_shadow_evaluation(
        "revalidation_without_shadow_search",
        price_source_id=getattr(schedule, "source_id", None),
        price_source_revision=getattr(schedule, "source_revision", None),
        price_quality=getattr(schedule, "quality", None),
    )
    return result


def _fixed_schedule_is_economic(
    settings: OptimizerInput, result: OptimizerResult,
) -> bool:
    """Check this selection against fresh self-use, without searching scales."""
    if result.net_objective_pln < result.baseline_net_objective_pln - EXACT_TIE_ROUNDOFF_PLN:
        return False
    if not settings.self_consumption_filter_enabled or not result.planned_exports:
        return True
    schedule = settings.tariff_price_schedule
    if (
        getattr(schedule, "quality", None) not in {"official_verified", "cached_verified"}
        or getattr(schedule, "coverage_complete", False) is not True
        or result.timeline_trace is None
    ):
        return False
    points = tuple(result.timeline_trace.points)
    slots: list[ShadowSlot] = []
    for point in points:
        if not isinstance(point.policy, RCEPolicyPoint):
            return False
        price = schedule.price_at(point.start)
        if price is None or not math.isfinite(price):
            return False
        slots.append(ShadowSlot(
            start=point.start, end=point.end,
            load_kwh_ac=float(point.load_kwh or 0.0),
            pv_kwh_ac=float(point.pv_kwh or 0.0),
            baseline_export_kwh_ac=max(float(point.policy.planned_export_kwh), 0.0),
            sell_price_pln_kwh_ac=point.policy.sell_price_pln_kwh,
            import_price_pln_kwh_ac=price,
            protected_floor_kwh_dc=(settings.battery_capacity_kwh
                * float(point.protected_soc_floor_percent or 0.0) / 100.0),
            hard_floor_kwh_dc=result.base_reserve_energy_kwh,
        ))
    selected = _shadow_variant_from_shared_physics(settings, result, tuple(slots), points, 1.0)
    self_use = _shadow_variant_from_shared_physics(settings, result, tuple(slots), points, 0.0)
    return bool(
        selected.feasible and self_use.feasible
        and selected.raw_objective_pln >= self_use.raw_objective_pln - EXACT_TIE_ROUNDOFF_PLN
    )


def _rce_result_has_no_execution(result: OptimizerResult) -> bool:
    """A diagnostic-only plan cannot retain or create a discharge request."""
    return bool(
        not result.planned_exports
        and result.current_slot_start_eligible is False
        and result.current_run_end is None
        and all(value == 0.0 for value in (
            result.current_slot_planned_export_kwh,
            result.current_slot_execution_export_power_kw,
            result.current_slot_execution_discharge_power_kw,
            result.current_slot_execution_power_percent,
        ))
    )


def _bounded_revalidation_result(result: OptimizerResult) -> OptimizerResult:
    """Keep the full private economic pass out of the 48-hour UI contract."""
    trace = result.timeline_trace
    if trace is not None and trace.points:
        end = trace.points[0].start + timedelta(hours=48)
        result.timeline_trace = replace(
            trace, points=tuple(point for point in trace.points if point.end <= end),
        )
    return result


def revalidate_rce_plan(
    settings_latest: OptimizerInput,
    captured_result: OptimizerResult,
    *,
    captured_settings: OptimizerInput,
    diagnostics: dict[str, Any] | None = None,
) -> OptimizerResult | None:
    """Re-attest only a captured selection using fresh, bounded slot physics.

    Entry/device identity, raw sample timestamps and coherent GCF readback are
    owned by the HA adapter. This function grants no execution/lease authority.
    It never searches, adds a trade, raises current power, or extends a run.
    """
    def reject(reason: str) -> None:
        if diagnostics is not None:
            diagnostics["reason"] = reason
        return None

    if diagnostics is not None:
        diagnostics.clear()
    try:
        negative_shortage = bool(
            captured_result.ready is False
            and captured_result.status_code == "home_energy_shortage"
            and _rce_result_has_no_execution(captured_result)
        )
        if captured_result.ready is not True and not negative_shortage:
            return reject("captured_result_not_ready")
        before, after = captured_settings.now, settings_latest.now
        if any(value.tzinfo is None or value.utcoffset() is None for value in (before, after)):
            return reject("invalid_clock")
        before_utc, after_utc = before.astimezone(dt_timezone.utc), after.astimezone(dt_timezone.utc)
        if (
            not 0.0 <= (after_utc - before_utc).total_seconds() <= 120.0
            or floor_half_hour(before_utc) != floor_half_hour(after_utc)
        ):
            return reject("elapsed_or_slot_boundary")
        if any(
            (getattr(settings_latest, name) is None)
            != (getattr(captured_settings, name) is None)
            for name in ("current_load_power_kw", "current_pv_power_kw")
        ):
            return reject("live_source_availability_changed")
        if _rce_result_has_no_execution(captured_result):
            # Fresh zero-authority outcomes (including BMS/cap=0) remain
            # current while telemetry changes. Immutable intent stays exact.
            if (
                not _revalidation_live_inputs_ready(settings_latest, allow_zero_authority=True, allow_profile_fallback=True)
                or (negative_shortage and not _revalidation_live_inputs_ready(
                    captured_settings, allow_zero_authority=True, allow_profile_fallback=True,
                ))
                or any(
                    getattr(settings_latest, item.name) != getattr(captured_settings, item.name)
                    for item in fields(OptimizerInput)
                    if item.name not in RCE_REVALIDATION_LIVE_FIELDS
                )
            ):
                return reject("inactive_inputs_or_intent_changed")
            inactive = _optimize_rce_impl(
                deepcopy(settings_latest), fixed_exports={},
                fixed_current_discharge_cap_kw=0.0,
            )
            return (
                _bounded_revalidation_result(inactive)
                if (
                    _rce_result_has_no_execution(inactive)
                    and (
                        inactive.ready is True
                        or (negative_shortage and inactive.ready is False
                            and inactive.status_code == "home_energy_shortage")
                    )
                )
                else None
            )
        if (
            not _revalidation_live_inputs_ready(settings_latest, allow_profile_fallback=True)
            or not _revalidation_live_inputs_ready(captured_settings, allow_profile_fallback=True)
        ):
            return reject("live_inputs_not_ready")
        if any(
            getattr(settings_latest, item.name) != getattr(captured_settings, item.name)
            for item in fields(OptimizerInput)
            if item.name not in RCE_REVALIDATION_LIVE_FIELDS
        ):
            if diagnostics is not None:
                diagnostics["changed_fields"] = [
                    item.name for item in fields(OptimizerInput)
                    if item.name not in RCE_REVALIDATION_LIVE_FIELDS
                    and getattr(settings_latest, item.name) != getattr(captured_settings, item.name)
                ]
            return reject("immutable_input_changed")
        if settings_latest.self_consumption_filter_enabled and captured_result.planned_exports and (
            captured_result.self_consumption_filter_active is not True
            or captured_result.self_consumption_filter_status_code != "applied"
        ):
            return reject("captured_filter_not_applied")
        # The caller's settings/maps/result remain owned by the caller.
        settings = deepcopy(settings_latest)
        selected: dict[datetime, float] = {}
        price_map = {row.start.astimezone(dt_timezone.utc): row for row in settings.price_slots}
        for item in captured_result.planned_exports:
            start = item.start.astimezone(dt_timezone.utc)
            row = price_map.get(start)
            if (
                start in selected or row is None or row.blocked
                or not math.isfinite(item.energy_kwh) or item.energy_kwh < 0.0
                or item.price_pln_kwh != row.price_pln_kwh
            ):
                return reject("captured_selection_invalid")
            # The fixed branch reduces the current slot directly from the
            # captured integer command and remaining hours. A second floating
            # time-ratio bound can spuriously subtract a whole command step.
            selected[start] = item.energy_kwh
        current_power_cap = captured_result.current_slot_execution_discharge_power_kw
        if not math.isfinite(current_power_cap) or current_power_cap < 0.0:
            return reject("captured_power_invalid")
        result = _optimize_rce_impl(
            settings, fixed_exports=selected,
            fixed_current_discharge_cap_kw=current_power_cap,
        )
        if result.ready is not True or result.status_code in {"optimizer_error", "missing_data", "home_energy_shortage"}:
            if diagnostics is not None:
                diagnostics["fixed_status"] = result.status_code
            return reject("fixed_schedule_not_ready")
        if result.current_slot_execution_discharge_power_kw > current_power_cap + 1e-9:
            return reject("current_power_increased")
        if not _fixed_schedule_is_economic(settings, result):
            return reject("fixed_schedule_uneconomic")
        original = {item.start.astimezone(dt_timezone.utc): item.energy_kwh for item in captured_result.planned_exports}
        if any(item.energy_kwh > original.get(item.start.astimezone(dt_timezone.utc), 0.0) + 1e-9 for item in result.planned_exports):
            return reject("selection_increased")
        if result.current_run_end is not None and (
            captured_result.current_run_end is None
            or result.current_run_end > captured_result.current_run_end
        ):
            return reject("run_extended")
        return _bounded_revalidation_result(result)
    except (AttributeError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return reject("invalid_revalidation_input")


def optimize_rce(settings: OptimizerInput) -> OptimizerResult:
    """Return the most valuable feasible RCE export plan."""
    return _optimize_rce_impl(settings)


def retain_active_rce_slot(
    settings: OptimizerInput,
    result: OptimizerResult,
    *,
    accepted_settings: OptimizerInput | None,
    accepted_result: OptimizerResult | None,
    commitment: RceActiveCommitment | None,
) -> OptimizerResult:
    """Keep an already accepted half-hour on fresh physical/economic evidence.

    This is the user's active-block policy, not an optimality assertion. Only
    the current slot may be restored; future exports may only shrink. The
    Supervisor still owns permission, writes, FC03 verification and the lease.
    All work is bounded fixed-schedule simulation, without a second search.
    """
    try:
        if (
            not isinstance(commitment, RceActiveCommitment)
            or accepted_settings is None or accepted_result is None
            or not result.ready or not accepted_result.ready
            or result.current_slot_planned_export_kwh >= 0.01
            or accepted_result.current_slot_planned_export_kwh < 0.01
            or accepted_result.current_run_end is None
            or not _revalidation_live_inputs_ready(settings, allow_profile_fallback=True)
        ):
            return result
        now = settings.now
        start = floor_half_hour(now).astimezone(dt_timezone.utc)
        end = start + SLOT
        if (
            floor_half_hour(accepted_settings.now).astimezone(dt_timezone.utc) != start
            or not commitment.started_at <= now < min(end, commitment.hard_deadline,
                                                       accepted_result.current_run_end)
            or not 0.0 <= (now - commitment.physical_verified_at).total_seconds() <= 25.0
            or settings.battery_soc_percent <= max(commitment.minimum_soc_percent,
                                                     result.minimum_soc_percent)
            or post_command_settling_market_fingerprint(settings) is None
            or post_command_settling_market_fingerprint(settings)
            != post_command_settling_market_fingerprint(accepted_settings)
        ):
            return result
        rows = [row for row in settings.price_slots
                if row.start.astimezone(dt_timezone.utc) == start]
        if (len(rows) != 1 or rows[0].blocked
            or rows[0].price_pln_kwh * settings.export_efficiency_percent / 100.0
            <= settings.battery_wear_cost_pln_kwh):
            return result
        percent = commitment.maximum_discharge_power_percent
        if (isinstance(percent, bool) or not math.isfinite(percent)
            or not 0.0 < percent <= 100.0):
            return result
        power_cap = min(
            settings.inverter_power_kw * settings.inverter_count * percent / 100.0,
            accepted_result.current_slot_execution_discharge_power_kw,
        )
        if not math.isfinite(power_cap) or power_cap <= 0.0:
            return result
        # The incumbent's remaining energy is bounded again by fresh LOAD/PV,
        # BMS, GCF, SOC quantization, home/night reserve and integer 4306.
        current_energy = min(accepted_result.current_slot_planned_export_kwh,
                             power_cap * (end - now).total_seconds() / 3600.0)
        future = {item.start.astimezone(dt_timezone.utc): item.energy_kwh
                  for item in result.planned_exports
                  if item.start.astimezone(dt_timezone.utc) > start}

        def trial(scale: float) -> OptimizerResult:
            exports = {stamp: energy * scale for stamp, energy in future.items()
                       if energy * scale >= 0.01}
            exports[start] = current_energy
            return _optimize_rce_impl(settings, fixed_exports=exports,
                                      fixed_current_discharge_cap_kw=power_cap)

        def feasible(value: OptimizerResult) -> bool:
            return bool(value.ready and value.status_code not in
                        {'optimizer_error', 'missing_data', 'home_energy_shortage'}
                        and value.current_slot_planned_export_kwh >= 0.01
                        and 0 < value.current_slot_execution_discharge_power_kw <= power_cap
                        and value.current_required_minimum_soc_percent
                        >= commitment.minimum_soc_percent)

        selected = trial(1.0)
        if not feasible(selected):
            selected = trial(0.0)
            if not feasible(selected):
                return result
            low, high = 0.0, 1.0
            # Restore only the largest feasible fraction of the new future
            # plan. Never add a future slot or reuse an old forecast/reserve.
            for _ in range(12):
                middle = (low + high) / 2.0
                candidate = trial(middle)
                if feasible(candidate):
                    low, selected = middle, candidate
                else:
                    high = middle
        if not _fixed_schedule_is_economic(settings, selected):
            return result
        selected.current_run_end = min(selected.current_run_end,
                                       accepted_result.current_run_end,
                                       commitment.hard_deadline)
        selected.active_slot_commitment_applied = True
        selected.solver_method = 'active_slot_fixed_schedule'
        selected.optimality_verified = False
        return _bounded_revalidation_result(selected)
    except (AttributeError, TypeError, ValueError, OverflowError, ZeroDivisionError):
        return result


def _optimize_rce_impl(
    settings: OptimizerInput,
    *,
    fixed_exports: Mapping[datetime, float] | None = None,
    fixed_current_discharge_cap_kw: float | None = None,
) -> OptimizerResult:
    """Share preparation/finalization with a fixed-selection feasibility pass."""
    finish = _attach_self_consumption_shadow if fixed_exports is None else _finish_fixed_schedule
    capacity = settings.battery_capacity_kwh
    if (
        capacity <= 0
        or settings.average_daily_load_kwh < 0
        or settings.inverter_power_kw <= 0
        or not math.isfinite(settings.inverter_power_kw)
        or _inverter_ac_power_kw(settings) <= 0
        or not math.isfinite(_inverter_ac_power_kw(settings))
        or settings.inverter_count <= 0
    ):
        return OptimizerResult(
            ready=False,
            status_code="missing_data",
            minimum_soc_percent=100,
            base_reserve_energy_kwh=0.0,
            protected_night_energy_kwh=0.0,
            additional_forecast_reserve_kwh=0.0,
            protected_home_energy_kwh=0.0,
            available_energy_now_kwh=0.0,
        )

    # Keep malformed/duplicated market rows from changing either solver
    # complexity or economics.  ``replace`` preserves the caller's input for
    # diagnostics and makes every downstream helper consume the same scope.
    settings = replace(
        settings,
        price_slots=_supported_price_slots(settings),
    )

    now_slot_local = floor_half_hour(settings.now)
    now_slot = now_slot_local.astimezone(dt_timezone.utc)
    horizon_end_local = _horizon_end(settings)
    horizon_end = horizon_end_local.astimezone(dt_timezone.utc)
    starts: list[datetime] = []
    cursor = now_slot
    while cursor < horizon_end:
        starts.append(cursor)
        cursor += SLOT
    first_fraction = _current_slot_fraction(settings.now)
    slot_fractions = {start: 1.0 for start in starts}
    if starts:
        slot_fractions[starts[0]] = first_fraction

    (
        historical_day_load,
        live_projected_day_load,
        modeled_day_load,
        daylight_progress,
    ) = _day_load_projection(settings)
    load_by_slot, load_profile_mode = _load_by_slot(
        settings,
        starts,
        historical_day_energy=historical_day_load,
        live_projected_day_energy=live_projected_day_load,
        modeled_day_energy=modeled_day_load,
        daylight_progress=daylight_progress,
    )
    if starts:
        load_by_slot[starts[0]] *= first_fraction
    current_slot_load_source = "profile"
    if (
        starts
        and settings.current_load_power_kw is not None
        and math.isfinite(settings.current_load_power_kw)
        and settings.current_load_power_kw >= 0.0
    ):
        load_by_slot[starts[0]] = (
            settings.current_load_power_kw * 0.5 * first_fraction
        )
        current_slot_load_source = "live"

    expected_pv_source = _utc_energy_map(settings.pv_by_slot_kwh)
    expected_pv = {
        start: max(float(expected_pv_source.get(start, 0.0)), 0.0)
        for start in starts
    }
    if starts:
        expected_pv[starts[0]] *= first_fraction
    current_slot_pv_source = "forecast"
    if (
        starts
        and settings.current_pv_power_kw is not None
        and math.isfinite(settings.current_pv_power_kw)
        and settings.current_pv_power_kw >= 0.0
    ):
        expected_pv[starts[0]] = (
            settings.current_pv_power_kw * 0.5 * first_fraction
        )
        current_slot_pv_source = "live"

    conservative_source = (
        settings.conservative_pv_by_slot_kwh
        if settings.conservative_pv_by_slot_kwh is not None
        else settings.pv_by_slot_kwh
    )
    conservative_source_utc = _utc_energy_map(conservative_source)
    conservative_pv = {
        start: max(float(conservative_source_utc.get(start, 0.0)), 0.0)
        for start in starts
    }
    if starts:
        conservative_pv[starts[0]] *= first_fraction
        if current_slot_pv_source == "live":
            conservative_pv[starts[0]] = expected_pv[starts[0]]

    conservative_load, load_risk_multiplier, load_risk_buffer = (
        _conservative_load_by_slot(
            starts,
            settings,
            load_by_slot,
            modeled_day_energy=modeled_day_load,
            current_slot_is_live=(current_slot_load_source == "live"),
        )
    )
    if settings.dynamic_reserve_enabled:
        base_soc = min(
            max(
                settings.outage_reserve_soc_percent
                + settings.safety_margin_soc_percent,
                0.0,
            ),
            100.0,
        )
    else:
        base_soc = min(max(settings.manual_minimum_soc_percent, 0.0), 100.0)
    floor_kwh = capacity * base_soc / 100.0
    protected_night_energy, export_reserve_by_slot = (
        _protected_night_reserve_by_slot(
            starts,
            settings,
            load_by_slot,
            floor_kwh,
        )
        if settings.dynamic_reserve_enabled
        else (0.0, {})
    )
    current_energy = capacity * settings.battery_soc_percent / 100.0
    # RCE is revenue-first.  Missing/wide P10 alone must not erase a large,
    # plausible PV forecast as it does in the tariff worst-case model.  The
    # zero-PV scenario becomes active only when stored energy cannot already
    # cover the base reserve plus the complete upcoming protected night.
    critical_guard_active = (
        settings.critical_zero_pv_guard
        and current_energy
        <= min(floor_kwh + protected_night_energy, capacity) + 1e-6
    )
    if critical_guard_active:
        (
            conservative_pv,
            critical_guard_until,
            critical_guarded_kwh,
        ) = _critical_zero_pv_scenario(
            starts,
            settings,
            conservative_pv,
            preserve_live_current=(current_slot_pv_source == "live"),
        )
    else:
        critical_guard_until = None
        critical_guarded_kwh = 0.0
    required_now = (
        _required_energy_now(
            starts,
            settings,
            load_by_slot,
            floor_kwh,
            conservative_pv,
            slot_fractions,
        )
        if settings.dynamic_reserve_enabled
        else floor_kwh
    )
    if settings.dynamic_reserve_enabled:
        # Keep the upcoming night explicit even when a sunny forecast before
        # sunset would otherwise reduce the backward energy requirement.
        required_now = max(
            required_now,
            min(floor_kwh + protected_night_energy, capacity),
        )
    minimum_soc = math.ceil(required_now / capacity * 100.0 - 1e-9)
    minimum_soc = min(max(minimum_soc, math.ceil(base_soc)), 100)
    control_reserve = capacity * minimum_soc / 100.0
    quantization_reserve = max(control_reserve - required_now, 0.0)
    if export_reserve_by_slot:
        # The scheduler writes one whole-percent Force Discharge SOC for the
        # current plan, not a fractional/per-slot value.  Every planned export
        # must therefore respect at least that exact register-level reserve.
        export_reserve_by_slot = {
            start: max(reserve, control_reserve)
            for start, reserve in export_reserve_by_slot.items()
        }
    available_now = max(current_energy - control_reserve, 0.0)

    baseline_ok, baseline_end, _ = _simulate(
        starts,
        settings,
        load_by_slot,
        {},
        floor_kwh,
        pv_by_slot_kwh=conservative_pv,
        slot_fractions=slot_fractions,
    )
    baseline_trace: list[_RCESimulationSlot] = []
    _, baseline_expected_end, baseline_natural = _simulate(
        starts,
        settings,
        load_by_slot,
        {},
        floor_kwh,
        pv_by_slot_kwh=expected_pv,
        slot_fractions=slot_fractions,
        trace_collector=baseline_trace,
    )
    system_power = settings.inverter_power_kw * settings.inverter_count
    ac_system_power = _inverter_ac_power_kw(settings) * settings.inverter_count
    requested_power = system_power * _quantize_4306_percent(
        settings.discharge_power_percent
    ) / 100.0
    bms_dc_power_limit = _bms_dc_power_limit_kw(settings)
    bms_charge_power_limit = _bms_charge_dc_power_limit_kw(settings)
    bms_power_limit: float | None = None
    if bms_dc_power_limit is not None:
        # Register 1917 is the dynamic DC-current limit reported by the BMS.
        # Convert it to safe AC export power and keep a separate guard below
        # that limit so voltage/temperature changes do not trip the battery.
        bms_power_limit = bms_dc_power_limit * min(
            max(settings.export_efficiency_percent, 0.0), 100.0
        ) / 100.0
    power_limits: list[tuple[str, float]] = [
        ("requested_power", requested_power)
    ]
    if bms_power_limit is not None:
        power_limits.append(("bms", bms_power_limit))
    if settings.export_power_cap_kw is not None:
        power_limits.append(
            ("gcf_export_cap", max(settings.export_power_cap_kw, 0.0))
        )
    if settings.effective_export_power_kw is not None:
        power_limits.append(
            (
                "effective_export_power",
                max(settings.effective_export_power_kw, 0.0),
            )
        )
    physical_limit_source, maximum_power = min(
        power_limits,
        key=lambda item: item[1],
    )
    bms_limit_percent = (
        min(max(bms_power_limit / system_power * 100.0, 0.0), 100.0)
        if bms_power_limit is not None and system_power > 0
        else None
    )
    export_efficiency = max(
        min(settings.export_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    day3_available = settings.day3_pv_forecast_kwh is not None
    day3_forecast = (
        max(settings.day3_pv_forecast_kwh, 0.0)
        if settings.day3_pv_forecast_kwh is not None
        else None
    )
    day3_load_requirement = max(settings.average_daily_load_kwh, 0.0)
    day3_shortfall = (
        max(
            day3_load_requirement - (day3_forecast or 0.0),
            0.0,
        )
        if day3_available
        else 0.0
    )
    if not day3_available:
        terminal_reserve_reason = "day3_forecast_missing"
    elif day3_load_requirement <= 0.0:
        terminal_reserve_reason = "day3_load_not_required"
    elif day3_shortfall <= 1e-9:
        terminal_reserve_reason = "day3_pv_covers_load"
    else:
        terminal_reserve_reason = "day3_pv_deficit"
    house_discharge_efficiency = max(
        min(settings.house_discharge_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    # Day-3 shortfall is AC energy consumed by the home, while battery state
    # and the terminal target are DC energy.  Reserve enough DC energy to
    # deliver the complete forecast shortfall after inverter losses.
    terminal_energy_target = min(
        day3_shortfall / house_discharge_efficiency,
        max(capacity - floor_kwh, 0.0),
    )
    terminal_unit_value = (
        max(settings.avoided_import_price_pln_kwh, 0.0)
        * house_discharge_efficiency
    )
    price_by_start = {
        slot.start.astimezone(dt_timezone.utc): slot.price_pln_kwh
        for slot in settings.price_slots
    }
    (
        baseline_objective,
        _,
        baseline_terminal_value,
    ) = _economic_objective(
        exports={},
        natural_exports=baseline_natural,
        price_by_start=price_by_start,
        ending_battery_kwh=baseline_expected_end,
        floor_kwh=floor_kwh,
        export_efficiency=export_efficiency,
        battery_wear_cost_pln_kwh=settings.battery_wear_cost_pln_kwh,
        terminal_energy_target_kwh=terminal_energy_target,
        terminal_energy_value_pln_kwh=terminal_unit_value,
    )
    result = OptimizerResult(
        ready=baseline_ok,
        status_code="ready",
        minimum_soc_percent=minimum_soc,
        base_reserve_energy_kwh=floor_kwh,
        protected_night_energy_kwh=protected_night_energy,
        additional_forecast_reserve_kwh=max(required_now - floor_kwh, 0.0),
        protected_home_energy_kwh=required_now,
        available_energy_now_kwh=available_now,
        ending_battery_kwh=max(baseline_end, 0.0),
        system_power_kw=system_power,
        requested_export_power_kw=requested_power,
        bms_discharge_power_limit_kw=bms_power_limit,
        bms_discharge_limit_percent=bms_limit_percent,
        bms_limit_active=(
            bms_power_limit is not None
            and bms_power_limit < requested_power - 0.001
        ),
        maximum_export_power_kw=maximum_power,
        historical_day_load_kwh=historical_day_load,
        live_projected_day_load_kwh=live_projected_day_load,
        modeled_day_load_kwh=modeled_day_load,
        daylight_progress_percent=daylight_progress * 100.0,
        load_profile_mode=load_profile_mode,
        forecast_confidence_percent=min(
            max(settings.forecast_confidence_percent, 0.0),
            100.0,
        ),
        bms_discharge_data_fresh=settings.bms_discharge_data_fresh,
        bms_discharge_data_age_seconds=settings.bms_discharge_data_age_seconds,
        bms_discharge_data_available=settings.bms_discharge_data_available,
        bms_charge_power_limit_kw=bms_charge_power_limit,
        bms_charge_data_fresh=settings.bms_charge_data_fresh,
        bms_charge_data_age_seconds=settings.bms_charge_data_age_seconds,
        bms_charge_data_available=settings.bms_charge_data_available,
        export_power_cap_kw=settings.export_power_cap_kw,
        effective_export_power_kw=settings.effective_export_power_kw,
        physical_limit_source=physical_limit_source,
        control_reserve_energy_kwh=control_reserve,
        soc_quantization_reserve_kwh=quantization_reserve,
        day3_forecast_available=day3_available,
        day3_forecast_kwh=day3_forecast,
        day3_load_requirement_kwh=day3_load_requirement,
        day3_energy_shortfall_kwh=day3_shortfall,
        terminal_reserve_reason=terminal_reserve_reason,
        terminal_energy_target_kwh=terminal_energy_target,
        terminal_energy_value_pln_kwh=terminal_unit_value,
        terminal_energy_value_pln=baseline_terminal_value,
        baseline_terminal_energy_value_pln=baseline_terminal_value,
        net_objective_pln=baseline_objective,
        baseline_net_objective_pln=baseline_objective,
        conservative_daily_load_kwh=settings.conservative_daily_load_kwh,
        conservative_night_load_kwh=settings.conservative_night_load_kwh,
        load_risk_multiplier=load_risk_multiplier,
        load_risk_buffer_kwh=load_risk_buffer,
        load_risk_mode="diagnostic_only",
        critical_zero_pv_guard_active=critical_guard_active,
        critical_zero_pv_guard_reason=(
            settings.critical_zero_pv_guard_reason
            if critical_guard_active
            else (
                "risk_not_energy_critical"
                if settings.critical_zero_pv_guard
                else "not_required"
            )
        ),
        critical_zero_pv_guard_until=(
            critical_guard_until.astimezone(settings.now.tzinfo)
            if critical_guard_until is not None
            else None
        ),
        critical_zero_pv_guarded_kwh=critical_guarded_kwh,
        current_slot_end=(
            (starts[0] + SLOT).astimezone(settings.now.tzinfo)
            if starts
            else None
        ),
        current_slot_remaining_minutes=(
            max(
                ((starts[0] + SLOT) - settings.now.astimezone(dt_timezone.utc))
                .total_seconds()
                / 60.0,
                0.0,
            )
            if starts
            else 0.0
        ),
        current_slot_fraction=first_fraction if starts else 0.0,
        current_required_minimum_soc_percent=(
            math.ceil(
                max(
                    export_reserve_by_slot.get(starts[0], control_reserve),
                    control_reserve,
                )
                / capacity
                * 100.0
                - 1e-9
            )
            if starts
            else minimum_soc
        ),
        current_slot_load_kwh=(
            load_by_slot.get(starts[0], 0.0) if starts else 0.0
        ),
        current_slot_pv_kwh=(expected_pv.get(starts[0], 0.0) if starts else 0.0),
        current_slot_load_source=current_slot_load_source,
        current_slot_pv_source=current_slot_pv_source,
        post_command_settling_market_fingerprint=(
            post_command_settling_market_fingerprint(settings)
        ),
        current_slot_shared_discharge_limit_kwh=(
            _slot_export_limit_kwh(
                settings,
                load_by_slot.get(starts[0], 0.0),
                conservative_pv.get(starts[0], 0.0),
                first_fraction,
            )
            if starts
            else 0.0
        ),
        uncontrolled_export_kwh=sum(baseline_natural.values()),
        uncontrolled_revenue_pln=_market_revenue(
            {},
            baseline_natural,
            price_by_start,
        ),
    )
    result.timeline_trace = _rce_timeline_trace(
        settings=settings,
        selected=baseline_trace,
        baseline=baseline_trace,
        exports={},
        price_by_start=price_by_start,
        floor_kwh=floor_kwh,
        export_reserve_by_slot=export_reserve_by_slot,
        horizon_hours=None if fixed_exports is not None else 48.0,
    )
    if not baseline_ok:
        result.status_code = "home_energy_shortage"
        return finish(settings, result)

    candidates = [
        (slot, slot.start.astimezone(dt_timezone.utc))
        for slot in settings.price_slots
        if slot.start.astimezone(dt_timezone.utc) >= now_slot
        and slot.start.astimezone(dt_timezone.utc) < horizon_end
        and not slot.blocked
    ]
    candidates.sort(key=lambda item: (-item[0].price_pln_kwh, item[1]))
    if not candidates or maximum_power <= 0:
        result.status_code = (
            "zero_export"
            if settings.export_power_cap_kw is not None
            and settings.export_power_cap_kw <= 0
            else "waiting_for_market"
        )
        result.natural_export_kwh = sum(baseline_natural.values())
        result.natural_revenue_pln = result.uncontrolled_revenue_pln
        bms_reason = _bms_start_suppression_reason(settings)
        if bms_reason is not None:
            result.current_slot_suppression_reason = bms_reason
        return finish(settings, result)

    solver_started = perf_counter()
    if fixed_exports is None:
        exports = _solve_joint_horizon_exports(
            starts=starts,
            settings=settings,
            candidates=candidates,
            load_by_slot=load_by_slot,
            floor_kwh=floor_kwh,
            export_reserve_by_slot=export_reserve_by_slot,
            conservative_pv=conservative_pv,
            expected_pv=expected_pv,
            slot_fractions=slot_fractions,
            price_by_start=price_by_start,
            baseline_objective=baseline_objective,
            export_efficiency=export_efficiency,
            terminal_energy_target=terminal_energy_target,
            terminal_unit_value=terminal_unit_value,
        )
    else:
        exports = {}
        allowed_starts = {start for _, start in candidates}
        for start, energy in fixed_exports.items():
            if start not in allowed_starts:
                result.ready = False
                result.status_code = "optimizer_error"
                return finish(settings, result)
            cap = _slot_export_limit_kwh(
                settings, load_by_slot.get(start, 0.0),
                conservative_pv.get(start, 0.0), slot_fractions.get(start, 1.0),
            )
            if starts and start == starts[0] and fixed_current_discharge_cap_kw is not None:
                hours = 0.5 * slot_fractions[start]
                cap = min(cap, max(
                    fixed_current_discharge_cap_kw * hours
                    - max(load_by_slot.get(start, 0.0) - expected_pv.get(start, 0.0), 0.0),
                    0.0,
                ))
            bounded = min(energy, cap)
            if bounded >= 0.01:
                exports[start] = bounded
    result.solver_runtime_ms = (perf_counter() - solver_started) * 1000.0

    feasible, ending_battery, _ = _simulate(
        starts,
        settings,
        load_by_slot,
        exports,
        floor_kwh,
        export_reserve_by_slot,
        conservative_pv,
        slot_fractions,
    )
    selected_trace: list[_RCESimulationSlot] = []
    _, expected_ending_battery, natural_exports = _simulate(
        starts,
        settings,
        load_by_slot,
        exports,
        floor_kwh,
        export_reserve_by_slot,
        expected_pv,
        slot_fractions,
        trace_collector=selected_trace,
    )
    if not feasible:
        result.ready = False
        result.status_code = "optimizer_error"
        return finish(settings, result)

    result.planned_exports = [
        PlannedExport(
            start=start.astimezone(settings.now.tzinfo),
            price_pln_kwh=price_by_start[start],
            energy_kwh=energy,
        )
        for start, energy in sorted(exports.items())
    ]
    result.natural_export_kwh = sum(natural_exports.values())
    result.natural_revenue_pln = sum(
        energy * price_by_start.get(start, 0.0)
        for start, energy in natural_exports.items()
    )
    result.ending_battery_kwh = ending_battery
    result.timeline_trace = _rce_timeline_trace(
        settings=settings,
        selected=selected_trace,
        baseline=baseline_trace,
        exports=exports,
        price_by_start=price_by_start,
        floor_kwh=floor_kwh,
        export_reserve_by_slot=export_reserve_by_slot,
        horizon_hours=None if fixed_exports is not None else 48.0,
    )
    (
        result.net_objective_pln,
        result.battery_wear_cost_pln,
        result.terminal_energy_value_pln,
    ) = _economic_objective(
        exports=exports,
        natural_exports=natural_exports,
        price_by_start=price_by_start,
        ending_battery_kwh=expected_ending_battery,
        floor_kwh=floor_kwh,
        export_efficiency=export_efficiency,
        battery_wear_cost_pln_kwh=settings.battery_wear_cost_pln_kwh,
        terminal_energy_target_kwh=terminal_energy_target,
        terminal_energy_value_pln_kwh=terminal_unit_value,
    )
    current_export = exports.get(starts[0], 0.0) if starts else 0.0
    result.current_slot_planned_export_kwh = current_export
    current_remaining_hours = 0.5 * first_fraction
    if current_export >= 0.01 and current_remaining_hours > 0.0:
        execution_export_power = current_export / current_remaining_hours
        current_system_energy = ac_system_power * current_remaining_hours
        current_pv_to_load = min(
            result.current_slot_pv_kwh,
            result.current_slot_load_kwh,
            current_system_energy,
        )
        current_load_deficit = max(
            result.current_slot_load_kwh - current_pv_to_load,
            0.0,
        )
        execution_discharge_power = (
            (current_export + current_load_deficit) / current_remaining_hours
        )
        current_load_deficit_power = (
            current_load_deficit / current_remaining_hours
        )
        bms_total_ac_power = _bms_total_ac_discharge_power_limit_kw(
            settings,
            current_load_deficit_power,
        )
        current_bridge_discharge_power = max(
            ac_system_power - current_pv_to_load / current_remaining_hours,
            0.0,
        )
        result.current_slot_execution_export_power_kw = (
            execution_export_power
        )
        result.current_slot_execution_discharge_power_kw = min(
            execution_discharge_power,
            requested_power,
            system_power,
            current_bridge_discharge_power,
            bms_total_ac_power,
        )
        result.current_slot_execution_power_percent = _quantize_4306_percent(
            result.current_slot_execution_discharge_power_kw
            / system_power
            * 100.0
        )
        if fixed_exports is not None:
            # Preserve an exactly feasible integer command after partial-slot
            # scaling, using energy and all fresh caps without a tolerance.
            next_percent = min(result.current_slot_execution_power_percent + 1.0, 100.0)
            next_power = system_power * next_percent / 100.0
            next_export_power = max(next_power - current_load_deficit_power, 0.0)
            if (
                next_power <= min(requested_power, system_power,
                    current_bridge_discharge_power, bms_total_ac_power,
                    fixed_current_discharge_cap_kw if fixed_current_discharge_cap_kw is not None else 0.0)
                and max(next_power * current_remaining_hours - current_load_deficit, 0.0) <= current_export
                and all(next_export_power <= cap for cap in (
                    settings.export_power_cap_kw, settings.effective_export_power_kw,
                ) if cap is not None)
            ):
                result.current_slot_execution_power_percent = next_percent
        result.current_slot_execution_discharge_power_kw = (
            system_power * result.current_slot_execution_power_percent / 100.0
        )
        result.current_slot_execution_export_power_kw = min(
            execution_export_power,
            max(result.current_slot_execution_discharge_power_kw - current_load_deficit_power, 0.0),
        )
    current_planned = current_export >= 0.01
    if current_planned:
        current_run_end_utc = starts[0]
        while exports.get(current_run_end_utc, 0.0) >= 0.01:
            current_run_end_utc += SLOT
        result.current_run_end = current_run_end_utc.astimezone(
            settings.now.tzinfo
        )
    live_ready = (
        current_slot_load_source == "live"
        and current_slot_pv_source == "live"
        and settings.current_battery_soc_fresh
    )
    bms_reason = _bms_start_suppression_reason(settings)
    if bms_reason is not None:
        suppression_reason = bms_reason
    elif not current_planned:
        suppression_reason = "no_current_plan"
    elif result.current_slot_remaining_minutes < 5.0:
        suppression_reason = "insufficient_runtime"
    elif current_export < 0.01:
        suppression_reason = "no_export_energy"
    elif result.current_slot_execution_power_percent <= 0.0:
        suppression_reason = "execution_power_unavailable"
    elif not live_ready:
        suppression_reason = "live_data_missing"
    elif result.current_slot_shared_discharge_limit_kwh < current_export - 1e-6:
        suppression_reason = "pv_or_grid_balance_unsafe"
    else:
        suppression_reason = "eligible"
    result.current_slot_suppression_reason = suppression_reason
    result.current_slot_start_eligible = suppression_reason == "eligible"
    if not result.planned_exports:
        result.status_code = "home_protected"
    result.current_slot_load_exhausts_requested_discharge_budget = (
        _current_slot_load_exhausts_requested_discharge_budget(settings, result)
    )
    return finish(settings, result)
