"""Deterministic AP-2R1 RCEm timeline and mutation contracts."""

from __future__ import annotations

import ast
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import types
from typing import Any, Callable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))

import automation_plan_timeline as TL  # noqa: E402
import rcm_optimizer as RCM  # noqa: E402


def _load_model() -> Any:
    package_name = "ap2r1_rcm_model_fixture"
    package = types.ModuleType(package_name)
    package.__path__ = [str(COMPONENT)]
    sys.modules[package_name] = package
    sys.modules[f"{package_name}.automation_plan_timeline"] = TL
    sys.modules[f"{package_name}.rcm_optimizer"] = RCM
    name = f"{package_name}.rcm_timeline_model"
    spec = importlib.util.spec_from_file_location(name, COMPONENT / "rcm_timeline_model.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


MODEL = _load_model()
UTC = timezone.utc
NOW = datetime(2026, 8, 8, 10, 0, tzinfo=UTC)


def optimizer_settings(**overrides: Any) -> RCM.RCMOptimizerInput:
    values: dict[str, Any] = {
        "now": NOW,
        "voltage_l1_v": 238.0,
        "voltage_l2_v": 239.0,
        "voltage_l3_v": 240.0,
        "filtered_voltage_v": 240.0,
        "rolling_10m_voltage_v": 240.0,
        "historical_p90_voltage_v": 250.0,
        "risk_windows": ((12 * 60 + 30, 14 * 60 + 15, 254.0),),
        "history_days": 4,
        "pv_power_kw": 8.0,
        "load_power_kw": 2.0,
        "grid_export_power_kw": 5.5,
        "battery_capacity_kwh": 21.0,
        "battery_soc_percent": 70.0,
        "reserve_soc_percent": 25.0,
        "safety_margin_soc_percent": 2.0,
        "protected_minimum_soc_percent": 35.0,
        "expected_risk_surplus_kwh": 8.0,
        "expected_natural_headroom_kwh": 0.4,
        "minutes_to_risk": 90,
        "risk_day_offset": 0,
        "system_power_kw": 10.0,
        "battery_voltage_v": 52.0,
        "bms_max_charge_current_a": 175.0,
        "bms_max_discharge_current_a": 175.0,
        "current_charge_limit_percent": 80.0,
        "saved_charge_limit_percent": 100.0,
        "export_control_enabled": True,
        "current_export_limit_percent": 50.0,
        "saved_export_limit_percent": 50.0,
        "user_export_cap_percent": 60.0,
        "charge_efficiency_percent": 95.0,
    }
    values.update(overrides)
    return RCM.RCMOptimizerInput(**values)


BASE_RESULT = RCM.optimize_rcm(optimizer_settings())


def energy_points(
    count: int = 96,
    *,
    start: datetime = NOW,
    pv_kwh: float | None = 0.45,
    load_kwh: float | None = 0.20,
) -> tuple[Any, ...]:
    return tuple(
        MODEL.RCMTimelineEnergyPoint(
            start=start + timedelta(minutes=15 * index),
            end=start + timedelta(minutes=15 * (index + 1)),
            pv_kwh=pv_kwh,
            load_kwh=load_kwh,
        )
        for index in range(count)
    )


def risk_intervals(*, overlapping: bool = False) -> tuple[Any, ...]:
    items = [
        MODEL.RCMTimelineRiskInterval(
            start=NOW + timedelta(hours=2),
            end=NOW + timedelta(hours=4),
            voltage_risk_code="historical_risk_window",
            required_headroom_kwh=4.2,
            headroom_shortfall_kwh=1.1,
        )
    ]
    if overlapping:
        items.append(
            MODEL.RCMTimelineRiskInterval(
                start=NOW + timedelta(hours=2, minutes=30),
                end=NOW + timedelta(hours=3),
                voltage_risk_code="secondary_risk_window",
                required_headroom_kwh=2.0,
                headroom_shortfall_kwh=0.5,
            )
        )
    return tuple(items)


def model_input(**overrides: Any) -> Any:
    requested_horizon_start = overrides.pop("requested_horizon_start", NOW)
    requested_horizon_end = overrides.pop(
        "requested_horizon_end", NOW + timedelta(hours=24)
    )
    values: dict[str, Any] = {
        "generated_at": NOW,
        "energy_points": energy_points(),
        "risk_intervals": risk_intervals(),
        "battery_capacity_kwh": 21.0,
        "current_soc_percent": 70.0,
        "protected_soc_floor_percent": 35.0,
        "maximum_soc_percent": 100.0,
        "charge_power_limit_kw": 9.1,
        "discharge_power_limit_kw": 9.1,
        "charge_efficiency": 0.95,
        "discharge_efficiency": 0.95,
        "system_power_kw": 10.0,
        "result": BASE_RESULT,
        "inputs_fresh": True,
        "quality": "complete",
    }
    fields = MODEL.RCMTimelineModelInput.__dataclass_fields__
    if "requested_horizon_start" in fields:
        values["requested_horizon_start"] = requested_horizon_start
        values["requested_horizon_end"] = requested_horizon_end
    values.update(overrides)
    return MODEL.RCMTimelineModelInput(**values)


def expect_model_error(call: Callable[[], Any], blocker: str) -> None:
    try:
        call()
    except MODEL.RCMTimelineModelError as err:
        assert err.blocker_code == blocker, (blocker, err.blocker_code)
    else:
        raise AssertionError(f"missing model error: {blocker}")


def payload(trace: Any) -> dict[str, Any]:
    return TL.build_current_payload(
        trace,
        config_entry_id="entry-rcm",
        generated_at=NOW,
        input_revision=8,
        plan_revision=1,
        plan_entity_id="sensor.hoymiles_hit_rcm_voltage_plan",
        current_actual=TL.unavailable_snapshot(),
        sources=[{"role": "plan", "entity_id": "sensor.hoymiles_hit_rcm_voltage_plan"}],
        physical_active=None,
    )


def _sensor_energy_adapter() -> Callable[..., tuple[Any, ...]]:
    source = (COMPONENT / "rcm_sensor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {
        "_utc_quarter_ceiling",
        "_timeline_horizon_bounds",
        "_timeline_energy_points",
    }
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    namespace: dict[str, Any] = {
        "date": date,
        "datetime": datetime,
        "time": time,
        "timedelta": timedelta,
        "timezone": timezone,
        "RCMTimelineEnergyPoint": MODEL.RCMTimelineEnergyPoint,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), "rcm_sensor.py", "exec"), namespace)
    return namespace["_timeline_energy_points"]


