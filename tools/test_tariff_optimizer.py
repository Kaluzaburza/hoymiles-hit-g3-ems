"""Deterministic tests for the tariff grid-charging optimizer."""

from __future__ import annotations

import ast
from dataclasses import asdict
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
from random import Random
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from tariff_optimizer import (  # noqa: E402
    TariffOptimizerInput,
    TariffSchedule,
    _classify_current_run_need,
    _simulate,
    adaptive_forecast_factor,
    horizon_gap_load_reserve_kwh,
    horizon_gap_expensive_load_reserve_kwh,
    is_polish_public_holiday,
    numeric_sample_is_fresh,
    optimize_tariff_charging,
    resolve_planning_horizon,
    robust_weighted_estimate,
    robust_weighted_upper_estimate,
    tariff_rate,
)


ZONE = ZoneInfo("Europe/Warsaw")


def existing_result_digest(result) -> str:
    """Hash every result field that existed before Phase 1B-1."""
    payload = asdict(result)
    payload.pop("timeline_trace")
    assert payload.pop("current_run_need_class") in {
        "required_energy",
        "economic",
        "mixed",
        "none",
    }
    serialized = json.dumps(
        payload,
        default=lambda value: value.isoformat(),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def schedule(kind: str = "G12") -> TariffSchedule:
    return TariffSchedule(
        tariff_type=kind,
        g11_price_pln_kwh=0.85,
        low_price_pln_kwh=0.62,
        medium_price_pln_kwh=0.82,
        peak_price_pln_kwh=1.03,
        cheap_windows=((22 * 60, 6 * 60), (13 * 60, 15 * 60)),
        medium_windows=((7 * 60, 13 * 60),),
        weekend_low_price=kind in {"G12w", "G13"},
        polish_holidays_low_price=True,
    )


def settings(now: datetime, **overrides) -> TariffOptimizerInput:
    values = {
        "now": now,
        "pv_by_slot_kwh": {},
        "battery_capacity_kwh": 20.0,
        "battery_soc_percent": 30.0,
        "reserve_soc_percent": 20.0,
        "maximum_soc_percent": 100.0,
        "average_daily_load_kwh": 18.0,
        "average_night_load_kwh": 8.0,
        "night_start_minute": 20 * 60,
        "night_end_minute": 7 * 60,
        "charge_power_kw": 5.0,
        "charge_efficiency_percent": 95.0,
        "discharge_efficiency_percent": 95.0,
        "minimum_saving_pln_kwh": 0.05,
        "schedule": schedule(),
    }
    values.update(overrides)
    return TariffOptimizerInput(**values)


def test_timeline_quality_and_legacy_output_contract() -> None:
    """Pin partial/complete truth without changing any legacy result field."""

    pre_peak = datetime(2026, 8, 6, 12, 30, tzinfo=ZONE)
    peak_load = {
        pre_peak.replace(hour=hour, minute=minute): 10.0 / 14.0
        for hour in range(15, 22)
        for minute in (0, 30)
    }
    common = {
        "now": pre_peak.replace(hour=14, minute=50),
        "reserve_soc_percent": 20.0,
        "average_daily_load_kwh": 0.0,
        "average_night_load_kwh": 0.0,
        "load_by_slot_kwh": peak_load,
        "charge_power_kw": 10.0,
        "battery_charge_power_kw": 20.0,
        "charge_efficiency_percent": 100.0,
        "discharge_efficiency_percent": 100.0,
        "minimum_saving_pln_kwh": 0.0,
    }

    soc_cases = (
        (20.0, 8.333333333333334, "eeee285a1ec96443037881f1bb4c0cb0dc4b5230b8c2b3b137d2889cc4a00938"),
        (25.0, 7.333333333333335, "1602ebc10ba2f6b81e5fb84eef1923b712f5418c7d6eeaa2529bd33b3fe42665"),
        (50.0, 2.333333333333331, "e88238e65fc132e9bc354d16b212c991b07e3ef88fafa2116688a6036393c37f"),
    )
    for soc, residual, legacy_digest in soc_cases:
        result = optimize_tariff_charging(
            settings(**common, battery_soc_percent=soc)
        )
        assert result.status_code == "insufficient_cheap_window"
        assert len(result.planned_charges) == 1
        assert abs(result.remaining_shortage_kwh - residual) < 1e-12
        assert existing_result_digest(result) == legacy_digest
        assert result.timeline_trace is not None
        assert len(result.timeline_trace.points) == 67
        assert result.timeline_trace.quality == "partial"
        assert result.timeline_trace.blocker_code == "insufficient_cheap_window"

    no_charge = optimize_tariff_charging(
        settings(**common, battery_soc_percent=90.0, horizon_days=3)
    )
    assert no_charge.status_code == "no_charge_needed"
    assert not no_charge.planned_charges
    assert no_charge.remaining_shortage_kwh == 0.0
    assert existing_result_digest(no_charge) == (
        "5c1d74084a7df2fc34ef7558ae92cb310ddaebf296ea4638f21576534f13ec8e"
    )
    assert no_charge.timeline_trace is not None
    assert len(no_charge.timeline_trace.points) == 96
    assert no_charge.timeline_trace.quality == "complete"
    assert no_charge.timeline_trace.blocker_code is None

    complete_battery = optimize_tariff_charging(
        settings(
            **{
                **common,
                "now": pre_peak,
                "battery_soc_percent": 20.0,
                "horizon_days": 3,
            }
        )
    )
    assert complete_battery.status_code == "ready"
    assert complete_battery.remaining_shortage_kwh == 0.0
    assert existing_result_digest(complete_battery) == (
        "75228e72e1d4b14cc51dc55907484d3ec25b39345097a7024ebc1e01c2a165cd"
    )
    assert complete_battery.timeline_trace is not None
    assert len(complete_battery.timeline_trace.points) == 96
    assert complete_battery.timeline_trace.quality == "complete"
    assert complete_battery.timeline_trace.blocker_code is None
    assert {
        point.action_code
        for point in complete_battery.timeline_trace.points
        if point.selected
    } == {"battery_charge"}

    loaded_cheap_period = dict(peak_load)
    loaded_cheap_period.update(
        {
            pre_peak.replace(hour=hour, minute=minute): 2.0
            for hour in (13, 14)
            for minute in (0, 30)
        }
    )
    support_and_charge = optimize_tariff_charging(
        settings(
            **{
                **common,
                "now": pre_peak.replace(hour=13, minute=0),
                "battery_soc_percent": 20.0,
                "load_by_slot_kwh": loaded_cheap_period,
                "horizon_days": 3,
            }
        )
    )
    assert support_and_charge.remaining_shortage_kwh == 0.0
    assert existing_result_digest(support_and_charge) == (
        "2ce2351bbb10b701abd28c2c4d60bc411d42e7ebc23e0aeeb865e7d64441c098"
    )
    assert support_and_charge.timeline_trace is not None
    assert len(support_and_charge.timeline_trace.points) == 96
    assert support_and_charge.timeline_trace.quality == "complete"
    assert support_and_charge.timeline_trace.blocker_code is None
    assert {
        point.action_code
        for point in support_and_charge.timeline_trace.points
        if point.selected
    } == {"grid_support_and_charge"}

    grid_support = optimize_tariff_charging(
        settings(
            **{
                **common,
                "now": pre_peak.replace(hour=13, minute=0),
                "battery_soc_percent": 100.0,
                "load_by_slot_kwh": loaded_cheap_period,
                "horizon_days": 3,
            }
        )
    )
    assert grid_support.remaining_shortage_kwh == 0.0
    assert existing_result_digest(grid_support) == (
        "f7bb21376e0fc4227ca9e79921363788487540d97855dccfe3c4401721d54845"
    )
    assert grid_support.timeline_trace is not None
    assert len(grid_support.timeline_trace.points) == 96
    assert grid_support.timeline_trace.quality == "complete"
    assert grid_support.timeline_trace.blocker_code is None
    assert {
        point.action_code
        for point in grid_support.timeline_trace.points
        if point.selected
    } == {"grid_support"}

    partial_horizon = optimize_tariff_charging(
        settings(
            **{
                **common,
                "now": pre_peak,
                "battery_soc_percent": 20.0,
                "horizon_days": 2,
            }
        )
    )
    assert partial_horizon.status_code == "ready"
    assert partial_horizon.remaining_shortage_kwh == 0.0
    assert existing_result_digest(partial_horizon) == (
        "cdd6a2a3644ec4ce336926e230b6c286d969d23198a3a6ce985c5a114b10a4cd"
    )
    assert partial_horizon.timeline_trace is not None
    assert len(partial_horizon.timeline_trace.points) == 71
    assert partial_horizon.timeline_trace.quality == "partial"
    assert partial_horizon.timeline_trace.blocker_code == "planning_horizon_limited"

    stale = optimize_tariff_charging(
        settings(
            **{
                **common,
                "now": pre_peak,
                "battery_soc_percent": 20.0,
                "horizon_days": 3,
                "control_inputs_fresh": False,
                "control_input_block_reason": "missing_soc",
            }
        )
    )
    assert not stale.control_inputs_fresh
    assert stale.control_input_block_reason == "missing_soc"
    assert stale.remaining_shortage_kwh == 0.0
    assert existing_result_digest(stale) == (
        "fc9951d16f8eed790a3a81cf8e967dd4025603a3d743a713ce4174c2072da302"
    )
    assert stale.timeline_trace is None

    # Missing critical input never reaches the optimizer in the HA adapter;
    # it clears the result and retains the existing unavailable publication.
    sensor_source = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "tariff_sensor.py"
    ).read_text(encoding="utf-8")
    publish_start = sensor_source.index("def _publish_timeline_result")
    publish_end = sensor_source.index("\n    def ", publish_start + 8)
    publish_source = sensor_source[publish_start:publish_end]
    assert "quality=" not in publish_source
    assert "self._result is None" in publish_source
    assert "publish_unavailable" in publish_source


