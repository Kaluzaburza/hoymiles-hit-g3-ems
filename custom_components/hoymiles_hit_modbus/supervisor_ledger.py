"""Pure canonical execution evidence ledger and tariff accounting v2.

The module has no Home Assistant, clock, filesystem, network or actuator
dependencies.  Callers provide one normalized intent and one timestamped
physical evidence cohort.  A ledger entry is observational only: it never
grants execution authority and never consumes a legacy accumulated value.

Tariff energy attribution is intentionally narrow.  It is non-zero only when
the tariff intent, owner, active action, physical Grid Charge readback, fresh
FC03 generation, fresh physical grid-to-battery channel and physical grid
import all agree.  The one authoritative value is::

    min(grid_to_battery_power_w, abs(grid_power_w))

Missing, stale, future-dated, incoherent or malformed evidence produces a
bounded zero-power entry with an explicit reason.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
from typing import Any

try:
    from .ems_supervisor import OwnerKind, PhysicalMode, PolicyId, RequestedAction
except ImportError:  # Standalone deterministic tools import component modules directly.
    from ems_supervisor import (  # type: ignore[no-redef]
        OwnerKind,
        PhysicalMode,
        PolicyId,
        RequestedAction,
    )


LEDGER_SCHEMA_VERSION = 1
ACCOUNTING_EPOCH = 2
ACCOUNTING_CONTRACT_ID = "ems.physical-grid-to-battery.v2"
ACCOUNTING_SOURCE = "physical_grid_to_battery"
MAX_EVIDENCE_AGE_SECONDS = 120.0
MAX_EVIDENCE_SKEW_SECONDS = 120.0
FUTURE_TOLERANCE_SECONDS = 5.0
MAX_READBACK_GENERATION = 16_000_000
MAX_LEDGER_SERIALIZED_BYTES = 4096
MAX_SOURCE_LENGTH = 160

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class TariffActiveAction(str, Enum):
    """Closed tariff executor action vocabulary used by the ledger."""

    NONE = "none"
    GRID_SUPPORT = "grid_support"
    BATTERY_CHARGE = "battery_charge"
    GRID_SUPPORT_AND_CHARGE = "grid_support_and_charge"
    UNKNOWN = "unknown"


class EvidenceStatus(str, Enum):
    """Freshness and structural status of one physical source sample."""

    FRESH = "fresh"
    MISSING = "missing"
    INVALID = "invalid"
    STALE = "stale"
    FUTURE = "future"


class ActualFlow(str, Enum):
    """Physical power-flow classification independent from policy intent."""

    GRID_TO_BATTERY = "grid_to_battery"
    BATTERY_CHARGE_WITHOUT_GRID_IMPORT = "battery_charge_without_grid_import"
    GRID_IMPORT_OTHER = "grid_import_other"
    NO_GRID_TO_BATTERY = "no_grid_to_battery"
    UNVERIFIED = "unverified"


class LedgerClassification(str, Enum):
    """Relationship between policy intent and independently observed flow."""

    ATTRIBUTED = "attributed"
    ELIGIBLE_INTENT_WITHOUT_CONFIRMED_ACTUAL = (
        "eligible_intent_without_confirmed_actual"
    )
    CONFIRMED_ACTUAL_WITHOUT_ELIGIBLE_INTENT = (
        "confirmed_actual_without_eligible_intent"
    )
    NOT_ATTRIBUTED = "not_attributed"
    UNVERIFIED = "unverified"


class LedgerReason(str, Enum):
    """One deterministic primary reason for a v2 attribution decision."""

    ATTRIBUTED_PHYSICAL_GRID_TO_BATTERY = "attributed_physical_grid_to_battery"
    POLICY_NOT_TARIFF = "policy_not_tariff"
    REQUESTED_ACTION_NOT_BATTERY_CHARGE = (
        "requested_action_not_battery_charge"
    )
    POLICY_INACTIVE = "policy_inactive"
    OWNER_NOT_TARIFF = "owner_not_tariff"
    ACTIVE_ACTION_NOT_GRID_CHARGE = "active_action_not_grid_charge"
    MODE_READBACK_MISSING = "mode_readback_missing"
    MODE_READBACK_INVALID = "mode_readback_invalid"
    MODE_READBACK_STALE = "mode_readback_stale"
    MODE_READBACK_FUTURE = "mode_readback_future"
    MODE_NOT_GRID_CHARGE = "mode_not_grid_charge"
    READBACK_GENERATION_MISSING = "readback_generation_missing"
    READBACK_GENERATION_INVALID = "readback_generation_invalid"
    READBACK_GENERATION_STALE = "readback_generation_stale"
    READBACK_GENERATION_FUTURE = "readback_generation_future"
    READBACK_NOT_CONFIRMED = "readback_not_confirmed"
    GRID_TO_BATTERY_MISSING = "grid_to_battery_missing"
    GRID_TO_BATTERY_INVALID = "grid_to_battery_invalid"
    GRID_TO_BATTERY_STALE = "grid_to_battery_stale"
    GRID_TO_BATTERY_FUTURE = "grid_to_battery_future"
    GRID_TO_BATTERY_NON_POSITIVE = "grid_to_battery_non_positive"
    GRID_IMPORT_MISSING = "grid_import_missing"
    GRID_IMPORT_INVALID = "grid_import_invalid"
    GRID_IMPORT_STALE = "grid_import_stale"
    GRID_IMPORT_FUTURE = "grid_import_future"
    GRID_IMPORT_NOT_CONFIRMED = "grid_import_not_confirmed"
    EVIDENCE_INCOHERENT = "evidence_incoherent"


@dataclass(frozen=True, slots=True)
class EvidenceSample:
    """One source value with physical provenance and report time."""

    value: Any
    reported_at: datetime | None
    source: str


@dataclass(frozen=True, slots=True)
class ExecutionIntent:
    """Normalized policy intent; it is not proof of physical execution."""

    policy_id: PolicyId
    requested_action: RequestedAction
    active: bool
    owner_kind: OwnerKind
    active_action: TariffActiveAction
    intent_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class PhysicalExecutionEvidence:
    """One coherent candidate cohort of physical executor evidence.

    ``grid_power_w`` uses the normalized project sign convention: a negative
    value is physical grid import and a positive value is physical export.
    """

    observed_at: datetime
    mode_readback: EvidenceSample
    readback_generation: EvidenceSample
    readback_confirmed: bool
    grid_to_battery_power_w: EvidenceSample
    grid_power_w: EvidenceSample


@dataclass(frozen=True, slots=True)
class EvidenceProvenance:
    """Bounded Recorder-safe provenance for one ledger input."""

    field: str
    source: str
    reported_at: datetime | None
    status: EvidenceStatus


@dataclass(frozen=True, slots=True)
class ExecutionLedgerEntry:
    """Immutable accounting-v2 result with no execution-authority fields."""

    ledger_schema_version: int
    accounting_epoch: int
    accounting_contract_id: str
    accounting_source: str
    observed_at: datetime
    policy_id: PolicyId
    requested_action: RequestedAction
    intent_active: bool
    owner_kind: OwnerKind
    active_action: TariffActiveAction
    intent_fingerprint: str | None
    intent_matched: bool
    physical_mode: PhysicalMode | None
    readback_generation: int | None
    readback_confirmed: bool
    grid_to_battery_power_w: float | None
    grid_power_w: float | None
    confirmed_grid_import_power_w: float | None
    actual_flow: ActualFlow
    classification: LedgerClassification
    qualified: bool
    reason_code: LedgerReason
    attributed_power_w: float
    provenance: tuple[EvidenceProvenance, ...]
    evidence_fingerprint: str


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
        raise ValueError("Ledger timestamps must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Non-finite values are not canonical ledger JSON")
        return 0.0 if value == 0.0 else value
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_fingerprint_value(value: Any) -> Any:
    """Represent malformed runtime evidence without emitting NaN JSON."""

    if isinstance(value, Enum):
        return value.value
    if value is None or type(value) in {bool, int, str}:
        return value
    if type(value) is float:
        if math.isfinite(value):
            return 0.0 if value == 0.0 else value
        if math.isnan(value):
            return "__non_finite_nan__"
        return "__non_finite_positive__" if value > 0 else "__non_finite_negative__"
    return f"__invalid_type_{type(value).__name__}__"


def _safe_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if not _aware(value):
        return "__invalid_timestamp__"
    return _timestamp(value)


def _valid_source(value: Any) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= MAX_SOURCE_LENGTH
        and value.isascii()
        and not any(character.isspace() for character in value)
    )


def _valid_number(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(float(value))


def _valid_generation(value: Any) -> bool:
    return type(value) is int and 0 <= value <= MAX_READBACK_GENERATION


def _sample_status(
    sample: EvidenceSample,
    observed_at: datetime,
    *,
    value_validator: Any,
) -> EvidenceStatus:
    if sample.value is None or sample.reported_at is None:
        return EvidenceStatus.MISSING
    if (
        not _valid_source(sample.source)
        or not _aware(sample.reported_at)
        or not value_validator(sample.value)
    ):
        return EvidenceStatus.INVALID
    age_seconds = (
        observed_at.astimezone(timezone.utc)
        - sample.reported_at.astimezone(timezone.utc)
    ).total_seconds()
    if age_seconds < -FUTURE_TOLERANCE_SECONDS:
        return EvidenceStatus.FUTURE
    if age_seconds > MAX_EVIDENCE_AGE_SECONDS:
        return EvidenceStatus.STALE
    return EvidenceStatus.FRESH


def _reason_for_status(
    status: EvidenceStatus,
    *,
    missing: LedgerReason,
    invalid: LedgerReason,
    stale: LedgerReason,
    future: LedgerReason,
) -> LedgerReason | None:
    return {
        EvidenceStatus.MISSING: missing,
        EvidenceStatus.INVALID: invalid,
        EvidenceStatus.STALE: stale,
        EvidenceStatus.FUTURE: future,
    }.get(status)


def _finite_float_or_none(value: Any) -> float | None:
    return float(value) if _valid_number(value) else None


def _sample_fingerprint(sample: EvidenceSample) -> dict[str, Any]:
    return {
        "value": _safe_fingerprint_value(sample.value),
        "reported_at": _safe_timestamp(sample.reported_at),
        "source": sample.source if type(sample.source) is str else "__invalid_source__",
    }


def _validate_structural_input(
    intent: ExecutionIntent,
    evidence: PhysicalExecutionEvidence,
) -> None:
    if type(intent) is not ExecutionIntent:
        raise ValueError("intent must be an exact ExecutionIntent")
    if type(evidence) is not PhysicalExecutionEvidence:
        raise ValueError("evidence must be an exact PhysicalExecutionEvidence")
    if not isinstance(intent.policy_id, PolicyId):
        raise ValueError("intent policy_id is invalid")
    if not isinstance(intent.requested_action, RequestedAction):
        raise ValueError("intent requested_action is invalid")
    if type(intent.active) is not bool:
        raise ValueError("intent active must be bool")
    if not isinstance(intent.owner_kind, OwnerKind):
        raise ValueError("intent owner_kind is invalid")
    if not isinstance(intent.active_action, TariffActiveAction):
        raise ValueError("intent active_action is invalid")
    if (
        intent.intent_fingerprint is not None
        and (
            type(intent.intent_fingerprint) is not str
            or _SHA256_RE.fullmatch(intent.intent_fingerprint) is None
        )
    ):
        raise ValueError("intent fingerprint must be lowercase SHA-256")
    if not _aware(evidence.observed_at):
        raise ValueError("observed_at must be timezone-aware")
    if type(evidence.readback_confirmed) is not bool:
        raise ValueError("readback_confirmed must be bool")
    for sample in (
        evidence.mode_readback,
        evidence.readback_generation,
        evidence.grid_to_battery_power_w,
        evidence.grid_power_w,
    ):
        if type(sample) is not EvidenceSample:
            raise ValueError("physical evidence contains a non-sample value")


def _intent_reason(intent: ExecutionIntent) -> LedgerReason | None:
    if intent.policy_id is not PolicyId.TARIFF:
        return LedgerReason.POLICY_NOT_TARIFF
    expected_active_action = {
        RequestedAction.TARIFF_BATTERY_CHARGE: TariffActiveAction.BATTERY_CHARGE,
        RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE: (
            TariffActiveAction.GRID_SUPPORT_AND_CHARGE
        ),
    }.get(intent.requested_action)
    if expected_active_action is None:
        return LedgerReason.REQUESTED_ACTION_NOT_BATTERY_CHARGE
    if not intent.active:
        return LedgerReason.POLICY_INACTIVE
    if intent.owner_kind is not OwnerKind.TARIFF:
        return LedgerReason.OWNER_NOT_TARIFF
    if intent.active_action is not expected_active_action:
        return LedgerReason.ACTIVE_ACTION_NOT_GRID_CHARGE
    return None


def _evidence_statuses(
    evidence: PhysicalExecutionEvidence,
) -> tuple[EvidenceStatus, EvidenceStatus, EvidenceStatus, EvidenceStatus]:
    return (
        _sample_status(
            evidence.mode_readback,
            evidence.observed_at,
            value_validator=lambda value: isinstance(value, PhysicalMode),
        ),
        _sample_status(
            evidence.readback_generation,
            evidence.observed_at,
            value_validator=_valid_generation,
        ),
        _sample_status(
            evidence.grid_to_battery_power_w,
            evidence.observed_at,
            value_validator=_valid_number,
        ),
        _sample_status(
            evidence.grid_power_w,
            evidence.observed_at,
            value_validator=_valid_number,
        ),
    )


def _evidence_coherent(
    evidence: PhysicalExecutionEvidence,
    statuses: tuple[EvidenceStatus, ...],
) -> bool:
    if any(status is not EvidenceStatus.FRESH for status in statuses):
        return False
    timestamps = (
        evidence.mode_readback.reported_at,
        evidence.readback_generation.reported_at,
        evidence.grid_to_battery_power_w.reported_at,
        evidence.grid_power_w.reported_at,
    )
    normalized = [
        timestamp.astimezone(timezone.utc)
        for timestamp in timestamps
        if timestamp is not None
    ]
    return (
        len(normalized) == len(timestamps)
        and (max(normalized) - min(normalized)).total_seconds()
        <= MAX_EVIDENCE_SKEW_SECONDS
    )


def _actual_flow(
    evidence: PhysicalExecutionEvidence,
    grid_to_battery_status: EvidenceStatus,
    grid_status: EvidenceStatus,
) -> ActualFlow:
    if (
        grid_to_battery_status is not EvidenceStatus.FRESH
        or grid_status is not EvidenceStatus.FRESH
    ):
        return ActualFlow.UNVERIFIED
    grid_to_battery_reported = evidence.grid_to_battery_power_w.reported_at
    grid_reported = evidence.grid_power_w.reported_at
    if grid_to_battery_reported is None or grid_reported is None:
        return ActualFlow.UNVERIFIED
    if (
        abs(
            (
                grid_to_battery_reported.astimezone(timezone.utc)
                - grid_reported.astimezone(timezone.utc)
            ).total_seconds()
        )
        > MAX_EVIDENCE_SKEW_SECONDS
    ):
        return ActualFlow.UNVERIFIED
    grid_to_battery = float(evidence.grid_to_battery_power_w.value)
    grid_power = float(evidence.grid_power_w.value)
    if grid_to_battery > 0.0 and grid_power < 0.0:
        return ActualFlow.GRID_TO_BATTERY
    if grid_to_battery > 0.0:
        return ActualFlow.BATTERY_CHARGE_WITHOUT_GRID_IMPORT
    if grid_power < 0.0:
        return ActualFlow.GRID_IMPORT_OTHER
    return ActualFlow.NO_GRID_TO_BATTERY


def _attribution_reason(
    intent: ExecutionIntent,
    evidence: PhysicalExecutionEvidence,
    statuses: tuple[EvidenceStatus, EvidenceStatus, EvidenceStatus, EvidenceStatus],
) -> LedgerReason:
    intent_reason = _intent_reason(intent)
    if intent_reason is not None:
        return intent_reason

    mode_status, generation_status, grid_to_battery_status, grid_status = statuses
    status_reason = _reason_for_status(
        mode_status,
        missing=LedgerReason.MODE_READBACK_MISSING,
        invalid=LedgerReason.MODE_READBACK_INVALID,
        stale=LedgerReason.MODE_READBACK_STALE,
        future=LedgerReason.MODE_READBACK_FUTURE,
    )
    if status_reason is not None:
        return status_reason
    if evidence.mode_readback.value is not PhysicalMode.GRID_CHARGE:
        return LedgerReason.MODE_NOT_GRID_CHARGE

    status_reason = _reason_for_status(
        generation_status,
        missing=LedgerReason.READBACK_GENERATION_MISSING,
        invalid=LedgerReason.READBACK_GENERATION_INVALID,
        stale=LedgerReason.READBACK_GENERATION_STALE,
        future=LedgerReason.READBACK_GENERATION_FUTURE,
    )
    if status_reason is not None:
        return status_reason
    if not evidence.readback_confirmed:
        return LedgerReason.READBACK_NOT_CONFIRMED

    status_reason = _reason_for_status(
        grid_to_battery_status,
        missing=LedgerReason.GRID_TO_BATTERY_MISSING,
        invalid=LedgerReason.GRID_TO_BATTERY_INVALID,
        stale=LedgerReason.GRID_TO_BATTERY_STALE,
        future=LedgerReason.GRID_TO_BATTERY_FUTURE,
    )
    if status_reason is not None:
        return status_reason
    status_reason = _reason_for_status(
        grid_status,
        missing=LedgerReason.GRID_IMPORT_MISSING,
        invalid=LedgerReason.GRID_IMPORT_INVALID,
        stale=LedgerReason.GRID_IMPORT_STALE,
        future=LedgerReason.GRID_IMPORT_FUTURE,
    )
    if status_reason is not None:
        return status_reason
    if not _evidence_coherent(evidence, statuses):
        return LedgerReason.EVIDENCE_INCOHERENT
    if float(evidence.grid_to_battery_power_w.value) <= 0.0:
        return LedgerReason.GRID_TO_BATTERY_NON_POSITIVE
    if float(evidence.grid_power_w.value) >= 0.0:
        return LedgerReason.GRID_IMPORT_NOT_CONFIRMED
    return LedgerReason.ATTRIBUTED_PHYSICAL_GRID_TO_BATTERY


def _classification(
    *,
    intent_matched: bool,
    qualified: bool,
    actual_flow: ActualFlow,
) -> LedgerClassification:
    if qualified:
        return LedgerClassification.ATTRIBUTED
    if actual_flow is ActualFlow.UNVERIFIED:
        return LedgerClassification.UNVERIFIED
    if intent_matched:
        return LedgerClassification.ELIGIBLE_INTENT_WITHOUT_CONFIRMED_ACTUAL
    if actual_flow is ActualFlow.GRID_TO_BATTERY:
        return LedgerClassification.CONFIRMED_ACTUAL_WITHOUT_ELIGIBLE_INTENT
    return LedgerClassification.NOT_ATTRIBUTED


def _provenance(
    evidence: PhysicalExecutionEvidence,
    statuses: tuple[EvidenceStatus, EvidenceStatus, EvidenceStatus, EvidenceStatus],
) -> tuple[EvidenceProvenance, ...]:
    samples = (
        ("mode_readback", evidence.mode_readback),
        ("readback_generation", evidence.readback_generation),
        ("grid_to_battery_power_w", evidence.grid_to_battery_power_w),
        ("grid_power_w", evidence.grid_power_w),
    )
    return tuple(
        EvidenceProvenance(
            field=field,
            source=(
                sample.source
                if type(sample.source) is str
                else "__invalid_source__"
            ),
            reported_at=sample.reported_at if _aware(sample.reported_at) else None,
            status=status,
        )
        for (field, sample), status in zip(samples, statuses, strict=True)
    )


def _evidence_fingerprint(
    intent: ExecutionIntent,
    evidence: PhysicalExecutionEvidence,
) -> str:
    return _sha256_json(
        {
            "ledger_schema_version": LEDGER_SCHEMA_VERSION,
            "accounting_epoch": ACCOUNTING_EPOCH,
            "accounting_contract_id": ACCOUNTING_CONTRACT_ID,
            "intent": {
                "policy_id": intent.policy_id,
                "requested_action": intent.requested_action,
                "active": intent.active,
                "owner_kind": intent.owner_kind,
                "active_action": intent.active_action,
                "intent_fingerprint": intent.intent_fingerprint,
            },
            "evidence": {
                "mode_readback": _sample_fingerprint(evidence.mode_readback),
                "readback_generation": _sample_fingerprint(
                    evidence.readback_generation
                ),
                "readback_confirmed": evidence.readback_confirmed,
                "grid_to_battery_power_w": _sample_fingerprint(
                    evidence.grid_to_battery_power_w
                ),
                "grid_power_w": _sample_fingerprint(evidence.grid_power_w),
            },
        }
    )


def build_execution_ledger_entry(
    intent: ExecutionIntent,
    evidence: PhysicalExecutionEvidence,
) -> ExecutionLedgerEntry:
    """Return one deterministic accounting-v2 execution evidence record."""

    _validate_structural_input(intent, evidence)
    statuses = _evidence_statuses(evidence)
    reason = _attribution_reason(intent, evidence, statuses)
    intent_matched = _intent_reason(intent) is None
    actual_flow = _actual_flow(evidence, statuses[2], statuses[3])
    qualified = reason is LedgerReason.ATTRIBUTED_PHYSICAL_GRID_TO_BATTERY

    grid_to_battery_power = _finite_float_or_none(
        evidence.grid_to_battery_power_w.value
    )
    grid_power = _finite_float_or_none(evidence.grid_power_w.value)
    confirmed_grid_import_power = (
        -grid_power
        if statuses[3] is EvidenceStatus.FRESH
        and grid_power is not None
        and grid_power < 0.0
        else None
    )
    attributed_power = (
        min(grid_to_battery_power, confirmed_grid_import_power)
        if qualified
        and grid_to_battery_power is not None
        and confirmed_grid_import_power is not None
        else 0.0
    )
    physical_mode = (
        evidence.mode_readback.value
        if isinstance(evidence.mode_readback.value, PhysicalMode)
        else None
    )
    readback_generation = (
        evidence.readback_generation.value
        if _valid_generation(evidence.readback_generation.value)
        else None
    )
    return ExecutionLedgerEntry(
        ledger_schema_version=LEDGER_SCHEMA_VERSION,
        accounting_epoch=ACCOUNTING_EPOCH,
        accounting_contract_id=ACCOUNTING_CONTRACT_ID,
        accounting_source=ACCOUNTING_SOURCE,
        observed_at=evidence.observed_at,
        policy_id=intent.policy_id,
        requested_action=intent.requested_action,
        intent_active=intent.active,
        owner_kind=intent.owner_kind,
        active_action=intent.active_action,
        intent_fingerprint=intent.intent_fingerprint,
        intent_matched=intent_matched,
        physical_mode=physical_mode,
        readback_generation=readback_generation,
        readback_confirmed=evidence.readback_confirmed,
        grid_to_battery_power_w=grid_to_battery_power,
        grid_power_w=grid_power,
        confirmed_grid_import_power_w=confirmed_grid_import_power,
        actual_flow=actual_flow,
        classification=_classification(
            intent_matched=intent_matched,
            qualified=qualified,
            actual_flow=actual_flow,
        ),
        qualified=qualified,
        reason_code=reason,
        attributed_power_w=max(float(attributed_power), 0.0),
        provenance=_provenance(evidence, statuses),
        evidence_fingerprint=_evidence_fingerprint(intent, evidence),
    )


def execution_ledger_entry_to_dict(entry: ExecutionLedgerEntry) -> dict[str, Any]:
    """Return the bounded canonical public projection for an exact v2 entry."""

    if type(entry) is not ExecutionLedgerEntry:
        raise ValueError("entry must be an exact ExecutionLedgerEntry")
    if (
        entry.ledger_schema_version != LEDGER_SCHEMA_VERSION
        or entry.accounting_epoch != ACCOUNTING_EPOCH
        or entry.accounting_contract_id != ACCOUNTING_CONTRACT_ID
        or entry.accounting_source != ACCOUNTING_SOURCE
    ):
        raise ValueError("entry does not belong to accounting epoch v2")
    payload = {
        "ledger_schema_version": entry.ledger_schema_version,
        "accounting_epoch": entry.accounting_epoch,
        "accounting_contract_id": entry.accounting_contract_id,
        "accounting_source": entry.accounting_source,
        "observed_at": entry.observed_at,
        "policy_id": entry.policy_id,
        "requested_action": entry.requested_action,
        "intent_active": entry.intent_active,
        "owner_kind": entry.owner_kind,
        "active_action": entry.active_action,
        "intent_fingerprint": entry.intent_fingerprint,
        "intent_matched": entry.intent_matched,
        "physical_mode": entry.physical_mode,
        "readback_generation": entry.readback_generation,
        "readback_confirmed": entry.readback_confirmed,
        "grid_to_battery_power_w": entry.grid_to_battery_power_w,
        "grid_power_w": entry.grid_power_w,
        "confirmed_grid_import_power_w": entry.confirmed_grid_import_power_w,
        "actual_flow": entry.actual_flow,
        "classification": entry.classification,
        "qualified": entry.qualified,
        "reason_code": entry.reason_code,
        "attributed_power_w": entry.attributed_power_w,
        "provenance": [
            {
                "field": item.field,
                "source": item.source,
                "reported_at": item.reported_at,
                "status": item.status,
            }
            for item in entry.provenance
        ],
        "evidence_fingerprint": entry.evidence_fingerprint,
    }
    serialized = _canonical_json(payload)
    if len(serialized.encode("utf-8")) > MAX_LEDGER_SERIALIZED_BYTES:
        raise ValueError("Execution ledger entry exceeds the bounded contract")
    return _canonical_value(payload)


def serialize_execution_ledger_entry(entry: ExecutionLedgerEntry) -> str:
    """Serialize one exact accounting-v2 entry as canonical JSON."""

    return _canonical_json(execution_ledger_entry_to_dict(entry))


def attributed_power_for_feedback(entry: ExecutionLedgerEntry) -> float:
    """Return the same trusted physical power used by accounting, or zero.

    This strict projection is the integration hook for delivered-power
    feedback.  It rejects legacy epochs, forged totals and internally
    inconsistent entries instead of falling back to grid or battery power.
    """

    if type(entry) is not ExecutionLedgerEntry:
        return 0.0
    if (
        entry.ledger_schema_version != LEDGER_SCHEMA_VERSION
        or entry.accounting_epoch != ACCOUNTING_EPOCH
        or entry.accounting_contract_id != ACCOUNTING_CONTRACT_ID
        or entry.accounting_source != ACCOUNTING_SOURCE
        or not entry.qualified
        or entry.reason_code
        is not LedgerReason.ATTRIBUTED_PHYSICAL_GRID_TO_BATTERY
        or entry.classification is not LedgerClassification.ATTRIBUTED
        or entry.policy_id is not PolicyId.TARIFF
        or entry.requested_action
        not in {
            RequestedAction.TARIFF_BATTERY_CHARGE,
            RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        }
        or not entry.intent_active
        or entry.owner_kind is not OwnerKind.TARIFF
        or entry.active_action
        not in {
            TariffActiveAction.BATTERY_CHARGE,
            TariffActiveAction.GRID_SUPPORT_AND_CHARGE,
        }
        or (
            entry.requested_action is RequestedAction.TARIFF_BATTERY_CHARGE
            and entry.active_action is not TariffActiveAction.BATTERY_CHARGE
        )
        or (
            entry.requested_action
            is RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE
            and entry.active_action
            is not TariffActiveAction.GRID_SUPPORT_AND_CHARGE
        )
        or entry.physical_mode is not PhysicalMode.GRID_CHARGE
        or not entry.readback_confirmed
        or entry.readback_generation is None
        or _SHA256_RE.fullmatch(entry.evidence_fingerprint) is None
        or entry.grid_to_battery_power_w is None
        or not math.isfinite(entry.grid_to_battery_power_w)
        or entry.grid_to_battery_power_w <= 0.0
        or entry.grid_power_w is None
        or not math.isfinite(entry.grid_power_w)
        or entry.grid_power_w >= 0.0
        or entry.confirmed_grid_import_power_w is None
        or not math.isfinite(entry.confirmed_grid_import_power_w)
        or entry.confirmed_grid_import_power_w <= 0.0
        or not math.isclose(
            entry.confirmed_grid_import_power_w,
            abs(entry.grid_power_w),
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        return 0.0
    expected = min(entry.grid_to_battery_power_w, abs(entry.grid_power_w))
    if (
        not math.isfinite(entry.attributed_power_w)
        or entry.attributed_power_w < 0.0
        or not math.isclose(
            entry.attributed_power_w,
            expected,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    ):
        return 0.0
    return entry.attributed_power_w


__all__ = (
    "ACCOUNTING_CONTRACT_ID",
    "ACCOUNTING_EPOCH",
    "ACCOUNTING_SOURCE",
    "ActualFlow",
    "EvidenceProvenance",
    "EvidenceSample",
    "EvidenceStatus",
    "ExecutionIntent",
    "ExecutionLedgerEntry",
    "LEDGER_SCHEMA_VERSION",
    "LedgerClassification",
    "LedgerReason",
    "MAX_EVIDENCE_AGE_SECONDS",
    "MAX_EVIDENCE_SKEW_SECONDS",
    "MAX_LEDGER_SERIALIZED_BYTES",
    "PhysicalExecutionEvidence",
    "TariffActiveAction",
    "attributed_power_for_feedback",
    "build_execution_ledger_entry",
    "execution_ledger_entry_to_dict",
    "serialize_execution_ledger_entry",
)
