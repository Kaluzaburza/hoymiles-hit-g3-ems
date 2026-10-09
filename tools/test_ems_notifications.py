"""Range grouping and Home Assistant delivery tests for EMS notifications."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from custom_components.hoymiles_hit_modbus import ems_notifications as MODULE  # noqa: E402
from custom_components.hoymiles_hit_modbus.ems_notifications import (  # noqa: E402
    ExecutionObservation,
    HoymilesEmsNotificationManager,
    RangeNotificationModel,
)


UTC = timezone.utc
WARSAW = ZoneInfo("Europe/Warsaw")
START = datetime(2026, 9, 12, 3, 0, tzinfo=UTC)


def _idle(
    at: datetime,
    *,
    restored: bool = False,
    failed: bool = False,
    interrupted: bool = False,
    transaction_id: str | None = None,
    reason: str | None = None,
) -> ExecutionObservation:
    return ExecutionObservation(
        observed_at=at,
        policy=None,
        executing=False,
        restoration_confirmed=restored,
        restoration_failed=failed,
        interrupted=interrupted,
        transaction_id=transaction_id,
        terminal_reason=reason,
    )


def _running(
    policy: str,
    at: datetime,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    transaction_id: str | None = None,
) -> ExecutionObservation:
    return ExecutionObservation(
        observed_at=at,
        policy=policy,
        executing=True,
        actual_start=start or at,
        planned_end=end,
        transaction_id=transaction_id,
    )


def _initialized_model(at: datetime = START) -> RangeNotificationModel:
    model = RangeNotificationModel()
    assert model.observe(_idle(at)) == ()
    assert model.initialized
    return model


def test_range_matrix() -> int:
    checks = 0
    for policy in ("tariff", "rce", "rcm"):
        model = _initialized_model()
        planned_end = START + timedelta(hours=1)
        events = model.observe(
            _running(policy, START + timedelta(seconds=1), end=planned_end)
        )
        assert [event.kind for event in events] == ["start"]
        checks += 1

        for index in range(100):
            # Power, SOC, revision, transaction and a shifted end are all
            # intentionally absent from the logical identity.
            shifted = planned_end + timedelta(minutes=15 if index >= 50 else 0)
            assert model.observe(
                _running(
                    policy,
                    START + timedelta(seconds=2 + index),
                    start=START + timedelta(seconds=1 + index),
                    end=shifted,
                )
            ) == ()
        checks += 100

        # A neutral transition shorter than the 120-second continuity window
        # models a slot boundary or technical retarget.
        assert model.observe(
            _idle(START + timedelta(minutes=30), restored=True)
        ) == ()
        assert model.observe(
            _running(
                policy,
                START + timedelta(minutes=31),
                start=START + timedelta(minutes=31),
                end=planned_end + timedelta(minutes=15),
            )
        ) == ()
        checks += 2

        assert model.observe(
            _idle(START + timedelta(hours=1, minutes=15), restored=True)
        ) == ()
        events = model.observe(
            _idle(
                START + timedelta(hours=1, minutes=17, seconds=1),
                restored=True,
            )
        )
        assert len(events) == 1 and events[0].kind == "end"
        assert events[0].outcome == "completed"
        assert events[0].occurred_at == START + timedelta(hours=1, minutes=15)
        checks += 3
    return checks


def test_distinct_ranges_stop_resume_and_bootstrap() -> int:
    model = _initialized_model()
    first = model.observe(_running("tariff", START + timedelta(seconds=1)))
    assert len(first) == 1 and first[0].kind == "start"
    assert model.observe(
        _idle(START + timedelta(minutes=10), restored=True, interrupted=True)
    )[0].outcome == "interrupted"

    second = model.observe(_running("tariff", START + timedelta(hours=1)))
    assert len(second) == 1 and second[0].event_id != first[0].event_id
    assert model.observe(_idle(START + timedelta(hours=2), failed=True))[0].outcome == (
        "unconfirmed"
    )

    bootstrap = RangeNotificationModel()
    assert bootstrap.observe(
        _running("rce", START + timedelta(minutes=1)), bootstrap=True
    ) == ()
    assert bootstrap.active is not None and bootstrap.active.start_handled
    assert bootstrap.observe(
        _idle(START + timedelta(hours=1), restored=True)
    ) == ()
    terminal = bootstrap.observe(
        _idle(START + timedelta(hours=1, minutes=2, seconds=1), restored=True)
    )
    assert len(terminal) == 1 and terminal[0].kind == "end"
    return 8


def test_early_authorization_loss_with_late_restore() -> int:
    """A missed STOPPING observation cannot turn an early stop into success."""

    planned_end = START + timedelta(minutes=10)
    started = START + timedelta(seconds=1)
    model = _initialized_model()
    assert model.observe(_running(
        "rce", started, end=planned_end, transaction_id="rce:early",
    ))
    manager = object.__new__(HoymilesEmsNotificationManager)
    manager.hass = SimpleNamespace(states=SimpleNamespace(
        is_state=lambda *_args: False,
    ))
    manager._model = model
    transaction = SimpleNamespace(
        owner=MODULE.ExecutionOwner.RCE,
        transaction_id="rce:early",
        reason=MODULE.ExecutionReason.AUTHORIZATION_LOST,
        readback_result=MODULE.VerificationStatus.CONFIRMED,
        physical_verification=SimpleNamespace(
            status=MODULE.VerificationStatus.CONFIRMED,
        ),
        rollback_status=MODULE.RollbackStatus.CONFIRMED,
        rollback_result=MODULE.VerificationStatus.CONFIRMED,
    )
    record = SimpleNamespace(
        state=MODULE.ActiveState.IDLE,
        transaction=None,
        last_transaction=transaction,
        reason=MODULE.ExecutionReason.RESTORE_CONFIRMED,
        starts_allowed=True,
        master_stop_result=SimpleNamespace(
            status=MODULE.MasterStopStatus.NOT_REQUESTED,
        ),
    )
    # Authorization was lost at T-2m; notification worker next observes only
    # the final restored record at T+1m, after the planned end.
    observed = manager._observation(record, SimpleNamespace(
        now=planned_end + timedelta(minutes=1),
    ))
    assert observed.restoration_confirmed and observed.interrupted, observed
    ended = model.observe(observed)
    assert len(ended) == 1 and ended[0].outcome == "interrupted", ended
    return 2


def test_policy_switch_and_local_time_boundaries() -> int:
    model = _initialized_model()
    assert len(model.observe(_running("rce", START + timedelta(seconds=1)))) == 1
    switched = model.observe(_running("rcm", START + timedelta(minutes=15)))
    assert [event.kind for event in switched] == ["end", "start"]
    assert switched[0].outcome == "completed"

    # The formatter must retain dates for midnight and distinguish both DST
    # folds even when their local clock labels are equal.
    before_midnight = datetime(2026, 9, 12, 23, 50, tzinfo=WARSAW)
    after_midnight = datetime(2026, 9, 13, 0, 10, tzinfo=WARSAW)
    assert "2026-09-12" in MODULE._format_span(before_midnight, after_midnight)
    first_fold = datetime(2026, 10, 25, 2, 30, tzinfo=WARSAW, fold=0)
    second_fold = datetime(2026, 10, 25, 2, 30, tzinfo=WARSAW, fold=1)
    assert first_fold.utcoffset() != second_fold.utcoffset()
    return 5


def test_continuous_hour_has_only_range_boundaries() -> int:
    model = _initialized_model(START - timedelta(minutes=1))
    end = START + timedelta(hours=1)
    events = model.observe(_running("tariff", START, end=end))
    assert [event.kind for event in events] == ["start"]
    for minutes in (15, 30, 45):
        assert model.observe(
            _running(
                "tariff",
                START + timedelta(minutes=minutes),
                start=START + timedelta(minutes=minutes),
                end=end + timedelta(minutes=minutes),
            )
        ) == ()
    assert model.observe(_idle(end, restored=True)) == ()
    terminal = model.observe(
        _idle(end + timedelta(minutes=2, seconds=1), restored=True)
    )
    assert len(terminal) == 1 and terminal[0].kind == "end"
    assert terminal[0].occurred_at == end
    return 8


def test_successful_resume_clears_transient_terminal_reason() -> int:
    model = _initialized_model()
    assert model.observe(_running("tariff", START + timedelta(seconds=1)))
    assert model.observe(
        _idle(
            START + timedelta(minutes=10),
            reason="physical_flow_contradiction",
            transaction_id=None,
        )
    ) == ()
    assert model.active is not None
    assert model.active.final_reason == "physical_flow_contradiction"
    assert model.observe(_running("tariff", START + timedelta(minutes=11))) == ()
    assert model.active is not None and model.active.final_reason is None
    return 4


def test_logical_range_correlation_and_real_cycle_split() -> int:
    model = _initialized_model()
    start = model.observe(
        _running("tariff", START + timedelta(seconds=1), transaction_id="tariff:a")
    )
    assert [event.kind for event in start] == ["start"]
    assert model.observe(
        _running("tariff", START + timedelta(seconds=10), transaction_id="tariff:a")
    ) == ()
    assert model.observe(
        _running("tariff", START + timedelta(seconds=20), transaction_id="tariff:b")
    ) == ()
    assert model.active is not None
    assert model.active.transaction_ids == ("tariff:a", "tariff:b")
    assert model.active.merged_replans == 1
    assert model.active.suppressed_duplicate_notifications == 2

    assert model.observe(
        _idle(
            START + timedelta(seconds=30),
            restored=True,
            transaction_id="tariff:b",
            reason="completed",
        )
    ) == ()
    split = model.observe(
        _running("tariff", START + timedelta(seconds=40), transaction_id="tariff:c")
    )
    assert [event.kind for event in split] == ["end", "start"]
    assert split[0].transaction_ids == ("tariff:a", "tariff:b")
    assert split[0].merged_replans == 1
    assert split[0].final_reason == "completed"
    assert split[1].transaction_ids == ("tariff:c",)
    return 12


def test_foreign_attempt_cannot_corrupt_completed_transaction() -> int:
    """A later unconfirmed try must not rewrite transaction A's outcome."""

    model = _initialized_model()
    start = START + timedelta(seconds=1)
    events = model.observe(
        _running(
            "tariff",
            start,
            start=start,
            end=START + timedelta(minutes=30),
            transaction_id="tariff:A",
        )
    )
    assert [event.kind for event in events] == ["start"]
    assert model.observe(
        _idle(
            START + timedelta(minutes=5),
            interrupted=True,
            transaction_id="tariff:A",
            reason="physical_flow_contradiction",
        )
    ) == ()
    ended = model.observe(
        _idle(
            START + timedelta(minutes=5, seconds=5),
            restored=True,
            transaction_id="tariff:A",
            reason="restore_confirmed",
        )
    )
    assert len(ended) == 1
    assert ended[0].outcome == "interrupted"
    assert ended[0].final_reason == "physical_flow_contradiction"
    assert ended[0].transaction_ids == ("tariff:A",)

    # B never reached confirmed execution and reports a mismatch while A is
    # still waiting for the 120-second grouping boundary.
    assert model.observe(
        _idle(
            START + timedelta(minutes=5, seconds=10),
            failed=True,
            transaction_id="tariff:B",
            reason="readback_mismatch",
        )
    ) == ()
    assert model.observe(
        _idle(
            START + timedelta(minutes=7, seconds=1),
            restored=True,
            transaction_id="tariff:B",
            reason="restore_confirmed",
        )
    ) == ()
    return 10


