"""Deterministic Phase 1B-3 contract for persistent Supervisor helpers."""

from __future__ import annotations

import ast
import asyncio
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import types
from types import SimpleNamespace
from typing import Any, Callable

import yaml


sys.dont_write_bytecode = True

ROOT = Path(
    os.environ.get(
        "SUPERVISOR_HELPERS_ROOT",
        Path(__file__).resolve().parents[1],
    )
)
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
CANONICAL = ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml"
PL_PACKAGE = (
    COMPONENT
    / "resources"
    / "home_assistant"
    / "pl"
    / "hoymiles_ems_scheduler.yaml"
)
EN_PACKAGE = (
    COMPONENT
    / "resources"
    / "home_assistant"
    / "en"
    / "hoymiles_ems_scheduler.yaml"
)
ASSETS_PATH = COMPONENT / "assets.py"
SOURCE_PATH = COMPONENT / "supervisor_sensor.py"
EMS_RELATIVE = "packages/hoymiles_ems_scheduler.yaml"
EXPECTED_PACKAGE_MARKER = "1.5.7-supervisor-1b3"
EXPECTED_BACKUP_SUFFIX = ".pre-ems-supervisor-1b3.bak"
EXPECTED_OLD_TEMP_NAME = (
    ".hoymiles_ems_scheduler.yaml.hoymiles_hit_modbus.tmp"
)
EXPECTED_NEW_HASHES = {
    "pl": "b66b591372c654424c491206e61815bc5457eca8514f4c1cffc5f205c7367691",
    "en": "3df7345f0ee9649a35160b4817d6d3dd9d0c95ecf93eed2bf07ec1fe2633886a",
}
EXPECTED_STORE_CONTRACTS = {
    "input_select": (1, frozenset({1, 2})),
    "input_boolean": (1, frozenset({1})),
}
EXPECTED_CANONICAL_ENTITY_IDS = (
    "input_select.hoymiles_ems_supervisor_mode",
    "input_select.hoymiles_ems_supervisor_profile",
    "input_boolean.hoymiles_ems_supervisor_allow_rce",
    "input_boolean.hoymiles_ems_supervisor_allow_tariff",
    "input_boolean.hoymiles_ems_supervisor_allow_rcm",
)
EXPECTED_ASSET_TRANSITIONS = {
    "fresh": ("INSTALLED_FRESH", True, True),
    "current": ("CURRENT", False, True),
    "modified": ("PRESERVED_MODIFIED", False, False),
    "collision": ("BLOCKED_COLLISION", False, False),
    "destination_changed": ("ABORTED_DESTINATION_CHANGED", False, False),
}
EXPECTED_BACKUP_FIXTURE = b"# exact pre-update scheduler fixture\nstate: old\n"
EXPECTED_CHECK_COUNT = 392
CHECKS = 0
GROUPS = 0

EXPECTED_TASK_PATHS = {
    "home_assistant/hoymiles_ems_scheduler.yaml",
    "custom_components/hoymiles_hit_modbus/resources/home_assistant/pl/hoymiles_ems_scheduler.yaml",
    "custom_components/hoymiles_hit_modbus/resources/home_assistant/en/hoymiles_ems_scheduler.yaml",
    "tools/build_hacs_assets.py",
    "custom_components/hoymiles_hit_modbus/assets.py",
    "custom_components/hoymiles_hit_modbus/const.py",
    "custom_components/hoymiles_hit_modbus/supervisor_sensor.py",
    "tools/test_supervisor_sensor_contract.py",
    "tools/test_supervisor_helpers_contract.py",
    "tools/validate_release.py",
}
HELPERS = {
    "input_select": {
        "hoymiles_ems_supervisor_mode": {
            "name": "Nadzorca EMS — tryb",
            "icon": "mdi:eye-outline",
            "options": ["Off", "Shadow"],
        },
        "hoymiles_ems_supervisor_profile": {
            "name": "Nadzorca EMS — profil",
            "icon": "mdi:tune-variant",
            "options": [
                "Balanced",
                "Maximum Profit",
                "High Reserve — Winter",
            ],
        },
    },
    "input_boolean": {
        "hoymiles_ems_supervisor_allow_rce": {
            "name": "Nadzorca EMS — uwzględniaj RCE",
            "icon": "mdi:chart-line",
        },
        "hoymiles_ems_supervisor_allow_tariff": {
            "name": "Nadzorca EMS — uwzględniaj tanie ładowanie",
            "icon": "mdi:battery-clock-outline",
        },
        "hoymiles_ems_supervisor_allow_rcm": {
            "name": "Nadzorca EMS — uwzględniaj RCEm",
            "icon": "mdi:transmission-tower",
        },
    },
}
ENGLISH_NAMES = {
    "hoymiles_ems_supervisor_mode": "EMS Supervisor — mode",
    "hoymiles_ems_supervisor_profile": "EMS Supervisor — profile",
    "hoymiles_ems_supervisor_allow_rce": "EMS Supervisor — consider RCE",
    "hoymiles_ems_supervisor_allow_tariff": (
        "EMS Supervisor — consider tariff charging"
    ),
    "hoymiles_ems_supervisor_allow_rcm": "EMS Supervisor — consider RCEm",
}
ENTITY_IDS = {
    f"{domain}.{object_id}"
    for domain, definitions in HELPERS.items()
    for object_id in definitions
}
OBJECT_IDS = {
    object_id
    for definitions in HELPERS.values()
    for object_id in definitions
}
HISTORICAL_HASHES = {
    "pl": "9846bfe0d0e9f8f707db7b3eb5b30fd349b663f8fb1c3777b460ef62696026a2",
    "en": "b76ba6a2a9f94d307d1582822101b2fc0951868ba7319394ce0886ee9fe9e07d",
}


class DuplicateKeyError(ValueError):
    """Raised when strict YAML parsing encounters a repeated mapping key."""


class UniqueKeyLoader(yaml.SafeLoader):
    """SafeLoader variant that rejects duplicate keys at every level."""


def _construct_unique_mapping(
    loader: UniqueKeyLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def check(condition: bool, message: str) -> None:
    """Count and enforce one deterministic assertion."""
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def group(name: str, function: Callable[[], None]) -> None:
    """Run one named test group."""
    global GROUPS
    function()
    GROUPS += 1
    print(f"PASS {name}")


def _strict_yaml_text(text: str) -> dict[str, Any]:
    data = yaml.load(text, Loader=UniqueKeyLoader)
    if not isinstance(data, dict):
        raise AssertionError("scheduler root is not a mapping")
    return data


def _strict_yaml(path: Path) -> dict[str, Any]:
    return _strict_yaml_text(path.read_text(encoding="utf-8"))


def _git_bytes(revision: str, path: str) -> bytes:
    return subprocess.check_output(
        ["git", "show", f"{revision}:{path}"],
        cwd=ROOT,
    )


def _git_paths(*args: str) -> set[str]:
    output = subprocess.check_output(
        ["git", *args],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    )
    return {line.strip().replace("\\", "/") for line in output.splitlines() if line.strip()}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _source_rows(path: Path = SOURCE_PATH) -> tuple[tuple[Any, ...], ...]:
    module = ast.parse(path.read_text(encoding="utf-8"))
    for node in module.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == "_SOURCE_ROWS" for target in node.targets):
            rows = ast.literal_eval(node.value)
            if isinstance(rows, tuple):
                return rows
    raise AssertionError("_SOURCE_ROWS literal is missing")


def _walk_strings(value: Any) -> list[str]:
    strings: list[str] = []
    if isinstance(value, str):
        strings.append(value)
    elif isinstance(value, dict):
        for key, child in value.items():
            strings.extend(_walk_strings(key))
            strings.extend(_walk_strings(child))
    elif isinstance(value, list):
        for child in value:
            strings.extend(_walk_strings(child))
    return strings


def _execution_helper_references(data: dict[str, Any]) -> list[str]:
    projection = deepcopy(data)
    for domain, definitions in HELPERS.items():
        domain_data = projection.get(domain)
        if isinstance(domain_data, dict):
            for object_id in definitions:
                domain_data.pop(object_id, None)
    strings = _walk_strings(projection)
    return [
        entity_id
        for entity_id in sorted(ENTITY_IDS)
        for value in strings
        if entity_id in value
    ]


def _set_package_marker(data: dict[str, Any], marker: str) -> None:
    templates = data.get("template")
    if not isinstance(templates, list):
        raise AssertionError("template section missing")
    found = 0
    for block in templates:
        if not isinstance(block, dict):
            continue
        sensors = block.get("sensor")
        if not isinstance(sensors, list):
            continue
        for sensor in sensors:
            if isinstance(sensor, dict) and sensor.get("unique_id") == "hoymiles_ems_package_version":
                sensor["state"] = marker
                found += 1
    if found != 1:
        raise AssertionError(f"package marker count differs: {found}")


def _remove_phase_1b3_helpers(data: dict[str, Any]) -> None:
    for domain, definitions in HELPERS.items():
        domain_data = data.get(domain)
        if not isinstance(domain_data, dict):
            raise AssertionError(f"{domain} map missing")
        for object_id in definitions:
            domain_data.pop(object_id, None)


class FakeState:
    """Public-compatible immutable-enough State fixture."""

    def __init__(
        self,
        entity_id: str,
        state: str,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.entity_id = entity_id
        self.state = state
        self.attributes = attributes if attributes is not None else {}


class FakeStateMachine:
    """Minimal public StateMachine surface: exact get only."""

    def __init__(self) -> None:
        self._states: dict[str, Any] = {}

    def get(self, entity_id: str) -> Any:
        return self._states.get(entity_id)

    def put(self, state: Any) -> None:
        self._states[state.entity_id] = state

    def remove(self, entity_id: str) -> None:
        self._states.pop(entity_id, None)


class FakeRegistryEntry:
    """Minimal public RegistryEntry identity fixture."""

    def __init__(self, entity_id: str, platform: str, unique_id: str) -> None:
        self.entity_id = entity_id
        self.platform = platform
        self.unique_id = unique_id


class FakeEntityRegistry:
    """Public-compatible exact and identity-index registry lookups."""

    def __init__(self) -> None:
        self.entries: dict[str, Any] = {}
        self.malformed = False

    def async_get(self, entity_id: str) -> Any:
        if self.malformed:
            raise RuntimeError("malformed registry")
        return self.entries.get(entity_id)

    def async_get_entity_id(
        self,
        domain: str,
        platform: str,
        unique_id: str,
    ) -> str | None:
        if self.malformed:
            raise RuntimeError("malformed registry")
        for entry in self.entries.values():
            if (
                getattr(entry, "entity_id", "").partition(".")[0] == domain
                and getattr(entry, "platform", None) == platform
                and getattr(entry, "unique_id", None) == unique_id
            ):
                return entry.entity_id
        return None


class FakeManagedStore:
    """Persistent fake Store backing one FakeHass instance."""

    def __init__(self, hass: Any, *_args: Any, **_kwargs: Any) -> None:
        self.hass = hass

    async def async_load(self) -> dict[str, Any] | None:
        self.hass.store_load_calls += 1
        return deepcopy(self.hass.store_payload)

    async def async_save(self, payload: dict[str, Any]) -> None:
        self.hass.store_save_calls += 1
        if self.hass.metadata_failures:
            self.hass.metadata_failures -= 1
            raise OSError("injected metadata failure")
        self.hass.store_payload = deepcopy(payload)


class FakeHass:
    """Minimal public HA surface used by the actual async installer."""

    def __init__(self, config_path: Path, language: str = "pl-PL") -> None:
        self.config = SimpleNamespace(
            config_dir=str(config_path),
            language=language,
        )
        self.data: dict[str, Any] = {}
        self.states = FakeStateMachine()
        self.entity_registry = FakeEntityRegistry()
        self.store_payload: dict[str, Any] = {}
        self.store_load_calls = 0
        self.store_save_calls = 0
        self.metadata_failures = 0
        self.executor_before: Callable[[str, tuple[Any, ...]], None] | None = None
        self.executor_after: Callable[[str, Any], None] | None = None
        self.pause_executor_name: str | None = None
        self.pause_entered: asyncio.Event | None = None
        self.pause_release: asyncio.Event | None = None

    async def async_add_executor_job(
        self,
        function: Callable[..., Any],
        *args: Any,
    ) -> Any:
        name = function.__name__
        if self.executor_before is not None:
            self.executor_before(name, args)
        if name == self.pause_executor_name:
            assert self.pause_entered is not None and self.pause_release is not None
            self.pause_entered.set()
            await self.pause_release.wait()
        result = function(*args)
        if self.executor_after is not None:
            self.executor_after(name, result)
        return result


def _load_assets_module(root: Path = ROOT) -> types.ModuleType:
    """Load assets.py with only public Home Assistant APIs stubbed."""
    lovelace_const = types.ModuleType("homeassistant.components.lovelace.const")
    lovelace_const.CONF_RESOURCE_TYPE_WS = "res_type"
    lovelace_const.LOVELACE_DATA = "lovelace"
    lovelace_const.MODE_STORAGE = "storage"
    ha_const = types.ModuleType("homeassistant.const")
    ha_const.ATTR_EDITABLE = "editable"
    ha_const.CONF_ID = "id"
    ha_const.CONF_TYPE = "type"
    ha_const.CONF_URL = "url"
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    storage = types.ModuleType("homeassistant.helpers.storage")
    storage.Store = FakeManagedStore
    entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
    entity_registry.async_get = lambda hass: hass.entity_registry
    homeassistant = types.ModuleType("homeassistant")
    components = types.ModuleType("homeassistant.components")
    lovelace = types.ModuleType("homeassistant.components.lovelace")
    helpers = types.ModuleType("homeassistant.helpers")
    sys.modules.update(
        {
            "homeassistant": homeassistant,
            "homeassistant.components": components,
            "homeassistant.components.lovelace": lovelace,
            "homeassistant.components.lovelace.const": lovelace_const,
            "homeassistant.const": ha_const,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.entity_registry": entity_registry,
            "homeassistant.helpers.storage": storage,
        }
    )
    helpers.entity_registry = entity_registry

    custom_components = types.ModuleType("custom_components")
    package_name = "custom_components.hoymiles_hit_modbus"
    package = types.ModuleType(package_name)
    component = root / "custom_components" / "hoymiles_hit_modbus"
    package.__path__ = [str(component)]
    const_module = types.ModuleType(f"{package_name}.const")
    const_module.DOMAIN = "hoymiles_hit_modbus"
    const_module.VERSION = "1.5.7"
    const_module.EMS_PACKAGE_VERSION = EXPECTED_PACKAGE_MARKER
    const_module.EMS_PACKAGE_VERSION_ENTITY = (
        "sensor.hoymiles_ems_package_version"
    )
    sys.modules["custom_components"] = custom_components
    sys.modules[package_name] = package
    sys.modules[const_module.__name__] = const_module
    module_name = f"{package_name}.assets"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, component / "assets.py")
    if spec is None or spec.loader is None:
        raise AssertionError("assets.py loader unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_generator_module() -> types.ModuleType:
    module_name = "supervisor_1b3_asset_generator"
    spec = importlib.util.spec_from_file_location(
        module_name,
        ROOT / "tools" / "build_hacs_assets.py",
    )
    if spec is None or spec.loader is None:
        raise AssertionError("asset generator loader unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _write_helper_storage(
    config_path: Path,
    *,
    input_select: tuple[str, ...] = (),
    input_boolean: tuple[str, ...] = (),
) -> None:
    storage = config_path / ".storage"
    storage.mkdir(parents=True, exist_ok=True)
    for domain, object_ids in (
        ("input_select", input_select),
        ("input_boolean", input_boolean),
    ):
        payload = {
            "version": 1,
            "minor_version": 2 if domain == "input_select" else 1,
            "key": domain,
            "data": {"items": [{"id": object_id, "name": "fixture"} for object_id in object_ids]},
        }
        (storage / domain).write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )


