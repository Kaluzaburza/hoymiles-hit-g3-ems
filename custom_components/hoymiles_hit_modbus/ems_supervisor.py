"""Pure deterministic Shadow arbiter for EMS Supervisor V1 Phase 1A.

This module deliberately has no Home Assistant, filesystem, network, clock or
actuator dependencies.  Callers provide an explicit UTC-aware ``now`` and
already-normalized policy and execution snapshots.  Phase 1A can only explain
an Off or theoretical Shadow decision; it can never authorize execution.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any


SCHEMA_VERSION = 1
PROFILE_TABLE_VERSION = 1
MAX_CANDIDATES = 3
MAX_SUPERVISOR_SUMMARY_BYTES = 8192
MAX_CANDIDATE_SUMMARY_BYTES = 1536

_UINT64_MAX = 18_446_744_073_709_551_615
_INT64_MIN = -9_223_372_036_854_775_808
_INT64_MAX = 9_223_372_036_854_775_807
_BINARY64_MAX = float.fromhex("0x1.fffffffffffffp+1023")

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_ECONOMIC_CONTRACT_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")


class SupervisorMode(str, Enum):
    """Configured Supervisor operating mode."""

    OFF = "off"
    SHADOW = "shadow"
    ACTIVE = "active"


class SupervisorProfile(str, Enum):
    """Experimental profile identity; Phase 1A applies no profile tuning."""

    MAXIMUM_PROFIT = "maximum_profit"
    BALANCED = "balanced"
    HIGH_RESERVE_WINTER = "high_reserve_winter"


class PolicyId(str, Enum):
    """Policy families supported by EMS Supervisor V1."""

    RCE = "rce"
    TARIFF = "tariff"
    RCM = "rcm"


class PhysicalMode(str, Enum):
    """Normalized physical inverter mode."""

    SELF_USE = "self_use"
    GRID_CHARGE = "grid_charge"
    GRID_DISCHARGE = "grid_discharge"
    OFF_GRID = "off_grid"
    UNKNOWN = "unknown"


class OwnerKind(str, Enum):
    """Normalized execution owner; ``none`` is not compatibility ``manual``."""

    NONE = "none"
    MANUAL = "manual"
    BALANCING = "balancing"
    RCE = "rce"
    TARIFF = "tariff"
    RCM = "rcm"
    FOREIGN = "foreign"
    UNKNOWN = "unknown"


class ExportState(str, Enum):
    """Verified export-authority verdict."""

    VERIFIED_ALLOWED = "verified_allowed"
    CONFIRMED_ZERO_EXPORT = "confirmed_zero_export"
    PROHIBITED = "prohibited"
    UNVERIFIED = "unverified"


class NeedClass(str, Enum):
    """Normalized policy need."""

    MANDATORY = "mandatory"
    PREVENTIVE = "preventive"
    OPTIONAL = "optional"
    NONE = "none"


class PriorityClass(str, Enum):
    """Policy-only arbitration priority; external authority is not a policy."""

    LIVE_EMERGENCY = "live_emergency"
    REQUIRED_ENERGY = "required_energy"
    PREVENTIVE_GRID = "preventive_grid"
    ECONOMIC = "economic"
    NONE = "none"


class RequestedAction(str, Enum):
    """Closed V1 action vocabulary."""

    NONE = "none"
    RCE_EXPORT = "rce_export"
    TARIFF_CHARGE = "tariff_charge"
    RCM_ABSORB_PV = "rcm_absorb_pv"
    RCM_LIMIT_EXPORT = "rcm_limit_export"
    RCM_PRE_DISCHARGE = "rcm_pre_discharge"


class ActuatorScope(str, Enum):
    """Physical actuator family requested by a candidate."""

    NONE = "none"
    EMS_BLOCK_4300_4306 = "ems_block_4300_4306"
    DIRECT_306 = "direct_306"
    DIRECT_259 = "direct_259"


class EconomicValueStatus(str, Enum):
    """Whether a candidate's marginal value may enter pairwise comparison."""

    UNAVAILABLE = "unavailable"
    PROVISIONAL = "provisional"
    COMPARABLE = "comparable"
    INCOMPATIBLE = "incompatible"


class TemporalStatus(str, Enum):
    """Bounded temporal verdict derived from caller-provided time."""

    FUTURE = "future"
    NOT_STARTED = "not_started"
    VALID = "valid"
    EXPIRED = "expired"


class SupervisorState(str, Enum):
    """Only states implemented by Phase 1A."""

    OFF = "off"
    SHADOW_IDLE = "shadow_idle"
    SHADOW_SELECTED = "shadow_selected"
    BLOCKED = "blocked"


class ExecutionPhase(str, Enum):
    """Observed execution phase; Phase 1A never starts or stops a writer."""

    IDLE = "idle"
    OBSERVED_ACTIVE_LATCHED = "observed_active_latched"
    BLOCKED = "blocked"


class SelectionKind(str, Enum):
    """Meaning of ``selected_policy`` in the decision."""

    NONE = "none"
    SHADOW = "shadow"
    PRESERVED_COMMITMENT = "preserved_commitment"
    OFF = "off"
    BLOCKED = "blocked"
    ACTIVE_NOT_IMPLEMENTED = "active_not_implemented"


class ProfileEffect(str, Enum):
    """Closed profile-effect schema reserved for later evidence-based tuning."""

    ECONOMIC_MINIMUM_ADVANTAGE = "economic_minimum_advantage"
    SWITCHING_ADVANTAGE = "switching_advantage"
    COMMITMENT_PREFERENCE = "commitment_preference"
    SOFT_RESERVE = "soft_reserve"
    BATTERY_WEAR_WEIGHT = "battery_wear_weight"
    PREFERRED_THROUGHPUT = "preferred_throughput"
    MINIMUM_HOLD = "minimum_hold"


class ReasonCode(str, Enum):
    """Bounded reasons consumed or produced by the pure arbiter."""

    CANDIDATE_READY = "candidate_ready"
    LIVE_EMERGENCY = "live_emergency"
    REQUIRED_ENERGY_RESTORE = "required_energy_restore"
    PREVENTIVE_VOLTAGE_ACTION = "preventive_voltage_action"
    ECONOMIC_CANDIDATE = "economic_candidate"
    NO_ACTION = "no_action"
    NO_ELIGIBLE_CANDIDATE = "no_eligible_candidate"
    NOT_ALLOWED = "not_allowed"
    POLICY_DISABLED = "policy_disabled"
    UNAVAILABLE = "unavailable"
    STALE_CANDIDATE = "stale_candidate"
    FUTURE_CANDIDATE = "future_candidate"
    NOT_STARTED = "not_started"
    RESULT_NOT_CURRENT = "result_not_current"
    RECALCULATION_PENDING_NEW_START = "recalculation_pending_new_start"
    EXPIRED = "expired"
    INVALID_INPUT = "invalid_input"
    INVALID_POLICY_SHAPE = "invalid_policy_shape"
    INVALID_ACTION_SCOPE = "invalid_action_scope"
    ACTUATOR_UNAVAILABLE = "actuator_unavailable"
    DIRECTION_UNAVAILABLE = "direction_unavailable"
    CONFIRMED_ZERO_EXPORT = "confirmed_zero_export"
    EXPORT_PROHIBITED = "export_prohibited"
    EXPORT_UNVERIFIED = "export_unverified"
    LOCAL_HARD_STOP = "local_hard_stop"
    NOT_START_ELIGIBLE = "not_start_eligible"
    NOT_CONTINUATION_ELIGIBLE = "not_continuation_eligible"
    EXTERNAL_AUTHORITY = "external_authority"
    MANUAL_AUTHORITY = "manual_authority"
    OFF_GRID = "off_grid"
    FOREIGN_OWNER = "foreign_owner"
    BALANCING_ACTIVE = "balancing_active"
    OWNER_CONFLICT = "owner_conflict"
    TRANSACTION_PENDING = "transaction_pending"
    PHYSICAL_MODE_STALE = "physical_mode_stale"
    PHYSICAL_MODE_UNKNOWN = "physical_mode_unknown"
    CRITICAL_BMS_UNAVAILABLE = "critical_bms_unavailable"
    ECONOMIC_CANDIDATES_NOT_COMPARABLE = (
        "economic_candidates_not_comparable"
    )
    ECONOMIC_TIE = "economic_tie"
    ACTIVE_NOT_IMPLEMENTED = "active_not_implemented"
    STRUCTURALLY_INCONSISTENT_CONTEXT = (
        "structurally_inconsistent_context"
    )
    INVALID_PENDING_OWNER_RELATIONSHIP = (
        "invalid_pending_owner_relationship"
    )
    MULTIPLE_ACTIVE_COMMITMENTS = "multiple_active_commitments"
    OWNER_COMMITMENT_MISMATCH = "owner_commitment_mismatch"
    INCONSISTENT_PRIORITY_TIE = "inconsistent_priority_tie"


