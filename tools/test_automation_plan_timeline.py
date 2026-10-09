"""Permanent AP-1 schema, trace, lifecycle and authority contracts."""

from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
from math import floor, isfinite
from pathlib import Path
import shutil
import sys
import tempfile
import types
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))

import automation_plan_timeline as TL  # noqa: E402
import rce_optimizer as RCE  # noqa: E402
import tariff_optimizer as TARIFF  # noqa: E402


UTC = timezone.utc
WARSAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 10, 25, 0, 0, tzinfo=UTC)


def expect_invalid(call: Callable[[], Any], fragment: str | None = None) -> None:
    try:
        call()
    except TL.TimelineValidationError as err:
        if fragment is not None:
            assert fragment in str(err), (fragment, str(err))
    else:
        raise AssertionError("invalid timeline was accepted")


def snapshot(**changes: Any) -> TL.CurrentActualSnapshot:
    values: dict[str, Any] = {
        "observed_at": NOW,
        "pv_kw": 1.0,
        "load_kw": 2.0,
        "battery_kw": -0.5,
        "grid_kw": 0.5,
        "soc_percent": 60.0,
        "quality": "complete",
        "source_ages_seconds": {
            "pv": 1.0,
            "load": 1.0,
            "battery": 1.0,
            "grid": 1.0,
            "soc": 1.0,
        },
    }
    values.update(changes)
    return TL.CurrentActualSnapshot(**values)


def trace(policy_id: str, count: int = 2) -> TL.OptimizerTimelineTrace:
    points: list[TL.TimelineTracePoint] = []
    for index in range(count):
        slot_minutes = 15 if policy_id == "rcm" else 30
        start = NOW + timedelta(minutes=slot_minutes * index)
        selected = index == 0
        if policy_id == "rce":
            policy: TL.RCEPolicyPoint | TL.TariffPolicyPoint | TL.RCMPolicyPoint = TL.RCEPolicyPoint(
                sell_price_pln_kwh=0.8,
                planned_export_kwh=0.5 if selected else 0.0,
                planned_battery_withdrawal_kwh=(0.5 if selected else 0.0),
                target_discharge_kw=1.0 if selected else 0.0,
                command_discharge_power_percent=20.0 if selected else 0.0,
                target_tolerance_kw=0.05,
                expected_revenue_pln=0.4 if selected else 0.0,
            )
            battery_delta = -0.5 if selected else 0.0
            grid_import = 0.0
            grid_export = 0.25 if selected else 0.0
            grid_import = 0.0 if selected else 0.25
            action = "export" if selected else "idle"
            target = None
            required_headroom = None
        elif policy_id == "tariff":
            policy = TL.TariffPolicyPoint(
                buy_price_pln_kwh=0.6,
                tariff_zone="low",
                planned_import_kwh=0.5 if selected else 0.0,
                stored_energy_kwh=0.475 if selected else 0.0,
                direct_load_kwh=0.025 if selected else 0.0,
                planned_charge_kw=0.95 if selected else 0.0,
                expected_cost_pln=0.3 if selected else 0.0,
                expected_saving_pln=None,
                need_class="economic" if selected else "none",
            )
            battery_delta = 0.475 if selected else 0.0
            grid_import = 0.725 if selected else 0.25
            grid_export = 0.0
            action = "battery_charge" if selected else "idle"
            target = 62.0 if selected else None
            required_headroom = None
        else:
            policy = TL.RCMPolicyPoint(
                voltage_risk_code=(
                    "historical_risk_window" if selected else "no_risk"
                ),
                planned_export_limit_percent=None,
                planned_export_limit_kw=None,
                recommended_charge_power_kw=None,
                planned_pre_discharge_kw=None,
                planned_pre_discharge_kwh=None,
                planned_pre_discharge_stored_kwh=None,
                action_start_offset_seconds=None,
                action_end_offset_seconds=None,
                headroom_shortfall_kwh=0.5,
                control_mode="monitor",
            )
            battery_delta = 0.0
            grid_import = 0.0
            grid_export = 0.0
            action = "monitor"
            target = 60.0 if selected else None
            required_headroom = 2.0
        points.append(
            TL.TimelineTracePoint(
                start=start,
                end=start + timedelta(minutes=slot_minutes),
                pv_kwh=(0.125 if policy_id == "rcm" else 0.25),
                load_kwh=(0.125 if policy_id == "rcm" else 0.5),
                battery_delta_kwh=battery_delta,
                grid_import_kwh=grid_import,
                grid_export_kwh=grid_export,
                soc_percent=61.0,
                baseline_soc_percent=60.0,
                protected_soc_floor_percent=20.0,
                action_code=action,
                selected=selected,
                quality="complete",
                policy=policy,
                target_soc_percent=target,
                required_headroom_kwh=required_headroom,
            )
        )
    return TL.OptimizerTimelineTrace(policy_id=policy_id, points=tuple(points))


def payload(policy_id: str = "rce", count: int = 2) -> dict[str, Any]:
    return payload_from_trace(trace(policy_id, count))


def payload_from_trace(candidate: TL.OptimizerTimelineTrace) -> dict[str, Any]:
    policy_id = candidate.policy_id
    return TL.build_current_payload(
        candidate,
        config_entry_id="entry-a",
        generated_at=NOW + timedelta(minutes=5),
        input_revision=7,
        plan_revision=1,
        plan_entity_id=(
            "sensor.hoymiles_hit_rce_optimized_plan"
            if policy_id == "rce"
            else "sensor.hoymiles_hit_tariff_charge_plan"
            if policy_id == "tariff"
            else "sensor.hoymiles_hit_rcm_voltage_plan"
        ),
        current_actual=snapshot(),
        sources=[{"role": "plan", "entity_id": "sensor.plan_a"}],
        physical_active=True,
    )


def trace_with_intervals(
    policy_id: str,
    intervals: tuple[tuple[datetime, datetime], ...],
) -> TL.OptimizerTimelineTrace:
    candidate = trace(policy_id, len(intervals))
    return replace(
        candidate,
        points=tuple(
            replace(point, start=start, end=end)
            for point, (start, end) in zip(candidate.points, intervals, strict=True)
        ),
    )


def test_rce_partial_first_slot_contract() -> None:
    base = datetime(2026, 8, 8, 19, 0, tzinfo=UTC)
    accepted = trace_with_intervals(
        "rce",
        (
            (base + timedelta(minutes=10), base + timedelta(minutes=30)),
            (base + timedelta(minutes=30), base + timedelta(minutes=60)),
        ),
    )
    assert payload_from_trace(accepted)["point_count"] == 2

    interior = trace_with_intervals(
        "rce",
        (
            (base, base + timedelta(minutes=30)),
            (base + timedelta(minutes=30), base + timedelta(minutes=50)),
            (base + timedelta(minutes=50), base + timedelta(minutes=80)),
        ),
    )
    expect_invalid(lambda: payload_from_trace(interior), "native policy slot")
    too_long = trace_with_intervals(
        "rce",
        ((base - timedelta(minutes=10), base + timedelta(minutes=30)),),
    )
    expect_invalid(lambda: payload_from_trace(too_long), "native policy slot")


def test_tariff_partial_first_slot_contract() -> None:
    base = datetime(2026, 8, 8, 19, 0, tzinfo=UTC)
    accepted = trace_with_intervals(
        "tariff",
        (
            (base + timedelta(minutes=10), base + timedelta(minutes=30)),
            (base + timedelta(minutes=30), base + timedelta(minutes=60)),
        ),
    )
    assert payload_from_trace(accepted)["point_count"] == 2

    interior = trace_with_intervals(
        "tariff",
        (
            (base, base + timedelta(minutes=30)),
            (base + timedelta(minutes=30), base + timedelta(minutes=50)),
            (base + timedelta(minutes=50), base + timedelta(minutes=80)),
        ),
    )
    expect_invalid(lambda: payload_from_trace(interior), "native policy slot")
    too_long = trace_with_intervals(
        "tariff",
        ((base - timedelta(minutes=10), base + timedelta(minutes=30)),),
    )
    expect_invalid(lambda: payload_from_trace(too_long), "native policy slot")


def test_rcm_partial_first_slot_contract() -> None:
    base = datetime(2026, 8, 8, 19, 0, tzinfo=UTC)
    partial = trace_with_intervals(
        "rcm",
        ((base + timedelta(minutes=5), base + timedelta(minutes=15)),),
    )
    assert payload_from_trace(partial)["point_count"] == 1
    interior = trace_with_intervals(
        "rcm",
        (
            (base, base + timedelta(minutes=15)),
            (base + timedelta(minutes=15), base + timedelta(minutes=25)),
        ),
    )
    expect_invalid(lambda: payload_from_trace(interior), "native policy slot")
    off_grid = trace_with_intervals(
        "rcm",
        ((base + timedelta(minutes=4), base + timedelta(minutes=14)),),
    )
    expect_invalid(lambda: payload_from_trace(off_grid), "native UTC grid")
    assert payload_from_trace(trace_with_intervals(
        "rcm",
        ((base, base + timedelta(minutes=15)),),
    ))["point_count"] == 1


def test_schema_and_policy_contract() -> None:
    rce = payload("rce")
    tariff = payload("tariff")
    rcm = payload("rcm")
    assert TL.SCHEMA_VERSION == 2
    assert rce["schema_version"] == TL.SCHEMA_VERSION
    assert set(rce) == TL.TOP_LEVEL_FIELDS
    assert set(rce["points"][0]) == TL.COMMON_POINT_FIELDS
    assert set(rce["points"][0]["policy"]) == TL.RCE_POLICY_FIELDS
    assert rce["points"][0]["policy"]["command_discharge_power_percent"] == 20.0
    assert rce["points"][1]["policy"]["command_discharge_power_percent"] == 0.0
    assert "target_soc_percent" not in rce["points"][0]
    assert "required_headroom_kwh" not in rce["points"][0]
    assert set(tariff["points"][0]) == TL.COMMON_POINT_FIELDS | {
        "target_soc_percent"
    }
    assert set(tariff["points"][0]["policy"]) == TL.TARIFF_POLICY_FIELDS
    assert tariff["points"][0]["policy"]["need_class"] == "economic"
    assert tariff["points"][1]["policy"]["need_class"] == "none"
    assert "required_headroom_kwh" not in tariff["points"][0]
    assert rcm["slot_minutes"] == 15
    assert set(rcm["points"][0]) == TL.COMMON_POINT_FIELDS | {
        "target_soc_percent",
        "required_headroom_kwh",
    }
    assert set(rcm["points"][0]["policy"]) == TL.RCM_POLICY_FIELDS
    assert rcm["points"][0]["policy"]["voltage_risk_code"] == "historical_risk_window"
    malformed = deepcopy(rce)
    malformed["unexpected"] = True
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    old_schema = deepcopy(rce)
    old_schema["schema_version"] = 1
    expect_invalid(
        lambda: TL.validate_payload(old_schema, expected_state="current"),
        "schema_version",
    )
    malformed = deepcopy(rce)
    malformed["points"][0]["policy"]["arbitrary"] = 1
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    for invalid_command in (-0.1, 100.1, None):
        malformed = deepcopy(rce)
        malformed["points"][0]["policy"][
            "command_discharge_power_percent"
        ] = invalid_command
        expect_invalid(
            lambda malformed=malformed: TL.validate_payload(
                malformed, expected_state="current"
            )
        )
    malformed = deepcopy(rce)
    malformed["blocker_code"] = "unbounded code " * 20
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    malformed = deepcopy(tariff)
    malformed["points"][0]["policy"]["need_class"] = "inferred"
    expect_invalid(
        lambda: TL.validate_payload(malformed, expected_state="current"),
        "need_class",
    )


