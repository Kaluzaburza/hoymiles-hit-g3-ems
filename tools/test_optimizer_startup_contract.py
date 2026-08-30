"""Contract tests for bounded, non-blocking optimizer startup warmups."""

from __future__ import annotations

import ast
import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from functools import partial
from pathlib import Path
from types import MethodType, SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
SENSORS = {
    "rce_sensor.py": "HoymilesRCEOptimizerSensor",
    "tariff_sensor.py": "HoymilesTariffOptimizerSensor",
    "rcm_sensor.py": "HoymilesRCMOptimizerSensor",
}
DYNAMIC_FORECAST_SENSORS = {
    "rce_sensor.py": (
        "HoymilesRCEOptimizerSensor",
        "WATCHED_ENTITIES",
    ),
    "tariff_sensor.py": (
        "HoymilesTariffOptimizerSensor",
        "WATCHED_TARIFF_ENTITIES",
    ),
}
SLOW_STARTUP_CALLS = {
    "_async_refresh_load_history",
    "_async_refresh_forecast_accuracy",
    "_async_refresh_voltage_history",
    "_async_startup_warmup",
    "_recalculate",
    "_recalculate_and_write",
    "_recalculate_locked",
}


def _class_node(tree: ast.Module, name: str) -> ast.ClassDef:
    matches = [
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == name
    ]
    assert len(matches) == 1
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
    assert len(matches) == 1, f"Expected one {class_node.name}.{name}"
    return matches[0]


def _self_call_name(node: ast.AST) -> str | None:
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "self"
    ):
        return node.func.attr
    return None


def _compile_probe_method(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> type[Any]:
    copied = deepcopy(method)
    copied.decorator_list = []
    base = ast.ClassDef(
        name="Base",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[
            ast.AsyncFunctionDef(
                name="async_added_to_hass",
                args=ast.arguments(
                    posonlyargs=[],
                    args=[ast.arg(arg="self")],
                    kwonlyargs=[],
                    kw_defaults=[],
                    defaults=[],
                ),
                body=[ast.Expr(value=ast.Constant(value=None))],
                decorator_list=[],
            )
        ],
    )
    probe = ast.ClassDef(
        name="Probe",
        bases=[ast.Name(id="Base", ctx=ast.Load())],
        keywords=[],
        decorator_list=[],
        body=[copied],
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[base, probe], type_ignores=[])
    )
    namespace: dict[str, Any] = {
        "CHARGE_POWER_FEEDBACK_MIN_SAMPLES": 3,
        "GRID_VOLTAGE_ENTITIES": (),
        "WATCHED_ENTITIES": (),
        "WATCHED_RCM_ENTITIES": (),
        "WATCHED_TARIFF_ENTITIES": (),
        "FORECAST_GCF_POLICY_TRIGGER_ENTITIES": (),
        "FORECAST_GCF_COHORT_REPORT_ENTITIES": (),
        "RCE_EVENT_DRIVEN_ENTITIES": (),
        "TARIFF_EVENT_DRIVEN_ENTITIES": (),
        "_forecast_learning_policy_snapshot": lambda *args: (object(), {}),
        "_forecast_learning_policy_signature": lambda policy: (True, "adaptive"),
        "dt_util": SimpleNamespace(now=lambda: object()),
        "async_track_state_change_event": lambda *args: lambda: None,
        "async_track_state_report_event": lambda *args: lambda: None,
        "async_track_time_interval": lambda *args: lambda: None,
        "timedelta": timedelta,
    }
    exec(compile(module, "<startup-contract-probe>", "exec"), namespace)
    return namespace["Probe"]


