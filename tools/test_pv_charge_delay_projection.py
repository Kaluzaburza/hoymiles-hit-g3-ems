"""PV export must suspend charging in the expected SOC projection as well."""
from dataclasses import replace
import copy
import unittest

import test_supervisor_canonical_dual_track as f


class ProjectionTests(unittest.TestCase):
    def projected_hold(self, initial_soc, *, first_delta=0., latched_target=None):
        """A future hold follows a neutral interval with a known energy delta."""
        frame = f._frame(soc_percent=initial_soc)
        frame.rce = replace(frame.rce, system_power_kw=5.,
            current_soc_percent=initial_soc, pv_charge_hold=latched_target is not None,
            pv_charge_hold_qualified=True, latched_minimum_soc_percent=latched_target)
        end_soc = initial_soc + first_delta / f.CAPACITY_KWH * 100.
        rows = []
        for index in range(2):
            point = f._point('rce', index=index, minutes=30,
                battery_delta_kwh=first_delta if index == 0 else 0.,
                pv_kwh=max(first_delta, 0.) if index == 0 else 1.,
                load_kwh=max(-first_delta, 0.) if index == 0 else 0.,
                grid_import_kwh=0., grid_export_kwh=0. if index == 0 else 1.,
                soc_percent=end_soc, baseline_soc_percent=end_soc,
                selected=index == 1, action='idle' if index == 0 else 'pv_charge_hold')
            rows.append(replace(point, policy=replace(point.policy,
                planned_export_kwh=0., planned_battery_withdrawal_kwh=0.,
                target_discharge_kw=0., expected_revenue_pln=0.)))
        timelines = f._flat_one_hour_timelines(soc_percent=initial_soc, rce=rows)
        # The canonical neutral backbone prefers tariff, with the same physical
        # consumption/charge before the future PV opportunity.
        tariff = [f._point('tariff', index=index, minutes=30,
            battery_delta_kwh=first_delta if index == 0 else 0.,
            pv_kwh=max(first_delta, 0.) if index == 0 else 0.,
            load_kwh=max(-first_delta, 0.) if index == 0 else 0.,
            grid_import_kwh=0., grid_export_kwh=0.,
            soc_percent=end_soc, baseline_soc_percent=end_soc) for index in range(2)]
        timelines['tariff'] = f._payload('tariff', tariff, actual_soc_percent=initial_soc)
        before = copy.deepcopy(timelines)
        ledger = f.runtime.build_supervisor_canonical_ledger(frame=frame,
            timelines=timelines, usable_capacity_kwh=f.CAPACITY_KWH, freshness_now=f.NOW)
        self.assertEqual(timelines, before)
        return f.canonical_execution_ledger_to_dict(ledger)

    def test_full_battery_now_does_not_break_future_hold_after_household_load(self):
        payload = self.projected_hold(100., first_delta=-2.)
        holds = [s for s in payload['slots'] if s['selected_action'] == 'pv_charge_hold']
        self.assertTrue(holds)
        for slot in holds:
            self.assertEqual(slot['start_eligibility'], 'unverified')
            values = slot['command_expectation']['values']
            self.assertEqual(values['force_discharge_soc'], 81.)

    def test_future_full_battery_rejects_only_hold_without_losing_neutral_plan(self):
        for initial_soc, delta in ((100., 0.), (90., 1.)):
            with self.subTest(initial_soc=initial_soc):
                payload = self.projected_hold(initial_soc, first_delta=delta)
                self.assertTrue(payload['slots'])
                self.assertFalse(any(s['selected_action'] == 'pv_charge_hold' for s in payload['slots']))

    def test_future_hold_does_not_reuse_a_present_episode_target(self):
        payload = self.projected_hold(90., first_delta=-1., latched_target=91.)
        holds = [s for s in payload['slots'] if s['selected_action'] == 'pv_charge_hold']
        self.assertTrue(holds)
        for slot in holds:
            values = slot['command_expectation']['values']
            self.assertEqual(values['force_discharge_soc'], 81.)

    def test_current_hold_preserves_its_original_target(self):
        frame = self.frame()
        frame.rce = replace(frame.rce, latched_minimum_soc_percent=70.)
        payload = f._augmented(frame, self.timelines(), None)
        current = payload['slots'][0]
        self.assertEqual(current['selected_action'], 'pv_charge_hold')
        self.assertEqual(current['command_expectation']['values']['force_discharge_soc'], 70.)

    def test_future_soc_99_retains_the_original_one_percent_headroom_rule(self):
        payload = self.projected_hold(99.)
        holds = [s for s in payload['slots'] if s['selected_action'] == 'pv_charge_hold']
        self.assertTrue(holds)
        self.assertTrue(all(s['command_expectation']['values']['force_discharge_soc'] == 100.
                            for s in holds))

    def test_future_soc_above_99_does_not_clamp_an_invalid_target(self):
        payload = self.projected_hold(99.1)
        self.assertFalse(any(s['selected_action'] == 'pv_charge_hold' for s in payload['slots']))

    def frame(self, export_state=f.ExportState.VERIFIED_ALLOWED):
        frame=f._frame(soc_percent=60.,export_state=export_state)
        frame.rce=replace(frame.rce,system_power_kw=5.,current_soc_percent=60.,
            pv_charge_hold=True,pv_charge_hold_qualified=True)
        return frame

    def timelines(self):
        rows = []
        for index in range(2):
            point = f._point('rce', index=index, minutes=30,
                battery_delta_kwh=0., pv_kwh=1., load_kwh=0.,
                grid_import_kwh=0., grid_export_kwh=1., soc_percent=60.,
                baseline_soc_percent=70.+10*index, selected=True,
                action='pv_charge_hold')
            rows.append(replace(point, policy=replace(point.policy,
                planned_export_kwh=0., planned_battery_withdrawal_kwh=0.,
                target_discharge_kw=0., expected_revenue_pln=0.)))
        return f._flat_one_hour_timelines(soc_percent=60., rce=rows)

    def test_p50_surplus_is_exported_without_charging_or_battery_sale(self):
        timelines = self.timelines()
        for provider in (None, f._expected_timeline([(6.,0.),(4.,0.)],initial_soc_percent=60.)):
            with self.subTest(provider=provider is not None):
                before = copy.deepcopy(timelines)
                payload = f._augmented(self.frame(),timelines,provider)
                holds = [s for s in payload['slots'] if s['selected_action']=='pv_charge_hold']
                self.assertTrue(holds, 'The qualified PV hold must be a canonical automation action')
                for slot in holds:
                    equation=slot['soc_equation']
                    self.assertAlmostEqual(equation['expected_soc_start_percent'],
                        equation['expected_soc_end_percent'], msg='PV hold must cancel all expected charging')
                    self.assertEqual(equation['battery_to_grid_kwh'],0.)
                self.assertEqual(timelines,before,'A display projection must not rewrite its source plan')
                if provider is not None:
                    self.assertAlmostEqual(payload['expected_grid_export_kwh'],5.)

    def test_zero_export_does_not_gain_a_hold_or_export(self):
        payload = f._augmented(self.frame(f.ExportState.CONFIRMED_ZERO_EXPORT), self.timelines(),
            f._expected_timeline([(6.,0.),(4.,0.)],initial_soc_percent=60.,zero_export_confirmed=True))
        self.assertFalse(any(s['selected_action']=='pv_charge_hold' for s in payload['slots']))
        self.assertEqual(payload['expected_grid_export_kwh'],0.)

    def test_household_deficit_is_not_hidden_by_a_fake_flat_projection(self):
        payload = f._augmented(self.frame(), self.timelines(),
            f._expected_timeline([(0.,1.),(0.,1.)],initial_soc_percent=60.))
        holds = [s for s in payload['slots'] if s['selected_action']=='pv_charge_hold']
        self.assertTrue(holds)
        for slot in holds:
            equation = slot['soc_equation']
            self.assertLess(equation['expected_soc_end_percent'],equation['expected_soc_start_percent'])
            self.assertEqual(equation['battery_to_grid_kwh'],0.)


if __name__=='__main__': unittest.main()
