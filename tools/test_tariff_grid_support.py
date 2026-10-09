"""Tariff Mode 4 field examples, tested with real model and physical verifier."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tariff_optimizer import settings, ZONE
from tariff_optimizer import _simulate, optimize_tariff_charging
from supervisor_active_bridge import physical_verification
from supervisor_executor import ExecutionAction, VerificationStatus
from supervisor_runtime import ExecutionSourceSnapshot

NOW = datetime(2026, 9, 8, 12, tzinfo=timezone.utc)
checks = 0
def check(value, message):
    global checks
    checks += 1
    assert value, message

def field_source(**overrides):
    at = NOW - timedelta(seconds=1)
    values = dict(
        physical_mode_code=4, full_block_generation=11,
        full_block_generation_at=at, full_block_execution_ready=True,
        hardware_readback_supported=True, machine_type_code=0, inverter_count=1,
        topology_generation_at=at, force_charge_soc_percent=60,
        maximum_charge_power_percent=60, battery_soc_percent=60,
        battery_soc_observed_at=at, grid_power_w=-1200,
        battery_power_w=0, pv_power_w=0, load_power_w=1200,
        grid_power_observed_at=at, battery_power_observed_at=at,
        pv_power_observed_at=at, load_power_observed_at=at,
    )
    values.update(overrides)
    return ExecutionSourceSnapshot(**values)

def verify(action, *, allow_tariff_settling=False, **overrides):
    return physical_verification('tariff-test', action, field_source(**overrides),
        command_sent_at=NOW-timedelta(seconds=5), now=NOW,
        allow_tariff_settling=allow_tariff_settling)

def test_physical_effect():
    hold = ExecutionAction.TARIFF_GRID_SUPPORT
    charging = ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE
    check(verify(hold).status is VerificationStatus.CONFIRMED, 'house from grid and idle battery')
    for bat in (-5000, -201, 201, 5000):
        # Keep the full balance coherent to isolate the battery predicate.
        proof=verify(hold, battery_power_w=bat, grid_power_w=bat-1200)
        check(proof.status is VerificationStatus.CONTRADICTED, f'hold must reject BAT {bat}')
    for bat in (-199, 199):
        check(verify(hold,battery_power_w=bat,grid_power_w=bat-1200).status is VerificationStatus.CONFIRMED,'bounded inverter idle tolerance')
    check(verify(charging,force_charge_soc_percent=65,battery_power_w=-5000,grid_power_w=-6200).status is VerificationStatus.CONFIRMED,'house and battery charging')
    for action in (charging,ExecutionAction.TARIFF_BATTERY_CHARGE):
        proof=verify(action)
        check(proof.status is VerificationStatus.CONFIRMED,'reached target maintains grid supply, including first ACK')
        check('tariff_phase=holding_target' in proof.evidence,'holding must not be diagnosed as active battery charging')
        check(verify(action,force_charge_soc_percent=65).status is not VerificationStatus.CONFIRMED,'idle battery below target does not prove charging')
        for soc in (None,True,float('nan'),float('inf')):
            check(verify(action,battery_soc_percent=soc).status is not VerificationStatus.CONFIRMED,'invalid SOC grants no target-reached authority')
        check(verify(action,battery_soc_observed_at=NOW-timedelta(seconds=121)).status is not VerificationStatus.CONFIRMED,'stale SOC cannot prove target reached')
    for action in (hold,charging):
        for override in (
            {'physical_mode_code':0}, {'physical_mode_code':1},
            {'full_block_generation_at':NOW-timedelta(seconds=20)},
            {'full_block_generation':None}, {'load_power_w':None},
            {'grid_power_observed_at':NOW-timedelta(seconds=20)},
            {'battery_power_observed_at':NOW-timedelta(seconds=20)},
            {'pv_power_observed_at':NOW+timedelta(seconds=6)},
            {'machine_type_code':2}, {'grid_power_w':-6000},
        ):
            check(verify(action,**override).status is not VerificationStatus.CONFIRMED,f'no authority from invalid physical evidence: {override}')
    check(verify(hold,pv_power_w=6200,battery_power_w=-5000,grid_power_w=0).status is not VerificationStatus.CONFIRMED,'PV charging does not prove grid support')
    for action in (hold,charging):
        proof=verify(action,allow_tariff_settling=True,battery_power_w=1200,grid_power_w=0)
        check(proof.status is VerificationStatus.PENDING,'ACK before physical transition stays pending, never success')
        check('tariff_phase=waiting_for_grid_charge_effect' in proof.evidence,'waiting effect is explicit')
        check(verify(action,allow_tariff_settling=True,grid_power_w=1000,battery_power_w=2200).status is VerificationStatus.CONTRADICTED,'export remains a contradiction while waiting')
        check(verify(action,allow_tariff_settling=True,grid_power_w=-6000).status is VerificationStatus.CONTRADICTED,'incoherent balance cannot use the settling wait')
        check(verify(action,allow_tariff_settling=True,load_power_w=None).status is VerificationStatus.UNAVAILABLE,'missing physical inputs remain unavailable during settling')

def test_model_examples():
    at=datetime(2026,8,6,14,tzinfo=ZONE)
    loads={at.replace(hour=h,minute=m):9/14 for h in range(15,22) for m in (0,30)}
    loads[at]=.5
    loads[at+timedelta(minutes=30)]=.5
    setup=settings(at,battery_soc_percent=60,reserve_soc_percent=20,
        maximum_soc_percent=100,average_daily_load_kwh=0,average_night_load_kwh=0,
        load_by_slot_kwh=loads,current_load_power_kw=1,current_pv_power_kw=0,
        current_battery_power_kw=1,charge_power_kw=6,requested_charge_power_kw=6,
        battery_charge_power_kw=6,charge_efficiency_percent=100,
        discharge_efficiency_percent=100,minimum_saving_pln_kwh=0,horizon_days=3)
    first=optimize_tariff_charging(setup)
    second=optimize_tariff_charging(replace(setup,now=at+timedelta(minutes=30)))
    check(first.current_action=='grid_support' and abs(first.target_soc_percent-58)<1e-9,'support at measured SOC 60 commands 58')
    check(second.current_action=='grid_support_and_charge' and abs(second.target_soc_percent-65)<1e-9,'next planned block charges to 65, not maximum 100')
    check(first.requested_charge_power_kw==second.requested_charge_power_kw==6,'fixed 60% of 10 kW in both blocks')
    check(first.current_slot_end==at+timedelta(minutes=30),'action boundary remains visible')
    check(first.current_grid_charge_run_end==second.current_grid_charge_run_end==at+timedelta(hours=1),'adjoining Mode 4 authorization reaches true run end')
    for minute in (40,50,59):
        reached=optimize_tariff_charging(replace(setup,now=at+timedelta(minutes=minute),
            battery_soc_percent=65,current_battery_power_kw=0))
        check(reached.current_action=='grid_support' and reached.current_run_continue_eligible,
            'real recalculation after reaching65 preserves house grid supply')
        check(abs(reached.target_soc_percent-63)<1e-9 and reached.current_grid_charge_run_end==at+timedelta(hours=1),
            'target reached does not shorten or extend original Mode4 run')
    for home in (.5,4,8):
        hold=_simulate(setup,[at],[home],{},{0:.1},[(.6,'low')],[1])
        check(hold.accepted_support_kwh[0]==home,'house grid supply is independent of the 3 kWh charge budget')
        check(hold.battery_delta_kwh[0]==0,'pure support preserves stored energy')
        charged=_simulate(setup,[at],[home],{0:1},{},[(.6,'low')],[1])
        check(charged.stored_import_kwh[0]==1,'home demand does not consume requested battery charging')
        check(charged.total_grid_import_kwh==home+1,'separate house and battery energy accounting')
    capped=_simulate(replace(setup,maximum_soc_percent=65),[at],[8],{0:3},{},[(.6,'low')],[1])
    check(capped.stored_import_kwh[0]==1,'configured maximum remains a battery cap')
    zero=_simulate(replace(setup,charge_power_kw=0),[at],[.5],{0:1},{0:.5},[(.6,'low')],[1])
    check(not zero.accepted_support_kwh and zero.accepted_import_kwh[0]==0,'zero charge capability must not become unlimited')

if __name__=='__main__':
    test_model_examples()
    test_physical_effect()
    print(f'Tariff grid support: {checks} checks passed (offline only)')