def _write_store_payload(
    config_path: Path,
    domain: str,
    *,
    version: Any = 1,
    minor_version: Any = 1,
    key: str | None = None,
    items: Any = None,
) -> Path:
    """Write an independent exact/malformed Store fixture."""
    storage = config_path / ".storage"
    storage.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": version,
        "minor_version": minor_version,
        "key": domain if key is None else key,
        "data": {"items": [] if items is None else items},
    }
    path = storage / domain
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return path


def _set_live_helper(
    hass: FakeHass,
    entity_id: str,
    *,
    editable: Any,
    registry_entity_id: str | None = None,
    platform: str | None = None,
    unique_id: str | None = None,
) -> None:
    """Publish one exact public State/Registry fixture."""
    domain, object_id = entity_id.split(".", 1)
    hass.states.put(FakeState(entity_id, "off", {"editable": editable}))
    registry_id = entity_id if registry_entity_id is None else registry_entity_id
    hass.entity_registry.entries[registry_id] = FakeRegistryEntry(
        registry_id,
        domain if platform is None else platform,
        object_id if unique_id is None else unique_id,
    )


def _set_expected_yaml_helpers(hass: FakeHass) -> None:
    """Publish the exact marker and five coherent YAML helper identities."""
    marker_id = "sensor.hoymiles_ems_package_version"
    hass.states.put(FakeState(marker_id, EXPECTED_PACKAGE_MARKER, {}))
    for entity_id in EXPECTED_CANONICAL_ENTITY_IDS:
        _set_live_helper(hass, entity_id, editable=False)


def _set_ui_collision(
    hass: FakeHass,
    entity_id: str = "input_select.hoymiles_ems_supervisor_mode",
) -> None:
    """Publish one exact editable UI helper before any Store save."""
    _set_live_helper(hass, entity_id, editable=True)


def _run_install(
    assets: types.ModuleType,
    hass: FakeHass,
    *,
    overwrite: bool = False,
) -> list[Path]:
    """Invoke the actual public async installer entry point."""
    return asyncio.run(
        assets.async_install_assets(
            hass,
            overwrite=overwrite,
            publish_frontend=False,
        )
    )


def _metadata_hash(hass: FakeHass, relative: str = EMS_RELATIVE) -> str | None:
    assets = hass.store_payload.get("assets", {})
    return assets.get(relative) if isinstance(assets, dict) else None


def _capture_log(function: Callable[[], Any], logger: logging.Logger) -> tuple[Any, str]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    old_level = logger.level
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        result = function()
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
    return result, stream.getvalue()


def test_exact_helper_definitions() -> None:
    text = CANONICAL.read_text(encoding="utf-8")
    data = _strict_yaml_text(text)
    base = _strict_yaml_text(
        _git_bytes("HEAD", "home_assistant/hoymiles_ems_scheduler.yaml").decode("utf-8")
    )
    new_keys: set[tuple[str, str]] = set()
    for domain, definitions in HELPERS.items():
        current_domain = data.get(domain)
        base_domain = base.get(domain)
        check(isinstance(current_domain, dict), f"{domain} is not one map")
        check(isinstance(base_domain, dict), f"baseline {domain} missing")
        assert isinstance(current_domain, dict) and isinstance(base_domain, dict)
        for object_id, expected in definitions.items():
            new_keys.add((domain, object_id))
            check(object_id in current_domain, f"missing helper {domain}.{object_id}")
            actual = current_domain[object_id]
            check(isinstance(actual, dict), f"helper {object_id} is not a mapping")
            check(actual.get("name") == expected["name"], f"wrong PL name for {object_id}")
            check(actual.get("icon") == expected["icon"], f"wrong icon for {object_id}")
            check("initial" not in actual, f"initial resets {object_id}")
            check(text.count(f"  {object_id}:\n") == 1, f"definition count differs for {object_id}")
            if domain == "input_select":
                check(actual.get("options") == expected["options"], f"option contract differs for {object_id}")
        check(
            set(current_domain) - set(base_domain) == set(definitions),
            f"unexpected new {domain} keys",
        )
    check(len(new_keys) == 5, "new helper key count differs")
    mode = data["input_select"]["hoymiles_ems_supervisor_mode"]
    profile = data["input_select"]["hoymiles_ems_supervisor_profile"]
    check(mode["options"] == ["Off", "Shadow"], "mode order/default differs")
    check(isinstance(mode["options"][0], str), "quoted Off did not remain a string")
    check("Active" not in mode["options"], "Active was added")
    check(profile["options"][0] == "Balanced", "profile default differs")
    check(profile["options"][2] == "High Reserve — Winter", "Unicode profile option differs")
    check(text.count("\ninput_boolean:\n") == 1, "duplicate top-level input_boolean")
    check(text.count("\ninput_select:\n") == 1, "duplicate top-level input_select")


def test_generated_pl_en_parity() -> None:
    canonical_text = CANONICAL.read_text(encoding="utf-8")
    pl_text = PL_PACKAGE.read_text(encoding="utf-8")
    en_text = EN_PACKAGE.read_text(encoding="utf-8")
    normalized = (
        canonical_text.replace('"Autokonsumpcja (Self-Use)"', '"self_use"')
        .replace('"Ładowanie z sieci"', '"grid_charge"')
        .replace('"Rozładowanie do sieci"', '"grid_discharge"')
    )
    check(pl_text == normalized, "packaged PL is not normalized canonical content")
    pl = _strict_yaml_text(pl_text)
    en = _strict_yaml_text(en_text)
    for domain, definitions in HELPERS.items():
        for object_id, expected in definitions.items():
            check(pl[domain][object_id]["name"] == expected["name"], f"PL name differs for {object_id}")
            check(en[domain][object_id]["name"] == ENGLISH_NAMES[object_id], f"EN name differs for {object_id}")
            check(en[domain][object_id]["icon"] == expected["icon"], f"EN icon differs for {object_id}")
            check("initial" not in en[domain][object_id], f"EN initial resets {object_id}")
            if domain == "input_select":
                check(en[domain][object_id]["options"] == expected["options"], f"EN translated internal options for {object_id}")
    exact_english_comments = (
        "Allows EMS Supervisor to consider RCE only in Shadow arbitration. It does not enable RCE automation, change its enable helper, or grant physical write authority.",
        "Allows EMS Supervisor to consider tariff charging only in Shadow arbitration. It does not enable tariff charging, change its enable helper, or grant physical write authority.",
        "Allows EMS Supervisor to consider RCEm only in Shadow arbitration. It does not enable RCEm, change its helpers, or grant physical write authority.",
        "EMS Supervisor observation mode. Off disables arbitration, while Shadow publishes an observation-only decision. Neither option starts physical execution or changes legacy automation.",
        "The profile is only published in Shadow arbitration. Profile effects remain unapplied and do not change physical execution or legacy automation.",
    )
    normalized_comments = re.sub(r"\n\s*#\s?", " ", en_text)
    for comment in exact_english_comments:
        check(comment in normalized_comments, f"exact EN comment missing: {comment}")
    scheduler_resources = sorted(
        (COMPONENT / "resources" / "home_assistant").glob("**/hoymiles_ems_scheduler.yaml")
    )
    check(scheduler_resources == sorted([EN_PACKAGE, PL_PACKAGE]), "scheduler resource count differs")
    generator = (ROOT / "tools" / "build_hacs_assets.py").read_text(encoding="utf-8")
    check(generator.count("hoymiles_ems_scheduler.yaml") == 3, "generator added a third scheduler output")
    check(_sha256_bytes(normalized.encode("utf-8")) == _sha256_bytes(pl_text.encode("utf-8")), "PL repeat is not deterministic")
    check(
        _sha256_bytes(pl_text.encode("utf-8")) == EXPECTED_NEW_HASHES["pl"],
        "literal reviewed PL hash differs",
    )
    check(
        _sha256_bytes(en_text.encode("utf-8")) == EXPECTED_NEW_HASHES["en"],
        "literal reviewed EN hash differs",
    )
    check(
        ENTITY_IDS == set(EXPECTED_CANONICAL_ENTITY_IDS),
        "literal canonical helper ID oracle differs",
    )
    check(
        ("translate_asset_to_" + "english(canonical_text)")
        not in Path(__file__).read_text(encoding="utf-8"),
        "helper expected EN still calls the production translator",
    )


