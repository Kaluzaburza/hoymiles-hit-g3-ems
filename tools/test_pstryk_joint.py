"""Executable contracts for the shared Pstryk trajectory and origin ledger."""
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from pathlib import Path
import sys
import unittest
import json

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'custom_components/hoymiles_hit_modbus'))
from pstryk_joint import EnergySlot, JointInput, optimize, simulate, revalidate_plan
from pstryk_origin import OriginLedger, CounterSample

NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)

def case(prices=(.1, .1, 1., 1.), loads=(0., 0., 1., 1.), pv=None, **kw):
    slots = tuple(EnergySlot(NOW + timedelta(minutes=30*i), NOW + timedelta(minutes=30*(i+1)),
                            price, loads[i], (pv or [0.]*len(prices))[i])
                  for i, price in enumerate(prices))
    return JointInput(slots=slots, capacity_kwh=10., initial_kwh=2., reserve_kwh=2.,
                      maximum_kwh=9., pv_origin_kwh=0., charge_kw=3., discharge_kw=3.,
                      ac_kw=5., export_kw=5., **kw)

class JointTests(unittest.TestCase):
    def test_large_battery_high_pv_field_replay_has_no_speculative_night_buy(self):
        raw=json.loads((Path(__file__).parent/'fixtures/pstryk_high_pv_night_grid_support.json').read_text())
        raw['slots']=tuple(EnergySlot(**(p|{'start':datetime.fromisoformat(p['start']),
            'end':datetime.fromisoformat(p['end'])})) for p in raw['slots'])
        data=JointInput(**raw)
        for soc in (83,84,85,86):
            scenario=replace(data,initial_kwh=data.capacity_kwh*soc/100)
            plan=optimize(scenario)
            self.assertAlmostEqual(sum(p.grid_import_kwh for p in plan.slots),0.)
            self.assertFalse(any(p.action=='buy' for p in plan.slots))
            self.assertGreaterEqual(plan.slots[-1].end_kwh+1e-8,data.terminal_kwh or data.reserve_kwh)
            self.assertTrue(all(p.end_kwh+1e-8>=scenario.reserve_kwh for p in plan.slots))
            self.assertGreater(sum(p.battery_export_kwh for p in plan.slots),0.)

    def test_grid_support_cannot_preserve_sufficient_home_stock_for_sale(self):
        data=replace(case(prices=(.6,.6,2.,.1),loads=(1.,1.,0.,0.),pv=(0.,0.,0.,8.)),
                     initial_kwh=8.,pv_origin_kwh=6.)
        baseline,_=simulate(data,(0,)*4)
        supported,_=simulate(data,(2,2,0,0))
        self.assertAlmostEqual(sum(p.grid_import_kwh for p in baseline),0.)
        self.assertAlmostEqual(sum(p.grid_import_kwh for p in supported),0.)
        self.assertEqual(supported,baseline)

    def test_grid_support_requires_home_shortfall_also_at_negative_price(self):
        for price in (-1.,.6):
            data=replace(case(prices=(price,)*4,loads=(.5,)*4,pv=(0,0,4,0)),
                         initial_kwh=8.,pv_origin_kwh=0.)
            plan=optimize(data)
            self.assertAlmostEqual(sum(p.grid_import_kwh for p in plan.slots),0.)
            self.assertFalse(any(p.action=='buy' for p in plan.slots))

    def test_fresh_revalidation_removes_buy_when_new_pv_covers_home(self):
        data=case();old=optimize(data)
        self.assertTrue(any(p.action=='buy' for p in old.slots))
        fresh=replace(data,initial_kwh=8.)
        updated=revalidate_plan(fresh,old,captured=data)
        self.assertIsNotNone(updated)
        self.assertFalse(any(p.action=='buy' for p in updated.slots))

    def test_fixed_revalidation_uses_live_physics_without_new_or_larger_trade(self):
        cases=[case(),replace(case(prices=(2.,.2,.2,.2),loads=(0,0,0,.1),pv=(0,3,3,0)),
                             initial_kwh=8.9,pv_origin_kwh=7.)]
        for data in cases:
            old=optimize(data)
            for seconds in (0,1,7,90):
                for delta in (-.01,0.,.01):
                    first=data.slots[0];ratio=(1800-seconds)/1800
                    fresh=replace(data,initial_kwh=data.initial_kwh+delta,
                        slots=(replace(first,start=first.start+timedelta(seconds=seconds),
                            load_kwh=first.load_kwh*ratio,pv_kwh=first.pv_kwh*ratio),*data.slots[1:]))
                    new=revalidate_plan(fresh,old,captured=data)
                    self.assertIsNotNone(new,(data,seconds,delta))
                    self.assertGreaterEqual(new.slots[-1].end_kwh+1e-8,data.terminal_kwh or data.reserve_kwh)
                    for a,b in zip(old.slots,new.slots):
                        self.assertLessEqual(b.command_kw,a.command_kw+1e-8)
                        self.assertLessEqual(b.battery_export_kwh,a.battery_export_kwh+1e-8)
                        self.assertLessEqual(b.grid_charge_kwh,a.grid_charge_kwh+1e-8)
                        self.assertTrue(b.action=='self_use' or b.action==a.action)
                        self.assertAlmostEqual(b.start_kwh+b.battery_in_kwh-b.battery_out_kwh,b.end_kwh)

    def test_revalidation_cannot_cross_slot_or_use_new_prices(self):
        data=case();plan=optimize(data)
        for first in (replace(data.slots[0],start=NOW+timedelta(seconds=121)),
                      replace(data.slots[0],net=9.),replace(data.slots[0],sale_blocked=True)):
            self.assertIsNone(revalidate_plan(replace(data,slots=(first,*data.slots[1:])),plan,captured=data))
        self.assertIsNone(revalidate_plan(replace(data,slots=data.slots[1:]),plan,captured=data))

    def test_inactive_selection_cannot_gain_authority_from_new_surplus(self):
        data=replace(case(),allow_buy=False);old=optimize(data)
        self.assertTrue(all(p.action=='self_use' for p in old.slots))
        live=replace(data,initial_kwh=9.,pv_origin_kwh=7.)
        new=revalidate_plan(live,old,captured=data)
        self.assertIsNotNone(new)
        self.assertTrue(all(p.action=='self_use' for p in new.slots))

    def test_unexecutable_sell_cannot_silently_defer_pv_charge(self):
        data=case(prices=(.9,.9,.1,.1),loads=(.2,)*4,pv=(2.,)*4)
        baseline,_=simulate(data,(0,)*4)
        attempted,_=simulate(data,(-2,0,0,0))
        self.assertEqual(attempted[0].action,'self_use')
        self.assertEqual(attempted[0].battery_export_kwh,0.)
        self.assertEqual(attempted[0].battery_in_kwh,baseline[0].battery_in_kwh)
        self.assertEqual(attempted[0].grid_export_kwh,baseline[0].grid_export_kwh)

    def test_start_profit_is_not_reearned_by_confirmed_continuation(self):
        data=replace(case(prices=(2.,.2,.2,.2),loads=(0,0,0,.1),pv=(0,3,3,0)),
                     initial_kwh=9.,pv_origin_kwh=7.)
        later=replace(data,initial_kwh=9.-3./60/.95,pv_origin_kwh=7.-3./60/.95,
            slots=(replace(data.slots[0],start=NOW+timedelta(minutes=1)),*data.slots[1:]))
        threshold=(optimize(data).benefit_pln+optimize(later).benefit_pln)/2
        data=replace(data,minimum_benefit_pln=threshold)
        later=replace(later,minimum_benefit_pln=threshold)
        first=optimize(data).slots[0]
        self.assertEqual(first.action,'sell')
        self.assertEqual(optimize(later).slots[0].action,'self_use')
        continued=replace(later,continuing_action='sell',continuing_until=first.end)
        self.assertEqual(optimize(continued).slots[0].action,'sell')
        self.assertEqual(optimize(replace(continued,export_kw=0.)).slots[0].action,'self_use')
        self.assertEqual(optimize(replace(continued,pv_origin_kwh=0.)).slots[0].action,'self_use')

    def test_minute_replan_with_small_soc_drift_keeps_useful_run(self):
        for action,data in [('buy',case()),('sell',replace(case(prices=(2.,.2,.2,.2),
                loads=(0,0,0,.1),pv=(0,3,3,0)),initial_kwh=9.,pv_origin_kwh=7.))]:
            energy,origin=data.initial_kwh,data.pv_origin_kwh
            for minute in range(30):
                live=replace(data,initial_kwh=energy,pv_origin_kwh=origin,
                    slots=(replace(data.slots[0],start=NOW+timedelta(minutes=minute)),*data.slots[1:]))
                p=optimize(live).slots[0]
                self.assertEqual(p.action,action,(action,minute))
                if action=='buy': energy=min(energy+p.command_kw/60*.95,p.end_kwh)
                else: energy-=p.command_kw/60/.95; origin=max(origin-p.command_kw/60/.95,0.)
                if minute==7: energy-=.005; origin=min(origin,energy)

    def test_grid_charge_energy_matches_integer_soc_target(self):
        plan=optimize(case())
        for point in plan.slots:
            if point.grid_charge_kwh:
                self.assertAlmostEqual(point.end_kwh/10.*100,round(point.end_kwh/10.*100))

    def test_cheap_purchase_serves_later_home(self):
        data = case()
        plan = optimize(data)
        self.assertGreater(sum(s.grid_charge_kwh for s in plan.slots), 0)
        self.assertGreater(plan.benefit_pln, .5)
        self.assertTrue(all(s.battery_export_kwh == 0 for s in plan.slots))
        self.assertGreaterEqual(plan.slots[-1].end_kwh + 1e-8, plan.baseline_end_kwh)

    def test_equal_prices_do_not_cycle(self):
        self.assertFalse(any(s.grid_charge_kwh for s in optimize(case(prices=(.3,)*4)).slots))

    def test_negative_prices_do_not_create_speculative_stock(self):
        plan = optimize(case(prices=(-1., -1., 2., 2.), loads=(0.,)*4))
        self.assertEqual(sum(s.grid_charge_kwh + s.battery_export_kwh for s in plan.slots), 0)

    def test_negative_sale_does_not_open_headroom_trade(self):
        data=replace(case(prices=(-.01,-1.,-1.,-1.),loads=(0,0,0,0),pv=(0,3,3,3)),
                     initial_kwh=9.,pv_origin_kwh=7.)
        self.assertEqual(sum(p.battery_export_kwh for p in optimize(data).slots),0.)

    def test_pv_headroom_not_double_counted(self):
        plan = optimize(case(pv=(3., 3., 0., 0.)))
        self.assertEqual(sum(s.grid_charge_kwh for s in plan.slots), 0)

    def test_unknown_or_grid_origin_not_sellable(self):
        data = replace(case(loads=(0.,)*4), initial_kwh=8.)
        self.assertEqual(sum(s.battery_export_kwh for s in optimize(data).slots), 0)

    def test_pv_sale_preserves_home_stock(self):
        data = replace(case(prices=(2., .2, .2, .2)), initial_kwh=8., pv_origin_kwh=6.)
        plan = optimize(data)
        # RCE parity: free stock may be sold without replacing it, while the
        # house and hard reserve remain covered without any new grid import.
        self.assertGreater(sum(s.battery_export_kwh for s in plan.slots), 0)
        self.assertAlmostEqual(sum(s.grid_import_kwh for s in plan.slots),0.)
        self.assertGreaterEqual(plan.slots[-1].end_kwh,data.reserve_kwh-1e-8)
        data = replace(case(prices=(2., .2, .2, .2), loads=(0., 0., 0., .1), pv=(0.,3.,3.,0.)),
                       initial_kwh=9., pv_origin_kwh=7.)
        plan = optimize(data)
        self.assertGreater(plan.slots[0].battery_export_kwh, 0)
        self.assertTrue(all(s.end_kwh >= data.reserve_kwh - 1e-8 for s in plan.slots))
        self.assertEqual(sum(s.grid_charge_kwh for s in plan.slots), 0)

    def test_zero_limits_and_permissions(self):
        for data in (replace(case(), charge_kw=0), replace(case(), allow_buy=False)):
            self.assertEqual(sum(s.grid_charge_kwh for s in optimize(data).slots), 0)
        data = replace(case(pv=(0,0,0,6)), initial_kwh=8., pv_origin_kwh=6., export_kw=0)
        self.assertEqual(sum(s.battery_export_kwh for s in optimize(data).slots), 0)

    def test_required_reserve_and_unpriced_tail_are_not_profit_filters(self):
        plan = optimize(replace(case(prices=(2.,)*4),initial_kwh=1.))
        self.assertTrue(plan.required_charge)
        self.assertGreater(plan.slots[0].grid_charge_kwh,0)
        data = replace(case(),terminal_kwh=4.)
        plan = optimize(data)
        self.assertTrue(plan.required_charge)
        self.assertGreater(plan.slots[-1].end_kwh,plan.baseline_end_kwh)

    def test_distinct_efficiencies_preserve_ac_dc_balance(self):
        data=replace(case(pv=(2,0,2,0)),charge_efficiency=.8,pv_charge_efficiency=.97,
                     discharge_efficiency=.91,sell_efficiency=.87)
        plan=optimize(data)
        for p in plan.slots:
            self.assertAlmostEqual(p.grid_import_kwh+p.pv_kwh+p.home_battery_kwh+p.battery_export_kwh,
                                   p.load_kwh+p.grid_export_kwh+p.grid_charge_kwh+p.pv_charge_kwh+p.curtailed_kwh)
            self.assertAlmostEqual(p.start_kwh+p.pv_charge_kwh*.97+p.grid_charge_kwh*.8-p.battery_out_kwh,p.end_kwh)

    def test_zero_dcl_does_not_buy_unusable_home_energy(self):
        data=replace(case(),discharge_kw=0.,demand_margin_percent=10.)
        self.assertEqual(sum(p.grid_charge_kwh for p in optimize(data).slots),0.)
        self.assertEqual(optimize(data).margin_requested_kwh,0.)

    def test_user_grid_limit_does_not_limit_pv_or_home_physics(self):
        data=replace(case(pv=(3,3,0,0)),charge_kw=0.,discharge_kw=0.,
                     pv_charge_kw=3.,charge_dc_kw=3.,home_discharge_kw=3.)
        plan=optimize(data)
        self.assertGreater(sum(p.pv_charge_kwh for p in plan.slots),0.)
        self.assertGreater(sum(p.home_battery_kwh for p in plan.slots),0.)
        self.assertEqual(sum(p.grid_charge_kwh+p.battery_export_kwh for p in plan.slots),0.)

    def test_partial_slot_and_balance(self):
        data = case()
        data = replace(data, slots=(replace(data.slots[0], start=NOW+timedelta(minutes=25)), *data.slots[1:]))
        for point in optimize(data).slots:
            self.assertAlmostEqual(point.grid_import_kwh + point.pv_kwh + point.battery_out_kwh * data.discharge_efficiency,
                                   point.load_kwh + point.grid_export_kwh + point.battery_in_kwh / data.charge_efficiency + point.curtailed_kwh)
        self.assertLessEqual(optimize(data).slots[0].grid_charge_kwh, 3/12)

    def test_bad_inputs_rejected(self):
        for value in (float('nan'), float('inf'), -1.):
            with self.assertRaises(ValueError): optimize(replace(case(), charge_kw=value))
        with self.assertRaises(ValueError): optimize(replace(case(), slots=(case().slots[0], case().slots[2])))

    def test_margin_is_consumable_energy(self):
        data = replace(case(), demand_margin_percent=10.)
        plan = optimize(data)
        self.assertAlmostEqual(plan.margin_requested_kwh, .2)
        self.assertTrue(all(s.end_kwh <= data.maximum_kwh + 1e-8 for s in plan.slots))

    def test_bounded_deterministic_and_no_opposed_actions(self):
        data = case(prices=(.1,.2,1,2)*24, loads=(.2,)*96, pv=(0,0,1,1)*24)
        a = optimize(data)
        self.assertEqual(a, optimize(data))
        # Shared RCE search includes day prefixes, removals and bounded swaps.
        # It is no longer the old three-pass greedy algorithm.
        self.assertLessEqual(a.simulations, 128*len(data.slots))
        self.assertTrue(all(not (p.grid_charge_kwh and p.battery_export_kwh) for p in a.slots))

