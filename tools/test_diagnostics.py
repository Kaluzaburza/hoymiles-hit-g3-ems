"""Standalone privacy and structure tests for support diagnostics."""

from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import importlib.util
from io import BytesIO
import json
from pathlib import Path
import random
import sys
import tempfile
import types
from uuid import RFC_4122, UUID
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"


class FakeStore:
    """Persistent in-memory replacement for Home Assistant Store."""

    backend: dict[str, dict] = {}
    load_count = 0
    save_count = 0
    fail_saves = False
    initialized: list[tuple[int, str]] = []

    def __init__(self, _hass, version: int, key: str) -> None:
        self.version = version
        self.key = key
        self.initialized.append((version, key))

    @classmethod
    def reset(cls) -> None:
        cls.backend = {}
        cls.load_count = 0
        cls.save_count = 0
        cls.fail_saves = False
        cls.initialized = []

    async def async_load(self):
        await asyncio.sleep(0)
        type(self).load_count += 1
        return deepcopy(type(self).backend.get(self.key))

    async def async_save(self, data) -> None:
        await asyncio.sleep(0)
        if type(self).fail_saves:
            raise OSError("simulated persistent storage failure")
        type(self).save_count += 1
        type(self).backend[self.key] = deepcopy(data)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def load_component_module(module_name: str, filename: str):
    package_name = "hoymiles_hit_modbus_test"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(COMPONENT)]
        sys.modules[package_name] = package
    full_name = f"{package_name}.{module_name}"
    path = COMPONENT / filename
    spec = importlib.util.spec_from_file_location(full_name, path)
    require(spec is not None and spec.loader is not None, f"Cannot load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


def load_installation_identity_module():
    """Load the identity helper with a behavioral Store stub."""
    homeassistant = types.ModuleType("homeassistant")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    storage = types.ModuleType("homeassistant.helpers.storage")
    core.HomeAssistant = object
    storage.Store = FakeStore
    homeassistant.core = core
    homeassistant.helpers = helpers
    helpers.storage = storage
    sys.modules["homeassistant"] = homeassistant
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.storage"] = storage

    package_name = "hoymiles_hit_modbus_test"
    if package_name not in sys.modules:
        package = types.ModuleType(package_name)
        package.__path__ = [str(COMPONENT)]
        sys.modules[package_name] = package
    const = types.ModuleType(f"{package_name}.const")
    const.DOMAIN = "hoymiles_hit_modbus"
    sys.modules[const.__name__] = const
    return load_component_module(
        "installation_identity",
        "installation_identity.py",
    )


def load_diagnostics_module():
    """Load native diagnostics with a deterministic Recorder test double."""

    context: dict[str, object] = {
        "block_executor": False,
        "calls": [],
        "executor_jobs": 0,
        "fail": False,
        "history": {},
        "release_executor": None,
        "started_executor": None,
    }

    class FakeState:
        def __init__(
            self,
            state: str,
            attributes: dict,
            observed_at: datetime,
        ) -> None:
            self.state = state
            self.attributes = attributes
            self.last_changed = observed_at
            self.last_updated = observed_at

    class FakeRecorder:
        def async_add_executor_job(self, job):
            # HA enqueues immediately and returns a Future, not a coroutine.
            context["executor_jobs"] = int(context["executor_jobs"]) + 1
            return asyncio.create_task(self._run(job))

        async def _run(self, job):
            if context["block_executor"]:
                started = context["started_executor"]
                release = context["release_executor"]
                assert isinstance(started, asyncio.Event)
                assert isinstance(release, asyncio.Event)
                started.set()
                await release.wait()
            await asyncio.sleep(0)
            return job()

    def get_significant_states(*args):
        if context["fail"]:
            raise RuntimeError("simulated Recorder failure")
        start, end = args[1], args[2]
        entity_ids = list(args[3])
        calls = context["calls"]
        assert isinstance(calls, list)
        calls.append(
            {
                "entity_ids": entity_ids,
                "significant_changes_only": args[6],
                "no_attributes": args[8],
            }
        )
        history = context["history"]
        assert isinstance(history, dict)
        return {
            entity_id: [
                item
                for item in history.get(entity_id, [])
                if not isinstance(item, FakeState)
                or start <= item.last_updated < end
            ]
            for entity_id in entity_ids
        }

    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    diagnostics_component = types.ModuleType("homeassistant.components.diagnostics")
    recorder_component = types.ModuleType("homeassistant.components.recorder")
    recorder_history = types.ModuleType("homeassistant.components.recorder.history")
    config_entries = types.ModuleType("homeassistant.config_entries")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    helpers_recorder = types.ModuleType("homeassistant.helpers.recorder")
    util = types.ModuleType("homeassistant.util")
    dt_util = types.ModuleType("homeassistant.util.dt")

    diagnostics_component.async_redact_data = lambda value, _keys: value
    recorder_history.get_significant_states = get_significant_states
    recorder_component.history = recorder_history
    config_entries.ConfigEntry = object
    core.HomeAssistant = object
    core.State = FakeState
    entity_registry.async_get = lambda _hass: types.SimpleNamespace()
    entity_registry.async_entries_for_config_entry = lambda _registry, _entry_id: []
    helpers.entity_registry = entity_registry
    helpers_recorder.get_instance = lambda _hass: FakeRecorder()
    dt_util.utcnow = lambda: datetime(2026, 9, 4, 12, tzinfo=timezone.utc)
    util.dt = dt_util
    homeassistant.components = components
    homeassistant.config_entries = config_entries
    homeassistant.core = core
    homeassistant.helpers = helpers
    homeassistant.util = util
    components.diagnostics = diagnostics_component
    components.recorder = recorder_component

    modules = {
        "homeassistant": homeassistant,
        "homeassistant.components": components,
        "homeassistant.components.diagnostics": diagnostics_component,
        "homeassistant.components.recorder": recorder_component,
        "homeassistant.components.recorder.history": recorder_history,
        "homeassistant.config_entries": config_entries,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.entity_registry": entity_registry,
        "homeassistant.helpers.recorder": helpers_recorder,
        "homeassistant.util": util,
        "homeassistant.util.dt": dt_util,
    }
    sys.modules.update(modules)

    package_name = "hoymiles_hit_modbus_test"
    const = types.ModuleType(f"{package_name}.const")
    const.CONF_RESOLVED_SOURCE_DEVICE_ID = "resolved_source_device_id"
    const.CONF_SOURCE_DEVICE_ID = "source_device_id"
    const.DOMAIN = "hoymiles_hit_modbus"
    const.VERSION = "test"
    sys.modules[const.__name__] = const
    installation_identity = types.ModuleType(
        f"{package_name}.installation_identity"
    )

    async def fake_installation_identity(_hass):
        return types.SimpleNamespace(as_dict=lambda: {})

    installation_identity.async_get_or_create_installation_identity = (
        fake_installation_identity
    )
    sys.modules[installation_identity.__name__] = installation_identity
    models = types.ModuleType(f"{package_name}.models")
    models.RuntimeData = object
    sys.modules[models.__name__] = models

    module = load_component_module("diagnostics", "diagnostics.py")
    return module, FakeState, context


async def assert_installation_identity_contract(
    identity_module,
) -> tuple[dict, dict]:
    """Prove generation, persistence, privacy and single-flight behavior."""
    expected_uuid = UUID("3f6f8b4e-7793-4f4b-9f45-486ddf65f78a")
    uuid_calls: list[tuple[tuple, dict]] = []

    def fake_uuid4(*args, **kwargs):
        uuid_calls.append((args, kwargs))
        return expected_uuid

    identity_module.uuid4 = fake_uuid4
    FakeStore.reset()
    private_markers = (
        "AA:BB:CC:DD:EE:FF",
        "192.0.2.10",
        "private-host",
        "device-id-private",
        "entry-id-private",
        "user-id-private",
        "serial-private",
    )

    def fake_hass():
        return types.SimpleNamespace(
            data={
                "device_id": private_markers[3],
                "entry_id": private_markers[4],
                "user_id": private_markers[5],
                "serial": private_markers[6],
                "network": [private_markers[0], private_markers[1]],
            },
            config=types.SimpleNamespace(
                location_name=private_markers[2],
            ),
        )

    first_hass = fake_hass()
    first = await identity_module.async_get_or_create_installation_identity(
        first_hass
    )
    first_payload = first.as_dict()
    require(
        first.anonymous_installation_id == str(expected_uuid),
        "Generated identity is not the direct output of uuid4()",
    )
    require(FakeStore.save_count == 1, "First start did not save identity once")
    require(len(uuid_calls) == 1, "First start did not generate exactly one UUID")
    require(
        uuid_calls[0] == ((), {}),
        "UUID factory received installation-derived arguments",
    )
    require(
        set(first_payload)
        == {
            "anonymous_installation_id",
            "installation_id_schema_version",
        },
        "Store payload contains fields outside the anonymous identity contract",
    )
    require(
        first_payload["installation_id_schema_version"] == 1,
        "Installation identity schema is not version 1",
    )
    require(
        FakeStore.backend[identity_module.INSTALLATION_ID_STORAGE_KEY]
        == first_payload,
        "Persisted identity differs from the published identity",
    )
    require(
        FakeStore.initialized[-1]
        == (1, "hoymiles_hit_modbus.installation_identity"),
        "Identity Store key is not installation-wide and stable",
    )

    # A new hass object simulates a full Home Assistant restart while Store
    # retains its on-disk data.
    restarted_hass = fake_hass()
    restarted = (
        await identity_module.async_get_or_create_installation_identity(
            restarted_hass
        )
    )
    require(
        restarted == first,
        "Home Assistant restart changed the anonymous installation ID",
    )
    require(FakeStore.save_count == 1, "Restart rewrote the persistent ID")
    require(len(uuid_calls) == 1, "Restart generated a replacement UUID")

    parsed = UUID(first.anonymous_installation_id)
    require(parsed.version == 4, "Installation ID is not UUID v4")
    require(parsed.variant == RFC_4122, "Installation ID is not RFC 4122")
    require(
        str(parsed) == first.anonymous_installation_id,
        "Installation ID is not in canonical UUID form",
    )
    serialized_identity = json.dumps(first_payload)
    for marker in private_markers:
        require(
            marker not in serialized_identity,
            f"Installation-derived value leaked into anonymous ID: {marker}",
        )

    # Multiple config entries call the same installation-wide getter and must
    # never receive per-device IDs.
    entry_a = await identity_module.async_get_or_create_installation_identity(
        restarted_hass
    )
    entry_b = await identity_module.async_get_or_create_installation_identity(
        restarted_hass
    )
    require(entry_a == entry_b == first, "Config entries received different IDs")

    # Concurrent first requests must serialize load/create/save and publish
    # only the one successfully persisted value.
    FakeStore.reset()
    uuid_calls.clear()
    concurrent_hass = fake_hass()
    concurrent = await asyncio.gather(
        *(
            identity_module.async_get_or_create_installation_identity(
                concurrent_hass
            )
            for _ in range(12)
        )
    )
    require(len(set(concurrent)) == 1, "Concurrent requests produced multiple IDs")
    require(FakeStore.save_count == 1, "Concurrent requests saved more than once")
    require(len(uuid_calls) == 1, "Concurrent requests generated multiple UUIDs")

    # Invalid persisted data must not be exported as the anonymous ID.
    FakeStore.reset()
    FakeStore.backend[identity_module.INSTALLATION_ID_STORAGE_KEY] = {
        "anonymous_installation_id": private_markers[6],
        "installation_id_schema_version": 1,
    }
    uuid_calls.clear()
    repaired = await identity_module.async_get_or_create_installation_identity(
        fake_hass()
    )
    require(repaired == first, "Invalid stored ID was not replaced by UUID v4")
    require(FakeStore.save_count == 1, "Invalid stored ID was not persisted safely")
    require(len(uuid_calls) == 1, "Invalid stored ID did not regenerate once")

    # JSON booleans and floats compare equal to integer 1 in Python, but are
    # not valid schema-version integers and must be repaired, never exported.
    for invalid_schema in (True, 1.0):
        FakeStore.reset()
        FakeStore.backend[identity_module.INSTALLATION_ID_STORAGE_KEY] = {
            "anonymous_installation_id": str(expected_uuid),
            "installation_id_schema_version": invalid_schema,
        }
        uuid_calls.clear()
        repaired_schema = (
            await identity_module.async_get_or_create_installation_identity(
                fake_hass()
            )
        )
        require(
            repaired_schema.installation_id_schema_version == 1
            and type(repaired_schema.installation_id_schema_version) is int,
            f"Invalid schema type was exported: {invalid_schema!r}",
        )
        require(
            FakeStore.save_count == 1 and len(uuid_calls) == 1,
            f"Invalid schema type was not regenerated: {invalid_schema!r}",
        )

    # A future schema must never be silently downgraded or overwritten.
    FakeStore.reset()
    future_payload = {
        "anonymous_installation_id": str(expected_uuid),
        "installation_id_schema_version": 2,
    }
    FakeStore.backend[identity_module.INSTALLATION_ID_STORAGE_KEY] = deepcopy(
        future_payload
    )
    uuid_calls.clear()
    try:
        await identity_module.async_get_or_create_installation_identity(
            fake_hass()
        )
    except identity_module.UnsupportedInstallationIdentitySchemaError:
        pass
    else:
        raise RuntimeError("Unknown future identity schema was accepted")
    require(FakeStore.save_count == 0, "Future identity schema was overwritten")
    require(len(uuid_calls) == 0, "Future identity schema generated a new UUID")
    require(
        FakeStore.backend[identity_module.INSTALLATION_ID_STORAGE_KEY]
        == future_payload,
        "Future identity schema changed on downgrade",
    )

    # A failed persistent write must not publish/cache an ephemeral identity.
    # A later retry must perform a fresh generation and persist it successfully.
    FakeStore.reset()
    FakeStore.fail_saves = True
    uuid_calls.clear()
    failing_hass = fake_hass()
    try:
        await identity_module.async_get_or_create_installation_identity(
            failing_hass
        )
    except OSError as err:
        require(
            str(err) == "simulated persistent storage failure",
            "Unexpected storage error escaped the identity getter",
        )
    else:
        raise RuntimeError("Failed Store write published an ephemeral identity")
    require(
        identity_module._DATA_IDENTITY not in failing_hass.data,
        "Unsaved anonymous ID entered the Home Assistant cache",
    )
    require(FakeStore.save_count == 0, "Failed Store write counted as persisted")
    require(
        identity_module.INSTALLATION_ID_STORAGE_KEY not in FakeStore.backend,
        "Failed Store write changed persistent data",
    )
    require(len(uuid_calls) == 1, "Failed first write did not generate once")

    FakeStore.fail_saves = False
    retried = await identity_module.async_get_or_create_installation_identity(
        failing_hass
    )
    require(retried == first, "Storage recovery published a different UUID value")
    require(FakeStore.save_count == 1, "Storage recovery did not persist identity")
    require(len(uuid_calls) == 2, "Storage recovery reused an unsaved UUID")
    return first_payload, restarted.as_dict()


def assert_canonical_sensor_recorder_contract() -> None:
    """Keep the full canonical slot ledger out of Home Assistant Recorder."""

    source = (
        COMPONENT / "supervisor_canonical_sensor.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    sensor_class = next(
        (
            statement
            for statement in tree.body
            if isinstance(statement, ast.ClassDef)
            and statement.name == "HoymilesSupervisorCanonicalPlanSensor"
        ),
        None,
    )
    require(sensor_class is not None, "Canonical-plan sensor class is missing")
    assignment = next(
        (
            statement.value
            for statement in sensor_class.body
            if isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "_unrecorded_attributes"
                for target in statement.targets
            )
        ),
        None,
    )
    require(
        isinstance(assignment, ast.Call)
        and isinstance(assignment.func, ast.Name)
        and assignment.func.id == "frozenset"
        and len(assignment.args) == 1
        and not assignment.keywords,
        "Canonical sensor does not declare a bounded Recorder exclusion",
    )
    unrecorded_attributes = ast.literal_eval(assignment.args[0])
    require(
        unrecorded_attributes == {"slots"},
        "Canonical sensor must exclude exactly the full slots array from Recorder",
    )


