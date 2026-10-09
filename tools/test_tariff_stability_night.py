"""Overnight regressions through production controller and HA lease adapter.

Only persistence, clock and device transport are faked. No host/device I/O.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch

import test_tariff_pending_dispatch_race as h
from test_tariff_initial_lease_settling import ack
from test_supervisor_control_lease import FirmwareLeaseModel
from supervisor_active_bridge import physical_verification

a, f = h.adapter, h.fixture


def arm(sensor, controller, at, mono):
    tx = controller.record.transaction
    request = sensor._control_lease_client.prepare_arm(
        transaction_id=tx.transaction_id, hard_deadline=tx.deadline,
        block=a.SENSOR._control_lease_block(tx.intent.command.ems_block),
        challenge_nonce='00000001', now_wall=at, now_monotonic=mono)
    assert sensor._control_lease_client.accept_arm(request, ack('accepted'))


class NightTests(unittest.IsolatedAsyncioTestCase):
    async def test_lease_reader_before_older_frame_never_stops_or_extends_hold(self):
        for delta in (-.05, 0., .000001, .05, .369926, .350956, .382426):
            with self.subTest(delta=delta):
                _, _, _, sensor = a.environment()
                sensor._pause_state = 'off'
                ctl, sent, _, clock = await f.confirmed_controller()
                sensor._controller = ctl
                clock[0] = f.NOW + timedelta(seconds=118)
                await ctl.async_reconcile(f.frame(clock[0], f.physical(clock[0], generation=29), ctl.record))
                tx = ctl.record.transaction
                loop = asyncio.get_running_loop(); mono = loop.time()
                arm(sensor, ctl, clock[0], mono)
                ctl._control_lease_valid_until = sensor._control_lease_valid_until
                clock[0] = f.NOW + timedelta(seconds=120)
                frame = f.frame(clock[0], replace(f.physical(clock[0], generation=30),
                    power_cohort_complete=True), ctl.record, result_current=False,
                    recalculation_pending=True, planned_slot_ready=None, control_data_ready=False)
                sensor._latest_active_frame = frame
                a.CLOCK['now'] = frame.now + timedelta(seconds=delta)
                sensor._fresh_active_frame = lambda: replace(frame, now=a.CLOCK['now'])
                with patch.object(loop, 'time', return_value=mono+2+max(delta, 0)):
                    self.assertIsNotNone(sensor._control_lease_renewal_evidence())
                    ceiling = ctl.tariff_execution_settling_deadline
                    await ctl.async_reconcile(frame)
                    self.assertIs(ctl.record.state, f.ActiveState.EXECUTING)
                    self.assertEqual(len(sent), 1)
                    self.assertEqual(ctl.record.transaction.deadline, tx.deadline)
                    self.assertLessEqual(ctl.tariff_execution_settling_deadline, ceiling)
                    self.assertLessEqual(ctl.tariff_execution_settling_deadline,
                        min(frame.now, a.CLOCK['now'])+timedelta(seconds=180))

    async def test_small_house_import_is_proof_but_battery_supply_is_not(self):
        for pv in (0., 1000.):
            for watts in (1., 50., 100., 189., 199., 200., 201.):
                for battery in (0., watts):
                    with self.subTest(pv=pv, watts=watts, battery=battery):
                        src = replace(f.physical(f.NOW+timedelta(seconds=2)),
                            pv_power_w=pv, load_power_w=pv+watts,
                            grid_power_w=battery-watts, battery_power_w=battery)
                        proof = physical_verification('tariff:house', f.ExecutionAction.TARIFF_GRID_SUPPORT,
                            src, command_sent_at=f.NOW, now=f.NOW+timedelta(seconds=2))
                        self.assertEqual(proof.status is a.SENSOR.VerificationStatus.CONFIRMED, battery==0)
            # Partial PV does not turn an incoherent 1 W import into proof.
            src = replace(f.physical(f.NOW+timedelta(seconds=2)),
                pv_power_w=pv, load_power_w=pv+189., grid_power_w=-1., battery_power_w=0.)
            proof = physical_verification('tariff:house', f.ExecutionAction.TARIFF_GRID_SUPPORT,
                src, command_sent_at=f.NOW, now=f.NOW+timedelta(seconds=2))
            self.assertIsNot(proof.status, a.SENSOR.VerificationStatus.CONFIRMED)

    async def test_support_commitment_requires_current_lease_and_control_evidence(self):
        _, _, _, sensor = a.environment(); sensor._pause_state = 'off'
        ctl, _, _, clock = await f.confirmed_controller(); sensor._controller = ctl
        loop = asyncio.get_running_loop(); mono = loop.time()
        arm(sensor, ctl, clock[0], mono)
        frame = f.frame(clock[0], f.physical(clock[0]), ctl.record)
        sensor._fresh_active_frame = lambda: frame
        a.CLOCK['now'] = clock[0]
        proof = sensor.current_tariff_active_commitment(clock[0])
        self.assertIsNotNone(proof)
        self.assertEqual(proof.action, 'grid_support')
        self.assertEqual(proof.hard_deadline, ctl.record.transaction.deadline)
        for label, change in (
            ('permission', {'tariff':replace(frame.tariff, allowed_by_user=False)}),
            ('bms', {'context':replace(frame.context, critical_bms_ready=False)}),
            ('block', {'execution':replace(frame.execution, force_charge_soc_percent=66)}),
        ):
            with self.subTest(label=label):
                sensor._fresh_active_frame = lambda: replace(frame, **change)
                self.assertIsNone(sensor.current_tariff_active_commitment(clock[0]))
        sensor._fresh_active_frame = lambda: frame
        sensor._pause_state = 'on'
        self.assertIsNone(sensor.current_tariff_active_commitment(clock[0]))
        sensor._pause_state = 'off'
        sensor._cohort_cancel = object()
        self.assertIsNone(sensor.current_tariff_active_commitment(clock[0]))
        sensor._cohort_cancel = None
        with patch.object(loop, 'time', return_value=mono+120):
            self.assertIsNone(sensor.current_tariff_active_commitment(clock[0]))

    async def test_support_ramp_renews_only_to_original_physical_deadline(self):
        for effect in (31., 140., 999.):
            with self.subTest(effect=effect):
                _, _, _, sensor = a.environment(); sensor._pause_state='off'
                ctl, sent, _, clock = await f.confirmed_controller(); sensor._controller=ctl
                # A charge command then a distinct support retarget, as in cycle 5.
                for second, target, battery, action in (
                    (3,65,0,f.TariffAction.GRID_SUPPORT_AND_CHARGE),
                    (5,65,-5000,f.TariffAction.GRID_SUPPORT_AND_CHARGE),
                    (7,60,-5000,f.TariffAction.GRID_SUPPORT)):
                    clock[0]=f.NOW+timedelta(seconds=second)
                    await ctl.async_reconcile(f.frame(clock[0], f.physical(clock[0],
                        target=65 if second>3 else 58,battery=battery,generation=11+second),
                        ctl.record,target=target,action=action))
                tx=ctl.record.transaction; anchor=clock[0]
                self.assertIs(ctl.record.state,f.ActiveState.RETARGETING)
                loop=asyncio.get_running_loop(); mono=loop.time(); arm(sensor,ctl,anchor,mono)
                esp=FirmwareLeaseModel()
                block=a.SENSOR._control_lease_block(tx.intent.command.ems_block)
                self.assertTrue(esp.arm(block,transaction=tx.transaction_id,generation=1,hard_seconds=1800))
                esp.observe(block)
                for i, elapsed in enumerate((5.,20.,40.,60.,80.,100.,120.,140.,160.,179.)):
                    clock[0]=anchor+timedelta(seconds=elapsed); a.CLOCK['now']=clock[0]
                    src=replace(f.physical(clock[0],target=58,generation=30+i,
                        battery=0 if elapsed>=effect else 1200),power_cohort_complete=True)
                    frame=f.frame(clock[0],src,ctl.record,target=60)
                    sensor._latest_active_frame=frame
                    await ctl.async_reconcile(frame)
                    esp.advance(elapsed-esp.now)
                    with patch.object(loop,'time',return_value=mono+elapsed):
                        evidence=sensor._control_lease_renewal_evidence()
                    if elapsed<179:
                        self.assertIsNotNone(evidence,(elapsed,sensor._control_lease_gate))
                    if elapsed<effect:
                        self.assertIsNot(ctl.record.state,f.ActiveState.EXECUTING)
                    else:
                        self.assertIs(ctl.record.state,f.ActiveState.EXECUTING)
                    request=sensor._control_lease_client.prepare_renew(authorized=evidence is not None,
                        snapshot_generation=30+i,now_wall=clock[0],now_monotonic=mono+elapsed,
                        authorization_deadline=sensor._control_lease_authorization_deadline)
                    if request:
                        self.assertTrue(esp.renew(authorized=True,sequence=request['sequence'],
                            authorization_seconds=request['authorization_seconds']))
                        if elapsed<effect:
                            self.assertLessEqual(esp.expiry,180.)
                        self.assertTrue(sensor._control_lease_client.accept_renew(
                            ack('renewed',f'{100+i:08x}',int((esp.expiry-esp.now)*1000)),
                            request=request,now_monotonic=mono+elapsed+.001))
                    self.assertEqual(ctl.record.transaction.deadline,tx.deadline)
                if effect>180:
                    self.assertIsNone(evidence)
                    self.assertTrue(sensor._control_lease_client.expire_if_due(now_monotonic=mono+180))

    async def test_support_ramp_vetoes_remain_fail_closed(self):
        _,_,_,sensor=a.environment(); sensor._pause_state='off'
        ctl,_,_,clock=await f.confirmed_controller(); sensor._controller=ctl
        clock[0]=f.NOW+timedelta(seconds=3)
        await ctl.async_reconcile(f.frame(clock[0],f.physical(clock[0],generation=12),ctl.record,
            target=65,action=f.TariffAction.GRID_SUPPORT_AND_CHARGE))
        clock[0]=f.NOW+timedelta(seconds=5)
        await ctl.async_reconcile(f.frame(clock[0],f.physical(clock[0],target=65,battery=-5000,generation=13),
            ctl.record,target=65,action=f.TariffAction.GRID_SUPPORT_AND_CHARGE))
        clock[0]=f.NOW+timedelta(seconds=7)
        await ctl.async_reconcile(f.frame(clock[0],f.physical(clock[0],target=65,battery=-5000,generation=14),ctl.record))
        arm(sensor,ctl,clock[0],asyncio.get_running_loop().time())
        clock[0]+=timedelta(seconds=5); a.CLOCK['now']=clock[0]
        clean=replace(f.physical(clock[0],target=58,battery=1200,generation=15),power_cohort_complete=True)
        frame=f.frame(clock[0],clean,ctl.record)
        await ctl.async_reconcile(frame); sensor._latest_active_frame=frame
        self.assertIsNotNone(sensor._control_lease_renewal_evidence())
        for change in (
            {'power_cohort_complete':False}, {'power_cohort_complete':None},
            {'bms_max_charge_current_a':0}, {'force_charge_soc_percent':66},
            {'full_block_generation_at':f.NOW-timedelta(seconds=1)},
            {'grid_power_observed_at':f.NOW-timedelta(seconds=100)},
            {'critical_grid_power_w':500,'critical_grid_power_observed_at':clock[0]},
            {'battery_power_w':4000,'grid_power_w':0},
        ):
            with self.subTest(change=change):
                sensor._latest_active_frame=f.frame(clock[0],replace(clean,**change),ctl.record)
                self.assertIsNone(sensor._control_lease_renewal_evidence())
        sensor._latest_active_frame=frame
        sensor._pause_state='on'
        self.assertIsNone(sensor._control_lease_renewal_evidence())
        sensor._pause_state='off'; sensor._master_stop_latched=True
        self.assertIsNone(sensor._control_lease_renewal_evidence())


if __name__=='__main__': unittest.main()
