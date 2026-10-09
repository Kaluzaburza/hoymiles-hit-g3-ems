#!/usr/bin/env python3
"""Static contract for the integration-owned EMS Supervisor MASTER STOP."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONST_PATH = ROOT / "custom_components/hoymiles_hit_modbus/const.py"
INIT_PATH = ROOT / "custom_components/hoymiles_hit_modbus/__init__.py"
SERVICES_PATH = ROOT / "custom_components/hoymiles_hit_modbus/services.yaml"


def _function(tree: ast.AST, name: str) -> ast.AsyncFunctionDef:
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    ]
    assert len(matches) == 1, f"expected one async function {name!r}"
    return matches[0]


def _calls(node: ast.AST, attribute: str) -> list[ast.Call]:
    return [
        candidate
        for candidate in ast.walk(node)
        if isinstance(candidate, ast.Call)
        and isinstance(candidate.func, ast.Attribute)
        and candidate.func.attr == attribute
    ]


def main() -> None:
    const_source = CONST_PATH.read_text(encoding="utf-8")
    init_source = INIT_PATH.read_text(encoding="utf-8")
    services_source = SERVICES_PATH.read_text(encoding="utf-8")
    const_tree = ast.parse(const_source)
    init_tree = ast.parse(init_source)

    constants = {
        target.id: node.value.value
        for node in const_tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance((target := node.targets[0]), ast.Name)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    assert constants.get("SERVICE_MASTER_STOP") == "master_stop"

    supervisor_imports = [
        node
        for node in init_tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "supervisor_sensor"
    ]
    assert len(supervisor_imports) == 1
    imported_names = {alias.name for alias in supervisor_imports[0].names}
    assert "async_request_supervisor_master_stop" in imported_names

    setup = _function(init_tree, "async_setup")
    handler = _function(setup, "async_handle_master_stop")
    awaits = [node for node in ast.walk(handler) if isinstance(node, ast.Await)]
    assert len(awaits) == 1, "MASTER STOP handler must block on one adapter call"
    awaited_call = awaits[0].value
    assert isinstance(awaited_call, ast.Call)
    assert isinstance(awaited_call.func, ast.Name)
    assert awaited_call.func.id == "async_request_supervisor_master_stop"
    assert len(awaited_call.args) == 1
    assert isinstance(awaited_call.args[0], ast.Name)
    assert awaited_call.args[0].id == "hass"
    assert not _calls(handler, "async_create_task")
    assert not _calls(handler, "async_call")

    handlers = [node for node in ast.walk(handler) if isinstance(node, ast.ExceptHandler)]
    assert len(handlers) == 2
    assert isinstance(handlers[0].type, ast.Name)
    assert handlers[0].type.id == "HomeAssistantError"
    assert isinstance(handlers[1].type, ast.Name)
    assert handlers[1].type.id == "Exception"
    raised_errors = [
        node.exc
        for node in ast.walk(handlers[1])
        if isinstance(node, ast.Raise) and node.exc is not None
    ]
    assert len(raised_errors) == 1
    assert isinstance(raised_errors[0], ast.Call)
    assert isinstance(raised_errors[0].func, ast.Name)
    assert raised_errors[0].func.id == "HomeAssistantError"

    registrations = _calls(setup, "async_register")
    master_stop_registrations = [
        call
        for call in registrations
        if len(call.args) >= 3
        and isinstance(call.args[1], ast.Name)
        and call.args[1].id == "SERVICE_MASTER_STOP"
    ]
    assert len(master_stop_registrations) == 1
    registration = master_stop_registrations[0]
    assert isinstance(registration.args[0], ast.Name)
    assert registration.args[0].id == "DOMAIN"
    assert isinstance(registration.args[2], ast.Name)
    assert registration.args[2].id == "async_handle_master_stop"
    assert not registration.keywords, "MASTER STOP service must expose no fields/schema"

    setup_entry = _function(init_tree, "async_setup_entry")
    assert not [
        call
        for call in _calls(setup_entry, "async_register")
        if any(
            isinstance(arg, ast.Name) and arg.id == "SERVICE_MASTER_STOP"
            for arg in call.args
        )
    ], "MASTER STOP must be registered globally, not per config entry"

    assert services_source.count("\nmaster_stop: {}\n") == 1

    forbidden = (
        "input_boolean.",
        "input_select.",
        "input_number.",
        "script.",
        "automation.",
    )
    handler_source = ast.get_source_segment(init_source, handler) or ""
    assert all(marker not in handler_source for marker in forbidden)

    class FakeHomeAssistantError(Exception):
        pass

    runtime_globals: dict[str, object] = {
        "HomeAssistantError": FakeHomeAssistantError,
        "ServiceCall": object,
        "hass": object(),
    }
    executable = ast.Module(body=[handler], type_ignores=[])
    ast.fix_missing_locations(executable)

    calls: list[object] = []

    async def successful_adapter(hass: object) -> None:
        await asyncio.sleep(0)
        calls.append(hass)

    runtime_globals["async_request_supervisor_master_stop"] = successful_adapter
    exec(compile(executable, "<master-stop-handler>", "exec"), runtime_globals)
    runtime_handler = runtime_globals["async_handle_master_stop"]
    assert callable(runtime_handler)
    asyncio.run(runtime_handler(object()))
    assert calls == [runtime_globals["hass"]]

    expected_error = FakeHomeAssistantError("transactional restore failed")

    async def home_assistant_error(_hass: object) -> None:
        raise expected_error

    runtime_globals["async_request_supervisor_master_stop"] = home_assistant_error
    try:
        asyncio.run(runtime_handler(object()))
    except FakeHomeAssistantError as err:
        assert err is expected_error, "HomeAssistantError must pass through unchanged"
    else:
        raise AssertionError("HomeAssistantError was swallowed")

    physical_error = RuntimeError("physical readback unavailable")

    async def unexpected_error(_hass: object) -> None:
        raise physical_error

    runtime_globals["async_request_supervisor_master_stop"] = unexpected_error
    try:
        asyncio.run(runtime_handler(object()))
    except FakeHomeAssistantError as err:
        assert err.__cause__ is physical_error
        assert "failed closed" in str(err)
    else:
        raise AssertionError("Unexpected adapter failure was swallowed")

    print("Supervisor MASTER STOP service contract: PASS")


if __name__ == "__main__":
    main()
