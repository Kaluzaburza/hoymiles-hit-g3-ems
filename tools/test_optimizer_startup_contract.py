"""Contract tests for bounded, non-blocking optimizer startup warmups."""

from __future__ import annotations

import ast
import asyncio
from collections import deque
import json
from copy import deepcopy
from datetime import datetime, timedelta
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
        "CHARGE_POWER_FEEDBACK_VERSION": 2,
        "GRID_VOLTAGE_ENTITIES": (),
        "WATCHED_ENTITIES": (),
        "WATCHED_RCM_ENTITIES": (),
        "WATCHED_TARIFF_ENTITIES": (),
        "FORECAST_GCF_POLICY_TRIGGER_ENTITIES": (),
        "FORECAST_GCF_COHORT_REPORT_ENTITIES": (),
        "RCE_EVENT_DRIVEN_ENTITIES": (),
        "RCE_FULL_OPTIMIZER_INTERVAL": timedelta(seconds=120),
        "RCM_FULL_PLAN_INTERVAL": timedelta(minutes=10),
        "RCM_HISTORY_REFRESH_INTERVAL": timedelta(hours=1),
        "RCM_FORECAST_VALUE_ENTITIES": (),
        "TARIFF_EVENT_DRIVEN_ENTITIES": (),
        "TARIFF_REQUIRED_INPUT_RECOVERY_ENTITIES": (),
        "TARIFF_FULL_OPTIMIZER_INTERVAL": timedelta(seconds=120),
        "MAX_IMMEDIATE_RECALCULATIONS": 3,
        "_forecast_learning_policy_snapshot": lambda *args: (object(), {}),
        "_forecast_learning_policy_signature": lambda policy: (True, "adaptive"),
        "dt_util": SimpleNamespace(
            now=lambda: object(),
            parse_datetime=datetime.fromisoformat,
        ),
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
    probe._runtime = SimpleNamespace(shared_inputs=None)
    probe._tariff_price_source = None
    probe._watched_rcm_entities = lambda: ()
    probe._event_driven_rcm_entities = lambda: ()
    probe._async_input_changed = lambda *args: None
    probe._async_forecast_value_changed = lambda *args: None
    probe._async_timer = lambda *args: None
    probe._async_history_timer = lambda *args: None
    probe._async_control_timer = lambda *args: None
    probe._async_full_plan_timer = lambda *args: None
    probe._async_forecast_accuracy_timer = lambda *args: None
    probe._async_forecast_gcf_policy_changed = lambda *args: None
    probe._async_required_input_recovered = lambda *args: None
    probe._cancel_delayed_recalculation = lambda: None
    probe._cancel_slot_boundary = lambda: None
    probe._schedule_slot_boundary = lambda: None
    probe._cancel_full_plan_recalculation = lambda: None
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


