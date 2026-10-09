"""Initial leased RCE start interrupted by a planner update during challenge.

Real HA adapter/controller/helper paths, deterministic local transport only.
The 2026-09-27 log proves retarget_not_authorized; the recorded RCM/power
publications motivate this race regression, not an exact internal-state trace.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

from test_rce_publication_continuity import Continuity, fixture, h


async def scenario(negative=None):
    probe = Continuity()
    original = fixture.LeaseServices
    challenges = []
    challenge_calls = []

    class PendingServices(original):
        async def async_call(self, domain, service, data, **kwargs):
            if service.endswith('control_lease_challenge'):
                challenge_calls.append(service)
            if service.endswith('control_lease_challenge') and not challenges:
                challenges.append(service)
                probe.supervisor._schedule_planner_callback(frozenset({'rcm_plan'}))
                if negative in ('generation', 'generation_bms'):
                    for key in ('ems_mode_readback', 'self_use_soc_readback',
                                'backup_soc_readback', 'charge_soc_readback',
                                'charge_power_ems_readback', 'discharge_soc_readback',
                                'discharge_power_readback'):
                        probe.report(key, probe.hass.states.values[probe.eid(key)].state)
                    probe.report('ems_generation', 2)
                if negative in ('bms', 'generation_bms'):
                    probe.report('bms_max_discharge_current', 0)
                    probe.direct('sensor.hoymiles_hit_maximum_discharge_current', 0)
                elif negative == 'permission':
                    probe.direct(probe.eid('allow_rce'), 'off')
                elif negative == 'pause':
                    probe.direct('input_boolean.hoymiles_ems_paused', 'on')
                elif negative == 'master_stop':
                    probe.supervisor.request_master_stop()

                async def finish_publication():
                    for _ in range(120 if negative == 'timeout' else 20):
                        await asyncio.sleep(0)
                        h.CLOCK['now'] += timedelta(milliseconds=50)
                        for handle in list(probe.hass.active_delays()):
                            if negative != 'timeout' and handle.when <= h.CLOCK['now']:
                                handle.run()

                probe.spawn(finish_publication())
            return await super().async_call(domain, service, data, **kwargs)

    fixture.LeaseServices = PendingServices
    try:
        try:
            await probe.setup()
        except AssertionError:
            if negative and negative != 'generation':
                assert probe.services.arms == 0
                tx=probe.supervisor._controller.record.transaction
                assert tx is None or tx.command_sent_at is None
                print('PASS pending start sends no forced command:',negative)
                return
            raise
        assert negative in (None, 'generation'), 'unsafe start accepted: '+str(negative)
        assert len(challenges) == 1
        assert len(challenge_calls) == (2 if negative == 'generation' else 1)
        assert probe.services.arms == 1
        assert probe.controller.record.transaction.command_sent_at is not None
        assert probe.controller.record.reason is not h.SENSOR.ExecutionReason.COMMAND_NOT_QUEUED
        if negative == 'generation':
            assert probe.services.arm_requests[0]['snapshot_generation'] == 2
        print('PASS initial RCE start waits for pending publication and rechecks authority')
    finally:
        fixture.LeaseServices = original
        if hasattr(probe, 'real_loop_time'):
            probe.loop.time = probe.real_loop_time
        for task in getattr(probe, 'tasks', ()):
            if not task.done():
                task.cancel()
        await asyncio.gather(*getattr(probe, 'tasks', ()), return_exceptions=True)


async def main():
    for negative in ('generation','generation_bms',None,'bms','permission','pause','master_stop','timeout'):
        await scenario(negative)


if __name__ == '__main__':
    asyncio.run(main())
