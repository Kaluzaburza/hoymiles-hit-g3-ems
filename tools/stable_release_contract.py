"""Exact current stable 1.5.8 contract, separate from immutable historical candidates.

The manifest covers every tracked byte/mode. Only its own hash literal in this
file is masked to resolve self-reference. The archive records the final SHA.
Re-sealing requires an explicit reviewed local change, never validator output.
"""
from __future__ import annotations
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tarfile

ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = 'tools/release_manifests/stable_1_5_8_contract.json'
SELF_PATH = 'tools/stable_release_contract.py'
BASE_SHA = '6617bc4de6592439ea2c64889b0a25bbe5bfa45e'
PARENT_SHA = 'fec6005633388525e9cd21e4a9114db60761fcb4'
SUBJECT = 'fix(release): route frontend checks through stable contract'
MANIFEST_SHA256 = 'f553139002867e0c6138d5df36a4e61b21fd22283c3fabe12855339c4c1c5973'
PUBLIC_BASE = '6617bc4de6592439ea2c64889b0a25bbe5bfa45e'
RUNTIME_SOURCE_SHA = 'c919ed9fbe8858d28f0dce0650ccfdb65af8b31c'
HISTORICAL_PUBLIC = RUNTIME_SOURCE_SHA


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def git(root, *args):
    return subprocess.check_output(['git', *args], cwd=root, stderr=subprocess.PIPE)


def digest(path, data):
    if path == SELF_PATH:
        data, count = re.subn(rb"MANIFEST_SHA256 = '[0-9a-f]{64}'",
                            b"MANIFEST_SHA256 = '" + b'0'*64 + b"'", data)
        require(count == 1, 'Manifest pin masking is not exactly one literal')
    return hashlib.sha256(data).hexdigest()


def verify_snapshot(manifest_bytes, files, *, parent, subject, pin=MANIFEST_SHA256):
    require(hashlib.sha256(manifest_bytes).hexdigest() == pin, 'stable 1.5.8 manifest pin differs')
    manifest = json.loads(manifest_bytes)
    require(set(manifest) == {'schema', 'base', 'parent', 'subject', 'version', 'frontend_revision', 'files', 'branch_paths'},
            'stable 1.5.8 manifest schema differs')
    require(manifest['schema'] == 1 and manifest['base'] == BASE_SHA and
            manifest['parent'] == parent == PARENT_SHA and manifest['subject'] == subject == SUBJECT,
            'stable 1.5.8 parent/subject/base differs')
    require(manifest['version'] == '1.5.8' and manifest['frontend_revision'] == 122,
            'stable 1.5.8 version differs')
    require(set(files) == set(manifest['files']), 'stable 1.5.8 tracked path set differs')
    for path, (mode, data) in files.items():
        require(mode in {'100644', '100755'} and not path.startswith('/') and '..' not in path.split('/'),
                'Unsafe stable 1.5.8 path/mode: ' + path)
        require(manifest['files'][path] == {'mode': mode, 'sha256': digest(path, data)},
                'stable 1.5.8 bytes/mode differ: ' + path)
    return manifest


def validate(root=ROOT):
    require(not git(root, 'status', '--porcelain=v1', '--untracked-files=all').strip(), 'stable 1.5.8 checkout is dirty')
    require(all(line.startswith('H ') for line in git(root, 'ls-files', '-v').decode().splitlines()),
            'stable 1.5.8 index flags mask files')
    git(root, 'merge-base', '--is-ancestor', BASE_SHA, 'HEAD')
    parent = git(root, 'show', '-s', '--format=%P', 'HEAD').decode().strip()
    subject = git(root, 'show', '-s', '--format=%s', 'HEAD').decode().strip()
    files = {}
    for entry in git(root, 'ls-tree', '-rz', 'HEAD').split(b'\0'):
        if not entry:
            continue
        header, path = entry.split(b'\t', 1)
        mode, kind, oid = header.decode().split()
        path = path.decode()
        require(kind == 'blob', 'stable 1.5.8 submodule/tree not allowed')
        if path == CONTRACT_PATH:
            require(mode == '100644', 'stable 1.5.8 manifest mode differs')
        data = (root/path).read_bytes()
        def blob_id(value):
            return hashlib.sha1(b'blob '+str(len(value)).encode()+b'\0'+value).hexdigest()
        if blob_id(data) != oid:
            data = data.replace(b'\r\n', b'\n')
        require(blob_id(data) == oid, 'stable 1.5.8 checkout bytes differ: ' + path)
        if path != CONTRACT_PATH:
            files[path] = (mode, data)
    manifest = verify_snapshot((root/CONTRACT_PATH).read_bytes().replace(b'\r\n', b'\n'), files,
                               parent=parent, subject=subject)
    require(git(root, 'diff', '--name-only', PUBLIC_BASE, 'HEAD').decode().splitlines() == manifest['branch_paths'],
            'stable 1.5.8 public-base branch manifest differs')
    provenance = json.loads((root/'tools/release_manifests/rc2_public_provenance.json').read_text())
    require(provenance['kind'] == 'public_snapshot' and provenance['public_base'] == PUBLIC_BASE,
            'Public provenance differs')
    verify_runtime(files, root)

    return manifest


