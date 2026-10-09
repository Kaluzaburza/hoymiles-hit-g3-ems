"""One optional daytime PV-charge deferral on an already priced trajectory.

No I/O, extra import, battery export or change to another committed action.
The search consumes qualified conservative forecasts, never future readings.
At recovery the original energy AND PV-origin trajectory are restored.
"""
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import ceil, isfinite
from zoneinfo import ZoneInfo

EPS = 1e-8
HELPER = 'input_boolean.hoymiles_pv_charge_delay_enabled'
PROFILE_HELPER = 'input_select.hoymiles_pv_charge_delay_profile'
PROFILE_WEIGHTS = {'Conservative': .55, 'Balanced': .50, 'Maximum': .20}
WARSAW = ZoneInfo('Europe/Warsaw')
MINIMUM_GAIN = .05
PLANNER_SETTLING_SECONDS = 180.
LATEST_START_HOUR = 14  # Europe/Warsaw; a new window must start before this hour.


def profile_weight(value):
    """P10 share for PV deferral only; missing preferences grant no window."""
    return PROFILE_WEIGHTS.get(value)


def qualified_today_refill(metadata):
    """Today's measured forecast quality, independent of tomorrow's reserve.

    This only admits the search. The exact proposed window must still regain
    the full original stock on today's lower PV trajectory before its deadline.
    """
    # Published P10 totals are raw. Compare in the same scale; slot maps apply
    # the correction to both series separately. Older callers have raw P50.
    expected = metadata.get('forecast_today_raw_kwh', metadata.get('forecast_today_kwh'))
    low = metadata.get('forecast_today_p10_kwh')
    return (metadata.get('forecast_today_data_fresh') is True
        and type(expected) in (int, float) and type(low) in (int, float)
        and isfinite(expected) and isfinite(low) and 0 < low <= expected)


def qualified_refill_dates(metadata, now):
    """Each day must have its own fresh lower forecast; never borrow today's."""
    result=[]
    for offset,label in enumerate(('today','tomorrow')):
        values={key.replace(label,'today'):value for key,value in metadata.items()
                if key.startswith('forecast_'+label+'_')}
        if qualified_today_refill(values):
            result.append((now.astimezone(WARSAW).date()+timedelta(days=offset)).isoformat())
    return tuple(result)


@dataclass(frozen=True, slots=True)
class ChargeDelayPlan:
    start: datetime
    end: datetime
    recovered_at: datetime
    benefit_pln: float
    deferred_kwh: float
    extra_export_kwh: float
    evaluated_windows: int
    following: tuple = ()
    recovery_target_kwh: float | None = None


@dataclass(frozen=True, slots=True)
class PvDelayCommitment:
    """Same-entry leased Mode 5 binding; an ACK is not execution acceptance."""
    transaction_id: str
    started_at: datetime
    hard_deadline: datetime
    command_sent_at: datetime | None = None
    physical_confirmed: bool = True


def active_delay_plan(previous, commitment, now):
    """An old forecast alone never authorizes cross-replan continuation."""
    if previous is None or commitment is None:
        return None
    return next((p for p in (previous, *previous.following)
        if p.start <= commitment.started_at <= now < p.end
        and p.end == commitment.hard_deadline
        and p.recovery_target_kwh is not None), None)


def hold_soc_target(soc):
    """Integer register target at least one percentage point above fresh SOC."""
    if type(soc) not in (int, float) or not isfinite(soc) or not 0 <= soc <= 99:
        raise ValueError('pv_delay_soc_headroom_missing')
    return ceil(soc + 1)


