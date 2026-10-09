"""Regression for queued FC16, one old FC03 cohort, and a late apply."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import test_supervisor_executor as fixture
from supervisor_executor import (
    ActiveState,
    ExecutionOwner,
    ExecutionReason,
    ReadbackVerdict,
    SupervisorExecutor,
)
from supervisor_executor_codec import record_from_dict, record_to_dict


NOW = fixture.NOW


def _begin(*, tariff: bool) -> tuple[
    SupervisorExecutor,
    fixture.SettingsSnapshot,
    fixture.CommandEnvelope,
]:
    base = fixture._snapshot(ems_generation=46837)
    intent = fixture._tariff_intent(base) if tariff else fixture._rce_intent(base)
    machine = SupervisorExecutor()
    machine.select_intent(
        intent,
        transaction_id="late-ack-field-regression",
        now=NOW,
    )
    machine.start(base, fixture._gates(), now=NOW)
    envelope = machine.prepare_command(
        base,
        fixture._gates(intent.policy),
        now=NOW,
    )
    assert envelope is not None
    machine.mark_command_sent(envelope, sent_at=NOW + timedelta(seconds=1))
    return machine, base, envelope


def _frame(
    base: fixture.SettingsSnapshot,
    generation: int,
    second: float,
    *,
    block: fixture.EmsBlock | None = None,
) -> fixture.SettingsSnapshot:
    return replace(
        base,
        ems_generation=generation,
        ems_observed_at=NOW + timedelta(seconds=second),
        ems_block=block or base.ems_block,
    )


def _roundtrip(machine: SupervisorExecutor) -> SupervisorExecutor:
    record = record_from_dict(record_to_dict(machine.record))
    assert record == machine.record
    return SupervisorExecutor.recover(record)


def _test_old_then_applied(*, tariff: bool) -> None:
    machine, base, envelope = _begin(tariff=tariff)
    old = _frame(base, 46838, 1.262)
    assert machine.observe_readback(
        old,
        now=NOW + timedelta(seconds=3),
    ) is ReadbackVerdict.PENDING
    assert machine.record.state is ActiveState.WAITING_READBACK
    assert machine.record.owner is envelope.owner
    transaction = machine.record.transaction
    assert transaction is not None
    sent_at = transaction.command_sent_at
    revision = machine.record.revision

    assert machine.observe_readback(
        old,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.PENDING
    assert machine.record.revision == revision
    transaction = machine.record.transaction
    assert transaction is not None
    assert transaction.command_sent_at == sent_at

    applied = _frame(
        base,
        46839,
        6.34,
        block=envelope.command.ems_block,
    )
    assert machine.observe_readback(
        applied,
        now=NOW + timedelta(seconds=6.4),
    ) is ReadbackVerdict.MATCH
    assert machine.record.owner is envelope.owner
    transaction = machine.record.transaction
    assert transaction is not None
    assert transaction.command_snapshot == base

    # After an actual ACK, a return to the old block is a contradiction.
    reverted = _frame(base, 46840, 7)
    assert machine.observe_readback(
        reverted,
        now=NOW + timedelta(seconds=7),
    ) is ReadbackVerdict.MISMATCH
    assert machine.record.state is ActiveState.FAULT
    assert machine.record.owner is envelope.owner


def _test_stop_before_ack(*, tariff: bool, late_apply: bool) -> None:
    machine, base, envelope = _begin(tariff=tariff)
    owner = envelope.owner
    machine.request_stop(reason=ExecutionReason.AUTHORIZATION_LOST)

    # The dispatch-generation cohort can never release the owner or authorize
    # a restore write.
    assert machine.prepare_restore(
        base,
        fixture._gates(owner),
        now=NOW + timedelta(seconds=2),
    ) is None
    assert machine.record.owner is owner

    old = _frame(base, 46838, 3)
    assert machine.prepare_restore(
        old,
        fixture._gates(owner),
        now=NOW + timedelta(seconds=3),
    ) is None
    assert machine.record.owner is owner
    transaction = machine.record.transaction
    assert transaction is not None
    assert transaction.restore_attempts == 0

    # The durable barrier survives restart without replaying charge/export.
    machine = _roundtrip(machine)
    assert machine.record.owner is owner
    assert machine.prepare_restore(
        old,
        fixture._gates(owner),
        now=NOW + timedelta(seconds=4),
    ) is None

    second = _frame(
        base,
        46839,
        5,
        block=envelope.command.ems_block if late_apply else None,
    )
    restore = machine.prepare_restore(
        second,
        fixture._gates(owner),
        now=NOW + timedelta(seconds=5),
    )
    assert restore is not None
    assert restore.command.ems_block == base.ems_block
    assert machine.record.owner is owner
    assert restore.atomic_writes[0].snapshot_generation == 46839
    transaction = machine.record.transaction
    assert transaction is not None
    assert transaction.restore_attempts == 1

    machine.mark_restore_sent(restore, sent_at=NOW + timedelta(seconds=6))
    assert machine.observe_restore_readback(
        second,
        now=NOW + timedelta(seconds=6.1),
    ) is ReadbackVerdict.PENDING
    assert machine.record.owner is owner

    neutral = _frame(base, 46840, 7)
    assert machine.observe_restore_readback(
        neutral,
        now=NOW + timedelta(seconds=7),
    ) is ReadbackVerdict.MATCH
    assert machine.record.owner is ExecutionOwner.NONE
    assert machine.record.state is ActiveState.IDLE


def _test_foreign_block_and_timeout(*, tariff: bool) -> None:
    machine, base, envelope = _begin(tariff=tariff)
    unexpected = _frame(
        base,
        46838,
        3,
        block=replace(base.ems_block, backup_soc_percent_4302=75),
    )
    assert machine.observe_readback(
        unexpected,
        now=NOW + timedelta(seconds=3),
    ) is ReadbackVerdict.MISMATCH
    assert machine.record.owner is envelope.owner
    assert machine.record.state is ActiveState.FAULT

    machine, base, envelope = _begin(tariff=tariff)
    for second in (3, 20, 40, 60, 80, 90.999):
        old = _frame(base, 46838 + int(second), second)
        assert machine.observe_readback(
            old,
            now=NOW + timedelta(seconds=second),
        ) is ReadbackVerdict.PENDING
        transaction = machine.record.transaction
        assert transaction is not None
        assert transaction.command_sent_at == NOW + timedelta(seconds=1)

    assert machine.observe_readback(
        _frame(base, 47000, 91),
        now=NOW + timedelta(seconds=91),
    ) is ReadbackVerdict.MISMATCH
    assert machine.record.reason is ExecutionReason.DEADLINE_REACHED
    assert machine.record.owner is envelope.owner


def main() -> None:
    for tariff in (False, True):
        _test_old_then_applied(tariff=tariff)
        _test_stop_before_ack(tariff=tariff, late_apply=False)
        _test_stop_before_ack(tariff=tariff, late_apply=True)
        _test_foreign_block_and_timeout(tariff=tariff)
    print(
        "PASS: 8 late-ACK/STOP regression groups "
        "(RCE and tariff), real trace ordering, codec/restart, "
        "no timeout renewal"
    )


if __name__ == "__main__":
    main()
