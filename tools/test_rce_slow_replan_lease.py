"""Slow solver publication must not silently expire a proven RCE run."""
import asyncio
import json
import sys
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from test_rce_tariff_lease_continuity import TariffContinuity
from test_rce_publication_continuity import h


class SlowContinuity(TariffContinuity):
    delay = 28
    async def executor(self, func, *args):
        result = func(*args)
        future = self.loop.create_future()
        observation = {"started": self.second, "due": self.second + self.delay,
                       "load_kw": args[0].current_load_power_kw,
                       "pv_kw": args[0].current_pv_power_kw,
                       "reports_at_start": len(self.telemetry)}
        self.solver_observations.append(observation)
        self.jobs.append((self.second + self.delay, future, result, observation))
        return await future


async def pending_vetoes():
    probe = SlowContinuity()
    await probe.setup()
    try:
        for second in range(1, 31):
            await probe.tick(second)
        sensor = probe.supervisor
        original = sensor._fresh_active_frame
        frame = original()
        assert sensor._control_lease_renewal_evidence() is not None
        cases = {
            'permission': replace(frame, rce=replace(frame.rce, allowed_by_user=False)),
            'disabled': replace(frame, rce=replace(frame.rce, enabled=False)),
            'slot_withdrawn': replace(frame, rce=replace(frame.rce, current_slot_continue_eligible=False)),
            'shortened_run': replace(frame, rce=replace(frame.rce, current_run_end=h.CLOCK['now']+timedelta(seconds=29))),
            'sale_block': replace(frame, rce=replace(frame.rce, sale_block_active=True)),
            'reserve': replace(frame, rce=replace(frame.rce, current_soc_percent=20)),
            'bms_drop': replace(frame, execution=replace(frame.execution, bms_max_discharge_current_a=1)),
            'stale_fc03': replace(frame, execution=replace(frame.execution, full_block_generation_at=h.CLOCK['now']-timedelta(seconds=60))),
            'off_grid': replace(frame, execution=replace(frame.execution, physical_mode_code=2)),
            'export_block': replace(frame,
                context=replace(frame.context, export_state=h.SENSOR.ExportState.CONFIRMED_ZERO_EXPORT),
                execution=replace(frame.execution, gcf_enable_code=1, effective_export_limit_percent=0)),
        }
        for name, bad in cases.items():
            sensor._fresh_active_frame = lambda: bad
            evidence = sensor._control_lease_renewal_evidence()
            if name == 'shortened_run':
                assert evidence is not None
                assert sensor._control_lease_authorization_deadline <= bad.rce.current_run_end
            else:
                assert evidence is None, name
            print('PASS pending renewal veto', name)
        sensor._fresh_active_frame = original
        for helper in (None, False, True):
            sensor._fresh_active_frame = lambda: replace(
                frame, rce=replace(frame.rce, price_above_threshold=helper))
            assert sensor._control_lease_renewal_evidence() is not None, helper
        sensor._fresh_active_frame = original
        sensor._pause_state = 'on'
        assert sensor._control_lease_renewal_evidence() is None
        sensor._pause_state = 'off'
        sensor._master_stop_latched = True
        assert sensor._control_lease_renewal_evidence() is None
        sensor._master_stop_latched = False
        anchor = probe.controller._rce_execution_hold
        assert anchor is not None
        assert not probe.controller.rce_replan_lease_authorized(
            frame, now=anchor[1]+timedelta(seconds=anchor[2]-29), lease_ttl_seconds=30)
        assert probe.controller._rce_execution_hold == anchor
        print('PASS pause, Master STOP, bounded renewal expires inside original hold')
    finally:
        probe.loop.time = probe.real_loop_time
        for task in probe.tasks:
            if not task.done(): task.cancel()
        await asyncio.gather(*probe.tasks, return_exceptions=True)


async def main():
    probe = SlowContinuity()
    try:
        result = await probe.run('master_stop')
        print('PASS slow 28-second solver, real publication/helpers/controller, unchanged tx and deadline')
        await pending_vetoes()
    except AssertionError as error:
        result = {'status': 'FAIL', 'error': str(error), 'frames': probe.frames,
                  'renewals': probe.services.renewals, 'publications': probe.publications}
        raise
    finally:
        if len(sys.argv) > 1:
            Path(sys.argv[1]).write_text(json.dumps(result, indent=2, default=str), encoding='utf-8')


if __name__ == '__main__':
    asyncio.run(main())