def test_profile_energy_mapping_conserves_dst_folds() -> None:
    adapter = _sensor_energy_adapter()
    warsaw = ZoneInfo("Europe/Warsaw")
    profile = (1.0,) * 48
    expected = (
        (date(2026, 8, 8), 96, 48.0, 0),
        (date(2026, 3, 29), 92, 46.0, 0),
        (date(2026, 10, 25), 100, 48.0, 4),
    )
    for target, count, expected_kwh, expected_missing in expected:
        local_midnight = datetime.combine(target, time.min, tzinfo=warsaw)
        points = adapter(
            now=local_midnight,
            target_date=target,
            risk_day_offset=0,
            pv_profile=profile,
            load_profile=profile,
        )
        assert len(points) == count
        assert len({(point.start, point.end) for point in points}) == count
        covered = [point for point in points if point.pv_kwh is not None]
        missing = [point for point in points if point.pv_kwh is None]
        assert abs(sum(float(point.pv_kwh) for point in covered) - expected_kwh) < 1e-8
        assert len(missing) == expected_missing
        assert all(point.load_kwh is None for point in missing)
        assert all(point.pv_kwh != 0.0 and point.load_kwh != 0.0 for point in covered)
    fall_points = adapter(
        now=datetime(2026, 10, 25, 0, 0, tzinfo=warsaw),
        target_date=date(2026, 10, 25),
        risk_day_offset=0,
        pv_profile=profile,
        load_profile=profile,
    )
    repeated = [
        point.start.astimezone(warsaw)
        for point in fall_points
        if point.start.astimezone(warsaw).hour == 2
    ]
    assert {value.utcoffset() for value in repeated} == {
        timedelta(hours=1),
        timedelta(hours=2),
    }


