"""Economic evidence regressions: measured cash, attribution, DST and durability."""
from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
from types import ModuleType, SimpleNamespace as NS
import unittest
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components/hoymiles_hit_modbus"
package = ModuleType("profit_test_package")
package.__path__ = [str(COMPONENT)]
sys.modules[package.__name__] = package
A = importlib.import_module("profit_test_package.profit_archive")
B = importlib.import_module("profit_test_package.baseline_energy_timeline")
H = importlib.import_module("profit_test_package.execution_history")
E = importlib.import_module("profit_test_package.energy_data")

# Execute the real runtime definitions with HA adapter names replaced, not the
# pricing/classification implementation. No HA/device service can be called.
source = ast.parse((COMPONENT / "profit_runtime.py").read_text(encoding="utf-8"))
runtime_ns = dict(asyncio=asyncio, timedelta=timedelta, time=time, uuid=uuid,
    ZoneInfo=ZoneInfo, logging=logging, callback=lambda f:f, DOMAIN="hoymiles_hit_modbus",
    BaselineEnergyInputs=B.BaselineEnergyInputs, numeric_state_sample=E.numeric_state_sample,
    decision_evidence=H.decision_evidence, timestamp=H.timestamp,
    ProfitAccumulator=A.ProfitAccumulator, ProfitArchive=A.ProfitArchive,
    Observation=A.Observation, Rate=A.Rate, finite=A.finite)
exec(compile(ast.Module(body=[n for n in source.body if not isinstance(n, (ast.Import,ast.ImportFrom))],type_ignores=[]), "profit_runtime.py", "exec"), runtime_ns)
Runtime = runtime_ns["ProfitRuntime"]
net = runtime_ns["net_import_rate"]
category = runtime_ns["category_from_state"]
UTC = timezone.utc
T = datetime(2026,10,6,10,tzinfo=UTC).timestamp()

def model(soc=50, **updates):
    values = dict(generated_at=datetime.fromtimestamp(T,UTC), config_entry_id="test",
        shared_inputs_revision=1,current_soc_percent=soc,battery_capacity_kwh=10,
        reserve_soc_percent=20,hardware_minimum_soc_percent=10,hardware_maximum_soc_percent=100,
        pv_to_battery_efficiency=1,battery_to_home_efficiency=1,maximum_charge_power_kw=10,
        maximum_discharge_power_kw=10,system_ac_power_kw=10,zero_export_confirmed=False,
        export_allowed=True,sources={},provenance={})
    return B.BaselineEnergyInputs(**(values | updates))

def rate(value=1, start=T-3600, end=T+86400, **changes):
    return A.Rate(start,end,value,changes.get("source","manual_G12w"),changes.get("zone","low"),"manual_net")

def obs(at, grid=-3, **updates):
    return A.Observation(**(dict(at=at,grid_kw=grid,category="self_use",buy=(rate(),),sell=(rate(.5),),context="entry:device") | updates))

def total(acc, field):
    return sum(r[field] for r in acc.rows.values())

def confirmed(policy="rce", action="rce_export", now=T):
    stamp=lambda t:datetime.fromtimestamp(t,UTC).isoformat()
    return NS(state="executing",attributes={"execution_phase":"executing", "owner":policy,
        "selected_policy":policy,"selected_action":action,"transaction_id":"tx1",
        "transaction_evidence_scope":"active", "execution_last_valid_read_at":stamp(now-1),
        "transaction_evidence":{"transaction_id":"tx1","command_sent_at":stamp(now-100),
            "readback_result":"confirmed", "physical_verification":{"status":"confirmed",
                "transaction_id":"tx1","action":action,"observed_at":stamp(now-90)}}})