@dataclass(frozen=True, slots=True)
class _ShadowProfileDefinition:
    """Versioned neutral profile evidence used by Phase 1A."""

    version: int
    effects_applied: tuple[ProfileEffect, ...]
    effects_not_applied: tuple[ProfileEffect, ...]


_ALL_PROFILE_EFFECTS = tuple(ProfileEffect)
_PROFILE_TABLE = MappingProxyType(
    {
        profile: _ShadowProfileDefinition(
            version=PROFILE_TABLE_VERSION,
            effects_applied=(),
            effects_not_applied=_ALL_PROFILE_EFFECTS,
        )
        for profile in SupervisorProfile
    }
)
_AUTOMATIC_OWNER_BY_POLICY = MappingProxyType(
    {
        PolicyId.RCE: OwnerKind.RCE,
        PolicyId.TARIFF: OwnerKind.TARIFF,
        PolicyId.RCM: OwnerKind.RCM,
    }
)
_POLICY_BY_AUTOMATIC_OWNER = MappingProxyType(
    {
        owner: policy
        for policy, owner in _AUTOMATIC_OWNER_BY_POLICY.items()
    }
)

_ALLOWED_POLICY_SHAPES = frozenset(
    {
        (
            PolicyId.RCE,
            PriorityClass.ECONOMIC,
            NeedClass.OPTIONAL,
            RequestedAction.RCE_EXPORT,
            ActuatorScope.EMS_BLOCK_4300_4306,
        ),
        (
            PolicyId.TARIFF,
            PriorityClass.REQUIRED_ENERGY,
            NeedClass.MANDATORY,
            RequestedAction.TARIFF_CHARGE,
            ActuatorScope.EMS_BLOCK_4300_4306,
        ),
        (
            PolicyId.TARIFF,
            PriorityClass.ECONOMIC,
            NeedClass.OPTIONAL,
            RequestedAction.TARIFF_CHARGE,
            ActuatorScope.EMS_BLOCK_4300_4306,
        ),
        (
            PolicyId.RCM,
            PriorityClass.LIVE_EMERGENCY,
            NeedClass.MANDATORY,
            RequestedAction.RCM_ABSORB_PV,
            ActuatorScope.DIRECT_306,
        ),
        (
            PolicyId.RCM,
            PriorityClass.LIVE_EMERGENCY,
            NeedClass.MANDATORY,
            RequestedAction.RCM_LIMIT_EXPORT,
            ActuatorScope.DIRECT_259,
        ),
        (
            PolicyId.RCM,
            PriorityClass.PREVENTIVE_GRID,
            NeedClass.PREVENTIVE,
            RequestedAction.RCM_ABSORB_PV,
            ActuatorScope.DIRECT_306,
        ),
        (
            PolicyId.RCM,
            PriorityClass.PREVENTIVE_GRID,
            NeedClass.PREVENTIVE,
            RequestedAction.RCM_LIMIT_EXPORT,
            ActuatorScope.DIRECT_259,
        ),
        (
            PolicyId.RCM,
            PriorityClass.PREVENTIVE_GRID,
            NeedClass.PREVENTIVE,
            RequestedAction.RCM_PRE_DISCHARGE,
            ActuatorScope.EMS_BLOCK_4300_4306,
        ),
        *(
            (
                policy,
                PriorityClass.NONE,
                NeedClass.NONE,
                RequestedAction.NONE,
                ActuatorScope.NONE,
            )
            for policy in PolicyId
        ),
    }
)

