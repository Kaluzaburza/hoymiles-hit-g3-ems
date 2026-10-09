"""Tariff real runtime -> arbiter -> controller -> codec with fake transport only."""
import asyncio
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_supervisor_active_controller import NOW, execution_source as _execution_source, empty_candidate
from test_tariff_optimizer import settings as optimizer_settings
from tariff_optimizer import optimize_tariff_charging
from ems_supervisor import OwnerKind,PolicyId,SupervisorMode,SupervisorProfile,arbitrate_supervisor
from supervisor_runtime import (TariffSourceSnapshot,TariffPlanStatus,TariffRunNeed,TariffAction,
    RceSourceSnapshot,RcmSourceSnapshot,build_tariff_candidate,build_execution_context)
from supervisor_active_controller import ActiveFrame,SupervisorActiveController
from supervisor_executor import ActiveState,ExecutionAction,ExecutionOwner
from supervisor_executor_codec import record_to_dict,record_from_dict

checks=0

def execution_source(at, **overrides):
    # A 10 kW inverter's 60/100% requests must fit the live fixture BMS.
    return _execution_source(at, **{"bms_max_charge_current_a": 240, **overrides})

def check(value,message):
    global checks
    checks+=1
    assert value,message

def encoded_time(value):
    return value.isoformat(timespec='microseconds').replace('+00:00','Z')

def frame(at,src,record=None,*,target=60,action=TariffAction.GRID_SUPPORT,power=60,deadline=None,**overrides):
    deadline=deadline or NOW+timedelta(minutes=30)
    tx=record.transaction if record else None
    owned=bool(tx and record.owner is ExecutionOwner.TARIFF)
    active=bool(owned and record.state in {ActiveState.WAITING_READBACK,ActiveState.EXECUTING,ActiveState.RETARGETING})
    actions={ExecutionAction.TARIFF_GRID_SUPPORT:TariffAction.GRID_SUPPORT,
        ExecutionAction.TARIFF_BATTERY_CHARGE:TariffAction.BATTERY_CHARGE,
        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE:TariffAction.GRID_SUPPORT_AND_CHARGE}
    values=dict(observed_at=at,allowed_by_user=True,enabled=True,active_latched=active,
        status_code=TariffPlanStatus.READY,result_current=True,recalculation_pending=False,
        input_revision=1,current_slot_planned=True,current_action=action,
        current_run_need_class=TariffRunNeed.ECONOMIC,current_run_start_eligible=True,
        current_run_continue_eligible=True,requested_charge_power_kw=power/10,system_power_kw=10,
        command_charge_power_percent=power,current_run_grid_import_kwh=1,current_run_benefit_pln=1,
        target_soc_percent=target,current_soc_percent=src.battery_soc_percent,
        current_soc_observed_at=src.battery_soc_observed_at,maximum_soc_percent=100,
        base_reserve_soc_percent=20,current_slot_end=deadline,
        current_grid_charge_run_end=deadline,control_data_ready=True,planned_slot_ready=True,
        control_inputs_fresh=True,forecast_data_fresh=True,bms_charge_power_limit_kw=3,
        active_action=actions.get(tx.intent.action) if active else None,
        latched_slot_end=tx.deadline if active else None,
        latched_target_soc_percent=tx.intent.command.ems_block.force_charge_soc_percent_4303 if active else None,
        active_4303_readback_percent=src.force_charge_soc_percent,
        active_4304_readback_percent=src.maximum_charge_power_percent)
    values.update(overrides)
    tariff=TariffSourceSnapshot(**values)
    src=replace(src,balancing_active=False,manual_charge_active=False,manual_discharge_active=False,
        rce_active=False,tariff_active=False,rcm_active=False,rcm_export_control_active=False,
        rcm_pre_discharge_active=False,charge_timer_active=False,discharge_timer_active=False)
    context=build_execution_context(src,now=at)
    if owned:
        pending=record.state is not ActiveState.EXECUTING
        context=replace(context,owner_kind=OwnerKind.TARIFF,transaction_pending=pending,
            transaction_owner_kind=OwnerKind.TARIFF if pending else OwnerKind.NONE)
    candidates=(empty_candidate(PolicyId.RCE,at),build_tariff_candidate(tariff,now=at),empty_candidate(PolicyId.RCM,at))
    decision=arbitrate_supervisor(mode=SupervisorMode.ACTIVE,profile=SupervisorProfile.BALANCED,
        context=context,candidates=candidates,now=at)
    return ActiveFrame(now=at,decision=decision,candidates=candidates,context=context,
        rce=RceSourceSnapshot(),tariff=tariff,rcm=RcmSourceSnapshot(),execution=src)

def physical(at,*,target=58,soc=60,battery=0,generation=11,mode=4,power=60):
    sample=at-timedelta(milliseconds=100)
    return execution_source(at,physical_mode_code=mode,full_block_generation=generation,
        full_block_generation_at=sample,force_charge_soc_percent=target,maximum_charge_power_percent=power,
        battery_soc_percent=soc,battery_soc_observed_at=sample,
        grid_power_w=battery-1200,grid_power_observed_at=sample,
        battery_power_w=battery,battery_power_observed_at=sample,pv_power_w=0,pv_power_observed_at=sample,
        load_power_w=1200,load_power_observed_at=sample)

