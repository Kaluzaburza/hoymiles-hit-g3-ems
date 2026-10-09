"""Production adapter: pending market prices must not erase fresh Solcast PV."""
from __future__ import annotations

import copy
from dataclasses import replace
from datetime import timedelta
import importlib
import unittest

import test_rce_daylight_publication as daylight


def fixture():
    m, sensor, clock, states, put = daylight.fixture()
    put('input_select.hoymiles_pv_charge_delay_profile', 'Conservative')
    for suffix, value in {
        "battery_capacity": 230, "overview_battery_soc": 69,
        "ems_self_use_soc_readback": 20,
        "maximum_discharge_current": 950, "maximum_charge_current": 950,
        "number_of_machines_master_and_slave": 2,
        "overview_pv_total_power": 16000, "gcf_enable_readback_code": 0,
    }.items():
        put("sensor.hoymiles_hit_" + suffix, value)
    put(m.EMS_INVERTER_RATED_POWER_HELPER, "20 kW")
    put("input_number.hoymiles_rce_soc_safety_margin", 40)
    put("input_number.hoymiles_ems_fallback_daily_home_load", 40)
    put("input_boolean.hoymiles_rce_dynamic_soc_enabled", "on")
    for entity, offset, total, p10 in (
        (m.TODAY_FORECAST_CANDIDATES[0], 0, 106.28, 78.92),
        (m.TOMORROW_FORECAST_CANDIDATES[0], 1, 107.27, 90.55),
    ):
        attrs = copy.deepcopy(states[m.TODAY_FORECAST_CANDIDATES[0]].attributes)
        for row in attrs["detailedForecast"]:
            start = daylight.datetime.fromisoformat(row["period_start"])
            row["period_start"] = (start + timedelta(days=offset)).isoformat()
            daylight_slot = 6 <= start.hour < 18
            row["pv_estimate"] = total / 12 if daylight_slot else 0.0
            row["pv_estimate10"] = p10 / 12 if daylight_slot else 0.0
            row["pv_estimate90"] = total / 12 if daylight_slot else 0.0
            row["dampening_factor"] = 1.0
        attrs.update(estimate10=p10, estimate90=total)
        put(entity, total, attrs)
    put(m.REMAINING_TODAY_CANDIDATES[0], 84.69)
    return m, sensor, clock, states, put


class ForecastMarketScopeTest(unittest.TestCase):
    def test_fresh_pv_and_p10_survive_pending_tomorrow_prices(self):
        m, sensor, clock, states, put = fixture()
        settings, meta = sensor._optimizer_input()
        self.assertIsNotNone(settings, meta.get("missing_entities"))
        self.assertEqual(meta["planning_scope"], "today_only")
        self.assertTrue(meta["tomorrow_data_pending"])
        self.assertEqual(meta["forecast_tomorrow_p10_kwh"], 90.55)
        self.assertEqual(meta["forecast_tomorrow_factor_used"], 1.0)
        self.assertEqual(meta["forecast_tomorrow_learning_mode"], "solcast_adaptive")
        self.assertFalse(settings.critical_zero_pv_guard)
        self.assertEqual({s.start.date() for s in settings.price_slots}, {clock[0].date()})
        tomorrow = clock[0].date() + timedelta(days=1)
        self.assertAlmostEqual(sum(v for k, v in settings.pv_by_slot_kwh.items()
                                   if k.astimezone(daylight.TZ).date() == tomorrow), 107.27)

        # The zero-PV guard still protects SELL. Household feasibility now
        # uses Self-Use, so an unsellable buffer is not a home energy shortage.
        opt = importlib.import_module("custom_components.hoymiles_hit_modbus.rce_optimizer")
        actual = opt._optimize_rce_impl(settings, fixed_exports={})
        erased = opt._optimize_rce_impl(replace(settings, critical_zero_pv_guard=True,
                                               critical_zero_pv_guard_reason="p10_missing"),
                                        fixed_exports={})
        self.assertNotEqual(actual.status_code, "home_energy_shortage")
        self.assertEqual(erased.status_code, "home_protected")
        self.assertTrue(erased.critical_zero_pv_guard_active)
        self.assertGreater(erased.critical_zero_pv_guarded_kwh, 0)
        self.assertEqual(erased.planned_export_kwh, 0)
        self.assertEqual(actual.base_reserve_energy_kwh, 46)
        self.assertEqual(actual.sale_base_reserve_energy_kwh, 138)
        self.assertGreaterEqual(actual.ending_battery_kwh, 46 - 1e-6)
        self.assertEqual(actual.critical_zero_pv_guarded_kwh, 0)
        print("PASS causal adapter + physics: valid P10 retained; 46kWh home / 138kWh sale floors")

    def test_price_arrival_changes_market_scope_without_changing_pv(self):
        m, sensor, clock, states, put = fixture()
        before, _ = sensor._optimizer_input()
        rows = copy.deepcopy(states["sensor.hoymiles_rce_day"].attributes["value"])
        for row in rows:
            row["dtime_utc"] = (daylight.datetime.fromisoformat(row["dtime_utc"])
                                + timedelta(days=1)).isoformat()
            row["business_date"] = (clock[0].date() + timedelta(days=1)).isoformat()
        put("sensor.hoymiles_rce_day_tomorrow", 500, {"value": rows})
        after, meta = sensor._optimizer_input()
        self.assertEqual(meta["planning_scope"], "today_and_tomorrow")
        self.assertEqual(before.pv_by_slot_kwh, after.pv_by_slot_kwh)
        self.assertEqual(before.conservative_pv_by_slot_kwh, after.conservative_pv_by_slot_kwh)
        self.assertFalse(after.critical_zero_pv_guard)

    def test_missing_stale_or_wrong_day_forecast_stays_guarded(self):
        for failure in ("missing", "stale", "wrong_day", "missing_p10"):
            with self.subTest(failure=failure):
                m, sensor, clock, states, put = fixture()
                entity = m.TOMORROW_FORECAST_CANDIDATES[0]
                state = states[entity]
                if failure == "missing":
                    states.pop(entity)
                elif failure == "stale":
                    state.last_reported = state.last_updated = state.last_changed = clock[0] - timedelta(days=3)
                elif failure == "wrong_day":
                    for row in state.attributes["detailedForecast"]:
                        row["period_start"] = (daylight.datetime.fromisoformat(row["period_start"])
                                                + timedelta(days=7)).isoformat()
                else:
                    state.attributes.pop("estimate10")
                    for row in state.attributes["detailedForecast"]:
                        row.pop("pv_estimate10")
                settings, meta = sensor._optimizer_input()
                self.assertIsNotNone(settings)
                self.assertTrue(settings.critical_zero_pv_guard)
                self.assertEqual(meta["planning_scope"], "today_only")
                if failure != "missing_p10":
                    self.assertEqual(meta["forecast_tomorrow_kwh"], 0.0)

    def test_localized_native_state_is_valid_text(self):
        m, sensor, _, _, _ = fixture()
        sensor._attributes = {"status_code": "home_energy_shortage"}
        self.assertEqual(sensor.native_value,
                         "Za mało energii na potrzeby domu — sprzedaż zablokowana")
        sensor._attributes = {"status_code": "ready", "planning_scope": "today_only"}
        self.assertEqual(sensor.native_value,
                         "Gotowa — plan zoptymalizowany — plan tylko na dziś; jutro zostanie przeliczone automatycznie")
        sensor.hass.config.language = "en"
        self.assertEqual(sensor.native_value,
                         "Ready — optimized plan — today-only plan; tomorrow will be recalculated automatically")


if __name__ == "__main__":
    unittest.main()
