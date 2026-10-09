"""Deterministic tests for the isolated Supervisor accounting-v2 runtime."""

from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
SOURCE_PATH = COMPONENT / "supervisor_accounting_v2.py"
sys.path.insert(0, str(COMPONENT))

import supervisor_accounting_v2 as ACCOUNTING  # noqa: E402
import supervisor_ledger as LEDGER  # noqa: E402


PYTHON = sys.executable
BASE_TIME = datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc)
BASE_INTENT_FINGERPRINT = "a" * 64

DISALLOWED_IMPORT_ROOTS = {
    "aiohttp",
    "asyncio",
    "homeassistant",
    "os",
    "pathlib",
    "requests",
    "socket",
    "subprocess",
    "urllib",
}
DISALLOWED_SOURCE_MARKERS = (
    "async_write_ha_state",
    "hass.",
    "homeassistant",
    "modbus.write",
    "services.async_call",
    "store(",
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _expect_value_error(callable_: Callable[[], Any], message: str) -> None:
    try:
        callable_()
    except ValueError:
        return
    raise AssertionError(message)


def _entry(
    observed_at: datetime,
    *,
    power_w: float = 3000.0,
    grid_power_w: float = -4000.0,
    generation: int = 41,
    intent_fingerprint: str | None = BASE_INTENT_FINGERPRINT,
    requested_action: Any = None,
    active_action: Any = None,
    source_suffix: str = "",
    reported_at: datetime | None = None,
) -> Any:
    requested_action = requested_action or (
        LEDGER.RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE
    )
    active_action = active_action or (
        LEDGER.TariffActiveAction.GRID_SUPPORT_AND_CHARGE
    )
    sample_time = reported_at or observed_at - timedelta(seconds=5)
    intent = LEDGER.ExecutionIntent(
        policy_id=LEDGER.PolicyId.TARIFF,
        requested_action=requested_action,
        active=True,
        owner_kind=LEDGER.OwnerKind.TARIFF,
        active_action=active_action,
        intent_fingerprint=intent_fingerprint,
    )
    evidence = LEDGER.PhysicalExecutionEvidence(
        observed_at=observed_at,
        mode_readback=LEDGER.EvidenceSample(
            value=LEDGER.PhysicalMode.GRID_CHARGE,
            reported_at=sample_time,
            source=f"sensor.mode{source_suffix}",
        ),
        readback_generation=LEDGER.EvidenceSample(
            value=generation,
            reported_at=sample_time,
            source=f"sensor.generation{source_suffix}",
        ),
        readback_confirmed=True,
        grid_to_battery_power_w=LEDGER.EvidenceSample(
            value=power_w,
            reported_at=sample_time,
            source=f"sensor.grid_to_battery{source_suffix}",
        ),
        grid_power_w=LEDGER.EvidenceSample(
            value=grid_power_w,
            reported_at=sample_time,
            source=f"sensor.grid{source_suffix}",
        ),
    )
    return LEDGER.build_execution_ledger_entry(intent, evidence)


def _seed() -> tuple[Any, Any]:
    initial = ACCOUNTING.new_accounting_state()
    seeded = ACCOUNTING.accumulate_execution_entry(
        initial, _entry(BASE_TIME, power_w=3000.0)
    )
    _assert(not seeded.accepted, "first qualified observation must only seed")
    _assert(
        seeded.reason is ACCOUNTING.AccountingUpdateReason.SEEDED,
        "first qualified observation did not seed",
    )
    return initial, seeded


def test_exact_physical_integration_and_feedback() -> None:
    initial, seeded = _seed()
    first = ACCOUNTING.accumulate_execution_entry(
        seeded.state,
        _entry(BASE_TIME + timedelta(seconds=60), power_w=3600.0, generation=42),
    )
    expected_first = ((3000.0 + 3600.0) * 0.5 * 60.0) / 3_600_000.0
    _assert(first.accepted, "continuous qualified interval was rejected")
    _assert(first.interval_seconds == 60.0, "wrong interval duration")
    _assert(
        abs(first.interval_energy_kwh - expected_first) < 1e-12,
        "first trapezoidal energy is wrong",
    )
    _assert(
        first.delivered_power_feedback_w
        == first.attributed_power_w
        == LEDGER.attributed_power_for_feedback(
            _entry(
                BASE_TIME + timedelta(seconds=60),
                power_w=3600.0,
                generation=42,
            )
        ),
        "delivered feedback diverged from canonical attributed power",
    )

    second = ACCOUNTING.accumulate_execution_entry(
        first.state,
        _entry(BASE_TIME + timedelta(seconds=120), power_w=2400.0, generation=42),
    )
    expected_second = ((3600.0 + 2400.0) * 0.5 * 60.0) / 3_600_000.0
    _assert(second.accepted, "second continuous interval was rejected")
    _assert(
        abs(
            second.state.accumulated_energy_kwh
            - expected_first
            - expected_second
        )
        < 1e-12,
        "accumulated physical energy is wrong",
    )
    _assert(second.state.accepted_interval_count == 2, "accepted count drifted")
    _assert(initial.accumulated_energy_kwh == 0.0, "state was mutated in-place")


def test_gap_and_unqualified_evidence_break_continuity() -> None:
    _initial, seeded = _seed()
    gap = ACCOUNTING.accumulate_execution_entry(
        seeded.state,
        _entry(BASE_TIME + timedelta(seconds=121), generation=42),
    )
    _assert(not gap.accepted, "over-bound interval was accepted")
    _assert(
        gap.reason is ACCOUNTING.AccountingUpdateReason.INTERVAL_EXCEEDS_BOUND,
        "over-bound interval reason drifted",
    )
    _assert(gap.state.accumulated_energy_kwh == 0.0, "gap added energy")
    _assert(gap.state.delivered_power_feedback_w == 0.0, "gap leaked feedback")
    after_gap = ACCOUNTING.accumulate_execution_entry(
        gap.state, _entry(BASE_TIME + timedelta(seconds=150), generation=43)
    )
    _assert(
        after_gap.reason is ACCOUNTING.AccountingUpdateReason.SEEDED,
        "valid sample after a gap bridged the rejected interval",
    )

    stale = ACCOUNTING.accumulate_execution_entry(
        seeded.state,
        _entry(
            BASE_TIME + timedelta(seconds=60),
            generation=42,
            reported_at=BASE_TIME - timedelta(seconds=62),
        ),
    )
    _assert(
        stale.reason is ACCOUNTING.AccountingUpdateReason.ENTRY_NOT_QUALIFIED,
        "stale physical evidence did not fail closed",
    )
    _assert(stale.state.accumulated_energy_kwh == 0.0, "stale data added energy")
    resumed = ACCOUNTING.accumulate_execution_entry(
        stale.state,
        _entry(BASE_TIME + timedelta(seconds=70), power_w=5000.0, generation=43),
    )
    _assert(
        resumed.reason is ACCOUNTING.AccountingUpdateReason.SEEDED,
        "qualified evidence after stale data bridged the gap",
    )
    _assert(resumed.state.accumulated_energy_kwh == 0.0, "resume added gap energy")


def test_intent_action_and_cohort_continuity() -> None:
    _initial, seeded = _seed()
    cases = (
        (
            ACCOUNTING.AccountingUpdateReason.INTENT_DISCONTINUITY,
            _entry(
                BASE_TIME + timedelta(seconds=60),
                generation=42,
                intent_fingerprint="b" * 64,
            ),
        ),
        (
            ACCOUNTING.AccountingUpdateReason.ACTION_DISCONTINUITY,
            _entry(
                BASE_TIME + timedelta(seconds=60),
                generation=42,
                requested_action=LEDGER.RequestedAction.TARIFF_BATTERY_CHARGE,
                active_action=LEDGER.TariffActiveAction.BATTERY_CHARGE,
            ),
        ),
        (
            ACCOUNTING.AccountingUpdateReason.SOURCE_COHORT_DISCONTINUITY,
            _entry(
                BASE_TIME + timedelta(seconds=60),
                generation=42,
                source_suffix="_replacement",
            ),
        ),
        (
            ACCOUNTING.AccountingUpdateReason.READBACK_GENERATION_REGRESSED,
            _entry(BASE_TIME + timedelta(seconds=60), generation=40),
        ),
        (
            ACCOUNTING.AccountingUpdateReason.NON_MONOTONIC_OBSERVATION,
            _entry(BASE_TIME - timedelta(seconds=1), generation=42),
        ),
    )
    for expected, entry in cases:
        update = ACCOUNTING.accumulate_execution_entry(seeded.state, entry)
        _assert(not update.accepted, f"{expected.value} was accepted")
        _assert(update.reason is expected, f"wrong reason for {expected.value}")
        _assert(update.interval_energy_kwh == 0.0, f"{expected.value} added energy")
        _assert(
            update.delivered_power_feedback_w == 0.0,
            f"{expected.value} leaked delivered feedback",
        )
        _assert(
            update.state.anchor_observed_at is None,
            f"{expected.value} did not break the continuity anchor",
        )

    # Re-observing the exact same physical evidence with a later wall time is
    # not a new power sample and must not be integrated twice.
    original = _entry(BASE_TIME, generation=41)
    duplicate = replace(original, observed_at=BASE_TIME + timedelta(seconds=60))
    duplicate_update = ACCOUNTING.accumulate_execution_entry(
        seeded.state, duplicate
    )
    _assert(
        duplicate_update.reason
        is ACCOUNTING.AccountingUpdateReason.DUPLICATE_EVIDENCE,
        "duplicate evidence was integrated",
    )

    stale_provenance = replace(
        _entry(BASE_TIME + timedelta(seconds=60), generation=42),
        provenance=tuple(
            replace(
                item,
                status=(
                    LEDGER.EvidenceStatus.STALE
                    if item.field == "grid_power_w"
                    else item.status
                ),
            )
            for item in _entry(
                BASE_TIME + timedelta(seconds=60), generation=42
            ).provenance
        ),
    )
    cohort_update = ACCOUNTING.accumulate_execution_entry(
        seeded.state, stale_provenance
    )
    _assert(
        cohort_update.reason
        is ACCOUNTING.AccountingUpdateReason.EVIDENCE_COHORT_MISMATCH,
        "forged cohort provenance was accepted",
    )


def test_missing_intent_and_physical_only_contract() -> None:
    _initial, seeded = _seed()
    missing_intent = ACCOUNTING.accumulate_execution_entry(
        seeded.state,
        _entry(
            BASE_TIME + timedelta(seconds=60),
            generation=42,
            intent_fingerprint=None,
        ),
    )
    _assert(
        missing_intent.reason
        is ACCOUNTING.AccountingUpdateReason.MISSING_INTENT_FINGERPRINT,
        "missing intent fingerprint was accepted",
    )
    _assert(missing_intent.attributed_power_w == 0.0, "intent gap leaked power")

    pv_charge = ACCOUNTING.accumulate_execution_entry(
        seeded.state,
        _entry(
            BASE_TIME + timedelta(seconds=60),
            power_w=3500.0,
            grid_power_w=500.0,
            generation=42,
        ),
    )
    _assert(
        pv_charge.reason is ACCOUNTING.AccountingUpdateReason.ENTRY_NOT_QUALIFIED,
        "PV charging without physical grid import was billed",
    )
    _assert(pv_charge.state.accumulated_energy_kwh == 0.0, "PV charge added energy")
    _assert(pv_charge.delivered_power_feedback_w == 0.0, "PV charge fed learning")


def test_storage_epoch_and_canonical_round_trip() -> None:
    _initial, seeded = _seed()
    state = ACCOUNTING.accumulate_execution_entry(
        seeded.state,
        _entry(BASE_TIME + timedelta(seconds=60), power_w=3600.0, generation=42),
    ).state
    serialized = ACCOUNTING.serialize_accounting_state(state)
    restored = ACCOUNTING.deserialize_accounting_state(serialized)
    _assert(restored == state, "accounting state did not round-trip")
    _assert(
        ACCOUNTING.serialize_accounting_state(restored) == serialized,
        "accounting serialization is unstable",
    )
    _assert(
        len(serialized.encode("utf-8")) <= ACCOUNTING.MAX_SERIALIZED_STATE_BYTES,
        "accounting persistence exceeds its bound",
    )
    _assert(state.storage_epoch == 2, "runtime is not isolated in epoch v2")
    _assert(
        state.storage_key.endswith("supervisor_accounting_v2")
        and state.entity_unique_id.endswith("energy_v2"),
        "v2 storage/entity identity is not isolated",
    )
    _assert(
        ACCOUNTING.legacy_v1_diagnostic()
        == {
            "storage_epoch": 1,
            "included_in_v2": False,
            "invalid_reason": "pv_grid_attribution_contaminated",
        },
        "legacy v1 diagnostic contract drifted",
    )
    legacy = ACCOUNTING.accounting_state_to_dict(state)
    legacy["storage_epoch"] = 1
    _expect_value_error(
        lambda: ACCOUNTING.accounting_state_from_dict(legacy),
        "legacy accumulated state entered epoch v2",
    )
    noncanonical = json.dumps(
        ACCOUNTING.accounting_state_to_dict(state), ensure_ascii=True, indent=2
    )
    _expect_value_error(
        lambda: ACCOUNTING.deserialize_accounting_state(noncanonical),
        "non-canonical persistence text was accepted",
    )


def test_strict_persistence_mutations(*, report: bool = False) -> None:
    _initial, seeded = _seed()
    baseline = ACCOUNTING.accounting_state_to_dict(seeded.state)

    def mutate(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        payload = deepcopy(baseline)
        change(payload)
        return payload

    mutations: tuple[tuple[str, dict[str, Any]], ...] = (
        ("missing_field", mutate(lambda p: p.pop("storage_key"))),
        ("unknown_field", mutate(lambda p: p.__setitem__("unknown", 1))),
        ("schema_version", mutate(lambda p: p.__setitem__("storage_schema_version", 2))),
        ("schema_bool", mutate(lambda p: p.__setitem__("storage_schema_version", True))),
        ("legacy_epoch", mutate(lambda p: p.__setitem__("storage_epoch", 1))),
        ("storage_key", mutate(lambda p: p.__setitem__("storage_key", "legacy"))),
        ("unique_id", mutate(lambda p: p.__setitem__("entity_unique_id", "legacy"))),
        ("contract", mutate(lambda p: p.__setitem__("accounting_contract_id", "v1"))),
        ("legacy_reason", mutate(lambda p: p.__setitem__("legacy_v1_invalid_reason", "ok"))),
        ("negative_energy", mutate(lambda p: p.__setitem__("accumulated_energy_kwh", -1.0))),
        ("nan_energy", mutate(lambda p: p.__setitem__("accumulated_energy_kwh", float("nan")))),
        ("accepted_bool", mutate(lambda p: p.__setitem__("accepted_interval_count", True))),
        ("accepted_negative", mutate(lambda p: p.__setitem__("accepted_interval_count", -1))),
        ("rejected_overflow", mutate(lambda p: p.__setitem__("rejected_interval_count", ACCOUNTING.MAX_COUNTER_VALUE + 1))),
        ("timestamp_offset", mutate(lambda p: p.__setitem__("anchor_observed_at", "2026-08-31T12:00:00+02:00"))),
        ("timestamp_invalid", mutate(lambda p: p.__setitem__("anchor_observed_at", "invalidZ"))),
        ("intent_fingerprint", mutate(lambda p: p.__setitem__("anchor_intent_fingerprint", "A" * 64))),
        ("missing_action", mutate(lambda p: p.__setitem__("anchor_requested_action", None))),
        ("wrong_action", mutate(lambda p: p.__setitem__("anchor_requested_action", "tariff_grid_support"))),
        ("missing_generation", mutate(lambda p: p.__setitem__("anchor_readback_generation", None))),
        ("negative_generation", mutate(lambda p: p.__setitem__("anchor_readback_generation", -1))),
        ("evidence_fingerprint", mutate(lambda p: p.__setitem__("anchor_evidence_fingerprint", "bad"))),
        ("source_fingerprint", mutate(lambda p: p.__setitem__("anchor_source_cohort_fingerprint", "bad"))),
        ("feedback_divergence", mutate(lambda p: p.__setitem__("delivered_power_feedback_w", 2999.0))),
    )
    detected = 0
    for name, payload in mutations:
        _expect_value_error(
            lambda payload=payload: ACCOUNTING.accounting_state_from_dict(payload),
            f"strict decoder accepted mutation {name}",
        )
        detected += 1
    _assert(detected == 24, f"mutation gate detected only {detected}/24")
    if report:
        print("Supervisor accounting v2 mutation gate: detected 24/24; survivors 0")


def test_pure_immutable_module() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SOURCE_PATH))
    imports: set[str] = set()
    for node in ast.walk(tree):
        _assert(
            not isinstance(node, ast.AsyncFunctionDef),
            "pure accounting runtime must not define async I/O hooks",
        )
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.lstrip(".").split(".", 1)[0])
    _assert(
        imports.isdisjoint(DISALLOWED_IMPORT_ROOTS),
        "accounting runtime imports I/O modules: "
        f"{sorted(imports & DISALLOWED_IMPORT_ROOTS)}",
    )
    lowered = source.lower()
    for marker in DISALLOWED_SOURCE_MARKERS:
        _assert(marker not in lowered, f"accounting runtime contains {marker!r}")
    for dataclass_type in (
        ACCOUNTING.AccountingV2State,
        ACCOUNTING.AccountingUpdate,
    ):
        _assert(
            dataclass_type.__dataclass_params__.frozen,
            f"{dataclass_type.__name__} must be immutable",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mutations", action="store_true")
    args = parser.parse_args()
    test_exact_physical_integration_and_feedback()
    test_gap_and_unqualified_evidence_break_continuity()
    test_intent_action_and_cohort_continuity()
    test_missing_intent_and_physical_only_contract()
    test_storage_epoch_and_canonical_round_trip()
    test_strict_persistence_mutations(report=args.mutations)
    test_pure_immutable_module()
    print("Supervisor accounting v2 tests: PASS (bounded physical integration)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
