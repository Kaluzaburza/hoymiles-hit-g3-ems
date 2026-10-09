#!/usr/bin/env python3
"""Dependency-free contract tests for the Active arbitration bridge."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path.insert(0, str(COMPONENT))

from ems_supervisor import (  # noqa: E402
    ActuatorScope,
    EconomicValueStatus,
    ExecutionContext,
    ExportState,
    NeedClass,
    OwnerKind,
    PhysicalMode,
    PolicyCandidate,
    PolicyId,
    PriorityClass,
    ReasonCode,
    RequestedAction,
    SupervisorMode,
    SupervisorProfile,
    arbitrate_supervisor,
)
from supervisor_active_bridge import (  # noqa: E402
    ActiveBridgeError,
    RCE_RECALCULATION_HOLD_SECONDS,
    RCE_RETARGET_COHORT_HOLD_SECONDS,
    authorization_matches,
    build_actuator_intent,
    build_same_window_rce_retarget,
    correlated_observed_owner,
    execution_gates,
    physical_verification,
    rce_sent_command_within_live_bms_limit,
    same_window_rce_execution_hold_seconds,
    same_window_rce_execution_hold_authorized,
    same_window_rce_retarget_wait_authorized,
    same_window_rce_target_authorized,
    same_window_rce_wait_authorized,
    settings_from_execution_source,
)
from supervisor_executor import (  # noqa: E402
    ActiveState,
    EmsMode,
    ExecutionAction,
    ExecutionOwner,
    ExecutionReason,
    RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
    SupervisorExecutor,
    VerificationStatus,
)
from supervisor_runtime import (  # noqa: E402
    ExecutionSourceSnapshot,
    RcePlanStatus,
    RceSourceSnapshot,
    RcmSourceSnapshot,
    TariffSourceSnapshot,
)


NOW = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
HASH = "a" * 64
CHECKS = 0


def check(condition: bool, message: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(message)


def source(**overrides: object) -> ExecutionSourceSnapshot:
    values: dict[str, object] = {
        "physical_mode_code": 0,
        "full_block_generation": 10,
        "full_block_generation_at": NOW - timedelta(seconds=1),
        "self_use_soc_percent": 20.0,
        "backup_soc_percent": 80.0,
        "force_charge_soc_percent": 90.0,
        "maximum_charge_power_percent": 50.0,
        "force_discharge_soc_percent": 25.0,
        "maximum_discharge_power_percent": 40.0,
        "full_block_execution_ready": True,
        "direct_306_execution_ready": True,
        "direct_259_execution_ready": True,
        "machine_type_code": 0,
        "inverter_count": 1,
        "topology_generation_at": NOW - timedelta(seconds=1),
        "battery_soc_percent": 60.0,
        "battery_soc_observed_at": NOW - timedelta(seconds=1),
        "bms_voltage_v": 51.2,
        "bms_voltage_observed_at": NOW - timedelta(seconds=1),
        "bms_max_charge_current_a": 100.0,
        "bms_charge_current_observed_at": NOW - timedelta(seconds=1),
        "bms_max_discharge_current_a": 100.0,
        "bms_discharge_current_observed_at": NOW - timedelta(seconds=1),
        "gcf_enable_code": 0,
        "effective_export_limit_percent": 100.0,
        "gcf_generation": 11,
        "gcf_generation_at": NOW - timedelta(seconds=1),
        "gcf_cohort_coherent": True,
        "battery_charge_limit_generation": 12,
        "battery_charge_limit_generation_at": NOW - timedelta(seconds=1),
        "battery_charge_limit_percent": 80.0,
        "hardware_readback_supported": True,
    }
    values.update(overrides)
    return ExecutionSourceSnapshot(**values)


_SHAPES = {
    PolicyId.RCE: (
        RequestedAction.RCE_EXPORT,
        ActuatorScope.EMS_BLOCK_4300_4306,
        PriorityClass.ECONOMIC,
        NeedClass.OPTIONAL,
    ),
    PolicyId.TARIFF: (
        RequestedAction.TARIFF_BATTERY_CHARGE,
        ActuatorScope.EMS_BLOCK_4300_4306,
        PriorityClass.REQUIRED_ENERGY,
        NeedClass.MANDATORY,
    ),
    PolicyId.RCM: (
        RequestedAction.RCM_ABSORB_PV,
        ActuatorScope.DIRECT_306,
        PriorityClass.PREVENTIVE_GRID,
        NeedClass.PREVENTIVE,
    ),
}


def candidate(policy: PolicyId, **overrides: object) -> PolicyCandidate:
    action, scope, priority, need = _SHAPES[policy]
    values: dict[str, object] = {
        "schema_version": 1,
        "policy_id": policy,
        "observed_at": NOW - timedelta(seconds=1),
        "allowed_by_user": True,
        "enabled": True,
        "available": True,
        "result_current": True,
        "recalculation_pending": False,
        "input_revision": 1,
        "candidate_revision": {PolicyId.RCE: 11, PolicyId.TARIFF: 12, PolicyId.RCM: 13}[policy],
        "start_eligible": True,
        "continuation_eligible": True,
        "active_latched": False,
        "local_hard_stop": False,
        "requested_action": action,
        "actuator_scope": scope,
        "priority_class": priority,
        "need_class": need,
        "reason_code": (
            ReasonCode.REQUIRED_ENERGY_RESTORE
            if policy is PolicyId.TARIFF
            else ReasonCode.PREVENTIVE_VOLTAGE_ACTION
            if policy is PolicyId.RCM
            else ReasonCode.ECONOMIC_CANDIDATE
        ),
        "blocked_reason": None,
        "valid_from": None,
        "valid_until": NOW + timedelta(minutes=20),
        "desired_actuator_fingerprint": HASH,
        "economic_value_status": EconomicValueStatus.UNAVAILABLE,
    }
    values.update(overrides)
    return PolicyCandidate(**values)


def no_action(policy: PolicyId) -> PolicyCandidate:
    return candidate(
        policy,
        requested_action=RequestedAction.NONE,
        actuator_scope=ActuatorScope.NONE,
        priority_class=PriorityClass.NONE,
        need_class=NeedClass.NONE,
        reason_code=ReasonCode.NO_ACTION,
        start_eligible=False,
        continuation_eligible=False,
        desired_actuator_fingerprint=None,
    )


def decide(selected: PolicyCandidate):
    candidates = tuple(
        selected if policy is selected.policy_id else no_action(policy)
        for policy in PolicyId
    )
    context = ExecutionContext(
        observed_at=NOW,
        physical_mode=PhysicalMode.SELF_USE,
        physical_mode_fresh=True,
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        transaction_pending=False,
        transaction_owner_kind=OwnerKind.NONE,
        full_block_execution_ready=True,
        direct_306_execution_ready=True,
        direct_259_execution_ready=True,
        topology_full_block_allowed=True,
        topology_direct_register_allowed=True,
        charge_direction_ready=True,
        discharge_direction_ready=True,
        critical_bms_ready=True,
        export_state=ExportState.VERIFIED_ALLOWED,
        battery_soc_percent=50.0,
        physical_protected_soc_floor_percent=10.0,
    )
    decision = arbitrate_supervisor(
        mode=SupervisorMode.ACTIVE,
        profile=SupervisorProfile.BALANCED,
        context=context,
        candidates=candidates,
        now=NOW,
    )
    return decision, candidates


def build(selected: PolicyCandidate):
    decision, candidates = decide(selected)
    intent = build_actuator_intent(
        decision,
        candidates,
        settings_from_execution_source(source()),
        rce=RceSourceSnapshot(
            protected_soc_floor_percent=30,
            effective_discharge_power_percent=40,
        ),
        tariff=TariffSourceSnapshot(
            target_soc_percent=85,
            command_charge_power_percent=50,
        ),
        rcm=RcmSourceSnapshot(
            recommended_charge_limit_percent=60,
            recommended_export_limit_percent=0,
            pre_discharge_target_soc_percent=40,
            pre_discharge_power_percent=20,
        ),
        now=NOW,
    )
    check(intent is not None, "selected candidate must create an intent")
    return intent, decision, candidates


def test_settings_and_gates() -> None:
    settings = settings_from_execution_source(source())
    check(settings.ems_block.mode is EmsMode.SELF_USE, "mode must come from FC03")
    check(settings.ems_generation == 10, "EMS generation must be exact")
    check(settings.battery_generation == 12, "306 generation must be exact")
    gates = execution_gates(
        source(),
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
    )
    check(gates.inputs_fresh and gates.full_block_topology_ready, "fresh single topology must pass")
    check(gates.observed_owner is None, "none must not become manual")
    delayed_ems = source(
        full_block_generation_at=NOW - timedelta(seconds=16.64),
    )
    strict_delayed = execution_gates(
        delayed_ems,
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
    )
    relaxed_delayed = execution_gates(
        delayed_ems,
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
        maximum_readback_age_seconds=RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
    )
    check(
        not strict_delayed.inputs_fresh
        and not strict_delayed.full_block_ready
        and relaxed_delayed.inputs_fresh
        and relaxed_delayed.full_block_ready
        and relaxed_delayed.direct_259_ready,
        "the 15-30 second EMS budget did not remain an explicit gate override",
    )
    at_boundary = execution_gates(
        source(full_block_generation_at=NOW - timedelta(seconds=30)),
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
        maximum_readback_age_seconds=RCE_EXECUTING_EMS_MAX_AGE_SECONDS,
    )
    check(
        not at_boundary.inputs_fresh
        and not at_boundary.full_block_ready
        and not at_boundary.direct_259_ready,
        "RCE continuation retained authority at the exclusive 30-second boundary",
    )
    below_floor = execution_gates(
        source(battery_soc_percent=24.0, self_use_soc_percent=25.0),
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
    )
    check(below_floor.bms_fresh, "fresh low SOC must keep the BMS gate current")
    check(below_floor.charge_direction_ready, "low SOC must allow safe charging")
    check(
        not below_floor.discharge_direction_ready,
        "low SOC must block discharge/export starts",
    )
    rcm_below_policy_floor = execution_gates(
        source(battery_soc_percent=24.0, self_use_soc_percent=10.0),
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
        candidate=candidate(
            PolicyId.RCM,
            requested_action=RequestedAction.RCM_PRE_DISCHARGE,
            actuator_scope=ActuatorScope.EMS_BLOCK_4300_4306,
            protected_soc_floor_percent=25.0,
            target_soc_percent=45.0,
        ),
    )
    check(
        not rcm_below_policy_floor.discharge_direction_ready,
        "SOC 24 must not pre-discharge below the RCEm 25/45 policy floor",
    )
    tariff_at_low_soc = execution_gates(
        source(battery_soc_percent=24.0, self_use_soc_percent=10.0),
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
        candidate=candidate(
            PolicyId.TARIFF,
            requested_action=RequestedAction.TARIFF_BATTERY_CHARGE,
            target_soc_percent=85.0,
        ),
    )
    check(
        tariff_at_low_soc.charge_direction_ready,
        "SOC 24 must keep tariff charging eligible when BMS can accept energy",
    )
    foreign = execution_gates(
        source(),
        owner_kind=OwnerKind.FOREIGN,
        owner_conflict=False,
        now=NOW,
    )
    check(foreign.observed_owner == "foreign", "foreign owner provenance must remain explicit")
    try:
        settings_from_execution_source(source(backup_soc_percent=20))
    except (ActiveBridgeError, ValueError):
        pass
    else:
        raise AssertionError("invalid backup SOC must fail closed")


def test_float_transport_noise_is_quantized_but_real_off_step_values_fail() -> None:
    noisy = settings_from_execution_source(
        source(
            physical_mode_code=5.0,
            self_use_soc_percent=20.0,
            backup_soc_percent=90.0,
            force_charge_soc_percent=70.0,
            maximum_charge_power_percent=99.70000457763672,
            force_discharge_soc_percent=44.0,
            maximum_discharge_power_percent=21.2000007629395,
            effective_export_limit_percent=199.90000915527344,
            battery_charge_limit_percent=79.99999924,
        )
    )
    check(noisy.ems_block.mode is EmsMode.GRID_DISCHARGE, "noisy mode must normalize")
    check(
        noisy.ems_block.maximum_discharge_power_percent_4306 == 21.2,
        "float32 noise at 4306 must normalize to the physical tenth-percent step",
    )
    check(
        noisy.ems_block.maximum_charge_power_percent_4304 == 99.7
        and noisy.export_limit_percent_259 == 199.9
        and noisy.battery_max_charge_power_percent_306 == 80.0,
        "all float32-backed tenth-step registers must share normalization",
    )
    try:
        settings_from_execution_source(source(physical_mode_code=5.00000001))
    except (ActiveBridgeError, ValueError):
        pass
    else:
        raise AssertionError("integer-only physical mode must remain exact")
    for field, value in (
        ("self_use_soc_percent", 20.01),
        ("maximum_charge_power_percent", 50.01),
        ("maximum_discharge_power_percent", 21.21),
    ):
        try:
            settings_from_execution_source(source(**{field: value}))
        except (ActiveBridgeError, ValueError):
            pass
        else:
            raise AssertionError(f"off-step {field} must fail closed")
    invalid_direct = settings_from_execution_source(
        source(
            effective_export_limit_percent=99.99,
            battery_charge_limit_percent=79.99,
        )
    )
    check(
        invalid_direct.export_limit_percent_259 is None
        and invalid_direct.gcf_generation is None
        and invalid_direct.battery_max_charge_power_percent_306 is None
        and invalid_direct.battery_generation is None,
        "off-step independent direct cohorts must remain explicitly unavailable",
    )


def test_action_local_readback_freshness() -> None:
    stale = NOW - timedelta(seconds=60)
    tariff_ems = execution_gates(
        source(
            gcf_generation_at=stale,
            battery_charge_limit_generation_at=stale,
            direct_259_execution_ready=False,
            direct_306_execution_ready=False,
        ),
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
    )
    check(
        tariff_ems.inputs_fresh and tariff_ems.full_block_ready,
        "stale/unavailable unused 259 and 306 must not block tariff EMS block",
    )
    check(
        not tariff_ems.direct_259_ready and not tariff_ems.direct_306_ready,
        "stale direct families must remain individually blocked",
    )

    direct_306 = execution_gates(
        source(
            gcf_generation_at=stale,
            direct_259_execution_ready=False,
        ),
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
    )
    check(
        direct_306.inputs_fresh and direct_306.direct_306_ready,
        "stale/unavailable 259 must not block a fresh direct-306 path",
    )
    check(
        not direct_306.direct_259_ready,
        "stale 259 must fail closed when 259 is touched",
    )

    direct_259 = execution_gates(
        source(
            battery_charge_limit_generation_at=stale,
            direct_306_execution_ready=False,
        ),
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
    )
    check(
        direct_259.inputs_fresh and direct_259.direct_259_ready,
        "stale/unavailable 306 must not block a fresh direct-259 path",
    )
    check(
        not direct_259.direct_306_ready,
        "stale 306 must fail closed when 306 is touched",
    )

    stale_ems = execution_gates(
        source(full_block_generation_at=stale),
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
    )
    check(
        not stale_ems.inputs_fresh and not stale_ems.full_block_ready,
        "stale EMS/off-grid readback must remain a global fail-closed gate",
    )


def test_missing_action_local_readback_cohorts() -> None:
    missing_direct = source(
        gcf_enable_code=None,
        effective_export_limit_percent=None,
        gcf_generation=None,
        gcf_generation_at=None,
        gcf_cohort_coherent=None,
        battery_charge_limit_generation=None,
        battery_charge_limit_generation_at=None,
        battery_charge_limit_percent=None,
    )
    settings = settings_from_execution_source(missing_direct)
    check(
        settings.gcf_enabled_258 is None
        and settings.export_limit_percent_259 is None
        and settings.gcf_generation is None,
        "missing GCF must remain explicit rather than receiving invented values",
    )
    check(
        settings.battery_max_charge_power_percent_306 is None
        and settings.battery_generation is None,
        "missing 306 must remain explicit rather than receiving invented values",
    )

    tariff_candidate = candidate(
        PolicyId.TARIFF,
        requested_action=RequestedAction.TARIFF_BATTERY_CHARGE,
        target_soc_percent=85,
    )
    tariff_decision, tariff_candidates = decide(tariff_candidate)
    tariff_intent = build_actuator_intent(
        tariff_decision,
        tariff_candidates,
        settings,
        rce=RceSourceSnapshot(),
        tariff=TariffSourceSnapshot(command_charge_power_percent=50),
        rcm=RcmSourceSnapshot(),
        now=NOW,
    )
    check(tariff_intent is not None, "missing unused cohorts blocked tariff intent")
    tariff_gates = execution_gates(
        missing_direct,
        owner_kind=OwnerKind.NONE,
        owner_conflict=False,
        now=NOW,
        candidate=tariff_candidate,
    )
    check(
        tariff_gates.inputs_fresh and tariff_gates.full_block_ready,
        "missing unused direct cohorts must not close the complete EMS path",
    )
    tariff_executor = SupervisorExecutor()
    tariff_executor.select_intent(
        tariff_intent,
        transaction_id="tariff:missing-unused",
        now=NOW,
    )
    tariff_executor.start(settings, tariff_gates, now=NOW)
    check(
        tariff_executor.record.state is ActiveState.STARTING,
        "tariff EMS-only charge must acquire ownership with unused cohorts unavailable",
    )
    envelope = tariff_executor.prepare_command(settings, tariff_gates, now=NOW)
    check(envelope is not None, "tariff EMS-only command must survive final prewrite")
    check(
        len(envelope.atomic_writes) == 1
        and envelope.atomic_writes[0].ems_block is not None,
        "tariff must dispatch exactly the complete EMS block",
    )

    rcm_candidate = candidate(PolicyId.RCM)
    rcm_decision, rcm_candidates = decide(rcm_candidate)
    rcm_intent = build_actuator_intent(
        rcm_decision,
        rcm_candidates,
        settings,
        rce=RceSourceSnapshot(),
        tariff=TariffSourceSnapshot(),
        rcm=RcmSourceSnapshot(recommended_charge_limit_percent=60),
        now=NOW,
    )
    check(rcm_intent is not None, "RCM fixture must produce a direct-306 intent")
    rcm_executor = SupervisorExecutor()
    rcm_executor.select_intent(
        rcm_intent,
        transaction_id="rcm:missing-required",
        now=NOW,
    )
    rcm_executor.start(
        settings,
        execution_gates(
            missing_direct,
            owner_kind=OwnerKind.NONE,
            owner_conflict=False,
            now=NOW,
            candidate=rcm_candidate,
        ),
        now=NOW,
    )
    check(
        rcm_executor.record.state is ActiveState.BLOCKED
        and rcm_executor.record.reason is ExecutionReason.STALE_INPUTS,
        "missing required 306 cohort must fail closed",
    )

    rce_candidate = candidate(
        PolicyId.RCE,
        protected_soc_floor_percent=30,
    )
    rce_decision, rce_candidates = decide(rce_candidate)
    rce_intent = build_actuator_intent(
        rce_decision,
        rce_candidates,
        settings,
        rce=RceSourceSnapshot(effective_discharge_power_percent=40),
        tariff=TariffSourceSnapshot(),
        rcm=RcmSourceSnapshot(),
        now=NOW,
    )
    check(rce_intent is not None, "RCE fixture must produce an export intent")
    rce_executor = SupervisorExecutor()
    rce_executor.select_intent(
        rce_intent,
        transaction_id="rce:missing-gcf",
        now=NOW,
    )
    rce_executor.start(
        settings,
        execution_gates(
            missing_direct,
            owner_kind=OwnerKind.NONE,
            owner_conflict=False,
            now=NOW,
            candidate=rce_candidate,
        ),
        now=NOW,
    )
    check(
        rce_executor.record.state is ActiveState.BLOCKED
        and rce_executor.record.reason is ExecutionReason.STALE_INPUTS,
        "missing GCF authority must fail closed for export",
    )


def test_all_action_mappings() -> None:
    rce, _, _ = build(
        candidate(
            PolicyId.RCE,
            protected_soc_floor_percent=30,
        )
    )
    check(rce.action is ExecutionAction.RCE_EXPORT, "RCE action identity drift")
    check(rce.command.ems_block.mode is EmsMode.GRID_DISCHARGE, "RCE must request Mode 5")
    check(rce.command.ems_block.force_discharge_soc_percent_4305 == 30, "RCE 4305 drift")

    tariff_shapes = (
        (RequestedAction.TARIFF_BATTERY_CHARGE, ExecutionAction.TARIFF_BATTERY_CHARGE),
        (RequestedAction.TARIFF_GRID_SUPPORT, ExecutionAction.TARIFF_GRID_SUPPORT),
        (
            RequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
            ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
        ),
    )
    for requested, expected in tariff_shapes:
        intent, _, _ = build(
            candidate(
                PolicyId.TARIFF,
                requested_action=requested,
                target_soc_percent=85,
            )
        )
        check(intent.action is expected, f"tariff identity drift: {requested.value}")
        check(intent.command.ems_block.mode is EmsMode.GRID_CHARGE, "tariff must request Mode 4")

    absorb, _, _ = build(candidate(PolicyId.RCM))
    check(absorb.action is ExecutionAction.RCM_ABSORB_PV, "RCM absorb identity drift")
    check(absorb.command.battery_max_charge_power_percent_306 == 60, "RCM 306 target drift")

    limit, _, _ = build(
        candidate(
            PolicyId.RCM,
            requested_action=RequestedAction.RCM_LIMIT_EXPORT,
            actuator_scope=ActuatorScope.DIRECT_259,
        )
    )
    check(limit.action is ExecutionAction.RCM_LIMIT_EXPORT, "RCM limit identity drift")
    check(limit.command.export_limit_percent_259 == 0, "RCM 259 target drift")

    positive_limit_candidate = candidate(
        PolicyId.RCM,
        requested_action=RequestedAction.RCM_LIMIT_EXPORT,
        actuator_scope=ActuatorScope.DIRECT_259,
    )
    positive_limit_decision, positive_limit_candidates = decide(
        positive_limit_candidate
    )
    try:
        build_actuator_intent(
            positive_limit_decision,
            positive_limit_candidates,
            settings_from_execution_source(source()),
            rce=RceSourceSnapshot(),
            tariff=TariffSourceSnapshot(),
            rcm=RcmSourceSnapshot(recommended_export_limit_percent=20),
            now=NOW,
        )
    except ActiveBridgeError as err:
        check(
            "rated-power evidence" in str(err),
            "positive 259 target failed for the wrong reason",
        )
    else:
        raise AssertionError("positive 259 target executed without rated-power evidence")

    pre, _, _ = build(
        candidate(
            PolicyId.RCM,
            requested_action=RequestedAction.RCM_PRE_DISCHARGE,
            actuator_scope=ActuatorScope.EMS_BLOCK_4300_4306,
            target_soc_percent=40,
        )
    )
    check(pre.action is ExecutionAction.RCM_PRE_DISCHARGE, "RCM pre-discharge identity drift")
    check(pre.command.ems_block.mode is EmsMode.GRID_DISCHARGE, "RCM pre-discharge must request Mode 5")


def test_authorization_and_physical_signs() -> None:
    intent, decision, candidates = build(
        candidate(
            PolicyId.TARIFF,
            requested_action=RequestedAction.TARIFF_BATTERY_CHARGE,
            target_soc_percent=85,
        )
    )
    check(authorization_matches(intent, decision, candidates), "same arbitration must continue")
    check(
        intent.candidate_revision
        == f"tariff:tariff_battery_charge:{HASH}",
        "persisted identity must contain policy, action and the full fingerprint",
    )
    continued_candidate = replace(
        next(item for item in candidates if item.policy_id is PolicyId.TARIFF),
        candidate_revision=987654321,
        start_eligible=False,
        continuation_eligible=True,
        active_latched=True,
    )
    continued_candidates = tuple(
        continued_candidate if item.policy_id is PolicyId.TARIFF else item
        for item in candidates
    )
    continued_decision = replace(
        decision,
        selected_candidate_revision=continued_candidate.candidate_revision,
    )
    check(
        authorization_matches(intent, continued_decision, continued_candidates),
        "start-to-active flag and selection revision churn broke continuation",
    )
    same_prefix_fingerprint = HASH[:24] + "b" * 40
    changed_physical_candidate = replace(
        continued_candidate,
        desired_actuator_fingerprint=same_prefix_fingerprint,
    )
    changed_candidates = tuple(
        changed_physical_candidate if item.policy_id is PolicyId.TARIFF else item
        for item in candidates
    )
    check(
        not authorization_matches(intent, continued_decision, changed_candidates),
        "fingerprint change beyond the old 24-character prefix was accepted",
    )
    check(
        not authorization_matches(intent, decision, tuple(replace(item, continuation_eligible=False) for item in candidates)),
        "lost continuation must fail closed",
    )

    sent = NOW - timedelta(seconds=4)
    physical_at = NOW - timedelta(seconds=1)
    tariff_charge_actions = (
        ExecutionAction.TARIFF_BATTERY_CHARGE,
        ExecutionAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
    )
    for index, action in enumerate(tariff_charge_actions):
        inferred_charge = physical_verification(
            f"tariff:inferred-{index}",
            action,
            source(
                physical_mode_code=4,
                full_block_generation=11,
                full_block_generation_at=physical_at,
                grid_power_w=-2500,
                grid_power_observed_at=physical_at,
                battery_power_w=-1800,
                battery_power_observed_at=physical_at,
                pv_power_w=500,
                pv_power_observed_at=physical_at,
                load_power_w=1200,
                load_power_observed_at=physical_at,
            ),
            command_sent_at=sent,
            now=NOW,
        )
        check(
            inferred_charge.status is VerificationStatus.CONFIRMED,
            f"{action.value}: coherent system flows must confirm without a direct channel",
        )
        check(
            "confirmation_source=single_system_power_balance"
            in inferred_charge.evidence
            and "accounting_authority=separate_direct_channel_required"
            in inferred_charge.evidence
            and not any(
                item.startswith("grid_to_battery_w=")
                for item in inferred_charge.evidence
            ),
            f"{action.value}: inference/accounting boundary is not explicit",
        )

        ignored_direct_channel = physical_verification(
            f"tariff:direct-independent-{index}",
            action,
            source(
                physical_mode_code=4,
                full_block_generation=11,
                full_block_generation_at=physical_at,
                grid_power_w=-2500,
                grid_power_observed_at=physical_at,
                grid_to_battery_power_w=9_999_999,
                grid_to_battery_power_observed_at=NOW - timedelta(minutes=5),
                battery_power_w=-1800,
                battery_power_observed_at=physical_at,
                pv_power_w=500,
                pv_power_observed_at=physical_at,
                load_power_w=1200,
                load_power_observed_at=physical_at,
            ),
            command_sent_at=sent,
            now=NOW,
        )
        check(
            ignored_direct_channel.status is VerificationStatus.CONFIRMED,
            f"{action.value}: accounting-only direct channel blocked execution proof",
        )
        check(
            any(item.startswith("battery_w=") for item in ignored_direct_channel.evidence)
            and not any(
                item.startswith("grid_to_battery_w=")
                for item in ignored_direct_channel.evidence
            ),
            f"{action.value}: execution proof consumed the accounting-only channel",
        )

        pre_command = physical_verification(
            f"tariff:pre-command-{index}",
            action,
            source(
                physical_mode_code=4,
                full_block_generation=11,
                full_block_generation_at=sent,
                grid_power_w=-2500,
                grid_power_observed_at=physical_at,
                battery_power_w=-1800,
                battery_power_observed_at=physical_at,
                pv_power_w=500,
                pv_power_observed_at=physical_at,
                load_power_w=1200,
                load_power_observed_at=physical_at,
            ),
            command_sent_at=sent,
            now=NOW,
        )
        check(
            pre_command.status is VerificationStatus.PENDING
            and "ems_readback_not_newer_than_command" in pre_command.evidence,
            f"{action.value}: pre-command FC03 readback must fail closed",
        )

        stale_sent = NOW - timedelta(seconds=30)
        stale_readback = physical_verification(
            f"tariff:stale-{index}",
            action,
            source(
                physical_mode_code=4,
                full_block_generation=11,
                full_block_generation_at=NOW - timedelta(seconds=20),
                grid_power_w=-2500,
                grid_power_observed_at=physical_at,
                battery_power_w=-1800,
                battery_power_observed_at=physical_at,
                pv_power_w=500,
                pv_power_observed_at=physical_at,
                load_power_w=1200,
                load_power_observed_at=physical_at,
            ),
            command_sent_at=stale_sent,
            now=NOW,
        )
        check(
            stale_readback.status is VerificationStatus.UNAVAILABLE
            and "ems_readback_stale_or_future" in stale_readback.evidence,
            f"{action.value}: stale FC03 evidence must fail closed",
        )

        incoherent_balance = physical_verification(
            f"tariff:incoherent-{index}",
            action,
            source(
                physical_mode_code=4,
                full_block_generation=11,
                full_block_generation_at=physical_at,
                grid_power_w=-2500,
                grid_power_observed_at=physical_at,
                battery_power_w=-1800,
                battery_power_observed_at=physical_at,
                pv_power_w=4000,
                pv_power_observed_at=physical_at,
                load_power_w=1000,
                load_power_observed_at=physical_at,
            ),
            command_sent_at=sent,
            now=NOW,
        )
        check(
            incoherent_balance.status is VerificationStatus.CONTRADICTED
            and any(
                item.startswith("power_balance_residual_w=")
                for item in incoherent_balance.evidence
            ),
            f"{action.value}: incoherent physical balance was accepted",
        )

        wrong_charge = physical_verification(
            f"tariff:export-{index}",
            action,
            source(
                physical_mode_code=4,
                full_block_generation=11,
                full_block_generation_at=physical_at,
                grid_power_w=1200,
                grid_power_observed_at=physical_at,
            ),
            command_sent_at=sent,
            now=NOW,
        )
        check(
            wrong_charge.status is VerificationStatus.CONTRADICTED,
            f"{action.value}: physical export must contradict charge",
        )

        parallel_master = physical_verification(
            f"tariff:parallel-master-{index}",
            action,
            source(
                physical_mode_code=4,
                full_block_generation=11,
                full_block_generation_at=physical_at,
                machine_type_code=1,
                inverter_count=3,
                grid_power_w=-2500,
                grid_power_observed_at=physical_at,
                battery_power_w=-1800,
                battery_power_observed_at=physical_at,
                pv_power_w=500,
                pv_power_observed_at=physical_at,
                load_power_w=1200,
                load_power_observed_at=physical_at,
            ),
            command_sent_at=sent,
            now=NOW,
        )
        check(
            parallel_master.status is VerificationStatus.CONFIRMED
            and "confirmation_source=parallel_master_aggregate_balance"
            in parallel_master.evidence,
            f"{action.value}: verified Master aggregate did not confirm",
        )
        slave = physical_verification(
            f"tariff:slave-{index}",
            action,
            source(
                physical_mode_code=4,
                full_block_generation=11,
                full_block_generation_at=physical_at,
                machine_type_code=2,
                inverter_count=1,
                grid_power_w=-2500,
                grid_power_observed_at=physical_at,
                battery_power_w=-1800,
                battery_power_observed_at=physical_at,
                pv_power_w=500,
                pv_power_observed_at=physical_at,
                load_power_w=1200,
                load_power_observed_at=physical_at,
            ),
            command_sent_at=sent,
            now=NOW,
        )
        check(
            slave.status is VerificationStatus.UNAVAILABLE
            and "parallel_slave_execution_blocked" in slave.evidence,
            f"{action.value}: Slave was allowed to confirm tariff execution",
        )

    grid_support = physical_verification(
        "tariff:grid-support",
        ExecutionAction.TARIFF_GRID_SUPPORT,
        source(
            physical_mode_code=4,
            full_block_generation_at=physical_at,
            pv_power_w=0,
            pv_power_observed_at=physical_at,
            load_power_w=1200,
            load_power_observed_at=physical_at,
            grid_power_w=-1200,
            grid_power_observed_at=physical_at,
            battery_power_w=0,
            battery_power_observed_at=physical_at,
        ),
        command_sent_at=sent,
        now=NOW,
    )
    check(
        grid_support.status is VerificationStatus.CONFIRMED,
        "tariff grid support requires Mode 4, grid-supplied LOAD and idle battery",
    )
    export_to_grid = physical_verification(
        "rce:1234567890",
        ExecutionAction.RCE_EXPORT,
        source(
            grid_power_w=1200,
            grid_power_observed_at=physical_at,
            battery_power_w=900,
            battery_power_observed_at=physical_at,
        ),
        command_sent_at=sent,
        now=NOW,
    )
    check(
        export_to_grid.status is VerificationStatus.CONFIRMED,
        "positive RCE battery discharge must confirm while exporting to grid",
    )
    export_to_house = physical_verification(
        "rce:1234567891",
        ExecutionAction.RCE_EXPORT,
        source(
            grid_power_w=-1200,
            grid_power_observed_at=physical_at,
            battery_power_w=900,
            battery_power_observed_at=physical_at,
        ),
        command_sent_at=sent,
        now=NOW,
    )
    check(
        export_to_house.status is VerificationStatus.CONFIRMED,
        "RCE battery discharge below house load must confirm regardless of GRID sign",
    )
    rce_charging = physical_verification(
        "rce:1234567892",
        ExecutionAction.RCE_EXPORT,
        source(
            grid_power_w=1200,
            grid_power_observed_at=physical_at,
            battery_power_w=-900,
            battery_power_observed_at=physical_at,
        ),
        command_sent_at=sent,
        now=NOW,
    )
    check(
        rce_charging.status is VerificationStatus.CONTRADICTED,
        "physical battery charging must contradict RCE discharge",
    )
    rce_charging_settling = physical_verification(
        "rce:1234567892-settling",
        ExecutionAction.RCE_EXPORT,
        source(
            grid_power_w=1200,
            grid_power_observed_at=physical_at,
            battery_power_w=-900,
            battery_power_observed_at=physical_at,
        ),
        command_sent_at=sent,
        now=NOW,
        allow_rce_settling=True,
    )
    check(
        rce_charging_settling.status is VerificationStatus.PENDING
        and "rce_phase=waiting_for_discharge_effect"
        in rce_charging_settling.evidence,
        "initial RCE flow must stay pending while the confirmed Mode 5 effect settles",
    )
    stale_rce_observed = sent + timedelta(seconds=1)
    stale_rce_now = sent + timedelta(seconds=20)
    stale_rce_source = source(
        battery_power_w=-900,
        battery_power_observed_at=stale_rce_observed,
    )
    stale_initial_rce = physical_verification(
        "rce:1234567892-stale-settling",
        ExecutionAction.RCE_EXPORT,
        stale_rce_source,
        command_sent_at=sent,
        now=stale_rce_now,
        allow_rce_settling=True,
    )
    strict_stale_rce = physical_verification(
        "rce:1234567892-stale-strict",
        ExecutionAction.RCE_EXPORT,
        stale_rce_source,
        command_sent_at=sent,
        now=stale_rce_now,
    )
    check(
        stale_initial_rce.status is VerificationStatus.PENDING
        and stale_initial_rce.observed_at == stale_rce_observed
        and strict_stale_rce.status is VerificationStatus.UNAVAILABLE,
        "only initial RCE settling may retain an old post-command BAT sample as pending",
    )
    confirmed_drop_source = replace(stale_rce_source, battery_power_w=900)
    for age_seconds, expected in (
        (15.0, VerificationStatus.CONFIRMED),
        (16.0, VerificationStatus.CONFIRMED),
        (24.9, VerificationStatus.CONFIRMED),
        (25.0, VerificationStatus.CONFIRMED),
        (25.1, VerificationStatus.UNAVAILABLE),
        (30.0, VerificationStatus.UNAVAILABLE),
    ):
        continued = physical_verification(
            f"rce:confirmed-drop-{age_seconds}",
            ExecutionAction.RCE_EXPORT,
            confirmed_drop_source,
            command_sent_at=sent,
            now=stale_rce_observed + timedelta(seconds=age_seconds),
            rce_confirmed_continuation=True,
        )
        check(
            continued.status is expected,
            f"confirmed RCE Wi-Fi-drop boundary failed at {age_seconds:.1f}s",
        )
    strict_at_16 = physical_verification(
        "rce:strict-drop-16",
        ExecutionAction.RCE_EXPORT,
        confirmed_drop_source,
        command_sent_at=sent,
        now=stale_rce_observed + timedelta(seconds=16),
    )
    check(
        strict_at_16.status is VerificationStatus.UNAVAILABLE,
        "unconfirmed RCE start inherited the 25-second continuation window",
    )
    for invalid_battery in (None, float("nan"), float("inf"), True, 10_000_001):
        invalid_stale = physical_verification(
            "rce:invalid-stale-bat",
            ExecutionAction.RCE_EXPORT,
            replace(stale_rce_source, battery_power_w=invalid_battery),
            command_sent_at=sent,
            now=stale_rce_now,
            allow_rce_settling=True,
        )
        check(
            invalid_stale.status is VerificationStatus.UNAVAILABLE,
            f"invalid stale BAT {invalid_battery!r} acquired an initial pending lease",
        )
    future_initial = physical_verification(
        "rce:future-bat",
        ExecutionAction.RCE_EXPORT,
        replace(stale_rce_source, battery_power_observed_at=stale_rce_now + timedelta(seconds=6)),
        command_sent_at=sent,
        now=stale_rce_now,
        allow_rce_settling=True,
    )
    fresh_initial = physical_verification(
        "rce:fresh-initial-bat",
        ExecutionAction.RCE_EXPORT,
        replace(stale_rce_source, battery_power_w=900, battery_power_observed_at=stale_rce_now),
        command_sent_at=sent,
        now=stale_rce_now,
        allow_rce_settling=True,
    )
    check(
        future_initial.status is VerificationStatus.UNAVAILABLE
        and fresh_initial.status is VerificationStatus.CONFIRMED,
        "initial RCE must reject future BAT and accept a fresh positive effect",
    )
    rce_without_grid_timestamp = physical_verification(
        "rce:1234567893",
        ExecutionAction.RCE_EXPORT,
        source(
            grid_power_w=-1200,
            grid_power_observed_at=None,
            battery_power_w=900,
            battery_power_observed_at=physical_at,
        ),
        command_sent_at=sent,
        now=NOW,
    )
    check(
        rce_without_grid_timestamp.status is VerificationStatus.CONFIRMED,
        "RCE battery discharge must remain provable when GRID telemetry is absent",
    )
    rce_with_split_grid_cohort = physical_verification(
        "rce:1234567894",
        ExecutionAction.RCE_EXPORT,
        source(
            grid_power_w=0,
            grid_power_observed_at=NOW - timedelta(seconds=14),
            battery_power_w=900,
            battery_power_observed_at=physical_at,
        ),
        command_sent_at=sent,
        now=NOW,
    )
    check(
        rce_with_split_grid_cohort.status is VerificationStatus.CONFIRMED,
        "an asynchronous GRID refresh must not revoke proven RCE battery discharge",
    )
    absorb = physical_verification(
        "rcm:1234567890",
        ExecutionAction.RCM_ABSORB_PV,
        source(
            pv_power_w=4000,
            pv_power_observed_at=physical_at,
            load_power_w=1000,
            load_power_observed_at=physical_at,
            battery_power_w=-2000,
            battery_power_observed_at=physical_at,
        ),
        command_sent_at=sent,
        now=NOW,
    )
    check(absorb.status is VerificationStatus.CONFIRMED, "PV surplus plus negative BAT must confirm absorption")
    zero_export = physical_verification(
        "rcm:1234567891",
        ExecutionAction.RCM_LIMIT_EXPORT,
        source(grid_power_w=30, grid_power_observed_at=physical_at),
        command_sent_at=sent,
        now=NOW,
        export_limit_target_percent=0,
    )
    check(zero_export.status is VerificationStatus.CONFIRMED, "physical zero export must confirm a 0% limit")
    positive_limit = physical_verification(
        "rcm:1234567892",
        ExecutionAction.RCM_LIMIT_EXPORT,
        source(grid_power_w=30, grid_power_observed_at=physical_at),
        command_sent_at=sent,
        now=NOW,
        export_limit_target_percent=20,
    )
    check(positive_limit.status is VerificationStatus.UNAVAILABLE, "positive percent needs rated-power evidence")


def test_same_window_rce_retarget_is_narrow_and_run_bounded() -> None:
    deadline = NOW + timedelta(minutes=30)
    slot_end = NOW + timedelta(minutes=5)
    old_candidate = candidate(
        PolicyId.RCE,
        protected_soc_floor_percent=41.0,
        requested_power_kw=8.0,
        requested_energy_kwh=4.0,
        valid_until=deadline,
    )
    old_decision, old_candidates = decide(old_candidate)
    base_settings = settings_from_execution_source(source())
    old_intent = build_actuator_intent(
        old_decision,
        old_candidates,
        base_settings,
        rce=RceSourceSnapshot(effective_discharge_power_percent=25.0),
        tariff=TariffSourceSnapshot(),
        rcm=RcmSourceSnapshot(),
        now=NOW,
    )
    check(old_intent is not None, "old RCE target fixture did not build")
    physical_old_source = source(
        physical_mode_code=5,
        force_discharge_soc_percent=41.0,
        maximum_discharge_power_percent=25.0,
    )
    physical_old = settings_from_execution_source(physical_old_source)

    def latest(floor: float, power: float) -> tuple[PolicyCandidate, RceSourceSnapshot]:
        new_candidate = candidate(
            PolicyId.RCE,
            active_latched=True,
            start_eligible=False,
            continuation_eligible=False,
            local_hard_stop=True,
            protected_soc_floor_percent=floor,
            requested_power_kw=10.0,
            requested_energy_kwh=5.0,
            valid_until=deadline,
            desired_actuator_fingerprint=(
                "b" if floor == 30.0 and power == 25.0
                else "c" if floor == 41.0
                else "d"
            )
            * 64,
        )
        snapshot = RceSourceSnapshot(
            observed_at=NOW - timedelta(seconds=1),
            allowed_by_user=True,
            enabled=True,
            active_latched=True,
            status_code=RcePlanStatus.READY,
            result_current=True,
            recalculation_pending=False,
            input_revision=2,
            current_slot_planned=True,
            current_slot_start_eligible=True,
            current_slot_continue_eligible=True,
            current_slot_end=slot_end,
            current_run_end=deadline,
            requested_discharge_power_kw=10.0,
            planned_export_energy_kwh=5.0,
            protected_soc_floor_percent=floor,
            effective_discharge_power_percent=power,
            system_power_kw=10.0,
            current_soc_percent=60.0,
            control_data_ready=True,
            price_above_threshold=True,
            reserve_ready=True,
            sale_block_active=False,
            latched_slot_end=deadline,
            latched_minimum_soc_percent=floor,
            active_4305_readback_percent=41.0,
            active_4306_readback_percent=25.0,
        )
        return new_candidate, snapshot

    assert old_intent is not None
    for floor, power in ((30.0, 25.0), (41.0, 50.0), (30.0, 50.0)):
        new_candidate, rce = latest(floor, power)
        decision, candidates = decide(new_candidate)
        decision = replace(
            decision,
            execution_blocked_reason=None,
            selection_reason=ReasonCode.LOCAL_HARD_STOP,
        )
        proposal = build_same_window_rce_retarget(
            old_intent,
            decision,
            candidates,
            physical_old,
            rce=rce,
            now=NOW,
        )
        check(
            proposal is not None,
            f"same-run RCE retarget {floor}/{power} was rejected",
        )
        assert proposal is not None and proposal.intent.command.ems_block is not None
        check(
            proposal.intent.deadline == deadline
            and proposal.intent.command.ems_block.force_discharge_soc_percent_4305 == floor
            and proposal.intent.command.ems_block.maximum_discharge_power_percent_4306 == power,
            "retarget changed the run lease or did not carry the exact latest target",
        )

    new_candidate, rce = latest(30.0, 50.0)
    decision, candidates = decide(new_candidate)
    decision = replace(
        decision,
        execution_blocked_reason=None,
        selection_reason=ReasonCode.LOCAL_HARD_STOP,
    )
    proposal = build_same_window_rce_retarget(
        old_intent,
        decision,
        candidates,
        physical_old,
        rce=rce,
        now=NOW,
    )
    assert proposal is not None
    check(
        same_window_rce_target_authorized(
            proposal.intent,
            decision,
            candidates,
            physical_old,
            rce=rce,
            now=NOW,
        )
        is new_candidate
        and same_window_rce_retarget_wait_authorized(
            proposal.intent,
            decision,
            candidates,
            physical_old,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=rce,
            now=NOW,
        ),
        "prepared/sent retarget rejected both exact same-run RCE latches",
    )
    candidate_without_latch = replace(new_candidate, active_latched=False)
    candidates_without_latch = tuple(
        candidate_without_latch if item.policy_id is PolicyId.RCE else item
        for item in candidates
    )
    check(
        same_window_rce_target_authorized(
            proposal.intent,
            decision,
            candidates_without_latch,
            physical_old,
            rce=rce,
            now=NOW,
        )
        is None
        and not same_window_rce_retarget_wait_authorized(
            proposal.intent,
            decision,
            candidates_without_latch,
            physical_old,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=rce,
            now=NOW,
        ),
        "candidate-latch loss retained retarget write/ACK authority",
    )
    rce_without_latch = replace(rce, active_latched=False)
    check(
        same_window_rce_target_authorized(
            proposal.intent,
            decision,
            candidates,
            physical_old,
            rce=rce_without_latch,
            now=NOW,
        )
        is None
        and not same_window_rce_retarget_wait_authorized(
            proposal.intent,
            decision,
            candidates,
            physical_old,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=rce_without_latch,
            now=NOW,
        ),
        "source-latch loss retained retarget write/ACK authority",
    )
    check(
        same_window_rce_target_authorized(
            proposal.intent,
            decision,
            candidates,
            physical_old,
            rce=replace(rce, latched_slot_end=slot_end),
            now=NOW,
        )
        is None,
        "slot end was accepted as the continuous-run retarget deadline",
    )
    settling = replace(rce, control_data_ready=False)
    for unrelated_block in (ReasonCode.OFF_GRID, ReasonCode.OWNER_CONFLICT, ReasonCode.CRITICAL_BMS_UNAVAILABLE, ReasonCode.EXPORT_UNVERIFIED):
        check(
            build_same_window_rce_retarget(
                old_intent,
                replace(decision, execution_blocked_reason=unrelated_block),
                candidates,
                physical_old,
                rce=rce,
                now=NOW,
            ) is None,
            f"same-window target bypassed unrelated block {unrelated_block.value}",
        )
    check(
        build_same_window_rce_retarget(
            old_intent,
            decision,
            candidates,
            physical_old,
            rce=settling,
            now=NOW,
        )
        is None
        and same_window_rce_execution_hold_authorized(
            old_intent,
            decision,
            candidates,
            physical_old,
            execution_source=physical_old_source,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=settling,
            now=NOW,
        ),
        "fresh split-event RCE cohort neither held nor withheld write authority",
    )
    check(
        not same_window_rce_execution_hold_authorized(
            old_intent,
            decision,
            candidates,
            physical_old,
            execution_source=physical_old_source,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=replace(settling, observed_at=NOW - timedelta(seconds=61)),
            now=NOW,
        )
        and not same_window_rce_execution_hold_authorized(
            old_intent,
            decision,
            candidates,
            physical_old,
            execution_source=physical_old_source,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=replace(settling, sale_block_active=True),
            now=NOW,
        ),
        "RCE cohort hold outlived its bound or bypassed sale block",
    )
    high_reserve_candidate, high_reserve_rce = latest(70.0, 50.0)
    high_reserve_decision, high_reserve_candidates = decide(high_reserve_candidate)
    high_reserve_decision = replace(
        high_reserve_decision,
        execution_blocked_reason=None,
        selection_reason=ReasonCode.LOCAL_HARD_STOP,
    )
    check(
        not same_window_rce_execution_hold_authorized(
            old_intent,
            high_reserve_decision,
            high_reserve_candidates,
            physical_old,
            execution_source=physical_old_source,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=replace(
                high_reserve_rce,
                control_data_ready=False,
                reserve_ready=False,
            ),
            now=NOW,
        ),
        "RCE cohort hold survived a reserve target above current SOC",
    )

    recalculating_candidate = replace(
        old_candidate,
        active_latched=True,
        start_eligible=False,
        continuation_eligible=False,
        local_hard_stop=True,
        result_current=False,
        recalculation_pending=True,
    )
    recalculating_decision, recalculating_candidates = decide(
        recalculating_candidate
    )
    recalculating_decision = replace(
        recalculating_decision,
        execution_blocked_reason=ReasonCode.LOCAL_HARD_STOP,
        selection_reason=ReasonCode.LOCAL_HARD_STOP,
    )
    recalculating_rce = replace(
        rce,
        observed_at=NOW - timedelta(seconds=60),
        result_current=False,
        recalculation_pending=True,
        control_data_ready=False,
        reserve_ready=False,
        protected_soc_floor_percent=41.0,
        effective_discharge_power_percent=25.0,
        latched_minimum_soc_percent=41.0,
    )
    check(
        same_window_rce_execution_hold_seconds(
            old_intent,
            recalculating_decision,
            recalculating_candidates,
            physical_old,
            execution_source=physical_old_source,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=recalculating_rce,
            now=NOW,
        )
        == RCE_RECALCULATION_HOLD_SECONDS
        and RCE_RECALCULATION_HOLD_SECONDS == 180.0,
        "real recalculation cohort with both readiness helpers off got no fixed hold",
    )
    for control_ready, reserve_ready in ((False, True), (True, False), (True, True), (None, False), (False, None)):
        hold = same_window_rce_execution_hold_seconds(
            old_intent,
            recalculating_decision,
            recalculating_candidates,
            physical_old,
            execution_source=physical_old_source,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=replace(recalculating_rce, control_data_ready=control_ready, reserve_ready=reserve_ready),
            now=NOW,
        )
        check(
            hold == (None if None in (control_ready, reserve_ready) else RCE_RECALCULATION_HOLD_SECONDS),
            f"pending helper publication order {control_ready}/{reserve_ready} lost its bounded hold contract",
        )
    check(
        same_window_rce_execution_hold_seconds(
            old_intent,
            decision,
            candidates,
            physical_old,
            execution_source=physical_old_source,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=settling,
            now=NOW,
        )
        == RCE_RETARGET_COHORT_HOLD_SECONDS
        and RCE_RETARGET_COHORT_HOLD_SECONDS == 60.0,
        "post-commit split cohort inherited the recalculation hold",
    )
    check(
        same_window_rce_execution_hold_seconds(
            old_intent,
            recalculating_decision,
            recalculating_candidates,
            physical_old,
            execution_source=physical_old_source,
            export_state=ExportState.CONFIRMED_ZERO_EXPORT,
            rce=recalculating_rce,
            now=NOW,
        )
        is None,
        "recalculation hold bypassed current verified export authority",
    )
    check(
        rce_sent_command_within_live_bms_limit(
            old_intent,
            physical_old_source,
            rce=settling,
            now=NOW,
        )
        and not rce_sent_command_within_live_bms_limit(
            old_intent,
            replace(physical_old_source, bms_max_discharge_current_a=10.0),
            rce=settling,
            now=NOW,
        )
        and not rce_sent_command_within_live_bms_limit(
            old_intent,
            physical_old_source,
            rce=replace(settling, system_power_kw=None),
            now=NOW,
        ),
        "sent RCE command was not bounded by current positive BMS power evidence",
    )

    pending_candidate = no_action(PolicyId.RCE)
    pending_candidate = replace(
        pending_candidate,
        result_current=False,
        recalculation_pending=True,
        allowed_by_user=True,
        enabled=True,
    )
    pending_decision, pending_candidates = decide(pending_candidate)
    pending_decision = replace(
        pending_decision,
        execution_blocked_reason=ReasonCode.RECALCULATION_PENDING_NEW_START,
        selection_reason=ReasonCode.RECALCULATION_PENDING_NEW_START,
    )
    pending_rce = replace(
        rce,
        active_latched=False,
        result_current=False,
        recalculation_pending=True,
        control_data_ready=None,
        reserve_ready=None,
    )
    check(
        same_window_rce_wait_authorized(
            old_intent,
            pending_decision,
            pending_candidates,
            physical_old,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=pending_rce,
            now=NOW,
        )
        and not same_window_rce_wait_authorized(
            old_intent,
            pending_decision,
            pending_candidates,
            physical_old,
            export_state=ExportState.VERIFIED_ALLOWED,
            rce=replace(pending_rce, current_run_end=deadline + timedelta(minutes=1)),
            now=NOW,
        ),
        "WAITING RCE replan hold did not enforce the exact continuous-run deadline",
    )
    check(
        not same_window_rce_wait_authorized(
            old_intent,
            pending_decision,
            pending_candidates,
            physical_old,
            export_state=ExportState.UNVERIFIED,
            rce=pending_rce,
            now=NOW,
        ),
        "WAITING RCE replan hold bypassed current export authority",
    )


def test_correlated_owner_diagnostics_preserve_real_foreign_changes() -> None:
    sent = NOW - timedelta(seconds=2)
    physical_at = NOW - timedelta(seconds=1)
    tariff_source = source(
        physical_mode_code=4,
        full_block_generation_at=physical_at,
        pv_power_w=0,
        pv_power_observed_at=physical_at,
        load_power_w=1200,
        load_power_observed_at=physical_at,
        grid_power_w=-1200,
        grid_power_observed_at=physical_at,
        battery_power_w=0,
        battery_power_observed_at=physical_at,
    )
    tariff_block = settings_from_execution_source(tariff_source).ems_block
    check(
        correlated_observed_owner(
            OwnerKind.FOREIGN,
            owner_conflict=False,
            transaction_id="tariff:correlated",
            transaction_owner=ExecutionOwner.TARIFF,
            action=ExecutionAction.TARIFF_GRID_SUPPORT,
            expected_block=tariff_block,
            command_sent_at=sent,
            source=tariff_source,
            now=NOW,
        ) is OwnerKind.TARIFF,
        "exact own tariff command and physical effect replace false foreign diagnostics",
    )
    changed = replace(tariff_source, force_charge_soc_percent=75.0)
    check(
        correlated_observed_owner(
            OwnerKind.FOREIGN,
            owner_conflict=False,
            transaction_id="tariff:foreign",
            transaction_owner=ExecutionOwner.TARIFF,
            action=ExecutionAction.TARIFF_GRID_SUPPORT,
            expected_block=tariff_block,
            command_sent_at=sent,
            source=changed,
            now=NOW,
        ) is OwnerKind.FOREIGN,
        "a real external tariff block change remains foreign",
    )
    check(
        correlated_observed_owner(
            OwnerKind.FOREIGN,
            owner_conflict=True,
            transaction_id="tariff:conflict",
            transaction_owner=ExecutionOwner.TARIFF,
            action=ExecutionAction.TARIFF_GRID_SUPPORT,
            expected_block=tariff_block,
            command_sent_at=sent,
            source=tariff_source,
            now=NOW,
        ) is OwnerKind.FOREIGN,
        "owner conflict is never hidden by command correlation",
    )
    rce_source = source(
        physical_mode_code=5,
        full_block_generation_at=physical_at,
        grid_power_w=1200,
        grid_power_observed_at=physical_at,
        battery_power_w=900,
        battery_power_observed_at=physical_at,
    )
    rce_block = settings_from_execution_source(rce_source).ems_block
    check(
        correlated_observed_owner(
            OwnerKind.FOREIGN,
            owner_conflict=False,
            transaction_id="rce:correlated",
            transaction_owner=ExecutionOwner.RCE,
            action=ExecutionAction.RCE_EXPORT,
            expected_block=rce_block,
            command_sent_at=sent,
            source=rce_source,
            now=NOW,
        ) is OwnerKind.RCE,
        "exact own RCE command and physical effect replace false foreign diagnostics",
    )
    check(
        correlated_observed_owner(
            OwnerKind.FOREIGN,
            owner_conflict=False,
            transaction_id="rce:foreign",
            transaction_owner=ExecutionOwner.RCE,
            action=ExecutionAction.RCE_EXPORT,
            expected_block=rce_block,
            command_sent_at=sent,
            source=replace(rce_source, maximum_discharge_power_percent=65.0),
            now=NOW,
        ) is OwnerKind.FOREIGN,
        "a real external RCE block change remains foreign",
    )


def main() -> None:
    test_settings_and_gates()
    test_float_transport_noise_is_quantized_but_real_off_step_values_fail()
    test_action_local_readback_freshness()
    test_missing_action_local_readback_cohorts()
    test_all_action_mappings()
    test_authorization_and_physical_signs()
    test_correlated_owner_diagnostics_preserve_real_foreign_changes()
    test_same_window_rce_retarget_is_narrow_and_run_bounded()
    print(f"Supervisor Active bridge contract: PASS ({CHECKS} checks)")


if __name__ == "__main__":
    main()
