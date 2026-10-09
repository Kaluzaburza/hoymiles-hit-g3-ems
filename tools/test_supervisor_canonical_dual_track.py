#!/usr/bin/env python3
"""Focused dual-track contracts for the Supervisor canonical projection.

The authoritative ledger remains the conservative execution trajectory.  The
augmentation adds a P50/expected presentation trajectory without granting any
additional execution authority or changing the selected command.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import copy
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(TOOLS))
sys.path.insert(0, str(COMPONENT))

import automation_plan_timeline as TL  # noqa: E402
import baseline_energy_timeline as BASE  # noqa: E402
from ems_supervisor import ExportState  # noqa: E402
from supervisor_canonical_ledger import (  # noqa: E402
    canonical_execution_ledger_to_dict,
)
import supervisor_canonical_runtime as runtime  # noqa: E402
import test_supervisor_canonical_runtime as fixtures  # noqa: E402


NOW = fixtures.NOW
CAPACITY_KWH = fixtures.CAPACITY
PROTECTED_FLOOR_PERCENT = 50.0
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def _actual(soc_percent: float) -> TL.CurrentActualSnapshot:
    return TL.CurrentActualSnapshot(
        observed_at=NOW,
        pv_kw=0.0,
        load_kw=0.0,
        battery_kw=0.0,
        grid_kw=0.0,
        soc_percent=soc_percent,
        quality="complete",
        source_ages_seconds={
            key: 0.0 for key in ("pv", "load", "battery", "grid", "soc")
        },
    )


def _policy(
    policy_id: str,
    *,
    selected: bool,
    action: str,
    battery_delta_kwh: float,
    grid_import_kwh: float,
    grid_export_kwh: float,
    target_soc_percent: float | None,
    planned_pre_discharge_kw: float | None = None,
    planned_pre_discharge_stored_kwh: float | None = None,
    recommended_charge_power_kw: float | None = None,
    planned_export_limit_percent: float | None = None,
    planned_export_limit_kw: float | None = None,
    action_start_offset_seconds: float | None = None,
    action_end_offset_seconds: float | None = None,
    tariff_stored_energy_kwh: float | None = None,
    tariff_direct_load_kwh: float | None = None,
    tariff_need_class: str = "required_energy",
) -> TL.RCEPolicyPoint | TL.TariffPolicyPoint | TL.RCMPolicyPoint:
    if policy_id == "rce":
        return TL.RCEPolicyPoint(
            sell_price_pln_kwh=0.8,
            planned_export_kwh=grid_export_kwh if selected else 0.0,
            planned_battery_withdrawal_kwh=(
                grid_export_kwh if selected else 0.0
            ),
            target_discharge_kw=(
                grid_export_kwh / 0.5 if selected else 0.0
            ),
            command_discharge_power_percent=40.0,
            target_tolerance_kw=0.05,
            expected_revenue_pln=(
                grid_export_kwh * 0.8 if selected else 0.0
            ),
        )
    if policy_id == "tariff":
        return TL.TariffPolicyPoint(
            buy_price_pln_kwh=0.4,
            tariff_zone="low" if selected else "standard",
            planned_import_kwh=grid_import_kwh if selected else 0.0,
            stored_energy_kwh=(
                tariff_stored_energy_kwh
                if selected and tariff_stored_energy_kwh is not None
                else max(battery_delta_kwh, 0.0)
                if selected
                else 0.0
            ),
            direct_load_kwh=(
                tariff_direct_load_kwh
                if selected and tariff_direct_load_kwh is not None
                else 0.0
            ),
            planned_charge_kw=(
                max(battery_delta_kwh, 0.0) / 0.5 if selected else 0.0
            ),
            expected_cost_pln=(
                grid_import_kwh * 0.4 if selected else 0.0
            ),
            expected_saving_pln=None,
            need_class=tariff_need_class if selected else "none",
        )
    return TL.RCMPolicyPoint(
        voltage_risk_code="no_risk",
        planned_export_limit_percent=planned_export_limit_percent,
        planned_export_limit_kw=planned_export_limit_kw,
        recommended_charge_power_kw=recommended_charge_power_kw,
        planned_pre_discharge_kw=(
            planned_pre_discharge_kw if selected else None
        ),
        planned_pre_discharge_kwh=(
            grid_export_kwh if selected and action == "grid_discharge_preparation" else None
        ),
        planned_pre_discharge_stored_kwh=(
            planned_pre_discharge_stored_kwh
            if selected and action == "grid_discharge_preparation"
            else None
        ),
        action_start_offset_seconds=(
            action_start_offset_seconds
            if action_start_offset_seconds is not None
            else 0.0
            if selected and action == "grid_discharge_preparation"
            else None
        ),
        action_end_offset_seconds=(
            action_end_offset_seconds
            if action_end_offset_seconds is not None
            else 900.0
            if selected and action == "grid_discharge_preparation"
            else None
        ),
        headroom_shortfall_kwh=None,
        control_mode=action,
    )


def _point(
    policy_id: str,
    *,
    index: int,
    minutes: int,
    battery_delta_kwh: float,
    pv_kwh: float,
    load_kwh: float,
    grid_import_kwh: float,
    grid_export_kwh: float,
    soc_percent: float,
    baseline_soc_percent: float,
    selected: bool = False,
    action: str = "idle",
    target_soc_percent: float | None = None,
    planned_pre_discharge_kw: float | None = None,
    planned_pre_discharge_stored_kwh: float | None = None,
    recommended_charge_power_kw: float | None = None,
    planned_export_limit_percent: float | None = None,
    planned_export_limit_kw: float | None = None,
    action_start_offset_seconds: float | None = None,
    action_end_offset_seconds: float | None = None,
    tariff_stored_energy_kwh: float | None = None,
    tariff_direct_load_kwh: float | None = None,
    tariff_need_class: str = "required_energy",
) -> TL.TimelineTracePoint:
    start = NOW + timedelta(minutes=minutes * index)
    return TL.TimelineTracePoint(
        start=start,
        end=start + timedelta(minutes=minutes),
        pv_kwh=pv_kwh,
        load_kwh=load_kwh,
        battery_delta_kwh=battery_delta_kwh,
        grid_import_kwh=grid_import_kwh,
        grid_export_kwh=grid_export_kwh,
        soc_percent=soc_percent,
        baseline_soc_percent=baseline_soc_percent,
        protected_soc_floor_percent=PROTECTED_FLOOR_PERCENT,
        action_code=action,
        selected=selected,
        quality="complete",
        policy=_policy(
            policy_id,
            selected=selected,
            action=action,
            battery_delta_kwh=battery_delta_kwh,
            grid_import_kwh=grid_import_kwh,
            grid_export_kwh=grid_export_kwh,
            target_soc_percent=target_soc_percent,
            planned_pre_discharge_kw=planned_pre_discharge_kw,
            planned_pre_discharge_stored_kwh=(
                planned_pre_discharge_stored_kwh
            ),
            recommended_charge_power_kw=recommended_charge_power_kw,
            planned_export_limit_percent=planned_export_limit_percent,
            planned_export_limit_kw=planned_export_limit_kw,
            action_start_offset_seconds=action_start_offset_seconds,
            action_end_offset_seconds=action_end_offset_seconds,
            tariff_stored_energy_kwh=tariff_stored_energy_kwh,
            tariff_direct_load_kwh=tariff_direct_load_kwh,
            tariff_need_class=tariff_need_class,
        ),
        target_soc_percent=(
            target_soc_percent if policy_id in {"tariff", "rcm"} else None
        ),
    )


def _payload(
    policy_id: str,
    points: list[TL.TimelineTracePoint],
    *,
    actual_soc_percent: float,
) -> dict[str, object]:
    return TL.build_current_payload(
        TL.OptimizerTimelineTrace(policy_id, tuple(points)),
        config_entry_id="dual-track-entry",
        generated_at=NOW,
        input_revision=41,
        plan_revision=17,
        plan_entity_id=f"sensor.dual_track_{policy_id}",
        current_actual=_actual(actual_soc_percent),
        sources=[
            {
                "role": "plan",
                "entity_id": f"sensor.dual_track_{policy_id}",
            }
        ],
        physical_active=False,
    )


def _expected_timeline(
    samples: list[tuple[float | None, float | None]],
    *,
    initial_soc_percent: float,
    zero_export_confirmed: bool = False,
    hardware_maximum_soc_percent: float | None = 100.0,
    capacity_kwh: float = CAPACITY_KWH,
    charge_efficiency: float = 1.0,
    discharge_efficiency: float = 1.0,
    maximum_charge_power_kw: float = 100.0,
    maximum_discharge_power_kw: float = 100.0,
    system_ac_power_kw: float = 100.0,
) -> dict[str, Any]:
    """Build the real output-only provider-P50 payload used by the adapter."""

    slots = [
        BASE.BaselineForecastSlot(
            start=NOW + timedelta(minutes=30 * index),
            end=NOW + timedelta(minutes=30 * (index + 1)),
            pv_kw=pv_kw,
            load_kw=load_kw,
            pv_source="sensor.provider_p50",
            load_source="sensor.load_profile",
            pv_provenance="detailed_forecast_p50",
            load_provenance="average_profile_30m_kwh",
        )
        for index, (pv_kw, load_kw) in enumerate(samples)
    ]
    return BASE.build_baseline_energy_timeline(
        BASE.BaselineEnergyInputs(
            generated_at=NOW,
            config_entry_id="dual-track-entry",
            shared_inputs_revision=43,
            current_soc_percent=initial_soc_percent,
            battery_capacity_kwh=capacity_kwh,
            reserve_soc_percent=PROTECTED_FLOOR_PERCENT,
            hardware_minimum_soc_percent=PROTECTED_FLOOR_PERCENT,
            hardware_maximum_soc_percent=hardware_maximum_soc_percent,
            pv_to_battery_efficiency=charge_efficiency,
            battery_to_home_efficiency=discharge_efficiency,
            maximum_charge_power_kw=maximum_charge_power_kw,
            maximum_discharge_power_kw=maximum_discharge_power_kw,
            system_ac_power_kw=system_ac_power_kw,
            zero_export_confirmed=zero_export_confirmed,
            export_allowed=not zero_export_confirmed,
            sources={},
            provenance={},
        ),
        slots,
    )


def _expected_48h() -> dict[str, Any]:
    samples = [(0.0, 0.0) for _ in range(96)]
    # The raw provider forecast stores 3 kWh, then the load consumes 1 kWh.
    # This intentionally differs from every policy-local baseline fixture.
    samples[1] = (6.0, 0.0)
    samples[2] = (0.0, 2.0)
    return _expected_timeline(samples, initial_soc_percent=60.0)


def _expected_full_battery_zero_export() -> dict[str, Any]:
    samples = [(0.0, 0.0) for _ in range(96)]
    samples[0] = (2.1, 0.0)
    return _expected_timeline(
        samples,
        initial_soc_percent=100.0,
        zero_export_confirmed=True,
    )


def _expected_morning_rcem() -> dict[str, Any]:
    samples = [(0.0, 0.0) for _ in range(96)]
    samples[0] = (1.0, 0.0)
    samples[1] = (1.0, 0.0)
    return _expected_timeline(samples, initial_soc_percent=80.0)


def _site_samples(
    *,
    pv_total_kwh: float,
    load_total_kwh: float,
) -> list[tuple[float | None, float | None]]:
    """48-hour load with the PV production concentrated in tomorrow's day."""

    load_kw = load_total_kwh / 48.0
    samples: list[tuple[float | None, float | None]] = [
        (0.0, load_kw) for _ in range(96)
    ]
    daylight = range(40, 65)
    pv_kw = pv_total_kwh / (len(daylight) * 0.5)
    for index in daylight:
        samples[index] = (pv_kw, load_kw)
    return samples


