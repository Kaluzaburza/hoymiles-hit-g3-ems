"""Legacy and current-base comparisons using isolated HA Recorder."""
import asyncio
import ast
import copy
from datetime import datetime, timezone
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from rc2_regression_baseline import read_source

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
for name, path in [('custom_components',ROOT/'custom_components'),('custom_components.hoymiles_hit_modbus',ROOT/'custom_components/hoymiles_hit_modbus')]:
    package=types.ModuleType(name); package.__path__=[str(path)]; sys.modules[name]=package
history=importlib.import_module('custom_components.hoymiles_hit_modbus.execution_history')
diagnostics=importlib.import_module('custom_components.hoymiles_hit_modbus.diagnostics')
http=importlib.import_module('custom_components.hoymiles_hit_modbus.execution_history_http')
spec=importlib.util.spec_from_file_location('baseline',ROOT/'tools/fixtures/recorder_119b_baseline.py')
baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
base_v2_spec=importlib.util.spec_from_file_location(
    'base_v2', ROOT/'tools/fixtures/recorder_6484a82_baseline.py'
)
base_v2=importlib.util.module_from_spec(base_v2_spec);base_v2_spec.loader.exec_module(base_v2)

def evidence(at):
    iso=lambda t:datetime.fromtimestamp(t,timezone.utc).isoformat()
    return {'execution_phase':'executing','selected_policy':'tariff','selected_action':'tariff_battery_charge','owner':'tariff','reason':'executing','lifecycle_reason':'executing','selection_reason':'selected','execution_blocked_reason':None,'transaction_id':'tx','transaction_evidence_scope':'active','rejected_reasons':[], 'candidate_summaries':[{'policy_id':'tariff','reason_code':'eligible'}], 'execution_last_valid_read_at':iso(at), 'transaction_evidence':{'transaction_id':'tx','command_sent_at':iso(at-2),'readback_result':'confirmed','physical_verification':{'transaction_id':'tx','action':'tariff_battery_charge','status':'confirmed','observed_at':iso(at-1)}}}

