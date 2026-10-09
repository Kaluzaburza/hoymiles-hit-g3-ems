"""Production LOAD-model diagnostic separation regression."""

from __future__ import annotations

import ast
from datetime import datetime
from math import isfinite
from pathlib import Path
from types import SimpleNamespace
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))

import load_history_store as store  # noqa: E402
import load_model  # noqa: E402
import rce_history as history  # noqa: E402


def _method(namespace: dict[str, object]):
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
    exec(compile(ast.fix_missing_locations(module), str(source), "exec"), namespace)
    return namespace["_load_model_values"]


def main() -> None:
    empty = history.LoadHistorySummary(None, 0, {}, None, 0, {})
    rejected = history.LoadHistorySummary(
        None,
        0,
        {},
        None,
        0,
        {},
        daily_quality_by_date={"2026-09-22": "partial_phase_counter"},
        phase_quality_by_date={
            "2026-09-22": {
                "l1": "missing_start_edge",
                "l2": "missing_start_edge",
                "l3": "missing_start_edge",
            }
        },
        night_quality_by_date={
            "2026-09-22": (
                "missing_or_reset:"
                "sensor.hoymiles_hit_load_energy_use_l1n_today"
            )
        },
    )
    missing = object()
    namespace = {
        "datetime": datetime,
        "Any": object,
        "daily_ages_days": load_model.daily_ages_days,
        "robust_weighted_estimate": load_model.robust_weighted_estimate,
        "_shared_sample_value": lambda *_args: missing,
        "_SHARED_INPUT_MISSING": missing,
        "_preferred_number_helper": lambda *_args: 17.0,
        "EMS_FALLBACK_DAILY_LOAD_HELPER": "input_number.fallback",
        "LEGACY_FALLBACK_DAILY_LOAD_HELPER": "input_number.legacy",
        "load_model_generated_at_is_fresh": lambda value, *, now: value is not None,
        "isfinite": isfinite,
        "state_reported_at": lambda _state: None,
        "_empty_load_summary": lambda: empty,
        "merge_history": store.merge_history,
        "history_in_window": store.history_in_window,
        "qualified_load_history_is_usable": lambda *_args, **_kwargs: False,
        "LOAD_EXTENDED_LOOKBACK_DAYS": 31,
    }
    load_values = _method(namespace)
    probe = SimpleNamespace(
        # Legacy raw-LOAD fixture: the new optional EV filter is off.
        _ev_filter=lambda: SimpleNamespace(project=lambda raw: raw, configure=lambda: (SimpleNamespace(enabled=False),)),
        _extended_load_history=rejected,
        _load_history=rejected,
        _load_profile_generated_at=None,
        _runtime=SimpleNamespace(),
        hass=SimpleNamespace(states=SimpleNamespace(get=lambda _entity: None)),
    )
    now = datetime(2026, 9, 23, 8, tzinfo=ZoneInfo("Europe/Warsaw"))
    values = load_values(probe, now)
    assert values["profile_history"].daily_quality_by_date in (None, {})
    assert values["load_history_days"] == 0.0
    assert values["average_load"] == 17.0
    assert values["load_model_source"] == "configured_daily_fallback"
    assert values["history_data_fresh"] is False
    assert values["diagnostic_history"].daily_quality_by_date == (
        rejected.daily_quality_by_date
    )
    assert values["diagnostic_history"].phase_quality_by_date == (
        rejected.phase_quality_by_date
    )
    assert store.qualification_diagnostics(values["diagnostic_history"]) == {
        "recorder_load_qualification_date": "2026-09-22",
        "recorder_load_qualification_reason": "partial_phase_counter",
        "recorder_night_qualification_date": "2026-09-22",
        "recorder_night_qualification_reason": (
            "missing_or_reset:"
            "sensor.hoymiles_hit_load_energy_use_l1n_today"
        ),
        "recorder_load_qualifier_version": store.QUALIFIER_VERSION,
        "recorder_load_cache_migrated_from": None,
        "recorder_load_availability_date": None,
        "recorder_load_availability_decision": None,
    }
    print("LOAD diagnostics: PASS (generated_at=null, fallback unchanged, reasons retained)")


if __name__ == "__main__":
    main()
