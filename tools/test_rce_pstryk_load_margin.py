"""Observed LOAD values through production planner/publication/helpers/controller.

Synthetic qualified forecast/market/capabilities and a deterministic firmware
model isolate the recorded mechanism. This is not an exact whole-site replay.
No optimizer result or execution eligibility/settling flag is fabricated.
"""
from pathlib import Path
import asyncio, json, sys, socket, hashlib, os
from datetime import timedelta
from dataclasses import replace
from types import SimpleNamespace
from datetime import datetime

ROOT=Path(os.environ.get('REPLAY_ROOT',str(Path(__file__).resolve().parents[1])))
sys.path.insert(0,str(ROOT/'tools'))
sys.dont_write_bytecode=True
def offline(event,args):
    if event=='socket.connect':
        caller=sys._getframe(1)
        if caller.f_code.co_name=='_fallback_socketpair' and caller.f_code.co_filename==socket.__file__:
            return
    if event in {'socket.connect','socket.getaddrinfo','socket.gethostbyname','socket.sendto'}:
        raise RuntimeError('OFFLINE_NETWORK_BLOCKED:'+event)
sys.addaudithook(offline)

from test_rce_publication_continuity import Continuity, fixture, h, NOW, LOAD, PV, VOLTAGE

CASES={
 '1730':(2.405,31.524,.120,97.,.85857),
 '1800':(.916,31.266,.024,92.,.752455),
 '1830':(.718,31.347,0.,91.,.820125),
 'full_budget':(2.405,33.120,.120,97.,.85857),
 'stable':(2.405,2.405,.120,97.,.85857),
}

