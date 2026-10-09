"""Translate the existing qualified EMS inputs and a single joint result.

The RCE input adapter continues to own LOAD history, forecast learning,
freshness, entry binding and GCF policy. This module starts no second model.
"""
from dataclasses import asdict, replace
from datetime import timedelta, timezone
import hashlib
import json
import math
from zoneinfo import ZoneInfo

from .pstryk_joint import EnergySlot, JointInput, simulate, revalidate_plan
from .pstryk_prices import PRICE_BASIS
from .load_model import expected_load_by_slot
from .tariff_optimizer import TariffActiveCommitment, ACTIVE_COMMITMENT_PHYSICAL_MAX_AGE_SECONDS
from .rce_optimizer import (
    _horizon_end, _optimize_rce_impl, _bms_dc_power_limit_kw,
    _bms_charge_dc_power_limit_kw, _quantize_4306_percent,
    retain_active_rce_slot, RceActiveCommitment, floor_half_hour,
)
from .automation_plan_timeline import (
    OptimizerTimelineTrace, TimelineTracePoint, RCEPolicyPoint, TariffPolicyPoint,
)
WARSAW = ZoneInfo('Europe/Warsaw')

SALE_REASON_TEXT = {
    'pl': {
        'sale_disabled': 'Sprzedaż wyłączona w ustawieniach',
        'export_blocked': 'Brak sprzedaży — eksport do sieci jest zablokowany',
        'discharge_power_unavailable': 'Brak sprzedaży — brak dostępnej mocy rozładowania magazynu',
        'sale_hours_blocked': 'Brak sprzedaży — dostępne godziny objęte są blokadą sprzedaży',
        'sale_price_too_low': 'Brak sprzedaży — cena nie pokrywa strat i kosztu zużycia magazynu',
        'pv_uses_inverter_power': 'Brak sprzedaży z magazynu — produkcja PV wykorzystuje dostępną moc falownika',
        'sale_below_minimum_power': 'Brak sprzedaży — nadwyżka jest za mała dla ustawionej minimalnej mocy eksportu',
        'sale_below_minimum_profit': 'Brak sprzedaży — przewidywany zysk jest niższy od ustawionego minimum',
        'conservative_forecast': 'Brak sprzedaży — przy ostrożnej prognozie PV energia może być potrzebna dla domu',
        'home_energy_needed': 'Brak sprzedaży — energia w magazynie jest potrzebna dla domu i wymaganej rezerwy',
    },
    'en': {
        'sale_disabled': 'Battery sale disabled in settings',
        'export_blocked': 'No sale — grid export is blocked',
        'discharge_power_unavailable': 'No sale — battery discharge power is unavailable',
        'sale_hours_blocked': 'No sale — the available hours are blocked for sale',
        'sale_price_too_low': 'No sale — the price does not cover losses and battery wear',
        'pv_uses_inverter_power': 'No battery sale — PV uses the available inverter power',
        'sale_below_minimum_power': 'No sale — surplus is too small for the minimum export power setting',
        'sale_below_minimum_profit': 'No sale — expected profit is below the configured minimum',
        'conservative_forecast': 'No sale — the conservative PV forecast may require the energy for the home',
        'home_energy_needed': 'No sale — battery energy is needed for the home and required reserve',
    },
}


def compatibility_rows(snapshot, local_zone):
    """Internal price-slot geometry for the established input preflight.

Not PSE observations and never published as PSE states. Validation of the
public frames precedes this lossless expansion of hourly net PLN/kWh.
"""
    rows = []
    for frame in snapshot.hours:
        if frame.net is None:
            continue
        for quarter in range(4):
            start = frame.start+timedelta(minutes=15*quarter)
            rows.append({'dtime_utc':(start+timedelta(minutes=15)).isoformat(),
                         'business_date':start.astimezone(local_zone).date().isoformat(),
                         'rce_pln':float(frame.net)*1000})
    return rows