async def scenario():
    sent=[]; saved=[]; clock=[NOW]
    async def persist(payload):
        saved.append(payload)
    async def dispatch(write): sent.append(write)
    controller=SupervisorActiveController(persist=persist,dispatch=dispatch,publish=lambda record:None,clock=lambda:clock[0])
    await controller.async_initialize()
    initial=execution_source(NOW)
    await controller.async_reconcile(frame(NOW,initial))
    check(controller.record.state is ActiveState.WAITING_READBACK,f'initial Mode 4 dispatch: {controller.record.reason}')
    baseline=controller.record.transaction.command_snapshot
    check(len(sent)==1 and sent[0].ems_block is not None,'one complete initial block')
    check(sent[0].ems_block.force_charge_soc_percent_4303==58,'physical SOC60 holds at encoded target58')
    clock[0]=NOW+timedelta(seconds=2)
    src=physical(clock[0])
    await controller.async_reconcile(frame(clock[0],src,controller.record))
    check(controller.record.state is ActiveState.EXECUTING,'hold60 physically confirmed')
    clock[0]+=timedelta(seconds=1)
    src=physical(clock[0],generation=12)
    await controller.async_reconcile(frame(clock[0],src,controller.record,target=65,action=TariffAction.GRID_SUPPORT_AND_CHARGE))
    check(controller.record.state is ActiveState.RETARGETING,f'direct charge65: {controller.record.reason}')
    check(len(sent)==2 and sent[-1].ems_block.mode.value==4,'Mode 4 stays active, no neutral write')
    check(controller.record.transaction.command_snapshot==baseline,'original restore baseline preserved')
    encoded=record_to_dict(controller.record)
    check(record_from_dict(encoded).state is ActiveState.RETARGETING,'tariff RETARGETING survives strict codec')
    clock[0]+=timedelta(seconds=2)
    src=physical(clock[0],target=65,battery=-5000,generation=13)
    await controller.async_reconcile(frame(clock[0],src,controller.record,target=65,action=TariffAction.GRID_SUPPORT_AND_CHARGE))
    check(controller.record.state is ActiveState.EXECUTING,'charge65 gets separate newer FC03 and physical proof')
    clock[0]+=timedelta(seconds=2)
    src=physical(clock[0],target=65,soc=67,generation=14)
    await controller.async_reconcile(frame(clock[0],src,controller.record,target=65,action=TariffAction.GRID_SUPPORT_AND_CHARGE))
    check(controller.record.state is ActiveState.EXECUTING,'target reached keeps house on grid')
    await controller.async_reconcile(frame(clock[0],src,controller.record,target=65,action=TariffAction.GRID_SUPPORT))
    check(controller.record.state is ActiveState.EXECUTING,'semantic charge to hold stays active')
    check(controller.record.transaction.intent.action is ExecutionAction.TARIFF_GRID_SUPPORT,'new hold meaning adopted')
    check(len(sent)==2,'same encoded command emits no FC16')
    check(controller.record.transaction.command_snapshot==baseline,'semantic update preserves original restore baseline')

async def new_controller(*, deadline=None):
    sent=[]; saved=[]; clock=[NOW]
    async def persist(record): saved.append(record_to_dict(record))
    async def dispatch(write): sent.append(write)
    controller=SupervisorActiveController(persist=persist,dispatch=dispatch,
        publish=lambda record:None,clock=lambda:clock[0])
    await controller.async_initialize()
    await controller.async_reconcile(frame(NOW,execution_source(NOW),deadline=deadline))
    return controller,sent,saved,clock

async def confirmed_controller(*, deadline=None):
    controller,sent,saved,clock=await new_controller(deadline=deadline)
    clock[0]=NOW+timedelta(seconds=2)
    await controller.async_reconcile(frame(clock[0],physical(clock[0]),controller.record,
        deadline=deadline))
    check(controller.record.state is ActiveState.EXECUTING,'fixture physically executes hold')
    return controller,sent,saved,clock

def optimizer_frame(at,src,plan,record=None,*,input_revision):
    action=(None if plan.current_action=='none' else TariffAction(plan.current_action))
    need=TariffRunNeed(plan.current_run_need_class)
    deadline=(plan.current_grid_charge_run_end or plan.current_slot_end
        or (record.transaction.deadline if record and record.transaction else at+timedelta(minutes=30)))
    deadline=deadline.astimezone(ZoneInfo('UTC'))
    slot_end=(plan.current_slot_end.astimezone(ZoneInfo('UTC'))
        if plan.current_slot_end else None)
    run_end=(plan.current_grid_charge_run_end.astimezone(ZoneInfo('UTC'))
        if plan.current_grid_charge_run_end else None)
    return frame(at,src,record,target=plan.target_soc_percent,
        action=action or TariffAction.GRID_SUPPORT,
        power=60,deadline=deadline,input_revision=input_revision,
        current_slot_planned=plan.current_slot_planned,current_action=action,
        current_run_need_class=need,
        current_run_start_eligible=plan.current_run_start_eligible,
        current_run_continue_eligible=plan.current_run_continue_eligible,
        current_run_grid_import_kwh=plan.current_run_grid_import_kwh,
        current_run_benefit_pln=plan.current_run_benefit_pln,
        current_slot_end=slot_end,current_grid_charge_run_end=run_end)

