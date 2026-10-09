"""Contract tests for non-blocking, serialized Home Assistant optimizers."""

from __future__ import annotations

import ast
import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import is_dataclass, replace
from datetime import datetime, timedelta, timezone
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


def _load_supervisor_ledger_module():
    """Load the pure accounting ledger without importing Home Assistant."""

    component_path = str(COMPONENT)
    sys.path.insert(0, component_path)
    try:
        path = COMPONENT / "supervisor_ledger.py"
        spec = importlib.util.spec_from_file_location(
            "supervisor_ledger_executor_contract",
            path,
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(component_path)


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
    """Accounting feedback must not become raw tariff-planning telemetry."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    watched = _literal_string_set(tree, "WATCHED_TARIFF_ENTITIES")
    feedback = _literal_string_set(tree, "TARIFF_EXECUTION_FEEDBACK_ENTITIES")
    expected_feedback = {
        "input_boolean.hoymiles_tariff_charge_active",
        "input_text.hoymiles_tariff_active_action",
        "sensor.hoymiles_ems_control_owner",
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "sensor.hoymiles_hit_ems_control_readback_generation",
        "sensor.hoymiles_hit_grid_to_battery_power",
        "sensor.hoymiles_hit_overview_grid_total_active_power",
    }
    assert feedback == expected_feedback
    assert watched.isdisjoint(feedback), (
        "Execution-only tariff feedback can withdraw planning authority"
    )
    feedback_name_uses = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and node.id == "TARIFF_EXECUTION_FEEDBACK_ENTITIES"
    ]
    assert len(feedback_name_uses) == 1 and isinstance(
        feedback_name_uses[0].ctx, ast.Store
    ), "Legacy telemetry feedback set is consumed after accounting-v2 cutover"

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
        expected_set = "TARIFF_EVENT_DRIVEN_ENTITIES"
        assert expected_set in names, (
            f"{method_name} no longer uses its authoritative planning-input set"
        )
        assert "TARIFF_EXECUTION_FEEDBACK_ENTITIES" not in names
        assert feedback.isdisjoint(literals), (
            f"{method_name} reintroduced an execution-only tariff input"
        )

    feedback_method = _method(
        class_node, "observe_supervisor_accounting_feedback"
    )
    assert [arg.arg for arg in feedback_method.args.kwonlyargs] == [
        "delivered_power_feedback_w",
        "evidence_fingerprint",
        "observed_at",
        "transaction_started_at",
    ], "Tariff feedback no longer accepts the bounded accounting-v2 projection"
    feedback_literals = {
        node.value
        for node in ast.walk(feedback_method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    forbidden_feedback_sources = feedback | {
        "sensor.hoymiles_tariff_grid_charge_power",
        "sensor.hoymiles_hit_overview_load_active_power",
        "sensor.hoymiles_hit_overview_battery_power",
        "sensor.hoymiles_hit_overview_pv_total_power",
    }
    assert forbidden_feedback_sources.isdisjoint(feedback_literals), (
        "Tariff feedback bypasses Supervisor accounting v2 and samples HA power"
    )
    feedback_attributes = {
        node.attr
        for node in ast.walk(feedback_method)
        if isinstance(node, ast.Attribute)
    }
    assert {
        "_invalidate_internal_inputs",
        "_recalculate_and_write",
        "_optimizer_input",
    }.isdisjoint(feedback_attributes), (
        "An accounting observation can invalidate or run an accepted plan"
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
        if item[1] in {"_invalidate_internal_inputs", "_recalculate_and_write"}
    ]
    assert relevant == [
        (relevant[0][0], "_invalidate_internal_inputs", False),
        (relevant[1][0], "_recalculate_and_write", True),
    ], "Tariff timer reintroduced a direct telemetry feedback sampler"

    assignments = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    feedback_version = ast.literal_eval(
        assignments["CHARGE_POWER_FEEDBACK_VERSION"]
    )
    assert feedback_version == 3
    assert all(version != feedback_version for version in (1, 2)), (
        "Legacy delivered-power feedback v1/v2 can be restored as current"
    )
    assert ast.unparse(
        assignments["TARIFF_INPUT_RECALCULATION_DELAY_SECONDS"]
    ) == "5 * 60.0"
    assert ast.literal_eval(
        assignments["TARIFF_IMMEDIATE_RECALCULATION_DELAY_SECONDS"]
    ) == 1.0
    assert ast.literal_eval(
        assignments["TARIFF_SHARED_BOOTSTRAP_RECALCULATION_DELAY_SECONDS"]
    ) == 1.0
    shared_changed = _method(class_node, "_async_shared_inputs_changed")
    shared_changed_names = {
        node.id for node in ast.walk(shared_changed) if isinstance(node, ast.Name)
    }
    shared_changed_attributes = {
        node.attr for node in ast.walk(shared_changed) if isinstance(node, ast.Attribute)
    }
    assert "TARIFF_SHARED_BOOTSTRAP_RECALCULATION_DELAY_SECONDS" in (
        shared_changed_names
    )
    assert "TARIFF_INPUT_RECALCULATION_DELAY_SECONDS" not in (
        shared_changed_names
    ), "Ordinary Shared EMS churn can still arm a second tariff timer"
    assert "_shared_inputs_bootstrap_seen" in shared_changed_attributes
    added = _method(class_node, "async_added_to_hass")
    version_restore_checks = [
        node
        for node in ast.walk(added)
        if isinstance(node, ast.Compare)
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Name)
        and node.comparators[0].id == "CHARGE_POWER_FEEDBACK_VERSION"
        and any(
            isinstance(item, ast.Constant)
            and item.value == "charge_power_feedback_version"
            for item in ast.walk(node.left)
        )
    ]
    assert len(version_restore_checks) == 1, (
        "Restored delivered-power feedback is not pinned to current v3"
    )
    full_plan_intervals = [
        node
        for node in ast.walk(added)
        if isinstance(node, ast.Name)
        and node.id == "TARIFF_FULL_OPTIMIZER_INTERVAL"
    ]
    assert len(full_plan_intervals) == 1
    immediate_input_names = {
        node.id
        for node in ast.walk(_method(class_node, "_async_input_changed"))
        if isinstance(node, ast.Name)
    }
    assert "TARIFF_IMMEDIATE_RECALCULATION_DELAY_SECONDS" in immediate_input_names
    assert "TARIFF_INPUT_RECALCULATION_DELAY_SECONDS" not in immediate_input_names
    assert "INPUT_RECALCULATION_DELAY_SECONDS" not in immediate_input_names
    gcf_policy_names = {
        node.id
        for node in ast.walk(_method(class_node, "_apply_forecast_gcf_policy"))
        if isinstance(node, ast.Name)
    }
    assert "TARIFF_INPUT_RECALCULATION_DELAY_SECONDS" in gcf_policy_names
    assert "INPUT_RECALCULATION_DELAY_SECONDS" not in gcf_policy_names

    source_text = path.read_text(encoding="utf-8")
    gcf_handler_source = ast.get_source_segment(
        source_text,
        _method(class_node, "_async_forecast_gcf_policy_changed"),
    )
    gcf_deadline_source = ast.get_source_segment(
        source_text,
        _method(class_node, "_async_evaluate_forecast_gcf_policy"),
    )
    gcf_apply_source = ast.get_source_segment(
        source_text,
        _method(class_node, "_apply_forecast_gcf_policy"),
    )
    assert gcf_handler_source is not None
    assert gcf_deadline_source is not None
    assert gcf_apply_source is not None
    assert "_invalidate_internal_inputs" not in gcf_handler_source, (
        "A partial GCF delivery cohort can still flap tariff authority"
    )
    assert "force_recalculate=cohort_pending" not in gcf_handler_source
    assert "force_recalculate" not in gcf_deadline_source
    assert "force_recalculate" not in gcf_apply_source, (
        "Tariff GCF can still withdraw authority without a semantic change"
    )

    sensor_source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    assert sensor_source.count("accounting_v2.attach_feedback_sink(") == 1
    assert (
        "tariff_plan.observe_supervisor_accounting_feedback" in sensor_source
    ), "Tariff feedback is not wired exclusively from accounting v2"

    accounting_path = COMPONENT / "supervisor_accounting_sensor.py"
    accounting_tree = ast.parse(
        accounting_path.read_text(encoding="utf-8"), filename=str(accounting_path)
    )
    accounting_method = _method(
        _class_node(accounting_tree, "HoymilesSupervisorAccountingV2Sensor"),
        "async_process_active_frame",
    )
    accounting_literals = {
        node.value
        for node in ast.walk(accounting_method)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert {
        "sensor.hoymiles_tariff_grid_charge_power",
        "sensor.hoymiles_hit_overview_load_active_power",
        "sensor.hoymiles_hit_overview_battery_power",
        "sensor.hoymiles_hit_overview_pv_total_power",
    }.isdisjoint(accounting_literals), (
        "Accounting-v2 sink can fall back to aggregate battery/PV telemetry"
    )


def _assert_tariff_shared_bootstrap_replaces_pending_timer() -> None:
    """The one-second broker bootstrap must replace one older general timer."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    method = deepcopy(
        _method(
            _class_node(tree, "HoymilesTariffOptimizerSensor"),
            "_async_shared_inputs_changed",
        )
    )
    method.decorator_list = []
    probe_class = ast.ClassDef(
        name="Probe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[method],
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[probe_class], type_ignores=[])
    )
    scheduled: list[dict[str, Any]] = []

    def async_call_later(_hass: Any, delay: float, callback: Any) -> Callable[[], None]:
        record = {"delay": delay, "callback": callback, "cancelled": False}
        scheduled.append(record)

        def cancel() -> None:
            record["cancelled"] = True

        return cancel

    namespace: dict[str, Any] = {
        "TARIFF_SHARED_BOOTSTRAP_RECALCULATION_DELAY_SECONDS": 1.0,
        "TARIFF_INPUT_RECALCULATION_DELAY_SECONDS": 300.0,
        "_shared_inputs_snapshot": lambda runtime: SimpleNamespace(
            load=SimpleNamespace(ready=runtime["load_ready"])
        ),
        "_tariff_shared_critical_signature": (
            lambda runtime: runtime["critical_signature"]
        ),
        "async_call_later": async_call_later,
    }
    exec(compile(module, "<tariff-shared-bootstrap-probe>", "exec"), namespace)
    probe = namespace["Probe"]()
    probe.hass = object()
    probe._lifecycle_stopped = False
    probe._runtime = {"critical_signature": "stable", "load_ready": False}
    probe._shared_inputs_critical_signature = "stable"
    probe._shared_inputs_load_ready = False
    probe._shared_inputs_bootstrap_seen = False
    probe._shared_inputs_dirty = False
    probe._required_input_recovery_pending = False
    probe._schedule_available_required_input_recovery = lambda: None
    invalidations: list[None] = []
    probe._invalidate_internal_inputs = lambda: invalidations.append(None)
    probe._async_debounced_recalculate = object()
    previous_cancelled: list[bool] = []
    probe._recalculate_cancel = lambda: previous_cancelled.append(True)

    probe._async_shared_inputs_changed()
    assert previous_cancelled == [True]
    assert [record["delay"] for record in scheduled] == [1.0]
    assert not scheduled[0]["cancelled"]
    assert probe._shared_inputs_bootstrap_seen

    # A second callback keeps the leading-edge bootstrap timer in place.
    probe._async_shared_inputs_changed()
    assert [record["delay"] for record in scheduled] == [1.0]
    assert not scheduled[0]["cancelled"]
    assert probe._shared_inputs_dirty

    # Once the bootstrap callback has fired, ordinary Shared EMS revisions are
    # only marked dirty.  The single periodic tariff timer consumes them.
    probe._recalculate_cancel = None
    probe._async_shared_inputs_changed()
    probe._async_shared_inputs_changed()
    assert [record["delay"] for record in scheduled] == [1.0]
    assert len(invalidations) == 1
    assert probe._shared_inputs_dirty

    # LOAD partial -> ready is a liveness edge and schedules one immediate run.
    probe._runtime["load_ready"] = True
    probe._runtime["critical_signature"] = "load-ready"
    probe._async_shared_inputs_changed()
    assert [record["delay"] for record in scheduled] == [1.0, 1.0]
    assert not scheduled[1]["cancelled"]
    assert len(invalidations) == 2
    assert probe._shared_inputs_load_ready is True

    # Ordinary broker revisions while LOAD remains ready still arm no timer.
    probe._recalculate_cancel = None
    probe._async_shared_inputs_changed()
    probe._async_shared_inputs_changed()
    assert [record["delay"] for record in scheduled] == [1.0, 1.0]

    # LOAD ready -> partial is retained for the next coherent five-minute
    # snapshot; physical execution gates remain independently fail-closed.
    probe._runtime["load_ready"] = False
    probe._runtime["critical_signature"] = "load-partial"
    probe._async_shared_inputs_changed()
    assert len(invalidations) == 2
    assert [record["delay"] for record in scheduled] == [1.0, 1.0]
    assert probe._shared_inputs_dirty
    assert probe._shared_inputs_load_ready is False

    # A shared-only control identity/freshness change is also part of that
    # bounded snapshot and cannot race the periodic solver.
    probe._runtime["critical_signature"] = "changed"
    probe._async_shared_inputs_changed()
    assert len(invalidations) == 2
    assert [record["delay"] for record in scheduled] == [1.0, 1.0]
    assert probe._shared_inputs_dirty


