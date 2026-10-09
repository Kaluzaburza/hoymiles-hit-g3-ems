"""Bounded offline comparison, NOT a production scheduler or control command.

Uses both production energy balances and integer 4306 power steps. Prices
and two LOAD levels come from the older miernik observation; all future
stock, LOAD, PV and limits in these scenarios are controlled test inputs.
No claim of the actual site's whole-day saving or future LOAD is made.
"""
from __future__ import annotations
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'custom_components/hoymiles_hit_modbus'))
import rce_optimizer as R
import pstryk_joint as P

START=datetime(2026,10,8,16,tzinfo=timezone.utc)
HOURS=.5
MINUTES=5.  # Existing RCE start requires >=5 minutes, not an invented cooldown.
STEP=.32    # 32 kW system, integer 4306 percent.
ETA=.95
EPS=1e-6

@dataclass(frozen=True)
class Case:
    name: str
    tail: float=1.325
    later: float=1.5
    load: float=2.312
    later_load: float=2.312
    early_price: float=.752455
    later_price: float | None=.820125
    pv: float=0.
    initial: float=209.3
    reserve: float=46.
    discharge: float=32.
    export: float=40.
    later_export: float=40.
    later_allowed: bool=True
    fresh: bool=True
    future_house: float=0.
    minimum_export: float=2.

def settings(c, at):
    return R.OptimizerInput(now=at,price_slots=[],pv_by_slot_kwh={},
        battery_capacity_kwh=230.,battery_soc_percent=c.initial/230.*100.,
        outage_reserve_soc_percent=c.reserve/230.*100.,safety_margin_soc_percent=0.,
        manual_minimum_soc_percent=c.reserve/230.*100.,dynamic_reserve_enabled=False,
        average_daily_load_kwh=0.,average_night_load_kwh=0.,
        night_start_minute=1200,night_end_minute=480,inverter_power_kw=16.,inverter_count=2,
        inverter_ac_power_kw=20.,discharge_power_percent=min(c.discharge/32.*100.,100.),
        export_efficiency_percent=95.,house_discharge_efficiency_percent=95.,
        minimum_net_export_power_kw=c.minimum_export,bms_max_discharge_current_a=c.discharge/ETA/.05/.95,
        bms_max_charge_current_a=640.,bms_charge_data_fresh=True,
        bms_charge_data_available=True,bms_charge_data_age_seconds=0.,
        battery_voltage_v=50.,bms_power_safety_percent=95.,bms_discharge_data_fresh=c.fresh,
        bms_discharge_data_available=c.fresh,bms_discharge_data_age_seconds=0.,
        current_battery_soc_fresh=c.fresh,current_load_power_kw=c.load,current_pv_power_kw=c.pv,
        export_power_cap_kw=c.export,effective_export_power_kw=c.export)

def shape(c, energy, load, pv, export_cap, *, shorten):
    if energy<=EPS: return 0.,0.,0.
    deficit=max(load-pv,0.)
    if not c.fresh: return None
    # Search finite existing register values. Never invent a sub-register power
    # or a time shorter than the current natural-start contract.
    choices=[]
    for percent in range(1,101):
        command=percent*STEP
        net=command-deficit
        if (net+EPS<c.minimum_export or command>c.discharge+EPS
                or command+min(load,pv)>40.+EPS
                or net+max(pv-load,0.)>export_cap+EPS): continue
        hours=energy/net if shorten else HOURS
        if hours>HOURS+EPS or hours<MINUTES/60.-EPS: continue
        actual=net*hours
        if actual>energy+EPS: continue
        choices.append((command,hours,actual))
    if not choices:return None
    # Prefer the longest usable duration at the lowest sufficient command.
    # Economics of each candidate is checked in the complete trajectory below.
    return min(choices,key=lambda x:x[0]) if shorten else max(choices,key=lambda x:x[2])

def pieces(c, kind):
    if kind=='defer' and (not c.later_allowed or c.later_price is None or c.later<=EPS):
        return None
    early=0. if kind in ('defer','keep_later_only') else c.tail
    late=c.later+(c.tail if kind=='defer' else 0.)
    specs=[]
    for i,energy in ((0,early),(1,0.),(2,late),(3,0.)):
        load=c.later_load if i==2 else c.future_house if i==3 else c.load
        pv=c.pv if i==0 else 0.
        price=c.early_price if i==0 else c.later_price if i==2 else .4
        if energy>EPS and (price is None or price<0):return None
        cap=c.later_export if i==2 else c.export
        block=shape(c,energy,load,pv,cap,shorten=kind!='spread' or i!=0)
        if block is None:return None
        command,hours,export=block
        start=START+timedelta(minutes=30*i)
        if export>EPS:
            end=start+timedelta(hours=hours)
            specs.append((start,end,price,load,pv,export,command,cap))
            start=end
        end=START+timedelta(minutes=30*(i+1))
        if (end-start).total_seconds()>1e-5:
            specs.append((start,end,price,load,pv,0.,0.,cap))
    return specs

