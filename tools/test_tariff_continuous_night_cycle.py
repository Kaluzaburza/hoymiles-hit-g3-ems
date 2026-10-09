"""Real tariff optimizer/arbiter/controller/lease client; modeled I/O only.

Controlled night input and ideal full-block/physical feedback. This is neither
a replay of missing field inputs nor live HA/ESP acceptance.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import json
from unittest.mock import patch
from zoneinfo import ZoneInfo

import test_tariff_pending_dispatch_race as h
from test_tariff_optimizer import settings
from tariff_optimizer import optimize_tariff_charging, TariffActiveCommitment
from test_supervisor_control_lease import FirmwareLeaseModel
from test_tariff_initial_lease_settling import ack

a, f = h.adapter, h.fixture

async def run(hour, minute, initial_soc):
    local_start = datetime(2026, 10, 9, hour, minute, tzinfo=ZoneInfo('Europe/Warsaw'))
    start = local_start.astimezone(ZoneInfo('UTC'))
    clock = [start]
    sent = []
    async def persist(_): pass
    async def dispatch(write): sent.append(write)
    ctl = f.SupervisorActiveController(persist=persist, dispatch=dispatch,
        publish=lambda _:None, clock=lambda:clock[0])
    await ctl.async_initialize()
    _, _, _, sensor = a.environment()
    sensor._pause_state = 'off'
    sensor._controller = ctl
    client = sensor._control_lease_client
    epoch = client.journal_snapshot()['journal_epoch']
    esp = FirmwareLeaseModel()
    loop = asyncio.get_running_loop()
    mono = loop.time()
    soc = initial_soc
    base = settings(local_start, battery_capacity_kwh=26, battery_soc_percent=soc,
        current_load_power_kw=.5, current_pv_power_kw=0, current_battery_power_kw=0)
    plan = optimize_tariff_charging(base)
    assert plan.current_slot_planned, (start, plan.current_action)
    source = f.execution_source(start, battery_soc_percent=soc)
    await ctl.async_reconcile(f.optimizer_frame(start, source, plan, input_revision=1))
    tx = ctl.record.transaction
    assert tx is not None and len(sent) == 1, (ctl.record, plan.current_action,
        plan.current_run_start_eligible, plan.current_run_continue_reason,
        f.optimizer_frame(start, source, plan, input_revision=1).decision)
    original_id, deadline = tx.transaction_id, tx.deadline
    assert deadline == local_start.replace(hour=6, minute=0), deadline
    original_first_command = tx.command_sent_at
    original_snapshot = tx.command_snapshot
    end_second = int((deadline-start).total_seconds())
    applied = 0
    completed_plans = []
    generations = []
    modeled_charge_kwh = 0.

    def transport(elapsed):
        nonlocal applied
        while applied < len(sent):
            write = sent[applied]
            block = a.SENSOR._control_lease_block(write.ems_block)
            applied += 1
            if int(block[0]) == 0:
                client.invalidate()
                esp.fallback = block
                esp.state = 'restoring'
                esp.observe(block)
            else:
                request = client.prepare_arm(transaction_id=original_id, hard_deadline=deadline,
                    block=block, challenge_nonce=f'{applied:08x}', now_wall=clock[0], now_monotonic=mono+elapsed)
                assert esp.arm(block, transaction=original_id, generation=request['command_generation'],
                    hard_seconds=(deadline-clock[0]).total_seconds())
                esp.observe(block)
                assert client.accept_arm(request, ack('accepted'), now_monotonic=mono+elapsed)
    transport(0)
    revision = 1
    for elapsed in range(5, end_second+611, 5):
        clock[0] = start+timedelta(seconds=elapsed)
        a.CLOCK['now'] = clock[0]
        esp.advance(5)
        block = esp.physical
        # Ideal, bounded actuator model: charge up to its actual encoded target;
        # otherwise serve 0.5 kW household demand directly from the grid.
        charging_kw = min(6., max(0., block[3]-soc)*26/100*3600/5) if block[0] == 4 else 0.
        added = charging_kw*5/3600
        modeled_charge_kwh += added
        soc += added/26*100
        source = replace(f.physical(clock[0], target=block[3], power=block[4], mode=int(block[0]),
            soc=soc, battery=-charging_kw*1000, generation=100+elapsed),
            self_use_soc_percent=block[1], backup_soc_percent=block[2],
            force_discharge_soc_percent=block[5], maximum_discharge_power_percent=block[6],
            load_power_w=500, grid_power_w=-500-charging_kw*1000, power_cohort_complete=True)
        if elapsed % 120 == 0 or elapsed == end_second:
            transaction = ctl.record.transaction
            commitment = None
            if transaction and ctl.record.state is f.ActiveState.EXECUTING:
                action = transaction.intent.action.value.removeprefix('tariff_')
                commitment = TariffActiveCommitment(original_id, action, start, deadline,
                    block[3], block[4], clock[0])
            plan = optimize_tariff_charging(replace(base, now=clock[0].astimezone(ZoneInfo('Europe/Warsaw')), battery_soc_percent=soc,
                current_battery_power_kw=-charging_kw, active_commitment=commitment))
            revision += 1
            completed_plans.append(dict(at=clock[0].isoformat(), action=plan.current_action,
                end=str(plan.current_grid_charge_run_end), stabilized=plan.active_commitment_applied))
        frame = f.optimizer_frame(clock[0], source, plan, ctl.record, input_revision=revision)
        sensor._latest_active_frame = frame
        await ctl.async_reconcile(frame)
        transport(elapsed)
        if elapsed < end_second:
            record = ctl.record
            assert record.state in {f.ActiveState.EXECUTING, f.ActiveState.RETARGETING}, (elapsed, record)
            assert record.transaction.transaction_id == original_id
            assert record.transaction.deadline == deadline and esp.deadline == end_second
            assert record.transaction.command_snapshot == original_snapshot
            assert all(w.ems_block.mode.value == 4 for w in sent)
            generations.append(source.full_block_generation)
            with patch.object(loop, 'time', return_value=mono+elapsed):
                evidence = sensor._control_lease_renewal_evidence()
            if evidence is None:
                # A newly armed retarget still needs its own newer FC03. No
                # renewal is due during these first modeled five seconds.
                assert client.handle is not None
                assert mono+elapsed-client.handle.last_accepted_monotonic < 20, (elapsed, sensor._control_lease_gate)
                continue
            request = client.prepare_renew(authorized=True, snapshot_generation=source.full_block_generation,
                now_wall=clock[0], now_monotonic=mono+elapsed,
                authorization_deadline=sensor._control_lease_authorization_deadline)
            if request:
                assert esp.renew(authorized=True, sequence=request['sequence'],
                    authorization_seconds=request['authorization_seconds'])
                assert client.accept_renew(ack('renewed', f'{elapsed+100:08x}', int((esp.expiry-esp.now)*1000)),
                    request=request, now_monotonic=mono+elapsed)
        elif elapsed in (end_second+120, end_second+610):
            assert ctl.record.state is f.ActiveState.IDLE, ctl.record
            assert ctl.record.owner is f.ExecutionOwner.NONE and ctl.record.transaction is None
            assert client.handle is None and esp.state == 'disarmed' and esp.physical[0] == 0
    last = ctl.record.last_transaction
    assert last.transaction_id == original_id and last.deadline == deadline
    assert last.reason.value == 'deadline_reached', last
    assert last.rollback_result.value == 'confirmed'
    assert len(generations)>2 and len(set(generations))==len(generations)
    assert len(completed_plans)>=3
    journal = client.journal_snapshot()
    assert journal['journal_epoch'] == epoch and journal['retention_complete'] and journal['dropped_events'] == 0
    events = [event for page in journal['pages'] for event in page]
    assert [e['ordinal'] for e in events] == list(range(1, len(events)+1))
    assert all(e['gap_before'] is None and e['request_correlated'] for e in events)
    assert {e['transaction_id'] for e in events} == {original_id}
    assert {e['hard_deadline'] for e in events} == {deadline.isoformat()}
    for generation in {e['command_generation'] for e in events}:
        group = [e for e in events if e['command_generation'] == generation]
        assert group[0]['kind'] == 'arm'
        assert [e['sequence'] for e in group] == list(range(len(group)))
    print('PASS', json.dumps(dict(scope='OFFLINE_MODEL', start=str(start), deadline=str(deadline),
        first_command=str(original_first_command), duration_seconds=end_second,
        postflight_seconds=610, completed_replans=len(completed_plans),
        active_replans=sum(datetime.fromisoformat(p['at'])<deadline for p in completed_plans), fresh_fc03=len(generations),
        commands=len(sent), accepted_events=len(events), modeled_charge_kwh=modeled_charge_kwh,
        ending_soc=soc, natural_restore=str(last.restore_sent_at)), default=str), flush=True)
    print('PLANS', json.dumps(completed_plans), flush=True)
    print('JOURNAL', json.dumps(journal), flush=True)

if __name__ == '__main__':
    asyncio.run(run(5, 0, 30.))
