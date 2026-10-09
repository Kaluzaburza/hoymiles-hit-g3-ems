"""Deterministic I3 cadence and publication-boundary contract tests.

The probes execute selected sensor callbacks from their source AST with small
fakes.  They deliberately avoid importing Home Assistant while still testing
the actual debounce/timer methods that protect the three full optimizers from
live-telemetry churn.
"""

from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import math
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from collections.abc import Mapping


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))
from supervisor_canonical_runtime import canonical_timeline_dependencies
CHECKS = 0


def check(condition: bool, message: str) -> None:
    """Record one focused contract assertion."""

    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


class Event:
    """Minimal subscription annotation and event payload stand-in."""

    @classmethod
    def __class_getitem__(cls, _item: object) -> type["Event"]:
        return cls


def _tree(name: str) -> ast.Module:
    path = COMPONENT / name
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _class(tree: ast.Module, name: str) -> ast.ClassDef:
    candidates = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name
    ]
    check(len(candidates) == 1, f"expected one {name}")
    return candidates[0]


def _method(
    class_node: ast.ClassDef,
    name: str,
) -> ast.FunctionDef | ast.AsyncFunctionDef:
    candidates = [
        node
        for node in class_node.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
    ]
    check(len(candidates) == 1, f"expected one {class_node.name}.{name}")
    return candidates[0]


def _literal_strings(tree: ast.Module, name: str) -> set[str]:
    assignments = [
        node
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and any(
            isinstance(target, ast.Name) and target.id == name
            for target in (node.targets if isinstance(node, ast.Assign) else [node.target])
        )
    ]
    check(len(assignments) == 1, f"expected one {name} assignment")
    def resolve(value: ast.expr | None) -> set[str]:
        if value is None:
            return set()
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and value.args
        ):
            return resolve(value.args[0])
        if isinstance(value, ast.Name):
            nested = [
                node
                for node in tree.body
                if isinstance(node, (ast.Assign, ast.AnnAssign))
                and any(
                    isinstance(target, ast.Name) and target.id == value.id
                    for target in (
                        node.targets if isinstance(node, ast.Assign) else [node.target]
                    )
                )
            ]
            return resolve(nested[0].value) if len(nested) == 1 else set()
        if isinstance(value, ast.BinOp):
            left = resolve(value.left)
            right = resolve(value.right)
            if isinstance(value.op, ast.Sub):
                return left - right
            if isinstance(value.op, (ast.BitOr, ast.Add)):
                return left | right
            return set()
        if isinstance(value, (ast.Set, ast.List, ast.Tuple)):
            values: set[str] = set()
            for item in value.elts:
                if isinstance(item, ast.Constant) and isinstance(item.value, str):
                    values.add(item.value)
                elif isinstance(item, ast.Starred):
                    values.update(resolve(item.value))
            return values
        return set()

    return resolve(assignments[0].value)


def _compile_probe(
    methods: list[ast.FunctionDef | ast.AsyncFunctionDef],
    namespace: dict[str, Any],
) -> type[Any]:
    """Compile exact callback bodies with decorators stripped for a fake."""

    copied = [deepcopy(method) for method in methods]
    for original, method in zip(methods, copied, strict=True):
        method.decorator_list = [
            decorator
            for decorator in original.decorator_list
            if isinstance(decorator, ast.Name) and decorator.id == "staticmethod"
        ]
    probe = ast.ClassDef(
        name="Probe",
        bases=[],
        keywords=[],
        decorator_list=[],
        body=copied,
    )
    module = ast.fix_missing_locations(ast.Module(body=[probe], type_ignores=[]))
    execution_namespace = {
        "canonical_timeline_dependencies": canonical_timeline_dependencies,
        "EV_LOAD_HELPERS": frozenset(),  # optional EV disabled in legacy cadence fixtures
        "Any": Any,
        "Event": Event,
        "EventStateChangedData": Any,
        "Mapping": Mapping,
        "datetime": datetime,
        "callback": lambda function: function,
        **namespace,
    }
    exec(compile(module, "<i3-cadence-probe>", "exec"), execution_namespace)
    return execution_namespace["Probe"]


def _scheduler(records: list[dict[str, Any]]):
    def schedule(_hass: Any, delay: float, callback: Any):
        record = {"delay": delay, "callback": callback, "cancelled": False}
        records.append(record)

        def cancel() -> None:
            record["cancelled"] = True

        return cancel

    return schedule


async def _rce_contract() -> None:
    tree = _tree("rce_sensor.py")
    source = (COMPONENT / "rce_sensor.py").read_text(encoding="utf-8")
    sensor = _class(tree, "HoymilesRCEOptimizerSensor")
    coalesced = _literal_strings(tree, "RCE_FIVE_MINUTE_COALESCED_ENTITIES")
    event_driven = _literal_strings(tree, "RCE_EVENT_DRIVEN_ENTITIES")
    fast = {
        "sensor.hoymiles_hit_overview_battery_soc",
        "sensor.hoymiles_actual_load_power",
        "sensor.hoymiles_hit_overview_pv_total_power",
        "sensor.solcast_pv_forecast_forecast_today",
        "sensor.solcast_pv_forecast_forecast_tomorrow",
    }
    check(fast <= coalesced and fast.isdisjoint(event_driven), "RCE fast telemetry can still enter the immediate solver listener")
    check("RCE_FULL_OPTIMIZER_INTERVAL = timedelta(seconds=120)" in source, "RCE full cadence is not 120 seconds")
    gcf_triggers = _literal_strings(tree, "FORECAST_GCF_POLICY_TRIGGER_ENTITIES")
    check(
        "sensor.hoymiles_hit_gcf_control_readback_generation" not in gcf_triggers,
        "unchanged 20-second GCF generations still wake the RCE optimizer",
    )
    check(
        "async_track_state_report_event" not in source,
        "unchanged GCF state reports still wake the RCE optimizer",
    )
    dynamic_listener = ast.get_source_segment(
        source, _method(sensor, "_refresh_dynamic_forecast_listener")
    ) or ""
    check(
        "self._async_forecast_value_changed" in dynamic_listener
        and "self._async_input_changed" not in dynamic_listener,
        "custom RCE forecast values still enter the immediate solver listener",
    )

    ForecastProbe = _compile_probe(
        [_method(sensor, "_async_forecast_value_changed")],
        {},
    )
    forecast_probe = ForecastProbe()
    forecast_probe._lifecycle_stopped = False
    forecast_probe._shared_inputs_dirty = False
    for _ in range(5):
        forecast_probe._async_forecast_value_changed(SimpleNamespace())
    check(
        forecast_probe._shared_inputs_dirty,
        "RCE did not retain forecast churn for its next bounded snapshot",
    )

    records: list[dict[str, Any]] = []
    Probe = _compile_probe(
        [_method(sensor, "_async_input_changed"), _method(sensor, "_async_timer")],
        {
            "FORECAST_ENTITY_HELPERS": frozenset(),
            "INPUT_RECALCULATION_DELAY_SECONDS": 1.0,
            "async_call_later": _scheduler(records),
        },
    )
    probe = Probe()
    probe.hass = object()
    probe._lifecycle_stopped = False
    probe._recalculate_cancel = None
    probe._shared_inputs_dirty = False
    probe._invalidate_input_event = lambda _event: True
    probe._async_debounced_recalculate = object()
    for _ in range(5):
        probe._async_input_changed(SimpleNamespace(data={"entity_id": "input_number.rce_price"}))
    check(len(records) == 1 and records[0]["delay"] == 1.0, "five RCE form changes did not collapse to one immediate debounce")

    periodic_calls: list[str] = []
    invalidations: list[str] = []

    async def recalculate() -> None:
        periodic_calls.append(probe._full_plan_trigger)

    probe._recalculate_cancel = None
    probe._invalidate_internal_inputs = lambda: invalidations.append("dirty")
    probe._recalculate_and_write = recalculate
    for _ in range(15):
        await probe._async_timer(datetime.now(timezone.utc))
    check(periodic_calls == ["periodic"] * 15, "RCE periodic callback did not produce exactly fifteen bounded runs")
    check(len(invalidations) == 15, "RCE periodic callback invalidated an unexpected number of snapshots")
    check(1 + len(periodic_calls) == 16, "thirty-minute RCE window is not startup plus fifteen 120-second runs")


