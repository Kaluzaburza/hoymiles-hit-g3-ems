"""Deterministic offline tests for the shared EMS copy-once ledger."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = (
    ROOT
    / "custom_components"
    / "hoymiles_hit_modbus"
    / "ems_shared_input_migration.py"
)
NOW = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc)

spec = importlib.util.spec_from_file_location("ems_shared_input_migration", MODULE_PATH)
assert spec is not None and spec.loader is not None
M = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = M
spec.loader.exec_module(M)


def _unmigrated_states() -> dict[str, Any]:
    values = {
        "forecast_today": ("unknown", "sensor.legacy_today"),
        "forecast_tomorrow": ("unknown", "sensor.legacy_tomorrow"),
        "forecast_day3": ("unknown", "sensor.legacy_day3"),
        "fallback_daily_home_load": ("0", "12.5"),
        "inverter_rated_power_each": ("Automatycznie", "10 kW"),
        "pv_to_battery_efficiency": ("0", "95"),
        "battery_to_home_efficiency": ("0", "94"),
    }
    states: dict[str, Any] = {}
    for field in M.MIGRATION_FIELD_SPECS:
        target, legacy = values[field.key]
        states[field.target_entity_id] = target
        states[field.legacy_entity_id] = legacy
    return states


def test_copy_once_idempotence_and_restart_recovery() -> None:
    states = _unmigrated_states()
    plan = M.plan_copy_once(M.MigrationLedger.empty(), states, now=NOW)
    assert len(plan.writes) == len(M.MIGRATION_FIELD_SPECS) == 7
    assert all(field.status == "copy_pending" for field in plan.ledger.fields)
    assert all(
        write.target_entity_id.startswith(("input_text.", "input_number.", "input_select."))
        for write in plan.writes
    )
    assert not any(
        write.target_entity_id == field.legacy_entity_id
        for write in plan.writes
        for field in M.MIGRATION_FIELD_SPECS
    )

    # A restart after the service call but before ledger completion verifies
    # the target and performs no second write.
    first_write = plan.writes[0]
    states[first_write.target_entity_id] = first_write.value
    resumed = M.plan_copy_once(plan.ledger, states, now=NOW)
    assert resumed.ledger.field(first_write.target_entity_id).status == "copied_legacy"
    assert first_write.target_entity_id not in {
        write.target_entity_id for write in resumed.writes
    }

    ledger = plan.ledger
    for write in plan.writes:
        ledger = M.complete_migration_write(
            ledger,
            target_entity_id=write.target_entity_id,
            observed_value=write.value,
            now=NOW,
        )
        states[write.target_entity_id] = write.value
    assert ledger.result == "complete"
    assert all(field.status == "copied_legacy" for field in ledger.fields)

    # Later legacy edits cannot overwrite the new source of truth.
    for field in M.MIGRATION_FIELD_SPECS:
        states[field.legacy_entity_id] = "sensor.changed" if field.kind == "entity_id" else "20"
    second = M.plan_copy_once(ledger, states, now=NOW)
    assert second.writes == ()
    assert second.ledger == ledger

    decoded = M.migration_ledger_from_dict(ledger.as_dict())
    assert decoded == ledger
    public = decoded.as_public_dict()
    assert public["result"] == "complete"
    assert public["provenance"]["pv_to_battery_efficiency"] == (
        "migration_seed_from_legacy"
    )
    assert public["seeded_values"] == {
        "forecast_today": "sensor.legacy_today",
        "forecast_tomorrow": "sensor.legacy_tomorrow",
        "forecast_day3": "sensor.legacy_day3",
        "fallback_daily_home_load": 12.5,
        "inverter_rated_power_each": "10 kW",
        "pv_to_battery_efficiency": 95.0,
        "battery_to_home_efficiency": 94.0,
    }


def test_preserve_new_missing_and_invalid() -> None:
    states = _unmigrated_states()
    today = M.MIGRATION_FIELD_SPECS[0]
    states[today.target_entity_id] = "sensor.user_selected"
    tomorrow = M.MIGRATION_FIELD_SPECS[1]
    states.pop(tomorrow.target_entity_id)
    day3 = M.MIGRATION_FIELD_SPECS[2]
    states[day3.legacy_entity_id] = ""
    fallback = M.MIGRATION_FIELD_SPECS[3]
    states[fallback.legacy_entity_id] = "nan"
    rated = M.MIGRATION_FIELD_SPECS[4]
    states[rated.target_entity_id] = "25 kW"
    plan = M.plan_copy_once(M.MigrationLedger.empty(), states, now=NOW)
    assert plan.ledger.field(today.target_entity_id).status == "preserved_new"
    assert plan.ledger.field(tomorrow.target_entity_id).status == "target_missing"
    assert plan.ledger.field(day3.target_entity_id).status == "no_legacy_value"
    assert plan.ledger.field(fallback.target_entity_id).status == "invalid_legacy"
    assert plan.ledger.field(rated.target_entity_id).status == "preserved_invalid_new"
    assert not any(write.target_entity_id == today.target_entity_id for write in plan.writes)

    # A failed write remains retryable and retains the pre-write expected value.
    retry_plan = M.plan_copy_once(M.MigrationLedger.empty(), _unmigrated_states(), now=NOW)
    write = retry_plan.writes[0]
    failed = M.fail_migration_write(
        retry_plan.ledger,
        target_entity_id=write.target_entity_id,
        error="simulated",
        now=NOW,
    )
    retry = M.plan_copy_once(failed, _unmigrated_states(), now=NOW)
    assert any(item.target_entity_id == write.target_entity_id for item in retry.writes)
    try:
        M.plan_copy_once(
            M.MigrationLedger.empty(),
            _unmigrated_states(),
            now=NOW.replace(tzinfo=None),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("naive migration clock must be rejected")


def test_entity_id_unknown_is_unset_but_unavailable_fails_closed() -> None:
    field = M.MIGRATION_FIELD_SPECS[0]
    legacy_value = "sensor.legacy_today"

    unknown = M.plan_copy_once(
        M.MigrationLedger.empty(),
        {
            field.target_entity_id: "unknown",
            field.legacy_entity_id: legacy_value,
        },
        now=NOW,
    )
    record = unknown.ledger.field(field.target_entity_id)
    assert record.status == "copy_pending"
    assert record.expected_value == legacy_value
    assert unknown.writes == (
        M.MigrationWrite(
            field.target_entity_id,
            legacy_value,
            baseline_value="unknown",
            source_entity_id=field.legacy_entity_id,
            source_value=legacy_value,
        ),
    )

    for unavailable_value in ("unavailable", None):
        unavailable = M.plan_copy_once(
            M.MigrationLedger.empty(),
            {
                field.target_entity_id: unavailable_value,
                field.legacy_entity_id: legacy_value,
            },
            now=NOW,
        )
        assert unavailable.ledger.field(field.target_entity_id).status == (
            "target_unavailable"
        )
        assert unavailable.writes == ()


def test_target_unavailable_ledger_recovers_from_unknown_target() -> None:
    field = M.MIGRATION_FIELD_SPECS[0]
    states = {
        field.target_entity_id: "unavailable",
        field.legacy_entity_id: "sensor.legacy_today",
    }
    unavailable = M.plan_copy_once(M.MigrationLedger.empty(), states, now=NOW)
    assert unavailable.ledger.field(field.target_entity_id).status == (
        "target_unavailable"
    )
    assert unavailable.writes == ()

    states[field.target_entity_id] = "unknown"
    resumed = M.plan_copy_once(unavailable.ledger, states, now=NOW)
    record = resumed.ledger.field(field.target_entity_id)
    assert record.status == "copy_pending"
    assert record.expected_value == "sensor.legacy_today"
    assert record.attempts == 1
    assert resumed.writes == (
        M.MigrationWrite(
            field.target_entity_id,
            "sensor.legacy_today",
            baseline_value="unknown",
            source_entity_id=field.legacy_entity_id,
            source_value="sensor.legacy_today",
        ),
    )

    completed = M.complete_migration_write(
        resumed.ledger,
        target_entity_id=field.target_entity_id,
        observed_value="sensor.legacy_today",
        now=NOW,
    )
    assert completed.field(field.target_entity_id).status == "copied_legacy"


def test_v1_ledger_upgrade_and_missing_helpers_fail_closed() -> None:
    states = _unmigrated_states()
    first_five = M.MIGRATION_FIELD_SPECS[:5]
    raw_v1 = {
        "version": 1,
        "fields": [
            {
                "key": field.key,
                "target_entity_id": field.target_entity_id,
                "legacy_entity_id": field.legacy_entity_id,
                "status": "preserved_new",
                "attempts": 0,
            }
            for field in first_five
        ],
    }
    upgraded = M.migration_ledger_from_dict(raw_v1)
    assert upgraded.version == 2
    assert all(
        upgraded.field(field.target_entity_id).status == "preserved_new"
        for field in first_five
    )
    assert all(
        upgraded.field(field.target_entity_id).status == "not_run"
        for field in M.MIGRATION_FIELD_SPECS[5:]
    )

    missing_target = M.MIGRATION_FIELD_SPECS[5]
    states.pop(missing_target.target_entity_id)
    plan = M.plan_copy_once(upgraded, states, now=NOW)
    assert plan.ledger.field(missing_target.target_entity_id).status == (
        "target_missing"
    )
    assert all(
        write.target_entity_id != missing_target.target_entity_id
        for write in plan.writes
    )
    assert {
        write.target_entity_id for write in plan.writes
    } == {
        spec.target_entity_id for spec in M.MIGRATION_FIELD_SPECS[6:]
    }


def test_corrupt_future_and_incomplete_ledgers_fail_closed() -> None:
    malformed = (
        {},
        {"version": 2, "fields": []},
        {"version": 2, "fields": "invalid"},
        {"version": 1, "fields": []},
    )
    for raw in malformed:
        try:
            M.migration_ledger_from_dict(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(f"malformed ledger was accepted: {raw!r}")

    try:
        M.migration_ledger_from_dict({"version": 3, "fields": []})
    except M.UnsupportedMigrationVersion:
        pass
    else:
        raise AssertionError("future migration ledger was accepted")

    valid = M.MigrationLedger.empty().as_dict()
    valid["fields"][0]["status"] = "copy_pending"
    valid["fields"][0]["expected_value"] = None
    decoded = M.migration_ledger_from_dict(valid)
    assert decoded.fields[0].status == "preserved_invalid_new"
    assert decoded.fields[0].error == "ledger_record_invalid"


def test_restored_target_is_never_rearmed() -> None:
    states = _unmigrated_states()
    target = M.MIGRATION_FIELD_SPECS[0].target_entity_id
    plan = M.plan_copy_once(
        M.MigrationLedger.empty(),
        states,
        now=NOW,
        restored_entity_ids={target},
    )
    assert plan.ledger.field(target).status == "preserved_invalid_new"
    assert target not in {write.target_entity_id for write in plan.writes}


def test_fresh_default_seed_blocks_only_pending_fallback_copy() -> None:
    states = _unmigrated_states()
    fallback = next(
        item
        for item in M.MIGRATION_FIELD_SPECS
        if item.key == "fallback_daily_home_load"
    )
    plan = M.plan_copy_once(
        M.MigrationLedger.empty(),
        states,
        now=NOW,
        blocked_target_entity_ids={fallback.target_entity_id},
    )
    record = plan.ledger.field(fallback.target_entity_id)
    assert record.status == "target_unavailable"
    assert record.error == "default_seed_pending"
    assert fallback.target_entity_id not in {
        write.target_entity_id for write in plan.writes
    }
    assert len(plan.writes) == len(M.MIGRATION_FIELD_SPECS) - 1

    states[fallback.legacy_entity_id] = "20"
    resumed = M.plan_copy_once(plan.ledger, states, now=NOW)
    assert resumed.ledger.field(fallback.target_entity_id).status == "copy_pending"
    assert any(
        write.target_entity_id == fallback.target_entity_id and write.value == 20
        for write in resumed.writes
    )


class FakeStore:
    def __init__(self) -> None:
        self.value: dict[str, Any] | None = None
        self.saves: list[dict[str, Any]] = []

    async def async_load(self) -> dict[str, Any] | None:
        return self.value

    async def async_save(self, value: dict[str, Any]) -> None:
        self.value = value
        self.saves.append(value)


class FakeContext:
    next_id = 0

    def __init__(self, *, user_id: str | None = None) -> None:
        type(self).next_id += 1
        self.id = f"context-{type(self).next_id}"
        self.user_id = user_id
        self.parent_id = None


class FakeState:
    def __init__(
        self,
        state: Any,
        *,
        context: FakeContext | None = None,
        last_updated: datetime = NOW,
    ) -> None:
        self.state = str(state)
        self.attributes = {"editable": False}
        self.context = context or FakeContext()
        self.last_updated = last_updated


class FakeStates:
    def __init__(self, values: dict[str, Any]) -> None:
        self.values = {key: FakeState(value) for key, value in values.items()}

    def get(self, entity_id: str) -> Any:
        return self.values.get(entity_id)


class FakeServices:
    def __init__(self, states: FakeStates) -> None:
        self.states = states
        self.calls: list[tuple[str, str, dict[str, Any], bool]] = []

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        *,
        blocking: bool,
        context: Any,
    ) -> None:
        self.calls.append((domain, service, dict(data), blocking))
        value = data.get("value", data.get("option"))
        self.states.values[data["entity_id"]] = FakeState(
            value,
            context=context,
        )


def _restored_legacy_ids() -> frozenset[str]:
    return frozenset(spec.legacy_entity_id for spec in M.MIGRATION_FIELD_SPECS)


def test_optional_coordinator_never_syncs_back() -> None:
    states = FakeStates(_unmigrated_states())
    services = FakeServices(states)
    hass = SimpleNamespace(
        states=states,
        services=services,
        data={},
        is_running=False,
    )
    store = FakeStore()
    coordinator = M.EMSSharedInputMigrationCoordinator(hass, store)
    core = SimpleNamespace(Context=FakeContext)
    old_homeassistant = sys.modules.get("homeassistant")
    old_core = sys.modules.get("homeassistant.core")
    sys.modules["homeassistant"] = SimpleNamespace(core=core)
    sys.modules["homeassistant.core"] = core
    try:
        ledger = asyncio.run(
            coordinator.async_run_copy_once(
                now=NOW,
                restored_entity_ids=_restored_legacy_ids(),
            )
        )
    finally:
        if old_homeassistant is None:
            sys.modules.pop("homeassistant", None)
        else:
            sys.modules["homeassistant"] = old_homeassistant
        if old_core is None:
            sys.modules.pop("homeassistant.core", None)
        else:
            sys.modules["homeassistant.core"] = old_core
    assert ledger.result == "complete"
    assert len(services.calls) == len(M.MIGRATION_FIELD_SPECS) == 7
    legacy_ids = {field.legacy_entity_id for field in M.MIGRATION_FIELD_SPECS}
    assert not any(call[2]["entity_id"] in legacy_ids for call in services.calls)

    # Persistent ledger makes a second run a read-only no-op.
    asyncio.run(
        coordinator.async_run_copy_once(
            now=NOW,
            restored_entity_ids=_restored_legacy_ids(),
        )
    )
    assert len(services.calls) == 7
    assert store.saves


def test_coordinator_preserves_midflight_user_change() -> None:
    async def run() -> None:
        states = FakeStates(_unmigrated_states())
        services = FakeServices(states)
        hass = SimpleNamespace(
            states=states,
            services=services,
            data={},
            is_running=False,
        )
        target = M.MIGRATION_FIELD_SPECS[0].target_entity_id

        class MutatingStore(FakeStore):
            mutated = False

            async def async_save(self, value: dict[str, Any]) -> None:
                await super().async_save(value)
                if not self.mutated and any(
                    field.get("status") == "copy_pending"
                    for field in value.get("fields", [])
                ):
                    self.mutated = True
                    states.values[target] = FakeState(
                        "sensor.user_selected",
                        context=FakeContext(user_id="user-1"),
                    )

        store = MutatingStore()
        coordinator = M.EMSSharedInputMigrationCoordinator(hass, store)
        core = SimpleNamespace(Context=FakeContext)
        old_homeassistant = sys.modules.get("homeassistant")
        old_core = sys.modules.get("homeassistant.core")
        sys.modules["homeassistant"] = SimpleNamespace(core=core)
        sys.modules["homeassistant.core"] = core
        try:
            ledger = await coordinator.async_run_copy_once(
                now=NOW,
                restored_entity_ids=_restored_legacy_ids(),
            )
        finally:
            if old_homeassistant is None:
                sys.modules.pop("homeassistant", None)
            else:
                sys.modules["homeassistant"] = old_homeassistant
            if old_core is None:
                sys.modules.pop("homeassistant.core", None)
            else:
                sys.modules["homeassistant.core"] = old_core
        assert states.values[target].state == "sensor.user_selected"
        assert ledger.field(target).status == "preserved_changed"
        assert not any(call[2]["entity_id"] == target for call in services.calls)
        assert len(services.calls) == len(M.MIGRATION_FIELD_SPECS) - 1

    asyncio.run(run())


def test_coordinator_rejects_midflight_legacy_source_change() -> None:
    async def run() -> None:
        states = FakeStates(_unmigrated_states())
        services = FakeServices(states)
        hass = SimpleNamespace(
            states=states,
            services=services,
            data={},
            is_running=False,
        )
        spec = M.MIGRATION_FIELD_SPECS[0]

        class MutatingSourceStore(FakeStore):
            mutated = False

            async def async_save(self, value: dict[str, Any]) -> None:
                await super().async_save(value)
                if not self.mutated and any(
                    field.get("status") == "copy_pending"
                    for field in value.get("fields", [])
                ):
                    self.mutated = True
                    states.values[spec.legacy_entity_id] = FakeState(
                        "sensor.user_changed_source",
                        context=FakeContext(user_id="user-1"),
                    )

        coordinator = M.EMSSharedInputMigrationCoordinator(
            hass,
            MutatingSourceStore(),
        )
        core = SimpleNamespace(Context=FakeContext)
        old_homeassistant = sys.modules.get("homeassistant")
        old_core = sys.modules.get("homeassistant.core")
        sys.modules["homeassistant"] = SimpleNamespace(core=core)
        sys.modules["homeassistant.core"] = core
        try:
            ledger = await coordinator.async_run_copy_once(
                now=NOW,
                restored_entity_ids=_restored_legacy_ids(),
            )
        finally:
            if old_homeassistant is None:
                sys.modules.pop("homeassistant", None)
            else:
                sys.modules["homeassistant"] = old_homeassistant
            if old_core is None:
                sys.modules.pop("homeassistant.core", None)
            else:
                sys.modules["homeassistant.core"] = old_core
        record = ledger.field(spec.target_entity_id)
        assert record.status == "preserved_changed"
        assert record.error == "legacy_changed_before_write"
        assert not any(
            call[2]["entity_id"] == spec.target_entity_id
            for call in services.calls
        )

    asyncio.run(run())


def test_retry_revalidates_authorized_legacy_source() -> None:
    spec = next(
        item
        for item in M.MIGRATION_FIELD_SPECS
        if item.key == "fallback_daily_home_load"
    )
    pending = M.MigrationLedger.empty().replace_field(
        replace(
            M.MigrationLedger.empty().field(spec.target_entity_id),
            status="copy_pending",
            expected_value=20.0,
            attempts=1,
            updated_at=NOW.isoformat(),
        )
    )
    base_states = {
        spec.target_entity_id: "0",
        spec.legacy_entity_id: "20",
    }
    valid = M.plan_copy_once(
        pending,
        base_states,
        now=NOW,
        restored_entity_ids={spec.legacy_entity_id},
    )
    assert len(valid.writes) == 1
    assert valid.writes[0].source_value == "20"

    changed_states = dict(base_states)
    changed_states[spec.legacy_entity_id] = "30"
    changed = M.plan_copy_once(
        pending,
        changed_states,
        now=NOW,
        restored_entity_ids={spec.legacy_entity_id},
    )
    assert changed.writes == ()
    changed_record = changed.ledger.field(spec.target_entity_id)
    assert changed_record.status == "preserved_changed"
    assert changed_record.error == "legacy_changed_after_authorization"

    missing = M.plan_copy_once(
        pending,
        {spec.target_entity_id: "0"},
        now=NOW,
        restored_entity_ids=frozenset(),
    )
    assert missing.writes == ()
    assert missing.ledger.field(spec.target_entity_id).status == (
        "legacy_unavailable"
    )


def test_global_lock_serializes_concurrent_entries() -> None:
    async def run() -> None:
        class ContendedStore(FakeStore):
            """Hold the first load so the second coordinator must contend."""

            def __init__(self) -> None:
                super().__init__()
                self.first_load_entered = asyncio.Event()
                self.release_first_load = asyncio.Event()
                self.load_calls = 0
                self.active_loads = 0
                self.max_active_loads = 0

            async def async_load(self) -> dict[str, Any] | None:
                self.load_calls += 1
                self.active_loads += 1
                self.max_active_loads = max(
                    self.max_active_loads,
                    self.active_loads,
                )
                snapshot = self.value
                try:
                    if self.load_calls == 1:
                        self.first_load_entered.set()
                        await self.release_first_load.wait()
                    return snapshot
                finally:
                    self.active_loads -= 1

        states = FakeStates(_unmigrated_states())
        services = FakeServices(states)
        hass = SimpleNamespace(
            states=states,
            services=services,
            data={},
            is_running=False,
        )
        store = ContendedStore()
        first = M.EMSSharedInputMigrationCoordinator(hass, store)
        second = M.EMSSharedInputMigrationCoordinator(hass, store)
        core = SimpleNamespace(Context=FakeContext)
        old_homeassistant = sys.modules.get("homeassistant")
        old_core = sys.modules.get("homeassistant.core")
        sys.modules["homeassistant"] = SimpleNamespace(core=core)
        sys.modules["homeassistant.core"] = core
        try:
            first_task = asyncio.create_task(
                first.async_run_copy_once(
                    now=NOW,
                    restored_entity_ids=_restored_legacy_ids(),
                )
            )
            await asyncio.wait_for(store.first_load_entered.wait(), timeout=1.0)

            second_started = asyncio.Event()

            async def run_second() -> M.MigrationLedger:
                second_started.set()
                return await second.async_run_copy_once(
                    now=NOW,
                    restored_entity_ids=_restored_legacy_ids(),
                )

            second_task = asyncio.create_task(run_second())
            await asyncio.wait_for(second_started.wait(), timeout=1.0)
            await asyncio.sleep(0)
            contended_load_calls = store.load_calls
            contended_max_active_loads = store.max_active_loads
            second_waited_for_lock = not second_task.done()

            store.release_first_load.set()
            ledgers = await asyncio.gather(
                first_task,
                second_task,
            )
        finally:
            store.release_first_load.set()
            if old_homeassistant is None:
                sys.modules.pop("homeassistant", None)
            else:
                sys.modules["homeassistant"] = old_homeassistant
            if old_core is None:
                sys.modules.pop("homeassistant.core", None)
            else:
                sys.modules["homeassistant.core"] = old_core
        assert contended_load_calls == 1
        assert contended_max_active_loads == 1
        assert second_waited_for_lock
        assert store.load_calls == 2
        assert store.max_active_loads == 1
        assert all(ledger.result == "complete" for ledger in ledgers)
        assert all(
            all(field.status == "copied_legacy" for field in ledger.fields)
            for ledger in ledgers
        )
        assert len(services.calls) == len(M.MIGRATION_FIELD_SPECS)
        assert len(hass.data) == 1

    asyncio.run(run())


def test_runtime_reload_defers_copy_until_restart() -> None:
    states = FakeStates(_unmigrated_states())
    services = FakeServices(states)
    hass = SimpleNamespace(
        states=states,
        services=services,
        data={},
        is_running=True,
    )
    store = FakeStore()
    coordinator = M.EMSSharedInputMigrationCoordinator(hass, store)
    ledger = asyncio.run(
        coordinator.async_run_copy_once(
            now=NOW,
            restored_entity_ids=frozenset(),
        )
    )
    assert ledger.result == "pending"
    assert services.calls == []
    assert store.saves == []


def test_fresh_framework_legacy_default_is_never_migrated() -> None:
    states = _unmigrated_states()
    fresh = M.plan_copy_once(
        M.MigrationLedger.empty(),
        states,
        now=NOW,
        restored_entity_ids=frozenset(),
    )
    assert fresh.writes == ()
    assert all(
        field.status == "legacy_not_restored"
        for field in fresh.ledger.fields
    )

    trusted_seeded = {
        "input_number.hoymiles_rce_fallback_daily_load": 20,
        "input_number.hoymiles_tariff_charge_efficiency": 95,
        "input_number.hoymiles_tariff_discharge_efficiency": 95,
    }
    seeded_states = dict(states)
    seeded_states.update(trusted_seeded)
    seeded_fresh = M.plan_copy_once(
        M.MigrationLedger.empty(),
        seeded_states,
        now=NOW,
        restored_entity_ids=frozenset(),
        trusted_seeded_source_values=trusted_seeded,
    )
    assert {
        write.source_entity_id for write in seeded_fresh.writes
    } == set(trusted_seeded)
    rated = next(
        spec for spec in M.MIGRATION_FIELD_SPECS if spec.kind == "rated_power"
    )
    assert seeded_fresh.ledger.field(rated.target_entity_id).status == (
        "legacy_not_restored"
    )

    restored = M.plan_copy_once(
        M.MigrationLedger.empty(),
        states,
        now=NOW,
        restored_entity_ids=_restored_legacy_ids(),
    )
    assert len(restored.writes) == len(M.MIGRATION_FIELD_SPECS)


def test_quarantined_store_gets_durable_no_replay_tombstone() -> None:
    class QuarantiningStore(FakeStore):
        first_load = True

        async def async_load_with_presence(self) -> tuple[bool, Any]:
            if self.first_load:
                self.first_load = False
                return True, None
            return True, self.value

    states = FakeStates(_unmigrated_states())
    services = FakeServices(states)
    hass = SimpleNamespace(
        states=states,
        services=services,
        data={},
        is_running=False,
    )
    store = QuarantiningStore()
    coordinator = M.EMSSharedInputMigrationCoordinator(hass, store)
    first = asyncio.run(
        coordinator.async_run_copy_once(
            now=NOW,
            restored_entity_ids=frozenset(),
        )
    )
    second = asyncio.run(
        coordinator.async_run_copy_once(
            now=NOW,
            restored_entity_ids=frozenset(),
        )
    )
    assert first == second
    assert first.result == "completed_with_errors"
    assert all(field.status == "storage_unreadable" for field in first.fields)
    assert services.calls == []
    assert len(store.saves) == 2
    assert store.saves[0] == store.saves[1] == first.as_dict()


def main() -> None:
    test_copy_once_idempotence_and_restart_recovery()
    test_preserve_new_missing_and_invalid()
    test_entity_id_unknown_is_unset_but_unavailable_fails_closed()
    test_target_unavailable_ledger_recovers_from_unknown_target()
    test_v1_ledger_upgrade_and_missing_helpers_fail_closed()
    test_corrupt_future_and_incomplete_ledgers_fail_closed()
    test_restored_target_is_never_rearmed()
    test_fresh_default_seed_blocks_only_pending_fallback_copy()
    test_optional_coordinator_never_syncs_back()
    test_coordinator_preserves_midflight_user_change()
    test_coordinator_rejects_midflight_legacy_source_change()
    test_retry_revalidates_authorized_legacy_source()
    test_global_lock_serializes_concurrent_entries()
    test_runtime_reload_defers_copy_until_restart()
    test_fresh_framework_legacy_default_is_never_migrated()
    test_quarantined_store_gets_durable_no_replay_tombstone()
    print("Shared EMS migration: 16 deterministic groups passed")


if __name__ == "__main__":
    main()
