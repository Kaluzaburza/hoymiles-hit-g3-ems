"""Profile provenance, bounded statistics and rare storage refreshes."""
import asyncio
from datetime import datetime,timedelta,timezone
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import sys,unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from custom_components.hoymiles_hit_modbus.charge_forecast_runtime import ChargeForecastRuntime

NOW=datetime(2026,10,1,15,tzinfo=timezone.utc)

class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    def model(self):
        return ChargeForecastRuntime(SimpleNamespace(states=SimpleNamespace(is_state=lambda *a:True)),{'source':'one'})

    async def test_validity_and_foreign_corrupt_cache(self):
        m=self.model();raw={'identity':m.identity,'at':NOW.isoformat(),'curve':[7.,4.,1.]}
        m.restore(raw);self.assertEqual(m.curve(NOW),(7.,4.,1.))
        self.assertIsNone(m.curve(NOW-timedelta(seconds=1)))
        self.assertIsNone(m.curve(NOW+timedelta(days=8)))
        for bad in ({**raw,'identity':{'source':'other'}},{**raw,'curve':[7.,0.,1.]},
                    {**raw,'at':'broken'},{**raw,'curve':[float('nan'),4.,1.]}):
            other=self.model();other.restore(bad);self.assertIsNone(other.curve(NOW))

    async def test_callback_frequency_does_not_create_queries(self):
        m=self.model();calls=[]
        rows=[(NOW.timestamp()-day*86400-i*300,soc,amps,50.)
            for day in (1,2) for soc,amps in ((50.,150.),(94.,100.),(99.,30.)) for i in range(10)]
        async def run(query):calls.append(query);return rows
        with patch('homeassistant.components.recorder.get_instance',return_value=SimpleNamespace(async_add_executor_job=run)):
            for i in range(1000):await m.refresh(NOW+timedelta(seconds=i))
            self.assertEqual(len(calls),1)
            self.assertIsNotNone(m.curve(NOW+timedelta(seconds=1000)))
            await m.refresh(NOW+timedelta(hours=6));self.assertEqual(len(calls),2)

    async def test_inflight_and_disabled_do_not_query(self):
        m=self.model();m.worker=asyncio.get_running_loop().create_future()
        await m.refresh(NOW);self.assertIsNone(m.last_attempt)
        m.worker.cancel();m.hass.states.is_state=lambda *a:False
        await m.refresh(NOW);self.assertIsNone(m.last_attempt)

class StatisticsTests(unittest.TestCase):
    def test_real_sqlite_query_pairs_units_timestamps_and_bounds(self):
        from contextlib import contextmanager
        from sqlalchemy import create_engine, event
        from sqlalchemy.orm import Session
        from homeassistant.components.recorder.db_schema import StatisticsMeta, StatisticsShortTerm
        from custom_components.hoymiles_hit_modbus.charge_forecast_runtime import IDS, read_statistics
        engine=create_engine('sqlite://')
        StatisticsMeta.__table__.create(engine)
        StatisticsShortTerm.__table__.create(engine)
        queries=[]
        event.listen(engine,'before_cursor_execute',lambda conn,cursor,stmt,params,ctx,many:queries.append(stmt))
        with Session(engine) as session:
            for i,(entity,unit,value) in enumerate(zip(IDS,('%','A','V'),(50.,150.,50.)),1):
                session.add(StatisticsMeta(id=i,statistic_id=entity,source='recorder',unit_of_measurement=unit,has_mean=True,has_sum=False))
                session.add_all(StatisticsShortTerm(metadata_id=i,start_ts=NOW.timestamp()-300*n,mean=value) for n in range(1,11))
            session.commit()
            @contextmanager
            def scope(**kwargs):
                self.assertTrue(kwargs['read_only']);yield session
            with patch('homeassistant.components.recorder.util.session_scope',scope):
                rows=read_statistics(None,NOW-timedelta(days=6),NOW)
                self.assertEqual(len(rows),10)
                self.assertEqual(rows[0][1:],(50.,150.,50.))
                self.assertEqual(sum('LIMIT' in q for q in queries),3)
                session.get(StatisticsMeta,2).unit_of_measurement='mA';session.commit()
                self.assertEqual(read_statistics(None,NOW-timedelta(days=6),NOW),[])
                session.get(StatisticsMeta,2).unit_of_measurement='A';session.commit()
                session.add_all(StatisticsShortTerm(metadata_id=1,start_ts=NOW.timestamp()-300.5-n,mean=50.) for n in range(1740))
                session.commit()
                self.assertEqual(read_statistics(None,NOW-timedelta(days=6),NOW),[])
        engine.dispose()

if __name__=='__main__':unittest.main()
