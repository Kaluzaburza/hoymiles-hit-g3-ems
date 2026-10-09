"""Production Pstryk LOAD qualification consumed by the real RCE controller."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import test_rce_post_command_settling as f
from pstryk_joint import EnergySlot, JointInput, optimize
from pstryk_settling import load_only_suppression, market_basis

async def main():
    slots=tuple(EnergySlot(f.t.NOW+timedelta(minutes=30*i),f.t.NOW+timedelta(minutes=30*(i+1)),
        2. if i==0 else .1,0.,0. if i==0 else 8.) for i in range(4))
    original=JointInput(slots,16.,11.84,7.84,14.4,7.,8.,8.,16.,16.)
    previous=optimize(original)
    assert previous.slots[0].action=='sell'
    bad=replace(original,slots=(replace(slots[0],load_kwh=4.),*slots[1:]))
    qualified=load_only_suppression(bad,optimize(bad),original,previous)
    assert qualified
    basis=market_basis(original,4,'public-net-v1')
    assert market_basis(bad,4,'public-net-v1')==basis
    c,clock,writes=await f.running(market=basis)
    for second in (25,60,100,150,175):
        at=f.t.NOW+timedelta(seconds=second)
        frame=f.load_block(at,post_command_settling_market_fingerprint=basis,
            current_slot_load_exhausts_requested_discharge_budget=qualified)
        await f.reconcile(c,clock,frame)
        assert c.record.state is f.t.ActiveState.EXECUTING
        assert c.rce_replan_lease_authorized(frame,now=at,lease_ttl_seconds=1)==(second<179)
        assert len(writes)==1
    assert c.post_command_settling_deadline==f.t.NOW+timedelta(seconds=180)
    at=f.t.NOW+timedelta(seconds=176)
    await f.reconcile(c,clock,f.load_block(at,post_command_settling_market_fingerprint=
        market_basis(bad,4,'changed-price')))
    assert c.record.state is f.t.ActiveState.RESTORING
    assert len(writes)==2
    print('PASS Pstryk LOAD-only evidence -> 180s controller hold -> price-change STOP')

if __name__=='__main__': asyncio.run(main())
