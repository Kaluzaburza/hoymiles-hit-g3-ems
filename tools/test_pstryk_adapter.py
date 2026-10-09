"""Public Pstryk prices through the actual qualified RCE input adapter."""
import unittest
from datetime import datetime,timedelta,timezone
from dataclasses import replace
import test_rce_forecast_market_scope as fixture

class AdapterTests(unittest.TestCase):
    def test_execution_limits_match_classic_rce_including_fail_closed_and_master(self):
        _,sensor,_,_,_=fixture.fixture()
        from custom_components.hoymiles_hit_modbus.pstryk_plan import execution_metadata
        from custom_components.hoymiles_hit_modbus.rce_optimizer import _optimize_rce_impl
        base,_=sensor._optimizer_input()
        for changes in ({}, {'inverter_count':2}, {'bms_max_discharge_current_a':0.},
                        {'bms_discharge_data_available':False}, {'bms_discharge_data_fresh':False},
                        {'battery_voltage_v':None}, {'bms_power_safety_percent':50.},
                        {'export_efficiency_percent':80.}, {'export_power_cap_kw':0.},
                        {'effective_export_power_kw':.5}):
            with self.subTest(changes=changes):
                settings=replace(base,**changes)
                classic=_optimize_rce_impl(settings,fixed_exports={})
                published=execution_metadata(settings)
                self.assertEqual(published['bms_discharge_power_limit_kw'],round(classic.bms_discharge_power_limit_kw,2))
                self.assertEqual(published['bms_discharge_limit_percent'],round(classic.bms_discharge_limit_percent,1))
                self.assertEqual(published['bms_limit_active'],classic.bms_limit_active)
                self.assertEqual(published['physical_limit_source'],classic.physical_limit_source)

    def test_bms_availability_and_safety_factor_match_rce(self):
        _,sensor,_,_,_=fixture.fixture()
        from custom_components.hoymiles_hit_modbus.pstryk_plan import build_joint_input
        base,_=sensor._optimizer_input()
        opts={'maximum_soc':95.,'charge_power_percent':30.,'charge_efficiency':95.,
              'minimum_saving':.01,'demand_margin_percent':10.,'allow_buy':True,'allow_sell':True}
        unavailable=replace(base,bms_charge_data_available=False,bms_discharge_data_available=False)
        data=build_joint_input(unavailable,opts)
        self.assertEqual((data.charge_kw,data.discharge_kw,data.home_discharge_kw,data.charge_dc_kw),(0.,0.,0.,0.))
        low=replace(base,bms_power_safety_percent=1.)
        data=build_joint_input(low,opts)
        self.assertLessEqual(data.charge_dc_kw,low.bms_max_charge_current_a*low.battery_voltage_v*.01/1000)

    def test_autumn_fold_survives_adapter_and_joint_timeline(self):
        _,sensor,_,_,_=fixture.fixture()
        from custom_components.hoymiles_hit_modbus.pstryk_prices import parse_prices,request_window,WARSAW
        from custom_components.hoymiles_hit_modbus.pstryk_plan import compatibility_rows,build_joint_input
        from custom_components.hoymiles_hit_modbus.rce_optimizer import parse_rce_rows
        now=datetime(2026,10,24,tzinfo=WARSAW)
        left,right=request_window(now);at=left;frames=[]
        while at<right:
            frames.append({'start':at.isoformat(),'end':(at+timedelta(hours=1)).isoformat(),'priceNet':len(frames)/100})
            at+=timedelta(hours=1)
        snapshot=parse_prices({'frames':frames},source_scope='dst',fetched_at=now,window_start=left,window_end=right)
        prices=parse_rce_rows(compatibility_rows(snapshot,WARSAW),WARSAW,
            block_enabled=False,block_start_minute=0,block_end_minute=0)
        base,_=sensor._optimizer_input()
        base=replace(base,now=now,price_slots=prices,conservative_pv_by_slot_kwh={p.start:0. for p in prices})
        data=build_joint_input(base,{'maximum_soc':95.,'charge_power_percent':30.,'charge_efficiency':95.,
            'minimum_saving':.01,'demand_margin_percent':10.,'allow_buy':True,'allow_sell':True},pv_origin_kwh=0.)
        self.assertEqual(len(data.slots),98)
        self.assertEqual(len({p.start for p in data.slots}),98)
        self.assertTrue(all(a.end==b.start for a,b in zip(data.slots,data.slots[1:])))
        repeated=[p for p in data.slots if p.start.astimezone(WARSAW).date().isoformat()=='2026-10-25'
                  and p.start.astimezone(WARSAW).hour==2]
        self.assertEqual(len(repeated),4)
        self.assertNotEqual(repeated[0].net,repeated[2].net)

    def test_clock_only_progress_does_not_change_qualified_plan_input(self):
        m,sensor,clock,states,put=fixture.fixture()
        from custom_components.hoymiles_hit_modbus.pstryk_plan import build_joint_input,input_key
        options={'maximum_soc':95.,'charge_power_percent':30.,'charge_efficiency':95.,
                 'minimum_saving':.01,'demand_margin_percent':10.,'allow_buy':True,'allow_sell':True}
        before,_=sensor._optimizer_input()
        a=build_joint_input(before,options,pv_origin_kwh=0.)
        clock[0]+=timedelta(milliseconds=50)
        after,_=sensor._optimizer_input()
        b=build_joint_input(after,options,pv_origin_kwh=0.)
        self.assertEqual(input_key(a,profile_revision=1,price_revision='p'),
                         input_key(b,profile_revision=1,price_revision='p'))

    def test_public_prices_do_not_require_pse(self):
        m,sensor,clock,states,put=fixture.fixture()
        from custom_components.hoymiles_hit_modbus.pstryk_prices import parse_prices,request_window
        now=clock[0]
        left,right=request_window(now)
        frames=[]; stamp=left
        while stamp<right:
            frames.append({'start':stamp.isoformat(),'end':(stamp+timedelta(hours=1)).isoformat(),'priceNet':-.1})
            stamp+=timedelta(hours=1)
        snapshot=parse_prices({'frames':frames},source_scope='entry',fetched_at=now,window_start=left,window_end=right)
        states.pop('sensor.hoymiles_rce_day',None)
        states.pop('sensor.hoymiles_rce_day_tomorrow',None)
        settings,meta=sensor._optimizer_input(public_prices=snapshot)
        self.assertIsNotNone(settings,meta.get('missing_entities'))
        self.assertTrue(meta['rce_today_data_fresh'])
        self.assertTrue(all(abs(p.price_pln_kwh+.1)<1e-8 for p in settings.price_slots))
        self.assertGreater(sum(settings.pv_by_slot_kwh.values()),0)
        # Stored day-ahead prices do not expire at the legacy REST 20-minute
        # boundary. Preserve the actual age; do not borrow PSE's validity flag.
        older = replace(snapshot, fetched_at=now-timedelta(hours=6))
        cached, cached_meta = sensor._optimizer_input(public_prices=older)
        self.assertIsNotNone(cached, cached_meta.get('missing_entities'))
        self.assertTrue(cached_meta['rce_today_data_fresh'])
        self.assertTrue(cached_meta['rce_tomorrow_data_fresh'])
        self.assertEqual(cached_meta['rce_today_age_seconds'],21600)
        expired, expired_meta = sensor._optimizer_input(public_prices=replace(snapshot,
            fetched_at=now-timedelta(days=4)))
        self.assertIsNone(expired)
        self.assertFalse(expired_meta['rce_today_data_fresh'])
        # Pstryk now uses the same sale settings and qualification as RCE.
        put('input_number.hoymiles_rce_minimum_net_export_power', 'unavailable')
        independent, independent_meta = sensor._optimizer_input(public_prices=snapshot)
        self.assertIsNone(independent)
        self.assertIn('input_number.hoymiles_rce_minimum_net_export_power',independent_meta.get('missing_entities',[]))
        # Existing classic mode still requires the real PSE input.
        classic,classic_meta=sensor._optimizer_input()
        self.assertIsNone(classic)
        self.assertIn('sensor.hoymiles_rce_day',classic_meta['missing_entities'])

    def test_missing_tomorrow_price_does_not_erase_pv(self):
        m,sensor,clock,states,put=fixture.fixture()
        from custom_components.hoymiles_hit_modbus.pstryk_prices import parse_prices,request_window,WARSAW
        now=clock[0]; left,right=request_window(now)
        frames=[]; stamp=left
        while stamp.astimezone(WARSAW).date()==now.astimezone(WARSAW).date():
            frames.append({'start':stamp.isoformat(),'end':(stamp+timedelta(hours=1)).isoformat(),'priceNet':.5})
            stamp+=timedelta(hours=1)
        snapshot=parse_prices({'frames':frames},source_scope='entry',fetched_at=now,window_start=left,window_end=right)
        settings,meta=sensor._optimizer_input(public_prices=snapshot)
        self.assertIsNotNone(settings,meta.get('missing_entities'))
        tomorrow=now.astimezone(WARSAW).date()+timedelta(days=1)
        self.assertGreater(sum(v for k,v in settings.pv_by_slot_kwh.items() if k.astimezone(WARSAW).date()==tomorrow),0)
        self.assertFalse(any(p.start.astimezone(WARSAW).date()==tomorrow for p in settings.price_slots))

if __name__=='__main__': unittest.main()