async def _tariff_contract() -> None:
    tree = _tree("tariff_sensor.py")
    source = (COMPONENT / "tariff_sensor.py").read_text(encoding="utf-8")
    sensor = _class(tree, "HoymilesTariffOptimizerSensor")
    coalesced = _literal_strings(tree, "TARIFF_FIVE_MINUTE_COALESCED_ENTITIES")
    event_driven = _literal_strings(tree, "TARIFF_EVENT_DRIVEN_ENTITIES")
    fast = {
        "sensor.hoymiles_hit_battery_capacity",
        "sensor.hoymiles_hit_overview_battery_soc",
        "sensor.hoymiles_actual_load_power",
        "sensor.hoymiles_hit_overview_pv_total_power",
        "sensor.hoymiles_hit_ems_self_use_soc_readback",
        "sensor.hoymiles_hit_number_of_machines_master_and_slave",
        "sensor.hoymiles_hit_rce_optimized_plan",
    }
    check(fast <= coalesced and fast.isdisjoint(event_driven), "tariff fast telemetry can still enter the generic immediate listener")
    required_recovery_entities = {
        "sensor.hoymiles_hit_battery_capacity",
        "sensor.hoymiles_hit_overview_battery_soc",
        "sensor.hoymiles_hit_ems_self_use_soc_readback",
        "sensor.hoymiles_hit_number_of_machines_master_and_slave",
    }
    check(
        _literal_strings(tree, "TARIFF_REQUIRED_INPUT_RECOVERY_ENTITIES")
        == required_recovery_entities,
        "tariff required-input recovery cohort changed",
    )
    check("TARIFF_FULL_OPTIMIZER_INTERVAL = timedelta(seconds=120)" in source, "tariff full cadence is not 120 seconds")
    check("TARIFF_INPUT_RECALCULATION_DELAY_SECONDS = 5 * 60.0" in source, "tariff GCF policy coalescing delay changed")
    check(
        "TARIFF_STALE_RESULT_RETRY_DELAY_SECONDS = 5.0" in source,
        "tariff stale-result recovery is not bounded to five seconds",
    )
    check(
        "async_track_state_report_event" not in source,
        "unchanged GCF state reports still wake the tariff optimizer",
    )
    dynamic_listener = ast.get_source_segment(
        source, _method(sensor, "_refresh_dynamic_forecast_listener")
    ) or ""
    check(
        "self._async_forecast_value_changed" in dynamic_listener
        and "self._async_input_changed" not in dynamic_listener,
        "custom tariff forecast values still enter the immediate solver listener",
    )

    retry_records: list[dict[str, Any]] = []
    RetryProbe = _compile_probe(
        [
            _method(sensor, "_mark_recalculation_pending"),
            _method(sensor, "_schedule_stale_result_retry"),
            _method(sensor, "_cancel_stale_result_retry"),
            _method(sensor, "_async_stale_result_retry"),
            _method(sensor, "_cancel_delayed_recalculation"),
            _method(sensor, "_recalculate_and_write"),
        ],
        {
            "MAX_IMMEDIATE_RECALCULATIONS": 3,
            "TARIFF_STALE_RESULT_RETRY_DELAY_SECONDS": 5.0,
            "async_call_later": _scheduler(retry_records),
        },
    )
    retry = RetryProbe()
    retry.hass = object()
    retry.native_value = "pending"
    retry._attributes = {
        "result_current": False,
        "recalculation_pending": True,
    }
    retry._optimizer_lock = asyncio.Lock()
    retry._stale_result_retry_cancel = None
    retry._recalculate_cancel = None
    retry._delayed_recalculate_tasks = set()
    retry._required_input_recovery_pending = False
    retry._lifecycle_stopped = False
    retry._shared_inputs_dirty = False
    retry._full_plan_solver_calls = 0
    retry._last_full_plan_at = None
    retry._full_plan_trigger = "periodic"
    retry._full_plan_rejected_for_input_drift = False
    retry._timeline_sensor = None
    retry.async_write_ha_state = lambda: None
    current_marks: list[None] = []
    timeline_publications: list[None] = []
    recovery_checks: list[None] = []
    retry._mark_result_current = lambda: current_marks.append(None)
    retry._publish_timeline_result = lambda: timeline_publications.append(None)
    retry._schedule_available_required_input_recovery = (
        lambda: recovery_checks.append(None)
    )
    attempt_modes: list[str] = []
    attempts = 0

    async def result_for_mode() -> bool:
        nonlocal attempts
        attempts += 1
        mode = attempt_modes.pop(0)
        retry._full_plan_rejected_for_input_drift = mode == "stale"
        return mode == "commit"

    retry._recalculate_locked = result_for_mode
    attempt_modes.extend(("stale", "missing", "stale"))
    await retry._recalculate_and_write()
    check(
        attempts == 3 and not retry_records,
        "mixed tariff failures incorrectly armed stale-result recovery",
    )

    attempt_modes.extend(("stale",) * 3)
    await retry._recalculate_and_write()
    check(
        attempts == 6
        and len(retry_records) == 1
        and retry_records[0]["delay"] == 5.0,
        "three stale tariff solver results did not arm one delayed retry",
    )
    retry._schedule_stale_result_retry()
    check(
        len(retry_records) == 1,
        "tariff stale-result retry is not deduplicated",
    )

    attempt_modes.extend(("stale",) * 3)
    await retry_records[0]["callback"](datetime.now(timezone.utc))
    check(
        attempts == 9
        and len(retry_records) == 1
        and retry._stale_result_retry_cancel is None
        and retry._attributes["last_full_plan_trigger"] == "stale_result_retry",
        "delayed tariff retry re-armed itself or lost its diagnostic trigger",
    )

    for failure_mode in ("missing", "optimizer_error"):
        attempt_modes.extend((failure_mode,) * 3)
        await retry._recalculate_and_write()
    check(
        attempts == 15 and len(retry_records) == 1,
        "tariff missing-data or optimizer-error failures armed a delayed retry",
    )

    retry._schedule_stale_result_retry()
    attempt_modes.append("commit")
    await retry_records[1]["callback"](datetime.now(timezone.utc))
    check(
        attempts == 16
        and current_marks == [None]
        and timeline_publications == [None]
        and recovery_checks == [None]
        and retry._stale_result_retry_cancel is None,
        "successful delayed tariff retry did not publish and clear its handle",
    )

    retry._schedule_stale_result_retry()
    retry._mark_recalculation_pending()
    check(
        retry_records[2]["cancelled"]
        and retry._stale_result_retry_cancel is None,
        "a newer tariff trigger did not supersede the stale-result retry",
    )
    retry._schedule_stale_result_retry()
    retry._cancel_delayed_recalculation()
    check(
        retry_records[3]["cancelled"]
        and retry._stale_result_retry_cancel is None
        and retry._lifecycle_stopped,
        "tariff unload cleanup left a stale-result retry armed",
    )

    ForecastProbe = _compile_probe(
        [_method(sensor, "_async_forecast_value_changed")],
        {},
    )
    forecast_probe = ForecastProbe()
    forecast_probe._lifecycle_stopped = False
    forecast_probe._shared_inputs_dirty = False
    for _ in range(5):
        forecast_probe._async_forecast_value_changed(SimpleNamespace())
    check(
        forecast_probe._shared_inputs_dirty,
        "tariff did not retain forecast churn for its next bounded snapshot",
    )

    records: list[dict[str, Any]] = []
    Probe = _compile_probe(
        [_method(sensor, "_async_input_changed"), _method(sensor, "_async_timer")],
        {
            "FORECAST_ENTITY_HELPERS": frozenset(),
            "TARIFF_IMMEDIATE_RECALCULATION_DELAY_SECONDS": 1.0,
            "async_call_later": _scheduler(records),
        },
    )
    probe = Probe()
    probe.hass = object()
    probe._forecast_accuracy_refreshed_at = object()
    probe._lifecycle_stopped = False
    probe._recalculate_cancel = None
    probe._shared_inputs_dirty = False
    probe._attributes = {}
    probe._invalidate_input_event = lambda _event: True
    probe._async_debounced_recalculate = object()
    for _ in range(5):
        probe._async_input_changed(SimpleNamespace(data={"entity_id": "input_select.hoymiles_tariff_type"}))
    check(len(records) == 1 and records[0]["delay"] == 1.0, "five tariff form changes did not collapse to one immediate debounce")

    periodic_calls: list[str] = []
    invalidations: list[str] = []

    async def recalculate() -> None:
        periodic_calls.append(probe._full_plan_trigger)

    probe._recalculate_cancel = None
    probe._invalidate_internal_inputs = lambda: invalidations.append("dirty")
    probe._recalculate_and_write = recalculate
    for _ in range(15):
        await probe._async_timer(datetime.now(timezone.utc))
    check(periodic_calls == ["periodic"] * 15, "tariff periodic callback did not produce exactly fifteen bounded runs")
    check(len(invalidations) == 15, "tariff periodic callback invalidated an unexpected number of snapshots")
    check(1 + len(periodic_calls) == 16, "thirty-minute tariff window is not startup plus fifteen 120-second runs")

    broker_records: list[dict[str, Any]] = []
    BrokerProbe = _compile_probe(
        [_method(sensor, "_async_rce_broker_changed")],
        {
            "TARIFF_IMMEDIATE_RECALCULATION_DELAY_SECONDS": 1.0,
            "_shared_inputs_snapshot": lambda runtime: getattr(
                runtime, "snapshot", None
            ),
            "async_call_later": _scheduler(broker_records),
        },
    )
    broker = BrokerProbe()
    broker.hass = object()
    broker._runtime = SimpleNamespace(snapshot=None)
    broker._lifecycle_stopped = False
    broker._recalculate_cancel = None
    broker._async_debounced_recalculate = object()
    broker._invalidate_input_event = lambda event: event.data["field"] in {
        "consumed",
        "generated_at",
    }
    broker._async_rce_broker_changed(SimpleNamespace(data={"field": "generated_at"}))
    check(
        len(broker_records) == 1,
        "consumed LOAD snapshot provenance did not trigger one bounded refresh",
    )
    broker._async_rce_broker_changed(SimpleNamespace(data={"field": "consumed"}))
    broker._async_rce_broker_changed(SimpleNamespace(data={"field": "consumed"}))
    check(
        len(broker_records) == 1
        and broker._full_plan_trigger == "rce_load_broker",
        "RCE LOAD changes did not coalesce into one tariff recalculation",
    )

    broker_records.clear()
    broker._recalculate_cancel = None
    broker._runtime = SimpleNamespace(
        snapshot=SimpleNamespace(load=object())
    )
    broker._async_rce_broker_changed(
        SimpleNamespace(data={"field": "consumed"})
    )
    check(
        not broker_records,
        "unused legacy RCE broker invalidated tariff while Shared EMS LOAD was active",
    )

    revision_source = (COMPONENT / "optimizer_revision.py").read_text(
        encoding="utf-8"
    )
    broker_projection = revision_source.split(
        "RCE_LOAD_BROKER_ATTRIBUTES = frozenset(", 1
    )[1].split(")", 1)[0]
    check(
        '"load_profile_generated_at"' in broker_projection,
        "consumed LOAD snapshot provenance is absent from counterpart projection",
    )

    shared_records: list[dict[str, Any]] = []
    shared_signature = ["stable"]
    SharedProbe = _compile_probe(
        [_method(sensor, "_async_shared_inputs_changed")],
        {
            "TARIFF_SHARED_BOOTSTRAP_RECALCULATION_DELAY_SECONDS": 1.0,
            "_shared_inputs_snapshot": lambda _runtime: SimpleNamespace(
                load=SimpleNamespace(ready=True)
            ),
            "_tariff_shared_critical_signature": lambda _runtime: tuple(
                shared_signature
            ),
            "async_call_later": _scheduler(shared_records),
        },
    )
    shared = SharedProbe()
    shared.hass = object()
    shared._runtime = object()
    shared._lifecycle_stopped = False
    shared._shared_inputs_load_ready = True
    shared._shared_inputs_critical_signature = tuple(shared_signature)
    shared._shared_inputs_bootstrap_seen = True
    shared._shared_inputs_dirty = False
    shared._recalculate_cancel = None
    shared._required_input_recovery_pending = False
    shared._schedule_available_required_input_recovery = lambda: None
    shared._async_debounced_recalculate = object()
    shared._invalidate_internal_inputs = lambda: invalidations.append("shared")

    for _ in range(300):
        shared._async_shared_inputs_changed()
    check(shared._shared_inputs_dirty, "tariff did not retain ordinary Shared EMS churn")
    check(not shared_records, "ordinary Shared EMS churn armed a second tariff timer")

    shared_signature[0] = "changed"
    shared._async_shared_inputs_changed()
    check(
        not shared_records and shared._shared_inputs_dirty,
        "physical Shared EMS cohort still created a second tariff run",
    )

    shared._shared_inputs_load_ready = False
    shared._async_shared_inputs_changed()
    check(
        len(shared_records) == 1
        and shared_records[0]["delay"] == 1.0
        and shared._full_plan_trigger == "shared_load_recovery",
        "required Shared LOAD recovery did not retain its one immediate pass",
    )

    recovery_records: list[dict[str, Any]] = []

    def recovery_sample(
        state: Any,
        _now: datetime,
        *,
        max_age_seconds: float,
        minimum: float,
        maximum: float,
    ) -> Any:
        del max_age_seconds
        try:
            value = float(state.state)
        except (AttributeError, TypeError, ValueError):
            value = math.nan
        return SimpleNamespace(
            fresh=bool(
                getattr(state, "fresh", False)
                and math.isfinite(value)
                and minimum <= value <= maximum
            )
        )

    RecoveryProbe = _compile_probe(
        [
            _method(sensor, "_async_required_input_recovered"),
            _method(sensor, "_schedule_available_required_input_recovery"),
        ],
        {
            "TARIFF_REQUIRED_INPUT_RECOVERY_ENTITIES": frozenset(
                required_recovery_entities
            ),
            "TARIFF_IMMEDIATE_RECALCULATION_DELAY_SECONDS": 1.0,
            "_FORECAST_GCF_READBACK_MAX_SKEW_SECONDS": 3.0,
            "SLOW_TELEMETRY_MAX_AGE_SECONDS": 120.0,
            "numeric_state_sample": recovery_sample,
            "math": math,
            "dt_util": SimpleNamespace(
                now=lambda: datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)
            ),
            "async_call_later": _scheduler(recovery_records),
        },
    )
    battery_entity = "sensor.hoymiles_hit_battery_capacity"
    states = {
        battery_entity: SimpleNamespace(state="26.0", fresh=False),
    }
    recovery = RecoveryProbe()
    recovery.hass = SimpleNamespace(
        states=SimpleNamespace(get=lambda entity_id: states.get(entity_id))
    )
    recovery._attributes = {"missing_entities": [battery_entity]}
    recovery._lifecycle_stopped = False
    recovery._required_input_recovery_pending = False
    recovery._forecast_gcf_policy_evaluation_cancel = None
    recovery._recalculate_cancel = None
    recovery._async_debounced_recalculate = object()
    revision = SimpleNamespace(value=4)

    def invalidate() -> None:
        revision.value += 1

    recovery._invalidate_internal_inputs = invalidate

    def recovery_event(entity_id: str, value: str) -> Any:
        return SimpleNamespace(
            data={
                "entity_id": entity_id,
                "new_state": SimpleNamespace(state=value),
            }
        )

    recovery._async_required_input_recovered(
        recovery_event("sensor.hoymiles_actual_load_power", "500.0")
    )
    recovery._async_required_input_recovered(
        recovery_event(battery_entity, "nan")
    )
    recovery._async_required_input_recovered(
        recovery_event(battery_entity, "26.0")
    )
    check(
        not recovery_records and revision.value == 4,
        "unrelated, non-finite or stale tariff reports armed recovery",
    )

    states[battery_entity].fresh = True
    for _ in range(20):
        recovery._async_required_input_recovered(
            recovery_event(battery_entity, "26.0")
        )
    check(
        len(recovery_records) == 1
        and recovery_records[0]["delay"] == 1.0
        and recovery_records[0]["callback"]
        is recovery._async_debounced_recalculate,
        "tariff required-input recovery bypassed the existing debounce",
    )
    check(
        revision.value == 5
        and recovery._required_input_recovery_pending
        and recovery._full_plan_trigger == "required_input_recovered",
        "tariff required-input recovery did not invalidate exactly one snapshot",
    )

    ordinary_records: list[dict[str, Any]] = []
    OrdinaryProbe = _compile_probe(
        [
            _method(sensor, "_async_required_input_recovered"),
            _method(sensor, "_schedule_available_required_input_recovery"),
        ],
        {
            "TARIFF_REQUIRED_INPUT_RECOVERY_ENTITIES": frozenset(
                required_recovery_entities
            ),
            "TARIFF_IMMEDIATE_RECALCULATION_DELAY_SECONDS": 1.0,
            "_FORECAST_GCF_READBACK_MAX_SKEW_SECONDS": 3.0,
            "SLOW_TELEMETRY_MAX_AGE_SECONDS": 120.0,
            "numeric_state_sample": recovery_sample,
            "math": math,
            "dt_util": SimpleNamespace(
                now=lambda: datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)
            ),
            "async_call_later": _scheduler(ordinary_records),
        },
    )
    ordinary = OrdinaryProbe()
    ordinary.hass = recovery.hass
    ordinary._attributes = {"missing_entities": []}
    ordinary._lifecycle_stopped = False
    ordinary._required_input_recovery_pending = False
    ordinary._forecast_gcf_policy_evaluation_cancel = None
    ordinary._recalculate_cancel = None
    ordinary._async_debounced_recalculate = object()
    ordinary_invalidations = 0

    def invalidate_ordinary() -> None:
        nonlocal ordinary_invalidations
        ordinary_invalidations += 1

    ordinary._invalidate_internal_inputs = invalidate_ordinary
    ordinary._async_required_input_recovered(
        recovery_event(battery_entity, "26.0")
    )
    check(
        not ordinary_records and ordinary_invalidations == 0,
        "ordinary numeric reports restarted tariff after missing inputs cleared",
    )

    cold_start_records: list[dict[str, Any]] = []
    ColdStartProbe = _compile_probe(
        [_method(sensor, "_schedule_available_required_input_recovery")],
        {
            "TARIFF_REQUIRED_INPUT_RECOVERY_ENTITIES": frozenset(
                required_recovery_entities
            ),
            "TARIFF_IMMEDIATE_RECALCULATION_DELAY_SECONDS": 1.0,
            "_FORECAST_GCF_READBACK_MAX_SKEW_SECONDS": 3.0,
            "SLOW_TELEMETRY_MAX_AGE_SECONDS": 120.0,
            "numeric_state_sample": recovery_sample,
            "dt_util": SimpleNamespace(
                now=lambda: datetime(2026, 9, 5, 14, 0, tzinfo=timezone.utc)
            ),
            "async_call_later": _scheduler(cold_start_records),
        },
    )
    cold_start = ColdStartProbe()
    cold_start.hass = recovery.hass
    cold_start._attributes = {"missing_entities": [battery_entity]}
    cold_start._lifecycle_stopped = False
    cold_start._required_input_recovery_pending = False
    cold_start._forecast_gcf_policy_evaluation_cancel = None
    cold_start._recalculate_cancel = None
    cold_start._async_debounced_recalculate = object()
    cold_start._invalidate_internal_inputs = lambda: None
    # No state event fires here: the numeric source preceded publication of
    # the missing projection, exactly as in the observed HA startup race.
    cold_start._schedule_available_required_input_recovery()
    check(
        len(cold_start_records) == 1,
        "numeric tariff input available before missing publication was not recovered",
    )


