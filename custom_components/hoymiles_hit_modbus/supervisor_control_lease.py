"""Bounded HA client for ESP lease protocol v2 (120 s / 20 s)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from enum import Enum
from math import floor, isfinite
from secrets import token_hex
from typing import Any, Mapping


CONTROL_LEASE_PROTOCOL_VERSION = 2
CONTROL_LEASE_RENEW_SECONDS = 20.0
# Five missed renewal opportunities; each grant may be shorter at a policy
# boundary. The ESP clock, original deadline and nonce anchor remain binding.
CONTROL_LEASE_TTL_SECONDS = 120.0
CONTROL_LEASE_MAX_HARD_DEADLINE_SECONDS = 86_400
# More than one full 24h lease at the existing 20s cadence. Retargets and
# subsequent cycles share this fixed process-local bound; overflow is explicit.
CONTROL_LEASE_JOURNAL_MAX_EVENTS = 8192


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("control lease requires a timezone-aware clock")
    return value.astimezone(timezone.utc)


def _token(value: object, name: str, maximum: int) -> str:
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError(f"{name} is invalid")
    return value


def _nonce(value: object) -> str:
    if type(value) is not str or len(value) != 8:
        raise ValueError("control lease nonce is malformed")
    try:
        int(value, 16)
    except ValueError as err:
        raise ValueError("control lease nonce is malformed") from err
    return value.lower()


def _block(value: tuple[float, ...]) -> tuple[float, ...]:
    if len(value) != 7 or any(
        isinstance(item, bool) or not isinstance(item, (int, float))
        or not isfinite(float(item)) for item in value
    ):
        raise ValueError("control lease block is invalid")
    mode = int(round(value[0]))
    if mode not in {4, 5} or abs(float(value[0]) - mode) > 1e-6:
        raise ValueError("control lease only protects forced modes 4/5")
    return tuple(float(item) for item in value)


def parse_protocol_response(
    response: object,
    *,
    success_reason: str,
    nonce_key: str,
) -> tuple[bool, str, str | None]:
    """Accept only an exact correlated schema/protocol response."""

    if not isinstance(response, Mapping):
        raise ValueError("control lease response is absent")
    if response.get("schema_version") != 1 or response.get("protocol_version") != CONTROL_LEASE_PROTOCOL_VERSION:
        raise ValueError("control lease response version is incompatible")
    accepted = response.get("accepted")
    reason = response.get("reason")
    if type(accepted) is not bool or type(reason) is not str or not reason:
        raise ValueError("control lease response is malformed")
    if accepted:
        if reason != success_reason:
            raise ValueError("control lease success is inconsistent")
        return True, reason, _nonce(response.get(nonce_key))
    return False, reason, None


def _granted_seconds(response: Mapping[str, Any]) -> float:
    remaining = response.get("soft_remaining_ms")
    if type(remaining) is not int or not 0 < remaining <= CONTROL_LEASE_TTL_SECONDS * 1000:
        raise ValueError("control lease duration is absent or incompatible")
    return remaining / 1000.0


@dataclass(slots=True)
class ControlLeaseHandle:
    """One process-local right which cannot survive an HA restart."""

    lease_id: str
    transaction_id: str
    command_generation: int
    hard_deadline: datetime
    block: tuple[float, ...]
    nonce: str
    sequence: int
    next_renew_monotonic: float
    last_monotonic: float
    last_accepted_monotonic: float
    last_wall: datetime
    granted_seconds: float = CONTROL_LEASE_TTL_SECONDS
    nonce_received_monotonic: float = 0.0
    pending_sequence: int | None = None
    pending_first_send_monotonic: float | None = None
    pending_authorization_seconds: int | None = None
    pending_authorization_deadline: datetime | None = None


class ControlLeaseRenewStatus(str, Enum):
    """Correlated disposition of one ESP renewal response."""

    ACCEPTED = "accepted"
    IGNORED_STALE_RESPONSE = "ignored_stale_response"
    RETRYABLE_STALE_SNAPSHOT = "retryable_stale_snapshot"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ControlLeaseRenewResult:
    """Typed result which keeps an ESP rejection reason observable."""

    status: ControlLeaseRenewStatus
    reason: str


class ControlLeaseClient:
    """Prepare bounded requests without using wall time as the soft lease clock."""

    def __init__(self, *, session_id: str | None = None) -> None:
        self.session_id = _token(session_id or token_hex(16), "session_id", 128)
        self._generation = 0
        self.handle: ControlLeaseHandle | None = None
        self._journal_epoch = token_hex(16)
        self._journal: deque[dict[str, Any]] = deque(maxlen=CONTROL_LEASE_JOURNAL_MAX_EVENTS)
        self._journal_total = 0

    def _record_acceptance(self, handle: ControlLeaseHandle, *, kind: str,
                           sequence: int, first_send: float, received: float,
                           granted: float, snapshot_generation: int | None,
                           correlated: bool) -> None:
        """Called only after a validated ESP ACK, never from request/status code.

        UTC is projected from HA's wall/monotonic anchor, not an ESP clock.
        No nonce/session capability is exported, and no Recorder write occurs.
        """
        lease_ref = sha256(handle.lease_id.encode()).hexdigest()
        previous = self._journal[-1] if self._journal else None
        if previous and (previous['lease_ref'], previous['sequence']) == (lease_ref, sequence):
            return
        gap = None
        if kind == 'renew':
            if previous is None or previous['lease_ref'] != lease_ref:
                gap = 'predecessor_missing'
            elif sequence != previous['sequence'] + 1:
                gap = 'sequence_gap'
            elif first_send >= previous['first_send_monotonic'] + previous['granted_seconds']:
                gap = 'conservative_coverage_gap'
        self._journal_total += 1
        self._journal.append({
            'ordinal': self._journal_total, 'kind': kind,
            'transaction_id': handle.transaction_id, 'lease_id': handle.lease_id, 'lease_ref': lease_ref,
            'command_generation': handle.command_generation, 'sequence': sequence,
            'snapshot_generation': snapshot_generation, 'request_correlated': correlated,
            'first_send_monotonic': first_send, 'response_received_monotonic': received,
            'accepted_at': (handle.last_wall + timedelta(seconds=received-handle.last_monotonic)).isoformat(),
            'hard_deadline': handle.hard_deadline.isoformat(), 'granted_seconds': granted,
            'gap_before': gap,
        })

    def journal_snapshot(self) -> dict[str, Any]:
        """On-demand export; survives invalidate, never survives HA client restart.

        Pages fit the existing diagnostic redactor's 500-item limit. Completeness
        refers only to retained process-local ACKs, not an entire physical cycle.
        """
        events = [dict(event) for event in self._journal]
        dropped = self._journal_total - len(events)
        return {
            'available': True, 'schema_version': 1, 'journal_epoch': self._journal_epoch,
            'scope': 'accepted_ESP_responses_observed_by_current_HA_client',
            'utc_basis': 'HA_wall_monotonic_projection_not_ESP_clock',
            'max_events': CONTROL_LEASE_JOURNAL_MAX_EVENTS,
            'retained_events': len(events), 'dropped_events': dropped,
            'retention_complete': dropped == 0, 'active_lease': self.handle is not None,
            'history_before_client_creation': 'unavailable',
            'pages': [events[index:index+256] for index in range(0, len(events), 256)],
        }

    def invalidate(self) -> None:
        self.handle = None

    def expire_if_due(self, *, now_monotonic: float) -> bool:
        """Never create a new renewal after the local conservative soft limit.

        An uncertain request may only retry its same sequence, bounded by that
        request's original first-send anchor (duplicate ACK cannot extend it).
        """
        handle = self.handle
        if handle is None:
            return False
        anchor = (
            handle.pending_first_send_monotonic
            if handle.pending_sequence is not None
            else handle.last_accepted_monotonic
        )
        if (
            anchor is None or not isfinite(anchor) or not isfinite(now_monotonic)
            or now_monotonic < handle.last_monotonic
            or now_monotonic >= anchor + (
                CONTROL_LEASE_TTL_SECONDS if handle.pending_sequence is not None
                else handle.granted_seconds)
        ):
            self.invalidate()
            return True
        return False

    def prepare_arm(
        self,
        *,
        transaction_id: str,
        hard_deadline: datetime,
        block: tuple[float, ...],
        challenge_nonce: str,
        now_wall: datetime,
        now_monotonic: float,
    ) -> dict[str, Any]:
        """Create one new identity; retargeting never extends the deadline."""

        transaction = _token(transaction_id, "transaction_id", 160)
        deadline = _utc(hard_deadline)
        now = _utc(now_wall)
        if not isfinite(now_monotonic) or now_monotonic < 0.0:
            raise ValueError("monotonic clock is invalid")
        current = self.handle
        if current is not None:
            if transaction != current.transaction_id:
                raise ValueError("control lease retarget changed transaction")
            if now_monotonic < current.last_monotonic or now < current.last_wall:
                self.invalidate()
                raise ValueError("control lease retarget clock moved backwards")
            deadline = min(deadline, current.hard_deadline)
        remaining = (deadline - now).total_seconds()
        if not 1.0 <= remaining <= CONTROL_LEASE_MAX_HARD_DEADLINE_SECONDS:
            raise ValueError("hard deadline is expired or outside protocol range")
        self._generation += 1
        lease_id = token_hex(16)
        return {
            "session_id": self.session_id,
            "lease_id": lease_id,
            "transaction_id": transaction,
            "command_generation": self._generation,
            # ESP anchors this at challenge issuance, before this HA receive.
            # Network latency can shorten this right but cannot extend it.
            "hard_deadline_seconds": int(floor(remaining)),
            "nonce": _nonce(challenge_nonce),
            "_hard_deadline": deadline,
            "_block": _block(block),
            "_now_wall": now,
            "_now_monotonic": float(now_monotonic),
        }

    def accept_arm(self, request: Mapping[str, Any], response: object, *, now_monotonic: float | None = None) -> bool:
        request_generation = request.get("command_generation")
        if (
            request.get("session_id") != self.session_id
            or type(request_generation) is not int
            or request_generation != self._generation
        ):
            # A delayed response for an older prepare_arm must not replace a
            # newer process-local right.
            return False
        try:
            accepted, _reason, nonce = parse_protocol_response(
                response,
                success_reason="accepted",
                nonce_key="renew_nonce",
            )
            granted = _granted_seconds(response) if accepted else 0.0
        except ValueError:
            self.invalidate()
            raise
        if not accepted:
            self.invalidate()
            return False
        assert nonce is not None
        sent = float(request["_now_monotonic"])
        received = sent if now_monotonic is None else now_monotonic
        if not isfinite(received) or not sent <= received < sent + granted:
            self.invalidate()
            return False
        self.handle = ControlLeaseHandle(
            lease_id=_token(request.get("lease_id"), "lease_id", 128),
            transaction_id=_token(
                request.get("transaction_id"), "transaction_id", 160
            ),
            command_generation=int(request["command_generation"]),
            hard_deadline=_utc(request["_hard_deadline"]),
            block=_block(request["_block"]),
            nonce=nonce,
            sequence=0,
            next_renew_monotonic=float(request["_now_monotonic"])
            + CONTROL_LEASE_RENEW_SECONDS,
            last_monotonic=float(request["_now_monotonic"]),
            last_accepted_monotonic=float(request["_now_monotonic"]),
            last_wall=_utc(request["_now_wall"]),
            granted_seconds=granted,
            nonce_received_monotonic=received,
        )
        self._record_acceptance(self.handle, kind='arm', sequence=0,
                                first_send=sent, received=received, granted=granted,
                                snapshot_generation=None, correlated=True)
        return True

    def prepare_renew(
        self,
        *,
        authorized: bool,
        snapshot_generation: int,
        now_wall: datetime,
        now_monotonic: float,
        authorization_deadline: datetime | None = None,
    ) -> dict[str, Any] | None:
        handle = self.handle
        if handle is None or not authorized:
            return None
        now = _utc(now_wall)
        if (
            not isfinite(now_monotonic)
            or now_monotonic < handle.last_monotonic
            or now < handle.last_wall
            or now >= handle.hard_deadline
        ):
            self.invalidate()
            return None
        if self.expire_if_due(now_monotonic=now_monotonic):
            return None
        handle.last_monotonic = float(now_monotonic)
        handle.last_wall = now
        if now_monotonic < handle.next_renew_monotonic:
            return None
        if type(snapshot_generation) is not int or snapshot_generation < 1:
            self.invalidate()
            return None
        end = min(handle.hard_deadline, _utc(authorization_deadline)) if authorization_deadline is not None else handle.hard_deadline
        if end <= now:
            return None
        horizon = floor((end-now).total_seconds() + now_monotonic-handle.nonce_received_monotonic)
        if not 0 < horizon <= CONTROL_LEASE_MAX_HARD_DEADLINE_SECONDS:
            return None
        sequence = handle.pending_sequence
        if sequence is None:
            # The ESP issued this nonce no later than its HA receive instant.
            # Sending a horizon from that fixed anchor bounds even delayed
            # requests by the policy end (a relative receipt TTL would not).
            sequence = handle.sequence + 1
            handle.pending_sequence = sequence
            handle.pending_first_send_monotonic = float(now_monotonic)
            handle.pending_authorization_seconds = horizon
            handle.pending_authorization_deadline = end
            handle.next_renew_monotonic = (
                float(now_monotonic) + CONTROL_LEASE_RENEW_SECONDS
            )
        elif handle.pending_first_send_monotonic is None:
            # A pending identity without its first-send anchor cannot be
            # reconciled safely with the ESP's non-extending duplicate ACK.
            self.invalidate()
            return None
        elif handle.pending_authorization_deadline is None or end < handle.pending_authorization_deadline:
            # An uncertain request may already be accepted by the ESP. Never
            # resend its older, wider authority after the policy shortened.
            self.invalidate()
            return None
        return {
            "session_id": self.session_id,
            "lease_id": handle.lease_id,
            "transaction_id": handle.transaction_id,
            "command_generation": handle.command_generation,
            "sequence": sequence,
            "nonce": handle.nonce,
            "authorization_current": True,
            "snapshot_generation": snapshot_generation,
            "authorization_seconds": handle.pending_authorization_seconds,
        }

    def defer_renew_retry(self, *, now_monotonic: float) -> None:
        """Retry one uncertain request later without advancing its sequence."""

        handle = self.handle
        if handle is None or not isfinite(now_monotonic):
            return
        handle.next_renew_monotonic = max(
            handle.next_renew_monotonic,
            float(now_monotonic) + CONTROL_LEASE_RENEW_SECONDS,
        )

    def process_renew_response(
        self,
        response: object,
        *,
        request: Mapping[str, Any] | None = None,
        now_monotonic: float,
    ) -> ControlLeaseRenewResult:
        handle = self.handle
        if request is not None and (
            handle is None
            or request.get("session_id") != self.session_id
            or request.get("lease_id") != handle.lease_id
            or request.get("transaction_id") != handle.transaction_id
            or request.get("command_generation") != handle.command_generation
            or request.get("sequence") != handle.pending_sequence
            or request.get("nonce") != handle.nonce
        ):
            # Ignore a delayed response from a completed/older transaction;
            # never invalidate or mutate the newer correlated handle.
            return ControlLeaseRenewResult(
                ControlLeaseRenewStatus.IGNORED_STALE_RESPONSE,
                "request_identity_no_longer_current",
            )
        if handle is None or handle.pending_sequence is None:
            raise ValueError("no correlated control lease renewal is pending")
        pending = handle.pending_sequence
        first_send = handle.pending_first_send_monotonic
        if first_send is None:
            self.invalidate()
            return ControlLeaseRenewResult(
                ControlLeaseRenewStatus.REJECTED,
                "local_renew_anchor_missing",
            )
        try:
            accepted, reason, nonce = parse_protocol_response(
                response,
                success_reason="renewed",
                nonce_key="renew_nonce",
            )
            granted = _granted_seconds(response) if accepted else 0.0
        except ValueError:
            self.invalidate()
            raise
        if not accepted:
            if reason == "snapshot_generation_advanced":
                # ESP still sees the exact authorized block, but obtained a
                # newer complete FC03 sample while this request was in flight.
                # Keep the same pending identity for one caller-bounded retry.
                return ControlLeaseRenewResult(
                    ControlLeaseRenewStatus.RETRYABLE_STALE_SNAPSHOT,
                    reason,
                )
            self.invalidate()
            return ControlLeaseRenewResult(
                ControlLeaseRenewStatus.REJECTED,
                reason,
            )
        if not isfinite(now_monotonic) or now_monotonic < handle.last_monotonic:
            self.invalidate()
            return ControlLeaseRenewResult(
                ControlLeaseRenewStatus.REJECTED,
                "local_monotonic_clock_invalid",
            )
        if (
            not isfinite(first_send)
            or first_send < handle.last_accepted_monotonic
            or first_send > now_monotonic
        ):
            self.invalidate()
            return ControlLeaseRenewResult(
                ControlLeaseRenewStatus.REJECTED,
                "local_renew_anchor_invalid",
            )
        # ESPHome anchors a new renewal at request receipt and accepts a
        # duplicate sequence idempotently without extending that expiry.  HA
        # therefore uses the more conservative first-send instant, never the
        # response-arrival instant, for the same lease interval.
        if now_monotonic >= first_send + granted:
            self.invalidate()
            return ControlLeaseRenewResult(
                ControlLeaseRenewStatus.REJECTED,
                "local_renew_ack_expired",
            )
        assert nonce is not None
        self._record_acceptance(handle, kind='renew', sequence=pending,
                                first_send=first_send, received=now_monotonic, granted=granted,
                                snapshot_generation=request.get('snapshot_generation') if request else None,
                                correlated=request is not None)
        handle.sequence = pending
        handle.pending_sequence = None
        handle.pending_first_send_monotonic = None
        handle.pending_authorization_seconds = None
        handle.pending_authorization_deadline = None
        handle.nonce = nonce
        handle.last_monotonic = float(now_monotonic)
        handle.last_accepted_monotonic = float(first_send)
        handle.granted_seconds = granted
        handle.nonce_received_monotonic = float(now_monotonic)
        handle.next_renew_monotonic = float(first_send) + CONTROL_LEASE_RENEW_SECONDS
        return ControlLeaseRenewResult(
            ControlLeaseRenewStatus.ACCEPTED,
            "renewed",
        )

    def accept_renew(
        self,
        response: object,
        *,
        request: Mapping[str, Any] | None = None,
        now_monotonic: float,
    ) -> bool:
        """Compatibility wrapper for callers which only need acceptance."""

        return self.process_renew_response(
            response,
            request=request,
            now_monotonic=now_monotonic,
        ).status is ControlLeaseRenewStatus.ACCEPTED


__all__ = [
    "CONTROL_LEASE_PROTOCOL_VERSION",
    "CONTROL_LEASE_RENEW_SECONDS",
    "CONTROL_LEASE_TTL_SECONDS",
    "ControlLeaseClient",
    "ControlLeaseHandle",
    "ControlLeaseRenewResult",
    "ControlLeaseRenewStatus",
    "parse_protocol_response",
]