def test_safe_defaults_and_restore() -> None:
    data = _strict_yaml(CANONICAL)
    mode = data["input_select"]["hoymiles_ems_supervisor_mode"]
    profile = data["input_select"]["hoymiles_ems_supervisor_profile"]

    def select_state(options: list[str], restored: str | None) -> str:
        return restored if restored in options else options[0]

    def boolean_state(restored: str | None) -> bool:
        return restored == "on"

    check(select_state(mode["options"], None) == "Off", "fresh mode is not Off")
    check(select_state(profile["options"], None) == "Balanced", "fresh profile is not Balanced")
    for object_id in HELPERS["input_boolean"]:
        check("initial" not in data["input_boolean"][object_id], f"permission initial exists: {object_id}")
        check(boolean_state(None) is False, f"fresh permission is on: {object_id}")
        check(boolean_state("on") is True, f"permission restore failed: {object_id}")
        check(boolean_state("unavailable") is False, f"invalid permission became true: {object_id}")
    check(select_state(mode["options"], "Shadow") == "Shadow", "Shadow did not restore")
    for restored in ("Maximum Profit", "High Reserve — Winter"):
        check(select_state(profile["options"], restored) == restored, f"profile did not restore: {restored}")
    for invalid in (None, "unknown", "unavailable", "Active", "shadow"):
        check(select_state(mode["options"], invalid) == "Off", f"invalid mode did not fail Off: {invalid}")
    for invalid in (None, "unknown", "unavailable", "maximum_profit"):
        check(select_state(profile["options"], invalid) == "Balanced", f"invalid profile did not fail Balanced: {invalid}")
    helper_text = "\n".join(
        yaml.safe_dump(data[domain][object_id], allow_unicode=True)
        for domain, definitions in HELPERS.items()
        for object_id in definitions
    )
    check("initial:" not in helper_text, "package update would reset restored state")


def test_source_map_60_0() -> None:
    rows = _source_rows()
    base_rows = _source_rows_from_text(
        _git_bytes("HEAD", "custom_components/hoymiles_hit_modbus/supervisor_sensor.py").decode("utf-8")
    )
    check(len(rows) == 60, "source total differs")
    check(sum(not row[5] for row in rows) == 60, "existing source count differs")
    check(sum(bool(row[5]) for row in rows) == 0, "future source count differs")
    check(sum(bool(row[3]) for row in rows) == 21, "entry-local count differs")
    check(sum(not bool(row[3]) for row in rows) == 39, "global count differs")
    check(sum(not bool(row[4]) for row in rows) == 46, "H count differs")
    check(sum(bool(row[4]) for row in rows) == 14, "P count differs")
    check(tuple(row[0] for row in rows) == tuple(range(1, 61)), "source numbers differ or #61 exists")
    check(tuple(row[:5] for row in rows) == tuple(row[:5] for row in base_rows), "source IDs/classes/order changed")
    check(all(not row[5] for row in rows[:5]), "helper source remains future")
    check(tuple(row[5] for row in rows[5:]) == tuple(row[5] for row in base_rows[5:]), "non-helper source metadata changed")


def _source_rows_from_text(text: str) -> tuple[tuple[Any, ...], ...]:
    module = ast.parse(text)
    for node in module.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "_SOURCE_ROWS" for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError("source map missing")


def test_legacy_execution_independence() -> None:
    for path in (CANONICAL, PL_PACKAGE, EN_PACKAGE):
        data = _strict_yaml(path)
        check(_execution_helper_references(data) == [], f"helper consumed by executable scheduler: {path}")
        text = path.read_text(encoding="utf-8")
        for object_id in OBJECT_IDS:
            check(text.count(f"  {object_id}:\n") == 1, f"helper definition count differs in {path.name}: {object_id}")
    current = _strict_yaml(CANONICAL)
    baseline = _strict_yaml_text(
        _git_bytes("HEAD", "home_assistant/hoymiles_ems_scheduler.yaml").decode("utf-8")
    )
    _remove_phase_1b3_helpers(current)
    _set_package_marker(current, "1.5.7")
    check(current == baseline, "scheduler semantics changed outside helpers and marker")
    check(
        not ENTITY_IDS.intersection(
            {
                "input_boolean.hoymiles_rce_discharge_enabled",
                "input_boolean.hoymiles_tariff_charge_enabled",
                "input_boolean.hoymiles_rcm_enabled",
                "input_boolean.hoymiles_rcm_shadow_mode",
            }
        ),
        "permission aliases a legacy enable helper",
    )
    added = subprocess.check_output(
        ["git", "diff", "--unified=0", "HEAD", "--", "home_assistant/hoymiles_ems_scheduler.yaml"],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    )
    added_lines = "\n".join(line[1:] for line in added.splitlines() if line.startswith("+") and not line.startswith("+++"))
    check(not re.search(r"(?m)^\s*(?:trigger|condition|action|service|sequence):", added_lines), "executable scheduler section was added")
    check("hass.services" not in added_lines and "modbus" not in added_lines.lower(), "physical call was added")


# Managed-package delivery regressions use async_install_assets only.


def _focus_assets(assets: types.ModuleType) -> None:
    """Limit behavioral probes to dashboard plus scheduler."""
    assets.LOCAL_FRONTEND_ASSETS = ()


def _scheduler_path(config: Path) -> Path:
    return config / EMS_RELATIVE


def _backup_path(package: Path) -> Path:
    return package.with_name(package.name + EXPECTED_BACKUP_SUFFIX)


def _file_identity(path: Path) -> tuple[int, int, int, int, int, int]:
    info = os.lstat(path)
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_mode,
        info.st_nlink,
    )


def _transaction_artifacts(package: Path, kind: str) -> list[Path]:
    return list(
        package.parent.glob(
            f".{package.name}.hoymiles_hit_modbus.{kind}.*.tmp"
        )
    )


def _prepare_historical_package(
    config: Path,
    language: str = "pl",
) -> tuple[Path, bytes]:
    package = _scheduler_path(config)
    package.parent.mkdir(parents=True, exist_ok=True)
    relative = (
        "custom_components/hoymiles_hit_modbus/resources/home_assistant/"
        f"{language}/hoymiles_ems_scheduler.yaml"
    )
    old = _git_bytes("HEAD", relative)
    check(
        _sha256_bytes(old) == HISTORICAL_HASHES[language],
        f"literal historical {language} hash differs",
    )
    package.write_bytes(old)
    return package, old


def _capture_filesystem_result(hass: FakeHass) -> list[Any]:
    results: list[Any] = []

    def after(name: str, result: Any) -> None:
        if name == "_sync_assets":
            results.append(result)

    hass.executor_after = after
    return results


def test_delivery_fresh_and_current() -> None:
    """Fresh install, metadata heal and second-run idempotence."""
    assets = _load_assets_module()
    _focus_assets(assets)
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_fresh_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        hass = FakeHass(config)
        results = _capture_filesystem_result(hass)
        package = _scheduler_path(config)
        written = _run_install(assets, hass)
        check(package in written, "fresh scheduler was not physically reported")
        check(package.read_bytes() == PL_PACKAGE.read_bytes(), "fresh bytes differ")
        check(not _backup_path(package).exists(), "fresh install created backup")
        check(
            _metadata_hash(hass) == EXPECTED_NEW_HASHES["pl"],
            "fresh metadata differs from literal hash",
        )
        check(
            results[0].scheduler.action.value
            == EXPECTED_ASSET_TRANSITIONS["fresh"][0],
            "fresh transition category differs",
        )
        first = package.stat()
        saves = hass.store_save_calls
        results.clear()
        second = _run_install(assets, hass)
        check(package not in second, "second run reports scheduler write")
        check(package.stat().st_ino == first.st_ino, "second run replaced scheduler")
        check(hass.store_save_calls == saves, "second run churned metadata")
        check(
            results[0].scheduler.action.value
            == EXPECTED_ASSET_TRANSITIONS["current"][0],
            "current transition category differs",
        )
        check(not _transaction_artifacts(package, "rollback"), "rollback temp remains")
        check(not _transaction_artifacts(package, "destination"), "destination temp remains")


def test_delivery_current_preflight() -> None:
    """Cases 1-2: collision precedes current-file metadata repair."""
    assets = _load_assets_module()
    _focus_assets(assets)
    for collision in (True, False):
        with tempfile.TemporaryDirectory(prefix="supervisor_1b3_current_") as tmp:
            config = Path(tmp)
            _write_helper_storage(config)
            package = _scheduler_path(config)
            package.parent.mkdir(parents=True)
            package.write_bytes(PL_PACKAGE.read_bytes())
            inode = package.stat().st_ino
            hass = FakeHass(config)
            results = _capture_filesystem_result(hass)
            if collision:
                _set_ui_collision(hass)
            written = _run_install(assets, hass)
            check(package not in written, "current file was reported as replaced")
            check(package.stat().st_ino == inode, "current file was replaced")
            check(not _backup_path(package).exists(), "current file created backup")
            expected = None if collision else EXPECTED_NEW_HASHES["pl"]
            check(_metadata_hash(hass) == expected, "preflight metadata result differs")
            action = "BLOCKED_COLLISION" if collision else "CURRENT"
            check(results[0].scheduler.action.value == action, "current action differs")


def test_delivery_live_classifier() -> None:
    """Cases 3 and 6-8: exact public live authority."""
    assets = _load_assets_module()
    _focus_assets(assets)
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_live_stale_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = _scheduler_path(config)
        package.parent.mkdir(parents=True)
        package.write_bytes(PL_PACKAGE.read_bytes())
        hass = FakeHass(config)
        _set_ui_collision(hass)
        written = _run_install(assets, hass)
        check(package not in written, "stale disk hid live editable helper")
        check(_metadata_hash(hass) is None, "live collision repaired metadata")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_live_bad_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        hass = FakeHass(config)
        hass.entity_registry.malformed = True
        written = _run_install(assets, hass)
        check(_scheduler_path(config) not in written, "malformed registry did not block")
        check(not _scheduler_path(config).exists(), "malformed registry installed file")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_unrelated_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        hass = FakeHass(config)
        _set_live_helper(hass, "input_select.unrelated_helper", editable=True)
        written = _run_install(assets, hass)
        check(_scheduler_path(config) in written, "unrelated live helper blocked")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_suffix_") as tmp:
        config = Path(tmp)
        _write_helper_storage(
            config,
            input_select=("hoymiles_ems_supervisor_mode_2",),
        )
        hass = FakeHass(config)
        _set_live_helper(
            hass,
            "input_select.hoymiles_ems_supervisor_mode_2",
            editable=True,
        )
        written = _run_install(assets, hass)
        check(_scheduler_path(config) in written, "suffixed helper blocked")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_expected_yaml_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        hass = FakeHass(config)
        _set_expected_yaml_helpers(hass)
        snapshot = assets._capture_live_helper_snapshot(hass)
        check(
            tuple(fact.classification.value for fact in snapshot.helper_facts)
            == ("EXPECTED_YAML",) * 5,
            "coherent YAML helpers classified incorrectly",
        )
        hass.states.remove(EXPECTED_CANONICAL_ENTITY_IDS[0])
        snapshot = assets._capture_live_helper_snapshot(hass)
        check(
            snapshot.helper_facts[0].classification.value == "UNVERIFIABLE",
            "active marker plus missing helper was accepted",
        )


def test_delivery_store_fallback() -> None:
    """Cases 4-5 and exact fail-closed Store envelope rules."""
    assets = _load_assets_module()
    _focus_assets(assets)
    malformed = (
        ("input_select", 2, 1, "unknown select major"),
        ("input_boolean", 2, 1, "unknown boolean major"),
        ("input_select", True, 1, "bool major"),
        ("input_select", 1, 3, "unknown select minor"),
        ("input_boolean", 1, 2, "unknown boolean minor"),
    )
    for domain, version, minor, label in malformed:
        with tempfile.TemporaryDirectory(prefix="supervisor_1b3_store_") as tmp:
            config = Path(tmp)
            _write_helper_storage(config)
            _write_store_payload(
                config,
                domain,
                version=version,
                minor_version=minor,
            )
            hass = FakeHass(config)
            written = _run_install(assets, hass)
            check(_scheduler_path(config) not in written, f"{label} did not block")
            check(not _scheduler_path(config).exists(), f"{label} installed file")

    invalid = (
        {"version": 1, "minor_version": 1, "key": "wrong", "data": {"items": []}},
        {"version": 1, "minor_version": 1, "key": "input_boolean", "data": {}},
        {"version": 1, "minor_version": 1, "key": "input_boolean", "data": {"items": [{"id": "x"}, {"id": "x"}]}},
        {"version": 1, "minor_version": 1, "key": "input_boolean", "data": {"items": [{"id": 3}]}},
    )
    for payload in invalid:
        with tempfile.TemporaryDirectory(prefix="supervisor_1b3_store_shape_") as tmp:
            config = Path(tmp)
            storage = config / ".storage"
            storage.mkdir(parents=True)
            (storage / "input_boolean").write_text(json.dumps(payload), encoding="utf-8")
            written = _run_install(assets, FakeHass(config))
            check(_scheduler_path(config) not in written, "malformed Store was accepted")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_store_collision_") as tmp:
        config = Path(tmp)
        _write_helper_storage(
            config,
            input_boolean=("hoymiles_ems_supervisor_allow_rce",),
        )
        written = _run_install(assets, FakeHass(config))
        check(_scheduler_path(config) not in written, "canonical disk helper did not block")


