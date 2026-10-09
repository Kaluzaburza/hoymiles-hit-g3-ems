"""Production forecast adapters must not calibrate adaptive Solcast twice."""
from __future__ import annotations
import asyncio
import copy
import sys
from datetime import timedelta
from types import SimpleNamespace
import test_rce_daylight_publication as daylight


def adapted(state):
    state.attributes['undampened_estimate']=48.0
    for row in state.attributes['detailedForecast']:
        row['dampening_factor']=0.5


async def main():
    m,rce,clock,values,put=daylight.fixture()
    m.dt_util.as_utc=lambda stamp: stamp.astimezone(daylight.timezone.utc)
    today=values[m.TODAY_FORECAST_CANDIDATES[0]]
    adapted(today)
    rce._forecast_accuracy_factor=0.6
    rce._forecast_accuracy_days=8
    before,meta=rce._optimizer_input()
    assert before is not None,meta.get('missing_entities')
    assert meta['forecast_today_effective_factor']==1.0,meta['forecast_today_effective_factor']
    assert meta['forecast_live_confidence']==0.0
    assert meta['forecast_learning_mode']=='solcast_adaptive'
    assert abs(sum(before.pv_by_slot_kwh.values())-20.0)<1e-8
    original_map=before.pv_by_slot_kwh
    rce._forecast_accuracy_factor=0.2
    unchanged,_=rce._optimizer_input()
    assert unchanged.pv_by_slot_kwh==original_map
    print('PASS RCE adapted forecast: 20kWh remains 20kWh, dormant historical factor ignored')

    # Actual Solcast refresh changes the production input; marker alone is not
    # another multiplier. Preserve each half-hour's already corrected shape.
    values[m.REMAINING_TODAY_CANDIDATES[0]].state='10'
    for row in today.attributes['detailedForecast']:
        row['dampening_factor']=0.25
        row['pv_estimate']*=0.5
    newer,_=rce._optimizer_input()
    assert abs(sum(newer.pv_by_slot_kwh.values())-10.0)<1e-8
    assert newer.pv_by_slot_kwh!=original_map

    # Full production tariff adapter, with synthetic HA observations. The
    # shared source fixture supplies only its required aggregate AC rating.
    sys.modules['homeassistant.const'].MATCH_ALL='*'
    daylight.stub._module('homeassistant.helpers.restore_state',
        RestoreEntity=type('RestoreEntity',(),{}))
    t=daylight.stub._load('custom_components.hoymiles_hit_modbus.tariff_sensor',
        daylight.stub.COMPONENT/'tariff_sensor.py')
    t.get_astral_event_date=m.get_astral_event_date
    t._shared_inputs_snapshot=lambda _runtime: SimpleNamespace(
        system=SimpleNamespace(system_rated_power_kw=SimpleNamespace(value=10.0,fresh=True)))
    for role,offset in ((m.TOMORROW_FORECAST_CANDIDATES[0],1),(m.DAY3_FORECAST_CANDIDATES[0],2)):
        attrs=copy.deepcopy(today.attributes)
        for row in attrs['detailedForecast']:
            row['period_start']=(daylight.datetime.fromisoformat(row['period_start'])+timedelta(days=offset)).isoformat()
        put(role,12,attrs)
    for name in ('cheap_1_start','cheap_1_end','cheap_2_start','cheap_2_end','medium_start','medium_end'):
        put('input_datetime.hoymiles_tariff_'+name,'00:00:00')
    for name,value in {'charge_efficiency':95,'discharge_efficiency':95,'g11_price':0.7,
        'low_price':0.5,'medium_price':0.7,'peak_price':1.0,'maximum_soc':90,
        'minimum_saving':0.05,'requested_charge_power':100,'soc_safety_margin':10}.items():
        put('input_number.hoymiles_tariff_'+name,value)
    put('input_select.hoymiles_tariff_type','G11')
    put('input_select.hoymiles_tariff_operator',t.MANUAL_OPERATOR)
    tariff=t.HoymilesTariffOptimizerSensor(rce.hass,
        SimpleNamespace(entry_id='daylight-test',options={},data={}),None)
    tariff._forecast_accuracy_factor=0.6
    tariff._forecast_accuracy_days=8
    tariff_input,tariff_meta=tariff._optimizer_input()
    assert tariff_input is not None,tariff_meta['missing_entities']
    assert tariff_meta['forecast_today_effective_factor']==1.0
    assert tariff_meta['forecast_tomorrow_factor_used']==1.0
    assert tariff_meta['forecast_day3_factor_used']==1.0
    assert tariff_meta['forecast_live_confidence']==0.0
    assert tariff_meta['forecast_learning_mode']=='solcast_adaptive'
    tariff._forecast_accuracy_factor=0.2
    same_tariff,_=tariff._optimizer_input()
    assert same_tariff.pv_by_slot_kwh==tariff_input.pv_by_slot_kwh
    # P10 uncertainty reserves are separate from production calibration.
    assert sum(tariff_input.pv_by_slot_kwh.values())>0
    print('PASS production tariff adapter: upstream calibration, retained uncertainty, stable against dormant model')

    queries=[]
    async def history(*args,**kwargs):
        queries.append(args)
        return {}
    m.async_get_bounded_state_reports=history
    t.async_get_bounded_state_reports=history
    await rce._async_refresh_forecast_accuracy(force=True)
    await tariff._async_refresh_forecast_accuracy()
    assert not queries, 'Adaptive upstream source must not trigger a duplicate Recorder calibration scan'

    # Different days can be manually mapped to different sources. Do not
    # transfer today's adaptation authority onto an unadapted tomorrow.
    for row in values[m.TOMORROW_FORECAST_CANDIDATES[0]].attributes['detailedForecast']:
        row.pop('dampening_factor')
    _,mixed=tariff._optimizer_input()
    assert mixed['forecast_factor_used']==1.0
    assert mixed['forecast_tomorrow_factor_used']==0.2
    assert mixed['forecast_day3_factor_used']==1.0

    # Explicit zero export and unverified GCF retain their conservative policy.
    put('sensor.hoymiles_hit_gcf_maximum_export_power_readback',0)
    for source in (rce,tariff):
        _,diag=source._optimizer_input()
        assert diag['forecast_learning_mode']=='fixed_zero_export'
        assert diag['forecast_factor_used']==0.8
    put('sensor.hoymiles_hit_gcf_maximum_export_power_readback',100)
    put('sensor.hoymiles_hit_ems_verified_hardware_readback_supported',0)
    for source in (rce,tariff):
        _,diag=source._optimizer_input()
        assert diag['forecast_learning_mode']=='conservative_gcf_unverified'
        assert diag['forecast_factor_used']<=0.8
    put('sensor.hoymiles_hit_ems_verified_hardware_readback_supported',1)

    # No marker, partial marker, manual undampened metadata, NaN, negative,
    # boolean, string and missing factor all retain the existing EMS path.
    model=sys.modules['custom_components.hoymiles_hit_modbus.forecast_model']
    policy=model.ForecastLearningPolicy(True,'adaptive',None)
    for attributes in ({}, {'undampened_estimate':48}, {'detailedForecast':[]},
        *({'detailedForecast':[{'dampening_factor':v}]} for v in (None,True,'0.5',float('nan'),float('inf'),-0.1)),
        {'detailedForecast':[{'dampening_factor':0.5},{}]}):
        assert model.forecast_policy_for_source(policy,attributes)==policy
    for factor in (0.0,0.5,1.0,1.2):
        assert model.forecast_policy_for_source(policy,
            {'detailedForecast':[{'dampening_factor':factor}]}).factor_override==1.0
    for row in today.attributes['detailedForecast']:
        row.pop('dampening_factor')
    _,plain=rce._optimizer_input()
    assert plain['forecast_learning_enabled'] is True
    assert plain['forecast_today_effective_factor']<1.0
    await rce._async_refresh_forecast_accuracy(force=True)
    await tariff._async_refresh_forecast_accuracy()
    assert len(queries)==2
    print('PASS source mixing, on/off, 10 ambiguous markers, GCF/zero-export precedence and calibration query ownership')


if __name__=='__main__': asyncio.run(main())
