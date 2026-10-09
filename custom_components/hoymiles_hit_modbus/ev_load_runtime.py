"""Bounded optional EV evidence for the existing, single shared LOAD provider."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from .bounded_history import async_get_bounded_state_reports
from .energy_data import state_reported_at
from .ev_load_filter import Config, ENABLED, POWER, SENSOR, MAX_DAYS, power_kw, integrate_day, project_history


class EvLoadRuntime:
    def __init__(self, hass):
        self.hass = hass
        self.key = None
        self.epoch = 0
        self.days = {}
        self.running = False
        self.last_batch = None
        self.status = 'off'
        self.excluded_days = 0
        self._projection = None

    def configure(self):
        def state(entity, default=''):
            return getattr(self.hass.states.get(entity), 'state', default)
        try:
            typical = float(state(POWER, '0'))
        except (TypeError, ValueError):
            typical = 0.
        if not isfinite(typical):
            typical = 0.
        entity_id = state(SENSOR).strip()
        # A new HA input_text is unknown until first edited. It is an empty
        # optional mapping; unavailable remains invalid and must fail closed.
        if entity_id == 'unknown':
            entity_id = ''
        config = Config(state(ENABLED) == 'on', typical, entity_id)
        source = self.hass.states.get(config.entity_id) if config.entity_id else None
        unit = (getattr(source, 'attributes', {}) or {}).get('unit_of_measurement', '')
        key = [1, config.enabled, config.typical_kw, config.entity_id, unit, self.hass.config.time_zone]
        if key != self.key:
            self.key = key
            self.epoch += 1
            self.days.clear()
            self.last_batch = None
            self._projection = None
        return config, source, unit

    def restore(self, payload):
        self.configure()
        if not isinstance(payload, dict) or payload.get('key') != self.key:
            return
        days = payload.get('days')
        if not isinstance(days, dict) or len(days) > MAX_DAYS:
            return
        try:
            today = datetime.now(ZoneInfo(self.hass.config.time_zone)).date()
            accepted = {}
            for key, row in days.items():
                day = date.fromisoformat(key)
                if not today-timedelta(days=MAX_DAYS) <= day < today:
                    continue
                if row is not None and (not isinstance(row, list) or len(row) != 48 or any(
                    type(v) not in (int, float) or not isfinite(v) or not 0 <= v <= 100 for v in row
                )):
                    return
                accepted[key] = tuple(row) if row is not None else None
            self.days = accepted
            self._projection = None
        except (TypeError, ValueError):
            return

    def payload(self):
        config, _, _ = self.configure()
        if not config.enabled or not config.entity_id or not config.valid:
            return None
        return {'key': self.key, 'days': {k:list(v) if v is not None else None for k,v in self.days.items()}}

    def project(self, raw):
        config, _, _ = self.configure()
        if self._projection is not None and self._projection[0] is raw:
            return self._projection[1]
        view, self.excluded_days = project_history(raw, config, self.days)
        self._projection = (raw, view)
        return view

    def forecast_power(self, actual_kw, base_kw, observed_at, now):
        config, state, unit = self.configure()
        if not config.enabled:
            self.status = 'off'
            return actual_kw
        if not config.valid:
            self.status = 'invalid_configuration'
            return None
        if not config.entity_id:
            suspect = actual_kw - base_kw >= .65 * config.typical_kw
            self.status = 'suspected_ev' if suspect else 'heuristic'
            return None if suspect else actual_kw
        stamp = state_reported_at(state) if state is not None else None
        ev_kw = power_kw(getattr(state, 'state', None), unit)
        if stamp is None or ev_kw is None:
            self.status = 'sensor_invalid'
            return None
        if not 0 <= (now-stamp).total_seconds() <= 180 or abs((observed_at-stamp).total_seconds()) > 90:
            self.status = 'sensor_stale'
            return None
        if ev_kw > actual_kw + .1:
            self.status = 'sensor_exceeds_load'
            return None
        self.status = 'sensor'
        return max(0., actual_kw-ev_kw)

    async def refresh(self, raw, now):
        """At most four daily queries/hour, newest first, 8 s total budget.

        First recent raw LOAD remains the priority. Missing/corrupt EV days are
        remembered too, preventing repeated full backfills or callback queries.
        No write here; the existing LOAD Store saves the bounded projection.
        """
        config, _, unit = self.configure()
        if self.running or not config.enabled or not config.valid or not config.entity_id or unit not in ('W', 'kW'):
            return
        if self.last_batch is not None and (now-self.last_batch).total_seconds() < 3600:
            return
        keys = [k for k in sorted(raw.daily_energy_kwh, reverse=True)
                if now.date()-timedelta(days=MAX_DAYS) <= date.fromisoformat(k) < now.date()][:MAX_DAYS]
        self.days = {k:v for k,v in self.days.items() if k in keys}
        self._projection = None
        missing = [k for k in keys if k not in self.days][:4]
        if not missing:
            return
        self.last_batch = now
        epoch = self.epoch
        self.running = True
        try:
            async with asyncio.timeout(8):
                for key in missing:
                    start = datetime.fromisoformat(key).replace(tzinfo=now.tzinfo)
                    rows = await async_get_bounded_state_reports(self.hass, start,
                        start+timedelta(days=1), (config.entity_id,), limit_per_entity=10000, timeout_seconds=7)
                    self.configure()
                    if epoch != self.epoch:
                        return
                    points = [(r.last_updated, r.state) for r in rows.get(config.entity_id, ())]
                    self.days[key] = integrate_day(points, start, unit)
                    self._projection = None
        except Exception:  # Optional EV failure must not fail raw LOAD refresh.
            if epoch == self.epoch:
                self.status = 'history_unavailable'
        finally:
            self.running = False