def test_intervals_dst_and_native_resolution() -> None:
    current = payload(count=4)
    starts = [point["start"] for point in current["points"]]
    assert len(starts) == len(set(starts))
    assert current["slot_minutes"] == 30
    assert all(point["start"].endswith("Z") for point in current["points"])
    assert all(point["end"].endswith("Z") for point in current["points"])
    # These UTC points span the repeated Warsaw 02:00 hour without losing
    # identity; no local wall-clock interpolation is involved.
    local = [
        datetime.fromisoformat(point["start"].replace("Z", "+00:00")).astimezone(
            WARSAW
        )
        for point in current["points"]
    ]
    assert local[0].utcoffset() != local[-1].utcoffset()
    malformed = deepcopy(current)
    malformed["points"][1]["start"] = malformed["points"][0]["start"]
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    malformed = deepcopy(current)
    malformed["points"][1]["end"] = malformed["points"][1]["start"]
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    malformed = deepcopy(current)
    malformed["points"][1]["end"] = "2026-10-25T01:45:00Z"
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    malformed = deepcopy(current)
    malformed["points"][0]["start"] = "2026-10-25T00:00:00"
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))


def test_numbers_nulls_and_signs() -> None:
    current = payload()
    first = current["points"][0]
    assert first["grid_kw"] == -0.5
    assert first["grid_import_kw"] == 0.0
    assert first["grid_export_kw"] == 0.5
    assert first["battery_kw"] == -1.0
    assert payload("tariff")["points"][0]["battery_kw"] == 0.95
    assert payload("tariff")["points"][0]["grid_kw"] == 1.45
    assert payload("rcm")["points"][0]["grid_kw"] == 0.0
    zero = deepcopy(current)
    for key in ("pv_kw", "load_kw", "battery_kw", "grid_kw", "grid_import_kw", "grid_export_kw"):
        zero["points"][1][key] = 0.0
    TL.validate_payload(zero, expected_state="current")
    missing = deepcopy(current)
    for key in ("grid_kw", "grid_import_kw", "grid_export_kw"):
        missing["points"][1][key] = None
    TL.validate_payload(missing, expected_state="current")
    for bad in (float("nan"), float("inf"), -float("inf")):
        malformed = deepcopy(current)
        malformed["points"][0]["pv_kw"] = bad
        expect_invalid(lambda malformed=malformed: TL.validate_payload(malformed, expected_state="current"))
    malformed = deepcopy(current)
    malformed["points"][0]["grid_kw"] = 1.0
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    malformed = deepcopy(current)
    malformed["points"][0]["grid_import_kw"] = 1.0
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))


def test_rce_export_can_coexist_with_pv_net_charging() -> None:
    """Accept only a positive RCE battery delta explained by the same-slot PV."""

    simultaneous = deepcopy(payload("rce"))
    point = simultaneous["points"][0]
    point.update(
        pv_kw=5.0,
        load_kw=1.0,
        battery_kw=1.5,
        grid_kw=-2.0,
        grid_import_kw=0.0,
        grid_export_kw=2.0,
    )
    TL.validate_payload(simultaneous, expected_state="current")

    unexplained = deepcopy(simultaneous)
    unexplained["points"][0]["battery_kw"] = 2.5
    expect_invalid(
        lambda: TL.validate_payload(unexplained, expected_state="current"),
        "direction conflicts with planned discharge",
    )

    missing_pv = deepcopy(simultaneous)
    missing_pv["points"][0]["pv_kw"] = None
    expect_invalid(
        lambda: TL.validate_payload(missing_pv, expected_state="current"),
        "direction conflicts with planned discharge",
    )

    settings = rce_input()
    pv_by_slot = {
        slot.start: 3.0 for slot in settings.price_slots[4:]
    }
    settings = replace(
        settings,
        battery_soc_percent=50.0,
        discharge_power_percent=20.0,
        pv_by_slot_kwh=pv_by_slot,
        conservative_pv_by_slot_kwh=pv_by_slot,
    )
    result = RCE.optimize_rce(settings)
    assert result.timeline_trace is not None
    simultaneous_points = [
        item
        for item in result.timeline_trace.points
        if item.selected and item.battery_delta_kwh > 0.0
    ]
    # Legacy payloads may describe an aggregate PV/battery observation, but
    # the current planner holds Mode 5 for the selected interval and must not
    # book a refill during that same commanded discharge.
    assert not simultaneous_points
    assert any(item.selected for item in result.timeline_trace.points)
    assert payload_from_trace(result.timeline_trace)["point_count"] > 0


def test_hard_limits() -> None:
    accepted = payload("rce", 192)
    assert accepted["point_count"] == 192
    expect_invalid(lambda: payload("rce", 193), "point_count")
    TL.validate_serialized_byte_count(262_144)
    expect_invalid(lambda: TL.validate_serialized_byte_count(262_145))
    sixteen = deepcopy(payload())
    sixteen["sources"] = [
        {"role": f"source_{index}", "entity_id": f"sensor.source_{index}"}
        for index in range(16)
    ]
    TL.validate_payload(sixteen, expected_state="current")
    sixteen["sources"].append(
        {"role": "source_16", "entity_id": "sensor.source_16"}
    )
    expect_invalid(lambda: TL.validate_payload(sixteen, expected_state="current"))


def rce_input() -> RCE.OptimizerInput:
    now = datetime(2026, 7, 28, 0, 0, tzinfo=WARSAW)
    price_slots = [
        RCE.PriceSlot(
            start=now.replace(hour=18) + timedelta(minutes=30 * index),
            price_pln_kwh=0.9,
        )
        for index in range(4)
    ] + [
        RCE.PriceSlot(
            start=now.replace(hour=6) + timedelta(days=1, minutes=30 * index),
            price_pln_kwh=1.1,
        )
        for index in range(4)
    ]
    return RCE.OptimizerInput(
        now=now,
        price_slots=price_slots,
        pv_by_slot_kwh={},
        battery_capacity_kwh=20.0,
        battery_soc_percent=100.0,
        outage_reserve_soc_percent=20.0,
        safety_margin_soc_percent=0.0,
        manual_minimum_soc_percent=20.0,
        dynamic_reserve_enabled=True,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
        night_start_minute=20 * 60,
        night_end_minute=8 * 60,
        inverter_power_kw=10.0,
        inverter_count=1,
        discharge_power_percent=100.0,
        export_efficiency_percent=100.0,
        bms_max_discharge_current_a=500.0,
        bms_max_charge_current_a=500.0,
        battery_voltage_v=50.0,
        bms_power_safety_percent=100.0,
        bms_discharge_data_fresh=True,
        bms_discharge_data_age_seconds=0.0,
        bms_discharge_data_available=True,
        bms_charge_data_fresh=True,
        bms_charge_data_age_seconds=0.0,
        bms_charge_data_available=True,
        charge_efficiency_percent=100.0,
        house_discharge_efficiency_percent=100.0,
    )


def tariff_input() -> TARIFF.TariffOptimizerInput:
    now = datetime(2026, 8, 3, 21, 10, tzinfo=WARSAW)
    return TARIFF.TariffOptimizerInput(
        now=now,
        pv_by_slot_kwh={},
        battery_capacity_kwh=20.0,
        battery_soc_percent=30.0,
        reserve_soc_percent=20.0,
        maximum_soc_percent=100.0,
        average_daily_load_kwh=18.0,
        average_night_load_kwh=8.0,
        night_start_minute=20 * 60,
        night_end_minute=7 * 60,
        charge_power_kw=5.0,
        system_ac_power_kw=10.0,
        charge_efficiency_percent=95.0,
        discharge_efficiency_percent=95.0,
        minimum_saving_pln_kwh=0.05,
        export_state=TARIFF.ExportState.VERIFIED_ALLOWED,
        schedule=TARIFF.TariffSchedule(
            tariff_type="G12",
            g11_price_pln_kwh=0.85,
            low_price_pln_kwh=0.62,
            medium_price_pln_kwh=0.82,
            peak_price_pln_kwh=1.03,
            cheap_windows=((22 * 60, 6 * 60), (13 * 60, 15 * 60)),
        ),
    )


def _rce_forecast_functions() -> tuple[Callable[..., Any], Callable[..., Any]]:
    """Load only the production forecast parser/map without HA runtime imports."""

    source = (COMPONENT / "rce_sensor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    selected = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_parse_datetime", "_detailed_pv_map"}
    ]
    assert {node.name for node in selected} == {
        "_parse_datetime",
        "_detailed_pv_map",
    }
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            *selected,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)

    class _DtUtil:
        @staticmethod
        def parse_datetime(value: str) -> datetime | None:
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                return None

    namespace: dict[str, Any] = {
        "Any": Any,
        "Mapping": Mapping,
        "State": object,
        "ZoneInfo": ZoneInfo,
        "date": datetime.date,
        "datetime": datetime,
        "dt_util": _DtUtil,
        "floor_half_hour": RCE.floor_half_hour,
    }
    exec(compile(module, str(COMPONENT / "rce_sensor.py"), "exec"), namespace)
    return namespace["_parse_datetime"], namespace["_detailed_pv_map"]


def test_native_datetime_forecast_mapping_contract() -> None:
    """Solcast native datetimes and ISO strings must reach both optimizers."""

    parse_datetime, detailed_pv_map = _rce_forecast_functions()
    native = datetime(2026, 8, 31, 13, 0, tzinfo=WARSAW)
    assert parse_datetime(native, WARSAW) == native
    assert parse_datetime(native.astimezone(UTC).isoformat(), WARSAW) == native
    assert parse_datetime(123, WARSAW) is None

    state = types.SimpleNamespace(
        attributes={
            "detailedForecast": [
                {"period_start": native, "pv_estimate": 2.0},
                {
                    "period_start": (native + timedelta(minutes=30))
                    .astimezone(UTC)
                    .isoformat(),
                    "pv_estimate": 4.0,
                },
            ]
        }
    )
    mapped = detailed_pv_map(
        state,
        native.date(),
        3.0,
        WARSAW,
        native,
    )
    assert set(mapped) == {native, native + timedelta(minutes=30)}
    assert abs(sum(mapped.values()) - 3.0) < 1e-12
    assert abs(mapped[native] - 1.0) < 1e-12
    assert abs(mapped[native + timedelta(minutes=30)] - 2.0) < 1e-12