def evaluate(c, kind, engine):
    specs=pieces(c,kind)
    if specs is None:return dict(kind=kind,engine=engine,valid=False,reason='no_executable_shape_or_unqualified_destination')
    end=c.initial;cost=0.;actual=0.;grid_import=0.;valid=True;rows=[]
    if engine=='pstryk':
        slots=tuple(P.EnergySlot(a,b,p,load*(b-a).total_seconds()/3600.,pv*(b-a).total_seconds()/3600.)
                    for a,b,p,load,pv,e,cmd,cap in specs)
        data=P.JointInput(slots,230.,c.initial,c.reserve,230.,max(c.initial-c.reserve,0.),
            32.,c.discharge,40.,max(c.export,c.later_export),
            power_step_kw=STEP,minimum_export_kw=c.minimum_export,allow_buy=False,
            sell_efficiency=ETA,discharge_efficiency=ETA,wear_pln_kwh=.08,
            sale_reserve_kwh=tuple(c.reserve for _ in slots))
        points,cost=P.simulate(data,tuple(-2 if e>EPS else 0 for *_,e,cmd,cap in specs),
            exports={a:e for a,b,p,load,pv,e,cmd,cap in specs if e>EPS})
        home_points,_=P.simulate(data,(0,)*len(slots))
        if cost is None:return dict(kind=kind,engine=engine,valid=False,reason='unpriced_horizon')
        end=points[-1].end_kwh
        for spec,point,home in zip(specs,points,home_points):
            a,b,p,load,pv,e,cmd,cap=spec
            valid &= point.battery_export_kwh+EPS>=e and point.end_kwh+EPS>=c.reserve
            valid &= point.grid_export_kwh<=cap*(b-a).total_seconds()/3600.+EPS
            # Like the production sale search, compare with the home-only
            # trajectory, not with a possibly already unsafe existing sale.
            valid &= point.grid_import_kwh<=home.grid_import_kwh+EPS
            if e>EPS:valid &= abs(point.command_kw-cmd)<=EPS
            actual+=point.battery_export_kwh;grid_import+=point.grid_import_kwh
            rows.append(dict(start=a.isoformat(),end=b.isoformat(),command_kw=point.command_kw,
                             export_kwh=point.battery_export_kwh,battery_kwh=point.end_kwh))
    else:
        for a,b,p,load,pv,e,cmd,cap in specs:
            if p is None:return dict(kind=kind,engine=engine,valid=False,reason='unpriced_horizon')
            hours=(b-a).total_seconds()/3600.
            s=replace(settings(c,a),export_power_cap_kw=cap,effective_export_power_kw=cap)
            result=R._simulate_physical_slot(s,battery_kwh_dc=end,load_kwh_ac=load*hours,
                pv_kwh_ac=pv*hours,controlled_export_kwh_ac=e,hard_floor_kwh_dc=c.reserve,
                export_floor_kwh_dc=c.reserve,slot_fraction=hours*2)
            valid &= result.feasible and not result.home_energy_shortage
            end=result.battery_after_kwh_dc
            dc_out=(result.delivered_to_load_kwh_ac+result.controlled_export_kwh_ac)/ETA
            cost+=(result.grid_import_kwh_ac-result.controlled_export_kwh_ac-result.natural_export_kwh_ac)*p+dc_out*.08
            actual+=result.controlled_export_kwh_ac;grid_import+=result.grid_import_kwh_ac
            rows.append(dict(start=a.isoformat(),end=b.isoformat(),command_kw=cmd,
                             export_kwh=result.controlled_export_kwh_ac,battery_kwh=end))
    count=sum(r['export_kwh']>EPS for r in rows)
    # A common controlled terminal value prices energy retained after this
    # scenario, rather than pretending that unsold stock has zero value.
    objective=-cost+max(end-c.reserve,0.)*.2
    return dict(kind=kind,engine=engine,valid=bool(valid and c.fresh),starts=count,
        export_kwh=actual,end_kwh=end,grid_import_kwh=grid_import,
        objective_pln=objective,cost_pln=cost,rows=rows)

