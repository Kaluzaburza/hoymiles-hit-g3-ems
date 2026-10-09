"""Deterministic deferral physics, economics and user permissions."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import math
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'custom_components/hoymiles_hit_modbus'))
from pstryk_joint import EnergySlot, JointInput, optimize, simulate, revalidate_plan
from pv_charge_delay import optimize_delay, hold_soc_target, attributes

NOW = datetime(2026, 9, 29, 6, tzinfo=timezone.utc)


def case():
    slots = tuple(EnergySlot(NOW+timedelta(minutes=30*i), NOW+timedelta(minutes=30*(i+1)),
        [.9,.9,.7,.7,.2,.2,.1,.1,.1,.1,.2,.2,.3,.3,.5,.5][i], .25, 2. if i < 4 else 3.)
        for i in range(16))
    return JointInput(slots=slots,capacity_kwh=10.,initial_kwh=2.,reserve_kwh=2.,maximum_kwh=9.,
        pv_origin_kwh=0.,charge_kw=4.,discharge_kw=3.,ac_kw=5.,export_kw=5.,
        allow_buy=False,allow_sell=True,allow_delay=True)


def trial(data=None, **kw):
    data = data or case()
    baseline, _ = simulate(data, (0,)*len(data.slots))
    points, plan = optimize_delay(data, baseline, **kw)
    return data,baseline,points,plan


class DelayTests(unittest.TestCase):
    def test_revalidation_keeps_exact_window_and_lower_forecast_proof(self):
        data=case();plan=optimize(data)
        self.assertIsNotNone(plan.delay_plan)
        first=data.slots[0];ratio=1797/1800
        fresh=replace(data,slots=(replace(first,start=first.start+timedelta(seconds=3),
            load_kwh=first.load_kwh*ratio,pv_kwh=first.pv_kwh*ratio),*data.slots[1:]))
        checked=revalidate_plan(fresh,plan,captured=data)
        self.assertIsNotNone(checked)
        self.assertEqual(checked.delay_plan.end,plan.delay_plan.end)
        self.assertLessEqual(checked.delay_plan.recovered_at,plan.delay_plan.recovered_at)
        self.assertEqual(sum(p.grid_charge_kwh+p.battery_export_kwh for p in checked.slots),0.)
        self.assertIsNone(revalidate_plan(replace(fresh,export_kw=0.),plan,captured=data))
        self.assertIsNone(revalidate_plan(replace(fresh,sale_pv_kwh=(.01,)*len(fresh.slots)),plan,captured=data))

    def test_real_gain_and_equal_energy_and_origin(self):
        data,baseline,points,plan=trial()
        self.assertIsNotNone(plan)
        self.assertGreater(plan.benefit_pln, 1.)
        self.assertLessEqual(plan.evaluated_windows, 1176)
        self.assertAlmostEqual(sum(p.battery_in_kwh for p in points),sum(p.battery_in_kwh for p in baseline))
        self.assertEqual(points[-1],baseline[-1])
        self.assertAlmostEqual(plan.benefit_pln,sum((a.grid_export_kwh-b.grid_export_kwh)*a.net for a,b in zip(points,baseline)))
        for p,b in zip(points,baseline):
            self.assertEqual(p.grid_import_kwh,b.grid_import_kwh)
            self.assertEqual(p.battery_out_kwh,b.battery_out_kwh)
            self.assertGreaterEqual(p.end_kwh,data.reserve_kwh-1e-7)
            self.assertLessEqual(p.end_kwh,data.maximum_kwh+1e-7)
            self.assertAlmostEqual(p.start_kwh+p.battery_in_kwh-p.battery_out_kwh,p.end_kwh)
            if p.action=='pv_charge_hold':
                self.assertEqual(p.battery_in_kwh,0.)
                self.assertEqual(p.battery_out_kwh,0.)
        for a,b in zip(points,points[1:]):
            self.assertAlmostEqual(a.end_kwh,b.start_kwh)

    def test_disabled_is_identical(self):
        _,before,after,plan=trial(replace(case(),allow_delay=False))
        self.assertIsNone(plan); self.assertIs(after,before)

    def test_no_sell_permission(self):
        self.assertIsNone(trial(replace(case(),allow_sell=False))[-1])

    def test_zero_export_or_zero_charge_disables(self):
        for kw in ({'export_kw':0.},{'charge_dc_kw':0.},{'pv_charge_kw':0.}):
            self.assertIsNone(trial(replace(case(),**kw))[-1])

    def test_flat_and_negative_prices(self):
        for price in (.2, 0., -.5):
            data=replace(case(),slots=tuple(replace(s,net=price) for s in case().slots))
            self.assertIsNone(trial(data)[-1])

    def test_sale_block_is_respected(self):
        data=replace(case(),slots=tuple(replace(s,sale_blocked=True) for s in case().slots))
        self.assertIsNone(trial(data)[-1])

    def test_weak_pv_cannot_reach_full_target(self):
        data=replace(case(),slots=tuple(replace(s,pv_kwh=.3) for s in case().slots))
        self.assertIsNone(trial(data)[-1])

    def test_less_charge_power_shortens_delay(self):
        full=trial()[-1]
        slow=trial(replace(case(),charge_kw=2.))[-1]
        self.assertIsNotNone(slow)
        self.assertLessEqual(slow.end,full.end)

    def test_actual_stock_and_soc_headroom(self):
        data, baseline, points, plan = trial(replace(case(), initial_kwh=1.9))
        self.assertIsNotNone(plan)
        self.assertTrue(all(p.end_kwh >= data.initial_kwh-1e-8 for p in points))
        self.assertEqual(points[-1], baseline[-1])
        self.assertIsNone(trial(replace(case(), initial_kwh=10., maximum_kwh=10.))[-1])

    def test_fixed_action_is_never_crossed(self):
        data,before,_,_=trial()
        before=tuple(replace(p,action='buy') if i==4 else p for i,p in enumerate(before))
        points,plan=optimize_delay(data,before)
        if plan:
            self.assertTrue(plan.recovered_at<=before[4].start or plan.start>=before[4].end)
        self.assertEqual(points[4],before[4])

    def test_no_new_start_from_1400(self):
        data=case()
        data=replace(data,slots=tuple(replace(s,start=s.start+timedelta(hours=6),end=s.end+timedelta(hours=6)) for s in data.slots))
        self.assertIsNone(trial(data)[-1])

    def test_integer_soc_target(self):
        for soc,target in ((12,13),(12.1,14),(98.9,100),(99,100)):
            self.assertEqual(hold_soc_target(soc),target)
        for soc in (99.1,100,-1,True,float('nan'),float('inf'),None):
            with self.assertRaises(ValueError): hold_soc_target(soc)

    def test_user_enable_applies_delay_to_executable_pstryk(self):
        data=case()
        on,off=optimize(data),optimize(replace(data,allow_delay=False))
        self.assertIsNotNone(on.delay_plan)
        self.assertTrue(any(p.action=='pv_charge_hold' for p in on.slots))
        self.assertFalse(any(p.action=='pv_charge_hold' for p in off.slots))
        self.assertGreater(on.benefit_pln,off.benefit_pln)
        self.assertAlmostEqual(off.cost_pln-on.cost_pln,on.delay_plan.benefit_pln)
        self.assertGreaterEqual(on.slots[-1].end_kwh,off.slots[-1].end_kwh-1e-8)
        for at, ready in ((on.delay_plan.start-timedelta(seconds=1),False),
                          (on.delay_plan.start,True),(on.delay_plan.end,False)):
            self.assertEqual(attributes(on.delay_plan,enabled=True,now=at)['pv_charge_delay_execution_ready'],ready)
        self.assertFalse(attributes(on.delay_plan,enabled=False,now=on.delay_plan.start)['pv_charge_delay_execution_ready'])



class PublicationTests(unittest.TestCase):
    def test_small_jitter_is_suppressed_but_window_and_readiness_are_immediate(self):
        from pv_charge_delay import stabilize_attributes
        plan=trial()[-1]
        first=attributes(plan,enabled=True,now=NOW)
        small=attributes(replace(plan,benefit_pln=plan.benefit_pln+.02,
            deferred_kwh=plan.deferred_kwh+.02),enabled=True,now=NOW)
        self.assertEqual(stabilize_attributes(small,first),first)
        moved=attributes(replace(plan,end=plan.end+timedelta(minutes=30)),enabled=True,now=NOW)
        self.assertNotEqual(stabilize_attributes(moved,first),first)
        disabled=attributes(plan,enabled=False,now=plan.start)
        self.assertFalse(disabled['pv_charge_delay_current'])
        self.assertFalse(disabled['pv_charge_delay_execution_ready'])
        self.assertEqual(stabilize_attributes(disabled,first),disabled)

if __name__=='__main__': unittest.main()