def test_action_windows_overlap_and_point_field_scope() -> None:
    flat = energy_points(count=8, pv_kwh=0.0, load_kwh=0.0)
    restore = replace(
        BASE_RESULT,
        action="restore",
        prediction_ready=True,
        prediction_block_reason="",
        pre_discharge_deadline=None,
    )
    natural = payload(MODEL.build_rcm_timeline_trace(model_input(
        energy_points=flat,
        risk_intervals=(),
        result=restore,
        requested_horizon_end=NOW + timedelta(hours=2),
    )))
    assert all(point["battery_kw"] == 0.0 for point in natural["points"])
    assert all(point["soc_percent"] == point["baseline_soc_percent"] for point in natural["points"])
    assert all(point["action_code"] == "idle" and not point["selected"] for point in natural["points"])

    pre = replace(
        restore,
        action="grid_discharge_preparation",
        pre_discharge_deadline=NOW + timedelta(minutes=45),
        pre_discharge_power_kw=2.0,
        pre_discharge_target_soc_percent=55.0,
    )
    prepared = payload(MODEL.build_rcm_timeline_trace(model_input(
        generated_at=NOW + timedelta(minutes=15),
        energy_points=flat,
        risk_intervals=(),
        result=pre,
        requested_horizon_end=NOW + timedelta(hours=2),
    )))
    assert prepared["points"][0]["battery_kw"] == 0.0
    assert prepared["points"][0]["action_code"] == "idle"
    assert prepared["points"][1]["battery_kw"] == -2.0
    assert prepared["points"][2]["battery_kw"] == -2.0
    assert prepared["points"][3]["battery_kw"] == 0.0
    assert prepared["points"][3]["action_code"] == "idle"
    assert prepared["points"][3]["soc_percent"] < prepared["points"][3]["baseline_soc_percent"]
    assert prepared["points"][0]["policy"]["planned_pre_discharge_kw"] is None
    assert prepared["points"][1]["policy"]["planned_pre_discharge_kw"] == 2.0
    assert prepared["points"][3]["policy"]["planned_pre_discharge_kw"] is None

    partial = replace(pre, pre_discharge_deadline=NOW + timedelta(minutes=22, seconds=30))
    prorated = payload(MODEL.build_rcm_timeline_trace(model_input(
        generated_at=NOW + timedelta(minutes=7, seconds=30),
        energy_points=flat,
        risk_intervals=(),
        result=partial,
        requested_horizon_end=NOW + timedelta(hours=2),
    )))
    assert abs(prorated["points"][0]["battery_kw"] + 1.0) < 1e-8
    assert abs(prorated["points"][1]["battery_kw"] + 1.0) < 1e-8
    assert prorated["points"][0]["policy"]["planned_pre_discharge_kw"] == 1.0
    assert prorated["points"][1]["policy"]["planned_pre_discharge_kw"] == 1.0

    window = (
        MODEL.RCMTimelineRiskInterval(
            start=NOW + timedelta(minutes=30),
            end=NOW + timedelta(minutes=60),
            voltage_risk_code="historical_risk_window",
            required_headroom_kwh=4.2,
            headroom_shortfall_kwh=1.1,
        ),
    )
    surplus = energy_points(count=8, pv_kwh=0.5, load_kwh=0.0)
    for action in ("absorb_pv", "preserve_headroom"):
        result = replace(restore, action=action, recommended_charge_power_kw=0.4)
        current = payload(MODEL.build_rcm_timeline_trace(model_input(
            energy_points=surplus,
            risk_intervals=window,
            result=result,
            requested_horizon_end=NOW + timedelta(hours=2),
        )))
        assert all(current["points"][index]["action_code"] == "idle" for index in (0, 1, 4, 5, 6, 7))
        assert all(current["points"][index]["action_code"] == action for index in (2, 3))
        assert all(current["points"][index]["battery_kw"] == current["points"][index]["pv_kw"] for index in (0, 1))
        assert all(abs(current["points"][index]["battery_kw"] - 0.4) < 1e-8 for index in (2, 3))

    limit = replace(restore, action="limit_export", recommended_export_limit_percent=5.0)
    limited = payload(MODEL.build_rcm_timeline_trace(model_input(
        energy_points=energy_points(count=8, pv_kwh=0.75, load_kwh=0.0),
        risk_intervals=window,
        result=limit,
        current_soc_percent=100.0,
        requested_horizon_end=NOW + timedelta(hours=2),
    )))
    assert all(limited["points"][index]["policy"]["planned_export_limit_percent"] is None for index in (0, 1, 4, 5, 6, 7))
    assert all(limited["points"][index]["policy"]["planned_export_limit_percent"] == 5.0 for index in (2, 3))
    assert limited["points"][0]["grid_export_kw"] == 3.0
    assert limited["points"][2]["grid_export_kw"] == 0.5

    no_window = payload(MODEL.build_rcm_timeline_trace(model_input(
        energy_points=surplus,
        risk_intervals=(),
        result=replace(restore, action="absorb_pv", recommended_charge_power_kw=0.4),
        requested_horizon_end=NOW + timedelta(hours=2),
    )))
    assert all(point["action_code"] == "idle" and not point["selected"] for point in no_window["points"])
    assert all(point["soc_percent"] == point["baseline_soc_percent"] for point in no_window["points"])

    overlap = window + (
        MODEL.RCMTimelineRiskInterval(
            start=NOW + timedelta(minutes=45),
            end=NOW + timedelta(minutes=60),
            voltage_risk_code="secondary_risk_window",
            required_headroom_kwh=2.0,
            headroom_shortfall_kwh=0.5,
        ),
    )
    single = payload(MODEL.build_rcm_timeline_trace(model_input(
        energy_points=surplus,
        risk_intervals=window,
        result=replace(restore, action="absorb_pv", recommended_charge_power_kw=0.4),
        requested_horizon_end=NOW + timedelta(hours=2),
    )))
    doubled = payload(MODEL.build_rcm_timeline_trace(model_input(
        energy_points=surplus,
        risk_intervals=overlap,
        result=replace(restore, action="absorb_pv", recommended_charge_power_kw=0.4),
        requested_horizon_end=NOW + timedelta(hours=2),
    )))
    assert doubled["points"][3]["battery_kw"] == single["points"][3]["battery_kw"]
    assert all("required_headroom_kwh" not in doubled["points"][index] for index in (0, 1, 4, 5, 6, 7))
    assert doubled["points"][2]["required_headroom_kwh"] == 4.2


