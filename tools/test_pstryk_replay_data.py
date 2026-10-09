"""Independent evidence-quality regressions for the replay CSV inspector."""
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from inspect_pstryk_replay_data import inspect

UTC = timezone.utc
START = datetime(2026, 9, 15, tzinfo=UTC)


class ReplayDataTest(unittest.TestCase):
    def read(self, rows):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "history.csv"
            path.write_text("entity_id,state,last_changed\n" + "\n".join(rows), encoding="utf-8")
            return inspect(path, start=START, end=START + timedelta(days=1))

    def test_explicit_outage_is_not_zero(self):
        result = self.read(["sensor.pv,0,2026-09-15T00:00:00Z",
                            "sensor.pv,unavailable,2026-09-15T00:05:00Z",
                            "sensor.pv,0.5,2026-09-15T00:10:00Z"])
        value = result["entities"]["sensor.pv"]
        self.assertEqual(value["numeric_rows"], 2)
        self.assertEqual(value["nonnumeric_states"], {"unavailable": 1})
        self.assertEqual(value["minimum"], 0)

    def test_numeric_spacing_does_not_invent_outage(self):
        result = self.read(["sensor.load,10,2026-09-15T00:00:00Z",
                            "sensor.load,11,2026-09-15T02:00:00Z"])
        value = result["entities"]["sensor.load"]
        self.assertEqual(value["numeric_spacings_over_600s"], 1)
        self.assertEqual(value["nonnumeric_states"], {})
        self.assertEqual(result["qualification"], "INSPECTED_NOT_REPLAY_ACCEPTED")

    def test_counter_reset_reported_without_guessing_energy(self):
        value = self.read(["sensor.load,20,2026-09-15T21:59:59Z",
                           "sensor.load,0,2026-09-15T22:00:00Z"])["entities"]["sensor.load"]
        self.assertEqual(value["decreases"], 1)
        self.assertNotIn("energy_kwh", value)

    def test_bad_dates_rejected_and_outside_window_counted(self):
        with self.assertRaises(ValueError):
            self.read(["sensor.load,1,2026-09-15T00:00:00"])
        value = self.read(["sensor.load,1,2026-09-14T00:00:00Z"])["entities"]["sensor.load"]
        self.assertEqual(value["outside_requested_window"], 1)
        self.assertIsNone(value["minimum"])

    def test_nonfinite_is_invalid_not_healthy(self):
        value = self.read(["sensor.load,NaN,2026-09-15T00:00:00Z"])["entities"]["sensor.load"]
        self.assertEqual(value["numeric_rows"], 0)
        self.assertEqual(value["nonnumeric_states"], {"invalid": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
