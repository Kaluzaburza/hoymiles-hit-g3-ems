"""Recorded startup divergence and bounded retries, without inverter I/O."""
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch
import test_pv_charge_delay_settling as s

p = s.f


class StartupLoopTests(unittest.IsolatedAsyncioTestCase):
    async def make(self):
        c, clock, writes = await s.SettlingTests().make()
        c._control_lease_valid_until = lambda: clock[0] + timedelta(seconds=60)
        return c, clock, writes

    async def test_recorded_divergence_and_missing_flows_do_not_veto_fresh_mode(self):
        c, clock, writes = await self.make()
        original = c.record.transaction
        firmware = s.FirmwareLeaseModel()
        firmware_block = s.adapter.SENSOR._control_lease_block(original.intent.command.ems_block)
        self.assertTrue(firmware.arm(firmware_block, transaction=original.transaction_id, hard_seconds=1200))
        firmware.observe(firmware_block)
        sequence = 0
        changes = (
            dict(battery_power_w=-3299., bms_battery_power_w=-5.,
                 pv_power_w=4226., load_power_w=927., grid_power_w=0.),
            dict(grid_power_w=-150., battery_power_w=120., bms_battery_power_w=115.),
            dict(pv_power_w=None, load_power_w=None, grid_power_w=None,
                 battery_power_w=None, bms_battery_power_w=None),
        )
        for second in range(3, 154, 5):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            src = replace(p.source(at, mode=5, generation=second+10),
                          **changes[(second//10) % len(changes)])
            fr = p.frame(at, src, pending=second <= 18, active=second > 18)
            await c.async_reconcile(fr)
            self.assertEqual(c.record.state, p.ActiveState.WAITING_READBACK
                             if second < 18 else p.ActiveState.EXECUTING)
            if c.record.state is p.ActiveState.EXECUTING:
                fr = p.frame(at, src, active=True)
            self.assertEqual(c.record.transaction.transaction_id, original.transaction_id)
            self.assertEqual(c.record.transaction.command_sent_at, original.command_sent_at)
            self.assertEqual(c.record.transaction.deadline, original.deadline)
            self.assertEqual(len(writes), 1)
            sensor, _ = s.SettlingTests().sensor(c, fr)
            gates=[]
            sensor._record_control_lease_gate=lambda **kw:gates.append(kw)
            with patch.object(s.adapter.SENSOR.dt_util, 'utcnow', return_value=at):
                self.assertIsNotNone(sensor._control_lease_renewal_evidence(), (second,gates))
            self.assertLessEqual(sensor._control_lease_authorization_deadline,
                                 p.NOW + timedelta(seconds=180) if second < 18 else p.END)
            firmware.advance(second-firmware.now)
            self.assertEqual(firmware.state, 'confirmed')
            if second % 20 == 3:
                sequence += 1
                self.assertTrue(firmware.renew(authorized=True, sequence=sequence))
        for second in (160, 175):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            await c.async_reconcile(p.frame(at, p.source(at, mode=5, generation=second+10), active=True))
        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)
        self.assertEqual(len(writes), 1)
        # Contradictory FC03 mode remains an execution veto.
        clock[0] = at = p.NOW + timedelta(seconds=190)
        await c.async_reconcile(p.frame(at, p.source(at, mode=3, generation=200,
                                                     battery=400., grid=-100.), active=True))
        self.assertNotEqual(c.record.state, p.ActiveState.EXECUTING)

    async def test_timeout_restore_cooldown_survives_restart_without_permanent_lock(self):
        c, clock, writes = await self.make()
        async def tick(second, mode=5):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            await c.async_reconcile(p.frame(at, p.source(at, mode=mode, generation=second+10,
                                                       battery=-1500., grid=0.), pending=mode==5))
        await tick(5)
        await tick(180)
        self.assertEqual(len(writes), 2)
        await tick(185, 0)
        self.assertEqual(c.record.state, p.ActiveState.IDLE)
        await tick(186, 0)
        self.assertEqual(len(writes), 2)
        for second in (245, 300, 359):
            await tick(second, 0)
            self.assertEqual(len(writes), 2, 'Replans cannot shorten the neutral cooldown')
        await tick(360, 0)
        self.assertEqual(len(writes), 3)
        self.assertTrue(c.record.transaction.transaction_id.startswith('pvhold_retry1:'))
        first_retry = c.record.transaction
        await tick(365)
        await tick(540)
        await tick(545, 0)
        self.assertEqual(len(writes), 4)
        async def persist(_): pass
        async def dispatch(write): writes.append(write)
        c = p.f.SupervisorActiveController(persist=persist, dispatch=dispatch,
            publish=lambda _:None, persisted_record=c.record, clock=lambda:clock[0])
        await c.async_initialize()
        for second in (600, 700, 719):
            await tick(second, 0)
            self.assertEqual(len(writes), 4, 'Restart cannot shorten the next cooldown')
        await tick(720, 0)
        self.assertEqual(len(writes), 5)
        self.assertTrue(c.record.transaction.transaction_id.startswith('pvhold_retry2:'))
        self.assertNotEqual(c.record.transaction.transaction_id, first_retry.transaction_id)
        self.assertEqual(c.record.transaction.deadline, first_retry.deadline)
        self.assertEqual(first_retry.deadline, p.END)

    async def test_pv_confirmed_cohort_gap_ignores_inactive_battery_sale_slot(self):
        # Installation 3 at +57 s: FC03 and BMS were ready, flow cohort was
        # incomplete, and the independent battery-sale slot said no_current_plan.
        # That inactive SELL flag must not revoke an authorized PV hold.
        c, clock, writes = await self.make()
        for second in (31, 52):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            fr = p.frame(at, p.source(at, mode=5, generation=second+10), pending=True)
            fr = replace(fr, rce=replace(fr.rce,
                current_slot_continue_eligible=False,
                current_slot_suppression_reason='no_current_plan'))
            await c.async_reconcile(fr)
        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)
        original = c.record.transaction
        clock[0] = at = p.NOW + timedelta(seconds=57)
        src = replace(p.source(at, mode=5, generation=67), power_cohort_complete=False)
        fr = p.frame(at, src, active=True)
        fr = replace(fr, rce=replace(fr.rce,
            current_slot_continue_eligible=False,
            current_slot_suppression_reason='no_current_plan'))
        await c.async_reconcile(fr)
        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)
        self.assertEqual(c.record.transaction.transaction_id, original.transaction_id)
        self.assertGreater(c.record.transaction.physical_verification.observed_at,
                           original.physical_verification.observed_at)
        self.assertEqual(c.record.transaction.physical_verification.observed_at,
                         src.full_block_generation_at)
        self.assertIsNone(c._telemetry_hold_until)
        self.assertEqual(len(writes), 1)
        clock[0] = at = p.NOW + timedelta(seconds=62)
        fr = p.frame(at, p.source(at, mode=5, generation=72), active=True)
        fr = replace(fr, rce=replace(fr.rce, current_slot_continue_eligible=False,
                                  current_slot_suppression_reason='no_current_plan'))
        await c.async_reconcile(fr)
        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)
        self.assertIsNone(c._telemetry_hold_until)
        sensor, _ = s.SettlingTests().sensor(c, fr)
        with patch.object(s.adapter.SENSOR.dt_util, 'utcnow', return_value=at):
            self.assertIsNotNone(sensor._control_lease_renewal_evidence())


if __name__ == '__main__': unittest.main()
