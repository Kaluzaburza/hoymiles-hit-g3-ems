"""Pstryk reserve restoration must satisfy the unchanged executor contract."""
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import unittest

import test_rce_forecast_market_scope as fixture

fixture.fixture()
from custom_components.hoymiles_hit_modbus.pstryk_joint import EnergySlot, JointInput, optimize, revalidate_plan
from custom_components.hoymiles_hit_modbus.pstryk_plan import projections
from custom_components.hoymiles_hit_modbus.supervisor_runtime import (
    TariffSourceSnapshot, TariffAction, TariffPlanStatus, TariffRunNeed, build_tariff_candidate)

NOW = datetime(2026, 10, 4, 4, 30, tzinfo=timezone.utc)


def scenario(**changes):
    # Reduced field case: 20 kWh, SOC 13%, reserve 15%, 400 W home load.
    return replace(JointInput(
        slots=tuple(EnergySlot(NOW+timedelta(minutes=30*i), NOW+timedelta(minutes=30*(i+1)),
                               .5, .2, 0.) for i in range(4)),
        capacity_kwh=20., initial_kwh=2.6, reserve_kwh=3., maximum_kwh=20.,
        pv_origin_kwh=0., charge_kw=5., discharge_kw=5., ac_kw=10., export_kw=10.), **changes)


def publication(data, plan):
    return projections(data, plan, revision=1, metadata={'input_revision':1}, system_power_kw=10.)[1]


def candidate(data, attrs):
    values={f.name:attrs[f.name] for f in fields(TariffSourceSnapshot) if f.name in attrs}
    for name in ('current_slot_end', 'current_grid_charge_run_end'):
        values[name]=datetime.fromisoformat(values[name]) if values.get(name) else None
    values.update(observed_at=data.slots[0].start, allowed_by_user=True, enabled=True,
        active_latched=False, status_code=TariffPlanStatus(attrs['status_code']),
        current_action=TariffAction(attrs['current_action']),
        current_run_need_class=TariffRunNeed(attrs['current_run_need_class']),
        current_soc_percent=data.initial_kwh/data.capacity_kwh*100,
        current_soc_observed_at=data.slots[0].start, maximum_soc_percent=100.,
        control_data_ready=True, planned_slot_ready=True)
    return build_tariff_candidate(TariffSourceSnapshot(**values), now=data.slots[0].start)


