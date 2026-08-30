"""Contract tests for non-blocking, serialized Home Assistant optimizers."""

from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
import threading
from types import MethodType, SimpleNamespace
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
SCHEDULER = ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
SENSORS = {
    "rce_sensor.py": ("HoymilesRCEOptimizerSensor", "optimize_rce"),
    "tariff_sensor.py": (
        "HoymilesTariffOptimizerSensor",
        "optimize_tariff_charging",
    ),
    "rcm_sensor.py": ("HoymilesRCMOptimizerSensor", "optimize_rcm"),
}


def _load_revision_module():
    path = COMPONENT / "optimizer_revision.py"
    spec = importlib.util.spec_from_file_location(
        "hoymiles_optimizer_revision_contract",
        path,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assert_revision_fingerprint_contract() -> None:
    revision_module = _load_revision_module()
    revision = revision_module.OptimizerInputRevision()
    old = SimpleNamespace(
        state="ready",
        attributes={"price": 1.0, "result_current": True},
        last_updated="old",
    )
    diagnostic_only = SimpleNamespace(
        state="waiting",
        attributes={"price": 1.0, "result_current": False},
        last_updated="new",
    )
    assert not revision.invalidate_state_change(
        old,
        diagnostic_only,
        attributes=("price",),
        include_state=False,
        include_last_updated=False,
    ), "Diagnostic publication caused a cross-optimizer revision ping-pong"
    consumed_change = SimpleNamespace(
        state="waiting",
        attributes={"price": 1.1, "result_current": False},
        last_updated="newer",
    )
    assert revision.invalidate_state_change(
        diagnostic_only,
        consumed_change,
        attributes=("price",),
        include_state=False,
        include_last_updated=False,
    )
    captured = revision.value
    revision.invalidate()
    assert not revision.is_current(captured)

    physical_old = SimpleNamespace(
        state="50",
        attributes={},
        last_reported="report-one",
        last_updated="unchanged",
    )
    physical_refreshed = SimpleNamespace(
        state="50",
        attributes={},
        last_reported="report-two",
        last_updated="unchanged",
    )
    assert revision.invalidate_state_change(
        physical_old,
        physical_refreshed,
        include_last_updated=True,
    ), "A fresh identical physical report did not invalidate signed-age inputs"


async def _assert_dirty_result_is_never_committed() -> None:
    """Change an input mid-executor and commit only the latest snapshot."""
    revision_module = _load_revision_module()
    revision = revision_module.OptimizerInputRevision()

    class States:
        def __init__(self) -> None:
            self.value = SimpleNamespace(
                state="80",
                attributes={},
                last_updated="one",
            )

        def get(self, entity_id: str) -> Any:
            assert entity_id == "sensor.battery_soc"
            return self.value

    hass = SimpleNamespace(states=States())
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    attempts = 0
    committed: list[str] = []

    async def solve(snapshot: str) -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            first_started.set()
            await release_first.wait()
        return snapshot

    async def run_latest() -> None:
        for _attempt in range(revision_module.MAX_IMMEDIATE_RECALCULATIONS):
            captured_revision = revision.value
            captured_fingerprint = revision_module.optimizer_input_fingerprint(
                hass,
                ("sensor.battery_soc",),
            )
            result = await solve(hass.states.value.state)
            if (
                not revision.is_current(captured_revision)
                or captured_fingerprint
                != revision_module.optimizer_input_fingerprint(
                    hass,
                    ("sensor.battery_soc",),
                )
            ):
                continue
            committed.append(result)
            return

    task = asyncio.create_task(run_latest())
    await asyncio.wait_for(first_started.wait(), timeout=5.0)
    hass.states.value = SimpleNamespace(
        state="20",
        attributes={},
        last_updated="two",
    )
    revision.invalidate()
    release_first.set()
    await asyncio.wait_for(task, timeout=5.0)
    assert attempts == 2
    assert committed == ["20"], "The stale executor result was committed"


def _assert_scheduler_result_current_gates() -> None:
    source = SCHEDULER.read_text(encoding="utf-8")
    rce_ready = source[
        source.index("unique_id: hoymiles_rce_control_data_ready") :
        source.index("unique_id: hoymiles_ems_export_allowed")
    ]
    tariff_ready = source[
        source.index("unique_id: hoymiles_tariff_control_data_ready") :
        source.index("unique_id: hoymiles_ems_control_conflict")
    ]
    assert "'result_current') is sameas true" in rce_ready
    assert "'result_current') is sameas true" in tariff_ready
    rce_post_ack = source[
        source.index("# A successful mode ACK is not permission") :
        source.index('stop: "RCE authorization lost after Grid Discharge ACK"')
    ]
    assert "condition: or" in rce_post_ack
    assert "value_template: *rce_pending_latched_execution_safe" in rce_post_ack, (
        "A normal optimizer refresh can roll back a physically confirmed RCE start"
    )
    rcm_control = source[
        source.index("id: hoymiles_rcm_voltage_charge_control") :
        source.index("id: hoymiles_rcm_pre_discharge_control")
    ]
    rcm_pre_discharge = source[
        source.index("id: hoymiles_rcm_pre_discharge_control") :
    ]
    for label, section in (
        ("RCEm voltage control", rcm_control),
        ("RCEm pre-discharge", rcm_pre_discharge),
    ):
        assert "result_current: >-" in section, f"{label} lacks a current-result variable"
        assert "and result_current" in section, f"{label} can start from a stale plan"
        assert "or not result_current" not in section, (
            f"{label} treats normal in-flight recalculation as a hardware failure"
        )


def _literal_string_set(tree: ast.Module, name: str) -> set[str]:
    matches = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        )
    ]
    assert len(matches) == 1, f"Expected exactly one {name} assignment"
    value = matches[0].value
    assert value is not None
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
    ):
        assert len(value.args) == 1
        value = value.args[0]
    assert isinstance(value, (ast.Set, ast.Tuple, ast.List))
    return {
        item.value
        for item in value.elts
        if isinstance(item, ast.Constant) and isinstance(item.value, str)
    }