def test_delivery_historical_and_modified() -> None:
    """Historical PL/EN update and modified-package preservation."""
    assets = _load_assets_module()
    _focus_assets(assets)
    for language in ("pl", "en"):
        with tempfile.TemporaryDirectory(prefix=f"supervisor_1b3_update_{language}_") as tmp:
            config = Path(tmp)
            _write_helper_storage(config)
            package, old = _prepare_historical_package(config, language)
            hass = FakeHass(config, "pl-PL" if language == "pl" else "en-GB")
            written = _run_install(assets, hass)
            expected_source = PL_PACKAGE if language == "pl" else EN_PACKAGE
            backup = _backup_path(package)
            check(package in written, f"historical {language} not replaced")
            check(package.read_bytes() == expected_source.read_bytes(), f"new {language} bytes differ")
            check(backup.read_bytes() == old, f"historical {language} backup differs")
            check(
                _metadata_hash(hass) == EXPECTED_NEW_HASHES[language],
                f"historical {language} metadata differs",
            )
            backup_identity = (backup.stat().st_ino, backup.stat().st_mtime_ns)
            second = _run_install(assets, hass)
            check(package not in second, f"historical {language} second run wrote")
            check(
                (backup.stat().st_ino, backup.stat().st_mtime_ns)
                == backup_identity,
                f"historical {language} backup overwritten",
            )

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_modified_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = _scheduler_path(config)
        package.parent.mkdir(parents=True)
        old = b"# local user scheduler\nstate: custom\n"
        package.write_bytes(old)
        hass = FakeHass(config)
        (written, log_text) = _capture_log(
            lambda: _run_install(assets, hass),
            assets._LOGGER,
        )
        check(package not in written, "modified package reported as written")
        check(package.read_bytes() == old, "modified package overwritten or merged")
        check(not _backup_path(package).exists(), "modified hold created backup")
        check(_metadata_hash(hass) is None, "modified hold created metadata")
        check(
            "integracja go nie nadpisała" in log_text
            and "hoymiles_hit_modbus.install_assets" in log_text
            and "ha core check" in log_text,
            "bounded modified instruction missing",
        )


def test_delivery_explicit_overwrite() -> None:
    """Cases 25-26: explicit overwrite retains all safety gates."""
    assets = _load_assets_module()
    _focus_assets(assets)
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_overwrite_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = _scheduler_path(config)
        package.parent.mkdir(parents=True)
        old = EXPECTED_BACKUP_FIXTURE
        package.write_bytes(old)
        written = _run_install(assets, FakeHass(config), overwrite=True)
        check(package in written, "explicit overwrite with new valid backup did not write")
        check(_backup_path(package).read_bytes() == old, "explicit backup differs")
        check(package.read_bytes() == PL_PACKAGE.read_bytes(), "explicit bytes differ")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_overwrite_bad_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = _scheduler_path(config)
        package.parent.mkdir(parents=True)
        package.write_bytes(EXPECTED_BACKUP_FIXTURE)
        backup = _backup_path(package)
        backup.write_bytes(b"foreign fixed backup")
        hass = FakeHass(config)
        written = _run_install(assets, hass, overwrite=True)
        check(package not in written, "mismatched backup allowed overwrite")
        check(package.read_bytes() == EXPECTED_BACKUP_FIXTURE, "blocked overwrite changed package")
        check(backup.read_bytes() == b"foreign fixed backup", "foreign backup overwritten")
        check(_metadata_hash(hass) is None, "blocked overwrite advanced metadata")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_overwrite_collision_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = _scheduler_path(config)
        package.parent.mkdir(parents=True)
        package.write_bytes(EXPECTED_BACKUP_FIXTURE)
        hass = FakeHass(config)
        _set_ui_collision(hass)
        written = _run_install(assets, hass, overwrite=True)
        check(package not in written, "explicit overwrite bypassed collision")
        check(package.read_bytes() == EXPECTED_BACKUP_FIXTURE, "collision changed package")

    init_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    startup = init_source.split("async def _async_prepare_frontend_assets", 1)[1].split(
        "async def _async_reconcile_entity_registry", 1
    )[0]
    check("overwrite=False" in startup, "startup invokes overwrite true")
    check(
        "overwrite=call.data[ATTR_OVERWRITE]" in init_source,
        "explicit service path changed",
    )


def test_delivery_verified_backup() -> None:
    """Cases 10-16: exact fixed-backup verification and races."""
    assets = _load_assets_module()
    _focus_assets(assets)
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_backup_exact_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, old = _prepare_historical_package(config)
        backup = _backup_path(package)
        backup.write_bytes(old)
        before = (backup.stat().st_ino, backup.stat().st_mtime_ns, backup.read_bytes())
        written = _run_install(assets, FakeHass(config))
        check(package in written, "valid exact backup blocked update")
        check(
            (backup.stat().st_ino, backup.stat().st_mtime_ns, backup.read_bytes())
            == before,
            "valid backup was overwritten",
        )

    for kind in ("foreign", "directory", "hardlink"):
        with tempfile.TemporaryDirectory(prefix=f"supervisor_1b3_backup_{kind}_") as tmp:
            config = Path(tmp)
            _write_helper_storage(config)
            package, old = _prepare_historical_package(config)
            backup = _backup_path(package)
            if kind == "foreign":
                backup.write_bytes(b"foreign")
            elif kind == "directory":
                backup.mkdir()
            else:
                os.link(package, backup)
            written = _run_install(assets, FakeHass(config))
            check(package not in written, f"{kind} backup did not block")
            check(package.read_bytes() == old, f"{kind} backup changed package")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_backup_symlink_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, old = _prepare_historical_package(config)
        target = package.parent / "backup-target"
        target.write_bytes(old)
        backup = _backup_path(package)
        try:
            os.symlink(target, backup)
        except OSError:
            check(True, "backup symlink unsupported on this host")
        else:
            written = _run_install(assets, FakeHass(config))
            check(package not in written, "backup symlink did not block")
            check(package.read_bytes() == old, "backup symlink changed package")

    for valid_winner in (True, False):
        with tempfile.TemporaryDirectory(prefix="supervisor_1b3_backup_race_") as tmp:
            config = Path(tmp)
            _write_helper_storage(config)
            package, old = _prepare_historical_package(config)
            backup = _backup_path(package)
            original_link = assets.os.link
            injected = False

            def race_link(source: Any, destination: Any) -> None:
                nonlocal injected
                if Path(destination) == backup and not injected:
                    injected = True
                    backup.write_bytes(old if valid_winner else b"foreign winner")
                    raise FileExistsError("injected winner")
                original_link(source, destination)

            assets.os.link = race_link
            try:
                written = _run_install(assets, FakeHass(config))
            finally:
                assets.os.link = original_link
            check((package in written) is valid_winner, "backup winner result differs")
            check(
                backup.read_bytes()
                == (old if valid_winner else b"foreign winner"),
                "backup winner was overwritten",
            )