class ReserveTests(unittest.TestCase):
    def test_small_required_top_up_reaches_executable_integer_reserve(self):
        for soc in (13., 14., 14.8):
            with self.subTest(soc=soc):
                data=scenario(initial_kwh=soc*.2)
                plan=optimize(data); attrs=publication(data, plan)
                self.assertEqual(attrs['target_soc_percent'], 15)
                self.assertAlmostEqual(plan.slots[0].end_kwh, 3.)
                self.assertTrue(candidate(data, attrs).start_eligible)
                self.assertFalse(any(p.action=='buy' for p in plan.slots[1:]))
                self.assertAlmostEqual(plan.slots[0].grid_charge_kwh*.95, 3.-data.initial_kwh)

    def test_fractional_floor_rounds_target_up_without_exceeding_maximum(self):
        data=scenario(reserve_kwh=3.04)
        plan=optimize(data); attrs=publication(data, plan)
        self.assertEqual(attrs['target_soc_percent'], 16)
        self.assertAlmostEqual(plan.slots[0].end_kwh, 3.2)
        self.assertTrue(candidate(data, attrs).start_eligible)

    def test_power_time_and_maximum_limits_never_claim_unreachable_reserve(self):
        short=replace(scenario().slots[0], end=NOW+timedelta(minutes=2), load_kwh=.4/30)
        for data in (scenario(charge_kw=.5, charge_dc_kw=.095),
                     scenario(slots=(short,), charge_kw=1.),
                     scenario(reserve_kwh=3.04, maximum_kwh=3.1)):
            with self.subTest(data=data):
                plan=optimize(data); attrs=publication(data, plan)
                self.assertFalse(attrs['current_run_start_eligible'])
                self.assertFalse(attrs['current_run_continue_eligible'])
                self.assertEqual(attrs['current_run_suppression_reason'], 'reserve_target_unreachable')
                self.assertGreater(attrs['current_run_target_shortfall_kwh'], 0.)
                self.assertFalse(candidate(data, attrs).start_eligible)
                for p,s in zip(plan.slots, data.slots):
                    self.assertLessEqual(p.command_kw, min(data.ac_kw,data.charge_kw)+1e-8)
                    self.assertLessEqual(p.end_kwh, data.maximum_kwh+1e-8)
                    self.assertLessEqual(p.grid_charge_kwh, max(p.command_kw*s.hours-s.load_kwh,0)+1e-8)
                    self.assertAlmostEqual(p.start_kwh+p.battery_in_kwh-p.battery_out_kwh,p.end_kwh)

    def test_partial_slot_and_hour_boundary_keep_real_run_target(self):
        for minute in (25, 30, 55):
            start=NOW.replace(minute=minute)
            end=start.replace(minute=30) if minute<30 else start.replace(minute=0)+timedelta(hours=1)
            slot=EnergySlot(start,end,.5,.4*(end-start).total_seconds()/3600,0.)
            following=EnergySlot(end,end+timedelta(minutes=30),.5,.2,0.)
            data=scenario(slots=(slot,following))
            plan=optimize(data); attrs=publication(data,plan)
            if minute==55:
                self.assertEqual(attrs['target_soc_percent'],15)
                self.assertTrue(attrs['current_run_start_eligible'])
                self.assertEqual(attrs['current_grid_charge_run_end'],following.end.isoformat())
            else:
                self.assertEqual(attrs['target_soc_percent'],15)
                self.assertTrue(attrs['current_run_start_eligible'])

    def test_fixed_revalidation_never_increases_trade_to_claim_ready(self):
        data=scenario(); old=optimize(data)
        fresh=replace(data,initial_kwh=data.initial_kwh-.01)
        updated=revalidate_plan(fresh,old,captured=data)
        self.assertIsNotNone(updated)
        for a,b in zip(old.slots,updated.slots):
            self.assertLessEqual(b.command_kw,a.command_kw+1e-8)
            self.assertLessEqual(b.grid_charge_kwh,a.grid_charge_kwh+1e-8)
        attrs=publication(fresh,updated)
        self.assertFalse(attrs['current_run_start_eligible'])
        self.assertEqual(attrs['current_run_suppression_reason'],'reserve_target_unreachable')

    def test_required_top_up_keeps_partial_energy_before_half_hour_boundary(self):
        # Field reduction: at 07:27:58, 5 kW cannot add a whole SOC point
        # before 07:30. The same hourly BUY run can finish the top-up by 08:00.
        # An intermediate forecast point is energy, not a 4303 stop command.
        start=NOW.replace(minute=27,second=58)
        boundary=start.replace(minute=30,second=0)
        end=start.replace(minute=0,second=0)+timedelta(hours=1)
        data=scenario(initial_kwh=3.8,reserve_kwh=4.,slots=(
            EnergySlot(start,boundary,.73,.015,0.),
            EnergySlot(boundary,end,.73,.064,0.)))
        plan=optimize(data); attrs=publication(data,plan)
        self.assertGreater(plan.slots[0].grid_charge_kwh,0.)
        self.assertGreater(plan.slots[0].end_kwh,3.8)
        self.assertLess(plan.slots[0].end_kwh,4.)
        self.assertAlmostEqual(plan.slots[1].end_kwh,4.)
        self.assertAlmostEqual(sum(p.grid_charge_kwh*.95 for p in plan.slots),.2)
        self.assertEqual(attrs['current_action'],'grid_support_and_charge')
        self.assertEqual(attrs['target_soc_percent'],20)
        self.assertTrue(attrs['current_run_continue_eligible'])
        self.assertTrue(candidate(data,attrs).start_eligible)
        for p,s in zip(plan.slots,data.slots):
            self.assertLessEqual(p.grid_charge_kwh+p.load_kwh,p.command_kw*s.hours+1e-8)
            self.assertAlmostEqual(p.start_kwh+p.battery_in_kwh-p.battery_out_kwh,p.end_kwh)

    def test_partial_energy_does_not_make_an_unreachable_hour_ready(self):
        start=NOW.replace(minute=59)
        end=start.replace(minute=0)+timedelta(hours=1)
        data=scenario(initial_kwh=3.8,reserve_kwh=4.,slots=(
            EnergySlot(start,end,.73,.01,0.),))
        plan=optimize(data); attrs=publication(data,plan)
        self.assertGreater(plan.slots[0].grid_charge_kwh,0.)
        self.assertFalse(attrs['current_run_continue_eligible'])
        self.assertEqual(attrs['current_run_suppression_reason'],'reserve_target_unreachable')
        self.assertGreater(attrs['current_run_target_shortfall_kwh'],0.)

    def test_no_new_buy_at_or_above_reserve_with_equal_prices(self):
        for soc in (15.,16.):
            data=scenario(initial_kwh=soc*.2)
            self.assertFalse(any(p.action=='buy' for p in optimize(data).slots))


if __name__=='__main__': unittest.main()
