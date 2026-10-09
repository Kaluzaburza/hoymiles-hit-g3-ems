"""Pstryk orchestration on an isolated real Home Assistant event loop/Store.

No network or device writes. Physical/forecast qualification remains covered
by the existing RCE adapter suite; here its accepted input is injected.
"""
import asyncio
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.template import Template
import yaml

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from custom_components.hoymiles_hit_modbus import pstryk_runtime as m
from custom_components.hoymiles_hit_modbus.pstryk_prices import parse_prices,request_window
from custom_components.hoymiles_hit_modbus.dynamic_price_profile import DynamicPriceProfile,paired_plan_current
from custom_components.hoymiles_hit_modbus.rce_optimizer import OptimizerInput,PriceSlot
from custom_components.hoymiles_hit_modbus.pstryk_plan import build_joint_input,projections,timeline,compatibility_rows
from custom_components.hoymiles_hit_modbus.pstryk_joint import optimize
from custom_components.hoymiles_hit_modbus import supervisor_runtime as supervisor
from custom_components.hoymiles_hit_modbus.ems_supervisor import apply_minimum_grid_power
from custom_components.hoymiles_hit_modbus.automation_plan_timeline import build_current_payload,CurrentActualSnapshot

NOW=datetime(2026,9,22,10,tzinfo=timezone.utc)

def settings():
    slots=[PriceSlot(NOW+timedelta(minutes=30*i), .1 if i<2 else 2.) for i in range(4)]
    return OptimizerInput(now=NOW,price_slots=slots,pv_by_slot_kwh={},battery_capacity_kwh=10.,
        battery_soc_percent=20.,outage_reserve_soc_percent=20.,safety_margin_soc_percent=0.,
        manual_minimum_soc_percent=20.,dynamic_reserve_enabled=True,average_daily_load_kwh=24.,
        average_night_load_kwh=8.,night_start_minute=1200,night_end_minute=360,
        inverter_power_kw=5.,inverter_ac_power_kw=5.,inverter_count=1,
        discharge_power_percent=60.,export_efficiency_percent=95.,bms_max_charge_current_a=100.,
        bms_max_discharge_current_a=100.,battery_voltage_v=51.2,
        bms_charge_data_fresh=True,bms_discharge_data_fresh=True,
        bms_charge_data_available=True,bms_discharge_data_available=True,
        conservative_pv_by_slot_kwh={},export_power_cap_kw=5.,
        current_load_power_kw=1.,current_pv_power_kw=0.,current_battery_soc_fresh=True)