async def _assert_tariff_feedback_restore_version() -> None:
    """Reject contaminated v1 feedback and restore only physical-source v2."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    added = _method(
        _class_node(tree, "HoymilesTariffOptimizerSensor"),
        "async_added_to_hass",
    )
    assert isinstance(added, ast.AsyncFunctionDef)
    probe_type = _compile_probe_method(added)

    async def run(version: int) -> Any:
        probe = probe_type()
        probe.hass = object()
        probe._runtime = SimpleNamespace(shared_inputs=None)
        probe._async_input_changed = lambda *args: None
        probe._async_timer = lambda *args: None
        probe._async_forecast_accuracy_timer = lambda *args: None
        probe._async_forecast_gcf_policy_changed = lambda *args: None
        probe._async_required_input_recovered = lambda *args: None
        probe._cancel_delayed_recalculation = lambda: None
        probe._cancel_slot_boundary = lambda: None
        probe._schedule_slot_boundary = lambda: None
        probe._effective_charge_power_factor = 1.0
        probe._effective_charge_power_source = "configured"
        probe._delivered_power_ratios = deque(maxlen=24)
        probe._charge_power_feedback_last_ratio = None
        probe._charge_power_feedback_last_sample_at = None

        async def restored() -> Any:
            return SimpleNamespace(
                attributes={
                    "charge_power_feedback_version": version,
                    "effective_charge_power_factor": 0.72,
                    "effective_charge_power_feedback_samples": 5,
                    "charge_power_feedback_last_ratio": 0.71,
                    "charge_power_feedback_last_sample_at": (
                        "2026-08-31T06:00:00+00:00"
                    ),
                }
            )

        probe.async_get_last_state = restored
        probe.removers = []
        probe.async_on_remove = probe.removers.append
        probe.async_write_ha_state = lambda: None
        probe._schedule_startup_warmup = lambda: None
        await probe.async_added_to_hass()
        return probe

    legacy = await run(1)
    assert legacy._effective_charge_power_factor == 1.0
    assert legacy._effective_charge_power_source == "configured"
    assert not legacy._delivered_power_ratios
    assert legacy._charge_power_feedback_last_ratio is None

    physical = await run(2)
    assert physical._effective_charge_power_factor == 0.72
    assert physical._effective_charge_power_source == "restored_grid_charge_feedback"
    assert list(physical._delivered_power_ratios) == [0.72] * 5
    assert physical._charge_power_feedback_last_ratio == 0.71
    assert physical._charge_power_feedback_last_sample_at == datetime.fromisoformat(
        "2026-08-31T06:00:00+00:00"
    )


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
        "HomeAssistant": Any,
        "datetime": datetime,
        "dt_util": SimpleNamespace(now=lambda: datetime(2026, 9, 26, 12, 0)),
        "CONF_SOURCE_DEVICE_ID": "source_device_id",
        "CONF_RESOLVED_SOURCE_DEVICE_ID": "resolved_source_device_id",
        "LOAD_PHASE_ENERGY_ENTITIES": (),
        "RCE_LOAD_BROKER_ATTRIBUTES": (),
        "TARIFF_PRICE_BROKER_ATTRIBUTES": (),
        "RCE_GCF_OPTIMIZER_ENTITIES": frozenset(),
        "WATCHED_ENTITIES": frozenset(),
        "RCE_EVENT_DRIVEN_ENTITIES": frozenset(),
        "WATCHED_TARIFF_ENTITIES": frozenset(),
        "TARIFF_EVENT_DRIVEN_ENTITIES": frozenset(),
        "_configured_forecast_entity_ids": (
            lambda hass: frozenset(hass["configured"])
        ),
        "_shared_inputs_snapshot": lambda _runtime: None,
        "_shared_optimizer_signature": lambda _runtime: None,
        "_tariff_shared_critical_signature": lambda _runtime: None,
        "_live_forecast_gcf_optimizer_signature": (
            lambda _hass, _runtime: None
        ),
        "async_track_state_change_event": track_dynamic,
        "optimizer_input_fingerprint": (
            lambda _hass, entity_ids, attribute_projections=None: tuple(
                (entity_id, None) for entity_id in sorted(entity_ids)
            )
        ),
        "_tariff_immediate_input_fingerprint": (
            lambda _hass, entity_ids: tuple(sorted(entity_ids))
        ),
    }
    assert watched_name in namespace
    namespace[watched_name] = watched
    if watched_name == "WATCHED_TARIFF_ENTITIES":
        namespace["TARIFF_EVENT_DRIVEN_ENTITIES"] = watched
    helper_nodes = []
    if watched_name == "WATCHED_ENTITIES":
        # The RCE method now decorates physical fingerprints with sample
        # freshness. Load its real helper; these custom forecast fixtures have
        # missing values, so numeric sampling itself is covered elsewhere.
        helper_nodes = [deepcopy(node) for node in tree.body if (
            isinstance(node, ast.FunctionDef) and node.name == "_rce_report_fingerprint"
        ) or (
            isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "_RCE_REVALIDATED_NUMERIC_INPUTS"
                for target in node.targets
            )
        )]
        assert len(helper_nodes) == 2
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
                ast.Module(body=[*helper_nodes, probe_class], type_ignores=[])
            ),
            f"<{path.name}-dynamic-forecast>",
            "exec",
        ),
        namespace,
    )
    probe = namespace["DynamicForecastProbe"]()
    probe._ev_filter = lambda: SimpleNamespace(configure=lambda: (SimpleNamespace(enabled=False),))
    probe.entity_id = "sensor.own_optimizer_plan"
    probe._runtime = SimpleNamespace(shared_inputs=None, source_device=SimpleNamespace(id="device-a"))
    probe._entry = SimpleNamespace(entry_id="entry-a", data={
        "source_device_id": "configured-a", "resolved_source_device_id": "resolved-a",
    })
    probe._forecast_gcf_policy_signature = None
    probe._rce_plan_source = None
    probe._tariff_price_source = None
    probe.hass = {
        "configured": {
            "sensor.custom_forecast_a",
            "sensor.static_forecast",
        }
    }
    probe._async_forecast_value_changed = object()
    probe._dynamic_forecast_entities = frozenset()
    probe._dynamic_forecast_unsub = None

    probe._refresh_dynamic_forecast_listener()
    assert registrations == [("sensor.custom_forecast_a",)]
    expected_fingerprint: tuple[Any, ...] = (
        (("sensor.static_forecast", None),)
        if watched_name == "WATCHED_TARIFF_ENTITIES"
        else (("sensor.custom_forecast_a", None), ("sensor.static_forecast", None))
    )
    if watched_name == "WATCHED_TARIFF_ENTITIES":
        expected_fingerprint += (
            ("__shared_ems_inputs_critical__", None),
            ("__forecast_gcf_optimizer__", None),
            ("__charge_power_feedback__", (None, None)),
            # TARYFA-CIAGLOSC-03 certifies the active transaction as an input.
            # This startup probe has no active lease/commitment.
            ("__active_tariff_commitment__", None),
        )
    else:
        expected_fingerprint += (
            ("__rce_execution_gcf__", ()),
            ("__rce_gcf_optimizer__", None),
            ("__r07_tariff_price__", None),
            ("__rce_publication_source__", ("entry-a", "configured-a", "resolved-a", "device-a")),
        )
    assert probe._current_input_fingerprint() == expected_fingerprint, (
        path.name, probe._current_input_fingerprint(), expected_fingerprint,
    )
    if watched_name == "WATCHED_ENTITIES":
        probe._entry.data["resolved_source_device_id"] = "resolved-b"
        changed = probe._current_input_fingerprint()
        assert changed != expected_fingerprint
        assert dict(changed)["__rce_publication_source__"] == (
            "entry-a", "configured-a", "resolved-b", "device-a",
        )
        probe._entry.data["resolved_source_device_id"] = "resolved-a"

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
    if watched_name == "WATCHED_TARIFF_ENTITIES":
        assert ("sensor.external_forecast_unavailable", None) not in fingerprint
    else:
        assert ("sensor.external_forecast_unavailable", None) in fingerprint
    assert probe.entity_id not in {
        item[0] if isinstance(item, tuple) else item for item in fingerprint
    }

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
    sampled_membership = [
        node
        for node in ast.walk(query)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "in_"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "last_updated_ts"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id == "latest"
    ]
    assert len(sampled_membership) == 1, (
        "Sampled Recorder history must use indexed timestamp membership; "
        "joining the bucket result causes a quadratic SQLite plan"
    )


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
            "_cancel_stale_result_retry",
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
    stale_retry_cancellations = 0

    def cancel_handle() -> None:
        nonlocal handle_cancellations
        handle_cancellations += 1

    def cancel_stale_retry() -> None:
        nonlocal stale_retry_cancellations
        stale_retry_cancellations += 1

    probe._lifecycle_stopped = False
    probe._required_input_recovery_pending = True
    probe._recalculate_cancel = cancel_handle
    probe._stale_result_retry_cancel = cancel_stale_retry
    probe._shared_inputs_dirty = False
    probe._delayed_recalculate_tasks = set()
    probe._cancel_delayed_recalculation()
    probe._cancel_delayed_recalculation()
    assert handle_cancellations == 1
    assert stale_retry_cancellations == 1
    assert probe._lifecycle_stopped
    assert not probe._required_input_recovery_pending
    assert probe._recalculate_cancel is None
    assert probe._stale_result_retry_cancel is None
    assert probe._delayed_recalculate_tasks == set()

    recalculations = 0

    async def count_recalculation() -> None:
        nonlocal recalculations
        recalculations += 1

    probe._recalculate_and_write = count_recalculation
    await probe._async_debounced_recalculate(None)
    assert recalculations == 0, "Queued callback published after entity removal"

    probe._lifecycle_stopped = False
    probe._required_input_recovery_pending = True
    await probe._async_debounced_recalculate(None)
    assert recalculations == 1
    assert not probe._required_input_recovery_pending
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
    probe._required_input_recovery_pending = True
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
    assert not probe._required_input_recovery_pending


async def _assert_tariff_required_input_recovery_startup_contract() -> None:
    """Recover a cold-start missing projection without telemetry churn."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = _class_node(tree, "HoymilesTariffOptimizerSensor")
    expected_entities = {
        "sensor.hoymiles_hit_battery_capacity",
        "sensor.hoymiles_hit_overview_battery_soc",
        "sensor.hoymiles_hit_ems_self_use_soc_readback",
        "sensor.hoymiles_hit_number_of_machines_master_and_slave",
    }
    assignments = [
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "TARIFF_REQUIRED_INPUT_RECOVERY_ENTITIES"
            for target in node.targets
        )
    ]
    assert len(assignments) == 1
    value = assignments[0].value
    assert (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "frozenset"
        and len(value.args) == 1
    )
    assert set(ast.literal_eval(value.args[0])) == expected_entities

    added = _method(class_node, "async_added_to_hass")
    recovery_subscriptions = [
        call
        for call in ast.walk(added)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "async_track_state_change_event"
        and len(call.args) >= 3
        and isinstance(call.args[1], ast.Call)
        and isinstance(call.args[1].func, ast.Name)
        and call.args[1].func.id == "sorted"
        and call.args[1].args
        and isinstance(call.args[1].args[0], ast.Name)
        and call.args[1].args[0].id
        == "TARIFF_REQUIRED_INPUT_RECOVERY_ENTITIES"
        and isinstance(call.args[2], ast.Attribute)
        and isinstance(call.args[2].value, ast.Name)
        and call.args[2].value.id == "self"
        and call.args[2].attr == "_async_required_input_recovered"
    ]
    assert len(recovery_subscriptions) == 1, (
        "tariff recovery must use one dedicated state-change subscription"
    )
    ordinary_subscriptions = [
        call
        for call in ast.walk(added)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "async_track_state_change_event"
        and len(call.args) >= 3
        and isinstance(call.args[1], ast.Call)
        and isinstance(call.args[1].func, ast.Name)
        and call.args[1].func.id == "sorted"
        and call.args[1].args
        and isinstance(call.args[1].args[0], ast.Name)
        and call.args[1].args[0].id == "TARIFF_EVENT_DRIVEN_ENTITIES"
        and isinstance(call.args[2], ast.Attribute)
        and call.args[2].attr == "_async_input_changed"
    ]
    assert len(ordinary_subscriptions) == 1, (
        "tariff recovery listener replaced the ordinary input listener"
    )

    shared_changed = _method(class_node, "_async_shared_inputs_changed")
    shared_recovery_calls = [
        call
        for call in ast.walk(shared_changed)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "self"
        and call.func.attr == "_schedule_available_required_input_recovery"
    ]
    assert len(shared_recovery_calls) == 1, (
        "state-reported freshness from shared inputs must reach the bounded "
        "required-input recovery path"
    )
    recovery_pending_guards = [
        node
        for node in ast.walk(shared_changed)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Attribute)
        and isinstance(node.test.value, ast.Name)
        and node.test.value.id == "self"
        and node.test.attr == "_required_input_recovery_pending"
        and any(isinstance(statement, ast.Return) for statement in node.body)
    ]
    assert len(recovery_pending_guards) == 1, (
        "shared recovery must not replace its one-shot debounce with a second "
        "broker-triggered recalculation"
    )

    recalculate = _method(class_node, "_recalculate_and_write")
    probe_type = _compile_probe_method(recalculate)
    probe = probe_type()
    probe.native_value = "missing"
    probe._attributes = {"missing_entities": []}
    probe._optimizer_lock = asyncio.Lock()
    probe._recalculate_cancel = None
    probe._shared_inputs_dirty = False
    probe._full_plan_solver_calls = 0
    probe._last_full_plan_at = None
    probe._full_plan_trigger = "startup"
    probe._full_plan_rejected_for_input_drift = False
    missing_entity = "sensor.hoymiles_hit_battery_capacity"

    async def commit_missing_projection() -> bool:
        # The source is already numeric in HA; the first completed solver pass
        # only now publishes that it had sampled it as missing during startup.
        probe._attributes = {
            "missing_entities": [missing_entity],
            "result_current": False,
            "recalculation_pending": True,
        }
        return True

    recovery_checks: list[tuple[str, ...]] = []
    probe._recalculate_locked = commit_missing_projection
    probe._mark_result_current = lambda: None
    probe._cancel_stale_result_retry = lambda: None
    probe._schedule_stale_result_retry = lambda: None
    probe.async_write_ha_state = lambda: None
    probe._publish_timeline_result = lambda: None
    probe._schedule_available_required_input_recovery = lambda: (
        recovery_checks.append(tuple(probe._attributes["missing_entities"]))
    )
    await probe._recalculate_and_write()
    assert recovery_checks == [(missing_entity,)], (
        "committed cold-start missing projection did not re-check already "
        "numeric required inputs"
    )


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
    calls: list[tuple[str, int, float | None]] = []
    rows_by_entity = {"sensor.a": 2, "sensor.b": 1}
    repeated = object()

    def fake_query(
        hass: Any,
        start: Any,
        end: Any,
        entity_id: str,
        limit: int,
        sample_interval_seconds: float | None,
        *,
        query_timeout_seconds: float,
    ) -> tuple[list[object], bool]:
        assert query_timeout_seconds > 0
        calls.append((entity_id, limit, sample_interval_seconds))
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
    assert calls == [("sensor.a", 3, None), ("sensor.b", 3, None)]

    calls.clear()
    sampled = await bounded(
        object(),
        1,
        2,
        ("sensor.b",),
        limit_per_entity=3,
        timeout_seconds=0.1,
        sample_interval_seconds=150.0,
    )
    assert len(sampled["sensor.b"]) == 1
    assert calls == [("sensor.b", 3, 150.0)]

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

    gate = asyncio.Event()

    class SlowRecorder:
        def __init__(self) -> None:
            self.started = 0

        async def async_add_executor_job(self, query: Any) -> Any:
            self.started += 1
            await gate.wait()
            return query()

    slow_recorder = SlowRecorder()
    rows_by_entity["sensor.a"] = 1
    namespace["get_recorder_instance"] = lambda hass: slow_recorder
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
        raise AssertionError("A second query overlapped the timed-out worker")
    assert slow_recorder.started == 1

    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    recovered = await bounded(
        object(),
        1,
        2,
        ("sensor.a",),
        limit_per_entity=3,
        timeout_seconds=0.1,
    )
    assert len(recovered["sensor.a"]) == 1
    assert slow_recorder.started == 2