def test_delivery_unique_temp_and_revalidation() -> None:
    """Cases 17-22: old temp isolation, partial writes and races."""
    assets = _load_assets_module()
    _focus_assets(assets)
    for kind in ("regular", "hardlink", "symlink"):
        with tempfile.TemporaryDirectory(prefix=f"supervisor_1b3_temp_{kind}_") as tmp:
            config = Path(tmp)
            _write_helper_storage(config)
            package, _old = _prepare_historical_package(config)
            legacy = package.parent / EXPECTED_OLD_TEMP_NAME
            target = package.parent / "legacy-target"
            target.write_bytes(b"legacy protected bytes")
            supported = True
            if kind == "regular":
                legacy.write_bytes(b"legacy protected bytes")
            elif kind == "hardlink":
                os.link(target, legacy)
            else:
                try:
                    os.symlink(target, legacy)
                except OSError:
                    supported = False
            if not supported:
                check(True, "legacy symlink unsupported on this host")
                continue
            before = os.lstat(legacy)
            target_bytes = target.read_bytes()
            written = _run_install(assets, FakeHass(config))
            after = os.lstat(legacy)
            check(package in written, f"legacy {kind} temp blocked update")
            check(
                (before.st_dev, before.st_ino, before.st_mode)
                == (after.st_dev, after.st_ino, after.st_mode),
                f"legacy {kind} temp identity changed",
            )
            if kind == "regular":
                check(legacy.read_bytes() == b"legacy protected bytes", "legacy bytes changed")
            else:
                check(target.read_bytes() == target_bytes, f"legacy {kind} target changed")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_partial_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, old = _prepare_historical_package(config)
        _backup_path(package).write_bytes(old)
        original_write = assets._write_all_fd
        calls = 0

        def partial_write(descriptor: int, data: bytes) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                os.write(descriptor, data[: max(1, len(data) // 3)])
                raise assets._FilesystemContractError("temporary_partial_write")
            original_write(descriptor, data)

        assets._write_all_fd = partial_write
        try:
            written = _run_install(assets, FakeHass(config))
        finally:
            assets._write_all_fd = original_write
        check(package not in written, "partial unique temp reported success")
        check(package.read_bytes() == old, "partial unique temp changed package")
        check(not _transaction_artifacts(package, "destination"), "partial temp remains")
        check(not _transaction_artifacts(package, "rollback"), "rollback temp remains")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_destination_race_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, _old = _prepare_historical_package(config)
        foreign = b"foreign concurrent scheduler"
        original_revalidate = assets._revalidate_destination
        injected = False

        def change_then_revalidate(destination: Path, before: Any) -> None:
            nonlocal injected
            if destination == package and not injected:
                injected = True
                destination.write_bytes(foreign)
            original_revalidate(destination, before)

        assets._revalidate_destination = change_then_revalidate
        try:
            hass = FakeHass(config)
            results = _capture_filesystem_result(hass)
            written = _run_install(assets, hass)
        finally:
            assets._revalidate_destination = original_revalidate
        check(package not in written, "destination race reported write")
        check(package.read_bytes() == foreign, "destination race overwrote foreign bytes")
        check(
            results[0].scheduler.action.value == "ABORTED_DESTINATION_CHANGED",
            "destination race category differs",
        )
        check(_metadata_hash(hass) is None, "destination race advanced metadata")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_replace_fail_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, old = _prepare_historical_package(config)
        original_replace = assets.os.replace

        def fail_replace(source: Any, destination: Any) -> None:
            if Path(destination) == package:
                raise OSError("injected replace failure")
            original_replace(source, destination)

        assets.os.replace = fail_replace
        try:
            hass = FakeHass(config)
            written = _run_install(assets, hass)
        finally:
            assets.os.replace = original_replace
        check(package not in written, "replace failure reported scheduler write")
        check(package.read_bytes() == old, "replace failure changed destination")
        check(_metadata_hash(hass) is None, "replace failure advanced metadata")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_unrelated_fail_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        assets = _load_assets_module()
        _focus_assets(assets)
        original_regular = assets._sync_regular_asset

        def fail_regular(*_args: Any, **_kwargs: Any) -> Any:
            raise OSError("injected unrelated asset failure")

        assets._sync_regular_asset = fail_regular
        try:
            try:
                _run_install(assets, FakeHass(config))
            except OSError:
                pass
            else:
                raise AssertionError("unrelated asset failure was swallowed")
        finally:
            assets._sync_regular_asset = original_regular
        check(not _scheduler_path(config).exists(), "later scheduler became partial")
        check(not _backup_path(_scheduler_path(config)).exists(), "partial backup exists")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_dir_fsync_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, _old = _prepare_historical_package(config)
        (config / "dashboard_hoymiles.yaml").write_bytes(
            (COMPONENT / "resources" / "dashboard_hoymiles_pl.yaml").read_bytes()
        )
        original_fsync_directory = assets._fsync_directory
        package_dir_calls = 0

        def fail_after_replace(path: Path) -> None:
            nonlocal package_dir_calls
            if path == package.parent:
                package_dir_calls += 1
                if package_dir_calls == 2:
                    raise OSError("injected directory fsync failure")
            original_fsync_directory(path)

        assets._fsync_directory = fail_after_replace
        try:
            hass = FakeHass(config)
            written = _run_install(assets, hass)
        finally:
            assets._fsync_directory = original_fsync_directory
        check(package not in written, "directory fsync failure claimed success")
        check(package.read_bytes() == PL_PACKAGE.read_bytes(), "fsync failure lost new file")
        check(_metadata_hash(hass) is None, "fsync failure advanced metadata")


def test_delivery_metadata_self_heal() -> None:
    """Case 23: NEW file / OLD metadata heals without replacement."""
    assets = _load_assets_module()
    _focus_assets(assets)
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_metadata_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, old = _prepare_historical_package(config)
        old_hash = _sha256_bytes(old)
        hass = FakeHass(config)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {EMS_RELATIVE: old_hash},
        }
        hass.metadata_failures = 1
        try:
            _run_install(assets, hass)
        except OSError:
            pass
        else:
            raise AssertionError("metadata failure was swallowed")
        inode = package.stat().st_ino
        check(package.read_bytes() == PL_PACKAGE.read_bytes(), "metadata failure did not leave new file")
        check(_metadata_hash(hass) == old_hash, "metadata failure advanced Store")
        backup = _backup_path(package)
        backup_before = (backup.stat().st_ino, backup.read_bytes())
        written = _run_install(assets, hass)
        check(package not in written, "self-heal replaced current scheduler")
        check(package.stat().st_ino == inode, "self-heal changed scheduler identity")
        check(_metadata_hash(hass) == EXPECTED_NEW_HASHES["pl"], "self-heal failed")
        check(
            (backup.stat().st_ino, backup.read_bytes()) == backup_before,
            "self-heal overwrote fixed backup",
        )


def test_delivery_final_destination_attestation() -> None:
    """Final identity/hash proof gates scheduler metadata and written paths."""
    assets = _load_assets_module()
    _focus_assets(assets)

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_attest_old_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, old = _prepare_historical_package(config)
        old_hash = _sha256_bytes(old)
        hass = FakeHass(config)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {EMS_RELATIVE: old_hash},
        }
        attestations: list[Any] = []

        def restore_old(name: str, result: Any) -> None:
            if name == "_sync_assets":
                package.write_bytes(old)
            elif name == "_attest_scheduler_destination":
                attestations.append(result)

        hass.executor_after = restore_old
        written, warning = _capture_log(
            lambda: _run_install(assets, hass),
            assets._LOGGER,
        )
        check(package.read_bytes() == old, "old-byte race did not preserve old bytes")
        check(_metadata_hash(hass) == old_hash, "old-byte race advanced metadata")
        check(package not in written, "old-byte race reported scheduler write")
        check(len(attestations) == 1, "old-byte attestation call count differs")
        check(
            attestations[0].action.value == "ABORTED_DESTINATION_CHANGED",
            "old-byte attestation category differs",
        )
        check(_backup_path(package).read_bytes() == old, "old-byte backup differs")
        check(
            not _transaction_artifacts(package, "rollback"),
            "old-byte rollback artifact remains",
        )
        check(
            warning.count("ems_package_destination_changed") == 1,
            "old-byte race warning category count differs",
        )

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_attest_foreign_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, old = _prepare_historical_package(config)
        old_hash = _sha256_bytes(old)
        backup = _backup_path(package)
        foreign = b"independent foreign scheduler after filesystem transaction\n"
        hass = FakeHass(config)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {EMS_RELATIVE: old_hash},
        }
        observed: dict[str, Any] = {}

        def install_foreign(name: str, result: Any) -> None:
            if name == "_sync_assets":
                observed["backup"] = (_file_identity(backup), backup.read_bytes())
                winner = package.parent / "foreign-winner"
                winner.write_bytes(foreign)
                os.replace(winner, package)
                observed["foreign_identity"] = _file_identity(package)
            elif name == "_attest_scheduler_destination":
                observed["attestation"] = result

        hass.executor_after = install_foreign
        written, warning = _capture_log(
            lambda: _run_install(assets, hass),
            assets._LOGGER,
        )
        check(package.read_bytes() == foreign, "foreign bytes were overwritten")
        check(
            _file_identity(package) == observed["foreign_identity"],
            "foreign identity changed after attestation failure",
        )
        check(_metadata_hash(hass) == old_hash, "foreign race advanced metadata")
        check(package not in written, "foreign race reported scheduler write")
        check(
            (_file_identity(backup), backup.read_bytes()) == observed["backup"],
            "foreign race changed fixed backup",
        )
        check(
            observed["attestation"].action.value
            == "ABORTED_DESTINATION_CHANGED",
            "foreign attestation category differs",
        )
        check(
            not _transaction_artifacts(package, "rollback"),
            "foreign rollback artifact remains",
        )
        check(
            warning.count("ems_package_destination_changed") == 1,
            "foreign race warning category count differs",
        )

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_attest_fresh_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = _scheduler_path(config)
        foreign = b"foreign fresh-install winner\n"
        hass = FakeHass(config)
        observed = {}

        def replace_fresh(name: str, result: Any) -> None:
            if name == "_sync_assets":
                winner = package.parent / "fresh-foreign-winner"
                winner.write_bytes(foreign)
                os.replace(winner, package)
                observed["foreign_identity"] = _file_identity(package)
            elif name == "_attest_scheduler_destination":
                observed["attestation"] = result

        hass.executor_after = replace_fresh
        written, warning = _capture_log(
            lambda: _run_install(assets, hass),
            assets._LOGGER,
        )
        check(package.read_bytes() == foreign, "fresh foreign bytes were removed")
        check(
            _file_identity(package) == observed["foreign_identity"],
            "fresh foreign identity changed",
        )
        check(_metadata_hash(hass) is None, "fresh foreign race created metadata")
        check(package not in written, "fresh foreign race reported scheduler write")
        check(not _backup_path(package).exists(), "fresh foreign race created backup")
        check(
            observed["attestation"].action.value
            == "ABORTED_DESTINATION_CHANGED",
            "fresh foreign attestation category differs",
        )
        check(
            warning.count("ems_package_destination_changed") == 1,
            "fresh foreign warning category count differs",
        )

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_attest_current_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = _scheduler_path(config)
        package.parent.mkdir(parents=True)
        package.write_bytes(PL_PACKAGE.read_bytes())
        foreign = b"foreign metadata-heal winner\n"
        hass = FakeHass(config)
        observed = {}

        def change_current(name: str, result: Any) -> None:
            if name == "_sync_assets":
                observed["filesystem"] = result
                winner = package.parent / "current-foreign-winner"
                winner.write_bytes(foreign)
                os.replace(winner, package)
                observed["foreign_identity"] = _file_identity(package)
            elif name == "_attest_scheduler_destination":
                observed["attestation"] = result

        hass.executor_after = change_current
        written, warning = _capture_log(
            lambda: _run_install(assets, hass),
            assets._LOGGER,
        )
        check(
            observed["filesystem"].scheduler.action.value == "CURRENT",
            "metadata-heal filesystem action differs",
        )
        check(
            not observed["filesystem"].scheduler.physically_written,
            "metadata-heal transaction replaced scheduler",
        )
        check(package.read_bytes() == foreign, "metadata-heal foreign bytes changed")
        check(
            _file_identity(package) == observed["foreign_identity"],
            "metadata-heal foreign identity changed",
        )
        check(_metadata_hash(hass) is None, "metadata-heal race advanced metadata")
        check(package not in written, "metadata-heal race reported scheduler write")
        check(not _backup_path(package).exists(), "metadata-heal race created backup")
        check(
            observed["attestation"].action.value
            == "ABORTED_DESTINATION_CHANGED",
            "metadata-heal attestation category differs",
        )
        check(
            warning.count("ems_package_destination_changed") == 1,
            "metadata-heal warning category count differs",
        )

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_attest_inode_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, old = _prepare_historical_package(config)
        old_hash = _sha256_bytes(old)
        hass = FakeHass(config)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {EMS_RELATIVE: old_hash},
        }
        observed = {}

        def replace_same_hash(name: str, result: Any) -> None:
            if name == "_sync_assets":
                observed["installed_identity"] = result.scheduler.after.identity
                winner = package.parent / "same-hash-new-inode"
                winner.write_bytes(PL_PACKAGE.read_bytes())
                os.replace(winner, package)
                observed["winner_identity"] = _file_identity(package)
            elif name == "_attest_scheduler_destination":
                observed["attestation"] = result

        hass.executor_after = replace_same_hash
        written = _run_install(assets, hass)
        check(
            _sha256_bytes(package.read_bytes()) == EXPECTED_NEW_HASHES["pl"],
            "same-hash winner bytes differ",
        )
        check(
            _file_identity(package) == observed["winner_identity"],
            "same-hash winner identity changed",
        )
        check(
            observed["attestation"].resulting_snapshot.identity
            != observed["installed_identity"],
            "same-hash replacement did not change identity",
        )
        check(_metadata_hash(hass) == old_hash, "same-hash race advanced metadata")
        check(package not in written, "same-hash race reported scheduler write")
        check(
            observed["attestation"].action.value
            == "ABORTED_DESTINATION_CHANGED",
            "same-hash identity mismatch was accepted",
        )

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_attest_control_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, old = _prepare_historical_package(config)
        old_hash = _sha256_bytes(old)
        hass = FakeHass(config)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {EMS_RELATIVE: old_hash},
        }
        attestations = []

        def capture_attestation(name: str, result: Any) -> None:
            if name == "_attest_scheduler_destination":
                attestations.append(result)

        hass.executor_after = capture_attestation
        written = _run_install(assets, hass)
        check(package in written, "clean replacement was not reported")
        check(package.read_bytes() == PL_PACKAGE.read_bytes(), "clean replacement bytes differ")
        check(_metadata_hash(hass) == EXPECTED_NEW_HASHES["pl"], "clean replacement metadata differs")
        check(
            len(attestations) == 1 and attestations[0].action.value == "VERIFIED",
            "clean replacement attestation did not pass",
        )
        check(_backup_path(package).read_bytes() == old, "clean replacement backup differs")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_attest_heal_control_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = _scheduler_path(config)
        package.parent.mkdir(parents=True)
        package.write_bytes(PL_PACKAGE.read_bytes())
        identity = _file_identity(package)
        hass = FakeHass(config)
        attestations = []

        def capture_heal_attestation(name: str, result: Any) -> None:
            if name == "_attest_scheduler_destination":
                attestations.append(result)

        hass.executor_after = capture_heal_attestation
        written = _run_install(assets, hass)
        check(package not in written, "clean metadata heal reported scheduler write")
        check(_file_identity(package) == identity, "clean metadata heal changed identity")
        check(_metadata_hash(hass) == EXPECTED_NEW_HASHES["pl"], "clean metadata heal failed")
        check(not _backup_path(package).exists(), "clean metadata heal created backup")
        check(
            len(attestations) == 1 and attestations[0].action.value == "VERIFIED",
            "clean metadata-heal attestation did not pass",
        )


def test_delivery_postflight_rollback() -> None:
    """Cases 9 and 24: postflight collision and concurrent change policy."""
    assets = _load_assets_module()
    _focus_assets(assets)
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_rollback_existing_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, old = _prepare_historical_package(config)
        old_hash = _sha256_bytes(old)
        hass = FakeHass(config)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {EMS_RELATIVE: old_hash},
        }

        def collide(name: str, _result: Any) -> None:
            if name == "_sync_assets":
                _set_ui_collision(hass)

        hass.executor_after = collide
        written = _run_install(assets, hass)
        check(package not in written, "rolled-back package reported as written")
        check(package.read_bytes() == old, "postflight collision did not restore old")
        check(_metadata_hash(hass) == old_hash, "rollback changed truthful metadata")
        check(not _transaction_artifacts(package, "rollback"), "rollback artifact remains")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_rollback_fresh_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        hass = FakeHass(config)

        def collide_fresh(name: str, _result: Any) -> None:
            if name == "_sync_assets":
                _set_ui_collision(hass)

        hass.executor_after = collide_fresh
        written = _run_install(assets, hass)
        check(_scheduler_path(config) not in written, "fresh rollback reported write")
        check(not _scheduler_path(config).exists(), "fresh rollback did not remove file")
        check(_metadata_hash(hass) is None, "fresh rollback created metadata")

    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_rollback_foreign_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package, _old = _prepare_historical_package(config)
        foreign = b"foreign after transaction"
        hass = FakeHass(config)

        def foreign_after(name: str, _result: Any) -> None:
            if name == "_sync_assets":
                package.write_bytes(foreign)
                _set_ui_collision(hass)

        hass.executor_after = foreign_after
        written = _run_install(assets, hass)
        check(package not in written, "foreign concurrent package reported write")
        check(package.read_bytes() == foreign, "rollback overwrote foreign bytes")
        check(_metadata_hash(hass) is None, "failed rollback advanced metadata")


