"""EMS for Hoymiles HIT-(5–20)L-G3 Home Assistant integration."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import voluptuous as vol

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir

from .assets import (
    FRONTEND_BOOTSTRAP_URL,
    FRONTEND_RESOURCE_URL,
    FRONTEND_STATIC_ROUTE,
    RESOURCE_ROOT,
    async_install_assets,
)
from .catalog import async_match_entities, matched_source_count
from .const import (
    ATTR_ENABLED,
    ATTR_OVERWRITE,
    ATTR_PAUSED,
    ATTR_POLICY,
    CONF_RESOLVED_SOURCE_DEVICE_ID,
    CONF_SOURCE_DEVICE_ID,
    DOMAIN,
    EMS_PACKAGE_SENTINEL,
    EMS_PACKAGE_VERSION,
    EMS_PACKAGE_VERSION_ENTITY,
    PLATFORMS,
    SERVICE_INSTALL_ASSETS,
    SERVICE_MASTER_STOP,
    SERVICE_RESUME_AFTER_MASTER_STOP,
    SERVICE_SET_EMS_PAUSED,
    SERVICE_SET_POLICY_ENABLED,
    VERSION,
)
from .installation_identity import async_get_or_create_installation_identity
from .models import RuntimeData
from .source_device import (
    async_resolve_source_device,
    persist_resolved_source_entry,
)
from .supervisor_sensor import (
    async_resume_supervisor_after_master_stop,
    async_request_supervisor_master_stop,
    async_set_supervisor_paused,
    async_set_supervisor_policy_enabled,
    notify_supervisor_guard,
)
from .support_http import HoymilesSupportBundleView
from .esphome_upgrade_http import HoymilesEsphomeUpgradeView
from .execution_history_http import HoymilesExecutionHistoryView
from .profit_http import HoymilesProfitView
from .timeline_sensor import (
    TIMELINE_POLICY_IDS,
    timeline_entity_id,
    timeline_unique_id,
)


_LOGGER = logging.getLogger(__name__)
STATIC_URL = f"/api/{DOMAIN}/{FRONTEND_STATIC_ROUTE}"
FRONTEND_MODULE_URL = FRONTEND_BOOTSTRAP_URL
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

INSTALL_ASSETS_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_OVERWRITE, default=False): cv.boolean,
    }
)
SET_EMS_PAUSED_SCHEMA = vol.Schema({vol.Required(ATTR_PAUSED): cv.boolean})
SET_POLICY_ENABLED_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_POLICY): vol.In(("rce", "tariff", "rcm", "balancing")),
        vol.Required(ATTR_ENABLED): cv.boolean,
    }
)
EMS_PACKAGE_DOCS_URL = (
    "https://github.com/Kaluzaburza/hoymiles-hit-g3-ems"
    "#ems-automation"
)
FRONTEND_ASSETS_RESTART_ISSUE_ID = "frontend_assets_restart_required"
FRONTEND_ASSETS_INSTALL_FAILED_ISSUE_ID = "frontend_assets_install_failed"
_BASELINE_ENTITY_ID = "sensor.hoymiles_ems_baseline_energy_timeline"
_BASELINE_TRANSLATION_KEY = "ems_baseline_energy_timeline"


class TimelineIdentityCollisionError(RuntimeError):
    """Block setup when an exact timeline identity is already foreign-owned."""


class BaselineIdentityCollisionError(RuntimeError):
    """Block setup when the neutral baseline identity is foreign-owned."""


def _ems_package_issue_id(entry: ConfigEntry) -> str:
    """Return a stable repair issue id for a config entry."""
    return f"ems_package_not_loaded_{entry.entry_id}"


def _ems_package_restart_issue_id(entry: ConfigEntry) -> str:
    """Return the stable issue id for an EMS package activation restart."""
    return f"ems_package_restart_required_{entry.entry_id}"


async def _async_prepare_frontend_assets(
    hass: HomeAssistant,
) -> tuple[list, bool, bool]:
    """Install optional assets without making device setup depend on them."""
    from .ems_initial_defaults import (  # noqa: PLC0415
        FreshInstallEvidence,
        async_authorize_fresh_install,
        async_capture_fresh_install_evidence,
        async_stage_fresh_install_authorization,
    )

    localized = "pl" if hass.config.language.startswith("pl") else "en"
    scheduler_source = (
        RESOURCE_ROOT
        / "home_assistant"
        / localized
        / "hoymiles_ems_scheduler.yaml"
    )
    shared_package_source = (
        RESOURCE_ROOT
        / "home_assistant"
        / localized
        / "hoymiles_ems_shared_inputs.yaml"
    )
    config_path = Path(hass.config.config_dir)
    try:
        fresh_seed_evidence = await async_capture_fresh_install_evidence(hass)
    except Exception:  # noqa: BLE001 - missing proof must only disable seeding
        fresh_seed_evidence = FreshInstallEvidence(
            False,
            "capture_failed",
            config_path / "packages" / "hoymiles_ems_scheduler.yaml",
            config_path / "packages" / "hoymiles_ems_shared_inputs.yaml",
        )
        _LOGGER.exception("Cannot capture first-install EMS helper evidence")
    try:
        await async_stage_fresh_install_authorization(
            hass,
            fresh_seed_evidence,
            scheduler_source=scheduler_source,
            shared_package_source=shared_package_source,
            package_version=EMS_PACKAGE_VERSION,
            now=datetime.now(timezone.utc),
        )
    except Exception:  # noqa: BLE001 - missing proof must only disable seeding
        _LOGGER.exception("Cannot stage first-install EMS helper defaults")
    try:
        # Frontend registers /local only when www exists during frontend setup.
        # Capture that fact before the installer is allowed to create www.
        frontend_local_ready = await hass.async_add_executor_job(
            (Path(hass.config.config_dir) / "www").is_dir
        )
        paths = await async_install_assets(
            hass,
            overwrite=False,
            publish_frontend=frontend_local_ready,
        )
    except Exception:  # noqa: BLE001 - optional assets must not disable devices
        _LOGGER.exception(
            "Failed to install optional Hoymiles dashboard/EMS assets"
        )
        ir.async_create_issue(
            hass,
            DOMAIN,
            FRONTEND_ASSETS_INSTALL_FAILED_ISSUE_ID,
            is_fixable=False,
            issue_domain=DOMAIN,
            learn_more_url=EMS_PACKAGE_DOCS_URL,
            severity=ir.IssueSeverity.WARNING,
            translation_key="frontend_assets_install_failed",
        )
        return [], False, False

    try:
        await async_authorize_fresh_install(
            hass,
            fresh_seed_evidence,
            written_paths=frozenset(paths),
            scheduler_source=scheduler_source,
            shared_package_source=shared_package_source,
            package_version=EMS_PACKAGE_VERSION,
            now=datetime.now(timezone.utc),
        )
    except Exception:  # noqa: BLE001 - missing proof must only disable seeding
        _LOGGER.exception("Cannot authorize first-install EMS helper defaults")

    ir.async_delete_issue(
        hass,
        DOMAIN,
        FRONTEND_ASSETS_INSTALL_FAILED_ISSUE_ID,
    )
    return paths, frontend_local_ready, True


def _async_update_ems_package_issue(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Explain the remaining YAML or restart step for the EMS package."""
    issue_id = _ems_package_issue_id(entry)
    restart_issue_id = _ems_package_restart_issue_id(entry)
    if hass.states.get(EMS_PACKAGE_SENTINEL) is not None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        package_version = hass.states.get(EMS_PACKAGE_VERSION_ENTITY)
        if (
            package_version is not None
            and package_version.state == EMS_PACKAGE_VERSION
        ):
            ir.async_delete_issue(hass, DOMAIN, restart_issue_id)
            return
        ir.async_create_issue(
            hass,
            DOMAIN,
            restart_issue_id,
            is_fixable=False,
            issue_domain=DOMAIN,
            learn_more_url=EMS_PACKAGE_DOCS_URL,
            severity=ir.IssueSeverity.WARNING,
            translation_key="ems_package_restart_required",
        )
        return
    ir.async_delete_issue(hass, DOMAIN, restart_issue_id)
    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        is_fixable=False,
        issue_domain=DOMAIN,
        learn_more_url=EMS_PACKAGE_DOCS_URL,
        severity=ir.IssueSeverity.WARNING,
        translation_key="ems_package_not_loaded",
    )