async def assert_control_history_contract(
    diagnostics,
    fake_state_type,
    context: dict[str, object],
) -> None:
    """Prove allowlists, Recorder partitioning and honest bounded coverage."""

    observed_at = datetime(2026, 9, 4, 10, tzinfo=timezone.utc)
    rce_entity = "sensor.hoymiles_hit_rce_optimized_plan"
    tariff_entity = "sensor.hoymiles_hit_tariff_charge_plan"
    rcm_entity = "sensor.hoymiles_hit_rcm_voltage_plan"
    timeline_entity = "sensor.hoymiles_hit_rce_automation_plan_timeline"
    automation_entity = "automation.hoymiles_rce_grid_discharge_control"
    supervisor_entity = "sensor.hoymiles_hit_ems_supervisor"
    canonical_entity = "sensor.hoymiles_hit_ems_supervisor_canonical_plan"
    balancing_entity = "sensor.hoymiles_battery_balancing_transaction"
    owner_entity = "sensor.hoymiles_ems_control_owner"
    outbox_entity = "sensor.hoymiles_battery_balancing_notification_outbox"
    regular_entity = "sensor.hoymiles_ems_hardware_mode"

    expected_profiles = {
        rce_entity: "rce_planner",
        tariff_entity: "tariff_planner",
        rcm_entity: "rcm_planner",
        timeline_entity: "timeline",
        automation_entity: "automation",
        supervisor_entity: "supervisor",
        canonical_entity: "canonical_plan",
        balancing_entity: "balancing_transaction",
        owner_entity: "owner",
        outbox_entity: "balancing_outbox",
        diagnostics.AGGREGATE_RESPONSE_ENTITY_ID: "aggregate_response",
    }
    for entity_id, profile in expected_profiles.items():
        require(
            diagnostics._history_attribute_profile(entity_id) == profile,
            f"Missing rich-history profile for {entity_id}",
        )
        require(
            diagnostics._needs_history(entity_id),
            f"Profiled entity was excluded from Recorder history: {entity_id}",
        )
    require(
        diagnostics._history_attribute_profile(regular_entity) is None,
        "State-only physical context unexpectedly enables attributes",
    )

    physical_context = {
        "sensor.hoymiles_hit_ems_mode_readback_code",
        "sensor.hoymiles_hit_ems_self_use_soc_readback",
        "sensor.hoymiles_hit_ems_backup_soc_readback",
        "sensor.hoymiles_hit_ems_force_charge_soc_readback",
        "sensor.hoymiles_hit_ems_maximum_charge_power_readback",
        "sensor.hoymiles_hit_ems_force_discharge_soc_readback",
        "sensor.hoymiles_hit_ems_maximum_discharge_power_readback",
        "sensor.hoymiles_hit_gcf_enable_readback_code",
        "sensor.hoymiles_hit_gcf_maximum_export_power_readback",
        "sensor.hoymiles_hit_battery_max_charge_power_readback",
        "sensor.hoymiles_hit_ems_control_readback_generation",
        "sensor.hoymiles_hit_gcf_control_readback_generation",
        "sensor.hoymiles_hit_battery_charge_power_readback_generation",
        "sensor.hoymiles_hit_parallel_aggregate_power_readback_generation",
        "sensor.hoymiles_hit_parallel_topology_readback_generation",
    }
    require(
        physical_context <= diagnostics.HISTORY_CONTEXT_SENSOR_ENTITY_IDS,
        "Physical 4300-4306/258/259/306 readbacks or generations are missing",
    )
    for entity_id in physical_context:
        require(
            diagnostics._needs_history(entity_id)
            and diagnostics._history_attribute_profile(entity_id) is None,
            f"Physical readback lost its state-only history contract: {entity_id}",
        )

    rce_event = diagnostics._serialize_history_item(
        fake_state_type(
            "current",
            {
                "status_code": "ready",
                "input_revision": 17,
                "pending_input_revision": 18,
                "last_full_plan_trigger": "periodic",
                "result_current": True,
                "recalculation_pending": False,
                "current_slot_planned": True,
                "current_slot_start_eligible": True,
                "current_slot_continue_eligible": True,
                "current_slot_execution_power_percent": 37,
                "current_slot_planned_export_kwh": 1.25,
                "planned_export_kwh": 4.5,
                "current_price_pln_kwh": 0.88,
                "minimum_soc": 25,
                "bms_discharge_data_fresh": True,
                "forecast_today_data_fresh": True,
                "gcf_execution_data_fresh": True,
                "points": [{"private": "large plan"}],
                "config_entry_id": "private-entry",
                "api_key": "private-secret",
            },
            observed_at,
        ),
        entity_id=rce_entity,
    )
    require(
        rce_event["event_schema_version"] == 1
        and rce_event["attribute_profile"] == "rce_planner"
        and rce_event["attributes_available"] is True,
        "RCE event lacks its independent schema/profile marker",
    )
    require(
        rce_event["attributes"]
        == {
            "bms_discharge_data_fresh": True,
            "current_price_pln_kwh": 0.88,
            "current_slot_continue_eligible": True,
            "current_slot_execution_power_percent": 37,
            "current_slot_planned": True,
            "current_slot_planned_export_kwh": 1.25,
            "current_slot_start_eligible": True,
            "forecast_today_data_fresh": True,
            "gcf_execution_data_fresh": True,
            "input_revision": 17,
            "last_full_plan_trigger": "periodic",
            "minimum_soc": 25,
            "pending_input_revision": 18,
            "planned_export_kwh": 4.5,
            "recalculation_pending": False,
            "result_current": True,
            "status_code": "ready",
        },
        "RCE history leaked a plan or discarded compact decision evidence",
    )

    canonical_event = diagnostics._serialize_history_item(
        fake_state_type(
            "current",
            {
                "schema_version": 1,
                "built_at": "2026-09-04T10:00:00Z",
                "arbitration_revision": "arbitration-revision-47",
                "ledger_revision": "ledger-revision-47",
                "projection_contract": "dual_soc_v1",
                "authorization_projection": "conservative_fail_closed",
                "expected_projection": "p50_observation_only",
                "expected_projection_quality": "complete",
                "authorization_final_soc_percent": 36.4,
                "expected_final_soc_percent": 61.7,
                "expected_min_soc_percent": 25.9,
                "authorization_grid_export_kwh": 4.25,
                "expected_grid_export_kwh": 12.5,
                "expected_pv_curtailed_kwh": 2.75,
                "authorization_reserve_violation_count": 0,
                "slots": [
                    {
                        "slot_id": index,
                        "soc_equation": {
                            "expected_soc_end_percent": 61.7,
                            "authorization_soc_end_percent": 36.4,
                        },
                    }
                    for index in range(192)
                ],
                "output_only": True,
                "usable_capacity_kwh": 230.0,
                "initial_soc_percent": 25.9,
                "final_soc_percent": 36.4,
                "audit": {"reserve_violation_count": 0},
                "unknown_future_field": "must-not-enter-history",
                "oversized_unknown_field": "x" * 100_000,
            },
            observed_at,
        ),
        entity_id=canonical_entity,
    )
    require(
        canonical_event["event_schema_version"] == 1
        and canonical_event["attribute_profile"] == "canonical_plan"
        and canonical_event["attributes_available"] is True,
        "Canonical event lacks its independent schema/profile marker",
    )
    canonical_expected_attributes = {
        "arbitration_revision": "arbitration-revision-47",
        "authorization_final_soc_percent": 36.4,
        "authorization_grid_export_kwh": 4.25,
        "authorization_projection": "conservative_fail_closed",
        "authorization_reserve_violation_count": 0,
        "built_at": "2026-09-04T10:00:00Z",
        "expected_final_soc_percent": 61.7,
        "expected_grid_export_kwh": 12.5,
        "expected_min_soc_percent": 25.9,
        "expected_projection": "p50_observation_only",
        "expected_projection_quality": "complete",
        "expected_pv_curtailed_kwh": 2.75,
        "ledger_revision": "ledger-revision-47",
        "projection_contract": "dual_soc_v1",
        "schema_version": 1,
    }
    require(
        set(canonical_event["attributes"])
        == set(canonical_expected_attributes),
        "Canonical history leaked slots/full ledger data or lost compact dual-track evidence",
    )
    require(
        canonical_event["attributes"] == canonical_expected_attributes,
        "Canonical compact expected/authorization values were changed or redacted",
    )

    original_event_limit = diagnostics.MAX_HISTORY_EVENT_BYTES
    diagnostics.MAX_HISTORY_EVENT_BYTES = 512
    try:
        oversized_canonical, canonical_omitted = diagnostics._bounded_history_item(
            diagnostics._serialize_history_item(
                fake_state_type(
                    "current",
                    {
                        "schema_version": 1,
                        "expected_projection_quality": "quality " * 400,
                    },
                    observed_at,
                ),
                entity_id=canonical_entity,
            )
        )
    finally:
        diagnostics.MAX_HISTORY_EVENT_BYTES = original_event_limit
    require(
        canonical_omitted
        and oversized_canonical["attributes_available"] is False
        and oversized_canonical["omission_reason"] == "event_size_limit"
        and "attributes" not in oversized_canonical,
        "Oversize canonical summary was partially retained",
    )

    tariff_event = diagnostics._serialize_history_item(
        fake_state_type(
            "charging",
            {
                "current_action": "force_charge",
                "current_run_start_eligible": True,
                "current_run_continue_eligible": True,
                "current_run_benefit_pln": 1.72,
                "command_charge_power_percent": 41,
                "target_soc_percent": 82,
                "control_inputs_fresh": True,
                "bms_charge_data_fresh": True,
                "live_power_data_fresh": True,
                "load_profile_data_fresh": True,
                "planned_slots": [{"private": "large plan"}],
            },
            observed_at,
        ),
        entity_id=tariff_entity,
    )
    require(
        tariff_event["attributes"]
        == {
            "bms_charge_data_fresh": True,
            "command_charge_power_percent": 41,
            "control_inputs_fresh": True,
            "current_action": "force_charge",
            "current_run_benefit_pln": 1.72,
            "current_run_continue_eligible": True,
            "current_run_start_eligible": True,
            "live_power_data_fresh": True,
            "load_profile_data_fresh": True,
            "target_soc_percent": 82,
        },
        "Tariff history leaked slots or discarded compact command evidence",
    )

    rcm_event = diagnostics._serialize_history_item(
        fake_state_type(
            "emergency",
            {
                "action": "emergency_charge",
                "live_emergency": True,
                "emergency_action_ready": True,
                "recommended_charge_limit_percent": 35,
                "recommended_export_limit_percent": 0,
                "pre_discharge_start_eligible": False,
                "pre_discharge_continue_eligible": True,
                "voltage_data_fresh": True,
                "bms_charge_data_fresh": True,
                "actuator_data_fresh": True,
                "discharge_registers_data_fresh": True,
                "ems_mode_data_fresh": True,
                "pre_discharge_actuator_data_fresh": True,
                "prediction_block_reason": "none",
                "reason": "voltage_high",
                "live_control_updated_at": "volatile-private-timestamp",
                "points": [{"private": "large plan"}],
            },
            observed_at,
        ),
        entity_id=rcm_entity,
    )
    require(
        rcm_event["attributes"]
        == {
            "action": "emergency_charge",
            "actuator_data_fresh": True,
            "bms_charge_data_fresh": True,
            "discharge_registers_data_fresh": True,
            "emergency_action_ready": True,
            "ems_mode_data_fresh": True,
            "live_emergency": True,
            "pre_discharge_actuator_data_fresh": True,
            "pre_discharge_continue_eligible": True,
            "pre_discharge_start_eligible": False,
            "prediction_block_reason": "none",
            "recommended_charge_limit_percent": 35,
            "recommended_export_limit_percent": 0,
            "reason": "voltage_high",
            "voltage_data_fresh": True,
        },
        "RCEm history retained volatile/full-plan data or lost live decisions",
    )

    timeline_event = diagnostics._serialize_history_item(
        fake_state_type(
            "pending",
            {
                "schema_version": 2,
                "policy_id": "rce",
                "plan_revision": 9,
                "pending_input_revision": 18,
                "current_actual": {
                    "action": "export",
                    "power_kw": 2.1,
                },
                "points": list(range(100)),
                "sources": [{"entity_id": "sensor.private"}],
                "plan_entity_id": rce_entity,
                "config_entry_id": "private-entry",
            },
            observed_at,
        ),
        entity_id=timeline_entity,
    )
    require(
        timeline_event["attributes"]
        == {
            "current_actual": {
                "action": "export",
                "power_kw": 2.1,
            },
            "pending_input_revision": 18,
            "plan_revision": 9,
            "policy_id": "rce",
            "schema_version": 2,
        },
        "Timeline history exported points, provenance or entry identity",
    )

    automation_event = diagnostics._serialize_history_item(
        fake_state_type(
            "on",
            {
                "current": 2,
                "last_triggered": "2026-09-04T09:59:00+00:00",
                "mode": "restart",
                "friendly_name": "Private installation automation",
                "entity_picture": "https://private.example/automation.png",
            },
            observed_at,
        ),
        entity_id=automation_entity,
    )
    require(
        automation_event["attributes"]
        == {
            "current": 2,
            "last_triggered": "2026-09-04T09:59:00+00:00",
            "mode": "restart",
        },
        "Automation history lost last_triggered or exported presentation data",
    )

    supervisor_event = diagnostics._serialize_history_item(
        fake_state_type(
            "verifying",
            {
                "executor_revision": 21,
                "transaction_id": "tx-21",
                "rollback_status": "pending",
                "transaction_evidence": {
                    "readback_result": "pending",
                    "token": "private-token",
                },
                "friendly_name": "Private installation",
            },
            observed_at,
        ),
        entity_id=supervisor_entity,
    )
    require(
        supervisor_event["attributes"]["executor_revision"] == 21
        and supervisor_event["attributes"]["transaction_id"] == "tx-21"
        and supervisor_event["attributes"]["transaction_evidence"][
            "readback_result"
        ]
        == "pending",
        "Supervisor transaction evidence was not retained",
    )
    require(
        supervisor_event["attributes"]["transaction_evidence"]["token"]
        == "[REDACTED]",
        "Nested Supervisor transaction secret bypassed redaction",
    )

    balancing_event = diagnostics._serialize_history_item(
        fake_state_type(
            "RESTORING",
            {
                "cycle_id": "c7",
                "transaction_state": "RESTORING",
                "snapshot_ems_generation": 40,
                "current_ems_generation": 42,
                "serialized_length": 200,
                "raw_lifecycle": "private",
            },
            observed_at,
        ),
        entity_id=balancing_entity,
    )
    require(
        balancing_event["attributes"]
        == {
            "current_ems_generation": 42,
            "cycle_id": "c7",
            "snapshot_ems_generation": 40,
            "transaction_state": "RESTORING",
        },
        "Balancing history did not enforce its parsed lifecycle allowlist",
    )
    outbox_event = diagnostics._serialize_history_item(
        fake_state_type(
            "VALID",
            {
                "slot_1_event_id": "c7.ST",
                "slot_1_delivery_state": "DELIVERED",
                "slot_1_attempt_count": 1,
                "slot_1_stable_tag": "bb.c7.ST",
            },
            observed_at,
        ),
        entity_id=outbox_entity,
    )
    require(
        "slot_1_stable_tag" not in outbox_event["attributes"],
        "Redundant notification target/tag leaked into history",
    )

    # Oversize rich evidence is omitted atomically instead of being partially
    # truncated into a potentially misleading transaction record.
    original_event_limit = diagnostics.MAX_HISTORY_EVENT_BYTES
    diagnostics.MAX_HISTORY_EVENT_BYTES = 512
    try:
        oversized, omitted = diagnostics._bounded_history_item(
            diagnostics._serialize_history_item(
                fake_state_type(
                    "verifying",
                    {"transaction_evidence": {"samples": list(range(300))}},
                    observed_at,
                ),
                entity_id=supervisor_entity,
            )
        )
    finally:
        diagnostics.MAX_HISTORY_EVENT_BYTES = original_event_limit
    require(omitted, "Oversize transaction evidence was not bounded")
    require(
        oversized["attributes_available"] is False
        and oversized["omission_reason"] == "event_size_limit"
        and "attributes" not in oversized,
        "Oversize evidence was partially retained",
    )

    history = context["history"]
    calls = context["calls"]
    assert isinstance(history, dict) and isinstance(calls, list)
    history.clear()
    calls.clear()
    history[regular_entity] = [
        fake_state_type("self_use", {}, observed_at),
    ]
    history[rce_entity] = [
        fake_state_type(
            "current",
            {"status_code": "ready", "points": list(range(100))},
            observed_at + timedelta(minutes=1),
        ),
    ]
    result = await diagnostics._async_history(
        object(),
        [regular_entity, rce_entity],
    )
    require(result["available"] and result["complete"], "History is incomplete")
    require(
        result["event_schema_version"] == 1
        and result["limits"]["max_events_total"] == 4_000,
        "History omitted its independent schema or limits",
    )
    regular_calls = [call for call in calls if call["no_attributes"]]
    profiled_calls = [call for call in calls if not call["no_attributes"]]
    require(
        regular_calls
        == [
            {
                "entity_ids": [regular_entity],
                "significant_changes_only": True,
                "no_attributes": True,
            }
        ],
        "State-only Recorder query changed its cheap historical contract",
    )
    expected_profiled_windows = (
        diagnostics.HISTORY_HOURS
        // diagnostics.PROFILED_HISTORY_QUERY_WINDOW_HOURS
    )
    require(
        len(profiled_calls) == expected_profiled_windows
        and result["profiled_query_window_count"] == expected_profiled_windows
        and all(
            call
            == {
                "entity_ids": [rce_entity],
                "significant_changes_only": False,
                "no_attributes": False,
            }
            for call in profiled_calls
        ),
        "Rich Recorder history was not read in bounded attribute windows",
    )

    # Fast RCEm timestamps must be discarded before count limiting so they do
    # not push an earlier semantic decision out of a one-day history.
    semantic_start = datetime(2026, 9, 3, 13, tzinfo=timezone.utc)
    history.clear()
    calls.clear()
    history[rcm_entity] = [
        fake_state_type(
            "current",
            {
                "action": "hold" if index < 300 else "emergency_charge",
                "result_current": True,
                "recalculation_pending": False,
                "live_control_updated_at": (
                    semantic_start + timedelta(minutes=index * 2)
                ).isoformat(),
            },
            semantic_start + timedelta(minutes=index * 2),
        )
        for index in range(600)
    ]
    compacted = await diagnostics._async_history(object(), [rcm_entity])
    require(
        compacted["available"] is True
        and compacted["complete"] is True
        and compacted["queried_event_count"] == 600
        and compacted["semantic_duplicate_count"] == 598
        and compacted["returned_event_count"] == 2
        and compacted["dropped_event_count"] == 0,
        "Volatile RCEm updates displaced compact semantic history",
    )
    require(
        [
            item["attributes"]["action"]
            for item in compacted["entities"][rcm_entity]
        ]
        == ["hold", "emergency_charge"]
        and all(
            "live_control_updated_at" not in item["attributes"]
            for item in compacted["entities"][rcm_entity]
        ),
        "RCEm semantic compaction retained volatile timestamps or lost a day",
    )

    # An existing selected entity with no Recorder rows is missing evidence;
    # it must never be advertised as a complete, healthy 24-hour history.
    history.clear()
    calls.clear()
    unknown = await diagnostics._async_history(object(), [regular_entity])
    unknown_coverage = unknown["entity_coverage"][regular_entity]
    require(
        unknown["available"] is True
        and unknown["complete"] is False
        and unknown["returned_event_count"] == 0
        and unknown_coverage["evidence_present"] is False
        and unknown_coverage["evidence_status"]
        == "unknown_no_recorder_rows"
        and regular_entity in unknown["incomplete_entities"]
        and regular_entity not in unknown["truncated_entities"],
        "Zero Recorder rows were presented as complete evidence",
    )

    # Exercise per-entity plus global count truncation with tiny temporary
    # limits, avoiding large test artifacts.
    history.clear()
    calls.clear()
    original_per_entity = diagnostics.MAX_HISTORY_EVENTS_PER_ENTITY
    original_total = diagnostics.MAX_HISTORY_EVENTS_TOTAL
    original_bytes = diagnostics.MAX_HISTORY_SERIALIZED_BYTES
    diagnostics.MAX_HISTORY_EVENTS_PER_ENTITY = 2
    diagnostics.MAX_HISTORY_EVENTS_TOTAL = 2
    diagnostics.MAX_HISTORY_SERIALIZED_BYTES = 1024 * 1024
    history[regular_entity] = [
        fake_state_type(
            f"mode-{index}",
            {},
            observed_at + timedelta(minutes=index),
        )
        for index in range(3)
    ]
    history[rce_entity] = [
        fake_state_type(
            "current",
            {"input_revision": index},
            observed_at + timedelta(minutes=3 + index),
        )
        for index in range(2)
    ]
    calls.clear()
    try:
        bounded = await diagnostics._async_history(
            object(),
            [regular_entity, rce_entity],
        )
    finally:
        diagnostics.MAX_HISTORY_EVENTS_PER_ENTITY = original_per_entity
        diagnostics.MAX_HISTORY_EVENTS_TOTAL = original_total
        diagnostics.MAX_HISTORY_SERIALIZED_BYTES = original_bytes
    require(
        bounded["available"] is True
        and bounded["complete"] is False
        and bounded["returned_event_count"] == 2
        and bounded["dropped_event_count"] == 3,
        "Bounded history did not report exact retained/dropped counts",
    )
    require(
        bounded["entities"][regular_entity] == []
        and len(bounded["entities"][rce_entity]) == 2,
        "Global limit did not retain the newest deterministic suffix",
    )
    require(
        bounded["entity_coverage"][regular_entity]["dropped_count"] == 3,
        "Per-entity coverage concealed truncation",
    )

    context["fail"] = True
    try:
        unavailable = await diagnostics._async_history(
            object(),
            [rce_entity],
        )
    finally:
        context["fail"] = False
    require(
        unavailable["available"] is False
        and unavailable["complete"] is False
        and unavailable["event_schema_version"] == 1
        and unavailable["error_type"] == "RuntimeError",
        "Recorder failure was not reported fail-honestly",
    )

    # A timed-out executor job cannot be cancelled by asyncio.  The exporter
    # must therefore retain ownership of that survivor and reject retries
    # until it really finishes, rather than queueing unbounded Recorder work.
    original_query_timeout = diagnostics.HISTORY_QUERY_TIMEOUT_SECONDS
    original_total_timeout = diagnostics.HISTORY_COLLECTION_TIMEOUT_SECONDS
    started = asyncio.Event()
    release = asyncio.Event()
    context["started_executor"] = started
    context["release_executor"] = release
    context["block_executor"] = True
    context["executor_jobs"] = 0
    diagnostics.HISTORY_QUERY_TIMEOUT_SECONDS = 0.01
    diagnostics.HISTORY_COLLECTION_TIMEOUT_SECONDS = 0.02
    timeout_hass = object()
    try:
        timed_out = await diagnostics._async_history(
            timeout_hass, [regular_entity]
        )
        await asyncio.wait_for(started.wait(), timeout=0.1)
        retry_started = asyncio.get_running_loop().time()
        busy = await diagnostics._async_history(timeout_hass, [regular_entity])
        retry_elapsed = asyncio.get_running_loop().time() - retry_started
        require(
            timed_out["complete"] is False
            and timed_out["omission_reason"] == "history_query_timeout"
            and busy["complete"] is False
            and busy["omission_reason"] == "history_query_busy"
            and retry_elapsed < 0.05
            and context["executor_jobs"] == 1,
            "Timed-out Recorder survivor allowed an unbounded concurrent retry",
        )
        release.set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        context["block_executor"] = False
        recovered = await diagnostics._async_history(
            timeout_hass, [regular_entity]
        )
        require(
            recovered["error_type"] if "error_type" in recovered else True,
            "Recovered history returned an invalid result",
        )
        require(
            context["executor_jobs"] == 2,
            "Recorder query ownership did not recover after survivor completion",
        )
    finally:
        release.set()
        context["block_executor"] = False
        diagnostics.HISTORY_QUERY_TIMEOUT_SECONDS = original_query_timeout
        diagnostics.HISTORY_COLLECTION_TIMEOUT_SECONDS = original_total_timeout