async def _rcm_contract() -> None:
    tree = _tree("rcm_sensor.py")
    source = (COMPONENT / "rcm_sensor.py").read_text(encoding="utf-8")
    sensor = _class(tree, "HoymilesRCMOptimizerSensor")
    coalesced = _literal_strings(tree, "RCM_FULL_PLAN_COALESCED_ENTITIES")
    event_driven = _literal_strings(tree, "RCM_EVENT_DRIVEN_ENTITIES")
    physical_plan_inputs = {
        "sensor.hoymiles_hit_battery_capacity",
        "sensor.hoymiles_hit_total_capacity",
        "sensor.hoymiles_hit_number_of_machines_master_and_slave",
        "sensor.hoymiles_hit_gcf_enable_readback_code",
    }
    check(
        physical_plan_inputs <= coalesced
        and physical_plan_inputs.isdisjoint(event_driven),
        "RCEm physical cohort can still enter the immediate setting listener",
    )
    check("RCM_FULL_PLAN_INTERVAL = timedelta(minutes=10)" in source, "RCEm full cadence is not ten minutes")
    check("RCM_FORECAST_BATCH_DELAY_SECONDS = 5.0" in source, "RCEm forecast cohort is not bounded to five seconds")
    check(
        "RCM_STALE_RESULT_RETRY_DELAY_SECONDS = 5.0" in source,
        "RCEm stale-result recovery is not bounded to five seconds",
    )
    check("timedelta(seconds=15)" in source, "RCEm live safety cadence changed from 15 seconds")
    added = ast.get_source_segment(source, _method(sensor, "async_added_to_hass")) or ""
    dynamic_listener = ast.get_source_segment(
        source, _method(sensor, "_refresh_dynamic_forecast_listener")
    ) or ""
    check(
        "sorted(self._event_driven_rcm_entities())" in added
        and "sorted(RCM_FORECAST_VALUE_ENTITIES)" in added,
        "RCEm static forecast values are not separated from immediate inputs",
    )
    check(
        "self._async_forecast_value_changed" in dynamic_listener
        and "self._async_input_changed" not in dynamic_listener,
        "custom RCEm forecast values still use the immediate-input listener",
    )
    event_entities = ast.get_source_segment(
        source, _method(sensor, "_event_driven_rcm_entities")
    ) or ""
    check(
        "rce_entity_id" not in event_entities,
        "legacy RCE LOAD publication still enters the immediate RCEm listener",
    )
    full_plan = ast.get_source_segment(
        source, _method(sensor, "_recalculate_locked")
    ) or ""
    hard_required = full_plan.split("missing = sorted", 1)[0]
    check(
        'required["sensor.hoymiles_hit_overview_pv_total_power"]'
        not in hard_required
        and 'required["sensor.hoymiles_actual_load_power"]'
        not in hard_required,
        "instantaneous PV/LOAD still invalidate the RCEm risk trajectory",
    )
    check(
        "live_power_data_fresh = pv_power_fresh and load_power_fresh"
        in full_plan
        and "live_power_data_fresh=live_power_data_fresh" in full_plan,
        "stale live PV/LOAD no longer reaches the fail-closed optimizer gate",
    )
    check(
        full_plan.count("self._full_plan_rejected_for_input_drift = True") == 2
        and full_plan.count(
            "self._full_plan_rejected_for_input_drift = False"
        )
        == 1,
        "RCEm delayed retry is no longer scoped to the two stale-result guards",
    )

    retry_records: list[dict[str, Any]] = []
    RetryProbe = _compile_probe(
        [
            _method(sensor, "_schedule_full_plan_recalculation"),
            _method(sensor, "_schedule_stale_result_retry"),
            _method(sensor, "_cancel_stale_result_retry"),
            _method(sensor, "_cancel_full_plan_recalculation"),
            _method(sensor, "_async_stale_result_retry"),
            _method(sensor, "_recalculate_and_write"),
        ],
        {
            "MAX_IMMEDIATE_RECALCULATIONS": 3,
            "RCM_FULL_PLAN_RECALCULATION_DELAY_SECONDS": 1.0,
            "RCM_STALE_RESULT_RETRY_DELAY_SECONDS": 5.0,
            "async_call_later": _scheduler(retry_records),
            "timezone": timezone,
        },
    )
    retry = RetryProbe()
    retry.hass = object()
    retry.native_value = "pending"
    retry._attributes = {
        "result_current": False,
        "recalculation_pending": True,
    }
    retry._optimizer_lock = asyncio.Lock()
    retry._full_plan_recalculate_cancel = None
    retry._forecast_batch_recalculate_cancel = None
    retry._stale_result_retry_cancel = None
    retry._required_input_recovery_pending = False
    retry._full_plan_solver_calls = 0
    retry._last_full_plan_at = None
    retry._full_plan_trigger = "periodic"
    retry._full_plan_rejected_for_input_drift = False
    retry._async_debounced_full_plan_recalculation = object()
    retry.async_write_ha_state = lambda: None
    current_marks: list[None] = []
    timeline_publications: list[None] = []
    retry._mark_result_current = lambda: current_marks.append(None)
    retry._publish_timeline_result = lambda: timeline_publications.append(None)
    attempts = 0

    async def reject_input_drift() -> bool:
        nonlocal attempts
        attempts += 1
        retry._full_plan_rejected_for_input_drift = True
        return False

    retry._recalculate_locked = reject_input_drift
    await retry._recalculate_and_write()
    check(
        attempts == 3
        and len(retry_records) == 1
        and retry_records[0]["delay"] == 5.0,
        "three stale RCEm solver results did not arm one delayed retry",
    )
    retry._schedule_stale_result_retry()
    check(
        len(retry_records) == 1,
        "RCEm stale-result retry is not deduplicated",
    )
    await retry_records[0]["callback"](datetime.now(timezone.utc))
    check(
        attempts == 6
        and len(retry_records) == 1
        and retry._stale_result_retry_cancel is None
        and retry._attributes["last_full_plan_trigger"] == "stale_result_retry",
        "delayed RCEm retry re-armed itself or lost its diagnostic trigger",
    )

    async def reject_missing_data() -> bool:
        nonlocal attempts
        attempts += 1
        retry._full_plan_rejected_for_input_drift = False
        return False

    retry._recalculate_locked = reject_missing_data
    await retry._recalculate_and_write()
    check(
        attempts == 9 and len(retry_records) == 1,
        "permanent RCEm missing data incorrectly armed a delayed solver retry",
    )

    async def commit_stable_result() -> bool:
        nonlocal attempts
        attempts += 1
        retry._full_plan_rejected_for_input_drift = False
        return True

    retry._recalculate_locked = commit_stable_result
    retry._schedule_stale_result_retry()
    await retry_records[1]["callback"](datetime.now(timezone.utc))
    check(
        attempts == 10
        and current_marks == [None]
        and timeline_publications == [None],
        "successful delayed RCEm retry did not publish the current plan",
    )

    retry._schedule_stale_result_retry()
    retry._schedule_full_plan_recalculation()
    check(
        retry_records[2]["cancelled"]
        and len(retry_records) == 4
        and retry_records[3]["delay"] == 1.0,
        "a newer RCEm trigger did not supersede the stale-result retry",
    )
    retry._schedule_stale_result_retry()
    retry._cancel_full_plan_recalculation()
    check(
        retry_records[3]["cancelled"]
        and retry_records[4]["cancelled"]
        and retry._stale_result_retry_cancel is None,
        "RCEm unload cleanup left a stale-result retry armed",
    )

    Probe = _compile_probe(
        [_method(sensor, "_async_control_timer"), _method(sensor, "_async_full_plan_timer")],
        {
            "timezone": timezone,
            "RCM_HISTORY_REFRESH_INTERVAL": __import__("datetime").timedelta(
                hours=1
            ),
        },
    )
    probe = Probe()
    samples: list[datetime] = []
    refreshes: list[None] = []
    invalidations: list[str] = []
    full_calls: list[str] = []
    probe._refresh_dynamic_forecast_listener = lambda: refreshes.append(None)
    probe._append_voltage_sample = lambda now: samples.append(now)

    async def no_threshold(_now: datetime) -> bool:
        return False

    async def recalculate() -> None:
        full_calls.append(probe._full_plan_trigger)

    probe._async_live_control_refresh = no_threshold
    probe._invalidate_internal_inputs = lambda: invalidations.append("dirty")
    probe._recalculate_and_write = recalculate
    probe._cancel_full_plan_recalculation = lambda: None
    probe._history_refreshed_at = None
    probe._history_refresh_running = False
    for _ in range(40):
        await probe._async_control_timer(datetime.now(timezone.utc))
    check(len(samples) == 40 and len(refreshes) == 40, "RCEm 15-second safety loop was not retained across ten minutes")
    check(not full_calls and not invalidations, "steady RCEm voltage loop rebuilt the full risk plan")
    await probe._async_full_plan_timer(datetime.now(timezone.utc))
    check(full_calls == ["periodic"] and invalidations == ["dirty"], "RCEm ten-minute timer did not create exactly one full plan run")
    check(1 + len(full_calls) == 2, "RCEm ten-minute window exceeds initial + ten-minute solver budget")

    boundary = datetime.now(timezone.utc)
    full_calls.clear()
    invalidations.clear()
    probe._history_refreshed_at = boundary - __import__("datetime").timedelta(
        hours=1
    )
    await probe._async_full_plan_timer(boundary)
    check(
        not full_calls and not invalidations,
        "RCEm periodic timer duplicated the due hourly history rebuild",
    )
    probe._history_refreshed_at = None

    async def threshold_edge(_now: datetime) -> bool:
        return True

    probe._async_live_control_refresh = threshold_edge
    full_calls.clear()
    invalidations.clear()
    await probe._async_control_timer(datetime.now(timezone.utc))
    check(
        not full_calls and not invalidations,
        "RCEm voltage threshold edge rebuilt the full forecast/risk plan",
    )

    live_event_calls: list[str] = []
    LiveEventProbe = _compile_probe(
        [_method(sensor, "_async_input_changed")],
        {
            "FORECAST_ENTITY_HELPERS": frozenset(),
            "GRID_VOLTAGE_ENTITIES": frozenset({"sensor.voltage"}),
            "RCM_LIVE_CONTROL_ENTITIES": frozenset({"sensor.voltage"}),
            "dt_util": SimpleNamespace(
                now=lambda: datetime.now(timezone.utc)
            ),
        },
    )
    live_event = LiveEventProbe()
    live_event._attributes = {"missing_entities": []}
    live_event._append_voltage_sample = lambda: live_event_calls.append(
        "sample"
    )

    async def live_event_refresh(_now: datetime) -> bool:
        live_event_calls.append("refresh")
        return True

    live_event._async_live_control_refresh = live_event_refresh
    live_event._invalidate_internal_inputs = lambda: live_event_calls.append(
        "invalidated"
    )

    async def live_event_recalculate() -> None:
        live_event_calls.append("full_plan")

    live_event._recalculate_and_write = live_event_recalculate
    await live_event._async_input_changed(
        SimpleNamespace(
            data={
                "entity_id": "sensor.voltage",
                "new_state": SimpleNamespace(state="253.1"),
            }
        )
    )
    check(
        live_event_calls == ["sample", "refresh"],
        "RCEm voltage state event rebuilt the full forecast/risk plan",
    )

    records: list[dict[str, Any]] = []
    InputProbe = _compile_probe(
        [_method(sensor, "_async_input_changed"), _method(sensor, "_schedule_full_plan_recalculation")],
        {
            "FORECAST_ENTITY_HELPERS": frozenset(),
            "RCM_LIVE_CONTROL_ENTITIES": frozenset(),
            "RCM_FULL_PLAN_RECALCULATION_DELAY_SECONDS": 1.0,
            "async_call_later": _scheduler(records),
        },
    )
    input_probe = InputProbe()
    input_probe.hass = object()
    input_probe._full_plan_recalculate_cancel = None
    input_probe._cancel_stale_result_retry = lambda: None
    input_probe._invalidate_input_event = lambda _event: True
    input_probe._async_debounced_full_plan_recalculation = object()
    for _ in range(5):
        await input_probe._async_input_changed(SimpleNamespace(data={"entity_id": "input_number.rcm_margin"}))
    check(len(records) == 1 and records[0]["delay"] == 1.0, "RCEm setting burst did not collapse to one immediate plan debounce")

    forecast_records: list[dict[str, Any]] = []
    ForecastProbe = _compile_probe(
        [
            _method(sensor, "_async_forecast_value_changed"),
            _method(sensor, "_schedule_forecast_batch_recalculation"),
        ],
        {
            "RCM_FORECAST_BATCH_DELAY_SECONDS": 5.0,
            "async_call_later": _scheduler(forecast_records),
        },
    )
    forecast = ForecastProbe()
    forecast.hass = object()
    forecast._forecast_batch_recalculate_cancel = None
    forecast._cancel_stale_result_retry = lambda: None
    forecast._invalidate_input_event = lambda _event: True
    forecast._async_debounced_forecast_batch_recalculation = object()
    for _ in range(5):
        forecast._async_forecast_value_changed(SimpleNamespace())
    check(
        len(forecast_records) == 5
        and all(record["cancelled"] for record in forecast_records[:-1])
        and not forecast_records[-1]["cancelled"]
        and forecast_records[-1]["delay"] == 5.0
        and forecast._full_plan_trigger == "forecast_batch",
        "five RCEm forecast fields did not collapse to one trailing cohort",
    )

    recovery_records: list[dict[str, Any]] = []
    RecoveryProbe = _compile_probe(
        [_method(sensor, "_async_input_changed"), _method(sensor, "_schedule_full_plan_recalculation")],
        {
            "FORECAST_ENTITY_HELPERS": frozenset(),
            "RCM_LIVE_CONTROL_ENTITIES": frozenset({"sensor.live_load"}),
            "GRID_VOLTAGE_ENTITIES": frozenset(),
            "RCM_FULL_PLAN_RECALCULATION_DELAY_SECONDS": 1.0,
            "isfinite": __import__("math").isfinite,
            "dt_util": SimpleNamespace(now=lambda: datetime.now(timezone.utc)),
            "async_call_later": _scheduler(recovery_records),
        },
    )
    recovery = RecoveryProbe()
    recovery.hass = object()
    recovery._attributes = {"missing_entities": ["sensor.live_load"]}
    recovery._required_input_recovery_pending = False
    recovery._full_plan_recalculate_cancel = None
    recovery._cancel_stale_result_retry = lambda: None
    recovery._input_revision = SimpleNamespace(
        value=4,
        invalidate=lambda: setattr(recovery._input_revision, "value", 5),
    )
    recovery._mark_recalculation_pending = lambda: None
    recovery._async_debounced_full_plan_recalculation = object()
    recovery._append_voltage_sample = lambda: None

    async def steady_live(_now: datetime) -> bool:
        return False

    recovery._async_live_control_refresh = steady_live
    recovered_event = SimpleNamespace(
        data={
            "entity_id": "sensor.live_load",
            "new_state": SimpleNamespace(state="123.0"),
        }
    )
    for _ in range(20):
        await recovery._async_input_changed(recovered_event)
    check(
        len(recovery_records) == 1
        and recovery._input_revision.value == 5
        and recovery._full_plan_trigger == "required_input_recovered",
        "RCEm required-source recovery did not create exactly one bounded pass",
    )