async def optimizer_replan_preserves_confirmed_tariff_run():
    """Exercise optimizer -> arbiter -> controller across the real 120 s replan."""
    local_start=datetime(2026,9,16,5,1,tzinfo=ZoneInfo('Europe/Warsaw'))
    start=local_start.astimezone(ZoneInfo('UTC'))
    base=optimizer_settings(local_start,battery_capacity_kwh=26.0,
        battery_soc_percent=38.0,reserve_soc_percent=50.0,
        base_reserve_soc_percent=25.0,maximum_soc_percent=95.0,
        average_daily_load_kwh=0.0,average_night_load_kwh=0.0,
        load_by_slot_kwh={local_start.replace(minute=0):.4},current_load_power_kw=0.8,
        current_pv_power_kw=0.0,current_battery_power_kw=0.8,
        charge_power_kw=6.0,battery_charge_power_kw=6.0,
        charge_efficiency_percent=92.0,discharge_efficiency_percent=95.0,
        minimum_saving_pln_kwh=0.05)
    initial_plan=optimize_tariff_charging(base)
    check(initial_plan.current_action=='grid_support_and_charge'
        and initial_plan.current_grid_charge_run_end==local_start.replace(hour=6,minute=0),
        '05:01 optimizer starts one continuous charge run through 06:00')

    sent=[]; saved=[]; clock=[start]
    async def persist(record): saved.append(record_to_dict(record))
    async def dispatch(write): sent.append(write)
    controller=SupervisorActiveController(persist=persist,dispatch=dispatch,
        publish=lambda record:None,clock=lambda:clock[0])
    await controller.async_initialize()
    source=execution_source(start,battery_soc_percent=38.0,
        battery_soc_observed_at=start-timedelta(milliseconds=100))
    await controller.async_reconcile(optimizer_frame(start,source,initial_plan,
        input_revision=1))
    check(controller.record.state is ActiveState.WAITING_READBACK and len(sent)==1,
        'optimizer plan dispatches one Mode 4 block')
    command_target=sent[0].ems_block.force_charge_soc_percent_4303
    clock[0]=start+timedelta(seconds=2)
    source=physical(clock[0],target=command_target,soc=38.2,battery=-5000,generation=501)
    await controller.async_reconcile(optimizer_frame(clock[0],source,initial_plan,
        controller.record,input_revision=1))
    check(controller.record.state is ActiveState.EXECUTING,
        f'physical Mode 4 and charging flow confirm the transaction: '
        f'{controller.record.state.value}/{controller.record.reason}/'
        f'{sent[0].ems_block.force_charge_soc_percent_4303}')

    clock[0]=start+timedelta(seconds=122)
    replanned_input=replace(base,now=local_start+timedelta(seconds=122),battery_soc_percent=40.0,
        current_load_power_kw=1.2,current_battery_power_kw=-5.0)
    uncommitted=optimize_tariff_charging(replanned_input)
    check(not uncommitted.current_slot_planned and uncommitted.current_action=='none'
        and uncommitted.planned_charges[0].start==local_start.replace(minute=30,second=0),
        'control fixture reproduces the 120 s replan moving charge to 05:30')

    source=physical(clock[0],target=command_target,soc=40.0,
        battery=-5000,generation=502)
    await controller.async_reconcile(optimizer_frame(clock[0],source,initial_plan,
        controller.record,input_revision=1))
    check(controller.record.state is ActiveState.EXECUTING and len(sent)==1,
        'fresh physical cohort keeps the executing transaction authoritative')
    transaction=controller.record.transaction
    commitment=SimpleNamespace(transaction_id=transaction.transaction_id,
        action=initial_plan.current_action,started_at=transaction.started_at,
        hard_deadline=transaction.deadline,
        target_soc_percent=transaction.intent.command.ems_block.force_charge_soc_percent_4303,
        maximum_charge_power_percent=transaction.intent.command.ems_block.maximum_charge_power_percent_4304,
        physical_verified_at=transaction.physical_verification.observed_at)
    committed=optimize_tariff_charging(replace(replanned_input,
        active_commitment=commitment))
    check(committed.current_slot_planned
        and committed.current_action=='grid_support_and_charge'
        and committed.current_grid_charge_run_end<=transaction.deadline,
        'authoritative active transaction stabilizes the still-justified current run')
    source=physical(clock[0],target=command_target,soc=40.0,battery=-5000,generation=503)
    await controller.async_reconcile(optimizer_frame(clock[0],source,committed,
        controller.record,input_revision=2))
    check(controller.record.state is ActiveState.EXECUTING and len(sent)==1,
        '120 s replan preserves continuous Mode 4 without STOP or duplicate FC16')

