"""Same nominal energy across real adapters; raw power still limits execution."""
from dataclasses import replace
from datetime import timedelta
import math

from test_pstryk_runtime import settings
from test_tariff_optimizer import settings as tariff_settings
from custom_components.hoymiles_hit_modbus.load_model import expected_load_by_slot, persistent_load_delta_kw
from custom_components.hoymiles_hit_modbus.rce_optimizer import optimize_rce
from custom_components.hoymiles_hit_modbus.tariff_optimizer import optimize_tariff_charging
from custom_components.hoymiles_hit_modbus.pstryk_plan import build_joint_input, projections
from custom_components.hoymiles_hit_modbus.pstryk_joint import optimize

OPTIONS = dict(charge_efficiency=95., charge_power_percent=60., maximum_soc=95.,
               minimum_saving=0., demand_margin_percent=0., allow_buy=True, allow_sell=True)


def main():
    base = settings()
    start = base.now
    now = start + timedelta(minutes=7, seconds=30)
    base = replace(base, now=now, average_daily_load_kwh=12., average_night_load_kwh=4.,
                   load_profile_30m_kwh=(.25,)*48, battery_soc_percent=90.)
    starts = [p.start for p in base.price_slots]
    common = expected_load_by_slot(starts, now=now, daily_energy_kwh=12.,
                                    average_profile_30m_kwh=(.25,)*48).by_slot_kwh
    expected = common[start] * .75
    reserves = []
    for live in (.5, 8.414, .5):
        data = replace(base, current_load_power_kw=live)
        rce = optimize_rce(data)
        tariff = optimize_tariff_charging(tariff_settings(now, average_daily_load_kwh=12.,
                            load_by_slot_kwh=common, current_load_power_kw=live, current_pv_power_kw=0.))
        joint = build_joint_input(data, OPTIONS)
        values = (rce.current_slot_load_kwh, tariff.current_slot_load_kwh, joint.slots[0].load_kwh)
        assert all(math.isclose(v, expected, abs_tol=1e-8) for v in values), (live, values, expected)
        reserves.append(rce.minimum_soc_percent)
        if live > data.inverter_ac_power_kw:
            assert not rce.current_slot_start_eligible
            rce_attrs, tariff_attrs = projections(joint, optimize(joint), revision=1,
                                                 metadata={}, system_power_kw=5.)
            assert not rce_attrs['current_slot_start_eligible']
            assert rce_attrs['current_slot_execution_export_power_kw'] == 0.
    assert len(set(reserves)) == 1, reserves

    # No future knowledge: a 30-second pulse does not become a half-hour load.
    observations = [(now-timedelta(minutes=18-i*3), .5, .5) for i in range(7)]
    observations[-1] = (now, 8.414, .5)
    assert persistent_load_delta_kw(observations, now=now)[0] == 0.
    observations = [(t, 2.5, .5) for t, _, _ in observations]
    delta, at, _ = persistent_load_delta_kw(observations, now=now)
    corrected = expected_load_by_slot(starts, now=now, daily_energy_kwh=12.,
                    average_profile_30m_kwh=(.25,)*48, persistence_delta_kw=delta,
                    persistence_observed_at=at)
    assert corrected.by_slot_kwh[start] > common[start]
    stale = expected_load_by_slot(starts, now=now, daily_energy_kwh=12.,
                    average_profile_30m_kwh=(.25,)*48, persistence_delta_kw=delta,
                    persistence_observed_at=now-timedelta(minutes=6))
    assert stale.by_slot_kwh == common
    future = expected_load_by_slot(starts, now=now, daily_energy_kwh=12.,
                    average_profile_30m_kwh=(.25,)*48, persistence_delta_kw=delta,
                    persistence_observed_at=now+timedelta(minutes=1))
    assert future.by_slot_kwh == common
    print('PASS shared current-slot nominal energy, physical caps, pulse/persistence, gap/future')


if __name__ == '__main__':
    main()
