"""Bounded tariff support excursions through runtime/controller and HA lease gate.

Only storage, clock and transport are fake. Raw physical evidence stays strict.
"""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

import test_tariff_initial_lease_settling as lease_fixture

a, f = lease_fixture.a, lease_fixture.f
from supervisor_active_bridge import physical_verification
from supervisor_executor import VerificationStatus, ExecutionReason
from tariff_optimizer import optimize_tariff_charging
from test_tariff_optimizer import settings, ZONE


class Support:
    async def start(self, *, soc=60, deadline=None, action=f.TariffAction.GRID_SUPPORT):
        self.clock = [f.NOW]
        self.writes = []
        self.saved = []
        self.action = action
        self.deadline = deadline or f.NOW + timedelta(minutes=30)

        async def persist(_record):
            self.saved.append(_record)

        async def dispatch(write):
            self.writes.append(write)

        self.ctl = f.SupervisorActiveController(persist=persist, dispatch=dispatch,
            publish=lambda _record: None, clock=lambda: self.clock[0])
        await self.ctl.async_initialize()
        source = f.execution_source(f.NOW, battery_soc_percent=soc)
        await self.ctl.async_reconcile(f.frame(f.NOW, source, target=65,
            action=action, deadline=self.deadline))
        assert self.ctl.record.state is f.ActiveState.WAITING_READBACK
        self.target = self.writes[0].ems_block.force_charge_soc_percent_4303
        await self.step(2, battery=0 if action is f.TariffAction.GRID_SUPPORT else -5000)
        assert self.ctl.record.state is f.ActiveState.EXECUTING, self.ctl.record.reason
        self.initial = self.ctl.record.transaction
        return self

    def frame(self, seconds, *, battery=0, source_changes=None, **changes):
        self.clock[0] = f.NOW + timedelta(seconds=seconds)
        source = replace(f.physical(self.clock[0], target=self.target, battery=battery,
            generation=11+int(seconds)),
            **{'power_cohort_complete': True, **(source_changes or {})})
        return f.frame(self.clock[0], source, self.ctl.record, target=65,
            action=self.action, deadline=self.deadline, **changes)

    async def step(self, seconds, **changes):
        frame = self.frame(seconds, **changes)
        await self.ctl.async_reconcile(frame)
        return frame


class SupportFlowFilterTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_field_excursions_recover_without_write_or_false_proof(self):
        for battery, extra in (
            (211, dict(load_power_w=211, grid_power_w=0)),
            (332, {}), (-1380, {}),
        ):
            with self.subTest(battery=battery):
                s = await Support().start()
                frame = await s.step(10, battery=battery, source_changes=extra)
                raw = physical_verification(s.initial.transaction_id,
                    s.initial.intent.action, frame.execution,
                    command_sent_at=s.initial.command_sent_at, now=frame.now)
                self.assertIs(raw.status, VerificationStatus.CONTRADICTED)
                self.assertIs(s.ctl.record.state, f.ActiveState.EXECUTING)
                self.assertEqual(s.ctl.record.transaction.physical_verification,
                    s.initial.physical_verification, 'excursion is not a new good proof')
                diag = s.ctl.recorder_attributes()['tariff_support_flow_filter']
                self.assertTrue(diag['active'])
                self.assertEqual(diag['budget_seconds'], 60)
                self.assertEqual(diag['raw_status'], 'contradicted')
                self.assertFalse(diag['lease_renewal_allowed'])
                health = a.SENSOR._execution_health_contract({
                    **s.ctl.recorder_attributes(),
                    'execution_last_valid_read_at': frame.execution.full_block_generation_at.isoformat(),
                    'execution_physical_mode_fresh': True,
                }, now=frame.now)
                self.assertEqual(health['control_status'], 'recovering')
                self.assertEqual(health['reason'], 'tariff_support_flow_filter')
                self.assertEqual(s.ctl.tariff_support_flow_filter_deadline,
                    s.initial.physical_verification.observed_at + timedelta(seconds=60))
                await s.step(59, input_revision=2)
                tx = s.ctl.record.transaction
                self.assertIs(s.ctl.record.state, f.ActiveState.EXECUTING)
                self.assertEqual(tx.transaction_id, s.initial.transaction_id)
                self.assertEqual(tx.command_sent_at, s.initial.command_sent_at)
                self.assertEqual(tx.deadline, s.initial.deadline)
                self.assertGreater(tx.physical_verification.observed_at,
                    s.initial.physical_verification.observed_at)
                self.assertIsNone(s.ctl.tariff_support_flow_filter_deadline)
                self.assertEqual(len(s.writes), 1)

    async def test_persistent_excursions_and_completed_replans_cannot_slide_expiry(self):
        s = await Support().start()
        end = s.initial.physical_verification.observed_at + timedelta(seconds=60)
        for revision, seconds in enumerate((3, 20, 40, 60), 2):
            await s.step(seconds, battery=332, input_revision=revision)
            self.assertIs(s.ctl.record.state, f.ActiveState.EXECUTING)
            self.assertEqual(s.ctl.tariff_support_flow_filter_deadline, end)
            self.assertLessEqual(s.ctl.execution_watchdog[1], end)
            self.assertEqual(s.ctl.record.transaction.command_sent_at, s.initial.command_sent_at)
        await s.step((end-f.NOW).total_seconds(), battery=332, input_revision=6)
        self.assertIs(s.ctl.record.state, f.ActiveState.RESTORING)
        self.assertIn(ExecutionReason.PHYSICAL_CONTRADICTION, [r.reason for r in s.saved])
        self.assertEqual([w.ems_block.mode.value for w in s.writes], [4, 0])

    async def test_duplicate_cohort_and_incomplete_cohort_do_not_stack_grace_periods(self):
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                s = await Support().start()
                frame = await s.step(10 if incomplete else 50, battery=332)
                self.assertIs(s.ctl.record.state, f.ActiveState.EXECUTING)
                # Refresh FC03/readiness, retain exactly the old power cohort.
                changes = {key: getattr(frame.execution, key) for key in (
                    'grid_power_observed_at', 'battery_power_observed_at',
                    'pv_power_observed_at', 'load_power_observed_at')}
                if incomplete:
                    changes['power_cohort_complete'] = False
                end = s.initial.physical_verification.observed_at + timedelta(seconds=60)
                if incomplete:
                    await s.step(20, battery=332, source_changes=changes)
                    self.assertIs(s.ctl.record.state, f.ActiveState.EXECUTING)
                    # Missing cohorts retain their original, shorter 30 s
                    # watchdog; the excursion filter adds no telemetry grace.
                    end = s.initial.physical_verification.observed_at + timedelta(seconds=30)
                    self.assertLessEqual(s.ctl.execution_watchdog[1], end)
                await s.step((end-f.NOW).total_seconds(), battery=332, source_changes=changes)
                self.assertIsNot(s.ctl.record.state, f.ActiveState.EXECUTING)
                self.assertEqual(len(s.writes), 2)

    async def test_safety_vetoes_bypass_filter(self):
        stale = f.NOW - timedelta(minutes=10)
        cases = {
            'fc03_mismatch': ({'force_charge_soc_percent': 70}, {}),
            'stale_fc03': ({'full_block_generation_at': stale}, {}),
            'stale_power': ({'load_power_observed_at': f.NOW+timedelta(seconds=5)}, {}),
            'missing_power': ({'load_power_w': None}, {}),
            'incoherent_balance': ({'load_power_w': 9000}, {}),
            'export': ({'grid_power_w': 300, 'battery_power_w': 1500}, {}),
            'zero_bms': ({'bms_max_charge_current_a': 0}, {}),
            'off_grid': ({'physical_mode_code': 1}, {}),
            'withdrawn_plan': ({}, {'current_slot_planned': False}),
            'consent_off': ({}, {'allowed_by_user': False}),
        }
        for name, (source_changes, changes) in cases.items():
            with self.subTest(veto=name):
                s = await Support().start()
                await s.step(3, battery=332)
                await s.step(44, battery=332, source_changes=source_changes, **changes)
                self.assertIsNot(s.ctl.record.state, f.ActiveState.EXECUTING, name)
                self.assertIsNone(getattr(s.ctl, 'tariff_support_flow_filter_deadline', None))
                if name == 'off_grid':
                    self.assertEqual(len(s.writes), 1, 'no Self-Use over Off-Grid')

    async def test_original_deadline_wins_over_filter(self):
        s = await Support().start(deadline=f.NOW+timedelta(seconds=600))
        await s.step(560)
        await s.step(580, battery=332)
        self.assertLessEqual(s.ctl.execution_watchdog[1], s.initial.deadline)
        await s.step(600, battery=332)
        self.assertIsNot(s.ctl.record.state, f.ActiveState.EXECUTING)
        self.assertIn(ExecutionReason.DEADLINE_REACHED, [r.reason for r in s.saved])
        self.assertEqual(len(s.writes), 2)

    async def test_charging_continuation_stays_strict(self):
        for action in (f.TariffAction.BATTERY_CHARGE, f.TariffAction.GRID_SUPPORT_AND_CHARGE):
            with self.subTest(action=action):
                s = await Support().start(action=action)
                await s.step(10, battery=332)
                self.assertIsNot(s.ctl.record.state, f.ActiveState.EXECUTING)

    async def test_late_good_sample_cannot_erase_expired_excursion(self):
        s = await Support().start()
        await s.step(10, battery=332)
        await s.step(63)
        self.assertIsNot(s.ctl.record.state, f.ActiveState.EXECUTING)
        self.assertEqual(len(s.writes), 2)

    async def test_restart_does_not_restore_filter_authority(self):
        s = await Support().start()
        await s.step(10, battery=332)
        persisted = f.record_from_dict(f.record_to_dict(s.ctl.record))

        async def ignore(_value):
            pass

        recovered = f.SupervisorActiveController(persist=ignore, dispatch=ignore,
            publish=lambda _record: None, clock=lambda: s.clock[0], persisted_record=persisted)
        await recovered.async_initialize()
        self.assertIsNot(recovered.record.state, f.ActiveState.EXECUTING)
        self.assertIsNone(recovered.tariff_support_flow_filter_deadline)

    async def test_existing_lease_expiry_wins_and_missing_reports_do_not_extend_wait(self):
        for lost_reports in (False, True):
            with self.subTest(lost_reports=lost_reports):
                s = await Support().start()
                lease_end = f.NOW + timedelta(seconds=120)
                s.ctl._control_lease_valid_until = lambda: lease_end
                await s.step(102)
                await s.step(110, battery=332)
                self.assertIs(s.ctl.record.state, f.ActiveState.EXECUTING)
                self.assertEqual(s.ctl.tariff_support_flow_filter_deadline, lease_end)
                changes = {'load_power_w': None} if lost_reports else {}
                await s.step(111 if lost_reports else 120, battery=332, source_changes=changes)
                self.assertIsNot(s.ctl.record.state, f.ActiveState.EXECUTING)
                self.assertEqual(len(s.writes), 2)

    async def test_current_excursion_never_renews_cached_confirmed_lease(self):
        s = await Support().start()
        _, _, _, sensor = a.environment()
        sensor._pause_state = 'off'
        sensor._controller = s.ctl
        client = sensor._control_lease_client
        block = a.SENSOR._control_lease_block(s.initial.intent.command.ems_block)
        loop = asyncio.get_running_loop()
        mono = loop.time()
        arm = client.prepare_arm(transaction_id=s.initial.transaction_id,
            hard_deadline=s.initial.deadline, block=block, challenge_nonce='00000001',
            now_wall=f.NOW, now_monotonic=mono)
        self.assertTrue(client.accept_arm(arm, lease_fixture.ack('accepted')))
        original_handle = client.handle
        esp = lease_fixture.FirmwareLeaseModel()
        self.assertTrue(esp.arm(block, transaction=s.initial.transaction_id,
            generation=1, hard_seconds=1800))
        esp.observe(block)
        for elapsed in (10, 20, 40, 60):
            frame = s.frame(elapsed, battery=332)
            sensor._latest_active_frame = frame
            a.CLOCK['now'] = frame.now
            esp.advance(elapsed-esp.now)
            # Sensor can see the source event before the serialized controller.
            with patch.object(loop, 'time', return_value=mono+elapsed):
                self.assertIsNone(sensor._control_lease_renewal_evidence())
            await s.ctl.async_reconcile(frame)
            with patch.object(loop, 'time', return_value=mono+elapsed):
                evidence = sensor._control_lease_renewal_evidence()
            self.assertIsNone(evidence)
            self.assertIsNone(client.prepare_renew(authorized=evidence is not None,
                snapshot_generation=31, now_wall=frame.now, now_monotonic=mono+elapsed))
            self.assertEqual(client.handle, original_handle)
            self.assertIs(s.ctl.record.state, f.ActiveState.EXECUTING)
        frame = await s.step(61)
        sensor._latest_active_frame = frame
        a.CLOCK['now'] = frame.now
        esp.advance(1)
        with patch.object(loop, 'time', return_value=mono+61):
            evidence = sensor._control_lease_renewal_evidence()
        self.assertIsNotNone(evidence, sensor._control_lease_gate)
        request = client.prepare_renew(authorized=True, snapshot_generation=evidence[0],
            now_wall=frame.now, now_monotonic=mono+61,
            authorization_deadline=sensor._control_lease_authorization_deadline)
        self.assertIsNotNone(request)
        self.assertTrue(esp.renew(authorized=True, sequence=request['sequence'],
            authorization_seconds=request['authorization_seconds']))
        self.assertTrue(client.accept_renew(lease_fixture.ack('renewed', '00000003'),
            request=request, now_monotonic=mono+61.001))
        self.assertEqual(esp.state, 'confirmed')
        self.assertEqual(len(s.writes), 1)

    async def test_minus_two_target_latches_across_soc_noise_and_replans(self):
        for soc in (60, 60.9, 65):
            with self.subTest(soc=soc):
                s = await Support().start(soc=soc)
                self.assertEqual(s.target, int(soc)-2)
                for revision, (second, live_soc) in enumerate(((4, soc-1), (10, soc+1), (30, soc)), 2):
                    await s.step(second, input_revision=revision,
                        source_changes={'battery_soc_percent': live_soc})
                    self.assertIs(s.ctl.record.state, f.ActiveState.EXECUTING)
                    self.assertEqual(s.ctl.record.transaction.intent.command.ems_block.force_charge_soc_percent_4303,
                        int(soc)-2)
                self.assertEqual(len(s.writes), 1)


