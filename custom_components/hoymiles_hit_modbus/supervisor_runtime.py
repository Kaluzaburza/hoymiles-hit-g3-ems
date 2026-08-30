"""Pure bounded normalization for EMS Supervisor policy snapshots."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
from typing import Any

try:  # Package import at runtime; direct import in deterministic tests.
    from .ems_supervisor import (
        ActuatorScope,
        EconomicValueStatus,
        ExecutionContext,
        ExportState,
        NeedClass,
        OwnerKind,
        PhysicalMode,
        PolicyCandidate,
        PolicyId,
        PriorityClass,
        ReasonCode,
        RequestedAction,
        SCHEMA_VERSION,
    )
except ImportError:  # pragma: no cover - direct tools execution
    from ems_supervisor import (
        ActuatorScope,
        EconomicValueStatus,
        ExecutionContext,
        ExportState,
        NeedClass,
        OwnerKind,
        PhysicalMode,
        PolicyCandidate,
        PolicyId,
        PriorityClass,
        ReasonCode,
        RequestedAction,
        SCHEMA_VERSION,
    )


_UINT64_MAX = 18_446_744_073_709_551_615
_INT64_MIN = -9_223_372_036_854_775_808
_INT64_MAX = 9_223_372_036_854_775_807
_MAX_SOURCE_NUMBER = 1_000_000_000.0
_RCE_PLAN_MAX_AGE_SECONDS = 300.0
_TARIFF_PLAN_MAX_AGE_SECONDS = 300.0
_RCM_PLAN_MAX_AGE_SECONDS = 60.0
_PHYSICAL_MAX_AGE_SECONDS = 180.0
_TOPOLOGY_MAX_AGE_SECONDS = 180.0
_SOC_MAX_AGE_SECONDS = 120.0
_BMS_MAX_AGE_SECONDS = 300.0
_EXPORT_MAX_AGE_SECONDS = 180.0
_RCE_MIN_REMAINING_SECONDS = 300.0
_TARIFF_MIN_REMAINING_SECONDS = 420.0
_READBACK_TOLERANCE = 0.5
_LOWER_TARGET_EPSILON = 1e-6

class RcePlanStatus(str, Enum):
    """RCE plan states accepted by the current execution contract."""

    READY = "ready"
    WAITING_FOR_MARKET = "waiting_for_market"
    HOME_PROTECTED = "home_protected"


class TariffPlanStatus(str, Enum):
    """Tariff plan states accepted by the current execution contract."""

    READY = "ready"
    INSUFFICIENT_CHEAP_WINDOW = "insufficient_cheap_window"


class TariffRunNeed(str, Enum):
    """Optimizer-owned origin of one current continuous tariff run."""

    REQUIRED_ENERGY = "required_energy"
    ECONOMIC = "economic"
    MIXED = "mixed"
    NONE = "none"


class TariffAction(str, Enum):
    """Bounded tariff action reported for the current run."""

    GRID_SUPPORT = "grid_support"
    BATTERY_CHARGE = "battery_charge"
    GRID_SUPPORT_AND_CHARGE = "grid_support_and_charge"
    NONE = "none"


class RcmAction(str, Enum):
    """Bounded optimizer recommendation used by the RCEm adapter."""

    ABSORB_PV = "absorb_pv"
    LIMIT_EXPORT = "limit_export"
    GRID_DISCHARGE_PREPARATION = "grid_discharge_preparation"
    MONITOR = "monitor"
    HOLD = "hold"
    RESTORE = "restore"
    RELEASE_EXPORT = "release_export"
    PRESERVE_HEADROOM = "preserve_headroom"
    UNKNOWN = "unknown"


_RCE_READY_STATUSES = frozenset(RcePlanStatus)
_TARIFF_READY_STATUSES = frozenset(TariffPlanStatus)
_TARIFF_ACTIONS = frozenset(
    {
        TariffAction.GRID_SUPPORT,
        TariffAction.BATTERY_CHARGE,
        TariffAction.GRID_SUPPORT_AND_CHARGE,
    }
)
_RCM_NO_ACTIONS = frozenset(
    {
        RcmAction.MONITOR,
        RcmAction.HOLD,
        RcmAction.RESTORE,
        RcmAction.RELEASE_EXPORT,
        RcmAction.PRESERVE_HEADROOM,
        RcmAction.UNKNOWN,
    }
)


@dataclass(frozen=True, slots=True)
class RceSourceSnapshot:
    """Bounded RCE facts already extracted from one physical system."""

    observed_at: datetime | None = None
    allowed_by_user: bool | None = None
    enabled: bool | None = None
    active_latched: bool | None = None
    status_code: RcePlanStatus | None = None
    result_current: bool | None = None
    recalculation_pending: bool | None = None
    input_revision: int | None = None
    current_slot_planned: bool | None = None
    current_slot_start_eligible: bool | None = None
    current_slot_continue_eligible: bool | None = None
    current_slot_end: datetime | None = None
    current_run_end: datetime | None = None
    requested_discharge_power_kw: int | float | None = None
    planned_export_energy_kwh: int | float | None = None
    protected_soc_floor_percent: int | float | None = None
    effective_discharge_power_percent: int | float | None = None
    current_soc_percent: int | float | None = None
    control_data_ready: bool | None = None
    price_above_threshold: bool | None = None
    reserve_ready: bool | None = None
    sale_block_active: bool | None = None
    latched_slot_end: datetime | None = None
    latched_minimum_soc_percent: int | float | None = None
    active_4305_readback_percent: int | float | None = None
    active_4306_readback_percent: int | float | None = None


@dataclass(frozen=True, slots=True)
class TariffSourceSnapshot:
    """Bounded tariff facts including optimizer-owned run provenance."""

    observed_at: datetime | None = None
    allowed_by_user: bool | None = None
    enabled: bool | None = None
    active_latched: bool | None = None
    status_code: TariffPlanStatus | None = None
    result_current: bool | None = None
    recalculation_pending: bool | None = None
    input_revision: int | None = None
    current_slot_planned: bool | None = None
    current_action: TariffAction | None = None
    current_run_need_class: TariffRunNeed | None = None
    current_run_start_eligible: bool | None = None
    current_run_continue_eligible: bool | None = None
    requested_charge_power_kw: int | float | None = None
    command_charge_power_percent: int | float | None = None
    current_run_grid_import_kwh: int | float | None = None
    current_run_benefit_pln: int | float | None = None
    target_soc_percent: int | float | None = None
    base_reserve_soc_percent: int | float | None = None
    current_slot_end: datetime | None = None
    active_action: TariffAction | None = None
    latched_slot_end: datetime | None = None
    latched_target_soc_percent: int | float | None = None
    control_data_ready: bool | None = None
    planned_slot_ready: bool | None = None
    active_4303_readback_percent: int | float | None = None
    active_4304_readback_percent: int | float | None = None


@dataclass(frozen=True, slots=True)
class RcmSourceSnapshot:
    """Bounded RCEm facts; the internal policy identity remains RCM."""

    observed_at: datetime | None = None
    allowed_by_user: bool | None = None
    enabled: bool | None = None
    result_current: bool | None = None
    recalculation_pending: bool | None = None
    input_revision: int | None = None
    live_emergency: bool | None = None
    emergency_action_ready: bool | None = None
    prediction_ready: bool | None = None
    action: RcmAction | None = None
    risk_window_active: bool | None = None
    voltage_risk_score_percent: int | float | None = None
    recommended_charge_limit_percent: int | float | None = None
    recommended_charge_power_kw: int | float | None = None
    recommended_export_limit_percent: int | float | None = None
    current_export_limit_percent: int | float | None = None
    current_export_limit_fresh: bool | None = None
    charge_path_locally_valid: bool | None = None
    export_path_locally_valid: bool | None = None
    direct_register_topology_allowed: bool | None = None
    full_block_topology_allowed: bool | None = None
    export_control_enabled: bool | None = None
    pre_discharge_enabled: bool | None = None
    absorb_active: bool | None = None
    export_active: bool | None = None
    pre_discharge_active: bool | None = None
    pre_discharge_start_eligible: bool | None = None
    pre_discharge_continue_eligible: bool | None = None
    pre_discharge_deadline: datetime | None = None
    pre_discharge_target_soc_percent: int | float | None = None
    pre_discharge_power_kw: int | float | None = None
    pre_discharge_power_percent: int | float | None = None
    planned_grid_discharge_kwh: int | float | None = None
    target_soc_before_risk_percent: int | float | None = None
    protected_minimum_soc_percent: int | float | None = None
    latched_pre_discharge_deadline: datetime | None = None
    latched_pre_discharge_target_soc_percent: int | float | None = None
    latched_pre_discharge_power_kw: int | float | None = None
    latched_pre_discharge_power_percent: int | float | None = None
    sale_block_active: bool | None = None
    export_state: ExportState | None = None


@dataclass(frozen=True, slots=True)
class ExecutionSourceSnapshot:
    """One immutable physical, ownership and readiness snapshot."""

    physical_mode_code: int | float | None = None
    full_block_generation_at: datetime | None = None
    full_block_execution_ready: bool | None = None
    direct_306_execution_ready: bool | None = None
    direct_259_execution_ready: bool | None = None
    machine_type_code: int | float | None = None
    inverter_count: int | float | None = None
    topology_generation_at: datetime | None = None
    battery_soc_percent: int | float | None = None
    battery_soc_observed_at: datetime | None = None
    bms_voltage_v: int | float | None = None
    bms_voltage_observed_at: datetime | None = None
    bms_max_charge_current_a: int | float | None = None
    bms_charge_current_observed_at: datetime | None = None
    bms_max_discharge_current_a: int | float | None = None
    bms_discharge_current_observed_at: datetime | None = None
    balancing_active: bool | None = None
    manual_charge_active: bool | None = None
    manual_discharge_active: bool | None = None
    rce_active: bool | None = None
    tariff_active: bool | None = None
    rcm_active: bool | None = None
    rcm_export_control_active: bool | None = None
    rcm_pre_discharge_active: bool | None = None
    charge_timer_active: bool | None = None
    discharge_timer_active: bool | None = None
    gcf_enable_code: int | float | None = None
    effective_export_limit_percent: int | float | None = None
    gcf_generation_at: datetime | None = None
    gcf_cohort_coherent: bool | None = None
    hardware_readback_supported: bool | None = None


@dataclass(frozen=True, slots=True)
class NormalizedOwner:
    """Normalized writer family and independently reported conflict flag."""

    owner_kind: OwnerKind
    owner_conflict: bool


def _is_aware(value: Any) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _require_now(now: Any) -> datetime:
    if not _is_aware(now) or now.utcoffset().total_seconds() != 0:
        raise ValueError("now must be an aware UTC datetime")
    return _utc(now)


def _observed_at(value: Any, now: datetime) -> datetime:
    return _utc(value) if _is_aware(value) else now


def _is_exact_bool(value: Any) -> bool:
    return type(value) is bool


def _bool_value(value: Any, fallback: bool) -> bool:
    return value if _is_exact_bool(value) else fallback


def _is_revision(value: Any) -> bool:
    return type(value) is int and 0 <= value <= _UINT64_MAX


def _revision_value(value: Any) -> int:
    return value if _is_revision(value) else 0


def _number(
    value: Any,
    *,
    minimum: float = -_MAX_SOURCE_NUMBER,
    maximum: float = _MAX_SOURCE_NUMBER,
) -> float | None:
    try:
        if type(value) is int:
            if not _INT64_MIN <= value <= _INT64_MAX:
                return None
            normalized = float(value)
        elif type(value) is float:
            normalized = value
        else:
            return None
    except (OverflowError, TypeError, ValueError):
        return None
    if not math.isfinite(normalized) or not minimum <= normalized <= maximum:
        return None
    return 0.0 if normalized == 0.0 else normalized


def _percent(value: Any) -> float | None:
    return _number(value, minimum=0.0, maximum=100.0)


def _fresh(value: Any, *, now: datetime, maximum_age_seconds: float) -> bool:
    if not _is_aware(value):
        return False
    age = (now - _utc(value)).total_seconds()
    return 0.0 <= age <= maximum_age_seconds


def _future(value: Any, now: datetime) -> bool:
    return _is_aware(value) and _utc(value) > now


def _remaining_seconds(value: Any, now: datetime) -> float | None:
    if not _is_aware(value):
        return None
    return (_utc(value) - now).total_seconds()


def _close(left: float | None, right: float | None) -> bool:
    return (
        left is not None
        and right is not None
        and abs(left - right) < _READBACK_TOLERANCE
    )


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _utc(value).isoformat().replace("+00:00", "Z")
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
            raise ValueError("non-finite canonical value")
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


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _candidate_revision(value: Any) -> int:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=False)


def _semantic_revision_projection(candidate: PolicyCandidate) -> dict[str, Any]:
    """Return only facts consumed by Phase 1A semantic arbitration."""
    comparable = (
        candidate.economic_value_status is EconomicValueStatus.COMPARABLE
    )
    return {
        "schema_version": candidate.schema_version,
        "policy_id": candidate.policy_id,
        "allowed_by_user": candidate.allowed_by_user,
        "enabled": candidate.enabled,
        "available": candidate.available,
        "result_current": candidate.result_current,
        "recalculation_pending": candidate.recalculation_pending,
        "input_revision": candidate.input_revision,
        "start_eligible": candidate.start_eligible,
        "continuation_eligible": candidate.continuation_eligible,
        "active_latched": candidate.active_latched,
        "local_hard_stop": candidate.local_hard_stop,
        "requested_action": candidate.requested_action,
        "actuator_scope": candidate.actuator_scope,
        "priority_class": candidate.priority_class,
        "need_class": candidate.need_class,
        "reason_code": candidate.reason_code,
        "blocked_reason": candidate.blocked_reason,
        "valid_from": candidate.valid_from,
        "valid_until": candidate.valid_until,
        "economic_value_status": candidate.economic_value_status,
        "economic_contract_id": (
            candidate.economic_contract_id if comparable else None
        ),
        "economic_basis_fingerprint": (
            candidate.economic_basis_fingerprint if comparable else None
        ),
        "expected_marginal_net_benefit_pln": (
            candidate.expected_marginal_net_benefit_pln
            if comparable
            else None
        ),
        "desired_actuator_fingerprint": (
            candidate.desired_actuator_fingerprint
        ),
    }


def _finalize(candidate: PolicyCandidate) -> PolicyCandidate:
    return replace(
        candidate,
        candidate_revision=_candidate_revision(
            _semantic_revision_projection(candidate)
        ),
    )


def _common_candidate_values(source: Any, now: datetime) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": _observed_at(getattr(source, "observed_at", None), now),
        "allowed_by_user": getattr(source, "allowed_by_user", None) is True,
        "enabled": getattr(source, "enabled", None) is True,
        "result_current": _bool_value(
            getattr(source, "result_current", None), False
        ),
        "recalculation_pending": _bool_value(
            getattr(source, "recalculation_pending", None), True
        ),
        "input_revision": _revision_value(
            getattr(source, "input_revision", None)
        ),
    }


def _plan_contract_valid(source: Any) -> bool:
    return (
        _is_exact_bool(getattr(source, "result_current", None))
        and _is_exact_bool(getattr(source, "recalculation_pending", None))
        and _is_revision(getattr(source, "input_revision", None))
    )


def _no_action(
    policy_id: PolicyId,
    source: Any,
    *,
    now: datetime,
    available: bool,
    blocked_reason: ReasonCode | None = None,
    active_latched: bool = False,
    local_hard_stop: bool = False,
) -> PolicyCandidate:
    candidate = PolicyCandidate(
        **_common_candidate_values(source, now),
        policy_id=policy_id,
        candidate_revision=0,
        available=bool(available),
        start_eligible=False,
        continuation_eligible=False,
        active_latched=bool(active_latched),
        local_hard_stop=bool(local_hard_stop),
        requested_action=RequestedAction.NONE,
        actuator_scope=ActuatorScope.NONE,
        priority_class=PriorityClass.NONE,
        need_class=NeedClass.NONE,
        reason_code=ReasonCode.NO_ACTION,
        blocked_reason=blocked_reason,
        valid_from=None,
        valid_until=None,
        desired_actuator_fingerprint=None,
        economic_value_status=EconomicValueStatus.UNAVAILABLE,
    )
    return _finalize(candidate)


def build_rce_candidate(
    snapshot: RceSourceSnapshot,
    *,
    now: datetime,
) -> PolicyCandidate:
    """Translate one RCE source snapshot into an export-only candidate."""
    now_utc = _require_now(now)
    active = snapshot.active_latched is True
    plan_fresh = _fresh(
        snapshot.observed_at,
        now=now_utc,
        maximum_age_seconds=_RCE_PLAN_MAX_AGE_SECONDS,
    )
    flags_valid = all(
        _is_exact_bool(value)
        for value in (
            snapshot.active_latched,
            snapshot.current_slot_planned,
            snapshot.current_slot_start_eligible,
            snapshot.current_slot_continue_eligible,
            snapshot.control_data_ready,
            snapshot.price_above_threshold,
            snapshot.reserve_ready,
            snapshot.sale_block_active,
        )
    )
    plan_valid = (
        plan_fresh
        and _plan_contract_valid(snapshot)
        and flags_valid
        and isinstance(snapshot.status_code, RcePlanStatus)
        and snapshot.status_code in _RCE_READY_STATUSES
    )
    requested = active or snapshot.current_slot_planned is True
    if not plan_valid:
        blocked = (
            ReasonCode.FUTURE_CANDIDATE
            if _future(snapshot.observed_at, now_utc)
            else ReasonCode.STALE_CANDIDATE
            if _is_aware(snapshot.observed_at)
            else ReasonCode.UNAVAILABLE
        )
        return _no_action(
            PolicyId.RCE,
            snapshot,
            now=now_utc,
            available=False,
            blocked_reason=blocked,
            active_latched=active,
            local_hard_stop=active,
        )
    if not requested:
        return _no_action(
            PolicyId.RCE,
            snapshot,
            now=now_utc,
            available=True,
        )

    requested_power = _number(
        snapshot.requested_discharge_power_kw,
        minimum=0.0,
    )
    requested_energy = _number(
        snapshot.planned_export_energy_kwh,
        minimum=0.0,
    )
    plan_floor = _percent(snapshot.protected_soc_floor_percent)
    latched_floor = _percent(snapshot.latched_minimum_soc_percent)
    target_4305 = latched_floor if active else plan_floor
    target_4306 = _percent(snapshot.effective_discharge_power_percent)
    current_soc = _percent(snapshot.current_soc_percent)
    if active:
        valid_until = (
            _utc(snapshot.latched_slot_end)
            if _is_aware(snapshot.latched_slot_end)
            else None
        )
    else:
        run_end = (
            snapshot.current_run_end
            if _is_aware(snapshot.current_run_end)
            else snapshot.current_slot_end
        )
        valid_until = _utc(run_end) if _is_aware(run_end) else None
    active_targets_coherent = (
        not active
        or (
            _close(
                _percent(snapshot.active_4305_readback_percent),
                target_4305,
            )
            and _close(
                _percent(snapshot.active_4306_readback_percent),
                target_4306,
            )
        )
    )
    intent_valid = (
        requested_power is not None
        and requested_power > 0.0
        and requested_energy is not None
        and requested_energy > 0.0
        and target_4305 is not None
        and target_4306 is not None
        and target_4306 > 0.0
        and current_soc is not None
        and valid_until is not None
        and valid_until > now_utc
        and (
            active
            or (
                _is_aware(snapshot.current_slot_end)
                and _utc(snapshot.current_slot_end) > now_utc
            )
        )
    )
    if not intent_valid:
        return _no_action(
            PolicyId.RCE,
            snapshot,
            now=now_utc,
            available=False,
            blocked_reason=ReasonCode.UNAVAILABLE,
            active_latched=active,
            local_hard_stop=active,
        )

    local_hard_stop = bool(
        active
        and (
            snapshot.sale_block_active is not False
            or snapshot.control_data_ready is not True
            or snapshot.reserve_ready is not True
            or snapshot.current_slot_continue_eligible is not True
            or not active_targets_coherent
        )
    )
    slot_remaining = _remaining_seconds(snapshot.current_slot_end, now_utc)
    start_eligible = bool(
        not active
        and snapshot.current_slot_planned is True
        and snapshot.current_slot_start_eligible is True
        and snapshot.control_data_ready is True
        and snapshot.price_above_threshold is True
        and snapshot.reserve_ready is True
        and snapshot.sale_block_active is False
        and slot_remaining is not None
        and slot_remaining >= _RCE_MIN_REMAINING_SECONDS
        and current_soc > target_4305
    )
    continuation_eligible = bool(
        active
        and snapshot.current_slot_continue_eligible is True
        and active_targets_coherent
        and not local_hard_stop
    )
    fingerprint = _sha256(
        {
            "policy": PolicyId.RCE,
            "action": RequestedAction.RCE_EXPORT,
            "actuator_scope": ActuatorScope.EMS_BLOCK_4300_4306,
            "mode": 5,
            "target_4305": target_4305,
            "target_4306_percent": target_4306,
            "valid_until": valid_until,
        }
    )
    candidate = PolicyCandidate(
        **_common_candidate_values(snapshot, now_utc),
        policy_id=PolicyId.RCE,
        candidate_revision=0,
        available=True,
        start_eligible=start_eligible,
        continuation_eligible=continuation_eligible,
        active_latched=active,
        local_hard_stop=local_hard_stop,
        requested_action=RequestedAction.RCE_EXPORT,
        actuator_scope=ActuatorScope.EMS_BLOCK_4300_4306,
        priority_class=PriorityClass.ECONOMIC,
        need_class=NeedClass.OPTIONAL,
        reason_code=ReasonCode.ECONOMIC_CANDIDATE,
        blocked_reason=(ReasonCode.LOCAL_HARD_STOP if local_hard_stop else None),
        valid_from=None,
        valid_until=valid_until,
        desired_actuator_fingerprint=fingerprint,
        economic_value_status=EconomicValueStatus.UNAVAILABLE,
        requested_mode=PhysicalMode.GRID_DISCHARGE,
        requested_power_kw=requested_power,
        requested_energy_kwh=requested_energy,
        protected_soc_floor_percent=target_4305,
    )
    return _finalize(candidate)


def build_tariff_candidate(
    snapshot: TariffSourceSnapshot,
    *,
    now: datetime,
) -> PolicyCandidate:
    """Translate one tariff run with explicit optimizer-owned need class."""
    now_utc = _require_now(now)
    active = snapshot.active_latched is True
    plan_fresh = _fresh(
        snapshot.observed_at,
        now=now_utc,
        maximum_age_seconds=_TARIFF_PLAN_MAX_AGE_SECONDS,
    )
    flags_valid = all(
        _is_exact_bool(value)
        for value in (
            snapshot.active_latched,
            snapshot.current_slot_planned,
            snapshot.current_run_start_eligible,
            snapshot.current_run_continue_eligible,
            snapshot.control_data_ready,
            snapshot.planned_slot_ready,
        )
    )
    plan_valid = (
        plan_fresh
        and _plan_contract_valid(snapshot)
        and flags_valid
        and isinstance(snapshot.status_code, TariffPlanStatus)
        and snapshot.status_code in _TARIFF_READY_STATUSES
    )
    if not plan_valid:
        blocked = (
            ReasonCode.FUTURE_CANDIDATE
            if _future(snapshot.observed_at, now_utc)
            else ReasonCode.STALE_CANDIDATE
            if _is_aware(snapshot.observed_at)
            else ReasonCode.UNAVAILABLE
        )
        return _no_action(
            PolicyId.TARIFF,
            snapshot,
            now=now_utc,
            available=False,
            blocked_reason=blocked,
            active_latched=active,
            local_hard_stop=active,
        )

    need_class = snapshot.current_run_need_class
    if need_class is TariffRunNeed.NONE and not active:
        normal_none = (
            snapshot.current_slot_planned is False
            and snapshot.current_action is TariffAction.NONE
        )
        return _no_action(
            PolicyId.TARIFF,
            snapshot,
            now=now_utc,
            available=normal_none,
            blocked_reason=None if normal_none else ReasonCode.UNAVAILABLE,
        )
    if need_class not in {
        TariffRunNeed.REQUIRED_ENERGY,
        TariffRunNeed.ECONOMIC,
    }:
        return _no_action(
            PolicyId.TARIFF,
            snapshot,
            now=now_utc,
            available=False,
            blocked_reason=ReasonCode.UNAVAILABLE,
            active_latched=active,
            local_hard_stop=active,
        )

    action = snapshot.active_action if active else snapshot.current_action
    requested = active or snapshot.current_slot_planned is True
    action_valid = isinstance(action, TariffAction) and action in _TARIFF_ACTIONS
    if (
        need_class is TariffRunNeed.REQUIRED_ENERGY
        and action is TariffAction.GRID_SUPPORT
    ):
        action_valid = False
    if not requested or not action_valid:
        return _no_action(
            PolicyId.TARIFF,
            snapshot,
            now=now_utc,
            available=False,
            blocked_reason=ReasonCode.UNAVAILABLE,
            active_latched=active,
            local_hard_stop=active,
        )

    requested_power = _number(snapshot.requested_charge_power_kw, minimum=0.0)
    target_4304 = _percent(snapshot.command_charge_power_percent)
    requested_energy = _number(
        snapshot.current_run_grid_import_kwh,
        minimum=0.0,
    )
    plan_target = _percent(snapshot.target_soc_percent)
    latched_target = _percent(snapshot.latched_target_soc_percent)
    target_4303 = latched_target if active else plan_target
    protected_floor = _percent(snapshot.base_reserve_soc_percent)
    end_source = snapshot.latched_slot_end if active else snapshot.current_slot_end
    valid_until = _utc(end_source) if _is_aware(end_source) else None
    active_targets_coherent = (
        not active
        or (
            _close(
                _percent(snapshot.active_4303_readback_percent),
                target_4303,
            )
            and _close(
                _percent(snapshot.active_4304_readback_percent),
                target_4304,
            )
        )
    )
    intent_valid = (
        requested_power is not None
        and requested_power > 0.0
        and target_4304 is not None
        and target_4304 > 0.0
        and requested_energy is not None
        and requested_energy > 0.0
        and target_4303 is not None
        and protected_floor is not None
        and target_4303 >= protected_floor
        and valid_until is not None
        and valid_until > now_utc
    )
    if not intent_valid:
        return _no_action(
            PolicyId.TARIFF,
            snapshot,
            now=now_utc,
            available=False,
            blocked_reason=ReasonCode.UNAVAILABLE,
            active_latched=active,
            local_hard_stop=active,
        )

    local_hard_stop = bool(
        active
        and (
            snapshot.current_run_continue_eligible is not True
            or snapshot.control_data_ready is not True
            or not active_targets_coherent
        )
    )
    remaining = _remaining_seconds(valid_until, now_utc)
    start_eligible = bool(
        not active
        and snapshot.current_slot_planned is True
        and snapshot.current_run_start_eligible is True
        and snapshot.control_data_ready is True
        and snapshot.planned_slot_ready is True
        and remaining is not None
        and remaining >= _TARIFF_MIN_REMAINING_SECONDS
    )
    continuation_eligible = bool(
        active
        and snapshot.current_run_continue_eligible is True
        and active_targets_coherent
        and not local_hard_stop
    )
    priority = (
        PriorityClass.REQUIRED_ENERGY
        if need_class is TariffRunNeed.REQUIRED_ENERGY
        else PriorityClass.ECONOMIC
    )
    need = (
        NeedClass.MANDATORY
        if need_class is TariffRunNeed.REQUIRED_ENERGY
        else NeedClass.OPTIONAL
    )
    reason = (
        ReasonCode.REQUIRED_ENERGY_RESTORE
        if need_class is TariffRunNeed.REQUIRED_ENERGY
        else ReasonCode.ECONOMIC_CANDIDATE
    )
    benefit = _number(snapshot.current_run_benefit_pln)
    economic_status = (
        EconomicValueStatus.UNAVAILABLE
        if need_class is TariffRunNeed.REQUIRED_ENERGY
        else EconomicValueStatus.PROVISIONAL
    )
    fingerprint = _sha256(
        {
            "policy": PolicyId.TARIFF,
            "action": RequestedAction.TARIFF_CHARGE,
            "actuator_scope": ActuatorScope.EMS_BLOCK_4300_4306,
            "mode": 4,
            "active_action": action,
            "target_4303": target_4303,
            "target_4304_percent": target_4304,
            "valid_until": valid_until,
        }
    )
    candidate = PolicyCandidate(
        **_common_candidate_values(snapshot, now_utc),
        policy_id=PolicyId.TARIFF,
        candidate_revision=0,
        available=True,
        start_eligible=start_eligible,
        continuation_eligible=continuation_eligible,
        active_latched=active,
        local_hard_stop=local_hard_stop,
        requested_action=RequestedAction.TARIFF_CHARGE,
        actuator_scope=ActuatorScope.EMS_BLOCK_4300_4306,
        priority_class=priority,
        need_class=need,
        reason_code=reason,
        blocked_reason=(ReasonCode.LOCAL_HARD_STOP if local_hard_stop else None),
        valid_from=None,
        valid_until=valid_until,
        desired_actuator_fingerprint=fingerprint,
        economic_value_status=economic_status,
        requested_mode=PhysicalMode.GRID_CHARGE,
        requested_power_kw=requested_power,
        requested_energy_kwh=requested_energy,
        target_soc_percent=target_4303,
        protected_soc_floor_percent=protected_floor,
        expected_marginal_net_benefit_pln=(
            benefit if need_class is TariffRunNeed.ECONOMIC else None
        ),
    )
    return _finalize(candidate)


def _rcm_plan_valid(snapshot: RcmSourceSnapshot, now: datetime) -> bool:
    return (
        _fresh(
            snapshot.observed_at,
            now=now,
            maximum_age_seconds=_RCM_PLAN_MAX_AGE_SECONDS,
        )
        and _plan_contract_valid(snapshot)
        and all(
            _is_exact_bool(value)
            for value in (
                snapshot.live_emergency,
                snapshot.emergency_action_ready,
                snapshot.prediction_ready,
                snapshot.risk_window_active,
                snapshot.absorb_active,
                snapshot.export_active,
                snapshot.pre_discharge_active,
            )
        )
        and isinstance(snapshot.action, RcmAction)
    )


def _rcm_limit_valid(snapshot: RcmSourceSnapshot) -> bool:
    current = _percent(snapshot.current_export_limit_percent)
    target = _percent(snapshot.recommended_export_limit_percent)
    return bool(
        snapshot.export_control_enabled is True
        and snapshot.export_path_locally_valid is True
        and snapshot.direct_register_topology_allowed is True
        and snapshot.current_export_limit_fresh is True
        and current is not None
        and target is not None
        and target < current - _LOWER_TARGET_EPSILON
    )


def _rcm_charge_valid(snapshot: RcmSourceSnapshot) -> bool:
    target = _percent(snapshot.recommended_charge_limit_percent)
    power = _number(snapshot.recommended_charge_power_kw, minimum=0.0)
    return bool(
        snapshot.charge_path_locally_valid is True
        and snapshot.direct_register_topology_allowed is True
        and target is not None
        and target > 0.0
        and power is not None
        and power > 0.0
    )


def build_rcm_candidate(
    snapshot: RcmSourceSnapshot,
    *,
    now: datetime,
) -> PolicyCandidate:
    """Translate one RCEm plan into one deterministic primary RCM action."""
    now_utc = _require_now(now)
    active_any = any(
        value is True
        for value in (
            snapshot.absorb_active,
            snapshot.export_active,
            snapshot.pre_discharge_active,
        )
    )
    if not _rcm_plan_valid(snapshot, now_utc):
        blocked = (
            ReasonCode.FUTURE_CANDIDATE
            if _future(snapshot.observed_at, now_utc)
            else ReasonCode.STALE_CANDIDATE
            if _is_aware(snapshot.observed_at)
            else ReasonCode.UNAVAILABLE
        )
        return _no_action(
            PolicyId.RCM,
            snapshot,
            now=now_utc,
            available=False,
            blocked_reason=blocked,
            active_latched=active_any,
            local_hard_stop=active_any,
        )

    charge_valid = _rcm_charge_valid(snapshot)
    limit_valid = _rcm_limit_valid(snapshot)
    # This primary-only Shadow projection is insufficient for Active
    # dual-actuator execution when both direct targets would be needed.
    action_kind: str | None = None
    if snapshot.live_emergency is True:
        if snapshot.action is RcmAction.ABSORB_PV and charge_valid:
            action_kind = "absorb"
        elif (
            snapshot.action is RcmAction.LIMIT_EXPORT or not charge_valid
        ) and limit_valid:
            action_kind = "limit"
    elif snapshot.action is RcmAction.GRID_DISCHARGE_PREPARATION:
        pre_active = snapshot.pre_discharge_active is True
        deadline_source = (
            snapshot.latched_pre_discharge_deadline
            if pre_active
            else snapshot.pre_discharge_deadline
        )
        deadline = _utc(deadline_source) if _is_aware(deadline_source) else None
        eligible = (
            snapshot.pre_discharge_continue_eligible is True
            if pre_active
            else snapshot.pre_discharge_start_eligible is True
        )
        if (
            snapshot.pre_discharge_enabled is True
            and snapshot.full_block_topology_allowed is True
            and eligible
            and deadline is not None
            and deadline > now_utc
            and snapshot.sale_block_active is False
            and snapshot.export_state is ExportState.VERIFIED_ALLOWED
        ):
            action_kind = "pre_discharge"
    elif snapshot.action is RcmAction.ABSORB_PV and charge_valid:
        action_kind = "absorb"
    elif snapshot.action is RcmAction.LIMIT_EXPORT and limit_valid:
        action_kind = "limit"
    elif snapshot.action in _RCM_NO_ACTIONS:
        return _no_action(
            PolicyId.RCM,
            snapshot,
            now=now_utc,
            available=True,
            active_latched=active_any,
        )

    if action_kind is None:
        return _no_action(
            PolicyId.RCM,
            snapshot,
            now=now_utc,
            available=False,
            blocked_reason=ReasonCode.ACTUATOR_UNAVAILABLE,
            active_latched=active_any,
            local_hard_stop=active_any,
        )

    live = snapshot.live_emergency is True
    priority = (
        PriorityClass.LIVE_EMERGENCY
        if live
        else PriorityClass.PREVENTIVE_GRID
    )
    need = NeedClass.MANDATORY if live else NeedClass.PREVENTIVE
    reason = (
        ReasonCode.LIVE_EMERGENCY
        if live
        else ReasonCode.PREVENTIVE_VOLTAGE_ACTION
    )
    protected_floor = _percent(snapshot.protected_minimum_soc_percent)
    severity = _percent(snapshot.voltage_risk_score_percent)
    valid_until: datetime | None = None
    requested_mode: PhysicalMode | None = None
    requested_power: float | None = None
    requested_energy: float | None = None
    target_soc: float | None = None

    if action_kind == "absorb":
        requested_action = RequestedAction.RCM_ABSORB_PV
        actuator_scope = ActuatorScope.DIRECT_306
        active = snapshot.absorb_active is True
        target = _percent(snapshot.recommended_charge_limit_percent)
        requested_power = _number(
            snapshot.recommended_charge_power_kw,
            minimum=0.0,
        )
        requested_mode = PhysicalMode.SELF_USE
        if not live:
            target_soc = _percent(snapshot.target_soc_before_risk_percent)
        fingerprint = _sha256(
            {
                "policy": PolicyId.RCM,
                "action": requested_action,
                "actuator_scope": actuator_scope,
                "target_306_percent": target,
            }
        )
        local_ready = charge_valid
    elif action_kind == "limit":
        requested_action = RequestedAction.RCM_LIMIT_EXPORT
        actuator_scope = ActuatorScope.DIRECT_259
        active = snapshot.export_active is True
        target = _percent(snapshot.recommended_export_limit_percent)
        fingerprint = _sha256(
            {
                "policy": PolicyId.RCM,
                "action": requested_action,
                "actuator_scope": actuator_scope,
                "target_259_percent": target,
            }
        )
        local_ready = limit_valid
    else:
        requested_action = RequestedAction.RCM_PRE_DISCHARGE
        actuator_scope = ActuatorScope.EMS_BLOCK_4300_4306
        active = snapshot.pre_discharge_active is True
        deadline_source = (
            snapshot.latched_pre_discharge_deadline
            if active
            else snapshot.pre_discharge_deadline
        )
        target_source = (
            snapshot.latched_pre_discharge_target_soc_percent
            if active
            else snapshot.pre_discharge_target_soc_percent
        )
        power_source = (
            snapshot.latched_pre_discharge_power_kw
            if active
            else snapshot.pre_discharge_power_kw
        )
        power_percent_source = (
            snapshot.latched_pre_discharge_power_percent
            if active
            else snapshot.pre_discharge_power_percent
        )
        valid_until = _utc(deadline_source) if _is_aware(deadline_source) else None
        target_soc = _percent(target_source)
        requested_power = _number(power_source, minimum=0.0)
        power_percent = _percent(power_percent_source)
        requested_energy = _number(
            snapshot.planned_grid_discharge_kwh,
            minimum=0.0,
        )
        requested_mode = PhysicalMode.GRID_DISCHARGE
        local_ready = bool(
            valid_until is not None
            and valid_until > now_utc
            and target_soc is not None
            and protected_floor is not None
            and target_soc >= protected_floor
            and requested_power is not None
            and requested_power > 0.0
            and power_percent is not None
            and power_percent > 0.0
            and requested_energy is not None
            and requested_energy > 0.0
        )
        if not local_ready:
            return _no_action(
                PolicyId.RCM,
                snapshot,
                now=now_utc,
                available=False,
                blocked_reason=ReasonCode.UNAVAILABLE,
                active_latched=active,
                local_hard_stop=active,
            )
        fingerprint = _sha256(
            {
                "policy": PolicyId.RCM,
                "action": requested_action,
                "actuator_scope": actuator_scope,
                "mode": 5,
                "target_4305": target_soc,
                "target_4306_percent": power_percent,
                "valid_until": valid_until,
            }
        )

    start_eligible = bool(
        not active
        and local_ready
        and (
            snapshot.emergency_action_ready is True
            if live
            else snapshot.pre_discharge_start_eligible is True
            if action_kind == "pre_discharge"
            else True
        )
    )
    continuation_eligible = bool(
        active
        and local_ready
        and (
            snapshot.pre_discharge_continue_eligible is True
            if action_kind == "pre_discharge"
            else True
        )
    )
    local_hard_stop = bool(active and not continuation_eligible)
    candidate = PolicyCandidate(
        **_common_candidate_values(snapshot, now_utc),
        policy_id=PolicyId.RCM,
        candidate_revision=0,
        available=True,
        start_eligible=start_eligible,
        continuation_eligible=continuation_eligible,
        active_latched=active,
        local_hard_stop=local_hard_stop,
        requested_action=requested_action,
        actuator_scope=actuator_scope,
        priority_class=priority,
        need_class=need,
        reason_code=reason,
        blocked_reason=(ReasonCode.LOCAL_HARD_STOP if local_hard_stop else None),
        valid_from=None,
        valid_until=valid_until,
        desired_actuator_fingerprint=fingerprint,
        economic_value_status=EconomicValueStatus.UNAVAILABLE,
        requested_mode=requested_mode,
        requested_power_kw=requested_power,
        requested_energy_kwh=requested_energy,
        target_soc_percent=target_soc,
        protected_soc_floor_percent=protected_floor,
        severity=severity,
    )
    return _finalize(candidate)


def _physical_mode_and_freshness(
    snapshot: ExecutionSourceSnapshot,
    now: datetime,
) -> tuple[PhysicalMode, bool]:
    code = _number(snapshot.physical_mode_code, minimum=0.0, maximum=65_535.0)
    mode = {
        0.0: PhysicalMode.SELF_USE,
        3.0: PhysicalMode.OFF_GRID,
        4.0: PhysicalMode.GRID_CHARGE,
        5.0: PhysicalMode.GRID_DISCHARGE,
    }.get(code, PhysicalMode.UNKNOWN)
    fresh = bool(
        mode is not PhysicalMode.UNKNOWN
        and _fresh(
            snapshot.full_block_generation_at,
            now=now,
            maximum_age_seconds=_PHYSICAL_MAX_AGE_SECONDS,
        )
    )
    return mode, fresh


def normalize_owner(
    snapshot: ExecutionSourceSnapshot,
    *,
    now: datetime,
) -> NormalizedOwner:
    """Normalize raw tri-state writer markers without compatibility fallbacks."""
    now_utc = _require_now(now)
    raw_markers = (
        snapshot.balancing_active,
        snapshot.manual_charge_active,
        snapshot.manual_discharge_active,
        snapshot.rce_active,
        snapshot.tariff_active,
        snapshot.rcm_active,
        snapshot.rcm_export_control_active,
        snapshot.rcm_pre_discharge_active,
        snapshot.charge_timer_active,
        snapshot.discharge_timer_active,
    )
    if not all(_is_exact_bool(value) for value in raw_markers):
        return NormalizedOwner(OwnerKind.UNKNOWN, True)
    if snapshot.manual_charge_active and snapshot.manual_discharge_active:
        return NormalizedOwner(OwnerKind.UNKNOWN, True)
    if (
        snapshot.charge_timer_active and not snapshot.manual_charge_active
    ) or (
        snapshot.discharge_timer_active and not snapshot.manual_discharge_active
    ):
        return NormalizedOwner(OwnerKind.UNKNOWN, True)

    families: list[OwnerKind] = []
    if snapshot.balancing_active:
        families.append(OwnerKind.BALANCING)
    if snapshot.manual_charge_active or snapshot.manual_discharge_active:
        families.append(OwnerKind.MANUAL)
    if snapshot.rce_active:
        families.append(OwnerKind.RCE)
    if snapshot.tariff_active:
        families.append(OwnerKind.TARIFF)
    if (
        snapshot.rcm_active
        or snapshot.rcm_export_control_active
        or snapshot.rcm_pre_discharge_active
    ):
        families.append(OwnerKind.RCM)
    if len(families) > 1:
        return NormalizedOwner(OwnerKind.UNKNOWN, True)
    if families:
        return NormalizedOwner(families[0], False)

    physical_mode, physical_mode_fresh = _physical_mode_and_freshness(
        snapshot,
        now_utc,
    )
    if not physical_mode_fresh or physical_mode is PhysicalMode.UNKNOWN:
        return NormalizedOwner(OwnerKind.UNKNOWN, False)
    if physical_mode in {PhysicalMode.SELF_USE, PhysicalMode.OFF_GRID}:
        return NormalizedOwner(OwnerKind.NONE, False)
    return NormalizedOwner(OwnerKind.FOREIGN, False)


def normalize_export_state(
    snapshot: ExecutionSourceSnapshot,
    *,
    now: datetime,
) -> ExportState:
    """Normalize one coherent physical GCF cohort."""
    now_utc = _require_now(now)
    if (
        snapshot.hardware_readback_supported is not True
        or snapshot.gcf_cohort_coherent is not True
        or not _fresh(
            snapshot.gcf_generation_at,
            now=now_utc,
            maximum_age_seconds=_EXPORT_MAX_AGE_SECONDS,
        )
    ):
        return ExportState.UNVERIFIED
    code = _number(snapshot.gcf_enable_code, minimum=0.0, maximum=1.0)
    limit = _percent(snapshot.effective_export_limit_percent)
    if code is None or limit is None:
        return ExportState.UNVERIFIED
    if code == 0.0:
        return ExportState.VERIFIED_ALLOWED
    if code == 1.0 and limit == 0.0:
        return ExportState.CONFIRMED_ZERO_EXPORT
    if code == 1.0 and limit > 0.0:
        return ExportState.VERIFIED_ALLOWED
    return ExportState.UNVERIFIED


def _topology(
    snapshot: ExecutionSourceSnapshot,
    now: datetime,
) -> tuple[bool, bool]:
    if not _fresh(
        snapshot.topology_generation_at,
        now=now,
        maximum_age_seconds=_TOPOLOGY_MAX_AGE_SECONDS,
    ):
        return False, False
    machine_type = _number(
        snapshot.machine_type_code,
        minimum=0.0,
        maximum=255.0,
    )
    count = _number(snapshot.inverter_count, minimum=0.0, maximum=255.0)
    if machine_type is None or count is None:
        return False, False
    if not machine_type.is_integer() or not count.is_integer():
        return False, False
    single = machine_type == 0.0 and count == 1.0
    master = machine_type == 1.0 and 2.0 <= count <= 10.0
    return single or master, single


def _positive_fresh(
    value: Any,
    observed_at: Any,
    *,
    now: datetime,
    maximum_age_seconds: float,
) -> bool:
    normalized = _number(value, minimum=0.0)
    return bool(
        normalized is not None
        and normalized > 0.0
        and _fresh(
            observed_at,
            now=now,
            maximum_age_seconds=maximum_age_seconds,
        )
    )


def build_execution_context(
    snapshot: ExecutionSourceSnapshot,
    *,
    now: datetime,
) -> ExecutionContext:
    """Build a Phase 1A execution context from one semantic snapshot."""
    now_utc = _require_now(now)
    physical_mode, physical_mode_fresh = _physical_mode_and_freshness(
        snapshot,
        now_utc,
    )
    owner = normalize_owner(snapshot, now=now_utc)
    full_topology, direct_topology = _topology(snapshot, now_utc)
    voltage_ready = _positive_fresh(
        snapshot.bms_voltage_v,
        snapshot.bms_voltage_observed_at,
        now=now_utc,
        maximum_age_seconds=_BMS_MAX_AGE_SECONDS,
    )
    charge_direction_ready = bool(
        voltage_ready
        and _positive_fresh(
            snapshot.bms_max_charge_current_a,
            snapshot.bms_charge_current_observed_at,
            now=now_utc,
            maximum_age_seconds=_BMS_MAX_AGE_SECONDS,
        )
    )
    discharge_direction_ready = bool(
        voltage_ready
        and _positive_fresh(
            snapshot.bms_max_discharge_current_a,
            snapshot.bms_discharge_current_observed_at,
            now=now_utc,
            maximum_age_seconds=_BMS_MAX_AGE_SECONDS,
        )
    )
    soc = _percent(snapshot.battery_soc_percent)
    critical_bms_ready = bool(
        voltage_ready
        and soc is not None
        and _fresh(
            snapshot.battery_soc_observed_at,
            now=now_utc,
            maximum_age_seconds=_SOC_MAX_AGE_SECONDS,
        )
    )
    # Supervisor transaction namespace has no transaction in Off/Shadow Phase 1B.
    # This states nothing about a legacy scheduler awaiting physical acknowledgement.
    return ExecutionContext(
        observed_at=now_utc,
        physical_mode=physical_mode,
        physical_mode_fresh=physical_mode_fresh,
        owner_kind=owner.owner_kind,
        owner_conflict=owner.owner_conflict,
        transaction_pending=False,
        transaction_owner_kind=OwnerKind.NONE,
        full_block_execution_ready=(
            snapshot.full_block_execution_ready is True
        ),
        direct_306_execution_ready=(
            snapshot.direct_306_execution_ready is True
        ),
        direct_259_execution_ready=(
            snapshot.direct_259_execution_ready is True
        ),
        topology_full_block_allowed=full_topology,
        topology_direct_register_allowed=direct_topology,
        charge_direction_ready=charge_direction_ready,
        discharge_direction_ready=discharge_direction_ready,
        critical_bms_ready=critical_bms_ready,
        export_state=normalize_export_state(snapshot, now=now_utc),
    )


__all__ = (
    "ExecutionSourceSnapshot",
    "NormalizedOwner",
    "RcePlanStatus",
    "RceSourceSnapshot",
    "RcmAction",
    "RcmSourceSnapshot",
    "TariffAction",
    "TariffPlanStatus",
    "TariffRunNeed",
    "TariffSourceSnapshot",
    "build_execution_context",
    "build_rce_candidate",
    "build_rcm_candidate",
    "build_tariff_candidate",
    "normalize_export_state",
    "normalize_owner",
)
