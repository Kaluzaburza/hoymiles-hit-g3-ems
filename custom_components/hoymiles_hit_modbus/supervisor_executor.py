"""Pure execution state machine for EMS Supervisor Active.

This module deliberately contains no Home Assistant or transport imports.  It
models execution authority, immutable command/snapshot evidence, physical
acknowledgement gates, rollback, restart recovery, and MASTER STOP.  A future
runtime adapter is responsible for persistence and for actually dispatching
the returned command envelopes.

The state machine never treats a command echo as acknowledgement.  An action
becomes active only after a complete newer readback and an independent
physical-flow confirmation for the same transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import Enum, IntEnum
from math import floor, isfinite
import re
from typing import Final, Iterable, Mapping


MAX_READBACK_AGE_SECONDS: Final = 15.0
# Four tariff power channels arrive in separate 8-20s cohorts on the field node.
# This limit affects physical flow evidence only; full EMS FC03 stays at 15s.
TARIFF_PHYSICAL_MAX_AGE_SECONDS: Final = 30.0
# A confirmed Mode 5 transaction may bridge one ordinary Wi-Fi telemetry drop.
# This is physical-flow evidence only; new writes and EMS FC03 stay at 15 s.
RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS: Final = 25.0
RCE_EXECUTING_EMS_MAX_AGE_SECONDS: Final = 30.0
SETTINGS_READBACK_MAX_AGE_SECONDS: Final = 30.0
MAX_FUTURE_SKEW_SECONDS: Final = 5.0
MAX_GENERATION: Final = 16_000_000
COMMAND_DISPATCH_TIMEOUT_SECONDS: Final = 30.0
COMMAND_ACK_TIMEOUT_SECONDS: Final = 90.0
PV_HOLD_PHYSICAL_TIMEOUT_SECONDS: Final = 180.0
CONFIRMED_TELEMETRY_GRACE_SECONDS: Final = 120.0


def command_wait_timeout(transaction) -> float:
    """An ACKed forced command has 180 s to settle; its timestamp never slides."""
    if (transaction.intent.command.ems_block is not None
        and transaction.intent.command.ems_block.mode in {EmsMode.GRID_CHARGE, EmsMode.GRID_DISCHARGE}
        and transaction.readback_result is VerificationStatus.CONFIRMED):
        return PV_HOLD_PHYSICAL_TIMEOUT_SECONDS
    return COMMAND_ACK_TIMEOUT_SECONDS

RESTORE_DISPATCH_TIMEOUT_SECONDS: Final = 30.0
RESTORE_ACK_TIMEOUT_SECONDS: Final = 90.0
DEFAULT_MASTER_STOP_DEADLINE_SECONDS: Final = 180.0

_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,95}$")
_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


class ActiveState(str, Enum):
    """Closed public Active execution states."""

    IDLE = "active_idle"
    SELECTED = "active_selected"
    STARTING = "active_starting"
    WAITING_READBACK = "active_waiting_readback"
    EXECUTING = "active_executing"
    RETARGETING = "active_retargeting"
    STOPPING = "active_stopping"
    RESTORING = "active_restoring"
    BLOCKED = "active_blocked"
    FAULT = "active_fault"


class ExecutionOwner(str, Enum):
    """Owners that may be acquired by the Active executor."""

    NONE = "none"
    TARIFF = "tariff"
    RCE = "rce"
    RCM = "rcm"
    BALANCING = "balancing"
    MANUAL = "manual"


class ExecutionAction(str, Enum):
    """Bounded physical actions accepted by the executor."""

    RCE_EXPORT = "rce_export"
    PV_CHARGE_HOLD = "pv_charge_hold"
    TARIFF_BATTERY_CHARGE = "tariff_battery_charge"
    TARIFF_GRID_SUPPORT = "tariff_grid_support"
    TARIFF_GRID_SUPPORT_AND_CHARGE = "tariff_grid_support_and_charge"
    RCM_ABSORB_PV = "rcm_absorb_pv"
    RCM_LIMIT_EXPORT = "rcm_limit_export"
    RCM_ABSORB_AND_LIMIT = "rcm_absorb_and_limit"
    RCM_PRE_DISCHARGE = "rcm_pre_discharge"
    BALANCING_CHARGE = "balancing_charge"
    MANUAL_GRID_CHARGE = "manual_grid_charge"
    MANUAL_GRID_DISCHARGE = "manual_grid_discharge"
    MASTER_STOP = "master_stop"


class ExecutionReason(str, Enum):
    """Stable reasons for state changes and fail-closed outcomes."""

    IDLE = "idle"
    CANDIDATE_SELECTED = "candidate_selected"
    OWNER_ACQUIRED = "owner_acquired"
    COMMAND_READY = "command_ready"
    WAITING_READBACK = "waiting_readback"
    WAITING_PHYSICAL = "waiting_physical_verification"
    PHYSICALLY_CONFIRMED = "physically_confirmed"
    CONTINUATION_CONFIRMED = "continuation_confirmed"
    AUTHORIZATION_LOST = "authorization_lost"
    STOP_REQUESTED = "stop_requested"
    DEADLINE_REACHED = "deadline_reached"
    RESTORING = "restoring"
    RESTORE_CONFIRMED = "restore_confirmed"
    RESTORE_NOT_REQUIRED = "restore_not_required"
    STARTS_DISABLED = "starts_disabled"
    POLICY_DISABLED = "policy_disabled"
    STALE_INPUTS = "stale_inputs"
    SNAPSHOT_INVALID = "snapshot_invalid"
    SNAPSHOT_CHANGED = "snapshot_changed_before_write"
    OWNER_CONFLICT = "owner_conflict"
    FOREIGN_OWNER = "foreign_owner"
    FULL_BLOCK_UNAVAILABLE = "full_block_unavailable"
    DIRECT_REGISTER_UNAVAILABLE = "direct_register_unavailable"
    TOPOLOGY_BLOCKED = "topology_blocked"
    BMS_UNAVAILABLE = "bms_unavailable"
    DIRECTION_UNAVAILABLE = "direction_unavailable"
    CONFIRMED_ZERO_EXPORT = "confirmed_zero_export"
    EXPORT_PROHIBITED = "export_prohibited"
    EXPORT_LIMIT_INCREASE = "export_limit_increase"
    READBACK_PENDING = "readback_pending"
    READBACK_MISMATCH = "readback_mismatch"
    PHYSICAL_PENDING = "physical_verification_pending"
    PHYSICAL_CONTRADICTION = "physical_flow_contradiction"
    PHYSICAL_UNAVAILABLE = "physical_verification_unavailable"
    COMMAND_NOT_QUEUED = "command_not_queued"
    COMMAND_OUTCOME_UNKNOWN = "command_outcome_unknown"
    ROLLBACK_FAILED = "rollback_failed"
    RESTART_RECOVERY = "restart_recovery"
    RESTART_RESELECT_REQUIRED = "restart_reselect_required"
    MASTER_STOP_REQUESTED = "master_stop_requested"
    MASTER_STOP_COMPLETE = "master_stop_complete"
    MASTER_STOP_BLOCKED = "master_stop_blocked"
    OFF_GRID_PRESERVED = "off_grid_preserved"
    INVALID_TRANSITION = "invalid_transition"


class EmsMode(IntEnum):
    """Physical values accepted by the EMS 4300 register."""

    SELF_USE = 0
    OFF_GRID = 3
    GRID_CHARGE = 4
    GRID_DISCHARGE = 5


class ExportAuthority(str, Enum):
    """Verdict derived only from coherent physical GCF readback."""

    VERIFIED_ALLOWED = "verified_allowed"
    CONFIRMED_ZERO_EXPORT = "confirmed_zero_export"
    PROHIBITED = "prohibited"


class VerificationStatus(str, Enum):
    """Result of readback or physical verification."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    CONTRADICTED = "contradicted"
    UNAVAILABLE = "unavailable"


class RollbackStatus(str, Enum):
    """Restoration lifecycle published with every transaction."""

    NOT_REQUIRED = "not_required"
    PENDING = "pending"
    CONFIRMED = "confirmed"
    FAILED = "failed"


class MasterStopStatus(str, Enum):
    """Durable MASTER STOP outcome."""

    NOT_REQUESTED = "not_requested"
    REQUESTED = "requested"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class ReadbackVerdict(str, Enum):
    """Internal comparison result for a complete settings cohort."""

    PENDING = "pending"
    MATCH = "match"
    MISMATCH = "mismatch"


class AtomicWriteFamily(str, Enum):
    """Exact backend actions exposed by the ESPHome Supervisor API."""

    EMS_COMPLETE_BLOCK = "ems_supervisor_write_complete_block"
    GCF_EXPORT_LIMIT = "ems_supervisor_write_gcf_export_limit"
    BATTERY_CHARGE_LIMIT = "ems_supervisor_write_battery_charge_limit"


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def _require_time(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or not _aware(value):
        raise ValueError(f"{name} must be timezone-aware")


def _require_number(
    value: float,
    name: str,
    minimum: float,
    maximum: float,
    *,
    scale: int,
) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    numeric = float(value)
    if not isfinite(numeric) or not minimum <= numeric <= maximum:
        raise ValueError(f"{name} is outside its physical range")
    if abs(numeric * scale - round(numeric * scale)) > 1e-6:
        raise ValueError(f"{name} has unsupported register precision")


def _require_generation(value: int, name: str) -> None:
    if type(value) is not int or not 1 <= value <= MAX_GENERATION:
        raise ValueError(f"{name} is not a valid physical generation")


def _generation_is_newer(current: int, baseline: int) -> bool:
    """Compare the wrapping physical generation using serial-number arithmetic."""

    distance = (current - baseline) % MAX_GENERATION
    return 0 < distance < MAX_GENERATION // 2


def _timed_out(now: datetime, since: datetime, seconds: float) -> bool:
    """Return true at the bounded timeout without trusting a backward clock."""

    return (now - since).total_seconds() >= seconds


def _close(left: float, right: float, tolerance: float) -> bool:
    return abs(float(left) - float(right)) <= tolerance


def _latest_observed_at(
    snapshot: "SettingsSnapshot",
    *,
    required_families: Iterable[AtomicWriteFamily] | None = None,
) -> datetime:
    required = (
        frozenset(AtomicWriteFamily)
        if required_families is None
        else frozenset(required_families)
    )
    observed = [snapshot.ems_observed_at]
    if AtomicWriteFamily.GCF_EXPORT_LIMIT in required:
        if snapshot.gcf_observed_at is None:
            raise ValueError("required GCF cohort is unavailable")
        observed.append(snapshot.gcf_observed_at)
    if AtomicWriteFamily.BATTERY_CHARGE_LIMIT in required:
        if snapshot.battery_observed_at is None:
            raise ValueError("required 306 cohort is unavailable")
        observed.append(snapshot.battery_observed_at)
    return max(observed)


@dataclass(frozen=True, slots=True)
class EmsBlock:
    """Complete physical EMS register block 4300-4306."""

    mode: EmsMode
    self_use_soc_percent_4301: float
    backup_soc_percent_4302: float
    force_charge_soc_percent_4303: float
    maximum_charge_power_percent_4304: float
    force_discharge_soc_percent_4305: float
    maximum_discharge_power_percent_4306: float

    def __post_init__(self) -> None:
        if not isinstance(self.mode, EmsMode):
            raise ValueError("mode must be EmsMode")
        _require_number(
            self.self_use_soc_percent_4301,
            "4301",
            10.0,
            100.0,
            scale=1,
        )
        _require_number(
            self.backup_soc_percent_4302,
            "4302",
            60.0,
            100.0,
            scale=1,
        )
        _require_number(
            self.force_charge_soc_percent_4303,
            "4303",
            10.0,
            100.0,
            scale=1,
        )
        _require_number(
            self.maximum_charge_power_percent_4304,
            "4304",
            0.0,
            100.0,
            scale=10,
        )
        _require_number(
            self.force_discharge_soc_percent_4305,
            "4305",
            0.0,
            100.0,
            scale=1,
        )
        _require_number(
            self.maximum_discharge_power_percent_4306,
            "4306",
            0.0,
            100.0,
            scale=10,
        )


def _ems_blocks_match(left: EmsBlock, right: EmsBlock) -> bool:
    return (
        left.mode is right.mode
        and _close(left.self_use_soc_percent_4301, right.self_use_soc_percent_4301, 0.5)
        and _close(left.backup_soc_percent_4302, right.backup_soc_percent_4302, 0.5)
        and _close(left.force_charge_soc_percent_4303, right.force_charge_soc_percent_4303, 0.5)
        and _close(
            left.maximum_charge_power_percent_4304,
            right.maximum_charge_power_percent_4304,
            0.05,
        )
        and _close(
            left.force_discharge_soc_percent_4305,
            right.force_discharge_soc_percent_4305,
            0.5,
        )
        and _close(
            left.maximum_discharge_power_percent_4306,
            right.maximum_discharge_power_percent_4306,
            0.05,
        )
    )


@dataclass(frozen=True, slots=True)
class SettingsSnapshot:
    """Provenance-preserving settings snapshot used by one transaction.

    The complete EMS 4300-4306 cohort is mandatory.  Independent direct
    register cohorts may be unavailable and stay explicit as an all-``None``
    group; they become mandatory only when the exact action requires them.
    """

    ems_block: EmsBlock
    ems_generation: int
    ems_observed_at: datetime
    ems_coherent: bool
    gcf_enabled_258: bool | None
    export_limit_percent_259: float | None
    gcf_generation: int | None
    gcf_observed_at: datetime | None
    gcf_coherent: bool
    battery_max_charge_power_percent_306: float | None
    battery_generation: int | None
    battery_observed_at: datetime | None
    battery_coherent: bool

    def __post_init__(self) -> None:
        _require_generation(self.ems_generation, "EMS generation")
        _require_time(self.ems_observed_at, "EMS observed_at")
        if type(self.ems_coherent) is not bool:
            raise ValueError("EMS coherence must be boolean")
        if type(self.gcf_coherent) is not bool:
            raise ValueError("GCF coherence must be boolean")
        if type(self.battery_coherent) is not bool:
            raise ValueError("306 coherence must be boolean")
        gcf_values = (
            self.gcf_enabled_258,
            self.export_limit_percent_259,
            self.gcf_generation,
            self.gcf_observed_at,
        )
        if any(value is not None for value in gcf_values) and not all(
            value is not None for value in gcf_values
        ):
            raise ValueError("GCF cohort must be complete or unavailable")
        if all(value is None for value in gcf_values):
            if self.gcf_coherent:
                raise ValueError("unavailable GCF cohort cannot be coherent")
        else:
            if type(self.gcf_enabled_258) is not bool:
                raise ValueError("GCF enable readback must be boolean")
            _require_number(
                self.export_limit_percent_259,
                "259",
                -10.0,
                200.0,
                scale=10,
            )
            _require_generation(self.gcf_generation, "GCF generation")
            _require_time(self.gcf_observed_at, "GCF observed_at")
        battery_values = (
            self.battery_max_charge_power_percent_306,
            self.battery_generation,
            self.battery_observed_at,
        )
        if any(value is not None for value in battery_values) and not all(
            value is not None for value in battery_values
        ):
            raise ValueError("306 cohort must be complete or unavailable")
        if all(value is None for value in battery_values):
            if self.battery_coherent:
                raise ValueError("unavailable 306 cohort cannot be coherent")
        else:
            _require_number(
                self.battery_max_charge_power_percent_306,
                "306",
                10.0,
                100.0,
                scale=10,
            )
            _require_generation(self.battery_generation, "306 generation")
            _require_time(self.battery_observed_at, "306 observed_at")

    @property
    def gcf_available(self) -> bool:
        return self.gcf_enabled_258 is not None

    @property
    def battery_306_available(self) -> bool:
        return self.battery_max_charge_power_percent_306 is not None

    @property
    def export_authority(self) -> ExportAuthority:
        if not self.gcf_available:
            return ExportAuthority.PROHIBITED
        assert self.gcf_enabled_258 is not None
        assert self.export_limit_percent_259 is not None
        if not self.gcf_enabled_258:
            return ExportAuthority.VERIFIED_ALLOWED
        if _close(self.export_limit_percent_259, 0.0, 0.05):
            return ExportAuthority.CONFIRMED_ZERO_EXPORT
        if self.export_limit_percent_259 > 0.0:
            return ExportAuthority.VERIFIED_ALLOWED
        return ExportAuthority.PROHIBITED

    def freshness_errors(
        self,
        now: datetime,
        *,
        maximum_age_seconds: float = MAX_READBACK_AGE_SECONDS,
        settings_maximum_age_seconds: float = SETTINGS_READBACK_MAX_AGE_SECONDS,
        required_families: Iterable[AtomicWriteFamily] | None = None,
    ) -> tuple[str, ...]:
        _require_time(now, "now")
        if not isfinite(maximum_age_seconds) or maximum_age_seconds <= 0.0:
            raise ValueError("maximum age must be positive")
        if (
            not isfinite(settings_maximum_age_seconds)
            or settings_maximum_age_seconds <= 0.0
        ):
            raise ValueError("settings maximum age must be positive")
        errors: list[str] = []
        required = (
            frozenset(AtomicWriteFamily)
            if required_families is None
            else frozenset(required_families)
        )
        cohorts: list[tuple[str, datetime, bool, float]] = [
            ("ems", self.ems_observed_at, self.ems_coherent, maximum_age_seconds)
        ]
        if AtomicWriteFamily.GCF_EXPORT_LIMIT in required:
            if not self.gcf_available:
                errors.append("gcf_unavailable")
            else:
                assert self.gcf_observed_at is not None
                cohorts.append(
                    (
                        "gcf",
                        self.gcf_observed_at,
                        self.gcf_coherent,
                        settings_maximum_age_seconds,
                    )
                )
        if AtomicWriteFamily.BATTERY_CHARGE_LIMIT in required:
            if not self.battery_306_available:
                errors.append("battery_306_unavailable")
            else:
                assert self.battery_observed_at is not None
                cohorts.append(
                    (
                        "battery_306",
                        self.battery_observed_at,
                        self.battery_coherent,
                        settings_maximum_age_seconds,
                    )
                )
        for name, observed_at, coherent, maximum_age in cohorts:
            if not coherent:
                errors.append(f"{name}_incoherent")
                continue
            age = (now - observed_at).total_seconds()
            if age < -MAX_FUTURE_SKEW_SECONDS:
                errors.append(f"{name}_future")
            elif age > maximum_age:
                errors.append(f"{name}_stale")
        return tuple(errors)

    def values_match(
        self,
        other: "SettingsSnapshot",
        *,
        required_families: Iterable[AtomicWriteFamily] | None = None,
    ) -> bool:
        required = (
            frozenset(AtomicWriteFamily)
            if required_families is None
            else frozenset(required_families)
        )
        if not _ems_blocks_match(self.ems_block, other.ems_block):
            return False
        if AtomicWriteFamily.GCF_EXPORT_LIMIT in required:
            if not self.gcf_available or not other.gcf_available:
                return False
            assert self.export_limit_percent_259 is not None
            assert other.export_limit_percent_259 is not None
            if (
                self.gcf_enabled_258 is not other.gcf_enabled_258
                or not _close(
                    self.export_limit_percent_259,
                    other.export_limit_percent_259,
                    0.05,
                )
            ):
                return False
        if AtomicWriteFamily.BATTERY_CHARGE_LIMIT in required:
            if not self.battery_306_available or not other.battery_306_available:
                return False
            assert self.battery_max_charge_power_percent_306 is not None
            assert other.battery_max_charge_power_percent_306 is not None
            if not _close(
                self.battery_max_charge_power_percent_306,
                other.battery_max_charge_power_percent_306,
                0.05,
            ):
                return False
        return True


class AtomicWriteNotQueued(RuntimeError):
    """A correlated accepted:false response proves a pre-transport rejection.

    An absent, malformed, timed-out or merely successful HA service response
    cannot be represented by this exception.
    """

    def __init__(self, reason: str) -> None:
        if not isinstance(reason, str) or not reason:
            raise ValueError("a confirmed prequeue rejection requires its reason")
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True, slots=True)
class AtomicWrite:
    """One generation-bound call to one of the three firmware API actions."""

    family: AtomicWriteFamily
    snapshot_generation: int
    ems_block: EmsBlock | None = None
    target_percent: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.family, AtomicWriteFamily):
            raise ValueError("atomic write family is invalid")
        _require_generation(self.snapshot_generation, "atomic snapshot generation")
        if self.family is AtomicWriteFamily.EMS_COMPLETE_BLOCK:
            if self.ems_block is None or self.target_percent is not None:
                raise ValueError("complete-block write requires exactly one EMS block")
            return
        if self.ems_block is not None or self.target_percent is None:
            raise ValueError("direct-register write requires exactly one target")
        if self.family is AtomicWriteFamily.GCF_EXPORT_LIMIT:
            _require_number(
                self.target_percent,
                "atomic command 259",
                -10.0,
                200.0,
                scale=10,
            )
            return
        _require_number(
            self.target_percent,
            "atomic command 306",
            10.0,
            100.0,
            scale=10,
        )


