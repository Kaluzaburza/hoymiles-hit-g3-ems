"""Full-horizon regression of unnecessary Sunday G12w action fragments."""
from dataclasses import replace
from datetime import datetime, date, timezone, timedelta
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'custom_components/hoymiles_hit_modbus'))
from tariff_optimizer import TariffOptimizerInput, TariffSchedule, TariffActiveCommitment, SLOT, optimize_tariff_charging


def fixture():
    values = json.loads((ROOT / 'tools/fixtures/tariff_g12w_20261004.json').read_text())['input']
    values['now'] = datetime.fromisoformat(values['now']).astimezone(ZoneInfo('Europe/Warsaw'))
    for key in ('pv_by_slot_kwh', 'load_by_slot_kwh', 'pv_p10_by_slot_kwh'):
        values[key] = {datetime.fromisoformat(k): v for k, v in values[key].items()}
    values['pv_p10_available_dates'] = tuple(date.fromisoformat(x) for x in values['pv_p10_available_dates'])
    values['schedule'] = TariffSchedule(**values['schedule'])
    return TariffOptimizerInput(**values)


def ranges(result):
    return sum(i == 0 or item.start.astimezone(timezone.utc) != result.planned_charges[i-1].start.astimezone(timezone.utc) + SLOT for i, item in enumerate(result.planned_charges))


def main():
    settings = fixture()
    result = optimize_tariff_charging(settings)
    assert result.planned_charges[0].start == datetime(2026, 10, 5, 0, 0, tzinfo=settings.now.tzinfo), result.planned_charges[0]
    assert ranges(result) <= 3, ranges(result)
    # Baseline e915066 with the SAME nominal first-slot LOAD (0.789336040811391
    # kWh/half-hour, remaining 0.6726846042680301 kWh). The old raw 0.282 kW
    # override understated this slot by 0.5525221838313634 kWh. Compare equal
    # demand: no extra purchases, wear or uncovered expensive demand.
    assert abs(result.current_slot_load_kwh - .6726846042680301) < 1e-9
    assert abs(result.optimized_grid_cost_pln - 6.681969383624824) < 1e-8
    assert abs(result.optimized_optimization_cost_pln - 6.767469383624824) < 1e-8
    assert abs(result.optimized_grid_import_kwh - 10.596208981327027) < 1e-8
    assert abs(result.ending_battery_kwh - 12.110888884827101) < 1e-8
    for field in ('remaining_expensive_import_kwh', 'terminal_shortfall_kwh', 'base_energy_shortfall_kwh', 'hard_reserve_shortfall_kwh', 'demand_margin_unserved_kwh'):
        assert getattr(result, field) < 1e-6, (field, getattr(result, field))
    assert min(p.soc_percent for p in result.timeline_trace.points) >= 25
    assert result.latest_equivalent_start == result.next_charge_start
    assert result.latest_feasible_start > result.latest_equivalent_start
    assert result.latest_start_search_complete
    assert result.layout_candidates_evaluated <= 19
    # A proven support run survives equivalent or better reallocations.
    # The old stabilizer also rejected a strictly cheaper 05:00 whole run
    # because it used abs(cost_delta). Compare the complete horizon below.
    for hour in (1, 2, 5):
        at = settings.now.replace(day=5, hour=hour, minute=0, second=0, microsecond=0)
        previous = next(p for p in result.timeline_trace.points if p.end == at)
        commitment = TariffActiveCommitment(
            'test:support', 'grid_support', result.next_charge_start,
            at.replace(hour=6), previous.soc_percent-1, 50, at,
        )
        active = replace(settings, now=at, battery_soc_percent=previous.soc_percent,
                         current_load_power_kw=settings.load_by_slot_kwh[at]*2,
                         current_battery_power_kw=0, active_commitment=commitment)
        for delta in (0, 1e-9, -1e-9):
            continued = optimize_tariff_charging(replace(active, current_load_power_kw=active.current_load_power_kw+delta))
            if hour == 5:
                import tariff_optimizer as module
                with patch.object(module, '_stabilize_active_tariff_support',
                    side_effect=lambda s,st,lo,pl,su,ra,fr,sim:(su,sim,False)):
                    before = optimize_tariff_charging(active)
                assert before.current_action == 'none'
                assert continued.current_action == 'grid_support'
                assert continued.active_commitment_applied
                assert continued.current_run_continue_eligible
                assert continued.optimized_optimization_cost_pln < before.optimized_optimization_cost_pln
                assert abs(continued.optimized_grid_import_kwh-before.optimized_grid_import_kwh)<1e-6
                assert abs(continued.ending_battery_kwh-before.ending_battery_kwh)<1e-6
                assert continued.current_grid_charge_run_end == commitment.hard_deadline
            else:
                assert continued.current_action == 'grid_support'
                assert continued.current_grid_charge_run_end == commitment.hard_deadline
                assert continued.current_run_continue_eligible
                assert continued.current_run_need_class == 'economic'
        stale = optimize_tariff_charging(replace(active, active_commitment=replace(
            commitment, physical_verified_at=at-timedelta(seconds=31))))
        assert not stale.active_commitment_applied
        missing = optimize_tariff_charging(replace(active, control_inputs_fresh=False))
        assert not missing.active_commitment_applied
        covered = optimize_tariff_charging(replace(active, current_pv_power_kw=10))
        assert not covered.active_commitment_applied
    print('PASS: Sunday layout, cost, imports, reserve, protected demand and terminal stock')


if __name__ == '__main__':
    main()
