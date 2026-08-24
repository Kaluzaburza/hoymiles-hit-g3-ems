"""Permanent AP-1 schema, trace, lifecycle and authority contracts."""

from __future__ import annotations

import asyncio
import ast
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
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
        start = NOW + timedelta(minutes=30 * index)
        selected = index == 0
        if policy_id == "rce":
            policy: TL.RCEPolicyPoint | TL.TariffPolicyPoint = TL.RCEPolicyPoint(
                sell_price_pln_kwh=0.8,
                planned_export_kwh=0.5 if selected else 0.0,
                target_discharge_kw=1.0 if selected else 0.0,
                target_tolerance_kw=0.05,
                expected_revenue_pln=0.4 if selected else 0.0,
            )
            battery_delta = -0.5 if selected else 0.0
            grid_import = 0.0
            grid_export = 0.5 if selected else 0.0
            action = "export" if selected else "idle"
            target = None
        else:
            policy = TL.TariffPolicyPoint(
                buy_price_pln_kwh=0.6,
                tariff_zone="low",
                planned_import_kwh=0.5 if selected else 0.0,
                planned_charge_kw=0.95 if selected else 0.0,
                expected_cost_pln=0.3 if selected else 0.0,
                expected_saving_pln=None,
            )
            battery_delta = 0.475 if selected else 0.0
            grid_import = 0.5 if selected else 0.0
            grid_export = 0.0
            action = "battery_charge" if selected else "idle"
            target = 62.0 if selected else None
        points.append(
            TL.TimelineTracePoint(
                start=start,
                end=start + timedelta(minutes=30),
                pv_kwh=0.25,
                load_kwh=0.5,
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
            )
        )
    return TL.OptimizerTimelineTrace(policy_id=policy_id, points=tuple(points))


def payload(policy_id: str = "rce", count: int = 2) -> dict[str, Any]:
    return TL.build_current_payload(
        trace(policy_id, count),
        config_entry_id="entry-a",
        generated_at=NOW + timedelta(minutes=5),
        input_revision=7,
        plan_revision=1,
        plan_entity_id=(
            "sensor.hoymiles_hit_rce_optimized_plan"
            if policy_id == "rce"
            else "sensor.hoymiles_hit_tariff_charge_plan"
        ),
        current_actual=snapshot(),
        sources=[{"role": "plan", "entity_id": "sensor.plan_a"}],
        physical_active=True,
    )


def test_schema_and_policy_contract() -> None:
    rce = payload("rce")
    tariff = payload("tariff")
    assert rce["schema_version"] == 1
    assert set(rce) == TL.TOP_LEVEL_FIELDS
    assert set(rce["points"][0]) == TL.COMMON_POINT_FIELDS
    assert set(rce["points"][0]["policy"]) == TL.RCE_POLICY_FIELDS
    assert "target_soc_percent" not in rce["points"][0]
    assert "required_headroom_kwh" not in rce["points"][0]
    assert set(tariff["points"][0]) == TL.COMMON_POINT_FIELDS | {
        "target_soc_percent"
    }
    assert set(tariff["points"][0]["policy"]) == TL.TARIFF_POLICY_FIELDS
    assert "required_headroom_kwh" not in tariff["points"][0]
    malformed = deepcopy(rce)
    malformed["unexpected"] = True
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    malformed = deepcopy(rce)
    malformed["points"][0]["policy"]["arbitrary"] = 1
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))
    malformed = deepcopy(rce)
    malformed["blocker_code"] = "unbounded code " * 20
    expect_invalid(lambda: TL.validate_payload(malformed, expected_state="current"))


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
    assert first["grid_kw"] == -1.0
    assert first["grid_import_kw"] == 0.0
    assert first["grid_export_kw"] == 1.0
    assert first["battery_kw"] == -1.0
    assert payload("tariff")["points"][0]["battery_kw"] == 0.95
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
        charge_efficiency_percent=95.0,
        discharge_efficiency_percent=95.0,
        minimum_saving_pln_kwh=0.05,
        schedule=TARIFF.TariffSchedule(
            tariff_type="G12",
            g11_price_pln_kwh=0.85,
            low_price_pln_kwh=0.62,
            medium_price_pln_kwh=0.82,
            peak_price_pln_kwh=1.03,
            cheap_windows=((22 * 60, 6 * 60), (13 * 60, 15 * 60)),
        ),
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
        assert point.target_soc_percent is not None
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
        "async_call_later",
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
    helpers = types.ModuleType("homeassistant.helpers")
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.EntityRegistry = object
    entity_registry.async_get = lambda hass: hass.registry
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    device_registry.DeviceInfo = dict
    util = types.ModuleType("homeassistant.util")
    dt_module = types.ModuleType("homeassistant.util.dt")
    dt_module.utcnow = lambda: NOW + timedelta(minutes=5)
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
        "homeassistant.util": util,
        "homeassistant.util.dt": dt_module,
    }
    sys.modules.update(modules)
    components.sensor = sensor_module
    helpers.entity_registry = entity_registry
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
        entity_id=f"sensor.{entry_id}_rce_plan",
        platform="hoymiles_hit_modbus",
        unique_id=f"{entry_id}_rce_optimized_plan",
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
        policy_id="rce",
        source_sensor=source,
    )
    sensor._fixture_writes = 0
    return sensor, source, states


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

    # Identical, generated-at-only and age-only refreshes are semantic no-ops.
    sensor.publish_current(trace("rce"), input_revision=3)
    assert sensor.extra_state_attributes == first
    assert sensor._fixture_writes == first_writes

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

    # Unavailable is another semantic transition and can never reset or reuse
    # the runtime-local counter. A repeated identical unavailable is a no-op.
    sensor.publish_current(trace("rce"), input_revision=5)
    current_revision = sensor.extra_state_attributes["plan_revision"]
    sensor.publish_unavailable(input_revision=5, blocker_code="missing_data")
    unavailable = deepcopy(sensor.extra_state_attributes)
    assert unavailable["plan_revision"] > current_revision
    unavailable_writes = sensor._fixture_writes
    sensor.publish_unavailable(input_revision=5, blocker_code="missing_data")
    assert sensor.extra_state_attributes == unavailable
    assert sensor._fixture_writes == unavailable_writes
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


TESTS = (
    test_schema_and_policy_contract,
    test_intervals_dst_and_native_resolution,
    test_numbers_nulls_and_signs,
    test_hard_limits,
    test_exact_rce_trace,
    test_exact_tariff_trace,
    test_revision_atomicity_and_states,
    test_active_snapshot_contract,
    test_malformed_data_and_partial_horizon,
    test_entity_lifecycle_recorder_multi_entry_and_authority,
    test_runtime_revision_and_stale_callback_contract,
    test_pre_add_detach_reload_and_active_runtime_contract,
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
    for test in TESTS:
        test()
    print(f"Automation plan timeline: {len(TESTS)} contract groups passed")


if __name__ == "__main__":
    main()
