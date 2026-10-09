"""Pure output-only canonical SOC ledger for EMS Supervisor Active.

The ledger is built *after* policy arbitration.  It binds the selected policy
and expected command/readback to one physically balanced SOC trajectory.  It
does not acquire an owner, call an actuator, or grant execution authority.

Energy sign contract:

* ``planned_battery_kwh`` is positive when stored battery energy increases;
* ``planned_grid_kwh`` is positive for import and negative for export;
* all battery-path flows and losses are non-negative magnitudes.

For every slot the canonical equation is evaluated in energy before conversion
to SOC percent::

    E_end = E_start + PV_to_battery + GRID_to_battery
            - battery_to_load - battery_to_grid - losses

Every following slot receives the preceding canonical ``soc_end_percent`` as
its only ``soc_start_percent`` source.  Caller-supplied slot starts or ends are
therefore impossible to splice into the trajectory.
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
from typing import Any


SCHEMA_VERSION = 1
MAX_SLOTS = 192
MAX_CANDIDATES = 3
MAX_LEDGER_BYTES = 524_288
ENERGY_TOLERANCE_KWH = 1e-9
SOC_TOLERANCE_PERCENT = 1e-8
CANONICAL_DECIMALS = 12

_UINT64_MAX = 18_446_744_073_709_551_615
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN_RE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_SLOT_ID_RE = re.compile(r"[a-z0-9][a-z0-9._:-]{0,63}\Z")


class PolicyId(str, Enum):
    """Policy identity copied from the completed arbitration result."""

    NONE = "none"
    RCE = "rce"
    TARIFF = "tariff"
    RCM = "rcm"


class RequestedAction(str, Enum):
    """Closed action identity preserved in the canonical timeline."""

    NONE = "none"
    RCE_EXPORT = "rce_export"
    PV_CHARGE_HOLD = "pv_charge_hold"
    TARIFF_BATTERY_CHARGE = "tariff_battery_charge"
    TARIFF_GRID_SUPPORT = "tariff_grid_support"
    TARIFF_GRID_SUPPORT_AND_CHARGE = "tariff_grid_support_and_charge"
    RCM_ABSORB_PV = "rcm_absorb_pv"
    RCM_LIMIT_EXPORT = "rcm_limit_export"
    RCM_PRE_DISCHARGE = "rcm_pre_discharge"


class OwnerKind(str, Enum):
    """Observed owner identity; publishing it never acquires ownership."""

    NONE = "none"
    SUPERVISOR = "supervisor"
    RCE = "rce"
    TARIFF = "tariff"
    RCM = "rcm"
    MANUAL = "manual"
    BALANCING = "balancing"
    FOREIGN = "foreign"
    UNKNOWN = "unknown"


class StartEligibility(str, Enum):
    """Start verdict copied from the authority layer without changing it."""

    ELIGIBLE = "eligible"
    BLOCKED = "blocked"
    UNVERIFIED = "unverified"
    NOT_APPLICABLE = "not_applicable"


class CommandTarget(str, Enum):
    """Physical command family expected for the selected slot action."""

    NONE = "none"
    EMS_BLOCK_4300_4306 = "ems_block_4300_4306"
    GCF_EXPORT_LIMIT_259 = "gcf_export_limit_259"
    BATTERY_CHARGE_LIMIT_306 = "battery_charge_limit_306"


class PhysicalExpectation(str, Enum):
    """Bounded physical predicate required after exact readback."""

    NONE = "none"
    GRID_EXPORT_AND_BATTERY_DISCHARGE = (
        "grid_export_and_battery_discharge"
    )
    GRID_IMPORT_AND_BATTERY_CHARGE = "grid_import_and_battery_charge"
    GRID_IMPORT_AND_NO_BATTERY_DISCHARGE = (
        "grid_import_and_no_battery_discharge"
    )
    PV_EXPORT_AND_IDLE_BATTERY = "pv_export_and_idle_battery"
    PV_EXPORT_AND_HOUSE_SELF_CONSUMPTION = "pv_export_and_house_self_consumption"
    PV_SURPLUS_AND_BATTERY_CHARGE = "pv_surplus_and_battery_charge"
    EXPORT_NOT_ABOVE_SNAPSHOT = "export_not_above_snapshot"


@dataclass(frozen=True, slots=True)
class ExpectationValue:
    """One scalar expected command or physical readback value."""

    name: str
    value: int | float | bool | str


@dataclass(frozen=True, slots=True)
class CommandExpectation:
    """Exact command payload expected by the separate executor."""

    target: CommandTarget
    source_generation: int | None
    values: tuple[ExpectationValue, ...]


@dataclass(frozen=True, slots=True)
class ReadbackExpectation:
    """Exact newer readback and physical predicate expected after a command."""

    target: CommandTarget
    newer_than_source_generation: bool
    values: tuple[ExpectationValue, ...]
    physical_expectation: PhysicalExpectation


@dataclass(frozen=True, slots=True)
class PolicyCandidate:
    """Bounded candidate projection needed by the visualization ledger."""

    policy_id: PolicyId
    requested_action: RequestedAction
    eligible: bool
    start_eligibility: StartEligibility
    input_revision: int
    candidate_revision: int
    rejected_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PlannedEnergy:
    """Canonical slot energy series, in kWh.

    Grid is import-positive/export-negative. Battery is positive for an
    increase in stored energy and negative for a decrease.
    """

    pv_kwh: float
    load_kwh: float
    battery_kwh: float
    grid_kwh: float


@dataclass(frozen=True, slots=True)
class BatteryEnergyFlows:
    """Non-negative battery-path magnitudes for the canonical SOC equation."""

    pv_to_battery_kwh: float
    grid_to_battery_kwh: float
    battery_to_load_kwh: float
    battery_to_grid_kwh: float
    losses_kwh: float


@dataclass(frozen=True, slots=True)
class CanonicalSlotInput:
    """Normalized post-arbitration input for one slot.

    Deliberately absent: caller-supplied SOC start/end and authority grants.
    """

    slot_id: str
    starts_at: datetime
    ends_at: datetime
    candidates: tuple[PolicyCandidate, ...]
    selected_policy: PolicyId
    selected_action: RequestedAction
    start_eligibility: StartEligibility
    owner: OwnerKind
    planned: PlannedEnergy
    flows: BatteryEnergyFlows
    protected_reserve_percent: float
    command_expectation: CommandExpectation
    readback_expectation: ReadbackExpectation


@dataclass(frozen=True, slots=True)
class CanonicalSlot:
    """One immutable canonical output slot."""

    slot_id: str
    starts_at: datetime
    ends_at: datetime
    candidates: tuple[PolicyCandidate, ...]
    selected_policy: PolicyId
    selected_action: RequestedAction
    rejected_reasons: tuple[tuple[PolicyId, tuple[str, ...]], ...]
    start_eligibility: StartEligibility
    owner: OwnerKind
    planned: PlannedEnergy
    flows: BatteryEnergyFlows
    soc_start_percent: float
    soc_end_percent: float
    protected_reserve_percent: float
    reserve_margin_end_percent: float
    reserve_respected: bool
    command_expectation: CommandExpectation
    readback_expectation: ReadbackExpectation
    continuity_residual_percent: float
    energy_balance_residual_kwh: float
    system_balance_residual_kwh: float


@dataclass(frozen=True, slots=True)
class CanonicalExecutionLedger:
    """Pure trajectory output tied to one arbitration revision."""

    schema_version: int
    built_at: datetime
    arbitration_revision: str
    usable_capacity_kwh: float
    initial_soc_percent: float
    final_soc_percent: float
    slots: tuple[CanonicalSlot, ...]
    max_continuity_residual_percent: float
    max_energy_balance_residual_kwh: float
    max_system_balance_residual_kwh: float
    reserve_violation_count: int


@dataclass(frozen=True, slots=True)
class LedgerAudit:
    """Independent recomputation of continuity and balance residuals."""

    valid: bool
    failures: tuple[str, ...]
    slot_count: int
    max_continuity_residual_percent: float
    max_energy_balance_residual_kwh: float
    max_system_balance_residual_kwh: float
    reserve_violation_count: int


_ACTION_POLICY = {
    RequestedAction.RCE_EXPORT: PolicyId.RCE,
    RequestedAction.PV_CHARGE_HOLD: PolicyId.RCE,
    RequestedAction.TARIFF_BATTERY_CHARGE: PolicyId.TARIFF,
    RequestedAction.TARIFF_GRID_SUPPORT: PolicyId.TARIFF,
    RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE: PolicyId.TARIFF,
    RequestedAction.RCM_ABSORB_PV: PolicyId.RCM,
    RequestedAction.RCM_LIMIT_EXPORT: PolicyId.RCM,
    RequestedAction.RCM_PRE_DISCHARGE: PolicyId.RCM,
}

_ACTION_TARGET = {
    RequestedAction.NONE: CommandTarget.NONE,
    RequestedAction.RCE_EXPORT: CommandTarget.EMS_BLOCK_4300_4306,
    RequestedAction.PV_CHARGE_HOLD: CommandTarget.EMS_BLOCK_4300_4306,
    RequestedAction.TARIFF_BATTERY_CHARGE: (
        CommandTarget.EMS_BLOCK_4300_4306
    ),
    RequestedAction.TARIFF_GRID_SUPPORT: CommandTarget.EMS_BLOCK_4300_4306,
    RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE: (
        CommandTarget.EMS_BLOCK_4300_4306
    ),
    RequestedAction.RCM_ABSORB_PV: CommandTarget.BATTERY_CHARGE_LIMIT_306,
    RequestedAction.RCM_LIMIT_EXPORT: CommandTarget.GCF_EXPORT_LIMIT_259,
    RequestedAction.RCM_PRE_DISCHARGE: CommandTarget.EMS_BLOCK_4300_4306,
}

_ACTION_PHYSICAL_EXPECTATION = {
    RequestedAction.NONE: PhysicalExpectation.NONE,
    RequestedAction.PV_CHARGE_HOLD: PhysicalExpectation.PV_EXPORT_AND_HOUSE_SELF_CONSUMPTION,
    RequestedAction.RCE_EXPORT: (
        PhysicalExpectation.GRID_EXPORT_AND_BATTERY_DISCHARGE
    ),
    RequestedAction.TARIFF_BATTERY_CHARGE: (
        PhysicalExpectation.GRID_IMPORT_AND_BATTERY_CHARGE
    ),
    RequestedAction.TARIFF_GRID_SUPPORT: (
        PhysicalExpectation.GRID_IMPORT_AND_NO_BATTERY_DISCHARGE
    ),
    RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE: (
        PhysicalExpectation.GRID_IMPORT_AND_BATTERY_CHARGE
    ),
    RequestedAction.RCM_ABSORB_PV: (
        PhysicalExpectation.PV_SURPLUS_AND_BATTERY_CHARGE
    ),
    RequestedAction.RCM_LIMIT_EXPORT: (
        PhysicalExpectation.EXPORT_NOT_ABOVE_SNAPSHOT
    ),
    RequestedAction.RCM_PRE_DISCHARGE: (
        PhysicalExpectation.GRID_EXPORT_AND_BATTERY_DISCHARGE
    ),
}

_COMMAND_FIELDS = {
    CommandTarget.NONE: (),
    CommandTarget.EMS_BLOCK_4300_4306: (
        "backup_soc",
        "force_charge_soc",
        "force_discharge_soc",
        "maximum_charge_power",
        "maximum_discharge_power",
        "mode_code",
        "self_use_soc",
    ),
    CommandTarget.GCF_EXPORT_LIMIT_259: ("export_limit_percent",),
    CommandTarget.BATTERY_CHARGE_LIMIT_306: (
        "battery_charge_limit_percent",
    ),
}


def _is_aware_datetime(value: Any) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


def _timestamp(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def _number(value: Any, *, field: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{field} must be a finite number")
    normalized = round(float(value), CANONICAL_DECIMALS)
    return 0.0 if normalized == 0.0 else normalized


def _revision(value: Any, *, field: str) -> int:
    if type(value) is not int or not 0 <= value <= _UINT64_MAX:
        raise ValueError(f"{field} must be a uint64 revision")
    return value


def _token(value: Any, *, field: str) -> str:
    if type(value) is not str or _TOKEN_RE.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded canonical token")
    return value


def _normalized_reasons(values: Any) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise ValueError("rejected_reasons must be an immutable tuple")
    normalized = tuple(
        sorted(_token(value, field="rejected reason") for value in values)
    )
    if len(normalized) != len(set(normalized)):
        raise ValueError("rejected reasons must be unique")
    return normalized


def _normalize_expectation_values(
    values: Any,
    *,
    target: CommandTarget,
) -> tuple[ExpectationValue, ...]:
    if type(values) is not tuple:
        raise ValueError("expectation values must be an immutable tuple")
    normalized: list[ExpectationValue] = []
    for item in values:
        if type(item) is not ExpectationValue:
            raise ValueError("expectation contains an invalid value record")
        name = _token(item.name, field="expectation value name")
        value = item.value
        if type(value) in (int, float):
            value = _number(value, field=f"expectation {name}")
            if type(item.value) is int:
                value = int(value)
        elif type(value) not in (bool, str):
            raise ValueError(f"expectation {name} has a non-scalar value")
        elif type(value) is str:
            _token(value, field=f"expectation {name}")
        normalized.append(ExpectationValue(name=name, value=value))
    normalized.sort(key=lambda item: item.name)
    names = tuple(item.name for item in normalized)
    if len(names) != len(set(names)):
        raise ValueError("expectation value names must be unique")
    if names != _COMMAND_FIELDS[target]:
        raise ValueError(f"incomplete expectation for {target.value}")
    payload = {item.name: item.value for item in normalized}
    if target is CommandTarget.EMS_BLOCK_4300_4306:
        if type(payload["mode_code"]) is not int or payload["mode_code"] not in {
            0,
            3,
            4,
            5,
        }:
            raise ValueError("mode_code is outside the verified EMS modes")
        ranges = {
            "self_use_soc": (10.0, 100.0),
            "backup_soc": (60.0, 100.0),
            "force_charge_soc": (10.0, 100.0),
            "maximum_charge_power": (0.0, 100.0),
            "force_discharge_soc": (0.0, 100.0),
            "maximum_discharge_power": (0.0, 100.0),
        }
        for name, (minimum, maximum) in ranges.items():
            value = payload[name]
            if type(value) not in (int, float) or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside the verified range")
    elif target is not CommandTarget.NONE:
        only_value = next(iter(payload.values()))
        if type(only_value) not in (int, float) or not 0 <= only_value <= 100:
            raise ValueError("direct-register expectation is outside 0..100")
    return tuple(normalized)


def _normalize_expectations(
    action: RequestedAction,
    command: Any,
    readback: Any,
) -> tuple[CommandExpectation, ReadbackExpectation]:
    if type(command) is not CommandExpectation:
        raise ValueError("missing command expectation")
    if type(readback) is not ReadbackExpectation:
        raise ValueError("missing readback expectation")
    if type(command.target) is not CommandTarget:
        raise ValueError("invalid command target")
    if type(readback.target) is not CommandTarget:
        raise ValueError("invalid readback target")
    if type(readback.physical_expectation) is not PhysicalExpectation:
        raise ValueError("invalid physical readback expectation")
    if type(readback.newer_than_source_generation) is not bool:
        raise ValueError("newer-generation expectation must be boolean")
    expected_target = _ACTION_TARGET[action]
    if command.target is not expected_target or readback.target is not expected_target:
        raise ValueError("action and command/readback target differ")
    command_values = _normalize_expectation_values(
        command.values,
        target=command.target,
    )
    readback_values = _normalize_expectation_values(
        readback.values,
        target=readback.target,
    )
    if command_values != readback_values:
        raise ValueError("command and exact readback values differ")
    if readback.physical_expectation is not _ACTION_PHYSICAL_EXPECTATION[action]:
        raise ValueError("action and physical readback predicate differ")
    if action is RequestedAction.NONE:
        if command.source_generation is not None:
            raise ValueError("no-action expectation cannot carry a generation")
        if readback.newer_than_source_generation:
            raise ValueError("no-action readback cannot require a generation")
    else:
        _revision(command.source_generation, field="command source_generation")
        if not readback.newer_than_source_generation:
            raise ValueError("a command requires a newer physical generation")
    return (
        CommandExpectation(
            target=command.target,
            source_generation=command.source_generation,
            values=command_values,
        ),
        ReadbackExpectation(
            target=readback.target,
            newer_than_source_generation=readback.newer_than_source_generation,
            values=readback_values,
            physical_expectation=readback.physical_expectation,
        ),
    )


def _normalize_candidate(candidate: Any) -> PolicyCandidate:
    if type(candidate) is not PolicyCandidate:
        raise ValueError("candidate projection is missing or invalid")
    if type(candidate.policy_id) is not PolicyId or candidate.policy_id is PolicyId.NONE:
        raise ValueError("candidate must identify one policy")
    if type(candidate.requested_action) is not RequestedAction:
        raise ValueError("candidate action is invalid")
    if type(candidate.eligible) is not bool:
        raise ValueError("candidate eligibility must be boolean")
    if type(candidate.start_eligibility) is not StartEligibility:
        raise ValueError("candidate start eligibility is invalid")
    if candidate.start_eligibility is StartEligibility.NOT_APPLICABLE:
        raise ValueError("a policy candidate requires a start verdict")
    if candidate.requested_action is not RequestedAction.NONE:
        if _ACTION_POLICY[candidate.requested_action] is not candidate.policy_id:
            raise ValueError("candidate action belongs to another policy")
    return PolicyCandidate(
        policy_id=candidate.policy_id,
        requested_action=candidate.requested_action,
        eligible=candidate.eligible,
        start_eligibility=candidate.start_eligibility,
        input_revision=_revision(
            candidate.input_revision,
            field="candidate input_revision",
        ),
        candidate_revision=_revision(
            candidate.candidate_revision,
            field="candidate candidate_revision",
        ),
        rejected_reasons=_normalized_reasons(candidate.rejected_reasons),
    )


def _normalize_candidates(
    candidates: Any,
    *,
    selected_policy: PolicyId,
    selected_action: RequestedAction,
    start_eligibility: StartEligibility,
) -> tuple[PolicyCandidate, ...]:
    if type(candidates) is not tuple or len(candidates) > MAX_CANDIDATES:
        raise ValueError("candidates must be a bounded immutable tuple")
    normalized = tuple(
        sorted(
            (_normalize_candidate(candidate) for candidate in candidates),
            key=lambda candidate: candidate.policy_id.value,
        )
    )
    policies = tuple(candidate.policy_id for candidate in normalized)
    if len(policies) != len(set(policies)):
        raise ValueError("candidate policies must be unique")
    if selected_policy is PolicyId.NONE:
        if selected_action is not RequestedAction.NONE:
            raise ValueError("no selected policy cannot select an action")
        if start_eligibility is not StartEligibility.NOT_APPLICABLE:
            raise ValueError("no selected policy has no start verdict")
        for candidate in normalized:
            if not candidate.rejected_reasons:
                raise ValueError("every unselected candidate needs a reason")
        return normalized
    if selected_action is RequestedAction.NONE:
        raise ValueError("a selected policy requires an exact action")
    if _ACTION_POLICY[selected_action] is not selected_policy:
        raise ValueError("selected action belongs to another policy")
    selected = next(
        (candidate for candidate in normalized if candidate.policy_id is selected_policy),
        None,
    )
    if selected is None:
        raise ValueError("selected policy is absent from candidates")
    if selected.requested_action is not selected_action:
        raise ValueError("selected action differs from selected candidate")
    if not selected.eligible or selected.rejected_reasons:
        raise ValueError("selected candidate must be eligible and unrejected")
    if selected.start_eligibility is not start_eligibility:
        raise ValueError("slot and selected-candidate start verdict differ")
    for candidate in normalized:
        if candidate.policy_id is not selected_policy and not candidate.rejected_reasons:
            raise ValueError("every unselected candidate needs a reason")
    return normalized


def _normalize_plan(plan: Any) -> PlannedEnergy:
    if type(plan) is not PlannedEnergy:
        raise ValueError("planned PV/LOAD/BAT/GRID is missing")
    normalized = PlannedEnergy(
        pv_kwh=_number(plan.pv_kwh, field="planned PV"),
        load_kwh=_number(plan.load_kwh, field="planned LOAD"),
        battery_kwh=_number(plan.battery_kwh, field="planned BAT"),
        grid_kwh=_number(plan.grid_kwh, field="planned GRID"),
    )
    if normalized.pv_kwh < 0 or normalized.load_kwh < 0:
        raise ValueError("planned PV and LOAD must be non-negative")
    return normalized


def _normalize_flows(flows: Any) -> BatteryEnergyFlows:
    if type(flows) is not BatteryEnergyFlows:
        raise ValueError("battery energy flows are missing")
    normalized = BatteryEnergyFlows(
        pv_to_battery_kwh=_number(
            flows.pv_to_battery_kwh,
            field="PV_to_battery",
        ),
        grid_to_battery_kwh=_number(
            flows.grid_to_battery_kwh,
            field="GRID_to_battery",
        ),
        battery_to_load_kwh=_number(
            flows.battery_to_load_kwh,
            field="battery_to_load",
        ),
        battery_to_grid_kwh=_number(
            flows.battery_to_grid_kwh,
            field="battery_to_grid",
        ),
        losses_kwh=_number(flows.losses_kwh, field="losses"),
    )
    if any(value < 0 for value in (
        normalized.pv_to_battery_kwh,
        normalized.grid_to_battery_kwh,
        normalized.battery_to_load_kwh,
        normalized.battery_to_grid_kwh,
        normalized.losses_kwh,
    )):
        raise ValueError("battery flows and losses must be non-negative")
    return normalized


def _net_battery_energy(flows: BatteryEnergyFlows) -> float:
    return (
        flows.pv_to_battery_kwh
        + flows.grid_to_battery_kwh
        - flows.battery_to_load_kwh
        - flows.battery_to_grid_kwh
        - flows.losses_kwh
    )


def _system_balance(plan: PlannedEnergy, flows: BatteryEnergyFlows) -> float:
    return (
        plan.pv_kwh
        + plan.grid_kwh
        - plan.load_kwh
        - plan.battery_kwh
        - flows.losses_kwh
    )


def _validate_energy_contract(
    plan: PlannedEnergy,
    flows: BatteryEnergyFlows,
) -> None:
    battery_residual = plan.battery_kwh - _net_battery_energy(flows)
    system_residual = _system_balance(plan, flows)
    if abs(battery_residual) > ENERGY_TOLERANCE_KWH:
        raise ValueError("planned BAT differs from the canonical battery paths")
    if abs(system_residual) > ENERGY_TOLERANCE_KWH:
        raise ValueError("planned PV/LOAD/BAT/GRID violates energy balance")
    if flows.pv_to_battery_kwh - plan.pv_kwh > ENERGY_TOLERANCE_KWH:
        raise ValueError("PV_to_battery exceeds planned PV")
    if flows.grid_to_battery_kwh - max(plan.grid_kwh, 0.0) > ENERGY_TOLERANCE_KWH:
        raise ValueError("GRID_to_battery exceeds planned import")
    if flows.battery_to_load_kwh - plan.load_kwh > ENERGY_TOLERANCE_KWH:
        raise ValueError("battery_to_load exceeds planned LOAD")
    if flows.battery_to_grid_kwh - max(-plan.grid_kwh, 0.0) > ENERGY_TOLERANCE_KWH:
        raise ValueError("battery_to_grid exceeds planned export")


def _normalized_slot_input(slot: Any) -> CanonicalSlotInput:
    if type(slot) is not CanonicalSlotInput:
        raise ValueError("slot input is missing or invalid")
    if type(slot.slot_id) is not str or _SLOT_ID_RE.fullmatch(slot.slot_id) is None:
        raise ValueError("slot_id is not canonical")
    if not _is_aware_datetime(slot.starts_at) or not _is_aware_datetime(slot.ends_at):
        raise ValueError("slot timestamps must be timezone-aware")
    starts_at = _utc(slot.starts_at)
    ends_at = _utc(slot.ends_at)
    if ends_at <= starts_at:
        raise ValueError("slot must have a positive duration")
    if type(slot.selected_policy) is not PolicyId:
        raise ValueError("selected policy is invalid")
    if type(slot.selected_action) is not RequestedAction:
        raise ValueError("selected action is invalid")
    if type(slot.start_eligibility) is not StartEligibility:
        raise ValueError("start eligibility is invalid")
    if type(slot.owner) is not OwnerKind:
        raise ValueError("owner is invalid")
    candidates = _normalize_candidates(
        slot.candidates,
        selected_policy=slot.selected_policy,
        selected_action=slot.selected_action,
        start_eligibility=slot.start_eligibility,
    )
    plan = _normalize_plan(slot.planned)
    flows = _normalize_flows(slot.flows)
    _validate_energy_contract(plan, flows)
    reserve = _number(
        slot.protected_reserve_percent,
        field="protected reserve",
    )
    if not 0 <= reserve <= 100:
        raise ValueError("protected reserve must be within 0..100")
    command, readback = _normalize_expectations(
        slot.selected_action,
        slot.command_expectation,
        slot.readback_expectation,
    )
    return CanonicalSlotInput(
        slot_id=slot.slot_id,
        starts_at=starts_at,
        ends_at=ends_at,
        candidates=candidates,
        selected_policy=slot.selected_policy,
        selected_action=slot.selected_action,
        start_eligibility=slot.start_eligibility,
        owner=slot.owner,
        planned=plan,
        flows=flows,
        protected_reserve_percent=reserve,
        command_expectation=command,
        readback_expectation=readback,
    )


def build_canonical_execution_ledger(
    *,
    built_at: datetime,
    arbitration_revision: str,
    usable_capacity_kwh: float,
    initial_soc_percent: float,
    slots: Sequence[CanonicalSlotInput],
) -> CanonicalExecutionLedger:
    """Build one output-only ledger and derive every slot start from its predecessor."""

    if not _is_aware_datetime(built_at):
        raise ValueError("built_at must be timezone-aware")
    if type(arbitration_revision) is not str or _SHA256_RE.fullmatch(
        arbitration_revision
    ) is None:
        raise ValueError("arbitration_revision must be a SHA-256 digest")
    capacity = _number(usable_capacity_kwh, field="usable capacity")
    initial_soc = _number(initial_soc_percent, field="initial SOC")
    if capacity <= 0:
        raise ValueError("usable capacity must be positive")
    if not 0 <= initial_soc <= 100:
        raise ValueError("initial SOC must be within 0..100")
    if isinstance(slots, (str, bytes)):
        raise ValueError("slots must be a bounded sequence")
    try:
        slot_inputs = tuple(slots)
    except TypeError as err:
        raise ValueError("slots must be a bounded sequence") from err
    if not 1 <= len(slot_inputs) <= MAX_SLOTS:
        raise ValueError("slot count is outside the bounded contract")

    output: list[CanonicalSlot] = []
    previous_end_at: datetime | None = None
    previous_soc_end = initial_soc
    seen_slot_ids: set[str] = set()
    for raw_slot in slot_inputs:
        slot = _normalized_slot_input(raw_slot)
        if slot.slot_id in seen_slot_ids:
            raise ValueError("slot IDs must be unique")
        seen_slot_ids.add(slot.slot_id)
        if previous_end_at is not None and slot.starts_at != previous_end_at:
            raise ValueError("slot time intervals must be contiguous")

        soc_start = previous_soc_end
        net_energy = _net_battery_energy(slot.flows)
        unbounded_end = soc_start + (100.0 * net_energy / capacity)
        if unbounded_end < -SOC_TOLERANCE_PERCENT or unbounded_end > 100 + SOC_TOLERANCE_PERCENT:
            raise ValueError("canonical SOC would leave the physical 0..100 range")
        soc_end = _number(
            min(100.0, max(0.0, unbounded_end)),
            field="canonical SOC end",
        )
        actual_delta = capacity * (soc_end - soc_start) / 100.0
        energy_residual = _number(
            actual_delta - net_energy,
            field="energy balance residual",
        )
        system_residual = _number(
            _system_balance(slot.planned, slot.flows),
            field="system balance residual",
        )
        rejected = tuple(
            (
                candidate.policy_id,
                candidate.rejected_reasons,
            )
            for candidate in slot.candidates
            if candidate.rejected_reasons
        )
        margin = _number(
            soc_end - slot.protected_reserve_percent,
            field="reserve margin",
        )
        output.append(
            CanonicalSlot(
                slot_id=slot.slot_id,
                starts_at=slot.starts_at,
                ends_at=slot.ends_at,
                candidates=slot.candidates,
                selected_policy=slot.selected_policy,
                selected_action=slot.selected_action,
                rejected_reasons=rejected,
                start_eligibility=slot.start_eligibility,
                owner=slot.owner,
                planned=slot.planned,
                flows=slot.flows,
                soc_start_percent=soc_start,
                soc_end_percent=soc_end,
                protected_reserve_percent=slot.protected_reserve_percent,
                reserve_margin_end_percent=margin,
                reserve_respected=margin >= -SOC_TOLERANCE_PERCENT,
                command_expectation=slot.command_expectation,
                readback_expectation=slot.readback_expectation,
                continuity_residual_percent=0.0,
                energy_balance_residual_kwh=energy_residual,
                system_balance_residual_kwh=system_residual,
            )
        )
        previous_end_at = slot.ends_at
        previous_soc_end = soc_end

    max_energy = max(abs(slot.energy_balance_residual_kwh) for slot in output)
    max_system = max(abs(slot.system_balance_residual_kwh) for slot in output)
    ledger = CanonicalExecutionLedger(
        schema_version=SCHEMA_VERSION,
        built_at=_utc(built_at),
        arbitration_revision=arbitration_revision,
        usable_capacity_kwh=capacity,
        initial_soc_percent=initial_soc,
        final_soc_percent=previous_soc_end,
        slots=tuple(output),
        max_continuity_residual_percent=0.0,
        max_energy_balance_residual_kwh=_number(
            max_energy,
            field="maximum energy residual",
        ),
        max_system_balance_residual_kwh=_number(
            max_system,
            field="maximum system residual",
        ),
        reserve_violation_count=sum(not slot.reserve_respected for slot in output),
    )
    audit = audit_canonical_execution_ledger(ledger)
    if not audit.valid:
        raise ValueError("built canonical ledger failed its own audit")
    return ledger


def audit_canonical_execution_ledger(ledger: Any) -> LedgerAudit:
    """Recompute the complete SOC chain without trusting stored residual fields."""

    failures: list[str] = []
    if type(ledger) is not CanonicalExecutionLedger:
        return LedgerAudit(
            valid=False,
            failures=("invalid_ledger_type",),
            slot_count=0,
            max_continuity_residual_percent=math.inf,
            max_energy_balance_residual_kwh=math.inf,
            max_system_balance_residual_kwh=math.inf,
            reserve_violation_count=0,
        )
    try:
        capacity = _number(ledger.usable_capacity_kwh, field="usable capacity")
        initial_soc = _number(ledger.initial_soc_percent, field="initial SOC")
        if ledger.schema_version != SCHEMA_VERSION:
            failures.append("schema_version")
        if not _is_aware_datetime(ledger.built_at):
            failures.append("built_at")
        if type(ledger.arbitration_revision) is not str or _SHA256_RE.fullmatch(
            ledger.arbitration_revision
        ) is None:
            failures.append("arbitration_revision")
        if capacity <= 0 or not 0 <= initial_soc <= 100:
            failures.append("initial_state")
        if type(ledger.slots) is not tuple or not 1 <= len(ledger.slots) <= MAX_SLOTS:
            failures.append("slot_count")
            slots: tuple[Any, ...] = ()
        else:
            slots = ledger.slots
    except (TypeError, ValueError):
        return LedgerAudit(
            valid=False,
            failures=("invalid_header",),
            slot_count=0,
            max_continuity_residual_percent=math.inf,
            max_energy_balance_residual_kwh=math.inf,
            max_system_balance_residual_kwh=math.inf,
            reserve_violation_count=0,
        )

    max_continuity = 0.0
    max_energy = 0.0
    max_system = 0.0
    reserve_violations = 0
    previous_soc = initial_soc
    previous_end_at: datetime | None = None
    seen_ids: set[str] = set()
    for index, slot in enumerate(slots):
        prefix = f"slot_{index}"
        if type(slot) is not CanonicalSlot:
            failures.append(f"{prefix}_type")
            continue
        try:
            normalized = _normalized_slot_input(
                CanonicalSlotInput(
                    slot_id=slot.slot_id,
                    starts_at=slot.starts_at,
                    ends_at=slot.ends_at,
                    candidates=slot.candidates,
                    selected_policy=slot.selected_policy,
                    selected_action=slot.selected_action,
                    start_eligibility=slot.start_eligibility,
                    owner=slot.owner,
                    planned=slot.planned,
                    flows=slot.flows,
                    protected_reserve_percent=slot.protected_reserve_percent,
                    command_expectation=slot.command_expectation,
                    readback_expectation=slot.readback_expectation,
                )
            )
            if (
                slot.candidates != normalized.candidates
                or slot.planned != normalized.planned
                or slot.flows != normalized.flows
                or slot.protected_reserve_percent
                != normalized.protected_reserve_percent
                or slot.command_expectation
                != normalized.command_expectation
                or slot.readback_expectation
                != normalized.readback_expectation
            ):
                failures.append(f"{prefix}_noncanonical_projection")
            if slot.slot_id in seen_ids:
                failures.append(f"{prefix}_duplicate_id")
            seen_ids.add(slot.slot_id)
            if previous_end_at is not None and normalized.starts_at != previous_end_at:
                failures.append(f"{prefix}_time_continuity")
            start = _number(slot.soc_start_percent, field="SOC start")
            end = _number(slot.soc_end_percent, field="SOC end")
            continuity = abs(start - previous_soc)
            max_continuity = max(max_continuity, continuity)
            net_energy = _net_battery_energy(normalized.flows)
            actual_delta = capacity * (end - start) / 100.0
            energy_residual = abs(actual_delta - net_energy)
            system_residual = abs(_system_balance(normalized.planned, normalized.flows))
            max_energy = max(max_energy, energy_residual)
            max_system = max(max_system, system_residual)
            stored_continuity = _number(
                slot.continuity_residual_percent,
                field="stored continuity residual",
            )
            stored_energy = _number(
                slot.energy_balance_residual_kwh,
                field="stored energy residual",
            )
            stored_system = _number(
                slot.system_balance_residual_kwh,
                field="stored system residual",
            )
            reserve = _number(
                slot.protected_reserve_percent,
                field="reserve",
            )
            margin = end - reserve
            respected = margin >= -SOC_TOLERANCE_PERCENT
            reserve_violations += not respected
            expected_rejections = tuple(
                (candidate.policy_id, candidate.rejected_reasons)
                for candidate in normalized.candidates
                if candidate.rejected_reasons
            )
            if continuity > SOC_TOLERANCE_PERCENT:
                failures.append(f"{prefix}_soc_continuity")
            if energy_residual > ENERGY_TOLERANCE_KWH:
                failures.append(f"{prefix}_soc_equation")
            if system_residual > ENERGY_TOLERANCE_KWH:
                failures.append(f"{prefix}_system_balance")
            if abs(stored_continuity - continuity) > SOC_TOLERANCE_PERCENT:
                failures.append(f"{prefix}_stored_continuity")
            if abs(stored_energy - (actual_delta - net_energy)) > ENERGY_TOLERANCE_KWH:
                failures.append(f"{prefix}_stored_energy_residual")
            if abs(stored_system - _system_balance(normalized.planned, normalized.flows)) > ENERGY_TOLERANCE_KWH:
                failures.append(f"{prefix}_stored_system_residual")
            if abs(slot.reserve_margin_end_percent - margin) > SOC_TOLERANCE_PERCENT:
                failures.append(f"{prefix}_reserve_margin")
            if type(slot.reserve_respected) is not bool or slot.reserve_respected != respected:
                failures.append(f"{prefix}_reserve_verdict")
            if type(slot.rejected_reasons) is not tuple or slot.rejected_reasons != expected_rejections:
                failures.append(f"{prefix}_rejections")
            previous_soc = end
            previous_end_at = normalized.ends_at
        except (TypeError, ValueError):
            failures.append(f"{prefix}_invalid")

    try:
        if abs(_number(ledger.final_soc_percent, field="final SOC") - previous_soc) > SOC_TOLERANCE_PERCENT:
            failures.append("final_soc")
        if abs(_number(ledger.max_continuity_residual_percent, field="max continuity") - max_continuity) > SOC_TOLERANCE_PERCENT:
            failures.append("max_continuity_residual")
        if abs(_number(ledger.max_energy_balance_residual_kwh, field="max energy") - max_energy) > ENERGY_TOLERANCE_KWH:
            failures.append("max_energy_residual")
        if abs(_number(ledger.max_system_balance_residual_kwh, field="max system") - max_system) > ENERGY_TOLERANCE_KWH:
            failures.append("max_system_residual")
        if type(ledger.reserve_violation_count) is not int or ledger.reserve_violation_count != reserve_violations:
            failures.append("reserve_violation_count")
    except (TypeError, ValueError):
        failures.append("invalid_summary")
    return LedgerAudit(
        valid=not failures,
        failures=tuple(failures),
        slot_count=len(slots),
        max_continuity_residual_percent=_number(max_continuity, field="audit continuity"),
        max_energy_balance_residual_kwh=_number(max_energy, field="audit energy"),
        max_system_balance_residual_kwh=_number(max_system, field="audit system"),
        reserve_violation_count=reserve_violations,
    )


def _expectation_values_payload(
    values: tuple[ExpectationValue, ...],
) -> dict[str, int | float | bool | str]:
    return {item.name: item.value for item in values}


def _candidate_payload(candidate: PolicyCandidate) -> dict[str, Any]:
    return {
        "policy_id": candidate.policy_id,
        "requested_action": candidate.requested_action,
        "eligible": candidate.eligible,
        "start_eligibility": candidate.start_eligibility,
        "input_revision": candidate.input_revision,
        "candidate_revision": candidate.candidate_revision,
        "rejected_reasons": candidate.rejected_reasons,
    }


def _slot_payload(slot: CanonicalSlot) -> dict[str, Any]:
    return {
        "slot_id": slot.slot_id,
        "starts_at": _timestamp(slot.starts_at),
        "ends_at": _timestamp(slot.ends_at),
        "policy_candidates": [
            _candidate_payload(candidate) for candidate in slot.candidates
        ],
        "selected_policy": slot.selected_policy,
        "selected_action": slot.selected_action,
        "rejected_reasons": [
            {
                "policy_id": policy_id,
                "reasons": reasons,
            }
            for policy_id, reasons in slot.rejected_reasons
        ],
        "start_eligibility": slot.start_eligibility,
        "owner": slot.owner,
        "planned": {
            "pv_kwh": slot.planned.pv_kwh,
            "load_kwh": slot.planned.load_kwh,
            "battery_kwh": slot.planned.battery_kwh,
            "grid_kwh_import_positive": slot.planned.grid_kwh,
        },
        "soc_equation": {
            "pv_to_battery_kwh": slot.flows.pv_to_battery_kwh,
            "grid_to_battery_kwh": slot.flows.grid_to_battery_kwh,
            "battery_to_load_kwh": slot.flows.battery_to_load_kwh,
            "battery_to_grid_kwh": slot.flows.battery_to_grid_kwh,
            "losses_kwh": slot.flows.losses_kwh,
            "soc_start_percent": slot.soc_start_percent,
            "soc_end_percent": slot.soc_end_percent,
            "energy_balance_residual_kwh": slot.energy_balance_residual_kwh,
            "continuity_residual_percent": slot.continuity_residual_percent,
            "system_balance_residual_kwh": slot.system_balance_residual_kwh,
        },
        "protected_reserve": {
            "percent": slot.protected_reserve_percent,
            "margin_end_percent": slot.reserve_margin_end_percent,
            "respected": slot.reserve_respected,
        },
        "command_expectation": {
            "target": slot.command_expectation.target,
            "source_generation": slot.command_expectation.source_generation,
            "values": _expectation_values_payload(
                slot.command_expectation.values
            ),
        },
        "readback_expectation": {
            "target": slot.readback_expectation.target,
            "newer_than_source_generation": (
                slot.readback_expectation.newer_than_source_generation
            ),
            "values": _expectation_values_payload(
                slot.readback_expectation.values
            ),
            "physical_expectation": (
                slot.readback_expectation.physical_expectation
            ),
        },
    }


def _canonical_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("non-finite canonical ledger value")
        normalized = round(value, CANONICAL_DECIMALS)
        return 0.0 if normalized == 0.0 else normalized
    if value is None or type(value) in (bool, int, str):
        return value
    raise ValueError("unsupported canonical ledger value")


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _ledger_payload_without_revision(
    ledger: CanonicalExecutionLedger,
) -> dict[str, Any]:
    return {
        "schema_version": ledger.schema_version,
        "output_only": True,
        "built_at": _timestamp(ledger.built_at),
        "arbitration_revision": ledger.arbitration_revision,
        "usable_capacity_kwh": ledger.usable_capacity_kwh,
        "initial_soc_percent": ledger.initial_soc_percent,
        "final_soc_percent": ledger.final_soc_percent,
        "slots": [_slot_payload(slot) for slot in ledger.slots],
        "audit": {
            "max_continuity_residual_percent": (
                ledger.max_continuity_residual_percent
            ),
            "max_energy_balance_residual_kwh": (
                ledger.max_energy_balance_residual_kwh
            ),
            "max_system_balance_residual_kwh": (
                ledger.max_system_balance_residual_kwh
            ),
            "reserve_violation_count": ledger.reserve_violation_count,
        },
    }


def canonical_execution_ledger_revision(
    ledger: CanonicalExecutionLedger,
) -> str:
    """Return the deterministic content digest without trusting stored state."""

    audit = audit_canonical_execution_ledger(ledger)
    if not audit.valid:
        raise ValueError("canonical ledger is invalid: " + ",".join(audit.failures))
    return hashlib.sha256(
        _canonical_json(_ledger_payload_without_revision(ledger)).encode("utf-8")
    ).hexdigest()


def canonical_execution_ledger_to_dict(
    ledger: CanonicalExecutionLedger,
) -> dict[str, Any]:
    """Return the complete deterministic visualization projection."""

    payload = _ledger_payload_without_revision(ledger)
    payload["ledger_revision"] = canonical_execution_ledger_revision(ledger)
    canonical = _canonical_value(payload)
    if len(_canonical_json(canonical).encode("utf-8")) > MAX_LEDGER_BYTES:
        raise ValueError("canonical execution ledger exceeds its bounded contract")
    return canonical


def serialize_canonical_execution_ledger(
    ledger: CanonicalExecutionLedger,
) -> str:
    """Serialize one validated ledger as stable canonical JSON."""

    return _canonical_json(canonical_execution_ledger_to_dict(ledger))


__all__ = (
    "BatteryEnergyFlows",
    "CANONICAL_DECIMALS",
    "CanonicalExecutionLedger",
    "CanonicalSlot",
    "CanonicalSlotInput",
    "CommandExpectation",
    "CommandTarget",
    "ENERGY_TOLERANCE_KWH",
    "ExpectationValue",
    "LedgerAudit",
    "MAX_CANDIDATES",
    "MAX_LEDGER_BYTES",
    "MAX_SLOTS",
    "OwnerKind",
    "PhysicalExpectation",
    "PlannedEnergy",
    "PolicyCandidate",
    "PolicyId",
    "ReadbackExpectation",
    "RequestedAction",
    "SCHEMA_VERSION",
    "SOC_TOLERANCE_PERCENT",
    "StartEligibility",
    "audit_canonical_execution_ledger",
    "build_canonical_execution_ledger",
    "canonical_execution_ledger_revision",
    "canonical_execution_ledger_to_dict",
    "serialize_canonical_execution_ledger",
)
