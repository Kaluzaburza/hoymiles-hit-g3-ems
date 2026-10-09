"""Verify exact local RC2 bytes and scoped test evidence, never field acceptance."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
BASE = "0fd50b7a3c088f8560bc4e4693ca22a1099c4be2"
REQUIRED = {
    "test_rce_optimizer.py", "test_rce_history.py", "test_tariff_profiles.py",
    "test_tariff_price_schedule.py", "test_tariff_optimizer.py",
    "test_tariff_sensor_cadence.py", "test_diagnostics.py",
    "test_diagnostic_analyzer.py", "test_execution_history.py",
    "test_pstryk_rce_parity.py", "test_pstryk_joint.py", "test_pstryk_offline.py",
    "test_pstryk_ui.js", "test_pstryk_idle_readiness_ui.js",
    "test_supervisor_aurora_ui_contract.js", "test_automation_matrix.py",
    "test_optimizer_executor_contract.py", "test_optimizer_startup_contract.py",
    "test_rce_post_command_settling_sensor.py",
}


def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    m = json.loads(args.manifest.read_text(encoding="utf-8"))
    assert m["base_sha"] == BASE
    assert m["commit"] == git("rev-parse", "HEAD")
    assert m["tree"] == git("rev-parse", "HEAD^{tree}")
    assert not git("status", "--porcelain"), "Candidate worktree is dirty"
    subprocess.run(["git", "merge-base", "--is-ancestor", BASE, m["commit"]], cwd=ROOT, check=True)
    assert not git("diff", "--name-only", BASE, "HEAD", "--", "packages", "AGENTS.md"), "Firmware/guide drift"
    assert m["frontend_revision"] == 107 and m["firmware_protocol"] == 2
    assert "FRONTEND_ASSET_REVISION = 107" in (ROOT / "custom_components/hoymiles_hit_modbus/assets.py").read_text()
    assert len(m["runtime"]) == 110
    for path, record in m["runtime"].items():
        source = (ROOT / record["source"]).resolve()
        assert source.is_relative_to(ROOT.resolve()), "Source escapes repository"
        assert digest(source) == record["sha256"], path
    results = {row["test"]: row for row in m["test_results"]}
    assert REQUIRED <= results.keys()
    for name in REQUIRED:
        row = results[name]
        assert row["exit_code"] == 0, name
        log = (args.manifest.parent / row["log"]).resolve()
        assert log.is_relative_to(args.manifest.parent.resolve())
        assert digest(log) == row["log_sha256"], name
    assert m["public_release"] is False and m["field_acceptance"] == "PENDING"
    print("PASS: exact RC2 candidate and scoped offline evidence; host/field acceptance separate")


if __name__ == "__main__":
    main()