async def _assert_tariff_shared_dirty_is_sampled_on_debounce() -> None:
    """Fast broker churn is sampled once when the coalesced run starts."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    method = deepcopy(
        _method(
            _class_node(tree, "HoymilesTariffOptimizerSensor"),
            "_async_debounced_recalculate",
        )
    )
    probe_class = ast.ClassDef(
        name="Probe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[method],
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[probe_class], type_ignores=[])
    )
    namespace: dict[str, Any] = {
        "asyncio": asyncio,
        "datetime": datetime,
    }
    exec(compile(module, "<tariff-shared-dirty-probe>", "exec"), namespace)
    probe = namespace["Probe"]()
    probe._recalculate_cancel = object()
    probe._lifecycle_stopped = False
    probe._shared_inputs_dirty = True
    probe._delayed_recalculate_tasks = set()
    invalidations: list[None] = []
    recalculations: list[None] = []
    probe._invalidate_internal_inputs = lambda: invalidations.append(None)

    async def recalculate() -> None:
        recalculations.append(None)

    probe._recalculate_and_write = recalculate
    await probe._async_debounced_recalculate(datetime.now(timezone.utc))
    assert invalidations == [None]
    assert recalculations == [None]
    assert not probe._shared_inputs_dirty

    # A general delayed run with no new broker revision must not double-count
    # the optimizer input revision.
    await probe._async_debounced_recalculate(datetime.now(timezone.utc))
    assert invalidations == [None]
    assert recalculations == [None, None]


def _assert_tariff_shared_critical_signature_is_cadence_free() -> None:
    """GCF report values may churn, but lost freshness is immediately critical."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function_names = {
        "_tariff_shared_sample_control_signature",
        "_tariff_shared_critical_signature",
    }
    functions = [
        deepcopy(node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in function_names
    ]
    assert {node.name for node in functions} == function_names
    namespace: dict[str, Any] = {
        "Any": Any,
        "RuntimeData": Any,
        "_shared_inputs_snapshot": lambda runtime: runtime,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[])),
            "<tariff-shared-critical-signature-probe>",
            "exec",
        ),
        namespace,
    )
    signature = namespace["_tariff_shared_critical_signature"]

    def sample(
        value: Any,
        *,
        fresh: bool = True,
        reason: str = "current",
        quality: str = "current",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            value=value,
            entity_id="sensor.fixture",
            source_entity_ids=("sensor.fixture",),
            selector_entity_id=None,
            fresh=fresh,
            reason=reason,
            quality=quality,
            provenance="physical_fc03",
        )

    snapshot = SimpleNamespace(
        schema_version=1,
        config_entry_id="entry-a",
        load=SimpleNamespace(ready=False),
        system=SimpleNamespace(
            battery_capacity_kwh=sample(26.0),
            inverter_count=sample(1),
            inverter_rated_power_each_kw=sample(10.0),
            system_rated_power_kw=sample(10.0),
        ),
        bms=SimpleNamespace(
            voltage_v=sample(51.2),
            maximum_charge_current_a=sample(100.0),
            maximum_discharge_current_a=sample(100.0),
            maximum_charge_power_kw=sample(5.12),
            maximum_discharge_power_kw=sample(5.12),
        ),
        efficiency=SimpleNamespace(
            battery_to_home_efficiency=sample(0.92),
        ),
        gcf=SimpleNamespace(
            hardware_readback_supported=sample(True),
            generation=sample(10),
            enable_code=sample(1),
            maximum_export_power_percent=sample(0.0),
        ),
    )
    baseline = signature(snapshot)
    snapshot.gcf.generation = sample(11)
    assert signature(snapshot) == baseline, (
        "GCF generation cadence reintroduced tariff authority flapping"
    )
    snapshot.load.ready = True
    assert signature(snapshot) != baseline, (
        "Shared LOAD partial -> ready is absent from the critical fingerprint"
    )
    snapshot.load.ready = False
    snapshot.gcf.generation = sample(
        11,
        fresh=False,
        reason="stale",
        quality="unavailable",
    )
    assert signature(snapshot) != baseline, (
        "GCF freshness loss no longer withdraws tariff authority immediately"
    )


