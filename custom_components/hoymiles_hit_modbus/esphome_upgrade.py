"""Prepare existing official ESPHome YAML without I/O, secrets lookup or OTA.

Only the official package block, import hint and OTA override may change.
All hardware, identity and user substitutions remain in the original source.
Unrecognized custom configurations need review rather than a guessed migration.
"""
from functools import lru_cache
import hashlib
import json
from pathlib import Path

import yaml

FIRMWARE_SHA = "ba91261597420ea3a58a6665fc996d7081f219e2"
REPOSITORY = "https://github.com/Kaluzaburza/hoymiles-hit-g3-ems"
MAX_SOURCE_BYTES = 131072
MAX_PACKAGE_BYTES = 1048576
_ALLOWED_ROOT = frozenset({
    "substitutions", "esp32", "psram", "dashboard_import", "logger", "packages",
    "uart", "modbus", "api", "wifi", "esphome", "ota",
})
_REVIEW_CODES = frozenset({
    "unsupported_yaml", "custom_configuration", "unsupported_packages",
    "incomplete_packages", "package_too_large", "modified_packages",
    "source_too_large", "transport_unknown", "missing_existing_settings",
    "unknown_board", "custom_api_key", "custom_ota", "ota_password_missing",
})


@lru_cache(maxsize=1)
def package_manifest():
    return json.loads(Path(__file__).with_name("esphome_upgrade_manifest.json").read_text(encoding="utf-8"))


def _text_hash(value):
    return hashlib.sha256(value.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _mapping(node):
    if not isinstance(node, yaml.MappingNode):
        raise ValueError("unsupported_yaml")
    result = {}
    for key, value in node.value:
        if not isinstance(key, yaml.ScalarNode) or key.value in result or key.value == "<<":
            raise ValueError("unsupported_yaml")
        result[key.value] = value
    return result


def _scalar(node):
    if not isinstance(node, yaml.ScalarNode):
        raise ValueError("unsupported_yaml")
    return node.value


def _review(code):
    return {"status": "review_required", "code": code}


def _parse(source):
    # No constructors are invoked: !secret is kept as a tag, never resolved.
    depth = 0
    for token in yaml.scan(source):
        if isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken)):
            raise ValueError("unsupported_yaml")
        if isinstance(token, (yaml.tokens.BlockMappingStartToken, yaml.tokens.BlockSequenceStartToken,
                              yaml.tokens.FlowMappingStartToken, yaml.tokens.FlowSequenceStartToken)):
            depth += 1
            if depth > 25:
                raise ValueError("unsupported_yaml")
        elif isinstance(token, (yaml.tokens.BlockEndToken, yaml.tokens.FlowMappingEndToken,
                                yaml.tokens.FlowSequenceEndToken)):
            depth -= 1
    root = yaml.compose(source)
    count = 0
    def check(node, depth=0):
        nonlocal count
        count += 1
        if depth > 25 or count > 4000:
            raise ValueError("unsupported_yaml")
        if isinstance(node, yaml.MappingNode):
            _mapping(node)  # Reject duplicates at every level.
            for key, value in node.value:
                check(key, depth + 1)
                check(value, depth + 1)
        elif isinstance(node, yaml.SequenceNode):
            for value in node.value:
                check(value, depth + 1)
        elif isinstance(node, yaml.ScalarNode) and node.tag not in {
            "tag:yaml.org,2002:str", "tag:yaml.org,2002:int", "tag:yaml.org,2002:float",
            "tag:yaml.org,2002:bool", "tag:yaml.org,2002:null", "!secret", "!include", "!extend", "!remove",
        }:
            raise ValueError("custom_configuration")
    check(root)
    return root


def _packages(node, local_packages):
    manifest = package_manifest()
    expected = set(manifest["files"])
    items = _mapping(node)
    if not items:
        raise ValueError("unsupported_packages")
    if all(isinstance(value, yaml.ScalarNode) and value.tag == "!include" for value in items.values()):
        names = [_scalar(value).replace("\\", "/") for value in items.values()]
        if set(names) != expected or len(names) != len(expected):
            raise ValueError("unsupported_packages")
        if not local_packages:
            return {"status": "need_packages", "code": "local_packages_required", "files": sorted(expected)}
        if set(local_packages) != expected:
            raise ValueError("incomplete_packages")
        for name, contents in local_packages.items():
            if not isinstance(contents, str) or len(contents.encode("utf-8")) > MAX_PACKAGE_BYTES:
                raise ValueError("package_too_large")
            if _text_hash(contents) not in manifest["files"][name]:
                raise ValueError("modified_packages")
        return None
    if len(items) != 1:
        raise ValueError("unsupported_packages")
    remote = _mapping(next(iter(items.values())))
    if set(remote) - {"url", "ref", "files", "refresh"}:
        raise ValueError("unsupported_packages")
    if _scalar(remote.get("url")) != REPOSITORY:
        raise ValueError("unsupported_packages")
    if _scalar(remote.get("ref")) not in manifest["accepted_refs"]:
        raise ValueError("unsupported_packages")
    files = remote.get("files")
    if not isinstance(files, yaml.SequenceNode):
        raise ValueError("unsupported_packages")
    names = [_scalar(item) for item in files.value]
    if set(names) != expected or len(names) != len(expected):
        raise ValueError("unsupported_packages")
    return None