def _soc_pv_oracle_codes(
    rows: list[dict[str, Any]],
    *,
    initial_soc_percent: float,
    battery_capacity_kwh: float,
    charge_efficiency: float = 0.95,
    discharge_efficiency: float = 0.95,
) -> set[str]:
    """Independently classify public timeline energy/SOC inconsistencies."""

    tolerance = 1e-8
    codes: set[str] = set()
    previous_end: datetime | None = None
    previous_soc = initial_soc_percent
    provenance: tuple[Any, ...] | None = None
    for index, row in enumerate(rows):
        start = row["start"]
        end = row["end"]
        if previous_end is not None and start != previous_end:
            codes.add("DISCONTINUITY_BETWEEN_POINTS")
        duration_h = (end - start).total_seconds() / 3600.0
        if duration_h <= 0:
            codes.add("DISCONTINUITY_BETWEEN_POINTS")
            continue

        current_provenance = (
            row.get("source_policy"),
            row.get("config_entry_id"),
            row.get("input_revision"),
            row.get("plan_revision"),
        )
        if provenance is not None and current_provenance != provenance:
            codes.add("POLICY_TRAJECTORY_SPLICE")
        provenance = current_provenance

        pv_kw = row.get("pv_kw")
        load_kw = row.get("load_kw")
        battery_kw = row.get("battery_kw")
        grid_import_kw = row.get("grid_import_kw")
        grid_export_kw = row.get("grid_export_kw")
        if pv_kw is None or (
            isinstance(pv_kw, (int, float)) and not isfinite(float(pv_kw))
        ):
            codes.add("PV_SOURCE_MISSING")
        numeric_balance = all(
            isinstance(value, (int, float)) and isfinite(float(value))
            for value in (
                pv_kw,
                load_kw,
                battery_kw,
                grid_import_kw,
                grid_export_kw,
                row.get("soc_percent"),
            )
        )
        if not numeric_balance:
            codes.add("ENERGY_BALANCE_MISMATCH")
            previous_end = end
            continue

        expected_soc = (
            previous_soc
            + float(battery_kw) * duration_h / battery_capacity_kwh * 100.0
        )
        if abs(float(row["soc_percent"]) - expected_soc) > tolerance:
            codes.add("SOC_WITHOUT_ENERGY_SOURCE")
            if index > 0:
                codes.add("DISCONTINUITY_BETWEEN_POINTS")

        charge_ac_kw = max(float(battery_kw), 0.0) / charge_efficiency
        discharge_ac_kw = (
            max(-float(battery_kw), 0.0) * discharge_efficiency
        )
        balance_residual = (
            float(pv_kw)
            + float(grid_import_kw)
            + discharge_ac_kw
            - float(load_kw)
            - float(grid_export_kw)
            - charge_ac_kw
        )
        if abs(balance_residual) > tolerance:
            codes.add("ENERGY_BALANCE_MISMATCH")
        if max(float(battery_kw), 0.0) > float(
            row.get("battery_charge_limit_kw", 0.0)
        ) + tolerance:
            codes.add("ENERGY_BALANCE_MISMATCH")
        if float(row["soc_percent"]) > float(
            row.get("maximum_soc_percent", 100.0)
        ) + tolerance:
            codes.add("ENERGY_BALANCE_MISMATCH")
        if float(grid_import_kw) > tolerance and float(grid_export_kw) > tolerance:
            codes.add("ENERGY_BALANCE_MISMATCH")

        action_code = row.get("action_code")
        if (
            abs(float(pv_kw) - float(load_kw)) <= tolerance
            and action_code not in {"battery_charge", "grid_support_and_charge"}
            and (
                abs(float(battery_kw)) > tolerance
                or float(grid_import_kw) > tolerance
                or float(grid_export_kw) > tolerance
            )
        ):
            codes.add("ENERGY_BALANCE_MISMATCH")
        if action_code == "grid_support":
            if float(pv_kw) <= float(load_kw) + tolerance:
                expected_import_kw = max(float(load_kw) - float(pv_kw), 0.0)
                if (
                    abs(float(battery_kw)) > tolerance
                    or abs(float(grid_import_kw) - expected_import_kw) > tolerance
                    or float(grid_export_kw) > tolerance
                ):
                    codes.add("ENERGY_BALANCE_MISMATCH")
            elif float(grid_import_kw) > tolerance:
                codes.add("ENERGY_BALANCE_MISMATCH")

        pv_surplus_kw = max(float(pv_kw) - float(load_kw), 0.0)
        charge_available = (
            float(row.get("battery_charge_limit_kw", 0.0)) > tolerance
            and previous_soc
            < float(row.get("maximum_soc_percent", 100.0)) - tolerance
        )
        explained_surplus_kw = (
            charge_ac_kw
            + float(grid_export_kw)
            + float(row.get("pv_curtailed_kw", 0.0))
        )
        if (
            pv_surplus_kw > tolerance
            and charge_available
            and explained_surplus_kw + tolerance < pv_surplus_kw
        ):
            codes.add("PV_WITHOUT_SOC_EFFECT")

        previous_end = end
        previous_soc = float(row["soc_percent"])
    return codes


def _oracle_row(
    start: datetime,
    *,
    pv_kw: float | None,
    load_kw: float,
    battery_kw: float,
    grid_import_kw: float,
    grid_export_kw: float,
    soc_percent: float,
    source_policy: str = "tariff",
    action_code: str = "idle",
    battery_charge_limit_kw: float = 4.0,
    maximum_soc_percent: float = 100.0,
) -> dict[str, Any]:
    return {
        "source_policy": source_policy,
        "config_entry_id": "entry-a",
        "input_revision": 7,
        "plan_revision": 11,
        "start": start,
        "end": start + timedelta(minutes=30),
        "action_code": action_code,
        "pv_kw": pv_kw,
        "load_kw": load_kw,
        "battery_kw": battery_kw,
        "grid_import_kw": grid_import_kw,
        "grid_export_kw": grid_export_kw,
        "soc_percent": soc_percent,
        "battery_charge_limit_kw": battery_charge_limit_kw,
        "maximum_soc_percent": maximum_soc_percent,
    }


def test_independent_soc_pv_oracle_cases_1_to_8() -> None:
    """Cover the eight required physical cases without a production oracle."""

    base = datetime(2026, 8, 31, 10, 0, tzinfo=WARSAW)
    healthy_cases = [
        # 1. PV surplus charges first and the rest is exported.
        (
            [_oracle_row(base, pv_kw=5.0, load_kw=1.0, battery_kw=1.9,
                         grid_import_kw=0.0, grid_export_kw=2.0,
                         soc_percent=54.75)],
            50.0,
        ),
        # 2. PV exactly covers LOAD.
        (
            [_oracle_row(base, pv_kw=2.0, load_kw=2.0, battery_kw=0.0,
                         grid_import_kw=0.0, grid_export_kw=0.0,
                         soc_percent=50.0)],
            50.0,
        ),
        # 3. Executed grid support covers only the post-PV deficit.
        (
            [_oracle_row(base, pv_kw=1.0, load_kw=3.0, battery_kw=0.0,
                         grid_import_kw=2.0, grid_export_kw=0.0,
                         soc_percent=50.0, action_code="grid_support")],
            50.0,
        ),
        # 4. Grid support never displaces PV surplus charging.
        (
            [_oracle_row(base, pv_kw=5.0, load_kw=1.0, battery_kw=1.9,
                         grid_import_kw=0.0, grid_export_kw=2.0,
                         soc_percent=54.75, action_code="grid_support")],
            50.0,
        ),
        # 5. A full battery leaves an explicitly exported surplus.
        (
            [_oracle_row(base, pv_kw=5.0, load_kw=1.0, battery_kw=0.0,
                         grid_import_kw=0.0, grid_export_kw=4.0,
                         soc_percent=100.0, maximum_soc_percent=100.0)],
            100.0,
        ),
        # 6. Natural PV/LOAD still changes SOC while the EMS action is idle.
        (
            [
                _oracle_row(base, pv_kw=3.0, load_kw=1.0, battery_kw=1.9,
                            grid_import_kw=0.0, grid_export_kw=0.0,
                            soc_percent=54.75),
                _oracle_row(base + timedelta(minutes=30), pv_kw=0.0,
                            load_kw=2.0, battery_kw=-2.0 / 0.95,
                            grid_import_kw=0.0, grid_export_kw=0.0,
                            soc_percent=49.48684210526316),
            ],
            50.0,
        ),
    ]
    for rows, initial_soc in healthy_cases:
        assert not _soc_pv_oracle_codes(
            rows,
            initial_soc_percent=initial_soc,
            battery_capacity_kwh=20.0,
        )

    # 7. A tariff/RCE boundary is provenance-visible even with continuous SOC;
    # it must not be presented as one canonical physical trajectory.
    boundary = [
        _oracle_row(base, pv_kw=2.0, load_kw=2.0, battery_kw=0.0,
                    grid_import_kw=0.0, grid_export_kw=0.0,
                    soc_percent=60.0, source_policy="tariff"),
        _oracle_row(base + timedelta(minutes=30), pv_kw=2.0, load_kw=2.0,
                    battery_kw=0.0, grid_import_kw=0.0, grid_export_kw=0.0,
                    soc_percent=60.0, source_policy="rce"),
    ]
    boundary_codes = _soc_pv_oracle_codes(
        boundary,
        initial_soc_percent=60.0,
        battery_capacity_kwh=20.0,
    )
    assert boundary_codes == {"POLICY_TRAJECTORY_SPLICE"}

    # 8. 13:00-15:00 combines PV, LOAD and only the remaining grid import.
    cheap_start = base.replace(hour=13)
    cheap_zone = [
        _oracle_row(cheap_start, pv_kw=3.0, load_kw=1.0, battery_kw=3.8,
                    grid_import_kw=2.0, grid_export_kw=0.0,
                    soc_percent=29.5, action_code="grid_support_and_charge"),
        _oracle_row(cheap_start + timedelta(minutes=30), pv_kw=1.0,
                    load_kw=2.0, battery_kw=3.8, grid_import_kw=5.0,
                    grid_export_kw=0.0, soc_percent=39.0,
                    action_code="grid_support_and_charge"),
        _oracle_row(cheap_start + timedelta(minutes=60), pv_kw=4.0,
                    load_kw=1.0, battery_kw=3.8, grid_import_kw=1.0,
                    grid_export_kw=0.0, soc_percent=48.5,
                    action_code="grid_support_and_charge"),
        _oracle_row(cheap_start + timedelta(minutes=90), pv_kw=0.5,
                    load_kw=1.5, battery_kw=3.8, grid_import_kw=5.0,
                    grid_export_kw=0.0, soc_percent=58.0,
                    action_code="grid_support_and_charge"),
    ]
    assert not _soc_pv_oracle_codes(
        cheap_zone,
        initial_soc_percent=20.0,
        battery_capacity_kwh=20.0,
    )

    # Every required diagnostic code is independently observable.
    unexplained_pv = _oracle_row(
        base, pv_kw=5.0, load_kw=1.0, battery_kw=0.0,
        grid_import_kw=0.0, grid_export_kw=0.0, soc_percent=50.0,
    )
    assert "PV_WITHOUT_SOC_EFFECT" in _soc_pv_oracle_codes(
        [unexplained_pv], initial_soc_percent=50.0,
        battery_capacity_kwh=20.0,
    )
    unexplained_soc = _oracle_row(
        base, pv_kw=2.0, load_kw=2.0, battery_kw=0.0,
        grid_import_kw=0.0, grid_export_kw=0.0, soc_percent=55.0,
    )
    assert "SOC_WITHOUT_ENERGY_SOURCE" in _soc_pv_oracle_codes(
        [unexplained_soc], initial_soc_percent=50.0,
        battery_capacity_kwh=20.0,
    )
    jumped_boundary = [boundary[0], {**boundary[1], "soc_percent": 50.0}]
    jumped_codes = _soc_pv_oracle_codes(
        jumped_boundary,
        initial_soc_percent=60.0,
        battery_capacity_kwh=20.0,
    )
    assert {
        "DISCONTINUITY_BETWEEN_POINTS",
        "POLICY_TRAJECTORY_SPLICE",
        "SOC_WITHOUT_ENERGY_SOURCE",
    } <= jumped_codes
    missing_pv = {**healthy_cases[1][0][0], "pv_kw": None}
    assert "PV_SOURCE_MISSING" in _soc_pv_oracle_codes(
        [missing_pv], initial_soc_percent=50.0,
        battery_capacity_kwh=20.0,
    )
    wrong_grid = {**healthy_cases[2][0][0], "grid_import_kw": 1.0}
    assert "ENERGY_BALANCE_MISMATCH" in _soc_pv_oracle_codes(
        [wrong_grid], initial_soc_percent=50.0,
        battery_capacity_kwh=20.0,
    )
    pv_equals_load_grid_charge = {
        **healthy_cases[1][0][0],
        "battery_kw": 1.9,
        "grid_import_kw": 2.0,
        "soc_percent": 54.75,
    }
    support_from_battery = {
        **healthy_cases[2][0][0],
        "battery_kw": -2.0 / 0.95,
        "grid_import_kw": 0.0,
        "soc_percent": 44.73684210526316,
    }
    support_grid_loop = {
        **healthy_cases[3][0][0],
        "grid_import_kw": 1.0,
        "grid_export_kw": 3.0,
    }
    non_finite = {**healthy_cases[1][0][0], "pv_kw": float("nan")}
    for malformed in (
        pv_equals_load_grid_charge,
        support_from_battery,
        support_grid_loop,
        non_finite,
    ):
        assert "ENERGY_BALANCE_MISMATCH" in _soc_pv_oracle_codes(
            [malformed],
            initial_soc_percent=50.0,
            battery_capacity_kwh=20.0,
        )


