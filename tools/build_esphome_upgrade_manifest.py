"""Generate immutable accepted official ESP package hashes, not private YAML."""
from pathlib import Path
import hashlib
import json
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[1]
FIRMWARE = "ba91261597420ea3a58a6665fc996d7081f219e2"
LEGACY = "e995f9b730eaecdfd07fd84ccff34108e2799f14"
files = yaml.load((ROOT / "hoymiles-inverter.yaml").read_text(encoding="utf-8"), Loader=yaml.BaseLoader)["packages"]["hoymiles_hit_g3"]["files"]
data = {"firmware_sha": FIRMWARE,
        "accepted_refs": ["v1.5.6", "v1.5.7", "v1.5.8RC2", "v1.5.8", FIRMWARE, LEGACY],
        "source_commits": [LEGACY, FIRMWARE], "files": {}}
for name in files:
    data["files"][name] = sorted({hashlib.sha256(subprocess.check_output(
        ["git", "show", f"{commit}:{name}"], cwd=ROOT).replace(b"\r\n", b"\n")).hexdigest()
        for commit in data["source_commits"]})
path = ROOT / "custom_components/hoymiles_hit_modbus/esphome_upgrade_manifest.json"
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8", newline="\n")
print(f"Generated {len(files)} accepted package paths from immutable public commits")