def assert_support_archive_bounds_contract(bundle, identity: dict) -> None:
    """Prove bounded collection, newest-log tails and atomic omissions."""

    def build(reports, log_path: Path) -> bytes:
        return bundle.build_support_archive(
            reports,
            log_path=log_path,
            generated_at="2026-09-04T12:00:00+00:00",
            home_assistant_version="2026.9.0",
            **identity,
        )

    def read_members(archive_bytes: bytes) -> dict[str, bytes]:
        with ZipFile(BytesIO(archive_bytes)) as archive:
            require(archive.testzip() is None, "Bounded diagnostics ZIP is corrupt")
            return {name: archive.read(name) for name in archive.namelist()}

    with tempfile.TemporaryDirectory(prefix="hoymiles_bundle_bounds_") as tmp:
        temp_path = Path(tmp)
        log_path = temp_path / "home-assistant.log"
        log_path.write_text(
            "unrelated source=cloud token=source-secret\n"
            "Commerce API token=commerce-secret\n"
            "items=7 password=items-secret\n"
            + "".join(
                f"[I][hoymiles] event {index:04d}\n"
                for index in range(2_600)
            ),
            encoding="utf-8",
        )
        newest_tail = bundle._relevant_log_lines(log_path)
        tail_lines = newest_tail.splitlines()
        require(
            len(tail_lines) == bundle.MAX_LOG_LINES
            and tail_lines[0].endswith("event 0100")
            and tail_lines[-1].endswith("event 2599"),
            "Relevant logs did not retain exactly the newest 2,500 lines",
        )
        for unrelated_secret in (
            "source-secret",
            "commerce-secret",
            "items-secret",
        ):
            require(
                unrelated_secret not in newest_tail,
                "A substring match admitted an unrelated private log line",
            )

        original_log_member_bytes = bundle.MAX_LOG_MEMBER_BYTES
        bundle.MAX_LOG_MEMBER_BYTES = 220
        try:
            byte_bounded_tail = bundle._relevant_log_lines(log_path)
        finally:
            bundle.MAX_LOG_MEMBER_BYTES = original_log_member_bytes
        require(
            len(byte_bounded_tail.encode("utf-8")) <= 220
            and byte_bounded_tail.startswith("[OMITTED:")
            and "event 2599" in byte_bounded_tail
            and "event 0100" not in byte_bounded_tail,
            "Log-member budget did not retain a complete newest suffix",
        )
        require(
            all(
                line.startswith("[I][hoymiles] event ")
                for line in byte_bounded_tail.splitlines()[1:]
            ),
            "Log-member budget retained a partial line",
        )

        missing_log = temp_path / "missing.log"
        seen: list[int] = []

        def many_reports():
            for index in range(10):
                seen.append(index)
                yield {"report_name": f"entry-{index}", "soc": index}

        original_max_reports = bundle.MAX_REPORTS
        bundle.MAX_REPORTS = 2
        try:
            count_bounded_archive = build(many_reports(), missing_log)
        finally:
            bundle.MAX_REPORTS = original_max_reports
        count_members = read_members(count_bounded_archive)
        count_environment = json.loads(count_members["environment.json"])
        count_reports = json.loads(count_members["hoymiles_diagnostics.json"])
        require(
            seen == [0, 1, 2],
            "Report iterator was consumed beyond the bounded one-item probe",
        )
        require(
            len(count_reports) == 3
            and count_reports[-1]["omission_reason"] == "report_count_limit"
            and count_environment["report_count"] == 3
            and count_environment["exported_report_count"] == 2
            and count_environment["report_collection_complete"] is False
            and count_environment["archive_complete"] is False,
            "Report-count truncation was not represented honestly",
        )

        original_report_bytes = bundle.MAX_REPORT_BYTES
        bundle.MAX_REPORT_BYTES = 512
        try:
            report_bounded_archive = build(
                [
                    {"report_name": "small-before", "soc": 60},
                    {
                        "report_name": "oversized",
                        "payload": "large-private-fragment!" * 80,
                        "client_secret": "must-not-survive",
                    },
                    {"report_name": "small-after", "soc": 61},
                ],
                missing_log,
            )
        finally:
            bundle.MAX_REPORT_BYTES = original_report_bytes
        report_members = read_members(report_bounded_archive)
        report_environment = json.loads(report_members["environment.json"])
        bounded_reports = json.loads(report_members["hoymiles_diagnostics.json"])
        require(
            bounded_reports[0]["report_name"] == "small-before"
            and bounded_reports[1]["omission_reason"] == "report_size_limit"
            and bounded_reports[2]["report_name"] == "small-after",
            "Oversized report was not replaced atomically between valid reports",
        )
        require(
            b"large-private-fragment" not in report_members[
                "hoymiles_diagnostics.json"
            ]
            and report_environment["omitted_report_count_at_least"] == 1
            and report_environment["report_collection_complete"] is False
            and report_environment["archive_complete"] is False,
            "Atomic report omission leaked content or claimed complete evidence",
        )

        partial_history_archive = build(
            [
                {
                    "report_name": "partial-history",
                    "control_history": {
                        "available": True,
                        "complete": False,
                        "omission_reason": "history_query_timeout",
                    },
                }
            ],
            missing_log,
        )
        partial_history_members = read_members(partial_history_archive)
        partial_history_environment = json.loads(
            partial_history_members["environment.json"]
        )
        require(
            partial_history_environment["report_collection_complete"] is True
            and partial_history_environment["report_content_complete"] is False
            and partial_history_environment["incomplete_history_report_count"]
            == 1
            and partial_history_environment["archive_complete"] is False,
            "Internal history timeout was hidden behind a complete ZIP marker",
        )

        original_diagnostics_bytes = bundle.MAX_DIAGNOSTICS_MEMBER_BYTES
        bundle.MAX_DIAGNOSTICS_MEMBER_BYTES = 1800
        try:
            member_bounded_archive = build(
                [
                    {"report_name": "member-first", "payload": "first!" * 40},
                    {"report_name": "member-second", "payload": "second!" * 40},
                    {"report_name": "member-third", "soc": 62},
                ],
                missing_log,
            )
        finally:
            bundle.MAX_DIAGNOSTICS_MEMBER_BYTES = original_diagnostics_bytes
        member_payloads = read_members(member_bounded_archive)
        member_reports = json.loads(member_payloads["hoymiles_diagnostics.json"])
        member_environment = json.loads(member_payloads["environment.json"])
        require(
            len(member_payloads["hoymiles_diagnostics.json"]) <= 1800
            and member_reports[0]["report_name"] == "member-first"
            and member_reports[-1]["omission_reason"]
            == "diagnostics_member_size_limit"
            and member_environment["archive_complete"] is False,
            "Diagnostics member limit did not emit a terminal omission marker",
        )
        require(
            b"member-second" not in member_payloads["hoymiles_diagnostics.json"]
            and b"member-third" not in member_payloads[
                "hoymiles_diagnostics.json"
            ],
            "Diagnostics member limit retained a partial omitted suffix",
        )

        original_uncompressed_bytes = bundle.MAX_ARCHIVE_UNCOMPRESSED_BYTES
        bundle.MAX_ARCHIVE_UNCOMPRESSED_BYTES = 1
        try:
            globally_bounded_archive = build(
                [{"report_name": "global-private-report", "soc": 63}],
                missing_log,
            )
        finally:
            bundle.MAX_ARCHIVE_UNCOMPRESSED_BYTES = original_uncompressed_bytes
        global_members = read_members(globally_bounded_archive)
        global_environment = json.loads(global_members["environment.json"])
        global_reports = json.loads(global_members["hoymiles_diagnostics.json"])
        require(
            global_environment["archive_complete"] is False
            and global_environment["archive_omission_reason"]
            == "archive_uncompressed_size_limit"
            and global_reports[0]["omission_reason"]
            == "archive_uncompressed_size_limit"
            and b"global-private-report" not in global_members[
                "hoymiles_diagnostics.json"
            ],
            "Global archive limit did not replace evidence atomically",
        )

        # A compressed-byte budget must also hold for data which does not
        # benefit materially from ZIP compression.  Keep the production member
        # limits and lower only the final archive budget for this small test.
        entropy_rng = random.Random(20260904)
        entropy_alphabet = (
            "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
            " !#$%&()*+,-./:;<=>?@[]^_{|}~"
        )
        high_entropy_payload = [
            "".join(entropy_rng.choice(entropy_alphabet) for _ in range(1024))
            for _ in range(128)
        ]
        compressed_budget = 32 * 1024
        original_archive_bytes = bundle.MAX_ARCHIVE_BYTES
        bundle.MAX_ARCHIVE_BYTES = compressed_budget
        try:
            high_entropy_archive = build(
                [
                    {
                        "report_name": "compressed-high-entropy-private",
                        "samples": high_entropy_payload,
                    }
                ],
                missing_log,
            )
        finally:
            bundle.MAX_ARCHIVE_BYTES = original_archive_bytes
        high_entropy_members = read_members(high_entropy_archive)
        high_entropy_environment = json.loads(
            high_entropy_members["environment.json"]
        )
        high_entropy_reports = json.loads(
            high_entropy_members["hoymiles_diagnostics.json"]
        )
        require(
            len(high_entropy_archive) <= compressed_budget
            and high_entropy_environment["archive_complete"] is False
            and high_entropy_environment["archive_omission_reason"]
            == "archive_compressed_size_limit"
            and high_entropy_reports[0]["omission_reason"]
            == "archive_compressed_size_limit"
            and b"compressed-high-entropy-private" not in high_entropy_members[
                "hoymiles_diagnostics.json"
            ],
            "High-entropy evidence bypassed the final ZIP byte budget",
        )

        member_limits = {
            "README.txt": bundle.MAX_README_MEMBER_BYTES,
            "environment.json": bundle.MAX_ENVIRONMENT_MEMBER_BYTES,
            "hoymiles_diagnostics.json": bundle.MAX_DIAGNOSTICS_MEMBER_BYTES,
            "home_assistant_relevant_logs.txt": bundle.MAX_LOG_MEMBER_BYTES,
        }
        for candidate in (
            count_bounded_archive,
            report_bounded_archive,
            member_bounded_archive,
            globally_bounded_archive,
            high_entropy_archive,
        ):
            require(
                len(candidate) <= bundle.MAX_ARCHIVE_BYTES,
                "Final diagnostic ZIP exceeded its global compressed budget",
            )
            with ZipFile(BytesIO(candidate)) as archive:
                for info in archive.infolist():
                    require(
                        info.file_size <= member_limits[info.filename],
                        f"ZIP member exceeded its budget: {info.filename}",
                    )
                    member = archive.read(info.filename)
                    for forbidden in (
                        b"must-not-survive",
                        b"large-private-fragment",
                    ):
                        require(
                            forbidden not in member,
                            f"ZIP member leaked private data: {info.filename}",
                        )


