"""Regression for field-reported Pstryk PV economics and dashboard contracts."""
from dataclasses import replace
from datetime import timedelta
import unittest
from types import SimpleNamespace
from test_pv_charge_delay_adapter import settings
from custom_components.hoymiles_hit_modbus.pstryk_plan import build_joint_input,projections,timeline
from custom_components.hoymiles_hit_modbus.pstryk_joint import EnergySlot,JointInput,optimize,simulate
from custom_components.hoymiles_hit_modbus.automation_plan_timeline import build_current_payload,CurrentActualSnapshot
import test_supervisor_canonical_runtime as canonical

OPTIONS=dict(maximum_soc=100.,charge_power_percent=100.,charge_efficiency=95.,
    minimum_saving=.01,demand_margin_percent=0.,allow_buy=True,allow_sell=True,allow_delay=True)

class EconomicPresentationTests(unittest.TestCase):
    def test_decimal_self_use_flows_never_publish_negative_import_or_export(self):
        now=settings().now
        for export in (0.,5.):
            for n in range(1,200):
                for load in (.01,.19,.27,1.37):
                    row=EnergySlot(now,now+timedelta(minutes=30),None,load,n/100,True)
                    data=JointInput((row,),10.,5.,2.,10.,0.,5.,5.,5.,export,
                        allow_buy=False,allow_sell=False)
                    points,cost=simulate(data,(0,))
                    self.assertIsNone(cost)
                    p=points[0]
                    self.assertGreaterEqual(p.grid_import_kwh,0.,(export,load,n,p))
                    self.assertGreaterEqual(p.grid_export_kwh,0.,(export,load,n,p))
                    self.assertGreaterEqual(p.curtailed_kwh,0.)
                    losses=(p.battery_in_kwh/data.pv_charge_efficiency-p.battery_in_kwh
                        +p.battery_out_kwh*(1-data.discharge_efficiency))
                    self.assertAlmostEqual(p.pv_kwh-p.curtailed_kwh+p.grid_import_kwh-p.grid_export_kwh,
                        p.load_kwh+p.end_kwh-p.start_kwh+losses)

    def test_unpriced_tomorrow_keeps_continuous_forecast_without_actions(self):
        s=settings();end=s.now+timedelta(days=1)
        pv={s.now+timedelta(minutes=30*i): (2. if i>=32 else 0.) for i in range(48)}
        s=replace(s,pv_by_slot_kwh=pv,conservative_pv_by_slot_kwh=pv)
        data=build_joint_input(s,OPTIONS,pv_origin_kwh=0.,forecast_end=end)
        plan=optimize(data)
        self.assertEqual(len(plan.slots),len(s.price_slots))
        for role in ('tariff','rce'):
            trace=timeline(data,plan,role,5.)
            self.assertEqual(trace.points[-1].end,end)
            tail=trace.points[len(plan.slots):]
            self.assertTrue(tail)
            self.assertTrue(all(not p.selected and p.action_code=='idle' for p in tail))
            for before,p in zip(trace.points,trace.points[1:]):
                self.assertEqual(before.end,p.start)
                self.assertAlmostEqual((p.soc_percent-before.soc_percent)*data.capacity_kwh/100,p.battery_delta_kwh)
            for p in tail:
                price=p.policy.buy_price_pln_kwh if role=='tariff' else p.policy.sell_price_pln_kwh
                self.assertIsNone(price)
            self.assertGreater(sum(p.pv_kwh for p in tail),0.)
        # Presentation must not alter the priced optimizer's action or result.
        original=optimize(replace(data,forecast_tail=()))
        self.assertEqual(plan,original)

    def test_empty_and_pending_pstryk_plans_do_not_claim_scheduled_actions(self):
        from custom_components.hoymiles_hit_modbus.rce_sensor import HoymilesRCEOptimizerSensor
        from custom_components.hoymiles_hit_modbus.tariff_sensor import HoymilesTariffOptimizerSensor
        for cls in (HoymilesRCEOptimizerSensor,HoymilesTariffOptimizerSensor):
            sensor=object.__new__(cls)
            sensor.hass=SimpleNamespace(config=SimpleNamespace(language='pl'))
            sensor._attributes={'status_code':'ready','price_provider':'Pstryk','result_current':True,
                'recalculation_pending':False,'planned_slots':[]}
            self.assertIn('Brak zaplanowan',sensor.native_value)
            sensor._attributes.update(result_current=False,recalculation_pending=True)
            self.assertEqual(sensor.native_value,'Przeliczanie planu Pstryk')

    def test_no_sale_displays_the_actual_reason_in_the_shared_plan_state(self):
        from custom_components.hoymiles_hit_modbus.rce_sensor import HoymilesRCEOptimizerSensor
        from custom_components.hoymiles_hit_modbus.pstryk_plan import SALE_REASON_TEXT
        sensor=object.__new__(HoymilesRCEOptimizerSensor)
        for language in ('pl','en'):
            sensor.hass=SimpleNamespace(config=SimpleNamespace(language=language))
            for reason,message in SALE_REASON_TEXT[language].items():
                sensor._attributes={'status_code':'ready','price_provider':'Pstryk','result_current':True,
                    'recalculation_pending':False,'planned_slots':[],'sale_decision_reason':reason}
                self.assertEqual(sensor.native_value,message)
                self.assertLess(len(message),255)
                sensor._attributes.update(result_current=False,recalculation_pending=True)
                self.assertNotEqual(sensor.native_value,message)

    def canonical_case(self,kind,unpriced=False):
        now=settings().now;canonical.NOW=now
        prices=(.4,.1,2.,2.) if kind=='buy' else (.1,2.,.1,.1)
        rows=tuple(EnergySlot(now+timedelta(minutes=30*i),now+timedelta(minutes=30*(i+1)),
            price,1. if kind=='buy' and i>0 else 0.,3. if kind=='sell' and i>=2 else 0.) for i,price in enumerate(prices))
        data=JointInput(rows,10.,2. if kind=='buy' else 9.,2.,9.,0. if kind=='buy' else 7.,3.,3.,5.,5.)
        if unpriced:
            data=replace(data,forecast_tail=tuple(EnergySlot(rows[-1].end+timedelta(minutes=30*i),
                rows[-1].end+timedelta(minutes=30*(i+1)),None,.5,1.5,True) for i in range(48)))
        plan=optimize(data);frame=canonical.frame()
        soc=data.initial_kwh/data.capacity_kwh*100
        frame.execution=replace(frame.execution,battery_soc_percent=soc)
        frame.context=replace(frame.context,battery_soc_percent=soc)
        frame.rce=replace(frame.rce,system_power_kw=5.)
        frame.tariff=replace(frame.tariff,requested_charge_power_kw=0.,command_charge_power_percent=0.)
        payloads=canonical.timelines(tariff_selected=False)
        actual=CurrentActualSnapshot(now,0.,0.,0.,0.,soc,'complete',{})
        for role in ('rce','tariff'):
            payloads[role]=build_current_payload(timeline(data,plan,role,5.),
                config_entry_id='entry-a',generated_at=now,input_revision=1,plan_revision=1,
                plan_entity_id='sensor.'+role+'_plan',current_actual=actual,sources=[],physical_active=False)
        payloads['rcm']['current_actual']['soc_percent']=soc
        for point in payloads['rcm']['points']:
            point['soc_percent']=point['baseline_soc_percent']=soc
        ledger=canonical.build_supervisor_canonical_ledger(frame=frame,timelines=payloads,usable_capacity_kwh=10.)
        return data,plan,ledger,payloads

    def test_real_canonical_ledger_extends_unpriced_forecast_without_authority(self):
        for action in ('buy','sell'):
            data,plan,ledger,payloads=self.canonical_case(action,unpriced=True)
            self.assertTrue(canonical.audit_canonical_execution_ledger(ledger).valid)
            self.assertEqual(ledger.slots[-1].ends_at,data.forecast_tail[-1].end)
            after=[p for p in ledger.slots if p.starts_at>=plan.slots[-1].end]
            self.assertTrue(after)
            self.assertTrue(all(p.selected_policy.value=='none' for p in after))

    def test_missing_prices_cannot_enter_optimizer_or_become_an_action(self):
        data=build_joint_input(settings(),OPTIONS,pv_origin_kwh=0.)
        missing=replace(data,slots=tuple(replace(p,net=None) for p in data.slots))
        with self.assertRaises(ValueError):optimize(missing)
        for action in (-2,-1,1,2,3,4):
            with self.assertRaisesRegex(ValueError,'unpriced_action_forbidden'):
                simulate(missing,(action,)*len(missing.slots))

    def test_real_canonical_ledger_keeps_future_buy_and_sell(self):
        for action,policy in (('buy','tariff'),('sell','rce')):
            data,plan,ledger,payloads=self.canonical_case(action)
            self.assertTrue(any(p.action==action and p.start>data.slots[0].start for p in plan.slots),action)
            selected=[s for s in ledger.slots if s.selected_policy.value==policy and s.starts_at>data.slots[0].start]
            self.assertTrue(selected,action)
            self.assertTrue(canonical.audit_canonical_execution_ledger(ledger).valid)
            if action=='buy':
                for slot in selected:
                    source=next(p for p in plan.slots if p.start<=slot.starts_at<p.end)
                    command=next(v.value for v in slot.command_expectation.values if v.name=='maximum_charge_power')
                    self.assertEqual(command,round(source.command_kw/5.*100))

    def test_sale_risk_guard_keeps_pv_in_purchase_balance(self):
        s=replace(settings(),critical_zero_pv_guard=True,export_power_cap_kw=0.)
        guarded=build_joint_input(s,OPTIONS,pv_origin_kwh=0.)
        regular=build_joint_input(replace(s,critical_zero_pv_guard=False),OPTIONS,pv_origin_kwh=0.)
        self.assertEqual([p.pv_kwh for p in guarded.slots],[p.pv_kwh for p in regular.slots])
        self.assertEqual(guarded.terminal_kwh,regular.terminal_kwh)
        plan=optimize(guarded)
        self.assertFalse(any(p.grid_charge_kwh>1e-8 for p in plan.slots))
        self.assertGreaterEqual(max(p.end_kwh for p in plan.slots),guarded.maximum_kwh-1e-8)

    def test_sale_stress_forecast_does_not_create_a_purchase(self):
        s=settings()
        s=replace(s,conservative_pv_by_slot_kwh={k:0. for k in s.pv_by_slot_kwh},
            critical_zero_pv_guard=False,export_power_cap_kw=0.)
        data=build_joint_input(s,OPTIONS,pv_origin_kwh=0.)
        self.assertGreater(sum(p.pv_kwh for p in data.slots[1:]),0.)
        self.assertFalse(any(p.grid_charge_kwh>1e-8 for p in optimize(data).slots))
        self.assertTrue(all(p==0. for p in data.sale_pv_kwh[1:]))

    def test_expected_full_battery_is_not_enough_to_allow_pv_delay(self):
        s=settings()
        s=replace(s,conservative_pv_by_slot_kwh={k:0. for k in s.pv_by_slot_kwh},critical_zero_pv_guard=False)
        data=build_joint_input(s,OPTIONS,pv_origin_kwh=0.)
        self.assertIsNone(optimize(data).delay_plan)

    def test_zero_pv_guard_protects_sale_without_fabricating_buy_deficit(self):
        s=replace(settings(),critical_zero_pv_guard=True,battery_soc_percent=90.)
        data=build_joint_input(s,OPTIONS,pv_origin_kwh=7.)
        points,_=simulate(data,(-2,)*len(data.slots))
        zero=replace(data,slots=tuple(replace(p,pv_kwh=0.) for p in data.slots))
        zero_points,_=simulate(zero,(-2,)*len(data.slots))
        for p,z in zip(points,zero_points):
            self.assertGreaterEqual(p.protected_kwh,z.protected_kwh)
            if p.action=='sell':self.assertGreaterEqual(p.end_kwh+1e-8,p.protected_kwh)
        self.assertIsNone(optimize(data).delay_plan)

    def test_shared_timelines_have_real_baseline_soc(self):
        data=build_joint_input(settings(),OPTIONS,pv_origin_kwh=0.)
        plan=optimize(data);baseline,_=simulate(data,(0,)*len(data.slots))
        for role in ('rce','tariff'):
            trace=timeline(data,plan,role,5.)
            self.assertEqual([p.baseline_soc_percent for p in trace.points],
                [p.end_kwh/data.capacity_kwh*100 for p in baseline])

    def test_rce_legacy_cards_receive_energy_and_revenue(self):
        now=settings().now
        rows=tuple(EnergySlot(now+timedelta(minutes=30*i),now+timedelta(minutes=30*(i+1)),
            2. if i==0 else .1,0.,0. if i==0 else 3.) for i in range(4))
        data=JointInput(rows,10.,9.,2.,9.,7.,3.,3.,5.,5.)
        plan=optimize(data);rce,_=projections(data,plan,revision=1,metadata={},system_power_kw=5.)
        self.assertTrue(rce['planned_slots'])
        self.assertAlmostEqual(rce['planned_revenue_pln'],sum(p.battery_export_kwh*p.net for p in plan.slots))
        for row in rce['planned_slots']:
            self.assertGreater(row['energy'],0.)
            self.assertAlmostEqual(row['revenue'],row['energy']*row['price'])

if __name__=='__main__':unittest.main()