async def latest_target_and_timeout():
    controller,sent,saved,clock=await new_controller()
    original=controller.record.transaction
    for offset,target in ((1,63),(2,65),(3,67)):
        clock[0]=NOW+timedelta(seconds=offset)
        await controller.async_reconcile(frame(clock[0],execution_source(NOW),controller.record,
            target=target,action=TariffAction.GRID_SUPPORT_AND_CHARGE))
        check(len(sent)==1,'A waits for its physical ACK while desired targets coalesce')
        check(controller.record.transaction.command_sent_at==original.command_sent_at,'replan never renews ACK time')
    clock[0]=NOW+timedelta(seconds=4)
    await controller.async_reconcile(frame(clock[0],physical(clock[0]),controller.record,
        target=67,action=TariffAction.GRID_SUPPORT_AND_CHARGE))
    check(len(sent)==2 and sent[-1].ems_block.force_charge_soc_percent_4303==67,'only newest target67 dispatched after A proof')
    check(controller.record.transaction.command_snapshot==original.command_snapshot,'coalescing preserves baseline')
    old_generation=physical(clock[0],target=58,generation=11)
    clock[0]+=timedelta(seconds=1)
    await controller.async_reconcile(frame(clock[0],old_generation,controller.record,
        target=67,action=TariffAction.GRID_SUPPORT_AND_CHARGE))
    check(controller.record.state is ActiveState.RETARGETING,'old physical generation cannot confirm successor')
    clock[0]+=timedelta(seconds=1)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],target=67,generation=12,battery=-4000),
        controller.record,target=67,action=TariffAction.GRID_SUPPORT_AND_CHARGE))
    check(controller.record.state is ActiveState.EXECUTING,'successor needs its own new physical generation')

    controller,sent,saved,clock=await new_controller()
    clock[0]=NOW+timedelta(seconds=91)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],target=60,generation=11),controller.record))
    check(controller.record.state is not ActiveState.EXECUTING,'late ACK does not resurrect expired write')
    check(controller.record.owner is ExecutionOwner.TARIFF,'timeout keeps owner until verified restore')

async def pending_and_stop():
    controller,sent,saved,clock=await confirmed_controller()
    clock[0]+=timedelta(seconds=1)
    async def pending(at):
        await controller.async_reconcile(frame(at,physical(at,generation=12),controller.record,
            result_current=False,recalculation_pending=True,control_data_ready=False))
    await pending(clock[0])
    check(controller.record.state is ActiveState.EXECUTING,'explicit same-run recalculation holds proven command')
    anchor=controller.tariff_execution_settling_deadline
    for offset in (1,20,56):
        clock[0]=NOW+timedelta(seconds=3+offset)
        await pending(clock[0])
        check(controller.tariff_execution_settling_deadline==anchor,
            'repeated pending events do not postpone tariff settling')
        check(len(sent)==1,'pending plan never grants new FC16')
        check(controller.record.state is ActiveState.EXECUTING,
            f'pending replan at {offset}s remains in the original transaction')
    clock[0]=NOW+timedelta(seconds=182, milliseconds=999)
    await pending(clock[0])
    check(controller.record.state is ActiveState.EXECUTING,
        f'fixed settling budget holds through 179.999s: {controller.record.state}/{controller.record.reason}')
    clock[0]=NOW+timedelta(seconds=183)
    await pending(clock[0])
    check(controller.record.state is ActiveState.RESTORING,
        f'fixed180s pending timeout restores: {controller.record.state}/{controller.record.reason}')
    check(controller.record.owner is ExecutionOwner.TARIFF,'restoration retains owner')
    check(sent[-1].ems_block.mode.value==0,'restoration uses original mode')
    clock[0]+=timedelta(seconds=2)
    src=execution_source(clock[0],full_block_generation=13,full_block_generation_at=clock[0]-timedelta(milliseconds=100))
    await controller.async_reconcile(frame(clock[0],src,controller.record,enabled=False))
    check(controller.record.state is ActiveState.IDLE and controller.record.owner is ExecutionOwner.NONE,'new FC03 releases restored owner')

    for name,override,source_override in (
        ('disabled',{'enabled':False},{}),
        ('permission revoked',{'allowed_by_user':False},{}),
        ('real schedule gap',{'current_slot_planned':False},{}),
        ('missing controls',{'control_data_ready':False,'control_inputs_fresh':False},{}),
        ('missing forecast',{'control_data_ready':False,'forecast_data_fresh':False},{}),
        ('BMS reduced while pending',{'result_current':False,'recalculation_pending':True,'control_data_ready':False},{'bms_max_charge_current_a':10}),
        ('SOC cap reduced while pending',{'maximum_soc_percent':55,'result_current':False,'recalculation_pending':True,'control_data_ready':False},{}),
        ('BMS zero',{}, {'bms_max_charge_current_a':0}),
        ('Off-Grid',{}, {'physical_mode_code':3}),
    ):
        for waiting in (True,False):
            controller,sent,saved,clock=await (new_controller() if waiting else confirmed_controller())
            clock[0]+=timedelta(seconds=1)
            src=replace(physical(clock[0],generation=12),**source_override)
            await controller.async_reconcile(frame(clock[0],src,controller.record,
                target=65,action=TariffAction.GRID_SUPPORT_AND_CHARGE,**override))
            check(controller.record.state not in {ActiveState.WAITING_READBACK,ActiveState.EXECUTING,ActiveState.RETARGETING},f'{name} stops even during target change, waiting={waiting}')
            if name=='Off-Grid':
                check(all(w.ems_block.mode.value==3 for w in sent[1:]),'automatic cleanup preserves physical Off-Grid mode')

