import sys,json
from pathlib import Path
from datetime import datetime,timedelta,date,time
from zoneinfo import ZoneInfo
from dataclasses import replace
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'tools'))
from test_rce_optimizer import RCE,base_input
from tariff_price_schedule import TariffPriceConfig,build_tariff_price_schedule
BASE=ROOT/'tools/fixtures/rce_stability'
live=json.loads((BASE/'field_20260927.json').read_text(encoding='utf-8'))
inp=live['inputs']
p=live['plan'];tz=ZoneInfo('Europe/Warsaw')
prices=[]
for day,rows in [(date(2026,9,27),inp['prices_today']),(date(2026,9,28),[(r['period'],r['rce_pln']) for r in live['prices'][0]['attributes']['value']])]:
 for i in range(0,len(rows),2):
  stamp=datetime.combine(day,time.fromisoformat(rows[i][0][:5]),tzinfo=tz)
  prices.append(RCE.PriceSlot(stamp,(rows[i][1]+rows[i+1][1])/2000))
pvraw={datetime.fromisoformat(r[0]).astimezone(tz):r[1]*.5 for r in inp['pv_tomorrow']}
lowraw={datetime.fromisoformat(r[0]).astimezone(tz):r[2]*.5 for r in inp['pv_tomorrow']}
pv={k:v/sum(pvraw.values())*p['forecast_tomorrow_kwh'] for k,v in pvraw.items()}
low={k:v/sum(lowraw.values())*p['forecast_tomorrow_p10_kwh'] for k,v in lowraw.items()}
risk=p['forecast_uncertainty_risk_weight'];conservative={k:pv[k]*(1-risk)+low[k]*risk for k in pv}
now=datetime(2026,9,27,19,27,34,tzinfo=tz)
cfg=TariffPriceConfig('G12w',.9741,.6306,1.2304,1.2304,((780,900),(1320,360)),weekend_low_price=True,polish_holidays_low_price=True,operator='TAURON')
schedule=build_tariff_price_schedule(cfg,start=now.replace(hour=0,minute=0),end=now+timedelta(days=3),local_zone=tz)
s=base_input(now=now,price_slots=prices,pv_by_slot_kwh=pv,conservative_pv_by_slot_kwh=conservative,
 battery_capacity_kwh=15,battery_soc_percent=91,outage_reserve_soc_percent=10,manual_minimum_soc_percent=60,
 average_daily_load_kwh=p['selected_average_daily_load_kwh'],average_night_load_kwh=7.8,night_start_minute=17*60+8,night_end_minute=8*60+14,
 discharge_power_percent=80,export_efficiency_percent=80,inverter_power_kw=10,
 bms_max_discharge_current_a=6.04/.8*1000/50,bms_max_charge_current_a=6.545*1000/50,battery_voltage_v=50,bms_power_safety_percent=100,
 charge_efficiency_percent=95,house_discharge_efficiency_percent=95,export_power_cap_kw=10,avoided_import_price_pln_kwh=.9741,
 day3_pv_forecast_kwh=51.41,battery_wear_cost_pln_kwh=.08,
 load_profile_30m_kwh=tuple(p['recorder_load_profile_30m_kwh']),weekday_load_profile_30m_kwh=tuple(p['recorder_load_weekday_profile_30m_kwh']),weekend_load_profile_30m_kwh=tuple(p['recorder_load_weekend_profile_30m_kwh']),
 current_load_power_kw=.54,current_pv_power_kw=0,tariff_price_schedule=schedule,self_consumption_filter_enabled=True,
 conservative_daily_load_kwh=19.2,conservative_night_load_kwh=7.8,load_history_days=10)
def compact(r):
 return dict(status=r.status_code,energy=r.planned_export_kwh,net=r.net_objective_pln,minimum_soc=r.minimum_soc_percent,slots=[(x.start.astimezone(tz).isoformat(),round(x.energy_kwh,4),x.price_pln_kwh) for x in r.planned_exports],legacy=[(x.start.astimezone(tz).isoformat(),round(x.energy_kwh,4)) for x in r.legacy_planned_exports],filter_reason=r.self_consumption_filter_reason_code)

def main():
    results=[]
    for clock in ['18:59:48','19:00:00','19:01:42']:
        t=datetime.fromisoformat('2026-09-27T'+clock).replace(tzinfo=tz)
        result=RCE.optimize_rce(replace(s,now=t,battery_soc_percent=93,current_load_power_kw=.648))
        assert result.ready
        assert result.net_objective_pln >= 20.44, (clock, compact(result))
        assert any(x.start.date()==t.date() for x in result.planned_exports), clock
        results.append(result)
        print('PASS better feasible combined plan',clock,result.net_objective_pln)
    assert results[-1].net_objective_pln >= results[0].net_objective_pln-.02

if __name__=='__main__':main()
