"""Exercise real asset installation over stable 1.5.8 without replacing settings."""
import hashlib
from pathlib import Path
import subprocess
import tempfile

from test_supervisor_helpers_contract import FakeHass, ROOT, _load_assets_module, _run_install, _write_helper_storage

BASE = 'dccf6629d695bf858ad33d9aa353799348f6621e'
COMPONENT = 'custom_components/hoymiles_hit_modbus'


def old(path):
    return subprocess.check_output(['git', 'show', BASE+':'+path], cwd=ROOT)


def main():
    assets = _load_assets_module()
    for language in ('pl', 'en'):
        scheduler = f'{COMPONENT}/resources/home_assistant/{language}/hoymiles_ems_scheduler.yaml'
        original = old(scheduler)
        assert original.replace(b'\r\n', b'\n') == (ROOT/scheduler).read_bytes().replace(b'\r\n', b'\n')
        for modified in (False, True):
            with tempfile.TemporaryDirectory(prefix='upgrade-158-') as temp:
                config = Path(temp)
                _write_helper_storage(config)
                storage = {p.name: p.read_bytes() for p in (config/'.storage').iterdir() if p.is_file()}
                path = config/'packages/hoymiles_ems_scheduler.yaml'
                path.parent.mkdir()
                installed = original + (b'\n# preserved customization\n' if modified else b'')
                path.write_bytes(installed)
                card = config/'www/hoymiles-rce-chart-card.js'
                card.parent.mkdir()
                old_card = old(COMPONENT+'/resources/www/hoymiles-rce-chart-card.js')
                card.write_bytes(old_card)
                hass = FakeHass(config, language)
                hass.store_payload = {'assets': {
                    'packages/hoymiles_ems_scheduler.yaml': hashlib.sha256(original).hexdigest(),
                    'www/hoymiles-rce-chart-card.js': hashlib.sha256(old_card).hexdigest(),
                }}
                written = _run_install(assets, hass)
                assert path not in written and path.read_bytes() == installed
                guide = config/'www/hoymiles-update-guide.js'
                assert guide in written and card in written
                assert guide.read_bytes() == (ROOT/COMPONENT/'resources/www/hoymiles-update-guide.js').read_bytes()
                assert card.read_bytes() == (ROOT/COMPONENT/'resources/www/hoymiles-rce-chart-card.js').read_bytes()
                assert guide not in _run_install(assets, hass)
                assert storage == {p.name: p.read_bytes() for p in (config/'.storage').iterdir() if p.is_file()}
    print('PASS: stable 1.5.8 PL/EN upgrade; guide delivered; scheduler/custom YAML/settings unchanged; idempotent')


if __name__ == '__main__':
    main()