def _retained_plan_contract() -> None:
    """A brief missing input keeps display data but never keeps authority."""

    now = datetime(2026, 9, 4, 10, 0, tzinfo=timezone.utc)

    class Timeline:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        def publish_unavailable(self, **kwargs: Any) -> None:
            self.calls.append(kwargs)

    for filename, class_name, grace_seconds, result_field in (
        ("rce_sensor.py", "HoymilesRCEOptimizerSensor", 15 * 60.0, "_result"),
        ("tariff_sensor.py", "HoymilesTariffOptimizerSensor", 15 * 60.0, "_result"),
        ("rcm_sensor.py", "HoymilesRCMOptimizerSensor", 10 * 60.0, "_timeline_trace"),
    ):
        tree = _tree(filename)
        sensor = _class(tree, class_name)
        Probe = _compile_probe(
            [_method(sensor, "_retain_last_complete_plan")],
            {
                "dt_util": SimpleNamespace(utcnow=lambda: now, now=lambda: now),
                "timezone": timezone,
                "_LAST_COMPLETE_PLAN_GRACE_SECONDS": grace_seconds,
            },
        )
        probe = Probe()
        setattr(probe, result_field, object())
        probe._last_full_plan_at = now
        probe._attributes = {
            "status_code": "ready",
            "planned_slots": [{"energy": 1.25}],
            "result_current": True,
        }
        probe._input_revision = SimpleNamespace(value=7)
        probe._timeline_sensor = Timeline()
        retained = probe._retain_last_complete_plan(
            blocker_code="missing_data",
            missing_entities=["sensor.missing"],
        )
        check(retained, f"{class_name} did not retain a recent complete plan")
        check(
            probe._attributes["planned_slots"] == [{"energy": 1.25}]
            and probe._attributes["status_code"] == "ready"
            and probe._attributes["result_current"] is False
            and probe._attributes["recalculation_pending"] is True,
            f"{class_name} either cleared display data or retained authority",
        )
        check(
            probe._timeline_sensor.calls == [
                {"input_revision": 7, "blocker_code": "missing_data"}
            ],
            f"{class_name} did not publish one retained pending timeline",
        )
        probe._last_full_plan_at = now - __import__("datetime").timedelta(
            seconds=grace_seconds + 1
        )
        check(
            not probe._retain_last_complete_plan(
                blocker_code="missing_data",
                missing_entities=["sensor.missing"],
            ),
            f"{class_name} retained a plan beyond its grace window",
        )


