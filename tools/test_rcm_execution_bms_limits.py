"""Regression for quantitative RCEm authority during confirmed execution."""

from __future__ import annotations

import ast
import asyncio
from dataclasses import replace
from datetime import timedelta, timezone
import math
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / "custom_components" / "hoymiles_hit_modbus"
sys.path[:0] = [str(ROOT / "tools"), str(COMPONENT)]

import test_rcm_live_control_refresh as live  # noqa: E402
from test_rcm_optimizer import settings  # noqa: E402
from test_supervisor_runtime_contract import rcm_source  # noqa: E402
from test_supervisor_active_controller import (  # noqa: E402
    empty_candidate,
    execution_source,
)
from ems_supervisor import (  # noqa: E402
    OwnerKind,
    PolicyId,
    SupervisorMode,
    SupervisorProfile,
    arbitrate_supervisor,
)
from rcm_optimizer import optimize_rcm  # noqa: E402
from supervisor_active_bridge import (  # noqa: E402
    authorization_matches,
    rcm_charge_command_within_live_bms_limit,
    rcm_pre_discharge_command_within_live_bms_limit,
)
from supervisor_active_controller import ActiveFrame, SupervisorActiveController  # noqa: E402
from supervisor_executor import (  # noqa: E402
    ActiveState,
    ExecutionAction,
    ExecutionOwner,
)
from supervisor_runtime import (  # noqa: E402
    RceSourceSnapshot,
    RcePlanStatus,
    RcmAction,
    TariffAction,
    TariffSourceSnapshot,
    build_execution_context,
    build_rcm_candidate,
)


SENSOR_PATH = COMPONENT / "supervisor_sensor.py"
TREE = ast.parse(SENSOR_PATH.read_text(encoding="utf-8"))
SENSOR = next(
    node
    for node in TREE.body
    if isinstance(node, ast.ClassDef) and node.name == "HoymilesSupervisorSensor"
)
METHOD = next(
    node
    for node in SENSOR.body
    if isinstance(node, ast.FunctionDef)
    and node.name == "_apply_executor_commitment"
)
NAMESPACE = dict(globals())
MODULE = ast.Module(
    body=[
        ast.ImportFrom(
            module="__future__",
            names=[ast.alias(name="annotations")],
            level=0,
        ),
        METHOD,
    ],
    type_ignores=[],
)
exec(compile(ast.fix_missing_locations(MODULE), str(SENSOR_PATH), "exec"), NAMESPACE)
APPLY_COMMITMENT = NAMESPACE["_apply_executor_commitment"]