async def executable_status_pending_boundaries():
    for status in (TariffPlanStatus.READY, TariffPlanStatus.INSUFFICIENT_CHEAP_WINDOW):
        controller,sent,saved,clock=await confirmed_controller()
        anchor_at=NOW+timedelta(seconds=3)
        async def pending(at):
            clock[0]=at
            await controller.async_reconcile(frame(at,physical(at,generation=12+int((at-NOW).total_seconds()*10)),
                controller.record,status_code=status,result_current=False,
                recalculation_pending=True,control_data_ready=False))
        await pending(anchor_at)
        anchor=controller.tariff_execution_settling_deadline
        check(controller.record.state is ActiveState.EXECUTING,
            f'{status.value} pending starts bounded hold')
        for elapsed in (0.1,0.8,6.0,20.0,59.999,120.,179.999):
            await pending(anchor_at+timedelta(seconds=elapsed))
            check(controller.record.state is ActiveState.EXECUTING,
                f'{status.value} pending holds at {elapsed}s')
            check(controller.tariff_execution_settling_deadline==anchor,
                f'{status.value} repeated pending cannot move fixed anchor')
            check(len(sent)==1,f'{status.value} pending cannot dispatch FC16')
        await pending(anchor_at+timedelta(seconds=180.0))
        check(controller.record.state is ActiveState.RESTORING,
            f'{status.value} pending restores exactly at 180.0s')
        check([write.ems_block.mode.value for write in sent]==[4,0],
            f'{status.value} timeout emits only the original Mode4 and one restore')

        controller,sent,saved,clock=await confirmed_controller()
        anchor_at=NOW+timedelta(seconds=3)
        await controller.async_reconcile(frame(anchor_at,physical(anchor_at,generation=22),
            controller.record,status_code=status,result_current=False,
            recalculation_pending=True,control_data_ready=False))
        clock[0]=anchor_at+timedelta(seconds=0.8)
        await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=23),
            controller.record,status_code=status,result_current=True,
            recalculation_pending=False,control_data_ready=False))
        check(controller.record.state is ActiveState.EXECUTING,
            f'{status.value} committed successor may wait for lagging readiness helper')
        check(len(sent)==1,
            f'{status.value} lagging helper retains only the confirmed command')

        controller,sent,saved,clock=await confirmed_controller()
        await pending_probe_after_six_seconds(controller,sent,clock,status)

async def pending_probe_after_six_seconds(controller,sent,clock,status):
    anchor_at=NOW+timedelta(seconds=3)
    clock[0]=anchor_at
    await controller.async_reconcile(frame(anchor_at,physical(anchor_at,generation=31),
        controller.record,status_code=status,result_current=False,
        recalculation_pending=True,control_data_ready=False))
    later=anchor_at+timedelta(seconds=6)
    clock[0]=later
    await controller.async_reconcile(frame(later,physical(later,generation=32),
        controller.record,status_code=status,result_current=False,
        recalculation_pending=True,control_data_ready=False))
    check(controller.record.state is ActiveState.EXECUTING,
        f'{status.value} pending survives 6s inside the fixed budget')
    check([write.ems_block.mode.value for write in sent]==[4],
        f'{status.value} 6s settling emits no restore or replacement write')


async def pending_budget_is_capped_by_transaction_deadline():
    short_deadline=NOW+timedelta(seconds=12)
    controller,sent,saved,clock=await confirmed_controller()
    controller._executor._record=replace(
        controller.record,
        transaction=replace(
            controller.record.transaction,
            deadline=short_deadline,
        ),
    )
    controller._tariff_planned_end=(
        controller.record.transaction.transaction_id,
        short_deadline,
    )
    anchor_at=NOW+timedelta(seconds=3)
    check(controller._bounded_tariff_execution_hold(
        controller.record.transaction,now=anchor_at),
        'settling starts inside the immutable transaction deadline')
    check(controller._bounded_tariff_execution_hold(
        controller.record.transaction,
        now=short_deadline-timedelta(microseconds=1)),
        'settling remains valid immediately before the immutable transaction deadline')
    check(not controller._bounded_tariff_execution_hold(
        controller.record.transaction,now=short_deadline),
        'transaction deadline ends settling even before the 60s budget')


async def pending_budget_is_capped_by_last_confirmed_lease():
    controller,sent,saved,clock=await confirmed_controller()
    lease_deadline=[NOW+timedelta(seconds=20)]
    controller._control_lease_valid_until=lambda:lease_deadline[0]
    anchor_at=NOW+timedelta(seconds=3)
    transaction=controller.record.transaction
    check(controller._bounded_tariff_execution_hold(transaction,now=anchor_at),
        'fresh confirmed lease permits bounded tariff settling')
    fixed=controller.tariff_execution_settling_deadline
    check(fixed==anchor_at+timedelta(seconds=180),
        'policy hold is fixed independently of shorter renewable grants')
    lease_deadline[0]=NOW+timedelta(seconds=40)
    check(controller._bounded_tariff_execution_hold(
        transaction,now=NOW+timedelta(seconds=19, milliseconds=999)),
        'settling remains valid immediately before the original lease expiry')
    check(controller.tariff_execution_settling_deadline==fixed,
        'a later renewal cannot extend an already anchored settling budget')
    check(not controller._bounded_tariff_execution_hold(
        transaction,now=NOW+timedelta(seconds=40)),
        'settling stops at the latest confirmed ESP lease expiry')