def build_joint_input(settings, options, *, pv_origin_kwh=None, forecast_end=None, active_pv_delay=None):
    now = settings.now.astimezone(timezone.utc)
    if active_pv_delay is not None:
        if (not options.get('allow_delay') or not options.get('allow_sell')
            or not active_pv_delay.start <= now < active_pv_delay.end):
            raise ValueError('pv_delay_binding_invalid')
        # Only an already-bound PV window can use this forecast continuation.
        # Default BUY/SELL and new-window selection keep their live-power input.
        settings = replace(settings, current_load_power_kw=None, current_pv_power_kw=None)
    prices = sorted((p for p in settings.price_slots if p.start.astimezone(timezone.utc)+timedelta(minutes=30)>now),
                    key=lambda p:p.start.astimezone(timezone.utc))
    starts = [p.start for p in prices]
    if not starts: raise ValueError('current_price_missing')
    if starts[0].astimezone(timezone.utc)>now: raise ValueError('current_price_missing')
    tail=[]
    cursor=starts[-1].astimezone(timezone.utc)+timedelta(minutes=30)
    tail_end=_horizon_end(settings).astimezone(timezone.utc)
    while cursor<tail_end:
        tail.append(cursor.astimezone(settings.now.tzinfo))
        cursor+=timedelta(minutes=30)
    if len(tail)>48: raise ValueError('unpriced_tail_too_long')
    display_starts=[]
    cursor=starts[-1].astimezone(timezone.utc)+timedelta(minutes=30)
    # The caller supplies only an age-qualified forecast horizon. This
    # continuation does not participate in the priced optimizer or its floor.
    if forecast_end is not None:
        if forecast_end.tzinfo is None or forecast_end>now+timedelta(hours=49):
            raise ValueError('forecast_horizon_invalid')
        while cursor<forecast_end:
            display_starts.append(cursor.astimezone(settings.now.tzinfo))
            cursor+=timedelta(minutes=30)
    if len(starts)+len(display_starts)>100: raise ValueError('forecast_horizon_invalid')
    load = expected_load_by_slot(list(dict.fromkeys(starts+tail+display_starts)), now=settings.now,
        daily_energy_kwh=settings.average_daily_load_kwh,
        average_profile_30m_kwh=settings.load_profile_30m_kwh,
        weekday_profile_30m_kwh=settings.weekday_load_profile_30m_kwh,
        weekend_profile_30m_kwh=settings.weekend_load_profile_30m_kwh,
        night_energy_kwh=settings.average_night_load_kwh,
        night_start_minute=settings.night_start_minute, night_end_minute=settings.night_end_minute,
        current_day_energy_kwh=settings.actual_day_load_today_kwh,
        current_day_observed_at=settings.actual_day_load_observed_at,
        persistence_delta_kw=settings.persistence_delta_kw,
        persistence_observed_at=settings.persistence_observed_at).by_slot_kwh
    pv_map = settings.pv_by_slot_kwh
    sale_pv_map = settings.conservative_pv_by_slot_kwh
    if sale_pv_map is None: raise ValueError('conservative_pv_missing')
    # Expected PV already includes the qualified forecast adjustment. Purchase
    # economics uses it just like tariff charging. Sale/deferral protection
    # separately retains the lower forecast and the zero-PV stress guard.
    slots = []
    sale_pv = []
    for price in prices:
        start = price.start.astimezone(timezone.utc)
        end = start+timedelta(minutes=30)
        clipped = max(start,now)
        hours = (end-clipped).total_seconds()/3600
        load_kwh = load.get(price.start)
        if load_kwh is None: raise ValueError('load_slot_missing')
        pv_kwh = pv_map.get(price.start, 0.)
        if start <= now < end:
            if active_pv_delay is not None:
                load_kwh *= hours/.5
                pv_kwh *= hours/.5
            elif settings.current_load_power_kw is None or settings.current_pv_power_kw is None:
                raise ValueError('live_power_missing')
            else:
                load_kwh *= hours/.5
                pv_kwh = max(settings.current_pv_power_kw,0)*hours
        slots.append(EnergySlot(clipped,end,price.price_pln_kwh,load_kwh,pv_kwh,price.blocked))
        lower = max(sale_pv_map.get(price.start,0.),0.)
        if active_pv_delay is not None and start<=now<end:
            lower *= hours/.5
        sale_pv.append(pv_kwh if start<=now<end and active_pv_delay is None else min(pv_kwh,lower))
    capacity = settings.battery_capacity_kwh
    # Battery power register percentages use the battery system base; the
    # inverter AC bridge and BMS caps are separate limits.
    system = settings.inverter_power_kw*settings.inverter_count
    ac = (settings.inverter_ac_power_kw or settings.inverter_power_kw)*settings.inverter_count
    eta_charge = options['charge_efficiency']/100
    eta_out = settings.house_discharge_efficiency_percent/100
    eta_sell = settings.export_efficiency_percent/100
    if not (settings.bms_charge_data_fresh and settings.bms_discharge_data_fresh and settings.current_battery_soc_fresh):
        raise ValueError('physical_input_stale')
    voltage = settings.battery_voltage_v
    if voltage is None or voltage<=0: raise ValueError('battery_voltage_missing')
    charge = settings.bms_max_charge_current_a
    discharge = settings.bms_max_discharge_current_a
    if charge is None or discharge is None: raise ValueError('bms_limit_missing')
    charge_dc = _bms_charge_dc_power_limit_kw(settings)
    discharge_dc = _bms_dc_power_limit_kw(settings)
    charge_kw = min(ac,system*options['charge_power_percent']/100, charge_dc/eta_charge)
    discharge_kw = min(ac,system*settings.discharge_power_percent/100,discharge_dc*min(eta_out,eta_sell))
    # BUY and home consumption use physical Self-Use. RCE's additional SOC
    # protects only SELL, including its separate terminal and per-slot floors.
    reserve_percent = min(100,max(settings.outage_reserve_soc_percent,0.))
    sale_reserve_percent = max(reserve_percent, min(100,
        settings.outage_reserve_soc_percent+settings.safety_margin_soc_percent
        if settings.dynamic_reserve_enabled else settings.manual_minimum_soc_percent))
    maximum = options['maximum_soc']
    if maximum<reserve_percent: raise ValueError('maximum_below_reserve')
    export_cap = settings.export_power_cap_kw
    # In the qualified adapter None means physically verified GCF disabled;
    # missing/stale GCF is rejected before this function. AC rating still caps
    # export, and a reported zero remains a real zero.
    if export_cap is None: export_cap = ac
    if settings.effective_export_power_kw is not None:
        export_cap = min(export_cap,settings.effective_export_power_kw)
    terminal = capacity*reserve_percent/100
    sale_terminal = capacity*sale_reserve_percent/100
    pv_eff = settings.charge_efficiency_percent/100
    home_kw = min(ac,discharge_dc*eta_out)
    pv_charge_kw = min(ac,charge_dc/pv_eff)
    for start in reversed(tail):
        forecast_pv = pv_map.get(start,0.)
        demand = load[start]
        deficit = min(max(demand-forecast_pv,0),home_kw*.5)/eta_out
        refill = min(max(forecast_pv-demand,0)*pv_eff,pv_charge_kw*.5*pv_eff,charge_dc*.5)
        terminal = max(capacity*reserve_percent/100,terminal+deficit-refill)
        risk_pv = 0. if settings.critical_zero_pv_guard else min(forecast_pv,max(sale_pv_map.get(start,0.),0.))
        risk_deficit = min(max(demand-risk_pv,0),home_kw*.5)/eta_out
        risk_refill = min(max(risk_pv-demand,0)*pv_eff,pv_charge_kw*.5*pv_eff,charge_dc*.5)
        sale_terminal = max(capacity*sale_reserve_percent/100,sale_terminal+risk_deficit-risk_refill)
    # Bounded fixed-schedule physical preflight, without a sale search. Reuse
    # RCE's exact protected night, forecast horizon and register SOC floors.
    # Aggregate Master input has already passed the same qualified adapter.
    sale_base = _optimize_rce_impl(replace(settings,tariff_price_schedule=None,
        self_consumption_filter_enabled=False),fixed_exports={})
    if not sale_base.protected_sale_by_slot:
        raise ValueError('rce_sale_physics_missing')
    protected={start+timedelta(minutes=30):floor for start,floor in sale_base.protected_sale_by_slot.items()}
    if any(s.end not in protected for s in slots):
        raise ValueError('rce_sale_horizon_incomplete')
    sale_reserve=tuple(max(capacity*sale_reserve_percent/100,protected[s.end]) for s in slots)
    delay_pv=tuple(sale_pv) if (sale_base.critical_zero_pv_guard_active and
        (options.get('delay_refill_qualified') is True or options.get('delay_qualified_dates'))) else None
    if settings.delay_pv_by_slot_kwh is not None:
        delay_pv=tuple(s.pv_kwh if s.start <= now < s.end and active_pv_delay is None else
            min(s.pv_kwh, max(settings.delay_pv_by_slot_kwh.get(p.start, 0.), 0.)
                * (s.hours/.5 if s.start <= now < s.end else 1.))
            for s,p in zip(slots,prices))
    sale_pv=[min(s.pv_kwh,sale_base.conservative_sale_pv_by_slot[s.end-timedelta(minutes=30)]) for s in slots]
    # Standard dynamic sale uses the same existing physical stock as RCE.
    # Future BUY is separately bounded and never credited as sale inventory.
    stock=capacity*settings.battery_soc_percent/100
    eligible=stock if pv_origin_kwh is None else min(pv_origin_kwh,stock)
    return JointInput(tuple(slots),capacity,capacity*settings.battery_soc_percent/100,
        capacity*reserve_percent/100,capacity*maximum/100,
        eligible,
        charge_kw,discharge_kw,ac,export_cap,eta_charge,eta_out,
        settings.battery_wear_cost_pln_kwh,options['minimum_saving'],
        system/100,.2,options['demand_margin_percent'],options['allow_buy'],options['allow_sell'],
        pv_charge_efficiency=pv_eff,sell_efficiency=eta_sell,
        terminal_kwh=min(terminal,capacity*maximum/100),
        unpriced_home_shortfall_kwh=max(terminal-capacity*maximum/100,0),
        pv_charge_kw=pv_charge_kw,charge_dc_kw=charge_dc,home_discharge_kw=home_kw,
        allow_delay=options.get('allow_delay', False),
        zero_pv_sale_guard=bool(sale_base.critical_zero_pv_guard_active),
        delay_refill_qualified=(options.get('delay_refill_qualified') is True or bool(options.get('delay_qualified_dates'))),
        sale_terminal_kwh=min(sale_terminal,capacity*maximum/100),sale_pv_kwh=tuple(sale_pv),
        sale_reserve_kwh=sale_reserve,minimum_export_kw=settings.minimum_net_export_power_kw,
        delay_pv_kwh=delay_pv,
        forecast_charge_curve_kw=settings.forecast_charge_curve_kw,
        forecast_charge_after=slots[0].end,
        delay_qualified_dates=options.get('delay_qualified_dates'),
        current_load_power_kw=settings.current_load_power_kw,
        current_pv_power_kw=settings.current_pv_power_kw,
        forecast_tail=tuple(EnergySlot(t.astimezone(timezone.utc),
            min(t.astimezone(timezone.utc)+timedelta(minutes=30),forecast_end),None,
            load[t],max(pv_map.get(t,0.),0.),True) for t in display_starts))


