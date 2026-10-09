"""72h isolated real HA/SQLite measurement of the new EV diagnostic attribute delta.

Production RCE exclusions, a minimal entity harness, no site access or history
queries. This is not whole-EMS growth or proof of field execution history.
"""
import argparse, asyncio, json, logging, sqlite3, tempfile, sys
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import SensorEntity
from homeassistant import loader, config_entries
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.setup import async_setup_component
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers import recorder as recorder_helper, entity as entity_helper, frame
from homeassistant.components.recorder import get_instance

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from custom_components.hoymiles_hit_modbus.rce_sensor import HoymilesRCEOptimizerSensor
LIVE_ATTRIBUTES = frozenset(("ev_load_filter_status", "ev_load_filter_excluded_days"))


class Probe(SensorEntity):
    _attr_should_poll=False
    _attr_native_value='ready'
    _attr_extra_state_attributes={}
    _attr_unique_id='offline_ev_load_probe'
    _unrecorded_attributes=HoymilesRCEOptimizerSensor._unrecorded_attributes
    # Use the real plan publication property; runtime EV diagnostics stay in
    # RAM and the card reads its source directly, not from Recorder attributes.
    extra_state_attributes = HoymilesRCEOptimizerSensor.extra_state_attributes
    _attributes = {}


async def benchmark(output):
    output.mkdir(parents=True,exist_ok=True)
    database=output/'ev.sqlite'
    if database.exists(): raise ValueError('Use a new evidence output directory')
    with tempfile.TemporaryDirectory(prefix='ev-load-recorder-') as temp:
        hass=HomeAssistant(temp)
        loader.async_setup(hass)
        hass.config_entries=config_entries.ConfigEntries(hass,{})
        await hass.config_entries.async_initialize()
        await async_load_base_functionality(hass)
        recorder_helper.async_initialize_recorder(hass)
        entity_helper.async_setup(hass);frame.async_setup(hass)
        hass.config.skip_pip=True
        assert await async_setup_component(hass,'recorder',{'recorder':{
            'db_url':'sqlite:///'+database.as_posix(),'auto_purge':False,'auto_repack':False,
            'purge_keep_days':2,'commit_interval':0,'include':{'entities':['sensor.ev_load_probe']}}})
        recorder=get_instance(hass)
        await hass.async_start();await recorder.async_block_till_done()
        sensor=Probe();sensor.entity_id='sensor.ev_load_probe'
        component=EntityComponent(logging.getLogger(__name__),'sensor',hass)
        await component.async_add_entities([sensor])
        await hass.async_block_till_done()
        first=datetime(2026,9,22,tzinfo=timezone.utc)
        for minute in range(72*60):
            now=first+timedelta(minutes=minute)
            sensor._ev_runtime_diagnostics={
                'ev_load_filter_status': ('off','sensor','heuristic','sensor_stale')[minute%4],
                'ev_load_filter_excluded_days': minute%29}
            sensor.async_write_ha_state()
            await asyncio.sleep(0)
        await hass.async_block_till_done();await recorder.async_block_till_done()
        with closing(sqlite3.connect(database)) as db:
            rows=db.execute('SELECT COUNT(*) FROM states').fetchone()[0]
            attrs=[json.loads(row[0]) for row in db.execute('SELECT shared_attrs FROM state_attributes')]
            pages=db.execute('PRAGMA page_count').fetchone()[0]
        await hass.async_stop()
        # Home Assistant stop returns before the Recorder thread necessarily
        # closes SQLite. Measure the final DB/WAL only after bounded shutdown.
        await asyncio.to_thread(recorder.join, 10)
        assert not recorder.is_alive(), "Recorder did not stop"
        if recorder.engine is not None:
            recorder.engine.dispose()
        source=(ROOT/'custom_components/hoymiles_hit_modbus/rce_sensor.py').read_text(encoding='utf-8')
        assert all(key not in source for key in LIVE_ATTRIBUTES)
        assert all(not LIVE_ATTRIBUTES.intersection(a) for a in attrs),attrs
        assert rows <= 2,(rows,attrs)
        report={'scope':'new_ev_diagnostic_attribute_delta_with_production_RCE_exclusions_real_HA_Recorder',
            'simulated_hours':72,'callbacks':4320,'state_rows':rows,'attribute_rows':len(attrs),
            'max_recorded_attributes_bytes':max(len(json.dumps(a).encode()) for a in attrs),
            'page_count':pages,'database_bytes':database.stat().st_size,
            'wal_bytes':Path(str(database)+'-wal').stat().st_size if Path(str(database)+'-wal').exists() else 0,
            'live_fields_not_published':True,'extra_history_queries_in_projection':0, 'history_reads_tested_separately':True,'new_persistent_store':False}
        report['database_bytes_after_shutdown']=database.stat().st_size
        report['wal_bytes_after_shutdown']=Path(str(database)+'-wal').stat().st_size if Path(str(database)+'-wal').exists() else 0
        (output/'RECORDER.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(benchmark(parser.parse_args().output))
