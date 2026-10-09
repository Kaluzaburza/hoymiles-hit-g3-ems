"""Persist delayed retarget phases for RCE and tariff, including stale ACK."""

from dataclasses import replace
from datetime import timedelta

import test_supervisor_executor_codec as fixture
from supervisor_executor import (
    ActiveState,
    EmsMode,
    ExecutionAction,
    ExecutionOwner,
    ExpectedReadback,
    PhysicalVerification,
    ReadbackVerdict,
    SupervisorExecutor,
    VerificationStatus,
)
from supervisor_executor_codec import record_from_dict, record_to_dict


def _persist(machine: SupervisorExecutor) -> None:
    assert record_from_dict(record_to_dict(machine.record)) == machine.record


def _machine(*, tariff: bool) -> SupervisorExecutor:
    record = fixture._record_for_state(ActiveState.RETARGETING)
    if not tariff:
        return SupervisorExecutor(record)
    transaction = record.transaction
    assert transaction is not None
    assert transaction.prewrite_snapshot is not None
    intent = replace(
        transaction.intent,
        policy=ExecutionOwner.TARIFF,
        action=ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        command=replace(
            transaction.intent.command,
            ems_block=replace(
                transaction.intent.command.ems_block,
                mode=EmsMode.GRID_CHARGE,
            ),
        ),
    )
    prewrite = replace(
        transaction.prewrite_snapshot,
        ems_block=replace(
            transaction.prewrite_snapshot.ems_block,
            mode=EmsMode.GRID_CHARGE,
        ),
    )
    transaction = replace(
        transaction,
        owner=ExecutionOwner.TARIFF,
        intent=intent,
        prewrite_snapshot=prewrite,
        expected_readback=ExpectedReadback.from_command(prewrite, intent.command),
    )
    return SupervisorExecutor(
        replace(record, owner=ExecutionOwner.TARIFF, transaction=transaction)
    )


def _scenario(*, tariff: bool = False, timeout: bool = False) -> None:
    machine = _machine(tariff=tariff)
    original = machine.record.transaction
    assert original is not None
    expected_owner = ExecutionOwner.TARIFF if tariff else ExecutionOwner.RCE
    expected_action = (
        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE
        if tariff
        else ExecutionAction.RCE_EXPORT
    )
    assert original.owner is expected_owner
    assert original.intent.action is expected_action
    assert original.prewrite_snapshot is not None

    now = fixture.NOW
    snapshot = replace(
        original.prewrite_snapshot,
        ems_block=original.intent.command.ems_block,
        ems_generation=original.prewrite_snapshot.ems_generation + 1,
        ems_observed_at=now + timedelta(seconds=3),
    )
    assert (
        machine.observe_readback(snapshot, now=now + timedelta(seconds=3))
        is ReadbackVerdict.MATCH
    )
    _persist(machine)

    pending = PhysicalVerification(
        original.transaction_id,
        original.intent.action,
        VerificationStatus.PENDING,
        now + timedelta(seconds=4),
        ("physical_cohort_incomplete",),
    )
    machine.observe_physical_verification(
        pending,
        now=now + timedelta(seconds=4),
    )
    _persist(machine)
    accepted = machine.record

    assert (
        machine.observe_readback(snapshot, now=now + timedelta(seconds=5))
        is ReadbackVerdict.MATCH
    )
    assert machine.record == accepted
    _persist(machine)

    # A stale repeat cannot authorize proof, erase the real ACK, or renew time.
    assert (
        machine.observe_readback(snapshot, now=now + timedelta(seconds=25))
        is ReadbackVerdict.PENDING
    )
    assert machine.record == accepted
    _persist(machine)

    if timeout:
        fresh = replace(
            snapshot,
            ems_generation=snapshot.ems_generation + 1,
            ems_observed_at=now + timedelta(seconds=182),
        )
        assert (
            machine.observe_readback(fresh, now=now + timedelta(seconds=182))
            is ReadbackVerdict.MISMATCH
        )
        assert machine.record.state is ActiveState.FAULT
        assert machine.record.owner is original.owner
        _persist(machine)
        return

    fresh = replace(
        snapshot,
        ems_generation=snapshot.ems_generation + 1,
        ems_observed_at=now + timedelta(seconds=26),
    )
    assert (
        machine.observe_readback(fresh, now=now + timedelta(seconds=26))
        is ReadbackVerdict.MATCH
    )
    assert machine.record == accepted
    _persist(machine)

    machine.observe_physical_verification(
        replace(
            pending,
            status=VerificationStatus.CONFIRMED,
            observed_at=now + timedelta(seconds=27),
        ),
        now=now + timedelta(seconds=27),
    )
    assert machine.record.state is ActiveState.EXECUTING
    assert machine.record.transaction is not None
    assert machine.record.transaction.command_snapshot == original.command_snapshot
    assert machine.record.transaction.deadline == original.deadline
    _persist(machine)


if __name__ == "__main__":
    for tariff in (False, True):
        _scenario(tariff=tariff)
        _scenario(tariff=tariff, timeout=True)
    print(
        "Retarget physical phase: RCE/tariff repeated ACK, stale ACK, persistence "
        "and fixed timeout PASS (offline only)"
    )
