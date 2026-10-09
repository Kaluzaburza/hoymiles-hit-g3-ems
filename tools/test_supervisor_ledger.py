"""Deterministic contract tests for canonical EMS accounting ledger v2.

The fixture is deliberately static.  Its 24 one-variable mutations cover the
complete fail-closed attribution chain and preserve the exact tariff executor
action identity.  No test imports Home Assistant or exercises live services.
"""

from __future__ import annotations

import argparse
import ast
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
SOURCE_PATH = COMPONENT / "supervisor_ledger.py"
FIXTURE_PATH = ROOT / "tools" / "supervisor_ledger_v2_cases.json"
sys.path.insert(0, str(COMPONENT))

import supervisor_ledger as LEDGER  # noqa: E402


EXPECTED_CASE_IDS = tuple(f"M{index:02d}_" for index in range(1, 25))
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
    "grant_execution",
    "hass.services",
    "modbus.write",
    "owner_acquire",
    "services.async_call",
)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _expect_value_error(callable_: Any, message: str) -> None:
    try:
        callable_()
    except ValueError:
        return
    raise AssertionError(message)


def _load_fixture() -> dict[str, Any]:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    _assert(
        fixture.get("schema") == "hoymiles.supervisor-ledger.cases.v1",
        "unexpected ledger fixture schema",
    )
    _assert(fixture.get("case_count") == 24, "fixture must declare 24 cases")
    _assert(len(fixture.get("cases", ())) == 24, "fixture must contain 24 cases")
    return fixture


def _deep_merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _timestamp(value: Any) -> datetime | None:
    if value is None:
        return None
    _assert(type(value) is str, f"fixture timestamp must be a string: {value!r}")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _sample(payload: dict[str, Any], *, mode: bool = False) -> Any:
    value = payload.get("value")
    if mode and isinstance(value, str):
        try:
            value = LEDGER.PhysicalMode(value)
        except ValueError:
            pass
    return LEDGER.EvidenceSample(
        value=value,
        reported_at=_timestamp(payload.get("reported_at")),
        source=payload.get("source"),
    )


def _inputs(payload: dict[str, Any]) -> tuple[Any, Any]:
    intent_payload = payload["intent"]
    evidence_payload = payload["evidence"]
    intent = LEDGER.ExecutionIntent(
        policy_id=LEDGER.PolicyId(intent_payload["policy_id"]),
        requested_action=LEDGER.RequestedAction(
            intent_payload["requested_action"]
        ),
        active=intent_payload["active"],
        owner_kind=LEDGER.OwnerKind(intent_payload["owner_kind"]),
        active_action=LEDGER.TariffActiveAction(
            intent_payload["active_action"]
        ),
        intent_fingerprint=intent_payload.get("intent_fingerprint"),
    )
    evidence = LEDGER.PhysicalExecutionEvidence(
        observed_at=_timestamp(evidence_payload["observed_at"]),
        mode_readback=_sample(evidence_payload["mode_readback"], mode=True),
        readback_generation=_sample(evidence_payload["readback_generation"]),
        readback_confirmed=evidence_payload["readback_confirmed"],
        grid_to_battery_power_w=_sample(
            evidence_payload["grid_to_battery_power_w"]
        ),
        grid_power_w=_sample(evidence_payload["grid_power_w"]),
    )
    return intent, evidence


def _entry(payload: dict[str, Any]) -> Any:
    intent, evidence = _inputs(payload)
    return LEDGER.build_execution_ledger_entry(intent, evidence)


def _result(entry: Any) -> dict[str, Any]:
    return {
        "qualified": entry.qualified,
        "reason_code": entry.reason_code.value,
        "classification": entry.classification.value,
        "actual_flow": entry.actual_flow.value,
        "intent_matched": entry.intent_matched,
        "active_action": entry.active_action.value,
        "attributed_power_w": entry.attributed_power_w,
        "feedback_power_w": LEDGER.attributed_power_for_feedback(entry),
    }


