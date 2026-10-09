"""Bounded LOAD-only evidence for the existing, confirmed RCE export hold.

No authority, timers or storage here. The controller keeps its command-time
150 s ceiling and rechecks live FC03, BMS, SOC, consent and physical discharge.
"""
from dataclasses import asdict, replace
import hashlib
import json

if __package__:
    from .pstryk_joint import _optimize_standard, EPS
else:
    from pstryk_joint import _optimize_standard, EPS


def market_basis(data, profile_revision, price_revision):
    payload = asdict(data)
    first = payload['slots'][0]
    first['pv_kwh'] = round(first['pv_kwh'] / data.slots[0].hours, 8)
    if payload.get('sale_pv_kwh') is not None:
        payload['sale_pv_kwh']=(round(payload['sale_pv_kwh'][0]/data.slots[0].hours,8),*payload['sale_pv_kwh'][1:])
    if payload.get('delay_pv_kwh') is not None:
        payload['delay_pv_kwh']=(round(payload['delay_pv_kwh'][0]/data.slots[0].hours,8),*payload['delay_pv_kwh'][1:])
    # The RCE protected floor is derived from nominal LOAD and live SOC. Retain the fresh
    # (possibly higher) floor in the counterfactual below rather than treating
    # its derived change as a new market. Explicit reserves, forecasts and all
    # other settings remain part of the immutable fingerprint.
    first.pop('load_kwh')
    first.pop('start')
    # Raw LOAD is a physical execution cap, not a different price/forecast
    # market. The caller still revalidates it before publishing readiness.
    for key in ('initial_kwh', 'pv_origin_kwh', 'continuing_action', 'continuing_until',
                'sale_reserve_kwh', 'current_load_power_kw'):
        payload.pop(key)
    return hashlib.sha256(json.dumps((profile_revision, price_revision, payload),
        sort_keys=True, default=str, allow_nan=False).encode()).hexdigest()


def load_only_suppression(data, plan, previous_data, previous_plan):
    """Prove that only replacing current LOAD restores a profitable sale.

    Never call this a LOAD suppression for changed prices, future demand/PV,
    reserve, capabilities, provenance loss, consent, or a competing BUY/hold.
    One additional bounded solve is run only for a qualifying active sale.
    """
    current, old = data.slots[0], previous_data.slots[0]
    if (plan.slots[0].action != 'self_use' or previous_plan.slots[0].action != 'sell'
        or current.end != old.end or current.start < old.start
        or current.net <= 0 or current.sale_blocked or not data.allow_sell
        or data.discharge_kw <= 0 or data.export_kw <= 0
        or data.pv_origin_kwh <= EPS or data.initial_kwh <= data.reserve_kwh
        or plan.required_charge or plan.base_shortfall_kwh > EPS
        or market_basis(data, 0, '') != market_basis(previous_data, 0, '')
        or (current.load_kwh-current.pv_kwh)/current.hours < data.discharge_kw-EPS):
        return False
    old_load_kw = old.load_kwh/old.hours
    if current.load_kwh/current.hours <= old_load_kw+EPS:
        return False
    counterfactual = replace(data,
        slots=(replace(current, load_kwh=old_load_kw*current.hours), *data.slots[1:]),
        continuing_action='sell', continuing_until=current.end)
    alternative = _optimize_standard(counterfactual)
    return bool(alternative.slots[0].action == 'sell' and alternative.benefit_pln > EPS
                and not alternative.required_charge and alternative.base_shortfall_kwh <= EPS)
