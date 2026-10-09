"""Fail-closed checks for proposed, disabled Recorder entity retention."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import unittest

from ems_retention_policy import proposed_actions


def policy() -> dict:
    return {
        "schema_version": 1,
        "enabled": False,
        "entities": [
            {"entity_id": "sensor.hoymiles_actual_load_energy_today",
             "keep_days": 35, "role": "load_qualified_raw",
             "consumer": "LOAD v5 31-day bootstrap",
             "external_use_reviewed": True},
            {"entity_id": "sensor.hoymiles_hit_grid_voltage_l1",
             "keep_days": 7, "role": "rcm_qualified_raw",
             "consumer": "RCEm five-day voltage history",
             "external_use_reviewed": True},
        ],
    }


class RetentionPolicyTests(unittest.TestCase):
    def test_home_assistant_service_schema_accepts_explicit_keep_days(self):
        from homeassistant.components.recorder.services import (
            SERVICE_PURGE_ENTITIES_SCHEMA,
        )

        for action in proposed_actions(
            policy(), at_utc=datetime(2026, 9, 27, tzinfo=timezone.utc)
        ):
            validated = SERVICE_PURGE_ENTITIES_SCHEMA(action["data"])
            self.assertEqual(validated["keep_days"], action["data"]["keep_days"])
            self.assertEqual(validated["domains"], [])
            self.assertEqual(validated["entity_globs"], [])

    def test_explicit_nonexecuting_calls_and_cutoffs(self):
        actions = proposed_actions(
            policy(), at_utc=datetime(2026, 9, 27, 12, tzinfo=timezone.utc)
        )
        self.assertEqual([item["data"]["keep_days"] for item in actions], [7, 35])
        self.assertEqual(actions[0]["rolling_cutoff_utc"], "2026-09-20T12:00:00Z")
        self.assertEqual(actions[1]["rolling_cutoff_utc"], "2026-08-23T12:00:00Z")
        self.assertTrue(all(item["execution_enabled"] is False for item in actions))
        self.assertTrue(all(set(item["data"]) == {"entity_id", "keep_days"}
                            for item in actions))

    def test_default_zero_day_or_broad_scope_cannot_be_generated(self):
        cases = []
        for entity in ("sensor.*", "sensor", "sensor.power-1", "",
                       "sensor.solcast_forecast_today"):
            candidate = deepcopy(policy())
            candidate["entities"][0]["entity_id"] = entity
            cases.append(candidate)
        for keep_days in (None, 0, 2, 7, 120, True):
            candidate = deepcopy(policy())
            candidate["entities"][0]["keep_days"] = keep_days
            cases.append(candidate)
        candidate = deepcopy(policy())
        candidate["enabled"] = True
        cases.append(candidate)
        candidate = deepcopy(policy())
        candidate["entities"][0]["external_use_reviewed"] = False
        cases.append(candidate)
        candidate = deepcopy(policy())
        candidate["entities"].append(deepcopy(candidate["entities"][0]))
        cases.append(candidate)
        for item in cases:
            with self.subTest(item=item), self.assertRaises(ValueError):
                proposed_actions(item, at_utc=datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
