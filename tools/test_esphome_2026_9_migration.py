"""ESPHome 2026.9 migration: poll budget, raw fault widths and authenticated OTA.

Run in the CI-pinned ESPHome environment. Uses the real full configuration;
does not contact an inverter, rotate a key or upload firmware.
"""
from pathlib import Path
import logging
import unittest

import yaml
from esphome.config import read_config
from esphome.core import CORE

ROOT = Path(__file__).resolve().parents[1]


class MigrationContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.warnings = []
        class Capture(logging.Handler):
            def emit(self, record):
                if record.levelno >= logging.WARNING:
                    cls.warnings.append(record.getMessage())
        handler = Capture()
        logging.getLogger().addHandler(handler)
        try:
            CORE.reset()
            CORE.config_path = ROOT / 'tools/esphome_verify_ci.yaml'
            cls.config = read_config({})
            assert cls.config is not None
        finally:
            logging.getLogger().removeHandler(handler)

    def test_no_retired_yaml_options(self):
        relevant = [w for w in self.warnings if any(s in w for s in
                    ('skip_updates', 'register_count', 'password wastes'))]
        self.assertEqual(relevant, [])

    def test_sparse_addresses_keep_old_ten_cycle_budget(self):
        config = self.config
        controllers = {str(c['id']): c for c in config['modbus_controller']}
        by_address = {int(s['address']): s for s in config['sensor']
                      if s.get('platform') == 'modbus_controller' and int(s.get('address', 0)) in range(6050, 6100, 5)}
        self.assertEqual(set(by_address), set(range(6050, 6100, 5)))
        for address, sensor in by_address.items():
            interval = controllers[str(sensor['modbus_controller_id'])]['update_interval']
            self.assertEqual(interval.total_milliseconds, 1500000, address)
        for cid, milliseconds in [('hoymiles_modbus', 150000),
                                  ('hoymiles_modbus_fast', 13000),
                                  ('hoymiles_modbus_control', 5000),
                                  ('hoymiles_modbus_settings', 20000)]:
            self.assertEqual(controllers[cid]['update_interval'].total_milliseconds, milliseconds)
        for c in controllers.values():
            self.assertEqual(str(c['modbus_id']), 'modbus_1')
            self.assertEqual(c['address'], 1)

    def test_full_width_faults_and_fast_topology_preserved(self):
        source = yaml.safe_load((ROOT/'packages/states_alarms.yaml').read_text(encoding='utf8'))
        faults = [s for s in source['text_sensor'] if s.get('response_size') == 4]
        self.assertEqual(len(faults), 6)
        for s in faults:
            self.assertNotIn('register_count', s)
            self.assertEqual(s['raw_encode'], 'HEXBYTES')
            self.assertIn('item->offset', s['lambda'])
        sensors = {s.get('id'): s for s in yaml.safe_load((ROOT/'packages/parallel_network.yaml').read_text(encoding='utf8'))['sensor']}
        for sid in ('machines_type_6048','number_of_machines_master_and_slave_6049'):
            self.assertEqual(sensors[sid]['modbus_controller_id'], '${modbus_settings_controller_id}')
            self.assertTrue(sensors[sid]['force_update'])

    def test_ota_requires_existing_api_key(self):
        api = self.config['api']['encryption']['key']
        ota = next(c for c in self.config['ota'] if c['platform'] == 'esphome')
        self.assertNotIn('password', ota)
        self.assertEqual(ota['encryption']['key'], api)
        self.assertEqual(ota['version'], 2)


if __name__ == '__main__':
    unittest.main(verbosity=2)
