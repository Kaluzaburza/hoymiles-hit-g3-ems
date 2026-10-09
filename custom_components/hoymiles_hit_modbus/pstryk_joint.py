"""Bounded, chronological Pstryk purchase/sale planning (no I/O).

One AC balance and one DC battery trajectory price both sides with priceNet.
The household floor is independent of prices; this is NOT the 1.5.9 aggressive
policy. Coordinate search accepts only a strictly better, executable complete
trajectory protecting home demand and the physical reserve. Sale block search
is shared with RCE; BUY cannot be enlarged to finance an export.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from itertools import combinations, product
import math
from types import SimpleNamespace
from zoneinfo import ZoneInfo

try:
    from .sale_block_search import select_sale_blocks
    from .rce_optimizer import PriceSlot, _pack_executable_exports, _minimum_executable_export
except ImportError:
    from sale_block_search import select_sale_blocks
    from rce_optimizer import PriceSlot, _pack_executable_exports, _minimum_executable_export

EPS = 1e-8
BUY_REFINEMENT_INDICES = 8
BUY_REFINEMENT_PASSES = 3

@dataclass(frozen=True, slots=True)
class EnergySlot:
    start: datetime
    end: datetime
    net: float | None
    load_kwh: float
    pv_kwh: float
    sale_blocked: bool = False

    @property
    def hours(self) -> float:
        return (self.end.astimezone(timezone.utc)-self.start.astimezone(timezone.utc)).total_seconds()/3600

@dataclass(frozen=True, slots=True)
class JointInput:
    slots: tuple[EnergySlot, ...]
    capacity_kwh: float
    initial_kwh: float
    reserve_kwh: float
    maximum_kwh: float
    pv_origin_kwh: float
    charge_kw: float
    discharge_kw: float
    ac_kw: float
    export_kw: float
    charge_efficiency: float = .95
    discharge_efficiency: float = .95
    wear_pln_kwh: float = .08
    minimum_benefit_pln: float = .01
    power_step_kw: float = .1
    minimum_power_kw: float = .2
    demand_margin_percent: float = 0.
    allow_buy: bool = True
    allow_sell: bool = True
    pv_charge_efficiency: float = .95
    sell_efficiency: float = .95
    terminal_kwh: float | None = None
    unpriced_home_shortfall_kwh: float = 0.
    pv_charge_kw: float | None = None
    charge_dc_kw: float | None = None
    home_discharge_kw: float | None = None
    allow_delay: bool = False
    continuing_action: str | None = None
    continuing_until: datetime | None = None
    zero_pv_sale_guard: bool = False
    # Deferral returns today's stock before its deadline; tomorrow's zero-PV
    # battery-sale stress must not veto a separately qualified same-day refill.
    delay_refill_qualified: bool = False
    sale_terminal_kwh: float | None = None
    sale_pv_kwh: tuple[float, ...] | None = None
    # Output-only continuation. Never included in priced optimization slots.
    forecast_tail: tuple[EnergySlot, ...] = ()
    # The same protected night/forecast/SOC floors as the native RCE model.
    sale_reserve_kwh: tuple[float, ...] | None = None
    minimum_export_kw: float = .2
    # Qualified same-day refill forecast, separate from RCE's zero-PV stress.
    delay_pv_kwh: tuple[float, ...] | None = None
    forecast_charge_curve_kw: tuple[float, ...] | None = None
    forecast_charge_after: datetime | None = None
    delay_qualified_dates: tuple[str, ...] | None = None
    active_delay: object | None = None
    # Adapter-qualified live power limits commands, never the LOAD forecast.
    current_load_power_kw: float | None = None
    current_pv_power_kw: float | None = None

@dataclass(frozen=True, slots=True)
class EnergyPoint:
    start: datetime
    end: datetime
    net: float | None
    action: str
    start_kwh: float
    end_kwh: float
    pv_origin_kwh: float
    load_kwh: float
    pv_kwh: float
    battery_in_kwh: float
    battery_out_kwh: float
    grid_charge_kwh: float
    grid_import_kwh: float
    grid_export_kwh: float
    battery_export_kwh: float
    curtailed_kwh: float
    command_kw: float
    protected_kwh: float
    pv_charge_kwh: float
    home_battery_kwh: float

@dataclass(frozen=True, slots=True)
class JointPlan:
    slots: tuple[EnergyPoint, ...]
    cost_pln: float
    baseline_cost_pln: float
    baseline_end_kwh: float
    benefit_pln: float
    margin_requested_kwh: float
    margin_unserved_kwh: float
    simulations: int
    required_charge: bool = False
    base_shortfall_kwh: float = 0.
    delay_plan: object | None = None
    action_levels: tuple[int, ...] = ()
    sale_reason: str = 'home_energy_needed'
    active_slot_commitment_applied: bool = False
    active_run_deadline: datetime | None = None

def _validate(data: JointInput) -> None:
    if not 1 <= len(data.slots) <= 100:
        raise ValueError('slot_count_invalid')
    if data.continuing_action is not None:
        if (data.continuing_action not in ('buy','sell') or data.continuing_until is None
                or data.continuing_until.tzinfo is None
                or not data.slots[0].start < data.continuing_until <= data.slots[0].end):
            raise ValueError('continuation_invalid')
    for name in ('capacity_kwh','initial_kwh','reserve_kwh','maximum_kwh','pv_origin_kwh',
                 'charge_kw','discharge_kw','ac_kw','export_kw','wear_pln_kwh',
                 'minimum_benefit_pln','power_step_kw','minimum_power_kw','demand_margin_percent'):
        value = getattr(data, name)
        if type(value) not in (float, int) or not math.isfinite(value) or value < 0:
            raise ValueError('numeric_input_invalid:'+name)
    if not (0 < data.capacity_kwh and data.reserve_kwh <= data.maximum_kwh <= data.capacity_kwh
            and data.initial_kwh <= data.capacity_kwh and data.pv_origin_kwh <= data.initial_kwh
            and data.power_step_kw > 0 and data.demand_margin_percent <= 100):
        raise ValueError('energy_bounds_invalid')
    for efficiency in (data.charge_efficiency, data.discharge_efficiency,
                       data.pv_charge_efficiency, data.sell_efficiency):
        if type(efficiency) not in (float, int) or not math.isfinite(efficiency) or not 0 < efficiency <= 1:
            raise ValueError('efficiency_invalid')
    if data.terminal_kwh is not None and (not math.isfinite(data.terminal_kwh) or not data.reserve_kwh<=data.terminal_kwh<=data.maximum_kwh):
        raise ValueError('terminal_energy_invalid')
    if type(data.zero_pv_sale_guard) is not bool:
        raise ValueError('sale_guard_invalid')
    if data.forecast_charge_curve_kw is not None:
        if __package__:
            from .charge_forecast import valid_curve
        else:
            from charge_forecast import valid_curve
        if (not valid_curve(data.forecast_charge_curve_kw) or data.forecast_charge_after is None
                or data.forecast_charge_after.tzinfo is None):
            raise ValueError('charge_forecast_invalid')
    if data.sale_pv_kwh is not None and (len(data.sale_pv_kwh)!=len(data.slots)
            or any(type(v) not in (int,float) or not math.isfinite(v) or v<0
                   or v>s.pv_kwh+EPS for v,s in zip(data.sale_pv_kwh,data.slots))):
        raise ValueError('sale_forecast_invalid')
    if data.delay_pv_kwh is not None and (len(data.delay_pv_kwh)!=len(data.slots)
            or any(type(v) not in (int,float) or not math.isfinite(v) or v<0
                   or v>s.pv_kwh+EPS for v,s in zip(data.delay_pv_kwh,data.slots))):
        raise ValueError('delay_forecast_invalid')
    if data.sale_terminal_kwh is not None and (not math.isfinite(data.sale_terminal_kwh) or not data.reserve_kwh<=data.sale_terminal_kwh<=data.maximum_kwh):
        raise ValueError('sale_terminal_energy_invalid')
    if (not math.isfinite(data.minimum_export_kw) or data.minimum_export_kw < 0
        or data.sale_reserve_kwh is not None and (len(data.sale_reserve_kwh)!=len(data.slots)
            or any(not math.isfinite(v) or v<data.reserve_kwh or v>data.capacity_kwh
                   for v in data.sale_reserve_kwh))):
        raise ValueError('sale_reserve_invalid')
    for value in (data.pv_charge_kw,data.charge_dc_kw,data.home_discharge_kw,
                  data.current_load_power_kw,data.current_pv_power_kw):
        if value is not None and (type(value) not in (int,float) or not math.isfinite(value) or value<0):
            raise ValueError('physical_power_invalid')
    previous = None
    for s in data.slots:
        if (s.start.tzinfo is None or s.end.tzinfo is None or not 0 < s.hours <= .5
                or previous is not None and previous != s.start.astimezone(timezone.utc)):
            raise ValueError('timeline_invalid')
        previous = s.end.astimezone(timezone.utc)
        if not all(type(v) in (float,int) and math.isfinite(v) for v in (s.net,s.load_kwh,s.pv_kwh)) or min(s.load_kwh,s.pv_kwh)<0:
            raise ValueError('slot_value_invalid')

def _floor(data: JointInput, *, bounded=True, for_sale=False) -> tuple[float, ...]:
    """DC stock protecting future home consumption; never an SOC-point margin."""
    if for_sale and data.sale_reserve_kwh is not None:
        return (data.sale_reserve_kwh[0], *data.sale_reserve_kwh)
    needed = data.terminal_kwh if data.terminal_kwh is not None else data.reserve_kwh
    guarded = for_sale and data.zero_pv_sale_guard
    if for_sale and data.sale_terminal_kwh is not None:
        needed = max(needed,data.sale_terminal_kwh)
    home_kw = data.discharge_kw if data.home_discharge_kw is None else data.home_discharge_kw
    pv_kw = data.charge_kw if data.pv_charge_kw is None else data.pv_charge_kw
    charge_dc = data.charge_dc_kw if data.charge_dc_kw is not None else data.charge_kw*min(data.charge_efficiency,data.pv_charge_efficiency)
    floors = [needed]
    for i in reversed(range(len(data.slots))):
        s=data.slots[i]
        pv = 0. if guarded else data.sale_pv_kwh[i] if for_sale and data.sale_pv_kwh is not None else s.pv_kwh
        deficit = min(max(s.load_kwh-pv,0)*(1+data.demand_margin_percent/100),home_kw*s.hours)/data.discharge_efficiency
        surplus = min(max(pv-s.load_kwh,0)*data.pv_charge_efficiency,
                      pv_kw*s.hours*data.pv_charge_efficiency,charge_dc*s.hours)
        needed = max(data.reserve_kwh, needed+deficit-surplus)
        if bounded: needed = min(data.maximum_kwh,needed)
        # Register 4305 has whole-percent SOC resolution. Round protection UP.
        protected = min(data.maximum_kwh, math.ceil(needed/data.capacity_kwh*100-1e-9)*data.capacity_kwh/100) if bounded else needed
        floors.append(protected)
    return tuple(reversed(floors))

def pv_charge_budget(data, slot, energy, surplus):
    """Future SOC curve and current physical cap are separate constraints."""
    eta = data.pv_charge_efficiency
    if (data.forecast_charge_curve_kw is not None and data.forecast_charge_after is not None
            and slot.start >= data.forecast_charge_after):
        if __package__:
            from .charge_forecast import charge_budget
        else:
            from charge_forecast import charge_budget
        return charge_budget(energy, data.capacity_kwh, data.maximum_kwh, slot.hours,
            min(surplus/slot.hours, data.ac_kw)*eta, data.forecast_charge_curve_kw)
    dc = data.charge_dc_kw if data.charge_dc_kw is not None else data.charge_kw*eta
    pv_kw = data.charge_kw if data.pv_charge_kw is None else data.pv_charge_kw
    return min(surplus*eta, max(data.maximum_kwh-energy,0.), pv_kw*slot.hours*eta, dc*slot.hours)


def simulate(data: JointInput, actions: tuple[int, ...], floors: tuple[float, ...] | None = None,
             sale_floors: tuple[float, ...] | None = None, *,
             fixed_points: tuple[EnergyPoint, ...] | None = None,
             exports=None, continuous_exports=False) -> tuple[tuple[EnergyPoint,...],float | None]:
    """0=self use, +1/+2=BUY, -1/-2=SELL, 3=restore reserve, 4=PV hold."""
    if len(actions) != len(data.slots) or any(a not in (-2,-1,0,1,2,3,4) for a in actions):
        raise ValueError('action_invalid')
    if fixed_points is not None and len(fixed_points) != len(actions):
        raise ValueError('fixed_selection_invalid')
    floors = floors or _floor(data)
    sale_floors = sale_floors or _floor(data,for_sale=True)
    energy, origin, cost = data.initial_kwh, data.pv_origin_kwh, 0.
    pv_kw = data.charge_kw if data.pv_charge_kw is None else data.pv_charge_kw
    charge_dc = data.charge_dc_kw if data.charge_dc_kw is not None else data.charge_kw*min(data.charge_efficiency,data.pv_charge_efficiency)
    home_kw = data.discharge_kw if data.home_discharge_kw is None else data.home_discharge_kw
    points = []
    def power(value: float, *, house: bool = False) -> float:
        rounded = math.floor((max(value,0)+EPS)/data.power_step_kw)*data.power_step_kw
        return rounded if house or rounded+EPS >= data.minimum_power_kw else 0.
    for i,(s,a) in enumerate(zip(data.slots,actions)):
        if s.net is None and a != 0:
            raise ValueError('unpriced_action_forbidden')
        start_energy = energy
        pv = min(s.pv_kwh, data.ac_kw*s.hours)
        curtailed = s.pv_kwh-pv
        direct = min(pv,s.load_kwh)
        deficit, surplus = s.load_kwh-direct, pv-direct
        grid_charge = bat_export = command = 0.
        bat_in = bat_out = pv_charge = home_dc = 0.
        hold = a == 4 and data.allow_delay and data.allow_sell and not s.sale_blocked and s.net > 0 and data.export_kw > 0
        buy = a in (1,2,3) and data.allow_buy and data.charge_kw > 0 and (a!=3 or energy<data.reserve_kwh-EPS)
        sell = a < 0 and data.allow_sell and not s.sale_blocked and data.export_kw > 0 and s.net >= 0
        fixed = fixed_points[i] if fixed_points is not None else None
        if fixed is not None:
            buy = buy and fixed.action == 'buy'
            sell = sell and (exports is not None or fixed.action == 'sell')
        if not sell and not hold:
            pv_store = pv_charge_budget(data,s,energy,surplus)
            energy += pv_store; origin += pv_store; bat_in += pv_store
            pv_charge = pv_store/data.pv_charge_efficiency
            # Multiplication/division by efficiency can overshoot the exact
            # available AC energy by a floating-point ulp. These remainders
            # are physically non-negative, including on a zero-export site.
            surplus = max(surplus-pv_charge,0.)
        if buy:
            # Like tariff charging, BUY must cover a household/reserve deficit.
            # Without this gate, grid support preserves already sufficient PV
            # stock for a later SELL: speculative arbitrage disguised as home
            # supply, even when no battery charging is requested at all.
            home_need = min(deficit*(1+data.demand_margin_percent/100),
                            home_kw*s.hours)/data.discharge_efficiency
            if a != 3 and energy+EPS >= floors[i+1]+home_need:
                buy = False
        if buy:
            # Only buy energy for home/reserve, never speculative inventory.
            # Mandatory restoration must reach a whole-percent 4303 target.
            # Flooring both power and resulting SOC otherwise strands a small
            # top-up one point below the reserve, which the executor rejects.
            reserve_target = math.ceil(data.reserve_kwh/data.capacity_kwh*100-EPS)*data.capacity_kwh/100
            wanted = max((reserve_target if a==3 else floors[i+1])-energy,0)/data.charge_efficiency
            room = max(data.maximum_kwh-energy,0)/data.charge_efficiency
            requested = (wanted+deficit)/s.hours
            if a==3 and wanted>EPS:
                requested = math.ceil((max(wanted/s.hours,data.minimum_power_kw)
                    +deficit/s.hours)/data.power_step_kw-EPS)*data.power_step_kw
            cap = min(data.ac_kw, data.charge_kw*min(abs(a),2)/2,
                      fixed.command_kw if fixed is not None else math.inf)
            command = power(min(cap,
                                requested,
                                fixed.command_kw if fixed is not None else math.inf), house=True)
            if a!=3:
                command = power(min(command,(room+deficit)/s.hours), house=True)
            # Mode 4 with a holding SOC target may support a house drawing
            # less than one register power step. Keep a nonzero command cap;
            # this never authorizes battery charging below its own minimum.
            if deficit > EPS:
                command = max(command, power(min(cap,
                    math.ceil(deficit/s.hours/data.power_step_kw-EPS)*data.power_step_kw), house=True))
            grid_charge = min(max(command*s.hours-deficit,0), room,
                              wanted if a==3 else math.inf,
                              max(charge_dc*s.hours-bat_in,0)/data.charge_efficiency,
                              fixed.grid_charge_kwh if fixed is not None else math.inf)
            # An economic BUY must fit its executable whole-percent target.
            # Mandatory restoration already has an integer reserve_target;
            # intermediate slot energy may be fractional while charging
            # towards it. Rounding that energy down can turn a real partial
            # top-up into support-only and revoke the still-required BUY.
            if a != 3:
                target = math.floor((energy+grid_charge*data.charge_efficiency)/data.capacity_kwh*100+EPS)*data.capacity_kwh/100
                grid_charge = min(grid_charge,max(target-energy,0)/data.charge_efficiency)
            if command-deficit/s.hours+EPS < data.minimum_power_kw:
                grid_charge = 0.
            energy += grid_charge*data.charge_efficiency
            bat_in += grid_charge*data.charge_efficiency
        if not hold and (not buy or command == 0):
            home_dc = min(deficit/data.discharge_efficiency, max(energy-data.reserve_kwh,0),
                          home_kw*s.hours/data.discharge_efficiency,
                          max(data.ac_kw*s.hours-direct,0)/data.discharge_efficiency)
            energy -= home_dc; origin = max(origin-home_dc,0); bat_out += home_dc
            deficit = max(deficit-home_dc*data.discharge_efficiency,0.)
        if sell:
            # The command includes home supply; export is the remaining AC.
            home_ac = bat_out*data.discharge_efficiency
            protected = max(sale_floors[i+1],energy-origin)
            protected = math.ceil(protected/data.capacity_kwh*100-1e-9)*data.capacity_kwh/100
            limit = min(data.discharge_kw*s.hours, max(data.ac_kw*s.hours-pv,0),
                        home_ac+max(data.export_kw*s.hours-surplus,0),
                        home_ac+min(origin,max(energy-protected,0))*data.sell_efficiency)
            command = power(limit/s.hours*abs(a)/2)
            if exports is not None:
                desired = min(limit/s.hours,(home_ac+exports.get(s.start,0.))/s.hours)
                command = desired if continuous_exports else power(desired)
            if fixed is not None and exports is None:
                command = power(min(command,fixed.command_kw,
                                    (home_ac+fixed.battery_export_kwh)/s.hours))
            bat_export = max(command*s.hours-home_ac,0)
            export_dc = bat_export/data.sell_efficiency
            energy -= export_dc; origin = max(origin-export_dc,0); bat_out += export_dc
            if bat_export <= EPS:
                command = 0.
                # A rejected/quantized-to-zero SELL sends no Mode 5 command.
                # It therefore cannot silently suppress ordinary PV charging
                # and claim the benefit of a hold action that never executes.
                pv_store = max(pv_charge_budget(data,s,energy,surplus)-bat_in,0.)
                energy += pv_store; origin += pv_store; bat_in += pv_store
                pv_charge = pv_store/data.pv_charge_efficiency
                surplus = max(surplus-pv_charge,0.)
        natural_export = min(surplus, max(data.export_kw*s.hours-bat_export,0))
        curtailed += surplus-natural_export
        grid_import = deficit+grid_charge
        grid_export = natural_export+bat_export
        # An unpriced SelfUse observation has no economic valuation. In
        # particular, missing tomorrow prices must never become zero prices.
        cost = None if s.net is None or cost is None else cost + (grid_import-grid_export)*s.net + bat_out*data.wear_pln_kwh
        points.append(EnergyPoint(s.start,s.end,s.net,
            'pv_charge_hold' if hold else 'buy' if buy and command else 'sell' if bat_export>EPS else 'self_use',
            start_energy,energy,min(origin,energy),s.load_kwh,s.pv_kwh,bat_in,bat_out,
            grid_charge,grid_import,grid_export,bat_export,curtailed,command,
            sale_floors[i+1] if sell else floors[i+1],
            pv_charge,home_dc*data.discharge_efficiency))
    return tuple(points), cost

def _optimize_standard(data: JointInput) -> JointPlan:
    _validate(data)
    floors = _floor(data)
    sale_floors = _floor(data,for_sale=True)
    actions = (0,)*len(data.slots)
    baseline, base_cost = simulate(data,actions,floors,sale_floors)
    base_need = max(_floor(replace(data,demand_margin_percent=0),bounded=False))
    total_need = max(_floor(data,bounded=False))
    margin = max(total_need-base_need,0)*data.discharge_efficiency
    margin_unserved = min(margin,max(total_need-max(base_need,data.maximum_kwh),0)*data.discharge_efficiency)
    base_shortfall = max(base_need-data.maximum_kwh,0)+data.unpriced_home_shortfall_kwh
    if data.initial_kwh<data.reserve_kwh-EPS:
        mandatory,cost = simulate(data,(3,)*len(data.slots),floors,sale_floors)
        return JointPlan(mandatory,cost,base_cost,baseline[-1].end_kwh,0.,margin,margin_unserved,
                         2,True,max(base_shortfall,data.reserve_kwh-mandatory[-1].end_kwh),action_levels=(3,)*len(data.slots))
    points, cost = baseline, base_cost
    terminal = data.terminal_kwh if data.terminal_kwh is not None else data.reserve_kwh
    required = baseline[-1].end_kwh+EPS < terminal
    evaluations = 1
    # Establish home purchases independently of sale. The sale search below
    # may use only this fixed BUY budget, never enlarge it through arbitrage.
    for _ in range(3):
        changed = False
        order = sorted(range(len(actions)), key=lambda i: (data.slots[i].net,i))
        for direction, indices in ((1,order),):
            if direction==1 and not data.allow_buy or direction==-1 and not data.allow_sell:
                continue
            for i in indices:
                for level in (2,1):
                    candidate_actions = actions[:i]+(direction*level,)+actions[i+1:]
                    candidate,candidate_cost = simulate(data,candidate_actions,floors,sale_floors); evaluations += 1
                    before_short = max(terminal-points[-1].end_kwh,0)
                    after_short = max(terminal-candidate[-1].end_kwh,0)
                    continuing = i==0 and data.continuing_action==('buy' if direction==1 else 'sell')
                    # A start's minimum profit is not re-earned every minute.
                    # Only a physically confirmed current action can use this
                    # continuation threshold; losses and all limits still fail.
                    saving_gate = EPS if continuing else max(data.minimum_benefit_pln,EPS)
                    better = (after_short < before_short-EPS or
                              after_short <= before_short+EPS and candidate_cost < cost-saving_gate)
                    if (better
                            and candidate[-1].end_kwh+EPS >= baseline[-1].end_kwh):
                        points,cost,actions = candidate,candidate_cost,candidate_actions
                        changed = True
        if not changed: break
    plan = JointPlan(points,cost,base_cost,baseline[-1].end_kwh,max(base_cost-cost,0),margin,
                     margin_unserved,evaluations,required,max(base_shortfall,terminal-points[-1].end_kwh),action_levels=actions)
    plan = _refine_home_purchases(data, plan, floors, sale_floors)
    return _optimize_sale(data,plan,floors,sale_floors)


def _refine_home_purchases(data, plan, floors, sale_floors):
    """Undo/swap BUY decisions before SELL, within 672 complete simulations.

    This bounded local search is not globally optimal. Confirmed active runs,
    mandatory restoration and PV-delay alternatives retain the established
    solver; this refinement never changes their commitment or refill policy.
    """
    if (not data.allow_buy or data.initial_kwh < data.reserve_kwh-EPS
            or data.continuing_action or data.active_delay or data.allow_delay):
        return plan
    actions, points, cost = plan.action_levels, plan.slots, plan.cost_pln
    terminal = data.terminal_kwh if data.terminal_kwh is not None else data.reserve_kwh
    # Existing purchases must be eligible for removal. Fill the bounded set
    # with cheapest alternatives in deterministic price/time order.
    purchased = sorted((i for i, a in enumerate(actions) if a > 0),
                       key=lambda i: (-data.slots[i].net, i))
    cheap = sorted(range(len(actions)), key=lambda i: (data.slots[i].net, i))
    indices = list(dict.fromkeys(purchased + cheap))[:BUY_REFINEMENT_INDICES]
    evaluations = 0
    for _ in range(BUY_REFINEMENT_PASSES):
        best = None
        for i, j in combinations(indices, 2):
            for ai, aj in product((0, 1, 2), repeat=2):
                trial = list(actions)
                trial[i], trial[j] = ai, aj
                trial = tuple(trial)
                if trial == actions:
                    continue
                candidate, candidate_cost = simulate(data, trial, floors, sale_floors)
                evaluations += 1
                if (candidate[-1].end_kwh + EPS < plan.baseline_end_kwh
                        or max(terminal-candidate[-1].end_kwh, 0.) > max(terminal-points[-1].end_kwh, 0.)+EPS):
                    continue
                if (candidate_cost < cost-max(data.minimum_benefit_pln, EPS)
                        and (best is None or candidate_cost < best[0]-EPS)):
                    best = candidate_cost, trial, candidate
        if best is None:
            break
        cost, actions, points = best
    return replace(plan, slots=points, cost_pln=cost, action_levels=actions,
                   benefit_pln=max(plan.baseline_cost_pln-cost, 0.),
                   base_shortfall_kwh=max(plan.base_shortfall_kwh, terminal-points[-1].end_kwh),
                   simulations=plan.simulations+evaluations)


def _optimize_sale(data, home_plan, floors, sale_floors):
    """Use the RCE active-set search and tail packer on one BUY/SELL balance."""
    if not data.allow_sell:
        return replace(home_plan,sale_reason='sale_disabled')
    if data.export_kw<=0:
        return replace(home_plan,sale_reason='export_blocked')
    if data.discharge_kw<=0:
        return replace(home_plan,sale_reason='discharge_power_unavailable')
    candidates=[(PriceSlot(s.start,s.net),s.start) for s,p in zip(data.slots,home_plan.slots)
                if not s.sale_blocked and s.net>=0 and p.action!='buy']
    if not candidates:
        reason='sale_hours_blocked' if all(s.sale_blocked for s in data.slots) else (
            'sale_price_too_low' if all(s.net<0 or s.sale_blocked for s in data.slots)
            else 'home_energy_needed')
        return replace(home_plan,sale_reason=reason)
    index={s.start:i for i,s in enumerate(data.slots)}
    cache={}
    evaluations=0
    physical_surplus=False
    profitable_surplus=False
    risk_blocked=False
    # Whole-horizon expected and lower-PV balances share the same commands.
    risk=replace(data,slots=tuple(replace(s,pv_kwh=pv) for s,pv in zip(data.slots,data.sale_pv_kwh)),
                 sale_pv_kwh=None) if data.sale_pv_kwh is not None else data
    risk_home,_=simulate(risk,home_plan.action_levels,floors,sale_floors,fixed_points=home_plan.slots)

    def trajectory(exports, *, continuous=True, model=data):
        actions=tuple(-2 if s.start in exports else a for s,a in zip(model.slots,home_plan.action_levels))
        return simulate(model,actions,floors,sale_floors,fixed_points=home_plan.slots,
                        exports=exports,continuous_exports=continuous)

    def evaluate(exports):
        nonlocal evaluations,physical_surplus,profitable_surplus,risk_blocked
        key=tuple(sorted(exports.items()))
        if key in cache:
            return cache[key]
        points,cost=trajectory(exports)
        evaluations+=1
        valid=all(points[index[t]].battery_export_kwh+EPS>=v for t,v in exports.items())
        # Selling cannot create additional home import or enlarge any BUY.
        valid=valid and all(p.grid_import_kwh<=b.grid_import_kwh+EPS
                           for p,b in zip(points,home_plan.slots))
        if valid and risk is not data:
            risk_points,_=trajectory(exports,model=risk)
            valid=all(risk_points[index[t]].battery_export_kwh+EPS>=v for t,v in exports.items())
            valid=valid and all(p.grid_import_kwh<=b.grid_import_kwh+EPS
                               for p,b in zip(risk_points,risk_home))
            risk_blocked = risk_blocked or bool(exports) and not valid
        if valid and sum(exports.values())>EPS:
            physical_surplus=True
            profitable_surplus=profitable_surplus or cost<home_plan.cost_pln-EPS
        value=(valid,-cost if valid else -math.inf)
        cache[key]=value
        return value

    caps={s.start:min(data.discharge_kw*s.hours,data.export_kw*s.hours,
                     max(data.ac_kw*s.hours-s.pv_kwh,0.)) for s in data.slots}
    if not any(caps[t]>EPS for _,t in candidates):
        return replace(home_plan,sale_reason='pv_uses_inverter_power')
    if not any(p.price_pln_kwh*data.sell_efficiency>data.wear_pln_kwh+EPS for p,_ in candidates):
        return replace(home_plan,sale_reason='sale_price_too_low')
    settings=SimpleNamespace(minimum_net_export_power_kw=data.minimum_export_kw,
        inverter_power_kw=data.power_step_kw*100,inverter_count=1,inverter_ac_power_kw=data.ac_kw)
    minimum={t:_minimum_executable_export(settings, data.slots[index[t]].hours,
        data.slots[index[t]].load_kwh, data.slots[index[t]].pv_kwh) for _,t in candidates}
    if all(minimum[t]>caps[t]+EPS for _,t in candidates):
        return replace(home_plan,sale_reason='sale_below_minimum_power')
    exports=select_sale_blocks(candidates=candidates,now=data.slots[0].start.astimezone(ZoneInfo('Europe/Warsaw')),
        battery_wear_cost_pln_kwh=data.wear_pln_kwh,export_efficiency=data.sell_efficiency,
        baseline_objective=-home_plan.cost_pln,feasible=lambda p:evaluate(p)[0],
        objective=lambda p:evaluate(p)[1],slot_physical_cap=lambda t:caps[t],
        exact_objective=lambda p:evaluate(p)[1],current_slot_start=data.slots[0].start,
        slot_minimum_export=lambda t: minimum[t],
        sale_suppresses_refill=any(s.pv_kwh>s.load_kwh for s in data.slots))
    exports=_pack_executable_exports(exports,settings=settings,starts=list(index),
        load_by_slot={s.start:s.load_kwh for s in data.slots},
        pv_by_slot={s.start:s.pv_kwh for s in data.slots},
        slot_fractions={s.start:s.hours*2 for s in data.slots},
        price_by_start={s.start:s.net for s in data.slots},
        feasible=lambda p:evaluate(p)[0],objective=lambda p:evaluate(p)[1])
    points,cost=trajectory(exports,continuous=False)
    saving_gate=EPS if data.continuing_action=='sell' else max(data.minimum_benefit_pln,EPS)
    if cost>=home_plan.cost_pln-saving_gate:
        reason=('sale_below_minimum_power' if profitable_surplus and not exports else
                'sale_below_minimum_profit' if physical_surplus else
                'conservative_forecast' if risk_blocked else 'home_energy_needed')
        return replace(home_plan,simulations=home_plan.simulations+evaluations,sale_reason=reason)
    actions=tuple(-2 if p.action=='sell' else a for p,a in zip(points,home_plan.action_levels))
    return replace(home_plan,slots=points,cost_pln=cost,benefit_pln=max(home_plan.baseline_cost_pln-cost,0),
                   simulations=home_plan.simulations+evaluations,action_levels=actions,sale_reason='sale_planned')


def optimize(data: JointInput) -> JointPlan:
    plan = _optimize_standard(data)
    if not data.allow_delay:
        return plan
    if __package__:
        from .pv_charge_delay import optimize_delay, WARSAW
    else:
        from pv_charge_delay import optimize_delay, WARSAW
    # Compare to both the accepted BUY/SELL schedule and a variant preserving
    # morning PV for direct export. Otherwise a preselected morning battery
    # sale would prevent considering the better no-cycle charge-delay action.
    morning = replace(data, slots=tuple(replace(s, sale_blocked=True)
        if s.start.astimezone(WARSAW).hour < 12
        or data.active_delay is not None and s.start < data.active_delay.end
        else s for s in data.slots))
    alternatives = (plan, _optimize_standard(morning))
    passive_variant = None
    if plan.required_charge and data.continuing_action is None:
        # A reserve-only BUY is still the normal fallback. A PV-only variant
        # may replace its uncommitted proposal when it serves every home
        # interval without extra import and restores at least the same stock.
        # The delay search additionally proves unchanged stock while holding
        # and full same-day recovery on the lower forecast. Active BUY stays.
        passive_actions = (0,) * len(data.slots)
        passive, passive_cost = simulate(data, passive_actions)
        if (passive[-1].end_kwh + EPS >= plan.slots[-1].end_kwh
                and all(p.grid_import_kwh <= b.grid_import_kwh + EPS
                        and (b.action != 'buy' or p.grid_import_kwh + p.battery_out_kwh <= EPS)
                        for p, b in zip(passive, plan.slots))):
            passive_variant = replace(plan, slots=passive, cost_pln=passive_cost,
                                      action_levels=passive_actions)
            alternatives += (passive_variant,)
    best = None
    for alternative in alternatives:
        conservative = None
        refill_pv=data.delay_pv_kwh if data.delay_pv_kwh is not None else data.sale_pv_kwh
        if refill_pv is not None:
            risk_data=replace(data,slots=tuple(replace(s,pv_kwh=pv) for s,pv in zip(data.slots,refill_pv)),sale_pv_kwh=None,delay_pv_kwh=None)
            risk_points,_=simulate(risk_data,alternative.action_levels,fixed_points=alternative.slots)
            if alternative is passive_variant:
                original_risk, _ = simulate(risk_data, plan.action_levels, fixed_points=plan.slots)
                if (risk_points[-1].end_kwh + EPS < original_risk[-1].end_kwh
                        or any(p.grid_import_kwh > b.grid_import_kwh + EPS
                            or old.action == 'buy' and p.grid_import_kwh + p.battery_out_kwh > EPS
                            for p, b, old in zip(risk_points, original_risk, plan.slots))):
                    continue
            conservative=(risk_data,risk_points)
        points, delay = optimize_delay(data, alternative.slots, conservative=conservative)
        if delay is None or points[-1].end_kwh + EPS < plan.slots[-1].end_kwh:
            continue
        from_delay = delay.benefit_pln + sum(p.benefit_pln for p in delay.following)
        cost = alternative.cost_pln-from_delay
        improvement = plan.cost_pln-cost
        if improvement <= (EPS if data.active_delay is not None else .05) or best is not None and cost >= best[0]-EPS:
            continue
        # Attribute any displaced morning battery sale proportionally, so no
        # individual day's displayed benefit becomes negative.
        scale = improvement/from_delay
        delay = replace(delay, benefit_pln=delay.benefit_pln*scale,
            following=tuple(replace(p,benefit_pln=p.benefit_pln*scale) for p in delay.following))
        best = (cost, points, delay, alternative.action_levels)
    if best is None:
        return plan
    cost, points, delay, actions = best
    return replace(plan, slots=points, delay_plan=delay, cost_pln=cost,
        benefit_pln=max(plan.baseline_cost_pln-cost, 0.), action_levels=actions)


def revalidate_plan(data: JointInput, plan: JointPlan, *, captured: JointInput) -> JointPlan | None:
    """Reprice a fixed selection on fresh physics, like RCE's commit proof.

    The caller certifies unchanged settings/market and fresh physical inputs.
    No search, new trade, higher command or extension of the delay window.
    """
    try:
        _validate(data)
        if (len(data.slots) != len(captured.slots) or len(plan.slots) != len(data.slots)
            or not 0 <= (data.slots[0].start-captured.slots[0].start).total_seconds() <= 120
            or any((a.end,a.net,a.sale_blocked)!=(b.end,b.net,b.sale_blocked)
                   for a,b in zip(data.slots,captured.slots))):
            return None
        # A latent, quantized-to-zero trial is not a selected transaction.
        actions=tuple(level if p.action in ('buy','sell') else 0
                      for level,p in zip(plan.action_levels,plan.slots))
        baseline,base_cost=simulate(data,(0,)*len(data.slots))
        points,cost=simulate(data,actions,fixed_points=plan.slots)
        if any(p.action=='sell' for p in points):
            home_actions=tuple(max(a,0) for a in actions)
            home,_=simulate(data,home_actions,fixed_points=plan.slots)
            if any(p.grid_import_kwh>b.grid_import_kwh+EPS for p,b in zip(points,home)):
                return None
            if data.sale_pv_kwh is not None:
                risk_data=replace(data,slots=tuple(replace(s,pv_kwh=pv)
                    for s,pv in zip(data.slots,data.sale_pv_kwh)),sale_pv_kwh=None)
                risk_home,risk_home_cost=simulate(risk_data,home_actions,fixed_points=plan.slots)
                risk_points,risk_cost=simulate(risk_data,actions,fixed_points=plan.slots)
                # A lower future PV forecast gives more of the SAME discharge
                # command to the home, so its exported share can be smaller.
                # Protect home import and profitability; requiring identical
                # export would reject an unchanged, otherwise valid plan.
                if (any(p.grid_import_kwh>b.grid_import_kwh+EPS for p,b in zip(risk_points,risk_home))
                    or risk_cost>risk_home_cost+EPS):
                    return None
        delay=None
        if plan.delay_plan is not None:
            if __package__:
                from .pv_charge_delay import optimize_delay
            else:
                from pv_charge_delay import optimize_delay
            risk=None
            refill_pv=data.delay_pv_kwh if data.delay_pv_kwh is not None else data.sale_pv_kwh
            if refill_pv is not None:
                risk_data=replace(data,slots=tuple(replace(s,pv_kwh=pv)
                    for s,pv in zip(data.slots,refill_pv)),sale_pv_kwh=None,delay_pv_kwh=None)
                risk_points,_=simulate(risk_data,actions,fixed_points=plan.slots)
                risk=(risk_data,risk_points)
            captured_windows=(plan.delay_plan,*plan.delay_plan.following)
            points,delay=optimize_delay(data,points,conservative=risk,
                _windows=tuple((p.start,p.end) for p in captured_windows))
            checked_windows=(delay,*delay.following) if delay is not None else ()
            if (len(checked_windows)!=len(captured_windows)
                    or any(p.recovered_at>b.recovered_at for p,b in zip(checked_windows,captured_windows))):
                return None
            cost-=sum(p.benefit_pln for p in checked_windows)
        terminal=data.terminal_kwh if data.terminal_kwh is not None else data.reserve_kwh
        required=baseline[-1].end_kwh+EPS<terminal or data.initial_kwh<data.reserve_kwh-EPS
        if (cost>base_cost+EPS and not required
            or any(p.action!='self_use' for p in points) and not required
                and base_cost-cost < (EPS if data.continuing_action or data.active_delay is not None else max(data.minimum_benefit_pln,EPS))):
            return None
        base_need=max(_floor(replace(data,demand_margin_percent=0),bounded=False))
        total_need=max(_floor(data,bounded=False))
        margin=max(total_need-base_need,0)*data.discharge_efficiency
        return replace(plan,slots=points,cost_pln=cost,baseline_cost_pln=base_cost,
            baseline_end_kwh=baseline[-1].end_kwh,benefit_pln=max(base_cost-cost,0),
            margin_requested_kwh=margin,
            margin_unserved_kwh=min(margin,max(total_need-max(base_need,data.maximum_kwh),0)*data.discharge_efficiency),
            required_charge=required,base_shortfall_kwh=max(base_need-data.maximum_kwh,0)
                +data.unpriced_home_shortfall_kwh+max(terminal-points[-1].end_kwh,0),
            delay_plan=delay,action_levels=actions)
    except (ValueError,TypeError,AttributeError,OverflowError,ZeroDivisionError):
        return None
