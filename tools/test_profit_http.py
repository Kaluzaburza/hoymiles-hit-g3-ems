"""Actual authenticated handler with small HA adapters; no network access."""
import ast
from datetime import date
from pathlib import Path
import sqlite3
from types import SimpleNamespace as NS
import unittest

ROOT=Path(__file__).resolve().parents[1]
class Bad(Exception):
    def __init__(self, *,text=None):super().__init__(text)
class Denied(Bad):pass
class Busy(Bad):pass
class Request(dict):
    def __init__(self,hass,query=None,denied=()):
        super().__init__(hass_user=NS(permissions=NS(check_entity=lambda entity,policy:entity not in denied)))
        self.app={"hass":hass};self.query={"entity_id":"sensor.one_supervisor","period":"year","date":"2026-10-06",**(query or {})}

class HTTP(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls=[]
        async def read(*args):self.calls.append(args);return {"ok":True}
        self.manager=NS(ready=True,sources={"grid":"sensor.one_grid"},read=read)
        domain="hoymiles_hit_modbus"
        hass=NS(data={domain:{"one":NS(profits=self.manager)}});self.hass=hass
        row=NS(entity_id="sensor.one_supervisor",config_entry_id="one",platform=domain,unique_id="one_ems_supervisor")
        env={"HomeAssistantView":type("View",(),{"json":staticmethod(lambda x:x)}),"date":date,"sqlite3":sqlite3,
             "DOMAIN":domain,"er":NS(async_get=lambda _:NS(async_get=lambda entity:row if entity==row.entity_id else None)),
             "web":NS(HTTPBadRequest=Bad,HTTPForbidden=Denied,HTTPServiceUnavailable=Busy)}
        tree=ast.parse((ROOT/"custom_components/hoymiles_hit_modbus/profit_http.py").read_text())
        exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.ClassDef)],type_ignores=[]),"profit_http.py","exec"),env)
        self.view=env["HoymilesProfitView"]()
    async def test_scoped_and_authenticated(self):
        self.assertTrue(self.view.requires_auth)
        self.assertEqual(await self.view.get(Request(self.hass)),{"ok":True})
        self.assertEqual(self.calls,[("year","2026-10-06",0)])
        for query in [{"entity_id":"sensor.two_supervisor"},{"period":"decade"},{"date":"1800-01-01"},{"date":"bad"},{"tariff_offset":"-1"},{"tariff_offset":"100001"}]:
            with self.assertRaises(Bad):await self.view.get(Request(self.hass,query))
        self.assertEqual(len(self.calls),1)
    async def test_permissions_before_cache_including_grid(self):
        for entity in ("sensor.one_supervisor","sensor.one_grid"):
            with self.assertRaises(Denied):await self.view.get(Request(self.hass,denied=(entity,)))
        request=Request(self.hass);request.pop("hass_user")
        with self.assertRaises(Denied):await self.view.get(request)
        self.assertFalse(self.calls)
    async def test_read_failure_and_offline_do_not_fabricate_zero(self):
        async def failure(*args):raise sqlite3.OperationalError("locked")
        self.manager.read=failure
        with self.assertRaises(Busy):await self.view.get(Request(self.hass))
        self.manager.ready=False
        with self.assertRaises(Busy):await self.view.get(Request(self.hass))

if __name__=="__main__":unittest.main(verbosity=2)
