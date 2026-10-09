"""Late PV starts retain the local cutoff and conservative refill contract."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'custom_components/hoymiles_hit_modbus'))
from pstryk_joint import EnergySlot, JointInput, optimize, simulate
from pv_charge_delay import no_window_reason, optimize_delay

Z = ZoneInfo('Europe/Warsaw')


def late_case(hour=13, minute=30, month=10):
    start = datetime(2026, month, 3, hour, minute, tzinfo=Z)
    at = start.astimezone(timezone.utc)
    stop = start.replace(hour=18, minute=0).astimezone(timezone.utc)
    rows = []
    while at < stop:
        end = min(at + timedelta(minutes=30), stop)
        hours = (end - at).total_seconds() / 3600
        rows.append(EnergySlot(at, end, .9 if at.astimezone(Z).hour < 15 else .1,
            .24 * hours, 6. * hours))
        at = end
    return JointInput(tuple(rows), 10., 7., 1., 10., 7., 4., 3., 5., 8.,
        wear_pln_kwh=10., allow_buy=False, allow_sell=True, allow_delay=True)


class StartCutoffTests(unittest.TestCase):
    def test_new_start_before_1400_in_local_summer_and_winter_time(self):
        for month in (1, 7, 10):
            for hour, minute in ((12, 0), (13, 30), (13, 59)):
                with self.subTest(month=month, hour=hour, minute=minute):
                    data = late_case(hour, minute, month)
                    baseline, _ = simulate(data, (0,) * len(data.slots))
                    rows, plan = optimize_delay(data, baseline)
                    self.assertIsNotNone(plan)
                    self.assertEqual(plan.start, data.slots[0].start)
                    self.assertLess(plan.start.astimezone(Z).hour, 14)
                    self.assertLessEqual(plan.recovered_at.astimezone(Z).time(),
                        datetime.min.replace(hour=16, minute=30).time())
                    self.assertLessEqual(plan.recovered_at, data.slots[-1].end - timedelta(hours=1))
                    self.assertEqual(rows[-1], baseline[-1])
                    self.assertTrue(all(p.grid_import_kwh == b.grid_import_kwh
                        and p.battery_out_kwh == b.battery_out_kwh for p, b in zip(rows, baseline)))
                    self.assertTrue(all(p.end_kwh >= data.reserve_kwh for p in rows))

    def test_no_new_start_at_or_after_1400_and_reason_matches(self):
        for hour, minute in ((14, 0), (14, 1), (15, 0)):
            data = late_case(hour, minute)
            baseline, _ = simulate(data, (0,) * len(data.slots))
            self.assertIsNone(optimize_delay(data, baseline)[1])
            self.assertEqual(no_window_reason(data, baseline), 'tomorrow_prices_pending')
        data = late_case(12, 0)
        baseline, _ = simulate(data, (0,) * len(data.slots))
        self.assertNotEqual(no_window_reason(data, baseline), 'tomorrow_prices_pending')

    def test_lower_forecast_and_remaining_refill_time_still_gate_late_start(self):
        data = late_case()
        self.assertIsNotNone(optimize(data).delay_plan)
        weak = replace(data, delay_pv_kwh=(.05,) * len(data.slots))
        self.assertIsNone(optimize(weak).delay_plan)
        short = replace(data, slots=data.slots[:4])
        self.assertIsNone(optimize(short).delay_plan)
        for change in ({'allow_sell': False}, {'export_kw': 0.}, {'charge_dc_kw': 0.}):
            self.assertIsNone(optimize(replace(data, **change)).delay_plan)


if __name__ == '__main__':
    unittest.main()
