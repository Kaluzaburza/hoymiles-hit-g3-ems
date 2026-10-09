"""BUY run retention, small house support and independent battery power floor."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import unittest
from test_pstryk_runtime import NOW
from custom_components.hoymiles_hit_modbus import pstryk_joint as J, pstryk_plan as P
from custom_components.hoymiles_hit_modbus.tariff_optimizer import TariffActiveCommitment


def fixture():
    slots=tuple(J.EnergySlot(NOW+timedelta(minutes=30*i),NOW+timedelta(minutes=30*(i+1)),
        .1 if i<4 else 2.,.05 if i<4 else 1.,0.) for i in range(8))
    data=J.JointInput(slots,10.,5.3,1.,10.,0.,5.,5.,10.,10.)
    return data,J.optimize(data),SimpleNamespace(inverter_power_kw=10.,inverter_count=1)


def remaining(data,minute):
    now=NOW+timedelta(minutes=minute)
    rows=tuple(replace(s,start=max(now,s.start),load_kwh=s.load_kwh*
        (s.end-max(now,s.start)).total_seconds()/(s.end-s.start).total_seconds())
        for s in data.slots if s.end>now)
    return replace(data,slots=rows)


class BuyTests(unittest.TestCase):
    def test_proven_buy_run_crosses_slots_without_respending_or_extension(self):
        before,accepted,settings=fixture()
        for minute in (5,31,65):
            data=remaining(before,minute)
            points,cost=J.simulate(data,(0,)*len(data.slots))
            candidate=replace(accepted,slots=points,cost_pln=cost,action_levels=(0,)*len(points))
            proof=TariffActiveCommitment('tariff:buy','grid_support',NOW,NOW+timedelta(minutes=90),
                52.,50.,data.slots[0].start)
            result=P.retain_active_buy_plan(data,candidate,settings,
                accepted=(before,accepted,settings),commitment=proof)
            self.assertTrue(result.active_slot_commitment_applied,minute)
            self.assertEqual(result.slots[0].action,'buy')
            self.assertEqual(result.active_run_deadline,proof.hard_deadline)
            self.assertLessEqual(result.slots[0].grid_import_kwh,.1*data.slots[0].hours+1e-8)
            self.assertEqual(result.slots[0].grid_charge_kwh,0.)
            self.assertLessEqual(result.cost_pln,candidate.cost_pln+1e-8)
            _,attrs=P.projections(data,result,revision=2,metadata={},system_power_kw=10.)
            self.assertEqual(attrs['current_grid_charge_run_end'],proof.hard_deadline.isoformat())
            self.assertFalse(attrs['current_run_start_eligible'])

    def test_current_vetoes_cancel_retention(self):
        before,accepted,settings=fixture(); data=remaining(before,31)
        candidate=J.optimize(data)
        proof=TariffActiveCommitment('tariff:buy','grid_support',NOW,NOW+timedelta(minutes=90),52.,50.,data.slots[0].start)
        for label,changed,evidence in (
            ('no proof',data,None),('stale',data,replace(proof,physical_verified_at=NOW)),
            ('expired',data,replace(proof,hard_deadline=data.slots[0].start)),
            ('future proof',data,replace(proof,physical_verified_at=data.slots[0].start+timedelta(seconds=1))),
            ('extended deadline',data,replace(proof,hard_deadline=NOW+timedelta(minutes=120))),
            ('permission',replace(data,allow_buy=False),proof),
            ('zero BMS',replace(data,charge_kw=0.),proof),
            ('price',replace(data,slots=(replace(data.slots[0],net=2.),*data.slots[1:])),proof),
            ('efficiency',replace(data,charge_efficiency=.8),proof),
            ('home no longer needs support',replace(data,initial_kwh=9.),proof),
        ):
            with self.subTest(label=label):
                result=P.retain_active_buy_plan(changed,candidate,settings,
                    accepted=(before,accepted,settings),commitment=evidence)
                self.assertFalse(result.active_slot_commitment_applied)

    def test_projection_cannot_use_later_purchase_to_raise_active_target(self):
        data, plan, _ = fixture()
        first = plan.slots[0]
        later = replace(plan.slots[1], action='buy', end_kwh=9.)
        active = replace(plan, slots=(first,later,*plan.slots[2:]),
            active_slot_commitment_applied=True,active_run_deadline=first.end)
        _,attrs=P.projections(data,active,revision=2,metadata={},system_power_kw=10.)
        self.assertLessEqual(attrs['target_soc_percent'],53.)
        self.assertEqual(attrs['current_grid_charge_run_end'],first.end.isoformat())
        self.assertAlmostEqual(attrs['current_run_grid_import_kwh'],first.grid_import_kwh)

    def test_small_house_power_is_not_rounded_to_no_action(self):
        before,_,_=fixture()
        for watts in (1,50,100,189,199,200,201):
            data=replace(before,slots=(replace(before.slots[0],load_kwh=watts/2000),*before.slots[1:]))
            points,_=J.simulate(data,(1,0,0,0,0,0,0,0))
            self.assertEqual(points[0].action,'buy',watts)
            self.assertAlmostEqual(points[0].grid_import_kwh,watts/2000)
            self.assertEqual(points[0].grid_charge_kwh,0.)
            self.assertGreater(points[0].command_kw,0.)


if __name__=='__main__': unittest.main()
