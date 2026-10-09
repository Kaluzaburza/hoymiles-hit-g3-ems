"""installation_3 load-step replay and Mode 5 authority for PV delay only."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
import unittest
import test_pv_charge_delay_settling as s

p = s.f


def diagnostic_only(src):
    return replace(src, pv_power_w=None, load_power_w=None, grid_power_w=None,
        battery_power_w=None, bms_battery_power_w=None,
        pv_power_observed_at=None, load_power_observed_at=None,
        grid_power_observed_at=None, battery_power_observed_at=None,
        bms_battery_power_observed_at=None, power_cohort_complete=False,
        power_cohort_generation=None)


class ModeAuthorityTests(unittest.TestCase):
    def test_start_does_not_require_flow_measurements(self):
        self.assertTrue(p.live_ready(diagnostic_only(p.source(p.NOW)), now=p.NOW))

    def test_flow_values_are_not_execution_vetoes_or_effect_proof(self):
        at = p.NOW + timedelta(seconds=20)
        good = p.source(at, mode=5, generation=30)
        for src in (diagnostic_only(good),
                p.source(at, mode=5, generation=30, pv=4594, load=2160, grid=3978),
                p.source(at, mode=5, generation=30, pv=4594, load=616, grid=3978),
                p.source(at, mode=5, generation=30, battery=-1500, grid=-900),
                p.source(at, mode=5, generation=30, battery=2500, grid=4000),
                replace(good, bms_battery_power_w=-1000,
                        pv_power_observed_at=p.NOW-timedelta(hours=1))):
            with self.subTest(src=src):
                proof = p.physical_verification('mode-authority', p.ExecutionAction.PV_CHARGE_HOLD,
                    src, command_sent_at=p.NOW, now=at)
                self.assertEqual(proof.status, p.VerificationStatus.CONFIRMED, proof)
                self.assertEqual(proof.observed_at, src.full_block_generation_at)
                self.assertIn('confirmation_source=pv_hold_mode5_fc03', proof.evidence)
                self.assertIn('power_flows=diagnostic_only', proof.evidence)

    def test_mode_proof_still_requires_new_fresh_complete_physical_readback(self):
        at = p.NOW + timedelta(seconds=20)
        good = diagnostic_only(p.source(at, mode=5, generation=30))
        for kw in ({'physical_mode_code': 0}, {'physical_mode_code': 3},
                   {'maximum_discharge_power_percent': 40},
                   {'full_block_generation': None}, {'full_block_generation_at': p.NOW},
                   {'full_block_generation_at': at-timedelta(seconds=16)},
                   {'full_block_generation_at': at+timedelta(seconds=6)},
                   {'full_block_execution_ready': False},
                   {'hardware_readback_supported': False},
                   {'machine_type_code': 2}, {'force_charge_soc_percent': None}):
            with self.subTest(kw=kw):
                proof = p.physical_verification('invalid-mode', p.ExecutionAction.PV_CHARGE_HOLD,
                    replace(good, **kw), command_sent_at=p.NOW, now=at)
                self.assertNotEqual(proof.status, p.VerificationStatus.CONFIRMED, proof)


class ModeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_export_revocation_overrides_prior_authorized_decision(self):
        for executing in (False, True):
            for permission in (p.ExportState.CONFIRMED_ZERO_EXPORT, p.ExportState.PROHIBITED):
                with self.subTest(executing=executing, permission=permission):
                    c, clock, writes = await s.SettlingTests().make()
                    if executing:
                        for second in (2, 17):
                            clock[0] = at = p.NOW + timedelta(seconds=second)
                            await c.async_reconcile(p.frame(at, diagnostic_only(
                                p.source(at, mode=5, generation=20+second)), pending=True))
                        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)
                    clock[0] = at = p.NOW + timedelta(seconds=22 if executing else 3)
                    fr = p.frame(at, diagnostic_only(p.source(at, mode=5, generation=50)),
                        pending=not executing, active=executing)
                    # Live permission can change before the next decision rebuild.
                    fr = replace(fr, context=replace(fr.context, export_state=permission))
                    await c.async_reconcile(fr)
                    self.assertNotIn(c.record.state, (
                        p.ActiveState.WAITING_READBACK, p.ActiveState.EXECUTING))
                    self.assertEqual(len(writes), 2)
                    self.assertEqual(writes[-1].ems_block.mode, p.EmsMode.SELF_USE)

    async def test_actual_load_step_and_three_replans_preserve_lease_then_restore(self):
        c, clock, writes = await s.SettlingTests().make()
        original = c.record.transaction
        fw = s.FirmwareLeaseModel()
        seq = 0
        for second in range(2, 223, 5):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            # No power generation can confer authority; fresh FC03 must do it.
            src = diagnostic_only(p.source(at, mode=5, generation=20+second))
            if second == 97:
                # Recorder: new GRID arrived 1.11773 s before LOAD 2160 -> 616.
                src = replace(src, pv_power_w=4594., load_power_w=2160., grid_power_w=3978.,
                    critical_grid_power_w=3978., critical_grid_power_observed_at=at,
                    battery_power_w=0., bms_battery_power_w=0.)
            pending = 27 <= second < 42 or 87 <= second < 102 or 147 <= second < 162
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
                self.assertIsNotNone(sensor._control_lease_renewal_evidence(), (second, c.record))
            if second % 20 == 2:
                seq += 1
                self.assertTrue(fw.renew(authorized=True, sequence=seq))
            self.assertEqual(fw.state, 'confirmed')
            self.assertEqual(c.record.transaction.transaction_id, original.transaction_id)
            self.assertEqual(c.record.transaction.command_sent_at, original.command_sent_at)
            self.assertEqual(c.record.transaction.deadline, original.deadline)
            self.assertEqual(len(writes), 1)
        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)
        clock[0] = p.END
        await c.async_reconcile(p.frame(p.END, diagnostic_only(p.source(p.END, mode=5, generation=1500)), active=True))
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[-1].ems_block.mode, p.EmsMode.SELF_USE)
        clock[0] = at = p.END+timedelta(seconds=3)
        await c.async_reconcile(p.frame(at, p.source(at, generation=1503)))
        self.assertEqual(c.record.state, p.ActiveState.IDLE)
        self.assertEqual(c.record.owner, p.ExecutionOwner.NONE)

    async def test_consent_and_wrong_readback_still_block_real_lease_renewal(self):
        c, clock, writes = await s.SettlingTests().make()
        for second in (2,17,22):
            clock[0] = at = p.NOW+timedelta(seconds=second)
            fr = p.frame(at, diagnostic_only(p.source(at, mode=5, generation=20+second)),
                pending=second < 22, active=second >= 22)
            await c.async_reconcile(fr)
        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)
        sensor, _ = s.SettlingTests().sensor(c, fr)
        for bad in (replace(fr, execution=replace(fr.execution, physical_mode_code=0)),
                replace(fr, execution=replace(fr.execution, maximum_discharge_power_percent=40)),
                replace(fr, execution=replace(fr.execution, full_block_generation_at=p.NOW)),
                p.frame(at, fr.execution, active=True, allowed=False)):
            sensor._latest_active_frame = bad
            with patch.object(s.adapter.SENSOR.dt_util, 'utcnow', return_value=at):
                self.assertIsNone(sensor._control_lease_renewal_evidence())
        sensor._latest_active_frame = fr
        sensor._pause_state = 'on'
        with patch.object(s.adapter.SENSOR.dt_util, 'utcnow', return_value=at):
            self.assertIsNone(sensor._control_lease_renewal_evidence())


if __name__ == '__main__':
    unittest.main()
