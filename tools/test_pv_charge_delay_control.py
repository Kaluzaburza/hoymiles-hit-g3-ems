"""Offline end-to-end lifecycle for Mode5 hold; never contacts an inverter."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'custom_components/hoymiles_hit_modbus'))
import test_supervisor_active_controller as f
import pv_charge_delay as delay
from pv_charge_delay_control import live_ready, sent_command_ready
from supervisor_runtime import build_rce_candidate
from supervisor_active_bridge import physical_verification, execution_gates, settings_from_execution_source
from supervisor_executor import ExecutionAction, ActiveState, VerificationStatus, ExecutionOwner, EmsMode
from ems_supervisor import RequestedAction, PolicyId, arbitrate_supervisor, SupervisorMode, SupervisorProfile, ExportState

NOW=f.NOW
END=NOW+timedelta(minutes=20)


def source(at, *, mode=0, generation=10, battery=0., grid=1500., pv=2000., load=500., **kw):
    stamp=at-timedelta(milliseconds=100)
    return f.execution_source(at,physical_mode_code=mode,full_block_generation=generation,
        full_block_generation_at=stamp,force_discharge_soc_percent=61 if mode==5 else 25,
        maximum_discharge_power_percent=1 if mode==5 else 40,
        bms_battery_power_w=battery,bms_battery_power_observed_at=stamp,
        power_cohort_complete=True,power_cohort_generation=generation,
        grid_power_w=grid,grid_power_observed_at=stamp,
        battery_power_w=battery,battery_power_observed_at=stamp,
        pv_power_w=pv,pv_power_observed_at=stamp,load_power_w=load,load_power_observed_at=stamp,**kw)


def frame(at, src, *, active=False, pending=False, allowed=True, **kw):
    base=f.rce_frame(at,src,deadline=END,floor=61.,power=1.,active=active,
        transaction_pending=pending,slot_end=END)
    rce=replace(base.rce,pv_charge_hold=True,pv_charge_hold_qualified=allowed,**kw)
    candidate=build_rce_candidate(rce,now=at)
    candidates=(candidate,*base.candidates[1:])
    decision=arbitrate_supervisor(mode=SupervisorMode.ACTIVE,profile=SupervisorProfile.BALANCED,
        context=base.context,candidates=candidates,now=at)
    return replace(base,rce=rce,candidates=candidates,decision=decision)


class ContractTests(unittest.TestCase):
    def test_hold_below_sale_reserve_keeps_non_discharge_target(self):
        candidate=frame(NOW,source(NOW,battery_soc_percent=21.),
            current_soc_percent=21.,reserve_ready=False,
            protected_soc_floor_percent=25.).candidates[0]
        self.assertTrue(candidate.start_eligible)
        self.assertEqual(candidate.requested_action,RequestedAction.PV_CHARGE_HOLD)
        self.assertEqual(candidate.protected_soc_floor_percent,22.)
        self.assertEqual(candidate.requested_energy_kwh,0.)

    def test_unqualified_plan_cannot_start(self):
        self.assertEqual(frame(NOW,source(NOW),allowed=False).candidates[0].requested_action,RequestedAction.NONE)

    def test_candidate_has_distinct_action_soc_plus_one_no_battery_sale(self):
        candidate=frame(NOW,source(NOW)).candidates[0]
        self.assertEqual(candidate.requested_action,RequestedAction.PV_CHARGE_HOLD)
        self.assertEqual(candidate.protected_soc_floor_percent,61)
        self.assertEqual(candidate.requested_energy_kwh,0.)
        self.assertTrue(candidate.start_eligible)
        self.assertEqual(frame(NOW,source(NOW)).decision.selected_policy,PolicyId.RCE)

    def test_new_hold_ignores_inactive_legacy_sale_latch(self):
        # Real HA defaults leave the old RCE latch at 0, even with a ready
        # PV window. A prior battery sale can leave any other old SOC floor.
        for old_floor in (0., 20., 61., 95., None):
            with self.subTest(old_floor=old_floor):
                candidate=frame(NOW,source(NOW,battery_soc_percent=27.),
                    current_soc_percent=27.,latched_minimum_soc_percent=old_floor).candidates[0]
                self.assertTrue(candidate.start_eligible)
                self.assertEqual(candidate.requested_action,RequestedAction.PV_CHARGE_HOLD)
                self.assertEqual(candidate.protected_soc_floor_percent,28.)

    def test_active_hold_keeps_its_confirmed_floor(self):
        candidate=frame(NOW,source(NOW,mode=5),active=True,
            current_soc_percent=60.4,latched_minimum_soc_percent=61.).candidates[0]
        self.assertTrue(candidate.continuation_eligible)
        self.assertEqual(candidate.protected_soc_floor_percent,61.)
        missing=frame(NOW,source(NOW,mode=5),active=True,
            current_soc_percent=60.4,latched_minimum_soc_percent=None).candidates[0]
        self.assertFalse(missing.continuation_eligible)

    def test_missing_stale_consent_and_pending_have_no_start(self):
        for kw in ({'allowed_by_user':False},{'enabled':False},{'sale_block_active':True},
                   {'result_current':False},{'recalculation_pending':True},
                   {'observed_at':NOW-timedelta(minutes=3)}, {'current_soc_percent':float('nan')}):
            with self.subTest(kw=kw):
                self.assertFalse(frame(NOW,source(NOW),**kw).candidates[0].start_eligible)

    def test_device_readiness_stays_required_without_pv_freshness_gate(self):
        self.assertTrue(live_ready(source(NOW),now=NOW))
        self.assertTrue(live_ready(replace(source(NOW),pv_power_observed_at=NOW-timedelta(seconds=20)),now=NOW))
        for src in (source(NOW,bms_max_charge_current_a=0),source(NOW,inverter_count=2),
                    replace(source(NOW),battery_soc_percent=100)):
            self.assertFalse(live_ready(src,now=NOW))

    def test_physical_predicate_confirms_mode_not_power_flows(self):
        at=NOW+timedelta(seconds=2)
        def proof(src): return physical_verification('pv-hold-1',ExecutionAction.PV_CHARGE_HOLD,
            src,command_sent_at=NOW,now=at)
        self.assertEqual(proof(source(at,mode=5,generation=12)).status,VerificationStatus.CONFIRMED)
        for src in (source(at,mode=5,battery=100),source(at,mode=5,battery=-100),
                    source(at,mode=5,grid=-100),source(at,mode=5,pv=400)):
            self.assertEqual(proof(src).status,VerificationStatus.CONFIRMED)
            self.assertIn('power_flows=diagnostic_only',proof(src).evidence)
        self.assertNotEqual(proof(replace(source(at),pv_power_observed_at=NOW)).status,VerificationStatus.CONFIRMED)


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_new_readback_continue_disable_restore(self):
        writes=[]; persisted=[]; clock=[NOW]
        async def persist(record): persisted.append(record)
        async def dispatch(write): writes.append(write)
        controller=f.SupervisorActiveController(persist=persist,dispatch=dispatch,
            publish=lambda _:None,clock=lambda:clock[0])
        await controller.async_initialize()
        await controller.async_reconcile(frame(NOW,source(NOW,battery=-1500,grid=0),
            latched_minimum_soc_percent=0.))
        self.assertEqual(controller.record.state,ActiveState.WAITING_READBACK)
        self.assertEqual(len(writes),1)
        intent=controller.record.transaction.intent
        self.assertEqual(intent.action,ExecutionAction.PV_CHARGE_HOLD)
        block=intent.command.ems_block
        self.assertEqual(block.force_discharge_soc_percent_4305,61)
        self.assertEqual(block.maximum_discharge_power_percent_4306,1)
        for key in ('self_use_soc_percent_4301','backup_soc_percent_4302','force_charge_soc_percent_4303','maximum_charge_power_percent_4304'):
            self.assertEqual(getattr(block,key),getattr(settings_from_execution_source(source(NOW)).ems_block,key))
        clock[0]=at=NOW+timedelta(seconds=2)
        await controller.async_reconcile(frame(at,source(at,mode=5,generation=12),pending=True))
        self.assertEqual(controller.record.state,ActiveState.WAITING_READBACK)
        clock[0]=at=NOW+timedelta(seconds=17)
        await controller.async_reconcile(frame(at,source(at,mode=5,generation=13),pending=True))
        self.assertEqual(controller.record.state,ActiveState.EXECUTING, controller.record)
        self.assertEqual(controller.record.transaction.physical_verification.status,VerificationStatus.CONFIRMED)
        clock[0]=at=NOW+timedelta(seconds=20)
        stable=frame(at,source(at,mode=5,generation=13),active=True)
        await controller.async_reconcile(stable)
        self.assertEqual(controller.record.state,ActiveState.EXECUTING, controller.record)
        self.assertEqual(len(writes),1)
        self.assertEqual(controller.record.transaction.deadline,END)
        from supervisor_executor_codec import record_to_dict, record_from_dict
        restored=record_from_dict(record_to_dict(controller.record))
        self.assertEqual(restored.transaction.intent.action,ExecutionAction.PV_CHARGE_HOLD)
        self.assertEqual(restored.transaction.intent.command,controller.record.transaction.intent.command)
        self.assertEqual(restored.transaction.deadline,END)
        self.assertTrue(sent_command_ready(intent,stable.execution,rce=stable.rce,now=at))
        clock[0]=at=NOW+timedelta(seconds=22)
        await controller.async_reconcile(frame(at,source(at,mode=5,generation=14),active=True,allowed=False))
        self.assertEqual(len(writes),2)
        restore=controller.record.transaction.restore_command.ems_block
        self.assertEqual(restore.mode,EmsMode.SELF_USE)
        clock[0]=at=NOW+timedelta(seconds=24)
        await controller.async_reconcile(frame(at,source(at,mode=0,generation=15),pending=True,allowed=False))
        self.assertEqual(controller.record.state,ActiveState.IDLE)
        self.assertEqual(controller.record.owner,ExecutionOwner.NONE)



class SafetyLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_failures_restore_instead_of_continuing(self):
        for reason in ('readback', 'ccl', 'end'):
            with self.subTest(reason=reason):
                writes=[];clock=[NOW]
                async def persist(_): pass
                async def dispatch(write): writes.append(write)
                controller=f.SupervisorActiveController(persist=persist,dispatch=dispatch,
                    publish=lambda _:None,clock=lambda:clock[0])
                await controller.async_initialize()
                await controller.async_reconcile(frame(NOW,source(NOW)))
                clock[0]=at=NOW+timedelta(seconds=2)
                await controller.async_reconcile(frame(at,source(at,mode=5,generation=12),pending=True))
                self.assertEqual(controller.record.state,ActiveState.WAITING_READBACK)
                clock[0]=at=NOW+timedelta(seconds=17)
                await controller.async_reconcile(frame(at,source(at,mode=5,generation=13),pending=True))
                self.assertEqual(controller.record.state,ActiveState.EXECUTING)
                clock[0]=at=END if reason=='end' else NOW+timedelta(seconds=20)
                src=source(at,mode=5,generation=13)
                if reason=='readback': src=replace(src,physical_mode_code=0)
                if reason=='ccl': src=replace(src,bms_max_charge_current_a=0.)
                await controller.async_reconcile(frame(at,src,active=True))
                self.assertNotEqual(controller.record.state,ActiveState.EXECUTING)
                self.assertEqual(len(writes),2)
                self.assertEqual(controller.record.transaction.restore_command.ems_block.mode,EmsMode.SELF_USE)

    async def test_off_grid_cannot_start(self):
        writes=[]
        async def persist(_): pass
        async def dispatch(write): writes.append(write)
        controller=f.SupervisorActiveController(persist=persist,dispatch=dispatch,
            publish=lambda _:None,clock=lambda:NOW)
        await controller.async_initialize()
        await controller.async_reconcile(frame(NOW,source(NOW,mode=3)))
        self.assertEqual(writes,[])

    def test_command_and_economic_identity_remain_distinct(self):
        import supervisor_canonical_ledger as ledger
        import supervisor_canonical_runtime as canonical
        import supervisor_accounting_runtime as accounting
        action=ledger.RequestedAction.PV_CHARGE_HOLD
        self.assertEqual(ledger._ACTION_PHYSICAL_EXPECTATION[action],ledger.PhysicalExpectation.PV_EXPORT_AND_HOUSE_SELF_CONSUMPTION)
        fr=frame(NOW,source(NOW))
        hold_target=canonical._pv_hold_target(fr, current_interval=True,
            projected_soc_percent=fr.execution.battery_soc_percent)
        target,generation,values=canonical._command_for_action(RequestedAction.PV_CHARGE_HOLD,
            {'policy':{}},fr,settings_from_execution_source(fr.execution),
            pv_hold_target_percent=hold_target)
        self.assertEqual({v.name:v.value for v in values}['force_discharge_soc'],61)
        self.assertEqual({v.name:v.value for v in values}['maximum_discharge_power'],1.)
        self.assertEqual(accounting._ACTION_TO_REQUEST[ExecutionAction.PV_CHARGE_HOLD][1],action)

if __name__=='__main__': unittest.main()
