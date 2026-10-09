"""Entry-local public prices and one Pstryk plan, using the existing executor.

No Modbus writes or Recorder queries. The two plan entities are projections of
one accepted revision; the Supervisor still owns every execution decision.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json
import logging
import math

import aiohttp
from homeassistant.components.sensor import SensorEntity
from homeassistant.core import Context, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .dynamic_price_profile import DynamicPriceProfile
from .const import DOMAIN
from .pstryk_client import PstrykClient, PstrykAPIError
from .pstryk_prices import price_projection
from .pstryk_daily_cache import DailyPriceCache as PriceCache, day_bounds
from .pstryk_origin import OriginLedger
from .pstryk_joint import optimize, revalidate_plan
from .rce_optimizer import _immutable_input_changes
from .pstryk_settling import market_basis, load_only_suppression
from .pstryk_plan import build_joint_input, execution_metadata, input_key, projections, timeline, retain_active_plan, retain_active_buy_plan, live_load_only_export_suppressed
from .tariff_price_schedule import TariffPriceInterval, TariffPriceScheduleSnapshot

_LOGGER = logging.getLogger(__name__)
from .pv_charge_delay import HELPER as DELAY_HELPER, PROFILE_HELPER as DELAY_PROFILE_HELPER, profile_weight

SALE = 'input_select.hoymiles_dynamic_sale_provider'
PURCHASE = 'input_select.hoymiles_tariff_operator'
TARIFF = 'input_select.hoymiles_tariff_type'
OPTION_HELPERS = {
    'maximum_soc':'input_number.hoymiles_tariff_maximum_soc',
    'charge_power_percent':'input_number.hoymiles_tariff_requested_charge_power',
    'charge_efficiency':'input_number.hoymiles_tariff_charge_efficiency',
    'minimum_saving':'input_number.hoymiles_tariff_minimum_saving',
    'demand_margin_percent':'input_number.hoymiles_tariff_soc_safety_margin',
}
PERMISSIONS = ('input_boolean.hoymiles_tariff_charge_enabled',
               'input_boolean.hoymiles_ems_supervisor_allow_tariff',
               'input_boolean.hoymiles_rce_discharge_enabled',
               'input_boolean.hoymiles_ems_supervisor_allow_rce')
PRICE_ENTITY_ID = 'sensor.hoymiles_hit_pstryk_public_net_price'


@callback
def prepare_price_entity(hass, entry):
    """Reserve the dashboard identity; migrate only our device-prefixed row."""
    registry = er.async_get(hass)
    unique_id = f'{entry.entry_id}_pstryk_public_net_price'
    canonical = registry.async_get(PRICE_ENTITY_ID)
    if canonical is not None and (
        canonical.platform != DOMAIN or canonical.unique_id != unique_id
        or canonical.config_entry_id != entry.entry_id
    ):
        raise ValueError('Pstryk price entity ID is owned by another entity')
    existing = registry.async_get_entity_id('sensor', DOMAIN, unique_id)
    if existing and existing != PRICE_ENTITY_ID:
        row = registry.async_get(existing)
        if (row.config_entry_id != entry.entry_id or not existing.endswith(
            '_hoymiles_hit_pstryk_public_net_price'
        )):
            raise ValueError('Pstryk price entity has a custom identity; preserve it')
        registry.async_update_entity(existing, new_entity_id=PRICE_ENTITY_ID)
    registry.async_get_or_create('sensor', DOMAIN, unique_id,
        suggested_object_id=PRICE_ENTITY_ID.split('.', 1)[1], config_entry=entry)


class PstrykRuntime:
    def __init__(self, hass, entry, runtime, rce, tariff):
        self.hass,self.entry,self.runtime,self.rce,self.tariff = hass,entry,runtime,rce,tariff
        self.profile = DynamicPriceProfile()
        self.cache = PriceCache(self.scope)
        self.origin = OriginLedger(f'{entry.entry_id}:{runtime.source_device.id}')
        self.store = Store(hass,1,f'hoymiles_pstryk_{entry.entry_id}')
        self._store_lock = asyncio.Lock()
        self._profile_lock = asyncio.Lock()
        self._context = Context()
        self._unsubs = []
        self._tasks = set()
        self._plan_task = self._refresh_task = None
        self._client = self._session = None
        self._saved = self._cache_payload = None
        self._closed = False
        self.initialized = self.storage_ready = False
        self._next_store_retry = None
        self.price_schedule_sensor = None
        self._revision = 0
        self._accepted = self._key = self._calculated_at = None
        self._accepted_source = None
        self._accepted_settings = None
        self._active_run_basis = None
        self._active_buy_basis = None
        self._pv_settling_basis = None
        self._settling_reference = self._settling_options = self._settling_basis = None
        self._intent = self._intent_since = None
        self.sensor = PstrykPriceSensor(self)

    @property
    def scope(self):
        return f'{self.entry.entry_id}:pstryk:{self.profile.revision}'

    @property
    def active(self):
        # Mixed helpers or an unfinished switch must NEVER fall back to RCE.
        return self.profile.mode=='pstryk' or any(self.hass.states.is_state(e,'Pstryk') for e in (SALE,PURCHASE))

    def ignore_event(self, entity_id):
        return self.active and entity_id in {
            self.rce.entity_id,self.tariff.entity_id,'sensor.hoymiles_rce_day',
            'sensor.hoymiles_rce_day_tomorrow','sensor.hoymiles_hit_tariff_price_schedule'}

    @callback
    def invalidate(self, reason='recalculation_pending'):
        self._key = None
        for sensor in (self.rce,self.tariff):
            old = sensor._attributes
            sensor._attributes = {**old,'result_current':False,'recalculation_pending':True,
                'current_slot_start_eligible':False,'current_run_start_eligible':False,
                'pstryk_blocker':reason}
            if sensor.entity_id and sensor._attributes != old:
                sensor.async_write_ha_state()
            if sensor._timeline_sensor:
                # Joint invalidation may keep the source revision unchanged.
                # publish_pending rejects equal completed revisions as late
                # callbacks; explicit failure withdraws authority while keeping
                # only bounded display geometry for the same cohort.
                sensor._timeline_sensor.publish_unavailable(
                    input_revision=sensor._input_revision.value, blocker_code=reason)

    def _spawn(self, coroutine):
        task = self.hass.async_create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def initialize(self):
        try:
            data = await self.store.async_load()
            if data is not None:
                if not isinstance(data,dict) or len(json.dumps(data))>40000:
                    raise ValueError('store_invalid')
                self.profile = DynamicPriceProfile.from_json(data['profile'])
                self.cache = PriceCache(self.scope)
                if data.get('prices'):
                    self.cache = PriceCache.restore(data['prices'],source_scope=self.scope,now=dt_util.utcnow())
                    self._cache_payload = data['prices']
                if data.get('origin'):
                    self.origin = OriginLedger.restore(self.origin.scope,data['origin'])
                self._saved = data
            self.storage_ready = True
        except (ValueError,KeyError,TypeError,OSError):
            # Corrupt/old cache is not authority. A fresh bounded store can be
            # rebuilt; unknown PV stock remains zero.
            self.cache = PriceCache(self.scope)
            self.origin = OriginLedger(self.origin.scope)
            self.storage_ready = False
        self.initialized = True

    async def start(self):
        if not self.initialized:
            await self.initialize()
        self._unsubs.append(async_track_state_change_event(self.hass,
            (SALE,PURCHASE,TARIFF,DELAY_HELPER,DELAY_PROFILE_HELPER,*OPTION_HELPERS.values(),*PERMISSIONS),self._changed))
        self._unsubs.append(async_track_time_interval(self.hass,self._tick,timedelta(seconds=30)))
        await self._select(None)
        self._tick(None)

    @callback
    def _changed(self,event):
        if self._closed or event.context.id==self._context.id: return
        entity = event.data['entity_id']
        if entity in (SALE,PURCHASE,TARIFF):
            if self.active: self.invalidate('profile_changing')
            self._spawn(self._select(entity))
        elif self.active:
            self.invalidate('settings_changed')
            self._spawn(self.recalculate())

    async def _select(self, changed):
        async with self._profile_lock:
            if self._closed: return
            try:
                profile = self.profile
                purchase = self.hass.states.get(PURCHASE)
                tariff = self.hass.states.get(TARIFF)
                sale = self.hass.states.get(SALE)
                if not all((purchase,tariff,sale)):
                    if self.active: self.invalidate('profile_helpers_missing')
                    return
                if sale.state=='RCE' and (changed==SALE or profile.mode=='pstryk' and profile.classic_operator is None):
                    classic = (purchase.state,tariff.state) if profile.classic_operator is None and purchase.state!='Pstryk' else None
                    profile = profile.select_sale('RCE',classic=classic)
                elif sale.state=='Pstryk' or purchase.state=='Pstryk':
                    if profile.mode=='classic' and purchase.state!='Pstryk':
                        profile = profile.select_purchase(purchase.state,tariff=tariff.state)
                    profile = profile.select_sale('Pstryk')
                elif profile.mode=='classic':
                    profile = profile.select_purchase(purchase.state,tariff=tariff.state)
                if profile!=self.profile:
                    self.invalidate('profile_changing')
                    self.profile = profile
                    self.cache = PriceCache(self.scope)
                    self._cache_payload = None
                    if self._client:
                        await self._client.close()
                        self._client = None
                await self._save()
                expected = ((SALE,self.profile.sale_provider),(PURCHASE,self.profile.purchase_provider))
                if self.profile.mode=='classic':
                    expected += ((TARIFF,self.profile.classic_tariff),)
                for entity,option in expected:
                    if option and not self.hass.states.is_state(entity,option):
                        await self.hass.services.async_call('input_select','select_option',
                            {'entity_id':entity,'option':option},blocking=True,context=self._context)
                if self.profile.mode=='classic':
                    # Classic sources, permissions and optimizer behaviour are
                    # restored by their existing recalculation paths.
                    for sensor in (self.rce,self.tariff):
                        sensor._result = None
                        self._spawn(sensor._recalculate_and_write())
                else:
                    self._tick(None)
            except (ValueError,OSError,KeyError):
                self.invalidate('profile_not_ready')

    async def _save(self):
        async with self._store_lock:
            if self._next_store_retry and dt_util.utcnow()<self._next_store_retry:
                raise ValueError('storage_backoff')
            cache = self.cache
            write = cache.storage_candidate()
            prices = write.payload_json if write else self._cache_payload
            origin = self.origin.storage_candidate()
            data = {'profile':self.profile.to_json(),'prices':prices,'origin':origin}
            if data!=self._saved:
                try:
                    await self.store.async_save(data)
                except Exception:
                    self.storage_ready = False
                    self._next_store_retry = dt_util.utcnow()+timedelta(minutes=5)
                    self.invalidate('storage_unavailable')
                    raise
                self._saved = data
            if write and cache is self.cache:
                cache.ack_storage(write)
                self._cache_payload = prices
            self.origin.acknowledge(origin)
            self.storage_ready = True
            self._next_store_retry = None

    @callback
    def _tick(self,_now):
        if self._closed or not self.initialized or not self.active: return
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = self._spawn(self._refresh())

    async def _refresh(self):
        now = dt_util.utcnow()
        try:
            if self.profile.mode=='pstryk':
                self.cache.prune(now)
                await self._save()
                for day in self.cache.due_days(now):
                    await self._fetch_day(day, now)
            self.sensor.publish()
            if self.price_schedule_sensor is not None:
                self.price_schedule_sensor._refresh()
                if self.price_schedule_sensor.entity_id:
                    self.price_schedule_sensor.async_write_ha_state()
            if not self.storage_ready and (self._next_store_retry is None or dt_util.utcnow()>=self._next_store_retry):
                await self._save()
            await self.recalculate()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.invalidate('pstryk_refresh_failed')
            _LOGGER.exception('Pstryk refresh failed')

    async def _fetch_day(self, day, now):
        # One shared local publication for BUY/SELL and both charts.
        # Persist the correction marker/backoff before network I/O.
        if self._closed or self.profile.mode != 'pstryk': return
        epoch = self.profile.revision
        self.cache.before_fetch(day, now)
        await self._save()
        if self._closed or epoch != self.profile.revision: return
        if self._session is None:
            self._session = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(),trust_env=False)
        if self._client is None:
            self._client = PstrykClient(self._session,source_scope=self.scope)
        try:
            start,end = day_bounds(day)
            snapshot = await self._client.fetch_window(start=start,end=end,received_at=now)
            if self._closed or epoch!=self.profile.revision: return
            before = self.cache.view(now=now).snapshot
            self.cache.accept(snapshot,now=dt_util.utcnow())
            after = self.cache.view(now=dt_util.utcnow()).snapshot
            if before is None or before.revision!=after.revision: self.invalidate('prices_changed')
        except (PstrykAPIError,ValueError) as err:
            if self._closed or epoch!=self.profile.revision: return
            self.cache.failed(err.reason if isinstance(err,PstrykAPIError) else 'incomplete_day')
        await self._save()

    def _options(self):
        values = {}
        for name,entity_id in OPTION_HELPERS.items():
            state = self.hass.states.get(entity_id)
            try: value = float(state.state)
            except (ValueError,TypeError,AttributeError): raise ValueError('helper_missing') from None
            if not math.isfinite(value) or value<0 or name!='minimum_saving' and value>100:
                raise ValueError('helper_invalid')
            values[name] = value
        if values['charge_efficiency']<=0: raise ValueError('charge_efficiency_invalid')
        for entity in PERMISSIONS:
            if self.hass.states.get(entity) is None or self.hass.states.get(entity).state not in ('on','off'):
                raise ValueError('permission_missing')
        values['allow_buy'] = all(self.hass.states.is_state(e,'on') for e in PERMISSIONS[:2])
        values['allow_sell'] = all(self.hass.states.is_state(e,'on') for e in PERMISSIONS[2:])
        profile = self.hass.states.get(DELAY_PROFILE_HELPER)
        values['delay_profile'] = profile.state if profile is not None else None
        values['allow_delay'] = (self.hass.states.is_state(DELAY_HELPER, 'on')
            and profile_weight(values['delay_profile']) is not None)
        return values

    def _input(self):
        if (not self.initialized or not self.storage_ready or self.profile.mode!='pstryk'
                or not all(self.hass.states.is_state(e,'Pstryk') for e in (SALE,PURCHASE))):
            raise ValueError('profile_not_ready')
        if any(s._lifecycle_stopped for s in (self.rce,self.tariff)):
            raise ValueError('unloaded')
        if any(s._forecast_gcf_policy_evaluation_cancel is not None
               for s in (self.rce,self.tariff)):
            raise ValueError('input_cohort_pending')
        view = self.cache.view(now=dt_util.utcnow())
        if view.snapshot is None: raise ValueError(view.reason)
        current = view.snapshot.at(dt_util.utcnow())
        if current is None or current.net is None: raise ValueError('current_price_missing')
        settings,metadata = self.rce._optimizer_input(public_prices=view.snapshot)
        if settings is None: raise ValueError('qualified_inputs_missing')
        if not all(metadata.get(k) is True for k in ('rce_today_data_fresh','forecast_today_data_fresh',
                                                      'soc_data_fresh','gcf_execution_data_fresh')):
            raise ValueError('qualified_inputs_stale')
        forecast_days=2 if metadata.get('forecast_tomorrow_data_fresh') is True else 1
        forecast_end=settings.now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=forecast_days)
        from .pv_charge_delay import qualified_today_refill, qualified_refill_dates
        options = {**self._options(), 'delay_refill_qualified': qualified_today_refill(metadata),
                   'delay_qualified_dates': qualified_refill_dates(metadata,settings.now)}
        source = (self.profile.revision, view.snapshot.revision)
        active_delay = None
        proof = None
        if (self._accepted is not None
            and self._accepted_source == source
            and self._settling_options == self._options()):
            from .pv_charge_delay import active_delay_plan
            reader = getattr(self.rce, '_active_pv_delay_commitment', None)
            proof = reader(settings.now) if callable(reader) else None
            active_delay = active_delay_plan(self._accepted[1].delay_plan, proof, settings.now)
        # Do not extrapolate a Mode 5 switching transient over the entire first
        # half-hour. This RAM-only reference belongs to one ACKed transaction;
        # every safety/SOC/forecast input remains fresh and measurements below
        # remain unmodified. Replans cannot refresh its command-based deadline.
        from .pv_charge_delay import PLANNER_SETTLING_SECONDS
        sent = getattr(proof, 'command_sent_at', None)
        settling = (active_delay is not None and sent is not None
                    and 0 <= (settings.now-sent).total_seconds() < PLANNER_SETTLING_SECONDS)
        binding = ((proof.transaction_id, sent, proof.hard_deadline, source, self._options())
                   if settling else None)
        basis = self._pv_settling_basis
        if basis is not None and (
            source != basis[0][3] or self._options() != basis[0][4]
            or settings.now >= min(basis[0][2], basis[0][1]+timedelta(seconds=PLANNER_SETTLING_SECONDS))
            or binding is not None and basis[0] != binding):
            self._pv_settling_basis = basis = None
        if settling and basis is None:
            reference = self._accepted_settings
            if reference is not None and 0 <= (sent-reference.now).total_seconds() <= 120:
                self._pv_settling_basis = basis = (binding, reference)
        planning = settings
        if settling and basis is not None:
            planning = replace(settings, current_load_power_kw=basis[1].current_load_power_kw,
                               current_pv_power_kw=basis[1].current_pv_power_kw)
        metadata = {**metadata,
            'pv_charge_delay_planner_power_basis': ('pre_command' if settling and basis is not None
                else 'forecast_and_load_profile' if active_delay is not None else 'live'),
            'pv_charge_delay_planner_settling_active': settling and basis is not None,
            'pv_charge_delay_planner_settling_until': (
                min(proof.hard_deadline, sent+timedelta(seconds=PLANNER_SETTLING_SECONDS)).isoformat()
                if settling and basis is not None else None)}
        data = replace(build_joint_input(planning,options,forecast_end=forecast_end,
            active_pv_delay=active_delay if not (settling and basis is not None) else None),
            active_delay=active_delay)
        if self._accepted_source==(self.profile.revision,view.snapshot.revision):
            previous=self._accepted[1].slots[0]
            for action,sensor,method in (('sell',self.rce,'_active_rce_commitment'),
                                         ('buy',self.tariff,'_active_tariff_commitment')):
                reader=getattr(sensor,method,None)
                proof=reader(settings.now) if callable(reader) else None
                basis = self._active_run_basis if action == 'sell' else self._active_buy_basis
                retained_run = (proof is not None and basis is not None
                    and basis[0] == proof.transaction_id and basis[2] == self._accepted_source
                    and basis[3] == self._options() and settings.now < basis[1])
                if (proof is not None and data.slots[0].start<proof.hard_deadline
                    and (previous.action==action and data.slots[0].start<previous.end or retained_run)):
                    data=replace(data,continuing_action=action,
                                 continuing_until=min(proof.hard_deadline,data.slots[0].end))
        key = input_key(data,profile_revision=self.profile.revision,price_revision=view.snapshot.revision)
        return data,metadata,settings,key

    async def recalculate(self):
        if self._closed or not self.active: return
        if self._plan_task is None or self._plan_task.done():
            self._plan_task = self._spawn(self._calculate())
        await asyncio.shield(self._plan_task)

    async def _calculate(self):
        try:
            for attempt in range(2):
                if self._closed: return
                data,metadata,settings,key = self._input()
                await self._save()
                # Cache/profile persistence must precede a new accepted plan.
                data,metadata,settings,key = self._input()
                now = dt_util.utcnow()
                # The existing 30 s timer may land just before this cutoff.
                # Leave two ticks before the unchanged 120 s authority limit
                # for timer phase, solver time and physical-cohort delivery.
                # Only a newly solved/revalidated pair refreshes that age.
                if (attempt == 0 and self.market_fingerprint() is not None and self._calculated_at
                    and (data.active_delay is None) == (self._accepted[0].active_delay is None)
                    and metadata['pv_charge_delay_planner_settling_active']
                        == self.rce._attributes.get('pv_charge_delay_planner_settling_active', False)
                    and (now-self._calculated_at).total_seconds()<60
                    and self._accepted_settings is not None
                    and not _immutable_input_changes(
                        replace(self._accepted_settings,tariff_price_schedule=None),
                        replace(settings,tariff_price_schedule=None))):
                    return
                epoch = self.profile.revision
                source = (epoch,self.cache.view(now=now).snapshot.revision)
                revisions = tuple(s._input_revision.value for s in (self.rce,self.tariff))
                options = self._options()
                settings = deepcopy(settings)
                plan = await self.hass.async_add_executor_job(optimize,data)
                if self._closed or not self.active or epoch!=self.profile.revision: return
                wait_cohort = getattr(self.rce, '_async_wait_active_rce_commitment_cohort', None)
                if callable(wait_cohort) and not await wait_cohort():
                    raise ValueError('input_cohort_pending')
                if self._closed or not self.active or epoch!=self.profile.revision: return
                fresh,latest,latest_settings,fresh_key = self._input()
                if (source!=(self.profile.revision,self.cache.view(now=dt_util.utcnow()).snapshot.revision)
                    or revisions!=tuple(s._input_revision.value for s in (self.rce,self.tariff))
                    or options!=self._options()
                    or _immutable_input_changes(replace(settings,tariff_price_schedule=None),
                                                replace(latest_settings,tariff_price_schedule=None))):
                    self.invalidate('input_changed_during_solve')
                    continue
                if fresh.active_delay != data.active_delay:
                    # ACK/confirmation or proof loss during the executor await
                    # changes the locked window. Revalidation of a negative
                    # result never searches it; solve the fresh binding once.
                    if attempt == 1:
                        raise ValueError('pv_delay_commitment_changed_during_solve')
                    continue
                plan = revalidate_plan(fresh,plan,captured=data)
                if plan is None:
                    self.invalidate('plan_revalidation_failed')
                    continue
                if (fresh.active_delay is not None
                    and (plan.delay_plan is None or plan.slots[0].action != 'pv_charge_hold')):
                    # PV-only reference/forecast inputs cannot authorize BUY/SELL.
                    # The controller alone bounds an unaccepted publication;
                    # loss of its proof immediately restores measured inputs.
                    raise ValueError('pv_delay_continuation_revalidation_failed')
                data,settings,key = fresh,latest_settings,fresh_key
                proof_reader = getattr(self.rce, '_active_rce_commitment', None)
                proof = proof_reader(settings.now) if callable(proof_reader) else None
                basis = self._active_run_basis
                if basis is not None and (
                    settings.now >= basis[1] or basis[2] != source or basis[3] != options
                    or proof is not None and proof.transaction_id != basis[0]
                ):
                    basis = None
                if (proof is not None and basis is None and self._accepted is not None
                    and self._accepted_source == source and self._accepted_settings is not None
                    and self._settling_options == options
                    and self._accepted[1].slots[0].action == 'sell'):
                    basis = (proof.transaction_id, proof.hard_deadline, source, options,
                             (self._accepted[0], self._accepted[1], self._accepted_settings))
                self._active_run_basis = basis
                plan = retain_active_plan(data, plan, settings,
                    accepted=basis[4] if basis is not None else None, commitment=proof)
                buy_reader = getattr(self.tariff, '_active_tariff_commitment', None)
                buy_proof = buy_reader(settings.now) if callable(buy_reader) else None
                buy_basis = self._active_buy_basis
                if buy_basis is not None and (
                    settings.now >= buy_basis[1] or buy_basis[2] != source or buy_basis[3] != options
                    or buy_proof is not None and buy_proof.transaction_id != buy_basis[0]
                ):
                    buy_basis = None
                if (buy_proof is not None and buy_basis is None and self._accepted is not None
                    and self._accepted_source == source and self._accepted_settings is not None
                    and self._settling_options == options
                    and self._accepted[1].slots[0].action == 'buy'):
                    buy_basis = (buy_proof.transaction_id, buy_proof.hard_deadline, source, options,
                                 (self._accepted[0], self._accepted[1], self._accepted_settings))
                self._active_buy_basis = buy_basis
                plan = retain_active_buy_plan(data, plan, settings,
                    accepted=buy_basis[4] if buy_basis is not None else None, commitment=buy_proof)
                reference = self._settling_reference
                qualifies = False
                live_load_qualifies = False
                if (proof is not None and reference is not None and reference[0] == source
                    and 0 <= (now-proof.started_at).total_seconds() < 150
                    and now < proof.hard_deadline):
                    live_load_qualifies = live_load_only_export_suppressed(
                        data, plan, reference[1], reference[2],
                        settings.inverter_power_kw * settings.inverter_count,
                    )
                    qualifies = await self.hass.async_add_executor_job(
                        load_only_suppression, data, plan, reference[1], reference[2])
                    if self._closed or not self.active or epoch != self.profile.revision:
                        return
                    # The extra bounded calculation has the same stale-result gate.
                    _, _, _, check_key = self._input()
                    if check_key != key:
                        self.invalidate('input_changed_during_settling_check')
                        continue
                self._revision += 1
                self._accepted = (data,plan,settings.inverter_power_kw*settings.inverter_count)
                self._accepted_settings = deepcopy(settings)
                self._accepted_source = (epoch,self.cache.view(now=dt_util.utcnow()).snapshot.revision)
                # Age the fresh cohort used for successful revalidation, not
                # the pre-solve capture. Cache reuse never refreshes this age.
                self._key,self._calculated_at = key,settings.now
                rce,tariff = projections(data,plan,revision=self._revision,
                                         metadata={**latest, **execution_metadata(settings)},
                                         system_power_kw=self._accepted[2])
                rce['current_slot_load_exhausts_requested_discharge_budget'] = qualifies
                rce['current_slot_load_only_export_suppressed'] = live_load_qualifies
                self._settling_options = self._options()
                self._settling_basis = market_basis(data, *source)
                if plan.slots[0].action == 'sell' and rce['current_slot_continue_eligible']:
                    self._settling_reference = (source, data, plan)
                intent = (plan.slots[0].action,plan.slots[0].end)
                if intent!=self._intent:
                    self._intent,self._intent_since = intent,now
                stable = (now-self._intent_since).total_seconds()
                tariff['current_run_intent_stable_seconds'] = stable
                if tariff['current_action']=='grid_support' and stable<120:
                    tariff.update(current_run_start_eligible=False,current_run_suppression_reason='intent_not_stable')
                from .pv_charge_delay import stabilize_attributes
                delay_projection = {k:v for k,v in rce.items() if k.startswith('pv_charge_delay_')}
                delay_projection = stabilize_attributes(delay_projection,
                    getattr(self.rce, '_pv_delay_projection', None))
                self.rce._pv_delay_projection = delay_projection
                rce.update(delay_projection)
                # Assign both projections synchronously before either state
                # event can wake the Supervisor. It independently checks the
                # common accepted revision as well.
                for sensor,attrs in ((self.rce,rce),(self.tariff,tariff)):
                    sensor._result = None
                    sensor._attributes = {**attrs,'input_revision':sensor._input_revision.value,
                                          'joint_profile_revision':self.profile.revision}
                # Publish the incumbent's counterpart first. The paired-plan
                # gate stays closed between events; its old eligible plan can
                # use the existing bounded publication wait until the owner
                # receives the new, complete pair (including a LOAD veto).
                publication_order = (self.tariff,self.rce) if proof is not None else (self.rce,self.tariff)
                for sensor in publication_order:
                    if sensor.entity_id: sensor.async_write_ha_state()
                self.publish_timeline(self.rce)
                self.publish_timeline(self.tariff)
                return
        except asyncio.CancelledError:
            raise
        except ValueError as err:
            # Match RCE's bounded 258/259/generation delivery coalescing.
            # Keep only an already-current pair in the same fresh market;
            # the native GCF deadline still invalidates an incoherent cohort.
            # Never certify a new result from this intermediate readback.
            if (str(err) == 'input_cohort_pending' and self.storage_ready
                and self.market_fingerprint() is not None
                and all(s._attributes.get('result_current') is True
                        and s._attributes.get('recalculation_pending') is False
                        for s in (self.rce,self.tariff))):
                return
            self.invalidate(str(err))
        except Exception:
            self.invalidate('pstryk_plan_failed')
            _LOGGER.exception('Pstryk joint plan failed')

    def publish_timeline(self,sensor):
        if self._accepted is None or not sensor._attributes.get('result_current') or not sensor._timeline_sensor: return
        data,plan,system = self._accepted
        role = 'rce' if sensor is self.rce else 'tariff'
        sensor._timeline_sensor.publish_current(timeline(data,plan,role,system),
            input_revision=sensor._input_revision.value,metadata=sensor._attributes,quality='partial')

    def market_fingerprint(self):
        if not self.active or self._closed or self._key is None or self._accepted is None:
            return None
        now = dt_util.utcnow()
        view = self.cache.view(now=now)
        if (view.snapshot is None or view.snapshot.at(now) is None
            or self._accepted_source != (self.profile.revision, view.snapshot.revision)
            or self._calculated_at is None or not 0 <= (now-self._calculated_at).total_seconds() <= 120
            or not self._accepted[0].slots[0].start <= now < self._accepted[0].slots[0].end
            or not all(self.hass.states.is_state(e, 'Pstryk') for e in (SALE, PURCHASE))):
            return None
        try:
            if self._options() != self._settling_options:
                return None
            return self._settling_basis
        except (ValueError, TypeError):
            return None

    def price_schedule(self):
        now = dt_util.utcnow()
        view = self.cache.view(now=now)
        snapshot = view.snapshot
        rows = tuple(TariffPriceInterval(max(row.start,now),row.end,float(row.net),'pstryk')
                     for row in snapshot.hours if row.net is not None and row.end>now) if snapshot else ()
        complete = bool(rows and snapshot.covers(now,snapshot.window_end))
        return TariffPriceScheduleSnapshot(now,now,snapshot.window_end if snapshot else now,
            rows[0].start_utc if rows else None,rows[-1].end_utc if rows else None,rows,
            'public_dynamic','pstryk','https://www.pstryk.pl/ceny',snapshot.revision if snapshot else 'unavailable',
            'complete' if complete else 'partial' if rows else 'unavailable',
            () if complete else ('prices_incomplete',), 'public_net_same_buy_sell',view.quality=='cached')

    async def close(self):
        self._closed = True
        if self.active: self.invalidate('unloaded')
        for unsub in self._unsubs: unsub()
        self._unsubs.clear()
        tasks = tuple(self._tasks)
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        if self._client: await self._client.close()
        if self._session: await self._session.close()


class PstrykPriceSensor(SensorEntity):
    # Full public table is live-only; Recorder keeps the tiny hourly scalar
    # projection. The bounded Store owns restart persistence.
    _unrecorded_attributes = frozenset({'hours'})
    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_name = 'Pstryk — cena netto'
    _attr_native_unit_of_measurement = 'PLN/kWh'
    _attr_icon = 'mdi:currency-usd'

    def __init__(self, coordinator):
        self.coordinator = coordinator
        self._attr_unique_id = f'{coordinator.entry.entry_id}_pstryk_public_net_price'
        self.entity_id = PRICE_ENTITY_ID
        self._projection = {}

    @property
    def suggested_object_id(self): return 'hoymiles_hit_pstryk_public_net_price'

    @property
    def device_info(self): return self.coordinator.rce.device_info

    @property
    def native_value(self): return self._projection.get('net_pln_kwh')

    @property
    def extra_state_attributes(self):
        view = self.coordinator.cache.view(now=dt_util.utcnow())
        return {**self._projection,'hours':[h.as_row() for h in view.snapshot.hours] if view.snapshot else []}

    async def async_added_to_hass(self):
        await super().async_added_to_hass()
        await self.coordinator.start()

    async def async_will_remove_from_hass(self):
        await self.coordinator.close()
        await super().async_will_remove_from_hass()

    @callback
    def publish(self):
        candidate = price_projection(self.coordinator.cache.view(now=dt_util.utcnow()),now=dt_util.utcnow())
        if candidate!=self._projection:
            self._projection = candidate
            if self.entity_id: self.async_write_ha_state()
