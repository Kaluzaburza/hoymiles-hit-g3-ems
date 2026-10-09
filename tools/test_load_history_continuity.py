"""A missing latest day cannot erase qualified household history in its window."""
from dataclasses import replace
from datetime import datetime, timedelta
import ast
from types import SimpleNamespace
import unittest
import test_load_model_freshness_alignment as f


class HistoryContinuityTest(unittest.TestCase):
    def test_localhost_gap_keeps_nine_qualified_days_and_original_timestamp(self):
        now = datetime(2026, 10, 4, 18, tzinfo=f.WARSAW)
        stamp = datetime(2026, 10, 3, 0, 3, 47, tzinfo=f.WARSAW)
        sensor = f._probe(stamp, now)
        daily = dict(zip(
            (f'2026-09-{n}' for n in range(24, 31)),
            (15.4, 14.8, 22.7, 26.8, 26.1, 22.7, 26.1),
        ))
        daily.update({'2026-10-01': 22.4, '2026-10-02': 19.1})
        sensor._extended_load_history = replace(
            sensor._extended_load_history, daily_energy_kwh=daily,
            daily_history_days=9, daily_coverage_ratio=0.999934,
            daily_quality_by_date={'2026-10-03': 'missing_end_edge'},
            profile_kwh_by_date={k: (v / 48,) * 48 for k, v in daily.items()},
        )
        sensor._load_history = sensor._extended_load_history
        values = f._load_values_method(configured_fallback=17.)(sensor, now)
        self.assertTrue(values['history_complete'])
        self.assertFalse(values['history_data_fresh'])
        self.assertEqual(values['profile_history'].daily_history_days, 9)
        self.assertEqual(values['profile_history'].profile_history_days, 9)
        self.assertGreater(values['profile_history'].daily_coverage_ratio, .99)
        self.assertNotIn('2026-10-03', values['profile_history'].daily_energy_kwh)
        self.assertEqual(values['diagnostic_history'].daily_quality_by_date['2026-10-03'], 'missing_end_edge')
        self.assertEqual(sensor._load_profile_generated_at, stamp)
        self.assertGreater(values['average_load'], 17.)
        bounded = f.shared.M._bounded_load_model_snapshot(dict(
            average_daily_home_load_kwh=values['average_load'],
            daily_history_days=9, daily_totals_kwh=tuple(daily.values()),
            daily_total_dates=tuple(daily), generated_at=stamp, ready=True), now=now)
        self.assertTrue(bounded.ready)
        self.assertEqual(bounded.generated_at, stamp)

    def test_history_ages_out_without_new_observations(self):
        now = datetime(2026, 10, 4, 18, tzinfo=f.WARSAW)
        sensor = f._probe(now - timedelta(hours=42), now)
        values = f._load_values_method(configured_fallback=7.)(sensor, now + timedelta(days=28))
        self.assertFalse(values['history_complete'])
        self.assertEqual(values['profile_history'].daily_history_days, 0)
        self.assertEqual(values['average_load'], 7.)

    def test_retained_history_boundaries_and_invalid_provenance(self):
        now = datetime(2026, 10, 4, 18, tzinfo=f.WARSAW)
        stamp = now - timedelta(hours=42)
        dates = tuple((now.date() - timedelta(days=n)).isoformat() for n in (1, 2, 28))
        usable = f.shared.M.qualified_load_history_is_usable
        self.assertTrue(usable(stamp, dates, now=now))
        for bad in ((), dates[:2], dates + (dates[0],), ('broken',) + dates[1:],
                    (now.date().isoformat(),) + dates[1:],
                    ((now.date() - timedelta(days=29)).isoformat(),) + dates[1:]):
            self.assertFalse(usable(stamp, bad, now=now), bad)
        for bad_stamp in (None, stamp.replace(tzinfo=None), now + timedelta(seconds=6), now - timedelta(days=28, seconds=1)):
            self.assertFalse(usable(bad_stamp, dates, now=now), bad_stamp)
        self.assertFalse(f.shared.M._bounded_load_model_snapshot(dict(
            average_daily_home_load_kwh=10., daily_history_days=9,
            daily_totals_kwh=(10.,)*3, daily_total_dates=dates,
            generated_at=stamp, ready=True), now=now).ready)
        dst_now = datetime(2026, 10, 25, 2, 30, tzinfo=f.WARSAW, fold=0)
        dst_dates = ('2026-10-22', '2026-10-23', '2026-10-24')
        self.assertFalse(usable(dst_now.replace(fold=1), dst_dates, now=dst_now))

    def test_calendar_projection_prunes_totals_and_profiles_without_mutating_cache(self):
        now = datetime(2026, 10, 4, 0, 30, tzinfo=f.WARSAW)
        sensor = f._probe(now - timedelta(hours=42), now)
        raw = replace(sensor._extended_load_history,
            daily_energy_kwh={'2026-09-05': 100., '2026-09-06': 20., '2026-09-07': 30., '2026-10-04': 99.},
            profile_kwh_by_date={'2026-09-05': (100./48,)*48, '2026-09-06': (20./48,)*48})
        bounded = f.load_history_store.history_in_window(raw, as_of=now.date())
        self.assertEqual(bounded.daily_energy_kwh, {'2026-09-06': 20., '2026-09-07': 30.})
        self.assertEqual(bounded.average_daily_kwh, 25.)
        self.assertEqual(bounded.profile_history_days, 1)
        self.assertEqual(len(raw.daily_energy_kwh), 4)
        self.assertEqual(len(raw.profile_kwh_by_date), 2)

    def test_actual_tariff_and_rcem_boundaries_require_fresh_broker_and_valid_days(self):
        now = datetime(2026, 10, 4, 18, tzinfo=f.WARSAW)
        stamp = now - timedelta(hours=42)
        dates = tuple((now.date()-timedelta(days=n)).isoformat() for n in (2, 3, 4))
        for filename, target in [('tariff_sensor.py', 'load_profile_broker_fresh'), ('rcm_sensor.py', 'load_profile_data_fresh')]:
            tree = ast.parse((f.COMPONENT/filename).read_text(encoding='utf-8'))
            # Execute the actual production gate expression, including its age
            # and ready requirements, rather than a copy of that policy.
            expressions = [n.value for n in ast.walk(tree) if isinstance(n, ast.Assign)
                and any(isinstance(t, ast.Name) and t.id == target for t in n.targets)
                and any(isinstance(c, ast.Name) and c.id == 'qualified_load_history_is_usable' for c in ast.walk(n.value))]
            self.assertEqual(len(expressions), 1)
            code = compile(ast.Expression(expressions[0]), filename, 'eval')
            for ready, age, sample_dates, expected in [(True, 60., dates, True), (False, 60., dates, False),
                    (True, 301., dates, False), (True, 60., dates[:2], False), (True, -6., dates, False)]:
                env = dict(now=now, shared_load=SimpleNamespace(ready=ready, daily_total_dates=sample_dates),
                    load_profile_generated_at=stamp, load_profile_age_seconds=age,
                    load_profile_snapshot_fresh=False, shared_fallback_profile_ready=False,
                    LOAD_BROKER_MAX_AGE_SECONDS=300., load_profile_snapshot_age=42*3600,
                    load_profile_data_fresh=(ready and -5. <= age <= 300.),
                    qualified_load_history_is_usable=f.shared.M.qualified_load_history_is_usable)
                self.assertEqual(eval(code, env), expected, (filename, ready, age, sample_dates))


if __name__ == '__main__':
    unittest.main()
