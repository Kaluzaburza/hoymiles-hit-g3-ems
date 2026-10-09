"""Whole-percent new commands, with unchanged physical/restore precision."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
import runpy

TOOLS = Path(__file__).resolve().parent
CHECKS = 0


def check(condition, message):
    global CHECKS
    assert condition, message
    CHECKS += 1


def policy_targets():
    f = runpy.run_path(str(TOOLS / 'test_supervisor_runtime_contract.py'), run_name='integer_policy_fixtures')
    rt = f['runtime_module']
    cases = (
        (rt.build_rce_candidate, f['rce_source'], 'effective_discharge_power_percent', {}),
        (rt.build_tariff_candidate, f['tariff_source'], 'command_charge_power_percent', {}),
        (rt.build_rcm_candidate, f['rcm_source'], 'recommended_charge_limit_percent', {}),
        (rt.build_rcm_candidate, f['rcm_source'], 'recommended_export_limit_percent', {'action': rt.RcmAction.LIMIT_EXPORT}),
        (rt.build_rcm_candidate, f['rcm_source'], 'pre_discharge_power_percent', {'action': rt.RcmAction.GRID_DISCHARGE_PREPARATION}),
        (rt.build_rcm_candidate, f['rcm_source'], 'pre_discharge_target_soc_percent', {'action': rt.RcmAction.GRID_DISCHARGE_PREPARATION}),
    )
    for builder, factory, field, extra in cases:
        valid = builder(factory(**extra, **{field: 49.0}), now=f['NOW'])
        check(valid.start_eligible, field + ': whole target lost start authority')
        for value in (49.1, 49.9, 0.9, True, float('nan'), float('inf')):
            invalid = builder(factory(**extra, **{field: value}), now=f['NOW'])
            check(not invalid.start_eligible and not invalid.continuation_eligible,
                  field + ': fractional or invalid target granted authority')
    # Measured SOC and other physical inputs remain continuous, not rounded.
    source = f['rce_source'](current_soc_percent=70.9)
    check(rt.build_rce_candidate(source, now=f['NOW']).start_eligible,
          'fractional physical SOC was incorrectly treated as a command')


async def manual_targets():
    f = runpy.run_path(str(TOOLS / 'test_supervisor_transport_lifecycle_contract.py'), run_name='integer_manual_fixtures')
    _, _, _, sensor, _ = await f['_added_environment']()
    frame = sensor._latest_active_frame
    module = f['SENSOR']
    names = ('gcf_export_soft_limit_ratio_259', 'battery_max_charge_power_306',
             'self_used_soc_4301', 'force_charge_soc_4303', 'maximum_charge_power_4304',
             'force_discharge_soc_4305', 'maximum_discharge_power_4306')
    for name in names:
        check(module._manual_proxy_readback_lease(name, 49.0, frame) is not None,
              name + ': whole manual target rejected')
        for value in (49.1, 49.9, True, float('nan')):
            check(module._manual_proxy_readback_lease(name, value, frame) is None,
                  name + ': invalid manual target granted a write lease')
    # A new integer target must preserve unrelated, physically read register
    # values from the complete block. The codec is also used by old rollback.
    physical = replace(frame, execution=replace(frame.execution, maximum_discharge_power_percent=21.2000007629395))
    lease = module._manual_proxy_readback_lease('force_charge_soc_4303', 90.0, physical)
    check(lease is not None and lease.expected[6] == 21.2,
          'historical physical precision was changed')
    write = module._manual_proxy_atomic_write(lease)
    check(write.ems_block.maximum_discharge_power_percent_4306 == 21.2,
          'full-block preservation quantized an unrelated original field')
    # Existing packed rollback encodes SOC and tenths of the original power.
    restore = module._manual_proxy_readback_lease('ems_complete_block_charge_rollback_command', 90 * 1001 + 499, frame)
    check(restore is not None and abs(restore.expected[4] - 49.9) < 1e-9,
          'original charge rollback precision was lost')


def main():
    policy_targets()
    asyncio.run(manual_targets())
    print(f'Integer control targets: PASS ({CHECKS} checks; offline only)')


if __name__ == '__main__':
    main()
