"""Public-only ancestry, unchanged runtime and pinned regression-fixture checks."""
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import stable_release_contract as contract
import rc2_regression_baseline as baseline


def main():
    contract.validate()
    provenance = json.loads((contract.ROOT / "tools/release_manifests/rc2_public_provenance.json").read_text())
    commits = subprocess.check_output(
        ["git", "rev-list", "HEAD", "^" + provenance["public_base"]],
        cwd=contract.ROOT, text=True).splitlines()
    assert provenance["source_commit"] not in commits
    assert commits and subprocess.check_output(
        ["git", "show", "-s", "--format=%P", commits[-1]],
        cwd=contract.ROOT, text=True).strip() == provenance["public_base"]
    manifest = json.loads((contract.ROOT / "tools/release_manifests/rc2_regression_baselines.json").read_text())
    for row in manifest["files"]:
        name = row["source_path"].rsplit("/", 1)[-1]
        assert len(baseline.read_source(row["commit"], name)) == row["bytes"]
    try:
        baseline.read_source("unknown", "execution_history.py")
    except ValueError:
        pass
    else:
        raise AssertionError("Unknown fixture accepted")
    original = baseline.ROOT
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        for relative in ["tools/release_manifests/rc2_regression_baselines.json", manifest["files"][0]["fixture"]]:
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(original / relative, target)
        row = manifest["files"][0]
        with (root / row["fixture"]).open("ab") as file:
            file.write(b"\n# tampered\n")
        baseline.ROOT = root
        try:
            try:
                baseline.read_source(row["commit"], row["source_path"].rsplit("/", 1)[-1])
            except ValueError:
                pass
            else:
                raise AssertionError("Tampered fixture accepted")
        finally:
            baseline.ROOT = original
    print(f"PASS: public ancestry, {len(provenance['runtime_files'])} runtime files verified with explicit version-only promotion, four exact fixtures and two negative controls")


if __name__ == "__main__":
    main()
