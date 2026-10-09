"""Real adapter regression for an unsent RCE retarget during callbacks."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import test_supervisor_sensor_contract as h
from custom_components.hoymiles_hit_modbus.supervisor_executor import (
    CommandSet,
    ExpectedReadback,
)


class FirmwareServices:
    """Protocol-faithful ESP service model for the field refusal regression."""

    def __init__(self, names: set[str], *, reject_arm_calls: set[int]) -> None:
        self.names = names
        self.calls = []
        self.arm_calls = 0
        self.challenge_calls = 0
        self.reject_hook = None
        self.challenge_hook = None
        self.reject_arm_calls = reject_arm_calls

    def async_services(self):
        return {"esphome": {name: object() for name in self.names}}

    async def async_call(self, domain, service, data, **kwargs):
        self.calls.append((domain, service, dict(data)))
        if not kwargs.get("return_response"):
            return None
        if service.endswith("ems_supervisor_control_lease_challenge"):
            self.challenge_calls += 1
            if self.challenge_hook is not None:
                self.challenge_hook(self.challenge_calls)
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
                "renew_nonce": f"{self.arm_calls + 16:08x}",
            }
        if service.endswith("ems_supervisor_renew_control_lease"):
            return {
                "schema_version": 1,
                "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                "accepted": True,
                "reason": "renewed",
                "renew_nonce": "00000040",
            }
        return {"schema_version": 1, "accepted": True, "reason": "accepted"}


async def scenario(outcome: str, *, enabled_rcm: bool = False) -> None:
    hass, entry, runtime, sensor = h.environment()
    now = h.NOW
    deadline = now + timedelta(minutes=30)
    writes = []
    trace = []
    firmware_refusal = (
        outcome.startswith("firmware_stale_snapshot")
        or outcome == "firmware_bms_challenge_drop"
        or outcome == "firmware_export_challenge_drop"
        or outcome == "firmware_master_stop_challenge"
        or outcome == "firmware_lease_gate_transition"
        or outcome.startswith("challenge_publication_")
        or outcome in {"firmware_permission_challenge", "firmware_off_grid_challenge", "firmware_pause_challenge"}
    )
    firmware_second_refusal = outcome == "firmware_stale_snapshot_twice"
    firmware_revert = outcome == "firmware_stale_snapshot_revert"
    field_forward = outcome == "disabled_rcm_restore_99_to_100"
    field_reverse = outcome == "disabled_rcm_release_100_to_99"
    high_power = firmware_refusal or field_forward or field_reverse
    predecessor_power = 100 if field_reverse else 99 if high_power else 26
    successor_power = 99 if field_reverse else 100 if high_power else 16
    services = None

    if firmware_refusal:
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
            for family in h.SENSOR.AtomicWriteFamily
        )
        services = FirmwareServices(
            names,
            reject_arm_calls=(set() if outcome.startswith("challenge_publication_")
                              else {2, 3} if firmware_second_refusal else {2}),
        )
        hass.services = services

    def eid(key: str) -> str:
        return h._source_entity_id(h.SENSOR._SOURCE_BY_KEY[key], entry.entry_id)

    def store(key: str, value, attrs=None, at=None) -> None:
        hass.states.values[eid(key)] = h.FakeState(
            str(value), attrs, at or h.CLOCK["now"],
        )

    plan = {
        **h._plan_attributes("rce_plan"),
        "system_power_kw": 32.0,
        "current_slot_planned": True,
        "current_slot_start_eligible": True,
        "current_slot_continue_eligible": True,
        "current_slot_end": deadline.isoformat(),
        "current_run_end": deadline.isoformat(),
        "current_slot_execution_discharge_power_kw": (
            predecessor_power * 0.32
        ),
        "current_slot_execution_power_percent": predecessor_power,
        "current_slot_planned_export_kwh": 2.2,
        "current_required_minimum_soc_percent": 49,
        "current_slot_suppression_reason": "eligible",
        "current_slot_load_exhausts_requested_discharge_budget": False,
    }
    rcm_plan = h._plan_attributes("rcm_plan")

    def publish_plan() -> None:
        store("rce_plan", "ready", plan.copy())

    async def dispatch(write) -> None:
        writes.append(write)

    async def drain() -> None:
        for _ in range(700 if outcome == "challenge_publication_cohort" else 120):
            await asyncio.sleep(0.01 if firmware_refusal else 0)
            if sensor._controller_task is None and sensor._pending_active_frame is None:
                return
        raise AssertionError("Controller adapter did not drain")

    sensor.attach_rce_settling_source(lambda: "m" * 64, lambda: None)
    for key, value in {
        "supervisor_mode": "Active", "allow_rce": "on", "rce_enabled": "on",
        "rce_active": "off", "rce_control_data_ready": "on",
        "rce_price_above_threshold": "on", "rce_reserve_ready": "on",
        "sale_block_active": "off", "ems_execution_ready": "on",
        "rce_requested_discharge_power": predecessor_power,
        "rce_effective_discharge_power": predecessor_power,
        "rce_latched_minimum_soc": 49, "battery_soc": 74,
        "bms_voltage": 53,
        "bms_max_discharge_current": 1000 if high_power else 390,
        "ems_mode_readback": 0, "ems_generation": 1,
        "self_use_soc_readback": 30, "backup_soc_readback": 90,
        "charge_soc_readback": 70, "charge_power_ems_readback": 50,
        "discharge_soc_readback": 30,
        "discharge_power_readback": predecessor_power,
        "gcf_enable_readback": 1, "gcf_export_limit_readback": 100,
        "battery_power": 33600 if high_power else 8715,
        "load_power": 1600 if high_power else 6715,
        "grid_power": -32000 if high_power else -2000,
        "pv_power": 0,
    }.items():
        store(key, value)
    publish_plan()
    store("rce_latched_slot_end", "ignored", {"timestamp": deadline.timestamp()})
    await sensor.add_to_platform_finish()
    controller = sensor._controller
    assert controller is not None
    controller._clock = lambda: h.CLOCK["now"]
    if not firmware_refusal:
        controller._dispatch = dispatch
        # These cases intentionally replace the real leased ESP adapter with
        # a local write collector, so they must also remove its lease getter.
        controller._control_lease_valid_until = None
    await drain()
    predecessor = controller.record.transaction
    assert predecessor is not None and predecessor.command_sent_at is not None
    original_id = predecessor.transaction_id
    original_deadline = predecessor.deadline
    original_snapshot = predecessor.command_snapshot
    if firmware_refusal:
        assert services is not None and services.arm_calls == 1
        initial_handle = sensor._control_lease_client.handle
        assert initial_handle is not None
        assert initial_handle.command_generation == 1
        assert initial_handle.block[6] == 99
    else:
        assert len(writes) == 1
        assert (
            writes[0].ems_block.maximum_discharge_power_percent_4306
            == predecessor_power
        )

    def refresh(seconds: float, *, power: float | None = None, mode: int = 5) -> None:
        if power is None:
            power = predecessor_power
        h.CLOCK["now"] = now + timedelta(seconds=seconds)
        for key in (
            "battery_soc", "bms_voltage", "bms_max_discharge_current",
            "self_use_soc_readback", "backup_soc_readback", "charge_soc_readback",
            "charge_power_ems_readback", "gcf_enable_readback",
            "gcf_export_limit_readback", "charge_power_readback",
            "battery_charge_generation", "topology_generation", "machine_type",
            "inverter_count",
        ):
            state = hass.states.values[eid(key)]
            store(key, state.state, state.attributes)
        for key, value in {
            "ems_mode_readback": mode, "ems_generation": int(seconds) + 2,
            "discharge_soc_readback": 49, "discharge_power_readback": power,
            "gcf_generation": int(seconds) + 2,
            "battery_power": 33600 if high_power else 8715,
            "load_power": 1600 if high_power else 6715,
            "grid_power": -32000 if high_power else -2000,
            "pv_power": 0,
        }.items():
            store(key, value)

    refresh(5)
    store("rce_active", "on")
    sensor._recompute()
    await drain()
    assert controller.record.state is h.SENSOR.ActiveState.EXECUTING, controller.record

    if outcome == "health_pending_rce":
        projected = controller.recorder_attributes()
        assert projected["rollback_status"] == "pending"
        assert projected["physical_verification_result"] == "confirmed"
        health = h.SENSOR._execution_health_contract(
            {
                **projected,
                "execution_last_valid_read_at": h.CLOCK["now"].isoformat(),
                "execution_physical_mode_fresh": True,
                "transaction_owner": "rce",
                "owner_conflict": False,
            },
            now=h.CLOCK["now"],
        )
        assert health["control_status"] == "healthy", health
        sensor._cleanup_lifecycle()
        return

    if outcome == "restore_stale_rce":
        from custom_components.hoymiles_hit_modbus.supervisor_executor import (
            AtomicWriteNotQueued,
        )

        async def reject_restore(write) -> None:
            assert write.ems_block is not None and write.ems_block.mode.value == 0
            writes.append(write)
            raise AtomicWriteNotQueued("stale_snapshot_generation")

        controller._dispatch = reject_restore
        store("allow_rce", "off")
        for second in (6, 7):
            refresh(second)
            sensor._recompute()
            await drain()
        transaction = controller.record.transaction
        assert transaction is not None
        assert controller.record.state is h.SENSOR.ActiveState.FAULT
        assert controller.record.reason.value == "command_not_queued"
        assert transaction.restore_attempts == 0
        assert transaction.restore_not_queued_count == 2
        refresh(8, mode=0)
        sensor._recompute()
        await drain()
        assert controller.record.state is h.SENSOR.ActiveState.FAULT
        assert controller.record.owner is h.SENSOR.ExecutionOwner.RCE
        sensor._cleanup_lifecycle()
        return

    if outcome == "notification_cause_rce":
        from custom_components.hoymiles_hit_modbus import ems_notifications as notify

        manager = object.__new__(notify.HoymilesEmsNotificationManager)
        manager.hass = hass
        manager._model = notify.RangeNotificationModel()
        manager._model.observe(notify.ExecutionObservation(
            observed_at=h.CLOCK["now"], policy=None, executing=False,
        ))
        frame = sensor._latest_active_frame
        assert frame is not None
        started = manager._observation(controller.record, frame)
        assert started.executing and started.transaction_id == original_id
        manager._model.observe(started)
        store("allow_rce", "off")
        refresh(6)
        sensor._recompute()
        await drain()
        frame = sensor._latest_active_frame
        assert frame is not None
        assert controller.record.reason.value == "restoring"
        assert controller.record.transaction is not None
        assert controller.record.transaction.reason.value == "authorization_lost"
        observed = manager._observation(controller.record, frame)
        assert observed.terminal_reason == "authorization_lost"
        assert observed.interrupted
        assert observed.transaction_id == original_id
        store("allow_rce", "on")
        terminal = replace(
            controller.record.transaction,
            state=h.SENSOR.ActiveState.IDLE,
            owner=h.SENSOR.ExecutionOwner.NONE,
            rollback_status=h.SENSOR.RollbackStatus.CONFIRMED,
            rollback_result=h.SENSOR.VerificationStatus.CONFIRMED,
        )
        settled = replace(
            controller.record,
            state=h.SENSOR.ActiveState.IDLE,
            owner=h.SENSOR.ExecutionOwner.NONE,
            transaction=None,
            last_transaction=terminal,
            reason=h.SENSOR.ExecutionReason.RESTORE_CONFIRMED,
        )
        manager._model.active.planned_end = deadline
        late_restore = manager._observation(settled, replace(frame, now=deadline))
        assert late_restore.terminal_reason == "authorization_lost"
        assert late_restore.restoration_confirmed and late_restore.interrupted
        sensor._cleanup_lifecycle()
        return

    if outcome == "firmware_lease_gate_transition":
        assert sensor._control_lease_renewal_evidence() is not None
        sensor._master_stop_latched = True
        assert sensor._control_lease_renewal_evidence() is None
        denied = sensor._control_lease_gate
        assert denied["reason"] == "master_stop"
        assert denied["transaction_id"] == original_id
        first_at = denied["first_at"]
        assert sensor._control_lease_renewal_evidence() is None
        assert sensor._control_lease_gate["first_at"] == first_at
        sensor._master_stop_latched = False
        assert sensor._control_lease_renewal_evidence() is not None
        assert sensor._control_lease_gate["status"] == "eligible"
        plan.update(result_current=False, recalculation_pending=True)
        refresh(6)
        publish_plan()
        sensor._recompute()
        await drain()
        assert controller.record.state is h.SENSOR.ActiveState.EXECUTING
        assert sensor._control_lease_renewal_evidence() is not None
        refresh(7)
        store("rce_control_data_ready", "off")
        store("rce_reserve_ready", "off")
        publish_plan()
        sensor._recompute()
        await drain()
        assert controller.record.state is h.SENSOR.ActiveState.EXECUTING
        # Pending helper publication does not revoke the already sent block:
        # fresh physical evidence and the anchored planner hold authorize its
        # bounded lease renewal, never a replacement command.
        assert sensor._control_lease_renewal_evidence() is not None
        assert sensor._control_lease_gate["status"] == "eligible"
        plan.update(result_current=True, recalculation_pending=False)
        refresh(8)
        store("rce_control_data_ready", "on")
        store("rce_reserve_ready", "on")
        publish_plan()
        sensor._recompute()
        await drain()
        assert sensor._control_lease_renewal_evidence() is not None
        assert sensor._control_lease_gate["status"] == "eligible"
        sensor._cleanup_lifecycle()
        return

    persisted = controller._persist
    injected = False
    recovered_unsent = []
    recover_unsent = controller._recover_proven_unsent_retarget

    def record_recovery(*args, **kwargs):
        recovered_unsent.append(True)
        return recover_unsent(*args, **kwargs)

    controller._recover_proven_unsent_retarget = record_recovery

    async def persist(record) -> None:
        nonlocal injected
        await persisted(record)
        transaction = record.transaction
        trace.append((
            record.state.value,
            record.reason.value,
            transaction.command_sent_at if transaction else None,
            sensor._retarget_pending_context(),
        ))
        if (
            not injected
            and record.state is h.SENSOR.ActiveState.RETARGETING
            and transaction is not None
            and transaction.command_sent_at is None
        ):
            injected = True
            h.CLOCK["now"] += timedelta(milliseconds=50)
            generation_id = eid("ems_generation")
            if outcome == "cohort_state_reported":
                state = hass.states.values[generation_id]
                hass.fire_report(
                    generation_id,
                    h.FakeState(state.state, state.attributes, h.CLOCK["now"]),
                )
                context = sensor._retarget_pending_context()
                assert context.cohort_pending and context.source == "state_reported"
            elif outcome == "cohort_state_changed":
                state = hass.states.values[generation_id]
                hass.fire_state(
                    generation_id,
                    h.FakeState(
                        str(int(float(state.state)) + 1),
                        state.attributes,
                        h.CLOCK["now"],
                    ),
                )
                context = sensor._retarget_pending_context()
                assert context.cohort_pending and context.source == "state_changed"
            elif outcome == "planner_pending":
                hass.fire_state(
                    eid("rce_plan"),
                    h.FakeState("ready", plan.copy(), h.CLOCK["now"]),
                )
                assert sensor._retarget_pending_context().planner_pending
            elif outcome.startswith("disabled_rcm_") or outcome.startswith(
                "reject_rcm_"
            ) or outcome == "enabled_rcm_pending":
                if enabled_rcm:
                    # Field configuration: RCEm permission and voltage logic
                    # enabled, but no RCEm actuator owns a physical command.
                    store("allow_rcm", "on")
                    store("rcm_enabled", "on")
                if outcome in {"enabled_rcm_pending", "reject_rcm_enabled_bms"}:
                    store("allow_rcm", "on")
                    store("rcm_enabled", "on")
                    rcm_plan["action"] = "monitor"
                    rcm_plan["live_emergency"] = False
                if outcome == "reject_rcm_enabled_bms":
                    store("bms_max_discharge_current", 10)
                if outcome == "reject_rcm_active":
                    store("rcm_active", "on")
                elif outcome == "reject_rcm_unknown":
                    store("rcm_enabled", "unknown")
                elif outcome == "reject_rcm_alarm":
                    rcm_plan["live_emergency"] = True
                elif outcome in {
                    "disabled_rcm_restore_pending",
                    "disabled_rcm_restore_99_to_100",
                    "reject_rcm_gcf_mismatch",
                    "reject_rcm_306_mismatch",
                    "reject_rcm_gcf_stale",
                    "reject_rcm_cleanup",
                    "reject_rcm_stale_result",
                    "reject_rcm_recalculating",
                }:
                    rcm_plan["action"] = "restore"
                elif outcome in {
                    "disabled_rcm_release_pending",
                    "disabled_rcm_release_100_to_99",
                }:
                    rcm_plan["action"] = "release_export"
                if outcome == "reject_rcm_gcf_mismatch":
                    store("gcf_export_limit_readback", 20)
                    store("gcf_generation", 20)
                elif outcome == "reject_rcm_306_mismatch":
                    store("charge_power_readback", 20)
                    store("battery_charge_generation", 20)
                elif outcome == "reject_rcm_gcf_stale":
                    stale = h.CLOCK["now"] - timedelta(seconds=181)
                    for key in (
                        "gcf_enable_readback",
                        "gcf_export_limit_readback",
                        "gcf_generation",
                    ):
                        state = hass.states.values[eid(key)]
                        store(key, state.state, state.attributes, stale)
                elif outcome == "reject_rcm_cleanup":
                    cleanup_command = CommandSet(export_limit_percent_259=100)
                    cleanup_intent = replace(
                        predecessor.intent,
                        policy=h.SENSOR.ExecutionOwner.RCM,
                        action=h.SENSOR.ExecutionAction.RCM_LIMIT_EXPORT,
                        command=cleanup_command,
                    )
                    unresolved = replace(
                        predecessor,
                        state=h.SENSOR.ActiveState.RESTORING,
                        owner=h.SENSOR.ExecutionOwner.RCM,
                        intent=cleanup_intent,
                        expected_readback=ExpectedReadback.from_command(
                            predecessor.command_snapshot,
                            cleanup_command,
                        ),
                        physical_verification=None,
                        rollback_status=h.SENSOR.RollbackStatus.PENDING,
                    )
                    controller._executor._record = replace(
                        controller.record,
                        last_transaction=unresolved,
                    )
                    assert not sensor._inactive_rcm_predecessor_attested(
                        record=controller.record,
                        transaction=predecessor,
                        action=None,
                        settings=predecessor.command_snapshot,
                        now=h.CLOCK["now"],
                    ), "unknown RCEm action acquired authority from unsettled cleanup"
                rcm_plan["input_revision"] = 2
                if outcome == "reject_rcm_stale_result":
                    rcm_plan["result_current"] = False
                elif outcome == "reject_rcm_recalculating":
                    rcm_plan["recalculation_pending"] = True
                hass.fire_state(
                    eid("rcm_plan"),
                    h.FakeState("monitoring", rcm_plan.copy(), h.CLOCK["now"]),
                )
                context = sensor._retarget_pending_context()
                assert context.planner_pending and context.source == "rcm_plan"
                if outcome == "reject_rcm_mixed":
                    hass.fire_state(
                        eid("rce_plan"),
                        h.FakeState("ready", plan.copy(), h.CLOCK["now"]),
                    )
                    assert sensor._planner_pending_keys == {
                        "rce_plan",
                        "rcm_plan",
                    }
            elif outcome == "new_complete_cohort" or outcome.startswith("fresh_flow_after_slow_unsent_retarget"):
                # A slow persistence boundary can outlive A's stored flow
                # proof while a newer complete FC03/flow cohort proves A.
                refresh(26 if outcome.startswith("fresh_flow_after_slow_unsent_retarget") else 11)
                if outcome.endswith("_stale"):
                    store("battery_power", 8715, at=now+timedelta(seconds=5))
                elif outcome.endswith("_contradicted"):
                    store("battery_power", -800)
                sensor._recompute()
            elif outcome.startswith("reject_"):
                if outcome == "reject_no_action":
                    plan.update(
                        current_slot_planned=False,
                        current_slot_start_eligible=False,
                        current_slot_continue_eligible=False,
                        current_run_end=None,
                        current_slot_execution_discharge_power_kw=0,
                        current_slot_execution_power_percent=0,
                        current_slot_planned_export_kwh=0,
                        current_slot_suppression_reason="no_current_plan",
                    )
                    publish_plan()
                elif outcome == "reject_permission":
                    store("allow_rce", "off")
                elif outcome == "reject_off_grid":
                    store("ems_mode_readback", 3)
                elif outcome == "reject_incomplete_fc03":
                    state = hass.states.values[eid("discharge_power_readback")]
                    store(
                        "discharge_power_readback",
                        state.state,
                        state.attributes,
                        h.CLOCK["now"] - timedelta(seconds=30),
                    )
                elif outcome == "reject_deadline":
                    h.CLOCK["now"] = original_deadline + timedelta(microseconds=1)
                else:
                    raise AssertionError(f"unknown rejection case {outcome}")
                state = hass.states.values[generation_id]
                hass.fire_report(
                    generation_id,
                    h.FakeState(state.state, state.attributes, h.CLOCK["now"]),
                )

    controller._persist = persist
    refresh(10)
    plan.update(
        result_current=True,
        recalculation_pending=False,
        input_revision=33,
        current_slot_execution_discharge_power_kw=successor_power * 0.32,
        current_slot_execution_power_percent=successor_power,
        current_slot_planned_export_kwh=2.008,
    )
    store("rce_requested_discharge_power", successor_power)
    store("rce_effective_discharge_power", successor_power)
    store("rce_control_data_ready", "on")
    store("rce_reserve_ready", "on")
    if firmware_refusal:
        assert services is not None

        if outcome.startswith("challenge_publication_"):
            def publication_during_challenge(challenge_number: int) -> None:
                if challenge_number != 2:
                    return
                # Arrives during the real leased adapter's challenge await,
                # after the controller has authorized and persisted retarget B.
                if outcome == "challenge_publication_generation":
                    refresh(11, power=predecessor_power)
                    sensor._recompute()
                else:
                    generation_id = eid("ems_generation")
                    state = hass.states.values[generation_id]
                    hass.fire_report(generation_id, h.FakeState(
                        state.state, state.attributes, h.CLOCK["now"],
                    ))
                    assert sensor._cohort_cancel is not None
                    if outcome.startswith("challenge_publication_completed"):
                        cohort = next(handle for handle in hass.active_delays()
                                      if "cohort_callback" in handle.callback.__qualname__)
                        def complete_publication():
                            if outcome.endswith("_bms"):
                                store("bms_max_discharge_current", 0)
                            elif outcome.endswith("_permission"):
                                store("allow_rce", "off")
                            elif outcome.endswith("_deadline"):
                                h.CLOCK["now"] = original_deadline + timedelta(seconds=1)
                            cohort.run()
                        asyncio.get_running_loop().call_soon(complete_publication)
            services.challenge_hook = publication_during_challenge

        if outcome in {
            "firmware_bms_challenge_drop",
            "firmware_export_challenge_drop",
            "firmware_master_stop_challenge",
            "firmware_permission_challenge", "firmware_off_grid_challenge", "firmware_pause_challenge",
        }:
            def revoke_during_challenge(challenge_number: int) -> None:
                if challenge_number == 2:
                    if outcome == "firmware_bms_challenge_drop":
                        # The delayed quantitative helper deliberately remains on.
                        store("bms_max_discharge_current", 10)
                    elif outcome == "firmware_export_challenge_drop":
                        store("gcf_export_limit_readback", 0)
                        store("gcf_generation", 12)
                    elif outcome == "firmware_permission_challenge":
                        store("allow_rce", "off")
                    elif outcome == "firmware_off_grid_challenge":
                        store("ems_mode_readback", 3)
                    elif outcome == "firmware_pause_challenge":
                        hass.states.values["input_boolean.hoymiles_ems_paused"] = h.FakeState("on", reported=h.CLOCK["now"])
                    else:
                        sensor.request_master_stop()

            services.challenge_hook = revoke_during_challenge

        def reject_with_new_cohort() -> None:
            refresh(11, power=99)
            if firmware_revert:
                plan.update(
                    input_revision=34,
                    current_slot_execution_discharge_power_kw=31.68,
                    current_slot_execution_power_percent=99,
                )
                store("rce_plan", "ready", plan.copy())
                store("rce_requested_discharge_power", 99)
                store("rce_effective_discharge_power", 99)
            generation_id = eid("ems_generation")
            state = hass.states.values[generation_id]
            hass.fire_report(
                generation_id,
                h.FakeState(state.state, state.attributes, h.CLOCK["now"]),
            )
            pending = list(hass.active_delays())
            assert pending
            cohort = next(
                handle for handle in pending
                if "cohort_callback" in handle.callback.__qualname__
            )
            asyncio.get_running_loop().call_soon(cohort.run)

        services.reject_hook = reject_with_new_cohort
    hass.fire_state(eid("rce_plan"), h.FakeState("ready", plan.copy(), h.CLOCK["now"]))
    h.CLOCK["now"] += timedelta(milliseconds=100)
    for handle in list(hass.active_delays()):
        if not firmware_refusal or "planner_callback" in handle.callback.__qualname__:
            handle.run()
    await drain()

    if outcome in {"fresh_flow_after_slow_unsent_retarget_stale",
                   "fresh_flow_after_slow_unsent_retarget_contradicted"}:
        assert controller.record.state is not h.SENSOR.ActiveState.EXECUTING
        assert all(write.ems_block.maximum_discharge_power_percent_4306 != successor_power
                   for write in writes), "Unproved predecessor authorized a successor"
        sensor._cleanup_lifecycle()
        return

    if firmware_refusal:
        current = controller.record.transaction
        if outcome in {"challenge_publication_completed_bms", "challenge_publication_completed_permission",
                       "challenge_publication_completed_deadline"}:
            assert services.arm_calls == 1, "Revoked successor reached transport after waiting"
            assert sensor._control_lease_renewal_evidence() is None
            sensor._cleanup_lifecycle()
            return
        assert current is not None
        if outcome.startswith("challenge_publication_"):
            if outcome == "challenge_publication_completed":
                assert not recovered_unsent, "Completed callback caused needless unsent recovery"
                assert services.arm_calls == 2, "Qualified successor was not sent in its original attempt"
            assert controller.record.state in {
                h.SENSOR.ActiveState.EXECUTING, h.SENSOR.ActiveState.RETARGETING,
            }, (
                outcome, controller.record.state, controller.record.reason,
            )
            assert current.transaction_id == original_id
            assert current.deadline == original_deadline
            assert current.command_snapshot == original_snapshot
            if outcome == "challenge_publication_cohort":
                assert services.arm_calls == 1, "pending publication reached transport"
            assert not any(call[2].get("mode_code") == 0 for call in services.calls)
            # Complete the ordinary callback and accept B on a later fresh
            # reconciliation, retaining the original transaction and deadline.
            for handle in list(hass.active_delays()):
                if "cohort_callback" in handle.callback.__qualname__ or "planner_callback" in handle.callback.__qualname__:
                    handle.run()
            refresh(12, power=predecessor_power)
            sensor._recompute()
            await drain()
            assert services.arm_calls == 2
            assert controller.record.state is h.SENSOR.ActiveState.RETARGETING
            refresh(13, power=successor_power)
            sensor._recompute()
            await drain()
            assert controller.record.state is h.SENSOR.ActiveState.EXECUTING
            assert controller.record.transaction.transaction_id == original_id
            assert controller.record.transaction.deadline == original_deadline
            # A recovered unsent challenge must regain ordinary renewal, not
            # merely display Mode5 until the previous soft lease expires.
            evidence = sensor._control_lease_renewal_evidence()
            lease_handle = sensor._control_lease_client.handle
            assert evidence is not None and lease_handle is not None
            lease_handle.next_renew_monotonic = asyncio.get_running_loop().time() - .1
            await sensor._async_renew_control_lease()
            assert lease_handle.sequence == 1
            assert controller.record.transaction.transaction_id == original_id
            assert controller.record.transaction.deadline == original_deadline
            store("bms_max_discharge_current", 0)
            sensor._recompute()
            await drain()
            assert sensor._control_lease_renewal_evidence() is None
            sensor._cleanup_lifecycle()
            return
        if outcome in {
            "firmware_bms_challenge_drop",
            "firmware_export_challenge_drop",
            "firmware_master_stop_challenge",
            "firmware_permission_challenge", "firmware_off_grid_challenge", "firmware_pause_challenge",
        }:
            assert services is not None and services.challenge_calls >= 2
            assert services.arm_calls == 1, "unsafe successor reached leased transport"
            assert controller.record.state is not h.SENSOR.ActiveState.EXECUTING
            sensor._cleanup_lifecycle()
            return
        if firmware_second_refusal:
            assert services is not None and services.arm_calls == 3
            assert sensor._control_lease_client.handle is None
            assert controller.record.state in {
                h.SENSOR.ActiveState.STOPPING,
                h.SENSOR.ActiveState.RESTORING,
            }
            sensor._cleanup_lifecycle()
            return
        assert (
            controller.record.state is h.SENSOR.ActiveState.RETARGETING
        ), controller.record
        assert current.transaction_id == original_id
        assert current.deadline == original_deadline
        assert current.command_snapshot == original_snapshot
        recovered_power = 99 if firmware_revert else 100
        assert (
            current.intent.command.ems_block.maximum_discharge_power_percent_4306
            == recovered_power
        )
        assert services is not None and services.arm_calls == 3
        arm_calls = [
            call for call in services.calls
            if call[1].endswith("ems_supervisor_write_complete_block_leased")
        ]
        assert [call[2]["maximum_discharge_power"] for call in arm_calls] == [
            99,
            100,
            recovered_power,
        ]
        assert [call[2]["command_generation"] for call in arm_calls] == [1, 2, 3]
        assert [call[2]["nonce"] for call in arm_calls] == ["00000001", "00000002", "00000003"]
        handle = sensor._control_lease_client.handle
        assert handle is not None and handle.command_generation == 3
        assert handle.block[6] == recovered_power and handle.sequence == 0
        refresh(13, power=recovered_power)
        sensor._recompute()
        await drain()
        current = controller.record.transaction
        assert current is not None
        assert controller.record.state is h.SENSOR.ActiveState.EXECUTING
        assert current.readback_result.value == "confirmed"
        handle.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
        await sensor._async_renew_control_lease()
        assert handle.sequence == 1
        sensor._cancel_control_lease_callback()
        sensor._cleanup_lifecycle()
        return

    if outcome.startswith("reject_"):
        for handle in list(hass.active_delays()):
            handle.run()
        await drain()
        assert controller.record.state is not h.SENSOR.ActiveState.EXECUTING
        if outcome.startswith("reject_rcm_"):
            assert len(writes) in {1, 2}
            if len(writes) == 2:
                assert writes[-1].ems_block.mode.value != 5
        elif outcome in {"reject_incomplete_fc03", "reject_deadline"}:
            assert len(writes) == 1
        else:
            assert len(writes) == 2
            if outcome == "reject_off_grid":
                assert writes[-1].ems_block.mode.value == 3
            else:
                assert writes[-1].ems_block.mode.value == 0
        assert all(
            write.ems_block.maximum_discharge_power_percent_4306 != successor_power
            for write in writes
        )
        sensor._cleanup_lifecycle()
        return

    if outcome in {
        "cohort_state_reported",
        "cohort_state_changed",
        "planner_pending",
        "disabled_rcm_pending",
        "enabled_rcm_pending",
        "disabled_rcm_restore_pending",
        "disabled_rcm_release_pending",
        "disabled_rcm_restore_99_to_100",
        "disabled_rcm_release_100_to_99",
    }:
        # The successor was cancelled before transport; the same physical
        # predecessor remains active until the one existing callback completes.
        current = controller.record.transaction
        assert controller.record.state is h.SENSOR.ActiveState.EXECUTING
        assert current is not None and current.command_sent_at is not None
        assert len(writes) == 1
        assert (
            writes[0].ems_block.maximum_discharge_power_percent_4306
            == predecessor_power
        )
        assert current.transaction_id == original_id
        assert current.deadline == original_deadline
        assert current.command_snapshot == original_snapshot
        pending_handles = list(hass.active_delays())
        assert pending_handles
        for handle in pending_handles:
            handle.run()
        await drain()

    current = controller.record.transaction
    assert current is not None
    assert current.transaction_id == original_id
    assert current.deadline == original_deadline
    assert current.command_snapshot == original_snapshot
    assert len(writes) == 2
    assert (
        writes[1].ems_block.maximum_discharge_power_percent_4306
        == successor_power
    )
    assert current.command_sent_at is not None
    assert current.command_sent_at > predecessor.command_sent_at
    refresh(28 if outcome == "fresh_flow_after_slow_unsent_retarget" else 13,
            power=successor_power)
    sensor._recompute()
    await drain()
    current = controller.record.transaction
    assert current is not None
    assert controller.record.state is h.SENSOR.ActiveState.EXECUTING
    assert current.readback_result.value == "confirmed"
    assert len(writes) == 2
    sensor._cleanup_lifecycle()


if __name__ == "__main__":
    loaded_count = h.SENSOR._loaded_entry_count
    for result in (
        "stable",
        "planner_pending",
        "disabled_rcm_pending",
        "enabled_rcm_pending",
        "disabled_rcm_restore_pending",
        "disabled_rcm_release_pending",
        "disabled_rcm_restore_99_to_100",
        "disabled_rcm_release_100_to_99",
        "cohort_state_reported",
        "cohort_state_changed",
        "new_complete_cohort",
        "fresh_flow_after_slow_unsent_retarget",
        "fresh_flow_after_slow_unsent_retarget_stale",
        "fresh_flow_after_slow_unsent_retarget_contradicted",
        "firmware_stale_snapshot_cohort",
        "firmware_stale_snapshot_revert",
        "firmware_stale_snapshot_twice",
        "firmware_bms_challenge_drop",
        "firmware_export_challenge_drop",
        "firmware_master_stop_challenge",
        "firmware_permission_challenge", "firmware_off_grid_challenge", "firmware_pause_challenge",
        "firmware_lease_gate_transition",
        "challenge_publication_generation",
        "challenge_publication_cohort",
        "challenge_publication_completed",
        "challenge_publication_completed_bms",
        "challenge_publication_completed_permission",
        "challenge_publication_completed_deadline",
        "health_pending_rce",
        "restore_stale_rce",
        "notification_cause_rce",
        "reject_no_action",
        "reject_permission",
        "reject_off_grid",
        "reject_incomplete_fc03",
        "reject_deadline",
        "reject_rcm_active",
        "reject_rcm_enabled_bms",
        "reject_rcm_unknown",
        "reject_rcm_alarm",
        "reject_rcm_mixed",
        "reject_rcm_cleanup",
        "reject_rcm_gcf_mismatch",
        "reject_rcm_306_mismatch",
        "reject_rcm_gcf_stale",
    ):
        try:
            asyncio.run(scenario(result))
            print(f"PASS RCE unsent retarget cohort: {result}")
        finally:
            h.SENSOR._loaded_entry_count = loaded_count