def test_command_uncertainty_survives_confirmed_restore() -> int:
    model = _initialized_model()
    assert model.observe(
        _running("rce", START + timedelta(seconds=1), transaction_id="rce:uncertain")
    )
    assert model.observe(_idle(
        START + timedelta(minutes=5),
        transaction_id="rce:uncertain",
        reason="command_outcome_unknown",
    )) == ()
    assert model.observe(_idle(
        START + timedelta(minutes=5, seconds=1),
        restored=True,
        transaction_id="rce:uncertain",
        reason="restore_confirmed",
    )) == ()
    ended = model.observe(_idle(
        START + timedelta(minutes=7, seconds=1),
        restored=True,
        transaction_id="rce:uncertain",
        reason="restore_confirmed",
    ))
    assert len(ended) == 1
    assert ended[0].outcome == "unconfirmed"
    assert ended[0].final_reason == "command_outcome_unknown"
    return 5


def test_command_uncertainty_persistence_and_correlated_resolution() -> int:
    model = _initialized_model()
    model.observe(_running(
        "tariff", START + timedelta(seconds=1), transaction_id="tariff:A",
    ))
    model.observe(_idle(
        START + timedelta(minutes=5), transaction_id="tariff:A",
        reason="command_outcome_unknown",
    ))
    assert model.active is not None
    restored = MODULE.ActiveRange.from_dict(model.active.as_dict())
    assert restored.operation_uncertain
    assert restored.operation_uncertain_transaction_id == "tariff:A"
    model.active = restored
    assert model.observe(_idle(
        START + timedelta(minutes=5, seconds=10),
        transaction_id="tariff:B", reason="restore_confirmed", restored=True,
    )) == ()
    assert model.active.operation_uncertain
    assert model.observe(_running(
        "tariff", START + timedelta(minutes=5, seconds=20),
        transaction_id="tariff:A",
    )) == ()
    assert model.active is not None and not model.active.operation_uncertain
    assert model.active.final_reason is None
    return 6


