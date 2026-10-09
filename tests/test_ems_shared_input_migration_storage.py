"""Exact Home Assistant Store-envelope coverage for Shared EMS migration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CoreState, HomeAssistant

from custom_components.hoymiles_hit_modbus.ems_shared_input_migration import (
    EMSSharedInputMigrationCoordinator,
    EMSSharedInputMigrationStore,
    MIGRATION_FIELD_SPECS,
    MIGRATION_STORAGE_KEY,
    MIGRATION_VERSION,
    migration_ledger_from_dict,
)


def _v1_ledger() -> dict[str, Any]:
    seeded = {
        "forecast_today": "sensor.legacy_today",
        "forecast_tomorrow": "sensor.legacy_tomorrow",
        "forecast_day3": "sensor.legacy_day3",
        "fallback_daily_home_load": 12.5,
        "inverter_rated_power_each": "10 kW",
    }
    return {
        "version": 1,
        "fields": [
            {
                "key": spec.key,
                "target_entity_id": spec.target_entity_id,
                "legacy_entity_id": spec.legacy_entity_id,
                "status": "copied_legacy",
                "expected_value": seeded[spec.key],
                "attempts": 1,
                "updated_at": "2026-09-01T08:00:00+00:00",
                "error": None,
            }
            for spec in MIGRATION_FIELD_SPECS[:5]
        ],
    }


def test_real_ha_store_upgrades_v1_envelope_without_replaying_fields(
    tmp_path: Path,
) -> None:
    assert HA_VERSION == "2026.8.2"

    async def run() -> None:
        config_dir = tmp_path / "config"
        storage_dir = config_dir / ".storage"
        storage_dir.mkdir(parents=True)
        storage_path = storage_dir / MIGRATION_STORAGE_KEY
        storage_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "minor_version": 1,
                    "key": MIGRATION_STORAGE_KEY,
                    "data": _v1_ledger(),
                }
            ),
            encoding="utf-8",
        )

        hass = HomeAssistant(str(config_dir))
        hass.set_state(CoreState.running)
        try:
            store = EMSSharedInputMigrationStore(hass)
            loaded = await store.async_load()
            assert loaded is not None
            ledger = migration_ledger_from_dict(loaded)
            assert ledger.version == MIGRATION_VERSION == 2
            assert all(
                ledger.field(spec.target_entity_id).status == "copied_legacy"
                for spec in MIGRATION_FIELD_SPECS[:5]
            )
            assert all(
                ledger.field(spec.target_entity_id).status == "not_run"
                for spec in MIGRATION_FIELD_SPECS[5:]
            )

            envelope = json.loads(storage_path.read_text(encoding="utf-8"))
            assert envelope["version"] == 2
            assert envelope["minor_version"] == 1
            assert envelope["key"] == MIGRATION_STORAGE_KEY
            assert envelope["data"] == loaded
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_real_ha_store_corruption_never_rearms_copy_once(
    tmp_path: Path,
) -> None:
    assert HA_VERSION == "2026.8.2"

    async def run() -> None:
        config_dir = tmp_path / "config"
        storage_dir = config_dir / ".storage"
        storage_dir.mkdir(parents=True)
        storage_path = storage_dir / MIGRATION_STORAGE_KEY
        storage_path.write_text("{broken-json", encoding="utf-8")

        hass = HomeAssistant(str(config_dir))
        hass.set_state(CoreState.starting)
        service_writes: list[str] = []

        async def record_write(call: Any) -> None:
            service_writes.append(f"{call.domain}.{call.service}")

        hass.services.async_register("input_text", "set_value", record_write)
        hass.services.async_register("input_number", "set_value", record_write)
        hass.services.async_register("input_select", "select_option", record_write)
        first = MIGRATION_FIELD_SPECS[0]
        hass.states.async_set(
            first.target_entity_id,
            "",
            {"editable": False},
        )
        hass.states.async_set(first.legacy_entity_id, "sensor.legacy_today")
        try:
            first = await EMSSharedInputMigrationCoordinator.for_home_assistant(
                hass
            ).async_run_copy_once(
                now=datetime.now(timezone.utc),
                restored_entity_ids=set(),
            )
            assert first.result == "completed_with_errors"
            assert all(field.status == "storage_unreadable" for field in first.fields)
            assert service_writes == []

            # Store quarantines malformed JSON. A completely new coordinator
            # must load the durable primary tombstone and remain read-only.
            second = await EMSSharedInputMigrationCoordinator.for_home_assistant(
                hass
            ).async_run_copy_once(
                now=datetime.now(timezone.utc),
                restored_entity_ids=set(),
            )
            assert second == first
            assert service_writes == []
            envelope = json.loads(storage_path.read_text(encoding="utf-8"))
            assert envelope["data"] == first.as_dict()
            quarantined = list(
                storage_dir.glob(f"{MIGRATION_STORAGE_KEY}.corrupt.*")
            )
            assert len(quarantined) == 1
            assert quarantined[0].read_bytes() == b"{broken-json"
            # HA uses an ISO timestamp on POSIX; only Windows requires the
            # integration's portable-name fallback after HA's rename fails.
            if os.name == "nt":
                assert ":" not in quarantined[0].name
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())
