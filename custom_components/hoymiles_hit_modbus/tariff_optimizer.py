"""Pure optimizer for time-of-use grid charging.

The optimizer is deliberately independent from Home Assistant.  It simulates
the battery in 30-minute steps and schedules grid charging only in a cheaper
tariff slot that occurs before the energy is needed by the home.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from math import ceil, isfinite

try:  # Package import in Home Assistant; direct import in deterministic tests.
    from .automation_plan_timeline import (
        OptimizerTimelineTrace,
        TariffPolicyPoint,
        TimelineTracePoint,
    )
    from .energy_data import numeric_sample_is_fresh
    from .forecast_model import adaptive_forecast_factor
    from .load_model import robust_weighted_estimate, robust_weighted_upper_estimate
    from .tariff_profiles import (
        MANUAL_OPERATOR,
        get_tariff_profile,
        profile_is_valid,
        profile_rate,
    )
except ImportError:  # pragma: no cover - exercised by tools/test_tariff_optimizer.py
    from automation_plan_timeline import (
        OptimizerTimelineTrace,
        TariffPolicyPoint,
        TimelineTracePoint,
    )
    from energy_data import numeric_sample_is_fresh
    from forecast_model import adaptive_forecast_factor
    from load_model import robust_weighted_estimate, robust_weighted_upper_estimate
    from tariff_profiles import (
        MANUAL_OPERATOR,
        get_tariff_profile,
        profile_is_valid,
        profile_rate,
    )


SLOT = timedelta(minutes=30)
_EPSILON = 1e-6
DEFAULT_BATTERY_WEAR_COST_PLN_KWH = 0.06
# Starting Grid Charge has a real operational cost: a Modbus write, an EMS
# mode transition and user-visible notifications. Pure home support therefore
# needs to form one meaningful continuous cycle. These conservative constants
# are deliberately not user settings. A genuine battery/reserve charge is
# never gated by them.
GRID_SUPPORT_MODE_TRANSITION_SECONDS = 2 * 60
MIN_GRID_SUPPORT_USEFUL_RUNTIME_SECONDS = 5 * 60
MIN_GRID_SUPPORT_CYCLE_ENERGY_KWH = 0.25
MIN_GRID_SUPPORT_CYCLE_BENEFIT_PLN = 0.10
GRID_SUPPORT_TARGET_SOC_OFFSET_PERCENT = 1.0
_ALLOCATION_REQUIRED_ENERGY = 1
_ALLOCATION_ECONOMIC = 2


@dataclass(frozen=True, slots=True)
class TariffSchedule:
    """User-configurable time-of-use schedule."""

    tariff_type: str
    g11_price_pln_kwh: float
    low_price_pln_kwh: float
    medium_price_pln_kwh: float
    peak_price_pln_kwh: float
    cheap_windows: tuple[tuple[int, int], ...]
    medium_windows: tuple[tuple[int, int], ...] = ()
    weekend_low_price: bool = False
    polish_holidays_low_price: bool = False
    operator: str = MANUAL_OPERATOR


@dataclass(frozen=True, slots=True)
class TariffOptimizerInput:
    """All deterministic inputs used by the charging optimizer."""

    now: datetime
    pv_by_slot_kwh: dict[datetime, float]
    battery_capacity_kwh: float
    battery_soc_percent: float
    reserve_soc_percent: float
    maximum_soc_percent: float
    average_daily_load_kwh: float
    average_night_load_kwh: float | None
    night_start_minute: int
    night_end_minute: int
    # Total AC power that Grid Charge may draw for the home and battery
    # together.  The inverter subtracts the live home load from this budget.
    charge_power_kw: float
    charge_efficiency_percent: float
    discharge_efficiency_percent: float
    minimum_saving_pln_kwh: float
    schedule: TariffSchedule
    load_by_slot_kwh: dict[datetime, float] | None = None
    # Battery-side DC charging limit reported by the BMS.
    battery_charge_power_kw: float | None = None
    # Battery-side DC discharge limit reported by the BMS.
    battery_discharge_power_kw: float | None = None
    pv_charge_power_kw: float | None = None
    # Optional third forecast day.  Two days remains the safe compatibility
    # fallback when the Solcast Day 3 sensor is disabled or unavailable.
    horizon_days: int = 2
    # Energy retained at the end of the planning horizon.  This is separate
    # from the operational Self-Use floor and protects the next, not-yet-priced
    # period from deterministic point-forecast error.
    terminal_reserve_soc_percent: float | None = None
    # Nominal configured AC budget, retained for diagnostics when feedback has
    # conservatively derated ``charge_power_kw``.
    requested_charge_power_kw: float | None = None
    # Conservative throughput cost used internally to reject marginal cycles.
    # It is intentionally automatic: users should not need battery-finance
    # expertise to avoid shifting energy for a few groszy.
    battery_wear_cost_pln_kwh: float = DEFAULT_BATTERY_WEAR_COST_PLN_KWH
    # Optional fresh powers for the unfinished current half-hour. Historical
    # profiles remain the deterministic fallback for missing/stale telemetry
    # and for every complete future interval.
    current_load_power_kw: float | None = None
    current_pv_power_kw: float | None = None
    # Overview convention: positive means battery discharge, negative charge.
    current_battery_power_kw: float | None = None
    # The user's actual Self-Use floor without the automatic safety margin.
    # Falling below it is a hard reserve deficit; the extra margin may still
    # be restored just in time before the next expensive period.
    base_reserve_soc_percent: float | None = None
    # P90-like daily LOAD inferred automatically from the same 28-day recorder
    # history as the normal estimate. It is applied only inside expensive
    # windows, separately for each contiguous window.
    conservative_daily_load_kwh: float | None = None
    load_uncertainty_ratio: float = 0.0
    load_history_days: int = 0
    # Slot-level Solcast P10 scenario. The normal plan retains its blended
    # forecast; only the first morning peak following an overnight low window
    # is protected with this map (or zero PV under high uncertainty).
    pv_p10_by_slot_kwh: dict[datetime, float] | None = None
    pv_p10_available_dates: tuple[date, ...] = ()
    forecast_uncertainty_ratio: float = 0.0
    # A hard Self-Use deficit may be left to PV only after fresh measured
    # PV>LOAD has remained stable. Forecast energy alone is never sufficient.
    live_pv_surplus_stable: bool = False
    live_pv_surplus_stable_seconds: float = 0.0
    # The HA adapter validates the age of SOC, BMS limits and the recorder
    # LOAD broker.  Keeping the verdict in the pure input prevents a caller
    # from accidentally presenting a stale current block as executable while
    # still allowing the deterministic plan to remain visible diagnostically.
    control_inputs_fresh: bool = True
    control_input_block_reason: str = "none"


@dataclass(frozen=True, slots=True)
class PlannedCharge:
    """One planned half-hour grid-support or battery-charge block."""

    start: datetime
    price_pln_kwh: float
    zone: str
    grid_import_kwh: float
    stored_energy_kwh: float
    direct_load_kwh: float
    action: str
    target_soc_percent: float


@dataclass(frozen=True, slots=True)
class ExpensiveWindowLoadBuffer:
    """Conservative LOAD scenario for one contiguous non-low tariff window."""

    start: datetime
    end: datetime
    expected_load_kwh: float
    conservative_load_kwh: float
    buffer_kwh: float


@dataclass(frozen=True, slots=True)
class TariffOptimizerResult:
    """Optimized charging plan and its diagnostics."""

    status_code: str
    planned_charges: tuple[PlannedCharge, ...]
    baseline_shortage_kwh: float
    remaining_shortage_kwh: float
    planned_grid_import_kwh: float
    planned_stored_energy_kwh: float
    planned_direct_load_kwh: float
    planned_cost_pln: float
    baseline_grid_cost_pln: float
    optimized_grid_cost_pln: float
    automation_savings_pln: float
    baseline_grid_import_kwh: float
    optimized_grid_import_kwh: float
    g11_reference_cost_pln: float
    estimated_savings_pln: float
    ending_battery_kwh: float
    ending_battery_soc_percent: float
    target_soc_percent: float
    current_slot_planned: bool
    current_action: str
    current_slot_end: datetime | None
    current_price_pln_kwh: float
    current_zone: str
    next_charge_start: datetime | None
    charge_power_kw: float
    requested_charge_power_kw: float
    effective_power_factor: float
    horizon_days: int
    horizon_end: datetime
    terminal_reserve_soc_percent: float
    terminal_shortfall_kwh: float
    planned_battery_wear_cost_pln: float
    planning_slot_count: int
    baseline_optimization_cost_pln: float
    optimized_optimization_cost_pln: float
    planning_horizon_hours: float = 0.0
    planning_horizon_extended_to_minimum: bool = False
    modeled_load_kwh: float = 0.0
    modeled_pv_kwh: float = 0.0
    effective_terminal_reserve_soc_percent: float = 0.0
    current_run_end: datetime | None = None
    current_run_need_class: str = "none"
    current_run_duration_seconds: float = 0.0
    current_run_grid_import_kwh: float = 0.0
    current_run_stored_kwh: float = 0.0
    current_run_direct_load_kwh: float = 0.0
    current_run_benefit_pln: float = 0.0
    current_run_start_eligible: bool = False
    current_run_suppression_reason: str = "not_support_only"
    # Continuation deliberately has a weaker contract than a new start. Once
    # Grid Charge supplies the home, battery discharge should fall to zero;
    # treating that expected effect as a fault would stop every support run.
    current_run_continue_eligible: bool = False
    current_run_continue_reason: str = "not_support_only"
    current_slot_load_kwh: float = 0.0
    current_slot_pv_kwh: float = 0.0
    current_slot_load_source: str = "profile"
    current_slot_pv_source: str = "forecast"
    current_battery_power_kw: float | None = None
    base_reserve_soc_percent: float = 0.0
    hard_reserve_deficit_kwh: float = 0.0
    hard_reserve_restoration_required: bool = False
    hard_reserve_restored_by_near_term_pv: bool = False
    hard_reserve_unavailable: bool = False
    hard_reserve_shortfall_kwh: float = 0.0
    hard_reserve_deferral_source: str = "not_required"
    live_pv_surplus_stable: bool = False
    live_pv_surplus_stable_seconds: float = 0.0
    expensive_window_load_buffers: tuple[ExpensiveWindowLoadBuffer, ...] = ()
    load_risk_multiplier: float = 1.0
    load_risk_buffer_kwh: float = 0.0
    morning_protection_active: bool = False
    morning_protection_mode: str = "not_applicable"
    morning_protection_window_start: datetime | None = None
    morning_protection_window_end: datetime | None = None
    morning_protection_expected_pv_kwh: float = 0.0
    morning_protection_conservative_pv_kwh: float = 0.0
    remaining_low_direct_import_kwh: float = 0.0
    remaining_expensive_import_kwh: float = 0.0
    capacity_or_power_shortfall_kwh: float = 0.0
    control_inputs_fresh: bool = True
    control_input_block_reason: str = "none"
    # Observation-only sidecar, never consumed by planning or execution.
    timeline_trace: OptimizerTimelineTrace | None = field(
        default=None,
        repr=False,
        compare=False,
    )


@dataclass(slots=True)
class _Simulation:
    shortage_kwh: float
    first_shortage_index: int | None
    ending_battery_kwh: float
    accepted_import_kwh: dict[int, float]
    accepted_support_kwh: dict[int, float]
    stored_import_kwh: dict[int, float]
    battery_after_kwh: dict[int, float]
    uncovered_import_kwh: dict[int, float]
    total_grid_import_kwh: float
    total_grid_cost_pln: float
    total_optimization_cost_pln: float
    terminal_shortfall_kwh: float
    terminal_import_kwh: float
    pv_kwh: dict[int, float]
    load_kwh: dict[int, float]
    battery_delta_kwh: dict[int, float]
    grid_import_kwh: dict[int, float]
    grid_export_kwh: dict[int, float]


def _classify_current_run_need(
    *,
    current_planned: bool,
    current_run_slot_indices: tuple[int, ...],
    allocation_provenance: dict[int, int],
) -> str:
    """Return bounded current-run need provenance from accepted allocations."""
    if not current_planned or not current_run_slot_indices:
        return "none"
    combined = 0
    for index in current_run_slot_indices:
        origin = allocation_provenance.get(index)
        if origin not in {
            _ALLOCATION_REQUIRED_ENERGY,
            _ALLOCATION_ECONOMIC,
            _ALLOCATION_REQUIRED_ENERGY | _ALLOCATION_ECONOMIC,
        }:
            return "none"
        combined |= origin
    return {
        _ALLOCATION_REQUIRED_ENERGY: "required_energy",
        _ALLOCATION_ECONOMIC: "economic",
        _ALLOCATION_REQUIRED_ENERGY | _ALLOCATION_ECONOMIC: "mixed",
    }.get(combined, "none")


def floor_half_hour(value: datetime) -> datetime:
    """Return the start of the half-hour containing ``value``."""
    return value.replace(
        minute=0 if value.minute < 30 else 30,
        second=0,
        microsecond=0,
    )


def resolve_planning_horizon(
    now: datetime,
    horizon_days: int,
    *,
    minimum_hours: float = 0.0,
) -> tuple[int, datetime, float, bool]:
    """Return a calendar horizon, optionally extended to real elapsed hours.

    ``horizon_days`` retains the historical meaning used by this integration:
    two means the remainder of today plus tomorrow, while three additionally
    includes Day 3.  A calendar boundary can be shorter than expected late in
    the day and around the spring DST transition.  When a fresh Day 3 forecast
    is available, ``minimum_hours`` therefore guarantees a real elapsed
    planning interval without changing the compatible two-day fallback.
    """
    resolved_days = min(max(int(horizon_days), 2), 3)
    calendar_end = datetime.combine(
        now.date() + timedelta(days=resolved_days),
        datetime.min.time(),
        tzinfo=now.tzinfo,
    )
    end_utc = calendar_end.astimezone(timezone.utc)
    extended = False
    if minimum_hours > _EPSILON:
        target_utc = now.astimezone(timezone.utc) + timedelta(
            hours=max(minimum_hours, 0.0)
        )
        # End on a complete half-hour boundary so the simulation never models
        # only an implicit fraction of its final slot.
        target_floor = floor_half_hour(target_utc)
        if target_floor < target_utc:
            target_floor += SLOT
        if target_floor > end_utc:
            end_utc = target_floor
            extended = True
    end = end_utc.astimezone(now.tzinfo)
    actual_hours = max(
        (end_utc - now.astimezone(timezone.utc)).total_seconds() / 3600.0,
        0.0,
    )
    return resolved_days, end, actual_hours, extended


def horizon_gap_load_reserve_kwh(
    average_daily_load_kwh: float,
    planning_horizon_hours: float,
    *,
    target_hours: float = 48.0,
) -> float:
    """Return conservative LOAD energy for the unmodelled horizon tail.

    Missing or stale Day 3 data must not be replaced by invented PV.  The safe
    fallback assumes zero PV for the hours missing from the target horizon and
    retains enough battery energy for the average household load instead.
    """
    missing_hours = max(target_hours - max(planning_horizon_hours, 0.0), 0.0)
    return max(average_daily_load_kwh, 0.0) * missing_hours / 24.0


def horizon_gap_expensive_load_reserve_kwh(
    average_daily_load_kwh: float,
    horizon_end: datetime,
    missing_hours: float,
    schedule: TariffSchedule,
    *,
    charge_power_kw: float | None = None,
    battery_charge_power_kw: float | None = None,
    charge_efficiency_percent: float = 100.0,
    discharge_efficiency_percent: float = 100.0,
    maximum_stored_energy_kwh: float | None = None,
) -> tuple[float, float]:
    """Return initial stored energy needed across an unseen zero-PV tail.

    The calculation walks the complete tail backwards. Expensive blocks add
    LOAD demand; low blocks subtract only the energy that can really be stored
    after the same shared Grid Charge budget supplies the home and after BMS,
    conversion and storage-headroom limits. Therefore an early low window does
    not erase a later peak when a cold home or a low BMS limit makes that window
    insufficient. UTC stepping preserves the real number of slots over DST.
    """
    bounded_gap = max(missing_hours, 0.0)
    if bounded_gap <= _EPSILON:
        return 0.0, 0.0
    charge_efficiency = min(
        max(charge_efficiency_percent / 100.0, 0.01),
        1.0,
    )
    discharge_efficiency = min(
        max(discharge_efficiency_percent / 100.0, 0.01),
        1.0,
    )
    headroom = (
        max(maximum_stored_energy_kwh, 0.0)
        if maximum_stored_energy_kwh is not None
        else float("inf")
    )
    slot_model: list[tuple[str, float, float, float]] = []
    elapsed_hours = 0.0
    cursor_utc = horizon_end.astimezone(timezone.utc)
    while elapsed_hours + _EPSILON < bounded_gap:
        slot_hours = min(0.5, bounded_gap - elapsed_hours)
        cursor = cursor_utc.astimezone(horizon_end.tzinfo)
        zone = tariff_rate(cursor, schedule)[1]
        load_kwh = max(average_daily_load_kwh, 0.0) * slot_hours / 24.0
        stored_kwh = 0.0
        if zone in {"low", "g11"}:
            grid_budget_kwh = (
                max(charge_power_kw, 0.0) * slot_hours
                if charge_power_kw is not None
                else float("inf")
            )
            battery_ac_kwh = max(grid_budget_kwh - load_kwh, 0.0)
            bms_stored_kwh = (
                max(battery_charge_power_kw, 0.0) * slot_hours
                if battery_charge_power_kw is not None
                else float("inf")
            )
            stored_kwh = min(
                battery_ac_kwh * charge_efficiency,
                bms_stored_kwh,
                headroom,
            )
        slot_model.append((zone, load_kwh, stored_kwh, slot_hours))
        elapsed_hours += slot_hours
        cursor_utc += SLOT

    required_stored_kwh = 0.0
    expensive_hours = 0.0
    for zone, load_kwh, storable_kwh, slot_hours in reversed(slot_model):
        if zone in {"low", "g11"}:
            required_stored_kwh = max(
                required_stored_kwh - storable_kwh,
                0.0,
            )
        else:
            required_stored_kwh = min(
                required_stored_kwh + load_kwh / discharge_efficiency,
                headroom,
            )
            expensive_hours += slot_hours
    return required_stored_kwh, expensive_hours


def _in_window(minute: int, start: int, end: int) -> bool:
    """Return whether a minute belongs to a possibly overnight window."""
    if start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def _easter_sunday(year: int) -> date:
    """Return Gregorian Easter Sunday using the Meeus/Jones/Butcher method."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    length = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * length) // 451
    month = (h + length - 7 * m + 114) // 31
    day = (h + length - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def is_polish_public_holiday(value: date) -> bool:
    """Return whether ``value`` is a statutory Polish public holiday."""
    if (value.month, value.day) in {
        (1, 1),
        (1, 6),
        (5, 1),
        (5, 3),
        (8, 15),
        (11, 1),
        (11, 11),
        (12, 24),
        (12, 25),
        (12, 26),
    }:
        return True
    easter = _easter_sunday(value.year)
    return value in {
        easter,
        easter + timedelta(days=1),
        easter + timedelta(days=49),
        easter + timedelta(days=60),
    }


def tariff_rate(
    start: datetime,
    schedule: TariffSchedule,
) -> tuple[float, str]:
    """Return the marginal price and zone for one half-hour slot."""
    if schedule.operator != MANUAL_OPERATOR:
        profile = get_tariff_profile(schedule.operator, schedule.tariff_type)
        if profile is not None:
            if not profile_is_valid(profile, start.date()):
                raise ValueError(
                    "official tariff profile does not cover planning slot "
                    f"{start.date().isoformat()}"
                )
            return profile_rate(
                start,
                profile,
                is_public_holiday=is_polish_public_holiday(start.date()),
            )
    tariff_type = schedule.tariff_type.casefold().replace(" ", "")
    if tariff_type == "g11":
        return schedule.g11_price_pln_kwh, "g11"

    low_day = (
        schedule.weekend_low_price and start.weekday() >= 5
    ) or (
        schedule.polish_holidays_low_price
        and is_polish_public_holiday(start.date())
    )
    minute = start.hour * 60 + start.minute
    if low_day or any(
        _in_window(minute, window_start, window_end)
        for window_start, window_end in schedule.cheap_windows
    ):
        return schedule.low_price_pln_kwh, "low"

    if tariff_type == "g13" and any(
        _in_window(minute, window_start, window_end)
        for window_start, window_end in schedule.medium_windows
    ):
        return schedule.medium_price_pln_kwh, "medium"

    return schedule.peak_price_pln_kwh, "peak"


def _night_slot(minute: int, start: int, end: int) -> bool:
    return _in_window(minute, start, end)


def _slot_loads(settings: TariffOptimizerInput, starts: list[datetime]) -> list[float]:
    """Distribute daily and protected-night demand across future slots."""
    if settings.load_by_slot_kwh:
        return [
            max(settings.load_by_slot_kwh.get(start, 0.0), 0.0)
            for start in starts
        ]

    daily = max(settings.average_daily_load_kwh, 0.0)
    night = settings.average_night_load_kwh
    if night is None:
        night = daily * 0.45
    night = min(max(night, 0.0), daily)

    night_slots_per_day = sum(
        _night_slot(
            hour * 60 + minute,
            settings.night_start_minute,
            settings.night_end_minute,
        )
        for hour in range(24)
        for minute in (0, 30)
    )
    day_slots_per_day = max(48 - night_slots_per_day, 1)
    night_per_slot = night / max(night_slots_per_day, 1)
    day_per_slot = (daily - night) / day_slots_per_day
    return [
        night_per_slot
        if _night_slot(
            start.hour * 60 + start.minute,
            settings.night_start_minute,
            settings.night_end_minute,
        )
        else day_per_slot
        for start in starts
    ]


def _effective_terminal_reserve_soc_percent(
    settings: TariffOptimizerInput,
) -> float:
    """Return the terminal SOC threshold that the simulation really applies."""
    reserve_soc = min(max(settings.reserve_soc_percent, 0.0), 100.0)
    if settings.schedule.tariff_type.casefold().replace(" ", "") == "g11":
        # G11 has no cheaper execution window.  A virtual terminal reserve must
        # not manufacture a battery cycle at the same tariff.
        return min(
            reserve_soc,
            min(max(settings.battery_soc_percent, 0.0), 100.0),
        )
    requested = (
        settings.terminal_reserve_soc_percent
        if settings.terminal_reserve_soc_percent is not None
        else reserve_soc
    )
    return min(
        max(requested, reserve_soc),
        min(max(settings.maximum_soc_percent, reserve_soc), 100.0),
    )


def _simulate(
    settings: TariffOptimizerInput,
    starts: list[datetime],
    loads: list[float],
    imports: dict[int, float],
    supports: dict[int, float],
    rates: list[tuple[float, str]],
    slot_fractions: list[float],
) -> _Simulation:
    capacity = max(settings.battery_capacity_kwh, 0.001)
    reserve = capacity * min(max(settings.reserve_soc_percent, 0.0), 100.0) / 100.0
    maximum = capacity * min(max(settings.maximum_soc_percent, 0.0), 100.0) / 100.0
    maximum = max(maximum, reserve)
    # The reserve and configured maximum are control thresholds, not physical
    # clamps on the energy already present.  Starting below reserve must not
    # create energy, while starting above the configured charge target must not
    # silently discard it.
    battery = min(
        max(capacity * settings.battery_soc_percent / 100.0, 0.0),
        capacity,
    )
    charge_efficiency = min(max(settings.charge_efficiency_percent / 100.0, 0.01), 1.0)
    discharge_efficiency = min(
        max(settings.discharge_efficiency_percent / 100.0, 0.01),
        1.0,
    )
    shortage = 0.0
    first_shortage: int | None = None
    accepted: dict[int, float] = {}
    accepted_support: dict[int, float] = {}
    stored: dict[int, float] = {}
    battery_after: dict[int, float] = {}
    uncovered_import: dict[int, float] = {}
    traced_pv: dict[int, float] = {}
    traced_load: dict[int, float] = {}
    battery_delta: dict[int, float] = {}
    grid_import: dict[int, float] = {}
    grid_export: dict[int, float] = {}

    for index, start in enumerate(starts):
        battery_before = battery
        fraction = slot_fractions[index]
        slot_hours = 0.5 * fraction
        pv = max(settings.pv_by_slot_kwh.get(start, 0.0), 0.0) * fraction
        traced_pv[index] = pv
        traced_load[index] = loads[index]
        net = pv - loads[index]
        requested_support = max(supports.get(index, 0.0), 0.0)
        requested_import = max(imports.get(index, 0.0), 0.0)
        grid_charge_active = (
            requested_support > _EPSILON or requested_import > _EPSILON
        )
        # Grid Charge has one shared AC input budget.  Once enabled, the
        # inverter supplies the complete remaining home load first and only
        # the unused part of the configured power can charge the battery.
        grid_budget = max(settings.charge_power_kw, 0.0) * slot_hours
        battery_charge_budget = (
            max(settings.battery_charge_power_kw, 0.0) * slot_hours
            if settings.battery_charge_power_kw is not None
            else float("inf")
        )
        direct_grid = 0.0
        if grid_charge_active and net < 0 and grid_budget > _EPSILON:
            direct_grid = min(-net, grid_budget)
            net += direct_grid
            accepted_support[index] = direct_grid
        remaining_grid_budget = max(grid_budget - direct_grid, 0.0)
        interval_grid_export = 0.0
        if net >= 0:
            # PV surplus is subject to the same physical BMS/inverter charging
            # limit and conversion loss as energy imported from the grid.
            pv_battery_limit = (
                settings.pv_charge_power_kw
                if settings.pv_charge_power_kw is not None
                else settings.charge_power_kw * charge_efficiency
            )
            stored_from_pv = min(
                net * charge_efficiency,
                max(pv_battery_limit, 0.0) * slot_hours,
                battery_charge_budget,
                max(maximum - battery, 0.0),
            )
            battery += max(stored_from_pv, 0.0)
            interval_grid_export = max(
                net - max(stored_from_pv, 0.0) / charge_efficiency,
                0.0,
            )
            battery_charge_budget = max(
                battery_charge_budget - max(stored_from_pv, 0.0),
                0.0,
            )
        else:
            required_from_battery = -net / discharge_efficiency
            available = max(battery - reserve, 0.0)
            discharge_budget = (
                max(settings.battery_discharge_power_kw, 0.0) * slot_hours
                if settings.battery_discharge_power_kw is not None
                else float("inf")
            )
            discharged = min(required_from_battery, available, discharge_budget)
            battery -= discharged
            uncovered = max(-net - discharged * discharge_efficiency, 0.0)
            if uncovered > _EPSILON:
                shortage += uncovered
                uncovered_import[index] = uncovered
                if first_shortage is None:
                    first_shortage = index

        if requested_import > 0:
            battery_ac_limit = battery_charge_budget / charge_efficiency
            accepted_import = min(
                requested_import,
                remaining_grid_budget,
                battery_ac_limit,
                max(maximum - battery, 0.0) / charge_efficiency,
            )
            stored_energy = accepted_import * charge_efficiency
            battery += stored_energy
            accepted[index] = accepted_import
            stored[index] = stored_energy
        else:
            accepted_import = 0.0
        battery_after[index] = battery
        interval_uncovered = uncovered_import.get(index, 0.0)
        battery_delta[index] = battery - battery_before
        gross_grid_import = direct_grid + accepted_import + interval_uncovered
        grid_import[index] = max(gross_grid_import - interval_grid_export, 0.0)
        grid_export[index] = max(interval_grid_export - gross_grid_import, 0.0)

    terminal_soc = _effective_terminal_reserve_soc_percent(settings)
    terminal_target = min(
        max(capacity * min(max(terminal_soc, 0.0), 100.0) / 100.0, reserve),
        maximum,
    )
    if settings.schedule.tariff_type.casefold().replace(" ", "") == "g11":
        terminal_target = min(
            reserve,
            capacity
            * min(max(settings.battery_soc_percent, 0.0), 100.0)
            / 100.0,
        )
    terminal_shortfall = max(terminal_target - battery, 0.0)
    terminal_import = 0.0
    terminal_cost_adjustment = 0.0
    if terminal_shortfall > _EPSILON and starts:
        # Treat the missing terminal energy as a virtual import at the end of
        # the horizon.  Earlier low-price charging can replace it, so the same
        # cost minimisation loop can protect the following unpriced period.
        terminal_import = terminal_shortfall / charge_efficiency
        last_index = len(starts) - 1
        shortage += terminal_import
        uncovered_import[last_index] = (
            uncovered_import.get(last_index, 0.0) + terminal_import
        )
        if first_shortage is None:
            first_shortage = last_index
        terminal_reference_rate = max(
            settings.schedule.medium_price_pln_kwh,
            settings.schedule.peak_price_pln_kwh,
            rates[last_index][0],
            *(
                price
                for price, zone in rates
                if zone not in {"low", "g11"}
            ),
        )
        terminal_cost_adjustment = max(
            terminal_reference_rate - rates[last_index][0],
            0.0,
        ) * terminal_import

    explicit_grid_import = sum(accepted.values()) + sum(accepted_support.values())
    total_grid_import = explicit_grid_import + sum(uncovered_import.values())
    total_grid_cost = sum(
        (
            accepted.get(index, 0.0)
            + accepted_support.get(index, 0.0)
            + uncovered_import.get(index, 0.0)
        )
        * rates[index][0]
        for index in range(len(starts))
    )
    total_optimization_cost = (
        total_grid_cost
        + terminal_cost_adjustment
        + sum(stored.values()) * max(
            settings.battery_wear_cost_pln_kwh,
            0.0,
        )
    )
    return _Simulation(
        shortage_kwh=shortage,
        first_shortage_index=first_shortage,
        ending_battery_kwh=battery,
        accepted_import_kwh=accepted,
        accepted_support_kwh=accepted_support,
        stored_import_kwh=stored,
        battery_after_kwh=battery_after,
        uncovered_import_kwh=uncovered_import,
        total_grid_import_kwh=total_grid_import,
        total_grid_cost_pln=total_grid_cost,
        total_optimization_cost_pln=total_optimization_cost,
        terminal_shortfall_kwh=terminal_shortfall,
        terminal_import_kwh=terminal_import,
        pv_kwh=traced_pv,
        load_kwh=traced_load,
        battery_delta_kwh=battery_delta,
        grid_import_kwh=grid_import,
        grid_export_kwh=grid_export,
    )


def _tariff_timeline_trace(
    *,
    settings: TariffOptimizerInput,
    starts: list[datetime],
    slot_fractions: list[float],
    rates: list[tuple[float, str]],
    baseline: _Simulation,
    selected: _Simulation,
    status_code: str,
    planning_horizon_hours: float,
) -> OptimizerTimelineTrace | None:
    """Merge already-executed baseline/selected simulations into a trace."""

    if (
        not settings.control_inputs_fresh
        or not starts
        or len(starts) != len(slot_fractions)
        or len(starts) != len(rates)
    ):
        return None
    capacity = max(settings.battery_capacity_kwh, 0.001)
    protected_soc = min(max(settings.reserve_soc_percent, 0.0), 100.0)
    if status_code == "insufficient_cheap_window":
        timeline_quality = "partial"
        timeline_blocker = status_code
    elif planning_horizon_hours < 48.0 - 0.01:
        timeline_quality = "partial"
        timeline_blocker = "planning_horizon_limited"
    else:
        timeline_quality = "complete"
        timeline_blocker = None
    points: list[TimelineTracePoint] = []
    first_fraction = min(max(slot_fractions[0], 0.0), 1.0)
    first_end = starts[0].astimezone(timezone.utc) + SLOT
    horizon_limit = first_end - SLOT * first_fraction + timedelta(hours=48)
    for index, slot_start in enumerate(starts):
        fraction = min(max(slot_fractions[index], 0.0), 1.0)
        end = slot_start.astimezone(timezone.utc) + SLOT
        start = end - SLOT * fraction
        if end > horizon_limit:
            break
        duration_hours = max((end - start).total_seconds() / 3600.0, 1e-9)
        battery_import = selected.accepted_import_kwh.get(index, 0.0)
        direct_support = selected.accepted_support_kwh.get(index, 0.0)
        planned_import = battery_import + direct_support
        if battery_import > _EPSILON and direct_support > _EPSILON:
            action = "grid_support_and_charge"
        elif battery_import > _EPSILON:
            action = "battery_charge"
        elif direct_support > _EPSILON:
            action = "grid_support"
        else:
            action = "idle"
        price, zone = rates[index]
        selected_point = planned_import > 0.001
        points.append(
            TimelineTracePoint(
                start=start,
                end=end,
                pv_kwh=selected.pv_kwh[index],
                load_kwh=selected.load_kwh[index],
                battery_delta_kwh=selected.battery_delta_kwh[index],
                grid_import_kwh=selected.grid_import_kwh[index],
                grid_export_kwh=selected.grid_export_kwh[index],
                soc_percent=min(
                    max(selected.battery_after_kwh[index] / capacity * 100.0, 0.0),
                    100.0,
                ),
                baseline_soc_percent=min(
                    max(baseline.battery_after_kwh[index] / capacity * 100.0, 0.0),
                    100.0,
                ),
                protected_soc_floor_percent=protected_soc,
                action_code=action,
                selected=selected_point,
                quality=timeline_quality,
                policy=TariffPolicyPoint(
                    buy_price_pln_kwh=price,
                    tariff_zone=zone,
                    planned_import_kwh=planned_import,
                    planned_charge_kw=(
                        selected.stored_import_kwh.get(index, 0.0) / duration_hours
                    ),
                    expected_cost_pln=planned_import * price,
                    # The existing optimizer exposes aggregate savings only.
                    expected_saving_pln=None,
                ),
                target_soc_percent=(
                    min(
                        max(
                            selected.battery_after_kwh[index]
                            / capacity
                            * 100.0,
                            0.0,
                        ),
                        100.0,
                    )
                    if selected_point
                    else None
                ),
            )
        )
    return OptimizerTimelineTrace(
        policy_id="tariff",
        points=tuple(points),
        quality=timeline_quality,
        blocker_code=timeline_blocker,
    )


def optimize_tariff_charging(
    settings: TariffOptimizerInput,
) -> TariffOptimizerResult:
    """Build the least-cost feasible charging plan for two or three days."""
    now_slot = floor_half_hour(settings.now)
    (
        horizon_days,
        horizon_end,
        planning_horizon_hours,
        horizon_extended,
    ) = resolve_planning_horizon(
        settings.now,
        settings.horizon_days,
        minimum_hours=48.0 if int(settings.horizon_days) >= 3 else 0.0,
    )
    starts: list[datetime] = []
    cursor_utc = now_slot.astimezone(timezone.utc)
    horizon_end_utc = horizon_end.astimezone(timezone.utc)
    while cursor_utc < horizon_end_utc:
        starts.append(cursor_utc.astimezone(settings.now.tzinfo))
        cursor_utc += SLOT

    seconds_into_slot = (
        (settings.now.minute % 30) * 60
        + settings.now.second
        + settings.now.microsecond / 1_000_000.0
    )
    first_fraction = min(
        max((30 * 60 - seconds_into_slot) / (30 * 60), 0.0),
        1.0,
    )
    slot_fractions = [1.0 for _ in starts]
    if slot_fractions:
        slot_fractions[0] = first_fraction
    loads = _slot_loads(settings, starts)
    if loads:
        loads[0] *= first_fraction
    current_slot_load_source = "profile"
    if (
        loads
        and settings.current_load_power_kw is not None
        and isfinite(settings.current_load_power_kw)
        and settings.current_load_power_kw >= 0.0
    ):
        loads[0] = settings.current_load_power_kw * 0.5 * first_fraction
        current_slot_load_source = "live"
    current_slot_pv_source = "forecast"
    effective_pv_by_slot = settings.pv_by_slot_kwh
    if (
        starts
        and settings.current_pv_power_kw is not None
        and isfinite(settings.current_pv_power_kw)
        and settings.current_pv_power_kw >= 0.0
    ):
        effective_pv_by_slot = dict(settings.pv_by_slot_kwh)
        # Store a complete-slot equivalent because the simulation applies the
        # same ``first_fraction`` as every other current-slot input.
        effective_pv_by_slot[starts[0]] = settings.current_pv_power_kw * 0.5
        current_slot_pv_source = "live"
    effective_settings = (
        replace(settings, pv_by_slot_kwh=effective_pv_by_slot)
        if effective_pv_by_slot is not settings.pv_by_slot_kwh
        else settings
    )
    rates = [tariff_rate(start, settings.schedule) for start in starts]
    base_loads = list(loads)
    load_risk_multiplier = 1.0
    if (
        settings.load_history_days >= 5
        and settings.conservative_daily_load_kwh is not None
        and settings.average_daily_load_kwh > _EPSILON
    ):
        load_risk_multiplier = min(
            max(
                settings.conservative_daily_load_kwh
                / settings.average_daily_load_kwh,
                1.0,
            ),
            1.35,
        )

    expensive_window_load_buffers: list[ExpensiveWindowLoadBuffer] = []
    index = 0
    while index < len(starts):
        if rates[index][1] in {"low", "g11"}:
            index += 1
            continue
        window_start = index
        while index < len(starts) and rates[index][1] not in {"low", "g11"}:
            index += 1
        window_end = index
        expected_load = sum(base_loads[window_start:window_end])
        # A fresh live sample is already more authoritative for the unfinished
        # current interval. Apply the upper scenario only to complete future
        # intervals so the buffer cannot double-count an observed cold spike.
        risk_start = window_start + (
            1
            if window_start == 0 and current_slot_load_source == "live"
            else 0
        )
        for risk_index in range(risk_start, window_end):
            loads[risk_index] *= load_risk_multiplier
        conservative_load = sum(loads[window_start:window_end])
        expensive_window_load_buffers.append(
            ExpensiveWindowLoadBuffer(
                start=starts[window_start],
                end=(
                    starts[window_end - 1].astimezone(timezone.utc) + SLOT
                ).astimezone(settings.now.tzinfo),
                expected_load_kwh=expected_load,
                conservative_load_kwh=conservative_load,
                buffer_kwh=max(conservative_load - expected_load, 0.0),
            )
        )

    morning_protection_active = False
    morning_protection_mode = "not_applicable"
    morning_window_start: datetime | None = None
    morning_window_end: datetime | None = None
    morning_expected_pv = 0.0
    morning_conservative_pv = 0.0
    # At the end of an overnight low-price run, protect the complete following
    # morning peak with Solcast P10. If P10 is unavailable and historical load
    # or forecast accuracy is highly variable, use the safe zero-PV scenario.
    if starts and rates[0][1] == "low":
        morning_start_index = next(
            (
                candidate
                for candidate in range(1, len(starts))
                if rates[candidate][1] not in {"low", "g11"}
            ),
            None,
        )
        if (
            morning_start_index is not None
            and starts[morning_start_index].hour <= 8
        ):
            morning_end_index = morning_start_index
            while (
                morning_end_index < len(starts)
                and rates[morning_end_index][1] not in {"low", "g11"}
            ):
                morning_end_index += 1
            morning_window_start = starts[morning_start_index]
            morning_window_end = (
                starts[morning_end_index - 1].astimezone(timezone.utc) + SLOT
            ).astimezone(settings.now.tzinfo)
            morning_expected_pv = sum(
                max(effective_pv_by_slot.get(starts[item], 0.0), 0.0)
                * slot_fractions[item]
                for item in range(morning_start_index, morning_end_index)
            )
            p10_available = (
                morning_window_start.date() in settings.pv_p10_available_dates
            )
            high_variability = (
                settings.load_uncertainty_ratio >= 0.20
                or settings.forecast_uncertainty_ratio >= 0.18
            )
            if p10_available or high_variability:
                protected_pv = dict(effective_pv_by_slot)
                if p10_available:
                    p10_map = settings.pv_p10_by_slot_kwh or {}
                    morning_protection_mode = "solcast_p10"
                    for item in range(morning_start_index, morning_end_index):
                        start = starts[item]
                        protected_pv[start] = min(
                            max(effective_pv_by_slot.get(start, 0.0), 0.0),
                            max(p10_map.get(start, 0.0), 0.0),
                        )
                else:
                    morning_protection_mode = "zero_pv_high_variability"
                    for item in range(morning_start_index, morning_end_index):
                        protected_pv[starts[item]] = 0.0
                effective_pv_by_slot = protected_pv
                effective_settings = replace(
                    effective_settings,
                    pv_by_slot_kwh=effective_pv_by_slot,
                )
                morning_protection_active = True
                morning_conservative_pv = sum(
                    max(effective_pv_by_slot.get(starts[item], 0.0), 0.0)
                    * slot_fractions[item]
                    for item in range(morning_start_index, morning_end_index)
                )
            else:
                morning_protection_mode = "p10_unavailable_stable_history"
                morning_conservative_pv = morning_expected_pv

    modeled_pv = sum(
        max(effective_pv_by_slot.get(start, 0.0), 0.0) * fraction
        for start, fraction in zip(starts, slot_fractions)
    )
    baseline = _simulate(
        effective_settings,
        starts,
        loads,
        {},
        {},
        rates,
        slot_fractions,
    )
    planned: dict[int, float] = {}
    planned_support: dict[int, float] = {}
    allocation_provenance: dict[int, int] = {}
    simulation = baseline
    charge_power = max(settings.charge_power_kw, 0.0)
    requested_charge_power = max(
        settings.requested_charge_power_kw
        if settings.requested_charge_power_kw is not None
        else charge_power,
        0.0,
    )
    charge_efficiency = min(
        max(settings.charge_efficiency_percent / 100.0, 0.01), 1.0
    )
    block_limits = [charge_power * 0.5 for _ in starts]
    if block_limits:
        block_limits[0] *= first_fraction
    battery_slot_limits = [
        min(limit, max(settings.battery_charge_power_kw, 0.0) * 0.5 * fraction)
        if settings.battery_charge_power_kw is not None
        else limit
        for limit, fraction in zip(block_limits, slot_fractions)
    ]
    support_limits = [
        max(
            loads[index]
            - max(effective_pv_by_slot.get(start, 0.0), 0.0)
            * slot_fractions[index],
            0.0,
        )
        for index, start in enumerate(starts)
    ]

    composite_reserve_soc = min(max(settings.reserve_soc_percent, 0.0), 100.0)
    base_reserve_soc = min(
        max(
            settings.base_reserve_soc_percent
            if settings.base_reserve_soc_percent is not None
            else composite_reserve_soc,
            0.0,
        ),
        composite_reserve_soc,
    )
    base_reserve_energy = (
        max(settings.battery_capacity_kwh, 0.001)
        * base_reserve_soc
        / 100.0
    )
    initial_battery_energy = (
        max(settings.battery_capacity_kwh, 0.001)
        * min(max(settings.battery_soc_percent, 0.0), 100.0)
        / 100.0
    )
    hard_reserve_deficit = max(
        base_reserve_energy - initial_battery_energy,
        0.0,
    )
    hard_reserve_restoration_required = False
    hard_reserve_restored_by_near_term_pv = False
    hard_reserve_deferral_source = (
        "not_required" if hard_reserve_deficit <= _EPSILON else "none"
    )
    soc_limits_conflict = (
        settings.maximum_soc_percent + _EPSILON < composite_reserve_soc
    )
    if soc_limits_conflict:
        # Invalid limits must fail closed. Keep the complete baseline
        # diagnostics, but make every Grid Charge trial physically inert.
        effective_settings = replace(effective_settings, charge_power_kw=0.0)
        charge_power = 0.0
        block_limits = [0.0 for _ in starts]
        battery_slot_limits = [0.0 for _ in starts]

    # Falling below the user's actual Self-Use floor is a hard reserve deficit,
    # unlike the small automatic safety margin. Restore it in the earliest low
    # slot. A forecast must never defer this restoration: only fresh measured
    # PV>LOAD that has remained stable and can physically replace the missing
    # energy inside two hours is accepted.
    if (
        not soc_limits_conflict
        and settings.schedule.tariff_type.casefold().replace(" ", "") != "g11"
        and hard_reserve_deficit > _EPSILON
    ):
        live_surplus_kw = max(
            (
                settings.current_pv_power_kw
                - settings.current_load_power_kw
            )
            if (
                settings.current_pv_power_kw is not None
                and settings.current_load_power_kw is not None
                and isfinite(settings.current_pv_power_kw)
                and isfinite(settings.current_load_power_kw)
            )
            else 0.0,
            0.0,
        )
        live_charge_limit_kw = min(
            limit
            for limit in (
                live_surplus_kw,
                (
                    max(settings.pv_charge_power_kw, 0.0)
                    if settings.pv_charge_power_kw is not None
                    else live_surplus_kw
                ),
                (
                    max(settings.battery_charge_power_kw, 0.0)
                    if settings.battery_charge_power_kw is not None
                    else live_surplus_kw
                ),
                (
                    abs(settings.current_battery_power_kw)
                    if (
                        settings.current_battery_power_kw is not None
                        and isfinite(settings.current_battery_power_kw)
                        and settings.current_battery_power_kw < 0.0
                    )
                    else 0.0
                ),
            )
        )
        live_surplus_two_hour_stored_kwh = (
            live_charge_limit_kw * 2.0 * charge_efficiency
        )
        hard_reserve_restored_by_near_term_pv = (
            settings.live_pv_surplus_stable
            and live_charge_limit_kw > 0.20
            and settings.current_battery_power_kw is not None
            and isfinite(settings.current_battery_power_kw)
            and settings.current_battery_power_kw < -0.20
            and live_surplus_two_hour_stored_kwh
            >= hard_reserve_deficit - _EPSILON
        )
        if hard_reserve_restored_by_near_term_pv:
            hard_reserve_deferral_source = "stable_live_pv_surplus"
        if not hard_reserve_restored_by_near_term_pv:
            hard_reserve_restoration_required = True
            hard_reserve_deferral_source = (
                "live_surplus_not_stable"
                if live_surplus_kw > 0.20
                else "no_live_pv_surplus"
            )
            # Forecast PV is not evidence that a hard reserve below the user's
            # Self-Use floor will recover. Until live PV charging is stable,
            # build this mandatory restoration against a zero-PV scenario up
            # to the end of the first available low window. A later SOC update
            # removes the plan automatically if real production did restore
            # the floor before that window starts.
            first_low_index = next(
                (
                    item
                    for item, (_, zone) in enumerate(rates)
                    if zone == "low"
                ),
                None,
            )
            conservative_end_index = len(starts)
            if first_low_index is not None:
                conservative_end_index = first_low_index
                while (
                    conservative_end_index < len(starts)
                    and rates[conservative_end_index][1] == "low"
                ):
                    conservative_end_index += 1
            hard_reserve_pv = dict(effective_pv_by_slot)
            for item in range(conservative_end_index):
                hard_reserve_pv[starts[item]] = 0.0
            effective_pv_by_slot = hard_reserve_pv
            effective_settings = replace(
                effective_settings,
                pv_by_slot_kwh=effective_pv_by_slot,
            )
            if current_slot_pv_source == "live" and conservative_end_index > 0:
                current_slot_pv_source = "live_unstable_reserve_guard"
            modeled_pv = sum(
                max(effective_pv_by_slot.get(start, 0.0), 0.0) * fraction
                for start, fraction in zip(starts, slot_fractions)
            )
            baseline = _simulate(
                effective_settings,
                starts,
                loads,
                {},
                {},
                rates,
                slot_fractions,
            )
            simulation = baseline
            support_limits = [
                max(
                    loads[item]
                    - max(effective_pv_by_slot.get(start, 0.0), 0.0)
                    * slot_fractions[item],
                    0.0,
                )
                for item, start in enumerate(starts)
            ]
            for index in range(len(starts)):
                if rates[index][1] != "low":
                    continue
                projected_before = (
                    simulation.battery_after_kwh.get(
                        index - 1,
                        initial_battery_energy,
                    )
                    if index > 0
                    else initial_battery_energy
                )
                if projected_before >= base_reserve_energy - _EPSILON:
                    break
                available = block_limits[index] - planned.get(index, 0.0)
                if available <= _EPSILON:
                    continue
                base_amount = planned.get(index, 0.0)
                full_trial = dict(planned)
                full_trial[index] = base_amount + available
                full_simulation = _simulate(
                    effective_settings,
                    starts,
                    loads,
                    full_trial,
                    planned_support,
                    rates,
                    slot_fractions,
                )
                full_projected = full_simulation.battery_after_kwh.get(
                    index,
                    projected_before,
                )
                if full_projected <= projected_before + _EPSILON:
                    continue
                if full_projected >= base_reserve_energy - _EPSILON:
                    lower = 0.0
                    upper = available
                    selected_simulation = full_simulation
                    for _ in range(18):
                        middle = (lower + upper) / 2.0
                        trial = dict(planned)
                        trial[index] = base_amount + middle
                        trial_simulation = _simulate(
                            effective_settings,
                            starts,
                            loads,
                            trial,
                            planned_support,
                            rates,
                            slot_fractions,
                        )
                        trial_projected = trial_simulation.battery_after_kwh.get(
                            index,
                            projected_before,
                        )
                        if trial_projected >= base_reserve_energy - _EPSILON:
                            upper = middle
                            selected_simulation = trial_simulation
                        else:
                            lower = middle
                    planned[index] = base_amount + upper
                    allocation_provenance[index] = (
                        allocation_provenance.get(index, 0)
                        | _ALLOCATION_REQUIRED_ENERGY
                    )
                    simulation = selected_simulation
                    break
                planned[index] = base_amount + available
                allocation_provenance[index] = (
                    allocation_provenance.get(index, 0)
                    | _ALLOCATION_REQUIRED_ENERGY
                )
                simulation = full_simulation

    # The configured Self-Use reserve plus the user's safety correction is a
    # real planning floor.  Being below that floor during a long low-price
    # period is not, however, a reason to charge immediately.  Self-Use can
    # safely buy the unavoidable household deficit at the same low rate.  The
    # reserve only needs to be restored before the next non-low slot, and only
    # when forecast PV has not restored it by then.
    #
    # Build this reserve-restoration run backwards from that deadline.  This
    # gives the inverter one stable, contiguous charge rather than repeatedly
    # taking small bites from every replan (especially on all-low G12w
    # weekends).  A trial simulation accounts for home LOAD consuming part of
    # the shared Grid Charge power and for the battery/BMS charge limit.
    reserve_energy = (
        max(settings.battery_capacity_kwh, 0.001)
        * min(max(settings.reserve_soc_percent, 0.0), 100.0)
        / 100.0
    )
    if (
        settings.schedule.tariff_type.casefold().replace(" ", "") != "g11"
        and settings.battery_capacity_kwh
        * min(max(settings.battery_soc_percent, 0.0), 100.0)
        / 100.0
        < reserve_energy - _EPSILON
    ):
        reserve_deadline = next(
            (
                index
                for index, (_, zone) in enumerate(rates)
                if zone not in {"low", "g11"}
            ),
            None,
        )
        if reserve_deadline is not None and reserve_deadline > 0:
            deadline_slot = reserve_deadline - 1
            projected_at_deadline = simulation.battery_after_kwh.get(
                deadline_slot,
                settings.battery_capacity_kwh
                * settings.battery_soc_percent
                / 100.0,
            )
            if projected_at_deadline < reserve_energy - _EPSILON:
                for index in range(reserve_deadline - 1, -1, -1):
                    if rates[index][1] != "low":
                        continue
                    available = block_limits[index] - planned.get(index, 0.0)
                    if available <= _EPSILON:
                        continue

                    base_amount = planned.get(index, 0.0)
                    full_trial = dict(planned)
                    full_trial[index] = base_amount + available
                    full_simulation = _simulate(
                        effective_settings,
                        starts,
                        loads,
                        full_trial,
                        planned_support,
                        rates,
                        slot_fractions,
                    )
                    full_projected = full_simulation.battery_after_kwh.get(
                        deadline_slot,
                        projected_at_deadline,
                    )
                    if full_projected <= projected_at_deadline + _EPSILON:
                        continue

                    if full_projected >= reserve_energy - _EPSILON:
                        # Find the smallest import that reaches the reserve at
                        # the deadline.  This avoids filling more than the
                        # requested safety floor merely because a whole block
                        # was available.
                        lower = 0.0
                        upper = available
                        selected_simulation = full_simulation
                        for _ in range(18):
                            middle = (lower + upper) / 2.0
                            trial = dict(planned)
                            trial[index] = base_amount + middle
                            trial_simulation = _simulate(
                                effective_settings,
                                starts,
                                loads,
                                trial,
                                planned_support,
                                rates,
                                slot_fractions,
                            )
                            trial_projected = (
                                trial_simulation.battery_after_kwh.get(
                                    deadline_slot,
                                    projected_at_deadline,
                                )
                            )
                            if trial_projected >= reserve_energy - _EPSILON:
                                upper = middle
                                selected_simulation = trial_simulation
                            else:
                                lower = middle
                        planned[index] = base_amount + upper
                        allocation_provenance[index] = (
                            allocation_provenance.get(index, 0)
                            | _ALLOCATION_REQUIRED_ENERGY
                        )
                        simulation = selected_simulation
                        break

                    planned[index] = base_amount + available
                    allocation_provenance[index] = (
                        allocation_provenance.get(index, 0)
                        | _ALLOCATION_REQUIRED_ENERGY
                    )
                    simulation = full_simulation
                    projected_at_deadline = full_projected

    # Half a kilowatt-hour provides sufficient precision for the dashboard and
    # keeps the repeated storage simulation inexpensive even for 230 kWh banks.
    quantum = max(min(charge_power * 0.5, 0.5), 0.05)
    max_iterations = max(ceil(baseline.shortage_kwh / 0.05) + len(starts) * 4, 100)
    uneconomic_low_trial_found = False
    for _ in range(max_iterations):
        if simulation.shortage_kwh <= 0.01:
            break
        first_shortage = simulation.first_shortage_index
        if first_shortage is None:
            break
        best: tuple[
            tuple[float, float, int, int],
            str,
            int,
            dict[int, float],
            dict[int, float],
            _Simulation,
        ] | None = None
        # An unavoidable shortage before the next low-price window must not
        # cancel the rest of the plan.  A later action cannot repair energy
        # already bought from the grid, but it can still reduce subsequent
        # expensive imports; the complete simulation accepts it only when the
        # total future shortage is genuinely reduced.
        for index in range(len(starts)):
            rate = rates[index][0]
            # Grid Charge is a low-zone actuator. Medium G13 pricing remains
            # part of the cost simulation, but it must never become an
            # execution window: the scheduler intentionally accepts only low.
            if rates[index][1] != "low":
                continue

            # Grid Charge is a mode for the complete slot: enabling it makes
            # the inverter supply the whole remaining home load from the grid.
            # A fractional support action would not match the hardware.
            support_remaining = support_limits[index]
            if (
                support_remaining > _EPSILON
                and index not in planned_support
            ):
                trial_support = dict(planned_support)
                trial_support[index] = support_remaining
                trial_simulation = _simulate(
                    effective_settings,
                    starts,
                    loads,
                    planned,
                    trial_support,
                    rates,
                    slot_fractions,
                )
                reduction = simulation.shortage_kwh - trial_simulation.shortage_kwh
                accepted_delta = (
                    trial_simulation.accepted_support_kwh.get(index, 0.0)
                    - simulation.accepted_support_kwh.get(index, 0.0)
                    + trial_simulation.accepted_import_kwh.get(index, 0.0)
                    - simulation.accepted_import_kwh.get(index, 0.0)
                )
                cost_reduction = (
                    simulation.total_optimization_cost_pln
                    - trial_simulation.total_optimization_cost_pln
                )
                feasible_reduction = (
                    reduction > 1e-5 and accepted_delta > _EPSILON
                )
                economically_beneficial = (
                    cost_reduction
                    > settings.minimum_saving_pln_kwh * accepted_delta
                )
                if feasible_reduction and not economically_beneficial:
                    uneconomic_low_trial_found = True
                if feasible_reduction and economically_beneficial:
                    score = (
                        -cost_reduction / accepted_delta,
                        rate,
                        index,
                        0,
                    )
                    candidate = (
                        score,
                        "grid_support",
                        index,
                        planned,
                        trial_support,
                        trial_simulation,
                    )
                    if best is None or candidate[0] < best[0]:
                        best = candidate

            charge_remaining = block_limits[index] - planned.get(index, 0.0)
            if charge_remaining > _EPSILON:
                increment = min(quantum, charge_remaining)
                trial_charge = dict(planned)
                trial_charge[index] = trial_charge.get(index, 0.0) + increment
                trial_simulation = _simulate(
                    effective_settings,
                    starts,
                    loads,
                    trial_charge,
                    planned_support,
                    rates,
                    slot_fractions,
                )
                reduction = simulation.shortage_kwh - trial_simulation.shortage_kwh
                accepted_delta = (
                    trial_simulation.accepted_import_kwh.get(index, 0.0)
                    - simulation.accepted_import_kwh.get(index, 0.0)
                    + trial_simulation.accepted_support_kwh.get(index, 0.0)
                    - simulation.accepted_support_kwh.get(index, 0.0)
                )
                cost_reduction = (
                    simulation.total_optimization_cost_pln
                    - trial_simulation.total_optimization_cost_pln
                )
                feasible_reduction = (
                    reduction > 1e-5 and accepted_delta > _EPSILON
                )
                economically_beneficial = (
                    cost_reduction
                    > settings.minimum_saving_pln_kwh * accepted_delta
                )
                if feasible_reduction and not economically_beneficial:
                    uneconomic_low_trial_found = True
                if feasible_reduction and economically_beneficial:
                    score = (
                        -cost_reduction / accepted_delta,
                        rate,
                        -index,
                        1,
                    )
                    candidate = (
                        score,
                        "battery_charge",
                        index,
                        trial_charge,
                        planned_support,
                        trial_simulation,
                    )
                    if best is None or candidate[0] < best[0]:
                        best = candidate

        if best is None:
            break
        _, _, selected_index, planned, planned_support, simulation = best
        allocation_provenance[selected_index] = (
            allocation_provenance.get(selected_index, 0)
            | _ALLOCATION_ECONOMIC
        )

    charges: list[PlannedCharge] = []
    charge_indices: list[int] = []
    capacity = max(settings.battery_capacity_kwh, 0.001)
    action_indices = sorted(
        set(simulation.accepted_import_kwh)
        | set(simulation.accepted_support_kwh)
    )
    for index in action_indices:
        battery_import = simulation.accepted_import_kwh.get(index, 0.0)
        direct_load = simulation.accepted_support_kwh.get(index, 0.0)
        grid_import = battery_import + direct_load
        if grid_import <= 0.001:
            continue
        price, zone = rates[index]
        if battery_import > 0.001 and direct_load > 0.001:
            action = "grid_support_and_charge"
        elif battery_import > 0.001:
            action = "battery_charge"
        else:
            action = "grid_support"
        charges.append(
            PlannedCharge(
                start=starts[index],
                price_pln_kwh=price,
                zone=zone,
                grid_import_kwh=grid_import,
                stored_energy_kwh=simulation.stored_import_kwh.get(index, 0.0),
                direct_load_kwh=direct_load,
                action=action,
                target_soc_percent=min(
                    max(
                        simulation.battery_after_kwh.get(index, 0.0)
                        / capacity
                        * 100.0,
                        0.0,
                    ),
                    100.0,
                ),
            )
        )
        charge_indices.append(index)

    planned_grid = sum(item.grid_import_kwh for item in charges)
    planned_stored = sum(item.stored_energy_kwh for item in charges)
    planned_direct = sum(item.direct_load_kwh for item in charges)
    cost = sum(item.grid_import_kwh * item.price_pln_kwh for item in charges)
    wear_cost = planned_stored * max(settings.battery_wear_cost_pln_kwh, 0.0)
    baseline_grid_cost = baseline.total_grid_cost_pln
    optimized_grid_cost = simulation.total_grid_cost_pln
    automation_savings = max(
        baseline.total_optimization_cost_pln
        - simulation.total_optimization_cost_pln,
        0.0,
    )
    reference_cost = baseline.total_grid_import_kwh * settings.schedule.g11_price_pln_kwh
    savings = max(reference_cost - optimized_grid_cost, 0.0)
    current_planned = bool(charges and charges[0].start == now_slot)
    current_action = charges[0].action if current_planned else "none"
    current_slot_end: datetime | None = None
    current_run_items: list[PlannedCharge] = []
    current_run_slot_indices: list[int] = []
    if current_planned:
        current_run_items = [charges[0]]
        current_run_slot_indices = [charge_indices[0]]
        current_slot_end_utc = charges[0].start.astimezone(timezone.utc) + SLOT
        current_action_family = (
            "support_only"
            if current_action == "grid_support"
            else "required_charge"
        )
        for item_index, item in zip(charge_indices[1:], charges[1:]):
            item_action_family = (
                "support_only"
                if item.action == "grid_support"
                else "required_charge"
            )
            if (
                item.start.astimezone(timezone.utc) != current_slot_end_utc
                or item_action_family != current_action_family
            ):
                break
            current_run_items.append(item)
            current_run_slot_indices.append(item_index)
            current_slot_end_utc += SLOT
        current_slot_end = current_slot_end_utc.astimezone(settings.now.tzinfo)
    current_run_need_class = _classify_current_run_need(
        current_planned=current_planned,
        current_run_slot_indices=tuple(current_run_slot_indices),
        allocation_provenance=allocation_provenance,
    )
    current_run_duration_seconds = (
        max(
            (
                current_slot_end.astimezone(timezone.utc)
                - settings.now.astimezone(timezone.utc)
            ).total_seconds(),
            0.0,
        )
        if current_slot_end is not None
        else 0.0
    )
    current_run_useful_seconds = max(
        current_run_duration_seconds - GRID_SUPPORT_MODE_TRANSITION_SECONDS,
        0.0,
    )
    current_run_grid_import = sum(
        item.grid_import_kwh for item in current_run_items
    )
    current_run_stored = sum(
        item.stored_energy_kwh for item in current_run_items
    )
    current_run_direct = sum(
        item.direct_load_kwh for item in current_run_items
    )
    current_run_benefit = 0.0
    if current_run_items:
        counterfactual_relative_indices = set(range(len(current_run_items)))
        counterfactual = _simulate(
            effective_settings,
            starts,
            loads,
            {
                index: amount
                for index, amount in planned.items()
                if index not in counterfactual_relative_indices
            },
            {
                index: amount
                for index, amount in planned_support.items()
                if index not in counterfactual_relative_indices
            },
            rates,
            slot_fractions,
        )
        current_run_benefit = max(
            counterfactual.total_optimization_cost_pln
            - simulation.total_optimization_cost_pln,
            0.0,
        )
    current_run_start_eligible = current_planned
    current_run_suppression_reason = "not_support_only"
    current_run_continue_eligible = current_planned
    current_run_continue_reason = "not_support_only"
    if current_action == "grid_support":
        # Continuing an already active support run only needs trustworthy live
        # evidence that the home still has a material net demand. Do not check
        # battery discharge here: successful Grid Charge makes it disappear.
        support_live_values = (
            settings.current_load_power_kw,
            settings.current_pv_power_kw,
        )
        if any(
            value is None or not isfinite(value)
            for value in support_live_values
        ):
            current_run_continue_eligible = False
            current_run_continue_reason = "live_data_missing"
        elif (
            settings.current_load_power_kw  # type: ignore[operator]
            <= settings.current_pv_power_kw + 0.20  # type: ignore[operator]
        ):
            current_run_continue_eligible = False
            current_run_continue_reason = "pv_covers_load"
        else:
            current_run_continue_reason = "eligible"

        live_values = (
            settings.current_load_power_kw,
            settings.current_pv_power_kw,
            settings.current_battery_power_kw,
        )
        if any(value is None or not isfinite(value) for value in live_values):
            current_run_start_eligible = False
            current_run_suppression_reason = "live_data_missing"
        elif (
            settings.current_load_power_kw  # type: ignore[operator]
            <= settings.current_pv_power_kw + 0.20  # type: ignore[operator]
        ):
            current_run_start_eligible = False
            current_run_suppression_reason = "pv_covers_load"
        elif settings.current_battery_power_kw <= 0.20:  # type: ignore[operator]
            current_run_start_eligible = False
            current_run_suppression_reason = "battery_not_discharging"
        elif (
            settings.battery_soc_percent
            <= settings.reserve_soc_percent + 1.0 + _EPSILON
        ):
            current_run_start_eligible = False
            current_run_suppression_reason = "battery_not_discharging"
        elif (
            current_run_useful_seconds + _EPSILON
            < MIN_GRID_SUPPORT_USEFUL_RUNTIME_SECONDS
        ):
            current_run_start_eligible = False
            current_run_suppression_reason = "insufficient_runtime"
        elif current_run_direct + _EPSILON < MIN_GRID_SUPPORT_CYCLE_ENERGY_KWH:
            current_run_start_eligible = False
            current_run_suppression_reason = "insufficient_energy"
        elif current_run_benefit + _EPSILON < MIN_GRID_SUPPORT_CYCLE_BENEFIT_PLN:
            current_run_start_eligible = False
            current_run_suppression_reason = "insufficient_benefit"
        else:
            current_run_suppression_reason = "eligible"
    if current_planned and not settings.control_inputs_fresh:
        block_reason = (
            settings.control_input_block_reason.strip()
            or "control_inputs_stale"
        )
        current_run_start_eligible = False
        current_run_suppression_reason = block_reason
        current_run_continue_eligible = False
        current_run_continue_reason = block_reason
    next_start = charges[0].start if charges else None

    current_index = 0
    if current_planned:
        if current_action == "grid_support":
            target_energy = simulation.battery_after_kwh.get(
                0,
                settings.battery_capacity_kwh
                * settings.battery_soc_percent
                / 100.0,
            )
            # A pure support cycle must preserve, not charge, the battery. A
            # target slightly below current SOC prevents the inverter from
            # interpreting a rounded 100% target as a request to top up.
            target_energy = min(
                target_energy,
                capacity
                * max(
                    settings.battery_soc_percent
                    - GRID_SUPPORT_TARGET_SOC_OFFSET_PERCENT,
                    settings.reserve_soc_percent,
                )
                / 100.0,
            )
        else:
            contiguous = [0]
            for index in range(1, len(starts)):
                if index not in simulation.accepted_import_kwh:
                    break
                contiguous.append(index)
            target_energy = max(
                simulation.battery_after_kwh.get(index, 0.0)
                for index in contiguous
            )
            # When the remaining part of the current low-price window is too
            # short, the feasible imports above describe only what can still
            # be stored.  Using that small value as the live Force Charge SOC
            # target made HA reach it within seconds, switch back to Self-Use,
            # recalculate another tiny target and loop until the tariff window
            # closed.  Keep the target at the energy actually required for the
            # future non-low slots.  It remains almost constant while SOC rises
            # and is capped by the user's configured maximum SOC.
            future_expensive_shortage = sum(
                energy
                for index, energy in simulation.uncovered_import_kwh.items()
                if index >= current_index and rates[index][1] not in {"low", "g11"}
            )
            discharge_efficiency = min(
                max(settings.discharge_efficiency_percent / 100.0, 0.01),
                1.0,
            )
            maximum_energy = max(
                capacity
                * min(max(settings.maximum_soc_percent, 0.0), 100.0)
                / 100.0,
                capacity
                * min(max(settings.reserve_soc_percent, 0.0), 100.0)
                / 100.0,
            )
            target_energy = min(
                max(
                    target_energy,
                    simulation.battery_after_kwh.get(0, target_energy)
                    + future_expensive_shortage / discharge_efficiency,
                ),
                maximum_energy,
            )
    elif charges:
        target_energy = charges[0].target_soc_percent / 100.0 * capacity
    else:
        target_energy = settings.battery_capacity_kwh * settings.battery_soc_percent / 100.0

    maximum_hard_reserve_energy = max(
        (
            simulation.battery_after_kwh.get(item, initial_battery_energy)
            for item, imported in simulation.accepted_import_kwh.items()
            if imported > _EPSILON
        ),
        default=initial_battery_energy,
    )
    hard_reserve_shortfall = (
        max(base_reserve_energy - maximum_hard_reserve_energy, 0.0)
        if hard_reserve_restoration_required
        and not hard_reserve_restored_by_near_term_pv
        else 0.0
    )
    hard_reserve_unavailable = hard_reserve_shortfall > _EPSILON
    remaining_low_direct_import = 0.0
    remaining_expensive_import = 0.0
    terminal_index = len(starts) - 1
    for shortage_index, shortage_energy in simulation.uncovered_import_kwh.items():
        real_energy = shortage_energy
        if shortage_index == terminal_index:
            real_energy = max(
                real_energy - simulation.terminal_import_kwh,
                0.0,
            )
        if real_energy <= _EPSILON:
            continue
        if rates[shortage_index][1] == "low":
            remaining_low_direct_import += real_energy
        elif rates[shortage_index][1] != "g11":
            remaining_expensive_import += real_energy
    # Attribute only the part that had at least one actionable low slot before
    # the expensive deficit to physical capacity/power limits. A deficit with
    # no preceding low slot is a timing/window problem, not a hardware limit.
    capacity_or_power_shortfall = 0.0
    for shortage_index, shortage_energy in simulation.uncovered_import_kwh.items():
        real_energy = shortage_energy
        if shortage_index == terminal_index:
            real_energy = max(real_energy - simulation.terminal_import_kwh, 0.0)
        if (
            real_energy > _EPSILON
            and rates[shortage_index][1] not in {"low", "g11"}
            and any(
                rates[candidate][1] == "low"
                and block_limits[candidate] > _EPSILON
                for candidate in range(shortage_index)
            )
        ):
            capacity_or_power_shortfall += real_energy
    if hard_reserve_unavailable:
        capacity_or_power_shortfall += hard_reserve_shortfall

    if soc_limits_conflict:
        status = "soc_limits_conflict"
    elif hard_reserve_unavailable and planned_stored <= 0.001:
        status = "hard_reserve_unavailable"
    elif hard_reserve_unavailable:
        # A constrained BMS may still provide a useful, safe partial plan.
        # Execute it and expose the remaining hard-floor gap diagnostically;
        # only a zero-throughput plan is blocked completely above.
        status = "insufficient_cheap_window"
    elif charges:
        # A partial plan is still worth executing, but it must not be presented
        # as fully feasible.  This happens, for example, when only the last few
        # minutes of a low-price window remain and the configured Grid Charge
        # power cannot store all energy needed for the following peak period.
        status = (
            "insufficient_cheap_window"
            if (
                remaining_expensive_import > 0.01
                or simulation.terminal_shortfall_kwh > 0.01
            )
            else "ready"
        )
    elif baseline.shortage_kwh <= 0.01:
        status = "no_charge_needed"
    elif settings.schedule.tariff_type.casefold().replace(" ", "") == "g11":
        status = "no_discount_window"
    elif not charges:
        uncovered_slots = tuple(baseline.uncovered_import_kwh)
        if uncovered_slots and all(
            rates[index][1] == "low" for index in uncovered_slots
        ):
            # No battery round trip is useful here: Self-Use will import the
            # unavoidable deficit directly while the low tariff is active.
            status = "shortage_in_low_period"
        elif uneconomic_low_trial_found:
            # A usable low-price actuator exists and physically reduces a
            # later shortage, but conversion losses, battery wear and the
            # configured minimum margin make the cycle more expensive than
            # direct import. This is an intentional economic decision, not a
            # missing time window or battery power/capacity failure.
            status = "not_economically_beneficial"
            capacity_or_power_shortfall = 0.0
        else:
            status = "no_cheap_window"
    else:
        status = "ready"

    current_price, current_zone = rates[current_index]
    return TariffOptimizerResult(
        status_code=status,
        planned_charges=tuple(charges),
        baseline_shortage_kwh=baseline.shortage_kwh,
        remaining_shortage_kwh=simulation.shortage_kwh,
        planned_grid_import_kwh=planned_grid,
        planned_stored_energy_kwh=planned_stored,
        planned_direct_load_kwh=planned_direct,
        planned_cost_pln=cost,
        baseline_grid_cost_pln=baseline_grid_cost,
        optimized_grid_cost_pln=optimized_grid_cost,
        automation_savings_pln=automation_savings,
        baseline_grid_import_kwh=baseline.total_grid_import_kwh,
        optimized_grid_import_kwh=simulation.total_grid_import_kwh,
        g11_reference_cost_pln=reference_cost,
        estimated_savings_pln=savings,
        ending_battery_kwh=simulation.ending_battery_kwh,
        ending_battery_soc_percent=simulation.ending_battery_kwh / capacity * 100.0,
        target_soc_percent=min(max(target_energy / capacity * 100.0, 0.0), 100.0),
        current_slot_planned=current_planned,
        current_action=current_action,
        current_slot_end=current_slot_end,
        current_price_pln_kwh=current_price,
        current_zone=current_zone,
        next_charge_start=next_start,
        charge_power_kw=charge_power,
        requested_charge_power_kw=requested_charge_power,
        effective_power_factor=(
            min(max(charge_power / requested_charge_power, 0.0), 1.0)
            if requested_charge_power > _EPSILON
            else 1.0
        ),
        horizon_days=horizon_days,
        horizon_end=horizon_end,
        terminal_reserve_soc_percent=min(
            max(
                settings.terminal_reserve_soc_percent
                if settings.terminal_reserve_soc_percent is not None
                else settings.reserve_soc_percent,
                settings.reserve_soc_percent,
            ),
            settings.maximum_soc_percent,
        ),
        terminal_shortfall_kwh=simulation.terminal_shortfall_kwh,
        planned_battery_wear_cost_pln=wear_cost,
        planning_slot_count=len(starts),
        baseline_optimization_cost_pln=baseline.total_optimization_cost_pln,
        optimized_optimization_cost_pln=simulation.total_optimization_cost_pln,
        planning_horizon_hours=planning_horizon_hours,
        planning_horizon_extended_to_minimum=horizon_extended,
        modeled_load_kwh=sum(loads),
        modeled_pv_kwh=modeled_pv,
        effective_terminal_reserve_soc_percent=(
            _effective_terminal_reserve_soc_percent(settings)
        ),
        current_run_end=current_slot_end,
        current_run_need_class=current_run_need_class,
        current_run_duration_seconds=current_run_duration_seconds,
        current_run_grid_import_kwh=current_run_grid_import,
        current_run_stored_kwh=current_run_stored,
        current_run_direct_load_kwh=current_run_direct,
        current_run_benefit_pln=current_run_benefit,
        current_run_start_eligible=current_run_start_eligible,
        current_run_suppression_reason=current_run_suppression_reason,
        current_run_continue_eligible=current_run_continue_eligible,
        current_run_continue_reason=current_run_continue_reason,
        current_slot_load_kwh=loads[0] if loads else 0.0,
        current_slot_pv_kwh=(
            max(effective_pv_by_slot.get(starts[0], 0.0), 0.0)
            * first_fraction
            if starts
            else 0.0
        ),
        current_slot_load_source=current_slot_load_source,
        current_slot_pv_source=current_slot_pv_source,
        current_battery_power_kw=settings.current_battery_power_kw,
        base_reserve_soc_percent=base_reserve_soc,
        hard_reserve_deficit_kwh=hard_reserve_deficit,
        hard_reserve_restoration_required=hard_reserve_restoration_required,
        hard_reserve_restored_by_near_term_pv=(
            hard_reserve_restored_by_near_term_pv
        ),
        hard_reserve_unavailable=hard_reserve_unavailable,
        hard_reserve_shortfall_kwh=hard_reserve_shortfall,
        hard_reserve_deferral_source=hard_reserve_deferral_source,
        live_pv_surplus_stable=settings.live_pv_surplus_stable,
        live_pv_surplus_stable_seconds=max(
            settings.live_pv_surplus_stable_seconds,
            0.0,
        ),
        expensive_window_load_buffers=tuple(expensive_window_load_buffers),
        load_risk_multiplier=load_risk_multiplier,
        load_risk_buffer_kwh=sum(
            item.buffer_kwh for item in expensive_window_load_buffers
        ),
        morning_protection_active=morning_protection_active,
        morning_protection_mode=morning_protection_mode,
        morning_protection_window_start=morning_window_start,
        morning_protection_window_end=morning_window_end,
        morning_protection_expected_pv_kwh=morning_expected_pv,
        morning_protection_conservative_pv_kwh=morning_conservative_pv,
        remaining_low_direct_import_kwh=remaining_low_direct_import,
        remaining_expensive_import_kwh=remaining_expensive_import,
        capacity_or_power_shortfall_kwh=capacity_or_power_shortfall,
        control_inputs_fresh=settings.control_inputs_fresh,
        control_input_block_reason=(
            settings.control_input_block_reason.strip()
            or (
                "none"
                if settings.control_inputs_fresh
                else "control_inputs_stale"
            )
        ),
        timeline_trace=_tariff_timeline_trace(
            settings=effective_settings,
            starts=starts,
            slot_fractions=slot_fractions,
            rates=rates,
            baseline=baseline,
            selected=simulation,
            status_code=status,
            planning_horizon_hours=planning_horizon_hours,
        ),
    )
