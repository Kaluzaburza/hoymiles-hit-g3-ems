"""Independent live AC/DC oracle while nominal LOAD stays unchanged."""
from dataclasses import replace
from datetime import timedelta
from test_rce_optimizer import RCE, NOW, base_input
from test_pstryk_runtime import projections
from custom_components.hoymiles_hit_modbus.pstryk_joint import JointInput, EnergySlot, optimize


def main():
    now = NOW.replace(hour=18, minute=10)
    hours = 1/3
    cases = 0
    for house_eff in (.8, 1.):
        for export_eff in (.8, .95, 1.):
            for load_kw in (0., 1., 2.6, 8.414):
                settings = base_input(now=now, price_slots=[RCE.PriceSlot(now.replace(minute=0), 2.)],
                    battery_capacity_kwh=10., battery_soc_percent=30., dynamic_reserve_enabled=False,
                    manual_minimum_soc_percent=20., outage_reserve_soc_percent=0.,
                    current_load_power_kw=load_kw, current_pv_power_kw=0., current_battery_soc_fresh=True,
                    house_discharge_efficiency_percent=100*house_eff, export_efficiency_percent=100*export_eff,
                    battery_wear_cost_pln_kwh=0.)
                rce = RCE.optimize_rce(settings)
                data = JointInput((EnergySlot(now, now+timedelta(minutes=20), 2., 0., 0.),),
                    10., 3., 2., 10., 3., 10., 10., 10., 10., allow_buy=False,
                    discharge_efficiency=house_eff, sell_efficiency=export_eff,
                    wear_pln_kwh=0., current_load_power_kw=load_kw, current_pv_power_kw=0.)
                plan = optimize(data)
                pstryk, _ = projections(data, plan, revision=1, metadata={}, system_power_kw=10.)
                assert rce.current_slot_load_kwh == 0. and data.slots[0].load_kwh == 0.
                for command in (rce.current_slot_execution_discharge_power_kw,
                                pstryk['current_slot_execution_discharge_power_kw']):
                    house_kw = min(load_kw, command)
                    dc = hours*(house_kw/house_eff+max(command-house_kw, 0.)/export_eff)
                    assert 3.-dc >= 2.-1e-9, (house_eff, export_eff, load_kw, command, dc)
                if rce.current_slot_start_eligible:
                    point = next(p for p in rce.timeline_trace.points if p.selected)
                    assert point.policy.command_discharge_power_percent == rce.current_slot_execution_power_percent
                cases += 1
    print(f'PASS {cases} shared-forecast/live-command cases: stock, home/export efficiencies, pulses, reserve and command quantization')


if __name__ == '__main__':
    main()
