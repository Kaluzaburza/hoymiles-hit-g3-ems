"""A fresh home-energy shortage is a current nonexecuting diagnostic plan."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import test_rce_lease_real_cadence as fixture
from test_rce_optimizer import RCE, base_input
from test_tariff_price_schedule import official, span


NOW = datetime(2026, 9, 26, 23, 59, tzinfo=ZoneInfo("Europe/Warsaw"))


def night_settings(now=NOW):
    start = RCE.floor_half_hour(now)
    prices = [RCE.PriceSlot(start + RCE.SLOT * i, 0.8 if 17 <= (start + RCE.SLOT * i).hour < 21 else 0.3)
              for i in range(49)]
    pv = {row.start: 50.83 / 24 for row in prices
          if row.start.date() > now.date() and 6 <= row.start.hour < 18}
    return base_input(
        now=now, price_slots=prices, pv_by_slot_kwh=pv, conservative_pv_by_slot_kwh=pv,
        battery_capacity_kwh=15.0, battery_soc_percent=27.0,
        outage_reserve_soc_percent=10.0, manual_minimum_soc_percent=60.0,
        average_daily_load_kwh=15.09, average_night_load_kwh=7.8,
        current_load_power_kw=0.641, current_pv_power_kw=0.0,
        current_battery_soc_fresh=True, export_power_cap_kw=10.0,
        load_history_days=9.0, load_profile_30m_kwh=tuple(15.09 / 48 for _ in range(48)),
        tariff_price_schedule=span(official("pge", "G12"), start, start + timedelta(hours=48)),
        self_consumption_filter_enabled=True,
    )


def assert_no_execution(result):
    assert not result.planned_exports
    assert not result.current_slot_start_eligible
    assert result.current_run_end is None
    for name in ("current_slot_planned_export_kwh", "current_slot_execution_export_power_kw",
                 "current_slot_execution_discharge_power_kw", "current_slot_execution_power_percent"):
        assert getattr(result, name) == 0.0, name


def pure_contract():
    captured = night_settings()
    original = RCE.optimize_rce(deepcopy(captured))
    assert original.ready is False and original.status_code == "home_energy_shortage"
    assert_no_execution(original)
    latest = replace(captured, now=NOW + timedelta(seconds=4), current_load_power_kw=0.73)
    result = RCE.revalidate_rce_plan(latest, original, captured_settings=captured)
    assert result is not None, "fresh valid shortage incorrectly discarded as stale result"
    assert result.ready is False and result.status_code == "home_energy_shortage"
    assert_no_execution(result)
    assert result.current_slot_load_source == 'shared_forecast'
    assert abs(result.current_slot_load_kwh - original.current_slot_load_kwh
               / original.current_slot_remaining_minutes * result.current_slot_remaining_minutes) < 1e-12
    for change in (
        {"current_battery_soc_fresh": False}, {"bms_discharge_data_fresh": False},
        {"bms_discharge_data_age_seconds": 301.0}, {"battery_soc_percent": float("nan")},
        {"manual_minimum_soc_percent": 59.0}, {"current_load_power_kw": None},
    ):
        assert RCE.revalidate_rce_plan(replace(latest, **change), original,
            captured_settings=captured) is None, change
    for status in ("missing_data", "optimizer_error", "unknown"):
        bad = deepcopy(original)
        bad.status_code = status
        assert RCE.revalidate_rce_plan(latest, bad, captured_settings=captured) is None, status
    bad = deepcopy(original)
    bad.current_slot_execution_power_percent = 1.0
    assert RCE.revalidate_rce_plan(latest, bad, captured_settings=captured) is None
    recovered = RCE.revalidate_rce_plan(replace(latest, battery_soc_percent=90.0), original,
        captured_settings=captured)
    assert recovered is not None and recovered.ready is True
    assert_no_execution(recovered)
    # A newly discovered shortage during an otherwise valid zero-cap plan
    # requires one new full solve; that next negative result then publishes.
    zero_cap = replace(captured, battery_soc_percent=90.0, export_power_cap_kw=0.0)
    before_shortage = RCE.optimize_rce(zero_cap)
    assert before_shortage.ready is True
    assert_no_execution(before_shortage)
    low = replace(zero_cap, now=latest.now, battery_soc_percent=27.0)
    assert RCE.revalidate_rce_plan(low, before_shortage, captured_settings=zero_cap) is None
    next_solve = RCE.optimize_rce(low)
    assert next_solve.ready is False and next_solve.status_code == "home_energy_shortage"
    next_current = RCE.revalidate_rce_plan(replace(low, now=low.now + timedelta(seconds=4)),
        next_solve, captured_settings=low)
    assert next_current is not None
    assert_no_execution(next_current)
    # Installation 1, 00:16 observation. Prices and intraday PV/LOAD shape were not
    # exported, so these are explicitly approximate early/delayed PV cases.
    # They preserve measured totals, SOC/floors, live LOAD and night window.
    local_now = datetime(2026, 9, 27, 0, 16, tzinfo=ZoneInfo("Europe/Warsaw"))
    start = RCE.floor_half_hour(local_now)
    prices = [RCE.PriceSlot(start + RCE.SLOT * i, 0.8 if 17 <= (start + RCE.SLOT * i).hour < 21 else 0.3)
              for i in range(48)]
    for solar_start in (7 * 60, 8 * 60 + 30):
        daylight = [row.start for row in prices if solar_start <= row.start.hour * 60 + row.start.minute < 17 * 60]
        pv = {stamp: 28.97 / len(daylight) for stamp in daylight}
        local = replace(captured, now=local_now, price_slots=prices,
            pv_by_slot_kwh=pv, conservative_pv_by_slot_kwh=pv,
            battery_capacity_kwh=26.0, battery_soc_percent=47.0,
            outage_reserve_soc_percent=25.0, manual_minimum_soc_percent=0.0,
            safety_margin_soc_percent=0.0, average_daily_load_kwh=17.0,
            average_night_load_kwh=10.5, current_load_power_kw=2.414,
            night_start_minute=17 * 60 + 9, night_end_minute=8 * 60 + 16,
            load_profile_30m_kwh=(), day3_pv_forecast_kwh=0.0,
            tariff_price_schedule=span(official("pge", "G12"), start, start + timedelta(hours=48)))
        local_result = RCE.optimize_rce(deepcopy(local))
        local_current = RCE.revalidate_rce_plan(replace(local, now=local_now + timedelta(seconds=4)),
            local_result, captured_settings=local)
        assert local_current is not None, (solar_start, local_result.status_code)
        if local_result.ready is False:
            assert local_result.status_code == "home_energy_shortage"
            assert local_current.ready is False and local_current.status_code == "home_energy_shortage"
            assert_no_execution(local_current)
        assert local_current.current_slot_start_eligible is False
        print(f"PASS Installation 1, 00:16 approximate PVstart={solar_start}: captured={local_result.status_code}, fresh={local_current.status_code}")


async def publication_contract():
    h = fixture.h
    h.CLOCK["now"] = fixture.NOW
    hass, _, _, _, source, _, renderer, _ = await fixture.solver_probe()
    h.CLOCK["now"] = NOW
    source._result = None
    source._last_full_plan_at = None
    source._full_plan_solver_calls = 0
    source._attributes = {"status_code": "missing_data", "result_current": False,
                          "recalculation_pending": True, "planned_slots": []}
    captured = night_settings()
    rendered = []

    def provider():
        return replace(captured, now=h.CLOCK["now"]), {
            "rce_today_data_fresh": True, "forecast_today_data_fresh": True,
            "soc_data_fresh": True, "gcf_execution_data_fresh": True,
            "rce_today_age_seconds": 0.0, "forecast_today_age_seconds": 0.0,
        }

    async def executor(func, *args):
        result = func(*args)
        h.CLOCK["now"] += timedelta(seconds=4)
        await asyncio.sleep(0)
        return result

    def publish():
        hass.states.values["sensor.hoymiles_hit_rce_optimized_plan"] = h.FakeState(
            source.native_value, dict(source._attributes), h.CLOCK["now"],
        )
        rendered.append(renderer.publish_all(h.CLOCK["now"]))

    source._optimizer_input = provider
    source.async_write_ha_state = publish
    hass.async_add_executor_job = executor
    source._invalidate_internal_inputs()
    await source._recalculate_and_write()
    assert source._full_plan_solver_calls == 1
    assert source._attributes["result_current"] is True, source._attributes
    assert source._attributes["recalculation_pending"] is False
    assert source._attributes["status_code"] == "home_energy_shortage"
    assert source._attributes["execution_input_valid"] is False
    assert source._attributes["execution_blocker_code"] == "home_energy_shortage"
    assert source._attributes["current_slot_continue_eligible"] is False
    assert source._attributes["current_slot_start_eligible"] is False
    assert_no_execution(source._result)
    assert rendered[-1]["hoymiles_rce_control_data_ready"] == "off"
    assert rendered[-1]["hoymiles_rce_reserve_ready"] == "off"


if __name__ == "__main__":
    pure_contract()
    asyncio.run(publication_contract())
    print("PASS real nighttime shortage: fresh diagnostic publication, latest physics, zero authority, actual YAML helpers deny, 10 negative guards")
