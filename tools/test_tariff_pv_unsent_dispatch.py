"""Production HA lease adapter races; local transport only, no inverter I/O."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest

import test_tariff_pending_dispatch_race as t
import test_pv_charge_delay_settling as pv


class Harness:
    def __init__(self, first):
        self.hass, entry, runtime, self.sensor = t.adapter.environment()
        self.sensor._pause_state = 'off'
        self.sensor._source_entity_ids = {
            spec.key: t.adapter._source_entity_id(spec, entry.entry_id)
            for spec in t.adapter.SENSOR.SUPERVISOR_SOURCE_SPECS}
        source = SimpleNamespace(entry_id='esphome-source', domain='esphome',
                                 data={'device_name': 'source-node'})
        runtime.source_device.config_entry_id = source.entry_id
        self.hass.config_entries = SimpleNamespace(async_get_entry=lambda key: source if key == source.entry_id else None)
        names = {'source_node_ems_supervisor_control_lease_challenge',
                 'source_node_ems_supervisor_write_complete_block_leased',
                 'source_node_ems_supervisor_renew_control_lease'}
        names.update(f'source_node_{family.value}' for family in t.adapter.SENSOR.AtomicWriteFamily)
        self.services = t.LeaseServices(names, reject_arm_calls=set())
        self.hass.services = self.services
        self.sensor._assert_single_transport_instance = lambda: None
        self.hass.states.values['input_select.hoymiles_ems_supervisor_mode'] = t.adapter.FakeState('Active')
        self.now = first.now
        t.adapter.CLOCK['now'] = self.now
        self.current = first
        self.sensor._fresh_active_frame = lambda: self.current
        self.records = []
        async def persist(record): self.records.append(record)
        self.controller = t.fixture.SupervisorActiveController(
            persist=persist, dispatch=self.sensor._async_dispatch_atomic_write,
            publish=lambda _: None, clock=lambda: self.now,
            frame_resampler=self.sensor._current_dispatch_frame,
            retarget_resume_frame=self.sensor._retarget_resume_frame,
            retarget_pending_context=self.sensor._retarget_pending_context,
            retarget_replan_pending=self.sensor._retarget_replan_pending)
        self.sensor._controller = self.controller
        self.sensor._latest_active_frame = first

    def frame(self, frame):
        self.now = frame.now
        t.adapter.CLOCK['now'] = self.now
        self.current = self.sensor._latest_active_frame = frame

    async def tick(self): await self.controller.async_reconcile(self.current)

    def close(self):
        self.sensor._cancel_control_lease_callback()
        self.sensor._cancel_planner_callback()


class DispatchTests(unittest.IsolatedAsyncioTestCase):
    async def prepared_pv_callback(self, callback='cohort'):
        f = pv.f
        h = Harness(f.frame(f.NOW, f.source(f.NOW)))
        self.addCleanup(h.close)
        persist = h.controller._persist
        injected = False

        async def persist_with_callback(record):
            nonlocal injected
            await persist(record)
            tx = record.transaction
            if (not injected and record.state is f.ActiveState.STARTING
                    and tx is not None and tx.prewrite_snapshot is not None
                    and tx.command_sent_at is None):
                injected = True
                if callback == 'cohort':
                    h.sensor._cohort_cancel = lambda: None
                    h.sensor._cohort_pending_source = 'state_reported'
                else:
                    h.sensor._planner_cancel = lambda: None
                await asyncio.sleep(0)

        h.controller._persist = persist_with_callback
        await h.controller.async_initialize()
        await h.tick()
        self.assertTrue(injected)
        self.assertEqual(h.controller.record.state, f.ActiveState.STARTING,
                         'A scheduled callback after prepare must not abandon an unsent PV start')
        self.assertFalse(h.services.calls)
        self.assertIsNone(h.controller.record.transaction.command_sent_at)
        return h

    async def test_pv_callback_after_prepare_waits_then_reprepares_without_new_attempt(self):
        for callback in ('cohort', 'planner'):
            with self.subTest(callback=callback):
                h = await self.prepared_pv_callback(callback)
                f = pv.f
                before = h.controller.record.transaction
                at = f.NOW + timedelta(seconds=2)
                h.sensor._cohort_cancel = h.sensor._planner_cancel = None
                h.sensor._cohort_pending_source = None
                h.frame(f.frame(at, f.source(at, generation=11)))
                await h.tick()
                tx = h.controller.record.transaction
                self.assertEqual(h.controller.record.state, f.ActiveState.WAITING_READBACK)
                self.assertEqual(h.services.arm_calls, 1)
                self.assertEqual(h.services.challenge_calls, 1)
                self.assertEqual(tx.transaction_id, before.transaction_id)
                self.assertEqual(tx.started_at, before.started_at)
                self.assertEqual(tx.deadline, before.deadline)
                self.assertEqual(tx.command_snapshot, before.command_snapshot)
                self.assertEqual(tx.prewrite_snapshot.ems_generation, 11)
                self.assertEqual(tx.command_sent_at, at)
                self.assertFalse(tx.transaction_id.startswith('pvhold_retry1:'))

    async def test_pv_prepared_callback_keeps_original_dispatch_budget_and_vetoes(self):
        for fault in ('timeout', 'permission', 'bms', 'baseline', 'stale', 'missing', 'owner', 'deadline'):
            with self.subTest(fault=fault):
                h = await self.prepared_pv_callback()
                f = pv.f
                before = h.controller.record.transaction
                for second in (10, 20, 29):
                    at = f.NOW + timedelta(seconds=second)
                    h.frame(f.frame(at, f.source(at, generation=10+second)))
                    await h.tick()
                    self.assertEqual(h.controller.record.state, f.ActiveState.STARTING)
                    self.assertFalse(h.services.calls)
                    self.assertEqual(h.controller.record.transaction.started_at, before.started_at)
                h.sensor._cohort_cancel = None
                h.sensor._cohort_pending_source = None
                at = f.NOW + timedelta(seconds=30 if fault == 'timeout' else 29.5)
                src = f.source(at, generation=50)
                if fault == 'bms': src = replace(src, bms_max_charge_current_a=0.)
                if fault == 'baseline': src = replace(src, backup_soc_percent=70.)
                if fault == 'stale': src = replace(src, full_block_generation_at=f.NOW)
                if fault == 'missing': src = replace(src, full_block_execution_ready=False)
                frame = f.frame(at, src, allowed=fault != 'permission')
                if fault == 'owner': frame = replace(frame, context=replace(frame.context, owner_conflict=True))
                if fault == 'deadline':
                    at = before.deadline
                    frame = replace(frame, now=at)
                h.frame(frame)
                await h.tick()
                self.assertEqual(h.services.arm_calls, 0, fault)
                if fault in {'timeout', 'deadline'}:
                    # The existing executor deadline may run neutral restore.
                    # It must never issue a delayed Mode 5 start or new lease.
                    self.assertTrue(all(call[2].get('mode_code') == 0
                                        for call in h.services.calls), fault)
                else:
                    self.assertFalse(h.services.calls, fault)
                self.assertIsNone(h.sensor._control_lease_client.handle)
                self.assertNotEqual(h.controller.record.state, f.ActiveState.STARTING)

    async def test_pv_prepared_wait_cannot_reset_timeout_during_persistence(self):
        h = await self.prepared_pv_callback()
        f = pv.f
        h.sensor._cohort_cancel = None
        h.sensor._cohort_pending_source = None
        at = f.NOW + timedelta(seconds=29)
        h.frame(f.frame(at, f.source(at, generation=50)))
        persist = h.controller._persist

        async def slow_persist(record):
            await persist(record)
            if record.transaction and record.transaction.prewrite_snapshot is not None:
                h.now = f.NOW + timedelta(seconds=30.1)
                t.adapter.CLOCK['now'] = h.now
        h.controller._persist = slow_persist
        await h.tick()
        self.assertFalse(h.services.calls)
        self.assertEqual(h.controller.record.state, f.ActiveState.BLOCKED)

    async def test_pv_defer_rejects_a_nonzero_or_ambiguous_dispatch_count(self):
        h = await self.prepared_pv_callback()
        from custom_components.hoymiles_hit_modbus.supervisor_executor import SupervisorExecutor
        prepared = next(record for record in h.records
                        if record.transaction and record.transaction.prewrite_snapshot is not None)
        for count in (1, -1, None, False):
            with self.subTest(count=count):
                executor = SupervisorExecutor(prepared)
                with self.assertRaises(ValueError):
                    executor.defer_pv_start_before_dispatch(dispatched_count=count)
                self.assertEqual(executor.record, prepared)

    async def test_pv_prepared_wait_restart_does_not_replay_start(self):
        h = await self.prepared_pv_callback()
        from custom_components.hoymiles_hit_modbus.supervisor_executor import SupervisorExecutor
        from custom_components.hoymiles_hit_modbus.supervisor_executor_codec import record_from_dict, record_to_dict
        recovered = SupervisorExecutor.recover(record_from_dict(record_to_dict(h.controller.record)))
        self.assertEqual(recovered.record.state, pv.f.ActiveState.STOPPING)
        self.assertEqual(recovered.record.transaction.transaction_id,
                         h.controller.record.transaction.transaction_id)
        self.assertIsNone(recovered.record.transaction.command_sent_at)

    async def test_tariff_publication_during_challenge_keeps_only_attested_predecessor(self):
        # Generic TARIFF contract: the executor has no provider/profile branch.
        for unsafe in (None, 'bms', 'permission', 'readback', 'deadline'):
            with self.subTest(unsafe=unsafe):
                f = t.fixture
                h = Harness(f.frame(t.NOW, f.execution_source(t.NOW), target=75, power=50,
                                    action=f.TariffAction.GRID_SUPPORT_AND_CHARGE))
                self.addCleanup(h.close)
                await h.controller.async_initialize(); await h.tick()
                at = t.NOW+timedelta(seconds=2)
                h.frame(f.frame(at, f.physical(at,target=75,power=50,battery=-5000,generation=11),
                                h.controller.record,target=75,power=50,
                                action=f.TariffAction.GRID_SUPPORT_AND_CHARGE))
                await h.tick()
                self.assertEqual(h.controller.record.state,f.ActiveState.EXECUTING)
                predecessor = h.controller.record.transaction
                handle = h.sensor._control_lease_client.handle
                at += timedelta(seconds=1)
                h.frame(f.frame(at, f.physical(at,target=75,power=50,battery=-5000,generation=12),
                                h.controller.record,target=80,power=49,
                                action=f.TariffAction.GRID_SUPPORT_AND_CHARGE))
                original = h.services.async_call
                async def race(domain,service,data,**kw):
                    result = await original(domain,service,data,**kw)
                    if service.endswith('ems_supervisor_control_lease_challenge'):
                        h.sensor._cohort_cancel = lambda: None
                        h.sensor._cohort_pending_source = 'state_reported'
                        if unsafe == 'bms':
                            h.current = replace(h.current, execution=replace(h.current.execution,bms_max_charge_current_a=1))
                        elif unsafe == 'permission':
                            h.current = replace(h.current, tariff=replace(h.current.tariff,allowed_by_user=False))
                        elif unsafe == 'readback':
                            h.current = replace(h.current, execution=replace(h.current.execution,maximum_charge_power_percent=20))
                        elif unsafe == 'deadline':
                            h.now = predecessor.deadline
                            t.adapter.CLOCK['now'] = h.now
                            h.current = replace(h.current,now=h.now)
                    return result
                h.services.async_call = race
                await h.tick()
                tx = h.controller.record.transaction
                self.assertEqual(h.services.arm_calls,1)
                self.assertEqual(tx.transaction_id,predecessor.transaction_id)
                self.assertEqual(tx.deadline,predecessor.deadline)
                self.assertEqual(tx.command_snapshot,predecessor.command_snapshot)
                if unsafe:
                    self.assertEqual(h.controller.record.state,f.ActiveState.STOPPING)
                else:
                    self.assertEqual(h.controller.record.state,f.ActiveState.EXECUTING)
                    self.assertEqual(tx.intent,predecessor.intent)
                    self.assertEqual(tx.command_sent_at,predecessor.command_sent_at)
                    self.assertIs(h.sensor._control_lease_client.handle,handle)
                    # A later complete publication may retarget, within A's deadline.
                    h.services.async_call = original
                    h.sensor._cohort_cancel = None
                    h.sensor._cohort_pending_source = None
                    await h.tick()
                    self.assertEqual(h.services.arm_calls,2)
                    self.assertEqual(h.controller.record.state,f.ActiveState.RETARGETING)
                    self.assertEqual(h.controller.record.transaction.deadline,predecessor.deadline)

    async def test_pv_generation_race_is_reprepared_once_with_original_identity(self):
        for refusal in ('once','twice','permission','baseline','unknown'):
            with self.subTest(refusal=refusal):
                f = pv.f
                h = Harness(f.frame(f.NOW,f.source(f.NOW,battery=-1500,grid=0)))
                self.addCleanup(h.close)
                original = h.services.async_call
                async def race(domain,service,data,**kw):
                    if refusal == 'unknown' and service.endswith('ems_supervisor_write_complete_block_leased'):
                        raise TimeoutError('unknown transport response')
                    result = await original(domain,service,data,**kw)
                    if service.endswith('ems_supervisor_control_lease_challenge') and (
                            h.services.challenge_calls == 1 or refusal == 'twice'):
                        at = h.now+timedelta(milliseconds=100)
                        overrides = {'backup_soc_percent':70} if refusal == 'baseline' else {}
                        h.frame(f.frame(at,f.source(at,generation=10+h.services.challenge_calls,
                                                    battery=-1500,grid=0,**overrides),
                                        allowed=refusal != 'permission'))
                    return result
                h.services.async_call = race
                await h.controller.async_initialize(); await h.tick()
                tx = h.controller.record.transaction
                prepared = next(r.transaction for r in h.records if r.transaction and r.transaction.prewrite_snapshot)
                self.assertEqual(tx.transaction_id,prepared.transaction_id)
                self.assertEqual(tx.started_at,prepared.started_at)
                self.assertEqual(tx.deadline,prepared.deadline)
                self.assertEqual(tx.command_snapshot,prepared.command_snapshot)
                self.assertLessEqual(h.services.challenge_calls,2)
                if refusal == 'once':
                    self.assertEqual(h.services.arm_calls,1)
                    self.assertEqual(h.controller.record.state,f.ActiveState.WAITING_READBACK)
                    self.assertEqual(tx.prewrite_snapshot.ems_generation,11)
                    self.assertIsNotNone(tx.command_sent_at)
                    self.assertEqual(tx.intent.command.ems_block.maximum_discharge_power_percent_4306,1)
                    self.assertEqual(tx.intent.command.ems_block.force_discharge_soc_percent_4305,61)
                elif refusal == 'unknown':
                    self.assertEqual(h.controller.record.state,f.ActiveState.FAULT)
                    self.assertEqual(h.controller.record.reason.value,'command_outcome_unknown')
                else:
                    self.assertEqual(h.services.arm_calls,0)
                    self.assertEqual(h.controller.record.state,f.ActiveState.BLOCKED)
                    self.assertIsNone(tx.command_sent_at)
                    self.assertIsNone(h.sensor._control_lease_client.handle)
                    self.assertNotEqual(h.controller.record.reason.value,'command_outcome_unknown')


if __name__ == '__main__': unittest.main()