def assert_current_ems_evidence_contract() -> None:
    """Exercise current Recorder projections, not hand-written legacy rows."""
    diagnostics, state_type, _ = load_diagnostics_module()
    execution = load_component_module("execution_history", "execution_history.py")
    at = datetime(2026, 10, 2, 18, 48, 50, tzinfo=timezone.utc)
    frames = [{"transaction_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
               "reason": "no_current_plan", "observed_at": at.isoformat(),
               "source_frame": {"soc": 53, "bms_ready": True,
                   "result_current": False, "recalculation_pending": True,
                   "lease": {"sequence": 42, "status": "accepted"}},
               "url": "http://private.example.local/secret"}]
    frames.append({**deepcopy(frames[0]), "transaction_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"})
    compressed_stops = execution.stop_recorder_projection(frames)
    require(compressed_stops["encoding"] == "zlib-base64-json", "Fixture did not exercise compressed STOP storage")
    attrs = {"recorded_execution": execution.supervisor_recorder_projection({
        "execution_phase": "executing", "transaction_id": frames[0]["transaction_id"],
        "tariff_decision": {"reason": "planner_pending"},
        "rce_export_evidence": {"status": "confirmed", "reason": "grid_discharge"},
    }), "recorded_stop_decisions": compressed_stops}
    event = diagnostics._serialize_history_item(state_type("executing", attrs, at),
        entity_id="sensor.hoymiles_hit_ems_supervisor")
    evidence = event["attributes"]
    require(evidence.get("stop_decisions_available") is True, "Compact STOP evidence was lost")
    require(evidence["recent_stop_decisions"][0]["source_frame"]["soc"] == 53, "Frozen STOP inputs lost")
    require(evidence.get("tariff_decision", {}).get("reason") == "planner_pending", "Tariff decision lost")
    require(evidence.get("rce_export_evidence", {}).get("status") == "confirmed", "Physical export evidence lost")
    require("private.example.local" not in json.dumps(event), "Decoded STOP was not redacted")
    tx = evidence["transaction_id"]
    require(tx not in {"[REDACTED_ID]", frames[0]["transaction_id"]}, "Transaction correlation was lost or exposed")
    require(tx == evidence["recent_stop_decisions"][0]["transaction_id"], "STOP correlation differs")
    require(tx != evidence["recent_stop_decisions"][1]["transaction_id"], "Historical transactions collapsed")
    redaction = load_component_module("diagnostic_redaction", "diagnostic_redaction.py")
    ids = redaction.sanitize_diagnostic_value({"transaction_ids": [frames[0]["transaction_id"]]})
    require(ids["transaction_ids"] == [tx], "Notification IDs cannot be correlated")
    require(redaction.sanitize_diagnostic_value({"transaction_id": tx})["transaction_id"] == tx,
            "Repeated archive sanitization changed the pseudonym")
    other = redaction.sanitize_diagnostic_value({
        "transaction_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "range_id": "rce.0123456789abcdef0123", "event_id": "rce.0123456789abcdef0123.start",
        "api_key": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    })
    require(other["transaction_id"] != tx, "Distinct transactions collapsed")
    require(other["range_id"] != other["event_id"], "Range/event correlation collapsed")
    require(other["api_key"] == "[REDACTED]", "Identifier support exposed a credential")
    bundle = load_component_module("diagnostic_bundle", "diagnostic_bundle.py")
    snapshot = diagnostics._state_snapshot(state_type("executing", attrs, at),
        key_hint="sensor.hoymiles_hit_ems_supervisor")
    report = {"report_schema": 1, "generated_at": at.isoformat(),
        "managed_state_snapshot": {"sensor.hoymiles_hit_ems_supervisor": snapshot},
        "control_history": {"available": True, "start": (at-timedelta(minutes=1)).isoformat(),
            "end": (at+timedelta(minutes=1)).isoformat(),
            "entities": {"sensor.hoymiles_hit_ems_supervisor": [event]}}}
    identity = {"anonymous_installation_id": "3f6f8b4e-7793-4f4b-9f45-486ddf65f78a",
                "installation_id_schema_version": 1}
    with tempfile.TemporaryDirectory(prefix="hoymiles-current-evidence-") as tmp:
        archive = bundle.build_support_archive([{**identity, **report}],
            log_path=Path(tmp) / "missing.log", generated_at=at.isoformat(),
            home_assistant_version="test", **identity)
        with ZipFile(BytesIO(archive)) as zipped:
            exported = json.loads(zipped.read("hoymiles_diagnostics.json"))[0]
        stop = exported["managed_state_snapshot"]["sensor.hoymiles_hit_ems_supervisor"]["attributes"]["recent_stop_decisions"][0]
        require(stop["source_frame"]["lease"]["sequence"] == 42, "ZIP depth limit destroyed STOP readback")
        from diagnostics_analysis.archive import load_diagnostic_archive
        from diagnostics_analysis.extractors import extract_archive_evidence, merge_events
        from diagnostics_analysis.analyzer import analyze_inputs
        from diagnostics_analysis.outputs import write_analysis_outputs
        path = Path(tmp) / "bundle.zip"
        path.write_bytes(archive)
        # The analyzer's loader verifies the actual ZIP contract before extraction.
        loaded = load_diagnostic_archive(path)
        _, _, events, _ = extract_archive_evidence(loaded)
        found = [item for item in events if item["entity_id"] == "sensor.hoymiles_hit_ems_supervisor"]
        require(any(item.get("attributes", {}).get("recent_stop_decisions") for item in found),
                "Offline analyzer discarded STOP evidence from the ZIP")
        plain = {key: value for key, value in found[0].items() if key != "attributes"}
        plain["archive_key"] = "000-earlier-state-only"
        for duplicates in ((found[0], plain), (plain, found[0])):
            merged = merge_events(duplicates)
            require(len(merged) == 1 and merged[0].get("attributes"), "State-only duplicate erased rich evidence")
        analyzed = analyze_inputs([path], generated_at=at)
        require(analyzed["control_events"][0]["attributes"]["recent_stop_decisions"][0]
                ["source_frame"]["lease"]["sequence"] == 42, "Final analysis lost STOP inputs")
        output = Path(tmp) / "analysis"
        write_analysis_outputs(analyzed, output)
        require("recent_stop_decisions" in (output / "control_events.csv").read_text(encoding="utf-8-sig"),
                "CSV discarded rich control evidence")
    for bad in ({}, {"recorded_stop_decisions": {"schema_version": 999}}):
        item = diagnostics._history_attributes("sensor.hoymiles_hit_ems_supervisor", bad)
        require(item.get("stop_decisions_available") is False, "Unknown STOP history became complete")
        require("recent_stop_decisions" not in item, "Unknown STOP history became an empty journal")
    missing = diagnostics._serialize_history_item({"state": "idle"},
        entity_id="sensor.hoymiles_hit_tariff_charge_plan")
    require(missing["attributes_available"] is False, "Missing attributes advertised as present")
    for entity in ("sensor.hoymiles_hit_rce_optimized_plan", "sensor.hoymiles_hit_tariff_charge_plan"):
        attrs = diagnostics._history_attributes(entity, {
            "price_provider": "Pstryk", "joint_plan_revision": 7, "joint_profile_revision": 2,
            "result_current": False, "recalculation_pending": True,
            "load_profile_generated_at": "2026-10-01T00:47:52+02:00",
            "load_model_quality": "fallback", "planned_slots": ["not a history payload"],
        })
        require(attrs.get("joint_plan_revision") == 7, "Joint replan evidence was discarded")
        require(attrs.get("load_profile_generated_at") is not None, "LOAD age provenance was discarded")
        require("planned_slots" not in attrs, "Large plan array added to rich history")


