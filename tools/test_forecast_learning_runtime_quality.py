"""A03 regression through both production forecast-learning loaders."""

from __future__ import annotations

import ast
import asyncio
from datetime import date, datetime, time, timedelta, timezone
import logging
import math
from pathlib import Path
from statistics import median
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))
import forecast_model as forecast

WARSAW = ZoneInfo("Europe/Warsaw")
NOW = datetime(2026, 9, 20, 12, tzinfo=WARSAW)
DAY = NOW.date() - timedelta(days=1)
START = datetime.combine(DAY, time.min, tzinfo=WARSAW)
FORECAST_ENTITY = "sensor.test_forecast"
ACTUAL_ENTITY = "sensor.hoymiles_hit_pv_total_energy_today"
EXPORT_ENTITY = "binary_sensor.hoymiles_ems_export_allowed"
VERSION_ENTITY = "sensor.hoymiles_ems_package_version"


def _method(filename: str):
    source = COMPONENT / filename
    tree = ast.parse(source.read_text(encoding="utf-8"))
    method = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_async_refresh_forecast_accuracy"
    )
    method.decorator_list = []
    return source, method


async def _run(
    filename: str,
    actual_rows: list[SimpleNamespace],
    *,
    initial_factor: float = 0.8,
    initial_source: str = "retained_last_qualified_factor_no_current_history",
) -> SimpleNamespace:
    raw = {
        FORECAST_ENTITY: [
            SimpleNamespace(state="30", last_updated=START + timedelta(hours=7))
        ],
        ACTUAL_ENTITY: actual_rows,
        EXPORT_ENTITY: [
            SimpleNamespace(state="on", last_updated=START - timedelta(minutes=1))
        ],
        VERSION_ENTITY: [
            SimpleNamespace(state="1.5.8rc2", last_updated=START - timedelta(minutes=1))
        ],
    }

    async def query(_hass, _start, _end, entities, **_kwargs):
        return {key: raw.get(key, []) for key in entities}

    source, method = _method(filename)
    namespace = {
        "datetime": datetime,
        "ZoneInfo": ZoneInfo,
        "timedelta": timedelta,
        "time": time,
        "date": date,
        "dt_util": SimpleNamespace(
            now=lambda: NOW,
            as_utc=lambda value: value.astimezone(timezone.utc),
        ),
        "async_get_bounded_state_reports": query,
        "_forecast_learning_policy_snapshot": lambda *_args: (
            SimpleNamespace(enabled=True, mode="adaptive"),
            {},
        ),
        "_forecast_learning_policy_signature": lambda _policy: "adaptive",
        "_resolved_forecast_entity_id": lambda *_args, **_kwargs: FORECAST_ENTITY,
        "_first_numeric_state": lambda *_args: (FORECAST_ENTITY, object()),
        "EMS_TODAY_FORECAST_ENTITY_HELPER": "today",
        "TODAY_FORECAST_ENTITY_HELPER": "today",
        "TODAY_FORECAST_CANDIDATES": (),
        "FORECAST_EXPORT_ALLOWED_ENTITY": EXPORT_ENTITY,
        "FORECAST_EMS_PACKAGE_VERSION_ENTITY": VERSION_ENTITY,
        "forecast_learning_history_day_eligible": (
            forecast.forecast_learning_history_day_eligible
        ),
        "forecast_policy_for_source": forecast.forecast_policy_for_source,
        "qualified_cumulative_energy_day": forecast.qualified_cumulative_energy_day,
        "get_astral_event_date": lambda _hass, event, day: datetime.combine(
            day,
            time(6 if event == "sunrise" else 18),
            tzinfo=WARSAW,
        ),
        "robust_weighted_factor": forecast.robust_weighted_factor,
        "median": median,
        "isfinite": math.isfinite,
        "math": math,
        "_LOGGER": logging.getLogger("test_forecast_learning_runtime_quality"),
    }
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
    refresh = namespace["_async_refresh_forecast_accuracy"]
    probe = SimpleNamespace(
        _forecast_refresh_running=False,
        _forecast_refresh_date=None,
        _forecast_accuracy_factor=initial_factor,
        _forecast_accuracy_uncertainty=0.15,
        _forecast_accuracy_days=4,
        _forecast_accuracy_source=initial_source,
        _forecast_accuracy_refreshed_at=None,
        hass=SimpleNamespace(config=SimpleNamespace(time_zone="Europe/Warsaw")),
        _runtime=SimpleNamespace(),
    )
    if filename == "rce_sensor.py":
        await refresh(probe, force=True)
    else:
        await refresh(probe)
    return probe


async def main() -> None:
    truncated = [
        SimpleNamespace(state="0", last_updated=START),
        SimpleNamespace(state="6", last_updated=START + timedelta(hours=12)),
    ]
    complete = [
        *truncated,
        SimpleNamespace(state="6", last_updated=START + timedelta(hours=18, minutes=5)),
    ]
    reset = [
        SimpleNamespace(state="0", last_updated=START),
        SimpleNamespace(state="6", last_updated=START + timedelta(hours=12)),
        SimpleNamespace(state="2", last_updated=START + timedelta(hours=18, minutes=5)),
    ]
    zero = [
        SimpleNamespace(state="0", last_updated=START),
        SimpleNamespace(state="0", last_updated=START + timedelta(hours=18, minutes=5)),
    ]
    for filename in ("rce_sensor.py", "tariff_sensor.py"):
        for rejected in (truncated, reset):
            probe = await _run(filename, rejected)
            assert probe._forecast_accuracy_factor == 0.8
            assert probe._forecast_accuracy_days == 0
            assert probe._forecast_accuracy_source == (
                "retained_last_qualified_factor_no_current_history"
            )
        learned = await _run(filename, complete)
        assert learned._forecast_accuracy_days == 1
        assert learned._forecast_accuracy_factor < 0.8
        real_zero = await _run(filename, zero)
        assert real_zero._forecast_accuracy_days == 1
        assert real_zero._forecast_accuracy_factor in {0.15, 0.5}
        cold = await _run(
            filename,
            truncated,
            initial_factor=0.9,
            initial_source="automatic_conservative_fallback",
        )
        assert cold._forecast_accuracy_factor == 0.9
        assert cold._forecast_accuracy_days == 0
        assert cold._forecast_accuracy_source == "automatic_conservative_fallback"
    print("Forecast learning runtime quality: 10 loader scenarios passed")


if __name__ == "__main__":
    asyncio.run(main())