class Economics(unittest.TestCase):
    def test_month_flow_groups_use_all_prices_and_keep_missing_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = A.ProfitArchive(Path(directory)/"profits.sqlite", {"entry":"flows", "timezone":"Europe/Warsaw"})
            archive.initialize(T)
            a = A.ProfitAccumulator("Europe/Warsaw")
            # 30 independent intervals: more than the UI's 24-row price page.
            for i in range(30):
                at = T + i*3600
                a.previous = None
                purchase = (rate(.5 if i < 20 else 1, start=at, end=at+3600,
                                 source="official:PGE:G12", zone="low" if i < 20 else "peak"),)
                a.observe(obs(at, grid=-120, buy=purchase))
                a.observe(obs(at+30, grid=-120, buy=purchase))
            for i, value in enumerate([-.2, 0, 1]):
                at = T + (40+i)*3600
                a.previous = None
                sale = (rate(value, start=at, end=at+3600, source="pstryk"),)
                a.observe(obs(at, grid=120, sell=sale)); a.observe(obs(at+30, grid=120, sell=sale))
            at = T+50*3600
            a.previous = None
            a.observe(obs(at, grid=-120, buy=())); a.observe(obs(at+30, grid=-120, buy=()))
            archive.write(a.drain(), "grouped", at+30)
            data = archive.read("month", "2026-10-07", at+60)
            groups = data["purchases"]
            low = next(r for r in groups if r["zone"] == "low")
            peak = next(r for r in groups if r["zone"] == "peak")
            unknown = next(r for r in groups if r["source"] == "unavailable")
            self.assertEqual((low["energy_kwh"], low["amount_pln"], low["average_price"]), (20, 10, .5))
            self.assertEqual((peak["energy_kwh"], peak["amount_pln"]), (10, 10))
            self.assertEqual(unknown["unpriced_kwh"], 1)
            self.assertIsNone(unknown["amount_pln"])
            self.assertIsNone(unknown["average_price"])
            self.assertAlmostEqual(sum(r["energy_kwh"] for r in groups), data["totals"]["import_kwh"])
            sale = next(r for r in data["sales"] if r["source"] == "pstryk")
            self.assertEqual((sale["energy_kwh"], sale["amount_pln"]), (3, .8))
            self.assertAlmostEqual(sale["average_price"], .8/3, places=6)
            self.assertEqual((sale["minimum_price"], sale["maximum_price"]), (-.2, 1))

    def test_fragment_comparison_is_not_a_whole_day_benefit(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=A.ProfitArchive(Path(directory)/"profits.sqlite", {"entry":"fragment", "timezone":"Europe/Warsaw"})
            archive.initialize(T)
            a=A.ProfitAccumulator("Europe/Warsaw")
            for i in range(121):
                a.observe(obs(T+i*30, category="rce_export", grid=3,
                    model=model(80-i*.25) if i<9 else None, pv_kw=0, load_kw=0))
            archive.write(a.drain(), "fragment", T+3600)
            totals=archive.read("day", "2026-10-06", T+3600)["totals"]
            self.assertEqual(totals["benefit_status"], "partial")
            self.assertAlmostEqual(totals["model_coverage_percent"], 240/3600*100, places=5)
            self.assertEqual(totals["model_seconds"], 240)

    def test_known_cash_and_attribution(self):
        a=A.ProfitAccumulator("Europe/Warsaw")
        for i in range(121): a.observe(obs(T+i*30))
        self.assertAlmostEqual(total(a,"import_kwh"),3)
        self.assertAlmostEqual(total(a,"import_cost"),3)
        self.assertEqual(total(a,"model_seconds"),0)
        self.assertEqual({r["category"] for r in a.rows.values()},{"self_use"})

    def test_price_boundary_freeze_negative_and_zero(self):
        a=A.ProfitAccumulator("Europe/Warsaw")
        rates=(rate(2,end=T+10),rate(-1,start=T+10,end=T+20),rate(0,start=T+20))
        a.observe(obs(T,grid=36,sell=rates))
        a.observe(obs(T+30,grid=36,sell=(rate(99),)))
        self.assertAlmostEqual(total(a,"export_revenue"),.1)
        self.assertAlmostEqual(total(a,"export_kwh"),.3)
        self.assertEqual(len(a.rows),3)
        self.assertEqual({r["meta"]["sell_net"] for r in a.rows.values()},{2,-1,0})

    def test_missing_overlapping_nan_price_is_not_zero(self):
        for rows in [(),(rate(float("nan")),),(rate(),rate(2))]:
            a=A.ProfitAccumulator("Europe/Warsaw")
            a.observe(obs(T,buy=rows));a.observe(obs(T+30,buy=rows))
            self.assertAlmostEqual(total(a,"import_kwh"),.025)
            self.assertEqual(total(a,"priced_import_kwh"),0)
            self.assertEqual(total(a,"buy_covered_seconds"),0)

    def test_outage_duplicate_order_context_restart(self):
        a=A.ProfitAccumulator("Europe/Warsaw")
        a.observe(obs(T));a.observe(obs(T+30));a.observe(obs(T+30));a.observe(obs(T+20))
        self.assertEqual(total(a,"covered_seconds"),30)
        a.observe(obs(T+200));a.observe(obs(T+230,context="other"))
        self.assertEqual(total(a,"covered_seconds"),30)
        self.assertEqual(sum(r["seconds"] for r in a.rows.values() if r["category"]=="gap"),200)
        a=A.ProfitAccumulator("Europe/Warsaw");a.observe(obs(T+400))
        self.assertFalse(a.rows)

    def test_battery_depletion_is_not_free_profit(self):
        # 3 kW exported for one hour, battery loses 3 kWh. Baseline idle.
        a=A.ProfitAccumulator("Europe/Warsaw")
        for i in range(121):
            a.observe(obs(T+i*30,grid=3,category="rce_export",model=model(80-i*.25),pv_kw=0,load_kw=0))
        self.assertAlmostEqual(total(a,"export_revenue"),1.5)
        self.assertAlmostEqual(total(a,"model_cash_delta"),1.5)
        self.assertAlmostEqual(total(a,"inventory_delta"),-3)
        self.assertAlmostEqual(total(a,"model_cash_delta")+total(a,"inventory_delta"),-1.5)

    def test_tariff_arbitrage_with_stock_and_following_self_use(self):
        # Buy 3 kWh at 1, then avoid buying 3 kWh at 2. Empty baseline battery.
        a=A.ProfitAccumulator("Europe/Warsaw")
        for i in range(121):
            a.observe(obs(T+i*30,grid=-3 if i<120 else 0,category="tariff_charge" if i<120 else "self_use",
                model=model(20+i*.25),pv_kw=0,load_kw=0 if i<120 else 3,buy=(rate(1 if i<120 else 2),)))
        self.assertAlmostEqual(total(a,"model_cash_delta")+total(a,"inventory_delta"),0,places=4)
        for i in range(1,121):
            a.observe(obs(T+3600+i*30,grid=0,category="self_use",model=model(50-i*.25),pv_kw=0,load_kw=3,buy=(rate(2),)))
        self.assertAlmostEqual(total(a,"model_cash_delta"),3,places=4)
        self.assertAlmostEqual(total(a,"inventory_delta"),0,places=4)

    def test_pv_delay_counts_opportunity_cost_and_model_gaps(self):
        a=A.ProfitAccumulator("Europe/Warsaw")
        for i in range(121):
            a.observe(obs(T+i*30,grid=3,category="pv_delay",model=model(50),pv_kw=4,load_kw=1))
        self.assertAlmostEqual(total(a,"model_cash_delta"),1.5,places=4)
        self.assertAlmostEqual(total(a,"inventory_delta"),-3,places=4)
        old=total(a,"model_seconds")
        a.observe(obs(T+3630,grid=3,category="pv_delay",model=model(50,battery_capacity_kwh=20),pv_kw=4,load_kw=1))
        self.assertEqual(total(a,"model_seconds"),old)
        a.observe(obs(T+3660,grid=3,category="pv_delay",model=None,pv_kw=4,load_kw=1))
        self.assertEqual(total(a,"model_seconds"),old)

    def test_calendar_dst_week_and_year(self):
        for day,hours in [("2026-03-29",23),("2026-10-25",25)]:
            _,_,start,end=A.period_bounds("day",day,"Europe/Warsaw")
            self.assertEqual(end-start,hours*3600)
        start,end,_,_=A.period_bounds("week","2027-01-01","Europe/Warsaw")
        self.assertEqual((str(start),str(end)),("2026-12-28","2027-01-04"))
        self.assertEqual(A.period_bounds("year","2024-10-02","Europe/Warsaw")[3]-A.period_bounds("year","2024-10-02","Europe/Warsaw")[2],366*86400)

    def test_archive_commit_retry_frozen_prices_and_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=A.ProfitArchive(Path(directory)/"profits.sqlite",{"entry":"one","timezone":"Europe/Warsaw"})
            archive.initialize(T)
            a=A.ProfitAccumulator("Europe/Warsaw");a.observe(obs(T));a.observe(obs(T+30))
            rows=a.drain();archive.write(rows,"batch1",T+30);archive.write(rows,"batch1",T+60)
            archive.initialize(T+500)
            data=archive.read("day","2026-10-06",T+600)
            self.assertAlmostEqual(data["totals"]["import_cost"],.025)
            self.assertEqual(len(data["series"]),24)
            self.assertEqual(sum(r["cash_balance"] is None for r in data["series"]),23)
            self.assertEqual(data["started_at"],T)
            self.assertFalse(data["historical_backfill"])
            with self.assertRaises(ValueError): A.ProfitArchive(archive.path,{"entry":"two","timezone":"Europe/Warsaw"}).initialize(T)
            # Roll back whole batch when any row fails, then retry same ID.
            bad=dict(rows[0],id="bad",import_cost=float("nan"))
            with self.assertRaises(ValueError):archive.write([*rows,bad],"batch2",T+60)
            self.assertAlmostEqual(archive.read("day","2026-10-06",T+600)["totals"]["import_cost"],.025)
            archive.write(rows,"batch2",T+60)
            self.assertAlmostEqual(archive.read("day","2026-10-06",T+600)["totals"]["import_cost"],.05)

    def test_dst_repeated_hours_preserve_offset(self):
        a=A.ProfitAccumulator("Europe/Warsaw")
        start=datetime(2026,10,25,0,tzinfo=UTC).timestamp()
        for hour in (0,1):
            a.previous=None
            a.observe(obs(start+hour*3600));a.observe(obs(start+hour*3600+30))
        self.assertEqual({r["meta"]["local_hour"] for r in a.rows.values()},{2})
        self.assertEqual({r["meta"]["utc_offset_minutes"] for r in a.rows.values()},{60,120})

    def test_net_prices_and_missing_basis(self):
        self.assertEqual(net(-.3,"public_net_same_buy_sell","Net")[0],-.3)
        self.assertEqual(net(1.23,"marginal_gross_import_cost_including_energy","Net")[0],1)
        basis="configured_marginal_import_cost_component_completeness_unverified"
        self.assertEqual(net(.9,basis,"Net")[0],.9)
        self.assertIsNone(net(.9,basis,"Unspecified")[0])
        self.assertEqual(net(1.23,basis,"Gross VAT 23%")[0],1)

    def test_confirmed_not_planned_or_historical_attribution(self):
        self.assertEqual(category(confirmed(),5,T),"rce_export")
        self.assertEqual(category(confirmed(action="pv_charge_hold"),5,T),"pv_delay")
        self.assertEqual(category(confirmed("tariff","tariff_grid_support"),4,T),"tariff_support")
        for field,value in [("transaction_evidence_scope","last"),("owner","none"),("execution_phase","preparing"),
                            ("execution_last_valid_read_at",datetime.fromtimestamp(T-121,UTC).isoformat())]:
            state=confirmed();state.attributes[field]=value
            self.assertEqual(category(state,5,T),"unclassified")
        self.assertEqual(category(confirmed(),0,T),"unclassified")
        self.assertEqual(category(NS(state="idle",attributes={"owner":"none"}),0,T),"self_use")
        self.assertEqual(category(NS(state="unavailable",attributes={"owner":"none"}),0,T),"unclassified")

    def test_runtime_batch_error_retry_and_paginated_cache(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                async def executor(fn,*args): return fn(*args)
                hass=NS(config=NS(time_zone="Europe/Warsaw",path=lambda *p:str(Path(directory)/p[-1])),
                        async_add_executor_job=executor,states=NS(get=lambda _:NS(state="Net")))
                manager=Runtime(hass,NS(entry_id="one"),NS(source_device=NS(id="device")),None,None)
                manager.archive.initialize(T);manager.ready=True
                manager.accumulator.observe(obs(T));manager.accumulator.observe(obs(T+30))
                write=manager.archive.write
                def uncertain(*args):write(*args);raise OSError("response lost after commit")
                manager.archive.write=uncertain
                await manager.flush();self.assertIsNotNone(manager._batch)
                manager.archive.write=write
                await manager.flush();self.assertIsNone(manager._batch)
                data=await manager.read("day","2026-10-06")
                self.assertAlmostEqual(data["totals"]["import_cost"],.025)
                self.assertEqual(data["manual_price_basis"],"Net")
                page2=await manager.read("day","2026-10-06",24)
                self.assertEqual(page2["tariffs"],[])
                self.assertEqual(page2["purchases"],data["purchases"])
                self.assertEqual(page2["sales"],data["sales"])
                self.assertTrue((await manager.read("day","2026-10-06",0))["tariffs"])
        asyncio.run(run())

    def test_missing_soc_bounds_leave_flows_but_no_model_benefit(self):
        # Reproduction of the live installation_3 shared-input condition, without
        # changing source freshness or accepting an unverified hardware limit.
        accumulator=A.ProfitAccumulator("Europe/Warsaw")
        for i in range(11):
            accumulator.observe(obs(T+i*30,grid=3,category="rce_export",pv_kw=0,load_kw=1,
                model=model(hardware_minimum_soc_percent=None,hardware_maximum_soc_percent=None)))
        rows=accumulator.drain()
        self.assertEqual(sum(row["model_seconds"] for row in rows),0)
        self.assertAlmostEqual(sum(row["export_kwh"] for row in rows),.25)
        self.assertAlmostEqual(sum(row["export_revenue"] for row in rows),.125)

    def test_partial_price_group_average_does_not_use_unpriced_energy(self):
        with tempfile.TemporaryDirectory() as directory:
            archive=A.ProfitArchive(Path(directory)/"partial.sqlite",{"entry":"one","timezone":"Europe/Warsaw"})
            archive.initialize(T)
            accumulator=A.ProfitAccumulator("Europe/Warsaw")
            for i in range(121):
                accumulator.observe(obs(T+i*30,grid=-3,buy=(rate(.5 if i<60 else None,source="pstryk",zone="dynamic"),)))
            archive.write(accumulator.drain(),"partial",T+3600)
            group=archive.read("day","2026-10-06",T+3600)["purchases"][0]
            self.assertAlmostEqual(group["energy_kwh"],3)
            self.assertAlmostEqual(group["unpriced_kwh"],1.5)
            self.assertAlmostEqual(group["amount_pln"],.75)
            self.assertAlmostEqual(group["average_price"],.5)

    def test_real_runtime_price_sources_and_fresh_units(self):
        now=datetime.fromtimestamp(T,UTC)
        schedule=NS(available=True,source_revision="1",price_basis="configured_marginal_import_cost_component_completeness_unverified",
            source_id="manual:G12w",intervals=[NS(start_utc=now-timedelta(seconds=60),end_utc=now+timedelta(hours=1),price_pln_kwh_ac=.42,zone="low")])
        states={"input_select.hoymiles_profit_manual_price_basis":NS(state="Net"),
                "input_select.hoymiles_dynamic_sale_provider":NS(state="RCE")}
        manager=Runtime(NS(config=NS(time_zone="Europe/Warsaw",path=lambda *p:"unused"),states=NS(get=states.get)),
            NS(entry_id="one"),NS(source_device=NS(id="device"),rce_prices=NS(cache=NS(get=lambda day,at: {"value":[{"dtime_utc":(now+timedelta(minutes=15)).isoformat(),"rce_pln":-123}]} if day==now.date() else None))),
            NS(current_price_schedule=schedule),NS(active=True,cache=NS(view=lambda **kw:NS(snapshot=NS(hours=[NS(start=now,end=now+timedelta(hours=1),net=-.22)])))))
        buys,sells=manager._prices(now)
        self.assertEqual(buys[0].value,.42);self.assertEqual(sells[0].value,-.123)
        self.assertEqual((sells[0].start,sells[0].end),(T,T+900))
        states["input_select.hoymiles_dynamic_sale_provider"].state="Pstryk"
        self.assertEqual(manager._prices(now)[1][0].value,-.22)
        state=NS(state="1500",attributes={"unit_of_measurement":"W"},last_reported=now)
        states["sensor.grid"]=state;manager.sources={"grid":"sensor.grid"}
        self.assertEqual(manager._number("grid",now),1.5)
        self.assertIsNone(manager._number("grid",now+timedelta(seconds=121)))

    def test_year_storage_and_indexed_aggregation(self):
        # 365 days x 24 hours x 4 distinct price blocks. Synthetic, not field data.
        with tempfile.TemporaryDirectory() as directory:
            archive=A.ProfitArchive(Path(directory)/"year.sqlite",{"entry":"benchmark","timezone":"Europe/Warsaw"})
            start=A.period_bounds("year","2026-01-01","Europe/Warsaw")[2]
            archive.initialize(start)
            rows=[]
            for i in range(365*96):
                at=start+i*900;local=datetime.fromtimestamp(at,ZoneInfo("Europe/Warsaw"))
                metadata=A._meta(rate(.5),rate((i%200-50)/1000,source="pse_rce"),local)
                rows.append(dict(id=str(i),hour=int(at//3600)*3600,day=local.date().isoformat(),category="self_use",meta=metadata,
                    **(dict.fromkeys(A.FIELDS,0.) | dict(seconds=900,covered_seconds=900,buy_covered_seconds=900,sell_covered_seconds=900,import_kwh=.1,priced_import_kwh=.1,import_cost=.05))))
            archive.write(rows,"year",start+365*86400)
            began=time.perf_counter();result=archive.read("year","2026-01-01",start+365*86400);seconds=time.perf_counter()-began
            self.assertEqual(result["row_count"],35040)
            self.assertEqual(len(result["series"]),12)
            self.assertAlmostEqual(result["totals"]["import_cost"],1752)
            self.assertLess(result["archive_bytes"],40*1048576)
            with archive._connect() as db:
                plan=str([tuple(r) for r in db.execute("EXPLAIN QUERY PLAN SELECT * FROM hours WHERE day>=? AND day<?",("2026-01-01","2027-01-01"))])
                self.assertIn("profit_day",plan)
            print(json.dumps({"synthetic_year_rows":result["row_count"],"sqlite_bytes":result["archive_bytes"],"year_read_seconds":round(seconds,3)}))

if __name__ == "__main__":
    unittest.main(verbosity=2)