class _FakeEntry:
    entry_id = "notification-test-entry"

    def async_create_background_task(self, hass, coro, name):
        return hass.async_create_task(coro, name)


async def test_ha_delivery_path() -> int:
    from homeassistant import config_entries, loader
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er
    from homeassistant.util import dt as dt_util

    calls: list[dict] = []
    block_provider = False

    async def provider(call: ServiceCall) -> None:
        calls.append(dict(call.data))
        if block_provider:
            await asyncio.sleep(5)

    with tempfile.TemporaryDirectory(prefix="hoymiles-ems-notify-") as config:
        hass = HomeAssistant(config)
        loader.async_setup(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        hass.config.language = "pl"
        dt_util.set_default_time_zone(WARSAW)
        hass.states.async_set(
            "input_boolean.hoymiles_ems_push_notifications_enabled", "on"
        )
        hass.states.async_set(
            "input_text.hoymiles_ems_push_notify_target", "notify.test_phone"
        )
        hass.states.async_set("notify.test_phone", "unknown")
        hass.services.async_register("notify", "send_message", provider)
        await hass.async_start()

        manager = HoymilesEmsNotificationManager(hass, _FakeEntry())
        await manager.async_initialize()
        manager._model.observe(_idle(START))
        events = manager._model.observe(
            _running(
                "tariff",
                START + timedelta(seconds=1),
                end=START + timedelta(hours=1),
                transaction_id="tariff:delivery",
            )
        )
        await manager._async_accept(events, now=START + timedelta(seconds=1))
        await hass.async_block_till_done()
        assert len(calls) == 1
        assert calls[0]["title"] == "Hoymiles — Powiadomienia EMS"
        assert "Rozpoczęto ładowanie taryfowe" in calls[0]["message"]
        assert manager._deliveries[-1].state == "delivered"
        status = manager.delivery_status
        assert status["transaction_ids"] == ["tariff:delivery"]
        assert status["merged_replans"] == 0
        assert status["suppressed_duplicate_notifications"] == 0
        assert status["final_outcome"] == "executing"
        assert status["provider_states"][-1]["state"] == "delivered"
        revisions = (manager._state_revision, manager._persisted_revision)
        diagnostic = manager.diagnostic_snapshot()
        assert diagnostic["available"] and not diagnostic["phone_delivery_verified"]
        assert diagnostic["events"][-1]["event"]["transaction_ids"] == ["tariff:delivery"]
        diagnostic["events"][-1]["event"]["transaction_ids"].append("copy-only")
        assert "copy-only" not in str(manager.diagnostic_snapshot())
        assert (manager._state_revision, manager._persisted_revision) == revisions
        assert len(calls) == 1

        # Global OFF consumes the event and re-enabling does not replay it.
        hass.states.async_set(
            "input_boolean.hoymiles_ems_push_notifications_enabled", "off"
        )
        suppressed_model = _initialized_model(START + timedelta(hours=2))
        suppressed = suppressed_model.observe(
            _running("rce", START + timedelta(hours=2, seconds=1))
        )
        await manager._async_accept(suppressed, now=START + timedelta(hours=2))
        assert manager._deliveries[-1].state == "suppressed"
        hass.states.async_set(
            "input_boolean.hoymiles_ems_push_notifications_enabled", "on"
        )
        await hass.async_block_till_done()
        assert len(calls) == 1

        # A possible provider acceptance followed by timeout is terminal and
        # never retried automatically.
        block_provider = True
        old_timeout = MODULE._DELIVERY_TIMEOUT_SECONDS
        old_logger_disabled = MODULE._LOGGER.disabled
        MODULE._DELIVERY_TIMEOUT_SECONDS = 0.02
        MODULE._LOGGER.disabled = True
        try:
            timeout_model = _initialized_model(START + timedelta(hours=3))
            timed = timeout_model.observe(
                _running("rcm", START + timedelta(hours=3, seconds=1))
            )
            await manager._async_accept(timed, now=START + timedelta(hours=3))
            await hass.async_block_till_done()
        finally:
            MODULE._DELIVERY_TIMEOUT_SECONDS = old_timeout
            MODULE._LOGGER.disabled = old_logger_disabled
        assert len(calls) == 2
        assert manager._deliveries[-1].state == "unconfirmed"
        await asyncio.sleep(0.05)
        assert len(calls) == 2

        assert len(manager._deliveries) <= MODULE._MAX_RECENT_EVENTS
        manager.close()
        await hass.async_stop()
    return 15


async def test_persistence_revisions_and_idle_coalescing() -> int:
    """Persist durable changes once and never clear a newer in-flight revision."""

    from homeassistant import config_entries, loader
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    saved_payloads: list[dict] = []

    class CountingStore:
        async def async_save(self, payload) -> None:
            saved_payloads.append(payload)

    with tempfile.TemporaryDirectory(prefix="hoymiles-ems-notify-revisions-") as config:
        hass = HomeAssistant(config)
        loader.async_setup(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        hass.states.async_set(
            "input_boolean.hoymiles_ems_push_notifications_enabled", "off"
        )
        await hass.async_start()

        manager = HoymilesEmsNotificationManager(hass, _FakeEntry())
        manager._initialized = True
        manager._model = _initialized_model()
        manager._first_observation = False
        manager._store = CountingStore()

        # Covers the 28-frame audit reproduction and the stronger 100-frame gate.
        for index in range(100):
            manager._enqueue_observation(_idle(START + timedelta(seconds=index + 1)))
            manager._ensure_worker()
            await asyncio.wait_for(manager._worker_task, timeout=2)
        assert saved_payloads == []

        manager._enqueue_observation(_running("rce", START + timedelta(minutes=1)))
        manager._ensure_worker()
        await asyncio.wait_for(manager._worker_task, timeout=2)
        assert len(saved_payloads) == 1
        assert saved_payloads[-1]["active"]["policy"] == "rce"

        manager._enqueue_observation(
            _idle(
                START + timedelta(minutes=2),
                restored=True,
                interrupted=True,
            )
        )
        manager._enqueue_observation(
            _running("tariff", START + timedelta(minutes=3))
        )
        manager._ensure_worker()
        await asyncio.wait_for(manager._worker_task, timeout=2)
        assert [item.event.kind for item in manager._deliveries] == [
            "start",
            "end",
            "start",
        ]
        assert manager._deliveries[1].event.outcome == "interrupted"
        assert saved_payloads[-1]["active"]["policy"] == "tariff"

        class RestartStore:
            def __init__(self, payload) -> None:
                self.payload = payload
                self.saves: list[dict] = []

            async def async_load(self):
                return self.payload

            async def async_save(self, payload) -> None:
                self.saves.append(payload)

        restart_store = RestartStore(saved_payloads[-1])
        successor = HoymilesEmsNotificationManager(hass, _FakeEntry())
        successor._store = restart_store
        await successor.async_initialize()
        delivery_count = len(successor._deliveries)
        duplicate = successor._deliveries[-1].event
        await successor._async_accept(
            (duplicate,), now=START + timedelta(minutes=3, seconds=1)
        )
        assert len(successor._deliveries) == delivery_count
        assert restart_store.saves == []
        successor.close()

        entered = asyncio.Event()
        release = asyncio.Event()
        in_flight_payloads: list[dict] = []

        class BlockingStore:
            async def async_save(self, payload) -> None:
                in_flight_payloads.append(payload)
                if len(in_flight_payloads) == 1:
                    entered.set()
                    await release.wait()

        manager._store = BlockingStore()
        manager._model.observe(
            _idle(
                START + timedelta(minutes=4),
                restored=True,
                interrupted=True,
            )
        )
        manager._mark_state_changed()
        save_task = asyncio.create_task(manager._safe_save())
        await asyncio.wait_for(entered.wait(), timeout=1)
        manager._model.observe(_running("rcm", START + timedelta(minutes=5)))
        manager._mark_state_changed()
        release.set()
        assert await asyncio.wait_for(save_task, timeout=2)
        assert len(in_flight_payloads) == 2
        assert in_flight_payloads[0]["active"] is None
        assert in_flight_payloads[1]["active"]["policy"] == "rcm"
        assert manager._persisted_revision == manager._state_revision

        manager.close()
        await hass.async_stop()
    return 16


async def test_persistence_failure_retries_current_state() -> int:
    """A failed Store write remains dirty and recovery saves the newest state."""

    from homeassistant import config_entries, loader
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    class FlakyStore:
        def __init__(self) -> None:
            self.fail = True
            self.payloads: list[dict] = []

        async def async_save(self, payload) -> None:
            self.payloads.append(payload)
            if self.fail:
                raise OSError("simulated first Store failure")

    with tempfile.TemporaryDirectory(prefix="hoymiles-ems-notify-retry-") as config:
        hass = HomeAssistant(config)
        loader.async_setup(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        await hass.async_start()

        manager = HoymilesEmsNotificationManager(hass, _FakeEntry())
        manager._initialized = True
        manager._model = _initialized_model()
        manager._first_observation = False
        store = FlakyStore()
        manager._store = store

        manager._model.observe(_running("rce", START + timedelta(minutes=1)))
        manager._mark_state_changed()
        old_logger_disabled = MODULE._LOGGER.disabled
        MODULE._LOGGER.disabled = True
        try:
            assert not await manager._safe_save()
        finally:
            MODULE._LOGGER.disabled = old_logger_disabled
        assert manager._persisted_revision < manager._state_revision
        assert store.payloads[-1]["active"]["policy"] == "rce"

        manager._model.observe(
            _idle(
                START + timedelta(minutes=2),
                restored=True,
                interrupted=True,
            )
        )
        manager._mark_state_changed()
        manager._model.observe(_running("tariff", START + timedelta(minutes=3)))
        manager._mark_state_changed()
        store.fail = False
        assert await manager._safe_save()
        assert store.payloads[-1]["active"]["policy"] == "tariff"
        assert manager._persisted_revision == manager._state_revision

        manager.close()
        await hass.async_stop()
    return 8


async def test_bounded_worker_and_hung_storage() -> int:
    """A never-ending Store write cannot create unbounded notifier work."""

    from homeassistant import config_entries, loader
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    entered = asyncio.Event()
    release = asyncio.Event()
    saves = 0

    class HangingStore:
        async def async_save(self, _payload) -> None:
            nonlocal saves
            saves += 1
            entered.set()
            await release.wait()

    with tempfile.TemporaryDirectory(prefix="hoymiles-ems-notify-bounded-") as config:
        hass = HomeAssistant(config)
        loader.async_setup(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        hass.states.async_set(
            "input_boolean.hoymiles_ems_push_notifications_enabled", "off"
        )
        await hass.async_start()

        manager = HoymilesEmsNotificationManager(hass, _FakeEntry())
        manager._initialized = True
        manager._model = _initialized_model()
        manager._first_observation = False
        manager._store = HangingStore()
        status_updates: list[str | None] = []
        manager.attach_status_sink(status_updates.append)

        manager._enqueue_observation(_running("rce", START + timedelta(seconds=1)))
        manager._ensure_worker()
        await asyncio.wait_for(entered.wait(), timeout=1)
        worker = manager._worker_task
        assert worker is not None and not worker.done()

        # A flood while Store is blocked remains one worker and one bounded queue.
        for index in range(100):
            manager._enqueue_observation(
                _running("rce", START + timedelta(seconds=2 + index))
            )
            manager._ensure_worker()
        terminal = _idle(
            START + timedelta(minutes=10),
            restored=True,
            interrupted=True,
        )
        manager._enqueue_observation(terminal)
        manager._ensure_worker()
        assert manager._worker_task is worker
        assert len(manager._observation_queue) <= MODULE._MAX_QUEUED_OBSERVATIONS
        assert terminal in manager._observation_queue
        assert manager.delivery_status["dropped_observations"] > 0
        assert manager.delivery_status["dropped_equivalent_observations"] > 0
        assert manager.delivery_status["dropped_material_observations"] == 0
        assert manager.delivery_status["backpressure_active"]
        assert manager.delivery_status["guarantee"] == "best_effort_unconfirmed"
        assert "notification_queue_backpressure" in status_updates

        release.set()
        await asyncio.wait_for(worker, timeout=2)
        await asyncio.sleep(0)
        assert [item.event.kind for item in manager._deliveries] == ["start", "end"]
        assert manager._deliveries[-1].event.outcome == "interrupted"
        assert all(item.state == "suppressed" for item in manager._deliveries)
        assert saves >= 2
        assert manager._worker_task is None
        assert manager.delivery_status["reason"] is None
        assert manager.delivery_status["status"] == "available"
        assert not manager.delivery_status["backpressure_active"]
        assert manager.delivery_status["guarantee"] == "durable_before_provider_attempt"
        assert (
            manager.delivery_status["history_quality"]
            == "equivalent_observations_coalesced"
        )
        assert status_updates[-1] is None

        previous_total = manager.delivery_status["dropped_observations"]
        distinct = (
            _running("rce", START + timedelta(hours=1)),
            _idle(START + timedelta(hours=1, minutes=1), restored=True),
            _running("tariff", START + timedelta(hours=1, minutes=2)),
            _idle(START + timedelta(hours=1, minutes=3), failed=True),
            _running("rcm", START + timedelta(hours=1, minutes=4)),
            _idle(START + timedelta(hours=1, minutes=5), interrupted=True),
            _running("rce", START + timedelta(hours=1, minutes=6)),
            _idle(START + timedelta(hours=1, minutes=7), restored=True),
        )
        for observation in distinct:
            manager._enqueue_observation(observation)
        manager._enqueue_observation(
            _running("tariff", START + timedelta(hours=1, minutes=8))
        )
        assert manager.delivery_status["dropped_material_observations"] == 1
        assert manager.delivery_status["backpressure_active"]
        manager._ensure_worker()
        await asyncio.wait_for(manager._worker_task, timeout=2)
        assert manager.delivery_status["reason"] is None
        assert not manager.delivery_status["backpressure_active"]
        assert manager.delivery_status["dropped_observations"] == previous_total + 1
        assert manager.delivery_status["dropped_material_observations"] == 1
        assert manager.delivery_status["history_quality"] == "material_observation_loss"

        # Unload cancels the single consumer, bounds outstanding work, and a
        # fresh setup owns a different single worker rather than a duplicate.
        entered.clear()
        release.clear()
        manager._enqueue_observation(_running("tariff", START + timedelta(hours=1)))
        manager._ensure_worker()
        await asyncio.wait_for(entered.wait(), timeout=1)
        blocked_worker = manager._worker_task
        manager.close()
        assert manager.delivery_status["reason"] == "notification_shutdown_unconfirmed"
        assert len(manager._observation_queue) == 0
        release.set()
        if blocked_worker is not None:
            try:
                await blocked_worker
            except asyncio.CancelledError:
                pass

        successor = HoymilesEmsNotificationManager(hass, _FakeEntry())
        successor._initialized = True
        successor._model = _initialized_model(START + timedelta(hours=2))
        successor._first_observation = False
        successor._enqueue_observation(
            _running("rcm", START + timedelta(hours=2, seconds=1))
        )
        successor._ensure_worker()
        assert successor._worker_task is not None
        assert successor._worker_task is not blocked_worker
        await asyncio.wait_for(successor._worker_task, timeout=2)
        successor.close()
        await hass.async_stop()
    return 32


async def test_storage_timeout_failure_and_provider_cancel() -> int:
    """Ambiguous Store/provider outcomes stay bounded and unconfirmed."""

    from homeassistant import config_entries, loader
    from homeassistant.core import HomeAssistant, ServiceCall
    from homeassistant.helpers import device_registry as dr
    from homeassistant.helpers import entity_registry as er

    class HangingStore:
        async def async_save(self, _payload) -> None:
            await asyncio.Event().wait()

    class FailingStore:
        async def async_save(self, _payload) -> None:
            raise OSError("simulated ledger failure")

    provider_entered = asyncio.Event()
    provider_release = asyncio.Event()
    provider_calls = 0

    async def provider(_call: ServiceCall) -> None:
        nonlocal provider_calls
        provider_calls += 1
        provider_entered.set()
        await provider_release.wait()

    with tempfile.TemporaryDirectory(prefix="hoymiles-ems-notify-failures-") as config:
        hass = HomeAssistant(config)
        loader.async_setup(hass)
        hass.config_entries = config_entries.ConfigEntries(hass, {})
        hass.data[dr.DATA_REGISTRY] = dr.DeviceRegistry(hass)
        await dr.async_load(hass, load_empty=True)
        await er.async_load(hass, load_empty=True)
        hass.states.async_set(
            "input_boolean.hoymiles_ems_push_notifications_enabled", "on"
        )
        hass.states.async_set(
            "input_text.hoymiles_ems_push_notify_target", "notify.test_phone"
        )
        hass.states.async_set("notify.test_phone", "unknown")
        hass.services.async_register("notify", "send_message", provider)
        await hass.async_start()

        old_storage_timeout = MODULE._STORAGE_TIMEOUT_SECONDS
        old_logger_disabled = MODULE._LOGGER.disabled
        MODULE._STORAGE_TIMEOUT_SECONDS = 0.02
        MODULE._LOGGER.disabled = True
        try:
            timed = HoymilesEmsNotificationManager(hass, _FakeEntry())
            timed._initialized = True
            timed._model = _initialized_model()
            timed._first_observation = False
            timed._store = HangingStore()
            timed._enqueue_observation(
                _running("rce", START + timedelta(seconds=1), end=START + timedelta(hours=1))
            )
            timed._ensure_worker()
            await asyncio.wait_for(timed._worker_task, timeout=0.5)
            assert timed.delivery_status["reason"] == "notification_persistence_timeout"
            assert timed._deliveries[-1].state == "unconfirmed"
            assert provider_calls == 0

            failed = HoymilesEmsNotificationManager(hass, _FakeEntry())
            failed._initialized = True
            failed._model = _initialized_model(START + timedelta(hours=1))
            failed._first_observation = False
            failed._store = FailingStore()
            failed._enqueue_observation(
                _running("tariff", START + timedelta(hours=1, seconds=1), end=START + timedelta(hours=2))
            )
            failed._ensure_worker()
            await asyncio.wait_for(failed._worker_task, timeout=0.5)
            assert failed.delivery_status["reason"] == "notification_persistence_failed"
            assert failed._deliveries[-1].state == "unconfirmed"
            assert provider_calls == 0

            cancelled = HoymilesEmsNotificationManager(hass, _FakeEntry())
            cancelled._initialized = True
            cancelled._model = _initialized_model(START + timedelta(hours=2))
            cancelled._first_observation = False
            cancelled._enqueue_observation(
                _running("rcm", START + timedelta(hours=2, seconds=1))
            )
            cancelled._ensure_worker()
            await asyncio.wait_for(provider_entered.wait(), timeout=1)
            worker = cancelled._worker_task
            cancelled.close()
            assert cancelled.delivery_status["reason"] == "notification_shutdown_unconfirmed"
            if worker is not None:
                try:
                    await worker
                except asyncio.CancelledError:
                    pass
            assert cancelled._deliveries[-1].state == "unconfirmed"
            assert provider_calls == 1
            provider_release.set()
            await asyncio.sleep(0)
            assert provider_calls == 1
        finally:
            MODULE._STORAGE_TIMEOUT_SECONDS = old_storage_timeout
            MODULE._LOGGER.disabled = old_logger_disabled
            provider_release.set()
            for manager in (
                locals().get("timed"),
                locals().get("failed"),
                locals().get("cancelled"),
            ):
                if manager is not None:
                    manager.close()
            await hass.async_stop()
    return 12


def test_scheduler_notification_contract() -> int:
    source = (ROOT / "home_assistant" / "hoymiles_ems_scheduler.yaml").read_text(
        encoding="utf-8"
    )
    package = yaml.safe_load(source)
    automations = {item["id"]: item for item in package["automation"]}
    morning = automations["hoymiles_battery_balancing_morning_notification"]
    alarm = automations["hoymiles_ems_push_status_notification"]
    provider = package["script"][
        "hoymiles_battery_balancing_notification_provider_attempt"
    ]
    producer = package["script"]["hoymiles_notify_battery_balancing_lifecycle"]
    timeout = package["script"][
        "hoymiles_battery_balancing_notification_attempt_timeout"
    ]
    text = str(producer)
    assert "'started': 'ST'" not in text
    assert "'failed': 'AB'" in text and "'restore_failed': 'AB'" in text
    assert "PERMANENT_FAILURE" in str(timeout) and "PENDING" not in str(timeout)
    assert str(morning["triggers"][0]["at"]) == "07:00:00"
    assert "timedelta(days=interval_days)" in str(morning)
    assert "exact_terminal_fault_coverage" not in str(alarm)
    assert "alarm_eligible" in str(alarm) and "intentional_off_grid" in str(alarm)
    assert "notify.send_message" not in str(alarm)
    assert "input_boolean.hoymiles_ems_push_notifications_enabled" not in str(alarm)
    assert sum(
        1
        for step in provider["sequence"]
        if isinstance(step, dict) and step.get("action") == "notify.send_message"
    ) == 1
    assert "Id zdarzenia" not in str(provider)
    for forbidden in ("modbus.write_register", "verified_set_ems_mode"):
        assert forbidden not in str(morning)
        assert forbidden not in str(provider)
    notifier_source = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "ems_notifications.py"
    ).read_text(encoding="utf-8")
    supervisor_source = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "supervisor_sensor.py"
    ).read_text(encoding="utf-8")
    platform_source = (
        ROOT / "custom_components" / "hoymiles_hit_modbus" / "sensor.py"
    ).read_text(encoding="utf-8")
    assert "_delivery_tasks" not in notifier_source
    assert "_MAX_QUEUED_OBSERVATIONS" in notifier_source
    assert "def process_active_frame(" in notifier_source
    assert "await self._async_publish_notifications" not in supervisor_source
    assert "self._publish_notifications(controller.record, frame)" in supervisor_source
    assert "notifications.process_active_frame" in platform_source
    return 19


async def async_main() -> None:
    results = {
        "range_matrix": test_range_matrix(),
        "distinct_stop_bootstrap": test_distinct_ranges_stop_resume_and_bootstrap(),
        "late_restore_after_early_auth_loss": test_early_authorization_loss_with_late_restore(),
        "switch_dst_midnight": test_policy_switch_and_local_time_boundaries(),
        "continuous_hour": test_continuous_hour_has_only_range_boundaries(),
        "resume_reason": test_successful_resume_clears_transient_terminal_reason(),
        "logical_correlation": test_logical_range_correlation_and_real_cycle_split(),
        "foreign_transaction_scope": test_foreign_attempt_cannot_corrupt_completed_transaction(),
        "command_uncertainty": test_command_uncertainty_survives_confirmed_restore(),
        "uncertainty_persistence": test_command_uncertainty_persistence_and_correlated_resolution(),
        "ha_delivery": await test_ha_delivery_path(),
        "persistence_revisions": await test_persistence_revisions_and_idle_coalescing(),
        "persistence_retry": await test_persistence_failure_retries_current_state(),
        "bounded_worker": await test_bounded_worker_and_hung_storage(),
        "failure_paths": await test_storage_timeout_failure_and_provider_cancel(),
        "scheduler_contract": test_scheduler_notification_contract(),
    }
    print(
        "EMS notification tests passed: "
        + ", ".join(f"{name}={count}" for name, count in results.items())
        + f"; total={sum(results.values())}"
    )


if __name__ == "__main__":
    asyncio.run(async_main())