async def restart_and_rejected_successor():
    controller,sent,saved,clock=await confirmed_controller()
    baseline=controller.record.transaction.command_snapshot
    async def persist(record): saved.append(record_to_dict(record))
    async def dispatch(write): sent.append(write)
    clock[0]+=timedelta(seconds=1)
    src=physical(clock[0],generation=12)
    latest=lambda:frame(clock[0],src,controller.record,target=68,action=TariffAction.GRID_SUPPORT_AND_CHARGE)
    controller._frame_resampler=latest
    await controller.async_reconcile(frame(clock[0],src,controller.record,target=65,action=TariffAction.GRID_SUPPORT_AND_CHARGE))
    check(len(sent)==1 and controller.record.state is ActiveState.EXECUTING,'unsent B superseded by C preserves running A and owner')
    check(controller.record.transaction.command_snapshot==baseline,'rejected successor preserves original baseline')
    await controller.async_reconcile(latest())
    check(len(sent)==2 and sent[-1].ems_block.force_charge_soc_percent_4303==68,'next frame dispatches newest C only')
    persisted=record_from_dict(record_to_dict(controller.record))
    restored=SupervisorActiveController(persist=persist,dispatch=dispatch,publish=lambda record:None,
        persisted_record=persisted,clock=lambda:clock[0])
    await restored.async_initialize()
    check(restored.record.owner is ExecutionOwner.TARIFF,'restart retains uncertain transaction owner')
    check(restored.record.transaction.command_snapshot==baseline,'restart retains original baseline')
    check(len(sent)==2,'restart never replays successor')

async def main():
    await scenario()
    await optimizer_replan_preserves_confirmed_tariff_run()
    await latest_target_and_timeout()
    await pending_and_stop()
    await executable_status_pending_boundaries()
    await pending_budget_is_capped_by_transaction_deadline()
    await pending_budget_is_capped_by_last_confirmed_lease()
    await restart_and_rejected_successor()
    await bounded_inverter_effect_delay()
    await staggered_power_cohorts()
    await critical_grid_export_stops_without_complete_cohort()
    await charge_hold_charge_cycle()
    await power_changes_without_neutral()
    await window_updates_preserve_continuous_run()

async def bounded_inverter_effect_delay():
    controller,sent,saved,clock=await new_controller()
    original=controller.record.transaction
    for second in (2,10,20,30):
        clock[0]=NOW+timedelta(seconds=second)
        await controller.async_reconcile(frame(clock[0],physical(clock[0],battery=1200,generation=11+second),controller.record))
        check(controller.record.state is ActiveState.WAITING_READBACK and len(sent)==1,'Mode4 ACK while old Self-Use flows settle grants no new write or success')
        check(controller.record.transaction.command_sent_at==original.command_sent_at,'physical settling never renews the original timeout')
    clock[0]=NOW+timedelta(seconds=35)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=50),controller.record))
    check(controller.record.state is ActiveState.EXECUTING,'actual grid import and idle battery confirm delayed effect')
    clock[0]=NOW+timedelta(seconds=40)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=51,battery=1200),controller.record))
    check(controller.record.state not in {ActiveState.WAITING_READBACK,ActiveState.EXECUTING},'after physical proof a reversed battery flow still stops immediately')

    controller,sent,saved,clock=await new_controller()
    for second in (2,30,60,89,120,179):
        clock[0]=NOW+timedelta(seconds=second)
        await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=11+second,battery=1200),controller.record))
    clock[0]=NOW+timedelta(seconds=180)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=110,battery=1200),controller.record))
    check(controller.record.state not in {ActiveState.WAITING_READBACK,ActiveState.EXECUTING},'no physical effect at original180s deadline ends the wait')
    check(controller.record.owner is ExecutionOwner.TARIFF,'timeout retains owner until separate physical restoration')

async def staggered_power_cohorts():
    controller,sent,saved,clock=await confirmed_controller()
    cohort_at=clock[0]-timedelta(milliseconds=100)
    for index in range(4):
        previous=controller.record.transaction.physical_verification
        clock[0]+=timedelta(seconds=12)
        partial=replace(physical(clock[0],generation=20+2*index),
            grid_power_observed_at=cohort_at,pv_power_observed_at=cohort_at,
            load_power_observed_at=cohort_at)
        await controller.async_reconcile(frame(clock[0],partial,controller.record))
        check(controller.record.state is ActiveState.EXECUTING and len(sent)==1,'field-like faster BAT report must not restore between coherent GRID/PV/LOAD cohorts')
        check(controller.record.transaction.physical_verification==previous,'partial cohort must not refresh the accepted physical proof')
        clock[0]+=timedelta(seconds=5)
        coherent=physical(clock[0],generation=21+2*index)
        await controller.async_reconcile(frame(clock[0],coherent,controller.record))
        check(controller.record.state is ActiveState.EXECUTING and len(sent)==1,'17s physical cohort cadence remains stable with fresh EMS FC03')
        cohort_at=coherent.grid_power_observed_at
    last_proof=controller.record.transaction.physical_verification
    for offset in (6,11,16,21,26):
        clock[0]=last_proof.observed_at+timedelta(seconds=offset)
        partial=replace(physical(clock[0],generation=100+offset),
            grid_power_observed_at=cohort_at,pv_power_observed_at=cohort_at,
            load_power_observed_at=cohort_at)
        await controller.async_reconcile(frame(clock[0],partial,controller.record))
        check(controller.record.state is ActiveState.EXECUTING and len(sent)==1,'partial events do not force an early restore or grant another write')
        check(controller.record.transaction.physical_verification==last_proof,'partial reports cannot extend the last coherent proof')
    clock[0]=last_proof.observed_at+timedelta(seconds=30)
    partial=replace(physical(clock[0],generation=140),
        grid_power_observed_at=cohort_at,pv_power_observed_at=cohort_at,
        load_power_observed_at=cohort_at)
    await controller.async_reconcile(frame(clock[0],partial,controller.record))
    check(controller.record.state is ActiveState.RESTORING and len(sent)==2,'30s without a new coherent power cohort restores from fresh EMS FC03')
    check(controller.record.owner is ExecutionOwner.TARIFF,'physical-data timeout retains owner until restore ACK')