def _assert_tariff_feedback_is_not_a_planning_input() -> None:
    """Execution feedback must not invalidate an accepted tariff plan."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    watched = _literal_string_set(tree, "WATCHED_TARIFF_ENTITIES")
    feedback = _literal_string_set(tree, "TARIFF_EXECUTION_FEEDBACK_ENTITIES")
    expected_feedback = {
        "input_boolean.hoymiles_tariff_charge_active",
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "sensor.hoymiles_hit_grid_to_battery_power",
        "sensor.hoymiles_hit_overview_load_active_power",
        "sensor.hoymiles_tariff_grid_charge_power",
    }
    assert feedback == expected_feedback
    assert watched.isdisjoint(feedback), (
        "Execution-only tariff feedback can withdraw planning authority"
    )

    # Genuine mathematical/freshness inputs must remain authoritative inputs.
    for entity_id in (
        "sensor.hoymiles_hit_overview_battery_soc",
        "sensor.hoymiles_actual_load_power",
        "sensor.hoymiles_hit_overview_pv_total_power",
        "sensor.hoymiles_hit_maximum_charge_current",
        "input_select.hoymiles_tariff_type",
        "input_number.hoymiles_tariff_low_price",
        "input_number.hoymiles_rce_fallback_daily_load",
    ):
        assert entity_id in watched, f"Real tariff input is not watched: {entity_id}"

    class_node = _class_node(tree, "HoymilesTariffOptimizerSensor")
    optimizer_input = _method(class_node, "_optimizer_input")
    optimizer_literals = {
        node.value
        for node in ast.walk(optimizer_input)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "input_number.hoymiles_rce_fallback_daily_load" in optimizer_literals, (
        "Fallback daily LOAD is watched without being consumed by tariff planning"
    )
    for method_name in ("async_added_to_hass", "_current_input_fingerprint"):
        method = _method(class_node, method_name)
        names = {
            node.id for node in ast.walk(method) if isinstance(node, ast.Name)
        }
        literals = {
            node.value
            for node in ast.walk(method)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        expected_set = (
            "TARIFF_EVENT_DRIVEN_ENTITIES"
            if method_name == "async_added_to_hass"
            else "WATCHED_TARIFF_ENTITIES"
        )
        assert expected_set in names, (
            f"{method_name} no longer uses its authoritative planning-input set"
        )
        assert "TARIFF_EXECUTION_FEEDBACK_ENTITIES" not in names
        assert feedback.isdisjoint(literals), (
            f"{method_name} reintroduced an execution-only tariff input"
        )

    feedback_method = _method(class_node, "_update_delivered_power_feedback")
    feedback_literals = {
        node.value
        for node in ast.walk(feedback_method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert feedback <= feedback_literals, (
        "Execution feedback was removed from its authoritative timer sampler"
    )


    timer = _method(class_node, "_async_timer")
    ordered_calls: list[tuple[int, str, bool]] = []
    for statement in timer.body:
        if not isinstance(statement, ast.Expr):
            continue
        awaited = isinstance(statement.value, ast.Await)
        call = statement.value.value if awaited else statement.value
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if not isinstance(call.func.value, ast.Name) or call.func.value.id != "self":
            continue
        ordered_calls.append((statement.lineno, call.func.attr, awaited))
    relevant = [
        item
        for item in sorted(ordered_calls)
        if item[1]
        in {
            "_update_delivered_power_feedback",
            "_invalidate_internal_inputs",
            "_recalculate_and_write",
        }
    ]
    assert relevant == [
        (relevant[0][0], "_update_delivered_power_feedback", False),
        (relevant[1][0], "_invalidate_internal_inputs", False),
        (relevant[2][0], "_recalculate_and_write", True),
    ], "Tariff feedback is no longer sampled before its timer-driven replan"

    assignments = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert ast.unparse(
        assignments["TARIFF_INPUT_RECALCULATION_DELAY_SECONDS"]
    ) == "5 * 60.0"
    added = _method(class_node, "async_added_to_hass")
    five_minute_intervals = [
        node
        for node in ast.walk(added)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "timedelta"
        and any(
            keyword.arg == "minutes"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value == 5
            for keyword in node.keywords
        )
    ]
    assert len(five_minute_intervals) == 1
    for method_name in ("_async_input_changed", "_apply_forecast_gcf_policy"):
        method_names = {
            node.id
            for node in ast.walk(_method(class_node, method_name))
            if isinstance(node, ast.Name)
        }
        assert "TARIFF_INPUT_RECALCULATION_DELAY_SECONDS" in method_names
        assert "INPUT_RECALCULATION_DELAY_SECONDS" not in method_names

    source_text = path.read_text(encoding="utf-8")
    gcf_handler_source = ast.get_source_segment(
        source_text,
        _method(class_node, "_async_forecast_gcf_policy_changed"),
    )
    gcf_deadline_source = ast.get_source_segment(
        source_text,
        _method(class_node, "_async_evaluate_forecast_gcf_policy"),
    )
    assert gcf_handler_source is not None
    assert gcf_deadline_source is not None
    assert "_invalidate_internal_inputs" not in gcf_handler_source, (
        "A partial GCF delivery cohort can still flap tariff authority"
    )
    assert "force_recalculate=cohort_pending" not in gcf_handler_source
    assert "force_recalculate=True" in gcf_deadline_source, (
        "A persistently incoherent GCF cohort no longer fails closed"
    )


def _assert_rce_live_telemetry_is_minute_coalesced() -> None:
    """Fast telemetry and RCE-owned writes must not cancel their own start."""

    path = COMPONENT / "rce_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    watched = _literal_string_set(tree, "WATCHED_ENTITIES")
    coalesced = _literal_string_set(tree, "RCE_MINUTE_COALESCED_ENTITIES")

    assert "sensor.hoymiles_hit_ems_maximum_discharge_power_readback" not in watched, (
        "Execution-only RCE power readback can withdraw planning authority"
    )
    for entity_id in (
        "sensor.hoymiles_hit_overview_battery_soc",
        "sensor.hoymiles_actual_load_power",
        "sensor.hoymiles_hit_overview_pv_total_power",
        "sensor.hoymiles_hit_maximum_discharge_current",
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
    ):
        assert entity_id in watched
        assert entity_id in coalesced, (
            f"Fast RCE input can still cause an event-driven authority flap: {entity_id}"
        )

    class_node = _class_node(tree, "HoymilesRCEOptimizerSensor")
    added = _method(class_node, "async_added_to_hass")
    added_names = {node.id for node in ast.walk(added) if isinstance(node, ast.Name)}
    assert "RCE_EVENT_DRIVEN_ENTITIES" in added_names
    assert "WATCHED_ENTITIES" not in added_names

    fingerprint = _method(class_node, "_current_input_fingerprint")
    fingerprint_names = {
        node.id for node in ast.walk(fingerprint) if isinstance(node, ast.Name)
    }
    assert "WATCHED_ENTITIES" in fingerprint_names, (
        "Minute-coalesced telemetry was removed from in-flight stale-result checks"
    )

    timer = _method(class_node, "_async_timer")
    timer_calls = {
        node.func.attr
        for node in ast.walk(timer)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    }
    assert {"_invalidate_internal_inputs", "_recalculate_and_write"} <= timer_calls


def _assert_tariff_live_telemetry_is_five_minute_coalesced() -> None:
    """Live planning telemetry must remain stable between five-minute runs."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    watched = _literal_string_set(tree, "WATCHED_TARIFF_ENTITIES")
    coalesced = _literal_string_set(
        tree,
        "TARIFF_FIVE_MINUTE_COALESCED_ENTITIES",
    )

    for entity_id in (
        "sensor.hoymiles_hit_rce_optimized_plan",
        "sensor.hoymiles_hit_overview_battery_soc",
        "sensor.hoymiles_hit_battery_voltage_bms",
        "sensor.hoymiles_hit_pv_total_energy_today",
        "sensor.hoymiles_hit_overview_battery_power",
        "sensor.hoymiles_actual_load_power",
        "sensor.hoymiles_hit_overview_pv_total_power",
        "sun.sun",
    ):
        assert entity_id in watched
        assert entity_id in coalesced, (
            f"Fast tariff input can still withdraw authority between runs: {entity_id}"
        )

    class_node = _class_node(tree, "HoymilesTariffOptimizerSensor")
    added = _method(class_node, "async_added_to_hass")
    added_names = {node.id for node in ast.walk(added) if isinstance(node, ast.Name)}
    assert "TARIFF_EVENT_DRIVEN_ENTITIES" in added_names
    assert "WATCHED_TARIFF_ENTITIES" not in added_names

    invalidator = _method(class_node, "_invalidate_input_event")
    invalidator_source = ast.get_source_segment(
        path.read_text(encoding="utf-8"), invalidator
    )
    assert invalidator_source is not None
    assert "include_last_updated=False" in invalidator_source, (
        "Unchanged physical reports can still withdraw tariff authority"
    )

    fingerprint = _method(class_node, "_current_input_fingerprint")
    fingerprint_names = {
        node.id for node in ast.walk(fingerprint) if isinstance(node, ast.Name)
    }
    assert "WATCHED_TARIFF_ENTITIES" in fingerprint_names, (
        "Five-minute telemetry was removed from in-flight stale-result checks"
    )

    timer = _method(class_node, "_async_timer")
    timer_calls = {
        node.func.attr
        for node in ast.walk(timer)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    }
    assert {"_invalidate_internal_inputs", "_recalculate_and_write"} <= timer_calls


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    ]
    assert len(matches) == 1, f"Expected exactly one {name} class"
    return matches[0]