class OptimizerTargetTests(unittest.TestCase):
    def test_support_target_and_charging_target_are_separate(self):
        at = datetime(2026, 8, 6, 14, tzinfo=ZONE)
        loads = {at.replace(hour=h, minute=m): 9/14 for h in range(15,22) for m in (0,30)}
        loads[at] = loads[at+timedelta(minutes=30)] = .5
        base = settings(at, battery_soc_percent=60, reserve_soc_percent=20,
            maximum_soc_percent=100, average_daily_load_kwh=0, average_night_load_kwh=0,
            load_by_slot_kwh=loads, current_load_power_kw=1, current_pv_power_kw=0,
            current_battery_power_kw=1, charge_power_kw=6, requested_charge_power_kw=6,
            battery_charge_power_kw=6, charge_efficiency_percent=100,
            discharge_efficiency_percent=100, minimum_saving_pln_kwh=0, horizon_days=3)
        first = optimize_tariff_charging(base)
        self.assertEqual(first.current_action, 'grid_support')
        self.assertAlmostEqual(first.target_soc_percent, 58)
        charge = optimize_tariff_charging(replace(base, now=at+timedelta(minutes=30)))
        self.assertEqual(charge.current_action, 'grid_support_and_charge')
        self.assertAlmostEqual(charge.target_soc_percent, 65)


if __name__ == '__main__':
    unittest.main(verbosity=2)