def test_quality_requires_requested_horizon_and_full_risk_coverage() -> None:
    restore = replace(
        BASE_RESULT,
        action="restore",
        prediction_ready=True,
        prediction_block_reason="",
        pre_discharge_deadline=None,
    )
    short = MODEL.build_rcm_timeline_trace(model_input(
        energy_points=energy_points(4),
        risk_intervals=(),
        result=restore,
        requested_horizon_end=NOW + timedelta(hours=24),
    ))
    assert short.quality == "partial"
    assert short.blocker_code == "planning_horizon_limited"

    partial_risk = (
        MODEL.RCMTimelineRiskInterval(
            start=NOW - timedelta(minutes=15),
            end=NOW + timedelta(hours=2),
            voltage_risk_code="historical_risk_window",
            required_headroom_kwh=2.0,
            headroom_shortfall_kwh=0.5,
        ),
    )
    risk_limited = MODEL.build_rcm_timeline_trace(model_input(
        energy_points=energy_points(4),
        risk_intervals=partial_risk,
        result=restore,
        requested_horizon_end=NOW + timedelta(hours=24),
    ))
    assert risk_limited.quality == "partial"
    assert risk_limited.blocker_code == "risk_window_partially_covered"

    missing = MODEL.build_rcm_timeline_trace(model_input(
        energy_points=energy_points(4),
        risk_intervals=partial_risk,
        result=restore,
        battery_capacity_kwh=None,
        requested_horizon_end=NOW + timedelta(hours=24),
    ))
    assert missing.quality == "partial"
    assert missing.blocker_code == "incomplete_physical_inputs"

    complete = MODEL.build_rcm_timeline_trace(model_input(
        risk_intervals=risk_intervals(),
        result=restore,
    ))
    assert complete.quality == "complete"
    assert complete.blocker_code is None


def test_normal_baseline_and_planned_scenario() -> None:
    trace = MODEL.build_rcm_timeline_trace(model_input())
    current = payload(trace)
    assert current["policy_id"] == "rcm"
    assert current["schema_version"] == 1
    assert current["slot_minutes"] == 15
    assert current["point_count"] == 96
    assert current["horizon_start"] == "2026-08-08T10:00:00Z"
    assert current["horizon_end"] == "2026-08-09T10:00:00Z"
    baseline = [point["baseline_soc_percent"] for point in current["points"]]
    planned = [point["soc_percent"] for point in current["points"]]
    assert all(value is not None for value in baseline)
    assert all(value is not None for value in planned)
    assert min(value for value in baseline if value is not None) >= 35.0
    assert max(value for value in baseline if value is not None) <= 100.0
    assert min(value for value in planned if value is not None) >= 35.0
    assert max(value for value in planned if value is not None) <= 100.0


def test_risk_headroom_target_and_actions() -> None:
    result = replace(
        BASE_RESULT,
        action="grid_discharge_preparation",
        pre_discharge_deadline=NOW + timedelta(hours=1),
        pre_discharge_power_kw=2.0,
        pre_discharge_target_soc_percent=55.0,
        target_soc_before_risk_percent=60.0,
        required_headroom_kwh=4.2,
        headroom_shortfall_kwh=1.1,
    )
    current = payload(MODEL.build_rcm_timeline_trace(model_input(result=result)))
    before_deadline = current["points"][:4]
    assert all(point["battery_kw"] < 0.0 for point in before_deadline)
    assert all(point["target_soc_percent"] == 55.0 for point in before_deadline)
    risk = [
        point
        for point in current["points"]
        if point["policy"]["voltage_risk_code"] == "historical_risk_window"
    ]
    assert risk
    assert all(point["required_headroom_kwh"] == 4.2 for point in risk)
    assert all(point["policy"]["headroom_shortfall_kwh"] == 1.1 for point in risk)
    assert all(point["target_soc_percent"] == 60.0 for point in risk)
    assert all(point["policy"]["planned_pre_discharge_kw"] is None for point in risk)


def test_no_risk_monitor_restore_and_overlap() -> None:
    restore = replace(
        BASE_RESULT,
        action="restore",
        risk_window_plans=(),
        prediction_ready=False,
        prediction_block_reason="history_learning",
    )
    no_risk = payload(
        MODEL.build_rcm_timeline_trace(
            model_input(result=restore, risk_intervals=())
        )
    )
    assert {point["action_code"] for point in no_risk["points"]} == {"idle"}
    assert no_risk["quality"] == "degraded"
    assert no_risk["blocker_code"] == "history_learning"
    assert {
        point["policy"]["voltage_risk_code"] for point in no_risk["points"]
    } == {"no_risk"}
    monitor = replace(restore, action="monitor")
    monitored = payload(
        MODEL.build_rcm_timeline_trace(
            model_input(result=monitor, risk_intervals=risk_intervals(overlapping=True))
        )
    )
    overlap_point = monitored["points"][10]
    assert overlap_point["policy"]["voltage_risk_code"] == "historical_risk_window"