async def critical_grid_export_stops_without_complete_cohort():
    controller,sent,saved,clock=await confirmed_controller()
    clock[0]+=timedelta(seconds=1)
    sample=clock[0]-timedelta(milliseconds=100)
    masked=replace(
        physical(clock[0],generation=12),
        power_cohort_complete=True,
        critical_grid_power_w=1000,
        critical_grid_power_observed_at=sample,
    )
    await controller.async_reconcile(frame(clock[0],masked,controller.record))
    check(
        controller.record.state is ActiveState.RESTORING
        and len(sent)==2
        and sent[-1].ems_block.mode.value==0,
        'fresh forbidden GRID export must restore even when the accepted balance cohort still shows import',
    )

async def charge_hold_charge_cycle():
    controller,sent,saved,clock=await confirmed_controller()
    baseline=controller.record.transaction.command_snapshot
    deadline=controller.record.transaction.deadline
    generation=100
    for target,action,previous_target,before_battery,after_battery in (
        (65,TariffAction.GRID_SUPPORT_AND_CHARGE,58,0,-4000),
        (58,TariffAction.GRID_SUPPORT,65,-4000,0),
        (67,TariffAction.GRID_SUPPORT_AND_CHARGE,58,0,-4000),
    ):
        clock[0]+=timedelta(seconds=2)
        generation+=1
        source=physical(clock[0],target=previous_target,battery=before_battery,generation=generation)
        await controller.async_reconcile(frame(clock[0],source,controller.record,target=target,action=action))
        check(controller.record.state is ActiveState.RETARGETING,'charge/hold/charge changes encoded target directly')
        check(sent[-1].ems_block.mode.value==4 and sent[-1].ems_block.force_charge_soc_percent_4303==target,'full successor block stays Mode4 with exact target')
        clock[0]+=timedelta(seconds=2)
        generation+=1
        source=physical(clock[0],target=target,battery=before_battery,generation=generation)
        await controller.async_reconcile(frame(clock[0],source,controller.record,target=target,action=action))
        if before_battery != after_battery:
            check(controller.record.state is ActiveState.RETARGETING,'new target ACK alone cannot prove the changed physical effect')
        clock[0]+=timedelta(seconds=2)
        generation+=1
        source=physical(clock[0],target=target,battery=after_battery,generation=generation)
        await controller.async_reconcile(frame(clock[0],source,controller.record,target=target,action=action))
        check(controller.record.state is ActiveState.EXECUTING,'new coherent flow confirms each phase')
        check(controller.record.transaction.command_snapshot==baseline and controller.record.transaction.deadline==deadline,'whole cycle preserves original baseline and deadline')
    check(len(sent)==4 and all(w.ems_block.mode.value==4 for w in sent),'entire cycle has no neutral write and exactly one FC16 per changed target')

async def power_changes_without_neutral():
    controller,sent,saved,clock=await confirmed_controller()
    baseline=controller.record.transaction.command_snapshot
    deadline=controller.record.transaction.deadline
    previous_target,previous_power,previous_battery,generation=58,60,0,200
    for target,power,action,battery in (
        (58,30,TariffAction.GRID_SUPPORT,0),
        (58,60,TariffAction.GRID_SUPPORT,0),
        (65,60,TariffAction.GRID_SUPPORT_AND_CHARGE,-6000),
        (65,30,TariffAction.GRID_SUPPORT_AND_CHARGE,-3000),
        (65,60,TariffAction.GRID_SUPPORT_AND_CHARGE,-6000),
    ):
        clock[0]+=timedelta(seconds=2); generation+=1
        old=physical(clock[0],target=previous_target,power=previous_power,battery=previous_battery,generation=generation)
        await controller.async_reconcile(frame(clock[0],old,controller.record,target=target,power=power,action=action))
        check(controller.record.state is ActiveState.RETARGETING,'power/target change uses same-owner retarget')
        count=len(sent)
        check(sent[-1].ems_block.maximum_charge_power_percent_4304==power,'exact requested charging power goes into full block')
        await controller.async_reconcile(frame(clock[0],old,controller.record,target=target,power=power,action=action))
        check(len(sent)==count,'same generation cannot ACK the new power or repeat its write')
        clock[0]+=timedelta(seconds=2); generation+=1
        new=physical(clock[0],target=target,power=power,battery=battery,generation=generation)
        await controller.async_reconcile(frame(clock[0],new,controller.record,target=target,power=power,action=action))
        check(controller.record.state is ActiveState.EXECUTING,'new FC03 plus flow confirms changed power')
        check(controller.record.transaction.command_snapshot==baseline and controller.record.transaction.deadline==deadline,'power changes preserve baseline and fixed window')
        previous_target,previous_power,previous_battery=target,power,battery
    check(len(sent)==6 and all(w.ems_block.mode.value==4 for w in sent),'hold and charge power changes never send neutral')