async def _assert_added_is_nonblocking(
    method: ast.AsyncFunctionDef,
    label: str,
) -> None:
    probe_type = _compile_probe_method(method)
    probe = probe_type()
    probe.hass = object()
    probe._async_input_changed = lambda *args: None
    probe._async_timer = lambda *args: None
    probe._async_history_timer = lambda *args: None
    probe._async_control_timer = lambda *args: None
    probe._async_forecast_accuracy_timer = lambda *args: None
    probe._async_forecast_gcf_policy_changed = lambda *args: None
    probe._cancel_delayed_recalculation = lambda: None
    probe._effective_charge_power_factor = 1.0
    probe._effective_charge_power_source = "configured"
    probe._delivered_power_ratios = SimpleNamespace(maxlen=24, append=lambda _v: None)
    probe._charge_power_feedback_last_ratio = None
    probe._charge_power_feedback_last_sample_at = None
    probe.async_get_last_state = MethodType(
        lambda self: _return_none(),
        probe,
    )
    probe.removers = []
    probe.async_on_remove = probe.removers.append
    probe.state_written = False
    probe.async_write_ha_state = lambda: setattr(probe, "state_written", True)
    probe.warmup_scheduled = False
    probe._schedule_startup_warmup = lambda: setattr(
        probe,
        "warmup_scheduled",
        True,
    )

    never = asyncio.Event()

    async def blocked(self: Any, *args: Any, **kwargs: Any) -> None:
        await never.wait()

    for name in SLOW_STARTUP_CALLS:
        setattr(probe, name, MethodType(blocked, probe))

    await asyncio.wait_for(probe.async_added_to_hass(), timeout=0.1)
    assert probe.state_written, f"{label} did not publish its fail-closed state"
    assert probe.warmup_scheduled, f"{label} did not schedule its warmup"


async def _return_none() -> None:
    return None


async def _assert_warmup_single_flight(
    scheduler: ast.FunctionDef,
    label: str,
) -> None:
    probe_type = _compile_probe_method(scheduler)
    probe = probe_type()
    release = asyncio.Event()

    async def warmup(self: Any) -> None:
        await release.wait()

    created: list[asyncio.Task[None]] = []

    class FakeEntry:
        def async_create_background_task(
            self,
            hass: Any,
            coroutine: Any,
            name: str,
        ) -> asyncio.Task[None]:
            assert hass is probe.hass
            task = asyncio.create_task(coroutine, name=name)
            created.append(task)
            return task

    probe.hass = object()
    probe._entry = FakeEntry()
    probe._startup_warmup_task = None
    probe._async_startup_warmup = MethodType(warmup, probe)
    probe.removers = []
    probe.async_on_remove = probe.removers.append

    probe._schedule_startup_warmup()
    probe._schedule_startup_warmup()
    await asyncio.sleep(0)
    assert len(created) == 1, f"{label} scheduled overlapping warmups"
    assert len(probe.removers) == 1, f"{label} did not register task cancellation"
    probe.removers[0]()
    try:
        await created[0]
    except asyncio.CancelledError:
        pass
    else:
        raise AssertionError(f"{label} warmup was not cancelled on removal")


