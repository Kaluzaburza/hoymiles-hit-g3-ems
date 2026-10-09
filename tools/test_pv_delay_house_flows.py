"""PV delay Mode 5 remains active across asynchronous flow measurements.

Pure controller/telemetry regression. No connection to any installation.
"""
from dataclasses import replace
from datetime import timedelta
import unittest

import test_pv_charge_delay_control as p


class HouseFlowTests(unittest.TestCase):
    def test_no_minimum_pv_or_surplus_for_start(self):
        # The qualified forecast window is independent of this instant's sun.
        for pv in (0., 1., 199., 200., 201., 408., 409., 533., 608.):
            with self.subTest(pv=pv):
                load = 408.
                src = p.source(p.NOW, pv=pv, load=load,
                    battery=max(0., load-pv), grid=max(0., pv-load))
                self.assertTrue(p.live_ready(src, now=p.NOW))
                self.assertTrue(p.frame(p.NOW, src).candidates[0].start_eligible)

    def test_proof_accepts_small_exports_and_battery_supplying_only_house(self):
        at = p.NOW + timedelta(seconds=20)
        for pv, load, battery, grid in (
            (0., 408., 408., 0.), (1., 408., 407., 0.),
            (1000., 1189., 189., 0.), (408., 408., 0., 0.),
            (409., 408., 0., 1.), (533., 408., 0., 125.),
            (608., 408., 0., 200.), (2408., 408., 0., 2000.),
        ):
            for parallel in (False, True):
                with self.subTest(pv=pv, load=load, parallel=parallel):
                    src = p.source(at, mode=5, generation=12,
                        pv=pv, load=load, battery=battery, grid=grid)
                    if parallel:
                        src = replace(src, machine_type_code=1, inverter_count=2,
                                      bms_battery_power_w=battery / 2.)
                    proof = p.physical_verification('house-pv', p.ExecutionAction.PV_CHARGE_HOLD,
                        src, command_sent_at=p.NOW, now=at)
                    self.assertEqual(proof.status, p.VerificationStatus.CONFIRMED, proof)

    def test_charging_sale_and_false_cohorts_do_not_override_inverter_mode(self):
        at = p.NOW + timedelta(seconds=20)
        for pv, load, battery, grid in (
            (2000., 500., -1500., 0.),  # Ordinary charging is not PV delay.
            (2000., 500., 300., 1800.), # Extra battery energy is being sold.
            (200., 500., 700., 400.),   # Battery covers more than the house.
            (1000., 1189., 0., 0.),    # An unaccounted house deficit.
            (500., 500., 1000., 0.),   # Contradictory measurements.
        ):
            with self.subTest(pv=pv, load=load, battery=battery):
                proof = p.physical_verification('bad-pv-flow', p.ExecutionAction.PV_CHARGE_HOLD,
                    p.source(at, mode=5, generation=12, pv=pv, load=load,
                             battery=battery, grid=grid), command_sent_at=p.NOW, now=at)
                self.assertEqual(proof.status, p.VerificationStatus.CONFIRMED, proof)
                self.assertIn('energy_effect=not_attested_by_mode_readback', proof.evidence)
        good = p.source(at, mode=5, generation=12, pv=1000., load=1189., battery=189., grid=0.)
        for kw in ({'bms_battery_power_w': -200.},
                   {'bms_battery_power_w': 900.},
                   {'power_cohort_complete': False},
                   {'bms_battery_power_observed_at': p.NOW}):
            with self.subTest(kw=kw):
                proof = p.physical_verification('bad-bms-flow', p.ExecutionAction.PV_CHARGE_HOLD,
                    replace(good, **kw), command_sent_at=p.NOW, now=at)
                self.assertEqual(proof.status, p.VerificationStatus.CONFIRMED, proof)


class HouseFlowLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_clouds_and_three_replans_keep_original_command_and_deadline(self):
        writes, clock = [], [p.NOW]
        async def persist(_): pass
        async def dispatch(write): writes.append(write)
        c = p.f.SupervisorActiveController(persist=persist, dispatch=dispatch,
            publish=lambda _: None, clock=lambda: clock[0])
        await c.async_initialize()
        await c.async_reconcile(p.frame(p.NOW,
            p.source(p.NOW, pv=533., load=408., battery=-125., grid=0.)))
        self.assertEqual(c.record.state, p.ActiveState.WAITING_READBACK)
        original = c.record.transaction
        # Two distinct proofs; a house deficit is legitimate from first ACK.
        for second in (2, 17):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            await c.async_reconcile(p.frame(at, p.source(at, mode=5, generation=second+20,
                pv=1000., load=1189., battery=189., grid=0.), pending=True))
        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)
        for second, pv in ((30, 0.), (60, 409.), (90, 608.), (120, 408.), (150, 2000.)):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            await c.async_reconcile(p.frame(at, p.source(at, mode=5, generation=second+20,
                pv=pv, load=408., battery=max(0., 408.-pv), grid=max(0., pv-408.)),
                active=True, input_revision=second))
            self.assertEqual(c.record.state, p.ActiveState.EXECUTING, c.record)
            self.assertEqual(c.record.transaction.transaction_id, original.transaction_id)
            self.assertEqual(c.record.transaction.deadline, original.deadline)
            self.assertEqual(len(writes), 1)
        clock[0] = p.END
        await c.async_reconcile(p.frame(p.END, p.source(p.END, mode=5, generation=1300), active=True))
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[-1].ems_block.mode, p.EmsMode.SELF_USE)
        clock[0] = at = p.END + timedelta(seconds=3)
        await c.async_reconcile(p.frame(at, p.source(at, generation=1303)))
        self.assertEqual(c.record.state, p.ActiveState.IDLE)
        self.assertEqual(c.record.owner, p.ExecutionOwner.NONE)


if __name__ == '__main__':
    unittest.main()