def _method(
    class_node: ast.ClassDef,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    matches = [
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
    ]
    assert len(matches) == 1, f"Expected exactly one {class_node.name}.{name}"
    return matches[0]


def _is_self_attribute(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == name
        and isinstance(node.value, ast.Name)
        and node.value.id == "self"
    )


def _is_hass_executor(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "async_add_executor_job"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "hass"
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "self"
    )


def _awaited_self_call(method: ast.AST, name: str) -> list[ast.Await]:
    return [
        node
        for node in ast.walk(method)
        if isinstance(node, ast.Await)
        and isinstance(node.value, ast.Call)
        and _is_self_attribute(node.value.func, name)
    ]


def _optimizer_executor_await(
    method: ast.AsyncFunctionDef,
    optimizer_name: str,
) -> ast.Await:
    matches: list[ast.Await] = []
    for node in ast.walk(method):
        if not isinstance(node, ast.Await) or not isinstance(node.value, ast.Call):
            continue
        call = node.value
        if not _is_hass_executor(call.func):
            continue
        if (
            len(call.args) >= 2
            and isinstance(call.args[0], ast.Name)
            and call.args[0].id == optimizer_name
        ):
            matches.append(node)
    assert len(matches) == 1, (
        f"{optimizer_name} must be awaited through exactly one "
        "hass.async_add_executor_job call"
    )
    return matches[0]


def _compile_probe_method(method: ast.AsyncFunctionDef) -> type[Any]:
    """Compile an actual wrapper method without importing Home Assistant."""
    probe_class = ast.ClassDef(
        name="Probe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[deepcopy(method)],
    )
    module = ast.fix_missing_locations(ast.Module(body=[probe_class], type_ignores=[]))
    namespace: dict[str, Any] = {"MAX_IMMEDIATE_RECALCULATIONS": 3}
    exec(compile(module, "<optimizer-lock-probe>", "exec"), namespace)
    return namespace["Probe"]


def _compile_executor_probe(executor_await: ast.Await) -> type[Any]:
    """Compile the executor call shape taken directly from a sensor method."""
    call = deepcopy(executor_await.value)
    assert isinstance(call, ast.Call)
    call.args = [
        ast.Name(id="optimizer_callable", ctx=ast.Load()),
        ast.Name(id="optimizer_input", ctx=ast.Load()),
    ]
    method = ast.AsyncFunctionDef(
        name="run_optimizer",
        args=ast.arguments(
            posonlyargs=[],
            args=[
                ast.arg(arg="self"),
                ast.arg(arg="optimizer_callable"),
                ast.arg(arg="optimizer_input"),
            ],
            kwonlyargs=[],
            kw_defaults=[],
            defaults=[],
        ),
        body=[ast.Return(value=ast.Await(value=call))],
        decorator_list=[],
    )
    return _compile_probe_method(method)


def _assert_static_contract(
    path: Path,
    class_name: str,
    optimizer_name: str,
) -> tuple[ast.AsyncFunctionDef, ast.Await]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    class_node = _class_node(tree, class_name)

    init = _method(class_node, "__init__")
    lock_assignments = [
        node
        for node in ast.walk(init)
        if isinstance(node, ast.Assign)
        and any(_is_self_attribute(target, "_optimizer_lock") for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "asyncio"
        and node.value.func.attr == "Lock"
    ]
    assert len(lock_assignments) == 1, f"{path.name} lacks one asyncio.Lock"
    revision_assignments = [
        node
        for node in ast.walk(init)
        if isinstance(node, ast.Assign)
        and any(_is_self_attribute(target, "_input_revision") for target in node.targets)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "OptimizerInputRevision"
    ]
    assert len(revision_assignments) == 1, f"{path.name} lacks one input revision"

    callback_names = [
        "_async_control_timer" if path.name == "rcm_sensor.py" else "_async_timer",
    ]
    if path.name == "rcm_sensor.py":
        callback_names.append("_async_input_changed")
    for callback_name in callback_names:
        assert isinstance(_method(class_node, callback_name), ast.AsyncFunctionDef), (
            f"{path.name}:{callback_name} must await the serialized recalculation"
        )

    recalculate = _method(class_node, "_recalculate")
    recalculate_and_write = _method(class_node, "_recalculate_and_write")
    locked = _method(class_node, "_recalculate_locked")
    assert isinstance(recalculate, ast.AsyncFunctionDef)
    assert isinstance(recalculate_and_write, ast.AsyncFunctionDef)
    assert isinstance(locked, ast.AsyncFunctionDef)

    for wrapper in (recalculate, recalculate_and_write):
        lock_contexts = [
            node
            for node in ast.walk(wrapper)
            if isinstance(node, ast.AsyncWith)
            and any(
                _is_self_attribute(item.context_expr, "_optimizer_lock")
                for item in node.items
            )
        ]
        assert len(lock_contexts) == 1, (
            f"{path.name}:{wrapper.name} must serialize with _optimizer_lock"
        )
        assert len(_awaited_self_call(wrapper, "_recalculate_locked")) == 1

    parents = {
        child: parent
        for parent in ast.walk(class_node)
        for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(class_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and _is_self_attribute(node.func, node.func.attr)
            and node.func.attr
            in {"_recalculate", "_recalculate_and_write", "_recalculate_locked"}
        ):
            assert isinstance(parents.get(node), ast.Await), (
                f"{path.name}:{node.func.attr} coroutine is called without await"
            )

    direct_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == optimizer_name
    ]
    assert not direct_calls, f"{path.name} still calls {optimizer_name} on the HA loop"
    executor_await = _optimizer_executor_await(locked, optimizer_name)
    revision_checks = [
        node
        for node in ast.walk(locked)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "is_current"
        and isinstance(node.func.value, ast.Attribute)
        and _is_self_attribute(node.func.value, "_input_revision")
    ]
    assert revision_checks, f"{path.name} never rejects a stale executor result"
    assert any(
        getattr(node, "lineno", 0) > getattr(executor_await, "lineno", 0)
        for node in revision_checks
    ), f"{path.name} checks revision only before the executor await"
    fingerprint_checks = [
        node
        for node in ast.walk(locked)
        if isinstance(node, ast.Call)
        and _is_self_attribute(node.func, "_current_input_fingerprint")
    ]
    assert len(fingerprint_checks) >= 2, (
        f"{path.name} does not compare HA state before and after the executor"
    )
    return recalculate, executor_await


async def _assert_fifo_single_flight(
    wrapper: ast.AsyncFunctionDef,
    label: str,
) -> None:
    probe_type = _compile_probe_method(wrapper)
    probe = probe_type()
    probe._optimizer_lock = asyncio.Lock()

    first_started = asyncio.Event()
    release_first = asyncio.Event()
    order: list[tuple[str, str]] = []
    active = 0
    maximum_active = 0

    async def fake_locked(self: Any) -> bool:
        nonlocal active, maximum_active
        task = asyncio.current_task()
        assert task is not None
        task_name = task.get_name()
        active += 1
        maximum_active = max(maximum_active, active)
        order.append(("start", task_name))
        if task_name == "first":
            first_started.set()
            await release_first.wait()
        await asyncio.sleep(0)
        order.append(("end", task_name))
        active -= 1
        return True

    probe._recalculate_locked = MethodType(fake_locked, probe)
    probe._mark_result_current = MethodType(lambda self: None, probe)
    probe._publish_timeline_result = MethodType(lambda self: None, probe)
    first = asyncio.create_task(probe._recalculate(), name="first")
    await asyncio.wait_for(first_started.wait(), timeout=5.0)
    second = asyncio.create_task(probe._recalculate(), name="second")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert order == [("start", "first")], f"{label} allowed overlapping runs"
    release_first.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5.0)
    assert maximum_active == 1, f"{label} violated single-flight"
    assert order == [
        ("start", "first"),
        ("end", "first"),
        ("start", "second"),
        ("end", "second"),
    ], f"{label} did not preserve FIFO ordering: {order}"


class _FakeHass:
    async def async_add_executor_job(
        self,
        target: Callable[[object], object],
        argument: object,
    ) -> object:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, target, argument)