def test_absorb_limit_export_signs_and_balance() -> None:
    absorb = replace(
        BASE_RESULT,
        action="absorb_pv",
        recommended_charge_power_kw=1.0,
    )
    absorbed = payload(MODEL.build_rcm_timeline_trace(model_input(result=absorb)))
    assert any(point["battery_kw"] > 0.0 for point in absorbed["points"])
    limited = replace(
        BASE_RESULT,
        action="limit_export",
        recommended_export_limit_percent=5.0,
    )
    current = payload(MODEL.build_rcm_timeline_trace(model_input(result=limited)))
    for point in current["points"]:
        assert point["grid_import_kw"] >= 0.0
        assert point["grid_export_kw"] >= 0.0
        assert not (point["grid_import_kw"] > 0.0 and point["grid_export_kw"] > 0.0)
        assert abs(
            point["grid_kw"]
            - (point["load_kw"] + point["battery_kw"] - point["pv_kw"])
        ) < 1e-8
        assert point["grid_import_kw"] == max(point["grid_kw"], 0.0)
        assert point["grid_export_kw"] == max(-point["grid_kw"], 0.0)
    risk_points = [point for point in current["points"] if point["selected"]]
    outside = [point for point in current["points"] if not point["selected"]]
    assert all(point["policy"]["planned_export_limit_percent"] == 5.0 for point in risk_points)
    assert all(point["policy"]["planned_export_limit_percent"] is None for point in outside)


def test_efficiency_limits_and_missing_facts() -> None:
    efficient = MODEL.build_rcm_timeline_trace(model_input(charge_efficiency=1.0))
    lossy = MODEL.build_rcm_timeline_trace(model_input(charge_efficiency=0.8))
    assert efficient.points[0].soc_percent > lossy.points[0].soc_percent
    low_power = MODEL.build_rcm_timeline_trace(model_input(charge_power_limit_kw=0.2))
    assert low_power.points[0].battery_delta_kwh <= 0.05 + 1e-9
    for field in (
        "battery_capacity_kwh",
        "current_soc_percent",
        "charge_power_limit_kw",
        "discharge_power_limit_kw",
        "charge_efficiency",
        "discharge_efficiency",
    ):
        partial = payload(
            MODEL.build_rcm_timeline_trace(model_input(**{field: None}))
        )
        assert partial["quality"] == "partial"
        assert all(point["soc_percent"] is None for point in partial["points"])
        assert all(point["baseline_soc_percent"] is None for point in partial["points"])
        assert all(point["battery_kw"] is None for point in partial["points"])
        assert all(point["grid_kw"] is None for point in partial["points"])
    missing_pv = payload(
        MODEL.build_rcm_timeline_trace(
            model_input(energy_points=energy_points(pv_kwh=None))
        )
    )
    assert all(point["pv_kw"] is None for point in missing_pv["points"])
    assert all(point["grid_kw"] is None for point in missing_pv["points"])


def test_freshness_horizon_dst_and_bounds() -> None:
    for fresh in (False,):
        expect_model_error(
            lambda fresh=fresh: MODEL.build_rcm_timeline_trace(
                model_input(inputs_fresh=fresh)
            ),
            "stale_or_unavailable_inputs",
        )
    expect_model_error(
        lambda: MODEL.build_rcm_timeline_trace(
            model_input(generated_at=NOW + timedelta(days=4))
        ),
        "past_only_horizon",
    )
    assert len(MODEL.build_rcm_timeline_trace(model_input()).points) == 96
    assert len(
        MODEL.build_rcm_timeline_trace(
            model_input(energy_points=energy_points(8))
        ).points
    ) == 8
    assert len(
        MODEL.build_rcm_timeline_trace(
            model_input(energy_points=energy_points(192))
        ).points
    ) == 192
    expect_model_error(
        lambda: MODEL.build_rcm_timeline_trace(
            model_input(energy_points=energy_points(193))
        ),
        "point_limit_exceeded",
    )
    # UTC stepping represents 23-hour and 25-hour local days without gaps,
    # duplicated interval identities, or local-wall interpolation.
    for count in (92, 100):
        trace = MODEL.build_rcm_timeline_trace(
            model_input(energy_points=energy_points(count))
        )
        identities = {(point.start, point.end) for point in trace.points}
        assert len(identities) == count


