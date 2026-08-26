"""Pure observation-only RCEm chronological timeline projection.

The model accepts timestamped energy that the RCEm source sensor already
captured plus the exact committed optimizer result.  It never reads Home
Assistant, Recorder, the network, an executor, or an inverter, and its output
is never an optimizer input.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from math import isfinite

from .automation_plan_timeline import (
    MAX_POINTS,
    OptimizerTimelineTrace,
    RCMPolicyPoint,
    TimelineTracePoint,
)
from .rcm_optimizer import RCMOptimizerResult


SLOT_MINUTES = 15
MAX_HORIZON_SECONDS = 48 * 60 * 60
_EPSILON = 1e-9


class RCMTimelineModelError(ValueError):
    """Bounded fail-closed model error suitable for timeline publication."""

    def __init__(self, blocker_code: str) -> None:
        super().__init__(blocker_code)
        self.blocker_code = blocker_code


@dataclass(frozen=True, slots=True)
class RCMTimelineEnergyPoint:
    """One exact UTC source interval expressed as energy."""

    start: datetime
    end: datetime
    pv_kwh: float | None
    load_kwh: float | None


@dataclass(frozen=True, slots=True)
class RCMTimelineRiskInterval:
    """One absolute interval derived from an existing historical risk window."""

    start: datetime
    end: datetime
    voltage_risk_code: str
    required_headroom_kwh: float
    headroom_shortfall_kwh: float | None


@dataclass(frozen=True, slots=True)
class RCMTimelineModelInput:
    """Immutable captured inputs for one deterministic RCEm projection."""

    generated_at: datetime
    energy_points: tuple[RCMTimelineEnergyPoint, ...]
    risk_intervals: tuple[RCMTimelineRiskInterval, ...]
    battery_capacity_kwh: float | None
    current_soc_percent: float | None
    protected_soc_floor_percent: float | None
    maximum_soc_percent: float | None
    charge_power_limit_kw: float | None
    discharge_power_limit_kw: float | None
    charge_efficiency: float | None
    discharge_efficiency: float | None
    system_power_kw: float | None
    result: RCMOptimizerResult
    inputs_fresh: bool
    quality: str = "complete"
    requested_horizon_start: datetime | None = None
    requested_horizon_end: datetime | None = None


def _finite_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and isfinite(float(value))
    )


def _optional_non_negative(value: float | None) -> bool:
    return value is None or (_finite_number(value) and float(value) >= 0.0)


def _utc(value: datetime, blocker: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise RCMTimelineModelError(blocker)
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError) as err:
        raise RCMTimelineModelError(blocker) from err


def _validate_inputs(inputs: RCMTimelineModelInput) -> None:
    generated_at = _utc(inputs.generated_at, "invalid_generated_at")
    if not inputs.inputs_fresh:
        raise RCMTimelineModelError("stale_or_unavailable_inputs")
    if inputs.quality not in {"complete", "partial", "degraded", "stale"}:
        raise RCMTimelineModelError("invalid_source_quality")
    if not inputs.energy_points:
        raise RCMTimelineModelError("source_coverage_unavailable")
    if len(inputs.energy_points) > MAX_POINTS:
        raise RCMTimelineModelError("point_limit_exceeded")

    previous_end: datetime | None = None
    for point in inputs.energy_points:
        start = _utc(point.start, "invalid_energy_interval")
        end = _utc(point.end, "invalid_energy_interval")
        if (end - start).total_seconds() != SLOT_MINUTES * 60:
            raise RCMTimelineModelError("non_native_energy_interval")
        if previous_end is not None and start != previous_end:
            raise RCMTimelineModelError("energy_interval_gap_or_overlap")
        previous_end = end
        if not _optional_non_negative(point.pv_kwh) or not _optional_non_negative(
            point.load_kwh
        ):
            raise RCMTimelineModelError("invalid_energy_value")
    first = _utc(inputs.energy_points[0].start, "invalid_energy_interval")
    last = _utc(inputs.energy_points[-1].end, "invalid_energy_interval")
    if (last - first).total_seconds() > MAX_HORIZON_SECONDS:
        raise RCMTimelineModelError("horizon_limit_exceeded")
    if last <= generated_at - (last - first):
        raise RCMTimelineModelError("past_only_horizon")
    requested_start, requested_end = _requested_horizon(inputs)
    if (requested_end - requested_start).total_seconds() > MAX_HORIZON_SECONDS:
        raise RCMTimelineModelError("horizon_limit_exceeded")

    for risk in inputs.risk_intervals:
        start = _utc(risk.start, "invalid_risk_interval")
        end = _utc(risk.end, "invalid_risk_interval")
        if end <= start:
            raise RCMTimelineModelError("invalid_risk_interval")
        if (
            not isinstance(risk.voltage_risk_code, str)
            or not risk.voltage_risk_code
            or len(risk.voltage_risk_code) > 64
            or not risk.voltage_risk_code.replace("_", "a").isalnum()
        ):
            raise RCMTimelineModelError("invalid_risk_code")
        if not _optional_non_negative(risk.required_headroom_kwh):
            raise RCMTimelineModelError("invalid_required_headroom")
        if not _optional_non_negative(risk.headroom_shortfall_kwh):
            raise RCMTimelineModelError("invalid_headroom_shortfall")

    for value in (
        inputs.battery_capacity_kwh,
        inputs.charge_power_limit_kw,
        inputs.discharge_power_limit_kw,
        inputs.system_power_kw,
    ):
        if not _optional_non_negative(value):
            raise RCMTimelineModelError("invalid_physical_limit")
    for value in (inputs.current_soc_percent, inputs.protected_soc_floor_percent, inputs.maximum_soc_percent):
        if value is not None and (
            not _finite_number(value) or not 0.0 <= float(value) <= 100.0
        ):
            raise RCMTimelineModelError("invalid_soc_limit")
    for value in (inputs.charge_efficiency, inputs.discharge_efficiency):
        if value is not None and (
            not _finite_number(value) or not 0.0 < float(value) <= 1.0
        ):
            raise RCMTimelineModelError("invalid_efficiency")


def _risk_for_point(
    point: RCMTimelineEnergyPoint,
    risks: tuple[RCMTimelineRiskInterval, ...],
) -> RCMTimelineRiskInterval | None:
    start = _utc(point.start, "invalid_energy_interval")
    end = _utc(point.end, "invalid_energy_interval")
    overlapping = [
        risk
        for risk in risks
        if _utc(risk.start, "invalid_risk_interval") < end
        and _utc(risk.end, "invalid_risk_interval") > start
    ]
    if not overlapping:
        return None
    return min(
        overlapping,
        key=lambda risk: (
            _utc(risk.start, "invalid_risk_interval"),
            _utc(risk.end, "invalid_risk_interval"),
            risk.voltage_risk_code,
        ),
    )


def _requested_horizon(
    inputs: RCMTimelineModelInput,
) -> tuple[datetime, datetime]:
    first = _utc(inputs.energy_points[0].start, "invalid_energy_interval")
    last = _utc(inputs.energy_points[-1].end, "invalid_energy_interval")
    start = (
        _utc(inputs.requested_horizon_start, "invalid_requested_horizon")
        if inputs.requested_horizon_start is not None
        else first
    )
    end = (
        _utc(inputs.requested_horizon_end, "invalid_requested_horizon")
        if inputs.requested_horizon_end is not None
        else last
    )
    if end <= start:
        raise RCMTimelineModelError("invalid_requested_horizon")
    return start, end


def _merged_action_windows(
    inputs: RCMTimelineModelInput,
) -> tuple[tuple[datetime, datetime], ...]:
    action = inputs.result.action
    requested_start, _requested_end = _requested_horizon(inputs)
    if action == "grid_discharge_preparation":
        if (
            inputs.result.pre_discharge_deadline is None
            or inputs.result.pre_discharge_power_kw <= 0.0
        ):
            return ()
        start = max(
            _utc(inputs.generated_at, "invalid_generated_at"),
            requested_start,
        )
        end = _utc(
            inputs.result.pre_discharge_deadline,
            "invalid_pre_discharge_deadline",
        )
        return ((start, end),) if end > start else ()
    if action not in {"absorb_pv", "preserve_headroom", "limit_export"}:
        return ()
    windows = sorted(
        (
            _utc(risk.start, "invalid_risk_interval"),
            _utc(risk.end, "invalid_risk_interval"),
        )
        for risk in inputs.risk_intervals
    )
    merged: list[tuple[datetime, datetime]] = []
    for start, end in windows:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def _overlap_seconds(
    start: datetime,
    end: datetime,
    window_start: datetime,
    window_end: datetime,
) -> float:
    return max(
        (min(end, window_end) - max(start, window_start)).total_seconds(),
        0.0,
    )


def _point_action_overlap_seconds(
    inputs: RCMTimelineModelInput,
    point: RCMTimelineEnergyPoint,
) -> float:
    start = _utc(point.start, "invalid_energy_interval")
    end = _utc(point.end, "invalid_energy_interval")
    return sum(
        _overlap_seconds(start, end, window_start, window_end)
        for window_start, window_end in _merged_action_windows(inputs)
    )


def _coverage_limited_by_window(inputs: RCMTimelineModelInput) -> bool:
    first = _utc(inputs.energy_points[0].start, "invalid_energy_interval")
    last = _utc(inputs.energy_points[-1].end, "invalid_energy_interval")
    requested_start, requested_end = _requested_horizon(inputs)
    windows = [
        (
            _utc(risk.start, "invalid_risk_interval"),
            _utc(risk.end, "invalid_risk_interval"),
        )
        for risk in inputs.risk_intervals
    ]
    if inputs.result.action == "grid_discharge_preparation":
        windows.extend(_merged_action_windows(inputs))
    return any(
        _overlap_seconds(start, end, requested_start, requested_end) > 0.0
        and (start < first or end > last)
        for start, end in windows
    )


def _battery_inputs_complete(inputs: RCMTimelineModelInput) -> bool:
    values = (
        inputs.battery_capacity_kwh,
        inputs.current_soc_percent,
        inputs.protected_soc_floor_percent,
        inputs.maximum_soc_percent,
        inputs.charge_power_limit_kw,
        inputs.discharge_power_limit_kw,
        inputs.charge_efficiency,
        inputs.discharge_efficiency,
    )
    complete = all(value is not None for value in values) and bool(
        inputs.battery_capacity_kwh and inputs.battery_capacity_kwh > 0.0
    )
    if inputs.result.action == "limit_export":
        complete = bool(
            complete
            and inputs.system_power_kw is not None
            and inputs.system_power_kw > 0.0
        )
    return complete


def _natural_step(
    *,
    stored_kwh: float,
    pv_kwh: float,
    load_kwh: float,
    floor_kwh: float,
    ceiling_kwh: float,
    duration_hours: float,
    charge_power_limit_kw: float,
    discharge_power_limit_kw: float,
    charge_efficiency: float,
    discharge_efficiency: float,
) -> tuple[float, float]:
    """Apply the existing chronological direct-PV/headroom semantics once."""

    direct_pv = min(pv_kwh, load_kwh)
    surplus_kwh = pv_kwh - direct_pv
    deficit_kwh = load_kwh - direct_pv
    if surplus_kwh > _EPSILON:
        charge_input_kwh = min(
            surplus_kwh,
            charge_power_limit_kw * duration_hours,
            max(ceiling_kwh - stored_kwh, 0.0) / charge_efficiency,
        )
        return (
            min(stored_kwh + charge_input_kwh * charge_efficiency, ceiling_kwh),
            charge_input_kwh,
        )
    discharge_output_kwh = min(
        deficit_kwh,
        discharge_power_limit_kw * duration_hours,
        max(stored_kwh - floor_kwh, 0.0) * discharge_efficiency,
    )
    return (
        max(stored_kwh - discharge_output_kwh / discharge_efficiency, floor_kwh),
        -discharge_output_kwh,
    )


def _planned_step(
    inputs: RCMTimelineModelInput,
    point: RCMTimelineEnergyPoint,
    *,
    stored_kwh: float,
    floor_kwh: float,
    ceiling_kwh: float,
    pv_kwh: float,
    load_kwh: float,
) -> tuple[float, float, float, float, float]:
    result = inputs.result
    point_start = _utc(point.start, "invalid_energy_interval")
    point_end = _utc(point.end, "invalid_energy_interval")
    point_seconds = (point_end - point_start).total_seconds()
    windows = _merged_action_windows(inputs)
    boundaries = {point_start, point_end}
    for window_start, window_end in windows:
        if window_start < point_end and window_end > point_start:
            boundaries.add(max(window_start, point_start))
            boundaries.add(min(window_end, point_end))
    ordered = sorted(boundaries)
    battery_ac_kwh = 0.0
    planned_pv_kwh = 0.0
    pre_discharge_output_kwh = 0.0
    action_overlap_seconds = 0.0
    current_stored = stored_kwh
    for segment_start, segment_end in zip(ordered, ordered[1:]):
        segment_seconds = (segment_end - segment_start).total_seconds()
        if segment_seconds <= 0.0:
            continue
        duration_hours = segment_seconds / 3600.0
        fraction = segment_seconds / point_seconds
        segment_pv = pv_kwh * fraction
        segment_load = load_kwh * fraction
        active = any(
            window_start < segment_end and window_end > segment_start
            for window_start, window_end in windows
        )
        if active:
            action_overlap_seconds += segment_seconds
        if active and result.action == "grid_discharge_preparation":
            target_kwh = max(
                floor_kwh,
                float(inputs.battery_capacity_kwh)
                * result.pre_discharge_target_soc_percent
                / 100.0,
            )
            discharge_output_kwh = min(
                result.pre_discharge_power_kw * duration_hours,
                float(inputs.discharge_power_limit_kw) * duration_hours,
                max(current_stored - target_kwh, 0.0)
                * float(inputs.discharge_efficiency),
            )
            current_stored = max(
                current_stored
                - discharge_output_kwh / float(inputs.discharge_efficiency),
                target_kwh,
            )
            segment_battery = -discharge_output_kwh
            segment_planned_pv = segment_pv
            pre_discharge_output_kwh += discharge_output_kwh
        else:
            charge_limit = float(inputs.charge_power_limit_kw)
            if active and result.action in {"absorb_pv", "preserve_headroom"}:
                charge_limit = min(
                    charge_limit,
                    result.recommended_charge_power_kw,
                )
            current_stored, segment_battery = _natural_step(
                stored_kwh=current_stored,
                pv_kwh=segment_pv,
                load_kwh=segment_load,
                floor_kwh=floor_kwh,
                ceiling_kwh=ceiling_kwh,
                duration_hours=duration_hours,
                charge_power_limit_kw=charge_limit,
                discharge_power_limit_kw=float(inputs.discharge_power_limit_kw),
                charge_efficiency=float(inputs.charge_efficiency),
                discharge_efficiency=float(inputs.discharge_efficiency),
            )
            segment_planned_pv = segment_pv
            if (
                active
                and result.action == "limit_export"
                and inputs.system_power_kw is not None
                and inputs.system_power_kw > 0.0
            ):
                uncapped_grid_kwh = (
                    segment_load + segment_battery - segment_planned_pv
                )
                export_cap_kwh = (
                    inputs.system_power_kw
                    * result.recommended_export_limit_percent
                    / 100.0
                    * duration_hours
                )
                if uncapped_grid_kwh < -export_cap_kwh:
                    segment_planned_pv -= -uncapped_grid_kwh - export_cap_kwh
                    segment_planned_pv = max(segment_planned_pv, 0.0)
        battery_ac_kwh += segment_battery
        planned_pv_kwh += segment_planned_pv
    return (
        current_stored,
        battery_ac_kwh,
        planned_pv_kwh,
        pre_discharge_output_kwh,
        action_overlap_seconds,
    )


def build_rcm_timeline_trace(
    inputs: RCMTimelineModelInput,
) -> OptimizerTimelineTrace:
    """Build one deterministic 15-minute RCEm trace from captured facts."""

    _validate_inputs(inputs)
    complete_battery = _battery_inputs_complete(inputs)
    if complete_battery:
        capacity = float(inputs.battery_capacity_kwh)
        floor_soc = float(inputs.protected_soc_floor_percent)
        ceiling_soc = float(inputs.maximum_soc_percent)
        if ceiling_soc < floor_soc:
            raise RCMTimelineModelError("incoherent_soc_limits")
        floor_kwh = capacity * floor_soc / 100.0
        ceiling_kwh = capacity * ceiling_soc / 100.0
        initial_kwh = capacity * float(inputs.current_soc_percent) / 100.0
        if initial_kwh < floor_kwh - _EPSILON or initial_kwh > ceiling_kwh + _EPSILON:
            raise RCMTimelineModelError("current_soc_outside_operating_bounds")
        baseline_stored = initial_kwh
        planned_stored = baseline_stored
    else:
        capacity = 0.0
        floor_soc = (
            float(inputs.protected_soc_floor_percent)
            if inputs.protected_soc_floor_percent is not None
            else None
        )
        floor_kwh = 0.0
        ceiling_kwh = 0.0
        baseline_stored = 0.0
        planned_stored = 0.0

    trace_points: list[TimelineTracePoint] = []
    for source in inputs.energy_points:
        risk = _risk_for_point(source, inputs.risk_intervals)
        action_overlap_seconds = _point_action_overlap_seconds(inputs, source)
        point_action = (
            inputs.result.action if action_overlap_seconds > _EPSILON else "idle"
        )
        source_complete = source.pv_kwh is not None and source.load_kwh is not None
        point_complete = complete_battery and source_complete
        required_headroom = (
            risk.required_headroom_kwh
            if risk is not None
            else inputs.result.required_headroom_kwh
            if point_action == "grid_discharge_preparation"
            else None
        )
        headroom_shortfall = (
            risk.headroom_shortfall_kwh
            if risk is not None
            else inputs.result.headroom_shortfall_kwh
            if point_action == "grid_discharge_preparation"
            else None
        )
        target_soc = (
            inputs.result.pre_discharge_target_soc_percent
            if point_action == "grid_discharge_preparation"
            else inputs.result.target_soc_before_risk_percent
            if risk is not None
            else None
        )
        planned_pre_discharge_kw: float | None = None
        if point_complete:
            pv_kwh = float(source.pv_kwh)
            load_kwh = float(source.load_kwh)
            duration_hours = (
                _utc(source.end, "invalid_energy_interval")
                - _utc(source.start, "invalid_energy_interval")
            ).total_seconds() / 3600.0
            baseline_stored, _baseline_battery_ac = _natural_step(
                stored_kwh=baseline_stored,
                pv_kwh=pv_kwh,
                load_kwh=load_kwh,
                floor_kwh=floor_kwh,
                ceiling_kwh=ceiling_kwh,
                duration_hours=duration_hours,
                charge_power_limit_kw=float(inputs.charge_power_limit_kw),
                discharge_power_limit_kw=float(inputs.discharge_power_limit_kw),
                charge_efficiency=float(inputs.charge_efficiency),
                discharge_efficiency=float(inputs.discharge_efficiency),
            )
            (
                planned_stored,
                planned_battery_ac,
                planned_pv,
                pre_discharge_output_kwh,
                _actual_action_overlap_seconds,
            ) = _planned_step(
                inputs,
                source,
                stored_kwh=planned_stored,
                floor_kwh=floor_kwh,
                ceiling_kwh=ceiling_kwh,
                pv_kwh=pv_kwh,
                load_kwh=load_kwh,
            )
            if point_action == "grid_discharge_preparation":
                planned_pre_discharge_kw = (
                    pre_discharge_output_kwh / duration_hours
                )
            grid_kwh = load_kwh + planned_battery_ac - planned_pv
            grid_import_kwh = max(grid_kwh, 0.0)
            grid_export_kwh = max(-grid_kwh, 0.0)
            baseline_soc = baseline_stored / capacity * 100.0
            planned_soc = planned_stored / capacity * 100.0
            point_quality = inputs.quality
            battery_delta_kwh = planned_battery_ac
            point_pv_kwh = planned_pv
            point_load_kwh = load_kwh
        else:
            grid_import_kwh = None
            grid_export_kwh = None
            baseline_soc = None
            planned_soc = None
            point_quality = "partial"
            battery_delta_kwh = None
            point_pv_kwh = source.pv_kwh
            point_load_kwh = source.load_kwh

        policy = RCMPolicyPoint(
            voltage_risk_code=(
                risk.voltage_risk_code if risk is not None else "no_risk"
            ),
            planned_export_limit_percent=(
                inputs.result.recommended_export_limit_percent
                if point_action == "limit_export"
                else None
            ),
            planned_pre_discharge_kw=planned_pre_discharge_kw,
            headroom_shortfall_kwh=headroom_shortfall,
            control_mode=point_action,
        )

        trace_points.append(
            TimelineTracePoint(
                start=_utc(source.start, "invalid_energy_interval"),
                end=_utc(source.end, "invalid_energy_interval"),
                pv_kwh=point_pv_kwh,
                load_kwh=point_load_kwh,
                battery_delta_kwh=battery_delta_kwh,
                grid_import_kwh=grid_import_kwh,
                grid_export_kwh=grid_export_kwh,
                soc_percent=planned_soc,
                baseline_soc_percent=baseline_soc,
                protected_soc_floor_percent=floor_soc,
                action_code=point_action,
                selected=(risk is not None or point_action != "idle"),
                quality=point_quality,
                policy=policy,
                target_soc_percent=target_soc,
                required_headroom_kwh=required_headroom,
            )
        )

    physical_inputs_complete = complete_battery and all(
        point.pv_kwh is not None and point.load_kwh is not None
        for point in inputs.energy_points
    )
    quality = inputs.quality if physical_inputs_complete else "partial"
    blocker_code: str | None = None
    first = _utc(inputs.energy_points[0].start, "invalid_energy_interval")
    last = _utc(inputs.energy_points[-1].end, "invalid_energy_interval")
    requested_start, requested_end = _requested_horizon(inputs)
    if not physical_inputs_complete or quality != "complete":
        quality = "partial"
        blocker_code = "incomplete_physical_inputs"
    elif _coverage_limited_by_window(inputs):
        quality = "partial"
        blocker_code = "risk_window_partially_covered"
    elif first != requested_start or last != requested_end:
        quality = "partial"
        blocker_code = "planning_horizon_limited"
    elif not inputs.result.prediction_ready:
        quality = "degraded"
        blocker_code = inputs.result.prediction_block_reason
    return OptimizerTimelineTrace(
        policy_id="rcm",
        points=tuple(trace_points),
        quality=quality,
        blocker_code=blocker_code,
    )
