"""Exercise the real asset/migration code against immutable public 1.5.7 bytes.

Offline only: temporary HA config, public API doubles, no device/network calls.
Historical release validators remain unchanged.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

import yaml

from test_supervisor_helpers_contract import (
    FakeHass, ROOT, _load_assets_module, _run_install, _write_helper_storage,
)
from test_ems_shared_input_migration import M, _unmigrated_states

BASE = "6617bc4de6592439ea2c64889b0a25bbe5bfa45e"
COMPONENT = "custom_components/hoymiles_hit_modbus"


def old(path):
    return subprocess.check_output(["git", "show", f"{BASE}:{path}"], cwd=ROOT)


def main():
    catalog = json.loads(old(f"{COMPONENT}/entity_catalog.json"))
    current = json.loads((ROOT / COMPONENT / "entity_catalog.json").read_text(encoding="utf8"))
    by_id = {(r["domain"], r["source_id"]): r for r in current}
    for record in catalog:
        successor = by_id[(record["domain"], record["source_id"])]
        for field in ("translation_key", "source_component", "source_name", "source_object_id"):
            assert record[field] == successor[field], (field, record["source_id"])
    assert len(catalog) == 294
    # The stable config-entry and proxy unique-ID implementations are unchanged.
    for path in ("config_flow.py", "entity.py"):
        assert old(f"{COMPONENT}/{path}").decode().replace("\r\n", "\n") == (
            ROOT / COMPONENT / path
        ).read_text(encoding="utf8")
    print("PASS: all 294 existing source identities and config-entry identity retained")

    legacy = yaml.safe_load(old("home_assistant/hoymiles_ems_scheduler.yaml"))
    now = yaml.safe_load((ROOT / "home_assistant/hoymiles_ems_scheduler.yaml").read_text(encoding="utf8"))
    for domain in ("input_number", "input_text", "input_select", "input_boolean", "input_datetime"):
        removed = set(legacy.get(domain, {})) - set(now.get(domain, {}))
        assert removed == ({"hoymiles_rcm_shadow_mode"} if domain == "input_boolean" else set())
        for key, config in legacy.get(domain, {}).items():
            if key in now.get(domain, {}):
                # Removing an old YAML initial enables HA restoration; adding or
                # changing one would overwrite a user's persisted selection.
                assert "initial" not in now[domain][key] or (
                    now[domain][key]["initial"] == config.get("initial")
                ), key
    print("PASS: existing helper restore semantics retained; only obsolete RCEm Shadow helper retired")

    assets = _load_assets_module()
    for language in ("pl", "en"):
        relative = f"{COMPONENT}/resources/home_assistant/{language}/hoymiles_ems_scheduler.yaml"
        original = old(relative)
        for modified in (False, True):
            with tempfile.TemporaryDirectory(prefix="upgrade-157-") as tmp:
                config = Path(tmp)
                _write_helper_storage(config)
                path = config / "packages/hoymiles_ems_scheduler.yaml"
                path.parent.mkdir()
                installed = original + (b"\n# user customization\n" if modified else b"")
                path.write_bytes(installed)
                hass = FakeHass(config, language)
                hass.store_payload = {"assets": {"packages/hoymiles_ems_scheduler.yaml": hashlib.sha256(original).hexdigest()}}
                written = _run_install(assets, hass)
                if modified:
                    assert path not in written and path.read_bytes() == installed
                else:
                    assert path in written and path.read_bytes() == (ROOT / relative).read_bytes()
                    backup = path.with_name(path.name + ".pre-ems-supervisor-1b3.bak")
                    assert backup.read_bytes() == original
                    assert path not in _run_install(assets, hass)
                shared = config / "packages/hoymiles_ems_shared_inputs.yaml"
                assert shared.read_bytes() == (ROOT / COMPONENT / "resources/home_assistant" / language / shared.name).read_bytes()
    print("PASS: actual 1.5.7 PL/EN packages update with exact backup, repeat safely, preserve custom edits")

    states = _unmigrated_states()
    for field in M.MIGRATION_FIELD_SPECS:
        domain, key = field.legacy_entity_id.split(".", 1)
        assert key in legacy[domain], field.legacy_entity_id
    plan = M.plan_copy_once(M.MigrationLedger.empty(), states, now=datetime.now(timezone.utc))
    assert len(plan.writes) == 7
    for write in plan.writes:
        states[write.target_entity_id] = write.value
    resumed = M.plan_copy_once(plan.ledger, states, now=datetime.now(timezone.utc))
    assert not resumed.writes and resumed.ledger.result == "complete"
    assert all(states[f.legacy_entity_id] == _unmigrated_states()[f.legacy_entity_id] for f in M.MIGRATION_FIELD_SPECS)
    print("PASS: seven actual legacy settings copied once; restart is idempotent and legacy values preserved")


if __name__ == "__main__":
    main()