def test_policy_schema_limits_and_future_voltage_absence() -> None:
    current = payload(MODEL.build_rcm_timeline_trace(model_input()))
    assert set(current["points"][0]["policy"]) == TL.RCM_POLICY_FIELDS
    assert "required_headroom_kwh" not in current["points"][0]
    assert any("required_headroom_kwh" in point for point in current["points"])
    serialized = TL.compact_json_bytes(current)
    forbidden = (
        "future_voltage_",
        "predicted_voltage_",
        "future_average_voltage",
        "future_maximum_voltage",
        "voltage_confidence_interval",
    )
    text = serialized.decode("utf-8")
    assert not any(token in text for token in forbidden)
    TL.validate_serialized_byte_count(262_144)
    try:
        TL.validate_serialized_byte_count(262_145)
    except TL.TimelineValidationError:
        pass
    else:
        raise AssertionError("262145-byte payload accepted")
    sixteen = deepcopy(current)
    sixteen["sources"] = [
        {"role": f"source_{index}", "entity_id": f"sensor.source_{index}"}
        for index in range(16)
    ]
    TL.validate_payload(sixteen, expected_state="current")
    sixteen["sources"].append({"role": "source_16", "entity_id": "sensor.source_16"})
    try:
        TL.validate_payload(sixteen, expected_state="current")
    except TL.TimelineValidationError:
        pass
    else:
        raise AssertionError("17 sources accepted")
    for bad in (float("nan"), float("inf"), -float("inf")):
        malformed = deepcopy(current)
        malformed["points"][0]["pv_kw"] = bad
        try:
            TL.compact_json_bytes(malformed)
        except TL.TimelineValidationError:
            pass
        else:
            raise AssertionError("non-finite payload accepted")


def test_current_pending_unavailable_and_semantic_revision() -> None:
    current = payload(MODEL.build_rcm_timeline_trace(model_input()))
    assert current["result_current"] is True
    assert current["recalculation_pending"] is False
    pending = TL.build_pending_payload(
        current,
        policy_id="rcm",
        config_entry_id="entry-rcm",
        generated_at=NOW + timedelta(minutes=1),
        pending_input_revision=9,
        plan_entity_id="sensor.hoymiles_hit_rcm_voltage_plan",
        plan_revision=2,
    )
    assert pending["points"] == current["points"]
    assert pending["pending_input_revision"] == 9
    unavailable = TL.build_unavailable_payload(
        policy_id="rcm",
        config_entry_id="entry-rcm",
        generated_at=NOW,
        input_revision=10,
        plan_entity_id="sensor.hoymiles_hit_rcm_voltage_plan",
        blocker_code="missing_data",
        plan_revision=3,
    )
    assert unavailable["points"] == [] and unavailable["point_count"] == 0
    fingerprint = TL.semantic_fingerprint(current)
    generated = deepcopy(current)
    generated["generated_at"] = "2026-08-08T10:01:00Z"
    generated["current_actual"]["source_ages_seconds"]["pv"] = 9.0
    generated["sources"] = list(reversed(generated["sources"]))
    assert TL.semantic_fingerprint(generated) == fingerprint
    assert TL.next_plan_revision(1, fingerprint, fingerprint) == 1


def test_purity_authority_and_optimizer_boundary() -> None:
    model_source = (COMPONENT / "rcm_timeline_model.py").read_text(encoding="utf-8")
    sensor_source = (COMPONENT / "rcm_sensor.py").read_text(encoding="utf-8")
    forbidden = (
        "hass.services",
        "services.async_call",
        "modbus",
        "write_register",
        "owner_acquire",
        "grant_execution",
        "handover_execution",
        "async_track_time_interval",
        "asyncio.sleep",
        "create_task",
    )
    assert not any(token in model_source for token in forbidden)
    assert "optimize_rcm" not in model_source
    assert sum(
        isinstance(node, ast.Name) and node.id == "optimize_rcm"
        for node in ast.walk(ast.parse(sensor_source))
    ) == 1
    optimize_call = sensor_source.index("optimize_rcm,", sensor_source.index("async_add_executor_job("))
    model_call = sensor_source.index("build_rcm_timeline_trace(")
    assert optimize_call < model_call
    before = BASE_RESULT
    MODEL.build_rcm_timeline_trace(model_input())
    assert BASE_RESULT == before
    assert hash(BASE_RESULT) == hash(before)
    ast.parse(model_source)


TESTS = (
    test_profile_energy_mapping_conserves_dst_folds,
    test_action_windows_overlap_and_point_field_scope,
    test_quality_requires_requested_horizon_and_full_risk_coverage,
    test_normal_baseline_and_planned_scenario,
    test_risk_headroom_target_and_actions,
    test_no_risk_monitor_restore_and_overlap,
    test_absorb_limit_export_signs_and_balance,
    test_efficiency_limits_and_missing_facts,
    test_freshness_horizon_dst_and_bounds,
    test_policy_schema_limits_and_future_voltage_absence,
    test_current_pending_unavailable_and_semantic_revision,
    test_purity_authority_and_optimizer_boundary,
)
REQUIRED_AP2R1F_TESTS = frozenset(
    {
        "test_profile_energy_mapping_conserves_dst_folds",
        "test_action_windows_overlap_and_point_field_scope",
        "test_quality_requires_requested_horizon_and_full_risk_coverage",
    }
)