def _large_site_timelines(
    *,
    capacity_kwh: float,
    initial_soc_percent: float = 53.0,
) -> dict[str, dict[str, object]]:
    """Policy-local 73.75/40.9 fallback that reproduces the old low line."""

    conservative = _expected_timeline(
        _site_samples(pv_total_kwh=73.75, load_total_kwh=40.9),
        initial_soc_percent=initial_soc_percent,
        capacity_kwh=capacity_kwh,
        charge_efficiency=0.90,
        discharge_efficiency=0.95,
    )

    def policy_points(policy_id: str) -> list[TL.TimelineTracePoint]:
        result: list[TL.TimelineTracePoint] = []
        for index, point in enumerate(conservative["points"]):
            stored_delta = capacity_kwh * (
                point["soc_end_percent"] - point["soc_start_percent"]
            ) / 100.0
            result.append(
                _point(
                    policy_id,
                    index=index,
                    minutes=30,
                    battery_delta_kwh=stored_delta,
                    pv_kwh=point["pv_kw"] * 0.5,
                    load_kwh=point["load_kw"] * 0.5,
                    grid_import_kwh=point["grid_import_kw"] * 0.5,
                    grid_export_kwh=point["grid_export_kw"] * 0.5,
                    soc_percent=point["soc_end_percent"],
                    baseline_soc_percent=point["soc_end_percent"],
                )
            )
        return result

    rcm = [
        _point(
            "rcm",
            index=index,
            minutes=15,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=initial_soc_percent,
            baseline_soc_percent=initial_soc_percent,
        )
        for index in range(192)
    ]
    return {
        "rce": _payload(
            "rce",
            policy_points("rce"),
            actual_soc_percent=initial_soc_percent,
        ),
        "tariff": _payload(
            "tariff",
            policy_points("tariff"),
            actual_soc_percent=initial_soc_percent,
        ),
        "rcm": _payload(
            "rcm",
            rcm,
            actual_soc_percent=initial_soc_percent,
        ),
    }


def _frame(
    *,
    soc_percent: float,
    export_state: ExportState = ExportState.VERIFIED_ALLOWED,
) -> Any:
    result = fixtures.frame(export_state)
    result.context = replace(
        result.context,
        battery_soc_percent=soc_percent,
        physical_protected_soc_floor_percent=PROTECTED_FLOOR_PERCENT,
    )
    result.execution = replace(
        result.execution,
        battery_soc_percent=soc_percent,
        battery_soc_observed_at=NOW - timedelta(seconds=1),
        effective_export_limit_percent=(
            0.0
            if export_state is ExportState.CONFIRMED_ZERO_EXPORT
            else 100.0
        ),
    )
    return result


def _sequence_48h() -> dict[str, dict[str, object]]:
    """Tariff charge -> P50 PV -> home load -> bounded RCE discharge."""

    tariff: list[TL.TimelineTracePoint] = []
    authorization_soc = 60.0
    tariff_baseline_soc = 60.0
    for index in range(96):
        selected = index == 0
        action = "battery_charge" if selected else "idle"
        if index == 0:
            delta, pv, load, imported = 1.0, 0.0, 0.0, 1.05
        elif index == 1:
            # Conservative/authorization PV stores only 1.0 kWh.
            delta, pv, load, imported = 1.0, 1.05, 0.0, 0.0
        elif index == 2:
            delta, pv, load, imported = -1.0, 0.0, 0.95, 0.0
        else:
            delta, pv, load, imported = 0.0, 0.0, 0.0, 0.0
        authorization_soc += delta * 100.0 / CAPACITY_KWH
        if index == 1:
            tariff_baseline_soc += 10.0
        elif index == 2:
            tariff_baseline_soc -= 10.0
        tariff.append(
            _point(
                "tariff",
                index=index,
                minutes=30,
                battery_delta_kwh=delta,
                pv_kwh=pv,
                load_kwh=load,
                grid_import_kwh=imported,
                grid_export_kwh=0.0,
                soc_percent=authorization_soc,
                # Baseline excludes the tariff action itself, so the P50
                # sidecar must replay the selected +1 kWh command.
                baseline_soc_percent=tariff_baseline_soc,
                selected=selected,
                action=action,
                target_soc_percent=70.0 if selected else None,
            )
        )

    rce: list[TL.TimelineTracePoint] = []
    action_soc = 60.0
    expected_baseline_soc = 60.0
    for index in range(96):
        # P50 self-use backbone: PV adds 3 kWh, then the home consumes 1 kWh.
        if index == 1:
            expected_baseline_soc += 30.0
        elif index == 2:
            expected_baseline_soc -= 10.0
        selected = 3 <= index <= 6
        delta = -1.0 if selected else 0.0
        action_soc += delta * 100.0 / CAPACITY_KWH
        rce.append(
            _point(
                "rce",
                index=index,
                minutes=30,
                battery_delta_kwh=delta,
                pv_kwh=0.0,
                load_kwh=0.0,
                grid_import_kwh=0.0,
                grid_export_kwh=0.95 if selected else 0.0,
                soc_percent=action_soc,
                baseline_soc_percent=expected_baseline_soc,
                selected=selected,
                action="export" if selected else "idle",
            )
        )

    rcm = [
        _point(
            "rcm",
            index=index,
            minutes=15,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=60.0,
            baseline_soc_percent=60.0,
        )
        for index in range(192)
    ]
    return {
        "rce": _payload("rce", rce, actual_soc_percent=60.0),
        "tariff": _payload("tariff", tariff, actual_soc_percent=60.0),
        "rcm": _payload("rcm", rcm, actual_soc_percent=60.0),
    }


