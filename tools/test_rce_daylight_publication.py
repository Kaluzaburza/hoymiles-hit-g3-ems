"""Real RCE input adapter: daylight clock drift must not invalidate a solve."""
from __future__ import annotations

import asyncio
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import importlib

import test_supervisor_sensor_contract as stub
from test_rce_plan_churn import production_rce_module

TZ = ZoneInfo('Europe/Warsaw')
NOW = datetime(2026, 9, 27, 9, 16, tzinfo=TZ)
PV = 'sensor.hoymiles_hit_pv_total_energy_today'


def fixture():
    # The generic Supervisor harness stubs these; use both actual parsers here.
    stub._load('custom_components.hoymiles_hit_modbus.energy_data', stub.COMPONENT/'energy_data.py')
    stub._load('custom_components.hoymiles_hit_modbus.pv_forecast_usability', stub.COMPONENT/'pv_forecast_usability.py')
    m = production_rce_module()
    m.Store = lambda *args, **kwargs: SimpleNamespace()
    clock = [NOW]
    m.dt_util.now = lambda: clock[0]
    m.dt_util.parse_datetime = datetime.fromisoformat
    m.dt_util.UTC = timezone.utc
    m.get_astral_event_date = lambda _hass, event, day: datetime(
        day.year, day.month, day.day, 6 if event == 'sunrise' else 18,
        44 if event == 'sunrise' else 40, tzinfo=TZ)
    values = {}

    def put(entity, value, attrs=None, at=NOW):
        values[entity] = SimpleNamespace(state=str(value), attributes=attrs or {},
            last_reported=at, last_updated=at, last_changed=at)

    for suffix, value in {
        'battery_capacity':15, 'overview_battery_soc':10,
        'ems_self_use_soc_readback':10, 'ems_force_discharge_soc_readback':60,
        'number_of_machines_master_and_slave':1,
        'maximum_discharge_current':100, 'maximum_charge_current':100,
        'battery_voltage_bms':50, 'gcf_enable_readback_code':1,
        'gcf_maximum_export_power_readback':100, 'gcf_control_readback_generation':12,
        'ems_verified_hardware_readback_supported':1,
        'overview_pv_total_power':2000, 'pv_total_energy_today':2,
    }.items():
        put('sensor.hoymiles_hit_'+suffix, value)
    for suffix, value in {'requested_discharge_power':100,'soc_safety_margin':10,
                          'export_efficiency':95,'fallback_daily_load':17}.items():
        put('input_number.hoymiles_rce_'+suffix,value)
    put('sensor.hoymiles_actual_load_power', 1500)
    put(m.EMS_INVERTER_RATED_POWER_HELPER,'10 kW')
    put('input_datetime.hoymiles_sale_block_start','00:00:00')
    put('input_datetime.hoymiles_sale_block_end','00:00:00')
    put('sun.sun','above_horizon', {'next_rising':'2026-09-28T06:44:00+02:00',
                                 'next_setting':'2026-09-27T18:40:00+02:00'})
    midnight=NOW.replace(hour=0, minute=0)
    rows=[{'dtime_utc':(midnight+timedelta(minutes=15*(i+1))).astimezone(timezone.utc).isoformat(),
           'business_date':NOW.date().isoformat(), 'rce_pln':500} for i in range(96)]
    put('sensor.hoymiles_rce_day',500,{'value':rows})
    details=[{'period_start':(midnight+timedelta(minutes=30*i)).isoformat(),
              'pv_estimate':2.0 if 12 <= i < 36 else 0.0,
              'pv_estimate10':1.0 if 12 <= i < 36 else 0.0} for i in range(48)]
    put(m.TODAY_FORECAST_CANDIDATES[0],24,{'detailedForecast':details,'estimate10':12})
    put(m.REMAINING_TODAY_CANDIDATES[0],20,{'detailedForecast':details})

    class States:
        def get(self,key): return values.get(key)
        def is_state(self,key,value):
            item=values.get(key)
            return item is not None and item.state==value

    hass=SimpleNamespace(states=States(),config=SimpleNamespace(time_zone='Europe/Warsaw',language='pl'))
    source=m.HoymilesRCEOptimizerSensor(hass,SimpleNamespace(entry_id='daylight-test',options={},data={}),None)
    return m,source,clock,values,put


