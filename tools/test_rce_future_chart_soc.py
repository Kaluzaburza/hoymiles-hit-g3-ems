"""Future chart arbitration uses its continuous SOC cursor, never live SOC."""
from copy import deepcopy
from dataclasses import replace

import test_supervisor_canonical_runtime as T


def scenario(*, charge=True, soc=60.0, floor=62.0, context_changes=None):
    frame = T.frame()
    frame.context = replace(frame.context, battery_soc_percent=soc,
                            **(context_changes or {}))
    frame.execution = replace(frame.execution, battery_soc_percent=soc)
    timelines = T.timelines(rce_future_selected=True, tariff_selected=charge)
    for point in timelines['rce']['points']:
        point['protected_soc_floor_percent'] = floor
    return frame, timelines


def run(frame, timelines):
    return T.build_supervisor_canonical_ledger(
        frame=frame, timelines=timelines, usable_capacity_kwh=T.CAPACITY)


def exports(ledger):
    return [s for s in ledger.slots if s.selected_action is T.LedgerAction.RCE_EXPORT]


def main():
    frame, timelines = scenario()
    before = deepcopy(frame)
    ledger = run(frame, timelines)
    assert exports(ledger), 'future sale disappeared despite prior charge above reserve'
    assert all(s.starts_at > T.NOW for s in exports(ledger))
    assert all(s.start_eligibility is T.StartEligibility.UNVERIFIED for s in exports(ledger))
    assert frame == before, 'display calculation changed the live frame'
    assert frame.decision.selected_policy is None, 'display authorized live execution'

    # PV alone can build the same reserve before the future sale.
    frame, timelines = scenario(charge=False)
    for policy in ('rce', 'tariff'):
        timelines[policy]['points'][0].update(pv_kw=2.1, battery_kw=1.0, soc_percent=65.0)
    assert exports(run(frame, timelines)), 'PV recharge was ignored in future arbitration'

    frame, timelines = scenario(charge=False)
    assert not exports(run(frame, timelines)), 'insufficient projected reserve allowed sale'
    frame, timelines = scenario(charge=False, floor=50.0)
    for policy in ('rce', 'tariff'):
        timelines[policy]['points'][0].update(load_kw=4.8, battery_kw=-4.0, soc_percent=40.0)
        timelines[policy]['points'][1]['soc_percent'] = 35.0 if policy == 'rce' else 40.0
    assert not exports(run(frame, timelines)), 'live SOC hid depletion before future sale'
    for changes in (
        {'discharge_direction_ready': False},
        {'full_block_execution_ready': False},
        {'topology_full_block_allowed': False},
        {'export_state': T.ExportState.CONFIRMED_ZERO_EXPORT},
    ):
        frame, timelines = scenario(context_changes=changes)
        assert not exports(run(frame, timelines)), f'bypassed physical gate: {changes}'
    print('RCE future chart SOC: PASS (recharge, no recharge, authority, physical gates)')


if __name__ == '__main__':
    main()
