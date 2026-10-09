"""Actual isolated HA Recorder/SQLite test; synthetic 72h, no site I/O.

Uses the real entity registration path, including unrecorded attributes. This
measures only the new price entity, NOT savings or retention of an entire HA.
"""
import argparse
from contextlib import closing
import asyncio
from datetime import datetime,timedelta,timezone
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant import loader,config_entries
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.setup import async_setup_component
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers import recorder as recorder_helper,entity as entity_helper,frame
from homeassistant.components.recorder import get_instance

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from custom_components.hoymiles_hit_modbus.pstryk_runtime import PstrykPriceSensor,dt_util
from custom_components.hoymiles_hit_modbus.pstryk_prices import parse_prices,request_window
from custom_components.hoymiles_hit_modbus.pstryk_daily_cache import DailyPriceCache as PriceCache


async def benchmark(output):
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pstryk-recorder-') as temp:
        hass=HomeAssistant(temp)
        loader.async_setup(hass)
        hass.config_entries=config_entries.ConfigEntries(hass,{})
        await hass.config_entries.async_initialize()
        await async_load_base_functionality(hass)
        recorder_helper.async_initialize_recorder(hass)
        entity_helper.async_setup(hass)
        frame.async_setup(hass)
        hass.config.skip_pip=True
        database=output/'pstryk_recorder.sqlite'
        if database.exists(): raise ValueError('Choose a new output directory; evidence DB already exists')
        assert await async_setup_component(hass,'recorder',{'recorder':{
            'db_url':'sqlite:///'+database.as_posix(),'auto_purge':False,'auto_repack':False,
            'purge_keep_days':2,'commit_interval':0,
            'include':{'entities':['sensor.pstryk_benchmark']}}})
        recorder=get_instance(hass)
        await hass.async_start()
        await recorder.async_block_till_done()
        async def nothing(): pass
        coordinator=SimpleNamespace(cache=PriceCache('benchmark'),entry=SimpleNamespace(entry_id='test'),
            rce=SimpleNamespace(device_info=None),start=nothing,close=nothing)
        sensor=PstrykPriceSensor(coordinator); sensor.entity_id='sensor.pstryk_benchmark'
        component=EntityComponent(logging.getLogger(__name__),'sensor',hass)
        await component.async_add_entities([sensor])
        await hass.async_block_till_done()
        start=datetime(2026,9,22,tzinfo=timezone.utc)
        saved=0; publications=0; maximum_live=0
        with patch.object(dt_util,'utcnow') as clock:
            for minute in range(72*60):
                now=start+timedelta(minutes=minute);clock.return_value=now
                if minute%60==0:
                    left,right=request_window(now)
                    frames=[]; at=left
                    while at<right:
                        frames.append({'start':at.isoformat(),'end':(at+timedelta(hours=1)).isoformat(),
                                       'priceNet':round(.1+(at.hour%6)/10,2)})
                        at+=timedelta(hours=1)
                    coordinator.cache.accept(parse_prices({'frames':frames},source_scope='benchmark',
                        fetched_at=now,window_start=left,window_end=right),now=now)
                    candidate=coordinator.cache.storage_candidate()
                    if candidate: saved+=1;coordinator.cache.ack_storage(candidate)
                before=dict(sensor._projection)
                sensor.publish()
                publications+=before!=sensor._projection
                maximum_live=max(maximum_live,len(json.dumps(sensor.extra_state_attributes)))
                await asyncio.sleep(0)
        await hass.async_block_till_done()
        await recorder.async_block_till_done()
        with closing(sqlite3.connect(database)) as db:
            count=db.execute('SELECT COUNT(*) FROM states').fetchone()[0]
            attrs=db.execute('SELECT shared_attrs FROM state_attributes').fetchall()
            pages=db.execute('PRAGMA page_count').fetchone()[0]
            size=db.execute('PRAGMA page_size').fetchone()[0]
            free=db.execute('PRAGMA freelist_count').fetchone()[0]
        db.close()
        result={'scope':'isolated_HA_Recorder_SQLite_new_price_entity_only','hours':72,'callbacks':4320,
            'semantic_publications':publications,'cache_checkpoints':saved,'state_rows':count,
            'unique_attribute_rows':len(attrs),'maximum_recorded_attributes_bytes':max(len(a[0].encode()) for a in attrs),
            'maximum_live_attributes_bytes':maximum_live,'database_bytes':database.stat().st_size,
            'wal_bytes':Path(str(database)+'-wal').stat().st_size if Path(str(database)+'-wal').exists() else 0,
            'page_count':pages,'page_size':size,'freelist_count':free,
            'full_history_excluded':all('hours' not in json.loads(a[0]) for a in attrs)}
        assert publications==72 and saved==4,result
        assert count<=74 and len(attrs)<=74,result
        assert result['full_history_excluded'] and result['maximum_recorded_attributes_bytes']<1024,result
        await hass.async_stop()
        # Home Assistant stop returns before the Recorder thread necessarily
        # closes SQLite. Measure the final DB/WAL only after bounded shutdown.
        await asyncio.to_thread(recorder.join, 10)
        assert not recorder.is_alive(), "Recorder did not stop"
        if recorder.engine is not None:
            recorder.engine.dispose()
        result['database_bytes_after_shutdown']=database.stat().st_size
        result['wal_bytes_after_shutdown']=Path(str(database)+'-wal').stat().st_size if Path(str(database)+'-wal').exists() else 0
        (output/'RECORDER.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result,indent=2))
        return result

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(benchmark(parser.parse_args().output))
