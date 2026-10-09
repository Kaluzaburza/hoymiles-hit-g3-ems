#!/usr/bin/env python3
"""HA persistence contract for the isolated Supervisor accounting-v2 entity."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

import test_supervisor_sensor_contract as base  # noqa: E402


AccountingSensor = base.SENSOR_PLATFORM.HoymilesSupervisorAccountingV2Sensor
CHECKS = 0
LEDGER = importlib.import_module(
    "custom_components.hoymiles_hit_modbus.supervisor_ledger"
)


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def physical_entry(at: datetime, *, suffix: str = "", power: float = 3000.0):
    sample_at = at - timedelta(seconds=5)
    intent = LEDGER.ExecutionIntent(
        policy_id=LEDGER.PolicyId.TARIFF,
        requested_action=LEDGER.RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        active=True,
        owner_kind=LEDGER.OwnerKind.TARIFF,
        active_action=LEDGER.TariffActiveAction.GRID_SUPPORT_AND_CHARGE,
        intent_fingerprint="a" * 64,
    )
    def sample(value, source):
        return LEDGER.EvidenceSample(value=value, reported_at=sample_at,
                                     source=f"sensor.{source}{suffix}")
    evidence = LEDGER.PhysicalExecutionEvidence(
        observed_at=at,
        mode_readback=sample(LEDGER.PhysicalMode.GRID_CHARGE, "mode"),
        readback_generation=sample(41, "generation"),
        readback_confirmed=True,
        grid_to_battery_power_w=sample(power, "grid_to_battery"),
        grid_power_w=sample(-4000.0, "grid"),
    )
    return LEDGER.build_execution_ledger_entry(intent, evidence)


def main() -> None:
    hass, entry, runtime, _matched = base._platform_environment(
        plan_registry_present=True
    )
    legacy_id = "sensor.hoymiles_tariff_grid_charge_energy_total"
    hass.states.values[legacy_id] = base.FakeState("12.345")
    sensor = AccountingSensor(hass, entry, runtime)
    base._add_platform_entity(sensor)

    check(sensor.available, "new accounting-v2 epoch is unavailable")
    check(sensor.native_value == 0.0, "legacy value leaked into v2 total")
    attributes = sensor.extra_state_attributes
    check(
        {"current_evidence_fingerprint", "current_provenance"}
        <= sensor._unrecorded_attributes,
        "per-frame evidence is still duplicated in Recorder",
    )
    check(
        "accepted_interval_count" not in sensor._unrecorded_attributes
        and "rejected_interval_count" not in sensor._unrecorded_attributes,
        "accounting transitions were excluded from Recorder",
    )
    check(attributes["storage_epoch"] == 2, "wrong accounting epoch")
    check(attributes["legacy_value_kwh"] == 12.345, "legacy cutover not frozen")
    check(attributes["legacy_included_in_v2"] is False, "legacy entered v2")
    check(
        attributes["legacy_invalid_reason"]
        == "pv_grid_attribution_contaminated",
        "legacy contamination reason changed",
    )

    store_key = f"hoymiles_hit_modbus.supervisor_accounting_v2.{entry.entry_id}"
    stored = deepcopy(hass.storage[store_key])
    check(stored["epoch_started_at"].endswith("Z"), "epoch timestamp missing")
    check(stored["legacy_v1_value_kwh"] == 12.345, "cutover not persisted")

    hass.states.values[legacy_id] = base.FakeState("99.0")
    check(
        sensor.extra_state_attributes["legacy_value_kwh"] == 12.345,
        "legacy diagnostic changed after v2 cutover",
    )
    restarted = AccountingSensor(hass, entry, runtime)
    base._add_platform_entity(restarted)
    check(restarted.available, "valid accounting store did not recover")
    check(
        restarted.extra_state_attributes["epoch_started_at"]
        == stored["epoch_started_at"],
        "restart changed deployment epoch",
    )
    check(
        restarted.extra_state_attributes["legacy_value_kwh"] == 12.345,
        "restart imported a newer legacy value",
    )

    at = datetime(2026, 9, 27, 13, 0, tzinfo=timezone.utc)
    active_entries = [
        physical_entry(at),
        physical_entry(at + timedelta(seconds=60), power=3600.0),
        physical_entry(at + timedelta(seconds=120), suffix="-other"),
    ]
    module = sys.modules[AccountingSensor.__module__]
    original_builder = module.build_active_accounting_entry
    pending = iter(active_entries)
    module.build_active_accounting_entry = lambda *_args: next(pending)
    try:
        for index, _ in enumerate(active_entries):
            asyncio.run(restarted.async_process_active_frame(
                SimpleNamespace(transaction=None), None, {}
            ))
            if index == 1:
                accepted_proof = restarted.extra_state_attributes[
                    "last_transition_proof"
                ]
                check(accepted_proof["accepted"] is True,
                      "accepted transition lacks durable proof")
                check(accepted_proof["interval_energy_kwh"] > 0,
                      "accepted transition lost physical energy")
                check(accepted_proof["previous_anchor_fingerprint"]
                      == active_entries[0].evidence_fingerprint,
                      "accepted transition lost prior anchor")
                check(accepted_proof["provenance"][0]["source"] == "sensor.mode",
                      "accepted transition lost source identity")
    finally:
        module.build_active_accounting_entry = original_builder
    after_transition = restarted.extra_state_attributes
    proof = after_transition["last_transition_proof"]
    check(proof["evidence_fingerprint"] == active_entries[-1].evidence_fingerprint,
          "rejected transition lost evidence fingerprint")
    check(proof["reason"] == after_transition["last_update_reason"],
          "rejected transition reason diverged")
    check(proof["provenance"][0]["source"] == "sensor.mode-other",
          "rejected transition lost source identity")
    check(proof["accepted_interval_count"] >= 1,
          "accepted transition was not accumulated")
    check(proof["rejected_interval_count"] >= 1,
          "rejected transition was not counted")
    check(hass.storage[store_key]["last_transition_proof"] == proof,
          "transition proof was not persisted")
    cold = AccountingSensor(hass, entry, runtime)
    base._add_platform_entity(cold)
    check(cold.extra_state_attributes["last_transition_proof"] == proof,
          "cold boot lost transition proof")
    check(cold.native_value == restarted.native_value,
          "cold boot changed accumulated energy")

    corrupted_proof = deepcopy(hass.storage[store_key])
    corrupted_proof["last_transition_proof"]["provenance"][0]["source"] = 42
    bad_proof_hass, bad_proof_entry, bad_proof_runtime, _ = (
        base._platform_environment(plan_registry_present=True)
    )
    bad_proof_key = (
        "hoymiles_hit_modbus.supervisor_accounting_v2."
        f"{bad_proof_entry.entry_id}"
    )
    bad_proof_hass.storage[bad_proof_key] = corrupted_proof
    bad_proof_sensor = AccountingSensor(
        bad_proof_hass, bad_proof_entry, bad_proof_runtime
    )
    base._add_platform_entity(bad_proof_sensor)
    check(not bad_proof_sensor.available,
          "corrupt transition proof silently restored")

    mismatched = deepcopy(hass.storage[store_key])
    mismatched["last_transition_proof"]["accepted_interval_count"] += 1
    mismatch_hass, mismatch_entry, mismatch_runtime, _ = (
        base._platform_environment(plan_registry_present=True)
    )
    mismatch_key = (
        "hoymiles_hit_modbus.supervisor_accounting_v2."
        f"{mismatch_entry.entry_id}"
    )
    mismatch_hass.storage[mismatch_key] = mismatched
    mismatch_sensor = AccountingSensor(
        mismatch_hass, mismatch_entry, mismatch_runtime
    )
    base._add_platform_entity(mismatch_sensor)
    check(not mismatch_sensor.available,
          "transition proof with wrong counters silently restored")

    bad_hass, bad_entry, bad_runtime, _ = base._platform_environment(
        plan_registry_present=True
    )
    bad_key = (
        "hoymiles_hit_modbus.supervisor_accounting_v2."
        f"{bad_entry.entry_id}"
    )
    bad_hass.storage[bad_key] = {"unexpected": True}
    corrupted = AccountingSensor(bad_hass, bad_entry, bad_runtime)
    base._add_platform_entity(corrupted)
    check(not corrupted.available, "corrupt v2 store was silently reset")
    check(corrupted.native_value is None, "corrupt v2 total published zero")
    check(
        bad_hass.storage[bad_key] == {"unexpected": True},
        "corrupt v2 store was overwritten",
    )

    print(f"Supervisor accounting sensor: PASS ({CHECKS} checks)")


if __name__ == "__main__":
    main()
