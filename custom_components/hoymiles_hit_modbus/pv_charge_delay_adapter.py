"""Reuse the qualified RCE inputs; do not open another Recorder/model path."""
from dataclasses import replace
from datetime import datetime, timezone

from .pstryk_plan import build_joint_input
from .pstryk_joint import simulate, _validate
from .rce_optimizer import (_bms_charge_dc_power_limit_kw, _slot_export_limit_kwh,
                            _quantize_4306_percent, floor_half_hour)
from .pv_charge_delay import optimize_delay, attributes, stabilize_attributes, qualified_today_refill, qualified_refill_dates, active_delay_plan, HELPER, WARSAW


def rce_delay(settings, result, *, enabled, tariff_enabled=False, tariff_attributes=None,
              refill_qualified=False, refill_dates=None, active_plan=None):
    if not enabled:
        return None
    if settings.critical_zero_pv_guard and not refill_qualified and not refill_dates:
        return None
    if any(not hasattr(p,'energy_kwh') for p in result.planned_exports):
        return None
    if result.current_slot_planned_export_kwh > 0:
        future_dates=tuple(sorted({p.start.astimezone(WARSAW).date().isoformat()
            for p in settings.price_slots if p.start.astimezone(WARSAW).date()>settings.now.astimezone(WARSAW).date()}))
        refill_dates=tuple(d for d in future_dates if refill_dates is None or d in refill_dates)
        if not refill_dates:
            return None
    # A fixed RCE or tariff action is a barrier: restore the original battery
    # BEFORE that action, preserving its existing energy and import decision.
    boundaries = [p.start.astimezone(timezone.utc) for p in result.planned_exports
                  if p.start.astimezone(timezone.utc) >= settings.now.astimezone(timezone.utc)]
    tariff_boundaries=[]
    if tariff_enabled:
        attrs = tariff_attributes or {}
        if attrs.get('result_current') is not True or attrs.get('recalculation_pending') is not False:
            return None
        if attrs.get('current_slot_planned') is True:
            return None
        if not isinstance(attrs.get('planned_slots'), (list, tuple)):
            return None
        for slot in attrs['planned_slots']:
            try:
                if slot.get('start_utc'):
                    start = datetime.fromisoformat(slot['start_utc'])
                else:
                    # Classic tariff publishes local date/HH:MM, while Pstryk
                    # also publishes UTC. Ambiguous DST hours are conservative
                    # barriers before this morning feature can start.
                    start = datetime.fromisoformat(slot['date']+'T'+slot['start']).replace(tzinfo=settings.now.tzinfo)
                if start.tzinfo is None:
                    return None
                if start >= settings.now:
                    tariff_boundaries.append(start.astimezone(timezone.utc))
            except (KeyError, TypeError, ValueError):
                return None
    # Classic RCE deferral retains its conservative forecast, independently
    # of the expected-PV balance used for Pstryk purchases.
    lower = settings.delay_pv_by_slot_kwh
    if lower is None:  # compatibility for callers predating per-day qualification
        lower = settings.conservative_pv_by_slot_kwh
    data = build_joint_input(replace(settings,pv_by_slot_kwh=lower), {
        'maximum_soc': 100., 'charge_power_percent': 100.,
        'charge_efficiency': settings.charge_efficiency_percent,
        'minimum_saving': .05, 'demand_margin_percent': 0.,
        'allow_buy': False, 'allow_sell': True, 'allow_delay': True,
        'delay_refill_qualified': refill_qualified,
        'delay_qualified_dates': refill_dates,
    }, pv_origin_kwh=0., active_pv_delay=active_plan)
    if tariff_boundaries:
        first = min(tariff_boundaries)
        slots=tuple(s for s in data.slots if s.end <= first)
        data = replace(data, slots=slots,sale_pv_kwh=data.sale_pv_kwh[:len(slots)],
            sale_reserve_kwh=data.sale_reserve_kwh[:len(slots)],
            delay_pv_kwh=data.delay_pv_kwh[:len(slots)] if data.delay_pv_kwh is not None else None)
    if not data.slots:
        return None
    data = replace(data, active_delay=active_plan)
    _validate(data)
    # Preserve today's committed sale when forecasting tomorrow's starting SOC.
    exports={p.start.astimezone(timezone.utc):getattr(p,'energy_kwh',0.) for p in result.planned_exports}
    if result.current_slot_planned_export_kwh > 0:
        exports[data.slots[0].start]=result.current_slot_planned_export_kwh
        boundaries.append(data.slots[0].start)
    actions=tuple(-2 if exports.get(s.start,0)>0 else 0 for s in data.slots)
    # Fixed RCE sales already use whole-percent commands and share BMS DC
    # between house/export at their separate efficiencies. Applying Pstryk's
    # aggregate min-efficiency cap and quantizing again can reject even the
    # unchanged baseline sale. Recheck every sale with RCE's native power
    # limits, then replay its exact energy; storage/reserve checks stay below.
    for slot in data.slots:
        requested = exports.get(slot.start, 0.)
        if requested > _slot_export_limit_kwh(settings, slot.load_kwh,
                slot.pv_kwh, slot.hours / .5,
                slot_start=floor_half_hour(slot.start)) + 1e-6:
            return None
    data = replace(data, discharge_kw=settings.inverter_power_kw * settings.inverter_count
        * _quantize_4306_percent(settings.discharge_power_percent) / 100.)
    points, _ = simulate(data, actions, exports=exports, continuous_exports=True)
    if any(p.battery_export_kwh+1e-6<exports.get(p.start,0.) for p in points):
        return None
    _, plan = optimize_delay(data, points,blocked_starts=frozenset(boundaries))
    return plan


