"""Whole-percent commands before planner publication; no HA or live actions."""
from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from dataclasses import replace
import math
from pathlib import Path
from types import SimpleNamespace

import test_rce_optimizer as rce_fixture
import test_rcm_optimizer as rcm_fixture
import test_rcm_live_control_refresh as live_fixture

ROOT = Path(__file__).resolve().parents[1]
RCE = rce_fixture.RCE
RCM = live_fixture.rcm_optimizer
checks = 0


def check(condition, message):
    global checks
    checks += 1
    assert condition, message


def rce_commands():
    now = rce_fixture.NOW.replace(hour=18)
    results = []
    for percent in (49.1, 49.9):
        settings = rce_fixture.base_input(now=now,
            price_slots=[RCE.PriceSlot(now, 2.0)], battery_capacity_kwh=100.0,
            discharge_power_percent=percent, current_load_power_kw=0.0,
            current_pv_power_kw=0.0, current_battery_soc_fresh=True)
        result = RCE.optimize_rce(settings)
        check(settings.discharge_power_percent == percent, 'RCE raw input retained')
        check(result.current_slot_execution_power_percent == 49.0, 'RCE fractional requested cap floors49')
        check(result.current_slot_execution_discharge_power_kw == 4.9, 'RCE command kW matches integer percent')
        check(result.planned_export_kwh <= 4.9*0.5 + 1e-9, 'RCE horizon respects integer requested cap')
        selected = [p for p in result.timeline_trace.points if p.selected]
        check(selected and all(p.policy.command_discharge_power_percent == 49.0 for p in selected), 'RCE timeline matches current command')
        results.append(result)
    check(results[0].planned_export_kwh == results[1].planned_export_kwh, '49.1 and49.9 plan the same command budget')
    for limit_percent in (0.99, 49.1, 49.9):
        settings = rce_fixture.base_input(now=now, price_slots=[RCE.PriceSlot(now,2.0)],
            battery_capacity_kwh=100.0, bms_max_discharge_current_a=limit_percent*2,
            current_load_power_kw=0.0,current_pv_power_kw=0.0,current_battery_soc_fresh=True)
        result=RCE.optimize_rce(settings)
        command=result.current_slot_execution_power_percent
        check(command == math.floor(limit_percent), 'RCE final BMS cap floored')
        check(result.current_slot_execution_discharge_power_kw <= limit_percent/10, 'RCE command cannot exceed BMS')
        if limit_percent < 1:
            check(not result.current_slot_start_eligible and result.planned_export_kwh == 0.0, 'RCE sub1 command grants no execution')
    for bad in (float('nan'),float('inf'),-1):
        check(RCE._quantize_4306_percent(bad)==0.0,'RCE invalid power fails closed')


def tariff_input_and_publication():
    """Execute the actual input conversion and publication expressions.

    Full HA source acquisition is outside this small regression. The compiled
    statements are the producer's body, not a duplicate implementation.
    """
    source=(ROOT/'custom_components/hoymiles_hit_modbus/tariff_sensor.py').read_text(encoding='utf-8')
    tree=ast.parse(source)
    method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_optimizer_input')
    body=method.body
    def assigns(node,name):
        return isinstance(node,ast.Assign) and any(isinstance(t,ast.Name) and t.id==name for t in node.targets)
    begin=next(i for i,n in enumerate(body) if assigns(n,'requested_percent'))
    end=next(i for i in range(begin,len(body)) if assigns(body[i],'effective_power_kw'))
    code=compile(ast.fix_missing_locations(ast.Module(body=deepcopy(body[begin:end+1]),type_ignores=[])), '<actual-tariff-command-input>', 'exec')
    published={}
    for node in ast.walk(method):
        if isinstance(node,ast.Dict):
            for key,value in zip(node.keys,node.values):
                if isinstance(key,ast.Constant) and key.value in ('command_charge_power_percent','modeled_effective_charge_power_percent'):
                    published[key.value]=compile(ast.Expression(deepcopy(value)),'<actual-tariff-command-publication>','eval')
    check(len(published)==2,'actual tariff command/model publication expressions found')
    for raw in (49.1,49.9,60.0):
        required={'input_number.hoymiles_tariff_requested_charge_power':raw}
        env={'required':required,'system_power_kw':10.0,'math':math,
             'self':SimpleNamespace(_effective_charge_power_factor=0.8)}
        exec(code,env)
        expected=float(math.floor(raw))
        check(required['input_number.hoymiles_tariff_requested_charge_power']==raw,'tariff raw source retained')
        check(env['requested_percent']==expected and env['requested_power_kw']==expected/10,'tariff floors before requested model power')
        check(math.isclose(env['effective_power_kw'],expected/10*0.8),'tariff learned factor applied once after integer command')
        check(eval(published['command_charge_power_percent'],env)==expected,'tariff publishes integer command')
        check(eval(published['modeled_effective_charge_power_percent'],env)==round(expected*0.8,1),'tariff modeled value remains diagnostic decimal')


