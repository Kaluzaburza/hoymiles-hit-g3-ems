"""Stable promotion exercises the real installer on published RC2 PL/EN assets."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from test_supervisor_helpers_contract import FakeHass, ROOT, _load_assets_module, _run_install, _write_helper_storage

BASE = 'ba91261597420ea3a58a6665fc996d7081f219e2'
COMPONENT = 'custom_components/hoymiles_hit_modbus'


def old(path):
    return subprocess.check_output(['git', 'show', BASE+':'+path], cwd=ROOT)


def main():
    for name in ('config_flow.py', 'entity.py', 'entity_catalog.json', 'ems_shared_input_migration.py',
                 'ems_initial_defaults.py', 'supervisor_active_controller.py', 'supervisor_control_lease.py'):
        assert old(COMPONENT+'/'+name).decode().replace('\r\n', '\n') == (ROOT/COMPONENT/name).read_text(encoding='utf-8'), name
    assets = _load_assets_module()
    for language in ('pl', 'en'):
        relative = f'{COMPONENT}/resources/home_assistant/{language}/hoymiles_ems_scheduler.yaml'
        original = old(relative)
        current = (ROOT/relative).read_bytes()
        assert original.replace(b'\r\n', b'\n').replace(b'1.5.8rc2', b'1.5.8') == current.replace(b'\r\n', b'\n')
        for modified in (False, True):
            with tempfile.TemporaryDirectory(prefix='upgrade-rc2-') as temp:
                config = Path(temp)
                _write_helper_storage(config)
                storage = {p.name: p.read_bytes() for p in (config/'.storage').iterdir() if p.is_file()}
                path = config/'packages/hoymiles_ems_scheduler.yaml'
                path.parent.mkdir()
                installed = original+(b'\n# preserved user customization\n' if modified else b'')
                path.write_bytes(installed)
                hass = FakeHass(config, language)
                hass.store_payload = {'assets': {'packages/hoymiles_ems_scheduler.yaml': hashlib.sha256(original).hexdigest()}}
                written = _run_install(assets, hass)
                if modified:
                    assert path not in written and path.read_bytes() == installed
                else:
                    assert path in written and path.read_bytes() == current
                    assert path.with_name(path.name+'.pre-ems-supervisor-1b3.bak').read_bytes() == original
                    assert path not in _run_install(assets, hass)
                assert storage == {p.name: p.read_bytes() for p in (config/'.storage').iterdir() if p.is_file()}
    print('PASS: RC2 PL/EN stable migration, backup, idempotence, custom YAML, settings and identity preserved')


if __name__ == '__main__':
    main()