def _canonical_contract() -> None:
    tree = _tree("supervisor_canonical_sensor.py")
    source = (COMPONENT / "supervisor_canonical_sensor.py").read_text(encoding="utf-8")
    sensor = _class(tree, "HoymilesSupervisorCanonicalPlanSensor")
    check("CANONICAL_COHORT_DELAY_SECONDS = 3.0" in source, "canonical cohort is outside the two-to-five second window")
    source_projection = ast.get_source_segment(source, _method(sensor, "_source_event_changes_plan")) or ""
    check('attributes.get("generated_at")' not in source_projection, "canonical source projection still consumes generated_at")
    check(
        'TIMELINE_MAX_AGE_SECONDS = {"rce": 150.0, "tariff": 150.0, "rcm": 630.0}'
        in (COMPONENT / "supervisor_canonical_runtime.py").read_text(encoding="utf-8"),
        "canonical freshness windows do not match full-plan cadences",
    )

    records: list[dict[str, Any]] = []
    Probe = _compile_probe(
        [
            _method(sensor, "_timestamp"),
            _method(sensor, "_empty_attributes"),
            _method(sensor, "_publish"),
            _method(sensor, "_retained_attributes"),
            _method(sensor, "_source_states"),
            _method(sensor, "_expected_semantic_value"),
            _method(sensor, "_expected_event_changes_projection"),
            _method(sensor, "_cancel_expected_recompute"),
            _method(sensor, "_schedule_recompute"),
            _method(sensor, "_publish_pending_cohort"),
            _method(sensor, "_state_semantic_value"),
            _method(sensor, "_source_event_changes_plan"),
            _method(sensor, "_source_event_refreshes_freshness"),
            _method(sensor, "_handle_source_event"),
            _method(sensor, "_handle_registry_event"),
        ],
        {
            "CANONICAL_COHORT_DELAY_SECONDS": 3.0,
            "async_call_later": _scheduler(records),
            "dt_util": SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc)),
            "timezone": timezone,
        },
    )
    probe = Probe()
    probe.hass = object()
    probe._removed = False
    probe._recompute_cancel = None
    expected_recompute_cancels: list[str] = []
    probe._expected_recompute_cancel = lambda: expected_recompute_cancels.append(
        "cancelled"
    )
    probe._freshness_expiry_cancel = None
    probe._freshness_expiry_generation = 0
    probe._recompute = lambda: None
    for _ in range(3):
        probe._schedule_recompute()
    check(
        len(records) == 1
        and records[0]["delay"] == 3.0
        and expected_recompute_cancels == ["cancelled"],
        "three policy revisions did not replace expected-only work with one canonical refresh",
    )

    timeline = SimpleNamespace(entity_id="sensor.rce_timeline")
    probe._expected_timeline = SimpleNamespace(
        entity_id="sensor.expected_timeline"
    )
    probe._supervisor = SimpleNamespace(entity_id="sensor.supervisor")
    probe._capacity_entity_id = "sensor.capacity"
    probe._timelines = {"rce": timeline}
    probe._timeline_dependencies = canonical_timeline_dependencies(None)
    old = SimpleNamespace(state="current", attributes={"plan_revision": 7, "generated_at": "old"})
    generated_only = SimpleNamespace(state="current", attributes={"plan_revision": 7, "generated_at": "new"})
    revised = SimpleNamespace(state="current", attributes={"plan_revision": 8, "generated_at": "new"})
    check(not probe._source_event_changes_plan(SimpleNamespace(data={"entity_id": "sensor.rce_timeline", "old_state": old, "new_state": generated_only})), "canonical regenerated on a timeline timestamp-only event")
    check(probe._source_event_refreshes_freshness(SimpleNamespace(data={"entity_id": "sensor.rce_timeline", "old_state": old, "new_state": generated_only})), "canonical ignored a successful metadata-only full-plan refresh")
    check(probe._source_event_changes_plan(SimpleNamespace(data={"entity_id": "sensor.rce_timeline", "old_state": generated_only, "new_state": revised})), "canonical ignored a semantic timeline revision")
    probe._last_complete_attributes = {
        "schema_version": 1,
        "output_only": True,
        "slots": [{"start": "2026-09-04T12:00:00Z"}],
    }
    probe._platform_added = False
    probe._recompute_cancel = None
    probe._state = "current"
    probe._attributes = dict(probe._last_complete_attributes)
    records.clear()
    probe._handle_source_event(
        SimpleNamespace(
            data={
                "entity_id": "sensor.rce_timeline",
                "old_state": old,
                "new_state": generated_only,
            }
        )
    )
    check(
        probe._state == "current"
        and len(records) == 1
        and records[0]["delay"] == 3.0,
        "metadata-only full-plan refresh did not schedule one non-flickering canonical rebuild",
    )
    probe._recompute_cancel = None
    records.clear()
    probe._handle_source_event(
        SimpleNamespace(
            data={
                "entity_id": "sensor.rce_timeline",
                "old_state": generated_only,
                "new_state": revised,
            }
        )
    )
    check(
        probe._state == "pending"
        and probe._attributes["result_current"] is False
        and probe._attributes["recalculation_pending"] is True
        and probe._attributes["slots"]
        == [{"start": "2026-09-04T12:00:00Z"}]
        and probe._attributes["canonical_blocker_code"]
        == "source_recalculation_pending"
        and len(records) == 1,
        "semantic timeline change left stale canonical SOC current during cohort delay",
    )
    probe._state = "current"
    probe._attributes = dict(probe._last_complete_attributes)
    probe._recompute_cancel = None
    records.clear()
    probe._handle_source_event(
        SimpleNamespace(
            data={
                "entity_id": "sensor.supervisor",
                "old_state": SimpleNamespace(
                    state="active_idle",
                    attributes={"selected_policy": "rce", "selected_candidate_revision": 7},
                ),
                "new_state": SimpleNamespace(
                    state="active_idle",
                    attributes={"selected_policy": "tariff", "selected_candidate_revision": 8},
                ),
            }
        )
    )
    check(
        probe._state == "pending"
        and probe._attributes["result_current"] is False
        and probe._attributes["slots"]
        == [{"start": "2026-09-04T12:00:00Z"}]
        and len(records) == 1,
        "semantic Supervisor change left stale canonical SOC current during cohort delay",
    )
    probe._state = "current"
    probe._attributes = dict(probe._last_complete_attributes)
    probe._recompute_cancel = None
    probe._capacity_entity_id = "sensor.capacity"
    probe._resolve_capacity_entity = lambda: "sensor.capacity_rebound"
    subscription_calls: list[str] = []
    probe._subscribe_sources = lambda: subscription_calls.append("subscribed")
    records.clear()
    probe._handle_registry_event(SimpleNamespace(data={"action": "update"}))
    check(
        probe._capacity_entity_id == "sensor.capacity_rebound"
        and subscription_calls == ["subscribed"]
        and probe._state == "pending"
        and probe._attributes["result_current"] is False
        and len(records) == 1,
        "capacity source rebind left stale canonical SOC current during cohort delay",
    )

    class CanonicalRuntimeError(ValueError):
        def __init__(self, code: str) -> None:
            super().__init__(code)
            self.code = code

    RetentionProbe = _compile_probe(
        [
            _method(sensor, "_timestamp"),
            _method(sensor, "_empty_attributes"),
            _method(sensor, "_publish"),
            _method(sensor, "_retained_attributes"),
            _method(sensor, "_source_states"),
            _method(sensor, "_cancel_expected_recompute"),
            _method(sensor, "_recompute"),
        ],
        {
            "CanonicalRuntimeError": CanonicalRuntimeError,
            "timezone": timezone,
            "dt_util": SimpleNamespace(utcnow=lambda: datetime.now(timezone.utc)),
            "build_supervisor_canonical_ledger": lambda **_kwargs: (_ for _ in ()).throw(
                CanonicalRuntimeError("rcm_timeline_stale")
            ),
            "canonical_execution_ledger_to_dict": lambda ledger: ledger,
        },
    )
    retention = RetentionProbe()
    retention._recompute_cancel = object()
    retention._expected_recompute_cancel = None
    retention._cancel_freshness_expiry = lambda: None
    retention._schedule_freshness_expiry = lambda: None
    retention._removed = False
    retention._platform_added = False
    retention._last_complete_attributes = {
        "schema_version": 1,
        "output_only": True,
        "slots": [{"start": "2026-09-04T12:00:00Z"}],
    }
    retention._supervisor = SimpleNamespace(latest_active_frame=object())
    retention._expected_timeline = SimpleNamespace(
        extra_state_attributes=None
    )
    retention._timelines = {
        policy: SimpleNamespace(native_value="current", extra_state_attributes={})
        for policy in ("rce", "tariff", "rcm")
    }
    retention._capacity_kwh = lambda: 10.0
    retention._recompute()
    check(
        retention._state == "partial"
        and retention._attributes["slots"]
        == [{"start": "2026-09-04T12:00:00Z"}]
        and retention._attributes["result_current"] is False
        and retention._attributes["recalculation_pending"] is True
        and retention._attributes["canonical_blocker_code"]
        == "rcm_timeline_stale",
        "stale source erased the retained output-only canonical geometry",
    )

    # The expiry callback must validate against wall clock, not the older
    # planning frame, and must not re-arm itself every 100 ms while waiting for
    # the next Supervisor frame.
    expiry_records: list[dict[str, Any]] = []
    wall_clock = {"now": datetime(2026, 9, 4, 12, 0, tzinfo=timezone.utc)}
    freshness_calls: list[datetime] = []

    def stale_on_wall_clock(**kwargs: Any) -> Any:
        freshness_calls.append(kwargs["freshness_now"])
        raise CanonicalRuntimeError("rce_timeline_stale")

    ExpiryProbe = _compile_probe(
        [
            _method(sensor, "_timestamp"),
            _method(sensor, "_empty_attributes"),
            _method(sensor, "_publish"),
            _method(sensor, "_retained_attributes"),
            _method(sensor, "_source_states"),
            _method(sensor, "_cancel_freshness_expiry"),
            _method(sensor, "_schedule_freshness_expiry"),
            _method(sensor, "_cancel_expected_recompute"),
            _method(sensor, "_recompute"),
        ],
        {
            "CanonicalRuntimeError": CanonicalRuntimeError,
            "FRESHNESS_EXPIRY_EPSILON_SECONDS": 0.1,
            "TIMELINE_MAX_AGE_SECONDS": {
                "rce": 150.0,
                "tariff": 150.0,
                "rcm": 630.0,
            },
            "async_call_later": _scheduler(expiry_records),
            "canonical_execution_ledger_to_dict": lambda ledger: ledger,
            "build_supervisor_canonical_ledger": stale_on_wall_clock,
            "dt_util": SimpleNamespace(
                utcnow=lambda: wall_clock["now"],
                parse_datetime=lambda value: datetime.fromisoformat(
                    value.replace("Z", "+00:00")
                ),
            ),
            "timedelta": __import__("datetime").timedelta,
            "timezone": timezone,
        },
    )
    expiry = ExpiryProbe()
    expiry.hass = object()
    expiry._removed = False
    expiry._platform_added = False
    expiry._state = "current"
    expiry._timeline_dependencies = canonical_timeline_dependencies(None)
    expiry._freshness_expiry_cancel = None
    expiry._freshness_expiry_generation = 0
    expiry._recompute_cancel = None
    expiry._expected_recompute_cancel = None
    expiry._last_complete_attributes = {
        "schema_version": 1,
        "output_only": True,
        "slots": [{"start": "2026-09-04T12:00:00Z"}],
    }
    expiry._attributes = dict(expiry._last_complete_attributes)
    expiry._supervisor = SimpleNamespace(
        latest_active_frame=SimpleNamespace(now=wall_clock["now"])
    )
    expiry._expected_timeline = SimpleNamespace(
        extra_state_attributes=None
    )
    expiry._timelines = {
        policy: SimpleNamespace(
            native_value="current",
            extra_state_attributes={"generated_at": "2026-09-04T12:00:00Z"},
        )
        for policy in ("rce", "tariff", "rcm")
    }
    expiry._capacity_kwh = lambda: 10.0
    expiry._schedule_freshness_expiry()
    check(
        len(expiry_records) == 1
        and abs(expiry_records[0]["delay"] - 150.1) < 1e-9,
        "canonical freshness deadline was not scheduled once",
    )
    wall_clock["now"] = wall_clock["now"].replace(
        minute=2,
        second=31,
    )
    expiry_records[0]["callback"](wall_clock["now"])
    check(
        freshness_calls == [wall_clock["now"]]
        and expiry._state == "partial"
        and expiry._freshness_expiry_cancel is None
        and len(expiry_records) == 1,
        "stale wall-clock expiry re-armed a 100 ms canonical loop",
    )


async def main() -> None:
    await _rce_contract()
    await _tariff_contract()
    await _rcm_contract()
    _retained_plan_contract()
    _canonical_contract()
    print(f"I3 policy cadence: {CHECKS} contract checks passed")


if __name__ == "__main__":
    asyncio.run(main())
