"""Offline regressions: one Mode-5 balance and qualified whole-slot deferral."""
from dataclasses import replace
from datetime import timedelta
import asyncio  # Load stdlib select before the standalone component fixture.
import math
import unittest

from test_sale_tail_options import Case, START, settings, evaluate, R, P


class SaleConsolidationTests(unittest.TestCase):
    def pack(self, *, tail=1.28, later=1.60, prices=(.70,.10,.80),
             pv=0., initial=180., export_cap=40., allow_transfer=True,
             callback=None, engine='rce', load=.96):
        case=Case('whole_slot',tail=tail,later=later,load=load,later_load=load,
                  pv=pv,initial=initial,export=export_cap,later_export=export_cap)
        starts=[START+timedelta(minutes=30*i) for i in range(3)]
        price=dict(zip(starts,prices))
        loads={s:load*.5 for s in starts}; pvs={s:pv*.5 if i==0 else 0. for i,s in enumerate(starts)}
        cfg=settings(case,START)
        original={starts[0]:tail,starts[2]:later}
        self.calls=0

        def trajectory(plan):
            self.calls+=1
            if callback is not None:
                return callback(plan,original)
            if engine=='pstryk':
                data=P.JointInput(tuple(P.EnergySlot(s,s+timedelta(minutes=30),price[s],loads[s],pvs[s]) for s in starts),
                    230.,initial,46.,230.,initial-46.,32.,32.,40.,export_cap,
                    power_step_kw=.32,minimum_export_kw=2.,allow_buy=False,
                    sale_reserve_kwh=(46.,)*3)
                points,cost=P.simulate(data,tuple(-2 if s in plan else 0 for s in starts),
                                      exports=plan,continuous_exports=True)
                valid=all(p.battery_export_kwh+1e-7>=plan.get(p.start,0.)
                          and p.grid_import_kwh<1e-7 and p.end_kwh>=46.-1e-7 for p in points)
                return valid,-cost+points[-1].end_kwh*.2
            energy=initial;cost=0.;valid=True
            for s in starts:
                v=R._simulate_physical_slot(cfg,battery_kwh_dc=energy,
                    load_kwh_ac=loads[s],pv_kwh_ac=pvs[s],controlled_export_kwh_ac=plan.get(s,0.),
                    hard_floor_kwh_dc=46.,export_floor_kwh_dc=46.,slot_fraction=1.)
                valid &= v.feasible and not v.home_energy_shortage
                cost+=(v.grid_import_kwh_ac-v.controlled_export_kwh_ac-v.natural_export_kwh_ac)*price[s]
                cost+=(v.delivered_to_load_kwh_ac+v.controlled_export_kwh_ac)/.95*.08
                energy=v.battery_after_kwh_dc
            return valid,-cost+energy*.2

        packed=R._pack_executable_exports(original,settings=cfg,starts=starts,
            load_by_slot=loads,pv_by_slot=pvs,slot_fractions={s:1. for s in starts},
            price_by_start=price,feasible=lambda p:trajectory(p)[0],
            objective=lambda p:trajectory(p)[1],allow_transfer=allow_transfer)
        return packed,original,trajectory

    def test_later_selected_window_absorbs_isolated_tail_in_both_balances(self):
        for engine in ('rce','pstryk'):
            with self.subTest(engine=engine):
                got,old,value=self.pack(engine=engine)
                self.assertEqual(len(got),1)
                self.assertAlmostEqual(sum(got.values()),sum(old.values()))
                self.assertAlmostEqual(got[START+timedelta(hours=1)],2.88)
                self.assertGreater(value(got)[1],value(old)[1])

    def test_unexecutable_tail_is_transferred_before_it_is_discarded(self):
        got,old,value=self.pack(tail=.32)
        self.assertEqual(len(got),1)
        self.assertAlmostEqual(sum(got.values()),sum(old.values()))
        self.assertGreaterEqual(value(got)[1],value(old)[1]-1e-8)

    def test_equal_price_reduces_starts_without_inventing_start_cost(self):
        got,old,value=self.pack(prices=(.7,.1,.7))
        self.assertEqual(len(got),1)
        self.assertAlmostEqual(value(got)[1],value(old)[1])

    def test_fixed_revalidation_never_retimes_accepted_energy(self):
        got,old,_=self.pack(allow_transfer=False)
        self.assertEqual(got,old)

    def test_lower_price_or_full_destination_keeps_valid_original(self):
        for kwargs in ({'prices':(.9,.1,.8)},{'export_cap':3.5}):
            got,old,_=self.pack(**kwargs)
            self.assertEqual(got,old)

    def test_whole_horizon_cost_and_safety_are_vetoes(self):
        def unsafe(plan,old):return (len(plan)>1,sum(plan.values()))
        def worse(plan,old):return (True,10. if len(plan)>1 else 9.)
        def unknown(plan,old):return (True,10. if len(plan)>1 else math.nan)
        for guard in (unsafe,worse,unknown):
            got,old,_=self.pack(callback=guard)
            self.assertEqual(got,old)

    def test_mode5_does_not_count_pv_charge_in_same_interval(self):
        c=Case('pv',initial=229.,pv=8.)
        cfg=settings(c,START)
        v=R._simulate_physical_slot(cfg,battery_kwh_dc=c.initial,
            load_kwh_ac=c.load*.5,pv_kwh_ac=4.,controlled_export_kwh_ac=1.6,
            hard_floor_kwh_dc=46.,export_floor_kwh_dc=46.,slot_fraction=1.)
        self.assertTrue(v.feasible)
        self.assertEqual(v.charge_input_kwh_ac,0.)
        self.assertAlmostEqual(v.battery_after_kwh_dc,229.-1.6/.95)

    def test_pv_subinterval_trajectories_agree_with_explicit_sell(self):
        for kind in ('shorter','defer','keep_later_only'):
            a=evaluate(Case('pv',initial=229.,pv=8.),kind,'rce')
            b=evaluate(Case('pv',initial=229.,pv=8.),kind,'pstryk')
            self.assertTrue(a['valid'] and b['valid'])
            for key in ('end_kwh','cost_pln','grid_import_kwh','export_kwh'):
                self.assertAlmostEqual(a[key],b[key],places=6,msg=(kind,key))

    def test_self_use_still_charges_pv(self):
        cfg=settings(Case('self',initial=150.,pv=8.),START)
        v=R._simulate_physical_slot(cfg,battery_kwh_dc=150.,load_kwh_ac=.5,
            pv_kwh_ac=4.,controlled_export_kwh_ac=0.,hard_floor_kwh_dc=46.,
            export_floor_kwh_dc=46.,slot_fraction=1.)
        self.assertTrue(v.feasible)
        self.assertAlmostEqual(v.charge_input_kwh_ac,3.5)
        self.assertAlmostEqual(v.battery_after_kwh_dc,150.+3.5*.95)

    def test_register_rounding_cannot_hide_value_loss(self):
        def value(plan, old):
            # Continuous retiming ties, but the rounded one-slot trajectory
            # loses value. The old two-slot rounded trajectory remains valid.
            worse = len(plan)==1 and sum(plan.values()) < sum(old.values())-1e-7
            return True, 9. if worse else 10.
        got, old, _ = self.pack(tail=1.30,callback=value)
        self.assertEqual(got,old)

    def test_transfer_search_has_a_fixed_work_budget(self):
        stamps=[START+timedelta(hours=i) for i in range(24)]
        plan={s:2.2 for s in stamps}
        calls=[]
        got=R._consolidate_sale_tails(plan,settings=settings(Case('bound'),START),
            starts=stamps,load_by_slot={},pv_by_slot={},slot_fractions={},
            price_by_start={s:float(i) for i,s in enumerate(stamps)},
            feasible=lambda p:calls.append(dict(p)) or False,objective=lambda p:10.)
        self.assertEqual(plan,got)
        self.assertEqual(len(calls),32)

    def test_complete_optimizers_reduce_two_starts_without_value_loss(self):
        from test_rce_optimizer import base_input, RCE
        start=START.replace(hour=16,minute=0)
        market=[RCE.PriceSlot(start,.7),RCE.PriceSlot(start+timedelta(minutes=30),.1,True),
                RCE.PriceSlot(start+timedelta(hours=1),.7)]
        cfg=base_input(now=start+timedelta(minutes=15),price_slots=market,
            battery_capacity_kwh=10.,battery_soc_percent=40.,dynamic_reserve_enabled=False,
            outage_reserve_soc_percent=20.,manual_minimum_soc_percent=20.,
            average_daily_load_kwh=0.,average_night_load_kwh=0.,pv_by_slot_kwh={},
            conservative_pv_by_slot_kwh={},inverter_power_kw=5.,inverter_count=1,
            current_load_power_kw=0.,current_pv_power_kw=0.)
        original=RCE._consolidate_sale_tails
        RCE._consolidate_sale_tails=lambda plan,**kwargs:plan
        try:before=RCE.optimize_rce(cfg)
        finally:RCE._consolidate_sale_tails=original
        after=RCE.optimize_rce(cfg)
        self.assertEqual(len(before.planned_exports),2)
        self.assertEqual(len(after.planned_exports),1)
        self.assertEqual(after.planned_exports[0].start,market[-1].start)
        self.assertGreaterEqual(after.net_objective_pln+1e-10,before.net_objective_pln)
        data=P.JointInput(tuple(P.EnergySlot(p.start if i else cfg.now,
            p.start+timedelta(minutes=30),p.price_pln_kwh,0.,0.,p.blocked)
            for i,p in enumerate(market)),10.,4.,2.,10.,2.,5.,5.,5.,5.,
            allow_buy=False,power_step_kw=.05,minimum_export_kw=.2)
        namespace=P._pack_executable_exports.__globals__
        original=namespace['_consolidate_sale_tails']
        namespace['_consolidate_sale_tails']=lambda plan,**kwargs:plan
        try:before=P.optimize(data)
        finally:namespace['_consolidate_sale_tails']=original
        after=P.optimize(data)
        self.assertEqual(sum(p.action=='sell' for p in before.slots),2)
        self.assertEqual(sum(p.action=='sell' for p in after.slots),1)
        self.assertEqual(after.slots[-1].action,'sell')
        self.assertLessEqual(after.cost_pln,before.cost_pln+1e-10)


if __name__=='__main__':unittest.main()
