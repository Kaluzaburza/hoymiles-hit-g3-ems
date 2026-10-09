"""BUY search counterexamples, holdout quality and bounded physical invariants."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from itertools import product
import json
import math
from pathlib import Path
import random
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'custom_components/hoymiles_hit_modbus'))
import pstryk_joint as joint


def check_physics(data, plan):
    for s, p in zip(data.slots, plan.slots):
        assert data.reserve_kwh - 1e-8 <= p.end_kwh <= data.maximum_kwh + 1e-8
        assert 0 <= p.pv_origin_kwh <= p.end_kwh + 1e-8
        assert p.command_kw <= data.ac_kw + 1e-8
        assert p.grid_charge_kwh <= data.charge_kw * s.hours + 1e-8
        assert math.isclose(p.start_kwh + p.battery_in_kwh - p.battery_out_kwh, p.end_kwh, abs_tol=1e-8)
        # AC balance is independent of the search's cost comparator.
        assert math.isclose(s.pv_kwh + p.grid_import_kwh + p.home_battery_kwh + p.battery_export_kwh,
                            s.load_kwh + p.grid_export_kwh + p.pv_charge_kwh + p.grid_charge_kwh + p.curtailed_kwh,
                            abs_tol=1e-8)
    assert plan.slots[-1].end_kwh >= plan.baseline_end_kwh - 1e-8


def main():
    cases = json.loads((ROOT / 'tools/fixtures/pstryk_buy_search_counterexamples.json').read_text())
    for row in cases:
        raw = row['input']
        raw['slots'] = tuple(joint.EnergySlot(**(s | {'start': datetime.fromisoformat(s['start']),
                                                     'end': datetime.fromisoformat(s['end'])})) for s in raw['slots'])
        data = joint.JointInput(**raw)
        plan = joint.optimize(data)
        assert plan.cost_pln < row['previous_cost'] - .000001, (row['case'], plan.cost_pln, row['previous_cost'])
        check_physics(data, plan)

    # New seed, varying 5/7-slot horizons. Exhaustive comparison shares the
    # simulator: it measures search quality, not a proof of physical modelling.
    rng = random.Random(614207)
    stamp = datetime(2026, 10, 25, 0, tzinfo=timezone.utc)
    gaps = []
    worst = 0.
    for ncase in range(48):
        size = 5 if ncase % 2 else 7
        slots = tuple(joint.EnergySlot(stamp+timedelta(minutes=30*i), stamp+timedelta(minutes=30*(i+1)),
                       rng.choice((-.2, .1, .4, .8, 1.4)), rng.choice((.1, .4, .8, 1.3)),
                       rng.choice((0., 0., .3, 1.2))) for i in range(size))
        data = joint.JointInput(slots, 6., rng.choice((1., 2., 3.)), 1., 6., 0.,
                                rng.choice((1., 2., 4.)), 4., 5., 5., allow_sell=False,
                                minimum_benefit_pln=0., terminal_kwh=1.)
        plan = joint.optimize(data)
        check_physics(data, plan)
        best = plan.cost_pln
        for actions in product((0, 1, 2), repeat=size):
            points, cost = joint.simulate(data, actions)
            if points[-1].end_kwh + 1e-8 >= plan.baseline_end_kwh:
                best = min(best, cost)
        gap = plan.cost_pln - best
        worst = max(worst, gap)
        if gap > 1e-6:
            gaps.append((ncase, gap))
        # SELL remains a second stage with a fixed home BUY budget.
        sold = joint.optimize(replace(data, allow_sell=True, pv_origin_kwh=data.initial_kwh-1))
        assert all(s.grid_charge_kwh <= p.grid_charge_kwh+1e-8 and
                   s.grid_import_kwh <= p.grid_import_kwh+1e-8 for s, p in zip(sold.slots, plan.slots))

    timings = []
    for size in (48, 96):
        slots = tuple(joint.EnergySlot(stamp+timedelta(minutes=30*i), stamp+timedelta(minutes=30*(i+1)),
                          (.2, .9, .5, .1)[i % 4], .4, max(0, math.sin(i*math.pi/24))*1.1) for i in range(size))
        data = joint.JointInput(slots, 20., 6., 2., 20., 0., 4., 5., 10., 10., allow_sell=False, terminal_kwh=2.)
        runs = []
        for _ in range(8):
            t0 = perf_counter()
            result = joint.optimize(data)
            runs.append(perf_counter()-t0)
            assert result.simulations <= 1 + 6*size + 672
        timings.append({'slots': size, 'p95_seconds': sorted(runs)[-1], 'simulations': result.simulations})
        assert max(runs) < 3., timings
    print(json.dumps({'counterexamples': len(cases), 'holdout_cases': 48,
                      'holdout_residual_gaps': gaps, 'largest_gap_pln': worst,
                      'benchmarks': timings, 'global_optimality_claimed': False}))


if __name__ == '__main__':
    main()
