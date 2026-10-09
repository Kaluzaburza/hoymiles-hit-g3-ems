"""Focused offline contract tests for the pure Supervisor Active executor."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from supervisor_executor import (  # noqa: E402
    ActiveState,
    ActuatorIntent,
    AtomicWriteFamily,
    CommandEnvelope,
    CommandSet,
    EmsBlock,
    EmsMode,
    ExecutionAction,
    ExecutionGates,
    ExecutionOwner,
    ExecutionReason,
    MasterStopStatus,
    PhysicalVerification,
    RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
    ReadbackVerdict,
    RollbackStatus,
    SettingsSnapshot,
    SupervisorExecutor,
    VerificationStatus,
)


NOW = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)


def _raises(exception: type[BaseException], action: Callable[[], object]) -> None:
    try:
        action()
    except exception:
        return
    raise AssertionError(f"expected {exception.__name__}")


def _block(mode: EmsMode = EmsMode.SELF_USE) -> EmsBlock:
    return EmsBlock(
        mode=mode,
        self_use_soc_percent_4301=20.0,
        backup_soc_percent_4302=70.0,
        force_charge_soc_percent_4303=90.0,
        maximum_charge_power_percent_4304=50.0,
        force_discharge_soc_percent_4305=20.0,
        maximum_discharge_power_percent_4306=60.0,
    )


def _snapshot(
    *,
    at: datetime = NOW,
    ems_at: datetime | None = None,
    gcf_at: datetime | None = None,
    battery_at: datetime | None = None,
    ems_block: EmsBlock | None = None,
    gcf_enabled: bool = True,
    export_limit: float = 50.0,
    battery_limit: float = 50.0,
    ems_generation: int = 10,
    gcf_generation: int = 20,
    battery_generation: int = 30,
) -> SettingsSnapshot:
    return SettingsSnapshot(
        ems_block=ems_block or _block(),
        ems_generation=ems_generation,
        ems_observed_at=ems_at or at,
        ems_coherent=True,
        gcf_enabled_258=gcf_enabled,
        export_limit_percent_259=export_limit,
        gcf_generation=gcf_generation,
        gcf_observed_at=gcf_at or at,
        gcf_coherent=True,
        battery_max_charge_power_percent_306=battery_limit,
        battery_generation=battery_generation,
        battery_observed_at=battery_at or at,
        battery_coherent=True,
    )


def _gates(
    owner: ExecutionOwner | str | None = None,
    *,
    full_block_ready: bool = True,
    direct_register_ready: bool = True,
    direct_306_ready: bool | None = None,
    direct_259_ready: bool | None = None,
) -> ExecutionGates:
    return ExecutionGates(
        inputs_fresh=True,
        bms_fresh=True,
        full_block_ready=full_block_ready,
        direct_306_ready=(
            direct_register_ready if direct_306_ready is None else direct_306_ready
        ),
        direct_259_ready=(
            direct_register_ready if direct_259_ready is None else direct_259_ready
        ),
        full_block_topology_ready=True,
        direct_register_topology_ready=True,
        charge_direction_ready=True,
        discharge_direction_ready=True,
        observed_owner=owner,
    )


def _rce_intent(base: SettingsSnapshot) -> ActuatorIntent:
    command_block = replace(
        base.ems_block,
        mode=EmsMode.GRID_DISCHARGE,
        force_discharge_soc_percent_4305=30.0,
        maximum_discharge_power_percent_4306=40.0,
    )
    return ActuatorIntent(
        policy=ExecutionOwner.RCE,
        action=ExecutionAction.RCE_EXPORT,
        command=CommandSet(ems_block=command_block),
        candidate_revision="rce:revision:0001",
        deadline=NOW + timedelta(seconds=120),
    )


def _tariff_intent(base: SettingsSnapshot) -> ActuatorIntent:
    return ActuatorIntent(
        policy=ExecutionOwner.TARIFF,
        action=ExecutionAction.TARIFF_BATTERY_CHARGE,
        command=CommandSet(
            ems_block=replace(
                base.ems_block,
                mode=EmsMode.GRID_CHARGE,
                force_charge_soc_percent_4303=90.0,
                maximum_charge_power_percent_4304=50.0,
            )
        ),
        candidate_revision="tariff:revision:local1",
        deadline=NOW + timedelta(seconds=120),
    )


def _direct_306_intent() -> ActuatorIntent:
    return ActuatorIntent(
        policy=ExecutionOwner.RCM,
        action=ExecutionAction.RCM_ABSORB_PV,
        command=CommandSet(battery_max_charge_power_percent_306=80.0),
        candidate_revision="rcm:revision:local306",
        deadline=NOW + timedelta(seconds=120),
    )


def _direct_259_intent() -> ActuatorIntent:
    return ActuatorIntent(
        policy=ExecutionOwner.RCM,
        action=ExecutionAction.RCM_LIMIT_EXPORT,
        command=CommandSet(export_limit_percent_259=20.0),
        candidate_revision="rcm:revision:local259",
        deadline=NOW + timedelta(seconds=120),
    )


def _apply_command_readback(
    snapshot: SettingsSnapshot,
    command: CommandSet,
    *,
    observed_at: datetime,
) -> SettingsSnapshot:
    values: dict[str, object] = {}
    if command.ems_block is not None:
        values.update(
            ems_block=command.ems_block,
            ems_generation=snapshot.ems_generation + 1,
            ems_observed_at=observed_at,
        )
    if command.export_limit_percent_259 is not None:
        values.update(
            export_limit_percent_259=command.export_limit_percent_259,
            gcf_generation=snapshot.gcf_generation + 1,
            gcf_observed_at=observed_at,
        )
    if command.battery_max_charge_power_percent_306 is not None:
        values.update(
            battery_max_charge_power_percent_306=(
                command.battery_max_charge_power_percent_306
            ),
            battery_generation=snapshot.battery_generation + 1,
            battery_observed_at=observed_at,
        )
    return replace(snapshot, **values)


def _begin_rce_until_waiting() -> tuple[
    SupervisorExecutor,
    SettingsSnapshot,
    CommandEnvelope,
]:
    base = _snapshot()
    machine = SupervisorExecutor()
    intent = _rce_intent(base)
    machine.select_intent(intent, transaction_id="tx:rce:0001", now=NOW)
    assert machine.record.state is ActiveState.SELECTED
    assert machine.record.owner is ExecutionOwner.NONE
    machine.start(base, _gates(), now=NOW + timedelta(seconds=1))
    assert machine.record.state is ActiveState.STARTING
    assert machine.record.owner is ExecutionOwner.RCE
    envelope = machine.prepare_command(
        base,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=2),
    )
    assert envelope is not None
    assert machine.record.owner is ExecutionOwner.RCE
    assert tuple(write.family for write in envelope.atomic_writes) == (
        AtomicWriteFamily.EMS_COMPLETE_BLOCK,
    )
    assert envelope.atomic_writes[0].snapshot_generation == base.ems_generation
    machine.mark_command_sent(envelope, sent_at=NOW + timedelta(seconds=3))
    assert machine.record.state is ActiveState.WAITING_READBACK
    return machine, base, envelope


def _begin_rce_until_executing() -> tuple[
    SupervisorExecutor,
    SettingsSnapshot,
    SettingsSnapshot,
]:
    machine, base, envelope = _begin_rce_until_waiting()
    readback = _apply_command_readback(
        base,
        envelope.command,
        observed_at=NOW + timedelta(seconds=4),
    )
    assert machine.observe_readback(
        readback,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.MATCH
    machine.observe_physical_verification(
        PhysicalVerification(
            transaction_id="tx:rce:0001",
            action=ExecutionAction.RCE_EXPORT,
            status=VerificationStatus.CONFIRMED,
            observed_at=NOW + timedelta(seconds=5),
            evidence=("fresh export", "fresh discharge"),
        ),
        now=NOW + timedelta(seconds=5),
    )
    assert machine.record.state is ActiveState.EXECUTING
    return machine, base, readback


def _rce_retarget_intent(
    current: SettingsSnapshot,
    *,
    deadline: datetime,
    floor: float = 25.0,
    power: float = 50.0,
) -> ActuatorIntent:
    return ActuatorIntent(
        policy=ExecutionOwner.RCE,
        action=ExecutionAction.RCE_EXPORT,
        command=CommandSet(
            ems_block=replace(
                current.ems_block,
                mode=EmsMode.GRID_DISCHARGE,
                force_discharge_soc_percent_4305=floor,
                maximum_discharge_power_percent_4306=power,
            )
        ),
        candidate_revision=f"rce:retarget:{int(floor)}:{int(power * 10)}",
        deadline=deadline,
    )


def _test_complete_block_transaction_and_rollback() -> None:
    machine, base, envelope = _begin_rce_until_waiting()
    readback = replace(
        base,
        ems_block=envelope.command.ems_block,
        ems_generation=base.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=4),
    )
    assert machine.observe_readback(
        readback,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.MATCH
    assert machine.record.state is ActiveState.WAITING_READBACK
    assert machine.record.reason is ExecutionReason.WAITING_PHYSICAL
    machine.observe_physical_verification(
        PhysicalVerification(
            transaction_id="tx:rce:0001",
            action=ExecutionAction.RCE_EXPORT,
            status=VerificationStatus.CONFIRMED,
            observed_at=NOW + timedelta(seconds=5),
            evidence=("fresh grid-flow cohort", "discharge direction confirmed"),
        ),
        now=NOW + timedelta(seconds=5),
    )
    assert machine.record.state is ActiveState.EXECUTING
    machine.check_continuation(
        readback,
        _gates(ExecutionOwner.RCE),
        PhysicalVerification(
            transaction_id="tx:rce:0001",
            action=ExecutionAction.RCE_EXPORT,
            status=VerificationStatus.CONFIRMED,
            observed_at=NOW + timedelta(seconds=6),
            evidence=("continuation flow remains coherent",),
        ),
        authorization_current=True,
        now=NOW + timedelta(seconds=6),
    )
    assert machine.record.state is ActiveState.EXECUTING
    assert machine.record.reason is ExecutionReason.CONTINUATION_CONFIRMED
    machine.request_stop()
    assert machine.record.state is ActiveState.STOPPING
    assert machine.record.owner is ExecutionOwner.RCE
    restore_source = replace(
        readback,
        gcf_observed_at=NOW - timedelta(seconds=60),
    )
    restore = machine.prepare_restore(
        restore_source,
        _gates(ExecutionOwner.RCE, direct_259_ready=False),
        now=NOW + timedelta(seconds=7),
    )
    assert restore is not None
    assert restore.command.ems_block == base.ems_block
    machine.mark_restore_sent(restore, sent_at=NOW + timedelta(seconds=8))
    restored = replace(
        restore_source,
        ems_block=base.ems_block,
        ems_generation=restore_source.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=9),
    )
    assert machine.observe_restore_readback(
        restored,
        now=NOW + timedelta(seconds=9),
    ) is ReadbackVerdict.MATCH
    assert machine.record.state is ActiveState.IDLE
    assert machine.record.owner is ExecutionOwner.NONE
    assert machine.record.last_transaction is not None
    assert machine.record.last_transaction.rollback_status is RollbackStatus.CONFIRMED


def _test_confirmed_rce_continuation_physical_window() -> None:
    for age_seconds, should_continue in ((25.0, True), (25.1, False)):
        machine, _base, readback = _begin_rce_until_executing()
        observed_at = NOW + timedelta(seconds=6)
        now = observed_at + timedelta(seconds=age_seconds)
        current = replace(
            readback,
            ems_observed_at=now - timedelta(seconds=1),
            gcf_observed_at=now - timedelta(seconds=1),
        )
        machine.check_continuation(
            current,
            _gates(ExecutionOwner.RCE),
            PhysicalVerification(
                transaction_id="tx:rce:0001",
                action=ExecutionAction.RCE_EXPORT,
                status=VerificationStatus.CONFIRMED,
                observed_at=observed_at,
                evidence=("confirmed discharge across one telemetry drop",),
            ),
            authorization_current=True,
            now=now,
            maximum_readback_age_seconds=RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
        )
        if should_continue:
            assert machine.record.state is ActiveState.EXECUTING
            assert machine.record.reason is ExecutionReason.CONTINUATION_CONFIRMED
        else:
            assert machine.record.state is ActiveState.STOPPING
            assert machine.record.reason is ExecutionReason.PHYSICAL_UNAVAILABLE


def _test_generation_bound_prewrite_and_bounded_restore_retry() -> None:
    base = _snapshot()
    prewrite = SupervisorExecutor()
    prewrite.select_intent(
        _rce_intent(base),
        transaction_id="tx:rce:prewrite1",
        now=NOW,
    )
    prewrite.start(base, _gates(), now=NOW + timedelta(seconds=1))
    envelope = prewrite.prepare_command(
        base,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=2),
    )
    assert envelope is not None
    generation_drift = replace(
        base,
        ems_generation=base.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=2, milliseconds=100),
    )
    assert not prewrite.command_prewrite_ready(
        generation_drift,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=2, milliseconds=200),
    )
    assert prewrite.command_prewrite_ready(
        base,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=2, milliseconds=200),
    )

    machine, active_base, active_envelope = _begin_rce_until_waiting()
    active = _apply_command_readback(
        active_base,
        active_envelope.command,
        observed_at=NOW + timedelta(seconds=4),
    )
    assert machine.observe_readback(
        active,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.MATCH
    machine.observe_physical_verification(
        PhysicalVerification(
            transaction_id="tx:rce:0001",
            action=ExecutionAction.RCE_EXPORT,
            status=VerificationStatus.CONFIRMED,
            observed_at=NOW + timedelta(seconds=5),
        ),
        now=NOW + timedelta(seconds=5),
    )
    machine.request_stop()
    restore = machine.prepare_restore(
        active,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    )
    assert restore is not None
    restore_generation_drift = replace(
        active,
        ems_generation=active.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=6, milliseconds=100),
    )
    assert not machine.restore_prewrite_ready(
        restore_generation_drift,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6, milliseconds=200),
    )

    machine.mark_restore_sent(restore, sent_at=NOW + timedelta(seconds=7))
    assert machine.record.transaction is not None
    assert machine.record.transaction.restore_attempts == 1
    no_effect = replace(
        active,
        ems_generation=active.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=8),
    )
    assert machine.observe_restore_readback(
        no_effect,
        now=NOW + timedelta(seconds=8),
    ) is ReadbackVerdict.MISMATCH
    assert machine.record.state is ActiveState.RESTORING
    assert machine.record.owner is ExecutionOwner.RCE
    assert machine.record.transaction is not None
    assert machine.record.transaction.rollback_status is RollbackStatus.PENDING
    assert machine.record.transaction.rollback_result is VerificationStatus.UNAVAILABLE
    assert machine.record.transaction.restore_sent_at == NOW + timedelta(seconds=7)

    restarted_between_generations = SupervisorExecutor.recover(machine.record)
    assert restarted_between_generations.record.state is ActiveState.RESTORING
    assert restarted_between_generations.record.reason is ExecutionReason.RESTART_RECOVERY
    assert restarted_between_generations.record.transaction is not None
    assert restarted_between_generations.record.transaction.restore_attempts == 1
    assert restarted_between_generations.observe_restore_readback(
        no_effect,
        now=NOW + timedelta(seconds=9),
    ) is ReadbackVerdict.PENDING
    settled_after_restart = replace(
        no_effect,
        ems_generation=no_effect.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=10),
    )
    assert restarted_between_generations.observe_restore_readback(
        settled_after_restart,
        now=NOW + timedelta(seconds=10),
    ) is ReadbackVerdict.MISMATCH
    assert restarted_between_generations.record.state is ActiveState.STOPPING
    assert restarted_between_generations.record.transaction is not None
    assert restarted_between_generations.record.transaction.restore_attempts == 1

    assert machine.observe_restore_readback(
        no_effect,
        now=NOW + timedelta(seconds=9),
    ) is ReadbackVerdict.PENDING
    assert machine.record.state is ActiveState.RESTORING

    settled_no_effect = replace(
        no_effect,
        ems_generation=no_effect.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=10),
    )
    assert machine.observe_restore_readback(
        settled_no_effect,
        now=NOW + timedelta(seconds=10),
    ) is ReadbackVerdict.MISMATCH
    assert machine.record.state is ActiveState.STOPPING

    foreign_guard = SupervisorExecutor(machine.record)
    foreign_change = replace(
        settled_no_effect,
        ems_block=replace(
            settled_no_effect.ems_block,
            force_discharge_soc_percent_4305=35.0,
        ),
        ems_generation=settled_no_effect.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=10, milliseconds=50),
    )
    assert foreign_guard.prepare_restore(
        foreign_change,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=10, milliseconds=75),
    ) is None
    assert foreign_guard.record.state is ActiveState.FAULT
    assert foreign_guard.record.owner is ExecutionOwner.RCE
    assert foreign_guard.record.reason is ExecutionReason.ROLLBACK_FAILED

    retry = machine.prepare_restore(
        settled_no_effect,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=10, milliseconds=100),
    )
    assert retry is not None
    assert (
        retry.atomic_writes[0].snapshot_generation
        == settled_no_effect.ems_generation
    )
    machine.mark_restore_sent(retry, sent_at=NOW + timedelta(seconds=11))
    assert machine.record.transaction is not None
    assert machine.record.transaction.restore_attempts == 2
    repeated_no_effect = replace(
        settled_no_effect,
        ems_generation=settled_no_effect.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=12),
    )
    assert machine.observe_restore_readback(
        repeated_no_effect,
        now=NOW + timedelta(seconds=12),
    ) is ReadbackVerdict.MISMATCH
    assert machine.record.state is ActiveState.RESTORING
    assert machine.record.owner is ExecutionOwner.RCE
    assert machine.record.transaction is not None
    assert machine.record.transaction.rollback_status is RollbackStatus.PENDING
    assert machine.record.transaction.restore_attempts == 2

    assert machine.observe_restore_readback(
        repeated_no_effect,
        now=NOW + timedelta(seconds=13),
    ) is ReadbackVerdict.PENDING
    assert machine.record.state is ActiveState.RESTORING

    retry_settled_no_effect = replace(
        repeated_no_effect,
        ems_generation=repeated_no_effect.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=14),
    )
    assert machine.observe_restore_readback(
        retry_settled_no_effect,
        now=NOW + timedelta(seconds=14),
    ) is ReadbackVerdict.MISMATCH
    assert machine.record.state is ActiveState.FAULT
    assert machine.record.owner is ExecutionOwner.RCE
    assert machine.record.reason is ExecutionReason.ROLLBACK_FAILED
    assert machine.record.transaction is not None
    assert machine.record.transaction.rollback_status is RollbackStatus.FAILED
    assert machine.record.transaction.restore_attempts == 2

    recovered = SupervisorExecutor.recover(machine.record)
    assert recovered.record.state is ActiveState.STOPPING
    assert recovered.record.owner is ExecutionOwner.RCE
    assert recovered.record.transaction is not None
    assert recovered.record.transaction.rollback_status is RollbackStatus.PENDING
    assert recovered.record.transaction.rollback_result is VerificationStatus.PENDING
    assert recovered.record.transaction.restore_command is None
    assert recovered.record.transaction.restore_snapshot is not None
    assert recovered.record.transaction.restore_expected_readback is None
    assert recovered.record.transaction.restore_sent_at is None
    assert recovered.record.transaction.restore_attempts == 2

    assert recovered.prepare_restore(
        retry_settled_no_effect,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=15),
    ) is None
    assert recovered.record.state is ActiveState.FAULT
    assert recovered.record.transaction is not None
    assert recovered.record.transaction.restore_attempts == 2

    recovered_target = SupervisorExecutor.recover(machine.record)
    physically_restored = replace(
        retry_settled_no_effect,
        ems_block=restore.command.ems_block,
        ems_generation=retry_settled_no_effect.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=16),
    )
    assert recovered_target.prepare_restore(
        physically_restored,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=16),
    ) is None
    assert recovered_target.record.state is ActiveState.IDLE
    assert recovered_target.record.owner is ExecutionOwner.NONE

    explicit_stop = SupervisorExecutor.recover(machine.record)
    explicit_stop.request_master_stop(
        retry_settled_no_effect,
        _gates(ExecutionOwner.RCE),
        transaction_id="master:retry:explicit",
        now=NOW + timedelta(seconds=17),
    )
    assert explicit_stop.record.transaction is not None
    assert explicit_stop.record.transaction.restore_attempts == 0
    explicit_restore = explicit_stop.prepare_restore(
        retry_settled_no_effect,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=18),
    )
    assert explicit_restore is not None
    assert explicit_stop.record.transaction is not None
    assert explicit_stop.record.transaction.restore_attempts == 1


def _test_migrated_v1_rce_retry_accepts_only_confirmed_original_intent() -> None:
    base = _snapshot(
        ems_block=replace(
            _block(),
            force_discharge_soc_percent_4305=30.0,
            maximum_discharge_power_percent_4306=50.0,
        )
    )
    rce_block = replace(
        base.ems_block,
        mode=EmsMode.GRID_DISCHARGE,
        force_discharge_soc_percent_4305=41.0,
        maximum_discharge_power_percent_4306=25.4,
    )
    intent = replace(
        _rce_intent(base),
        command=CommandSet(ems_block=rce_block),
        candidate_revision="rce:live:retry1",
    )
    machine = SupervisorExecutor()
    machine.select_intent(intent, transaction_id="tx:rce:live-retry1", now=NOW)
    machine.start(base, _gates(), now=NOW + timedelta(seconds=1))
    envelope = machine.prepare_command(
        base,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=2),
    )
    assert envelope is not None
    machine.mark_command_sent(envelope, sent_at=NOW + timedelta(seconds=3))
    confirmed_intent = _apply_command_readback(
        base,
        envelope.command,
        observed_at=NOW + timedelta(seconds=4),
    )
    assert machine.observe_readback(
        confirmed_intent,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.MATCH
    machine.observe_physical_verification(
        PhysicalVerification(
            transaction_id="tx:rce:live-retry1",
            action=ExecutionAction.RCE_EXPORT,
            status=VerificationStatus.CONFIRMED,
            observed_at=NOW + timedelta(seconds=5),
        ),
        now=NOW + timedelta(seconds=5),
    )
    transaction = machine.record.transaction
    assert transaction is not None
    migrated_transaction = replace(
        transaction,
        state=ActiveState.FAULT,
        reason=ExecutionReason.ROLLBACK_FAILED,
        rollback_status=RollbackStatus.FAILED,
        rollback_result=VerificationStatus.CONTRADICTED,
        restore_command=None,
        restore_snapshot=base,
        restore_expected_readback=None,
        restore_sent_at=None,
        restore_attempts=1,
    )
    migrated_record = replace(
        machine.record,
        state=ActiveState.FAULT,
        transaction=migrated_transaction,
        reason=ExecutionReason.ROLLBACK_FAILED,
    )

    recovered = SupervisorExecutor.recover(migrated_record)
    retry = recovered.prepare_restore(
        confirmed_intent,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    )
    assert retry is not None
    assert retry.command.ems_block == base.ems_block
    assert retry.atomic_writes[0].snapshot_generation == confirmed_intent.ems_generation
    assert recovered.record.transaction is not None
    assert recovered.record.transaction.restore_attempts == 2

    foreign = replace(
        confirmed_intent,
        ems_block=replace(
            confirmed_intent.ems_block,
            force_discharge_soc_percent_4305=42.0,
        ),
        ems_generation=confirmed_intent.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=5, milliseconds=500),
    )
    foreign_recovery = SupervisorExecutor.recover(migrated_record)
    assert foreign_recovery.prepare_restore(
        foreign,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    ) is None
    assert foreign_recovery.record.state is ActiveState.FAULT
    assert foreign_recovery.record.reason is ExecutionReason.ROLLBACK_FAILED
    assert foreign_recovery.record.transaction is not None
    assert foreign_recovery.record.transaction.restore_attempts == 1

    old_generation = replace(
        confirmed_intent,
        ems_generation=base.ems_generation,
    )
    old_generation_recovery = SupervisorExecutor.recover(migrated_record)
    assert old_generation_recovery.prepare_restore(
        old_generation,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    ) is None
    assert old_generation_recovery.record.state is ActiveState.FAULT
    assert old_generation_recovery.record.reason is ExecutionReason.ROLLBACK_FAILED

    old_evidence = replace(
        confirmed_intent,
        ems_observed_at=NOW + timedelta(seconds=3),
    )
    old_evidence_recovery = SupervisorExecutor.recover(migrated_record)
    assert old_evidence_recovery.prepare_restore(
        old_evidence,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    ) is None
    assert old_evidence_recovery.record.state is ActiveState.FAULT
    assert old_evidence_recovery.record.reason is ExecutionReason.ROLLBACK_FAILED


def _test_direct_atomic_families_and_touched_generations() -> None:
    base = _snapshot(export_limit=50.0, battery_limit=50.0)
    intent = ActuatorIntent(
        policy=ExecutionOwner.RCM,
        action=ExecutionAction.RCM_ABSORB_AND_LIMIT,
        command=CommandSet(
            export_limit_percent_259=20.0,
            battery_max_charge_power_percent_306=80.0,
        ),
        candidate_revision="rcm:revision:0001",
        deadline=NOW + timedelta(seconds=120),
    )
    machine = SupervisorExecutor()
    machine.select_intent(intent, transaction_id="tx:rcm:0001", now=NOW)
    machine.start(base, _gates(), now=NOW + timedelta(seconds=1))
    envelope = machine.prepare_command(
        base,
        _gates("rcm"),
        now=NOW + timedelta(seconds=2),
    )
    assert envelope is not None
    assert tuple(write.family.value for write in envelope.atomic_writes) == (
        "ems_supervisor_write_gcf_export_limit",
        "ems_supervisor_write_battery_charge_limit",
    )
    assert tuple(write.snapshot_generation for write in envelope.atomic_writes) == (20, 30)
    machine.mark_command_sent(envelope, sent_at=NOW + timedelta(seconds=3))
    only_battery_advanced = replace(
        base,
        export_limit_percent_259=20.0,
        battery_max_charge_power_percent_306=80.0,
        battery_generation=31,
        battery_observed_at=NOW + timedelta(seconds=4),
    )
    assert machine.observe_readback(
        only_battery_advanced,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.PENDING
    complete = replace(
        only_battery_advanced,
        gcf_generation=21,
        gcf_observed_at=NOW + timedelta(seconds=5),
    )
    assert machine.observe_readback(
        complete,
        now=NOW + timedelta(seconds=5),
    ) is ReadbackVerdict.MATCH
    machine.observe_physical_verification(
        PhysicalVerification(
            transaction_id="tx:rcm:0001",
            action=ExecutionAction.RCM_ABSORB_AND_LIMIT,
            status=VerificationStatus.CONFIRMED,
            observed_at=NOW + timedelta(seconds=6),
        ),
        now=NOW + timedelta(seconds=6),
    )
    assert machine.record.state is ActiveState.EXECUTING
    machine.check_continuation(
        complete,
        _gates(ExecutionOwner.RCM),
        PhysicalVerification(
            transaction_id="tx:rcm:0001",
            action=ExecutionAction.RCM_ABSORB_AND_LIMIT,
            status=VerificationStatus.CONFIRMED,
            observed_at=NOW + timedelta(seconds=7),
        ),
        authorization_current=False,
        now=NOW + timedelta(seconds=7),
    )
    assert machine.record.state is ActiveState.STOPPING
    assert machine.record.reason is ExecutionReason.AUTHORIZATION_LOST

    partial_dispatch = SupervisorExecutor(machine.record)
    partial_restore = partial_dispatch.prepare_restore(
        complete,
        _gates(ExecutionOwner.RCM),
        now=NOW + timedelta(seconds=8),
    )
    assert partial_restore is not None
    partial_dispatch.mark_restore_outcome_unknown()
    partial_readback = replace(
        complete,
        export_limit_percent_259=base.export_limit_percent_259,
        gcf_generation=22,
        gcf_observed_at=NOW + timedelta(seconds=9),
        battery_generation=32,
        battery_observed_at=NOW + timedelta(seconds=9),
    )
    assert partial_dispatch.prepare_restore(
        partial_readback,
        _gates(ExecutionOwner.RCM),
        now=NOW + timedelta(seconds=9),
    ) is None
    assert partial_dispatch.record.state is ActiveState.FAULT
    assert partial_dispatch.record.reason is ExecutionReason.ROLLBACK_FAILED
    assert partial_dispatch.record.transaction is not None
    assert partial_dispatch.record.transaction.restore_attempts == 1

    restore = machine.prepare_restore(
        complete,
        _gates(ExecutionOwner.RCM),
        now=NOW + timedelta(seconds=8),
    )
    assert restore is not None
    machine.mark_restore_sent(restore, sent_at=NOW + timedelta(seconds=9))
    first_old_readback = replace(
        complete,
        gcf_generation=22,
        gcf_observed_at=NOW + timedelta(seconds=10),
        battery_generation=32,
        battery_observed_at=NOW + timedelta(seconds=10),
    )
    assert machine.observe_restore_readback(
        first_old_readback,
        now=NOW + timedelta(seconds=10),
    ) is ReadbackVerdict.MISMATCH
    assert machine.record.state is ActiveState.RESTORING

    only_gcf_settled = replace(
        first_old_readback,
        gcf_generation=23,
        gcf_observed_at=NOW + timedelta(seconds=11),
    )
    assert machine.observe_restore_readback(
        only_gcf_settled,
        now=NOW + timedelta(seconds=11),
    ) is ReadbackVerdict.PENDING
    assert machine.record.state is ActiveState.RESTORING

    both_families_settled = replace(
        only_gcf_settled,
        battery_generation=33,
        battery_observed_at=NOW + timedelta(seconds=12),
    )
    assert machine.observe_restore_readback(
        both_families_settled,
        now=NOW + timedelta(seconds=12),
    ) is ReadbackVerdict.MISMATCH
    assert machine.record.state is ActiveState.STOPPING

    retry = machine.prepare_restore(
        both_families_settled,
        _gates(ExecutionOwner.RCM),
        now=NOW + timedelta(seconds=13),
    )
    assert retry is not None
    assert tuple(write.snapshot_generation for write in retry.atomic_writes) == (23, 33)
    machine.mark_restore_sent(retry, sent_at=NOW + timedelta(seconds=14))
    restored = replace(
        both_families_settled,
        export_limit_percent_259=base.export_limit_percent_259,
        gcf_generation=24,
        gcf_observed_at=NOW + timedelta(seconds=15),
        battery_max_charge_power_percent_306=(
            base.battery_max_charge_power_percent_306
        ),
        battery_generation=34,
        battery_observed_at=NOW + timedelta(seconds=15),
    )
    assert machine.observe_restore_readback(
        restored,
        now=NOW + timedelta(seconds=15),
    ) is ReadbackVerdict.MATCH
    assert machine.record.state is ActiveState.IDLE
    assert machine.record.owner is ExecutionOwner.NONE


def _test_action_local_readiness_through_transaction_and_restore() -> None:
    stale_at = NOW - timedelta(seconds=60)
    cases = (
        (
            "tariff-ems",
            _snapshot(gcf_at=stale_at, battery_at=stale_at),
            lambda base: _tariff_intent(base),
            ExecutionOwner.TARIFF,
            _gates(direct_259_ready=False, direct_306_ready=False),
            _gates(
                ExecutionOwner.TARIFF,
                direct_259_ready=False,
                direct_306_ready=False,
            ),
            lambda current: replace(
                current,
                gcf_enabled_258=True,
                export_limit_percent_259=0.0,
                battery_max_charge_power_percent_306=70.0,
            ),
        ),
        (
            "direct-306",
            _snapshot(gcf_at=stale_at),
            lambda _base: _direct_306_intent(),
            ExecutionOwner.RCM,
            _gates(direct_259_ready=False, direct_306_ready=True),
            _gates(
                ExecutionOwner.RCM,
                direct_259_ready=False,
                direct_306_ready=True,
            ),
            lambda current: replace(
                current,
                gcf_enabled_258=True,
                export_limit_percent_259=0.0,
            ),
        ),
        (
            "direct-259",
            _snapshot(battery_at=stale_at),
            lambda _base: _direct_259_intent(),
            ExecutionOwner.RCM,
            _gates(direct_259_ready=True, direct_306_ready=False),
            _gates(
                ExecutionOwner.RCM,
                direct_259_ready=True,
                direct_306_ready=False,
            ),
            lambda current: replace(
                current,
                battery_max_charge_power_percent_306=70.0,
            ),
        ),
    )
    for index, (
        label,
        base,
        intent_factory,
        owner,
        start_gates,
        owned_gates,
        mutate_irrelevant,
    ) in enumerate(cases, start=1):
        intent = intent_factory(base)
        machine = SupervisorExecutor()
        machine.select_intent(
            intent,
            transaction_id=f"tx:local:{index:04d}",
            now=NOW,
        )
        machine.start(base, start_gates, now=NOW + timedelta(seconds=1))
        assert machine.record.state is ActiveState.STARTING, (
            f"{label}: an unavailable/stale unused family blocked start"
        )

        prewrite = mutate_irrelevant(base)
        envelope = machine.prepare_command(
            prewrite,
            owned_gates,
            now=NOW + timedelta(seconds=2),
        )
        assert envelope is not None, (
            f"{label}: unused-family drift blocked the pre-write snapshot"
        )
        if intent.command.touches_ems:
            assert tuple(write.family for write in envelope.atomic_writes) == (
                AtomicWriteFamily.EMS_COMPLETE_BLOCK,
            ), f"{label}: EMS write must remain the complete 4300-4306 block"
        machine.mark_command_sent(
            envelope,
            sent_at=NOW + timedelta(seconds=3),
        )
        readback = _apply_command_readback(
            prewrite,
            envelope.command,
            observed_at=NOW + timedelta(seconds=4),
        )
        assert machine.observe_readback(
            readback,
            now=NOW + timedelta(seconds=4),
        ) is ReadbackVerdict.MATCH, (
            f"{label}: stale unused family blocked command readback"
        )
        machine.observe_physical_verification(
            PhysicalVerification(
                transaction_id=f"tx:local:{index:04d}",
                action=intent.action,
                status=VerificationStatus.CONFIRMED,
                observed_at=NOW + timedelta(seconds=5),
            ),
            now=NOW + timedelta(seconds=5),
        )
        assert machine.record.state is ActiveState.EXECUTING
        assert machine.check_continuation_authority(
            readback,
            owned_gates,
            authorization_current=True,
            now=NOW + timedelta(seconds=6),
        ), f"{label}: stale unused family revoked continuation"

        machine.request_stop()
        restore = machine.prepare_restore(
            readback,
            owned_gates,
            now=NOW + timedelta(seconds=7),
        )
        assert restore is not None, (
            f"{label}: stale unused family blocked bounded restore"
        )
        machine.mark_restore_sent(
            restore,
            sent_at=NOW + timedelta(seconds=8),
        )
        restored = _apply_command_readback(
            readback,
            restore.command,
            observed_at=NOW + timedelta(seconds=9),
        )
        assert machine.observe_restore_readback(
            restored,
            now=NOW + timedelta(seconds=9),
        ) is ReadbackVerdict.MATCH, (
            f"{label}: stale unused family blocked restore confirmation"
        )
        assert machine.record.state is ActiveState.IDLE
        assert machine.record.owner is ExecutionOwner.NONE


def _test_touched_family_and_ems_remain_fail_closed() -> None:
    stale_at = NOW - timedelta(seconds=60)
    stale_cases = (
        (
            "EMS",
            _snapshot(ems_at=stale_at),
            lambda base: _tariff_intent(base),
            _gates(),
        ),
        (
            "306",
            _snapshot(battery_at=stale_at),
            lambda _base: _direct_306_intent(),
            _gates(direct_306_ready=True),
        ),
        (
            "259",
            _snapshot(gcf_at=stale_at),
            lambda _base: _direct_259_intent(),
            _gates(direct_259_ready=True),
        ),
    )
    for index, (label, snapshot, intent_factory, gates) in enumerate(
        stale_cases,
        start=1,
    ):
        machine = SupervisorExecutor()
        machine.select_intent(
            intent_factory(snapshot),
            transaction_id=f"tx:stale:{index:04d}",
            now=NOW,
        )
        machine.start(snapshot, gates, now=NOW + timedelta(seconds=1))
        assert machine.record.state is ActiveState.BLOCKED
        assert machine.record.reason is ExecutionReason.STALE_INPUTS, (
            f"{label}: stale touched/EMS cohort did not fail closed"
        )

    unavailable_cases = (
        (_tariff_intent(_snapshot()), _gates(full_block_ready=False)),
        (_direct_306_intent(), _gates(direct_306_ready=False)),
        (_direct_259_intent(), _gates(direct_259_ready=False)),
    )
    for index, (intent, gates) in enumerate(unavailable_cases, start=1):
        machine = SupervisorExecutor()
        machine.select_intent(
            intent,
            transaction_id=f"tx:unavailable:{index:04d}",
            now=NOW,
        )
        machine.start(_snapshot(), gates, now=NOW + timedelta(seconds=1))
        assert machine.record.state is ActiveState.BLOCKED
        assert machine.record.reason in {
            ExecutionReason.FULL_BLOCK_UNAVAILABLE,
            ExecutionReason.DIRECT_REGISTER_UNAVAILABLE,
        }


def _test_prewrite_zero_export_recheck_releases_owner_without_write() -> None:
    initially_allowed = _snapshot(gcf_enabled=False, export_limit=0.0)
    machine = SupervisorExecutor()
    machine.select_intent(
        _rce_intent(initially_allowed),
        transaction_id="tx:rce:zero1",
        now=NOW,
    )
    machine.start(initially_allowed, _gates(), now=NOW + timedelta(seconds=1))
    confirmed_zero = replace(
        initially_allowed,
        gcf_enabled_258=True,
        gcf_generation=initially_allowed.gcf_generation + 1,
        gcf_observed_at=NOW + timedelta(seconds=2),
    )
    assert machine.prepare_command(
        confirmed_zero,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=2),
    ) is None
    assert machine.record.state is ActiveState.BLOCKED
    assert machine.record.owner is ExecutionOwner.NONE
    assert machine.record.reason is ExecutionReason.CONFIRMED_ZERO_EXPORT
    assert machine.record.transaction is not None
    assert machine.record.transaction.command_sent_at is None


def _test_off_grid_blocks_normal_start() -> None:
    off_grid = _snapshot(ems_block=_block(EmsMode.OFF_GRID))
    machine = SupervisorExecutor()
    machine.select_intent(
        _rce_intent(off_grid),
        transaction_id="tx:rce:offgrid",
        now=NOW,
    )
    machine.start(off_grid, _gates(), now=NOW + timedelta(seconds=1))
    assert machine.record.state is ActiveState.BLOCKED
    assert machine.record.owner is ExecutionOwner.NONE
    assert machine.record.reason is ExecutionReason.OFF_GRID_PRESERVED


def _test_restart_recovery_never_assumes_success() -> None:
    machine, _, _ = _begin_rce_until_waiting()
    recovered = SupervisorExecutor.recover(machine.record)
    assert recovered.record.state is ActiveState.STOPPING
    assert recovered.record.owner is ExecutionOwner.RCE
    assert recovered.record.reason is ExecutionReason.RESTART_RECOVERY
    assert recovered.record.transaction is not None
    assert recovered.record.transaction.rollback_status is RollbackStatus.PENDING

    selected = SupervisorExecutor()
    base = _snapshot()
    selected.select_intent(
        _rce_intent(base),
        transaction_id="tx:rce:selected",
        now=NOW,
    )
    reselected = SupervisorExecutor.recover(selected.record)
    assert reselected.record.state is ActiveState.IDLE
    assert reselected.record.owner is ExecutionOwner.NONE
    assert reselected.record.reason is ExecutionReason.RESTART_RESELECT_REQUIRED


def _test_unknown_restore_outcome_keeps_owner_for_recovery() -> None:
    machine, base, command = _begin_rce_until_waiting()
    machine.request_stop()
    active = _apply_command_readback(
        base,
        command.command,
        observed_at=NOW + timedelta(seconds=4),
    )
    restore = machine.prepare_restore(
        active,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=5),
    )
    assert restore is not None
    machine.mark_restore_outcome_unknown()
    assert machine.record.state is ActiveState.FAULT
    assert machine.record.owner is ExecutionOwner.RCE
    assert machine.record.reason is ExecutionReason.COMMAND_OUTCOME_UNKNOWN
    assert machine.record.transaction is not None
    assert machine.record.transaction.restore_command is None
    assert machine.record.transaction.restore_expected_readback is None
    assert machine.record.transaction.restore_attempts == 1
    recovered = SupervisorExecutor.recover(machine.record)
    assert recovered.record.state is ActiveState.STOPPING
    assert recovered.record.owner is ExecutionOwner.RCE
    assert recovered.record.transaction is not None
    assert recovered.record.transaction.restore_snapshot is not None
    assert recovered.record.transaction.restore_attempts == 1


def _test_current_restore_target_finishes_without_dispatch() -> None:
    machine, base, _ = _begin_rce_until_waiting()
    machine.mark_command_outcome_unknown()
    machine.request_master_stop(
        base,
        _gates(ExecutionOwner.RCE),
        transaction_id="master:stop:already-restored",
        now=NOW + timedelta(seconds=4),
    )
    uncertain_restore = machine.prepare_restore(
        base,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=5),
    )
    assert uncertain_restore is None  # No post-dispatch FC03 yet.
    assert machine.record.state is ActiveState.STOPPING
    assert machine.record.owner is ExecutionOwner.RCE
    assert machine.record.master_stop_result.status is MasterStopStatus.REQUESTED

    confirmed, confirmed_base, _ = _begin_rce_until_waiting()
    confirmed.mark_command_outcome_unknown()
    post_dispatch_base = replace(
        confirmed_base,
        ems_generation=confirmed_base.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=4),
    )
    confirmed.request_master_stop(
        post_dispatch_base,
        _gates(ExecutionOwner.RCE),
        transaction_id="master:stop:physically-restored",
        now=NOW + timedelta(seconds=4),
    )
    assert confirmed.prepare_restore(
        post_dispatch_base,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=5),
    ) is None
    assert confirmed.record.state is ActiveState.STOPPING
    assert confirmed.record.owner is ExecutionOwner.RCE
    second_neutral = replace(
        post_dispatch_base,
        ems_generation=post_dispatch_base.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=6),
    )
    explicit_restore = confirmed.prepare_restore(
        second_neutral, _gates(ExecutionOwner.RCE), now=NOW + timedelta(seconds=6)
    )
    assert explicit_restore is not None
    confirmed.mark_restore_sent(explicit_restore, sent_at=NOW + timedelta(seconds=7))
    confirmed_neutral = replace(
        second_neutral,
        ems_generation=second_neutral.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=8),
    )
    assert confirmed.observe_restore_readback(
        confirmed_neutral, now=NOW + timedelta(seconds=8)
    ) is ReadbackVerdict.MATCH
    assert confirmed.record.state is ActiveState.IDLE
    assert confirmed.record.owner is ExecutionOwner.NONE
    assert confirmed.record.reason is ExecutionReason.MASTER_STOP_COMPLETE
    assert confirmed.record.master_stop_result.status is MasterStopStatus.COMPLETED
    assert confirmed.record.last_transaction is not None
    assert confirmed.record.last_transaction.restore_sent_at == NOW + timedelta(seconds=7)
    assert confirmed.record.last_transaction.rollback_status is RollbackStatus.CONFIRMED
    assert confirmed.record.last_transaction.rollback_result is VerificationStatus.CONFIRMED


def _test_physical_generation_rollover_is_newer() -> None:
    base = _snapshot(ems_generation=15_999_998)
    machine = SupervisorExecutor()
    machine.select_intent(
        _rce_intent(base),
        transaction_id="tx:rce:rollover",
        now=NOW,
    )
    machine.start(base, _gates(), now=NOW + timedelta(seconds=1))
    envelope = machine.prepare_command(
        base,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=2),
    )
    assert envelope is not None
    machine.mark_command_sent(envelope, sent_at=NOW + timedelta(seconds=3))
    same_generation = replace(
        base,
        ems_block=envelope.command.ems_block,
        ems_observed_at=NOW + timedelta(seconds=4),
    )
    assert machine.observe_readback(
        same_generation,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.PENDING
    rollover = replace(
        base,
        ems_block=envelope.command.ems_block,
        ems_generation=1,
        ems_observed_at=NOW + timedelta(seconds=5),
    )
    assert machine.observe_readback(
        rollover,
        now=NOW + timedelta(seconds=5),
    ) is ReadbackVerdict.MATCH

    reverse_base = _snapshot(ems_generation=2)
    reverse = SupervisorExecutor()
    reverse.select_intent(
        _rce_intent(reverse_base),
        transaction_id="tx:rce:oldwrap",
        now=NOW,
    )
    reverse.start(reverse_base, _gates(), now=NOW + timedelta(seconds=1))
    reverse_envelope = reverse.prepare_command(
        reverse_base,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=2),
    )
    assert reverse_envelope is not None
    reverse.mark_command_sent(
        reverse_envelope,
        sent_at=NOW + timedelta(seconds=3),
    )
    stale_pre_wrap = replace(
        reverse_base,
        ems_block=reverse_envelope.command.ems_block,
        ems_generation=16_000_000,
        ems_observed_at=NOW + timedelta(seconds=4),
    )
    assert reverse.observe_readback(
        stale_pre_wrap,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.PENDING


def _test_ack_timeouts_freshness_and_pending_idempotence() -> None:
    machine, base, _ = _begin_rce_until_waiting()
    first_revision = machine.record.revision
    assert machine.observe_readback(
        base,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.PENDING
    pending_revision = machine.record.revision
    assert pending_revision == first_revision + 1
    assert machine.observe_readback(
        base,
        now=NOW + timedelta(seconds=5),
    ) is ReadbackVerdict.PENDING
    assert machine.record.revision == pending_revision
    transaction = machine.record.transaction
    assert transaction is not None
    assert transaction.command_sent_at is not None
    machine.check_deadline(
        now=transaction.command_sent_at + timedelta(seconds=89, milliseconds=999)
    )
    assert machine.record.state is ActiveState.WAITING_READBACK
    timeout_at = transaction.command_sent_at + timedelta(seconds=90)
    machine.check_deadline(now=timeout_at)
    assert machine.record.state is ActiveState.FAULT
    assert machine.record.reason is ExecutionReason.DEADLINE_REACHED
    assert machine.record.transaction is not None
    assert machine.record.transaction.rollback_status is RollbackStatus.PENDING

    # The timeout and restore preparation may share a frame whose FC03 cohort
    # is already stale.  This is absence of rollback evidence, not a failed
    # rollback; a later fresh FC03 must be able to resume the safe return.
    fault_revision = machine.record.revision
    assert machine.prepare_restore(
        base,
        _gates(ExecutionOwner.RCE),
        now=timeout_at,
    ) is None
    assert machine.record.revision == fault_revision
    assert machine.record.state is ActiveState.FAULT
    assert machine.record.reason is ExecutionReason.DEADLINE_REACHED
    assert machine.record.transaction is not None
    assert machine.record.transaction.rollback_status is RollbackStatus.PENDING
    assert machine.record.transaction.restore_attempts == 0

    fresh_at = timeout_at + timedelta(seconds=1)
    fresh = replace(
        base,
        ems_generation=base.ems_generation + 1,
        ems_observed_at=fresh_at - timedelta(seconds=1),
    )
    assert machine.prepare_restore(
        fresh, _gates(ExecutionOwner.RCE), now=fresh_at
    ) is None
    assert machine.record.owner is ExecutionOwner.RCE
    next_fresh = replace(
        fresh,
        ems_generation=fresh.ems_generation + 1,
        ems_observed_at=fresh_at + timedelta(seconds=1),
    )
    resumed_restore = machine.prepare_restore(
        next_fresh,
        _gates(ExecutionOwner.RCE),
        now=fresh_at + timedelta(seconds=1),
    )
    assert resumed_restore is not None
    assert resumed_restore.command.ems_block == base.ems_block
    assert machine.record.state is ActiveState.FAULT
    assert machine.record.owner is ExecutionOwner.RCE
    assert machine.record.reason is ExecutionReason.RESTORING
    assert machine.record.transaction is not None
    assert machine.record.transaction.rollback_status is RollbackStatus.PENDING
    assert machine.record.transaction.restore_attempts == 1

    physical, physical_base, physical_envelope = _begin_rce_until_waiting()
    matched = replace(
        physical_base,
        ems_block=physical_envelope.command.ems_block,
        ems_generation=physical_base.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=4),
    )
    assert physical.observe_readback(
        matched,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.MATCH
    matched_revision = physical.record.revision
    assert physical.observe_readback(
        matched,
        now=NOW + timedelta(seconds=5),
    ) is ReadbackVerdict.MATCH
    assert physical.record.revision == matched_revision
    physical.observe_physical_verification(
        PhysicalVerification(
            transaction_id="tx:rce:0001",
            action=ExecutionAction.RCE_EXPORT,
            status=VerificationStatus.CONFIRMED,
            observed_at=NOW + timedelta(seconds=5),
        ),
        now=NOW + timedelta(seconds=21),
    )
    assert physical.record.state is ActiveState.FAULT
    assert physical.record.reason is ExecutionReason.PHYSICAL_UNAVAILABLE

    authority, authority_base, authority_envelope = _begin_rce_until_waiting()
    authority_readback = replace(
        authority_base,
        ems_block=authority_envelope.command.ems_block,
        ems_generation=authority_base.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=4),
    )
    assert authority.observe_readback(
        authority_readback,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.MATCH
    authority.observe_physical_verification(
        PhysicalVerification(
            transaction_id="tx:rce:0001",
            action=ExecutionAction.RCE_EXPORT,
            status=VerificationStatus.CONFIRMED,
            observed_at=NOW + timedelta(seconds=5),
        ),
        now=NOW + timedelta(seconds=5),
    )
    assert authority.record.state is ActiveState.EXECUTING
    authority_transaction = authority.record.transaction
    assert authority_transaction is not None
    assert authority_transaction.expected_readback is not None
    assert authority_transaction.command_sent_at is not None
    assert authority_readback.freshness_errors(
        NOW + timedelta(seconds=20),
        required_families=frozenset({AtomicWriteFamily.GCF_EXPORT_LIMIT}),
    ) == ("ems_stale",)
    assert authority_transaction.expected_readback.compare(
        authority_readback,
        now=NOW + timedelta(seconds=20),
        not_before=authority_transaction.command_sent_at,
        required_families=frozenset({AtomicWriteFamily.GCF_EXPORT_LIMIT}),
    ) is ReadbackVerdict.PENDING
    aged_continuation = replace(
        authority_readback,
        gcf_observed_at=NOW + timedelta(seconds=10),
    )
    assert authority.check_continuation_authority(
        aged_continuation,
        _gates(ExecutionOwner.RCE),
        authorization_current=True,
        now=NOW + timedelta(seconds=20),
        maximum_readback_age_seconds=RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
    )
    assert authority.record.state is ActiveState.EXECUTING
    for age in (24.318, 27.934, 29.999):
        assert authority.check_continuation_authority(
            aged_continuation,
            _gates(ExecutionOwner.RCE),
            authorization_current=True,
            now=NOW + timedelta(seconds=4 + age),
            maximum_readback_age_seconds=RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
        )
        assert authority.record.state is ActiveState.EXECUTING
    assert not authority.check_continuation_authority(
        aged_continuation,
        _gates(ExecutionOwner.RCE),
        authorization_current=True,
        now=NOW + timedelta(seconds=34),
        maximum_readback_age_seconds=RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
    )
    assert authority.record.state is ActiveState.STOPPING
    assert authority.record.reason is ExecutionReason.STALE_INPUTS

    restoring, restore_base, active_command = _begin_rce_until_waiting()
    restoring.request_stop()
    active_restore_source = _apply_command_readback(
        restore_base,
        active_command.command,
        observed_at=NOW + timedelta(seconds=4),
    )
    restore = restoring.prepare_restore(
        active_restore_source,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=5),
    )
    assert restore is not None
    restoring.mark_restore_sent(restore, sent_at=NOW + timedelta(seconds=6))
    restoring.check_deadline(
        now=NOW + timedelta(seconds=95, milliseconds=999)
    )
    assert restoring.record.state is ActiveState.RESTORING
    restoring.check_deadline(now=NOW + timedelta(seconds=96))
    assert restoring.record.state is ActiveState.FAULT
    assert restoring.record.owner is ExecutionOwner.RCE
    assert restoring.record.reason is ExecutionReason.ROLLBACK_FAILED
    assert restoring.record.transaction is not None
    assert restoring.record.transaction.rollback_status is RollbackStatus.FAILED


def _test_tariff_physical_requires_acknowledged_grid_charge_readback() -> None:
    base = _snapshot()
    machine = SupervisorExecutor()
    intent = _tariff_intent(base)
    machine.select_intent(
        intent,
        transaction_id="tx:tariff:0001",
        now=NOW + timedelta(seconds=1),
    )
    machine.start(base, _gates(), now=NOW + timedelta(seconds=2))
    envelope = machine.prepare_command(
        base,
        _gates(ExecutionOwner.TARIFF),
        now=NOW + timedelta(seconds=2),
    )
    assert envelope is not None
    machine.mark_command_sent(envelope, sent_at=NOW + timedelta(seconds=3))
    verification = PhysicalVerification(
        transaction_id="tx:tariff:0001",
        action=ExecutionAction.TARIFF_BATTERY_CHARGE,
        status=VerificationStatus.CONFIRMED,
        observed_at=NOW + timedelta(seconds=5),
        evidence=("single coherent GRID/LOAD/BAT/PV cohort",),
    )
    _raises(
        RuntimeError,
        lambda: machine.observe_physical_verification(
            verification,
            now=NOW + timedelta(seconds=5),
        ),
    )
    readback = replace(
        base,
        ems_block=envelope.command.ems_block,
        ems_generation=base.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=4),
    )
    assert machine.observe_readback(
        readback,
        now=NOW + timedelta(seconds=4),
    ) is ReadbackVerdict.MATCH
    machine.observe_physical_verification(
        verification,
        now=NOW + timedelta(seconds=5),
    )
    assert machine.record.state is ActiveState.EXECUTING
    assert machine.record.owner is ExecutionOwner.TARIFF
    assert not machine.check_continuation_authority(
        readback,
        _gates(ExecutionOwner.TARIFF),
        authorization_current=True,
        now=NOW + timedelta(seconds=20),
        maximum_readback_age_seconds=RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
    )
    assert machine.record.state is ActiveState.STOPPING
    assert machine.record.reason is ExecutionReason.STALE_INPUTS


def _test_master_stop_preserves_physical_off_grid() -> None:
    off_grid = _snapshot(ems_block=_block(EmsMode.OFF_GRID))
    idle = SupervisorExecutor()
    idle.request_master_stop(
        off_grid,
        _gates(),
        transaction_id="master:stop:idle1",
        now=NOW,
    )
    assert idle.record.state is ActiveState.IDLE
    assert not idle.record.starts_allowed
    assert not idle.record.automatic_policies_enabled
    assert idle.record.master_stop_result.status is MasterStopStatus.COMPLETED
    assert idle.record.master_stop_result.off_grid_preserved
    assert idle.record.reason is ExecutionReason.OFF_GRID_PRESERVED

    machine = SupervisorExecutor()
    base = _snapshot()
    machine.select_intent(
        _rce_intent(base),
        transaction_id="tx:rce:master1",
        now=NOW,
    )
    machine.start(base, _gates(), now=NOW + timedelta(seconds=1))
    changed_to_off_grid = replace(
        base,
        ems_block=_block(EmsMode.OFF_GRID),
        ems_generation=base.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=2),
    )
    machine.request_master_stop(
        changed_to_off_grid,
        _gates(ExecutionOwner.RCE),
        transaction_id="master:stop:active1",
        now=NOW + timedelta(seconds=2),
    )
    assert machine.record.state is ActiveState.STOPPING
    assert machine.record.owner is ExecutionOwner.RCE
    restore = machine.prepare_restore(
        changed_to_off_grid,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=3),
    )
    assert restore is None
    assert machine.record.state is ActiveState.IDLE
    assert machine.record.owner is ExecutionOwner.NONE
    assert machine.record.master_stop_result.status is MasterStopStatus.COMPLETED
    assert machine.record.master_stop_result.off_grid_preserved
    assert not machine.record.starts_allowed
    machine.rearm_after_master_stop()
    assert machine.record.starts_allowed
    assert machine.record.automatic_policies_enabled


def _test_master_stop_gates_actual_restore_families() -> None:
    base = _snapshot()
    intent = ActuatorIntent(
        policy=ExecutionOwner.RCM,
        action=ExecutionAction.RCM_ABSORB_PV,
        command=CommandSet(battery_max_charge_power_percent_306=80.0),
        candidate_revision="rcm:revision:stop1",
        deadline=NOW + timedelta(seconds=120),
    )
    machine = SupervisorExecutor()
    machine.select_intent(intent, transaction_id="tx:rcm:stop1", now=NOW)
    machine.start(base, _gates(), now=NOW + timedelta(seconds=1))
    machine.request_master_stop(
        base,
        _gates(ExecutionOwner.RCM),
        transaction_id="master:stop:gates1",
        now=NOW + timedelta(seconds=2),
    )
    assert machine.prepare_restore(
        base,
        _gates(ExecutionOwner.RCM, full_block_ready=False),
        now=NOW + timedelta(seconds=3),
    ) is None
    assert machine.record.state is ActiveState.FAULT
    assert machine.record.owner is ExecutionOwner.RCM
    assert machine.record.reason is ExecutionReason.FULL_BLOCK_UNAVAILABLE
    assert machine.record.master_stop_result.status is MasterStopStatus.FAILED


def _test_master_stop_preempts_authorized_manual_cycle_only() -> None:
    charging = _snapshot(ems_block=_block(EmsMode.GRID_CHARGE))
    machine = SupervisorExecutor()
    machine.request_master_stop(
        charging,
        _gates(ExecutionOwner.MANUAL),
        transaction_id="master:stop:manual-cycle1",
        now=NOW,
    )
    assert machine.record.state is ActiveState.STOPPING
    assert machine.record.owner is ExecutionOwner.MANUAL
    assert machine.record.transaction is not None
    assert machine.record.transaction.master_stop_requested
    assert machine.record.master_stop_result.status is MasterStopStatus.REQUESTED

    restore = machine.prepare_restore(
        charging,
        _gates(ExecutionOwner.MANUAL),
        now=NOW + timedelta(seconds=1),
    )
    assert restore is not None
    assert restore.command.ems_block is not None
    assert restore.command.ems_block.mode is EmsMode.SELF_USE
    machine.mark_restore_sent(restore, sent_at=NOW + timedelta(seconds=2))
    restored = _apply_command_readback(
        charging,
        restore.command,
        observed_at=NOW + timedelta(seconds=3),
    )
    assert machine.observe_restore_readback(
        restored,
        now=NOW + timedelta(seconds=3),
    ) is ReadbackVerdict.MATCH
    assert machine.record.state is ActiveState.IDLE
    assert machine.record.owner is ExecutionOwner.NONE
    assert machine.record.master_stop_result.status is MasterStopStatus.COMPLETED
    assert machine.record.reason is ExecutionReason.MASTER_STOP_COMPLETE

    foreign = SupervisorExecutor()
    foreign.request_master_stop(
        charging,
        _gates(ExecutionOwner.RCE),
        transaction_id="master:stop:foreign-cycle1",
        now=NOW,
    )
    assert foreign.record.state is ActiveState.BLOCKED
    assert foreign.record.reason is ExecutionReason.FOREIGN_OWNER
    assert foreign.record.master_stop_result.status is MasterStopStatus.BLOCKED


def _test_latched_block_survives_restart_and_register_ranges_are_exact() -> None:
    assert tuple(state.value for state in ActiveState) == (
        "active_idle",
        "active_selected",
        "active_starting",
        "active_waiting_readback",
        "active_executing",
        "active_retargeting",
        "active_stopping",
        "active_restoring",
        "active_blocked",
        "active_fault",
    )
    stale = _snapshot(at=NOW - timedelta(seconds=30))
    machine = SupervisorExecutor()
    machine.request_master_stop(
        stale,
        _gates(),
        transaction_id="master:stop:stale1",
        now=NOW,
    )
    assert machine.record.state is ActiveState.BLOCKED
    assert machine.record.transaction is None
    recovered = SupervisorExecutor.recover(machine.record)
    assert recovered.record.state is ActiveState.BLOCKED
    assert not recovered.record.starts_allowed

    retry_at = NOW + timedelta(seconds=1)
    recovered.request_master_stop(
        _snapshot(at=retry_at),
        _gates(),
        transaction_id="master:stop:stale-retry1",
        now=retry_at,
    )
    assert recovered.record.state is ActiveState.STOPPING
    assert recovered.record.owner is ExecutionOwner.MANUAL
    assert recovered.record.master_stop_result.status is MasterStopStatus.REQUESTED

    recovered = SupervisorExecutor.recover(machine.record)
    recovered.reset_blocked()
    assert recovered.record.state is ActiveState.IDLE
    assert not recovered.record.starts_allowed
    _raises(RuntimeError, recovered.rearm_after_master_stop)

    _raises(
        ValueError,
        lambda: replace(_block(), backup_soc_percent_4302=59.0),
    )
    assert replace(_block(), backup_soc_percent_4302=60.0).backup_soc_percent_4302 == 60.0
    _raises(
        ValueError,
        lambda: replace(_rce_intent(_snapshot()), physical_verification_required=False),
    )
    _raises(
        RuntimeError,
        lambda: SupervisorExecutor().prepare_command(
            _snapshot(),
            _gates(),
            now=NOW,
        ),
    )


def _test_same_window_rce_retarget_preserves_baseline_and_fresh_proof() -> None:
    for floor, power in ((25.0, 40.0), (30.0, 50.0)):
        variant, variant_baseline, variant_old = _begin_rce_until_executing()
        variant_transaction = variant.record.transaction
        assert variant_transaction is not None
        variant_intent = _rce_retarget_intent(
            variant_old,
            deadline=variant_transaction.deadline,
            floor=floor,
            power=power,
        )
        variant_envelope = variant.prepare_same_owner_retarget(
            variant_intent,
            replace(variant_old, ems_observed_at=NOW + timedelta(seconds=6)),
            _gates(ExecutionOwner.RCE),
            now=NOW + timedelta(seconds=6),
        )
        assert variant_envelope is not None
        variant.mark_retarget_sent(
            variant_envelope,
            sent_at=NOW + timedelta(seconds=7),
        )
        variant_readback = _apply_command_readback(
            variant_old,
            variant_envelope.command,
            observed_at=NOW + timedelta(seconds=8),
        )
        assert variant.observe_readback(
            variant_readback,
            now=NOW + timedelta(seconds=8),
        ) is ReadbackVerdict.MATCH
        variant.observe_physical_verification(
            PhysicalVerification(
                transaction_id="tx:rce:0001",
                action=ExecutionAction.RCE_EXPORT,
                status=VerificationStatus.CONFIRMED,
                observed_at=NOW + timedelta(seconds=9),
                evidence=("variant export", "variant discharge"),
            ),
            now=NOW + timedelta(seconds=9),
        )
        assert variant.record.state is ActiveState.EXECUTING
        assert variant.record.transaction is not None
        assert variant.record.transaction.intent == variant_intent
        assert variant.record.transaction.command_snapshot == variant_baseline

    # A newer desired target may arrive after the successor was persisted but
    # before its sole FC16 reached transport.  Only that unsent successor is
    # discarded; the exact, freshly confirmed predecessor keeps ownership.
    resumed, resumed_baseline, resumed_old = _begin_rce_until_executing()
    predecessor = resumed.record.transaction
    assert predecessor is not None
    prepared_latest = resumed.prepare_same_owner_retarget(
        _rce_retarget_intent(
            resumed_old,
            deadline=predecessor.deadline,
            floor=25.0,
            power=50.0,
        ),
        replace(resumed_old, ems_observed_at=NOW + timedelta(seconds=6)),
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    )
    assert prepared_latest is not None
    assert resumed.cancel_unsent_same_owner_retarget(
        predecessor,
        replace(resumed_old, ems_observed_at=NOW + timedelta(seconds=7)),
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=7),
    )
    assert resumed.record.state is ActiveState.EXECUTING
    assert resumed.record.owner is ExecutionOwner.RCE
    assert resumed.record.transaction is not None
    assert resumed.record.transaction.intent == predecessor.intent
    assert resumed.record.transaction.command_snapshot == resumed_baseline
    assert resumed.record.transaction.physical_verification == (
        predecessor.physical_verification
    )

    rejected, _rejected_baseline, rejected_old = _begin_rce_until_executing()
    rejected_predecessor = rejected.record.transaction
    assert rejected_predecessor is not None
    assert rejected.prepare_same_owner_retarget(
        _rce_retarget_intent(
            rejected_old,
            deadline=rejected_predecessor.deadline,
        ),
        replace(rejected_old, ems_observed_at=NOW + timedelta(seconds=6)),
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    ) is not None
    foreign = replace(
        rejected_old,
        ems_block=replace(
            rejected_old.ems_block,
            force_discharge_soc_percent_4305=35.0,
        ),
        ems_generation=rejected_old.ems_generation + 1,
        ems_observed_at=NOW + timedelta(seconds=7),
    )
    assert not rejected.cancel_unsent_same_owner_retarget(
        rejected_predecessor,
        foreign,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=7),
    )
    assert rejected.record.state is ActiveState.STOPPING
    assert rejected.record.owner is ExecutionOwner.RCE

    machine, baseline, old_readback = _begin_rce_until_executing()
    transaction = machine.record.transaction
    assert transaction is not None
    original_deadline = transaction.deadline
    original_sent_at = transaction.command_sent_at
    retarget = _rce_retarget_intent(
        old_readback,
        deadline=original_deadline,
        floor=25.0,
        power=50.0,
    )
    envelope = machine.prepare_same_owner_retarget(
        retarget,
        replace(
            old_readback,
            ems_observed_at=NOW + timedelta(seconds=6),
        ),
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    )
    assert envelope is not None
    assert machine.record.state is ActiveState.RETARGETING
    prepared = machine.record.transaction
    assert prepared is not None
    assert prepared.command_snapshot == baseline
    assert prepared.deadline == original_deadline
    assert prepared.command_sent_at is None
    assert prepared.expected_readback is not None
    assert prepared.expected_readback.base_ems_generation == old_readback.ems_generation
    assert tuple(write.family for write in envelope.atomic_writes) == (
        AtomicWriteFamily.EMS_COMPLETE_BLOCK,
    )
    assert envelope.atomic_writes[0].ems_block == retarget.command.ems_block

    sent_at = NOW + timedelta(seconds=7)
    machine.mark_retarget_sent(envelope, sent_at=sent_at)
    assert machine.record.state is ActiveState.RETARGETING
    assert machine.record.transaction is not None
    assert machine.record.transaction.command_sent_at == sent_at
    pending = replace(
        old_readback,
        ems_observed_at=NOW + timedelta(seconds=8),
    )
    assert machine.waiting_same_owner_replan_hold_ready(
        pending,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=8),
    )
    assert machine.record.transaction.command_sent_at == sent_at
    assert machine.record.transaction.deadline == original_deadline
    assert machine.observe_readback(
        pending,
        now=NOW + timedelta(seconds=8),
    ) is ReadbackVerdict.PENDING
    assert machine.record.state is ActiveState.RETARGETING

    new_readback = _apply_command_readback(
        pending,
        envelope.command,
        observed_at=NOW + timedelta(seconds=9),
    )
    assert machine.observe_readback(
        new_readback,
        now=NOW + timedelta(seconds=9),
    ) is ReadbackVerdict.MATCH
    machine.observe_physical_verification(
        PhysicalVerification(
            transaction_id="tx:rce:0001",
            action=ExecutionAction.RCE_EXPORT,
            status=VerificationStatus.CONFIRMED,
            observed_at=NOW + timedelta(seconds=10),
            evidence=("retarget export", "retarget discharge"),
        ),
        now=NOW + timedelta(seconds=10),
    )
    assert machine.record.state is ActiveState.EXECUTING
    assert machine.record.transaction is not None
    assert machine.record.transaction.command_snapshot == baseline
    assert machine.record.transaction.intent == retarget
    assert original_sent_at != machine.record.transaction.command_sent_at

    machine.request_stop(reason=ExecutionReason.AUTHORIZATION_LOST)
    assert machine.record.state is ActiveState.STOPPING
    restore = machine.prepare_restore(
        new_readback,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=11),
    )
    assert restore is not None
    assert restore.command.ems_block == baseline.ems_block
    assert restore.command.ems_block != old_readback.ems_block
    assert restore.command.ems_block != new_readback.ems_block
    machine.mark_restore_sent(restore, sent_at=NOW + timedelta(seconds=12))
    restored = _apply_command_readback(
        new_readback,
        restore.command,
        observed_at=NOW + timedelta(seconds=13),
    )
    assert machine.observe_restore_readback(
        restored,
        now=NOW + timedelta(seconds=13),
    ) is ReadbackVerdict.MATCH
    assert machine.record.state is ActiveState.IDLE
    assert machine.record.owner is ExecutionOwner.NONE
    assert machine.record.last_transaction is not None
    assert machine.record.last_transaction.command_snapshot == baseline
    assert machine.record.last_transaction.rollback_status is RollbackStatus.CONFIRMED

    restart_machine, restart_baseline, restart_old = _begin_rce_until_executing()
    restart_tx = restart_machine.record.transaction
    assert restart_tx is not None
    restart_envelope = restart_machine.prepare_same_owner_retarget(
        _rce_retarget_intent(restart_old, deadline=restart_tx.deadline),
        replace(restart_old, ems_observed_at=NOW + timedelta(seconds=6)),
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    )
    assert restart_envelope is not None
    recovered = SupervisorExecutor.recover(restart_machine.record)
    assert recovered.record.state is ActiveState.STOPPING
    assert recovered.record.transaction is not None
    assert recovered.record.transaction.command_snapshot == restart_baseline
    assert recovered.record.transaction.restore_attempts == 0

    sent_restart_machine, sent_restart_baseline, sent_restart_old = (
        _begin_rce_until_executing()
    )
    sent_restart_tx = sent_restart_machine.record.transaction
    assert sent_restart_tx is not None
    sent_restart_envelope = sent_restart_machine.prepare_same_owner_retarget(
        _rce_retarget_intent(
            sent_restart_old,
            deadline=sent_restart_tx.deadline,
        ),
        replace(sent_restart_old, ems_observed_at=NOW + timedelta(seconds=6)),
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    )
    assert sent_restart_envelope is not None
    sent_restart_machine.mark_retarget_sent(
        sent_restart_envelope,
        sent_at=NOW + timedelta(seconds=7),
    )
    sent_recovered = SupervisorExecutor.recover(sent_restart_machine.record)
    assert sent_recovered.record.state is ActiveState.STOPPING
    assert sent_recovered.record.transaction is not None
    assert sent_recovered.record.transaction.command_snapshot == sent_restart_baseline
    assert sent_recovered.record.transaction.restore_attempts == 0

    foreign_machine, _base, foreign_old = _begin_rce_until_executing()
    foreign_tx = foreign_machine.record.transaction
    assert foreign_tx is not None
    foreign_envelope = foreign_machine.prepare_same_owner_retarget(
        _rce_retarget_intent(foreign_old, deadline=foreign_tx.deadline),
        replace(foreign_old, ems_observed_at=NOW + timedelta(seconds=6)),
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=6),
    )
    assert foreign_envelope is not None
    foreign_machine.mark_retarget_sent(
        foreign_envelope,
        sent_at=NOW + timedelta(seconds=7),
    )
    foreign = replace(
        foreign_old,
        ems_block=replace(foreign_old.ems_block, force_discharge_soc_percent_4305=35.0),
        ems_observed_at=NOW + timedelta(seconds=8),
    )
    assert not foreign_machine.waiting_same_owner_replan_hold_ready(
        foreign,
        _gates(ExecutionOwner.RCE),
        now=NOW + timedelta(seconds=8),
    )
    assert foreign_machine.record.state is ActiveState.STOPPING
    assert foreign_machine.record.reason is ExecutionReason.SNAPSHOT_CHANGED

    invalid_machine, _base, invalid_old = _begin_rce_until_executing()
    invalid_tx = invalid_machine.record.transaction
    assert invalid_tx is not None
    _raises(
        ValueError,
        lambda: invalid_machine.prepare_same_owner_retarget(
            replace(
                _rce_retarget_intent(invalid_old, deadline=invalid_tx.deadline),
                deadline=invalid_tx.deadline - timedelta(seconds=1),
            ),
            replace(invalid_old, ems_observed_at=NOW + timedelta(seconds=6)),
            _gates(ExecutionOwner.RCE),
            now=NOW + timedelta(seconds=6),
        ),
    )


def main() -> None:
    tests = (
        _test_complete_block_transaction_and_rollback,
        _test_confirmed_rce_continuation_physical_window,
        _test_generation_bound_prewrite_and_bounded_restore_retry,
        _test_migrated_v1_rce_retry_accepts_only_confirmed_original_intent,
        _test_direct_atomic_families_and_touched_generations,
        _test_action_local_readiness_through_transaction_and_restore,
        _test_touched_family_and_ems_remain_fail_closed,
        _test_prewrite_zero_export_recheck_releases_owner_without_write,
        _test_off_grid_blocks_normal_start,
        _test_restart_recovery_never_assumes_success,
        _test_unknown_restore_outcome_keeps_owner_for_recovery,
        _test_current_restore_target_finishes_without_dispatch,
        _test_physical_generation_rollover_is_newer,
        _test_ack_timeouts_freshness_and_pending_idempotence,
        _test_tariff_physical_requires_acknowledged_grid_charge_readback,
        _test_master_stop_preserves_physical_off_grid,
        _test_master_stop_gates_actual_restore_families,
        _test_master_stop_preempts_authorized_manual_cycle_only,
        _test_latched_block_survives_restart_and_register_ranges_are_exact,
        _test_same_window_rce_retarget_preserves_baseline_and_fresh_proof,
    )
    for test in tests:
        test()
    print(f"Supervisor executor contract tests passed: {len(tests)}")


if __name__ == "__main__":
    main()