async def _assert_tariff_gcf_deadline_is_semantic_and_cancel_safe() -> None:
    """Apply each GCF semantic transition once, including forced late callbacks."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = _class_node(tree, "HoymilesTariffOptimizerSensor")
    method_names = {
        "_async_forecast_gcf_policy_changed",
        "_cancel_forecast_gcf_policy_evaluation",
        "_schedule_forecast_gcf_recovery_if_pending",
        "_async_evaluate_forecast_gcf_policy",
        "_apply_forecast_gcf_policy",
    }
    methods = [
        deepcopy(node)
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in method_names
    ]
    assert {node.name for node in methods} == method_names

    probe_class = ast.ClassDef(
        name="TariffGCFEventProbe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=methods,
    )
    module = ast.fix_missing_locations(
        ast.Module(
            body=[
                ast.ImportFrom(
                    module="__future__",
                    names=[ast.alias(name="annotations")],
                    level=0,
                ),
                probe_class,
            ],
            type_ignores=[],
        )
    )

    coherent = SimpleNamespace(
        enabled=False,
        mode="fixed_zero_export",
        excluded_reason=None,
        factor_override=0.80,
    )
    incoherent = SimpleNamespace(
        enabled=False,
        mode="conservative_gcf_unverified",
        excluded_reason="gcf_readback_incoherent",
        factor_override=0.65,
    )
    policy_slot = {"policy": coherent}
    scheduled: list[dict[str, Any]] = []

    def policy_signature(policy: Any) -> tuple[Any, ...]:
        return (
            policy.enabled,
            policy.mode,
            policy.excluded_reason,
            policy.factor_override,
        )

    def async_call_later(
        _hass: Any,
        delay: float,
        callback_func: Any,
    ) -> Callable[[], None]:
        token = {
            "delay": delay,
            "callback": callback_func,
            "cancelled": False,
        }
        scheduled.append(token)

        def cancel() -> None:
            token["cancelled"] = True

        return cancel

    namespace: dict[str, Any] = {
        "Any": Any,
        "callback": lambda method: method,
        "dt_util": SimpleNamespace(
            now=lambda: datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        ),
        "async_call_later": async_call_later,
        "_FORECAST_GCF_READBACK_MAX_SKEW_SECONDS": 5.0,
        "TARIFF_INPUT_RECALCULATION_DELAY_SECONDS": 300.0,
        "_forecast_learning_policy_signature": policy_signature,
        "_forecast_learning_policy_snapshot": (
            lambda _hass, _now, _runtime: (policy_slot["policy"], {})
        ),
    }
    exec(compile(module, "<tariff-gcf-event-probe>", "exec"), namespace)
    probe_type = namespace["TariffGCFEventProbe"]

    def new_probe(initial_policy: Any) -> tuple[Any, list[None], list[None]]:
        probe = probe_type()
        probe.hass = object()
        probe._runtime = object()
        probe._forecast_gcf_policy_evaluation_cancel = None
        probe._forecast_gcf_policy_signature = policy_signature(initial_policy)
        probe._recalculate_cancel = None
        probe._lifecycle_stopped = False
        probe._attributes = {
            "result_current": True,
            "recalculation_pending": False,
        }
        probe._async_debounced_recalculate = object()
        invalidations: list[None] = []
        refreshes: list[None] = []
        probe._invalidate_internal_inputs = lambda: invalidations.append(None)
        probe._schedule_forecast_policy_refresh = lambda: refreshes.append(None)
        return probe, invalidations, refreshes

    async def force_fire(token: dict[str, Any]) -> None:
        callback_func = token["callback"]
        assert callable(callback_func)
        await callback_func(datetime(2026, 9, 1, 12, 0, 5, tzinfo=timezone.utc))

    # A mixed delivery arms the deadline, but recovery to the identical
    # coherent semantic policy before it fires must be a complete no-op even
    # if callback ordering prevented the normal cancellation path.
    probe, invalidations, refreshes = new_probe(coherent)
    policy_slot["policy"] = incoherent
    probe._async_forecast_gcf_policy_changed(object())
    assert len(scheduled) == 1
    recovered_deadline = scheduled[0]
    policy_slot["policy"] = coherent
    await force_fire(recovered_deadline)
    assert invalidations == []
    assert refreshes == []
    assert probe._forecast_gcf_policy_evaluation_cancel is None
    assert probe._recalculate_cancel is None
    assert len(scheduled) == 1

    # If a calculation fires during the grace period, it withdraws authority
    # and consumes its old timer at the grace barrier.  Identical coherent
    # closure must queue exactly one immediate retry without a new revision.
    scheduled.clear()
    probe, invalidations, refreshes = new_probe(coherent)
    policy_slot["policy"] = incoherent
    probe._async_forecast_gcf_policy_changed(object())
    assert len(scheduled) == 1
    pending_deadline = scheduled[0]
    probe._attributes = {
        "result_current": False,
        "recalculation_pending": True,
    }
    policy_slot["policy"] = coherent
    probe._async_forecast_gcf_policy_changed(object())
    assert pending_deadline["cancelled"] is True
    assert invalidations == []
    assert refreshes == []
    assert probe._forecast_gcf_policy_evaluation_cancel is None
    assert probe._recalculate_cancel is not None
    assert len(scheduled) == 2
    assert scheduled[1]["delay"] == 0.0
    probe._async_forecast_gcf_policy_changed(object())
    assert invalidations == []
    assert len(scheduled) == 2

    # A duplicate coherent report without an active GCF grace period must not
    # wake a plan that is pending for an unrelated input or lifecycle reason.
    scheduled.clear()
    probe, invalidations, refreshes = new_probe(coherent)
    probe._attributes = {
        "result_current": False,
        "recalculation_pending": True,
    }
    policy_slot["policy"] = coherent
    probe._async_forecast_gcf_policy_changed(object())
    assert invalidations == []
    assert refreshes == []
    assert probe._recalculate_cancel is None
    assert scheduled == []

    # The same liveness repair is required when callback ordering lets the
    # deadline itself observe the already-restored coherent policy.
    scheduled.clear()
    probe, invalidations, refreshes = new_probe(coherent)
    policy_slot["policy"] = incoherent
    probe._async_forecast_gcf_policy_changed(object())
    assert len(scheduled) == 1
    pending_deadline = scheduled[0]
    probe._attributes = {
        "result_current": False,
        "recalculation_pending": True,
    }
    policy_slot["policy"] = coherent
    await force_fire(pending_deadline)
    assert invalidations == []
    assert refreshes == []
    assert probe._forecast_gcf_policy_evaluation_cancel is None
    assert probe._recalculate_cancel is not None
    assert len(scheduled) == 2
    assert scheduled[1]["delay"] == 0.0
    await force_fire(pending_deadline)
    assert invalidations == []
    assert len(scheduled) == 2

    # The first persistent incoherence fails closed exactly once and queues one
    # replacement plan.  Identical later reports cannot arm another deadline.
    scheduled.clear()
    probe, invalidations, refreshes = new_probe(coherent)
    policy_slot["policy"] = incoherent
    probe._async_forecast_gcf_policy_changed(object())
    assert len(scheduled) == 1
    await force_fire(scheduled[0])
    assert invalidations == [None]
    assert refreshes == []
    assert probe._forecast_gcf_policy_signature == policy_signature(incoherent)
    assert probe._recalculate_cancel is not None
    assert len(scheduled) == 2
    for _report in range(4):
        probe._async_forecast_gcf_policy_changed(object())
    assert invalidations == [None]
    assert len(scheduled) == 2
    assert probe._forecast_gcf_policy_evaluation_cancel is None

    # Recovery from the applied fail-closed condition is one semantic change,
    # so it invalidates and replans once; duplicate coherent reports are inert.
    scheduled.clear()
    probe, invalidations, refreshes = new_probe(incoherent)
    policy_slot["policy"] = coherent
    probe._async_forecast_gcf_policy_changed(object())
    probe._async_forecast_gcf_policy_changed(object())
    assert invalidations == [None]
    assert refreshes == []
    assert probe._forecast_gcf_policy_signature == policy_signature(coherent)
    assert probe._recalculate_cancel is not None
    assert len(scheduled) == 1

    # Removal cancels the retained deadline.  Even a deliberately forced late
    # callback, event, or direct apply cannot mutate revision or queue work.
    scheduled.clear()
    probe, invalidations, refreshes = new_probe(coherent)
    policy_slot["policy"] = incoherent
    probe._async_forecast_gcf_policy_changed(object())
    assert len(scheduled) == 1
    late_deadline = scheduled[0]
    probe._attributes = {
        "result_current": False,
        "recalculation_pending": True,
    }
    probe._lifecycle_stopped = True
    probe._cancel_forecast_gcf_policy_evaluation()
    assert late_deadline["cancelled"] is True
    await force_fire(late_deadline)
    probe._async_forecast_gcf_policy_changed(object())
    probe._apply_forecast_gcf_policy(incoherent)
    assert invalidations == []
    assert refreshes == []
    assert probe._forecast_gcf_policy_signature == policy_signature(coherent)
    assert probe._recalculate_cancel is None
    assert len(scheduled) == 1


def _assert_tariff_feedback_uses_only_physical_grid_import() -> None:
    """Prove the feedback path consumes only qualified physical attribution."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    method = deepcopy(
        _method(
            _class_node(tree, "HoymilesTariffOptimizerSensor"),
            "observe_supervisor_accounting_feedback",
        )
    )
    probe_class = ast.ClassDef(
        name="Probe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[method],
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[probe_class], type_ignores=[])
    )
    now = datetime(2026, 8, 31, 8, 0, tzinfo=timezone.utc)

    def numeric_state_sample(
        state: Any,
        sample_now: datetime,
        *,
        max_age_seconds: float,
        minimum: float | None = None,
        maximum: float | None = None,
    ) -> Any:
        if state is None:
            return SimpleNamespace(fresh=False, value=None)
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return SimpleNamespace(fresh=False, value=None)
        age = (sample_now - state.last_updated).total_seconds()
        valid = -5.0 <= age <= max_age_seconds
        valid = valid and (minimum is None or value >= minimum)
        valid = valid and (maximum is None or value <= maximum)
        return SimpleNamespace(fresh=valid, value=value if valid else None)

    namespace: dict[str, Any] = {
        "CHARGE_POWER_FEEDBACK_MIN_SAMPLES": 5,
        "LIVE_TELEMETRY_MAX_AGE_SECONDS": 120.0,
        "SLOW_TELEMETRY_MAX_AGE_SECONDS": 300.0,
        "datetime": datetime,
        "dt_util": SimpleNamespace(utcnow=lambda: now),
        "math": __import__("math"),
        "numeric_state_sample": numeric_state_sample,
        "_shared_inputs_snapshot": lambda _runtime: None,
        "_policy_numeric_sample": (
            lambda _runtime, _section, _field, legacy_state, sample_now, **kwargs:
                numeric_state_sample(legacy_state, sample_now, **kwargs)
        ),
        "timedelta": timedelta,
        "timezone": timezone,
        "median": __import__("statistics").median,
        "_state_number": lambda hass, entity_id: (
            float(hass.states.get(entity_id).state)
            if hass.states.get(entity_id) is not None
            else None
        ),
    }
    exec(compile(module, "<tariff-feedback-probe>", "exec"), namespace)
    probe_type = namespace["Probe"]

    def state(
        value: str,
        *,
        age_seconds: float = 0.0,
        attributes: dict[str, Any] | None = None,
        changed_seconds: float = 300.0,
    ) -> Any:
        return SimpleNamespace(
            state=value,
            attributes=attributes or {},
            last_updated=now - timedelta(seconds=age_seconds),
            last_changed=now - timedelta(seconds=changed_seconds),
        )

    base_states = {
        "sensor.hoymiles_hit_overview_battery_soc": state("50"),
        "input_number.hoymiles_tariff_maximum_soc": state("100"),
        # Deliberately contaminated legacy/aggregate sources are inaccessible
        # to the v3 consumer and therefore cannot change its result.
        "sensor.hoymiles_tariff_grid_charge_power": state("5000"),
        "sensor.hoymiles_hit_overview_battery_power": state("9000"),
        "sensor.hoymiles_hit_overview_pv_total_power": state("12000"),
    }

    def run(
        *,
        delivered_power_w: Any = 4000.0,
        fingerprint: Any = "a" * 64,
        observed_age_seconds: float = 0.0,
        transaction_age_seconds: float = 180.0,
        overrides: dict[str, Any | None] | None = None,
    ) -> Any:
        values = dict(base_states)
        for entity_id, value in (overrides or {}).items():
            if value is None:
                values.pop(entity_id, None)
            else:
                values[entity_id] = value

        class States:
            def get(self, entity_id: str) -> Any:
                return values.get(entity_id)

        probe = probe_type()
        probe._runtime = SimpleNamespace(shared_inputs=None)
        probe.hass = SimpleNamespace(states=States())
        probe._attributes = {"requested_charge_power_kw": 5.0}
        probe._delivered_power_ratios = deque(maxlen=24)
        probe._charge_power_feedback_last_ratio = None
        probe._charge_power_feedback_last_sample_at = None
        probe._charge_power_feedback_last_evidence_fingerprint = None
        probe._effective_charge_power_factor = 1.0
        probe._effective_charge_power_source = "configured"
        probe.observe_supervisor_accounting_feedback(
            delivered_power_feedback_w=delivered_power_w,
            evidence_fingerprint=fingerprint,
            observed_at=now - timedelta(seconds=observed_age_seconds),
            transaction_started_at=(
                now - timedelta(seconds=transaction_age_seconds)
            ),
        )
        return probe

    valid = run()
    assert list(valid._delivered_power_ratios) == [0.8]
    assert valid._charge_power_feedback_last_ratio == 0.8
    assert valid._charge_power_feedback_last_evidence_fingerprint == "a" * 64

    blocked_cases = {
        "non-positive accounting projection": {"delivered_power_w": 0.0},
        "malformed accounting fingerprint": {"fingerprint": "legacy-v2"},
        "stale accounting projection": {"observed_age_seconds": 121.0},
        "transaction still ramping": {"transaction_age_seconds": 119.0},
        "high-SOC taper": {
            "overrides": {
                "sensor.hoymiles_hit_overview_battery_soc": state("96")
            }
        },
    }
    for label, arguments in blocked_cases.items():
        blocked = run(**arguments)
        assert not blocked._delivered_power_ratios, label
        assert blocked._charge_power_feedback_last_ratio is None, label

    # The consumer deduplicates the exact physical evidence cohort.
    valid.observe_supervisor_accounting_feedback(
        delivered_power_feedback_w=4000.0,
        evidence_fingerprint="a" * 64,
        observed_at=now,
        transaction_started_at=now - timedelta(minutes=3),
    )
    assert list(valid._delivered_power_ratios) == [0.8]

    ledger = _load_supervisor_ledger_module()
    intent = ledger.ExecutionIntent(
        policy_id=ledger.PolicyId.TARIFF,
        requested_action=ledger.RequestedAction.TARIFF_BATTERY_CHARGE,
        active=True,
        owner_kind=ledger.OwnerKind.TARIFF,
        active_action=ledger.TariffActiveAction.BATTERY_CHARGE,
        intent_fingerprint="b" * 64,
    )

    def ledger_entry(
        grid_to_battery_power_w: Any,
        grid_power_w: Any,
    ) -> Any:
        def sample(value: Any, source: str) -> Any:
            return ledger.EvidenceSample(
                value=value,
                reported_at=now,
                source=source,
            )

        return ledger.build_execution_ledger_entry(
            intent,
            ledger.PhysicalExecutionEvidence(
                observed_at=now,
                mode_readback=sample(
                    ledger.PhysicalMode.GRID_CHARGE,
                    "sensor.physical_ems_mode",
                ),
                readback_generation=sample(
                    41,
                    "sensor.physical_fc03_generation",
                ),
                readback_confirmed=True,
                grid_to_battery_power_w=sample(
                    grid_to_battery_power_w,
                    "sensor.physical_grid_to_battery",
                ),
                grid_power_w=sample(
                    grid_power_w,
                    "sensor.physical_grid_power",
                ),
            ),
        )

    qualified = ledger_entry(5000.0, -3000.0)
    assert qualified.attributed_power_w == 3000.0
    assert ledger.attributed_power_for_feedback(qualified) == 3000.0

    # Positive battery charge/PV without physical import and grid import not
    # entering the battery both fail closed; neither aggregate is a fallback.
    for label, entry in (
        ("PV-only battery charging", ledger_entry(5000.0, 0.0)),
        ("grid import outside battery", ledger_entry(0.0, -5000.0)),
        ("missing physical grid-to-battery", ledger_entry(None, -5000.0)),
    ):
        assert entry.attributed_power_w == 0.0, label
        assert ledger.attributed_power_for_feedback(entry) == 0.0, label

    # Old accounting epoch/contract and forged whole-battery totals can never
    # cross the accounting-v2 projection into feedback v3.
    for label, entry in (
        ("legacy accounting epoch v1", replace(qualified, accounting_epoch=1)),
        (
            "legacy accounting contract v1",
            replace(
                qualified,
                accounting_contract_id="ems.physical-grid-to-battery.v1",
            ),
        ),
        (
            "forged whole-battery power",
            replace(qualified, attributed_power_w=9000.0),
        ),
    ):
        assert ledger.attributed_power_for_feedback(entry) == 0.0, label