def _result_digest(entries: list[tuple[str, Any]]) -> str:
    payload = [
        {
            "id": case_id,
            "entry": LEDGER.execution_ledger_entry_to_dict(entry),
        }
        for case_id, entry in entries
    ]
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _case_entries(
    fixture: dict[str, Any],
) -> tuple[Any, list[tuple[str, Any, dict[str, Any]]]]:
    base_payload = fixture["base"]
    baseline = _entry(base_payload)
    cases = [
        (
            case["id"],
            _entry(_deep_merge(base_payload, case["overrides"])),
            case["expected"],
        )
        for case in fixture["cases"]
    ]
    return baseline, cases


def test_static_matrix_and_drift(
    fixture: dict[str, Any], *, enforce_digest: bool = True
) -> str:
    baseline, cases = _case_entries(fixture)
    _assert(
        _result(baseline) == fixture["baseline_expected"],
        f"baseline result drifted: {_result(baseline)!r}",
    )
    case_ids = [case_id for case_id, _entry_, _expected in cases]
    _assert(len(case_ids) == len(set(case_ids)) == 24, "case ids must be unique")
    for prefix, case_id in zip(EXPECTED_CASE_IDS, case_ids, strict=True):
        _assert(case_id.startswith(prefix), f"case order/id drift: {case_id}")

    detected = 0
    for case_id, entry, expected in cases:
        actual = _result(entry)
        _assert(actual == expected, f"{case_id}: {actual!r} != {expected!r}")
        _assert(
            actual != fixture["baseline_expected"],
            f"{case_id}: mutation did not alter the public decision",
        )
        _assert(
            entry.evidence_fingerprint != baseline.evidence_fingerprint,
            f"{case_id}: mutation did not alter evidence fingerprint",
        )
        detected += 1
    _assert(detected == 24, f"mutation gate detected only {detected}/24")

    digest_entries = [("BASELINE", baseline)] + [
        (case_id, entry) for case_id, entry, _expected in cases
    ]
    digest = _result_digest(digest_entries)
    if enforce_digest:
        _assert(
            digest == fixture["expected_results_sha256"],
            "ledger golden drift: "
            f"expected {fixture['expected_results_sha256']}, got {digest}",
        )
    return digest


def test_exact_action_identity(fixture: dict[str, Any]) -> None:
    baseline, cases = _case_entries(fixture)
    entries = {case_id: entry for case_id, entry, _expected in cases}
    _assert(
        baseline.active_action
        is LEDGER.TariffActiveAction.GRID_SUPPORT_AND_CHARGE,
        "baseline action identity was not preserved",
    )
    battery_charge = entries["M02_BATTERY_CHARGE_IDENTITY_IS_PRESERVED"]
    _assert(
        battery_charge.active_action is LEDGER.TariffActiveAction.BATTERY_CHARGE,
        "battery_charge identity was collapsed",
    )
    _assert(
        battery_charge.attributed_power_w == 3000.0,
        "battery_charge should qualify with confirmed physical flow",
    )
    grid_support = entries["M07_GRID_SUPPORT_IS_NOT_BATTERY_ACCOUNTING"]
    _assert(
        grid_support.active_action is LEDGER.TariffActiveAction.GRID_SUPPORT,
        "grid_support identity was collapsed",
    )
    _assert(
        grid_support.attributed_power_w == 0.0,
        "grid_support must not be attributed as battery charging",
    )