def optimize_delay(data, points, *, blocked_starts=frozenset(), minimum_gain=MINIMUM_GAIN,
                   conservative=None, _window=None, _windows=None):
    """At most one window per local day, carrying the accepted overnight stock."""
    if not points or not data.allow_delay or not data.allow_sell:
        return points, None
    dates = tuple(dict.fromkeys(p.start.astimezone(WARSAW).date() for p in points))
    if len(dates) > 3 or len(points) > 100:
        return points, None
    fixed = _windows if _windows is not None else ((_window,) if _window else None)
    result = list(points)
    plans = []
    def slice_day(d, rows, lo, hi):
        replacements = dict(slots=d.slots[lo:hi], initial_kwh=rows[lo].start_kwh,
            pv_origin_kwh=min(d.pv_origin_kwh if lo == 0 else rows[lo-1].pv_origin_kwh, rows[lo].start_kwh),
            forecast_tail=())
        for key in ('sale_pv_kwh','sale_reserve_kwh','delay_pv_kwh'):
            value=getattr(d,key,None)
            if value is not None: replacements[key]=value[lo:hi]
        return replace(d,**replacements),tuple(rows[lo:hi])
    for day in dates:
        qualified=getattr(data,'delay_qualified_dates',None)
        if qualified is not None and day.isoformat() not in qualified:
            continue
        indices=[i for i,p in enumerate(points) if p.start.astimezone(WARSAW).date()==day]
        lo,hi=indices[0],indices[-1]+1
        d,rows=slice_day(data,points,lo,hi)
        risk=slice_day(*conservative,lo,hi) if conservative else None
        window=next((w for w in fixed if w[0].astimezone(WARSAW).date()==day),None) if fixed else None
        active = getattr(data, 'active_delay', None)
        active = (active if active is not None and active.start <= rows[0].start < active.end
            and active.start.astimezone(WARSAW).date() == day else None)
        if active is not None:
            window = (active.start, active.end)
        if fixed is not None and window is None:
            continue
        updated,plan=_optimize_day(d,rows,blocked_starts=blocked_starts,minimum_gain=minimum_gain,
            conservative=risk,_window=window,active=active)
        if plan is not None:
            result[lo:hi]=updated
            plans.append(plan)
    if fixed is not None and len(plans)!=len(fixed):
        return points,None
    return (tuple(result),replace(plans[0],following=tuple(plans[1:]))) if plans else (points,None)


