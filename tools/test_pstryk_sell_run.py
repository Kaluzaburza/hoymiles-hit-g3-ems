"""Pstryk SELL deadlines follow confirmed contiguous slots, as in RCE.

Uses the joint energy simulation and the production execution projection.
BUY retains the already-frozen contiguous-run contract. No device access.
"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from test_pstryk_active_run_commitment import P, J, fixture, selected


class SellRunTests(unittest.TestCase):
    def project(self, data, plan):
        return P.projections(data, plan, revision=1, metadata={}, system_power_kw=5.)

    def test_sell_spans_different_hourly_prices(self):
        before, _, _ = fixture()
        before = replace(before, price_slots=[replace(p, price_pln_kwh=1.+i//2)
                                             for i, p in enumerate(before.price_slots)])
        data = P.build_joint_input(before, dict(charge_efficiency=95., charge_power_percent=60.,
            maximum_soc=100., minimum_saving=.01, demand_margin_percent=0.,
            allow_buy=True, allow_sell=True))
        plan = selected(data, {i: 1. for i in range(4)})
        sale, buy = self.project(data, plan)
        self.assertEqual(sale['current_run_end'], plan.slots[3].end.isoformat())
        self.assertTrue(sale['current_slot_start_eligible'])
        self.assertFalse(buy['current_run_start_eligible'])
        self.assertEqual(sale['current_slot_end'], plan.slots[0].end.isoformat())

    def test_neutral_buy_hold_or_missing_slot_breaks_the_sell_run(self):
        _, data, _ = fixture()
        plan = selected(data, {i: 1. for i in range(6)})
        for action in ('self_use', 'buy', 'pv_charge_hold'):
            with self.subTest(action=action):
                points = (*plan.slots[:3], replace(plan.slots[3], action=action), *plan.slots[4:])
                sale, _ = self.project(data, replace(plan, slots=points))
                self.assertEqual(sale['current_run_end'], plan.slots[2].end.isoformat())
        sale, _ = self.project(data, replace(plan, slots=(*plan.slots[:3], *plan.slots[4:])))
        self.assertEqual(sale['current_run_end'], plan.slots[2].end.isoformat())

    def test_buy_contiguous_run_respects_gaps_and_original_deadline(self):
        _, original, _ = fixture()
        data = replace(original, initial_kwh=10., pv_origin_kwh=0.)
        home = J.optimize(replace(data, allow_sell=False))
        points, cost = J.simulate(data, (3,)*len(data.slots))
        self.assertTrue(all(p.action == 'buy' for p in points))
        sale, buy = self.project(data, replace(home, slots=points, cost_pln=cost))
        self.assertFalse(sale['current_slot_start_eligible'])
        self.assertEqual(buy['current_run_end'], points[-1].end.isoformat())
        self.assertEqual(buy['current_run_remaining_minutes'], 240.)
        for action in ('self_use', 'sell', 'pv_charge_hold'):
            broken = (*points[:3], replace(points[3], action=action), *points[4:])
            _, buy = self.project(data, replace(home, slots=broken, cost_pln=cost))
            self.assertEqual(buy['current_run_end'], points[2].end.isoformat())
        _, buy = self.project(data, replace(home, slots=(*points[:3], *points[4:]), cost_pln=cost))
        self.assertEqual(buy['current_run_end'], points[2].end.isoformat())
        _, buy = self.project(data, replace(home, slots=points, cost_pln=cost,
            active_slot_commitment_applied=True, active_run_deadline=points[1].end))
        self.assertEqual(buy['current_run_end'], points[1].end.isoformat())
        self.assertEqual(buy['current_run_remaining_minutes'], 60.)

    def test_midnight_and_dst_use_contiguous_instants(self):
        _, data, plan = fixture()
        for start in (datetime(2026, 10, 3, 21, 30, tzinfo=timezone.utc),
                      datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)):
            with self.subTest(start=start):
                shifted = tuple(replace(p, start=start+timedelta(minutes=30*i),
                    end=start+timedelta(minutes=30*(i+1))) for i, p in enumerate(plan.slots))
                sale, _ = self.project(data, replace(plan, slots=shifted))
                self.assertEqual(sale['current_run_end'], (start+timedelta(hours=2)).isoformat())


if __name__ == '__main__':
    unittest.main()