def _async_reconcile_entity_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    runtime: RuntimeData,
) -> None:
    """Remove stale proxies and migrate active entities to stable identities."""
    entity_registry = er.async_get(hass)
    active_translation_keys = {
        matched_entity.catalog["translation_key"]
        for entities in runtime.entities.values()
        for matched_entity in entities
    }
    # The optimizer is an integration-native entity rather than an ESPHome
    # catalog proxy, so it must survive catalog reconciliation.
    active_translation_keys.add("rce_optimized_plan")
    active_translation_keys.add("tariff_charge_plan")
    active_translation_keys.add("tariff_import_price_schedule")
    active_translation_keys.add("rcm_voltage_plan")
    active_translation_keys.add("setup_status")
    active_translation_keys.add("ems_shared_inputs")
    active_translation_keys.add(_BASELINE_TRANSLATION_KEY)
    active_translation_keys.add("ems_supervisor")
    active_translation_keys.add("ems_supervisor_canonical_plan")
    active_translation_keys.add("ems_supervisor_grid_to_battery_energy_v2")
    active_translation_keys.add("rce_automation_plan_timeline")
    active_translation_keys.add("tariff_automation_plan_timeline")
    active_translation_keys.add("rcm_automation_plan_timeline")

    for registry_entry in er.async_entries_for_config_entry(
        entity_registry,
        entry.entry_id,
    ):
        if (
            registry_entry.platform != DOMAIN
            or not registry_entry.translation_key
        ):
            continue

        if registry_entry.translation_key not in active_translation_keys:
            entity_registry.async_remove(registry_entry.entity_id)
            _LOGGER.info(
                "Removed stale Hoymiles entity %s (translation key: %s)",
                registry_entry.entity_id,
                registry_entry.translation_key,
            )
            continue

        desired_entity_id = (
            _BASELINE_ENTITY_ID
            if registry_entry.translation_key
            == _BASELINE_TRANSLATION_KEY
            else (
                f"{registry_entry.domain}."
                f"hoymiles_hit_{registry_entry.translation_key}"
            )
        )
        desired_unique_id = (
            f"{entry.entry_id}_{registry_entry.translation_key}"
        )
        entity_id_changed = registry_entry.entity_id != desired_entity_id
        unique_id_changed = registry_entry.unique_id != desired_unique_id
        if not entity_id_changed and not unique_id_changed:
            continue

        existing = entity_registry.async_get(desired_entity_id)
        if (
            entity_id_changed
            and existing is not None
            and existing.entity_id != registry_entry.entity_id
        ):
            _LOGGER.warning(
                "Cannot migrate %s to %s because the target already exists",
                registry_entry.entity_id,
                desired_entity_id,
            )
            continue

        old_entity_id = registry_entry.entity_id
        update: dict[str, str] = {}
        if entity_id_changed:
            update["new_entity_id"] = desired_entity_id
        if unique_id_changed:
            update["new_unique_id"] = desired_unique_id
        try:
            entity_registry.async_update_entity(old_entity_id, **update)
        except ValueError:
            _LOGGER.exception(
                "Cannot migrate registry identity for %s",
                old_entity_id,
            )
            continue
        _LOGGER.info(
            "Migrated Hoymiles registry identity from %s to %s",
            old_entity_id,
            desired_entity_id,
        )


