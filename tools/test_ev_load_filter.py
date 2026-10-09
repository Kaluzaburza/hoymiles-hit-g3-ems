"""Offline EV learning contracts; raw LOAD is never rewritten."""
import unittest
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
import sys, types

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType('custom_components.hoymiles_hit_modbus')
pkg.__path__ = [str(ROOT/'custom_components/hoymiles_hit_modbus')]
sys.modules.setdefault('custom_components', types.ModuleType('custom_components'))
sys.modules['custom_components.hoymiles_hit_modbus'] = pkg
from custom_components.hoymiles_hit_modbus.ev_load_filter import (
    Config, integrate_day, project_history, power_kw, suspected_ev,
)
from custom_components.hoymiles_hit_modbus.rce_history import LoadHistorySummary


def history():
    rows = {}
    for i in range(14):
        row = [.5]*48
        if i in (2, 5, 8, 11, 13):
            row[20:24] = [6.]*4  # 1 kW home + 11 kW EV, two hours
        rows[(datetime(2026, 9, 16)+timedelta(days=i)).date().isoformat()] = tuple(row)
    return LoadHistorySummary(None, 14, {k:sum(v) for k,v in rows.items()},
        10., 14, {k:10. for k in rows}, profile_kwh_by_date=rows,
        profile_history_days=14, daily_coverage_ratio=1., profile_coverage_ratio=1.)


class Tests(unittest.TestCase):
    def test_user_trial_two_to_three_kw_for_one_to_two_hours(self):
        from dataclasses import replace
        for kw in (2.,2.5,3.):
            for hours in (1,2):
                raw=history()
                plain=(.5,)*48
                trial=list(plain)
                trial[20:20+2*hours]=[.5+kw/2]*(2*hours)
                profiles={k:plain for k in raw.daily_energy_kwh}
                day=sorted(profiles)[-1]
                profiles[day]=tuple(trial)
                raw=replace(raw,profile_kwh_by_date=profiles,
                    daily_energy_kwh={k:sum(v) for k,v in profiles.items()})
                with self.subTest(kw=kw,hours=hours):
                    view,removed=project_history(raw,Config(True,kw,''),{})
                    self.assertEqual(removed,1)
                    self.assertEqual(view.average_daily_kwh,24.)
                    self.assertEqual(raw.daily_energy_kwh[day],24.+kw*hours)
                    corrections={k:tuple(v-.5 for v in row) for k,row in profiles.items()}
                    measured,removed=project_history(raw,Config(True,kw,'sensor.ev'),corrections)
                    self.assertEqual(removed,0)
                    self.assertEqual(measured.daily_history_days,14)
                    self.assertEqual(measured.average_daily_kwh,24.)

    def test_off_is_exact_identity(self):
        raw = history()
        self.assertIs(project_history(raw, Config(False, 11., ''), {})[0], raw)

    def test_heuristic_excludes_days_without_nominal_subtraction(self):
        raw = history()
        view, removed = project_history(raw, Config(True, 11., ''), {})
        self.assertEqual(removed, 5)
        self.assertEqual(view.daily_history_days, 9)
        self.assertEqual(set(view.daily_energy_kwh.values()), {24.})
        self.assertEqual(view.average_profile_kwh, (.5,)*48)
        self.assertEqual(raw.daily_history_days, 14)
        self.assertEqual(raw.daily_energy_kwh['2026-09-29'], 46.)
        self.assertIsNone(view.average_night_kwh)

    def test_sensor_measured_energy_not_nominal(self):
        raw = history()
        corrections = {k:tuple(max(0., p-.5) for p in v) for k,v in raw.profile_kwh_by_date.items()}
        view, removed = project_history(raw, Config(True, 7., 'sensor.ev'), corrections)
        self.assertEqual(removed, 0)
        self.assertEqual(view.daily_history_days, 14)
        self.assertEqual(set(view.daily_energy_kwh.values()), {24.})

    def test_sensor_missing_and_excess_energy_rejected(self):
        raw = history()
        config = Config(True, 11., 'sensor.ev')
        self.assertEqual(project_history(raw, config, {})[0].daily_history_days, 0)
        excessive = {k:(100.,)*48 for k in raw.daily_energy_kwh}
        self.assertEqual(project_history(raw, config, excessive)[0].daily_history_days, 0)

    def test_power_units_and_invalid(self):
        self.assertEqual(power_kw('11000', 'W'), 11.)
        self.assertEqual(power_kw('11', 'kW'), 11.)
        for value, unit in [('nan','W'), ('-1','W'), ('unknown','W'), ('11','kWh'), ('1','MW')]:
            self.assertIsNone(power_kw(value, unit))

    def test_short_kettle_not_ev(self):
        self.assertFalse(suspected_ev(tuple([.5]*20+[1.5]+[.5]*27), 11.))
        self.assertFalse(suspected_ev((.5,)*48, 11.))

    def test_variable_pv_charging_requires_sensor(self):
        raw=history()
        from dataclasses import replace
        row=(.5,)*20+(1.5,2.,2.5,3.)+(.5,)*24
        profiles={k:row for k in raw.daily_energy_kwh}
        raw=replace(raw,profile_kwh_by_date=profiles,daily_energy_kwh={k:sum(row) for k in profiles})
        heuristic,_=project_history(raw,Config(True,11.,''),{})
        self.assertGreater(heuristic.average_daily_kwh,24.)
        measured,_=project_history(raw,Config(True,11.,'sensor.ev'),{k:tuple(p-.5 for p in row) for k in profiles})
        self.assertEqual(measured.average_daily_kwh,24.)

    def test_full_day_continuous_load_is_not_identifiable_as_ev(self):
        self.assertFalse(suspected_ev((6.,)*48,11.))

    def test_dst_energy_exact_and_sparse_positive_rejected(self):
        tz = ZoneInfo('Europe/Warsaw')
        for day, hours in [('2026-03-29',23), ('2026-10-25',25), ('2026-09-28',24)]:
            start = datetime.fromisoformat(day).replace(tzinfo=tz)
            end = start+timedelta(days=1)
            a, b = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
            rows = [(a+timedelta(minutes=5*i), '1000') for i in range(int((b-a).total_seconds()/300)+1)]
            profile = integrate_day(rows, start, 'W')
            self.assertIsNotNone(profile)
            self.assertAlmostEqual(sum(profile), hours)
            self.assertIsNone(integrate_day([(a,'1000'), (b,'0')], start, 'W'))
            self.assertEqual(sum(integrate_day([(a,'0')], start, 'W')), 0.)

    def test_unknown_duplicate_conflict_and_missing_start(self):
        start = datetime(2026,9,28,tzinfo=timezone.utc)
        self.assertIsNone(integrate_day([(start,'0'), (start+timedelta(hours=1),'unavailable')], start, 'W'))
        self.assertIsNone(integrate_day([(start,'0'), (start,'1000')], start, 'W'))
        self.assertIsNone(integrate_day([(start+timedelta(hours=1),'0')], start, 'W'))

if __name__ == '__main__': unittest.main()