VERSION_ONLY_FILES = {
    'custom_components/hoymiles_hit_modbus/const.py': 2,
    'custom_components/hoymiles_hit_modbus/manifest.json': 1,
    'custom_components/hoymiles_hit_modbus/resources/home_assistant/en/hoymiles_ems_scheduler.yaml': 1,
    'custom_components/hoymiles_hit_modbus/resources/home_assistant/pl/hoymiles_ems_scheduler.yaml': 1,
    'home_assistant/hoymiles_ems_scheduler.yaml': 1,
}


def verify_runtime(files, root=ROOT):
    """Allow exactly the five reviewed version-string changes, no logic drift."""
    provenance = json.loads((root/'tools/release_manifests/rc2_public_provenance.json').read_text())
    require(set(VERSION_ONLY_FILES) <= set(provenance['runtime_files']), 'Version path lacks RC2 provenance')
    for path, expected in provenance['runtime_files'].items():
        original = git(root, 'show', RUNTIME_SOURCE_SHA+':'+path)
        require(hashlib.sha256(original).hexdigest() == expected, 'RC2 source provenance differs: '+path)
        if path in VERSION_ONLY_FILES:
            require(original.count(b'1.5.8rc2') == VERSION_ONLY_FILES[path], 'Unexpected version occurrence: '+path)
            original = original.replace(b'1.5.8rc2', b'1.5.8')
        require(path in files and files[path][1] == original, 'Runtime promotion differs: '+path)


def historical_contract(legacy):
    """Run unchanged parent RC2 checks; its structural gate retains public 1.5.7."""
    with tempfile.TemporaryDirectory(prefix='rc2-historical-') as temp:
        root = Path(temp)/'repo'
        subprocess.run(['git', 'clone', '--quiet', '--no-local', '--no-hardlinks', '--single-branch',
                        '--no-tags', '--no-checkout', str(ROOT), str(root)], check=True)
        git(root, 'config', 'core.autocrlf', 'false')
        git(root, 'checkout', '--quiet', '--detach', HISTORICAL_PUBLIC)
        # Restore the already published tag identity only inside the disposable
        # historical fixture; no candidate/release tag or remote mutation.
        git(root, 'update-ref', 'refs/tags/v1.5.7', PUBLIC_BASE)
        require(not (root/'.git/objects/info/alternates').exists(), 'Historical fixture uses alternates')
        # Public history contains 1.5.7, not workstation-only candidate commits.
        # Their original validators and private results remain separately scoped.
        for script in ('validate_release.py', 'test_rc2_release_contract.py', 'test_rc2_public_snapshot.py'):
            run = subprocess.run([sys.executable, '-B', 'tools/'+script], cwd=root,
                                 capture_output=True, text=True, encoding='utf-8',
                                 env=dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8',
                                          PYTHONDONTWRITEBYTECODE='1'))
            require(run.returncode == 0, 'Historical RC2 gate failed: '+script+'\n'+run.stdout+run.stderr)
            print(run.stdout, end='')


