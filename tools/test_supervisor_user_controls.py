"""Deterministic safety contract for the task-2 EMS user controls."""

from __future__ import annotations

import asyncio
from types import MethodType, SimpleNamespace
from typing import Any

from test_supervisor_sensor_contract import FakeHass, FakeState, SENSOR


class ControlSensor:
    def __init__(self) -> None:
        self.recomputes = 0
        self.master_stops = 0
        self._master_stop_latched = False
        self.controls_finished = 0
        self._controller: Any = None

    def _recompute(self, *, raise_on_error: bool) -> None:
        assert raise_on_error is True
        self.recomputes += 1

    async def async_master_stop(self) -> None:
        self.master_stops += 1

    def request_master_stop(self) -> None:
        self._master_stop_latched = True

    async def async_finish_master_stop_controls(self) -> None:
        self.controls_finished += 1


class ControlController:
    def __init__(self, log: list[tuple[Any, ...]]) -> None:
        self.log = log
        self.record = SimpleNamespace(
            master_stop_result=SimpleNamespace(
                status=SENSOR.MasterStopStatus.COMPLETED,
            ),
            owner=SENSOR.ExecutionOwner.NONE,
            transaction=None,
        )

    async def async_rearm_after_master_stop(self) -> None:
        self.log.append(("controller", "rearm"))


