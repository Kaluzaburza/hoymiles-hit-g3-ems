"""Six-day, indexed statistics read at most four times/day; no new HA entities."""
import asyncio
from datetime import datetime, timedelta
from functools import partial

from .charge_forecast import learn_curve, valid_curve

IDS = ('sensor.hoymiles_hit_battery_soc_bms',
       'sensor.hoymiles_hit_maximum_charge_current',
       'sensor.hoymiles_hit_battery_voltage_bms')


def read_statistics(hass, start, end):
    from sqlalchemy import select
    from homeassistant.components.recorder.db_schema import StatisticsMeta, StatisticsShortTerm
    from homeassistant.components.recorder.util import session_scope
    output = []
    with session_scope(hass=hass, read_only=True) as session:
        for entity, unit in zip(IDS, ('%', 'A', 'V')):
            meta = session.execute(select(StatisticsMeta.id, StatisticsMeta.unit_of_measurement)
                .where(StatisticsMeta.statistic_id == entity)).first()
            if meta is None or meta[1] != unit:
                return []
            rows = session.execute(select(StatisticsShortTerm.start_ts, StatisticsShortTerm.mean)
                .where(StatisticsShortTerm.metadata_id == meta[0],
                       StatisticsShortTerm.start_ts >= start.timestamp(),
                       StatisticsShortTerm.start_ts < end.timestamp())
                .order_by(StatisticsShortTerm.start_ts).limit(1731)).all()
            if len(rows) > 1730:
                return []
            output.append(dict(rows))
    return [(t, *(values[t] for values in output)) for t in sorted(set.intersection(*(set(v) for v in output)))]


class ChargeForecastRuntime:
    def __init__(self, hass, identity):
        self.hass, self.identity = hass, identity
        self.saved = None
        self.last_attempt = None
        self.worker = None

    def restore(self, raw):
        if not isinstance(raw, dict) or raw.get('identity') != self.identity:
            return
        try:
            stamp = datetime.fromisoformat(raw['at'])
            if stamp.tzinfo is None or not valid_curve(raw.get('curve')):
                return
            self.saved = {'identity': self.identity, 'at': stamp.isoformat(), 'curve': list(raw['curve'])}
            self.last_attempt = stamp
        except (ValueError, TypeError, KeyError):
            return

    def curve(self, now):
        if self.saved is None:
            return None
        if not 0 <= (now-datetime.fromisoformat(self.saved['at'])).total_seconds() <= 7*86400:
            return None
        return tuple(self.saved['curve'])

    def payload(self):
        return self.saved

    async def refresh(self, now):
        if not self.hass.states.is_state('input_boolean.hoymiles_pv_charge_delay_enabled', 'on'):
            return
        if self.worker is not None and not self.worker.done():
            return
        if self.last_attempt is not None and 0 <= (now-self.last_attempt).total_seconds() < 6*3600:
            return
        self.last_attempt = now
        try:
            from homeassistant.components.recorder import get_instance
            query = partial(read_statistics, self.hass, now-timedelta(days=6), now)
            self.worker = asyncio.ensure_future(get_instance(self.hass).async_add_executor_job(query))
            self.worker.add_done_callback(lambda task: None if task.cancelled() else task.exception())
            rows = await asyncio.wait_for(asyncio.shield(self.worker), timeout=8)
            curve = learn_curve(rows)
            if curve:
                self.saved = {'identity': self.identity, 'at': now.isoformat(), 'curve': list(curve)}
        except Exception:
            # Optional forecasting evidence is never allowed to fail LOAD/control.
            return
