"""Neutral, output-only Self-Use energy forecast timeline.

This module deliberately has no Home Assistant imports and no execution hooks.
It turns already-resolved shared EMS inputs into exact backend points for the
Aurora planner.  It is not a policy plan and cannot become an EMS owner.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Any, Mapping, Sequence

BASELINE_TIMELINE_SCHEMA_VERSION = "2.0"
BASELINE_TIMELINE_KIND = "baseline_self_use"
BASELINE_POINT_KEYS = frozenset(
    {
        "start",
        "end",
        "pv_kw",
        "load_kw",
        "battery_kw",
        "grid_import_kw",
        "grid_export_kw",
        "soc_start_percent",
        "soc_end_percent",
        "reserve_soc_percent",
        "quality",
        "blocker_code",
        "source",
        "provenance",
    }
)
BASELINE_PAYLOAD_KEYS = frozenset(
    {
        "schema_version",
        "timeline_kind",
        "output_only",
        "authority",
        "supervisor_candidate",
        "current",
        "generated_at",
        "config_entry_id",
        "shared_inputs_revision",
        "quality",
        "blocker_code",
        "point_count",
        "interval_minutes",
        "battery_sign_convention",
        "current_actual_soc_percent",
        "effective_reserve_soc_percent",
        "effective_maximum_soc_percent",
        "pv_to_battery_efficiency",
        "battery_to_home_efficiency",
        "maximum_charge_power_kw",
        "maximum_discharge_power_kw",
        "system_ac_power_kw",
        "zero_export_confirmed",
        "source",
        "provenance",
        "points",
        "state",
    }
)

_MAX_POINTS = 96
_EPSILON = 1e-9


def _number(
    value: object,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    """Return a finite bounded number without accepting booleans."""

    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not isfinite(parsed):
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _rounded(value: float | None) -> float | None:
    if value is None:
        return None
    rounded = round(value, 4)
    # JSON preserves IEEE negative zero.  Publishing ``-0.0`` for a blocked
    # discharge makes the exact inspector look like a real battery outflow.
    return 0.0 if rounded == 0.0 else rounded


def _bounded_text(value: object, *, limit: int = 256) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def _bounded_mapping(values: Mapping[str, str | None]) -> dict[str, str | None]:
    result: dict[str, str | None] = {}
    for raw_key in sorted(values, key=lambda item: str(item)):
        key = str(raw_key)
        if not key or len(key) > 64:
            continue
        result[key] = _bounded_text(values[raw_key])
        if len(result) == 16:
            break
    return result


@dataclass(frozen=True, slots=True)
class BaselineForecastSlot:
    """One exact backend forecast interval before battery simulation."""

    start: datetime
    end: datetime
    pv_kw: float | None
    load_kw: float | None
    pv_source: str | None = None
    load_source: str | None = None
    pv_provenance: str | None = None
    load_provenance: str | None = None


@dataclass(frozen=True, slots=True)
class BaselineEnergyInputs:
    """Current shared EMS values used by the neutral Self-Use model."""

    generated_at: datetime
    config_entry_id: str
    shared_inputs_revision: int
    current_soc_percent: float | None
    battery_capacity_kwh: float | None
    reserve_soc_percent: float | None
    hardware_minimum_soc_percent: float | None
    hardware_maximum_soc_percent: float | None
    pv_to_battery_efficiency: float | None
    battery_to_home_efficiency: float | None
    maximum_charge_power_kw: float | None
    maximum_discharge_power_kw: float | None
    system_ac_power_kw: float | None
    zero_export_confirmed: bool
    export_allowed: bool | None
    sources: Mapping[str, str | None]
    provenance: Mapping[str, str | None]


def _point_blocker(
    *,
    pv_ready: bool,
    load_ready: bool,
    soc_prerequisites_ready: bool,
    soc_continuity_ready: bool,
    export_unknown_for_surplus: bool,
    hardware_bounds_complete: bool,
) -> str | None:
    if not pv_ready and not load_ready:
        return "pv_and_load_unavailable"
    if not pv_ready:
        return "pv_unavailable"
    if not load_ready:
        return "load_unavailable"
    if not soc_prerequisites_ready:
        return "soc_series_prerequisites_unavailable"
    if not soc_continuity_ready:
        return "soc_series_discontinuous"
    if export_unknown_for_surplus:
        return "zero_export_state_unverified"
    if not hardware_bounds_complete:
        return "hardware_soc_bounds_partial"
    return None


def _validate_slots(slots: Sequence[BaselineForecastSlot]) -> None:
    if len(slots) > _MAX_POINTS:
        raise ValueError(f"baseline timeline is limited to {_MAX_POINTS} points")
    previous_end: datetime | None = None
    for slot in slots:
        if not _aware(slot.start) or not _aware(slot.end):
            raise ValueError("baseline slot timestamps must be timezone-aware")
        if slot.end <= slot.start:
            raise ValueError("baseline slot end must be newer than start")
        if (slot.end - slot.start).total_seconds() > 3600:
            raise ValueError("baseline slot duration exceeds one hour")
        if previous_end is not None and slot.start != previous_end:
            raise ValueError("baseline slots must be contiguous")
        previous_end = slot.end


def build_baseline_energy_timeline(
    inputs: BaselineEnergyInputs,
    slots: Sequence[BaselineForecastSlot],
) -> dict[str, Any]:
    """Build a bounded output-only Self-Use timeline.

    The battery follows ordinary self-consumption only: PV surplus charges the
    battery, household deficit discharges it no lower than the effective
    reserve, and the grid covers what the battery cannot.  Missing data never
    receives a fabricated value.  A missing PV or LOAD point breaks only the
    dependent battery/SOC/grid series; the independent series stays public.
    """

    if not _aware(inputs.generated_at):
        raise ValueError("baseline generated_at must be timezone-aware")
    _validate_slots(slots)

    current_soc = _number(inputs.current_soc_percent, minimum=0.0, maximum=100.0)
    capacity = _number(inputs.battery_capacity_kwh, minimum=0.001, maximum=100_000.0)
    configured_reserve = _number(
        inputs.reserve_soc_percent,
        minimum=0.0,
        maximum=100.0,
    )
    hardware_minimum = _number(
        inputs.hardware_minimum_soc_percent,
        minimum=0.0,
        maximum=100.0,
    )
    hardware_maximum = _number(
        inputs.hardware_maximum_soc_percent,
        minimum=0.0,
        maximum=100.0,
    )
    charge_efficiency = _number(
        inputs.pv_to_battery_efficiency,
        minimum=0.01,
        maximum=1.0,
    )
    discharge_efficiency = _number(
        inputs.battery_to_home_efficiency,
        minimum=0.01,
        maximum=1.0,
    )
    charge_limit = _number(
        inputs.maximum_charge_power_kw,
        minimum=0.0,
        maximum=1_000.0,
    )
    discharge_limit = _number(
        inputs.maximum_discharge_power_kw,
        minimum=0.0,
        maximum=1_000.0,
    )
    system_ac_power = _number(
        inputs.system_ac_power_kw,
        minimum=0.0,
        maximum=1_000.0,
    )

    reserve_candidates = tuple(
        value for value in (configured_reserve, hardware_minimum) if value is not None
    )
    effective_reserve = max(reserve_candidates) if reserve_candidates else None
    # 100% is the mathematical upper boundary of the physical SOC telemetry,
    # not an invented inverter setting.  A fresh hardware maximum narrows it.
    effective_maximum = hardware_maximum if hardware_maximum is not None else 100.0
    hardware_bounds_complete = (
        hardware_minimum is not None and hardware_maximum is not None
    )
    limits_coherent = bool(
        effective_reserve is not None and effective_reserve <= effective_maximum
    )
    soc_prerequisites_ready = all(
        value is not None
        for value in (
            current_soc,
            capacity,
            effective_reserve,
            charge_efficiency,
            discharge_efficiency,
            charge_limit,
            discharge_limit,
            system_ac_power,
        )
    ) and limits_coherent

    soc_cursor = current_soc if soc_prerequisites_ready else None
    points: list[dict[str, Any]] = []
    for slot in slots:
        pv_kw = _number(slot.pv_kw, minimum=0.0, maximum=1_000.0)
        load_kw = _number(slot.load_kw, minimum=0.0, maximum=1_000.0)
        duration_hours = (slot.end - slot.start).total_seconds() / 3600.0
        pv_ready = pv_kw is not None
        load_ready = load_kw is not None
        dependent_inputs_ready = pv_ready and load_ready
        soc_continuity_ready = soc_cursor is not None

        battery_kw: float | None = None
        grid_import_kw: float | None = None
        grid_export_kw: float | None = None
        soc_start: float | None = None
        soc_end: float | None = None
        export_unknown_for_surplus = False

        if dependent_inputs_ready and soc_prerequisites_ready and soc_cursor is not None:
            assert capacity is not None
            assert effective_reserve is not None
            assert charge_efficiency is not None
            assert discharge_efficiency is not None
            assert charge_limit is not None
            assert discharge_limit is not None
            assert system_ac_power is not None

            soc_start = soc_cursor
            pv_energy_kwh = pv_kw * duration_hours
            load_energy_kwh = load_kw * duration_hours
            system_energy_kwh = system_ac_power * duration_hours
            pv_to_load_kwh = min(
                pv_energy_kwh,
                load_energy_kwh,
                system_energy_kwh,
            )
            remaining_pv_kwh = max(pv_energy_kwh - pv_to_load_kwh, 0.0)
            remaining_load_kwh = max(load_energy_kwh - pv_to_load_kwh, 0.0)
            remaining_bridge_kwh = max(
                system_energy_kwh - pv_to_load_kwh,
                0.0,
            )
            if remaining_pv_kwh > _EPSILON:
                headroom_kwh = max(
                    (effective_maximum - soc_cursor) * capacity / 100.0,
                    0.0,
                )
                maximum_charge_input_kwh = min(
                    remaining_pv_kwh,
                    remaining_bridge_kwh,
                    charge_limit * duration_hours / charge_efficiency,
                    headroom_kwh / charge_efficiency,
                )
                stored_kwh = maximum_charge_input_kwh * charge_efficiency
                battery_kw = maximum_charge_input_kwh / duration_hours
                update_ceiling = max(effective_maximum, soc_cursor)
                soc_cursor = min(
                    soc_cursor + stored_kwh / capacity * 100.0,
                    update_ceiling,
                )
                residual_pv_kwh = max(
                    remaining_pv_kwh - maximum_charge_input_kwh,
                    0.0,
                )
                bridge_export_kwh = min(
                    residual_pv_kwh,
                    max(remaining_bridge_kwh - maximum_charge_input_kwh, 0.0),
                )
                grid_import_kw = remaining_load_kwh / duration_hours
                if inputs.zero_export_confirmed:
                    grid_export_kw = 0.0
                elif inputs.export_allowed is True:
                    grid_export_kw = bridge_export_kwh / duration_hours
                else:
                    # Unknown export permission is not silently interpreted as
                    # either export or curtailment.  A fully absorbed surplus,
                    # however, proves an exact zero independently of permission.
                    grid_export_kw = (
                        0.0 if bridge_export_kwh <= _EPSILON else None
                    )
                    export_unknown_for_surplus = bridge_export_kwh > _EPSILON
            elif remaining_load_kwh > _EPSILON:
                usable_stored_kwh = max(
                    (soc_cursor - effective_reserve) * capacity / 100.0,
                    0.0,
                )
                delivered_kwh = min(
                    remaining_load_kwh,
                    remaining_bridge_kwh,
                    discharge_limit * duration_hours * discharge_efficiency,
                    usable_stored_kwh * discharge_efficiency,
                )
                withdrawn_kwh = delivered_kwh / discharge_efficiency
                delivered_kw = delivered_kwh / duration_hours
                battery_kw = -delivered_kw
                soc_cursor = max(
                    soc_cursor - withdrawn_kwh / capacity * 100.0,
                    effective_reserve if soc_cursor >= effective_reserve else soc_cursor,
                )
                grid_import_kw = max(
                    remaining_load_kwh - delivered_kwh,
                    0.0,
                ) / duration_hours
                grid_export_kw = 0.0
            else:
                battery_kw = 0.0
                grid_import_kw = 0.0
                grid_export_kw = 0.0
            soc_end = soc_cursor
        elif not dependent_inputs_ready:
            # Once an interval cannot be simulated, later SOC is unknowable;
            # do not bridge the gap with a held or interpolated value.
            soc_cursor = None

        blocker = _point_blocker(
            pv_ready=pv_ready,
            load_ready=load_ready,
            soc_prerequisites_ready=soc_prerequisites_ready,
            soc_continuity_ready=soc_continuity_ready,
            export_unknown_for_surplus=export_unknown_for_surplus,
            hardware_bounds_complete=hardware_bounds_complete,
        )
        available_series = sum(
            value is not None
            for value in (pv_kw, load_kw, battery_kw, grid_import_kw, grid_export_kw)
        )
        point_quality = (
            "current"
            if blocker is None
            else "partial"
            if available_series
            else "unavailable"
        )
        point_sources = _bounded_mapping(
            {
                "pv": slot.pv_source,
                "load": slot.load_source,
                "shared_inputs": inputs.sources.get("shared_inputs"),
                "soc": inputs.sources.get("soc"),
                "capacity": inputs.sources.get("capacity"),
                "reserve": inputs.sources.get("reserve"),
                "hardware_minimum_soc": inputs.sources.get(
                    "hardware_minimum_soc"
                ),
                "hardware_maximum_soc": inputs.sources.get(
                    "hardware_maximum_soc"
                ),
                "pv_to_battery_efficiency": inputs.sources.get(
                    "pv_to_battery_efficiency"
                ),
                "battery_to_home_efficiency": inputs.sources.get(
                    "battery_to_home_efficiency"
                ),
                "bms_charge_limit": inputs.sources.get("bms_charge_limit"),
                "bms_discharge_limit": inputs.sources.get(
                    "bms_discharge_limit"
                ),
                "system_ac_power": inputs.sources.get("system_ac_power"),
                "gcf_readback": inputs.sources.get("gcf_readback"),
            }
        )
        point_provenance = _bounded_mapping(
            {
                "pv": slot.pv_provenance,
                "load": slot.load_provenance,
                "battery": "backend_self_use_model_v1",
                "shared_inputs_revision": (
                    f"entry_local_snapshot_revision:{inputs.shared_inputs_revision}"
                ),
                "soc": inputs.provenance.get("soc"),
                "capacity": inputs.provenance.get("capacity"),
                "reserve": inputs.provenance.get("reserve"),
                "hardware_minimum_soc": inputs.provenance.get(
                    "hardware_minimum_soc"
                ),
                "hardware_maximum_soc": inputs.provenance.get(
                    "hardware_maximum_soc"
                ),
                "pv_to_battery_efficiency": inputs.provenance.get(
                    "pv_to_battery_efficiency"
                ),
                "battery_to_home_efficiency": inputs.provenance.get(
                    "battery_to_home_efficiency"
                ),
                "bms_charge_limit": inputs.provenance.get("bms_charge_limit"),
                "bms_discharge_limit": inputs.provenance.get(
                    "bms_discharge_limit"
                ),
                "system_ac_power": inputs.provenance.get("system_ac_power"),
                "gcf_readback": inputs.provenance.get("gcf_readback"),
            }
        )
        point = {
            "start": slot.start.isoformat(),
            "end": slot.end.isoformat(),
            "pv_kw": _rounded(pv_kw),
            "load_kw": _rounded(load_kw),
            "battery_kw": _rounded(battery_kw),
            "grid_import_kw": _rounded(grid_import_kw),
            "grid_export_kw": _rounded(grid_export_kw),
            "soc_start_percent": _rounded(soc_start),
            "soc_end_percent": _rounded(soc_end),
            "reserve_soc_percent": _rounded(effective_reserve),
            "quality": point_quality,
            "blocker_code": blocker,
            "source": point_sources,
            "provenance": point_provenance,
        }
        if set(point) != BASELINE_POINT_KEYS:
            raise RuntimeError("baseline point schema drift")
        points.append(point)

    point_qualities = {point["quality"] for point in points}
    any_independent_series = any(
        point["pv_kw"] is not None or point["load_kw"] is not None
        for point in points
    )
    if points and point_qualities == {"current"}:
        state = "current"
        quality = "current"
        blocker_code = None
    elif any_independent_series:
        state = "partial"
        quality = "partial"
        blocker_code = "series_partial"
    else:
        state = "unavailable"
        quality = "unavailable"
        blocker_code = "pv_and_load_unavailable"

    public_sources = _bounded_mapping(inputs.sources)
    public_provenance = _bounded_mapping(inputs.provenance)
    payload = {
        "schema_version": BASELINE_TIMELINE_SCHEMA_VERSION,
        "timeline_kind": BASELINE_TIMELINE_KIND,
        "output_only": True,
        "authority": False,
        "supervisor_candidate": False,
        "current": state != "unavailable",
        "generated_at": inputs.generated_at.isoformat(),
        "config_entry_id": str(inputs.config_entry_id)[:64],
        "shared_inputs_revision": max(int(inputs.shared_inputs_revision), 0),
        "quality": quality,
        "blocker_code": blocker_code,
        "point_count": len(points),
        "interval_minutes": 30,
        "battery_sign_convention": "positive_charge",
        "current_actual_soc_percent": _rounded(current_soc),
        "effective_reserve_soc_percent": _rounded(effective_reserve),
        "effective_maximum_soc_percent": _rounded(effective_maximum),
        "pv_to_battery_efficiency": _rounded(charge_efficiency),
        "battery_to_home_efficiency": _rounded(discharge_efficiency),
        "maximum_charge_power_kw": _rounded(charge_limit),
        "maximum_discharge_power_kw": _rounded(discharge_limit),
        "system_ac_power_kw": _rounded(system_ac_power),
        "zero_export_confirmed": bool(inputs.zero_export_confirmed),
        "source": public_sources,
        "provenance": public_provenance,
        "points": points,
        "state": state,
    }
    if set(payload) != BASELINE_PAYLOAD_KEYS:
        raise RuntimeError("baseline payload schema drift")
    return payload
