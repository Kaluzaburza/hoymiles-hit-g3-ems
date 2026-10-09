"""Actual Supervisor state projection and owner commitments with HA stubs."""
import asyncio, unittest
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
import test_supervisor_sensor_contract as f
from custom_components.hoymiles_hit_modbus import pv_charge_delay as delay


class SensorTests(unittest.TestCase):
    def setup_sensor(self):
        hass,entry,runtime,sensor=f.environment()
        def set_state(key,value,attrs=None):
            spec=next(s for s in f.SENSOR.SUPERVISOR_SOURCE_SPECS if s.key==key)
            entity=f._source_entity_id(spec,entry.entry_id)
            hass.states.values[entity]=f.FakeState(value,attrs or {})
        attrs=f._plan_attributes('rce_plan')
        attrs.update(pv_charge_delay_execution_ready=True,pv_charge_delay_end=(f.NOW+timedelta(hours=1)).isoformat(),
            rce_today_data_fresh=True,forecast_today_data_fresh=True,soc_data_fresh=True,gcf_execution_data_fresh=True)
        set_state('pv_charge_delay_enabled','on')
        set_state('rce_plan','ready',attrs)
        set_state('rce_enabled','on');set_state('allow_rce','on')
        sensor._resolve_source_entity_ids()
        return sensor,set_state,attrs

    def test_user_toggle_and_strict_forecast_qualification(self):
        sensor,set_state,attrs=self.setup_sensor()
        def read(): return sensor._build_snapshots(sensor._read_source_states(),f.NOW)[2]
        self.assertTrue(read().pv_charge_hold)
        self.assertTrue(read().pv_charge_hold_qualified)
        for value in (False,'true',1,None):
            attrs['forecast_today_data_fresh']=value
            set_state('rce_plan','ready',attrs)
            self.assertFalse(read().pv_charge_hold_qualified)
        set_state('pv_charge_delay_enabled','off')
        self.assertFalse(read().pv_charge_hold)

    def test_pending_ack_pins_target_despite_soc_rounding_change(self):
        sensor,_,_=self.setup_sensor()
        _,_,rce,tariff,rcm,execution=sensor._build_snapshots(sensor._read_source_states(),f.NOW)
        execution=replace(execution,battery_soc_percent=60.1)
        rce=replace(rce,current_soc_percent=60.1)
        transaction=SimpleNamespace(owner=f.SENSOR.ExecutionOwner.RCE,deadline=f.NOW+timedelta(minutes=30),
            intent=SimpleNamespace(action=f.SENSOR.ExecutionAction.PV_CHARGE_HOLD,
                command=SimpleNamespace(ems_block=SimpleNamespace(force_discharge_soc_percent_4305=61,
                    maximum_discharge_power_percent_4306=1))))
        sensor._controller=SimpleNamespace(record=SimpleNamespace(state=f.SENSOR.ActiveState.WAITING_READBACK,
            owner=f.SENSOR.ExecutionOwner.RCE,transaction=transaction))
        context=f.RUNTIME.build_execution_context(execution,now=f.NOW)
        pinned,_,_,_=sensor._apply_executor_commitment(rce,tariff,rcm,context,execution,now=f.NOW)
        self.assertFalse(pinned.active_latched)
        self.assertTrue(pinned.pv_charge_hold_target_from_transaction)
        self.assertEqual(pinned.latched_minimum_soc_percent,61)
        self.assertEqual(pinned.current_run_end,transaction.deadline)
        candidate=f.RUNTIME.build_rce_candidate(pinned,now=f.NOW)
        self.assertEqual(candidate.protected_soc_floor_percent,61.)
        self.assertTrue(candidate.start_eligible)

    def test_inactive_helper_default_cannot_supply_a_transaction_target(self):
        sensor,set_state,attrs=self.setup_sensor()
        attrs['pv_charge_hold_target_from_transaction']=True
        set_state('rce_plan','ready',attrs)
        set_state('rce_active','off')
        set_state('rce_latched_minimum_soc','0')
        rce=sensor._build_snapshots(sensor._read_source_states(),f.NOW)[2]
        self.assertEqual(rce.latched_minimum_soc_percent,0.)
        self.assertFalse(rce.pv_charge_hold_target_from_transaction)
        candidate=f.RUNTIME.build_rce_candidate(rce,now=f.NOW)
        self.assertTrue(candidate.start_eligible)
        self.assertEqual(candidate.protected_soc_floor_percent,
            delay.hold_soc_target(rce.current_soc_percent))

if __name__=='__main__': unittest.main()
