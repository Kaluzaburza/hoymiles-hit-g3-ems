"""Offline adversarial tests for the strict Supervisor executor codec."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components" / "hoymiles_hit_modbus"))

from supervisor_executor import (  # noqa: E402
    ActiveState,
    ActuatorIntent,
    CommandSet,
    EmsBlock,
    EmsMode,
    ExecutionAction,
    ExecutionOwner,
    ExecutionReason,
    ExecutorRecord,
    ExpectedReadback,
    MasterStopResult,
    MasterStopStatus,
    PhysicalVerification,
    RollbackStatus,
    SettingsSnapshot,
    SupervisorExecutor,
    TransactionRecord,
    VerificationStatus,
)
from supervisor_executor_codec import (  # noqa: E402
    MAX_RECORD_JSON_BYTES,
    SCHEMA_VERSION,
    record_from_dict,
    record_to_dict,
)


NOW = datetime(2026, 8, 31, 10, 0, 0, 123456, tzinfo=timezone.utc)


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
    block: EmsBlock | None = None,
    ems_generation: int = 101,
    gcf_generation: int = 202,
    battery_generation: int = 303,
) -> SettingsSnapshot:
    return SettingsSnapshot(
        ems_block=block or _block(),
        ems_generation=ems_generation,
        ems_observed_at=at,
        ems_coherent=True,
        gcf_enabled_258=True,
        export_limit_percent_259=42.5,
        gcf_generation=gcf_generation,
        gcf_observed_at=at + timedelta(microseconds=1),
        gcf_coherent=True,
        battery_max_charge_power_percent_306=55.5,
        battery_generation=battery_generation,
        battery_observed_at=at + timedelta(microseconds=2),
        battery_coherent=True,
    )


def _command() -> CommandSet:
    return CommandSet(
        ems_block=replace(
            _block(),
            mode=EmsMode.GRID_CHARGE,
            force_charge_soc_percent_4303=98.0,
            maximum_charge_power_percent_4304=40.5,
        ),
        export_limit_percent_259=25.0,
        battery_max_charge_power_percent_306=45.5,
    )


def _intent() -> ActuatorIntent:
    return ActuatorIntent(
        policy=ExecutionOwner.TARIFF,
        action=ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        command=_command(),
        candidate_revision="tariff:codec:fixture:0001",
        deadline=NOW + timedelta(minutes=10),
    )


def _rich_transaction(
    state: ActiveState,
    owner: ExecutionOwner,
) -> TransactionRecord:
    intent = _intent()
    snapshot = _snapshot()
    expected = ExpectedReadback.from_command(snapshot, intent.command)
    changed = replace(
        snapshot,
        ems_block=intent.command.ems_block,
        export_limit_percent_259=intent.command.export_limit_percent_259,
        battery_max_charge_power_percent_306=(
            intent.command.battery_max_charge_power_percent_306
        ),
        ems_generation=102,
        gcf_generation=203,
        battery_generation=304,
        ems_observed_at=NOW + timedelta(seconds=3),
        gcf_observed_at=NOW + timedelta(seconds=3, microseconds=1),
        battery_observed_at=NOW + timedelta(seconds=3, microseconds=2),
    )
    restore_command = CommandSet(
        ems_block=snapshot.ems_block,
        export_limit_percent_259=snapshot.export_limit_percent_259,
        battery_max_charge_power_percent_306=(
            snapshot.battery_max_charge_power_percent_306
        ),
    )
    restore_expected = ExpectedReadback.from_command(changed, restore_command)
    verification = PhysicalVerification(
        transaction_id="tx:codec:0001",
        action=intent.action,
        status=VerificationStatus.CONFIRMED,
        observed_at=NOW + timedelta(seconds=4),
        evidence=("fresh grid import", "fresh grid-to-battery power"),
    )
    return TransactionRecord(
        transaction_id="tx:codec:0001",
        state=state,
        intent=intent,
        owner=owner,
        started_at=NOW,
        deadline=intent.deadline,
        reason=(
            ExecutionReason.PHYSICALLY_CONFIRMED
            if state is ActiveState.EXECUTING
            else ExecutionReason.RESTORING
            if state is ActiveState.RESTORING
            else ExecutionReason.WAITING_READBACK
            if state is ActiveState.WAITING_READBACK
            else ExecutionReason.CANDIDATE_SELECTED
        ),
        command_snapshot=snapshot,
        prewrite_snapshot=snapshot,
        expected_readback=expected,
        command_sent_at=NOW + timedelta(seconds=2),
        readback_result=(
            VerificationStatus.CONFIRMED
            if state is ActiveState.EXECUTING
            else VerificationStatus.PENDING
        ),
        physical_verification=(
            verification if state is ActiveState.EXECUTING else None
        ),
        rollback_status=(
            RollbackStatus.PENDING
            if state in {ActiveState.STOPPING, ActiveState.RESTORING, ActiveState.FAULT}
            else RollbackStatus.NOT_REQUIRED
        ),
        rollback_result=VerificationStatus.PENDING,
        restore_command=restore_command,
        restore_snapshot=changed,
        restore_expected_readback=restore_expected,
        restore_sent_at=NOW + timedelta(seconds=7),
        restore_attempts=1,
        master_stop_requested=True,
        off_grid_preserved=False,
    )


def _record_for_state(state: ActiveState) -> ExecutorRecord:
    terminal = replace(
        _rich_transaction(ActiveState.IDLE, ExecutionOwner.NONE),
        rollback_status=RollbackStatus.CONFIRMED,
        rollback_result=VerificationStatus.CONFIRMED,
    )
    if state is ActiveState.IDLE:
        return ExecutorRecord(
            state=ActiveState.IDLE,
            owner=ExecutionOwner.NONE,
            transaction=None,
            last_transaction=terminal,
            starts_allowed=False,
            automatic_policies_enabled=False,
            reason=ExecutionReason.OFF_GRID_PRESERVED,
            master_stop_result=MasterStopResult(
                status=MasterStopStatus.COMPLETED,
                transaction_id="tx:stop:0001",
                requested_at=NOW,
                completed_at=NOW + timedelta(seconds=9),
                off_grid_preserved=True,
                reason=ExecutionReason.OFF_GRID_PRESERVED,
            ),
            revision=9,
        )
    owner = (
        ExecutionOwner.NONE
        if state in {ActiveState.SELECTED, ActiveState.BLOCKED}
        else ExecutionOwner.RCE
        if state is ActiveState.RETARGETING
        else ExecutionOwner.TARIFF
    )
    transaction = _rich_transaction(state, owner)
    if state is ActiveState.RETARGETING:
        snapshot = _snapshot()
        prewrite = _snapshot(
            at=NOW + timedelta(seconds=1),
            block=replace(
                snapshot.ems_block,
                mode=EmsMode.GRID_DISCHARGE,
                force_discharge_soc_percent_4305=41.0,
                maximum_discharge_power_percent_4306=25.4,
            ),
            ems_generation=102,
        )
        intent = ActuatorIntent(
            policy=ExecutionOwner.RCE,
            action=ExecutionAction.RCE_EXPORT,
            command=CommandSet(
                ems_block=replace(
                    snapshot.ems_block,
                    mode=EmsMode.GRID_DISCHARGE,
                    force_discharge_soc_percent_4305=30.0,
                    maximum_discharge_power_percent_4306=50.0,
                )
            ),
            candidate_revision="rce:codec:retarget:0001",
            deadline=NOW + timedelta(minutes=10),
        )
        transaction = replace(
            transaction,
            intent=intent,
            owner=ExecutionOwner.RCE,
            deadline=intent.deadline,
            command_snapshot=snapshot,
            prewrite_snapshot=prewrite,
            expected_readback=ExpectedReadback.from_command(prewrite, intent.command),
            command_sent_at=NOW + timedelta(seconds=2),
            readback_result=VerificationStatus.PENDING,
            physical_verification=None,
            rollback_status=RollbackStatus.PENDING,
            restore_command=None,
            restore_snapshot=None,
            restore_expected_readback=None,
            restore_sent_at=None,
            restore_attempts=0,
            master_stop_requested=False,
            reason=ExecutionReason.WAITING_READBACK,
        )
    if state is ActiveState.SELECTED:
        transaction = replace(
            transaction,
            command_snapshot=None,
            prewrite_snapshot=None,
            expected_readback=None,
            command_sent_at=None,
            restore_command=None,
            restore_snapshot=None,
            restore_expected_readback=None,
            restore_sent_at=None,
            restore_attempts=0,
            master_stop_requested=False,
        )
    elif state is ActiveState.STARTING:
        transaction = replace(
            transaction,
            command_sent_at=None,
            restore_command=None,
            restore_snapshot=None,
            restore_expected_readback=None,
            restore_sent_at=None,
            restore_attempts=0,
            master_stop_requested=False,
        )
    elif state is ActiveState.BLOCKED:
        transaction = replace(
            transaction,
            command_snapshot=None,
            prewrite_snapshot=None,
            expected_readback=None,
            command_sent_at=None,
            restore_command=None,
            restore_snapshot=None,
            restore_expected_readback=None,
            restore_sent_at=None,
            restore_attempts=0,
            master_stop_requested=False,
        )
    return ExecutorRecord(
        state=state,
        owner=owner,
        transaction=transaction,
        last_transaction=terminal,
        reason=transaction.reason,
        revision=17,
    )


def _test_exact_roundtrip_for_every_active_state() -> None:
    assert SCHEMA_VERSION == 4
    seen: set[ActiveState] = set()
    for state in ActiveState:
        original = _record_for_state(state)
        payload = record_to_dict(original)
        assert payload["schema_version"] == 4
        if state is ActiveState.RETARGETING:
            assert payload["record"]["state"] == ActiveState.WAITING_READBACK.value
            assert (
                payload["record"]["transaction"]["state"]
                == ActiveState.WAITING_READBACK.value
            )
        assert record_from_dict(payload) == original
        assert record_to_dict(record_from_dict(payload)) == payload
        rendered = json.dumps(payload, allow_nan=False, separators=(",", ":"))
        assert len(rendered.encode("utf-8")) <= MAX_RECORD_JSON_BYTES
        seen.add(record_from_dict(payload).state)
    assert seen == set(ActiveState)


def _test_retarget_alias_is_downgrade_safe_and_marker_strict() -> None:
    sent = _record_for_state(ActiveState.RETARGETING)
    assert sent.transaction is not None
    sent_payload = record_to_dict(sent)
    assert sent_payload["record"]["state"] == ActiveState.WAITING_READBACK.value
    assert (
        sent_payload["record"]["transaction"]["state"]
        == ActiveState.WAITING_READBACK.value
    )
    assert record_from_dict(sent_payload) == sent

    stale_generation_transaction = replace(
        sent.transaction,
        reason=ExecutionReason.READBACK_PENDING,
    )
    stale_generation = replace(
        sent,
        transaction=stale_generation_transaction,
        reason=ExecutionReason.READBACK_PENDING,
    )
    stale_generation_payload = record_to_dict(stale_generation)
    assert (
        stale_generation_payload["record"]["state"]
        == ActiveState.WAITING_READBACK.value
    )
    assert record_from_dict(stale_generation_payload) == stale_generation

    unsent_transaction = replace(
        sent.transaction,
        command_sent_at=None,
        reason=ExecutionReason.COMMAND_READY,
    )
    unsent = replace(
        sent,
        transaction=unsent_transaction,
        reason=ExecutionReason.COMMAND_READY,
    )
    unsent_payload = record_to_dict(unsent)
    assert unsent_payload["record"]["state"] == ActiveState.STARTING.value
    assert (
        unsent_payload["record"]["transaction"]["state"]
        == ActiveState.STARTING.value
    )
    assert record_from_dict(unsent_payload) == unsent

    pending_physical_transaction = replace(
        sent.transaction,
        readback_result=VerificationStatus.CONFIRMED,
        physical_verification=PhysicalVerification(
            transaction_id=sent.transaction.transaction_id,
            action=sent.transaction.intent.action,
            status=VerificationStatus.PENDING,
            observed_at=NOW + timedelta(seconds=3),
            evidence=("flow cohort pending",),
        ),
        reason=ExecutionReason.PHYSICAL_PENDING,
    )
    pending_physical = replace(
        sent,
        transaction=pending_physical_transaction,
        reason=ExecutionReason.PHYSICAL_PENDING,
    )
    pending_payload = record_to_dict(pending_physical)
    assert pending_payload["record"]["state"] == ActiveState.WAITING_READBACK.value
    assert record_from_dict(pending_payload) == pending_physical

    # The previous executor sees only states from its existing enum and
    # therefore recovers either phase by restoring the original baseline.
    for internal, alias in (
        (unsent, ActiveState.STARTING),
        (sent, ActiveState.WAITING_READBACK),
    ):
        assert internal.transaction is not None
        legacy_transaction = replace(internal.transaction, state=alias)
        legacy = replace(internal, state=alias, transaction=legacy_transaction)
        SupervisorExecutor(legacy)
        recovered = SupervisorExecutor.recover(legacy).record
        assert recovered.state is ActiveState.STOPPING
        assert recovered.owner is ExecutionOwner.RCE
        assert recovered.transaction is not None
        assert (
            recovered.transaction.command_snapshot
            == internal.transaction.command_snapshot
        )
        assert recovered.transaction.restore_attempts == 0

    # A look-alike old STARTING record without the rollback-pending marker is
    # not lifted.  This keeps ordinary old transactions semantically intact.
    weak_marker = deepcopy(unsent_payload)
    weak_marker["record"]["transaction"]["rollback_status"] = (
        RollbackStatus.NOT_REQUIRED.value
    )
    decoded_weak = record_from_dict(weak_marker)
    assert decoded_weak.state is ActiveState.STARTING
    assert decoded_weak.transaction is not None
    assert decoded_weak.transaction.state is ActiveState.STARTING


def _test_canonical_utc_and_all_nested_fields() -> None:
    original = _record_for_state(ActiveState.RESTORING)
    payload = record_to_dict(original)
    transaction = payload["record"]["transaction"]
    assert transaction["started_at"] == "2026-08-31T10:00:00.123456Z"
    assert transaction["command_snapshot"]["gcf_observed_at"].endswith("Z")
    assert transaction["physical_verification"] is None
    assert transaction["restore_expected_readback"]["written_families"] == sorted(
        transaction["restore_expected_readback"]["written_families"]
    )
    restored = record_from_dict(payload)
    assert restored.transaction is not None
    assert restored.transaction.restore_command is not None
    assert restored.transaction.restore_snapshot is not None
    assert restored.transaction.restore_expected_readback is not None
    assert restored.transaction.restore_sent_at is not None
    assert restored.transaction.restore_attempts == 1
    assert restored.transaction.started_at.tzinfo is timezone.utc


def _test_restart_fixture_recovers_fail_closed() -> None:
    persisted = _record_for_state(ActiveState.WAITING_READBACK)
    reloaded = record_from_dict(record_to_dict(persisted))
    recovered = SupervisorExecutor.recover(reloaded).record
    assert recovered.state is ActiveState.STOPPING
    assert recovered.owner is ExecutionOwner.TARIFF
    assert recovered.reason is ExecutionReason.RESTART_RECOVERY
    assert recovered.transaction is not None
    assert recovered.transaction.rollback_status is RollbackStatus.PENDING
    assert record_from_dict(record_to_dict(recovered)) == recovered


def _test_roundtrip_preserves_explicit_unavailable_direct_cohorts() -> None:
    partial = replace(
        _snapshot(),
        gcf_enabled_258=None,
        export_limit_percent_259=None,
        gcf_generation=None,
        gcf_observed_at=None,
        gcf_coherent=False,
        battery_max_charge_power_percent_306=None,
        battery_generation=None,
        battery_observed_at=None,
        battery_coherent=False,
    )
    intent = ActuatorIntent(
        policy=ExecutionOwner.TARIFF,
        action=ExecutionAction.TARIFF_BATTERY_CHARGE,
        command=CommandSet(
            ems_block=replace(
                partial.ems_block,
                mode=EmsMode.GRID_CHARGE,
                force_charge_soc_percent_4303=98.0,
                maximum_charge_power_percent_4304=40.5,
            )
        ),
        candidate_revision="tariff:codec:partial:0001",
        deadline=NOW + timedelta(minutes=10),
    )
    original = _record_for_state(ActiveState.STARTING)
    assert original.transaction is not None
    transaction = replace(
        original.transaction,
        intent=intent,
        command_snapshot=partial,
        prewrite_snapshot=partial,
        expected_readback=ExpectedReadback.from_command(partial, intent.command),
    )
    original = replace(original, transaction=transaction)
    payload = record_to_dict(original)
    snapshot = payload["record"]["transaction"]["command_snapshot"]
    expected = payload["record"]["transaction"]["expected_readback"]
    assert snapshot["gcf_enabled_258"] is None
    assert snapshot["battery_generation"] is None
    assert expected["base_gcf_generation"] is None
    assert expected["base_battery_generation"] is None
    assert record_from_dict(payload) == original

    incomplete = deepcopy(payload)
    incomplete["record"]["transaction"]["command_snapshot"][
        "gcf_enabled_258"
    ] = False
    _raises(ValueError, lambda: record_from_dict(incomplete))


def _schema_v1_payload(record: ExecutorRecord) -> dict[str, object]:
    payload = deepcopy(record_to_dict(record))
    payload["schema_version"] = 1
    encoded_record = payload["record"]
    assert type(encoded_record) is dict
    for key in ("transaction", "last_transaction"):
        transaction = encoded_record[key]
        if type(transaction) is dict:
            for field in (
                "restore_attempts", "restore_not_queued_count",
                "restore_not_queued_first_at",
                "lease_identity", "terminal_epoch", "interruption_reason",
            ):
                del transaction[field]
    return payload


def _test_schema_v1_migrates_restore_attempts_conservatively() -> None:
    no_restore = _record_for_state(ActiveState.SELECTED)
    migrated_no_restore = record_from_dict(_schema_v1_payload(no_restore))
    assert migrated_no_restore.transaction is not None
    assert migrated_no_restore.transaction.restore_sent_at is None
    assert migrated_no_restore.transaction.restore_attempts == 0

    sent_restore = _record_for_state(ActiveState.RESTORING)
    migrated_sent_restore = record_from_dict(_schema_v1_payload(sent_restore))
    assert migrated_sent_restore.transaction is not None
    assert migrated_sent_restore.transaction.restore_sent_at is not None
    assert migrated_sent_restore.transaction.restore_attempts == 1

    prepared_restore = _record_for_state(ActiveState.STOPPING)
    assert prepared_restore.transaction is not None
    prepared_transaction = replace(
        prepared_restore.transaction,
        reason=ExecutionReason.RESTORING,
        restore_sent_at=None,
        restore_attempts=1,
    )
    prepared_restore = replace(
        prepared_restore,
        transaction=prepared_transaction,
        reason=ExecutionReason.RESTORING,
    )
    prepared_payload = _schema_v1_payload(prepared_restore)
    prepared_payload_transaction = prepared_payload["record"]["transaction"]
    assert prepared_payload_transaction["restore_command"] is not None
    assert prepared_payload_transaction["restore_sent_at"] is None
    migrated_prepared_restore = record_from_dict(prepared_payload)
    assert migrated_prepared_restore.transaction is not None
    assert migrated_prepared_restore.transaction.restore_attempts == 1

    unknown_restore = _record_for_state(ActiveState.FAULT)
    assert unknown_restore.transaction is not None
    unknown_transaction = replace(
        unknown_restore.transaction,
        reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
        rollback_status=RollbackStatus.PENDING,
        rollback_result=VerificationStatus.PENDING,
        restore_sent_at=None,
        restore_attempts=1,
    )
    unknown_restore = replace(
        unknown_restore,
        transaction=unknown_transaction,
        reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
    )
    unknown_payload = _schema_v1_payload(unknown_restore)
    unknown_payload_transaction = unknown_payload["record"]["transaction"]
    assert unknown_payload_transaction["restore_command"] is not None
    assert unknown_payload_transaction["restore_sent_at"] is None
    assert unknown_payload_transaction["rollback_status"] == "pending"
    migrated_unknown_restore = record_from_dict(unknown_payload)
    assert migrated_unknown_restore.transaction is not None
    assert migrated_unknown_restore.transaction.restore_attempts == 1

    failed_restore = _record_for_state(ActiveState.FAULT)
    assert failed_restore.transaction is not None
    failed_transaction = replace(
        failed_restore.transaction,
        reason=ExecutionReason.ROLLBACK_FAILED,
        rollback_status=RollbackStatus.FAILED,
        rollback_result=VerificationStatus.CONTRADICTED,
        restore_attempts=1,
    )
    failed_restore = replace(
        failed_restore,
        transaction=failed_transaction,
        reason=ExecutionReason.ROLLBACK_FAILED,
    )
    failed_payload = _schema_v1_payload(failed_restore)
    migrated_failed_restore = record_from_dict(failed_payload)
    assert migrated_failed_restore.transaction is not None
    assert migrated_failed_restore.transaction.restore_attempts == 1

    # Schema v1 remains closed: the v2-only field is not tolerated early.
    unexpected_v2_field = deepcopy(failed_payload)
    unexpected_v2_field["record"]["transaction"]["restore_attempts"] = 1
    _raises(ValueError, lambda: record_from_dict(unexpected_v2_field))

    # A migrated record is emitted in the current schema on its next save.
    upgraded = record_to_dict(migrated_sent_restore)
    assert upgraded["schema_version"] == 4
    assert upgraded["record"]["transaction"]["restore_attempts"] == 1


def _test_schema_v3_persists_restore_admission_budget() -> None:
    original = _record_for_state(ActiveState.FAULT)
    assert original.transaction is not None
    transaction = replace(
        original.transaction,
        restore_not_queued_count=1,
        restore_not_queued_first_at=NOW,
    )
    pending = replace(original, transaction=transaction)
    payload = record_to_dict(pending)
    assert payload["schema_version"] == 4
    assert payload["record"]["transaction"]["restore_not_queued_count"] == 1
    assert record_from_dict(payload) == pending

    baseline = record_to_dict(original)
    baseline["schema_version"] = 2
    for key in ("transaction", "last_transaction"):
        encoded = baseline["record"][key]
        if encoded is not None:
            del encoded["restore_not_queued_count"]
            del encoded["restore_not_queued_first_at"]
            del encoded["lease_identity"]
            del encoded["terminal_epoch"]
            del encoded["interruption_reason"]
    migrated = record_from_dict(baseline)
    assert migrated.transaction is not None
    assert migrated.transaction.restore_not_queued_count == 0
    assert migrated.transaction.restore_not_queued_first_at is None
    assert record_to_dict(migrated)["schema_version"] == 4

    v3 = deepcopy(payload)
    v3["schema_version"] = 3
    for key in ("transaction", "last_transaction"):
        encoded = v3["record"][key]
        if encoded is not None:
            del encoded["lease_identity"]
            del encoded["terminal_epoch"]
            del encoded["interruption_reason"]
    assert record_from_dict(v3).transaction.lease_identity is None

    missing = deepcopy(payload)
    del missing["record"]["transaction"]["restore_not_queued_count"]
    _raises(ValueError, lambda: record_from_dict(missing))
    unpaired = deepcopy(payload)
    unpaired["record"]["transaction"]["restore_not_queued_first_at"] = None
    _raises(ValueError, lambda: record_from_dict(unpaired))


def _test_rejects_schema_drift_and_malformed_values() -> None:
    baseline = record_to_dict(_record_for_state(ActiveState.EXECUTING))

    cases: list[dict[str, object]] = []

    missing_top = deepcopy(baseline)
    del missing_top["schema_version"]
    cases.append(missing_top)

    extra_top = deepcopy(baseline)
    extra_top["future"] = False
    cases.append(extra_top)

    wrong_version = deepcopy(baseline)
    wrong_version["schema_version"] = 5
    cases.append(wrong_version)

    missing_attempts = deepcopy(baseline)
    del missing_attempts["record"]["transaction"]["restore_attempts"]
    cases.append(missing_attempts)

    boolean_attempts = deepcopy(baseline)
    boolean_attempts["record"]["transaction"]["restore_attempts"] = True
    cases.append(boolean_attempts)

    float_attempts = deepcopy(baseline)
    float_attempts["record"]["transaction"]["restore_attempts"] = 1.0
    cases.append(float_attempts)

    excessive_attempts = deepcopy(baseline)
    excessive_attempts["record"]["transaction"]["restore_attempts"] = 3
    cases.append(excessive_attempts)

    negative_attempts = deepcopy(baseline)
    negative_attempts["record"]["transaction"]["restore_attempts"] = -1
    cases.append(negative_attempts)

    missing_nested = deepcopy(baseline)
    del missing_nested["record"]["transaction"]["deadline"]
    cases.append(missing_nested)

    extra_nested = deepcopy(baseline)
    extra_nested["record"]["transaction"]["intent"]["shadow"] = True
    cases.append(extra_nested)

    unknown_state = deepcopy(baseline)
    unknown_state["record"]["state"] = "active_future"
    cases.append(unknown_state)

    unknown_action = deepcopy(baseline)
    unknown_action["record"]["transaction"]["intent"]["action"] = "future_action"
    cases.append(unknown_action)

    offset_datetime = deepcopy(baseline)
    offset_datetime["record"]["transaction"]["started_at"] = (
        "2026-08-31T12:00:00.123456+02:00"
    )
    cases.append(offset_datetime)

    imprecise_datetime = deepcopy(baseline)
    imprecise_datetime["record"]["transaction"]["started_at"] = (
        "2026-08-31T10:00:00Z"
    )
    cases.append(imprecise_datetime)

    boolean_revision = deepcopy(baseline)
    boolean_revision["record"]["revision"] = True
    cases.append(boolean_revision)

    nonfinite = deepcopy(baseline)
    nonfinite["record"]["transaction"]["command_snapshot"][
        "export_limit_percent_259"
    ] = float("nan")
    cases.append(nonfinite)

    duplicate_family = deepcopy(baseline)
    families = duplicate_family["record"]["transaction"]["expected_readback"][
        "written_families"
    ]
    families.append(families[0])
    cases.append(duplicate_family)

    noncanonical_family_order = deepcopy(baseline)
    families = noncanonical_family_order["record"]["transaction"][
        "expected_readback"
    ]["written_families"]
    families.reverse()
    cases.append(noncanonical_family_order)

    wrong_physical_transaction = deepcopy(baseline)
    wrong_physical_transaction["record"]["transaction"]["physical_verification"][
        "transaction_id"
    ] = "tx:other:0001"
    cases.append(wrong_physical_transaction)

    for payload in cases:
        _raises(ValueError, lambda payload=payload: record_from_dict(payload))


def _test_every_object_is_closed_to_missing_and_extra_keys() -> None:
    payload = record_to_dict(_record_for_state(ActiveState.EXECUTING))

    def objects(value: object) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        if type(value) is dict:
            found.append(value)
            for child in value.values():
                found.extend(objects(child))
        elif type(value) is list:
            for child in value:
                found.extend(objects(child))
        return found

    closed_objects = objects(payload)
    assert len(closed_objects) >= 20
    for item in closed_objects:
        for key in tuple(item):
            original = item.pop(key)
            _raises(ValueError, lambda: record_from_dict(payload))
            item[key] = original
        item["__unknown_schema_field__"] = None
        _raises(ValueError, lambda: record_from_dict(payload))
        del item["__unknown_schema_field__"]


def _test_rejects_unbounded_and_invalid_outbound_records() -> None:
    oversized = record_to_dict(_record_for_state(ActiveState.EXECUTING))
    oversized["record"]["transaction"]["physical_verification"]["evidence"][0] = (
        "x" * MAX_RECORD_JSON_BYTES
    )
    _raises(ValueError, lambda: record_from_dict(oversized))

    valid = _record_for_state(ActiveState.SELECTED)
    assert valid.transaction is not None
    naive_transaction = replace(valid.transaction, started_at=NOW.replace(tzinfo=None))
    _raises(
        ValueError,
        lambda: record_to_dict(replace(valid, transaction=naive_transaction)),
    )

    _raises(ValueError, lambda: record_to_dict(object()))  # type: ignore[arg-type]


def main() -> int:
    _test_exact_roundtrip_for_every_active_state()
    _test_retarget_alias_is_downgrade_safe_and_marker_strict()
    _test_canonical_utc_and_all_nested_fields()
    _test_restart_fixture_recovers_fail_closed()
    _test_roundtrip_preserves_explicit_unavailable_direct_cohorts()
    _test_schema_v1_migrates_restore_attempts_conservatively()
    _test_schema_v3_persists_restore_admission_budget()
    _test_rejects_schema_drift_and_malformed_values()
    _test_every_object_is_closed_to_missing_and_extra_keys()
    _test_rejects_unbounded_and_invalid_outbound_records()
    print("Supervisor executor persistence codec: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