def _assert_dynamic_forecast_listener_runtime(
    path: Path,
    class_name: str,
    watched_name: str,
) -> None:
    """Exercise custom-source rebinding, self-denial and unload cleanup."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = _class_node(tree, class_name)
    method_names = (
        "_configured_forecast_source_ids",
        "_refresh_dynamic_forecast_listener",
        "_remove_dynamic_forecast_listener",
        "_current_input_fingerprint",
    )
    methods = [deepcopy(_method(class_node, name)) for name in method_names]
    for method in methods:
        method.decorator_list = []

    registrations: list[tuple[str, ...]] = []
    unsubscriptions: list[tuple[str, ...]] = []

    def track_dynamic(_hass: Any, entity_ids: Any, _handler: Any) -> Any:
        registration = tuple(entity_ids)
        registrations.append(registration)

        def unsubscribe() -> None:
            unsubscriptions.append(registration)

        return unsubscribe

    watched = frozenset({"sensor.static_forecast"})
    namespace: dict[str, Any] = {
        "Any": Any,
        "RCE_LOAD_BROKER_ATTRIBUTES": (),
        "TARIFF_PRICE_BROKER_ATTRIBUTES": (),
        "WATCHED_ENTITIES": frozenset(),
        "WATCHED_TARIFF_ENTITIES": frozenset(),
        "_configured_forecast_entity_ids": (
            lambda hass: frozenset(hass["configured"])
        ),
        "async_track_state_change_event": track_dynamic,
        "optimizer_input_fingerprint": (
            lambda _hass, entity_ids, attribute_projections=None: tuple(
                sorted(entity_ids)
            )
        ),
    }
    assert watched_name in namespace
    namespace[watched_name] = watched
    probe_class = ast.ClassDef(
        name="DynamicForecastProbe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=methods,
    )
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(body=[probe_class], type_ignores=[])
            ),
            f"<{path.name}-dynamic-forecast>",
            "exec",
        ),
        namespace,
    )
    probe = namespace["DynamicForecastProbe"]()
    probe.entity_id = "sensor.own_optimizer_plan"
    probe.hass = {
        "configured": {
            "sensor.custom_forecast_a",
            "sensor.static_forecast",
        }
    }
    probe._async_input_changed = object()
    probe._dynamic_forecast_entities = frozenset()
    probe._dynamic_forecast_unsub = None

    probe._refresh_dynamic_forecast_listener()
    assert registrations == [("sensor.custom_forecast_a",)]
    assert probe._current_input_fingerprint() == (
        "sensor.custom_forecast_a",
        "sensor.static_forecast",
    )

    # Re-reading an unchanged helper must be a strict no-op.
    probe._refresh_dynamic_forecast_listener()
    assert registrations == [("sensor.custom_forecast_a",)]
    assert unsubscriptions == []

    probe.hass["configured"] = {"sensor.custom_forecast_b"}
    probe._refresh_dynamic_forecast_listener()
    assert unsubscriptions == [("sensor.custom_forecast_a",)]
    assert registrations[-1] == ("sensor.custom_forecast_b",)

    # A custom external sensor remains tracked without inspecting its current
    # availability. The optimizer's own output is denied from both listener
    # targets and its publication fingerprint, preventing a self-replan loop.
    probe.hass["configured"] = {
        "sensor.external_forecast_unavailable",
        probe.entity_id,
    }
    probe._refresh_dynamic_forecast_listener()
    assert unsubscriptions[-1] == ("sensor.custom_forecast_b",)
    assert registrations[-1] == ("sensor.external_forecast_unavailable",)
    fingerprint = probe._current_input_fingerprint()
    assert "sensor.external_forecast_unavailable" in fingerprint
    assert probe.entity_id not in fingerprint

    probe._remove_dynamic_forecast_listener()
    assert unsubscriptions[-1] == ("sensor.external_forecast_unavailable",)
    assert probe._dynamic_forecast_entities == frozenset()
    assert probe._dynamic_forecast_unsub is None
    cleanup_count = len(unsubscriptions)
    probe._remove_dynamic_forecast_listener()
    assert len(unsubscriptions) == cleanup_count


def _assert_static_sensor_contract(
    path: Path,
    class_name: str,
) -> tuple[ast.AsyncFunctionDef, ast.FunctionDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = _class_node(tree, class_name)
    added = _method(class_node, "async_added_to_hass")
    scheduler = _method(class_node, "_schedule_startup_warmup")
    warmup = _method(class_node, "_async_startup_warmup")
    assert isinstance(added, ast.AsyncFunctionDef)
    assert isinstance(scheduler, ast.FunctionDef)
    assert isinstance(warmup, ast.AsyncFunctionDef)

    tracked_tasks = [
        node
        for node in ast.walk(class_node)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_create_task"
        and isinstance(node.func.value, ast.Attribute)
        and isinstance(node.func.value.value, ast.Name)
        and node.func.value.value.id == "self"
        and node.func.value.attr == "hass"
    ]
    assert not tracked_tasks, (
        f"{path.name} schedules optimizer work as startup-blocking HA tasks"
    )

    awaited_slow_calls = {
        name
        for node in ast.walk(added)
        if isinstance(node, ast.Await)
        and (name := _self_call_name(node.value)) in SLOW_STARTUP_CALLS
    }
    assert not awaited_slow_calls, (
        f"{path.name} blocks entity registration on {awaited_slow_calls}"
    )
    scheduled = [
        node
        for node in ast.walk(added)
        if _self_call_name(node) == "_schedule_startup_warmup"
    ]
    assert len(scheduled) == 1

    created = [
        node
        for node in ast.walk(scheduler)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_create_background_task"
    ]
    assert len(created) == 1
    cancelled = [
        node
        for node in ast.walk(scheduler)
        if isinstance(node, ast.Attribute) and node.attr == "cancel"
    ]
    assert len(cancelled) == 1

    cancellation_handlers = [
        node
        for node in ast.walk(warmup)
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Attribute)
        and isinstance(node.type.value, ast.Name)
        and node.type.value.id == "asyncio"
        and node.type.attr == "CancelledError"
        and any(isinstance(item, ast.Raise) for item in node.body)
    ]
    assert len(cancellation_handlers) == 1
    return added, scheduler


def _assert_bounded_recorder_contract() -> None:
    path = COMPONENT / "bounded_history.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    query = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_query_state_reports"
    )
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_get_bounded_state_reports"
    )
    calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
    assert any(
        isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
        and node.func.attr == "wait_for"
        for node in calls
    )
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr in {"last_changed", "last_changed_ts"}
        for node in ast.walk(query)
    ), "Bounded query filters repeated Recorder reports"
    limits = [
        node.args[0]
        for node in ast.walk(query)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "limit"
        and node.args
    ]
    assert any(
        isinstance(value, ast.BinOp)
        and isinstance(value.op, ast.Add)
        and isinstance(value.left, ast.Name)
        and value.left.id == "limit"
        and isinstance(value.right, ast.Constant)
        and value.right.value == 1
        for value in limits
    )
    assert any(isinstance(value, ast.Constant) and value.value == 1 for value in limits)


def _assert_tariff_recorder_attribute_contract() -> None:
    """Keep the live plan rich while excluding it from Recorder's 16 KiB row."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    match_all_imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and node.module == "homeassistant.const"
        and any(alias.name == "MATCH_ALL" for alias in node.names)
    ]
    assert len(match_all_imports) == 1
    class_node = _class_node(tree, "HoymilesTariffOptimizerSensor")
    unrecorded = [
        node.value
        for node in class_node.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name)
            and target.id == "_unrecorded_attributes"
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        )
    ]
    assert len(unrecorded) == 1
    value = unrecorded[0]
    assert (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
        and len(value.args) == 1
        and isinstance(value.args[0], ast.Set)
        and len(value.args[0].elts) == 1
        and isinstance(value.args[0].elts[0], ast.Name)
        and value.args[0].elts[0].id == "MATCH_ALL"
    ), "Tariff plan custom attributes are no longer fully excluded from Recorder"

    bases = {
        node.id for node in class_node.bases if isinstance(node, ast.Name)
    }
    assert "RestoreEntity" in bases
    extra = _method(class_node, "extra_state_attributes")
    returns = [node for node in ast.walk(extra) if isinstance(node, ast.Return)]
    assert len(returns) == 1
    returned = returns[0].value
    assert (
        isinstance(returned, ast.Attribute)
        and isinstance(returned.value, ast.Name)
        and returned.value.id == "self"
        and returned.attr == "_attributes"
    ), "Recorder exclusion truncated the live tariff plan"

    restored = _method(class_node, "async_added_to_hass")
    restored_literals = {
        node.value
        for node in ast.walk(restored)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {
        "charge_power_feedback_version",
        "effective_charge_power_factor",
        "effective_charge_power_feedback_samples",
    } <= restored_literals, "Recorder fix removed existing RestoreEntity feedback"

    synthetic_plan = {
        "status_code": "ready",
        "planned_slots": [
            {
                "start": f"2026-08-{1 + index // 48:02d}T{index % 24:02d}:00:00+02:00",
                "end": f"2026-08-{1 + index // 48:02d}T{index % 24:02d}:30:00+02:00",
                "action": "grid_support_and_charge",
                "energy_kwh": 3.141,
                "power_kw": 12.5,
                "price_pln_kwh": 0.4321,
                "target_soc": 98.0,
            }
            for index in range(96)
        ],
        "expensive_window_load_buffers": [
            {
                "start": f"2026-08-{1 + index // 24:02d}T{index % 24:02d}:00:00+02:00",
                "required_kwh": 14.25,
                "uncertainty_kwh": 2.75,
            }
            for index in range(48)
        ],
    }
    full_size = len(
        json.dumps(synthetic_plan, separators=(",", ":")).encode("utf-8")
    )
    assert full_size > 16_384, "Recorder regression fixture is not oversized"
    recorder_custom_attributes: dict[str, Any] = {}
    recorder_size = len(
        json.dumps(
            recorder_custom_attributes,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    assert recorder_size < 4_096


def _compile_tariff_delayed_recalculation_probe() -> type[Any]:
    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = _class_node(tree, "HoymilesTariffOptimizerSensor")
    methods = [
        deepcopy(_method(class_node, name))
        for name in (
            "_async_debounced_recalculate",
            "_cancel_delayed_recalculation",
        )
    ]
    for method in methods:
        method.decorator_list = []
    probe_class = ast.ClassDef(
        name="TariffDelayedRecalculationProbe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=methods,
    )
    namespace: dict[str, Any] = {"asyncio": asyncio}
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(body=[probe_class], type_ignores=[])
            ),
            "<tariff-delayed-recalculation-probe>",
            "exec",
        ),
        namespace,
    )
    return namespace["TariffDelayedRecalculationProbe"]


