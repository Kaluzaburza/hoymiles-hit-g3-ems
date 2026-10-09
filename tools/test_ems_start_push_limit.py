"""Provider-boundary regression: shared rolling cap survives restart/ambiguity."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from test_ems_notifications import MODULE, START


class Store:
    def __init__(self, raw=None):
        self.raw = raw
    async def async_load(self):
        return deepcopy(self.raw)
    async def async_save(self, raw):
        self.raw = deepcopy(raw)


async def main():
    calls = []
    clock = [START]
    async def send(*args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 2:
            raise TimeoutError('provider may have accepted')
    hass = SimpleNamespace(config=SimpleNamespace(language='pl'),
        services=SimpleNamespace(async_call=send))
    def manager(store):
        # Store constructor needs HA; the tested paths use this durable fake.
        obj = object.__new__(MODULE.HoymilesEmsNotificationManager)
        obj.hass = hass
        obj._store = store
        obj._model = MODULE.RangeNotificationModel()
        obj._deliveries = []
        obj._start_push_attempts = []
        obj._closed = False
        obj._delivery_degradation = None
        obj._status_sink = None
        obj._state_revision = obj._persisted_revision = 0
        obj._dropped_observations = obj._dropped_equivalent_observations = obj._dropped_material_observations = 0
        obj._delivery_configuration = lambda: (True, 'notify.fake')
        return obj
    def event(n, policy='rce', kind='start', outcome=None, end=True):
        return MODULE.NotificationEvent(str(n), str(n), policy, kind,
            clock[0], clock[0], clock[0] + timedelta(minutes=30) if end else None, outcome)
    original = MODULE.dt_util.utcnow
    disabled = MODULE._LOGGER.disabled
    MODULE.dt_util.utcnow = lambda: clock[0]
    MODULE._LOGGER.disabled = True
    try:
        store = Store()
        obj = manager(store)
        await obj.async_initialize()
        for n, policy in ((1, 'rce'), (2, 'tariff'), (3, 'rce')):
            clock[0] += timedelta(minutes=1)
            await obj._async_accept((event(n, policy),), now=clock[0])
        assert len(calls) == 2
        assert obj._deliveries[-1].state == 'suppressed'
        assert len(store.raw['start_push_attempts']) == 2
        for n, outcome in enumerate(('completed', 'interrupted', 'unconfirmed'), 10):
            await obj._async_accept((event(n, kind='end', outcome=outcome),), now=clock[0])
            assert obj._deliveries[-1].state == 'suppressed'
        # Diagnostic churn must never evict the separate hourly quota.
        for n in range(40, 90):
            await obj._async_accept((event(n, end=False),), now=clock[0])
        restarted = manager(store)
        await restarted.async_initialize()
        await restarted._async_accept((event(100),), now=clock[0])
        assert len(calls) == 2
        clock[0] = START + timedelta(minutes=61)
        await restarted._async_accept((event(101),), now=clock[0])
        assert len(calls) == 3
        assert 'Planowany zakres:' in calls[-1][0][2]['message']
        # Backward clock jumps cannot grant more capacity.
        clock[0] = START
        await restarted._async_accept((event(102),), now=clock[0])
        assert len(calls) == 3
        legacy = Store({'schema_version': 1, 'recent': []})
        migrated = manager(legacy)
        await migrated.async_initialize()
        await migrated._async_accept((event(103),), now=clock[0])
        assert len(calls) == 3
        assert len(legacy.raw['start_push_attempts']) == 2
    finally:
        MODULE.dt_util.utcnow = original
        MODULE._LOGGER.disabled = disabled
    print('PASS shared rolling START cap, terminals, ambiguity, restart, migration, clock rollback')


if __name__ == '__main__':
    asyncio.run(main())
