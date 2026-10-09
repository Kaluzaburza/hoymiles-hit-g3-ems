"""Real HA projection plus bounded optimizer evidence; no network/devices."""
from dataclasses import replace
from datetime import timedelta
import unittest
import test_pstryk_runtime as f


class RuntimeSettlingTests(f.RuntimeTests):
    async def test_market_hash_is_usable_and_withdrawn_on_change(self):
        await self.pstryk()
        await self.c.recalculate()
        basis = self.c.market_fingerprint()
        self.assertRegex(basis, r'^[0-9a-f]{64}$')
        self.hass.states.async_set(f.m.OPTION_HELPERS['minimum_saving'], '99')
        self.assertIsNone(self.c.market_fingerprint())
        self.c.invalidate('input_changed')
        self.assertIsNone(self.c.market_fingerprint())

    async def test_raw_load_step_preserves_nominal_plan_but_blocks_physical_sale(self):
        await self.pstryk()
        # The accepted input still passes through the production translator.
        self.settings = replace(self.settings, battery_soc_percent=90.,
            average_daily_load_kwh=1., average_night_load_kwh=.1,
            current_load_power_kw=0., conservative_pv_by_slot_kwh={
                p.start: 3. for p in self.settings.price_slots[1:]})
        self.settings = replace(self.settings, price_slots=[replace(p, price_pln_kwh=2. if i==0 else .1)
            for i,p in enumerate(self.settings.price_slots)])
        self.settings = replace(self.settings,pv_by_slot_kwh=self.settings.conservative_pv_by_slot_kwh)
        async def save(): pass
        self.c._save = save
        await self.c.recalculate()
        self.assertTrue(self.rce._attributes['current_slot_planned'])
        basis = self.c.market_fingerprint()
        from custom_components.hoymiles_hit_modbus.rce_optimizer import RceActiveCommitment
        self.rce._active_rce_commitment = lambda now: RceActiveCommitment(
            transaction_id='rce:settling', started_at=f.NOW,
            hard_deadline=f.NOW+timedelta(minutes=30), physical_verified_at=now,
            maximum_discharge_power_percent=60., minimum_soc_percent=20.)
        self.settings = replace(self.settings, current_load_power_kw=3.)
        f.m.dt_util.utcnow.return_value = f.NOW+timedelta(seconds=20)
        # Real post-command refresh bypasses the periodic telemetry cadence.
        from custom_components.hoymiles_hit_modbus.rce_sensor import HoymilesRCEOptimizerSensor
        self.rce._delayed_recalculate_tasks=set()
        self.rce._invalidate_internal_inputs=self.c.invalidate
        self.rce._recalculate_and_write=self.c.recalculate
        await HoymilesRCEOptimizerSensor.async_recalculate_post_command_settling(self.rce)
        # Raw power no longer suppresses the common nominal trajectory.
        # It must still veto an undeliverable physical SELL command.
        self.assertFalse(self.rce._attributes['current_slot_load_exhausts_requested_discharge_budget'])
        self.assertTrue(self.rce._attributes['current_slot_load_only_export_suppressed'])
        self.assertTrue(self.rce._attributes['current_slot_planned'])
        self.assertFalse(self.rce._attributes['current_slot_continue_eligible'])
        self.assertEqual(self.rce._attributes['current_slot_suppression_reason'], 'live_power_insufficient')
        self.assertEqual(self.c.market_fingerprint(), basis)
        self.hass.states.async_set(f.m.PERMISSIONS[2], 'off')
        self.assertIsNone(self.c.market_fingerprint())
        await self.c.recalculate()
        self.assertFalse(self.rce._attributes['current_slot_load_exhausts_requested_discharge_budget'])