class Replay(Continuity):
    def __init__(self,name,family='rce',spike_window=(15,60)):
        self.name=name
        self.family=family
        self.spike_window=spike_window
        self.low,self.high,self.pv,self.soc,self.price=CASES[name]
        self.milestones=[]
        self.first_stop=None

    async def setup(self):
        original=fixture.solver_probe
        async def configured(case):
            result=await original(case)
            hass,entry,runtime,supervisor,source,module,renderer,solves=result
            provider=source._optimizer_input
            def inputs():
                data,metadata=provider()
                prices=[replace(p,price_pln_kwh=self.price if i==0 else .1)
                        for i,p in enumerate(data.price_slots)]
                data=replace(data,price_slots=prices,battery_capacity_kwh=230.,
                    battery_soc_percent=self.soc,outage_reserve_soc_percent=20.,
                    safety_margin_soc_percent=0.,manual_minimum_soc_percent=20.,
                    dynamic_reserve_enabled=False,average_daily_load_kwh=0.,
                    average_night_load_kwh=0.,inverter_power_kw=16.,inverter_count=2,
                    inverter_ac_power_kw=20.,discharge_power_percent=100.,
                    minimum_net_export_power_kw=2.,bms_max_discharge_current_a=2000.,
                    battery_voltage_v=50.,bms_discharge_data_fresh=True,
                    bms_discharge_data_available=True,bms_discharge_data_age_seconds=0.,
                    current_battery_soc_fresh=True,export_power_cap_kw=40.,
                    effective_export_power_kw=40.,conservative_pv_by_slot_kwh={},
                    current_load_power_kw=self.low,current_pv_power_kw=self.pv)
                return data,{**metadata,'current_price_pln_kwh':self.price,
                    'bms_discharge_data_fresh':True,'bms_discharge_data_available':True,
                    'bms_discharge_data_age_seconds':0.}
            source._optimizer_input=inputs
            source.attach_supervisor_commitment_source(supervisor)
            if self.family=='pstryk':
                sys.modules['homeassistant.core'].Context=lambda:SimpleNamespace(id='offline')
                M=h._load('custom_components.hoymiles_hit_modbus.pstryk_runtime',h.COMPONENT/'pstryk_runtime.py')
                from custom_components.hoymiles_hit_modbus import pstryk_plan as P
                M.Store=lambda *args:None
                tariff=SimpleNamespace(entity_id=h._source_entity_id(h.SENSOR._SOURCE_BY_KEY['tariff_plan'],entry.entry_id),
                    _attributes={},_result=None,_timeline_sensor=None,_input_revision=SimpleNamespace(value=0))
                tariff.async_write_ha_state=lambda:hass.fire_state(tariff.entity_id,
                    h.FakeState('ready',dict(tariff._attributes),h.CLOCK['now']))
                source.entity_id=h._source_entity_id(h.SENSOR._SOURCE_BY_KEY['rce_plan'],entry.entry_id)
                runtime.source_device.id='offline-fixture'
                coordinator=M.PstrykRuntime(hass,entry,runtime,source,tariff)
                coordinator.profile=coordinator.profile.select_purchase('PGE',tariff='G12w').select_sale('Pstryk')
                coordinator.initialized=coordinator.storage_ready=True
                for entity in (M.SALE,M.PURCHASE):
                    hass.states.values[entity]=h.FakeState('Pstryk',reported=h.CLOCK['now'])
                options=dict(charge_efficiency=95.,charge_power_percent=60.,maximum_soc=100.,
                    minimum_saving=.01,demand_margin_percent=0.,allow_buy=True,allow_sell=True)
                coordinator._options=lambda:options
                coordinator.cache=SimpleNamespace(view=lambda **kw:SimpleNamespace(snapshot=SimpleNamespace(
                    revision='offline-public-net',at=lambda now:True)))
                async def save():pass
                coordinator._save=save
                def joint_input():
                    settings,metadata=source._optimizer_input()
                    data=P.build_joint_input(settings,options)
                    return data,{**metadata,'pv_charge_delay_planner_settling_active':False},settings,P.input_key(data,
                        profile_revision=coordinator.profile.revision,price_revision='offline-public-net')
                coordinator._input=joint_input
                source._pstryk=coordinator
                self.coordinator=coordinator
                hass.async_create_task=lambda coro,name=None:asyncio.create_task(coro,name=name)
                real_recalculate=source._recalculate_and_write
                async def initial_recalculate():
                    await real_recalculate()
                    # The RCE fixture reads only this slot-end property after
                    # initialization. Pstryk correctly publishes _result=None;
                    # restore it immediately after that fixture-only read.
                    source._result=SimpleNamespace(current_slot_end=datetime.fromisoformat(source._attributes['current_slot_end']))
                source._recalculate_and_write=initial_recalculate
                self.real_recalculate=real_recalculate
                supervisor.attach_rce_settling_source(coordinator.market_fingerprint,coordinator.recalculate)
            else:
                supervisor.attach_rce_settling_source(
                    lambda: source._result.post_command_settling_market_fingerprint,
                    source.async_recalculate_post_command_settling)
            return result
        fixture.solver_probe=configured
        try: await super().setup()
        finally: fixture.solver_probe=original
        if self.family=='pstryk':
            self.source._result=None
            self.source._recalculate_and_write=self.real_recalculate
        # The accepted command may span several slots. Model the automation's
        # latch using the accepted run deadline, not the first slot boundary.
        self.hass.states.values[self.eid('rce_latched_slot_end')]=h.FakeState(
            'ignored',{'timestamp':self.original_deadline.timestamp()},h.CLOCK['now'])
        self.mark('initial_arm')

    async def executor(self,function,*args):
        result=function(*args)
        future=self.loop.create_future()
        observation={'started':self.second,'due':self.second+4,'function':function.__name__,
            'load_kw':getattr(args[0],'current_load_power_kw',None),
            'reports_at_start':len(self.telemetry)}
        self.solver_observations.append(observation)
        self.jobs.append((self.second+4,future,result,observation))
        return await future

    def direct(self,entity_id,value):
        # Identical initial physical limits in the virtual entity source and
        # the immutable input sent to the real optimizer.
        if entity_id==LOAD: value=self.low*1000
        elif entity_id==PV: value=self.pv*1000
        elif entity_id==VOLTAGE or entity_id.endswith('_battery_voltage_bms'): value=50.
        elif entity_id.endswith('_overview_battery_soc'): value=self.soc
        elif entity_id.endswith('_maximum_discharge_current'): value=2000.
        super().direct(entity_id,value)

    def battery(self):
        spike=self.spike_window[0]<=self.second<self.spike_window[1]
        self.load_kw=self.high if spike else self.low
        self.pv_kw=self.pv
        block=self.services.oracle.physical
        discharge_kw=32.*block[6]/100. if int(block[0])==5 else 0.
        # Recorded phenomenon: a near-budget LOAD pulse while actual
        # battery discharge and grid export remain positive. No balance
        # threshold is bypassed in the production controller.
        grid_kw=max(discharge_kw-self.low+self.pv,0.)
        for key,value in (
            ('battery_soc',self.soc),('bms_voltage',50.),
            ('bms_max_charge_current',2000.),('bms_max_discharge_current',2000.),
            ('grid_power',grid_kw*1000),('battery_power',discharge_kw*1000),
            ('load_power',self.load_kw*1000),('pv_power',self.pv*1000)):
            self.hass.fire_state(self.eid(key),h.FakeState(str(value),reported=h.CLOCK['now']))
        for eid,value in (
            ('sensor.hoymiles_hit_overview_battery_soc',self.soc),
            ('sensor.hoymiles_hit_maximum_discharge_current',2000.),
            (VOLTAGE,50.),(LOAD,self.load_kw*1000),(PV,self.pv*1000)):
            self.hass.fire_state(eid,h.FakeState(str(value),reported=h.CLOCK['now']))
        self.telemetry.append({'at':self.second,'load_kw':self.load_kw,'pv_kw':self.pv,
            'battery_kw':discharge_kw,'grid_export_kw':grid_kw})

    def mark(self,label):
        a=self.source._attributes
        r=self.controller.record
        tx=r.transaction or r.last_transaction
        self.milestones.append({'at':self.second,'label':label,'state':r.state.value,
            'reason':r.reason.value,'transaction':tx.transaction_id if tx else None,
            'deadline':tx.deadline if tx else None,'plan':{k:a.get(k) for k in (
            'result_current','recalculation_pending','input_revision',
            'full_plan_solver_calls','last_full_plan_at','current_slot_planned',
            'current_slot_start_eligible','current_slot_continue_eligible',
            'current_slot_suppression_reason','current_slot_planned_export_kwh',
            'current_slot_load_exhausts_requested_discharge_budget',
            'current_slot_load_only_export_suppressed',
            'current_slot_shared_discharge_limit_kwh','active_slot_commitment_applied')},
            'arm_calls':self.services.arms,'restore_calls':len(self.services.restore_calls),
            'renewals':len(self.services.renewals),
            'physical_generation':self.generation,
            'physical_block':self.services.oracle.physical})

    async def step(self,second):
        # Same public fixture scheduler without its expectation of success:
        # stopped/restored is an outcome to preserve, not to hide.
        delta=second-self.second
        h.CLOCK['now']=NOW+timedelta(seconds=second)
        self.services.oracle.advance(delta)
        if second==1 or second%5==1: self.ems()
        if second==1 or second%3==0: self.battery()
        if second==1:
            self.hass.fire_state(self.eid('rce_active'),h.FakeState('on',reported=h.CLOCK['now']))
        if second%20==7: self.settings()
        if second in (20,65,120,150,180,240):
            self.spawn(self.source._async_timer(h.CLOCK['now']))
        for job in tuple(self.jobs):
            due,future,result,observation=job
            if due<=second:
                observation.update(completed=second,reports_at_end=len(self.telemetry))
                if not future.done(): future.set_result(result)
                self.jobs.remove(job)
        self.renderer.publish_all(h.CLOCK['now'])
        await self.settle()
        self.renderer.publish_all(h.CLOCK['now'])
        await self.settle()
        if second in (2,6,14,20,24,25,26,31,59,65,70,120,150,180,240,270):
            self.mark('sample')
        if self.services.restore_calls and self.first_stop is None:
            self.first_stop=second
            self.mark('first_restore')