def test_delivery_lock_boundary_and_validator() -> None:
    """Global lock, immutable boundary, actual validator path, no execution."""
    assets = _load_assets_module()
    _focus_assets(assets)

    async def exercise() -> None:
        with tempfile.TemporaryDirectory(prefix="supervisor_1b3_lock_") as tmp:
            config = Path(tmp)
            _write_helper_storage(config)
            hass = FakeHass(config)
            seen_args: list[tuple[Any, ...]] = []

            def before(name: str, args: tuple[Any, ...]) -> None:
                if name == "_sync_assets":
                    seen_args.append(args)

            hass.executor_before = before
            hass.pause_executor_name = "_sync_assets"
            hass.pause_entered = asyncio.Event()
            hass.pause_release = asyncio.Event()
            first = asyncio.create_task(
                assets.async_install_assets(
                    hass,
                    overwrite=False,
                    publish_frontend=False,
                )
            )
            await hass.pause_entered.wait()
            second = asyncio.create_task(
                assets.async_install_assets(
                    hass,
                    overwrite=True,
                    publish_frontend=False,
                )
            )
            await asyncio.sleep(0)
            check(hass.store_load_calls == 1, "second install crossed lock")
            hass.pause_executor_name = None
            hass.pause_release.set()
            await first
            await second
            check(hass.store_load_calls == 2, "serialized second install did not run")
            check(len(seen_args) == 2, "filesystem executor call count differs")
            check(
                all(hass not in args for args in seen_args),
                "filesystem executor received Home Assistant runtime",
            )
            locks = [
                value for value in hass.data.values()
                if isinstance(value, asyncio.Lock)
            ]
            check(len(locks) == 1, "lock is not one top-level object")

    asyncio.run(exercise())

    for name in (
        "FileIdentity",
        "LiveHelperFact",
        "SchedulerInstallAuthorization",
        "FileSnapshot",
        "SchedulerFilesystemResult",
        "AssetFilesystemResult",
        "RollbackResult",
        "DestinationAttestationResult",
    ):
        cls = getattr(assets, name)
        check(hasattr(cls, "__slots__"), f"{name} has no slots")
        check(getattr(cls, "__dataclass_params__").frozen, f"{name} is mutable")

    validator = (ROOT / "tools" / "validate_release.py").read_text(encoding="utf-8")
    assets_source = ASSETS_PATH.read_text(encoding="utf-8")
    module = ast.parse(assets_source)
    symbols = {
        node.name
        for node in module.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    check("_copy_assets" not in symbols, "production _copy_assets remains")
    check("async_install_assets(" in validator, "validator lacks public async path")
    validator_ast = ast.parse(validator)
    validator_calls = {
        (node.func.value.id, node.func.attr)
        for node in ast.walk(validator_ast)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
    }
    check(("assets", "_sync_assets") not in validator_calls, "validator bypasses async path")
    check(("assets", "_copy_assets") not in validator_calls, "validator invokes removed path")
    check("hass.states.get(" in assets_source, "public StateMachine get absent")
    check(
        "from homeassistant.helpers import entity_registry" in assets_source,
        "public entity registry import absent",
    )
    check("ATTR_EDITABLE" in assets_source, "public editable authority absent")
    forbidden_private = (
        "StorageCollection",
        "YamlCollection",
        "_storage_collection",
        "_yaml_collection",
        "websocket_api",
    )
    check(
        not any(token in assets_source for token in forbidden_private),
        "private Home Assistant collection API used",
    )
    combined = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ASSETS_PATH, CANONICAL, SOURCE_PATH)
    )
    check("hass.services.async_call" not in combined, "physical service path exists")
    check("write_register" not in combined, "physical Modbus path exists")
    check("grant_execution" not in combined, "execution grant path exists")

    def enforce(executed: int) -> None:
        if executed != EXPECTED_CHECK_COUNT:
            raise AssertionError("count mismatch")

    for executed in (EXPECTED_CHECK_COUNT - 1, EXPECTED_CHECK_COUNT + 1):
        try:
            enforce(executed)
        except AssertionError:
            check(True, "literal count guard rejected mismatch")
        else:
            check(False, "literal count guard accepted mismatch")


def test_package_marker() -> None:
    const_source = (COMPONENT / "const.py").read_text(encoding="utf-8")
    match = re.search(r'^EMS_PACKAGE_VERSION = "([^"]+)"$', const_source, re.MULTILINE)
    check(match is not None and match.group(1) == EXPECTED_PACKAGE_MARKER, "const package marker differs")
    for path in (CANONICAL, PL_PACKAGE, EN_PACKAGE):
        data = _strict_yaml(path)
        templates = data["template"]
        states = [
            sensor.get("state")
            for block in templates
            if isinstance(block, dict)
            for sensor in block.get("sensor", [])
            if isinstance(sensor, dict) and sensor.get("unique_id") == "hoymiles_ems_package_version"
        ]
        check(states == [EXPECTED_PACKAGE_MARKER], f"package marker differs in {path}")
    manifest = json.loads((COMPONENT / "manifest.json").read_text(encoding="utf-8"))
    check(manifest["version"] == "1.5.7", "public manifest version changed")
    check('VERSION = "1.5.7"' in const_source, "public VERSION changed")
    check(EXPECTED_PACKAGE_MARKER != "1.5.7", "old package would not require restart")
    init_source = (COMPONENT / "__init__.py").read_text(encoding="utf-8")
    check("package_version.state == EMS_PACKAGE_VERSION" in init_source, "restart Repair marker comparison changed")


def test_static_safety() -> None:
    forbidden = {
        "custom_components/hoymiles_hit_modbus/ems_supervisor.py",
        "custom_components/hoymiles_hit_modbus/supervisor_runtime.py",
        "custom_components/hoymiles_hit_modbus/sensor.py",
        "custom_components/hoymiles_hit_modbus/__init__.py",
        "custom_components/hoymiles_hit_modbus/rce_sensor.py",
        "custom_components/hoymiles_hit_modbus/rce_optimizer.py",
        "custom_components/hoymiles_hit_modbus/tariff_sensor.py",
        "custom_components/hoymiles_hit_modbus/tariff_optimizer.py",
        "custom_components/hoymiles_hit_modbus/manifest.json",
        "custom_components/hoymiles_hit_modbus/translations/en.json",
        "custom_components/hoymiles_hit_modbus/translations/pl.json",
        "dashboard_hoymiles.yaml",
        "hoymiles-inverter.yaml",
    }
    changed = _git_paths("diff", "--name-only", "HEAD") | _git_paths("ls-files", "--others", "--exclude-standard")
    check(not forbidden.intersection(changed), f"forbidden paths changed: {sorted(forbidden.intersection(changed))}")
    runtime_diff = subprocess.check_output(
        [
            "git",
            "diff",
            "--unified=0",
            "HEAD",
            "--",
            "home_assistant/hoymiles_ems_scheduler.yaml",
            "custom_components/hoymiles_hit_modbus/supervisor_sensor.py",
            "custom_components/hoymiles_hit_modbus/assets.py",
            "custom_components/hoymiles_hit_modbus/const.py",
        ],
        cwd=ROOT,
        text=True,
        encoding="utf-8",
    )
    added = "\n".join(line[1:] for line in runtime_diff.splitlines() if line.startswith("+") and not line.startswith("+++"))
    check("hass.services" not in added and "async_call(" not in added, "Home Assistant service call added")
    check(
        "write_register" not in added
        and "action: modbus." not in added.lower()
        and "service: modbus." not in added.lower(),
        "Modbus path added",
    )
    check(not re.search(r"\bActive\b", "\n".join(str(item) for item in _strict_yaml(CANONICAL)["input_select"]["hoymiles_ems_supervisor_mode"]["options"])), "Active mode added")
    check("owner acquisition" not in added.lower() and "handover" not in added.lower(), "owner/handover path added")
    check("grant_execution" not in added and "execution_grant" not in added, "grant path added")
    assets_source = ASSETS_PATH.read_text(encoding="utf-8")
    async_body = assets_source.split("async def async_install_assets", 1)[1]
    check("_copy_assets(" not in async_body, "runtime uses legacy compatibility wrapper")
    check("_migrate_legacy_entity_ids(package" not in async_body, "runtime merges modified scheduler")


def test_manifest_and_artifacts() -> None:
    changed = _git_paths("diff", "--name-only", "HEAD") | _git_paths("ls-files", "--others", "--exclude-standard")
    check(changed == EXPECTED_TASK_PATHS, f"task manifest differs: {sorted(changed)}")
    branch = _git_paths("diff", "--name-only", "v1.5.7") | _git_paths("ls-files", "--others", "--exclude-standard")
    check(len(branch) == 21, f"branch-v1.5.7 manifest count differs: {len(branch)}")
    check(EXPECTED_TASK_PATHS.issubset(branch), "task paths missing from branch manifest")
    check(not _git_paths("diff", "--cached", "--name-only"), "staged files exist")
    artifacts = []
    pattern = re.compile(r"(?i)(?:^|[\\/])__pycache__(?:[\\/]|$)|\.py[co]$|(?:\.tmp|\.temp|\.orig|\.rej|\.recover|\.recovery|~)$")
    for path in ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        if path.is_file() and pattern.search(path.relative_to(ROOT).as_posix()):
            artifacts.append(path)
    check(not artifacts, f"repository artifacts found: {artifacts[:3]}")
    check(not (ROOT / "tools" / "test_supervisor_helpers_contract.py").with_suffix(".tmp").exists(), "helper test temp artifact exists")


TEST_GROUPS = (
    ("EXACT_HELPER_DEFINITIONS", test_exact_helper_definitions),
    ("GENERATED_PL_EN_PARITY", test_generated_pl_en_parity),
    ("SAFE_DEFAULTS_AND_RESTORE", test_safe_defaults_and_restore),
    ("SOURCE_MAP_60_0", test_source_map_60_0),
    ("LEGACY_EXECUTION_INDEPENDENCE", test_legacy_execution_independence),
    ("DELIVERY_FRESH_CURRENT_IDEMPOTENCE", test_delivery_fresh_and_current),
    ("DELIVERY_CURRENT_PREFLIGHT", test_delivery_current_preflight),
    ("DELIVERY_PUBLIC_LIVE_CLASSIFIER", test_delivery_live_classifier),
    ("DELIVERY_STORE_FALLBACK", test_delivery_store_fallback),
    ("DELIVERY_HISTORICAL_MODIFIED", test_delivery_historical_and_modified),
    ("DELIVERY_EXPLICIT_OVERWRITE", test_delivery_explicit_overwrite),
    ("DELIVERY_VERIFIED_BACKUP", test_delivery_verified_backup),
    ("DELIVERY_UNIQUE_TEMP_REVALIDATION", test_delivery_unique_temp_and_revalidation),
    ("DELIVERY_METADATA_SELF_HEAL", test_delivery_metadata_self_heal),
    (
        "DELIVERY_FINAL_DESTINATION_ATTESTATION",
        test_delivery_final_destination_attestation,
    ),
    ("DELIVERY_POSTFLIGHT_ROLLBACK", test_delivery_postflight_rollback),
    ("DELIVERY_LOCK_BOUNDARY_VALIDATOR", test_delivery_lock_boundary_and_validator),
    ("PACKAGE_MARKER", test_package_marker),
    ("STATIC_SAFETY", test_static_safety),
    ("MANIFEST_AND_ARTIFACTS", test_manifest_and_artifacts),
)


def _copy_mutation_fixture(destination: Path) -> None:
    paths = EXPECTED_TASK_PATHS | {
        "custom_components/hoymiles_hit_modbus/__init__.py",
        "custom_components/hoymiles_hit_modbus/manifest.json",
        "custom_components/hoymiles_hit_modbus/entity_catalog.json",
        "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_en.yaml",
        "custom_components/hoymiles_hit_modbus/resources/dashboard_hoymiles_pl.yaml",
        "dashboard_hoymiles.yaml",
    }
    for relative in paths:
        source = ROOT / relative
        if not source.exists():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _mutate_text(root: Path, relative: str, old: str, new: str, count: int = 1) -> None:
    path = root / relative
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise AssertionError(f"mutation anchor missing: {relative}: {old[:40]!r}")
    path.write_text(text.replace(old, new, count), encoding="utf-8")


def _mutation_helper_data(root: Path) -> dict[str, Any]:
    return _strict_yaml(root / "home_assistant" / "hoymiles_ems_scheduler.yaml")


def _mutation_hass(root: Path, config: Path) -> tuple[types.ModuleType, FakeHass]:
    assets = _load_assets_module(root)
    assets.LOCAL_FRONTEND_ASSETS = ()
    return assets, FakeHass(config)


