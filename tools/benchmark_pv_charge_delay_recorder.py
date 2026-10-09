"""72h isolated real HA/SQLite measurement of the NEW delay attribute delta.

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
from custom_components.hoymiles_hit_modbus.pv_charge_delay import ChargeDelayPlan, attributes, LIVE_ATTRIBUTES, stabilize_attributes, PROFILE_WEIGHTS


class Probe(SensorEntity):
    _attr_should_poll=False
    _attr_native_value='ready'
    _attr_extra_state_attributes={}
    _attr_unique_id='offline_pv_charge_delay_probe'
    _unrecorded_attributes=HoymilesRCEOptimizerSensor._unrecorded_attributes


async def benchmark(output):
    output.mkdir(parents=True,exist_ok=True)
    database=output/'delay.sqlite'
    if database.exists(): raise ValueError('Use a new evidence output directory')
    with tempfile.TemporaryDirectory(prefix='pv-delay-recorder-') as temp:
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
            'purge_keep_days':2,'commit_interval':0,'include':{'entities':['sensor.pv_delay_probe']}}})
        recorder=get_instance(hass)
        await hass.async_start();await recorder.async_block_till_done()
        sensor=Probe();sensor.entity_id='sensor.pv_delay_probe'
        component=EntityComponent(logging.getLogger(__name__),'sensor',hass)
        await component.async_add_entities([sensor])
        await hass.async_block_till_done()
        first=datetime(2026,9,22,tzinfo=timezone.utc)
        for minute in range(72*60):
            now=first+timedelta(minutes=minute)
            dawn=now.replace(hour=6,minute=0,second=0,microsecond=0)
            plan=ChargeDelayPlan(dawn,dawn+timedelta(hours=3.5),dawn+timedelta(hours=6),
                2.5+(minute%60)/1000,8.+(minute%3)/100,8.4,117) if 4<=now.hour<10 else None
            proposal=attributes(plan,enabled=True,now=now)
            profile=tuple(PROFILE_WEIGHTS)[minute//1440]
            proposal.update(pv_charge_delay_profile=profile,
                pv_charge_delay_p10_weight=PROFILE_WEIGHTS[profile],
                # These are lifecycle diagnostics, not per-minute counters.
                # Exercise exclusion in the existing lifecycle budget; this
                # benchmark does not claim state-event deduplication by HA.
                pv_charge_delay_planner_power_basis='pv_hold' if plan else 'self_use',
                pv_charge_delay_planner_settling_active=bool(plan),
                pv_charge_delay_planner_settling_until=plan.start.isoformat() if plan else None)
            sensor._attr_extra_state_attributes=stabilize_attributes(
                proposal, sensor._attr_extra_state_attributes)
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
        assert LIVE_ATTRIBUTES <= sensor._unrecorded_attributes
        assert all(not LIVE_ATTRIBUTES.intersection(a) for a in attrs),attrs
        # Existing 14-row budget plus exactly two explicit user profile changes.
        assert rows <= 16,(rows,attrs)
        report={'scope':'new_delay_attribute_delta_with_production_RCE_exclusions_real_HA_Recorder',
            'simulated_hours':72,'callbacks':4320,'state_rows':rows,'attribute_rows':len(attrs),
            'explicit_profile_changes':2,'profile_weights':PROFILE_WEIGHTS,
            'max_recorded_attributes_bytes':max(len(json.dumps(a).encode()) for a in attrs),
            'page_count':pages,'database_bytes':database.stat().st_size,
            'wal_bytes':Path(str(database)+'-wal').stat().st_size if Path(str(database)+'-wal').exists() else 0,
            'live_fields_excluded':True,'extra_history_queries':0,'new_persistent_store':False}
        report['database_bytes_after_shutdown']=database.stat().st_size
        report['wal_bytes_after_shutdown']=Path(str(database)+'-wal').stat().st_size if Path(str(database)+'-wal').exists() else 0
        (output/'RECORDER.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    asyncio.run(benchmark(parser.parse_args().output))