def input_key(data, *, profile_revision, price_revision):
    # Only elapsed time inside the same unfinished slot is excluded. Before
    # commit the adapter validates freshness AGAIN and checks the slot end.
    payload = asdict(data)
    first = payload['slots'][0]
    hours = (first['end']-first['start']).total_seconds()/3600
    first['load_kwh'] = round(first['load_kwh']/hours,8)
    first['pv_kwh'] = round(first['pv_kwh']/hours,8)
    first['start'] = first['end']-timedelta(minutes=30)
    if payload.get('sale_pv_kwh') is not None:
        payload['sale_pv_kwh']=(round(payload['sale_pv_kwh'][0]/hours,8),*payload['sale_pv_kwh'][1:])
    if payload.get('delay_pv_kwh') is not None:
        payload['delay_pv_kwh']=(round(payload['delay_pv_kwh'][0]/hours,8),*payload['delay_pv_kwh'][1:])
    return hashlib.sha256(json.dumps((profile_revision,price_revision,payload),default=str,
                                    sort_keys=True,allow_nan=False).encode()).hexdigest()


def retain_active_plan(data, plan, settings, *, accepted, commitment):
    """Apply RCE's existing active-run policy, then attest the joint balance.

    The native routine owns proof age, deadline, unelapsed energy, physical
    caps and bounded allocation to later sales. Its output is only a ceiling:
    Pstryk rechecks BUY, home import and lower-PV economics on the same commands.
    No new search, writes, persistence or unconditional transaction latch.
    """
    if accepted is None or not isinstance(commitment, RceActiveCommitment):
        return plan
    _before_data, before_plan, before_settings = accepted
    if not data.allow_sell or before_plan.slots[0].action != 'sell':
        return plan
    # Pstryk's joint balance below owns BUY/SELL valuation. As in
    # build_joint_input, native RCE supplies physics without its tariff shadow.
    settings = replace(settings, tariff_price_schedule=None, self_consumption_filter_enabled=False)
    before_settings = replace(before_settings, tariff_price_schedule=None, self_consumption_filter_enabled=False)

    def native(source, selected):
        return _optimize_rce_impl(
            source,
            fixed_exports={floor_half_hour(p.start): p.battery_export_kwh
                           for p in selected.slots if p.action == 'sell'},
            # The joint row carries forecast energy, not instantaneous LOAD.
            # Reconstruct the physical ceiling with the same live cohort used
            # by RCE, rather than mistaking forecast gross power for an ACK.
            fixed_current_discharge_cap_kw=(min(
                source.inverter_power_kw * source.inverter_count * source.discharge_power_percent / 100.,
                max((source.current_load_power_kw or 0.)
                    - (source.current_pv_power_kw or 0.), 0.)
                + selected.slots[0].battery_export_kwh
                    / max((selected.slots[0].end.astimezone(timezone.utc)
                           - selected.slots[0].start.astimezone(timezone.utc)).total_seconds()/3600., 1e-8),
            ) if selected.slots[0].action == 'sell' else None),
        )

    retained = retain_active_rce_slot(
        settings, native(settings, plan), accepted_settings=before_settings,
        accepted_result=native(before_settings, before_plan), commitment=commitment,
    )
    if not retained.active_slot_commitment_applied:
        return plan
    exports = {p.start: p.energy_kwh for p in retained.planned_exports}
    exports = {s.start: exports[floor_half_hour(s.start)] for s in data.slots
               if floor_half_hour(s.start) in exports}
    # Keep the physical floor of the incumbent even if a fresh forecast lowers
    # the home's reserve. This changes only the attested plan's output points.
    floor = commitment.minimum_soc_percent * data.capacity_kwh / 100.
    guarded = replace(data, continuing_action='sell',
        continuing_until=min(data.slots[0].end, commitment.hard_deadline),
        sale_reserve_kwh=tuple(max(v, floor) if s.start < commitment.hard_deadline else v
            for s, v in zip(data.slots, data.sale_reserve_kwh)))
    actions = tuple(-2 if s.start in exports else max(a, 0)
                    for s, a in zip(data.slots, plan.action_levels))
    points, cost = simulate(guarded, actions, fixed_points=plan.slots, exports=exports)
    trial = replace(plan, slots=points, cost_pln=cost, action_levels=actions,
                    active_slot_commitment_applied=True, sale_reason='sale_planned',
                    active_run_deadline=retained.current_run_end)
    checked = revalidate_plan(guarded, trial, captured=guarded)
    if (checked is None or checked.slots[0].action != 'sell'
        or checked.slots[0].command_kw > retained.current_slot_execution_discharge_power_kw + 1e-8
        or checked.slots[0].battery_export_kwh / data.slots[0].hours
            + 1e-8 < data.minimum_export_kw):
        return plan
    return checked


