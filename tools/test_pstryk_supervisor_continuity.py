"""Pstryk publication with the real Supervisor proof/controller/lease harness.

Qualified forecast inputs and a proposed moved selection are deterministic.
No network/device writes; the scheduler, physical proof, joint revalidation,
HA templates, publication, controller and firmware lease model run normally.
"""
import asyncio
from dataclasses import replace
from datetime import timedelta
import sys
from types import SimpleNamespace

from test_rce_publication_continuity import Continuity, h


async def main():
    worker_seconds = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    duration = int(sys.argv[2]) if len(sys.argv) > 2 else 270
    probe = Continuity()
    await probe.setup()
    sys.modules['homeassistant.core'].Context = lambda: SimpleNamespace(id='test')
    M = h._load('custom_components.hoymiles_hit_modbus.pstryk_runtime',
                h.COMPONENT / 'pstryk_runtime.py')
    from custom_components.hoymiles_hit_modbus import pstryk_plan as P
    from custom_components.hoymiles_hit_modbus import pstryk_joint as J
    M.Store = lambda *args: None
    tariff = SimpleNamespace(entity_id=probe.eid('tariff_plan'), _attributes={},
        _result=None, _timeline_sensor=None, _input_revision=SimpleNamespace(value=0))
    tariff.async_write_ha_state = lambda: probe.hass.fire_state(tariff.entity_id,
        h.FakeState('ready', dict(tariff._attributes), h.CLOCK['now']))
    probe.runtime.source_device.id = 'fixture-device'
    probe.source.entity_id = probe.eid('rce_plan')
    coordinator = M.PstrykRuntime(probe.hass, probe.entry, probe.runtime, probe.source, tariff)
    coordinator.profile = coordinator.profile.select_purchase('PGE', tariff='G12w').select_sale('Pstryk')
    coordinator.initialized = coordinator.storage_ready = True
    for entity in (M.SALE, M.PURCHASE):
        probe.direct(entity, 'Pstryk')
    options = dict(charge_efficiency=95., charge_power_percent=60., maximum_soc=100.,
        minimum_saving=.01, demand_margin_percent=0., allow_buy=True, allow_sell=True)
    coordinator._options = lambda: options
    coordinator.cache = SimpleNamespace(view=lambda **kw: SimpleNamespace(snapshot=SimpleNamespace(
        revision='test-public-net', at=lambda now: True)))
    async def saved():
        pass  # Store persistence has an independent real-HA regression.
    coordinator._save = saved
    original_provider = probe.source._optimizer_input
    def provider():
        settings, metadata = original_provider()
        return replace(settings, conservative_pv_by_slot_kwh={}, current_battery_soc_fresh=True), {
            **metadata, 'bms_discharge_data_fresh': True,
            'bms_discharge_data_available': True, 'bms_discharge_data_age_seconds': 0.,
            'pv_charge_delay_planner_settling_active': False,
        }
    before, metadata = provider()
    data = P.build_joint_input(before, options)

    def selection(current, exports):
        home = J.optimize(replace(current, allow_sell=False))
        actions = tuple(-2 if i in exports else a for i, a in enumerate(home.action_levels))
        points, cost = J.simulate(current, actions,
            exports={current.slots[i].start: value for i, value in exports.items()})
        return replace(home, slots=points, cost_pln=cost, action_levels=actions,
            benefit_pln=max(home.baseline_cost_pln-cost, 0.), sale_reason='sale_planned')

    accepted = selection(data, {i: 4.5 for i in range(4)})
    coordinator._accepted = (data, accepted, 10.)
    coordinator._accepted_settings = before
    coordinator._accepted_source = (coordinator.profile.revision, 'test-public-net')
    coordinator._settling_options = options
    probe.source.attach_supervisor_commitment_source(probe.supervisor)
    probe.source._pstryk = coordinator

    def inputs():
        settings, metadata = provider()
        fresh = P.build_joint_input(settings, options)
        return fresh, metadata, settings, P.input_key(fresh,
            profile_revision=coordinator.profile.revision, price_revision='test-public-net')
    coordinator._input = inputs
    original_solver = M.optimize
    M.optimize = lambda current: selection(current, {len(current.slots)-1: 1.})
    proofs = []
    reader = probe.source._active_rce_commitment
    def commitment(now):
        proof = reader(now)
        proofs.append((probe.second, proof is not None))
        return proof
    probe.source._active_rce_commitment = commitment
    async def executor(function, *args):
        result = function(*args)
        future = probe.loop.create_future()
        probe.jobs.append((probe.second+worker_seconds, future, result, {}))
        return await future
    probe.hass.async_add_executor_job = executor
    try:
        for second in range(1, duration+1):
            if second > 270 and second % 120 == 0:
                probe.spawn(probe.source._async_timer(h.CLOCK['now']))
            await probe.tick(second)
        assert any(value for _, value in proofs), proofs
        assert probe.source._attributes['active_slot_commitment_applied'], proofs
        assert coordinator._active_run_basis[4][1] is accepted
        assert probe.controller.record.transaction.transaction_id == probe.original_tx
        assert not probe.services.restore_calls
        assert sum(value for _, value in proofs) >= 2, proofs
        assert all(accepted for _, _, accepted in probe.services.renewals)
        print('PASS Pstryk real Supervisor commitment/publication/lease:', duration,
              's, same transaction/deadline, accepted renewals', len(probe.services.renewals),
              'worker', worker_seconds)
        print('Proof samples', proofs)
        if duration == 270:
            print('Fresh BMS veto', await probe.negative('bms'))
    except AssertionError:
        record = probe.controller.record
        print('FINAL', record.state, record.reason, 'proof samples', proofs)
        print('STOPS', probe.controller._stop_decisions)
        raise
    finally:
        M.optimize = original_solver
        probe.loop.time = probe.real_loop_time
        for task in probe.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*probe.tasks, return_exceptions=True)


if __name__ == '__main__':
    asyncio.run(main())
