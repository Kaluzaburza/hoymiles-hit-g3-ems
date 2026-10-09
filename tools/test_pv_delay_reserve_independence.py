"""PV deferral preserves existing stock instead of requiring a SELL reserve."""
from dataclasses import replace
from datetime import timedelta
import unittest

from test_pv_charge_delay_adapter import settings, rce_delay, result
from test_pv_charge_delay import case, trial
from pstryk_joint import optimize, simulate, revalidate_plan
from custom_components.hoymiles_hit_modbus.pstryk_plan import projections


def below_reserve():
    data = replace(case(), initial_kwh=2.1, reserve_kwh=2.5, allow_buy=True)
    return replace(data, slots=tuple(replace(s, pv_kwh=.4) if i < 4 else s
        for i, s in enumerate(data.slots)))


class ReserveIndependenceTests(unittest.TestCase):
    def test_shared_delay_holds_actual_stock_below_sale_reserve(self):
        for reserve in (2.5, 5., 8.):
            data = replace(case(), initial_kwh=2.1, reserve_kwh=reserve)
            _, baseline, points, plan = trial(data)
            self.assertIsNotNone(plan)
            self.assertEqual(plan.start, data.slots[0].start)
            self.assertTrue(all(p.end_kwh >= data.initial_kwh-1e-8 for p in points))
            self.assertTrue(all(p.battery_out_kwh == 0 and p.grid_import_kwh == 0 for p in points))
            self.assertEqual(points[-1], baseline[-1])

    def test_pstryk_can_replace_uncommitted_reserve_only_buy_with_pv(self):
        data = below_reserve()
        old = optimize(replace(data, allow_delay=False))
        self.assertEqual(old.slots[0].action, 'buy')
        plan = optimize(data)
        self.assertIsNotNone(plan.delay_plan)
        self.assertEqual(plan.slots[0].action, 'pv_charge_hold')
        self.assertGreater(plan.benefit_pln, old.benefit_pln)
        self.assertGreaterEqual(plan.slots[-1].end_kwh+1e-8, old.slots[-1].end_kwh)
        self.assertTrue(all(p.grid_import_kwh <= b.grid_import_kwh+1e-8
            for p, b in zip(plan.slots, old.slots)))
        held = [p for p in plan.slots if p.action == 'pv_charge_hold']
        self.assertTrue(all(p.battery_in_kwh == 0 and p.battery_out_kwh == 0
            and p.grid_import_kwh == 0 for p in held))
        rce, tariff = projections(data, plan, revision=2, metadata={}, system_power_kw=5.)
        self.assertTrue(rce['pv_charge_delay_execution_ready'])
        self.assertFalse(rce['current_slot_planned'])
        self.assertFalse(tariff['current_slot_planned'])
        self.assertEqual(tariff['base_reserve_soc_percent'], 25.)
        self.assertIsNotNone(revalidate_plan(data, plan, captured=data))
        first = data.slots[0]
        fresh = replace(data, slots=(replace(first, start=first.start+timedelta(seconds=3),
            load_kwh=first.load_kwh*1797/1800, pv_kwh=first.pv_kwh*1797/1800), *data.slots[1:]))
        checked = revalidate_plan(fresh, plan, captured=data)
        self.assertIsNotNone(checked)
        self.assertEqual(checked.delay_plan.end, plan.delay_plan.end)
        self.assertLessEqual(checked.delay_plan.recovered_at, plan.delay_plan.recovered_at)

    def test_rce_adapter_ignores_sale_safety_margin_for_hold(self):
        for margin in (15., 40.):
            config = replace(settings(), battery_soc_percent=21.,
                outage_reserve_soc_percent=10., safety_margin_soc_percent=margin)
            plan = rce_delay(config, result(), enabled=True)
            self.assertIsNotNone(plan)
            self.assertEqual(plan.start, config.now)

    def test_active_buy_is_not_replaced(self):
        data = below_reserve()
        active = replace(data, continuing_action='buy', continuing_until=data.slots[0].end)
        before = optimize(replace(active, allow_delay=False))
        after = optimize(active)
        self.assertEqual(after.slots[0], before.slots[0])
        if after.delay_plan:
            self.assertGreaterEqual(after.delay_plan.start, active.continuing_until)

    def test_low_soc_active_hold_keeps_deadline_and_recovery_across_replans(self):
        data = replace(case(), initial_kwh=2.1, reserve_kwh=2.5, allow_buy=True)
        plan = optimize(data)
        original = plan.delay_plan
        self.assertIsNotNone(original)
        for _ in range(2):
            data = replace(data, slots=data.slots[1:], initial_kwh=plan.slots[0].end_kwh,
                pv_origin_kwh=plan.slots[0].pv_origin_kwh, active_delay=original)
            plan = optimize(data)
            self.assertIsNotNone(plan.delay_plan)
            self.assertEqual(plan.slots[0].action, 'pv_charge_hold')
            self.assertEqual((plan.delay_plan.start, plan.delay_plan.end,
                plan.delay_plan.recovered_at, plan.delay_plan.recovery_target_kwh),
                (original.start, original.end, original.recovered_at, original.recovery_target_kwh))
            self.assertIsNotNone(revalidate_plan(data, plan, captured=data))

    def test_lower_forecast_and_existing_home_demand_still_guard_delay(self):
        data = below_reserve()
        weak = replace(data, delay_pv_kwh=(.25,)*len(data.slots))
        self.assertIsNone(optimize(weak).delay_plan)
        dark = replace(data, slots=tuple(replace(s, pv_kwh=0.) for s in data.slots))
        self.assertIsNone(optimize(dark).delay_plan)
        demand = replace(data, slots=tuple(replace(s, load_kwh=1.) if i < 4 else s
            for i, s in enumerate(data.slots)))
        before = optimize(replace(demand, allow_delay=False))
        after = optimize(demand)
        self.assertEqual(after.slots[0], before.slots[0])
        self.assertTrue(all(p.grid_import_kwh <= b.grid_import_kwh+1e-8
            for p, b in zip(after.slots, before.slots)))
        lower = replace(data, delay_pv_kwh=tuple(0. if i == 0 else s.pv_kwh
            for i, s in enumerate(data.slots)))
        self.assertEqual(optimize(lower).slots[0],
            optimize(replace(lower, allow_delay=False)).slots[0])

    def test_sell_reserve_and_disabled_behavior_are_unchanged(self):
        data = below_reserve()
        old = optimize(replace(data, allow_delay=False))
        self.assertTrue(old.required_charge)
        self.assertEqual(old.slots[0].action, 'buy')
        sale, _ = simulate(replace(data, allow_buy=False), (-2,)*len(data.slots))
        self.assertEqual(sale[0].battery_export_kwh, 0.)
        self.assertTrue(all(p.end_kwh+1e-8 >= data.reserve_kwh
            for p in sale if p.battery_export_kwh > 0))


if __name__ == '__main__':
    unittest.main()
