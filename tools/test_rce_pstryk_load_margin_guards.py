"""The new LOAD evidence uses existing command/physical/time safety gates."""
import asyncio
from dataclasses import replace
from datetime import timedelta

import test_rce_post_command_settling as f
import test_rce_post_command_settling_optimizer as o


async def controller_guards():
    original = f.load_block
    for shape in ('rce', 'pstryk'):
        def margin(at, physical=None, *, pending=False, **extra):
            values = dict(current_slot_load_exhausts_requested_discharge_budget=False,
                          current_slot_load_only_export_suppressed=True)
            if shape == 'pstryk':
                values.update(current_slot_planned=True,
                    current_slot_suppression_reason='live_power_insufficient',
                    planned_export_energy_kwh=1., requested_discharge_power_kw=8.)
            values.update(extra)
            return original(at, physical, pending=pending, **values)
        f.load_block = margin
        try:
            # Whole production controller, unchanged dispatch/restore oracle.
            for test in (f.test_fixed_budget_and_restore, f.test_real_successor_and_command_identity,
                         f.test_immediate_stops_and_no_late_basis,
                         f.test_healthy_interlude_cannot_renew_budget,
                         f.test_unsent_successor_preserves_old_anchor,
                         f.test_restart_has_no_grace_context, f.test_late_ack_keeps_send_time_anchor):
                await test()
            c, clock, writes = await f.running()
            tx = c.record.transaction
            at = f.t.NOW + timedelta(seconds=27)
            good = margin(at)
            def authorized(frame):
                return f.post_command_rce_measurement_hold_authorized(
                    tx.intent, frame.decision, frame.candidates,
                    f.settings_from_execution_source(frame.execution),
                    execution_source=frame.execution, export_state=frame.context.export_state,
                    rce=frame.rce, market_fingerprint=f.MARKET, recognized_pending=False, now=frame.now)
            f.check(authorized(good), shape+' positive LOAD proof')
            for field, value in (
                ('current_slot_load_only_export_suppressed',False),
                ('current_slot_load_only_export_suppressed',None),
                ('current_slot_load_only_export_suppressed',1),
                ('current_slot_start_eligible',True),
                ('current_slot_continue_eligible',True),
                ('current_slot_suppression_reason','insufficient_runtime'),
                ('current_slot_suppression_reason','home_energy_shortage'),
                ('result_current',False),('recalculation_pending',True),
                ('planned_export_energy_kwh',float('nan')),
                ('requested_discharge_power_kw',float('nan')),
                ('requested_discharge_power_kw',-1),
                ('post_command_settling_market_fingerprint','c'*64),
                ('post_command_settling_market_fingerprint',None),
                ('requested_discharge_power_percent',0),
                ('requested_discharge_power_percent',float('nan')),
                ('protected_soc_floor_percent',74),('protected_soc_floor_percent',None),
                ('input_revision',None),('input_revision',True),
                ('sale_block_active',True),('enabled',False),('allowed_by_user',False),
                ('latched_slot_end',f.END+timedelta(minutes=30)),
                ('status_code',f.t.RcePlanStatus.HOME_ENERGY_SHORTAGE),
            ):
                f.check(not authorized(f.rebuild(good,rce=replace(good.rce,**{field:value}))),shape+':'+field)
            for field, value in (
                ('bms_max_discharge_current_a',0),('bms_max_discharge_current_a',20),
                ('bms_voltage_v',float('nan')),
                ('bms_discharge_current_observed_at',at-timedelta(seconds=301)),
                ('battery_soc_percent',50),('battery_soc_percent',None),
                ('battery_soc_observed_at',at-timedelta(seconds=121)),
            ):
                f.check(not authorized(replace(good,execution=replace(good.execution,**{field:value}))),shape+':'+field)
            for prefix in ('load','pv','grid'):
                for value in (None,float('nan'),float('inf'),True):
                    f.check(not authorized(replace(good,execution=replace(good.execution,
                        **{prefix+'_power_w':value}))),shape+':invalid '+prefix)
                for stamp in (None,at-timedelta(seconds=16),at+timedelta(seconds=6)):
                    f.check(not authorized(replace(good,execution=replace(good.execution,
                        **{prefix+'_power_observed_at':stamp}))),shape+':age '+prefix)
        finally:
            f.load_block = original


def rce_counterfactual():
    R = o.RCE
    before = o.settings(current_load_power_kw=.748,minimum_net_export_power_kw=2.)
    accepted = R.optimize_rce(before)
    at = before.now + timedelta(seconds=24)
    proof = R.RceActiveCommitment('rce:margin',before.now,accepted.current_run_end,at,
                                 accepted.current_slot_execution_power_percent,accepted.minimum_soc_percent)
    pulse = replace(before,now=at,current_load_power_kw=5.86)
    def qualifies(data, *, commitment=proof):
        return R.active_load_only_export_suppressed(data,R.optimize_rce(data),
            accepted_settings=before,accepted_result=accepted,commitment=commitment)
    assert qualifies(pulse)
    assert not qualifies(before)
    assert not qualifies(pulse,commitment=None)
    for changes in (
        {'current_load_power_kw':None}, {'current_load_power_kw':float('nan')},
        {'current_pv_power_kw':None}, {'current_battery_soc_fresh':False},
        {'bms_max_discharge_current_a':0}, {'bms_discharge_data_fresh':False},
        {'bms_discharge_data_age_seconds':301}, {'battery_soc_percent':10},
        {'export_power_cap_kw':0}, {'effective_export_power_kw':0},
        {'outage_reserve_soc_percent':99}, {'average_night_load_kwh':100},
        {'price_slots':[R.PriceSlot(before.now,2.01)]},
        {'price_slots':[R.PriceSlot(before.now,2.,True)]},
        {'now':before.now+timedelta(seconds=150)},
    ):
        assert not qualifies(replace(pulse,**changes)), changes
    print('PASS RCE counterfactual rejects changed economics/reserve, missing/old data and expired proof')


async def main():
    rce_counterfactual()
    await controller_guards()
    print(f'PASS LOAD margin guards ({f.CHECKS} controller checks, both plan shapes)')


if __name__ == '__main__':
    asyncio.run(main())
