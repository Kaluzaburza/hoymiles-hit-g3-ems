"""installation_3: a denied pre-command start must not require a fictitious restore."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest

import test_pv_charge_delay_control as p
from supervisor_executor import ExecutionReason, RollbackStatus
from supervisor_executor_codec import record_from_dict, record_to_dict


class PrecommandRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_authorization_aborted_before_prepare_can_retry_after_cooldown(self):
        writes, clock, frames = [], [p.NOW], [None]
        async def persist(_): pass
        async def dispatch(write): writes.append(write)
        c = p.f.SupervisorActiveController(persist=persist, dispatch=dispatch,
            publish=lambda _: None, clock=lambda: clock[0], frame_resampler=lambda: frames[0])
        await c.async_initialize()
        await c.async_reconcile(p.frame(p.NOW, p.source(p.NOW)))
        self.assertEqual(c.record.reason, ExecutionReason.AUTHORIZATION_LOST)
        self.assertIsNone(c.record.transaction.prewrite_snapshot)
        self.assertIsNone(c.record.transaction.command_sent_at)
        self.assertIsNotNone(c.record.transaction.expected_readback)
        clock[0] = at = p.NOW + timedelta(seconds=2)
        frames[0] = p.frame(at, p.source(at, generation=12))
        await c.async_reconcile(frames[0])
        self.assertEqual(c.record.state, p.ActiveState.IDLE)
        original = c.record.last_transaction
        # The exact persisted installation_3 shape is enough; null timestamps alone
        # must not authorize an ambiguous transport or an altered neutral block.
        at = p.NOW + timedelta(seconds=181)
        fr = p.frame(at, p.source(at, generation=81))
        settings = p.settings_from_execution_source(fr.execution)
        gates = c._gates(fr)
        intent = p.f.controller_module.build_actuator_intent(fr.decision, fr.candidates,
            settings, rce=fr.rce, tariff=fr.tariff, rcm=fr.rcm, now=at)
        for change in (
            dict(prewrite_snapshot=original.command_snapshot),
            dict(command_sent_at=p.NOW), dict(lease_identity=('lease', original.transaction_id, 1)),
            dict(expected_readback=None),
            dict(expected_readback=replace(original.expected_readback, base_ems_generation=999)),
            dict(reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN),
            dict(rollback_status=RollbackStatus.PENDING),
            dict(master_stop_requested=True),
        ):
            with self.subTest(ambiguous=change):
                probe = SimpleNamespace(record=replace(c.record,
                    last_transaction=replace(original, **change)))
                self.assertFalse(p.f.SupervisorActiveController._pv_hold_retry_start_allowed(
                    probe, intent, settings, gates, now=at))
        c = p.f.SupervisorActiveController(persist=persist, dispatch=dispatch,
            publish=lambda _: None, clock=lambda: clock[0], frame_resampler=lambda: frames[0],
            persisted_record=record_from_dict(record_to_dict(c.record)))
        await c.async_initialize()
        clock[0] = at = p.NOW + timedelta(seconds=181)
        frames[0] = p.frame(at, p.source(at, generation=81))
        await c.async_reconcile(frames[0])
        self.assertEqual(len(writes), 1, 'No I/O before prepare must not demand a fictitious restore')
        self.assertEqual(c.record.transaction.deadline, original.deadline)
        self.assertTrue(c.record.transaction.transaction_id.startswith('pvhold_retry1:'))

    async def denied(self):
        writes, persisted, clock = [], [], [p.NOW]
        async def persist(record): persisted.append(record)
        async def dispatch(write): writes.append(write)
        controller = p.f.SupervisorActiveController(
            persist=persist, dispatch=dispatch, publish=lambda _: None,
            clock=lambda: clock[0])
        await controller.async_initialize()
        # A qualified plan still needs device readiness (positive BMS charge
        # capability). Power cohorts are diagnostic only.
        # The old no-surplus/flow veto has the same persisted transaction shape.
        await controller.async_reconcile(p.frame(p.NOW,
            replace(p.source(p.NOW), bms_max_charge_current_a=0.)))
        self.assertEqual(controller.record.state, p.ActiveState.BLOCKED)
        self.assertEqual(controller.record.reason, ExecutionReason.DIRECTION_UNAVAILABLE)
        clock[0] = at = p.NOW + timedelta(seconds=2)
        await controller.async_reconcile(p.frame(at, p.source(at, generation=12)))
        self.assertEqual(controller.record.state, p.ActiveState.IDLE)
        self.assertFalse(writes)
        previous = controller.record.last_transaction
        self.assertIsNone(previous.command_sent_at)
        self.assertIsNone(previous.prewrite_snapshot)
        self.assertIsNone(previous.expected_readback)
        self.assertIsNone(previous.lease_identity)
        self.assertEqual(previous.rollback_status, RollbackStatus.NOT_REQUIRED)
        return controller, clock, writes, persist, dispatch

    async def test_denied_precommand_recovers_after_cooldown_and_restart(self):
        c, clock, writes, persist, dispatch = await self.denied()
        original = c.record.last_transaction
        c = p.f.SupervisorActiveController(persist=persist, dispatch=dispatch,
            publish=lambda _: None, persisted_record=record_from_dict(record_to_dict(c.record)),
            clock=lambda: clock[0])
        await c.async_initialize()
        for second in (3, 30, 179):
            clock[0] = at = p.NOW + timedelta(seconds=second)
            await c.async_reconcile(p.frame(at, p.source(at, generation=second+20)))
            self.assertFalse(writes)
        clock[0] = at = p.NOW + timedelta(seconds=181)
        await c.async_reconcile(p.frame(at, replace(p.source(at, generation=81),
            power_cohort_complete=False, battery_power_w=None, grid_power_w=None,
            load_power_w=None, pv_power_w=None, bms_battery_power_w=None)))
        self.assertEqual(len(writes), 1,
            'Unsent direction veto wrongly demands a physical restore until noon')
        self.assertEqual(c.record.state, p.ActiveState.WAITING_READBACK)
        self.assertTrue(c.record.transaction.transaction_id.startswith('pvhold_retry1:'))
        self.assertEqual(c.record.transaction.deadline, original.deadline)
        self.assertEqual(c.record.transaction.command_sent_at, at)
        accepted = c.record.transaction
        for second in (183, 199, 210, 240, 270):
            clock[0] = tick = p.NOW + timedelta(seconds=second)
            await c.async_reconcile(p.frame(tick,
                p.source(tick, mode=5, generation=100+second),
                pending=second < 210, active=second >= 210, input_revision=second))
        self.assertEqual(c.record.state, p.ActiveState.EXECUTING)
        self.assertEqual(c.record.transaction.transaction_id, accepted.transaction_id)
        self.assertEqual(c.record.transaction.deadline, original.deadline)
        self.assertEqual(len(writes), 1)
        clock[0] = p.END
        await c.async_reconcile(p.frame(p.END, p.source(p.END, mode=5, generation=1400), active=True))
        self.assertEqual(len(writes), 2)
        self.assertEqual(writes[-1].ems_block.mode, p.EmsMode.SELF_USE)
        clock[0] = tick = p.END + timedelta(seconds=5)
        await c.async_reconcile(p.frame(tick, p.source(tick, generation=1405)))
        self.assertEqual(c.record.state, p.ActiveState.IDLE)
        self.assertEqual(c.record.owner, p.ExecutionOwner.NONE)
        self.assertEqual(c.record.last_transaction.rollback_status, RollbackStatus.CONFIRMED)

    async def test_ambiguous_attempt_cannot_use_no_command_exception(self):
        c, _, _, _, _ = await self.denied()
        previous = c.record.last_transaction
        at = p.NOW + timedelta(seconds=181)
        fr = p.frame(at, p.source(at, generation=81))
        settings = p.settings_from_execution_source(fr.execution)
        gates = c._gates(fr)
        intent = p.f.controller_module.build_actuator_intent(fr.decision, fr.candidates,
            settings, rce=fr.rce, tariff=fr.tariff, rcm=fr.rcm, now=at)
        from supervisor_executor import ExpectedReadback
        uncertain_shapes = (
            dict(command_sent_at=p.NOW),
            dict(prewrite_snapshot=previous.command_snapshot),
            dict(expected_readback=ExpectedReadback.from_command(settings, intent.command)),
            dict(lease_identity=('lease', previous.transaction_id, 1)),
            dict(rollback_status=RollbackStatus.PENDING),
            dict(reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN),
            dict(restore_sent_at=p.NOW),
            dict(master_stop_requested=True),
            dict(owner=p.ExecutionOwner.RCE),
            dict(command_snapshot=None),
        )
        for change in uncertain_shapes:
            with self.subTest(change=change):
                # Exercise the proof classifier with deliberately incomplete
                # evidence: no transport callback or physical restore is mocked.
                probe = SimpleNamespace(record=replace(c.record,
                    last_transaction=replace(previous, **change)))
                allowed = p.f.SupervisorActiveController._pv_hold_retry_start_allowed(
                    probe, intent, settings, gates, now=at)
                self.assertFalse(allowed)
        changed = replace(settings, ems_block=replace(settings.ems_block,
            backup_soc_percent_4302=settings.ems_block.backup_soc_percent_4302-1))
        self.assertFalse(c._pv_hold_retry_start_allowed(intent, changed, gates, now=at))

    async def test_current_vetoes_still_deny_the_unsent_retry(self):
        for fault in ('old_fc03', 'missing_fc03', 'bms', 'mode5', 'owner', 'consent'):
            with self.subTest(fault=fault):
                c, clock, writes, _, _ = await self.denied()
                clock[0] = at = p.NOW + timedelta(seconds=181)
                src = p.source(at, generation=81)
                if fault == 'old_fc03':
                    src = replace(src, full_block_generation_at=p.NOW-timedelta(seconds=1))
                elif fault == 'missing_fc03':
                    src = replace(src, full_block_execution_ready=False)
                elif fault == 'bms':
                    src = replace(src, bms_max_charge_current_a=0.)
                elif fault == 'mode5':
                    src = p.source(at, mode=5, generation=81)
                fr = p.frame(at, src, allowed=fault != 'consent')
                if fault == 'owner':
                    fr = replace(fr, context=replace(fr.context, owner_conflict=True))
                await c.async_reconcile(fr)
                self.assertFalse(writes, fault)


if __name__ == '__main__':
    unittest.main()
