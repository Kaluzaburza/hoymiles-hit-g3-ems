"""Fresh energy drift trims the captured sale, with an independent DC oracle."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
from rc2_regression_baseline import read_source

from test_rce_optimizer import RCE, NOW, base_input
from test_rce_self_consumption_filter import VerifiedSchedule

if "--base" in sys.argv:
    source = read_source("177215b", "rce_optimizer.py")
    exec(compile(source, "rce_optimizer_at_177215b.py", "exec"), RCE.__dict__)


def main():
    now = NOW.replace(hour=19, minute=2)
    start = RCE.floor_half_hour(now)
    for efficiency in (80.0, 95.0, 100.0):
        for active in (False, True):
            settings = base_input(
                now=now, battery_capacity_kwh=10.0, battery_soc_percent=90.0,
                dynamic_reserve_enabled=False,
                price_slots=[RCE.PriceSlot(start + RCE.SLOT * i, 2.0, i == 0 and not active) for i in range(3)],
                current_load_power_kw=0.6, current_pv_power_kw=0.0,
                self_consumption_filter_enabled=True,
                tariff_price_schedule=VerifiedSchedule(0.2),
                export_efficiency_percent=efficiency,
            )
            captured = RCE.optimize_rce(settings)
            assert captured.ready and captured.planned_exports
            preserved = deepcopy(captured)
            search, shadow = RCE._solve_joint_horizon_exports, RCE.evaluate_sale_vs_preserve
            def forbidden(*args, **kwargs):
                raise AssertionError("Revalidation ran an optimization search")
            RCE._solve_joint_horizon_exports = RCE.evaluate_sale_vs_preserve = forbidden
            try:
                for soc_drop, load_rise in ((0.05, 0.0), (0.0, 0.1), (0.5, 2.0)):
                    latest = replace(settings, now=now + timedelta(seconds=4),
                        battery_soc_percent=90.0 - soc_drop, current_load_power_kw=0.6 + load_rise)
                    diagnostics = {}
                    actual = RCE.revalidate_rce_plan(latest, captured,
                        captured_settings=settings, diagnostics=diagnostics)
                    assert actual is not None, (efficiency, active, soc_drop, load_rise, diagnostics)
                    assert actual.planned_exports, "A small drift discarded the whole sale"
                    original = {x.start: x.energy_kwh for x in captured.planned_exports}
                    assert all(0 < x.energy_kwh <= original[x.start] for x in actual.planned_exports)
                    assert actual.current_slot_execution_discharge_power_kw <= captured.current_slot_execution_discharge_power_kw
                    if actual.current_run_end is not None:
                        assert actual.current_run_end <= captured.current_run_end
                    # Common nominal LOAD is zero in this fixture. Do not turn
                    # its raw pulse into a second, provider-local energy forecast.
                    battery_end = 10 * latest.battery_soc_percent / 100 - actual.planned_export_kwh / (efficiency / 100)
                    assert battery_end >= 2.0 - 1e-6, battery_end
                    assert abs(actual.ending_battery_kwh - battery_end) < 1e-6
                    hours = (28*60-4)/3600
                    command = actual.current_slot_execution_discharge_power_kw
                    house_kw = min(command, latest.current_load_power_kw)
                    physical_end = 10*latest.battery_soc_percent/100-hours*(house_kw+max(command-house_kw,0.)/(efficiency/100))
                    assert physical_end >= 2.0-1e-9, physical_end
                    assert actual.self_consumption_filter_status_code == "applied"
                assert captured == preserved, "Captured evidence mutated"
                invalid = replace(settings, battery_soc_percent=19.0)
                assert RCE.revalidate_rce_plan(invalid, captured, captured_settings=settings) is None
            finally:
                RCE._solve_joint_horizon_exports, RCE.evaluate_sale_vs_preserve = search, shadow
    print("PASS: 18 fresh SOC/LOAD drifts, active/future sales, AC/DC oracle, subset/power/run limits, economics, no search, immutable evidence and hard reserve veto")


if __name__ == "__main__":
    main()