async def main():
    m,source,clock,values,put=fixture()
    opt=importlib.import_module('custom_components.hoymiles_hit_modbus.rce_optimizer')
    before,meta=source._optimizer_input()
    assert before is not None,meta.get('missing_entities')
    assert meta['forecast_learning_enabled'] is True
    assert meta['forecast_live_confidence'] > 0
    clock[0]+=timedelta(milliseconds=50)
    after,metadata=source._optimizer_input()
    assert after is not None
    changed=[f.name for f in fields(before) if getattr(before,f.name)!=getattr(after,f.name)]
    immutable=[n for n in changed if n not in opt.RCE_REVALIDATION_LIVE_FIELDS]
    assert not immutable, f'Clock-only drift changed immutable optimizer input: {immutable}'
    captured=opt.optimize_rce(before)
    fresh=opt.revalidate_rce_plan(after,captured,captured_settings=before)
    assert captured.status_code=='home_energy_shortage'
    assert fresh is not None and fresh.status_code=='home_energy_shortage'
    assert not fresh.planned_exports and fresh.current_run_end is None

    # Exercise the full production async calculation/publication, with an actual
    # input provider on both sides of the await and no state change at all.
    publications=[]
    source.async_write_ha_state=lambda: publications.append(dict(source._attributes))
    async def executor(fn,*args):
        result=fn(*args)
        clock[0]+=timedelta(milliseconds=50)
        await asyncio.sleep(0)
        return result
    source.hass.async_add_executor_job=executor
    await source._recalculate_and_write()
    assert source._attributes['result_current'] is True,source._attributes
    assert source._attributes['recalculation_pending'] is False
    assert source._attributes['status_code']=='home_energy_shortage'
    assert source._attributes['execution_input_valid'] is False
    assert source._attributes['execution_blocker_code']=='home_energy_shortage'
    print('PASS real daylight input adapter + async publication: 50ms drift, current negative result, zero execution authority')

    # A newly reported PV counter really changes the adaptive input. It must
    # still invalidate a captured forecast; the strict revalidator is unchanged.
    put(PV,3,at=clock[0])
    changed,_=source._optimizer_input()
    assert changed.pv_by_slot_kwh!=after.pv_by_slot_kwh
    assert opt.revalidate_rce_plan(changed,captured,captured_settings=before) is None
    assert opt.revalidate_rce_plan(replace(after,bms_max_discharge_current_a=float('nan')),
        captured,captured_settings=before) is None
    print('PASS new PV telemetry and invalid BMS still reject captured result')

    # Old, undated, future, previous-day and invalid counters cannot drive
    # adaptive learning. Fallback is stable; current telemetry still uses now.
    for value,at in [('2',NOW-timedelta(seconds=301)),('2',None),
                     ('2',NOW.replace(tzinfo=None)),
                     ('2',NOW+timedelta(seconds=10)),('2',NOW-timedelta(days=1)),
                     ('nan',NOW),('unavailable',NOW),('-1',NOW)]:
        put(PV,value,at=at)
        _,diagnostics=source._optimizer_input()
        assert diagnostics['forecast_live_confidence']==0.0,(value,at,diagnostics['forecast_live_confidence'])
    put(PV,0,at=NOW)
    _,zero_diagnostics=source._optimizer_input()
    assert zero_diagnostics['forecast_live_confidence'] > 0.0
    print('PASS 8 invalid PV observation guards; valid zero preserved; no protection limits relaxed')


if __name__=='__main__':
    asyncio.run(main())
