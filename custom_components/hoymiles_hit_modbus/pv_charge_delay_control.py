"""PV charge deferral with house self-consumption, using the existing RCE owner.

This module cannot issue writes. The Supervisor/executor retains full FC16,
FC03, ownership, deadline, physical verification, lease and restore control.
"""
from dataclasses import replace
from datetime import timezone
import math

if __package__:
    from . import pv_charge_delay as delay
    from . import supervisor_runtime as runtime
    from .ems_supervisor import (PolicyId, RequestedAction, ActuatorScope, PriorityClass,
        NeedClass, ReasonCode, EconomicValueStatus, PhysicalMode, PolicyCandidate)
else:
    import pv_charge_delay as delay
    import supervisor_runtime as runtime
    from ems_supervisor import (PolicyId, RequestedAction, ActuatorScope, PriorityClass,
        NeedClass, ReasonCode, EconomicValueStatus, PhysicalMode, PolicyCandidate)


def candidate(snapshot, *, now):
    now = runtime._require_now(now)
    active = snapshot.active_latched is True
    end = snapshot.current_run_end
    target = snapshot.latched_minimum_soc_percent
    try:
        # The legacy RCE helper survives an earlier battery sale and defaults
        # to zero. A live executor transaction also pins its target while
        # awaiting readback; an inactive helper cannot provide that authority.
        if not active and snapshot.pv_charge_hold_target_from_transaction is not True:
            target = delay.hold_soc_target(snapshot.current_soc_percent)
        valid = bool(runtime._plan_contract_valid(snapshot)
            and runtime._fresh(snapshot.observed_at, now=now, maximum_age_seconds=120.)
            and snapshot.result_current is True and snapshot.recalculation_pending is False
            and snapshot.pv_charge_hold_qualified is True
            and snapshot.allowed_by_user is True and snapshot.enabled is True
            and snapshot.sale_block_active is False
            and snapshot.current_slot_planned is True
            and type(snapshot.current_soc_percent) in (float,int)
            and math.isfinite(snapshot.current_soc_percent)
            and type(target) in (float,int) and math.isfinite(target)
            and 0 <= snapshot.current_soc_percent < target <= 100
            and float(target).is_integer()
            and runtime._is_aware(end) and end > now
            and type(snapshot.system_power_kw) in (float,int)
            and math.isfinite(snapshot.system_power_kw) and snapshot.system_power_kw > 0)
        if active:
            valid = valid and snapshot.active_4305_readback_percent == target and snapshot.active_4306_readback_percent == 1.
        start = valid and not active and (end-now).total_seconds() >= 420
    except (ValueError,TypeError,OverflowError):
        valid = start = False
    if not valid:
        return runtime._no_action(PolicyId.RCE,snapshot,now=now,available=False,
            blocked_reason=ReasonCode.UNAVAILABLE,active_latched=active,local_hard_stop=active)
    target = float(target)
    fingerprint = runtime._sha256({'policy':PolicyId.RCE,'action':RequestedAction.PV_CHARGE_HOLD,
        'mode':5,'target_4305':target,'target_4306_percent':1.,'valid_until':end})
    return runtime._finalize(PolicyCandidate(
        **runtime._common_candidate_values(snapshot,now), policy_id=PolicyId.RCE,
        candidate_revision=0,available=True,start_eligible=start,continuation_eligible=active,
        active_latched=active,local_hard_stop=False,requested_action=RequestedAction.PV_CHARGE_HOLD,
        actuator_scope=ActuatorScope.EMS_BLOCK_4300_4306,priority_class=PriorityClass.ECONOMIC,
        need_class=NeedClass.OPTIONAL,reason_code=ReasonCode.ECONOMIC_CANDIDATE,blocked_reason=None,
        valid_from=None,valid_until=end,desired_actuator_fingerprint=fingerprint,
        economic_value_status=EconomicValueStatus.UNAVAILABLE,requested_mode=PhysicalMode.GRID_DISCHARGE,
        requested_power_kw=snapshot.system_power_kw*.01,requested_energy_kwh=0.,
        protected_soc_floor_percent=target))


def live_ready(source, *, now):
    """Mode-control readiness; inverter power flows are diagnostic only.

    PV delay delegates battery routing to the inverter's Mode 5. Asynchronous
    PV/LOAD/GRID/BAT reports, including BMS power, cannot start or stop it.
    Topology, SOC and device capability/readiness keep their own authority.
    """
    if __package__:
        from .supervisor_active_bridge import _full_block_topology_ready
    else:
        from supervisor_active_bridge import _full_block_topology_ready
    try:
        def fresh(value, stamp, minimum, maximum_age=15.):
            return (type(value) in (float,int) and math.isfinite(value) and value >= minimum
                and stamp is not None and stamp.tzinfo is not None
                and 0 <= (now-stamp).total_seconds() <= maximum_age)
        return bool(_full_block_topology_ready(source, now)
            and fresh(source.battery_soc_percent,source.battery_soc_observed_at,0.,120.)
            and source.battery_soc_percent <= 99
            and fresh(source.bms_voltage_v,source.bms_voltage_observed_at,.001,300.)
            and fresh(source.bms_max_charge_current_a,source.bms_charge_current_observed_at,.001,300.)
            and fresh(source.bms_max_discharge_current_a,source.bms_discharge_current_observed_at,.001,300.))
    except (ValueError,TypeError,AttributeError,OverflowError):
        return False


def sent_command_ready(intent, source, *, rce, now):
    block = intent.command.ems_block
    return bool(block is not None and live_ready(source,now=now)
        and source.battery_soc_percent < block.force_discharge_soc_percent_4305
        and block.maximum_discharge_power_percent_4306 == 1.
        and type(rce.system_power_kw) in (float,int) and math.isfinite(rce.system_power_kw)
        and rce.system_power_kw > 0
        and rce.system_power_kw*.01 <= source.bms_voltage_v*source.bms_max_discharge_current_a*.95/1000.)


def pending_same_command(intent, snapshot, *, now):
    """An explicit publication gap may retain only the originally sent hold.

    This is not a start candidate or a current plan. The controller bounds the
    gap and proves the physical command; the lease gate shares that decision.
    """
    block = intent.command.ems_block
    if (block is None or snapshot.pv_charge_hold is not True
            or snapshot.result_current is not False
            or snapshot.recalculation_pending is not True):
        return False
    checked = candidate(replace(snapshot, result_current=True, recalculation_pending=False), now=now)
    return bool(checked.available
        and checked.requested_action is RequestedAction.PV_CHARGE_HOLD
        and checked.valid_until == intent.deadline
        and checked.protected_soc_floor_percent == block.force_discharge_soc_percent_4305
        and block.maximum_discharge_power_percent_4306 == 1.)