def _async_prepare_timeline_entity_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Normalize all three timeline identities before platform setup."""

    entity_registry = er.async_get(hass)
    contracts = []
    for policy_id in TIMELINE_POLICY_IDS:
        unique_id = timeline_unique_id(entry.entry_id, policy_id)
        desired_entity_id = timeline_entity_id(policy_id)
        deleted_key = ("sensor", DOMAIN, unique_id)
        active_entity_id = entity_registry.async_get_entity_id(
            "sensor",
            DOMAIN,
            unique_id,
        )
        active_entry = (
            entity_registry.async_get(active_entity_id)
            if active_entity_id is not None
            else None
        )
        desired_entry = entity_registry.async_get(desired_entity_id)
        deleted_entry = entity_registry.deleted_entities.get(deleted_key)

        if (
            active_entry is not None
            and active_entry.config_entry_id != entry.entry_id
        ):
            raise TimelineIdentityCollisionError(
                f"{policy_id} timeline unique ID belongs to another config entry"
            )
        if desired_entry is not None and (
            active_entry is None or desired_entry.id != active_entry.id
        ):
            raise TimelineIdentityCollisionError(
                f"{policy_id} timeline entity ID is already in use"
            )
        if (
            deleted_entry is not None
            and deleted_entry.config_entry_id != entry.entry_id
        ):
            raise TimelineIdentityCollisionError(
                f"{policy_id} deleted timeline identity belongs to another config entry"
            )
        contracts.append(
            (
                desired_entity_id,
                active_entry,
                deleted_key,
                deleted_entry,
            )
        )

    deleted_changed = False
    for desired_entity_id, active_entry, deleted_key, deleted_entry in contracts:
        if active_entry is not None and active_entry.entity_id != desired_entity_id:
            entity_registry.async_update_entity(
                active_entry.entity_id,
                new_entity_id=desired_entity_id,
            )
        if deleted_entry is not None and deleted_entry.entity_id != desired_entity_id:
            entity_registry.deleted_entities[deleted_key] = (
                deleted_entry.__replace__(entity_id=desired_entity_id)
            )
            deleted_changed = True

    if deleted_changed:
        entity_registry.async_schedule_save()


def _async_prepare_baseline_entity_registry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Reserve the exact policy-neutral baseline identity before setup."""

    entity_registry = er.async_get(hass)
    unique_id = f"{entry.entry_id}_{_BASELINE_TRANSLATION_KEY}"
    deleted_key = ("sensor", DOMAIN, unique_id)
    active_entity_id = entity_registry.async_get_entity_id(
        "sensor",
        DOMAIN,
        unique_id,
    )
    active_entry = (
        entity_registry.async_get(active_entity_id)
        if active_entity_id is not None
        else None
    )
    desired_entry = entity_registry.async_get(_BASELINE_ENTITY_ID)
    deleted_entry = entity_registry.deleted_entities.get(deleted_key)

    if active_entry is not None and active_entry.config_entry_id != entry.entry_id:
        raise BaselineIdentityCollisionError(
            "baseline timeline unique ID belongs to another config entry"
        )
    if desired_entry is not None and (
        active_entry is None or desired_entry.id != active_entry.id
    ):
        raise BaselineIdentityCollisionError(
            "baseline timeline entity ID is already in use"
        )
    if (
        deleted_entry is not None
        and deleted_entry.config_entry_id != entry.entry_id
    ):
        raise BaselineIdentityCollisionError(
            "deleted baseline identity belongs to another config entry"
        )

    if (
        active_entry is not None
        and active_entry.entity_id != _BASELINE_ENTITY_ID
    ):
        entity_registry.async_update_entity(
            active_entry.entity_id,
            new_entity_id=_BASELINE_ENTITY_ID,
        )
    if (
        deleted_entry is not None
        and deleted_entry.entity_id != _BASELINE_ENTITY_ID
    ):
        entity_registry.deleted_entities[deleted_key] = (
            deleted_entry.__replace__(entity_id=_BASELINE_ENTITY_ID)
        )
        entity_registry.async_schedule_save()


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register integration-wide services."""
    hass.data.setdefault(DOMAIN, {})

    try:
        # One installation-wide random UUID, persisted before any diagnostic
        # report can expose it. It is intentionally independent of entries and
        # device, network or user identifiers.
        await async_get_or_create_installation_identity(hass)
    except Exception:  # noqa: BLE001 - diagnostics must not disable devices
        _LOGGER.exception(
            "Cannot initialize the anonymous diagnostics installation ID"
        )

    # Materialize every /local frontend dependency before publishing its URL.
    # Availability is restart-gated: frontend registers /local only when www
    # exists during frontend startup. The required post-install/update restart
    # therefore makes the files routable before Lovelace requests them.
    paths, frontend_local_ready, frontend_assets_ready = (
        await _async_prepare_frontend_assets(hass)
    )
    try:
        # Installation-wide and independent of source-device availability.
        # A valid token exists only on the restart after a fresh managed
        # package install; every later call is a ledger-backed no-op.
        from .ems_initial_defaults import EMSInitialDefaultSeeder  # noqa: PLC0415

        await EMSInitialDefaultSeeder.for_home_assistant(hass).async_run_once(
            now=datetime.now(timezone.utc)
        )
    except Exception:  # noqa: BLE001 - defaults fail closed, integration starts
        _LOGGER.exception("First-install EMS helper defaults were not applied")
    await hass.http.async_register_static_paths(
        [
            StaticPathConfig(
                STATIC_URL,
                str(RESOURCE_ROOT / "www"),
                cache_headers=False,
            )
        ]
    )
    # Publish one canonical ES module. It defines the dashboard strategy before
    # registering the remaining custom cards. Storage mode may also list this
    # exact URL as a Lovelace resource; browser module loading is idempotent.
    if frontend_assets_ready and frontend_local_ready:
        add_extra_js_url(hass, FRONTEND_MODULE_URL)
        ir.async_delete_issue(
            hass,
            DOMAIN,
            FRONTEND_ASSETS_RESTART_ISSUE_ID,
        )
    elif frontend_assets_ready:
        ir.async_create_issue(
            hass,
            DOMAIN,
            FRONTEND_ASSETS_RESTART_ISSUE_ID,
            is_fixable=False,
            issue_domain=DOMAIN,
            learn_more_url=EMS_PACKAGE_DOCS_URL,
            severity=ir.IssueSeverity.WARNING,
            translation_key="frontend_assets_restart_required",
        )
    if paths:
        _LOGGER.info(
            "Installed initial Hoymiles assets: %s",
            ", ".join(str(path) for path in paths),
        )
    hass.http.register_view(HoymilesSupportBundleView())
    hass.http.register_view(HoymilesEsphomeUpgradeView())
    hass.http.register_view(HoymilesExecutionHistoryView())
    hass.http.register_view(HoymilesProfitView())

    async def async_handle_install_assets(call: ServiceCall) -> None:
        paths = await async_install_assets(
            hass,
            overwrite=call.data[ATTR_OVERWRITE],
            publish_frontend=frontend_local_ready,
        )
        _LOGGER.info(
            "Installed %s Hoymiles dashboard/automation assets: %s",
            len(paths),
            ", ".join(str(path) for path in paths),
        )

    async def async_handle_master_stop(_call: ServiceCall) -> None:
        """Run the integration-owned transactional MASTER STOP."""
        try:
            await async_request_supervisor_master_stop(hass)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(
                "EMS Supervisor MASTER STOP failed closed"
            ) from err

    async def async_handle_set_ems_paused(call: ServiceCall) -> None:
        """Apply the explicit pause latch and its ordered mode transition."""

        try:
            await async_set_supervisor_paused(hass, call.data[ATTR_PAUSED])
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError("EMS pause operation failed closed") from err

    async def async_handle_set_policy_enabled(call: ServiceCall) -> None:
        """Apply one serialized enabled+allow policy operation."""

        try:
            await async_set_supervisor_policy_enabled(
                hass,
                call.data[ATTR_POLICY],
                call.data[ATTR_ENABLED],
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError("EMS policy operation failed closed") from err

    async def async_handle_resume_after_master_stop(_call: ServiceCall) -> None:
        """Restore saved policy choices after a completed MASTER STOP."""

        try:
            await async_resume_supervisor_after_master_stop(hass)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError("EMS MASTER STOP resume failed closed") from err

    hass.services.async_register(
        DOMAIN,
        SERVICE_INSTALL_ASSETS,
        async_handle_install_assets,
        schema=INSTALL_ASSETS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MASTER_STOP,
        async_handle_master_stop,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_EMS_PAUSED,
        async_handle_set_ems_paused,
        schema=SET_EMS_PAUSED_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_POLICY_ENABLED,
        async_handle_set_policy_enabled,
        schema=SET_POLICY_ENABLED_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESUME_AFTER_MASTER_STOP,
        async_handle_resume_after_master_stop,
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Set up a localized Hoymiles device from an ESPHome device."""
    # Keep integration bootstrap lightweight; the sensor/event imports used by
    # the broker are needed only after a config entry is actually set up.
    from .ems_shared_inputs import EMSSharedInputsCoordinator  # noqa: PLC0415

    source_device_id = entry.data[CONF_SOURCE_DEVICE_ID]
    resolution = await async_resolve_source_device(
        hass,
        source_device_id,
        async_match_entities,
        entry.data.get(CONF_RESOLVED_SOURCE_DEVICE_ID),
    )
    source_device = resolution.source_device
    matched = resolution.matched
    if source_device is None:
        _LOGGER.error(
            "ESPHome source device %s is not live and has no unambiguous "
            "compatible successor (exact: %s, compatible: %s)",
            source_device_id,
            resolution.exact_successor_count,
            resolution.compatible_successor_count,
        )
        return False

    if resolution.rebound:
        resolved_device_id = resolution.resolved_device_id
        # Preserve source_device_id as the stable composite anchor. A future
        # split successor continues to point to that old id, not necessarily to
        # the currently resolved child id.
        persist_resolved_source_entry(
            hass,
            entry,
            resolution,
            CONF_RESOLVED_SOURCE_DEVICE_ID,
        )
        _LOGGER.info(
            "Rebound ESPHome source device %s to verified split successor %s",
            source_device_id,
            resolved_device_id,
        )

    shared_inputs = EMSSharedInputsCoordinator(
        hass,
        config_entry_id=entry.entry_id,
        source_device_model=source_device.model,
    )
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = RuntimeData(
        source_device=source_device,
        entities=matched,
        shared_inputs=shared_inputs,
    )
    notify_supervisor_guard(hass)
    source_count = matched_source_count(matched)
    catalog_count = sum(len(entities) for entities in matched.values())
    if source_count < catalog_count:
        _LOGGER.warning(
            "%s of %s Hoymiles entities are present in ESPHome; "
            "the remaining proxies will stay unavailable until the "
            "ESPHome firmware is updated",
            source_count,
            catalog_count,
        )
    _async_prepare_timeline_entity_registry(hass, entry)
    _async_prepare_baseline_entity_registry(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # The entry owns the broker lifecycle.  Start only after every platform
    # completed setup so an identity collision or setup exception cannot leak
    # broker listeners.  Registered consumers receive the initial refresh.
    from .ems_initial_defaults import (  # noqa: PLC0415
        EMSInitialDefaultSeeder,
        blocked_shared_migration_targets,
        trusted_shared_migration_source_values,
    )
    from .ems_shared_input_migration import (  # noqa: PLC0415
        EMSSharedInputMigrationCoordinator,
        MIGRATION_VERSION,
    )

    # If the defaults ledger itself cannot be decoded, fail closed only for
    # the shared fallback whose legacy helper may still expose HA's technical
    # minimum instead of the intended first-install 20 kWh seed.
    blocked_migration_targets = blocked_shared_migration_targets(None)
    trusted_seeded_source_values = {}
    try:
        seed_ledger = await EMSInitialDefaultSeeder.for_home_assistant(
            hass
        ).async_run_once(now=datetime.now(timezone.utc))
    except Exception:  # noqa: BLE001 - defaults must fail closed, not block setup
        _LOGGER.exception("First-install EMS helper defaults were not applied")
    else:
        blocked_migration_targets = blocked_shared_migration_targets(seed_ledger)
        trusted_seeded_source_values = trusted_shared_migration_source_values(
            seed_ledger
        )
    try:
        migration = EMSSharedInputMigrationCoordinator.for_home_assistant(hass)
        migration_ledger = await migration.async_run_copy_once(
            now=datetime.now(timezone.utc),
            blocked_target_entity_ids=blocked_migration_targets,
            trusted_seeded_source_values=trusted_seeded_source_values,
        )
    except Exception:  # noqa: BLE001 - diagnostics must not break entry setup
        _LOGGER.exception("Shared EMS copy-once migration pass failed")
        shared_inputs.set_migration_status(
            {
                "version": MIGRATION_VERSION,
                "result": "runtime_error",
                "fields": {},
                "seeded_values": {},
            }
        )
    else:
        shared_inputs.set_migration_status(migration_ledger.as_public_dict())
    await shared_inputs.async_start()
    # Entity-registry entries for newly added catalog records do not exist
    # until their platforms finish setup. Reconcile after forwarding so fresh
    # entities immediately receive the same stable IDs as existing entities.
    _async_reconcile_entity_registry(hass, entry, hass.data[DOMAIN][entry.entry_id])

    if hass.is_running:
        _async_update_ems_package_issue(hass, entry)
    else:
        async def async_update_ems_package_issue_after_start(_event) -> None:
            """Update repairs on the Home Assistant event loop."""
            _async_update_ems_package_issue(hass, entry)

        hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED,
            async_update_ems_package_issue_after_start,
        )
    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> bool:
    """Unload the integration."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        runtime: RuntimeData | None = hass.data[DOMAIN].get(entry.entry_id)
        profits = getattr(runtime, "profits", None)
        if profits is not None:
            await profits.close()
        rce_prices = getattr(runtime, "rce_prices", None)
        if rce_prices is not None:
            await rce_prices.async_stop()
        shared_inputs = getattr(runtime, "shared_inputs", None)
        if shared_inputs is not None:
            await shared_inputs.async_stop()
        hass.data[DOMAIN].pop(entry.entry_id, None)
        notify_supervisor_guard(hass)
        ir.async_delete_issue(hass, DOMAIN, _ems_package_issue_id(entry))
        ir.async_delete_issue(
            hass,
            DOMAIN,
            _ems_package_restart_issue_id(entry),
        )
    return unloaded
