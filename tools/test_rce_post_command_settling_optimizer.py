"""Offline diagnostics tests; the settling flag never grants execution authority."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom_components/hoymiles_hit_modbus/rce_optimizer.py"
sys.path.insert(0, str(MODULE_PATH.parent))
SPEC = importlib.util.spec_from_file_location("rce_settling_optimizer", MODULE_PATH)
RCE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = RCE
SPEC.loader.exec_module(RCE)
NOW = datetime(2026, 9, 8, 18, 0, tzinfo=timezone.utc)
FLAG = "current_slot_load_exhausts_requested_discharge_budget"
FINGERPRINT = "post_command_settling_market_fingerprint"


def settings(**changes):
    complete = RCE.OptimizerInput(
        now=NOW,
        price_slots=[RCE.PriceSlot(NOW, 2.0)],
        pv_by_slot_kwh={},
        battery_capacity_kwh=100.0,
        battery_soc_percent=95.0,
        outage_reserve_soc_percent=10.0,
        safety_margin_soc_percent=0.0,
        manual_minimum_soc_percent=10.0,
        dynamic_reserve_enabled=False,
        average_daily_load_kwh=0.0,
        average_night_load_kwh=0.0,
        night_start_minute=1200,
        night_end_minute=480,
        inverter_power_kw=16.0,
        inverter_count=1,
        discharge_power_percent=40.0,
        export_efficiency_percent=95.0,
        house_discharge_efficiency_percent=95.0,
        bms_max_discharge_current_a=800.0,
        battery_voltage_v=50.0,
        bms_power_safety_percent=95.0,
        bms_discharge_data_fresh=True,
        bms_discharge_data_available=True,
        bms_discharge_data_age_seconds=0.0,
        current_battery_soc_fresh=True,
        current_load_power_kw=7.86,
        current_pv_power_kw=0.0,
        export_power_cap_kw=16.0,
        effective_export_power_kw=16.0,
    )
    return replace(complete, **changes)


class PostCommandSettlingOptimizerTest(unittest.TestCase):
    def test_observed_load_step_changes_only_the_diagnostic_interpretation(self):
        low = RCE.optimize_rce(settings(current_load_power_kw=0.748))
        high = RCE.optimize_rce(settings(current_load_power_kw=7.86))
        self.assertFalse(getattr(low, FLAG))
        self.assertTrue(getattr(high, FLAG))
        self.assertEqual(low.requested_export_power_kw, 6.4)
        self.assertEqual(high.requested_export_power_kw, 6.4)
        # Existing planner behavior recorded before adding diagnostics.
        self.assertAlmostEqual(low.current_slot_shared_discharge_limit_kwh, 2.826)
        self.assertEqual(low.current_slot_execution_power_percent, 40.0)
        self.assertTrue(low.current_slot_start_eligible)
        self.assertEqual(high.current_slot_shared_discharge_limit_kwh, 0.0)
        self.assertEqual(high.current_slot_execution_power_percent, 0.0)
        self.assertFalse(high.current_slot_start_eligible)
        self.assertTrue(high.ready)
        self.assertEqual(high.status_code, "home_protected")
        self.assertEqual(getattr(low, FINGERPRINT), getattr(high, FINGERPRINT))

    def test_requested_budget_uses_whole_percent_quantization_and_net_load(self):
        for changes, expected in (
            ({"current_load_power_kw": 6.4}, True),
            ({"current_load_power_kw": 6.399}, False),
            ({"discharge_power_percent": 40.9}, True),
            ({"discharge_power_percent": 50.0}, False),
            ({"discharge_power_percent": 0.9}, False),
            ({"discharge_power_percent": 0.0}, False),
            ({"current_pv_power_kw": 2.0}, False),
        ):
            with self.subTest(changes=changes):
                self.assertEqual(getattr(RCE.optimize_rce(settings(**changes)), FLAG), expected)

    def test_other_zero_caps_missing_live_or_blocked_current_slot_never_flag(self):
        variants = (
            {"export_power_cap_kw": 0.0},
            {"effective_export_power_kw": 0.0},
            {"bms_max_discharge_current_a": 0.0},
            {"bms_max_discharge_current_a": 100.0},
            {"battery_voltage_v": None},
            {"bms_discharge_data_fresh": False},
            {"bms_discharge_data_available": False},
            {"bms_discharge_data_age_seconds": None},
            {"bms_discharge_data_age_seconds": -1.0},
            {"current_load_power_kw": None},
            {"current_pv_power_kw": None},
            {"current_load_power_kw": float("nan")},
            {"current_battery_soc_fresh": False},
            {"inverter_ac_power_kw": 7.86},
            {"price_slots": [RCE.PriceSlot(NOW, 2.0, True)]},
            {"price_slots": [RCE.PriceSlot(NOW + timedelta(minutes=30), 2.0)]},
            {"price_slots": []},
        )
        for changes in variants:
            with self.subTest(changes=changes):
                self.assertFalse(getattr(RCE.optimize_rce(settings(**changes)), FLAG))
        # Optional absent caps mean no additional cap, not invented zero power.
        uncapped = RCE.optimize_rce(settings(export_power_cap_kw=None, effective_export_power_kw=None))
        self.assertTrue(getattr(uncapped, FLAG))
        self.assertFalse(uncapped.current_slot_start_eligible)

    def test_invalid_optional_caps_are_rejected_by_the_diagnostic(self):
        complete = settings()
        result = RCE.optimize_rce(complete)
        self.assertTrue(getattr(result, FLAG))
        for name in ("export_power_cap_kw", "effective_export_power_kw"):
            for invalid in (-1.0, float("nan"), float("inf"), True, "missing"):
                with self.subTest(cap=name, value=invalid):
                    self.assertFalse(RCE._current_slot_load_exhausts_requested_discharge_budget(
                        replace(complete, **{name: invalid}), result
                    ))

    def test_shortage_missing_data_and_optimizer_error_never_flag(self):
        # A genuine modeled shortage, independently of a raw LOAD pulse.
        shortage = RCE.optimize_rce(settings(battery_soc_percent=10.0,
            average_daily_load_kwh=24., average_night_load_kwh=8.))
        self.assertEqual(shortage.status_code, "home_energy_shortage")
        self.assertFalse(getattr(shortage, FLAG))
        missing = RCE.optimize_rce(settings(battery_capacity_kwh=0.0))
        self.assertEqual(missing.status_code, "missing_data")
        self.assertFalse(getattr(missing, FLAG))
        with patch.object(RCE, "_solve_joint_horizon_exports", return_value={NOW: 1000.0}):
            invalid = RCE.optimize_rce(settings())
        self.assertEqual(invalid.status_code, "optimizer_error")
        self.assertFalse(getattr(invalid, FLAG))

    def test_market_hash_ignores_commands_telemetry_and_representation_order(self):
        complete = settings(price_slots=[RCE.PriceSlot(NOW, 2.0), RCE.PriceSlot(NOW + timedelta(minutes=30), 1.5)])
        fingerprint = RCE.post_command_settling_market_fingerprint(complete)
        self.assertIsNotNone(fingerprint)
        self.assertEqual(len(fingerprint), 64)
        for changes in (
            {"discharge_power_percent": 50.0},
            {"current_load_power_kw": 0.748, "current_pv_power_kw": 3.0},
            {"battery_soc_percent": 75.0, "battery_voltage_v": 48.0},
            {"export_power_cap_kw": 0.0, "effective_export_power_kw": 8.0},
            {"now": NOW + timedelta(minutes=5)},
            {"price_slots": list(reversed(complete.price_slots))},
            {"price_slots": [*complete.price_slots, RCE.PriceSlot(NOW, 3.0)]},
            {"price_slots": [*complete.price_slots, RCE.PriceSlot(NOW + timedelta(days=4), 100.0)]},
            {"price_slots": [RCE.PriceSlot(slot.start.astimezone(timezone(timedelta(hours=2))), slot.price_pln_kwh) for slot in complete.price_slots]},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(RCE.post_command_settling_market_fingerprint(replace(complete, **changes)), fingerprint)

    def test_price_block_and_each_economic_scalar_change_hash(self):
        complete = settings()
        fingerprint = RCE.post_command_settling_market_fingerprint(complete)
        for changes in (
            {"price_slots": [RCE.PriceSlot(NOW, 2.01)]},
            {"price_slots": [RCE.PriceSlot(NOW, 2.0, True)]},
            {"price_slots": [RCE.PriceSlot(NOW + timedelta(minutes=30), 2.0)]},
            {"battery_wear_cost_pln_kwh": 0.09},
            {"export_efficiency_percent": 94.0},
            {"house_discharge_efficiency_percent": 94.0},
            {"avoided_import_price_pln_kwh": 1.01},
        ):
            with self.subTest(changes=changes):
                changed = RCE.post_command_settling_market_fingerprint(replace(complete, **changes))
                self.assertIsNotNone(changed)
                self.assertNotEqual(changed, fingerprint)

    def test_market_hash_fails_closed_for_invalid_or_missing_basis(self):
        complete = settings()
        for name in ("battery_wear_cost_pln_kwh", "export_efficiency_percent",
                     "house_discharge_efficiency_percent", "avoided_import_price_pln_kwh"):
            for invalid in (None, float("nan"), float("inf"), -1.0, True, "invalid"):
                with self.subTest(scalar=name, invalid=invalid):
                    self.assertIsNone(RCE.post_command_settling_market_fingerprint(replace(complete, **{name: invalid})))
        for name in ("export_efficiency_percent", "house_discharge_efficiency_percent"):
            for invalid in (0.0, 100.01):
                self.assertIsNone(RCE.post_command_settling_market_fingerprint(replace(complete, **{name: invalid})))
        for changes in (
            {"price_slots": []}, {"price_slots": None},
            {"price_slots": [RCE.PriceSlot(NOW, float("nan"))]},
            {"price_slots": [RCE.PriceSlot(NOW.replace(tzinfo=None), 2.0)]},
            {"price_slots": [RCE.PriceSlot(NOW + timedelta(days=2), 2.0)]},
            {"now": NOW.replace(tzinfo=None)}, {"now": None},
        ):
            with self.subTest(changes=changes):
                self.assertIsNone(RCE.post_command_settling_market_fingerprint(replace(complete, **changes)))

    def test_fingerprint_helper_is_cheap_and_does_not_mutate_input(self):
        complete = settings(price_slots=[RCE.PriceSlot(NOW, 3.0), RCE.PriceSlot(NOW, 2.0)])
        original = list(complete.price_slots)
        with patch.object(RCE, "_simulate", side_effect=AssertionError("no simulation for fingerprint")), \
             patch.object(RCE, "_solve_joint_horizon_exports", side_effect=AssertionError("no solver for fingerprint")):
            self.assertIsNotNone(RCE.post_command_settling_market_fingerprint(complete))
        self.assertEqual(complete.price_slots, original)

    def test_disabling_diagnostics_changes_no_solver_or_authority_result(self):
        for changes in (
            {"current_load_power_kw": 0.748}, {"current_load_power_kw": 7.86},
            {"discharge_power_percent": 50.0}, {"current_pv_power_kw": 2.0},
        ):
            with self.subTest(changes=changes), patch.object(RCE, "perf_counter", return_value=0.0):
                complete = settings(**changes)
                normal = asdict(RCE.optimize_rce(complete))
                with patch.object(RCE, "_current_slot_load_exhausts_requested_discharge_budget", return_value=False), \
                     patch.object(RCE, "post_command_settling_market_fingerprint", return_value=None):
                    without = asdict(RCE.optimize_rce(complete))
                for diagnostic in (FLAG, FINGERPRINT):
                    normal.pop(diagnostic)
                    without.pop(diagnostic)
                self.assertEqual(normal, without)  # Includes timeline, planned exports and eligibility.


if __name__ == "__main__":
    unittest.main(verbosity=2)
