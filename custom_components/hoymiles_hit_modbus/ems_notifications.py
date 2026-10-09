"""Durable, range-based EMS operating notifications.

The notifier consumes only confirmed Supervisor execution.  It never grants
execution authority and never writes an inverter setting.  Delivery is kept
outside the Supervisor reconcile path so a phone provider cannot delay
physical restoration or owner release.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import logging
from typing import Any, Callable, Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .supervisor_active_controller import ActiveFrame
from .supervisor_executor import (
    ActiveState,
    ExecutionOwner,
    ExecutionReason,
    ExecutorRecord,
    MasterStopStatus,
    RollbackStatus,
    VerificationStatus,
)


_LOGGER = logging.getLogger(__name__)

_STORAGE_VERSION: Final = 1
_STORAGE_KEY_PREFIX: Final = f"{DOMAIN}.ems_notifications_v1"
_CONTINUITY_SECONDS: Final = 120.0
_DELIVERY_TIMEOUT_SECONDS: Final = 15.0
_STORAGE_TIMEOUT_SECONDS: Final = 5.0
_MAX_QUEUED_OBSERVATIONS: Final = 8
_MAX_RECENT_EVENTS: Final = 32
_START_ONLY_POLICIES: Final = frozenset({"tariff", "rce"})
_START_PUSH_WINDOW: Final = timedelta(hours=1)
_MAX_START_PUSHES: Final = 2
_SUPPORTED_POLICIES: Final = frozenset(
    {ExecutionOwner.TARIFF.value, ExecutionOwner.RCE.value, ExecutionOwner.RCM.value}
)
_EXPLICIT_INTERRUPT_REASONS: Final = frozenset(
    {
        ExecutionReason.AUTHORIZATION_LOST,
        ExecutionReason.STOP_REQUESTED,
        ExecutionReason.STARTS_DISABLED,
        ExecutionReason.POLICY_DISABLED,
        ExecutionReason.MASTER_STOP_REQUESTED,
        ExecutionReason.MASTER_STOP_BLOCKED,
        ExecutionReason.OFF_GRID_PRESERVED,
        ExecutionReason.STALE_INPUTS,
        ExecutionReason.SNAPSHOT_INVALID,
        ExecutionReason.SNAPSHOT_CHANGED,
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
        ExecutionReason.READBACK_MISMATCH,
        ExecutionReason.PHYSICAL_CONTRADICTION,
        ExecutionReason.PHYSICAL_UNAVAILABLE,
        ExecutionReason.COMMAND_NOT_QUEUED,
        ExecutionReason.COMMAND_OUTCOME_UNKNOWN,
        ExecutionReason.ROLLBACK_FAILED,
        ExecutionReason.RESTART_RECOVERY,
        ExecutionReason.RESTART_RESELECT_REQUIRED,
        ExecutionReason.INVALID_TRANSITION,
    }
)
_RESTORATION_PROGRESS_REASONS: Final = frozenset(
    {
        ExecutionReason.RESTORING.value,
        ExecutionReason.RESTORE_CONFIRMED.value,
        ExecutionReason.RESTORE_NOT_REQUIRED.value,
    }
)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("notification timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value is not None else None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("notification timestamp must be text or null")
    parsed = datetime.fromisoformat(value)
    return _utc(parsed)


@dataclass(frozen=True, slots=True)
class ExecutionObservation:
    """One immutable projection of confirmed physical execution."""

    observed_at: datetime
    policy: str | None
    executing: bool
    actual_start: datetime | None = None
    planned_end: datetime | None = None
    restoration_confirmed: bool = False
    restoration_failed: bool = False
    interrupted: bool = False
    transaction_id: str | None = None
    terminal_reason: str | None = None

    def __post_init__(self) -> None:
        _utc(self.observed_at)
        if self.actual_start is not None:
            _utc(self.actual_start)
        if self.planned_end is not None:
            _utc(self.planned_end)
        if self.executing and (
            self.policy not in _SUPPORTED_POLICIES or self.actual_start is None
        ):
            raise ValueError("confirmed execution requires a supported policy and start")
        if self.transaction_id is not None and not self.transaction_id:
            raise ValueError("transaction_id cannot be empty")
        if self.terminal_reason is not None and (
            type(self.terminal_reason) is not str or not self.terminal_reason
        ):
            raise ValueError("terminal_reason must be a non-empty string")


@dataclass(slots=True)
class ActiveRange:
    """Durable identity for one logical policy range."""

    range_id: str
    policy: str
    actual_start: datetime
    planned_end: datetime | None
    start_handled: bool
    gap_started_at: datetime | None = None
    interrupted: bool = False
    restoration_failed: bool = False
    restoration_confirmed: bool = False
    operation_uncertain: bool = False
    operation_uncertain_transaction_id: str | None = None
    transaction_ids: tuple[str, ...] = ()
    merged_replans: int = 0
    suppressed_duplicate_notifications: int = 0
    final_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "range_id": self.range_id,
            "policy": self.policy,
            "actual_start": _iso(self.actual_start),
            "planned_end": _iso(self.planned_end),
            "start_handled": self.start_handled,
            "gap_started_at": _iso(self.gap_started_at),
            "interrupted": self.interrupted,
            "restoration_failed": self.restoration_failed,
            "restoration_confirmed": self.restoration_confirmed,
            "operation_uncertain": self.operation_uncertain,
            "operation_uncertain_transaction_id": (
                self.operation_uncertain_transaction_id
            ),
            "transaction_ids": list(self.transaction_ids),
            "merged_replans": self.merged_replans,
            "suppressed_duplicate_notifications": self.suppressed_duplicate_notifications,
            "final_reason": self.final_reason,
        }

    @classmethod
    def from_dict(cls, raw: object) -> ActiveRange:
        if not isinstance(raw, Mapping):
            raise ValueError("active notification range must be an object")
        range_id = raw.get("range_id")
        policy = raw.get("policy")
        actual_start = _parse_datetime(raw.get("actual_start"))
        transaction_ids = raw.get("transaction_ids", [])
        merged_replans = raw.get("merged_replans", 0)
        suppressed = raw.get("suppressed_duplicate_notifications", 0)
        final_reason = raw.get("final_reason")
        uncertain_id = raw.get("operation_uncertain_transaction_id")
        operation_uncertain = raw.get("operation_uncertain", False)
        if (
            not isinstance(range_id, str)
            or not range_id
            or policy not in _SUPPORTED_POLICIES
            or actual_start is None
            or type(raw.get("start_handled")) is not bool
            or type(raw.get("interrupted")) is not bool
            or type(raw.get("restoration_failed")) is not bool
            or type(operation_uncertain) is not bool
            or (uncertain_id is not None and (type(uncertain_id) is not str or not uncertain_id))
            or not isinstance(transaction_ids, list)
            or any(type(item) is not str or not item for item in transaction_ids)
            or type(merged_replans) is not int
            or merged_replans < 0
            or type(suppressed) is not int
            or suppressed < 0
            or (
                final_reason is not None
                and (type(final_reason) is not str or not final_reason)
            )
        ):
            raise ValueError("active notification range is malformed")
        return cls(
            range_id=range_id,
            policy=policy,
            actual_start=actual_start,
            planned_end=_parse_datetime(raw.get("planned_end")),
            start_handled=raw["start_handled"],
            gap_started_at=_parse_datetime(raw.get("gap_started_at")),
            interrupted=raw["interrupted"],
            restoration_failed=raw["restoration_failed"],
            restoration_confirmed=raw.get("restoration_confirmed") is True,
            operation_uncertain=operation_uncertain,
            operation_uncertain_transaction_id=uncertain_id,
            transaction_ids=tuple(transaction_ids),
            merged_replans=merged_replans,
            suppressed_duplicate_notifications=suppressed,
            final_reason=final_reason,
        )


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    """One stable operating event queued for the provider."""

    event_id: str
    range_id: str
    policy: str
    kind: str
    actual_start: datetime
    occurred_at: datetime
    planned_end: datetime | None = None
    outcome: str | None = None
    transaction_ids: tuple[str, ...] = ()
    merged_replans: int = 0
    suppressed_duplicate_notifications: int = 0
    final_reason: str | None = None

    def __post_init__(self) -> None:
        if self.policy not in _SUPPORTED_POLICIES:
            raise ValueError("unsupported notification policy")
        if self.kind not in {"start", "end"}:
            raise ValueError("unsupported notification event kind")
        if self.kind == "end" and self.outcome not in {
            "completed",
            "interrupted",
            "unconfirmed",
        }:
            raise ValueError("terminal notification requires an honest outcome")
        _utc(self.actual_start)
        _utc(self.occurred_at)
        if self.planned_end is not None:
            _utc(self.planned_end)
        if any(type(item) is not str or not item for item in self.transaction_ids):
            raise ValueError("transaction_ids must contain non-empty strings")
        if type(self.merged_replans) is not int or self.merged_replans < 0:
            raise ValueError("merged_replans must be a non-negative integer")
        if (
            type(self.suppressed_duplicate_notifications) is not int
            or self.suppressed_duplicate_notifications < 0
        ):
            raise ValueError(
                "suppressed_duplicate_notifications must be a non-negative integer"
            )
        if self.final_reason is not None and (
            type(self.final_reason) is not str or not self.final_reason
        ):
            raise ValueError("final_reason must be a non-empty string")

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key in ("actual_start", "occurred_at", "planned_end"):
            payload[key] = _iso(payload[key])
        payload["transaction_ids"] = list(self.transaction_ids)
        return payload

    @classmethod
    def from_dict(cls, raw: object) -> NotificationEvent:
        if not isinstance(raw, Mapping):
            raise ValueError("notification event must be an object")
        actual_start = _parse_datetime(raw.get("actual_start"))
        occurred_at = _parse_datetime(raw.get("occurred_at"))
        transaction_ids = raw.get("transaction_ids", [])
        if actual_start is None or occurred_at is None:
            raise ValueError("notification event timestamps are missing")
        if not isinstance(transaction_ids, list):
            raise ValueError("notification transaction_ids must be a list")
        return cls(
            event_id=str(raw.get("event_id", "")),
            range_id=str(raw.get("range_id", "")),
            policy=str(raw.get("policy", "")),
            kind=str(raw.get("kind", "")),
            actual_start=actual_start,
            occurred_at=occurred_at,
            planned_end=_parse_datetime(raw.get("planned_end")),
            outcome=(str(raw["outcome"]) if raw.get("outcome") is not None else None),
            transaction_ids=tuple(transaction_ids),
            merged_replans=raw.get("merged_replans", 0),
            suppressed_duplicate_notifications=raw.get(
                "suppressed_duplicate_notifications", 0
            ),
            final_reason=raw.get("final_reason"),
        )


@dataclass(slots=True)
class DeliveryRecord:
    """Bounded durable delivery result; IDs never depend on message text."""

    event: NotificationEvent
    state: str
    updated_at: datetime

    def as_dict(self) -> dict[str, Any]:
        return {
            "event": self.event.as_dict(),
            "state": self.state,
            "updated_at": _iso(self.updated_at),
        }

    @classmethod
    def from_dict(cls, raw: object) -> DeliveryRecord:
        if not isinstance(raw, Mapping):
            raise ValueError("delivery record must be an object")
        state = raw.get("state")
        updated_at = _parse_datetime(raw.get("updated_at"))
        if state not in {
            "pending",
            "sending",
            "delivered",
            "suppressed",
            "unconfirmed",
        } or updated_at is None:
            raise ValueError("delivery record is malformed")
        return cls(
            event=NotificationEvent.from_dict(raw.get("event")),
            state=state,
            updated_at=updated_at,
        )


class RangeNotificationModel:
    """Pure range grouper used by the HA adapter and deterministic tests."""

    def __init__(
        self,
        *,
        initialized: bool = False,
        active: ActiveRange | None = None,
    ) -> None:
        self.initialized = initialized
        self.active = active

    @staticmethod
    def _new_range(observation: ExecutionObservation) -> ActiveRange:
        assert observation.policy is not None
        assert observation.actual_start is not None
        seed = (
            f"{observation.policy}|{_iso(observation.actual_start)}"
        ).encode("ascii")
        digest = sha256(seed).hexdigest()[:20]
        return ActiveRange(
            range_id=f"{observation.policy}.{digest}",
            policy=observation.policy,
            actual_start=_utc(observation.actual_start),
            planned_end=(
                _utc(observation.planned_end)
                if observation.planned_end is not None
                else None
            ),
            start_handled=False,
            transaction_ids=(
                (observation.transaction_id,)
                if observation.transaction_id is not None
                else ()
            ),
        )

    @staticmethod
    def _start_event(active: ActiveRange, now: datetime) -> NotificationEvent:
        return NotificationEvent(
            event_id=f"{active.range_id}.start",
            range_id=active.range_id,
            policy=active.policy,
            kind="start",
            actual_start=active.actual_start,
            occurred_at=_utc(now),
            planned_end=active.planned_end,
            transaction_ids=active.transaction_ids,
            merged_replans=active.merged_replans,
            suppressed_duplicate_notifications=(
                active.suppressed_duplicate_notifications
            ),
        )

    @staticmethod
    def _end_event(
        active: ActiveRange,
        now: datetime,
        *,
        restoration_confirmed: bool,
    ) -> NotificationEvent:
        outcome = (
            "unconfirmed"
            if (
                active.restoration_failed
                or active.operation_uncertain
                or not restoration_confirmed
            )
            else "interrupted"
            if active.interrupted
            else "completed"
        )
        return NotificationEvent(
            event_id=f"{active.range_id}.end",
            range_id=active.range_id,
            policy=active.policy,
            kind="end",
            actual_start=active.actual_start,
            occurred_at=_utc(active.gap_started_at or now),
            planned_end=active.planned_end,
            outcome=outcome,
            transaction_ids=active.transaction_ids,
            merged_replans=active.merged_replans,
            suppressed_duplicate_notifications=(
                active.suppressed_duplicate_notifications
            ),
            final_reason=active.final_reason,
        )

    def observe(
        self,
        observation: ExecutionObservation,
        *,
        bootstrap: bool = False,
    ) -> tuple[NotificationEvent, ...]:
        """Advance grouping without using slot, revision or transaction IDs."""

        now = _utc(observation.observed_at)
        events: list[NotificationEvent] = []
        if not self.initialized:
            self.initialized = True
            if observation.executing:
                self.active = self._new_range(observation)
                self.active.start_handled = True
            return ()

        if observation.executing:
            assert observation.policy is not None
            if self.active is not None and self.active.policy == observation.policy:
                resumed_gap = self.active.gap_started_at is not None
                new_transaction = bool(
                    observation.transaction_id is not None
                    and observation.transaction_id not in self.active.transaction_ids
                )
                if (
                    self.active.gap_started_at is not None
                    and self.active.restoration_confirmed
                    and new_transaction
                ):
                    events.append(
                        self._end_event(
                            self.active,
                            now,
                            restoration_confirmed=True,
                        )
                    )
                    self.active = self._new_range(observation)
                    self.active.start_handled = True
                    events.append(self._start_event(self.active, now))
                    return tuple(events)
                if new_transaction:
                    self.active.transaction_ids += (observation.transaction_id,)
                    self.active.merged_replans += 1
                    self.active.suppressed_duplicate_notifications += 2
                self.active.gap_started_at = None
                self.active.interrupted = False
                self.active.restoration_failed = False
                self.active.restoration_confirmed = False
                if (
                    self.active.operation_uncertain_transaction_id
                    == observation.transaction_id
                ):
                    # The same command now has physical execution proof.
                    self.active.operation_uncertain = False
                    self.active.operation_uncertain_transaction_id = None
                if resumed_gap and not self.active.operation_uncertain:
                    # A transient STOP/restore marker is not the terminal
                    # cause of a range that successfully resumed.
                    self.active.final_reason = None
                if observation.planned_end is not None:
                    self.active.planned_end = _utc(observation.planned_end)
                return ()

            if self.active is not None:
                events.append(
                    self._end_event(
                        self.active,
                        now,
                        # The single-owner Supervisor cannot confirm a different
                        # policy before the previous owner has been released.
                        restoration_confirmed=True,
                    )
                )
            self.active = self._new_range(observation)
            if bootstrap:
                self.active.start_handled = True
            else:
                self.active.start_handled = True
                events.append(self._start_event(self.active, now))
            return tuple(events)

        if self.active is None:
            return ()

        observation_belongs_to_range = bool(
            observation.transaction_id is None
            or observation.transaction_id in self.active.transaction_ids
        )
        if observation_belongs_to_range:
            if observation.terminal_reason == "command_outcome_unknown":
                self.active.operation_uncertain = True
                self.active.operation_uncertain_transaction_id = (
                    observation.transaction_id
                )
            self.active.interrupted = (
                self.active.interrupted or observation.interrupted
            )
            self.active.restoration_failed = (
                self.active.restoration_failed or observation.restoration_failed
            )
            self.active.restoration_confirmed = (
                self.active.restoration_confirmed
                or observation.restoration_confirmed
            )
            if (
                observation.terminal_reason is not None
                and (
                    self.active.final_reason is None
                    or observation.terminal_reason
                    not in _RESTORATION_PROGRESS_REASONS
                )
            ):
                self.active.final_reason = observation.terminal_reason
        if self.active.gap_started_at is None:
            self.active.gap_started_at = now

        gap_age = (now - self.active.gap_started_at).total_seconds()
        may_close = (
            self.active.restoration_confirmed or self.active.restoration_failed
        )
        close_now = may_close and (
            self.active.interrupted
            or self.active.restoration_failed
            or gap_age >= _CONTINUITY_SECONDS
        )
        if not close_now:
            return ()
        events.append(
            self._end_event(
                self.active,
                now,
                restoration_confirmed=self.active.restoration_confirmed,
            )
        )
        self.active = None
        return tuple(events)


class HoymilesEmsNotificationManager:
    """Home Assistant adapter for the pure range notification model."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self._entry = entry
        self._store: Store[dict[str, Any]] = Store(
            hass,
            _STORAGE_VERSION,
            f"{_STORAGE_KEY_PREFIX}.{entry.entry_id}",
        )
        self._model = RangeNotificationModel()
        self._deliveries: list[DeliveryRecord] = []
        self._start_push_attempts: list[datetime] = []
        self._initialized = False
        self._first_observation = True
        self._gap_cancel: Any | None = None
        self._last_observation: ExecutionObservation | None = None
        self._observation_queue: deque[ExecutionObservation] = deque()
        self._worker_task: asyncio.Task[None] | None = None
        self._status_sink: Callable[[str | None], None] | None = None
        self._delivery_degradation: str | None = None
        self._dropped_observations = 0
        self._dropped_equivalent_observations = 0
        self._dropped_material_observations = 0
        self._backpressure_active = False
        self._closed = False
        self._state_revision = 0
        self._persisted_revision = 0

    @property
    def delivery_status(self) -> dict[str, Any]:
        """Expose bounded delivery truth without claiming exactly-once semantics."""

        active = self._model.active
        return {
            "status": "degraded" if self._delivery_degradation else "available",
            "reason": self._delivery_degradation,
            "queued_observations": len(self._observation_queue),
            "worker_active": self._worker_task is not None
            and not self._worker_task.done(),
            "dropped_observations": self._dropped_observations,
            "dropped_equivalent_observations": self._dropped_equivalent_observations,
            "dropped_material_observations": self._dropped_material_observations,
            "dropped_unclassified_observations": max(
                self._dropped_observations
                - self._dropped_equivalent_observations
                - self._dropped_material_observations,
                0,
            ),
            "backpressure_active": self._backpressure_active,
            "history_quality": (
                "material_observation_loss"
                if self._dropped_material_observations
                else (
                    "unclassified_observation_loss"
                    if self._dropped_observations
                    > self._dropped_equivalent_observations
                    else (
                        "equivalent_observations_coalesced"
                        if self._dropped_equivalent_observations
                        else "complete"
                    )
                )
            ),
            "guarantee": (
                "best_effort_unconfirmed"
                if self._delivery_degradation
                else "durable_before_provider_attempt"
            ),
            "logical_range_id": active.range_id if active is not None else None,
            "transaction_ids": (
                list(active.transaction_ids) if active is not None else []
            ),
            "merged_replans": active.merged_replans if active is not None else 0,
            "suppressed_duplicate_notifications": (
                active.suppressed_duplicate_notifications
                if active is not None
                else 0
            ),
            "final_outcome": (
                "recovery_in_progress"
                if active is not None and active.gap_started_at is not None
                else "executing"
                if active is not None
                else None
            ),
            "final_reason": active.final_reason if active is not None else None,
            "provider_states": [
                {
                    "event_id": item.event.event_id,
                    "range_id": item.event.range_id,
                    "state": item.state,
                }
                for item in self._deliveries[-8:]
            ],
        }

    def diagnostic_snapshot(self) -> dict[str, Any]:
        """Read the already-loaded bounded ledger; never send or write to Store."""
        return {
            "schema_version": 1,
            "available": self._initialized,
            "scope": "retained_logical_ranges_and_provider_results",
            "phone_delivery_verified": False,
            "retained_event_limit": _MAX_RECENT_EVENTS,
            "retained_event_count": min(len(self._deliveries), _MAX_RECENT_EVENTS),
            "complete_lifetime_history": False,
            "state_revision": self._state_revision,
            "persisted_revision": self._persisted_revision,
            "persistence_current": self._initialized and self._state_revision == self._persisted_revision,
            "status": self.delivery_status,
            "active_range": self._model.active.as_dict() if self._model.active else None,
            "events": [item.as_dict() for item in self._deliveries[-_MAX_RECENT_EVENTS:]],
        }

    def attach_status_sink(self, sink: Callable[[str | None], None]) -> None:
        """Attach the Supervisor's observation-only degradation projection."""

        if self._status_sink is not None:
            raise RuntimeError("notification status sink is already attached")
        self._status_sink = sink
        sink(self._delivery_degradation)

    def _set_degradation(self, reason: str | None) -> None:
        if reason == self._delivery_degradation:
            return
        self._delivery_degradation = reason
        if self._status_sink is not None:
            self._status_sink(reason)

    async def async_initialize(self) -> None:
        """Load state; never replay a pre-restart provider attempt."""

        try:
            async with asyncio.timeout(_STORAGE_TIMEOUT_SECONDS):
                raw = await self._store.async_load()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - notifier startup remains bounded
            raw = None
            self._start_push_attempts = [_utc(dt_util.utcnow())] * _MAX_START_PUSHES
            self._set_degradation("notification_persistence_unavailable")
            _LOGGER.warning("EMS notification ledger load did not complete", exc_info=True)
        if raw is not None:
            try:
                if raw.get("schema_version") != 1:
                    raise ValueError("unsupported notification ledger schema")
                self._model = RangeNotificationModel(
                    initialized=raw.get("initialized") is True,
                    active=(
                        ActiveRange.from_dict(raw["active"])
                        if raw.get("active") is not None
                        else None
                    ),
                )
                records = raw.get("recent", [])
                if not isinstance(records, list) or len(records) > _MAX_RECENT_EVENTS:
                    raise ValueError("notification ledger history is malformed")
                drop_counters = (
                    raw.get("dropped_observations", 0),
                    raw.get("dropped_equivalent_observations", 0),
                    raw.get("dropped_material_observations", 0),
                )
                if any(type(value) is not int or value < 0 for value in drop_counters):
                    raise ValueError("notification drop history is malformed")
                if drop_counters[0] < drop_counters[1] + drop_counters[2]:
                    raise ValueError("notification drop history is inconsistent")
                self._dropped_observations = drop_counters[0]
                self._dropped_equivalent_observations = drop_counters[1]
                self._dropped_material_observations = drop_counters[2]
                self._deliveries = [DeliveryRecord.from_dict(item) for item in records]
                now = _utc(dt_util.utcnow())
                attempts = raw.get("start_push_attempts")
                if attempts is None:
                    # Old ledgers did not retain a complete hourly send budget.
                    # Conservatively wait one hour; never reset quota on upgrade.
                    self._start_push_attempts = [now] * _MAX_START_PUSHES
                    self._mark_state_changed()
                else:
                    if not isinstance(attempts, list) or len(attempts) > _MAX_START_PUSHES:
                        raise ValueError("invalid START push budget")
                    parsed = [_parse_datetime(value) for value in attempts]
                    if any(value is None for value in parsed):
                        raise ValueError("missing START push timestamp")
                    self._start_push_attempts = parsed
                restart_changed = False
                for delivery in self._deliveries:
                    if delivery.state in {"pending", "sending"}:
                        delivery.state = "unconfirmed"
                        delivery.updated_at = now
                        restart_changed = True
                        self._set_degradation("notification_restart_unconfirmed")
                if restart_changed:
                    self._mark_state_changed()
            except (KeyError, TypeError, ValueError, OverflowError):
                _LOGGER.warning("EMS notification ledger was invalid; seeding safely")
                self._model = RangeNotificationModel()
                self._deliveries = []
                self._start_push_attempts = [_utc(dt_util.utcnow())] * _MAX_START_PUSHES
                self._mark_state_changed()
        else:
            self._mark_state_changed()
        self._initialized = True
        await self._safe_save()

    def close(self) -> None:
        """Cancel only notifier-owned callbacks and provider tasks."""

        self._closed = True
        self._cancel_gap()
        if self._worker_task is not None and not self._worker_task.done():
            self._set_degradation("notification_shutdown_unconfirmed")
            self._worker_task.cancel()
        self._observation_queue.clear()

    def process_active_frame(
        self,
        record: ExecutorRecord,
        frame: ActiveFrame,
        _source_entity_ids: Mapping[str, str | None],
    ) -> None:
        """Snapshot and queue one frame without awaiting storage or a provider."""

        if self._closed:
            return
        observation = self._observation(record, frame)
        self._enqueue_observation(observation)
        self._ensure_worker()

    async def async_process_active_frame(
        self,
        record: ExecutorRecord,
        frame: ActiveFrame,
        _source_entity_ids: Mapping[str, str | None],
    ) -> None:
        """Compatibility wrapper; it only performs the synchronous enqueue."""

        self.process_active_frame(record, frame, _source_entity_ids)

    @staticmethod
    def _terminal_observation(observation: ExecutionObservation) -> bool:
        return bool(
            not observation.executing
            and (
                observation.restoration_confirmed
                or observation.restoration_failed
                or observation.interrupted
            )
        )

    def _enqueue_observation(self, observation: ExecutionObservation) -> None:
        """Bound memory while preserving admitted starts and terminal evidence."""

        if len(self._observation_queue) < _MAX_QUEUED_OBSERVATIONS:
            self._observation_queue.append(observation)
            return
        if self._observations_equivalent(self._observation_queue[-1], observation):
            self._record_queue_drop(material=False)
            return
        if self._terminal_observation(observation):
            for index in range(len(self._observation_queue) - 1, 0, -1):
                if self._observations_equivalent(
                    self._observation_queue[index - 1],
                    self._observation_queue[index],
                ):
                    del self._observation_queue[index]
                    self._observation_queue.append(observation)
                    self._record_queue_drop(material=False)
                    return
            for index in range(len(self._observation_queue) - 1, -1, -1):
                if not self._terminal_observation(self._observation_queue[index]):
                    del self._observation_queue[index]
                    self._observation_queue.append(observation)
                    self._record_queue_drop(material=True)
                    return
        self._record_queue_drop(material=True)

    @staticmethod
    def _observations_equivalent(
        left: ExecutionObservation,
        right: ExecutionObservation,
    ) -> bool:
        """Match redundant frames without joining real execution boundaries."""

        return (
            left.policy,
            left.executing,
            left.restoration_confirmed,
            left.restoration_failed,
            left.interrupted,
        ) == (
            right.policy,
            right.executing,
            right.restoration_confirmed,
            right.restoration_failed,
            right.interrupted,
        )

    def _record_queue_drop(self, *, material: bool) -> None:
        self._dropped_observations += 1
        if material:
            self._dropped_material_observations += 1
        else:
            self._dropped_equivalent_observations += 1
        self._backpressure_active = True
        self._mark_state_changed()
        self._set_degradation("notification_queue_backpressure")

    def _recover_backpressure_if_current(self, *, progress_ok: bool) -> None:
        if not self._backpressure_active or self._observation_queue or not progress_ok:
            return
        self._backpressure_active = False
        if self._delivery_degradation == "notification_queue_backpressure":
            self._set_degradation(None)

    def _ensure_worker(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            return
        self._worker_task = self._entry.async_create_background_task(
            self.hass,
            self._worker_loop(),
            "Hoymiles EMS notification worker",
        )
        self._worker_task.add_done_callback(self._worker_done)

    def _worker_done(self, task: asyncio.Task[None]) -> None:
        if self._worker_task is task:
            self._worker_task = None
        if not self._closed and self._observation_queue:
            self._ensure_worker()

    async def _worker_loop(self) -> None:
        """Drain one bounded FIFO with no child task per event."""

        try:
            if not self._initialized:
                await self.async_initialize()
            while not self._closed and self._observation_queue:
                observation = self._observation_queue.popleft()
                self._last_observation = observation
                bootstrap = self._first_observation
                self._first_observation = False
                before = self._storage_payload()
                events = self._model.observe(observation, bootstrap=bootstrap)
                if self._storage_payload() != before:
                    self._mark_state_changed()
                await self._async_accept(events, now=observation.observed_at)
                self._sync_gap_timer(observation)
                progress_ok = await self._safe_save()
                self._recover_backpressure_if_current(progress_ok=progress_ok)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - one failed worker is observable/restartable
            self._set_degradation("notification_worker_failed")
            _LOGGER.exception("EMS notification worker failed")

    def _observation(
        self,
        record: ExecutorRecord,
        frame: ActiveFrame,
    ) -> ExecutionObservation:
        now = _utc(frame.now)
        transaction = record.transaction
        executing = bool(
            record.state is ActiveState.EXECUTING
            and transaction is not None
            and transaction.owner.value in _SUPPORTED_POLICIES
            and transaction.physical_verification is not None
            and transaction.physical_verification.status is VerificationStatus.CONFIRMED
        )
        policy = transaction.owner.value if executing and transaction is not None else None
        actual_start = (
            transaction.physical_verification.observed_at
            if executing
            and transaction is not None
            and transaction.physical_verification is not None
            else None
        )
        planned_end: datetime | None = None
        if executing and transaction is not None:
            if transaction.owner is ExecutionOwner.RCE:
                planned_end = frame.rce.current_run_end or transaction.deadline
            elif transaction.owner is ExecutionOwner.TARIFF:
                planned_end = frame.tariff.current_grid_charge_run_end or transaction.deadline
            elif (
                transaction.owner is ExecutionOwner.RCM
                and transaction.intent.action.value == "rcm_pre_discharge"
            ):
                planned_end = (
                    frame.rcm.latched_pre_discharge_deadline
                    or frame.rcm.pre_discharge_deadline
                    or transaction.deadline
                )

        terminal = record.last_transaction if record.transaction is None else transaction
        terminal_cause = (
            terminal.reason
            if terminal is not None
            and terminal.reason.value not in _RESTORATION_PROGRESS_REASONS
            else record.reason
        )
        restoration_confirmed = bool(
            record.state is ActiveState.IDLE
            and terminal is not None
            and terminal.rollback_status in {
                RollbackStatus.CONFIRMED,
                RollbackStatus.NOT_REQUIRED,
            }
            and terminal.rollback_result
            in {VerificationStatus.CONFIRMED, VerificationStatus.PENDING}
        )
        restoration_failed = bool(
            terminal is not None
            and (
                terminal.rollback_status is RollbackStatus.FAILED
                or (
                    terminal.rollback_status is not RollbackStatus.PENDING
                    and terminal.rollback_result
                    in {
                        VerificationStatus.CONTRADICTED,
                        VerificationStatus.UNAVAILABLE,
                    }
                )
            )
        )
        paused = self.hass.states.is_state("input_boolean.hoymiles_ems_paused", "on")
        policy_disabled = False
        if self._model.active is not None:
            helper = {
                ExecutionOwner.RCE.value: "input_boolean.hoymiles_rce_discharge_enabled",
                ExecutionOwner.TARIFF.value: "input_boolean.hoymiles_tariff_charge_enabled",
                ExecutionOwner.RCM.value: "input_boolean.hoymiles_rcm_enabled",
            }[self._model.active.policy]
            policy_disabled = self.hass.states.is_state(helper, "off")
        interrupted = bool(
            paused
            or policy_disabled
            or not record.starts_allowed
            or terminal_cause in _EXPLICIT_INTERRUPT_REASONS
            or record.master_stop_result.status
            in {
                MasterStopStatus.REQUESTED,
                MasterStopStatus.IN_PROGRESS,
                MasterStopStatus.COMPLETED,
                MasterStopStatus.BLOCKED,
                MasterStopStatus.FAILED,
            }
        )
        return ExecutionObservation(
            observed_at=now,
            policy=policy,
            executing=executing,
            actual_start=actual_start,
            planned_end=planned_end,
            restoration_confirmed=restoration_confirmed,
            restoration_failed=restoration_failed,
            interrupted=interrupted,
            transaction_id=(
                transaction.transaction_id
                if executing and transaction is not None
                else terminal.transaction_id
                if terminal is not None
                else None
            ),
            terminal_reason=(
                terminal_cause.value
                if not executing and terminal_cause is not None
                else None
            ),
        )

    def _sync_gap_timer(self, observation: ExecutionObservation) -> None:
        self._cancel_gap()
        active = self._model.active
        if active is None or active.gap_started_at is None or observation.executing:
            return
        due = active.gap_started_at + timedelta(seconds=_CONTINUITY_SECONDS)
        delay = max((due - _utc(observation.observed_at)).total_seconds(), 0.0)
        self._gap_cancel = async_call_later(self.hass, delay, self._gap_elapsed)

    def _cancel_gap(self) -> None:
        if self._gap_cancel is not None:
            self._gap_cancel()
            self._gap_cancel = None

    async def _gap_elapsed(self, _now: datetime) -> None:
        self._gap_cancel = None
        observation = self._last_observation
        if self._closed or observation is None or observation.executing:
            return
        advanced = ExecutionObservation(
            observed_at=_utc(dt_util.utcnow()),
            policy=None,
            executing=False,
            restoration_confirmed=observation.restoration_confirmed,
            restoration_failed=observation.restoration_failed,
            interrupted=observation.interrupted,
            transaction_id=observation.transaction_id,
            terminal_reason=observation.terminal_reason,
        )
        self._enqueue_observation(advanced)
        self._ensure_worker()

    async def _async_accept(
        self,
        events: tuple[NotificationEvent, ...],
        *,
        now: datetime,
    ) -> None:
        known = {record.event.event_id for record in self._deliveries}
        accepted: list[DeliveryRecord] = []
        for event in events:
            if event.event_id in known:
                continue
            enabled, target = self._delivery_configuration()
            if not enabled or not self._push_event_allowed(event):
                state = "suppressed"
            elif target is None:
                state = "unconfirmed"
                self._set_degradation("notification_provider_unavailable")
            else:
                state = "pending"
            delivery = DeliveryRecord(event=event, state=state, updated_at=_utc(now))
            self._deliveries.append(delivery)
            self._deliveries = self._deliveries[-_MAX_RECENT_EVENTS:]
            known.add(event.event_id)
            accepted.append(delivery)
        if not accepted:
            return
        self._mark_state_changed()
        if not await self._safe_save():
            changed = False
            for delivery in accepted:
                if delivery.state == "pending":
                    delivery.state = "unconfirmed"
                    delivery.updated_at = _utc(now)
                    changed = True
            if changed:
                self._mark_state_changed()
            return
        for delivery in accepted:
            if delivery.state == "pending":
                await self._async_deliver(delivery.event.event_id)

    def _delivery_configuration(self) -> tuple[bool, str | None]:
        enabled = self.hass.states.is_state(
            "input_boolean.hoymiles_ems_push_notifications_enabled", "on"
        )
        target_state = self.hass.states.get("input_text.hoymiles_ems_push_notify_target")
        target = target_state.state.strip() if target_state is not None else ""
        valid = bool(
            target.startswith("notify.")
            and self.hass.states.get(target) is not None
            and self.hass.services.has_service("notify", "send_message")
        )
        return enabled, target if valid else None

    @staticmethod
    def _push_event_allowed(event: NotificationEvent) -> bool:
        """Keep terminal truth in the ledger, never push tariff/RCE terminals."""
        return event.policy not in _START_ONLY_POLICIES or (
            event.kind == "start" and event.planned_end is not None
            and event.planned_end > event.actual_start
        )

    async def _async_deliver(self, event_id: str) -> None:
        delivery = next(
            (item for item in self._deliveries if item.event.event_id == event_id),
            None,
        )
        if delivery is None or delivery.state != "pending" or self._closed:
            return
        now = _utc(dt_util.utcnow())
        limited = delivery.event.policy in _START_ONLY_POLICIES
        # Future timestamps (clock rollback) remain charged until time catches up.
        self._start_push_attempts = [at for at in self._start_push_attempts
                                     if now - at < _START_PUSH_WINDOW]
        if (not self._push_event_allowed(delivery.event)
                or limited and len(self._start_push_attempts) >= _MAX_START_PUSHES):
            delivery.state = "suppressed"
            delivery.updated_at = now
            self._mark_state_changed()
            await self._safe_save()
            return
        enabled, target = self._delivery_configuration()
        if not enabled or target is None:
            delivery.state = "suppressed" if not enabled else "unconfirmed"
            delivery.updated_at = _utc(dt_util.utcnow())
            self._mark_state_changed()
            if enabled:
                self._set_degradation("notification_provider_unavailable")
            await self._safe_save()
            return
        delivery.state = "sending"
        delivery.updated_at = now
        if limited:
            # Reserve BEFORE provider I/O and persist with sending. Ambiguous
            # outcomes consume quota; neither restart nor retries grant extras.
            self._start_push_attempts.append(now)
        self._mark_state_changed()
        if not await self._safe_save():
            delivery.state = "unconfirmed"
            self._mark_state_changed()
            return
        title, message = self._message(delivery.event)
        try:
            async with asyncio.timeout(_DELIVERY_TIMEOUT_SECONDS):
                await self.hass.services.async_call(
                    "notify",
                    "send_message",
                    {"title": title, "message": message},
                    target={ATTR_ENTITY_ID: target},
                    blocking=True,
                )
        except asyncio.CancelledError:
            delivery.state = "unconfirmed"
            delivery.updated_at = _utc(dt_util.utcnow())
            self._mark_state_changed()
            self._set_degradation("notification_delivery_cancelled")
            raise
        except Exception:  # noqa: BLE001 - ambiguous provider outcome is terminal
            _LOGGER.warning(
                "EMS notification provider outcome is unconfirmed for %s",
                event_id,
                exc_info=True,
            )
            delivery.state = "unconfirmed"
            self._set_degradation("notification_provider_unconfirmed")
        else:
            delivery.state = "delivered"
        delivery.updated_at = _utc(dt_util.utcnow())
        self._mark_state_changed()
        if not await self._safe_save():
            delivery.state = "unconfirmed"
            self._mark_state_changed()
        elif delivery.state == "delivered" and self._delivery_degradation in {
            "notification_provider_unavailable",
            "notification_provider_unconfirmed",
        }:
            self._set_degradation(None)

    def _message(self, event: NotificationEvent) -> tuple[str, str]:
        polish = str(self.hass.config.language).lower().startswith("pl")
        start = dt_util.as_local(event.actual_start)
        occurred = dt_util.as_local(event.occurred_at)
        planned = dt_util.as_local(event.planned_end) if event.planned_end else None
        planned_span = _format_span(start, planned) if planned is not None else None
        actual_span = _format_span(start, occurred)
        if polish:
            labels = {
                "tariff": "ładowanie taryfowe",
                "rce": "sprzedaż dynamiczną",
                "rcm": "działanie RCEm",
            }
            negative_labels = {
                "tariff": "ładowania taryfowego",
                "rce": "sprzedaży dynamicznej",
                "rcm": "działania RCEm",
            }
            label = labels[event.policy]
            if event.kind == "start":
                message = f"Rozpoczęto {label}."
                message += (
                    f" Planowany zakres: {planned_span}."
                    if planned_span is not None
                    else f" Początek: {start.strftime('%H:%M')}."
                )
            elif event.outcome == "completed":
                message = f"Zakończono {label}. Wykonanie: {actual_span}."
            elif event.outcome == "interrupted":
                message = (
                    f"Przerwano {label}; ustawienia przywrócone. "
                    f"Wykonanie: {actual_span}."
                )
            else:
                message = (
                    f"Nie zakończono poprawnie {negative_labels[event.policy]}. "
                    f"Odtworzenie ustawień niepotwierdzone. Wykonanie: {actual_span}."
                )
            title = ("Hoymiles — Sprzedaż dynamiczna" if event.policy == "rce"
                     else "Hoymiles — Powiadomienia EMS")
            return title, message

        labels = {
            "tariff": "tariff charging",
            "rce": "dynamic sales",
            "rcm": "RCEm operation",
        }
        label = labels[event.policy]
        if event.kind == "start":
            message = f"Started {label}."
            message += (
                f" Planned range: {planned_span}."
                if planned_span is not None
                else f" Start: {start.strftime('%H:%M')}."
            )
        elif event.outcome == "completed":
            message = f"Finished {label}. Actual range: {actual_span}."
        elif event.outcome == "interrupted":
            message = (
                f"Interrupted {label}; settings restored. "
                f"Actual range: {actual_span}."
            )
        else:
            message = (
                f"{label.capitalize()} did not finish correctly. "
                f"Settings restoration is unconfirmed. Actual range: {actual_span}."
            )
        title = ("Hoymiles — Dynamic sales" if event.policy == "rce"
                 else "Hoymiles — EMS notifications")
        return title, message

    def _storage_payload(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "initialized": self._model.initialized,
            "active": self._model.active.as_dict() if self._model.active else None,
            "recent": [record.as_dict() for record in self._deliveries],
            "start_push_attempts": [_iso(at) for at in self._start_push_attempts],
            "dropped_observations": self._dropped_observations,
            "dropped_equivalent_observations": self._dropped_equivalent_observations,
            "dropped_material_observations": self._dropped_material_observations,
        }

    def _mark_state_changed(self) -> None:
        """Advance the durable revision without performing Store I/O."""

        self._state_revision += 1

    async def _safe_save(self) -> bool:
        """Bound Store I/O and expose loss of the durable-delivery guarantee."""

        while self._persisted_revision != self._state_revision:
            revision = self._state_revision
            payload = self._storage_payload()
            try:
                async with asyncio.timeout(_STORAGE_TIMEOUT_SECONDS):
                    await self._store.async_save(payload)
            except asyncio.CancelledError:
                raise
            except TimeoutError:
                self._set_degradation("notification_persistence_timeout")
                _LOGGER.warning("EMS notification ledger save timed out")
                return False
            except Exception:  # noqa: BLE001 - persistence is observation-only
                self._set_degradation("notification_persistence_failed")
                _LOGGER.warning("EMS notification ledger save failed", exc_info=True)
                return False
            self._persisted_revision = revision
        if self._delivery_degradation in {
            "notification_persistence_unavailable",
            "notification_persistence_timeout",
            "notification_persistence_failed",
        }:
            self._set_degradation(None)
        return True


def _format_span(start: datetime, end: datetime) -> str:
    """Format local spans without hiding a midnight or DST date boundary."""

    if start.date() == end.date():
        return f"{start.strftime('%H:%M')}–{end.strftime('%H:%M')}"
    return f"{start.strftime('%Y-%m-%d %H:%M')}–{end.strftime('%Y-%m-%d %H:%M')}"


__all__ = (
    "ActiveRange",
    "DeliveryRecord",
    "ExecutionObservation",
    "HoymilesEmsNotificationManager",
    "NotificationEvent",
    "RangeNotificationModel",
)
