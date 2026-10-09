"""EV integration on the real HA state machine; no host, network or commands."""
import asyncio
from dataclasses import replace
from collections import deque
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys, tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from homeassistant.core import HomeAssistant
from custom_components.hoymiles_hit_modbus import ev_load_runtime as runtime
from custom_components.hoymiles_hit_modbus.ev_load_filter import ENABLED, POWER, SENSOR
from custom_components.hoymiles_hit_modbus.rce_history import LoadHistorySummary
from custom_components.hoymiles_hit_modbus.rce_sensor import HoymilesRCEOptimizerSensor
from custom_components.hoymiles_hit_modbus import rce_sensor as rce
from custom_components.hoymiles_hit_modbus.load_history_store import merge_history


def history(now, days=28):
    profiles = {(now.date()-timedelta(days=i)).isoformat(): (.5,)*20+(6.,)*4+(.5,)*24 for i in range(1,days+1)}
    raw = LoadHistorySummary(46.,days,{k:46. for k in profiles},10.,days,{k:10. for k in profiles},
        profile_kwh_by_date=profiles, profile_history_days=days, daily_coverage_ratio=1.,profile_coverage_ratio=1.)
    return merge_history(raw,raw,limit=28)


class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.hass.config.time_zone = 'Europe/Warsaw'
        self.now = datetime.now(timezone.utc)
        self.set(ENABLED,'on'); self.set(POWER,'11'); self.set(SENSOR,'sensor.ev')
        self.set('sensor.ev','11000',{'unit_of_measurement':'W'})
        self.ev = runtime.EvLoadRuntime(self.hass)

    async def asyncTearDown(self):
        await self.hass.async_stop()
        self.temp.cleanup()

    def set(self, entity, value, attrs=None):
        self.hass.states.async_set(entity,value,attrs or {},force_update=True)

    def power(self, total=12., at=None):
        now=datetime.now(timezone.utc)
        return self.ev.forecast_power(total,1.,at or now,now)

    async def test_sensor_power_and_invalid_do_not_fall_back(self):
        self.assertEqual(self.power(),1.)
        self.set('sensor.ev','11',{'unit_of_measurement':'kW'})
        self.assertEqual(self.power(),1.)
        for value,unit in [('unavailable','W'),('-10','W'),('11','kWh')]:
            self.set('sensor.ev',value,{'unit_of_measurement':unit})
            self.assertIsNone(self.power())
            self.assertEqual(self.ev.status,'sensor_invalid')
        self.set('sensor.ev','13000',{'unit_of_measurement':'W'})
        self.assertIsNone(self.power())
        self.assertEqual(self.ev.status,'sensor_exceeds_load')
        self.set(SENSOR,'sensor.hoymiles_actual_load_power')
        self.assertIsNone(self.power())
        self.assertEqual(self.ev.status,'invalid_configuration')

    async def test_heuristic_never_subtracts_nominal(self):
        self.set(SENSOR,'')
        self.assertIsNone(self.power())
        self.assertEqual(self.ev.status,'suspected_ev')
        self.assertEqual(self.power(1.7),1.7)
        self.set(ENABLED,'off')
        self.assertEqual(self.power(),12.)

    async def test_fresh_unconfigured_text_uses_heuristic_but_requires_manual_power(self):
        self.set(SENSOR,'unknown')  # Real first-start input_text state in HA.
        self.assertEqual(self.power(1.7),1.7)
        self.assertEqual(self.ev.status,'heuristic')
        self.set(POWER,'0')
        self.assertIsNone(self.power())
        self.assertEqual(self.ev.status,'invalid_configuration')
        self.set(POWER,'11'); self.set(SENSOR,'unavailable')
        self.assertIsNone(self.power())
        self.assertEqual(self.ev.status,'invalid_configuration')

    async def test_stale_future_and_mixed_cohort(self):
        stale = SimpleNamespace(state='11000', attributes={'unit_of_measurement':'W'})
        for offset in (-181,1,-100):
            with patch.object(runtime,'state_reported_at',return_value=self.now+timedelta(seconds=offset)):
                self.assertIsNone(self.ev.forecast_power(12,1,self.now,self.now))
                self.assertEqual(self.ev.status,'sensor_stale')

    async def test_bounded_history_storage_restart_and_identity(self):
        raw = history(self.now)
        calls=[]
        async def query(_hass,start,end,entities,**kwargs):
            calls.append((entities,kwargs))
            return {'sensor.ev':[SimpleNamespace(last_updated=start,state='0')]}
        with patch.object(runtime,'async_get_bounded_state_reports',query):
            for hour in range(72):
                now=self.now+timedelta(hours=hour)
                await self.ev.refresh(raw,now)
                for _ in range(60):
                    self.ev.project(raw); self.ev.payload()
        self.assertLessEqual(len(calls),28)
        self.assertTrue(all(e==('sensor.ev',) and k['limit_per_entity']==10000 and 'sample_interval_seconds' not in k for e,k in calls))
        payload=self.ev.payload()
        self.assertLess(len(json.dumps(payload)),16000)
        restored=runtime.EvLoadRuntime(self.hass); restored.restore(payload)
        self.assertEqual(restored.days,self.ev.days)
        self.set(SENSOR,'sensor.other')
        restored.configure()
        self.assertEqual(restored.days,{})
        restored.restore(payload)
        self.assertEqual(restored.days,{})

    async def test_inflight_aba_and_single_flight(self):
        entered, release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def query(_hass,start,end,entities,**kwargs):
            calls.append(entities); entered.set(); await release.wait()
            return {'sensor.ev':[SimpleNamespace(last_updated=start,state='0')]}
        with patch.object(runtime,'async_get_bounded_state_reports',query):
            task=asyncio.create_task(self.ev.refresh(history(self.now),self.now))
            await entered.wait()
            await self.ev.refresh(history(self.now),self.now)
            self.set(SENSOR,'sensor.other');self.ev.configure()
            self.set(SENSOR,'sensor.ev');self.ev.configure()
            release.set();await task
        self.assertEqual(len(calls),1)
        self.assertEqual(self.ev.days,{})

    async def test_shared_model_filters_history_and_persistence_only(self):
        self.set(SENSOR,'')
        self.set('sensor.hoymiles_actual_load_power','12000',{'unit_of_measurement':'W'})
        self.set('sensor.hoymiles_actual_load_energy_today','36',{'unit_of_measurement':'kWh'})
        self.set('input_number.hoymiles_ems_fallback_daily_home_load','24')
        sensor=HoymilesRCEOptimizerSensor.__new__(HoymilesRCEOptimizerSensor)
        sensor.hass=self.hass
        sensor._extended_load_history=history(self.now)
        sensor._load_history=sensor._extended_load_history
        sensor._load_profile_generated_at=self.now
        sensor._runtime=SimpleNamespace()
        sensor._load_power_observations=deque(maxlen=240)
        sensor._load_persistence_delta_kw=3.
        sensor._load_persistence_observed_at=self.now
        sensor._load_persistence_sample_count=10
        snapshot=sensor.shared_load_model_snapshot(datetime.now(timezone.utc))
        self.assertEqual(snapshot['daily_history_days'],0)
        self.assertEqual(snapshot['model_quality'],'fallback')
        # All days suspect: keep the conservative old demand, not a guessed
        # 24 kWh house, until at least three measured/clean days are available.
        self.assertEqual(snapshot['average_daily_home_load_kwh'],46.)
        self.assertIsNone(snapshot['current_day_energy_kwh'])
        self.assertEqual(snapshot['persistence_delta_kw'],0.)
        self.assertEqual(self.hass.states.get('sensor.hoymiles_actual_load_power').state,'12000')
        self.assertEqual(sensor._extended_load_history.daily_history_days,28)
        self.set(ENABLED,'off')
        off=sensor.shared_load_model_snapshot(datetime.now(timezone.utc))
        self.assertEqual(off['average_daily_home_load_kwh'],46.)
        self.assertEqual(off['current_day_energy_kwh'],36.)
        self.assertEqual(off['daily_history_days'],28)

    async def test_failure_does_not_repeat_queries_each_callback(self):
        calls=[]
        async def broken(*args,**kwargs):
            calls.append(1); raise RuntimeError('Recorder offline')
        with patch.object(runtime,'async_get_bounded_state_reports',broken):
            for minute in range(60):
                await self.ev.refresh(history(self.now),self.now+timedelta(minutes=minute))
        self.assertEqual(len(calls),1)
        self.assertEqual(self.ev.days,{})

    async def test_corrected_shared_profile_and_cache_keep_raw_load(self):
        self.set('sensor.hoymiles_actual_load_power','12000')
        self.set('sensor.hoymiles_actual_load_energy_today','36')
        self.set('input_number.hoymiles_ems_fallback_daily_home_load','24')
        sensor=HoymilesRCEOptimizerSensor.__new__(HoymilesRCEOptimizerSensor)
        sensor.hass=self.hass
        sensor._extended_load_history=history(self.now)
        sensor._load_history=sensor._extended_load_history
        sensor._load_profile_generated_at=self.now
        sensor._runtime=SimpleNamespace()
        sensor._load_power_observations=deque(maxlen=240)
        ev=sensor._ev_filter()
        ev.days={k:(0.,)*20+(5.5,)*4+(0.,)*24 for k in sensor._load_history.daily_energy_kwh}
        snapshot=sensor.shared_load_model_snapshot(datetime.now(timezone.utc))
        self.assertEqual(snapshot['model_quality'],'complete')
        self.assertEqual(snapshot['average_daily_home_load_kwh'],24.)
        self.assertEqual(snapshot['average_profile_30m_kwh'],(.5,)*48)
        self.assertIsNone(snapshot['current_day_energy_kwh'])
        self.assertEqual(sensor._load_power_observations[-1][1],1.)
        self.assertIs(ev.project(sensor._extended_load_history),ev.project(sensor._extended_load_history))
        from unittest.mock import AsyncMock
        from custom_components.hoymiles_hit_modbus.load_history_store import source_identity, decode_cache
        sensor._load_history_identity=lambda: source_identity(entry_id='ev-test', entry_unique_id='ev-test',
            source_device_id='source',resolved_source_device_id='source',timezone='Europe/Warsaw')
        sensor._load_history_store=SimpleNamespace(async_save=AsyncMock())
        sensor._load_history_store_payload=None
        await sensor._async_save_load_history_cache()
        payload=sensor._load_history_store.async_save.call_args.args[0]
        raw,_=decode_cache(payload,expected_identity=sensor._load_history_identity(),empty=rce._empty_load_summary())
        self.assertEqual(set(raw.daily_energy_kwh.values()),{46.})
        self.assertIn('ev_load_filter',payload)
        await sensor._async_save_load_history_cache()
        self.assertEqual(sensor._load_history_store.async_save.call_count,1)

    async def test_pstryk_retains_ev_power_separately_from_household_forecast(self):
        from test_pstryk_runtime import settings
        from custom_components.hoymiles_hit_modbus.pstryk_plan import build_joint_input
        # The same corrected household forecast (1 kW) with physical LOAD 12 kW.
        base=replace(settings(),current_load_power_kw=12.,average_daily_load_kwh=24.)
        options={'maximum_soc':95.,'charge_power_percent':30.,'charge_efficiency':95.,
                 'minimum_saving':.01,'demand_margin_percent':10.,'allow_buy':True,'allow_sell':True}
        joint=build_joint_input(base,options,pv_origin_kwh=0.)
        without_pulse=build_joint_input(replace(base,current_load_power_kw=1.),options,pv_origin_kwh=0.)
        self.assertEqual([p.load_kwh for p in joint.slots],
                         [p.load_kwh for p in without_pulse.slots])
        self.assertEqual(joint.current_load_power_kw,12.)
        self.assertEqual(without_pulse.current_load_power_kw,1.)
        self.assertLess(joint.slots[0].load_kwh,1.)
        self.assertLess(joint.slots[1].load_kwh,1.)

if __name__=='__main__': unittest.main()
