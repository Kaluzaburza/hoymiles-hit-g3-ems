"""Planner binding during the ACK-to-physical-confirmation interval."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest
import test_pv_charge_delay_settling as s

p = s.f


class PlanningProofTests(unittest.IsolatedAsyncioTestCase):
    async def test_acknowledged_mode_pins_plan_without_claiming_execution(self):
        controller, clock, writes = await s.SettlingTests().make()
        clock[0] = at = p.NOW + timedelta(seconds=5)
        frame = p.frame(at, p.source(at, mode=5, generation=12), pending=True)
        await controller.async_reconcile(frame)
        self.assertEqual(controller.record.state, p.ActiveState.WAITING_READBACK)
        sensor, _ = s.SettlingTests().sensor(controller, frame)
        sensor._control_lease_valid_until = lambda: at + timedelta(seconds=30)
        proof = sensor.current_pv_delay_commitment(at)
        self.assertIsNotNone(proof)
        self.assertEqual(proof.command_sent_at, controller.record.transaction.command_sent_at)
        self.assertFalse(proof.physical_confirmed)
        self.assertIsNone(sensor.current_rce_active_commitment(at))
        self.assertEqual(len(writes), 1)

    async def test_pending_proof_still_requires_exact_new_fc03_and_live_gates(self):
        controller, clock, _ = await s.SettlingTests().make()
        clock[0] = at = p.NOW + timedelta(seconds=5)
        frame = p.frame(at, p.source(at, mode=5, generation=12), pending=True)
        await controller.async_reconcile(frame)
        sensor, _ = s.SettlingTests().sensor(controller, frame)
        sensor._control_lease_valid_until = lambda: at + timedelta(seconds=30)
        for kw in ({'physical_mode_code': 0}, {'maximum_discharge_power_percent': 20},
                   {'full_block_generation_at': p.NOW}, {'bms_max_charge_current_a': 0}):
            sensor._latest_active_frame = replace(frame, execution=replace(frame.execution, **kw))
            self.assertIsNone(sensor.current_pv_delay_commitment(at))
        sensor._latest_active_frame = frame
        sensor._pause_state = 'on'
        self.assertIsNone(sensor.current_pv_delay_commitment(at))

    async def test_ack_binding_expires_and_cannot_survive_lost_lease_or_consent(self):
        controller, clock, _ = await s.SettlingTests().make()
        clock[0] = at = p.NOW + timedelta(seconds=5)
        frame = p.frame(at, p.source(at, mode=5, generation=12), pending=True)
        await controller.async_reconcile(frame)
        sensor, _ = s.SettlingTests().sensor(controller, frame)
        sensor._control_lease_valid_until = lambda: at + timedelta(seconds=300)
        self.assertIsNotNone(sensor.current_pv_delay_commitment(at))
        sensor._latest_active_frame = replace(frame, rce=replace(frame.rce, allowed_by_user=False))
        self.assertIsNone(sensor.current_pv_delay_commitment(at))
        sensor._latest_active_frame = frame
        handle = sensor._control_lease_client.handle
        sensor._control_lease_client.handle = None
        self.assertIsNone(sensor.current_pv_delay_commitment(at))
        sensor._control_lease_client.handle = SimpleNamespace(**{**vars(handle), 'transaction_id': 'different'})
        self.assertIsNone(sensor.current_pv_delay_commitment(at))
        sensor._control_lease_client.handle = handle
        at = p.NOW + timedelta(seconds=180)
        sensor._latest_active_frame = p.frame(at, p.source(at, mode=5, generation=30), pending=True)
        self.assertIsNone(sensor.current_pv_delay_commitment(at))


if __name__ == '__main__':
    unittest.main()
