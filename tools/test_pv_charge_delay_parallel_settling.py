"""Parallel Master ramp and renewal exercise the actual HA lease path."""
from dataclasses import replace
from unittest.mock import patch
import unittest
import test_pv_charge_delay_settling as settling

class ParallelSettlingTests(settling.SettlingTests):
    def setUp(self):
        original=settling.f.source
        def plant(at,**kw):
            source=original(at,**kw)
            return replace(source,machine_type_code=1,inverter_count=2,
                direct_306_execution_ready=False,direct_259_execution_ready=False,
                bms_battery_power_w=source.battery_power_w*.5)
        active=patch.object(settling.f,'source',plant)
        active.start()
        self.addCleanup(active.stop)

if __name__=='__main__':unittest.main()
