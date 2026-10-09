"""Deterministic offline contracts for I2 first-install helper defaults."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import tempfile
import types
from typing import Any

import yaml


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "custom_components"
    / "hoymiles_hit_modbus"
    / "ems_initial_defaults.py"
)
NOW = datetime(2026, 9, 2, 8, 0, tzinfo=timezone.utc)

spec = importlib.util.spec_from_file_location("ems_initial_defaults", MODULE_PATH)
assert spec is not None and spec.loader is not None
M = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = M
spec.loader.exec_module(M)


def _authorized(ledger: Any | None = None) -> Any:
    ledger = ledger or M.DefaultSeedLedger.empty()
    authorization = M.DefaultSeedAuthorization(
        M.DEFAULT_SEED_RULESET_VERSION,
        M.DEFAULT_SEED_RULESET_FINGERPRINT,
        "1.5.8",
        "a" * 64,
        "b" * 64,
        NOW.isoformat(),
    )
    return replace(ledger, authorization=authorization)


def _observations() -> dict[str, Any]:
    return {
        item.target_entity_id: M.HelperObservation(
            item.fallback_value,
            yaml_owned=True,
            pristine=True,
            shape_valid=True,
            context_id="fresh-context",
            last_updated=NOW,
        )
        for item in M.DEFAULT_SEED_SPECS
    }


def test_exact_inventory_and_canonical_initial_contract() -> None:
    assert len(M.DEFAULT_SEED_SPECS) == 33
    assert len({item.target_entity_id for item in M.DEFAULT_SEED_SPECS}) == 33
    defaults = {item.key: item.default_value for item in M.DEFAULT_SEED_SPECS}
    assert defaults["pv_charge_delay_enabled"] == "off"
    assert defaults["ev_load_filter_enabled"] == "off"
    assert defaults["ev_charge_power"] == 0  # Unconfigured until the user enters it.
    assert defaults["tariff_requested_charge_power"] == 50
    assert defaults["rce_requested_discharge_power"] == 50
    assert defaults["rcm_export_cap_percent"] == 50
    assert defaults["tariff_minimum_saving"] == 1
    assert defaults["rce_soc_safety_margin"] == 5
    assert defaults["tariff_soc_safety_margin"] == 5
    assert defaults["rcm_soc_safety_margin"] == 5
    assert defaults["fallback_daily_home_load"] == 20

    scheduler = (ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml").read_text(
        encoding="utf-8"
    )
    parsed = yaml.safe_load(scheduler)
    assert scheduler.count("    initial:") == 3
    for item in M.DEFAULT_SEED_SPECS:
        domain, object_id = item.target_entity_id.split(".", 1)
        definition = parsed[domain][object_id]
        assert "initial" not in definition
        if item.kind == "number":
            assert float(definition["min"]) == item.minimum
            assert float(definition["max"]) == item.maximum
            assert float(definition["step"]) == item.step
        elif item.kind == "time":
            assert definition["has_date"] is False
            assert definition["has_time"] is True
    for object_id, value in (
        ("hoymiles_rce_latched_minimum_soc", "0"),
        ("hoymiles_rcm_latched_pre_discharge_target_soc", "100"),
        ("hoymiles_rcm_latched_pre_discharge_power", "0"),
    ):
        start = scheduler.index(f"  {object_id}:")
        block = scheduler[start : start + 500]
        assert f"initial: {value}" in block

    # User choices must survive every Home Assistant restart.  An ``initial``
    # value on any of these helpers would override RestoreEntity and silently
    # reset the compact EMS control strip after startup.
    persistent_boolean_controls = (
        "hoymiles_ems_supervisor_allow_rce",
        "hoymiles_ems_supervisor_allow_tariff",
        "hoymiles_ems_supervisor_allow_rcm",
        "hoymiles_rce_discharge_enabled",
        "hoymiles_rce_dynamic_soc_enabled",
        "hoymiles_tariff_charge_enabled",
        "hoymiles_rcm_enabled",
        "hoymiles_rcm_export_control_enabled",
        "hoymiles_rcm_pre_discharge_enabled",
        "hoymiles_battery_balancing_enabled",
    )
    for object_id in persistent_boolean_controls:
        assert "initial" not in parsed["input_boolean"][object_id]
    assert "initial" not in parsed["input_select"][
        "hoymiles_ems_supervisor_mode"
    ]


def test_fresh_plan_seeds_defaults_and_is_idempotent() -> None:
    observations = _observations()
    plan = M.plan_default_seeds(
        _authorized(), observations, restored_entity_ids=frozenset(), now=NOW
    )
    assert len(plan.writes) == 27
    writes = {item.target_entity_id: item.value for item in plan.writes}
    assert "input_number.hoymiles_ev_charge_power" not in writes
    assert writes["input_number.hoymiles_tariff_requested_charge_power"] == 50
    assert writes["input_number.hoymiles_rce_requested_discharge_power"] == 50
    assert writes["input_number.hoymiles_rcm_export_cap_percent"] == 50
    assert writes["input_number.hoymiles_tariff_minimum_saving"] == 1
    assert writes["input_number.hoymiles_rce_soc_safety_margin"] == 5
    assert writes["input_number.hoymiles_tariff_soc_safety_margin"] == 5
    assert writes["input_number.hoymiles_rcm_soc_safety_margin"] == 5
    assert writes["input_number.hoymiles_rce_fallback_daily_load"] == 20

    ledger = plan.ledger
    for write in plan.writes:
        ledger = M.complete_default_seed_write(
            ledger,
            target_entity_id=write.target_entity_id,
            observed_value=write.value,
            now=NOW,
        )
        observations[write.target_entity_id] = replace(
            observations[write.target_entity_id], value=write.value
        )
    assert ledger.result == "complete"
    restart = M.plan_default_seeds(
        ledger, observations, restored_entity_ids=frozenset(), now=NOW
    )
    assert restart.writes == ()
    assert restart.ledger == ledger


def test_existing_restored_and_user_changed_values_are_preserved() -> None:
    observations = _observations()
    target = "input_number.hoymiles_rce_requested_discharge_power"

    no_token = M.DefaultSeedLedger.empty()
    ineligible = M.plan_default_seeds(
        no_token, observations, restored_entity_ids=frozenset(), now=NOW
    )
    assert ineligible.writes == ()
    assert ineligible.ledger == no_token

    restored = M.plan_default_seeds(
        _authorized(), observations, restored_entity_ids={target}, now=NOW
    )
    assert restored.ledger.field(target).status == "preserved_restored"
    assert target not in {item.target_entity_id for item in restored.writes}

    observations[target] = replace(
        observations[target], value=0, pristine=False, context_id="user-context"
    )
    changed = M.plan_default_seeds(
        _authorized(), observations, restored_entity_ids=frozenset(), now=NOW
    )
    assert changed.ledger.field(target).status == "preserved_changed"
    assert target not in {item.target_entity_id for item in changed.writes}


def test_write_ahead_retry_requires_identical_cas_and_no_restore() -> None:
    observations = _observations()
    target = "input_number.hoymiles_rce_requested_discharge_power"
    first = M.plan_default_seeds(
        _authorized(), observations, restored_entity_ids=frozenset(), now=NOW
    )
    failed = M.fail_default_seed_write(
        first.ledger, target_entity_id=target, error="simulated", now=NOW
    )
    retry = M.plan_default_seeds(
        failed, observations, restored_entity_ids=frozenset(), now=NOW
    )
    assert target in {item.target_entity_id for item in retry.writes}

    raced = dict(observations)
    raced[target] = replace(raced[target], context_id="different-context")
    stopped = M.plan_default_seeds(
        failed, raced, restored_entity_ids=frozenset(), now=NOW
    )
    assert stopped.ledger.field(target).status == "preserved_changed"
    assert target not in {item.target_entity_id for item in stopped.writes}

    restored = M.plan_default_seeds(
        failed, observations, restored_entity_ids={target}, now=NOW
    )
    assert restored.ledger.field(target).status == "preserved_restored"
    assert target not in {item.target_entity_id for item in restored.writes}


def test_upgrade_downgrade_and_shared_fallback_gate() -> None:
    v1 = {
        "version": 1,
        "fields": [
            {
                "key": item.key,
                "target_entity_id": item.target_entity_id,
                "status": "preserved_restored",
            }
            for item in M.DEFAULT_SEED_SPECS
            if item.introduced_version == 1
        ],
    }
    upgraded = M.default_seed_ledger_from_dict(v1)
    assert upgraded.authorization is None
    assert M.plan_default_seeds(
        upgraded,
        _observations(),
        restored_entity_ids=frozenset(),
        now=NOW,
    ).writes == ()
    assert M.blocked_shared_migration_targets(upgraded) == frozenset()

    fresh = _authorized()
    assert M.blocked_shared_migration_targets(fresh) == {
        "input_number.hoymiles_ems_fallback_daily_home_load"
    }
    fallback = "input_number.hoymiles_rce_fallback_daily_load"
    terminal = fresh.replace_field(
        replace(fresh.field(fallback), status="seeded", expected_value=20)
    )
    assert M.blocked_shared_migration_targets(terminal) == frozenset()
    trusted = M.trusted_shared_migration_source_values(terminal)
    assert trusted == {fallback: 20}

    corrupt = M._storage_unreadable_default_seed_ledger(NOW)
    expected_fallback_block = {
        "input_number.hoymiles_ems_fallback_daily_home_load"
    }
    assert M.blocked_shared_migration_targets(corrupt) == expected_fallback_block
    assert M.blocked_shared_migration_targets(None) == expected_fallback_block
    malformed_fallback = replace(
        upgraded,
        fields=tuple(
            replace(
                field,
                status="preserved_invalid",
                error="ledger_field_missing",
            )
            if field.target_entity_id == fallback
            else field
            for field in upgraded.fields
        ),
    )
    assert (
        M.blocked_shared_migration_targets(malformed_fallback)
        == expected_fallback_block
    )

    try:
        M.default_seed_ledger_from_dict({"version": 999, "fields": []})
    except M.UnsupportedDefaultSeedVersion:
        pass
    else:
        raise AssertionError("future ledger version must fail closed")


class _MemoryStore:
    def __init__(self, value: dict[str, Any] | None = None) -> None:
        self.value = value
        self.saves: list[dict[str, Any]] = []

    async def async_load(self) -> dict[str, Any] | None:
        return self.value

    async def async_save(self, value: dict[str, Any]) -> None:
        self.value = value
        self.saves.append(value)


class _FakeContext:
    next_id = 0

    def __init__(self) -> None:
        type(self).next_id += 1
        self.id = f"operation-{type(self).next_id}"
        self.user_id = None
        self.parent_id = None


class _State:
    def __init__(
        self,
        value: Any,
        attributes: dict[str, Any],
        *,
        context: Any,
        changed_at: datetime = NOW,
    ) -> None:
        self.state = str(value)
        self.attributes = attributes
        self.context = context
        self.last_updated = changed_at
        self.last_reported = changed_at


class _StateMachine:
    def __init__(self) -> None:
        self.values: dict[str, _State] = {}

    def get(self, entity_id: str) -> _State | None:
        return self.values.get(entity_id)


class _Services:
    def __init__(self, states: _StateMachine) -> None:
        self.states = states
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        *,
        blocking: bool,
        context: Any,
    ) -> None:
        assert blocking
        self.calls.append((domain, service, dict(data)))
        old = self.states.values[data["entity_id"]]
        if domain == "input_boolean":
            value = "on" if service == "turn_on" else "off"
        else:
            value = data.get("value", data.get("time"))
        self.states.values[data["entity_id"]] = _State(
            value,
            dict(old.attributes),
            context=context,
            changed_at=NOW,
        )


class _Hass:
    def __init__(self, config_path: Path) -> None:
        self.config = types.SimpleNamespace(
            config_dir=str(config_path), language="pl-PL", safe_mode=False
        )
        self.is_running = False
        self.safe_mode = False
        self.data: dict[str, Any] = {}
        self.states = _StateMachine()
        self.services = _Services(self.states)

    async def async_add_executor_job(self, function: Any, *args: Any) -> Any:
        return function(*args)


def _fresh_runtime_states(hass: _Hass) -> None:
    for item in M.DEFAULT_SEED_SPECS:
        attributes: dict[str, Any] = {"editable": False}
        if item.kind == "number":
            attributes.update(
                {
                    "initial": None,
                    "min": item.minimum,
                    "max": item.maximum,
                    "step": item.step,
                }
            )
        elif item.kind == "time":
            attributes.update({"has_date": False, "has_time": True})
        hass.states.values[item.target_entity_id] = _State(
            item.fallback_value,
            attributes,
            context=types.SimpleNamespace(
                id="fresh-context", user_id=None, parent_id=None
            ),
        )


def test_exact_fresh_install_authorization_and_runtime_restart() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="ems_seed_token_") as tmp:
            config = Path(tmp)
            source = config / "source"
            source.mkdir()
            packages = config / "packages"
            packages.mkdir()
            scheduler_source = source / "scheduler.yaml"
            shared_source = source / "shared.yaml"
            scheduler_destination = packages / "hoymiles_ems_scheduler.yaml"
            shared_destination = packages / "hoymiles_ems_shared_inputs.yaml"
            scheduler_source.write_bytes(b"scheduler-v2")
            shared_source.write_bytes(b"shared-v2")
            scheduler_destination.write_bytes(scheduler_source.read_bytes())
            shared_destination.write_bytes(shared_source.read_bytes())
            hass = _Hass(config)
            evidence = M.FreshInstallEvidence(
                True,
                "absent",
                scheduler_destination,
                shared_destination,
            )
            token_store = _MemoryStore()
            old_store = M.EMSInitialDefaultSeedStore
            old_absence = M._async_helper_absence

            async def helpers_absent(_hass: Any, _path: Path) -> tuple[bool, str]:
                return True, "absent"

            M.EMSInitialDefaultSeedStore = lambda _hass: token_store
            M._async_helper_absence = helpers_absent
            try:
                armed = await M.async_authorize_fresh_install(
                    hass,
                    evidence,
                    written_paths={scheduler_destination, shared_destination},
                    scheduler_source=scheduler_source,
                    shared_package_source=shared_source,
                    package_version="1.5.8",
                    now=NOW,
                )
            finally:
                M.EMSInitialDefaultSeedStore = old_store
                M._async_helper_absence = old_absence
            assert armed
            assert token_store.value is not None
            authorized = M.default_seed_ledger_from_dict(token_store.value)
            assert authorized.authorization is not None
            assert authorized.authorization.valid

            _fresh_runtime_states(hass)
            runtime_store = _MemoryStore(token_store.value)
            coordinator = M.EMSInitialDefaultSeeder(hass, runtime_store)

            async def authorization_current(_authorization: Any) -> bool:
                return True

            coordinator._async_authorization_current = authorization_current
            coordinator._restored_entity_ids = lambda: frozenset()
            core = types.ModuleType("homeassistant.core")
            core.Context = _FakeContext
            homeassistant = types.ModuleType("homeassistant")
            sys.modules["homeassistant"] = homeassistant
            sys.modules["homeassistant.core"] = core

            completed = await coordinator.async_run_once(now=NOW)
            assert completed.result == "complete"
            assert len(hass.services.calls) == 27
            calls_after_first_run = list(hass.services.calls)
            saves_after_first_run = len(runtime_store.saves)
            user_target = "input_number.hoymiles_rce_requested_discharge_power"
            previous = hass.states.values[user_target]
            hass.states.values[user_target] = _State(
                33,
                dict(previous.attributes),
                context=types.SimpleNamespace(
                    id="user-change", user_id="user-1", parent_id=None
                ),
            )
            restarted = await coordinator.async_run_once(now=NOW)
            assert restarted.result == "complete"
            assert hass.services.calls == calls_after_first_run
            assert len(runtime_store.saves) == saves_after_first_run
            assert hass.states.values[user_target].state == "33"

    asyncio.run(run())


def test_fresh_install_crash_window_recovers_from_pending_attestation() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="ems_seed_pending_") as tmp:
            config = Path(tmp)
            source = config / "source"
            source.mkdir()
            packages = config / "packages"
            packages.mkdir()
            scheduler_source = source / "scheduler.yaml"
            shared_source = source / "shared.yaml"
            scheduler_destination = packages / "hoymiles_ems_scheduler.yaml"
            shared_destination = packages / "hoymiles_ems_shared_inputs.yaml"
            scheduler_source.write_bytes(b"scheduler-v2")
            shared_source.write_bytes(b"shared-v2")
            hass = _Hass(config)
            evidence = M.FreshInstallEvidence(
                True,
                "absent",
                scheduler_destination,
                shared_destination,
            )
            token_store = _MemoryStore()
            old_store = M.EMSInitialDefaultSeedStore
            M.EMSInitialDefaultSeedStore = lambda _hass: token_store
            try:
                staged = await M.async_stage_fresh_install_authorization(
                    hass,
                    evidence,
                    scheduler_source=scheduler_source,
                    shared_package_source=shared_source,
                    package_version="1.5.8",
                    now=NOW,
                )
                assert staged
                pending = M.default_seed_ledger_from_dict(token_store.value)
                assert pending.authorization is None
                assert pending.pending_authorization is not None
                assert pending.result == "not_authorized"

                # Simulate a process crash after both exact package copies but
                # before the post-copy promotion call.
                scheduler_destination.write_bytes(scheduler_source.read_bytes())
                shared_destination.write_bytes(shared_source.read_bytes())
                recovery_evidence = M.FreshInstallEvidence(
                    False,
                    "managed_package_exists",
                    scheduler_destination,
                    shared_destination,
                )
                promoted = await M.async_authorize_fresh_install(
                    hass,
                    recovery_evidence,
                    written_paths=frozenset(),
                    scheduler_source=scheduler_source,
                    shared_package_source=shared_source,
                    package_version="1.5.8",
                    now=NOW,
                )
            finally:
                M.EMSInitialDefaultSeedStore = old_store
            assert promoted
            authorized = M.default_seed_ledger_from_dict(token_store.value)
            assert authorized.authorization is not None
            assert authorized.authorization.valid
            assert authorized.pending_authorization is None

    asyncio.run(run())


def test_helper_store_collision_and_corruption_fail_closed() -> None:
    with tempfile.TemporaryDirectory(prefix="ems_seed_store_") as tmp:
        config = Path(tmp)
        assert M._helper_storage_absence(config) == (True, "absent")
        storage = config / ".storage"
        storage.mkdir()
        (storage / "input_number").write_text(
            '{"version":1,"minor_version":1,"key":"input_number",'
            '"data":{"items":[{"id":"hoymiles_rce_soc_safety_margin"}]}}',
            encoding="utf-8",
        )
        absent, reason = M._helper_storage_absence(config)
        assert not absent and reason == "input_number_store_collision"
        (storage / "input_number").write_text("not-json", encoding="utf-8")
        absent, reason = M._helper_storage_absence(config)
        assert not absent and reason == "input_number_store_unverifiable"
        (storage / "input_number").unlink()
        (storage / "core.restore_state").write_text("not-json", encoding="utf-8")
        absent, reason = M._disk_persistence_absence(config)
        assert not absent and reason == "restore_store_unverifiable"


def test_lifecycle_order_is_authorize_then_seed_then_shared_copy() -> None:
    source = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "__init__.py"
    ).read_text(encoding="utf-8")
    prepare = source.index("async def _async_prepare_frontend_assets")
    capture = source.index("async_capture_fresh_install_evidence(hass)", prepare)
    stage = source.index("await async_stage_fresh_install_authorization(", capture)
    install = source.index("paths = await async_install_assets(", stage)
    authorize = source.index("await async_authorize_fresh_install(", install)
    assert capture < stage < install < authorize

    setup_entry = source.index("async def async_setup_entry")
    seed = source.index(".async_run_once(now=datetime.now(timezone.utc))", setup_entry)
    migrate = source.index("await migration.async_run_copy_once(", seed)
    blocked = source.index("blocked_target_entity_ids=blocked_migration_targets", migrate)
    assert seed < migrate < blocked
    fail_closed = source.index(
        "blocked_migration_targets = blocked_shared_migration_targets(None)",
        setup_entry,
    )
    assert fail_closed < seed < migrate


def test_preinstall_evidence_requires_empty_live_registry_and_restore() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="ems_seed_evidence_") as tmp:
            hass = _Hass(Path(tmp))
            registry = types.SimpleNamespace(async_get=lambda _entity_id: None)
            restore = types.SimpleNamespace(last_states={})
            entity_registry = types.ModuleType(
                "homeassistant.helpers.entity_registry"
            )
            entity_registry.async_get = lambda _hass: registry
            restore_state = types.ModuleType("homeassistant.helpers.restore_state")
            restore_state.async_get = lambda _hass: restore
            helpers = types.ModuleType("homeassistant.helpers")
            helpers.entity_registry = entity_registry
            homeassistant = sys.modules.setdefault(
                "homeassistant", types.ModuleType("homeassistant")
            )
            homeassistant.helpers = helpers
            sys.modules["homeassistant.helpers"] = helpers
            sys.modules["homeassistant.helpers.entity_registry"] = entity_registry
            sys.modules["homeassistant.helpers.restore_state"] = restore_state

            evidence = await M.async_capture_fresh_install_evidence(hass)
            assert evidence.eligible and evidence.reason == "absent"

            target = M.DEFAULT_SEED_SPECS[0].target_entity_id
            restore.last_states[target] = object()
            blocked = await M.async_capture_fresh_install_evidence(hass)
            assert not blocked.eligible
            assert blocked.reason == "restore_state_exists"

    asyncio.run(run())


def test_corrupt_seed_store_creates_durable_no_authorization_tombstone() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="ems_seed_corrupt_") as tmp:
            config = Path(tmp)
            source = config / "source"
            source.mkdir()
            packages = config / "packages"
            packages.mkdir()
            scheduler_source = source / "scheduler.yaml"
            shared_source = source / "shared.yaml"
            scheduler_source.write_bytes(b"scheduler-v2")
            shared_source.write_bytes(b"shared-v2")
            evidence = M.FreshInstallEvidence(
                True,
                "absent",
                packages / "hoymiles_ems_scheduler.yaml",
                packages / "hoymiles_ems_shared_inputs.yaml",
            )
            hass = _Hass(config)

            class QuarantiningStore(_MemoryStore):
                first_load = True

                async def async_load_with_presence(self) -> tuple[bool, Any]:
                    if self.first_load:
                        self.first_load = False
                        return True, None
                    return True, self.value

            store = QuarantiningStore()
            old_store = M.EMSInitialDefaultSeedStore
            M.EMSInitialDefaultSeedStore = lambda _hass: store
            try:
                first = await M.async_stage_fresh_install_authorization(
                    hass,
                    evidence,
                    scheduler_source=scheduler_source,
                    shared_package_source=shared_source,
                    package_version="1.5.8",
                    now=NOW,
                )
                second = await M.async_stage_fresh_install_authorization(
                    hass,
                    evidence,
                    scheduler_source=scheduler_source,
                    shared_package_source=shared_source,
                    package_version="1.5.8",
                    now=NOW,
                )
            finally:
                M.EMSInitialDefaultSeedStore = old_store
            assert not first and not second
            ledger = M.default_seed_ledger_from_dict(store.value)
            assert ledger.authorization is None
            assert ledger.pending_authorization is None
            assert all(
                field.status == "preserved_invalid"
                and field.error == "storage_unreadable"
                for field in ledger.fields
            )

    asyncio.run(run())


def test_stale_pending_is_replaced_only_with_fresh_absence_evidence() -> None:
    async def run() -> None:
        with tempfile.TemporaryDirectory(prefix="ems_seed_restaged_") as tmp:
            config = Path(tmp)
            source = config / "source"
            source.mkdir()
            packages = config / "packages"
            packages.mkdir()
            scheduler_source = source / "scheduler.yaml"
            shared_source = source / "shared.yaml"
            scheduler_source.write_bytes(b"scheduler-current")
            shared_source.write_bytes(b"shared-current")
            scheduler_destination = packages / "hoymiles_ems_scheduler.yaml"
            shared_destination = packages / "hoymiles_ems_shared_inputs.yaml"
            hass = _Hass(config)
            evidence = M.FreshInstallEvidence(
                True,
                "absent",
                scheduler_destination,
                shared_destination,
            )
            stale = M.DefaultSeedAuthorization(
                M.DEFAULT_SEED_RULESET_VERSION,
                M.DEFAULT_SEED_RULESET_FINGERPRINT,
                "1.5.7",
                "1" * 64,
                "2" * 64,
                NOW.isoformat(),
            )
            store = _MemoryStore(
                replace(
                    M.DefaultSeedLedger.empty(),
                    pending_authorization=stale,
                ).as_dict()
            )
            old_store = M.EMSInitialDefaultSeedStore
            M.EMSInitialDefaultSeedStore = lambda _hass: store
            try:
                restaged = await M.async_stage_fresh_install_authorization(
                    hass,
                    evidence,
                    scheduler_source=scheduler_source,
                    shared_package_source=shared_source,
                    package_version="1.5.8",
                    now=NOW,
                )
                assert restaged
                current = M.default_seed_ledger_from_dict(store.value)
                assert current.authorization is None
                assert current.pending_authorization is not None
                assert current.pending_authorization.package_version == "1.5.8"
                assert current.pending_authorization.scheduler_sha256 != "1" * 64

                unchanged = store.value
                blocked = await M.async_stage_fresh_install_authorization(
                    hass,
                    replace(evidence, eligible=False, reason="managed_package_exists"),
                    scheduler_source=scheduler_source,
                    shared_package_source=shared_source,
                    package_version="1.5.9",
                    now=NOW,
                )
                assert not blocked
                assert store.value == unchanged
            finally:
                M.EMSInitialDefaultSeedStore = old_store

    asyncio.run(run())


def main() -> None:
    test_exact_inventory_and_canonical_initial_contract()
    test_fresh_plan_seeds_defaults_and_is_idempotent()
    test_existing_restored_and_user_changed_values_are_preserved()
    test_write_ahead_retry_requires_identical_cas_and_no_restore()
    test_upgrade_downgrade_and_shared_fallback_gate()
    test_exact_fresh_install_authorization_and_runtime_restart()
    test_fresh_install_crash_window_recovers_from_pending_attestation()
    test_helper_store_collision_and_corruption_fail_closed()
    test_lifecycle_order_is_authorize_then_seed_then_shared_copy()
    test_preinstall_evidence_requires_empty_live_registry_and_restore()
    test_corrupt_seed_store_creates_durable_no_authorization_tombstone()
    test_stale_pending_is_replaced_only_with_fresh_absence_evidence()
    print("EMS initial defaults: 12 deterministic groups passed")


if __name__ == "__main__":
    main()
