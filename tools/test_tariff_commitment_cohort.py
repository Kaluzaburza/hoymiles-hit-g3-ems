"""Production tariff coroutine waits for transient FC03 delivery, boundedly."""
import asyncio
from types import SimpleNamespace, MethodType
import unittest
from test_pstryk_runtime import NOW
from custom_components.hoymiles_hit_modbus.tariff_sensor import HoymilesTariffOptimizerSensor as Sensor


class CohortTests(unittest.IsolatedAsyncioTestCase):
    def probe(self, *, finish=True):
        pending=[False]; calls=[]
        probe=SimpleNamespace(_pstryk=None,_lifecycle_stopped=False,
            _forecast_gcf_policy_evaluation_cancel=None,_input_revision=SimpleNamespace(value=7),
            _full_plan_solver_calls=0,_active_commitment_cohort_source=lambda:pending[0])
        probe._async_wait_active_tariff_commitment_cohort=MethodType(Sensor._async_wait_active_tariff_commitment_cohort,probe)
        probe._current_input_fingerprint=lambda:('missing' if pending[0] else 'same physical commitment',)
        probe._optimizer_input=lambda:(object(),{})
        probe._mark_recalculation_pending=lambda:calls.append('pending')
        async def solve(*args):
            calls.append('solve'); pending[0]=True
            if finish:
                asyncio.get_running_loop().call_later(.05,lambda:pending.__setitem__(0,False))
            return object()
        probe.hass=SimpleNamespace(async_add_executor_job=solve)
        def reject(revision,fingerprint):
            calls.append(('checked',revision,fingerprint,probe._current_input_fingerprint()))
            # End this isolated publication probe at the real stale-result gate.
            return True
        probe._reject_stale_executor_result=reject
        return probe,pending,calls

    async def test_completed_solve_waits_then_checks_fresh_fingerprint(self):
        probe,pending,calls=self.probe()
        self.assertFalse(await Sensor._recalculate_locked(probe))
        self.assertEqual(calls, ['solve',('checked',7,('same physical commitment',),('same physical commitment',))])

    async def test_neither_permanently_partial_start_nor_result_can_publish(self):
        for initial in (False,True):
            probe,pending,calls=self.probe(finish=False); pending[0]=initial
            self.assertFalse(await asyncio.wait_for(Sensor._recalculate_locked(probe),1.))
            self.assertEqual(calls,['pending'] if initial else ['solve','pending'])

    async def test_wait_yields_and_unload_cancels_it(self):
        probe,pending,calls=self.probe(); pending[0]=True
        task=asyncio.create_task(probe._async_wait_active_tariff_commitment_cohort())
        await asyncio.sleep(.01)
        self.assertFalse(task.done())
        probe._lifecycle_stopped=True
        self.assertFalse(await task)


if __name__=='__main__': unittest.main()