def update_rce_attributes(sensor, settings, result):
    sensor._pv_delay_timeline_trace = None
    sensor._pv_delay_timeline_result = result
    enabled = sensor.hass.states.is_state(HELPER, 'on')
    allowed = enabled and sensor.hass.states.is_state('input_boolean.hoymiles_rce_discharge_enabled', 'on')
    state = sensor.hass.states.get('sensor.hoymiles_hit_tariff_charge_plan')
    plan = None
    try:
        tariff_on = sensor.hass.states.is_state('input_boolean.hoymiles_tariff_charge_enabled', 'on')
        if tariff_on and (state is None or state.last_reported.tzinfo is None
                or not 0 <= (settings.now-state.last_reported).total_seconds() <= 120):
            raise ValueError('tariff_plan_stale')
        reader = getattr(sensor, '_active_pv_delay_commitment', None)
        proof = reader(settings.now) if callable(reader) else None
        active = active_delay_plan(getattr(sensor, '_pv_delay_accepted_plan', None), proof, settings.now)
        plan = rce_delay(settings, result, enabled=allowed, active_plan=active,
            tariff_enabled=tariff_on,
            tariff_attributes=state.attributes if state else None,
            refill_qualified=qualified_today_refill(sensor._attributes),
            refill_dates=qualified_refill_dates(sensor._attributes,settings.now))
        if plan is not None:
            sensor._pv_delay_timeline_trace = project_delay_trace(
                getattr(result, 'timeline_trace', None), plan, settings)
        reason = ('sale_disabled' if not allowed else
            'export_blocked' if settings.export_power_cap_kw == 0 else
            'forecast_unqualified' if not qualified_refill_dates(sensor._attributes,settings.now) else
            'no_safe_profitable_window')
        proposal = attributes(plan, enabled=enabled, now=settings.now, reason=reason)
    except (ValueError, TypeError, AttributeError, OverflowError):
        proposal = attributes(None, enabled=enabled, now=settings.now,reason='inputs_unavailable')
    proposal = stabilize_attributes(proposal, getattr(sensor, "_pv_delay_projection", None))
    sensor._pv_delay_accepted_plan = plan if proposal['pv_charge_delay_start'] else None
    sensor._pv_delay_projection = proposal
    sensor._attributes.update(proposal)


def project_delay_trace(trace, plan, settings):
    """Replay the approved window in the existing RCE display trajectory.

    Keep the optimizer result immutable. Deferred DC charge is paid back only
    from later PV export, within the same existing charge/BMS limits. No extra
    Recorder query, policy action or import is introduced by this projection.
    """
    if trace is None or plan is None:
        return trace
    eta = settings.charge_efficiency_percent / 100.
    capacity = settings.battery_capacity_kwh
    limit = min((settings.inverter_ac_power_kw or settings.inverter_power_kw)
        * settings.inverter_count * eta, _bms_charge_dc_power_limit_kw(settings))
    debt = 0.
    points = []
    windows=(plan,*plan.following)
    for point in trace.points:
        holding = any(p.start <= point.start < p.end for p in windows)
        delta = 0.
        if holding:
            if point.selected or point.battery_delta_kwh < -1e-8:
                raise ValueError('pv_delay_trace_action_conflict')
            delta = -max(point.battery_delta_kwh, 0.)
        elif debt > 1e-8:
            if point.selected:
                raise ValueError('pv_delay_trace_recovery_conflict')
            hours = (point.end-point.start).total_seconds()/3600
            budget=limit*hours
            if settings.forecast_charge_curve_kw and point.start>settings.now:
                from .charge_forecast import charge_budget
                before=point.soc_percent*capacity/100-point.battery_delta_kwh-debt
                available=max(point.battery_delta_kwh,0.)+max(point.grid_export_kwh,0.)*eta
                budget=charge_budget(before,capacity,capacity,hours,
                    min(available/hours,(settings.inverter_ac_power_kw or settings.inverter_power_kw)*settings.inverter_count*eta),
                    settings.forecast_charge_curve_kw)
            delta = min(debt, max(point.grid_export_kwh,0.)*eta,
                max(budget-max(point.battery_delta_kwh,0.),0.))
        debt = max(debt-delta,0.)
        if any(point.end==p.recovered_at for p in windows) and debt>1e-6:
            raise ValueError('pv_delay_trace_recovery_late')
        points.append(replace(point,
            battery_delta_kwh=point.battery_delta_kwh+delta,
            grid_export_kwh=max(point.grid_export_kwh-delta/eta,0.),
            soc_percent=point.soc_percent-debt/capacity*100.,
            selected=True if holding else point.selected,
            action_code='pv_charge_hold' if holding else point.action_code))
    if debt > 1e-6:
        raise ValueError('pv_delay_trace_recovery_missing')
    return replace(trace,points=tuple(points))
