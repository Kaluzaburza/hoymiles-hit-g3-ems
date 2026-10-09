"""RCE minimum is planned net export, never a physical STOP threshold."""
from dataclasses import fields, replace
from datetime import timedelta, timezone
from test_rce_optimizer import RCE, base_input, NOW


def settings(**changes):
    value = base_input(**changes)
    # The baseline has no configurable minimum: still exercise its behaviour
    # for a meaningful RED rather than failing at constructor/import time.
    if 'minimum_net_export_power_kw' in {f.name for f in fields(value)}:
        value = replace(value, minimum_net_export_power_kw=2.0)
    return value


def main():
    cfg = settings()
    for new_run in (False, True):
        for net, expected in ((.1, False), (1.99, False), (2, True), (2.1, True)):
            assert RCE._rce_export_is_executable(cfg, net * .5, .5, 0, 0,
                                                new_run=new_run) is expected, (net, new_run)
        # 2 kW BAT - 1.5 kW LOAD leaves only .5 kW net; 4306 quantum matters.
        assert not RCE._rce_export_is_executable(cfg, .25, .5, .75, 0, new_run=new_run)
        assert RCE._rce_export_is_executable(cfg, 1, .5, .75, 0, new_run=new_run)
        assert not RCE._rce_export_is_executable(cfg, 1, .5, .755, 0, new_run=new_run)
        assert RCE._rce_export_is_executable(cfg, 1, .5, .75, .75, new_run=new_run)
    a = NOW.astimezone(timezone.utc)
    b = a + timedelta(minutes=30)
    args = dict(settings=cfg, starts=[a, b], load_by_slot={}, pv_by_slot={},
                slot_fractions={a: 1, b: 1}, price_by_start={a: 2, b: 1},
                objective=lambda p: p.get(a, 0)*2+p.get(b, 0))
    assert RCE._pack_executable_exports({a: 1.5, b: .26},
        feasible=lambda p: sum(p.values()) <= 1.76, **args) == {a: 1.76}
    assert RCE._pack_executable_exports({a: 1.5, b: .26},
        feasible=lambda p: p.get(a, 0) <= 1.5, **args) == {a: 1.5}
    assert RCE._pack_executable_exports({b: .26}, feasible=lambda p: True, **args) == {}
    assert RCE._pack_executable_exports({a: 1.5, b: .26},
        feasible=lambda p: True, allow_transfer=False, **args) == {a: 1.5}
    # Optimizer caps and energy budgets must never be increased to hit minimum.
    for cap in (1.99, 2.0, 3.0):
        plan_cfg = settings(price_slots=[RCE.PriceSlot(NOW, 2.0)], export_power_cap_kw=cap,
            current_load_power_kw=0, current_pv_power_kw=0)
        result = RCE.optimize_rce(plan_cfg)
        for item in result.planned_exports:
            assert item.energy_kwh / .5 >= 2.0 - 1e-9
            assert item.energy_kwh / .5 <= cap + 1e-9
        if cap < 2: assert not result.planned_exports
    assert RCE.post_command_settling_market_fingerprint(plan_cfg) != (
        RCE.post_command_settling_market_fingerprint(replace(plan_cfg, minimum_net_export_power_kw=3)))
    print('PASS: minimum net power, LOAD/PV/quantization, caps and bounded tail packing')


if __name__ == '__main__':
    main()
