"""Validate generated upgrade YAML in real ESPHome, without network or OTA."""
from pathlib import Path
from contextlib import redirect_stdout, redirect_stderr
import io
import subprocess
import tempfile
import unittest

from esphome.config import read_config
from esphome.core import CORE

from test_esphome_upgrade import ROOT, upgrade
from test_esphome_entry_points import SECRETS


class GeneratedConfigTest(unittest.TestCase):
    def validate(self, source, transport):
        result = upgrade.prepare_upgrade(source, transport=transport)
        self.assertEqual(result["status"], "ready", result)
        output = result["yaml"]
        root = upgrade._parse(output)
        key, value = next((k, v) for k, v in root.value if k.value == "packages")
        package_paths = list(upgrade.package_manifest()["files"])
        # Validate the pinned bytes, not whatever unrelated working tree is near us.
        for name in package_paths:
            pinned = subprocess.check_output(["git", "show", f"{upgrade.FIRMWARE_SHA}:{name}"], cwd=ROOT).decode()
            self.assertEqual(pinned.replace("\r\n", "\n"), (ROOT / name).read_text(encoding="utf-8"))
        wrapper = "packages:\n" + "".join(
            f'  p{i}: !include "{(ROOT / name).as_posix()}"\n' for i, name in enumerate(package_paths)
        )
        output = output[:key.start_mark.index] + wrapper + output[value.end_mark.index:]
        with tempfile.TemporaryDirectory(prefix="hoymiles-upgrade-config-") as folder:
            folder = Path(folder)
            (folder / "secrets.yaml").write_text(SECRETS + "ota_password: original-ci-password\n", encoding="utf-8")
            config_path = folder / "hoymiles.yaml"
            config_path.write_text(output, encoding="utf-8")
            CORE.reset()
            CORE.config_path = config_path
            config = read_config({})
            self.assertIsNotNone(config, "ESPHome rejected generated YAML")
            native = [item for item in config["ota"] if item["platform"] == "esphome"]
            self.assertEqual(len(native), 1)
            self.assertEqual(config["api"]["encryption"]["key"], "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY=")
            if transport == "legacy":
                self.assertEqual(native[0]["password"], "original-ci-password")
                self.assertNotIn("encryption", native[0])
            else:
                self.assertNotIn("password", native[0])
                self.assertIn("encryption", native[0])
                self.assertEqual(native[0]["encryption"]["key"], config["api"]["encryption"]["key"])
            uart = config["uart"]
            self.assertEqual(len(uart), 1)
            self.assertEqual(uart[0]["tx_pin"]["number"], 17)
            self.assertEqual(uart[0]["rx_pin"]["number"], 16)
            self.assertEqual(uart[0]["baud_rate"], 115200)
            self.assertEqual(len(config["modbus"]), 1)
            self.assertEqual(len(config["modbus_controller"]), 5)
            return config, result["yaml"]

    def test_all_public_profiles(self):
        for name in ("hoymiles-inverter.yaml", "hoymiles-inverter-s3.yaml", "hoymiles-inverter-flow-control.yaml"):
            with self.subTest(profile=name):
                config, _ = self.validate((ROOT / name).read_text(encoding="utf-8"), "encrypted")
                if "-s3" in name:
                    self.assertEqual(config["esp32"]["flash_size"], "16MB")
                    self.assertEqual(config["psram"]["mode"], "octal")
                if "flow-control" in name:
                    self.assertEqual(config["uart"][0]["flow_control_pin"]["number"], 4)

    def test_157_bridge_then_encryption_and_old_chip(self):
        source = subprocess.check_output(["git", "show", "6617bc4de6592439ea2c64889b0a25bbe5bfa45e:hoymiles-inverter.yaml"], cwd=ROOT).decode()
        config, bridge = self.validate(source, "legacy")
        self.assertEqual(config["esp32"]["board"], "esp32dev")
        self.validate(bridge, "encrypted")
        # Earlier ESP32 modules must not silently acquire a rev3.1 requirement.
        source = (ROOT / "hoymiles-inverter.yaml").read_text(encoding="utf-8")
        source = source.replace('minimum_chip_revision: "3.1"', 'minimum_chip_revision: "1.0"').replace("sram1_as_iram: true", "sram1_as_iram: false")
        config, _ = self.validate(source, "encrypted")
        self.assertEqual(str(config["esp32"]["framework"]["advanced"]["minimum_chip_revision"]), "1.0")
        self.assertFalse(config["esp32"]["framework"]["advanced"]["sram1_as_iram"])

    def test_old_register_packages_reproduce_validation_failure(self):
        source = subprocess.check_output(["git", "show", "6617bc4de6592439ea2c64889b0a25bbe5bfa45e:hoymiles-inverter.yaml"], cwd=ROOT).decode()
        with tempfile.TemporaryDirectory(prefix="hoymiles-upgrade-red-") as folder:
            folder = Path(folder)
            (folder / "secrets.yaml").write_text(SECRETS + "ota_password: original-ci-password\n", encoding="utf-8")
            paths = list(upgrade.package_manifest()["files"])
            for name in paths:
                path = folder / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(subprocess.check_output(["git", "show", f"e995f9b730eaecdfd07fd84ccff34108e2799f14:{name}"], cwd=ROOT))
            root = upgrade._parse(source)
            key, value = next((k, v) for k, v in root.value if k.value == "packages")
            wrapper = "packages:\n" + "".join(f"  p{i}: !include {name}\n" for i, name in enumerate(paths))
            original = source[:key.start_mark.index] + wrapper + source[value.end_mark.index:]
            config_path = folder / "hoymiles.yaml"
            config_path.write_text(original, encoding="utf-8")
            CORE.reset()
            CORE.config_path = config_path
            captured = io.StringIO()
            with redirect_stdout(captured), redirect_stderr(captured):
                broken = read_config({})
            self.assertIsNone(broken)
            self.assertIn("register_count", captured.getvalue())
            contents = {name: (folder / name).read_text(encoding="utf-8") for name in paths}
            result = upgrade.prepare_upgrade(original, transport="legacy", local_packages=contents)
            self.assertEqual(result["status"], "ready")
            self.validate(result["yaml"], "legacy")
            print("RED reproduced: old register_count rejected; GREEN: the same local configuration migrates and validates")


if __name__ == "__main__":
    unittest.main(verbosity=2)