class Plan:
    def __init__(self, hass, name):
        self.hass=hass; self.entity_id='sensor.'+name
        self._attributes={}; self._result=None; self._timeline_sensor=None
        self._input_revision=SimpleNamespace(value=1)
        self._lifecycle_stopped=False; self._forecast_gcf_policy_evaluation_cancel=None
        self.device_info=None
        self.calls=0
    def async_write_ha_state(self):
        self.hass.states.async_set(self.entity_id,'ready',self._attributes)
    async def _recalculate_and_write(self): self.calls+=1

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_day_survives_transport_error_and_four_hours(self):
        await self.pstryk()
        original = self.c.cache.view(now=NOW).snapshot
        self.c.cache.failed('invalid_response')
        view = self.c.cache.view(now=NOW+timedelta(hours=4))
        self.assertIsNotNone(view.snapshot)
        self.assertEqual(view.snapshot.revision, original.revision)
        self.assertEqual(view.snapshot.fetched_at, original.fetched_at)

    async def test_daily_cache_avoids_http_for_complete_days_after_restart(self):
        await self.pstryk()
        raw = await self.c.store.async_load()
        self.c.cache = m.PriceCache.restore(raw['prices'], source_scope=self.c.scope, now=NOW)
        self.c.sensor.publish = lambda: None
        # The transport stub fails on *any* HTTP. Both dates are already saved.
        for hour in (0,1,4,8):
            with patch.object(m.dt_util, 'utcnow', return_value=NOW+timedelta(hours=hour)):
                await self.c._refresh()
            self.assertTrue(self.rce._attributes['result_current'])
        self.assertEqual(await self.c.store.async_load(), raw)

    async def test_missing_tomorrow_does_not_refetch_or_invalidate_today(self):
        from custom_components.hoymiles_hit_modbus.pstryk_daily_cache import day_bounds
        from custom_components.hoymiles_hit_modbus.pstryk_prices import WARSAW
        await self.pstryk()
        today = NOW.astimezone(WARSAW).date()
        self.c.cache.days.pop((today+timedelta(days=1)).isoformat())
        self.c.sensor.publish = lambda: None
        calls = []
        async def unpublished(**kwargs):
            calls.append(kwargs)
            return parse_prices({'frames':[]},source_scope=self.c.scope,fetched_at=kwargs['received_at'],
                window_start=kwargs['start'],window_end=kwargs['end'])
        self.c._client = SimpleNamespace(fetch_window=unpublished,close=lambda: asyncio.sleep(0))
        for minutes in (0,1,14,15,16,44,45):
            with patch.object(m.dt_util,'utcnow',return_value=NOW+timedelta(minutes=minutes)):
                await self.c._refresh()
            self.assertTrue(self.rce._attributes['result_current'])
            self.assertIsNotNone(self.c.cache.get(today,NOW))
        self.assertEqual(len(calls),3)
        self.assertTrue(all((c['start'],c['end'])==day_bounds(today+timedelta(days=1)) for c in calls))

    async def test_pv_delay_commitment_is_carried_through_real_joint_publications(self):
        from custom_components.hoymiles_hit_modbus.pv_charge_delay import PvDelayCommitment
        start = NOW.replace(hour=11, minute=30)  # 13:30 Europe/Warsaw
        await self.pstryk()
        # Same public market scope, fetched before the late-start run.
        self.c.cache.days = {k: replace(v, fetched_at=start) for k,v in self.c.cache.days.items()}
        prices = [PriceSlot(start+timedelta(minutes=30*i), .9 if i<3 else .1) for i in range(25)]
        pv = {p.start:3. if p.start.hour<15 else 0. for p in prices}
        self.settings = replace(self.settings, now=start, price_slots=prices,
            pv_by_slot_kwh=pv, conservative_pv_by_slot_kwh=pv, delay_pv_by_slot_kwh=pv,
            battery_wear_cost_pln_kwh=10., average_daily_load_kwh=5., average_night_load_kwh=2.,
            current_load_power_kw=.5, current_pv_power_kw=5., battery_soc_percent=70.)
        self.metadata.update(forecast_today_raw_kwh=40., forecast_today_p10_kwh=30.)
        self.hass.states.async_set(m.DELAY_HELPER, 'on')
        self.hass.states.async_set(m.OPTION_HELPERS['maximum_soc'], '100')
        with patch.object(m.dt_util, 'utcnow', return_value=start):
            await self.c.recalculate()
        original = self.c._accepted[1].delay_plan
        self.assertIsNotNone(original)
        proof = PvDelayCommitment('pv-hold:verified', start, original.end)
        self.rce._active_pv_delay_commitment = lambda now: proof
        for minutes in (30,60):
            now = start+timedelta(minutes=minutes)
            self.settings = replace(self.settings, now=now)
            with patch.object(m.dt_util, 'utcnow', return_value=now):
                self.c.invalidate('recalculation_pending')
                await self.c.recalculate()
            self.assertTrue(self.rce._attributes['result_current'])
            self.assertTrue(self.rce._attributes['pv_charge_delay_execution_ready'],
                (minutes, original, self.c._accepted[0].active_delay, self.c._accepted[1].delay_plan))
            self.assertEqual(self.c._accepted[0].active_delay.end, original.end)
            self.assertEqual(self.c._accepted[1].delay_plan.recovered_at, original.recovered_at)
            self.assertEqual(self.rce._attributes['pv_charge_delay_start'], original.start.isoformat())
            self.assertEqual(self.rce._attributes['joint_plan_revision'], self.tariff._attributes['joint_plan_revision'])
        self.assertEqual(self.c._revision, 3)
        self.rce._active_pv_delay_commitment = lambda now: None
        with patch.object(m.dt_util, 'utcnow', return_value=now):
            self.c.invalidate('recalculation_pending')
            await self.c.recalculate()
        self.assertIsNone(self.c._accepted[0].active_delay)
        self.assertFalse(self.rce._attributes['pv_charge_delay_execution_ready'])

    async def test_forecast_horizon_requires_qualified_tomorrow_and_never_adds_prices(self):
        await self.pstryk()
        for fresh,days in ((True,2),(False,1)):
            self.metadata['forecast_tomorrow_data_fresh']=fresh
            data,metadata,settings,key=self.c._input()
            self.assertEqual(len(data.slots),len(self.settings.price_slots))
            self.assertEqual(data.forecast_tail[-1].end,
                self.settings.now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=days))
            self.assertTrue(all(p.net is None and p.sale_blocked for p in data.forecast_tail))

    def render_gate(self, key):
        package=yaml.safe_load((ROOT/'home_assistant/hoymiles_ems_scheduler.yaml').read_text(encoding='utf-8'))
        row=next(row for block in package['template'] for domain in ('sensor','binary_sensor')
                 for row in block.get(domain,[]) if row.get('unique_id')==key)
        return Template(row['state'],self.hass).async_render(parse_result=True)

    def publish_template_sensor(self, key):
        package=yaml.safe_load((ROOT/'home_assistant/hoymiles_ems_scheduler.yaml').read_text(encoding='utf-8'))
        row=next(row for block in package['template'] for row in block.get('sensor',[])
                 if row.get('unique_id')==key)
        available=Template(row.get('availability','{{ true }}'),self.hass).async_render(parse_result=True)
        value=self.render_gate(key) if available else 'unavailable'
        self.hass.states.async_set('sensor.'+key,str(value))
        return value

    async def test_real_tariff_template_and_supervisor_accept_joint_buy(self):
        await self.pstryk(); await self.c.recalculate()
        attrs={**self.tariff._attributes,'forecast_today_age_minutes':0.,'forecast_tomorrow_age_minutes':0.}
        self.hass.states.async_set('sensor.hoymiles_hit_tariff_charge_plan','ready',attrs)
        for entity,value in {'binary_sensor.hoymiles_ems_execution_ready':'on',
            'sensor.hoymiles_hit_maximum_charge_current':100,'sensor.hoymiles_hit_battery_voltage_bms':51.2,
            'sensor.hoymiles_hit_overview_battery_soc':20,'sensor.hoymiles_tariff_target_soc':attrs['target_soc_percent'],
            'sensor.hoymiles_hit_ems_force_charge_soc_readback':90,
            'sensor.hoymiles_hit_ems_maximum_charge_power_readback':60}.items():
            self.hass.states.async_set(entity,str(value))
        ready=self.render_gate('hoymiles_tariff_control_data_ready')
        planned=self.render_gate('hoymiles_tariff_planned_charge_slot')
        self.assertIs(ready,True); self.assertIs(planned,True)
        fields={k:v for k,v in attrs.items() if k in supervisor.TariffSourceSnapshot.__dataclass_fields__}
        for k in ('observed_at','input_revision','current_soc_percent','maximum_soc_percent','current_soc_observed_at'):
            fields.pop(k,None)
        fields.update(status_code=supervisor.TariffPlanStatus(attrs['status_code']),
            current_action=supervisor.TariffAction(attrs['current_action']),
            current_run_need_class=supervisor.TariffRunNeed(attrs['current_run_need_class']),
            current_slot_end=datetime.fromisoformat(attrs['current_slot_end']),
            current_grid_charge_run_end=datetime.fromisoformat(attrs['current_grid_charge_run_end']))
        source=supervisor.TariffSourceSnapshot(**fields,observed_at=NOW,input_revision=1,
            allowed_by_user=True,enabled=True,active_latched=False,current_soc_percent=20.,
            current_soc_observed_at=NOW,maximum_soc_percent=90.,control_data_ready=ready,planned_slot_ready=planned)
        self.assertTrue(supervisor.build_tariff_candidate(source,now=NOW).start_eligible)
        self.hass.states.async_set('sensor.hoymiles_hit_maximum_charge_current','0')
        self.assertIs(self.render_gate('hoymiles_tariff_control_data_ready'),False)

    async def test_real_rce_template_and_supervisor_accept_joint_sell(self):
        await self.pstryk()
        slots=[PriceSlot(NOW+timedelta(minutes=30*i),2. if i==0 else .1) for i in range(4)]
        pv={p.start:0. if i==0 else 3. for i,p in enumerate(slots)}
        self.settings=replace(self.settings,price_slots=slots,pv_by_slot_kwh=pv,
            conservative_pv_by_slot_kwh=pv,battery_soc_percent=90.,average_daily_load_kwh=0.,
            average_night_load_kwh=0.,current_load_power_kw=0.)
        self.metadata.update({
            'rce_today_data_fresh':True,'rce_today_age_seconds':0.,
            'forecast_today_data_fresh':True,'forecast_today_age_seconds':0.,
            'bms_discharge_data_fresh':True,'bms_discharge_data_available':True,
            'bms_discharge_data_age_seconds':0.,'gcf_execution_data_fresh':True,
            'soc_data_fresh':True,'soc_data_age_seconds':0.})
        await self.c.recalculate()
        attrs=self.rce._attributes
        self.assertTrue(attrs['current_slot_planned'])
        self.hass.states.async_set('sensor.hoymiles_hit_rce_optimized_plan','ready',attrs)
        for entity,value in {'binary_sensor.hoymiles_ems_execution_ready':'on',
            'binary_sensor.hoymiles_ems_export_allowed':'on','input_boolean.hoymiles_rce_dynamic_soc_enabled':'on',
            'sensor.hoymiles_hit_ems_mode_readback_code':0,
            'sensor.hoymiles_hit_maximum_discharge_current':100,'sensor.hoymiles_hit_battery_voltage_bms':51.2,
            'sensor.hoymiles_hit_overview_battery_soc':90,
            'sensor.hoymiles_hit_ems_force_discharge_soc_readback':20,
            'input_number.hoymiles_rce_requested_discharge_power':60,
            'sensor.hoymiles_rce_current_price':2.,'sensor.hoymiles_hit_ems_maximum_discharge_power_readback':60}.items():
            self.hass.states.async_set(entity,str(value))
        bms=self.publish_template_sensor('hoymiles_rce_bms_safe_discharge_power')
        self.assertNotEqual(bms,'unavailable','The accepted Pstryk plan must publish the BMS limit required by RCE execution')
        effective=self.publish_template_sensor('hoymiles_rce_effective_discharge_power_percent')
        self.assertGreater(effective,0)
        dynamic=self.publish_template_sensor('hoymiles_rce_dynamic_minimum_soc')
        self.assertNotEqual(dynamic,'unavailable','Pstryk must publish the legacy minimum_soc used by HA')
        self.assertEqual(self.publish_template_sensor('hoymiles_rce_effective_minimum_soc'),dynamic)
        ready=self.render_gate('hoymiles_rce_control_data_ready')
        self.assertIs(ready,True)
        source=supervisor.RceSourceSnapshot(observed_at=NOW,allowed_by_user=True,enabled=True,active_latched=False,
            status_code=supervisor.RcePlanStatus.READY,result_current=True,recalculation_pending=False,input_revision=1,
            current_slot_planned=attrs['current_slot_planned'],current_slot_start_eligible=attrs['current_slot_start_eligible'],
            current_slot_continue_eligible=attrs['current_slot_continue_eligible'],
            current_slot_end=datetime.fromisoformat(attrs['current_slot_end']),current_run_end=datetime.fromisoformat(attrs['current_run_end']),
            requested_discharge_power_kw=attrs['current_slot_execution_discharge_power_kw'],
            planned_export_energy_kwh=attrs['current_slot_planned_export_kwh'],protected_soc_floor_percent=attrs['minimum_soc_percent'],
            effective_discharge_power_percent=effective,system_power_kw=5.,current_soc_percent=90.,
            control_data_ready=ready,price_above_threshold=True,reserve_ready=True,sale_block_active=False)
        candidate=apply_minimum_grid_power(supervisor.build_rce_candidate(source,now=NOW),
            attrs.get('current_slot_execution_export_power_kw'))
        self.assertTrue(candidate.available, candidate.blocked_reason)
        self.assertTrue(candidate.start_eligible)
        hours=(datetime.fromisoformat(attrs['current_slot_end'])-NOW).total_seconds()/3600
        self.assertAlmostEqual(attrs['current_slot_execution_export_power_kw'],
                               attrs['current_slot_planned_export_kwh']/hours)
        for invalid in (None, float('nan'), 0., .199):
            self.assertFalse(apply_minimum_grid_power(candidate,invalid).available)
        self.hass.states.async_set('binary_sensor.hoymiles_ems_export_allowed','off')
        self.assertIs(self.render_gate('hoymiles_rce_control_data_ready'),False)
        self.hass.states.async_set('binary_sensor.hoymiles_ems_export_allowed','on')
        for missing in ({'bms_discharge_power_limit_kw':None}, {'bms_discharge_power_limit_kw':0.},
                        {'bms_discharge_data_fresh':False}, {'bms_discharge_data_available':False},
                        {'result_current':False}):
            with self.subTest(missing=missing):
                self.hass.states.async_set('sensor.hoymiles_hit_rce_optimized_plan','ready',{**attrs,**missing})
                self.assertEqual(self.publish_template_sensor('hoymiles_rce_bms_safe_discharge_power'),'unavailable')
                self.assertEqual(self.publish_template_sensor('hoymiles_rce_effective_discharge_power_percent'),'unavailable')
                self.assertIs(self.render_gate('hoymiles_rce_control_data_ready'),False)
                self.assertFalse(supervisor.build_rce_candidate(replace(source,
                    effective_discharge_power_percent=None,control_data_ready=False),now=NOW).start_eligible)

    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='pstryk-ha-')
        self.hass=HomeAssistant(self.tmp.name)
        self.clock=patch.object(m.dt_util,'utcnow',return_value=NOW); self.clock.start()
        self.rce=Plan(self.hass,'pstryk_test_rce'); self.tariff=Plan(self.hass,'pstryk_test_tariff')
        self.settings=settings()
        self.metadata={k:True for k in ('rce_today_data_fresh','forecast_today_data_fresh','soc_data_fresh','gcf_execution_data_fresh')}
        self.rce._optimizer_input=lambda **kw:(self.settings,dict(self.metadata))
        self.runtime=SimpleNamespace(source_device=SimpleNamespace(id='device'),entities={'sensor':[]})
        self.c=m.PstrykRuntime(self.hass,SimpleNamespace(entry_id='entry'),self.runtime,self.rce,self.tariff)
        # No network permitted: this fails the test if orchestration tries it.
        async def network_forbidden(*args,**kwargs): raise AssertionError('Unexpected network')
        self.no_network=patch.object(m.PstrykClient,'fetch_window',network_forbidden); self.no_network.start()
        for entity,value in ((m.SALE,'RCE'),(m.PURCHASE,'PGE'),(m.TARIFF,'G12w')):
            self.hass.states.async_set(entity,value)
        for name,value in {'maximum_soc':90,'charge_power_percent':60,'charge_efficiency':95,
                           'minimum_saving':.01,'demand_margin_percent':0}.items():
            self.hass.states.async_set(m.OPTION_HELPERS[name],str(value))
        for e in m.PERMISSIONS: self.hass.states.async_set(e,'on')
        self.hass.states.async_set(m.DELAY_PROFILE_HELPER, 'Conservative')
        @callback
        def select(call):
            self.hass.states.async_set(call.data['entity_id'],call.data['option'],context=call.context)
        self.hass.services.async_register('input_select','select_option',select)
        await self.c.initialize()
        await self.c._select(None)
        self.c._tick=lambda _: None  # Explicit test scheduling; no HTTP timer.

    async def test_master_uses_qualified_system_stock_without_daily_counter_gate(self):
        await self.pstryk()
        self.settings=replace(self.settings,inverter_count=2,battery_soc_percent=85.)
        data,_,_,_=self.c._input()
        self.assertEqual(data.initial_kwh,8.5)
        self.assertEqual(data.pv_origin_kwh,8.5)
        self.assertEqual(self.runtime.entities['sensor'],[])
        self.settings=replace(self.settings,current_battery_soc_fresh=False)
        with self.assertRaisesRegex(ValueError,'physical_input_stale'):
            self.c._input()

    async def asyncTearDown(self):
        await self.c.close()
        await self.hass.async_block_till_done()
        self.no_network.stop(); self.clock.stop()
        await self.hass.async_stop()
        self.tmp.cleanup()

    def prices(self):
        start,end=request_window(NOW)
        rows=[]
        cursor=start
        while cursor<end:
            rows.append({'start':cursor.isoformat(),'end':(cursor+timedelta(hours=1)).isoformat(),'priceNet':.1})
            cursor+=timedelta(hours=1)
        # Provider spelling is deliberately identical to captured frames.
        return parse_prices({'frames':rows},source_scope=self.c.scope,fetched_at=NOW,window_start=start,window_end=end)

    async def pstryk(self):
        self.hass.states.async_set(m.SALE,'Pstryk')
        await self.c._select(m.SALE)
        self.c.cache.accept(self.prices(),now=NOW)
        await self.c._save()

    async def test_pair_selection_restore_and_permissions(self):
        before=[self.hass.states.get(e).state for e in m.PERMISSIONS]
        await self.pstryk()
        self.assertEqual(self.hass.states.get(m.PURCHASE).state,'Pstryk')
        self.hass.states.async_set(m.SALE,'RCE'); await self.c._select(m.SALE)
        self.assertEqual(self.hass.states.get(m.PURCHASE).state,'PGE')
        self.assertEqual(self.hass.states.get(m.TARIFF).state,'G12w')
        self.assertEqual(before,[self.hass.states.get(e).state for e in m.PERMISSIONS])

    async def test_purchase_selector_selects_both(self):
        self.hass.states.async_set(m.PURCHASE,'Pstryk'); await self.c._select(m.PURCHASE)
        self.assertEqual(self.c.profile.mode,'pstryk')
        self.assertEqual(self.hass.states.get(m.SALE).state,'Pstryk')

    async def test_missing_classic_can_be_explicitly_selected(self):
        self.c.profile=DynamicPriceProfile(mode='pstryk')
        self.hass.states.async_set(m.PURCHASE,'Pstryk')
        self.hass.states.async_set(m.SALE,'RCE')
        await self.c._select(m.SALE)
        self.assertTrue(self.c.active)
        self.hass.states.async_set(m.PURCHASE,'Manual')
        await self.c._select(m.PURCHASE)
        self.assertFalse(self.c.active)
        self.assertEqual(self.c.profile.classic_operator,'Manual')

    async def test_one_joint_revision_and_real_disk_store(self):
        await self.pstryk()
        await asyncio.gather(self.c.recalculate(),self.c.recalculate())
        a,b=self.rce._attributes,self.tariff._attributes
        self.assertTrue(paired_plan_current(a,b,pstryk_selected=True,pstryk_bound=True),(a,b))
        self.assertTrue(b['current_slot_planned'])
        self.assertFalse(a['current_slot_planned'])
        self.assertEqual(self.c._revision,1)
        raw=await self.c.store.async_load()
        self.assertLess(len(json.dumps(raw)),15000)
        self.assertEqual(json.loads(raw['profile'])['mode'],'pstryk')
        self.assertIsNone(b['estimated_savings_pln'])
        self.assertEqual(a['optimization_gain_pln'],a['joint_benefit_pln'])

    async def test_continuation_requires_physical_proof_and_cannot_restart(self):
        await self.pstryk(); await self.c.recalculate()
        self.assertIsNone(self.c._input()[0].continuing_action)
        from custom_components.hoymiles_hit_modbus.tariff_optimizer import TariffActiveCommitment
        proof=TariffActiveCommitment('tx','grid_support_and_charge',NOW-timedelta(seconds=20),
            NOW+timedelta(minutes=30),90.,60.,NOW)
        self.tariff._active_tariff_commitment=lambda _now:proof
        data,_,_,_=self.c._input()
        self.assertEqual(data.continuing_action,'buy')
        _,attrs=projections(data,optimize(data),revision=3,metadata={},system_power_kw=5.)
        self.assertTrue(attrs['current_run_continue_eligible'])
        self.assertFalse(attrs['current_run_start_eligible'])
        self.tariff._active_tariff_commitment=lambda _now:None
        self.assertIsNone(self.c._input()[0].continuing_action)

    async def test_buy_input_continuation_crosses_slot_only_with_original_basis(self):
        await self.pstryk(); await self.c.recalculate()
        from custom_components.hoymiles_hit_modbus.tariff_optimizer import TariffActiveCommitment
        at=NOW+timedelta(minutes=31)
        proof=TariffActiveCommitment('tx','grid_support_and_charge',NOW,
            NOW+timedelta(minutes=90),90.,60.,at)
        self.settings=replace(self.settings,now=at)
        self.tariff._active_tariff_commitment=lambda _now:proof
        with patch.object(m.dt_util,'utcnow',return_value=at):
            self.assertIsNone(self.c._input()[0].continuing_action)
            self.c._active_buy_basis=(proof.transaction_id,proof.hard_deadline,
                self.c._accepted_source,self.c._options(),self.c._accepted)
            data=self.c._input()[0]
            self.assertEqual(data.continuing_action,'buy')
            self.assertEqual(data.continuing_until,data.slots[0].end)
            self.tariff._active_tariff_commitment=lambda _now:None
            self.assertIsNone(self.c._input()[0].continuing_action)

    async def test_real_publication_keeps_original_confirmed_sale_basis(self):
        from test_pstryk_active_run_commitment import fixture, selected, OPTIONS
        from custom_components.hoymiles_hit_modbus.rce_optimizer import RceActiveCommitment
        await self.pstryk()
        before, original_data, original_plan = fixture()
        source = (self.c.profile.revision, self.c.cache.view(now=NOW).snapshot.revision)
        self.c._accepted = (original_data, original_plan, 5.)
        self.c._accepted_settings = before
        self.c._accepted_source = source
        self.c._settling_options = self.c._options()
        for minute in (5, 31):
            latest = replace(before, now=NOW+timedelta(minutes=minute), battery_soc_percent=85.)
            live = build_joint_input(latest, OPTIONS)
            moved = selected(live, {len(live.slots)-1: 1.})
            self.assertEqual(moved.slots[0].action, 'self_use')
            proof = RceActiveCommitment('rce:confirmed', NOW, NOW+timedelta(hours=2), latest.now, 40., 20.)
            self.rce._active_rce_commitment = lambda now: proof
            self.c._calculated_at = None
            with patch.object(m.dt_util, 'utcnow', return_value=latest.now), \
                 patch.object(self.c, '_input', return_value=(live, self.metadata, latest, str(minute))), \
                 patch.object(m, 'optimize', return_value=moved):
                await self.c.recalculate()
            self.assertTrue(self.rce._attributes['result_current'], self.rce._attributes)
            self.assertTrue(self.rce._attributes['active_slot_commitment_applied'])
            self.assertTrue(self.rce._attributes['current_slot_continue_eligible'])
            self.assertEqual(self.rce._attributes['solver_method'], 'active_slot_fixed_schedule')
            self.assertEqual(self.rce._attributes['joint_plan_revision'], self.tariff._attributes['joint_plan_revision'])
            self.assertEqual(self.c._active_run_basis[0], proof.transaction_id)
            self.assertIs(self.c._active_run_basis[4][1], original_plan)

    async def test_real_publication_keeps_original_confirmed_buy_basis(self):
        from test_pstryk_buy_commitment import fixture, remaining
        from custom_components.hoymiles_hit_modbus import pstryk_joint as J
        from custom_components.hoymiles_hit_modbus.tariff_optimizer import TariffActiveCommitment
        await self.pstryk()
        before_data,original,_=fixture()
        before=replace(settings(),inverter_power_kw=10.,inverter_ac_power_kw=10.)
        self.c._accepted=(before_data,original,10.)
        self.c._accepted_settings=before
        self.c._accepted_source=(self.c.profile.revision,self.c.cache.view(now=NOW).snapshot.revision)
        self.c._settling_options=self.c._options()
        for minute in (5,31,65):
            live=remaining(before_data,minute); latest=replace(before,now=live.slots[0].start)
            points,cost=J.simulate(live,(0,)*len(live.slots))
            moved=replace(original,slots=points,cost_pln=cost,action_levels=(0,)*len(points))
            proof=TariffActiveCommitment('tariff:confirmed','grid_support',NOW,
                NOW+timedelta(minutes=90),52.,50.,latest.now)
            self.tariff._active_tariff_commitment=lambda now:proof
            self.c._calculated_at=None
            with patch.object(m.dt_util,'utcnow',return_value=latest.now), \
                 patch.object(self.c,'_input',return_value=(live,self.metadata,latest,str(minute))), \
                 patch.object(m,'optimize',return_value=moved):
                await self.c.recalculate()
            self.assertTrue(self.tariff._attributes['result_current'])
            self.assertTrue(self.tariff._attributes['active_slot_commitment_applied'])
            self.assertTrue(self.tariff._attributes['current_run_continue_eligible'])
            self.assertEqual(self.tariff._attributes['current_grid_charge_run_end'],proof.hard_deadline.isoformat())
            self.assertEqual(self.c._active_buy_basis[0],proof.transaction_id)
            self.assertIs(self.c._active_buy_basis[4][1],original)

    async def test_price_failure_keeps_verified_day_but_mixed_profile_cannot_fallback(self):
        await self.pstryk(); await self.c.recalculate()
        self.c.cache.failed('invalid_response'); await self.c.recalculate()
        self.assertTrue(self.rce._attributes['result_current'])
        self.assertTrue(self.c.active)
        self.hass.states.async_set(m.PURCHASE,'PGE'); await self.c.recalculate()
        self.assertFalse(self.tariff._attributes['result_current'])

    async def test_missing_current_hour_cannot_execute_next_hour(self):
        await self.pstryk()
        original=self.prices()
        frames=[p.as_row() for p in original.hours if not p.start<=NOW<p.end]
        missing=parse_prices({'frames':frames},source_scope=self.c.scope,fetched_at=NOW,
            window_start=original.window_start,window_end=original.window_end)
        self.c.cache = m.PriceCache(self.c.scope)
        with self.assertRaises(ValueError):
            self.c.cache.accept(missing,now=NOW)
        await self.c.recalculate()
        self.assertFalse(self.rce._attributes['result_current'])
        self.assertEqual(self.rce._attributes['pstryk_blocker'],'not_loaded')

    async def test_bad_inputs_and_zero_bms(self):
        await self.pstryk()
        self.settings=replace(self.settings,bms_max_charge_current_a=0.)
        await self.c.recalculate()
        self.assertFalse(self.tariff._attributes['current_slot_planned'])
        self.settings=replace(self.settings,battery_voltage_v=0.)
        await self.c.recalculate()
        self.assertFalse(self.tariff._attributes['result_current'])

    async def test_bounded_gcf_delivery_preserves_current_pair_like_rce(self):
        await self.pstryk(); await self.c.recalculate()
        revision=self.c._revision
        self.rce._forecast_gcf_policy_evaluation_cancel=lambda:None
        await self.c.recalculate()
        self.assertTrue(self.rce._attributes['result_current'])
        self.assertTrue(self.tariff._attributes['result_current'])
        self.assertEqual(self.c._revision,revision,'Mixed cohort cannot publish a new candidate')
        self.c.invalidate('gcf_readback_incoherent')
        await self.c.recalculate()
        self.assertFalse(self.rce._attributes['result_current'])
        self.assertFalse(self.tariff._attributes['result_current'])

    async def test_physical_cohort_wait_rechecks_inputs_before_publication(self):
        await self.pstryk()
        calls=[]
        async def wait_cohort():
            calls.append(1)
            self.settings=replace(self.settings,bms_max_discharge_current_a=0.)
            return True
        self.rce._async_wait_active_rce_commitment_cohort=wait_cohort
        await self.c.recalculate()
        self.assertTrue(calls)
        self.assertFalse(self.rce._attributes['current_slot_planned'])
        self.assertEqual(self.rce._attributes['bms_discharge_power_limit_kw'],0.)

    async def test_same_revision_invalidation_withdraws_both_real_timelines(self):
        from test_automation_plan_timeline import (
            _load_timeline_runtime_module, _runtime_timeline_sensor, trace,
        )
        await self.pstryk(); await self.c.recalculate()
        module, base = _load_timeline_runtime_module()
        timelines = []
        for role, source in (('rce', self.rce), ('tariff', self.tariff)):
            sensor, owner, _ = _runtime_timeline_sensor(module, base, policy_id=role)
            owner._timeline_sensor.publish_current(trace(role), input_revision=1)
            source._timeline_sensor = owner._timeline_sensor
            timelines.append((sensor, sensor.extra_state_attributes['points']))
        self.c.invalidate('input_changed_during_solve')
        for sensor, points in timelines:
            self.assertEqual(sensor.native_value, 'pending')
            self.assertFalse(sensor.extra_state_attributes['result_current'])
            self.assertEqual(sensor.extra_state_attributes['input_revision'], 1)
            self.assertEqual(sensor.extra_state_attributes['points'], points)
        self.assertIsNone(self.c.market_fingerprint())

    async def test_slow_solve_age_starts_at_fresh_revalidation_not_old_capture(self):
        await self.pstryk()
        solve = m.optimize
        fresh_at = NOW + timedelta(seconds=45)
        with patch.object(m.dt_util, 'utcnow', return_value=NOW) as clock:
            def slow_solve(data):
                plan = solve(data)
                self.settings = replace(self.settings, now=fresh_at)
                clock.return_value = fresh_at
                return plan
            with patch.object(m, 'optimize', side_effect=slow_solve):
                await self.c.recalculate()
            self.assertTrue(self.rce._attributes['result_current'])
            self.assertEqual(self.c._calculated_at, fresh_at)
            clock.return_value = fresh_at + timedelta(seconds=120)
            self.assertIsNotNone(self.c.market_fingerprint())
            clock.return_value += timedelta(seconds=1)
            self.assertIsNone(self.c.market_fingerprint())

    async def test_unfinished_physical_cohort_cannot_publish_new_plan(self):
        await self.pstryk()
        async def still_pending():
            return False
        self.rce._async_wait_active_rce_commitment_cohort=still_pending
        await self.c.recalculate()
        self.assertFalse(self.rce._attributes['result_current'])
        self.assertEqual(self.c._revision,0)
        self.assertIsNone(self.c._accepted)
        self.assertEqual(self.rce._attributes['pstryk_blocker'],'input_cohort_pending')

    async def test_active_commitment_coalescer_wait_is_bounded_and_yields(self):
        from custom_components.hoymiles_hit_modbus.rce_sensor import HoymilesRCEOptimizerSensor
        probe=SimpleNamespace(_active_commitment_cohort_source=lambda:True, _lifecycle_stopped=False)
        wait=HoymilesRCEOptimizerSensor._async_wait_active_rce_commitment_cohort
        task=asyncio.create_task(wait(probe))
        await asyncio.sleep(.01)
        self.assertFalse(task.done(),'The pending cohort must yield to HA callbacks')
        self.assertFalse(await asyncio.wait_for(task,1.))
        probe._active_commitment_cohort_source=lambda:False
        self.assertTrue(await wait(probe))
        probe._active_commitment_cohort_source=lambda:True
        probe._lifecycle_stopped=True
        self.assertFalse(await wait(probe))

    async def test_gcf_delivery_cannot_retain_expired_or_foreign_market(self):
        await self.pstryk(); await self.c.recalculate()
        self.c._calculated_at=NOW-timedelta(seconds=121)
        self.rce._forecast_gcf_policy_evaluation_cancel=lambda:None
        await self.c.recalculate()
        self.assertFalse(self.rce._attributes['result_current'])

    async def test_gcf_delivery_cannot_mask_settings_or_unload(self):
        for cause in ('settings','unload','profile','delay_profile','price'):
            self.rce._forecast_gcf_policy_evaluation_cancel=None
            self.rce._lifecycle_stopped=False
            await self.pstryk(); self.c.invalidate(); await self.c.recalculate()
            self.assertTrue(self.rce._attributes['result_current'])
            self.rce._forecast_gcf_policy_evaluation_cancel=lambda:None
            if cause=='settings':
                self.hass.states.async_set(m.OPTION_HELPERS['minimum_saving'],'1.0')
            elif cause=='unload': self.rce._lifecycle_stopped=True
            elif cause=='profile': self.hass.states.async_set(m.PURCHASE,'PGE')
            elif cause=='delay_profile': self.hass.states.async_set(m.DELAY_PROFILE_HELPER,'Maximum')
            else: self.c.cache = m.PriceCache(self.c.scope)
            await self.c.recalculate()
            self.assertFalse(self.rce._attributes['result_current'],cause)

    async def test_delay_profile_requires_valid_preference_without_blocking_buy_sell(self):
        await self.pstryk()
        self.hass.states.async_set(m.DELAY_HELPER,'on')
        options = []
        for value in ('Conservative','Balanced','Maximum','unavailable'):
            self.hass.states.async_set(m.DELAY_PROFILE_HELPER,value)
            row = self.c._options()
            self.assertEqual(row['allow_delay'], value != 'unavailable')
            self.assertTrue(row['allow_buy'])
            self.assertTrue(row['allow_sell'])
            options.append(row)
        self.assertNotEqual(options[0], options[1])
        self.assertNotEqual(options[1], options[2])

    async def test_live_drift_is_revalidated_without_another_search(self):
        await self.pstryk()
        real=self.hass.async_add_executor_job; calls=[]
        async def changing(func,*args):
            result=await real(func,*args)
            if func is optimize:
                calls.append(1)
                self.settings=replace(self.settings,now=NOW+timedelta(seconds=3),
                    battery_soc_percent=20.1,current_load_power_kw=1.04,battery_voltage_v=51.25)
            return result
        with patch.object(self.hass,'async_add_executor_job',side_effect=changing):
            await self.c.recalculate()
        self.assertTrue(self.rce._attributes['result_current'])
        self.assertEqual(len(calls),1)
        self.assertAlmostEqual(self.tariff._attributes['battery_soc_percent'],20.1)
        self.assertEqual(self.c._accepted[0].slots[0].start,self.settings.now)
        from custom_components.hoymiles_hit_modbus.pstryk_plan import execution_metadata
        self.assertEqual(self.rce._attributes['bms_discharge_power_limit_kw'],
                         execution_metadata(self.settings)['bms_discharge_power_limit_kw'])
        self.assertNotEqual(self.rce._attributes['bms_discharge_power_limit_kw'],
                            execution_metadata(settings())['bms_discharge_power_limit_kw'])

    async def test_live_samples_coalesce_but_settings_replan_immediately(self):
        await self.pstryk(); await self.c.recalculate()
        revision=self.c._revision
        for i in range(6):
            self.settings=replace(self.settings,current_load_power_kw=1.+i/100)
            await self.c.recalculate()
        self.assertEqual(self.c._revision,revision)
        self.c._calculated_at=NOW-timedelta(seconds=120)
        await self.c.recalculate()
        self.assertEqual(self.c._revision,revision+1)
        self.hass.states.async_set(m.PERMISSIONS[0],'off')
        self.c.invalidate('settings_changed');await self.c.recalculate()
        self.assertEqual(self.c._revision,revision+2)
        self.assertFalse(self.tariff._attributes['current_slot_planned'])

    async def test_periodic_refresh_precedes_pv_authorization_expiry(self):
        from custom_components.hoymiles_hit_modbus.pv_charge_delay import PvDelayCommitment
        from custom_components.hoymiles_hit_modbus.pv_charge_delay_control import candidate
        await self.pstryk()
        self.c.sensor.publish = lambda: None
        prices = [PriceSlot(NOW+timedelta(minutes=30*i), .9 if i<4 else .1) for i in range(25)]
        pv = {p.start:3. if p.start.hour<15 else 0. for p in prices}
        self.settings = replace(self.settings, price_slots=prices,
            pv_by_slot_kwh=pv, conservative_pv_by_slot_kwh=pv, delay_pv_by_slot_kwh=pv,
            battery_wear_cost_pln_kwh=10., average_daily_load_kwh=5., average_night_load_kwh=2.,
            current_load_power_kw=.5, current_pv_power_kw=5., battery_soc_percent=70.)
        self.metadata.update(forecast_today_raw_kwh=40., forecast_today_p10_kwh=30.)
        self.hass.states.async_set(m.DELAY_HELPER, 'on')
        self.hass.states.async_set(m.OPTION_HELPERS['maximum_soc'], '100')
        await self.c.recalculate()
        original = self.c._accepted[1].delay_plan
        self.assertIsNotNone(original)
        self.rce._active_pv_delay_commitment = lambda now: PvDelayCommitment('pv:confirmed', NOW, original.end)
        # Timer phase from the field: its 30 s tick can arrive just BEFORE
        # the cache age threshold, leaving another whole tick before refresh.
        for seconds in (29.8, 59.8, 89.8, 119.8):
            at = NOW+timedelta(seconds=seconds)
            self.settings = replace(self.settings, now=at)
            with patch.object(m.dt_util, 'utcnow', return_value=at):
                await self.c._refresh()
        at = NOW+timedelta(seconds=120.176)
        with patch.object(m.dt_util, 'utcnow', return_value=at):
            self.assertIsNotNone(self.c.market_fingerprint(), 'Accepted authority expired between normal ticks')
        attrs = self.rce._attributes
        snapshot = supervisor.RceSourceSnapshot(observed_at=self.c._calculated_at,
            allowed_by_user=True, enabled=True, active_latched=True,
            status_code=supervisor.RcePlanStatus.READY, result_current=attrs['result_current'],
            recalculation_pending=attrs['recalculation_pending'], input_revision=attrs['input_revision'],
            pv_charge_hold=True, pv_charge_hold_qualified=True,
            current_slot_planned=attrs['pv_charge_delay_execution_ready'],
            current_run_end=datetime.fromisoformat(attrs['pv_charge_delay_end']),
            current_soc_percent=70., latched_minimum_soc_percent=71.,
            active_4305_readback_percent=71., active_4306_readback_percent=1.,
            sale_block_active=False, system_power_kw=5.)
        self.assertTrue(candidate(snapshot, now=at).continuation_eligible)
        self.assertEqual(self.c._accepted[1].delay_plan.end, original.end)
        self.assertEqual(self.c._accepted[1].delay_plan.recovered_at, original.recovered_at)
        self.assertEqual(attrs['joint_plan_revision'], self.tariff._attributes['joint_plan_revision'])

    async def test_refresh_failure_does_not_extend_accepted_authority(self):
        await self.pstryk(); await self.c.recalculate()
        self.c.sensor.publish = lambda: None
        accepted_at, revision = self.c._calculated_at, self.c._revision
        for seconds in (89.8, 119.8, 120.176):
            at = NOW+timedelta(seconds=seconds)
            self.settings = replace(self.settings, now=at)
            self.metadata['soc_data_fresh'] = False
            with patch.object(m.dt_util, 'utcnow', return_value=at):
                await self.c._refresh()
                self.assertIsNone(self.c.market_fingerprint())
            self.assertEqual(self.c._calculated_at, accepted_at)
            self.assertEqual(self.c._revision, revision)
            self.assertFalse(self.rce._attributes['result_current'])
            self.assertFalse(self.tariff._attributes['result_current'])

    async def test_native_rce_tariff_timers_and_price_mirror_do_not_withdraw_pair(self):
        from custom_components.hoymiles_hit_modbus.rce_sensor import HoymilesRCEOptimizerSensor
        from custom_components.hoymiles_hit_modbus.tariff_sensor import HoymilesTariffOptimizerSensor
        await self.pstryk(); await self.c.recalculate()
        revision=self.c._revision
        self.rce._pstryk=self.tariff._pstryk=self.c
        self.rce._invalidate_internal_inputs=lambda: self.fail('Pstryk mirror invalidated its own source')
        HoymilesRCEOptimizerSensor._async_tariff_price_changed(self.rce)
        await asyncio.gather(HoymilesRCEOptimizerSensor._async_timer(self.rce,NOW),
                             HoymilesTariffOptimizerSensor._async_timer(self.tariff,NOW))
        self.assertTrue(self.rce._attributes['result_current'])
        self.assertEqual(self.c._revision,revision)


    async def test_worker_cannot_commit_previous_profile(self):
        await self.pstryk()
        real=self.hass.async_add_executor_job
        started=asyncio.Event(); release=asyncio.Event()
        async def delayed(func,*args):
            if func is optimize:
                started.set(); await release.wait()
            return await real(func,*args)
        with patch.object(self.hass,'async_add_executor_job',side_effect=delayed):
            task=asyncio.create_task(self.c.recalculate()); await started.wait()
            self.hass.states.async_set(m.SALE,'RCE'); await self.c._select(m.SALE)
            release.set(); await task
        self.assertIsNone(self.c._accepted)

    async def test_semantic_input_drift_discards_worker_result(self):
        await self.pstryk()
        real=self.hass.async_add_executor_job; calls=[]
        async def changed(func,*args):
            result=await real(func,*args)
            if func is optimize:
                calls.append(1)
                self.settings=replace(self.settings,safety_margin_soc_percent=2.+len(calls))
            return result
        with patch.object(self.hass,'async_add_executor_job',side_effect=changed): await self.c.recalculate()
        self.assertIsNone(self.c._accepted)
        self.assertFalse(self.rce._attributes['result_current'])

    async def test_save_failure_withdraws_authority(self):
        await self.pstryk(); await self.c.recalculate()
        self.c._saved=None
        async def failing(_data): raise OSError('simulated full disk')
        with patch.object(self.c.store,'async_save',side_effect=failing):
            await self.c.recalculate()
        self.assertFalse(self.c.storage_ready)
        self.assertFalse(self.rce._attributes['result_current'])

    async def test_unload_drains_owned_workers(self):
        await self.pstryk()
        self.c._spawn(asyncio.sleep(60))
        await self.c.close()
        self.assertFalse(self.c._tasks)
        self.assertFalse(self.rce._attributes['result_current'])

    async def test_missing_net_and_utc_fold_compatibility(self):
        await self.pstryk()
        rows=compatibility_rows(self.prices(),m.dt_util.UTC)
        self.assertEqual(len(rows),192)
        self.assertEqual(len({r['dtime_utc'] for r in rows}),192)

    async def test_both_timelines_use_same_soc_and_validate(self):
        await self.pstryk(); await self.c.recalculate()
        data,plan,system=self.c._accepted
        traces=[timeline(data,plan,role,system) for role in ('rce','tariff')]
        self.assertEqual([p.soc_percent for p in traces[0].points],[p.soc_percent for p in traces[1].points])
        for trace in traces:
            build_current_payload(trace,config_entry_id='entry',generated_at=NOW,input_revision=1,plan_revision=1,
                plan_entity_id='sensor.plan',current_actual=CurrentActualSnapshot(NOW,0.,1.,0.,1.,20.,'complete',{}),
                sources=[],physical_active=False)

    async def test_supervisor_rejects_torn_and_aba_pair(self):
        await self.pstryk(); await self.c.recalculate()
        a,b=self.rce._attributes,self.tariff._attributes
        for broken in ({**b,'joint_plan_revision':99},{**b,'joint_profile_revision':99},
                       {**b,'price_provider':'RCE'},{**b,'result_current':False}):
            self.assertFalse(paired_plan_current(a,broken,pstryk_selected=True,pstryk_bound=True))
        self.assertFalse(paired_plan_current(a,b,pstryk_selected=True,pstryk_bound=False))
        self.assertTrue(paired_plan_current({}, {},pstryk_selected=False,pstryk_bound=False))

if __name__=='__main__': unittest.main()