async def window_updates_preserve_continuous_run():
    hard_end=NOW+timedelta(minutes=30)
    short_end=NOW+timedelta(seconds=10)
    controller,sent,saved,clock=await confirmed_controller()
    baseline=controller.record.transaction.command_snapshot

    clock[0]=NOW+timedelta(seconds=3)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=21),
        controller.record,deadline=hard_end,current_grid_charge_run_end=short_end,
        current_slot_end=NOW+timedelta(minutes=15),input_revision=2))
    check(controller.record.state is ActiveState.EXECUTING,
        'shortening the economic end keeps the proven Mode 4 run active')
    check([write.ems_block.mode.value for write in sent]==[4],
        'shortening the economic end emits no neutral or duplicate FC16')
    check(controller.record.transaction.deadline==hard_end
        and controller.record.transaction.command_snapshot==baseline,
        'shortening preserves the immutable hard deadline and rollback baseline')
    attrs=controller.recorder_attributes()
    check(attrs['tariff_planned_end']==encoded_time(short_end)
        and attrs['tariff_raw_plan_end']==encoded_time(short_end)
        and attrs['deadline']==encoded_time(hard_end),
        'raw, effective planned and hard ends remain separately observable')
    check(controller.execution_watchdog==(controller.record.transaction.transaction_id,short_end),
        'the shortened effective end becomes the exact next watchdog boundary')

    clock[0]=NOW+timedelta(seconds=4)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=22),
        controller.record,deadline=hard_end,current_grid_charge_run_end=hard_end,
        current_slot_end=NOW+timedelta(minutes=15),input_revision=3))
    check(controller.record.state is ActiveState.EXECUTING and len(sent)==1,
        'moving the plan end back to the original hard end stays continuously in Mode 4')
    attrs=controller.recorder_attributes()
    check(attrs['tariff_planned_end']==encoded_time(hard_end)
        and attrs['tariff_raw_plan_end']==encoded_time(hard_end)
        and controller.record.transaction.deadline==hard_end,
        'moving the plan end back cannot replace or extend the original deadline')

    extended_end=NOW+timedelta(minutes=40)
    clock[0]=NOW+timedelta(seconds=5)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=23),
        controller.record,deadline=hard_end,current_grid_charge_run_end=extended_end,
        input_revision=4))
    attrs=controller.recorder_attributes()
    check(controller.record.state is ActiveState.EXECUTING and len(sent)==1,
        'a raw plan extension remains a no-write same-run update')
    check(controller.record.transaction.deadline==hard_end
        and attrs['tariff_planned_end']==encoded_time(hard_end)
        and attrs['tariff_raw_plan_end']==encoded_time(extended_end),
        'a raw plan extension is capped by the immutable original hard deadline')

    clock[0]=NOW+timedelta(seconds=6)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=24),
        controller.record,deadline=hard_end,current_grid_charge_run_end=extended_end,
        current_slot_end=NOW+timedelta(minutes=20),input_revision=5))
    check(controller.record.state is ActiveState.EXECUTING and len(sent)==1,
        'slot boundaries and a new plan revision alone neither stop nor rewrite Mode 4')

    controller,sent,saved,clock=await confirmed_controller()
    clock[0]=NOW+timedelta(seconds=3)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=31),
        controller.record,deadline=hard_end,current_grid_charge_run_end=short_end,
        input_revision=2))
    clock[0]=NOW+timedelta(seconds=4)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=32),
        controller.record,deadline=hard_end,current_grid_charge_run_end=short_end,
        input_revision=2,result_current=False,recalculation_pending=True,
        control_data_ready=False))
    check(controller.record.state is ActiveState.EXECUTING and len(sent)==1
        and controller.record.transaction.deadline==hard_end,
        'pending recalculation keeps the shortened run without extending its hard deadline')
    clock[0]=NOW+timedelta(seconds=5)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=33),
        controller.record,deadline=hard_end,current_grid_charge_run_end=short_end,
        input_revision=3))
    check(controller.record.state is ActiveState.EXECUTING and len(sent)==1,
        'a current successor clears the bounded pending hold without a write')
    before_end=short_end-timedelta(microseconds=1)
    clock[0]=before_end
    await controller.async_reconcile(frame(before_end,physical(before_end,generation=34),
        controller.record,deadline=hard_end,current_grid_charge_run_end=short_end,
        input_revision=3))
    check(controller.record.state is ActiveState.EXECUTING and len(sent)==1,
        'the shortened run remains active immediately before its effective end')
    clock[0]=short_end
    await controller.async_reconcile(frame(short_end,physical(short_end,generation=35),
        controller.record,deadline=hard_end,current_grid_charge_run_end=short_end,
        input_revision=3))
    check(controller.record.state is ActiveState.RESTORING
        and [write.ems_block.mode.value for write in sent]==[4,0],
        'the effective planned end emits exactly one normal restore at equality')

    controller,sent,saved,clock=await confirmed_controller()
    clock[0]=NOW+timedelta(seconds=3)
    await controller.async_reconcile(frame(clock[0],physical(clock[0],generation=41),
        controller.record,deadline=hard_end,current_action=None,input_revision=2))
    check(controller.record.state is ActiveState.RESTORING
        and [write.ems_block.mode.value for write in sent]==[4,0],
        'a real no-action plan still stops and restores normally')

if __name__=='__main__':
    asyncio.run(main())
    print(f'Tariff controller: {checks} checks passed (offline only)')
