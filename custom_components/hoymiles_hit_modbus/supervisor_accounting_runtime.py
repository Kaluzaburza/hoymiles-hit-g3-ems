"""Pure Active-runtime adapter for EMS Supervisor accounting epoch v2.

The adapter only normalizes already-persisted executor intent and independent
physical observations into the output-only ledger contract.  It grants no
execution authority and never reads or imports legacy accumulated energy.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

try:
    from .ems_supervisor import (
        OwnerKind,
        PhysicalMode,
        PolicyId,
        RequestedAction,
    )
    from .supervisor_active_bridge import authorization_matches
    from .supervisor_active_controller import ActiveFrame
    from .supervisor_executor import (
        ActiveState,
        ExecutionAction,
        ExecutionOwner,
        ExecutorRecord,
        VerificationStatus,
    )
    from .supervisor_ledger import (
        EvidenceSample,
        ExecutionIntent,
        ExecutionLedgerEntry,
        PhysicalExecutionEvidence,
        TariffActiveAction,
        build_execution_ledger_entry,
    )
except ImportError:  # Deterministic tools import component modules directly.
    from ems_supervisor import (  # type: ignore[no-redef]
        OwnerKind,
        PhysicalMode,
        PolicyId,
        RequestedAction,
    )
    from supervisor_active_bridge import authorization_matches  # type: ignore[no-redef]
    from supervisor_active_controller import ActiveFrame  # type: ignore[no-redef]
    from supervisor_executor import (  # type: ignore[no-redef]
        ActiveState,
        ExecutionAction,
        ExecutionOwner,
        ExecutorRecord,
        VerificationStatus,
    )
    from supervisor_ledger import (  # type: ignore[no-redef]
        EvidenceSample,
        ExecutionIntent,
        ExecutionLedgerEntry,
        PhysicalExecutionEvidence,
        TariffActiveAction,
        build_execution_ledger_entry,
    )


_ACTION_TO_REQUEST = {
    ExecutionAction.RCE_EXPORT: (PolicyId.RCE, RequestedAction.RCE_EXPORT),
    ExecutionAction.PV_CHARGE_HOLD: (PolicyId.RCE, RequestedAction.PV_CHARGE_HOLD),
    ExecutionAction.TARIFF_BATTERY_CHARGE: (
        PolicyId.TARIFF,
        RequestedAction.TARIFF_BATTERY_CHARGE,
    ),
    ExecutionAction.TARIFF_GRID_SUPPORT: (
        PolicyId.TARIFF,
        RequestedAction.TARIFF_GRID_SUPPORT,
    ),
    ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE: (
        PolicyId.TARIFF,
        RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
    ),
    ExecutionAction.RCM_ABSORB_PV: (PolicyId.RCM, RequestedAction.RCM_ABSORB_PV),
    ExecutionAction.RCM_LIMIT_EXPORT: (
        PolicyId.RCM,
        RequestedAction.RCM_LIMIT_EXPORT,
    ),
    ExecutionAction.RCM_ABSORB_AND_LIMIT: (
        PolicyId.RCM,
        RequestedAction.RCM_ABSORB_PV,
    ),
    ExecutionAction.RCM_PRE_DISCHARGE: (
        PolicyId.RCM,
        RequestedAction.RCM_PRE_DISCHARGE,
    ),
}

_OWNER_KIND = {
    ExecutionOwner.RCE: OwnerKind.RCE,
    ExecutionOwner.TARIFF: OwnerKind.TARIFF,
    ExecutionOwner.RCM: OwnerKind.RCM,
    ExecutionOwner.BALANCING: OwnerKind.BALANCING,
    ExecutionOwner.MANUAL: OwnerKind.MANUAL,
}

_TARIFF_ACTIVE_ACTION = {
    ExecutionAction.TARIFF_BATTERY_CHARGE: TariffActiveAction.BATTERY_CHARGE,
    ExecutionAction.TARIFF_GRID_SUPPORT: TariffActiveAction.GRID_SUPPORT,
    ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE: (
        TariffActiveAction.GRID_SUPPORT_AND_CHARGE
    ),
}

_SOURCE_FALLBACKS = {
    "ems_mode_readback": "sensor.unresolved_ems_mode_readback",
    "ems_generation": "sensor.unresolved_ems_readback_generation",
    "grid_to_battery_power": "provider_absent.grid_to_battery_power",
    "grid_power": "sensor.unresolved_grid_power",
}


def _source(source_entity_ids: Mapping[str, str | None], key: str) -> str:
    value = source_entity_ids.get(key)
    if isinstance(value, str) and value and value.isascii() and not any(
        character.isspace() for character in value
    ):
        return value[:160]
    return _SOURCE_FALLBACKS[key]


def _generation(value: object) -> int | None:
    if type(value) not in {int, float}:
        return None
    numeric = float(value)
    if not numeric.is_integer() or not 0 <= numeric <= 16_000_000:
        return None
    return int(numeric)


def _physical_mode(value: object) -> PhysicalMode | None:
    if type(value) not in {int, float}:
        return None
    return {
        0.0: PhysicalMode.SELF_USE,
        3.0: PhysicalMode.OFF_GRID,
        4.0: PhysicalMode.GRID_CHARGE,
        5.0: PhysicalMode.GRID_DISCHARGE,
    }.get(float(value))


def _intent(record: ExecutorRecord, frame: ActiveFrame) -> ExecutionIntent:
    transaction = record.transaction
    if transaction is None or transaction.intent.action not in _ACTION_TO_REQUEST:
        return ExecutionIntent(
            policy_id=PolicyId.TARIFF,
            requested_action=RequestedAction.NONE,
            active=False,
            owner_kind=OwnerKind.NONE,
            active_action=TariffActiveAction.NONE,
            intent_fingerprint=None,
        )

    action = transaction.intent.action
    policy_id, requested_action = _ACTION_TO_REQUEST[action]
    executing = record.state is ActiveState.EXECUTING
    authorized = bool(
        executing
        and transaction.owner is transaction.intent.policy
        and record.owner is transaction.owner
        and authorization_matches(
            transaction.intent,
            frame.decision,
            frame.candidates,
        )
    )
    fingerprint = None
    if authorized:
        for candidate in frame.candidates:
            if (
                candidate.policy_id is policy_id
                and candidate.requested_action is requested_action
                and candidate.candidate_revision
                == frame.decision.selected_candidate_revision
            ):
                fingerprint = candidate.desired_actuator_fingerprint
                break
    return ExecutionIntent(
        policy_id=policy_id,
        requested_action=requested_action,
        active=executing,
        owner_kind=_OWNER_KIND.get(record.owner, OwnerKind.NONE),
        active_action=(
            _TARIFF_ACTIVE_ACTION.get(action, TariffActiveAction.NONE)
            if executing
            else TariffActiveAction.NONE
        ),
        intent_fingerprint=fingerprint,
    )


def build_active_accounting_entry(
    record: ExecutorRecord,
    frame: ActiveFrame,
    source_entity_ids: Mapping[str, str | None],
) -> ExecutionLedgerEntry:
    """Build one fail-closed accounting-v2 entry from an Active frame."""

    if type(record) is not ExecutorRecord:
        raise ValueError("record must be an exact ExecutorRecord")
    if type(frame) is not ActiveFrame:
        raise ValueError("frame must be an exact ActiveFrame")
    if not isinstance(source_entity_ids, Mapping):
        raise ValueError("source_entity_ids must be a mapping")

    execution = frame.execution
    transaction = record.transaction
    readback_confirmed = bool(
        record.state is ActiveState.EXECUTING
        and transaction is not None
        and transaction.readback_result is VerificationStatus.CONFIRMED
        and transaction.expected_readback is not None
        and transaction.physical_verification is not None
        and transaction.physical_verification.status
        is VerificationStatus.CONFIRMED
    )
    evidence = PhysicalExecutionEvidence(
        observed_at=frame.now,
        mode_readback=EvidenceSample(
            value=_physical_mode(execution.physical_mode_code),
            reported_at=execution.full_block_generation_at,
            source=_source(source_entity_ids, "ems_mode_readback"),
        ),
        readback_generation=EvidenceSample(
            value=_generation(execution.full_block_generation),
            reported_at=execution.full_block_generation_at,
            source=_source(source_entity_ids, "ems_generation"),
        ),
        readback_confirmed=readback_confirmed,
        # There is no verified entry-local physical Grid-to-Battery provider
        # in this release.  Never promote the historical global entity id (or
        # a manually created entity with that id) into accounting authority.
        grid_to_battery_power_w=EvidenceSample(
            value=None,
            reported_at=None,
            source=_SOURCE_FALLBACKS["grid_to_battery_power"],
        ),
        grid_power_w=EvidenceSample(
            value=execution.grid_power_w,
            reported_at=execution.grid_power_observed_at,
            source=_source(source_entity_ids, "grid_power"),
        ),
    )
    return build_execution_ledger_entry(_intent(record, frame), evidence)


def entry_is_accounting_relevant(entry: ExecutionLedgerEntry) -> bool:
    """Return whether an entry belongs to a tariff charging intent."""

    return bool(
        entry.policy_id is PolicyId.TARIFF
        and entry.requested_action
        in {
            RequestedAction.TARIFF_BATTERY_CHARGE,
            RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        }
    )


__all__ = (
    "build_active_accounting_entry",
    "entry_is_accounting_relevant",
)
