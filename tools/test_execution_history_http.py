"""Offline access/lifecycle tests for the actual HTTP handler AST.

Recorder SQL is separately exercised read-only on HA; these tests use no HA
installation or external dependencies and never replace the handler logic.
"""
import ast
import asyncio
from functools import partial
import json
from pathlib import Path
import time
from types import SimpleNamespace as NS
import unittest

ROOT = Path(__file__).resolve().parents[1]
tree = ast.parse((ROOT / 'custom_components/hoymiles_hit_modbus/execution_history_http.py').read_text())
handler = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'HoymilesExecutionHistoryView')


class HttpError(Exception):
    def __init__(self, *, text=None): super().__init__(text)


class BadRequest(HttpError): pass
class Forbidden(HttpError): pass
class Busy(HttpError): pass
class View:
    @staticmethod
    def json(value): return value


class Request(dict):
    def __init__(self, hass, entity, allow=True):
        super().__init__(hass_user=NS(permissions=NS(check_entity=lambda _eid, _policy: allow)))
        self.app = {'hass': hass}
        self.query = {'entity_id': entity, 'hours': '999999', 'start': '1900-01-01'}


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    def test_sql_projection_matches_recorded_attribute_names(self):
        fields = next(node for node in tree.body if isinstance(node, ast.Assign)
                      and any(isinstance(target, ast.Name) and target.id == '_DECISION_FIELDS' for target in node.targets))
        decode = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == '_decode_projection')
        env = {'json': json}
        exec(compile(ast.Module(body=[fields, decode], type_ignores=[]), '<real projection>', 'exec'), env)
        values = [None] * len(env['_DECISION_FIELDS'])
        values[1] = 'tariff'; values[2] = 'tariff_battery_charge'
        values[10:17] = ['tx', 'sent', 'confirmed', 'confirmed', 'observed', 'tx', 'tariff_battery_charge']
        values[17:19] = ['tariff', 'required_energy_restore']
        values[23] = [{'policy_id':'rce','reason':'not_allowed'}]
        attrs = env['_decode_projection'](json.dumps(values), 'supervisor', True)
        self.assertEqual(attrs['transaction_evidence']['physical_verification']['action'], attrs['selected_action'])
        self.assertEqual(attrs['candidate_summaries'][0]['reason_code'], 'required_energy_restore')
        self.assertEqual(attrs['rejected_reasons'], values[23])
        self.assertEqual(env['_decode_projection']('null', 'supervisor', True), {})
        self.assertEqual(env['_decode_projection']('{}', 'supervisor', True), {})
        self.assertEqual(env['_decode_projection']('{broken', 'supervisor', False), {})

    def setUp(self):
        self.entities = {}
        self.roles = {'soc': 'overview_battery_soc', 'supervisor': 'ems_supervisor'}
        for entry in ('one', 'two'):
            for role, suffix in self.roles.items():
                eid = f'sensor.{entry}_{role}'
                self.entities[eid] = NS(entity_id=eid, config_entry_id=entry, platform='hoymiles_hit_modbus', unique_id=f'{entry}_{suffix}')
        self.registry = NS(entities=self.entities, async_get=self.entities.get)
        self.hass = NS(config_entries=NS(async_entries=lambda domain: [NS(entry_id='one'), NS(entry_id='two')]))
        self.calls = []
        def read(hass, sources, start, end):
            self.calls.append((sources, start, end))
            return {'missing': [], 'series': {}, 'events': [], 'start': start, 'end': end}
        async def worker(job): return job()
        env = {'HomeAssistantView': View, 'DOMAIN': 'hoymiles_hit_modbus',
            'web': NS(Request=Request, Response=dict, HTTPBadRequest=BadRequest, HTTPForbidden=Forbidden, HTTPServiceUnavailable=Busy),
            'er': NS(async_get=lambda hass: self.registry), '_ROLES': self.roles,
            'get_instance': lambda hass: NS(async_add_executor_job=worker),
            '_read_history': read, 'asyncio': asyncio, 'time': time, 'partial': partial, 'HISTORY_SECONDS':172800}
        exec(compile(ast.Module(body=[handler], type_ignores=[]), '<real history handler>', 'exec'), env)
        self.env = env
        self.view = env['HoymilesExecutionHistoryView']()

    async def test_registry_scope_and_server_owned_window(self):
        result = await self.view.get(Request(self.hass, 'sensor.one_supervisor'))
        self.assertEqual(result['end'] - result['start'], 172800)
        self.assertEqual(set(self.calls[0][0].values()), {'sensor.one_supervisor', 'sensor.one_soc'})
        self.assertIn('load', result['missing'])
        with self.assertRaises(BadRequest):
            await self.view.get(Request(self.hass, 'sensor.two_soc'))
        with self.assertRaises(BadRequest):
            await self.view.get(Request(self.hass, 'sensor.unknown'))

    async def test_permissions_rechecked_before_cached_data(self):
        request = Request(self.hass, 'sensor.one_supervisor')
        await self.view.get(request); await self.view.get(request)
        self.assertEqual(len(self.calls), 1)
        with self.assertRaises(Forbidden):
            await self.view.get(Request(self.hass, 'sensor.one_supervisor', allow=False))
        await self.view.get(Request(self.hass, 'sensor.two_supervisor'))
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(set(self.calls[1][0].values()), {'sensor.two_supervisor', 'sensor.two_soc'})

    async def test_ambiguous_measurement_is_not_first_match(self):
        duplicate = NS(**vars(self.entities['sensor.one_soc'])); duplicate.entity_id='sensor.duplicate'
        self.entities[duplicate.entity_id] = duplicate
        result=await self.view.get(Request(self.hass,'sensor.one_supervisor'))
        self.assertNotIn('soc',self.calls[0][0]); self.assertIn('soc',result['missing'])

    async def test_busy_worker_does_not_start_second_read(self):
        gate=asyncio.Event()
        async def worker(job): await gate.wait(); return job()
        self.env['get_instance']=lambda hass:NS(async_add_executor_job=worker)
        pending=asyncio.create_task(self.view.get(Request(self.hass,'sensor.one_supervisor')))
        await asyncio.sleep(0)
        with self.assertRaises(Busy):
            await self.view.get(Request(self.hass,'sensor.two_supervisor'))
        gate.set(); await pending
        self.assertEqual(len(self.calls),1)

    async def test_reader_limits_fail_without_partial_cache(self):
        def read(*args): raise ValueError('history_row_limit')
        self.env['_read_history']=read
        with self.assertRaises(Busy):
            await self.view.get(Request(self.hass,'sensor.one_supervisor'))
        self.assertIsNone(self.view._cache)

    async def test_query_timeout_is_distinct_and_does_not_cache_partial_data(self):
        def read(*args): raise TimeoutError('recorder_query_deadline')
        self.env['_read_history']=read
        with self.assertRaises(Busy) as result:
            await self.view.get(Request(self.hass,'sensor.one_supervisor'))
        self.assertEqual(str(result.exception), 'history_timeout')
        self.assertIsNone(self.view._cache)


if __name__ == '__main__': unittest.main()
