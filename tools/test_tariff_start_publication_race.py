"""Real adapter/controller, synthetic concurrent publication, no host I/O."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import test_tariff_pending_dispatch_race as base
h, f = base.adapter, base.fixture


async def scenario(action, change):
    hass, entry, runtime, sensor = h.environment()
    sensor._pause_state = 'off'
    source = SimpleNamespace(entry_id='source', domain='esphome', data={'device_name':'source-node'})
    runtime.source_device.config_entry_id = source.entry_id
    hass.config_entries = SimpleNamespace(async_get_entry=lambda _:source)
    names = {f'source_node_{s}' for s in (
        'ems_supervisor_control_lease_challenge', 'ems_supervisor_write_complete_block_leased',
        'ems_supervisor_renew_control_lease')}
    names.update(f'source_node_{fam.value}' for fam in h.SENSOR.AtomicWriteFamily)
    tasks = []
    clock = [f.NOW]
    current = [None]
    class Services(base.LeaseServices):
        async def async_call(self, domain, service, data, **kwargs):
            if service.endswith('control_lease_challenge') and self.challenge_calls == 0:
                sensor._planner_cancel = lambda:None
                async def publish():
                    await asyncio.sleep(0)
                    sensor._cancel_planner_callback()
                    src = f.execution_source(clock[0])
                    opts = {}
                    if change in ('generation', 'generation_bms'):
                        src = replace(src, full_block_generation=12)
                    if change in ('bms', 'generation_bms'):
                        src = replace(src, bms_max_charge_current_a=0)
                    if change == 'invalid_settings':
                        src = replace(src, force_charge_soc_percent=101)
                    if change == 'permission': opts['allowed_by_user'] = False
                    if change == 'slot': opts['current_slot_planned'] = False
                    if change == 'pause': sensor._pause_state = 'on'
                    if change == 'stop': sensor._master_stop_latched = True
                    if change == 'deadline': clock[0] += timedelta(minutes=31)
                    h.CLOCK['now'] = clock[0]
                    current[0] = f.frame(clock[0], src, target=75, power=50, action=action, **opts)
                    sensor._latest_active_frame = current[0]
                tasks.append(asyncio.create_task(publish()))
            return await super().async_call(domain, service, data, **kwargs)
    services = Services(names, reject_arm_calls=set())
    hass.services = services
    sensor._assert_single_transport_instance = lambda:None
    async def persist(_): pass
    ctl = f.SupervisorActiveController(persist=persist, dispatch=sensor._async_dispatch_atomic_write,
        publish=lambda _:None, clock=lambda:clock[0], frame_resampler=lambda:current[0])
    sensor._controller = ctl
    # Production gating consumes this explicit immutable frame fixture; only
    # concurrent publication and transport are synthetic.
    sensor._current_dispatch_frame = lambda:current[0]
    sensor._fresh_active_frame = lambda:current[0]
    current[0] = f.frame(clock[0], f.execution_source(clock[0]), target=75, power=50, action=action)
    sensor._latest_active_frame = current[0]
    h.CLOCK['now'] = clock[0]
    await ctl.async_initialize()
    await ctl.async_reconcile(current[0])
    await asyncio.gather(*tasks)
    if change in (None, 'generation'):
        assert services.arm_calls == 1, (action, change, ctl.record.reason)
        assert ctl.record.transaction.command_sent_at is not None
        assert services.challenge_calls == (2 if change == 'generation' else 1)
    else:
        assert services.arm_calls == 0, (action, change)
        assert ctl.record.transaction is None or ctl.record.transaction.command_sent_at is None
        assert ctl.record.reason is not h.SENSOR.ExecutionReason.COMMAND_OUTCOME_UNKNOWN
    sensor._cancel_planner_callback()
    print('PASS', action.value, change)


async def main():
    for action in (f.TariffAction.GRID_SUPPORT, f.TariffAction.BATTERY_CHARGE,
                   f.TariffAction.GRID_SUPPORT_AND_CHARGE):
        for change in (None, 'generation', 'generation_bms', 'bms', 'invalid_settings', 'permission', 'slot', 'pause', 'stop', 'deadline'):
            await scenario(action, change)


if __name__ == '__main__': asyncio.run(main())
