"""A late firmware fallback settles only its own fully observed transaction."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import asyncio
import runpy
import sys
from types import SimpleNamespace


fixture = runpy.run_path(
    str(Path(__file__).with_name("test_supervisor_executor_codec.py")),
    run_name="terminal_fixture",
)
controller_fixture = runpy.run_path(
    str(Path(__file__).with_name("test_supervisor_active_controller.py")),
    run_name="terminal_controller_fixture",
)
NOW = fixture["NOW"]
identity = ("session-A", "lease-A", 7)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def case():
    snapshot = fixture["_snapshot"]()
    changed = fixture["_snapshot"](
        at=NOW + timedelta(seconds=5),
        block=fixture["_command"]().ems_block,
        ems_generation=102,
        gcf_generation=203,
        battery_generation=304,
    )
    original = fixture["_rich_transaction"](
        fixture["ActiveState"].FAULT, fixture["ExecutionOwner"].TARIFF
    )
    transaction = replace(
        original,
        state=fixture["ActiveState"].FAULT,
        owner=fixture["ExecutionOwner"].TARIFF,
        reason=fixture["ExecutionReason"].COMMAND_NOT_QUEUED,
        readback_result=fixture["VerificationStatus"].CONFIRMED,
        rollback_status=fixture["RollbackStatus"].PENDING,
        rollback_result=fixture["VerificationStatus"].PENDING,
        restore_command=None,
        restore_snapshot=changed,
        restore_expected_readback=None,
        restore_sent_at=None,
        restore_attempts=0,
        restore_not_queued_count=2,
        restore_not_queued_first_at=NOW + timedelta(seconds=8),
        lease_identity=identity,
        interruption_reason=fixture["ExecutionReason"].AUTHORIZATION_LOST,
        master_stop_requested=False,
    )
    record = fixture["ExecutorRecord"](
        state=fixture["ActiveState"].FAULT,
        owner=fixture["ExecutionOwner"].TARIFF,
        transaction=transaction,
        reason=fixture["ExecutionReason"].COMMAND_NOT_QUEUED,
    )
    fresh = fixture["_snapshot"](
        at=NOW + timedelta(seconds=12),
        ems_generation=103,
        gcf_generation=204,
        battery_generation=305,
    )
    block = snapshot.ems_block
    words = (
        int(block.mode), int(block.self_use_soc_percent_4301),
        int(block.backup_soc_percent_4302),
        int(block.force_charge_soc_percent_4303),
        int(block.maximum_charge_power_percent_4304 * 10),
        int(block.force_discharge_soc_percent_4305),
        int(block.maximum_discharge_power_percent_4306 * 10),
    )
    proof = {
        "schema_version": 2,
        "protocol_version": 2,
        "lease_cleared": True,
        "lease_state": 0,
        "terminal_valid": True,
        "terminal_kind": 1,
        "session_id": identity[0],
        "lease_id": identity[1],
        "transaction_id": transaction.transaction_id,
        "command_generation": identity[2],
        "terminal_epoch": 4,
        "fc03_generation": 103,
        **{f"fallback_430{index}": word for index, word in enumerate(words)},
    }
    return record, fresh, proof


def settle(record, fresh, proof):
    executor = fixture["SupervisorExecutor"](record)
    result = executor.settle_late_terminal_fallback(
        fresh, proof, now=NOW + timedelta(seconds=13)
    )
    return result, executor.record


def main() -> None:
    record, fresh, proof = case()
    result, terminal = settle(record, fresh, proof)
    assert result
    assert terminal.state is fixture["ActiveState"].IDLE
    assert terminal.owner is fixture["ExecutionOwner"].NONE
    assert terminal.last_transaction.rollback_status is fixture["RollbackStatus"].CONFIRMED
    assert terminal.last_transaction.reason is fixture["ExecutionReason"].AUTHORIZATION_LOST
    assert terminal.last_transaction.restore_sent_at is None
    assert terminal.last_transaction.restore_attempts == 0
    assert terminal.last_transaction.terminal_epoch == 4
    assert fixture["record_from_dict"](fixture["record_to_dict"](terminal)) == terminal
    print("PASS exact terminal fallback, original cause, persistence")

    # The ESP may execute its durable fallback even when HA never received
    # the initial FC03 ACK. A correlated terminal proof must close recovery
    # without rewriting that missing/failed start into a confirmed execution.
    for status in (fixture["VerificationStatus"].PENDING,
                   fixture["VerificationStatus"].UNAVAILABLE,
                   fixture["VerificationStatus"].CONTRADICTED):
        awaiting_ack = replace(record, transaction=replace(
            record.transaction, readback_result=status,
        ))
        accepted, closed = settle(awaiting_ack, fresh, proof)
        assert accepted, ("terminal fallback stranded without initial ACK", status)
        assert closed.owner is fixture["ExecutionOwner"].NONE
        assert closed.last_transaction.readback_result is status
        assert closed.last_transaction.reason is fixture["ExecutionReason"].AUTHORIZATION_LOST
        assert closed.last_transaction.restore_sent_at is None
        assert fixture["record_from_dict"](fixture["record_to_dict"](closed)) == closed
    print("PASS missing or failed initial ACK retains failure and closes proven fallback")

    negatives = {
        "foreign_lease": ({**proof, "lease_id": "lease-B"}, fresh, record),
        "foreign_transaction": ({**proof, "transaction_id": "tx:foreign:0001"}, fresh, record),
        "foreign_session": ({**proof, "session_id": "session-B"}, fresh, record),
        "active_owner": ({**proof, "lease_cleared": False}, fresh, record),
        "retired_by_new_arm": ({**proof, "terminal_valid": False}, fresh, record),
        "release_only": ({**proof, "terminal_kind": 2}, fresh, record),
        "wrong_fallback": ({**proof, "fallback_4306": 1}, fresh, record),
        "older_proof": ({**proof, "fc03_generation": 104}, fresh, record),
        "replay": (proof, fresh, replace(record, transaction=replace(
            record.transaction, terminal_epoch=4
        ))),
        "old_ems_cohort": (proof, replace(fresh, ems_generation=102), record),
        "old_gcf_cohort": (proof, replace(fresh, gcf_generation=203), record),
        "old_battery_cohort": (proof, replace(fresh, battery_generation=304), record),
        "incomplete_gcf": (proof, replace(fresh, gcf_coherent=False), record),
        "off_grid": (proof, replace(fresh, ems_block=replace(
            fresh.ems_block, mode=fixture["EmsMode"].OFF_GRID
        )), record),
        "no_proven_unsent": (proof, fresh, replace(record, transaction=replace(
            record.transaction, restore_not_queued_count=0,
            restore_not_queued_first_at=None,
        ))),
        "pre_upgrade": (proof, fresh, replace(record, transaction=replace(
            record.transaction, lease_identity=None
        ))),
        "never_dispatched": (proof, fresh, replace(record, transaction=replace(
            record.transaction, command_sent_at=None
        ))),
    }
    for name, (candidate, cohort, candidate_record) in negatives.items():
        accepted, still_owned = settle(candidate_record, cohort, candidate)
        assert not accepted and still_owned.owner is not fixture["ExecutionOwner"].NONE, name
        print("PASS reject", name)
        # Terminal correlation and physical gates are equally mandatory when
        # initial confirmation was lost; missing ACK is never a bypass.
        unconfirmed = replace(candidate_record, transaction=replace(
            candidate_record.transaction,
            readback_result=fixture["VerificationStatus"].PENDING,
        ))
        accepted, still_owned = settle(unconfirmed, cohort, candidate)
        assert not accepted and still_owned.owner is not fixture["ExecutionOwner"].NONE, name

    firmware = (Path(__file__).resolve().parents[1] / "packages" / "settings.yaml").read_text(
        encoding="utf-8"
    )
    arm = firmware.index("std::array<uint8_t, 472> no_terminal{}")
    assert arm < firmware.index("const bool identity_durable = terminal_retired")
    assert "if (terminal_retired)\n                  id(ems_control_terminal_record) = no_terminal" in firmware
    assert "const bool epoch_only = allow_epoch_only && value[1] == 0U" in firmware
    assert "id(ems_control_terminal_record), true);" in firmware
    assert "no_terminal[0] = 2U;" in firmware
    assert "std::memcpy(no_terminal.data() + 432U," in firmware
    assert "id(ems_control_terminal_record).data() + 432U, 4U);" in firmware
    assert "const uint32_t retired_crc = crc32(no_terminal.data(), 468U);" in firmware
    print("PASS firmware retires proof while retaining durable terminal epoch")

    asyncio.run(controller_path())
    notification_path()


async def controller_path() -> None:
    record, _fresh, proof = case()
    record = replace(record, transaction=replace(
        record.transaction, readback_result=fixture["VerificationStatus"].PENDING,
    ))
    now = NOW + timedelta(seconds=13)
    source = controller_fixture["execution_source"](
        now,
        physical_mode_code=0,
        full_block_generation=103,
        full_block_generation_at=NOW + timedelta(seconds=12),
        self_use_soc_percent=20,
        backup_soc_percent=70,
        force_charge_soc_percent=90,
        maximum_charge_power_percent=50,
        force_discharge_soc_percent=20,
        maximum_discharge_power_percent=60,
        gcf_enable_code=1,
        effective_export_limit_percent=42.5,
        gcf_generation=204,
        gcf_generation_at=NOW + timedelta(seconds=12, microseconds=1),
        battery_charge_limit_percent=55.5,
        battery_charge_limit_generation=305,
        battery_charge_limit_generation_at=NOW + timedelta(seconds=12, microseconds=2),
    )
    frame = controller_fixture["frame"](
        now, source, mode=controller_fixture["SupervisorMode"].OFF,
        enabled=False,
    )
    dispatches = []
    saves = []

    async def persist(value):
        saves.append(value)

    async def dispatch(value):
        dispatches.append(value)
        raise AssertionError("late fallback issued a write")

    async def terminal():
        return proof

    controller = controller_fixture["SupervisorActiveController"](
        persist=persist, dispatch=dispatch, publish=lambda _value: None,
        persisted_record=record, clock=lambda: now,
        terminal_proof=terminal,
    )
    await controller.async_initialize()
    await controller.async_reconcile(frame)
    assert controller.record.state is fixture["ActiveState"].IDLE
    assert not dispatches
    assert controller.record.last_transaction.terminal_epoch == 4
    assert controller.record.last_transaction.readback_result is fixture["VerificationStatus"].PENDING
    assert saves
    print("PASS controller read-only terminal response, no restore dispatch")

    fail_store = False

    async def failed_persist(_value):
        if fail_store:
            raise RuntimeError("injected Store failure")

    failed = controller_fixture["SupervisorActiveController"](
        persist=failed_persist, dispatch=dispatch, publish=lambda _value: None,
        persisted_record=record, clock=lambda: now,
        terminal_proof=terminal,
    )
    await failed.async_initialize()
    prior = failed.record
    fail_store = True
    try:
        await failed.async_reconcile(frame)
    except RuntimeError as err:
        assert str(err) == "injected Store failure"
    else:
        raise AssertionError("Store failure was hidden")
    assert failed.record == prior, (failed.record, prior)
    assert failed.record.owner is fixture["ExecutionOwner"].TARIFF
    assert failed.record.transaction.restore_not_queued_count == 2
    assert not dispatches
    print("PASS failed terminal Store keeps durable and in-memory owner")


def notification_path() -> None:
    from custom_components.hoymiles_hit_modbus import supervisor_executor as product
    from custom_components.hoymiles_hit_modbus.ems_notifications import (
        HoymilesEmsNotificationManager,
        RangeNotificationModel,
    )

    record, fresh, proof = case()
    tx = record.transaction
    snapshot = tx.command_snapshot
    command = fixture["CommandSet"](ems_block=replace(
        snapshot.ems_block,
        mode=fixture["EmsMode"].GRID_DISCHARGE,
        force_discharge_soc_percent_4305=30.0,
        maximum_discharge_power_percent_4306=50.0,
    ))
    intent = fixture["ActuatorIntent"](
        policy=fixture["ExecutionOwner"].RCE,
        action=fixture["ExecutionAction"].RCE_EXPORT,
        command=command,
        candidate_revision="rce:terminal:0001",
        deadline=tx.deadline,
    )
    physical = fixture["PhysicalVerification"](
        transaction_id=tx.transaction_id,
        action=intent.action,
        status=fixture["VerificationStatus"].CONFIRMED,
        observed_at=NOW + timedelta(seconds=5),
        evidence=("fresh export", "fresh discharge"),
    )
    tx = replace(
        tx, intent=intent, owner=fixture["ExecutionOwner"].RCE,
        expected_readback=fixture["ExpectedReadback"].from_command(snapshot, command),
        physical_verification=physical,
        restore_snapshot=replace(tx.restore_snapshot, ems_block=command.ems_block),
    )
    record = replace(record, owner=fixture["ExecutionOwner"].RCE, transaction=tx)
    accepted, finished = settle(record, fresh, proof)
    assert accepted
    assert finished.last_transaction.reason is fixture["ExecutionReason"].AUTHORIZATION_LOST

    manager = object.__new__(HoymilesEmsNotificationManager)
    manager.hass = SimpleNamespace(states=SimpleNamespace(is_state=lambda *_args: False))

    def frame(at, planned_end):
        return SimpleNamespace(
            now=at,
            rce=SimpleNamespace(current_run_end=planned_end),
            tariff=SimpleNamespace(current_grid_charge_run_end=None),
            rcm=SimpleNamespace(
                latched_pre_discharge_deadline=None,
                pre_discharge_deadline=None,
            ),
        )

    projected_tx = replace(
        tx, owner=product.ExecutionOwner.RCE,
        readback_result=product.VerificationStatus.CONFIRMED,
        physical_verification=replace(
            physical, status=product.VerificationStatus.CONFIRMED
        ),
    )
    running_tx = replace(
        projected_tx, state=product.ActiveState.EXECUTING,
        reason=product.ExecutionReason.PHYSICALLY_CONFIRMED,
        rollback_status=product.RollbackStatus.NOT_REQUIRED,
    )
    running = replace(
        record, state=product.ActiveState.EXECUTING,
        transaction=running_tx,
        reason=product.ExecutionReason.PHYSICALLY_CONFIRMED,
    )
    projected_terminal = replace(
        finished, state=product.ActiveState.IDLE,
        last_transaction=replace(
            projected_tx,
            owner=product.ExecutionOwner.NONE,
            reason=product.ExecutionReason.AUTHORIZATION_LOST,
            rollback_status=product.RollbackStatus.CONFIRMED,
            rollback_result=product.VerificationStatus.CONFIRMED,
        ),
    )
    planned_end = NOW + timedelta(minutes=10)
    model = RangeNotificationModel()
    manager._model = model
    model.observe(manager._observation(fixture["ExecutorRecord"](), frame(NOW, planned_end)))
    running_observation = manager._observation(
        running, frame(NOW + timedelta(seconds=6), planned_end)
    )
    starts = model.observe(running_observation)
    assert len(starts) == 1 and starts[0].kind == "start", (
        starts, running_observation, model.active
    )
    terminal = manager._observation(
        projected_terminal, frame(NOW + timedelta(seconds=13), planned_end)
    )
    assert terminal.restoration_confirmed and terminal.interrupted
    assert terminal.terminal_reason == "authorization_lost"
    event = model.observe(terminal)
    assert len(event) == 1 and event[0].outcome == "interrupted"
    assert event[0].transaction_ids == (tx.transaction_id,)
    assert model.observe(terminal) == ()

    natural = RangeNotificationModel()
    manager._model = natural
    natural.observe(manager._observation(
        fixture["ExecutorRecord"](), frame(NOW, NOW + timedelta(seconds=13))
    ))
    natural.observe(manager._observation(
        running, frame(NOW + timedelta(seconds=6), NOW + timedelta(seconds=13))
    ))
    planned_record = replace(
        projected_terminal,
        last_transaction=replace(
            projected_terminal.last_transaction,
            reason=product.ExecutionReason.DEADLINE_REACHED,
        ),
    )
    planned_terminal = manager._observation(
        planned_record, frame(NOW + timedelta(seconds=13), NOW + timedelta(seconds=13))
    )
    assert not planned_terminal.interrupted
    natural.observe(planned_terminal)
    completed = natural.observe(replace(
        planned_terminal, observed_at=NOW + timedelta(minutes=3)
    ))
    assert len(completed) == 1 and completed[0].outcome == "completed"
    print("PASS RCE N08/N10 early versus natural terminal projection")


if __name__ == "__main__":
    main()