def retain_active_buy_plan(data, plan, settings, *, accepted, commitment):
    """Revalidate the remaining accepted BUY run against fresh house need.

    Retain only an equally good or better balance, within the original price,
    power, energy, SOC and deadline ceilings. A physical commitment is mandatory.
    """
    c = commitment
    if accepted is None or not isinstance(c, TariffActiveCommitment):
        return plan
    before, selected, _ = accepted
    now = data.slots[0].start
    if (not c.transaction_id or c.action not in ('grid_support','battery_charge','grid_support_and_charge')
        or any(d.tzinfo is None or d.utcoffset() is None for d in (now,c.started_at,c.physical_verified_at,c.hard_deadline))
        or not c.started_at <= c.physical_verified_at <= now < c.hard_deadline
        or (now-c.physical_verified_at).total_seconds() > ACTIVE_COMMITMENT_PHYSICAL_MAX_AGE_SECONDS
        or not data.allow_buy or data.charge_kw <= 0 or selected.slots[0].action != 'buy'
        or any(getattr(data,k) != getattr(before,k) for k in (
            'capacity_kwh','reserve_kwh','maximum_kwh','charge_efficiency','discharge_efficiency',
            'wear_pln_kwh','minimum_benefit_pln','demand_margin_percent','power_step_kw'))):
        return plan
    original_end = selected.slots[0].end
    for row in selected.slots[1:]:
        if row.action != 'buy' or row.start != original_end:
            break
        original_end = row.end
    if c.hard_deadline > original_end:
        return plan
    old = {p.end:(p,a) for p,a in zip(selected.slots,selected.action_levels)}
    fixed, actions, held = list(plan.slots), list(plan.action_levels), []
    power_cap = settings.inverter_power_kw*settings.inverter_count*c.maximum_charge_power_percent/100
    if not math.isfinite(power_cap) or power_cap <= 0:
        return plan
    for i,s in enumerate(data.slots):
        if s.start >= c.hard_deadline:
            break
        pair = old.get(s.end)
        if pair is None or s.end > c.hard_deadline:
            return plan
        row, level = pair
        if row.action != 'buy' or row.net != s.net:
            return plan
        fraction = (s.end-s.start).total_seconds()/(row.end-row.start).total_seconds()
        if not 0 < fraction <= 1:
            return plan
        fixed[i] = replace(row,start=s.start,command_kw=min(row.command_kw,power_cap),
            grid_charge_kwh=row.grid_charge_kwh*fraction)
        actions[i] = level
        held.append(i)
    if not held:
        return plan
    guarded = replace(data,continuing_action='buy',continuing_until=min(data.slots[0].end,c.hard_deadline))
    points,cost = simulate(guarded,tuple(actions),fixed_points=tuple(fixed))
    trial = replace(plan,slots=points,cost_pln=cost,action_levels=tuple(actions),
        active_slot_commitment_applied=True,active_run_deadline=c.hard_deadline)
    checked = revalidate_plan(guarded,trial,captured=guarded)
    ceiling = max(data.initial_kwh, c.target_soc_percent*data.capacity_kwh/100)
    if (checked is None or any(checked.slots[i].action != 'buy' for i in held)
        or checked.cost_pln > plan.cost_pln+1e-8
        or checked.base_shortfall_kwh > plan.base_shortfall_kwh+1e-8
        or any(checked.slots[i].end_kwh > ceiling+1e-8 for i in held)):
        return plan
    return checked