def main() -> None:
    test_timeline_quality_and_legacy_output_contract()
    monday = datetime(2026, 8, 3, 21, 10, tzinfo=ZONE)

    # Freshness treats a repeated exact zero as a real sample. HA's
    # ``last_reported`` age, not only value changes, decides whether it is
    # current; stale, missing, non-finite and implausibly future data fail.
    assert numeric_sample_is_fresh(0.0, 0.0, 300.0)
    assert numeric_sample_is_fresh(0.0, 299.9, 300.0)
    assert not numeric_sample_is_fresh(0.0, 300.1, 300.0)
    assert not numeric_sample_is_fresh(None, 0.0, 300.0)
    assert not numeric_sample_is_fresh(float("nan"), 0.0, 300.0)
    assert not numeric_sample_is_fresh(10.0, -5.1, 300.0)

    # The inverter has one shared Grid Charge budget.  With a 10 kW command in
    # a 30-minute slot, 4 kWh of LOAD leaves only 1 kWh AC for the battery.
    # Merely enabling the mode also moves the complete remaining LOAD to grid;
    # the requested support value is a mode flag, not a fractional flow.
    physical = settings(
        monday.replace(hour=22, minute=0),
        battery_soc_percent=50.0,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
        charge_power_kw=10.0,
        battery_charge_power_kw=20.0,
    )
    physical_simulation = _simulate(
        physical,
        [physical.now],
        [4.0],
        {0: 5.0},
        {0: 0.1},
        [(0.62, "low")],
        [1.0],
    )
    assert abs(physical_simulation.accepted_support_kwh[0] - 4.0) < 1e-6
    assert abs(physical_simulation.accepted_import_kwh[0] - 1.0) < 1e-6
    assert abs(
        physical_simulation.accepted_support_kwh[0]
        + physical_simulation.accepted_import_kwh[0]
        - 5.0
    ) < 1e-6

    # The BMS limit applies only to battery-side DC power.  It must not reduce
    # the part of the common AC budget that supplies the home.
    bms_limited = settings(
        physical.now,
        battery_soc_percent=50.0,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
        charge_power_kw=10.0,
        battery_charge_power_kw=2.0,
    )
    bms_simulation = _simulate(
        bms_limited,
        [physical.now],
        [2.0],
        {0: 5.0},
        {},
        [(0.62, "low")],
        [1.0],
    )
    assert abs(bms_simulation.accepted_support_kwh[0] - 2.0) < 1e-6
    assert abs(bms_simulation.stored_import_kwh[0] - 1.0) < 1e-6
    assert bms_simulation.total_grid_import_kwh < 5.0

    # PV and grid charging share the same BMS current limit.  One source must
    # not receive the full limit after the other has already consumed it.
    mixed_source = settings(
        physical.now,
        pv_by_slot_kwh={physical.now: 1.0},
        battery_soc_percent=50.0,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
        charge_power_kw=10.0,
        battery_charge_power_kw=2.0,
        pv_charge_power_kw=2.0,
    )
    mixed_simulation = _simulate(
        mixed_source,
        [physical.now],
        [0.0],
        {0: 5.0},
        {},
        [(0.62, "low")],
        [1.0],
    )
    assert abs(
        mixed_simulation.stored_import_kwh[0] + 0.95 - 1.0
    ) < 1e-6

    # A BMS discharge-current limit can force grid import even when sufficient
    # energy remains above reserve.
    discharge_limited = settings(
        physical.now,
        battery_soc_percent=100.0,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
        battery_discharge_power_kw=2.0,
    )
    discharge_simulation = _simulate(
        discharge_limited,
        [physical.now],
        [3.0],
        {},
        {},
        [(1.03, "peak")],
        [1.0],
    )
    assert abs(discharge_simulation.uncovered_import_kwh[0] - 2.05) < 1e-6

    # Control thresholds cannot manufacture or discard the initial energy.
    below_reserve = settings(
        physical.now,
        battery_soc_percent=10.0,
        reserve_soc_percent=20.0,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
    )
    below_simulation = _simulate(
        below_reserve, [physical.now], [0.0], {}, {}, [(0.62, "low")], [1.0]
    )
    assert abs(below_simulation.ending_battery_kwh - 2.0) < 1e-6
    above_target = settings(
        physical.now,
        battery_soc_percent=90.0,
        maximum_soc_percent=80.0,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
    )
    above_simulation = _simulate(
        above_target, [physical.now], [0.0], {}, {}, [(0.62, "low")], [1.0]
    )
    assert abs(above_simulation.ending_battery_kwh - 18.0) < 1e-6

    result = optimize_tariff_charging(settings(monday))
    assert result.planned_grid_import_kwh > 0
    assert result.estimated_savings_pln > 0
    assert all(item.price_pln_kwh == 0.62 for item in result.planned_charges)
    assert all(
        item.grid_import_kwh <= result.charge_power_kw * 0.5 + 1e-6
        for item in result.planned_charges
    )

    # Charging lead time is derived from energy, effective AC power and the
    # 30-minute slot duration.  A 10 kW Grid Charge budget needs two slots to
    # move 10 kWh, so the latest feasible start before the 15:00 peak is 14:00.
    pre_peak = datetime(2026, 8, 6, 12, 30, tzinfo=ZONE)
    peak_load = {
        pre_peak.replace(hour=hour, minute=minute): 10.0 / 14.0
        for hour in range(15, 22)
        for minute in (0, 30)
    }
    ten_kw_lead = optimize_tariff_charging(
        settings(
            pre_peak,
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=peak_load,
            charge_power_kw=10.0,
            battery_charge_power_kw=20.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert ten_kw_lead.status_code == "ready"
    assert [item.start.strftime("%H:%M") for item in ten_kw_lead.planned_charges] == [
        "14:00",
        "14:30",
    ]
    assert abs(ten_kw_lead.planned_stored_energy_kwh - 10.0) < 1e-6
    assert ten_kw_lead.remaining_shortage_kwh < 0.01

    # The percentage sent to the inverter is a shared Grid Charge budget, not
    # pure battery power.  With 2 kWh of LOAD in every cheap half-hour, only
    # 3 kWh from each 10 kW block remain for storage, so charging must begin at
    # 13:00 instead of incorrectly assuming that 14:00 is still sufficient.
    loaded_cheap_period = dict(peak_load)
    loaded_cheap_period.update(
        {
            pre_peak.replace(hour=hour, minute=minute): 2.0
            for hour in (13, 14)
            for minute in (0, 30)
        }
    )
    net_power_lead = optimize_tariff_charging(
        settings(
            pre_peak,
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=loaded_cheap_period,
            charge_power_kw=10.0,
            battery_charge_power_kw=20.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert net_power_lead.status_code == "ready"
    assert net_power_lead.planned_charges[0].start.strftime("%H:%M") == "13:00"
    assert abs(net_power_lead.planned_stored_energy_kwh - 10.0) < 1e-6
    assert abs(net_power_lead.planned_direct_load_kwh - 8.0) < 1e-6

    combined_active = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=13, minute=0),
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=loaded_cheap_period,
            charge_power_kw=10.0,
            battery_charge_power_kw=20.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert combined_active.current_slot_planned
    assert combined_active.current_action == "grid_support_and_charge"
    assert combined_active.current_slot_end is not None
    assert combined_active.current_slot_end > combined_active.planned_charges[0].start

    # Combined home support + charging and battery-only charging are one
    # required-charge run at the inverter. Crossing that action boundary must
    # not shorten the latch and cause a stop/start notification loop.
    mixed_run_load = {
        pre_peak.replace(hour=hour, minute=minute): 1.3
        for hour in range(15, 22)
        for minute in (0, 30)
    }
    mixed_run_load[pre_peak.replace(hour=13, minute=0)] = 2.0
    mixed_required_charge = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=13, minute=0),
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=mixed_run_load,
            charge_power_kw=10.0,
            battery_charge_power_kw=20.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert mixed_required_charge.planned_charges[0].action == (
        "grid_support_and_charge"
    )
    assert any(
        item.action == "battery_charge"
        for item in mixed_required_charge.planned_charges[1:]
    )
    assert mixed_required_charge.current_slot_end == max(
        item.start for item in mixed_required_charge.planned_charges
    ) + timedelta(minutes=30)

    # On a single 10 kW inverter, a 50% command is only 5 kW.  The same 10 kWh
    # therefore needs all four half-hour slots from 13:00 until 15:00.
    five_kw_lead = optimize_tariff_charging(
        settings(
            pre_peak,
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=peak_load,
            charge_power_kw=5.0,
            battery_charge_power_kw=20.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert five_kw_lead.status_code == "ready"
    assert [item.start.strftime("%H:%M") for item in five_kw_lead.planned_charges] == [
        "13:00",
        "13:30",
        "14:00",
        "14:30",
    ]
    assert abs(five_kw_lead.planned_stored_energy_kwh - 10.0) < 1e-6

    # If only ten minutes remain, the optimizer may safely use them, but it
    # must report that the cheap window cannot cover the complete deficit.
    too_late = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=50),
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=peak_load,
            charge_power_kw=10.0,
            battery_charge_power_kw=20.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert too_late.status_code == "insufficient_cheap_window"
    assert too_late.remaining_shortage_kwh > 8.0
    # The live SOC target must remain the complete required target, not the
    # tiny amount that fits into the last ten minutes.  Otherwise HA reaches
    # the moving target, stops and restarts Grid Charge every minute.
    assert too_late.target_soc_percent >= 69.9
    minute_later = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=55),
            battery_soc_percent=21.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=peak_load,
            charge_power_kw=10.0,
            battery_charge_power_kw=20.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert minute_later.current_slot_planned
    assert abs(minute_later.target_soc_percent - too_late.target_soc_percent) < 0.1
    assert minute_later.current_slot_end == pre_peak.replace(hour=15, minute=0)

    # Regression for the 14:50 micro-cycle: a full battery and a small home
    # load may produce a few groszy of theoretical Grid Support saving, but it
    # must not be eligible for a new EMS mode transition. Fresh live telemetry
    # replaces the historical current-slot estimate.
    micro_support_load = {
        start: 20.0 / 14.0
        for start in peak_load
    }
    micro_support_load[pre_peak.replace(hour=14, minute=30)] = 0.75
    micro_support = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=50, second=15),
            battery_soc_percent=100.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=micro_support_load,
            current_load_power_kw=0.45,
            current_pv_power_kw=0.0,
            current_battery_power_kw=0.45,
            charge_power_kw=10.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert micro_support.current_action == "grid_support"
    assert not micro_support.current_run_start_eligible
    assert micro_support.current_run_suppression_reason == "insufficient_energy"
    assert micro_support.current_run_continue_eligible
    assert micro_support.current_run_continue_reason == "eligible"
    assert micro_support.current_slot_load_source == "live"
    assert abs(micro_support.current_slot_load_kwh - 0.45 * 585 / 3600) < 1e-6
    assert abs(micro_support.current_run_duration_seconds - 585.0) < 1e-6
    assert micro_support.target_soc_percent <= 99.0 + 1e-6

    # The same ten-minute tail remains useful for a genuinely high LOAD. The
    # energy and absolute benefit gates scale naturally instead of imposing a
    # blunt 15-minute cutoff.
    material_support = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=50),
            battery_soc_percent=100.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=micro_support_load,
            current_load_power_kw=5.0,
            current_pv_power_kw=0.0,
            current_battery_power_kw=5.0,
            charge_power_kw=10.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert material_support.current_action == "grid_support"
    assert material_support.current_run_start_eligible
    assert material_support.current_run_suppression_reason == "eligible"
    assert material_support.current_run_continue_eligible
    assert material_support.current_run_continue_reason == "eligible"
    assert material_support.current_run_direct_load_kwh > 0.8
    assert material_support.current_run_benefit_pln >= 0.10

    # Phase 1B-1 provenance is owned by the allocation steps themselves. A
    # normal empty current run is not promoted to an economic action.
    provenance_none = optimize_tariff_charging(
        settings(
            monday.replace(day=4, hour=12, minute=0),
            battery_soc_percent=100.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    assert provenance_none.current_action == "none"
    assert provenance_none.current_run_need_class == "none"

    provenance_required = optimize_tariff_charging(
        settings(
            datetime(2026, 8, 9, 6, 13, tzinfo=ZONE),
            schedule=schedule("G12w"),
            battery_soc_percent=20.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    assert provenance_required.current_action == "battery_charge"
    assert provenance_required.current_run_need_class == "required_energy"
    assert material_support.current_run_need_class == "economic"

    mixed_now = datetime(2026, 8, 3, 22, 0, tzinfo=ZONE)
    provenance_mixed = optimize_tariff_charging(
        settings(
            mixed_now,
            battery_soc_percent=15.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=30.0,
            average_night_load_kwh=12.0,
            charge_power_kw=8.0,
            battery_charge_power_kw=8.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert provenance_mixed.current_slot_planned
    assert provenance_mixed.current_run_need_class == "mixed"

    # The current run is classified across every contiguous accepted slot,
    # including separate required and economic allocations merged by action.
    merged_now = datetime(2026, 8, 4, 4, 30, tzinfo=ZONE)
    merged_load = {
        merged_now.replace(hour=hour, minute=minute): 2.0
        for hour in range(6, 22)
        for minute in (0, 30)
    }
    provenance_merged = optimize_tariff_charging(
        settings(
            merged_now,
            battery_soc_percent=15.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=merged_load,
            charge_power_kw=8.0,
            battery_charge_power_kw=8.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert provenance_merged.current_run_need_class == "mixed"
    assert provenance_merged.current_slot_end == datetime(
        2026, 8, 4, 6, 0, tzinfo=ZONE
    )
    assert len([
        item
        for item in provenance_merged.planned_charges
        if item.start < provenance_merged.current_slot_end
    ]) == 3

    # UTC stepping preserves the origin while a required run crosses a local
    # day boundary.
    midnight_now = datetime(2026, 8, 3, 23, 30, tzinfo=ZONE)
    midnight_run = optimize_tariff_charging(
        settings(
            midnight_now,
            battery_capacity_kwh=20.0,
            battery_soc_percent=0.0,
            base_reserve_soc_percent=50.0,
            reserve_soc_percent=50.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            charge_power_kw=5.0,
            battery_charge_power_kw=5.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert midnight_run.current_run_need_class == "required_energy"
    assert midnight_run.current_slot_end == datetime(
        2026, 8, 4, 1, 30, tzinfo=ZONE
    )

    # Missing or malformed allocation metadata fails closed to none; it can
    # never be silently interpreted as an economic origin.
    assert _classify_current_run_need(
        current_planned=False,
        current_run_slot_indices=(),
        allocation_provenance={},
    ) == "none"
    assert _classify_current_run_need(
        current_planned=True,
        current_run_slot_indices=(0,),
        allocation_provenance={},
    ) == "none"
    assert _classify_current_run_need(
        current_planned=True,
        current_run_slot_indices=(0,),
        allocation_provenance={0: 99},
    ) == "none"

    # Frozen pre-change hashes cover every existing result field. The new
    # bounded scalar is removed before hashing and is the sole schema delta.
    assert existing_result_digest(result) == (
        "f2624de8dbdcdae8afe9397708c78b8ccf4667bfb38de816e023038edcbd620b"
    )
    assert existing_result_digest(provenance_required) == (
        "b2dcbab49ce360888b4e52b075a62dd0e735164ab66266803cead7ae9e96abff"
    )
    assert existing_result_digest(material_support) == (
        "4be14bb629b2e3ba87904d37402dabd623bb55089f5aef105399485946eb25cd"
    )
    assert existing_result_digest(provenance_none) == (
        "c09feecb9602724237e107f12a770588a80e442bc8ddd954e63e318bb01b7806"
    )

    # Pure support fails closed when live powers are missing/stale, or when PV
    # already covers LOAD / the battery is not actually discharging. Required
    # battery charging is deliberately unaffected by this execution-only gate.
    missing_live = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=40),
            battery_soc_percent=100.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=micro_support_load,
            charge_power_kw=10.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert missing_live.current_action == "grid_support"
    assert not missing_live.current_run_start_eligible
    assert missing_live.current_run_suppression_reason == "live_data_missing"
    assert not missing_live.current_run_continue_eligible
    assert missing_live.current_run_continue_reason == "live_data_missing"

    # Once Grid Charge starts, successful support makes battery discharge fall
    # to zero. That expected control effect blocks a *new* start but must not
    # abort the already active run while LOAD still exceeds PV materially.
    active_support_effect = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=40),
            battery_soc_percent=100.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=micro_support_load,
            current_load_power_kw=5.0,
            current_pv_power_kw=0.0,
            current_battery_power_kw=0.0,
            charge_power_kw=10.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert active_support_effect.current_action == "grid_support"
    assert not active_support_effect.current_run_start_eligible
    assert (
        active_support_effect.current_run_suppression_reason
        == "battery_not_discharging"
    )
    assert active_support_effect.current_run_continue_eligible
    assert active_support_effect.current_run_continue_reason == "eligible"

    pv_nearly_covers = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=40),
            battery_soc_percent=100.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=micro_support_load,
            current_load_power_kw=5.0,
            current_pv_power_kw=4.9,
            current_battery_power_kw=0.1,
            charge_power_kw=10.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert pv_nearly_covers.current_action == "grid_support"
    assert not pv_nearly_covers.current_run_continue_eligible
    assert pv_nearly_covers.current_run_continue_reason == "pv_covers_load"
    pv_covers = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=40),
            battery_soc_percent=100.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=micro_support_load,
            current_load_power_kw=2.0,
            current_pv_power_kw=2.0,
            current_battery_power_kw=0.0,
            charge_power_kw=10.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert not pv_covers.current_slot_planned
    urgent_without_live = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=50),
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=peak_load,
            charge_power_kw=10.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert urgent_without_live.current_action == "battery_charge"
    assert urgent_without_live.current_run_start_eligible
    assert urgent_without_live.current_run_suppression_reason == "not_support_only"
    assert urgent_without_live.current_run_continue_eligible
    assert urgent_without_live.current_run_continue_reason == "not_support_only"

    # Remaining-run duration is elapsed time, not a naive wall-clock delta.
    # It therefore remains exact in both directions of the DST transition.
    spring_now = datetime(2026, 3, 29, 1, 30, tzinfo=ZONE)
    spring_run = optimize_tariff_charging(
        settings(
            spring_now,
            battery_capacity_kwh=40.0,
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh={
                datetime(2026, 3, 29, 7, 0, tzinfo=ZONE): 16.0,
            },
            charge_power_kw=5.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert spring_run.current_slot_planned
    assert spring_run.current_action == "battery_charge"
    assert spring_run.current_slot_end == datetime(
        2026, 3, 29, 6, 0, tzinfo=ZONE
    )
    assert abs(spring_run.current_run_duration_seconds - 210 * 60) < 1e-6
    assert len([
        item for item in spring_run.planned_charges
        if item.start.astimezone(ZoneInfo("UTC"))
        < spring_run.current_slot_end.astimezone(ZoneInfo("UTC"))
    ]) == 7
    autumn_now = datetime(2026, 10, 25, 1, 30, tzinfo=ZONE)
    autumn_run = optimize_tariff_charging(
        settings(
            autumn_now,
            battery_capacity_kwh=40.0,
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh={
                datetime(2026, 10, 25, 7, 0, tzinfo=ZONE): 26.0,
            },
            charge_power_kw=5.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert autumn_run.current_slot_planned
    assert autumn_run.current_action == "battery_charge"
    assert autumn_run.current_slot_end == datetime(
        2026, 10, 25, 6, 0, tzinfo=ZONE
    )
    assert abs(autumn_run.current_run_duration_seconds - 330 * 60) < 1e-6
    autumn_run_starts_utc = [
        item.start.astimezone(ZoneInfo("UTC"))
        for item in autumn_run.planned_charges
        if item.start.astimezone(ZoneInfo("UTC"))
        < autumn_run.current_slot_end.astimezone(ZoneInfo("UTC"))
    ]
    assert len(autumn_run_starts_utc) == 11
    assert all(
        later - earlier == timedelta(minutes=30)
        for earlier, later in zip(autumn_run_starts_utc, autumn_run_starts_utc[1:])
    )

    # The controller latches the end of the complete contiguous run selected
    # at cycle start.  A live replan may remove the current slot after SOC moves,
    # but it must not shorten this already accepted 14:00-15:00 charging window.
    active_contiguous_run = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=0),
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=peak_load,
            charge_power_kw=10.0,
            battery_charge_power_kw=20.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert active_contiguous_run.current_slot_planned
    assert active_contiguous_run.current_action == "battery_charge"
    assert active_contiguous_run.current_slot_end == pre_peak.replace(
        hour=15,
        minute=0,
    )

    # Live correction ignores tiny dawn samples, then progressively lowers
    # only today's forecast when a string fault causes sustained shortfall.
    dawn_factor = adaptive_forecast_factor(
        0.90,
        actual_energy_kwh=0.4,
        expected_elapsed_kwh=0.7,
        eligible=True,
    )
    assert dawn_factor == (0.90, None, 0.0)
    failed_string_factor, live_ratio, confidence = adaptive_forecast_factor(
        0.90,
        actual_energy_kwh=2.5,
        expected_elapsed_kwh=5.0,
        eligible=True,
    )
    assert live_ratio == 0.5
    assert 0.8 < confidence < 0.9
    assert 0.50 < failed_string_factor < 0.60

    # A 28-day weighted load estimate absorbs one abnormal day while recent
    # demand remains more important than old history.
    robust_load, load_uncertainty, load_days = robust_weighted_estimate(
        [18.0] * 13 + [120.0] + [20.0] * 14
    )
    assert load_days == 28
    assert robust_load is not None and 19.0 < robust_load < 21.0
    assert 0.0 < load_uncertainty < 0.25
    conservative_load, conservative_days = robust_weighted_upper_estimate(
        [20.0] * 24 + [22.0, 24.0, 38.0, 41.0]
    )
    assert conservative_days == 28
    assert conservative_load is not None and conservative_load >= 24.0

    # Day 3 is optional.  When available, winter PV is included before grid
    # energy is bought for that day's peak; without it the same load requires
    # a low-price charge on the preceding night.
    day_3_start = datetime(2026, 8, 5, 18, 0, tzinfo=ZONE)
    day_3_load = {day_3_start: 8.0}
    day_3_without_pv = optimize_tariff_charging(
        settings(
            monday,
            horizon_days=3,
            battery_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=day_3_load,
            charge_power_kw=10.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    day_3_with_pv = optimize_tariff_charging(
        settings(
            monday,
            horizon_days=3,
            battery_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=day_3_load,
            pv_by_slot_kwh={day_3_start: 8.0},
            charge_power_kw=10.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert day_3_without_pv.planned_grid_import_kwh > 0.0
    assert day_3_with_pv.planned_grid_import_kwh == 0.0
    assert day_3_with_pv.horizon_days == 3
    assert day_3_with_pv.horizon_end == datetime(2026, 8, 6, 0, 0, tzinfo=ZONE)
    assert day_3_with_pv.planning_horizon_hours >= 48.0
    assert abs(day_3_with_pv.modeled_load_kwh - 8.0) < 1e-6
    assert abs(day_3_with_pv.modeled_pv_kwh - 8.0) < 1e-6

    # Forecast uncertainty may retain a small terminal margin without adding
    # another user field.  It is restored in a low-price period and remains
    # bounded by the configured maximum SOC.
    terminal_margin = optimize_tariff_charging(
        settings(
            monday,
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            terminal_reserve_soc_percent=30.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            charge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert terminal_margin.planned_stored_energy_kwh >= 1.99
    assert terminal_margin.ending_battery_soc_percent >= 29.9
    assert terminal_margin.terminal_shortfall_kwh < 0.01

    # Live feedback can derate a nominal 10 kW command to the 5 kW actually
    # delivered.  Planning then reserves twice as many half-hour blocks rather
    # than discovering the shortfall in the last minutes of the cheap window.
    delivered_shortfall = optimize_tariff_charging(
        settings(
            pre_peak,
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=peak_load,
            charge_power_kw=5.0,
            requested_charge_power_kw=10.0,
            battery_charge_power_kw=20.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert delivered_shortfall.requested_charge_power_kw == 10.0
    assert delivered_shortfall.charge_power_kw == 5.0
    assert delivered_shortfall.effective_power_factor == 0.5
    assert len(delivered_shortfall.planned_charges) == 4

    # The horizon follows real elapsed half-hours across DST.  It neither
    # invents the missing spring hour nor drops the repeated autumn hour.
    spring_dst = optimize_tariff_charging(
        settings(
            datetime(2026, 3, 28, 23, 30, tzinfo=ZONE),
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    autumn_dst = optimize_tariff_charging(
        settings(
            datetime(2026, 10, 24, 23, 30, tzinfo=ZONE),
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    assert spring_dst.planning_slot_count == 47
    assert autumn_dst.planning_slot_count == 51

    # A fresh Day 3 forecast promises a *real* 48-hour horizon.  The spring
    # clock jump shortens three calendar days, so the end is extended to the
    # next complete half-hour instead of silently exposing a forecast gap.
    spring_day_3 = optimize_tariff_charging(
        settings(
            datetime(2026, 3, 28, 23, 30, tzinfo=ZONE),
            horizon_days=3,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    assert spring_day_3.planning_horizon_hours >= 48.0
    assert spring_day_3.planning_horizon_extended_to_minimum
    assert spring_day_3.horizon_end == datetime(2026, 3, 31, 0, 30, tzinfo=ZONE)

    # Without Day 3, the compatible calendar fallback remains shorter late in
    # the day.  Its explicit gap reserve assumes zero PV and retains average
    # home consumption for exactly the unmodelled tail.
    _, fallback_end, fallback_hours, fallback_extended = resolve_planning_horizon(
        datetime(2026, 8, 3, 23, 30, tzinfo=ZONE),
        2,
    )
    assert fallback_end == datetime(2026, 8, 5, 0, 0, tzinfo=ZONE)
    assert abs(fallback_hours - 24.5) < 1e-6
    assert not fallback_extended
    assert abs(
        horizon_gap_load_reserve_kwh(24.0, fallback_hours) - 23.5
    ) < 1e-6

    # The HA sensor must expose why a fallback was selected, the exact values
    # passed to the pure model and whether power feedback is still learning.
    tariff_sensor_source = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "tariff_sensor.py"
    ).read_text(encoding="utf-8")
    tariff_sensor_tree = ast.parse(tariff_sensor_source)
    for required_attribute in (
        '"planning_horizon_fallback_reason"',
        '"planning_horizon_gap_to_target_hours"',
        '"fallback_zero_pv_load_reserve_kwh"',
        '"model_input_forecast_day_3_kwh"',
        '"model_input_modeled_load_kwh"',
        '"model_input_effective_charge_power_kw"',
        '"charge_power_feedback_state"',
        '"charge_power_feedback_samples_remaining"',
        '"charge_power_feedback_applied_factor"',
        '"current_run_need_class"',
        '"current_run_start_eligible"',
        '"current_run_suppression_reason"',
        '"current_run_continue_eligible"',
        '"current_run_continue_reason"',
        '"current_run_intent_stable_seconds"',
        '"current_live_power_fresh"',
        '"expensive_window_load_buffers"',
        '"morning_protection_mode"',
        '"hard_reserve_deferral_source"',
        '"remaining_low_direct_import_kwh"',
        '"remaining_expensive_import_kwh"',
        '"capacity_or_power_shortfall_kwh"',
        '"control_inputs_fresh"',
        '"control_input_block_reason"',
        '"soc_data_fresh"',
        '"soc_age_seconds"',
        '"bms_charge_data_fresh"',
        '"bms_charge_age_seconds"',
        '"bms_charge_available"',
        '"bms_discharge_data_fresh"',
        '"bms_discharge_age_seconds"',
        '"bms_discharge_available"',
        '"load_profile_data_fresh"',
        '"load_profile_broker_fresh"',
        '"load_profile_age_seconds"',
        '"live_power_data_fresh"',
        '"forecast_day_3_configured_entity"',
        '"forecast_day_3_source_available"',
        '"forecast_day_3_data_fresh"',
        '"forecast_day_3_data_complete"',
        '"forecast_day_3_age_seconds"',
        '"forecast_day_3_data_reason"',
        '"forecast_today_data_reason"',
        '"forecast_tomorrow_data_reason"',
        '"forecast_remaining_today_data_reason"',
        "from .energy_data import numeric_state_sample, state_age_seconds",
        "return state_age_seconds(state, now or dt_util.utcnow())",
        "sample = numeric_state_sample(",
    ):
        assert required_attribute in tariff_sensor_source
    pending_method = tariff_sensor_source.split(
        "def _mark_recalculation_pending", 1
    )[1].split("def _mark_result_current", 1)[0]
    current_method = tariff_sensor_source.split(
        "def _mark_result_current", 1
    )[1].split("async def", 1)[0]
    assert "**self._attributes" in pending_method
    assert "**self._attributes" in current_method
    assert '"current_run_need_class"' not in pending_method
    assert '"current_run_need_class"' not in current_method
    assert "if bms_charge_data_fresh\n            else 0.0" in tariff_sensor_source
    assert "if bms_discharge_data_fresh\n            else 0.0" in tariff_sensor_source
    assert "battery_charge_power_kw=bms_power_kw" in tariff_sensor_source
    assert "battery_discharge_power_kw=bms_discharge_power_kw" in tariff_sensor_source
    assert "pv_charge_power_kw=bms_power_kw" in tariff_sensor_source
    assert "else system_power_kw" not in tariff_sensor_source
    assert "efficiency_value is None" in tariff_sensor_source
    assert "max_age_seconds=max_age_seconds" in tariff_sensor_source
    assert "rce_state_raw is None" in tariff_sensor_source
    assert "and load_profile_broker_fresh" in tariff_sensor_source
    for marker in (
        "FORECAST_ENTITY_HELPERS",
        "def _configured_forecast_source_ids(",
        "def _refresh_dynamic_forecast_listener(",
        "_configured_forecast_entity_ids(self.hass)",
        'if event.data["entity_id"] in FORECAST_ENTITY_HELPERS:',
        "day_3_forecast_sample = numeric_state_sample(",
        "max_age_seconds=FORECAST_MAX_AGE_SECONDS",
        'if day_3_status != "fresh":',
    ):
        assert marker in tariff_sensor_source
    assert 'required["Solcast Forecast Day 3"]' not in tariff_sensor_source

    # Published Supervisor provenance is an output, never a consumed planner
    # input. Execute the real fingerprint method body against two otherwise
    # identical fixtures whose only difference is the published scalar.
    fingerprint_method = next(
        node
        for node in ast.walk(tariff_sensor_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_current_input_fingerprint"
    )
    fingerprint_segment = ast.get_source_segment(
        tariff_sensor_source,
        fingerprint_method,
    )
    assert fingerprint_segment is not None
    assert "current_run_need_class" not in fingerprint_segment
    assert "self._attributes" not in fingerprint_segment
    fingerprint_method.decorator_list = []
    fingerprint_module = ast.fix_missing_locations(
        ast.Module(body=[fingerprint_method], type_ignores=[])
    )
    fingerprint_namespace = {
        "WATCHED_TARIFF_ENTITIES": {"sensor.input"},
        "RCE_LOAD_BROKER_ATTRIBUTES": ("bounded_attribute",),
        "optimizer_input_fingerprint": lambda hass, entities, **kwargs: (
            hass,
            tuple(sorted(entities)),
            kwargs,
        ),
    }
    exec(
        compile(fingerprint_module, "<tariff-fingerprint-contract>", "exec"),
        fingerprint_namespace,
    )

    class FingerprintFixture:
        hass = "same-hass-state"

        def __init__(self, published_need: str) -> None:
            self._attributes = {"current_run_need_class": published_need}

        @staticmethod
        def _configured_forecast_source_ids() -> frozenset[str]:
            return frozenset({"sensor.forecast"})

    fingerprint_function = fingerprint_namespace[
        "_current_input_fingerprint"
    ]
    assert fingerprint_function(
        FingerprintFixture("required_energy")
    ) == fingerprint_function(FingerprintFixture("economic"))

    watched_assignment = next(
        node
        for node in tariff_sensor_tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name)
            and target.id == "WATCHED_TARIFF_ENTITIES"
            for target in (
                node.targets if isinstance(node, ast.Assign) else (node.target,)
            )
        )
    )
    watched_segment = ast.get_source_segment(
        tariff_sensor_source,
        watched_assignment,
    )
    assert watched_segment is not None
    assert "sensor.hoymiles_hit_tariff_charge_plan" not in watched_segment
    assert 'return "hoymiles_hit_tariff_charge_plan"' in tariff_sensor_source
    assert '"current_run_need_class": result.current_run_need_class' in (
        tariff_sensor_source
    )

    # The reserve is dynamic.  With Self-Use 25% and a 2-point correction the
    # optimizer restores 27%; changing the user threshold changes the target.
    cheap_now = monday.replace(hour=22, minute=0)
    dynamic_reserve = optimize_tariff_charging(
        settings(
            cheap_now,
            battery_soc_percent=25.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    assert dynamic_reserve.status_code == "ready"
    assert dynamic_reserve.planned_stored_energy_kwh >= 0.39
    assert dynamic_reserve.target_soc_percent >= 27.0 - 0.1
    assert not dynamic_reserve.current_slot_planned
    assert dynamic_reserve.planned_charges[0].start.strftime("%H:%M") == "05:30"
    user_changed_reserve = optimize_tariff_charging(
        settings(
            cheap_now,
            battery_soc_percent=30.0,
            base_reserve_soc_percent=35.0,
            reserve_soc_percent=37.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    assert user_changed_reserve.planned_stored_energy_kwh >= 1.39
    assert user_changed_reserve.target_soc_percent >= 35.0 - 0.1
    assert user_changed_reserve.ending_battery_soc_percent >= 37.0 - 0.1

    # G12w is low-price throughout Sunday.  A small reserve gap must not
    # start Grid Charge in the morning; it is restored once, immediately
    # before Monday's first expensive slot.
    sunday_morning = datetime(2026, 8, 9, 6, 13, tzinfo=ZONE)
    weekend_reserve = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_soc_percent=25.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    assert weekend_reserve.status_code == "ready"
    assert not weekend_reserve.current_slot_planned
    assert weekend_reserve.current_action == "none"
    assert weekend_reserve.planned_charges[0].start == datetime(
        2026, 8, 10, 5, 30, tzinfo=ZONE
    )
    assert weekend_reserve.planned_charges[-1].start < datetime(
        2026, 8, 10, 6, 0, tzinfo=ZONE
    )

    # The user's Self-Use floor is a hard reserve, while the automatic +2 pp
    # safety margin remains JIT. On an all-low weekend, falling below 25%
    # restores the hard floor now instead of waiting until Monday morning.
    weekend_below_hard_reserve = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_soc_percent=20.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    assert weekend_below_hard_reserve.current_slot_planned
    assert weekend_below_hard_reserve.current_action == "battery_charge"
    assert weekend_below_hard_reserve.current_run_start_eligible
    assert weekend_below_hard_reserve.base_reserve_soc_percent == 25.0
    assert weekend_below_hard_reserve.hard_reserve_deficit_kwh >= 0.99
    assert weekend_below_hard_reserve.hard_reserve_restoration_required

    # A current low-price block may remain visible as a diagnostic plan, but
    # stale SOC/BMS/LOAD inputs must make the pure execution contract fail
    # closed.  The reason is preserved verbatim for the HA scheduler.
    for stale_reason in (
        "soc_data_stale",
        "bms_charge_data_stale",
        "bms_discharge_data_stale",
        "load_profile_data_stale",
    ):
        stale_current_slot = optimize_tariff_charging(
            settings(
                sunday_morning,
                schedule=schedule("G12w"),
                battery_soc_percent=20.0,
                base_reserve_soc_percent=25.0,
                reserve_soc_percent=27.0,
                average_daily_load_kwh=0.0,
                average_night_load_kwh=0.0,
                control_inputs_fresh=False,
                control_input_block_reason=stale_reason,
            )
        )
        assert stale_current_slot.current_slot_planned
        assert stale_current_slot.current_action == "battery_charge"
        assert not stale_current_slot.current_run_start_eligible
        assert not stale_current_slot.current_run_continue_eligible
        assert stale_current_slot.current_run_suppression_reason == stale_reason
        assert stale_current_slot.current_run_continue_reason == stale_reason
        assert not stale_current_slot.control_inputs_fresh
        assert stale_current_slot.control_input_block_reason == stale_reason

    # Fresh exact-zero BMS limits are valid data, not a missing value and not
    # unlimited power. No battery energy may be scheduled through either path.
    exact_zero_bms = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_soc_percent=20.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            battery_charge_power_kw=0.0,
            battery_discharge_power_kw=0.0,
            pv_charge_power_kw=0.0,
            control_inputs_fresh=True,
            control_input_block_reason="none",
        )
    )
    assert exact_zero_bms.control_inputs_fresh
    assert exact_zero_bms.planned_stored_energy_kwh == 0.0
    assert exact_zero_bms.planned_grid_import_kwh == 0.0
    assert not exact_zero_bms.current_run_start_eligible
    assert exact_zero_bms.hard_reserve_unavailable

    zero_charge_bms_support = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=50),
            battery_soc_percent=100.0,
            reserve_soc_percent=20.0,
            maximum_soc_percent=100.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=micro_support_load,
            current_load_power_kw=5.0,
            current_pv_power_kw=0.0,
            current_battery_power_kw=5.0,
            charge_power_kw=10.0,
            battery_charge_power_kw=0.0,
            battery_discharge_power_kw=10.0,
            pv_charge_power_kw=0.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
            control_inputs_fresh=True,
        )
    )
    assert zero_charge_bms_support.current_action == "grid_support"
    assert zero_charge_bms_support.current_run_start_eligible
    assert zero_charge_bms_support.planned_stored_energy_kwh == 0.0

    # Live powers are action-specific rather than part of the core freshness
    # verdict. Required reserve charging may start from fresh SOC/BMS/profile
    # plus the deterministic forecast, while pure Grid Support still needs
    # live LOAD, PV and battery-flow evidence.
    required_without_live = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_soc_percent=20.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            current_load_power_kw=None,
            current_pv_power_kw=None,
            current_battery_power_kw=None,
            control_inputs_fresh=True,
            charge_power_kw=10.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert required_without_live.current_action == "battery_charge"
    assert required_without_live.current_run_start_eligible

    support_without_live = optimize_tariff_charging(
        settings(
            pre_peak.replace(hour=14, minute=50, second=15),
            battery_soc_percent=100.0,
            reserve_soc_percent=20.0,
            maximum_soc_percent=100.0,
            average_daily_load_kwh=20.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh=micro_support_load,
            current_load_power_kw=None,
            current_pv_power_kw=None,
            current_battery_power_kw=None,
            control_inputs_fresh=True,
            charge_power_kw=10.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert support_without_live.current_action == "grid_support"
    assert not support_without_live.current_run_start_eligible
    assert support_without_live.current_run_suppression_reason == "live_data_missing"

    # Forecast PV alone may no longer defer restoration of the hard floor.
    # The inverter must first observe a fresh, stable PV>LOAD surplus.
    weekend_hard_reserve_with_pv = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_soc_percent=20.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            pv_by_slot_kwh={
                datetime(2026, 8, 9, 7, 0, tzinfo=ZONE): 2.0,
            },
        )
    )
    assert weekend_hard_reserve_with_pv.current_slot_planned
    assert not weekend_hard_reserve_with_pv.hard_reserve_restored_by_near_term_pv
    assert (
        weekend_hard_reserve_with_pv.hard_reserve_deferral_source
        == "no_live_pv_surplus"
    )
    # A future PV forecast before the next low window cannot silently satisfy
    # the hard floor. At 07:00 the plan must still reserve Grid Charge at 13:00;
    # if PV really restores SOC, a later live replan will remove that block.
    monday_morning_below_hard_reserve = optimize_tariff_charging(
        settings(
            datetime(2026, 8, 3, 7, 0, tzinfo=ZONE),
            battery_soc_percent=20.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            pv_by_slot_kwh={
                datetime(2026, 8, 3, 8, 0, tzinfo=ZONE): 2.0,
            },
            current_load_power_kw=0.4,
            current_pv_power_kw=0.0,
            current_battery_power_kw=0.0,
        )
    )
    assert monday_morning_below_hard_reserve.hard_reserve_restoration_required
    assert not monday_morning_below_hard_reserve.hard_reserve_unavailable
    assert monday_morning_below_hard_reserve.planned_charges
    assert monday_morning_below_hard_reserve.planned_charges[0].start == (
        datetime(2026, 8, 3, 13, 0, tzinfo=ZONE)
    )
    assert monday_morning_below_hard_reserve.planned_stored_energy_kwh >= 1.0
    assert monday_morning_below_hard_reserve.modeled_pv_kwh == 0.0
    weekend_hard_reserve_with_stable_live_pv = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_soc_percent=20.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            current_load_power_kw=0.4,
            current_pv_power_kw=1.2,
            current_battery_power_kw=-0.8,
            live_pv_surplus_stable=True,
            live_pv_surplus_stable_seconds=300.0,
        )
    )
    assert not weekend_hard_reserve_with_stable_live_pv.current_slot_planned
    assert (
        weekend_hard_reserve_with_stable_live_pv
        .hard_reserve_restored_by_near_term_pv
    )
    assert (
        weekend_hard_reserve_with_stable_live_pv.hard_reserve_deferral_source
        == "stable_live_pv_surplus"
    )
    tapered_real_charge_cannot_defer = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_soc_percent=20.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            current_load_power_kw=0.4,
            current_pv_power_kw=5.4,
            current_battery_power_kw=-0.25,
            pv_charge_power_kw=5.0,
            battery_charge_power_kw=5.0,
            live_pv_surplus_stable=True,
            live_pv_surplus_stable_seconds=300.0,
        )
    )
    assert not tapered_real_charge_cannot_defer.hard_reserve_restored_by_near_term_pv
    assert tapered_real_charge_cannot_defer.hard_reserve_restoration_required
    assert tapered_real_charge_cannot_defer.hard_reserve_deferral_source == (
        "live_surplus_not_stable"
    )
    export_only_cannot_defer_hard_reserve = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_soc_percent=20.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            current_load_power_kw=0.4,
            current_pv_power_kw=1.2,
            current_battery_power_kw=0.0,
            pv_charge_power_kw=0.0,
            battery_charge_power_kw=0.0,
            live_pv_surplus_stable=True,
            live_pv_surplus_stable_seconds=300.0,
        )
    )
    assert not export_only_cannot_defer_hard_reserve.current_slot_planned
    assert not (
        export_only_cannot_defer_hard_reserve
        .hard_reserve_restored_by_near_term_pv
    )
    assert export_only_cannot_defer_hard_reserve.hard_reserve_restoration_required
    assert export_only_cannot_defer_hard_reserve.hard_reserve_unavailable
    assert export_only_cannot_defer_hard_reserve.status_code == (
        "hard_reserve_unavailable"
    )
    assert (
        export_only_cannot_defer_hard_reserve.capacity_or_power_shortfall_kwh
        >= export_only_cannot_defer_hard_reserve.hard_reserve_deficit_kwh
    )
    assert (
        export_only_cannot_defer_hard_reserve.hard_reserve_shortfall_kwh
        >= export_only_cannot_defer_hard_reserve.hard_reserve_deficit_kwh
    )

    # A very low BMS charge limit cannot restore the complete Self-Use floor,
    # but the feasible partial charge must still execute.  The status remains
    # actionable for the scheduler while diagnostics expose the residual gap.
    partial_hard_reserve_restore = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_capacity_kwh=20.0,
            battery_soc_percent=0.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            charge_power_kw=5.0,
            pv_charge_power_kw=0.1,
            battery_charge_power_kw=0.1,
        )
    )
    assert partial_hard_reserve_restore.current_slot_planned
    assert partial_hard_reserve_restore.current_action == "battery_charge"
    assert 0.0 < partial_hard_reserve_restore.planned_stored_energy_kwh
    assert (
        partial_hard_reserve_restore.planned_stored_energy_kwh
        < partial_hard_reserve_restore.hard_reserve_deficit_kwh
    )
    assert partial_hard_reserve_restore.hard_reserve_unavailable
    assert partial_hard_reserve_restore.hard_reserve_shortfall_kwh > 0.0
    assert partial_hard_reserve_restore.status_code == "insufficient_cheap_window"

    # Missing Day 3 assumes zero PV, but only until the next cheap opportunity
    # in the unmodelled tail. It must not turn every winter fallback into a
    # permanent 100% terminal-SOC request.
    gap_reserve, gap_hours = horizon_gap_expensive_load_reserve_kwh(
        36.0,
        datetime(2026, 1, 6, 0, 0, tzinfo=ZONE),
        22.0,
        schedule("G12w"),
    )
    assert gap_hours == 0.0  # statutory holiday: the unseen tail is all low
    assert gap_reserve == 0.0
    weekday_gap_reserve, weekday_gap_hours = (
        horizon_gap_expensive_load_reserve_kwh(
            36.0,
            datetime(2026, 1, 7, 6, 0, tzinfo=ZONE),
            18.0,
            schedule("G12w"),
        )
    )
    assert weekday_gap_hours == 14.0
    assert abs(weekday_gap_reserve - 10.5) < 1e-6
    cold_bms_gap_reserve, cold_bms_expensive_hours = (
        horizon_gap_expensive_load_reserve_kwh(
            55.0,
            datetime(2026, 1, 8, 0, 0, tzinfo=ZONE),
            12.0,
            schedule("G12"),
            charge_power_kw=5.0,
            battery_charge_power_kw=2.0,
            charge_efficiency_percent=95.0,
            discharge_efficiency_percent=95.0,
            maximum_stored_energy_kwh=20.0,
        )
    )
    assert cold_bms_expensive_hours == 6.0
    # The 00:00-06:00 low window can store only 12 kWh through the 2 kW BMS,
    # while the zero-PV 06:00-12:00 peak needs about 14.47 kWh DC. Preserve
    # the missing part before the modeled horizon ends.
    assert 2.4 < cold_bms_gap_reserve < 2.6

    unlimited_bms_gap_reserve, _ = horizon_gap_expensive_load_reserve_kwh(
        55.0,
        datetime(2026, 1, 8, 0, 0, tzinfo=ZONE),
        12.0,
        schedule("G12"),
        charge_power_kw=5.0,
        battery_charge_power_kw=20.0,
        charge_efficiency_percent=95.0,
        discharge_efficiency_percent=95.0,
        maximum_stored_energy_kwh=20.0,
    )
    assert unlimited_bms_gap_reserve == 0.0

    # The spring clock change has only five real low-price hours between local
    # midnight and 06:00. UTC stepping must expose three, not two, expensive
    # hours inside an eight-hour absolute tail.
    _, dst_expensive_hours = horizon_gap_expensive_load_reserve_kwh(
        24.0,
        datetime(2026, 3, 29, 0, 0, tzinfo=ZONE),
        8.0,
        schedule("G12"),
        charge_power_kw=5.0,
        battery_charge_power_kw=5.0,
        maximum_stored_energy_kwh=20.0,
    )
    assert dst_expensive_hours == 3.0

    # SOC between the base floor and the safety-corrected reserve is not an
    # emergency. Only the extra margin is restored JIT before the first peak.
    weekend_inside_safety_margin = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_soc_percent=26.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    assert not weekend_inside_safety_margin.current_slot_planned
    assert weekend_inside_safety_margin.planned_charges[0].start == datetime(
        2026, 8, 10, 5, 30, tzinfo=ZONE
    )

    # A user maximum below the hard Self-Use floor is contradictory and must
    # fail closed instead of silently overriding either setting.
    invalid_soc_limits = optimize_tariff_charging(
        settings(
            sunday_morning,
            schedule=schedule("G12w"),
            battery_soc_percent=20.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            # Above the 25% base but below the 27% composite reserve: the
            # safety correction is still a real floor and must not be silently
            # exceeded by `_simulate`.
            maximum_soc_percent=26.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
        )
    )
    assert invalid_soc_limits.status_code == "soc_limits_conflict"
    assert invalid_soc_limits.planned_grid_import_kwh == 0.0
    assert not invalid_soc_limits.current_run_start_eligible

    # Do not buy energy only because SOC is initially below the dynamic floor
    # when forecast PV will rebuild the reserve before the next cheap window.
    pv_restores_reserve = optimize_tariff_charging(
        settings(
            datetime(2026, 8, 4, 6, 0, tzinfo=ZONE),
            battery_soc_percent=25.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            pv_by_slot_kwh={
                datetime(2026, 8, 4, 8, 0, tzinfo=ZONE): 1.0,
            },
        )
    )
    assert pv_restores_reserve.status_code == "no_charge_needed"
    assert pv_restores_reserve.planned_grid_import_kwh == 0.0

    # Abundant PV removes the need to charge from the grid.
    sunny = {
        monday.replace(day=4, hour=hour, minute=0): 5.0
        for hour in range(8, 17)
    }
    sunny_result = optimize_tariff_charging(
        settings(
            monday,
            battery_soc_percent=70.0,
            pv_by_slot_kwh=sunny,
            average_daily_load_kwh=8.0,
            average_night_load_kwh=3.0,
        )
    )
    assert sunny_result.status_code == "no_charge_needed"
    assert sunny_result.planned_grid_import_kwh == 0

    # If the battery lasts through most of the cheap night, buy only the small
    # amount that genuinely lowers the full future tariff bill.
    morning_pv = {
        monday.replace(day=4, hour=hour, minute=minute): 5.0
        for hour in range(7, 24)
        for minute in (0, 30)
    }
    late_bridge = optimize_tariff_charging(
        settings(
            monday.replace(minute=0),
            battery_soc_percent=50.0,
            pv_by_slot_kwh=morning_pv,
            average_daily_load_kwh=20.0,
            average_night_load_kwh=8.0,
        )
    )
    assert late_bridge.planned_grid_import_kwh > 0
    assert late_bridge.automation_savings_pln > 0
    assert not any(
        item.start.hour == 22 and item.start.date() == monday.date()
        for item in late_bridge.planned_charges
    )

    # Winter: very low PV and high demand require both direct low-tariff home
    # supply and battery charging for the following expensive periods.
    winter = optimize_tariff_charging(
        settings(
            monday,
            battery_soc_percent=20.0,
            pv_by_slot_kwh={},
            average_daily_load_kwh=30.0,
            average_night_load_kwh=12.0,
            charge_power_kw=8.0,
        )
    )
    assert winter.planned_stored_energy_kwh > 0
    assert any(
        item.action in {"battery_charge", "grid_support_and_charge"}
        for item in winter.planned_charges
    )
    assert all(item.zone == "low" for item in winter.planned_charges)
    assert winter.ending_battery_soc_percent >= 20.0 - 0.1
    assert winter.optimized_grid_cost_pln < winter.baseline_grid_cost_pln

    # Local HIT-10 winter fixture: 21 kWh storage, 31 kWh/day heat-pump LOAD
    # and a shared 5 kW Grid Charge budget. The current block must respect the
    # shared AC and BMS limits and preserve the 27% composite reserve.
    local_winter_fixture = optimize_tariff_charging(
        settings(
            monday.replace(hour=22, minute=0),
            battery_capacity_kwh=21.0,
            battery_soc_percent=27.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            maximum_soc_percent=100.0,
            average_daily_load_kwh=31.0,
            average_night_load_kwh=13.9,
            charge_power_kw=5.0,
            requested_charge_power_kw=5.0,
            battery_charge_power_kw=5.0,
            battery_discharge_power_kw=5.0,
            pv_charge_power_kw=5.0,
            pv_by_slot_kwh={},
        )
    )
    assert local_winter_fixture.planned_charges
    assert local_winter_fixture.next_charge_start is not None
    assert all(
        item.grid_import_kwh <= 2.5 + 1e-6
        and item.stored_energy_kwh <= 2.5 * 0.95 + 1e-6
        for item in local_winter_fixture.planned_charges
    )
    assert local_winter_fixture.ending_battery_soc_percent >= 27.0 - 0.1

    # Field-scale miernik.com.pl fixture: 230 kWh at 58% SOC, 37.4 kWh/day
    # LOAD and 167 kWh of next-day PV. It must not manufacture a reason to
    # fill a large already-protected store to 100%.
    meter_now = monday.replace(hour=21, minute=0)
    meter_pv = {
        meter_now.replace(day=4, hour=hour, minute=minute): 167.0 / 16.0
        for hour in range(8, 16)
        for minute in (0, 30)
    }
    meter_fixture = optimize_tariff_charging(
        settings(
            meter_now,
            battery_capacity_kwh=230.0,
            battery_soc_percent=58.0,
            base_reserve_soc_percent=25.0,
            reserve_soc_percent=27.0,
            maximum_soc_percent=100.0,
            average_daily_load_kwh=37.4,
            average_night_load_kwh=11.4,
            charge_power_kw=20.0,
            requested_charge_power_kw=20.0,
            battery_charge_power_kw=20.0,
            battery_discharge_power_kw=30.0,
            pv_charge_power_kw=20.0,
            pv_by_slot_kwh=meter_pv,
        )
    )
    assert meter_fixture.status_code == "no_charge_needed"
    assert meter_fixture.planned_grid_import_kwh == 0.0
    assert meter_fixture.planned_stored_energy_kwh == 0.0
    assert meter_fixture.target_soc_percent <= 58.0 + 0.1
    assert not meter_fixture.current_slot_planned

    # A sudden heat-pump cold spell is represented by a recorder-derived P90
    # daily LOAD. The multiplier is applied independently to every future
    # expensive window and causes an earlier/larger overnight purchase without
    # altering all-low G12w periods.
    winter_night = datetime(2026, 8, 3, 22, 0, tzinfo=ZONE)
    winter_morning_load = {
        datetime(2026, 8, 4, hour, minute, tzinfo=ZONE): 0.9
        for hour in range(6, 13)
        for minute in (0, 30)
    }
    winter_expected = optimize_tariff_charging(
        settings(
            winter_night,
            battery_soc_percent=55.0,
            average_daily_load_kwh=30.0,
            average_night_load_kwh=12.0,
            load_by_slot_kwh=winter_morning_load,
            charge_power_kw=5.0,
        )
    )
    winter_p90 = optimize_tariff_charging(
        settings(
            winter_night,
            battery_soc_percent=55.0,
            average_daily_load_kwh=30.0,
            average_night_load_kwh=12.0,
            load_by_slot_kwh=winter_morning_load,
            charge_power_kw=5.0,
            conservative_daily_load_kwh=40.5,
            load_uncertainty_ratio=0.24,
            load_history_days=28,
        )
    )
    assert winter_p90.load_risk_multiplier == 1.35
    assert winter_p90.load_risk_buffer_kwh > 0.0
    assert winter_p90.expensive_window_load_buffers
    assert all(
        item.conservative_load_kwh + 1e-6 >= item.expected_load_kwh
        for item in winter_p90.expensive_window_load_buffers
    )
    assert (
        winter_p90.planned_stored_energy_kwh
        > winter_expected.planned_stored_energy_kwh + 0.5
    )

    # Before the overnight low window closes, protect the first morning peak
    # with the slot-level P10 scenario. A 20 kWh optimistic forecast must not
    # strand the home at the Self-Use floor if P10 says the morning may be dark.
    optimistic_morning_pv = {
        datetime(2026, 8, 4, hour, minute, tzinfo=ZONE): 0.8
        for hour in range(6, 13)
        for minute in (0, 30)
    }
    p10_morning_pv = {
        start: 0.05 for start in optimistic_morning_pv
    }
    expected_only = optimize_tariff_charging(
        settings(
            winter_night,
            battery_soc_percent=35.0,
            load_by_slot_kwh=winter_morning_load,
            pv_by_slot_kwh=optimistic_morning_pv,
            charge_power_kw=5.0,
        )
    )
    protected_morning = optimize_tariff_charging(
        settings(
            winter_night,
            battery_soc_percent=35.0,
            load_by_slot_kwh=winter_morning_load,
            pv_by_slot_kwh=optimistic_morning_pv,
            pv_p10_by_slot_kwh=p10_morning_pv,
            pv_p10_available_dates=(datetime(2026, 8, 4).date(),),
            charge_power_kw=5.0,
        )
    )
    assert protected_morning.morning_protection_active
    assert protected_morning.morning_protection_mode == "solcast_p10"
    assert (
        protected_morning.morning_protection_conservative_pv_kwh
        < protected_morning.morning_protection_expected_pv_kwh
    )
    assert (
        protected_morning.planned_stored_energy_kwh
        > expected_only.planned_stored_energy_kwh + 0.5
    )

    zero_pv_guard = optimize_tariff_charging(
        settings(
            winter_night,
            battery_soc_percent=35.0,
            load_by_slot_kwh=winter_morning_load,
            pv_by_slot_kwh=optimistic_morning_pv,
            forecast_uncertainty_ratio=0.25,
            charge_power_kw=5.0,
        )
    )
    assert zero_pv_guard.morning_protection_active
    assert zero_pv_guard.morning_protection_mode == "zero_pv_high_variability"
    assert zero_pv_guard.morning_protection_conservative_pv_kwh == 0.0

    # A cheap period after the first energy deficit cannot repair the past, but
    # it must still protect later expensive hours instead of cancelling the
    # remainder of the plan.
    before_peak = datetime(2026, 8, 3, 6, 10, tzinfo=ZONE)
    constrained = optimize_tariff_charging(
        settings(
            before_peak,
            battery_soc_percent=20.0,
            average_daily_load_kwh=30.0,
            average_night_load_kwh=10.0,
        )
    )
    assert constrained.remaining_shortage_kwh > 0
    assert any(
        13 <= item.start.hour < 15 for item in constrained.planned_charges
        if item.start > before_peak.replace(hour=7, minute=0)
    )

    # A deficit that exists only during the currently cheap period should be
    # bought directly for the home. Charging the battery first would add
    # conversion losses without avoiding any higher-priced import.
    cheap_direct = optimize_tariff_charging(
        settings(
            monday.replace(hour=22, minute=0),
            battery_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh={
                monday.replace(hour=22, minute=0): 2.0,
            },
        )
    )
    assert cheap_direct.baseline_shortage_kwh > 1.9
    assert cheap_direct.planned_grid_import_kwh == 0
    assert cheap_direct.status_code == "shortage_in_low_period"

    # G11 is a reference tariff; it must never create a fake saving.
    g11 = optimize_tariff_charging(
        settings(monday, schedule=schedule("G11"))
    )
    assert g11.status_code == "no_discount_window"
    assert g11.planned_grid_import_kwh == 0

    g11_below_reserve = optimize_tariff_charging(
        settings(
            monday,
            schedule=schedule("G11"),
            battery_soc_percent=15.0,
            reserve_soc_percent=20.0,
            terminal_reserve_soc_percent=50.0,
        )
    )
    assert g11_below_reserve.terminal_reserve_soc_percent == 50.0
    assert g11_below_reserve.effective_terminal_reserve_soc_percent == 15.0
    assert g11_below_reserve.planned_grid_import_kwh == 0.0

    # A nominally lower zone is rejected if conversion losses and the user's
    # minimum margin make shifting energy more expensive than buying it later.
    narrow_spread = TariffSchedule(
        tariff_type="G12",
        g11_price_pln_kwh=0.85,
        low_price_pln_kwh=0.80,
        medium_price_pln_kwh=0.82,
        peak_price_pln_kwh=0.84,
        cheap_windows=((22 * 60, 6 * 60),),
    )
    narrow = optimize_tariff_charging(
        settings(monday, schedule=narrow_spread, minimum_saving_pln_kwh=0.05)
    )
    assert narrow.planned_grid_import_kwh == 0
    assert narrow.automation_savings_pln == 0
    assert narrow.status_code == "not_economically_beneficial"
    assert narrow.capacity_or_power_shortfall_kwh == 0.0

    # Even at perfect conversion efficiency, a four-grosz spread is smaller
    # than the automatic six-grosz battery-throughput allowance.  The home is
    # supplied directly later instead of consuming a cycle for a paper gain.
    wear_limited = optimize_tariff_charging(
        settings(
            monday,
            schedule=narrow_spread,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
            battery_wear_cost_pln_kwh=0.06,
        )
    )
    assert wear_limited.planned_grid_import_kwh == 0.0
    assert wear_limited.planned_battery_wear_cost_pln == 0.0
    assert wear_limited.status_code == "not_economically_beneficial"
    assert wear_limited.capacity_or_power_shortfall_kwh == 0.0

    # A real spread must be exploited even when both prices are below the G11
    # reference; decisions use the future avoided cost, not a fixed G11 gate.
    real_spread = TariffSchedule(
        tariff_type="G12",
        g11_price_pln_kwh=1.30,
        low_price_pln_kwh=0.45,
        medium_price_pln_kwh=0.85,
        peak_price_pln_kwh=1.05,
        cheap_windows=((22 * 60, 6 * 60),),
    )
    shifted = optimize_tariff_charging(
        settings(monday, schedule=real_spread)
    )
    assert shifted.planned_grid_import_kwh > 0
    assert shifted.automation_savings_pln > 0
    assert shifted.optimized_grid_cost_pln < shifted.baseline_grid_cost_pln

    # A learned 30-minute profile is accepted directly, without adding any
    # user-facing configuration fields.
    profiled_load = {
        monday.replace(hour=22, minute=0): 1.5,
        monday.replace(hour=22, minute=30): 1.5,
    }
    profiled = optimize_tariff_charging(
        settings(
            monday,
            schedule=real_spread,
            load_by_slot_kwh=profiled_load,
            battery_soc_percent=20.0,
        )
    )
    assert profiled.baseline_shortage_kwh > 2.9

    saturday = datetime(2026, 8, 8, 10, 0, tzinfo=ZONE)
    assert tariff_rate(saturday, schedule("G12w"))[1] == "low"
    assert tariff_rate(saturday, schedule("G12"))[1] == "peak"

    # G13 medium prices are modeled for avoided-cost decisions, but Grid
    # Charge may execute only in an explicitly low window. Even when the one
    # low half-hour cannot cover the complete later peak deficit, the optimizer
    # must not fill the gap by scheduling a 12:00-13:00 medium block.
    g13_low_only = TariffSchedule(
        tariff_type="G13",
        g11_price_pln_kwh=0.95,
        low_price_pln_kwh=0.45,
        medium_price_pln_kwh=0.65,
        peak_price_pln_kwh=1.25,
        cheap_windows=((13 * 60, 13 * 60 + 30),),
        medium_windows=((12 * 60, 13 * 60),),
    )
    g13_now = datetime(2026, 8, 3, 12, 0, tzinfo=ZONE)
    g13_result = optimize_tariff_charging(
        settings(
            g13_now,
            schedule=g13_low_only,
            battery_soc_percent=20.0,
            reserve_soc_percent=20.0,
            average_daily_load_kwh=0.0,
            average_night_load_kwh=0.0,
            load_by_slot_kwh={
                datetime(2026, 8, 3, 14, 0, tzinfo=ZONE): 8.0,
            },
            charge_power_kw=5.0,
            charge_efficiency_percent=100.0,
            discharge_efficiency_percent=100.0,
            minimum_saving_pln_kwh=0.0,
        )
    )
    assert g13_result.planned_charges
    assert all(item.zone == "low" for item in g13_result.planned_charges)
    assert g13_result.planned_charges[0].start == datetime(
        2026, 8, 3, 13, 0, tzinfo=ZONE
    )
    assert not any(
        item.zone == "medium" for item in g13_result.planned_charges
    )
    assert g13_result.remaining_shortage_kwh > 5.0

    assert is_polish_public_holiday(datetime(2026, 12, 24).date())
    assert is_polish_public_holiday(datetime(2026, 4, 6).date())
    holiday_only = TariffSchedule(
        tariff_type="G12w",
        g11_price_pln_kwh=0.85,
        low_price_pln_kwh=0.62,
        medium_price_pln_kwh=0.82,
        peak_price_pln_kwh=1.03,
        cheap_windows=((22 * 60, 6 * 60), (13 * 60, 15 * 60)),
        weekend_low_price=False,
        polish_holidays_low_price=True,
    )
    holiday = datetime(2026, 12, 25, 10, 0, tzinfo=ZONE)
    assert tariff_rate(holiday, holiday_only) == (0.62, "low")

    # An official price table must never leak over New Year merely because the
    # plan started on 31 December.  The HA sensor catches this as an expired
    # profile; the pure optimizer also refuses an uncovered slot.
    official = TariffSchedule(
        tariff_type="G12",
        g11_price_pln_kwh=0.0,
        low_price_pln_kwh=0.0,
        medium_price_pln_kwh=0.0,
        peak_price_pln_kwh=0.0,
        cheap_windows=(),
        operator="PGE",
    )
    try:
        tariff_rate(datetime(2027, 1, 1, 0, 0, tzinfo=ZONE), official)
    except ValueError as err:
        assert "does not cover" in str(err)
    else:
        raise AssertionError("expired official tariff profile was accepted")

    # Deterministic property sweep: varied batteries, loads, power limits and
    # efficiencies must never violate energy, shared-power or cost invariants.
    random = Random(20260804)
    for _ in range(40):
        capacity = random.uniform(5.0, 230.0)
        charge_efficiency = random.uniform(82.0, 98.0)
        case = settings(
            monday,
            battery_capacity_kwh=capacity,
            battery_soc_percent=random.uniform(5.0, 100.0),
            reserve_soc_percent=random.uniform(5.0, 40.0),
            maximum_soc_percent=random.uniform(70.0, 100.0),
            average_daily_load_kwh=random.uniform(4.0, 90.0),
            average_night_load_kwh=random.uniform(1.0, 25.0),
            charge_power_kw=random.uniform(2.0, 40.0),
            battery_charge_power_kw=random.uniform(1.0, 30.0),
            battery_discharge_power_kw=random.uniform(1.0, 35.0),
            charge_efficiency_percent=charge_efficiency,
            discharge_efficiency_percent=random.uniform(82.0, 98.0),
        )
        swept = optimize_tariff_charging(case)
        assert -1e-6 <= swept.ending_battery_kwh <= capacity + 1e-6
        assert swept.remaining_shortage_kwh <= swept.baseline_shortage_kwh + 1e-6
        reserve_gap = max(
            case.reserve_soc_percent - case.battery_soc_percent,
            0.0,
        ) / 100.0 * capacity
        reserve_restoration_cost = (
            reserve_gap
            / max(case.charge_efficiency_percent / 100.0, 0.01)
            * case.schedule.low_price_pln_kwh
        )
        assert swept.optimized_grid_cost_pln <= (
            swept.baseline_grid_cost_pln + reserve_restoration_cost + 1e-6
        )
        assert 0.0 <= swept.target_soc_percent <= 100.0
        for item in swept.planned_charges:
            assert item.zone == "low"
            assert item.grid_import_kwh <= swept.charge_power_kw * 0.5 + 1e-6
            assert item.stored_energy_kwh <= (
                (item.grid_import_kwh - item.direct_load_kwh)
                * charge_efficiency
                / 100.0
                + 1e-6
            )

    sensor_source = (
        ROOT
        / "custom_components"
        / "hoymiles_hit_modbus"
        / "tariff_sensor.py"
    ).read_text(encoding="utf-8")
    assert '"sensor.hoymiles_hit_ems_self_use_soc_readback"' in sensor_source
    assert '"number.hoymiles_hit_self_use_soc"' not in sensor_source
    assert '"self_use_soc_data_fresh"' in sensor_source
    assert "minimum=10.0,\n            maximum=100.0" in sensor_source
    assert "minimum=0.0,\n            maximum=100.0" in sensor_source
    assert '"self_use_soc_age_seconds"' in sensor_source
    assert '"inverter_count_data_fresh"' in sensor_source
    assert '"inverter_count_age_seconds"' in sensor_source
    assert "1.0 if inverter_count_raw is None" not in sensor_source

    print("Tariff optimizer: deterministic scenarios passed")


if __name__ == "__main__":
    main()