@dataclass(frozen=True, slots=True)
class CommandSet:
    """Logical transaction composed only of the three firmware write families."""

    ems_block: EmsBlock | None = None
    export_limit_percent_259: float | None = None
    battery_max_charge_power_percent_306: float | None = None

    def __post_init__(self) -> None:
        if (
            self.ems_block is None
            and self.export_limit_percent_259 is None
            and self.battery_max_charge_power_percent_306 is None
        ):
            raise ValueError("command set is empty")
        if self.export_limit_percent_259 is not None:
            _require_number(
                self.export_limit_percent_259,
                "command 259",
                -10.0,
                200.0,
                scale=10,
            )
        if self.battery_max_charge_power_percent_306 is not None:
            _require_number(
                self.battery_max_charge_power_percent_306,
                "command 306",
                10.0,
                100.0,
                scale=10,
            )

    @property
    def touches_ems(self) -> bool:
        return self.ems_block is not None

    @property
    def touches_gcf(self) -> bool:
        return self.export_limit_percent_259 is not None

    @property
    def touches_battery_306(self) -> bool:
        return self.battery_max_charge_power_percent_306 is not None

    @property
    def families(self) -> frozenset[AtomicWriteFamily]:
        families: set[AtomicWriteFamily] = set()
        if self.touches_ems:
            families.add(AtomicWriteFamily.EMS_COMPLETE_BLOCK)
        if self.touches_gcf:
            families.add(AtomicWriteFamily.GCF_EXPORT_LIMIT)
        if self.touches_battery_306:
            families.add(AtomicWriteFamily.BATTERY_CHARGE_LIMIT)
        return frozenset(families)

    def atomic_writes(
        self,
        *,
        ems_snapshot_generation: int,
        gcf_snapshot_generation: int | None,
        battery_snapshot_generation: int | None,
    ) -> tuple[AtomicWrite, ...]:
        """Return the exact generation-bound API calls in safe dispatch order."""

        writes: list[AtomicWrite] = []
        if self.ems_block is not None:
            writes.append(
                AtomicWrite(
                    family=AtomicWriteFamily.EMS_COMPLETE_BLOCK,
                    snapshot_generation=ems_snapshot_generation,
                    ems_block=self.ems_block,
                )
            )
        if self.export_limit_percent_259 is not None:
            if gcf_snapshot_generation is None:
                raise ValueError("GCF write has no physical base generation")
            writes.append(
                AtomicWrite(
                    family=AtomicWriteFamily.GCF_EXPORT_LIMIT,
                    snapshot_generation=gcf_snapshot_generation,
                    target_percent=self.export_limit_percent_259,
                )
            )
        if self.battery_max_charge_power_percent_306 is not None:
            if battery_snapshot_generation is None:
                raise ValueError("306 write has no physical base generation")
            writes.append(
                AtomicWrite(
                    family=AtomicWriteFamily.BATTERY_CHARGE_LIMIT,
                    snapshot_generation=battery_snapshot_generation,
                    target_percent=self.battery_max_charge_power_percent_306,
                )
            )
        return tuple(writes)


_ACTION_OWNER = {
    ExecutionAction.RCE_EXPORT: ExecutionOwner.RCE,
    ExecutionAction.PV_CHARGE_HOLD: ExecutionOwner.RCE,
    ExecutionAction.TARIFF_BATTERY_CHARGE: ExecutionOwner.TARIFF,
    ExecutionAction.TARIFF_GRID_SUPPORT: ExecutionOwner.TARIFF,
    ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE: ExecutionOwner.TARIFF,
    ExecutionAction.RCM_ABSORB_PV: ExecutionOwner.RCM,
    ExecutionAction.RCM_LIMIT_EXPORT: ExecutionOwner.RCM,
    ExecutionAction.RCM_ABSORB_AND_LIMIT: ExecutionOwner.RCM,
    ExecutionAction.RCM_PRE_DISCHARGE: ExecutionOwner.RCM,
    ExecutionAction.BALANCING_CHARGE: ExecutionOwner.BALANCING,
    ExecutionAction.MANUAL_GRID_CHARGE: ExecutionOwner.MANUAL,
    ExecutionAction.MANUAL_GRID_DISCHARGE: ExecutionOwner.MANUAL,
    ExecutionAction.MASTER_STOP: ExecutionOwner.MANUAL,
}

_EXPORT_ACTIONS = frozenset(
    {
        ExecutionAction.PV_CHARGE_HOLD,
        ExecutionAction.RCE_EXPORT,
        ExecutionAction.RCM_PRE_DISCHARGE,
        ExecutionAction.MANUAL_GRID_DISCHARGE,
    }
)

_CHARGE_ACTIONS = frozenset(
    {
        ExecutionAction.TARIFF_BATTERY_CHARGE,
        ExecutionAction.TARIFF_GRID_SUPPORT,
        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        ExecutionAction.RCM_ABSORB_PV,
        ExecutionAction.RCM_ABSORB_AND_LIMIT,
        ExecutionAction.BALANCING_CHARGE,
        ExecutionAction.MANUAL_GRID_CHARGE,
    }
)


def _required_readback_families(
    command: CommandSet,
    action: ExecutionAction,
    *,
    include_action_authority: bool = True,
) -> frozenset[AtomicWriteFamily]:
    """Return non-EMS cohorts that grant or confirm this exact action.

    A fresh coherent EMS block is always required separately.  Direct 259/306
    cohorts are policy-local: a transaction observes only the families it
    writes, except that every export/discharge action also requires the
    physical GCF/export-limit cohort as authority.
    """

    required = set(command.families)
    required.discard(AtomicWriteFamily.EMS_COMPLETE_BLOCK)
    if include_action_authority and action in _EXPORT_ACTIONS:
        required.add(AtomicWriteFamily.GCF_EXPORT_LIMIT)
    return frozenset(required)


@dataclass(frozen=True, slots=True)
class ActuatorIntent:
    """Exact selected intent passed from arbitration to the executor."""

    policy: ExecutionOwner
    action: ExecutionAction
    command: CommandSet
    candidate_revision: str
    deadline: datetime
    physical_verification_required: bool = True

    def __post_init__(self) -> None:
        if self.policy is ExecutionOwner.NONE:
            raise ValueError("none cannot own an actuator intent")
        if _ACTION_OWNER.get(self.action) is not self.policy:
            raise ValueError("action does not belong to the selected owner")
        if not isinstance(self.candidate_revision, str) or not _REVISION.fullmatch(
            self.candidate_revision
        ):
            raise ValueError("candidate revision is malformed")
        _require_time(self.deadline, "intent deadline")
        if type(self.physical_verification_required) is not bool:
            raise ValueError("physical verification flag must be boolean")
        if not self.physical_verification_required:
            raise ValueError("Active intents cannot bypass physical verification")
        _validate_action_command(self.action, self.command)


def _retarget_mode(intent: ActuatorIntent) -> EmsMode | None:
    """The two explicitly supported automatic full-block retarget families."""
    mode = None
    if intent.policy is ExecutionOwner.RCE and intent.action is ExecutionAction.RCE_EXPORT:
        mode = EmsMode.GRID_DISCHARGE
    elif intent.policy is ExecutionOwner.TARIFF and intent.action in {
        ExecutionAction.TARIFF_BATTERY_CHARGE,
        ExecutionAction.TARIFF_GRID_SUPPORT,
        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
    }:
        mode = EmsMode.GRID_CHARGE
    if (
        mode is None
        or intent.command.families != frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
        or intent.command.ems_block is None
        or intent.command.ems_block.mode is not mode
    ):
        return None
    return mode


def _validate_action_command(action: ExecutionAction, command: CommandSet) -> None:
    if action is ExecutionAction.PV_CHARGE_HOLD:
        if (command.ems_block is None or command.ems_block.mode is not EmsMode.GRID_DISCHARGE
                or command.ems_block.maximum_discharge_power_percent_4306 != 1.
                or command.touches_gcf or command.touches_battery_306):
            raise ValueError("PV hold requires a complete Mode 5 block and a 1% discharge limit")
    elif action in _EXPORT_ACTIONS:
        if command.ems_block is None or command.ems_block.mode is not EmsMode.GRID_DISCHARGE:
            raise ValueError("export action requires a complete Grid Discharge block")
    elif action in {
        ExecutionAction.TARIFF_BATTERY_CHARGE,
        ExecutionAction.TARIFF_GRID_SUPPORT,
        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        ExecutionAction.BALANCING_CHARGE,
        ExecutionAction.MANUAL_GRID_CHARGE,
    }:
        if command.ems_block is None or command.ems_block.mode is not EmsMode.GRID_CHARGE:
            raise ValueError("charge action requires a complete Grid Charge block")
    elif action is ExecutionAction.RCM_ABSORB_PV:
        if not command.touches_battery_306 or command.touches_ems or command.touches_gcf:
            raise ValueError("RCM absorb requires only register 306")
    elif action is ExecutionAction.RCM_LIMIT_EXPORT:
        if not command.touches_gcf or command.touches_ems or command.touches_battery_306:
            raise ValueError("RCM limit requires only register 259")
    elif action is ExecutionAction.RCM_ABSORB_AND_LIMIT:
        if command.touches_ems or not command.touches_gcf or not command.touches_battery_306:
            raise ValueError("combined RCM action requires registers 259 and 306")
    elif action is ExecutionAction.MASTER_STOP:
        if command.ems_block is None or command.ems_block.mode not in {
            EmsMode.SELF_USE,
            EmsMode.OFF_GRID,
        }:
            raise ValueError("MASTER STOP requires a complete neutral EMS block")


@dataclass(frozen=True, slots=True)
class ExecutionGates:
    """Backend observations required before ownership or a physical write."""

    inputs_fresh: bool
    bms_fresh: bool
    full_block_ready: bool
    direct_306_ready: bool
    direct_259_ready: bool
    full_block_topology_ready: bool
    direct_register_topology_ready: bool
    charge_direction_ready: bool
    discharge_direction_ready: bool
    owner_conflict: bool = False
    observed_owner: ExecutionOwner | str | None = None
    pv_charge_hold_ready: bool = False

    def __post_init__(self) -> None:
        for name in (
            "inputs_fresh",
            "bms_fresh",
            "full_block_ready",
            "direct_306_ready",
            "direct_259_ready",
            "full_block_topology_ready",
            "direct_register_topology_ready",
            "charge_direction_ready",
            "discharge_direction_ready",
            "pv_charge_hold_ready",
            "owner_conflict",
        ):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if self.observed_owner is ExecutionOwner.NONE:
            object.__setattr__(self, "observed_owner", None)
        elif isinstance(self.observed_owner, str):
            try:
                parsed_owner = ExecutionOwner(self.observed_owner)
            except ValueError:
                pass
            else:
                object.__setattr__(
                    self,
                    "observed_owner",
                    None if parsed_owner is ExecutionOwner.NONE else parsed_owner,
                )
        elif self.observed_owner is not None and not isinstance(
            self.observed_owner,
            ExecutionOwner,
        ):
            raise ValueError("observed_owner must be an owner code or string")


@dataclass(frozen=True, slots=True)
class ExpectedReadback:
    """Expected settings and exact generations that must advance.

    Unrelated unavailable direct-register cohorts remain ``None`` and do not
    acquire authority by being persisted with an invented placeholder.
    """

    ems_block: EmsBlock
    gcf_enabled_258: bool | None
    export_limit_percent_259: float | None
    battery_max_charge_power_percent_306: float | None
    base_ems_generation: int
    base_gcf_generation: int | None
    base_battery_generation: int | None
    written_families: frozenset[AtomicWriteFamily]

    def __post_init__(self) -> None:
        _require_generation(self.base_ems_generation, "expected EMS generation")
        if not self.written_families or any(
            not isinstance(family, AtomicWriteFamily)
            for family in self.written_families
        ):
            raise ValueError("expected readback must name written API families")
        gcf_values = (
            self.gcf_enabled_258,
            self.export_limit_percent_259,
            self.base_gcf_generation,
        )
        if any(value is not None for value in gcf_values) and not all(
            value is not None for value in gcf_values
        ):
            raise ValueError("expected GCF cohort must be complete or unavailable")
        if all(value is not None for value in gcf_values):
            if type(self.gcf_enabled_258) is not bool:
                raise ValueError("expected GCF enable state must be boolean")
            _require_number(
                self.export_limit_percent_259,
                "expected 259",
                -10.0,
                200.0,
                scale=10,
            )
            _require_generation(self.base_gcf_generation, "expected GCF generation")
        elif AtomicWriteFamily.GCF_EXPORT_LIMIT in self.written_families:
            raise ValueError("written GCF family has no physical base cohort")
        battery_values = (
            self.battery_max_charge_power_percent_306,
            self.base_battery_generation,
        )
        if any(value is not None for value in battery_values) and not all(
            value is not None for value in battery_values
        ):
            raise ValueError("expected 306 cohort must be complete or unavailable")
        if all(value is not None for value in battery_values):
            _require_number(
                self.battery_max_charge_power_percent_306,
                "expected 306",
                10.0,
                100.0,
                scale=10,
            )
            _require_generation(
                self.base_battery_generation,
                "expected battery generation",
            )
        elif AtomicWriteFamily.BATTERY_CHARGE_LIMIT in self.written_families:
            raise ValueError("written 306 family has no physical base cohort")

    @classmethod
    def from_command(
        cls,
        snapshot: SettingsSnapshot,
        command: CommandSet,
    ) -> "ExpectedReadback":
        return cls(
            ems_block=command.ems_block or snapshot.ems_block,
            gcf_enabled_258=snapshot.gcf_enabled_258,
            export_limit_percent_259=(
                command.export_limit_percent_259
                if command.export_limit_percent_259 is not None
                else snapshot.export_limit_percent_259
            ),
            battery_max_charge_power_percent_306=(
                command.battery_max_charge_power_percent_306
                if command.battery_max_charge_power_percent_306 is not None
                else snapshot.battery_max_charge_power_percent_306
            ),
            base_ems_generation=snapshot.ems_generation,
            base_gcf_generation=snapshot.gcf_generation,
            base_battery_generation=snapshot.battery_generation,
            written_families=command.families,
        )

    def compare(
        self,
        readback: SettingsSnapshot,
        *,
        now: datetime,
        not_before: datetime,
        required_families: Iterable[AtomicWriteFamily] | None = None,
        maximum_readback_age_seconds: float = MAX_READBACK_AGE_SECONDS,
    ) -> ReadbackVerdict:
        required = (
            frozenset(AtomicWriteFamily)
            if required_families is None
            else frozenset(required_families)
        )
        if readback.freshness_errors(
            now,
            maximum_age_seconds=maximum_readback_age_seconds,
            required_families=required,
        ) or (
            maximum_readback_age_seconds > MAX_READBACK_AGE_SECONDS
            and (now - readback.ems_observed_at).total_seconds()
            >= maximum_readback_age_seconds
        ):
            return ReadbackVerdict.PENDING
        if (
            AtomicWriteFamily.EMS_COMPLETE_BLOCK in self.written_families
            and (
                not _generation_is_newer(
                    readback.ems_generation,
                    self.base_ems_generation,
                )
                or readback.ems_observed_at <= not_before
            )
        ):
            return ReadbackVerdict.PENDING
        if (
            AtomicWriteFamily.GCF_EXPORT_LIMIT in self.written_families
            and (
                self.base_gcf_generation is None
                or readback.gcf_generation is None
                or readback.gcf_observed_at is None
                or not _generation_is_newer(
                    readback.gcf_generation,
                    self.base_gcf_generation,
                )
                or readback.gcf_observed_at <= not_before
            )
        ):
            return ReadbackVerdict.PENDING
        if (
            AtomicWriteFamily.BATTERY_CHARGE_LIMIT in self.written_families
            and (
                self.base_battery_generation is None
                or readback.battery_generation is None
                or readback.battery_observed_at is None
                or not _generation_is_newer(
                    readback.battery_generation,
                    self.base_battery_generation,
                )
                or readback.battery_observed_at <= not_before
            )
        ):
            return ReadbackVerdict.PENDING
        if not _ems_blocks_match(readback.ems_block, self.ems_block):
            return ReadbackVerdict.MISMATCH
        if AtomicWriteFamily.GCF_EXPORT_LIMIT in required:
            if (
                not readback.gcf_available
                or self.gcf_enabled_258 is None
                or self.export_limit_percent_259 is None
            ):
                return ReadbackVerdict.PENDING
            assert readback.export_limit_percent_259 is not None
            if (
                readback.gcf_enabled_258 is not self.gcf_enabled_258
                or not _close(
                    readback.export_limit_percent_259,
                    self.export_limit_percent_259,
                    0.05,
                )
            ):
                return ReadbackVerdict.MISMATCH
        if AtomicWriteFamily.BATTERY_CHARGE_LIMIT in required:
            if (
                not readback.battery_306_available
                or self.battery_max_charge_power_percent_306 is None
            ):
                return ReadbackVerdict.PENDING
            assert readback.battery_max_charge_power_percent_306 is not None
            if not _close(
                readback.battery_max_charge_power_percent_306,
                self.battery_max_charge_power_percent_306,
                0.05,
            ):
                return ReadbackVerdict.MISMATCH
        return ReadbackVerdict.MATCH