async def _charge_case(
    initial_ccl_a: float,
    next_ccl_a: float,
    *,
    next_fresh: bool = True,
) -> tuple[ActiveState, list[object], float | None, float | None]:
    now = live.NOW.astimezone(timezone.utc)
    clock = [now]
    writes: list[object] = []

    async def persist(_record: object) -> None:
        return None

    async def dispatch(write: object) -> None:
        writes.append(write)

    controller = SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: clock[0],
    )
    await controller.async_initialize()
    adapter = SimpleNamespace(_controller=controller)

    def build(
        ccl_a: float,
        *,
        generation: int,
        fresh: bool,
        readback_percent: float | None = None,
    ) -> tuple[ActiveFrame, object]:
        at = clock[0]
        source = execution_source(
            at,
            bms_voltage_v=52.0,
            bms_max_charge_current_a=ccl_a,
            bms_charge_current_observed_at=at
            - timedelta(seconds=1 if fresh else 301),
            battery_charge_limit_percent=(
                80.0
                if readback_percent is None and generation == 12
                else 100.0
                if readback_percent is None
                else readback_percent
            ),
            battery_charge_limit_generation=generation,
            pv_power_w=4000.0,
            pv_power_observed_at=at - timedelta(milliseconds=100),
            load_power_w=1000.0,
            load_power_observed_at=at - timedelta(milliseconds=100),
            battery_power_w=-800.0,
            battery_power_observed_at=at - timedelta(milliseconds=100),
            balancing_active=False,
            manual_charge_active=False,
            manual_discharge_active=False,
            rce_active=False,
            tariff_active=False,
            rcm_active=False,
            rcm_export_control_active=False,
            rcm_pre_discharge_active=False,
            charge_timer_active=False,
            discharge_timer_active=False,
        )
        result = optimize_rcm(
            settings(
                now=at,
                voltage_l1_v=253.0,
                voltage_l2_v=240.0,
                voltage_l3_v=240.0,
                battery_voltage_v=52.0,
                bms_max_charge_current_a=ccl_a,
                bms_charge_data_fresh=fresh,
                current_charge_limit_percent=source.battery_charge_limit_percent,
            )
        )
        snapshot = rcm_source(
            observed_at=at,
            action=RcmAction(result.action),
            live_emergency=True,
            emergency_action_ready=True,
            charge_path_locally_valid=result.bms_charge_available,
            recommended_charge_limit_percent=(
                result.recommended_charge_limit_percent
            ),
            recommended_charge_power_kw=result.recommended_charge_power_kw,
            recommended_export_limit_percent=(
                result.recommended_export_limit_percent
            ),
        )
        rce, tariff, projected, context = APPLY_COMMITMENT(
            adapter,
            RceSourceSnapshot(),
            TariffSourceSnapshot(),
            snapshot,
            build_execution_context(source, now=at),
            source,
            now=at,
        )
        candidates = (
            empty_candidate(PolicyId.RCE, at),
            empty_candidate(PolicyId.TARIFF, at),
            build_rcm_candidate(projected, now=at),
        )
        decision = arbitrate_supervisor(
            mode=SupervisorMode.ACTIVE,
            profile=SupervisorProfile.BALANCED,
            context=context,
            candidates=candidates,
            now=at,
        )
        return (
            ActiveFrame(
                now=at,
                decision=decision,
                candidates=candidates,
                context=context,
                rce=rce,
                tariff=tariff,
                rcm=projected,
                execution=source,
            ),
            result,
        )

    initial, initial_plan = build(initial_ccl_a, generation=12, fresh=True)
    await controller.async_reconcile(initial)
    clock[0] += timedelta(seconds=2)
    acknowledged, _ = build(
        initial_ccl_a,
        generation=13,
        fresh=True,
        readback_percent=initial_plan.recommended_charge_limit_percent,
    )
    await controller.async_reconcile(acknowledged)
    assert controller.record.state is ActiveState.EXECUTING, controller.record

    clock[0] += timedelta(seconds=2)
    changed, changed_plan = build(
        next_ccl_a,
        generation=14,
        fresh=next_fresh,
        readback_percent=initial_plan.recommended_charge_limit_percent,
    )
    await controller.async_reconcile(changed)
    return (
        controller.record.state,
        writes,
        initial_plan.recommended_charge_limit_percent,
        changed.rcm.recommended_charge_limit_percent,
    )