MUTATION_FILES = (
    "custom_components/hoymiles_hit_modbus/rcm_timeline_model.py",
    "custom_components/hoymiles_hit_modbus/automation_plan_timeline.py",
    "custom_components/hoymiles_hit_modbus/timeline_sensor.py",
    "custom_components/hoymiles_hit_modbus/rcm_sensor.py",
    "custom_components/hoymiles_hit_modbus/sensor.py",
    "custom_components/hoymiles_hit_modbus/__init__.py",
    "custom_components/hoymiles_hit_modbus/translations/en.json",
    "custom_components/hoymiles_hit_modbus/translations/pl.json",
    "tests/test_timeline_platform_registration.py",
    "tools/validate_release.py",
)


def _copy_fixture(destination: Path) -> None:
    for relative in MUTATION_FILES:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)


def _replace(root: Path, relative: str, old: str, new: str) -> None:
    path = root / relative
    source = path.read_text(encoding="utf-8")
    assert source.count(old) >= 1, (relative, old)
    path.write_text(source.replace(old, new, 1), encoding="utf-8", newline="\n")


def _append(root: Path, relative: str, text: str) -> None:
    path = root / relative
    path.write_text(path.read_text(encoding="utf-8") + text, encoding="utf-8", newline="\n")


def _violations(root: Path) -> set[str]:
    model = (root / MUTATION_FILES[0]).read_text(encoding="utf-8")
    common = (root / MUTATION_FILES[1]).read_text(encoding="utf-8")
    timeline = (root / MUTATION_FILES[2]).read_text(encoding="utf-8")
    rcm_sensor = (root / MUTATION_FILES[3]).read_text(encoding="utf-8")
    sensor = (root / MUTATION_FILES[4]).read_text(encoding="utf-8")
    init = (root / MUTATION_FILES[5]).read_text(encoding="utf-8")
    en = json.loads((root / MUTATION_FILES[6]).read_text(encoding="utf-8"))
    pl = json.loads((root / MUTATION_FILES[7]).read_text(encoding="utf-8"))
    registration = (root / MUTATION_FILES[8]).read_text(encoding="utf-8")
    validator = (root / MUTATION_FILES[9]).read_text(encoding="utf-8")
    found: set[str] = set()
    if "future_voltage_l1" in model:
        found.add("M01")
    if "synthetic_voltage_from_risk" in model:
        found.add("M02")
    if "grid_kwh = load_kwh + planned_battery_ac - planned_pv" not in model:
        found.add("M03")
    if "battery_ac_kwh = -battery_ac_kwh  # M04" in model:
        found.add("M04")
    if "point_pv_kwh = source.pv_kwh or 0.0" in model:
        found.add("M05")
    if "if imported > 0 and exported > 0:" not in common:
        found.add("M06")
    if "grid - (load + battery - pv)" not in common:
        found.add("M07")
    if "max(stored_kwh - discharge_output_kwh / discharge_efficiency, floor_kwh)" not in model:
        found.add("M08")
    if "min(stored_kwh + charge_input_kwh * charge_efficiency, ceiling_kwh)" not in model:
        found.add("M09")
    if "required_headroom_kwh=required_headroom" not in model:
        found.add("M10")
    if "MAX_POINTS = 192" not in common:
        found.add("M11")
    if "MAX_SERIALIZED_BYTES = 262_144" not in common:
        found.add("M12")
    if "_unrecorded_attributes = frozenset({MATCH_ALL})" not in timeline:
        found.add("M13")
    if 'candidate.pop("generated_at", None)' not in common:
        found.add("M14")
    if "generation == self._lifecycle_generation" not in timeline:
        found.add("M15")
    if sum(
        isinstance(node, ast.Name) and node.id == "optimize_rcm"
        for node in ast.walk(ast.parse(rcm_sensor))
    ) != 1:
        found.add("M16")
    if "timeline_trace_as_optimizer_input" in rcm_sensor:
        found.add("M17")
    if any(token in model for token in ("services.async_call", "write_register", "owner_acquire")):
        found.add("M18")
    if "entities.append(rcm_plan)" not in sensor:
        found.add("M19")
    if 'policy_id="rcm"' not in sensor:
        found.add("M20")
    if 'active_translation_keys.add("rcm_automation_plan_timeline")' not in init:
        found.add("M21")
    if "self.entity_id = timeline_entity_id(policy_id)" not in timeline:
        found.add("M22")
    prepare = init.split("def _async_prepare_timeline_entity_registry(", 1)[1].split("\n\nasync def ", 1)[0]
    if prepare.index("for policy_id in TIMELINE_POLICY_IDS:") > prepare.index("entity_registry.async_update_entity("):
        found.add("M23")
    translations = (
        en.get("entity", {}).get("sensor", {}),
        pl.get("entity", {}).get("sensor", {}),
    )
    if (
        any("rcm_automation_plan_timeline" not in item for item in translations)
        or "rcm_automation_plan_timeline" not in registration
        or "AP2R1_CORRECTION_PATHS" not in validator
    ):
        found.add("M24")
    return found


