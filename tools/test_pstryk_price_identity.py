"""Real HA registry/platform identity, including device-name prefixing."""
import logging,sys,tempfile,unittest
from datetime import timedelta
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from homeassistant.helpers.entity_platform import EntityPlatform
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.config_entries import ConfigEntry, ConfigEntries
from homeassistant.core import HomeAssistant
from custom_components.hoymiles_hit_modbus import pstryk_runtime as m

ID='sensor.hoymiles_hit_pstryk_public_net_price'
async def make_hass(path):
    hass=HomeAssistant(str(path))
    hass.config_entries=ConfigEntries(hass,{})
    dr.async_setup(hass);await dr.async_load(hass);await er.async_load(hass)
    return hass

def make_entry(hass):
    entry=ConfigEntry(data={},discovery_keys=MappingProxyType({}),
        domain='hoymiles_hit_modbus',minor_version=1,options={},source='user',
        subentries_data=None,title='test',unique_id='test',version=1)
    hass.config_entries._entries[entry.entry_id]=entry
    return entry

class Tests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.hass=await make_hass(Path(self.temp.name))
        self.entry=make_entry(self.hass);self.registry=er.async_get(self.hass)
    async def asyncTearDown(self):
        await self.hass.async_stop();self.temp.cleanup()
    async def test_real_platform_does_not_add_device_name(self):
        if hasattr(m,'prepare_price_entity'): m.prepare_price_entity(self.hass,self.entry)
        coordinator=SimpleNamespace(entry=self.entry,rce=SimpleNamespace(device_info={
            'identifiers':{('hoymiles_hit_modbus','source')},'name':'Hoymiles Inverter'}),
            cache=SimpleNamespace(view=lambda **kwargs:SimpleNamespace(snapshot=None)),
            start=AsyncMock(),close=AsyncMock())
        entity=m.PstrykPriceSensor(coordinator)
        platform=EntityPlatform(hass=self.hass,logger=logging.getLogger(__name__),domain='sensor',
            platform_name='hoymiles_hit_modbus',platform=None,scan_interval=timedelta(seconds=30),entity_namespace=None)
        platform.config_entry=self.entry
        await platform.async_add_entities([entity]);await self.hass.async_block_till_done()
        try:
            self.assertEqual(entity.entity_id,ID)
            self.assertIsNotNone(self.hass.states.get(ID))
            self.assertEqual(len([e for e in self.registry.entities.values() if e.unique_id.endswith('_pstryk_public_net_price')]),1)
        finally: await platform.async_reset()
    async def test_legacy_prefix_migrates_same_row_and_preserves_user_metadata(self):
        unique=f'{self.entry.entry_id}_pstryk_public_net_price'
        old=self.registry.async_get_or_create('sensor','hoymiles_hit_modbus',unique,
            suggested_object_id='hoymiles_inverter_hoymiles_hit_pstryk_public_net_price',config_entry=self.entry)
        old=self.registry.async_update_entity(old.entity_id,name='Moja cena',icon='mdi:cash')
        m.prepare_price_entity(self.hass,self.entry)
        row=self.registry.async_get(ID)
        self.assertEqual((row.id,row.name,row.icon),(old.id,'Moja cena','mdi:cash'))
        m.prepare_price_entity(self.hass,self.entry)
        self.assertEqual(self.registry.async_get(ID).id,old.id)
    async def test_foreign_canonical_collision_is_preserved(self):
        foreign=self.registry.async_get_or_create('sensor','other','foreign',suggested_object_id=ID.split('.',1)[1])
        with self.assertRaises(ValueError): m.prepare_price_entity(self.hass,self.entry)
        self.assertEqual(self.registry.async_get(ID),foreign)
    async def test_custom_rename_is_preserved(self):
        row=self.registry.async_get_or_create('sensor','hoymiles_hit_modbus',
            f'{self.entry.entry_id}_pstryk_public_net_price',
            suggested_object_id='moja_cena',config_entry=self.entry)
        with self.assertRaises(ValueError): m.prepare_price_entity(self.hass,self.entry)
        self.assertEqual(self.registry.async_get(row.entity_id),row)
        self.assertIsNone(self.registry.async_get(ID))
if __name__=='__main__': unittest.main()
