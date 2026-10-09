"""Build and verify the stable 1.5.8 source ZIP; never publish or deploy it."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import zipfile
from stable_release_contract import validate, BASE_SHA, RUNTIME_SOURCE_SHA


ROOT = Path(__file__).resolve().parents[1]
NAME = "1.5.8"



def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


def require(ok: bool, reason: str) -> None:
    if not ok:
        raise RuntimeError(reason)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    output = args.output_dir.resolve()
    require(not output.is_relative_to(ROOT.resolve()), "Output must be outside the checkout")
    require(not git("status", "--porcelain").strip(), "Candidate worktree is dirty")
    contract = validate(ROOT)
    require(git("rev-parse", "--show-object-format").strip() == b"sha1", "Unsupported Git object format")
    commit = git("rev-parse", "HEAD").decode().strip()
    tree = git("rev-parse", "HEAD^{tree}").decode().strip()
    provenance = json.loads((ROOT/'tools/release_manifests/rc2_public_provenance.json').read_text())
    version = json.loads(git("show", commit + ":custom_components/hoymiles_hit_modbus/manifest.json"))["version"]
    require(version == "1.5.8", "Unexpected integration version")
    assets = git("show", commit + ":custom_components/hoymiles_hit_modbus/assets.py")
    revision = contract["frontend_revision"]
    require(f"FRONTEND_ASSET_REVISION = {revision}".encode() in assets, "Unexpected frontend revision")
    tracked = {}
    for entry in git("ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, kind, oid = header.decode().split()
        path = raw_path.decode("utf-8")
        require(kind == "blob" and mode in {"100644", "100755"}, "Unsupported tree entry: " + path)
        require(not path.startswith("/") and ".." not in path.split("/"), "Unsafe tree path")
        tracked[path] = {"git_blob": oid, "git_mode": mode}
    output.mkdir(parents=True, exist_ok=True)
    archive = output / (NAME + ".zip")
    manifest_path = output / (NAME + ".manifest.json")
    require(not archive.exists() and not manifest_path.exists(), "Output already exists; retain earlier evidence")
    # Archive conversion must not inherit a Windows checkout's CRLF preference.
    subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "core.eol=lf",
                    "archive", "--format=zip", "--prefix=" + NAME + "/",
                    "--output=" + str(archive), commit], cwd=ROOT, check=True)
    with zipfile.ZipFile(archive) as package:
        require(package.testzip() is None, "ZIP CRC failure")
        members = [item for item in package.infolist() if not item.is_dir()]
        require(len(members) == len(tracked), "ZIP member count differs from commit")
        require(len({item.filename for item in members}) == len(members), "Duplicate ZIP member")
        require({item.filename for item in members} == {NAME + "/" + p for p in tracked},
                "ZIP paths differ from commit (including export-ignore rules)")
        for item in members:
            path = item.filename.removeprefix(NAME + "/")
            data = package.read(item)
            blob_id = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
            require(blob_id == tracked[path]["git_blob"], "ZIP bytes differ from commit: " + path)
            tracked[path].update(bytes=len(data), sha256=sha256(data))
    for path, expected in provenance["runtime_files"].items():
        require(tracked[path]["sha256"] == contract["files"][path]["sha256"], "Archive runtime differs from sealed stable source: " + path)
    require(git("rev-parse", "HEAD").decode().strip() == commit, "HEAD changed during packaging")
    require(not git("status", "--porcelain").strip(), "Worktree changed during packaging")
    result = {
        "schema": 1, "name": NAME, "branch": git("branch", "--show-current").decode().strip(), "commit": commit, "tree": tree,
        "source_commit": RUNTIME_SOURCE_SHA, "source_tree": git("rev-parse", RUNTIME_SOURCE_SHA+"^{tree}").decode().strip(),
        "public_base": BASE_SHA, "runtime_matches_source_except_explicit_version_metadata": True,
        "compatible_firmware_tag": "v1.5.8RC2", "lease_protocol": 2,
        "integration_version": version, "frontend_version": f"{version}.{revision}",
        "archive": archive.name, "archive_bytes": archive.stat().st_size,
        "archive_sha256": sha256(archive.read_bytes()), "files": tracked,
        "archive_verification": "PASS",
    }
    manifest_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items() if key != "files"}, indent=2))
    print(f"Verified {len(tracked)} files against commit blobs; privacy and release gates are separate.")


if __name__ == "__main__":
    main()