def _assert_rce_live_telemetry_is_five_minute_coalesced() -> None:
    """Fast telemetry and RCE-owned writes must not cancel their own start."""

    path = COMPONENT / "rce_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    watched = _literal_string_set(tree, "WATCHED_ENTITIES")
    coalesced = _literal_string_set(
        tree,
        "RCE_FIVE_MINUTE_COALESCED_ENTITIES",
    )

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
        "Sampled RCE inputs were removed from in-flight stale-result checks"
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
    added = _method(class_node, "async_added_to_hass")
    full_cadence = [
        node
        for node in ast.walk(added)
        if isinstance(node, ast.Name) and node.id == "RCE_FULL_OPTIMIZER_INTERVAL"
    ]
    assert len(full_cadence) == 1, "RCE full optimizer lost its bounded timer"


def _assert_rce_inflight_fingerprint_rejects_consumed_drift() -> None:
    """A periodic trigger never permits a stale in-flight RCE result."""

    path = COMPONENT / "rce_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    method = deepcopy(
        _method(
            _class_node(tree, "HoymilesRCEOptimizerSensor"),
            "_current_input_fingerprint",
        )
    )
    method.decorator_list = []
    probe_class = ast.ClassDef(
        name="Probe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[method],
    )
    namespace: dict[str, Any] = {
        "Any": Any,
        "CONF_SOURCE_DEVICE_ID": "source_device_id",
        "CONF_RESOLVED_SOURCE_DEVICE_ID": "resolved_source_device_id",
        "dt_util": SimpleNamespace(now=lambda: datetime(2026, 9, 26, tzinfo=timezone.utc)),
        "_RCE_REVALIDATED_NUMERIC_INPUTS": eval(compile(ast.Expression(next(
            node.value for node in tree.body if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "_RCE_REVALIDATED_NUMERIC_INPUTS"
                    for target in node.targets)
        )), "<rce-revalidation-inputs>", "eval"), {"LOAD_PHASE_ENERGY_ENTITIES": ()}),
        "WATCHED_ENTITIES": frozenset(
            {
                "input_number.critical",
                "sensor.fast_power",
                "sensor.hoymiles_hit_tariff_charge_plan",
            }
        ),
        "TARIFF_PRICE_BROKER_ATTRIBUTES": (),
        "RCE_GCF_OPTIMIZER_ENTITIES": frozenset(
            {
                "sensor.gcf_enable_raw",
                "sensor.gcf_limit_raw",
            }
        ),
        "optimizer_input_fingerprint": lambda hass, entity_ids, **_kwargs: tuple(
            (entity_id, hass.get(entity_id)) for entity_id in sorted(entity_ids)
        ),
        "_shared_optimizer_signature": lambda runtime: (
            runtime["schema"],
            runtime["entry"],
            runtime["revision"],
        ),
        "_live_forecast_gcf_optimizer_signature": (
            lambda _hass, runtime: runtime["gcf"]
        ),
    }
    report_helper = deepcopy(next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_rce_report_fingerprint"
    ))
    module = ast.fix_missing_locations(
        ast.Module(body=[
            ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0),
            report_helper, probe_class,
        ], type_ignores=[])
    )
    exec(compile(module, "<rce-inflight-fingerprint-probe>", "exec"), namespace)
    probe = namespace["Probe"]()
    probe._ev_filter = lambda: SimpleNamespace(configure=lambda: (SimpleNamespace(enabled=False),))
    probe.hass = {
        "input_number.critical": "stable",
        "sensor.fast_power": "1.0",
        "sensor.dynamic_forecast": "10.0",
        "sensor.tariff_exact": "1.00",
        "sensor.gcf_enable_raw": "1",
        "sensor.gcf_limit_raw": "0",
    }
    probe._runtime = {
        "schema": 1,
        "entry": "entry-a",
        "revision": 10,
        "gcf": (
            True,
            "fixed_zero_export",
            None,
            0.8,
            1.0,
            0.0,
            100,
        ),
    }
    probe._tariff_plan_source = SimpleNamespace(entity_id="sensor.tariff_exact")
    probe._configured_forecast_source_ids = lambda: frozenset(
        {"sensor.dynamic_forecast"}
    )

    captured = probe._current_input_fingerprint()
    assert probe._current_input_fingerprint() == captured, (
        "Stable RCE snapshot did not retain publication authority"
    )
    probe.hass["sensor.fast_power"] = "2.0"
    assert probe._current_input_fingerprint() != captured, (
        "In-flight SOC/PV/load/BMS drift no longer rejects stale RCE work"
    )
    probe.hass["sensor.fast_power"] = "1.0"
    probe.hass["sensor.dynamic_forecast"] = "11.0"
    assert probe._current_input_fingerprint() != captured, (
        "Dynamic forecast drift no longer rejects stale RCE work"
    )
    probe.hass["sensor.dynamic_forecast"] = "10.0"
    probe._ev_filter = lambda: SimpleNamespace(configure=lambda: (
        SimpleNamespace(enabled=True, valid=True, entity_id="sensor.ev_power"),))
    probe.hass["sensor.ev_power"] = "11000"
    ev_captured = probe._current_input_fingerprint()
    probe.hass["sensor.ev_power"] = "0"
    assert probe._current_input_fingerprint() != ev_captured, (
        "Selected EV power drift can still certify an old household forecast"
    )
    probe._ev_filter = lambda: SimpleNamespace(configure=lambda: (SimpleNamespace(enabled=False),))
    probe.hass["sensor.gcf_limit_raw"] = "50"
    assert probe._current_input_fingerprint() != captured, (
        "Raw RCE execution GCF drift can still certify stale work"
    )
    probe.hass["sensor.gcf_limit_raw"] = "0"
    probe.hass["sensor.tariff_exact"] = "1.10"
    assert probe._current_input_fingerprint() != captured, (
        "Exact counterpart tariff drift no longer rejects stale RCE work"
    )
    probe.hass["sensor.tariff_exact"] = "1.00"
    probe._runtime["revision"] = 11
    assert probe._current_input_fingerprint() != captured, (
        "Shared EMS revision drift no longer rejects stale RCE work"
    )
    probe._runtime["revision"] = 10
    probe.hass["input_number.critical"] = "changed"
    assert probe._current_input_fingerprint() != captured, (
        "Immediate RCE setting change no longer rejects stale work"
    )
    probe.hass["input_number.critical"] = "stable"
    probe._runtime["gcf"] = (
        False,
        "conservative",
        "stale",
        0.65,
        1.0,
        50.0,
        101,
    )
    assert probe._current_input_fingerprint() != captured, (
        "Coherent RCE GCF cohort change no longer rejects stale work"
    )


