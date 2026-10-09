"""Two-hour simulated run through four slots, repeated omission and tariffs."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sys
from test_rce_tariff_lease_continuity import TariffContinuity, NOW, h


async def main():
    probe = TariffContinuity()
    await probe.setup()
    optimizer = sys.modules[probe.module.optimize_rce.__module__]
    original = probe.module.optimize_rce
    probe.module.optimize_rce = lambda settings: optimizer._optimize_rce_impl(
        settings, fixed_exports={}, fixed_current_discharge_cap_kw=0)
    try:
        for second in range(1, 7191):
            if second > 240 and second % 120 == 0:
                probe.spawn(probe.source._async_timer(h.CLOCK['now']))
            await probe.tick(second)
        assert len(probe.solver_observations) >= 50
        assert probe.source._attributes['active_slot_commitment_applied']
        print('PASS same transaction through 119m50s, four slots, >=50 replans, no restore')
        # At the immutable run deadline, physical restoration must occur.
        h.CLOCK['now'] = NOW + timedelta(hours=2, microseconds=1)
        probe.services.oracle.advance(10.000001)
        probe.renderer.publish_all(h.CLOCK['now'])
        await probe.settle()
        assert probe.services.restore_calls or probe.services.oracle.state == 'restoring'
        print('PASS natural deadline withdraws execution and starts restore')
        result = {'scope': 'deterministic scheduler/transport/firmware model',
                  'transaction_id': probe.original_tx,
                  'duration_s': 7200, 'solver_jobs': probe.solver_observations,
                  'renewals': len(probe.services.renewals),
                  'restore_calls': probe.services.restore_calls,
                  'final_controller': probe.controller.record.state.value,
                  'final_oracle': probe.services.oracle.state}
        if len(sys.argv) > 1:
            Path(sys.argv[1]).write_text(json.dumps(result, indent=2, default=str),encoding='utf-8')
    finally:
        probe.module.optimize_rce = original
        probe.loop.time = probe.real_loop_time
        for task in probe.tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*probe.tasks, return_exceptions=True)


if __name__ == '__main__': asyncio.run(main())
