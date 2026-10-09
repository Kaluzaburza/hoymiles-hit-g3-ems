"""Real HA renewal gate and bounded flow gaps with battery house support."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
import unittest
import test_pv_charge_delay_settling as s

p = s.f


class HouseLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_lease_survives_house_deficits_and_completed_replans(self):
        c, clock, writes = await s.SettlingTests().make()
        original = c.record.transaction
        fw = s.FirmwareLeaseModel()
        sequence = 0
        for second in range(2, 223, 5):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            pv = (0., 409., 533., 2000.)[(second//20) % 4]
            pending = 27 <= second < 42 or 87 <= second < 102 or 147 <= second < 162
            src = p.source(at, mode=5, generation=second+20,
                pv=pv, load=408., battery=max(0., 408.-pv), grid=max(0., pv-408.))
            fr = p.frame(at, src, pending=second < 22, active=second >= 22,
                result_current=not pending, recalculation_pending=pending,
                input_revision=second//20)
            await c.async_reconcile(fr)
            if c.record.state is p.ActiveState.EXECUTING:
                fr = p.frame(at, src, active=True, result_current=not pending,
                    recalculation_pending=pending, input_revision=second//20)
            sensor, block = s.SettlingTests().sensor(c, fr)
            if second == 2:
                self.assertTrue(fw.arm(block, transaction=original.transaction_id, hard_seconds=1200))
                fw.observe(block)
            fw.advance(second-fw.now)
            with patch.object(s.adapter.SENSOR.dt_util, 'utcnow', return_value=at):
                self.assertIsNotNone(sensor._control_lease_renewal_evidence(), (second,c.record))
            if second % 20 == 2:
                sequence += 1
                self.assertTrue(fw.renew(authorized=True, sequence=sequence))
            self.assertEqual(fw.state, 'confirmed')
            self.assertEqual(c.record.transaction.transaction_id, original.transaction_id)
            self.assertEqual(c.record.transaction.command_sent_at, original.command_sent_at)
            self.assertEqual(c.record.transaction.deadline, original.deadline)
            self.assertEqual(len(writes), 1)
        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)

    async def test_missing_flow_cohort_does_not_need_telemetry_grace(self):
        c, clock, writes = await s.SettlingTests().make()
        for second in (2, 17, 22):
            clock[0] = at = p.NOW+timedelta(seconds=second)
            src = p.source(at, mode=5, generation=second+20,
                pv=1000., load=1189., battery=189., grid=0.)
            fr = p.frame(at, src, pending=second < 22, active=second >= 22)
            await c.async_reconcile(fr)
        original = c.record.transaction
        c._control_lease_valid_until = lambda: p.NOW+timedelta(seconds=120)
        clock[0] = at = p.NOW+timedelta(seconds=27)
        gap = replace(src, power_cohort_complete=False)
        await c.async_reconcile(p.frame(at, gap, active=True))
        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)
        self.assertEqual(c.record.transaction.physical_verification, original.physical_verification)
        self.assertEqual(len(writes), 1)
        # A valid FC03 proves this mode even with missing/contrary power data;
        # this is normal execution and must not suppress renewal with a grace.
        self.assertIsNone(c._telemetry_hold_until)
        bad = p.frame(at, replace(gap, battery_power_w=1500., bms_battery_power_w=1500.), active=True)
        self.assertFalse(c._confirmed_telemetry_wait(bad, now=at))
        bad = p.frame(at, replace(gap, battery_power_w=-200., bms_battery_power_w=-200.), active=True)
        self.assertFalse(c._confirmed_telemetry_wait(bad, now=at))


if __name__ == '__main__':
    unittest.main()
