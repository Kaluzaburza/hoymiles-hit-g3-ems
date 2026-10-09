"""Full solves across 14:00 retain only a physically committed PV window."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import sys, unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
from test_pv_charge_delay_adapter import settings, rce_delay, optimize_rce, PriceSlot
from test_pv_charge_delay_horizon import case
from pstryk_joint import optimize, revalidate_plan
from pv_charge_delay import active_delay_plan, PvDelayCommitment

Z = ZoneInfo('Europe/Warsaw')
START = datetime(2026, 10, 1, 13, 30, tzinfo=Z)


def fixture():
    data = replace(case(START, days=1), initial_kwh=7., pv_origin_kwh=7.)
    return replace(data, slots=tuple(replace(s, net=.9 if s.start.astimezone(Z).hour < 15 else .1)
        for s in data.slots))


class ActiveWindowTests(unittest.TestCase):
    def test_pstryk_two_full_replans_keep_original_deadline_and_recovery(self):
        data = fixture(); first = plan = optimize(data)
        original = first.delay_plan
        self.assertIsNotNone(original)
        proof = PvDelayCommitment('same-transaction', START, original.end)
        for _ in range(2):
            data = replace(data, slots=data.slots[1:], initial_kwh=plan.slots[0].end_kwh,
                pv_origin_kwh=plan.slots[0].pv_origin_kwh, active_delay=None)
            self.assertIsNone(optimize(data).delay_plan, 'Never authorize a new start from 14:00')
            active = active_delay_plan(plan.delay_plan, proof, data.slots[0].start)
            data = replace(data, active_delay=active)
            plan = optimize(data)
            self.assertIsNotNone(plan.delay_plan)
            self.assertEqual(plan.delay_plan.start, original.start)
            self.assertEqual(plan.delay_plan.end, original.end)
            self.assertEqual(plan.delay_plan.recovered_at, original.recovered_at)
            self.assertEqual(plan.delay_plan.recovery_target_kwh, original.recovery_target_kwh)
            self.assertEqual(plan.slots[0].action, 'pv_charge_hold')
            self.assertAlmostEqual(plan.slots[0].end_kwh, plan.slots[0].start_kwh)
            self.assertIsNotNone(revalidate_plan(data, plan, captured=data))
        self.assertIsNone(active_delay_plan(plan.delay_plan, proof, original.end))
        self.assertIsNone(active_delay_plan(plan.delay_plan, None, data.slots[0].start))
        self.assertIsNone(active_delay_plan(plan.delay_plan, replace(proof, hard_deadline=original.end+timedelta(minutes=30)), data.slots[0].start))

    def test_rce_adapter_carries_the_same_window(self):
        now = START.astimezone(timezone.utc)
        prices = [PriceSlot(now+timedelta(minutes=30*i), .9 if i < 3 else .1) for i in range(25)]
        pv = {p.start: 3. if p.start.astimezone(Z).hour < 17 else 0. for p in prices}
        config = replace(settings(), now=now, price_slots=prices, pv_by_slot_kwh=pv,
            conservative_pv_by_slot_kwh=pv, battery_wear_cost_pln_kwh=100.,
            average_daily_load_kwh=5., average_night_load_kwh=2., battery_soc_percent=70.)
        first = rce_delay(config, optimize_rce(config), enabled=True)
        self.assertIsNotNone(first)
        for minutes in (30, 60):
            config = replace(config, now=now+timedelta(minutes=minutes))
            # The test owns an active PV window, with no new native SELL
            # barrier. Same-slot PV export must not invalidate that premise.
            result = optimize_rce(config)
            self.assertFalse(result.planned_exports)
            self.assertIsNone(rce_delay(config, result, enabled=True))
            plan = rce_delay(config, result, enabled=True, active_plan=first)
            self.assertIsNotNone(plan)
            self.assertEqual((plan.start, plan.end, plan.recovered_at, plan.recovery_target_kwh),
                (first.start, first.end, first.recovered_at, first.recovery_target_kwh))

        for minutes, load, pv_power in ((4, 6., 5.), (7, .4, 0.), (10, 9., .1)):
            live = replace(config, now=now+timedelta(minutes=minutes),
                current_load_power_kw=load, current_pv_power_kw=pv_power)
            plan = rce_delay(live, optimize_rce(live), enabled=True, active_plan=first)
            self.assertIsNotNone(plan, 'Instantaneous deficit revoked a bound classic PV window')
            self.assertEqual((plan.start, plan.end, plan.recovered_at),
                             (first.start, first.end, first.recovered_at))

    def test_failed_refill_import_or_consent_cannot_retain_window(self):
        data = fixture(); plan = optimize(data)
        fresh = replace(data, slots=data.slots[1:], initial_kwh=plan.slots[0].end_kwh,
            pv_origin_kwh=plan.slots[0].pv_origin_kwh, active_delay=plan.delay_plan)
        cases = [replace(fresh, export_kw=0.), replace(fresh, allow_sell=False),
            replace(fresh, delay_qualified_dates=()),
            replace(fresh, slots=tuple(replace(s, pv_kwh=0.) for s in fresh.slots)),
            replace(fresh, slots=tuple(replace(s, net=-.1) if s.start<plan.delay_plan.end else s for s in fresh.slots)),
            replace(fresh, active_delay=replace(plan.delay_plan, recovery_target_kwh=11.)),
            replace(fresh, active_delay=replace(plan.delay_plan, recovered_at=plan.delay_plan.end))]
        for bad in cases:
            with self.subTest(bad=bad):
                self.assertIsNone(optimize(bad).delay_plan)


if __name__ == '__main__': unittest.main()
