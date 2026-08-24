"""Structural release validation without requiring a Home Assistant checkout."""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import json
import py_compile
import re
import struct
import sys
import tempfile
import types
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPONENT_ROOT = ROOT / "custom_components"
COMPONENT = COMPONENT_ROOT / "hoymiles_hit_modbus"
RESOURCES = COMPONENT / "resources"
EXPECTED_PROJECT_NAME = "EMS for Hoymiles HIT-(5–20)L-G3"
EXPECTED_REPOSITORY = "Kaluzaburza/hoymiles-hit-g3-ems"
EXPECTED_DESCRIPTION = (
    "Unofficial local EMS for Hoymiles HIT-G3 hybrid inverters — "
    "Home Assistant, ESPHome, Modbus, RCE, tariff optimization and RCEm."
)
LEGACY_REPOSITORY_SLUG = "Hoymiles_HIT_xxL_G3_ModBus"


def require(condition: bool, message: str) -> None:
    """Raise a readable release validation error."""
    if not condition:
        raise RuntimeError(message)


def load_json(path: Path) -> dict | list:
    """Load and validate UTF-8 JSON."""
    with path.open(encoding="utf-8") as json_file:
        return json.load(json_file)


def iter_mappings(value):
    """Yield every mapping in a nested dashboard payload."""
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from iter_mappings(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_mappings(child)


def dashboard_structure(value, parent_key: str = ""):
    """Return a locale-neutral dashboard structure signature."""
    if isinstance(value, dict):
        return {
            key: dashboard_structure(child, key)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [dashboard_structure(child, parent_key) for child in value]
    if isinstance(value, str) and (
        parent_key in {"type", "entity", "action", "service"}
        or parent_key.endswith("_entity")
        or re.fullmatch(r"[a-z_]+\.[a-z0-9_]+", value)
    ):
        return value
    return type(value).__name__


def load_localization_module():
    """Load the standalone localization module without Home Assistant."""
    path = COMPONENT / "localization.py"
    spec = importlib.util.spec_from_file_location("hoymiles_localization", path)
    require(spec is not None and spec.loader is not None, "Cannot load localization")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_assets_module():
    """Load the asset installer with a minimal Home Assistant type stub."""
    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    lovelace = types.ModuleType("homeassistant.components.lovelace")
    lovelace_const = types.ModuleType("homeassistant.components.lovelace.const")
    ha_const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    helpers = types.ModuleType("homeassistant.helpers")
    storage = types.ModuleType("homeassistant.helpers.storage")
    core.HomeAssistant = object
    lovelace_const.CONF_RESOURCE_TYPE_WS = "res_type"
    lovelace_const.LOVELACE_DATA = "lovelace"
    lovelace_const.MODE_STORAGE = "storage"
    ha_const.CONF_ID = "id"
    ha_const.CONF_TYPE = "type"
    ha_const.CONF_URL = "url"
    storage.Store = object
    homeassistant.components = components
    components.lovelace = lovelace
    lovelace.const = lovelace_const
    homeassistant.const = ha_const
    homeassistant.core = core
    homeassistant.helpers = helpers
    helpers.storage = storage
    sys.modules.setdefault("homeassistant", homeassistant)
    sys.modules.setdefault("homeassistant.components", components)
    sys.modules.setdefault("homeassistant.components.lovelace", lovelace)
    sys.modules.setdefault("homeassistant.components.lovelace.const", lovelace_const)
    sys.modules.setdefault("homeassistant.const", ha_const)
    sys.modules.setdefault("homeassistant.core", core)
    sys.modules.setdefault("homeassistant.helpers", helpers)
    sys.modules.setdefault("homeassistant.helpers.storage", storage)

    custom_components = types.ModuleType("custom_components")
    package = types.ModuleType("custom_components.hoymiles_hit_modbus")
    package.__path__ = [str(COMPONENT)]
    sys.modules.setdefault("custom_components", custom_components)
    sys.modules.setdefault("custom_components.hoymiles_hit_modbus", package)

    const_module = types.ModuleType("custom_components.hoymiles_hit_modbus.const")
    const_module.DOMAIN = "hoymiles_hit_modbus"
    const_module.VERSION = json.loads(
        (COMPONENT / "manifest.json").read_text(encoding="utf-8")
    )["version"]
    sys.modules[const_module.__name__] = const_module

    path = COMPONENT / "assets.py"
    spec = importlib.util.spec_from_file_location(
        "custom_components.hoymiles_hit_modbus.assets",
        path,
    )
    require(spec is not None and spec.loader is not None, "Cannot load assets")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def validate_fresh_asset_install() -> None:
    """Exercise fresh installation and the legacy-dashboard migration path."""
    assets = load_assets_module()
    with tempfile.TemporaryDirectory(prefix="hoymiles_hacs_install_") as tmp:
        config_path = Path(tmp)
        dashboard_path = config_path / "dashboard_hoymiles.yaml"
        package_path = (
            config_path / "packages" / "hoymiles_ems_scheduler.yaml"
        )
        frontend_local_ready = (config_path / "www").is_dir()
        polish_written = assets._copy_assets(config_path, "pl-PL", False)
        require(
            len(polish_written) == 7,
            "Fresh Polish installation did not copy all seven assets",
        )
        require(
            (config_path / "www").is_dir()
            and not frontend_local_ready,
            "Fresh-no-www setup must remain restart-gated after copying assets",
        )
        require(
            dashboard_path.read_text(encoding="utf-8")
            == (RESOURCES / "dashboard_hoymiles_pl.yaml").read_text(
                encoding="utf-8"
            ),
            "Fresh Polish installation copied the wrong dashboard",
        )
        for filename in assets.LOCAL_FRONTEND_ASSETS:
            require(
                (config_path / "www" / filename).read_bytes()
                == (RESOURCES / "www" / filename).read_bytes(),
                f"Fresh installation copied the wrong /local asset: {filename}",
            )
        require(
            assets._copy_assets(config_path, "pl-PL", False) == [],
            "Asset installer overwrites user files without explicit permission",
        )

        legacy_dashboard = """\
title: Custom user dashboard
entities:
  - sensor.hoymiles_inverter_pv1_voltage
  - sensor.pv_hoymiles_inverter_pv1_current
  - sensor.unrelated_user_entity
"""
        dashboard_path.write_text(legacy_dashboard, encoding="utf-8")
        legacy_package = """\
script:
  custom_user_script:
    sequence:
      - action: select.select_option
        target:
          entity_id: select.pv_hoymiles_inverter_tryb_ems
"""
        package_path.write_text(legacy_package, encoding="utf-8")
        migrated = assets._copy_assets(config_path, "pl-PL", False)
        require(
            migrated == [dashboard_path, package_path],
            "Existing legacy assets were not migrated in place",
        )
        migrated_text = dashboard_path.read_text(encoding="utf-8")
        require(
            "sensor.hoymiles_hit_pv1_voltage" in migrated_text
            and "sensor.hoymiles_hit_pv1_current" in migrated_text,
            "Legacy dashboard ids were not replaced with stable proxy ids",
        )
        require(
            "title: Custom user dashboard" in migrated_text
            and "sensor.unrelated_user_entity" in migrated_text,
            "Legacy migration did not preserve user dashboard content",
        )
        backup_path = dashboard_path.with_name(
            f"{dashboard_path.name}{assets.LEGACY_ENTITY_BACKUP_SUFFIX}"
        )
        require(
            backup_path.read_text(encoding="utf-8") == legacy_dashboard,
            "Legacy dashboard migration did not create an exact backup",
        )
        migrated_package = package_path.read_text(encoding="utf-8")
        require(
            "select.hoymiles_hit_ems_mode" in migrated_package
            and "custom_user_script" in migrated_package,
            "Legacy EMS package was not safely migrated",
        )
        package_backup = package_path.with_name(
            f"{package_path.name}{assets.LEGACY_ENTITY_BACKUP_SUFFIX}"
        )
        require(
            package_backup.read_text(encoding="utf-8") == legacy_package,
            "Legacy EMS migration did not create an exact backup",
        )
        require(
            assets._copy_assets(config_path, "pl-PL", False) == [],
            "Stable dashboard migration is not idempotent",
        )

        english_written = assets._copy_assets(config_path, "en-GB", True)
        require(
            len(english_written) == 7,
            "English overwrite installation did not copy all seven assets",
        )
        require(
            (config_path / "dashboard_hoymiles.yaml").read_text(
                encoding="utf-8"
            )
            == (RESOURCES / "dashboard_hoymiles_en.yaml").read_text(
                encoding="utf-8"
            ),
            "English installation copied the wrong dashboard",
        )

    with tempfile.TemporaryDirectory(prefix="hoymiles_managed_upgrade_") as tmp:
        config_path = Path(tmp)
        dashboard_path = config_path / "dashboard_hoymiles.yaml"
        dashboard_path.write_text("title: previous managed release\n", encoding="utf-8")
        previous_hash = assets._sha256(dashboard_path)
        written, managed = assets._sync_assets(
            config_path,
            "pl-PL",
            False,
            {"dashboard_hoymiles.yaml": previous_hash},
        )
        require(
            dashboard_path in written
            and managed["dashboard_hoymiles.yaml"]
            == assets._sha256(dashboard_path),
            "An unchanged managed dashboard was not upgraded",
        )

        dashboard_path.write_text("title: user customization\n", encoding="utf-8")
        custom_content = dashboard_path.read_text(encoding="utf-8")
        written, managed = assets._sync_assets(
            config_path,
            "pl-PL",
            False,
            {"dashboard_hoymiles.yaml": previous_hash},
        )
        require(
            dashboard_path not in written
            and dashboard_path.read_text(encoding="utf-8") == custom_content
            and "dashboard_hoymiles.yaml" not in managed,
            "A user-modified dashboard was overwritten",
        )

    class FakeResourceCollection:
        """Exercise the same live collection contract as Lovelace websocket."""

        def __init__(self, items: list[dict]) -> None:
            self.data = {item["id"]: dict(item) for item in items}
            self.loaded = False

        async def async_get_info(self) -> dict[str, int]:
            self.loaded = True
            return {"resources": len(self.data)}

        def async_items(self) -> list[dict]:
            require(self.loaded, "Resource items were read before lazy loading")
            return list(self.data.values())

        async def async_update_item(self, item_id: str, updates: dict) -> dict:
            require(self.loaded, "Resource was updated before lazy loading")
            normalized = dict(updates)
            if "res_type" in normalized:
                normalized["type"] = normalized.pop("res_type")
            self.data[item_id].update(normalized)
            return self.data[item_id]

        async def async_create_item(self, data: dict) -> dict:
            require(self.loaded, "Resource was created before lazy loading")
            item = dict(data)
            if "res_type" in item:
                item["type"] = item.pop("res_type")
            item["id"] = "generated-hoymiles-resource"
            self.data[item["id"]] = item
            return item

        async def async_delete_item(self, item_id: str) -> None:
            require(self.loaded, "Resource was deleted before lazy loading")
            del self.data[item_id]

    def fake_hass(
        collection: FakeResourceCollection,
        mode: str = "storage",
    ) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            data={
                "lovelace": types.SimpleNamespace(
                    resource_mode=mode,
                    resources=collection,
                )
            }
        )

    fresh_collection = FakeResourceCollection([])
    fresh_hass = fake_hass(fresh_collection)
    require(
        asyncio.run(assets._async_sync_lovelace_resource(fresh_hass)),
        "Fresh storage setup did not create the live Lovelace resource",
    )
    require(
        fresh_collection.async_items()
        == [
            {
                "id": "generated-hoymiles-resource",
                "url": assets.FRONTEND_RESOURCE_URL,
                "type": "module",
            }
        ],
        "Fresh setup created an invalid live Lovelace resource",
    )
    require(
        not asyncio.run(assets._async_sync_lovelace_resource(fresh_hass)),
        "Live Lovelace resource setup is not idempotent",
    )

    bootstrap_only = FakeResourceCollection(
        [
            {
                "id": "legacy-bootstrap",
                "url": (
                    "/local/hoymiles-dashboard-strategy.js"
                    "?v=1.5.2.15"
                ),
                "type": "module",
            }
        ]
    )
    require(
        asyncio.run(
            assets._async_sync_lovelace_resource(fake_hass(bootstrap_only))
        )
        and bootstrap_only.async_items()
        == [
            {
                "id": "legacy-bootstrap",
                "url": assets.FRONTEND_RESOURCE_URL,
                "type": "module",
            }
        ],
        "A lone legacy bootstrap was not upgraded to the full module",
    )

    yaml_collection = FakeResourceCollection(
        [
            {
                "id": "yaml-bootstrap",
                "url": "/local/hoymiles-dashboard-strategy.js?v=old",
                "type": "module",
            }
        ]
    )
    require(
        not asyncio.run(
            assets._async_sync_lovelace_resource(
                fake_hass(yaml_collection, mode="yaml")
            )
        )
        and not yaml_collection.loaded
        and yaml_collection.data["yaml-bootstrap"]["url"]
        == "/local/hoymiles-dashboard-strategy.js?v=old",
        "YAML resource mode was mutated instead of using the global module",
    )

    with tempfile.TemporaryDirectory(prefix="hoymiles_storage_upgrade_") as tmp:
        config_path = Path(tmp)
        storage_path = config_path / ".storage"
        storage_path.mkdir()
        resources_path = storage_path / "lovelace_resources"
        resources_payload = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace_resources",
            "data": {
                "items": [
                    {
                        "id": "legacy-hoymiles-bootstrap",
                        "url": (
                            "/api/hoymiles_hit_modbus/static-r2/"
                            "hoymiles-dashboard-strategy.js?v=1.5.2.15"
                        ),
                        "type": "module",
                    },
                    {
                        "id": "canonical-hoymiles-resource",
                        "url": assets.FRONTEND_RESOURCE_URL,
                        "type": "module",
                    },
                    {
                        "id": "duplicate-hoymiles-resource",
                        "url": (
                            "/api/hoymiles_hit_modbus/static-r2/"
                            "hoymiles-rce-chart-card.js?v=1.5.2.15"
                        ),
                        "type": "module",
                    },
                    {
                        "id": "unrelated-resource",
                        "url": "/local/user-card.js",
                        "type": "module",
                    },
                ]
            },
        }
        resources_path.write_text(
            json.dumps(resources_payload), encoding="utf-8"
        )

        dashboard_path = storage_path / "lovelace.hoymiles_test"
        dashboard_payload = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace.hoymiles_test",
            "data": {
                "config": {
                    "title": "Custom user layout",
                    "views": [
                        {
                            "cards": [
                                {
                                    "type": "entities",
                                    "title": "User card",
                                    "entities": [
                                        "sensor.hoymiles_hit_overview_pv_total_power",
                                        "sensor.hoymiles_hit_overview_battery_power",
                                        "sensor.unrelated_user_entity",
                                        {
                                            "entity": "sensor.hoymiles_rce_pv_self_consumption_today",
                                            "name": "PV → odbiorniki dzisiaj",
                                        },
                                        {
                                            "entity": "sensor.hoymiles_rce_battery_to_load_today",
                                            "name": "Bateria → odbiorniki dzisiaj",
                                        },
                                        {
                                            "entity": "sensor.hoymiles_rce_grid_to_load_today",
                                            "name": "Sieć → odbiorniki dzisiaj",
                                        },
                                    ],
                                },
                                {
                                    "type": "markdown",
                                    "content": "User content",
                                },
                                {
                                    "type": "custom:hoymiles-power-flow-card",
                                    "inverter_image": (
                                        "/api/hoymiles_hit_modbus/static/"
                                        "hoymiles-inverter.png"
                                    ),
                                },
                            ]
                        }
                    ],
                }
            },
        }
        dashboard_path.write_text(
            json.dumps(dashboard_payload), encoding="utf-8"
        )

        unrelated_path = storage_path / "lovelace.unrelated"
        unrelated_payload = {
            "version": 1,
            "minor_version": 1,
            "key": "lovelace.unrelated",
            "data": {
                "config": {
                    "views": [
                        {"cards": [{"type": "entities", "entities": []}]}
                    ]
                }
            },
        }
        unrelated_text = json.dumps(unrelated_payload)
        unrelated_path.write_text(unrelated_text, encoding="utf-8")

        migrated = assets._sync_lovelace_storage(config_path)
        require(
            migrated == [dashboard_path],
            "Storage-mode dashboard migration changed unexpected files",
        )
        migrated_dashboard = json.loads(
            dashboard_path.read_text(encoding="utf-8")
        )
        cards = migrated_dashboard["data"]["config"]["views"][0]["cards"]
        require(
            cards[0]["type"] == assets.ZEBRA_CARD_TYPE,
            "Storage-mode entities cards were not upgraded to zebra cards",
        )
        require(
            cards[0]["title"] == "User card"
            and "sensor.unrelated_user_entity" in cards[0]["entities"]
            and cards[1]["content"] == "User content"
            and cards[2]["inverter_image"] == assets.INVERTER_IMAGE_PATH,
            "Storage-mode migration did not preserve user customizations",
        )
        migrated_rows = [
            row
            for row in cards[0]["entities"]
            if isinstance(row, dict)
        ]
        require(
            migrated_rows[0]
            == {
                "entity": "sensor.hoymiles_actual_load_energy_today",
                "name": "Rzeczywiste zużycie odbiorników dzisiaj",
            }
            and migrated_rows[1]["name"]
            == "PV → odbiorniki — rejestr diagnostyczny"
            and migrated_rows[2]["name"]
            == "Energia oddana przez baterię — diagnostycznie"
            and migrated_rows[3]["name"]
            == "Energia pobrana z sieci — diagnostycznie",
            "Storage-mode RCE LOAD rows were not migrated safely",
        )
        require(
            json.loads(resources_path.read_text(encoding="utf-8"))
            == resources_payload,
            "Dashboard migration edited Lovelace resources behind HA's live collection",
        )
        collection = FakeResourceCollection(resources_payload["data"]["items"])
        require(
            asyncio.run(assets._async_sync_lovelace_resource(fake_hass(collection))),
            "Legacy integration-static resource was not migrated live",
        )
        migrated_resources = collection.async_items()
        require(
            migrated_resources[0]["id"] == "canonical-hoymiles-resource"
            and migrated_resources[0]["url"] == assets.FRONTEND_RESOURCE_URL
            and migrated_resources[0]["type"] == "module"
            and migrated_resources[1]["url"] == "/local/user-card.js",
            "Managed Lovelace resources were not reduced to one full module",
        )
        require(
            not asyncio.run(
                assets._async_sync_lovelace_resource(fake_hass(collection))
            ),
            "Migrated live Lovelace resource is not idempotent",
        )
        require(
            dashboard_path.with_name(
                f"{dashboard_path.name}.pre-{assets.VERSION}.bak"
            ).is_file(),
            "Dashboard storage migration did not create a rollback backup",
        )
        require(
            unrelated_path.read_text(encoding="utf-8") == unrelated_text,
            "Storage migration touched an unrelated dashboard",
        )
        require(
            assets._sync_lovelace_storage(config_path) == [],
            "Storage-mode migration is not idempotent",
        )

    require(
        assets.FRONTEND_RESOURCE_URL
        == f"/local/hoymiles-rce-chart-card.js?v={assets.VERSION}.24"
        and assets.FRONTEND_BOOTSTRAP_URL
        == f"/local/hoymiles-dashboard-strategy.js?v={assets.VERSION}.24"
        and "/local/hoymiles-dashboard-strategy.js"
        in assets.MANAGED_FRONTEND_RESOURCE_PATHS,
        "Frontend revision 24 or bootstrap migration paths changed",
    )


