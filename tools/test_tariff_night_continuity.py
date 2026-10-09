"""Night-like controlled trajectories, not an exact replay of lost inputs."""
from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import unittest
from unittest.mock import patch
from test_tariff_optimizer import settings
import tariff_optimizer as m

def night(hour=0,minute=58,soc=58.6,action='grid_support'):
    now=datetime(2026,10,9,hour,minute,tzinfo=ZoneInfo('Europe/Warsaw'))
    commitment=m.TariffActiveCommitment('test:night',action,now-timedelta(minutes=30),
        now.replace(hour=6,minute=0),soc-2 if action=='grid_support' else 60,50,now)
    return settings(now,battery_capacity_kwh=26,battery_soc_percent=soc,
        active_commitment=commitment,current_load_power_kw=.5,
        current_pv_power_kw=0,current_battery_power_kw=0)

class NightContinuityTests(unittest.TestCase):
    def test_whole_horizon_cost_energy_and_reserve_oracle(self):
        for s in (night(),night(2,30,55),night(action='grid_support_and_charge')):
            with patch.object(m,'_stabilize_active_tariff_support',
                side_effect=lambda settings,starts,loads,planned,support,rates,fractions,sim:(support,sim,False)):
                before=m.optimize_tariff_charging(s)
            after=m.optimize_tariff_charging(s)
            self.assertLess(after.optimized_optimization_cost_pln,before.optimized_optimization_cost_pln)
            self.assertAlmostEqual(after.optimized_grid_import_kwh,before.optimized_grid_import_kwh)
            self.assertAlmostEqual(after.ending_battery_kwh,before.ending_battery_kwh)
            for field in ('remaining_expensive_import_kwh','terminal_shortfall_kwh',
                'base_energy_shortfall_kwh','hard_reserve_shortfall_kwh','demand_margin_unserved_kwh'):
                self.assertLessEqual(getattr(after,field),getattr(before,field)+1e-6)
            self.assertTrue(all(p.soc_percent>=s.reserve_soc_percent-1e-6 for p in after.timeline_trace.points))
    def test_small_charge_keeps_valid_support_after_boundary(self):
        for action in ('grid_support','grid_support_and_charge','battery_charge'):
            s=night(action=action)
            plan=m.optimize_tariff_charging(s)
            self.assertTrue(plan.active_commitment_applied,action)
            self.assertEqual(plan.current_action,'grid_support_and_charge')
            self.assertEqual(plan.current_run_end,s.now.replace(hour=1,minute=0))
            self.assertEqual(plan.current_grid_charge_run_end,s.active_commitment.hard_deadline)
            self.assertTrue(plan.current_run_continue_eligible)
            self.assertEqual(plan.active_commitment_deadline,s.active_commitment.hard_deadline)
    def test_better_whole_support_not_rejected_for_cost_improvement(self):
        s=night(2,30,55)
        p=m.optimize_tariff_charging(s)
        self.assertEqual(p.current_action,'grid_support')
        self.assertTrue(p.active_commitment_applied)
        self.assertEqual(p.current_grid_charge_run_end,s.active_commitment.hard_deadline)
    def test_missing_inputs_expired_proof_and_pv_still_block_stabilization(self):
        s=night()
        variants=(replace(s,control_inputs_fresh=False),replace(s,current_load_power_kw=None),
            replace(s,current_pv_power_kw=None),replace(s,current_load_power_kw=float('nan')),
            replace(s,current_pv_power_kw=1),replace(s,active_commitment=None),
            replace(s,active_commitment=replace(s.active_commitment,physical_verified_at=s.now-timedelta(seconds=31))),
            replace(s,active_commitment=replace(s.active_commitment,physical_verified_at=s.now+timedelta(seconds=1))),
            replace(s,active_commitment=replace(s.active_commitment,hard_deadline=s.now)))
        for item in variants:
            self.assertFalse(m.optimize_tariff_charging(item).active_commitment_applied)
    def test_support_still_requires_actual_remaining_need(self):
        s=night(2,30,100)
        # No future demand and no reserve shortfall: do not keep buying merely
        # because a previous command is active.
        s=replace(s,average_daily_load_kwh=0,average_night_load_kwh=0,
            load_by_slot_kwh={},current_load_power_kw=0)
        p=m.optimize_tariff_charging(s)
        self.assertEqual(p.current_action,'none')
        self.assertFalse(p.current_run_continue_eligible)

if __name__=='__main__': unittest.main()
