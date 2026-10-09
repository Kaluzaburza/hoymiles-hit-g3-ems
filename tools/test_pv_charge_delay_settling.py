"""PV mode stability with actual HA lease gate, controller, and firmware TTL oracle."""
import asyncio
import importlib
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch
import test_supervisor_sensor_contract as adapter

for name in ('ems_supervisor','supervisor_runtime','supervisor_executor','supervisor_active_bridge',
             'supervisor_executor_codec','supervisor_active_controller','pv_charge_delay','pv_charge_delay_control'):
    sys.modules[name] = importlib.import_module('custom_components.hoymiles_hit_modbus.'+name)
import test_pv_charge_delay_control as f
from test_supervisor_control_lease import FirmwareLeaseModel


class SettlingTests(unittest.IsolatedAsyncioTestCase):
    async def make(self):
        writes=[]; clock=[f.NOW]
        async def persist(_): pass
        async def dispatch(write): writes.append(write)
        c=f.f.SupervisorActiveController(persist=persist,dispatch=dispatch,publish=lambda _:None,clock=lambda:clock[0])
        await c.async_initialize()
        await c.async_reconcile(f.frame(f.NOW,f.source(f.NOW,battery=-1500,grid=0)))
        return c,clock,writes

    def sensor(self,c,frame):
        _,_,_,sensor=adapter.environment()
        tx=c.record.transaction
        block=adapter.SENSOR._control_lease_block(tx.intent.command.ems_block)
        sensor._controller=c
        sensor._latest_active_frame=frame
        sensor._fresh_active_frame=lambda: sensor._latest_active_frame
        sensor._record_control_lease_gate=lambda **kw: None
        sensor._pause_state='off'
        sensor._control_lease_client=SimpleNamespace(handle=SimpleNamespace(
            transaction_id=tx.transaction_id,hard_deadline=tx.deadline,block=block))
        return sensor,block

    async def test_two_distinct_fc03_then_renewals_ignore_battery_power_ramp(self):
        c,clock,writes=await self.make()
        fw=FirmwareLeaseModel()
        first=None; seq=0
        for second in range(5,166,5):
            clock[0]=at=f.NOW+timedelta(seconds=second)
            charging=second<140
            src=f.source(at,mode=5,generation=10+second,
                battery=-1500 if charging else 0,grid=0 if charging else 1500)
            fr=f.frame(at,src,pending=second<=20,active=second>20)
            await c.async_reconcile(fr)
            if c.record.state is f.ActiveState.EXECUTING:
                fr=f.frame(at,src,active=True)
            sensor,block=self.sensor(c,fr)
            if first is None:
                first=c.record.transaction.command_sent_at
                self.assertTrue(fw.arm(block,transaction=c.record.transaction.transaction_id,hard_seconds=1200))
                fw.observe(block)
            fw.advance(second-fw.now)
            self.assertEqual(fw.state,'confirmed')
            with patch.object(adapter.SENSOR.dt_util,'utcnow',return_value=at):
                evidence=sensor._control_lease_renewal_evidence()
            self.assertIsNotNone(evidence,(second,c.record.state,c.record.reason))
            if second % 20 == 5:
                seq+=1
                self.assertTrue(fw.renew(authorized=True,sequence=seq))
            self.assertLessEqual(sensor._control_lease_authorization_deadline, f.END)
            self.assertEqual(c.record.state,f.ActiveState.WAITING_READBACK if second<20 else f.ActiveState.EXECUTING)
            self.assertEqual(len(writes),1)
            self.assertEqual(c.record.transaction.command_sent_at,first)
            self.assertEqual(c.record.transaction.deadline,f.END)

    async def test_timeout_repeat_bad_inputs_and_renewal_vetoes(self):
        c,clock,writes=await self.make()
        clock[0]=at=f.NOW+timedelta(seconds=5)
        fr=f.frame(at,f.source(at,mode=5,generation=12),pending=True)
        await c.async_reconcile(fr)
        self.assertEqual(c.record.state,f.ActiveState.WAITING_READBACK)
        for second in (10,20,100,149):
            # Same FC03 generation cannot qualify even after many callbacks.
            clock[0]=at=f.NOW+timedelta(seconds=second)
            fr=f.frame(at,f.source(at,mode=5,generation=12),pending=True)
            await c.async_reconcile(fr)
            self.assertEqual(c.record.state,f.ActiveState.WAITING_READBACK)
        sensor,_=self.sensor(c,fr)
        for bad in (replace(fr.execution,battery_soc_percent=100),
                    replace(fr.execution,physical_mode_code=3),
                    replace(fr.execution,battery_soc_observed_at=at-timedelta(seconds=121)),
                    replace(fr.execution,full_block_execution_ready=False),
                    replace(fr.execution,maximum_discharge_power_percent=20),
                    replace(fr.execution,bms_max_charge_current_a=0)):
            sensor._latest_active_frame=replace(fr,execution=bad)
            with patch.object(adapter.SENSOR.dt_util,'utcnow',return_value=at):
                self.assertIsNone(sensor._control_lease_renewal_evidence())
        sensor._latest_active_frame=fr
        sensor._master_stop_latched=True
        self.assertIsNone(sensor._control_lease_renewal_evidence())
        clock[0]=at=f.NOW+timedelta(seconds=180)
        await c.async_reconcile(f.frame(at,f.source(at,mode=5,generation=100),pending=True))
        self.assertNotEqual(c.record.state,f.ActiveState.EXECUTING)
        self.assertEqual(len(writes),2)
        self.assertEqual(writes[-1].ems_block.mode,f.EmsMode.SELF_USE)

    async def test_ack_budget_is_still_90_and_restart_cannot_resume_hold(self):
        c,clock,writes=await self.make()
        clock[0]=at=f.NOW+timedelta(seconds=90)
        await c.async_reconcile(f.frame(at,f.source(at,mode=0,generation=10),pending=True))
        self.assertNotEqual(c.record.state,f.ActiveState.WAITING_READBACK)
        self.assertNotEqual(c.record.state,f.ActiveState.EXECUTING)
        c,clock,writes=await self.make()
        async def no_io(_): pass
        recovered=f.f.SupervisorActiveController(persist=no_io,dispatch=no_io,publish=lambda _:None,
            persisted_record=c.record,clock=lambda:clock[0])
        await recovered.async_initialize()
        self.assertNotEqual(recovered.record.state,f.ActiveState.EXECUTING)
        self.assertIsNone(recovered._pv_hold_first_proof)


if __name__=='__main__': unittest.main()
