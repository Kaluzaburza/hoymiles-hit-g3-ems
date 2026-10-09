"""A disabled, idle RCEm must not erase current BUY/SELL chart projections."""
from dataclasses import replace
from datetime import timedelta
import unittest

import test_supervisor_canonical_runtime as F
import supervisor_canonical_runtime as C


def disabled_frame():
    frame = F.frame()
    frame.rcm = replace(frame.rcm, allowed_by_user=False, enabled=False,
                        export_control_enabled=False, pre_discharge_enabled=False,
                        absorb_active=False, export_active=False, pre_discharge_active=False)
    frame.candidates = tuple(
        replace(c, allowed_by_user=False, enabled=False, available=False,
                result_current=False, recalculation_pending=True)
        if c.policy_id is F.PolicyId.RCM else c for c in frame.candidates)
    return frame


class DisabledRcmTests(unittest.TestCase):
    def project(self, frame, timelines):
        ledger = C.build_supervisor_canonical_ledger(
            frame=frame, timelines=timelines, usable_capacity_kwh=F.CAPACITY)
        return C.augment_canonical_projection_payload(
            F.canonical_execution_ledger_to_dict(ledger), frame=frame,
            timelines=timelines, expected_timeline=None,
            usable_capacity_kwh=F.CAPACITY)

    def test_stale_or_absent_disabled_trace_does_not_supply_energy_or_hide_actions(self):
        timelines = F.timelines()
        frame = disabled_frame()
        fresh = self.project(frame, timelines)
        for bad in ({}, None, {**timelines['rcm'], 'generated_at':
                              (F.NOW-timedelta(hours=2)).isoformat()}):
            with self.subTest(bad_type=type(bad).__name__):
                timelines['rcm'] = bad
                current = self.project(frame, timelines)
                self.assertEqual(current['slots'], fresh['slots'])
                self.assertGreater(len(current['slots']), 0)
                self.assertTrue(any(s['selected_action'] != 'none' for s in current['slots']))
                self.assertFalse(any(s['selected_policy'] == 'rcm' for s in current['slots']))

    def test_unknown_enabled_or_active_rcm_remains_a_required_dependency(self):
        stale = F.timelines()
        stale['rcm']['generated_at'] = (F.NOW-timedelta(hours=2)).isoformat()
        variants = []
        for field in ('allowed_by_user', 'enabled', 'export_control_enabled',
                      'pre_discharge_enabled', 'absorb_active', 'export_active',
                      'pre_discharge_active'):
            for value in (None, True):
                frame = disabled_frame()
                frame.rcm = replace(frame.rcm, **{field:value})
                variants.append((field, value, frame))
        for field in ('owner_kind', 'transaction_owner_kind'):
            for value in (F.OwnerKind.RCM, F.OwnerKind.UNKNOWN):
                frame = disabled_frame()
                frame.context = replace(frame.context, **{field:value})
                variants.append((field, value, frame))
        frame = disabled_frame()
        frame.context = replace(frame.context, owner_conflict=True)
        variants.append(('owner_conflict', True, frame))
        for field in ('allowed_by_user', 'enabled', 'active_latched', 'local_hard_stop'):
            for value in (None, True):
                frame = disabled_frame()
                frame.candidates = tuple(
                    replace(c, **{field: value}) if c.policy_id is F.PolicyId.RCM else c
                    for c in frame.candidates)
                variants.append(('candidate_'+field, value, frame))
        for field, value, frame in variants:
            with self.subTest(field=field, value=value):
                with self.assertRaisesRegex(C.CanonicalRuntimeError, 'rcm_timeline_stale'):
                    self.project(frame, stale)

    def test_reenable_restores_validation_and_fresh_sources_still_work(self):
        timelines = F.timelines()
        frame = disabled_frame()
        self.project(frame, timelines)
        frame.rcm = replace(frame.rcm, enabled=True, allowed_by_user=True)
        frame.candidates = tuple(F.no_action(p, i+1) for i,p in enumerate(F.PolicyId))
        self.project(frame, timelines)
        timelines['rcm'] = {}
        with self.assertRaisesRegex(C.CanonicalRuntimeError, 'rcm_timeline_invalid'):
            self.project(frame, timelines)

    def test_required_buy_sell_sources_and_physical_gates_remain_closed(self):
        for policy in ('rce','tariff'):
            timelines = F.timelines()
            timelines[policy]['generated_at'] = (F.NOW-timedelta(minutes=3)).isoformat()
            with self.assertRaisesRegex(C.CanonicalRuntimeError, policy+'_timeline_stale'):
                self.project(disabled_frame(), timelines)
        frame = disabled_frame()
        frame.context = replace(frame.context, critical_bms_ready=False)
        with self.assertRaisesRegex(C.CanonicalRuntimeError, 'battery_soc_unverified'):
            self.project(frame, F.timelines())

    def test_missing_frame_or_rcm_candidate_does_not_relax_requirements(self):
        self.assertEqual(C.canonical_timeline_dependencies(None), C.POLICY_ORDER)
        frame = disabled_frame()
        frame.candidates = tuple(c for c in frame.candidates if c.policy_id is not F.PolicyId.RCM)
        self.assertEqual(C.canonical_timeline_dependencies(frame), C.POLICY_ORDER)


if __name__ == '__main__':
    unittest.main()
