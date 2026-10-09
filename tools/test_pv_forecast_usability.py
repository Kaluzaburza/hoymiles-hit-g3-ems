#!/usr/bin/env python3
"""Offline PV-NOC-01 time, horizon and fail-closed regression tests."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


custom_components = ModuleType("custom_components")
custom_components.__path__ = [str(ROOT / "custom_components")]
package = ModuleType("custom_components.hoymiles_hit_modbus")
package.__path__ = [str(COMPONENT)]
sys.modules[custom_components.__name__] = custom_components
sys.modules[package.__name__] = package
_load_module(
    "custom_components.hoymiles_hit_modbus.energy_data",
    COMPONENT / "energy_data.py",
)
PV = _load_module(
    "custom_components.hoymiles_hit_modbus.pv_forecast_usability",
    COMPONENT / "pv_forecast_usability.py",
)
SCHEDULED_PAUSE_GRACE_SECONDS = PV.SCHEDULED_PAUSE_GRACE_SECONDS
evaluate_pv_forecast_usefulness = PV.evaluate_pv_forecast_usefulness


WARSAW = ZoneInfo("Europe/Warsaw")


def forecast_rows(target: date) -> list[dict[str, object]]:
    start = datetime(
        target.year,
        target.month,
        target.day,
        tzinfo=WARSAW,
    ).astimezone(timezone.utc)
    end = datetime(
        *(target + timedelta(days=1)).timetuple()[:3],
        tzinfo=WARSAW,
    ).astimezone(timezone.utc)
    rows: list[dict[str, object]] = []
    cursor = start
    while cursor < end:
        local = cursor.astimezone(WARSAW)
        rows.append(
            {
                "period_start": local,
                "pv_estimate": 2.0 if 10 <= local.hour <= 16 else 0.0,
            }
        )
        cursor += timedelta(minutes=30)
    return rows


def state(
    value: object,
    reported: datetime | None,
    target: date,
    *,
    data_correct: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        state=str(value),
        last_reported=reported,
        last_updated=reported,
        attributes={
            "dataCorrect": data_correct,
            "detailedForecast": forecast_rows(target),
        },
    )


def update_state(last_success: datetime, next_update: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        state=last_success.isoformat(),
        attributes={
            "next_auto_update": next_update.isoformat(),
            "auto_update_queue": next_update.isoformat(),
        },
    )


def check_live_reproduction() -> None:
    now = datetime(2026, 9, 13, 23, 47, tzinfo=WARSAW)
    last = datetime(2026, 9, 13, 16, 38, 18, tzinfo=WARSAW)
    next_update = datetime(2026, 9, 14, 6, 26, 29, tzinfo=WARSAW)
    result = evaluate_pv_forecast_usefulness(
        state(14.5, last, now.date()),
        now,
        target_date=now.date(),
        max_age_seconds=6 * 60 * 60,
        update_state=update_state(last, next_update),
    )
    assert not result.source_fresh
    assert 24_000 < (result.age_seconds or 0) < 26_000
    assert result.usable and result.mode == "scheduled_pause"
    assert result.value == 14.5
    assert result.next_update_at == next_update


def check_boundary_and_first_update() -> None:
    last = datetime(2026, 9, 13, 16, 38, 18, tzinfo=WARSAW)
    next_update = datetime(2026, 9, 14, 6, 26, 29, tzinfo=WARSAW)
    deadline = next_update + timedelta(seconds=SCHEDULED_PAUSE_GRACE_SECONDS)
    before = evaluate_pv_forecast_usefulness(
        state(40.0, last, date(2026, 9, 14)),
        deadline,
        target_date=date(2026, 9, 14),
        max_age_seconds=6 * 60 * 60,
        update_state=update_state(last, next_update),
    )
    assert before.usable and before.valid_until == deadline
    after = evaluate_pv_forecast_usefulness(
        state(40.0, last, date(2026, 9, 14)),
        deadline + timedelta(seconds=1),
        target_date=date(2026, 9, 14),
        max_age_seconds=6 * 60 * 60,
        update_state=update_state(last, next_update),
    )
    assert not after.usable and after.mode == "expired"
    refreshed = deadline + timedelta(seconds=2)
    fresh = evaluate_pv_forecast_usefulness(
        state(41.0, refreshed, date(2026, 9, 14)),
        refreshed,
        target_date=date(2026, 9, 14),
        max_age_seconds=6 * 60 * 60,
        update_state=update_state(refreshed, refreshed + timedelta(hours=2)),
    )
    assert fresh.usable and fresh.source_fresh and fresh.mode == "fresh"


def check_midnight_rollover() -> None:
    now = datetime(2026, 9, 14, 0, 1, tzinfo=WARSAW)
    last = now - timedelta(hours=7)
    next_update = now + timedelta(hours=6)
    old_today = evaluate_pv_forecast_usefulness(
        state(12.0, last, date(2026, 9, 13)),
        now,
        target_date=now.date(),
        max_age_seconds=6 * 60 * 60,
        update_state=update_state(last, next_update),
    )
    assert not old_today.usable
    assert old_today.reason == "forecast_horizon_mismatch"
    rolled = evaluate_pv_forecast_usefulness(
        state(15.0, last, now.date()),
        now,
        target_date=now.date(),
        max_age_seconds=6 * 60 * 60,
        update_state=update_state(last, next_update),
    )
    assert rolled.usable and rolled.mode == "scheduled_pause"


def check_dst_windows() -> None:
    for now, next_update in (
        (
            datetime(2026, 3, 29, 1, 30, tzinfo=WARSAW),
            datetime(2026, 3, 29, 7, 0, tzinfo=WARSAW),
        ),
        (
            datetime(2026, 10, 25, 2, 30, tzinfo=WARSAW, fold=1),
            datetime(2026, 10, 25, 7, 0, tzinfo=WARSAW),
        ),
    ):
        last = now.astimezone(timezone.utc) - timedelta(hours=7)
        result = evaluate_pv_forecast_usefulness(
            state(5.0, last, now.date()),
            now,
            target_date=now.date(),
            max_age_seconds=6 * 60 * 60,
            update_state=update_state(last, next_update),
        )
        assert result.usable and result.mode == "scheduled_pause"


def check_fail_closed_inputs() -> None:
    now = datetime(2026, 12, 21, 2, 0, tzinfo=WARSAW)
    last = now - timedelta(hours=7)
    next_update = now + timedelta(hours=6)
    cases = (
        (state("unknown", last, now.date()), "unavailable"),
        (state("nan", last, now.date()), "not_finite"),
        (state(-1, last, now.date()), "below_minimum"),
        (state(5, last, now.date(), data_correct=False), "source_data_incorrect"),
        (state(5, now + timedelta(minutes=1), now.date()), "future_timestamp"),
    )
    for candidate, expected in cases:
        result = evaluate_pv_forecast_usefulness(
            candidate,
            now,
            target_date=now.date(),
            max_age_seconds=6 * 60 * 60,
            update_state=update_state(last, next_update),
        )
        assert not result.usable and result.reason == expected

    no_schedule = evaluate_pv_forecast_usefulness(
        state(5, last, now.date()),
        now,
        target_date=now.date(),
        max_age_seconds=6 * 60 * 60,
    )
    assert not no_schedule.usable
    assert no_schedule.reason == "schedule_unavailable"

    missing_coverage = state(5, last, now.date())
    missing_coverage.attributes = {}
    result = evaluate_pv_forecast_usefulness(
        missing_coverage,
        now,
        target_date=now.date(),
        max_age_seconds=6 * 60 * 60,
        update_state=update_state(last, next_update),
    )
    assert not result.usable
    assert result.reason == "forecast_coverage_unavailable"

    gap = state(5, last, now.date())
    gap.attributes["detailedForecast"] = gap.attributes["detailedForecast"][::4]
    result = evaluate_pv_forecast_usefulness(
        gap,
        now,
        target_date=now.date(),
        max_age_seconds=6 * 60 * 60,
        update_state=update_state(last, next_update),
    )
    assert not result.usable
    assert result.reason == "forecast_coverage_incomplete"

    too_old = evaluate_pv_forecast_usefulness(
        state(5, now - timedelta(hours=31), now.date()),
        now,
        target_date=now.date(),
        max_age_seconds=6 * 60 * 60,
        update_state=update_state(now - timedelta(hours=31), next_update),
    )
    assert not too_old.usable
    assert too_old.reason == "stale"


def check_sunrise_fallback() -> None:
    now = datetime(2026, 1, 15, 2, 0, tzinfo=WARSAW)
    last = now - timedelta(hours=7)
    rising = datetime(2026, 1, 15, 7, 35, tzinfo=WARSAW)
    sun = SimpleNamespace(
        state="below_horizon",
        attributes={"next_rising": rising.isoformat()},
    )
    result = evaluate_pv_forecast_usefulness(
        state(4, last, now.date()),
        now,
        target_date=now.date(),
        max_age_seconds=6 * 60 * 60,
        sun_state=sun,
    )
    assert result.usable
    assert result.reason == "scheduled_pause_sunrise_fallback"
    above = SimpleNamespace(
        state="above_horizon",
        attributes={"next_rising": rising.isoformat()},
    )
    result = evaluate_pv_forecast_usefulness(
        state(4, last, now.date()),
        now,
        target_date=now.date(),
        max_age_seconds=6 * 60 * 60,
        sun_state=above,
    )
    assert not result.usable and result.reason == "schedule_unavailable"


def check_three_policy_parity() -> None:
    now = datetime(2026, 9, 14, 5, 0, tzinfo=WARSAW)
    last = now - timedelta(hours=19)
    next_update = now + timedelta(hours=1, minutes=30)
    results = {
        policy: evaluate_pv_forecast_usefulness(
            state(8, last, now.date()),
            now,
            target_date=now.date(),
            max_age_seconds=max_age,
            update_state=update_state(last, next_update),
        )
        for policy, max_age in {
            "rce": 6 * 60 * 60,
            "tariff": 18 * 60 * 60,
            "rcem": 12 * 60 * 60,
        }.items()
    }
    assert all(result.usable for result in results.values())
    assert {result.mode for result in results.values()} == {"scheduled_pause"}
    expired_at = next_update + timedelta(
        seconds=SCHEDULED_PAUSE_GRACE_SECONDS + 1
    )
    assert all(
        not evaluate_pv_forecast_usefulness(
            state(8, last, expired_at.date()),
            expired_at,
            target_date=expired_at.date(),
            max_age_seconds=max_age,
            update_state=update_state(last, next_update),
        ).usable
        for max_age in (6 * 60 * 60, 18 * 60 * 60, 12 * 60 * 60)
    )


def check_engine_integration_contract() -> None:
    component = ROOT / "custom_components" / "hoymiles_hit_modbus"
    sources = {
        name: (component / name).read_text(encoding="utf-8")
        for name in ("rce_sensor.py", "tariff_sensor.py", "rcm_sensor.py")
    }
    for source in sources.values():
        assert "evaluate_pv_forecast_usefulness(" in source
        assert "resolve_solcast_update_state(self.hass.states)" in source
        assert "SOLCAST_UPDATE_ENTITY_CANDIDATES" in source
        assert (
            "forecast_usability_mode" in source
            or "forecast_today_usability_mode" in source
        )
        assert "forecast_valid_until" in source
    assert "today_forecast_sample.usable" in sources["rce_sensor.py"]
    assert "tomorrow_forecast_sample.usable" in sources["rce_sensor.py"]
    assert "remaining_sample = evaluate_pv_forecast_usefulness(" in sources[
        "rce_sensor.py"
    ]
    energy_source = (component / "energy_data.py").read_text(encoding="utf-8")
    assert "if age > max(float(max_age_seconds), 0.0):" in energy_source
    assert "return NumericStateSample(None, age, False, \"stale\", reported)" in energy_source


def main() -> None:
    checks = (
        check_live_reproduction,
        check_boundary_and_first_update,
        check_midnight_rollover,
        check_dst_windows,
        check_fail_closed_inputs,
        check_sunrise_fallback,
        check_three_policy_parity,
        check_engine_integration_contract,
    )
    for check in checks:
        check()
    print(f"PV forecast usability: PASS ({len(checks)} groups)")


if __name__ == "__main__":
    main()