async def _assert_tariff_delayed_recalculation_lifecycle() -> None:
    """Cancel queued and running tariff recalculations during entity removal."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = _class_node(tree, "HoymilesTariffOptimizerSensor")
    added = _method(class_node, "async_added_to_hass")
    cleanup_registrations = [
        node
        for node in ast.walk(added)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "async_on_remove"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Attribute)
        and isinstance(node.args[0].value, ast.Name)
        and node.args[0].value.id == "self"
        and node.args[0].attr == "_cancel_delayed_recalculation"
    ]
    assert len(cleanup_registrations) == 1

    probe_type = _compile_tariff_delayed_recalculation_probe()
    probe = probe_type()
    handle_cancellations = 0

    def cancel_handle() -> None:
        nonlocal handle_cancellations
        handle_cancellations += 1

    probe._lifecycle_stopped = False
    probe._recalculate_cancel = cancel_handle
    probe._delayed_recalculate_tasks = set()
    probe._cancel_delayed_recalculation()
    probe._cancel_delayed_recalculation()
    assert handle_cancellations == 1
    assert probe._lifecycle_stopped
    assert probe._recalculate_cancel is None
    assert probe._delayed_recalculate_tasks == set()

    recalculations = 0

    async def count_recalculation() -> None:
        nonlocal recalculations
        recalculations += 1

    probe._recalculate_and_write = count_recalculation
    await probe._async_debounced_recalculate(None)
    assert recalculations == 0, "Queued callback published after entity removal"

    probe._lifecycle_stopped = False
    await probe._async_debounced_recalculate(None)
    assert recalculations == 1
    assert probe._delayed_recalculate_tasks == set()

    entered = asyncio.Event()
    entered_count = 0
    published = 0

    async def blocked_recalculation() -> None:
        nonlocal entered_count, published
        entered_count += 1
        if entered_count == 2:
            entered.set()
        await asyncio.Event().wait()
        published += 1

    probe._lifecycle_stopped = False
    probe._recalculate_and_write = blocked_recalculation
    running = [
        asyncio.create_task(probe._async_debounced_recalculate(None))
        for _ in range(2)
    ]
    await asyncio.wait_for(entered.wait(), timeout=0.1)
    assert probe._delayed_recalculate_tasks == set(running)
    probe._cancel_delayed_recalculation()
    results = await asyncio.wait_for(
        asyncio.gather(*running, return_exceptions=True),
        timeout=1.0,
    )
    assert all(isinstance(result, asyncio.CancelledError) for result in results), (
        "An overlapping delayed recalculation survived removal"
    )
    assert published == 0
    assert probe._delayed_recalculate_tasks == set()


def _compile_bounded_history_probe() -> dict[str, Any]:
    path = COMPONENT / "bounded_history.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    selected = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.ImportFrom)
            and node.module == "__future__"
        )
        or (
            isinstance(node, ast.ClassDef)
            and node.name
            in {"RecorderHistoryLimitExceeded", "RecorderHistoryQueryTimeout"}
        )
        or (
            isinstance(node, ast.AsyncFunctionDef)
            and node.name == "async_get_bounded_state_reports"
        )
    ]
    module = ast.fix_missing_locations(ast.Module(body=selected, type_ignores=[]))
    namespace: dict[str, Any] = {
        "RECORDER_QUERY_TIMEOUT_SECONDS": 15.0,
        "RECORDER_STATES_PER_ENTITY_LIMIT": 50_000,
        "asyncio": asyncio,
        "partial": partial,
    }
    exec(compile(module, "<bounded-history-probe>", "exec"), namespace)
    return namespace


async def _assert_bounded_recorder_runtime() -> None:
    namespace = _compile_bounded_history_probe()
    calls: list[tuple[str, int]] = []
    rows_by_entity = {"sensor.a": 2, "sensor.b": 1}
    repeated = object()

    def fake_query(
        hass: Any,
        start: Any,
        end: Any,
        entity_id: str,
        limit: int,
    ) -> tuple[list[object], bool]:
        calls.append((entity_id, limit))
        count = rows_by_entity[entity_id]
        return [repeated] * count, count > limit

    class FakeRecorder:
        async def async_add_executor_job(self, query: Any) -> Any:
            await asyncio.sleep(0)
            return query()

    namespace["_query_state_reports"] = fake_query
    namespace["get_recorder_instance"] = lambda hass: FakeRecorder()
    bounded = namespace["async_get_bounded_state_reports"]
    result = await bounded(
        object(),
        1,
        2,
        ("sensor.a", "sensor.b"),
        limit_per_entity=3,
        timeout_seconds=0.1,
    )
    assert [len(result[key]) for key in ("sensor.a", "sensor.b")] == [2, 1]
    assert result["sensor.a"][0] is result["sensor.a"][1] is repeated
    assert calls == [("sensor.a", 3), ("sensor.b", 3)]

    rows_by_entity["sensor.a"] = 4
    try:
        await bounded(
            object(),
            1,
            2,
            ("sensor.a",),
            limit_per_entity=3,
            timeout_seconds=0.1,
        )
    except namespace["RecorderHistoryLimitExceeded"]:
        pass
    else:
        raise AssertionError("Recorder row budget accepted an incomplete result")

    class SlowRecorder:
        async def async_add_executor_job(self, query: Any) -> Any:
            await asyncio.Event().wait()

    namespace["get_recorder_instance"] = lambda hass: SlowRecorder()
    try:
        await bounded(
            object(),
            1,
            2,
            ("sensor.a",),
            limit_per_entity=3,
            timeout_seconds=0.01,
        )
    except namespace["RecorderHistoryQueryTimeout"]:
        pass
    else:
        raise AssertionError("Recorder query timeout did not fail closed")


def _assert_timeline_entity_lifecycle_contract() -> None:
    """AP-1 timeline entities add no startup work or removable listeners."""

    timeline_path = COMPONENT / "timeline_sensor.py"
    timeline_source = timeline_path.read_text(encoding="utf-8")
    timeline_tree = ast.parse(timeline_source)
    timeline_class = next(
        node
        for node in timeline_tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "HoymilesAutomationPlanTimelineSensor"
    )
    method_names = {
        node.name
        for node in timeline_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "async_added_to_hass" not in method_names
    assert "async_will_remove_from_hass" not in method_names
    assert not any(
        token in timeline_source
        for token in (
            "async_track_state_change_event",
            "async_track_time_interval",
            "async_call_later",
            "async_create_background_task",
        )
    )

    platform_source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    assert platform_source.index("entities.append(rce_plan)") < platform_source.index(
        'policy_id="rce"'
    )
    assert platform_source.index("entities.append(tariff_plan)") < platform_source.index(
        'policy_id="tariff"'
    )
    assert platform_source.count("HoymilesAutomationPlanTimelineSensor(") == 2


async def _async_main() -> None:
    contracts = []
    for filename, class_name in SENSORS.items():
        added, scheduler = _assert_static_sensor_contract(
            COMPONENT / filename,
            class_name,
        )
        contracts.append((filename, added, scheduler))
    _assert_bounded_recorder_contract()
    _assert_tariff_recorder_attribute_contract()
    _assert_timeline_entity_lifecycle_contract()
    for filename, (class_name, watched_name) in DYNAMIC_FORECAST_SENSORS.items():
        _assert_dynamic_forecast_listener_runtime(
            COMPONENT / filename,
            class_name,
            watched_name,
        )

    for filename, added, scheduler in contracts:
        await _assert_added_is_nonblocking(added, filename)
        await _assert_warmup_single_flight(scheduler, filename)
    await _assert_tariff_delayed_recalculation_lifecycle()
    await _assert_bounded_recorder_runtime()


def main() -> None:
    asyncio.run(_async_main())
    print("Optimizer startup: non-blocking, bounded and cancel-safe contracts passed")


if __name__ == "__main__":
    main()