def _full_battery_zero_export() -> dict[str, dict[str, object]]:
    """A fresh plan still asks for PV charge after the physical SOC reached 100%."""

    tariff = [
        _point(
            "tariff",
            index=index,
            minutes=30,
            battery_delta_kwh=1.0 if index == 0 else 0.0,
            pv_kwh=1.05 if index == 0 else 0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=100.0,
            baseline_soc_percent=100.0,
        )
        for index in range(2)
    ]
    rce = [
        _point(
            "rce",
            index=index,
            minutes=30,
            battery_delta_kwh=1.0 if index == 0 else 0.0,
            pv_kwh=1.05 if index == 0 else 0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=100.0,
            baseline_soc_percent=100.0,
        )
        for index in range(2)
    ]
    rcm_soc = 90.0
    rcm: list[TL.TimelineTracePoint] = []
    for index in range(4):
        delta = 0.5 if index < 2 else 0.0
        rcm_soc += delta * 100.0 / CAPACITY_KWH
        rcm.append(
            _point(
                "rcm",
                index=index,
                minutes=15,
                battery_delta_kwh=delta,
                pv_kwh=delta,
                load_kwh=0.0,
                grid_import_kwh=0.0,
                grid_export_kwh=0.0,
                soc_percent=rcm_soc,
                baseline_soc_percent=rcm_soc,
            )
        )
    return {
        "rce": _payload("rce", rce, actual_soc_percent=90.0),
        "tariff": _payload("tariff", tariff, actual_soc_percent=90.0),
        "rcm": _payload("rcm", rcm, actual_soc_percent=90.0),
    }


def _flat_one_hour_timelines(
    *,
    soc_percent: float,
    rce: list[TL.TimelineTracePoint] | None = None,
) -> dict[str, dict[str, object]]:
    """Build a neutral one-hour authorization backbone for focused replay."""

    if rce is None:
        rce = [
            _point(
                "rce",
                index=index,
                minutes=30,
                battery_delta_kwh=0.0,
                pv_kwh=0.0,
                load_kwh=0.0,
                grid_import_kwh=0.0,
                grid_export_kwh=0.0,
                soc_percent=soc_percent,
                baseline_soc_percent=soc_percent,
            )
            for index in range(2)
        ]
    tariff = [
        _point(
            "tariff",
            index=index,
            minutes=30,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=soc_percent,
            baseline_soc_percent=soc_percent,
        )
        for index in range(2)
    ]
    rcm = [
        _point(
            "rcm",
            index=index,
            minutes=15,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=soc_percent,
            baseline_soc_percent=soc_percent,
        )
        for index in range(4)
    ]
    return {
        "rce": _payload("rce", rce, actual_soc_percent=soc_percent),
        "tariff": _payload("tariff", tariff, actual_soc_percent=soc_percent),
        "rcm": _payload("rcm", rcm, actual_soc_percent=soc_percent),
    }


def _morning_rcem_pre_discharge(
    *,
    authorization_entry_soc: float = 70.0,
    expected_entry_soc: float = 90.0,
) -> dict[str, dict[str, object]]:
    """Two source paths replay one future RCEm discharge down to its target."""

    # The conservative authorization backbone reaches the morning action at
    # 70%, while the expected/P50 backbone reaches it at 90%.  RCEm's own
    # native trace was calculated from 80% and requests four 15-minute steps
    # down to 60%.  A correct state-dependent replay therefore stops the
    # authorization path at 60%, while the expected path ends at 70% because
    # the one-hour action window expires first.
    tariff: list[TL.TimelineTracePoint] = []
    tariff_soc = 80.0
    tariff_step = (authorization_entry_soc - tariff_soc) / 2.0
    for index in range(96):
        if index < 2:
            tariff_soc += tariff_step
            stored_delta = tariff_step * CAPACITY_KWH / 100.0
            pv = max(stored_delta, 0.0)
            load = max(-stored_delta, 0.0)
        else:
            stored_delta = 0.0
            pv = 0.0
            load = 0.0
        tariff.append(
            _point(
                "tariff",
                index=index,
                minutes=30,
                battery_delta_kwh=stored_delta,
                pv_kwh=pv,
                load_kwh=load,
                grid_import_kwh=0.0,
                grid_export_kwh=0.0,
                soc_percent=tariff_soc,
                baseline_soc_percent=tariff_soc,
            )
        )

    rce: list[TL.TimelineTracePoint] = []
    p50_soc = 80.0
    p50_step = (expected_entry_soc - p50_soc) / 2.0
    for index in range(96):
        if index < 2:
            p50_soc += p50_step
            stored_delta = p50_step * CAPACITY_KWH / 100.0
            pv = max(stored_delta, 0.0)
            load = max(-stored_delta, 0.0)
        else:
            stored_delta = 0.0
            pv = 0.0
            load = 0.0
        rce.append(
            _point(
                "rce",
                index=index,
                minutes=30,
                battery_delta_kwh=stored_delta,
                pv_kwh=pv,
                load_kwh=load,
                grid_import_kwh=0.0,
                grid_export_kwh=0.0,
                soc_percent=p50_soc,
                baseline_soc_percent=p50_soc,
            )
        )

    rcm: list[TL.TimelineTracePoint] = []
    rcm_soc = 80.0
    for index in range(192):
        selected = 4 <= index <= 7
        if selected:
            rcm_soc -= 5.0
            # Timeline battery power uses the AC-side output while SOC tracks
            # the 0.5 kWh removed from storage (95% discharge efficiency).
            stored_delta = -0.475
            exported = 0.475
        else:
            stored_delta = 0.0
            exported = 0.0
        rcm.append(
            _point(
                "rcm",
                index=index,
                minutes=15,
                battery_delta_kwh=stored_delta,
                pv_kwh=0.0,
                load_kwh=0.0,
                grid_import_kwh=0.0,
                grid_export_kwh=exported,
                soc_percent=rcm_soc,
                baseline_soc_percent=80.0,
                selected=selected,
                action="grid_discharge_preparation" if selected else "idle",
                target_soc_percent=60.0 if selected else None,
                planned_pre_discharge_kw=1.9 if selected else None,
                planned_pre_discharge_stored_kwh=(0.5 if selected else None),
            )
        )
    return {
        "rce": _payload("rce", rce, actual_soc_percent=80.0),
        "tariff": _payload("tariff", tariff, actual_soc_percent=80.0),
        "rcm": _payload("rcm", rcm, actual_soc_percent=80.0),
    }


def _flat_timelines_with_rcm_action(
    *,
    soc_percent: float,
    action: str,
    selected_soc_percent: float,
    baseline_soc_percent: float,
    recommended_charge_power_kw: float | None = None,
    planned_export_limit_percent: float | None = None,
    planned_export_limit_kw: float | None = None,
) -> dict[str, dict[str, object]]:
    """Build one exact RCEm action point over otherwise neutral timelines."""

    half_hour = {
        policy_id: [
            _point(
                policy_id,
                index=index,
                minutes=30,
                battery_delta_kwh=0.0,
                pv_kwh=0.0,
                load_kwh=0.0,
                grid_import_kwh=0.0,
                grid_export_kwh=0.0,
                soc_percent=soc_percent,
                baseline_soc_percent=soc_percent,
            )
            for index in range(2)
        ]
        for policy_id in ("rce", "tariff")
    }
    rcm = [
        _point(
            "rcm",
            index=index,
            minutes=15,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=(selected_soc_percent if index == 0 else soc_percent),
            baseline_soc_percent=(
                baseline_soc_percent if index == 0 else baseline_soc_percent
            ),
            selected=index == 0,
            action=action if index == 0 else "idle",
            recommended_charge_power_kw=(
                recommended_charge_power_kw if index == 0 else None
            ),
            planned_export_limit_percent=(
                planned_export_limit_percent if index == 0 else None
            ),
            planned_export_limit_kw=(
                planned_export_limit_kw if index == 0 else None
            ),
            action_start_offset_seconds=0.0 if index == 0 else None,
            action_end_offset_seconds=900.0 if index == 0 else None,
        )
        for index in range(4)
    ]
    return {
        "rce": _payload(
            "rce", half_hour["rce"], actual_soc_percent=soc_percent
        ),
        "tariff": _payload(
            "tariff", half_hour["tariff"], actual_soc_percent=soc_percent
        ),
        "rcm": _payload("rcm", rcm, actual_soc_percent=soc_percent),
    }