class QualificationTests(unittest.TestCase):
    def test_live_minimum_export_proof_keeps_fresh_other_inputs(self):
        from custom_components.hoymiles_hit_modbus.pstryk_plan import live_load_only_export_suppressed
        from custom_components.hoymiles_hit_modbus.pstryk_joint import optimize, JointInput, EnergySlot
        slots=tuple(EnergySlot(f.NOW+timedelta(minutes=30*i),f.NOW+timedelta(minutes=30*(i+1)),
            2. if i==0 else .1, 0., 0.) for i in range(4))
        before=JointInput(slots,100.,95.,75.,100.,20.,8.,8.,16.,16.,
                          current_load_power_kw=.748,current_pv_power_kw=0.,minimum_export_kw=2.)
        previous=optimize(before)
        pulse=replace(before,current_load_power_kw=7.86)
        plan=optimize(pulse)
        qualify=lambda d,p=plan: live_load_only_export_suppressed(d,p,before,previous,16.)
        self.assertTrue(qualify(pulse))
        for changes in (
            {'current_load_power_kw':None},{'current_load_power_kw':float('nan')},
            {'current_load_power_kw':.748},{'current_pv_power_kw':None},
            {'current_pv_power_kw':1.},{'allow_sell':False},{'discharge_kw':0},
            {'export_kw':0},{'reserve_kwh':99.},{'initial_kwh':19.},
            {'slots':(replace(slots[0],net=2.01),*slots[1:])},
            {'slots':(replace(slots[0],sale_blocked=True),*slots[1:])},
            {'slots':(slots[0],replace(slots[1],load_kwh=4.),*slots[2:])},
        ):
            self.assertFalse(qualify(replace(pulse,**changes)),changes)
        self.assertFalse(qualify(pulse,replace(plan,required_charge=True)))
        self.assertFalse(qualify(pulse,replace(plan,base_shortfall_kwh=1.)))

    def test_physical_load_does_not_change_the_nominal_market_fingerprint(self):
        from custom_components.hoymiles_hit_modbus.pstryk_settling import market_basis
        from custom_components.hoymiles_hit_modbus.pstryk_plan import build_joint_input
        from test_pv_charge_delay_adapter import settings
        options=dict(maximum_soc=100.,charge_power_percent=100.,charge_efficiency=95.,
            minimum_saving=.01,demand_margin_percent=0.,allow_buy=True,allow_sell=True)
        s=settings(); before=build_joint_input(s,options,pv_origin_kwh=0.)
        pulse=build_joint_input(replace(s,current_load_power_kw=12.),options,pv_origin_kwh=0.)
        self.assertEqual([p.load_kwh for p in before.slots],[p.load_kwh for p in pulse.slots])
        self.assertNotEqual(before.current_load_power_kw,pulse.current_load_power_kw)
        self.assertEqual(market_basis(before,1,'p'),market_basis(pulse,1,'p'))

    def test_elapsed_fraction_preserves_both_forecast_identities(self):
        from custom_components.hoymiles_hit_modbus.pstryk_settling import market_basis
        from custom_components.hoymiles_hit_modbus.pstryk_plan import build_joint_input
        from test_pv_charge_delay_adapter import settings
        options=dict(maximum_soc=100.,charge_power_percent=100.,charge_efficiency=95.,
            minimum_saving=.01,demand_margin_percent=0.,allow_buy=True,allow_sell=True)
        s=settings();before=build_joint_input(s,options,pv_origin_kwh=0.)
        after=build_joint_input(replace(s,now=s.now+timedelta(seconds=30)),options,pv_origin_kwh=0.)
        self.assertEqual(market_basis(before,1,'p'),market_basis(after,1,'p'))
        changed=replace(after,sale_pv_kwh=(after.sale_pv_kwh[0],0.,*after.sale_pv_kwh[2:]))
        self.assertNotEqual(market_basis(before,1,'p'),market_basis(changed,1,'p'))

    def test_load_is_proved_by_counterfactual_and_other_changes_are_vetoes(self):
        from custom_components.hoymiles_hit_modbus.pstryk_settling import load_only_suppression, market_basis
        from custom_components.hoymiles_hit_modbus.pstryk_joint import optimize, JointInput, EnergySlot
        at=f.NOW
        slots=tuple(EnergySlot(at+timedelta(minutes=30*i),at+timedelta(minutes=30*(i+1)),
            2. if i==0 else .1, 0., 0. if i==0 else 3.) for i in range(4))
        before=JointInput(slots,10.,9.,2.,9.,7.,3.,3.,5.,5.)
        previous=optimize(before)
        self.assertEqual(previous.slots[0].action,'sell')
        data=replace(before,slots=(replace(slots[0],load_kwh=1.5),*slots[1:]))
        self.assertTrue(load_only_suppression(data,optimize(data),before,previous))
        for changed in (replace(data,pv_origin_kwh=0),replace(data,allow_sell=False),
            replace(data,discharge_kw=2.9),replace(data,reserve_kwh=8.9),
            replace(data,sale_reserve_kwh=(8.9,)*len(data.slots)),
            replace(data,slots=(replace(data.slots[0],net=-.1),*slots[1:])),
            replace(data,slots=(data.slots[0],replace(slots[1],pv_kwh=0.),*slots[2:])),
            replace(data,slots=(replace(data.slots[0],load_kwh=.5),*slots[1:]))):
            self.assertFalse(load_only_suppression(changed,optimize(changed),before,previous),changed)
        self.assertNotEqual(market_basis(data,1,'a'),market_basis(data,2,'a'))
        self.assertNotEqual(market_basis(data,1,'a'),market_basis(data,1,'b'))


if __name__=='__main__': unittest.main()