def assert_notification_snapshot_read_only() -> None:
    """Read the real manager/model ledger with transport and Store unavailable."""
    diagnostics, _, _ = load_diagnostics_module()
    const = types.ModuleType("homeassistant.const")
    const.ATTR_ENTITY_ID = "entity_id"
    events = types.ModuleType("homeassistant.helpers.event")
    storage = types.ModuleType("homeassistant.helpers.storage")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Diagnostic snapshot attempted I/O")
    events.async_call_later = forbidden
    storage.Store = FakeStore
    sys.modules.update({const.__name__: const, events.__name__: events, storage.__name__: storage})
    module = load_component_module("ems_notifications", "ems_notifications.py")
    FakeStore.reset()
    manager = module.HoymilesEmsNotificationManager(
        types.SimpleNamespace(services=types.SimpleNamespace(async_call=forbidden)),
        types.SimpleNamespace(entry_id="test"),
    )
    require(manager.diagnostic_snapshot()["available"] is False, "Unloaded ledger claimed available")
    at = datetime(2026, 10, 2, 16, 30, tzinfo=timezone.utc)
    manager._model = module.RangeNotificationModel(initialized=True)
    start_events = manager._model.observe(module.ExecutionObservation(
        observed_at=at, policy="rce", executing=True, actual_start=at,
        planned_end=at+timedelta(hours=2), transaction_id="rce:diagnostic-test",
    ))
    require(len(start_events) == 1, "Notification fixture did not start")
    manager._deliveries = [module.DeliveryRecord(event=start_events[0], state="delivered", updated_at=at)]
    manager._initialized = True
    manager._state_revision = 2
    manager._persisted_revision = 1
    snapshot = manager.diagnostic_snapshot()
    require(snapshot["available"] and not snapshot["phone_delivery_verified"], "Provider result became phone delivery")
    require(not snapshot["persistence_current"] and not snapshot["complete_lifetime_history"], "Ledger completeness overstated")
    require(snapshot["events"][0]["event"]["transaction_ids"] == ["rce:diagnostic-test"], "Notification transaction missing")
    snapshot["events"][0]["event"]["transaction_ids"].append("mutated-copy")
    snapshot["active_range"]["transaction_ids"].append("mutated-copy")
    require("mutated-copy" not in str(manager.diagnostic_snapshot()), "Export mutates the live ledger")
    require(FakeStore.load_count == FakeStore.save_count == 0, "Export accessed Store")
    require((manager._state_revision, manager._persisted_revision) == (2, 1), "Export advanced persistence revision")
    native_hass = types.SimpleNamespace(
        data={"hoymiles_hit_modbus": {"test": types.SimpleNamespace(
            entities={}, source_device=types.SimpleNamespace(), notifications=manager)}},
        states=types.SimpleNamespace(async_all=lambda: [], get=lambda _id: None),
    )
    native = asyncio.run(diagnostics.async_get_config_entry_diagnostics(
        native_hass, types.SimpleNamespace(entry_id="test", data={}, options={})))
    require(native["notification_history"]["retained_event_count"] == 1, "Native report did not collect the ledger")
    require(native["evidence_contract"]["accepted_lease_renewal_journal_available"] is False,
            "Native report overstates lease evidence")
    lease = load_component_module("supervisor_control_lease", "supervisor_control_lease.py")
    client = lease.ControlLeaseClient(session_id="secret-session")
    native_hass.data["hoymiles_hit_modbus"]["test"].control_lease = client
    for index in range(513):
        client.invalidate()
        request = client.prepare_arm(transaction_id="pvhold:diagnostic-test",
            hard_deadline=at+timedelta(hours=2), block=(5, 25, 90, 98, 5, 20, 0),
            challenge_nonce="1234abcd", now_wall=at, now_monotonic=float(index))
        client.accept_arm(request, dict(schema_version=1, protocol_version=2,
            accepted=True, reason="accepted", renew_nonce="1234abcd", soft_remaining_ms=120000))
    native = asyncio.run(diagnostics.async_get_config_entry_diagnostics(
        native_hass, types.SimpleNamespace(entry_id="test", data={}, options={})))
    journal = native["accepted_lease_journal"]
    require(native["evidence_contract"]["accepted_lease_renewal_journal_available"] is True,
            "Native report omitted available accepted ACKs")
    require(sum(len(page) for page in journal["pages"]) == 513,
            "Diagnostic redaction silently truncated journal")
    bundle = load_component_module("diagnostic_bundle", "diagnostic_bundle.py")
    with tempfile.TemporaryDirectory(prefix="lease_journal_zip_") as temp:
        archive = bundle.build_support_archive([native], log_path=Path(temp)/"absent.log",
            generated_at=at.isoformat(), home_assistant_version="2026.8.2",
            anonymous_installation_id="00000000-0000-4000-8000-000000000001",
            installation_id_schema_version=1)
        with ZipFile(BytesIO(archive)) as package:
            payload = package.read("hoymiles_diagnostics.json").decode()
        require('"ordinal": 513' in payload and '"ordinal": 1' in payload,
                "Support ZIP lost accepted journal pages")
        require("secret-session" not in payload and "1234abcd" not in payload,
                "Support ZIP exposed lease capabilities")
    require(FakeStore.load_count == FakeStore.save_count == 0, "Native ledger export accessed Store")
    manager._deliveries *= 40
    require(len(manager.diagnostic_snapshot()["events"]) == 32, "Notification export is unbounded")
    # Both providers retain policy=rce; names must not infer the price source.
    manager.hass.config = types.SimpleNamespace(language="pl")
    module.dt_util.as_local = lambda value: value
    event = types.SimpleNamespace(policy="rce", actual_start=at,
        occurred_at=at+timedelta(minutes=30), planned_end=at+timedelta(hours=2))
    for kind, outcome, expected in (
        ("start", None, "Rozpoczęto sprzedaż dynamiczną."),
        ("end", "completed", "Zakończono sprzedaż dynamiczną."),
        ("end", "interrupted", "Przerwano sprzedaż dynamiczną; ustawienia przywrócone."),
        ("end", "failed", "Nie zakończono poprawnie sprzedaży dynamicznej."),
    ):
        event.kind, event.outcome = kind, outcome
        title, message = manager._message(event)
        require(title == "Hoymiles — Sprzedaż dynamiczna", "Push title reveals internal policy name")
        require(message.startswith(expected), f"Incorrect Polish grammar: {message}")
        require("RCE" not in message and "Pstryk" not in message, "Provider leaked into common push copy")
    require(FakeStore.load_count == FakeStore.save_count == 0, "Copy test attempted delivery")