def _augmented(
    test_frame: Any,
    timelines: dict[str, dict[str, object]],
    expected_timeline: dict[str, Any] | None,
    *,
    capacity_kwh: float = CAPACITY_KWH,
) -> dict[str, Any]:
    augment = getattr(runtime, "augment_canonical_projection_payload", None)
    check(
        callable(augment),
        "missing augment_canonical_projection_payload(payload, *, frame, "
        "timelines, expected_timeline, usable_capacity_kwh)",
    )
    ledger = runtime.build_supervisor_canonical_ledger(
        frame=test_frame,
        timelines=timelines,
        usable_capacity_kwh=capacity_kwh,
    )
    payload = canonical_execution_ledger_to_dict(ledger)
    result = augment(
        payload,
        frame=test_frame,
        timelines=timelines,
        expected_timeline=expected_timeline,
        usable_capacity_kwh=capacity_kwh,
    )
    check(isinstance(result, dict), "dual-track augmentation must return a payload")
    return result


def _soc_equation(slot: dict[str, Any]) -> dict[str, float]:
    equation = slot.get("soc_equation")
    check(isinstance(equation, dict), "slot has no soc_equation object")
    required = {
        "soc_start_percent",
        "soc_end_percent",
        "expected_soc_start_percent",
        "expected_soc_end_percent",
        "authorization_soc_start_percent",
        "authorization_soc_end_percent",
    }
    check(required <= set(equation), "dual-track SOC fields are incomplete")
    check(
        equation["soc_start_percent"]
        == equation["authorization_soc_start_percent"],
        "legacy soc_start_percent is not the authorization alias",
    )
    check(
        equation["soc_end_percent"]
        == equation["authorization_soc_end_percent"],
        "legacy soc_end_percent is not the authorization alias",
    )
    return equation


def test_48h_dual_track_replays_one_command_against_two_soc_states() -> None:
    timelines = _sequence_48h()
    result = _augmented(
        _frame(soc_percent=60.0),
        timelines,
        _expected_48h(),
    )
    slots = result["slots"]
    check(len(slots) == 192, "48 h at the common 15-minute grid must have 192 slots")

    equations = [_soc_equation(slot) for slot in slots]
    for previous, current in zip(equations, equations[1:]):
        check(
            previous["authorization_soc_end_percent"]
            == current["authorization_soc_start_percent"],
            "authorization SOC chain is discontinuous",
        )
        check(
            previous["expected_soc_end_percent"]
            == current["expected_soc_start_percent"],
            "expected SOC chain is discontinuous",
        )

    tariff_indices = [
        index
        for index, slot in enumerate(slots)
        if slot["selected_action"] == "tariff_battery_charge"
    ]
    rce_indices = [
        index
        for index, slot in enumerate(slots)
        if slot["selected_action"] == "rce_export"
    ]
    check(tariff_indices, "48 h sequence lost the tariff charge")
    check(rce_indices, "48 h sequence lost the RCE discharge window")
    check(max(tariff_indices) < min(rce_indices), "tariff must precede RCE")
    check(
        equations[tariff_indices[-1]]["expected_soc_end_percent"]
        > equations[tariff_indices[0]]["expected_soc_start_percent"],
        "tariff action did not increase expected SOC",
    )

    first_rce = equations[rce_indices[0]]
    check(
        first_rce["expected_source"] == "provider_p50",
        "expected path did not use the neutral provider baseline",
    )
    check(
        first_rce["expected_soc_start_percent"]
        > first_rce["authorization_soc_start_percent"],
        "P50 backbone did not enter RCE with more expected energy",
    )
    expected_drop = sum(
        max(
            equation["expected_soc_start_percent"]
            - equation["expected_soc_end_percent"],
            0.0,
        )
        for equation in (equations[index] for index in rce_indices)
    )
    authorization_drop = sum(
        max(
            equation["authorization_soc_start_percent"]
            - equation["authorization_soc_end_percent"],
            0.0,
        )
        for equation in (equations[index] for index in rce_indices)
    )
    check(
        expected_drop > 0.0 and authorization_drop > 0.0,
        "both trajectories must replay the selected discharge",
    )
    check(
        all(equations[index]["authorization_soc_start_percent"] > PROTECTED_FLOOR_PERCENT
            for index in rce_indices),
        "future RCE must stop selection when the canonical reserve is reached",
    )
    for index in rce_indices:
        equation = equations[index]
        check(
            equation["expected_soc_end_percent"] >= PROTECTED_FLOOR_PERCENT,
            "expected RCE replay crossed the protected floor",
        )
        check(
            equation["authorization_soc_end_percent"]
            >= PROTECTED_FLOOR_PERCENT,
            "authorization RCE replay crossed the protected floor",
        )
        command = slots[index]["command_expectation"]["values"]
        check(isinstance(command, dict), "RCE command values are not an object")
        check(command["mode_code"] == 5, "RCE mode command drifted")
        check(
            command["maximum_discharge_power"] == 40.0,
            "RCE replay lost its per-slot power limit",
        )
        check(
            command["force_discharge_soc"] == PROTECTED_FLOOR_PERCENT,
            "RCE replay lost its protected target SOC",
        )

    for slot in slots:
        planned = slot.get("planned")
        check(isinstance(planned, dict), "slot has no planned energy object")
        required = {
            "potential_pv_kwh",
            "usable_pv_kwh",
            "pv_curtailed_kwh",
            "expected_grid_export_kwh",
            "authorization_grid_export_kwh",
        }
        check(required <= set(planned), "dual-track planned fields are incomplete")
        check(
            planned["potential_pv_kwh"] + 1e-9
            >= planned["usable_pv_kwh"] >= -1e-9,
            "usable PV exceeds potential PV",
        )
        check(planned["pv_curtailed_kwh"] >= -1e-9, "negative PV curtailment")

    expected_export = sum(
        slot["planned"]["expected_grid_export_kwh"] for slot in slots
    )
    authorization_export = sum(
        slot["planned"]["authorization_grid_export_kwh"] for slot in slots
    )
    check(
        abs(expected_export - 1.9) < 1e-9 and abs(authorization_export - 1.9) < 1e-9,
        "both tracks must replay the four selected intervals, without extra P50-only sales",
    )


def test_full_battery_zero_export_is_flat_and_reports_curtailment() -> None:
    timelines = _full_battery_zero_export()
    result = _augmented(
        _frame(
            soc_percent=100.0,
            export_state=ExportState.CONFIRMED_ZERO_EXPORT,
        ),
        timelines,
        _expected_full_battery_zero_export(),
    )
    slots = result["slots"]
    curtailed = 0.0
    potential = 0.0
    for slot in slots:
        equation = _soc_equation(slot)
        for field in (
            "expected_soc_start_percent",
            "expected_soc_end_percent",
            "authorization_soc_start_percent",
            "authorization_soc_end_percent",
        ):
            check(equation[field] == 100.0, "full-battery SOC must remain flat")
        planned = slot["planned"]
        check(
            abs(planned["expected_grid_export_kwh"]) <= 1e-9,
            "zero export leaked expected energy to grid",
        )
        check(
            abs(planned["authorization_grid_export_kwh"]) <= 1e-9,
            "zero export leaked authorized energy to grid",
        )
        curtailed += planned["pv_curtailed_kwh"]
        potential += planned["potential_pv_kwh"]
    check(potential > 0.0, "zero-export fixture has no PV potential")
    check(curtailed > 0.0, "full battery did not report curtailed PV")


def test_tariff_grid_support_only_offsets_its_direct_load_share() -> None:
    """One supported kWh cannot cancel a five-kWh household deficit."""

    tariff = [
        _point(
            "tariff",
            index=0,
            minutes=30,
            battery_delta_kwh=-4.0,
            pv_kwh=0.0,
            load_kwh=5.0,
            grid_import_kwh=1.0,
            grid_export_kwh=0.0,
            soc_percent=60.0,
            baseline_soc_percent=50.0,
            selected=True,
            action="grid_support",
            target_soc_percent=60.0,
            tariff_stored_energy_kwh=0.0,
            tariff_direct_load_kwh=1.0,
            tariff_need_class="economic",
        ),
        _point(
            "tariff",
            index=1,
            minutes=30,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=60.0,
            baseline_soc_percent=50.0,
        ),
    ]
    rce = [
        _point(
            "rce",
            index=index,
            minutes=30,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=100.0,
            baseline_soc_percent=100.0,
        )
        for index in range(2)
    ]
    rcm = [
        _point(
            "rcm",
            index=index,
            minutes=15,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=100.0,
            baseline_soc_percent=100.0,
        )
        for index in range(4)
    ]
    timelines = {
        "rce": _payload("rce", rce, actual_soc_percent=100.0),
        "tariff": _payload("tariff", tariff, actual_soc_percent=100.0),
        "rcm": _payload("rcm", rcm, actual_soc_percent=100.0),
    }
    result = _augmented(
        _frame(soc_percent=100.0),
        timelines,
        _expected_timeline(
            [(0.0, 10.0), (0.0, 0.0)],
            initial_soc_percent=100.0,
            maximum_discharge_power_kw=10.0,
        ),
    )
    first_half_hour = result["slots"][:2]
    check(
        all(
            slot["selected_action"] == "tariff_grid_support"
            for slot in first_half_hour
        ),
        "partial canonical slots lost the tariff support action: "
        f"{[slot['selected_action'] for slot in first_half_hour]}",
    )
    check(
        _soc_equation(first_half_hour[-1])["expected_soc_end_percent"] == 60.0,
        "one supported kWh incorrectly cancelled the full five-kWh load",
    )
    floor_limited = _augmented(
        _frame(soc_percent=100.0),
        timelines,
        _expected_timeline(
            [(0.0, 10.0), (0.0, 0.0)],
            initial_soc_percent=100.0,
            discharge_efficiency=0.8,
            maximum_discharge_power_kw=10.0,
        ),
    )
    check(
        _soc_equation(floor_limited["slots"][1])["expected_soc_end_percent"]
        == 50.0,
        "support manufactured SOC after an efficiency/floor-limited discharge",
    )
    power_limited = _augmented(
        _frame(soc_percent=100.0),
        timelines,
        _expected_timeline(
            [(0.0, 10.0), (0.0, 0.0)],
            initial_soc_percent=100.0,
            maximum_discharge_power_kw=2.0,
        ),
    )
    check(
        _soc_equation(power_limited["slots"][1])["expected_soc_end_percent"]
        == 90.0,
        "support bypassed the provider battery-discharge power limit",
    )


