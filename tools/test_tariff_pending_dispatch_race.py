"""Replay a tariff prewrite/RCEm debounce race through the real HA adapter.

HA storage and transport are local fakes. Source normalization, the pending
callback, runtime, executor, controller and watchdog are production code.
"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
import sys
from types import SimpleNamespace

import test_supervisor_sensor_contract as adapter

# Share the package's enums and records with the existing pure tariff fixture.
for name in (
    "ems_supervisor", "supervisor_runtime", "supervisor_executor",
    "supervisor_active_bridge", "supervisor_executor_codec", "supervisor_active_controller",
):
    sys.modules[name] = sys.modules[f"custom_components.hoymiles_hit_modbus.{name}"]

import test_tariff_active_controller as fixture

NOW = fixture.NOW
CHECKS = 0


class LeaseServices:
    """ESP-shaped Mode 4 lease endpoint with one correlated refusal."""

    def __init__(self, names, *, reject_arm_calls=frozenset({2})):
        self.names = names
        self.calls = []
        self.arm_calls = 0
        self.challenge_calls = 0
        self.reject_hook = None
        self.reject_arm_calls = set(reject_arm_calls)

    def async_services(self):
        return {"esphome": {name: object() for name in self.names}}

    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, dict(data)))
        if not kwargs.get("return_response"):
            return None
        if service.endswith("ems_supervisor_control_lease_challenge"):
            self.challenge_calls += 1
            return {
                "schema_version": 1,
                "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                "nonce": f"{self.challenge_calls:08x}",
            }
        if service.endswith("ems_supervisor_write_complete_block_leased"):
            self.arm_calls += 1
            if self.arm_calls in self.reject_arm_calls:
                if self.arm_calls == 2:
                    assert self.reject_hook is not None
                    self.reject_hook()
                return {
                    "schema_version": 1,
                    "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                    "accepted": False,
                    "reason": "stale_snapshot",
                }
            return {
                "schema_version": 1,
                "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                "accepted": True,
                "reason": "accepted",
                "renew_nonce": f"{self.arm_calls + 32:08x}",
            }
        if service.endswith("ems_supervisor_renew_control_lease"):
            return {
                "schema_version": 1,
                "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                "accepted": True,
                "reason": "renewed",
                "renew_nonce": "00000060",
            }
        return {"schema_version": 1, "accepted": True, "reason": "accepted"}


def check(value, message):
    global CHECKS
    CHECKS += 1
    assert value, message


class Replay:
    def __init__(self):
        self.hass, entry, _runtime, self.sensor = adapter.environment()
        self.sensor._source_entity_ids = {
            spec.key: adapter._source_entity_id(spec, entry.entry_id)
            for spec in adapter.SENSOR.SUPERVISOR_SOURCE_SPECS
        }
        self.now = NOW
        self.writes = []
        self.race = False
        self.keys = frozenset({"rcm_plan"})
        self.during_persist = None
        self.callback_results = []
        for state in self.hass.states.values.values():
            state.last_reported = NOW
            state.last_updated = NOW
        for key in (
            "allow_tariff", "tariff_enabled", "allow_rcm", "tariff_control_data_ready",
            "tariff_planned_charge_slot", "ems_execution_ready", "direct_execution_ready",
        ):
            self.put(key, "on")
        self.put("supervisor_mode", "Active")
        self.put("tariff_maximum_soc", "100")
        self.plan("tariff_plan", control_inputs_fresh=True, forecast_data_fresh=True,
                  system_power_kw=10.0, command_charge_power_percent=30.0)
        self.plan("rcm_plan", action="monitor", live_emergency=False)

        async def persist(_record):
            if (self.race and self.controller.record.state is fixture.ActiveState.RETARGETING
                    and self.controller.record.transaction.command_sent_at is None):
                self.sensor._schedule_planner_callback(self.keys)
                if self.during_persist is not None:
                    self.during_persist(self)

        async def dispatch(write):
            self.writes.append(write)

        self.controller = fixture.SupervisorActiveController(
            persist=persist, dispatch=dispatch, publish=lambda _record: None,
            clock=lambda: self.now, frame_resampler=self.sensor._current_dispatch_frame,
            retarget_replan_pending=self.pending,
        )
        self.sensor._controller = self.controller

    def pending(self):
        result = self.sensor._retarget_replan_pending()
        self.callback_results.append(result)
        return result

    def put(self, key, value, *, at=None, attrs=None):
        entity_id = self.sensor._source_entity_ids[key]
        self.hass.states.values[entity_id] = adapter.FakeState(
            str(value), attrs, self.now if at is None else at,
        )

    def plan(self, key, *, at=None, **changes):
        old = self.hass.states.values[self.sensor._source_entity_ids[key]]
        self.put(key, old.state, at=at, attrs={**old.attributes, **changes})

    def physical(self, *, generation, target=75, mode=4, battery=-1500):
        at = self.now - timedelta(milliseconds=100)
        values = {
            "ems_generation": generation, "ems_mode_readback": mode,
            "self_use_soc_readback": 20, "backup_soc_readback": 80,
            "charge_soc_readback": target, "charge_power_ems_readback": 30,
            "discharge_soc_readback": 25, "discharge_power_readback": 40,
            "battery_soc": 60, "bms_voltage": 54.1, "bms_max_charge_current": 240,
            "bms_max_discharge_current": 240, "machine_type": 0, "inverter_count": 1,
            "topology_generation": generation, "hardware_readback_supported": 1,
            "grid_power": battery - 1200, "battery_power": battery, "pv_power": 0,
            "load_power": 1200,
        }
        for key, value in values.items():
            self.put(key, value, at=at)
        # ``physical`` represents one complete, atomically sampled power cohort.
        # Keep this callback-free race fixture aligned with the production report
        # collector, which is deliberately not registered by ``Replay``.
        self.sensor._power_cohort_states = {
            key: self.hass.states.values[self.sensor._source_entity_ids[key]]
            for key in ("grid_power", "battery_power", "pv_power", "load_power")
        }
        self.sensor._power_cohort_generation += 1

    def frame(self, *, hold=False):
        adapter.CLOCK["now"] = self.now
        _mode, _profile, _rce, _tariff, _rcm, execution = self.sensor._build_snapshots(
            self.sensor._read_source_states(), self.now,
        )
        # Like the field harness, only the authorized tariff plan is projected.
        # The callback must still consume unmodified physical/helper states.
        result = fixture.frame(
            self.now, execution, self.controller.record, target=60 if hold else 75,
            power=30, action=(fixture.TariffAction.GRID_SUPPORT if hold
                             else fixture.TariffAction.GRID_SUPPORT_AND_CHARGE),
        )
        self.sensor._latest_active_frame = result
        return result

    async def start(self):
        await self.controller.async_initialize()
        self.physical(generation=10, mode=0, target=90)
        await self.controller.async_reconcile(self.frame())
        check(len(self.writes) == 1, "one initial full Mode4 command")
        self.now += timedelta(seconds=2)
        self.physical(generation=11)
        await self.controller.async_reconcile(self.frame())
        check(self.controller.record.state is fixture.ActiveState.EXECUTING,
              f"75/30 predecessor physically confirmed: {self.controller.record.reason}")

    async def reject_hold(self, *, elapsed=1, generation=12):
        self.now += timedelta(seconds=elapsed)
        self.physical(generation=generation)
        self.race = True
        await self.controller.async_reconcile(self.frame(hold=True))


async def retained_then_latest():
    replay = Replay()
    await replay.start()
    predecessor = replay.controller.record.transaction
    await replay.reject_hold()
    check(replay.callback_results == [True], "real normalized sensor attests disabled RCEm debounce")
    check(replay.controller.record.state is fixture.ActiveState.EXECUTING,
          "unsent hold preserves the physically confirmed charge command")
    check(len(replay.writes) == 1, "fallback issues neither successor nor restore FC16")
    retained = replay.controller.record.transaction
    for name in ("transaction_id", "intent", "command_snapshot", "expected_readback",
                 "deadline", "command_sent_at"):
        check(getattr(retained, name) == getattr(predecessor, name), f"cancel preserves {name}")
    check(retained.physical_verification.observed_at>
          predecessor.physical_verification.observed_at,
          "cancel records the fresh correlated physical predecessor proof")
    check(replay.controller.tariff_execution_settling_deadline ==
        replay.now + timedelta(seconds=180),
        f"first rejection arms fixed180s tariff budget: "
        f"{replay.controller._tariff_execution_hold!r} / "
        f"{replay.controller.tariff_execution_settling_deadline!r} / {replay.now!r}")
    replay.race = False
    replay.sensor._cancel_planner_callback()
    replay.now += timedelta(seconds=1)
    replay.physical(generation=13)
    latest = replay.frame(hold=True)
    await replay.controller.async_reconcile(latest)
    check(replay.controller.record.state is fixture.ActiveState.RETARGETING,
          "fresh frame can dispatch the latest hold after cancellation")
    check(len(replay.writes) == 2 and replay.writes[-1].ems_block.mode.value == 4
          and replay.writes[-1].ems_block.force_charge_soc_percent_4303 == 58,
          "fresh successor is full Mode4/58/30 without SelfUse")
    check(replay.controller._tariff_execution_hold is None, "actual dispatch ends the pending hold")


async def fixed_expiry():
    replay = Replay()
    await replay.start()
    proof = replay.controller.record.transaction.physical_verification
    await replay.reject_hold()
    settling_boundary = replay.controller.tariff_execution_settling_deadline
    for seconds, generation in ((5, 13), (10, 14), (10, 15)):
        replay.sensor._cancel_planner_callback()
        await replay.reject_hold(elapsed=seconds, generation=generation)
        check(replay.controller.record.state is fixture.ActiveState.EXECUTING,
              f"bounded retry keeps A at {replay.now}: "
              f"{replay.controller.record.state}/{replay.controller.record.reason}")
        check(replay.controller.tariff_execution_settling_deadline == settling_boundary,
              "preparation cannot renew the fixed180s tariff anchor")
        check(replay.controller.record.transaction.physical_verification.observed_at>
              proof.observed_at,
              "only a fresh confirmed predecessor cohort advances physical proof")
    replay.sensor._cancel_planner_callback()
    await replay.reject_hold(elapsed=30, generation=16)
    check(replay.controller.record.state is fixture.ActiveState.RESTORING,
          "an independent current-authority gate may stop before fixed180s expiry")
    check([write.ems_block.mode.value for write in replay.writes] == [4,0],
          "the stopped fallback emits exactly one safe restore")


async def allowed_idle():
    for action in ("monitor", "hold"):
        replay = Replay()
        await replay.start()
        replay.put("rcm_enabled", "on")
        replay.plan("rcm_plan", action=action)
        await replay.reject_hold()
        check(replay.callback_results == [True]
              and replay.controller.record.state is fixture.ActiveState.EXECUTING,
              f"fresh committed enabled RCEm {action} can retain A")
        check(len(replay.writes) == 1, "enabled idle RCEm does not permit a fallback write")


async def rejected_evidence():
    cases = (
        ("other planner", lambda r: setattr(r, "keys", frozenset({"tariff_plan"}))),
        ("mixed planners", lambda r: setattr(r, "keys", frozenset({"rcm_plan", "supervisor_profile"}))),
        ("physical cohort", lambda r: setattr(r.sensor, "_cohort_cancel", lambda: None)),
        ("permission revoked", lambda r: r.put("allow_tariff", "off")),
        ("policy disabled", lambda r: r.put("tariff_enabled", "off")),
        ("supervisor Off", lambda r: r.put("supervisor_mode", "Off")),
        ("manual writer", lambda r: r.put("manual_charge_active", "on")),
        ("OffGrid", lambda r: r.put("ems_mode_readback", 3)),
        ("hardware lost", lambda r: r.put("hardware_readback_supported", 0)),
        ("CCL reduced", lambda r: r.put("bms_max_charge_current", 10)),
        ("CCL stale", lambda r: r.put("bms_max_charge_current", 240, at=r.now-timedelta(seconds=301))),
        ("SOC cap reduced", lambda r: r.put("tariff_maximum_soc", 70)),
        ("RCEm emergency", lambda r: r.plan("rcm_plan", live_emergency=True)),
        ("RCEm unknown emergency", lambda r: r.plan("rcm_plan", live_emergency=None)),
        ("RCEm active", lambda r: r.put("rcm_active", "on")),
    )
    for name, mutation in cases:
        replay = Replay()
        await replay.start()
        replay.during_persist = mutation
        # Planner-key changes must take effect before scheduling the callback.
        if "planner" in name:
            mutation(replay)
            replay.during_persist = None
        await replay.reject_hold()
        check(replay.controller.record.state is fixture.ActiveState.STOPPING,
              f"{name} cannot retain A through cancellation")
        check(len(replay.writes) == 1, f"{name} sends no fallback write")
    for name, attrs in (
        ("action", {"action": "absorb_pv"}),
        ("recalculation", {"result_current": False, "recalculation_pending": True}),
        ("incomplete", {"prediction_ready": None}),
        ("stale", {}),
    ):
        replay = Replay()
        await replay.start()
        replay.put("rcm_enabled", "on")
        replay.plan("rcm_plan", at=replay.now-timedelta(seconds=61) if name == "stale" else replay.now,
                    **attrs)
        await replay.reject_hold()
        check(replay.controller.record.state is fixture.ActiveState.STOPPING,
              f"enabled RCEm {name} is not evidence of idle authority")


async def current_clock():
    replay = Replay()
    await replay.start()
    proof = replay.controller.record.transaction.physical_verification
    def short_delay(r):
        r.now += timedelta(milliseconds=500)
        adapter.CLOCK["now"] = r.now
    replay.during_persist = short_delay
    await replay.reject_hold()
    check(replay.controller.record.state is fixture.ActiveState.EXECUTING,
          "short persistence delay still allows cancellation")
    check(replay.controller.tariff_execution_settling_deadline ==
        replay.now+timedelta(seconds=180),
        "hold anchor uses actual cancellation clock, not older fallback.now")
    check(replay.controller.record.transaction.physical_verification.observed_at>
          proof.observed_at,
          "using the current clock records only the newly confirmed physical proof")
    replay = Replay()
    await replay.start()
    def delay(r):
        r.now += timedelta(seconds=16)
        adapter.CLOCK["now"] = r.now
    replay.during_persist = delay
    await replay.reject_hold()
    check(replay.controller.record.state is fixture.ActiveState.STOPPING,
          "persistence delay ages actual settings; fallback.now cannot revive them")
    check(len(replay.writes) == 1, "delayed fallback cannot write")


async def firmware_stale_snapshot_continuity(
    *, second_refusal=False, revert_to_predecessor=False
):
    """Mode 4 retarget obtains a new arm, ACK and renew after not-queued."""

    hass, entry, runtime, sensor = adapter.environment()
    # This transport-only fixture bypasses async_added_to_hass, which normally
    # initializes the pause state before dispatch/renew can run.
    sensor._pause_state = "off"
    sensor._source_entity_ids = {
        spec.key: adapter._source_entity_id(spec, entry.entry_id)
        for spec in adapter.SENSOR.SUPERVISOR_SOURCE_SPECS
    }
    source_entry = SimpleNamespace(
        entry_id="esphome-source",
        domain="esphome",
        data={"device_name": "source-node"},
    )
    runtime.source_device.config_entry_id = source_entry.entry_id
    hass.config_entries = SimpleNamespace(
        async_get_entry=lambda entry_id: (
            source_entry if entry_id == source_entry.entry_id else None
        )
    )
    names = {
        "source_node_ems_supervisor_control_lease_challenge",
        "source_node_ems_supervisor_write_complete_block_leased",
        "source_node_ems_supervisor_renew_control_lease",
    }
    names.update(
        f"source_node_{family.value}"
        for family in adapter.SENSOR.AtomicWriteFamily
    )
    services = LeaseServices(
        names,
        reject_arm_calls={2, 3} if second_refusal else {2},
    )
    hass.services = services
    sensor._assert_single_transport_instance = lambda: None
    hass.states.values["input_select.hoymiles_ems_supervisor_mode"] = (
        adapter.FakeState("Active")
    )

    clock = [NOW]
    adapter.CLOCK["now"] = NOW
    current = [fixture.frame(
        NOW,
        fixture.execution_source(NOW),
        target=75,
        power=50,
        action=fixture.TariffAction.GRID_SUPPORT_AND_CHARGE,
    )]
    # This transport fixture supplies immutable frames directly. Its generic
    # fake HA states are deliberately not a second valid physical source.
    # The Replay cases above retain production live-state normalization.
    sensor._fresh_active_frame = lambda: current[0]

    async def persist(_record):
        return None

    controller = fixture.SupervisorActiveController(
        persist=persist,
        dispatch=sensor._async_dispatch_atomic_write,
        publish=lambda _record: None,
        clock=lambda: clock[0],
        frame_resampler=sensor._current_dispatch_frame,
    )
    sensor._controller = controller
    sensor._latest_active_frame = current[0]
    await controller.async_initialize()
    await controller.async_reconcile(current[0])
    check(services.arm_calls == 1, "tariff start uses one real leased Mode 4 arm")
    initial_handle = sensor._control_lease_client.handle
    check(initial_handle is not None and initial_handle.command_generation == 1,
          "tariff start owns the first correlated lease")

    clock[0] += timedelta(seconds=2)
    adapter.CLOCK["now"] = clock[0]
    current[0] = fixture.frame(
        clock[0],
        fixture.physical(
            clock[0], target=75, power=50, battery=-5000, generation=11,
        ),
        controller.record,
        target=75,
        power=50,
        action=fixture.TariffAction.GRID_SUPPORT_AND_CHARGE,
    )
    sensor._latest_active_frame = current[0]
    await controller.async_reconcile(current[0])
    check(controller.record.state is fixture.ActiveState.EXECUTING,
          "tariff predecessor is physically confirmed before retarget")
    predecessor = controller.record.transaction
    baseline = predecessor.command_snapshot
    transaction_id = predecessor.transaction_id
    deadline = predecessor.deadline

    clock[0] += timedelta(seconds=1)
    adapter.CLOCK["now"] = clock[0]
    current[0] = fixture.frame(
        clock[0],
        fixture.physical(
            clock[0], target=75, power=50, battery=-5000, generation=12,
        ),
        controller.record,
        target=80,
        power=49,
        action=fixture.TariffAction.GRID_SUPPORT_AND_CHARGE,
    )
    sensor._latest_active_frame = current[0]

    def reject_with_new_cohort():
        recovered_target = 75 if revert_to_predecessor else 80
        recovered_power = 50 if revert_to_predecessor else 49
        clock[0] += timedelta(milliseconds=100)
        adapter.CLOCK["now"] = clock[0]
        current[0] = fixture.frame(
            clock[0],
            fixture.physical(
                clock[0], target=75, power=50, battery=-5000, generation=13,
            ),
            controller.record,
            target=recovered_target,
            power=recovered_power,
            action=fixture.TariffAction.GRID_SUPPORT_AND_CHARGE,
        )
        sensor._latest_active_frame = current[0]
        sensor._cohort_cancel = lambda: None
        sensor._cohort_pending_source = "state_reported"

        def publish_complete_cohort():
            sensor._cohort_cancel = None
            sensor._cohort_pending_source = None

        asyncio.get_running_loop().call_soon(publish_complete_cohort)

    services.reject_hook = reject_with_new_cohort
    await controller.async_reconcile(current[0])
    transaction = controller.record.transaction
    if second_refusal:
        check(services.arm_calls == 3,
              "tariff recovery is bounded to one additional arm")
        check(sensor._control_lease_client.handle is None,
              "a second negative arm grants no tariff lease")
        check(controller.record.state in {
            fixture.ActiveState.STOPPING,
            fixture.ActiveState.RESTORING,
        }, "a second tariff refusal enters the safe STOP/restore path")
        return
    check(controller.record.state is fixture.ActiveState.RETARGETING,
          "tariff successor remains pending until its own FC03 ACK")
    check(transaction.transaction_id == transaction_id
          and transaction.deadline == deadline
          and transaction.command_snapshot == baseline,
          "tariff recovery preserves owner transaction deadline and restore baseline")
    arm_calls = [
        call for call in services.calls
        if call[1].endswith("ems_supervisor_write_complete_block_leased")
    ]
    recovered_target = 75 if revert_to_predecessor else 80
    recovered_power = 50 if revert_to_predecessor else 49
    check(
        [call[2]["force_charge_soc"] for call in arm_calls]
        == [75, 80, recovered_target]
        and [call[2]["maximum_charge_power"] for call in arm_calls]
        == [50, 49, recovered_power],
        "recovery arms only the latest tariff decision without neutral Mode 0",
    )
    check([call[2]["command_generation"] for call in arm_calls] == [1, 2, 3]
          and [call[2]["nonce"] for call in arm_calls]
          == ["00000001", "00000002", "00000003"],
          "negative arm cannot grant or recycle a lease identity")
    handle = sensor._control_lease_client.handle
    check(handle is not None and handle.command_generation == 3
          and handle.block[3:5] == (float(recovered_target), float(recovered_power)),
          "accepted retry installs only the new tariff lease")

    clock[0] += timedelta(seconds=1)
    adapter.CLOCK["now"] = clock[0]
    current[0] = fixture.frame(
        clock[0],
        fixture.physical(
            clock[0], target=recovered_target, power=recovered_power,
            battery=-4000, generation=14,
        ),
        controller.record,
        target=recovered_target,
        power=recovered_power,
        action=fixture.TariffAction.GRID_SUPPORT_AND_CHARGE,
    )
    sensor._latest_active_frame = current[0]
    await controller.async_reconcile(current[0])
    check(controller.record.state is fixture.ActiveState.EXECUTING,
          "accepted tariff successor needs its own newer physical FC03")
    sensor._control_lease_renewal_evidence = lambda: (14, handle.block)
    handle.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
    await sensor._async_renew_control_lease()
    check(handle.sequence == 1 and sensor._control_lease_client.handle is handle,
          "new tariff lease completes a correlated subsequent renew")
    timing = sensor._control_lease_last_result
    check(
        timing is not None
        and timing["status"] == "accepted"
        and timing["response_received_monotonic"]
        >= timing["first_send_monotonic"]
        and timing["response_latency_seconds"] >= 0
        and 0 < timing["soft_lease_remaining_seconds"] <= 120,
        "renew evidence omitted conservative first-send/response timing",
    )
    sensor._cancel_control_lease_callback()


async def main():
    replay = Replay()
    await replay.start()
    projected = replay.controller.recorder_attributes()
    check(projected["rollback_status"] == "pending",
          "tariff fixture did not retain its real rollback obligation")
    check(projected["physical_verification_result"] == "confirmed",
          "tariff fixture lacked physical proof")
    health = adapter.SENSOR._execution_health_contract(
        {
            **projected,
            "execution_last_valid_read_at": replay.now.isoformat(),
            "execution_physical_mode_fresh": True,
            "transaction_owner": "tariff",
            "owner_conflict": False,
        },
        now=replay.now,
    )
    check(health["control_status"] == "healthy",
          f"confirmed tariff execution was mislabeled: {health}")
    await retained_then_latest()
    await fixed_expiry()
    await allowed_idle()
    await rejected_evidence()
    await current_clock()
    await firmware_stale_snapshot_continuity()
    await firmware_stale_snapshot_continuity(revert_to_predecessor=True)
    await firmware_stale_snapshot_continuity(second_refusal=True)
    print(f"Tariff pending dispatch race: PASS ({CHECKS} checks)")


if __name__ == "__main__":
    asyncio.run(main())
