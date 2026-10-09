"""Bound the Recorder cost of fresh, unchanged physical BMS reports.

Uses the production sensor's force_update setting and real HA/SQLite. It does
not claim physical Modbus cadence, full-host growth, or register accuracy.
"""
import argparse, asyncio, json, logging, sqlite3, tempfile, re
from contextlib import closing
from pathlib import Path
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.components.sensor import SensorEntity
from homeassistant import loader, config_entries
from homeassistant.bootstrap import async_load_base_functionality
from homeassistant.setup import async_setup_component
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.helpers import recorder as recorder_helper, entity as entity_helper, frame
from homeassistant.components.recorder import get_instance

ROOT=Path(__file__).resolve().parents[1]

async def benchmark(output):
    source=yaml.safe_load((ROOT/'packages/battery.yaml').read_text(encoding='utf-8'))
    bms=next(s for s in source['sensor'] if s.get('id')=='battery_power_1914')
    assert bms['modbus_controller_id']=='${modbus_fast_controller_id}'
    assert bms['platform']=='modbus_controller' and bms['force_update'] is True
    assert bms['register_type']=='read' and bms['address']==1914 and bms['value_type']=='S_DWORD'
    assert not any(k in bms for k in ('on_value','lambda','filters','skip_updates'))
    entry=(ROOT/'hoymiles-inverter.yaml').read_text(encoding='utf-8')
    period=int(re.search(r'fast_update_interval: "(\d+)s"',entry)[1])
    assert period==13
    samples=len(range(0,72*3600,period))
    output.mkdir(parents=True,exist_ok=True);database=output/'bms.sqlite'
    if database.exists():raise ValueError('Use a new evidence output directory')
    with tempfile.TemporaryDirectory(prefix='pv-bms-recorder-') as temp:
        hass=HomeAssistant(temp);loader.async_setup(hass)
        hass.config_entries=config_entries.ConfigEntries(hass,{})
        await hass.config_entries.async_initialize();await async_load_base_functionality(hass)
        recorder_helper.async_initialize_recorder(hass)
        entity_helper.async_setup(hass);frame.async_setup(hass);hass.config.skip_pip=True
        assert await async_setup_component(hass,'recorder',{'recorder':{
            'db_url':'sqlite:///'+database.as_posix(),'auto_purge':False,'auto_repack':False,
            'purge_keep_days':2,'commit_interval':0,'include':{'entities':['sensor.pv_bms_probe']}}})
        recorder=get_instance(hass);await hass.async_start();await recorder.async_block_till_done()
        class Probe(SensorEntity):
            _attr_should_poll=False
            _attr_native_value=0
            _attr_force_update=bms['force_update']
            _attr_native_unit_of_measurement=bms['unit_of_measurement']
            _attr_device_class=bms['device_class']
            _attr_state_class=bms['state_class']
            _attr_unique_id='offline_pv_bms_probe'
        sensor=Probe();sensor.entity_id='sensor.pv_bms_probe'
        component=EntityComponent(logging.getLogger(__name__),'sensor',hass)
        await component.async_add_entities([sensor]);await hass.async_block_till_done()
        first=hass.states.get(sensor.entity_id)
        for _ in range(samples):
            sensor.async_write_ha_state();await asyncio.sleep(0)
        last=hass.states.get(sensor.entity_id)
        assert last.last_reported>first.last_reported and last.state==first.state=='0'
        await hass.async_block_till_done();await recorder.async_block_till_done()
        with closing(sqlite3.connect(database)) as db:
            rows=db.execute('SELECT COUNT(*) FROM states').fetchone()[0]
            attrs=db.execute('SELECT COUNT(*) FROM state_attributes').fetchone()[0]
        runtime_wal=Path(str(database)+'-wal').stat().st_size if Path(str(database)+'-wal').exists() else 0
        await hass.async_stop();await asyncio.to_thread(recorder.join,10)
        assert not recorder.is_alive()
        if recorder.engine is not None:recorder.engine.dispose()
        assert rows==samples+1,(rows,samples)
        assert attrs==1,attrs
        size=database.stat().st_size
        # Includes empty HA schema, so this is a conservative fixture size bound.
        assert size<8*1024*1024,size
        report={'scope':'one_existing_BMS_entity_constant_zero_real_HA_Recorder',
            'simulated_hours':72,'configured_seconds':period,'callbacks':samples,
            'state_rows':rows,'attribute_rows':attrs,'database_bytes_after_shutdown':size,
            'wal_bytes_during_run':runtime_wal,
            'wal_bytes_after_shutdown':Path(str(database)+'-wal').stat().st_size if Path(str(database)+'-wal').exists() else 0,
            'maximum_additional_fc04_per_second':1/period,
            'new_entities':0,'history_preserved':True,'physical_cadence_verified':False}
        (output/'RECORDER.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
        print(json.dumps(report,indent=2))

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True)
    asyncio.run(benchmark(p.parse_args().output))
