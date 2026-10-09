"""Audit PV hold continuity with the production controller and fresh physics.

No host I/O. A completed, physically confirmed hold receives a short pending
plan publication while every physical and user gate remains unchanged.
"""
from datetime import timedelta
from dataclasses import replace
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

R = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(R/'tools'), str(R/'custom_components/hoymiles_hit_modbus')]
import test_pv_charge_delay_settling as settling
p = settling.f


class ReplanAudit(unittest.IsolatedAsyncioTestCase):
    async def test_lease_callback_ahead_of_frame_cannot_reset_or_cancel_pending_hold(self):
        for offset in (.000001,.05,.35,.38):
            controller,clock,writes=await self.confirmed()
            clock[0]=at=p.NOW+timedelta(seconds=20)
            frame=p.frame(at,p.source(at,mode=5,generation=20),active=True,
                result_current=False,recalculation_pending=True)
            sensor,_=settling.SettlingTests().sensor(controller,frame)
            with patch.object(settling.adapter.SENSOR.dt_util,'utcnow',return_value=at+timedelta(seconds=offset)):
                self.assertIsNotNone(sensor._control_lease_renewal_evidence())
            original=controller.record.transaction
            await controller.async_reconcile(frame)
            self.assertIs(controller.record.state,p.ActiveState.EXECUTING)
            self.assertEqual(controller.record.transaction.deadline,original.deadline)
            self.assertEqual(len(writes),1)
            self.assertLessEqual(controller._rce_execution_hold[1],at)

    async def test_supervisor_exposes_hold_proof_separately_from_battery_sale(self):
        controller, clock, _ = await self.confirmed()
        clock[0] = at = p.NOW + timedelta(seconds=20)
        frame = p.frame(at, p.source(at, mode=5, generation=20), active=True)
        await controller.async_reconcile(frame)
        sensor, _ = settling.SettlingTests().sensor(controller, frame)
        sensor._control_lease_valid_until = lambda: at+timedelta(seconds=30)
        self.assertIsNone(sensor.current_rce_active_commitment(at))
        self.assertEqual(sensor.current_pv_delay_commitment(at).transaction_id,
                         controller.record.transaction.transaction_id)
        sensor._control_lease_valid_until = lambda: at
        self.assertIsNone(sensor.current_pv_delay_commitment(at))

    async def test_pending_during_ramp_renews_real_lease_without_restarting_settling(self):
        controller, clock, writes = await settling.SettlingTests().make()
        firmware = settling.FirmwareLeaseModel()
        sequence = 0
        original = controller.record.transaction
        for second in range(5, 81, 5):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            pending = 10 <= second < 40
            charging = second < 55
            frame = p.frame(at, p.source(at, mode=5, generation=second+10,
                battery=-1500 if charging else 0, grid=0 if charging else 1500),
                pending=second<=20, active=second>20,
                result_current=not pending, recalculation_pending=pending)
            await controller.async_reconcile(frame)
            if controller.record.state is p.ActiveState.EXECUTING:
                frame = p.frame(at, frame.execution, active=True,
                    result_current=not pending, recalculation_pending=pending)
            sensor, block = settling.SettlingTests().sensor(controller, frame)
            if second == 5:
                self.assertTrue(firmware.arm(block, transaction=original.transaction_id, hard_seconds=1200))
                firmware.observe(block)
            firmware.advance(second-firmware.now)
            with patch.object(settling.adapter.SENSOR.dt_util, 'utcnow', return_value=at):
                proof = sensor._control_lease_renewal_evidence()
            self.assertIsNotNone(proof, second)
            # Two seconds of transport latency do not reset the ramp clock.
            firmware.advance(2.)
            sequence += 1
            self.assertTrue(firmware.renew(authorized=True, sequence=sequence))
            self.assertEqual(controller.record.transaction.transaction_id, original.transaction_id)
            self.assertEqual(controller.record.transaction.command_sent_at, original.command_sent_at)
            self.assertEqual(len(writes), 1)
        self.assertEqual(controller.record.state, p.ActiveState.EXECUTING)

    async def test_fixed_pending_budget_and_earlier_firmware_expiry(self):
        controller, clock, _ = await self.confirmed()
        for elapsed in (0, 10, 59, 60, 89, 90, 91, 149, 150, 179, 180, 181):
            clock[0] = at = p.NOW + timedelta(seconds=20+elapsed)
            frame = p.frame(at, p.source(at, mode=5, generation=20+elapsed), active=True,
                result_current=False, recalculation_pending=True)
            self.assertEqual(controller.pv_hold_replan_authorized(frame, now=at), elapsed<180)
            self.assertEqual(controller.pv_hold_replan_authorized(frame, now=at, lease_ttl_seconds=30), elapsed<150)
        self.assertEqual(controller._rce_execution_hold[1], p.NOW+timedelta(seconds=20))
        # M01's last accepted renewal can expire before the 180-second logical
        # budget. A fresh neutral FC03 cannot be reported as continuing Mode 5.
        firmware = settling.FirmwareLeaseModel()
        _, block = settling.SettlingTests().sensor(controller, frame)
        firmware.arm(block, hard_seconds=1200); firmware.observe(block)
        firmware.advance(29); firmware.renew(authorized=True, sequence=1)
        firmware.advance(29); firmware.renew(authorized=True, sequence=2)
        firmware.advance(120)
        self.assertEqual(firmware.state, 'restoring')
        frame = p.frame(at, p.source(at, mode=0, generation=140), active=True,
            result_current=False, recalculation_pending=True)
        self.assertFalse(controller.pv_hold_replan_authorized(frame, now=at))

    async def test_pending_never_bypasses_current_safety_gates(self):
        controller, clock, _ = await self.confirmed()
        clock[0] = at = p.NOW + timedelta(seconds=20)
        frame = p.frame(at, p.source(at, mode=5, generation=20), active=True,
            result_current=False, recalculation_pending=True)
        bads = [replace(frame, rce=replace(frame.rce, **kw)) for kw in (
            {'allowed_by_user':False}, {'enabled':False}, {'pv_charge_hold_qualified':False},
            {'current_run_end':p.END+timedelta(minutes=30)}, {'sale_block_active':True})]
        bads += [replace(frame, execution=replace(frame.execution, **kw)) for kw in (
            {'bms_max_charge_current_a':0}, {'bms_max_discharge_current_a':0},
            {'physical_mode_code':0}, {'maximum_discharge_power_percent':40},
            {'full_block_generation_at':at-timedelta(seconds=16)})]
        bads += [replace(frame, context=replace(frame.context, **kw)) for kw in (
            {'owner_conflict':True}, {'export_state':p.f.ExportState.CONFIRMED_ZERO_EXPORT},
            {'export_state':p.f.ExportState.PROHIBITED}, {'export_state':p.f.ExportState.UNVERIFIED})]
        for bad in bads:
            with self.subTest(bad=bad):
                self.assertFalse(controller.pv_hold_replan_authorized(bad, now=at))
        for kw in ({'grid_power_w':-100}, {'pv_power_w':300}, {'power_cohort_complete':False},
                   {'battery_power_w':3000}, {'bms_battery_power_observed_at':at-timedelta(seconds=16)}):
            with self.subTest(flow=kw):
                self.assertTrue(controller.pv_hold_replan_authorized(
                    replace(frame, execution=replace(frame.execution, **kw)), now=at))

    async def confirmed(self):
        writes = []
        clock = [p.NOW]
        async def persist(_):
            pass
        async def dispatch(write):
            writes.append(write)
        controller = p.f.SupervisorActiveController(
            persist=persist, dispatch=dispatch, publish=lambda _: None,
            clock=lambda: clock[0])
        await controller.async_initialize()
        await controller.async_reconcile(p.frame(p.NOW, p.source(p.NOW)))
        for second, generation in ((2, 12), (17, 13)):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            await controller.async_reconcile(p.frame(
                at, p.source(at, mode=5, generation=generation), pending=True))
        self.assertEqual(controller.record.state, p.ActiveState.EXECUTING)
        return controller, clock, writes

    async def test_confirmed_hold_survives_short_pending_plan(self):
        controller, clock, writes = await self.confirmed()
        original = controller.record.transaction
        for second in (22, 23, 24):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            await controller.async_reconcile(p.frame(
                at, p.source(at, mode=5, generation=second), active=True,
                result_current=False, recalculation_pending=True))
            self.assertEqual(controller.record.state, p.ActiveState.EXECUTING,
                f'{second}s: {controller.record.reason}; stops={controller._stop_decisions}')
            self.assertEqual(controller.record.transaction.transaction_id, original.transaction_id)
            self.assertEqual(controller.record.transaction.deadline, original.deadline)
            self.assertEqual(len(writes), 1)
        clock[0] = at = p.NOW + timedelta(seconds=25)
        await controller.async_reconcile(p.frame(
            at, p.source(at, mode=5, generation=25), active=True))
        self.assertEqual(controller.record.state, p.ActiveState.EXECUTING)
        self.assertEqual(len(writes), 1)

    async def test_fresh_confirmed_hold_lease_survives_short_pending_plan(self):
        controller, clock, writes = await self.confirmed()
        clock[0] = at = p.NOW + timedelta(seconds=20)
        stable = p.frame(at, p.source(at, mode=5, generation=20), active=True)
        await controller.async_reconcile(stable)
        sensor, _ = settling.SettlingTests().sensor(controller, stable)
        with patch.object(settling.adapter.SENSOR.dt_util, 'utcnow', return_value=at):
            self.assertIsNotNone(sensor._control_lease_renewal_evidence())
            sensor._latest_active_frame = p.frame(at, p.source(at, mode=5, generation=20),
                active=True, result_current=False, recalculation_pending=True)
            self.assertIsNotNone(sensor._control_lease_renewal_evidence(),
                'Pending publication also withdraws firmware lease renewal')


if __name__ == '__main__':
    unittest.main()
