#!/usr/bin/env python3
"""Source contract: the absent provider is normalized to explicit ``None``."""

from __future__ import annotations

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"


def _call_named(node: ast.AST, name: str) -> ast.Call:
    for candidate in ast.walk(node):
        if (
            isinstance(candidate, ast.Call)
            and isinstance(candidate.func, ast.Name)
            and candidate.func.id == name
        ):
            return candidate
    raise AssertionError(f"missing {name} call")


def _keyword(call: ast.Call, name: str) -> ast.AST:
    for item in call.keywords:
        if item.arg == name:
            return item.value
    raise AssertionError(f"missing {name} keyword")


def _is_none(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and node.value is None


def main() -> None:
    sensor_tree = ast.parse(
        (COMPONENT / "supervisor_sensor.py").read_text(encoding="utf-8")
    )
    build = next(
        node
        for node in ast.walk(sensor_tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_build_snapshots"
    )
    execution = _call_named(build, "ExecutionSourceSnapshot")
    assert _is_none(_keyword(execution, "grid_to_battery_power_w"))
    assert _is_none(_keyword(execution, "grid_to_battery_power_observed_at"))

    accounting_path = COMPONENT / "supervisor_accounting_runtime.py"
    accounting_text = accounting_path.read_text(encoding="utf-8")
    accounting_tree = ast.parse(accounting_text)
    evidence = _call_named(accounting_tree, "PhysicalExecutionEvidence")
    provider_sample = _keyword(evidence, "grid_to_battery_power_w")
    assert isinstance(provider_sample, ast.Call)
    assert _is_none(_keyword(provider_sample, "value"))
    assert _is_none(_keyword(provider_sample, "reported_at"))
    assert (
        '"grid_to_battery_power": "provider_absent.grid_to_battery_power"'
        in accounting_text
    )
    print(
        "Supervisor Grid-to-Battery provider absence: PASS "
        "(foreign global has no authority)"
    )


if __name__ == "__main__":
    main()
