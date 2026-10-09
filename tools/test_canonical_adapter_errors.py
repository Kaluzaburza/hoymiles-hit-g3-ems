"""Unexpected projection failures stay closed and remain diagnosable."""
import importlib
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_supervisor_sensor_contract as fixtures

module = importlib.import_module(
    'custom_components.hoymiles_hit_modbus.supervisor_canonical_sensor')


class AdapterErrorTests(unittest.TestCase):
    def setUp(self):
        hass, entry, runtime, _ = fixtures.environment()
        timeline = SimpleNamespace(native_value='current', extra_state_attributes={})
        self.sensor = module.HoymilesSupervisorCanonicalPlanSensor(
            hass, entry, runtime,
            supervisor=SimpleNamespace(latest_active_frame=object()),
            timelines={p: timeline for p in ('rce', 'tariff', 'rcm')},
            expected_timeline=timeline)
        self.sensor._capacity_kwh = lambda: 10.

    def test_ledger_failure_keeps_diagnostics_without_repeated_tracebacks(self):
        with patch.object(module._LOGGER, 'exception') as log:
            with patch.object(module, 'build_supervisor_canonical_ledger',
                              side_effect=ValueError('fixture_failure')):
                for _ in range(3):
                    self.sensor._recompute()
                    attrs = self.sensor.extra_state_attributes
                    self.assertEqual(self.sensor.native_value, 'unavailable')
                    self.assertEqual(attrs['slots'], [])
                    self.assertTrue(attrs['output_only'])
                    self.assertEqual(attrs['blocker_code'], 'canonical_adapter_error')
                    self.assertEqual(attrs['adapter_error_type'], 'ValueError')
                    self.assertEqual(attrs['adapter_error_stage'], 'canonical_ledger')
                self.assertEqual(log.call_count, 1)
            with patch.object(module, 'build_supervisor_canonical_ledger'), \
                 patch.object(module, 'canonical_execution_ledger_to_dict'), \
                 patch.object(module, 'augment_canonical_projection_payload',
                              return_value={'output_only': True, 'slots': []}):
                self.sensor._recompute()
                self.assertEqual(self.sensor.native_value, 'current')
                self.assertNotIn('adapter_error_type', self.sensor.extra_state_attributes)
            with patch.object(module, 'build_supervisor_canonical_ledger',
                              side_effect=ValueError('fixture_failure')):
                self.sensor._recompute()
            self.assertEqual(log.call_count, 2, 'A new failure after recovery needs a traceback')

    def test_expected_projection_failure_reports_its_own_stage(self):
        with patch.object(module._LOGGER, 'exception'), \
             patch.object(module, 'build_supervisor_canonical_ledger'), \
             patch.object(module, 'canonical_execution_ledger_to_dict'), \
             patch.object(module, 'augment_canonical_projection_payload',
                          side_effect=TypeError('fixture_projection_failure')):
            self.sensor._recompute()
        self.assertEqual(self.sensor.native_value, 'unavailable')
        self.assertEqual(self.sensor.extra_state_attributes['adapter_error_stage'], 'expected_projection')
        self.assertEqual(self.sensor.extra_state_attributes['slots'], [])


class DisabledRcmAdapterTests(unittest.TestCase):
    def setUp(self):
        hass, entry, runtime, supervisor = fixtures.environment()
        supervisor._source_entity_ids = {
            s.key: fixtures._source_entity_id(s, entry.entry_id)
            for s in fixtures.SENSOR.SUPERVISOR_SOURCE_SPECS}
        self.frame = supervisor._fresh_active_frame()
        self.assertEqual(module.canonical_timeline_dependencies(self.frame), ('rce', 'tariff'))
        self.supervisor = SimpleNamespace(entity_id='sensor.supervisor', latest_active_frame=self.frame)
        self.timelines = {
            p: SimpleNamespace(entity_id='sensor.'+p, native_value='current',
                               extra_state_attributes={'generated_at': fixtures.NOW.isoformat()})
            for p in ('rce', 'tariff', 'rcm')}
        self.sensor = module.HoymilesSupervisorCanonicalPlanSensor(
            hass, entry, runtime, supervisor=self.supervisor, timelines=self.timelines,
            expected_timeline=SimpleNamespace(native_value='unavailable', extra_state_attributes=None))
        self.sensor._capacity_kwh = lambda: 10.
        self.hass = hass

    def recompute(self):
        # The pure trajectory/physical contracts have separate real-data tests.
        # Here exercise the adapter's source gates, payload filtering and timers.
        with patch.object(module.dt_util, 'parse_datetime', datetime.fromisoformat, create=True), \
             patch.object(module, 'build_supervisor_canonical_ledger') as build, \
             patch.object(module, 'canonical_execution_ledger_to_dict'), \
             patch.object(module, 'augment_canonical_projection_payload',
                          return_value={'output_only': True, 'slots': []}):
            self.sensor._recompute()
        return build

    def test_disabled_pending_unavailable_or_malformed_rcm_does_not_block_adapter(self):
        for state in ('pending', 'unavailable', 'current'):
            with self.subTest(state=state):
                self.timelines['rcm'].native_value = state
                self.timelines['rcm'].extra_state_attributes = None
                build = self.recompute()
                self.assertEqual(self.sensor.native_value, 'current')
                self.assertEqual(set(build.call_args.kwargs['timelines']), {'rce', 'tariff'})
                self.assertEqual(self.sensor.extra_state_attributes['excluded_timeline_policies'],
                                 {'rcm': 'disabled_by_user_and_inactive'})
                self.assertEqual(self.sensor._source_states()['rcm'], state)
                handles = self.hass.active_delays()
                self.assertEqual(len(handles), 1)
                self.assertAlmostEqual(handles[0].when, 150.1)

    def test_reenable_invalidates_even_unchanged_supervisor_publication(self):
        self.recompute()
        self.timelines['rcm'].native_value = 'pending'
        self.supervisor.latest_active_frame = replace(
            self.frame, rcm=replace(self.frame.rcm, enabled=True))
        state = fixtures.FakeState('Active', {})
        event = SimpleNamespace(data={'entity_id':'sensor.supervisor',
                                      'old_state':state, 'new_state':state})
        self.assertTrue(self.sensor._source_event_changes_plan(event))
        build = self.recompute()
        build.assert_not_called()
        self.assertEqual(self.sensor.native_value, 'pending')
        self.assertFalse(self.sensor.extra_state_attributes['result_current'])
        self.assertFalse(self.hass.active_delays())

    def test_required_source_pending_is_still_pending(self):
        for policy in ('rce', 'tariff'):
            with self.subTest(policy=policy):
                self.timelines[policy].native_value = 'pending'
                self.recompute().assert_not_called()
                self.assertEqual(self.sensor.native_value, 'pending')
                self.timelines[policy].native_value = 'current'


if __name__ == '__main__':
    unittest.main()