def validate_frontend_asset_failure_isolation(init_source: str) -> None:
    """Prove optional asset failures cannot disable integration-wide setup."""
    source_tree = ast.parse(init_source)
    selected = [
        node
        for node in source_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"_async_prepare_frontend_assets", "async_setup"}
    ]
    require(
        {node.name for node in selected}
        == {"_async_prepare_frontend_assets", "async_setup"},
        "Frontend failure-isolation functions are missing",
    )
    executable = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            *selected,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(executable)

    class FakeIssueRegistry:
        def __init__(self) -> None:
            self.created: list[tuple] = []
            self.deleted: list[tuple] = []

        def async_create_issue(self, *args, **kwargs) -> None:
            self.created.append((args, kwargs))

        def async_delete_issue(self, *args, **kwargs) -> None:
            self.deleted.append((args, kwargs))

        class IssueSeverity:
            WARNING = "warning"

    class FakeLogger:
        def exception(self, *args, **kwargs) -> None:
            return None

        def info(self, *args, **kwargs) -> None:
            return None

    class FakeHttp:
        def __init__(self) -> None:
            self.static_paths: list = []
            self.views: list = []

        async def async_register_static_paths(self, paths) -> None:
            self.static_paths.extend(paths)

        def register_view(self, view) -> None:
            self.views.append(view)

    class FakeServices:
        def __init__(self) -> None:
            self.registrations: list[tuple] = []

        def async_register(self, *args, **kwargs) -> None:
            self.registrations.append((args, kwargs))

    class FakeHass:
        def __init__(self, config_dir: Path) -> None:
            self.config = types.SimpleNamespace(
                config_dir=str(config_dir),
                language="en",
            )
            self.data: dict = {}
            self.http = FakeHttp()
            self.services = FakeServices()

        async def async_add_executor_job(self, target, *args):
            return target(*args)

    issues = FakeIssueRegistry()
    module_globals = {
        "Path": Path,
        "HomeAssistant": object,
        "ServiceCall": object,
        "_LOGGER": FakeLogger(),
        "ir": issues,
        "DOMAIN": "hoymiles_hit_modbus",
        "EMS_PACKAGE_DOCS_URL": "https://example.invalid/docs",
        "FRONTEND_ASSETS_RESTART_ISSUE_ID": "frontend_restart",
        "FRONTEND_ASSETS_INSTALL_FAILED_ISSUE_ID": "frontend_failed",
        "STATIC_URL": "/api/hoymiles/static-r2",
        "RESOURCE_ROOT": Path("resources"),
        "FRONTEND_MODULE_URL": "/local/card.js?v=test",
        "ATTR_OVERWRITE": "overwrite",
        "SERVICE_INSTALL_ASSETS": "install_assets",
        "INSTALL_ASSETS_SCHEMA": object(),
        "StaticPathConfig": lambda *args, **kwargs: (args, kwargs),
        "HoymilesSupportBundleView": lambda: object(),
    }
    exec(compile(executable, "<frontend-startup-contract>", "exec"), module_globals)

    async def exercise_failure(error: Exception) -> None:
        async def failing_install(*args, **kwargs):
            raise error

        module_globals["async_install_assets"] = failing_install
        with tempfile.TemporaryDirectory(prefix="hoymiles_asset_failure_") as tmp:
            hass = FakeHass(Path(tmp))
            result = await module_globals["_async_prepare_frontend_assets"](hass)
        require(
            result == ([], False, False),
            f"Optional asset error was not isolated: {type(error).__name__}",
        )
        require(
            any(
                args[2] == "frontend_failed"
                for args, _kwargs in issues.created
            ),
            "Optional asset failure did not create a Repair issue",
        )

    asyncio.run(exercise_failure(OSError("disk full")))
    asyncio.run(exercise_failure(RuntimeError("live resource failed")))

    async def setup_failure_tuple(_hass):
        return [], False, False

    extra_urls: list[str] = []
    module_globals["_async_prepare_frontend_assets"] = setup_failure_tuple
    module_globals["add_extra_js_url"] = (
        lambda _hass, url: extra_urls.append(url)
    )
    module_globals["async_install_assets"] = lambda *args, **kwargs: None
    with tempfile.TemporaryDirectory(prefix="hoymiles_setup_failure_") as tmp:
        failed_hass = FakeHass(Path(tmp))
        require(
            asyncio.run(module_globals["async_setup"](failed_hass, {})) is True,
            "Asset failure disabled integration-wide setup",
        )
    require(
        len(failed_hass.http.static_paths) == 1
        and len(failed_hass.http.views) == 1
        and len(failed_hass.services.registrations) == 1
        and not extra_urls,
        "Asset failure did not preserve setup or published an unsafe module",
    )

    async def setup_success_tuple(_hass):
        return [Path("asset")], True, True

    module_globals["_async_prepare_frontend_assets"] = setup_success_tuple
    with tempfile.TemporaryDirectory(prefix="hoymiles_setup_success_") as tmp:
        success_hass = FakeHass(Path(tmp))
        require(
            asyncio.run(module_globals["async_setup"](success_hass, {})) is True,
            "Successful frontend setup did not complete",
        )
    require(
        extra_urls == ["/local/card.js?v=test"],
        "Successful frontend setup did not publish exactly one canonical module",
    )