def main() -> None:
    assert_current_ems_evidence_contract()
    assert_notification_snapshot_read_only()
    redaction = load_component_module(
        "diagnostic_redaction",
        "diagnostic_redaction.py",
    )
    payload = {
        "voltage": 252.4,
        "planned_slots": ["18:00", "18:30"],
        "api_key": "this-must-never-be-exported",
        "encryption_key": "another-private-key",
        "wifi_ssid": "Private network",
        "note": (
            "host 192.0.2.10, AA:BB:CC:DD:EE:FF, "
            "owner@example.com, password=short-secret and "
            "https://private.example/path?token=abc"
        ),
        "nested": {"refresh_token": "secret-token", "soc": 72},
    }
    cleaned = redaction.sanitize_diagnostic_value(payload)
    serialized = repr(cleaned)
    for forbidden in (
        "this-must-never-be-exported",
        "another-private-key",
        "Private network",
        "192.0.2.10",
        "AA:BB:CC:DD:EE:FF",
        "owner@example.com",
        "short-secret",
        "private.example",
        "secret-token",
    ):
        require(forbidden not in serialized, f"Sensitive value leaked: {forbidden}")
    require(cleaned["voltage"] == 252.4, "Numeric telemetry was changed")
    require(cleaned["nested"]["soc"] == 72, "SOC telemetry was changed")
    require(cleaned["planned_slots"] == ["18:00", "18:30"], "Plan changed")

    adversarial_payload = {
        "client_secret": "multi word client secret",
        "clientSecret": "camel case client secret",
        "apiKey": "short-api-key",
        "APIKey": "acronym-api-key",
        "clientId": "private-client-id",
        "deviceId": "private-device-id",
        "message": (
            '{"token":"short-json-secret","state":"ok"}; '
            "Bearer short.bearer.token; Basic QWxhZGRpbjpvcGVuIHNlc2FtZQ==; "
            'password="two word password"; IPv6=2001:db8::42; '
            "serial number SN-PRIVATE-17; peer private-ha.local; "
            "broker mqtt://private-broker.local/topic; "
            "dotted MAC aabb.ccdd.eeff; "
            "UUID ffffffff-ffff-ffff-ffff-ffffffffffff; "
            "clock=10:20:30"
        ),
        "owner@example.com": "map-key-email",
        "2001:db8::7": "map-key-ipv6",
        "https://private.example/path": "map-key-url",
        "serial-private-123": "map-key-serial",
        "telemetry": {
            "soc": 64,
            "phase": "L1",
            "machine_type": "master",
            "machines_type": 2,
            "ghost_state": "idle",
        },
    }
    adversarial_cleaned = redaction.sanitize_diagnostic_value(
        adversarial_payload
    )
    adversarial_serialized = repr(adversarial_cleaned)
    for forbidden in (
        "multi word client secret",
        "camel case client secret",
        "short-api-key",
        "acronym-api-key",
        "private-client-id",
        "private-device-id",
        "short-json-secret",
        "short.bearer.token",
        "QWxhZGRpbjpvcGVuIHNlc2FtZQ==",
        "two word password",
        "2001:db8::42",
        "owner@example.com",
        "2001:db8::7",
        "private.example",
        "SN-PRIVATE-17",
        "private-ha.local",
        "private-broker.local",
        "aabb.ccdd.eeff",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "serial-private-123",
    ):
        require(
            forbidden not in adversarial_serialized,
            f"Adversarial sensitive value leaked: {forbidden}",
        )
    require(
        adversarial_cleaned["client_secret"] == redaction.REDACTED
        and adversarial_cleaned["clientSecret"] == redaction.REDACTED
        and adversarial_cleaned["apiKey"] == redaction.REDACTED
        and adversarial_cleaned["APIKey"] == redaction.REDACTED
        and adversarial_cleaned["clientId"] == redaction.REDACTED
        and adversarial_cleaned["deviceId"] == redaction.REDACTED,
        "Structured snake/camel-case secret keys bypassed redaction",
    )
    require(
        adversarial_cleaned["telemetry"]
        == {
            "soc": 64,
            "phase": "L1",
            "machine_type": "master",
            "machines_type": 2,
            "ghost_state": "idle",
        }
        and "clock=10:20:30" in adversarial_cleaned["message"],
        "Redaction changed safe telemetry or treated a clock as IPv6",
    )
    require(
        "[REDACTED_EMAIL]" in adversarial_cleaned
        and "[REDACTED_IP]" in adversarial_cleaned
        and "[REDACTED_URL]" in adversarial_cleaned,
        "Sensitive mapping keys were not sanitized",
    )

    safe_authorization_metrics = {
        "authorization_soc_start_percent": 66.0,
        "authorization_soc_end_percent": 36.4,
        "authorization_final_soc_percent": 36.4,
        "authorization_grid_export_kwh": 4.25,
        "authorization_reserve_violation_count": 0,
        "authorization_projection": "conservative_fail_closed",
    }
    require(
        redaction.sanitize_diagnostic_value(safe_authorization_metrics)
        == safe_authorization_metrics,
        "Typed canonical authorization metrics were unexpectedly redacted",
    )
    authorization_spoof = redaction.sanitize_diagnostic_value(
        {
            "authorization_soc_start_percent": "Bearer secret",
            "authorization_projection": "conservative_fail_open",
        }
    )
    require(
        authorization_spoof
        == {
            "authorization_soc_start_percent": redaction.REDACTED,
            "authorization_projection": redaction.REDACTED,
        },
        "Authorization schema exception accepted a secret or unknown label",
    )

    identity_module = load_installation_identity_module()
    identity, restarted_identity = asyncio.run(
        assert_installation_identity_contract(identity_module)
    )
    preserved = redaction.sanitize_diagnostic_value(
        identity,
        allow_anonymous_installation_id=True,
    )
    require(
        preserved["anonymous_installation_id"]
        == identity["anonymous_installation_id"],
        "Redaction removed the intentional anonymous installation ID",
    )
    rejected = redaction.sanitize_diagnostic_value(
        {"anonymous_installation_id": "serial-private"}
    )
    require(
        rejected["anonymous_installation_id"] == redaction.REDACTED,
        "Invalid data bypassed redaction through the anonymous ID field",
    )
    nested_spoof = redaction.sanitize_diagnostic_value(
        {
            "nested": {
                "anonymous_installation_id": (
                    "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                )
            }
        },
        allow_anonymous_installation_id=True,
    )
    require(
        nested_spoof["nested"]["anonymous_installation_id"]
        == redaction.REDACTED,
        "A nested UUID spoof bypassed anonymous-ID provenance",
    )

    bundle = load_component_module("diagnostic_bundle", "diagnostic_bundle.py")
    with tempfile.TemporaryDirectory(prefix="hoymiles_diagnostics_test_") as tmp:
        log_path = Path(tmp) / "home-assistant.log"
        log_path.write_text(
            "unrelated component message\n"
            "[E][hoymiles] Modbus timeout host=192.0.2.10 "
            "password=short-secret\n"
            '[E][hoymiles] {"token":"short-json-secret"} '
            "Authorization: Bearer dXNlcjpwYXNz peer=private-ha.local\n",
            encoding="utf-8",
        )
        archive_bytes = bundle.build_support_archive(
            [
                {
                    **identity,
                    "report_name": "entry-a",
                    "soc": 72,
                    "api_key": "this-must-never-be-exported",
                },
                {
                    **identity,
                    "report_name": "entry-b",
                    "soc": 68,
                    # The bundle's installation-wide identity is authoritative
                    # even if a future caller accidentally supplies a mismatch.
                    "anonymous_installation_id": (
                        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
                    ),
                    "installation_id_schema_version": 99,
                    "nested": {
                        "anonymous_installation_id": (
                            "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
                        )
                    },
                },
            ],
            log_path=log_path,
            generated_at="2026-08-09T12:00:00+00:00",
            home_assistant_version="2026.8.0",
            **identity,
        )
        second_archive_bytes = bundle.build_support_archive(
            [
                {
                    **restarted_identity,
                    "report_name": "entry-a",
                    "soc": 71,
                }
            ],
            log_path=log_path,
            generated_at="2026-08-10T12:00:00+00:00",
            home_assistant_version="2026.8.1",
            **restarted_identity,
        )
    with ZipFile(BytesIO(archive_bytes)) as archive:
        require(
            set(archive.namelist())
            == {
                "README.txt",
                "environment.json",
                "hoymiles_diagnostics.json",
                "home_assistant_relevant_logs.txt",
            },
            "Browser ZIP has an unexpected structure",
        )
        archive_members = {
            name: archive.read(name).decode("utf-8")
            for name in archive.namelist()
        }
        archive_text = "\n".join(archive_members.values())
        environment = json.loads(archive.read("environment.json"))
        reports = json.loads(archive.read("hoymiles_diagnostics.json"))
    with ZipFile(BytesIO(second_archive_bytes)) as second_archive:
        second_environment = json.loads(
            second_archive.read("environment.json")
        )
        second_reports = json.loads(
            second_archive.read("hoymiles_diagnostics.json")
        )
    for exported in [environment, second_environment, *reports, *second_reports]:
        require(
            exported["anonymous_installation_id"]
            == identity["anonymous_installation_id"],
            "Two diagnostics archives or config entries changed the ID",
        )
        require(
            exported["installation_id_schema_version"] == 1,
            "ZIP omitted the installation ID schema version",
        )
    require("Modbus timeout" in archive_text, "Relevant Core log was omitted")
    require("info@kaluzaaa.com" in archive_text, "Support email is missing from ZIP")
    require("unrelated component" not in archive_text, "Unrelated log leaked")
    for forbidden in (
        "192.0.2.10",
        "short-secret",
        "short-json-secret",
        "dXNlcjpwYXNz",
        "private-ha.local",
        "this-must-never-be-exported",
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    ):
        for member_name, member_text in archive_members.items():
            require(
                forbidden not in member_text,
                f"ZIP member leaked {forbidden}: {member_name}",
            )

    assert_support_archive_bounds_contract(bundle, identity)

    script = (COMPONENT / "collect_diagnostics.sh").read_text(encoding="utf-8")
    require("secrets.yaml" not in script, "Collector must not read secrets.yaml")
    require(
        "cp /config/.storage" not in script,
        "Collector must not copy the Home Assistant storage database",
    )
    require("[REDACTED_SECRET]" in script, "Shell secret masking is missing")
    require("native_diagnostics_" in script, "Native report collection is missing")
    support_source = (COMPONENT / "support_http.py").read_text(encoding="utf-8")
    for expected in (
        'user.is_admin',
        'content_type="application/zip"',
        '"Cache-Control": "no-store"',
        'build_support_archive',
        'async_get_or_create_installation_identity',
        'HTTPTooManyRequests',
        'REPORT_COLLECTION_TIMEOUT_SECONDS',
    ):
        require(expected in support_source, f"HTTP ZIP endpoint is missing: {expected}")

    diagnostics_source = (COMPONENT / "diagnostics.py").read_text(
        encoding="utf-8"
    )
    for expected in (
        "await async_get_or_create_installation_identity(",
        "**installation_identity.as_dict()",
        "CONTROL_HISTORY_EVENT_SCHEMA_VERSION = 1",
        '"sensor.hoymiles_ems_hardware_mode"',
        '"sensor.hoymiles_parallel_aggregate_physical_response"',
        "AGGREGATE_RESPONSE_HISTORY_ATTRIBUTE_KEYS",
        "HISTORY_ATTRIBUTE_KEYS_BY_PROFILE",
        "HISTORY_ATTRIBUTE_PROFILE_BY_ENTITY_ID",
        "regular_query = partial(",
        "profiled_query = partial(",
        '"sampled_transition_peak_kw"',
    ):
        require(
            expected in diagnostics_source,
            f"Native diagnostics report is missing identity wiring: {expected}",
        )
    diagnostics_tree = ast.parse(diagnostics_source)
    response_attribute_keys: set[str] | None = None
    for statement in diagnostics_tree.body:
        if (
            isinstance(statement, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "AGGREGATE_RESPONSE_HISTORY_ATTRIBUTE_KEYS"
                for target in statement.targets
            )
            and isinstance(statement.value, ast.Call)
            and statement.value.args
        ):
            response_attribute_keys = set(
                ast.literal_eval(statement.value.args[0])
            )
            break
    require(
        response_attribute_keys
        == {
            "authoritative_expected_power",
            "baseline_generation",
            "candidate_generations",
            "collection_baseline_generation",
            "completed_at",
            "configuration_acknowledgement_scope",
            "detected_inverters",
            "evidence_scope",
            "expected_power_kw",
            "final_generation",
            "formula",
            "grid_samples_kw",
            "individual_inverter_acknowledgement",
            "latched_machine_type",
            "observed_median_power_kw",
            "observed_spread_kw",
            "owner",
            "pending_at",
            "phase",
            "reason",
            "required_stable_generations",
            "requires_parallel_proof",
            "sample_count",
            "sampled_transition_observed",
            "sampled_transition_peak_kw",
            "sampled_transition_scope",
            "samples_kw",
            "stable_window_start",
            "tolerance_kw",
            "topology_known",
            "transaction_id",
            "transaction_started_epoch",
            "transition_grace_seconds",
            "verification_horizon_seconds",
        },
        "Recorder response-attribute allowlist diverged from the frozen contract",
    )

    assert_canonical_sensor_recorder_contract()
    diagnostics, fake_state_type, history_context = load_diagnostics_module()
    asyncio.run(
        assert_control_history_contract(
            diagnostics,
            fake_state_type,
            history_context,
        )
    )

    setup_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    identity_init = setup_source.index(
        "await async_get_or_create_installation_identity(hass)"
    )
    asset_init = setup_source.index("await _async_prepare_frontend_assets(hass)")
    require(
        identity_init < asset_init,
        "Installation identity is not initialized before optional assets",
    )

    card_type = "custom:hoymiles-diagnostics-download-card"
    for language in ("pl", "en"):
        dashboard = json.loads(
            (
                COMPONENT
                / "resources"
                / "www"
                / f"dashboard_hoymiles_{language}.json"
            ).read_text(encoding="utf-8")
        )
        diagnostic_view = next(
            view for view in dashboard["views"] if view.get("path") == "diagnostyka"
        )
        panel_cards = diagnostic_view.get("cards", [])
        require(
            diagnostic_view.get("type") == "panel"
            and len(panel_cards) == 1
            and panel_cards[0].get("type") == "vertical-stack",
            f"{language} diagnostics view does not preserve the panel/vertical-stack shell",
        )
        diagnostic_cards = panel_cards[0].get("cards", [])
        require(
            len(diagnostic_cards) >= 2
            and diagnostic_cards[0].get("type")
            == "custom:hoymiles-local-nav-card"
            and diagnostic_cards[1].get("type") == card_type,
            f"{language} dashboard does not place ZIP download directly after local navigation",
        )
    card_source = (ROOT / "home_assistant" / "www" / "hoymiles-rce-chart-card.js")
    bundled_card = COMPONENT / "resources" / "www" / "hoymiles-rce-chart-card.js"
    require(
        card_source.read_bytes() == bundled_card.read_bytes(),
        "Bundled diagnostics card differs from its source",
    )
    require(
        b"info@kaluzaaa.com" in card_source.read_bytes(),
        "Diagnostic card does not show the support email",
    )
    card_text = card_source.read_text(encoding="utf-8")
    for expected in (
        "const hoymilesDiagnosticsDownloadState =",
        "new AbortController()",
        "signal: controller.signal",
        "blob.slice(0, 4).arrayBuffer()",
        "URL.revokeObjectURL(downloadUrl)",
        "hoymilesMountDiagnosticsDownload",
        "hoymilesUpgradeCustomElement",
        "data-settings-diagnostics",
        ".settings-support",
        ".service-link",
    ):
        require(
            expected in card_text,
            f"Diagnostic browser/settings contract is missing: {expected}",
        )
    require(
        card_text.count("data-settings-diagnostics") >= 2,
        "ZIP control is not mounted in both settings shells",
    )
    require(
        "Pobierz stan EMS, dostępną historię sterowania z ostatnich 24 godzin "
        "i logi HA. Wyślij ZIP na info@kaluzaaa.com. Dopisz, co się stało, "
        "co powinno się wydarzyć oraz datę i godzinę ze strefą czasową. "
        "Przed udostępnieniem przejrzyj zawartość paczki."
        in card_text,
        "Settings ZIP control does not publish the approved Polish guidance",
    )
    print("Diagnostics privacy and browser ZIP tests passed")


if __name__ == "__main__":
    main()