def execution_metadata(settings):
    """Publish the RCE execution contract from the freshly revalidated input.

    The optimizer's requested/selected power is not a substitute for the
    physical BMS limit. Existing HA templates require both independently.
    """
    system = settings.inverter_power_kw * settings.inverter_count
    requested = system * _quantize_4306_percent(settings.discharge_power_percent) / 100.
    bms = _bms_dc_power_limit_kw(settings) * min(max(settings.export_efficiency_percent, 0.), 100.) / 100.
    limits = [('requested_power', requested), ('bms', bms)]
    for name, value in (('gcf_export_cap', settings.export_power_cap_kw),
                        ('effective_export_power', settings.effective_export_power_kw)):
        if value is not None:
            limits.append((name, max(value, 0.)))
    return {
        'bms_discharge_power_limit_kw': round(bms, 2),
        'bms_discharge_limit_percent': round(min(max(bms / system * 100., 0.), 100.), 1),
        'bms_limit_active': bms < requested - .001,
        'physical_limit_source': min(limits, key=lambda item: item[1])[0],
    }


def projections(data, plan, *, revision, metadata, system_power_kw):
    current = plan.slots[0]
    buy, sell = current.action=='buy',current.action=='sell'
    end = current.end
    # Like RCE, BUY and SELL cover the confirmed contiguous run, including
    # accepted adjacent hourly prices.
    # Later replans cannot extend the original execution hard deadline.
    run = [current]
    for row in plan.slots[1:]:
        if row.action != current.action or row.start != end: break
        if (plan.active_slot_commitment_applied and plan.active_run_deadline is not None
            and row.start >= plan.active_run_deadline): break
        run.append(row); end = row.end
    if plan.active_slot_commitment_applied and plan.active_run_deadline is not None:
        end = min(end, plan.active_run_deadline)
    minutes = (end-current.start).total_seconds()/60
    target = min(data.maximum_kwh, max(p.end_kwh for p in run))/data.capacity_kwh*100
    target_percent = math.floor(target+1e-8)
    reserve_percent = data.reserve_kwh/data.capacity_kwh*100
    required_top_up = buy and plan.required_charge
    # Retain the modeled trajectory, but never claim execution readiness for
    # a run that cannot reach the executor's protected integer SOC target.
    reserve_shortfall = max((math.ceil(reserve_percent-1e-8)-target_percent)*data.capacity_kwh/100,0.)
    reserve_blocked = buy and (reserve_shortfall>1e-8
        or required_top_up and current.grid_charge_kwh<=1e-8)
    buy_ready = buy and not reserve_blocked
    buy_reason = 'reserve_target_unreachable' if reserve_blocked else None if buy else 'no_current_plan'
    # The physical stock and protected home floor also bound execution,
    # independently of future forecast or executor delay.
    protected = max(current.protected_kwh,data.initial_kwh-data.pv_origin_kwh)
    floor_percent = math.ceil(protected/data.capacity_kwh*100-1e-8)
    # The nominal trajectory stays on the common LOAD model. Execution uses
    # the latest qualified raw power without projecting a pulse into reserves.
    live_load = (data.current_load_power_kw if data.current_load_power_kw is not None
                 else current.load_kwh/data.slots[0].hours)
    live_pv = (data.current_pv_power_kw if data.current_pv_power_kw is not None
               else current.pv_kwh/data.slots[0].hours)
    live_deficit = max(live_load-live_pv, 0.)
    live_surplus = max(live_pv-live_load, 0.)
    available_dc_kw = max(data.initial_kwh-data.capacity_kwh*floor_percent/100, 0.) / data.slots[0].hours
    house_ac_kw = min(live_deficit, available_dc_kw*data.discharge_efficiency)
    energy_discharge_kw = house_ac_kw + max(available_dc_kw-house_ac_kw/data.discharge_efficiency, 0.)*data.sell_efficiency
    sell_command = min(data.discharge_kw,
                       energy_discharge_kw,
                       live_deficit+current.battery_export_kwh/data.slots[0].hours,
                       max(data.ac_kw-min(live_pv,live_load), 0.),
                       live_deficit+max(data.export_kw-live_surplus, 0.)) if sell else 0.
    sell_percent = math.floor(sell_command/system_power_kw*100+1e-8)
    sell_command = sell_percent*system_power_kw/100
    sell_export = max(sell_command-live_deficit, 0.)
    sell_ready = sell and sell_export+1e-8 >= data.minimum_export_kw and sell_export > 1e-8
    buy_charge = max(current.command_kw-live_deficit, 0.) if buy else 0.
    if buy and ((current.grid_charge_kwh>1e-8 and buy_charge+1e-8<data.minimum_power_kw)
                or (current.grid_charge_kwh<=1e-8 and live_deficit<=1e-8)):
        buy_ready, buy_reason = False, 'live_power_insufficient'
    common = {**metadata,'price_provider':'Pstryk','price_basis':PRICE_BASIS,
        'solver_method':('active_slot_fixed_schedule' if plan.active_slot_commitment_applied
                         else 'rce_shared_bounded_active_set'),
        'active_slot_commitment_applied':plan.active_slot_commitment_applied,
        'battery_stock_basis':'qualified_system_soc',
        'joint_plan_revision':revision,'joint_benefit_pln':round(plan.benefit_pln,4),
        'result_current':True,'recalculation_pending':False,'status_code':'ready',
        'missing_entities':[],'current_price_pln_kwh':current.net,
        'system_power_kw':system_power_kw,'battery_soc_percent':data.initial_kwh/data.capacity_kwh*100,
        'current_slot_end':current.end.isoformat(),'current_run_end':end.isoformat(),
        'forecast_data_fresh':True,'control_inputs_fresh':True,'control_input_block_reason':None,
        'source_scope':'pstryk_public_net','ending_battery_soc_percent':plan.slots[-1].end_kwh/data.capacity_kwh*100}
    rce = {**common,'current_slot_planned':sell,
        'sale_decision_reason':plan.sale_reason,
        'current_slot_start_eligible':sell_ready and minutes>=7 and data.continuing_action!='sell'
            and not plan.active_slot_commitment_applied,
        'current_slot_continue_eligible':sell_ready,
        'current_slot_suppression_reason':None if sell_ready else 'live_power_insufficient' if sell else 'no_current_plan',
        'current_slot_execution_discharge_power_kw':sell_command,
        # The Supervisor's common minimum-grid-power guard needs net export,
        # independently of gross inverter discharge (which also feeds home).
        'current_slot_execution_export_power_kw':sell_export,
        'current_slot_execution_power_percent':sell_percent,
        'effective_discharge_power_percent':sell_percent,
        'current_slot_planned_export_kwh':current.battery_export_kwh,
        'current_required_minimum_soc_percent':floor_percent,'minimum_soc_percent':floor_percent,
        'minimum_soc':floor_percent,
        'current_slot_load_exhausts_requested_discharge_budget':False,
        'current_slot_load_only_export_suppressed':False,
        'planned_export_kwh':sum(p.battery_export_kwh for p in plan.slots),
        'expected_revenue_pln':sum(p.battery_export_kwh*p.net for p in plan.slots),
        'planned_revenue_pln':sum(p.battery_export_kwh*p.net for p in plan.slots),
        # Combined benefit belongs to one joint result; never add two gains.
        'optimization_gain_pln':plan.benefit_pln,'optimization_gain_scope':'joint_pstryk',
        'planned_slots':[{'date':p.start.astimezone(WARSAW).date().isoformat(),
            'start':p.start.astimezone(WARSAW).strftime('%H:%M'),'end':p.end.astimezone(WARSAW).strftime('%H:%M'),
            'start_utc':p.start.isoformat(),'end_utc':p.end.isoformat(),
            'price':p.net,'export_kwh':p.battery_export_kwh,'energy':p.battery_export_kwh,
            'revenue':p.battery_export_kwh*p.net} for p in plan.slots if p.action=='sell']}
    tariff = {**common,'current_slot_planned':buy,'current_action':
        ('grid_support_and_charge' if current.grid_charge_kwh>1e-8 else 'grid_support') if buy else 'none',
        'current_run_need_class':('required_energy' if plan.required_charge else 'economic') if buy else 'none',
        'current_run_start_eligible':buy_ready and minutes>=7 and data.continuing_action!='buy'
            and not plan.active_slot_commitment_applied,'current_run_continue_eligible':buy_ready,
        'current_run_suppression_reason':buy_reason,
        'current_run_continue_reason':buy_reason,
        'current_run_target_shortfall_kwh':reserve_shortfall if buy else 0.,
        'requested_charge_power_kw':current.command_kw if buy else 0.,
        'command_charge_power_percent':round(current.command_kw/system_power_kw*100) if buy else 0.,
        'current_slot_planned_import_power_kw':current.grid_import_kwh/((current.end-current.start).total_seconds()/3600) if buy else 0.,
        'current_slot_planned_charge_power_kw':buy_charge,
        'current_run_grid_import_kwh':sum(p.grid_import_kwh for p in run) if buy else 0.,
        'current_run_stored_kwh':sum(p.grid_charge_kwh*data.charge_efficiency for p in run) if buy else 0.,
        'current_run_direct_load_kwh':sum(p.grid_import_kwh-p.grid_charge_kwh for p in run) if buy else 0.,
        'current_run_benefit_pln':plan.benefit_pln if buy else 0.,
        'current_run_remaining_minutes':minutes if buy else 0.,
        'current_grid_charge_run_end':end.isoformat() if buy else None,
        'target_soc_percent':target_percent,'base_reserve_soc_percent':reserve_percent,
        'requested_target_energy_kwh':max(target/100*data.capacity_kwh-data.initial_kwh,0),
        'demand_margin_requested_kwh':plan.margin_requested_kwh,
        'demand_margin_unserved_kwh':plan.margin_unserved_kwh,'base_energy_shortfall_kwh':plan.base_shortfall_kwh,
        'current_zone':'Pstryk','tariff_operator':'Pstryk','tariff_type':'Pstryk',
        'planned_grid_import_kwh':sum(p.grid_import_kwh for p in plan.slots if p.action=='buy'),
        # One combined benefit is exposed explicitly; a second legacy tariff
        # gain would let dashboards accidentally add the same benefit twice.
        'estimated_savings_pln':None,'estimated_savings_scope':'joint_pstryk',
        'planned_slots':[{'date':p.start.astimezone(WARSAW).date().isoformat(),
            'start':p.start.astimezone(WARSAW).strftime('%H:%M'),'end':p.end.astimezone(WARSAW).strftime('%H:%M'),
            'start_utc':p.start.isoformat(),'end_utc':p.end.isoformat(),'zone':'Pstryk','price':p.net,
            'action':'grid_support_and_charge' if p.grid_charge_kwh>1e-8 else 'grid_support',
            'grid_import_kwh':p.grid_import_kwh,'stored_energy_kwh':p.grid_charge_kwh*data.charge_efficiency,
            'direct_load_kwh':p.grid_import_kwh-p.grid_charge_kwh,
            'target_soc_percent':math.floor(p.end_kwh/data.capacity_kwh*100+1e-8)}
            for p in plan.slots if p.action=='buy']}
    from .pv_charge_delay import attributes,no_window_reason
    rce.update(attributes(plan.delay_plan, enabled=data.allow_delay, now=current.start,
        reason=no_window_reason(data,plan.slots)))
    return rce,tariff


