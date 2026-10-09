"""Real controller continuity under bounded missing reports; no device I/O."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import test_pv_delay_continuity as pv
import test_tariff_active_controller as tariff

p = pv.p
f = p.f


async def setup(kind):
    if kind == 'pv':
        c, clock, writes = await pv.ReplanAudit().confirmed()
        make = lambda at, src: p.frame(at, src, active=True)
        fresh = lambda at: p.source(at, mode=5, generation=40)
    elif kind == 'tariff':
        c, writes, _, clock = await tariff.confirmed_controller()
        make = lambda at, src: tariff.frame(at, src, c.record)
        fresh = lambda at: tariff.physical(at, generation=40)
    else:
        clock = [p.NOW]; writes = []
        async def persist(_): pass
        async def dispatch(write): writes.append(write)
        c = f.SupervisorActiveController(persist=persist, dispatch=dispatch,
            publish=lambda _: None, clock=lambda: clock[0])
        await c.async_initialize()
        def make(at, src, active=True):
            return f.rce_frame(at, src, deadline=p.END, floor=30, power=10,
                active=active, slot_end=p.END)
        await c.async_reconcile(make(p.NOW, f.execution_source(p.NOW), False))
        def fresh(at):
            stamp = at-timedelta(milliseconds=100)
            return f.execution_source(at, physical_mode_code=5,
                force_discharge_soc_percent=30, maximum_discharge_power_percent=10,
                full_block_generation=40, full_block_generation_at=stamp,
                battery_power_w=1000, battery_power_observed_at=stamp,
                grid_power_w=500, grid_power_observed_at=stamp,
                pv_power_w=0, pv_power_observed_at=stamp,
                load_power_w=500, load_power_observed_at=stamp)
    clock[0] = at = p.NOW+timedelta(seconds=20)
    original_frame = make(at, fresh(at))
    await c.async_reconcile(original_frame)
    assert c.record.state is p.ActiveState.EXECUTING, (kind,c.record.reason)
    lease_end = at+timedelta(seconds=115)
    c._control_lease_valid_until = lambda: lease_end
    return c, clock, writes, make, fresh, original_frame


async def continuity(kind, missing):
    c, clock, writes, make, fresh, original = await setup(kind)
    tx = c.record.transaction
    writes_before = len(writes)
    proof = tx.physical_verification
    for gap in (20, 40, 60, 90, 110):
        clock[0] = at = original.now+timedelta(seconds=gap)
        source = original.execution
        if missing:
            source = replace(source, full_block_generation=None,
                full_block_generation_at=None, battery_power_w=None,
                battery_power_observed_at=None, grid_power_w=None, grid_power_observed_at=None)
        await c.async_reconcile(make(at, source))
        assert c.record.state is p.ActiveState.EXECUTING, (kind, gap, c.record.reason)
        assert c.record.transaction.transaction_id == tx.transaction_id
        assert c.record.transaction.physical_verification == proof, 'a report gap is not fresh proof'
        assert len(writes) == writes_before, 'report gaps cannot trigger another command'
        assert c.execution_watchdog[1] <= original.now+timedelta(seconds=115)
    clock[0] = at = original.now+timedelta(seconds=111)
    await c.async_reconcile(make(at, fresh(at)))
    assert c.record.state is p.ActiveState.EXECUTING
    assert c._telemetry_hold_until is None
    assert len(writes) == writes_before
    print('PASS confirmed telemetry recovery',kind,missing)


async def veto(kind, name):
    c, clock, writes, make, fresh, original = await setup(kind)
    clock[0] = at = original.now+timedelta(seconds=40)
    frame = make(at, original.execution)
    if name == 'expiry': clock[0] = at = original.now+timedelta(seconds=115); frame=make(at,original.execution)
    if name == 'owner': frame=replace(frame,context=replace(frame.context,owner_conflict=True))
    if name == 'off': frame=replace(frame,decision=replace(frame.decision,supervisor_mode=f.SupervisorMode.OFF))
    if name == 'off_grid': frame=replace(frame,execution=replace(frame.execution,physical_mode_code=3))
    if name == 'zero_export': frame=replace(frame,context=replace(frame.context,export_state=f.ExportState.CONFIRMED_ZERO_EXPORT))
    if name == 'bms': frame=replace(frame,execution=replace(frame.execution,**{'bms_max_charge_current_a' if kind=='tariff' else 'bms_max_discharge_current_a':0}))
    if name == 'opposite_flow': frame=replace(frame,execution=replace(frame.execution,critical_grid_power_w=1000 if kind=='tariff' else -1000,critical_grid_power_observed_at=at))
    if name == 'permission':
        key='tariff' if kind=='tariff' else 'rce'
        frame=replace(frame,**{key:replace(getattr(frame,key),allowed_by_user=False)})
    if name == 'unqualified': frame=replace(frame,rce=replace(frame.rce,pv_charge_hold_qualified=False))
    if name == 'shortened_run':
        key='tariff' if kind=='tariff' else 'rce'
        field='current_grid_charge_run_end' if kind=='tariff' else 'current_run_end'
        frame=replace(frame,**{key:replace(getattr(frame,key),**{field:at})})
    assert not c._confirmed_telemetry_wait(frame,now=at), (kind,name)
    # Real reconciliation must stop or enter recovery; never create a new right.
    await c.async_reconcile(frame)
    assert c._telemetry_hold_until is None
    assert c.record.state is not p.ActiveState.EXECUTING, (kind,name,c.record.reason)
    print('PASS immediate telemetry veto',kind,name)


async def pv_power_reports_during_readback_gap():
    c, clock, writes, make, _, original = await setup('pv')
    tx = c.record.transaction
    written = len(writes)
    clock[0] = at = original.now+timedelta(seconds=40)
    # Existing lease bounds the missing FC03 report; asynchronous power
    # changes cannot revoke Mode 5 or count as a new physical confirmation.
    frame = make(at, replace(original.execution,
        critical_grid_power_w=-1000, critical_grid_power_observed_at=at,
        pv_power_w=0, pv_power_observed_at=at,
        battery_power_w=2500, bms_battery_power_w=-1500))
    assert c._confirmed_telemetry_wait(frame,now=at)
    await c.async_reconcile(frame)
    assert c.record.state is p.ActiveState.EXECUTING
    assert c.record.transaction.transaction_id == tx.transaction_id
    assert c.record.transaction.physical_verification == tx.physical_verification
    assert c.execution_watchdog[1] <= original.now+timedelta(seconds=115)
    assert len(writes) == written
    print('PASS PV diagnostic powers do not revoke bounded readback gap')


async def main():
    for kind in ('rce','tariff','pv'):
        for missing in (False,True): await continuity(kind,missing)
        for name in ('expiry','owner','off','off_grid','bms','permission','shortened_run'):
            await veto(kind,name)
        if kind!='pv': await veto(kind,'opposite_flow')
        if kind!='tariff': await veto(kind,'zero_export')
        if kind=='pv':
            await pv_power_reports_during_readback_gap(); await veto(kind,'unqualified')


if __name__=='__main__': asyncio.run(main())