async def main():
    case=sys.argv[1]
    target=Path(sys.argv[2])
    assert not target.exists(),target
    window=tuple(map(int,sys.argv[4].split(':'))) if len(sys.argv)>4 else (15,60)
    p=Replay(case,sys.argv[3] if len(sys.argv)>3 else 'rce',window)
    status='ERROR'
    try:
        await p.setup()
        for second in range(1,271):
            await p.step(second)
            if p.first_stop is not None and second>=p.first_stop+8:
                break
        assert p.milestones[1]['state']=='active_executing'
        assert p.milestones[2]['physical_generation']>p.milestones[1]['physical_generation']
        assert p.milestones[1]['physical_block']==p.milestones[2]['physical_block']
        assert all(m['deadline']==p.original_deadline for m in p.milestones)
        assert all(m['transaction']==p.original_tx for m in p.milestones)
        assert p.first_stop is None, p.first_stop
        status='COMPLETED_OBSERVATION'
    finally:
        if hasattr(p,'loop'): p.loop.time=p.real_loop_time
        for task in getattr(p,'tasks',[]):
            if not task.done():task.cancel()
        await asyncio.gather(*getattr(p,'tasks',[]),return_exceptions=True)
        data={'case':case,'family':p.family,'spike_window_s':window,'status':status,'base_sha':'164508b353806a2cd66c56a41567bba5dc2c203f','source':str(ROOT),
            'test_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'virtual_clock_anchor':NOW.isoformat(),
            'desired_stability_status':'RED' if p.first_stop is not None else 'PASS_CONTROL',
            'scope':'Production solver/publication/Jinja/HA adapter/controller/lease client; simulated clock, I/O, firmware and qualified market inputs. Not live acceptance.',
            'observed_powers':CASES[case],'first_restore_s':p.first_stop,
            'milestones':p.milestones,'publications':getattr(p,'publications',[]),
            'jobs':getattr(p,'solver_observations',[]),
            'telemetry':getattr(p,'telemetry',[])}
        if hasattr(p,'services'):
            data.update(arms=p.services.arm_requests,renewals=p.services.renewals,
                restore_calls=p.services.restore_calls,stop_decisions=p.controller._stop_decisions)
        target.write_text(json.dumps(data,indent=2,default=str),encoding='utf-8')
        print(json.dumps({'status':status,'family':p.family,'case':case,'first_restore_s':p.first_stop,
            'desired_stability_status':data['desired_stability_status']},default=str))

if __name__=='__main__':
    if len(sys.argv)>1:
        asyncio.run(main())
    else:
        import subprocess, tempfile
        with tempfile.TemporaryDirectory() as folder:
            checks=[(family,case,window) for family,window in [('rce','15:60'),('pstryk','55:100')]
                    for case in CASES]
            checks.append(('pstryk','1730','15:60'))
            for family,case,window in checks:
                output=Path(folder)/(family+'_'+case+'_'+window.replace(':','_')+'.json')
                subprocess.run([sys.executable,'-B','-X','utf8',__file__,case,str(output),family,window],check=True)
        print('PASS 11 full-stack LOAD margin/controls; original transaction/deadline, FC03 and renewals')