def _environment() -> tuple[FakeHass, ControlSensor, list[tuple[Any, ...]]]:
    hass = FakeHass()
    sensor = ControlSensor()
    log: list[tuple[Any, ...]] = []
    hass.data[SENSOR._GUARD_KEY] = SENSOR._SupervisorGuard(
        sensors={"entry": sensor},
        loaded_entry_count=1,
    )
    hass.states.values[SENSOR.EMS_PAUSED_ENTITY_ID] = FakeState("off")
    hass.states.values[SENSOR._SUPERVISOR_MODE_ENTITY_ID] = FakeState(
        "Active", {"options": ["Off", "Active"]}
    )
    for preference, enabled, allowed in SENSOR._POLICY_HELPERS.values():
        hass.states.values[preference] = FakeState("off")
        hass.states.values[enabled] = FakeState("off")
        if allowed is not None:
            hass.states.values[allowed] = FakeState("off")

    async def service_call(
        domain: str,
        service: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        assert kwargs == {"blocking": True}
        entity_id = data["entity_id"]
        log.append((domain, service, entity_id, data.get("option")))
        if domain == "input_boolean":
            hass.states.values[entity_id] = FakeState(
                "on" if service == "turn_on" else "off"
            )
        elif domain == "input_select":
            hass.states.values[entity_id] = FakeState(
                data["option"], {"options": ["Off", "Active"]}
            )
        else:  # pragma: no cover - a regression should fail loudly
            raise AssertionError(f"unexpected service: {domain}.{service}")

    hass.services.async_call = service_call
    return hass, sensor, log


def _entities(policy_id: str) -> tuple[str, str, str | None]:
    return SENSOR._POLICY_HELPERS[policy_id]


async def _test_policy_pair_order_and_idempotence() -> None:
    hass, sensor, log = _environment()
    preference, enabled, allowed = _entities("rce")
    assert allowed is not None

    await SENSOR.async_set_supervisor_policy_enabled(hass, "rce", True)
    assert [entry[2] for entry in log] == [preference, enabled, allowed]
    assert [entry[1] for entry in log] == ["turn_on", "turn_on", "turn_on"]

    log.clear()
    await asyncio.gather(
        SENSOR.async_set_supervisor_policy_enabled(hass, "rce", True),
        SENSOR.async_set_supervisor_policy_enabled(hass, "rce", True),
    )
    assert log == []

    await SENSOR.async_set_supervisor_policy_enabled(hass, "rce", False)
    assert [entry[2] for entry in log] == [preference, allowed, enabled]
    assert [entry[1] for entry in log] == ["turn_off", "turn_off", "turn_off"]
    assert sensor.recomputes == 4


async def _test_policy_midflight_mode_change_fails_closed() -> None:
    hass, sensor, log = _environment()
    _preference, enabled, allowed = _entities("tariff")
    assert allowed is not None
    normal_call = hass.services.async_call

    async def mode_flip_call(*args: Any, **kwargs: Any) -> None:
        await normal_call(*args, **kwargs)
        data = args[2]
        if data["entity_id"] == enabled and args[1] == "turn_on":
            hass.states.values[SENSOR._SUPERVISOR_MODE_ENTITY_ID] = FakeState(
                "Off", {"options": ["Off", "Active"]}
            )

    hass.services.async_call = mode_flip_call
    try:
        await SENSOR.async_set_supervisor_policy_enabled(hass, "tariff", True)
    except RuntimeError as err:
        assert "mode changed" in str(err)
    else:  # pragma: no cover
        raise AssertionError("mid-flight mode change was accepted")
    assert hass.states.values[allowed].state == "off"
    assert hass.states.values[SENSOR.EMS_PAUSED_ENTITY_ID].state == "on"
    assert hass.states.values[SENSOR._SUPERVISOR_MODE_ENTITY_ID].state == "Off"
    assert sensor.recomputes == 1
    assert log[-1][2] == SENSOR.EMS_PAUSED_ENTITY_ID


async def _test_policy_failure_boundaries_remain_observable_and_fail_closed() -> None:
    preference, enabled, allowed = _entities("rce")
    assert allowed is not None
    scenarios = (
        ("before_first", preference, "before", False, False, True),
        ("after_preference", preference, "after", True, False, True),
        ("after_enabled", enabled, "after", True, True, True),
        ("at_allowed", allowed, "before", True, True, True),
        ("at_pause", allowed, "before", True, True, False),
    )
    for name, failed_entity, timing, preference_on, enabled_on, pause_on in scenarios:
        hass, sensor, _log = _environment()
        normal_call = hass.services.async_call

        async def boundary_call(*args: Any, **kwargs: Any) -> None:
            entity_id = args[2]["entity_id"]
            service = args[1]
            fail_pause = name == "at_pause" and entity_id == SENSOR.EMS_PAUSED_ENTITY_ID
            fail_primary = entity_id == failed_entity and service == "turn_on"
            if (fail_pause or (fail_primary and timing == "before")):
                raise RuntimeError(f"simulated {name} failure")
            await normal_call(*args, **kwargs)
            if fail_primary and timing == "after":
                raise RuntimeError(f"simulated {name} response failure")

        hass.services.async_call = boundary_call
        try:
            await SENSOR.async_set_supervisor_policy_enabled(hass, "rce", True)
        except RuntimeError as err:
            assert name in str(err)
        else:  # pragma: no cover
            raise AssertionError(f"{name} failure was accepted")

        assert (hass.states.values[preference].state == "on") is preference_on
        assert (hass.states.values[enabled].state == "on") is enabled_on
        assert hass.states.values[allowed].state == "off"
        assert (hass.states.values[SENSOR.EMS_PAUSED_ENTITY_ID].state == "on") is pause_on
        assert hass.states.values[SENSOR._SUPERVISOR_MODE_ENTITY_ID].state == "Off"
        assert sensor.recomputes == 1


async def _test_pause_resume_order() -> None:
    hass, sensor, log = _environment()
    await SENSOR.async_set_supervisor_paused(hass, True)
    assert [entry[2] for entry in log] == [
        SENSOR.EMS_PAUSED_ENTITY_ID,
        SENSOR._SUPERVISOR_MODE_ENTITY_ID,
    ]
    assert hass.states.values[SENSOR.EMS_PAUSED_ENTITY_ID].state == "on"
    assert hass.states.values[SENSOR._SUPERVISOR_MODE_ENTITY_ID].state == "Off"

    log.clear()
    await SENSOR.async_set_supervisor_paused(hass, False)
    assert [entry[2] for entry in log] == [
        SENSOR._SUPERVISOR_MODE_ENTITY_ID,
        SENSOR.EMS_PAUSED_ENTITY_ID,
    ]
    assert hass.states.values[SENSOR.EMS_PAUSED_ENTITY_ID].state == "off"
    assert hass.states.values[SENSOR._SUPERVISOR_MODE_ENTITY_ID].state == "Active"
    assert sensor.recomputes == 2


async def _test_master_stop_snapshot_failure_does_not_block_stop() -> None:
    hass, sensor, _log = _environment()
    preference, enabled, allowed = _entities("rce")
    assert allowed is not None
    hass.states.values[enabled] = FakeState("on")
    hass.states.values[allowed] = FakeState("on")
    normal_call = hass.services.async_call

    async def failing_snapshot(*args: Any, **kwargs: Any) -> None:
        if args[2]["entity_id"] == preference:
            raise RuntimeError("simulated preference failure")
        await normal_call(*args, **kwargs)

    hass.services.async_call = failing_snapshot
    await SENSOR.async_request_supervisor_master_stop(hass)
    assert sensor.master_stops == 1
    assert sensor.controls_finished == 1


async def _test_master_stop_preempts_hung_control() -> None:
    hass, sensor, _log = _environment()
    preference, _enabled, _allowed = _entities("rce")
    entered = asyncio.Event()
    release = asyncio.Event()
    normal_call = hass.services.async_call

    async def hanging_control(*args: Any, **kwargs: Any) -> None:
        if args[2]["entity_id"] == preference:
            entered.set()
            await release.wait()
        await normal_call(*args, **kwargs)

    hass.services.async_call = hanging_control
    control = asyncio.create_task(
        SENSOR.async_set_supervisor_policy_enabled(hass, "rce", True)
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    stop = asyncio.create_task(SENSOR.async_request_supervisor_master_stop(hass))
    await asyncio.sleep(0)
    assert sensor._master_stop_latched is True
    assert sensor.master_stops == 1
    assert not stop.done()

    release.set()
    try:
        await asyncio.wait_for(control, timeout=1)
    except RuntimeError as err:
        assert "superseded by MASTER STOP" in str(err)
    else:  # pragma: no cover
        raise AssertionError("late policy completion survived MASTER STOP")
    await asyncio.wait_for(stop, timeout=1)
    assert sensor.controls_finished == 1


async def _test_master_stop_latches_before_store_finishes() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    class HangingStore:
        async def async_save(self, _value: Any) -> None:
            entered.set()
            await release.wait()

    sensor = SimpleNamespace(
        _master_stop_latched=False,
        _master_stop_requested_at=None,
        _master_stop_latch_persisted=False,
        _master_stop_controls_fenced=False,
        _master_stop_store=HangingStore(),
        _execution_adapter_error=None,
        _unloading=False,
        _publish_composite_state=lambda: None,
    )
    sensor.request_master_stop = MethodType(
        SENSOR.HoymilesSupervisorSensor.request_master_stop, sensor
    )
    sensor._async_latch_master_stop = MethodType(
        SENSOR.HoymilesSupervisorSensor._async_latch_master_stop, sensor
    )

    task = asyncio.create_task(sensor._async_latch_master_stop())
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert sensor._master_stop_latched is True
    assert sensor._master_stop_requested_at is not None
    assert sensor._master_stop_latch_persisted is False
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, timeout=1)
    assert sensor._master_stop_latch_persisted is True


async def _test_master_stop_store_failure_does_not_block_controller() -> None:
    class FailingStore:
        def __init__(self, *, hang: bool) -> None:
            self.hang = hang

        async def async_save(self, _value: Any) -> None:
            if self.hang:
                await asyncio.Future()
            raise OSError("simulated latch Store failure")

    class PhysicalController:
        def __init__(self) -> None:
            self.calls = 0
            self.record = SimpleNamespace(
                master_stop_result=SimpleNamespace(
                    status=SENSOR.MasterStopStatus.NOT_REQUESTED,
                ),
                transaction=None,
            )

        async def async_master_stop(self, _frame: Any) -> None:
            self.calls += 1
            self.record.master_stop_result.status = SENSOR.MasterStopStatus.IN_PROGRESS

    async def run(*, hang: bool, expected_error: str) -> None:
        controller = PhysicalController()
        sensor = SimpleNamespace(
            _master_stop_latched=False,
            _master_stop_requested_at=None,
            _master_stop_latch_persisted=False,
            _master_stop_latch_persist_error=None,
            _master_stop_controller_start_pending=False,
            _master_stop_controls_fenced=False,
            _master_stop_store=FailingStore(hang=hang),
            _execution_adapter_error=None,
            _unloading=False,
            _removed=False,
            _publish_composite_state=lambda: None,
            _controller=controller,
            _latest_active_frame=object(),
            _available=True,
            _sync_execution_watchdog=lambda _controller: None,
            _publish_notifications=lambda *_args: None,
        )

        async def accounting(*_args: Any) -> None:
            return None

        async def finalize() -> None:
            return None

        sensor._async_publish_accounting = accounting
        sensor._async_finalize_master_stop_if_safe = finalize
        sensor.request_master_stop = MethodType(
            SENSOR.HoymilesSupervisorSensor.request_master_stop, sensor
        )
        sensor._async_latch_master_stop = MethodType(
            SENSOR.HoymilesSupervisorSensor._async_latch_master_stop, sensor
        )
        sensor._async_advance_master_stop = MethodType(
            SENSOR.HoymilesSupervisorSensor._async_advance_master_stop, sensor
        )
        sensor.async_master_stop = MethodType(
            SENSOR.HoymilesSupervisorSensor.async_master_stop, sensor
        )

        original_timeout = SENSOR._MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS
        SENSOR._MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS = 0.01
        try:
            stop_task = asyncio.create_task(sensor.async_master_stop())
            await asyncio.sleep(0)
            assert controller.calls == 1
            if hang:
                assert not stop_task.done()
            await asyncio.wait_for(stop_task, timeout=0.2)
        finally:
            SENSOR._MASTER_STOP_AUXILIARY_TIMEOUT_SECONDS = original_timeout
        assert sensor._master_stop_latched is True
        assert sensor._master_stop_latch_persisted is False
        assert sensor._master_stop_latch_persist_error == expected_error

    await run(hang=True, expected_error="master_stop_latch_persist_timeout")
    await run(hang=False, expected_error="master_stop_latch_persist_failed")


async def _test_completed_master_stop_starts_a_new_controller_generation() -> None:
    class GenerationController:
        def __init__(self) -> None:
            self.record = SimpleNamespace(
                master_stop_result=SimpleNamespace(
                    status=SENSOR.MasterStopStatus.COMPLETED,
                ),
                transaction=None,
            )
            self.requests = 0
            self.reconciles = 0

        async def async_master_stop(self, _frame: Any) -> None:
            self.requests += 1
            self.record.master_stop_result.status = SENSOR.MasterStopStatus.REQUESTED

        async def async_reconcile(self, _frame: Any) -> None:
            self.reconciles += 1

    controller = GenerationController()
    sensor = SimpleNamespace()
    advance = MethodType(
        SENSOR.HoymilesSupervisorSensor._async_advance_master_stop, sensor
    )
    await advance(controller, object(), retry_blocked=True)
    assert controller.requests == 1
    assert controller.reconciles == 0
    await advance(controller, object())
    assert controller.requests == 1
    assert controller.reconciles == 1


async def _test_conscious_master_stop_resume_order() -> None:
    hass, sensor, log = _environment()
    sensor._controller = ControlController(log)
    hass.states.values[SENSOR.EMS_PAUSED_ENTITY_ID] = FakeState("on")
    hass.states.values[SENSOR._SUPERVISOR_MODE_ENTITY_ID] = FakeState(
        "Off", {"options": ["Off", "Active"]}
    )
    preference, enabled, allowed = _entities("rce")
    assert allowed is not None
    hass.states.values[preference] = FakeState("on")

    await SENSOR.async_resume_supervisor_after_master_stop(hass)
    assert log == [
        ("controller", "rearm"),
        ("input_boolean", "turn_on", enabled, None),
        ("input_boolean", "turn_on", allowed, None),
        (
            "input_select",
            "select_option",
            SENSOR._SUPERVISOR_MODE_ENTITY_ID,
            "Active",
        ),
        ("input_boolean", "turn_off", SENSOR.EMS_PAUSED_ENTITY_ID, None),
    ]
    assert hass.states.values[SENSOR.EMS_PAUSED_ENTITY_ID].state == "off"
    assert hass.states.values[SENSOR._SUPERVISOR_MODE_ENTITY_ID].state == "Active"
    assert sensor.recomputes == 1


async def _test_notification_store_cannot_delay_master_stop() -> None:
    """MASTER STOP finalizes while notifier persistence remains suspended."""

    entered = asyncio.Event()
    release = asyncio.Event()
    store_tasks: list[asyncio.Task[None]] = []
    advances = 0
    finalized = 0

    class HangingNotificationStore:
        async def async_save(self, _value: Any) -> None:
            entered.set()
            await release.wait()

    store = HangingNotificationStore()

    def notification_sink(*_args: Any) -> None:
        store_tasks.append(asyncio.create_task(store.async_save({"event": "stop"})))

    async def latch() -> None:
        return None

    async def advance(_controller: Any, _frame: Any, **_kwargs: Any) -> None:
        nonlocal advances
        advances += 1

    async def accounting(_record: Any, _frame: Any) -> None:
        return None

    async def finalize() -> None:
        nonlocal finalized
        finalized += 1

    controller = SimpleNamespace(record=object())
    sensor = SimpleNamespace(
        request_master_stop=lambda: None,
        _notification_sink=notification_sink,
        _notification_adapter_error=None,
        _source_entity_ids={},
        _publish_composite_state=lambda: None,
        _async_latch_master_stop=latch,
        _controller=controller,
        _latest_active_frame=object(),
        _available=True,
        _async_advance_master_stop=advance,
        _sync_execution_watchdog=lambda _controller: None,
        _async_publish_accounting=accounting,
        _async_finalize_master_stop_if_safe=finalize,
    )
    sensor._publish_notifications = MethodType(
        SENSOR.HoymilesSupervisorSensor._publish_notifications, sensor
    )
    sensor.async_master_stop = MethodType(
        SENSOR.HoymilesSupervisorSensor.async_master_stop, sensor
    )

    await asyncio.wait_for(sensor.async_master_stop(), timeout=0.2)
    await asyncio.wait_for(entered.wait(), timeout=0.2)
    assert advances == 1
    assert finalized == 1
    assert store_tasks and not store_tasks[0].done()

    # A second safety evaluation also reaches physical reconciliation/finalize
    # before the first notification write is released.
    await asyncio.wait_for(sensor.async_master_stop(), timeout=0.2)
    assert advances == 2
    assert finalized == 2
    release.set()
    await asyncio.gather(*store_tasks)


async def _main() -> None:
    tests = (
        _test_policy_pair_order_and_idempotence,
        _test_policy_midflight_mode_change_fails_closed,
        _test_policy_failure_boundaries_remain_observable_and_fail_closed,
        _test_pause_resume_order,
        _test_master_stop_snapshot_failure_does_not_block_stop,
        _test_master_stop_preempts_hung_control,
        _test_master_stop_latches_before_store_finishes,
        _test_master_stop_store_failure_does_not_block_controller,
        _test_completed_master_stop_starts_a_new_controller_generation,
        _test_conscious_master_stop_resume_order,
        _test_notification_store_cannot_delay_master_stop,
    )
    for test in tests:
        await test()
        print(f"PASS {test.__name__.removeprefix('_test_').upper()}")
    print(f"Supervisor user controls: PASS groups={len(tests)}/{len(tests)}")


if __name__ == "__main__":
    asyncio.run(_main())