def _assert_timeline_entity_lifecycle_contract() -> None:
    """Policy timelines stay passive; canonical output wires all three exactly once."""

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
            "async_create_background_task",
        )
    )
    assert timeline_source.count("async_call_later(") == 1
    assert "def _schedule_retention_expiry(" in timeline_source
    assert 'blocker_code="last_complete_expired"' in timeline_source
    retention_schedule = next(
        node
        for node in timeline_class.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_schedule_retention_expiry"
    )
    retention_callbacks = [
        node
        for node in retention_schedule.body
        if isinstance(node, ast.FunctionDef) and node.name == "expire"
    ]
    assert len(retention_callbacks) == 1
    assert [
        decorator.id
        for decorator in retention_callbacks[0].decorator_list
        if isinstance(decorator, ast.Name)
    ] == ["callback"], "timeline expiry must stay on the HA event loop"

    platform_path = COMPONENT / "sensor.py"
    platform_source = platform_path.read_text(encoding="utf-8")
    platform_tree = ast.parse(platform_source, filename=str(platform_path))
    setup = next(
        node
        for node in platform_tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "async_setup_entry"
    )

    def call_name(call: ast.Call) -> str | None:
        return call.func.id if isinstance(call.func, ast.Name) else None

    def appended_entity(statement: ast.stmt) -> str | None:
        if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
            return None
        call = statement.value
        if (
            not isinstance(call.func, ast.Attribute)
            or not isinstance(call.func.value, ast.Name)
            or call.func.value.id != "entities"
            or call.func.attr != "append"
            or len(call.args) != 1
        ):
            return None
        entity = call.args[0]
        if isinstance(entity, ast.Name):
            return entity.id
        if isinstance(entity, ast.Call):
            return call_name(entity)
        return None

    registered = [
        entity
        for statement in setup.body
        if (entity := appended_entity(statement)) is not None
    ]
    assert registered == [
        "supervisor",
        "accounting_v2",
        "rce_plan",
        "tariff_plan",
        "rce_timeline",
        "tariff_timeline",
        "rcm_plan",
        "rcm_timeline",
        "HoymilesSupervisorCanonicalPlanSensor",
        "HoymilesSetupStatusSensor",
        "HoymilesEMSSharedInputsSensor",
        "baseline_timeline",
        "tariff_price",
    ]

    timeline_assignments: list[tuple[int, str, ast.Call]] = []
    for position, statement in enumerate(setup.body):
        if (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
            and isinstance(statement.value, ast.Call)
            and call_name(statement.value) == "HoymilesAutomationPlanTimelineSensor"
        ):
            timeline_assignments.append(
                (position, statement.targets[0].id, statement.value)
            )
    assert [name for _, name, _ in timeline_assignments] == [
        "rce_timeline",
        "tariff_timeline",
        "rcm_timeline",
    ]
    expected_sources = {
        "rce_timeline": ("rce", "rce_plan"),
        "tariff_timeline": ("tariff", "tariff_plan"),
        "rcm_timeline": ("rcm", "rcm_plan"),
    }
    append_positions = {
        entity: position
        for position, statement in enumerate(setup.body)
        if (entity := appended_entity(statement)) is not None
    }
    for position, timeline_name, call in timeline_assignments:
        policy_id, source_name = expected_sources[timeline_name]
        assert len(call.args) == 3
        assert all(isinstance(arg, ast.Name) for arg in call.args)
        assert [arg.id for arg in call.args] == [
            "hass",
            "entry",
            "runtime",
        ]
        assert [keyword.arg for keyword in call.keywords] == [
            "policy_id",
            "source_sensor",
        ]
        assert isinstance(call.keywords[0].value, ast.Constant)
        assert call.keywords[0].value.value == policy_id
        assert isinstance(call.keywords[1].value, ast.Name)
        assert call.keywords[1].value.id == source_name
        assert append_positions[source_name] < position < append_positions[timeline_name]

    canonical_calls = [
        node
        for node in ast.walk(setup)
        if isinstance(node, ast.Call)
        and call_name(node) == "HoymilesSupervisorCanonicalPlanSensor"
    ]
    assert len(canonical_calls) == 1
    canonical_call = canonical_calls[0]
    assert len(canonical_call.args) == 3
    assert all(isinstance(arg, ast.Name) for arg in canonical_call.args)
    assert [arg.id for arg in canonical_call.args] == [
        "hass",
        "entry",
        "runtime",
    ]
    assert [keyword.arg for keyword in canonical_call.keywords] == [
        "supervisor",
        "timelines",
        "expected_timeline",
    ]
    assert isinstance(canonical_call.keywords[0].value, ast.Name)
    assert canonical_call.keywords[0].value.id == "supervisor"
    timeline_mapping = canonical_call.keywords[1].value
    assert isinstance(timeline_mapping, ast.Dict)
    assert len(timeline_mapping.keys) == len(timeline_mapping.values) == 3
    assert all(isinstance(key, ast.Constant) for key in timeline_mapping.keys)
    assert all(isinstance(value, ast.Name) for value in timeline_mapping.values)
    assert [key.value for key in timeline_mapping.keys] == [
        "rce",
        "tariff",
        "rcm",
    ]
    assert [value.id for value in timeline_mapping.values] == [
        "rce_timeline",
        "tariff_timeline",
        "rcm_timeline",
    ]
    assert isinstance(canonical_call.keywords[2].value, ast.Name)
    assert canonical_call.keywords[2].value.id == "baseline_timeline"
    assert all(
        append_positions[timeline_name]
        < append_positions["HoymilesSupervisorCanonicalPlanSensor"]
        for timeline_name in ("rce_timeline", "tariff_timeline", "rcm_timeline")
    )

    canonical_path = COMPONENT / "supervisor_canonical_sensor.py"
    canonical_source = canonical_path.read_text(encoding="utf-8")
    canonical_tree = ast.parse(canonical_source, filename=str(canonical_path))
    canonical_class = next(
        node
        for node in canonical_tree.body
        if isinstance(node, ast.ClassDef)
        and node.name == "HoymilesSupervisorCanonicalPlanSensor"
    )
    canonical_methods = {
        node.name: node
        for node in canonical_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert {"async_added_to_hass", "async_will_remove_from_hass"} <= set(
        canonical_methods
    )
    assert not any(
        token in canonical_source
        for token in (
            "async_track_time_interval",
            "async_create_background_task",
        )
    )

    init = canonical_methods["__init__"]
    assert [arg.arg for arg in init.args.kwonlyargs] == [
        "supervisor",
        "timelines",
        "expected_timeline",
    ]
    assert init.args.kw_defaults == [None, None, None]

    empty_attributes = canonical_methods["_empty_attributes"]
    payloads = [node for node in ast.walk(empty_attributes) if isinstance(node, ast.Dict)]
    output_payload = next(
        payload
        for payload in payloads
        if any(
            isinstance(key, ast.Constant) and key.value == "output_only"
            for key in payload.keys
        )
    )
    output_fields = {
        key.value: value
        for key, value in zip(output_payload.keys, output_payload.values, strict=True)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    assert isinstance(output_fields["output_only"], ast.Constant)
    assert output_fields["output_only"].value is True
    assert isinstance(output_fields["slots"], ast.List)
    assert output_fields["slots"].elts == []

    recompute = canonical_methods["_recompute"]
    build_calls = [
        node
        for node in ast.walk(recompute)
        if isinstance(node, ast.Call)
        and call_name(node) == "build_supervisor_canonical_ledger"
    ]
    assert len(build_calls) == 1
    assert [keyword.arg for keyword in build_calls[0].keywords] == [
        "frame",
        "timelines",
        "usable_capacity_kwh",
        "freshness_now",
    ]
    retained_publishes = [
        call
        for call in ast.walk(recompute)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "_publish"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value in {"pending", "partial"}
    ]
    assert len(retained_publishes) == 3
    retained_with_empty_fallback = [
        call
        for call in retained_publishes
        if len(call.args) == 2 and isinstance(call.args[1], ast.BoolOp)
    ]
    assert len(retained_with_empty_fallback) == 2
    assert all(
        len(call.args) == 2
        and isinstance(call.args[1], ast.BoolOp)
        and isinstance(call.args[1].op, ast.Or)
        and len(call.args[1].values) == 2
        and isinstance(call.args[1].values[1], ast.Call)
        and isinstance(call.args[1].values[1].func, ast.Attribute)
        and call.args[1].values[1].func.attr == "_empty_attributes"
        for call in retained_with_empty_fallback
    ), "pending or partial canonical output must retain only a prior complete trajectory"
    retained_stale_failure = [
        call
        for call in retained_publishes
        if len(call.args) == 2
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "partial"
        and isinstance(call.args[1], ast.Name)
        and call.args[1].id == "retained"
    ]
    assert len(retained_stale_failure) == 1, (
        "a stale canonical adapter result may retain only the explicitly "
        "validated prior trajectory"
    )
    unavailable_publishes = [
        call
        for call in ast.walk(recompute)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "_publish"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and call.args[0].value == "unavailable"
    ]
    assert len(unavailable_publishes) == 3
    assert all(
        len(call.args) == 2
        and isinstance(call.args[1], ast.Call)
        and isinstance(call.args[1].func, ast.Attribute)
        and call.args[1].func.attr == "_empty_attributes"
        for call in unavailable_publishes
    ), "unrecoverable canonical failures must remain fail-closed"

    schedule = canonical_methods["_schedule_recompute"]
    delayed_calls = [
        call
        for call in ast.walk(schedule)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "async_call_later"
    ]
    assert len(delayed_calls) == 1
    assert isinstance(delayed_calls[0].args[1], ast.Name)
    assert delayed_calls[0].args[1].id == "CANONICAL_COHORT_DELAY_SECONDS"
    recompute_callbacks = [
        node
        for node in schedule.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "recompute_callback"
    ]
    assert len(recompute_callbacks) == 1
    assert [
        decorator.id
        for decorator in recompute_callbacks[0].decorator_list
        if isinstance(decorator, ast.Name)
    ] == ["callback"], "canonical cohort timer must stay on the HA event loop"
    assert isinstance(delayed_calls[0].args[2], ast.Name)
    assert delayed_calls[0].args[2].id == "recompute_callback"
    freshness_schedule = canonical_methods["_schedule_freshness_expiry"]
    freshness_callbacks = [
        node
        for node in freshness_schedule.body
        if isinstance(node, ast.FunctionDef) and node.name == "expire"
    ]
    assert len(freshness_callbacks) == 1
    assert [
        decorator.id
        for decorator in freshness_callbacks[0].decorator_list
        if isinstance(decorator, ast.Name)
    ] == ["callback"], "canonical freshness timer must stay on the HA event loop"
    assert not any(
        isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Attribute)
            and target.attr == "_recompute_cancel"
            for target in node.targets
        )
        for node in ast.walk(recompute)
    ), "freshness recompute must not clear a pending cohort timer handle"

    added = canonical_methods["async_added_to_hass"]
    added_calls = {
        node.func.attr
        for node in ast.walk(added)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"_subscribe_sources", "async_listen", "_recompute"} <= added_calls
    removed = canonical_methods["async_will_remove_from_hass"]
    removed_constants = {
        node.value
        for node in ast.walk(removed)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {"_source_unsub", "_registry_unsub"} <= removed_constants
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "_recompute_cancel"
        for node in ast.walk(removed)
    ), "canonical recompute coalescing must be cancelled on entity removal"


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
    await _assert_tariff_feedback_restore_version()
    await _assert_tariff_delayed_recalculation_lifecycle()
    await _assert_tariff_required_input_recovery_startup_contract()
    await _assert_bounded_recorder_runtime()


def main() -> None:
    asyncio.run(_async_main())
    print("Optimizer startup: non-blocking, bounded and cancel-safe contracts passed")


if __name__ == "__main__":
    main()
