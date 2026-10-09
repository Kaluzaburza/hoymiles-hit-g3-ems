#!/usr/bin/env python3
"""Offline contract for integer UI steps and lossless legacy percent transport.

Run with the ESPHome validation Python environment (PyYAML is required).
This validates parsed YAML, targeted transport contracts, and an explicitly
modelled float32/word conversion. It does not execute firmware C++, prove that
the inverter accepts fractional commands, or make native number.set integer-only.
"""

from __future__ import annotations

import math
from pathlib import Path
import struct
import unittest

import yaml


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "packages" / "settings.yaml"

# id: address, type, minimum, maximum, register scale. Scale is deliberately
# independent of UI step: an integer 50% still occupies raw word 500 at scale 10.
PERCENT_NUMBERS = {
    "gcf_export_soft_limit_ratio_259": (259, "S_WORD", -10, 200, 10),
    "battery_max_charge_power_306": (306, "U_WORD", 10, 100, 10),
    "battery_max_discharge_power_307": (307, "U_WORD", 10, 100, 10),
    "max_soc_308": (308, "U_WORD", 10, 100, 1),
    "min_soc_309": (309, "U_WORD", 10, 90, 1),
    "rsvd_replenish_power_311": (311, "U_WORD", 0, 100, 10),
    "self_used_soc_4301": (4301, "U_WORD", 10, 100, 1),
    "force_charge_soc_4303": (4303, "U_WORD", 10, 100, 1),
    "maximum_charge_power_4304": (4304, "U_WORD", 0, 100, 10),
    "force_discharge_soc_4305": (4305, "U_WORD", 0, 100, 1),
    "maximum_discharge_power_4306": (4306, "U_WORD", 0, 100, 10),
}

class FirmwareLoader(yaml.SafeLoader):
    """Preserve !lambda source as text; never execute YAML contents."""


FirmwareLoader.add_constructor(
    "!lambda", lambda loader, node: loader.construct_scalar(node)
)


def float32(value: float) -> float:
    return struct.unpack(">f", struct.pack(">f", value))[0]


def model_word(value: float, scale: int = 10) -> int:
    """Model finite in-range C++ float product and lround/llroundf to word.

    ESPHome 2026.8.2 modbus_helpers.h uses llroundf for S_WORD/U_WORD,
    then number_to_payload masks to 16 bits. The shared EMS writer uses
    std::lround(value * scale). Both round halves away from zero.
    """
    scaled = float32(float32(value) * float32(scale))
    if not math.isfinite(scaled):
        raise ValueError("model requires a finite in-range input")
    magnitude = math.floor(abs(scaled) + 0.5)
    raw = -magnitude if scaled < 0 else magnitude
    return raw & 0xFFFF


class IntegerPercentFirmwareContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.document = yaml.load(
            SETTINGS.read_text(encoding="utf-8"), Loader=FirmwareLoader
        )
        cls.numbers = {item["id"]: item for item in cls.document["number"]}

    def test_all_eleven_manual_percent_fields_have_integer_steps(self) -> None:
        actual = {
            key for key, value in self.numbers.items()
            if value.get("unit_of_measurement") == "%"
        }
        self.assertEqual(actual, set(PERCENT_NUMBERS))
        for key, (address, value_type, minimum, maximum, scale) in PERCENT_NUMBERS.items():
            with self.subTest(register=address):
                number = self.numbers[key]
                self.assertEqual(number["address"], address)
                self.assertEqual(number["value_type"], value_type)
                self.assertEqual(number["min_value"], minimum)
                self.assertEqual(number["max_value"], maximum)
                self.assertEqual(number["step"], 1)
                if scale == 10:
                    self.assertEqual(number["lambda"], "return x * 0.1f;")

    def test_public_api_keeps_float_values_and_physical_generation(self) -> None:
        actions = {a["action"]: a for a in self.document["api"]["actions"]}
        complete = actions["ems_supervisor_write_complete_block"]
        self.assertEqual(complete["supports_response"], "optional")
        for argument in (
            "self_use_soc", "backup_soc", "force_charge_soc",
            "maximum_charge_power", "force_discharge_soc",
            "maximum_discharge_power", "snapshot_generation",
        ):
            self.assertEqual(complete["variables"][argument], "float")
        for action, target in (
            ("ems_supervisor_write_gcf_export_limit", "gcf_export_soft_limit_ratio_259"),
            ("ems_supervisor_write_battery_charge_limit", "battery_max_charge_power_306"),
        ):
            with self.subTest(action=action):
                self.assertEqual(actions[action]["variables"]["target_percent"], "float")
                assignment = actions[action]["then"][0]["if"]["then"][0]["number.set"]
                self.assertEqual(assignment["id"], target)
                self.assertEqual(assignment["value"], "return target_percent;")
                # These numbers are shared with compatible restore transport.
                self.assertNotIn("x != std::round(x)", self.numbers[target]["write_lambda"])

    def test_whole_percent_commands_keep_original_register_scale(self) -> None:
        for _, (address, _, minimum, maximum, scale) in PERCENT_NUMBERS.items():
            with self.subTest(register=address):
                for percent in range(minimum, maximum + 1):
                    self.assertEqual(model_word(percent, scale), (percent * scale) & 0xFFFF)
        self.assertEqual(model_word(50), 500)
        self.assertEqual(model_word(-10), 0xFF9C)
        self.assertEqual(model_word(200), 2000)

    def test_legacy_tenths_survive_physical_float_roundtrip(self) -> None:
        # Includes all valid raw power words and the wider signed GCF range.
        # This is representation preservation, not inverter acceptance evidence.
        for signed_raw in range(-100, 2001):
            physical_percent = float32(float32(signed_raw) * float32(0.1))
            self.assertEqual(model_word(physical_percent), signed_raw & 0xFFFF)
        legacy = float32(49.9)
        self.assertEqual(model_word(legacy), 499)
        self.assertEqual(model_word(legacy).to_bytes(2, "big"), b"\x01\xf3")
        self.assertEqual(model_word(50).to_bytes(2, "big"), b"\x01\xf4")

    def test_legacy_charge_rollback_payload_stays_lossless(self) -> None:
        rollback = self.numbers["ems_complete_block_charge_rollback_command"]
        self.assertEqual(rollback["step"], 1)  # Packed command, not percent step.
        self.assertFalse(rollback["optimistic"])
        self.assertFalse(rollback["restore_value"])
        source = rollback["set_action"][0]["lambda"]
        self.assertIn("payload / 1001", source)
        self.assertIn("payload % 1001", source)
        self.assertIn("static_cast<float>(maximum_charge_power_raw) * 0.1f", source)
        for soc in range(10, 101):
            for raw_power in range(1001):
                payload = soc * 1001 + raw_power
                self.assertEqual(float32(payload), payload)
                self.assertEqual(divmod(payload, 1001), (soc, raw_power))
        saved_soc, saved_raw = divmod(90 * 1001 + 499, 1001)
        self.assertEqual(saved_soc, 90)
        self.assertEqual(model_word(float32(saved_raw) * float32(0.1)), 499)

    def test_full_block_manual_change_keeps_other_physical_fields(self) -> None:
        field_ids = (
            "self_used_soc_readback_4301", "backup_soc_raw_4302",
            "force_charge_soc_readback_4303", "maximum_charge_power_readback_4304",
            "force_discharge_soc_readback_4305", "maximum_discharge_power_readback_4306",
        )
        changes = {
            "self_used_soc_4301": 0,
            "force_charge_soc_4303": 2,
            "maximum_charge_power_4304": 3,
            "force_discharge_soc_4305": 4,
            "maximum_discharge_power_4306": 5,
        }
        for key, changed_index in changes.items():
            with self.subTest(number=key):
                source = self.numbers[key]["write_lambda"]
                self.assertIn("id(ems_write_complete_block_4300_4306)->execute(", source)
                for index, source_id in enumerate(field_ids):
                    if index != changed_index:
                        self.assertIn(f"id({source_id}).state", source)
                self.assertIn(
                    "id(ems_control_readback_generation).state, false, nullptr",
                    source,
                )
                self.assertTrue(source.rstrip().endswith("return {};"))
        writer = self.document["script"][0]["then"][0]["lambda"]
        self.assertIn("std::vector<uint16_t> values", writer)
        self.assertIn("std::lround(value * scale)", writer)
        for field in ("maximum_charge_power", "maximum_discharge_power"):
            self.assertIn(f"encode({field}, 10.0f)", writer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
