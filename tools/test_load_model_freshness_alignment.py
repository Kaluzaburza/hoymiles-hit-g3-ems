"""The RCE consumer and shared broker use one LOAD-model age contract."""

from __future__ import annotations

import ast
from datetime import datetime, timedelta
import math
from pathlib import Path
import select as _stdlib_select  # noqa: F401 - load before local select.py is visible
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path[:0] = [str(ROOT / "tools"), str(COMPONENT)]

import load_model  # noqa: E402
import load_history_store  # noqa: E402
import rce_history  # noqa: E402
import test_ems_shared_inputs as shared  # noqa: E402


WARSAW = ZoneInfo("Europe/Warsaw")


def _load_values_method(
    *,
    state_numbers: dict[str, float] | None = None,
    state_attributes: dict[tuple[str, str], float] | None = None,
    configured_fallback: float = 40.0,
):
    source = COMPONENT / "rce_sensor.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_load_model_values"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            method,
        ],
        type_ignores=[],
    )
    missing = object()
    numbers = state_numbers or {}
    attributes = state_attributes or {}
    namespace = {
        "daily_ages_days": load_model.daily_ages_days,
        "robust_weighted_estimate": load_model.robust_weighted_estimate,
        "merge_history": load_history_store.merge_history,
        "history_in_window": load_history_store.history_in_window,
        "qualified_load_history_is_usable": shared.M.qualified_load_history_is_usable,
        "LOAD_EXTENDED_LOOKBACK_DAYS": 31,
        "load_model_generated_at_is_fresh": (
            shared.M.load_model_generated_at_is_fresh
        ),
        "_empty_load_summary": lambda: rce_history.LoadHistorySummary(
            None, 0, {}, None, 0, {}
        ),
        "_state_number": lambda _hass, entity_id: numbers.get(entity_id),
        "_state_attribute_number": (
            lambda _hass, entity_id, attribute: attributes.get(
                (entity_id, attribute)
            )
        ),
        "_shared_sample_value": lambda *_args: missing,
        "_SHARED_INPUT_MISSING": missing,
        "_preferred_number_helper": lambda *_args: configured_fallback,
        "EMS_FALLBACK_DAILY_LOAD_HELPER": "fallback",
        "LEGACY_FALLBACK_DAILY_LOAD_HELPER": "legacy",
        "isfinite": math.isfinite,
        "state_reported_at": lambda _state: None,
    }
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["_load_model_values"]


def _probe(generated_at: datetime | None, now: datetime) -> SimpleNamespace:
    daily = {
        (now.date() - timedelta(days=offset)).isoformat(): 10.0
        for offset in (5, 4, 3, 2)
    }
    profile = rce_history.LoadHistorySummary(10.0, 4, daily, None, 0, {})
    return SimpleNamespace(
        # Legacy raw-LOAD fixture: the new optional EV filter is off.
        _ev_filter=lambda: SimpleNamespace(project=lambda raw: raw, configure=lambda: (SimpleNamespace(enabled=False),)),
        _extended_load_history=profile,
        _load_history=profile,
        _load_profile_generated_at=generated_at,
        _runtime=SimpleNamespace(),
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: None)),
    )


def _empty_probe(generated_at: datetime | None) -> SimpleNamespace:
    empty = rce_history.LoadHistorySummary(None, 0, {}, None, 0, {})
    return SimpleNamespace(
        # Legacy raw-LOAD fixture: the new optional EV filter is off.
        _ev_filter=lambda: SimpleNamespace(project=lambda raw: raw, configure=lambda: (SimpleNamespace(enabled=False),)),
        _extended_load_history=empty,
        _load_history=empty,
        _load_profile_generated_at=generated_at,
        _runtime=SimpleNamespace(),
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _entity_id: None)),
    )


def _shared_ready(generated_at: datetime | None, now: datetime) -> bool:
    daily = tuple(10.0 for _ in range(4))
    dates = tuple(
        (now.date() - timedelta(days=offset)).isoformat()
        for offset in (5, 4, 3, 2)
    )
    bounded = shared.M._bounded_load_model_snapshot(
        {
            "average_daily_home_load_kwh": 10.0,
            "daily_history_days": 4,
            "daily_totals_kwh": daily,
            "daily_total_dates": dates,
            "generated_at": generated_at,
            "ready": True,
            "source": "recorder_phase_counters_and_actual_load",
        },
        now=now,
    )
    return bounded.ready