@dataclass(frozen=True, slots=True)
class PhysicalVerification:
    """Independent physical-flow evidence correlated to one transaction."""

    transaction_id: str
    action: ExecutionAction
    status: VerificationStatus
    observed_at: datetime
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_transaction_id(self.transaction_id)
        _require_time(self.observed_at, "physical verification observed_at")
        if len(self.evidence) > 16 or any(
            not isinstance(item, str) or len(item) > 160 for item in self.evidence
        ):
            raise ValueError("physical verification evidence is unbounded")


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    """Command returned to the transport adapter after the final pre-write gate."""

    transaction_id: str
    owner: ExecutionOwner
    action: ExecutionAction
    command: CommandSet
    expected_readback: ExpectedReadback

    @property
    def atomic_writes(self) -> tuple[AtomicWrite, ...]:
        """Exact calls an adapter must dispatch before marking the envelope sent."""

        return self.command.atomic_writes(
            ems_snapshot_generation=self.expected_readback.base_ems_generation,
            gcf_snapshot_generation=self.expected_readback.base_gcf_generation,
            battery_snapshot_generation=self.expected_readback.base_battery_generation,
        )


@dataclass(frozen=True, slots=True)
class MasterStopResult:
    """Recorder-safe MASTER STOP result."""

    status: MasterStopStatus = MasterStopStatus.NOT_REQUESTED
    transaction_id: str | None = None
    requested_at: datetime | None = None
    completed_at: datetime | None = None
    off_grid_preserved: bool = False
    reason: ExecutionReason = ExecutionReason.IDLE


@dataclass(frozen=True, slots=True)
class TransactionRecord:
    """Durable evidence for one selected or executing action."""

    transaction_id: str
    state: ActiveState
    intent: ActuatorIntent
    owner: ExecutionOwner
    started_at: datetime
    deadline: datetime
    reason: ExecutionReason
    command_snapshot: SettingsSnapshot | None = None
    prewrite_snapshot: SettingsSnapshot | None = None
    expected_readback: ExpectedReadback | None = None
    command_sent_at: datetime | None = None
    readback_result: VerificationStatus = VerificationStatus.PENDING
    physical_verification: PhysicalVerification | None = None
    rollback_status: RollbackStatus = RollbackStatus.NOT_REQUIRED
    rollback_result: VerificationStatus = VerificationStatus.PENDING
    restore_command: CommandSet | None = None
    restore_snapshot: SettingsSnapshot | None = None
    restore_expected_readback: ExpectedReadback | None = None
    restore_sent_at: datetime | None = None
    restore_attempts: int = 0
    restore_not_queued_count: int = 0
    restore_not_queued_first_at: datetime | None = None
    lease_identity: tuple[str, str, int] | None = None
    terminal_epoch: int | None = None
    interruption_reason: ExecutionReason | None = None
    master_stop_requested: bool = False
    off_grid_preserved: bool = False


@dataclass(frozen=True, slots=True)
class ExecutorRecord:
    """Complete persistence boundary for the pure executor."""

    state: ActiveState = ActiveState.IDLE
    owner: ExecutionOwner = ExecutionOwner.NONE
    transaction: TransactionRecord | None = None
    last_transaction: TransactionRecord | None = None
    starts_allowed: bool = True
    automatic_policies_enabled: bool = True
    reason: ExecutionReason = ExecutionReason.IDLE
    master_stop_result: MasterStopResult = MasterStopResult()
    revision: int = 0


_LEGAL_TRANSITIONS = {
    ActiveState.IDLE: frozenset(
        {ActiveState.SELECTED, ActiveState.STOPPING, ActiveState.BLOCKED}
    ),
    ActiveState.SELECTED: frozenset(
        {ActiveState.STARTING, ActiveState.BLOCKED, ActiveState.IDLE, ActiveState.STOPPING}
    ),
    ActiveState.STARTING: frozenset(
        {
            ActiveState.WAITING_READBACK,
            ActiveState.STOPPING,
            ActiveState.BLOCKED,
            ActiveState.FAULT,
        }
    ),
    ActiveState.WAITING_READBACK: frozenset(
        {ActiveState.EXECUTING, ActiveState.STOPPING, ActiveState.FAULT}
    ),
    ActiveState.EXECUTING: frozenset(
        {ActiveState.RETARGETING, ActiveState.STOPPING, ActiveState.FAULT}
    ),
    ActiveState.RETARGETING: frozenset(
        {ActiveState.EXECUTING, ActiveState.STOPPING, ActiveState.FAULT}
    ),
    ActiveState.STOPPING: frozenset(
        {ActiveState.RESTORING, ActiveState.IDLE, ActiveState.FAULT}
    ),
    ActiveState.RESTORING: frozenset(
        {ActiveState.IDLE, ActiveState.STOPPING, ActiveState.FAULT}
    ),
    ActiveState.BLOCKED: frozenset(
        {ActiveState.IDLE, ActiveState.STOPPING, ActiveState.BLOCKED}
    ),
    ActiveState.FAULT: frozenset(
        {ActiveState.STOPPING, ActiveState.RESTORING, ActiveState.FAULT}
    ),
}


def _require_transaction_id(value: str) -> None:
    if not isinstance(value, str) or not _TRANSACTION_ID.fullmatch(value):
        raise ValueError("transaction id is malformed")


def _automatic_owner(owner: ExecutionOwner) -> bool:
    return owner in {ExecutionOwner.RCE, ExecutionOwner.TARIFF, ExecutionOwner.RCM}


def _command_gate_reason(
    command: CommandSet,
    action: ExecutionAction,
    gates: ExecutionGates,
    *,
    expected_owner: ExecutionOwner | None,
    master_stop: bool = False,
) -> ExecutionReason | None:
    if not gates.inputs_fresh:
        return ExecutionReason.STALE_INPUTS
    if gates.owner_conflict:
        return ExecutionReason.OWNER_CONFLICT
    observed = gates.observed_owner
    if observed is not None and observed is not expected_owner:
        return ExecutionReason.FOREIGN_OWNER
    if command.touches_ems:
        if not gates.full_block_ready:
            return ExecutionReason.FULL_BLOCK_UNAVAILABLE
        if not gates.full_block_topology_ready:
            return ExecutionReason.TOPOLOGY_BLOCKED
    if command.touches_gcf and not gates.direct_259_ready:
        return ExecutionReason.DIRECT_REGISTER_UNAVAILABLE
    if command.touches_battery_306 and not gates.direct_306_ready:
        return ExecutionReason.DIRECT_REGISTER_UNAVAILABLE
    if command.touches_gcf or command.touches_battery_306:
        if not gates.direct_register_topology_ready:
            return ExecutionReason.TOPOLOGY_BLOCKED
    if not master_stop and action in _CHARGE_ACTIONS:
        if not gates.bms_fresh:
            return ExecutionReason.BMS_UNAVAILABLE
        if not gates.charge_direction_ready:
            return ExecutionReason.DIRECTION_UNAVAILABLE
    if not master_stop and action in _EXPORT_ACTIONS:
        if not gates.bms_fresh:
            return ExecutionReason.BMS_UNAVAILABLE
        direction_ready = (gates.pv_charge_hold_ready if action is ExecutionAction.PV_CHARGE_HOLD
                           else gates.discharge_direction_ready)
        if not direction_ready:
            return ExecutionReason.DIRECTION_UNAVAILABLE
    return None


def _gate_reason(
    intent: ActuatorIntent,
    gates: ExecutionGates,
    *,
    expected_owner: ExecutionOwner | None,
    master_stop: bool = False,
) -> ExecutionReason | None:
    return _command_gate_reason(
        intent.command,
        intent.action,
        gates,
        expected_owner=expected_owner,
        master_stop=master_stop,
    )


def _settings_reason(
    intent: ActuatorIntent,
    settings: SettingsSnapshot,
) -> ExecutionReason | None:
    if (
        intent.action is not ExecutionAction.MASTER_STOP
        and settings.ems_block.mode is EmsMode.OFF_GRID
    ):
        return ExecutionReason.OFF_GRID_PRESERVED
    if intent.action in _EXPORT_ACTIONS:
        authority = settings.export_authority
        if authority is ExportAuthority.CONFIRMED_ZERO_EXPORT:
            return ExecutionReason.CONFIRMED_ZERO_EXPORT
        if authority is ExportAuthority.PROHIBITED:
            return ExecutionReason.EXPORT_PROHIBITED
    if intent.action in {
        ExecutionAction.RCM_LIMIT_EXPORT,
        ExecutionAction.RCM_ABSORB_AND_LIMIT,
    }:
        target = intent.command.export_limit_percent_259
        assert target is not None
        if settings.export_limit_percent_259 is None:
            return ExecutionReason.DIRECT_REGISTER_UNAVAILABLE
        if target > settings.export_limit_percent_259 + 0.05:
            return ExecutionReason.EXPORT_LIMIT_INCREASE
    return None


def _snapshot_generations_match(
    before: SettingsSnapshot,
    current: SettingsSnapshot,
    *,
    required_families: Iterable[AtomicWriteFamily],
) -> bool:
    """Match the exact physical generations consumed by firmware writes."""

    required = frozenset(required_families)
    if (
        AtomicWriteFamily.EMS_COMPLETE_BLOCK in required
        and before.ems_generation != current.ems_generation
    ):
        return False
    if (
        AtomicWriteFamily.GCF_EXPORT_LIMIT in required
        and before.gcf_generation != current.gcf_generation
    ):
        return False
    if (
        AtomicWriteFamily.BATTERY_CHARGE_LIMIT in required
        and before.battery_generation != current.battery_generation
    ):
        return False
    return True


def _written_generations_advanced(
    expected: ExpectedReadback,
    current: SettingsSnapshot,
    *,
    not_before: datetime,
) -> bool:
    """Require post-dispatch physical evidence for every written family."""

    for family in expected.written_families:
        if family is AtomicWriteFamily.EMS_COMPLETE_BLOCK:
            generation = current.ems_generation
            baseline = expected.base_ems_generation
            observed_at = current.ems_observed_at
        elif family is AtomicWriteFamily.GCF_EXPORT_LIMIT:
            generation = current.gcf_generation
            baseline = expected.base_gcf_generation
            observed_at = current.gcf_observed_at
        else:
            generation = current.battery_generation
            baseline = expected.base_battery_generation
            observed_at = current.battery_observed_at
        if (
            generation is None
            or baseline is None
            or observed_at is None
            or not _generation_is_newer(generation, baseline)
            or observed_at <= not_before
        ):
            return False
    return True


def _snapshot_generations_match_expected_base(
    snapshot: SettingsSnapshot,
    expected: ExpectedReadback,
) -> bool:
    """Return whether a snapshot is the baseline used by an expected readback."""

    for family in expected.written_families:
        if (
            family is AtomicWriteFamily.EMS_COMPLETE_BLOCK
            and snapshot.ems_generation != expected.base_ems_generation
        ):
            return False
        if (
            family is AtomicWriteFamily.GCF_EXPORT_LIMIT
            and snapshot.gcf_generation != expected.base_gcf_generation
        ):
            return False
        if (
            family is AtomicWriteFamily.BATTERY_CHARGE_LIMIT
            and snapshot.battery_generation != expected.base_battery_generation
        ):
            return False
    return True


def _unchanged_fields_preserved(
    intent: ActuatorIntent,
    settings: SettingsSnapshot,
) -> bool:
    command = intent.command
    if command.ems_block is None:
        return True
    before = settings.ems_block
    after = command.ems_block
    if intent.action in _EXPORT_ACTIONS:
        return (
            _close(before.self_use_soc_percent_4301, after.self_use_soc_percent_4301, 0.5)
            and _close(before.backup_soc_percent_4302, after.backup_soc_percent_4302, 0.5)
            and _close(
                before.force_charge_soc_percent_4303,
                after.force_charge_soc_percent_4303,
                0.5,
            )
            and _close(
                before.maximum_charge_power_percent_4304,
                after.maximum_charge_power_percent_4304,
                0.05,
            )
        )
    if intent.action in _CHARGE_ACTIONS:
        return (
            _close(before.self_use_soc_percent_4301, after.self_use_soc_percent_4301, 0.5)
            and _close(before.backup_soc_percent_4302, after.backup_soc_percent_4302, 0.5)
            and _close(
                before.force_discharge_soc_percent_4305,
                after.force_discharge_soc_percent_4305,
                0.5,
            )
            and _close(
                before.maximum_discharge_power_percent_4306,
                after.maximum_discharge_power_percent_4306,
                0.05,
            )
        )
    if intent.action is ExecutionAction.MASTER_STOP:
        neutral_before = replace(before, mode=after.mode)
        return _ems_blocks_match(neutral_before, after)
    return False


