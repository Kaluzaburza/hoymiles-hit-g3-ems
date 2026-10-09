"""RCE sale buffers must not become the household or tariff BUY floor."""
from dataclasses import replace
import unittest

import test_rce_forecast_market_scope as fixture
fixture.fixture()
from test_pv_charge_delay_adapter import settings
from custom_components.hoymiles_hit_modbus.rce_optimizer import _optimize_rce_impl
from custom_components.hoymiles_hit_modbus.pstryk_plan import build_joint_input
from custom_components.hoymiles_hit_modbus.pstryk_joint import simulate, optimize


def case(**kw):
    s = settings()
    return replace(s, battery_capacity_kwh=20., battery_soc_percent=20.,
        outage_reserve_soc_percent=10., safety_margin_soc_percent=15.,
        average_daily_load_kwh=.5, average_night_load_kwh=0.,
        # Explicit nominal 0.5 kWh at the first slot; do not synthesize it
        # from the instantaneous 1 kW measurement.
        load_profile_30m_kwh=tuple(.5 if i == s.now.hour*2+s.now.minute//30 else 0.
                                   for i in range(48)),
        current_load_power_kw=1., current_pv_power_kw=0.,
        pv_by_slot_kwh={p.start:0. for p in s.price_slots},
        conservative_pv_by_slot_kwh={p.start:0. for p in s.price_slots},
        **kw)


def joint(s, **kw):
    return build_joint_input(s, dict(maximum_soc=100., charge_power_percent=100.,
        charge_efficiency=95., minimum_saving=.01, demand_margin_percent=0.,
        allow_buy=False, allow_sell=False, allow_delay=False, **kw))


class ReserveScopeTests(unittest.TestCase):
    def test_native_house_can_consume_below_sale_buffer(self):
        for dynamic, manual in ((True, 0.), (False, 60.)):
            s = case(dynamic_reserve_enabled=dynamic, manual_minimum_soc_percent=manual)
            plan = _optimize_rce_impl(s, fixed_exports={})
            self.assertTrue(plan.ready, plan.status_code)
            self.assertAlmostEqual(plan.base_reserve_energy_kwh, 2.)
            p = plan.timeline_trace.points[0]
            self.assertAlmostEqual(p.grid_import_kwh, 0.)
            self.assertAlmostEqual(p.battery_delta_kwh, -.5/.95)
            self.assertGreaterEqual(plan.minimum_soc_percent, 25 if dynamic else 60)

    def test_pstryk_house_and_buy_floor_are_physical(self):
        for dynamic, manual in ((True, 0.), (False, 60.)):
            data = joint(case(dynamic_reserve_enabled=dynamic, manual_minimum_soc_percent=manual))
            self.assertAlmostEqual(data.reserve_kwh, 2.)
            self.assertAlmostEqual(data.terminal_kwh, 2.)
            points, _ = simulate(data, (0,)*len(data.slots))
            self.assertAlmostEqual(points[0].home_battery_kwh, .5)
            self.assertAlmostEqual(points[0].grid_import_kwh, 0.)
            self.assertGreaterEqual(min(data.sale_reserve_kwh), 5. if dynamic else 12.)

    def test_sale_buffer_does_not_request_mandatory_buy(self):
        data = replace(joint(case()), allow_buy=True)
        plan = optimize(data)
        self.assertFalse(plan.required_charge)
        self.assertAlmostEqual(sum(p.grid_charge_kwh for p in plan.slots), 0.)

    def test_maximum_buy_target_below_sale_buffer_is_valid(self):
        data = build_joint_input(case(), dict(maximum_soc=20., charge_power_percent=100.,
            charge_efficiency=95., minimum_saving=.01, demand_margin_percent=0.,
            allow_buy=True, allow_sell=True, allow_delay=False))
        plan = optimize(data)
        self.assertAlmostEqual(data.reserve_kwh, 2.)
        self.assertEqual(sum(p.battery_export_kwh for p in plan.slots), 0.)

    def test_sale_still_respects_buffer_and_active_command_floor(self):
        s = replace(case(), battery_soc_percent=90.)
        for minimum in (0., 80.):
            plan = _optimize_rce_impl(s, fixed_exports={s.price_slots[0].start:1.},
                fixed_export_minimum_soc_percent=minimum)
            self.assertTrue(plan.ready, plan.status_code)
            self.assertGreater(plan.planned_export_kwh, 0.)
            self.assertGreaterEqual(plan.minimum_soc_percent, max(25, minimum))
            self.assertTrue(all(p.soc_percent >= p.protected_soc_floor_percent-1e-6
                for p in plan.timeline_trace.points if p.selected))

    def test_physical_floor_still_limits_house(self):
        s = replace(case(), battery_soc_percent=10.)
        data = joint(s)
        points, _ = simulate(data, (0,)*len(data.slots))
        self.assertAlmostEqual(points[0].home_battery_kwh, 0.)
        self.assertAlmostEqual(points[0].grid_import_kwh, .5)
        self.assertFalse(_optimize_rce_impl(s, fixed_exports={}).ready)


if __name__ == '__main__':
    unittest.main()
