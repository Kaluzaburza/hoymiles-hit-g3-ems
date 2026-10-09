"""Regress the 2026-09-27 natural installation_2 plan-end/pending sequence.

The real optimizer, helper templates, Supervisor and lease model are reused.
Only the raw planner run end is supplied as the recorded field sequence.
This is an offline virtual-clock replay, not inverter acceptance.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from test_rce_publication_continuity import Continuity, NOW, h


async def pending_after_shorter_run(negative=None):
    probe = Continuity()
    await probe.setup()
    try:
        shorter = (NOW + timedelta(seconds=18) if negative == 'fresh_planned_end'
                   else NOW + timedelta(seconds=22) if negative == 'planned_end'
                   else probe.original_deadline - timedelta(minutes=15))
        for second in range(1, 31):
            if second == 2:
                # Supervisor Active owns the latch; the legacy YAML helper
                # remained off in the recorded installation_2 sequence.
                probe.hass.fire_state(probe.eid('rce_active'),
                    h.FakeState('off', reported=h.CLOCK['now']))
            if second == 10:
                probe.source._attributes = {
                    **probe.source._attributes,
                    'current_run_end': shorter.isoformat(),
                }
                probe.source.async_write_ha_state()
            if second == 20 and negative in ('bms', 'permission', 'master_stop'):
                if negative == 'bms':
                    probe.report('bms_max_discharge_current', 0)
                    probe.direct('sensor.hoymiles_hit_maximum_discharge_current', 0)
                elif negative == 'permission':
                    probe.hass.fire_state(probe.eid('allow_rce'),
                        h.FakeState('off', reported=h.CLOCK['now']))
                else:
                    probe.supervisor.request_master_stop()
            try:
                await probe.tick(second)
            except AssertionError:
                record = probe.controller.record
                expected_stop = (18 if negative == 'fresh_planned_end'
                                 else 22 if negative == 'planned_end' else 20)
                if negative and second == expected_stop:
                    assert record.state in (h.SENSOR.ActiveState.STOPPING,
                                            h.SENSOR.ActiveState.RESTORING)
                    assert record.transaction.transaction_id == probe.original_tx
                    if negative == 'master_stop':
                        assert record.transaction.deadline <= probe.original_deadline
                    else:
                        assert record.transaction.deadline == probe.original_deadline
                    assert probe.supervisor._control_lease_renewal_evidence() is None
                    print(f'PASS shortened pending run still stops for {negative} at {second}s')
                    return
                raise AssertionError(
                    f'Field pending sequence interrupted at {second}s: '
                    f'{record.state.value}/{record.reason.value}; '
                    f'raw_end={shorter.isoformat()}, '
                    f'hard_deadline={probe.original_deadline.isoformat()}'
                ) from None
        assert negative is None, f'missing required STOP: {negative}'
        assert probe.controller.record.transaction.transaction_id == probe.original_tx
        assert not probe.services.restore_calls
        print('PASS shortened live plan + pending retains the confirmed RCE command')
    finally:
        probe.loop.time = probe.real_loop_time
        for task in probe.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*probe.tasks, return_exceptions=True)


async def main():
    for case in ('fresh_planned_end', None, 'planned_end', 'bms', 'permission', 'master_stop'):
        await pending_after_shorter_run(case)


if __name__ == '__main__':
    asyncio.run(main())