def _optimize_day(data, points, *, blocked_starts=frozenset(), minimum_gain=MINIMUM_GAIN,
                   conservative=None, _window=None, active=None):
    """Return (adjusted EnergyPoints, plan), preserving fixed BUY/SELL slots.

    Bounded to today's <=48 half-hours and a single daytime window. The
    battery must recover baseline stock before another action and at least
    one hour before the end of forecast surplus, no later than 16:30 local.
    A day that cannot reach configured maximum stock cannot defer charging.
    """
    if (not getattr(data, 'allow_delay', False) or not data.allow_sell or not points
            or (getattr(data, 'zero_pv_sale_guard', False)
                and not getattr(data, 'delay_refill_qualified', False))
            or data.export_kw <= 0):
        return points, None
    eta = data.pv_charge_efficiency
    dc_kw = data.charge_dc_kw if data.charge_dc_kw is not None else data.charge_kw * eta
    pv_kw = data.charge_kw if data.pv_charge_kw is None else data.pv_charge_kw
    limit = min(dc_kw, pv_kw * eta)
    if limit <= 0 and not getattr(data,'forecast_charge_curve_kw',None):
        return points, None
    day = points[0].start.astimezone(WARSAW).date()
    today = tuple(p for p in points if p.start.astimezone(WARSAW).date() == day)
    if len(today) > 50 or len(today) < 2:
        return points, None
    # Full target may be lower than 100% when the user configured a ceiling.
    if max(p.end_kwh for p in today) < data.maximum_kwh - EPS:
        return points, None
    surplus = [p.end for p in today if p.pv_kwh > p.load_kwh + EPS]
    if not surplus:
        return points, None
    deadline = min(max(surplus) - timedelta(hours=1),
        datetime.combine(day, datetime.min.time(), WARSAW).replace(hour=16, minute=30).astimezone(timezone.utc))
    if active is not None:
        deadline = min(deadline, active.recovered_at)
        minimum_gain = EPS
    best, best_rows = None, points
    evaluated = 0
    for start_idx, first in enumerate(today):
        local = first.start.astimezone(WARSAW)
        continuing = (_window is not None and _window[0] < first.start < _window[1]
            and _window[0].astimezone(WARSAW).hour < LATEST_START_HOUR
            and first.start == today[0].start)
        if ((local.hour >= LATEST_START_HOUR and not continuing) or first.battery_in_kwh <= EPS
                or first.start_kwh/data.capacity_kwh*100 > 99):
            continue
        for stop in range(start_idx + 1, len(today) + 1):
            end = today[stop - 1].end
            if end > deadline:
                break
            if _window is not None and (first.start,end)!=(max(_window[0],today[0].start),_window[1]):
                continue
            evaluated += 1
            debt = gain = withheld = extra_export = 0.
            recovered = None
            rows = list(points)
            valid = True
            for i in range(start_idx, len(today)):
                p = today[i]
                holding = i < stop
                hours = (p.end.astimezone(timezone.utc)-p.start.astimezone(timezone.utc)).total_seconds()/3600
                # No purchased energy, existing discharge, blocked export or
                # another policy action can finance a deferred PV charge.
                if p.action != 'self_use' or p.start in blocked_starts:
                    valid = False
                    break
                if (p.grid_import_kwh > EPS or p.battery_out_kwh > EPS
                        or p.grid_charge_kwh > EPS or p.battery_export_kwh > EPS):
                    valid = False
                    break
                if not 0 < hours <= .5 or not isfinite(p.net):
                    valid = False
                    break
                if holding:
                    if p.net <= 0 or data.slots[i].sale_blocked:
                        valid = False
                        break
                    removed = p.battery_in_kwh
                    added_export = removed / eta
                    if p.grid_export_kwh + added_export > data.export_kw * hours + EPS:
                        valid = False
                        break
                    debt += removed
                    withheld += removed
                    extra_export += added_export
                    delta_charge, delta_export = -removed, added_export
                else:
                    if __package__:
                        from .pstryk_joint import pv_charge_budget
                    else:
                        from pstryk_joint import pv_charge_budget
                    budget=pv_charge_budget(data,data.slots[i],p.start_kwh-debt,
                        p.pv_charge_kwh+p.grid_export_kwh)
                    restored = min(debt, p.grid_export_kwh * eta,
                                   max(budget-p.battery_in_kwh, 0.))
                    debt -= restored
                    delta_charge, delta_export = restored, -restored / eta
                start_debt = debt + delta_charge
                end_energy = p.end_kwh - debt
                # Deferral exports only present PV while PV supplies the home.
                # Preserve the stock held at the start, not a battery-SELL
                # reserve that would unnecessarily veto charging from low SOC.
                if (end_energy < first.start_kwh - EPS
                        or p.start_kwh - start_debt < first.start_kwh - EPS
                        or end_energy > data.maximum_kwh + EPS):
                    valid = False
                    break
                gain += delta_export * p.net
                rows[i] = replace(p, action='pv_charge_hold' if holding else p.action,
                    start_kwh=p.start_kwh-start_debt, end_kwh=end_energy,
                    pv_origin_kwh=max(p.pv_origin_kwh-debt, 0.),
                    battery_in_kwh=p.battery_in_kwh+delta_charge,
                    pv_charge_kwh=p.pv_charge_kwh+delta_charge/eta,
                    grid_export_kwh=p.grid_export_kwh+delta_export)
                if (i >= stop and debt <= EPS and (active is None
                        or end_energy + EPS >= active.recovery_target_kwh)):
                    recovered = p.end
                    break
                if p.end >= deadline:
                    valid = False
                    break
            if (valid and recovered is not None and recovered <= deadline
                    and withheld > EPS and gain > minimum_gain
                    and (best is None or gain > best.benefit_pln + EPS)):
                if conservative is not None:
                    # The exact proposed window must also refill in time on
                    # the lower PV forecast with the same fixed BUY/SELL plan.
                    _,risk_plan=_optimize_day(*conservative,blocked_starts=blocked_starts,
                        minimum_gain=minimum_gain,_window=(active.start if active else first.start,end), active=active)
                    if risk_plan is None:
                        continue
                    recovered=max(recovered,risk_plan.recovered_at)
                best = ChargeDelayPlan(first.start, end, recovered, gain, withheld,
                    extra_export, evaluated, recovery_target_kwh=next(
                        p.end_kwh for p in rows if p.end == recovered))
                if active is not None:
                    # Preserve the original promise, including charge deferred
                    # before this solve. Never push recovery later or lower its SOC.
                    best = replace(best, start=active.start, recovered_at=active.recovered_at,
                        recovery_target_kwh=active.recovery_target_kwh)
                best_rows = tuple(rows)
    if best is not None:
        best = replace(best, evaluated_windows=evaluated)
    return best_rows, best


def no_window_reason(data, points):
    if not data.allow_sell:
        return 'sale_disabled'
    if data.export_kw <= 0:
        return 'export_blocked'
    dates=getattr(data,'delay_qualified_dates',None)
    if dates == ():
        return 'forecast_unqualified'
    if not any(p.start.astimezone(WARSAW).hour < LATEST_START_HOUR for p in points):
        return 'tomorrow_prices_pending'
    if data.charge_dc_kw == 0 and not data.forecast_charge_curve_kw:
        return 'charge_power_unavailable'
    return 'no_safe_profitable_window'


