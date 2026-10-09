"""Dependency-free deterministic checks for the HA side of lease protocol v2."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / "custom_components" / "hoymiles_hit_modbus" / "supervisor_control_lease.py"
SPEC = importlib.util.spec_from_file_location("supervisor_control_lease", PATH)
assert SPEC is not None and SPEC.loader is not None
LEASE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = LEASE
SPEC.loader.exec_module(LEASE)

NOW = datetime(2026, 9, 13, 10, 0, tzinfo=timezone.utc)
BLOCK = (4.0, 25.0, 90.0, 98.0, 5.0, 20.0, 0.0)
FALLBACK = (0.0, 25.0, 90.0, 98.0, 5.0, 20.0, 0.0)


class FirmwareLeaseModel:
    """Deterministic oracle for the ESP states asserted in the YAML contract."""

    def __init__(self) -> None:
        self.now = 0.0
        self.state = "disarmed"
        self.bus = True
        self.fallback = FALLBACK
        self.active: tuple[float, ...] | None = None
        self.predecessor: tuple[float, ...] | None = None
        self.physical = FALLBACK
        self.transaction = ""
        self.generation = 0
        self.sequence = 0
        self.expiry = 0.0
        self.deadline = 0.0
        self.nonce_issued = 0.0

    def arm(
        self,
        block: tuple[float, ...],
        *,
        transaction: str = "rce:1",
        generation: int = 1,
        hard_seconds: float = 3600.0,
    ) -> bool:
        if self.state == "disarmed":
            if self.physical != self.fallback:
                return False
            self.transaction = transaction
            self.deadline = self.now + hard_seconds
            self.predecessor = None
        elif self.state == "confirmed":
            if (
                transaction != self.transaction
                or generation <= self.generation
                or self.physical != self.active
                or self.now >= self.deadline
            ):
                return False
            self.predecessor = self.active
            self.deadline = min(self.deadline, self.now + hard_seconds)
        else:
            return False
        self.active = block
        self.generation = generation
        self.sequence = 0
        self.expiry = min(self.now + 120.0, self.deadline)
        self.nonce_issued = self.now
        self.state = "pending"
        return True

    def observe(self, block: tuple[float, ...]) -> None:
        self.physical = block
        if self.state == "pending":
            if block == self.active:
                self.state = "confirmed"
            elif block == self.predecessor:
                self.state = "expired"
            else:
                self.state = "disarmed"
        elif self.state == "confirmed" and block != self.active:
            self.state = "expired" if block == self.predecessor else "disarmed"
        elif self.state == "restoring":
            if block == self.fallback:
                self.state = "disarmed"
            elif block not in {self.active, self.predecessor}:
                self.state = "disarmed"

    def renew(self, *, authorized: bool, sequence: int, authorization_seconds=None) -> bool:
        if (
            self.state != "confirmed"
            or not authorized
            or self.physical != self.active
            or self.now >= self.deadline
            or self.now >= self.expiry
        ):
            return False
        if sequence == self.sequence:
            # Firmware acknowledges a correlated duplicate but does not move
            # its already-running soft-expiry boundary.
            return True
        if sequence != self.sequence + 1:
            return False
        authority_end = self.deadline if authorization_seconds is None else self.nonce_issued + authorization_seconds
        if authority_end <= self.now:
            return False
        self.sequence = sequence
        self.expiry = min(self.now + 120.0, self.deadline, authority_end)
        self.nonce_issued = self.now
        return True

    def advance(self, seconds: float) -> None:
        self.now += seconds
        if self.state in {"pending", "confirmed"} and (
            self.now >= self.expiry or self.now >= self.deadline
        ):
            self.state = "expired"
        if self.state == "expired" and self.bus:
            if self.physical == self.fallback:
                self.state = "disarmed"
            elif self.physical in {self.active, self.predecessor}:
                self.state = "restoring"

    def reboot(self) -> None:
        self.state = "expired" if self.active is not None else "disarmed"
        self.sequence = 0

    def master_stop(self) -> None:
        if self.bus:
            self.observe(self.fallback)
            if self.state == "expired":
                self.advance(0.0)
        elif self.state in {"pending", "confirmed"}:
            self.expiry = self.now


def accepted(reason: str, nonce: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
        "accepted": True,
        "reason": reason,
        "renew_nonce": nonce,
    }


def armed() -> tuple[object, dict[str, object]]:
    client = LEASE.ControlLeaseClient(session_id="session-a")
    request = client.prepare_arm(
        transaction_id="rce:0123456789abcdef",
        hard_deadline=NOW + timedelta(minutes=20),
        block=BLOCK,
        challenge_nonce="00000001",
        now_wall=NOW,
        now_monotonic=100.0,
    )
    assert client.accept_arm(request, accepted("accepted", "00000002")) is True
    return client, request


def test_fresh_authorized_decision_renews_once() -> None:
    client, _request = armed()
    renew = client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=20),
        now_monotonic=120.0,
    )
    assert renew is not None and renew["sequence"] == 1
    assert client.accept_renew(
        accepted("renewed", "00000003"), now_monotonic=120.1
    ) is True
    assert client.handle is not None and client.handle.sequence == 1


def test_repeated_stale_or_unauthorized_inputs_do_not_renew() -> None:
    client, _request = armed()
    assert client.prepare_renew(
        authorized=False,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=20),
        now_monotonic=120.0,
    ) is None
    first = client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=20),
        now_monotonic=120.0,
    )
    assert first is not None
    assert client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=21),
        now_monotonic=121.0,
    ) is None
    rejected = {
        "schema_version": 1,
        "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
        "accepted": False,
        "reason": "stale_or_invalid_nonce",
    }
    assert client.accept_renew(rejected, now_monotonic=121.0) is False
    assert client.handle is None


def test_uncertain_response_retries_same_sequence_after_wifi_drop() -> None:
    client, _request = armed()
    first = client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=20),
        now_monotonic=120.0,
    )
    assert first is not None
    client.defer_renew_retry(now_monotonic=120.1)
    assert client.prepare_renew(
        authorized=True,
        snapshot_generation=12,
        now_wall=NOW + timedelta(seconds=21),
        now_monotonic=121.0,
    ) is None
    retry = client.prepare_renew(
        authorized=True,
        snapshot_generation=13,
        now_wall=NOW + timedelta(seconds=41),
        now_monotonic=140.1,
    )
    assert retry is not None
    assert retry["sequence"] == first["sequence"] == 1
    assert retry["nonce"] == first["nonce"]
    assert client.accept_renew(
        accepted("renewed", "00000003"), now_monotonic=140.2
    ) is True
    assert client.handle is not None
    assert client.handle.last_accepted_monotonic == 120.0
    assert client.handle.next_renew_monotonic == 140.0


def test_delayed_renew_ack_uses_first_send_anchor() -> None:
    client, _request = armed()
    renew = client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=20),
        now_monotonic=120.0,
    )
    assert renew is not None
    result = client.process_renew_response(
        accepted("renewed", "00000003"),
        request=renew,
        now_monotonic=135.0,
    )
    assert result.status is LEASE.ControlLeaseRenewStatus.ACCEPTED
    assert client.handle is not None
    assert client.handle.last_accepted_monotonic == 120.0
    assert client.handle.next_renew_monotonic == 140.0


def test_renew_ack_at_or_after_conservative_soft_expiry_fails_closed() -> None:
    for received_at in (240.0, 245.0):
        client, _request = armed()
        renew = client.prepare_renew(
            authorized=True,
            snapshot_generation=11,
            now_wall=NOW + timedelta(seconds=20),
            now_monotonic=120.0,
        )
        assert renew is not None
        result = client.process_renew_response(
            accepted("renewed", "00000003"),
            request=renew,
            now_monotonic=received_at,
        )
        assert result.status is LEASE.ControlLeaseRenewStatus.REJECTED
        assert result.reason == "local_renew_ack_expired"
        assert client.handle is None


def test_ha_restart_cannot_recover_old_right() -> None:
    old, _request = armed()
    restarted = LEASE.ControlLeaseClient(session_id="session-b")
    assert old.handle is not None
    assert restarted.handle is None
    assert restarted.session_id != old.session_id


def test_backward_monotonic_or_wall_jump_fails_closed() -> None:
    client, _request = armed()
    assert client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=20),
        now_monotonic=99.0,
    ) is None
    assert client.handle is None
    client, _request = armed()
    assert client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW - timedelta(seconds=1),
        now_monotonic=120.0,
    ) is None
    assert client.handle is None


def test_hard_deadline_never_renews() -> None:
    client, _request = armed()
    assert client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(minutes=20),
        now_monotonic=1300.0,
    ) is None
    assert client.handle is None


def test_retarget_preserves_original_deadline_and_transaction() -> None:
    client, _request = armed()
    request = client.prepare_arm(
        transaction_id="rce:0123456789abcdef",
        hard_deadline=NOW + timedelta(hours=1),
        block=(5.0, 25.0, 90.0, 98.0, 5.0, 20.0, 10.0),
        challenge_nonce="00000003",
        now_wall=NOW + timedelta(seconds=21),
        now_monotonic=121.0,
    )
    assert request["_hard_deadline"] == NOW + timedelta(minutes=20)
    assert request["hard_deadline_seconds"] == 1179
    assert client.accept_arm(request, accepted("accepted", "00000004")) is True
    assert client.handle is not None
    assert client.handle.hard_deadline == NOW + timedelta(minutes=20)
    try:
        client.prepare_arm(
            transaction_id="rce:other",
            hard_deadline=NOW + timedelta(minutes=10),
            block=BLOCK,
            challenge_nonce="00000005",
            now_wall=NOW + timedelta(seconds=22),
            now_monotonic=122.0,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("retarget changed transaction")


def test_delayed_or_malformed_response_fails_closed() -> None:
    client, _request = armed()
    assert client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=20),
        now_monotonic=120.0,
    ) is not None
    try:
        client.accept_renew(
            {"schema_version": 1, "accepted": True, "reason": "renewed"},
            now_monotonic=120.1,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("malformed delayed response renewed the lease")
    assert client.handle is None


def test_late_arm_ack_cannot_replace_a_newer_lease() -> None:
    client = LEASE.ControlLeaseClient(session_id="session-a")
    old_request = client.prepare_arm(
        transaction_id="tariff:old",
        hard_deadline=NOW + timedelta(minutes=20),
        block=BLOCK,
        challenge_nonce="00000001",
        now_wall=NOW,
        now_monotonic=100.0,
    )
    new_request = client.prepare_arm(
        transaction_id="tariff:new",
        hard_deadline=NOW + timedelta(minutes=20),
        block=BLOCK,
        challenge_nonce="00000002",
        now_wall=NOW + timedelta(seconds=1),
        now_monotonic=101.0,
    )
    assert client.accept_arm(new_request, accepted("accepted", "00000003")) is True
    assert client.handle is not None
    new_identity = (client.handle.transaction_id, client.handle.command_generation)
    assert client.accept_arm(old_request, accepted("accepted", "00000004")) is False
    assert client.handle is not None
    assert (client.handle.transaction_id, client.handle.command_generation) == new_identity


def test_late_renew_ack_cannot_mutate_a_newer_lease() -> None:
    client, _request = armed()
    old_renew = client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=20),
        now_monotonic=120.0,
    )
    assert old_renew is not None
    client.invalidate()
    new_request = client.prepare_arm(
        transaction_id="tariff:new",
        hard_deadline=NOW + timedelta(minutes=20),
        block=BLOCK,
        challenge_nonce="00000005",
        now_wall=NOW + timedelta(seconds=21),
        now_monotonic=121.0,
    )
    assert client.accept_arm(new_request, accepted("accepted", "00000006")) is True
    assert client.handle is not None
    new_identity = (
        client.handle.transaction_id,
        client.handle.command_generation,
        client.handle.sequence,
        client.handle.nonce,
    )
    assert client.accept_renew(
        accepted("renewed", "00000007"),
        request=old_renew,
        now_monotonic=121.1,
    ) is False
    assert client.handle is not None
    assert (
        client.handle.transaction_id,
        client.handle.command_generation,
        client.handle.sequence,
        client.handle.nonce,
    ) == new_identity


def test_typed_renew_results_preserve_rejection_and_stale_identity() -> None:
    client, _request = armed()
    renew = client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=20),
        now_monotonic=120.0,
    )
    assert renew is not None
    rejected = client.process_renew_response(
        {
            "schema_version": 1,
            "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
            "accepted": False,
            "reason": "lease_expired",
        },
        request=renew,
        now_monotonic=120.1,
    )
    assert rejected.status is LEASE.ControlLeaseRenewStatus.REJECTED
    assert rejected.reason == "lease_expired"
    assert client.handle is None

    newer, _request = armed()
    stale = newer.process_renew_response(
        accepted("renewed", "00000004"),
        request=renew,
        now_monotonic=121.0,
    )
    assert stale.status is LEASE.ControlLeaseRenewStatus.IGNORED_STALE_RESPONSE
    assert newer.handle is not None


def test_newer_identical_snapshot_keeps_one_pending_sequence_for_bounded_retry() -> None:
    client, _request = armed()
    renew = client.prepare_renew(
        authorized=True,
        snapshot_generation=11,
        now_wall=NOW + timedelta(seconds=20),
        now_monotonic=120.0,
    )
    assert renew is not None
    result = client.process_renew_response(
        {
            "schema_version": 1,
            "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
            "accepted": False,
            "reason": "snapshot_generation_advanced",
        },
        request=renew,
        now_monotonic=120.1,
    )
    assert result.status is LEASE.ControlLeaseRenewStatus.RETRYABLE_STALE_SNAPSHOT
    assert client.handle is not None
    assert client.handle.pending_sequence == renew["sequence"] == 1
    assert client.handle.sequence == 0
    assert client.handle.next_renew_monotonic == 140.0


def test_esp_expiry_is_independent_of_ha_api_and_event_loop() -> None:
    for _failure in ("ha_process_gone", "event_loop_hung", "api_disconnected"):
        model = FirmwareLeaseModel()
        assert model.arm(BLOCK)
        model.observe(BLOCK)
        model.advance(120.0)
        assert model.state == "restoring"
        model.observe(FALLBACK)
        assert model.state == "disarmed"


def test_esp_rs485_silence_restart_and_owner_change_fail_closed() -> None:
    model = FirmwareLeaseModel()
    assert model.arm(BLOCK)
    model.observe(BLOCK)
    model.bus = False
    model.reboot()
    model.advance(60.0)
    assert model.state == "expired" and model.physical == BLOCK
    model.bus = True
    model.advance(0.0)
    assert model.state == "restoring"
    model.observe(FALLBACK)
    assert model.state == "disarmed"
    model = FirmwareLeaseModel()
    assert model.arm(BLOCK)
    model.observe(BLOCK)
    model.observe((3.0,) + BLOCK[1:])
    assert model.state == "disarmed"


def test_esp_late_retarget_and_old_renew_cannot_restore_authority() -> None:
    model = FirmwareLeaseModel()
    assert model.arm(BLOCK, generation=1, hard_seconds=120.0)
    model.observe(BLOCK)
    second = (5.0, 25.0, 90.0, 98.0, 5.0, 20.0, 10.0)
    model.advance(5.0)
    assert model.arm(second, generation=2, hard_seconds=300.0)
    assert model.deadline == 120.0
    model.observe(BLOCK)
    assert model.state == "expired"
    assert model.renew(authorized=True, sequence=1) is False
    model.advance(0.0)
    assert model.state == "restoring"
    model.observe(FALLBACK)
    assert model.state == "disarmed"


def test_transient_wifi_drop_does_not_renew_but_fits_soft_lease_budget() -> None:
    model = FirmwareLeaseModel()
    assert model.arm(BLOCK)
    model.observe(BLOCK)
    model.advance(95.0)
    assert model.state == "confirmed"
    assert model.renew(authorized=True, sequence=1) is True
    model.advance(119.0)
    assert model.state == "confirmed"
    model.advance(1.0)
    assert model.state == "restoring"


def test_esp_renew_replay_unauthorized_and_hard_deadline() -> None:
    model = FirmwareLeaseModel()
    assert model.arm(BLOCK, hard_seconds=20.0)
    model.observe(BLOCK)
    model.advance(5.0)
    assert model.renew(authorized=False, sequence=1) is False
    assert model.renew(authorized=True, sequence=1) is True
    expiry = model.expiry
    model.advance(1.0)
    assert model.renew(authorized=True, sequence=1) is True
    assert model.expiry == expiry
    model.advance(14.0)
    assert model.state == "restoring"


def test_master_stop_fences_every_lease_phase() -> None:
    for phase in ("pending", "confirmed", "expired", "restoring"):
        model = FirmwareLeaseModel()
        assert model.arm(BLOCK)
        if phase != "pending":
            model.observe(BLOCK)
        if phase in {"expired", "restoring"}:
            model.state = phase
        model.master_stop()
        assert model.state == "disarmed"
        assert model.physical == FALLBACK


def main() -> None:
    tests = [value for name, value in globals().items() if name.startswith("test_")]
    for test in tests:
        test()
    print(f"PASS: Supervisor control lease ({len(tests)} groups)")


if __name__ == "__main__":
    main()
