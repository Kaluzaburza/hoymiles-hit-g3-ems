"""Exact HA Store-envelope coverage for I2 initial-default ledger upgrades."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Any

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import CoreState, HomeAssistant

from custom_components.hoymiles_hit_modbus.ems_initial_defaults import (
    DEFAULT_SEED_SPECS,
    DEFAULT_SEED_STORAGE_KEY,
    DEFAULT_SEED_VERSION,
    EMSInitialDefaultSeeder,
    EMSInitialDefaultSeedStore,
    FreshInstallEvidence,
    async_stage_fresh_install_authorization,
    default_seed_ledger_from_dict,
)


def _v1_ledger() -> dict[str, Any]:
    return {
        "version": 1,
        "fields": [
            {
                "key": item.key,
                "target_entity_id": item.target_entity_id,
                "status": "preserved_restored",
                "attempts": 0,
                "updated_at": "2026-09-02T08:00:00+00:00",
            }
            for item in DEFAULT_SEED_SPECS
            if item.introduced_version == 1
        ],
    }


def test_real_ha_store_upgrade_never_arms_new_defaults(tmp_path: Path) -> None:
    assert HA_VERSION == "2026.8.2"

    async def run() -> None:
        config_dir = tmp_path / "config"
        storage_dir = config_dir / ".storage"
        storage_dir.mkdir(parents=True)
        storage_path = storage_dir / DEFAULT_SEED_STORAGE_KEY
        storage_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "minor_version": 1,
                    "key": DEFAULT_SEED_STORAGE_KEY,
                    "data": _v1_ledger(),
                }
            ),
            encoding="utf-8",
        )

        hass = HomeAssistant(str(config_dir))
        hass.set_state(CoreState.running)
        try:
            loaded = await EMSInitialDefaultSeedStore(hass).async_load()
            assert loaded is not None
            ledger = default_seed_ledger_from_dict(loaded)
            assert ledger.version == DEFAULT_SEED_VERSION == 2
            assert ledger.authorization is None
            assert ledger.result == "not_authorized"
            assert all(
                ledger.field(item.target_entity_id).status == "preserved_restored"
                for item in DEFAULT_SEED_SPECS
                if item.introduced_version == 1
            )
            assert all(
                ledger.field(item.target_entity_id).status == "not_run"
                for item in DEFAULT_SEED_SPECS
                if item.introduced_version == 2
            )

            envelope = json.loads(storage_path.read_text(encoding="utf-8"))
            assert envelope["version"] == 2
            assert envelope["minor_version"] == 1
            assert envelope["key"] == DEFAULT_SEED_STORAGE_KEY
            assert envelope["data"] == loaded
        finally:
            await hass.async_stop(force=True)

    asyncio.run(run())


def test_real_ha_corrupt_seed_store_never_reauthorizes_defaults(
    tmp_path: Path,
) -> None:
    assert HA_VERSION == "2026.8.2"

    async def run() -> None:
        config_dir = tmp_path / "config"
        storage_dir = config_dir / ".storage"
        storage_dir.mkdir(parents=True)
        storage_path = storage_dir / DEFAULT_SEED_STORAGE_KEY
        storage_path.write_text("{broken-json", encoding="utf-8")
        source_dir = tmp_path / "source"
        source_dir.mkdir()
        scheduler_source = source_dir / "scheduler.yaml"
        shared_source = source_dir / "shared.yaml"
        scheduler_source.write_bytes(b"scheduler-v2")
        shared_source.write_bytes(b"shared-v2")
        packages_dir = config_dir / "packages"
        packages_dir.mkdir()
        evidence = FreshInstallEvidence(
            True,
            "absent",
            packages_dir / "hoymiles_ems_scheduler.yaml",
            packages_dir / "hoymiles_ems_shared_inputs.yaml",
        )

        hass = HomeAssistant(str(config_dir))
        hass.set_state(CoreState.starting)
        service_writes: list[str] = []

        async def record_write(call: Any) -> None:
            service_writes.append(f"{call.domain}.{call.service}")

        hass.services.async_register("input_boolean", "turn_on", record_write)
        hass.services.async_register("input_boolean", "turn_off", record_write)
        hass.services.async_register("input_number", "set_value", record_write)
        hass.services.async_register("input_datetime", "set_datetime", record_write)
        try:
            first = await async_stage_fresh_install_authorization(
                hass,
                evidence,
                scheduler_source=scheduler_source,
                shared_package_source=shared_source,
                package_version="1.5.8",
                now=datetime.now(timezone.utc),
            )
            second = await async_stage_fresh_install_authorization(
                hass,
                evidence,
                scheduler_source=scheduler_source,
                shared_package_source=shared_source,
                package_version="1.5.8",
                now=datetime.now(timezone.utc),
            )
            assert not first and not second
            ledger = await EMSInitialDefaultSeeder.for_home_assistant(
                hass
            ).async_run_once(
                now=datetime.now(timezone.utc),
                restored_entity_ids=set(),
            )
            assert ledger.authorization is None
            assert ledger.pending_authorization is None
            assert all(field.status == "preserved_invalid" for field in ledger.fields)
            assert service_writes == []
            envelope = json.loads(storage_path.read_text(encoding="utf-8"))
            assert envelope["data"] == ledger.as_dict()
            quarantined = list(
                storage_dir.glob(f"{DEFAULT_SEED_STORAGE_KEY}.corrupt.*")
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
