"""Pure RCE energy-planning helpers.

The optimizer is intentionally independent from Home Assistant so the energy
model can be tested without importing Home Assistant.  It maximizes expected
RCE revenue while preserving enough battery energy for the house, including
the protected night after the second market day.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
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
    from .load_model import robust_weighted_upper_estimate
except ImportError:  # pragma: no cover - exercised by tools/test_rce_optimizer.py
    from automation_plan_timeline import (
        OptimizerTimelineTrace,
        RCEPolicyPoint,
        TimelineTracePoint,
    )
    from load_model import robust_weighted_upper_estimate


SLOT = timedelta(minutes=30)
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

    @property
    def planned_export_kwh(self) -> float:
        """Return scheduled export energy."""
        return sum(item.energy_kwh for item in self.planned_exports)

    @property
    def planned_revenue_pln(self) -> float:
        """Return scheduled export revenue."""
        return sum(item.revenue_pln for item in self.planned_exports)

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

    day_minutes = max((24 * 60) - night_minutes, 30)
    current_minute = settings.now.hour * 60 + settings.now.minute
    day_start = settings.night_end_minute % (24 * 60)
    day_end = settings.night_start_minute % (24 * 60)
    elapsed = (current_minute - day_start) % (24 * 60)
    in_daylight_window = elapsed < day_minutes
    if in_daylight_window:
        progress = min(max(elapsed / day_minutes, 0.0), 1.0)
    elif day_start <= day_end:
        # With the normal European solar window, the hours before day_start
        # belong to the new day and those after day_end to the completed day.
        progress = 1.0 if current_minute >= day_end else 0.0
    else:
        progress = 0.0

    actual_day_load = max(settings.actual_day_load_today_kwh or 0.0, 0.0)
    live_projection = 0.0
    if progress >= 0.05 and actual_day_load > 0:
        # Cap a transiently high early projection.  The cap is deliberately
        # generous: it protects the house while preventing one 0.1 kWh counter
        # step just after sunrise from reserving the entire battery.
        projection_cap = max(
            daily * 2.0,
            historical_day_energy,
            actual_day_load,
        )
        live_projection = min(
            actual_day_load / progress,
            projection_cap,
        )
    modeled_day_energy = max(historical_day_energy, live_projection)
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
    night_minutes = (
        settings.night_end_minute - settings.night_start_minute
    ) % (24 * 60)
    night_hours = max(night_minutes / 60.0, 0.5)
    daily = max(settings.average_daily_load_kwh, 0.0)
    if settings.average_night_load_kwh is None:
        night_energy = daily * night_hours / 24.0
    else:
        night_energy = min(max(settings.average_night_load_kwh, 0.0), daily)
    night_slot = night_energy / night_hours * 0.5

    def valid_profile(values: tuple[float, ...]) -> tuple[float, ...]:
        if len(values) != 48:
            return ()
        normalized = tuple(max(float(value), 0.0) for value in values)
        return normalized if sum(normalized) > 0 else ()

    average_profile = valid_profile(settings.load_profile_30m_kwh)
    weekday_profile = valid_profile(settings.weekday_load_profile_30m_kwh)
    weekend_profile = valid_profile(settings.weekend_load_profile_30m_kwh)

    if average_profile or weekday_profile or weekend_profile:
        loads: dict[datetime, float] = {}
        modes: set[str] = set()
        dates = sorted({_local_slot(start, settings).date() for start in starts})
        for slot_date in dates:
            weekend = slot_date.weekday() >= 5
            profile = weekend_profile if weekend else weekday_profile
            if profile:
                modes.add("weekend_48_slot" if weekend else "weekday_48_slot")
            else:
                profile = average_profile
                if profile:
                    modes.add("average_48_slot")
            if not profile:
                # One day type can be absent after a fresh installation.  Use
                # whichever learned profile exists instead of dropping back to
                # a flat curve for that day only.
                profile = weekday_profile or weekend_profile
                modes.add("cross_day_48_slot_fallback")

            night_indexes = [
                index
                for index in range(48)
                if _is_night(
                    datetime.combine(
                        slot_date,
                        time(hour=index // 2, minute=(index % 2) * 30),
                        tzinfo=settings.now.tzinfo,
                    ),
                    settings.night_start_minute,
                    settings.night_end_minute,
                )
            ]
            night_weight = sum(profile[index] for index in night_indexes)
            day_weight = max(sum(profile) - night_weight, 0.0)
            day_indexes = [
                index for index in range(48) if index not in night_indexes
            ]
            night_scale = (
                night_energy / night_weight if night_weight > 0 else 0.0
            )
            day_scale = (
                modeled_day_energy / day_weight if day_weight > 0 else 0.0
            )
            for start in (
                item
                for item in starts
                if _local_slot(item, settings).date() == slot_date
            ):
                local_start = _local_slot(start, settings)
                index = local_start.hour * 2 + local_start.minute // 30
                is_night = _is_night(
                    local_start,
                    settings.night_start_minute,
                    settings.night_end_minute,
                )
                if is_night and night_weight <= 0:
                    loads[start] = night_energy / max(len(night_indexes), 1)
                elif not is_night and day_weight <= 0:
                    loads[start] = modeled_day_energy / max(len(day_indexes), 1)
                else:
                    loads[start] = profile[index] * (
                        night_scale if is_night else day_scale
                    )

        # For the unfinished daylight window, actual measured consumption may
        # imply more remaining energy than the historical curve.  Scale only
        # future daytime slots upward; never weaken the historical reserve.
        today_day_starts = [
            start
            for start in starts
            if _local_slot(start, settings).date() == settings.now.date()
            and not _is_night(
                _local_slot(start, settings),
                settings.night_start_minute,
                settings.night_end_minute,
            )
        ]
        if today_day_starts:
            historical_remaining = historical_day_energy * max(
                1.0 - daylight_progress,
                0.0,
            )
            live_remaining = max(
                live_projected_day_energy
                - max(settings.actual_day_load_today_kwh or 0.0, 0.0),
                0.0,
            )
            current_profile_remaining = sum(
                loads[start] for start in today_day_starts
            )
            target_remaining = max(
                current_profile_remaining,
                historical_remaining,
                live_remaining,
            )
            if current_profile_remaining > 0:
                scale = target_remaining / current_profile_remaining
                for start in today_day_starts:
                    loads[start] *= scale
            elif target_remaining > 0:
                per_slot = target_remaining / len(today_day_starts)
                for start in today_day_starts:
                    loads[start] = per_slot

        if {"weekday_48_slot", "weekend_48_slot"} <= modes:
            mode = "weekday_weekend_48_slot"
        elif "weekday_48_slot" in modes:
            mode = "weekday_48_slot"
        elif "weekend_48_slot" in modes:
            mode = "weekend_48_slot"
        elif "average_48_slot" in modes:
            mode = "average_48_slot"
        else:
            mode = "cross_day_48_slot_fallback"
        return loads, mode

    loads: dict[datetime, float] = {}
    day_slots_by_date: dict[date, list[datetime]] = {}
    for start in starts:
        if _is_night(
            _local_slot(start, settings),
            settings.night_start_minute,
            settings.night_end_minute,
        ):
            loads[start] = night_slot
        else:
            day_slots_by_date.setdefault(
                _local_slot(start, settings).date(), []
            ).append(start)

    for slot_date, day_starts in day_slots_by_date.items():
        day_energy = modeled_day_energy
        if slot_date == settings.now.date():
            historical_remaining = historical_day_energy * max(
                1.0 - daylight_progress,
                0.0,
            )
            live_remaining = max(
                live_projected_day_energy
                - max(settings.actual_day_load_today_kwh or 0.0, 0.0),
                0.0,
            )
            day_energy = max(historical_remaining, live_remaining)
        per_slot = day_energy / max(len(day_starts), 1)
        for start in day_starts:
            loads[start] = per_slot
    return loads, "flat_day_night_fallback"


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


def _slot_export_limit_kwh(
    settings: OptimizerInput,
    load_kwh: float,
    pv_kwh: float,
    slot_fraction: float,
) -> float:
    """Return grid-export energy after sharing discharge power with LOAD."""
    fraction = min(max(slot_fraction, 0.0), 1.0)
    hours = 0.5 * fraction
    if hours <= 0.0:
        return 0.0
    system_power = settings.inverter_power_kw * settings.inverter_count
    requested_power = system_power * min(
        max(settings.discharge_power_percent, 0.0), 100.0
    ) / 100.0
    load_deficit_ac = max(load_kwh - pv_kwh, 0.0)
    # Battery discharge power is shared with battery-fed LOAD, while the whole
    # inverter AC bridge is shared by PV/LOAD and every grid-export branch.
    shared_limits = [max(requested_power * hours - load_deficit_ac, 0.0)]
    shared_limits.append(
        max(system_power * hours - min(load_kwh, system_power * hours), 0.0)
    )

    bms_dc_power = _bms_dc_power_limit_kw(settings)
    export_efficiency = max(
        min(settings.export_efficiency_percent / 100.0, 1.0), 0.01
    )
    house_efficiency = max(
        min(settings.house_discharge_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    if bms_dc_power is not None:
        remaining_dc = max(
            bms_dc_power * hours - load_deficit_ac / house_efficiency,
            0.0,
        )
        shared_limits.append(remaining_dc * export_efficiency)

    # GCF and learned delivered power limit only the grid branch.  They do not
    # reduce the separate AC power needed to keep the house supplied.
    if settings.export_power_cap_kw is not None:
        shared_limits.append(max(settings.export_power_cap_kw, 0.0) * hours)
    if settings.effective_export_power_kw is not None:
        shared_limits.append(
            max(settings.effective_export_power_kw, 0.0) * hours
        )
    return max(min(shared_limits), 0.0)


def _slot_charge_input_limit_kwh(
    settings: OptimizerInput,
    load_kwh: float,
    pv_kwh: float,
    slot_fraction: float,
    controlled_export_kwh: float = 0.0,
) -> float:
    """Return PV AC energy that can physically charge the battery this slot."""
    surplus = max(pv_kwh - load_kwh, 0.0)
    if surplus <= 0.0:
        return 0.0
    fraction = min(max(slot_fraction, 0.0), 1.0)
    hours = 0.5 * fraction
    if hours <= 0.0:
        return 0.0
    system_energy = settings.inverter_power_kw * settings.inverter_count * hours
    remaining_conversion = max(
        system_energy
        - min(load_kwh, system_energy)
        - max(controlled_export_kwh, 0.0),
        0.0,
    )
    charge_efficiency = max(
        min(settings.charge_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    bms_ac_input = (
        _bms_charge_dc_power_limit_kw(settings)
        * hours
        / charge_efficiency
    )
    return max(min(surplus, remaining_conversion, bms_ac_input), 0.0)


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
    capacity = settings.battery_capacity_kwh
    battery = capacity * settings.battery_soc_percent / 100.0
    efficiency = max(
        min(settings.export_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    charge_efficiency = max(
        min(settings.charge_efficiency_percent / 100.0, 1.0),
        0.01,
    )
    house_discharge_efficiency = max(
        min(settings.house_discharge_efficiency_percent / 100.0, 1.0),
        0.01,
    )
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
        if export > _slot_export_limit_kwh(
            settings,
            load,
            pv,
            fraction,
        ) + 1e-6:
            return False, battery, {}
        if pv < load:
            battery -= (load - pv) / house_discharge_efficiency
        battery -= export / efficiency
        natural_export = 0.0
        if pv >= load:
            charge_input_ac = _slot_charge_input_limit_kwh(
                settings,
                load,
                pv,
                fraction,
                export,
            )
            charge_input_ac = min(
                charge_input_ac,
                max(capacity - battery, 0.0) / charge_efficiency,
            )
            battery += charge_input_ac * charge_efficiency
        else:
            charge_input_ac = 0.0
        if battery < floor_kwh - 1e-6:
            return False, battery, {}
        if (
            exports.get(start, 0.0) > 0
            and export_reserve_by_slot is not None
            and battery
            < max(float(export_reserve_by_slot.get(start, floor_kwh)), floor_kwh)
            - 1e-6
        ):
            return False, battery, {}
        if pv >= load:
            unallocated_pv = max(pv - load - charge_input_ac, 0.0)
            system_energy = (
                settings.inverter_power_kw
                * settings.inverter_count
                * 0.5
                * fraction
            )
            remaining_system_headroom = max(
                system_energy
                - min(load, system_energy)
                - export
                - charge_input_ac,
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
                max(min(grid_caps) * 0.5 * fraction - export, 0.0)
                if grid_caps
                else remaining_system_headroom
            )
            natural_export = min(
                unallocated_pv,
                remaining_system_headroom,
                remaining_grid_headroom,
            )
            if natural_export > 0.0:
                natural_exports[start] = natural_export
        battery = min(battery, capacity)
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
                    grid_import_kwh=0.0,
                    grid_export_kwh=export + natural_export,
                    battery_after_kwh=battery,
                )
            )
    return True, battery, natural_exports


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
) -> OptimizerTimelineTrace | None:
    """Merge two already-executed simulations into one immutable RCE trace."""

    if not selected or len(selected) != len(baseline):
        return None
    capacity = max(settings.battery_capacity_kwh, 0.001)
    baseline_by_start = {item.slot_start: item for item in baseline}
    points: list[TimelineTracePoint] = []
    horizon_limit = selected[0].start + timedelta(hours=48)
    for item in selected:
        if item.end > horizon_limit:
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
                    target_discharge_kw=planned_export / duration_hours,
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
    return best_plan


def optimize_rce(settings: OptimizerInput) -> OptimizerResult:
    """Return the most valuable feasible RCE export plan."""
    capacity = settings.battery_capacity_kwh
    if (
        capacity <= 0
        or settings.average_daily_load_kwh < 0
        or settings.inverter_power_kw <= 0
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
    requested_power = system_power * min(
        max(settings.discharge_power_percent, 0.0),
        100.0,
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
    )
    if not baseline_ok:
        result.status_code = "home_energy_shortage"
        return result

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
        return result

    solver_started = perf_counter()
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
        return result

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
    current_remaining_hours = result.current_slot_remaining_minutes / 60.0
    if current_export >= 0.01 and current_remaining_hours > 0.0:
        execution_export_power = current_export / current_remaining_hours
        current_load_deficit = max(
            result.current_slot_load_kwh - result.current_slot_pv_kwh,
            0.0,
        )
        execution_discharge_power = (
            execution_export_power
            + current_load_deficit / current_remaining_hours
        )
        result.current_slot_execution_export_power_kw = (
            execution_export_power
        )
        result.current_slot_execution_discharge_power_kw = min(
            execution_discharge_power,
            requested_power,
            system_power,
        )
        result.current_slot_execution_power_percent = min(
            max(
                result.current_slot_execution_discharge_power_kw
                / system_power
                * 100.0,
                0.0,
            ),
            100.0,
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
    return result
