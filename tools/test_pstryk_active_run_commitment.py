"""Pstryk uses the existing RCE active-run policy on a joint BUY/SELL balance."""
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from custom_components.hoymiles_hit_modbus import pstryk_plan as P
from custom_components.hoymiles_hit_modbus import pstryk_joint as J
from custom_components.hoymiles_hit_modbus import rce_optimizer as R
from test_pstryk_runtime import settings, NOW

OPTIONS = dict(charge_efficiency=95., charge_power_percent=60., maximum_soc=100.,
               minimum_saving=.01, demand_margin_percent=0., allow_buy=True, allow_sell=True)


def selected(data, exports):
    home = J.optimize(replace(data, allow_sell=False))
    actions = tuple(-2 if i in exports else a for i, a in enumerate(home.action_levels))
    points, cost = J.simulate(data, actions,
        exports={data.slots[i].start: value for i, value in exports.items()})
    return replace(home, slots=points, cost_pln=cost, action_levels=actions,
                   benefit_pln=max(home.baseline_cost_pln-cost, 0.), sale_reason='sale_planned')


def fixture():
    before = replace(settings(), now=NOW, battery_capacity_kwh=100., battery_soc_percent=90.,
        average_daily_load_kwh=0., average_night_load_kwh=0., current_load_power_kw=0.,
        bms_charge_data_age_seconds=0., bms_discharge_data_age_seconds=0.,
        price_slots=[R.PriceSlot(NOW+timedelta(minutes=30*i), 1. if i < 4 else 2.)
                     for i in range(8)])
    data = P.build_joint_input(before, OPTIONS)
    accepted = selected(data, {i: 1. for i in range(4)})
    return before, data, accepted