def structural(legacy):
    """Current metadata/assets + historical gates; behavior has its full CI suite."""
    manifest = validate()
    component = ROOT/'custom_components/hoymiles_hit_modbus'
    require([p.name for p in (ROOT/'custom_components').iterdir() if p.is_dir()] == ['hoymiles_hit_modbus'],
            'HACS must contain one integration')
    meta = json.loads((component/'manifest.json').read_text())
    require(meta['version'] == manifest['version'] and meta['domain'] == component.name, 'Integration metadata differs')
    require(meta['documentation'] == 'https://github.com/Kaluzaburza/hoymiles-hit-g3-ems' and
            meta['issue_tracker'] == 'https://github.com/Kaluzaburza/hoymiles-hit-g3-ems/issues', 'Public metadata differs')
    require(json.loads((ROOT/'hacs.json').read_text())['name'] == meta['name'], 'HACS name differs')
    const = (component/'const.py').read_text(encoding='utf-8')
    require('VERSION = "1.5.8"' in const, 'Python version differs')
    catalog = json.loads((component/'entity_catalog.json').read_text(encoding='utf-8'))
    require(len(catalog) >= 250, 'Entity catalog is incomplete')
    for language in ('en', 'pl'):
        translations = json.loads((component/'translations'/f'{language}.json').read_text(encoding='utf-8'))
        for row in catalog:
            require(row['translation_key'] in translations['entity'][row['domain']], 'Missing catalog translation')
        dashboard = json.loads((component/'resources/www'/f'dashboard_hoymiles_{language}.json').read_text(encoding='utf-8'))
        require(len(dashboard['views']) == 21 and len([view for view in dashboard['views'] if not view.get('subview')]) == 7,
                'stable 1.5.8 requires 21 views including seven visible tabs')
    legacy.validate_managed_asset_freshness(catalog)
    legacy.validate_protected_assets_ast((component/'assets.py').read_text(encoding='utf-8'))
    anonymous = json.loads((ROOT/'tools/release_manifests/rc2_anonymous_frontend_fixture.json').read_text())
    archive = ROOT/anonymous['archive']
    require(hashlib.sha256(archive.read_bytes()).hexdigest() == anonymous['public_archive_sha256'],
            'Anonymous historical fixture archive differs')
    with tarfile.open(archive) as package:
        members = {member.name: member for member in package.getmembers() if member.isfile()}
        require(set(members) == {item['path'] for item in anonymous['members']}, 'Anonymous fixture member set differs')
        for item in anonymous['members']:
            data = package.extractfile(members[item['path']]).read()
            require(len(data) == item['size_bytes'] and hashlib.sha256(data).hexdigest() == item['public_sha256'],
                    'Anonymous fixture member differs: '+item['path'])
    for path in component.glob('*.py'):
        ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
    for name in ('icon.png', 'icon@2x.png', 'dark_icon.png', 'dark_icon@2x.png', 'logo.png', 'dark_logo.png'):
        path = component/'brand'/name
        width, height = legacy.png_dimensions(path)
        require(width >= 128 and height >= 128, 'Brand image too small: '+name)
    for path in (ROOT/'CHANGELOG.md', ROOT/'docs/releases/v1.5.8.md'):
        require('### User update steps / Kroki po aktualizacji' in path.read_text(encoding='utf-8'),
                'Release lacks bilingual update steps')
    import yaml
    workflow = yaml.safe_load((ROOT/'.github/workflows/validate.yml').read_text(encoding='utf-8'))
    require({'validate-hacs', 'hassfest', 'project-checks', 'firmware-compile'} <= set(workflow['jobs']), 'CI job missing')
    for job in workflow['jobs'].values():
        require(not job.get('continue-on-error', False), 'CI job is warning-only')
        for step in job.get('steps', []):
            require(not step.get('continue-on-error', False), 'CI step is warning-only')
    workflow_text = (ROOT/'.github/workflows/validate.yml').read_text(encoding='utf-8')
    for command in ('python tools/test_lease_acceptance_journal.py', 'python tools/test_stable_release_contract.py',
                    'python tools/test_diagnostics.py', 'node tools/validate_rce_card.js'):
        require(command in workflow_text, 'Mandatory stable 1.5.8 CI command absent: '+command)
    historical_contract(legacy)
    print(f'stable 1.5.8 current structural contract: PASS ({len(manifest["files"])+1} tracked files, exact hashes/modes, public baseline gate retained)')
    return 0


if __name__ == '__main__':
    print(json.dumps(validate(), sort_keys=True))
