"""A stale, higher history estimate cannot disable the current configured LOAD fallback."""
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
import test_load_model_freshness_alignment as f


class StaleHistoryFallbackTest(unittest.TestCase):
    def test_stale_higher_history_uses_explicit_fallback_without_renewing_history(self):
        now = datetime(2026, 10, 4, 17, tzinfo=f.WARSAW)
        stamp = now-timedelta(hours=41)
        # The qualified-day pool has aged outside the 28-day forecast window.
        sensor = f._probe(stamp, now - timedelta(days=28))
        method = f._load_values_method(configured_fallback=7.)
        values = method(sensor, now)
        self.assertEqual(values['average_load'], 7.)
        self.assertEqual(values['load_model_source'], 'configured_daily_fallback')
        self.assertFalse(values['history_complete'])
        self.assertEqual(sensor._load_profile_generated_at, stamp)
        self.assertEqual(values['profile_history'].daily_history_days, 0)
        bounded = f.shared.M._bounded_load_model_snapshot(dict(
            average_daily_home_load_kwh=values['average_load'],
            fallback_currently_used=(values['average_load']==values['fallback_load']),
            generated_at=stamp, ready=True), now=now)
        self.assertFalse(bounded.ready, 'Old history must remain unqualified')
        sample = SimpleNamespace(value=7., fresh=True)
        self.assertTrue(f.shared.M._explicit_fallback_model_ready(bounded, sample))
        self.assertTrue(f._shared_fallback_profile_ready()(SimpleNamespace(
            fallback_currently_used=True, ready=True,
            fallback_daily_home_load_kwh=sample), values['average_load']))
        self.assertFalse(f.shared.M._explicit_fallback_model_ready(
            bounded, SimpleNamespace(value=7., fresh=False)))
        self.assertAlmostEqual(method(f._probe(now, now), now)['average_load'], 10.)


if __name__ == '__main__':
    unittest.main()
