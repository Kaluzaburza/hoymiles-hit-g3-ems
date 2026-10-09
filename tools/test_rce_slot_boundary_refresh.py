"""Production boundary callback refreshes independently of periodic phase."""
import asyncio
from datetime import timedelta
from test_rce_lease_real_cadence import solver_probe, h, NOW


async def main():
    h.CLOCK['now'] = NOW
    hass, entry, runtime, supervisor, source, module, renderer, solves = await solver_probe('natural')
    scheduled = []
    module.async_call_later = lambda hass, delay, callback: scheduled.append(
        (delay, callback)) or (lambda: scheduled.append('cancelled'))
    h.CLOCK['now'] = NOW + timedelta(minutes=29, seconds=48)
    source._schedule_slot_boundary()
    delay, callback = scheduled[-1]
    assert delay == 12
    h.CLOCK['now'] = NOW + timedelta(minutes=30)
    await callback(h.CLOCK['now'])
    assert source._attributes['result_current'] is True
    assert source._result.current_slot_end == h.CLOCK['now'] + timedelta(minutes=30)
    assert source._attributes['last_full_plan_trigger'] == 'slot_boundary'
    assert scheduled[-1][0] == 1800
    count = len(solves)
    source._lifecycle_stopped = True
    source._cancel_slot_boundary()
    await callback(h.CLOCK['now'])
    assert len(solves) == count
    print('PASS exact half-hour refresh and lifecycle cancellation')


if __name__ == '__main__': asyncio.run(main())
