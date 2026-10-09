"""Offline migration of existing ESP YAML: preserve hardware; never upload."""
import importlib.util
from pathlib import Path
import subprocess
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("esphome_upgrade", ROOT / "custom_components/hoymiles_hit_modbus/esphome_upgrade.py")
upgrade = importlib.util.module_from_spec(spec)
spec.loader.exec_module(upgrade)


class UpgradeTest(unittest.TestCase):
    def setUp(self):
        self.source = (ROOT / "hoymiles-inverter.yaml").read_text(encoding="utf-8")

    def prepare(self, source=None, **kwargs):
        return upgrade.prepare_upgrade(source or self.source, transport="encrypted", **kwargs)

    def test_current_profiles_preserve_hardware_and_secrets(self):
        for path in ("hoymiles-inverter.yaml", "hoymiles-inverter-s3.yaml", "hoymiles-inverter-flow-control.yaml"):
            source = (ROOT / path).read_text(encoding="utf-8")
            result = self.prepare(source)
            self.assertEqual(result["status"], "ready", result)
            before = yaml.load(source, Loader=yaml.BaseLoader)
            after = yaml.load(result["yaml"], Loader=yaml.BaseLoader)
            for key in before.keys() - {"packages", "ota", "dashboard_import"}:
                self.assertEqual(before[key], after[key], key)
            self.assertEqual(result["backup"], source)
            self.assertEqual(after["packages"]["hoymiles_hit_g3"]["ref"], upgrade.FIRMWARE_SHA)
            self.assertIn("encryption:", result["yaml"])
            self.assertEqual(result["validation"], "not_run")

    def test_public_157_remote_migrates_without_board_replacement(self):
        source = subprocess.check_output(["git", "show", "6617bc4de6592439ea2c64889b0a25bbe5bfa45e:hoymiles-inverter.yaml"], cwd=ROOT).decode()
        result = upgrade.prepare_upgrade(source, transport="legacy")
        self.assertEqual(result["status"], "ready", result)
        self.assertIn("encryption: !remove", result["yaml"])
        self.assertIn("password: ${ota_password}", result["yaml"])
        self.assertEqual(result["stage"], "bridge")
        self.assertNotIn("final_yaml", result)
        before = yaml.load(source, Loader=yaml.BaseLoader)
        after = yaml.load(result["yaml"], Loader=yaml.BaseLoader)
        self.assertEqual(before["esp32"], after["esp32"])

    def test_local_packages_require_matching_contents(self):
        start = self.source.index("packages:\n")
        files = upgrade.package_manifest()["files"]
        source = self.source[:start] + "packages:\n" + "".join(f"  p{i}: !include {name}\n" for i, name in enumerate(files))
        self.assertEqual(self.prepare(source)["status"], "need_packages")
        contents = {name: (ROOT / name).read_text(encoding="utf-8") for name in files}
        self.assertEqual(self.prepare(source, local_packages=contents)["status"], "ready")
        contents["packages/overview.yaml"] += "\n# personal modification\n"
        self.assertEqual(self.prepare(source, local_packages=contents)["code"], "modified_packages")

    def test_unknown_firmware_does_not_choose_a_transport(self):
        result = upgrade.prepare_upgrade(self.source, transport="unknown")
        self.assertEqual(result["code"], "transport_unknown")
        self.assertNotIn("yaml", result)

    def test_legacy_requires_existing_password(self):
        self.assertEqual(upgrade.prepare_upgrade(self.source, transport="legacy")["code"], "ota_password_missing")

    def test_ambiguous_custom_or_invalid_inputs_fail_closed(self):
        cases = [
            self.source + "\nsensor:\n  - platform: template\n    name: Custom\n",
            self.source + "\nesp32: {board: esp32dev}\n",
            self.source.replace("https://github.com/Kaluzaburza", "https://example.org/Kaluzaburza"),
            self.source.replace("ref: v1.5.8RC2", "ref: private-custom-branch"),
            self.source.replace('board: "esp32dev"', 'board: "unknown-board"'),
            self.source + "\nextra: &x [*x]\n",
            "---\na: x\n---\na: y\n",
            "x" * 131073,
        ]
        for source in cases:
            result = self.prepare(source)
            self.assertEqual(result["status"], "review_required", result)
            self.assertNotIn("yaml", result)

    def test_final_removes_bridge_without_rotating_keys(self):
        source = self.source.replace("substitutions:\n", "substitutions:\n  ota_password: !secret ota_password\n")
        bridge = upgrade.prepare_upgrade(source, transport="legacy")["yaml"]
        final = self.prepare(bridge)
        self.assertEqual(final["stage"], "final")
        self.assertNotIn("encryption: !remove", final["yaml"])
        self.assertIn("password: !remove", final["yaml"])
        self.assertIn("api_key: !secret api_key", final["yaml"])
        self.assertEqual(final["yaml"], self.prepare(final["yaml"])["yaml"])

    def test_errors_do_not_echo_input_secrets(self):
        result = self.prepare("secret: PRIVATE_SENTINEL\nnot yaml [")
        self.assertNotIn("PRIVATE_SENTINEL", str(result))

    def test_deep_yaml_and_blank_password_are_rejected(self):
        self.assertEqual(self.prepare("x: " + "[" * 1000 + "0" + "]" * 1000)["code"], "unsupported_yaml")
        blank = self.source.replace("substitutions:\n", 'substitutions:\n  ota_password: ""\n')
        self.assertEqual(upgrade.prepare_upgrade(blank, transport="legacy")["code"], "ota_password_missing")

    def test_text_preserves_custom_chip_pins_and_crlf(self):
        source = self.source.replace('minimum_chip_revision: "3.1"', 'minimum_chip_revision: "1.0"')
        source = source.replace('"GPIO17"', '"GPIO25"').replace('fast_update_interval: "13s"', 'fast_update_interval: "17s"')
        source = source.replace("\n", "\r\n")
        result = self.prepare(source)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["backup"], source)
        self.assertEqual(result["yaml"].split("packages:")[0].split("dashboard_import:")[0], source.split("packages:")[0].split("dashboard_import:")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