REASONS = {
    'inputs_unavailable': ('Brak aktualnych danych potrzebnych do sprawdzenia okna.', 'Current inputs required to validate the window are unavailable.'),
    'sale_disabled': ('Sprzedaż jest wyłączona.', 'Export is disabled.'),
    'export_blocked': ('Eksport do sieci jest zablokowany.', 'Grid export is blocked.'),
    'forecast_unqualified': ('Brak świeżej, ostrożnej prognozy PV dla dnia wykonania.', 'No fresh lower PV forecast for the execution day.'),
    'tomorrow_prices_pending': ('Czas na rozpoczęcie dzisiejszego opóźnienia minął (14:00). Plan na jutro czeka na ceny lub prognozę.', 'Today\u2019s start window ended at 14:00. Tomorrow\u2019s plan awaits prices or forecast.'),
    'charge_power_unavailable': ('Brak potwierdzonej mocy późniejszego ładowania.', 'Later charging capability is not confirmed.'),
    'no_safe_profitable_window': ('Brak opłacalnego okna, które pozwala później odzyskać zapas energii także przy ostrożnej prognozie PV.', 'No profitable window restores the battery stock in time on the lower PV forecast.'),
}


def attributes(plan, *, enabled, now, reason='no_safe_profitable_window'):
    """Small semantic status plus live-only scalar diagnostics (no history)."""
    windows=(plan,*plan.following) if plan else ()
    plan=next((p for p in windows if p.end>now),None)
    active = bool(enabled and plan and plan.start <= now < plan.end)
    return {
        'pv_charge_delay_enabled': bool(enabled),
        'pv_charge_delay_status': ('disabled' if not enabled else
            'planned' if plan else reason),
        'pv_charge_delay_reason': ('' if plan or not enabled else REASONS.get(reason,REASONS['no_safe_profitable_window'])[0]),
        'pv_charge_delay_reason_en': ('' if plan or not enabled else REASONS.get(reason,REASONS['no_safe_profitable_window'])[1]),
        'pv_charge_delay_start': plan.start.isoformat() if plan else None,
        'pv_charge_delay_end': plan.end.isoformat() if plan else None,
        'pv_charge_delay_recovered_at': plan.recovered_at.isoformat() if plan else None,
        'pv_charge_delay_benefit_pln': round(plan.benefit_pln, 4) if plan else 0.,
        'pv_charge_delay_deferred_kwh': round(plan.deferred_kwh, 4) if plan else 0.,
        'pv_charge_delay_current': active,
        'pv_charge_delay_execution_ready': active,
        'pv_charge_delay_windows': [dict(start=p.start.isoformat(),end=p.end.isoformat(),
            recovered_at=p.recovered_at.isoformat()) for p in windows],
    }


LIVE_ATTRIBUTES = frozenset({
    'pv_charge_delay_planner_power_basis', 'pv_charge_delay_planner_settling_active',
    'pv_charge_delay_planner_settling_until',
    'pv_charge_delay_windows',
    'pv_charge_delay_start', 'pv_charge_delay_end', 'pv_charge_delay_recovered_at',
    'pv_charge_delay_benefit_pln', 'pv_charge_delay_deferred_kwh',
    'pv_charge_delay_current', 'pv_charge_delay_execution_ready',
})


def stabilize_attributes(proposal, previous=None):
    """Suppress diagnostic jitter; never delay time, readiness or status changes.

    Recorder omits these values but still inserts rows on attribute changes.
    Keep this tiny projection in entity RAM only. Solver economics retain their
    full precision; display changes of <0.10 PLN / kWh can wait until material.
    """
    previous = previous or {}
    result = dict(proposal)
    metrics = ('pv_charge_delay_benefit_pln', 'pv_charge_delay_deferred_kwh')
    keys = ('pv_charge_delay_enabled', 'pv_charge_delay_status',
            'pv_charge_delay_start', 'pv_charge_delay_end',
            'pv_charge_delay_recovered_at', 'pv_charge_delay_current',
            'pv_charge_delay_execution_ready')
    if all(proposal.get(k) == previous.get(k) for k in keys):
        for key in metrics:
            value, old = proposal.get(key), previous.get(key)
            if (type(value) in (int, float) and type(old) in (int, float)
                    and abs(value-old) < .1):
                result[key] = old
    return result
