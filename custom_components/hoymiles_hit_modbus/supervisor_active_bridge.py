"""Pure bridge from Supervisor arbitration to the Active executor.

The arbiter intentionally grants no authority.  This module performs the
strict, fail-closed translation of one immutable arbitration frame into the
executor's settings, gates and actuator intent.  It also classifies independent
physical power evidence after a command.  Home Assistant persistence and
transport remain outside this module.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from math import isfinite
import re
from typing import Any, Final, Iterable

if __package__:
    from .ems_supervisor import (
        ActuatorScope,
        ExportState,
        OwnerKind,
        PolicyCandidate,
        PolicyId,
        ReasonCode,
        RequestedAction,
        SupervisorDecision,
        SupervisorMode,
    )
    from .supervisor_executor import (
        ActuatorIntent,
        AtomicWriteFamily,
        CommandSet,
        EmsBlock,
        EmsMode,
        ExecutionAction,
        ExecutionGates,
        ExecutionOwner,
        MAX_READBACK_AGE_SECONDS,
        RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS,
        TARIFF_PHYSICAL_MAX_AGE_SECONDS,
        PhysicalVerification,
        SettingsSnapshot,
        VerificationStatus,
        _ems_blocks_match,
        _retarget_mode,
    )
    from .supervisor_runtime import (
        ExecutionSourceSnapshot,
        RcePlanStatus,
        RceSourceSnapshot,
        RcmAction,
        RcmSourceSnapshot,
        TariffPlanStatus,
        TariffSourceSnapshot,
    )
else:  # Direct import used by dependency-free repository tests.
    from ems_supervisor import (  # type: ignore[no-redef]
        ActuatorScope,
        ExportState,
        OwnerKind,
        PolicyCandidate,
        PolicyId,
        ReasonCode,
        RequestedAction,
        SupervisorDecision,
        SupervisorMode,
    )
    from supervisor_executor import (  # type: ignore[no-redef]
        ActuatorIntent,
        AtomicWriteFamily,
        CommandSet,
        EmsBlock,
        EmsMode,
        ExecutionAction,
        ExecutionGates,
        ExecutionOwner,
        MAX_READBACK_AGE_SECONDS,
        RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS,
        TARIFF_PHYSICAL_MAX_AGE_SECONDS,
        PhysicalVerification,
        SettingsSnapshot,
        VerificationStatus,
        _ems_blocks_match,
        _retarget_mode,
    )
    from supervisor_runtime import (  # type: ignore[no-redef]
        ExecutionSourceSnapshot,
        RcePlanStatus,
        RceSourceSnapshot,
        RcmAction,
        RcmSourceSnapshot,
        TariffPlanStatus,
        TariffSourceSnapshot,
    )


PHYSICAL_MAX_AGE_SECONDS: Final = 15.0
PHYSICAL_COHORT_SPAN_SECONDS: Final = 5.0
DIRECT_ACTION_DEADLINE_SECONDS: Final = 300.0
FLOW_THRESHOLD_W: Final = 200.0
# Mode 4 confirmation also requires coherent total GRID import above the
# global flow threshold.  This smaller battery-direction threshold therefore
# distinguishes a supported 1% / 10 kW charge (100 W) from zero/noise without
# weakening the grid-flow or four-channel power-balance proof.
TARIFF_CHARGE_FLOW_THRESHOLD_W: Final = 50.0
ZERO_EXPORT_TOLERANCE_W: Final = 100.0
POWER_BALANCE_ABSOLUTE_TOLERANCE_W: Final = 250.0
POWER_BALANCE_RELATIVE_TOLERANCE: Final = 0.10
MAX_FUTURE_SKEW_SECONDS: Final = 5.0
REGISTER_FLOAT_NOISE_TOLERANCE: Final = 1e-4
RCE_RETARGET_PLAN_MAX_AGE_SECONDS: Final = 300.0
RCE_RETARGET_COHORT_HOLD_SECONDS: Final = 60.0
RCE_RECALCULATION_HOLD_SECONDS: Final = 180.0
# Extra 90 seconds for the measured Mode 5 LOAD/ramp transient. This is
# the acknowledged, physically verified RCE sale hold, not FC03 timeout
# or permission to overlook a battery/BMS contradiction.
RCE_POST_COMMAND_SETTLING_SECONDS: Final = 180.0
RCE_POST_COMMAND_REPLAN_SECONDS: Final = 150.0
TARIFF_POST_COMMAND_SETTLING_SECONDS: Final = 180.0
_SHA256: Final = re.compile(r"^[0-9a-f]{64}$")

_TARIFF_CHARGE_ACTIONS: Final = frozenset(
    {
        ExecutionAction.TARIFF_BATTERY_CHARGE,
        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
    }
)


class ActiveBridgeError(ValueError):
    """A bounded arbitration frame cannot safely enter execution."""


@dataclass(frozen=True, slots=True)
class RceRetarget:
    """One exact same-window RCE successor and its candidate safety facts."""

    intent: ActuatorIntent
    candidate: PolicyCandidate


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ActiveBridgeError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def _number(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ActiveBridgeError(f"{name} is unavailable")
    normalized = float(value)
    if not isfinite(normalized) or not minimum <= normalized <= maximum:
        raise ActiveBridgeError(f"{name} is outside its physical range")
    return normalized


def _register_number(
    value: object,
    name: str,
    *,
    minimum: float,
    maximum: float,
    scale: int,
) -> float:
    """Normalize only insignificant float transport noise at a register step."""

    normalized = _number(value, name, minimum=minimum, maximum=maximum)
    scaled = normalized * scale
    quantized = round(scaled)
    if abs(scaled - quantized) > REGISTER_FLOAT_NOISE_TOLERANCE:
        raise ActiveBridgeError(f"{name} has unsupported register precision")
    return quantized / scale


def _generation(value: object, name: str) -> int:
    normalized = _number(value, name, minimum=1.0, maximum=16_000_000.0)
    if not normalized.is_integer():
        raise ActiveBridgeError(f"{name} is not an integer generation")
    return int(normalized)


def _mode(value: object) -> EmsMode:
    normalized = _number(value, "physical EMS mode", minimum=0.0, maximum=65_535.0)
    if not normalized.is_integer():
        raise ActiveBridgeError("physical EMS mode is not an integer")
    try:
        return EmsMode(int(normalized))
    except ValueError as err:
        raise ActiveBridgeError("physical EMS mode is unsupported") from err


def _gcf_cohort(
    source: ExecutionSourceSnapshot,
) -> tuple[bool | None, float | None, int | None, datetime | None, bool]:
    """Return one complete GCF cohort or explicit unavailable evidence."""

    try:
        if source.gcf_enable_code not in (0, 0.0, 1, 1.0):
            raise ActiveBridgeError("GCF enable readback is unavailable")
        enabled = bool(int(source.gcf_enable_code))  # type: ignore[arg-type]
        limit = _register_number(
            source.effective_export_limit_percent,
            "259",
            minimum=-10.0,
            maximum=200.0,
            scale=10,
        )
        generation = _generation(source.gcf_generation, "GCF generation")
        observed_at = _aware_utc(
            source.gcf_generation_at,  # type: ignore[arg-type]
            "GCF observed_at",
        )
    except (ActiveBridgeError, TypeError, ValueError):
        return None, None, None, None, False
    return (
        enabled,
        limit,
        generation,
        observed_at,
        source.gcf_cohort_coherent is True,
    )


def _battery_306_cohort(
    source: ExecutionSourceSnapshot,
) -> tuple[float | None, int | None, datetime | None, bool]:
    """Return one complete register-306 cohort or explicit unavailable evidence."""

    try:
        limit = _register_number(
            source.battery_charge_limit_percent,
            "306",
            minimum=10.0,
            maximum=100.0,
            scale=10,
        )
        generation = _generation(
            source.battery_charge_limit_generation,
            "306 generation",
        )
        observed_at = _aware_utc(
            source.battery_charge_limit_generation_at,  # type: ignore[arg-type]
            "306 observed_at",
        )
    except (ActiveBridgeError, TypeError, ValueError):
        return None, None, None, False
    return limit, generation, observed_at, True


def settings_from_execution_source(
    source: ExecutionSourceSnapshot,
) -> SettingsSnapshot:
    """Build mandatory EMS and independently available direct cohorts."""

    gcf_enabled, export_limit, gcf_generation, gcf_at, gcf_coherent = (
        _gcf_cohort(source)
    )
    battery_limit, battery_generation, battery_at, battery_coherent = (
        _battery_306_cohort(source)
    )
    return SettingsSnapshot(
        ems_block=EmsBlock(
            mode=_mode(source.physical_mode_code),
            self_use_soc_percent_4301=_number(
                source.self_use_soc_percent,
                "4301",
                minimum=10.0,
                maximum=100.0,
            ),
            backup_soc_percent_4302=_number(
                source.backup_soc_percent,
                "4302",
                minimum=60.0,
                maximum=100.0,
            ),
            force_charge_soc_percent_4303=_number(
                source.force_charge_soc_percent,
                "4303",
                minimum=10.0,
                maximum=100.0,
            ),
            maximum_charge_power_percent_4304=_register_number(
                source.maximum_charge_power_percent,
                "4304",
                minimum=0.0,
                maximum=100.0,
                scale=10,
            ),
            force_discharge_soc_percent_4305=_number(
                source.force_discharge_soc_percent,
                "4305",
                minimum=0.0,
                maximum=100.0,
            ),
            maximum_discharge_power_percent_4306=_register_number(
                source.maximum_discharge_power_percent,
                "4306",
                minimum=0.0,
                maximum=100.0,
                scale=10,
            ),
        ),
        ems_generation=_generation(source.full_block_generation, "EMS generation"),
        ems_observed_at=_aware_utc(
            source.full_block_generation_at,  # type: ignore[arg-type]
            "EMS observed_at",
        ),
        ems_coherent=True,
        gcf_enabled_258=gcf_enabled,
        export_limit_percent_259=export_limit,
        gcf_generation=gcf_generation,
        gcf_observed_at=gcf_at,
        gcf_coherent=gcf_coherent,
        battery_max_charge_power_percent_306=battery_limit,
        battery_generation=battery_generation,
        battery_observed_at=battery_at,
        battery_coherent=battery_coherent,
    )


_OWNER_MAP = {
    OwnerKind.RCE: ExecutionOwner.RCE,
    OwnerKind.TARIFF: ExecutionOwner.TARIFF,
    OwnerKind.RCM: ExecutionOwner.RCM,
    OwnerKind.BALANCING: ExecutionOwner.BALANCING,
    OwnerKind.MANUAL: ExecutionOwner.MANUAL,
}


def execution_gates(
    source: ExecutionSourceSnapshot,
    *,
    owner_kind: OwnerKind,
    owner_conflict: bool,
    now: datetime,
    candidate: PolicyCandidate | None = None,
    maximum_readback_age_seconds: float = MAX_READBACK_AGE_SECONDS,
) -> ExecutionGates:
    """Translate independently normalized readiness without owner fallbacks."""

    now_utc = _aware_utc(now, "now")
    try:
        settings = settings_from_execution_source(source)
    except (ActiveBridgeError, ValueError):
        ems_fresh = False
        gcf_fresh = False
        battery_306_fresh = False
    else:
        ems_fresh = not settings.freshness_errors(
            now_utc,
            maximum_age_seconds=maximum_readback_age_seconds,
            required_families=frozenset(),
        )
        gcf_fresh = not settings.freshness_errors(
            now_utc,
            maximum_age_seconds=maximum_readback_age_seconds,
            required_families=frozenset(
                {AtomicWriteFamily.GCF_EXPORT_LIMIT}
            ),
        )
        battery_306_fresh = not settings.freshness_errors(
            now_utc,
            maximum_age_seconds=maximum_readback_age_seconds,
            required_families=frozenset(
                {AtomicWriteFamily.BATTERY_CHARGE_LIMIT}
            ),
        )
        if (
            maximum_readback_age_seconds > MAX_READBACK_AGE_SECONDS
            and (now_utc - settings.ems_observed_at).total_seconds()
            >= maximum_readback_age_seconds
        ):
            # The relaxed RCE continuation budget is an exclusive boundary;
            # the established 15-second default keeps its existing semantics.
            ems_fresh = False
            gcf_fresh = False
            battery_306_fresh = False
    observed_owner: ExecutionOwner | str | None
    if owner_kind is OwnerKind.NONE:
        observed_owner = None
    elif owner_kind in _OWNER_MAP:
        observed_owner = _OWNER_MAP[owner_kind]
    else:
        observed_owner = owner_kind.value
    soc = source.battery_soc_percent
    protected_floor = source.self_use_soc_percent
    soc_numeric = bool(
        not isinstance(soc, bool)
        and isinstance(soc, (int, float))
        and isfinite(float(soc))
        and 0.0 <= float(soc) <= 100.0
    )
    floor_numeric = bool(
        not isinstance(protected_floor, bool)
        and isinstance(protected_floor, (int, float))
        and isfinite(float(protected_floor))
        and 10.0 <= float(protected_floor) <= 100.0
    )
    discharge_candidate = bool(
        candidate is not None
        and candidate.requested_action
        in {RequestedAction.RCE_EXPORT, RequestedAction.RCM_PRE_DISCHARGE}
    )
    candidate_floor = (
        candidate.protected_soc_floor_percent
        if discharge_candidate and candidate is not None
        else None
    )
    candidate_floor_numeric = bool(
        not isinstance(candidate_floor, bool)
        and isinstance(candidate_floor, (int, float))
        and isfinite(float(candidate_floor))
        and 0.0 <= float(candidate_floor) <= 100.0
    )
    command_floor = (
        candidate.target_soc_percent
        if candidate is not None
        and candidate.requested_action is RequestedAction.RCM_PRE_DISCHARGE
        else None
    )
    command_floor_numeric = bool(
        not isinstance(command_floor, bool)
        and isinstance(command_floor, (int, float))
        and isfinite(float(command_floor))
        and 0.0 <= float(command_floor) <= 100.0
    )
    effective_discharge_floor = (
        max(
            float(protected_floor),
            float(candidate_floor) if candidate_floor_numeric else 0.0,
            float(command_floor) if command_floor_numeric else 0.0,
        )
        if floor_numeric
        and (not discharge_candidate or candidate_floor_numeric)
        and (
            candidate is None
            or candidate.requested_action is not RequestedAction.RCM_PRE_DISCHARGE
            or command_floor_numeric
        )
        else None
    )
    soc_fresh = bool(
        soc_numeric
        and _fresh(source.battery_soc_observed_at, now_utc)
    )
    voltage_fresh = _positive_fresh(
        source.bms_voltage_v,
        source.bms_voltage_observed_at,
        now_utc,
        maximum_age=300.0,
    )
    if __package__:
        from .pv_charge_delay_control import live_ready as pv_hold_live_ready
    else:
        from pv_charge_delay_control import live_ready as pv_hold_live_ready
    return ExecutionGates(
        pv_charge_hold_ready=pv_hold_live_ready(source, now=now_utc),
        inputs_fresh=ems_fresh and source.hardware_readback_supported is True,
        bms_fresh=soc_fresh and voltage_fresh,
        full_block_ready=(
            source.full_block_execution_ready is True and ems_fresh
        ),
        direct_306_ready=(
            source.direct_306_execution_ready is True and battery_306_fresh
        ),
        direct_259_ready=(
            source.direct_259_execution_ready is True and gcf_fresh
        ),
        full_block_topology_ready=_full_block_topology_ready(source, now_utc),
        direct_register_topology_ready=_direct_topology_ready(source, now_utc),
        charge_direction_ready=(
            voltage_fresh
            and _positive_fresh(
                source.bms_max_charge_current_a,
                source.bms_charge_current_observed_at,
                now_utc,
                maximum_age=300.0,
            )
        ),
        discharge_direction_ready=(
            voltage_fresh
            and soc_fresh
            and effective_discharge_floor is not None
            and float(soc) > effective_discharge_floor
            and _positive_fresh(
                source.bms_max_discharge_current_a,
                source.bms_discharge_current_observed_at,
                now_utc,
                maximum_age=300.0,
            )
        ),
        owner_conflict=owner_conflict,
        observed_owner=observed_owner,
    )


def _fresh(
    observed_at: object,
    now: datetime,
    *,
    maximum_age: float = 120.0,
) -> bool:
    if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
        return False
    age = (now - observed_at.astimezone(timezone.utc)).total_seconds()
    return -MAX_FUTURE_SKEW_SECONDS <= age <= maximum_age


def _positive_fresh(
    value: object,
    observed_at: object,
    now: datetime,
    *,
    maximum_age: float,
) -> bool:
    return bool(
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and isfinite(float(value))
        and float(value) > 0.0
        and _fresh(observed_at, now, maximum_age=maximum_age)
    )


def _topology_values(
    source: ExecutionSourceSnapshot,
    now: datetime,
) -> tuple[int, int] | None:
    if not _fresh(source.topology_generation_at, now, maximum_age=180.0):
        return None
    try:
        machine = int(_number(source.machine_type_code, "machine type", minimum=0, maximum=255))
        count = int(_number(source.inverter_count, "inverter count", minimum=1, maximum=10))
    except ActiveBridgeError:
        return None
    if source.machine_type_code != machine or source.inverter_count != count:
        return None
    return machine, count


def _full_block_topology_ready(source: ExecutionSourceSnapshot, now: datetime) -> bool:
    values = _topology_values(source, now)
    return values is not None and (
        values == (0, 1) or (values[0] == 1 and 2 <= values[1] <= 10)
    )


def _direct_topology_ready(source: ExecutionSourceSnapshot, now: datetime) -> bool:
    return _topology_values(source, now) == (0, 1)


_ACTION_MAP = {
    RequestedAction.RCE_EXPORT: ExecutionAction.RCE_EXPORT,
    RequestedAction.PV_CHARGE_HOLD: ExecutionAction.PV_CHARGE_HOLD,
    RequestedAction.TARIFF_BATTERY_CHARGE: ExecutionAction.TARIFF_BATTERY_CHARGE,
    RequestedAction.TARIFF_GRID_SUPPORT: ExecutionAction.TARIFF_GRID_SUPPORT,
    RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE: (
        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE
    ),
    RequestedAction.RCM_ABSORB_PV: ExecutionAction.RCM_ABSORB_PV,
    RequestedAction.RCM_LIMIT_EXPORT: ExecutionAction.RCM_LIMIT_EXPORT,
    RequestedAction.RCM_PRE_DISCHARGE: ExecutionAction.RCM_PRE_DISCHARGE,
}
_POLICY_OWNER = {
    PolicyId.RCE: ExecutionOwner.RCE,
    PolicyId.TARIFF: ExecutionOwner.TARIFF,
    PolicyId.RCM: ExecutionOwner.RCM,
}


def _continuation_identity(candidate: PolicyCandidate) -> str:
    """Return the stable physical identity persisted by the executor.

    ``candidate_revision`` remains the per-frame arbitration identity.  It is
    intentionally excluded here because start/continuation and latch facts
    change after a successful start without changing the physical command.
    """

    fingerprint = candidate.desired_actuator_fingerprint
    if not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
        raise ActiveBridgeError("selected candidate has no full actuator fingerprint")
    if candidate.requested_action is RequestedAction.NONE:
        raise ActiveBridgeError("selected candidate requests no action")
    return (
        f"{candidate.policy_id.value}:{candidate.requested_action.value}:"
        f"{fingerprint}"
    )


def selected_candidate(
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
) -> PolicyCandidate | None:
    """Resolve exactly one selected raw candidate from the decision identity."""

    if decision.selected_policy is None or decision.selected_candidate_revision is None:
        return None
    matches = tuple(
        candidate
        for candidate in candidates
        if candidate.policy_id is decision.selected_policy
        and candidate.candidate_revision == decision.selected_candidate_revision
    )
    if len(matches) != 1:
        raise ActiveBridgeError("selected candidate identity is ambiguous")
    return matches[0]


def build_actuator_intent(
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    rce: RceSourceSnapshot,
    tariff: TariffSourceSnapshot,
    rcm: RcmSourceSnapshot,
    now: datetime,
) -> ActuatorIntent | None:
    """Translate one selected candidate without inventing actuator values."""

    now_utc = _aware_utc(now, "now")
    if decision.supervisor_mode is not SupervisorMode.ACTIVE:
        return None
    candidate = selected_candidate(decision, candidates)
    if candidate is None:
        return None
    if candidate.requested_action is RequestedAction.NONE:
        raise ActiveBridgeError("selected candidate requests no action")
    if not (candidate.start_eligible or candidate.continuation_eligible):
        raise ActiveBridgeError("selected candidate has no current eligibility")
    if candidate.local_hard_stop or candidate.desired_actuator_fingerprint is None:
        raise ActiveBridgeError("selected candidate has no safe actuator fingerprint")
    action = _ACTION_MAP.get(candidate.requested_action)
    owner = _POLICY_OWNER.get(candidate.policy_id)
    if action is None or owner is None:
        raise ActiveBridgeError("selected action is unsupported")

    before = settings.ems_block
    if action is ExecutionAction.PV_CHARGE_HOLD:
        target = _required_percent(candidate.protected_soc_floor_percent, "PV hold target 4305", whole=True)
        command = CommandSet(ems_block=replace(before,mode=EmsMode.GRID_DISCHARGE,
            force_discharge_soc_percent_4305=target, maximum_discharge_power_percent_4306=1.))
    elif action is ExecutionAction.RCE_EXPORT:
        target = _required_percent(
            candidate.protected_soc_floor_percent,
            "RCE target 4305",
            allow_zero=True,
            whole=True,
        )
        power = _required_percent(
            rce.effective_discharge_power_percent,
            "RCE target 4306",
            whole=True,
        )
        command = CommandSet(
            ems_block=replace(
                before,
                mode=EmsMode.GRID_DISCHARGE,
                force_discharge_soc_percent_4305=target,
                maximum_discharge_power_percent_4306=power,
            )
        )
    elif action in {
        ExecutionAction.TARIFF_BATTERY_CHARGE,
        ExecutionAction.TARIFF_GRID_SUPPORT,
        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
    }:
        target = _required_percent(candidate.target_soc_percent, "tariff target 4303", whole=True)
        power = _required_percent(
            tariff.command_charge_power_percent,
            "tariff target 4304",
            allow_zero=True,
            whole=True,
        )
        command = CommandSet(
            ems_block=replace(
                before,
                mode=EmsMode.GRID_CHARGE,
                force_charge_soc_percent_4303=target,
                maximum_charge_power_percent_4304=power,
            )
        )
    elif action is ExecutionAction.RCM_ABSORB_PV:
        command = CommandSet(
            battery_max_charge_power_percent_306=_required_percent(
                rcm.recommended_charge_limit_percent,
                "RCM target 306",
                minimum=10.0,
                whole=True,
            )
        )
    elif action is ExecutionAction.RCM_LIMIT_EXPORT:
        export_limit = _required_percent(
            rcm.recommended_export_limit_percent,
            "RCM target 259",
            allow_zero=True,
            maximum=200.0,
            whole=True,
        )
        if export_limit > 0.05:
            raise ActiveBridgeError(
                "positive RCM export limit requires rated-power evidence"
            )
        command = CommandSet(export_limit_percent_259=export_limit)
    else:
        target = _required_percent(
            rcm.latched_pre_discharge_target_soc_percent
            if candidate.active_latched
            else rcm.pre_discharge_target_soc_percent,
            "RCM target 4305",
            whole=True,
        )
        power = _required_percent(
            rcm.latched_pre_discharge_power_percent
            if candidate.active_latched
            else rcm.pre_discharge_power_percent,
            "RCM target 4306",
            whole=True,
        )
        command = CommandSet(
            ems_block=replace(
                before,
                mode=EmsMode.GRID_DISCHARGE,
                force_discharge_soc_percent_4305=target,
                maximum_discharge_power_percent_4306=power,
            )
        )

    deadline = (
        _aware_utc(candidate.valid_until, "candidate deadline")
        if candidate.valid_until is not None
        else now_utc + timedelta(seconds=DIRECT_ACTION_DEADLINE_SECONDS)
    )
    if deadline <= now_utc:
        raise ActiveBridgeError("selected candidate deadline has passed")
    return ActuatorIntent(
        policy=owner,
        action=action,
        command=command,
        candidate_revision=_continuation_identity(candidate),
        deadline=deadline,
    )


def _same_instant(value: object, expected: datetime) -> bool:
    try:
        return _aware_utc(value, "RCE window deadline") == expected
    except ActiveBridgeError:
        return False


def _single_rce_candidate(
    candidates: Iterable[PolicyCandidate],
) -> PolicyCandidate | None:
    matches = tuple(
        candidate for candidate in candidates if candidate.policy_id is PolicyId.RCE
    )
    return matches[0] if len(matches) == 1 else None


def _rce_export_block(intent: ActuatorIntent) -> EmsBlock | None:
    if (
        intent.policy is not ExecutionOwner.RCE
        or intent.action is not ExecutionAction.RCE_EXPORT
        or intent.command.families
        != frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
        or intent.command.ems_block is None
        or intent.command.ems_block.mode is not EmsMode.GRID_DISCHARGE
    ):
        return None
    return intent.command.ems_block


def _same_window_rce_desired(
    current_intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    rce: RceSourceSnapshot,
    now: datetime,
) -> RceRetarget | None:
    """Build a current desired RCE target without granting it write authority."""

    old_block = _rce_export_block(current_intent)
    if old_block is None:
        return None
    now_utc = _aware_utc(now, "now")
    deadline = _aware_utc(current_intent.deadline, "current RCE deadline")
    if (
        now_utc >= deadline
        or decision.supervisor_mode is not SupervisorMode.ACTIVE
        or decision.execution_blocked_reason
        not in {
            None,
            ReasonCode.LOCAL_HARD_STOP,
            ReasonCode.NOT_CONTINUATION_ELIGIBLE,
            ReasonCode.TRANSACTION_PENDING,
        }
        or decision.selected_policy not in {None, PolicyId.RCE}
    ):
        return None
    candidate = _single_rce_candidate(candidates)
    if candidate is None:
        return None
    if (
        decision.selected_policy is PolicyId.RCE
        and decision.selected_candidate_revision != candidate.candidate_revision
    ):
        return None
    try:
        observed_at = _aware_utc(rce.observed_at, "RCE plan observation")
        planned_end = _aware_utc(rce.current_run_end, "RCE planned run end")
        requested_power = _number(
            candidate.requested_power_kw,
            "RCE requested power",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
        requested_energy = _number(
            candidate.requested_energy_kwh,
            "RCE requested energy",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
        target = _required_percent(
            candidate.protected_soc_floor_percent,
            "RCE retarget 4305",
            allow_zero=True,
            whole=True,
        )
        power = _required_percent(
            rce.effective_discharge_power_percent,
            "RCE retarget 4306",
            whole=True,
        )
        current_soc = _required_percent(
            rce.current_soc_percent,
            "RCE current SOC",
            allow_zero=True,
        )
    except ActiveBridgeError:
        return None
    plan_age = (now_utc - observed_at).total_seconds()
    if (
        not 0.0 <= plan_age <= RCE_RETARGET_PLAN_MAX_AGE_SECONDS
        or requested_power <= 0.0
        or requested_energy <= 0.0
        or current_soc
        <= max(target, settings.ems_block.self_use_soc_percent_4301)
        or rce.allowed_by_user is not True
        or rce.enabled is not True
        or rce.status_code is not RcePlanStatus.READY
        or rce.result_current is not True
        or rce.recalculation_pending is not False
        or rce.current_slot_planned is not True
        or rce.current_slot_continue_eligible is not True
        or rce.control_data_ready is not True
        or rce.reserve_ready is not True
        or rce.sale_block_active is not False
        # A fresh plan can shorten the run without replacing the lease.
        # The sensor clips covering plans to the immutable hard deadline.
        or not now_utc < planned_end <= deadline
        or not _same_instant(candidate.valid_until, deadline)
        or candidate.allowed_by_user is not True
        or candidate.enabled is not True
        or candidate.available is not True
        or candidate.result_current is not True
        or candidate.recalculation_pending is not False
        or candidate.requested_action is not RequestedAction.RCE_EXPORT
        or candidate.actuator_scope is not ActuatorScope.EMS_BLOCK_4300_4306
    ):
        return None
    if candidate.active_latched and not _same_instant(rce.latched_slot_end, deadline):
        return None
    command = CommandSet(
        ems_block=replace(
            settings.ems_block,
            mode=EmsMode.GRID_DISCHARGE,
            force_discharge_soc_percent_4305=target,
            maximum_discharge_power_percent_4306=power,
        )
    )
    try:
        identity = _continuation_identity(candidate)
        intent = ActuatorIntent(
            policy=ExecutionOwner.RCE,
            action=ExecutionAction.RCE_EXPORT,
            command=command,
            candidate_revision=identity,
            # A same-window retarget must never extend or replace the lease.
            deadline=deadline,
        )
    except (ActiveBridgeError, ValueError):
        return None
    return RceRetarget(intent=intent, candidate=candidate)


def build_same_window_rce_retarget(
    current_intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    rce: RceSourceSnapshot,
    now: datetime,
) -> RceRetarget | None:
    """Recognize only a newer RCE target masked by old-target incoherence.

    The normal continuation identity remains exact.  This compatibility path
    exists solely while the source still reports the physically confirmed old
    4305/4306 values and every other same-window RCE safety fact is current.
    The executor independently proves the complete old FC03 block before any
    successor can be dispatched.
    """

    proposal = _same_window_rce_desired(
        current_intent,
        decision,
        candidates,
        settings,
        rce=rce,
        now=now,
    )
    if proposal is None:
        return None
    candidate = proposal.candidate
    old_block = _rce_export_block(current_intent)
    assert old_block is not None
    try:
        active_4305 = _required_percent(
            rce.active_4305_readback_percent,
            "active RCE 4305",
            allow_zero=True,
        )
        active_4306 = _required_percent(
            rce.active_4306_readback_percent,
            "active RCE 4306",
            allow_zero=True,
        )
    except ActiveBridgeError:
        return None
    if (
        rce.active_latched is not True
        or candidate.active_latched is not True
        or candidate.local_hard_stop is not True
        or candidate.continuation_eligible is not False
        or abs(active_4305 - old_block.force_discharge_soc_percent_4305) > 0.5
        or abs(active_4306 - old_block.maximum_discharge_power_percent_4306) > 0.05
        or proposal.intent.command == current_intent.command
        or proposal.intent.candidate_revision == current_intent.candidate_revision
    ):
        return None
    return proposal


def same_window_rce_rearm_desired(
    current_intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    rce: RceSourceSnapshot,
    now: datetime,
) -> RceRetarget | None:
    """Return an unchanged current RCE target that needs a fresh lease arm."""

    proposal = _same_window_rce_desired(
        current_intent,
        decision,
        candidates,
        settings,
        rce=rce,
        now=now,
    )
    if proposal is None or proposal.intent.command != current_intent.command:
        return None
    return proposal


def same_window_rce_target_authorized(
    intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    rce: RceSourceSnapshot,
    now: datetime,
) -> PolicyCandidate | None:
    """Return the exact desired candidate for an already prepared retarget."""

    proposal = _same_window_rce_desired(
        intent,
        decision,
        candidates,
        settings,
        rce=rce,
        now=now,
    )
    if proposal is None or proposal.intent != intent:
        return None
    deadline = _aware_utc(intent.deadline, "current RCE deadline")
    if (
        rce.active_latched is not True
        or proposal.candidate.active_latched is not True
        or not _same_instant(rce.latched_slot_end, deadline)
    ):
        return None
    return proposal.candidate


def same_window_rce_retarget_wait_authorized(
    intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    export_state: ExportState,
    rce: RceSourceSnapshot,
    now: datetime,
) -> bool:
    """Keep a dispatched retarget only while both RCE latches remain exact."""

    candidate = _single_rce_candidate(candidates)
    deadline = _aware_utc(intent.deadline, "current RCE deadline")
    return bool(
        candidate is not None
        and rce.active_latched is True
        and candidate.active_latched is True
        and _same_instant(rce.latched_slot_end, deadline)
        and same_window_rce_wait_authorized(
            intent,
            decision,
            candidates,
            settings,
            export_state=export_state,
            rce=rce,
            now=now,
        )
    )


def same_window_rce_wait_authorized(
    intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    export_state: ExportState,
    rce: RceSourceSnapshot,
    now: datetime,
) -> bool:
    """Bound a WAITING readback across one transient RCE recalculation.

    This never changes the command timestamp or deadline.  It only prevents a
    short result-current gap, or an already committed same-window successor,
    from revoking the acknowledgement lease of the command already in flight.
    """
    if intent.action is ExecutionAction.PV_CHARGE_HOLD:
        candidate = _single_rce_candidate(candidates)
        return bool(candidate is not None
            and decision.supervisor_mode is SupervisorMode.ACTIVE
            and decision.execution_blocked_reason in {None, ReasonCode.TRANSACTION_PENDING}
            and decision.selected_policy in {None, PolicyId.RCE}
            and export_state is ExportState.VERIFIED_ALLOWED
            and candidate.requested_action is RequestedAction.PV_CHARGE_HOLD
            and candidate.start_eligible and candidate.result_current
            and not candidate.recalculation_pending and not candidate.local_hard_stop
            and _continuation_identity(candidate) == intent.candidate_revision
            and _same_instant(candidate.valid_until, intent.deadline)
            and now < intent.deadline)

    old_block = _rce_export_block(intent)
    if old_block is None:
        return False
    now_utc = _aware_utc(now, "now")
    deadline = _aware_utc(intent.deadline, "current RCE deadline")
    if (
        now_utc >= deadline
        or decision.supervisor_mode is not SupervisorMode.ACTIVE
        or decision.selected_policy not in {None, PolicyId.RCE}
        or export_state is not ExportState.VERIFIED_ALLOWED
        or rce.allowed_by_user is not True
        or rce.enabled is not True
        or rce.sale_block_active is not False
    ):
        return False
    try:
        observed_at = _aware_utc(rce.observed_at, "RCE plan observation")
        planned_end = _aware_utc(rce.current_run_end, "RCE planned run end")
        current_soc = _required_percent(
            rce.current_soc_percent,
            "RCE current SOC",
            allow_zero=True,
        )
    except ActiveBridgeError:
        return False
    if (
        not now_utc < planned_end <= deadline
        or not 0.0
        <= (now_utc - observed_at).total_seconds()
        <= RCE_RETARGET_PLAN_MAX_AGE_SECONDS
        or current_soc
        <= max(
            old_block.force_discharge_soc_percent_4305,
            settings.ems_block.self_use_soc_percent_4301,
        )
    ):
        return False
    candidate = _single_rce_candidate(candidates)
    if candidate is None or candidate.allowed_by_user is not True or candidate.enabled is not True:
        return False
    if (
        rce.status_code is RcePlanStatus.READY
        and rce.result_current is False
        and rce.recalculation_pending is True
        and rce.current_slot_planned is True
        and isinstance(rce.current_slot_start_eligible, bool)
        and rce.current_slot_continue_eligible is True
        and candidate.result_current is False
        and candidate.recalculation_pending is True
        and (
            candidate.requested_action is RequestedAction.NONE
            or (
                candidate.available is True
                and candidate.requested_action is RequestedAction.RCE_EXPORT
                and candidate.actuator_scope is ActuatorScope.EMS_BLOCK_4300_4306
                and _same_instant(candidate.valid_until, deadline)
            )
        )
        and decision.execution_blocked_reason
        in {
            None,
            ReasonCode.RECALCULATION_PENDING_NEW_START,
            ReasonCode.RESULT_NOT_CURRENT,
            ReasonCode.TRANSACTION_PENDING,
        }
    ):
        return True
    return (
        _same_window_rce_desired(
            intent,
            decision,
            candidates,
            settings,
            rce=rce,
            now=now,
        )
        is not None
    )


def rce_sent_command_within_live_bms_limit(
    intent: ActuatorIntent,
    execution_source: ExecutionSourceSnapshot,
    *,
    rce: RceSourceSnapshot,
    now: datetime,
) -> bool:
    """Prove that the already-sent RCE command still fits the live BMS cap."""
    if intent.action is ExecutionAction.PV_CHARGE_HOLD:
        if __package__:
            from .pv_charge_delay_control import sent_command_ready
        else:
            from pv_charge_delay_control import sent_command_ready
        return sent_command_ready(intent, execution_source, rce=rce, now=now)

    old_block = _rce_export_block(intent)
    if old_block is None:
        return False
    now_utc = _aware_utc(now, "now")
    if (
        execution_source.hardware_readback_supported is not True
        or not _full_block_topology_ready(execution_source, now_utc)
    ):
        return False
    try:
        plan_observed_at = _aware_utc(rce.observed_at, "RCE plan observation")
        system_power_kw = _number(
            rce.system_power_kw,
            "RCE system power",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
        voltage_v = _number(
            execution_source.bms_voltage_v,
            "BMS voltage",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
        discharge_current_a = _number(
            execution_source.bms_max_discharge_current_a,
            "BMS maximum discharge current",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
    except ActiveBridgeError:
        return False
    if (
        system_power_kw <= 0.0
        or voltage_v <= 0.0
        or discharge_current_a <= 0.0
        or not _fresh(plan_observed_at, now_utc, maximum_age=RCE_RETARGET_PLAN_MAX_AGE_SECONDS)
        or not _fresh(
            execution_source.bms_voltage_observed_at,
            now_utc,
            maximum_age=300.0,
        )
        or not _fresh(
            execution_source.bms_discharge_current_observed_at,
            now_utc,
            maximum_age=300.0,
        )
    ):
        return False
    sent_power_kw = (
        old_block.maximum_discharge_power_percent_4306 * system_power_kw / 100.0
    )
    live_bms_power_kw = voltage_v * discharge_current_a * 0.95 / 1000.0
    return sent_power_kw <= live_bms_power_kw + 0.05


def rcm_charge_command_within_live_bms_limit(
    intent: ActuatorIntent,
    execution_source: ExecutionSourceSnapshot,
    *,
    rcm: RcmSourceSnapshot,
    now: datetime,
) -> bool:
    """Prove that an active RCEm 306 target still fits current BMS authority."""

    if intent.action not in {
        ExecutionAction.RCM_ABSORB_PV,
        ExecutionAction.RCM_ABSORB_AND_LIMIT,
    }:
        return False
    target = intent.command.battery_max_charge_power_percent_306
    now_utc = _aware_utc(now, "now")
    if (
        target is None
        or execution_source.hardware_readback_supported is not True
        or not _direct_topology_ready(execution_source, now_utc)
        or rcm.charge_path_locally_valid is not True
    ):
        return False
    try:
        target_percent = _required_percent(
            target,
            "active RCM target 306",
            minimum=10.0,
            whole=True,
        )
        current_target = _required_percent(
            rcm.recommended_charge_limit_percent,
            "current RCM target 306",
            minimum=10.0,
            whole=True,
        )
        current_power_kw = _number(
            rcm.recommended_charge_power_kw,
            "current RCM charge power",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
        voltage_v = _number(
            execution_source.bms_voltage_v,
            "BMS voltage",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
        charge_current_a = _number(
            execution_source.bms_max_charge_current_a,
            "BMS maximum charge current",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
    except ActiveBridgeError:
        return False
    if (
        current_power_kw <= 0.0
        or voltage_v <= 0.0
        or charge_current_a <= 0.0
        or target_percent > current_target + 0.05
        or not _fresh(rcm.observed_at, now_utc, maximum_age=300.0)
        or not _fresh(
            execution_source.bms_voltage_observed_at,
            now_utc,
            maximum_age=300.0,
        )
        or not _fresh(
            execution_source.bms_charge_current_observed_at,
            now_utc,
            maximum_age=300.0,
        )
    ):
        return False
    system_power_kw = current_power_kw * 100.0 / current_target
    commanded_kw = target_percent * system_power_kw / 100.0
    live_bms_power_kw = voltage_v * charge_current_a / 1000.0
    return commanded_kw <= live_bms_power_kw + 0.05


def rcm_pre_discharge_command_within_live_bms_limit(
    intent: ActuatorIntent,
    execution_source: ExecutionSourceSnapshot,
    *,
    rcm: RcmSourceSnapshot,
    now: datetime,
) -> bool:
    """Prove that active RCEm 4306 remains inside fresh DCL authority."""

    block = intent.command.ems_block
    now_utc = _aware_utc(now, "now")
    if (
        intent.action is not ExecutionAction.RCM_PRE_DISCHARGE
        or block is None
        or execution_source.hardware_readback_supported is not True
        or not _full_block_topology_ready(execution_source, now_utc)
        or rcm.allowed_by_user is not True
        or rcm.enabled is not True
        or rcm.result_current is not True
        or rcm.recalculation_pending is not False
        or rcm.action is not RcmAction.GRID_DISCHARGE_PREPARATION
        or rcm.pre_discharge_enabled is not True
        or rcm.pre_discharge_continue_eligible is not True
        or rcm.sale_block_active is not False
        or not _same_instant(rcm.pre_discharge_deadline, intent.deadline)
    ):
        return False
    try:
        target_percent = _required_percent(
            block.maximum_discharge_power_percent_4306,
            "active RCM target 4306",
            whole=True,
        )
        current_target = _required_percent(
            rcm.pre_discharge_power_percent,
            "current RCM target 4306",
            whole=True,
        )
        current_power_kw = _number(
            rcm.pre_discharge_power_kw,
            "current RCM pre-discharge power",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
        voltage_v = _number(
            execution_source.bms_voltage_v,
            "BMS voltage",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
        discharge_current_a = _number(
            execution_source.bms_max_discharge_current_a,
            "BMS maximum discharge current",
            minimum=0.0,
            maximum=1_000_000_000.0,
        )
    except ActiveBridgeError:
        return False
    if (
        current_power_kw <= 0.0
        or voltage_v <= 0.0
        or discharge_current_a <= 0.0
        or target_percent > current_target + 0.05
        or not _fresh(rcm.observed_at, now_utc, maximum_age=300.0)
        or not _fresh(
            execution_source.bms_voltage_observed_at,
            now_utc,
            maximum_age=300.0,
        )
        or not _fresh(
            execution_source.bms_discharge_current_observed_at,
            now_utc,
            maximum_age=300.0,
        )
    ):
        return False
    system_power_kw = current_power_kw * 100.0 / current_target
    commanded_kw = target_percent * system_power_kw / 100.0
    live_bms_power_kw = voltage_v * discharge_current_a * 0.95 / 1000.0
    return commanded_kw <= live_bms_power_kw + 0.05


def post_command_rce_measurement_hold_authorized(
    intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    execution_source: ExecutionSourceSnapshot,
    export_state: ExportState,
    rce: RceSourceSnapshot,
    market_fingerprint: str,
    recognized_pending: bool,
    now: datetime,
) -> bool:
    """Retain only an acknowledged command during its bounded LOAD response.

    The controller supplies the original market basis and command-time budget.
    This predicate never creates a successor or changes published plan authority.
    """
    old_block = _rce_export_block(intent)
    if old_block is None or not isinstance(market_fingerprint, str):
        return False
    if not _SHA256.fullmatch(market_fingerprint):
        return False
    now_utc = _aware_utc(now, "now")
    if (
        now_utc >= intent.deadline
        or decision.supervisor_mode is not SupervisorMode.ACTIVE
        or decision.selected_policy not in {None, PolicyId.RCE}
        or export_state is not ExportState.VERIFIED_ALLOWED
        or rce.allowed_by_user is not True
        or rce.enabled is not True
        or rce.active_latched is not True
        or rce.sale_block_active is not False
        or rce.post_command_settling_market_fingerprint != market_fingerprint
        or type(rce.input_revision) is not int
        or rce.input_revision < 0
        or not _same_instant(rce.latched_slot_end, intent.deadline)
        or not all(type(value) is bool for value in (
            rce.control_data_ready, rce.reserve_ready,
            rce.current_slot_planned, rce.current_slot_start_eligible,
            rce.current_slot_continue_eligible,
        ))
    ):
        return False
    candidate = _single_rce_candidate(candidates)
    if candidate is None or candidate.enabled is not True or candidate.allowed_by_user is not True:
        return False
    pending = bool(
        recognized_pending
        and rce.result_current is False
        and rce.recalculation_pending is True
        and candidate.result_current is False
        and candidate.recalculation_pending is True
    )
    fresh_load_block = bool(
        rce.result_current is True
        and rce.recalculation_pending is False
        and candidate.result_current is True
        and candidate.recalculation_pending is False
        and rce.current_slot_suppression_reason == "no_current_plan"
        and rce.current_slot_load_exhausts_requested_discharge_budget is True
        and rce.current_slot_planned is False
        and rce.current_slot_continue_eligible is False
        and type(rce.planned_export_energy_kwh) in {int, float}
        and rce.planned_export_energy_kwh == 0
        and type(rce.requested_discharge_power_kw) in {int, float}
        and rce.requested_discharge_power_kw == 0
    )
    fresh_minimum_export_block = bool(
        rce.result_current is True and rce.recalculation_pending is False
        and candidate.result_current is True and candidate.recalculation_pending is False
        and rce.current_slot_load_only_export_suppressed is True
        and rce.current_slot_start_eligible is False
        and rce.current_slot_continue_eligible is False
        and type(rce.planned_export_energy_kwh) in {int, float}
        and isfinite(rce.planned_export_energy_kwh)
        and type(rce.requested_discharge_power_kw) in {int, float}
        and isfinite(rce.requested_discharge_power_kw)
        and (
            (rce.current_slot_suppression_reason == "no_current_plan"
             and rce.current_slot_planned is False
             and rce.planned_export_energy_kwh == 0
             and rce.requested_discharge_power_kw == 0)
            or (rce.current_slot_suppression_reason == "live_power_insufficient"
                and rce.current_slot_planned is True
                and rce.planned_export_energy_kwh > 0
                and rce.requested_discharge_power_kw >= 0)
        )
    )
    if not (pending or fresh_load_block or fresh_minimum_export_block):
        return False
    # Home-energy shortage, missing inputs and optimizer failures remain stops.
    if rce.status_code not in {RcePlanStatus.READY, RcePlanStatus.HOME_PROTECTED}:
        return False
    try:
        for name, value, reported_at, minimum in (
            ("LOAD", execution_source.load_power_w, execution_source.load_power_observed_at, 0.0),
            ("PV", execution_source.pv_power_w, execution_source.pv_power_observed_at, 0.0),
            ("GRID", execution_source.grid_power_w, execution_source.grid_power_observed_at, -1_000_000_000.0),
        ):
            _number(value, "settling " + name, minimum=minimum, maximum=1_000_000_000.0)
            if not _fresh(reported_at, now_utc, maximum_age=PHYSICAL_MAX_AGE_SECONDS):
                return False
        observed = _aware_utc(rce.observed_at, "RCE plan observation")
        current_soc = _required_percent(rce.current_soc_percent, "RCE SOC", allow_zero=True)
        live_soc = _required_percent(execution_source.battery_soc_percent, "physical SOC", allow_zero=True)
        if not _fresh(execution_source.battery_soc_observed_at, now_utc):
            return False
        new_floor = _required_percent(rce.protected_soc_floor_percent, "RCE reserve", allow_zero=True)
        _required_percent(rce.requested_discharge_power_percent, "RCE requested power")
        active_soc = _required_percent(rce.active_4305_readback_percent, "RCE physical SOC", allow_zero=True)
        active_power = _required_percent(rce.active_4306_readback_percent, "RCE physical power")
    except (ActiveBridgeError, TypeError, ValueError):
        return False
    return bool(
        0.0 <= (now_utc - observed).total_seconds() <= RCE_RETARGET_PLAN_MAX_AGE_SECONDS
        and min(current_soc, live_soc) > max(new_floor, old_block.force_discharge_soc_percent_4305,
                              settings.ems_block.self_use_soc_percent_4301)
        and abs(active_soc - old_block.force_discharge_soc_percent_4305) <= 0.5
        and abs(active_power - old_block.maximum_discharge_power_percent_4306) <= 0.05
        and rce_sent_command_within_live_bms_limit(
            intent, execution_source, rce=rce, now=now_utc,
        )
    )


def same_window_rce_execution_hold_seconds(
    intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    execution_source: ExecutionSourceSnapshot,
    export_state: ExportState,
    rce: RceSourceSnapshot,
    now: datetime,
) -> float | None:
    """Return the non-renewable hold for one exact same-run RCE gap.

    An explicit planner recalculation may last up to 180 seconds.  A committed
    successor split across HA helper events keeps the bounded sixty-second
    cohort allowance.  Neither result grants write authority or renews the
    current RCE lease.
    """

    old_block = _rce_export_block(intent)
    if old_block is None:
        return None
    now_utc = _aware_utc(now, "now")
    deadline = _aware_utc(intent.deadline, "current RCE deadline")
    try:
        planned_end = _aware_utc(rce.current_run_end, "RCE planned run end")
    except ActiveBridgeError:
        return None
    if (
        now_utc >= min(deadline, planned_end)
        or decision.supervisor_mode is not SupervisorMode.ACTIVE
        or decision.selected_policy not in {None, PolicyId.RCE}
        or export_state is not ExportState.VERIFIED_ALLOWED
        or rce.allowed_by_user is not True
        or rce.enabled is not True
        or rce.active_latched is not True
        or rce.status_code is not RcePlanStatus.READY
        or rce.current_slot_planned is not True
        or rce.current_slot_continue_eligible is not True
        or rce.sale_block_active is not False
    ):
        return None
    try:
        observed_at = _aware_utc(rce.observed_at, "RCE plan observation")
        current_soc = _required_percent(
            rce.current_soc_percent,
            "RCE current SOC",
            allow_zero=True,
        )
        active_4305 = _required_percent(
            rce.active_4305_readback_percent,
            "active RCE 4305",
            allow_zero=True,
        )
        active_4306 = _required_percent(
            rce.active_4306_readback_percent,
            "active RCE 4306",
            allow_zero=True,
        )
    except ActiveBridgeError:
        return None
    plan_age = (now_utc - observed_at).total_seconds()
    if (
        not 0.0 <= plan_age <= RCE_RETARGET_PLAN_MAX_AGE_SECONDS
        or current_soc
        <= max(
            old_block.force_discharge_soc_percent_4305,
            settings.ems_block.self_use_soc_percent_4301,
        )
        or abs(active_4305 - old_block.force_discharge_soc_percent_4305) > 0.5
        or abs(active_4306 - old_block.maximum_discharge_power_percent_4306) > 0.05
        or not rce_sent_command_within_live_bms_limit(
            intent,
            execution_source,
            rce=rce,
            now=now_utc,
        )
    ):
        return None
    candidate = _single_rce_candidate(candidates)
    if (
        candidate is None
        or candidate.allowed_by_user is not True
        or candidate.enabled is not True
    ):
        return None
    try:
        pending_identity_matches = (
            _continuation_identity(candidate) == intent.candidate_revision
        )
    except ActiveBridgeError:
        pending_identity_matches = False
    if (
        rce.result_current is False
        and rce.recalculation_pending is True
        and isinstance(rce.control_data_ready, bool)
        and isinstance(rce.reserve_ready, bool)
        and candidate.result_current is False
        and candidate.recalculation_pending is True
        and decision.execution_blocked_reason
        in {
            None,
            ReasonCode.LOCAL_HARD_STOP,
            ReasonCode.NOT_CONTINUATION_ELIGIBLE,
            ReasonCode.RESULT_NOT_CURRENT,
            ReasonCode.RECALCULATION_PENDING_NEW_START,
        }
        and candidate.active_latched is True
        and (
            candidate.requested_action is RequestedAction.NONE
            or (
                candidate.available is True
                and candidate.active_latched is True
                and candidate.requested_action is RequestedAction.RCE_EXPORT
                and candidate.actuator_scope is ActuatorScope.EMS_BLOCK_4300_4306
                and pending_identity_matches
                and _same_instant(candidate.valid_until, deadline)
            )
        )
    ):
        # A fresh plan may shorten the economic run while the immutable ESP
        # hard deadline stays unchanged. A subsequent periodic replan must
        # not stop the already confirmed command before that shorter end.
        # This only retains the existing command, never renews its lease or
        # grants a retarget, and cannot outlive either run boundary.
        return min(
            RCE_RECALCULATION_HOLD_SECONDS,
            (min(deadline, planned_end) - now_utc).total_seconds(),
        )
    try:
        target = _required_percent(
            candidate.protected_soc_floor_percent,
            "settling RCE 4305",
            allow_zero=True,
            whole=True,
        )
        power = _required_percent(
            rce.effective_discharge_power_percent,
            "settling RCE 4306",
            whole=True,
        )
    except ActiveBridgeError:
        return None
    if (
        not (
            0.0 <= plan_age <= RCE_RETARGET_COHORT_HOLD_SECONDS
            and rce.result_current is True
            and rce.recalculation_pending is False
            and decision.execution_blocked_reason
            in {
                None,
                ReasonCode.LOCAL_HARD_STOP,
                ReasonCode.NOT_CONTINUATION_ELIGIBLE,
            }
            and candidate.available is True
            and candidate.result_current is True
            and candidate.recalculation_pending is False
            and candidate.active_latched is True
            and candidate.local_hard_stop is True
            and candidate.continuation_eligible is False
            and candidate.requested_action is RequestedAction.RCE_EXPORT
            and candidate.actuator_scope is ActuatorScope.EMS_BLOCK_4300_4306
            and _same_instant(candidate.valid_until, deadline)
            and current_soc
            > max(target, settings.ems_block.self_use_soc_percent_4301)
            and (
                rce.control_data_ready is not True
                or rce.reserve_ready is not True
            )
            and (
                abs(target - old_block.force_discharge_soc_percent_4305) > 0.5
                or abs(power - old_block.maximum_discharge_power_percent_4306)
                > 0.05
            )
        )
    ):
        return None
    return min(
        RCE_RETARGET_COHORT_HOLD_SECONDS,
        (min(deadline, planned_end) - now_utc).total_seconds(),
    )


def same_window_rce_execution_hold_authorized(
    intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    execution_source: ExecutionSourceSnapshot,
    export_state: ExportState,
    rce: RceSourceSnapshot,
    now: datetime,
) -> bool:
    """Compatibility predicate for callers that only need yes/no."""

    return same_window_rce_execution_hold_seconds(
        intent,
        decision,
        candidates,
        settings,
        execution_source=execution_source,
        export_state=export_state,
        rce=rce,
        now=now,
    ) is not None


def _required_percent(
    value: object,
    name: str,
    *,
    minimum: float = 0.0,
    maximum: float = 100.0,
    allow_zero: bool = False,
    whole: bool = False,
) -> float:
    normalized = _number(value, name, minimum=minimum, maximum=maximum)
    if whole:
        # New targets are whole percentages; physical readback and old restore
        # snapshots retain the native register precision.
        normalized = _register_number(
            normalized, name, minimum=minimum, maximum=maximum, scale=1
        )
    if not allow_zero and normalized <= 0.0:
        raise ActiveBridgeError(f"{name} must be positive")
    return normalized


def authorization_matches(
    record_intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    *,
    allow_selected_start: bool = False,
) -> bool:
    """Confirm that a fresh arbitration frame still authorizes this intent."""

    try:
        candidate = selected_candidate(decision, candidates)
    except ActiveBridgeError:
        return False
    if candidate is None or candidate.desired_actuator_fingerprint is None:
        return False
    try:
        identity = _continuation_identity(candidate)
    except ActiveBridgeError:
        return False
    return bool(
        decision.supervisor_mode is SupervisorMode.ACTIVE
        and _POLICY_OWNER.get(candidate.policy_id) is record_intent.policy
        and _ACTION_MAP.get(candidate.requested_action) is record_intent.action
        and identity == record_intent.candidate_revision
        and (
            candidate.continuation_eligible
            or (
                allow_selected_start
                and not candidate.active_latched
                and candidate.start_eligible
                and candidate.result_current
                and not candidate.recalculation_pending
            )
        )
        and not candidate.local_hard_stop
    )


@dataclass(frozen=True, slots=True)
class TariffRetarget:
    intent: ActuatorIntent
    candidate: PolicyCandidate
    planned_end: datetime


def same_run_tariff_desired(
    current_intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    tariff: TariffSourceSnapshot,
    now: datetime,
) -> TariffRetarget | None:
    """Recognize a fresh tariff target within the original continuous Mode 4 run.

    A differing readback may describe the predecessor, so this supplies only
    desired intent. The executor must independently prove the full predecessor
    before any update, and the dispatch boundary checks this exact intent again.
    """
    if current_intent.policy is not ExecutionOwner.TARIFF or _retarget_mode(current_intent) is not EmsMode.GRID_CHARGE:
        return None
    now_utc = _aware_utc(now, "now")
    deadline = _aware_utc(current_intent.deadline, "tariff deadline")
    try:
        raw_plan_end = _aware_utc(
            tariff.current_grid_charge_run_end,
            "tariff current run end",
        )
    except ActiveBridgeError:
        return None
    planned_end = min(raw_plan_end, deadline)
    matches = tuple(c for c in candidates if c.policy_id is PolicyId.TARIFF)
    if len(matches) != 1:
        return None
    candidate = matches[0]
    if (
        now_utc >= deadline
        or decision.supervisor_mode is not SupervisorMode.ACTIVE
        or decision.execution_blocked_reason is not None
        or decision.selected_policy not in {None, PolicyId.TARIFF}
        or (decision.selected_policy is PolicyId.TARIFF and decision.selected_candidate_revision != candidate.candidate_revision)
        or now_utc >= planned_end
        or not _same_instant(candidate.valid_until, raw_plan_end)
        or (tariff.active_latched is True and not _same_instant(tariff.latched_slot_end, deadline))
        or tariff.allowed_by_user is not True
        or tariff.enabled is not True
        or tariff.result_current is not True
        or tariff.recalculation_pending is not False
        or tariff.current_slot_planned is not True
        or tariff.current_run_continue_eligible is not True
        or tariff.control_data_ready is not True
        or candidate.allowed_by_user is not True
        or candidate.enabled is not True
        or candidate.available is not True
        or candidate.result_current is not True
        or candidate.recalculation_pending is not False
        or candidate.actuator_scope is not ActuatorScope.EMS_BLOCK_4300_4306
        or candidate.requested_action not in {
            RequestedAction.TARIFF_BATTERY_CHARGE,
            RequestedAction.TARIFF_GRID_SUPPORT,
            RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        }
    ):
        return None
    try:
        observed = _aware_utc(tariff.observed_at, "tariff observation")
        target = _required_percent(candidate.target_soc_percent, "tariff target", minimum=10.0, whole=True)
        cap = _required_percent(tariff.maximum_soc_percent, "tariff maximum SOC", minimum=10.0)
        power = _required_percent(tariff.command_charge_power_percent, "tariff power", whole=True)
        if not 0.0 <= (now_utc-observed).total_seconds() <= 300.0 or target > cap:
            return None
        command = CommandSet(ems_block=replace(settings.ems_block,
            mode=EmsMode.GRID_CHARGE, force_charge_soc_percent_4303=target,
            maximum_charge_power_percent_4304=power))
        intent = ActuatorIntent(policy=ExecutionOwner.TARIFF,
            action=_ACTION_MAP[candidate.requested_action],command=command,
            candidate_revision=_continuation_identity(candidate),deadline=deadline)
    except (ActiveBridgeError,ValueError):
        return None
    return TariffRetarget(
        intent=intent,
        candidate=candidate,
        planned_end=planned_end,
    )


def tariff_command_within_live_bms_limit(
    intent: ActuatorIntent,
    execution_source: ExecutionSourceSnapshot,
    *,
    tariff: TariffSourceSnapshot,
    now: datetime,
) -> bool:
    """Check this command against live CCL, not a historical BMS capacity.

    Comparing the Grid Charge request to the DC budget at unity efficiency is
    conservative. Unlike discharge/export, no 0.95 conversion is applied here.
    The 4304 request is not a cap on total battery charging from PV plus grid.
    """
    if (
        intent.policy is not ExecutionOwner.TARIFF
        or _retarget_mode(intent) is not EmsMode.GRID_CHARGE
        or execution_source.hardware_readback_supported is not True
        or not _full_block_topology_ready(execution_source, now)
    ):
        return False
    try:
        system_power = _number(tariff.system_power_kw, "tariff system power", minimum=0.0, maximum=1e9)
        voltage = _number(execution_source.bms_voltage_v, "BMS voltage", minimum=0.0, maximum=1e9)
        current = _number(execution_source.bms_max_charge_current_a, "BMS charge current", minimum=0.0, maximum=1e9)
    except ActiveBridgeError:
        return False
    if (
        not 0.0 < system_power < 1e9
        or not 0.0 < voltage < 1e9 or not 0.0 < current < 1e9
        or not _fresh(tariff.observed_at, now, maximum_age=300.0)
        or not _fresh(execution_source.bms_voltage_observed_at, now, maximum_age=300.0)
        or not _fresh(execution_source.bms_charge_current_observed_at, now, maximum_age=300.0)
    ):
        return False
    command_power = intent.command.ems_block.maximum_charge_power_percent_4304 * system_power / 100.0
    return command_power > 0.0 and command_power <= voltage * current / 1000.0 + 0.05


def same_run_tariff_wait_authorized(
    intent: ActuatorIntent,
    decision: SupervisorDecision,
    candidates: Iterable[PolicyCandidate],
    settings: SettingsSnapshot,
    *,
    tariff: TariffSourceSnapshot,
    execution_source: ExecutionSourceSnapshot,
    now: datetime,
    planned_end: datetime | None = None,
) -> bool:
    """Recognize only an explicit recalculation of the still-authorized run.

    This grants no new write. The controller bounds the wait and the executor
    separately checks the old command, physical inputs and immutable deadline.
    """
    matches = tuple(c for c in candidates if c.policy_id is PolicyId.TARIFF)
    try:
        current_cap = _required_percent(tariff.maximum_soc_percent,"tariff current maximum SOC",minimum=10.0)
        hard_deadline = _aware_utc(intent.deadline, "tariff hard deadline")
        raw_plan_end = _aware_utc(
            tariff.current_grid_charge_run_end,
            "tariff current run end",
        )
        expected_end = (
            min(_aware_utc(planned_end, "tariff planned end"), hard_deadline)
            if planned_end is not None
            else hard_deadline
        )
    except ActiveBridgeError:
        return False
    hard_ready = bool(
        intent.policy is ExecutionOwner.TARIFF
        and _retarget_mode(intent) is EmsMode.GRID_CHARGE
        and intent.command.ems_block.force_charge_soc_percent_4303 <= current_cap
        and now < expected_end
        and decision.supervisor_mode is SupervisorMode.ACTIVE
        and decision.execution_blocked_reason is None
        and decision.selected_policy in {None, PolicyId.TARIFF}
        and tariff.allowed_by_user is True and tariff.enabled is True
        and tariff.active_latched is True
        and tariff.status_code in {
            TariffPlanStatus.READY,
            TariffPlanStatus.INSUFFICIENT_CHEAP_WINDOW,
        }
        and tariff.current_slot_planned is True
        and tariff.current_run_continue_eligible is True
        and type(tariff.control_data_ready) is bool
        and tariff.control_inputs_fresh is True and tariff.forecast_data_fresh is True
        and tariff_command_within_live_bms_limit(
            intent, execution_source, tariff=tariff, now=now,
        )
        and _same_instant(min(raw_plan_end, hard_deadline), expected_end)
        and _same_instant(tariff.latched_slot_end, hard_deadline)
        and _fresh(tariff.observed_at, now, maximum_age=300.0)
        and len(matches) == 1
        and matches[0].allowed_by_user is True and matches[0].enabled is True
    )
    if not hard_ready:
        return False
    if (tariff.result_current is False and tariff.recalculation_pending is True
        and matches[0].result_current is False and matches[0].recalculation_pending is True):
        return True
    # A committed successor can arrive before the plan-dependent HA readiness
    # helper. Independently verified raw inputs authorize only holding A;
    # dispatch still requires the unmodified helper and a current full frame.
    return bool(tariff.control_data_ready is False and same_run_tariff_desired(
        intent,decision,candidates,settings,
        tariff=replace(tariff,control_data_ready=True),now=now,
    ) is not None)


def rce_export_evidence(source: ExecutionSourceSnapshot, *, now: datetime) -> dict[str, Any]:
    """Observation-only net export proof; battery discharge alone is insufficient."""
    values: dict[str, float] = {}
    stamps: list[datetime] = []
    for channel in ("grid", "battery", "pv", "load"):
        at = getattr(source, channel + "_power_observed_at")
        try:
            value = _number(getattr(source, channel + "_power_w"), channel,
                            minimum=-10_000_000, maximum=10_000_000)
            at = _aware_utc(at, channel)
        except (ActiveBridgeError, TypeError, ValueError):
            return {"status": "pending", "reason": "missing_flow_cohort", "control_authority": False}
        if not _fresh(at, now, maximum_age=PHYSICAL_MAX_AGE_SECONDS):
            return {"status": "pending", "reason": "stale_flow_cohort", "control_authority": False}
        values[channel] = value
        stamps.append(at)
    residual = values["pv"] + values["battery"] - values["load"] - values["grid"]
    coherent = (source.power_cohort_complete is not False
                and (max(stamps) - min(stamps)).total_seconds() <= PHYSICAL_COHORT_SPAN_SECONDS
                and abs(residual) <= max(POWER_BALANCE_ABSOLUTE_TOLERANCE_W,
                    POWER_BALANCE_RELATIVE_TOLERANCE * max(abs(v) for v in values.values())))
    exporting = coherent and values["grid"] > FLOW_THRESHOLD_W
    return {"status": "confirmed" if exporting else "pending",
            "reason": "net_export_observed" if exporting else
                      "incoherent_flow_cohort" if not coherent else "no_net_export",
            "observed_at": min(stamps).isoformat(), "power_w": values,
            "balance_residual_w": residual, "control_authority": False}


def physical_verification(
    transaction_id: str,
    action: ExecutionAction,
    source: ExecutionSourceSnapshot,
    *,
    command_sent_at: datetime,
    now: datetime,
    export_limit_target_percent: float | None = None,
    allow_rce_settling: bool = False,
    allow_pv_hold_settling: bool = False,
    rce_confirmed_continuation: bool = False,
    allow_tariff_settling: bool = False,
) -> PhysicalVerification:
    """Classify newer physical FC03 power evidence for one transaction.

    Native inverter signs are preserved: positive GRID/BAT is export/discharge,
    while negative GRID/BAT is import/charge.  Tariff charging deliberately
    separates execution proof from accounting authority: a coherent physical
    system-flow cohort may confirm execution without manufacturing a dedicated
    Grid-to-Battery value.  Accounting remains fail-closed in its own ledger.
    """

    now_utc = _aware_utc(now, "now")
    sent = _aware_utc(command_sent_at, "command_sent_at")
    if action is ExecutionAction.PV_CHARGE_HOLD:
        # This action delegates power routing to inverter Mode 5. Confirm the
        # physical configuration, not a sum of asynchronously reported powers.
        # The executor/lease additionally compare the entire intended block
        # and its generation against the pre-command FC03 snapshot.
        try:
            settings = settings_from_execution_source(source)
            if (source.hardware_readback_supported is not True
                or source.full_block_execution_ready is not True
                or not _full_block_topology_ready(source, now_utc)
                or settings.freshness_errors(now_utc,
                    maximum_age_seconds=PHYSICAL_MAX_AGE_SECONDS,
                    required_families=frozenset())):
                raise ActiveBridgeError("PV hold FC03 unavailable")
        except (ActiveBridgeError, TypeError, ValueError):
            return _verification(transaction_id, action, VerificationStatus.UNAVAILABLE,
                now_utc, "pv_hold_mode5_fc03_unavailable")
        if settings.ems_observed_at <= sent:
            return _verification(transaction_id, action, VerificationStatus.PENDING,
                settings.ems_observed_at, "ems_readback_not_newer_than_command")
        block = settings.ems_block
        status = (VerificationStatus.CONFIRMED
            if block.mode is EmsMode.GRID_DISCHARGE
                and block.maximum_discharge_power_percent_4306 == 1.
            else VerificationStatus.CONTRADICTED)
        return PhysicalVerification(transaction_id=transaction_id, action=action,
            status=status, observed_at=settings.ems_observed_at,
            evidence=("confirmation_source=pv_hold_mode5_fc03",
                f"ems_mode={block.mode.value}", f"ems_generation={settings.ems_generation}",
                "power_flows=diagnostic_only", "energy_effect=not_attested_by_mode_readback"))
    tariff_charge = action in _TARIFF_CHARGE_ACTIONS
    tariff_support = action is ExecutionAction.TARIFF_GRID_SUPPORT
    tariff_mode4 = tariff_charge or tariff_support
    tariff_evidence: list[str] = []
    if tariff_mode4:
        critical_grid_at = source.critical_grid_power_observed_at
        if (
            isinstance(critical_grid_at, datetime)
            and critical_grid_at.tzinfo is not None
        ):
            critical_grid_timestamp = critical_grid_at.astimezone(timezone.utc)
            if critical_grid_timestamp > sent and _fresh(
                critical_grid_timestamp,
                now_utc,
                maximum_age=TARIFF_PHYSICAL_MAX_AGE_SECONDS,
            ):
                try:
                    critical_grid = _number(
                        source.critical_grid_power_w,
                        "critical grid",
                        minimum=-10_000_000.0,
                        maximum=10_000_000.0,
                    )
                except ActiveBridgeError:
                    pass
                else:
                    if critical_grid > FLOW_THRESHOLD_W:
                        return PhysicalVerification(
                            transaction_id=transaction_id,
                            action=action,
                            status=VerificationStatus.CONTRADICTED,
                            observed_at=critical_grid_timestamp,
                            evidence=(
                                f"critical_grid_export_w={critical_grid:.3f}",
                                "balance_cohort_authority=revocation_only",
                            ),
                        )
        # ``None`` is retained for codec/test callers predating the cohort
        # marker.  The production adapter always supplies an explicit bool.
        if source.power_cohort_complete is False:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.PENDING,
                now_utc,
                "physical_cohort_incomplete",
            )
        readback_at = source.full_block_generation_at
        if not isinstance(readback_at, datetime) or readback_at.tzinfo is None:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                now_utc,
                "ems_readback_timestamp_unavailable",
            )
        readback_timestamp = readback_at.astimezone(timezone.utc)
        if readback_timestamp <= sent:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.PENDING,
                readback_timestamp,
                "ems_readback_not_newer_than_command",
            )
        if not _fresh(
            readback_timestamp,
            now_utc,
            maximum_age=PHYSICAL_MAX_AGE_SECONDS,
        ):
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                readback_timestamp,
                "ems_readback_stale_or_future",
            )
        try:
            readback_generation = _generation(
                source.full_block_generation,
                "EMS generation",
            )
            readback_mode = _mode(source.physical_mode_code)
        except ActiveBridgeError:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                readback_timestamp,
                "ems_readback_value_unavailable",
            )
        if readback_mode is not EmsMode.GRID_CHARGE:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.CONTRADICTED,
                readback_timestamp,
                f"ems_mode={readback_mode.value}",
            )

        topology = _topology_values(source, now_utc)
        if topology is None:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                readback_timestamp,
                "physical_topology_unavailable",
            )
        machine_type, inverter_count = topology
        if (machine_type, inverter_count) == (0, 1):
            confirmation_source = "single_system_power_balance"
        elif machine_type == 1 and 2 <= inverter_count <= 10:
            confirmation_source = "parallel_master_aggregate_balance"
        elif machine_type == 2:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                readback_timestamp,
                "parallel_slave_execution_blocked",
            )
        else:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                readback_timestamp,
                "physical_topology_unsupported",
            )
        tariff_evidence.extend(
            (
                f"ems_mode={readback_mode.value}",
                f"ems_generation={readback_generation}",
                f"topology={machine_type}:{inverter_count}",
                f"confirmation_source={confirmation_source}",
                "accounting_authority=separate_direct_channel_required",
                f"power_cohort_generation={source.power_cohort_generation}",
            )
        )
    samples = {
        "grid": (source.grid_power_w, source.grid_power_observed_at),
        "battery": (source.battery_power_w, source.battery_power_observed_at),
        "pv": (source.pv_power_w, source.pv_power_observed_at),
        "load": (source.load_power_w, source.load_power_observed_at),
    }
    required = {
        # Mode 5 may discharge only into house load when requested power is
        # below consumption.  Battery flow proves execution; GCF separately
        # retains export authority, so GRID is not part of this flow cohort.
        ExecutionAction.RCE_EXPORT: ("battery",),
        ExecutionAction.RCM_PRE_DISCHARGE: ("grid", "battery"),
        ExecutionAction.TARIFF_BATTERY_CHARGE: (
            "grid",
            "battery",
            "pv",
            "load",
        ),
        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE: (
            "grid",
            "battery",
            "pv",
            "load",
        ),
        ExecutionAction.TARIFF_GRID_SUPPORT: ("grid", "battery", "pv", "load"),
        ExecutionAction.RCM_ABSORB_PV: ("pv", "load", "battery"),
        ExecutionAction.RCM_LIMIT_EXPORT: ("grid",),
    }.get(action)
    if required is None:
        return _verification(
            transaction_id,
            action,
            VerificationStatus.UNAVAILABLE,
            now_utc,
            "unsupported_physical_predicate",
        )
    observed: list[datetime] = []
    values: dict[str, float] = {}
    for name in required:
        value, observed_at = samples[name]
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                now_utc,
                f"{name}_timestamp_unavailable",
            )
        timestamp = observed_at.astimezone(timezone.utc)
        if timestamp <= sent:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.PENDING,
                timestamp,
                f"{name}_not_newer_than_command",
            )
        try:
            values[name] = _number(value, name, minimum=-10_000_000.0, maximum=10_000_000.0)
        except ActiveBridgeError:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                timestamp,
                f"{name}_value_unavailable",
            )
        flow_maximum_age = (
            RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS
            if (
                action is ExecutionAction.RCE_EXPORT
                and rce_confirmed_continuation is True
            )
            else TARIFF_PHYSICAL_MAX_AGE_SECONDS
            if tariff_mode4
            else PHYSICAL_MAX_AGE_SECONDS
        )
        if not _fresh(
            timestamp,
            now_utc,
            maximum_age=flow_maximum_age,
        ):
            age = (now_utc - timestamp).total_seconds()
            if (
                action is ExecutionAction.RCE_EXPORT
                and allow_rce_settling is True
                and age > PHYSICAL_MAX_AGE_SECONDS
            ):
                return _verification(
                    transaction_id,
                    action,
                    VerificationStatus.PENDING,
                    timestamp,
                    f"{name}_stale_while_waiting_for_discharge_effect",
                )
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                timestamp,
                f"{name}_stale_or_future",
            )
        observed.append(timestamp)
        if (
            name == "grid"
            and tariff_mode4
            and values[name] > FLOW_THRESHOLD_W
        ):
            return PhysicalVerification(
                transaction_id=transaction_id,
                action=action,
                status=VerificationStatus.CONTRADICTED,
                observed_at=timestamp,
                evidence=(f"grid_w={values[name]:.3f}",),
            )
    if (max(observed) - min(observed)).total_seconds() > PHYSICAL_COHORT_SPAN_SECONDS:
        return _verification(
            transaction_id,
            action,
            VerificationStatus.PENDING,
            max(observed),
            "physical_cohort_incomplete",
        )

    grid = values.get("grid")
    battery = values.get("battery")
    pv = values.get("pv")
    load = values.get("load")
    confirmed = False
    contradicted = False
    rce_settling = False
    if action is ExecutionAction.RCE_EXPORT:
        confirmed = battery is not None and battery > FLOW_THRESHOLD_W
        contradicted = bool(battery is not None and battery < -FLOW_THRESHOLD_W)
        rce_settling = bool(allow_rce_settling is True and contradicted)
        if rce_settling:
            # The full Mode 5 block can be ACKed before the inverter changes
            # its battery flow.  The controller enables this only for the
            # initial proof; the original fixed 180-second physical deadline
            # bounds the pending phase and EXECUTING remains strict.
            contradicted = False
    elif action is ExecutionAction.RCM_PRE_DISCHARGE:
        confirmed = grid is not None and battery is not None and grid > FLOW_THRESHOLD_W and battery > FLOW_THRESHOLD_W
        contradicted = bool(
            (grid is not None and grid < -FLOW_THRESHOLD_W)
            or (battery is not None and battery < -FLOW_THRESHOLD_W)
        )
    elif tariff_mode4:
        assert grid is not None and battery is not None
        assert pv is not None and load is not None
        balance_residual = pv + battery - load - grid
        balance_scale = max(
            abs(grid),
            abs(battery),
            abs(pv),
            abs(load),
            FLOW_THRESHOLD_W,
        )
        balance_tolerance = max(
            POWER_BALANCE_ABSOLUTE_TOLERANCE_W,
            POWER_BALANCE_RELATIVE_TOLERANCE * balance_scale,
        )
        balance_coherent = abs(balance_residual) <= balance_tolerance
        holding = abs(battery) <= FLOW_THRESHOLD_W
        target_reached = False
        if tariff_charge and holding:
            try:
                soc = _number(source.battery_soc_percent, "SOC", minimum=0.0, maximum=100.0)
                target = _number(source.force_charge_soc_percent, "4303", minimum=10.0, maximum=100.0)
            except ActiveBridgeError:
                pass
            else:
                target_reached = (
                    _fresh(source.battery_soc_observed_at, now_utc)
                    and soc >= target
                )
        pv_covers_demand = bool(
            abs(grid) <= FLOW_THRESHOLD_W
            and pv > FLOW_THRESHOLD_W
            and load >= 0.0
            # A positive house deficit needs the small-import proof below.
            # The ordinary 200 W PV tolerance cannot hide battery-fed demand.
            and (not tariff_support or pv >= load)
            and pv + FLOW_THRESHOLD_W >= load + max(-battery, 0.0)
        )
        # Direct home supply has no 200 W economic minimum. Below the normal
        # flow threshold, absolute 200/250 W tolerances could mistake battery
        # supply or an incoherent tiny meter reading for support. Require the
        # existing relative balance tolerance on both residual and battery.
        small_house_import = bool(tariff_support and -FLOW_THRESHOLD_W <= grid < 0
            and load > pv and abs(battery) <= POWER_BALANCE_RELATIVE_TOLERANCE * (load-pv)
            and abs(balance_residual) <= POWER_BALANCE_RELATIVE_TOLERANCE * (load-pv))
        confirmed = bool(
            (grid < -FLOW_THRESHOLD_W or pv_covers_demand or small_house_import)
            and (
                (tariff_support and holding)
                or (
                    tariff_charge
                    and (
                        battery < -TARIFF_CHARGE_FLOW_THRESHOLD_W
                        or target_reached
                    )
                )
            )
            and balance_coherent
        )
        contradicted = bool(
            grid > FLOW_THRESHOLD_W
            or battery > FLOW_THRESHOLD_W
            or (tariff_support and battery < -FLOW_THRESHOLD_W)
            or not balance_coherent
        )
        if (tariff_support and source.power_cohort_complete is True
            and balance_coherent and grid <= FLOW_THRESHOLD_W and not holding):
            # Classification only: the raw verdict remains contradicted. Only
            # the controller may defer stopping an already-confirmed support
            # command, within its fixed, non-renewable excursion budget.
            tariff_evidence.append("tariff_flow=battery_excursion")
        settling = bool(
            allow_tariff_settling is True
            and not confirmed
            and balance_coherent
            and grid <= FLOW_THRESHOLD_W
        )
        if settling:
            # The full Mode 4 block can be ACKed before the inverter changes
            # its power flows. The controller's original 180s physical deadline
            # bounds this pending phase; EXECUTING never enables this option.
            contradicted = False
        tariff_evidence.extend(
            (
                f"grid_w={grid:.3f}@{source.grid_power_observed_at.astimezone(timezone.utc).isoformat()}",
                f"battery_w={battery:.3f}@{source.battery_power_observed_at.astimezone(timezone.utc).isoformat()}",
                f"pv_w={pv:.3f}@{source.pv_power_observed_at.astimezone(timezone.utc).isoformat()}",
                f"load_w={load:.3f}@{source.load_power_observed_at.astimezone(timezone.utc).isoformat()}",
                f"power_balance_residual_w={balance_residual:.3f}",
                f"power_balance_tolerance_w={balance_tolerance:.3f}",
                "tariff_supply=" + (
                    "pv_or_idle_no_grid_import" if pv_covers_demand
                    else "grid_import_observed" if grid < -FLOW_THRESHOLD_W or small_house_import
                    else "unconfirmed"
                ),
                "tariff_phase=" + (
                    "waiting_for_grid_charge_effect" if settling
                    else "holding_target" if target_reached
                    else "grid_support" if tariff_support
                    else "charging"
                ),
            )
        )
    elif action is ExecutionAction.RCM_ABSORB_PV:
        confirmed = pv is not None and load is not None and battery is not None and pv - load > FLOW_THRESHOLD_W and battery < -FLOW_THRESHOLD_W
        contradicted = bool(battery is not None and battery > FLOW_THRESHOLD_W)
    else:
        if export_limit_target_percent is None:
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                max(observed),
                "export_limit_target_unavailable",
            )
        if export_limit_target_percent <= 0.05:
            confirmed = grid is not None and grid <= ZERO_EXPORT_TOLERANCE_W
            contradicted = bool(grid is not None and grid > FLOW_THRESHOLD_W)
        else:
            # A percent limit cannot be converted to watts without a fresh
            # physical system rating.  Readback alone is intentionally not
            # promoted to physical confirmation.
            return _verification(
                transaction_id,
                action,
                VerificationStatus.UNAVAILABLE,
                max(observed),
                "positive_export_limit_requires_rated_power_evidence",
            )
    status = (
        VerificationStatus.CONFIRMED
        if confirmed
        else VerificationStatus.CONTRADICTED
        if contradicted
        else VerificationStatus.PENDING
    )
    evidence = tuple(tariff_evidence)
    if not tariff_mode4:
        evidence += tuple(f"{name}_w={values[name]:.3f}" for name in required)
    if rce_settling:
        evidence += ("rce_phase=waiting_for_discharge_effect",)
    return PhysicalVerification(
        transaction_id=transaction_id,
        action=action,
        status=status,
        observed_at=max(observed),
        evidence=evidence,
    )


def correlated_observed_owner(
    raw_owner: OwnerKind,
    *,
    owner_conflict: bool,
    transaction_id: str,
    transaction_owner: ExecutionOwner,
    action: ExecutionAction,
    expected_block: EmsBlock,
    command_sent_at: datetime,
    source: ExecutionSourceSnapshot,
    now: datetime,
) -> OwnerKind:
    """Replace a false legacy foreign label only with correlated proof."""

    if owner_conflict or raw_owner not in {OwnerKind.FOREIGN, OwnerKind.UNKNOWN}:
        return raw_owner
    owner_kind = {
        ExecutionOwner.RCE: OwnerKind.RCE,
        ExecutionOwner.TARIFF: OwnerKind.TARIFF,
        ExecutionOwner.RCM: OwnerKind.RCM,
        ExecutionOwner.BALANCING: OwnerKind.BALANCING,
        ExecutionOwner.MANUAL: OwnerKind.MANUAL,
    }.get(transaction_owner)
    if owner_kind is None:
        return raw_owner
    try:
        current = settings_from_execution_source(source)
    except (ActiveBridgeError, TypeError, ValueError):
        return raw_owner
    if not _ems_blocks_match(current.ems_block, expected_block):
        return raw_owner
    proof = physical_verification(
        transaction_id,
        action,
        source,
        command_sent_at=command_sent_at,
        now=now,
        rce_confirmed_continuation=(transaction_owner is ExecutionOwner.RCE),
    )
    if (
        proof.status is VerificationStatus.CONFIRMED
        and proof.observed_at > command_sent_at
    ):
        return owner_kind
    return raw_owner


def _verification(
    transaction_id: str,
    action: ExecutionAction,
    status: VerificationStatus,
    observed_at: datetime,
    reason: str,
) -> PhysicalVerification:
    return PhysicalVerification(
        transaction_id=transaction_id,
        action=action,
        status=status,
        observed_at=observed_at,
        evidence=(reason,),
    )


__all__ = (
    "ActiveBridgeError",
    "RCE_RECALCULATION_HOLD_SECONDS",
    "RCE_POST_COMMAND_SETTLING_SECONDS",
    "RCE_POST_COMMAND_REPLAN_SECONDS",
    "TARIFF_POST_COMMAND_SETTLING_SECONDS",
    "RCE_RETARGET_COHORT_HOLD_SECONDS",
    "RceRetarget",
    "authorization_matches",
    "build_actuator_intent",
    "build_same_window_rce_retarget",
    "correlated_observed_owner",
    "execution_gates",
    "physical_verification",
    "post_command_rce_measurement_hold_authorized",
    "rce_sent_command_within_live_bms_limit",
    "rcm_charge_command_within_live_bms_limit",
    "rcm_pre_discharge_command_within_live_bms_limit",
    "tariff_command_within_live_bms_limit",
    "same_window_rce_execution_hold_authorized",
    "same_window_rce_execution_hold_seconds",
    "same_window_rce_rearm_desired",
    "same_window_rce_retarget_wait_authorized",
    "same_window_rce_target_authorized",
    "same_window_rce_wait_authorized",
    "selected_candidate",
    "settings_from_execution_source",
)
