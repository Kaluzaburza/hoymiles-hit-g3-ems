"""Measured Mode 4 PV/grid sharing; offline execution and energy regressions."""
import asyncio
from dataclasses import replace
from datetime import timedelta

from test_tariff_grid_support import NOW, verify
from test_tariff_optimizer import settings, _simulate, ExportState
from test_tariff_active_controller import confirmed_controller, physical, frame
from supervisor_executor import ExecutionAction, ActiveState, VerificationStatus
from supervisor_runtime import TariffAction

checks = 0


def check(value, message):
    global checks
    checks += 1
    assert value, message


def model():
    # kW inputs, one half-hour interval, unity efficiency to expose allocation.
    for pv, load, power, expected_grid, expected_battery in (
        (5, 1, 3, 0, 2),
        (5, 1, 6, 1, 3),
        (5, 1, 10, 3, 5),
        (10, 1, 6, 0, 4.5),
        (0, 8, 3, 5.5, 1.5),
    ):
        setup = settings(NOW, battery_capacity_kwh=100, battery_soc_percent=30,
            charge_power_kw=power, pv_charge_power_kw=12, battery_charge_power_kw=12,
            system_ac_power_kw=10, charge_efficiency_percent=100,
            pv_by_slot_kwh={NOW: pv/2})
        result = _simulate(setup, [NOW], [load/2], {0:10}, {}, [(0.6,'low')], [1])
        check(abs(result.total_grid_import_kwh-expected_grid)<1e-9,
            f'PV{pv}/LOAD{load}/setting{power}: import is the actual grid shortfall')
        check(abs(result.battery_delta_kwh[0]-expected_battery)<1e-9,
            'PV is not added on top of the full charging setting')
    for disposition in (ExportState.CONFIRMED_ZERO_EXPORT, ExportState.VERIFIED_ALLOWED):
        setup = settings(NOW, battery_capacity_kwh=100, battery_soc_percent=30,
            charge_power_kw=6, pv_charge_power_kw=12, battery_charge_power_kw=12,
            system_ac_power_kw=10, charge_efficiency_percent=100,
            export_state=disposition, pv_by_slot_kwh={NOW:2.5})
        result = _simulate(setup,[NOW],[0.5],{},{0:0.1},[(0.6,'low')],[1])
        check(result.battery_delta_kwh[0]==0, 'Mode4 hold stops PV battery charging')
        check(result.total_grid_import_kwh==0, 'PV-covered home needs no grid import')
        if disposition is ExportState.CONFIRMED_ZERO_EXPORT:
            check(result.pv_curtailed_kwh[0]==2, 'verified zero export curtails the unused PV')
        else:
            check(result.grid_export_kwh[0]==2, 'model retains the pre-existing verified export disposition')


def proof():
    for action in (ExecutionAction.TARIFF_BATTERY_CHARGE,
                   ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE):
        result=verify(action, force_charge_soc_percent=75, battery_soc_percent=44,
            pv_power_w=5820, load_power_w=1243, battery_power_w=-4577, grid_power_w=0)
        check(result.status is VerificationStatus.CONFIRMED, 'measured PV-only Mode4 charge is execution evidence')
        check('tariff_supply=pv_or_idle_no_grid_import' in result.evidence,
            'no grid import must be explicit rather than invented grid-to-battery accounting')
        check('accounting_authority=separate_direct_channel_required' in result.evidence,
            'execution proof does not grant accounting authority')
    result=verify(ExecutionAction.TARIFF_GRID_SUPPORT,
        pv_power_w=929, load_power_w=929, battery_power_w=0, grid_power_w=0)
    check(result.status is VerificationStatus.CONFIRMED, 'PV-covered holding is not a false rollback')
    result=verify(ExecutionAction.TARIFF_GRID_SUPPORT,maximum_charge_power_percent=30,
        pv_power_w=5899,load_power_w=12683,battery_power_w=0,grid_power_w=-6784)
    check(result.status is VerificationStatus.CONFIRMED,
        'measured 12.7kW home supply remains valid at a 30 percent charging setting')
    for updates in (
        dict(battery_power_w=500, grid_power_w=500),
        dict(battery_power_w=-500, grid_power_w=-500),
        dict(pv_power_w=0),
        dict(grid_power_w=None),
        dict(pv_power_observed_at=NOW+timedelta(seconds=6)),
        dict(physical_mode_code=0),
    ):
        args=dict(pv_power_w=929, load_power_w=929, battery_power_w=0, grid_power_w=0)
        args.update(updates)
        check(verify(ExecutionAction.TARIFF_GRID_SUPPORT,**args).status is not VerificationStatus.CONFIRMED,
            f'PV exception cannot hide a different flow, missing source or wrong mode: {updates}')


async def variable_pv():
    controller,sent,saved,clock=await confirmed_controller()
    action=TariffAction.GRID_SUPPORT_AND_CHARGE
    clock[0]+=timedelta(seconds=2)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=12),
        controller.record,target=75,action=action))
    baseline=controller.record.transaction.command_snapshot
    deadline=controller.record.transaction.deadline
    for index,(pv,battery,grid) in enumerate(((0,-6000,-7200),(6200,-6000,-1000),
            (10200,-9000,0),(6200,-6000,-1000),(0,-6000,-7200))):
        clock[0]+=timedelta(seconds=4)
        src=replace(physical(clock[0],target=75,soc=44,battery=battery,generation=13+index),
            pv_power_w=pv,grid_power_w=grid)
        await controller.async_reconcile(frame(clock[0],src,controller.record,target=75,action=action))
        check(controller.record.state is ActiveState.EXECUTING,
            f'cloud-like PV changes preserve valid Mode4 execution: {index}: {controller.record.state}/{controller.record.reason}, {controller.record.transaction.physical_verification}')
        check(len(sent)==2,'PV variation alone sends no new command or neutral')
        check(controller.record.transaction.command_snapshot==baseline and controller.record.transaction.deadline==deadline,
            'PV variation never replaces the original restore snapshot or lease')


if __name__=='__main__':
    model()
    proof()
    asyncio.run(variable_pv())
    print(f'Tariff PV contract: {checks} checks passed (offline only)')
