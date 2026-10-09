"""RCE/Pstryk adapters and UI helper on the isolated HA runtime."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys, unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from custom_components.hoymiles_hit_modbus.rce_optimizer import OptimizerInput, PriceSlot, optimize_rce
from custom_components.hoymiles_hit_modbus.pv_charge_delay_adapter import rce_delay, update_rce_attributes, project_delay_trace
from custom_components.hoymiles_hit_modbus.pstryk_plan import build_joint_input,projections,input_key
from custom_components.hoymiles_hit_modbus.pstryk_joint import optimize

NOW=datetime(2026,9,29,6,tzinfo=timezone.utc)

def settings():
    prices=[PriceSlot(NOW+timedelta(minutes=30*i),.9 if i<4 else .1) for i in range(16)]
    pv={p.start:2. if i<4 else 3. for i,p in enumerate(prices)}
    return OptimizerInput(now=NOW,price_slots=prices,pv_by_slot_kwh=pv,battery_capacity_kwh=10.,
        battery_soc_percent=20.,outage_reserve_soc_percent=20.,safety_margin_soc_percent=0.,
        manual_minimum_soc_percent=20.,dynamic_reserve_enabled=True,average_daily_load_kwh=12.,
        average_night_load_kwh=4.,night_start_minute=1200,night_end_minute=360,
        inverter_power_kw=5.,inverter_ac_power_kw=5.,inverter_count=1,
        discharge_power_percent=60.,export_efficiency_percent=95.,bms_max_charge_current_a=100.,
        bms_max_discharge_current_a=100.,battery_voltage_v=51.2,bms_charge_data_fresh=True,
        bms_discharge_data_fresh=True,bms_charge_data_available=True,bms_discharge_data_available=True,
        conservative_pv_by_slot_kwh=pv,export_power_cap_kw=5.,current_load_power_kw=.5,
        current_pv_power_kw=4.,current_battery_soc_fresh=True)

def result(**kw): return SimpleNamespace(planned_exports=[],current_slot_planned_export_kwh=0.,**kw)

class AdapterTests(unittest.TestCase):
    def test_native_rce_sale_does_not_disable_profitable_morning_delay(self):
        from custom_components.hoymiles_hit_modbus.rce_optimizer import _slot_export_limit_kwh
        prices=[PriceSlot(NOW+timedelta(minutes=30*i),.9 if i<4 else .1) for i in range(20)]
        pv={p.start:3. if i<15 else 0. for i,p in enumerate(prices)}
        s=replace(settings(),price_slots=prices,pv_by_slot_kwh=pv,conservative_pv_by_slot_kwh=pv,
            battery_capacity_kwh=20.,battery_soc_percent=47.,inverter_power_kw=10.,
            inverter_ac_power_kw=10.,discharge_power_percent=80.,export_efficiency_percent=80.,
            bms_max_charge_current_a=200.,bms_max_discharge_current_a=200.,battery_voltage_v=52.4,
            export_power_cap_kw=10.)
        end=prices[-1].start
        # 8 kWh of daytime LOAD across fourteen hours, in this half hour.
        amount=_slot_export_limit_kwh(s,8./14./2.,0.,1.,slot_start=end)
        sale=SimpleNamespace(start=end,energy_kwh=amount)
        r=result();r.planned_exports=[sale]
        plan=rce_delay(s,r,enabled=True)
        self.assertIsNotNone(plan,'Native qualified sale must not be capped/quantized again by Pstryk replay')
        self.assertLessEqual(plan.recovered_at,end)
        self.assertGreater(plan.benefit_pln,.05)
        self.assertEqual(r.planned_exports,[sale])
        self.assertEqual(sale.energy_kwh,amount)
        for changes in ({'bms_max_discharge_current_a':100.}, {'export_power_cap_kw':1.}):
            with self.subTest(changes=changes):
                self.assertIsNone(rce_delay(replace(s,**changes),r,enabled=True),
                    'A fixed sale exceeding native current bounds must still block delay')
        with self.assertRaisesRegex(ValueError,'physical_input_stale'):
            rce_delay(replace(s,bms_discharge_data_fresh=False),r,enabled=True)

    def test_rce_tomorrow_curve_and_display_carry_overnight_energy(self):
        from zoneinfo import ZoneInfo
        from custom_components.hoymiles_hit_modbus.rce_optimizer import _immutable_input_changes
        zone=ZoneInfo('Europe/Warsaw')
        now=NOW.astimezone(zone).replace(hour=16)
        prices=[PriceSlot(now+timedelta(minutes=30*i),.9 if (now+timedelta(minutes=30*i)).hour<11 else .1) for i in range(64)]
        pv={p.start:3. if 8<=p.start.hour<17 else 0. for p in prices}
        s=replace(settings(),now=now,price_slots=prices,pv_by_slot_kwh=pv,conservative_pv_by_slot_kwh=pv,
            battery_soc_percent=100.,average_daily_load_kwh=5.,average_night_load_kwh=2.,
            forecast_charge_curve_kw=(4.,2.,.5),battery_wear_cost_pln_kwh=10.)
        original=optimize_rce(s)
        self.assertFalse(original.planned_exports)
        plan=rce_delay(s,original,enabled=True)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.start.astimezone(zone).date(),(now+timedelta(days=1)).date())
        projected=project_delay_trace(original.timeline_trace,plan,s)
        holds=[p for p in projected.points if p.action_code=='pv_charge_hold']
        self.assertTrue(holds)
        self.assertTrue(all(abs(p.battery_delta_kwh)<1e-8 for p in holds))
        self.assertAlmostEqual(projected.points[-1].soc_percent,original.timeline_trace.points[-1].soc_percent)
        self.assertEqual(_immutable_input_changes(s,replace(s,forecast_charge_curve_kw=(3.9,2.,.5))),[])

    def test_rce_trace_publishes_hold_and_recovery_without_mutating_optimizer(self):
        # Isolate the delay projection from native battery SELL. With Mode 5
        # suppressing refill, 10 PLN/kWh can still fund a tiny sale through
        # the accompanying high-price PV export. This fixture must have none.
        s=replace(settings(),battery_wear_cost_pln_kwh=100.)
        original=optimize_rce(s)
        self.assertFalse(original.planned_exports)
        source=original.timeline_trace
        plan=rce_delay(s,original,enabled=True)
        self.assertIsNotNone(plan)
        projected=project_delay_trace(source,plan,s)
        self.assertIsNot(projected,source)
        self.assertFalse(any(p.selected for p in source.points))
        holds=[p for p in projected.points if p.action_code=='pv_charge_hold']
        self.assertTrue(holds)
        for p in holds:
            self.assertTrue(p.selected)
            self.assertAlmostEqual(p.battery_delta_kwh,0.)
            self.assertEqual(p.policy.planned_export_kwh,0.)
        self.assertAlmostEqual(projected.points[-1].soc_percent,source.points[-1].soc_percent)
        previous=s.battery_soc_percent
        for p in projected.points:
            self.assertAlmostEqual((p.soc_percent-previous)*s.battery_capacity_kwh/100,p.battery_delta_kwh)
            previous=p.soc_percent

    def test_today_refill_is_independent_of_tomorrow_battery_sale_guard(self):
        from custom_components.hoymiles_hit_modbus.pv_charge_delay import qualified_today_refill
        metadata={'forecast_today_data_fresh':True,'forecast_today_kwh':52.99,
                  'forecast_today_p10_kwh':37.02}
        self.assertTrue(qualified_today_refill(metadata))
        for broken in ({}, {**metadata,'forecast_today_data_fresh':False},
                {**metadata,'forecast_today_p10_kwh':None},
                {**metadata,'forecast_today_p10_kwh':0},
                {**metadata,'forecast_today_p10_kwh':60},
                {**metadata,'forecast_today_p10_kwh':float('nan')}):
            self.assertFalse(qualified_today_refill(broken))
        s=replace(settings(),critical_zero_pv_guard=True,critical_zero_pv_guard_reason='p10_high_risk')
        self.assertIsNone(rce_delay(s,result(),enabled=True))
        rce=rce_delay(s,result(),enabled=True,refill_qualified=True)
        self.assertIsNotNone(rce)
        options={'maximum_soc':100.,'charge_power_percent':100.,'charge_efficiency':95.,
            'minimum_saving':.01,'demand_margin_percent':0.,'allow_buy':False,'allow_sell':True,
            'allow_delay':True,'delay_refill_qualified':True}
        data=build_joint_input(s,options,pv_origin_kwh=0.)
        self.assertTrue(data.zero_pv_sale_guard)
        plan=optimize(data)
        self.assertIsNotNone(plan.delay_plan)
        self.assertEqual(sum(p.battery_export_kwh+p.grid_charge_kwh for p in plan.slots),0.)
        lower=replace(data,delay_pv_kwh=tuple(.05 for p in data.slots))
        self.assertIsNone(optimize(lower).delay_plan,'Same window must refill on the lower forecast')
        self.assertIsNone(optimize(replace(data,export_kw=0.)).delay_plan)

    def test_rce_and_off(self):
        self.assertIsNone(rce_delay(settings(),result(),enabled=False))
        plan=rce_delay(settings(),result(),enabled=True)
        self.assertIsNotNone(plan)
        self.assertGreater(plan.benefit_pln,.05)

    def test_zero_risk_and_zero_export(self):
        for kw in ({'critical_zero_pv_guard':True},{'export_power_cap_kw':0.}):
            self.assertIsNone(rce_delay(replace(settings(),**kw),result(),enabled=True))

    def test_parallel_rce_and_pstryk_use_system_power(self):
        s=replace(settings(),inverter_count=2)
        self.assertIsNotNone(rce_delay(s,result(),enabled=True))
        data=build_joint_input(s,{'allow_delay':True,'maximum_soc':100.,
            'charge_power_percent':100.,'charge_efficiency':95.,
            'minimum_saving':.01,'demand_margin_percent':0.,
            'allow_buy':True,'allow_sell':True},pv_origin_kwh=0.)
        self.assertTrue(data.allow_delay)

    def test_an_earlier_fixed_rce_action_is_a_barrier(self):
        r=result();r.planned_exports=[SimpleNamespace(start=NOW+timedelta(minutes=30))]
        self.assertIsNone(rce_delay(settings(),r,enabled=True))
        r=result();r.current_slot_planned_export_kwh=.5
        self.assertIsNone(rce_delay(settings(),r,enabled=True))

    def test_tariff_missing_or_early_action_is_a_barrier(self):
        for attrs in ({}, {'result_current':True,'recalculation_pending':False,'current_slot_planned':True},
            {'result_current':True,'recalculation_pending':False,'current_slot_planned':False,
             'planned_slots':[{'date':'2026-09-29','start':'06:30'}]}):
            self.assertIsNone(rce_delay(settings(),result(),enabled=True,tariff_enabled=True,tariff_attributes=attrs))

    def test_stale_or_future_tariff_publication_withdraws_proposal(self):
        for stamp in (NOW-timedelta(seconds=121), NOW+timedelta(seconds=1)):
            state=SimpleNamespace(last_reported=stamp,attributes={
                'result_current':True,'recalculation_pending':False,'planned_slots':[]})
            sensor=SimpleNamespace(_attributes={},hass=SimpleNamespace(states=SimpleNamespace(
                is_state=lambda entity,value:value=='on',get=lambda entity:state)))
            update_rce_attributes(sensor,settings(),result())
            self.assertEqual(sensor._attributes['pv_charge_delay_status'],'inputs_unavailable')
            self.assertFalse(sensor._attributes['pv_charge_delay_execution_ready'])

    def test_pstryk_projection_exposes_enabled_delay_and_its_gain(self):
        s=settings();options={'maximum_soc':90.,'charge_power_percent':100.,'charge_efficiency':95.,
            'minimum_saving':.01,'demand_margin_percent':0.,'allow_buy':False,'allow_sell':True,'allow_delay':True}
        data=build_joint_input(s,options,pv_origin_kwh=0.)
        on=optimize(data);off=optimize(replace(data,allow_delay=False))
        a,b=projections(data,on,revision=2,metadata={},system_power_kw=5.)
        self.assertTrue(a['pv_charge_delay_enabled'])
        self.assertTrue(a['pv_charge_delay_execution_ready'])
        self.assertEqual(on.slots[0].action,'pv_charge_hold')
        self.assertEqual(on.slots[0].battery_out_kwh,0.)
        self.assertGreater(on.benefit_pln,off.benefit_pln)
        self.assertAlmostEqual(off.cost_pln-on.cost_pln,on.delay_plan.benefit_pln)
        self.assertNotEqual(input_key(data,profile_revision=1,price_revision='p'),
            input_key(replace(data,allow_delay=False),profile_revision=1,price_revision='p'))

if __name__=='__main__': unittest.main()
