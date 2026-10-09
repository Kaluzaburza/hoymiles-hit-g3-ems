"""Actual YAML readiness -> production candidate -> complete-block START.

Regression for 2026-09-28: idle 4305=60, SOC=30, dynamic target=15.
Synthetic state only; never connects to Home Assistant or an inverter.
"""
from datetime import timedelta
import asyncio
import test_rce_lease_real_cadence as helpers
import test_supervisor_runtime_contract as runtime
import test_supervisor_active_bridge as bridge
import test_supervisor_active_controller as control

h = helpers.h
NOW = bridge.NOW


def readiness(**changes):
    hass, *_ = h.environment()
    h.CLOCK['now'] = NOW
    values = {
        'binary_sensor.hoymiles_ems_execution_ready': 'on',
        'binary_sensor.hoymiles_ems_export_allowed': 'on',
        'input_boolean.hoymiles_rce_dynamic_soc_enabled': 'on',
        'sensor.hoymiles_hit_overview_battery_soc': 30,
        'sensor.hoymiles_hit_ems_force_discharge_soc_readback': 60,
        'sensor.hoymiles_hit_ems_self_use_soc_readback': 10,
        'sensor.hoymiles_hit_ems_mode_readback_code': 0,
        'sensor.hoymiles_rce_effective_minimum_soc': 15,
        'sensor.hoymiles_rce_effective_discharge_power_percent': 40,
        'sensor.hoymiles_hit_ems_maximum_discharge_power_readback': 40,
        'sensor.hoymiles_hit_maximum_discharge_current': 200,
        'sensor.hoymiles_hit_battery_voltage_bms': 52,
        'sensor.hoymiles_rce_current_price': 1.0689,
        'sensor.hoymiles_hit_ems_supervisor': 'idle',
    }
    attrs = dict(status_code='ready', result_current=True, recalculation_pending=False,
                 rce_today_data_fresh=True, rce_today_age_seconds=0,
                 forecast_today_data_fresh=True, forecast_today_age_seconds=0,
                 gcf_execution_data_fresh=True, bms_discharge_data_fresh=True,
                 bms_discharge_data_available=True, bms_discharge_data_age_seconds=0,
                 soc_data_fresh=True, soc_data_age_seconds=0, system_power_kw=10)
    sup = dict(active_state='active_idle', owner='none', transaction_id=None,
               control_lease={'active': False})
    attrs.update(changes.pop('plan', {}))
    sup.update(changes.pop('supervisor', {}))
    aged = changes.pop('aged', {})
    values.update(changes)
    for entity, value in values.items():
        hass.states.values[entity] = h.FakeState(str(value),
            sup if entity.endswith('ems_supervisor') else {},
            reported=NOW-timedelta(seconds=aged.get(entity, 0)))
    hass.states.values['sensor.hoymiles_hit_rce_optimized_plan'] = h.FakeState('ready', attrs, NOW)
    renderer = helpers.RealHelperRenderer(hass)
    template = renderer.templates['hoymiles_rce_control_data_ready'][1]
    return (renderer.env.from_string(template['state']).render().strip() == 'True',
            renderer.env.from_string(template['attributes']['reason']).render().strip())


