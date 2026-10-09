"""Executable tails: quantization, packing and no relaxed physical authority."""
from datetime import timedelta, timezone
from test_rce_optimizer import RCE, base_input, NOW


def test_contract():
    settings = base_input()
    # Field sequence 822/824/825/827: a new run needs a complete 4306
    # quantum of headroom above 200 W; an existing run keeps the 200 W gate.
    for power in (.062, .211, .116, .294):
        assert not RCE._rce_export_is_executable(settings, power * .5, .5, 0, 0, new_run=True)
    assert RCE._rce_export_is_executable(settings, .2, .5, 0, 0, new_run=True)
    assert RCE._rce_export_is_executable(settings, .15, .5, 0, 0, new_run=False)
    assert not RCE._rce_export_is_executable(settings, .1, .5, .099, 0, new_run=False)
    for minutes in (5, 20, 30):
        for energy in (.03, .04, .09, .2):
            hours = minutes / 60
            net = int((energy / hours + .484) / .1 + 1e-9) * .1 - .484
            assert RCE._rce_export_is_executable(settings, energy, hours, .484 * hours, 0, new_run=True) == (net >= .3 - 1e-9)


def test_pack():
    settings = base_input()
    a = NOW.astimezone(timezone.utc)
    b = a + timedelta(minutes=30)
    args = dict(settings=settings, starts=[a, b], load_by_slot={},
                pv_by_slot={}, slot_fractions={a: 1, b: 1},
                objective=lambda plan: plan.get(a, 0) * 1.1 + plan.get(b, 0),
                price_by_start={a: 1.1, b: 1.0})
    packed = RCE._pack_executable_exports({a: 1.0, b: .04},
        feasible=lambda plan: sum(plan.values()) <= 1.04 + 1e-9, **args)
    assert packed == {a: 1.04}
    # A saturated earlier block must not exceed its BMS/energy cap.
    limited = RCE._pack_executable_exports({a: 1.0, b: .04},
        feasible=lambda plan: plan.get(a, 0) <= 1.0, **args)
    assert limited == {a: 1.0}
    assert RCE._pack_executable_exports({b: .04}, feasible=lambda plan: True, **args) == {}
    assert RCE._pack_executable_exports({a: 1.0, b: .04},
        feasible=lambda plan: True, allow_transfer=False, **args) == {a: 1.0}
    equal = {**args, 'price_by_start': {a: 1, b: 1}, 'objective': lambda plan: sum(plan.values())}
    packed = RCE._pack_executable_exports({a: 1.0, b: .04},
        feasible=lambda plan: plan.get(a, 0) <= 1.0, **equal)
    assert abs(sum(packed.values()) - 1.04) < 1e-9
    assert packed[b] >= .15 - 1e-9 and packed[a] < 1.0
    # Even a same-price move is refused if natural PV/terminal value makes
    # the whole-horizon objective worse.
    adverse = {**args, 'objective': lambda plan: 1 if plan.get(b) == .04 else 0}
    assert RCE._pack_executable_exports({a: 1.0, b: .04},
        feasible=lambda plan: True, **adverse) == {a: 1.0}


if __name__ == '__main__':
    test_contract()
    test_pack()
    print('PASS executable RCE tails and bounded packing')
