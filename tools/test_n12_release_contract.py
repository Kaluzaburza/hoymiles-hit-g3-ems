"""Mutation regressions for the exact EMS 1.5.8 N12 release contract."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "tools" / "validate_release.py"
N12_STATE = "N12_EXACT_LOCAL_BASE"
RELEASE_STATE = "V158_RELEASE_CANDIDATE"
TASK02_STATE = "V158_TASK02_CANDIDATE"
HISTORICAL_STATE = "CONSOLIDATED_RELEASE_CANDIDATE"
HISTORICAL_REF = "1c20cb9d7a9c09ea45050ba294c5f18fac14dae1"
N12_ACCEPTED_REF = "c736b69d985d6f2a75015abdacb8839a461ac7f1"
CANDIDATE_BASE_REF = "96fc91b7bc6c4b258e654958c2ecf2400438ae74"
UNREACHABLE_LOCAL_REF = "f2fbfb89af7bb239092aef55d079e15a3be664dc"
RUNTIME_PATH = "custom_components/hoymiles_hit_modbus/rce_optimizer.py"


checks = 0


def check(condition: bool, message: str) -> None:
    """Count one exact contract assertion."""

    global checks
    if not condition:
        raise AssertionError(message)
    checks += 1


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> str:
    """Run Git with deterministic local identity in a disposable clone."""

    environment = os.environ.copy()
    environment.update(
        {
            "GIT_AUTHOR_NAME": "N12 Contract Test",
            "GIT_AUTHOR_EMAIL": "n12@example.invalid",
            "GIT_COMMITTER_NAME": "N12 Contract Test",
            "GIT_COMMITTER_EMAIL": "n12@example.invalid",
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
    )
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=environment,
        input=input_bytes,
        check=True,
        capture_output=True,
    )
    return completed.stdout.decode("utf-8", errors="strict").strip()


@contextmanager
def clone_at(reference: str = "HEAD"):
    """Clone through transport without alternates, hardlinks or private objects."""

    directory = Path(tempfile.mkdtemp(prefix="n12-contract-"))
    repo = directory / "repo"
    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--quiet",
                "--no-local",
                "--no-hardlinks",
                "--no-checkout",
                str(ROOT),
                str(repo),
            ],
            check=True,
            capture_output=True,
        )
        # A portable fixture must not inherit a Windows user's checkout policy.
        # Persist the policy before checkout so the subsequent clean-tree gate
        # observes the transported blobs rather than an autocrlf rewrite.
        git(repo, "config", "core.autocrlf", "false")
        git(repo, "checkout", "--quiet", "--detach", reference)
        check(not git(repo, "status", "--porcelain=v1"), f"fixture is dirty at {reference}")
        yield repo
    finally:
        def remove_readonly(function, path, _exception):
            os.chmod(path, stat.S_IWRITE)
            function(path)

        shutil.rmtree(directory, ignore_errors=False, onexc=remove_readonly)


def classify(repo: Path, validator: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run only the real classifier from the selected validator source."""

    source = validator or repo / "tools" / "validate_release.py"
    code = (
        "import importlib.util, pathlib, sys; "
        f"spec=importlib.util.spec_from_file_location('n12_validator', {json.dumps(str(source))}); "
        "module=importlib.util.module_from_spec(spec); sys.modules[spec.name]=module; "
        "spec.loader.exec_module(module); "
        f"module.ROOT=pathlib.Path({json.dumps(str(repo))}); "
        "print(module.validate_current_integrated_manifests())"
    )
    return subprocess.run(
        [sys.executable, "-B", "-c", code],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


def commit_all(repo: Path, message: str) -> None:
    """Commit one mutation so rejection cannot rely on a dirty worktree."""

    git(repo, "add", "-A")
    git(repo, "commit", "--quiet", "-m", message)
    check(not git(repo, "status", "--porcelain=v1"), f"mutation commit is dirty: {message}")


def rejected(repo: Path, label: str, validator: Path | None = None) -> None:
    """Require fail-closed classification for one mutation."""

    result = classify(repo, validator)
    check(result.returncode != 0, f"N12 mutation survived: {label}\n{result.stdout}{result.stderr}")


def main() -> int:
    """Exercise exact acceptance, history and all required fail-closed mutations."""

    with clone_at() as repo:
        result = classify(repo)
        check(
            result.returncode == 0 and result.stdout.strip() == TASK02_STATE,
            f"exact reviewed Task 02 candidate rejected\n{result.stdout}{result.stderr}",
        )
        work_state = (repo / "docs" / "WORK_STATE.MD").read_text(encoding="utf-8")
        check(
            "G3_LOCAL = CLOSED / PASS WITH ACCEPTED RESIDUAL RISK" in work_state
            and "dokładnej 18-plikowej delty" in work_state
            and "f2fbfb89af7bb239092aef55d079e15a3be664dc" in work_state,
            "corrected release handoff facts are missing from WORK_STATE",
        )
        current_section = work_state.split("Poniższe sekcje N04-N06", 1)[0]
        check(
            "G3_LOCAL = PARTIAL/HOLD" not in current_section
            and "dokładnej 16-plikowej delty" not in current_section
            and "f2fbfb8926a92f531acff5b81de37bc07665c39a" not in current_section,
            "stale release handoff facts survived in the current WORK_STATE section",
        )
        unreachable = subprocess.run(
            ["git", "cat-file", "-e", f"{UNREACHABLE_LOCAL_REF}^{{commit}}"],
            cwd=repo,
            check=False,
            capture_output=True,
        )
        check(
            unreachable.returncode != 0,
            "transport clone unexpectedly contains the private f2fbfb fixture",
        )

    with clone_at(N12_ACCEPTED_REF) as repo:
        result = classify(repo, VALIDATOR)
        check(
            result.returncode == 0 and result.stdout.strip() == N12_STATE,
            f"accepted N12 base changed\n{result.stdout}{result.stderr}",
        )

    with clone_at() as repo:
        path = repo / RUNTIME_PATH
        path.write_bytes(path.read_bytes() + b"\n# n12-byte-mutation\n")
        commit_all(repo, "mutate runtime byte")
        rejected(repo, "runtime byte")

    with clone_at() as repo:
        (repo / RUNTIME_PATH).unlink()
        commit_all(repo, "remove runtime path")
        rejected(repo, "missing path")

    with clone_at() as repo:
        rogue = repo / "custom_components" / "hoymiles_hit_modbus" / "n12_rogue.py"
        rogue.write_text("raise RuntimeError('must never ship')\n", encoding="utf-8")
        git(repo, "add", "--", str(rogue.relative_to(repo)))
        git(repo, "update-index", "--chmod=+x", str(rogue.relative_to(repo)))
        git(repo, "commit", "--quiet", "-m", "add executable runtime path")
        rejected(repo, "extra executable")

    with clone_at() as repo:
        renamed = "custom_components/hoymiles_hit_modbus/rce_optimizer_renamed.py"
        git(repo, "mv", RUNTIME_PATH, renamed)
        commit_all(repo, "rename runtime path")
        rejected(repo, "rename")

    with clone_at() as repo:
        oid = git(repo, "hash-object", "-w", "--stdin", input_bytes=b"rce_sensor.py\n")
        git(repo, "update-index", "--cacheinfo", f"120000,{oid},{RUNTIME_PATH}")
        git(repo, "commit", "--quiet", "-m", "replace runtime path with symlink")
        rejected(repo, "symlink type")

    with clone_at() as repo:
        git(repo, "update-index", "--chmod=+x", RUNTIME_PATH)
        git(repo, "commit", "--quiet", "-m", "change runtime mode")
        rejected(repo, "runtime mode")

    with clone_at() as repo:
        (repo / "README.md").write_bytes((repo / "README.md").read_bytes() + b"\n")
        rejected(repo, "dirty image")

    with clone_at() as repo:
        path = repo / RUNTIME_PATH
        path.write_bytes(path.read_bytes() + b"\n# hidden-assume\n")
        git(repo, "update-index", "--assume-unchanged", RUNTIME_PATH)
        check(not git(repo, "status", "--porcelain=v1"), "assume-unchanged fixture is visible")
        rejected(repo, "assume-unchanged runtime")

    with clone_at() as repo:
        path = repo / RUNTIME_PATH
        path.write_bytes(path.read_bytes() + b"\n# hidden-skip\n")
        git(repo, "update-index", "--skip-worktree", RUNTIME_PATH)
        check(not git(repo, "status", "--porcelain=v1"), "skip-worktree fixture is visible")
        rejected(repo, "skip-worktree runtime")

    with clone_at() as repo:
        manifest = repo / "tools" / "release_manifests" / "n12_v1_5_8.json"
        manifest.write_bytes(manifest.read_bytes().replace(b'"schema": 1', b'"schema": 2', 1))
        commit_all(repo, "mutate N12 manifest")
        rejected(repo, "foreign manifest")

    with clone_at() as repo:
        manifest = repo / "tools" / "release_manifests" / "v1_5_8_release_delta.json"
        manifest.write_bytes(manifest.read_bytes().replace(b'"schema": 1', b'"schema": 2', 1))
        commit_all(repo, "mutate release-delta manifest")
        rejected(repo, "foreign release-delta manifest")

    with clone_at() as repo:
        foreign = repo / "docs" / "N12_FOREIGN.md"
        foreign.write_text("not part of the reviewed delta\n", encoding="utf-8")
        commit_all(repo, "add foreign delta")
        rejected(repo, "foreign diff")

    for relative_path, label in (
        ("CHANGELOG.md", "foreign reviewed changelog"),
        ("docs/WORK_STATE.MD", "foreign reviewed work state"),
        ("README.md", "foreign reviewed English README"),
        ("docs/releases/v1.5.8.md", "foreign reviewed release notes"),
    ):
        with clone_at() as repo:
            path = repo / relative_path
            path.write_bytes(path.read_bytes() + b"\nN12 unreviewed documentation mutation\n")
            commit_all(repo, f"mutate {label}")
            rejected(repo, label)

    with clone_at() as repo:
        tree = git(repo, "rev-parse", "HEAD^{tree}")
        foreign_head = git(repo, "commit-tree", tree, input_bytes=b"foreign root\n")
        git(repo, "checkout", "--quiet", "--detach", foreign_head)
        rejected(repo, "foreign base")

    with clone_at() as repo:
        # 96fc91... is the correct reachable pre-validation candidate, but its
        # historical WORK_STATE blob has one non-canonical final EOL and is
        # therefore dirty after an attributes-compliant checkout. Rebuild only
        # that normalized tree deterministically inside the transport clone.
        git(repo, "checkout", "--quiet", "--detach", CANDIDATE_BASE_REF)
        check(
            git(repo, "status", "--porcelain=v1") == "M docs/WORK_STATE.MD",
            "reachable candidate base has an unexpected checkout delta",
        )
        git(repo, "add", "--", "docs/WORK_STATE.MD")
        tree = git(repo, "write-tree")
        candidate = git(
            repo,
            "commit-tree",
            tree,
            "-p",
            CANDIDATE_BASE_REF,
            input_bytes=b"fixture: normalize reachable pre-N12 candidate\n",
        )
        git(repo, "checkout", "--quiet", "--detach", candidate)
        check(not git(repo, "status", "--porcelain=v1"), "normalized candidate is dirty")
        rejected(repo, "unknown candidate without reviewed validation delta", VALIDATOR)

    with clone_at() as repo:
        git(repo, "checkout", "--quiet", "-b", "release/v1.5.8-fake-authority")
        path = repo / RUNTIME_PATH
        path.write_bytes(path.read_bytes() + b"\n# branch-name-bypass\n")
        commit_all(repo, "try branch-name runtime bypass")
        rejected(repo, "branch name cannot authorize runtime")

    with clone_at(HISTORICAL_REF) as repo:
        result = classify(repo, VALIDATOR)
        check(
            result.returncode == 0 and result.stdout.strip() == HISTORICAL_STATE,
            f"historical consolidated contract changed\n{result.stdout}{result.stderr}",
        )

    for relative_path, label in (
        ("home_assistant/www/hoymiles-rce-chart-card.js", "canonical frontend"),
        (
            "custom_components/hoymiles_hit_modbus/resources/www/hoymiles-rce-chart-card.js",
            "bundled frontend",
        ),
        ("home_assistant/hoymiles_ems_scheduler.yaml", "canonical scheduler"),
        (
            "custom_components/hoymiles_hit_modbus/resources/home_assistant/pl/hoymiles_ems_scheduler.yaml",
            "bundled scheduler",
        ),
    ):
        with clone_at() as repo:
            path = repo / relative_path
            path.write_bytes(path.read_bytes() + b"\n# asset-mismatch\n")
            commit_all(repo, f"mutate {label}")
            rejected(repo, label)

    print(f"N12 release contract: {checks}/{checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
