"""Rolling offline replay of production Pstryk planning on qualified site data.

Reads the explicit CSV produced by the public-history investigation. No HA,
network, API key or inverter access. Actual future LOAD/PV is used ONLY to
score the applied first action; forecasts use previously completed days.
"""
import argparse
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
from collections import defaultdict
import csv,hashlib,json,math,sys,time
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'custom_components/hoymiles_hit_modbus'))
from pstryk_joint import EnergySlot,JointInput,optimize,simulate

WARSAW=ZoneInfo('Europe/Warsaw')

def replay(path, output, capacity=12.5, *, pv_charge_delay=False):
    rows=list(csv.DictReader(path.open(encoding='utf-8-sig',newline='')))
    data=[(datetime.fromisoformat(r['start_utc']),float(r['price_net_reference_pln_kwh']),
           float(r['load_observed_profile_kwh']),float(r['pv_counter_kwh_linear_allocation'])*.95) for r in rows]
    assert all(data[i][0]+timedelta(minutes=30)==data[i+1][0] for i in range(len(data)-1))
    history=defaultdict(dict)
    for at,price,load,pv in data:
        local=at.astimezone(WARSAW)
        history[local.date()][local.hour*2+local.minute//30]=(load,pv)
    initial=capacity*.43; reserve=capacity*.20; maximum=capacity*.90
    policy_energy=base_energy=initial
    cost=base_cost=0.; actions=defaultdict(int); trace=[]
    total_import=total_export=charge_total=sale_total=0.
    durations=[]
    for index,(at,price,actual_load,actual_pv) in enumerate(data):
        day=at.astimezone(WARSAW).date()
        previous_days=sorted(d for d in history if d<day)[-7:]
        assert all(d<day for d in previous_days), 'Forecast lookahead'
        def forecast(stamp):
            local=stamp.astimezone(WARSAW); slot=local.hour*2+local.minute//30
            accepted=[history[d][slot] for d in previous_days if slot in history[d]]
            if not accepted: return 16./48,0.  # explicit fixed cold-start fallback
            return tuple(sum(pair[k] for pair in accepted)/len(accepted) for k in (0,1))
        slots=[]
        for future,net,_unused_load,_unused_pv in data[index:]:
            if future.astimezone(WARSAW).date()!=day: break
            load,pv=forecast(future)
            if not slots and index:
                # Latest completed interval is a conservative telemetry proxy;
                # the current interval's future integral is not fed to planning.
                load,pv=data[index-1][2:]
            slots.append(EnergySlot(future,future+timedelta(minutes=30),net,load,pv))
        terminal=reserve
        tail=[]; cursor=slots[-1].end
        night_end=datetime.combine(day+timedelta(days=1),datetime.min.time(),WARSAW)+timedelta(hours=6)
        while cursor<night_end.astimezone(timezone.utc):
            tail.append(forecast(cursor));cursor+=timedelta(minutes=30)
        for load,pv in reversed(tail):
            terminal=max(reserve,terminal+max(load-pv,0)/.95-min(max(pv-load,0)*.95,1.5*.95))
        request=JointInput(tuple(slots),capacity,policy_energy,reserve,maximum,
            policy_energy,3.,3.,5.,5.,terminal_kwh=min(terminal,maximum),
            unpriced_home_shortfall_kwh=max(terminal-maximum,0), allow_delay=pv_charge_delay)
        began=time.perf_counter()
        plan=optimize(request)
        durations.append(time.perf_counter()-began)
        selected=plan.slots[0]
        actions[selected.action]+=1
        real_slot=EnergySlot(at,at+timedelta(minutes=30),price,actual_load,actual_pv)
        actual_input=replace(request,slots=(real_slot,),terminal_kwh=None,
            charge_kw=min(3.,selected.command_kw) if selected.action=='buy' else 3.,
            discharge_kw=min(3.,selected.command_kw) if selected.action=='sell' else 3.)
        if selected.action=='sell':
            command=-2; target=selected.protected_kwh
        elif selected.action=='buy':
            command=2
            target=max(reserve,math.floor(selected.end_kwh/capacity*100+1e-8)*capacity/100)
        elif selected.action=='pv_charge_hold': command=4;target=reserve
        else: command=0;target=reserve
        applied,step_cost=simulate(actual_input,(command,),(target,target))
        p=applied[0]
        baseline_input=replace(actual_input,initial_kwh=base_energy,pv_origin_kwh=0.,charge_kw=3.,discharge_kw=3.)
        baseline,baseline_cost=simulate(baseline_input,(0,))
        base_energy=baseline[0].end_kwh;base_cost+=baseline_cost
        policy_energy=p.end_kwh;cost+=step_cost
        total_import+=p.grid_import_kwh;total_export+=p.grid_export_kwh
        charge_total+=p.grid_charge_kwh;sale_total+=p.battery_export_kwh
        assert reserve-1e-7<=p.end_kwh<=max(maximum,p.start_kwh)+1e-7
        assert not(p.grid_charge_kwh>1e-8 and p.battery_export_kwh>1e-8)
        assert p.battery_export_kwh/.95<=request.pv_origin_kwh+1e-7
        assert abs(p.grid_import_kwh+p.pv_kwh+p.home_battery_kwh+p.battery_export_kwh-
                   p.load_kwh-p.grid_export_kwh-p.grid_charge_kwh-p.pv_charge_kwh-p.curtailed_kwh)<1e-7
        trace.append({'start_utc':at.isoformat(),'net':price,'load_actual_kwh':actual_load,'pv_actual_ac_assumed_kwh':actual_pv,
            'forecast_completed_days':len(previous_days),'action':selected.action,'command_kw':selected.command_kw,
            'grid_charge_kwh':p.grid_charge_kwh,'battery_export_kwh':p.battery_export_kwh,
            'soc_percent':policy_energy/capacity*100,'qualified_physical_stock_kwh':policy_energy,
            'cumulative_cost_pln':cost,'baseline_cost_pln':base_cost})
        if index%48==47: print(f"Completed {day}: {index+1} decisions",flush=True)
    terminal_value=.5
    report={'status':'OFFLINE_ROLLING_PRODUCTION_CORE_REPLAY','input_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
        'optimizer_sha256':hashlib.sha256(Path(sys.modules['pstryk_joint'].__file__).read_bytes()).hexdigest(),
        'from':data[0][0].isoformat(),'to':(data[-1][0]+timedelta(minutes=30)).isoformat(),
        'pv_charge_delay_assumed_physically_accepted':pv_charge_delay,
        'decisions':len(data),'forecast_lookahead':False,'price_availability_assumption':'Current Warsaw day quotes known; no tomorrow quotes assumed',
        'physical_assumptions':{'capacity_kwh':capacity,'initial_soc':43,'reserve_soc':20,'maximum_soc':90,
            'ac_charge_discharge_kw':3,'inverter_ac_kw':5,'gcf_export_kw':5,'efficiencies':.95,'pv_counter_to_ac':.95,
            'wear_dc_pln_kwh':.08,'sale_stock_basis':'qualified_system_soc_like_rce'},
        'cost_pln':round(cost,6),'baseline_cost_pln':round(base_cost,6),'end_kwh':round(policy_energy,6),
        'baseline_end_kwh':round(base_energy,6),'terminal_valuation_pln_ac_kwh':terminal_value,
        'terminal_adjusted_benefit_pln':round(base_cost-cost+(policy_energy-base_energy)*.95*terminal_value,6),
        'actions':dict(actions),'grid_import_kwh':round(total_import,6),'grid_export_kwh':round(total_export,6),
        'grid_to_battery_ac_kwh':round(charge_total,6),'battery_export_ac_kwh':round(sale_total,6),
        'origin_store_changes':0,'solver_ms_p95':round(sorted(durations)[int(len(durations)*.95)]*1000,2),
        'solver_ms_max':round(max(durations)*1000,2),
        'limitations':['Scenario, not invoice savings or field acceptance','Forecast uses past days only; first day fixed fallback',
            'Native counter timing and PV AC/DC calibration unavailable in archived sample',
            'Executor/leases/readbacks validated separately, not simulated as real device acknowledgements']}
    output.mkdir(parents=True,exist_ok=True)
    (output/'REPLAY.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    with (output/'TRACE.csv').open('w',newline='',encoding='utf-8') as stream:
        writer=csv.DictWriter(stream,fieldnames=list(trace[0]));writer.writeheader();writer.writerows(trace)
    print(json.dumps(report,indent=2))
    return report

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--capacity-kwh',type=float,default=12.5)
    parser.add_argument('--pv-charge-delay',action='store_true',help='Offline simulation of the new device contract, not field approval')
    args=parser.parse_args()
    replay(args.input,args.output,args.capacity_kwh,pv_charge_delay=args.pv_charge_delay)