def test_epoch_v2_isolation(fixture: dict[str, Any]) -> None:
    entry = _entry(fixture["base"])
    _assert(
        entry.accounting_epoch == 2
        and entry.accounting_contract_id
        == "ems.physical-grid-to-battery.v2",
        "baseline is not accounting epoch v2",
    )
    _assert(
        LEDGER.attributed_power_for_feedback(entry) == entry.attributed_power_w,
        "feedback did not use the canonical attributed power",
    )

    legacy_epoch = replace(entry, accounting_epoch=1)
    legacy_contract = replace(
        entry, accounting_contract_id="ems.physical-grid-to-battery.v1"
    )
    for label, legacy in (
        ("epoch", legacy_epoch),
        ("contract", legacy_contract),
    ):
        _assert(
            LEDGER.attributed_power_for_feedback(legacy) == 0.0,
            f"legacy {label} leaked into delivered-power feedback",
        )
        _expect_value_error(
            lambda legacy=legacy: LEDGER.execution_ledger_entry_to_dict(legacy),
            f"legacy {label} was serialized as v2",
        )

    forged = (
        replace(entry, attributed_power_w=4000.0),
        replace(entry, grid_to_battery_power_w=2500.0),
        replace(entry, grid_power_w=-2000.0),
        replace(entry, confirmed_grid_import_power_w=3500.0),
        replace(entry, qualified=False),
    )
    for index, candidate in enumerate(forged, start=1):
        _assert(
            LEDGER.attributed_power_for_feedback(candidate) == 0.0,
            f"forged ledger variant {index} reached feedback",
        )


def test_canonical_serialization_and_provenance(
    fixture: dict[str, Any]
) -> None:
    entry = _entry(fixture["base"])
    serialized = LEDGER.serialize_execution_ledger_entry(entry)
    _assert(
        serialized == LEDGER.serialize_execution_ledger_entry(entry),
        "unstable JSON",
    )
    _assert(
        len(serialized.encode("utf-8")) <= LEDGER.MAX_LEDGER_SERIALIZED_BYTES,
        "ledger projection exceeds its bounded contract",
    )
    parsed = json.loads(serialized)
    _assert(parsed["accounting_epoch"] == 2, "serialized epoch mismatch")
    _assert(
        parsed["accounting_source"] == "physical_grid_to_battery",
        "serialized source mismatch",
    )
    _assert(
        len(parsed["provenance"]) == 4
        and {item["field"] for item in parsed["provenance"]}
        == {
            "mode_readback",
            "readback_generation",
            "grid_to_battery_power_w",
            "grid_power_w",
        },
        "physical source provenance is incomplete",
    )
    _assert(
        all(item["status"] == "fresh" for item in parsed["provenance"]),
        "baseline provenance is not fresh",
    )

    negative_zero_payload = _deep_merge(
        fixture["base"],
        {"evidence": {"grid_to_battery_power_w": {"value": -0.0}}},
    )
    negative_zero = json.loads(
        LEDGER.serialize_execution_ledger_entry(_entry(negative_zero_payload))
    )
    _assert(
        negative_zero["grid_to_battery_power_w"] == 0.0,
        "canonical ledger JSON retained negative zero",
    )


def test_timezone_canonicalization(fixture: dict[str, Any]) -> None:
    intent, evidence = _inputs(fixture["base"])
    baseline = LEDGER.build_execution_ledger_entry(intent, evidence)
    plus_two = timezone(timedelta(hours=2))

    def shifted(sample: Any) -> Any:
        return replace(
            sample,
            reported_at=(
                sample.reported_at.astimezone(plus_two)
                if sample.reported_at is not None
                else None
            ),
        )

    shifted_evidence = replace(
        evidence,
        observed_at=evidence.observed_at.astimezone(plus_two),
        mode_readback=shifted(evidence.mode_readback),
        readback_generation=shifted(evidence.readback_generation),
        grid_to_battery_power_w=shifted(evidence.grid_to_battery_power_w),
        grid_power_w=shifted(evidence.grid_power_w),
    )
    shifted_entry = LEDGER.build_execution_ledger_entry(intent, shifted_evidence)
    _assert(
        baseline.evidence_fingerprint == shifted_entry.evidence_fingerprint,
        "equivalent timestamp offsets changed the evidence fingerprint",
    )
    _assert(
        LEDGER.serialize_execution_ledger_entry(baseline)
        == LEDGER.serialize_execution_ledger_entry(shifted_entry),
        "equivalent timestamp offsets changed canonical serialization",
    )