class OriginTests(unittest.TestCase):
    def test_charge_and_output_in_same_interval_do_not_invent_pv(self):
        ledger=OriginLedger('entry'); ledger.observe(self.sample())
        ledger.observe(self.sample(60,pv=2.,charge=2.,discharge=2.))
        ledger.acknowledge(ledger.storage_candidate())
        self.assertEqual(ledger.available_kwh,0.)

    def sample(self, seconds=0, pv=0., charge=0., discharge=0., grid=0., **kw):
        return CounterSample(NOW+timedelta(seconds=seconds), pv, charge, discharge, grid, 8., **kw)

    def test_unknown_initial_and_ack(self):
        ledger = OriginLedger('entry')
        ledger.observe(self.sample())
        ledger.observe(self.sample(60, pv=2., charge=2.))
        self.assertEqual(ledger.available_kwh, 0.)
        blob = ledger.storage_candidate()
        ledger.acknowledge(blob)
        self.assertAlmostEqual(ledger.available_kwh, 1.4)
        ledger.observe(self.sample(90, pv=2., charge=2., discharge=1.))
        self.assertLess(ledger.available_kwh, 1.)

    def test_import_cannot_be_credited(self):
        ledger = OriginLedger('entry'); ledger.observe(self.sample())
        ledger.observe(self.sample(60, pv=2., charge=2., grid=2.))
        ledger.acknowledge(ledger.storage_candidate())
        self.assertEqual(ledger.available_kwh, 0.)

    def test_restart_requires_fresh_reconciliation(self):
        ledger = OriginLedger('entry'); ledger.observe(self.sample())
        ledger.observe(self.sample(60, pv=2., charge=2.))
        blob = ledger.storage_candidate(); ledger.acknowledge(blob)
        restored = OriginLedger.restore('entry', blob)
        self.assertEqual(restored.available_kwh, 0.)
        restored.observe(self.sample(90, pv=2., charge=2., discharge=1.))
        restored.acknowledge(restored.storage_candidate())
        self.assertLess(restored.available_kwh, 1.)
        self.assertGreater(restored.available_kwh, .1)

    def test_bad_scope_reset_gap_tombstone(self):
        for seconds, pv in ((600, 2.), (90, .1), (86400, 2.)):
            ledger = OriginLedger('entry'); ledger.observe(self.sample())
            ledger.observe(self.sample(60, pv=2., charge=2.))
            ledger.acknowledge(ledger.storage_candidate())
            ledger.observe(self.sample(seconds, pv=pv, charge=2.))
            self.assertEqual(ledger.available_kwh, 0.)
        with self.assertRaises(ValueError): OriginLedger.restore('different', ledger.storage_candidate())

    def test_no_per_callback_storage_and_bounded_payload(self):
        ledger = OriginLedger('entry'); ledger.observe(self.sample())
        before = ledger.storage_candidate(); ledger.acknowledge(before)
        ledger.observe(self.sample(60))
        self.assertEqual(before, ledger.storage_candidate())
        self.assertLess(len(before), 1200)

    def test_late_ack_cannot_restore_debited_energy(self):
        ledger = OriginLedger('entry'); ledger.observe(self.sample())
        ledger.observe(self.sample(60, pv=2., charge=2.))
        old = ledger.storage_candidate()
        ledger.observe(self.sample(90, pv=2., charge=2., discharge=2.))
        ledger.acknowledge(old)
        self.assertEqual(ledger.available_kwh, 0.)

if __name__ == '__main__': unittest.main()
