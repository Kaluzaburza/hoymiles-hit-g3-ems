"""Offline STOP evidence through the real controller; no relaxed freshness."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import sys

# Run this exact regression against the predecessor for RED evidence.
ROOT = Path(os.environ.get('FOLLOWUP_SOURCE', Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(ROOT/'tools'))
import test_supervisor_active_controller as f
from supervisor_active_bridge import settings_from_execution_source
from supervisor_executor import AtomicWriteFamily

NOW = f.NOW
DEADLINE = NOW + timedelta(minutes=30)
RESULTS = []

def sample(at, **changes):
    return f.execution_source(at, physical_mode_code=5, full_block_generation=11,
        force_discharge_soc_percent=41., maximum_discharge_power_percent=25.,
        grid_power_w=1200., grid_power_observed_at=at-timedelta(seconds=.5),
        battery_power_w=900., battery_power_observed_at=at-timedelta(seconds=.5),
        **changes)

def frame(at, source, *, active=True):
    return f.rce_frame(at, source, deadline=DEADLINE, floor=41., power=25.,
                       active=active, slot_end=DEADLINE)

async def running():
    writes=[]
    async def persist(record): pass
    async def dispatch(write): writes.append(write)
    c=f.SupervisorActiveController(persist=persist, dispatch=dispatch,
        publish=lambda record: None, clock=f.ScriptedClock(NOW+timedelta(seconds=.1)))
    await c.async_initialize()
    await c.async_reconcile(frame(NOW, f.execution_source(NOW), active=False))
    await c.async_reconcile(frame(NOW+timedelta(seconds=2), sample(NOW+timedelta(seconds=2))))
    assert c.record.state is f.ActiveState.EXECUTING
    return c,writes

async def case(name, changes, error=None, channel=None, limit=None):
    c,writes=await running()
    at=NOW+timedelta(seconds=240)
    source=replace(sample(at), **changes(at))
    tx=c.record.transaction.transaction_id
    await c.async_reconcile(frame(at,source))
    if error is None:
        assert c.record.state is f.ActiveState.EXECUTING, c.record.reason
        assert len(writes)==1 and not c.recorder_attributes()['recent_stop_decisions']
        return {'case':name,'status':'PASS','writes':len(writes)}
    stops=c.recorder_attributes()['recent_stop_decisions']
    assert stops and stops[0]['reason']=='stale_inputs', (c.record.state,c.record.reason)
    stop=stops[0]
    evidence=stop.get('input_freshness')
    assert evidence is not None, 'STOP drops the channel, observed timestamp, age and applied limit'
    assert datetime.fromisoformat(evidence['checked_at'])==at
    assert error in evidence['errors'], evidence
    assert stop['transaction_id']==tx and datetime.fromisoformat(stop['deadline'])==DEADLINE
    if channel:
        cohort=evidence['cohorts'][channel]
        assert cohort['maximum_age_seconds']==limit, cohort
        assert cohort['required'] is True
    frozen=json.dumps(stop,sort_keys=True)
    # Recovery sampling must not rewrite the original STOP evidence.
    await c.async_reconcile(frame(at+timedelta(seconds=1), sample(at+timedelta(seconds=1))))
    assert json.dumps(c.recorder_attributes()['recent_stop_decisions'][0],sort_keys=True)==frozen
    return {'case':name,'status':'PASS','evidence':evidence}

async def main():
    cases=[
        ('unchanged_values_new_generation',lambda at:{'full_block_generation':99},None,None,None),
        ('ordinary_ems_gap_29_999',lambda at:{'full_block_generation_at':at-timedelta(seconds=29.999)},None,None,None),
        ('ems_exclusive_30',lambda at:{'full_block_generation_at':at-timedelta(seconds=30)},'ems_expired','ems',30.),
        ('ems_stale_30_001',lambda at:{'full_block_generation_at':at-timedelta(seconds=30.001)},'ems_stale','ems',30.),
        ('gcf_stale',lambda at:{'gcf_generation_at':at-timedelta(seconds=30.001)},'gcf_stale','gcf',30.),
        ('gcf_incoherent',lambda at:{'gcf_cohort_coherent':False},'gcf_incoherent','gcf',30.),
        ('gcf_unavailable',lambda at:{'gcf_generation_at':None},'gcf_unavailable','gcf',30.),
        ('ems_future',lambda at:{'full_block_generation_at':at+timedelta(seconds=5.001)},'ems_future','ems',30.),
        ('nonrequired_306_missing',lambda at:{'battery_charge_limit_generation_at':None},None,None,None),
        ('ems_decode_failure',lambda at:{'maximum_charge_power_percent':None},'settings_decode_failed',None,None),
    ]
    for args in cases:
        try: RESULTS.append(await case(*args))
        except Exception as exc: RESULTS.append({'case':args[0],'status':'FAIL','error':str(exc)})
    # A fresh FC03 does not refresh an old companion register. Defaults and
    # future/incoherent data remain fail closed independently of the journal.
    at=NOW+timedelta(seconds=240)
    snapshot=settings_from_execution_source(sample(at))
    for age,expected in [(15.,()),(15.001,('ems_stale',)),(-5.,()),(-5.001,('ems_future',))]:
        assert replace(snapshot,ems_observed_at=at-timedelta(seconds=age)).freshness_errors(
            at,required_families=())==expected
    assert replace(snapshot,ems_coherent=False).freshness_errors(at,required_families=())==('ems_incoherent',)
    assert replace(snapshot,battery_observed_at=at-timedelta(seconds=31)).freshness_errors(
        at,required_families=(AtomicWriteFamily.BATTERY_CHARGE_LIMIT,))==('battery_306_stale',)
    print(json.dumps({'cases':RESULTS,'pure_boundary_checks':6},indent=2))
    assert all(row['status']=='PASS' for row in RESULTS), 'STOP evidence regression failed'

if __name__=='__main__': asyncio.run(main())
