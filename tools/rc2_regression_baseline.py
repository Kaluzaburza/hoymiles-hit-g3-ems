"""Read exact regression source bytes without publishing private Git history."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read_source(revision: str, name: str) -> bytes:
    manifest = json.loads((ROOT / "tools/release_manifests/rc2_regression_baselines.json").read_text())
    rows = [row for row in manifest["files"]
            if revision in (row["revision"], row["commit"])
            and row["source_path"] == "custom_components/hoymiles_hit_modbus/" + name]
    if len(rows) != 1:
        raise ValueError("Unknown regression source")
    row = rows[0]
    path = (ROOT / row["fixture"]).resolve()
    if not path.is_relative_to((ROOT / "tools/release_fixtures/rc2_baselines").resolve()):
        raise ValueError("Regression source outside fixture directory")
    data = path.read_bytes()
    # Git may check text out with CRLF; the manifest pins canonical Git bytes.
    if hashlib.sha256(data).hexdigest() != row["sha256"]:
        data = data.replace(b"\r\n", b"\n")
    if len(data) != row["bytes"] or hashlib.sha256(data).hexdigest() != row["sha256"]:
        raise ValueError("Regression source hash differs")
    return data