async def _assert_rce_stale_result_retry_is_bounded() -> None:
    """Three stale RCE solves queue one retry that cannot re-arm itself."""

    path = COMPONENT / "rce_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    class_node = _class_node(tree, "HoymilesRCEOptimizerSensor")
    wrapper = _method(class_node, "_recalculate_and_write")
    assert isinstance(wrapper, ast.AsyncFunctionDef)
    probe_type = _compile_probe_method(wrapper)
    probe = probe_type()
    probe._optimizer_lock = asyncio.Lock()
    probe.native_value = "retained"
    probe._attributes = {}
    probe._full_plan_solver_calls = 0
    probe._last_full_plan_at = None
    probe._full_plan_trigger = "test"
    probe._recalculate_cancel = None
    probe._shared_inputs_dirty = False
    probe._full_plan_rejected_for_input_drift = False
    attempts = 0
    scheduled_retries: list[None] = []

    async def reject_for_input_drift() -> bool:
        nonlocal attempts
        attempts += 1
        probe._full_plan_rejected_for_input_drift = True
        return False

    probe._recalculate_locked = reject_for_input_drift
    probe._mark_result_current = lambda: None
    probe._cancel_stale_result_retry = lambda: None
    probe._schedule_stale_result_retry = lambda: scheduled_retries.append(None)
    probe.async_write_ha_state = lambda: None
    probe._publish_timeline_result = lambda: None
    probe._schedule_available_required_input_recovery = lambda: None

    await probe._recalculate_and_write()
    assert attempts == 3
    assert scheduled_retries == [None], (
        "Three stale RCE results did not queue exactly one deferred retry"
    )

    await probe._recalculate_and_write(allow_deferred_retry=False)
    assert attempts == 6
    assert scheduled_retries == [None], (
        "The deferred RCE retry can recursively re-arm itself"
    )

    retry_callback = _method(class_node, "_async_stale_result_retry")
    retry_calls = _awaited_self_call(retry_callback, "_recalculate_and_write")
    assert len(retry_calls) == 1
    retry_call = retry_calls[0].value
    assert isinstance(retry_call, ast.Call)
    deferred_keywords = {
        keyword.arg: keyword.value
        for keyword in retry_call.keywords
        if keyword.arg is not None
    }
    assert "allow_deferred_retry" in deferred_keywords
    assert ast.literal_eval(deferred_keywords["allow_deferred_retry"]) is False


def _assert_shared_optimizer_signature_tracks_exact_revision() -> None:
    """Unknown broker contracts retain the strict immutable revision guard."""

    path = COMPONENT / "rce_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = deepcopy(
        next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_shared_optimizer_signature"
        )
    )
    function.decorator_list = []
    function.returns = None
    for argument in (*function.args.posonlyargs, *function.args.args):
        argument.annotation = None
    namespace: dict[str, Any] = {
        "_shared_inputs_snapshot": lambda runtime: runtime.get("snapshot"),
        "is_dataclass": is_dataclass,
    }
    module = ast.fix_missing_locations(
        ast.Module(body=[function], type_ignores=[])
    )
    exec(compile(module, "<shared-signature-probe>", "exec"), namespace)
    signature = namespace["_shared_optimizer_signature"]
    snapshot = SimpleNamespace(
        schema_version=1,
        config_entry_id="entry-a",
        revision=10,
    )
    runtime = {"snapshot": snapshot}
    captured = signature(runtime)
    assert signature(runtime) == captured
    snapshot.revision = 11
    assert signature(runtime) != captured, (
        "Shared physical revision no longer rejects an in-flight result"
    )
    snapshot.revision = 10
    snapshot.config_entry_id = "entry-b"
    assert signature(runtime) != captured, (
        "Shared broker identity change no longer invalidates stale work"
    )


