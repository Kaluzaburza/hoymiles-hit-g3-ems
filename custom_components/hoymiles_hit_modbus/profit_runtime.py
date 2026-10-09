"""Observation-only economics. Uses existing HA states and price caches only."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
import time
import uuid
from zoneinfo import ZoneInfo

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .baseline_energy_timeline import BaselineEnergyInputs
from .const import DOMAIN
from .energy_data import numeric_state_sample
from .execution_history import decision_evidence, timestamp
from .profit_archive import ProfitAccumulator, ProfitArchive, Observation, Rate, finite

_LOGGER = logging.getLogger(__name__)
MANUAL_BASIS_ENTITY = "input_select.hoymiles_profit_manual_price_basis"
SALE_PROVIDER = "input_select.hoymiles_dynamic_sale_provider"
PURCHASE_PROVIDER = "input_select.hoymiles_tariff_operator"


def net_import_rate(value, basis, manual_basis):
    """Convert ONLY known gross values. Unknown is never labelled net."""
    if basis == "public_net_same_buy_sell":
        return value, "public_priceNet"
    if basis.startswith("marginal_gross_import_cost_including_"):
        return value / 1.23, "profile_gross_divided_by_1.23"
    if basis == "configured_marginal_import_cost_component_completeness_unverified":
        if manual_basis == "Net":
            return value, "manual_net_components_unverified"
        if manual_basis == "Gross VAT 23%":
            return value / 1.23, "manual_gross_divided_by_1.23_components_unverified"
    return None, "net_basis_unverified"


def category_from_state(state, mode, now):
    if state is None or mode is None:
        return "unclassified"
    attrs = state.attributes
    evidence = decision_evidence(state.state, attrs, now)
    physical_at = timestamp(attrs.get("execution_last_valid_read_at"))
    if evidence["confirmed"] and physical_at is not None and 0 <= now - physical_at <= 120:
        action = evidence["action"]
        if action == "rce_export" and mode == 5:
            return "rce_export"
        if action == "pv_charge_hold" and mode == 5:
            return "pv_delay"
        if action in {"tariff_battery_charge", "tariff_grid_support_and_charge"} and mode == 4:
            return "tariff_charge"
        if action == "tariff_grid_support" and mode == 4:
            return "tariff_support"
        if evidence["policy"] == "rcm":
            return "rcm"
    if (mode == 0 and attrs.get("owner") in {None, "none"}
            and attrs.get("transaction_evidence_scope") != "active"
            and state.state not in {"unknown", "unavailable"}):
        return "self_use"
    if attrs.get("owner") == "manual":
        return "manual"
    return "unclassified"


class ProfitRuntime:
    """One observer and serialized archive worker per config entry."""
    def __init__(self, hass, entry, runtime, tariff, pstryk):
        self.hass, self.entry, self.runtime = hass, entry, runtime
        self.tariff, self.pstryk = tariff, pstryk
        self.zone = hass.config.time_zone
        self.identity = {"entry_id": entry.entry_id, "source_device_id": runtime.source_device.id,
                         "timezone": self.zone}
        self.archive = ProfitArchive(hass.config.path(".storage", f"{DOMAIN}_profits_{entry.entry_id}.sqlite"), self.identity)
        self.accumulator = ProfitAccumulator(self.zone)
        self.sources = {}
        self.error = None
        self.observation_error = None
        self.ready = False
        self._unsubs = []
        self._lock = asyncio.Lock()
        self._batch = None
        self._task = None
        self._closed = False
        self._next_flush = 0
        self._price_key = None
        self._price_rows = ((), ())
        self._state_unsub = None
        self._read_cache = None

    async def initialize(self):
        try:
            await self.hass.async_add_executor_job(self.archive.initialize, time.time())
        except Exception as err:  # Observation failure must not disable control.
            self.error = type(err).__name__
            _LOGGER.error("Economics archive initialization failed: %s", self.error)
            return
        self.ready = True
        self._resolve_sources()
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, timedelta(seconds=30)))
        self._unsubs.append(self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stop_event))
        self._tick()

    def _resolve_sources(self):
        registry = er.async_get(self.hass)
        previous = dict(self.sources)
        self.sources = {}
        for role, suffix in {"grid": "overview_grid_total_active_power", "mode": "ems_mode_readback_code",
                             "supervisor": "ems_supervisor"}.items():
            entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{self.entry.entry_id}_{suffix}")
            if entity_id:
                self.sources[role] = entity_id
        if self.sources != previous:
            if self._state_unsub:
                self._state_unsub()
            self._state_unsub = async_track_state_change_event(
                self.hass, [*self.sources.values(), MANUAL_BASIS_ENTITY, SALE_PROVIDER, PURCHASE_PROVIDER], self._tick)

    def _text(self, entity):
        state = self.hass.states.get(entity)
        return state.state if state else "unknown"

    def _number(self, role, now):
        state = self.hass.states.get(self.sources.get(role, ""))
        if role == "grid":
            unit = state.attributes.get("unit_of_measurement") if state else None
            if unit not in {"W", "kW"}:
                return None
            scale = .001 if unit == "W" else 1
        else:
            scale = 1
        sample = numeric_state_sample(state, now, max_age_seconds=120, scale=scale)
        return sample.value if sample.fresh else None

    def _prices(self, now):
        # Provider source tables are already fetched by the existing coordinators.
        # Cache only for this minute; tariff/user changes invalidate the key.
        schedule = self.tariff.current_price_schedule
        manual = self._text(MANUAL_BASIS_ENTITY)
        seller = self._text(SALE_PROVIDER)
        buyer = self._text(PURCHASE_PROVIDER)
        key = (int(now.timestamp() // 60), manual, seller, buyer,
               schedule.source_revision if schedule else None)
        if key == self._price_key:
            return self._price_rows
        buys, sells = [], []
        if schedule and schedule.available:
            for row in schedule.intervals:
                value, basis = net_import_rate(row.price_pln_kwh_ac, schedule.price_basis, manual)
                buys.append(Rate(row.start_utc.timestamp(), row.end_utc.timestamp(), value,
                                 schedule.source_id, row.zone, basis))
        if seller == "Pstryk":
            view = self.pstryk.cache.view(now=now)
            if self.pstryk.active and view.snapshot:
                sells = [Rate(r.start.timestamp(), r.end.timestamp(), float(r.net) if r.net is not None else None,
                              "pstryk", "hourly", "public_priceNet") for r in view.snapshot.hours]
        elif seller in {"RCE", "PSE", "PSE RCE"} and self.runtime.rce_prices:
            today = now.astimezone(ZoneInfo("Europe/Warsaw")).date()
            for day in (today, today + timedelta(days=1)):
                cached = self.runtime.rce_prices.cache.get(day, now)
                if cached:
                    for row in cached["value"]:
                        end = timestamp(row["dtime_utc"])
                        sells.append(Rate(end - 900, end, row["rce_pln"] / 1000, "pse_rce", "quarter_hour", "market_net_PLN_MWh_divided_by_1000"))
        self._price_key, self._price_rows = key, (tuple(buys), tuple(sells))
        return self._price_rows

    def _model(self, now):
        snapshot = self.runtime.shared_inputs.snapshot
        if not -5 <= (now - snapshot.captured_at).total_seconds() <= 60:
            return None, None, None
        def value(sample):
            return finite(sample.value) if sample.fresh else None
        capacity = snapshot.system.total_capacity_kwh
        if not capacity.fresh:
            capacity = snapshot.system.battery_capacity_kwh
        kwargs = dict(
            generated_at=now, config_entry_id=self.entry.entry_id, shared_inputs_revision=snapshot.revision,
            current_soc_percent=value(snapshot.system.battery_soc_percent), battery_capacity_kwh=value(capacity),
            reserve_soc_percent=value(snapshot.system.self_use_reserve_soc_percent),
            hardware_minimum_soc_percent=value(snapshot.system.hardware_minimum_soc_percent),
            hardware_maximum_soc_percent=value(snapshot.system.hardware_maximum_soc_percent),
            pv_to_battery_efficiency=value(snapshot.efficiency.pv_to_battery_efficiency),
            battery_to_home_efficiency=value(snapshot.efficiency.battery_to_home_efficiency),
            maximum_charge_power_kw=value(snapshot.bms.maximum_charge_power_kw),
            maximum_discharge_power_kw=value(snapshot.bms.maximum_discharge_power_kw),
            system_ac_power_kw=value(snapshot.system.system_rated_power_kw),
            zero_export_confirmed=snapshot.gcf.zero_export_confirmed is True,
            export_allowed=snapshot.gcf.export_allowed, sources={}, provenance={},
        )
        for key in ("pv_to_battery_efficiency", "battery_to_home_efficiency"):
            if kwargs[key] is not None:
                kwargs[key] /= 100
        model = BaselineEnergyInputs(**kwargs)
        if model.current_soc_percent is None or model.battery_capacity_kwh is None:
            model = None
        return model, value(snapshot.power.pv_power_kw), value(snapshot.power.home_load_power_kw)

    @callback
    def _tick(self, _now=None):
        if self._closed or not self.ready:
            return
        try:
            self._resolve_sources()
            now = dt_util.utcnow()
            grid, mode = self._number("grid", now), self._number("mode", now)
            supervisor = self.hass.states.get(self.sources.get("supervisor", ""))
            category = category_from_state(supervisor, mode, now.timestamp())
            buy, sell = self._prices(now)
            model, pv, load = self._model(now)
            self.accumulator.observe(Observation(now.timestamp(), grid, category, buy, sell, pv, load, model,
                                                 f"{self.entry.entry_id}:{self.runtime.source_device.id}"))
            self.observation_error = None
            if now.timestamp() >= self._next_flush and (self._task is None or self._task.done()):
                self._next_flush = now.timestamp() + 300
                self._task = self.hass.async_create_task(self.flush(), "hoymiles_profit_archive_flush")
        except Exception as err:
            self.accumulator.previous = None
            self.accumulator._reset_model()
            if self.observation_error != type(err).__name__:
                _LOGGER.warning("Economics observation unavailable: %s", type(err).__name__)
            self.observation_error = type(err).__name__

    async def flush(self):
        async with self._lock:
            try:
                if self._batch is None:
                    rows = self.accumulator.drain()
                    if rows:
                        self._batch = (rows, uuid.uuid4().hex, time.time())
                if self._batch:
                    await self.hass.async_add_executor_job(self.archive.write, *self._batch)
                    self._batch = None
                    self._read_cache = None
                self.error = None
            except Exception as err:
                self.error = type(err).__name__
                _LOGGER.error("Economics archive write failed; batch retained: %s", self.error)

    async def read(self, period, selected, offset=0):
        async with self._lock:
            key = (period, selected)
            if self._read_cache and self._read_cache[0] == key and time.monotonic() < self._read_cache[1]:
                result = dict(self._read_cache[2])
            else:
                result = await self.hass.async_add_executor_job(self.archive.read, period, selected, time.time())
                self._read_cache = (key, time.monotonic() + 30, dict(result))
        result["tariff_count"] = len(result["tariffs"])
        result["tariffs"] = result["tariffs"][offset:offset + 24]
        result["storage_error"] = self.error
        result["observation_error"] = self.observation_error
        result["manual_price_basis"] = self._text(MANUAL_BASIS_ENTITY)
        result["save_interval_seconds"] = 300
        return result

    async def _stop_event(self, _event):
        await self.close()

    async def close(self):
        if self._closed:
            return
        self._tick()
        self._closed = True
        if self._state_unsub:
            self._state_unsub()
        for unsubscribe in self._unsubs:
            unsubscribe()
        self._unsubs.clear()
        if self.ready:
            await self.flush()