def test_morning_rcem_pre_discharge_replays_against_both_soc_states() -> None:
    result = _augmented(
        _frame(soc_percent=80.0),
        _morning_rcem_pre_discharge(),
        _expected_morning_rcem(),
    )
    slots = result["slots"]
    rcm_indices = [
        index
        for index, slot in enumerate(slots)
        if slot["selected_action"] == "rcm_pre_discharge"
    ]
    check(rcm_indices == [4, 5], "future RCEm must stop selection at its projected target")

    equations = [_soc_equation(slot) for slot in slots]
    for previous, current in zip(equations, equations[1:]):
        check(
            previous["authorization_soc_end_percent"]
            == current["authorization_soc_start_percent"],
            "authorization SOC jumped when RCEm became active",
        )
        check(
            previous["expected_soc_end_percent"]
            == current["expected_soc_start_percent"],
            "expected SOC jumped to the independent RCEm timeline",
        )

    first = equations[rcm_indices[0]]
    check(
        first["authorization_soc_start_percent"] == 70.0,
        "authorization path did not preserve its conservative backbone",
    )
    check(
        first["expected_soc_start_percent"] == 90.0,
        "expected path did not preserve its P50 backbone",
    )
    check(
        equations[rcm_indices[-1]]["authorization_soc_end_percent"] == 60.0,
        "RCEm authorization replay crossed its commanded 60% target",
    )
    check(
        equations[rcm_indices[-1]]["expected_soc_end_percent"] == 80.0,
        "expected RCEm replay must use only the intervals selected by the conservative path",
    )
    for index in rcm_indices:
        command = slots[index]["command_expectation"]["values"]
        check(command["mode_code"] == 5, "RCEm lost Grid Discharge mode")
        check(command["force_discharge_soc"] == 60.0, "RCEm target SOC drifted")
        check(
            command["maximum_discharge_power"] == 20.0,
            "RCEm command lost its configured discharge-power limit",
        )


def test_rcem_below_target_neither_creates_energy_nor_discharges_further() -> None:
    result = _augmented(
        _frame(soc_percent=80.0),
        _morning_rcem_pre_discharge(
            authorization_entry_soc=55.0,
            expected_entry_soc=55.0,
        ),
        _expected_timeline(
            [(0.0, 2.5), (0.0, 2.5)]
            + [(0.0, 0.0) for _ in range(94)],
            initial_soc_percent=80.0,
        ),
    )
    first = result["slots"][4]
    check(first["selected_action"] == "none", "below-target future RCEm must remain unselected")
    equation = _soc_equation(first)
    for field in (
        "authorization_soc_start_percent",
        "authorization_soc_end_percent",
        "expected_soc_start_percent",
        "expected_soc_end_percent",
    ):
        check(
            abs(equation[field] - 55.0) <= 1e-9,
            "RCEm target above current SOC must neither create energy nor discharge",
        )
    planned = first["planned"]
    check(
        abs(planned["battery_kwh"]) <= 1e-9,
        "below-target RCEm replay retained impossible battery discharge",
    )
    check(
        abs(planned["grid_kwh_import_positive"]) <= 1e-9,
        "below-target RCEm replay retained impossible grid export",
    )


def test_rcem_absorb_replays_raw_cursor_without_negative_derivative() -> None:
    """An independent saturated baseline cannot make absorb-PV lower cyan SOC."""

    result = _augmented(
        _frame(soc_percent=70.0),
        _flat_timelines_with_rcm_action(
            soc_percent=70.0,
            action="absorb_pv",
            selected_soc_percent=70.0,
            baseline_soc_percent=80.0,
            recommended_charge_power_kw=2.0,
        ),
        _expected_timeline(
            [(0.0, 0.0) for _ in range(2)],
            initial_soc_percent=70.0,
        ),
    )
    first = result["slots"][0]
    check(first["selected_action"] == "rcm_absorb_pv", "absorb-PV action lost")
    equation = _soc_equation(first)
    check(
        equation["expected_soc_start_percent"] == 70.0
        and equation["expected_soc_end_percent"] == 70.0,
        "absorb-PV replay applied an independent negative SOC derivative",
    )


def test_rcem_zero_export_limit_curtails_provider_surplus() -> None:
    """RCEm limit 0 caps the active provider surplus without changing SOC."""

    result = _augmented(
        _frame(soc_percent=100.0),
        _flat_timelines_with_rcm_action(
            soc_percent=100.0,
            action="limit_export",
            selected_soc_percent=100.0,
            baseline_soc_percent=100.0,
            planned_export_limit_percent=0.0,
            planned_export_limit_kw=0.0,
        ),
        _expected_timeline(
            [(10.0, 0.0), (0.0, 0.0)],
            initial_soc_percent=100.0,
        ),
    )
    first = result["slots"][0]
    check(first["selected_action"] == "rcm_limit_export", "limit-export action lost")
    planned = first["planned"]
    check(
        abs(planned["expected_grid_export_kwh"]) <= 1e-9,
        "RCEm zero export limit leaked provider surplus",
    )
    check(
        abs(planned["pv_curtailed_kwh"] - 2.5) <= 1e-9,
        "RCEm zero export limit did not expose active PV curtailment",
    )


