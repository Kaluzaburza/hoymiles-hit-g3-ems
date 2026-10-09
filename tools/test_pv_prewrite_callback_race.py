"""installation_3 2026-10-06: a scheduled source cohort is not revoked PV consent.

Exercise the real HA adapter, executor persistence and registered trailing-edge
callback. No transport result, planner or physical readiness gate is replaced.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import unittest

import test_supervisor_sensor_contract as h


class PrewriteCallbackRaceTests(unittest.IsolatedAsyncioTestCase):
    async def test_pending_cohort_between_start_persistence_and_prepare(self):
        for outcome in ('healthy', 'planner'):
            with self.subTest(outcome=outcome):
                await self.scenario(outcome)

    async def test_scheduled_callback_cannot_hide_new_veto_or_extend_budget(self):
        for outcome in ('off', 'consent', 'bms', 'soc', 'off_grid', 'topology',
                        'missing_fc03', 'changed_deadline', 'expired_wait', 'pending_expired',
                        'expired_after_prepare'):
            with self.subTest(outcome=outcome):
                await self.scenario(outcome)

    async def scenario(self, outcome):
        hass, entry, _, sensor = h.environment()
        writes, transitions, pending = [], [], []
        deadline = h.NOW + timedelta(hours=2)

        def store(key, value, attrs=None):
            entity_id = h._source_entity_id(h.SENSOR._SOURCE_BY_KEY[key], entry.entry_id)
            hass.states.values[entity_id] = h.FakeState(str(value), attrs, h.CLOCK['now'])

        for key, value in {
            'supervisor_mode': 'Active', 'allow_rce': 'on', 'rce_enabled': 'on',
            'pv_charge_delay_enabled': 'on', 'ems_execution_ready': 'on',
            'rce_control_data_ready': 'on', 'rce_price_above_threshold': 'on',
            'rce_reserve_ready': 'on', 'battery_soc': 14,
            'bms_voltage': 52, 'bms_max_discharge_current': 100,
            'bms_max_charge_current': 100, 'ems_generation': 67747,
            'self_use_soc_readback': 10, 'backup_soc_readback': 90,
            'charge_soc_readback': 65, 'charge_power_ems_readback': 80,
            'discharge_soc_readback': 60, 'discharge_power_readback': 80,
            'gcf_enable_readback': 1, 'gcf_export_limit_readback': 100,
        }.items():
            store(key, value)
        plan = {**h._plan_attributes('rce_plan'),
            'pv_charge_delay_execution_ready': True,
            'pv_charge_delay_end': deadline.isoformat(),
            'rce_today_data_fresh': True, 'forecast_today_data_fresh': True,
            'soc_data_fresh': True, 'gcf_execution_data_fresh': True}
        store('rce_plan', 'ready', plan)

        async def drain():
            for _ in range(120):
                await asyncio.sleep(0)
                if sensor._controller_task is None and sensor._pending_active_frame is None:
                    return
            raise AssertionError('adapter drain did not terminate')

        async def dispatch(write):
            writes.append(write)

        await sensor.add_to_platform_finish()
        controller = sensor._controller
        controller._clock = lambda: h.CLOCK['now']
        controller._dispatch = dispatch
        controller._control_lease_valid_until = None
        persist_original = controller._persist

        async def persist(record):
            transitions.append((record.state.value, record.reason.value))
            if (outcome == 'expired_after_prepare'
                and record.state is h.SENSOR.ActiveState.STARTING
                and record.transaction.prewrite_snapshot is not None):
                h.CLOCK['now'] = h.NOW + timedelta(seconds=31)
            if record.state is h.SENSOR.ActiveState.STARTING and not pending:
                self.assertIsNone(record.transaction.prewrite_snapshot)
                self.assertIsNone(record.transaction.command_sent_at)
                self.assertIsNotNone(record.transaction.expected_readback)
                # Fresh state_reported arrives while HA persists STARTING.
                if outcome == 'planner':
                    sensor._schedule_planner_callback(frozenset({'rce_plan'}))
                else:
                    sensor._schedule_cohort_callback(source='state_reported')
                pending.append(hass.delay_handles[-1])
            await persist_original(record)

        controller._persist = persist
        try:
            await drain()
            self.assertTrue(pending, transitions)
            self.assertFalse(writes)
            self.assertEqual(controller.record.state, h.SENSOR.ActiveState.STARTING,
                'A pending trailing-edge callback must not become authorization_lost')
            tx = controller.record.transaction
            original = tx.transaction_id, tx.started_at, tx.deadline, tx.command_snapshot
            if outcome == 'pending_expired':
                for second in (10, 20, 29):
                    h.CLOCK['now'] = h.NOW + timedelta(seconds=second)
                    await controller.async_reconcile(sensor._latest_active_frame)
                    self.assertEqual(controller.record.state, h.SENSOR.ActiveState.STARTING)
                    self.assertEqual(controller.record.transaction.started_at, original[1])
                h.CLOCK['now'] = h.NOW + timedelta(seconds=30)
                await controller.async_reconcile(sensor._latest_active_frame)
                self.assertFalse(writes)
                self.assertEqual(controller.record.state, h.SENSOR.ActiveState.BLOCKED)
                self.assertEqual(controller.record.transaction.deadline, deadline)
                return
            h.CLOCK['now'] += timedelta(seconds=31 if outcome == 'expired_wait' else pending[0].when)
            changes = {
                'off': ('supervisor_mode', 'Off'), 'consent': ('allow_rce', 'off'),
                'bms': ('bms_max_discharge_current', 0), 'soc': ('battery_soc', 100),
                'off_grid': ('ems_mode_readback', 3), 'topology': ('inverter_count', 0),
                'missing_fc03': ('ems_generation', 'unavailable'),
            }
            if outcome in changes:
                store(*changes[outcome])
            if outcome == 'changed_deadline':
                store('rce_plan', 'ready', {**plan,
                    'pv_charge_delay_end': (deadline + timedelta(minutes=30)).isoformat()})
            pending[0].run()
            await drain()
            if outcome not in {'healthy', 'planner'}:
                if outcome == 'off':
                    self.assertTrue(all(w.ems_block.mode.value == 0 for w in writes), transitions)
                else:
                    self.assertFalse(writes, (outcome, transitions))
                self.assertNotEqual(controller.record.state, h.SENSOR.ActiveState.WAITING_READBACK)
                tx = controller.record.transaction or controller.record.last_transaction
                self.assertEqual(tx.deadline, deadline)
                return
            self.assertEqual(len(writes), 1, transitions)
            self.assertEqual(writes[0].ems_block.mode.value, 5)
            tx = controller.record.transaction
            self.assertEqual((tx.transaction_id, tx.started_at, tx.deadline, tx.command_snapshot), original)
            self.assertEqual(controller.record.state, h.SENSOR.ActiveState.WAITING_READBACK)
        finally:
            sensor._cleanup_lifecycle()


if __name__ == '__main__':
    unittest.main()
