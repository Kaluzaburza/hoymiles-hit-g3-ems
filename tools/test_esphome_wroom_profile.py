"""Validate the opt-in WROOM build without changing the inverter contract."""
from pathlib import Path
import unittest

from esphome.config import read_config
from esphome.core import CORE

ROOT = Path(__file__).resolve().parents[1]


def read(name):
    CORE.reset()
    CORE.config_path = ROOT / 'tools' / name
    config = read_config({})
    assert config is not None
    return config


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    if hasattr(value, 'is_manual') and not value.is_manual:
        # The added log lambda shifts auto-generated action IDs, not behavior.
        return ('generated_id', str(value.type), value.is_declaration)
    return str(value)


class WroomProfile(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = read('esphome_verify_ci.yaml')
        cls.actual = read('esphome_verify_wroom.yaml')

    def test_classic_target_does_not_assume_new_silicon(self):
        esp = self.actual['esp32']
        self.assertEqual(esp['board'], 'esp32dev')
        self.assertEqual(esp['variant'], 'ESP32')
        advanced = esp['framework']['advanced']
        self.assertEqual(str(advanced['minimum_chip_revision']), '0.0')
        self.assertFalse(advanced['sram1_as_iram'])
        self.assertNotIn('psram', self.actual)

    def test_serial_polling_and_authority_unchanged(self):
        for key in ('uart', 'modbus', 'modbus_controller', 'api', 'ota',
                    'sensor', 'text_sensor', 'number', 'select', 'switch',
                    'button', 'globals', 'script', 'wifi'):
            self.assertEqual(normalized(self.actual.get(key)),
                             normalized(self.base.get(key)), key)

    def test_only_one_bounded_log_interval_added(self):
        before = self.base['interval']
        after = self.actual['interval']
        self.assertEqual(len(after), len(before) + 1)
        diag = [x for x in after if 'wroom_diag' in str(x)]
        self.assertEqual(len(diag), 1)
        self.assertEqual(diag[0]['interval'].total_milliseconds, 60000)
        self.assertEqual(diag[0]['startup_delay'].total_milliseconds, 15000)
        self.assertEqual([str(x) for x in after if x not in diag],
                         [str(x) for x in before])

    def test_running_elf_hash_is_not_truncated_by_idf(self):
        # esp_app_get_elf_sha256 caps output at this SDK setting even with
        # a 65-byte destination. The host default produced only nine digits.
        options = self.actual['esp32']['framework']['sdkconfig_options']
        self.assertEqual(str(options.get('CONFIG_APP_RETRIEVE_LEN_ELF_SHA')), '64')


if __name__ == '__main__':
    unittest.main(verbosity=2)
