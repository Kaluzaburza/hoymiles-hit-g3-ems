"""Production HA gates/client with independent ESP expiry, no host I/O."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

import test_tariff_pending_dispatch_race as h
from test_supervisor_control_lease import FirmwareLeaseModel
a, f = h.adapter, h.fixture


def ack(reason, nonce='00000002', remaining=120000):
    return dict(schema_version=1, protocol_version=2, soft_remaining_ms=remaining, maximum_lease_seconds=120, renew_interval_seconds=20, accepted=True, reason=reason, renew_nonce=nonce)


async def scenario(effect_at, veto=None, *, initial=False):
    _, _, _, sensor = a.environment()
    sensor._pause_state = 'off'
    if initial:
        clock = [f.NOW]
        async def persist(_): pass
        async def dispatch(_): pass
        ctl = f.SupervisorActiveController(persist=persist, dispatch=dispatch,
            publish=lambda _:None, clock=lambda:clock[0])
        await ctl.async_initialize()
    else:
        ctl, _, _, clock = await f.confirmed_controller()
    sensor._controller = ctl
    clock[0] = f.NOW + timedelta(seconds=3)
    frame = f.frame(clock[0], f.execution_source(clock[0]) if initial else f.physical(clock[0], generation=12), ctl.record,
        target=65, power=60, action=f.TariffAction.GRID_SUPPORT_AND_CHARGE)
    await ctl.async_reconcile(frame)
    tx = ctl.record.transaction
    anchor = clock[0]
    loop = asyncio.get_running_loop()
    mono = loop.time()
    client = sensor._control_lease_client
    block = a.SENSOR._control_lease_block(tx.intent.command.ems_block)
    arm = client.prepare_arm(transaction_id=tx.transaction_id, hard_deadline=tx.deadline,
        block=block, challenge_nonce='00000001', now_wall=anchor, now_monotonic=mono)
    assert client.accept_arm(arm, ack('accepted'))
    esp = FirmwareLeaseModel()
    assert esp.arm(block, transaction=tx.transaction_id, generation=1, hard_seconds=1800)
    esp.observe(block)
    renewals = 0
    for i, elapsed in enumerate(sorted(set([5., 10., 20., 29., 30., 30.222421, 45., 59., 60., 90., 119., 120., 140., 160., 175., 179., 180.]))):
        clock[0] = anchor + timedelta(seconds=elapsed)
        a.CLOCK['now'] = clock[0]
        src = f.physical(clock[0], target=65, battery=-5000 if elapsed >= effect_at else 0, generation=13+i)
        src = replace(src, power_cohort_complete=True)
        args = {}
        if veto == 'bms': src = replace(src, bms_max_charge_current_a=0)
        if veto == 'incomplete': src = replace(src, power_cohort_complete=False)
        if veto == 'stale': src = replace(src, grid_power_observed_at=anchor-timedelta(seconds=60))
        if veto == 'export': src = replace(src, critical_grid_power_w=500, critical_grid_power_observed_at=clock[0])
        if veto == 'battery_discharge': src = replace(src, battery_power_w=1000, grid_power_w=-200)
        if veto == 'wrong_block': src = replace(src, force_charge_soc_percent=66)
        if veto == 'missing_marker': src = replace(src, power_cohort_complete=None)
        if veto == 'fc03': src = replace(src, full_block_generation_at=anchor-timedelta(seconds=1))
        if veto == 'permission': args['allowed_by_user'] = False
        if veto == 'slot': args['current_slot_planned'] = False
        if veto == 'pending': args.update(result_current=False, recalculation_pending=True)
        frame = f.frame(clock[0], src, ctl.record, target=65, power=60,
            action=f.TariffAction.GRID_SUPPORT_AND_CHARGE, **args)
        sensor._latest_active_frame = frame
        if veto == 'pause': sensor._pause_state = 'on'
        if veto == 'stop': sensor._master_stop_latched = True
        # Establish the real ACK first. Otherwise every negative case would
        # trivially fail on the missing readback rather than its own veto.
        if veto and i == 0:
            clean = replace(f.physical(clock[0], target=65, generation=13), power_cohort_complete=True)
            await ctl.async_reconcile(f.frame(clock[0], clean, ctl.record,
                target=65, power=60, action=f.TariffAction.GRID_SUPPORT_AND_CHARGE))
        if veto is None: await ctl.async_reconcile(frame)
        esp.advance(elapsed-esp.now)
        with patch.object(loop, 'time', return_value=mono+elapsed):
            evidence = sensor._control_lease_renewal_evidence()
        if veto:
            assert evidence is None, (veto, elapsed)
            break
        if elapsed < min(effect_at, 179):
            assert ctl.record.state is (f.ActiveState.WAITING_READBACK if initial else f.ActiveState.RETARGETING)
            assert evidence is not None, (elapsed, sensor._control_lease_gate)
            assert ctl.record.transaction.physical_verification.status is not a.SENSOR.VerificationStatus.CONFIRMED
        if elapsed >= 179 and elapsed < effect_at:
            assert evidence is None, 'pending phase cannot move the fixed 180 s effect limit'
        request = client.prepare_renew(authorized=evidence is not None, snapshot_generation=13+i,
            now_wall=clock[0], now_monotonic=mono+elapsed,
            authorization_deadline=sensor._control_lease_authorization_deadline)
        if request:
            assert esp.state == 'confirmed', 'ESP must not have independently expired'
            assert esp.renew(authorized=True, sequence=request['sequence'], authorization_seconds=request['authorization_seconds'])
            assert client.accept_renew(ack('renewed', f'{100+i:08x}', int((esp.expiry-esp.now)*1000)), request=request, now_monotonic=mono+elapsed+.001)
            renewals += 1
        if elapsed >= effect_at:
            assert ctl.record.state is f.ActiveState.EXECUTING
            break
    if veto is None and effect_at < 180:
        assert (renewals > 0 or effect_at < 20) and esp.state == 'confirmed'
    if veto is None and effect_at >= 180:
        assert esp.state == 'restoring'
        assert ctl.record.state is not f.ActiveState.EXECUTING
    print('PASS bounded physical settling', effect_at, veto, 'initial' if initial else 'retarget')


async def expired_first_renew():
    _, _, _, sensor = a.environment()
    client = sensor._control_lease_client
    for elapsed in (120., 120.222421, 180.):
        arm = client.prepare_arm(transaction_id='tariff:expired', hard_deadline=f.NOW+timedelta(minutes=30),
            block=(4.,25.,90.,65.,60.,0.,100.), challenge_nonce='00000001', now_wall=f.NOW, now_monotonic=100.)
        assert client.accept_arm(arm, ack('accepted'))
        assert client.prepare_renew(authorized=True, snapshot_generation=13,
            now_wall=f.NOW+timedelta(seconds=elapsed), now_monotonic=100.+elapsed) is None
        assert client.handle is None
    print('PASS no first renewal at or after soft expiry')


async def rejection_timing():
    _, _, _, sensor = a.environment()
    client = sensor._control_lease_client
    arm = client.prepare_arm(transaction_id='tariff:rejected', hard_deadline=f.NOW+timedelta(minutes=30),
        block=(4.,25.,90.,65.,60.,0.,100.), challenge_nonce='00000001', now_wall=f.NOW, now_monotonic=100.)
    assert client.accept_arm(arm, ack('accepted'))
    request = client.prepare_renew(authorized=True, snapshot_generation=13,
        now_wall=f.NOW+timedelta(seconds=20), now_monotonic=120.)
    response = dict(schema_version=1, protocol_version=2, soft_remaining_ms=120000, maximum_lease_seconds=120, renew_interval_seconds=20, accepted=False, reason='stale_or_invalid_nonce')
    result = client.process_renew_response(response, request=request, now_monotonic=120.1)
    sensor._record_control_lease_result(status=result.status.value, reason=result.reason,
        request=request, requested_at=f.NOW+timedelta(seconds=20), lost=True,
        first_send_monotonic=120., observed_monotonic=120.1)
    assert sensor._control_lease_last_result['soft_lease_remaining_seconds'] == 0
    assert client.handle is None
    print('PASS rejected lease has zero remaining validity')


async def main():
    for initial in (False, True):
        for effect in (5.,29.,30.,30.222421,60.,120.,175.,240.): await scenario(effect, initial=initial)
    for veto in ('bms','incomplete','stale','export','battery_discharge','wrong_block','missing_marker','fc03','permission','slot','pending','pause','stop'):
        await scenario(60.,veto)
    await expired_first_renew()
    await rejection_timing()


if __name__ == '__main__': asyncio.run(main())