class RecorderTest(unittest.TestCase):
    def test_frozen_v2_fixture_matches_exact_canonical_base(self):
        source = read_source("6484a82", "execution_history.py").decode("utf-8")
        fixture = (ROOT / "tools/fixtures/recorder_6484a82_baseline.py").read_text(
            encoding="utf-8"
        )
        def nodes(text):
            return {
                node.name if isinstance(node, ast.FunctionDef) else node.targets[0].id: node
                for node in ast.parse(text).body
                if (isinstance(node, ast.FunctionDef)
                    and node.name == "supervisor_recorder_projection")
                or (isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name)
                            and target.id == "_RECORDED_DECISION_KEYS"
                            for target in node.targets))
            }
        frozen = nodes(fixture)
        canonical = nodes(source)
        self.assertEqual(set(frozen), {"_RECORDED_DECISION_KEYS",
                                      "supervisor_recorder_projection"})
        for key in frozen:
            self.assertEqual(ast.dump(frozen[key]), ast.dump(canonical[key]), key)

    def test_current_v2_vs_v3_tariff_provenance_in_actual_recorder(self):
        from homeassistant import config_entries, loader
        from homeassistant.components import recorder
        from homeassistant.components.recorder import get_instance
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import recorder as recorder_helper
        from homeassistant.setup import async_setup_component

        async def run():
            with tempfile.TemporaryDirectory(prefix="stor01-6484a82-") as folder:
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
                base = time.time()
                previous = None
                try:
                    for tick in range(120):
                        at = base + tick
                        attrs = evidence(base - 2)
                        attrs["tariff_decision"] = {
                            "schema_version": 1,
                            "source_frame": {
                                "observed_at": datetime.fromtimestamp(
                                    at, timezone.utc
                                ).isoformat(),
                                "input_revision": tick + 1,
                                "power_cohort_generation": tick + 1,
                            },
                            "action": "charge", "result_current": True,
                            "recalculation_pending": False,
                            "target_soc_percent": 60.0,
                            "requested_target_energy_kwh": 5.0,
                            "start_eligible": True,
                        }
                        current = history.supervisor_recorder_projection(
                            attrs, previous=previous
                        )
                        previous = current
                        old = base_v2.supervisor_recorder_projection(attrs)
                        self.assertEqual(
                            history.decision_evidence("executing", old, at),
                            history.decision_evidence("executing", current, at),
                        )
                        for entity, projection in (("before", old), ("after", current)):
                            live = copy.deepcopy(attrs)
                            live["recorded_execution"] = projection
                            hass.states.async_set(
                                f"sensor.{entity}", "executing", live,
                                state_info={
                                    "unrecorded_attributes":
                                    history.SUPERVISOR_UNRECORDED_ATTRIBUTES
                                }, timestamp=at,
                            )
                        before_live = dict(hass.states.get("sensor.before").attributes)
                        after_live = dict(hass.states.get("sensor.after").attributes)
                        before_live.pop("recorded_execution")
                        after_live.pop("recorded_execution")
                        self.assertEqual(before_live, after_live)
                    await instance.async_block_till_done()
                    v3_endpoint = await instance.async_add_executor_job(
                        http._read_history, hass,
                        {"supervisor": "sensor.after"}, base, base + 121,
                    )
                    v2_endpoint = await instance.async_add_executor_job(
                        http._read_history, hass,
                        {"supervisor": "sensor.before"}, base, base + 121,
                    )
                    self.assertTrue(v3_endpoint["events"])
                    self.assertTrue(v3_endpoint["executions"])
                    self.assertEqual(v2_endpoint["events"], v3_endpoint["events"])
                    self.assertEqual(v2_endpoint["executions"],
                                     v3_endpoint["executions"])
                    self.assertEqual(v2_endpoint["summary"],
                                     v3_endpoint["summary"])
                    mixed_attrs = evidence(base + 202)
                    mixed_attrs["tariff_decision"] = {
                        "schema_version": 1,
                        "source_frame": {"observed_at": datetime.fromtimestamp(
                            base + 202, timezone.utc
                        ).isoformat(), "input_revision": 1},
                        "action": "charge", "result_current": True,
                    }
                    v2 = base_v2.supervisor_recorder_projection(mixed_attrs)
                    v3 = history.supervisor_recorder_projection(mixed_attrs)
                    for offset, projection in enumerate((None, {**v2, "schema_version": 1},
                                                         v2, v3)):
                        live = copy.deepcopy(mixed_attrs)
                        if projection is not None:
                            live["recorded_execution"] = projection
                        hass.states.async_set(
                            "sensor.mixed", "executing", live,
                            state_info={"unrecorded_attributes": (
                                frozenset() if projection is None else
                                history.SUPERVISOR_UNRECORDED_ATTRIBUTES
                            )}, timestamp=base + 201 + offset,
                        )
                    unknown = copy.deepcopy(mixed_attrs)
                    unknown["recorded_execution"] = {**v3, "schema_version": 99}
                    hass.states.async_set(
                        "sensor.unknown", "executing", unknown,
                        state_info={"unrecorded_attributes":
                                    history.SUPERVISOR_UNRECORDED_ATTRIBUTES},
                        timestamp=base + 205,
                    )
                    await instance.async_block_till_done()
                    mixed_endpoint = await instance.async_add_executor_job(
                        http._read_history, hass,
                        {"supervisor": "sensor.mixed"}, base + 200, base + 206,
                    )
                    unknown_endpoint = await instance.async_add_executor_job(
                        http._read_history, hass,
                        {"supervisor": "sensor.unknown"}, base + 200, base + 206,
                    )
                    self.assertTrue(mixed_endpoint["events"])
                    self.assertTrue(mixed_endpoint["executions"])
                    self.assertFalse(unknown_endpoint["executions"])
                    accounting = importlib.import_module(
                        "custom_components.hoymiles_hit_modbus.supervisor_accounting_sensor"
                    )
                    sources = [
                        {"field": field, "source": f"sensor.{field}",
                         "reported_at": datetime.fromtimestamp(
                             base + 210, timezone.utc
                         ).isoformat().replace("+00:00", "Z"),
                         "status": "fresh"}
                        for field in ("mode_readback", "readback_generation",
                                      "grid_to_battery_power_w", "grid_power_w")
                    ]
                    for index, accepted in enumerate((False, True, False)):
                        proof = {
                            "schema_version": 1,
                            "observed_at": datetime.fromtimestamp(
                                base + 210 + index, timezone.utc
                            ).isoformat().replace("+00:00", "Z"),
                            "evidence_fingerprint": f"{index + 1:064x}",
                            "previous_anchor_fingerprint": (
                                f"{index:064x}" if index else None
                            ),
                            "reason": "accumulated" if accepted else "seeded",
                            "accepted": accepted,
                            "interval_energy_kwh": 0.01 if accepted else 0.0,
                            "accepted_interval_count": 1 if index else 0,
                            "rejected_interval_count": 1 if index == 2 else 0,
                            "provenance": sources,
                        }
                        accounting._validate_transition_proof(proof)
                        hass.states.async_set(
                            "sensor.accounting", str(0.01 if index else 0.0),
                            {"state_class": "total_increasing",
                             "unit_of_measurement": "kWh",
                             "accepted_interval_count": proof["accepted_interval_count"],
                             "rejected_interval_count": proof["rejected_interval_count"],
                             "last_transition_proof": proof,
                             "current_evidence_fingerprint": f"{index + 1:064x}",
                             "current_provenance": sources},
                            state_info={"unrecorded_attributes":
                                accounting.HoymilesSupervisorAccountingV2Sensor.
                                _unrecorded_attributes},
                            timestamp=base + 210 + index,
                        )
                    await instance.async_block_till_done()
                    with sqlite3.connect(db) as connection:
                        accounting_rows = connection.execute("""
                            SELECT a.shared_attrs FROM states s
                            JOIN states_meta sm ON sm.metadata_id=s.metadata_id
                            JOIN state_attributes a ON a.attributes_id=s.attributes_id
                            WHERE sm.entity_id='sensor.accounting'
                            ORDER BY s.state_id
                        """).fetchall()
                        self.assertEqual(len(accounting_rows), 3)
                        proofs = [json.loads(row[0]) for row in accounting_rows]
                        self.assertEqual(
                            [row["last_transition_proof"]["accepted"] for row in proofs],
                            [False, True, False],
                        )
                        self.assertTrue(all("current_provenance" not in row
                                            for row in proofs))
                        self.assertEqual(proofs[1]["last_transition_proof"]
                                         ["provenance"][0]["source"],
                                         "sensor.mode_readback")
                        rows = connection.execute("""
                            SELECT sm.entity_id, COUNT(*), COUNT(DISTINCT s.attributes_id),
                                   SUM(LENGTH(a.shared_attrs))
                            FROM states s JOIN states_meta sm ON sm.metadata_id=s.metadata_id
                            JOIN state_attributes a ON a.attributes_id=s.attributes_id
                            WHERE sm.entity_id IN ('sensor.before','sensor.after')
                            GROUP BY sm.entity_id
                        """).fetchall()
                        unique_bytes = {
                            entity: connection.execute("""
                                SELECT SUM(LENGTH(a.shared_attrs))
                                FROM state_attributes a
                                WHERE a.attributes_id IN (
                                    SELECT DISTINCT s.attributes_id FROM states s
                                    JOIN states_meta sm ON sm.metadata_id=s.metadata_id
                                    WHERE sm.entity_id=?
                                )
                            """, (f"sensor.{entity}",)).fetchone()[0]
                            for entity in ("before", "after")
                        }
                    connection.close()
                    measured = {name: {"rows": count, "unique_attributes": unique,
                                       "referenced_attribute_bytes": size,
                                       "unique_attribute_bytes": unique_bytes[name[7:]]}
                                for name, count, unique, size in rows}
                    print("MEASUREMENT_STOR01_V3 " + json.dumps(measured), flush=True)
                    self.assertEqual(measured["sensor.before"]["rows"], 120)
                    self.assertEqual(measured["sensor.after"]["rows"], 120)
                    self.assertEqual(measured["sensor.before"]["unique_attributes"], 120)
                    self.assertEqual(measured["sensor.after"]["unique_attributes"], 1)
                    self.assertLess(
                        measured["sensor.after"]["unique_attribute_bytes"],
                        measured["sensor.before"]["unique_attribute_bytes"],
                    )
                finally:
                    await hass.async_stop()
                    await asyncio.to_thread(instance.join, 5)
                    if instance.engine is not None:
                        instance.engine.dispose()

        asyncio.run(run())

    def test_rcm_raw_inputs_stay_live_without_repeating_plan_attributes(self):
        from homeassistant.components.recorder.db_schema import StateAttributes
        from homeassistant.core import Event, State
        from sqlalchemy.dialects import sqlite

        rcm = importlib.import_module("custom_components.hoymiles_hit_modbus.rcm_sensor")
        excluded = rcm.HoymilesRCMOptimizerSensor._unrecorded_attributes
        recorded = []
        for voltage, surplus in ((240.0, 1.0), (241.0, 1.2)):
            attrs = {
                "voltage_l1_v": voltage,
                "voltage_l2_v": 243.0,
                "voltage_l3_v": 242.0,
                "filtered_voltage_v": voltage,
                "pv_surplus_power_kw": surplus,
                "maximum_voltage_v": 243.0,
                "rolling_10m_voltage_v": 241.5,
                "status_code": "ready",
                "action": "monitor",
                "result_current": True,
            }
            state = State("sensor.hoymiles_hit_rcm_voltage_plan", "ready", attrs,
                          state_info={"unrecorded_attributes": excluded})
            self.assertEqual(state.attributes["voltage_l1_v"], voltage)
            event = Event("state_changed", {"entity_id": state.entity_id,
                                            "old_state": None, "new_state": state})
            recorded.append(json.loads(StateAttributes.shared_attrs_bytes_from_event(
                event, sqlite.dialect()
            )))
        self.assertEqual(recorded[0], recorded[1])
        self.assertEqual(recorded[0]["maximum_voltage_v"], 243.0)
        self.assertEqual(recorded[0]["status_code"], "ready")

    def test_accounting_evidence_changes_keep_live_values_without_new_attributes(self):
        from homeassistant.components.recorder.db_schema import StateAttributes
        from homeassistant.core import Event, State
        from sqlalchemy.dialects import sqlite

        accounting = importlib.import_module(
            "custom_components.hoymiles_hit_modbus.supervisor_accounting_sensor"
        )
        excluded = accounting.HoymilesSupervisorAccountingV2Sensor._unrecorded_attributes
        samples = []
        for tick in range(2):
            attrs = {
                "state_class": "total_increasing",
                "unit_of_measurement": "kWh",
                "storage_epoch": 2,
                "accepted_interval_count": 3,
                "rejected_interval_count": 1,
                "current_entry_qualified": True,
                "current_evidence_fingerprint": f"frame-{tick}",
                "current_provenance": {"observed_at": f"frame-{tick}"},
            }
            state = State(
                "sensor.hoymiles_hit_ems_supervisor_grid_to_battery_energy_v2",
                "1.25", attrs, state_info={"unrecorded_attributes": excluded},
            )
            self.assertEqual(state.attributes["current_evidence_fingerprint"], f"frame-{tick}")
            event = Event("state_changed", {"entity_id": state.entity_id,
                                            "old_state": None, "new_state": state})
            samples.append(json.loads(StateAttributes.shared_attrs_bytes_from_event(
                event, sqlite.dialect()
            )))
        self.assertEqual(samples[0], samples[1])
        self.assertNotIn("current_provenance", samples[0])
        self.assertEqual(samples[0]["accepted_interval_count"], 3)

    def test_48h_decisions_and_support_projection(self):
        from homeassistant.components.recorder.db_schema import StateAttributes
        from homeassistant.core import State, Event
        from sqlalchemy.dialects import sqlite
        start=time.time()-48*3600
        original=[]; recorded=[]
        for index in range(576):
            at=start+index*300
            attrs=evidence(at)
            if index%20==0: attrs['transaction_evidence_scope']='last'
            original.append((at,'executing',attrs))
            attrs['recorded_execution']=history.supervisor_recorder_projection(attrs)
            state=State('sensor.hoymiles_hit_ems_supervisor','executing',attrs,state_info={'unrecorded_attributes':history.SUPERVISOR_UNRECORDED_ATTRIBUTES})
            event=Event('state_changed',{'entity_id':state.entity_id,'old_state':None,'new_state':state})
            stored=json.loads(StateAttributes.shared_attrs_bytes_from_event(event,sqlite.dialect()))
            self.assertNotIn('selected_policy',stored)
            live_diagnostic = diagnostics._history_attributes(state.entity_id, attrs)
            stored_diagnostic = diagnostics._history_attributes(state.entity_id, stored)
            for key in ("execution_phase", "selected_policy", "selected_action",
                        "owner", "transaction_id", "transaction_evidence_scope"):
                self.assertEqual(live_diagnostic.get(key), stored_diagnostic.get(key), key)
            self.assertEqual(history.decision_evidence('executing',attrs,at),history.decision_evidence('executing',stored,at))
            recorded.append((at,'executing',stored))
        self.assertEqual(history.decision_intervals(original,start,start+48*3600),history.decision_intervals(recorded,start,start+48*3600))

    def test_actual_recorder_all_live_reports_and_smaller_attributes(self):
        from homeassistant import config_entries, loader
        from homeassistant.components import recorder
        from homeassistant.components.recorder import get_instance
        from homeassistant.core import HomeAssistant
        from homeassistant.helpers import recorder as recorder_helper
        from homeassistant.setup import async_setup_component
        async def run():
            with tempfile.TemporaryDirectory(prefix='rw01-119b-') as folder:
                db=Path(folder)/'recorder.db'
                hass=HomeAssistant(folder);loader.async_setup(hass)
                hass.config_entries=config_entries.ConfigEntries(hass,{})
                recorder_helper.async_initialize_recorder(hass)
                config=recorder.CONFIG_SCHEMA({'recorder':{'db_url':f'sqlite:///{db.as_posix()}','auto_purge':False,'auto_repack':False}})
                self.assertTrue(await async_setup_component(hass,'recorder',config))
                await hass.async_start();instance=get_instance(hass);base=time.time()
                try:
                    for index in range(224):
                        at=base+index*300/224;attrs=evidence(at)
                        attrs['recorded_execution']=history.supervisor_recorder_projection(attrs)
                        for name,excluded in [('before',baseline.SUPERVISOR_UNRECORDED_ATTRIBUTES),('after',history.SUPERVISOR_UNRECORDED_ATTRIBUTES)]:
                            hass.states.async_set('sensor.'+name,'executing',copy.deepcopy(attrs),state_info={'unrecorded_attributes':excluded},timestamp=at)
                        # Both live states contain the full fresh proof at every tick.
                        self.assertEqual(dict(hass.states.get('sensor.before').attributes),dict(hass.states.get('sensor.after').attributes))
                    await instance.async_block_till_done()
                    endpoint=await instance.async_add_executor_job(http._read_history,hass,{'supervisor':'sensor.after'},base,base+300)
                    with sqlite3.connect(db) as connection:
                        rows=connection.execute('SELECT sm.entity_id,COUNT(*),SUM(LENGTH(a.shared_attrs)) FROM states s JOIN states_meta sm ON sm.metadata_id=s.metadata_id JOIN state_attributes a ON a.attributes_id=s.attributes_id WHERE sm.entity_id IN (\'sensor.before\',\'sensor.after\') GROUP BY sm.entity_id').fetchall()
                    result={name:{'rows':count,'referenced_attribute_bytes':size} for name,count,size in rows}
                    connection.close()
                    result['reduction_percent']=round(100*(1-result['sensor.after']['referenced_attribute_bytes']/result['sensor.before']['referenced_attribute_bytes']),2)
                    print('MEASUREMENT_119B '+json.dumps(result),flush=True)
                    self.assertEqual(result['sensor.before']['rows'],224);self.assertEqual(result['sensor.after']['rows'],224)
                    self.assertLess(result['sensor.after']['referenced_attribute_bytes'],result['sensor.before']['referenced_attribute_bytes'])
                    self.assertTrue(endpoint['events']);self.assertTrue(endpoint['executions'])
                finally:
                    await hass.async_stop();await asyncio.to_thread(instance.join,5)
                    if instance.engine is not None:instance.engine.dispose()
        asyncio.run(run())

if __name__=='__main__':unittest.main()