_PRIORITY_ORDER = MappingProxyType(
    {
        PriorityClass.LIVE_EMERGENCY: 0,
        PriorityClass.REQUIRED_ENERGY: 1,
        PriorityClass.PREVENTIVE_GRID: 2,
        PriorityClass.ECONOMIC: 3,
        PriorityClass.NONE: 4,
    }
)


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Shared physical and execution facts, normalized outside the arbiter."""

    observed_at: datetime
    physical_mode: PhysicalMode
    physical_mode_fresh: bool
    owner_kind: OwnerKind
    owner_conflict: bool
    transaction_pending: bool
    transaction_owner_kind: OwnerKind
    full_block_execution_ready: bool
    direct_306_execution_ready: bool
    direct_259_execution_ready: bool
    topology_full_block_allowed: bool
    topology_direct_register_allowed: bool
    charge_direction_ready: bool
    discharge_direction_ready: bool
    critical_bms_ready: bool
    export_state: ExportState


@dataclass(frozen=True, slots=True)
class PolicyCandidate:
    """Bounded immutable policy-specific snapshot consumed by arbitration."""

    schema_version: int
    policy_id: PolicyId
    observed_at: datetime
    allowed_by_user: bool
    enabled: bool
    available: bool
    result_current: bool
    recalculation_pending: bool
    input_revision: int
    candidate_revision: int
    start_eligible: bool
    continuation_eligible: bool
    active_latched: bool
    local_hard_stop: bool
    requested_action: RequestedAction
    actuator_scope: ActuatorScope
    priority_class: PriorityClass
    need_class: NeedClass
    reason_code: ReasonCode
    blocked_reason: ReasonCode | None
    valid_from: datetime | None
    valid_until: datetime | None
    desired_actuator_fingerprint: str | None
    economic_value_status: EconomicValueStatus
    requested_mode: PhysicalMode | None = None
    requested_power_kw: float | None = None
    requested_energy_kwh: float | None = None
    target_soc_percent: float | None = None
    protected_soc_floor_percent: float | None = None
    economic_contract_id: str | None = None
    economic_basis_fingerprint: str | None = None
    expected_marginal_net_benefit_pln: float | None = None
    urgency: float | None = None
    severity: float | None = None


@dataclass(frozen=True, slots=True)
class CandidateSummary:
    """Recorder-safe candidate projection used by the future HA adapter."""

    policy_id: PolicyId
    input_revision: int
    candidate_revision: int
    temporal_status: TemporalStatus
    allowed_by_user: bool
    enabled: bool
    available: bool
    result_current: bool
    recalculation_pending: bool
    start_eligible: bool
    continuation_eligible: bool
    active_latched: bool
    local_hard_stop: bool
    requested_action: RequestedAction
    actuator_scope: ActuatorScope
    priority_class: PriorityClass
    need_class: NeedClass
    reason_code: ReasonCode
    blocked_reason: ReasonCode | None
    rejection_reason: ReasonCode | None
    valid_from: datetime | None
    valid_until: datetime | None
    desired_actuator_fingerprint: str | None
    requested_mode: PhysicalMode | None
    requested_power_kw: float | None
    requested_energy_kwh: float | None
    target_soc_percent: float | None
    protected_soc_floor_percent: float | None
    economic_value_status: EconomicValueStatus
    economic_contract_id: str | None
    economic_basis_fingerprint: str | None
    expected_marginal_net_benefit_pln: float | None
    urgency: float | None
    severity: float | None


# The Boolean marks fields consumed by arbitration.  This single explicit
# whitelist drives raw-candidate fingerprinting, CandidateSummary construction,
# and serialized candidate payloads.
_CANDIDATE_PROJECTION_FIELDS = (
    ("policy_id", True),
    ("input_revision", True),
    ("candidate_revision", True),
    ("temporal_status", True),
    ("allowed_by_user", True),
    ("enabled", True),
    ("available", True),
    ("result_current", True),
    ("recalculation_pending", True),
    ("start_eligible", True),
    ("continuation_eligible", True),
    ("active_latched", True),
    ("local_hard_stop", True),
    ("requested_action", True),
    ("actuator_scope", True),
    ("priority_class", True),
    ("need_class", True),
    ("reason_code", True),
    ("blocked_reason", True),
    ("rejection_reason", False),
    ("valid_from", True),
    ("valid_until", True),
    ("desired_actuator_fingerprint", True),
    ("requested_mode", False),
    ("requested_power_kw", False),
    ("requested_energy_kwh", False),
    ("target_soc_percent", False),
    ("protected_soc_floor_percent", False),
    ("economic_value_status", True),
    ("economic_contract_id", True),
    ("economic_basis_fingerprint", True),
    ("expected_marginal_net_benefit_pln", True),
    ("urgency", False),
    ("severity", False),
)


@dataclass(frozen=True, slots=True)
class CandidateRejection:
    """One bounded primary rejection reason for a policy."""

    policy_id: PolicyId
    reason: ReasonCode


@dataclass(frozen=True, slots=True)
class SupervisorDecision:
    """Immutable Phase 1A result; it contains no physical grant fields."""

    supervisor_mode: SupervisorMode
    profile: SupervisorProfile
    state: SupervisorState
    execution_phase: ExecutionPhase
    selected_policy: PolicyId | None
    selected_candidate_revision: int | None
    selection_kind: SelectionKind
    selection_reason: ReasonCode
    execution_blocked_reason: ReasonCode | None
    arbitration_revision: str
    supervisor_execution_authorized: bool
    legacy_execution_unchanged: bool
    candidate_summaries: tuple[CandidateSummary, ...]
    rejected_reasons: tuple[CandidateRejection, ...]
    profile_effects_applied: tuple[ProfileEffect, ...]
    profile_effects_not_applied: tuple[ProfileEffect, ...]


def _is_aware_datetime(value: Any) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime | None) -> str | None:
    if value is None:
        return None
    return _utc(value).isoformat().replace("+00:00", "Z")


def _is_exact_bool(value: Any) -> bool:
    return type(value) is bool


def _is_revision(value: Any) -> bool:
    return type(value) is int and 0 <= value <= _UINT64_MAX


def _is_bounded_number(value: Any) -> bool:
    """Accept only bounded JSON-safe int64 or finite binary64 values."""
    if type(value) is int:
        return _INT64_MIN <= value <= _INT64_MAX
    if type(value) is float:
        return math.isfinite(value) and abs(value) <= _BINARY64_MAX
    return False


def _normalize_number(value: int | float | None) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, float) and value == 0.0:
        return 0.0
    return value


def _valid_sha256(value: Any) -> bool:
    return type(value) is str and _SHA256_RE.fullmatch(value) is not None


def _valid_contract_id(value: Any) -> bool:
    return (
        type(value) is str
        and value.isascii()
        and _ECONOMIC_CONTRACT_RE.fullmatch(value) is not None
    )


def _valid_optional_numeric_fields(snapshot: Any) -> bool:
    """Validate the one numeric contract shared by inputs and public summaries."""
    numeric_fields = (
        snapshot.requested_power_kw,
        snapshot.requested_energy_kwh,
        snapshot.target_soc_percent,
        snapshot.protected_soc_floor_percent,
        snapshot.expected_marginal_net_benefit_pln,
        snapshot.urgency,
        snapshot.severity,
    )
    if any(value is not None and not _is_bounded_number(value) for value in numeric_fields):
        return False
    if snapshot.requested_power_kw is not None and snapshot.requested_power_kw < 0:
        return False
    if snapshot.requested_energy_kwh is not None and snapshot.requested_energy_kwh < 0:
        return False
    if any(
        value is not None and not 0 <= value <= 100
        for value in (
            snapshot.target_soc_percent,
            snapshot.protected_soc_floor_percent,
        )
    ):
        return False
    return not any(
        value is not None and value < 0
        for value in (snapshot.urgency, snapshot.severity)
    )


def _valid_economic_fields(snapshot: Any) -> bool:
    if snapshot.economic_contract_id is not None and not _valid_contract_id(
        snapshot.economic_contract_id
    ):
        return False
    if snapshot.economic_basis_fingerprint is not None and not _valid_sha256(
        snapshot.economic_basis_fingerprint
    ):
        return False
    economic_fields = (
        snapshot.expected_marginal_net_benefit_pln,
        snapshot.economic_contract_id,
        snapshot.economic_basis_fingerprint,
    )
    if snapshot.economic_value_status is EconomicValueStatus.UNAVAILABLE:
        return not any(value is not None for value in economic_fields)
    if snapshot.economic_value_status in {
        EconomicValueStatus.PROVISIONAL,
        EconomicValueStatus.INCOMPATIBLE,
    }:
        return (
            snapshot.economic_contract_id is None
            and snapshot.economic_basis_fingerprint is None
        )
    if snapshot.economic_value_status is EconomicValueStatus.COMPARABLE:
        return (
            snapshot.expected_marginal_net_benefit_pln is not None
            and _valid_contract_id(snapshot.economic_contract_id)
            and _valid_sha256(snapshot.economic_basis_fingerprint)
        )
    return False


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
            raise ValueError("Non-finite values are not canonical JSON")
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


def _temporal_status(candidate: PolicyCandidate, now: datetime) -> TemporalStatus:
    normalized_now = _utc(now)
    if _utc(candidate.observed_at) > normalized_now:
        return TemporalStatus.FUTURE
    if candidate.valid_from is not None and _utc(candidate.valid_from) > normalized_now:
        return TemporalStatus.NOT_STARTED
    if candidate.valid_until is not None and _utc(candidate.valid_until) <= normalized_now:
        return TemporalStatus.EXPIRED
    return TemporalStatus.VALID


def _validate_context(context: Any, now: datetime) -> ReasonCode | None:
    if type(context) is not ExecutionContext:
        return ReasonCode.STRUCTURALLY_INCONSISTENT_CONTEXT
    if not _is_aware_datetime(context.observed_at):
        return ReasonCode.STRUCTURALLY_INCONSISTENT_CONTEXT
    if _utc(context.observed_at) > _utc(now):
        return ReasonCode.STRUCTURALLY_INCONSISTENT_CONTEXT
    if not isinstance(context.physical_mode, PhysicalMode):
        return ReasonCode.STRUCTURALLY_INCONSISTENT_CONTEXT
    if not isinstance(context.owner_kind, OwnerKind):
        return ReasonCode.STRUCTURALLY_INCONSISTENT_CONTEXT
    if not isinstance(context.transaction_owner_kind, OwnerKind):
        return ReasonCode.INVALID_PENDING_OWNER_RELATIONSHIP
    if not isinstance(context.export_state, ExportState):
        return ReasonCode.STRUCTURALLY_INCONSISTENT_CONTEXT
    boolean_fields = (
        context.physical_mode_fresh,
        context.owner_conflict,
        context.transaction_pending,
        context.full_block_execution_ready,
        context.direct_306_execution_ready,
        context.direct_259_execution_ready,
        context.topology_full_block_allowed,
        context.topology_direct_register_allowed,
        context.charge_direction_ready,
        context.discharge_direction_ready,
        context.critical_bms_ready,
    )
    if not all(_is_exact_bool(value) for value in boolean_fields):
        return ReasonCode.STRUCTURALLY_INCONSISTENT_CONTEXT
    if (
        not context.transaction_pending
        and context.transaction_owner_kind is not OwnerKind.NONE
    ):
        return ReasonCode.INVALID_PENDING_OWNER_RELATIONSHIP
    if (
        context.transaction_pending
        and context.transaction_owner_kind is OwnerKind.NONE
    ):
        return ReasonCode.INVALID_PENDING_OWNER_RELATIONSHIP
    return None


def _validate_candidate(candidate: Any) -> ReasonCode | None:
    if type(candidate) is not PolicyCandidate:
        return ReasonCode.INVALID_INPUT
    if (
        type(candidate.schema_version) is not int
        or candidate.schema_version != SCHEMA_VERSION
    ):
        return ReasonCode.INVALID_INPUT
    if not isinstance(candidate.policy_id, PolicyId):
        return ReasonCode.INVALID_POLICY_SHAPE
    if not _is_aware_datetime(candidate.observed_at):
        return ReasonCode.INVALID_INPUT
    if candidate.valid_from is not None and not _is_aware_datetime(candidate.valid_from):
        return ReasonCode.INVALID_INPUT
    if candidate.valid_until is not None and not _is_aware_datetime(candidate.valid_until):
        return ReasonCode.INVALID_INPUT
    if (
        candidate.valid_from is not None
        and candidate.valid_until is not None
        and _utc(candidate.valid_until) <= _utc(candidate.valid_from)
    ):
        return ReasonCode.INVALID_INPUT
    if not _is_revision(candidate.input_revision) or not _is_revision(
        candidate.candidate_revision
    ):
        return ReasonCode.INVALID_INPUT
    boolean_fields = (
        candidate.allowed_by_user,
        candidate.enabled,
        candidate.available,
        candidate.result_current,
        candidate.recalculation_pending,
        candidate.start_eligible,
        candidate.continuation_eligible,
        candidate.active_latched,
        candidate.local_hard_stop,
    )
    if not all(_is_exact_bool(value) for value in boolean_fields):
        return ReasonCode.INVALID_INPUT
    enum_fields = (
        (candidate.requested_action, RequestedAction),
        (candidate.actuator_scope, ActuatorScope),
        (candidate.priority_class, PriorityClass),
        (candidate.need_class, NeedClass),
        (candidate.reason_code, ReasonCode),
        (candidate.economic_value_status, EconomicValueStatus),
    )
    if any(not isinstance(value, expected) for value, expected in enum_fields):
        return ReasonCode.INVALID_INPUT
    if candidate.blocked_reason is not None and not isinstance(
        candidate.blocked_reason, ReasonCode
    ):
        return ReasonCode.INVALID_INPUT
    if candidate.requested_mode is not None and not isinstance(
        candidate.requested_mode, PhysicalMode
    ):
        return ReasonCode.INVALID_INPUT
    shape = (
        candidate.policy_id,
        candidate.priority_class,
        candidate.need_class,
        candidate.requested_action,
        candidate.actuator_scope,
    )
    if shape not in _ALLOWED_POLICY_SHAPES:
        if (
            candidate.requested_action is RequestedAction.NONE
            or candidate.actuator_scope is ActuatorScope.NONE
        ):
            return ReasonCode.INVALID_ACTION_SCOPE
        return ReasonCode.INVALID_POLICY_SHAPE
    if candidate.requested_action is RequestedAction.NONE:
        if candidate.desired_actuator_fingerprint is not None:
            return ReasonCode.INVALID_ACTION_SCOPE
    elif not _valid_sha256(candidate.desired_actuator_fingerprint):
        return ReasonCode.INVALID_ACTION_SCOPE
    if not _valid_optional_numeric_fields(candidate):
        return ReasonCode.INVALID_INPUT
    if not _valid_economic_fields(candidate):
        return ReasonCode.INVALID_INPUT
    return None


def _validate_owner_commitment_consistency(
    context: ExecutionContext,
    candidates: tuple[PolicyCandidate, ...],
) -> ReasonCode | None:
    active = tuple(candidate for candidate in candidates if candidate.active_latched)
    if len(active) > 1:
        return ReasonCode.MULTIPLE_ACTIVE_COMMITMENTS
    if active:
        expected_owner = _AUTOMATIC_OWNER_BY_POLICY[active[0].policy_id]
        if context.owner_kind is not expected_owner:
            return ReasonCode.OWNER_COMMITMENT_MISMATCH
        if context.transaction_pending:
            transaction_owner = context.transaction_owner_kind
            if transaction_owner is not OwnerKind.UNKNOWN and transaction_owner is not expected_owner:
                return ReasonCode.INVALID_PENDING_OWNER_RELATIONSHIP
    elif context.owner_kind in _POLICY_BY_AUTOMATIC_OWNER:
        return ReasonCode.OWNER_COMMITMENT_MISMATCH
    if (
        context.transaction_pending
        and context.transaction_owner_kind in _POLICY_BY_AUTOMATIC_OWNER
    ):
        expected_policy = _POLICY_BY_AUTOMATIC_OWNER[
            context.transaction_owner_kind
        ]
        if not active or active[0].policy_id is not expected_policy:
            return ReasonCode.INVALID_PENDING_OWNER_RELATIONSHIP
    return None


def _global_execution_blocker(context: ExecutionContext) -> ReasonCode | None:
    """Return one primary blocker using a fixed diagnostic precedence."""
    if context.owner_conflict:
        return ReasonCode.OWNER_CONFLICT
    if context.physical_mode is PhysicalMode.OFF_GRID:
        return ReasonCode.OFF_GRID
    if context.owner_kind is OwnerKind.MANUAL:
        return ReasonCode.MANUAL_AUTHORITY
    if context.owner_kind is OwnerKind.BALANCING:
        return ReasonCode.BALANCING_ACTIVE
    if context.owner_kind in {OwnerKind.FOREIGN, OwnerKind.UNKNOWN}:
        return ReasonCode.FOREIGN_OWNER
    if context.transaction_pending:
        return ReasonCode.TRANSACTION_PENDING
    if not context.physical_mode_fresh:
        return ReasonCode.PHYSICAL_MODE_STALE
    if context.physical_mode is PhysicalMode.UNKNOWN:
        return ReasonCode.PHYSICAL_MODE_UNKNOWN
    if not context.critical_bms_ready:
        return ReasonCode.CRITICAL_BMS_UNAVAILABLE
    return None


def _scope_rejection(
    candidate: PolicyCandidate,
    context: ExecutionContext,
) -> ReasonCode | None:
    if candidate.actuator_scope is ActuatorScope.EMS_BLOCK_4300_4306:
        if (
            not context.full_block_execution_ready
            or not context.topology_full_block_allowed
        ):
            return ReasonCode.ACTUATOR_UNAVAILABLE
    elif candidate.actuator_scope is ActuatorScope.DIRECT_306:
        if (
            not context.direct_306_execution_ready
            or not context.topology_direct_register_allowed
        ):
            return ReasonCode.ACTUATOR_UNAVAILABLE
    elif candidate.actuator_scope is ActuatorScope.DIRECT_259:
        if (
            not context.direct_259_execution_ready
            or not context.topology_direct_register_allowed
        ):
            return ReasonCode.ACTUATOR_UNAVAILABLE
    return None


def _direction_rejection(
    candidate: PolicyCandidate,
    context: ExecutionContext,
) -> ReasonCode | None:
    if candidate.requested_action in {
        RequestedAction.RCE_EXPORT,
        RequestedAction.RCM_PRE_DISCHARGE,
    } and not context.discharge_direction_ready:
        return ReasonCode.DIRECTION_UNAVAILABLE
    if candidate.requested_action in {
        RequestedAction.TARIFF_CHARGE,
        RequestedAction.RCM_ABSORB_PV,
    } and not context.charge_direction_ready:
        return ReasonCode.DIRECTION_UNAVAILABLE
    return None


def _export_rejection(
    candidate: PolicyCandidate,
    context: ExecutionContext,
) -> ReasonCode | None:
    if candidate.requested_action is not RequestedAction.RCE_EXPORT:
        return None
    if context.export_state is ExportState.VERIFIED_ALLOWED:
        return None
    if context.export_state is ExportState.CONFIRMED_ZERO_EXPORT:
        return ReasonCode.CONFIRMED_ZERO_EXPORT
    if context.export_state is ExportState.PROHIBITED:
        return ReasonCode.EXPORT_PROHIBITED
    return ReasonCode.EXPORT_UNVERIFIED


def _candidate_rejection(
    candidate: PolicyCandidate,
    context: ExecutionContext,
    temporal_status: TemporalStatus,
) -> ReasonCode | None:
    if candidate.requested_action is RequestedAction.NONE:
        return ReasonCode.NO_ACTION
    if not candidate.allowed_by_user:
        return ReasonCode.NOT_ALLOWED
    if not candidate.enabled:
        return ReasonCode.POLICY_DISABLED
    if not candidate.available:
        return candidate.blocked_reason or ReasonCode.UNAVAILABLE
    if temporal_status is TemporalStatus.FUTURE:
        return ReasonCode.FUTURE_CANDIDATE
    if temporal_status is TemporalStatus.NOT_STARTED:
        return ReasonCode.NOT_STARTED
    if temporal_status is TemporalStatus.EXPIRED:
        return ReasonCode.EXPIRED
    if candidate.local_hard_stop:
        return ReasonCode.LOCAL_HARD_STOP
    for rejection in (
        _scope_rejection(candidate, context),
        _direction_rejection(candidate, context),
        _export_rejection(candidate, context),
    ):
        if rejection is not None:
            return rejection
    if candidate.active_latched:
        if not candidate.continuation_eligible:
            return candidate.blocked_reason or ReasonCode.NOT_CONTINUATION_ELIGIBLE
        if (
            context.transaction_pending
            and context.transaction_owner_kind is OwnerKind.UNKNOWN
        ):
            return ReasonCode.TRANSACTION_PENDING
        if not candidate.result_current and not candidate.recalculation_pending:
            return ReasonCode.RESULT_NOT_CURRENT
        return None
    if candidate.recalculation_pending:
        return ReasonCode.RECALCULATION_PENDING_NEW_START
    if not candidate.result_current:
        return ReasonCode.RESULT_NOT_CURRENT
    if not candidate.start_eligible:
        return candidate.blocked_reason or ReasonCode.NOT_START_ELIGIBLE
    return None


def _economic_candidates_comparable(
    left: PolicyCandidate,
    right: PolicyCandidate,
) -> bool:
    return (
        left.economic_value_status is EconomicValueStatus.COMPARABLE
        and right.economic_value_status is EconomicValueStatus.COMPARABLE
        and left.expected_marginal_net_benefit_pln is not None
        and right.expected_marginal_net_benefit_pln is not None
        and left.economic_contract_id is not None
        and left.economic_contract_id == right.economic_contract_id
        and left.economic_basis_fingerprint is not None
        and left.economic_basis_fingerprint
        == right.economic_basis_fingerprint
    )


def _selection_reason(candidate: PolicyCandidate) -> ReasonCode:
    if candidate.priority_class is PriorityClass.LIVE_EMERGENCY:
        return ReasonCode.LIVE_EMERGENCY
    if candidate.priority_class is PriorityClass.REQUIRED_ENERGY:
        return ReasonCode.REQUIRED_ENERGY_RESTORE
    if candidate.priority_class is PriorityClass.PREVENTIVE_GRID:
        return ReasonCode.PREVENTIVE_VOLTAGE_ACTION
    return ReasonCode.ECONOMIC_CANDIDATE


def _select_shadow_winner(
    eligible: tuple[PolicyCandidate, ...],
) -> tuple[PolicyCandidate | None, ReasonCode]:
    if not eligible:
        return None, ReasonCode.NO_ELIGIBLE_CANDIDATE
    best_rank = min(_PRIORITY_ORDER[candidate.priority_class] for candidate in eligible)
    best = tuple(
        sorted(
            (
                candidate
                for candidate in eligible
                if _PRIORITY_ORDER[candidate.priority_class] == best_rank
            ),
            key=lambda candidate: candidate.policy_id.value,
        )
    )
    if len(best) == 1:
        return best[0], _selection_reason(best[0])
    if best[0].priority_class is not PriorityClass.ECONOMIC:
        return None, ReasonCode.INCONSISTENT_PRIORITY_TIE
    active = tuple(candidate for candidate in best if candidate.active_latched)
    if active:
        # Phase 1A has no evidence-based switching-advantage threshold.
        return active[0], ReasonCode.ECONOMIC_CANDIDATE
    left, right = best
    if not _economic_candidates_comparable(left, right):
        return None, ReasonCode.ECONOMIC_CANDIDATES_NOT_COMPARABLE
    left_value = left.expected_marginal_net_benefit_pln
    right_value = right.expected_marginal_net_benefit_pln
    if left_value == right_value:
        return None, ReasonCode.ECONOMIC_TIE
    winner = left if left_value > right_value else right
    return winner, ReasonCode.ECONOMIC_CANDIDATE


def _candidate_projection(
    candidate: PolicyCandidate,
    temporal_status: TemporalStatus,
    rejection_reason: ReasonCode | None,
    *,
    semantic_only: bool,
) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for field_name, consumed_by_arbitration in _CANDIDATE_PROJECTION_FIELDS:
        if semantic_only and not consumed_by_arbitration:
            continue
        if field_name == "temporal_status":
            value: Any = temporal_status
        elif field_name == "rejection_reason":
            value = rejection_reason
        else:
            value = getattr(candidate, field_name)
        if field_name in {
            "requested_power_kw",
            "requested_energy_kwh",
            "target_soc_percent",
            "protected_soc_floor_percent",
            "expected_marginal_net_benefit_pln",
            "urgency",
            "severity",
        }:
            value = _normalize_number(value)
        if (
            semantic_only
            and field_name == "expected_marginal_net_benefit_pln"
            and candidate.economic_value_status
            is not EconomicValueStatus.COMPARABLE
        ):
            value = None
        projection[field_name] = value
    return projection


def _candidate_summary(
    candidate: PolicyCandidate,
    temporal_status: TemporalStatus,
    rejection_reason: ReasonCode | None,
) -> CandidateSummary:
    return CandidateSummary(
        **_candidate_projection(
            candidate,
            temporal_status,
            rejection_reason,
            semantic_only=False,
        )
    )


def _build_candidate_outputs(
    candidates: tuple[PolicyCandidate, ...],
    temporal_statuses: dict[PolicyId, TemporalStatus],
    rejection_by_policy: dict[PolicyId, ReasonCode | None],
) -> tuple[tuple[CandidateSummary, ...], tuple[CandidateRejection, ...]]:
    summaries = tuple(
        _candidate_summary(
            candidate,
            temporal_statuses[candidate.policy_id],
            rejection_by_policy[candidate.policy_id],
        )
        for candidate in candidates
    )
    rejections = tuple(
        CandidateRejection(policy_id, reason)
        for policy_id, reason in sorted(
            rejection_by_policy.items(), key=lambda pair: pair[0].value
        )
        if reason is not None
    )
    return summaries, rejections


def _semantic_payload(
    mode: SupervisorMode,
    profile: SupervisorProfile,
    context: ExecutionContext,
    candidates: tuple[PolicyCandidate, ...],
    temporal_statuses: dict[PolicyId, TemporalStatus],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_table_version": PROFILE_TABLE_VERSION,
        "supervisor_mode": mode,
        "profile": profile,
        "execution_context": {
            # observed_at is intentionally excluded once accepted as non-future.
            "physical_mode": context.physical_mode,
            "physical_mode_fresh": context.physical_mode_fresh,
            "owner_kind": context.owner_kind,
            "owner_conflict": context.owner_conflict,
            "transaction_pending": context.transaction_pending,
            "transaction_owner_kind": context.transaction_owner_kind,
            "full_block_execution_ready": context.full_block_execution_ready,
            "direct_306_execution_ready": context.direct_306_execution_ready,
            "direct_259_execution_ready": context.direct_259_execution_ready,
            "topology_full_block_allowed": context.topology_full_block_allowed,
            "topology_direct_register_allowed": context.topology_direct_register_allowed,
            "charge_direction_ready": context.charge_direction_ready,
            "discharge_direction_ready": context.discharge_direction_ready,
            "critical_bms_ready": context.critical_bms_ready,
            "export_state": context.export_state,
        },
        "candidates": [
            {
                "schema_version": candidate.schema_version,
                **_candidate_projection(
                    candidate,
                    temporal_statuses[candidate.policy_id],
                    None,
                    semantic_only=True,
                ),
            }
            for candidate in candidates
        ],
    }


def _invalid_decision(
    mode: SupervisorMode,
    profile: SupervisorProfile,
    reason: ReasonCode,
) -> SupervisorDecision:
    profile_definition = _PROFILE_TABLE[profile]
    revision = _sha256_json(
        {
            "schema_version": SCHEMA_VERSION,
            "profile_table_version": PROFILE_TABLE_VERSION,
            "supervisor_mode": mode,
            "profile": profile,
            "structural_error": reason,
        }
    )
    return SupervisorDecision(
        supervisor_mode=mode,
        profile=profile,
        state=SupervisorState.BLOCKED,
        execution_phase=ExecutionPhase.BLOCKED,
        selected_policy=None,
        selected_candidate_revision=None,
        selection_kind=SelectionKind.BLOCKED,
        selection_reason=reason,
        execution_blocked_reason=reason,
        arbitration_revision=revision,
        supervisor_execution_authorized=False,
        legacy_execution_unchanged=True,
        candidate_summaries=(),
        rejected_reasons=(),
        profile_effects_applied=profile_definition.effects_applied,
        profile_effects_not_applied=profile_definition.effects_not_applied,
    )


def arbitrate_supervisor(
    *,
    mode: SupervisorMode,
    profile: SupervisorProfile,
    context: ExecutionContext,
    candidates: Sequence[PolicyCandidate],
    now: datetime,
) -> SupervisorDecision:
    """Return a deterministic Off/Shadow decision without execution authority."""
    if not isinstance(mode, SupervisorMode):
        raise ValueError("mode must be SupervisorMode")
    if not isinstance(profile, SupervisorProfile):
        raise ValueError("profile must be SupervisorProfile")
    if not _is_aware_datetime(now):
        raise ValueError("now must be a timezone-aware datetime")
    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        return _invalid_decision(mode, profile, ReasonCode.INVALID_INPUT)
    try:
        candidate_count = len(candidates)
    except (OverflowError, TypeError, ValueError):
        return _invalid_decision(mode, profile, ReasonCode.INVALID_INPUT)
    if candidate_count > MAX_CANDIDATES:
        return _invalid_decision(mode, profile, ReasonCode.INVALID_INPUT)
    try:
        raw_candidates = tuple(
            candidates[index] for index in range(candidate_count)
        )
    except (IndexError, TypeError):
        return _invalid_decision(mode, profile, ReasonCode.INVALID_INPUT)
    context_error = _validate_context(context, now)
    if context_error is not None:
        return _invalid_decision(mode, profile, context_error)
    candidate_errors = tuple(
        error
        for error in (_validate_candidate(candidate) for candidate in raw_candidates)
        if error is not None
    )
    if candidate_errors:
        return _invalid_decision(
            mode,
            profile,
            min(candidate_errors, key=lambda error: error.value),
        )
    policy_ids = tuple(candidate.policy_id for candidate in raw_candidates)
    if len(set(policy_ids)) != len(policy_ids):
        return _invalid_decision(mode, profile, ReasonCode.INVALID_INPUT)
    normalized_candidates = tuple(
        sorted(raw_candidates, key=lambda candidate: candidate.policy_id.value)
    )
    consistency_error = _validate_owner_commitment_consistency(
        context,
        normalized_candidates,
    )
    if consistency_error is not None:
        return _invalid_decision(mode, profile, consistency_error)

    temporal_statuses = {
        candidate.policy_id: _temporal_status(candidate, now)
        for candidate in normalized_candidates
    }
    rejection_by_policy = {
        candidate.policy_id: _candidate_rejection(
            candidate,
            context,
            temporal_statuses[candidate.policy_id],
        )
        for candidate in normalized_candidates
    }
    eligible = tuple(
        candidate
        for candidate in normalized_candidates
        if rejection_by_policy[candidate.policy_id] is None
    )
    revision = _sha256_json(
        _semantic_payload(
            mode,
            profile,
            context,
            normalized_candidates,
            temporal_statuses,
        )
    )
    global_blocker = _global_execution_blocker(context)
    profile_definition = _PROFILE_TABLE[profile]
    observed_phase = (
        ExecutionPhase.OBSERVED_ACTIVE_LATCHED
        if any(candidate.active_latched for candidate in normalized_candidates)
        else ExecutionPhase.IDLE
    )

    if mode is SupervisorMode.OFF:
        summaries, rejections = _build_candidate_outputs(
            normalized_candidates,
            temporal_statuses,
            rejection_by_policy,
        )
        return SupervisorDecision(
            supervisor_mode=mode,
            profile=profile,
            state=SupervisorState.OFF,
            execution_phase=observed_phase,
            selected_policy=None,
            selected_candidate_revision=None,
            selection_kind=SelectionKind.OFF,
            selection_reason=ReasonCode.NO_ACTION,
            execution_blocked_reason=None,
            arbitration_revision=revision,
            supervisor_execution_authorized=False,
            legacy_execution_unchanged=True,
            candidate_summaries=summaries,
            rejected_reasons=rejections,
            profile_effects_applied=profile_definition.effects_applied,
            profile_effects_not_applied=profile_definition.effects_not_applied,
        )

    if mode is SupervisorMode.ACTIVE:
        summaries, rejections = _build_candidate_outputs(
            normalized_candidates,
            temporal_statuses,
            rejection_by_policy,
        )
        return SupervisorDecision(
            supervisor_mode=mode,
            profile=profile,
            state=SupervisorState.BLOCKED,
            execution_phase=ExecutionPhase.BLOCKED,
            selected_policy=None,
            selected_candidate_revision=None,
            selection_kind=SelectionKind.ACTIVE_NOT_IMPLEMENTED,
            selection_reason=ReasonCode.ACTIVE_NOT_IMPLEMENTED,
            execution_blocked_reason=ReasonCode.ACTIVE_NOT_IMPLEMENTED,
            arbitration_revision=revision,
            supervisor_execution_authorized=False,
            legacy_execution_unchanged=True,
            candidate_summaries=summaries,
            rejected_reasons=rejections,
            profile_effects_applied=profile_definition.effects_applied,
            profile_effects_not_applied=profile_definition.effects_not_applied,
        )

    winner, selection_reason = _select_shadow_winner(eligible)
    if selection_reason is ReasonCode.INCONSISTENT_PRIORITY_TIE:
        return _invalid_decision(mode, profile, selection_reason)
    if (
        winner is None
        and selection_reason
        in {
            ReasonCode.ECONOMIC_CANDIDATES_NOT_COMPARABLE,
            ReasonCode.ECONOMIC_TIE,
        }
    ):
        for candidate in eligible:
            if candidate.priority_class is PriorityClass.ECONOMIC:
                rejection_by_policy[candidate.policy_id] = selection_reason
    summaries, rejections = _build_candidate_outputs(
        normalized_candidates,
        temporal_statuses,
        rejection_by_policy,
    )
    if winner is None:
        state = SupervisorState.SHADOW_IDLE
        selection_kind = SelectionKind.NONE
        selected_policy = None
        selected_revision = None
    else:
        state = SupervisorState.SHADOW_SELECTED
        selection_kind = (
            SelectionKind.PRESERVED_COMMITMENT
            if winner.active_latched
            else SelectionKind.SHADOW
        )
        selected_policy = winner.policy_id
        selected_revision = winner.candidate_revision
    return SupervisorDecision(
        supervisor_mode=mode,
        profile=profile,
        state=state,
        execution_phase=observed_phase,
        selected_policy=selected_policy,
        selected_candidate_revision=selected_revision,
        selection_kind=selection_kind,
        selection_reason=selection_reason,
        execution_blocked_reason=global_blocker,
        arbitration_revision=revision,
        supervisor_execution_authorized=False,
        legacy_execution_unchanged=True,
        candidate_summaries=summaries,
        rejected_reasons=rejections,
        profile_effects_applied=profile_definition.effects_applied,
        profile_effects_not_applied=profile_definition.effects_not_applied,
    )


def _valid_candidate_summary(summary: Any) -> bool:
    if type(summary) is not CandidateSummary:
        return False
    if not isinstance(summary.policy_id, PolicyId):
        return False
    if not _is_revision(summary.input_revision) or not _is_revision(
        summary.candidate_revision
    ):
        return False
    if not isinstance(summary.temporal_status, TemporalStatus):
        return False
    boolean_fields = (
        summary.allowed_by_user,
        summary.enabled,
        summary.available,
        summary.result_current,
        summary.recalculation_pending,
        summary.start_eligible,
        summary.continuation_eligible,
        summary.active_latched,
        summary.local_hard_stop,
    )
    if not all(_is_exact_bool(value) for value in boolean_fields):
        return False
    enum_fields = (
        (summary.requested_action, RequestedAction),
        (summary.actuator_scope, ActuatorScope),
        (summary.priority_class, PriorityClass),
        (summary.need_class, NeedClass),
        (summary.reason_code, ReasonCode),
        (summary.economic_value_status, EconomicValueStatus),
    )
    if any(not isinstance(value, expected) for value, expected in enum_fields):
        return False
    for reason in (summary.blocked_reason, summary.rejection_reason):
        if reason is not None and not isinstance(reason, ReasonCode):
            return False
    if summary.requested_mode is not None and not isinstance(
        summary.requested_mode, PhysicalMode
    ):
        return False
    for instant in (summary.valid_from, summary.valid_until):
        if instant is not None and not _is_aware_datetime(instant):
            return False
    if (
        summary.valid_from is not None
        and summary.valid_until is not None
        and _utc(summary.valid_until) <= _utc(summary.valid_from)
    ):
        return False
    shape = (
        summary.policy_id,
        summary.priority_class,
        summary.need_class,
        summary.requested_action,
        summary.actuator_scope,
    )
    if shape not in _ALLOWED_POLICY_SHAPES:
        return False
    if summary.requested_action is RequestedAction.NONE:
        if summary.desired_actuator_fingerprint is not None:
            return False
    elif not _valid_sha256(summary.desired_actuator_fingerprint):
        return False
    return _valid_optional_numeric_fields(summary) and _valid_economic_fields(summary)


def _valid_decision(decision: Any) -> bool:
    if type(decision) is not SupervisorDecision:
        return False
    enum_fields = (
        (decision.supervisor_mode, SupervisorMode),
        (decision.profile, SupervisorProfile),
        (decision.state, SupervisorState),
        (decision.execution_phase, ExecutionPhase),
        (decision.selection_kind, SelectionKind),
        (decision.selection_reason, ReasonCode),
    )
    if any(not isinstance(value, expected) for value, expected in enum_fields):
        return False
    if decision.execution_blocked_reason is not None and not isinstance(
        decision.execution_blocked_reason, ReasonCode
    ):
        return False
    if decision.selected_policy is not None and not isinstance(
        decision.selected_policy, PolicyId
    ):
        return False
    if decision.selected_candidate_revision is not None and not _is_revision(
        decision.selected_candidate_revision
    ):
        return False
    if not _valid_sha256(decision.arbitration_revision):
        return False
    if decision.supervisor_execution_authorized is not False:
        return False
    if decision.legacy_execution_unchanged is not True:
        return False
    if type(decision.candidate_summaries) is not tuple:
        return False
    if not 0 <= len(decision.candidate_summaries) <= MAX_CANDIDATES:
        return False
    if not all(_valid_candidate_summary(item) for item in decision.candidate_summaries):
        return False
    policy_ids = tuple(item.policy_id for item in decision.candidate_summaries)
    if policy_ids != tuple(sorted(policy_ids, key=lambda policy: policy.value)):
        return False
    if len(set(policy_ids)) != len(policy_ids):
        return False
    if type(decision.rejected_reasons) is not tuple or not all(
        type(item) is CandidateRejection
        and isinstance(item.policy_id, PolicyId)
        and isinstance(item.reason, ReasonCode)
        for item in decision.rejected_reasons
    ):
        return False
    expected_rejections = tuple(
        CandidateRejection(summary.policy_id, summary.rejection_reason)
        for summary in decision.candidate_summaries
        if summary.rejection_reason is not None
    )
    if decision.rejected_reasons != expected_rejections:
        return False
    profile_definition = _PROFILE_TABLE[decision.profile]
    if decision.profile_effects_applied != profile_definition.effects_applied:
        return False
    if decision.profile_effects_not_applied != profile_definition.effects_not_applied:
        return False
    if not all(
        isinstance(effect, ProfileEffect)
        for effect in (
            *decision.profile_effects_applied,
            *decision.profile_effects_not_applied,
        )
    ):
        return False
    observed_phase = (
        ExecutionPhase.OBSERVED_ACTIVE_LATCHED
        if any(summary.active_latched for summary in decision.candidate_summaries)
        else ExecutionPhase.IDLE
    )

    structural_reasons = {
        ReasonCode.INVALID_INPUT,
        ReasonCode.INVALID_POLICY_SHAPE,
        ReasonCode.INVALID_ACTION_SCOPE,
        ReasonCode.STRUCTURALLY_INCONSISTENT_CONTEXT,
        ReasonCode.INVALID_PENDING_OWNER_RELATIONSHIP,
        ReasonCode.MULTIPLE_ACTIVE_COMMITMENTS,
        ReasonCode.OWNER_COMMITMENT_MISMATCH,
        ReasonCode.INCONSISTENT_PRIORITY_TIE,
    }
    if decision.selection_kind is SelectionKind.BLOCKED:
        return (
            decision.state is SupervisorState.BLOCKED
            and decision.execution_phase is ExecutionPhase.BLOCKED
            and decision.selected_policy is None
            and decision.selected_candidate_revision is None
            and decision.selection_reason in structural_reasons
            and decision.execution_blocked_reason is decision.selection_reason
            and not decision.candidate_summaries
            and not decision.rejected_reasons
        )
    if decision.supervisor_mode is SupervisorMode.OFF:
        return (
            decision.state is SupervisorState.OFF
            and decision.execution_phase is observed_phase
            and decision.selected_policy is None
            and decision.selected_candidate_revision is None
            and decision.selection_kind is SelectionKind.OFF
            and decision.selection_reason is ReasonCode.NO_ACTION
            and decision.execution_blocked_reason is None
        )
    if decision.supervisor_mode is SupervisorMode.ACTIVE:
        return (
            decision.state is SupervisorState.BLOCKED
            and decision.execution_phase is ExecutionPhase.BLOCKED
            and decision.selected_policy is None
            and decision.selected_candidate_revision is None
            and decision.selection_kind is SelectionKind.ACTIVE_NOT_IMPLEMENTED
            and decision.selection_reason is ReasonCode.ACTIVE_NOT_IMPLEMENTED
            and decision.execution_blocked_reason is ReasonCode.ACTIVE_NOT_IMPLEMENTED
        )

    blockers = {
        None,
        ReasonCode.OWNER_CONFLICT,
        ReasonCode.OFF_GRID,
        ReasonCode.MANUAL_AUTHORITY,
        ReasonCode.BALANCING_ACTIVE,
        ReasonCode.FOREIGN_OWNER,
        ReasonCode.TRANSACTION_PENDING,
        ReasonCode.PHYSICAL_MODE_STALE,
        ReasonCode.PHYSICAL_MODE_UNKNOWN,
        ReasonCode.CRITICAL_BMS_UNAVAILABLE,
    }
    if (
        decision.supervisor_mode is not SupervisorMode.SHADOW
        or decision.execution_phase is not observed_phase
        or decision.execution_blocked_reason not in blockers
    ):
        return False
    if decision.selection_kind is SelectionKind.NONE:
        return (
            decision.state is SupervisorState.SHADOW_IDLE
            and decision.selected_policy is None
            and decision.selected_candidate_revision is None
            and decision.selection_reason
            in {
                ReasonCode.NO_ELIGIBLE_CANDIDATE,
                ReasonCode.ECONOMIC_CANDIDATES_NOT_COMPARABLE,
                ReasonCode.ECONOMIC_TIE,
            }
            and all(
                summary.rejection_reason is not None
                for summary in decision.candidate_summaries
            )
            and (
                decision.selection_reason is ReasonCode.NO_ELIGIBLE_CANDIDATE
                or sum(
                    summary.rejection_reason is decision.selection_reason
                    for summary in decision.candidate_summaries
                )
                >= 2
            )
        )
    if decision.selection_kind not in {
        SelectionKind.SHADOW,
        SelectionKind.PRESERVED_COMMITMENT,
    }:
        return False
    selected = next(
        (
            summary
            for summary in decision.candidate_summaries
            if summary.policy_id is decision.selected_policy
        ),
        None,
    )
    if selected is None:
        return False
    eligible_selection = (
        selected.allowed_by_user
        and selected.enabled
        and selected.available
        and selected.temporal_status is TemporalStatus.VALID
        and not selected.local_hard_stop
        and selected.requested_action is not RequestedAction.NONE
        and (
            (
                selected.active_latched
                and selected.continuation_eligible
                and (selected.result_current or selected.recalculation_pending)
            )
            or (
                not selected.active_latched
                and selected.start_eligible
                and selected.result_current
                and not selected.recalculation_pending
            )
        )
    )
    return (
        decision.state is SupervisorState.SHADOW_SELECTED
        and decision.selected_candidate_revision == selected.candidate_revision
        and selected.rejection_reason is None
        and eligible_selection
        and decision.selection_reason
        is {
            PriorityClass.LIVE_EMERGENCY: ReasonCode.LIVE_EMERGENCY,
            PriorityClass.REQUIRED_ENERGY: ReasonCode.REQUIRED_ENERGY_RESTORE,
            PriorityClass.PREVENTIVE_GRID: ReasonCode.PREVENTIVE_VOLTAGE_ACTION,
            PriorityClass.ECONOMIC: ReasonCode.ECONOMIC_CANDIDATE,
        }.get(selected.priority_class)
        and (
            decision.selection_kind is SelectionKind.PRESERVED_COMMITMENT
        )
        is selected.active_latched
    )


def _candidate_summary_payload(summary: CandidateSummary) -> dict[str, Any]:
    return {
        field_name: getattr(summary, field_name)
        for field_name, _consumed_by_arbitration in _CANDIDATE_PROJECTION_FIELDS
    }


def serialize_supervisor_summary(decision: SupervisorDecision) -> str:
    """Serialize a bounded deterministic summary for a future HA sensor."""
    if not _valid_decision(decision):
        raise ValueError("decision violates the Phase 1A public contract")
    candidate_payloads = [
        _candidate_summary_payload(summary)
        for summary in decision.candidate_summaries
    ]
    for payload in candidate_payloads:
        size = len(_canonical_json(payload).encode("utf-8"))
        if size > MAX_CANDIDATE_SUMMARY_BYTES:
            raise ValueError("candidate summary exceeds the bounded contract")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "supervisor_mode": decision.supervisor_mode,
        "profile": decision.profile,
        "state": decision.state,
        "execution_phase": decision.execution_phase,
        "selected_policy": decision.selected_policy,
        "selected_candidate_revision": decision.selected_candidate_revision,
        "selection_kind": decision.selection_kind,
        "selection_reason": decision.selection_reason,
        "execution_blocked_reason": decision.execution_blocked_reason,
        "arbitration_revision": decision.arbitration_revision,
        "supervisor_execution_authorized": (
            decision.supervisor_execution_authorized
        ),
        "legacy_execution_unchanged": decision.legacy_execution_unchanged,
        "candidate_summaries": candidate_payloads,
        "rejected_reasons": [
            {
                "policy_id": rejection.policy_id,
                "reason": rejection.reason,
            }
            for rejection in decision.rejected_reasons
        ],
        "profile_table_version": PROFILE_TABLE_VERSION,
        "profile_effects_applied": decision.profile_effects_applied,
        "profile_effects_not_applied": decision.profile_effects_not_applied,
    }
    serialized = _canonical_json(payload)
    if len(serialized.encode("utf-8")) > MAX_SUPERVISOR_SUMMARY_BYTES:
        raise ValueError("Supervisor summary exceeds the bounded contract")
    return serialized


__all__ = (
    "ActuatorScope",
    "CandidateRejection",
    "CandidateSummary",
    "EconomicValueStatus",
    "ExecutionContext",
    "ExecutionPhase",
    "ExportState",
    "MAX_CANDIDATES",
    "MAX_CANDIDATE_SUMMARY_BYTES",
    "MAX_SUPERVISOR_SUMMARY_BYTES",
    "NeedClass",
    "OwnerKind",
    "PROFILE_TABLE_VERSION",
    "PhysicalMode",
    "PolicyCandidate",
    "PolicyId",
    "PriorityClass",
    "ProfileEffect",
    "ReasonCode",
    "RequestedAction",
    "SCHEMA_VERSION",
    "SelectionKind",
    "SupervisorDecision",
    "SupervisorMode",
    "SupervisorProfile",
    "SupervisorState",
    "TemporalStatus",
    "arbitrate_supervisor",
    "serialize_supervisor_summary",
)