class SupervisorExecutor:
    """Deterministic, backend-agnostic Active transaction state machine."""

    def __init__(self, record: ExecutorRecord | None = None) -> None:
        self._record = record or ExecutorRecord()
        # Diagnostic only; deliberately excluded from persisted authority.
        self.last_continuation_input_failure: dict[str, object] | None = None
        self._validate_record(self._record)

    @property
    def record(self) -> ExecutorRecord:
        return self._record

    def select_intent(
        self,
        intent: ActuatorIntent,
        *,
        transaction_id: str,
        now: datetime,
    ) -> ExecutorRecord:
        self._require_state(ActiveState.IDLE)
        _require_transaction_id(transaction_id)
        _require_time(now, "now")
        if intent.deadline <= now:
            raise ValueError("intent deadline has already passed")
        transaction = TransactionRecord(
            transaction_id=transaction_id,
            state=ActiveState.SELECTED,
            intent=intent,
            owner=ExecutionOwner.NONE,
            started_at=now,
            deadline=intent.deadline,
            reason=ExecutionReason.CANDIDATE_SELECTED,
        )
        if not self._record.starts_allowed:
            return self._publish_blocked(transaction, ExecutionReason.STARTS_DISABLED)
        if _automatic_owner(intent.policy) and not self._record.automatic_policies_enabled:
            return self._publish_blocked(transaction, ExecutionReason.POLICY_DISABLED)
        return self._publish(
            ActiveState.SELECTED,
            owner=ExecutionOwner.NONE,
            transaction=transaction,
            reason=ExecutionReason.CANDIDATE_SELECTED,
        )

    def start(
        self,
        snapshot: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> ExecutorRecord:
        self._require_state(ActiveState.SELECTED)
        _require_time(now, "now")
        transaction = self._transaction()
        if now >= transaction.deadline:
            return self._publish_blocked(
                transaction,
                ExecutionReason.DEADLINE_REACHED,
            )
        reason = self._start_rejection(transaction.intent, snapshot, gates, now=now)
        if reason is not None:
            blocked = replace(
                transaction,
                state=ActiveState.BLOCKED,
                command_snapshot=snapshot,
                reason=reason,
            )
            return self._publish_blocked(blocked, reason)
        expected = ExpectedReadback.from_command(snapshot, transaction.intent.command)
        started = replace(
            transaction,
            state=ActiveState.STARTING,
            owner=transaction.intent.policy,
            command_snapshot=snapshot,
            expected_readback=expected,
            rollback_status=RollbackStatus.PENDING,
            reason=ExecutionReason.OWNER_ACQUIRED,
        )
        return self._publish(
            ActiveState.STARTING,
            owner=transaction.intent.policy,
            transaction=started,
            reason=ExecutionReason.OWNER_ACQUIRED,
        )

    def prepare_command(
        self,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> CommandEnvelope | None:
        self._require_state(ActiveState.STARTING)
        _require_time(now, "now")
        transaction = self._transaction()
        assert transaction.command_snapshot is not None
        reason = self._prewrite_rejection(transaction, current, gates, now=now)
        if reason is not None:
            blocked = replace(
                transaction,
                state=ActiveState.BLOCKED,
                owner=ExecutionOwner.NONE,
                prewrite_snapshot=current,
                reason=reason,
                rollback_status=RollbackStatus.NOT_REQUIRED,
            )
            self._publish_blocked(blocked, reason)
            return None
        expected = ExpectedReadback.from_command(current, transaction.intent.command)
        prepared = replace(
            transaction,
            prewrite_snapshot=current,
            expected_readback=expected,
            reason=ExecutionReason.COMMAND_READY,
        )
        self._record = replace(
            self._record,
            transaction=prepared,
            reason=ExecutionReason.COMMAND_READY,
            revision=self._record.revision + 1,
        )
        self._validate_record(self._record)
        return CommandEnvelope(
            transaction_id=prepared.transaction_id,
            owner=prepared.owner,
            action=prepared.intent.action,
            command=prepared.intent.command,
            expected_readback=expected,
        )

    def reprepare_unsent_start(
        self,
        intent: ActuatorIntent,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        rejection: AtomicWriteNotQueued,
        now: datetime,
    ) -> CommandEnvelope | None:
        """Rebuild one initial RCE/PV/tariff command after a proven prequeue rejection.

        The controller bounds this to one retry in the original dispatch budget.
        No successful or uncertain transport may call this method.
        """

        self._require_state(ActiveState.STARTING)
        _require_time(now, "now")
        transaction = self._transaction()
        before = transaction.prewrite_snapshot
        if (
            not isinstance(rejection, AtomicWriteNotQueued)
            or rejection.reason not in {
                "stale_snapshot", "stale_snapshot_generation",
            }
            or transaction.owner not in {ExecutionOwner.RCE, ExecutionOwner.TARIFF}
            or transaction.intent.action not in {
                ExecutionAction.RCE_EXPORT,
                ExecutionAction.PV_CHARGE_HOLD,
                ExecutionAction.TARIFF_GRID_SUPPORT,
                ExecutionAction.TARIFF_BATTERY_CHARGE,
                ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
            }
            or intent.command.families != frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
            or intent.policy is not transaction.owner
            or intent.action is not transaction.intent.action
            or intent.deadline != transaction.deadline
            or transaction.command_sent_at is not None
            or before is None
            or not _generation_is_newer(current.ems_generation, before.ems_generation)
            or not 0.0 <= (now - transaction.started_at).total_seconds()
            < COMMAND_DISPATCH_TIMEOUT_SECONDS
            or not _unchanged_fields_preserved(intent, current)
            or transaction.command_snapshot is None
            or not transaction.command_snapshot.values_match(
                current, required_families=intent.command.families
            )
        ):
            self.abort_prewrite(reason=ExecutionReason.SNAPSHOT_CHANGED)
            return None
        # Preserve the original restore baseline byte/value-identically. Only
        # the complete prewrite/ACK snapshot may advance after a proven reject.
        proposed = replace(
            transaction,
            intent=intent,
            prewrite_snapshot=None,
            expected_readback=ExpectedReadback.from_command(current, intent.command),
        )
        reason = self._prewrite_rejection(proposed, current, gates, now=now)
        if reason is not None:
            self.abort_prewrite(reason=reason)
            return None
        self._record = replace(
            self._record,
            transaction=proposed,
            revision=self._record.revision + 1,
        )
        self._validate_record(self._record)
        return self.prepare_command(current, gates, now=now)

    def command_prewrite_ready(
        self,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> bool:
        """Re-attest a prepared command without mutating its transaction."""

        self._require_state(ActiveState.STARTING)
        _require_time(now, "now")
        return self._prewrite_rejection(
            self._transaction(),
            current,
            gates,
            now=now,
        ) is None

    def adopt_tariff_action_without_write(
        self,
        intent: ActuatorIntent,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        verification: PhysicalVerification,
        *,
        now: datetime,
    ) -> bool:
        """Update tariff meaning when the exact inverter command is unchanged."""
        self._require_state(ActiveState.EXECUTING)
        _require_time(now, "now")
        transaction = self._transaction()
        if (
            transaction.owner is not ExecutionOwner.TARIFF
            or intent.policy is not ExecutionOwner.TARIFF
            or _retarget_mode(transaction.intent) is not EmsMode.GRID_CHARGE
            or _retarget_mode(intent) is not EmsMode.GRID_CHARGE
            or intent.command != transaction.intent.command
            or intent.deadline != transaction.deadline
        ):
            raise ValueError("semantic tariff update must preserve command, owner and deadline")
        if not self.check_continuation_authority(current, gates, authorization_current=True, now=now):
            return False
        if (
            verification.transaction_id != transaction.transaction_id
            or verification.action is not intent.action
            or verification.status is not VerificationStatus.CONFIRMED
            or transaction.command_sent_at is None
            or verification.observed_at <= transaction.command_sent_at
            or not -MAX_FUTURE_SKEW_SECONDS <= (now-verification.observed_at).total_seconds() <= TARIFF_PHYSICAL_MAX_AGE_SECONDS
        ):
            self.request_stop(reason=ExecutionReason.PHYSICAL_UNAVAILABLE)
            return False
        self._publish(
            ActiveState.EXECUTING,
            owner=transaction.owner,
            transaction=replace(transaction,intent=intent,physical_verification=verification),
            reason=ExecutionReason.CONTINUATION_CONFIRMED,
        )
        return True

    def prepare_same_owner_retarget(
        self,
        intent: ActuatorIntent,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> CommandEnvelope | None:
        """Prepare one same-run RCE or tariff update without neutral restoration.

        The original ``command_snapshot`` remains the eventual rollback target.
        ``current`` must still physically confirm the previous command, so a
        changed planner target can replace it directly while ownership remains
        continuous. Direct-register and cross-policy handoffs keep the normal
        stop/restore/reselect contract.
        """

        self._require_state(ActiveState.EXECUTING)
        _require_time(now, "now")
        transaction = self._transaction()
        if (
            transaction.owner is not transaction.intent.policy
            or intent.policy is not transaction.owner
            or _retarget_mode(transaction.intent) is None
            or _retarget_mode(intent) is not _retarget_mode(transaction.intent)
        ):
            raise ValueError("retarget requires the same supported automatic owner and mode")
        complete_block_only = frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
        if (
            transaction.intent.command.families != complete_block_only
            or intent.command.families != complete_block_only
        ):
            raise ValueError("retarget requires one complete EMS block")
        if (
            intent.candidate_revision == transaction.intent.candidate_revision
            or intent.command == transaction.intent.command
        ):
            raise ValueError("retarget requires a changed physical command identity")
        old_block = transaction.intent.command.ems_block
        new_block = intent.command.ems_block
        if (
            old_block is None
            or new_block is None
            or old_block.mode is not new_block.mode
        ):
            raise ValueError("retarget requires the same mode before and after")
        if intent.deadline != transaction.deadline:
            raise ValueError("retarget cannot replace or extend the current deadline")
        if now >= transaction.deadline:
            self.request_stop(reason=ExecutionReason.DEADLINE_REACHED)
            return None
        if not self._record.starts_allowed:
            self.request_stop(reason=ExecutionReason.STARTS_DISABLED)
            return None
        if _automatic_owner(intent.policy) and not self._record.automatic_policies_enabled:
            self.request_stop(reason=ExecutionReason.POLICY_DISABLED)
            return None
        if not self.check_continuation_authority(
            current,
            gates,
            authorization_current=True,
            now=now,
        ):
            return None
        transaction = self._transaction()
        previous = transaction.physical_verification
        if (
            previous is None
            or previous.status is not VerificationStatus.CONFIRMED
            or previous.observed_at > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
            or (now - previous.observed_at).total_seconds() > (
                TARIFF_PHYSICAL_MAX_AGE_SECONDS
                if transaction.owner is ExecutionOwner.TARIFF
                else MAX_READBACK_AGE_SECONDS
            )
        ):
            self.request_stop(reason=ExecutionReason.PHYSICAL_UNAVAILABLE)
            return None
        required = _required_readback_families(intent.command, intent.action)
        if current.freshness_errors(now, required_families=required):
            self.request_stop(reason=ExecutionReason.STALE_INPUTS)
            return None
        reason = _gate_reason(intent, gates, expected_owner=transaction.owner)
        if reason is None:
            reason = _settings_reason(intent, current)
        if reason is None and not _unchanged_fields_preserved(intent, current):
            reason = ExecutionReason.SNAPSHOT_INVALID
        if reason is not None:
            self.request_stop(reason=reason)
            return None
        expected = ExpectedReadback.from_command(current, intent.command)
        prepared = replace(
            transaction,
            state=ActiveState.RETARGETING,
            intent=intent,
            prewrite_snapshot=current,
            expected_readback=expected,
            command_sent_at=None,
            readback_result=VerificationStatus.PENDING,
            physical_verification=None,
            rollback_status=RollbackStatus.PENDING,
            rollback_result=VerificationStatus.PENDING,
            restore_command=None,
            restore_snapshot=None,
            restore_expected_readback=None,
            restore_sent_at=None,
            restore_attempts=0,
            reason=ExecutionReason.COMMAND_READY,
        )
        self._publish(
            ActiveState.RETARGETING,
            owner=transaction.owner,
            transaction=prepared,
            reason=ExecutionReason.COMMAND_READY,
        )
        return CommandEnvelope(
            transaction_id=prepared.transaction_id,
            owner=prepared.owner,
            action=prepared.intent.action,
            command=prepared.intent.command,
            expected_readback=expected,
        )

    def retarget_prewrite_ready(
        self,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> bool:
        """Re-attest a persisted same-owner retarget before transport."""

        self._require_state(ActiveState.RETARGETING)
        _require_time(now, "now")
        transaction = self._transaction()
        snapshot = transaction.prewrite_snapshot
        expected = transaction.expected_readback
        if snapshot is None or expected is None or transaction.command_sent_at is not None:
            return False
        complete_block_only = frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
        if transaction.intent.command.families != complete_block_only:
            return False
        required = _required_readback_families(
            transaction.intent.command,
            transaction.intent.action,
        )
        if current.freshness_errors(now, required_families=required):
            return False
        if _gate_reason(
            transaction.intent,
            gates,
            expected_owner=transaction.owner,
        ) is not None:
            return False
        if _settings_reason(transaction.intent, current) is not None:
            return False
        if not _unchanged_fields_preserved(transaction.intent, current):
            return False
        return bool(
            expected == ExpectedReadback.from_command(snapshot, transaction.intent.command)
            and snapshot.values_match(current, required_families=required)
            and _snapshot_generations_match(
                snapshot,
                current,
                required_families=transaction.intent.command.families,
            )
        )

    def prepare_same_owner_rearm(
        self,
        intent: ActuatorIntent,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> CommandEnvelope | None:
        """Re-arm an unchanged physical block after a proven negative arm.

        The controller may call this only after it has discarded an unsent
        successor and re-attested the physical predecessor.  Re-sending the
        already physical block is required because the ESP rejected arm has
        invalidated the previous lease identity; it does not extend the
        transaction deadline or replace the original restore baseline.
        """

        self._require_state(ActiveState.EXECUTING)
        _require_time(now, "now")
        transaction = self._transaction()
        complete_block_only = frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
        if (
            transaction.owner is not transaction.intent.policy
            or intent.policy is not transaction.owner
            or _retarget_mode(transaction.intent) is None
            or _retarget_mode(intent) is not _retarget_mode(transaction.intent)
            or transaction.intent.command.families != complete_block_only
            or intent.command.families != complete_block_only
            or intent.command != transaction.intent.command
            or intent.deadline != transaction.deadline
        ):
            raise ValueError("same-owner rearm must preserve block owner mode and deadline")
        if now >= transaction.deadline:
            self.request_stop(reason=ExecutionReason.DEADLINE_REACHED)
            return None
        if not self.check_continuation_authority(
            current,
            gates,
            authorization_current=True,
            now=now,
        ):
            return None
        transaction = self._transaction()
        required = _required_readback_families(intent.command, intent.action)
        if current.freshness_errors(now, required_families=required):
            self.request_stop(reason=ExecutionReason.STALE_INPUTS)
            return None
        reason = _gate_reason(intent, gates, expected_owner=transaction.owner)
        if reason is None:
            reason = _settings_reason(intent, current)
        if reason is None and not _unchanged_fields_preserved(intent, current):
            reason = ExecutionReason.SNAPSHOT_INVALID
        if reason is not None:
            self.request_stop(reason=reason)
            return None
        expected = ExpectedReadback.from_command(current, intent.command)
        prepared = replace(
            transaction,
            state=ActiveState.RETARGETING,
            intent=intent,
            prewrite_snapshot=current,
            expected_readback=expected,
            command_sent_at=None,
            readback_result=VerificationStatus.PENDING,
            physical_verification=None,
            rollback_status=RollbackStatus.PENDING,
            rollback_result=VerificationStatus.PENDING,
            restore_command=None,
            restore_snapshot=None,
            restore_expected_readback=None,
            restore_sent_at=None,
            restore_attempts=0,
            reason=ExecutionReason.COMMAND_READY,
        )
        self._publish(
            ActiveState.RETARGETING,
            owner=transaction.owner,
            transaction=prepared,
            reason=ExecutionReason.COMMAND_READY,
        )
        return CommandEnvelope(
            transaction_id=prepared.transaction_id,
            owner=prepared.owner,
            action=prepared.intent.action,
            command=prepared.intent.command,
            expected_readback=expected,
        )

    def cancel_unsent_same_owner_retarget(
        self,
        predecessor: TransactionRecord,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> bool:
        """Discard only an unsent successor and resume its proven predecessor."""

        self._require_state(ActiveState.RETARGETING)
        _require_time(now, "now")
        prepared = self._transaction()
        complete_block_only = frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
        if (
            prepared.command_sent_at is not None
            or predecessor.state is not ActiveState.EXECUTING
            or predecessor.transaction_id != prepared.transaction_id
            or predecessor.owner is not prepared.owner
            or predecessor.intent.policy is not predecessor.owner
            or _retarget_mode(predecessor.intent) is None
            or predecessor.intent.command.families != complete_block_only
            or prepared.intent.policy is not predecessor.owner
            or _retarget_mode(prepared.intent) is not _retarget_mode(predecessor.intent)
            or prepared.intent.command.families != complete_block_only
            or predecessor.deadline != prepared.deadline
            or predecessor.command_snapshot != prepared.command_snapshot
            or predecessor.expected_readback is None
            or predecessor.command_sent_at is None
            or predecessor.readback_result is not VerificationStatus.CONFIRMED
        ):
            raise ValueError("retarget predecessor evidence is not exact")
        if now >= predecessor.deadline:
            self.request_stop(reason=ExecutionReason.DEADLINE_REACHED)
            return False
        if not self._record.starts_allowed:
            self.request_stop(reason=ExecutionReason.STARTS_DISABLED)
            return False
        if not self._record.automatic_policies_enabled:
            self.request_stop(reason=ExecutionReason.POLICY_DISABLED)
            return False
        required = _required_readback_families(
            predecessor.intent.command,
            predecessor.intent.action,
        )
        if current.freshness_errors(now, required_families=required):
            self.request_stop(reason=ExecutionReason.STALE_INPUTS)
            return False
        reason = _gate_reason(
            predecessor.intent,
            gates,
            expected_owner=predecessor.owner,
        )
        if reason is None:
            reason = _settings_reason(predecessor.intent, current)
        if reason is not None:
            self.request_stop(reason=reason)
            return False
        readback = predecessor.expected_readback.compare(
            current,
            now=now,
            not_before=predecessor.command_sent_at,
            required_families=required,
        )
        if readback is not ReadbackVerdict.MATCH:
            self.request_stop(
                reason=(
                    ExecutionReason.READBACK_MISMATCH
                    if readback is ReadbackVerdict.MISMATCH
                    else ExecutionReason.READBACK_PENDING
                )
            )
            return False
        proof = predecessor.physical_verification
        if (
            proof is None
            or proof.status is not VerificationStatus.CONFIRMED
            or proof.transaction_id != predecessor.transaction_id
            or proof.action is not predecessor.intent.action
            or proof.observed_at > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
            or (now - proof.observed_at).total_seconds() > (
                TARIFF_PHYSICAL_MAX_AGE_SECONDS
                if predecessor.owner is ExecutionOwner.TARIFF
                else MAX_READBACK_AGE_SECONDS
            )
        ):
            self.request_stop(reason=ExecutionReason.PHYSICAL_UNAVAILABLE)
            return False
        resumed = replace(
            predecessor,
            state=ActiveState.EXECUTING,
            reason=ExecutionReason.CONTINUATION_CONFIRMED,
        )
        self._publish(
            ActiveState.EXECUTING,
            owner=predecessor.owner,
            transaction=resumed,
            reason=ExecutionReason.CONTINUATION_CONFIRMED,
        )
        return True

    def defer_pv_start_before_dispatch(self, *, dispatched_count: int) -> ExecutorRecord:
        """Discard only a prepared PV envelope attested unsent by its caller.

        The controller calls this after its own pre-dispatch guard rejected
        before invoking transport. A missing sent timestamp alone is not this
        proof. Retain the original transaction, baseline and fixed deadlines;
        the next callback must pass ordinary prepare/authorization again.
        """
        self._require_state(ActiveState.STARTING)
        transaction = self._transaction()
        if (
            type(dispatched_count) is not int or dispatched_count != 0
            or transaction.intent.action is not ExecutionAction.PV_CHARGE_HOLD
            or transaction.owner is not ExecutionOwner.RCE
            or transaction.reason is not ExecutionReason.COMMAND_READY
            or transaction.command_sent_at is not None
            or transaction.lease_identity is not None
            or transaction.command_snapshot is None
            or transaction.prewrite_snapshot is None
        ):
            raise ValueError("PV preparation has no zero-dispatch attestation")
        deferred = replace(
            transaction,
            prewrite_snapshot=None,
            expected_readback=ExpectedReadback.from_command(
                transaction.command_snapshot, transaction.intent.command),
            reason=ExecutionReason.OWNER_ACQUIRED,
        )
        self._record = replace(self._record, transaction=deferred,
            reason=deferred.reason, revision=self._record.revision + 1)
        self._validate_record(self._record)
        return self._record

    def abort_prewrite(
        self,
        *,
        reason: ExecutionReason = ExecutionReason.AUTHORIZATION_LOST,
    ) -> ExecutorRecord:
        """Release an acquired owner when no command reached transport."""

        self._require_state(ActiveState.STARTING)
        transaction = self._transaction()
        blocked = replace(
            transaction,
            state=ActiveState.BLOCKED,
            owner=ExecutionOwner.NONE,
            reason=reason,
            rollback_status=RollbackStatus.NOT_REQUIRED,
        )
        return self._publish_blocked(blocked, reason)

    def mark_command_sent(
        self,
        envelope: CommandEnvelope,
        *,
        sent_at: datetime,
        lease_identity: tuple[str, str, int] | None = None,
    ) -> ExecutorRecord:
        self._require_state(ActiveState.STARTING)
        _require_time(sent_at, "command sent_at")
        transaction = self._transaction()
        if transaction.prewrite_snapshot is None or transaction.expected_readback is None:
            raise RuntimeError("command was not prepared")
        if (
            envelope.transaction_id != transaction.transaction_id
            or envelope.owner is not transaction.owner
            or envelope.action is not transaction.intent.action
            or envelope.command != transaction.intent.command
            or envelope.expected_readback != transaction.expected_readback
        ):
            raise ValueError("command envelope does not belong to the transaction")
        assert transaction.prewrite_snapshot is not None
        required = _required_readback_families(
            transaction.intent.command,
            transaction.intent.action,
        )
        if sent_at < _latest_observed_at(
            transaction.prewrite_snapshot,
            required_families=required,
        ):
            raise ValueError("command cannot precede its physical snapshot")
        if sent_at >= transaction.deadline:
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                command_sent_at=sent_at,
                lease_identity=lease_identity,
                reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
                rollback_status=RollbackStatus.PENDING,
            )
            return self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
            )
        waiting = replace(
            transaction,
            state=ActiveState.WAITING_READBACK,
            command_sent_at=sent_at,
            lease_identity=lease_identity,
            reason=ExecutionReason.WAITING_READBACK,
        )
        return self._publish(
            ActiveState.WAITING_READBACK,
            owner=transaction.owner,
            transaction=waiting,
            reason=ExecutionReason.WAITING_READBACK,
        )

    def mark_retarget_sent(
        self,
        envelope: CommandEnvelope,
        *,
        sent_at: datetime,
        lease_identity: tuple[str, str, int] | None = None,
    ) -> ExecutorRecord:
        """Record dispatch of a prepared same-window RCE retarget.

        Retargeting deliberately remains a distinct durable state until the
        newer FC03 generation and physical effect confirm the replacement.
        """

        self._require_state(ActiveState.RETARGETING)
        _require_time(sent_at, "retarget command sent_at")
        transaction = self._transaction()
        if transaction.prewrite_snapshot is None or transaction.expected_readback is None:
            raise RuntimeError("retarget command was not prepared")
        if transaction.command_sent_at is not None:
            raise RuntimeError("retarget command was already dispatched")
        if (
            envelope.transaction_id != transaction.transaction_id
            or envelope.owner is not transaction.owner
            or envelope.action is not transaction.intent.action
            or envelope.command != transaction.intent.command
            or envelope.expected_readback != transaction.expected_readback
        ):
            raise ValueError("retarget envelope does not belong to the transaction")
        required = _required_readback_families(
            transaction.intent.command,
            transaction.intent.action,
        )
        if sent_at < _latest_observed_at(
            transaction.prewrite_snapshot,
            required_families=required,
        ):
            raise ValueError("retarget command cannot precede its physical snapshot")
        if sent_at >= transaction.deadline:
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                command_sent_at=sent_at,
                lease_identity=lease_identity,
                reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
                rollback_status=RollbackStatus.PENDING,
            )
            return self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
            )
        waiting = replace(
            transaction,
            command_sent_at=sent_at,
            lease_identity=lease_identity,
            reason=ExecutionReason.WAITING_READBACK,
        )
        self._record = replace(
            self._record,
            transaction=waiting,
            reason=ExecutionReason.WAITING_READBACK,
            revision=self._record.revision + 1,
        )
        self._validate_record(self._record)
        return self._record

    def mark_command_outcome_unknown(self) -> ExecutorRecord:
        self._require_state(
            ActiveState.STARTING,
            ActiveState.WAITING_READBACK,
            ActiveState.RETARGETING,
        )
        transaction = self._transaction()
        fault = replace(
            transaction,
            state=ActiveState.FAULT,
            reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
            rollback_status=RollbackStatus.PENDING,
        )
        return self._publish(
            ActiveState.FAULT,
            owner=transaction.owner,
            transaction=fault,
            reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
        )

    def observe_readback(
        self,
        readback: SettingsSnapshot,
        *,
        now: datetime,
    ) -> ReadbackVerdict:
        self._require_state(ActiveState.WAITING_READBACK, ActiveState.RETARGETING)
        _require_time(now, "now")
        transaction = self._transaction()
        if transaction.command_sent_at is None or transaction.expected_readback is None:
            raise RuntimeError("transaction has no dispatched command")
        if now >= transaction.deadline or _timed_out(
            now,
            transaction.command_sent_at,
            command_wait_timeout(transaction),
        ):
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=ExecutionReason.DEADLINE_REACHED,
                rollback_status=RollbackStatus.PENDING,
            )
            self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=ExecutionReason.DEADLINE_REACHED,
            )
            return ReadbackVerdict.MISMATCH
        verdict = transaction.expected_readback.compare(
            readback,
            now=now,
            not_before=transaction.command_sent_at,
            required_families=_required_readback_families(
                transaction.intent.command,
                transaction.intent.action,
            ),
        )
        # FC16 is queued by the device. The first newer FC03 can still
        # contain exactly the prewrite block. It is neither an ACK nor a
        # contradiction until the existing deadline; never renew that timeout.
        if (
            verdict is ReadbackVerdict.MISMATCH
            and transaction.readback_result is not VerificationStatus.CONFIRMED
            and transaction.intent.command.families
            == frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
            and transaction.prewrite_snapshot is not None
            and _snapshot_generations_match_expected_base(
                transaction.prewrite_snapshot, transaction.expected_readback
            )
            and transaction.prewrite_snapshot.values_match(
                readback,
                required_families=_required_readback_families(
                    transaction.intent.command, transaction.intent.action
                ),
            )
        ):
            verdict = ReadbackVerdict.PENDING
        if verdict is ReadbackVerdict.PENDING:
            if transaction.readback_result is VerificationStatus.CONFIRMED:
                # A missing fresh sample does not erase the historical ACK.
                # Return PENDING without granting physical verification or
                # resetting the fixed timeout or the durable physical phase.
                return verdict
            pending = replace(
                transaction,
                reason=ExecutionReason.READBACK_PENDING,
                readback_result=VerificationStatus.PENDING,
            )
            if (
                pending == transaction
                and self._record.reason is ExecutionReason.READBACK_PENDING
            ):
                return verdict
            self._record = replace(
                self._record,
                transaction=pending,
                reason=ExecutionReason.READBACK_PENDING,
                revision=self._record.revision + 1,
            )
            self._validate_record(self._record)
            return verdict
        if verdict is ReadbackVerdict.MISMATCH:
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=ExecutionReason.READBACK_MISMATCH,
                readback_result=VerificationStatus.CONTRADICTED,
                rollback_status=RollbackStatus.PENDING,
            )
            self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=ExecutionReason.READBACK_MISMATCH,
            )
            return verdict
        if transaction.readback_result is VerificationStatus.CONFIRMED:
            # Repeated matching FC03 must preserve PHYSICAL_PENDING. Replacing
            # its reason with WAITING_PHYSICAL makes a retarget non-canonical.
            return verdict
        confirmed = replace(
            transaction,
            reason=ExecutionReason.WAITING_PHYSICAL,
            readback_result=VerificationStatus.CONFIRMED,
        )
        if (
            confirmed == transaction
            and self._record.reason is ExecutionReason.WAITING_PHYSICAL
        ):
            return verdict
        self._record = replace(
            self._record,
            transaction=confirmed,
            reason=ExecutionReason.WAITING_PHYSICAL,
            revision=self._record.revision + 1,
        )
        self._validate_record(self._record)
        return verdict

    def observe_physical_verification(
        self,
        verification: PhysicalVerification,
        *,
        now: datetime,
    ) -> ExecutorRecord:
        self._require_state(ActiveState.WAITING_READBACK, ActiveState.RETARGETING)
        _require_time(now, "now")
        transaction = self._transaction()
        if transaction.readback_result is not VerificationStatus.CONFIRMED:
            raise RuntimeError("physical verification cannot precede readback")
        if (
            transaction.intent.action
            in {
                ExecutionAction.TARIFF_BATTERY_CHARGE,
                ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
            }
            and (
                transaction.owner is not ExecutionOwner.TARIFF
                or self._record.owner is not ExecutionOwner.TARIFF
                or transaction.intent.command.ems_block is None
                or transaction.intent.command.ems_block.mode
                is not EmsMode.GRID_CHARGE
            )
        ):
            raise RuntimeError(
                "tariff charge physical proof requires tariff ownership and "
                "acknowledged Grid Charge command"
            )
        if (
            verification.transaction_id != transaction.transaction_id
            or verification.action is not transaction.intent.action
            or transaction.command_sent_at is None
            or verification.observed_at <= transaction.command_sent_at
            or verification.observed_at > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
        ):
            raise ValueError("physical verification is not correlated to the transaction")
        if now >= transaction.deadline or _timed_out(
            now,
            transaction.command_sent_at,
            command_wait_timeout(transaction),
        ):
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                physical_verification=verification,
                reason=ExecutionReason.DEADLINE_REACHED,
                rollback_status=RollbackStatus.PENDING,
            )
            return self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=ExecutionReason.DEADLINE_REACHED,
            )
        verification_age = (now - verification.observed_at).total_seconds()
        stale_initial_rce_pending = bool(
            self._record.state is ActiveState.WAITING_READBACK
            and transaction.owner is ExecutionOwner.RCE
            and transaction.intent.action is ExecutionAction.RCE_EXPORT
            and verification.status is VerificationStatus.PENDING
        )
        if (
            verification_age > (
                TARIFF_PHYSICAL_MAX_AGE_SECONDS
                if transaction.owner is ExecutionOwner.TARIFF
                else MAX_READBACK_AGE_SECONDS
            )
            and not stale_initial_rce_pending
        ):
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                physical_verification=verification,
                reason=ExecutionReason.PHYSICAL_UNAVAILABLE,
                rollback_status=RollbackStatus.PENDING,
            )
            return self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=ExecutionReason.PHYSICAL_UNAVAILABLE,
            )
        if verification.status is VerificationStatus.PENDING:
            pending = replace(
                transaction,
                physical_verification=verification,
                reason=ExecutionReason.PHYSICAL_PENDING,
            )
            if (
                pending == transaction
                and self._record.reason is ExecutionReason.PHYSICAL_PENDING
            ):
                return self._record
            self._record = replace(
                self._record,
                transaction=pending,
                reason=ExecutionReason.PHYSICAL_PENDING,
                revision=self._record.revision + 1,
            )
            self._validate_record(self._record)
            return self._record
        if verification.status is not VerificationStatus.CONFIRMED:
            reason = (
                ExecutionReason.PHYSICAL_CONTRADICTION
                if verification.status is VerificationStatus.CONTRADICTED
                else ExecutionReason.PHYSICAL_UNAVAILABLE
            )
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                physical_verification=verification,
                reason=reason,
                rollback_status=RollbackStatus.PENDING,
            )
            return self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=reason,
            )
        executing = replace(
            transaction,
            state=ActiveState.EXECUTING,
            physical_verification=verification,
            reason=ExecutionReason.PHYSICALLY_CONFIRMED,
        )
        return self._publish(
            ActiveState.EXECUTING,
            owner=transaction.owner,
            transaction=executing,
            reason=ExecutionReason.PHYSICALLY_CONFIRMED,
        )

    def waiting_same_owner_replan_hold_ready(
        self,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> bool:
        """Validate a bounded planner-settling hold without renewing its lease.

        This exception is limited to dispatched RCE export or tariff Mode 4.
        It never changes ``command_sent_at`` or ``deadline``;
        the original 90-second acknowledgement timeout and run boundary remain
        authoritative.  While no newer FC03 generation exists, the physical
        snapshot must still be exactly the command's pre-write snapshot.
        """

        self._require_state(ActiveState.WAITING_READBACK, ActiveState.RETARGETING)
        _require_time(now, "now")
        transaction = self._transaction()
        complete_block_only = frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
        if (
            transaction.owner is not transaction.intent.policy
            or (_retarget_mode(transaction.intent) is None and not (
                transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD
                and self._record.state is ActiveState.WAITING_READBACK))
            or transaction.intent.command.families != complete_block_only
            or transaction.command_sent_at is None
            or transaction.prewrite_snapshot is None
            or transaction.expected_readback is None
        ):
            self.request_stop(reason=ExecutionReason.AUTHORIZATION_LOST)
            return False
        if (
            now >= transaction.deadline
            or _timed_out(
                now,
                transaction.command_sent_at,
                command_wait_timeout(transaction),
            )
        ):
            self.check_deadline(now=now)
            return False
        if not self._record.starts_allowed:
            self.request_stop(reason=ExecutionReason.STARTS_DISABLED)
            return False
        if not self._record.automatic_policies_enabled:
            self.request_stop(reason=ExecutionReason.POLICY_DISABLED)
            return False
        required = _required_readback_families(
            transaction.intent.command,
            transaction.intent.action,
        )
        if current.freshness_errors(now, required_families=required):
            self.request_stop(reason=ExecutionReason.STALE_INPUTS)
            return False
        reason = _gate_reason(
            transaction.intent,
            gates,
            expected_owner=transaction.owner,
        )
        if reason is None:
            reason = _settings_reason(transaction.intent, current)
        if reason is not None:
            self.request_stop(reason=reason)
            return False
        verdict = transaction.expected_readback.compare(
            current,
            now=now,
            not_before=transaction.command_sent_at,
            required_families=required,
        )
        if verdict is not ReadbackVerdict.PENDING:
            return True
        if (
            not transaction.prewrite_snapshot.values_match(
                current,
                required_families=required,
            )
            or not _snapshot_generations_match(
                transaction.prewrite_snapshot,
                current,
                required_families=transaction.intent.command.families,
            )
        ):
            self.request_stop(reason=ExecutionReason.SNAPSHOT_CHANGED)
            return False
        return True

    def check_continuation(
        self,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        verification: PhysicalVerification,
        *,
        authorization_current: bool,
        now: datetime,
        maximum_readback_age_seconds: float = MAX_READBACK_AGE_SECONDS,
    ) -> ExecutorRecord:
        """Fail closed unless authority, settings and physical flow remain current."""

        if not self.check_continuation_authority(
            current,
            gates,
            authorization_current=authorization_current,
            now=now,
            maximum_readback_age_seconds=maximum_readback_age_seconds,
        ):
            return self._record
        transaction = self._transaction()
        previous = transaction.physical_verification
        physical_maximum_age_seconds = (
            RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS
            if (
                transaction.owner is ExecutionOwner.RCE
                and transaction.intent.action is ExecutionAction.RCE_EXPORT
                and previous is not None
                and previous.status is VerificationStatus.CONFIRMED
            )
            else TARIFF_PHYSICAL_MAX_AGE_SECONDS
            if transaction.owner is ExecutionOwner.TARIFF
            else MAX_READBACK_AGE_SECONDS
        )
        if (
            previous is None
            or verification.transaction_id != transaction.transaction_id
            or verification.action is not transaction.intent.action
            or verification.observed_at <= previous.observed_at
            or verification.observed_at > now + timedelta(seconds=MAX_FUTURE_SKEW_SECONDS)
            or (now - verification.observed_at).total_seconds()
            > physical_maximum_age_seconds
        ):
            return self.request_stop(reason=ExecutionReason.PHYSICAL_UNAVAILABLE)
        if verification.status is not VerificationStatus.CONFIRMED:
            reason = (
                ExecutionReason.PHYSICAL_CONTRADICTION
                if verification.status is VerificationStatus.CONTRADICTED
                else ExecutionReason.PHYSICAL_UNAVAILABLE
                if verification.status is VerificationStatus.UNAVAILABLE
                else ExecutionReason.PHYSICAL_PENDING
            )
            return self.request_stop(reason=reason, physical_verification=verification)
        continued = replace(
            transaction,
            physical_verification=verification,
            reason=ExecutionReason.CONTINUATION_CONFIRMED,
        )
        self._record = replace(
            self._record,
            transaction=continued,
            reason=ExecutionReason.CONTINUATION_CONFIRMED,
            revision=self._record.revision + 1,
        )
        self._validate_record(self._record)
        return self._record

    def check_continuation_authority(
        self,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        authorization_current: bool,
        now: datetime,
        maximum_readback_age_seconds: float = MAX_READBACK_AGE_SECONDS,
    ) -> bool:
        """Revalidate non-flow authority when a power sample has not advanced."""

        self._require_state(ActiveState.EXECUTING)
        _require_time(now, "now")
        self.last_continuation_input_failure = None
        if type(authorization_current) is not bool:
            raise ValueError("continuation authorization must be boolean")
        transaction = self._transaction()
        if now >= transaction.deadline:
            self.request_stop(reason=ExecutionReason.DEADLINE_REACHED)
            return False
        if not authorization_current:
            self.request_stop(reason=ExecutionReason.AUTHORIZATION_LOST)
            return False
        if maximum_readback_age_seconds > MAX_READBACK_AGE_SECONDS:
            proof = transaction.physical_verification
            extended_rce_continuation = bool(
                transaction.owner is ExecutionOwner.RCE
                and transaction.intent.action is ExecutionAction.RCE_EXPORT
                and proof is not None
                and proof.status is VerificationStatus.CONFIRMED
            )
            maximum_readback_age_seconds = (
                min(
                    maximum_readback_age_seconds,
                    RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
                )
                if extended_rce_continuation
                else MAX_READBACK_AGE_SECONDS
            )
        required = _required_readback_families(
            transaction.intent.command,
            transaction.intent.action,
        )
        errors = current.freshness_errors(
            now,
            maximum_age_seconds=maximum_readback_age_seconds,
            required_families=required,
        )
        expired = (
            maximum_readback_age_seconds > MAX_READBACK_AGE_SECONDS
            and (now - current.ems_observed_at).total_seconds()
            >= maximum_readback_age_seconds
        )
        if errors or expired:
            self._capture_continuation_input_failure(
                current, now, required, maximum_readback_age_seconds,
                errors or ("ems_expired",),
            )
            self.request_stop(reason=ExecutionReason.STALE_INPUTS)
            return False
        reason = _gate_reason(
            transaction.intent,
            gates,
            expected_owner=transaction.owner,
        )
        if reason is None:
            reason = _settings_reason(transaction.intent, current)
        if reason is not None:
            if reason is ExecutionReason.STALE_INPUTS:
                self._capture_continuation_input_failure(
                    current, now, required, maximum_readback_age_seconds,
                    ("inputs_fresh_false",),
                )
            self.request_stop(reason=reason)
            return False
        if transaction.expected_readback is None or transaction.command_sent_at is None:
            raise RuntimeError("executing transaction has no command evidence")
        readback = transaction.expected_readback.compare(
            current,
            now=now,
            not_before=transaction.command_sent_at,
            required_families=required,
            maximum_readback_age_seconds=maximum_readback_age_seconds,
        )
        if readback is ReadbackVerdict.MISMATCH:
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=ExecutionReason.READBACK_MISMATCH,
                readback_result=VerificationStatus.CONTRADICTED,
                rollback_status=RollbackStatus.PENDING,
            )
            self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=ExecutionReason.READBACK_MISMATCH,
            )
            return False
        if readback is ReadbackVerdict.PENDING:
            self.request_stop(reason=ExecutionReason.READBACK_PENDING)
            return False
        return True

    def _capture_continuation_input_failure(
        self, current: SettingsSnapshot, now: datetime,
        required: Iterable[AtomicWriteFamily], maximum_age: float,
        errors: tuple[str, ...],
    ) -> None:
        """Keep the exact failed check, once per STOP, outside execution state."""
        cohorts = {}
        for name, needed, observed, coherent, generation, limit in (
            ("ems", True, current.ems_observed_at, current.ems_coherent,
             current.ems_generation, maximum_age),
            ("gcf", AtomicWriteFamily.GCF_EXPORT_LIMIT in required,
             current.gcf_observed_at, current.gcf_coherent,
             current.gcf_generation, SETTINGS_READBACK_MAX_AGE_SECONDS),
            ("battery_306", AtomicWriteFamily.BATTERY_CHARGE_LIMIT in required,
             current.battery_observed_at, current.battery_coherent,
             current.battery_generation, SETTINGS_READBACK_MAX_AGE_SECONDS),
        ):
            cohorts[name] = {
                "required": needed, "observed_at": observed.isoformat() if observed else None,
                "age_seconds": (now-observed).total_seconds() if observed else None,
                "maximum_age_seconds": limit, "coherent": coherent,
                "generation": generation,
            }
        self.last_continuation_input_failure = {
            "transaction_id": self._transaction().transaction_id,
            "checked_at": now.isoformat(), "stage": "continuation_authority",
            "errors": list(errors), "cohorts": cohorts, "cause_attested": True,
            "ems_expiry_exclusive": maximum_age > MAX_READBACK_AGE_SECONDS,
        }

    def request_stop(
        self,
        *,
        reason: ExecutionReason = ExecutionReason.STOP_REQUESTED,
        physical_verification: PhysicalVerification | None = None,
    ) -> ExecutorRecord:
        self._require_state(
            ActiveState.SELECTED,
            ActiveState.STARTING,
            ActiveState.WAITING_READBACK,
            ActiveState.EXECUTING,
            ActiveState.RETARGETING,
            ActiveState.STOPPING,
            ActiveState.RESTORING,
            ActiveState.BLOCKED,
            ActiveState.FAULT,
        )
        transaction = self._transaction()
        if physical_verification is not None:
            if (
                physical_verification.transaction_id != transaction.transaction_id
                or physical_verification.action is not transaction.intent.action
            ):
                raise ValueError("stop verification is not correlated to transaction")
            transaction = replace(
                transaction,
                physical_verification=physical_verification,
            )
        if transaction.owner is ExecutionOwner.NONE:
            finished = replace(
                transaction,
                state=ActiveState.IDLE,
                reason=ExecutionReason.RESTORE_NOT_REQUIRED,
                rollback_status=RollbackStatus.NOT_REQUIRED,
            )
            return self._finish_transaction(finished, ExecutionReason.RESTORE_NOT_REQUIRED)
        stopping = replace(
            transaction,
            state=ActiveState.STOPPING,
            reason=reason,
            interruption_reason=transaction.interruption_reason or reason,
            rollback_status=RollbackStatus.PENDING,
        )
        if self._record.state is ActiveState.STOPPING:
            self._record = replace(
                self._record,
                transaction=stopping,
                reason=reason,
                revision=self._record.revision + 1,
            )
            return self._record
        return self._publish(
            ActiveState.STOPPING,
            owner=transaction.owner,
            transaction=stopping,
            reason=reason,
        )

    def prepare_restore(
        self,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> CommandEnvelope | None:
        self._require_state(ActiveState.STOPPING, ActiveState.FAULT)
        _require_time(now, "now")
        transaction = self._transaction()
        snapshot = transaction.command_snapshot
        if snapshot is None or transaction.owner is ExecutionOwner.NONE:
            raise RuntimeError("transaction has no restorable owned snapshot")
        intent = transaction.intent
        master_stop = transaction.master_stop_requested
        off_grid = current.ems_block.mode is EmsMode.OFF_GRID
        restore_command = self._restore_command(transaction, off_grid=off_grid)
        restore_action = (
            ExecutionAction.MASTER_STOP if master_stop else intent.action
        )
        required = _required_readback_families(
            restore_command,
            restore_action,
            include_action_authority=False,
        )
        if (
            transaction.restore_not_queued_first_at is not None
            and (now - transaction.restore_not_queued_first_at).total_seconds()
            >= RESTORE_DISPATCH_TIMEOUT_SECONDS
        ):
            if (
                self._record.state is ActiveState.FAULT
                and self._record.reason is ExecutionReason.COMMAND_NOT_QUEUED
            ):
                return None
            held = replace(
                transaction,
                state=ActiveState.FAULT,
                rollback_status=RollbackStatus.PENDING,
                rollback_result=VerificationStatus.PENDING,
            )
            self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=held,
                reason=ExecutionReason.COMMAND_NOT_QUEUED,
                master_stop_failure=master_stop,
            )
            return None
        # A command timeout can be detected before the next physical FC03
        # cohort arrives. That stale transition sample is not proof that the
        # rollback failed: keep the original fault and pending ownership so a
        # later fresh readback can safely prepare the restore.
        if current.freshness_errors(now, required_families=required):
            return None
        reason = _command_gate_reason(
            restore_command,
            restore_action,
            gates,
            expected_owner=transaction.owner,
            master_stop=True,
        )
        if reason is not None:
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=reason,
                rollback_status=RollbackStatus.FAILED,
                rollback_result=VerificationStatus.UNAVAILABLE,
            )
            self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=reason,
                master_stop_failure=master_stop,
            )
            return None
        prepared_unsent = bool(
            transaction.restore_command is not None
            and transaction.restore_sent_at is None
        )
        retry_snapshot = (
            transaction.restore_snapshot
            if transaction.restore_attempts > 0
            else None
        )
        expected = ExpectedReadback.from_command(current, restore_command)
        if transaction.restore_not_queued_count > 0:
            rejected_snapshot = transaction.restore_snapshot
            if (
                rejected_snapshot is None
                or not _written_generations_advanced(
                    ExpectedReadback.from_command(
                        rejected_snapshot, restore_command
                    ),
                    current,
                    not_before=_latest_observed_at(
                        rejected_snapshot, required_families=required
                    ),
                )
            ):
                return None
        restored_values = replace(
            current,
            ems_block=restore_command.ems_block or current.ems_block,
            export_limit_percent_259=(
                restore_command.export_limit_percent_259
                if restore_command.export_limit_percent_259 is not None
                else current.export_limit_percent_259
            ),
            battery_max_charge_power_percent_306=(
                restore_command.battery_max_charge_power_percent_306
                if restore_command.battery_max_charge_power_percent_306 is not None
                else current.battery_max_charge_power_percent_306
            ),
        )
        no_prior_write_possible = (
            intent.action is ExecutionAction.MASTER_STOP
            and transaction.command_sent_at is None
        )
        post_write_neutral_confirmed = bool(
            transaction.restore_not_queued_count == 0
            and transaction.readback_result is VerificationStatus.CONFIRMED
            and transaction.command_sent_at is not None
            and transaction.expected_readback is not None
            and _written_generations_advanced(
                transaction.expected_readback,
                current,
                not_before=transaction.command_sent_at,
            )
        )
        original_intent_still_confirmed = bool(
            retry_snapshot is not None
            and snapshot.values_match(
                retry_snapshot,
                required_families=required,
            )
            and transaction.prewrite_snapshot is not None
            and transaction.command_sent_at is not None
            and transaction.expected_readback is not None
            and transaction.expected_readback
            == ExpectedReadback.from_command(
                transaction.prewrite_snapshot,
                intent.command,
            )
            and transaction.expected_readback.compare(
                current,
                now=now,
                not_before=transaction.command_sent_at,
                required_families=intent.command.families,
            )
            is ReadbackVerdict.MATCH
        )
        if (
            not off_grid
            and not no_prior_write_possible
            and not post_write_neutral_confirmed
            and transaction.restore_attempts == 0
            and transaction.restore_command is None
            and intent.command.families
            == frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
            and transaction.expected_readback is not None
            and transaction.prewrite_snapshot is not None
            and restored_values.values_match(current, required_families=required)
        ):
            # STOP before ACK must not mistake the still-old neutral block for
            # a completed restore. The firmware retains its pending-write
            # barrier through the first mismatching FC03. Observe another
            # complete generation before attempting an explicit restore.
            not_before = transaction.command_sent_at or _latest_observed_at(
                transaction.prewrite_snapshot, required_families=required
            )
            if not _written_generations_advanced(
                transaction.expected_readback, current, not_before=not_before
            ):
                return None
            barrier = transaction.restore_snapshot
            if barrier is None:
                self._record = replace(
                    self._record,
                    transaction=replace(transaction, restore_snapshot=current),
                    revision=self._record.revision + 1,
                )
                self._validate_record(self._record)
                return None
            if not _written_generations_advanced(
                ExpectedReadback.from_command(barrier, restore_command),
                current,
                not_before=_latest_observed_at(barrier, required_families=required),
            ):
                return None
        if (
            restored_values.values_match(current, required_families=required)
            and (
                off_grid
                or no_prior_write_possible
                or post_write_neutral_confirmed
            )
        ):
            completion_reason = (
                ExecutionReason.OFF_GRID_PRESERVED
                if off_grid
                else ExecutionReason.MASTER_STOP_COMPLETE
                if master_stop
                else ExecutionReason.RESTORE_CONFIRMED
            )
            finished = replace(
                transaction,
                state=ActiveState.IDLE,
                owner=ExecutionOwner.NONE,
                reason=(
                    completion_reason
                    if master_stop or off_grid
                    else transaction.reason
                ),
                restore_command=restore_command,
                restore_snapshot=current,
                restore_expected_readback=expected,
                rollback_status=RollbackStatus.CONFIRMED,
                rollback_result=VerificationStatus.CONFIRMED,
                off_grid_preserved=off_grid,
            )
            self._finish_transaction(finished, completion_reason, completed_at=now)
            return None
        if (
            transaction.restore_attempts > 0
            and (
                retry_snapshot is None
                or (
                    not retry_snapshot.values_match(
                        current,
                        required_families=required,
                    )
                    and not original_intent_still_confirmed
                )
            )
        ):
            failed = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=ExecutionReason.ROLLBACK_FAILED,
                rollback_status=RollbackStatus.FAILED,
                rollback_result=VerificationStatus.CONTRADICTED,
            )
            self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=failed,
                reason=ExecutionReason.ROLLBACK_FAILED,
                master_stop_failure=master_stop,
            )
            return None
        if (
            transaction.restore_not_queued_count >= 2
            or (
                transaction.restore_not_queued_first_at is not None
                and (now - transaction.restore_not_queued_first_at).total_seconds()
                >= RESTORE_DISPATCH_TIMEOUT_SECONDS
            )
        ):
            if (
                self._record.state is ActiveState.FAULT
                and self._record.reason is ExecutionReason.COMMAND_NOT_QUEUED
                and transaction.rollback_status is RollbackStatus.PENDING
            ):
                return None
            held = replace(
                transaction,
                state=ActiveState.FAULT,
                rollback_status=RollbackStatus.PENDING,
                rollback_result=VerificationStatus.PENDING,
            )
            self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=held,
                reason=ExecutionReason.COMMAND_NOT_QUEUED,
                master_stop_failure=master_stop,
            )
            return None
        if not prepared_unsent and transaction.restore_attempts >= 2:
            failed = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=ExecutionReason.ROLLBACK_FAILED,
                rollback_status=RollbackStatus.FAILED,
                rollback_result=VerificationStatus.CONTRADICTED,
            )
            self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=failed,
                reason=ExecutionReason.ROLLBACK_FAILED,
                master_stop_failure=master_stop,
            )
            return None
        restore_attempts = (
            transaction.restore_attempts
            if prepared_unsent
            else transaction.restore_attempts + 1
        )
        restoring = replace(
            transaction,
            restore_command=restore_command,
            restore_snapshot=current,
            restore_expected_readback=expected,
            rollback_status=RollbackStatus.PENDING,
            rollback_result=VerificationStatus.PENDING,
            restore_sent_at=None,
            restore_attempts=restore_attempts,
            off_grid_preserved=off_grid,
        )
        self._record = replace(
            self._record,
            transaction=restoring,
            reason=ExecutionReason.RESTORING,
            master_stop_result=(
                replace(
                    self._record.master_stop_result,
                    status=MasterStopStatus.IN_PROGRESS,
                    off_grid_preserved=off_grid,
                    reason=(
                        ExecutionReason.OFF_GRID_PRESERVED
                        if off_grid
                        else ExecutionReason.MASTER_STOP_REQUESTED
                    ),
                )
                if master_stop
                else self._record.master_stop_result
            ),
            revision=self._record.revision + 1,
        )
        return CommandEnvelope(
            transaction_id=transaction.transaction_id,
            owner=transaction.owner,
            action=ExecutionAction.MASTER_STOP if master_stop else intent.action,
            command=restore_command,
            expected_readback=expected,
        )

    def restore_prewrite_ready(
        self,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> bool:
        """Confirm a prepared restore still preserves the latest hardware."""

        self._require_state(ActiveState.STOPPING, ActiveState.FAULT)
        _require_time(now, "now")
        transaction = self._transaction()
        prepared = transaction.restore_command
        snapshot = transaction.restore_snapshot
        if prepared is None or snapshot is None:
            return False
        master_stop = transaction.master_stop_requested
        current_command = self._restore_command(
            transaction,
            off_grid=current.ems_block.mode is EmsMode.OFF_GRID,
        )
        if current_command != prepared:
            return False
        action = (
            ExecutionAction.MASTER_STOP
            if master_stop
            else transaction.intent.action
        )
        required = _required_readback_families(
            prepared,
            action,
            include_action_authority=False,
        )
        if current.freshness_errors(now, required_families=required):
            return False
        if _command_gate_reason(
            prepared,
            action,
            gates,
            expected_owner=transaction.owner,
            master_stop=True,
        ) is not None:
            return False
        return snapshot.values_match(
            current,
            required_families=required,
        ) and _snapshot_generations_match(
            snapshot,
            current,
            required_families=prepared.families,
        )

    def mark_restore_sent(
        self,
        envelope: CommandEnvelope,
        *,
        sent_at: datetime,
    ) -> ExecutorRecord:
        self._require_state(ActiveState.STOPPING, ActiveState.FAULT)
        _require_time(sent_at, "restore sent_at")
        transaction = self._transaction()
        if (
            transaction.restore_command is None
            or transaction.restore_snapshot is None
            or transaction.restore_expected_readback is None
        ):
            raise RuntimeError("restore was not prepared")
        expected_action = (
            ExecutionAction.MASTER_STOP
            if transaction.master_stop_requested
            else transaction.intent.action
        )
        if (
            envelope.transaction_id != transaction.transaction_id
            or envelope.owner is not transaction.owner
            or envelope.action is not expected_action
            or envelope.command != transaction.restore_command
            or envelope.expected_readback != transaction.restore_expected_readback
        ):
            raise ValueError("restore envelope does not belong to the transaction")
        required = _required_readback_families(
            transaction.restore_command,
            expected_action,
            include_action_authority=False,
        )
        if sent_at < _latest_observed_at(
            transaction.restore_snapshot,
            required_families=required,
        ):
            raise ValueError("restore cannot precede its physical snapshot")
        restoring = replace(
            transaction,
            state=ActiveState.RESTORING,
            restore_sent_at=sent_at,
        )
        return self._publish(
            ActiveState.RESTORING,
            owner=transaction.owner,
            transaction=restoring,
            reason=ExecutionReason.RESTORING,
        )

    def mark_restore_outcome_unknown(self) -> ExecutorRecord:
        """Retain ownership when a restore API call may have partially executed."""

        self._require_state(
            ActiveState.STOPPING,
            ActiveState.RESTORING,
            ActiveState.FAULT,
        )
        transaction = self._transaction()
        if transaction.restore_command is None:
            raise RuntimeError("restore was not prepared")
        fault = replace(
            transaction,
            state=ActiveState.FAULT,
            reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
            rollback_status=RollbackStatus.PENDING,
            rollback_result=VerificationStatus.PENDING,
            restore_command=None,
            restore_expected_readback=None,
            restore_sent_at=None,
        )
        return self._publish(
            ActiveState.FAULT,
            owner=transaction.owner,
            transaction=fault,
            reason=ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
        )

    def mark_restore_not_queued(
        self,
        rejection: AtomicWriteNotQueued,
        *,
        now: datetime,
    ) -> ExecutorRecord:
        """Defer a restore that firmware proved never entered transport.

        ``prepare_restore`` reserves one physical attempt before dispatch. A
        correlated pending-writer or stale-snapshot response proves this
        restore did not enter transport. Refund that attempt; admission refusals
        have a separate durable count and fixed deadline. The next attempt
        still needs a newer complete FC03 cohort.
        """

        self._require_state(ActiveState.STOPPING, ActiveState.FAULT)
        if not isinstance(rejection, AtomicWriteNotQueued):
            raise TypeError("restore rejection must prove that transport was not queued")
        if rejection.reason not in {
            "previous_write_pending", "stale_snapshot", "stale_snapshot_generation",
        }:
            raise ValueError("restore refusal does not prove a bounded admission failure")
        _require_time(now, "restore admission refusal time")
        transaction = self._transaction()
        if transaction.restore_command is None or transaction.restore_sent_at is not None:
            raise RuntimeError("only an unsent prepared restore can be deferred")
        not_queued_count = transaction.restore_not_queued_count + 1
        first_at = transaction.restore_not_queued_first_at
        if first_at is None:
            first_at = now
        exhausted = bool(
            not_queued_count >= 2
            or (now - first_at).total_seconds()
            >= RESTORE_DISPATCH_TIMEOUT_SECONDS
        )
        deferred = replace(
            transaction,
            state=ActiveState.FAULT if exhausted else ActiveState.STOPPING,
            rollback_status=RollbackStatus.PENDING,
            rollback_result=VerificationStatus.PENDING,
            restore_command=None,
            restore_expected_readback=None,
            restore_sent_at=None,
            restore_attempts=max(0, transaction.restore_attempts - 1),
            restore_not_queued_count=not_queued_count,
            restore_not_queued_first_at=first_at,
        )
        return self._publish(
            deferred.state,
            owner=transaction.owner,
            transaction=deferred,
            reason=(
                ExecutionReason.COMMAND_NOT_QUEUED
                if exhausted else ExecutionReason.RESTORING
            ),
        )

    def settle_late_terminal_fallback(
        self,
        readback: SettingsSnapshot,
        proof: Mapping[str, object],
        *,
        now: datetime,
    ) -> bool:
        """Resolve only this transaction's durable ESP fallback, without a write.

        The read-only ESP response is evidence only when the exact lease was
        cleared in NVS and every touched physical family has a newer coherent
        cohort matching the original fallback. Any ambiguity retains owner.
        """

        self._require_state(ActiveState.STOPPING, ActiveState.FAULT)
        _require_time(now, "terminal proof observation")
        transaction = self._transaction()
        identity = transaction.lease_identity
        snapshot = transaction.command_snapshot
        if (
            transaction.owner is ExecutionOwner.NONE
            or transaction.restore_not_queued_count == 0
            or transaction.restore_sent_at is not None
            or transaction.command_sent_at is None
            or identity is None
            or snapshot is None
            or not isinstance(proof, Mapping)
            or proof.get("schema_version") != 2
            or proof.get("protocol_version") != 2
            or proof.get("lease_cleared") is not True
            or proof.get("terminal_valid") is not True
            or type(proof.get("lease_state")) is not int
            or proof["lease_state"] != 0
            or type(proof.get("terminal_kind")) is not int
            or proof["terminal_kind"] != 1
            or proof.get("session_id") != identity[0]
            or proof.get("lease_id") != identity[1]
            or proof.get("transaction_id") != transaction.transaction_id
            or type(proof.get("command_generation")) is not int
            or proof["command_generation"] != identity[2]
            or type(proof.get("terminal_epoch")) is not int
            or not 1 <= proof["terminal_epoch"] <= 2**32 - 1
            or type(proof.get("fc03_generation")) is not int
            or not 1 <= proof["fc03_generation"] <= 16_000_000
            or readback.ems_generation < proof["fc03_generation"]
        ):
            return False
        # The terminal record proves this exact lease completed its fallback,
        # independently of whether HA received the initial command's FC03 ACK.
        # Keep the original readback/physical failure on the closed transaction;
        # only rollback and ownership are settled by the newer evidence below.
        # A wrap between ESP confirmation and HA's cohort is ambiguous.
        if transaction.terminal_epoch is not None and (
            proof["terminal_epoch"] <= transaction.terminal_epoch
        ):
            return False
        expected_block = snapshot.ems_block
        if expected_block.mode is not EmsMode.SELF_USE:
            return False
        expected_words = (
            int(expected_block.mode),
            floor(expected_block.self_use_soc_percent_4301 + 0.5),
            floor(expected_block.backup_soc_percent_4302 + 0.5),
            floor(expected_block.force_charge_soc_percent_4303 + 0.5),
            floor(expected_block.maximum_charge_power_percent_4304 * 10.0 + 0.5),
            floor(expected_block.force_discharge_soc_percent_4305 + 0.5),
            floor(expected_block.maximum_discharge_power_percent_4306 * 10.0 + 0.5),
        )
        if any(
            type(proof.get(f"fallback_430{index}")) is not int
            or proof[f"fallback_430{index}"] != word
            for index, word in enumerate(expected_words)
        ):
            return False
        restore_command = self._restore_command(transaction, off_grid=False)
        required = _required_readback_families(
            restore_command, transaction.intent.action,
            include_action_authority=False,
        )
        if readback.freshness_errors(now, required_families=required):
            return False
        barrier = transaction.restore_snapshot or snapshot
        barrier_at = _latest_observed_at(barrier, required_families=required)
        if not _written_generations_advanced(
            ExpectedReadback.from_command(barrier, restore_command),
            readback,
            not_before=max(barrier_at, transaction.command_sent_at),
        ):
            return False
        original_expected = ExpectedReadback.from_command(snapshot, restore_command)
        if original_expected.compare(
            readback, now=now, not_before=transaction.command_sent_at,
            required_families=required,
        ) is not ReadbackVerdict.MATCH:
            return False
        finished = replace(
            transaction,
            state=ActiveState.IDLE,
            owner=ExecutionOwner.NONE,
            reason=transaction.interruption_reason or transaction.reason,
            rollback_status=RollbackStatus.CONFIRMED,
            rollback_result=VerificationStatus.CONFIRMED,
            restore_command=restore_command,
            restore_snapshot=readback,
            terminal_epoch=proof["terminal_epoch"],
        )
        self._finish_transaction(
            finished, ExecutionReason.RESTORE_CONFIRMED, completed_at=now
        )
        return True

    def observe_restore_readback(
        self,
        readback: SettingsSnapshot,
        *,
        now: datetime,
    ) -> ReadbackVerdict:
        self._require_state(ActiveState.RESTORING)
        _require_time(now, "now")
        transaction = self._transaction()
        if transaction.restore_sent_at is None or transaction.restore_expected_readback is None:
            raise RuntimeError("restore has not been dispatched")
        if _timed_out(
            now,
            transaction.restore_sent_at,
            RESTORE_ACK_TIMEOUT_SECONDS,
        ):
            failed = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=ExecutionReason.ROLLBACK_FAILED,
                rollback_status=RollbackStatus.FAILED,
                rollback_result=VerificationStatus.UNAVAILABLE,
            )
            self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=failed,
                reason=ExecutionReason.ROLLBACK_FAILED,
                master_stop_failure=transaction.master_stop_requested,
            )
            return ReadbackVerdict.MISMATCH
        required = _required_readback_families(
            transaction.restore_command,
            (
                ExecutionAction.MASTER_STOP
                if transaction.master_stop_requested
                else transaction.intent.action
            ),
            include_action_authority=False,
        )
        verdict = transaction.restore_expected_readback.compare(
            readback,
            now=now,
            not_before=transaction.restore_sent_at,
            required_families=required,
        )
        if verdict is ReadbackVerdict.PENDING:
            return verdict
        if verdict is ReadbackVerdict.MISMATCH:
            restore_snapshot = transaction.restore_snapshot
            if (
                restore_snapshot is not None
                and _snapshot_generations_match_expected_base(
                    restore_snapshot,
                    transaction.restore_expected_readback,
                )
                and restore_snapshot.values_match(
                    readback,
                    required_families=required,
                )
            ):
                retry = replace(
                    transaction,
                    state=ActiveState.RESTORING,
                    rollback_status=RollbackStatus.PENDING,
                    rollback_result=VerificationStatus.UNAVAILABLE,
                    restore_snapshot=readback,
                )
                self._publish(
                    ActiveState.RESTORING,
                    owner=transaction.owner,
                    transaction=retry,
                    reason=ExecutionReason.RESTORING,
                )
                return verdict
            if (
                restore_snapshot is not None
                and not _snapshot_generations_match_expected_base(
                    restore_snapshot,
                    transaction.restore_expected_readback,
                )
            ):
                barrier_observed_at = _latest_observed_at(
                    restore_snapshot,
                    required_families=required,
                )
                barrier_expected = ExpectedReadback.from_command(
                    restore_snapshot,
                    transaction.restore_command,
                )
                if not _written_generations_advanced(
                    barrier_expected,
                    readback,
                    not_before=barrier_observed_at,
                ):
                    return ReadbackVerdict.PENDING
                if restore_snapshot.values_match(
                    readback,
                    required_families=required,
                ) and transaction.restore_attempts < 2:
                    retry = replace(
                        transaction,
                        state=ActiveState.STOPPING,
                        rollback_status=RollbackStatus.PENDING,
                        rollback_result=VerificationStatus.PENDING,
                        restore_command=None,
                        restore_snapshot=readback,
                        restore_expected_readback=None,
                        restore_sent_at=None,
                    )
                    self._publish(
                        ActiveState.STOPPING,
                        owner=transaction.owner,
                        transaction=retry,
                        reason=ExecutionReason.RESTORING,
                    )
                    return verdict
            failed = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=ExecutionReason.ROLLBACK_FAILED,
                rollback_status=RollbackStatus.FAILED,
                rollback_result=VerificationStatus.CONTRADICTED,
            )
            self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=failed,
                reason=ExecutionReason.ROLLBACK_FAILED,
                master_stop_failure=transaction.master_stop_requested,
            )
            return verdict
        reason = (
            ExecutionReason.OFF_GRID_PRESERVED
            if transaction.off_grid_preserved
            else ExecutionReason.MASTER_STOP_COMPLETE
            if transaction.master_stop_requested
            else ExecutionReason.RESTORE_CONFIRMED
        )
        finished = replace(
            transaction,
            state=ActiveState.IDLE,
            owner=ExecutionOwner.NONE,
            rollback_status=RollbackStatus.CONFIRMED,
            rollback_result=VerificationStatus.CONFIRMED,
        )
        self._finish_transaction(finished, reason, completed_at=now)
        return verdict

    def request_master_stop(
        self,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        transaction_id: str,
        now: datetime,
        deadline: datetime | None = None,
    ) -> ExecutorRecord:
        """Latch MASTER STOP before any cleanup and preserve physical Off-Grid."""

        _require_transaction_id(transaction_id)
        _require_time(now, "now")
        stop_deadline = deadline or (
            now + timedelta(seconds=DEFAULT_MASTER_STOP_DEADLINE_SECONDS)
        )
        _require_time(stop_deadline, "MASTER STOP deadline")
        if stop_deadline <= now:
            raise ValueError("MASTER STOP deadline has already passed")
        base_result = MasterStopResult(
            status=MasterStopStatus.REQUESTED,
            transaction_id=transaction_id,
            requested_at=now,
            reason=ExecutionReason.MASTER_STOP_REQUESTED,
        )
        self._record = replace(
            self._record,
            starts_allowed=False,
            automatic_policies_enabled=False,
            master_stop_result=base_result,
            reason=ExecutionReason.MASTER_STOP_REQUESTED,
            revision=self._record.revision + 1,
        )
        if current.freshness_errors(now, required_families=frozenset()):
            return self._master_stop_blocked(
                transaction_id,
                now,
                ExecutionReason.STALE_INPUTS,
            )
        existing = self._record.transaction
        expected_owner = (
            self._record.owner
            if self._record.owner is not ExecutionOwner.NONE
            else None
        )
        # Manual schedules are authorized local writers outside the automatic
        # Supervisor transaction namespace.  MASTER STOP must be able to take
        # them over; otherwise the emergency path rejects the exact active
        # cycle it is meant to stop.  Automatic or unknown foreign owners stay
        # fail-closed.
        manual_takeover = (
            expected_owner is None
            and gates.observed_owner is ExecutionOwner.MANUAL
        )
        if gates.owner_conflict or (
            gates.observed_owner is not None
            and gates.observed_owner is not expected_owner
            and not manual_takeover
        ):
            return self._master_stop_blocked(
                transaction_id,
                now,
                ExecutionReason.FOREIGN_OWNER
                if not gates.owner_conflict
                else ExecutionReason.OWNER_CONFLICT,
            )
        if existing is not None and existing.owner is not ExecutionOwner.NONE:
            stopping = replace(
                existing,
                state=ActiveState.STOPPING,
                reason=ExecutionReason.MASTER_STOP_REQUESTED,
                rollback_status=RollbackStatus.PENDING,
                rollback_result=VerificationStatus.PENDING,
                restore_command=None,
                restore_snapshot=None,
                restore_expected_readback=None,
                restore_sent_at=None,
                restore_attempts=0,
                master_stop_requested=True,
                off_grid_preserved=current.ems_block.mode is EmsMode.OFF_GRID,
                deadline=stop_deadline,
            )
            if self._record.state is ActiveState.STOPPING:
                self._record = replace(
                    self._record,
                    transaction=stopping,
                    reason=ExecutionReason.MASTER_STOP_REQUESTED,
                    revision=self._record.revision + 1,
                )
                return self._record
            return self._publish(
                ActiveState.STOPPING,
                owner=existing.owner,
                transaction=stopping,
                reason=ExecutionReason.MASTER_STOP_REQUESTED,
            )
        if current.ems_block.mode is EmsMode.OFF_GRID:
            result = replace(
                base_result,
                status=MasterStopStatus.COMPLETED,
                completed_at=now,
                off_grid_preserved=True,
                reason=ExecutionReason.OFF_GRID_PRESERVED,
            )
            previous = existing
            self._record = ExecutorRecord(
                state=ActiveState.IDLE,
                owner=ExecutionOwner.NONE,
                transaction=None,
                last_transaction=previous,
                starts_allowed=False,
                automatic_policies_enabled=False,
                reason=ExecutionReason.OFF_GRID_PRESERVED,
                master_stop_result=result,
                revision=self._record.revision + 1,
            )
            return self._record
        neutral = replace(current.ems_block, mode=EmsMode.SELF_USE)
        intent = ActuatorIntent(
            policy=ExecutionOwner.MANUAL,
            action=ExecutionAction.MASTER_STOP,
            command=CommandSet(ems_block=neutral),
            candidate_revision=f"master-stop:{transaction_id}",
            deadline=stop_deadline,
        )
        transaction = TransactionRecord(
            transaction_id=transaction_id,
            state=ActiveState.STOPPING,
            intent=intent,
            owner=ExecutionOwner.MANUAL,
            started_at=now,
            deadline=stop_deadline,
            reason=ExecutionReason.MASTER_STOP_REQUESTED,
            command_snapshot=current,
            expected_readback=ExpectedReadback.from_command(current, intent.command),
            rollback_status=RollbackStatus.PENDING,
            master_stop_requested=True,
        )
        if self._record.state is ActiveState.IDLE:
            return self._publish(
                ActiveState.STOPPING,
                owner=ExecutionOwner.MANUAL,
                transaction=transaction,
                reason=ExecutionReason.MASTER_STOP_REQUESTED,
            )
        prior = self._record.transaction
        self._record = replace(
            self._record,
            state=ActiveState.STOPPING,
            owner=ExecutionOwner.MANUAL,
            transaction=transaction,
            last_transaction=prior or self._record.last_transaction,
            reason=ExecutionReason.MASTER_STOP_REQUESTED,
            revision=self._record.revision + 1,
        )
        self._validate_record(self._record)
        return self._record

    def check_deadline(self, *, now: datetime) -> ExecutorRecord:
        _require_time(now, "now")
        transaction = self._record.transaction
        if transaction is None:
            return self._record
        command_phase_timed_out = (
            self._record.state is ActiveState.STARTING
            and _timed_out(
                now,
                transaction.started_at,
                COMMAND_DISPATCH_TIMEOUT_SECONDS,
            )
        ) or (
            self._record.state in {
                ActiveState.WAITING_READBACK,
                ActiveState.RETARGETING,
            }
            and transaction.command_sent_at is not None
            and _timed_out(
                now,
                transaction.command_sent_at,
                command_wait_timeout(transaction),
            )
        )
        if command_phase_timed_out:
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=ExecutionReason.DEADLINE_REACHED,
                rollback_status=RollbackStatus.PENDING,
            )
            return self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=ExecutionReason.DEADLINE_REACHED,
            )
        if (
            self._record.state is ActiveState.RESTORING
            and transaction.restore_sent_at is not None
            and _timed_out(
                now,
                transaction.restore_sent_at,
                RESTORE_ACK_TIMEOUT_SECONDS,
            )
        ):
            failed = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=ExecutionReason.ROLLBACK_FAILED,
                rollback_status=RollbackStatus.FAILED,
                rollback_result=VerificationStatus.UNAVAILABLE,
            )
            return self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=failed,
                reason=ExecutionReason.ROLLBACK_FAILED,
                master_stop_failure=transaction.master_stop_requested,
            )
        if now <= transaction.deadline:
            return self._record
        if self._record.state in {ActiveState.SELECTED, ActiveState.BLOCKED}:
            return self.request_stop(reason=ExecutionReason.DEADLINE_REACHED)
        if self._record.state is ActiveState.EXECUTING:
            return self.request_stop(reason=ExecutionReason.DEADLINE_REACHED)
        if self._record.state in {
            ActiveState.STARTING,
            ActiveState.WAITING_READBACK,
            ActiveState.RETARGETING,
        }:
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=ExecutionReason.DEADLINE_REACHED,
                rollback_status=RollbackStatus.PENDING,
            )
            return self._publish(
                ActiveState.FAULT,
                owner=transaction.owner,
                transaction=fault,
                reason=ExecutionReason.DEADLINE_REACHED,
            )
        return self._record

    def reset_blocked(self) -> ExecutorRecord:
        self._require_state(ActiveState.BLOCKED)
        transaction = self._record.transaction
        if transaction is None:
            self._record = replace(
                self._record,
                state=ActiveState.IDLE,
                owner=ExecutionOwner.NONE,
                revision=self._record.revision + 1,
            )
            self._validate_record(self._record)
            return self._record
        if transaction.owner is not ExecutionOwner.NONE:
            raise RuntimeError("an owned blocked transaction cannot be discarded")
        finished = replace(
            transaction,
            state=ActiveState.IDLE,
            owner=ExecutionOwner.NONE,
        )
        return self._finish_transaction(finished, transaction.reason)

    def blocked_retry_ready(
        self,
        intent: ActuatorIntent,
        snapshot: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> bool:
        """Check a newly rebuilt intent before releasing a transient block."""

        self._require_state(ActiveState.BLOCKED)
        _require_time(now, "now")
        transaction = self._record.transaction
        if transaction is None or transaction.owner is not ExecutionOwner.NONE:
            return False
        if intent.deadline <= now or not self._record.starts_allowed:
            return False
        if _automatic_owner(intent.policy) and not self._record.automatic_policies_enabled:
            return False
        return self._start_rejection(intent, snapshot, gates, now=now) is None

    def rearm_after_master_stop(self) -> ExecutorRecord:
        self._require_state(ActiveState.IDLE)
        if self._record.owner is not ExecutionOwner.NONE or self._record.transaction is not None:
            raise RuntimeError("cannot rearm while a transaction owns execution")
        if self._record.master_stop_result.status is not MasterStopStatus.COMPLETED:
            raise RuntimeError("cannot rearm an incomplete MASTER STOP")
        self._record = replace(
            self._record,
            starts_allowed=True,
            automatic_policies_enabled=True,
            reason=ExecutionReason.IDLE,
            master_stop_result=MasterStopResult(),
            revision=self._record.revision + 1,
        )
        return self._record

    @classmethod
    def recover(cls, persisted: ExecutorRecord) -> "SupervisorExecutor":
        """Recover persisted state without assuming any command succeeded."""

        machine = cls(persisted)
        transaction = persisted.transaction
        if persisted.state is ActiveState.IDLE:
            return machine
        if transaction is None:
            # The only valid transaction-less non-idle state is a latched,
            # fail-closed MASTER STOP block.  Preserve it across restart.
            return machine
        if transaction.owner is ExecutionOwner.NONE:
            finished = replace(
                transaction,
                state=ActiveState.IDLE,
                reason=ExecutionReason.RESTART_RESELECT_REQUIRED,
            )
            machine._finish_transaction(
                finished,
                ExecutionReason.RESTART_RESELECT_REQUIRED,
            )
            return machine
        if (
            persisted.state is ActiveState.RESTORING
            and transaction.rollback_status is RollbackStatus.PENDING
            and transaction.restore_command is not None
            and transaction.restore_snapshot is not None
            and transaction.restore_expected_readback is not None
            and transaction.restore_sent_at is not None
            and transaction.restore_attempts >= 1
        ):
            resumed = replace(
                transaction,
                reason=ExecutionReason.RESTART_RECOVERY,
            )
            machine._record = replace(
                persisted,
                transaction=resumed,
                reason=ExecutionReason.RESTART_RECOVERY,
                revision=persisted.revision + 1,
            )
            machine._validate_record(machine._record)
            return machine
        recovered = replace(
            transaction,
            state=ActiveState.STOPPING,
            reason=ExecutionReason.RESTART_RECOVERY,
            rollback_status=RollbackStatus.PENDING,
            rollback_result=VerificationStatus.PENDING,
            restore_command=None,
            restore_expected_readback=None,
            restore_sent_at=None,
        )
        machine._record = replace(
            persisted,
            state=ActiveState.STOPPING,
            owner=transaction.owner,
            transaction=recovered,
            reason=ExecutionReason.RESTART_RECOVERY,
            revision=persisted.revision + 1,
        )
        machine._validate_record(machine._record)
        return machine

    def _start_rejection(
        self,
        intent: ActuatorIntent,
        snapshot: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> ExecutionReason | None:
        required = _required_readback_families(intent.command, intent.action)
        if snapshot.freshness_errors(now, required_families=required):
            return ExecutionReason.STALE_INPUTS
        reason = _gate_reason(intent, gates, expected_owner=None)
        if reason is not None:
            return reason
        if not _unchanged_fields_preserved(intent, snapshot):
            return ExecutionReason.SNAPSHOT_INVALID
        return _settings_reason(intent, snapshot)

    def _prewrite_rejection(
        self,
        transaction: TransactionRecord,
        current: SettingsSnapshot,
        gates: ExecutionGates,
        *,
        now: datetime,
    ) -> ExecutionReason | None:
        if now >= transaction.deadline:
            return ExecutionReason.DEADLINE_REACHED
        required = _required_readback_families(
            transaction.intent.command,
            transaction.intent.action,
        )
        if current.freshness_errors(now, required_families=required):
            return ExecutionReason.STALE_INPUTS
        reason = _gate_reason(
            transaction.intent,
            gates,
            expected_owner=transaction.owner,
        )
        if reason is not None:
            return reason
        assert transaction.command_snapshot is not None
        settings_reason = _settings_reason(transaction.intent, current)
        if settings_reason is not None:
            return settings_reason
        if not transaction.command_snapshot.values_match(
            current,
            required_families=required,
        ):
            return ExecutionReason.SNAPSHOT_CHANGED
        if (
            transaction.prewrite_snapshot is not None
            and not _snapshot_generations_match(
                transaction.prewrite_snapshot,
                current,
                required_families=transaction.intent.command.families,
            )
        ):
            return ExecutionReason.SNAPSHOT_CHANGED
        return None

    def _restore_command(
        self,
        transaction: TransactionRecord,
        *,
        off_grid: bool,
    ) -> CommandSet:
        snapshot = transaction.command_snapshot
        assert snapshot is not None
        touched = transaction.intent.command
        restore_ems: EmsBlock | None = None
        if touched.touches_ems or transaction.master_stop_requested:
            mode = (
                EmsMode.OFF_GRID
                if off_grid
                else EmsMode.SELF_USE
                if transaction.master_stop_requested
                else snapshot.ems_block.mode
            )
            restore_ems = replace(snapshot.ems_block, mode=mode)
        if touched.touches_gcf and snapshot.export_limit_percent_259 is None:
            raise RuntimeError("touched GCF family has no restorable snapshot")
        if (
            touched.touches_battery_306
            and snapshot.battery_max_charge_power_percent_306 is None
        ):
            raise RuntimeError("touched 306 family has no restorable snapshot")
        return CommandSet(
            ems_block=restore_ems,
            export_limit_percent_259=(
                snapshot.export_limit_percent_259 if touched.touches_gcf else None
            ),
            battery_max_charge_power_percent_306=(
                snapshot.battery_max_charge_power_percent_306
                if touched.touches_battery_306
                else None
            ),
        )

    def _master_stop_blocked(
        self,
        transaction_id: str,
        now: datetime,
        reason: ExecutionReason,
    ) -> ExecutorRecord:
        result = MasterStopResult(
            status=MasterStopStatus.BLOCKED,
            transaction_id=transaction_id,
            requested_at=now,
            completed_at=now,
            reason=reason,
        )
        transaction = self._record.transaction
        if transaction is not None and transaction.owner is not ExecutionOwner.NONE:
            fault = replace(
                transaction,
                state=ActiveState.FAULT,
                reason=reason,
                master_stop_requested=True,
            )
            self._record = replace(
                self._record,
                state=ActiveState.FAULT,
                transaction=fault,
                master_stop_result=result,
                reason=reason,
                revision=self._record.revision + 1,
            )
            return self._record
        self._record = replace(
            self._record,
            state=ActiveState.BLOCKED,
            owner=ExecutionOwner.NONE,
            master_stop_result=result,
            reason=reason,
            revision=self._record.revision + 1,
        )
        return self._record

    def _publish_blocked(
        self,
        transaction: TransactionRecord,
        reason: ExecutionReason,
    ) -> ExecutorRecord:
        blocked = replace(
            transaction,
            state=ActiveState.BLOCKED,
            owner=ExecutionOwner.NONE,
            reason=reason,
        )
        return self._publish(
            ActiveState.BLOCKED,
            owner=ExecutionOwner.NONE,
            transaction=blocked,
            reason=reason,
        )

    def _finish_transaction(
        self,
        transaction: TransactionRecord,
        reason: ExecutionReason,
        *,
        completed_at: datetime | None = None,
    ) -> ExecutorRecord:
        master_result = self._record.master_stop_result
        if transaction.master_stop_requested:
            master_result = replace(
                master_result,
                status=MasterStopStatus.COMPLETED,
                completed_at=completed_at,
                off_grid_preserved=transaction.off_grid_preserved,
                reason=reason,
            )
        self._record = replace(
            self._record,
            state=ActiveState.IDLE,
            owner=ExecutionOwner.NONE,
            transaction=None,
            last_transaction=transaction,
            reason=reason,
            master_stop_result=master_result,
            revision=self._record.revision + 1,
        )
        self._validate_record(self._record)
        return self._record

    def _publish(
        self,
        state: ActiveState,
        *,
        owner: ExecutionOwner,
        transaction: TransactionRecord,
        reason: ExecutionReason,
        master_stop_failure: bool = False,
    ) -> ExecutorRecord:
        current = self._record.state
        if state is not current and state not in _LEGAL_TRANSITIONS[current]:
            raise RuntimeError(
                f"illegal Active transition {current.value} -> {state.value}"
            )
        master_result = self._record.master_stop_result
        if master_stop_failure:
            master_result = replace(
                master_result,
                status=MasterStopStatus.FAILED,
                reason=reason,
            )
        self._record = replace(
            self._record,
            state=state,
            owner=owner,
            transaction=replace(
                transaction,
                state=state,
                owner=owner,
                reason=(
                    transaction.reason
                    if reason is ExecutionReason.RESTORING
                    else reason
                ),
            ),
            reason=reason,
            master_stop_result=master_result,
            revision=self._record.revision + 1,
        )
        self._validate_record(self._record)
        return self._record

    def _transaction(self) -> TransactionRecord:
        transaction = self._record.transaction
        if transaction is None:
            raise RuntimeError("Active state has no transaction")
        return transaction

    def _require_state(self, *states: ActiveState) -> None:
        if self._record.state not in states:
            expected = ", ".join(state.value for state in states)
            raise RuntimeError(
                f"state {self._record.state.value} does not permit this operation; "
                f"expected {expected}"
            )

    @staticmethod
    def _validate_record(record: ExecutorRecord) -> None:
        if type(record.starts_allowed) is not bool:
            raise ValueError("starts_allowed must be boolean")
        if type(record.automatic_policies_enabled) is not bool:
            raise ValueError("automatic policy flag must be boolean")
        if type(record.revision) is not int or record.revision < 0:
            raise ValueError("executor revision is invalid")
        transaction = record.transaction
        if record.state is ActiveState.IDLE:
            if record.owner is not ExecutionOwner.NONE or transaction is not None:
                raise ValueError("idle executor cannot retain an owner or transaction")
            return
        if transaction is None:
            if record.state is ActiveState.BLOCKED and record.owner is ExecutionOwner.NONE:
                return
            raise ValueError("non-idle executor requires a transaction")
        _require_transaction_id(transaction.transaction_id)
        _require_time(transaction.started_at, "transaction started_at")
        _require_time(transaction.deadline, "transaction deadline")
        if transaction.deadline <= transaction.started_at:
            raise ValueError("transaction deadline must follow its start")
        if (
            type(transaction.restore_attempts) is not int
            or not 0 <= transaction.restore_attempts <= 2
        ):
            raise ValueError("restore attempt count is invalid")
        if (
            type(transaction.restore_not_queued_count) is not int
            or not 0 <= transaction.restore_not_queued_count <= 2
            or (
                transaction.restore_not_queued_first_at is None
            ) != (transaction.restore_not_queued_count == 0)
        ):
            raise ValueError("restore admission count is invalid")
        if transaction.restore_not_queued_first_at is not None:
            _require_time(
                transaction.restore_not_queued_first_at,
                "restore admission first refusal",
            )
        if transaction.state is not record.state or transaction.owner is not record.owner:
            raise ValueError("transaction and executor ownership/state diverged")
        if record.state in {ActiveState.SELECTED, ActiveState.BLOCKED}:
            if record.owner is not ExecutionOwner.NONE:
                raise ValueError("selected or blocked state cannot own execution")
        elif record.owner is ExecutionOwner.NONE:
            raise ValueError("owned Active state has no owner")
        if record.state in {
            ActiveState.STARTING,
            ActiveState.WAITING_READBACK,
            ActiveState.EXECUTING,
            ActiveState.RETARGETING,
            ActiveState.STOPPING,
            ActiveState.RESTORING,
            ActiveState.FAULT,
        } and transaction.command_snapshot is None:
            raise ValueError("owned transaction has no complete settings snapshot")
        if record.state is ActiveState.WAITING_READBACK and transaction.command_sent_at is None:
            raise ValueError("waiting state has no dispatched command")
        if record.state is ActiveState.RETARGETING and (
            transaction.prewrite_snapshot is None
            or transaction.expected_readback is None
            or transaction.owner is not transaction.intent.policy
            or _retarget_mode(transaction.intent) is None
            or transaction.intent.command.families
            != frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
        ):
            raise ValueError("retargeting state lacks a supported prepared full-block command")
        if record.state is ActiveState.EXECUTING:
            verification = transaction.physical_verification
            if (
                transaction.readback_result is not VerificationStatus.CONFIRMED
                or verification is None
                or verification.status is not VerificationStatus.CONFIRMED
            ):
                raise ValueError("executing state lacks readback and physical proof")
        if record.state is ActiveState.RESTORING and (
            transaction.restore_command is None
            or transaction.restore_snapshot is None
            or transaction.restore_expected_readback is None
            or transaction.restore_sent_at is None
            or transaction.restore_attempts < 1
        ):
            raise ValueError("restoring state lacks a dispatched rollback")


__all__ = (
    "ActiveState",
    "ActuatorIntent",
    "AtomicWrite",
    "AtomicWriteNotQueued",
    "AtomicWriteFamily",
    "CommandEnvelope",
    "CommandSet",
    "COMMAND_ACK_TIMEOUT_SECONDS",
    "COMMAND_DISPATCH_TIMEOUT_SECONDS",
    "DEFAULT_MASTER_STOP_DEADLINE_SECONDS",
    "EmsBlock",
    "EmsMode",
    "ExecutionAction",
    "ExecutionGates",
    "ExecutionOwner",
    "ExecutionReason",
    "ExecutorRecord",
    "ExpectedReadback",
    "ExportAuthority",
    "MAX_FUTURE_SKEW_SECONDS",
    "MAX_GENERATION",
    "MAX_READBACK_AGE_SECONDS",
    "RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS",
    "RCE_EXECUTING_EMS_MAX_AGE_SECONDS",
    "SETTINGS_READBACK_MAX_AGE_SECONDS",
    "MasterStopResult",
    "MasterStopStatus",
    "PhysicalVerification",
    "ReadbackVerdict",
    "RESTORE_ACK_TIMEOUT_SECONDS",
    "RESTORE_DISPATCH_TIMEOUT_SECONDS",
    "RollbackStatus",
    "SettingsSnapshot",
    "SupervisorExecutor",
    "TransactionRecord",
    "VerificationStatus",
)