def _assert_gcf_signature_tracks_physical_generation() -> None:
    """The coherent GCF projection retains its physical FC03 generation."""

    path = COMPONENT / "rce_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    functions = [
        deepcopy(node)
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name
        in {
            "_forecast_gcf_optimizer_signature",
            "_forecast_gcf_publication_signature",
        }
    ]
    assert len(functions) == 2
    for function in functions:
        function.decorator_list = []
        function.returns = None
        for argument in (*function.args.posonlyargs, *function.args.args):
            argument.annotation = None
    namespace: dict[str, Any] = {
        "_forecast_learning_policy_signature": lambda _policy: (
            True,
            "adaptive",
            None,
            None,
        )
    }
    module = ast.fix_missing_locations(ast.Module(body=functions, type_ignores=[]))
    exec(compile(module, "<gcf-signature-probe>", "exec"), namespace)
    signature = namespace["_forecast_gcf_publication_signature"]
    diagnostics = {
        "forecast_learning_gcf_enable_code": 1.0,
        "forecast_learning_gcf_export_limit_percent": 50.0,
        "forecast_learning_gcf_generation": 100,
    }
    captured = signature(object(), diagnostics)
    diagnostics["forecast_learning_gcf_generation"] = 101
    assert signature(object(), diagnostics) != captured, (
        "A newer physical GCF cohort can still certify old solver work"
    )


def _assert_tariff_live_telemetry_is_five_minute_coalesced() -> None:
    """Live planning telemetry must remain stable between periodic runs."""

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

    watched_assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "WATCHED_TARIFF_ENTITIES"
            for target in node.targets
        )
    )
    coalesced_assignment = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "TARIFF_FIVE_MINUTE_COALESCED_ENTITIES"
            for target in node.targets
        )
    )
    critical_helper_name = "NEW_BATTERY_TO_HOME_EFFICIENCY_HELPER"
    assert any(
        isinstance(node, ast.Name) and node.id == critical_helper_name
        for node in ast.walk(watched_assignment)
    )
    assert not any(
        isinstance(node, ast.Name) and node.id == critical_helper_name
        for node in ast.walk(coalesced_assignment)
    ), (
        "Shared battery-to-home efficiency no longer invalidates tariff immediately"
    )
    forecast_helper_name = "FORECAST_ENTITY_HELPERS"
    assert any(
        isinstance(node, ast.Name) and node.id == forecast_helper_name
        for node in ast.walk(watched_assignment)
    )
    assert not any(
        isinstance(node, ast.Name) and node.id == forecast_helper_name
        for node in ast.walk(coalesced_assignment)
    ), "Forecast source selectors are settings and must invalidate immediately"

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
    immediate_fingerprint = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_tariff_immediate_input_fingerprint"
    )
    immediate_fingerprint_source = ast.get_source_segment(
        path.read_text(encoding="utf-8"),
        immediate_fingerprint,
    )
    assert immediate_fingerprint_source is not None
    assert "include_last_updated=False" in immediate_fingerprint_source, (
        "Publication guard can still flap on unchanged report cadence"
    )

    fingerprint = _method(class_node, "_current_input_fingerprint")
    fingerprint_names = {
        node.id for node in ast.walk(fingerprint) if isinstance(node, ast.Name)
    }
    assert "TARIFF_EVENT_DRIVEN_ENTITIES" in fingerprint_names, (
        "Immediate tariff settings were removed from in-flight stale-result checks"
    )
    assert "WATCHED_TARIFF_ENTITIES" not in fingerprint_names, (
        "Coalesced telemetry can still reject every slow tariff solve"
    )
    assert "_shared_optimizer_signature" not in fingerprint_names, (
        "Ordinary Shared EMS cadence can still reject every slow tariff solve"
    )
    assert "_tariff_shared_critical_signature" in fingerprint_names, (
        "Shared control identity/freshness changes lost in-flight protection"
    )
    assert "_live_forecast_gcf_optimizer_signature" in fingerprint_names, (
        "Coherent GCF cohort lost in-flight publication protection"
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


def _assert_tariff_inflight_fingerprint_rejects_consumed_drift() -> None:
    """A periodic trigger never permits a stale in-flight tariff result."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    method = deepcopy(
        _method(
            _class_node(tree, "HoymilesTariffOptimizerSensor"),
            "_current_input_fingerprint",
        )
    )
    method.decorator_list = []
    probe_class = ast.ClassDef(
        name="Probe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[method],
    )
    revision_module = _load_revision_module()
    broker_attributes = ("load_profile",)

    def broker_fingerprint(
        state: Any,
        *,
        include_last_reported: bool,
    ) -> tuple[Any, ...]:
        return (
            bool(
                state is not None
                and state.state not in {"unknown", "unavailable", "none"}
            ),
            revision_module.state_fingerprint(
                state,
                attributes=broker_attributes,
                include_state=False,
                include_last_updated=include_last_reported,
            ),
        )

    namespace: dict[str, Any] = {
        "TARIFF_EVENT_DRIVEN_ENTITIES": frozenset(
            {
                "input_number.critical",
                "sensor.hoymiles_hit_rce_optimized_plan",
            }
        ),
        "RCE_LOAD_BROKER_ATTRIBUTES": broker_attributes,
        "optimizer_input_fingerprint": revision_module.optimizer_input_fingerprint,
        "_rce_load_broker_fingerprint": broker_fingerprint,
        "_shared_inputs_snapshot": lambda runtime: runtime["snapshot"],
        "_tariff_shared_critical_signature": lambda runtime: runtime["critical"],
        "_live_forecast_gcf_optimizer_signature": (
            lambda _hass, runtime: runtime["gcf"]
        ),
    }
    module = ast.fix_missing_locations(
        ast.Module(body=[probe_class], type_ignores=[])
    )
    exec(compile(module, "<tariff-inflight-fingerprint-probe>", "exec"), namespace)
    probe = namespace["Probe"]()
    reported = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)

    def state(
        value: str,
        *,
        attributes: dict[str, Any] | None = None,
        last_reported: datetime = reported,
    ) -> Any:
        return SimpleNamespace(
            state=value,
            attributes=attributes or {},
            last_reported=last_reported,
            last_updated=last_reported,
        )

    probe.hass = SimpleNamespace(
        states={
            "input_number.critical": state("stable"),
            "sensor.fast_power": state("1.0"),
            "sensor.dynamic_forecast": state("10.0"),
            "sensor.rce_exact": state(
                "ready",
                attributes={"load_profile": "load-v1"},
            ),
        }
    )
    probe._runtime = {
        "revision": 10,
        "critical": ("ready",),
        "snapshot": SimpleNamespace(load=object()),
        "gcf": (True, "fixed_zero_export", None, 0.8, 1.0, 0.0, 100),
    }
    probe._dynamic_forecast_entities = frozenset({"sensor.dynamic_forecast"})
    probe._rce_plan_source = SimpleNamespace(entity_id="sensor.rce_exact")
    probe._effective_charge_power_factor = 1.0
    probe._effective_charge_power_source = "configured"
    probe._configured_forecast_source_ids = lambda: frozenset(
        {"sensor.dynamic_forecast"}
    )

    captured = probe._current_input_fingerprint()
    assert probe._current_input_fingerprint() == captured, (
        "Stable tariff snapshot did not retain publication authority"
    )
    probe.hass.states["sensor.fast_power"] = state("2.0")
    assert probe._current_input_fingerprint() == captured, (
        "Coalesced live power still rejects every slow tariff solve"
    )
    probe.hass.states["sensor.fast_power"] = state("1.0")
    probe.hass.states["sensor.dynamic_forecast"] = state("11.0")
    assert probe._current_input_fingerprint() == captured, (
        "Coalesced forecast churn still rejects every slow tariff solve"
    )
    probe.hass.states["sensor.dynamic_forecast"] = state("10.0")
    # Shared EMS is authoritative, so the legacy RCE counterpart is genuinely
    # not consumed in this mode.
    probe.hass.states["sensor.rce_exact"] = state(
        "ready",
        attributes={"load_profile": "load-v2"},
    )
    assert probe._current_input_fingerprint() == captured
    probe.hass.states["sensor.rce_exact"] = state(
        "ready",
        attributes={"load_profile": "load-v1"},
    )
    probe._runtime["revision"] = 11
    assert probe._current_input_fingerprint() == captured, (
        "Ordinary Shared EMS cadence still rejects every slow tariff solve"
    )
    probe._runtime["revision"] = 10
    probe.hass.states["input_number.critical"] = state("changed")
    assert probe._current_input_fingerprint() != captured, (
        "Immediate tariff setting change no longer rejects stale work"
    )
    probe.hass.states["input_number.critical"] = state("stable")
    probe._runtime["critical"] = ("not_ready",)
    assert probe._current_input_fingerprint() != captured, (
        "Shared critical change no longer rejects stale work"
    )
    probe._runtime["critical"] = ("ready",)
    probe._runtime["gcf"] = (
        False,
        "conservative",
        "stale",
        0.65,
        1.0,
        50.0,
        101,
    )
    assert probe._current_input_fingerprint() != captured, (
        "Coherent GCF cohort change no longer rejects stale work"
    )

    # Without Shared EMS LOAD, the exact same-entry counterpart projection is
    # a consumed input and must reject a result calculated from older history.
    probe._runtime["gcf"] = captured[-2][1]
    probe._runtime["snapshot"] = None
    legacy_captured = probe._current_input_fingerprint()
    probe.hass.states["sensor.rce_exact"] = state(
        "ready",
        attributes={"load_profile": "load-v2"},
    )
    assert probe._current_input_fingerprint() != legacy_captured, (
        "Exact legacy RCE LOAD broker drift no longer rejects tariff work"
    )
    probe.hass.states["sensor.rce_exact"] = state(
        "unavailable",
        attributes={"load_profile": "load-v1"},
    )
    assert probe._current_input_fingerprint() != legacy_captured, (
        "Legacy RCE LOAD availability loss can still certify stale work"
    )
    probe.hass.states["sensor.rce_exact"] = state(
        "ready",
        attributes={"load_profile": "load-v1"},
        last_reported=reported + timedelta(seconds=1),
    )
    assert probe._current_input_fingerprint() != legacy_captured, (
        "Legacy RCE LOAD provenance refresh is absent from publication guard"
    )

    probe._runtime["snapshot"] = SimpleNamespace(load=object())
    feedback_captured = probe._current_input_fingerprint()
    probe._effective_charge_power_factor = 0.9
    assert probe._current_input_fingerprint() != feedback_captured, (
        "In-flight charge-power feedback drift can still certify stale tariff work"
    )


def _assert_tariff_legacy_load_broker_fingerprint_contract() -> None:
    """Availability is immediate; report provenance guards only in-flight work."""

    path = COMPONENT / "tariff_sensor.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    helper = deepcopy(
        next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_rce_load_broker_fingerprint"
        )
    )
    helper.decorator_list = []
    helper.returns = None
    for argument in (
        *helper.args.posonlyargs,
        *helper.args.args,
        *helper.args.kwonlyargs,
    ):
        argument.annotation = None
    namespace: dict[str, Any] = {
        "STATE_UNKNOWN": "unknown",
        "STATE_UNAVAILABLE": "unavailable",
        "RCE_LOAD_BROKER_ATTRIBUTES": ("load_profile",),
        "state_fingerprint": _load_revision_module().state_fingerprint,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
            "<tariff-legacy-load-broker-fingerprint>",
            "exec",
        ),
        namespace,
    )
    fingerprint = namespace["_rce_load_broker_fingerprint"]
    first_report = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)

    def state(value: str, load: str, reported: datetime) -> Any:
        return SimpleNamespace(
            state=value,
            attributes={"load_profile": load},
            last_reported=reported,
            last_updated=reported,
        )

    ready = state("ready", "v1", first_report)
    same_facts_new_report = state(
        "waiting",
        "v1",
        first_report + timedelta(seconds=1),
    )
    unavailable = state("unavailable", "v1", first_report)
    changed_load = state("ready", "v2", first_report)
    assert fingerprint(
        ready,
        include_last_reported=False,
    ) == fingerprint(same_facts_new_report, include_last_reported=False), (
        "Legacy broker report/status churn can still arm an extra tariff run"
    )
    assert fingerprint(
        ready,
        include_last_reported=True,
    ) != fingerprint(same_facts_new_report, include_last_reported=True), (
        "In-flight legacy broker provenance drift is not guarded"
    )
    assert fingerprint(ready, include_last_reported=False) != fingerprint(
        unavailable,
        include_last_reported=False,
    ), "Legacy broker availability loss does not invalidate immediately"
    assert fingerprint(ready, include_last_reported=False) != fingerprint(
        changed_load,
        include_last_reported=False,
    ), "Legacy broker LOAD-model changes do not invalidate immediately"

    invalidator = _method(
        _class_node(tree, "HoymilesTariffOptimizerSensor"),
        "_invalidate_input_event",
    )
    invalidator_source = ast.get_source_segment(source, invalidator)
    assert invalidator_source is not None
    assert invalidator_source.count("_rce_load_broker_fingerprint(") == 2
    assert "self._input_revision.invalidate()" in invalidator_source


def _assert_tariff_immediate_fingerprint_semantics() -> None:
    """Ignore report cadence but retain value, attributes and availability."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    function = deepcopy(
        next(
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef)
            and node.name == "_tariff_immediate_input_fingerprint"
        )
    )
    function.decorator_list = []
    function.returns = None
    for argument in (*function.args.posonlyargs, *function.args.args):
        argument.annotation = None
    module = ast.fix_missing_locations(
        ast.Module(body=[function], type_ignores=[])
    )
    namespace: dict[str, Any] = {
        "state_fingerprint": _load_revision_module().state_fingerprint,
    }
    exec(compile(module, "<tariff-immediate-fingerprint-probe>", "exec"), namespace)
    fingerprint = namespace["_tariff_immediate_input_fingerprint"]

    states: dict[str, Any] = {
        "input_number.critical": SimpleNamespace(
            state="10",
            attributes={"unit_of_measurement": "A"},
            last_reported="t1",
            last_updated="t1",
        )
    }
    hass = SimpleNamespace(states=SimpleNamespace(get=states.get))
    captured = fingerprint(hass, {"input_number.critical"})

    states["input_number.critical"] = SimpleNamespace(
        state="10",
        attributes={"unit_of_measurement": "A"},
        last_reported="t2",
        last_updated="t2",
    )
    assert fingerprint(hass, {"input_number.critical"}) == captured

    states["input_number.critical"].state = "11"
    assert fingerprint(hass, {"input_number.critical"}) != captured
    states["input_number.critical"].state = "10"
    states["input_number.critical"].attributes = {"unit_of_measurement": "kA"}
    assert fingerprint(hass, {"input_number.critical"}) != captured
    states.pop("input_number.critical")
    assert fingerprint(hass, {"input_number.critical"}) != captured