def run_mutation_campaign() -> None:
    model, common, timeline, rcm_sensor, sensor, init, en, _pl, _registration, _validator = MUTATION_FILES
    mutations: list[tuple[str, str, Callable[[Path], None]]] = [
        ("M01", "add future phase voltage", lambda root: _append(root, model, "\nfuture_voltage_l1 = 250.0\n")),
        ("M02", "synthesize voltage from risk", lambda root: _append(root, model, "\ndef synthetic_voltage_from_risk(risk): return risk * 253.0\n")),
        ("M03", "remove grid sign inversion", lambda root: _replace(root, model, "grid_kwh = load_kwh + planned_battery_ac - planned_pv", "grid_kwh = planned_pv - load_kwh - planned_battery_ac")),
        ("M04", "double invert battery sign", lambda root: _append(root, model, "\nbattery_ac_kwh = -battery_ac_kwh  # M04\n")),
        ("M05", "missing PV becomes zero", lambda root: _append(root, model, "\npoint_pv_kwh = source.pv_kwh or 0.0\n")),
        ("M06", "simultaneous import/export", lambda root: _replace(root, common, "if imported > 0 and exported > 0:", "if False:")),
        ("M07", "break canonical balance", lambda root: _replace(root, common, "grid - (load + battery - pv)", "grid - (load - battery - pv)")),
        ("M08", "allow SOC below floor", lambda root: _replace(root, model, "max(stored_kwh - discharge_output_kwh / discharge_efficiency, floor_kwh)", "stored_kwh - discharge_output_kwh / discharge_efficiency")),
        ("M09", "allow SOC above maximum", lambda root: _replace(root, model, "min(stored_kwh + charge_input_kwh * charge_efficiency, ceiling_kwh)", "stored_kwh + charge_input_kwh * charge_efficiency")),
        ("M10", "ignore required headroom", lambda root: _replace(root, model, "required_headroom_kwh=required_headroom", "required_headroom_kwh=None")),
        ("M11", "accept 193 points", lambda root: _replace(root, common, "MAX_POINTS = 192", "MAX_POINTS = 193")),
        ("M12", "accept 262145 bytes", lambda root: _replace(root, common, "MAX_SERIALIZED_BYTES = 262_144", "MAX_SERIALIZED_BYTES = 262_145")),
        ("M13", "remove MATCH_ALL", lambda root: _replace(root, timeline, "_unrecorded_attributes = frozenset({MATCH_ALL})", "_unrecorded_attributes = frozenset()")),
        ("M14", "generated_at revision churn", lambda root: _replace(root, common, '    candidate.pop("generated_at", None)\n', "")),
        ("M15", "accept stale reload callback", lambda root: _replace(root, timeline, "generation == self._lifecycle_generation", "True")),
        ("M16", "add second optimize_rcm", lambda root: _append(root, rcm_sensor, "\ndef m16(settings): return optimize_rcm(settings),\n")),
        ("M17", "feed timeline to optimizer", lambda root: _append(root, rcm_sensor, "\ntimeline_trace_as_optimizer_input = True\n")),
        ("M18", "add physical authority", lambda root: _append(root, model, "\nasync def m18(hass): await hass.services.async_call('modbus', 'write_register', {'owner_acquire': True})\n")),
        ("M19", "omit rcm source", lambda root: _replace(root, sensor, "    entities.append(rcm_plan)\n", "")),
        ("M20", "omit rcm timeline", lambda root: _replace(root, sensor, '            policy_id="rcm",', '            policy_id="tariff",')),
        ("M21", "omit active key", lambda root: _replace(root, init, '    active_translation_keys.add("rcm_automation_plan_timeline")\n', "")),
        ("M22", "assign identity after add", lambda root: _replace(root, timeline, "        self.entity_id = timeline_entity_id(policy_id)\n", "")),
        ("M23", "partial migration before preflight", lambda root: _replace(root, init, "    contracts = []\n", "    contracts = []\n    entity_registry.async_update_entity('partial')\n")),
        ("M24", "remove translation contract", lambda root: _replace(root, en, '      "rcm_automation_plan_timeline": {', '      "removed_rcm_timeline": {')),
    ]
    detected: list[str] = []
    survivors: list[str] = []
    for mutation_id, description, mutate in mutations:
        with tempfile.TemporaryDirectory(prefix=f"aurora_ap2r1_{mutation_id.lower()}_") as temporary:
            root = Path(temporary)
            _copy_fixture(root)
            mutate(root)
            if mutation_id in _violations(root):
                detected.append(mutation_id)
                print(f"DETECTED {mutation_id} — {description}")
            else:
                survivors.append(mutation_id)
                print(f"SURVIVED {mutation_id} — {description}")
    assert len(detected) == 24, detected
    assert not survivors, survivors
    print("RCEm AP-2R1 mutations: detected 24/24; survivors 0; NOT RUN 0")


def main() -> None:
    assert REQUIRED_AP2R1F_TESTS <= {test.__name__ for test in TESTS}
    for test in TESTS:
        test()
    run_mutation_campaign()
    print(f"RCEm timeline model: {len(TESTS)} contract groups passed")


if __name__ == "__main__":
    main()
