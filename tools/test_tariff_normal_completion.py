"""A planned tariff finish is distinct from losing authority mid-cycle."""
import asyncio
from types import SimpleNamespace as NS
import test_supervisor_transport_lifecycle_contract as T


async def main():
    E = T.EXECUTOR
    block = E.EmsBlock(E.EmsMode.GRID_CHARGE, 25, 90, 45, 5, 0, 100)
    for planned in (False, True):
        _, _, _, sensor, _ = await T._added_environment()
        lease = T._arm_sensor_lease(sensor, transaction_id='tariff:finish', block=block)
        lease.last_accepted_monotonic = asyncio.get_running_loop().time() - lease.granted_seconds - 1
        tx = NS(transaction_id=lease.transaction_id, owner=E.ExecutionOwner.TARIFF)
        sensor._controller = NS(record=NS(
            state=E.ActiveState.STOPPING if planned else E.ActiveState.EXECUTING,
            reason=E.ExecutionReason.DEADLINE_REACHED if planned else E.ExecutionReason.IDLE,
            transaction=tx), _tariff_planned_boundary=lambda _: T.H.NOW)
        sensor._publish_composite_state = lambda **kw: None
        sensor._sync_control_lease()
        assert sensor._control_lease_client.handle is None
        assert (sensor._control_lease_error is None) == planned
        assert sensor._control_lease_gate['reason'] == ('planned_tariff_end' if planned else 'local_soft_deadline')
        if planned:
            continue
        terminal = NS(state=E.ActiveState.IDLE, transaction=None, last_transaction=NS(
            transaction_id=lease.transaction_id, owner=E.ExecutionOwner.NONE,
            rollback_status=E.RollbackStatus.PENDING))
        sensor._release_completed_control_lease(terminal)
        assert sensor._control_lease_error is not None
        terminal.last_transaction.rollback_status = E.RollbackStatus.CONFIRMED
        terminal.last_transaction.transaction_id = 'tariff:older'
        sensor._release_completed_control_lease(terminal)
        assert sensor._control_lease_error is not None
        terminal.last_transaction.transaction_id = lease.transaction_id
        sensor._release_completed_control_lease(terminal)
        assert sensor._control_lease_error is None
        assert sensor._control_lease_gate['reason'] == 'local_soft_deadline'  # retain evidence
    print('PASS: planned end, unexpected expiry, pending restore, identity and confirmed recovery')


if __name__ == '__main__':
    asyncio.run(main())
