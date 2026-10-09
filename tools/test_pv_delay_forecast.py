"""PV deferral must qualify each day's own, consistently scaled lower forecast."""
import unittest
from datetime import timedelta

from test_rce_forecast_market_scope import fixture
from custom_components.hoymiles_hit_modbus.pv_charge_delay import qualified_today_refill


class ForecastTests(unittest.TestCase):
    def test_user_profiles_change_only_delay_forecast(self):
        m, sensor, clock, states, put = fixture()
        helper = 'input_select.hoymiles_pv_charge_delay_profile'
        values = []
        for name, weight in [('Conservative', .55), ('Balanced', .50), ('Maximum', .20)]:
            put(helper, name)
            data, meta = sensor._optimizer_input()
            self.assertEqual(meta.get('pv_charge_delay_profile'), name)
            self.assertEqual(meta.get('pv_charge_delay_p10_weight'), weight)
            values.append(data)
        for data in values[1:]:
            self.assertEqual(data.pv_by_slot_kwh, values[0].pv_by_slot_kwh)
            self.assertEqual(data.conservative_pv_by_slot_kwh, values[0].conservative_pv_by_slot_kwh)
            self.assertEqual(data.critical_zero_pv_guard, values[0].critical_zero_pv_guard)
        keys = [k for k, v in values[0].delay_pv_by_slot_kwh.items() if v > 0 and k > clock[0]]
        self.assertTrue(keys)
        for key in keys:
            self.assertLess(values[0].delay_pv_by_slot_kwh[key], values[1].delay_pv_by_slot_kwh[key])
            self.assertLess(values[1].delay_pv_by_slot_kwh[key], values[2].delay_pv_by_slot_kwh[key])
        self.assertIn(helper, m.WATCHED_ENTITIES)

    def test_missing_or_invalid_profile_cannot_grant_delay_forecast(self):
        m, sensor, clock, states, put = fixture()
        helper = 'input_select.hoymiles_pv_charge_delay_profile'
        for value in [None, 'unknown', 'unavailable', '85%', 'aggressive']:
            if value is None:
                states.pop(helper, None)
            else:
                put(helper, value)
            data, meta = sensor._optimizer_input()
            self.assertIsNotNone(data, 'Other EMS planning must remain available')
            self.assertEqual(data.delay_pv_by_slot_kwh, {})
            self.assertIsNone(meta.get('pv_charge_delay_p10_weight'))

    def test_raw_p10_compares_to_raw_expected_not_corrected_expected(self):
        meta = dict(forecast_today_data_fresh=True, forecast_today_raw_kwh=20.,
                    forecast_today_kwh=18., forecast_today_p10_kwh=19.)
        self.assertTrue(qualified_today_refill(meta))
        for low in (21., 0., float('nan'), None):
            self.assertFalse(qualified_today_refill({**meta, 'forecast_today_p10_kwh': low}))

    def test_missing_tomorrow_p10_cannot_remove_todays_margin(self):
        m, sensor, clock, states, put = fixture()
        entity = m.TOMORROW_FORECAST_CANDIDATES[0]
        states[entity].attributes.pop('estimate10')
        for row in states[entity].attributes['detailedForecast']:
            row.pop('pv_estimate10')
        data, meta = sensor._optimizer_input()
        self.assertIsNotNone(data)
        self.assertTrue(data.critical_zero_pv_guard)  # battery sale stays protected
        self.assertTrue(qualified_today_refill(meta))
        rows = [(k, v) for k, v in data.pv_by_slot_kwh.items()
                if k.date() == clock[0].date() and k > clock[0] and v > 0]
        self.assertTrue(rows)
        self.assertTrue(all(0 < data.delay_pv_by_slot_kwh[k] < v for k, v in rows))
        tomorrow = clock[0].date() + timedelta(days=1)
        self.assertFalse(any(k.date() == tomorrow for k in data.delay_pv_by_slot_kwh))

    def test_tomorrow_band_does_not_change_todays_delay_map(self):
        m, sensor, clock, states, put = fixture()
        before, _ = sensor._optimizer_input()
        states[m.TOMORROW_FORECAST_CANDIDATES[0]].attributes['estimate90'] = 500.
        after, _ = sensor._optimizer_input()
        today = lambda data: {k:v for k,v in data.delay_pv_by_slot_kwh.items()
                              if k.date() == clock[0].date()}
        self.assertEqual(today(before), today(after))
        self.assertTrue(today(before))


if __name__ == '__main__':
    unittest.main()