async def _assert_loop_remains_responsive(
    executor_await: ast.Await,
    label: str,
) -> None:
    probe_type = _compile_executor_probe(executor_await)
    probe = probe_type()
    probe.hass = _FakeHass()
    worker_started: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    release_worker = threading.Event()
    loop = asyncio.get_running_loop()

    def slow_optimizer(value: object) -> object:
        loop.call_soon_threadsafe(worker_started.set_result, None)
        assert release_worker.wait(timeout=5.0)
        return value

    optimizer_task = asyncio.create_task(
        probe.run_optimizer(slow_optimizer, label),
    )
    await asyncio.wait_for(worker_started, timeout=5.0)
    ticks = 0
    for _ in range(8):
        await asyncio.sleep(0)
        ticks += 1
    assert ticks == 8 and not optimizer_task.done(), (
        f"{label} blocked the event loop while its optimizer was running"
    )
    release_worker.set()
    result = await asyncio.wait_for(optimizer_task, timeout=5.0)
    assert result == label


def _assert_timeline_has_no_execution_authority() -> None:
    """AP-1 projections may observe results but never enter execution paths."""

    timeline_source = (COMPONENT / "timeline_sensor.py").read_text(encoding="utf-8")
    forbidden = (
        "services.async_call",
        "write_register",
        "owner_acquire",
        "grant_execution",
        "handover_execution",
        "scheduler_command",
        "executor_command",
    )
    assert not any(token in timeline_source for token in forbidden)
    for filename in ("rce_sensor.py", "tariff_sensor.py"):
        tree = ast.parse((COMPONENT / filename).read_text(encoding="utf-8"))
        optimizer_name = SENSORS[filename][1]
        assert sum(
            isinstance(node, ast.Name) and node.id == optimizer_name
            for node in ast.walk(tree)
        ) == 1, f"{filename} introduced a second optimizer invocation"
    execution_sources = (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
    ).read_text(encoding="utf-8") + (
        COMPONENT / "supervisor_runtime.py"
    ).read_text(encoding="utf-8")
    assert "automation_plan_timeline" not in execution_sources


async def _async_main() -> None:
    _assert_revision_fingerprint_contract()
    _assert_scheduler_result_current_gates()
    _assert_tariff_feedback_is_not_a_planning_input()
    _assert_rce_live_telemetry_is_minute_coalesced()
    _assert_tariff_live_telemetry_is_five_minute_coalesced()
    _assert_timeline_has_no_execution_authority()
    await _assert_dirty_result_is_never_committed()
    contracts: list[tuple[str, ast.AsyncFunctionDef, ast.Await]] = []
    for filename, (class_name, optimizer_name) in SENSORS.items():
        wrapper, executor_await = _assert_static_contract(
            COMPONENT / filename,
            class_name,
            optimizer_name,
        )
        contracts.append((filename, wrapper, executor_await))

    for filename, wrapper, executor_await in contracts:
        await _assert_fifo_single_flight(wrapper, filename)
        await _assert_loop_remains_responsive(executor_await, filename)


def main() -> None:
    asyncio.run(_async_main())
    print("Optimizer executor: offload, FIFO and single-flight contracts passed")


if __name__ == "__main__":
    main()