def test_malformed_evidence_fails_closed(fixture: dict[str, Any]) -> None:
    intent, evidence = _inputs(fixture["base"])
    malformed_samples = (
        replace(evidence.grid_to_battery_power_w, value=float("nan")),
        replace(evidence.grid_to_battery_power_w, source="sensor.invalid source"),
        replace(
            evidence.grid_to_battery_power_w,
            reported_at=evidence.grid_to_battery_power_w.reported_at.replace(
                tzinfo=None
            ),
        ),
    )
    for index, sample in enumerate(malformed_samples, start=1):
        entry = LEDGER.build_execution_ledger_entry(
            intent, replace(evidence, grid_to_battery_power_w=sample)
        )
        _assert(not entry.qualified, f"malformed sample {index} qualified")
        _assert(entry.attributed_power_w == 0.0, f"malformed sample {index} billed")
        _assert(
            LEDGER.attributed_power_for_feedback(entry) == 0.0,
            f"malformed sample {index} entered feedback",
        )
        serialized = LEDGER.serialize_execution_ledger_entry(entry)
        _assert("NaN" not in serialized, f"malformed sample {index} emitted NaN")

    _expect_value_error(
        lambda: LEDGER.build_execution_ledger_entry(
            replace(intent, active=1), evidence
        ),
        "non-boolean intent was accepted",
    )
    _expect_value_error(
        lambda: LEDGER.build_execution_ledger_entry(
            intent,
            replace(
                evidence,
                observed_at=evidence.observed_at.replace(tzinfo=None),
            ),
        ),
        "naive observation time was accepted",
    )


def test_pure_observational_module() -> None:
    source = SOURCE_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SOURCE_PATH))
    imports: set[str] = set()
    for node in ast.walk(tree):
        _assert(
            not isinstance(node, ast.AsyncFunctionDef),
            "pure ledger must not define asynchronous runtime hooks",
        )
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.lstrip(".").split(".", 1)[0])
    _assert(
        imports.isdisjoint(DISALLOWED_IMPORT_ROOTS),
        "pure ledger imports runtime/I/O modules: "
        f"{sorted(imports & DISALLOWED_IMPORT_ROOTS)}",
    )
    lowered = source.lower()
    for marker in DISALLOWED_SOURCE_MARKERS:
        _assert(
            marker not in lowered,
            f"pure ledger contains actuator marker {marker!r}",
        )
    for dataclass_type in (
        LEDGER.EvidenceSample,
        LEDGER.ExecutionIntent,
        LEDGER.PhysicalExecutionEvidence,
        LEDGER.EvidenceProvenance,
        LEDGER.ExecutionLedgerEntry,
    ):
        _assert(
            dataclass_type.__dataclass_params__.frozen,
            f"{dataclass_type.__name__} must be immutable",
        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--show-drift-digest",
        action="store_true",
        help="print the current result digest without enforcing the frozen value",
    )
    parser.add_argument(
        "--mutations",
        action="store_true",
        help="run and explicitly report the deterministic 24-case mutation gate",
    )
    args = parser.parse_args()
    fixture = _load_fixture()
    digest = test_static_matrix_and_drift(
        fixture, enforce_digest=not args.show_drift_digest
    )
    if args.show_drift_digest:
        print(digest)
        return 0

    test_exact_action_identity(fixture)
    test_epoch_v2_isolation(fixture)
    test_canonical_serialization_and_provenance(fixture)
    test_timezone_canonicalization(fixture)
    test_malformed_evidence_fails_closed(fixture)
    test_pure_observational_module()
    if args.mutations:
        print("Supervisor ledger v2 mutation gate: detected 24/24; survivors 0")
    else:
        print("Supervisor ledger v2 tests: PASS (24 deterministic cases)")
    print(f"Supervisor ledger v2 drift digest: {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
