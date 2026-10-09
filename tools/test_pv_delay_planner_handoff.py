"""Real HA planner publication across the Mode 5 confirmation boundary.

No network/device I/O. The solver is real; only the live capture/proof timing
is controlled. A temporary lack of current PV precedes confirmation; LOAD
always remains on the shared nominal forecast, with raw telemetry preserved.
"""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import unittest

import test_pstryk_runtime as f
from custom_components.hoymiles_hit_modbus.pv_charge_delay import PvDelayCommitment


class PlannerHandoffTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = f.RuntimeTests.asyncSetUp
    asyncTearDown = f.RuntimeTests.asyncTearDown
    prices = f.RuntimeTests.prices
    pstryk = f.RuntimeTests.pstryk

    async def planned(self):
        await self.pstryk()
        self.start = f.NOW.replace(hour=11, minute=30)
        self.c.cache.days = {k: replace(v, fetched_at=self.start)
                             for k, v in self.c.cache.days.items()}
        prices = [f.PriceSlot(self.start + timedelta(minutes=30*i), .9 if i < 3 else .1)
                  for i in range(25)]
        pv = {p.start: 3. if p.start.hour < 15 else 0. for p in prices}
        self.settings = replace(self.settings, now=self.start, price_slots=prices,
            pv_by_slot_kwh=pv, conservative_pv_by_slot_kwh=pv, delay_pv_by_slot_kwh=pv,
            battery_wear_cost_pln_kwh=10., average_daily_load_kwh=5.,
            average_night_load_kwh=2., current_load_power_kw=.5,
            current_pv_power_kw=5., battery_soc_percent=70.)
        self.metadata.update(forecast_today_raw_kwh=40., forecast_today_p10_kwh=30.)
        self.hass.states.async_set(f.m.DELAY_HELPER, 'on')
        self.hass.states.async_set(f.m.OPTION_HELPERS['maximum_soc'], '100')
        with patch.object(f.m.dt_util, 'utcnow', return_value=self.start):
            await self.c.recalculate()
        self.original = self.c._accepted[1].delay_plan
        self.assertIsNotNone(self.original)
        first = self.c._accepted[0].slots[0]
        self.nominal_load_kw = first.load_kwh/first.hours

    async def test_confirmation_during_solve_rechecks_preconfirmation_negative_plan(self):
        await self.planned()
        now = self.start + timedelta(seconds=35)
        self.settings = replace(self.settings, now=now, current_load_power_kw=5., current_pv_power_kw=0.)
        proof = [None]
        self.rce._active_pv_delay_commitment = lambda _: proof[0]
        executor = self.hass.async_add_executor_job
        captured = []

        async def solve_then_confirm(function, *args):
            result = await executor(function, *args)
            if function is f.m.optimize:
                captured.append((args[0], result))
                if len(captured) == 1:
                    self.assertIsNone(args[0].active_delay)
                    self.assertIsNone(result.delay_plan)
                    self.settings = replace(self.settings, current_load_power_kw=.5, current_pv_power_kw=5.)
                    proof[0] = PvDelayCommitment('same-live-cycle', self.start, self.original.end)
            return result

        with patch.object(f.m.dt_util, 'utcnow', return_value=now), \
                patch.object(self.hass, 'async_add_executor_job', side_effect=solve_then_confirm):
            self.c.invalidate('recalculation_pending')
            await self.c.recalculate()
        self.assertTrue(self.rce._attributes['result_current'])
        self.assertTrue(self.rce._attributes['pv_charge_delay_execution_ready'],
                        'A preconfirmation no-window result revoked a now-confirmed live cycle')
        kept = self.c._accepted[1].delay_plan
        self.assertEqual((kept.start, kept.end, kept.recovered_at, kept.recovery_target_kwh),
                         (self.original.start, self.original.end, self.original.recovered_at,
                          self.original.recovery_target_kwh))
        self.assertEqual(self.rce._attributes['joint_plan_revision'],
                         self.tariff._attributes['joint_plan_revision'])

    async def test_planner_transient_budget_is_fixed_at_command_plus_180_seconds(self):
        await self.planned()
        proof = SimpleNamespace(transaction_id='one-command', started_at=self.start,
            command_sent_at=self.start, hard_deadline=self.original.end)
        self.rce._active_pv_delay_commitment = lambda _: proof
        for seconds, load in ((5, 5.), (35, 7.), (100, 6.502), (179, 9.), (180, .8)):
            now = self.start + timedelta(seconds=seconds)
            self.settings = replace(self.settings, now=now, current_load_power_kw=load)
            with patch.object(f.m.dt_util, 'utcnow', return_value=now):
                data, metadata, measured, _ = self.c._input()
                self.assertEqual(measured.current_load_power_kw, load,
                                 'Measured evidence must never be rewritten')
                if seconds < 180:
                    self.assertAlmostEqual(data.slots[0].load_kwh/data.slots[0].hours, self.nominal_load_kw)
                    self.assertEqual(data.current_load_power_kw, .5)
                    self.assertEqual(metadata['pv_charge_delay_planner_power_basis'], 'pre_command')
                else:
                    self.assertEqual(metadata['pv_charge_delay_planner_power_basis'], 'forecast_and_load_profile')
                self.c.invalidate('recalculation_pending')
                await self.c.recalculate()
            self.assertTrue(self.rce._attributes['pv_charge_delay_execution_ready'])
            self.assertEqual(self.c._accepted[1].delay_plan.end, self.original.end)

    async def test_lost_proof_does_not_keep_transient_reference(self):
        await self.planned()
        now = self.start + timedelta(seconds=30)
        self.settings = replace(self.settings, now=now, current_load_power_kw=5., current_pv_power_kw=0.)
        self.rce._active_pv_delay_commitment = lambda _: None
        with patch.object(f.m.dt_util, 'utcnow', return_value=now):
            self.c.invalidate('recalculation_pending')
            await self.c.recalculate()
        self.assertFalse(self.rce._attributes['pv_charge_delay_execution_ready'])

    async def test_loss_during_solve_cannot_publish_stale_binding(self):
        await self.planned()
        now = self.start + timedelta(seconds=35)
        proof = [PvDelayCommitment('live', self.start, self.original.end, self.start)]
        self.rce._active_pv_delay_commitment = lambda _: proof[0]
        self.settings = replace(self.settings, now=now, current_load_power_kw=7., current_pv_power_kw=0.)
        executor = self.hass.async_add_executor_job
        async def solve_then_lose(function, *args):
            result = await executor(function, *args)
            if function is f.m.optimize:
                proof[0] = None
            return result
        with patch.object(f.m.dt_util, 'utcnow', return_value=now), \
                patch.object(self.hass, 'async_add_executor_job', side_effect=solve_then_lose):
            self.c.invalidate()
            await self.c.recalculate()
        self.assertTrue(self.rce._attributes['result_current'])
        self.assertIsNone(self.c._accepted[0].active_delay)
        self.assertFalse(self.rce._attributes['pv_charge_delay_execution_ready'])
        self.assertAlmostEqual(self.c._accepted[0].slots[0].load_kwh/self.c._accepted[0].slots[0].hours, self.nominal_load_kw)
        self.assertEqual(self.c._accepted[0].current_load_power_kw, 7.)

    async def test_cache_cannot_extend_settling_or_replace_another_action(self):
        await self.planned()
        proof = PvDelayCommitment('live', self.start, self.original.end, self.start)
        self.rce._active_pv_delay_commitment = lambda _: proof
        for seconds in (150, 180):
            now = self.start + timedelta(seconds=seconds)
            self.settings = replace(self.settings, now=now, current_load_power_kw=.8)
            with patch.object(f.m.dt_util, 'utcnow', return_value=now):
                await self.c.recalculate()  # No explicit invalidate at the 180 s boundary.
            self.assertEqual(self.rce._attributes['pv_charge_delay_planner_settling_active'], seconds < 180)
            self.assertEqual(self.rce._attributes['pv_charge_delay_end'], self.original.end.isoformat())
        self.assertEqual(self.c._revision, 3)

    async def test_readback_cohort_gap_neither_applies_nor_refreshes_reference(self):
        await self.planned()
        proof = [PvDelayCommitment('live', self.start, self.original.end, self.start)]
        self.rce._active_pv_delay_commitment = lambda _: proof[0]
        now = self.start + timedelta(seconds=20)
        self.settings = replace(self.settings, now=now, current_load_power_kw=7.)
        with patch.object(f.m.dt_util, 'utcnow', return_value=now):
            self.c.invalidate()
            await self.c.recalculate()
            reference = self.c._pv_settling_basis
            proof[0] = None
            data, metadata, _, _ = self.c._input()
            self.assertFalse(metadata['pv_charge_delay_planner_settling_active'])
            self.assertAlmostEqual(data.slots[0].load_kwh/data.slots[0].hours, self.nominal_load_kw)
            self.assertEqual(data.current_load_power_kw, 7.)
            proof[0] = PvDelayCommitment('live', self.start, self.original.end, self.start)
            data, metadata, _, _ = self.c._input()
            self.assertTrue(metadata['pv_charge_delay_planner_settling_active'])
            self.assertIs(self.c._pv_settling_basis, reference)
            self.assertAlmostEqual(data.slots[0].load_kwh/data.slots[0].hours, self.nominal_load_kw)
            self.assertEqual(data.current_load_power_kw, .5)

    async def test_changed_consent_and_unrecoverable_forecast_do_not_create_trade(self):
        await self.planned()
        now = self.start + timedelta(seconds=20)
        self.settings = replace(self.settings, now=now, current_load_power_kw=7.)
        proof = PvDelayCommitment('live', self.start, self.original.end, self.start)
        self.rce._active_pv_delay_commitment = lambda _: proof
        with patch.object(f.m.dt_util, 'utcnow', return_value=now):
            self.c._input()  # Bind the real pre-command reference.
            self.settings = replace(self.settings, pv_by_slot_kwh={},
                delay_pv_by_slot_kwh={}, conservative_pv_by_slot_kwh={})
            revision = self.c._revision
            self.c.invalidate()
            await self.c.recalculate()
            self.assertFalse(self.rce._attributes['result_current'])
            self.assertEqual(self.c._revision, revision)
            self.hass.states.async_set(f.m.DELAY_HELPER, 'off')
            data, metadata, _, _ = self.c._input()
            self.assertIsNone(data.active_delay)
            self.assertFalse(metadata['pv_charge_delay_planner_settling_active'])

    async def test_confirmed_block_survives_power_deficit_after_settling(self):
        await self.planned()
        self.rce._active_pv_delay_commitment = lambda _: PvDelayCommitment(
            'live', self.start, self.original.end, self.start)
        for seconds, load, pv in ((200, 6., 5.), (330, .4, 0.), (490, 9., .1)):
            now = self.start + timedelta(seconds=seconds)
            self.settings = replace(self.settings, now=now,
                                    current_load_power_kw=load, current_pv_power_kw=pv)
            with patch.object(f.m.dt_util, 'utcnow', return_value=now):
                self.c.invalidate()
                await self.c.recalculate()
            self.assertTrue(self.rce._attributes['result_current'])
            self.assertTrue(self.rce._attributes['pv_charge_delay_execution_ready'],
                            'An instantaneous power deficit revoked a confirmed PV block')
            self.assertEqual(self.c._accepted_settings.current_load_power_kw, load)
            self.assertEqual(self.c._accepted_settings.current_pv_power_kw, pv)
            self.assertEqual(self.c._accepted[1].delay_plan.end, self.original.end)

    async def test_forecast_continuation_clips_lower_current_slot_and_ends_on_time(self):
        await self.planned()
        self.rce._active_pv_delay_commitment = lambda _: PvDelayCommitment(
            'live', self.start, self.original.end, self.start)
        now = self.start + timedelta(minutes=10)
        self.settings = replace(self.settings, now=now,
            delay_pv_by_slot_kwh={p.start:1. for p in self.settings.price_slots})
        with patch.object(f.m.dt_util, 'utcnow', return_value=now):
            data, _, measured, _ = self.c._input()
        self.assertAlmostEqual(data.slots[0].pv_kwh, 2.)
        self.assertAlmostEqual(data.delay_pv_kwh[0], 2./3.)
        self.assertEqual(measured.current_pv_power_kw, 5.)
        now = self.original.end
        self.settings = replace(self.settings, now=now)
        with patch.object(f.m.dt_util, 'utcnow', return_value=now):
            data, metadata, _, _ = self.c._input()
        self.assertIsNone(data.active_delay)
        self.assertEqual(metadata['pv_charge_delay_planner_power_basis'], 'live')


if __name__ == '__main__':
    unittest.main()