async def rcm_full_and_fast():
    for percent in (49.1,49.9):
        samples=live_fixture._base_samples()
        samples[live_fixture.GRID_VOLTAGE_ENTITIES[0]]=(253.2,True)
        samples['sensor.hoymiles_hit_battery_voltage_bms']=(50.0,True)
        samples['sensor.hoymiles_hit_maximum_charge_current']=(percent*2,True)
        probe=live_fixture._new_probe(samples)
        settings=replace(probe._last_full_optimizer_input,voltage_l1_v=253.2,
                         battery_voltage_v=50.0,bms_max_charge_current_a=percent*2)
        full=RCM.optimize_rcm(settings)
        await probe._async_live_control_refresh(live_fixture.NOW)
        fast=probe._attributes
        check(full.recommended_charge_limit_percent==fast['recommended_charge_limit_percent']==49.0,
              'RCEm306 full/fast floor fractional BMS cap identically')
        check(full.recommended_charge_power_kw==fast['recommended_charge_power_kw']==4.9,
              'RCEm306 full/fast command kW parity')
        check(full.recommended_charge_power_kw<=percent/10 and settings.bms_max_charge_current_a==percent*2,
              'RCEm BMS cap not increased or source rounded')
        at=live_fixture.NOW.replace(hour=11)
        samples=live_fixture._base_samples()
        for name,value in {'battery_voltage_bms':50.0,'maximum_discharge_current':percent*2,
                           'overview_battery_soc':90.0,'overview_pv_total_power':0.0}.items():
            samples['sensor.hoymiles_hit_'+name]=(value,True)
        samples['sensor.hoymiles_actual_load_power']=(0.0,True)
        probe=live_fixture._new_probe(samples)
        settings=rcm_fixture.settings(now=at,expected_risk_surplus_kwh=30.0,
            expected_natural_headroom_kwh=0.0,battery_soc_percent=90.0,pv_power_kw=0.0,
            load_power_kw=0.0,battery_voltage_v=50.0,bms_max_discharge_current_a=percent*2,
            house_discharge_efficiency_percent=100.0,minutes_to_risk=90,
            voltage_l1_v=240.0,voltage_l2_v=240.0,voltage_l3_v=240.0)
        full=RCM.optimize_rcm(settings)
        probe._last_full_optimizer_input=settings
        probe._last_full_optimizer_result=full
        probe._last_full_plan_at=at
        await probe._async_live_control_refresh(at)
        fast=probe._attributes
        check(full.pre_discharge_start_eligible and fast['pre_discharge_start_eligible'],
              'RCEm4306 full/fast comparison exercises an eligible pre-discharge')
        check(full.pre_discharge_power_percent==fast['pre_discharge_power_percent']==49.0,
              'RCEm4306 full/fast floor fractional BMS cap identically')
        check(full.pre_discharge_power_kw==fast['pre_discharge_power_kw']==4.9,
              'RCEm4306 full/fast command kW parity')
        check(full.pre_discharge_target_soc_percent==fast['pre_discharge_target_soc_percent']==28.0,
              'RCEm4305 full/fast retain upward whole SOC floor')
    for raw in (0.99,49.1,49.9):
        check(RCM._quantize_power_percent(raw)==math.floor(raw),'RCEm shared full/fast floor helper')
    # Independent export-control ceiling also uses whole prospective targets.
    capped=RCM.optimize_rcm(rcm_fixture.settings(current_export_limit_percent=70,
        saved_export_limit_percent=49.9,user_export_cap_percent=49.9))
    check(capped.recommended_export_limit_percent==49.0,'RCEm259 floors user/saved export ceiling')
    head=RCM.optimize_rcm(rcm_fixture.settings(now=rcm_fixture.settings().now.replace(hour=11),pv_power_kw=0.0))
    check(head.pre_discharge_target_soc_percent==66.0 and head.target_soc_before_risk_percent==64.0,'RCEm4305 SOC rounds upward')
    check(head.planned_grid_discharge_kwh==0.84,'RCEm energy uses the66% command floor')
    check(head.pre_discharge_power_percent.is_integer() and math.isclose(head.pre_discharge_power_kw,head.pre_discharge_power_percent/10),
          'RCEm4306 command kW matches whole percent')


def main():
    rce_commands()
    tariff_input_and_publication()
    asyncio.run(rcm_full_and_fast())
    print(f'Integer optimizer commands: PASS ({checks} checks; real producers/full-fast callback, offline only)')


if __name__=='__main__':main()
