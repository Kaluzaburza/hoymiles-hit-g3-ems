"""Accepted response evidence is bounded, correlated and never request evidence."""
from datetime import timedelta
import json
from test_supervisor_control_lease import LEASE, NOW, BLOCK


def ack(reason='renewed', accepted=True):
    return dict(schema_version=1, protocol_version=2, accepted=accepted,
                reason=reason, renew_nonce='1234abcd', soft_remaining_ms=120000)


def arm(client, mono=0):
    request = client.prepare_arm(transaction_id='pvhold:test', hard_deadline=NOW+timedelta(hours=24),
        block=BLOCK, challenge_nonce='1234abcd', now_wall=NOW+timedelta(seconds=mono), now_monotonic=mono)
    assert client.accept_arm(request, ack('accepted'), now_monotonic=mono+0.25)
    return request


def events(client):
    return [event for page in client.journal_snapshot()['pages'] for event in page]


def renew(client, mono, generation=7):
    return client.prepare_renew(authorized=True, snapshot_generation=generation,
        now_wall=NOW+timedelta(seconds=mono), now_monotonic=mono)


def main():
    client = LEASE.ControlLeaseClient(session_id='private-session')
    assert events(client) == []
    request = arm(client)
    assert len(events(client)) == 1
    first = events(client)[0]
    assert first['kind'] == 'arm' and first['sequence'] == 0
    assert first['hard_deadline'] == (NOW+timedelta(hours=24)).isoformat()
    request = renew(client, 20)
    assert len(events(client)) == 1, 'request must not create accepted evidence'
    result = client.process_renew_response(ack('snapshot_generation_advanced', False), request=request, now_monotonic=20.1)
    assert result.status == LEASE.ControlLeaseRenewStatus.RETRYABLE_STALE_SNAPSHOT
    assert len(events(client)) == 1
    assert client.accept_renew(ack(), request=request, now_monotonic=20.5)
    second = events(client)[1]
    assert second['sequence'] == 1 and second['snapshot_generation'] == 7
    assert second['first_send_monotonic'] == 20 and second['response_received_monotonic'] == 20.5
    assert second['accepted_at'] == (NOW+timedelta(seconds=20.5)).isoformat()
    assert second['gap_before'] is None and second['request_correlated'] is True
    assert client.handle.next_renew_monotonic == 40 and client.handle.last_accepted_monotonic == 20
    assert not client.accept_renew(ack(), request=request, now_monotonic=21)
    assert len(events(client)) == 2, 'duplicate/stale response must not add evidence'
    request = renew(client, 40)
    assert not client.accept_renew(ack('authorization_lost', False), request=request, now_monotonic=40.5)
    assert client.handle is None and len(events(client)) == 2
    saved = client.journal_snapshot()
    saved['pages'][0][0]['sequence'] = 999
    assert events(client)[0]['sequence'] == 0, 'export must not mutate journal'
    raw = json.dumps(client.journal_snapshot())
    assert 'private-session' not in raw and '1234abcd' not in raw and 'renew_nonce' not in raw
    assert client.journal_snapshot()['active_lease'] is False
    restarted = LEASE.ControlLeaseClient(session_id='private-session')
    assert restarted.journal_snapshot()['journal_epoch'] != client.journal_snapshot()['journal_epoch']
    assert events(restarted) == [], 'new process must not invent old evidence'
    arm(restarted)
    # Retarget creates a distinct command/lease; it never extends the deadline.
    old = events(restarted)[0]
    arm(restarted, 1)
    assert events(restarted)[1]['command_generation'] == 2
    assert events(restarted)[1]['lease_ref'] != old['lease_ref']
    assert events(restarted)[1]['hard_deadline'] == old['hard_deadline']
    # Exercise the actual bound and page boundaries, without touching control cadence.
    bounded = LEASE.ControlLeaseClient()
    arm(bounded)
    for index in range(1, LEASE.CONTROL_LEASE_JOURNAL_MAX_EVENTS + 4):
        bounded.invalidate()
        arm(bounded)
    snapshot = bounded.journal_snapshot()
    assert snapshot['dropped_events'] == 4 and not snapshot['retention_complete']
    assert snapshot['retained_events'] == LEASE.CONTROL_LEASE_JOURNAL_MAX_EVENTS
    assert all(len(page) <= 256 for page in snapshot['pages'])
    assert events(bounded)[0]['ordinal'] == 5
    gap = LEASE.ControlLeaseClient()
    arm(gap)
    gap.handle.sequence = 9  # Simulated missing journal segment, never a live reset.
    request = renew(gap, 20)
    assert gap.accept_renew(ack(), request=request, now_monotonic=20.5)
    assert events(gap)[1]['gap_before'] == 'sequence_gap'
    late = LEASE.ControlLeaseClient()
    arm(late)
    request = renew(late, 20)
    assert not late.accept_renew(ack(), request=request, now_monotonic=141)
    assert len(events(late)) == 1
    print('Lease acceptance journal: PASS (requests, rejection, stale/duplicate, clocks, privacy, retarget, gaps, overflow, restart)')


if __name__ == '__main__':
    main()
