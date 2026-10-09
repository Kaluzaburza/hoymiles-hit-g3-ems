"""Parallel PV hold requires validated Master topology and physical Mode 5."""
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
import unittest
import test_pv_charge_delay_control as f

def parallel(at, **kw):
    src=f.source(at, machine_type_code=1, inverter_count=2, **kw)
    return replace(src, direct_306_execution_ready=False, direct_259_execution_ready=False)

class ParallelTests(unittest.TestCase):
    def test_valid_master_enabled_slave_stale_invalid_rejected(self):
        self.assertTrue(f.live_ready(parallel(f.NOW),now=f.NOW))
        for kw in ({'machine_type_code':2},{'inverter_count':1},{'inverter_count':2.5},
                   {'topology_generation_at':f.NOW-timedelta(seconds=181)}):
            with self.subTest(kw=kw):
                self.assertFalse(f.live_ready(replace(parallel(f.NOW),**kw),now=f.NOW))

    def test_master_bms_does_not_equal_aggregate_during_pv_ramp(self):
        at=f.NOW+timedelta(seconds=20)
        src=replace(parallel(at,mode=5,generation=12,battery=-1500,grid=0),
                    bms_battery_power_w=-750)
        proof=f.physical_verification('parallel-pv',f.ExecutionAction.PV_CHARGE_HOLD,
            src,command_sent_at=f.NOW,now=at,allow_pv_hold_settling=True)
        self.assertEqual(proof.status,f.VerificationStatus.CONFIRMED)
        self.assertIn('power_flows=diagnostic_only',proof.evidence)

    def test_aggregate_and_master_bms_powers_do_not_control_mode(self):
        at=f.NOW+timedelta(seconds=20)
        src=parallel(at,mode=5,generation=12)
        def proof(v):
            return f.physical_verification('parallel-pv',f.ExecutionAction.PV_CHARGE_HOLD,
                v,command_sent_at=f.NOW,now=at,allow_pv_hold_settling=True)
        self.assertEqual(proof(src).status,f.VerificationStatus.CONFIRMED)
        for kw in ({'bms_battery_power_w':100.},{'battery_power_w':100.},
                   {'grid_power_w':-100.},
                   {'power_cohort_complete':False}):
            with self.subTest(kw=kw):
                self.assertEqual(proof(replace(src,**kw)).status,f.VerificationStatus.CONFIRMED)
        self.assertNotEqual(proof(replace(src,machine_type_code=2)).status,f.VerificationStatus.CONFIRMED)

class ParallelLifecycleTests(f.LifecycleTests):
    async def test_start_new_readback_continue_disable_restore(self):
        original=f.source
        def plant(at,**kw):
            return replace(original(at,**kw),machine_type_code=1,inverter_count=2,
                direct_306_execution_ready=False,direct_259_execution_ready=False)
        with patch.object(f,'source',plant):
            await super().test_start_new_readback_continue_disable_restore()

if __name__=='__main__':unittest.main()