def _shared_fallback_profile_ready():
    source = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_shared_fallback_profile_ready"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            function,
        ],
        type_ignores=[],
    )
    namespace = {"math": math}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["_shared_fallback_profile_ready"]


def _rcm_shared_fallback_profile_ready():
    source = COMPONENT / "rcm_sensor.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_shared_fallback_profile_ready"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            function,
        ],
        type_ignores=[],
    )
    namespace = {"Any": object, "isfinite": math.isfinite}
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["_shared_fallback_profile_ready"]


def main() -> None:
    method = _load_values_method()
    now = datetime(2026, 9, 23, 12, tzinfo=WARSAW)
    for age, expected_ready in (
        (timedelta(hours=29, minutes=59), True),
        (timedelta(hours=30), True),
        (timedelta(hours=30, seconds=1), True),
        (timedelta(hours=42), True),
    ):
        generated_at = now - age
        values = method(_probe(generated_at, now), now)
        assert values["history_data_fresh"] is (age <= timedelta(hours=30))
        assert values["history_complete"] is expected_ready
        assert _shared_ready(generated_at, now) is expected_ready
        if expected_ready:
            assert abs(values["average_load"] - 10.0) <= 1e-9
            assert values["load_model_source"] == "history_28d_gap_weighted"
            assert values["profile_history"].daily_history_days == 4
        else:
            assert values["average_load"] == 40.0
            assert values["load_model_source"] == "configured_daily_fallback"
            assert values["profile_history"].daily_history_days == 0

    missing = method(_probe(None, now), now)
    assert missing["history_complete"] is False
    assert missing["average_load"] == 40.0
    assert _shared_ready(None, now) is False

    fallback_ready = _shared_fallback_profile_ready()
    fallback_sample = SimpleNamespace(value=17.0, fresh=True)
    fallback_load = SimpleNamespace(
        fallback_currently_used=True,
        ready=True,
        fallback_daily_home_load_kwh=fallback_sample,
    )
    assert fallback_ready(fallback_load, 17.0)
    assert not fallback_ready(
        SimpleNamespace(
            fallback_currently_used=True,
            ready=False,
            fallback_daily_home_load_kwh=fallback_sample,
        ),
        17.0,
    )
    assert not fallback_ready(fallback_load, 18.0)

    rcm_fallback_ready = _rcm_shared_fallback_profile_ready()
    assert rcm_fallback_ready(fallback_load, 17.0)
    assert not rcm_fallback_ready(
        SimpleNamespace(
            fallback_currently_used=True,
            ready=False,
            fallback_daily_home_load_kwh=fallback_sample,
        ),
        17.0,
    )
    assert not rcm_fallback_ready(fallback_load, 18.0)

    unsafe_statistics = _load_values_method(
        state_numbers={
            "sensor.hoymiles_load_average_4_days": 67.48,
            "sensor.hoymiles_night_load_average_4_days": 1199.55,
        },
        state_attributes={
            ("sensor.hoymiles_load_average_4_days", "history_days"): 1.04,
            ("sensor.hoymiles_night_load_average_4_days", "history_days"): 1.04,
        },
        configured_fallback=17.0,
    )(_empty_probe(now), now)
    assert unsafe_statistics["history_load"] is None
    assert unsafe_statistics["load_history_days"] == 0.0
    assert unsafe_statistics["load_history_source"] == "no_valid_recorder_history"
    assert unsafe_statistics["average_load"] == 17.0
    assert unsafe_statistics["load_model_source"] == "configured_daily_fallback"
    assert unsafe_statistics["average_night_load"] is None
    assert unsafe_statistics["night_history_days"] == 0.0

    scheduler = (ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml").read_text(
        encoding="utf-8"
    )
    load_block = scheduler.split(
        '- name: "Hoymiles Load Average 4 Days"', 1
    )[1].split('- name: "Hoymiles Night Load Average 4 Days"', 1)[0]
    night_block = scheduler.split(
        '- name: "Hoymiles Night Load Average 4 Days"', 1
    )[1].split('- name: "Hoymiles RCE PV Self Consumption Today"', 1)[0]
    assert "statistics_fallback" not in load_block
    assert "hoymiles_ems_fallback_daily_home_load" in load_block
    assert "configured_daily_fallback" in load_block
    assert "statistics_fallback" not in night_block
    assert "is_number(recorder_average) and recorder_days >= 1" in night_block
    print("LOAD model freshness alignment: PASS")


if __name__ == "__main__":
    main()