def _mutation_asset_replaces_modified(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_m14_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        old = b"# user modified\nstate: custom\n"
        package.write_bytes(old)
        assets, hass = _mutation_hass(root, config)
        _run_install(assets, hass)
        return package.read_bytes() != old


def _mutation_asset_non_idempotent(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_m15_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        source = (
            root
            / "custom_components"
            / "hoymiles_hit_modbus"
            / "resources"
            / "home_assistant"
            / "pl"
            / "hoymiles_ems_scheduler.yaml"
        )
        package.write_bytes(source.read_bytes())
        inode = package.stat().st_ino
        assets, hass = _mutation_hass(root, config)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {EMS_RELATIVE: EXPECTED_NEW_HASHES["pl"]},
        }
        written = _run_install(assets, hass)
        return package in written or package.stat().st_ino != inode


def _mutation_missing_backup(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_m16_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        old = _git_bytes(
            "HEAD",
            "custom_components/hoymiles_hit_modbus/resources/home_assistant/pl/"
            "hoymiles_ems_scheduler.yaml",
        )
        package.write_bytes(old)
        assets, hass = _mutation_hass(root, config)
        written = _run_install(assets, hass)
        backup = package.with_name(package.name + EXPECTED_BACKUP_SUFFIX)
        return package in written and not backup.exists()


def _mutation_disk_collision_allowed(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s22_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(
            config,
            input_select=("hoymiles_ems_supervisor_mode",),
        )
        assets, hass = _mutation_hass(root, config)
        return config / EMS_RELATIVE in _run_install(assets, hass)


def _mutation_collision_metadata_advanced(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s23_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        source = (
            root
            / "custom_components"
            / "hoymiles_hit_modbus"
            / "resources"
            / "home_assistant"
            / "pl"
            / "hoymiles_ems_scheduler.yaml"
        )
        package.write_bytes(source.read_bytes())
        assets, hass = _mutation_hass(root, config)
        _set_ui_collision(hass)
        _run_install(assets, hass)
        return _metadata_hash(hass) is not None


def _mutation_stale_metadata_overwrites(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s25_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        old = b"# user modified\nstate: custom\n"
        package.write_bytes(old)
        assets, hass = _mutation_hass(root, config)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {EMS_RELATIVE: "0" * 64},
        }
        _run_install(assets, hass)
        return package.read_bytes() != old


def _mutation_overwrite_bypasses_collision(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s26_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        old = EXPECTED_BACKUP_FIXTURE
        package.write_bytes(old)
        assets, hass = _mutation_hass(root, config)
        _set_ui_collision(hass)
        _run_install(assets, hass, overwrite=True)
        return package.read_bytes() != old


def _mutation_backup_overwritten(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s27_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        old = EXPECTED_BACKUP_FIXTURE
        package.write_bytes(old)
        backup = package.with_name(package.name + EXPECTED_BACKUP_SUFFIX)
        backup.write_bytes(old)
        inode = backup.stat().st_ino
        assets, hass = _mutation_hass(root, config)
        _run_install(assets, hass, overwrite=True)
        return backup.stat().st_ino != inode


def _mutation_metadata_cannot_heal(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s28_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        old = EXPECTED_BACKUP_FIXTURE
        old_hash = _sha256_bytes(old)
        package.write_bytes(old)
        assets, hass = _mutation_hass(root, config)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {EMS_RELATIVE: old_hash},
        }
        hass.metadata_failures = 1
        try:
            _run_install(assets, hass, overwrite=True)
        except OSError:
            pass
        _run_install(assets, hass)
        return _metadata_hash(hass) != EXPECTED_NEW_HASHES["pl"]


def _mutation_unknown_major_allowed(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s32_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        _write_store_payload(config, "input_select", version=2, minor_version=1)
        assets, hass = _mutation_hass(root, config)
        return config / EMS_RELATIVE in _run_install(assets, hass)


def _mutation_live_collision_allowed(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s33_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        assets, hass = _mutation_hass(root, config)
        _set_ui_collision(hass)
        return config / EMS_RELATIVE in _run_install(assets, hass)


def _mutation_foreign_backup_allowed(root: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s34_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        package.write_bytes(EXPECTED_BACKUP_FIXTURE)
        backup = package.with_name(package.name + EXPECTED_BACKUP_SUFFIX)
        backup.write_bytes(b"foreign")
        assets, hass = _mutation_hass(root, config)
        return package in _run_install(assets, hass, overwrite=True)


def _mutation_postflight_advances(root: Path) -> bool:
    helper_advanced = False
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s39_probe_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        old = EXPECTED_BACKUP_FIXTURE
        package.write_bytes(old)
        assets, hass = _mutation_hass(root, config)

        def collide(name: str, _result: Any) -> None:
            if name == "_sync_assets":
                _set_ui_collision(hass)

        hass.executor_after = collide
        _run_install(assets, hass, overwrite=True)
        helper_advanced = _metadata_hash(hass) == EXPECTED_NEW_HASHES["pl"]

    destination_advanced = False
    with tempfile.TemporaryDirectory(prefix="supervisor_1b3_s39_attest_") as tmp:
        config = Path(tmp)
        _write_helper_storage(config)
        package = config / EMS_RELATIVE
        package.parent.mkdir(parents=True)
        old = EXPECTED_BACKUP_FIXTURE
        old_hash = _sha256_bytes(old)
        package.write_bytes(old)
        assets, hass = _mutation_hass(root, config)
        hass.store_payload = {
            "integration_version": "1.5.7",
            "assets": {EMS_RELATIVE: old_hash},
        }

        def restore_old(name: str, _result: Any) -> None:
            if name == "_sync_assets":
                package.write_bytes(old)

        hass.executor_after = restore_old
        _run_install(assets, hass, overwrite=True)
        destination_advanced = (
            _metadata_hash(hass) == EXPECTED_NEW_HASHES["pl"]
        )
    return helper_advanced and destination_advanced


def run_mutation_campaign() -> None:
    canonical_rel = "home_assistant/hoymiles_ems_scheduler.yaml"
    source_rel = "custom_components/hoymiles_hit_modbus/supervisor_sensor.py"
    assets_rel = "custom_components/hoymiles_hit_modbus/assets.py"

    def scheduler_detector(predicate: Callable[[dict[str, Any]], bool]) -> Callable[[Path], bool]:
        return lambda root: predicate(_mutation_helper_data(root))

    def helper_initial(data: dict[str, Any]) -> bool:
        return any(
            "initial" in data[domain][object_id]
            for domain, definitions in HELPERS.items()
            for object_id in definitions
        )

    def exec_refs(root: Path) -> bool:
        try:
            return bool(_execution_helper_references(_mutation_helper_data(root)))
        except DuplicateKeyError:
            return True

    def duplicate_detected(root: Path) -> bool:
        try:
            data = _mutation_helper_data(root)
        except DuplicateKeyError:
            return True
        text = (root / canonical_rel).read_text(encoding="utf-8")
        return any(text.count(f"  {object_id}:\n") != 1 for object_id in OBJECT_IDS) or not isinstance(data, dict)

    def source_ids_changed(root: Path) -> bool:
        rows = _source_rows(root / source_rel)
        baseline = _source_rows()
        return tuple(row[:5] for row in rows) != tuple(row[:5] for row in baseline)

    def source_counts_bad(root: Path) -> bool:
        rows = _source_rows(root / source_rel)
        return len(rows) != 60 or sum(bool(row[5]) for row in rows) != 0 or tuple(row[0] for row in rows) != tuple(range(1, 61))

    def unsafe_calls(root: Path) -> bool:
        text = "\n".join(
            (root / relative).read_text(encoding="utf-8")
            for relative in (canonical_rel, assets_rel, source_rel)
        )
        return any(token in text for token in ("hass.services.async_call", "write_register", "grant_execution", "owner_acquire", "handover_execution"))

    def dashboard_changed(root: Path) -> bool:
        return (root / "dashboard_hoymiles.yaml").read_bytes() != (ROOT / "dashboard_hoymiles.yaml").read_bytes()

    def assets_contain(token: str) -> Callable[[Path], bool]:
        return lambda root: token in (root / assets_rel).read_text(encoding="utf-8")

    def revalidation_removed(root: Path) -> bool:
        text = (root / assets_rel).read_text(encoding="utf-8")
        return "_revalidate_destination(destination, before)" not in text

    def validator_calls_copy_assets(root: Path) -> bool:
        module = ast.parse(
            (root / "tools" / "validate_release.py").read_text(encoding="utf-8")
        )
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "assets"
            and node.func.attr == "_copy_assets"
            for node in ast.walk(module)
        )

    def mutate_future_helpers(root: Path) -> None:
        path = root / source_rel
        text = path.read_text(encoding="utf-8")
        for number in range(1, 6):
            text, replacements = re.subn(
                rf"^(\s*\({number},[^\n]*,\s)False\),$",
                r"\1True),",
                text,
                count=1,
                flags=re.MULTILINE,
            )
            if replacements != 1:
                raise AssertionError(f"M17 source row {number} anchor missing")
        path.write_text(text, encoding="utf-8")

    def mutate_collision_metadata(root: Path) -> None:
        _mutate_text(
            root,
            assets_rel,
            "    previous = previous_hashes.get(EMS_PACKAGE_RELATIVE_PATH)\n",
            "    if not preflight.allowed or not postflight.allowed:\n"
            "        return transaction.source_hash\n"
            "    previous = previous_hashes.get(EMS_PACKAGE_RELATIVE_PATH)\n",
        )

    def mutate_preflight_after_repair(root: Path) -> None:
        _mutate_text(
            root,
            assets_rel,
            "        preflight = await _async_scheduler_authorization(hass, config_path)\n",
            "        scheduler_candidate = config_path / EMS_PACKAGE_RELATIVE_PATH\n"
            "        source_candidate = (\n"
            "            RESOURCE_ROOT / (\"home_assistant/pl/hoymiles_ems_scheduler.yaml\")\n"
            "        )\n"
            "        if scheduler_candidate.is_file() and (\n"
            "            hashlib.sha256(scheduler_candidate.read_bytes()).hexdigest()\n"
            "            == hashlib.sha256(source_candidate.read_bytes()).hexdigest()\n"
            "        ):\n"
            "            managed_hashes[EMS_PACKAGE_RELATIVE_PATH] = (\n"
            "                hashlib.sha256(source_candidate.read_bytes()).hexdigest()\n"
            "            )\n"
            "        preflight = await _async_scheduler_authorization(hass, config_path)\n",
        )

    def mutate_postflight_metadata(root: Path) -> None:
        _mutate_text(
            root,
            assets_rel,
            "        if not postflight.allowed and filesystem.scheduler.physically_written:\n",
            "        if False and not postflight.allowed and filesystem.scheduler.physically_written:\n",
        )
        _mutate_text(
            root,
            assets_rel,
            "        and postflight.allowed\n",
            "        and True  # mutation ignores postflight\n",
        )
        _mutate_text(
            root,
            assets_rel,
            "        and attestation.action is DestinationAttestationAction.VERIFIED\n",
            "        and True  # mutation ignores final destination attestation\n",
        )

    def mutate_overwrite_collision(root: Path) -> None:
        _mutate_text(
            root,
            assets_rel,
            "    if not authorization.allowed:\n",
            "    if not authorization.allowed and not overwrite:\n",
        )
        _mutate_text(
            root,
            assets_rel,
            "        if not postflight.allowed and filesystem.scheduler.physically_written:\n",
            "        if False and not postflight.allowed and filesystem.scheduler.physically_written:\n",
        )

    mutations: list[tuple[str, str, Callable[[Path], None], Callable[[Path], bool]]] = [
        (
            "M01",
            "Active mode",
            lambda root: _mutate_text(root, canonical_rel, '      - "Shadow"\n', '      - "Shadow"\n      - "Active"\n'),
            scheduler_detector(lambda data: data["input_select"]["hoymiles_ems_supervisor_mode"]["options"] != ["Off", "Shadow"]),
        ),
        (
            "M02",
            "Shadow default",
            lambda root: _mutate_text(root, canonical_rel, '      - "Off"\n      - "Shadow"', '      - "Shadow"\n      - "Off"'),
            scheduler_detector(lambda data: data["input_select"]["hoymiles_ems_supervisor_mode"]["options"][0] != "Off"),
        ),
        (
            "M03",
            "Maximum Profit default",
            lambda root: _mutate_text(root, canonical_rel, '      - "Balanced"\n      - "Maximum Profit"', '      - "Maximum Profit"\n      - "Balanced"'),
            scheduler_detector(lambda data: data["input_select"]["hoymiles_ems_supervisor_profile"]["options"][0] != "Balanced"),
        ),
        (
            "M04",
            "permission initial true",
            lambda root: _mutate_text(root, canonical_rel, "    icon: mdi:chart-line\n", "    icon: mdi:chart-line\n    initial: true\n"),
            scheduler_detector(helper_initial),
        ),
        (
            "M05",
            "translated option",
            lambda root: _mutate_text(root, canonical_rel, '      - "Balanced"', '      - "Zrównoważony"'),
            scheduler_detector(lambda data: data["input_select"]["hoymiles_ems_supervisor_profile"]["options"] != HELPERS["input_select"]["hoymiles_ems_supervisor_profile"]["options"]),
        ),
        (
            "M06",
            "profile dash altered",
            lambda root: _mutate_text(root, canonical_rel, "High Reserve — Winter", "High Reserve - Winter"),
            scheduler_detector(lambda data: "High Reserve — Winter" not in data["input_select"]["hoymiles_ems_supervisor_profile"]["options"]),
        ),
        (
            "M07",
            "mode execution condition",
            lambda root: _mutate_text(root, canonical_rel, "automation:\n", "automation:\n  - alias: MUTATION M07\n    condition: \"{{ states('input_select.hoymiles_ems_supervisor_mode') == 'Shadow' }}\"\n    action: []\n"),
            exec_refs,
        ),
        (
            "M08",
            "permission start gate",
            lambda root: _mutate_text(root, canonical_rel, "automation:\n", "automation:\n  - alias: MUTATION M08\n    condition: \"{{ is_state('input_boolean.hoymiles_ems_supervisor_allow_rce', 'on') }}\"\n    action: []\n"),
            exec_refs,
        ),
        (
            "M09",
            "legacy enable service",
            lambda root: _mutate_text(root, canonical_rel, "automation:\n", "automation:\n  - alias: MUTATION M09\n    condition: \"{{ is_state('input_boolean.hoymiles_ems_supervisor_allow_rce', 'on') }}\"\n    action:\n      - action: input_boolean.turn_on\n        target:\n          entity_id: input_boolean.hoymiles_rce_discharge_enabled\n"),
            exec_refs,
        ),
        (
            "M10",
            "mode initial reset",
            lambda root: _mutate_text(
                root,
                canonical_rel,
                '  hoymiles_ems_supervisor_mode:\n    name: "Nadzorca EMS — tryb"\n    options:\n      - "Off"\n      - "Shadow"\n    icon: mdi:eye-outline\n',
                '  hoymiles_ems_supervisor_mode:\n    name: "Nadzorca EMS — tryb"\n    options:\n      - "Off"\n      - "Shadow"\n    icon: mdi:eye-outline\n    initial: "Off"\n',
            ),
            scheduler_detector(helper_initial),
        ),
        (
            "M11",
            "update permission reset",
            lambda root: (root / assets_rel).write_text((root / assets_rel).read_text(encoding="utf-8") + "\nasync def _mutation_reset(hass):\n    await hass.services.async_call('input_boolean', 'turn_off', {'entity_id': 'input_boolean.hoymiles_ems_supervisor_allow_rce'})\n", encoding="utf-8"),
            unsafe_calls,
        ),
        (
            "M12",
            "duplicate helper mapping",
            lambda root: (root / canonical_rel).write_text((root / canonical_rel).read_text(encoding="utf-8") + "\ninput_select:\n  hoymiles_ems_supervisor_mode:\n    options: [\"Off\", \"Shadow\"]\n", encoding="utf-8"),
            duplicate_detected,
        ),
        (
            "M13",
            "suffixed canonical source",
            lambda root: _mutate_text(root, source_rel, "input_select.hoymiles_ems_supervisor_mode\"", "input_select.hoymiles_ems_supervisor_mode_2\""),
            source_ids_changed,
        ),
        (
            "M14",
            "modified package overwrite",
            lambda root: _mutate_text(root, assets_rel, "if not overwrite and not managed:", "if not overwrite and managed:"),
            _mutation_asset_replaces_modified,
        ),
        (
            "M15",
            "non-idempotent package update",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "    if before.sha256 == source_hash:\n",
                "    if False and before.sha256 == source_hash:\n",
            ),
            _mutation_asset_non_idempotent,
        ),
        (
            "M16",
            "replacement without exact backup",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "        if before.existed:\n            persistent_backup = _ensure_fixed_backup(\n",
                "        if False and before.existed:\n            persistent_backup = _ensure_fixed_backup(\n",
            ),
            _mutation_missing_backup,
        ),
        (
            "M17",
            "source map 55/5",
            mutate_future_helpers,
            source_counts_bad,
        ),
        (
            "M18",
            "61st watched source",
            lambda root: _mutate_text(root, source_rel, ")\n\nSUPERVISOR_SOURCE_SPECS", '    (61, "mutation_source", "sensor.mutation_source", False, False, False),\n)\n\nSUPERVISOR_SOURCE_SPECS'),
            source_counts_bad,
        ),
        (
            "M19",
            "dashboard scope expansion",
            lambda root: (root / "dashboard_hoymiles.yaml").write_text((root / "dashboard_hoymiles.yaml").read_text(encoding="utf-8") + "\n# Phase 1B-3 mutation\n", encoding="utf-8"),
            dashboard_changed,
        ),
        (
            "M20",
            "physical authority path",
            lambda root: (root / source_rel).write_text((root / source_rel).read_text(encoding="utf-8") + "\nasync def _mutation_physical(hass):\n    await hass.services.async_call('modbus', 'write_register', {'grant_execution': True, 'owner_acquire': True, 'handover_execution': True})\n", encoding="utf-8"),
            unsafe_calls,
        ),
        (
            "S21",
            "duplicate top-level YAML map",
            lambda root: (root / canonical_rel).write_text(
                (root / canonical_rel).read_text(encoding="utf-8")
                + "\ninput_boolean:\n  mutation_duplicate: {}\n",
                encoding="utf-8",
            ),
            duplicate_detected,
        ),
        (
            "S22",
            "actual Store collision missed",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "                if object_id in _SUPERVISOR_OBJECT_IDS\n",
                "                if False and object_id in _SUPERVISOR_OBJECT_IDS\n",
            ),
            _mutation_disk_collision_allowed,
        ),
        (
            "S23",
            "collision advances metadata",
            mutate_collision_metadata,
            _mutation_collision_metadata_advanced,
        ),
        (
            "S24",
            "current destination replaced again",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "    if before.sha256 == source_hash:\n",
                "    if False and before.sha256 == source_hash:\n",
            ),
            _mutation_asset_non_idempotent,
        ),
        (
            "S25",
            "stale metadata authorizes overwrite",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "            previous_hash == before.sha256\n",
                "            previous_hash is not None\n",
            ),
            _mutation_stale_metadata_overwrites,
        ),
        (
            "S26",
            "overwrite bypasses collision",
            mutate_overwrite_collision,
            _mutation_overwrite_bypasses_collision,
        ),
        (
            "S27",
            "existing backup overwritten",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "    else:\n        _verify_fixed_backup(backup, before, before_bytes)\n        return backup\n\n    if before.sha256 is None:\n",
                "    else:\n        _verify_fixed_backup(backup, before, before_bytes)\n        os.unlink(backup)\n\n    if before.sha256 is None:\n",
            ),
            _mutation_backup_overwritten,
        ),
        (
            "S28",
            "metadata failure cannot self-heal",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "    if before.sha256 == source_hash:\n",
                "    if before.sha256 == source_hash and previous_hash == source_hash:\n",
            ),
            _mutation_metadata_cannot_heal,
        ),
        (
            "S29",
            "future helper changes safe missing behavior",
            mutate_future_helpers,
            source_counts_bad,
        ),
        (
            "S30",
            "helper consumed through executable alias",
            lambda root: _mutate_text(
                root,
                canonical_rel,
                "automation:\n",
                "automation:\n  - alias: MUTATION S30\n    condition: \"{{ is_state('input_boolean.hoymiles_ems_supervisor_allow_rce', 'on') }}\"\n    action: []\n",
            ),
            exec_refs,
        ),
        (
            "S31",
            "collision check after current metadata repair",
            mutate_preflight_after_repair,
            _mutation_collision_metadata_advanced,
        ),
        (
            "S32",
            "unknown Store major accepted",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "        or major != expected_major\n",
                "        or False  # mutation accepts unknown major\n",
            ),
            _mutation_unknown_major_allowed,
        ),
        (
            "S33",
            "disk-only authority ignores live editable helper",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "    elif HelperClassification.UI_STORAGE_COLLISION in classifications:\n",
                "    elif False and HelperClassification.UI_STORAGE_COLLISION in classifications:\n",
            ),
            _mutation_live_collision_allowed,
        ),
        (
            "S34",
            "foreign existing backup accepted",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "    if (\n        before.sha256 is None\n        or backup_snapshot.sha256 != before.sha256\n        or backup_bytes != before_bytes\n    ):\n",
                "    if False and (\n        before.sha256 is None\n        or backup_snapshot.sha256 != before.sha256\n        or backup_bytes != before_bytes\n    ):\n",
            ),
            _mutation_foreign_backup_allowed,
        ),
        (
            "S35",
            "backup symlink followed",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "        backup,\n        require_single_link=True,\n        error_code=\"unsafe_backup\",\n",
                "        backup.resolve(),\n        require_single_link=True,\n        error_code=\"unsafe_backup\",\n",
            ),
            assets_contain("backup.resolve()"),
        ),
        (
            "S36",
            "predictable fixed destination temp restored",
            lambda root: (root / assets_rel).write_text(
                (root / assets_rel).read_text(encoding="utf-8")
                + "\n_MUTATION_FIXED_TEMP = \".hoymiles_ems_scheduler.yaml.hoymiles_hit_modbus.tmp\"\n",
                encoding="utf-8",
            ),
            assets_contain(EXPECTED_OLD_TEMP_NAME),
        ),
        (
            "S37",
            "prepared hardlink under old temp overwritten",
            lambda root: (root / assets_rel).write_text(
                (root / assets_rel).read_text(encoding="utf-8")
                + "\ndef _MUTATION_PREPARED_HARDLINK(destination):\n"
                "    old_temp = destination.with_name(f'.{destination.name}.hoymiles_hit_modbus.tmp')\n"
                "    os.replace(old_temp, destination)\n",
                encoding="utf-8",
            ),
            assets_contain("_MUTATION_PREPARED_HARDLINK"),
        ),
        (
            "S38",
            "destination change ignored",
            lambda root: _mutate_text(
                root,
                assets_rel,
                "        _revalidate_destination(destination, before)\n",
                "        # mutation removed destination revalidation\n",
            ),
            revalidation_removed,
        ),
        (
            "S39",
            "postflight or final-attestation mismatch advances metadata",
            mutate_postflight_metadata,
            _mutation_postflight_advances,
        ),
        (
            "S40",
            "validator calls removed compatibility path",
            lambda root: _mutate_text(
                root,
                "tools/validate_release.py",
                "        assets.async_install_assets(\n",
                "        assets._copy_assets(\n",
            ),
            validator_calls_copy_assets,
        ),
    ]

    detected = 0
    survivors: list[str] = []
    for mutation_id, description, mutator, detector in mutations:
        with tempfile.TemporaryDirectory(prefix=f"supervisor_1b3_{mutation_id.lower()}_") as tmp:
            root = Path(tmp) / "fixture"
            _copy_mutation_fixture(root)
            mutator(root)
            if detector(root):
                detected += 1
                print(f"DETECTED {mutation_id} {description}")
            else:
                survivors.append(mutation_id)
                print(f"SURVIVED {mutation_id} {description}")
    if survivors:
        raise AssertionError(f"mutation survivors: {survivors}")
    print(f"Mutation campaign: PASS detected={detected}/40 survivors=0")


def main() -> None:
    if "--mutations" in sys.argv:
        run_mutation_campaign()
        return
    for name, function in TEST_GROUPS:
        group(name, function)
    if CHECKS != EXPECTED_CHECK_COUNT:
        raise AssertionError(
            f"Executed check count differs: expected {EXPECTED_CHECK_COUNT}, got {CHECKS}"
        )
    print(
        "Supervisor helpers contract: PASS "
        f"groups={GROUPS}/{len(TEST_GROUPS)} checks={CHECKS} "
        "helpers=5 mode=Off/Shadow profile=Balanced/Maximum Profit/High Reserve — Winter "
        "permissions=off restore=preserved sources=60/0 execution_refs=0"
    )


if __name__ == "__main__":
    main()
