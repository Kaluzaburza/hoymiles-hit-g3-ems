"""Protocol timing regressions, including an independently delayed wire clock."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'custom_components'/'hoymiles_hit_modbus'))
import supervisor_control_lease as lease

NOW = datetime(2026, 10, 2, tzinfo=timezone.utc)
BLOCK = (5.,20.,90.,100.,100.,50.,80.)

def response(reason, remaining=120000, nonce='00000002', version=2):
    return dict(schema_version=1, protocol_version=version, accepted=True,
                reason=reason, renew_nonce=nonce, soft_remaining_ms=remaining)

def arm(latency=0.):
    c=lease.ControlLeaseClient(session_id='test')
    r=c.prepare_arm(transaction_id='rce:timing-test', hard_deadline=NOW+timedelta(hours=1),
        block=BLOCK, challenge_nonce='00000001', now_wall=NOW, now_monotonic=100.)
    assert c.accept_arm(r,response('accepted'),now_monotonic=100.+latency)
    return c

def test_requested_contract():
    assert lease.CONTROL_LEASE_PROTOCOL_VERSION == 2
    assert lease.CONTROL_LEASE_TTL_SECONDS == 120
    assert lease.CONTROL_LEASE_RENEW_SECONDS == 20

def test_no_renewal_before_twenty_seconds():
    c=arm()
    assert c.prepare_renew(authorized=True,snapshot_generation=1,now_wall=NOW+timedelta(seconds=19),now_monotonic=119.) is None
    assert c.prepare_renew(authorized=True,snapshot_generation=1,now_wall=NOW+timedelta(seconds=20),now_monotonic=120.)

def test_response_latency_never_extends_right():
    for lag in (0.,1.,5.,20.,40.,60.,90.,119.):
        c=arm(lag)
        assert not c.expire_if_due(now_monotonic=219.999)
        assert c.expire_if_due(now_monotonic=220.)

def test_short_grant_and_late_ack():
    c=arm()
    r=c.prepare_renew(authorized=True,snapshot_generation=7,now_wall=NOW+timedelta(seconds=20),now_monotonic=120.,authorization_deadline=NOW+timedelta(seconds=50))
    assert r['authorization_seconds'] == 50
    assert c.accept_renew(response('renewed',remaining=29000),request=r,now_monotonic=122.)
    assert not c.expire_if_due(now_monotonic=148.9)
    assert c.expire_if_due(now_monotonic=149.)
    c=arm()
    r=c.prepare_renew(authorized=True,snapshot_generation=7,now_wall=NOW+timedelta(seconds=20),now_monotonic=120.)
    assert not c.accept_renew(response('renewed',remaining=29000),request=r,now_monotonic=149.)
    assert c.handle is None

def test_old_firmware_is_rejected():
    c=lease.ControlLeaseClient(session_id='test')
    r=c.prepare_arm(transaction_id='rce:timing-test',hard_deadline=NOW+timedelta(hours=1),block=BLOCK,challenge_nonce='00000001',now_wall=NOW,now_monotonic=100.)
    try:
        c.accept_arm(r,response('accepted',version=1),now_monotonic=100.)
    except ValueError:
        assert c.handle is None
    else:
        raise AssertionError('protocol v1 must not be mistaken for the longer lease')

def test_nonce_anchor_caps_delayed_request_to_policy_end():
    # ESP issued its nonce at wall t=0; HA only received it at t=7.
    # At t=27, a remaining policy interval of 45 s ends at t=72.
    c=arm(7.)
    r=c.prepare_renew(authorized=True,snapshot_generation=1,now_wall=NOW+timedelta(seconds=27),now_monotonic=127.,authorization_deadline=NOW+timedelta(seconds=72))
    assert r['authorization_seconds']==65
    # ESP expiry is at nonce-issued+65, regardless of outgoing delay.
    for outgoing in (0,5,20,35):
        arrival=27+outgoing
        expiry=min(arrival+120,r['authorization_seconds'],3600)
        assert expiry<=72

def test_retry_cannot_resend_withdrawn_horizon():
    c=arm()
    original=c.prepare_renew(authorized=True,snapshot_generation=1,
        now_wall=NOW+timedelta(seconds=20),now_monotonic=120.,
        authorization_deadline=NOW+timedelta(seconds=180))
    retry=c.prepare_renew(authorized=True,snapshot_generation=2,
        now_wall=NOW+timedelta(seconds=40.001),now_monotonic=140.,
        authorization_deadline=NOW+timedelta(seconds=180))
    assert retry['sequence']==original['sequence']
    assert retry['authorization_seconds']==original['authorization_seconds']
    assert c.prepare_renew(authorized=True,snapshot_generation=3,
        now_wall=NOW+timedelta(seconds=60),now_monotonic=160.,
        authorization_deadline=NOW+timedelta(seconds=100)) is None
    assert c.handle is None

def test_invalid_duration_cannot_keep_old_right():
    for remaining in (0,-1,120001,None,True,1.5):
        c=arm()
        r=c.prepare_renew(authorized=True,snapshot_generation=1,
            now_wall=NOW+timedelta(seconds=20),now_monotonic=120.)
        try: c.accept_renew(response('renewed',remaining=remaining),request=r,now_monotonic=121.)
        except ValueError: pass
        else: raise AssertionError(remaining)
        assert c.handle is None

if __name__=='__main__':
    tests=[v for k,v in tuple(globals().items()) if k.startswith('test_')]
    for test in tests: test()
    print(f'PASS {len(tests)} shared lease timing regressions')
