"""Isolated HA Recorder proof: exact entity purge preserves controls and LTS."""

from __future__ import annotations

import asyncio
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

from homeassistant import config_entries, loader
from homeassistant.components import recorder
from homeassistant.components.recorder import get_instance, statistics
from homeassistant.core import HomeAssistant
from homeassistant.helpers import recorder as recorder_helper
from homeassistant.setup import async_setup_component


class RetentionRuntimeTests(unittest.TestCase):
    def test_exact_purge_keeps_recent_states_other_entity_and_long_term_sum(self):
        async def run():
            with tempfile.TemporaryDirectory(prefix="stor02-purge-") as folder:
                db = Path(folder) / "recorder.db"
                hass = HomeAssistant(folder)
                loader.async_setup(hass)
                hass.config_entries = config_entries.ConfigEntries(hass, {})
                recorder_helper.async_initialize_recorder(hass)
                config = recorder.CONFIG_SCHEMA({"recorder": {
                    "db_url": f"sqlite:///{db.as_posix()}",
                    "auto_purge": False, "auto_repack": False,
                }})
                self.assertTrue(await async_setup_component(hass, "recorder", config))
                await hass.async_start()
                instance = get_instance(hass)
                now = time.time()

                def snapshot():
                    with closing(sqlite3.connect(db)) as connection:
                        states = dict(connection.execute("""
                            SELECT sm.entity_id, COUNT(*) FROM states s
                            JOIN states_meta sm ON sm.metadata_id=s.metadata_id
                            WHERE sm.entity_id IN ('sensor.target', 'sensor.control')
                            GROUP BY sm.entity_id
                        """).fetchall())
                        stats = connection.execute("""
                            SELECT COUNT(*), SUM(st.sum) FROM statistics st
                            JOIN statistics_meta sm ON sm.id=st.metadata_id
                            WHERE sm.statistic_id='sensor.target'
                        """).fetchone()
                    return states, stats

                async def await_statistics(expected):
                    # HA can requeue an import task behind the first commit
                    # barrier. Wait for the actual DB row, not one queue pass.
                    deadline = asyncio.get_running_loop().time() + 3.0
                    while True:
                        await instance.async_block_till_done()
                        _, observed = snapshot()
                        if observed == expected:
                            return
                        if asyncio.get_running_loop().time() >= deadline:
                            self.assertEqual(observed, expected)
                        await asyncio.sleep(0.02)

                try:
                    for age_days in (40, 2, 1):
                        for entity in ("sensor.target", "sensor.control"):
                            hass.states.async_set(
                                entity, str(100 - age_days),
                                {"state_class": "total_increasing",
                                 "device_class": "energy",
                                 "unit_of_measurement": "kWh"},
                                timestamp=now - age_days * 86400,
                            )
                    start = datetime.fromtimestamp(now - 40 * 86400,
                                                   timezone.utc).replace(
                        minute=0, second=0, microsecond=0
                    )
                    statistics.async_import_statistics(hass, {
                        "statistic_id": "sensor.target", "source": "recorder",
                        "name": "target", "unit_of_measurement": "kWh",
                        "has_mean": False, "has_sum": True,
                        "mean_type": 0, "unit_class": "energy",
                    }, [{"start": start, "sum": 10.0, "state": 10.0}])
                    await await_statistics((1, 10.0))
                    before_states, before_stats = snapshot()
                    self.assertEqual(before_states, {
                        "sensor.control": 3, "sensor.target": 3
                    })
                    self.assertEqual(before_stats, (1, 10.0))

                    await hass.services.async_call("recorder", "purge_entities", {
                        "entity_id": ["sensor.target"], "keep_days": 7,
                    }, blocking=True)
                    await instance.async_block_till_done()
                    after_states, after_stats = snapshot()
                    self.assertEqual(after_states, {
                        "sensor.control": 3, "sensor.target": 2
                    })
                    self.assertEqual(after_stats, before_stats)

                    next_start = datetime.fromtimestamp(now, timezone.utc).replace(
                        minute=0, second=0, microsecond=0
                    )
                    statistics.async_import_statistics(hass, {
                        "statistic_id": "sensor.target", "source": "recorder",
                        "name": "target", "unit_of_measurement": "kWh",
                        "has_mean": False, "has_sum": True,
                        "mean_type": 0, "unit_class": "energy",
                    }, [{"start": next_start, "sum": 11.0, "state": 11.0}])
                    await await_statistics((2, 21.0))
                    _, extended_stats = snapshot()
                    self.assertEqual(extended_stats, (2, 21.0))
                finally:
                    await hass.async_stop()
                    await asyncio.to_thread(instance.join, 5)
                    if instance.engine is not None:
                        instance.engine.dispose()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
