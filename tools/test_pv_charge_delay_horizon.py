"""Tomorrow/midnight deferral, fixed selections and SOC-dependent refill."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import sys, unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'custom_components/hoymiles_hit_modbus'))
from pstryk_joint import JointInput, EnergySlot, simulate, optimize, revalidate_plan, pv_charge_budget
from pv_charge_delay import optimize_delay, attributes, qualified_refill_dates
from charge_forecast import learn_curve, charge_budget, forecast_curve

Z=ZoneInfo('Europe/Warsaw')

def case(start=datetime(2026,10,1,16,tzinfo=Z),days=2):
    end=start.replace(hour=0)+timedelta(days=days)
    at=start.astimezone(timezone.utc); rows=[]
    while at<end.astimezone(timezone.utc):
        h=at.astimezone(Z).hour
        rows.append(EnergySlot(at,at+timedelta(minutes=30),.9 if h<11 else .15,
            .12,2. if 8<=h<11 else 3. if 11<=h<17 else 0.))
        at+=timedelta(minutes=30)
    return JointInput(tuple(rows),10.,10.,1.,10.,10.,4.,3.,5.,5.,
        wear_pln_kwh=10.,allow_buy=False,allow_sell=True,allow_delay=True)

class HorizonTests(unittest.TestCase):
    def test_afternoon_plans_tomorrow_even_when_full_now(self):
        data=case();plan=optimize(data)
        self.assertIsNotNone(plan.delay_plan)
        self.assertEqual(plan.delay_plan.start.astimezone(Z).date().isoformat(),'2026-10-02')
        self.assertFalse(attributes(plan.delay_plan,enabled=True,now=data.slots[0].start)['pv_charge_delay_execution_ready'])
        self.assertIsNotNone(revalidate_plan(data,plan,captured=data))

    def test_two_days_are_independent_and_do_not_spend_overnight_stock(self):
        data=replace(case(datetime(2026,10,1,8,tzinfo=Z)),initial_kwh=2.,pv_origin_kwh=2.)
        before,_=simulate(data,(0,)*len(data.slots));after,plan=optimize_delay(data,before)
        self.assertEqual(len(plan.following),1)
        self.assertEqual(after[-1],before[-1])
        for a,b in zip(after,after[1:]):self.assertAlmostEqual(a.end_kwh,b.start_kwh)
        for a,b in zip(before,after):
            self.assertAlmostEqual(a.grid_import_kwh,b.grid_import_kwh)
            if a.start.astimezone(Z).hour==0:self.assertEqual(a,b)
        joint=optimize(data)
        checked=revalidate_plan(data,joint,captured=data)
        self.assertIsNotNone(checked)
        self.assertEqual(len(checked.delay_plan.following),1)

    def test_tomorrow_needs_its_own_qualified_forecast(self):
        data=replace(case(),zero_pv_sale_guard=True,delay_refill_qualified=True,
            delay_qualified_dates=('2026-10-01',))
        self.assertIsNone(optimize(data).delay_plan)
        data=replace(data,delay_qualified_dates=('2026-10-02',))
        self.assertIsNotNone(optimize(data).delay_plan)
        meta={'forecast_today_kwh':30.,'forecast_today_p10_kwh':20.,'forecast_today_data_fresh':True,
            'forecast_tomorrow_kwh':30.,'forecast_tomorrow_p10_kwh':20.,'forecast_tomorrow_data_fresh':False}
        self.assertEqual(qualified_refill_dates(meta,data.slots[0].start),('2026-10-01',))

    def test_dst_and_zero_export(self):
        data=case(datetime(2026,10,24,16,tzinfo=Z))
        self.assertIsNotNone(optimize(data).delay_plan)
        self.assertIsNone(optimize(replace(data,export_kw=0.)).delay_plan)

    def test_missing_tomorrow_prices_never_becomes_zero_prices(self):
        data=case();data=replace(data,slots=data.slots[:16])
        self.assertIsNone(optimize(data).delay_plan)

    def test_revalidation_losing_one_days_forecast_rejects_whole_selection(self):
        data=replace(case(datetime(2026,10,1,8,tzinfo=Z)),initial_kwh=2.,pv_origin_kwh=2.)
        plan=optimize(data)
        self.assertEqual(len(plan.delay_plan.following),1)
        self.assertIsNone(revalidate_plan(replace(data,delay_qualified_dates=('2026-10-01',)),plan,captured=data))

    def test_fixed_window_can_cross_1400_without_becoming_a_new_search(self):
        data=replace(case(datetime(2026,10,1,14,tzinfo=Z),days=1),initial_kwh=7.,pv_origin_kwh=7.)
        data=replace(data,slots=tuple(replace(s,net=.9 if s.start.astimezone(Z).hour==14 else .1) for s in data.slots))
        points,_=simulate(data,(0,)*len(data.slots))
        self.assertIsNone(optimize_delay(data,points)[1])
        end=data.slots[0].end
        accepted_start=data.slots[0].start-timedelta(minutes=1)
        _,checked=optimize_delay(data,points,_windows=((accepted_start,end),))
        self.assertIsNotNone(checked)
        self.assertEqual(checked.end,end)

    def test_current_zero_and_future_capability_are_separate(self):
        data=case();data=replace(data,initial_kwh=5.,charge_dc_kw=0.,pv_charge_kw=0.,
            forecast_charge_curve_kw=(4.,2.,.1),forecast_charge_after=data.slots[0].end)
        self.assertEqual(pv_charge_budget(data,data.slots[0],5.,3.),0.)
        self.assertGreater(pv_charge_budget(data,data.slots[1],5.,3.),0.)
        self.assertLess(pv_charge_budget(data,data.slots[1],9.85,3.),.15)

class CurveTests(unittest.TestCase):
    def test_integrates_taper_instead_of_filling_at_low_soc_power(self):
        self.assertAlmostEqual(charge_budget(89.,100.,100.,1.,10.,(10.,5.,1.)),5.5)
        self.assertAlmostEqual(charge_budget(99.,100.,100.,.5,10.,(10.,5.,1.)),.5)
        self.assertEqual(charge_budget(50.,100.,100.,.5,0.,(10.,5.,1.)),0.)

    def test_bounded_learning_and_low_soc_derating(self):
        rows=[]
        for day in range(2):
            for soc,amps in ((50.,150.),(94.,100.),(99.,30.)):
                rows.extend((1790286900.+day*86400+i*300,soc,amps,50.) for i in range(10))
        curve=learn_curve(rows)
        self.assertEqual(curve,(7.12,4.75,1.42))
        self.assertEqual(forecast_curve(curve,soc=99.,live_kw=0.),curve)
        self.assertIsNone(forecast_curve(curve,soc=50.,live_kw=0.))
        self.assertEqual(forecast_curve(curve,soc=50.,live_kw=1.),(1.,1.,1.))
        self.assertIsNone(learn_curve(rows[:10]))
        self.assertIsNone(learn_curve(rows*40))

if __name__=='__main__':unittest.main()