def _assert_tariff_unreported_fingerprint_drift_increments_revision() -> None:
    """A guard-only drift must become a real optimizer input revision."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    method = deepcopy(
        _method(
            _class_node(tree, "HoymilesTariffOptimizerSensor"),
            "_reject_stale_executor_result",
        )
    )
    method.decorator_list = []
    probe_class = ast.ClassDef(
        name="Probe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=[method],
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[probe_class], type_ignores=[])
    )
    namespace: dict[str, Any] = {}
    exec(compile(module, "<tariff-stale-result-probe>", "exec"), namespace)
    probe = namespace["Probe"]()
    current_revision = 7
    current_fingerprint: tuple[Any, ...] = ("stable",)
    invalidations: list[None] = []
    pending_marks: list[None] = []

    class Revision:
        def is_current(self, captured: int) -> bool:
            return captured == current_revision

    probe._input_revision = Revision()
    probe._current_input_fingerprint = lambda: current_fingerprint
    probe._full_plan_rejected_for_input_drift = False

    def invalidate() -> None:
        nonlocal current_revision
        current_revision += 1
        invalidations.append(None)

    probe._invalidate_internal_inputs = invalidate
    probe._mark_recalculation_pending = lambda: pending_marks.append(None)

    assert not probe._reject_stale_executor_result(7, ("stable",))
    assert not probe._full_plan_rejected_for_input_drift
    current_fingerprint = ("critical-drift",)
    assert probe._reject_stale_executor_result(7, ("stable",))
    assert probe._full_plan_rejected_for_input_drift
    assert current_revision == 8
    assert invalidations == [None]
    assert pending_marks == []

    assert probe._reject_stale_executor_result(7, ("critical-drift",))
    assert current_revision == 8
    assert invalidations == [None]
    assert pending_marks == [None]


async def _assert_tariff_commit_preserves_shared_dirty_timer() -> None:
    """A Shared revision arriving in-flight must retain its queued sample."""

    path = COMPONENT / "tariff_sensor.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    wrapper = _method(
        _class_node(tree, "HoymilesTariffOptimizerSensor"),
        "_recalculate_and_write",
    )
    assert isinstance(wrapper, ast.AsyncFunctionDef)
    probe_type = _compile_probe_method(wrapper)
    probe = probe_type()
    probe._optimizer_lock = asyncio.Lock()
    probe.native_value = "stable"
    probe._attributes = {}
    probe._full_plan_solver_calls = 0
    probe._last_full_plan_at = None
    probe._full_plan_trigger = "test"
    probe._shared_inputs_dirty = False
    cancellations: list[None] = []
    stale_retry_cancellations: list[None] = []
    current_marks: list[None] = []
    publications: list[None] = []

    def cancel() -> None:
        cancellations.append(None)

    async def commit_with_shared_revision() -> bool:
        probe._shared_inputs_dirty = True
        probe._recalculate_cancel = cancel
        return True

    probe._recalculate_locked = commit_with_shared_revision
    probe._mark_result_current = lambda: current_marks.append(None)
    probe._cancel_stale_result_retry = (
        lambda: stale_retry_cancellations.append(None)
    )
    probe._schedule_stale_result_retry = lambda: None
    probe._full_plan_rejected_for_input_drift = False
    probe.async_write_ha_state = lambda: None
    probe._publish_timeline_result = lambda: publications.append(None)
    probe._schedule_available_required_input_recovery = lambda: None
    await probe._recalculate_and_write()
    assert cancellations == []
    assert probe._recalculate_cancel is cancel
    assert probe._shared_inputs_dirty
    assert current_marks == [None]
    assert publications == [None]
    assert stale_retry_cancellations == [None]
    assert probe._full_plan_solver_calls == 0
    assert probe._attributes["last_full_plan_at"] is None
    assert probe._attributes["last_full_plan_trigger"] == "test"

    async def stable_commit() -> bool:
        return True

    probe._shared_inputs_dirty = False
    probe._recalculate_cancel = cancel
    probe._recalculate_locked = stable_commit
    await probe._recalculate_and_write()
    assert cancellations == [None]
    assert stale_retry_cancellations == [None, None]
    assert probe._recalculate_cancel is None
    assert probe._full_plan_solver_calls == 0
    assert probe._attributes["last_full_plan_at"] is None


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
    namespace: dict[str, Any] = {
        "MAX_IMMEDIATE_RECALCULATIONS": 3,
        "dt_util": SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc)),
    }
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


def _assert_full_plan_diagnostics_contract(
    path: Path,
    class_node: ast.ClassDef,
    recalculate_and_write: ast.AsyncFunctionDef,
    locked: ast.AsyncFunctionDef,
    executor_await: ast.Await,
) -> None:
    """Bind solver telemetry to dispatch and accepted-result boundaries."""

    counter_updates = [
        node
        for node in ast.walk(class_node)
        if isinstance(node, ast.AugAssign)
        and _is_self_attribute(node.target, "_full_plan_solver_calls")
    ]
    assert len(counter_updates) == 1, (
        f"{path.name} must count exactly at its one full-solver dispatch"
    )
    counter_update = counter_updates[0]
    assert isinstance(counter_update.op, ast.Add)
    assert isinstance(counter_update.value, ast.Constant)
    assert counter_update.value.value == 1
    assert counter_update in set(ast.walk(locked)), (
        f"{path.name} counts wrapper commits instead of solver dispatches"
    )
    assert counter_update.lineno < executor_await.lineno, (
        f"{path.name} cannot count a solver that raises in the executor"
    )

    timestamp_updates = [
        node
        for node in ast.walk(locked)
        if isinstance(node, ast.Assign)
        and any(
            _is_self_attribute(target, "_last_full_plan_at")
            for target in node.targets
        )
    ]
    assert len(timestamp_updates) == 1, (
        f"{path.name} must timestamp only one accepted-result boundary"
    )
    timestamp_update = timestamp_updates[0]
    assert timestamp_update.lineno > executor_await.lineno

    locked_parents = {
        child: parent
        for parent in ast.walk(locked)
        for child in ast.iter_child_nodes(parent)
    }

    def has_ancestor(node: ast.AST, kind: type[ast.AST]) -> bool:
        parent = locked_parents.get(node)
        while parent is not None and parent is not locked:
            if isinstance(parent, kind):
                return True
            parent = locked_parents.get(parent)
        return False

    assert not has_ancestor(timestamp_update, ast.ExceptHandler), (
        f"{path.name} timestamps optimizer errors as accepted plans"
    )
    missing_returns = [
        node
        for node in ast.walk(locked)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value is True
        and node.lineno < counter_update.lineno
    ]
    assert missing_returns, (
        f"{path.name} lacks a pre-dispatch missing-input completion path"
    )
    stale_returns = [
        node
        for node in ast.walk(locked)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Constant)
        and node.value.value is False
        and executor_await.lineno < node.lineno < timestamp_update.lineno
    ]
    assert stale_returns, (
        f"{path.name} timestamps before rejecting a stale executor result"
    )
    completed_projections = [
        node
        for node in ast.walk(locked)
        if isinstance(node, ast.Assign)
        and any(
            _is_self_attribute(target, "_attributes") for target in node.targets
        )
        and executor_await.lineno < node.lineno < timestamp_update.lineno
        and not has_ancestor(node, ast.ExceptHandler)
    ]
    assert completed_projections, (
        f"{path.name} timestamps before completing its accepted projection"
    )

    wrapper_counter_updates = [
        node
        for node in ast.walk(recalculate_and_write)
        if isinstance(node, ast.AugAssign)
        and _is_self_attribute(node.target, "_full_plan_solver_calls")
    ]
    wrapper_timestamp_updates = [
        node
        for node in ast.walk(recalculate_and_write)
        if isinstance(node, ast.Assign)
        and any(
            _is_self_attribute(target, "_last_full_plan_at")
            for target in node.targets
        )
    ]
    assert not wrapper_counter_updates, (
        f"{path.name} still counts missing/error wrapper commits as solver calls"
    )
    assert not wrapper_timestamp_updates, (
        f"{path.name} still timestamps missing/error wrapper commits"
    )

    wrapper_parents = {
        child: parent
        for parent in ast.walk(recalculate_and_write)
        for child in ast.iter_child_nodes(parent)
    }
    telemetry_projections: list[ast.Assign] = []
    for node in ast.walk(recalculate_and_write):
        if not isinstance(node, ast.Assign) or not any(
            _is_self_attribute(target, "_attributes") for target in node.targets
        ):
            continue
        if not isinstance(node.value, ast.Dict):
            continue
        keys = {
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if {"full_plan_solver_calls", "last_full_plan_at"}.issubset(keys):
            telemetry_projections.append(node)
    assert len(telemetry_projections) == 1
    projection = telemetry_projections[0]
    parent = wrapper_parents.get(projection)
    while parent is not None and parent is not recalculate_and_write:
        assert not isinstance(parent, ast.If), (
            f"{path.name} hides stale/error dispatch telemetry behind commit"
        )
        parent = wrapper_parents.get(parent)


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
    _assert_full_plan_diagnostics_contract(
        path,
        class_node,
        recalculate_and_write,
        locked,
        executor_await,
    )
    has_stale_guard_helper = any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_reject_stale_executor_result"
        for node in class_node.body
    )
    stale_guard = (
        _method(class_node, "_reject_stale_executor_result")
        if has_stale_guard_helper
        else locked
    )
    revision_checks = [
        node
        for node in ast.walk(stale_guard)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "is_current"
        and isinstance(node.func.value, ast.Attribute)
        and _is_self_attribute(node.func.value, "_input_revision")
    ]
    assert revision_checks, f"{path.name} never rejects a stale executor result"
    if has_stale_guard_helper:
        stale_guard_calls = [
            node
            for node in ast.walk(locked)
            if isinstance(node, ast.Call)
            and _is_self_attribute(node.func, "_reject_stale_executor_result")
        ]
        assert len(stale_guard_calls) >= 2
        assert any(
            getattr(node, "lineno", 0) > getattr(executor_await, "lineno", 0)
            for node in stale_guard_calls
        ), f"{path.name} checks revision only before the executor await"
    else:
        assert any(
            getattr(node, "lineno", 0) > getattr(executor_await, "lineno", 0)
            for node in revision_checks
        ), f"{path.name} checks revision only before the executor await"
    fingerprint_roots = (
        (locked, stale_guard)
        if stale_guard is not locked
        else (locked,)
    )
    fingerprint_checks = [
        node
        for guard_node in fingerprint_roots
        for node in ast.walk(guard_node)
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
    _assert_tariff_shared_bootstrap_replaces_pending_timer()
    _assert_tariff_feedback_uses_only_physical_grid_import()
    _assert_rce_live_telemetry_is_five_minute_coalesced()
    _assert_rce_inflight_fingerprint_rejects_consumed_drift()
    await _assert_rce_stale_result_retry_is_bounded()
    _assert_shared_optimizer_signature_tracks_exact_revision()
    _assert_gcf_signature_tracks_physical_generation()
    _assert_tariff_live_telemetry_is_five_minute_coalesced()
    _assert_tariff_inflight_fingerprint_rejects_consumed_drift()
    _assert_tariff_legacy_load_broker_fingerprint_contract()
    _assert_tariff_immediate_fingerprint_semantics()
    _assert_tariff_unreported_fingerprint_drift_increments_revision()
    _assert_tariff_shared_critical_signature_is_cadence_free()
    _assert_timeline_has_no_execution_authority()
    await _assert_tariff_gcf_deadline_is_semantic_and_cancel_safe()
    await _assert_tariff_shared_dirty_is_sampled_on_debounce()
    await _assert_tariff_commit_preserves_shared_dirty_timer()
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
