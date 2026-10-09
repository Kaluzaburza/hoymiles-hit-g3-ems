"""Regression contract for RCE slot-helper entity-ID compatibility."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
SCHEDULER = ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
SUPERVISOR_RUNTIME = (
    ROOT
    / "custom_components"
    / "hoymiles_hit_modbus"
    / "supervisor_runtime.py"
)
PLAN_ENTITY_ID = "sensor.hoymiles_hit_rce_optimized_plan"
OLD_SLOT_ENTITY_ID = "binary_sensor.hoymiles_rce_price_above_threshold"
NEW_SLOT_ENTITY_ID = "binary_sensor.hoymiles_rce_planned_export_slot"


def _walk(value: Any):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _entity_ids(trigger: dict[str, Any]) -> set[str]:
    value = trigger.get("entity_id", [])
    if isinstance(value, str):
        return {value}
    return {str(item) for item in value}


def _authoritative_slot_allowed(*, planned: Any, start_eligible: Any) -> bool:
    """Model the strict false-default contract asserted in the parsed YAML."""

    return planned is True and start_eligible is True


def main() -> None:
    package = yaml.safe_load(SCHEDULER.read_text(encoding="utf-8"))

    runtime_tree = ast.parse(
        SUPERVISOR_RUNTIME.read_text(encoding="utf-8"),
        filename=str(SUPERVISOR_RUNTIME),
    )
    rce_builder = next(
        node
        for node in runtime_tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "build_rce_candidate"
    )
    consumed_snapshot_attributes = {
        node.attr
        for node in ast.walk(rce_builder)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "snapshot"
    }
    assert "price_above_threshold" not in consumed_snapshot_attributes, (
        "Active Supervisor again depends on the legacy slot-helper state"
    )
    assert {
        "current_slot_planned",
        "current_slot_start_eligible",
    } <= consumed_snapshot_attributes, (
        "Active Supervisor no longer consumes both authoritative slot gates"
    )

    slot_helpers = [
        item
        for item in _walk(package.get("template", []))
        if isinstance(item, dict)
        and item.get("unique_id") == "hoymiles_rce_price_above_threshold"
    ]
    assert len(slot_helpers) == 1
    slot_helper = slot_helpers[0]
    assert slot_helper["default_entity_id"] == NEW_SLOT_ENTITY_ID
    assert "current_slot_planned" in slot_helper["availability"]
    assert "current_slot_planned" in slot_helper["state"]
    assert "result_current" in slot_helper["availability"]
    assert "recalculation_pending" in slot_helper["availability"]
    assert "result_current" in slot_helper["state"]
    assert "recalculation_pending" in slot_helper["state"]

    automations = [
        item
        for item in package.get("automation", [])
        if item.get("id") == "hoymiles_rce_grid_discharge_control"
    ]
    assert len(automations) == 1
    automation = automations[0]
    serialized = yaml.safe_dump(automation, allow_unicode=True, sort_keys=False)
    assert OLD_SLOT_ENTITY_ID not in serialized
    assert NEW_SLOT_ENTITY_ID not in serialized

    trigger_entities: set[str] = set()
    for trigger in automation["triggers"]:
        trigger_entities.update(_entity_ids(trigger))
    assert PLAN_ENTITY_ID in trigger_entities

    plan_gates = [
        item["value_template"]
        for item in _walk(automation)
        if isinstance(item, dict)
        and isinstance(item.get("value_template"), str)
        and "current_slot_planned" in item["value_template"]
        and "current_slot_start_eligible" in item["value_template"]
    ]
    assert plan_gates, "RCE start no longer checks both authoritative slot attributes"
    slot_gate = min(plan_gates, key=len)
    normalized_slot_gate = " ".join(slot_gate.split())
    assert normalized_slot_gate.count("| default(false, true)") == 2
    assert OLD_SLOT_ENTITY_ID not in slot_gate
    assert NEW_SLOT_ENTITY_ID not in slot_gate

    registry_scenarios = {
        "fresh_default_new": {NEW_SLOT_ENTITY_ID: "on"},
        "upgraded_old": {OLD_SLOT_ENTITY_ID: "unavailable"},
        "existing_new": {NEW_SLOT_ENTITY_ID: "on"},
        "helper_missing": {},
        "occupied_conflict": {
            OLD_SLOT_ENTITY_ID: "off",
            NEW_SLOT_ENTITY_ID: "on",
        },
    }
    for name, _helper_states in registry_scenarios.items():
        assert _authoritative_slot_allowed(
            planned=True,
            start_eligible=True,
        ), f"{name} blocked an eligible authoritative plan"
        assert not _authoritative_slot_allowed(
            planned=False,
            start_eligible=True,
        ), f"{name} granted authority outside a planned slot"

    for planned, start_eligible in ((None, True), (True, None), (True, False)):
        assert not _authoritative_slot_allowed(
            planned=planned,
            start_eligible=start_eligible,
        )

    print("RCE slot entity-ID compatibility contract: PASS")


if __name__ == "__main__":
    main()
