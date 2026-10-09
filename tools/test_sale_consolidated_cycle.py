"""Offline whole-slot SELL: real publication/controller, modeled FC03/transport.

The preceding selection is tested independently with the full production
optimizers. This runs its later whole-slot execution with modeled-energy
feedback, cooperative pending solves, natural deadline and 600 s postflight.
No HA connection, invented eligibility or shortened transaction is used.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import sys

from test_rce_pstryk_load_margin import Replay, CASES, h, NOW
from test_rce_lease_real_cadence import optimizer_fixture


class ConsolidatedCycle(Replay):
    def __init__(self,family):
        CASES['consolidated']=(0.,0.,0.,26.5,.7)
        super().__init__('consolidated',family,spike_window=(-2,-1))
        self.energy_kwh=0.
        self.previous_second=0.
        self.efficiency=1.

    def battery(self):
        elapsed=self.second-self.previous_second
        block=self.services.oracle.physical
        power=32.*block[6]/100. if int(block[0])==5 else 0.
        delivered=power*elapsed/3600.
        self.energy_kwh+=delivered
        self.soc-=delivered/self.efficiency/230.*100.
        self.previous_second=self.second
        super().battery()

    def ems(self):
        oracle=self.services.oracle
        restore=bool(self.services.restore_calls) or oracle.state=='restoring'
        block=oracle.fallback if restore or oracle.state=='disarmed' else oracle.active
        assert block is not None
        oracle.observe(block)
        self.generation+=1
        self.direct('sensor.hoymiles_hit_esp_uptime',1000+self.generation)
        for key,value in zip(('ems_mode_readback','self_use_soc_readback','backup_soc_readback',
                'charge_soc_readback','charge_power_ems_readback','discharge_soc_readback',
                'discharge_power_readback'),block):
            self.report(key,int(value) if key=='ems_mode_readback' else value)
        self.report('ems_generation',self.generation)
        for entity,value in (
                ('sensor.hoymiles_hit_ems_mode_readback_code',int(block[0])),
                ('sensor.hoymiles_hit_ems_control_readback_generation',self.generation),
                ('sensor.hoymiles_hit_ems_force_discharge_soc_readback',block[5]),
                ('sensor.hoymiles_hit_ems_maximum_discharge_power_readback',block[6])):
            self.direct(entity,value)
        if block==oracle.fallback:
            self.hass.fire_state(self.eid('rce_active'),h.FakeState('off',reported=h.CLOCK['now']))


async def run(family):
    p=ConsolidatedCycle(family)
    try:
        await p.setup()
        client=p.supervisor._control_lease_client
        journal_epoch=client.journal_snapshot()['journal_epoch']
        # Whole-optimizer proof on the same stock, caps and future sale. The
        # first candidate has only 15 minutes left; it can be absorbed later.
        settings,_=p.source._optimizer_input()
        p.efficiency=settings.export_efficiency_percent/100.
        R=optimizer_fixture.RCE
        earlier=NOW-timedelta(hours=1)
        before_input=replace(settings,now=earlier+timedelta(minutes=15),price_slots=(
            R.PriceSlot(earlier,.7),R.PriceSlot(earlier+timedelta(minutes=30),.1,True),
            R.PriceSlot(NOW,.7)))
        selection=R.optimize_rce(before_input)
        assert len(selection.planned_exports)==1,selection.planned_exports
        assert selection.planned_exports[0].start==NOW
        deadline_s=int((p.original_deadline-NOW).total_seconds())
        assert deadline_s==1800,deadline_s
        first_command=p.controller.record.transaction.command_sent_at
        assert first_command is not None
        generations=[]
        published_replans={}
        for second in range(1,deadline_s+611):
            if second>240 and second%120==0:
                p.spawn(p.source._async_timer(h.CLOCK['now']))
            await p.step(second)
            record=p.controller.record
            if 6<=second<deadline_s:
                assert record.state in {h.SENSOR.ActiveState.EXECUTING,h.SENSOR.ActiveState.RETARGETING},(
                    second,record.state,record.reason,record.transaction)
                assert record.transaction.transaction_id==p.original_tx
                assert record.transaction.deadline==p.original_deadline
                assert p.services.oracle.deadline==p.original_hard
                assert not p.services.restore_calls
                attrs=p.source._attributes
                if attrs.get('result_current') is True and attrs.get('recalculation_pending') is False:
                    revision=(attrs.get('last_full_plan_at') if family=='rce'
                              else attrs.get('joint_plan_revision'))
                    if revision is not None:
                        published_replans.setdefault(str(revision),dict(at=second,
                            revision=revision,solver_calls=attrs.get('full_plan_solver_calls')))
                if second%5==1:
                    assert p.services.oracle.physical==p.services.oracle.active
                    generations.append(p.generation)
            if second in (deadline_s+120,deadline_s+610):
                assert record.state is h.SENSOR.ActiveState.IDLE,(second,record)
                assert record.owner is h.SENSOR.ExecutionOwner.NONE
                assert record.transaction is None
                assert p.services.oracle.state=='disarmed'
                assert p.services.oracle.physical==p.services.oracle.fallback
                assert p.services.oracle.physical[0]==0
                p.mark('postflight')
        assert p.services.arms==1,p.services.arm_requests
        assert len(generations)>=2 and len(set(generations))==len(generations)
        assert len(p.solver_observations)>=3
        completed=[x for x in p.solver_observations if 'completed' in x]
        assert len(completed)>=3
        assert len(published_replans)>=3,published_replans
        accepted=p.services.renewals
        assert len(accepted)>=80 and all(x[2] for x in accepted),accepted
        assert [x[1] for x in accepted]==list(range(1,len(accepted)+1))
        assert max(b[0]-a[0] for a,b in zip(accepted,accepted[1:]))<=21
        assert p.soc>=20.-1e-6 and p.energy_kwh>14.
        last=p.controller.record.last_transaction
        assert last.transaction_id==p.original_tx
        assert last.reason is h.SENSOR.ExecutionReason.DEADLINE_REACHED,last
        assert last.interruption_reason is h.SENSOR.ExecutionReason.DEADLINE_REACHED,last
        assert last.rollback_result is h.SENSOR.VerificationStatus.CONFIRMED,last
        assert last.deadline==p.original_deadline
        journal=client.journal_snapshot()
        assert client.handle is None and not journal['active_lease']
        assert journal['journal_epoch']==journal_epoch
        assert journal['retention_complete'] and journal['dropped_events']==0
        events=[event for page in journal['pages'] for event in page]
        assert len(events)==journal['retained_events']==len(accepted)+1
        assert [event['ordinal'] for event in events]==list(range(1,len(events)+1))
        assert [event['sequence'] for event in events]==list(range(len(events)))
        assert events[0]['kind']=='arm' and all(e['kind']=='renew' for e in events[1:])
        assert all(e['gap_before'] is None and e['request_correlated'] for e in events)
        assert all(e['transaction_id']==p.original_tx for e in events)
        assert len({e['lease_id'] for e in events})==1
        assert len({e['command_generation'] for e in events})==1
        assert {e['hard_deadline'] for e in events}=={p.original_deadline.isoformat()}
        print('PASS',json.dumps(dict(family=family,scope='OFFLINE_MODEL',
            run_seconds=deadline_s,postflight_seconds=610,arms=p.services.arms,
            completed_solves=len(completed),fresh_fc03=len(generations),accepted_renewals=len(accepted),
            published_replans=list(published_replans.values()),
            deadline=str(p.original_deadline),transaction=p.original_tx,
            model_energy_kwh=p.energy_kwh,model_end_soc=p.soc,
            restore_calls=len(p.services.restore_calls),restore_sent_at=str(last.restore_sent_at),
            final_state=p.controller.record.state.value)),flush=True)
        print('JOURNAL',family,json.dumps(journal),flush=True)
    finally:
        if hasattr(p,'real_loop_time'):
            p.loop.time=p.real_loop_time
        for task in getattr(p,'tasks',()):
            if not task.done():task.cancel()
        await asyncio.gather(*getattr(p,'tasks',()),return_exceptions=True)


async def main():
    for family in ('rce','pstryk'):
        await run(family)

if __name__=='__main__':asyncio.run(main())