async def _pre_discharge_reduction_case(*, telemetry_test=False):
    now = live.NOW.astimezone(timezone.utc)
    clock = [now]
    writes: list[object] = []

    async def persist(_record: object) -> None:
        return None

    async def dispatch(write: object) -> None:
        writes.append(write)

    controller = SupervisorActiveController(
        persist=persist,
        dispatch=dispatch,
        publish=lambda _record: None,
        clock=lambda: clock[0],
    )
    await controller.async_initialize()
    adapter = SimpleNamespace(_controller=controller)
    deadline = now + timedelta(minutes=30)

    def build(dcl_a: float, *, generation: int, power_percent: float) -> ActiveFrame:
        at = clock[0]
        applied = generation > 12
        active = generation > 13
        source = execution_source(
            at,
            physical_mode_code=5 if applied else 0,
            full_block_generation=generation,
            full_block_generation_at=at - timedelta(seconds=1),
            force_discharge_soc_percent=30.0 if applied else 25.0,
            maximum_discharge_power_percent=power_percent if applied else 40.0,
            bms_voltage_v=52.0,
            bms_max_discharge_current_a=dcl_a,
            bms_discharge_current_observed_at=at - timedelta(seconds=1),
            battery_power_w=800.0,
            battery_power_observed_at=at - timedelta(milliseconds=100),
            grid_power_w=300.0,
            grid_power_observed_at=at - timedelta(milliseconds=100),
            pv_power_w=0.0,
            pv_power_observed_at=at - timedelta(milliseconds=100),
            load_power_w=500.0,
            load_power_observed_at=at - timedelta(milliseconds=100),
            balancing_active=False,
            manual_charge_active=False,
            manual_discharge_active=False,
            rce_active=False,
            tariff_active=False,
            rcm_active=False,
            rcm_export_control_active=False,
            rcm_pre_discharge_active=active,
            charge_timer_active=False,
            discharge_timer_active=False,
        )
        snapshot = rcm_source(
            observed_at=at,
            action=RcmAction.GRID_DISCHARGE_PREPARATION,
            live_emergency=False,
            prediction_ready=True,
            pre_discharge_enabled=True,
            pre_discharge_start_eligible=not active,
            pre_discharge_continue_eligible=True,
            pre_discharge_deadline=deadline,
            pre_discharge_target_soc_percent=30.0,
            pre_discharge_power_kw=10.0 * power_percent / 100.0,
            pre_discharge_power_percent=power_percent,
            planned_grid_discharge_kwh=2.0,
            target_soc_before_risk_percent=60.0,
            protected_minimum_soc_percent=20.0,
            sale_block_active=False,
        )
        rce, tariff, projected, context = APPLY_COMMITMENT(
            adapter,
            RceSourceSnapshot(),
            TariffSourceSnapshot(),
            snapshot,
            build_execution_context(source, now=at),
            source,
            now=at,
        )
        candidates = (
            empty_candidate(PolicyId.RCE, at),
            empty_candidate(PolicyId.TARIFF, at),
            build_rcm_candidate(projected, now=at),
        )
        decision = arbitrate_supervisor(
            mode=SupervisorMode.ACTIVE,
            profile=SupervisorProfile.BALANCED,
            context=context,
            candidates=candidates,
            now=at,
        )
        return ActiveFrame(
            now=at,
            decision=decision,
            candidates=candidates,
            context=context,
            rce=rce,
            tariff=tariff,
            rcm=projected,
            execution=source,
        )

    await controller.async_reconcile(build(250.0, generation=12, power_percent=100.0))
    clock[0] += timedelta(seconds=2)
    acknowledged = build(250.0, generation=13, power_percent=100.0)
    assert authorization_matches(
        controller.record.transaction.intent,
        acknowledged.decision,
        acknowledged.candidates,
        allow_selected_start=True,
    ), (acknowledged.decision, acknowledged.candidates, acknowledged.rcm)
    await controller.async_reconcile(acknowledged)
    assert controller.record.state is ActiveState.EXECUTING, controller.record
    if telemetry_test:
        return controller, clock, writes, build
    clock[0] += timedelta(seconds=2)
    reduced = build(20.0, generation=14, power_percent=10.0)
    await controller.async_reconcile(reduced)
    return (
        controller.record.state,
        writes,
        reduced.rcm.latched_pre_discharge_power_percent,
    )


async def main() -> None:
    for mutation in ('none', 'bms', 'withdrawn', 'zero_export'):
        c, clock, writes, build = await _pre_discharge_reduction_case(telemetry_test=True)
        original = build(250.0, generation=14, power_percent=100.0)
        await c.async_reconcile(original)
        end = clock[0] + timedelta(seconds=115)
        c._control_lease_valid_until = lambda: end
        proof = c.record.transaction.physical_verification
        for gap in (40, 90, 110):
            clock[0] = original.now + timedelta(seconds=gap)
            frame = replace(build(250.0, generation=14, power_percent=100.0), execution=original.execution)
            if mutation == 'bms': frame=replace(frame,execution=replace(frame.execution,bms_max_discharge_current_a=20))
            if mutation == 'withdrawn': frame=replace(frame,rcm=replace(frame.rcm,pre_discharge_continue_eligible=False))
            if mutation == 'zero_export': frame=replace(frame,context=replace(frame.context,export_state=type(frame.context.export_state).CONFIRMED_ZERO_EXPORT))
            assert c._confirmed_telemetry_wait(frame,now=clock[0]) == (mutation=='none'), mutation
            await c.async_reconcile(frame)
            if mutation!='none':
                assert c.record.state is not ActiveState.EXECUTING
                break
            assert c.record.transaction.physical_verification == proof
            assert len(writes)==1
        print('PASS RCEm bounded telemetry',mutation)
    state, writes, initial, projected = await _charge_case(200.0, 20.0)
    assert initial == 100.0
    assert projected == 10.0
    assert state is ActiveState.RESTORING
    assert len(writes) == 2

    state, writes, initial, projected = await _charge_case(20.0, 200.0)
    assert initial == 10.0
    assert projected == 10.0
    assert state is ActiveState.EXECUTING
    assert len(writes) == 1

    for ccl, fresh in ((0.0, True), (20.0, False)):
        state, writes, _initial, _projected = await _charge_case(
            200.0,
            ccl,
            next_fresh=fresh,
        )
        assert state is ActiveState.RESTORING
        assert len(writes) == 2

    state, writes, projected = await _pre_discharge_reduction_case()
    assert projected == 10.0
    assert state is ActiveState.RESTORING
    assert len(writes) == 2

    print("RCEm execution BMS limits: PASS")


if __name__ == "__main__":
    asyncio.run(main())
