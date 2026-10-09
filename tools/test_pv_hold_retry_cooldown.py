"""PV-only timed requalification after a completed neutral attempt; no I/O."""
from dataclasses import replace
from datetime import timedelta
import unittest

import test_pv_hold_precommand_recovery as recovery
from supervisor_executor_codec import record_from_dict, record_to_dict

p = recovery.p


class PvRetryCooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_replans_do_not_reset_three_minute_pause(self):
        c, clock, writes, _, _ = await recovery.PrecommandRecoveryTests().denied()
        original = c.record.last_transaction
        for second in (60, 90, 120, 150, 179):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            await c.async_reconcile(p.frame(at, p.source(at, generation=second+20),
                input_revision=second))
            self.assertFalse(writes, second)
            self.assertEqual(c._pv_hold_retry_status['reason'], 'retry_spacing_active')
            self.assertEqual(c.execution_watchdog,
                (original.transaction_id, p.NOW + timedelta(seconds=180)))
        clock[0] = at = p.NOW + timedelta(seconds=180)
        self.assertIsNone(c.execution_watchdog, 'An elapsed cooldown must not reschedule itself')
        await c.async_reconcile(p.frame(at, p.source(at, generation=200)))
        self.assertEqual(len(writes), 1)
        self.assertEqual(c.record.transaction.deadline, original.deadline)

    async def test_current_replanned_end_does_not_block_a_new_neutral_attempt(self):
        for delta in (-5, 5):
            with self.subTest(end_delta_minutes=delta):
                c, clock, writes, _, _ = await recovery.PrecommandRecoveryTests().denied()
                prior = c.record.last_transaction
                clock[0] = at = p.NOW + timedelta(seconds=180)
                current_end = p.END + timedelta(minutes=delta)
                await c.async_reconcile(p.frame(at, p.source(at, generation=200),
                    current_slot_end=current_end, current_run_end=current_end))
                self.assertEqual(len(writes), 1)
                self.assertEqual(c.record.transaction.deadline, current_end)
                self.assertNotEqual(c.record.transaction.transaction_id, prior.transaction_id)
                self.assertEqual(prior.deadline, p.END, 'completed attempt remains immutable')

    async def test_used_retry_remains_rate_limited_after_restart_but_not_exhausted(self):
        c, clock, writes, persist, dispatch = await recovery.PrecommandRecoveryTests().denied()
        prior = replace(c.record.last_transaction, transaction_id='pvhold_retry1:completed')
        record = replace(c.record, last_transaction=prior)
        c = p.f.SupervisorActiveController(persist=persist, dispatch=dispatch,
            publish=lambda _: None, clock=lambda: clock[0],
            persisted_record=record_from_dict(record_to_dict(record)))
        await c.async_initialize()
        clock[0] = at = p.NOW + timedelta(seconds=179)
        await c.async_reconcile(p.frame(at, p.source(at, generation=199)))
        self.assertFalse(writes)
        clock[0] = at = p.NOW + timedelta(seconds=180)
        await c.async_reconcile(p.frame(at, p.source(at, generation=200)))
        self.assertEqual(len(writes), 1)
        self.assertTrue(c.record.transaction.transaction_id.startswith('pvhold_retry2:'))
        self.assertEqual(c._pv_hold_retry_status['maximum_retries'], None)

    async def test_crossing_old_deadline_does_not_bypass_cooldown(self):
        c, clock, writes, persist, dispatch = await recovery.PrecommandRecoveryTests().denied()
        prior = replace(c.record.last_transaction, deadline=p.NOW+timedelta(seconds=120))
        c = p.f.SupervisorActiveController(persist=persist, dispatch=dispatch,
            publish=lambda _: None, clock=lambda: clock[0],
            persisted_record=record_from_dict(record_to_dict(replace(c.record,last_transaction=prior))))
        await c.async_initialize()
        clock[0] = at = p.NOW + timedelta(seconds=179)
        await c.async_reconcile(p.frame(at, p.source(at, generation=199)))
        self.assertFalse(writes)
        self.assertEqual(c._pv_hold_retry_status['reason'], 'retry_spacing_active')
        clock[0] = at = p.NOW + timedelta(seconds=180)
        await c.async_reconcile(p.frame(at, p.source(at, generation=200)))
        self.assertEqual(len(writes), 1)
        self.assertEqual(c.record.transaction.deadline, p.END)
        self.assertEqual(prior.deadline, p.NOW+timedelta(seconds=120))


if __name__ == '__main__':
    unittest.main()
