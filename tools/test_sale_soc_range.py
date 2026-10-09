"""Extra sale reserve 0..90: schema, migration and actual optimizer limits."""
from dataclasses import replace
from pathlib import Path
import unittest
import yaml
from test_soc_reserve_scope import case, joint, _optimize_rce_impl, simulate
from custom_components.hoymiles_hit_modbus.ems_initial_defaults import DEFAULT_SEED_SPECS

ROOT=Path(__file__).resolve().parents[1]
class SaleReserveRangeTests(unittest.TestCase):
    def test_helper_and_initial_shape_keep_default_and_expand_range(self):
        spec=next(x for x in DEFAULT_SEED_SPECS if x.key=='rce_soc_safety_margin')
        self.assertEqual((spec.minimum,spec.maximum),(0,90))
        data=yaml.safe_load((ROOT/'home_assistant/hoymiles_ems_scheduler.yaml').read_text(encoding='utf-8'))
        helper=data['input_number']['hoymiles_rce_soc_safety_margin']
        self.assertEqual((helper['min'],helper['max'],helper['step']),(0,90,1))
        self.assertNotIn('initial',helper,'Do not overwrite restored user values')
    def test_floor_is_capped_and_does_not_raise_house_floor(self):
        for extra, floor in ((0,20),(50,70),(60,80),(90,100)):
            s=replace(case(),battery_soc_percent=100,outage_reserve_soc_percent=20,
                safety_margin_soc_percent=extra)
            plan=_optimize_rce_impl(s,fixed_exports={})
            self.assertGreaterEqual(plan.minimum_soc_percent,floor)
            self.assertLessEqual(plan.minimum_soc_percent,100)
            self.assertAlmostEqual(plan.base_reserve_energy_kwh,4)
            data=joint(s)
            self.assertAlmostEqual(data.reserve_kwh,4)
            self.assertGreaterEqual(min(data.sale_reserve_kwh),20*floor/100)
            self.assertLessEqual(max(data.sale_reserve_kwh),20)
            if extra==90:
                points, _=simulate(data,(0,)*len(data.slots))
                self.assertEqual(sum(p.battery_export_kwh for p in points),0)
                # Explicit sale request cannot bypass the fully protected store.
                attempted=_optimize_rce_impl(s,fixed_exports={s.price_slots[0].start:1})
                self.assertEqual(attempted.planned_export_kwh,0)
                enabled=replace(data,allow_sell=True)
                attempted_points,_=simulate(enabled,(-2,)*len(enabled.slots),
                    exports={slot.start:1 for slot in enabled.slots})
                self.assertAlmostEqual(sum(p.battery_export_kwh for p in attempted_points),0,places=12)

if __name__=='__main__': unittest.main()
