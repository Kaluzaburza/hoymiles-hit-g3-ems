"""A confirmed 19:00-21:00 run survives replans and half-hour boundaries."""
from dataclasses import replace
from datetime import timedelta
from test_rce_optimizer import RCE, base_input, NOW


def main():
    start=NOW.replace(hour=19,minute=0,second=0)
    before=base_input(now=start, battery_capacity_kwh=100, battery_soc_percent=90,
        price_slots=[RCE.PriceSlot(start+RCE.SLOT*i, 1.0 if i<4 else 2.0) for i in range(8)],
        average_daily_load_kwh=0,average_night_load_kwh=0,current_load_power_kw=0,current_pv_power_kw=0)
    accepted=RCE._optimize_rce_impl(before,fixed_exports={start+RCE.SLOT*i:1.0 for i in range(4)},fixed_current_discharge_cap_kw=2)
    assert accepted.current_run_end==start+timedelta(hours=2)
    for minute in [5,31,65,95,119]:
        latest=replace(before,now=start+timedelta(minutes=minute),battery_soc_percent=85)
        new=RCE._optimize_rce_impl(latest,fixed_exports={start+timedelta(hours=3):1},fixed_current_discharge_cap_kw=2)
        proof=RCE.RceActiveCommitment(transaction_id='rce:run',started_at=start,
            hard_deadline=start+timedelta(hours=2),physical_verified_at=latest.now,
            maximum_discharge_power_percent=20,minimum_soc_percent=20)
        kept=RCE.retain_active_rce_slot(latest,new,accepted_settings=before,accepted_result=accepted,commitment=proof)
        assert kept.active_slot_commitment_applied, ('run lost',minute)
        assert kept.current_slot_planned_export_kwh>0,minute
        assert kept.current_run_end==proof.hard_deadline,minute
        assert kept.current_slot_execution_discharge_power_kw<=2,minute
        run_slots=[x for x in kept.planned_exports if RCE.floor_half_hour(latest.now)<=x.start<proof.hard_deadline]
        assert len(run_slots)==4-minute//30,minute
        print('PASS committed run at minute',minute)
    for label,change in [('zero BMS',{'bms_max_discharge_current_a':0}),
                         ('zero export',{'export_power_cap_kw':0}),
                         ('reserve',{'battery_soc_percent':20}),
                         ('stale BMS',{'bms_discharge_data_fresh':False})]:
        bad=RCE.retain_active_rce_slot(replace(latest,**change),new,accepted_settings=before,accepted_result=accepted,commitment=proof)
        assert not bad.active_slot_commitment_applied,label
        print('PASS veto',label)


if __name__=='__main__':main()
