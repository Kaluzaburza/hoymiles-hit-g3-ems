"""Extra 90 s LOAD settling with real controller and an ESP lease oracle."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import test_rce_post_command_settling as f
from test_supervisor_control_lease import FirmwareLeaseModel
from supervisor_active_bridge import RCE_POST_COMMAND_SETTLING_SECONDS, RCE_POST_COMMAND_REPLAN_SECONDS


async def run():
    c, clock, writes = await f.running()
    tx = c.record.transaction
    fw = FirmwareLeaseModel()
    block = (5., 25., 90., 98., 5., 49., 50.)
    assert fw.arm(block, transaction=tx.transaction_id, hard_seconds=1800)
    fw.observe(block)
    fw.advance(2)
    sequence = 0
    for second in range(20, 176, 20):
        at = f.t.NOW + timedelta(seconds=second)
        bad_load = 25 <= second < 150
        frame = f.load_block(at) if bad_load else f.frame(at, f.source(at))
        await f.reconcile(c, clock, frame)
        assert c.record.state is f.t.ActiveState.EXECUTING, (second, c.record.reason)
        assert len(writes) == 1, (second, 'unwanted restore or rewrite')
        fw.advance(second-fw.now)
        assert fw.state == 'confirmed', (second, 'ESP expired despite controller hold')
        if bad_load:
            authorized = c.rce_replan_lease_authorized(frame, now=at, lease_ttl_seconds=1)
            assert authorized == (second < 179), (second, 'lease must end within fixed180')
        else:
            authorized = True  # ordinary fresh, current-plan renewal branch
        if authorized:
            sequence += 1
            assert fw.renew(authorized=True, sequence=sequence, authorization_seconds=int((c.lease_policy_deadline(now=at)-f.t.NOW).total_seconds()-fw.nonce_issued))
    assert RCE_POST_COMMAND_SETTLING_SECONDS == 180
    assert RCE_POST_COMMAND_REPLAN_SECONDS == 150
    assert c.record.transaction.deadline == tx.deadline
    assert c.record.transaction.command_sent_at == tx.command_sent_at
    assert c.record.transaction.command_snapshot == tx.command_snapshot

    # A new settled frame cannot launder changed consent/market/physical data.
    c, clock, writes = await f.running()
    at = f.t.NOW + timedelta(seconds=100)
    good = f.load_block(at)
    await f.reconcile(c, clock, good)
    for name, bad in (
        ('consent', f.rebuild(good, rce=replace(good.rce, allowed_by_user=False))),
        ('price', f.rebuild(good, rce=replace(good.rce, price_above_threshold=False))),
        ('market', f.rebuild(good, rce=replace(good.rce, post_command_settling_market_fingerprint='c'*64))),
        ('BMS', replace(good, execution=replace(good.execution, bms_max_discharge_current_a=0))),
        ('opposite BAT', replace(good, execution=replace(good.execution, battery_power_w=-800))),
        ('stale BAT', replace(good, execution=replace(good.execution, battery_power_observed_at=at-timedelta(seconds=16)))),
        ('stale LOAD', replace(good, execution=replace(good.execution, load_power_observed_at=at-timedelta(seconds=16)))),
    ):
        assert not c.rce_replan_lease_authorized(bad, now=at, lease_ttl_seconds=1), name
    for seconds in (0, -1, float('nan'), float('inf'), True):
        assert not c.rce_replan_lease_authorized(good, now=at, lease_ttl_seconds=seconds), seconds
    assert not c.rce_replan_lease_authorized(good, now=f.t.NOW+timedelta(seconds=179), lease_ttl_seconds=1)
    await f.reconcile(c, clock, f.load_block(f.t.NOW+timedelta(seconds=180)))
    assert c.record.state is f.t.ActiveState.RESTORING
    assert len(writes) == 2 and writes[-1].ems_block.mode == 0
    print('PASS: 180s bounded RCE settling, 150s replan, actual bounded 120s ESP lease / 20s renew, fresh vetoes, no timer sliding')


if __name__ == '__main__':
    asyncio.run(run())