def _authorization_signature(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Capture only fields owned by the pre-existing canonical ledger."""

    signature: list[dict[str, Any]] = []
    for slot in result["slots"]:
        equation = slot["soc_equation"]
        signature.append(
            {
                "selected_policy": slot["selected_policy"],
                "selected_action": slot["selected_action"],
                "soc_start_percent": equation["soc_start_percent"],
                "soc_end_percent": equation["soc_end_percent"],
                "planned_battery_kwh": slot["planned"]["battery_kwh"],
                "planned_grid_kwh": slot["planned"][
                    "grid_kwh_import_positive"
                ],
                "command_expectation": copy.deepcopy(
                    slot["command_expectation"]
                ),
                "readback_expectation": copy.deepcopy(
                    slot["readback_expectation"]
                ),
            }
        )
    return signature


def test_partial_hardware_bounds_still_uses_provider_p50_observation() -> None:
    samples: list[tuple[float | None, float | None]] = [
        (0.0, 0.0) for _ in range(96)
    ]
    samples[1] = (6.0, 0.0)
    expected = _expected_timeline(
        samples,
        initial_soc_percent=60.0,
        hardware_maximum_soc_percent=None,
    )
    check(expected["state"] == "partial", "partial-bounds fixture is not partial")
    check(
        expected["points"][0]["blocker_code"]
        == "hardware_soc_bounds_partial",
        "partial-bounds fixture has the wrong blocker",
    )
    result = _augmented(_frame(soc_percent=60.0), _sequence_48h(), expected)
    provider_slots = [
        slot
        for slot in result["slots"]
        if slot["soc_equation"]["expected_source"]
        == "provider_p50_partial_bounds"
    ]
    check(
        len(provider_slots) == len(result["slots"]),
        "mathematical 100% bound discarded otherwise complete provider points",
    )
    check(
        result["expected_projection"] == "provider_p50_observation_only",
        "partial hardware bounds hid the real provider source",
    )
    check(
        result["expected_projection_quality"] == "partial",
        "missing hardware maximum was not exposed as partial quality",
    )


def test_missing_stale_or_malformed_expected_timeline_is_safe_fallback() -> None:
    timelines = _sequence_48h()
    reference = _augmented(
        _frame(soc_percent=60.0),
        timelines,
        _expected_48h(),
    )
    reference_authority = _authorization_signature(reference)

    stale = _expected_48h()
    stale["generated_at"] = (NOW - timedelta(minutes=6)).isoformat()
    malformed = _expected_48h()
    malformed["point_count"] = 97
    incomplete_partial_bounds = _expected_timeline(
        [(0.0, 0.0) for _ in range(96)],
        initial_soc_percent=60.0,
        hardware_maximum_soc_percent=None,
    )
    incomplete_partial_bounds["points"][0]["soc_end_percent"] = None
    wrong_entry = _expected_48h()
    wrong_entry["config_entry_id"] = "another-entry"
    missing_model_parameter = _expected_48h()
    missing_model_parameter["maximum_charge_power_kw"] = None
    missing_system_ac_power = _expected_48h()
    missing_system_ac_power["system_ac_power_kw"] = None
    for label, expected in (
        ("missing", None),
        ("stale", stale),
        ("malformed", malformed),
        ("incomplete_partial_bounds", incomplete_partial_bounds),
        ("wrong_entry", wrong_entry),
        ("missing_model_parameter", missing_model_parameter),
        ("missing_system_ac_power", missing_system_ac_power),
    ):
        result = _augmented(_frame(soc_percent=60.0), timelines, expected)
        check(
            _authorization_signature(result) == reference_authority,
            f"{label} observation changed authorization ledger fields",
        )
        check(
            result["expected_projection"]
            == "authorization_fallback_observation_only",
            f"{label} observation was presented as provider P50",
        )
        check(
            result["expected_projection_quality"] == "partial",
            f"{label} observation did not expose fallback quality",
        )
        check(
            result["slots"][0]["soc_equation"]["expected_source"]
            == "rce_calibrated_fallback",
            f"{label} observation lost explicit RCE fallback provenance",
        )


def test_partial_expected_coverage_falls_back_per_slot() -> None:
    samples: list[tuple[float | None, float | None]] = [
        (0.0, 0.0) for _ in range(96)
    ]
    samples[1] = (None, 0.0)
    expected = _expected_timeline(samples, initial_soc_percent=60.0)
    check(expected["state"] == "partial", "coverage fixture is not partial")
    result = _augmented(_frame(soc_percent=60.0), _sequence_48h(), expected)
    sources = [slot["soc_equation"]["expected_source"] for slot in result["slots"]]
    check(sources[:2] == ["provider_p50", "provider_p50"], "current point lost")
    check(
        sources[2] == "rce_calibrated_fallback",
        "partial provider point was not rejected per slot",
    )
    check(
        result["expected_projection"] == "mixed_observation_only",
        "mixed provider/fallback coverage was not exposed",
    )
    check(
        result["expected_projection_quality"] == "partial",
        "mixed provider/fallback coverage was not marked partial",
    )


def test_live_like_large_battery_uses_raw_129_97_kwh_provider_forecast() -> None:
    """Guard the reported 230 kWh site against the old 73.75 kWh fallback."""

    capacity_kwh = 230.0
    expected = _expected_timeline(
        _site_samples(pv_total_kwh=129.97, load_total_kwh=40.56),
        initial_soc_percent=53.0,
        capacity_kwh=capacity_kwh,
        charge_efficiency=0.90,
        discharge_efficiency=0.95,
    )
    result = _augmented(
        _frame(soc_percent=53.0),
        _large_site_timelines(capacity_kwh=capacity_kwh),
        expected,
        capacity_kwh=capacity_kwh,
    )
    equations = [_soc_equation(slot) for slot in result["slots"]]
    expected_max = max(item["expected_soc_end_percent"] for item in equations)
    expected_end = equations[-1]["expected_soc_end_percent"]
    authorization_max = max(
        item["authorization_soc_end_percent"] for item in equations
    )
    potential_pv = sum(
        slot["planned"]["potential_pv_kwh"] for slot in result["slots"]
    )
    expected_load = sum(
        slot["planned"]["expected_load_kwh"] for slot in result["slots"]
    )
    check(
        abs(potential_pv - 129.97) <= 0.01,
        "live-like projection did not consume the raw 129.97 kWh forecast",
    )
    check(
        abs(expected_load - 40.56) <= 0.01,
        "live-like projection did not consume the 40.56 kWh load model",
    )
    check(
        90.0 <= expected_end <= expected_max <= 98.0,
        "raw provider forecast did not produce the realistic high SOC range",
    )
    check(
        authorization_max < 78.0,
        "conservative 73.75 kWh fallback fixture no longer detects source drift",
    )
    check(
        expected_max - authorization_max >= 15.0,
        "provider P50 accidentally reused the old conservative policy series",
    )
    check(
        all(item["expected_source"] == "provider_p50" for item in equations),
        "live-like 48-hour horizon did not retain provider provenance",
    )


def test_live_like_remaining_today_surplus_cannot_render_flat_soc() -> None:
    """The reported remaining-today energy must visibly move a 230 kWh SOC."""

    capacity_kwh = 230.0
    initial_soc = 65.0
    remaining_today_pv_kwh = 66.18
    remaining_today_load_kwh = 43.16
    current_day_slots = 24
    samples: list[tuple[float | None, float | None]] = [
        (0.0, 0.0) for _ in range(96)
    ]
    pv_kw = remaining_today_pv_kwh / (current_day_slots * 0.5)
    load_kw = remaining_today_load_kwh / (current_day_slots * 0.5)
    for index in range(current_day_slots):
        samples[index] = (pv_kw, load_kw)

    expected = _expected_timeline(
        samples,
        initial_soc_percent=initial_soc,
        capacity_kwh=capacity_kwh,
        charge_efficiency=0.90,
        discharge_efficiency=0.95,
        maximum_charge_power_kw=32.0,
        maximum_discharge_power_kw=32.0,
        system_ac_power_kw=40.0,
    )
    result = _augmented(
        _frame(soc_percent=initial_soc),
        _large_site_timelines(
            capacity_kwh=capacity_kwh,
            initial_soc_percent=initial_soc,
        ),
        expected,
        capacity_kwh=capacity_kwh,
    )
    equations = [_soc_equation(slot) for slot in result["slots"]]
    # Twenty-four native 30-minute inputs become 48 canonical 15-minute slots.
    today_equations = equations[: current_day_slots * 2]
    expected_gain_percent = (
        (remaining_today_pv_kwh - remaining_today_load_kwh)
        * 0.90
        / capacity_kwh
        * 100.0
    )
    rendered_gain_percent = (
        today_equations[-1]["expected_soc_end_percent"] - initial_soc
    )
    check(
        abs(rendered_gain_percent - expected_gain_percent) <= 0.02,
        "remaining-today P50 surplus was not conserved in the SOC line",
    )
    check(
        rendered_gain_percent >= 8.9,
        "reported 66.18/43.16 kWh remaining-today balance rendered flat",
    )
    check(
        all(
            current["expected_soc_end_percent"]
            >= previous["expected_soc_end_percent"]
            for previous, current in zip(
                today_equations,
                today_equations[1:],
            )
        ),
        "positive remaining-today surplus produced a falling SOC segment",
    )


def test_rce_created_headroom_is_refilled_by_later_raw_p50() -> None:
    """A full neutral trace must be re-simulated after the selected discharge."""

    samples = [(0.0, 0.0) for _ in range(96)]
    samples[0] = (8.0, 0.0)
    samples[7] = (8.0, 0.0)
    expected = _expected_timeline(
        samples,
        initial_soc_percent=95.0,
        hardware_maximum_soc_percent=95.0,
    )
    # Both raw-PV points are flat in the independent neutral trace because it
    # starts full.  Only a cursor-based replay can use the later point after
    # the intervening RCE action creates headroom.
    check(
        expected["points"][7]["soc_start_percent"]
        == expected["points"][7]["soc_end_percent"]
        == 95.0,
        "headroom regression fixture is not initially full",
    )
    timelines = _sequence_48h()
    before = _augmented(_frame(soc_percent=95.0), timelines, None)
    result = _augmented(_frame(soc_percent=95.0), timelines, expected)
    check(
        _authorization_signature(result) == _authorization_signature(before),
        "cursor-based P50 replay changed the authorization ledger",
    )
    slots = result["slots"]
    rce_indices = [
        index
        for index, slot in enumerate(slots)
        if slot["selected_action"] == "rce_export"
    ]
    check(rce_indices, "headroom regression lost the RCE action")
    after_rce = slots[rce_indices[-1]]["soc_equation"][
        "expected_soc_end_percent"
    ]
    later_peak = max(
        slot["soc_equation"]["expected_soc_end_percent"]
        for slot in slots[rce_indices[-1] + 1 :]
    )
    check(after_rce < 95.0, "RCE did not create expected battery headroom")
    check(
        later_peak == 95.0 and later_peak > after_rce,
        "later raw P50 did not refill action-created headroom",
    )


def test_tariff_charge_does_not_raise_expected_soc_above_its_target() -> None:
    expected = _expected_timeline(
        [(0.0, 0.0) for _ in range(96)],
        initial_soc_percent=80.0,
        hardware_maximum_soc_percent=95.0,
    )
    result = _augmented(
        _frame(soc_percent=80.0),
        _sequence_48h(),
        expected,
    )
    tariff_slots = [
        slot
        for slot in result["slots"]
        if slot["selected_action"] == "tariff_battery_charge"
    ]
    check(tariff_slots, "target regression lost the tariff action")
    for slot in tariff_slots:
        equation = slot["soc_equation"]
        check(
            equation["expected_soc_start_percent"]
            == equation["expected_soc_end_percent"]
            == 80.0,
            "tariff charged even though expected SOC already exceeded target",
        )


def test_provider_effective_maximum_is_a_hard_charge_ceiling() -> None:
    samples = [(0.0, 0.0) for _ in range(96)]
    samples[1] = (40.0, 0.0)
    result = _augmented(
        _frame(soc_percent=90.0),
        _sequence_48h(),
        _expected_timeline(
            samples,
            initial_soc_percent=90.0,
            hardware_maximum_soc_percent=95.0,
        ),
    )
    expected_soc = [
        slot["soc_equation"]["expected_soc_end_percent"]
        for slot in result["slots"]
    ]
    check(max(expected_soc) == 95.0, "provider path did not reach the 95% bound")
    check(
        all(value <= 95.0 for value in expected_soc),
        "provider path crossed effective_maximum_soc_percent",
    )


def test_dynamic_charge_limit_reports_residual_export_and_curtailment() -> None:
    samples = [(10.0, 0.0), (0.0, 0.0)]
    timelines = _full_battery_zero_export()
    allowed = _augmented(
        _frame(soc_percent=50.0, export_state=ExportState.VERIFIED_ALLOWED),
        timelines,
        _expected_timeline(
            samples,
            initial_soc_percent=50.0,
            capacity_kwh=10.0,
            charge_efficiency=0.8,
            maximum_charge_power_kw=2.0,
        ),
    )
    check(
        abs(allowed["expected_final_soc_percent"] - 60.0) <= 1e-9,
        "2 kW stored-power limit did not bound the raw-P50 SOC rise",
    )
    check(
        abs(allowed["expected_grid_export_kwh"] - 3.75) <= 1e-9,
        "residual PV was not reported as allowed export",
    )
    check(
        abs(allowed["expected_pv_curtailed_kwh"]) <= 1e-9,
        "allowed residual PV was incorrectly curtailed",
    )

    blocked = _augmented(
        _frame(
            soc_percent=50.0,
            export_state=ExportState.CONFIRMED_ZERO_EXPORT,
        ),
        timelines,
        _expected_timeline(
            samples,
            initial_soc_percent=50.0,
            zero_export_confirmed=True,
            capacity_kwh=10.0,
            charge_efficiency=0.8,
            maximum_charge_power_kw=2.0,
        ),
    )
    check(
        abs(blocked["expected_grid_export_kwh"]) <= 1e-9,
        "zero export leaked the charge-limit residual to grid",
    )
    check(
        abs(blocked["expected_pv_curtailed_kwh"] - 3.75) <= 1e-9,
        "zero export did not report the charge-limit residual as curtailment",
    )


def test_rce_action_never_duplicates_pv_that_refills_its_headroom() -> None:
    """A rising selected trace inside RCE is PV, not charge by the action."""

    rce = [
        _point(
            "rce",
            index=0,
            minutes=30,
            battery_delta_kwh=-2.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=2.0,
            soc_percent=80.0,
            baseline_soc_percent=100.0,
            selected=True,
            action="export",
        ),
        replace(
            _point(
                "rce",
                index=1,
                minutes=30,
                # The public battery direction remains discharge while the
                # SOC derivative rises because PV refills the headroom.
                battery_delta_kwh=-0.5,
                pv_kwh=1.0,
                load_kwh=0.0,
                grid_import_kwh=0.0,
                grid_export_kwh=0.0,
                soc_percent=90.0,
                baseline_soc_percent=100.0,
                selected=True,
                action="export",
            ),
            policy=_policy(
                "rce",
                selected=True,
                action="export",
                battery_delta_kwh=-0.5,
                grid_import_kwh=0.0,
                grid_export_kwh=0.5,
                target_soc_percent=None,
            ),
        ),
    ]
    tariff = [
        _point(
            "tariff",
            index=index,
            minutes=30,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=100.0,
            baseline_soc_percent=100.0,
        )
        for index in range(2)
    ]
    rcm = [
        _point(
            "rcm",
            index=index,
            minutes=15,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=100.0,
            baseline_soc_percent=100.0,
        )
        for index in range(4)
    ]
    timelines = {
        "rce": _payload("rce", rce, actual_soc_percent=100.0),
        "tariff": _payload("tariff", tariff, actual_soc_percent=100.0),
        "rcm": _payload("rcm", rcm, actual_soc_percent=100.0),
    }
    result = _augmented(
        _frame(soc_percent=100.0),
        timelines,
        _expected_timeline(
            [(0.0, 0.0), (2.0, 0.0)],
            initial_soc_percent=100.0,
        ),
    )
    slots = result["slots"]
    check(
        slots[1]["soc_equation"]["expected_soc_end_percent"] == 80.0,
        "first RCE interval did not create the fixture headroom",
    )
    check(
        slots[-1]["soc_equation"]["expected_soc_end_percent"] == 85.0,
        "RCE action duplicated the raw PV recharge as positive action energy",
    )


def test_rce_export_and_pv_charge_share_the_system_ac_bridge() -> None:
    """Controlled export consumes bridge headroom before same-slot PV refill."""

    rce = [
        _point(
            "rce",
            index=0,
            minutes=30,
            battery_delta_kwh=-2.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=2.0,
            soc_percent=70.0,
            baseline_soc_percent=80.0,
            selected=True,
            action="export",
        ),
        _point(
            "rce",
            index=1,
            minutes=30,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=70.0,
            baseline_soc_percent=80.0,
        ),
    ]
    timelines = _flat_one_hour_timelines(soc_percent=80.0, rce=rce)
    expected = _expected_timeline(
        [(10.0, 0.0), (0.0, 0.0)],
        initial_soc_percent=80.0,
        capacity_kwh=20.0,
        system_ac_power_kw=10.0,
    )
    before = _augmented(
        _frame(soc_percent=80.0),
        timelines,
        None,
        capacity_kwh=20.0,
    )
    result = _augmented(
        _frame(soc_percent=80.0),
        timelines,
        expected,
        capacity_kwh=20.0,
    )

    check(
        _authorization_signature(result) == _authorization_signature(before),
        "shared-bridge P50 replay changed authorization",
    )
    check(
        result["slots"][1]["soc_equation"]["expected_soc_end_percent"]
        == 85.0,
        "RCE export did not share the 10 kW bridge with same-slot PV charging",
    )
    check(
        abs(result["expected_grid_export_kwh"] - 2.0) <= 1e-9,
        "shared bridge changed the actually applied RCE export",
    )
    check(
        abs(result["expected_pv_curtailed_kwh"] - 2.0) <= 1e-9,
        "PV above the RCE-occupied bridge was not marked curtailed",
    )


def test_floor_limited_rce_reserves_only_applied_bridge_export() -> None:
    """A reserve clamp cannot consume bridge headroom for an unrealized sale."""

    selected = _point(
        "rce",
        index=0,
        minutes=30,
        battery_delta_kwh=-1.0,
        pv_kwh=0.0,
        load_kwh=0.0,
        grid_import_kwh=0.0,
        grid_export_kwh=1.0,
        soc_percent=50.0,
        baseline_soc_percent=55.0,
        selected=True,
        action="export",
    )
    selected = replace(
        selected,
        policy=_policy(
            "rce",
            selected=True,
            action="export",
            battery_delta_kwh=-2.0,
            grid_import_kwh=0.0,
            grid_export_kwh=2.0,
            target_soc_percent=None,
        ),
    )
    rce = [
        selected,
        _point(
            "rce",
            index=1,
            minutes=30,
            battery_delta_kwh=0.0,
            pv_kwh=0.0,
            load_kwh=0.0,
            grid_import_kwh=0.0,
            grid_export_kwh=0.0,
            soc_percent=50.0,
            baseline_soc_percent=55.0,
        )
    ]
    timelines = _flat_one_hour_timelines(soc_percent=55.0, rce=rce)
    result = _augmented(
        _frame(soc_percent=55.0),
        timelines,
        _expected_timeline(
            [(10.0, 0.0), (0.0, 0.0)],
            initial_soc_percent=55.0,
            capacity_kwh=20.0,
            system_ac_power_kw=4.8,
        ),
        capacity_kwh=20.0,
    )

    check(
        result["slots"][0]["selected_action"] == "rce_export",
        "floor-limited regression lost the RCE action",
    )
    check(
        result["slots"][0]["soc_equation"]["expected_soc_end_percent"]
        == 51.0,
        "neutral replay reserved bridge power for unapplied RCE export",
    )
    check(
        abs(
            result["slots"][1]["soc_equation"]["expected_soc_end_percent"]
            - 55.0
        )
        <= 1e-9,
        "floor-limited slot did not preserve the exact expected SOC chain",
    )
    check(
        abs(result["expected_grid_export_kwh"] - 1.2) <= 1e-9,
        "floor clamp did not scale the displayed RCE export",
    )
    check(
        abs(result["expected_pv_curtailed_kwh"] - 3.8) <= 1e-9,
        "floor-limited RCE bridge curtailment is incorrect",
    )


def test_provider_disposition_respects_system_ac_bridge() -> None:
    """Residual P50 is exported or curtailed only within the rated bridge."""

    timelines = _flat_one_hour_timelines(soc_percent=100.0)
    expected = _expected_timeline(
        [(20.0, 0.0), (0.0, 0.0)],
        initial_soc_percent=100.0,
        capacity_kwh=20.0,
        system_ac_power_kw=10.0,
    )
    allowed = _augmented(
        _frame(soc_percent=100.0, export_state=ExportState.VERIFIED_ALLOWED),
        timelines,
        expected,
        capacity_kwh=20.0,
    )
    check(
        abs(allowed["expected_grid_export_kwh"] - 5.0) <= 1e-9,
        "allowed provider surplus exceeded the 10 kW AC bridge",
    )
    check(
        abs(allowed["expected_pv_curtailed_kwh"] - 5.0) <= 1e-9,
        "allowed provider surplus hid bridge curtailment",
    )

    blocked = _augmented(
        _frame(
            soc_percent=100.0,
            export_state=ExportState.CONFIRMED_ZERO_EXPORT,
        ),
        timelines,
        _expected_timeline(
            [(20.0, 0.0), (0.0, 0.0)],
            initial_soc_percent=100.0,
            zero_export_confirmed=True,
            capacity_kwh=20.0,
            system_ac_power_kw=10.0,
        ),
        capacity_kwh=20.0,
    )
    check(
        abs(blocked["expected_grid_export_kwh"]) <= 1e-9,
        "zero export leaked provider surplus through the AC bridge",
    )
    check(
        abs(blocked["expected_pv_curtailed_kwh"] - 10.0) <= 1e-9,
        "zero export did not curtail all unabsorbed provider PV",
    )


def test_provider_discharge_matches_baseline_system_ac_cap() -> None:
    """Canonical neutral discharge reuses the exact baseline bridge ceiling."""

    timelines = _flat_one_hour_timelines(soc_percent=100.0)
    expected = _expected_timeline(
        [(0.0, 15.0), (0.0, 0.0)],
        initial_soc_percent=100.0,
        capacity_kwh=20.0,
        discharge_efficiency=0.95,
        maximum_discharge_power_kw=20.0,
        system_ac_power_kw=10.0,
    )
    result = _augmented(
        _frame(soc_percent=100.0),
        timelines,
        expected,
        capacity_kwh=20.0,
    )
    baseline_end = expected["points"][0]["soc_end_percent"]
    canonical_end = result["slots"][1]["soc_equation"][
        "expected_soc_end_percent"
    ]
    check(
        abs(canonical_end - baseline_end) <= 1e-4,
        "canonical discharge diverged from the baseline system-AC cap",
    )
    check(
        abs(canonical_end - 73.684211) <= 1e-6,
        "15 kW deficit bypassed the 10 kW system AC bridge",
    )


def test_provider_shared_bridge_keeps_load_import_and_pv_disposition_exact() -> None:
    """PV cannot both satisfy raw LOAD and reuse the complete AC bridge."""

    timelines = _flat_one_hour_timelines(soc_percent=100.0)
    saturated = _expected_timeline(
        [(20.0, 15.0), (0.0, 0.0)],
        initial_soc_percent=100.0,
        capacity_kwh=20.0,
        system_ac_power_kw=10.0,
    )
    saturated_point = saturated["points"][0]
    check(
        saturated_point["battery_kw"] == 0.0
        and saturated_point["grid_import_kw"] == 5.0
        and saturated_point["grid_export_kw"] == 0.0,
        "baseline did not preserve import behind bridge-limited PV→LOAD",
    )
    result = _augmented(
        _frame(soc_percent=100.0),
        timelines,
        saturated,
        capacity_kwh=20.0,
    )
    first_half_hour = result["slots"][:2]
    check(
        result["slots"][1]["soc_equation"]["expected_soc_end_percent"]
        == 100.0,
        "bridge-limited raw surplus changed SOC without battery headroom",
    )
    check(
        abs(
            sum(slot["planned"]["usable_pv_kwh"] for slot in first_half_hour)
            - 5.0
        )
        <= 1e-9,
        "canonical usable PV exceeded the PV→LOAD bridge share",
    )
    check(
        abs(
            sum(slot["planned"]["pv_curtailed_kwh"] for slot in first_half_hour)
            - 5.0
        )
        <= 1e-9,
        "canonical did not curtail the bridge-blocked raw PV share",
    )
    check(
        abs(result["expected_grid_export_kwh"]) <= 1e-9,
        "bridge-saturated LOAD leaked raw PV to export",
    )

    deficit = _expected_timeline(
        [(8.0, 15.0), (0.0, 0.0)],
        initial_soc_percent=100.0,
        capacity_kwh=20.0,
        discharge_efficiency=1.0,
        maximum_discharge_power_kw=100.0,
        system_ac_power_kw=10.0,
    )
    deficit_point = deficit["points"][0]
    check(
        deficit_point["battery_kw"] == -2.0
        and deficit_point["grid_import_kw"] == 5.0,
        "baseline gave PV and battery separate 10 kW bridges",
    )
    result = _augmented(
        _frame(soc_percent=100.0),
        timelines,
        deficit,
        capacity_kwh=20.0,
    )
    first_half_hour = result["slots"][:2]
    check(
        result["slots"][1]["soc_equation"]["expected_soc_end_percent"]
        == 95.0,
        "canonical battery supplied more than the 1 kWh bridge remainder",
    )
    check(
        abs(
            sum(slot["planned"]["usable_pv_kwh"] for slot in first_half_hour)
            - 4.0
        )
        <= 1e-9,
        "canonical lost usable PV while sharing the bridge with battery LOAD",
    )
    check(
        abs(
            sum(slot["planned"]["pv_curtailed_kwh"] for slot in first_half_hour)
        )
        <= 1e-9,
        "canonical curtailed PV that fits before battery-to-LOAD",
    )


def main() -> None:
    test_48h_dual_track_replays_one_command_against_two_soc_states()
    test_full_battery_zero_export_is_flat_and_reports_curtailment()
    test_tariff_grid_support_only_offsets_its_direct_load_share()
    test_morning_rcem_pre_discharge_replays_against_both_soc_states()
    test_rcem_below_target_neither_creates_energy_nor_discharges_further()
    test_rcem_absorb_replays_raw_cursor_without_negative_derivative()
    test_rcem_zero_export_limit_curtails_provider_surplus()
    test_partial_hardware_bounds_still_uses_provider_p50_observation()
    test_missing_stale_or_malformed_expected_timeline_is_safe_fallback()
    test_partial_expected_coverage_falls_back_per_slot()
    test_live_like_large_battery_uses_raw_129_97_kwh_provider_forecast()
    test_live_like_remaining_today_surplus_cannot_render_flat_soc()
    test_rce_created_headroom_is_refilled_by_later_raw_p50()
    test_tariff_charge_does_not_raise_expected_soc_above_its_target()
    test_provider_effective_maximum_is_a_hard_charge_ceiling()
    test_dynamic_charge_limit_reports_residual_export_and_curtailment()
    test_rce_action_never_duplicates_pv_that_refills_its_headroom()
    test_rce_export_and_pv_charge_share_the_system_ac_bridge()
    test_floor_limited_rce_reserves_only_applied_bridge_export()
    test_provider_disposition_respects_system_ac_bridge()
    test_provider_discharge_matches_baseline_system_ac_cap()
    test_provider_shared_bridge_keeps_load_import_and_pv_disposition_exact()
    print(f"Supervisor canonical dual track: PASS ({CHECKS} checks)")


if __name__ == "__main__":
    main()
