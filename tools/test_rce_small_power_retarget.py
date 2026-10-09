"""Offline actual RCE builder/controller retarget regression; no network writes."""
import asyncio,importlib.util,runpy,sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
R=Path(__file__).resolve().parent
W=R.parent
CMP=W/"custom_components"/"hoymiles_hit_modbus"
sys.path.insert(0,str(W/"custom_components"/"hoymiles_hit_modbus"))
from rce_optimizer import _quantize_4306_percent as whole_power
def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec)
    sys.modules[name]=m;spec.loader.exec_module(m);return m
E=load("supervisor_executor",CMP/"supervisor_executor.py")
RT=load("supervisor_runtime",CMP/"supervisor_runtime.py")
B=load("supervisor_active_bridge",CMP/"supervisor_active_bridge.py")
C=load("supervisor_active_controller",CMP/"supervisor_active_controller.py")
f=runpy.run_path(str(W/"tools"/"test_supervisor_active_controller.py"),run_name="fixture")
N=f["NOW"];checks=0

def check(ok,message):
    global checks
    assert ok,message
    checks+=1

async def scenario(kind):
    now=N;deadline=N+timedelta(hours=2);writes=[];records=[];generation=10
    physical=50.0;goal=50.0;started=False;superseded=False
    def frame(**changes):
        source=f["execution_source"](now,physical_mode_code=5 if started else 0,
            full_block_generation=generation,full_block_generation_at=now-timedelta(milliseconds=100),
            force_discharge_soc_percent=55 if started else 25,maximum_discharge_power_percent=physical if started else 40,
            battery_soc_percent=70,bms_max_discharge_current_a=390,battery_power_w=1000,
            battery_power_observed_at=now-timedelta(milliseconds=100))
        source=replace(source,**changes)
        return f["rce_frame"](now,source,deadline=deadline,floor=55,power=whole_power(goal) if kind=="same_integer" else goal,active=started,
            start_eligible=not(kind=="last_five" and started),slot_end=N+timedelta(seconds=301 if kind=="last_five" else 600))
    current=frame()
    async def persist(record):
        nonlocal current,goal,superseded
        records.append(record)
        if (kind=="latest_only" and not superseded and record.state is E.ActiveState.RETARGETING
            and record.transaction.command_sent_at is None):
            superseded=True;goal=48.0;current=frame()
    async def dispatch(write): writes.append(write)
    ctl=C.SupervisorActiveController(persist=persist,dispatch=dispatch,publish=lambda rec:None,
        clock=lambda:now,frame_resampler=lambda:current)
    await ctl.async_initialize();await ctl.async_reconcile(current)
    check(ctl.record.state is E.ActiveState.WAITING_READBACK,"initial not waiting")
    original=ctl.record.transaction
    started=True;now+=timedelta(seconds=1);generation+=1;current=frame()
    await ctl.async_reconcile(current)
    check(ctl.record.state is E.ActiveState.EXECUTING,"initial A not confirmed")
    if kind == "same_integer":
        targets=(49.1,49.9)
    elif kind in {"sequence","latest_only"}:
        targets=(40.0,50.0,49.0) if kind=="sequence" else (49.0,)
    else: targets=(50.00000001 if kind=="same_encoded" else 50.0 if kind=="last_five" else 49.0,)
    for target in targets:
        now+=timedelta(seconds=1);generation+=1;goal=target;current=frame()
        if kind=="off_grid": current=frame(physical_mode_code=3)
        if kind=="zero_export": current=frame(gcf_enable_code=1,effective_export_limit_percent=0)
        if kind=="owner_conflict": current=replace(current,context=replace(current.context,owner_conflict=True))
        if kind=="permission": current=replace(current,decision=replace(current.decision,supervisor_mode=f["SupervisorMode"].OFF))
        await ctl.async_reconcile(current)
        if kind == "same_integer" and target == 49.9:
            check(ctl.record.state is E.ActiveState.EXECUTING and len(writes)==2,
                  "same whole-percent command caused another FC16 or STOP")
            check(writes[-1].ems_block.maximum_discharge_power_percent_4306==49.0,
                  "fractional planner value escaped whole-percent command")
            continue
        if kind in {"same_encoded","last_five"}:
            check(ctl.record.state is E.ActiveState.EXECUTING and len(writes)==1,kind+": unnecessary write/STOP")
            continue
        if kind in {"off_grid","zero_export","owner_conflict","permission"}:
            check(all(w.ems_block.maximum_discharge_power_percent_4306!=49.0 for w in writes),kind+": unsafe new target sent")
            continue
        if kind=="latest_only":
            check(ctl.record.state is E.ActiveState.EXECUTING and len(writes)==1,"superseded B was sent or A lost")
            await ctl.async_reconcile(current)
            check(writes[-1].ems_block.maximum_discharge_power_percent_4306==48.0,"latest C did not win")
        check(ctl.record.state is E.ActiveState.RETARGETING,"small target change did not retarget directly")
        check(all(w.ems_block.mode is E.EmsMode.GRID_DISCHARGE for w in writes),"retarget passed through Mode0")
        check(ctl.record.transaction.transaction_id==original.transaction_id
              and ctl.record.transaction.deadline==original.deadline
              and ctl.record.transaction.command_snapshot==original.command_snapshot,"retarget changed original lease/baseline")
        sent=ctl.record.transaction.command_sent_at
        physical=whole_power(goal) if kind=="same_integer" else goal
        now+=timedelta(seconds=1)
        # A matching echo in the already consumed generation cannot acknowledge B.
        current=frame();await ctl.async_reconcile(current)
        check(ctl.record.state is E.ActiveState.RETARGETING,"old FC03 generation acknowledged successor")
        generation+=1
        # Real float32 readback noise must remain inside physical ACK tolerance.
        current=frame(maximum_discharge_power_percent=physical+0.0000015)
        await ctl.async_reconcile(current)
        check(ctl.record.state is E.ActiveState.EXECUTING,"own newer FC03+float32 physical report was rejected")
        check(ctl.record.transaction.physical_verification.observed_at>sent,"physical evidence predates successor")
    print("PASS",kind)

async def main():
    for kind in ("sequence","same_encoded","same_integer","latest_only","last_five","off_grid","zero_export","owner_conflict","permission"):
        await scenario(kind)
    raw=f["rce_frame"](N,f["execution_source"](N),deadline=N+timedelta(minutes=10),floor=55,power=50,active=False)
    for value in (49.1,49.9,49.91,float("nan"),float("inf"),-1,101):
        cand=RT.build_rce_candidate(replace(raw.rce,effective_discharge_power_percent=value),now=N)
        check(not cand.start_eligible,"invalid/unsupported precision gained start authority")
    print(
        f"PASS {checks} checks; integrated production runtime/bridge/"
        "controller/executor; offline only"
    )

if __name__=="__main__":asyncio.run(main())