def _prepare(source, transport, local_packages):
    if not isinstance(source, str) or len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError("source_too_large")
    if transport not in {"legacy", "encrypted"}:
        raise ValueError("transport_unknown")
    root = _parse(source)
    fields = _mapping(root)
    if set(fields) - _ALLOWED_ROOT:
        raise ValueError("custom_configuration")
    pending = _packages(fields.get("packages"), local_packages)
    if pending:
        return pending
    substitutions = _mapping(fields.get("substitutions"))
    for key in ("name", "api_key", "wifi_ssid", "wifi_password", "fallback_password"):
        if key not in substitutions or not _scalar(substitutions[key]).strip():
            raise ValueError("missing_existing_settings")
    esp32 = _mapping(fields.get("esp32"))
    board = _scalar(esp32.get("board"))
    if board == "${board}":
        board = _scalar(substitutions.get("board"))
    if board not in {"esp32dev", "esp32-s3-devkitc-1"}:
        raise ValueError("unknown_board")
    if "api" in fields:
        api = _mapping(fields["api"])
        if "encryption" in api and _scalar(_mapping(api["encryption"]).get("key")) != "${api_key}":
            raise ValueError("custom_api_key")
    # Preserve all physical board/partition/UART fields verbatim, including an
    # older chip's explicit minimum revision. Never inject public defaults.
    entrypoint = "hoymiles-inverter.yaml"
    if board == "esp32-s3-devkitc-1":
        entrypoint = "hoymiles-inverter-s3.yaml"
    elif "uart" in fields and "flow_control_pin" in _mapping(fields["uart"]):
        entrypoint = "hoymiles-inverter-flow-control.yaml"

    password = None
    if "ota_password" in substitutions and _scalar(substitutions["ota_password"]).strip():
        password = "${ota_password}"
    if "ota" in fields:
        ota = fields["ota"]
        if not isinstance(ota, yaml.SequenceNode) or len(ota.value) != 1:
            raise ValueError("custom_ota")
        settings = _mapping(ota.value[0])
        if set(settings) - {"id", "platform", "password", "encryption", "version"}:
            raise ValueError("custom_ota")
        if "id" in settings and _scalar(settings["id"]) != "ota_esphome":
            raise ValueError("custom_ota")
        if "platform" in settings and _scalar(settings["platform"]) != "esphome":
            raise ValueError("custom_ota")
        if "password" in settings and settings["password"].tag != "!remove":
            value = settings["password"]
            if value.style in {"|", ">"}:
                raise ValueError("custom_ota")
            password = (source[value.start_mark.index:value.end_mark.index].strip()
                        if _scalar(value).strip() else None)
        if "encryption" in settings and isinstance(settings["encryption"], yaml.MappingNode) and _mapping(settings["encryption"]):
            raise ValueError("custom_ota")
    if transport == "legacy" and not password:
        raise ValueError("ota_password_missing")
    if "dashboard_import" in fields:
        hint = _mapping(fields["dashboard_import"])
        url = _scalar(hint.get("package_import_url"))
        if not url.startswith("github://Kaluzaburza/hoymiles-hit-g3-ems/"):
            raise ValueError("custom_configuration")

    packages = "packages:\n  hoymiles_hit_g3:\n" + f"    url: {REPOSITORY}\n    ref: {FIRMWARE_SHA}\n    refresh: 1d\n    files:\n"
    packages += "".join(f"      - {name}\n" for name in package_manifest()["files"])
    dashboard = "dashboard_import:\n" + f"  package_import_url: github://Kaluzaburza/hoymiles-hit-g3-ems/{entrypoint}@{FIRMWARE_SHA}\n  import_full_config: true\n"
    ota = "ota:\n  - id: !extend ota_esphome\n"
    ota += (f"    encryption: !remove\n    password: {password}\n" if transport == "legacy"
            else "    password: !remove\n    encryption: {}\n")
    replacements = {"packages": packages, "dashboard_import": dashboard, "ota": ota}
    edits = []
    for key, value in root.value:
        if key.value in replacements:
            edits.append((key.start_mark.index, value.end_mark.index, replacements.pop(key.value)))
    output = source
    for start, end, replacement in sorted(edits, reverse=True):
        output = output[:start] + replacement + output[end:]
    for replacement in replacements.values():
        output = output.rstrip() + "\n\n" + replacement
    _parse(output)
    return {
        "status": "ready", "yaml": output, "backup": source,
        "stage": "bridge" if transport == "legacy" else "final",
        "board": board, "firmware_sha": FIRMWARE_SHA,
        "firmware_version": "1.5.8rc2", "compatible_integration": "1.5.8.1",
        "validation": "not_run", "uploaded": False,
        "sha256": hashlib.sha256(output.encode("utf-8")).hexdigest(),
    }


def prepare_upgrade(source, *, transport="unknown", local_packages=None):
    """Return a prepared document or a bounded, non-sensitive reason code."""
    try:
        if local_packages is not None and not isinstance(local_packages, dict):
            raise ValueError("incomplete_packages")
        return _prepare(source, transport, local_packages)
    except ValueError as err:
        return _review(str(err) if str(err) in _REVIEW_CODES else "unsupported_yaml")
    except (yaml.YAMLError, RecursionError, TypeError, AttributeError):
        # YAML exceptions may contain snippets with passwords. Never expose
        # those exceptions to the client or logs.
        return _review("unsupported_yaml")
