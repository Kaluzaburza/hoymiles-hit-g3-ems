"""Transactional runtime coordinator for EMS Supervisor Active.

The coordinator owns one :class:`SupervisorExecutor`, persists every mutation
before transport, and serializes reconciliation through one asyncio lock.  It
contains no Home Assistant imports; the sensor adapter supplies bounded Store,
publish and ESPHome dispatch callbacks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import logging
from math import isfinite
from typing import Any, Final
from uuid import uuid4


_LOGGER = logging.getLogger(__name__)
MASTER_STOP_PERSIST_TIMEOUT_SECONDS = 10.0

if __package__:
    from .ems_supervisor import (
        ExportState,
        ExecutionContext,
        PolicyId,
        PolicyCandidate,
        SupervisorDecision,
        SupervisorMode,
    )
    from .supervisor_active_bridge import (
        ActiveBridgeError,
        RCE_RECALCULATION_HOLD_SECONDS,
        RCE_RETARGET_COHORT_HOLD_SECONDS,
        RCE_POST_COMMAND_SETTLING_SECONDS,
        RCE_POST_COMMAND_REPLAN_SECONDS,
        TARIFF_POST_COMMAND_SETTLING_SECONDS,
        authorization_matches,
        build_actuator_intent,
        build_same_window_rce_retarget,
        execution_gates,
        physical_verification,
        post_command_rce_measurement_hold_authorized,
        rcm_charge_command_within_live_bms_limit,
        rcm_pre_discharge_command_within_live_bms_limit,
        rce_sent_command_within_live_bms_limit,
        same_window_rce_execution_hold_seconds,
        same_window_rce_rearm_desired,
        same_window_rce_retarget_wait_authorized,
        same_window_rce_target_authorized,
        same_window_rce_wait_authorized,
        same_run_tariff_desired,
        same_run_tariff_wait_authorized,
        tariff_command_within_live_bms_limit,
        selected_candidate,
        rce_export_evidence,
        settings_from_execution_source,
    )
    from .supervisor_executor import (
        ActiveState,
        AtomicWriteFamily,
        AtomicWrite,
        AtomicWriteNotQueued,
        COMMAND_ACK_TIMEOUT_SECONDS,
        CONFIRMED_TELEMETRY_GRACE_SECONDS,
        command_wait_timeout,
        COMMAND_DISPATCH_TIMEOUT_SECONDS,
        ExecutionAction,
        ExecutionOwner,
        ExecutionReason,
        EmsMode,
        ExecutorRecord,
        ExpectedReadback,
        MAX_READBACK_AGE_SECONDS,
        PhysicalVerification,
        RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS,
        TARIFF_PHYSICAL_MAX_AGE_SECONDS,
        RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
        MasterStopStatus,
        ReadbackVerdict,
        RESTORE_ACK_TIMEOUT_SECONDS,
        RESTORE_DISPATCH_TIMEOUT_SECONDS,
        RollbackStatus,
        SETTINGS_READBACK_MAX_AGE_SECONDS,
        SettingsSnapshot,
        SupervisorExecutor,
        TransactionRecord,
        VerificationStatus,
    )
    from .supervisor_executor_codec import record_to_dict
    from .supervisor_runtime import (
        ExecutionSourceSnapshot,
        RceSourceSnapshot,
        RcmSourceSnapshot,
        TariffSourceSnapshot,
    )
else:  # Direct import used by dependency-free repository tests.
    from ems_supervisor import (  # type: ignore[no-redef]
        ExportState,
        ExecutionContext,
        PolicyId,
        PolicyCandidate,
        SupervisorDecision,
        SupervisorMode,
    )
    from supervisor_active_bridge import (  # type: ignore[no-redef]
        ActiveBridgeError,
        RCE_RECALCULATION_HOLD_SECONDS,
        RCE_RETARGET_COHORT_HOLD_SECONDS,
        RCE_POST_COMMAND_SETTLING_SECONDS,
        RCE_POST_COMMAND_REPLAN_SECONDS,
        TARIFF_POST_COMMAND_SETTLING_SECONDS,
        authorization_matches,
        build_actuator_intent,
        build_same_window_rce_retarget,
        execution_gates,
        physical_verification,
        post_command_rce_measurement_hold_authorized,
        rcm_charge_command_within_live_bms_limit,
        rcm_pre_discharge_command_within_live_bms_limit,
        rce_sent_command_within_live_bms_limit,
        same_window_rce_execution_hold_seconds,
        same_window_rce_rearm_desired,
        same_window_rce_retarget_wait_authorized,
        same_window_rce_target_authorized,
        same_window_rce_wait_authorized,
        same_run_tariff_desired,
        same_run_tariff_wait_authorized,
        tariff_command_within_live_bms_limit,
        selected_candidate,
        rce_export_evidence,
        settings_from_execution_source,
    )
    from supervisor_executor import (  # type: ignore[no-redef]
        ActiveState,
        AtomicWriteFamily,
        AtomicWrite,
        AtomicWriteNotQueued,
        COMMAND_ACK_TIMEOUT_SECONDS,
        CONFIRMED_TELEMETRY_GRACE_SECONDS,
        command_wait_timeout,
        COMMAND_DISPATCH_TIMEOUT_SECONDS,
        ExecutionAction,
        ExecutionOwner,
        ExecutionReason,
        EmsMode,
        ExecutorRecord,
        ExpectedReadback,
        MAX_READBACK_AGE_SECONDS,
        PhysicalVerification,
        RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS,
        TARIFF_PHYSICAL_MAX_AGE_SECONDS,
        RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
        MasterStopStatus,
        ReadbackVerdict,
        RESTORE_ACK_TIMEOUT_SECONDS,
        RESTORE_DISPATCH_TIMEOUT_SECONDS,
        RollbackStatus,
        SETTINGS_READBACK_MAX_AGE_SECONDS,
        SettingsSnapshot,
        SupervisorExecutor,
        TransactionRecord,
        VerificationStatus,
    )
    from supervisor_executor_codec import record_to_dict  # type: ignore[no-redef]
    from supervisor_runtime import (  # type: ignore[no-redef]
        ExecutionSourceSnapshot,
        RceSourceSnapshot,
        RcmSourceSnapshot,
        TariffSourceSnapshot,
    )


MAX_RECONCILE_STEPS: Final = 12
PV_HOLD_RETRY_INTERVAL_SECONDS: Final = 180
TARIFF_RETRY_MIN_INTERVAL_SECONDS: Final = 15.0
TARIFF_RETRY_MAX_ATTEMPTS_PER_WINDOW: Final = 1
TARIFF_SUPPORT_FLOW_FILTER_SECONDS: Final = 60.0
# Finish local bookkeeping before the earliest device/transaction boundary.
# This is deliberately small relative to the 120-second soft lease, but makes
# the exclusive transport boundary explicit instead of racing it at equality.
RETARGET_HANDLING_MARGIN_SECONDS: Final = 0.25


def _pv_hold_retry_reference(previous: TransactionRecord) -> datetime:
    """Use durable attempt evidence, never the timestamp of a later replan."""
    return max(at for at in (
        previous.started_at, previous.command_sent_at, previous.restore_sent_at,
        previous.restore_snapshot.ems_observed_at if previous.restore_snapshot else None,
        previous.physical_verification.observed_at if previous.physical_verification else None,
    ) if at is not None)

_EXECUTION_PHASE_BY_ACTIVE_STATE: Final = {
    ActiveState.IDLE: "idle",
    ActiveState.SELECTED: "selected",
    ActiveState.STARTING: "starting",
    ActiveState.WAITING_READBACK: "waiting_readback",
    ActiveState.EXECUTING: "executing",
    # Public Aurora phase stays within the established allowlist while the
    # raw ``active_state`` still exposes ``active_retargeting`` for evidence.
    ActiveState.RETARGETING: "waiting_readback",
    ActiveState.STOPPING: "stopping",
    ActiveState.RESTORING: "restoring",
    ActiveState.BLOCKED: "blocked",
    ActiveState.FAULT: "fault",
}

_RETRYABLE_BLOCK_REASONS: Final = frozenset(
    {
        ExecutionReason.STALE_INPUTS,
        ExecutionReason.OWNER_CONFLICT,
        ExecutionReason.FOREIGN_OWNER,
        ExecutionReason.FULL_BLOCK_UNAVAILABLE,
        ExecutionReason.DIRECT_REGISTER_UNAVAILABLE,
        ExecutionReason.TOPOLOGY_BLOCKED,
        ExecutionReason.BMS_UNAVAILABLE,
        ExecutionReason.DIRECTION_UNAVAILABLE,
        ExecutionReason.CONFIRMED_ZERO_EXPORT,
        ExecutionReason.EXPORT_PROHIBITED,
        ExecutionReason.EXPORT_LIMIT_INCREASE,
        ExecutionReason.SNAPSHOT_CHANGED,
        ExecutionReason.OFF_GRID_PRESERVED,
    }
)

PersistRecord = Callable[[ExecutorRecord], Awaitable[None]]
DispatchWrite = Callable[[AtomicWrite], Awaitable[None]]
PublishRecord = Callable[[ExecutorRecord], None]
UtcClock = Callable[[], datetime]
ManualAuthorityCheck = Callable[[], bool]
ManualDispatch = Callable[[], Awaitable[None]]
ManualReadbackResolved = Callable[[], bool]


def _system_utc_now() -> datetime:
    """Return the system clock at the physical dispatch-completion boundary."""

    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class ActiveFrame:
    """One immutable arbitration/readback frame consumed by the controller."""

    now: datetime
    decision: SupervisorDecision
    candidates: tuple[PolicyCandidate, ...]
    context: ExecutionContext
    rce: RceSourceSnapshot
    tariff: TariffSourceSnapshot
    rcm: RcmSourceSnapshot
    execution: ExecutionSourceSnapshot

    def __post_init__(self) -> None:
        if self.now.tzinfo is None or self.now.utcoffset() is None:
            raise ValueError("Active frame time must be timezone-aware")
        if len(self.candidates) != 3:
            raise ValueError("Active frame requires exactly three policy candidates")


FrameResampler = Callable[[], ActiveFrame | None]
RetargetReplanPending = Callable[[], bool]
RetargetResumeFrame = Callable[[], ActiveFrame | None]
ControlLeaseValidUntil = Callable[[], datetime | None]


@dataclass(frozen=True, slots=True)
class RetargetPendingContext:
    """Bounded adapter-callback context for one unsent retarget decision."""

    planner_pending: bool = False
    cohort_pending: bool = False
    source: str | None = None
    generation: int | None = None


RetargetPendingContextSource = Callable[[], RetargetPendingContext]


class _PreDispatchRejected(RuntimeError):
    """A newer frame revoked a prepared envelope before one of its writes."""

    def __init__(self, dispatched_count: int) -> None:
        super().__init__("prepared envelope lost current physical authority")
        self.dispatched_count = dispatched_count


class _RetargetAttemptExpired(RuntimeError):
    """The one immutable retarget budget ended before safe completion."""


class SupervisorActiveController:
    """Persist-first owner and sole dispatcher for automatic inverter writes."""

    def __init__(
        self,
        *,
        persist: PersistRecord,
        dispatch: DispatchWrite,
        publish: PublishRecord,
        persisted_record: ExecutorRecord | None = None,
        clock: UtcClock = _system_utc_now,
        frame_resampler: FrameResampler | None = None,
        retarget_replan_pending: RetargetReplanPending | None = None,
        retarget_resume_frame: RetargetResumeFrame | None = None,
        retarget_pending_context: RetargetPendingContextSource | None = None,
        control_lease_valid_until: ControlLeaseValidUntil | None = None,
        control_lease_identity: Callable[[], tuple[str, str, str, int] | None] | None = None,
        terminal_proof: Callable[[], Awaitable[Mapping[str, object] | None]] | None = None,
    ) -> None:
        self._persist = persist
        self._dispatch = dispatch
        self._publish = publish
        self._clock = clock
        self._frame_resampler = frame_resampler
        self._retarget_replan_pending = retarget_replan_pending
        self._retarget_resume_frame = retarget_resume_frame
        self._retarget_pending_context = retarget_pending_context
        self._control_lease_valid_until = control_lease_valid_until
        self._control_lease_identity = control_lease_identity
        self._terminal_proof = terminal_proof
        self._executor = (
            SupervisorExecutor.recover(persisted_record)
            if persisted_record is not None
            else SupervisorExecutor()
        )
        self._lock = asyncio.Lock()
        self._last_frame: ActiveFrame | None = None
        self._confirmed_telemetry_frame: tuple[str, datetime, ActiveFrame] | None = None
        self._telemetry_hold_until: datetime | None = None
        self._last_retry_evidence: object | None = None
        self._rce_execution_hold: tuple[str, datetime, float] | None = None
        self._pv_hold_first_proof = None
        self._pv_hold_next_retry = 0
        self._pv_hold_retry_status = {"allowed": True, "reason": "not_applicable"}
        self._post_command_dispatch_market: str | None = None
        self._post_command_market_basis: tuple[str, datetime, str] | None = None
        self._post_command_settling_key: tuple[str, datetime] | None = None
        self._tariff_execution_hold: tuple[str, datetime, datetime, datetime] | None = None
        self._tariff_support_flow_filter: tuple[str, datetime, datetime, PhysicalVerification] | None = None
        self._tariff_next_retry_attempt = 0
        self._tariff_retry_status: dict[str, Any] = {
            "allowed": True,
            "reason": "not_applicable",
            "attempts": 0,
            "maximum_attempts": TARIFF_RETRY_MAX_ATTEMPTS_PER_WINDOW,
            "retry_after": None,
        }
        recovered = self._executor.record.transaction
        self._tariff_planned_end: tuple[str, datetime] | None = (
            (recovered.transaction_id, recovered.deadline)
            if recovered is not None
            and recovered.owner is ExecutionOwner.TARIFF
            else None
        )
        self._initialized = False
        self._manual_readback_resolved: ManualReadbackResolved | None = None
        self._persisted_revision = (
            persisted_record.revision if persisted_record is not None else -1
        )
        self._persistence_error: str | None = None
        self._last_retarget_authorization_check: dict[str, Any] = {}
        self._last_retarget_resume_check: dict[str, Any] = {}
        self._stop_decisions: list[dict[str, Any]] = []
        self._settings_decode_failure: tuple[datetime, str] | None = None

    @property
    def record(self) -> ExecutorRecord:
        return self._executor.record

    @property
    def last_frame(self) -> ActiveFrame | None:
        return self._last_frame

    @property
    def manual_proxy_readback_pending(self) -> bool:
        return self._manual_readback_resolved is not None

    @property
    def record_persisted(self) -> bool:
        """Return whether the exact published executor revision is durable."""

        return (
            self._persistence_error is None
            and self._persisted_revision == self.record.revision
        )

    @property
    def persistence_error(self) -> str | None:
        """Expose a bounded safety-journal failure without hiding physical state."""

        return self._persistence_error

    @property
    def tariff_retry_status(self) -> dict[str, Any]:
        """Expose the bounded tariff retry decision for diagnostics only."""

        result = dict(self._tariff_retry_status)
        retry_after = result.get("retry_after")
        result["retry_after"] = (
            _iso(retry_after) if isinstance(retry_after, datetime) else None
        )
        return result

    @property
    def post_command_settling_deadline(self) -> datetime | None:
        """Expose a recognized command's immutable settling boundary to HA."""
        transaction = self.record.transaction
        key = self._post_command_settling_key
        if (transaction is None or key is None
            or self.record.state is not ActiveState.EXECUTING
            or key != (transaction.transaction_id, transaction.command_sent_at)):
            return None
        return min(key[1] + timedelta(seconds=RCE_POST_COMMAND_SETTLING_SECONDS),
                   transaction.deadline)

    @property
    def tariff_support_flow_filter_deadline(self) -> datetime | None:
        """Expose the bounded excursion wait; never a new physical proof."""
        tx = self.record.transaction
        held = self._tariff_support_flow_filter
        if (self.record.state is not ActiveState.EXECUTING or tx is None
            or tx.intent.action is not ExecutionAction.TARIFF_GRID_SUPPORT
            or held is None or tx.physical_verification is None
            or held[:3] != (tx.transaction_id, tx.command_sent_at,
                            tx.physical_verification.observed_at)):
            return None
        lease_end = (self._control_lease_valid_until()
            if self._control_lease_valid_until is not None else tx.deadline)
        return min(held[2] + timedelta(seconds=TARIFF_SUPPORT_FLOW_FILTER_SECONDS),
            tx.deadline, lease_end or held[2])

    def _tariff_support_excursion_pending(
        self, transaction: TransactionRecord, verification: PhysicalVerification, *, now: datetime,
    ) -> bool:
        """Recognize only a fresh coherent battery excursion after confirmed support."""
        previous = transaction.physical_verification
        lease_end = (self._control_lease_valid_until()
            if self._control_lease_valid_until is not None else transaction.deadline)
        return bool(
            transaction.owner is ExecutionOwner.TARIFF
            and transaction.intent.action is ExecutionAction.TARIFF_GRID_SUPPORT
            and previous is not None and previous.status is VerificationStatus.CONFIRMED
            and verification.status is VerificationStatus.CONTRADICTED
            and "tariff_flow=battery_excursion" in verification.evidence
            and verification.observed_at > previous.observed_at
            and 0 <= (now-previous.observed_at).total_seconds() < TARIFF_SUPPORT_FLOW_FILTER_SECONDS
            and now < transaction.deadline
            and lease_end is not None and now < lease_end
        )

    @property
    def tariff_execution_settling_deadline(self) -> datetime | None:
        """Expose the immutable tariff hold boundary without granting authority."""

        transaction = self.record.transaction
        hold = self._tariff_execution_hold
        if (
            transaction is None
            or transaction.owner is not ExecutionOwner.TARIFF
            or transaction.command_sent_at is None
            or hold is None
            or hold[:2]
            != (transaction.transaction_id, transaction.command_sent_at)
        ):
            return None
        return hold[3]

    @property
    def post_command_settling_replan(self) -> tuple[tuple[str, datetime], datetime] | None:
        """Request one adapter-owned replan, without granting write authority."""
        deadline = self.post_command_settling_deadline
        key = self._post_command_settling_key
        if deadline is None or key is None or _clock_utc(self._clock) >= deadline:
            return None
        frame = self._last_frame
        if (frame is not None and frame.rce.result_current is True
            and frame.rce.recalculation_pending is False
            and frame.rce.current_slot_planned is True
            and frame.rce.current_slot_continue_eligible is True):
            return None
        at = key[1] + timedelta(seconds=RCE_POST_COMMAND_REPLAN_SECONDS)
        return (key, at) if at < deadline else None

    @property
    def execution_watchdog(self) -> tuple[str, datetime] | None:
        """Return the earliest immutable controller boundary needing a frame."""

        transaction = self.record.transaction
        if transaction is None:
            previous = self.record.last_transaction
            if (self.record.state is ActiveState.IDLE and previous is not None
                and previous.intent.action is ExecutionAction.PV_CHARGE_HOLD
                and self._pv_hold_retry_status.get("reason") == "retry_spacing_active"):
                boundary = _pv_hold_retry_reference(previous) + timedelta(
                    seconds=PV_HOLD_RETRY_INTERVAL_SECONDS)
                if boundary > _clock_utc(self._clock):
                    # Reuse the adapter's single frame callback. This grants no
                    # execution authority and neither polls nor resets a plan.
                    return previous.transaction_id, boundary
            return None
        state = self.record.state
        boundaries: list[datetime] = []
        if state is ActiveState.EXECUTING and self._telemetry_hold_until is not None:
            return transaction.transaction_id, min(transaction.deadline, self._telemetry_hold_until)
        if state in {
            ActiveState.WAITING_READBACK,
            ActiveState.RETARGETING,
        }:
            if transaction.command_sent_at is None:
                return None
            if self._telemetry_hold_until is not None:
                boundaries.append(self._telemetry_hold_until)
            boundaries.extend(
                (
                    transaction.deadline,
                    transaction.command_sent_at
                    + timedelta(seconds=command_wait_timeout(transaction)),
                )
            )
            proof = transaction.physical_verification
            if (
                state in {
                    ActiveState.WAITING_READBACK,
                    ActiveState.RETARGETING,
                }
                and transaction.owner is ExecutionOwner.RCE
                and transaction.intent.action is ExecutionAction.RCE_EXPORT
                and transaction.readback_result is VerificationStatus.CONFIRMED
            ):
                # Once a Mode 5 FC03 is acknowledged, a physical-pending phase
                # may not outlive the evidence that granted it.  This also
                # covers a neutral-flow retarget without granting it the
                # initial negative-flow settling exception.
                # The sensor schedules this boundary at +1 microsecond, so the
                # inclusive freshness limits below expire deterministically.
                if (
                    proof is not None
                    and proof.status is VerificationStatus.PENDING
                    and state is ActiveState.RETARGETING
                ):
                    boundaries.append(
                        proof.observed_at
                        + timedelta(seconds=MAX_READBACK_AGE_SECONDS)
                    )
                frame = self._last_frame
                if frame is not None:
                    try:
                        settings = settings_from_execution_source(frame.execution)
                    except (ActiveBridgeError, TypeError, ValueError):
                        pass
                    else:
                        boundaries.append(
                            settings.ems_observed_at
                            + timedelta(seconds=MAX_READBACK_AGE_SECONDS)
                        )
                        if settings.gcf_observed_at is not None:
                            boundaries.append(
                                settings.gcf_observed_at
                                + timedelta(
                                    seconds=SETTINGS_READBACK_MAX_AGE_SECONDS
                                )
                            )
        elif state is ActiveState.EXECUTING:
            boundaries.append(transaction.deadline)
            if transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD:
                frame = self._last_frame
                proof = transaction.physical_verification
                if proof is not None:
                    boundaries.append(proof.observed_at + timedelta(seconds=15))
                if frame is not None:
                    for observed, maximum_age in (
                        (frame.execution.full_block_generation_at, 15),
                        (frame.execution.battery_soc_observed_at, 120),
                        (frame.execution.bms_voltage_observed_at, 300),
                        (frame.execution.bms_charge_current_observed_at, 300),
                        (frame.execution.bms_discharge_current_observed_at, 300),
                    ):
                        if observed is not None:
                            boundaries.append(observed+timedelta(seconds=maximum_age))
            if transaction.owner is ExecutionOwner.TARIFF:
                planned = self._tariff_planned_end
                if planned is not None and planned[0] == transaction.transaction_id:
                    boundaries.append(min(planned[1], transaction.deadline))
                frame = self._last_frame
                proof = transaction.physical_verification
                if frame is not None:
                    for observed, maximum_age in (
                        (frame.execution.full_block_generation_at,15),
                        (frame.execution.battery_soc_observed_at,120),
                        (frame.execution.bms_voltage_observed_at,300),
                        (frame.execution.bms_charge_current_observed_at,300),
                    ):
                        if observed is not None:
                            boundaries.append(observed+timedelta(seconds=maximum_age))
                flow_filter_end = self.tariff_support_flow_filter_deadline
                if proof is not None:
                    # The ordinary physical watchdog cannot be advanced by a
                    # bad cohort. Replace only its power-proof boundary while
                    # retaining all FC03/BMS/SOC/plan and lease boundaries.
                    boundaries.append(flow_filter_end or
                        proof.observed_at+timedelta(seconds=TARIFF_PHYSICAL_MAX_AGE_SECONDS))
                if flow_filter_end is not None and frame is not None:
                    raw = physical_verification(
                        transaction.transaction_id, transaction.intent.action,
                        frame.execution,
                        command_sent_at=transaction.command_sent_at,
                        now=_clock_utc(self._clock),
                    )
                    if (raw.status is VerificationStatus.PENDING
                        and raw.evidence == ("physical_cohort_incomplete",)):
                        # Incomplete data keeps its shorter existing watchdog;
                        # an earlier excursion must not add telemetry grace.
                        boundaries.append(proof.observed_at+timedelta(
                            seconds=TARIFF_PHYSICAL_MAX_AGE_SECONDS))
                    for observed in (frame.execution.grid_power_observed_at,
                        frame.execution.battery_power_observed_at,
                        frame.execution.pv_power_observed_at,
                        frame.execution.load_power_observed_at):
                        if observed is not None:
                            boundaries.append(observed+timedelta(seconds=TARIFF_PHYSICAL_MAX_AGE_SECONDS))
            if (
                transaction.owner is ExecutionOwner.RCE
                and transaction.intent.action is ExecutionAction.RCE_EXPORT
            ):
                frame = self._last_frame
                proof = transaction.physical_verification
                if frame is not None:
                    try:
                        settings = settings_from_execution_source(frame.execution)
                    except (ActiveBridgeError, TypeError, ValueError):
                        pass
                    else:
                        boundaries.append(
                            settings.ems_observed_at
                            + timedelta(
                                seconds=RCE_EXECUTING_EMS_MAX_AGE_SECONDS
                            )
                        )
                        if settings.gcf_observed_at is not None:
                            boundaries.append(
                                settings.gcf_observed_at
                                + timedelta(
                                    seconds=SETTINGS_READBACK_MAX_AGE_SECONDS
                                )
                            )
                if proof is not None:
                    boundaries.append(
                        proof.observed_at
                        + timedelta(
                            seconds=RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS
                        )
                    )
        elif (
            state is ActiveState.RESTORING
            and transaction.restore_sent_at is not None
        ):
            boundaries.append(
                transaction.restore_sent_at
                + timedelta(seconds=RESTORE_ACK_TIMEOUT_SECONDS)
            )
        else:
            return None
        hold = self._rce_execution_hold
        settling_deadline = self.post_command_settling_deadline
        if settling_deadline is not None:
            boundaries.append(settling_deadline)
        if (
            state in {ActiveState.EXECUTING, ActiveState.WAITING_READBACK}
            and transaction.owner is ExecutionOwner.RCE
            and hold is not None
            and hold[0] == transaction.transaction_id
        ):
            boundaries.append(
                hold[1] + timedelta(seconds=hold[2])
            )
        tariff_hold = self._tariff_execution_hold
        if (state is ActiveState.EXECUTING and transaction.owner is ExecutionOwner.TARIFF
            and tariff_hold is not None
            and tariff_hold[:2] == (transaction.transaction_id, transaction.command_sent_at)):
            boundaries.append(tariff_hold[3])
        return transaction.transaction_id, min(boundaries)

    def _tariff_planned_boundary(self, transaction: Any) -> datetime:
        """Return the current economic end, capped by the hard deadline."""

        planned = self._tariff_planned_end
        if planned is None or planned[0] != transaction.transaction_id:
            return transaction.deadline
        return min(planned[1], transaction.deadline)

    def _remember_tariff_planned_end(
        self,
        transaction: Any,
        planned_end: datetime,
    ) -> None:
        """Update only the soft economic boundary of this tariff transaction."""

        self._tariff_planned_end = (
            transaction.transaction_id,
            min(_utc(planned_end), transaction.deadline),
        )

    async def async_initialize(self) -> None:
        """Persist restart recovery before the adapter publishes or writes."""

        async with self._lock:
            if self._initialized:
                return
            await self._commit()
            self._initialized = True

    async def async_reconcile(self, frame: ActiveFrame) -> ExecutorRecord:
        """Advance at most one bounded transaction using the newest frame."""

        async with self._lock:
            if not self._initialized:
                raise RuntimeError("Active controller is not initialized")
            self._last_frame = frame
            if self._manual_proxy_readback_pending() and self.record.state is ActiveState.IDLE:
                return self.record
            await self._reconcile_locked(frame)
            tx = self.record.transaction
            if (self.record.state is ActiveState.EXECUTING and tx is not None
                and tx.command_sent_at is not None and tx.physical_verification is not None
                and tx.physical_verification.status is VerificationStatus.CONFIRMED
                and 0 <= (frame.now-tx.physical_verification.observed_at).total_seconds() <= 15
                and (self._confirmed_telemetry_frame is None
                     or self._confirmed_telemetry_frame[:2] != (tx.transaction_id, tx.command_sent_at)
                     or tx.physical_verification.observed_at > self._confirmed_telemetry_frame[2].now)):
                self._confirmed_telemetry_frame = (tx.transaction_id, tx.command_sent_at, frame)
            return self.record

    async def async_manual_proxy_dispatch(
        self,
        *,
        authority_valid: ManualAuthorityCheck,
        dispatch: ManualDispatch,
        readback_resolved: ManualReadbackResolved,
    ) -> bool:
        """Serialize one manual proxy write with every automatic write.

        Authority is checked only after acquiring the controller lock.  The
        lock is retained until the source service call completes, so an Active
        reconcile can neither acquire ownership nor dispatch behind this
        check.  A queued Active reconcile runs afterwards and starts from the
        resulting physical readback generation.
        """

        async with self._lock:
            if (
                not self._initialized
                or self._manual_proxy_readback_pending()
                or not authority_valid()
            ):
                return False
            self._manual_readback_resolved = readback_resolved
            self._publish(self.record)
            await dispatch()
            self._manual_proxy_readback_pending()
            return True

    async def async_master_stop(self, frame: ActiveFrame) -> ExecutorRecord:
        """Latch MASTER STOP and continue restoration across Store failures."""

        async with self._lock:
            if not self._initialized:
                raise RuntimeError("Active controller is not initialized")
            self._last_frame = frame
            if self._manual_proxy_readback_pending() and self.record.state is ActiveState.IDLE:
                return self.record
            settings = settings_from_execution_source(frame.execution)
            gates = self._gates(frame)
            now = _utc(frame.now)
            self._executor.request_master_stop(
                settings,
                gates,
                transaction_id=_transaction_id("master-stop"),
                now=now,
            )
            # The RAM fence is already authoritative for new starts. A failed
            # Store remains visible and retryable, but may not indefinitely
            # prevent the sole controller from reaching the physical rollback.
            await self._commit_master_stop(force_retry=True)
            await self._reconcile_locked(frame, master_stop=True)
            return self.record

    async def async_retry_master_stop_persistence(self) -> bool:
        """Retry only the newest MASTER STOP revision, never an older snapshot."""

        async with self._lock:
            if self.record_persisted:
                return True
            if not self._master_stop_record():
                return False
            return await self._commit_master_stop(force_retry=True)

    async def async_rearm_after_master_stop(self) -> ExecutorRecord:
        """Rearm only the completed, owner-free MASTER STOP lifecycle."""

        async with self._lock:
            self._executor.rearm_after_master_stop()
            await self._commit()
            return self.record

    async def _reconcile_locked(
        self,
        frame: ActiveFrame,
        *,
        master_stop: bool = False,
    ) -> None:
        now = _utc(frame.now)
        initial_state = self.record.state
        transaction = self.record.transaction
        if (
            transaction is not None
            and transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD
            and initial_state in {
                ActiveState.SELECTED, ActiveState.STARTING,
                ActiveState.WAITING_READBACK, ActiveState.EXECUTING,
                ActiveState.RETARGETING,
            }
            and frame.context.export_state in {
                ExportState.CONFIRMED_ZERO_EXPORT, ExportState.PROHIBITED,
            }
        ):
            # Live permission can be revoked before the decision is rebuilt.
            # A correct Mode 5 readback cannot authorize prohibited export.
            self._executor.request_stop(reason=ExecutionReason.AUTHORIZATION_LOST)
            await self._commit()
        if not master_stop and self._confirmed_telemetry_wait(frame, now=now):
            # Keep only the already acknowledged command. No write, replan
            # adoption, lease renewal, timestamp refresh or new physical proof.
            return
        if not master_stop and self._pv_initial_input_wait(frame, now=now):
            # FC03 and critical controls are authoritative during startup;
            # transient/missing flow data is pending, never execution proof.
            before = self.record.revision
            self._executor.observe_readback(
                settings_from_execution_source(frame.execution), now=now)
            tx = self.record.transaction
            if tx is not None and tx.readback_result is VerificationStatus.CONFIRMED:
                proof = physical_verification(tx.transaction_id, tx.intent.action,
                    frame.execution, command_sent_at=tx.command_sent_at, now=now)
                self._executor.observe_physical_verification(replace(proof,
                    status=VerificationStatus.PENDING, observed_at=now,
                    evidence=("pv_hold_phase=initial_stabilization",) + proof.evidence), now=now)
            if self.record.revision != before:
                await self._commit()
            return
        try:
            settings = settings_from_execution_source(frame.execution)
        except (ActiveBridgeError, TypeError, ValueError) as exc:
            self._last_retry_evidence = ("invalid_settings", frame.execution)
            self._settings_decode_failure = (now, str(exc)[:160])
            if self.record.state in {
                ActiveState.SELECTED,
                ActiveState.STARTING,
                ActiveState.WAITING_READBACK,
                ActiveState.EXECUTING,
                ActiveState.RETARGETING,
            }:
                self._executor.request_stop(reason=ExecutionReason.STALE_INPUTS)
                await self._commit()
            return
        gates = self._gates(frame)
        previous_retry_evidence = self._last_retry_evidence
        retry_evidence = (settings, gates)
        self._last_retry_evidence = retry_evidence

        current_transaction = self.record.transaction
        if (
            current_transaction is not None
            and current_transaction.owner is ExecutionOwner.TARIFF
            and self.record.state in {
                ActiveState.SELECTED,
                ActiveState.STARTING,
                ActiveState.WAITING_READBACK,
                ActiveState.EXECUTING,
                ActiveState.RETARGETING,
            }
            and now >= self._tariff_planned_boundary(current_transaction)
        ):
            self._executor.request_stop(reason=ExecutionReason.DEADLINE_REACHED)
            await self._commit()

        before_deadline = self.record.revision
        self._executor.check_deadline(now=now)
        if self.record.revision != before_deadline:
            await self._commit()

        for _step in range(MAX_RECONCILE_STEPS):
            state = self.record.state

            transaction = self.record.transaction
            if (
                state in {
                    ActiveState.SELECTED, ActiveState.STARTING,
                    ActiveState.WAITING_READBACK, ActiveState.EXECUTING,
                    ActiveState.RETARGETING,
                }
                and transaction is not None
                and transaction.owner is ExecutionOwner.TARIFF
                and not tariff_command_within_live_bms_limit(
                    transaction.intent, frame.execution, tariff=frame.tariff, now=now,
                )
            ):
                self._executor.request_stop(reason=ExecutionReason.DIRECTION_UNAVAILABLE)
                await self._commit()
                continue

            if (
                state is ActiveState.EXECUTING
                and transaction is not None
                and transaction.owner is ExecutionOwner.RCM
                and transaction.intent.action
                in {
                    ExecutionAction.RCM_ABSORB_PV,
                    ExecutionAction.RCM_ABSORB_AND_LIMIT,
                }
                and not rcm_charge_command_within_live_bms_limit(
                    transaction.intent,
                    frame.execution,
                    rcm=frame.rcm,
                    now=now,
                )
            ):
                self._executor.request_stop(reason=ExecutionReason.BMS_UNAVAILABLE)
                await self._commit()
                continue

            if (
                state is ActiveState.EXECUTING
                and transaction is not None
                and transaction.owner is ExecutionOwner.RCM
                and transaction.intent.action
                is ExecutionAction.RCM_PRE_DISCHARGE
                and not rcm_pre_discharge_command_within_live_bms_limit(
                    transaction.intent,
                    frame.execution,
                    rcm=frame.rcm,
                    now=now,
                )
            ):
                self._executor.request_stop(reason=ExecutionReason.BMS_UNAVAILABLE)
                await self._commit()
                continue

            if (
                frame.decision.supervisor_mode is SupervisorMode.OFF
                and state
                in {
                    ActiveState.SELECTED,
                    ActiveState.STARTING,
                    ActiveState.WAITING_READBACK,
                    ActiveState.EXECUTING,
                    ActiveState.RETARGETING,
                }
            ):
                self._executor.request_stop(
                    reason=ExecutionReason.AUTHORIZATION_LOST
                )
                await self._commit()
                continue

            if state is ActiveState.IDLE:
                if master_stop or frame.decision.supervisor_mode is SupervisorMode.OFF:
                    return
                if (
                    self.record.master_stop_result.status
                    is MasterStopStatus.COMPLETED
                ):
                    if any(candidate.enabled for candidate in frame.candidates):
                        return
                    self._executor.rearm_after_master_stop()
                    await self._commit()
                    return
                intent = build_actuator_intent(
                    frame.decision,
                    frame.candidates,
                    settings,
                    rce=frame.rce,
                    tariff=frame.tariff,
                    rcm=frame.rcm,
                    now=now,
                )
                if intent is None:
                    return
                if (intent.action is ExecutionAction.PV_CHARGE_HOLD
                    and not self._pv_hold_retry_start_allowed(intent, settings, gates, now=now)):
                    return
                if (
                    intent.policy is ExecutionOwner.TARIFF
                    and not self._tariff_retry_start_allowed(
                        intent,
                        settings,
                        gates,
                        now=now,
                    )
                ):
                    return
                transaction_owner = intent.policy.value
                if intent.action is ExecutionAction.PV_CHARGE_HOLD and self._pv_hold_next_retry:
                    transaction_owner = f"pvhold_retry{self._pv_hold_next_retry}"
                if (
                    intent.policy is ExecutionOwner.TARIFF
                    and self._tariff_next_retry_attempt > 0
                ):
                    transaction_owner = (
                        f"tariff_retry{self._tariff_next_retry_attempt}"
                    )
                self._executor.select_intent(
                    intent,
                    transaction_id=_transaction_id(transaction_owner),
                    now=now,
                )
                self._tariff_next_retry_attempt = 0
                self._pv_hold_next_retry = 0
                selected = self.record.transaction
                if selected is not None and selected.owner is ExecutionOwner.TARIFF:
                    self._remember_tariff_planned_end(selected, intent.deadline)
                await self._commit()
                continue

            if state is ActiveState.SELECTED:
                if frame.decision.supervisor_mode is SupervisorMode.OFF:
                    self._executor.request_stop(reason=ExecutionReason.AUTHORIZATION_LOST)
                    await self._commit()
                    continue
                self._executor.start(settings, gates, now=now)
                await self._commit()
                continue

            if state is ActiveState.STARTING:
                current = self._resample_frame(frame)
                pv_start = transaction is not None and transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD
                dispatch_now = _clock_utc(self._clock) if pv_start else None
                if current is None and pv_start:
                    if (transaction.prewrite_snapshot is None
                        and self._pv_start_callback_pending(dispatch_now)):
                        # The existing trailing-edge callback will rebuild the
                        # frame. Do not revoke consent or send from partial data.
                        # Neither that callback nor a replan resets this budget.
                        return
                if not self._command_frame_authorized(current, now=dispatch_now):
                    self._executor.abort_prewrite()
                    await self._commit()
                    return
                assert current is not None
                settings = settings_from_execution_source(current.execution)
                gates = self._gates(current)
                now = _utc(current.now)
                envelope = self._executor.prepare_command(settings, gates, now=now)
                await self._commit()
                if envelope is None:
                    return
                confirmed_not_queued = False
                try:
                    transaction = self.record.transaction
                    assert transaction is not None
                    dispatch_budget = COMMAND_DISPATCH_TIMEOUT_SECONDS
                    if pv_start:
                        dispatch_budget -= (_clock_utc(self._clock) - transaction.started_at).total_seconds()
                        if dispatch_budget <= 0:
                            self._executor.abort_prewrite()
                            await self._commit()
                            return
                    async with asyncio.timeout(dispatch_budget) as dispatch_timeout:
                        for attempt in range(2):
                            try:
                                confirmed_not_queued = False
                                await self._dispatch_envelope(
                                    envelope.atomic_writes,
                                    pre_dispatch_current=lambda: self._command_frame_authorized(
                                        self._resample_frame(frame),
                                        now=_clock_utc(self._clock) if attempt or pv_start else None,
                                    ),
                                )
                                break
                            except AtomicWriteNotQueued as err:
                                if (
                                    len(envelope.atomic_writes) != 1
                                    or envelope.action not in {
                                        ExecutionAction.RCE_EXPORT,
                                        ExecutionAction.PV_CHARGE_HOLD,
                                        ExecutionAction.TARIFF_GRID_SUPPORT,
                                        ExecutionAction.TARIFF_BATTERY_CHARGE,
                                        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
                                    }
                                ):
                                    # Another family may already be queued.
                                    # Preserve the existing uncertain-outcome path.
                                    raise
                                confirmed_not_queued = True
                                if attempt != 0 or err.reason not in {
                                    "stale_snapshot", "stale_snapshot_generation",
                                }:
                                    self._executor.abort_prewrite(
                                        reason=ExecutionReason.COMMAND_NOT_QUEUED
                                    )
                                    await self._commit()
                                    return
                                remaining = COMMAND_DISPATCH_TIMEOUT_SECONDS - (
                                    _clock_utc(self._clock) - transaction.started_at
                                ).total_seconds()
                                if remaining <= 0.0:
                                    self._executor.abort_prewrite(reason=ExecutionReason.COMMAND_NOT_QUEUED)
                                    await self._commit()
                                    return
                                # Tighten the existing timer; never restart it.
                                dispatch_timeout.reschedule(min(
                                    dispatch_timeout.when(),
                                    asyncio.get_running_loop().time() + remaining,
                                ))
                                envelope = await self._reprepare_rejected_ems_start(frame, err)
                                if envelope is None:
                                    if self.record.state is ActiveState.STARTING:
                                        self._executor.abort_prewrite(
                                            reason=ExecutionReason.COMMAND_NOT_QUEUED
                                        )
                                    await self._commit()
                                    return
                                await self._commit()
                    sent_at = _clock_utc(self._clock)
                    self._executor.mark_command_sent(
                        envelope, sent_at=sent_at,
                        lease_identity=self._dispatched_lease_identity(
                            envelope.transaction_id
                        ),
                    )
                    self._remember_sent_market_basis()
                except _PreDispatchRejected as err:
                    if (err.dispatched_count == 0 and attempt == 0 and pv_start
                        and self._resample_frame(frame) is None
                        and self._pv_start_callback_pending(_clock_utc(self._clock))):
                        # Persistence may yield to the same callback after
                        # prepare. No transport was called: retain this start
                        # and reprepare from a fresh frame on the next callback.
                        # Never take this path after a transport rejection; that
                        # would reset its separately bounded retry attempt.
                        self._executor.defer_pv_start_before_dispatch(
                            dispatched_count=err.dispatched_count)
                        await self._commit()
                        return
                    if err.dispatched_count == 0:
                        self._executor.abort_prewrite(reason=(
                            ExecutionReason.COMMAND_NOT_QUEUED
                            if attempt == 1 else ExecutionReason.AUTHORIZATION_LOST
                        ))
                    else:
                        self._executor.mark_command_outcome_unknown()
                    await self._commit()
                    return
                except Exception:
                    _LOGGER.exception("EMS Supervisor command dispatch failed closed")
                    if confirmed_not_queued:
                        self._executor.abort_prewrite(reason=ExecutionReason.COMMAND_NOT_QUEUED)
                    else:
                        self._executor.mark_command_outcome_unknown()
                    await self._commit()
                    return
                await self._commit()
                return

            if state in {
                ActiveState.WAITING_READBACK,
                ActiveState.RETARGETING,
            }:
                transaction = self.record.transaction
                assert transaction is not None
                if transaction.command_sent_at is None:
                    # A persisted prepared retarget proves no transport
                    # outcome.  Never replay it after cancellation/restart;
                    # restore the original transaction baseline instead.
                    self._executor.request_stop(
                        reason=ExecutionReason.AUTHORIZATION_LOST
                    )
                    await self._commit()
                    continue
                authorization_current = (
                    same_window_rce_retarget_wait_authorized(
                        transaction.intent,
                        frame.decision,
                        frame.candidates,
                        settings,
                        export_state=frame.context.export_state,
                        rce=frame.rce,
                        now=now,
                    )
                    if state is ActiveState.RETARGETING
                    else authorization_matches(
                        transaction.intent,
                        frame.decision,
                        frame.candidates,
                        allow_selected_start=True,
                    )
                )
                if (transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD
                    and authorization_current and frame.rce.result_current is True
                    and frame.rce.recalculation_pending is False):
                    self._rce_execution_hold = None
                post_ack_rce_retarget = (
                    build_same_window_rce_retarget(
                        transaction.intent,
                        frame.decision,
                        frame.candidates,
                        settings,
                        rce=frame.rce,
                        now=now,
                    )
                    is not None
                )
                if (
                    transaction.owner is ExecutionOwner.RCE
                    and not rce_sent_command_within_live_bms_limit(
                        transaction.intent,
                        frame.execution,
                        rce=frame.rce,
                        now=now,
                    )
                ):
                    self._executor.request_stop(
                        reason=ExecutionReason.BMS_UNAVAILABLE
                    )
                    await self._commit()
                    continue
                if transaction.owner is ExecutionOwner.TARIFF:
                    tariff_target = same_run_tariff_desired(
                        transaction.intent,
                        frame.decision,
                        frame.candidates,
                        settings,
                        tariff=frame.tariff,
                        now=now,
                    )
                    if tariff_target is not None:
                        self._remember_tariff_planned_end(
                            transaction,
                            tariff_target.planned_end,
                        )
                    authorization_current = tariff_target is not None
                    # Re-evaluate immediately after ACK, including starting
                    # the fixed pending-plan watchdog without another event.
                    post_ack_rce_retarget = True
                    if not authorization_current:
                        authorization_current = same_run_tariff_wait_authorized(
                            transaction.intent,
                            frame.decision,
                            frame.candidates,
                            settings,
                            tariff=frame.tariff,
                            execution_source=frame.execution,
                            now=now,
                            planned_end=self._tariff_planned_boundary(transaction),
                        )
                    if authorization_current:
                        before_hold = self.record.revision
                        ready = self._executor.waiting_same_owner_replan_hold_ready(
                            settings,
                            gates,
                            now=now,
                        )
                        if self.record.revision != before_hold:
                            await self._commit()
                        if not ready:
                            if self.record.state in {
                                ActiveState.STOPPING,
                                ActiveState.FAULT,
                            }:
                                continue
                            return
                if not authorization_current:
                    hold_authorized = (
                        False
                        if state is ActiveState.RETARGETING
                        else same_window_rce_wait_authorized(
                            transaction.intent,
                            frame.decision,
                            frame.candidates,
                            settings,
                            export_state=frame.context.export_state,
                            rce=frame.rce,
                            now=now,
                        )
                    )
                    if transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD:
                        hold_authorized = hold_authorized or self.pv_hold_replan_authorized(frame, now=now)
                    if not hold_authorized:
                        self._executor.request_stop(
                            reason=ExecutionReason.AUTHORIZATION_LOST
                        )
                        await self._commit()
                        continue
                    before_hold = self.record.revision
                    hold_ready = self._executor.waiting_same_owner_replan_hold_ready(
                        settings,
                        gates,
                        now=now,
                    )
                    if self.record.revision != before_hold:
                        await self._commit()
                    if not hold_ready:
                        if self.record.state in {
                            ActiveState.STOPPING,
                            ActiveState.FAULT,
                        }:
                            continue
                        return
                before_readback = self.record.revision
                verdict = self._executor.observe_readback(settings, now=now)
                if self.record.revision != before_readback:
                    await self._commit()
                transaction = self.record.transaction
                assert transaction is not None
                if (
                    self.record.state
                    in {
                        ActiveState.WAITING_READBACK,
                        ActiveState.RETARGETING,
                    }
                    and transaction.owner is ExecutionOwner.RCE
                    and transaction.readback_result
                    is VerificationStatus.CONFIRMED
                ):
                    before_hold = self.record.revision
                    hold_ready = self._executor.waiting_same_owner_replan_hold_ready(
                        settings,
                        gates,
                        now=now,
                    )
                    if self.record.revision != before_hold:
                        await self._commit()
                    if not hold_ready:
                        if self.record.state in {
                            ActiveState.STOPPING,
                            ActiveState.FAULT,
                        }:
                            continue
                        return
                if verdict is not ReadbackVerdict.MATCH:
                    return
                sent_at = transaction.command_sent_at
                if sent_at is None:
                    raise RuntimeError("readback-confirmed transaction has no command time")
                verification = physical_verification(
                    transaction.transaction_id,
                    transaction.intent.action,
                    frame.execution,
                    command_sent_at=sent_at,
                    now=now,
                    export_limit_target_percent=(
                        transaction.intent.command.export_limit_percent_259
                    ),
                    allow_rce_settling=(
                        state is ActiveState.WAITING_READBACK
                        and transaction.owner is ExecutionOwner.RCE
                    ),
                    allow_pv_hold_settling=state is ActiveState.WAITING_READBACK,
                    allow_tariff_settling=transaction.owner is ExecutionOwner.TARIFF,
                )
                if transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD:
                    verification = self._qualify_pv_hold_proof(transaction, frame, verification)
                # Old samples express pending evidence but cannot be submitted
                # as transaction-correlated physical proof.
                if verification.observed_at <= sent_at:
                    return
                before_physical = self.record.revision
                self._executor.observe_physical_verification(
                    verification,
                    now=now,
                )
                if self.record.revision != before_physical:
                    await self._commit()
                if self.record.state in {
                    ActiveState.STOPPING,
                    ActiveState.FAULT,
                }:
                    # A timer-fired frame may be the only event after the
                    # physical proof expires or contradicts the command.
                    # Continue immediately into the existing fail-closed
                    # restore path; never depend on unrelated HA churn.
                    continue
                if (
                    self.record.state is ActiveState.EXECUTING
                    and post_ack_rce_retarget
                ):
                    # The same coherent adapter frame may already contain a
                    # newer target for this exact RCE run.  Finish proving A,
                    # then let the normal EXECUTING branch validate and send
                    # only the latest successor without waiting for an
                    # unrelated Home Assistant event.
                    continue
                return

            if state is ActiveState.EXECUTING:
                transaction = self.record.transaction
                assert transaction is not None
                previous = transaction.physical_verification
                assert previous is not None
                flow_filter_end = self.tariff_support_flow_filter_deadline
                if flow_filter_end is not None and now >= flow_filter_end:
                    self._executor.request_stop(
                        reason=ExecutionReason.PHYSICAL_CONTRADICTION,
                        physical_verification=self._tariff_support_flow_filter[3],
                    )
                    await self._commit()
                    continue
                rce_export_continuation = (
                    transaction.owner is ExecutionOwner.RCE
                    and transaction.intent.action is ExecutionAction.RCE_EXPORT
                )
                continuation_maximum_age = (
                    RCE_EXECUTING_EMS_MAX_AGE_SECONDS
                    if rce_export_continuation
                    else MAX_READBACK_AGE_SECONDS
                )
                continuation_gates = self._gates(
                    frame,
                    maximum_readback_age_seconds=continuation_maximum_age,
                )
                authorization_current = authorization_matches(
                    transaction.intent,
                    frame.decision,
                    frame.candidates,
                )
                if transaction.owner is ExecutionOwner.TARIFF and (
                    frame.tariff.result_current is not True
                    or frame.tariff.recalculation_pending is not False
                ):
                    authorization_current = False
                if (
                    rce_export_continuation
                    and frame.rce.result_current is False
                    and frame.rce.recalculation_pending is True
                ):
                    # Readiness helpers may still describe the previous plan.
                    # Every explicit replan uses the same fixed hold, even if
                    # the candidate temporarily remains continuation-eligible.
                    authorization_current = False
                if authorization_current:
                    self._rce_execution_hold = None
                    self._tariff_execution_hold = None
                    settling_deadline = self.post_command_settling_deadline
                    if settling_deadline is not None and now >= settling_deadline:
                        self._post_command_settling_key = None
                if not authorization_current:
                    if transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD:
                        authorization_current = self.pv_hold_replan_authorized(frame, now=now)
                    retarget = build_same_window_rce_retarget(
                        transaction.intent,
                        frame.decision,
                        frame.candidates,
                        settings,
                        rce=frame.rce,
                        now=now,
                    )
                    if transaction.owner is ExecutionOwner.TARIFF:
                        retarget = same_run_tariff_desired(
                            transaction.intent,frame.decision,frame.candidates,settings,
                            tariff=frame.tariff,now=now,
                        )
                        if retarget is not None:
                            self._remember_tariff_planned_end(
                                transaction,
                                retarget.planned_end,
                            )
                        if retarget is not None and retarget.intent.command == transaction.intent.command:
                            self._tariff_execution_hold = None
                            if retarget.intent == transaction.intent:
                                authorization_current = True
                            else:
                                proof = physical_verification(
                                    transaction.transaction_id,retarget.intent.action,frame.execution,
                                    command_sent_at=transaction.command_sent_at,now=now,
                                )
                                if (retarget.intent.action is transaction.intent.action
                                    and self._tariff_support_excursion_pending(transaction, proof, now=now)):
                                    # A completed replan authorizes the same
                                    # block, but cannot refresh physical proof
                                    # from a pulse. Run the normal authority
                                    # gates below before deferring the stop.
                                    authorization_current = True
                                else:
                                    self._executor.adopt_tariff_action_without_write(
                                        retarget.intent,settings,
                                        self._gates_for_candidate(frame,retarget.candidate),
                                        proof,now=now,
                                    )
                                    await self._commit()
                                    return
                            retarget = None
                    if (
                        retarget is not None
                        and self._retarget_delayed_only_by_ems_age(
                            settings,
                            now=now,
                        )
                        and rce_sent_command_within_live_bms_limit(
                            transaction.intent,
                            frame.execution,
                            rce=frame.rce,
                            now=now,
                        )
                    ):
                        # A write still needs the strict 15-second snapshot.
                        # Keep proving the old command until the next complete
                        # FC03 cohort, without dispatching or renewing its lease.
                        self._rce_execution_hold = None
                        authorization_current = True
                        retarget = None
                    if retarget is not None:
                        if (
                            transaction.owner is ExecutionOwner.TARIFF
                            and not tariff_command_within_live_bms_limit(
                                retarget.intent, frame.execution, tariff=frame.tariff, now=now,
                            )
                        ):
                            self._executor.request_stop(reason=ExecutionReason.DIRECTION_UNAVAILABLE)
                            await self._commit()
                            continue
                        self._rce_execution_hold = None
                        predecessor = transaction
                        attempt_end = self._same_owner_retarget_attempt_end(
                            predecessor,
                            now=now,
                        )
                        if attempt_end is None:
                            # No successor state or lease is created when the
                            # predecessor has too little immutable authority
                            # left for persist + transport + local handling.
                            return
                        retarget_gates = self._gates_for_candidate(
                            frame,
                            retarget.candidate,
                        )
                        envelope = self._executor.prepare_same_owner_retarget(
                            retarget.intent,
                            settings,
                            retarget_gates,
                            now=now,
                        )
                        confirmed_not_queued = False
                        transport_started = False

                        def mark_transport_started() -> None:
                            nonlocal transport_started
                            transport_started = True

                        try:
                            async with asyncio.timeout(
                                self._same_owner_retarget_remaining(
                                    attempt_end,
                                    now=now,
                                )
                            ):
                                # Persist is part of the same non-renewable
                                # budget.  A slow Store must not leave a fresh
                                # 30-second transport window afterward.
                                await self._commit()
                                if envelope is None:
                                    if self.record.state in {
                                        ActiveState.STOPPING,
                                        ActiveState.FAULT,
                                    }:
                                        continue
                                    return
                                for attempt in range(2):
                                    try:
                                        confirmed_not_queued = False
                                        await self._dispatch_envelope(
                                            envelope.atomic_writes,
                                            pre_dispatch_current=lambda: (
                                                self._retarget_dispatch_frame_authorized(
                                                    frame,
                                                    attempt_end=attempt_end,
                                                )
                                            ),
                                            on_dispatch_started=mark_transport_started,
                                        )
                                        break
                                    except AtomicWriteNotQueued as err:
                                        if len(envelope.atomic_writes) != 1:
                                            raise
                                        confirmed_not_queued = True
                                        if (
                                            predecessor.owner in {
                                                ExecutionOwner.RCE,
                                                ExecutionOwner.TARIFF,
                                            }
                                            and attempt == 0
                                            and err.reason in {
                                                "stale_snapshot",
                                                "stale_snapshot_generation",
                                            }
                                        ):
                                            envelope = await self._reprepare_rejected_same_owner_retarget(
                                                predecessor,
                                                frame,
                                                err,
                                                attempt_end=attempt_end,
                                            )
                                            if envelope is not None:
                                                await self._commit()
                                                continue
                                        if (
                                            predecessor.owner in {
                                                ExecutionOwner.RCE,
                                                ExecutionOwner.TARIFF,
                                            }
                                            and err.reason == "retarget_not_authorized"
                                        ):
                                            # The leased adapter rechecks after its
                                            # challenge await. A publication may
                                            # invalidate unsent B while confirmed A
                                            # remains valid. Use the same fresh
                                            # predecessor attestation as a rejection
                                            # before dispatch; never replay B or
                                            # extend A's lease/deadline here.
                                            self._recover_proven_unsent_retarget(
                                                predecessor,
                                                observed_now=_clock_utc(self._clock),
                                                fallback=frame,
                                                rejection=err,
                                            )
                                            await self._commit()
                                            return
                                        successor = self.record.transaction
                                        latest = self._resample_frame(frame)
                                        self._log_unsent_rce_retarget(
                                            "rejected",
                                            predecessor,
                                            frame=latest,
                                            observed_now=now,
                                            successor=successor,
                                            rejection=err,
                                        )
                                        if self.record.state in {
                                            ActiveState.RETARGETING,
                                            ActiveState.EXECUTING,
                                        }:
                                            self._executor.request_stop(
                                                reason=ExecutionReason.AUTHORIZATION_LOST
                                            )
                                        await self._commit()
                                        return
                            sent_at = _clock_utc(self._clock)
                            if sent_at >= attempt_end:
                                raise _RetargetAttemptExpired()
                            self._executor.mark_retarget_sent(
                                envelope,
                                sent_at=sent_at,
                                lease_identity=self._dispatched_lease_identity(
                                    envelope.transaction_id
                                ),
                            )
                            self._remember_sent_market_basis()
                            self._tariff_execution_hold = None
                        except _PreDispatchRejected as err:
                            if err.dispatched_count == 0:
                                self._recover_proven_unsent_retarget(
                                    predecessor,
                                    observed_now=now,
                                    fallback=frame,
                                    rejection=err,
                                )
                            else:
                                self._executor.mark_command_outcome_unknown()
                            await self._commit()
                            return
                        except (TimeoutError, _RetargetAttemptExpired):
                            _LOGGER.warning(
                                "EMS retarget attempt expired owner=%s transaction_id=%s "
                                "transport_started=%s proven_not_queued=%s attempt_end=%s",
                                predecessor.owner.value,
                                predecessor.transaction_id,
                                transport_started,
                                confirmed_not_queued,
                                _iso(attempt_end),
                            )
                            if not transport_started or confirmed_not_queued:
                                self._recover_proven_unsent_retarget(
                                    predecessor,
                                    observed_now=_clock_utc(self._clock),
                                    fallback=frame,
                                )
                            else:
                                self._executor.mark_command_outcome_unknown()
                            await self._commit()
                            return
                        except Exception:
                            _LOGGER.exception(
                                "EMS Supervisor retarget dispatch failed closed"
                            )
                            if not transport_started or confirmed_not_queued:
                                self._recover_proven_unsent_retarget(
                                    predecessor,
                                    observed_now=_clock_utc(self._clock),
                                    fallback=frame,
                                )
                            else:
                                self._executor.mark_command_outcome_unknown()
                            await self._commit()
                            return
                        await self._commit()
                        return
                    recognized_settling = self._post_command_settling_key == (
                        transaction.transaction_id, transaction.command_sent_at
                    )
                    if not authorization_current and self._post_command_measurement_hold(
                        frame, settings, now=now,
                    ):
                        authorization_current = True
                    hold_seconds = (
                        same_window_rce_execution_hold_seconds(
                            transaction.intent,
                            frame.decision,
                            frame.candidates,
                            settings,
                            execution_source=frame.execution,
                            export_state=frame.context.export_state,
                            rce=frame.rce,
                            now=now,
                        )
                        if not authorization_current and not recognized_settling
                        else None
                    )
                    if hold_seconds is not None and self._bounded_rce_execution_hold(
                        transaction.transaction_id,
                        duration_seconds=hold_seconds,
                        now=now,
                    ):
                        # Only retain the old, physically confirmed command
                        # while the new HA cohort settles.  This grants no
                        # replacement write and does not renew the lease.
                        authorization_current = True
                    if not authorization_current and same_run_tariff_wait_authorized(
                        transaction.intent,frame.decision,frame.candidates,settings,
                        tariff=frame.tariff,execution_source=frame.execution,now=now,
                        planned_end=self._tariff_planned_boundary(transaction),
                    ):
                        authorization_current = self._bounded_tariff_execution_hold(
                            transaction,
                            now=now,
                        )
                if (
                    rce_export_continuation
                    and not rce_sent_command_within_live_bms_limit(
                        transaction.intent, frame.execution, rce=frame.rce, now=now,
                    )
                ):
                    # A delayed HA readiness helper cannot authorize the
                    # already-sent block after the raw discharge cap falls.
                    # A fresh feasible successor was considered above first.
                    self._executor.request_stop(reason=ExecutionReason.BMS_UNAVAILABLE)
                    await self._commit()
                    continue
                before_authority = self.record.revision
                authority_current = self._executor.check_continuation_authority(
                    settings,
                    continuation_gates,
                    authorization_current=authorization_current,
                    now=now,
                    maximum_readback_age_seconds=continuation_maximum_age,
                )
                if self.record.revision != before_authority:
                    await self._commit()
                if not authority_current:
                    if self.record.state in {
                        ActiveState.STOPPING,
                        ActiveState.FAULT,
                    }:
                        continue
                    return
                verification = physical_verification(
                    transaction.transaction_id,
                    transaction.intent.action,
                    frame.execution,
                    command_sent_at=transaction.command_sent_at,  # type: ignore[arg-type]
                    now=now,
                    export_limit_target_percent=(
                        transaction.intent.command.export_limit_percent_259
                    ),
                    rce_confirmed_continuation=rce_export_continuation,
                )
                physical_maximum_age = (
                    RCE_CONFIRMED_PHYSICAL_MAX_AGE_SECONDS
                    if rce_export_continuation
                    else TARIFF_PHYSICAL_MAX_AGE_SECONDS
                    if transaction.owner is ExecutionOwner.TARIFF
                    else MAX_READBACK_AGE_SECONDS
                )
                if self._tariff_support_excursion_pending(transaction, verification, now=now):
                    # Keep the original confirmed proof/command/deadline. Fresh
                    # contradictory cohorts and replans never move this anchor;
                    # the HA lease gate separately rejects such renewals.
                    self._tariff_support_flow_filter = (
                        transaction.transaction_id, transaction.command_sent_at,
                        previous.observed_at, verification,
                    )
                    return
                if verification.status is VerificationStatus.CONFIRMED and verification.observed_at > previous.observed_at:
                    self._tariff_support_flow_filter = None
                if (
                    transaction.owner is ExecutionOwner.TARIFF
                    and verification.status is VerificationStatus.PENDING
                    and verification.evidence == ("physical_cohort_incomplete",)
                    and 0.0 <= (now - previous.observed_at).total_seconds()
                    < TARIFF_PHYSICAL_MAX_AGE_SECONDS
                ):
                    # A faster BAT channel is not a new coherent GRID/PV/LOAD
                    # proof. Retain the last confirmed cohort without renewing
                    # its watchdog or issuing any new command.
                    return
                if verification.observed_at <= previous.observed_at:
                    if verification.status in {
                        VerificationStatus.CONTRADICTED,
                        VerificationStatus.UNAVAILABLE,
                    }:
                        reason = (
                            ExecutionReason.PHYSICAL_CONTRADICTION
                            if verification.status
                            is VerificationStatus.CONTRADICTED
                            else ExecutionReason.PHYSICAL_UNAVAILABLE
                        )
                        self._executor.request_stop(
                            reason=reason,
                            physical_verification=verification,
                        )
                        await self._commit()
                        continue
                    proof_age = (now - previous.observed_at).total_seconds()
                    proof_expired = (
                        proof_age > physical_maximum_age
                        if rce_export_continuation
                        else proof_age >= physical_maximum_age
                    )
                    if proof_expired:
                        self._executor.request_stop(
                            reason=ExecutionReason.PHYSICAL_UNAVAILABLE
                        )
                        await self._commit()
                        continue
                    return
                before_continuation = self.record.revision
                self._executor.check_continuation(
                    settings,
                    continuation_gates,
                    verification,
                    authorization_current=authorization_current,
                    now=now,
                    maximum_readback_age_seconds=continuation_maximum_age,
                )
                if self.record.revision != before_continuation:
                    await self._commit()
                if self.record.state is ActiveState.STOPPING:
                    continue
                return

            if state is ActiveState.BLOCKED:
                transaction = self.record.transaction
                if transaction is not None and transaction.owner.value != "none":
                    return
                if frame.decision.supervisor_mode is SupervisorMode.OFF:
                    if transaction is not None:
                        self._executor.request_stop(
                            reason=ExecutionReason.AUTHORIZATION_LOST
                        )
                        await self._commit()
                    return
                if self.record.master_stop_result.status in {
                    MasterStopStatus.BLOCKED,
                    MasterStopStatus.FAILED,
                }:
                    return
                if transaction is None:
                    return
                current = build_actuator_intent(
                    frame.decision,
                    frame.candidates,
                    settings,
                    rce=frame.rce,
                    tariff=frame.tariff,
                    rcm=frame.rcm,
                    now=now,
                )
                if current is None:
                    return
                candidate_changed = (
                    current.candidate_revision
                    != transaction.intent.candidate_revision
                )
                evidence_changed = (
                    initial_state is ActiveState.BLOCKED
                    and (
                        previous_retry_evidence is None
                        or previous_retry_evidence != retry_evidence
                    )
                    and self.record.reason in _RETRYABLE_BLOCK_REASONS
                )
                authorization_restored = (
                    initial_state is ActiveState.BLOCKED
                    and self.record.reason is ExecutionReason.AUTHORIZATION_LOST
                )
                if (
                    not candidate_changed
                    and not evidence_changed
                    and not authorization_restored
                ):
                    return
                if not self._executor.blocked_retry_ready(
                    current,
                    settings,
                    gates,
                    now=now,
                ):
                    return
                self._executor.reset_blocked()
                await self._commit()
                return

            if state in {ActiveState.STOPPING, ActiveState.FAULT}:
                transaction = self.record.transaction
                assert transaction is not None
                if transaction.rollback_status is RollbackStatus.FAILED:
                    return
                current = self._resample_frame(frame)
                if current is None:
                    return
                try:
                    settings = settings_from_execution_source(current.execution)
                except (ActiveBridgeError, TypeError, ValueError):
                    return
                gates = self._gates(current)
                now = _utc(current.now)
                master_stop_record = transaction.master_stop_requested
                if (
                    transaction.restore_not_queued_count > 0
                    and self._terminal_proof is not None
                ):
                    try:
                        terminal = await self._terminal_proof()
                    except Exception:
                        terminal = None
                    prior_record = self.record
                    if terminal is not None and self._executor.settle_late_terminal_fallback(
                        settings, terminal, now=now
                    ):
                        try:
                            if master_stop_record:
                                await self._commit_master_stop(force_retry=True)
                            else:
                                await self._commit()
                        except Exception:
                            # Persistence is part of owner release. A failed
                            # Store write must keep the old fault in memory.
                            self._executor = SupervisorExecutor(prior_record)
                            raise
                        return
                prior_restore_record = self.record
                envelope = self._executor.prepare_restore(settings, gates, now=now)
                if self.record != prior_restore_record or not self.record_persisted:
                    if master_stop_record:
                        await self._commit_master_stop()
                    else:
                        await self._commit()
                if envelope is None:
                    return
                try:
                    async with asyncio.timeout(RESTORE_DISPATCH_TIMEOUT_SECONDS):
                        await self._dispatch_envelope(
                            envelope.atomic_writes,
                            pre_dispatch_current=lambda: self._restore_frame_current(
                                self._resample_frame(frame)
                            ),
                        )
                    sent_at = _clock_utc(self._clock)
                    self._executor.mark_restore_sent(envelope, sent_at=sent_at)
                except AtomicWriteNotQueued as err:
                    if (
                        err.reason in {
                            "previous_write_pending",
                            "stale_snapshot",
                            "stale_snapshot_generation",
                        }
                        and len(envelope.atomic_writes) == 1
                    ):
                        self._executor.mark_restore_not_queued(
                            err, now=now
                        )
                    else:
                        self._executor.mark_restore_outcome_unknown()
                    if master_stop_record:
                        await self._commit_master_stop()
                    else:
                        await self._commit()
                    return
                except _PreDispatchRejected as err:
                    if err.dispatched_count > 0:
                        self._executor.mark_restore_outcome_unknown()
                        if master_stop_record:
                            await self._commit_master_stop()
                        else:
                            await self._commit()
                    return
                except Exception:
                    self._executor.mark_restore_outcome_unknown()
                    if master_stop_record:
                        await self._commit_master_stop(force_retry=True)
                    else:
                        await self._commit()
                    return
                if master_stop_record:
                    await self._commit_master_stop()
                else:
                    await self._commit()
                return

            if state is ActiveState.RESTORING:
                transaction = self.record.transaction
                assert transaction is not None
                verdict = self._executor.observe_restore_readback(settings, now=now)
                if verdict is not ReadbackVerdict.PENDING:
                    if transaction.master_stop_requested:
                        await self._commit_master_stop(force_retry=True)
                    else:
                        await self._commit()
                return

            raise RuntimeError(f"unsupported Active state: {state.value}")

        raise RuntimeError("Active reconciliation exceeded its transition bound")

    def _tariff_retry_start_allowed(
        self,
        intent: Any,
        settings: SettingsSnapshot,
        gates: Any,
        *,
        now: datetime,
    ) -> bool:
        """Allow at most one evidence-backed retry in one tariff deadline.

        A planner revision or a newer FC03 generation alone never clears a
        physical/readback failure.  The retry needs a confirmed restore, a
        newer coherent neutral FC03 cohort and a short cadence-derived spacing.
        A different hard deadline is a new independent tariff window.
        """

        self._tariff_next_retry_attempt = 0
        previous = self.record.last_transaction
        if (
            previous is None
            or previous.intent.policy is not ExecutionOwner.TARIFF
        ):
            self._tariff_retry_status = {
                "allowed": True,
                "reason": "no_previous_tariff_failure",
                "attempts": 0,
                "maximum_attempts": TARIFF_RETRY_MAX_ATTEMPTS_PER_WINDOW,
                "retry_after": None,
            }
            return True
        retry_reasons = {
            ExecutionReason.READBACK_MISMATCH,
            ExecutionReason.PHYSICAL_CONTRADICTION,
            ExecutionReason.PHYSICAL_UNAVAILABLE,
            ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
        }
        if previous.reason not in retry_reasons:
            self._tariff_retry_status = {
                "allowed": True,
                "reason": "previous_tariff_end_not_retry_guarded",
                "attempts": 0,
                "maximum_attempts": TARIFF_RETRY_MAX_ATTEMPTS_PER_WINDOW,
                "retry_after": None,
            }
            return True
        if intent.deadline != previous.deadline:
            self._tariff_retry_status = {
                "allowed": True,
                "reason": "independent_tariff_window",
                "attempts": 0,
                "maximum_attempts": TARIFF_RETRY_MAX_ATTEMPTS_PER_WINDOW,
                "retry_after": None,
            }
            return True
        attempts = int(
            previous.transaction_id.startswith("tariff_retry1:")
        )
        reference = (
            previous.restore_sent_at
            or (
                previous.physical_verification.observed_at
                if previous.physical_verification is not None
                else None
            )
            or previous.command_sent_at
            or previous.started_at
        )
        retry_after = reference + timedelta(
            seconds=TARIFF_RETRY_MIN_INTERVAL_SECONDS
        )
        neutral_evidence = bool(
            previous.rollback_status in {
                RollbackStatus.CONFIRMED,
                RollbackStatus.NOT_REQUIRED,
            }
            and previous.rollback_result
            in {VerificationStatus.CONFIRMED, VerificationStatus.PENDING}
            and settings.ems_coherent
            and settings.ems_block.mode is EmsMode.SELF_USE
            and settings.ems_observed_at > reference
            and gates.inputs_fresh
            and gates.bms_fresh
            and gates.full_block_ready
            and gates.full_block_topology_ready
            and gates.charge_direction_ready
            and not gates.owner_conflict
            and gates.observed_owner in {None, ExecutionOwner.NONE}
        )
        if attempts >= TARIFF_RETRY_MAX_ATTEMPTS_PER_WINDOW:
            reason = "retry_budget_exhausted"
            allowed = False
        elif now < retry_after:
            reason = "retry_spacing_active"
            allowed = False
        elif not neutral_evidence:
            reason = "neutral_evidence_incomplete"
            allowed = False
        else:
            attempts += 1
            self._tariff_next_retry_attempt = attempts
            reason = "bounded_retry_allowed"
            allowed = True
        self._tariff_retry_status = {
            "allowed": allowed,
            "reason": reason,
            "attempts": attempts,
            "maximum_attempts": TARIFF_RETRY_MAX_ATTEMPTS_PER_WINDOW,
            "retry_after": retry_after,
        }
        return allowed

    def _gates(
        self,
        frame: ActiveFrame,
        *,
        maximum_readback_age_seconds: float = MAX_READBACK_AGE_SECONDS,
    ):
        try:
            candidate = selected_candidate(frame.decision, frame.candidates)
        except ActiveBridgeError:
            candidate = None
        return execution_gates(
            frame.execution,
            owner_kind=frame.context.owner_kind,
            owner_conflict=frame.context.owner_conflict,
            now=frame.now,
            candidate=candidate,
            maximum_readback_age_seconds=maximum_readback_age_seconds,
        )

    def _pv_hold_startup_controls_ready(self, frame, *, now):
        """Retain only an ACKed SOC+1/1% command inside its fixed 180 s budget.

        The inverter's startup flow reports may disagree, disappear, or briefly
        reverse. They grant neither execution nor extra time. Fresh full FC03,
        live critical BMS gates, the exact plan and consent remain mandatory.
        """
        tx = self.record.transaction
        if (self.record.state is not ActiveState.WAITING_READBACK or tx is None
            or tx.intent.action is not ExecutionAction.PV_CHARGE_HOLD
            or tx.command_sent_at is None
            or frame.decision.supervisor_mode is not SupervisorMode.ACTIVE
            or frame.context.owner_conflict
            or frame.context.owner_kind.value not in {'none', tx.owner.value}
            or frame.context.export_state is not ExportState.VERIFIED_ALLOWED
            or not tx.command_sent_at <= now < min(tx.deadline, tx.command_sent_at
                + timedelta(seconds=command_wait_timeout(tx)))):
            return False
        if __package__:
            from .pv_charge_delay_control import pending_same_command
        else:
            from pv_charge_delay_control import pending_same_command
        current = authorization_matches(tx.intent, frame.decision, frame.candidates,
                                        allow_selected_start=True)
        pending = pending_same_command(tx.intent, frame.rce, now=now)
        if not current and not pending:
            return False
        source, block = frame.execution, tx.intent.command.ems_block
        try:
            settings = settings_from_execution_source(source)
            gates = self._gates(frame)
            if (block is None or settings.ems_block != block
                or not tx.command_sent_at < settings.ems_observed_at <= now
                or not gates.inputs_fresh or not gates.full_block_ready
                or not gates.full_block_topology_ready or not gates.bms_fresh
                or not gates.charge_direction_ready or not gates.discharge_direction_ready
                or block.maximum_discharge_power_percent_4306 != 1.):
                return False
            system_kw = frame.rce.system_power_kw
            if (type(system_kw) not in (int,float) or not isfinite(system_kw)
                or system_kw <= 0
                or system_kw*.01 > source.bms_voltage_v
                    * source.bms_max_discharge_current_a*.95/1000.
                or not 0 <= source.battery_soc_percent < block.force_discharge_soc_percent_4305):
                return False
            if pending:
                if not self._bounded_rce_execution_hold(tx.transaction_id, now=now,
                        duration_seconds=RCE_RECALCULATION_HOLD_SECONDS):
                    return False
                if now >= self._rce_execution_hold[1] + timedelta(seconds=RCE_RECALCULATION_HOLD_SECONDS):
                    return False
            return True
        except (ActiveBridgeError,TypeError,ValueError):
            return False

    def _pv_initial_input_wait(self, frame, *, now):
        """Wait on startup flows only while a real existing lease is alive."""
        if self._control_lease_valid_until is None:
            return False
        lease_end = self._control_lease_valid_until()
        if lease_end is None or now >= lease_end or not self._pv_hold_startup_controls_ready(frame, now=now):
            return False
        tx = self.record.transaction
        proof = physical_verification(tx.transaction_id, tx.intent.action,
            frame.execution, command_sent_at=tx.command_sent_at, now=now)
        if proof.status is VerificationStatus.CONFIRMED:
            return False
        self._pv_hold_first_proof = None
        self._telemetry_hold_until = min(lease_end, self.lease_policy_deadline(now=now))
        return now < self._telemetry_hold_until

    def _pv_hold_retry_start_allowed(self, intent, settings, gates, *, now):
        """Requalify a neutral PV attempt after a fixed three-minute cooldown.

        Completed attempts never grant authority to the next one. Its full
        current plan is checked normally, including its own hard deadline.
        Replans cannot slide this cooldown or alter a running transaction.
        The durable previous transaction preserves spacing through restart.
        """
        self._pv_hold_next_retry = 0
        previous = self.record.last_transaction
        if previous is None or previous.intent.action is not ExecutionAction.PV_CHARGE_HOLD:
            self._pv_hold_retry_status = {"allowed": True, "reason": "new_pv_episode"}
            return True
        reference = _pv_hold_retry_reference(previous)
        retry_after = reference + timedelta(seconds=PV_HOLD_RETRY_INTERVAL_SECONDS)
        # A rejected start never acquired the device and has nothing to restore.
        # Require the complete persisted pre-command shape and an unchanged
        # neutral snapshot; a missing sent timestamp alone is not proof of no I/O.
        rejected_before_prepare = (
            (previous.reason is ExecutionReason.DIRECTION_UNAVAILABLE
             and previous.expected_readback is None)
            or (previous.reason is ExecutionReason.AUTHORIZATION_LOST
                and previous.command_snapshot is not None
                and previous.expected_readback == ExpectedReadback.from_command(
                    previous.command_snapshot, previous.intent.command))
        )
        never_issued = bool(
            rejected_before_prepare
            and previous.state is ActiveState.IDLE
            and previous.owner is ExecutionOwner.NONE
            and previous.rollback_status is RollbackStatus.NOT_REQUIRED
            and previous.rollback_result is VerificationStatus.PENDING
            and previous.command_sent_at is None
            and previous.prewrite_snapshot is None
            and previous.lease_identity is None
            and previous.physical_verification is None
            and previous.readback_result is VerificationStatus.PENDING
            and previous.restore_command is None
            and previous.restore_snapshot is None
            and previous.restore_expected_readback is None
            and previous.restore_sent_at is None
            and previous.restore_attempts == 0
            and not previous.master_stop_requested
            and previous.command_snapshot is not None
            and previous.command_snapshot.ems_coherent
            and previous.command_snapshot.ems_block.mode is EmsMode.SELF_USE
            and previous.command_snapshot.values_match(settings,
                required_families=frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK}))
        )
        restored = (previous.rollback_status is RollbackStatus.CONFIRMED
            and previous.rollback_result is VerificationStatus.CONFIRMED)
        neutral = ((restored or never_issued)
            and previous.state is ActiveState.IDLE and previous.owner is ExecutionOwner.NONE
            and not previous.master_stop_requested and not previous.off_grid_preserved
            and settings.ems_coherent and settings.ems_block.mode is EmsMode.SELF_USE
            and settings.ems_observed_at > reference
            and gates.inputs_fresh and gates.bms_fresh and gates.full_block_ready
            and gates.full_block_topology_ready and gates.pv_charge_hold_ready
            and not gates.owner_conflict and gates.observed_owner in {None, ExecutionOwner.NONE})
        prefix = previous.transaction_id.split(':', 1)[0]
        prior_attempt = prefix.removeprefix('pvhold_retry')
        attempt = int(prior_attempt) + 1 if prefix.startswith('pvhold_retry') and prior_attempt.isdigit() else 1
        if now < retry_after:
            reason = "retry_spacing_active"
        elif not neutral:
            reason = "neutral_evidence_incomplete"
        else:
            reason = "bounded_retry_allowed"
            self._pv_hold_next_retry = attempt
        self._pv_hold_retry_status = {"allowed": bool(self._pv_hold_next_retry), "reason": reason,
            "retry_after": _iso(retry_after), "previous_deadline": _iso(previous.deadline),
            "current_plan_deadline": _iso(intent.deadline), "next_attempt": attempt,
            "retry_policy": "neutral_requalification_after_cooldown",
            "neutral_basis": "confirmed_restore" if restored else
                "start_denied_before_command" if never_issued else "unproven",
            "maximum_retries": None, "minimum_interval_seconds": PV_HOLD_RETRY_INTERVAL_SECONDS}
        return bool(self._pv_hold_next_retry)

    def _confirmed_telemetry_wait(self, frame, *, now):
        """Allow a bounded report gap without hiding any observed veto.

        This retains existing authority only. Missing data cannot start or
        renew a command; the original ESP lease and last real proof cap it.
        """
        self._telemetry_hold_until = None
        tx, cached = self.record.transaction, self._confirmed_telemetry_frame
        if (self.record.state is not ActiveState.EXECUTING or tx is None or cached is None
            or self.tariff_support_flow_filter_deadline is not None
            or tx.command_sent_at is None or cached[:2] != (tx.transaction_id, tx.command_sent_at)
            or self._control_lease_valid_until is None
            or frame.decision.supervisor_mode is not SupervisorMode.ACTIVE
            or frame.context.owner_conflict
            or frame.context.owner_kind.value not in {'none', tx.owner.value}
            or tx.intent.command.ems_block is None
            or tx.intent.command.ems_block.mode not in {EmsMode.GRID_CHARGE, EmsMode.GRID_DISCHARGE}):
            return False
        lease_end = self._control_lease_valid_until()
        proof = tx.physical_verification
        if lease_end is None or proof is None or proof.status is not VerificationStatus.CONFIRMED:
            return False
        end = min(lease_end, self.lease_policy_deadline(now=now),
                  proof.observed_at + timedelta(seconds=CONFIRMED_TELEMETRY_GRACE_SECONDS),
                  cached[2].now + timedelta(seconds=CONFIRMED_TELEMETRY_GRACE_SECONDS))
        if not cached[2].now <= now < end:
            return False
        source, previous, block = frame.execution, cached[2].execution, tx.intent.command.ems_block
        def number(value):
            return type(value) in (int,float) and isfinite(value)
        def recent(stamp):
            return isinstance(stamp,datetime) and stamp.tzinfo is not None and 0 <= (now-stamp).total_seconds() <= 15
        # Reuse the action's real flow predicate; RCE and tariff deliberately
        # require different cohorts. Never turn a valid RCE battery report
        # into a missing-PV hold that would suppress every renewal at night.
        current_proof = physical_verification(tx.transaction_id, tx.intent.action,
            source, command_sent_at=tx.command_sent_at, now=now,
            rce_confirmed_continuation=True)
        if (current_proof.status is VerificationStatus.CONTRADICTED
            or current_proof.status is VerificationStatus.CONFIRMED
                and recent(source.full_block_generation_at)):
            return False
        if source.hardware_readback_supported is False:
            return False
        for name in ('physical_mode_code','self_use_soc_percent','backup_soc_percent',
                     'force_charge_soc_percent','maximum_charge_power_percent','force_discharge_soc_percent',
                     'maximum_discharge_power_percent','machine_type_code','inverter_count',
                     'gcf_enable_code','effective_export_limit_percent'):
            value = getattr(source,name)
            if value is not None and (not number(value) or value != getattr(previous,name)):
                return False
        bms = {}
        bms_names = ['bms_voltage_v', 'bms_max_charge_current_a' if block.mode is EmsMode.GRID_CHARGE else 'bms_max_discharge_current_a']
        if tx.intent.action is ExecutionAction.PV_CHARGE_HOLD:
            bms_names.append('bms_max_charge_current_a')
        for name in bms_names:
            value = getattr(source,name)
            if value is None:
                value = getattr(previous,name)
            if not number(value) or value <= 0:
                return False
            bms[name] = value
        policy = frame.rce if tx.owner is ExecutionOwner.RCE else frame.tariff if tx.owner is ExecutionOwner.TARIFF else frame.rcm
        if getattr(policy,'allowed_by_user',None) is not True or getattr(policy,'enabled',None) is not True:
            return False
        # Voltage naturally varies; only a cap insufficient for the actual
        # sent power is a veto. Missing values reuse the bounded last proof.
        system_kw = getattr(policy, 'system_power_kw', None)
        if tx.owner in {ExecutionOwner.RCE, ExecutionOwner.TARIFF}:
            if not number(system_kw) or system_kw <= 0:
                return False
            charging = block.mode is EmsMode.GRID_CHARGE
            percent = block.maximum_charge_power_percent_4304 if charging else block.maximum_discharge_power_percent_4306
            current = bms['bms_max_charge_current_a' if charging else 'bms_max_discharge_current_a']
            budget = bms['bms_voltage_v'] * current * (1.0 if charging else .95) / 1000.
            if percent * system_kw / 100. > budget + .05:
                return False
        elif tx.owner is ExecutionOwner.RCM:
            # RCEm owns its own power/plan contract. Reuse its existing gate
            # with the last known values only where the report is missing;
            # retain their real timestamps and never manufacture fresh proof.
            names = ('hardware_readback_supported', 'machine_type_code',
                     'inverter_count', 'topology_generation_at', 'bms_voltage_v',
                     'bms_voltage_observed_at', 'bms_max_discharge_current_a',
                     'bms_discharge_current_observed_at')
            bounded_source = replace(source, **{
                name: getattr(previous, name) for name in names
                if getattr(source, name) is None
            })
            if (frame.context.export_state in {ExportState.CONFIRMED_ZERO_EXPORT, ExportState.PROHIBITED}
                or not rcm_pre_discharge_command_within_live_bms_limit(
                    tx.intent, bounded_source, rcm=frame.rcm, now=now)):
                return False
        if tx.owner is ExecutionOwner.RCE:
            if (frame.context.export_state in {ExportState.CONFIRMED_ZERO_EXPORT, ExportState.PROHIBITED}
                or frame.rce.sale_block_active is not False
                or (frame.rce.result_current is True and (frame.rce.current_slot_planned is not True
                    or (tx.intent.action is not ExecutionAction.PV_CHARGE_HOLD
                        and frame.rce.current_slot_continue_eligible is False)))):
                return False
            if frame.rce.current_run_end is not None:
                end = min(end, frame.rce.current_run_end)
            if tx.intent.action is ExecutionAction.PV_CHARGE_HOLD and (
                frame.rce.pv_charge_hold is not True or frame.rce.pv_charge_hold_qualified is not True):
                return False
            if frame.rce.result_current is False and frame.rce.recalculation_pending is True:
                if not self._bounded_rce_execution_hold(tx.transaction_id, now=now,
                    duration_seconds=RCE_RECALCULATION_HOLD_SECONDS):
                    return False
        if tx.owner is ExecutionOwner.TARIFF and frame.tariff.result_current is True and (
            frame.tariff.current_slot_planned is not True or frame.tariff.current_run_continue_eligible is not True):
            return False
        if tx.owner is ExecutionOwner.TARIFF:
            if frame.tariff.current_grid_charge_run_end is not None:
                end = min(end, frame.tariff.current_grid_charge_run_end)
            if frame.tariff.result_current is False and frame.tariff.recalculation_pending is True:
                if not self._bounded_tariff_execution_hold(tx, now=now):
                    return False
        end = min(end, self.lease_policy_deadline(now=now))
        if not now < end:
            return False
        soc = source.battery_soc_percent
        if soc is not None:
            if not number(soc) or not 0 <= soc <= 100:
                return False
            if tx.intent.action is ExecutionAction.PV_CHARGE_HOLD:
                if soc >= block.force_discharge_soc_percent_4305:
                    return False
            elif block.mode is EmsMode.GRID_DISCHARGE and soc <= max(block.force_discharge_soc_percent_4305, block.self_use_soc_percent_4301):
                return False
            elif (block.mode is EmsMode.GRID_CHARGE
                  and tx.intent.action is not ExecutionAction.TARIFF_GRID_SUPPORT
                  and soc >= block.force_charge_soc_percent_4303):
                return False
        grid = source.critical_grid_power_w if recent(source.critical_grid_power_observed_at) else source.grid_power_w
        if number(grid) and ((block.mode is EmsMode.GRID_CHARGE and grid > 200)
                            or (block.mode is EmsMode.GRID_DISCHARGE and grid < -50
                                and tx.intent.action is not ExecutionAction.PV_CHARGE_HOLD)):
            return False
        battery = source.battery_power_w
        if tx.intent.action is not ExecutionAction.PV_CHARGE_HOLD and number(battery) and ((block.mode is EmsMode.GRID_CHARGE and battery > 200)
                                 or (block.mode is EmsMode.GRID_DISCHARGE and battery < -200)):
            return False
        self._telemetry_hold_until = end
        return True

    @staticmethod
    def _gates_for_candidate(frame: ActiveFrame, candidate: PolicyCandidate):
        return execution_gates(
            frame.execution,
            owner_kind=frame.context.owner_kind,
            owner_conflict=frame.context.owner_conflict,
            now=frame.now,
            candidate=candidate,
        )

    @staticmethod
    def _retarget_delayed_only_by_ems_age(
        settings: SettingsSnapshot,
        *,
        now: datetime,
    ) -> bool:
        """Recognize the measured 15-30 s EMS gap without hiding another gate."""

        now_utc = _utc(now)
        age = (now_utc - settings.ems_observed_at).total_seconds()
        if not (
            MAX_READBACK_AGE_SECONDS < age
            < RCE_EXECUTING_EMS_MAX_AGE_SECONDS
        ):
            return False
        return settings.freshness_errors(
            now_utc,
            required_families=frozenset(
                {AtomicWriteFamily.GCF_EXPORT_LIMIT}
            ),
        ) == ("ems_stale",)

    def _qualify_pv_hold_proof(self, transaction, frame, proof):
        """Two physical FC03 generations spanning 15 s; power reports do not count."""
        key = (transaction.transaction_id, transaction.command_sent_at)
        first = self._pv_hold_first_proof
        if proof.status is not VerificationStatus.CONFIRMED:
            self._pv_hold_first_proof = None
            return proof
        generation = frame.execution.full_block_generation
        if first is None or first[0] != key:
            self._pv_hold_first_proof = (key, generation, proof.observed_at)
        elif generation > first[1] and (proof.observed_at-first[2]).total_seconds() >= 15.:
            return proof
        return replace(proof, status=VerificationStatus.PENDING,
            evidence=proof.evidence + ("pv_hold_waiting_for_second_fc03",))

    def pv_hold_replan_authorized(self, frame, *, now, lease_ttl_seconds=0.):
        """One fixed pending budget for both execution and real ESP renewal."""
        if __package__:
            from .pv_charge_delay_control import pending_same_command, sent_command_ready
        else:
            from pv_charge_delay_control import pending_same_command, sent_command_ready
        tx = self.record.transaction
        if (tx is None or tx.intent.action is not ExecutionAction.PV_CHARGE_HOLD
            or self.record.state not in {ActiveState.EXECUTING, ActiveState.WAITING_READBACK}
            or tx.command_sent_at is None or not tx.command_sent_at <= now < tx.deadline
            or frame.decision.supervisor_mode is not SupervisorMode.ACTIVE
            or frame.decision.selected_policy not in {None, PolicyId.RCE}
            or frame.context.owner_conflict
            or frame.context.export_state is not ExportState.VERIFIED_ALLOWED
            or not pending_same_command(tx.intent, frame.rce, now=now)
            or not sent_command_ready(tx.intent, frame.execution, rce=frame.rce, now=now)):
            return False
        try:
            settings = settings_from_execution_source(frame.execution)
            waiting = self.record.state is ActiveState.WAITING_READBACK
            if (not waiting or lease_ttl_seconds > 0) and settings.ems_block != tx.intent.command.ems_block:
                return False
            proof = physical_verification(tx.transaction_id, tx.intent.action, frame.execution,
                command_sent_at=tx.command_sent_at, now=now, allow_pv_hold_settling=waiting)
            if proof.status is not VerificationStatus.CONFIRMED:
                return False
            if not self._bounded_rce_execution_hold(tx.transaction_id, duration_seconds=RCE_RECALCULATION_HOLD_SECONDS, now=now):
                return False
            end = min(tx.deadline, self._rce_execution_hold[1] + timedelta(seconds=RCE_RECALCULATION_HOLD_SECONDS))
            if waiting:
                end = min(end, tx.command_sent_at + timedelta(seconds=command_wait_timeout(tx)))
            return now + timedelta(seconds=lease_ttl_seconds) < end
        except (ActiveBridgeError, TypeError, ValueError):
            return False

    def pv_hold_settling_lease_authorized(self, frame, *, now, lease_ttl_seconds):
        """Renew only from fresh control evidence inside the initial budget.

        Flow observations are deliberately not authority during inverter startup.
        This does not renew from missing FC03/BMS, prove execution, or move the
        command timestamp, immutable hard deadline, or the 180-second ceiling.
        """
        tx = self.record.transaction
        return bool(self._pv_hold_startup_controls_ready(frame, now=now)
            and tx.readback_result is VerificationStatus.CONFIRMED
            and type(lease_ttl_seconds) in (float,int) and 0 < lease_ttl_seconds < 180
            and now + timedelta(seconds=lease_ttl_seconds) < self.lease_policy_deadline(now=now))

    def rce_settling_lease_authorized(self, frame, *, now, lease_ttl_seconds):
        """A matching Mode 5 and observed initial ramp may renew inside 180 s."""
        tx = self.record.transaction
        if (tx is None or tx.intent.action is not ExecutionAction.RCE_EXPORT
            or self.record.state not in {ActiveState.WAITING_READBACK, ActiveState.RETARGETING}
            or tx.readback_result is not VerificationStatus.CONFIRMED
            or tx.command_sent_at is None or now < tx.command_sent_at
            or now + timedelta(seconds=lease_ttl_seconds) >= self.lease_policy_deadline(now=now)):
            return False
        try:
            settings = settings_from_execution_source(frame.execution)
            if (settings.ems_block != tx.intent.command.ems_block
                or not same_window_rce_wait_authorized(tx.intent, frame.decision,
                    frame.candidates, settings, export_state=frame.context.export_state,
                    rce=frame.rce, now=now)):
                return False
            proof = physical_verification(tx.transaction_id, tx.intent.action, frame.execution,
                command_sent_at=tx.command_sent_at, now=now,
                allow_rce_settling=self.record.state is ActiveState.WAITING_READBACK)
            return (proof.observed_at > tx.command_sent_at and (
                proof.status is VerificationStatus.CONFIRMED or
                proof.status is VerificationStatus.PENDING and
                'rce_phase=waiting_for_discharge_effect' in proof.evidence))
        except (ActiveBridgeError, TypeError, ValueError):
            return False

    def rce_replan_lease_authorized(
        self, frame: ActiveFrame, *, now: datetime, lease_ttl_seconds: float
    ) -> bool:
        """Reattest the sent command inside an already anchored planner hold.

        A renewal must expire within the existing hold and hard deadline. This
        does not authorize a replacement command or move the hold's anchor.
        The adapter separately requires fresh FC03, physical proof and gates.

        Economic authority is the already accepted intent, retained planned
        slot/continuation and bounded run end checked by the same-window bridge.
        The presentation slot helper is unavailable during recalculation and
        may retain a legacy entity ID; it is not a separate price veto. Explicit
        withdrawal of the slot, sale consent or run still fails the bridge.
        """
        transaction = self.record.transaction
        if type(lease_ttl_seconds) not in {int, float} or not 0.0 < lease_ttl_seconds < 3600.0:
            return False
        if transaction is not None and transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD:
            return self.pv_hold_replan_authorized(frame, now=now, lease_ttl_seconds=lease_ttl_seconds)
        settling_deadline = self.post_command_settling_deadline
        if (transaction is not None and settling_deadline is not None
            and frame.rce.price_above_threshold is True
            and now + timedelta(seconds=lease_ttl_seconds) < settling_deadline):
            # The fixed LOAD-settling hold also needs real ESP renewals.
            # A fresh, still-confirmed physical command is mandatory; the
            # adapter rechecks FC03, all live gates and exact lease identity.
            # No renewal may outlive the hold or the original sale window.
            try:
                settings = settings_from_execution_source(frame.execution)
                return self._post_command_measurement_hold(frame, settings, now=now)
            except (ActiveBridgeError, TypeError, ValueError):
                return False
        hold = self._rce_execution_hold
        if (
            self.record.state is not ActiveState.EXECUTING
            or self.record.owner is not ExecutionOwner.RCE
            or transaction is None or hold is None
            or hold[0] != transaction.transaction_id
            or frame.rce.result_current is not False
            or frame.rce.recalculation_pending is not True
            or now + timedelta(seconds=lease_ttl_seconds)
            >= min(transaction.deadline, hold[1] + timedelta(seconds=hold[2]))
        ):
            return False
        try:
            settings = settings_from_execution_source(frame.execution)
            remaining = same_window_rce_execution_hold_seconds(
                transaction.intent, frame.decision, frame.candidates, settings,
                execution_source=frame.execution, export_state=frame.context.export_state,
                rce=frame.rce, now=now,
            )
            return remaining is not None and 0 < lease_ttl_seconds < remaining
        except (ActiveBridgeError, TypeError, ValueError):
            return False

    def tariff_replan_lease_authorized(self, frame, *, now, lease_ttl_seconds):
        """Reattest the same Mode 4 command inside its anchored policy wait."""
        tx = self.record.transaction
        if (tx is None or tx.owner is not ExecutionOwner.TARIFF
            or self.record.state is not ActiveState.EXECUTING):
            return False
        try:
            settings = settings_from_execution_source(frame.execution)
            if (settings.ems_block != tx.intent.command.ems_block
                or not same_run_tariff_wait_authorized(tx.intent, frame.decision,
                    frame.candidates, settings, tariff=frame.tariff,
                    execution_source=frame.execution, now=now,
                    planned_end=self._tariff_planned_boundary(tx))
                or not self._bounded_tariff_execution_hold(tx, now=now)):
                return False
            end = self.tariff_execution_settling_deadline
            return end is not None and now + timedelta(seconds=lease_ttl_seconds) < end
        except (ActiveBridgeError, TypeError, ValueError):
            return False

    def lease_policy_deadline(self, *, now):
        """Cap a real ESP grant at every applicable immutable controller end."""
        tx = self.record.transaction
        if tx is None:
            return now
        ends = [tx.deadline]
        if self.record.state in {ActiveState.WAITING_READBACK, ActiveState.RETARGETING}:
            if tx.command_sent_at is None:
                return now
            ends.append(tx.command_sent_at + timedelta(seconds=command_wait_timeout(tx)))
        if tx.owner is ExecutionOwner.TARIFF:
            ends.append(self._tariff_planned_boundary(tx))
            if self.tariff_execution_settling_deadline is not None:
                ends.append(self.tariff_execution_settling_deadline)
        if self._rce_execution_hold is not None and self._rce_execution_hold[0] == tx.transaction_id:
            ends.append(self._rce_execution_hold[1] + timedelta(seconds=self._rce_execution_hold[2]))
        if self.post_command_settling_deadline is not None:
            ends.append(self.post_command_settling_deadline)
        return min(ends)

    def _bounded_rce_execution_hold(
        self,
        transaction_id: str,
        *,
        duration_seconds: float,
        now: datetime,
    ) -> bool:
        """Bound one planner/cohort gap without allowing churn to renew it."""

        anchor = self._rce_execution_hold
        if anchor is None or anchor[0] != transaction_id:
            self._rce_execution_hold = (transaction_id, now, duration_seconds)
            return True
        if now < anchor[1]:
            # A lease callback may inspect a newer frame before reconcile
            # consumes the preceding one. Anchor at the earliest observation;
            # an out-of-order callback may shorten, never extend, this budget.
            anchor = (anchor[0], now, anchor[2])
            self._rce_execution_hold = anchor
        if anchor[2] != duration_seconds:
            if (
                anchor[2] > RCE_RETARGET_COHORT_HOLD_SECONDS
                and duration_seconds == RCE_RETARGET_COHORT_HOLD_SECONDS
            ):
                # A committed successor starts its own short split-event
                # cohort.  A later return to planner-pending never expands it.
                self._rce_execution_hold = (
                    transaction_id,
                    now,
                    duration_seconds,
                )
                return True
            duration_seconds = anchor[2]
        elapsed = (now - anchor[1]).total_seconds()
        return 0.0 <= elapsed <= duration_seconds

    def _bounded_tariff_execution_hold(
        self,
        transaction: TransactionRecord,
        *,
        now: datetime,
    ) -> bool:
        """Keep one proven Mode 4 command within one immutable safe budget.

        The fixed 180-second policy ceiling does not slide with renewals.
        The adapter reattests fresh physics before any new bounded ESP grant.
        """

        sent_at = transaction.command_sent_at
        if sent_at is None or now < sent_at or now >= transaction.deadline:
            return False
        key = (transaction.transaction_id, sent_at)
        hold = self._tariff_execution_hold
        lease_valid_until = (
            self._control_lease_valid_until()
            if self._control_lease_valid_until is not None
            else None
        )
        if self._control_lease_valid_until is not None and (lease_valid_until is None or lease_valid_until <= now):
            return False
        if hold is None or hold[:2] != key:
            deadline = min(
                now + timedelta(seconds=TARIFF_POST_COMMAND_SETTLING_SECONDS),
                transaction.deadline,
            )
            hold = (key[0], key[1], now, deadline)
            self._tariff_execution_hold = hold
        elif now < hold[2]:
            # Lease evidence and the controller consume independently captured
            # frames. The earlier valid observation owns the fixed budget, even
            # when its callback runs second. Never slide the end forwards.
            hold = (*key, now, min(hold[3], now + timedelta(
                seconds=TARIFF_POST_COMMAND_SETTLING_SECONDS)))
            self._tariff_execution_hold = hold
        deadline = hold[3]
        return hold[2] <= now < deadline

    def _remember_sent_market_basis(self) -> None:
        """Bind only the successful pre-dispatch frame to this exact command."""
        transaction = self.record.transaction
        if (transaction is not None and transaction.owner is ExecutionOwner.RCE
            and transaction.intent.action is ExecutionAction.RCE_EXPORT
            and transaction.command_sent_at is not None
            and self._post_command_dispatch_market is not None):
            self._post_command_market_basis = (
                transaction.transaction_id, transaction.command_sent_at,
                self._post_command_dispatch_market,
            )
        else:
            self._post_command_market_basis = None
        self._post_command_dispatch_market = None
        self._post_command_settling_key = None

    def _capture_dispatch_market(self, frame: ActiveFrame, authorized: bool) -> bool:
        """Capture context before RPC, never from a later no-current-plan frame."""
        self._post_command_dispatch_market = None
        if (authorized and frame.rce.result_current is True
            and frame.rce.recalculation_pending is False
            and frame.rce.current_slot_planned is True):
            fingerprint = frame.rce.post_command_settling_market_fingerprint
            if isinstance(fingerprint, str) and len(fingerprint) == 64:
                self._post_command_dispatch_market = fingerprint
        return authorized

    def _post_command_measurement_hold(
        self, frame: ActiveFrame, settings: SettingsSnapshot, *, now: datetime,
    ) -> bool:
        transaction = self.record.transaction
        basis = self._post_command_market_basis
        if transaction is None or basis is None:
            return False
        key = (transaction.transaction_id, transaction.command_sent_at)
        proof = transaction.physical_verification
        if (basis[:2] != key or transaction.command_sent_at is None
            or self.record.state is not ActiveState.EXECUTING
            or transaction.readback_result is not VerificationStatus.CONFIRMED
            or proof is None or proof.status is not VerificationStatus.CONFIRMED
            or not 0.0 <= (now - transaction.command_sent_at).total_seconds()
            < RCE_POST_COMMAND_SETTLING_SECONDS
            or now >= transaction.deadline):
            return False
        current_proof = physical_verification(
            transaction.transaction_id, transaction.intent.action, frame.execution,
            command_sent_at=transaction.command_sent_at, now=now,
        )
        if (current_proof.status is not VerificationStatus.CONFIRMED
            or current_proof.observed_at <= transaction.command_sent_at):
            return False
        if not post_command_rce_measurement_hold_authorized(
            transaction.intent, frame.decision, frame.candidates, settings,
            execution_source=frame.execution, export_state=frame.context.export_state,
            rce=frame.rce, market_fingerprint=basis[2],
            recognized_pending=self._post_command_settling_key == key, now=now,
        ):
            return False
        self._post_command_settling_key = (key[0], transaction.command_sent_at)
        # The fixed post-command budget supersedes an earlier replan hold.
        self._rce_execution_hold = None
        return True

    def _same_owner_retarget_attempt_end(
        self,
        predecessor: TransactionRecord,
        *,
        now: datetime,
    ) -> datetime | None:
        """Freeze one end-to-end budget before creating a successor.

        The earliest of the predecessor ESP soft lease, immutable hard
        deadline, and controller dispatch ceiling wins.  No later ACK,
        retry, persistence completion, or fresh FC03 cohort can move it.
        """

        now = _utc(now)
        boundaries = [
            predecessor.deadline,
            now + timedelta(seconds=COMMAND_DISPATCH_TIMEOUT_SECONDS),
        ]
        if self._control_lease_valid_until is not None:
            try:
                lease_valid_until = self._control_lease_valid_until()
                if lease_valid_until is None:
                    return None
                boundaries.append(_utc(lease_valid_until))
            except Exception:  # noqa: BLE001 - callback cannot grant authority
                _LOGGER.exception(
                    "EMS retarget lease boundary lookup failed closed"
                )
                return None
        raw_end = min(boundaries)
        attempt_end = raw_end - timedelta(
            seconds=RETARGET_HANDLING_MARGIN_SECONDS
        )
        if attempt_end <= now:
            return None
        _LOGGER.debug(
            "EMS retarget budget owner=%s transaction_id=%s start=%s "
            "attempt_end=%s remaining_s=%.3f lease_capped=%s",
            predecessor.owner.value,
            predecessor.transaction_id,
            _iso(now),
            _iso(attempt_end),
            (attempt_end - now).total_seconds(),
            self._control_lease_valid_until is not None,
        )
        return attempt_end

    @staticmethod
    def _same_owner_retarget_remaining(
        attempt_end: datetime,
        *,
        now: datetime,
    ) -> float:
        remaining = (attempt_end - _utc(now)).total_seconds()
        if remaining <= 0.0:
            raise _RetargetAttemptExpired()
        return remaining

    def _retarget_dispatch_frame_authorized(
        self,
        fallback: ActiveFrame,
        *,
        attempt_end: datetime,
    ) -> bool:
        frame = self._resample_frame(fallback)
        return bool(
            frame is not None
            and _utc(frame.now) < attempt_end
            and self._retarget_frame_authorized(frame)
        )

    async def _dispatch_envelope(
        self,
        writes: tuple[AtomicWrite, ...],
        *,
        pre_dispatch_current: Callable[[], bool] | None = None,
        on_dispatch_started: Callable[[], None] | None = None,
    ) -> None:
        if not writes:
            raise RuntimeError("executor produced an empty atomic envelope")
        dispatched = 0
        for write in writes:
            if pre_dispatch_current is not None and not pre_dispatch_current():
                raise _PreDispatchRejected(dispatched)
            if on_dispatch_started is not None:
                on_dispatch_started()
            await self._dispatch(write)
            dispatched += 1

    def _dispatched_lease_identity(
        self, transaction_id: str
    ) -> tuple[str, str, int] | None:
        getter = self._control_lease_identity
        if getter is None:
            return None
        identity = getter()
        if identity is None:
            return None
        session_id, lease_id, observed_tx, generation = identity
        if observed_tx != transaction_id:
            raise RuntimeError("dispatched control lease belongs to another transaction")
        return session_id, lease_id, generation

    def _resample_frame(self, fallback: ActiveFrame) -> ActiveFrame | None:
        if self._frame_resampler is None:
            return fallback
        return self._frame_resampler()

    async def _reprepare_rejected_ems_start(
        self,
        fallback: ActiveFrame,
        rejection: AtomicWriteNotQueued,
    ):
        """Await a whole new cohort inside the original shared dispatch budget."""

        transaction = self.record.transaction
        assert transaction is not None and transaction.prewrite_snapshot is not None
        previous_generation = transaction.prewrite_snapshot.ems_generation
        while True:
            now = _clock_utc(self._clock)
            if (now >= transaction.deadline
                or not 0.0 <= (now - transaction.started_at).total_seconds()
                < COMMAND_DISPATCH_TIMEOUT_SECONDS):
                return None
            current = self._resample_frame(fallback)
            if current is None:
                await asyncio.sleep(0.1)
                continue
            current = replace(current, now=now)
            try:
                settings = settings_from_execution_source(current.execution)
                candidate = selected_candidate(current.decision, current.candidates)
                if candidate is None or not candidate.start_eligible:
                    return None
                intent = build_actuator_intent(
                    current.decision,
                    current.candidates,
                    settings,
                    rce=current.rce,
                    tariff=current.tariff,
                    rcm=current.rcm,
                    now=current.now,
                )
                if intent is None:
                    return None
                if intent.policy is ExecutionOwner.RCE and not rce_sent_command_within_live_bms_limit(
                    intent, current.execution, rce=current.rce, now=now
                ):
                    return None
                if intent.policy is ExecutionOwner.TARIFF and not tariff_command_within_live_bms_limit(
                    intent, current.execution, tariff=current.tariff, now=now
                ):
                    return None
                gates = self._gates(current)
                if settings.ems_generation == previous_generation:
                    # An API rejection is not the new FC03 data. Wait for HA's
                    # complete cohort, while rechecking current authorization.
                    if not self._executor.command_prewrite_ready(settings, gates, now=now):
                        return None
                    await asyncio.sleep(0.1)
                    continue
                return self._executor.reprepare_unsent_start(
                    intent,
                    settings,
                    gates,
                    rejection=rejection,
                    now=now,
                )
            except (ActiveBridgeError, TypeError, ValueError):
                return None

    async def _reprepare_rejected_same_owner_retarget(
        self,
        predecessor: TransactionRecord,
        fallback: ActiveFrame,
        rejection: AtomicWriteNotQueued,
        *,
        attempt_end: datetime,
    ):
        """Rebuild one proven-unsent RCE/tariff successor on a new FC03 cohort.

        The caller permits one additional dispatch inside the original fixed
        30-second budget.  A firmware refusal invalidates the old control-lease
        handle, so success must pass through a new adapter challenge and arm.
        """

        prepared = self.record.transaction
        if (
            prepared is None
            or prepared.prewrite_snapshot is None
            or prepared.command_sent_at is not None
            or prepared.transaction_id != predecessor.transaction_id
            or prepared.owner not in {ExecutionOwner.RCE, ExecutionOwner.TARIFF}
            or prepared.deadline != predecessor.deadline
            or prepared.command_snapshot != predecessor.command_snapshot
            or rejection.reason not in {
                "stale_snapshot",
                "stale_snapshot_generation",
            }
        ):
            return None
        previous_generation = prepared.prewrite_snapshot.ems_generation
        while True:
            now = _clock_utc(self._clock)
            if now >= attempt_end:
                raise _RetargetAttemptExpired()
            if now >= predecessor.deadline:
                return None
            current = self._resample_frame(fallback)
            if current is None:
                await asyncio.sleep(0.1)
                continue
            current = replace(current, now=now)
            try:
                settings = settings_from_execution_source(current.execution)
                if predecessor.owner is ExecutionOwner.RCE:
                    proposal = build_same_window_rce_retarget(
                        predecessor.intent,
                        current.decision,
                        current.candidates,
                        settings,
                        rce=current.rce,
                        now=now,
                    )
                    if proposal is None:
                        proposal = same_window_rce_rearm_desired(
                            predecessor.intent,
                            current.decision,
                            current.candidates,
                            settings,
                            rce=current.rce,
                            now=now,
                        )
                else:
                    proposal = same_run_tariff_desired(
                        predecessor.intent,
                        current.decision,
                        current.candidates,
                        settings,
                        tariff=current.tariff,
                        now=now,
                    )
                if proposal is None:
                    return None
                if (
                    predecessor.owner is ExecutionOwner.TARIFF
                    and not tariff_command_within_live_bms_limit(
                        proposal.intent,
                        current.execution,
                        tariff=current.tariff,
                        now=now,
                    )
                ):
                    return None
                gates = self._gates_for_candidate(current, proposal.candidate)
                if settings.ems_generation == previous_generation:
                    if not self._retarget_frame_authorized(current):
                        return None
                    await asyncio.sleep(0.1)
                    continue
                if not self._resume_unsent_rce_predecessor(
                    predecessor,
                    current,
                    fallback=fallback,
                ):
                    return None
                if proposal.intent.command == predecessor.intent.command:
                    return self._executor.prepare_same_owner_rearm(
                        proposal.intent,
                        settings,
                        gates,
                        now=now,
                    )
                return self._executor.prepare_same_owner_retarget(
                    proposal.intent,
                    settings,
                    gates,
                    now=now,
                )
            except (ActiveBridgeError, TypeError, ValueError):
                return None

    def _pv_start_callback_pending(self, now: datetime) -> bool:
        transaction = self.record.transaction
        pending = self._pending_retarget_context()
        return bool(
            self.record.state is ActiveState.STARTING
            and transaction is not None
            and transaction.intent.action is ExecutionAction.PV_CHARGE_HOLD
            and transaction.command_sent_at is None
            and (pending.planner_pending or pending.cohort_pending)
            and 0 <= (now - transaction.started_at).total_seconds()
                < COMMAND_DISPATCH_TIMEOUT_SECONDS
        )

    def _command_frame_authorized(
        self, frame: ActiveFrame | None, *, now: datetime | None = None
    ) -> bool:
        if frame is None or self.record.state is not ActiveState.STARTING:
            return False
        if now is not None:
            frame = replace(frame, now=now)
        transaction = self.record.transaction
        if now is not None and transaction is not None:
            if (not 0.0 <= (now - transaction.started_at).total_seconds()
                < COMMAND_DISPATCH_TIMEOUT_SECONDS):
                return False
        if transaction is None or not authorization_matches(
            transaction.intent,
            frame.decision,
            frame.candidates,
            allow_selected_start=True,
        ):
            return False
        try:
            settings = settings_from_execution_source(frame.execution)
        except (ActiveBridgeError, TypeError, ValueError):
            return False
        if transaction.owner is ExecutionOwner.RCE and not rce_sent_command_within_live_bms_limit(
            transaction.intent, frame.execution, rce=frame.rce,
            now=_utc(frame.now) if now is None else now,
        ):
            return False
        if transaction.owner is ExecutionOwner.TARIFF and not tariff_command_within_live_bms_limit(
            transaction.intent, frame.execution, tariff=frame.tariff, now=_utc(frame.now),
        ):
            return False
        return self._capture_dispatch_market(frame, self._executor.command_prewrite_ready(
            settings,
            self._gates(frame),
            now=_utc(frame.now) if now is None else now,
        ))

    def _retarget_frame_authorized(self, frame: ActiveFrame | None) -> bool:
        pending = self._pending_retarget_context()
        check: dict[str, Any] = {
            "frame_rejection_reason": None,
            "exact_target": False,
            "gates": None,
            "prewrite": False,
            "planner_pending": pending.planner_pending,
            "cohort_pending": pending.cohort_pending,
            "pending_source": pending.source,
            "pending_generation": pending.generation,
        }
        self._last_retarget_authorization_check = check
        if frame is None:
            check["frame_rejection_reason"] = (
                "cohort_pending"
                if pending.cohort_pending
                else "planner_pending"
                if pending.planner_pending
                else "frame_unavailable"
            )
            return False
        if self.record.state is not ActiveState.RETARGETING:
            check["frame_rejection_reason"] = "state_changed"
            return False
        transaction = self.record.transaction
        if transaction is None:
            check["frame_rejection_reason"] = "transaction_missing"
            return False
        try:
            settings = settings_from_execution_source(frame.execution)
        except (ActiveBridgeError, TypeError, ValueError):
            check["frame_rejection_reason"] = "settings_invalid"
            return False
        check["now"] = _iso(_utc(frame.now))
        check["ems_generation"] = settings.ems_generation
        check["ems_proof_age_seconds"] = round(
            (_utc(frame.now) - settings.ems_observed_at).total_seconds(), 6
        )
        candidate = same_window_rce_target_authorized(
            transaction.intent,
            frame.decision,
            frame.candidates,
            settings,
            rce=frame.rce,
            now=_utc(frame.now),
        )
        if transaction.owner is ExecutionOwner.TARIFF:
            proposal = same_run_tariff_desired(
                transaction.intent,frame.decision,frame.candidates,settings,
                tariff=frame.tariff,now=_utc(frame.now),
            )
            candidate = (
                proposal.candidate
                if proposal is not None and proposal.intent == transaction.intent
                else None
            )
        if candidate is None:
            check["frame_rejection_reason"] = "exact_target_rejected"
            return False
        check["exact_target"] = True
        if transaction.owner is ExecutionOwner.RCE and not rce_sent_command_within_live_bms_limit(
            transaction.intent, frame.execution, rce=frame.rce, now=_utc(frame.now),
        ):
            check["frame_rejection_reason"] = "rce_bms_rejected"
            return False
        if transaction.owner is ExecutionOwner.TARIFF and not tariff_command_within_live_bms_limit(
            transaction.intent, frame.execution, tariff=frame.tariff, now=_utc(frame.now),
        ):
            check["frame_rejection_reason"] = "tariff_bms_rejected"
            return False
        gates = self._gates_for_candidate(frame, candidate)
        check["gates"] = self._gate_diagnostic(gates)
        prewrite = self._executor.retarget_prewrite_ready(
            settings,
            gates,
            now=_utc(frame.now),
        )
        check["prewrite"] = prewrite
        if not prewrite:
            check["frame_rejection_reason"] = "prewrite_rejected"
            return False
        return self._capture_dispatch_market(frame, True)

    def _pending_retarget_context(self) -> RetargetPendingContext:
        source = self._retarget_pending_context
        if source is None:
            return RetargetPendingContext()
        try:
            context = source()
        except Exception:  # noqa: BLE001 - diagnostics cannot grant authority
            return RetargetPendingContext(source="context_error")
        return (
            context
            if isinstance(context, RetargetPendingContext)
            else RetargetPendingContext(source="context_invalid")
        )

    @staticmethod
    def _gate_diagnostic(gates: Any) -> str:
        """Serialize only fixed booleans needed to explain a prewrite gate."""

        names = (
            "inputs_fresh", "bms_fresh", "full_block_ready",
            "full_block_topology_ready", "discharge_direction_ready",
            "owner_conflict",
        )
        return ",".join(
            f"{name}={int(bool(getattr(gates, name, False)))}" for name in names
        )

    def _log_unsent_rce_retarget(
        self,
        stage: str,
        predecessor: TransactionRecord,
        *,
        frame: ActiveFrame | None,
        observed_now: datetime,
        successor: TransactionRecord | None = None,
        rejection: AtomicWriteNotQueued | _PreDispatchRejected | None = None,
        resumed: bool | None = None,
    ) -> None:
        """Write one bounded line for a rejected/resumed unsent RCE successor."""

        if predecessor.owner is not ExecutionOwner.RCE:
            return
        prepared = successor if successor is not None else self.record.transaction
        old_block = predecessor.intent.command.ems_block
        new_block = prepared.intent.command.ems_block if prepared is not None else None
        proof = predecessor.physical_verification
        now = _utc(frame.now) if frame is not None else _utc(observed_now)
        proof_age = (
            round((now - proof.observed_at).total_seconds(), 6)
            if proof is not None
            else None
        )
        pending = self._pending_retarget_context()
        frame_generation = None
        frame_proof_age = None
        if frame is not None:
            try:
                frame_settings = settings_from_execution_source(frame.execution)
            except (ActiveBridgeError, TypeError, ValueError):
                pass
            else:
                frame_generation = frame_settings.ems_generation
                frame_proof_age = round(
                    (now - frame_settings.ems_observed_at).total_seconds(), 6
                )
        dispatched_count = (
            rejection.dispatched_count
            if isinstance(rejection, _PreDispatchRejected)
            else 0
            if isinstance(rejection, AtomicWriteNotQueued)
            else None
        )
        firmware_reason = (
            rejection.reason
            if isinstance(rejection, AtomicWriteNotQueued)
            else None
        )
        auth = self._last_retarget_authorization_check
        resume_check = (
            self._last_retarget_resume_check if stage == "resume" else {}
        )
        _LOGGER.warning(
            "EMS RCE unsent retarget stage=%s frame_reason=%s "
            "planner_pending=%s cohort_pending=%s pending_source=%s "
            "pending_generation=%s now=%s proof_age_s=%s ems_generation=%s "
            "old_4306=%s new_4306=%s owner=%s transaction_id=%s deadline=%s "
            "exact_target=%s gates=%s prewrite=%s dispatched_count=%s "
            "firmware_reason=%s resume_exact_target=%s resume_gates=%s "
            "resume_attestation=%s resumed=%s",
            stage,
            auth.get("frame_rejection_reason"),
            pending.planner_pending,
            pending.cohort_pending,
            pending.source,
            pending.generation,
            _iso(now),
            frame_proof_age if frame_proof_age is not None else proof_age,
            frame_generation if frame_generation is not None else auth.get("ems_generation"),
            old_block.maximum_discharge_power_percent_4306 if old_block else None,
            new_block.maximum_discharge_power_percent_4306 if new_block else None,
            predecessor.owner.value,
            predecessor.transaction_id,
            _iso(predecessor.deadline),
            auth.get("exact_target"),
            auth.get("gates"),
            auth.get("prewrite"),
            dispatched_count,
            firmware_reason,
            resume_check.get("exact_target"),
            resume_check.get("gates"),
            resume_check.get("attested"),
            resumed,
        )

    def _recover_proven_unsent_retarget(
        self,
        predecessor: TransactionRecord,
        *,
        observed_now: datetime,
        fallback: ActiveFrame,
        rejection: AtomicWriteNotQueued | _PreDispatchRejected | None = None,
    ) -> bool:
        """Cancel B only when transport is proven unsent and A re-attests."""

        successor = self.record.transaction
        latest = self._resample_frame(fallback)
        pending = self._pending_retarget_context()
        resume_frame = latest
        if (
            isinstance(rejection, AtomicWriteNotQueued)
            and rejection.reason == "retarget_not_authorized"
            and self._retarget_resume_frame is not None
        ):
            # The challenge yielded after the cached dispatch frame was built.
            # Re-read raw BMS/permissions even without a queued HA callback.
            resume_frame = self._retarget_resume_frame()
        if (
            resume_frame is None
            and pending.cohort_pending
            and not pending.planner_pending
            and self._retarget_resume_frame is not None
        ):
            # This callback may re-attest A only.  It can never authorize B;
            # the normal complete trailing-edge cohort is still required.
            resume_frame = self._retarget_resume_frame()
        self._log_unsent_rce_retarget(
            "rejected",
            predecessor,
            frame=resume_frame,
            observed_now=observed_now,
            successor=successor,
            rejection=rejection,
        )
        resumed = self._resume_unsent_rce_predecessor(
            predecessor,
            resume_frame,
            fallback=fallback,
        )
        self._log_unsent_rce_retarget(
            "resume",
            predecessor,
            frame=resume_frame,
            observed_now=observed_now,
            successor=successor,
            rejection=rejection,
            resumed=resumed,
        )
        if not resumed and self.record.state is ActiveState.RETARGETING:
            self._executor.request_stop(
                reason=ExecutionReason.AUTHORIZATION_LOST
            )
        return resumed

    def _resume_unsent_rce_predecessor(
        self,
        predecessor: TransactionRecord,
        frame: ActiveFrame | None,
        *,
        fallback: ActiveFrame,
    ) -> bool:
        """Resume a proven predecessor only after rejecting an unsent successor."""

        self._last_retarget_resume_check = {
            "exact_target": False,
            "gates": None,
            "attested": False,
        }
        current = frame
        tariff_pending = False
        resume_predecessor = predecessor
        candidate: PolicyCandidate | None = None
        if current is not None:
            try:
                current_settings = settings_from_execution_source(current.execution)
            except (ActiveBridgeError, TypeError, ValueError):
                return False
            proposal = build_same_window_rce_retarget(
                predecessor.intent,
                current.decision,
                current.candidates,
                current_settings,
                rce=current.rce,
                now=_utc(current.now),
            )
            if proposal is None and predecessor.owner is ExecutionOwner.RCE:
                proposal = same_window_rce_rearm_desired(
                    predecessor.intent,
                    current.decision,
                    current.candidates,
                    current_settings,
                    rce=current.rce,
                    now=_utc(current.now),
                )
            if predecessor.owner is ExecutionOwner.TARIFF:
                proposal = same_run_tariff_desired(
                    predecessor.intent,current.decision,current.candidates,current_settings,
                    tariff=current.tariff,now=_utc(current.now),
                )
            if proposal is None:
                return False
            candidate = proposal.candidate
            self._last_retarget_resume_check["exact_target"] = True
            if predecessor.owner is ExecutionOwner.TARIFF and not tariff_command_within_live_bms_limit(
                predecessor.intent, current.execution, tariff=current.tariff, now=_utc(current.now),
            ):
                return False
        else:
            if predecessor.owner not in {ExecutionOwner.RCE, ExecutionOwner.TARIFF}:
                return False
            settling = self._retarget_replan_pending
            if settling is None or not settling():
                return False
            current = fallback
            if predecessor.owner is ExecutionOwner.TARIFF:
                # The callback attests only a harmless RCEm plan debounce.
                # This frame can retain A, but must never dispatch B.
                now = _clock_utc(self._clock)
                anchor = self._tariff_execution_hold
                if (
                    anchor is not None
                    and (
                        anchor[:2]
                        != (predecessor.transaction_id, predecessor.command_sent_at)
                        or not self._bounded_tariff_execution_hold(
                            predecessor,
                            now=now,
                        )
                    )
                ):
                    return False
                current = replace(fallback, now=now)
                tariff_pending = True
            try:
                current_settings = settings_from_execution_source(current.execution)
            except (ActiveBridgeError, TypeError, ValueError):
                return False
            if tariff_pending and not tariff_command_within_live_bms_limit(
                predecessor.intent, current.execution, tariff=current.tariff, now=current.now,
            ):
                return False
            if predecessor.owner is ExecutionOwner.RCE and not rce_sent_command_within_live_bms_limit(
                predecessor.intent,
                current.execution,
                rce=current.rce,
                now=current.now,
            ):
                return False
            if tariff_pending:
                proof = physical_verification(
                    predecessor.transaction_id,
                    predecessor.intent.action,
                    current.execution,
                    command_sent_at=predecessor.command_sent_at,
                    now=current.now,
                )
                if (
                    proof.status is not VerificationStatus.CONFIRMED
                    or predecessor.command_sent_at is None
                    or proof.observed_at <= predecessor.command_sent_at
                ):
                    return False
                resume_predecessor = replace(
                    predecessor,
                    physical_verification=proof,
                )
        if predecessor.owner is ExecutionOwner.RCE and not rce_sent_command_within_live_bms_limit(
            predecessor.intent, current.execution, rce=current.rce, now=current.now,
        ):
            # The predecessor also needs today's quantitative cap, including
            # when a complete frame is available but a readiness helper lags.
            return False
        gates = (
            self._gates_for_candidate(current, candidate)
            if candidate is not None
            else self._gates(current)
        )
        self._last_retarget_resume_check["gates"] = self._gate_diagnostic(gates)
        if predecessor.owner is ExecutionOwner.RCE:
            # Persistence/dispatch may outlive the stored proof for A while
            # fresh FC03 and power samples still prove A. Re-attest A from the
            # current frame; never extend the old proof's time or relax its
            # 15-second bound. This cannot authorize the unsent successor B.
            proof = physical_verification(
                predecessor.transaction_id,
                predecessor.intent.action,
                current.execution,
                command_sent_at=predecessor.command_sent_at,
                now=current.now,
            )
            if (
                proof.status is not VerificationStatus.CONFIRMED
                or predecessor.command_sent_at is None
                or proof.observed_at <= predecessor.command_sent_at
            ):
                return False
            resume_predecessor = replace(predecessor, physical_verification=proof)
        try:
            resumed = self._executor.cancel_unsent_same_owner_retarget(
                resume_predecessor,
                current_settings,
                gates,
                now=_utc(current.now),
            )
        except (TypeError, ValueError):
            return False
        self._last_retarget_resume_check["attested"] = resumed
        if resumed and tariff_pending:
            if not self._bounded_tariff_execution_hold(
                predecessor,
                now=current.now,
            ):
                return False
        return resumed

    def _restore_frame_current(self, frame: ActiveFrame | None) -> bool:
        if frame is None or self.record.state not in {
            ActiveState.STOPPING,
            ActiveState.FAULT,
        }:
            return False
        try:
            settings = settings_from_execution_source(frame.execution)
        except (ActiveBridgeError, TypeError, ValueError):
            return False
        return self._executor.restore_prewrite_ready(
            settings,
            self._gates(frame),
            now=_utc(frame.now),
        )

    def _manual_proxy_readback_pending(self) -> bool:
        resolved = self._manual_readback_resolved
        if resolved is None:
            return False
        try:
            if not resolved():
                return True
        except Exception:
            return True
        self._manual_readback_resolved = None
        self._publish(self.record)
        return False

    def _master_stop_record(self) -> bool:
        transaction = self.record.transaction
        return bool(
            (transaction is not None and transaction.master_stop_requested)
            or self.record.master_stop_result.status
            is not MasterStopStatus.NOT_REQUESTED
        )

    async def _commit_master_stop(self, *, force_retry: bool = False) -> bool:
        """Bound Store I/O while retaining the newest safety state in RAM."""

        record = self.record
        # Invalid state is never allowed to reach either persistence or transport.
        record_to_dict(record)
        if self._persistence_error is not None and not force_retry:
            # One failed attempt is enough for this reconciliation pass. Keep
            # moving toward physical safety and retry only on a newer FC03 or
            # the bounded continuation callback.
            self._publish(record)
            return False
        try:
            await asyncio.wait_for(
                self._persist(record),
                timeout=MASTER_STOP_PERSIST_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            self._persistence_error = "master_stop_record_persist_timeout"
            self._publish(record)
            return False
        except Exception:  # noqa: BLE001 - physical STOP must retain priority
            self._persistence_error = "master_stop_record_persist_failed"
            self._publish(record)
            return False
        self._persisted_revision = record.revision
        self._persistence_error = None
        self._publish(record)
        return True

    async def _commit(self) -> None:
        record = self.record
        self._capture_stop_decision()
        # Validate exact codec representability before the persistence callback.
        record_to_dict(record)
        await self._persist(record)
        self._persisted_revision = record.revision
        self._persistence_error = None
        self._publish(record)

    def _stop_input_evidence(self, tx, frame) -> dict[str, Any] | None:
        """Event-only diagnostic; never turn a STOP-frame observation into authority."""
        if self.record.reason is not ExecutionReason.STALE_INPUTS:
            return None
        failure = self._executor.last_continuation_input_failure
        if (failure is not None and failure["transaction_id"] == tx.transaction_id
                and failure["checked_at"] == _utc(frame.now).isoformat()):
            return dict(failure)
        decode = self._settings_decode_failure
        evidence: dict[str, Any] = {
            "checked_at": _iso(frame.now), "stage": "stop_frame_only",
            "errors": [], "cause_attested": False,
        }
        if decode is not None and decode[0] == _utc(frame.now):
            evidence.update(stage="settings_decode", errors=["settings_decode_failed"],
                            detail=decode[1], cause_attested=True)
        # Other stale-input paths do not expose the precise failed check yet.
        # Retain raw cohort timing, explicitly without guessing an applied limit.
        source = frame.execution
        evidence["cohorts"] = {}
        for name, observed, generation in (
            ("ems", source.full_block_generation_at, source.full_block_generation),
            ("gcf", source.gcf_generation_at, source.gcf_generation),
            ("battery_306", source.battery_charge_limit_generation_at,
             source.battery_charge_limit_generation),
        ):
            valid = isinstance(observed, datetime) and observed.tzinfo is not None
            evidence["cohorts"][name] = {
                "observed_at": _iso(observed) if valid else None,
                "age_seconds": (_utc(frame.now)-_utc(observed)).total_seconds() if valid else None,
                "generation": generation, "maximum_age_seconds": None,
            }
        return evidence

    def _capture_stop_decision(self) -> None:
        """Freeze the first STOP frame, retaining eight TX across subsequent STARTs.

        Recorder persists this diagnostic projection. It does not participate in
        authority and its absence after process restart cannot qualify a cycle.
        """
        tx = self.record.transaction
        frame = self._last_frame
        if (tx is None or frame is None or self.record.state not in
                {ActiveState.STOPPING, ActiveState.FAULT}
                or any(item["transaction_id"] == tx.transaction_id for item in self._stop_decisions)):
            return
        source = frame.rce if tx.owner is ExecutionOwner.RCE else frame.tariff
        candidate = next((c for c in frame.candidates if c.policy_id.value == tx.owner.value), None)
        hold = self._rce_execution_hold
        self._stop_decisions.append({
            "transaction_id": tx.transaction_id, "at": _iso(frame.now),
            "reason": self.record.reason.value, "deadline": _iso(tx.deadline),
            "input_freshness": self._stop_input_evidence(tx, frame),
            "tariff_live_power_input": (
                {**frame.tariff.live_power_input_evidence,
                 "channels": {key: dict(value) for key, value in
                    frame.tariff.live_power_input_evidence["channels"].items()}}
                if tx.owner is ExecutionOwner.TARIFF
                and frame.tariff.live_power_input_evidence is not None else None
            ),
            "lease_identity": list(tx.lease_identity) if tx.lease_identity else None,
            "command_sent_at": _iso(tx.command_sent_at) if tx.command_sent_at else None,
            "readback_result": tx.readback_result.value,
            "plan_revision": source.input_revision,
            "result_current": source.result_current,
            "recalculation_pending": source.recalculation_pending,
            "plan_status": source.status_code.value if source.status_code else None,
            "candidate_blocker": candidate.blocked_reason.value if candidate and candidate.blocked_reason else None,
            "candidate_reason": candidate.reason_code.value if candidate else None,
            "continuation_eligible": candidate.continuation_eligible if candidate else None,
            "local_hard_stop": candidate.local_hard_stop if candidate else None,
            "source_suppression_reason": getattr(source, "current_slot_suppression_reason",
                                                   getattr(source, "current_run_suppression_reason", None)),
            "requested_power_kw": candidate.requested_power_kw if candidate else None,
            "soc": source.current_soc_percent,
            "protected_floor": candidate.protected_soc_floor_percent if candidate else None,
            "critical_bms_ready": frame.context.critical_bms_ready,
            "discharge_direction_ready": frame.context.discharge_direction_ready,
            "export_state": frame.context.export_state.value,
            "gcf_enable_code": frame.execution.gcf_enable_code,
            "export_limit_percent": frame.execution.effective_export_limit_percent,
            "physical_mode": frame.context.physical_mode.value,
            "full_block_generation": frame.execution.full_block_generation,
            "bms_max_discharge_current_a": frame.execution.bms_max_discharge_current_a,
            "bms_battery_power_w": frame.execution.bms_battery_power_w,
            "bms_battery_power_observed_at": (_iso(frame.execution.bms_battery_power_observed_at)
                if frame.execution.bms_battery_power_observed_at is not None else None),
            "pv_hold_retry": dict(self._pv_hold_retry_status),
            "bms_voltage_v": frame.execution.bms_voltage_v,
            "rce_hold_started_at": _iso(hold[1]) if hold else None,
            "rce_hold_budget_seconds": hold[2] if hold else None,
            "retarget_check": dict(self._last_retarget_authorization_check),
            "retarget_resume_check": dict(self._last_retarget_resume_check),
            "export_evidence": rce_export_evidence(frame.execution, now=frame.now),
        })
        self._stop_decisions = self._stop_decisions[-8:]

    def recorder_attributes(self) -> dict[str, Any]:
        """Return bounded A4 state evidence for the Supervisor sensor."""

        record = self.record
        encoded_record = record_to_dict(record)
        encoded = encoded_record["record"]
        assert isinstance(encoded, dict)
        transaction = record.transaction or record.last_transaction
        active_transaction = record.transaction
        encoded_transaction = encoded["transaction"] or encoded["last_transaction"]
        transaction_scope = (
            "active"
            if encoded["transaction"] is not None
            else "last"
            if encoded["last_transaction"] is not None
            else "none"
        )
        context = self._last_frame.context if self._last_frame is not None else None
        execution_context_evidence = (
            {
                "observed_at": _iso(context.observed_at),
                "physical_mode": context.physical_mode.value,
                "physical_mode_fresh": context.physical_mode_fresh,
                "owner_kind": context.owner_kind.value,
                "owner_conflict": context.owner_conflict,
                "transaction_pending": context.transaction_pending,
                "transaction_owner_kind": context.transaction_owner_kind.value,
                "full_block_execution_ready": context.full_block_execution_ready,
                "direct_306_execution_ready": context.direct_306_execution_ready,
                "direct_259_execution_ready": context.direct_259_execution_ready,
                "topology_full_block_allowed": context.topology_full_block_allowed,
                "topology_direct_register_allowed": (
                    context.topology_direct_register_allowed
                ),
                "charge_direction_ready": context.charge_direction_ready,
                "discharge_direction_ready": context.discharge_direction_ready,
                "critical_bms_ready": context.critical_bms_ready,
                "export_state": context.export_state.value,
            }
            if context is not None
            else None
        )
        tariff_planned_end = (
            self._tariff_planned_boundary(active_transaction)
            if active_transaction is not None
            and active_transaction.owner is ExecutionOwner.TARIFF
            else None
        )
        tariff_raw_plan_end = (
            self._last_frame.tariff.current_grid_charge_run_end
            if self._last_frame is not None
            else None
        )
        return {
            "active_state": record.state.value,
            "recent_stop_decisions": list(self._stop_decisions),
            "rce_export_evidence": (
                rce_export_evidence(self._last_frame.execution, now=self._last_frame.now)
                if self._last_frame is not None else {"status": "pending", "reason": "no_frame"}
            ),
            "post_command_settling": {
                "active": self.post_command_settling_deadline is not None,
                "deadline": (_iso(self.post_command_settling_deadline)
                             if self.post_command_settling_deadline is not None else None),
                "replan_at": (
                    _iso(self.post_command_settling_replan[1])
                    if self.post_command_settling_replan is not None else None
                ),
            },
            "tariff_post_command_settling": {
                "active": self.tariff_execution_settling_deadline is not None,
                "deadline": (
                    _iso(self.tariff_execution_settling_deadline)
                    if self.tariff_execution_settling_deadline is not None
                    else None
                ),
                "budget_seconds": TARIFF_POST_COMMAND_SETTLING_SECONDS,
                "lease_capped": self._control_lease_valid_until is not None,
            },
            "tariff_retry": self.tariff_retry_status,
            "tariff_support_flow_filter": {
                "active": self.tariff_support_flow_filter_deadline is not None,
                "deadline": (_iso(self.tariff_support_flow_filter_deadline)
                    if self.tariff_support_flow_filter_deadline is not None else None),
                "budget_seconds": TARIFF_SUPPORT_FLOW_FILTER_SECONDS,
                "raw_status": (self._tariff_support_flow_filter[3].status.value
                    if self.tariff_support_flow_filter_deadline is not None else None),
                "raw_observed_at": (_iso(self._tariff_support_flow_filter[3].observed_at)
                    if self.tariff_support_flow_filter_deadline is not None else None),
                "last_confirmed_at": (_iso(self._tariff_support_flow_filter[2])
                    if self.tariff_support_flow_filter_deadline is not None else None),
                "lease_renewal_allowed": False,
            },
            "pv_hold_retry": dict(self._pv_hold_retry_status),
            "execution_phase": _EXECUTION_PHASE_BY_ACTIVE_STATE[record.state],
            "manual_proxy_readback_pending": (
                self.manual_proxy_readback_pending
            ),
            "selected_policy": (
                active_transaction.intent.policy.value
                if active_transaction is not None
                else None
            ),
            "transaction_candidate_identity": (
                active_transaction.intent.candidate_revision
                if active_transaction is not None
                else None
            ),
            "selected_action": (
                active_transaction.intent.action.value
                if active_transaction is not None
                else None
            ),
            "reason": record.reason.value,
            "lifecycle_reason": record.reason.value,
            "owner": record.owner.value,
            "owner_conflict": bool(
                self._last_frame is not None
                and self._last_frame.context.owner_conflict
            ),
            "transaction_id": (
                active_transaction.transaction_id
                if active_transaction is not None
                else None
            ),
            "started_at": _iso(active_transaction.started_at)
            if active_transaction is not None
            else None,
            "deadline": _iso(active_transaction.deadline)
            if active_transaction is not None
            else None,
            "tariff_planned_end": (
                _iso(tariff_planned_end)
                if tariff_planned_end is not None
                else None
            ),
            "tariff_raw_plan_end": (
                _iso(tariff_raw_plan_end)
                if isinstance(tariff_raw_plan_end, datetime)
                and tariff_raw_plan_end.tzinfo is not None
                and tariff_raw_plan_end.utcoffset() is not None
                else None
            ),
            "command_snapshot": (
                encoded["transaction"]["command_snapshot"]
                if active_transaction is not None
                else None
            ),
            "expected_readback": (
                encoded["transaction"]["expected_readback"]
                if active_transaction is not None
                else None
            ),
            "physical_verification_result": (
                active_transaction.physical_verification.status.value
                if active_transaction is not None
                and active_transaction.physical_verification is not None
                else VerificationStatus.PENDING.value
            ),
            "physical_verification": (
                encoded["transaction"]["physical_verification"]
                if active_transaction is not None
                else None
            ),
            "rollback_result": (
                active_transaction.rollback_result.value
                if active_transaction is not None
                else transaction.rollback_result.value
                if transaction is not None
                else VerificationStatus.PENDING.value
            ),
            "rollback_status": (
                active_transaction.rollback_status.value
                if active_transaction is not None
                else transaction.rollback_status.value
                if transaction is not None
                else RollbackStatus.NOT_REQUIRED.value
            ),
            "transaction_evidence_schema_version": encoded_record["schema_version"],
            "transaction_evidence_scope": transaction_scope,
            "transaction_evidence": encoded_transaction,
            "execution_context_evidence": execution_context_evidence,
            "starts_allowed": record.starts_allowed,
            "automatic_policies_enabled": record.automatic_policies_enabled,
            "master_stop": encoded["master_stop_result"],
            "executor_revision": record.revision,
            "execution_record_durable": self.record_persisted,
            "execution_persisted_revision": (
                self._persisted_revision if self._persisted_revision >= 0 else None
            ),
            "execution_persistence_error": self._persistence_error,
            "supervisor_execution_authorized": (
                record.state is ActiveState.EXECUTING
            ),
            "legacy_execution_unchanged": not (
                record.state is not ActiveState.IDLE
                or (
                    self._last_frame is not None
                    and self._last_frame.decision.supervisor_mode
                    is SupervisorMode.ACTIVE
                )
            ),
        }


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("runtime time must be timezone-aware")
    return value.astimezone(timezone.utc)


def _clock_utc(clock: UtcClock) -> datetime:
    value = clock()
    if not isinstance(value, datetime):
        raise TypeError("runtime clock must return datetime")
    return _utc(value)


def _iso(value: datetime) -> str:
    return _utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _transaction_id(owner: str) -> str:
    return f"{owner}:{uuid4().hex}"


__all__ = (
    "ActiveFrame",
    "RetargetPendingContext",
    "SupervisorActiveController",
)