def png_dimensions(path: Path) -> tuple[int, int]:
    """Return PNG dimensions using only the Python standard library."""
    header = path.read_bytes()[:24]
    require(
        len(header) == 24
        and header[:8] == b"\x89PNG\r\n\x1a\n"
        and header[12:16] == b"IHDR",
        f"{path.name} is not a valid PNG",
    )
    return struct.unpack(">II", header[16:24])


def entity_translation_keys(translations: dict) -> dict[str, set[str]]:
    """Return translation keys grouped by entity domain."""
    return {
        domain: set(entries)
        for domain, entries in translations.get("entity", {}).items()
    }


def main() -> int:
    """Validate HACS layout, translations, Python and bundled assets."""
    integration_dirs = [
        path for path in COMPONENT_ROOT.iterdir() if path.is_dir()
    ]
    require(
        integration_dirs == [COMPONENT],
        "HACS repositories may contain only one custom integration",
    )

    hacs = load_json(ROOT / "hacs.json")
    require(
        hacs.get("name") == EXPECTED_PROJECT_NAME,
        "hacs.json has the wrong public project name",
    )

    manifest = load_json(COMPONENT / "manifest.json")
    required_manifest = {
        "domain",
        "documentation",
        "issue_tracker",
        "codeowners",
        "name",
        "version",
    }
    require(
        required_manifest <= set(manifest),
        f"manifest.json is missing: {sorted(required_manifest - set(manifest))}",
    )
    require(manifest["domain"] == "hoymiles_hit_modbus", "Unexpected domain")
    require(
        manifest["name"] == EXPECTED_PROJECT_NAME
        and manifest["documentation"]
        == f"https://github.com/{EXPECTED_REPOSITORY}"
        and manifest["issue_tracker"]
        == f"https://github.com/{EXPECTED_REPOSITORY}/issues",
        "Manifest project metadata does not match the public rename contract",
    )
    require(
        re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"]) is not None,
        "Release version must use semantic MAJOR.MINOR.PATCH format",
    )

    entity_source = (COMPONENT / "entity.py").read_text(encoding="utf-8")
    init_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    assets_source = (COMPONENT / "assets.py").read_text(encoding="utf-8")
    config_flow_source = (COMPONENT / "config_flow.py").read_text(encoding="utf-8")
    sensor_platform_source = (COMPONENT / "sensor.py").read_text(encoding="utf-8")
    const_source = (COMPONENT / "const.py").read_text(encoding="utf-8")
    require(
        f'VERSION = "{manifest["version"]}"' in const_source,
        "const.py VERSION does not match manifest.json",
    )
    require(
        f'NAME = "{EXPECTED_PROJECT_NAME}"' in const_source,
        "const.py NAME does not match the public project name",
    )
    require(
        "async_track_state_report_event" in entity_source
        and "EventStateReportedData" in entity_source
        and "_async_source_state_reported" in entity_source,
        "Proxy entities must forward unchanged source reports for signed freshness",
    )
    firmware_core_source = (ROOT / "packages" / "core.yaml").read_text(
        encoding="utf-8"
    )
    firmware_version_match = re.search(
        r'^\s*version: "(\d+\.\d+\.\d+)"$',
        firmware_core_source,
        re.MULTILINE,
    )
    require(
        firmware_version_match is not None,
        "ESPHome project version is missing from packages/core.yaml",
    )
    compatible_firmware_version = firmware_version_match.group(1)
    require(
        'name: "hoymiles.energy-storage-modbus"' in firmware_core_source,
        "Stable ESPHome project.name must not change during a marketing rename",
    )
    ems_package_version_match = re.search(
        r'^EMS_PACKAGE_VERSION = "([^"]+)"$', const_source, re.MULTILINE
    )
    require(
        ems_package_version_match is not None,
        "const.py is missing EMS_PACKAGE_VERSION",
    )
    expected_ems_package_version = ems_package_version_match.group(1)
    ems_package_source = (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
    ).read_text(encoding="utf-8")
    require(
        'EMS_PACKAGE_SENTINEL = "input_boolean.hoymiles_rce_discharge_enabled"'
        in const_source
        and 'EMS_PACKAGE_VERSION_ENTITY = "sensor.hoymiles_ems_package_version"'
        in const_source
        and "EMS_PACKAGE_VERSION_ENTITY," in sensor_platform_source
        and "EMS_PACKAGE_SENTINEL," in init_source
        and "hoymiles_rce_discharge_enabled:" in ems_package_source
        and "hoymiles_rce_automation_enabled" not in init_source
        and "hoymiles_rce_automation_enabled" not in sensor_platform_source,
        "Setup status and Repairs must use an existing shared EMS package sentinel",
    )
    require(
        "ems_package_restart_required" in init_source
        and "_ems_package_restart_issue_id" in init_source
        and "package_version.state == EMS_PACKAGE_VERSION" in init_source
        and "EMS_PACKAGE_VERSION," in sensor_platform_source
        and '"expected_ems_package_version": EMS_PACKAGE_VERSION'
        in sensor_platform_source
        and '"restart_required": self._ems_restart_required'
        in sensor_platform_source,
        "Managed EMS package updates do not expose the required restart state",
    )
    require(
        "def suggested_object_id(self)" in entity_source,
        "Proxy entities need an explicit stable suggested_object_id property",
    )
    require(
        "_async_reconcile_entity_registry(" in init_source
        and "entity_registry.async_remove" in init_source,
        "Localized entity ids and stale catalog proxies are not reconciled",
    )
    require(
        init_source.index("async_forward_entry_setups")
        < init_source.index("_async_reconcile_entity_registry", init_source.index("async_forward_entry_setups")),
        "Entity registry must be reconciled after platform setup",
    )
    require(
        "firmware_update_required" in entity_source
        and "matched.source is not None" in entity_source,
        "Missing-firmware proxy entities are not represented safely",
    )
    require(
        "StaticPathConfig" in init_source
        and 'STATIC_URL = f"/api/{DOMAIN}/{FRONTEND_STATIC_ROUTE}"'
        in init_source
        and 'FRONTEND_STATIC_ROUTE = "static-r2"' in assets_source,
        "Integration assets are not exposed through the stable no-cache URL",
    )
    require(
        "add_extra_js_url" in init_source
        and "FRONTEND_MODULE_URL" in init_source
        and "FRONTEND_RESOURCE_URL" in init_source
        and "?v={VERSION}" in assets_source,
        "Dashboard strategy module is not registered with versioned cache busting",
    )
    setup_body = init_source.split(
        "async def async_setup(hass: HomeAssistant, config: dict) -> bool:", 1
    )[1].split("async def async_setup_entry", 1)[0]
    require(
        setup_body.index("await _async_prepare_frontend_assets")
        < setup_body.index("add_extra_js_url"),
        "Local frontend assets must be prepared before their URL is published",
    )
    require(
        "Availability is restart-gated" in setup_body
        and "frontend_assets_ready and frontend_local_ready" in setup_body
        and setup_body.count("add_extra_js_url(hass,") == 1
        and "FRONTEND_MODULE_URL = FRONTEND_BOOTSTRAP_URL" in init_source
        and "FRONTEND_BOOTSTRAP_URL" in init_source,
        "Restart-gated /local startup or early strategy loader is missing",
    )
    require(
        "frontend" in manifest.get("dependencies", []),
        "Frontend dependency is required for automatic strategy registration",
    )
    require(
        "lovelace" in manifest.get("dependencies", [])
        and "_async_sync_lovelace_resource" in assets_source
        and "async_update_item" in assets_source
        and "async_create_item" in assets_source,
        "Managed resource migration must use Lovelace's live storage collection",
    )
    require(
        "_sync_lovelace_storage" not in init_source
        and "_sync_lovelace_storage" not in assets_source.split(
            "async def async_install_assets", 1
        )[1],
        "Runtime asset install must not mutate .storage/lovelace.* behind HA",
    )
    require(
        "_async_default_source_device_id" in config_flow_source
        and "CONF_COPY_ASSETS: True" in config_flow_source
        and "BooleanSelector" not in config_flow_source,
        "Config flow still exposes avoidable asset-copy choices",
    )
    require(
        "ems_package_not_loaded" in init_source
        and "issue_registry" in init_source
        and "HoymilesSetupStatusSensor" in sensor_platform_source
        and "firmware_coverage_percent" in sensor_platform_source,
        "Beginner setup status or EMS package Repair is missing",
    )

    catalog = load_json(COMPONENT / "entity_catalog.json")
    require(isinstance(catalog, list), "Entity catalog must be a list")
    require(len(catalog) >= 250, "The generated catalog is unexpectedly small")
    identities = {
        (entry["domain"], entry["translation_key"]) for entry in catalog
    }
    require(
        len(identities) == len(catalog),
        "Duplicate domain/translation_key in entity catalog",
    )
    require(
        ("button", "clear_fault") in identities,
        "Generated catalog is missing the Clear Fault button",
    )
    system_package = (ROOT / "packages" / "system.yaml").read_text(
        encoding="utf-8"
    )
    require(
        "create_write_single_command" in system_package
        and "controller, 3004, 1" in system_package,
        "Clear Fault must write value 1 to holding register 3004",
    )
    meter_package = (ROOT / "packages" / "meters.yaml").read_text(
        encoding="utf-8"
    )
    for power_name in (
        "Meter Grid Active Power L1",
        "Meter Grid Active Power L2",
        "Meter Grid Active Power L3",
        "Meter Grid Total Active Power",
    ):
        require(
            re.search(
                rf"modbus_controller_id: \$\{{modbus_fast_controller_id\}}\s+"
                rf'name: "{re.escape(power_name)}"',
                meter_package,
            )
            is not None,
            f"{power_name} must use the fast Modbus polling controller",
        )

    english = load_json(COMPONENT / "translations" / "en.json")
    polish = load_json(COMPONENT / "translations" / "pl.json")
    require(
        "ems_package_restart_required" in english.get("issues", {})
        and "ems_package_restart_required" in polish.get("issues", {}),
        "EMS package restart Repair is not translated in both languages",
    )
    require(
        entity_translation_keys(english) == entity_translation_keys(polish),
        "English and Polish entity translation keys differ",
    )
    for entry in catalog:
        domain = entry["domain"]
        key = entry["translation_key"]
        require(key in english["entity"][domain], f"Missing English key {domain}.{key}")
        require(key in polish["entity"][domain], f"Missing Polish key {domain}.{key}")

    required_assets = [
        RESOURCES / "dashboard_hoymiles_en.yaml",
        RESOURCES / "dashboard_hoymiles_pl.yaml",
        RESOURCES / "home_assistant" / "en" / "hoymiles_ems_scheduler.yaml",
        RESOURCES / "home_assistant" / "pl" / "hoymiles_ems_scheduler.yaml",
        RESOURCES / "www" / "hoymiles-rce-chart-card.js",
        RESOURCES / "www" / "hoymiles-dashboard-strategy.js",
        RESOURCES / "www" / "hoymiles-inverter.png",
        RESOURCES / "www" / "dashboard_hoymiles_en.json",
        RESOURCES / "www" / "dashboard_hoymiles_pl.json",
    ]
    for asset in required_assets:
        require(asset.is_file(), f"Missing bundled asset: {asset.relative_to(ROOT)}")

    for package_path in (
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml",
        required_assets[2],
        required_assets[3],
    ):
        package_text = package_path.read_text(encoding="utf-8")
        require(
            "unique_id: hoymiles_ems_package_version" in package_text
            and f'state: "{expected_ems_package_version}"' in package_text,
            "EMS package version marker does not match "
            f"{expected_ems_package_version} "
            f"in {package_path.relative_to(ROOT)}",
        )

    english_package = required_assets[2].read_text(encoding="utf-8")
    polish_package = required_assets[3].read_text(encoding="utf-8")
    require(
        "No active automation" in english_package
        and "Balancing" in english_package
        and "Manual control" in english_package
        and "Enabled — waiting for a selected slot" in english_package
        and "Active — grid charging" in english_package
        and "Unavailable — initializing" in english_package
        and "Enabled — blocked: RCE policy is enabled" in english_package,
        "English EMS package lacks the human ownership/tariff policy states",
    )
    require(
        "Brak aktywnej automatyki" in polish_package
        and "Balansowanie" in polish_package
        and "Sterowanie ręczne" in polish_package
        and "Włączone — oczekuje na wybrany blok" in polish_package
        and "Aktywne — ładowanie z sieci" in polish_package
        and "Niedostępne — trwa inicjalizacja" in polish_package
        and "Włączone — zablokowane: włączona polityka RCE" in polish_package,
        "Polish EMS package lacks the human ownership/tariff policy states",
    )

    dashboard_source = (
        ROOT / "dashboard_hoymiles.yaml"
    ).read_text(encoding="utf-8")

    def require_owner_and_tariff_rows(text: str, label: str) -> None:
        owner_marker = "entity: sensor.hoymiles_ems_control_owner"
        tariff_marker = "entity: sensor.hoymiles_tariff_charge_status"
        conflict_marker = "entity: binary_sensor.hoymiles_ems_control_conflict"
        main_start = text.index(owner_marker)
        main_end = text.index("\n      - type:", main_start)
        main_card = text[main_start:main_end]
        require(
            main_card.index(owner_marker)
            < main_card.index(tariff_marker)
            < main_card.index(conflict_marker),
            f"{label} main EMS card does not pair owner and tariff policy rows",
        )
        tariff_view = text.split("path: ladowanie-taryfowe", 1)[1].split(
            "\n  - title:", 1
        )[0]
        require(
            tariff_view.count(owner_marker) == 1
            and tariff_view.count(tariff_marker) == 1
            and tariff_view.count(conflict_marker) == 1
            and tariff_view.index(owner_marker)
            < tariff_view.index(tariff_marker)
            < tariff_view.index(conflict_marker),
            f"{label} tariff view must show one always-visible owner/policy pair",
        )

    require_owner_and_tariff_rows(dashboard_source, "Source dashboard")
    require_owner_and_tariff_rows(
        required_assets[0].read_text(encoding="utf-8"),
        "English dashboard",
    )
    require_owner_and_tariff_rows(
        required_assets[1].read_text(encoding="utf-8"),
        "Polish dashboard",
    )
    expected_zebra_cards = dashboard_source.count(
        "type: custom:hoymiles-zebra-entities-card"
    )
    require(
        expected_zebra_cards >= 56,
        "Source dashboard unexpectedly lost zebra entity cards",
    )
    rce_plan_index = dashboard_source.index("title: Plan rozładowań RCE")
    rce_details_index = dashboard_source.index(
        "title: RCE — szczegóły i diagnostyka"
    )
    rce_details_end = dashboard_source.index(
        "\n      - type: markdown\n",
        rce_details_index,
    )
    require(
        rce_plan_index < rce_details_index
        and "position: sidebar"
        not in dashboard_source[rce_details_index:rce_details_end],
        "RCE details must remain in the main column below the discharge plan",
    )
    dashboard_payloads = []
    for dashboard_json in required_assets[-2:]:
        dashboard_data = load_json(dashboard_json)
        dashboard_payloads.append(dashboard_data)
        require(
            isinstance(dashboard_data, dict)
            and isinstance(dashboard_data.get("views"), list)
            and len(dashboard_data["views"]) >= 10,
            f"Invalid dashboard strategy payload: {dashboard_json.name}",
        )
        dashboard_json_text = json.dumps(dashboard_data)
        require(
            dashboard_json_text.count(
                '"type": "custom:hoymiles-zebra-entities-card"'
            )
            == expected_zebra_cards
            and '"type": "entities"' not in dashboard_json_text,
            f"{dashboard_json.name} does not use all "
            f"{expected_zebra_cards} zebra entity cards",
        )
        require(
            sum(
                item.get("type")
                == "custom:hoymiles-aurora-frame-card"
                for item in iter_mappings(dashboard_data)
            )
            == 4,
            f"{dashboard_json.name} must contain four authored Aurora frames",
        )
    require(
        dashboard_structure(dashboard_payloads[0])
        == dashboard_structure(dashboard_payloads[1]),
        "English and Polish strategy payloads have different card structure",
    )

    card_source = required_assets[4].read_text(encoding="utf-8")
    require(
        "ll-strategy-dashboard-hoymiles-hit-xxl-g3" in card_source
        and "dashboard_hoymiles_${language}.json" in card_source
        and "import.meta.url" in card_source
        and "/api/hoymiles_hit_modbus/static/dashboard_hoymiles_" not in card_source
        and "window.customStrategies" in card_source,
        "Dashboard strategy is not update-safe or hard-codes a stale asset route",
    )
    bootstrap_source = required_assets[5].read_text(encoding="utf-8")
    require(
        "ll-strategy-dashboard-hoymiles-hit-xxl-g3" in bootstrap_source
        and "document.currentScript" in bootstrap_source
        and "document.scripts" in bootstrap_source
        and "canonicalModuleUrl" in bootstrap_source
        and "import(canonicalModuleUrl.href)" in bootstrap_source
        and "/api/hoymiles_hit_modbus/static-r2/" not in bootstrap_source
        and "import.meta" not in bootstrap_source,
        "Classic dashboard bootstrap is missing or uses ES-module-only syntax",
    )
    require(
        card_source
        == (ROOT / "home_assistant" / "www" / "hoymiles-rce-chart-card.js")
        .read_text(encoding="utf-8")
        and bootstrap_source
        == (
            ROOT
            / "home_assistant"
            / "www"
            / "hoymiles-dashboard-strategy.js"
        ).read_text(encoding="utf-8"),
        "Generated frontend resources differ from their canonical sources",
    )
    require(
        "futureNoData" in card_source
        and "will recalculate automatically after publication" in card_source,
        "RCE chart does not explain automatic replanning after tomorrow's data",
    )
    require(
        "unitMultipliers" in card_source
        and "kWh: 1000" in card_source
        and "_resolveBatteryEnergy" in card_source,
        "Power-flow card does not convert the capacity entity from kWh to Wh",
    )
    require(
        "class HoymilesZebraEntitiesCard" in card_source
        and 'customElements.define(\n    "hoymiles-zebra-entities-card"' in card_source
        and "color-mix(" in card_source
        and "var(--hoymiles-aurora-accent) 9%" in card_source,
        "Theme-aware zebra entities card is not registered",
    )
    require(
        "class HoymilesAuroraFrameCard" in card_source
        and "class HoymilesAuroraStatusCard" in card_source
        and "class HoymilesAuroraHistoryCard" in card_source
        and "class HoymilesAuroraFinanceCard" in card_source,
        "Complete Aurora dashboard card set is not registered",
    )
    for dashboard_path in required_assets[:2]:
        dashboard_text = dashboard_path.read_text(encoding="utf-8")
        require(
            "entity: sensor.hoymiles_rce_day_tomorrow\n"
            "            future_data: true" in dashboard_text,
            f"{dashboard_path.name} does not mark the tomorrow chart as future data",
        )
        require(
            "type: custom:hoymiles-aurora-energy-card" in dashboard_text
            and "type: custom:hoymiles-aurora-status-card" in dashboard_text
            and "type: custom:hoymiles-aurora-history-card" in dashboard_text
            and "type: custom:hoymiles-aurora-finance-card" in dashboard_text
            and "type: custom:hoymiles-aurora-frame-card" in dashboard_text
            and "battery_soc_entity: sensor.hoymiles_hit_overview_battery_soc"
            in dashboard_text
            and "forecast_remaining_entity: "
            "sensor.hoymiles_solcast_forecast_remaining_today"
            in dashboard_text,
            f"{dashboard_path.name} does not contain the complete Aurora card",
        )
        require(
            "path: automatyka-ems" in dashboard_text
            and "sensor.hoymiles_rce_revenue_total" in dashboard_text
            and "sensor.hoymiles_rce_grid_export_energy_total" in dashboard_text,
            f"{dashboard_path.name} lacks the consolidated RCE results view",
        )
        for hidden_path in ("zyski", "falownik", "generator", "stany-alarmy"):
            require(
                re.search(
                    rf"path:\s+{re.escape(hidden_path)}"
                    rf"[\s\S]{{0,120}}?subview:\s+true",
                    dashboard_text,
                ),
                f"{dashboard_path.name} still exposes {hidden_path} in navigation",
            )
        require(
            "invert_power:" not in dashboard_text,
            (
                f"{dashboard_path.name} incorrectly inverts the normalized "
                "Hoymiles battery power sign"
            ),
        )
        require(
            dashboard_text.count(
                "type: custom:hoymiles-zebra-entities-card"
            )
            == expected_zebra_cards
            and not re.search(r"^\s*-?\s*type:\s+entities\s*$", dashboard_text, re.M),
            f"{dashboard_path.name} does not use all "
            f"{expected_zebra_cards} zebra entity cards",
        )
        require(
            dashboard_text.count("type: statistics-graph") >= 12
            and "period: 5minute" in dashboard_text
            and "min_y_axis: 220" in dashboard_text
            and 'color: "#FF1744"' in dashboard_text
            and 'color: "#00B0FF"' in dashboard_text
            and 'color: "#FFD600"' in dashboard_text,
            f"{dashboard_path.name} lacks the native statistics graph set",
        )
        load_graph_title = (
            "Odbiorniki — moc ostatnie 24 godziny [W]"
            if dashboard_path.name.endswith("_pl.yaml")
            else "Loads — power over the last 24 hours [W]"
        )
        load_energy_title = (
            "Zużycie domu — ostatnie 30 dni [kWh]"
            if dashboard_path.name.endswith("_pl.yaml")
            else "Home consumption — last 30 days [kWh]"
        )
        require(
            load_graph_title in dashboard_text
            and load_energy_title in dashboard_text
            and "entity: sensor.hoymiles_actual_load_energy_total"
            in dashboard_text,
            f"{dashboard_path.name} lacks the LOAD power/energy graphs",
        )

    rce_sensor_source = (
        COMPONENT / "rce_sensor.py"
    ).read_text(encoding="utf-8")
    require(
        '"planning_scope": (' in rce_sensor_source
        and '"tomorrow_data_pending": not tomorrow_rows_complete' in rce_sensor_source
        and '"automatic_replan": True' in rce_sensor_source
        and "[*today_rows, *usable_tomorrow_rows]" in rce_sensor_source,
        "RCE sensor lacks the safe today-only planning fallback",
    )
    rce_optimizer_source = (
        COMPONENT / "rce_optimizer.py"
    ).read_text(encoding="utf-8")
    rce_test_source = (ROOT / "tools" / "test_rce_optimizer.py").read_text(
        encoding="utf-8"
    )
    require(
        "def _solve_joint_horizon_exports(" in rce_optimizer_source
        and "def maximum_feasible(" in rce_optimizer_source
        and 'solver_method: str = "joint_horizon_bounded_active_set"'
        in rce_optimizer_source
        and "optimality_verified: bool = False" in rce_optimizer_source
        and "exports[candidate.start] = round(low, 2)" not in rce_optimizer_source
        and "test_solver_matches_independent_random_oracle"
        in rce_test_source
        and "test_real_horizon_solver_runtime_is_bounded" in rce_test_source,
        "RCE optimizer lacks the bounded joint-horizon/oracle safety contract",
    )
    require(
        "minimum_price_pln_kwh" not in rce_optimizer_source
        and "hoymiles_rce_price_threshold" not in rce_sensor_source,
        "RCE optimizer still depends on a manually configured price threshold",
    )
    tariff_optimizer_source = (
        COMPONENT / "tariff_optimizer.py"
    ).read_text(encoding="utf-8")
    tariff_sensor_source = (
        COMPONENT / "tariff_sensor.py"
    ).read_text(encoding="utf-8")
    require(
        "first_shortage_index" in tariff_optimizer_source
        and "accepted_support_kwh" in tariff_optimizer_source
        and "for index in range(len(starts))" in tariff_optimizer_source
        and "trial_simulation.shortage_kwh" in tariff_optimizer_source
        and "minimum_saving_pln_kwh" in tariff_optimizer_source,
        "Tariff optimizer lacks deficit-reducing direct support or charging",
    )
    require(
        "bms_charge_power_limit_kw" in tariff_sensor_source
        and "effective_charge_power_percent" in tariff_sensor_source
        and "forecast_tomorrow_kwh" in tariff_sensor_source,
        "Tariff sensor lacks forecast or BMS-safe charge-power diagnostics",
    )

    stable_entity_assets = [
        ROOT / "dashboard_hoymiles.yaml",
        ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml",
        required_assets[0],
        required_assets[1],
        required_assets[2],
        required_assets[3],
    ]
    stable_entity_pattern = re.compile(
        r"\b(button|sensor|number|select)\.hoymiles_hit_([a-z0-9_]+)\b"
    )
    legacy_entity_pattern = re.compile(
        r"\b(?:button|sensor|number|select)\."
        r"[a-z0-9_]*hoymiles_inverter[a-z0-9_]*\b"
    )
    native_integration_entities = {
        ("sensor", "rce_optimized_plan"),
        ("sensor", "tariff_charge_plan"),
        ("sensor", "rcm_voltage_plan"),
        ("sensor", "setup_status"),
    }
    for asset_path in stable_entity_assets:
        asset_text = asset_path.read_text(encoding="utf-8")
        require(
            not legacy_entity_pattern.search(asset_text),
            "Installation-specific entity id remains in "
            f"{asset_path.relative_to(ROOT)}",
        )
        for domain, translation_key in stable_entity_pattern.findall(
            asset_text
        ):
            require(
                (domain, translation_key) in identities
                or (domain, translation_key) in native_integration_entities,
                f"Asset references an entity absent from the catalog: "
                f"{domain}.hoymiles_hit_{translation_key}",
            )

    for package_path in required_assets[2:4]:
        package_text = package_path.read_text(encoding="utf-8")
        dynamic_reserve_markers = (
            "hoymiles_ems_push_notifications_enabled:",
            "hoymiles_ems_push_notify_target:",
            "unique_id: hoymiles_ems_push_notification_status",
            "id: hoymiles_ems_push_status_notification",
            "action: notify.send_message",
            "input_boolean.hoymiles_rce_dynamic_soc_enabled",
            "input_number.hoymiles_rce_soc_safety_margin",
            "input_text.hoymiles_solcast_forecast_today_entity",
            "input_text.hoymiles_solcast_forecast_tomorrow_entity",
            "hoymiles_rce_inverter_rated_power:",
            "hoymiles_rce_fallback_daily_load:",
            "hoymiles_rce_export_efficiency:",
            "sensor.solcast_pv_forecast_forecast_today",
            "sensor.solcast_pv_forecast_forecast_tomorrow",
            "sensor.solcast_pv_forecast_prognoza_na_dzisiaj",
            "sensor.solcast_pv_forecast_prognoza_na_jutro",
            "unique_id: hoymiles_rce_day_tomorrow",
            "state_characteristic: sum_differences_nonnegative",
            "max_age:\n      days: 4",
            "hoymiles_tariff_charge_enabled:",
            "hoymiles_tariff_type:",
            "hoymiles_tariff_latched_target_soc:",
            "hoymiles_tariff_latched_slot_end:",
            "input_number.hoymiles_tariff_latched_target_soc",
            "input_datetime.hoymiles_tariff_latched_slot_end",
            "'current_slot_end'",
            'for: "00:00:45"',
            "unique_id: hoymiles_tariff_planned_charge_slot",
            "id: hoymiles_automatic_ems_mode_interlock",
            "id: hoymiles_tariff_grid_charge_control",
            "hoymiles_rcm_pre_discharge_enabled:",
            "hoymiles_rcm_pre_discharge_active:",
            "unique_id: hoymiles_rcm_natural_headroom_before_risk",
            "unique_id: hoymiles_rcm_planned_grid_discharge",
            "unique_id: hoymiles_rcm_pre_discharge_power",
            "unique_id: hoymiles_rcm_pre_discharge_target_soc",
            "id: hoymiles_rcm_pre_discharge_control",
            "input_boolean.hoymiles_rcm_shadow_mode",
            "binary_sensor.hoymiles_sale_block_active",
            "number.hoymiles_hit_maximum_discharge_power",
            "number.hoymiles_hit_force_discharge_soc",
            "hoymiles_battery_balancing_enabled:",
            "hoymiles_battery_balancing_active:",
            "hoymiles_battery_balancing_interval_days:",
            "hoymiles_battery_balancing_hold_hours:",
            "unique_id: hoymiles_battery_balancing_slow_charge_power",
            "unique_id: hoymiles_battery_balancing_due",
            "hoymiles_start_battery_balancing:",
            "hoymiles_stop_battery_balancing:",
            "id: hoymiles_battery_balancing_control",
            "timer.hoymiles_battery_balancing_watchdog",
            "input_boolean.hoymiles_battery_balancing_active",
            "sensor.hoymiles_actual_load_energy_today",
            "unique_id: hoymiles_actual_load_energy_total",
            "unique_id: hoymiles_actual_load_energy_daily",
            "source: sensor.hoymiles_actual_load_energy_total",
            "source_entity: sensor.hoymiles_actual_load_power",
            "sensor.hoymiles_actual_load_power",
            "sensor.hoymiles_hit_load_power_l1n",
            "sensor.hoymiles_hit_load_power_l2n",
            "sensor.hoymiles_hit_load_power_l3n",
            "sensor.hoymiles_night_protected_load_power",
            "sensor.hoymiles_night_protection_window_remaining",
            "sensor.hoymiles_protected_window_expected_load",
            "{% set upcoming_start = setting - buffer %}",
            "{% set upcoming_end = rising + buffer %}",
            "upcoming_end - upcoming_start",
            "sensor.hoymiles_rce_protected_home_energy",
            "state_attr('sun.sun', 'next_rising')",
            "state_attr('sun.sun', 'next_setting')",
            "sensor.hoymiles_hit_battery_capacity",
            "sensor.hoymiles_hit_ems_self_use_soc_readback",
            "sensor.hoymiles_hit_rce_optimized_plan",
            "sensor.hoymiles_rce_dynamic_minimum_soc",
            "sensor.hoymiles_rce_effective_minimum_soc",
            "hoymiles_rce_accounting_date:",
            "hoymiles_rce_grid_sell_checkpoint:",
            "hoymiles_rce_realized_controlled_export_store:",
            "hoymiles_rce_realized_natural_export_store:",
            "hoymiles_rce_realized_controlled_revenue_store:",
            "hoymiles_rce_realized_natural_revenue_store:",
            "hoymiles_rce_unclassified_export_store:",
            "id: hoymiles_rce_export_accounting",
            "unique_id: hoymiles_rce_self_use_baseline_export",
            "unique_id: hoymiles_rce_realized_controlled_export_today",
            "unique_id: hoymiles_rce_realized_natural_export_today",
            "unique_id: hoymiles_rce_realized_controlled_revenue_today",
            "unique_id: hoymiles_rce_realized_natural_revenue_today",
            "unique_id: hoymiles_rce_realized_revenue_today",
            "unique_id: hoymiles_rce_unclassified_export_today",
            "([grid / 1000, 0] | max)",
            "binary_sensor.hoymiles_rce_reserve_ready",
            "or is_state('binary_sensor.hoymiles_rce_reserve_ready', 'off')",
        )
        for marker in dynamic_reserve_markers:
            require(
                marker in package_text,
                f"Dynamic RCE reserve marker missing in {package_path.name}: {marker}",
            )
        require(
            'source_registers: "2129 + 2130 + 2131"' not in package_text
            and "sensor.hoymiles_hit_load_energy_use_l1n_today"
            not in package_text
            and "sensor.hoymiles_hit_load_energy_use_l2n_today"
            not in package_text
            and "sensor.hoymiles_hit_load_energy_use_l3n_today"
            not in package_text,
            f"Clean home-energy calculation regressed to inverter daily LOAD "
            f"counters in {package_path.name}",
        )
        require(
            "or not is_state(\n"
            "                       'binary_sensor.hoymiles_tariff_planned_charge_slot', 'on')"
            not in package_text,
            f"Tariff control still stops on a transient live-plan change in "
            f"{package_path.name}",
        )
        require(
            "[-grid / 1000, 0]" not in package_text,
            f"Grid export power still uses the reversed sign in {package_path.name}",
        )
        require(
            not re.search(
                r"device_class:\s*energy\s+state_class:\s*measurement",
                package_text,
            ),
            f"Energy helper uses invalid measurement state class in {package_path.name}",
        )

    rcm_optimizer_source = (
        COMPONENT / "rcm_optimizer.py"
    ).read_text(encoding="utf-8")
    rcm_sensor_source = (
        COMPONENT / "rcm_sensor.py"
    ).read_text(encoding="utf-8")
    for marker in (
        "expected_natural_headroom_kwh",
        "planned_grid_discharge_kwh",
        "pre_discharge_target_soc_percent",
        "pre_discharge_power_percent",
        "pre_discharge_ready",
        "protected_minimum_soc",
        "export_capacity_kw > 0.1",
    ):
        require(
            marker in rcm_optimizer_source,
            f"RCEm morning-discharge safety marker missing: {marker}",
        )
    require(
        "sensor.hoymiles_hit_maximum_discharge_current" in rcm_sensor_source
        and "minutes_to_risk" in rcm_sensor_source
        and "risk_day_offset" in rcm_sensor_source,
        "RCEm sensor lacks BMS/time inputs for safe morning discharge",
    )

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_pl = (ROOT / "README.pl.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    github_release_notes = (
        ROOT / "docs" / "releases" / f"v{manifest['version']}.md"
    ).read_text(encoding="utf-8")
    release_match = re.search(
        rf"^## \[{re.escape(manifest['version'])}\][^\n]*\n(.*?)(?=^## \[|\Z)",
        changelog,
        re.M | re.S,
    )
    require(
        release_match is not None,
        f"CHANGELOG lacks the {manifest['version']} release section",
    )
    release_notes = release_match.group(1)
    github_release_body = re.match(
        rf"^# v{re.escape(manifest['version'])}[^\n]*\n\n(.*)\Z",
        github_release_notes,
        re.S,
    )
    require(
        github_release_body is not None
        and github_release_body.group(1).strip() == release_notes.strip(),
        "Versioned GitHub Release body differs from the CHANGELOG release section",
    )
    normalized_readme = " ".join(readme.split())
    require(
        readme.startswith(f"# {EXPECTED_PROJECT_NAME}\n")
        and readme_pl.startswith(f"# {EXPECTED_PROJECT_NAME}\n")
        and EXPECTED_DESCRIPTION in normalized_readme,
        "README project title or exact GitHub description is inconsistent",
    )
    translations_en = load_json(COMPONENT / "translations" / "en.json")
    translations_pl = load_json(COMPONENT / "translations" / "pl.json")
    require(
        translations_en.get("title") == EXPECTED_PROJECT_NAME
        and translations_pl.get("title") == EXPECTED_PROJECT_NAME,
        "Localized integration titles do not match the public project name",
    )
    require(
        "### User update steps / Kroki po aktualizacji" in release_notes,
        "Release changelog lacks the HACS-visible user update steps",
    )
    for update_step in (
        "1. **Safety / Bezpieczeństwo:**",
        "2. **HACS:**",
        "3. **Home Assistant:**",
        "4. **ESP32 / ESPHome:**",
        "5. **Verification / Weryfikacja:**",
    ):
        require(
            update_step in release_notes,
            f"Release changelog lacks required user step: {update_step}",
        )
    normalized_release_notes = " ".join(release_notes.split())
    for release_contract in (
        "exactly **two Home Assistant restarts**",
        "locally modified scheduler",
        "No ESP32 / ESPHome rebuild or upload is required for",
        "Wersja v1.5.8 nie wymaga ponownej kompilacji ani wgrywania firmware",
        "inverter_nameplate_power_each_kw=20",
        "inverter_power_each_kw=16",
    ):
        require(
            release_contract in normalized_release_notes,
            f"Release changelog lacks hotfix update contract: {release_contract}",
        )
    release_procedure = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
    require(
        "GitHub Release body visible in HACS" in release_procedure
        and "does not flash the ESP32" in release_procedure,
        "Release procedure does not require complete HACS/ESP32 instructions",
    )
    readme_images = [ROOT / "docs" / "images" / "dashboard-overview.png"]
    for image in readme_images:
        require(image.is_file(), f"Missing README image: {image.relative_to(ROOT)}")
        require(
            str(image.relative_to(ROOT)).replace("\\", "/") in readme,
            f"README does not reference {image.name}",
        )
        width, height = png_dimensions(image)
        require(
            width >= 1000 and height >= 700,
            f"README image is unexpectedly small: {image.name}",
        )
    require(
        "docs/QUICK_START.md" in readme
        and "docs/QUICK_START.md" in readme_pl
        and (ROOT / "docs" / "QUICK_START.md").is_file(),
        "English and Polish READMEs must expose the beginner quick-start guide",
    )
    require(
        "### User update steps / Kroki po aktualizacji" in github_release_notes
        and "ESP32 / ESPHome" in github_release_notes
        and "2064/2064" in github_release_notes,
        "GitHub Release notes are incomplete for HACS users",
    )
    require(
        "[English](README.md) · [Polski](README.pl.md)" in readme
        and "[English](README.md) · [Polski](README.pl.md)" in readme_pl,
        "README language switch is missing or inconsistent",
    )
    for documentation_text, language, documentation_markers in (
        (
            readme,
            "English",
            (
                "/releases/latest",
                "## Compatibility and requirements",
                "## Safety",
                "### RCE market-price optimization",
                "### Tariff-aware grid charging",
                "### Experimental RCEm 253 V+ voltage management",
                "### LiFePO4 battery balancing",
                "## Parallel inverter systems",
                "docs/AUTOMATION_TEST_REPORT.md",
            ),
        ),
        (
            readme_pl,
            "Polish",
            (
                "/releases/latest",
                "## Zgodność i wymagania",
                "## Bezpieczeństwo",
                "### Optymalizacja cen RCE",
                "### Automatyczne ładowanie taryfowe",
                "### Eksperymentalne zarządzanie napięciem RCEm 253 V+",
                "### Wyrównywanie baterii LiFePO4",
                "## Instalacje z falownikami połączonymi równolegle",
                "docs/AUTOMATION_TEST_REPORT.md",
            ),
        ),
    ):
        for documentation_marker in documentation_markers:
            require(
                documentation_marker in documentation_text,
                f"{language} README is missing documentation: {documentation_marker}",
            )
    require(
        "README.pl.md" in readme and len(readme_pl.split()) >= len(readme.split()) * 0.8,
        "Polish README is not a complete edition of the English documentation",
    )

    esphome_entry_files = [
        ROOT / "hoymiles-inverter.yaml",
        ROOT / "examples" / "esphome" / "hoymiles-hit-g3.yaml",
    ]
    required_esphome_packages = {
        f"packages/{path.name}"
        for path in (ROOT / "packages").glob("*.yaml")
        if not path.name.startswith("optional_")
    }
    workflow_source = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )
    firmware_ci_path = ROOT / "tools" / "esphome_verify_ci.yaml"
    firmware_ci_source = firmware_ci_path.read_text(encoding="utf-8")
    require(
        "firmware-compile:" in workflow_source
        and "github.event_name == 'workflow_dispatch'" in workflow_source
        and "startsWith(github.ref, 'refs/tags/v')" in workflow_source
        and '"esphome==2026.7.2"' in workflow_source
        and "esphome config tools/esphome_verify_ci.yaml" in workflow_source
        and "esphome compile tools/esphome_verify_ci.yaml" in workflow_source,
        "Release workflow lacks the pinned full ESPHome compile gate",
    )
    require(
        "CI-only full firmware fixture" in firmware_ci_source
        and 'wifi_ssid: "ci-placeholder-network"' in firmware_ci_source
        and 'wifi_password: "ci-placeholder-password"' in firmware_ci_source
        and 'ota_password: "ci-placeholder-ota"' in firmware_ci_source,
        "Firmware CI fixture must contain only documented placeholder credentials",
    )
    firmware_ci_packages = set(
        re.findall(r"!include ../(packages/[a-z0-9_]+\.yaml)", firmware_ci_source)
    )
    require(
        firmware_ci_packages == required_esphome_packages,
        "Firmware CI fixture does not compile the complete required package set",
    )
    for entry_file in esphome_entry_files:
        entry_text = entry_file.read_text(encoding="utf-8")
        require(
            "!include packages/" not in entry_text,
            f"Public ESPHome entry point uses local includes: {entry_file.name}",
        )
        require(
            f"url: https://github.com/{EXPECTED_REPOSITORY}"
            in entry_text,
            f"Public ESPHome entry point has no remote package: {entry_file.name}",
        )
        require(
            f"ref: v{compatible_firmware_version}" in entry_text,
            "ESPHome entry point is not pinned to the compatible firmware version: "
            f"{entry_file.name}",
        )
        require(
            "dashboard_import:" in entry_text
            and f"package_import_url: github://{EXPECTED_REPOSITORY}/"
            in entry_text
            and "import_full_config: true" in entry_text,
            f"ESPHome adoption/update metadata is missing in {entry_file.name}",
        )
        included_packages = set(
            re.findall(r"^\s*-\s+(packages/[a-z0-9_]+\.yaml)\s*$", entry_text, re.M)
        )
        require(
            included_packages == required_esphome_packages,
            f"ESPHome package list differs in {entry_file.name}",
        )
        require(
            entry_text.index("- packages/parallel_network.yaml")
            < entry_text.index("- packages/settings.yaml"),
            f"Parallel topology must load before EMS settings in {entry_file.name}",
        )

    active_repository_files = [
        ROOT / "README.md",
        ROOT / "README.pl.md",
        ROOT / "NOTICE",
        ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
        COMPONENT / "manifest.json",
        COMPONENT / "__init__.py",
        ROOT / "home_assistant" / "www" / "hoymiles-dashboard-strategy.js",
        ROOT / "home_assistant" / "www" / "hoymiles-rce-chart-card.js",
        RESOURCES / "www" / "hoymiles-dashboard-strategy.js",
        RESOURCES / "www" / "hoymiles-rce-chart-card.js",
        *esphome_entry_files,
    ]
    for active_repository_file in active_repository_files:
        require(
            LEGACY_REPOSITORY_SLUG
            not in active_repository_file.read_text(encoding="utf-8"),
            f"Active file still references the legacy repository slug: "
            f"{active_repository_file.relative_to(ROOT)}",
        )

    parallel_source = (ROOT / "packages" / "parallel_network.yaml").read_text(
        encoding="utf-8"
    )
    settings_source = (ROOT / "packages" / "settings.yaml").read_text(
        encoding="utf-8"
    )
    for marker in (
        "address: 6048",
        "address: 6049",
        'name: "Parallel Topology"',
        'name: "Parallel EMS Control Status"',
        'name: "Parallel Topology Readback Generation"',
        'name: "EMS Verified Hardware Readback Supported"',
        'name: "Direct Register Verified Readback Supported"',
        "count < 2 || count > 10",
        "return count >= 2 && count <= 10 ? 1.0f : 0.0f;",
        "Gotowe - broadcast EMS, odczyt Mastera",
    ):
        require(
            marker in parallel_source,
            f"Parallel topology marker missing: {marker}",
        )
    for marker in (
        "machine_type == 2",
        "machine_count < 2 || machine_count > 10",
        'name: "EMS Control Readback Generation"',
        'name: "GCF Control Readback Generation"',
        'name: "Battery Charge Power Readback Generation"',
        "id(ems_verified_hardware_readback_supported).state",
        "id(direct_register_verified_readback_supported).state",
        "id: ems_write_complete_block_4300_4306",
        "0x00, 0x10, 0x10, 0xCC, 0x00, 0x07, 0x0E",
        "id(modbus_1).send_raw(payload);",
        "create_write_multiple_command",
    ):
        require(
            marker in settings_source,
            f"Parallel EMS safety marker missing: {marker}",
        )
    require(
        settings_source.count("send_raw(payload)") == 1
        and settings_source.count("0x00, 0x10, 0x10, 0xCC") == 1,
        "Parallel EMS broadcast must have one canonical complete-block writer",
    )
    require(
        'name: "Hoymiles Direct Register Execution Ready"' in ems_package_source
        and "sensor.hoymiles_hit_direct_register_verified_readback_supported"
        in ems_package_source
        and "system_broadcast_with_master_fc03" in ems_package_source,
        "HA package does not separate broadcast EMS from direct registers",
    )
    require(
        "hoymiles_modbus_slave_" not in parallel_source
        and "hoymiles_modbus_slave_" not in settings_source,
        "External Modbus must not poll or write internal parallel Slave addresses",
    )

    overview_source = (ROOT / "packages" / "overview.yaml").read_text(
        encoding="utf-8"
    )

    def esphome_sensor_block(source: str, sensor_id: str) -> str:
        """Return one ESPHome sensor block for structural assertions."""
        marker = f"    id: {sensor_id}\n"
        start = source.index(marker)
        end = source.find("\n  - platform:", start)
        return source[start:] if end == -1 else source[start:end]

    overview_ids = (
        "pv_total_power_30001",
        "inv_active_power_30007",
        "battery_power_30009",
        "grid_total_active_power_30011",
        "load_active_power_30015",
        "battery_soc_30020",
    )
    for overview_id in overview_ids:
        overview_block = esphome_sensor_block(overview_source, overview_id)
        require(
            "update_interval: never" in overview_block
            and "force_update: true" in overview_block
            and "lambda:" not in overview_block,
            f"{overview_id} must be published only by physical FC03 callbacks",
        )

    pv_source = (ROOT / "packages" / "pv.yaml").read_text(encoding="utf-8")
    load_source = (ROOT / "packages" / "backup_load.yaml").read_text(
        encoding="utf-8"
    )
    grid_source = (ROOT / "packages" / "meters.yaml").read_text(
        encoding="utf-8"
    )
    battery_source = (ROOT / "packages" / "battery.yaml").read_text(
        encoding="utf-8"
    )
    physical_publications = {
        "pv_total_power_30001": (overview_source, pv_source),
        "inv_active_power_30007": (overview_source, load_source),
        "battery_power_30009": (overview_source, load_source),
        "grid_total_active_power_30011": (overview_source, grid_source),
        "load_active_power_30015": (overview_source, load_source),
        "battery_soc_30020": (overview_source, battery_source),
    }
    for overview_id, sources in physical_publications.items():
        marker = f"id({overview_id}).publish_state("
        require(
            all(marker in source for source in sources),
            f"{overview_id} lacks one single/Master physical publication path",
        )

    require(
        "bat_total_power_8546" not in load_source,
        "Parallel overview power must not use the signed 16-bit register 2162",
    )
    for marker in (
        "id(pv_total_power_8528)",
        "id(grid_total_active_power_1814)",
        "static_cast<uint32_t>(now_ms - grid_ms) <= 30000U",
        "static_cast<uint32_t>(now_ms - pv_ms) <= 30000U",
    ):
        require(
            marker in load_source,
            f"Parallel event-driven balance is missing input/coherence: {marker}",
        )
    require(
        "const float inverter_power =" in load_source
        and "x + id(grid_total_active_power_1814).state" in load_source,
        "Parallel inverter overview must equal LOAD plus grid export",
    )
    require(
        "inverter_power - id(pv_total_power_8528).state" in load_source,
        "Parallel battery overview must equal LOAD plus grid export minus PV",
    )
    require(
        "65 535 W" in overview_source
        and "nie pozwala wykryć zawinięcia do zera" in overview_source,
        "Parallel overview must document the U_WORD range limitation",
    )

    pv_system_block = esphome_sensor_block(
        pv_source,
        "pv_total_power_8528",
    )
    load_system_block = esphome_sensor_block(
        load_source,
        "load_power_total_8553",
    )
    grid_system_block = esphome_sensor_block(
        grid_source,
        "grid_total_active_power_1814",
    )
    require(
        "address: 2150" in pv_system_block
        and "value_type: U_WORD" in pv_system_block
        and "address: 2169" in load_system_block
        and "value_type: U_WORD" in load_system_block
        and "address: 1814" in grid_system_block
        and "value_type: S_DWORD" in grid_system_block,
        "Parallel overview balance input widths no longer match the documented map",
    )

    for asset in required_assets[:4]:
        text = asset.read_text(encoding="utf-8")
        require(
            not legacy_entity_pattern.search(text),
            f"Installation-specific entity id remains in {asset.name}",
        )
        require(
            not re.search(r"\bPV[56]\b", text, flags=re.IGNORECASE),
            f"Unsupported PV5/PV6 reference remains in {asset.name}",
        )

    polish_dashboard = required_assets[1].read_text(encoding="utf-8")
    english_dashboard = required_assets[0].read_text(encoding="utf-8")
    for dashboard_text, language in (
        (polish_dashboard, "Polish"),
        (english_dashboard, "English"),
    ):
        dashboard_lines = dashboard_text.splitlines()
        require(
            not re.search(
                r"^\s*-\s+(?:button|sensor|number|select)\.hoymiles_hit_[a-z0-9_]+\s*$",
                dashboard_text,
                re.M,
            ),
            f"{language} dashboard contains entity rows without short names",
        )
        for index, line in enumerate(dashboard_lines):
            entity_match = re.match(
                r"^(?P<indent>\s*)-\s+entity:\s+"
                r"(?:button|sensor|number|select)\.hoymiles_hit_[a-z0-9_]+\s*$",
                line,
            )
            if not entity_match:
                continue
            following = (
                dashboard_lines[index + 1]
                if index + 1 < len(dashboard_lines)
                else ""
            )
            require(
                following.startswith(
                    f"{entity_match.group('indent')}  name:"
                ),
                f"{language} dashboard entity row on line {index + 1} "
                "has no dashboard-only short name",
            )
        require(
            not re.search(
                r"^\s*name:\s*[\"']?Hoymiles Inverter\b",
                dashboard_text,
                re.M | re.I,
            ),
            f"{language} dashboard still displays the Hoymiles Inverter prefix",
        )
        require(
            "<table>" not in dashboard_text,
            f"{language} dashboard still contains non-clickable HTML tables",
        )
        require(
            "type: custom:hoymiles-aurora-energy-card" in dashboard_text,
            f"{language} dashboard does not use the Aurora live-energy card",
        )
        require(
            "sensor.hoymiles_hit_battery_current_inverter" in dashboard_text,
            f"{language} dashboard does not show inverter-side battery current",
        )
        require(
            "sensor.hoymiles_hit_battery_1_voltage" in dashboard_text,
            f"{language} dashboard does not show inverter-side battery voltage",
        )
        require(
            "button.hoymiles_hit_clear_fault" in dashboard_text,
            f"{language} dashboard lacks the Clear Fault button",
        )
        require(
            '<ha-alert alert-type="error">' not in dashboard_text,
            f"{language} dashboard still contains the removed red alert rows",
        )
        state_title = (
            "Stan systemu i łączność"
            if language == "Polish"
            else "System and connectivity"
        )
        alarm_title = (
            "Alarmy — szybki podgląd"
            if language == "Polish"
            else "Alarms — quick view"
        )
        start_section = dashboard_text.split("  - title: Start", 1)[1].split(
            "\n  - title:", 1
        )[0]
        require(
            state_title not in start_section and alarm_title not in start_section,
            f"{language} Start view still duplicates diagnostics/status cards",
        )
        require(
            f"path: {'sterowanie' if language == 'Polish' else 'control'}\n    icon: mdi:tune-variant\n    type: sidebar"
            in dashboard_text,
            f"{language} control view is not using the sidebar layout",
        )
        for entity_id in (
            "input_boolean.hoymiles_ems_push_notifications_enabled",
            "input_text.hoymiles_ems_push_notify_target",
            "sensor.hoymiles_ems_push_notification_status",
            "input_boolean.hoymiles_rce_dynamic_soc_enabled",
            "input_number.hoymiles_rce_soc_safety_margin",
            "input_text.hoymiles_solcast_forecast_today_entity",
            "input_text.hoymiles_solcast_forecast_tomorrow_entity",
            "input_select.hoymiles_rce_inverter_rated_power",
            "input_number.hoymiles_rce_fallback_daily_load",
            "sensor.hoymiles_hit_rce_optimized_plan",
            "sensor.hoymiles_solcast_forecast_today",
            "sensor.hoymiles_solcast_forecast_remaining_today",
            "sensor.hoymiles_solcast_forecast_tomorrow",
            "sensor.hoymiles_load_average_4_days",
            "sensor.hoymiles_night_load_average_4_days",
            "sensor.hoymiles_rce_protected_home_energy",
            "sensor.hoymiles_rce_dynamic_minimum_soc",
            "sensor.hoymiles_rce_self_use_baseline_export",
            "sensor.hoymiles_hit_grid_energy_sell_today",
            "sensor.hoymiles_rce_realized_controlled_export_today",
            "sensor.hoymiles_rce_realized_natural_export_today",
            "sensor.hoymiles_rce_unclassified_export_today",
            "sensor.hoymiles_rce_realized_controlled_revenue_today",
            "sensor.hoymiles_rce_realized_natural_revenue_today",
            "sensor.hoymiles_rce_realized_revenue_today",
            "sensor.hoymiles_rce_day",
            "sensor.hoymiles_rce_day_tomorrow",
            "input_boolean.hoymiles_tariff_charge_enabled",
            "input_select.hoymiles_tariff_type",
            "input_number.hoymiles_tariff_g11_price",
            "input_number.hoymiles_tariff_low_price",
            "sensor.hoymiles_hit_tariff_charge_plan",
            "binary_sensor.hoymiles_tariff_planned_charge_slot",
            "sensor.hoymiles_tariff_target_soc",
            "sensor.hoymiles_tariff_planned_grid_import",
            "sensor.hoymiles_tariff_estimated_savings",
            "sensor.hoymiles_tariff_grid_charge_energy_daily",
            "sensor.hoymiles_tariff_savings_daily",
            "input_boolean.hoymiles_battery_balancing_enabled",
            "input_number.hoymiles_battery_balancing_interval_days",
            "input_number.hoymiles_battery_balancing_hold_hours",
            "sensor.hoymiles_battery_balancing_status",
            "sensor.hoymiles_battery_balancing_next_run",
            "timer.hoymiles_battery_balancing_hold",
            "sensor.hoymiles_battery_balancing_slow_charge_power",
        ):
            require(
                entity_id in dashboard_text,
                f"{language} dashboard lacks dynamic RCE entity {entity_id}",
            )
        require(
            "path: ladowanie-taryfowe" in dashboard_text,
            f"{language} dashboard lacks the tariff charging view",
        )
        require(
            "https://github.com/BJReplay/ha-solcast-solar" in dashboard_text,
            f"{language} dashboard does not document the Solcast dependency",
        )
    require(
        "Wyczyść alarmy falownika" in polish_dashboard,
        "Polish dashboard lacks the localized Clear Fault name",
    )
    require(
        "Clear Fault" in english_dashboard,
        "English dashboard lacks the localized Clear Fault name",
    )
    require(
        'name: "Docelowy SOC ładowania z sieci"' in polish_dashboard,
        "Polish dashboard lacks the localized Force Charge SOC name",
    )
    require(
        'name: "Maksymalna moc rozładowania do sieci"' in polish_dashboard,
        "Polish dashboard lacks the localized Maximum Discharge Power name",
    )
    for dashboard_text, language in (
        (polish_dashboard, "Polish"),
        (english_dashboard, "English"),
    ):
        for entity_id in (
            "sensor.hoymiles_hit_grid_voltage_l1",
            "sensor.hoymiles_hit_grid_voltage_l2",
            "sensor.hoymiles_hit_grid_voltage_l3",
            "sensor.hoymiles_hit_inverter_grid_frequency",
        ):
            require(
                entity_id in dashboard_text,
                f"{language} dashboard lacks grid diagnostic entity {entity_id}",
            )
    require(
        "potrzebna domowi" not in english_dashboard,
        "English dashboard contains an untranslated RCE reserve explanation",
    )
    for dashboard_text, language in (
        (polish_dashboard, "Polish"),
        (english_dashboard, "English"),
    ):
        for entity_id in (
            "sensor.hoymiles_hit_load_power_l1n",
            "sensor.hoymiles_hit_load_power_l2n",
            "sensor.hoymiles_hit_load_power_l3n",
            "sensor.hoymiles_hit_load_power_total",
        ):
            require(
                entity_id in dashboard_text,
                f"{language} dashboard lacks live load-power entity {entity_id}",
            )
        for entity_id in (
            "sensor.hoymiles_rce_revenue_daily",
            "sensor.hoymiles_rce_revenue_weekly",
            "sensor.hoymiles_rce_revenue_monthly",
            "sensor.hoymiles_rce_revenue_yearly",
            "sensor.hoymiles_rce_grid_export_energy_daily",
            "sensor.hoymiles_rce_grid_export_energy_weekly",
            "sensor.hoymiles_rce_grid_export_energy_monthly",
            "sensor.hoymiles_rce_grid_export_energy_yearly",
        ):
            require(
                entity_id in dashboard_text,
                f"{language} dashboard lacks profit-period entity {entity_id}",
            )
        for entity_id in (
            "select.hoymiles_hit_generation_control_function",
            "number.hoymiles_hit_maximum_export_power_limit",
            "select.hoymiles_hit_gen_port_mode",
            "sensor.hoymiles_hit_overview_generator_active_power",
        ):
            require(
                entity_id in dashboard_text,
                f"{language} dashboard lacks GCF/GEN entity {entity_id}",
            )
        require(
            "grid_input_power_limitation_valley" not in dashboard_text,
            f"{language} dashboard still contains the removed Valley setting",
        )
    require(
        'id: replenish_power_310' in settings_source
        and 'max_value: 1000' in settings_source
        and 'return x > 1000.0f ? 1000.0f' in settings_source,
        "Low-SOC grid-charge register 310 is not protected by the 1000 W limit",
    )
    for register_id in (
        "battery_max_charge_power_306",
        "battery_max_discharge_power_307",
    ):
        register_offset = settings_source.index(f"id: {register_id}")
        register_block = settings_source[register_offset : register_offset + 1300]
        require(
            "lambda: return x * 0.1f;" in register_block
            and "return safe * 10.0f;" in register_block,
            f"Register {register_id} does not use the required 0.1% scale",
        )
    for dashboard_text, language, topology_name, status_name in (
        (
            polish_dashboard,
            "Polish",
            'name: "Topologia sieci"',
            'name: "Gotowość sterowania EMS"',
        ),
        (
            english_dashboard,
            "English",
            'name: "Network topology"',
            'name: "EMS control readiness"',
        ),
    ):
        require(
            "sensor.hoymiles_hit_parallel_topology" in dashboard_text
            and "sensor.hoymiles_hit_parallel_ems_control_status" in dashboard_text
            and topology_name in dashboard_text
            and status_name in dashboard_text,
            f"{language} dashboard lacks localized parallel EMS diagnostics",
        )

    english_assets = [required_assets[0], required_assets[2]]
    polish_characters = re.compile(r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")
    for asset in english_assets:
        visible_lines = [
            line
            for line in asset.read_text(encoding="utf-8").splitlines()
            if not line.lstrip().startswith("#")
        ]
        visible_text = "\n".join(visible_lines).replace(
            "sensor.solcast_pv_forecast_prognoza_na_jutro",
            "sensor.solcast_pv_forecast_localized_tomorrow",
        )
        require(
            not polish_characters.search(visible_text),
            f"Visible Polish text remains in English asset {asset.name}",
        )

    for python_file in COMPONENT.glob("*.py"):
        py_compile.compile(python_file, doraise=True)

    localization = load_localization_module()
    require(
        localization.localized_text_state("Praca z siecią", "en") == "On-grid operation",
        "English text state localization failed",
    )
    require(
        localization.localized_text_state("Praca z siecią", "pl") == "Praca z siecią",
        "Polish text state localization failed",
    )
    require(
        localization.localized_text_state("Brak błędu", "en") == "No error",
        "English fault state localization failed",
    )
    require(
        localization.localized_text_state(
            "Gotowe - sterowanie bezpośrednie", "en"
        )
        == "Ready - direct control",
        "English parallel EMS state localization failed",
    )
    require(
        localization.localized_text_state(
            "Zablokowane - ESP32 podłączone do Slave", "pl"
        )
        == "Zablokowane - ESP32 podłączone do Slave",
        "Polish parallel EMS state localization failed",
    )
    validate_fresh_asset_install()
    validate_frontend_asset_failure_isolation(init_source)

    for image_name in (
        "icon.png",
        "dark_icon.png",
        "icon@2x.png",
        "dark_icon@2x.png",
        "logo.png",
        "dark_logo.png",
    ):
        image_path = COMPONENT / "brand" / image_name
        require(image_path.is_file(), f"Missing brand asset {image_name}")
        width, height = png_dimensions(image_path)
        require(width >= 128 and height >= 128, f"{image_name} is too small")
    for image_name, expected_size in (
        ("icon.png", 256),
        ("dark_icon.png", 256),
        ("icon@2x.png", 512),
        ("dark_icon@2x.png", 512),
    ):
        width, height = png_dimensions(COMPONENT / "brand" / image_name)
        require(
            (width, height) == (expected_size, expected_size),
            f"{image_name} must be {expected_size}x{expected_size}",
        )

    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    license_policy = (ROOT / "LICENSE_POLICY.md").read_text(encoding="utf-8")
    notice_text = (ROOT / "NOTICE").read_text(encoding="utf-8")
    contribution_text = (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")
    pr_template_text = (
        ROOT / ".github" / "pull_request_template.md"
    ).read_text(encoding="utf-8")
    codeowners_text = (ROOT / ".github" / "CODEOWNERS").read_text(
        encoding="utf-8"
    )
    workflow_text = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
        encoding="utf-8"
    )
    require(
        license_text.startswith("MIT License\n\nCopyright (c) 2026 Kaluzaburza")
        and "Permission is hereby granted, free of charge" in license_text
        and 'THE SOFTWARE IS PROVIDED "AS IS"' in license_text,
        "MIT license text is missing or incomplete",
    )
    require(
        hashlib.sha256(
            (ROOT / "LICENSE").read_bytes().replace(b"\r\n", b"\n")
        ).hexdigest()
        == "fa0bb01cef85cd8e77d7930efae10d9c93812cfa6e4878f3d7d57ad23c224290",
        "LICENSE is not the reviewed MIT text",
    )
    require(
        "[MIT License](LICENSE)" in license_policy
        and "Private and commercial use" in license_policy
        and "Oprogramowanie jest udostępniane bez gwarancji" in license_policy
        and "PolyForm" not in license_policy,
        "License policy does not describe the current MIT terms cleanly",
    )
    require(
        notice_text.startswith("Copyright (c) 2026 Kaluzaburza")
        and "Licensed under the MIT License" in notice_text,
        "Required copyright notice is missing",
    )
    require(
        "[MIT License](LICENSE)" in contribution_text
        and "commercial purposes" in contribution_text
        and "Contribution Certificate 1.0" in contribution_text
        and "Signed-off-by:" in contribution_text,
        "Contribution rights and sign-off terms are incomplete",
    )
    require(
        "I accept [CONTRIBUTING.md]" in pr_template_text
        and "Signed-off-by:" in pr_template_text
        and "* @Kaluzaburza" in codeowners_text,
        "Pull-request rights confirmation or CODEOWNERS is missing",
    )
    for test_command in (
        "python tools/test_tariff_profiles.py",
        "python tools/test_tariff_optimizer.py",
        "python tools/test_rcm_history.py",
        "python tools/test_rcm_optimizer.py",
        "python tools/test_diagnostic_analyzer.py",
        "python tools/test_optimizer_startup_contract.py",
        "python tools/test_automation_matrix.py",
        "python tools/test_automation_matrix.py --exhaustive",
    ):
        require(test_command in workflow_text, f"CI does not run {test_command}")
    require(
        "validate-hacs:" in workflow_text
        and "HACS validation" in workflow_text
        and "continue-on-error" not in workflow_text
        and "ignore:" not in workflow_text,
        "Official HACS validation must be mandatory and unignored",
    )

    print(f"HACS layout: OK ({len(integration_dirs)} integration)")
    print(f"Manifest: OK (version {manifest['version']})")
    print(f"Localized entities: {len(catalog)} (English and Polish)")
    print("Bundled dashboards/EMS assets: OK")
    print("HACS-visible user update instructions: OK")
    print("README screenshots: OK")
    print("Public ESPHome remote packages: OK")
    print("Python syntax: OK")
    print("Text-state localization: OK")
    print("Fresh PL/EN asset installation: OK")
    print("Brand assets: OK")
    print("MIT/OSI license and current license documentation: OK")
    print("Contribution rights, sign-off and CODEOWNERS: OK")
    print("RCE/tariff/RCEm CI regression matrix: OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