def test():
    ready, reason = readiness()
    assert ready, ('dynamic START is blocked by previous idle 4305', reason)
    rce = runtime.rce_source(observed_at=NOW, current_slot_end=NOW+timedelta(minutes=30),
        current_run_end=NOW+timedelta(minutes=30), control_data_ready=ready,
        current_soc_percent=30, protected_soc_floor_percent=15)
    candidate = runtime.build_rce_candidate(rce, now=NOW)
    assert candidate.start_eligible
    decision, candidates = bridge.decide(candidate)
    settings = bridge.settings_from_execution_source(bridge.source(
        self_use_soc_percent=10, force_discharge_soc_percent=60))
    intent = bridge.build_actuator_intent(decision, candidates, settings,
        rce=rce, tariff=bridge.TariffSourceSnapshot(), rcm=bridge.RcmSourceSnapshot(), now=NOW)
    assert intent.command.ems_block.mode == bridge.EmsMode.GRID_DISCHARGE
    assert intent.command.ems_block.force_discharge_soc_percent_4305 == 15
    assert settings.ems_block.force_discharge_soc_percent_4305 == 60
    print('PASS dynamic YAML -> candidate -> complete START target15, preserved snapshot60')
    rejected = [
        {'input_boolean.hoymiles_rce_dynamic_soc_enabled': 'off'},
        {'input_boolean.hoymiles_rce_dynamic_soc_enabled': 'unavailable'},
        {'sensor.hoymiles_hit_overview_battery_soc': 15},
        {'sensor.hoymiles_hit_overview_battery_soc': 10},
        {'sensor.hoymiles_rce_effective_minimum_soc': 'unknown'},
        {'sensor.hoymiles_hit_ems_force_discharge_soc_readback': 'unknown'},
        {'aged': {'sensor.hoymiles_hit_ems_force_discharge_soc_readback': 181}},
        {'plan': {'result_current': False}},
        {'plan': {'recalculation_pending': True}},
        {'binary_sensor.hoymiles_ems_execution_ready': 'off'},
        {'binary_sensor.hoymiles_ems_export_allowed': 'off'},
        {'sensor.hoymiles_hit_maximum_discharge_current': 0},
        {'sensor.hoymiles_hit_maximum_discharge_current': 1},
        {'sensor.hoymiles_hit_ems_mode_readback_code': 5},
        {'sensor.hoymiles_hit_ems_mode_readback_code': 2},
    ]
    for mutation in rejected:
        assert not readiness(**mutation)[0], mutation
    assert readiness(**{'supervisor': {'owner':'rce','transaction_id':'tx','active_state':'active_executing'},
                       'sensor.hoymiles_hit_ems_mode_readback_code':5,
                       'sensor.hoymiles_hit_ems_force_discharge_soc_readback':15})[0]
    print('PASS', len(rejected), 'negative gates and coherent active continuation')


async def lifecycle():
    sent = []
    clock = [NOW]
    async def persist(record):
        pass
    async def dispatch(write):
        sent.append(write)
    controller = control.SupervisorActiveController(persist=persist, dispatch=dispatch,
        publish=lambda record: None, clock=lambda: clock[0])
    await controller.async_initialize()
    source = control.execution_source(NOW, battery_soc_percent=30,
        self_use_soc_percent=10, force_discharge_soc_percent=60,
        bms_max_discharge_current_a=250)
    def frame(source, active=False, pending=False):
        ready = readiness(**{
            'sensor.hoymiles_hit_ems_mode_readback_code': source.physical_mode_code,
            'sensor.hoymiles_hit_ems_force_discharge_soc_readback': source.force_discharge_soc_percent})[0]
        return control.rce_frame(clock[0], source, deadline=NOW+timedelta(minutes=30),
            floor=15, power=40, active=active, transaction_pending=pending,
            control_data_ready=ready)
    await controller.async_reconcile(frame(source))
    assert len(sent) == 1
    assert controller.record.state is control.ActiveState.WAITING_READBACK
    tx = controller.record.transaction.transaction_id
    # An unchanged pre-command readback must not acknowledge the write or
    # invalidate dynamic start merely because physical 4305 is still 60.
    clock[0] += timedelta(seconds=1)
    await controller.async_reconcile(frame(source, pending=True))
    assert len(sent) == 1
    assert controller.record.state is control.ActiveState.WAITING_READBACK
    clock[0] += timedelta(seconds=1)
    fresh = control.execution_source(clock[0], physical_mode_code=5,
        full_block_generation=11, full_block_generation_at=clock[0],
        self_use_soc_percent=10, force_discharge_soc_percent=15,
        battery_soc_percent=30, bms_max_discharge_current_a=250,
        battery_power_w=4500, battery_power_observed_at=clock[0],
        grid_power_w=4000, grid_power_observed_at=clock[0],
        load_power_w=500, load_power_observed_at=clock[0],
        pv_power_w=0, pv_power_observed_at=clock[0])
    await controller.async_reconcile(frame(fresh, pending=True))
    assert controller.record.transaction.transaction_id == tx
    assert controller.record.state is control.ActiveState.EXECUTING, controller.record
    assert len(sent) == 1
    print('PASS controller START -> waiting on old FC03 -> newer matching FC03; same transaction, no restore')


if __name__ == '__main__':
    test()
    asyncio.run(lifecycle())
