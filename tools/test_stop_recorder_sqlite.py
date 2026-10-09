"""Actual HA Recorder replay: STOP compression, mixed history and cold reopen."""
import asyncio
import copy
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from test_recorder_duplication import history, http, evidence
from test_stop_recorder_storage import frames


class StopRecorderSQLiteTests(unittest.TestCase):
    def test_real_recorder_and_cold_reopen_preserve_full_frames(self):
        from homeassistant import config_entries, loader
        from homeassistant.components import recorder
        from homeassistant.components.recorder import get_instance
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import recorder as recorder_helper
        from homeassistant.setup import async_setup_component

        async def run():
            with tempfile.TemporaryDirectory(prefix='ems-stop-recorder-') as folder:
                db_path = Path(folder) / 'recorder.db'
                base = time.time()
                expected = {}
                async def boot():
                    hass = HomeAssistant(folder)
                    loader.async_setup(hass)
                    hass.config_entries = config_entries.ConfigEntries(hass, {})
                    recorder_helper.async_initialize_recorder(hass)
                    cfg = recorder.CONFIG_SCHEMA({'recorder': {
                        'db_url': f'sqlite:///{db_path.as_posix()}', 'auto_purge': False,
                        'auto_repack': False}})
                    self.assertTrue(await async_setup_component(hass, 'recorder', cfg))
                    await hass.async_start()
                    return hass, get_instance(hass)

                async def stop(hass, instance):
                    await hass.async_stop()
                    await asyncio.to_thread(instance.join, 5)
                    if instance.engine is not None:
                        instance.engine.dispose()

                hass, instance = await boot()
                try:
                    rolling = []
                    for tick in range(240):
                        if tick % 10 == 0:
                            frame = {**frames()[tick % 8], 'transaction_id': f'tx-{tick}',
                                     'at': datetime.fromtimestamp(base + tick, timezone.utc).isoformat()}
                            expected[frame['transaction_id']] = frame
                            rolling = (rolling + [frame])[-8:]
                        attrs = evidence(base)
                        attrs['reason'] = 'cohort_missing' if tick % 2 else 'ready'
                        attrs['recent_stop_decisions'] = copy.deepcopy(rolling)
                        attrs['recorded_execution'] = history.supervisor_recorder_projection(attrs)
                        for name in ('before', 'after', 'mixed'):
                            live = copy.deepcopy(attrs)
                            excluded = history.SUPERVISOR_UNRECORDED_ATTRIBUTES
                            if name == 'after' or (name == 'mixed' and 60 <= tick < 180):
                                live[history.RECORDED_STOP_ATTRIBUTE] = history.stop_recorder_projection(rolling)
                                excluded = excluded | {'recent_stop_decisions'}
                            # Every main payload remains below HA's hard limit.
                            recorded = {k:v for k,v in live.items() if k not in excluded}
                            self.assertLess(len(json.dumps(recorded).encode()), 16384)
                            hass.states.async_set(f'sensor.{name}', str(tick % 2), live,
                                                  state_info={'unrecorded_attributes': excluded},
                                                  timestamp=base + tick)
                        before = dict(hass.states.get('sensor.before').attributes)
                        after = dict(hass.states.get('sensor.after').attributes)
                        after.pop(history.RECORDED_STOP_ATTRIBUTE)
                        self.assertEqual(before, after)
                    await instance.async_block_till_done()
                    for name in ('before', 'after', 'mixed'):
                        answer = await instance.async_add_executor_job(http._read_stop_history, hass,
                            {'supervisor': f'sensor.{name}'}, base-1, base+241)
                        self.assertEqual(answer['status'], 'available', answer)
                        self.assertFalse(answer['truncated'])
                        self.assertEqual({f['transaction_id']: f for f in answer['events']}, expected)
                    for name, extra in (
                        ('bad_compact_with_raw', {'recorded_stop_decisions': {'schema_version': 99}, 'recent_stop_decisions': rolling}),
                        ('bad_compact_only', {'recorded_stop_decisions': {'schema_version': 99}}),
                        ('conflict', {'recorded_stop_decisions': history.stop_recorder_projection(rolling),
                                     'recent_stop_decisions': [{**f, 'reason':'conflicting'} for f in rolling]}),
                    ):
                        hass.states.async_set(f'sensor.{name}', 'idle', extra, timestamp=base+240)
                    await instance.async_block_till_done()
                    for name in ('bad_compact_with_raw', 'bad_compact_only', 'conflict'):
                        report = await instance.async_add_executor_job(http._read_stop_history, hass,
                            {'supervisor':f'sensor.{name}'}, base-1, base+242)
                        self.assertEqual(report['status'], 'partial')
                        self.assertGreater(report['invalid_payloads'], 0)
                        if name == 'bad_compact_with_raw':
                            self.assertEqual({f['transaction_id']:f for f in report['events']},
                                             {f['transaction_id']:f for f in rolling})
                        elif name == 'bad_compact_only':
                            self.assertEqual(report['events'], [])
                        else:
                            self.assertEqual(report['conflicting_transactions'], len(rolling))
                            self.assertEqual(len(report['events']), 2*len(rolling))
                    with closing(sqlite3.connect(db_path)) as db:
                        metrics = {}
                        for name in ('before', 'after'):
                            rows = db.execute('SELECT s.attributes_id, a.shared_attrs FROM states s '
                                'JOIN states_meta m ON s.metadata_id=m.metadata_id '
                                'JOIN state_attributes a ON a.attributes_id=s.attributes_id '
                                'WHERE m.entity_id=?', (f'sensor.{name}',)).fetchall()
                            unique = dict(rows)
                            metrics[name] = {'rows': len(rows), 'attrs': len(unique),
                                             'bytes': sum(len(v.encode()) for v in unique.values())}
                            for raw in unique.values():
                                decoded = history.recorded_stop_decisions(json.loads(raw))
                                self.assertIsNotNone(decoded)
                                for f in decoded:
                                    self.assertEqual(f, expected[f['transaction_id']])
                        self.assertEqual(metrics['before']['rows'], metrics['after']['rows'])
                        self.assertLess(metrics['after']['bytes'], metrics['before']['bytes'] * .5)
                        print('SQLITE_METRICS', json.dumps(metrics, sort_keys=True))
                finally:
                    await stop(hass, instance)
                # Fresh HA/Recorder instance sees old/new/rollback-era rows.
                hass, instance = await boot()
                try:
                    answer = await instance.async_add_executor_job(http._read_stop_history, hass,
                        {'supervisor':'sensor.mixed'}, base-1, base+241)
                    self.assertEqual({f['transaction_id']:f for f in answer['events']}, expected)
                finally:
                    await stop(hass, instance)
        asyncio.run(run())


if __name__ == '__main__':
    unittest.main()