def timeline(data, plan, role, system_power_kw):
    points=[]
    baseline,_ = simulate(data,(0,)*len(data.slots))
    planned=plan.slots
    if data.forecast_tail:
        if data.forecast_tail[0].start!=planned[-1].end:
            raise ValueError('forecast_tail_gap')
        def continuation(previous):
            # Reuse the same physical energy balance, starting from this
            # trajectory's own terminal SOC. No BUY/SELL/hold is permitted.
            observer=replace(data,slots=data.forecast_tail,forecast_tail=(),
                initial_kwh=previous.end_kwh,pv_origin_kwh=previous.pv_origin_kwh,
                allow_buy=False,allow_sell=False,allow_delay=False,
                continuing_action=None,continuing_until=None,terminal_kwh=data.reserve_kwh,
                sale_terminal_kwh=None,sale_pv_kwh=None,sale_reserve_kwh=None,delay_pv_kwh=None,zero_pv_sale_guard=False)
            return simulate(observer,(0,)*len(observer.slots))[0]
        planned=planned+continuation(planned[-1])
        baseline=baseline+continuation(baseline[-1])
    for p,base in zip(planned,baseline):
        selected = p.action in ('sell', 'pv_charge_hold') if role=='rce' else p.action=='buy'
        action = 'pv_charge_hold' if p.action=='pv_charge_hold' else 'export' if p.action=='sell' else ('grid_support_and_charge' if p.grid_charge_kwh else 'grid_support') if p.action=='buy' else 'idle'
        policy = (RCEPolicyPoint(p.net,p.battery_export_kwh,p.battery_export_kwh/data.sell_efficiency,
                    p.command_kw if selected else 0.,p.command_kw/system_power_kw*100 if selected else 0.,.1,p.battery_export_kwh*p.net if p.net is not None else None)
                  if role=='rce' else TariffPolicyPoint(p.net,'pstryk',p.grid_import_kwh if selected else 0.,
                    p.grid_charge_kwh*data.charge_efficiency,p.grid_import_kwh-p.grid_charge_kwh if selected else 0.,
                    p.command_kw if selected else 0.,(p.grid_import_kwh*p.net if selected else 0.) if p.net is not None else None,None,'economic' if selected else 'none'))
        points.append(TimelineTracePoint(p.start,p.end,p.pv_kwh-p.curtailed_kwh,p.load_kwh,
            p.end_kwh-p.start_kwh,p.grid_import_kwh,p.grid_export_kwh,p.end_kwh/data.capacity_kwh*100,
            base.end_kwh/data.capacity_kwh*100,
            # Every published slot has qualified prices, flows and both SOC
            # trajectories. The horizon can still be partial (tomorrow's
            # prices pending); that does not make these known slots incomplete.
            p.protected_kwh/data.capacity_kwh*100,action if selected else 'idle',selected,'complete',policy,
            target_soc_percent=(math.floor(p.end_kwh/data.capacity_kwh*100+1e-8)
                if role=='tariff' and selected else None)))
    return OptimizerTimelineTrace(role,tuple(points),'partial')


