"""Tariff retargets through production HA sensor callbacks in virtual time."""
import asyncio
from datetime import timedelta
import test_supervisor_sensor_contract as fixture

S=fixture.SENSOR
NOW=fixture.NOW
checks=0

def check(condition,message):
    global checks
    checks+=1
    assert condition,message

async def scenario():
    hass,entry,_runtime,sensor=fixture.environment()
    deadline=NOW+timedelta(minutes=10)
    writes=[]
    def eid(key): return fixture._source_entity_id(S._SOURCE_BY_KEY[key],entry.entry_id)
    def store(key,value,attrs=None): hass.states.values[eid(key)]=fixture.FakeState(str(value),attrs,NOW)
    def event(key,value,attrs=None,report=False):
        method=hass.fire_report if report else hass.fire_state
        method(eid(key),fixture.FakeState(str(value),attrs,fixture.CLOCK['now']))
    async def dispatch(write): writes.append(write)
    async def drain():
        for _ in range(128):
            await asyncio.sleep(0)
            if sensor._controller_task is None and sensor._pending_active_frame is None: return
        raise AssertionError('callback queue did not drain')
    async def flush():
        for _ in range(16):
            for item in hass.active_delays(): item.run()
            await drain()
            if not hass.active_delays(): return
        raise AssertionError('debounce queue did not drain')
    def physical(generation,target=58,soc=60,battery=0,mode=4,power=60):
        for key,value in (
            ('ems_mode_readback',mode),('self_use_soc_readback',20),('backup_soc_readback',80),
            ('charge_soc_readback',target),('charge_power_ems_readback',power),
            ('discharge_soc_readback',20),('discharge_power_readback',50),('ems_generation',generation)):
            event(key,value,report=True)
        for key,value in (
            ('battery_soc',soc),('bms_voltage',51.2),('bms_max_charge_current',240),
            ('bms_max_discharge_current',100),('pv_power',0),('load_power',1200),
            ('battery_power',battery),('grid_power',battery-1200)):
            event(key,value,report=key in {'pv_power','load_power','battery_power','grid_power'})
        for key,value in (('machine_type',0),('inverter_count',1),('topology_generation',generation),
            ('gcf_enable_readback',0),('gcf_export_limit_readback',100),('gcf_generation',generation)):
            event(key,value,report=True)
    plan={**fixture._plan_attributes('tariff_plan'),'status_code':'ready',
        'current_slot_planned':True,'current_action':'grid_support','current_run_need_class':'economic',
        'current_run_start_eligible':True,'current_run_continue_eligible':True,
        'command_charge_power_percent':60.,'requested_charge_power_kw':6.,'target_soc_percent':60.,
        'current_slot_end':deadline.isoformat(),'current_grid_charge_run_end':deadline.isoformat(),
        'model_input_maximum_soc_percent':100.,'control_inputs_fresh':True,
        'forecast_data_fresh':True,'bms_charge_power_limit_kw':3.,
        'configured_daily_fallback_kwh':20.,'load_profile_source':'configured_daily_fallback'}
    for key,value in (
        ('supervisor_mode','Active'),('allow_tariff','on'),('tariff_enabled','on'),
        ('tariff_active','off'),('tariff_control_data_ready','on'),('tariff_planned_charge_slot','on'),
        ('ems_execution_ready','on'),('battery_soc',60),('bms_voltage',51.2),
        ('bms_max_charge_current',240),('bms_max_discharge_current',100),
        ('ems_mode_readback',0),('ems_generation',1),('self_use_soc_readback',20),
        ('backup_soc_readback',80),('charge_soc_readback',80),('charge_power_ems_readback',40),
        ('discharge_soc_readback',20),('discharge_power_readback',50),
        ('gcf_enable_readback',0),('gcf_export_limit_readback',100),('gcf_generation',1)):
        store(key,value)
    store('tariff_plan','ready',plan)
    await sensor.add_to_platform_finish()
    controller=sensor._controller
    controller._clock=lambda:fixture.CLOCK['now']
    controller._dispatch=dispatch
    await drain()
    check(len(writes)==1 and controller.record.state is S.ActiveState.WAITING_READBACK,
        f'callback start failed: {controller.record.state}/{controller.record.reason}')
    baseline=controller.record.transaction.command_snapshot
    fixture.CLOCK['now']=NOW+timedelta(seconds=1)
    physical(2)
    await flush()
    check(controller.record.state is S.ActiveState.EXECUTING,'real callbacks confirm hold60')
    lease_expiry=NOW+timedelta(seconds=30)
    controller._control_lease_valid_until=lambda:lease_expiry
    fixture.CLOCK['now']=NOW+timedelta(seconds=2)
    plan.update(result_current=False,recalculation_pending=True,input_revision=2,
        configured_daily_fallback_kwh=30.,input_change_reason='user_fallback_changed',
        input_change_previous_value='20',input_change_new_value='30')
    event('tariff_plan','ready',plan.copy())
    event('tariff_control_data_ready','off')
    await flush()
    check(controller.record.state is S.ActiveState.EXECUTING and len(writes)==1,
        'real plan/helper pending publications preserve proven hold')
    check(sensor.extra_state_attributes['tariff_decision']['input_change_reason']=='user_fallback_changed'
        and sensor.extra_state_attributes['tariff_decision']['configured_daily_fallback_kwh']==30.,
        'fallback change provenance is missing from the semantic decision')
    fixture.CLOCK['now']=NOW+timedelta(seconds=3)
    plan.update(result_current=True,recalculation_pending=False,current_action='grid_support_and_charge',target_soc_percent=65.)
    event('tariff_plan','ready',plan.copy())
    await flush()
    check(controller.record.state is S.ActiveState.EXECUTING and len(writes)==1,
        'committed plan before readiness helper grants no write')
    fixture.CLOCK['now']=NOW+timedelta(seconds=4)
    event('tariff_control_data_ready','on')
    await flush()
    check(controller.record.state is S.ActiveState.RETARGETING and len(writes)==2,
        f'fresh target and helper must dispatch once: {controller.record.state}/{controller.record.reason}')
    check(writes[-1].ems_block.force_charge_soc_percent_4303==65,'fresh SOC65 survives sensor commitment')
    fixture.CLOCK['now']=NOW+timedelta(seconds=5)
    physical(3,target=65,battery=-4000)
    await flush()
    check(controller.record.state is S.ActiveState.EXECUTING,'new FC03 confirms updated65')
    transaction=controller.record.transaction
    original_handle=sensor._control_lease_client.handle
    sensor._pause_state='off'
    request=sensor._control_lease_client.prepare_arm(
        transaction_id=transaction.transaction_id, hard_deadline=transaction.deadline,
        block=S._control_lease_block(transaction.intent.command.ems_block),
        challenge_nonce='00000001',now_wall=fixture.CLOCK['now'],
        now_monotonic=asyncio.get_running_loop().time())
    check(sensor._control_lease_client.accept_arm(request,dict(schema_version=1,
        protocol_version=2,soft_remaining_ms=120000,maximum_lease_seconds=120,
        renew_interval_seconds=20,accepted=True,reason='accepted',renew_nonce='00000002')),
        'real current lease is required for optimizer commitment')
    commitment=sensor.current_tariff_active_commitment(fixture.CLOCK['now'])
    check(commitment is not None and commitment.transaction_id==controller.record.transaction.transaction_id
        and commitment.hard_deadline==controller.record.transaction.deadline,
        f'same-entry Supervisor exposes the executing physical tariff commitment: '
        f'{controller.record.state}/{controller.record.owner}/'
        f'{controller.record.transaction.intent.action}/'
        f'{sensor._control_lease_client.handle}/'
        f'{controller.record.transaction.physical_verification}')
    check(sensor.current_tariff_active_commitment(
        fixture.CLOCK['now']+timedelta(seconds=31)) is None,
        'stale physical proof cannot stabilize a future optimizer pass')
    sensor._control_lease_client.handle=original_handle
    for generation,second in enumerate(range(10,61,5),4):
        fixture.CLOCK['now']=NOW+timedelta(seconds=second)
        physical(generation,target=65,battery=-4000)
        plan['input_revision']+=1
        event('tariff_plan','ready',plan.copy())
        await flush()
        check(controller.record.state is S.ActiveState.EXECUTING and len(writes)==2,
            f'healthy frame at{second}s must not retune or restore')
    fixture.CLOCK['now']=NOW+timedelta(seconds=62)
    physical(20,target=65,soc=67)
    plan.update(current_action='grid_support',target_soc_percent=65.)
    event('tariff_plan','ready',plan.copy())
    await flush()
    check(controller.record.state is S.ActiveState.EXECUTING and len(writes)==2,'target reached changes meaning without FC16')
    check(sensor.current_tariff_active_commitment(fixture.CLOCK['now']) is None,
        'support-only semantic continuation grants no battery-charge commitment')
    check(controller.record.transaction.command_snapshot==baseline,'callback sequence preserves original baseline')
    # No further state event: the real scheduled callback must expire FC03.
    expiry=fixture.CLOCK['now']+timedelta(seconds=15,microseconds=1)
    fixture.CLOCK['now']=expiry
    for handle in sorted(hass.active_points(),key=lambda item:item.when):
        if handle.when<=expiry: handle.run()
    await flush()
    check(controller.record.state is S.ActiveState.STOPPING and len(writes)==2,
        'silent stale FC03 stops without a write from stale data')
    fixture.CLOCK['now']=expiry+timedelta(seconds=1)
    physical(21,target=65,soc=67)
    await flush()
    check(controller.record.state is S.ActiveState.RESTORING and len(writes)==3,'fresh FC03 permits original restore')
    fixture.CLOCK['now']+=timedelta(seconds=1)
    event('tariff_enabled','off')
    physical(22,target=80,soc=65,mode=0,power=40)
    await flush()
    check(controller.record.state is S.ActiveState.IDLE and controller.record.owner is S.ExecutionOwner.NONE,
        'physical restore releases owner through actual sensor callbacks')
    await sensor.remove_from_platform()

if __name__=='__main__':
    asyncio.run(scenario())
    print(f'Tariff HA callback cadence: {checks} checks PASS (virtual time, offline only)')