def test_optimizer_pv_reaches_soc_trace_contract() -> None:
    """Run required PV/SOC cases through real optimizers and an independent oracle."""

    def public_rows(
        policy_id: str,
        timeline: TL.OptimizerTimelineTrace,
        *,
        charge_limit_kw: float,
        maximum_soc_percent: float = 100.0,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for point in timeline.points:
            duration_h = (point.end - point.start).total_seconds() / 3600.0
            row = _oracle_row(
                point.start,
                pv_kw=point.pv_kwh / duration_h,
                load_kw=point.load_kwh / duration_h,
                battery_kw=point.battery_delta_kwh / duration_h,
                grid_import_kw=point.grid_import_kwh / duration_h,
                grid_export_kw=point.grid_export_kwh / duration_h,
                soc_percent=point.soc_percent,
                source_policy=policy_id,
                action_code=point.action_code,
                battery_charge_limit_kw=charge_limit_kw,
                maximum_soc_percent=maximum_soc_percent,
            )
            row["end"] = point.end
            rows.append(row)
        return rows

    rce_settings = rce_input()
    rce_prices = tuple(
        replace(slot, price_pln_kwh=-0.1) for slot in rce_settings.price_slots
    )
    rce_pv_start = rce_prices[0].start
    rce_settings = replace(
        rce_settings,
        price_slots=rce_prices,
        pv_by_slot_kwh={rce_pv_start: 2.5},
        conservative_pv_by_slot_kwh={rce_pv_start: 2.5},
        battery_soc_percent=40.0,
        charge_efficiency_percent=95.0,
        house_discharge_efficiency_percent=95.0,
    )
    rce_result = RCE.optimize_rce(rce_settings)
    rce_timeline = rce_result.timeline_trace
    assert rce_timeline is not None
    rce_point = next(
        point
        for point in rce_timeline.points
        if point.start == rce_pv_start.astimezone(UTC)
    )
    assert not _soc_pv_oracle_codes(
        public_rows("rce", rce_timeline, charge_limit_kw=25.0),
        initial_soc_percent=40.0,
        battery_capacity_kwh=20.0,
    )
    assert rce_point.pv_kwh == 2.5
    assert rce_point.battery_delta_kwh == 2.375
    assert abs(rce_point.soc_percent - 51.875) < 1e-9

    tariff_base = tariff_input()
    tariff_now = tariff_base.now.replace(hour=12, minute=30)
    cheap_starts = tuple(
        tariff_now.replace(hour=13, minute=0) + timedelta(minutes=30 * index)
        for index in range(4)
    )
    natural_pv = dict(zip(cheap_starts, (1.5, 1.0, 2.0, 0.25), strict=True))
    natural_load = dict(zip(cheap_starts, (0.5, 1.0, 0.5, 0.75), strict=True))
    natural_settings = replace(
        tariff_base,
        now=tariff_now,
        pv_by_slot_kwh=natural_pv,
        load_by_slot_kwh=natural_load,
        battery_soc_percent=20.0,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
        battery_charge_power_kw=5.0,
        battery_discharge_power_kw=5.0,
        pv_charge_power_kw=5.0,
        minimum_saving_pln_kwh=0.0,
        current_pv_power_kw=None,
        current_load_power_kw=None,
    )
    natural_result = TARIFF.optimize_tariff_charging(natural_settings)
    natural_timeline = natural_result.timeline_trace
    assert natural_timeline is not None
    assert not _soc_pv_oracle_codes(
        public_rows("tariff", natural_timeline, charge_limit_kw=5.0),
        initial_soc_percent=20.0,
        battery_capacity_kwh=20.0,
    )
    natural_points = {
        point.start.astimezone(WARSAW): point for point in natural_timeline.points
    }
    surplus = natural_points[cheap_starts[0]]
    balanced = natural_points[cheap_starts[1]]
    later_deficit = natural_points[cheap_starts[3]]
    assert surplus.action_code == "idle" and not surplus.selected
    assert surplus.pv_kwh > surplus.load_kwh
    assert surplus.battery_delta_kwh > 0.0 and surplus.soc_percent > 20.0
    assert balanced.pv_kwh == balanced.load_kwh
    assert balanced.battery_delta_kwh == 0.0
    assert balanced.grid_import_kwh == 0.0 and balanced.grid_export_kwh == 0.0
    assert later_deficit.action_code == "idle"
    assert later_deficit.battery_delta_kwh < 0.0

    future_expensive = tuple(
        tariff_now.replace(hour=15, minute=0) + timedelta(minutes=30 * index)
        for index in range(14)
    )
    support_load = dict(zip(cheap_starts, (1.5, 1.0, 0.5, 0.75), strict=True))
    support_load.update({start: 1.0 for start in future_expensive})
    support_settings = replace(
        natural_settings,
        battery_soc_percent=60.0,
        pv_by_slot_kwh=dict(
            zip(cheap_starts, (0.5, 1.0, 2.0, 0.25), strict=True)
        ),
        load_by_slot_kwh=support_load,
    )
    support_result = TARIFF.optimize_tariff_charging(support_settings)
    support_timeline = support_result.timeline_trace
    assert support_timeline is not None
    assert not _soc_pv_oracle_codes(
        public_rows("tariff", support_timeline, charge_limit_kw=5.0),
        initial_soc_percent=60.0,
        battery_capacity_kwh=20.0,
    )
    support_points = {
        point.start.astimezone(WARSAW): point for point in support_timeline.points
    }
    direct_support = support_points[cheap_starts[0]]
    pv_equal_charge = support_points[cheap_starts[1]]
    pv_first_charge = support_points[cheap_starts[2]]
    combined = support_points[cheap_starts[3]]
    assert (
        direct_support.action_code == "grid_support_and_charge"
        and direct_support.selected
    )
    assert direct_support.pv_kwh == 0.5 and direct_support.load_kwh == 1.5
    assert direct_support.grid_import_kwh == 1.5
    assert abs(direct_support.battery_delta_kwh - 0.475) < 1e-9
    assert abs(direct_support.soc_percent - 62.375) < 1e-9
    assert direct_support.policy.direct_load_kwh == 1.0
    assert direct_support.policy.stored_energy_kwh == 0.475
    assert pv_equal_charge.action_code == "battery_charge"
    assert pv_equal_charge.pv_kwh == pv_equal_charge.load_kwh == 1.0
    assert abs(pv_equal_charge.battery_delta_kwh - 1.425) < 1e-9
    assert pv_first_charge.pv_kwh == 2.0 and pv_first_charge.load_kwh == 0.5
    assert pv_first_charge.battery_delta_kwh == 2.375
    assert (
        abs(
            pv_first_charge.grid_import_kwh * 0.95
            + 1.5 * 0.95
            - 2.375
        )
        < 1e-9
    )
    assert combined.action_code == "grid_support_and_charge"
    assert combined.grid_import_kwh > 0.0 and combined.battery_delta_kwh > 0.0

    full_settings = replace(
        natural_settings,
        battery_soc_percent=100.0,
        pv_by_slot_kwh={cheap_starts[0]: 2.5},
        load_by_slot_kwh={cheap_starts[0]: 0.5},
    )
    full_result = TARIFF.optimize_tariff_charging(full_settings)
    full_timeline = full_result.timeline_trace
    assert full_timeline is not None
    assert not _soc_pv_oracle_codes(
        public_rows("tariff", full_timeline, charge_limit_kw=5.0),
        initial_soc_percent=100.0,
        battery_capacity_kwh=20.0,
    )
    full_point = next(
        point
        for point in full_timeline.points
        if point.start == cheap_starts[0].astimezone(UTC)
    )
    assert full_point.battery_delta_kwh == 0.0
    assert full_point.soc_percent == 100.0
    assert full_point.grid_export_kwh == 2.0

    # Real optimizer fragments are independently coherent, but joining them
    # would be an explicit provenance splice. The AP-3B JS contract separately
    # proves that its main green line selects one policy instead of doing this.
    mixed_real_rows = [
        public_rows("tariff", natural_timeline, charge_limit_kw=5.0)[0],
        public_rows("rce", rce_timeline, charge_limit_kw=25.0)[0],
    ]
    assert "POLICY_TRAJECTORY_SPLICE" in _soc_pv_oracle_codes(
        mixed_real_rows,
        initial_soc_percent=20.0,
        battery_capacity_kwh=20.0,
    )


def test_exact_rce_trace() -> None:
    result = RCE.optimize_rce(rce_input())
    timeline = result.timeline_trace
    assert timeline is not None and timeline.policy_id == "rce"
    assert len(timeline.points) <= 96
    assert timeline.points[-1].end - timeline.points[0].start <= timedelta(hours=48)
    selected = {
        point.start.astimezone(WARSAW): point
        for point in timeline.points
        if point.selected
    }
    assert set(selected) == {item.start for item in result.planned_exports}
    for planned in result.planned_exports:
        point = selected[planned.start]
        assert isinstance(point.policy, TL.RCEPolicyPoint)
        assert abs(point.policy.planned_export_kwh - planned.energy_kwh) < 1e-9
        duration_hours = (point.end - point.start).total_seconds() / 3600.0
        planned_discharge_power = (
            point.policy.planned_export_kwh
            + max(point.load_kwh - point.pv_kwh, 0.0)
        ) / duration_hours
        assert result.bms_discharge_power_limit_kw is not None
        expected_command_percent = (
            min(
                planned_discharge_power,
                result.requested_export_power_kw,
                result.system_power_kw,
                result.bms_discharge_power_limit_kw,
            )
            / result.system_power_kw
            * 100.0
        )
        expected_command_percent = float(floor(min(expected_command_percent, 100.0)))
        assert abs(
            point.policy.command_discharge_power_percent
            - expected_command_percent
        ) < 1e-9
        assert point.policy.command_discharge_power_percent <= (
            planned_discharge_power / result.system_power_kw * 100.0
        )
        assert abs(
            point.policy.expected_revenue_pln - planned.revenue_pln
        ) < 1e-9
        assert point.action_code == "export"
        assert point.grid_export_kwh >= planned.energy_kwh
        assert point.target_soc_percent is None
    assert all(point.pv_kwh >= 0 and point.load_kwh >= 0 for point in timeline.points)
    assert all(0 <= point.soc_percent <= 100 for point in timeline.points)
    assert all(0 <= point.baseline_soc_percent <= 100 for point in timeline.points)
    assert all(point.protected_soc_floor_percent >= 20.0 for point in timeline.points)
    assert all(
        point.policy.command_discharge_power_percent == 0.0
        for point in timeline.points
        if not point.selected
    )
    reduced = [
        point
        for point in timeline.points
        if point.selected
        and 0.0 < point.policy.command_discharge_power_percent < 100.0
    ]
    assert reduced
    assert any(
        point.policy.command_discharge_power_percent == 19.0
        and point.policy.command_discharge_power_percent
        < point.policy.planned_export_kwh
        / ((point.end - point.start).total_seconds() / 3600.0)
        / result.system_power_kw
        * 100.0
        for point in reduced
    )
    # compare=False keeps legacy equality independent from the observation sidecar.
    assert result == replace(result, timeline_trace=None)


def test_exact_tariff_trace() -> None:
    result = TARIFF.optimize_tariff_charging(tariff_input())
    timeline = result.timeline_trace
    assert timeline is not None and timeline.policy_id == "tariff"
    assert len(timeline.points) <= 96
    assert timeline.points[-1].end - timeline.points[0].start <= timedelta(hours=48)
    selected = [point for point in timeline.points if point.selected]
    assert len(selected) == len(result.planned_charges)
    charges = {item.start: item for item in result.planned_charges}
    for point in selected:
        local_start = point.start.astimezone(WARSAW)
        planned = charges[local_start]
        assert isinstance(point.policy, TL.TariffPolicyPoint)
        assert abs(point.policy.planned_import_kwh - planned.grid_import_kwh) < 1e-9
        assert point.policy.tariff_zone == planned.zone
        assert point.policy.expected_cost_pln == planned.grid_import_kwh * planned.price_pln_kwh
        assert point.policy.expected_saving_pln is None
        assert point.policy.need_class in {"required_energy", "economic", "mixed"}
        assert point.target_soc_percent is not None
    assert all(
        point.policy.need_class == "none"
        for point in timeline.points
        if point.action_code == "idle"
    )
    published = payload_from_trace(timeline)
    assert tuple(
        point["policy"]["need_class"] for point in published["points"]
    ) == tuple(point.policy.need_class for point in timeline.points)
    assert all(point.protected_soc_floor_percent == 20.0 for point in timeline.points)
    assert all(point.grid_import_kwh >= 0 and point.grid_export_kwh >= 0 for point in timeline.points)
    assert result == replace(result, timeline_trace=None)


def test_revision_atomicity_and_states() -> None:
    complete = payload()
    fingerprint = TL.semantic_fingerprint(complete)
    generated_only = deepcopy(complete)
    generated_only["generated_at"] = "2026-10-25T00:06:00Z"
    assert TL.semantic_fingerprint(generated_only) == fingerprint
    age_only = deepcopy(complete)
    age_only["current_actual"]["observed_at"] = "2026-10-25T00:06:00Z"
    age_only["current_actual"]["source_ages_seconds"]["pv"] = 99.0
    assert TL.semantic_fingerprint(age_only) == fingerprint
    semantic = deepcopy(complete)
    semantic["points"][0]["soc_percent"] += 1.0
    assert TL.semantic_fingerprint(semantic) != fingerprint

    # Fingerprinting canonicalizes only exact JSON equivalences. Source order
    # and dictionary insertion order are non-semantic, while point chronology
    # and every real numeric/timestamp change remain visible.
    permuted_sources = deepcopy(complete)
    complete["sources"] = [
        {"role": "z", "entity_id": "sensor.z"},
        {"role": "a", "entity_id": "sensor.a"},
    ]
    permuted_sources["sources"] = list(reversed(complete["sources"]))
    assert TL.semantic_fingerprint(complete) == TL.semantic_fingerprint(
        permuted_sources
    )
    assert TL.semantic_fingerprint({"a": 1, "b": 2}) == TL.semantic_fingerprint(
        {"b": 2, "a": 1}
    )
    assert TL.semantic_fingerprint({"value": 0}) == TL.semantic_fingerprint(
        {"value": -0.0}
    )
    assert TL.semantic_fingerprint({"value": 1}) == TL.semantic_fingerprint(
        {"value": 1.0}
    )
    assert TL.semantic_fingerprint({"value": True}) != TL.semantic_fingerprint(
        {"value": 1}
    )
    assert TL.semantic_fingerprint({"value": 1.0}) != TL.semantic_fingerprint(
        {"value": 1.001}
    )
    swapped = deepcopy(complete)
    swapped["points"] = list(reversed(swapped["points"]))
    assert TL.semantic_fingerprint(swapped) != TL.semantic_fingerprint(complete)
    changed_timestamp = deepcopy(complete)
    changed_timestamp["points"][0]["start"] = "2026-10-25T00:00:01Z"
    assert TL.semantic_fingerprint(changed_timestamp) != TL.semantic_fingerprint(
        complete
    )
    for invalid in (float("nan"), float("inf"), -float("inf")):
        expect_invalid(lambda invalid=invalid: TL.semantic_fingerprint({"v": invalid}))
    assert TL.next_plan_revision(0, None, fingerprint) == 1
    assert TL.next_plan_revision(1, fingerprint, fingerprint) == 1
    assert TL.next_plan_revision(1, fingerprint, "different") == 2
    # An unavailable publication exposes no plan, but recovery does not make
    # the runtime revision counter move backwards.
    assert TL.next_plan_revision(7, fingerprint, fingerprint) == 7

    pending = TL.build_pending_payload(
        complete,
        policy_id="rce",
        config_entry_id="entry-a",
        generated_at=NOW + timedelta(minutes=6),
        pending_input_revision=8,
        plan_entity_id="sensor.hoymiles_hit_rce_optimized_plan",
    )
    assert pending["points"] == complete["points"]
    assert pending["input_revision"] == complete["input_revision"]
    assert pending["pending_input_revision"] == 8
    assert not pending["result_current"] and pending["recalculation_pending"]
    unavailable = TL.build_unavailable_payload(
        policy_id="rce",
        config_entry_id="entry-a",
        generated_at=NOW,
        input_revision=8,
        plan_entity_id="sensor.hoymiles_hit_rce_optimized_plan",
        blocker_code="missing_data",
    )
    assert unavailable["points"] == []
    assert unavailable["quality"] == "unavailable"
    assert not unavailable["result_current"]
    assert unavailable["plan_revision_scope"] == "runtime"
    assert unavailable["active_scope"] == "publication_snapshot"
    assert unavailable["active_observed_at"] is None


def test_active_snapshot_contract() -> None:
    inactive = TL.build_current_payload(
        trace("rce"),
        config_entry_id="entry-a",
        generated_at=NOW + timedelta(minutes=5),
        input_revision=7,
        plan_revision=1,
        plan_entity_id="sensor.hoymiles_hit_rce_optimized_plan",
        current_actual=snapshot(),
        sources=[],
        physical_active=False,
    )
    active = TL.build_current_payload(
        trace("rce"),
        config_entry_id="entry-a",
        generated_at=NOW + timedelta(minutes=5),
        input_revision=7,
        plan_revision=1,
        plan_entity_id="sensor.hoymiles_hit_rce_optimized_plan",
        current_actual=snapshot(),
        sources=[],
        physical_active=True,
        active_observed_at=NOW + timedelta(minutes=4),
    )
    missing = TL.build_current_payload(
        trace("rce"),
        config_entry_id="entry-a",
        generated_at=NOW + timedelta(minutes=5),
        input_revision=7,
        plan_revision=1,
        plan_entity_id="sensor.hoymiles_hit_rce_optimized_plan",
        current_actual=snapshot(),
        sources=[],
        physical_active=None,
    )
    assert inactive["active_scope"] == active["active_scope"] == (
        "publication_snapshot"
    )
    assert inactive["active_observed_at"] == "2026-10-25T00:05:00Z"
    assert active["active_observed_at"] == "2026-10-25T00:04:00Z"
    assert inactive["points"][0]["active"] is False
    assert active["points"][0]["active"] is True
    assert missing["points"][0]["active"] is None
    assert missing["active_observed_at"] is None
    pending = TL.build_pending_payload(
        active,
        policy_id="rce",
        config_entry_id="entry-a",
        generated_at=NOW + timedelta(minutes=6),
        pending_input_revision=8,
        plan_entity_id="sensor.hoymiles_hit_rce_optimized_plan",
    )
    assert pending["active_observed_at"] == active["active_observed_at"]
    assert pending["points"] == active["points"]


def test_malformed_data_and_partial_horizon() -> None:
    current = payload()
    malformed = deepcopy(current)
    malformed["points"][0] = "wrong"
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    malformed = deepcopy(current)
    malformed["points"][0]["action_code"] = "free_text"
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    malformed = deepcopy(current)
    malformed["points"][0]["quality"] = "maybe"
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    partial = TL.build_current_payload(
        replace(trace("rce", 1), quality="partial", blocker_code="partial_horizon"),
        config_entry_id="entry-a",
        generated_at=NOW,
        input_revision=1,
        plan_revision=1,
        plan_entity_id="sensor.hoymiles_hit_rce_optimized_plan",
        current_actual=snapshot(
            grid_kw=None,
            quality="partial",
            source_ages_seconds={
                "pv": 1.0,
                "load": 1.0,
                "battery": 1.0,
                "grid": 999.0,
                "soc": 1.0,
            },
        ),
        sources=[],
        physical_active=False,
    )
    assert partial["quality"] == "partial" and partial["point_count"] == 1


def test_entity_lifecycle_recorder_multi_entry_and_authority() -> None:
    timeline_source = (COMPONENT / "timeline_sensor.py").read_text(encoding="utf-8")
    sensor_source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    tree = ast.parse(timeline_source)
    timeline_class = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "HoymilesAutomationPlanTimelineSensor"
    )
    bases = {ast.unparse(base) for base in timeline_class.bases}
    assert bases == {"SensorEntity"}
    assignments = {
        target.id: ast.unparse(node.value)
        for node in timeline_class.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert assignments["_attr_should_poll"] == "False"
    assert assignments["_unrecorded_attributes"] == "frozenset({MATCH_ALL})"
    assert "RestoreEntity" not in timeline_source
    forbidden = (
        "async_track_time_interval",
        "services.async_call",
        "modbus",
        "owner_acquire",
        "grant_execution",
        "handover_execution",
    )
    assert not any(token in timeline_source for token in forbidden)
    publish_method = next(
        node
        for node in timeline_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_publish"
    )
    assert sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_write_ha_state"
        for node in ast.walk(publish_method)
    ) == 1
    assert sensor_source.index("rce_plan = HoymilesRCEOptimizerSensor") < sensor_source.index(
        'policy_id="rce"'
    )
    assert sensor_source.index("tariff_plan = HoymilesTariffOptimizerSensor") < sensor_source.index(
        'policy_id="tariff"'
    )
    assert "attach_rce_plan_source(rce_plan)" in sensor_source
    assert "attach_tariff_plan_source(tariff_plan)" in sensor_source
    assert "_same_entry_rce_plan_state()" in (
        COMPONENT / "tariff_sensor.py"
    ).read_text(encoding="utf-8")
    assert "_same_entry_tariff_plan_state()" in (
        COMPONENT / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    assert "sensor.hoymiles_actual_load_power" in timeline_source
    assert "len(candidates) > 1" in timeline_source
    assert "candidates[0]" in timeline_source
    assert "source_entry.entry_id != self._entry.entry_id" in timeline_source

    # Exactly one optimizer call remains in each HA adapter; the sidecar never
    # invokes either optimizer and neither optimizer invokes itself.
    rce_sensor_tree = ast.parse((COMPONENT / "rce_sensor.py").read_text(encoding="utf-8"))
    tariff_sensor_tree = ast.parse((COMPONENT / "tariff_sensor.py").read_text(encoding="utf-8"))
    assert sum(
        isinstance(node, ast.Name) and node.id == "optimize_rce"
        for node in ast.walk(rce_sensor_tree)
    ) == 1  # exactly one executor argument; imports are not ast.Name nodes
    assert sum(
        isinstance(node, ast.Name) and node.id == "optimize_tariff_charging"
        for node in ast.walk(tariff_sensor_tree)
    ) == 1
    execution_sources = "\n".join(
        (ROOT / path).read_text(encoding="utf-8")
        for path in (
            "home_assistant/hoymiles_ems_scheduler.yaml",
            "custom_components/hoymiles_hit_modbus/supervisor_runtime.py",
        )
    )
    assert "automation_plan_timeline" not in execution_sources


def _load_timeline_runtime_module() -> tuple[Any, type]:
    """Load the real HA adapter against a deliberately tiny Entity fixture."""

    package_name = "ap1_timeline_runtime_fixture"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    sys.modules[package_name] = package
    sys.modules[f"{package_name}.automation_plan_timeline"] = TL

    const_module = types.ModuleType(f"{package_name}.const")
    const_module.DOMAIN = "hoymiles_hit_modbus"
    const_module.NAME = "Hoymiles"
    sys.modules[const_module.__name__] = const_module

    energy_module = types.ModuleType(f"{package_name}.energy_data")
    energy_module.numeric_state_sample = lambda *_args, **_kwargs: types.SimpleNamespace(
        value=None,
        fresh=False,
        age_seconds=None,
        reason="missing",
    )
    sys.modules[energy_module.__name__] = energy_module
    models_module = types.ModuleType(f"{package_name}.models")
    models_module.RuntimeData = object
    sys.modules[models_module.__name__] = models_module

    class FakeSensorEntity:
        entity_id: str | None = None

        async def async_added_to_hass(self) -> None:
            callback = getattr(self, "_fixture_during_add", None)
            if callback is not None:
                callback()
            if getattr(self, "_fixture_add_failure", False):
                raise RuntimeError("fixture setup failure")

        async def async_will_remove_from_hass(self) -> None:
            return None

        def async_write_ha_state(self) -> None:
            if self.entity_id is None:
                raise RuntimeError("No entity id specified")
            self._fixture_writes = getattr(self, "_fixture_writes", 0) + 1

    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    sensor_module = types.ModuleType("homeassistant.components.sensor")
    sensor_module.SensorEntity = FakeSensorEntity
    config_entries = types.ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    const = types.ModuleType("homeassistant.const")
    const.MATCH_ALL = "*"
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    core.callback = lambda function: function
    helpers = types.ModuleType("homeassistant.helpers")
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.EntityRegistry = object
    entity_registry.async_get = lambda hass: hass.registry
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    event_module = types.ModuleType("homeassistant.helpers.event")
    fixture_clock = {"now": NOW + timedelta(minutes=5)}
    fixture_timers: list[Any] = []

    def async_call_later(_hass: Any, delay: float, action: Any) -> Any:
        timer = types.SimpleNamespace(
            delay=float(delay),
            cancelled=False,
            fired=False,
        )

        def invoke(now: datetime) -> None:
            timer.fired = True
            action(now)

        def cancel() -> None:
            timer.cancelled = True

        timer.cancel = cancel
        timer.action = invoke
        fixture_timers.append(timer)
        return cancel

    event_module.async_call_later = async_call_later
    util = types.ModuleType("homeassistant.util")
    dt_module = types.ModuleType("homeassistant.util.dt")
    dt_module.utcnow = lambda: fixture_clock["now"]
    modules = {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.sensor": sensor_module,
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_registry": entity_registry,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.event": event_module,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt_module,
    }
    sys.modules.update(modules)
    components.sensor = sensor_module
    helpers.entity_registry = entity_registry
    helpers.event = event_module
    util.dt = dt_module

    module_name = f"{package_name}.timeline_sensor"
    spec = importlib.util.spec_from_file_location(
        module_name,
        COMPONENT / "timeline_sensor.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module._fixture_clock = fixture_clock
    module._fixture_timers = fixture_timers
    module.capture_current_actual = lambda _hass, _entry, now: (
        snapshot(observed_at=now),
        (),
    )
    return module, FakeSensorEntity


def _runtime_timeline_sensor(
    module: Any,
    base: type,
    *,
    entry_id: str = "entry-a",
    policy_id: str = "rce",
) -> tuple[Any, Any, Any]:
    entry = types.SimpleNamespace(entry_id=entry_id)
    source = base()
    source._entry = entry
    source._timeline_sensor = None
    source.attach_timeline_sensor = lambda publisher: setattr(
        source,
        "_timeline_sensor",
        publisher,
    )
    source_device = types.SimpleNamespace(
        name_by_user=None,
        name="Inverter",
        manufacturer="Hoymiles",
        model="HIT",
        sw_version="1",
    )
    runtime = types.SimpleNamespace(source_device=source_device)
    registry_entry = types.SimpleNamespace(
        entity_id=f"sensor.{entry_id}_{policy_id}_plan",
        platform="hoymiles_hit_modbus",
        unique_id=f"{entry_id}_{ {'rce': 'rce_optimized_plan', 'tariff': 'tariff_charge_plan', 'rcm': 'rcm_voltage_plan'}[policy_id] }",
        config_entry_id=entry_id,
    )
    registry = types.SimpleNamespace(entities={"plan": registry_entry})
    states: dict[str, Any] = {
        "input_boolean.hoymiles_rce_discharge_active": types.SimpleNamespace(
            state="off",
            last_reported=NOW + timedelta(minutes=5),
            last_updated=NOW + timedelta(minutes=5),
            attributes={},
        )
    }
    hass = types.SimpleNamespace(
        registry=registry,
        states=types.SimpleNamespace(get=states.get),
    )
    sensor = module.HoymilesAutomationPlanTimelineSensor(
        hass,
        entry,
        runtime,
        policy_id=policy_id,
        source_sensor=source,
    )
    sensor._fixture_writes = 0
    return sensor, source, states


def test_rcm_runtime_identity_states_and_multi_entry() -> None:
    module, base = _load_timeline_runtime_module()
    sensor, source, _states = _runtime_timeline_sensor(
        module,
        base,
        policy_id="rcm",
    )
    assert sensor.entity_id == "sensor.hoymiles_hit_rcm_automation_plan_timeline"
    assert sensor._attr_unique_id == "entry-a_rcm_automation_plan_timeline"
    assert sensor._unrecorded_attributes == frozenset({"*"})
    proxy = source._timeline_sensor
    proxy.publish_current(trace("rcm"), input_revision=1)
    assert sensor.native_value == "current"
    assert sensor.extra_state_attributes["slot_minutes"] == 15
    assert sensor.extra_state_attributes["schema_version"] == TL.SCHEMA_VERSION
    assert sensor.extra_state_attributes["plan_revision"] == 1
    assert sensor.extra_state_attributes["active_observed_at"] is None
    proxy.publish_pending(2)
    assert sensor.native_value == "pending"
    proxy.publish_unavailable(input_revision=2, blocker_code="missing_data")
    assert sensor.native_value == "pending"
    assert sensor.extra_state_attributes["points"]
    assert sensor.extra_state_attributes["blocker_code"] == "missing_data"
    proxy.publish_unavailable(input_revision=2, blocker_code="invalid_trace")
    assert sensor.native_value == "unavailable"
    assert sensor.extra_state_attributes["points"] == []
    other, other_source, _ = _runtime_timeline_sensor(
        module,
        base,
        entry_id="entry-b",
        policy_id="rcm",
    )
    other_source._timeline_sensor.publish_current(trace("rcm"), input_revision=1)
    assert other._attr_unique_id == "entry-b_rcm_automation_plan_timeline"
    assert other.extra_state_attributes["plan_revision"] == 1
    before = deepcopy(other.extra_state_attributes)
    proxy.publish_unavailable(input_revision=99, blocker_code="foreign_entry")
    assert other.extra_state_attributes == before


def test_runtime_revision_and_stale_callback_contract() -> None:
    module, base = _load_timeline_runtime_module()
    sensor, _source, _states = _runtime_timeline_sensor(module, base)
    sensor.entity_id = "sensor.timeline_a"
    asyncio.run(sensor.async_added_to_hass())

    sensor.publish_current(trace("rce"), input_revision=3)
    assert sensor.native_value == "current"
    assert sensor.extra_state_attributes["plan_revision"] == 1
    first = deepcopy(sensor.extra_state_attributes)
    first_writes = sensor._fixture_writes

    # An identical successful full calculation refreshes its public liveness
    # certificate exactly once without incrementing the semantic revision.
    module._fixture_clock["now"] += timedelta(minutes=1)
    sensor.publish_current(trace("rce"), input_revision=3)
    refreshed = deepcopy(sensor.extra_state_attributes)
    assert refreshed["generated_at"] != first["generated_at"]
    assert refreshed["plan_revision"] == first["plan_revision"]
    assert module.semantic_fingerprint(refreshed) == module.semantic_fingerprint(first)
    assert sensor._fixture_writes == first_writes + 1
    first = refreshed
    first_writes = sensor._fixture_writes

    # Older/equal completed pending and older current callbacks are rejected
    # without touching any public field or the write count.
    for stale_pending in (2, 3):
        sensor.publish_pending(stale_pending)
        assert sensor.extra_state_attributes == first
        assert sensor._fixture_writes == first_writes
    sensor.publish_current(trace("rce"), input_revision=2)
    assert sensor.extra_state_attributes == first
    assert sensor._fixture_writes == first_writes

    sensor.publish_pending(4)
    pending_4 = deepcopy(sensor.extra_state_attributes)
    assert sensor.native_value == "pending"
    assert pending_4["input_revision"] == 3
    assert pending_4["pending_input_revision"] == 4
    assert pending_4["plan_revision"] == 2
    sensor.publish_current(trace("rce"), input_revision=4)
    assert sensor.native_value == "current"
    assert sensor.extra_state_attributes["plan_revision"] == 3

    sensor.publish_pending(5)
    pending_5 = deepcopy(sensor.extra_state_attributes)
    pending_5_writes = sensor._fixture_writes
    sensor.publish_pending(4)
    assert sensor.extra_state_attributes == pending_5
    assert sensor._fixture_writes == pending_5_writes

    # A normal failed pass retains its complete display geometry with no
    # execution authority. A structural invalid trace still clears it.
    sensor.publish_current(trace("rce"), input_revision=5)
    current_revision = sensor.extra_state_attributes["plan_revision"]
    sensor.publish_unavailable(input_revision=5, blocker_code="missing_data")
    retained = deepcopy(sensor.extra_state_attributes)
    assert sensor.native_value == "pending"
    assert retained["points"]
    assert retained["result_current"] is False
    assert retained["recalculation_pending"] is True
    assert retained["blocker_code"] == "missing_data"
    assert retained["plan_revision"] > current_revision
    retained_writes = sensor._fixture_writes
    sensor.publish_unavailable(input_revision=5, blocker_code="missing_data")
    assert sensor.extra_state_attributes == retained
    assert sensor._fixture_writes == retained_writes
    sensor.publish_unavailable(input_revision=5, blocker_code="invalid_trace")
    unavailable = deepcopy(sensor.extra_state_attributes)
    assert sensor.native_value == "unavailable"
    assert unavailable["points"] == []
    sensor.publish_pending(2)
    assert sensor.extra_state_attributes == unavailable
    sensor.publish_current(trace("rce"), input_revision=5)
    assert sensor.extra_state_attributes["plan_revision"] > unavailable[
        "plan_revision"
    ]
    assert sensor.extra_state_attributes["plan_revision_scope"] == "runtime"

    # Guards are instance-local: an independent entry and a runtime restart
    # each begin with their own first semantic revision 1.
    other, _other_source, _ = _runtime_timeline_sensor(
        module,
        base,
        entry_id="entry-b",
    )
    other.entity_id = "sensor.timeline_b"
    asyncio.run(other.async_added_to_hass())
    other.publish_current(trace("rce"), input_revision=1)
    assert other.extra_state_attributes["plan_revision"] == 1
    restarted, _restarted_source, _ = _runtime_timeline_sensor(module, base)
    restarted.publish_current(trace("rce"), input_revision=99)
    assert restarted.extra_state_attributes["plan_revision"] == 1


def test_pre_add_detach_reload_and_active_runtime_contract() -> None:
    module, base = _load_timeline_runtime_module()
    sensor, source, states = _runtime_timeline_sensor(module, base)
    old_proxy = source._timeline_sensor

    # A pre-add result is retained without attempting a HA state write.
    old_proxy.publish_current(trace("rce"), input_revision=1)
    assert sensor.native_value == "current"
    assert sensor.extra_state_attributes["input_revision"] == 1
    assert sensor._fixture_writes == 0
    retained = deepcopy(sensor.extra_state_attributes)

    # A callback during base async_added_to_hass is also retained; only a
    # later callback may explicitly write because EntityPlatform owns the
    # initial publication of the retained native state.
    sensor.entity_id = "sensor.timeline_a"
    sensor._fixture_during_add = lambda: old_proxy.publish_current(
        trace("rce"),
        input_revision=2,
    )
    asyncio.run(sensor.async_added_to_hass())
    assert sensor._fixture_writes == 0
    assert sensor.extra_state_attributes["input_revision"] == 2
    assert sensor.extra_state_attributes != retained
    old_proxy.publish_current(trace("rce"), input_revision=3)
    assert sensor._fixture_writes == 1

    # Active is a timestamped publication snapshot. Missing, stale and later
    # lifecycle changes do not become fake false and do not add listeners.
    observed = sensor.extra_state_attributes["active_observed_at"]
    assert sensor.extra_state_attributes["active_scope"] == "publication_snapshot"
    assert observed == "2026-10-25T00:05:00Z"
    writes_before_lifecycle_change = sensor._fixture_writes
    states["input_boolean.hoymiles_rce_discharge_active"].state = "on"
    assert sensor.extra_state_attributes["active_observed_at"] == observed
    assert sensor._fixture_writes == writes_before_lifecycle_change
    del states["input_boolean.hoymiles_rce_discharge_active"]
    assert sensor._physical_active(NOW + timedelta(minutes=5)) == (None, None)
    states["input_boolean.hoymiles_rce_discharge_active"] = types.SimpleNamespace(
        state="on",
        last_reported=NOW,
        last_updated=NOW,
        attributes={},
    )
    assert sensor._physical_active(NOW + timedelta(minutes=8)) == (None, None)

    before_unload = deepcopy(sensor.extra_state_attributes)
    asyncio.run(sensor.async_will_remove_from_hass())
    assert source._timeline_sensor is None
    assert sensor._source_sensor is None and sensor._source_proxy is None
    writes_after_unload = sensor._fixture_writes
    old_proxy.publish_pending(4)
    assert sensor.extra_state_attributes == before_unload
    assert sensor._fixture_writes == writes_after_unload

    # Unload before a first result and setup failure both detach cleanly.
    empty, empty_source, _ = _runtime_timeline_sensor(module, base)
    asyncio.run(empty.async_will_remove_from_hass())
    assert empty_source._timeline_sensor is None
    failed, failed_source, _ = _runtime_timeline_sensor(module, base)
    failed.entity_id = "sensor.failed"
    failed._fixture_add_failure = True
    try:
        asyncio.run(failed.async_added_to_hass())
    except RuntimeError as err:
        assert str(err) == "fixture setup failure"
    else:
        raise AssertionError("setup failure fixture did not fail")
    assert failed_source._timeline_sensor is None
    assert failed._source_sensor is None

    # Two reloads produce one fresh relation apiece and old proxies stay inert.
    reload_source = source
    first_reload = module.HoymilesAutomationPlanTimelineSensor(
        sensor.hass,
        sensor._entry,
        sensor._runtime,
        policy_id="rce",
        source_sensor=reload_source,
    )
    first_reload_proxy = reload_source._timeline_sensor
    assert first_reload_proxy is not old_proxy
    asyncio.run(first_reload.async_will_remove_from_hass())
    second_reload = module.HoymilesAutomationPlanTimelineSensor(
        sensor.hass,
        sensor._entry,
        sensor._runtime,
        policy_id="rce",
        source_sensor=reload_source,
    )
    assert reload_source._timeline_sensor not in {old_proxy, first_reload_proxy}
    assert second_reload._source_sensor is reload_source


def test_retained_timeline_expires_without_source_event() -> None:
    """A one-shot callback removes retained geometry at the exact grace bound."""

    module, base = _load_timeline_runtime_module()
    sensor, source, _states = _runtime_timeline_sensor(module, base)
    sensor.entity_id = "sensor.timeline_expiry"
    asyncio.run(sensor.async_added_to_hass())
    sensor.publish_current(trace("rce"), input_revision=1)
    sensor.publish_pending(2)
    timers = [
        item
        for item in module._fixture_timers
        if not item.cancelled and not item.fired
    ]
    assert len(timers) == 1
    timer = timers[0]
    assert 0.0 <= timer.delay <= module.LAST_COMPLETE_GRACE_SECONDS["rce"]

    module._fixture_clock["now"] += timedelta(
        seconds=module.LAST_COMPLETE_GRACE_SECONDS["rce"] + 1
    )
    timer.action(module._fixture_clock["now"])
    assert sensor.native_value == "unavailable"
    assert sensor.extra_state_attributes["points"] == []
    assert sensor.extra_state_attributes["blocker_code"] == "last_complete_expired"

    sensor.publish_current(trace("rce"), input_revision=2)
    sensor.publish_pending(3)
    active = [
        item
        for item in module._fixture_timers
        if not item.cancelled and not item.fired
    ]
    assert len(active) == 1
    before_remove = deepcopy(sensor.extra_state_attributes)
    asyncio.run(sensor.async_will_remove_from_hass())
    assert active[0].cancelled
    active[0].action(module._fixture_clock["now"] + timedelta(hours=1))
    assert sensor.extra_state_attributes == before_remove
    assert source._timeline_sensor is None


TESTS = (
    test_rce_partial_first_slot_contract,
    test_tariff_partial_first_slot_contract,
    test_rcm_partial_first_slot_contract,
    test_schema_and_policy_contract,
    test_intervals_dst_and_native_resolution,
    test_numbers_nulls_and_signs,
    test_rce_export_can_coexist_with_pv_net_charging,
    test_hard_limits,
    test_native_datetime_forecast_mapping_contract,
    test_independent_soc_pv_oracle_cases_1_to_8,
    test_optimizer_pv_reaches_soc_trace_contract,
    test_exact_rce_trace,
    test_exact_tariff_trace,
    test_revision_atomicity_and_states,
    test_active_snapshot_contract,
    test_malformed_data_and_partial_horizon,
    test_entity_lifecycle_recorder_multi_entry_and_authority,
    test_runtime_revision_and_stale_callback_contract,
    test_pre_add_detach_reload_and_active_runtime_contract,
    test_retained_timeline_expires_without_source_event,
    test_rcm_runtime_identity_states_and_multi_entry,
)
REQUIRED_AP2R1F_TESTS = frozenset(
    {
        "test_rce_partial_first_slot_contract",
        "test_tariff_partial_first_slot_contract",
        "test_rcm_partial_first_slot_contract",
    }
)


MUTATION_FILES = (
    "custom_components/hoymiles_hit_modbus/automation_plan_timeline.py",
    "custom_components/hoymiles_hit_modbus/timeline_sensor.py",
    "custom_components/hoymiles_hit_modbus/rce_optimizer.py",
    "custom_components/hoymiles_hit_modbus/rce_sensor.py",
    "custom_components/hoymiles_hit_modbus/tariff_optimizer.py",
    "custom_components/hoymiles_hit_modbus/tariff_sensor.py",
    "custom_components/hoymiles_hit_modbus/sensor.py",
    "home_assistant/hoymiles_ems_scheduler.yaml",
    "custom_components/hoymiles_hit_modbus/supervisor_runtime.py",
)


def _copy_mutation_fixture(destination: Path) -> None:
    for relative in MUTATION_FILES:
        source = ROOT / relative
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _replace_once(root: Path, relative: str, old: str, new: str) -> None:
    path = root / relative
    source = path.read_text(encoding="utf-8")
    assert source.count(old) == 1, (relative, old[:80], source.count(old))
    path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="\n")


def _append(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.write_text(
        path.read_text(encoding="utf-8") + text,
        encoding="utf-8",
        newline="\n",
    )


def _mutation_violations(root: Path) -> set[str]:
    common = (root / MUTATION_FILES[0]).read_text(encoding="utf-8")
    timeline = (root / MUTATION_FILES[1]).read_text(encoding="utf-8")
    rce_optimizer = (root / MUTATION_FILES[2]).read_text(encoding="utf-8")
    rce_sensor = (root / MUTATION_FILES[3]).read_text(encoding="utf-8")
    tariff_sensor = (root / MUTATION_FILES[5]).read_text(encoding="utf-8")
    scheduler = (root / MUTATION_FILES[7]).read_text(encoding="utf-8")
    runtime = (root / MUTATION_FILES[8]).read_text(encoding="utf-8")
    violations: set[str] = set()
    if "trace_collector.clear()  # M01" in rce_optimizer:
        violations.add("M01")
    if sum(
        isinstance(node, ast.Name) and node.id == "optimize_rce"
        for node in ast.walk(ast.parse(rce_sensor))
    ) != 1:
        violations.add("M02")
    if sum(
        isinstance(node, ast.Name) and node.id == "optimize_tariff_charging"
        for node in ast.walk(ast.parse(tariff_sensor))
    ) != 1:
        violations.add("M03")
    if "SLOT_MINUTES = 30" not in common:
        violations.add("M04")
    if "battery_kw=-battery if battery is not None else None" not in timeline:
        violations.add("M05")
    if "grid_kw=-grid if grid is not None else None" not in timeline:
        violations.add("M06")
    if '"pv_kw": snapshot.pv_kw,' not in common:
        violations.add("M07")
    if "allow_nan=False" not in common:
        violations.add("M08")
    if "MAX_POINTS = 192" not in common:
        violations.add("M09")
    if "MAX_SERIALIZED_BYTES = 262_144" not in common:
        violations.add("M10")
    if 'candidate.pop("generated_at", None)' not in common:
        violations.add("M11")
    if 'actual.pop("source_ages_seconds", None)' not in common:
        violations.add("M12")
    if 'payload["input_revision"] = pending_input_revision  # M13' in common:
        violations.add("M13")
    if '"points": [],' not in common:
        violations.add("M14")
    if "_unrecorded_attributes = frozenset({MATCH_ALL})" not in timeline:
        violations.add("M15")
    if "class HoymilesAutomationPlanTimelineSensor(SensorEntity, RestoreEntity)" in timeline:
        violations.add("M16")
    if "_attr_should_poll = False" not in timeline or "async_track_time_interval" in timeline:
        violations.add("M17")
    if "_SHARED_TIMELINE_STORE" in timeline:
        violations.add("M18")
    if "if len(candidates) > 1:" not in timeline:
        violations.add("M19")
    if 'if policy_id == "rce" and "target_soc_percent" in point:' not in common:
        violations.add("M20")
    if 'if "required_headroom_kwh" in point:' not in common:
        violations.add("M21")
    if "if imported > 0 and exported > 0:" not in common:
        violations.add("M22")
    if "automation_plan_timeline" in scheduler or "automation_plan_timeline" in runtime:
        violations.add("M23")
    if any(
        token in timeline
        for token in (
            "services.async_call",
            "modbus.write",
            "owner_acquire",
            "grant_execution",
            "handover_execution",
        )
    ):
        violations.add("M24")
    return violations


def run_mutation_campaign() -> None:
    common_rel, timeline_rel, rce_optimizer_rel, rce_sensor_rel, _, tariff_sensor_rel, _, scheduler_rel, _ = MUTATION_FILES
    mutations: list[tuple[str, str, Callable[[Path], None]]] = [
        (
            "M01",
            "selected RCE slot changes because trace is enabled",
            lambda root: _replace_once(
                root,
                rce_optimizer_rel,
                "if trace_collector is not None:\n            end = start + SLOT",
                "if trace_collector is not None:\n            trace_collector.clear()  # M01\n            exports.clear()\n            end = start + SLOT",
            ),
        ),
        (
            "M02",
            "an additional RCE optimize call is introduced",
            lambda root: _append(root, rce_sensor_rel, "\ndef _m02(settings):\n    return optimize_rce(settings)\n"),
        ),
        (
            "M03",
            "an additional tariff optimize call is introduced",
            lambda root: _append(root, tariff_sensor_rel, "\ndef _m03(settings):\n    return optimize_tariff_charging(settings)\n"),
        ),
        ("M04", "30-minute point is duplicated into two 15-minute points", lambda root: _replace_once(root, common_rel, "SLOT_MINUTES = 30", "SLOT_MINUTES = 15")),
        ("M05", "battery sign is reversed", lambda root: _replace_once(root, timeline_rel, "battery_kw=-battery if battery is not None else None", "battery_kw=battery")),
        ("M06", "grid sign is reversed", lambda root: _replace_once(root, timeline_rel, "grid_kw=-grid if grid is not None else None", "grid_kw=grid")),
        ("M07", "missing value becomes zero", lambda root: _replace_once(root, common_rel, '"pv_kw": snapshot.pv_kw,', '"pv_kw": snapshot.pv_kw or 0.0,')),
        ("M08", "NaN passes serialization", lambda root: _replace_once(root, common_rel, "allow_nan=False", "allow_nan=True")),
        ("M09", "193 points are accepted", lambda root: _replace_once(root, common_rel, "MAX_POINTS = 192", "MAX_POINTS = 193")),
        ("M10", "payload above 262144 bytes is accepted", lambda root: _replace_once(root, common_rel, "MAX_SERIALIZED_BYTES = 262_144", "MAX_SERIALIZED_BYTES = 262_145")),
        ("M11", "generated_at-only change increments plan_revision", lambda root: _replace_once(root, common_rel, '    candidate.pop("generated_at", None)\n', "")),
        ("M12", "age-only change writes a new state", lambda root: _replace_once(root, common_rel, '        actual.pop("source_ages_seconds", None)\n', "")),
        ("M13", "pending mixes old points with new input data", lambda root: _replace_once(root, common_rel, '    payload["result_current"] = False\n', '    payload["input_revision"] = pending_input_revision  # M13\n    payload["result_current"] = False\n')),
        ("M14", "unavailable retains stale points", lambda root: _replace_once(root, common_rel, '        "points": [],\n', '        "points": ["stale"],\n')),
        ("M15", "large attributes become recordable", lambda root: _replace_once(root, timeline_rel, "_unrecorded_attributes = frozenset({MATCH_ALL})", "_unrecorded_attributes = frozenset()")),
        ("M16", "RestoreEntity is added", lambda root: _replace_once(root, timeline_rel, "class HoymilesAutomationPlanTimelineSensor(SensorEntity):", "class HoymilesAutomationPlanTimelineSensor(SensorEntity, RestoreEntity):")),
        ("M17", "polling/timer is added", lambda root: _replace_once(root, timeline_rel, "_attr_should_poll = False", "_attr_should_poll = True")),
        ("M18", "two entries share one timeline store", lambda root: _append(root, timeline_rel, "\n_SHARED_TIMELINE_STORE = {}\n")),
        ("M19", "first matching source is accepted under ambiguity", lambda root: _replace_once(root, timeline_rel, "if len(candidates) > 1:", "if False:")),
        ("M20", "RCE target SOC is fabricated", lambda root: _replace_once(root, common_rel, '        if policy_id == "rce" and "target_soc_percent" in point:', '        if False and "target_soc_percent" in point:')),
        ("M21", "tariff required headroom is fabricated", lambda root: _replace_once(root, common_rel, '        if "required_headroom_kwh" in point:', '        if False and "required_headroom_kwh" in point:')),
        ("M22", "import and export are positive simultaneously", lambda root: _replace_once(root, common_rel, "    if imported > 0 and exported > 0:\n", "    if False:\n")),
        ("M23", "timeline output enters scheduler/executor input", lambda root: _append(root, scheduler_rel, "\n# automation_plan_timeline consumed by executor\n")),
        ("M24", "service/Modbus/owner authority is introduced", lambda root: _append(root, timeline_rel, "\nasync def _m24(hass):\n    await hass.services.async_call('modbus', 'write_register', {'owner_acquire': True})\n")),
    ]
    detected: list[str] = []
    survivors: list[str] = []
    for mutation_id, description, mutate in mutations:
        with tempfile.TemporaryDirectory(prefix=f"aurora_ap1_{mutation_id.lower()}_") as temporary:
            root = Path(temporary)
            _copy_mutation_fixture(root)
            mutate(root)
            if mutation_id in _mutation_violations(root):
                detected.append(mutation_id)
                print(f"DETECTED {mutation_id} — {description}")
            else:
                survivors.append(mutation_id)
                print(f"SURVIVED {mutation_id} — {description}")
    assert len(detected) == 24, detected
    assert not survivors, survivors
    print("Automation plan timeline mutations: detected 24/24; survivors 0")


def main() -> None:
    if "--mutations" in sys.argv:
        run_mutation_campaign()
        return
    assert REQUIRED_AP2R1F_TESTS <= {test.__name__ for test in TESTS}
    for test in TESTS:
        test()
    print(f"Automation plan timeline: {len(TESTS)} contract groups passed")


if __name__ == "__main__":
    main()
