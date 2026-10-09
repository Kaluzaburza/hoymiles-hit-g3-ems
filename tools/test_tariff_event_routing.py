"""HA changed/reported routing through production power collector/controller."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import test_supervisor_sensor_contract as h
import test_tariff_pending_dispatch_race as race

f = race.fixture
KEYS = ('pv_power', 'battery_power', 'grid_power', 'load_power')
VALUES = dict(zip(KEYS, (0, 0, -1200, 1200)))


def setup():
    hass, entry, _, sensor = h.environment()
    h.add(sensor)
    sensor._controller = SimpleNamespace(record=SimpleNamespace(
        transaction=SimpleNamespace(owner=h.SENSOR.ExecutionOwner.TARIFF)))
    sensor._recompute = lambda: None
    return hass, entry, sensor


def route(hass, entry, sensor, changed, *, missing=None, expired=False):
    for i, key in enumerate(KEYS):
        if key == missing:
            continue
        eid = h._source_entity_id(h.SENSOR._SOURCE_BY_KEY[key], entry.entry_id)
        at = f.NOW + timedelta(seconds=10 + (0, .10, .18, .26)[i])
        new = h.FakeState(str(VALUES[key]), reported=at)
        if expired and key == 'load_power':
            for timer in tuple(hass.active_delays()):
                timer.run()
        # HA emits ONE class per state write, including attribute-only changes.
        (hass.fire_state if key in changed else hass.fire_report)(eid, new)
    return sensor._read_source_states()


def matrix():
    for mask in range(16):
        hass, entry, sensor = setup()
        before = sensor._power_cohort_generation
        changed = {key for i, key in enumerate(KEYS) if mask & (1 << i)}
        recomputes = []
        sensor._recompute = lambda: recomputes.append(sensor._power_cohort_generation)
        states = route(hass, entry, sensor, changed)
        assert sensor._power_cohort_generation == before + 1, (mask, changed)
        assert {key: float(states[key].state) for key in KEYS} == VALUES
        assert recomputes == [before, before + 1], (mask, recomputes)
        assert not hass.active_delays()
        for kwargs in ({'missing': 'load_power'}, {'expired': True}):
            old = dict(sensor._power_cohort_states)
            gen = sensor._power_cohort_generation
            route(hass, entry, sensor, changed, **kwargs)
            assert sensor._power_cohort_states == old
            assert sensor._power_cohort_generation == gen
            for timer in tuple(hass.active_delays()):
                timer.run()


async def controller(hass, entry, sensor):
    clock = [f.NOW]
    async def persist(_): pass
    async def dispatch(_): pass
    control = f.SupervisorActiveController(persist=persist, dispatch=dispatch,
        publish=lambda _: None, clock=lambda: clock[0])
    await control.async_initialize()
    await control.async_reconcile(f.frame(clock[0], f.execution_source(clock[0]), target=58))
    assert control.record.state is f.ActiveState.WAITING_READBACK
    sensor._controller = control
    sensor._power_cohort_states = {key: h.FakeState(str(value), reported=f.NOW)
                                  for key, value in VALUES.items()}
    states = route(hass, entry, sensor, {'battery_power', 'grid_power', 'load_power'})
    clock[0] = f.NOW + timedelta(seconds=11)
    updates = {}
    for key in KEYS:
        updates[key + '_w'] = float(states[key].state)
        updates[key + '_observed_at'] = states[key].last_reported
    src = replace(f.physical(clock[0]), **updates, power_cohort_complete=True)
    await control.async_reconcile(f.frame(clock[0], src, control.record, target=58))
    assert control.record.state is f.ActiveState.EXECUTING, control.record.reason


if __name__ == '__main__':
    matrix()
    asyncio.run(controller(*setup()))
    print('PASS: 16 HA event routes, bounded missing/late data, single publication and controller proof')
