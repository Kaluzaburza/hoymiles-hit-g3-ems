"""Real Supervisor commitment -> RCE publication -> same live transaction.

The candidate selection omits the current slot, as in the field capture. This
does not claim an exact reconstruction of the historical solver's inputs.
"""
from __future__ import annotations
import asyncio
import sys
from datetime import timedelta
from test_rce_publication_continuity import Continuity, h


async def main():
    probe=Continuity()
    await probe.setup()
    if len(sys.argv) > 1:
        worker_seconds = int(sys.argv[1])
        async def executor(function, *args):
            result = function(*args)
            future = probe.loop.create_future()
            probe.jobs.append((probe.second+worker_seconds, future, result, {}))
            return await future
        probe.hass.async_add_executor_job = executor
    original_solver=probe.module.optimize_rce
    try:
        attach=getattr(probe.source,'attach_supervisor_commitment_source',None)
        if attach:
            attach(probe.supervisor)
        optimizer=sys.modules[probe.module.optimize_rce.__module__]
        def recorded_selection(settings):
            start=optimizer.floor_half_hour(settings.now)+timedelta(minutes=30)
            return optimizer._optimize_rce_impl(settings, fixed_exports={start:1.0},
                                                fixed_current_discharge_cap_kw=0)
        probe.module.optimize_rce=recorded_selection
        for second in range(1,31):
            await probe.tick(second)
            if second == 12 and attach:
                assert probe.source._active_rce_commitment(h.CLOCK['now']) is not None
                # FC03 floats may differ within the executor's established
                # register tolerance. Confirmation must not depend on ==.
                entity_id = probe.eid('discharge_power_readback')
                saved = probe.hass.states.values[entity_id]
                probe.direct(entity_id, float(saved.state) - 0.0000015)
                assert probe.source._active_rce_commitment(h.CLOCK['now']) is not None, 'float32 readback rejected'
                probe.direct(entity_id, float(saved.state) - 1.0)
                assert probe.source._active_rce_commitment(h.CLOCK['now']) is None, 'different physical target accepted'
                probe.hass.states.values[entity_id] = saved
                probe.supervisor._pause_state='on'
                assert probe.source._active_rce_commitment(h.CLOCK['now']) is None
                probe.supervisor._pause_state='off'
                probe.supervisor._master_stop_latched=True
                assert probe.source._active_rce_commitment(h.CLOCK['now']) is None
                probe.supervisor._master_stop_latched=False
        assert probe.source._attributes['active_slot_commitment_applied'] is True
        assert probe.controller.record.transaction.transaction_id == probe.original_tx
        assert not probe.services.restore_calls
        print('PASS fresh selection omits current slot; confirmed same-entry export continues')
        print('PASS commitment is denied during operator pause and Master STOP')
    finally:
        probe.module.optimize_rce=original_solver
        probe.loop.time=probe.real_loop_time
        for task in probe.tasks:
            if not task.done():task.cancel()
        await asyncio.gather(*probe.tasks,return_exceptions=True)


if __name__=='__main__':
    asyncio.run(main())
