"""Offline initial-RCE pre-transport-rejection regression."""
from __future__ import annotations
import asyncio
from dataclasses import replace
from datetime import timedelta
import importlib.util
from pathlib import Path
import runpy
import sys

HERE=Path(__file__).resolve().parent
WORK=HERE.parent
sys.path.insert(0,str(WORK/"custom_components"/"hoymiles_hit_modbus"))
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module
E=load("supervisor_executor",WORK/"custom_components"/"hoymiles_hit_modbus"/"supervisor_executor.py")
C=load("supervisor_active_controller",WORK/"custom_components"/"hoymiles_hit_modbus"/"supervisor_active_controller.py")
f=runpy.run_path(str(WORK/"tools"/"test_supervisor_active_controller.py"),run_name="fixture")
import supervisor_executor_codec as codec
N=f["NOW"]
checks=0

def check(ok,message):
    global checks
    assert ok,message
    checks+=1

async def scenario(kind):
    case=kind
    stale_variant=kind.endswith("_stale_snapshot")
    if stale_variant:
        kind=kind.removesuffix("_stale_snapshot")
    now=N
    deadline=N+timedelta(minutes=10)
    writes=[]
    persisted=[]
    rejected_baseline=None
    rejected_id=None
    old_sleep=asyncio.sleep
    poll_count=0
    gate_times=[]
    def make(generation=10, *, target=30, power=40, **overrides):
        source=f["execution_source"](now,full_block_generation=generation,**overrides)
        return f["rce_frame"](now,source,deadline=deadline,floor=target,power=power,
                              active=False,transaction_pending=bool(writes),
                              slot_end=N+timedelta(minutes=6))
    current=make()
    async def persist(record):
        nonlocal now,current
        persisted.append(record)
        codec.record_from_dict(codec.record_to_dict(record))
        tx=record.transaction
        if (kind in {"stale_after_prepare","budget_after_prepare"}
            and tx is not None and tx.prewrite_snapshot is not None
            and tx.prewrite_snapshot.ems_generation==11):
            now=N+timedelta(seconds=17 if kind=="stale_after_prepare" else 31)
        if (kind=="bms_after_prepare" and tx is not None and tx.prewrite_snapshot is not None
            and tx.prewrite_snapshot.ems_generation==11):
            current=replace(current,execution=replace(current.execution,bms_max_discharge_current_a=10))
        if (kind in {"none_after_prepare","generation_after_prepare","stale_gcf_after_prepare","stale_bms_after_prepare"}
            and tx is not None and tx.prewrite_snapshot is not None
            and tx.prewrite_snapshot.ems_generation==11):
            if kind=="none_after_prepare": current=None
            elif kind=="generation_after_prepare": current=make(12,target=35,power=30)
            else:
                now=N+timedelta(seconds=1.2)
                field="gcf_generation_at" if kind=="stale_gcf_after_prepare" else "bms_discharge_current_observed_at"
                age=29.6 if kind=="stale_gcf_after_prepare" else 299.6
                current=replace(current,execution=replace(current.execution,**{field:N-timedelta(seconds=age)}))
    async def dispatch(write):
        nonlocal current,now,rejected_baseline,rejected_id
        writes.append(write)
        if len(writes)==1:
            rejected_baseline=controller.record.transaction.command_snapshot
            rejected_id=controller.record.transaction.transaction_id
            if kind=="unknown":
                raise TimeoutError("response outcome unknown")
            if kind=="lease_already_active":
                raise RuntimeError("ESP control lease rejected before transport: lease_already_active")
            now=N+timedelta(milliseconds=200)
            current=make(11,target=35,power=30)
            if kind=="delayed": current=None
            if kind=="never_new": current=make(10,target=30,power=40)
            if kind=="changed_baseline": current=make(11,backup_soc_percent=70)
            if kind=="off_grid": current=make(11,physical_mode_code=3)
            if kind=="stale_bms": current=make(11,bms_discharge_current_observed_at=N-timedelta(seconds=301))
            if kind=="bad_topology": current=make(11,inverter_count=0)
            if kind=="positive_bms_shortfall": current=make(11,bms_max_discharge_current_a=10)
            if kind=="zero_export": current=make(11,gcf_enable_code=1,effective_export_limit_percent=0)
            if kind=="owner_conflict": current=replace(current,context=replace(current.context,owner_conflict=True))
            if kind=="no_start_authority":
                current=f["rce_frame"](now,current.execution,deadline=deadline,floor=35,power=30,
                    active=False,transaction_pending=True,start_eligible=False)
            if kind=="permission":
                current=replace(current,decision=replace(current.decision,supervisor_mode=f["SupervisorMode"].OFF))
            raise E.AtomicWriteNotQueued(
                "other_reason" if kind=="other_rejection" else
                "stale_snapshot" if stale_variant else "stale_snapshot_generation"
            )
        if len(writes)==3 and kind=="retarget_rejected":
            now+=timedelta(milliseconds=200)
            current=f["rce_frame"](now,f["execution_source"](now,physical_mode_code=5,
                full_block_generation=14,full_block_generation_at=now-timedelta(milliseconds=50),
                force_discharge_soc_percent=35,maximum_discharge_power_percent=30,
                battery_power_w=1000,battery_power_observed_at=now-timedelta(milliseconds=50)),
                deadline=deadline,floor=36,power=25,active=True)
            raise E.AtomicWriteNotQueued(
                "stale_snapshot" if stale_variant else "stale_snapshot_generation"
            )
        if len(writes)==3 and kind=="retarget_unknown":
            raise TimeoutError("retarget transport outcome unknown")
        if kind=="twice":
            raise E.AtomicWriteNotQueued(
                "stale_snapshot" if stale_variant else "stale_snapshot_generation"
            )
        if kind=="unknown_second":
            raise TimeoutError("second transport outcome unknown")
    controller=C.SupervisorActiveController(persist=persist,dispatch=dispatch,
        publish=lambda record:None,clock=lambda:now,frame_resampler=lambda:current)
    original_gates=controller._gates
    def gates(frame,**kwargs):
        gate_times.append(frame.now)
        return original_gates(frame,**kwargs)
    controller._gates=gates
    async def sleep(seconds):
        nonlocal now,current,poll_count
        poll_count+=1
        now+=timedelta(seconds=seconds)
        if kind=="delayed":
            current=None if poll_count==1 else make(10 if poll_count==2 else 11,
                                                   target=30 if poll_count==2 else 35,
                                                   power=40 if poll_count==2 else 30)
        elif kind=="never_new": current=make(10,target=30,power=40)
        await old_sleep(0)
    asyncio.sleep=sleep
    try:
        await controller.async_initialize()
        await controller.async_reconcile(current)
    finally:
        asyncio.sleep=old_sleep
    tx=controller.record.transaction
    check(tx is not None and tx.command_snapshot==rejected_baseline,
          kind+": original command_snapshot was replaced")
    check(tx.transaction_id==rejected_id and tx.deadline==deadline and tx.started_at==N,
          kind+": original transaction identity/deadlines changed")
    check(len(writes)<=2,kind+": exceeded the one retry bound")
    if kind in {"success","delayed","retarget_rejected","retarget_unknown"}:
        check(controller.record.state is E.ActiveState.WAITING_READBACK and len(writes)==2,
              kind+": explicit rejection did not reprepare once")
        check(writes[0].snapshot_generation==10 and writes[1].snapshot_generation==11,
              kind+": second command did not consume a whole new cohort")
        check(writes[1].ems_block.force_discharge_soc_percent_4305==35
              and writes[1].ems_block.maximum_discharge_power_percent_4306==30,
              kind+": stale plan target was replayed")
        check(tx.prewrite_snapshot.ems_generation==11
              and tx.expected_readback.base_ems_generation==11,
              kind+": prewrite and expected ACK baseline were not rebuilt together")
        if kind=="delayed": check(poll_count>=3,"did not wait through actual missing/old cohort")
        sent=tx.command_sent_at
        now+=timedelta(seconds=1)
        for generation,state in ((11,E.ActiveState.WAITING_READBACK),(12,E.ActiveState.EXECUTING)):
            current=f["rce_frame"](now,f["execution_source"](now,physical_mode_code=5,
                full_block_generation=generation,full_block_generation_at=now-timedelta(milliseconds=50),
                force_discharge_soc_percent=35,maximum_discharge_power_percent=30,
                battery_power_w=1000,battery_power_observed_at=now-timedelta(milliseconds=50)),
                deadline=deadline,floor=35,power=30,active=True,transaction_pending=True)
            await controller.async_reconcile(current)
            check(controller.record.state is state,"new FC03/physical ACK contract failed")
        check(controller.record.transaction.physical_verification.observed_at>sent,
              "physical proof was reused from before actual accepted transport")
        if kind.startswith("retarget_"):
            now+=timedelta(seconds=1)
            current=f["rce_frame"](now,f["execution_source"](now,physical_mode_code=5,
                full_block_generation=13,full_block_generation_at=now-timedelta(milliseconds=50),
                force_discharge_soc_percent=35,maximum_discharge_power_percent=30,
                battery_power_w=1000,battery_power_observed_at=now-timedelta(milliseconds=50)),
                deadline=deadline,floor=36,power=25,active=True)
            await controller.async_reconcile(current)
            expected=E.ActiveState.RETARGETING if kind=="retarget_rejected" else E.ActiveState.FAULT
            expected_writes=4 if kind=="retarget_rejected" else 3
            check(controller.record.state is expected and len(writes)==expected_writes,
                  kind+": unexpected retarget rejection outcome/retry")
            check(controller.record.owner is E.ExecutionOwner.RCE
                  and controller.record.transaction.command_snapshot==rejected_baseline
                  and controller.record.transaction.transaction_id==rejected_id
                  and controller.record.transaction.deadline==deadline,
                  kind+": retarget released owner or replaced the original restore baseline")
            if kind=="retarget_rejected":
                check(controller.record.transaction.intent.command.ems_block.maximum_discharge_power_percent_4306==25
                      and controller.record.transaction.command_sent_at>sent,
                      "rejected retarget did not send the fresh successor once")
                now+=timedelta(seconds=1)
                current=f["rce_frame"](now,f["execution_source"](now,physical_mode_code=5,
                    full_block_generation=15,full_block_generation_at=now-timedelta(milliseconds=50),
                    force_discharge_soc_percent=36,maximum_discharge_power_percent=25,
                    battery_power_w=1000,battery_power_observed_at=now-timedelta(milliseconds=50)),
                    deadline=deadline,floor=36,power=25,active=True)
                await controller.async_reconcile(current)
                check(controller.record.state is E.ActiveState.EXECUTING,
                      "retried retarget did not require its own newer FC03 ACK")
    elif kind in {"unknown","unknown_second","lease_already_active"}:
        check(controller.record.state is E.ActiveState.FAULT
              and controller.record.owner is E.ExecutionOwner.RCE,
              kind+": unknown outcome was treated as not queued/released")
        check(len(writes)==(2 if kind=="unknown_second" else 1),
              "unknown or active-lease outcome got an extra retry")
    elif kind=="twice":
        check(controller.record.state is E.ActiveState.BLOCKED
              and controller.record.reason is E.ExecutionReason.COMMAND_NOT_QUEUED
              and controller.record.owner is E.ExecutionOwner.NONE,
              "second explicit reject did not block as command_not_queued")
        check(codec.record_from_dict(codec.record_to_dict(controller.record)).reason
              is E.ExecutionReason.COMMAND_NOT_QUEUED,"new reason failed codec roundtrip")
        for generation in (12,13,14,15):
            now+=timedelta(seconds=1)
            current=f["rce_frame"](now,f["execution_source"](now,full_block_generation=generation),
                deadline=deadline,floor=35,power=30,active=False,transaction_pending=False,
                slot_end=N+timedelta(minutes=6))
            await controller.async_reconcile(current)
            check(controller.record.state is E.ActiveState.BLOCKED and len(writes)==2,
                  "fresh FC03 silently restarted the exhausted rejection series")
        # Counterfactual using the existing retryable reason proves this guard
        # is reason-dependent, not an accidental owner conflict in the fixture.
        blocked=controller.record
        controller._executor._record=replace(blocked,reason=E.ExecutionReason.SNAPSHOT_CHANGED,
            transaction=replace(blocked.transaction,reason=E.ExecutionReason.SNAPSHOT_CHANGED))
        now+=timedelta(seconds=1)
        current=f["rce_frame"](now,f["execution_source"](now,full_block_generation=16),
            deadline=deadline,floor=35,power=30,active=False,transaction_pending=False,
            slot_end=N+timedelta(minutes=6))
        await controller.async_reconcile(current)
        check(controller.record.state is E.ActiveState.IDLE,
              "counterfactual old retryable reason did not expose the fresh-generation reset")
        controller._executor._record=blocked
        current=f["rce_frame"](now,f["execution_source"](now,full_block_generation=16),
            deadline=deadline,floor=36,power=30,active=False,transaction_pending=False,
            slot_end=N+timedelta(minutes=6))
        await controller.async_reconcile(current)
        check(controller.record.state is E.ActiveState.IDLE and len(writes)==2,
              "a genuinely changed desired candidate lost existing rearm behavior")
    else:
        check(controller.record.state is E.ActiveState.BLOCKED and len(writes)==1,
              kind+": invalid current authorization/baseline caused another write")
        check(tx.command_sent_at is None,kind+": a proven unsent command was marked sent")
        if kind=="never_new":
            check((now-N).total_seconds()<=30.1 and poll_count>0,
                  "new cohort wait renewed the original 30-second dispatch budget")
        if kind=="stale_gcf_after_prepare":
            check(gate_times[-1]==now, "second prewrite gates reused cached frame.now")
        if kind in {"none_after_prepare","generation_after_prepare","stale_gcf_after_prepare","stale_bms_after_prepare"}:
            check(controller.record.reason is E.ExecutionReason.COMMAND_NOT_QUEUED,
                  "local second-attempt rejection allowed automatic authorization retry")
            for generation in (13,14,15):
                now+=timedelta(seconds=1)
                current=f["rce_frame"](now,f["execution_source"](now,full_block_generation=generation),
                    deadline=deadline,floor=35,power=30,active=False,transaction_pending=False,
                    slot_end=N+timedelta(minutes=6))
                await controller.async_reconcile(current)
                check(controller.record.state is E.ActiveState.BLOCKED and len(writes)==1,
                      "local second-attempt rejection restarted from new FC03 alone")
    print("PASS",case)

async def main():
    for kind in ("success","delayed","twice","unknown","unknown_second",
                 "lease_already_active","other_rejection",
                 "changed_baseline","off_grid","stale_bms","bad_topology","permission","never_new",
                 "retarget_rejected","retarget_unknown","stale_after_prepare","budget_after_prepare",
                 "positive_bms_shortfall","zero_export","owner_conflict","no_start_authority",
                 "bms_after_prepare","none_after_prepare","generation_after_prepare",
                 "stale_gcf_after_prepare","stale_bms_after_prepare",
                 "success_stale_snapshot","twice_stale_snapshot",
                 "never_new_stale_snapshot","retarget_rejected_stale_snapshot"):
        await scenario(kind)
    print(f"PASS {checks} checks; real runtime/controller/executor, stubbed adapter; offline only")

if __name__=="__main__": asyncio.run(main())
