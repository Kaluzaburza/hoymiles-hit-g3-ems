"""Accepted active half-hour survives a small economic move to a later slot."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

from test_rce_optimizer import RCE, base_input, NOW


def fixture():
    start = NOW + timedelta(hours=6)
    before = base_input(
        now=start + timedelta(minutes=20),
        price_slots=[RCE.PriceSlot(start, .6781),
                     RCE.PriceSlot(start + timedelta(minutes=30), .68492)],
        battery_capacity_kwh=20, battery_soc_percent=40,
        average_daily_load_kwh=0, average_night_load_kwh=0,
        current_load_power_kw=0, current_pv_power_kw=0,
    )
    # The confirmed incumbent is a feasible previously accepted selection.
    accepted=RCE._optimize_rce_impl(before, fixed_exports={start: 1.5},
                                  fixed_current_discharge_cap_kw=9)
    assert accepted.current_slot_planned_export_kwh > .01
    latest=replace(before, now=start + timedelta(minutes=29,seconds=22))
    candidate=RCE.optimize_rce(latest)
    assert candidate.current_slot_planned_export_kwh == 0
    cls=getattr(RCE, 'RceActiveCommitment', SimpleNamespace)
    commitment=cls(transaction_id='rce:confirmed', started_at=before.now,
        hard_deadline=start + timedelta(hours=1),
        physical_verified_at=latest.now-timedelta(seconds=1),
        maximum_discharge_power_percent=90, minimum_soc_percent=20)
    return before,accepted,latest,candidate,commitment


def main():
    before,accepted,latest,candidate,commitment=fixture()
    retain=getattr(RCE, 'retain_active_rce_slot', lambda settings,result,**kwargs: result)
    result=retain(latest,candidate,accepted_settings=before,
                  accepted_result=accepted,commitment=commitment)
    assert result.current_slot_planned_export_kwh > .01, 'confirmed current block disappeared on replan'
    assert result.current_slot_execution_discharge_power_kw <= 9
    assert result.current_run_end <= commitment.hard_deadline
    assert result.active_slot_commitment_applied
    assert candidate.current_slot_planned_export_kwh == 0, 'caller result mutated'
    print('PASS confirmed current block preserved on fresh, bounded economic replan')
    for name, settings, evidence in (
        ('no execution', latest, None),
        ('stale physical proof', latest, replace(commitment, physical_verified_at=latest.now-timedelta(seconds=26))),
        ('expired deadline', latest, replace(commitment, hard_deadline=latest.now)),
        ('next half hour', replace(latest, now=RCE.floor_half_hour(latest.now)+timedelta(minutes=30)), commitment),
        ('zero BMS', replace(latest, bms_max_discharge_current_a=0), commitment),
        ('stale BMS', replace(latest, bms_discharge_data_fresh=False), commitment),
        ('zero GCF', replace(latest, export_power_cap_kw=0), commitment),
        ('SOC reserve', replace(latest, battery_soc_percent=20), commitment),
        ('LOAD uses discharge budget', replace(latest, current_load_power_kw=10), commitment),
        ('changed price', replace(latest, price_slots=[replace(latest.price_slots[0],price_pln_kwh=-1),latest.price_slots[1]]), commitment),
        ('changed wear', replace(latest, battery_wear_cost_pln_kwh=2), commitment),
        ('malformed power', latest, replace(commitment,maximum_discharge_power_percent=float('nan'))),
    ):
        denied=retain(settings,candidate,accepted_settings=before,
                      accepted_result=accepted,commitment=evidence)
        assert not denied.active_slot_commitment_applied, name
        print('PASS commitment denied:',name)
    capped=retain(replace(latest,bms_max_discharge_current_a=30),candidate,
                  accepted_settings=before,accepted_result=accepted,commitment=commitment)
    assert capped.active_slot_commitment_applied
    assert 0 < capped.current_slot_execution_discharge_power_kw <= 1.5
    print('PASS fresh positive BMS cap reduces retained power to <=1.5 kW')


if __name__ == '__main__':
    main()
