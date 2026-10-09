"""Regression: Pstryk uses RCE sale selection with one protected BUY budget."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'custom_components/hoymiles_hit_modbus'))
from pstryk_joint import EnergySlot, JointInput, optimize, revalidate_plan, simulate

NOW=datetime(2026,10,1,16,tzinfo=timezone.utc)

def scenario(prices=(.4,1.,.2,.2), loads=(.2,.2,.2,.2), pv=(0.,0.,0.,0.)):
    return JointInput(tuple(EnergySlot(NOW+timedelta(minutes=30*i),
        NOW+timedelta(minutes=30*(i+1)),price,loads[i],pv[i]) for i,price in enumerate(prices)),
        10.,9.,2.,10.,9.,5.,5.,5.,5.,power_step_kw=.05)

class ParityTests(unittest.TestCase):
    def test_free_stock_can_be_sold_without_recreating_self_use_terminal_stock(self):
        data=scenario()
        plan=optimize(data)
        self.assertGreater(sum(p.battery_export_kwh for p in plan.slots),3.)
        self.assertLess(plan.slots[-1].end_kwh,plan.baseline_end_kwh-1.)
        self.assertGreaterEqual(plan.slots[-1].end_kwh,data.reserve_kwh-1e-8)
        self.assertFalse(any(p.action=='buy' for p in plan.slots))

    def test_fixed_sale_revalidation_preserves_lower_but_protected_terminal_stock(self):
        data=scenario()
        plan=optimize(data)
        self.assertTrue(any(p.action=='sell' for p in plan.slots))
        checked=revalidate_plan(data,plan,captured=data)
        self.assertIsNotNone(checked)
        self.assertTrue(any(p.action=='sell' for p in checked.slots))

    def test_sale_cannot_add_any_grid_import_to_buy_only_plan(self):
        for stock in (2.,4.,9.):
            for prices in ((.01,.01,2.,2.),(-1.,-1.,2.,2.)):
                data=replace(scenario(prices=prices,loads=(.5,.5,.5,.5)),
                    initial_kwh=stock,pv_origin_kwh=stock)
                buy_only=optimize(replace(data,allow_sell=False))
                combined=optimize(data)
                self.assertLessEqual(sum(p.grid_import_kwh for p in combined.slots),
                    sum(p.grid_import_kwh for p in buy_only.slots)+1e-8)

    def test_lower_future_pv_can_reduce_export_without_invalidating_safe_commands(self):
        # installation_3 replay: the same command supplies more of the home when PV
        # is lower. This changes the export share, not the home/import safety.
        data=replace(scenario(prices=(1.,1.,.2,.2),loads=(.1,.5,.5,.5),pv=(1.,.4,0.,0.)),
            initial_kwh=7.,pv_origin_kwh=7.,sale_pv_kwh=(1.,.3,0.,0.),minimum_export_kw=2.)
        plan=optimize(data)
        risk=replace(data,slots=tuple(replace(s,pv_kwh=pv)
            for s,pv in zip(data.slots,data.sale_pv_kwh)),sale_pv_kwh=None)
        actions=tuple(-2 if p.action=='sell' else 0 for p in plan.slots)
        risk_points,_=simulate(risk,actions,fixed_points=plan.slots)
        self.assertTrue(any(r.battery_export_kwh<p.battery_export_kwh-1e-8
            for r,p in zip(risk_points,plan.slots)))
        self.assertTrue(all(p.grid_import_kwh<1e-8 for p in risk_points))
        self.assertIsNotNone(revalidate_plan(data,plan,captured=data))

    def test_expensive_evening_is_preferred_to_cheap_earlier_slots(self):
        plan=optimize(scenario(prices=(.01,.01,1.,1.)))
        sold=[p for p in plan.slots if p.action=='sell']
        self.assertTrue(sold)
        self.assertTrue(all(p.net==1. for p in sold))

    def test_zero_export_and_disabled_sale_stay_closed(self):
        for data in (replace(scenario(),export_kw=0.),replace(scenario(),allow_sell=False)):
            self.assertFalse(any(p.action=='sell' for p in optimize(data).slots))

    def test_no_sale_reason_matches_the_binding_constraint(self):
        cases={
            'sale_disabled':replace(scenario(),allow_sell=False),
            'export_blocked':replace(scenario(),export_kw=0.),
            'discharge_power_unavailable':replace(scenario(),discharge_kw=0.),
            'home_energy_needed':replace(scenario(),initial_kwh=2.,pv_origin_kwh=2.,allow_buy=False),
            'sale_price_too_low':scenario(prices=(.01,)*4),
            'sale_below_minimum_profit':replace(scenario(),minimum_benefit_pln=100.),
            'sale_below_minimum_power':replace(scenario(),minimum_export_kw=10.),
            'sale_hours_blocked':replace(scenario(),slots=tuple(replace(s,sale_blocked=True) for s in scenario().slots)),
            'pv_uses_inverter_power':scenario(pv=(3.,)*4),
        }
        for reason,data in cases.items():
            with self.subTest(reason=reason):
                plan=optimize(data)
                self.assertFalse(any(p.action=='sell' for p in plan.slots))
                self.assertEqual(plan.sale_reason,reason)

    def test_revalidation_cannot_finance_sale_with_future_home_import(self):
        data=scenario(prices=(2.,.2,.2,.2),loads=(0.,.1,.1,.1))
        plan=optimize(data)
        # The remaining measured demand may change while the worker runs.
        fresh=replace(data,slots=tuple(replace(s,load_kwh=2.) for s in data.slots))
        checked=revalidate_plan(fresh,plan,captured=data)
        if checked is not None:
            home=optimize(replace(fresh,allow_sell=False))
            self.assertLessEqual(sum(p.grid_import_kwh for p in checked.slots),
                sum(p.grid_import_kwh for p in home.slots)+1e-8)

if __name__=='__main__': unittest.main()