class CommitmentTests(unittest.TestCase):
    def test_lower_home_forecast_keeps_the_confirmed_higher_soc_floor(self):
        before, _, _ = fixture()
        before = replace(before, battery_capacity_kwh=30., battery_soc_percent=80.,
            average_daily_load_kwh=15.25, average_night_load_kwh=7.45,
            current_load_power_kw=.705)
        data = P.build_joint_input(before, OPTIONS)
        accepted = selected(data, {0: 1., 1: 1.})
        attrs, _ = P.projections(data, accepted, revision=1, metadata={}, system_power_kw=5.)
        fresh = replace(before, now=NOW+timedelta(minutes=5), battery_soc_percent=79.,
            average_daily_load_kwh=14., average_night_load_kwh=6., current_load_power_kw=1.151)
        current = P.build_joint_input(fresh, OPTIONS)
        candidate = selected(current, {len(current.slots)-1: 1.})
        proof = R.RceActiveCommitment('rce:falling-home-floor', NOW, NOW+timedelta(hours=1),
            fresh.now, attrs['current_slot_execution_power_percent'], attrs['minimum_soc_percent'])
        result = P.retain_active_plan(current, candidate, fresh,
            accepted=(data, accepted, before), commitment=proof)
        self.assertTrue(result.active_slot_commitment_applied)
        self.assertEqual(result.slots[0].action, 'sell')
        self.assertGreaterEqual(result.slots[0].protected_kwh,
                                proof.minimum_soc_percent*current.capacity_kwh/100)
        published, _ = P.projections(current, result, revision=2, metadata={}, system_power_kw=5.)
        self.assertGreaterEqual(published['minimum_soc'], proof.minimum_soc_percent)
        self.assertFalse(published['current_slot_start_eligible'])

    def test_native_rce_run_survives_joint_economic_reallocation(self):
        before, data, accepted = fixture()
        before = replace(before, self_consumption_filter_enabled=True)
        for minute in (5, 31, 65, 95, 119):
            fresh = replace(before, now=NOW+timedelta(minutes=minute), battery_soc_percent=85.)
            current = P.build_joint_input(fresh, OPTIONS)
            candidate = selected(current, {len(current.slots)-1: 1.})
            self.assertEqual(candidate.slots[0].action, 'self_use')
            proof = R.RceActiveCommitment('rce:run', NOW, NOW+timedelta(hours=2),
                                         fresh.now, 40., 20.)
            result = P.retain_active_plan(current, candidate, fresh,
                accepted=(data, accepted, before), commitment=proof)
            self.assertTrue(result.active_slot_commitment_applied, minute)
            self.assertEqual(result.slots[0].action, 'sell', minute)
            self.assertLessEqual(result.slots[0].command_kw, 2.)
            self.assertGreaterEqual(result.slots[0].protected_kwh, 20.)
            self.assertLessEqual(result.active_run_deadline, proof.hard_deadline)
            attrs, _ = P.projections(current, result, revision=2, metadata={}, system_power_kw=5.)
            self.assertEqual(attrs['current_run_end'], proof.hard_deadline.isoformat(), minute)
            self.assertEqual(candidate.slots[0].action, 'self_use')
            # No repeated spending of the original full half-hour allocation.
            self.assertLessEqual(result.slots[0].battery_export_kwh,
                                 1.*(30-minute % 30)/30+1e-8)
            home = J.optimize(replace(current, allow_sell=False))
            self.assertTrue(all(p.grid_import_kwh <= b.grid_import_kwh+1e-8
                                for p, b in zip(result.slots, home.slots)))

    def test_same_native_vetoes_and_smaller_bms_cap(self):
        before, data, accepted = fixture()
        fresh = replace(before, now=NOW+timedelta(minutes=5), battery_soc_percent=85.)
        proof = R.RceActiveCommitment('rce:run', NOW, NOW+timedelta(hours=2), fresh.now, 40., 20.)
        for label, changed, evidence in (
            ('no proof', fresh, None),
            ('stale proof', fresh, replace(proof, physical_verified_at=fresh.now-timedelta(seconds=26))),
            ('expired', fresh, replace(proof, hard_deadline=fresh.now)),
            ('zero BMS', replace(fresh, bms_max_discharge_current_a=0.), proof),
            ('zero GCF', replace(fresh, export_power_cap_kw=0.), proof),
            ('reserve', replace(fresh, battery_soc_percent=20.), proof),
            ('load', replace(fresh, current_load_power_kw=5.), proof),
            ('loss price', replace(fresh, price_slots=[replace(p, price_pln_kwh=-1.)
                                                      for p in fresh.price_slots]), proof),
            ('changed wear', replace(fresh, battery_wear_cost_pln_kwh=2.), proof),
        ):
            with self.subTest(label=label):
                current = P.build_joint_input(changed, OPTIONS)
                candidate = selected(current, {len(current.slots)-1: 1.})
                result = P.retain_active_plan(current, candidate, changed,
                    accepted=(data, accepted, before), commitment=evidence)
                self.assertFalse(result.active_slot_commitment_applied)
        capped = replace(fresh, bms_max_discharge_current_a=30.)
        current = P.build_joint_input(capped, OPTIONS)
        result = P.retain_active_plan(current, selected(current, {}), capped,
            accepted=(data, accepted, before), commitment=proof)
        self.assertTrue(result.active_slot_commitment_applied)
        self.assertLessEqual(result.slots[0].command_kw, current.discharge_kw)

    def test_adjacent_new_sale_cannot_extend_the_old_deadline(self):
        before, data, accepted = fixture()
        fresh = replace(before, now=NOW+timedelta(minutes=5), battery_soc_percent=85.)
        current = P.build_joint_input(fresh, OPTIONS)
        proof = R.RceActiveCommitment('rce:short', NOW, NOW+timedelta(minutes=30), fresh.now, 40., 20.)
        result = P.retain_active_plan(current, selected(current, {1: 1.}), fresh,
            accepted=(data, accepted, before), commitment=proof)
        self.assertTrue(result.active_slot_commitment_applied)
        attrs, _ = P.projections(current, result, revision=2, metadata={}, system_power_kw=5.)
        self.assertEqual(attrs['current_run_end'], proof.hard_deadline.isoformat())
        self.assertFalse(attrs['current_slot_start_eligible'])


if __name__ == '__main__':
    unittest.main()
