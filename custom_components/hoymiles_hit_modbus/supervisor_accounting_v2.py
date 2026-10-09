"""Pure bounded accumulator for Supervisor tariff accounting v2.

The canonical execution ledger qualifies one instantaneous physical
Grid-to-Battery power sample.  This module is the separate persistence model
that integrates those samples into energy.  It has no Home Assistant, clock,
filesystem, network or actuator dependency.

Energy is added only between two consecutive, independently qualified ledger
entries.  The entries must retain the same exact tariff intent and physical
source cohort, use a non-regressing FC03 generation, carry different evidence
fingerprints and be no more than ``MAX_INTEGRATION_INTERVAL_SECONDS`` apart.
Any rejected observation clears the integration anchor, so a later valid
sample can only seed a new chain and cannot bridge an evidence gap.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any

try:
    from . import supervisor_ledger as ledger
except ImportError:  # Deterministic tools import component modules directly.
    import supervisor_ledger as ledger  # type: ignore[no-redef]


STORAGE_SCHEMA_VERSION = 1
STORAGE_EPOCH = 2
STORAGE_KEY = "hoymiles_hit_modbus.supervisor_accounting_v2"
ENTITY_UNIQUE_ID = "ems_supervisor_grid_to_battery_energy_v2"
ACCOUNTING_CONTRACT_ID = ledger.ACCOUNTING_CONTRACT_ID
LEGACY_V1_INVALID_REASON = "pv_grid_attribution_contaminated"

MAX_INTEGRATION_INTERVAL_SECONDS = 120.0
MAX_ACCOUNTING_POWER_W = 1_000_000.0
MAX_ACCUMULATED_ENERGY_KWH = 1_000_000_000.0
MAX_COUNTER_VALUE = 9_007_199_254_740_991
MAX_SERIALIZED_STATE_BYTES = 4096

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_PROVENANCE_FIELDS = frozenset(
    {
        "mode_readback",
        "readback_generation",
        "grid_to_battery_power_w",
        "grid_power_w",
    }
)


class AccountingUpdateReason(str, Enum):
    """Closed reason vocabulary for one accumulator transition."""

    INITIAL = "initial"
    SEEDED = "seeded_qualified_evidence"
    ACCUMULATED = "accumulated"
    ENTRY_NOT_QUALIFIED = "entry_not_qualified"
    MISSING_INTENT_FINGERPRINT = "missing_intent_fingerprint"
    INVALID_ATTRIBUTED_POWER = "invalid_attributed_power"
    EVIDENCE_COHORT_MISMATCH = "evidence_cohort_mismatch"
    NON_MONOTONIC_OBSERVATION = "non_monotonic_observation"
    INTERVAL_EXCEEDS_BOUND = "interval_exceeds_bound"
    INTENT_DISCONTINUITY = "intent_discontinuity"
    ACTION_DISCONTINUITY = "action_discontinuity"
    SOURCE_COHORT_DISCONTINUITY = "source_cohort_discontinuity"
    READBACK_GENERATION_REGRESSED = "readback_generation_regressed"
    DUPLICATE_EVIDENCE = "duplicate_evidence"
    ENERGY_BOUND_EXCEEDED = "energy_bound_exceeded"


@dataclass(frozen=True, slots=True)
class AccountingV2State:
    """Strict persisted state for the isolated accounting-v2 epoch."""

    storage_schema_version: int
    storage_epoch: int
    storage_key: str
    entity_unique_id: str
    accounting_contract_id: str
    accumulated_energy_kwh: float
    accepted_interval_count: int
    rejected_interval_count: int
    anchor_observed_at: datetime | None
    anchor_intent_fingerprint: str | None
    anchor_requested_action: ledger.RequestedAction | None
    anchor_active_action: ledger.TariffActiveAction | None
    anchor_readback_generation: int | None
    anchor_evidence_fingerprint: str | None
    anchor_source_cohort_fingerprint: str | None
    anchor_attributed_power_w: float
    delivered_power_feedback_w: float
    last_update_reason: AccountingUpdateReason
    legacy_v1_invalid_reason: str


@dataclass(frozen=True, slots=True)
class AccountingUpdate:
    """One deterministic transition and its non-persisted interval detail."""

    state: AccountingV2State
    accepted: bool
    reason: AccountingUpdateReason
    interval_seconds: float
    interval_energy_kwh: float
    attributed_power_w: float
    delivered_power_feedback_w: float


@dataclass(frozen=True, slots=True)
class _QualifiedContext:
    observed_at: datetime
    intent_fingerprint: str
    requested_action: ledger.RequestedAction
    active_action: ledger.TariffActiveAction
    readback_generation: int
    evidence_fingerprint: str
    source_cohort_fingerprint: str
    attributed_power_w: float


def _aware(value: Any) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    if not _aware(value):
        raise ValueError("Accounting timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: Any, *, minimum: float, maximum: float) -> float:
    if type(value) not in {int, float}:
        raise ValueError("Accounting number has an invalid type")
    normalized = float(value)
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ValueError("Accounting number is outside its bounded contract")
    return 0.0 if normalized == 0.0 else normalized


def _counter(value: Any) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER_VALUE:
        raise ValueError("Accounting counter is outside its bounded contract")
    return value


def _fingerprint(value: Any, *, optional: bool = False) -> str | None:
    if value is None and optional:
        return None
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError("Accounting fingerprint must be lowercase SHA-256")
    return value


def _parse_timestamp(value: Any, *, optional: bool = False) -> datetime | None:
    if value is None and optional:
        return None
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("Persisted accounting timestamp is not canonical UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as err:
        raise ValueError("Persisted accounting timestamp is invalid") from err
    if _timestamp(parsed) != value:
        raise ValueError("Persisted accounting timestamp is not canonical")
    return parsed


def _source_cohort_fingerprint(entry: ledger.ExecutionLedgerEntry) -> str | None:
    if type(entry.provenance) is not tuple or len(entry.provenance) != 4:
        return None
    fields: set[str] = set()
    timestamps: list[datetime] = []
    sources: list[tuple[str, str]] = []
    for item in entry.provenance:
        if type(item) is not ledger.EvidenceProvenance:
            return None
        if (
            item.field not in _PROVENANCE_FIELDS
            or item.field in fields
            or item.status is not ledger.EvidenceStatus.FRESH
            or type(item.source) is not str
            or not item.source
            or not item.source.isascii()
            or any(character.isspace() for character in item.source)
            or not _aware(item.reported_at)
        ):
            return None
        fields.add(item.field)
        timestamps.append(item.reported_at)
        sources.append((item.field, item.source))
    if fields != _PROVENANCE_FIELDS:
        return None
    observed_at = entry.observed_at.astimezone(timezone.utc)
    normalized = [value.astimezone(timezone.utc) for value in timestamps]
    if any(
        (observed_at - value).total_seconds()
        < -ledger.FUTURE_TOLERANCE_SECONDS
        or (observed_at - value).total_seconds()
        > ledger.MAX_EVIDENCE_AGE_SECONDS
        for value in normalized
    ):
        return None
    if (
        max(normalized) - min(normalized)
    ).total_seconds() > ledger.MAX_EVIDENCE_SKEW_SECONDS:
        return None
    serialized = json.dumps(
        sorted(sources),
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(serialized).hexdigest()


def _qualified_context(
    entry: ledger.ExecutionLedgerEntry,
) -> tuple[_QualifiedContext | None, AccountingUpdateReason]:
    if type(entry) is not ledger.ExecutionLedgerEntry:
        raise ValueError("entry must be an exact ExecutionLedgerEntry")
    if not _aware(entry.observed_at):
        return None, AccountingUpdateReason.EVIDENCE_COHORT_MISMATCH
    power = ledger.attributed_power_for_feedback(entry)
    if power == 0.0:
        return None, AccountingUpdateReason.ENTRY_NOT_QUALIFIED
    if not math.isfinite(power) or not 0.0 < power <= MAX_ACCOUNTING_POWER_W:
        return None, AccountingUpdateReason.INVALID_ATTRIBUTED_POWER
    if (
        entry.intent_fingerprint is None
        or _SHA256_RE.fullmatch(entry.intent_fingerprint) is None
    ):
        return None, AccountingUpdateReason.MISSING_INTENT_FINGERPRINT
    if (
        type(entry.readback_generation) is not int
        or not 0 <= entry.readback_generation <= ledger.MAX_READBACK_GENERATION
        or _SHA256_RE.fullmatch(entry.evidence_fingerprint) is None
    ):
        return None, AccountingUpdateReason.EVIDENCE_COHORT_MISMATCH
    source_fingerprint = _source_cohort_fingerprint(entry)
    if source_fingerprint is None:
        return None, AccountingUpdateReason.EVIDENCE_COHORT_MISMATCH
    return (
        _QualifiedContext(
            observed_at=entry.observed_at,
            intent_fingerprint=entry.intent_fingerprint,
            requested_action=entry.requested_action,
            active_action=entry.active_action,
            readback_generation=entry.readback_generation,
            evidence_fingerprint=entry.evidence_fingerprint,
            source_cohort_fingerprint=source_fingerprint,
            attributed_power_w=power,
        ),
        AccountingUpdateReason.SEEDED,
    )


def new_accounting_state() -> AccountingV2State:
    """Return a clean epoch-v2 state; no legacy value is imported."""

    return AccountingV2State(
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        storage_epoch=STORAGE_EPOCH,
        storage_key=STORAGE_KEY,
        entity_unique_id=ENTITY_UNIQUE_ID,
        accounting_contract_id=ACCOUNTING_CONTRACT_ID,
        accumulated_energy_kwh=0.0,
        accepted_interval_count=0,
        rejected_interval_count=0,
        anchor_observed_at=None,
        anchor_intent_fingerprint=None,
        anchor_requested_action=None,
        anchor_active_action=None,
        anchor_readback_generation=None,
        anchor_evidence_fingerprint=None,
        anchor_source_cohort_fingerprint=None,
        anchor_attributed_power_w=0.0,
        delivered_power_feedback_w=0.0,
        last_update_reason=AccountingUpdateReason.INITIAL,
        legacy_v1_invalid_reason=LEGACY_V1_INVALID_REASON,
    )


def _clear_anchor(
    state: AccountingV2State, reason: AccountingUpdateReason
) -> AccountingV2State:
    return replace(
        state,
        rejected_interval_count=min(
            state.rejected_interval_count + 1, MAX_COUNTER_VALUE
        ),
        anchor_observed_at=None,
        anchor_intent_fingerprint=None,
        anchor_requested_action=None,
        anchor_active_action=None,
        anchor_readback_generation=None,
        anchor_evidence_fingerprint=None,
        anchor_source_cohort_fingerprint=None,
        anchor_attributed_power_w=0.0,
        delivered_power_feedback_w=0.0,
        last_update_reason=reason,
    )


def _seed_anchor(
    state: AccountingV2State,
    context: _QualifiedContext,
    reason: AccountingUpdateReason,
) -> AccountingV2State:
    return replace(
        state,
        anchor_observed_at=context.observed_at,
        anchor_intent_fingerprint=context.intent_fingerprint,
        anchor_requested_action=context.requested_action,
        anchor_active_action=context.active_action,
        anchor_readback_generation=context.readback_generation,
        anchor_evidence_fingerprint=context.evidence_fingerprint,
        anchor_source_cohort_fingerprint=context.source_cohort_fingerprint,
        anchor_attributed_power_w=context.attributed_power_w,
        delivered_power_feedback_w=context.attributed_power_w,
        last_update_reason=reason,
    )


def _update(
    state: AccountingV2State,
    *,
    accepted: bool,
    reason: AccountingUpdateReason,
    interval_seconds: float = 0.0,
    interval_energy_kwh: float = 0.0,
    attributed_power_w: float = 0.0,
) -> AccountingUpdate:
    return AccountingUpdate(
        state=state,
        accepted=accepted,
        reason=reason,
        interval_seconds=interval_seconds,
        interval_energy_kwh=interval_energy_kwh,
        attributed_power_w=attributed_power_w,
        delivered_power_feedback_w=state.delivered_power_feedback_w,
    )


def accumulate_execution_entry(
    state: AccountingV2State,
    entry: ledger.ExecutionLedgerEntry,
) -> AccountingUpdate:
    """Apply one ledger entry without ever bridging a rejected observation."""

    # Validate persisted input first.  Runtime callers must never accumulate on
    # a hand-built or partially recovered state.
    accounting_state_from_dict(accounting_state_to_dict(state))
    context, entry_reason = _qualified_context(entry)
    if context is None:
        cleared = _clear_anchor(state, entry_reason)
        return _update(cleared, accepted=False, reason=entry_reason)

    if state.anchor_observed_at is None:
        seeded = _seed_anchor(state, context, AccountingUpdateReason.SEEDED)
        return _update(
            seeded,
            accepted=False,
            reason=AccountingUpdateReason.SEEDED,
            attributed_power_w=context.attributed_power_w,
        )

    interval_seconds = (
        context.observed_at.astimezone(timezone.utc)
        - state.anchor_observed_at.astimezone(timezone.utc)
    ).total_seconds()
    reason: AccountingUpdateReason | None = None
    if interval_seconds <= 0.0:
        reason = AccountingUpdateReason.NON_MONOTONIC_OBSERVATION
    elif interval_seconds > MAX_INTEGRATION_INTERVAL_SECONDS:
        reason = AccountingUpdateReason.INTERVAL_EXCEEDS_BOUND
    elif context.intent_fingerprint != state.anchor_intent_fingerprint:
        reason = AccountingUpdateReason.INTENT_DISCONTINUITY
    elif (
        context.requested_action is not state.anchor_requested_action
        or context.active_action is not state.anchor_active_action
    ):
        reason = AccountingUpdateReason.ACTION_DISCONTINUITY
    elif (
        context.source_cohort_fingerprint
        != state.anchor_source_cohort_fingerprint
    ):
        reason = AccountingUpdateReason.SOURCE_COHORT_DISCONTINUITY
    elif context.readback_generation < state.anchor_readback_generation:
        reason = AccountingUpdateReason.READBACK_GENERATION_REGRESSED
    elif context.evidence_fingerprint == state.anchor_evidence_fingerprint:
        reason = AccountingUpdateReason.DUPLICATE_EVIDENCE

    if reason is not None:
        cleared = _clear_anchor(state, reason)
        return _update(cleared, accepted=False, reason=reason)

    interval_energy_kwh = (
        (state.anchor_attributed_power_w + context.attributed_power_w)
        * 0.5
        * interval_seconds
        / 3_600_000.0
    )
    total = state.accumulated_energy_kwh + interval_energy_kwh
    if (
        not math.isfinite(interval_energy_kwh)
        or interval_energy_kwh < 0.0
        or not math.isfinite(total)
        or total > MAX_ACCUMULATED_ENERGY_KWH
    ):
        reason = AccountingUpdateReason.ENERGY_BOUND_EXCEEDED
        cleared = _clear_anchor(state, reason)
        return _update(cleared, accepted=False, reason=reason)

    accumulated = _seed_anchor(
        replace(
            state,
            accumulated_energy_kwh=0.0 if total == 0.0 else total,
            accepted_interval_count=min(
                state.accepted_interval_count + 1, MAX_COUNTER_VALUE
            ),
        ),
        context,
        AccountingUpdateReason.ACCUMULATED,
    )
    return _update(
        accumulated,
        accepted=True,
        reason=AccountingUpdateReason.ACCUMULATED,
        interval_seconds=interval_seconds,
        interval_energy_kwh=interval_energy_kwh,
        attributed_power_w=context.attributed_power_w,
    )


def legacy_v1_diagnostic() -> dict[str, Any]:
    """Return the only allowed projection of the contaminated legacy epoch."""

    return {
        "storage_epoch": 1,
        "included_in_v2": False,
        "invalid_reason": LEGACY_V1_INVALID_REASON,
    }


def accounting_state_to_dict(state: AccountingV2State) -> dict[str, Any]:
    """Return the exact, bounded persistence projection for epoch v2."""

    if type(state) is not AccountingV2State:
        raise ValueError("state must be an exact AccountingV2State")
    if state.anchor_requested_action is not None and not isinstance(
        state.anchor_requested_action, ledger.RequestedAction
    ):
        raise ValueError("Accounting requested action is invalid")
    if state.anchor_active_action is not None and not isinstance(
        state.anchor_active_action, ledger.TariffActiveAction
    ):
        raise ValueError("Accounting active action is invalid")
    if not isinstance(state.last_update_reason, AccountingUpdateReason):
        raise ValueError("Accounting update reason is invalid")
    payload = {
        "storage_schema_version": state.storage_schema_version,
        "storage_epoch": state.storage_epoch,
        "storage_key": state.storage_key,
        "entity_unique_id": state.entity_unique_id,
        "accounting_contract_id": state.accounting_contract_id,
        "accumulated_energy_kwh": state.accumulated_energy_kwh,
        "accepted_interval_count": state.accepted_interval_count,
        "rejected_interval_count": state.rejected_interval_count,
        "anchor_observed_at": _timestamp(state.anchor_observed_at),
        "anchor_intent_fingerprint": state.anchor_intent_fingerprint,
        "anchor_requested_action": (
            state.anchor_requested_action.value
            if state.anchor_requested_action is not None
            else None
        ),
        "anchor_active_action": (
            state.anchor_active_action.value
            if state.anchor_active_action is not None
            else None
        ),
        "anchor_readback_generation": state.anchor_readback_generation,
        "anchor_evidence_fingerprint": state.anchor_evidence_fingerprint,
        "anchor_source_cohort_fingerprint": (
            state.anchor_source_cohort_fingerprint
        ),
        "anchor_attributed_power_w": state.anchor_attributed_power_w,
        "delivered_power_feedback_w": state.delivered_power_feedback_w,
        "last_update_reason": state.last_update_reason.value,
        "legacy_v1_invalid_reason": state.legacy_v1_invalid_reason,
    }
    # The strict decoder is also the state invariant checker.
    _state_from_exact_payload(payload)
    serialized = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(serialized.encode("ascii")) > MAX_SERIALIZED_STATE_BYTES:
        raise ValueError("Accounting state exceeds its bounded persistence contract")
    return payload


_STATE_KEYS = frozenset(
    {
        "storage_schema_version",
        "storage_epoch",
        "storage_key",
        "entity_unique_id",
        "accounting_contract_id",
        "accumulated_energy_kwh",
        "accepted_interval_count",
        "rejected_interval_count",
        "anchor_observed_at",
        "anchor_intent_fingerprint",
        "anchor_requested_action",
        "anchor_active_action",
        "anchor_readback_generation",
        "anchor_evidence_fingerprint",
        "anchor_source_cohort_fingerprint",
        "anchor_attributed_power_w",
        "delivered_power_feedback_w",
        "last_update_reason",
        "legacy_v1_invalid_reason",
    }
)


def _state_from_exact_payload(payload: dict[str, Any]) -> AccountingV2State:
    if type(payload) is not dict or frozenset(payload) != _STATE_KEYS:
        raise ValueError("Accounting state has unknown or missing fields")
    if type(payload["storage_schema_version"]) is not int or (
        payload["storage_schema_version"] != STORAGE_SCHEMA_VERSION
    ):
        raise ValueError("Unsupported accounting storage schema version")
    if type(payload["storage_epoch"]) is not int or (
        payload["storage_epoch"] != STORAGE_EPOCH
    ):
        raise ValueError("Legacy or unknown accounting storage epoch")
    expected_strings = {
        "storage_key": STORAGE_KEY,
        "entity_unique_id": ENTITY_UNIQUE_ID,
        "accounting_contract_id": ACCOUNTING_CONTRACT_ID,
        "legacy_v1_invalid_reason": LEGACY_V1_INVALID_REASON,
    }
    for field, expected in expected_strings.items():
        if type(payload[field]) is not str or payload[field] != expected:
            raise ValueError(f"Accounting identity mismatch: {field}")

    accumulated = _number(
        payload["accumulated_energy_kwh"],
        minimum=0.0,
        maximum=MAX_ACCUMULATED_ENERGY_KWH,
    )
    accepted_count = _counter(payload["accepted_interval_count"])
    rejected_count = _counter(payload["rejected_interval_count"])
    if type(payload["last_update_reason"]) is not str:
        raise ValueError("Accounting update reason must be text")
    try:
        reason = AccountingUpdateReason(payload["last_update_reason"])
    except (TypeError, ValueError) as err:
        raise ValueError("Unknown accounting update reason") from err

    observed_at = _parse_timestamp(payload["anchor_observed_at"], optional=True)
    intent_fingerprint = _fingerprint(
        payload["anchor_intent_fingerprint"], optional=True
    )
    evidence_fingerprint = _fingerprint(
        payload["anchor_evidence_fingerprint"], optional=True
    )
    source_fingerprint = _fingerprint(
        payload["anchor_source_cohort_fingerprint"], optional=True
    )
    generation = payload["anchor_readback_generation"]
    requested_raw = payload["anchor_requested_action"]
    active_raw = payload["anchor_active_action"]
    anchor_power = _number(
        payload["anchor_attributed_power_w"],
        minimum=0.0,
        maximum=MAX_ACCOUNTING_POWER_W,
    )
    feedback_power = _number(
        payload["delivered_power_feedback_w"],
        minimum=0.0,
        maximum=MAX_ACCOUNTING_POWER_W,
    )

    anchor_absent = observed_at is None
    nullable_values = (
        intent_fingerprint,
        requested_raw,
        active_raw,
        generation,
        evidence_fingerprint,
        source_fingerprint,
    )
    if anchor_absent:
        if (
            any(value is not None for value in nullable_values)
            or anchor_power != 0.0
            or feedback_power != 0.0
        ):
            raise ValueError("Cleared accounting anchor contains residual data")
        requested_action = None
        active_action = None
        if reason in {
            AccountingUpdateReason.SEEDED,
            AccountingUpdateReason.ACCUMULATED,
        }:
            raise ValueError("Cleared accounting anchor has a qualified reason")
    else:
        if any(value is None for value in nullable_values):
            raise ValueError("Qualified accounting anchor is incomplete")
        if type(generation) is not int or not (
            0 <= generation <= ledger.MAX_READBACK_GENERATION
        ):
            raise ValueError("Accounting readback generation is invalid")
        if type(requested_raw) is not str or type(active_raw) is not str:
            raise ValueError("Accounting action identity must be text")
        try:
            requested_action = ledger.RequestedAction(requested_raw)
            active_action = ledger.TariffActiveAction(active_raw)
        except (TypeError, ValueError) as err:
            raise ValueError("Accounting action identity is invalid") from err
        if requested_action not in {
            ledger.RequestedAction.TARIFF_BATTERY_CHARGE,
            ledger.RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        }:
            raise ValueError("Accounting requested action is not charge-capable")
        expected_active = {
            ledger.RequestedAction.TARIFF_BATTERY_CHARGE: (
                ledger.TariffActiveAction.BATTERY_CHARGE
            ),
            ledger.RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE: (
                ledger.TariffActiveAction.GRID_SUPPORT_AND_CHARGE
            ),
        }[requested_action]
        if active_action is not expected_active:
            raise ValueError("Accounting active action identity is inconsistent")
        if anchor_power <= 0.0 or feedback_power != anchor_power:
            raise ValueError("Accounting feedback differs from attributed power")
        if reason not in {
            AccountingUpdateReason.SEEDED,
            AccountingUpdateReason.ACCUMULATED,
        }:
            raise ValueError("Qualified anchor has a non-qualified update reason")

    if reason is AccountingUpdateReason.INITIAL and (
        accumulated != 0.0 or accepted_count != 0 or rejected_count != 0
    ):
        raise ValueError("Initial accounting state contains accumulated data")
    if accumulated > 0.0 and accepted_count == 0:
        raise ValueError("Accounting energy has no accepted interval provenance")
    return AccountingV2State(
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        storage_epoch=STORAGE_EPOCH,
        storage_key=STORAGE_KEY,
        entity_unique_id=ENTITY_UNIQUE_ID,
        accounting_contract_id=ACCOUNTING_CONTRACT_ID,
        accumulated_energy_kwh=accumulated,
        accepted_interval_count=accepted_count,
        rejected_interval_count=rejected_count,
        anchor_observed_at=observed_at,
        anchor_intent_fingerprint=intent_fingerprint,
        anchor_requested_action=requested_action,
        anchor_active_action=active_action,
        anchor_readback_generation=(generation if not anchor_absent else None),
        anchor_evidence_fingerprint=evidence_fingerprint,
        anchor_source_cohort_fingerprint=source_fingerprint,
        anchor_attributed_power_w=anchor_power,
        delivered_power_feedback_w=feedback_power,
        last_update_reason=reason,
        legacy_v1_invalid_reason=LEGACY_V1_INVALID_REASON,
    )


def accounting_state_from_dict(payload: dict[str, Any]) -> AccountingV2State:
    """Strictly decode one complete epoch-v2 persistence payload."""

    return _state_from_exact_payload(payload)


def serialize_accounting_state(state: AccountingV2State) -> str:
    """Serialize one state as stable, bounded canonical JSON."""

    serialized = json.dumps(
        accounting_state_to_dict(state),
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(serialized.encode("ascii")) > MAX_SERIALIZED_STATE_BYTES:
        raise ValueError("Accounting state exceeds its bounded persistence contract")
    return serialized


def deserialize_accounting_state(serialized: str) -> AccountingV2State:
    """Strictly deserialize a complete canonical epoch-v2 state."""

    if type(serialized) is not str or not serialized:
        raise ValueError("Serialized accounting state must be non-empty text")
    if len(serialized.encode("utf-8")) > MAX_SERIALIZED_STATE_BYTES:
        raise ValueError("Serialized accounting state exceeds its size bound")
    try:
        payload = json.loads(serialized)
    except (TypeError, json.JSONDecodeError) as err:
        raise ValueError("Serialized accounting state is invalid JSON") from err
    state = accounting_state_from_dict(payload)
    if serialize_accounting_state(state) != serialized:
        raise ValueError("Serialized accounting state is not canonical")
    return state


__all__ = (
    "ACCOUNTING_CONTRACT_ID",
    "ENTITY_UNIQUE_ID",
    "LEGACY_V1_INVALID_REASON",
    "MAX_ACCOUNTING_POWER_W",
    "MAX_ACCUMULATED_ENERGY_KWH",
    "MAX_COUNTER_VALUE",
    "MAX_INTEGRATION_INTERVAL_SECONDS",
    "MAX_SERIALIZED_STATE_BYTES",
    "STORAGE_EPOCH",
    "STORAGE_KEY",
    "STORAGE_SCHEMA_VERSION",
    "AccountingUpdate",
    "AccountingUpdateReason",
    "AccountingV2State",
    "accounting_state_from_dict",
    "accounting_state_to_dict",
    "accumulate_execution_entry",
    "deserialize_accounting_state",
    "legacy_v1_diagnostic",
    "new_accounting_state",
    "serialize_accounting_state",
)