def run():
    cases=[Case('recorded_prices_controlled_stock'),
        Case('later_cheaper',early_price=.90,later_price=.60),
        Case('equal_prices',later_price=.752455),
        Case('tiny_tail',tail=.3),Case('too_short_even_at_minimum',tail=.05),
        Case('later_cap_full',later_export=3.2),
        Case('later_buy_or_blocked',later=0.,later_allowed=False),
        Case('future_prices_missing',later_price=None),
        Case('reserve_tight',initial=49.),Case('future_home_needed',initial=52.,future_house=8.),
        Case('bms_zero',discharge=0.),Case('gcf_zero',export=0.,later_export=0.),
        Case('stale_data',fresh=False),Case('lower_load',load=.933,later_load=.933),
        Case('pv_headroom_cost',initial=229.,pv=8.,later=1.5),
    ]
    reports=[]
    for c in cases:
        engines={}
        for engine in ('rce','pstryk'):
            variants=[evaluate(c,kind,engine) for kind in ('spread','shorter','defer','keep_later_only')]
            baseline=variants[3]
            allowed=[v for v in variants if v['valid'] and baseline['valid']
                     and v['grid_import_kwh']<=baseline['grid_import_kwh']+EPS
                     and v['objective_pln']>=baseline['objective_pln']-EPS]
            best=max(allowed,key=lambda v:(round(v['objective_pln'],7),-v['starts'])) if allowed else None
            engines[engine]={'variants':variants,'best':best['kind'] if best else None}
        reports.append(dict(case=c.name,inputs=c.__dict__,engines=engines))
    by={r['case']:r['engines'] for r in reports}
    for engine in ('rce','pstryk'):
        for name in ('recorded_prices_controlled_stock','equal_prices','tiny_tail','too_short_even_at_minimum','lower_load'):
            assert by[name][engine]['best']=='defer',(name,engine,by[name][engine]['best'])
        for name in ('later_cheaper','later_cap_full','later_buy_or_blocked'):
            assert by[name][engine]['best']=='shorter',(name,engine,by[name][engine]['best'])
        for name in ('bms_zero','gcf_zero','stale_data','future_prices_missing','reserve_tight','future_home_needed'):
            assert by[name][engine]['best'] is None,(name,engine)
        typical=by['recorded_prices_controlled_stock'][engine]['variants']
        short=next(v for v in typical if v['kind']=='shorter')
        deferred=next(v for v in typical if v['kind']=='defer')
        assert deferred['starts']==1 and short['starts']==2
        assert abs(deferred['export_kwh']-short['export_kwh'])<EPS
        assert abs(deferred['objective_pln']-short['objective_pln']-1.325*(.820125-.752455))<EPS
    # Shared physical definitions, not a cloned arithmetic oracle: both
    # production simulators must agree on the same feasible trajectory.
    parity=0
    model_differences=[]
    for report in reports:
        left=report['engines']['rce']['variants'];right=report['engines']['pstryk']['variants']
        for a,b in zip(left,right):
            if a['valid'] and b['valid']:
                differences={key:{'rce':a[key],'pstryk':b[key]}
                    for key in ('export_kwh','end_kwh','cost_pln','grid_import_kwh')
                    if abs(a[key]-b[key])>=EPS}
                if differences:
                    model_differences.append(dict(case=report['case'],kind=a['kind'],differences=differences))
                else:parity+=1
    assert not model_differences, model_differences
    lower=Case('recorded_load_step',load=.933)
    command,hours,energy=shape(lower,1.325,lower.load,0.,40.,shorten=True)
    after_net=command-2.312
    assert after_net<lower.minimum_export
    load_step=dict(initial_load_kw=.933,later_load_kw=2.312,planned_command_kw=command,
        planned_duration_minutes=hours*60.,net_export_after_load_step_kw=after_net,
        minimum_net_export_kw=lower.minimum_export,
        conclusion='A short block alone still loses export eligibility after the recorded LOAD increase; no inferred LOAD headroom is added.')
    return dict(status='PASS_OFFLINE_COMPARISON',cases=len(cases),engines=2,
        variants_per_case=4,parity_trajectories=parity,pv_parity='PASS_CONTROLLED_TRAJECTORIES',
        model_differences=model_differences,
        load_step_counterexample=load_step,
        scope='Controlled trajectories, not real-site daily optimum or deployment acceptance.',
        runtime_planner_changed=True,production_short_block_deadline_supported=False,
        conclusion='Deferral can remove one start when already-selected later capacity is profitable and feasible; short blocks improve power feasibility but do not inherently reduce starts.',
        reports=reports)

if __name__=='__main__':
    result=run()
    if len(sys.argv)>1:Path(sys.argv[1]).write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='reports'},indent=2))
