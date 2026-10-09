"""Installation 3 startup: FC03 authority despite delayed diagnostic power."""
from dataclasses import replace
from datetime import timedelta
import unittest
from unittest.mock import patch
import test_pv_charge_delay_settling as s

p = s.f


class InitialCohortWait(unittest.IsolatedAsyncioTestCase):
    async def make(self):
        c, clock, writes = await s.SettlingTests().make()
        c._control_lease_valid_until = lambda: p.NOW + timedelta(seconds=120)
        return c, clock, writes

    def delayed(self, at, *, old_bms=False, incomplete=False, active=False):
        src = p.source(at, mode=5, generation=10+int((at-p.NOW).total_seconds()),
                       battery=-1500., grid=0.)
        if old_bms:
            src = replace(src, bms_battery_power_observed_at=p.NOW-timedelta(seconds=4))
        if incomplete:
            src = replace(src, power_cohort_complete=False)
        return p.frame(at, src, pending=not active, active=active)

    async def test_installation_3_ack_before_bms_then_incomplete_cohort_does_not_restart(self):
        c, clock, writes = await self.make()
        original = c.record.transaction
        for second in (3, 8, 16, 20, 40):
            clock[0] = at = p.NOW+timedelta(seconds=second)
            fr = self.delayed(at, old_bms=second<20, incomplete=second>=20, active=second>20)
            await c.async_reconcile(fr)
            self.assertEqual(c.record.state, p.ActiveState.WAITING_READBACK
                             if second < 20 else p.ActiveState.EXECUTING,
                             (second,c.record.reason))
            if c.record.state is p.ActiveState.EXECUTING:
                fr = p.frame(at, fr.execution, active=True)
            self.assertEqual(c.record.transaction.transaction_id, original.transaction_id)
            self.assertEqual(c.record.transaction.command_sent_at, original.command_sent_at)
            self.assertEqual(c.record.transaction.deadline, original.deadline)
            self.assertEqual(c.record.transaction.readback_result, p.VerificationStatus.CONFIRMED)
            self.assertEqual(len(writes),1)
            sensor,_ = s.SettlingTests().sensor(c, fr)
            with patch.object(s.adapter.SENSOR.dt_util,'utcnow',return_value=at):
                self.assertIsNotNone(sensor._control_lease_renewal_evidence())
        for second in (55,70):
            clock[0] = at = p.NOW+timedelta(seconds=second)
            await c.async_reconcile(p.frame(at,p.source(at,mode=5,generation=second+10),active=True))
        self.assertEqual(c.record.state,p.ActiveState.EXECUTING)
        self.assertEqual(len(writes),1)

    async def test_repeated_fc03_generation_never_creates_execution_or_extends_wait(self):
        c,clock,writes = await self.make()
        for second in (3,60,119,179):
            clock[0]=at=p.NOW+timedelta(seconds=second)
            fr=self.delayed(at,incomplete=True)
            fr=replace(fr,execution=replace(fr.execution,full_block_generation=13))
            await c.async_reconcile(fr)
            self.assertEqual(c.record.state,p.ActiveState.WAITING_READBACK)
            self.assertLessEqual(c.execution_watchdog[1],p.NOW+timedelta(seconds=180))
            self.assertEqual(c.record.transaction.deadline,p.END)
        clock[0]=at=p.NOW+timedelta(seconds=180)
        await c.async_reconcile(self.delayed(at,incomplete=True))
        self.assertNotEqual(c.record.state,p.ActiveState.EXECUTING)
        self.assertNotEqual(c.record.state,p.ActiveState.WAITING_READBACK)
        self.assertEqual(len(writes),2)

    async def test_critical_controls_are_not_flow_stabilization(self):
        for change in ({'bms_max_charge_current_a':0.},
                       {'bms_max_discharge_current_a':0.},
                       {'bms_max_discharge_current_a':.01},
                       {'physical_mode_code':3}, {'battery_soc_percent':61.}):
            with self.subTest(change=change):
                c,clock,writes=await self.make()
                clock[0]=at=p.NOW+timedelta(seconds=3)
                fr=self.delayed(at,incomplete=True)
                fr=replace(fr,execution=replace(fr.execution,**change))
                await c.async_reconcile(fr)
                self.assertNotEqual(c.record.state,p.ActiveState.EXECUTING)
                self.assertNotEqual(c.record.state,p.ActiveState.WAITING_READBACK)
        c,clock,writes=await self.make()
        clock[0]=at=p.NOW+timedelta(seconds=3)
        fr=self.delayed(at,old_bms=True)
        fr=replace(fr,context=replace(fr.context,export_state=p.f.ExportState.CONFIRMED_ZERO_EXPORT))
        await c.async_reconcile(fr)
        self.assertNotEqual(c.record.state,p.ActiveState.WAITING_READBACK)

    async def test_no_lease_no_gap_exception_for_missing_fc03(self):
        c,clock,writes=await self.make()
        c._control_lease_valid_until=lambda:None
        clock[0]=at=p.NOW+timedelta(seconds=3)
        fr=self.delayed(at,old_bms=True)
        fr=replace(fr,execution=replace(fr.execution,full_block_execution_ready=False))
        self.assertFalse(c._pv_initial_input_wait(fr,now=at))
        self.assertFalse(c.pv_hold_settling_lease_authorized(fr,now=at,lease_ttl_seconds=1))
        await c.async_reconcile(fr)
        self.assertNotEqual(c.record.state,p.ActiveState.EXECUTING)

    async def test_user_stop_pause_and_revoked_consent_end_the_wait(self):
        for reason in ('stop', 'pause', 'consent', 'disabled', 'plan_removed'):
            with self.subTest(reason=reason):
                c,clock,writes=await self.make()
                clock[0]=at=p.NOW+timedelta(seconds=3)
                fr=self.delayed(at,old_bms=True)
                if reason=='pause':
                    fr=replace(fr,decision=replace(fr.decision,supervisor_mode=p.SupervisorMode.OFF))
                elif reason in ('consent','disabled','plan_removed'):
                    field={'consent':'allowed_by_user','disabled':'enabled',
                           'plan_removed':'current_slot_planned'}[reason]
                    fr=p.frame(at,fr.execution,pending=True,**{field:False})
                if reason=='stop':
                    await c.async_master_stop(fr)
                else:
                    await c.async_reconcile(fr)
                self.assertFalse(c._pv_initial_input_wait(fr,now=at))
                self.assertNotEqual(c.record.state,p.ActiveState.WAITING_READBACK)
                self.assertNotEqual(c.record.state,p.ActiveState.EXECUTING)

    async def test_pending_replan_keeps_deadline_but_invalid_timestamps_do_not(self):
        c,clock,writes=await self.make()
        clock[0]=at=p.NOW+timedelta(seconds=3)
        src=self.delayed(at,old_bms=True).execution
        fr=p.frame(at,src,pending=True,result_current=False,recalculation_pending=True)
        await c.async_reconcile(fr)
        self.assertEqual(c.record.state,p.ActiveState.WAITING_READBACK)
        self.assertEqual(c.record.transaction.deadline,p.END)
        for value in (None,at+timedelta(seconds=1),at-timedelta(seconds=121)):
            with self.subTest(value=value):
                c,clock,writes=await self.make()
                clock[0]=at
                fr=self.delayed(at,incomplete=True)
                fr=replace(fr,execution=replace(fr.execution,full_block_generation_at=value))
                await c.async_reconcile(fr)
                self.assertFalse(c._pv_hold_startup_controls_ready(fr,now=at))
                self.assertFalse(c.pv_hold_settling_lease_authorized(fr,now=at,lease_ttl_seconds=1))


if __name__=='__main__': unittest.main()
