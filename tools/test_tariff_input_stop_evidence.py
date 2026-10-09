"""A fresh independent FC03/power frame must not hide rejected planner LOAD."""
import asyncio, json
from dataclasses import replace
from datetime import timedelta
import test_tariff_pending_dispatch_race as h
a,f=h.adapter,h.fixture

async def main():
    ctl,sent,_,clock=await f.confirmed_controller()
    clock[0]=f.NOW+timedelta(seconds=240)
    attrs=dict(current_run_continue_reason='live_data_missing',
        current_live_power_sampled_at=clock[0].isoformat(),
        current_live_power_max_age_seconds=120.,current_live_power_shared_revision=71,
        current_live_load_power_kw=None,current_live_load_power_age_seconds=121.,current_live_load_power_source='stale',
        current_live_pv_power_kw=0.,current_live_pv_power_age_seconds=1.,current_live_pv_power_source='live',
        current_live_battery_power_kw=0.,current_live_battery_power_age_seconds=1.,current_live_battery_power_source='live')
    probe=h.Replay();probe.now=clock[0];probe.physical(generation=51)
    probe.plan('tariff_plan',**attrs)
    parts=probe.sensor._build_snapshots(probe.sensor._read_source_states(),clock[0])
    evidence=parts[3].live_power_input_evidence
    assert evidence['channels']['load']['reason']=='stale'
    assert evidence['channels']['load']['age_seconds']==121.
    assert evidence['channels']['load']['maximum_age_seconds']==120.
    assert evidence['shared_revision']==71
    fresh=f.physical(clock[0],generation=52)
    frame=f.frame(clock[0],fresh,ctl.record,current_run_continue_eligible=False,
        current_run_continue_reason='live_data_missing',live_power_input_evidence=evidence)
    tx=ctl.record.transaction.transaction_id
    await ctl.async_reconcile(frame)
    stops=ctl.recorder_attributes()['recent_stop_decisions']
    assert stops[0]['reason']=='authorization_lost'
    assert stops[0]['transaction_id']==tx
    saved=stops[0]['tariff_live_power_input']
    assert saved==evidence
    frozen=json.dumps(saved,sort_keys=True)
    # Recovery cannot retroactively replace the actual rejected input.
    attrs['current_live_load_power_kw']=.39
    attrs['current_run_continue_reason']='eligible'
    probe.plan('tariff_plan',**attrs)
    assert probe.sensor._build_snapshots(probe.sensor._read_source_states(),clock[0])[3].live_power_input_evidence is None
    assert json.dumps(ctl.recorder_attributes()['recent_stop_decisions'][0]['tariff_live_power_input'],sort_keys=True)==frozen
    assert any(int(w.ems_block.mode)==0 for w in sent)
    print('PASS exact planner sample -> HA snapshot -> immutable first STOP; physical freshness does not grant missing planner authority')

if __name__=='__main__':asyncio.run(main())
