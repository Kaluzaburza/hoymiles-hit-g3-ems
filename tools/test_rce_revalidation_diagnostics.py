"""A rejected fresh plan is not missing source data and names its failed proof."""
import asyncio
from dataclasses import replace
import subprocess
from rc2_regression_baseline import read_source
import sys
from types import SimpleNamespace
from pathlib import Path
from test_rce_daylight_publication import fixture
from test_rce_plan_revalidation import seed, RCE

async def main():
    m, source, clock, values, put = fixture()
    if '--base' in sys.argv:
        root=Path(__file__).resolve().parents[1]
        for mod,name in [(RCE,'rce_optimizer.py'),(m,'rce_sensor.py')]:
            raw=read_source('6484a82',name)
            exec(compile(raw,name,'exec'),mod.__dict__)
        m.Store=lambda *a,**k:SimpleNamespace()
        source=m.HoymilesRCEOptimizerSensor(source.hass,source._entry,None)
    settings,result=seed()
    changed=replace(settings,battery_capacity_kwh=settings.battery_capacity_kwh+1)
    metadata={k:True for k in ('rce_today_data_fresh','forecast_today_data_fresh','soc_data_fresh','gcf_execution_data_fresh')}
    metadata['missing_entities']=[]
    sequence=iter([(settings,metadata),(changed,metadata)])
    source._optimizer_input=lambda:next(sequence)
    source._reject_stale_executor_result=lambda *a:False
    async def executor(fn,*args): return result
    source.hass.async_add_executor_job=executor
    await source._recalculate_locked()
    assert source._attributes['status_code']=='plan_revalidation_failed',source._attributes
    assert source._attributes['missing_entities']==[]
    assert source._attributes['result_current'] is False
    assert source._attributes['execution_input_valid'] is False
    assert source._attributes['planned_slots']==[]
    assert 'weryfikacji' in source.native_value
    # Exercise actual optimizer diagnostics separately from adapter publication.
    d={}
    assert RCE.revalidate_rce_plan(changed,result,captured_settings=settings,diagnostics=d) is None
    assert d=={'changed_fields':['battery_capacity_kwh'],'reason':'immutable_input_changed'},d
    assert RCE.revalidate_rce_plan(replace(settings,bms_discharge_data_fresh=False),result,captured_settings=settings,diagnostics=d) is None
    assert d=={'reason':'live_inputs_not_ready'},d
    assert RCE.revalidate_rce_plan(settings,result,captured_settings=settings,diagnostics=d) is not None
    assert d=={},d
    print('PASS: rejected plan status, changed-field reason, fresh fail-closed inputs and cleared diagnostics')

if __name__=='__main__': asyncio.run(main())