def live_load_only_export_suppressed(data, plan, previous_data, previous_plan, system_power_kw):
    """Qualify the live-power case without changing the nominal shared LOAD.

    A valid joint SELL can remain planned while only its current LOAD cap
    falls below minimum export. Replacing that one sample must restore
    execution on the SAME fresh plan, SOC, reserve and capabilities.
    """
    from .pstryk_settling import market_basis

    try:
        current, old = data.slots[0], previous_data.slots[0]
        if (
            plan.slots[0].action != 'sell' or previous_plan.slots[0].action != 'sell'
            or current.end != old.end or not old.start <= current.start < current.end
            or current.net <= 0 or current.sale_blocked or not data.allow_sell
            or plan.required_charge or plan.base_shortfall_kwh > 1e-8
            or plan.benefit_pln <= 1e-8
            or market_basis(data, 0, '') != market_basis(previous_data, 0, '')
            or any(type(v) not in {int, float} or not math.isfinite(v) or v < 0
                   for v in (data.current_load_power_kw, data.current_pv_power_kw,
                             previous_data.current_load_power_kw))
            or data.current_load_power_kw <= previous_data.current_load_power_kw
        ):
            return False
        observed, _ = projections(data, plan, revision=0, metadata={}, system_power_kw=system_power_kw)
        alternative, _ = projections(
            replace(data, current_load_power_kw=previous_data.current_load_power_kw),
            plan, revision=0, metadata={}, system_power_kw=system_power_kw,
        )
        return bool(observed['current_slot_suppression_reason'] == 'live_power_insufficient'
                    and not observed['current_slot_continue_eligible']
                    and alternative['current_slot_continue_eligible'])
    except (AttributeError, TypeError, ValueError, OverflowError, ZeroDivisionError, IndexError):
        return False
