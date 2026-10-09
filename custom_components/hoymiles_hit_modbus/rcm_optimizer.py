"""Pure planning and feedback logic for RCEm 253 V+ automation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from math import ceil, floor, isfinite

try:
    from .load_model import expected_load_by_slot
except ImportError:
    from load_model import expected_load_by_slot


MINIMUM_CHARGE_LIMIT_PERCENT = 10.0
P_U_START_V = 248.4
CONTROL_TARGET_V = 249.2
WARNING_V = 251.0
EMERGENCY_V = 252.2
LIVE_ACTION_VALIDITY_SECONDS = 60.0


def _clamp(value: float, lower: float, upper: float) -> float:
    return min(max(value, lower), upper)


def _quantize_power_percent(value: float) -> float:
    """Floor a new control limit; never quantize observed or saved settings."""
    if not isfinite(value) or value < 1.0:
        return 0.0
    return float(floor(min(value, 100.0)))


def _within_window(minute: int, start: int, end: int) -> bool:
    if start <= end:
        return start <= minute < end
    return minute >= start or minute < end


@dataclass(frozen=True, slots=True)
class RCMProfileSelection:
    """One normalized 48-slot forecast/profile selection."""

    slot_kwh: tuple[float, ...]
    source: str
    confidence: float
    selected_total_kwh: float
    by_start_kwh: Mapping[datetime, float] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RCMLoadEnvelopeSelection:
    """Nominal, low and high household-load profiles for RCEm."""

    nominal: RCMProfileSelection
    low: RCMProfileSelection
    high: RCMProfileSelection


def stateful_pre_risk_home_buffer_kwh(
    pv_kwh: Sequence[float],
    load_kwh: Sequence[float],
    *,
    charge_efficiency: float = 1.0,
    house_discharge_efficiency: float = 1.0,
    charge_input_limits_kwh: Sequence[float] | None = None,
) -> float:
    """Return the initial battery energy needed before a risk window.

    The calculation follows the slots in chronological order.  PV available
    before a later household load can therefore replenish the battery, while
    PV arriving after an earlier load cannot retroactively fund that load.
    Optional per-slot limits keep the recharge path inside the shared
    inverter/BMS charging capability.
    """
    if len(pv_kwh) != len(load_kwh):
        raise ValueError("PV and LOAD profiles must have equal length")
    if charge_input_limits_kwh is not None and len(charge_input_limits_kwh) != len(
        pv_kwh
    ):
        raise ValueError("charge limits must match the profile length")

    efficiency = _clamp(float(charge_efficiency), 0.0, 1.0)
    discharge_efficiency = _clamp(
        float(house_discharge_efficiency),
        0.01,
        1.0,
    )
    running_energy = 0.0
    lowest_energy = 0.0
    for index, (raw_pv, raw_load) in enumerate(zip(pv_kwh, load_kwh)):
        pv = max(float(raw_pv), 0.0)
        load = max(float(raw_load), 0.0)
        direct_pv = min(pv, load)
        deficit = load - direct_pv
        surplus = pv - direct_pv
        if charge_input_limits_kwh is not None:
            surplus = min(
                surplus,
                max(float(charge_input_limits_kwh[index]), 0.0),
            )
        # LOAD is measured on the AC side.  Supplying it from the battery
        # consumes more DC energy according to the house-discharge efficiency.
        running_energy += surplus * efficiency - deficit / discharge_efficiency
        lowest_energy = min(lowest_energy, running_energy)
    return max(-lowest_energy, 0.0)


def stateful_natural_headroom_kwh(
    pv_kwh: Sequence[float],
    load_kwh: Sequence[float],
    *,
    initial_headroom_kwh: float,
    maximum_headroom_kwh: float,
    charge_efficiency: float = 1.0,
    house_discharge_efficiency: float = 1.0,
    charge_input_limits_kwh: Sequence[float] | None = None,
) -> float:
    """Return additional battery headroom present after the last slot.

    Headroom is simulated in chronological order. Household demand first
    creates room in the battery, while later PV can fill that room again.
    Conversely, PV arriving while the battery is already full cannot be
    credited against a later household discharge. Optional charge-input
    limits model the shared inverter path and the BMS power ceiling.

    The result is *additional* headroom relative to the initial SOC, not the
    battery's total empty capacity.
    """
    if len(pv_kwh) != len(load_kwh):
        raise ValueError("PV and LOAD profiles must have equal length")
    if charge_input_limits_kwh is not None and len(charge_input_limits_kwh) != len(
        pv_kwh
    ):
        raise ValueError("charge limits must match the profile length")

    efficiency = _clamp(float(charge_efficiency), 0.0, 1.0)
    discharge_efficiency = _clamp(
        float(house_discharge_efficiency),
        0.01,
        1.0,
    )
    maximum_headroom = max(float(maximum_headroom_kwh), 0.0)
    initial_headroom = _clamp(
        float(initial_headroom_kwh),
        0.0,
        maximum_headroom,
    )
    headroom = initial_headroom

    for index, (raw_pv, raw_load) in enumerate(zip(pv_kwh, load_kwh)):
        pv = max(float(raw_pv), 0.0)
        load = max(float(raw_load), 0.0)
        direct_pv = min(pv, load)
        deficit = load - direct_pv
        surplus = pv - direct_pv
        if charge_input_limits_kwh is not None:
            surplus = min(
                surplus,
                max(float(charge_input_limits_kwh[index]), 0.0),
            )

        headroom = min(
            headroom + deficit / discharge_efficiency,
            maximum_headroom,
        )
        headroom = max(headroom - surplus * efficiency, 0.0)

    return max(headroom - initial_headroom, 0.0)


@dataclass(frozen=True, slots=True)
class RCMRiskWindowInput:
    """Forecast energy balance for one recurring voltage-risk window."""

    start_minute: int
    end_minute: int
    peak_voltage_v: float
    day_offset: int
    expected_pv_kwh: float
    expected_load_kwh: float
    expected_surplus_kwh: float
    natural_headroom_before_kwh: float
    absorbable_surplus_kwh: float | None = None
    protected_home_energy_kwh: float = 0.0
    stress_protected_home_energy_kwh: float = 0.0
    absorption_power_limited: bool = False


@dataclass(frozen=True, slots=True)
class RCMRiskWindowPlan:
    """Battery-space requirement calculated for one risk window."""

    start_minute: int
    end_minute: int
    peak_voltage_v: float
    day_offset: int
    expected_pv_kwh: float
    expected_load_kwh: float
    expected_surplus_kwh: float
    required_headroom_kwh: float
    projected_headroom_before_kwh: float
    cumulative_headroom_shortfall_kwh: float
    absorbable_surplus_kwh: float = 0.0
    protected_home_energy_kwh: float = 0.0
    stress_protected_home_energy_kwh: float = 0.0
    absorption_power_limited: bool = False


def _valid_48_slot_profile(values: Sequence[float] | None) -> tuple[float, ...]:
    """Return a sanitized profile or an empty tuple when it is unusable."""
    if values is None or isinstance(values, (str, bytes)):
        return ()
    try:
        if len(values) != 48:
            return ()
        profile = tuple(max(float(value), 0.0) for value in values)
    except (TypeError, ValueError):
        return ()
    return profile if sum(profile) > 0 else ()


def select_rcm_load_profile(
    *,
    weekend: bool,
    average_profile: Sequence[float] | None,
    weekday_profile: Sequence[float] | None,
    weekend_profile: Sequence[float] | None,
    average_daily_kwh: float,
) -> RCMProfileSelection:
    """Select weekday/weekend LOAD history with conservative fallbacks."""
    preferred = _valid_48_slot_profile(
        weekend_profile if weekend else weekday_profile
    )
    if preferred:
        return RCMProfileSelection(
            preferred,
            "weekend_48_slot" if weekend else "weekday_48_slot",
            0.95,
            sum(preferred),
        )
    average = _valid_48_slot_profile(average_profile)
    if average:
        return RCMProfileSelection(
            average,
            "average_48_slot_fallback",
            0.75,
            sum(average),
        )
    cross_day = _valid_48_slot_profile(
        weekday_profile if weekend else weekend_profile
    )
    if cross_day:
        return RCMProfileSelection(
            cross_day,
            "cross_day_48_slot_fallback",
            0.55,
            sum(cross_day),
        )
    try:
        daily = max(float(average_daily_kwh), 0.0)
    except (TypeError, ValueError):
        daily = 0.0
    flat = (daily / 48.0,) * 48
    return RCMProfileSelection(
        flat,
        "flat_daily_fallback",
        0.30 if daily > 0 else 0.0,
        daily,
    )


def _quantile(values: Sequence[float], fraction: float) -> float:
    """Return a linearly interpolated quantile without extra dependencies."""
    ordered = sorted(values)
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    position = _clamp(fraction, 0.0, 1.0) * (len(ordered) - 1)
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def select_rcm_load_envelopes(
    *,
    weekend: bool,
    average_profile: Sequence[float] | None,
    weekday_profile: Sequence[float] | None,
    weekend_profile: Sequence[float] | None,
    average_daily_kwh: float,
    daily_totals_kwh: Sequence[float] | None = None,
    nominal: RCMProfileSelection | None = None,
) -> RCMLoadEnvelopeSelection:
    """Build opposite LOAD envelopes for headroom and reserve protection.

    A low-load (P10) profile is conservative for an overvoltage/headroom
    scenario because more PV can reach the battery or grid. A high-load
    (P90) profile is conservative for protecting household energy before a
    planned discharge. Both retain the selected weekday/weekend shape.
    """
    nominal = nominal or select_rcm_load_profile(
        weekend=weekend,
        average_profile=average_profile,
        weekday_profile=weekday_profile,
        weekend_profile=weekend_profile,
        average_daily_kwh=average_daily_kwh,
    )
    clean: list[float] = []
    if daily_totals_kwh is not None and not isinstance(
        daily_totals_kwh, (str, bytes)
    ):
        for raw in daily_totals_kwh:
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if value >= 0.0:
                clean.append(value)

    total = nominal.selected_total_kwh
    if total <= 0.0:
        return RCMLoadEnvelopeSelection(nominal, nominal, nominal)
    if len(clean) >= 3:
        low_total = _clamp(_quantile(clean, 0.10), total * 0.50, total)
        high_total = _clamp(_quantile(clean, 0.90), total, total * 1.75)
        source_suffix = "daily_quantile"
        confidence = min(nominal.confidence, 0.85)
    else:
        # With too little history, retain bounded asymmetric uncertainty.
        low_total = total * 0.80
        high_total = total * 1.25
        source_suffix = "bounded_fallback"
        confidence = min(nominal.confidence, 0.45)

    def scaled(selected_total: float, percentile: str) -> RCMProfileSelection:
        ratio = selected_total / total
        slots = tuple(value * ratio for value in nominal.slot_kwh)
        return RCMProfileSelection(
            slots,
            f"{nominal.source}_{percentile}_{source_suffix}",
            confidence,
            sum(slots),
            {stamp: value * ratio for stamp, value in nominal.by_start_kwh.items()},
        )

    return RCMLoadEnvelopeSelection(
        nominal=nominal,
        low=scaled(low_total, "p10"),
        high=scaled(high_total, "p90"),
    )


def shared_rcm_load_envelopes(
    *, now: datetime, target_date: date, daily_totals_kwh: Sequence[float] = (),
    **model_inputs,
) -> RCMLoadEnvelopeSelection:
    """Use the common nominal LOAD; RCEm owns only P10/P90 policy margins.

    Absolute UTC keys preserve both folds and omit the missing spring hour.
    Values are full half-hours; consumers clip elapsed time exactly once.
    """
    cursor = datetime.combine(target_date, time.min, now.tzinfo).astimezone(timezone.utc)
    end = datetime.combine(target_date + timedelta(days=1), time.min,
                           now.tzinfo).astimezone(timezone.utc)
    starts = []
    while cursor < end:
        starts.append(cursor)
        cursor += timedelta(minutes=30)
    forecast = expected_load_by_slot(starts, now=now, **model_inputs)
    profile = [0.] * 48
    for stamp, value in forecast.by_slot_kwh.items():
        local = stamp.astimezone(now.tzinfo)
        profile[local.hour*2 + local.minute//30] += value
    nominal = RCMProfileSelection(tuple(profile), 'shared_' + forecast.profile_mode,
        .95 if 'fallback' not in forecast.profile_mode else .30,
        sum(forecast.by_slot_kwh.values()), forecast.by_slot_kwh)
    return select_rcm_load_envelopes(weekend=target_date.weekday() >= 5,
        average_profile=None, weekday_profile=None, weekend_profile=None,
        average_daily_kwh=model_inputs['daily_energy_kwh'],
        daily_totals_kwh=daily_totals_kwh, nominal=nominal)


def select_rcm_pv_profile(
    *,
    forecast_total_kwh: float,
    forecast_p90_total_kwh: float | None,
    detailed_p50_by_slot: Mapping[int, float] | None,
    detailed_p90_by_slot: Mapping[int, float] | None,
    first_slot: int,
    current_slot_fraction: float = 1.0,
    risk_slots: Sequence[int] = (),
    forecast_p10_total_kwh: float | None = None,
    detailed_p10_by_slot: Mapping[int, float] | None = None,
    scenario: str = "high",
) -> RCMProfileSelection:
    """Prefer Solcast 30-minute intervals and retain a safe shaped fallback.

    P90 is used as the headroom energy envelope. For the separate reserve
    scenario, ``scenario="low"`` selects P10. If Solcast only exposes
    detailed P50 intervals, their shape is scaled to the selected percentile
    total rather than discarding the detailed timing.
    """
    try:
        first = int(_clamp(float(first_slot), 0.0, 47.0))
    except (TypeError, ValueError):
        first = 0
    try:
        fraction = _clamp(float(current_slot_fraction), 0.0, 1.0)
    except (TypeError, ValueError):
        fraction = 1.0
    try:
        expected_total = max(float(forecast_total_kwh), 0.0)
    except (TypeError, ValueError):
        expected_total = 0.0
    try:
        p90_total = (
            max(float(forecast_p90_total_kwh), 0.0)
            if forecast_p90_total_kwh is not None
            else None
        )
    except (TypeError, ValueError):
        p90_total = None
    try:
        p10_total = (
            max(float(forecast_p10_total_kwh), 0.0)
            if forecast_p10_total_kwh is not None
            else None
        )
    except (TypeError, ValueError):
        p10_total = None
    scenario_name = str(scenario).lower()
    low_scenario = scenario_name == "low"
    nominal_scenario = scenario_name in {"nominal", "p50"}
    if nominal_scenario:
        target_total = expected_total
    elif low_scenario:
        target_total = p10_total if p10_total is not None else expected_total
    else:
        target_total = p90_total if p90_total is not None else expected_total

    def sanitize(
        values: Mapping[int, float] | None,
    ) -> tuple[list[float], set[int]]:
        profile = [0.0] * 48
        present: set[int] = set()
        if not isinstance(values, Mapping):
            return profile, present
        for raw_slot, raw_value in values.items():
            try:
                slot = int(raw_slot)
                value = max(float(raw_value), 0.0)
            except (TypeError, ValueError):
                continue
            if slot < first or slot >= 48:
                continue
            profile[slot] += value
            present.add(slot)
        if first in present:
            profile[first] *= fraction
        return profile, present

    p90_profile, p90_present = sanitize(detailed_p90_by_slot)
    p10_profile, p10_present = sanitize(detailed_p10_by_slot)
    p50_profile, p50_present = sanitize(detailed_p50_by_slot)
    relevant_slots: set[int] = set()
    for raw_slot in risk_slots:
        try:
            slot = int(raw_slot)
        except (TypeError, ValueError):
            continue
        if first <= slot < 48:
            relevant_slots.add(slot)

    def covers_risk(present: set[int]) -> bool:
        # A partial detailed forecast must not silently turn missing risk slots
        # into 0 kWh.  Prefer another complete detailed percentile, otherwise
        # use the conservative shaped-total fallback.
        return not relevant_slots or relevant_slots.issubset(present)

    if (
        nominal_scenario
        and p50_present
        and sum(p50_profile) > 0
        and covers_risk(p50_present)
    ):
        selected = p50_profile
        present = p50_present
        source = "solcast_30m_p50"
        base_confidence = 0.90
    elif (
        low_scenario
        and p10_present
        and sum(p10_profile) > 0
        and covers_risk(p10_present)
    ):
        selected = p10_profile
        present = p10_present
        source = "solcast_30m_p10"
        base_confidence = 0.95
    elif (
        not low_scenario
        and not nominal_scenario
        and p90_present
        and sum(p90_profile) > 0
        and covers_risk(p90_present)
    ):
        selected = p90_profile
        present = p90_present
        source = "solcast_30m_p90"
        base_confidence = 0.95
    elif p50_present and sum(p50_profile) > 0 and covers_risk(p50_present):
        selected = p50_profile
        present = p50_present
        if nominal_scenario:
            source = "solcast_30m_p50"
            base_confidence = 0.78
        elif low_scenario:
            source = (
                "solcast_30m_p50_shape_p10_total"
                if p10_total is not None
                else "solcast_30m_p50_low_fallback"
            )
            base_confidence = 0.82 if p10_total is not None else 0.65
        else:
            source = (
                "solcast_30m_p50_shape_p90_total"
                if p90_total is not None
                else "solcast_30m_p50"
            )
            base_confidence = 0.82 if p90_total is not None else 0.78
    else:
        selected = [0.0] * 48
        present = set()
        for slot in range(max(12, first), 42):
            distance = abs(slot - 27) / 15.0
            selected[slot] = max(1.0 - distance * distance, 0.0)
        if nominal_scenario:
            source = "solcast_total_nominal_shaped_fallback"
            base_confidence = 0.45
        elif low_scenario:
            source = (
                "solcast_p10_total_shaped_fallback"
                if p10_total is not None
                else "solcast_total_low_shaped_fallback"
            )
            base_confidence = 0.50 if p10_total is not None else 0.35
        else:
            source = (
                "solcast_p90_total_shaped_fallback"
                if p90_total is not None
                else "solcast_total_shaped_fallback"
            )
            base_confidence = 0.50 if p90_total is not None else 0.40

    total = sum(selected)
    if total > 0 and target_total > 0:
        selected = [value * target_total / total for value in selected]
    elif target_total <= 0:
        selected = [0.0] * 48

    if present and relevant_slots:
        coverage = len(present & relevant_slots) / len(relevant_slots)
        confidence = base_confidence * (0.65 + 0.35 * coverage)
    else:
        confidence = base_confidence
    return RCMProfileSelection(
        tuple(selected),
        source,
        round(_clamp(confidence, 0.0, 1.0), 3),
        round(sum(selected), 6),
    )


@dataclass(frozen=True, slots=True)
class RCMOptimizerInput:
    """Current system state and the compact four-day risk model."""

    now: datetime
    voltage_l1_v: float
    voltage_l2_v: float
    voltage_l3_v: float
    filtered_voltage_v: float
    rolling_10m_voltage_v: float
    historical_p90_voltage_v: float
    risk_windows: tuple[tuple[int, int, float], ...]
    history_days: int
    pv_power_kw: float
    load_power_kw: float
    grid_export_power_kw: float
    battery_capacity_kwh: float
    battery_soc_percent: float
    reserve_soc_percent: float
    safety_margin_soc_percent: float
    protected_minimum_soc_percent: float
    expected_risk_surplus_kwh: float
    expected_natural_headroom_kwh: float
    minutes_to_risk: int | None
    risk_day_offset: int
    system_power_kw: float
    battery_voltage_v: float | None
    bms_max_charge_current_a: float | None
    bms_max_discharge_current_a: float | None
    current_charge_limit_percent: float
    saved_charge_limit_percent: float
    export_control_enabled: bool
    current_export_limit_percent: float
    saved_export_limit_percent: float
    user_export_cap_percent: float
    # Physical provenance is independent from RCEm's permission to modify the
    # export register.  When GCF is disabled the register is dormant; when it
    # is enabled its live value remains a hard cap even if RCEm control is off.
    gcf_active: bool = True
    gcf_data_fresh: bool = True
    charge_efficiency_percent: float = 95.0
    house_discharge_efficiency_percent: float = 95.0
    expected_pre_risk_surplus_kwh: float = 0.0
    risk_window_forecasts: tuple[RCMRiskWindowInput, ...] = ()
    expected_unavoidable_charge_input_kwh: float | None = None
    expected_absorbable_risk_surplus_kwh: float | None = None
    # Nominal/P50 chronological household buffer.  This is a transient
    # pre-discharge budget, not an additional hard SOC floor.
    expected_protected_home_energy_kwh: float = 0.0
    # P10-PV/P90-LOAD stress branch.  It may cancel an unsafe predictive
    # discharge when the battery is energy-critical, but never raises the
    # normal RCEm hard floor.
    expected_stress_home_energy_kwh: float = 0.0
    # Prediction needs a coherent three-phase snapshot, while the statutory
    # 253 V feedback path must still react when any one phase is positively
    # fresh and high.  ``None`` preserves the legacy pure-model contract.
    voltage_data_fresh: bool = True
    emergency_voltage_data_fresh: bool | None = None
    actuator_data_fresh: bool = True
    history_data_fresh: bool = True
    forecast_data_fresh: bool = True
    load_profile_data_fresh: bool = True
    live_power_data_fresh: bool = True
    charge_actuator_data_fresh: bool | None = None
    export_actuator_data_fresh: bool | None = None
    bms_charge_data_fresh: bool = True
    bms_discharge_data_fresh: bool = True
    pre_discharge_actuator_data_fresh: bool = True
    pre_discharge_active: bool = False
    # Rated inverter power and the active machine count/topology must be
    # positively known before a percentage can be converted to shared kW.
    # Export-only emergency clamping remains independent of this charge path.
    system_power_data_valid: bool = True


@dataclass(frozen=True, slots=True)
class RCMOptimizerResult:
    """One deterministic recommendation for the RCEm controller."""

    status_code: str
    action: str
    action_interval_start: datetime | None
    action_interval_end: datetime | None
    maximum_voltage_v: float
    control_voltage_v: float
    voltage_risk_score: float
    risk_window_active: bool
    next_risk_start_minute: int | None
    reserve_soc_percent: float
    protected_minimum_soc_percent: float
    required_headroom_kwh: float
    available_headroom_kwh: float
    headroom_shortfall_kwh: float
    expected_natural_headroom_kwh: float
    planned_grid_discharge_kwh: float
    pre_discharge_target_soc_percent: float
    pre_discharge_power_kw: float
    pre_discharge_power_percent: float
    pre_discharge_ready: bool
    target_soc_before_risk_percent: float
    pv_surplus_power_kw: float
    bms_charge_power_limit_kw: float
    recommended_charge_limit_percent: float
    recommended_charge_power_kw: float
    effective_export_cap_percent: float
    recommended_export_limit_percent: float
    estimated_safe_export_power_kw: float | None
    saturated: bool
    unavoidable_minimum_charge_kwh: float
    risk_window_plans: tuple[RCMRiskWindowPlan, ...]
    live_emergency: bool
    emergency_action_ready: bool
    prediction_ready: bool
    prediction_block_reason: str
    system_power_data_valid: bool
    voltage_data_fresh: bool
    emergency_voltage_data_fresh: bool
    actuator_data_fresh: bool
    charge_actuator_data_fresh: bool
    export_actuator_data_fresh: bool
    forecast_data_fresh: bool
    load_profile_data_fresh: bool
    history_data_fresh: bool
    live_power_data_fresh: bool
    bms_charge_available: bool
    bms_charge_quantization_limited: bool
    bms_discharge_available: bool
    bms_discharge_dc_power_limit_kw: float
    bms_discharge_power_limit_kw: float
    absorbable_risk_surplus_kwh: float
    protected_home_energy_kwh: float
    headroom_power_limited: bool
    headroom_capacity_limited: bool
    pre_discharge_start_eligible: bool
    pre_discharge_continue_eligible: bool
    pre_discharge_transaction_ready: bool
    pre_discharge_deadline: datetime | None
    unconstrained_required_headroom_kwh: float
    creatable_headroom_kwh: float
    unabsorbed_surplus_due_floor_kwh: float
    nominal_pre_risk_home_buffer_kwh: float
    stress_protected_home_energy_kwh: float
    stress_reserve_energy_critical: bool
    stress_discharge_limited: bool


def optimize_rcm(settings: RCMOptimizerInput) -> RCMOptimizerResult:
    """Return a bounded, ramp-limited PV charge recommendation.

    The controller uses the global battery charge limit and, when explicitly
    enabled, a bounded maximum-export limit.  It never requests Grid Charge,
    GCF, phase-unbalance or protection-setting changes.
    """
    if settings.battery_capacity_kwh < 0 or settings.system_power_kw <= 0:
        raise ValueError("battery capacity cannot be negative and system power must be positive")
    if settings.history_days < 0:
        raise ValueError("history_days cannot be negative")

    # An exact 0 kWh is a valid fail-closed telemetry value, not permission to
    # invent a small battery.  Keep it visible in all energy diagnostics while
    # preventing predictive discharge from starting or continuing.  Live
    # 253 V emergency feedback remains independent and may still use fresh
    # voltage/BMS/actuator evidence.
    battery_capacity_available = settings.battery_capacity_kwh > 0.0

    current_limit = _clamp(
        settings.current_charge_limit_percent,
        MINIMUM_CHARGE_LIMIT_PERCENT,
        100.0,
    )
    saved_limit = _clamp(
        settings.saved_charge_limit_percent,
        MINIMUM_CHARGE_LIMIT_PERCENT,
        100.0,
    )
    charge_actuator_fresh = (
        settings.actuator_data_fresh
        if settings.charge_actuator_data_fresh is None
        else settings.charge_actuator_data_fresh
    )
    export_actuator_fresh = (
        settings.actuator_data_fresh
        if settings.export_actuator_data_fresh is None
        else settings.export_actuator_data_fresh
    )
    efficiency = _clamp(settings.charge_efficiency_percent, 1.0, 100.0) / 100.0
    discharge_efficiency = (
        _clamp(
            settings.house_discharge_efficiency_percent,
            1.0,
            100.0,
        )
        / 100.0
    )
    bms_charge_telemetry_available = bool(
        settings.system_power_data_valid
        and settings.bms_charge_data_fresh
        and settings.battery_voltage_v is not None
        and settings.bms_max_charge_current_a is not None
        and settings.battery_voltage_v > 0.0
        and settings.bms_max_charge_current_a > 0.0
    )
    bms_discharge_available = bool(
        settings.system_power_data_valid
        and settings.bms_discharge_data_fresh
        and settings.battery_voltage_v is not None
        and settings.bms_max_discharge_current_a is not None
        and settings.battery_voltage_v > 0.0
        and settings.bms_max_discharge_current_a > 0.0
    )
    bms_limit_kw = (
        min(
            settings.system_power_kw,
            settings.battery_voltage_v
            * settings.bms_max_charge_current_a
            / 1000.0,
        )
        if bms_charge_telemetry_available
        else 0.0
    )
    minimum_representable_charge_kw = (
        settings.system_power_kw * MINIMUM_CHARGE_LIMIT_PERCENT / 100.0
    )
    # Register 306 cannot represent a command below 10%.  Treat a smaller BMS
    # allowance as an unavailable charge actuator instead of writing 10% and
    # relying on the BMS to clamp or alarm. Export limiting remains available
    # as an independent emergency path.
    bms_charge_quantization_limited = bool(
        bms_charge_telemetry_available
        and bms_limit_kw + 1e-6 < minimum_representable_charge_kw
    )
    bms_charge_available = bool(
        bms_charge_telemetry_available
        and not bms_charge_quantization_limited
    )
    bms_discharge_dc_limit_kw = (
        settings.battery_voltage_v
        * settings.bms_max_discharge_current_a
        / 1000.0
        if bms_discharge_available
        else 0.0
    )
    # BMS voltage x current is a battery-side (DC) capability. Register 4306
    # controls inverter AC output, so discharge conversion losses must be
    # applied before this value is allowed to cap a command.
    bms_discharge_limit_kw = min(
        settings.system_power_kw,
        bms_discharge_dc_limit_kw * discharge_efficiency,
    )

    maximum_voltage = max(
        settings.voltage_l1_v,
        settings.voltage_l2_v,
        settings.voltage_l3_v,
    )
    control_voltage = max(
        maximum_voltage,
        settings.filtered_voltage_v,
        settings.rolling_10m_voltage_v + 0.25,
    )
    emergency_voltage_data_fresh = (
        settings.voltage_data_fresh
        if settings.emergency_voltage_data_fresh is None
        else settings.emergency_voltage_data_fresh
    )
    live_emergency = bool(
        emergency_voltage_data_fresh and maximum_voltage >= 253.0
    )
    emergency_action_ready = bool(
        live_emergency
        and (
            (charge_actuator_fresh and bms_charge_available)
            or (
                settings.export_control_enabled
                and settings.gcf_active
                and settings.gcf_data_fresh
                and export_actuator_fresh
            )
        )
    )
    prediction_ready = bool(
        battery_capacity_available
        and settings.system_power_data_valid
        and settings.history_days > 0
        and settings.history_data_fresh
        and settings.forecast_data_fresh
        and settings.load_profile_data_fresh
        and settings.gcf_data_fresh
    )
    if not battery_capacity_available:
        prediction_block_reason = "battery_capacity_unavailable"
    elif not settings.system_power_data_valid:
        prediction_block_reason = "system_power_unavailable"
    elif settings.history_days == 0:
        prediction_block_reason = "history_learning"
    elif not settings.history_data_fresh:
        prediction_block_reason = "history_stale"
    elif not settings.forecast_data_fresh:
        prediction_block_reason = "forecast_stale"
    elif not settings.load_profile_data_fresh:
        prediction_block_reason = "load_profile_stale"
    elif not settings.gcf_data_fresh:
        prediction_block_reason = "gcf_state_stale"
    else:
        prediction_block_reason = "ready"
    fast_score = _clamp(
        (control_voltage - P_U_START_V) / (EMERGENCY_V - P_U_START_V),
        0.0,
        1.0,
    )
    history_score = (
        _clamp(
            (settings.historical_p90_voltage_v - P_U_START_V)
            / (EMERGENCY_V - P_U_START_V),
            0.0,
            1.0,
        )
        if settings.history_days > 0 and settings.history_data_fresh
        else 0.0
    )
    risk_score = max(fast_score, history_score * 0.35)

    minute = settings.now.hour * 60 + settings.now.minute
    active_window = settings.history_data_fresh and any(
        _within_window(minute, start, end)
        for start, end, _peak in settings.risk_windows
    )
    future_starts = sorted(
        start if start > minute else start + 24 * 60
        for start, _end, _peak in settings.risk_windows
    )
    next_risk_start = (
        future_starts[0]
        if future_starts and settings.history_data_fresh
        else None
    )

    base_reserve_soc = _clamp(
        settings.reserve_soc_percent + settings.safety_margin_soc_percent,
        0.0,
        100.0,
    )
    # RCEm is export/headroom-first.  Its hard floor is owned solely by the
    # inverter Self-Use reserve plus the RCEm safety margin.  The nominal home
    # buffer below is a transient pre-risk discharge budget; the independent
    # P10/P90 stress branch can cancel an unsafe start but cannot permanently
    # lift this floor (or inherit another optimizer's target).
    protected_minimum_soc = base_reserve_soc
    reserve_soc = protected_minimum_soc
    nominal_home_buffer = max(
        settings.expected_protected_home_energy_kwh,
        0.0,
    )
    stress_home_buffer = max(
        settings.expected_stress_home_energy_kwh,
        nominal_home_buffer,
    )
    base_reserve_energy = (
        settings.battery_capacity_kwh * protected_minimum_soc / 100.0
    )
    current_battery_energy = (
        settings.battery_capacity_kwh
        * _clamp(settings.battery_soc_percent, 0.0, 100.0)
        / 100.0
    )
    nominal_pre_discharge_floor_energy = min(
        base_reserve_energy + nominal_home_buffer,
        settings.battery_capacity_kwh,
    )
    nominal_pre_discharge_floor_soc = (
        nominal_pre_discharge_floor_energy
        / settings.battery_capacity_kwh
        * 100.0
        if battery_capacity_available
        else protected_minimum_soc
    )
    stress_reserve_floor_energy = min(
        base_reserve_energy + stress_home_buffer,
        settings.battery_capacity_kwh,
    )
    stress_reserve_energy_critical = bool(
        current_battery_energy <= stress_reserve_floor_energy + 0.001
    )
    usable_capacity = (
        settings.battery_capacity_kwh * (100.0 - reserve_soc) / 100.0
    )
    pre_risk_hours = max(
        min(float(settings.minutes_to_risk or 0), 24 * 60) / 60.0,
        0.0,
    )
    minimum_charge_floor_kw = minimum_representable_charge_kw
    if settings.expected_unavoidable_charge_input_kwh is not None:
        unavoidable_charge_input = max(
            settings.expected_unavoidable_charge_input_kwh,
            0.0,
        )
    else:
        # Backward-compatible fallback for pure callers without a slot model.
        unavoidable_charge_input = min(
            max(settings.expected_pre_risk_surplus_kwh, 0.0),
            minimum_charge_floor_kw * pre_risk_hours,
        )
    unavoidable_minimum_charge = unavoidable_charge_input * efficiency
    absorbable_risk_surplus = max(
        settings.expected_absorbable_risk_surplus_kwh
        if settings.expected_absorbable_risk_surplus_kwh is not None
        else settings.expected_risk_surplus_kwh,
        0.0,
    )
    # Headroom is battery-side stored energy. Charging losses reduce the
    # amount that reaches the cells; dividing here would reserve too much
    # space and needlessly sacrifice earlier export.
    unconstrained_aggregate_headroom = (
        absorbable_risk_surplus * efficiency + unavoidable_minimum_charge
    )
    aggregate_required_headroom = unconstrained_aggregate_headroom
    available_headroom = settings.battery_capacity_kwh * (
        100.0 - _clamp(settings.battery_soc_percent, 0.0, 100.0)
    ) / 100.0
    natural_headroom = min(
        max(settings.expected_natural_headroom_kwh, 0.0),
        max(
            settings.battery_capacity_kwh
            * (
                _clamp(settings.battery_soc_percent, 0.0, 100.0)
                - protected_minimum_soc
            )
            / 100.0,
            0.0,
        ),
    )
    available_above_protected = max(
        current_battery_energy - nominal_pre_discharge_floor_energy,
        0.0,
    )
    window_plans: list[RCMRiskWindowPlan] = []
    cumulative_window_requirement = 0.0
    operational_required_headroom = 0.0
    window_discharge_requirement = 0.0
    any_window_power_limited = False
    total_window_absorbable_surplus = 0.0
    for index, window in enumerate(
        sorted(
            settings.risk_window_forecasts,
            key=lambda item: (item.day_offset, item.start_minute),
        )
    ):
        window_absorbable_surplus = max(
            window.absorbable_surplus_kwh
            if window.absorbable_surplus_kwh is not None
            else window.expected_surplus_kwh,
            0.0,
        )
        total_window_absorbable_surplus += window_absorbable_surplus
        power_limited = bool(
            window.absorption_power_limited
            or window_absorbable_surplus
            < max(window.expected_surplus_kwh, 0.0) - 0.001
        )
        any_window_power_limited = any_window_power_limited or power_limited
        incremental_requirement = (
            window_absorbable_surplus * efficiency
            + (unavoidable_minimum_charge if index == 0 else 0.0)
        )
        natural_before = min(
            max(window.natural_headroom_before_kwh, 0.0),
            available_above_protected,
        )
        projected_before = max(
            available_headroom
            + natural_before
            - cumulative_window_requirement,
            0.0,
        )
        cumulative_window_requirement += incremental_requirement
        required_from_initial_headroom = max(
            cumulative_window_requirement - natural_before,
            0.0,
        )
        operational_required_headroom = max(
            operational_required_headroom,
            required_from_initial_headroom,
        )
        cumulative_shortfall = max(
            required_from_initial_headroom - available_headroom,
            0.0,
        )
        window_discharge_requirement = max(
            window_discharge_requirement,
            cumulative_shortfall,
        )
        window_plans.append(
            RCMRiskWindowPlan(
                start_minute=window.start_minute,
                end_minute=window.end_minute,
                peak_voltage_v=round(window.peak_voltage_v, 3),
                day_offset=window.day_offset,
                expected_pv_kwh=round(max(window.expected_pv_kwh, 0.0), 3),
                expected_load_kwh=round(max(window.expected_load_kwh, 0.0), 3),
                expected_surplus_kwh=round(
                    max(window.expected_surplus_kwh, 0.0),
                    3,
                ),
                required_headroom_kwh=round(incremental_requirement, 3),
                projected_headroom_before_kwh=round(projected_before, 3),
                cumulative_headroom_shortfall_kwh=round(
                    cumulative_shortfall,
                    3,
                ),
                absorbable_surplus_kwh=round(window_absorbable_surplus, 3),
                protected_home_energy_kwh=round(
                    max(window.protected_home_energy_kwh, 0.0),
                    3,
                ),
                stress_protected_home_energy_kwh=round(
                    max(window.stress_protected_home_energy_kwh, 0.0),
                    3,
                ),
                absorption_power_limited=power_limited,
            )
        )
    unconstrained_required_headroom = (
        operational_required_headroom
        if window_plans
        else unconstrained_aggregate_headroom
    )
    required_headroom = unconstrained_required_headroom
    creatable_headroom = available_headroom + available_above_protected
    unabsorbed_surplus_due_floor = max(
        unconstrained_required_headroom - creatable_headroom,
        0.0,
    )
    headroom_capacity_limited = unabsorbed_surplus_due_floor > 0.001
    headroom_power_limited = (
        any_window_power_limited
        if window_plans
        else absorbable_risk_surplus
        < max(settings.expected_risk_surplus_kwh, 0.0) - 0.001
    )
    if window_plans:
        absorbable_risk_surplus = total_window_absorbable_surplus
    headroom_shortfall = max(required_headroom - available_headroom, 0.0)
    planned_grid_discharge = (
        min(window_discharge_requirement, usable_capacity)
        if window_plans
        else max(
            required_headroom - available_headroom - natural_headroom,
            0.0,
        )
    )
    planned_grid_discharge = min(
        planned_grid_discharge,
        available_above_protected,
    )
    safe_stress_discharge = max(
        current_battery_energy - stress_reserve_floor_energy,
        0.0,
    )
    stress_discharge_limited = bool(
        planned_grid_discharge > safe_stress_discharge + 0.001
    )
    # The pessimistic P10-PV/P90-LOAD branch limits predictive discharge to
    # the safe portion instead of discarding useful headroom wholesale. Live
    # voltage emergency handling remains independent and may still absorb PV
    # or limit export.
    planned_grid_discharge = min(
        planned_grid_discharge,
        safe_stress_discharge,
    )
    if battery_capacity_available:
        pre_discharge_target_soc = _clamp(
            settings.battery_soc_percent
            - planned_grid_discharge / settings.battery_capacity_kwh * 100.0,
            nominal_pre_discharge_floor_soc,
            100.0,
        )
        target_soc = _clamp(
            100.0 - required_headroom / settings.battery_capacity_kwh * 100.0,
            nominal_pre_discharge_floor_soc,
            100.0,
        )
    else:
        # Without a physical capacity there is no sound kWh->SOC conversion.
        # Holding the current target describes the fail-closed no-op exactly.
        pre_discharge_target_soc = _clamp(
            settings.battery_soc_percent,
            0.0,
            100.0,
        )
        target_soc = pre_discharge_target_soc

    # Preserve the SOC floor before calculating achievable commanded energy.
    pre_discharge_target_soc = float(ceil(pre_discharge_target_soc))
    target_soc = float(ceil(target_soc))
    planned_grid_discharge = min(
        planned_grid_discharge,
        max(settings.battery_soc_percent - pre_discharge_target_soc, 0.0)
        * settings.battery_capacity_kwh / 100.0,
    )

    maximum_limit_percent = (
        _clamp(
            bms_limit_kw * 100.0 / settings.system_power_kw,
            MINIMUM_CHARGE_LIMIT_PERCENT,
            100.0,
        )
        if bms_charge_available
        else current_limit
    )
    pv_surplus = (
        max(settings.pv_power_kw - settings.load_power_kw, 0.0)
        if settings.live_power_data_fresh
        else 0.0
    )
    risk_ahead = next_risk_start is not None and next_risk_start > minute

    if live_emergency:
        status = (
            "emergency" if emergency_action_ready else "emergency_actuator_unavailable"
        )
        if (
            charge_actuator_fresh
            and bms_charge_available
        ):
            action = "absorb_pv"
            unconstrained_target = maximum_limit_percent
        elif (
            settings.export_control_enabled
            and settings.gcf_active
            and settings.gcf_data_fresh
            and export_actuator_fresh
        ):
            action = "limit_export"
            unconstrained_target = current_limit
        else:
            action = "monitor"
            unconstrained_target = current_limit
    elif not settings.voltage_data_fresh:
        status = "stale_voltage"
        action = "hold"
        unconstrained_target = current_limit
    elif active_window or control_voltage >= P_U_START_V:
        status = "controlling"
        if not settings.live_power_data_fresh:
            action = "monitor"
            unconstrained_target = current_limit
        elif not bms_charge_available:
            status = "battery_charge_unavailable"
            action = (
                "limit_export"
                if settings.export_control_enabled
                and settings.gcf_active
                and settings.gcf_data_fresh
                else "monitor"
            )
            unconstrained_target = current_limit
        elif pv_surplus <= 0.05:
            action = "monitor"
            unconstrained_target = saved_limit
        else:
            # The predicted profile starts the battery gently.  Live voltage
            # then takes over and can use the complete BMS-safe power.
            desired_absorption_kw = min(
                pv_surplus * max(risk_score, 0.20 if active_window else 0.0),
                bms_limit_kw,
            )
            unconstrained_target = _clamp(
                desired_absorption_kw / settings.system_power_kw * 100.0,
                MINIMUM_CHARGE_LIMIT_PERCENT,
                maximum_limit_percent,
            )
            if control_voltage >= WARNING_V:
                unconstrained_target = max(
                    unconstrained_target,
                    min(75.0, maximum_limit_percent),
                )
            if control_voltage >= EMERGENCY_V:
                unconstrained_target = maximum_limit_percent
            action = (
                "absorb_pv"
                if unconstrained_target >= current_limit
                else "release_export"
            )
    elif settings.history_days == 0:
        status = "learning"
        action = "restore"
        unconstrained_target = saved_limit
    elif not settings.history_data_fresh:
        status = "history_stale"
        action = "restore"
        unconstrained_target = saved_limit
    elif not settings.forecast_data_fresh:
        status = "forecast_stale"
        action = "restore"
        unconstrained_target = saved_limit
    elif (
        prediction_ready
        and settings.risk_day_offset == 0
        and planned_grid_discharge > 0.1
        and control_voltage < P_U_START_V
    ):
        status = "preparing_discharge"
        action = "grid_discharge_preparation"
        unconstrained_target = MINIMUM_CHARGE_LIMIT_PERCENT
    elif (
        risk_ahead
        and not stress_reserve_energy_critical
        and settings.battery_soc_percent >= target_soc - 0.5
    ):
        status = "preparing_headroom"
        action = "preserve_headroom"
        unconstrained_target = MINIMUM_CHARGE_LIMIT_PERCENT
    else:
        status = "ready"
        action = "restore"
        unconstrained_target = saved_limit

    # Normal regulation changes the global battery limit by at most ten
    # percentage points per run.  The emergency path may move by 25 points.
    if (
        live_emergency
        and charge_actuator_fresh
        and bms_charge_available
    ):
        # At the legal disconnection boundary there is no time for the normal
        # minute-by-minute ramp.  Use all BMS-safe absorption immediately.
        recommended = maximum_limit_percent
    elif live_emergency or not charge_actuator_fresh:
        # Never invent a new battery command from stale actuator/BMS data.
        recommended = current_limit
    else:
        recommended = _clamp(
            unconstrained_target,
            current_limit - 10.0,
            current_limit + 10.0,
        )
    recommended = _clamp(
        recommended,
        MINIMUM_CHARGE_LIMIT_PERCENT,
        maximum_limit_percent,
    )
    recommended = _quantize_power_percent(recommended)
    recommended_power = (
        min(
            settings.system_power_kw * recommended / 100.0,
            bms_limit_kw,
        )
        if bms_charge_available
        else 0.0
    )
    saturated = (
        bms_charge_available
        and recommended >= maximum_limit_percent - 0.05
        and control_voltage >= WARNING_V
    )

    # The explicit user cap is always a hard ceiling. Actual GCF provenance is
    # separate from permission to modify its register. With GCF enabled the
    # *live* register is an immediate physical cap and the saved value is the
    # restore ceiling. With GCF disabled both are dormant and must be ignored.
    effective_export_cap = _clamp(
        (
            min(
                settings.current_export_limit_percent,
                settings.saved_export_limit_percent,
                settings.user_export_cap_percent,
            )
            if settings.gcf_active and settings.gcf_data_fresh
            else settings.user_export_cap_percent
        ),
        0.0,
        100.0,
    )
    current_export_limit = _clamp(
        settings.current_export_limit_percent,
        0.0,
        100.0,
    )
    export_control_path_enabled = bool(
        settings.export_control_enabled
        and settings.gcf_active
        and settings.gcf_data_fresh
    )
    export_adjustment_ceiling = _clamp(
        min(
            settings.saved_export_limit_percent,
            settings.user_export_cap_percent,
        ),
        0.0,
        100.0,
    )
    if not export_control_path_enabled:
        recommended_export_limit = current_export_limit
    elif live_emergency and export_actuator_fresh:
        recommended_export_limit = 0.0
    elif (
        not settings.voltage_data_fresh
        or not export_actuator_fresh
    ):
        recommended_export_limit = current_export_limit
    elif current_export_limit > effective_export_cap:
        recommended_export_limit = effective_export_cap
    elif control_voltage >= EMERGENCY_V:
        recommended_export_limit = current_export_limit - 15.0
    elif control_voltage >= WARNING_V and (
        saturated or available_headroom <= 0.25
    ):
        recommended_export_limit = current_export_limit - 5.0
    elif control_voltage <= P_U_START_V:
        recommended_export_limit = current_export_limit + 5.0
    else:
        recommended_export_limit = current_export_limit
    recommended_export_limit = _clamp(
        recommended_export_limit,
        0.0,
        export_adjustment_ceiling
        if export_control_path_enabled
        else current_export_limit,
    )
    recommended_export_limit = _quantize_power_percent(recommended_export_limit)

    export_capacity_kw = (
        settings.system_power_kw * effective_export_cap / 100.0
    )
    discharge_window_hours = max(
        ((settings.minutes_to_risk or 0) - 30) / 60.0,
        0.0,
    )
    # Maximum Discharge Power controls total battery AC output, while the
    # contractual cap applies to the common grid export after PV and the home
    # have been balanced: GRID = PV + BATTERY - LOAD.  Subtract the existing
    # fresh PV contribution before assigning any export budget to the battery.
    # The measured GRID value is deliberately not added here because during an
    # active pre-discharge it already contains battery power and would count
    # the same output twice.
    live_pv_for_export_kw = (
        max(settings.pv_power_kw, 0.0)
        if settings.live_power_data_fresh
        else 0.0
    )
    live_load_for_export_kw = (
        max(settings.load_power_kw, 0.0)
        if settings.live_power_data_fresh
        else 0.0
    )
    battery_export_budget_kw = max(
        export_capacity_kw
        + live_load_for_export_kw
        - live_pv_for_export_kw,
        0.0,
    )
    if (
        planned_grid_discharge > 0.1
        and discharge_window_hours > 0
        and export_capacity_kw > 0.1
        and settings.live_power_data_fresh
    ):
        desired_grid_export_kw = max(
            planned_grid_discharge / discharge_window_hours * 1.10,
            settings.system_power_kw * 0.05,
        )
        desired_battery_discharge_kw = max(
            desired_grid_export_kw
            + live_load_for_export_kw
            - live_pv_for_export_kw,
            0.0,
        )
        pre_discharge_power = min(
            desired_battery_discharge_kw,
            bms_discharge_limit_kw,
            battery_export_budget_kw,
        )
    else:
        pre_discharge_power = 0.0
    pre_discharge_power_percent = _clamp(
        pre_discharge_power * 100.0 / settings.system_power_kw,
        0.0,
        100.0,
    )
    # New register 4306 commands use whole percentage points. Quantize down,
    # then use that same command-equivalent AC power in the public result and
    # timeline so rounding can never exceed the battery-side DC allowance.
    pre_discharge_power_percent = _quantize_power_percent(pre_discharge_power_percent)
    pre_discharge_power = min(
        pre_discharge_power,
        settings.system_power_kw * pre_discharge_power_percent / 100.0,
    )
    pre_discharge_deadline = (
        settings.now
        + timedelta(minutes=max((settings.minutes_to_risk or 0) - 30, 0))
        if settings.risk_day_offset == 0
        and (settings.minutes_to_risk or 0) > 30
        else None
    )
    pre_discharge_hard_safe = (
        battery_capacity_available
        and settings.risk_day_offset == 0
        and settings.voltage_data_fresh
        and settings.gcf_data_fresh
        and settings.actuator_data_fresh
        and settings.pre_discharge_actuator_data_fresh
        and bms_charge_available
        and bms_discharge_available
        and maximum_voltage < P_U_START_V
        and settings.rolling_10m_voltage_v < CONTROL_TARGET_V
        and (settings.minutes_to_risk or 0) > 30
        and settings.battery_soc_percent > nominal_pre_discharge_floor_soc + 0.5
        and not stress_reserve_energy_critical
    )
    pre_discharge_continue_eligible = bool(
        settings.pre_discharge_active and pre_discharge_hard_safe
    )
    pre_discharge_start_eligible = bool(
        pre_discharge_hard_safe
        and not settings.pre_discharge_active
        and prediction_ready
        and settings.live_power_data_fresh
        and planned_grid_discharge > 0.1
        and pre_discharge_power > 0.1
        and export_capacity_kw > 0.1
        and settings.battery_soc_percent > pre_discharge_target_soc + 0.5
    )
    pre_discharge_transaction_ready = bool(
        pre_discharge_start_eligible
        and settings.pre_discharge_actuator_data_fresh
    )
    # Backward-compatible public flag remains true for either a safe new start
    # or a safe continuation. New schedulers should consume the two explicit
    # contracts and latch target/deadline at transaction start.
    pre_discharge_ready = bool(
        pre_discharge_start_eligible or pre_discharge_continue_eligible
    )

    # This estimate is intentionally conservative until local voltage/export
    # sensitivity has been learned from a site with actual export.
    safe_export_factor = _clamp(
        (EMERGENCY_V - control_voltage) / (EMERGENCY_V - CONTROL_TARGET_V),
        0.0,
        1.0,
    )
    export_limit_kw = (
        settings.system_power_kw * recommended_export_limit / 100.0
    )
    safe_export = (
        max(
            min(
                pv_surplus,
                settings.grid_export_power_kw
                if settings.grid_export_power_kw > 0.05
                else pv_surplus,
                export_limit_kw,
            )
            * safe_export_factor,
            0.0,
        )
        if pv_surplus > 0.05
        else None
    )
    if saturated and status == "controlling":
        status = "battery_limited"

    action_interval_start: datetime | None = None
    action_interval_end: datetime | None = None
    if action == "grid_discharge_preparation":
        action_interval_start = settings.now
        action_interval_end = (
            pre_discharge_deadline
            if pre_discharge_deadline is not None
            and pre_discharge_deadline > settings.now
            else settings.now
            + timedelta(seconds=LIVE_ACTION_VALIDITY_SECONDS)
        )
    elif action == "preserve_headroom" and settings.minutes_to_risk is not None:
        action_interval_start = settings.now
        action_interval_end = settings.now + timedelta(
            minutes=max(settings.minutes_to_risk, 0)
        )
    elif action in {"absorb_pv", "limit_export"}:
        action_interval_start = settings.now
        action_interval_end = settings.now + timedelta(
            seconds=LIVE_ACTION_VALIDITY_SECONDS
        )

    return RCMOptimizerResult(
        status_code=status,
        action=action,
        action_interval_start=action_interval_start,
        action_interval_end=action_interval_end,
        maximum_voltage_v=round(maximum_voltage, 2),
        control_voltage_v=round(control_voltage, 2),
        voltage_risk_score=round(risk_score * 100.0, 1),
        risk_window_active=active_window,
        next_risk_start_minute=next_risk_start,
        reserve_soc_percent=round(reserve_soc, 1),
        protected_minimum_soc_percent=round(protected_minimum_soc, 1),
        required_headroom_kwh=round(required_headroom, 3),
        available_headroom_kwh=round(available_headroom, 3),
        headroom_shortfall_kwh=round(headroom_shortfall, 3),
        expected_natural_headroom_kwh=round(natural_headroom, 3),
        planned_grid_discharge_kwh=round(planned_grid_discharge, 3),
        pre_discharge_target_soc_percent=pre_discharge_target_soc,
        pre_discharge_power_kw=round(pre_discharge_power, 3),
        pre_discharge_power_percent=round(pre_discharge_power_percent, 1),
        pre_discharge_ready=pre_discharge_ready,
        target_soc_before_risk_percent=target_soc,
        pv_surplus_power_kw=round(pv_surplus, 3),
        bms_charge_power_limit_kw=round(bms_limit_kw, 3),
        recommended_charge_limit_percent=round(recommended, 1),
        recommended_charge_power_kw=round(recommended_power, 3),
        effective_export_cap_percent=round(effective_export_cap, 1),
        recommended_export_limit_percent=round(
            recommended_export_limit,
            1,
        ),
        estimated_safe_export_power_kw=(
            round(safe_export, 3) if safe_export is not None else None
        ),
        saturated=saturated,
        unavoidable_minimum_charge_kwh=round(
            unavoidable_minimum_charge,
            3,
        ),
        risk_window_plans=tuple(window_plans),
        live_emergency=live_emergency,
        emergency_action_ready=emergency_action_ready,
        prediction_ready=prediction_ready,
        prediction_block_reason=prediction_block_reason,
        system_power_data_valid=settings.system_power_data_valid,
        voltage_data_fresh=settings.voltage_data_fresh,
        emergency_voltage_data_fresh=emergency_voltage_data_fresh,
        actuator_data_fresh=settings.actuator_data_fresh,
        charge_actuator_data_fresh=charge_actuator_fresh,
        export_actuator_data_fresh=export_actuator_fresh,
        forecast_data_fresh=settings.forecast_data_fresh,
        load_profile_data_fresh=settings.load_profile_data_fresh,
        history_data_fresh=settings.history_data_fresh,
        live_power_data_fresh=settings.live_power_data_fresh,
        bms_charge_available=bms_charge_available,
        bms_charge_quantization_limited=bms_charge_quantization_limited,
        bms_discharge_available=bms_discharge_available,
        bms_discharge_dc_power_limit_kw=round(
            bms_discharge_dc_limit_kw,
            3,
        ),
        bms_discharge_power_limit_kw=round(bms_discharge_limit_kw, 3),
        absorbable_risk_surplus_kwh=round(absorbable_risk_surplus, 3),
        protected_home_energy_kwh=round(nominal_home_buffer, 3),
        headroom_power_limited=headroom_power_limited,
        headroom_capacity_limited=headroom_capacity_limited,
        pre_discharge_start_eligible=pre_discharge_start_eligible,
        pre_discharge_continue_eligible=pre_discharge_continue_eligible,
        pre_discharge_transaction_ready=pre_discharge_transaction_ready,
        pre_discharge_deadline=pre_discharge_deadline,
        unconstrained_required_headroom_kwh=round(
            unconstrained_required_headroom,
            3,
        ),
        creatable_headroom_kwh=round(creatable_headroom, 3),
        unabsorbed_surplus_due_floor_kwh=round(
            unabsorbed_surplus_due_floor,
            3,
        ),
        nominal_pre_risk_home_buffer_kwh=round(nominal_home_buffer, 3),
        stress_protected_home_energy_kwh=round(stress_home_buffer, 3),
        stress_reserve_energy_critical=stress_reserve_energy_critical,
        stress_discharge_limited=stress_discharge_limited,
    )
