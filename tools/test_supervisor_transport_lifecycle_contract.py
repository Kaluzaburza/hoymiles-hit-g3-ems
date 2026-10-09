"""Narrow HA-adapter safety contract for Supervisor transport and teardown."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from enum import Enum
import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
HARNESS_PATH = ROOT / "tools" / "test_supervisor_sensor_contract.py"


def _load_harness() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "supervisor_sensor_contract_harness",
        HARNESS_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load Supervisor sensor contract harness")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


H = _load_harness()
SENSOR = H.SENSOR
EXECUTOR = sys.modules[
    "custom_components.hoymiles_hit_modbus.supervisor_executor"
]


class Services:
    """Small mutable service registry with a complete call trace."""

    def __init__(self, names: set[str]) -> None:
        self.names = names
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def async_services(self) -> dict[str, dict[str, object]]:
        return {"esphome": {name: object() for name in self.names}}

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, object] | None:
        self.calls.append((domain, service, data))
        if kwargs.get("return_response") is True:
            if service.endswith("ems_supervisor_control_lease_challenge"):
                return {
                    "schema_version": 1,
                    "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                    "nonce": "00000001",
                }
            if service.endswith("ems_supervisor_write_complete_block_leased"):
                return {
                    "schema_version": 1,
                    "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                    "accepted": True,
                    "reason": "accepted",
                    "renew_nonce": "00000002",
                }
            if service.endswith("ems_supervisor_renew_control_lease"):
                return {
                    "schema_version": 1,
                    "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                    "accepted": True,
                    "reason": "renewed",
                    "renew_nonce": "00000003",
                }
            return {
                "schema_version": 1,
                "accepted": True,
                "reason": "accepted",
            }
        return None


def _install_source_entry(hass: Any, runtime: Any, *, node: str) -> None:
    source_entry = SimpleNamespace(
        entry_id="esphome-source",
        domain="esphome",
        data={"device_name": node},
    )
    runtime.source_device.config_entry_id = source_entry.entry_id
    hass.config_entries = SimpleNamespace(
        async_get_entry=lambda entry_id: (
            source_entry if entry_id == source_entry.entry_id else None
        )
    )


def _direct_writes() -> tuple[Any, ...]:
    block = EXECUTOR.EmsBlock(
        mode=EXECUTOR.EmsMode.SELF_USE,
        self_use_soc_percent_4301=20.0,
        backup_soc_percent_4302=80.0,
        force_charge_soc_percent_4303=90.0,
        maximum_charge_power_percent_4304=50.0,
        force_discharge_soc_percent_4305=20.0,
        maximum_discharge_power_percent_4306=50.0,
    )
    return (
        EXECUTOR.AtomicWrite(
            EXECUTOR.AtomicWriteFamily.EMS_COMPLETE_BLOCK,
            10,
            ems_block=block,
        ),
        EXECUTOR.AtomicWrite(
            EXECUTOR.AtomicWriteFamily.GCF_EXPORT_LIMIT,
            11,
            target_percent=0.0,
        ),
        EXECUTOR.AtomicWrite(
            EXECUTOR.AtomicWriteFamily.BATTERY_CHARGE_LIMIT,
            12,
            target_percent=40.0,
        ),
    )


async def _added_environment() -> tuple[Any, Any, Any, Any, Services]:
    hass, entry, runtime, sensor = H.environment()
    node = "source-node"
    _install_source_entry(hass, runtime, node=node)
    names = {
        f"{node.replace('-', '_')}_{family.value}"
        for family in EXECUTOR.AtomicWriteFamily
    }
    names.add("sibling_node_ems_supervisor_write_complete_block")
    names.update(
        {
            "source_node_ems_supervisor_control_lease_challenge",
            "source_node_ems_supervisor_write_complete_block_leased",
            "source_node_ems_supervisor_renew_control_lease",
        }
    )
    services = Services(names)
    hass.services = services
    await sensor.add_to_platform_finish()
    await asyncio.sleep(0)
    frame = sensor._latest_active_frame
    assert frame is not None
    sensor._latest_active_frame = replace(
        frame,
        execution=replace(
            frame.execution,
            force_charge_soc_percent=90.0,
            maximum_charge_power_percent=50.0,
            force_discharge_soc_percent=20.0,
            maximum_discharge_power_percent=50.0,
        ),
    )
    return hass, entry, runtime, sensor, services


def _ems_readback_frame(
    frame: Any,
    lease: Any,
    generation: int,
    *,
    mismatch: bool = False,
) -> Any:
    expected = lease.expected
    assert isinstance(expected, tuple)
    values = list(expected)
    if mismatch:
        values[3] += 5.0
    return replace(
        frame,
        execution=replace(
            frame.execution,
            physical_mode_code=values[0],
            self_use_soc_percent=values[1],
            backup_soc_percent=values[2],
            force_charge_soc_percent=values[3],
            maximum_charge_power_percent=values[4],
            force_discharge_soc_percent=values[5],
            maximum_discharge_power_percent=values[6],
            full_block_generation=generation,
            full_block_generation_at=H.NOW,
        ),
    )


async def test_manual_ems_full_block_lease_resolves_on_matching_new_generation() -> None:
    _hass, _entry, _runtime, sensor, _services = await _added_environment()
    frame = sensor._latest_active_frame
    assert frame is not None
    lease = SENSOR._manual_proxy_readback_lease(
        "force_charge_soc_4303",
        90.0,
        frame,
    )
    assert lease is not None
    assert lease.resolved(frame) is False
    matching = _ems_readback_frame(
        frame,
        lease,
        lease.base_generation + 1,
    )
    assert lease.resolved(matching) is True


async def test_manual_ems_full_block_normalizes_float32_readback_noise() -> None:
    _hass, _entry, _runtime, sensor, _services = await _added_environment()
    frame = sensor._latest_active_frame
    assert frame is not None
    noisy = replace(
        frame,
        execution=replace(
            frame.execution,
            maximum_discharge_power_percent=21.2000007629395,
        ),
    )
    lease = SENSOR._manual_proxy_readback_lease(
        "force_charge_soc_4303",
        90.0,
        noisy,
    )
    assert lease is not None
    assert isinstance(lease.expected, tuple)
    assert lease.expected[6] == 21.2
    write = SENSOR._manual_proxy_atomic_write(lease)
    assert write.ems_block is not None
    assert write.ems_block.maximum_discharge_power_percent_4306 == 21.2

    off_step = replace(
        frame,
        execution=replace(
            frame.execution,
            maximum_discharge_power_percent=21.21,
        ),
    )
    assert (
        SENSOR._manual_proxy_readback_lease(
            "force_charge_soc_4303",
            90.0,
            off_step,
        )
        is None
    )


async def test_manual_ems_full_block_lease_waits_for_second_coherent_generation_after_mismatch() -> None:
    _hass, _entry, _runtime, sensor, _services = await _added_environment()
    frame = sensor._latest_active_frame
    assert frame is not None
    lease = SENSOR._manual_proxy_readback_lease(
        "force_charge_soc_4303",
        90.0,
        frame,
    )
    assert lease is not None
    first_mismatch = _ems_readback_frame(
        frame,
        lease,
        lease.base_generation + 1,
        mismatch=True,
    )
    assert lease.resolved(first_mismatch) is False
    assert lease.first_mismatch_generation == lease.base_generation + 1
    second_coherent = _ems_readback_frame(
        frame,
        lease,
        lease.base_generation + 2,
        mismatch=True,
    )
    assert lease.resolved(second_coherent) is True


async def test_manual_ems_malformed_mode_is_never_command_or_ack_authority() -> None:
    hass, _entry, _runtime, sensor, services = await _added_environment()
    frame = sensor._latest_active_frame
    assert frame is not None
    valid = replace(
        frame,
        execution=replace(
            frame.execution,
            force_charge_soc_percent=90.0,
            maximum_charge_power_percent=50.0,
            maximum_discharge_power_percent=100.0,
        ),
    )
    sensor._latest_active_frame = valid
    lease = SENSOR._manual_proxy_readback_lease(
        "ems_mode_4300",
        "off_grid",
        valid,
    )
    assert lease is not None
    malformed_ack = _ems_readback_frame(
        valid,
        lease,
        lease.base_generation + 1,
    )
    malformed_ack = replace(
        malformed_ack,
        execution=replace(malformed_ack.execution, physical_mode_code=2.6),
    )
    assert lease.resolved(malformed_ack) is False
    assert lease.first_mismatch_generation is None

    requests = (
        ("ems_mode_4300", "self_use"),
        ("ems_complete_block_charge_rollback_command", 90 * 1001 + 500),
        ("self_used_soc_4301", 20.0),
        ("force_charge_soc_4303", 90.0),
        ("maximum_charge_power_4304", 50.0),
        ("force_discharge_soc_4305", 10.0),
        ("maximum_discharge_power_4306", 50.0),
    )
    for malformed_mode in (0.4, 2.6, 2.9999, 3.0001, 3.4, 1.0, 2.0):
        sensor._latest_active_frame = replace(
            valid,
            execution=replace(
                valid.execution,
                physical_mode_code=malformed_mode,
            ),
        )
        for source_id, value in requests:
            assert not await SENSOR.async_dispatch_manual_ems_proxy_write(
                hass,
                "entry-a",
                source_id,
                value,
            )
    assert services.calls == []


async def test_manual_gcf_259_lease_resolves_on_matching_new_generation() -> None:
    _hass, _entry, _runtime, sensor, _services = await _added_environment()
    frame = sensor._latest_active_frame
    assert frame is not None
    lease = SENSOR._manual_proxy_readback_lease(
        "gcf_export_soft_limit_ratio_259",
        37.0,
        frame,
    )
    assert lease is not None
    matching = replace(
        frame,
        execution=replace(
            frame.execution,
            effective_export_limit_percent=37.0,
            gcf_generation=lease.base_generation + 1,
            gcf_generation_at=H.NOW,
            gcf_cohort_coherent=True,
        ),
    )
    assert lease.resolved(matching) is True


async def test_manual_battery_306_lease_resolves_on_matching_new_generation() -> None:
    _hass, _entry, _runtime, sensor, _services = await _added_environment()
    frame = sensor._latest_active_frame
    assert frame is not None
    lease = SENSOR._manual_proxy_readback_lease(
        "battery_max_charge_power_306",
        60.0,
        frame,
    )
    assert lease is not None
    matching = replace(
        frame,
        execution=replace(
            frame.execution,
            battery_charge_limit_percent=60.0,
            battery_charge_limit_generation=lease.base_generation + 1,
            battery_charge_limit_generation_at=H.NOW,
        ),
    )
    assert lease.resolved(matching) is True


async def test_source_bound_transport_and_second_guard() -> None:
    hass, _entry, runtime, sensor, services = await _added_environment()
    writes = _direct_writes()
    assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is True
    hass.states.values[
        "input_select.hoymiles_ems_supervisor_mode"
    ] = H.FakeState("Active")
    assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is False
    for write in writes:
        await sensor._async_dispatch_atomic_write(write)
    assert [call[1] for call in services.calls] == [
        "source_node_ems_supervisor_write_complete_block",
        "source_node_ems_supervisor_write_gcf_export_limit",
        "source_node_ems_supervisor_write_battery_charge_limit",
    ]
    assert all(call[0] == "esphome" for call in services.calls)

    services.calls.clear()
    services.names.remove("source_node_ems_supervisor_write_complete_block")
    try:
        await sensor._async_dispatch_atomic_write(writes[0])
    except RuntimeError as err:
        assert "source ESPHome action" in str(err)
    else:
        raise AssertionError("a sibling ESPHome node received the source write")
    assert services.calls == []
    services.names.add("source_node_ems_supervisor_write_complete_block")

    original = sensor._esphome_action_service
    resolutions = 0

    def insert_second_entry(family: Any) -> str:
        nonlocal resolutions
        service = original(family)
        resolutions += 1
        if resolutions == 1:
            hass.data[H.DOMAIN]["entry-b"] = H.FakeRuntimeData(
                runtime.source_device,
                {},
            )
        return service

    sensor._esphome_action_service = insert_second_entry
    try:
        await sensor._async_dispatch_atomic_write(writes[1])
    except RuntimeError as err:
        assert "exactly one loaded instance" in str(err)
    else:
        raise AssertionError("cardinality changed between resolution and dispatch")
    assert services.calls == []


async def test_forced_block_requires_correlated_transaction_after_challenge() -> None:
    _hass, _entry, _runtime, sensor, services = await _added_environment()
    services.calls.clear()
    block = EXECUTOR.EmsBlock(
        mode=EXECUTOR.EmsMode.GRID_CHARGE,
        self_use_soc_percent_4301=20.0,
        backup_soc_percent_4302=80.0,
        force_charge_soc_percent_4303=90.0,
        maximum_charge_power_percent_4304=5.0,
        force_discharge_soc_percent_4305=20.0,
        maximum_discharge_power_percent_4306=0.0,
    )
    try:
        await sensor._async_dispatch_atomic_write(
            EXECUTOR.AtomicWrite(
                EXECUTOR.AtomicWriteFamily.EMS_COMPLETE_BLOCK,
                10,
                ems_block=block,
            )
        )
    except EXECUTOR.AtomicWriteNotQueued as err:
        assert str(err) == "retarget_not_authorized"
    else:
        raise AssertionError("uncorrelated forced block reached leased transport")
    assert [call[1] for call in services.calls] == [
        "source_node_ems_supervisor_control_lease_challenge",
    ]
    assert sensor._control_lease_client.handle is None


def _arm_sensor_lease(sensor: Any, *, transaction_id: str, block: Any) -> Any:
    loop_now = asyncio.get_running_loop().time()
    request = sensor._control_lease_client.prepare_arm(
        transaction_id=transaction_id,
        hard_deadline=H.NOW + H.timedelta(minutes=20),
        block=SENSOR._control_lease_block(block),
        challenge_nonce="00000001",
        now_wall=H.NOW,
        now_monotonic=loop_now - 26.0,
    )
    assert sensor._control_lease_client.accept_arm(
        request,
        {
            "schema_version": 1,
            "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
            "accepted": True,
            "reason": "accepted",
            "renew_nonce": "00000002",
        },
    ) is True
    return sensor._control_lease_client.handle


async def test_due_lease_renewal_replaces_waiting_unauthorized_timer() -> None:
    hass, _entry, _runtime, sensor, _services = await _added_environment()
    block = EXECUTOR.EmsBlock(
        mode=EXECUTOR.EmsMode.GRID_CHARGE,
        self_use_soc_percent_4301=25.0,
        backup_soc_percent_4302=90.0,
        force_charge_soc_percent_4303=45.0,
        maximum_charge_power_percent_4304=5.0,
        force_discharge_soc_percent_4305=0.0,
        maximum_discharge_power_percent_4306=100.0,
    )
    lease = _arm_sensor_lease(sensor, transaction_id="tariff:late-proof", block=block)
    assert lease is not None
    lease.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
    sensor._control_lease_renewal_evidence = lambda: None
    sensor._sync_control_lease()
    waiting = hass.active_delays()[-1]
    assert waiting.when == SENSOR.CONTROL_LEASE_RENEW_SECONDS
    sensor._control_lease_renewal_evidence = lambda: (11, lease.block)
    sensor._sync_control_lease()
    assert waiting.cancelled is True
    due = hass.active_delays()[-1]
    assert due is not waiting and due.when == 0.0
    sensor._cancel_control_lease_callback()


def test_rce_lease_direction_gate_supports_single_and_parallel_full_block() -> None:
    """RCE renewal follows the full-block topology, not direct register 259."""

    standalone = SimpleNamespace(
        charge_direction_ready=False,
        discharge_direction_ready=True,
        direct_259_ready=True,
    )
    parallel = SimpleNamespace(
        charge_direction_ready=False,
        discharge_direction_ready=True,
        direct_259_ready=False,
    )
    assert SENSOR._control_lease_direction_ready(
        owner=EXECUTOR.ExecutionOwner.RCE,
        action=EXECUTOR.ExecutionAction.RCE_EXPORT,
        mode=EXECUTOR.EmsMode.GRID_DISCHARGE,
        gates=standalone,
    ) is True
    assert SENSOR._control_lease_direction_ready(
        owner=EXECUTOR.ExecutionOwner.RCE,
        action=EXECUTOR.ExecutionAction.RCE_EXPORT,
        mode=EXECUTOR.EmsMode.GRID_DISCHARGE,
        gates=parallel,
    ) is True
    assert SENSOR._control_lease_direction_ready(
        owner=EXECUTOR.ExecutionOwner.RCM,
        action=EXECUTOR.ExecutionAction.RCM_PRE_DISCHARGE,
        mode=EXECUTOR.EmsMode.GRID_DISCHARGE,
        gates=parallel,
    ) is False


async def test_renewal_transport_sequences_and_expiry_recovery() -> None:
    _hass, _entry, _runtime, sensor, services = await _added_environment()
    block = EXECUTOR.EmsBlock(
        mode=EXECUTOR.EmsMode.GRID_CHARGE,
        self_use_soc_percent_4301=25.0,
        backup_soc_percent_4302=90.0,
        force_charge_soc_percent_4303=45.0,
        maximum_charge_power_percent_4304=5.0,
        force_discharge_soc_percent_4305=0.0,
        maximum_discharge_power_percent_4306=100.0,
    )
    lease = _arm_sensor_lease(sensor, transaction_id="tariff:sequence", block=block)
    assert lease is not None
    sensor._control_lease_renewal_evidence = lambda: None
    services.calls.clear()
    lease.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
    await sensor._async_renew_control_lease()
    assert services.calls == []
    assert sensor._control_lease_client.handle is lease
    sensor._cancel_control_lease_callback()

    sensor._control_lease_renewal_evidence = lambda: (11, lease.block)
    for expected_sequence in (1, 2):
        lease.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
        await sensor._async_renew_control_lease()
        assert lease.sequence == expected_sequence
        assert lease.pending_sequence is None
        sensor._cancel_control_lease_callback()
    renew_calls = [
        call for call in services.calls
        if call[1].endswith("ems_supervisor_renew_control_lease")
    ]
    assert [call[2]["sequence"] for call in renew_calls] == [1, 2]

    original_call = services.async_call

    async def reject_expired(
        domain: str,
        service: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> dict[str, object] | None:
        if service.endswith("ems_supervisor_renew_control_lease"):
            services.calls.append((domain, service, data))
            return {
                "schema_version": 1,
                "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                "accepted": False,
                "reason": "lease_expired",
            }
        return await original_call(domain, service, data, **kwargs)

    services.async_call = reject_expired
    lease.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
    await sensor._async_renew_control_lease()
    assert sensor._control_lease_client.handle is None
    sensor._cancel_control_lease_callback()

    services.async_call = original_call
    try:
        await sensor._async_dispatch_leased_ems(
            EXECUTOR.AtomicWrite(
                EXECUTOR.AtomicWriteFamily.EMS_COMPLETE_BLOCK,
                12,
                ems_block=block,
            ),
            block,
        )
    except EXECUTOR.AtomicWriteNotQueued as err:
        assert str(err) == "retarget_not_authorized"
    else:
        raise AssertionError("lease was rearmed without a live transaction")
    assert sensor._control_lease_client.handle is None
    assert not any(
        call[1].endswith("ems_supervisor_write_complete_block_leased")
        for call in services.calls
    )
    sensor._cancel_control_lease_callback()


async def test_renewal_is_single_flight_and_task_cleanup_is_identity_owned() -> None:
    hass, _entry, _runtime, sensor, services = await _added_environment()
    hass.async_create_task = lambda coro, _name: asyncio.create_task(coro)
    block = EXECUTOR.EmsBlock(
        mode=EXECUTOR.EmsMode.GRID_CHARGE,
        self_use_soc_percent_4301=25.0,
        backup_soc_percent_4302=90.0,
        force_charge_soc_percent_4303=45.0,
        maximum_charge_power_percent_4304=5.0,
        force_discharge_soc_percent_4305=0.0,
        maximum_discharge_power_percent_4306=100.0,
    )
    lease = _arm_sensor_lease(sensor, transaction_id="tariff:single", block=block)
    sensor._control_lease_renewal_evidence = lambda: (11, lease.block)
    lease.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
    pending_response: asyncio.Future[dict[str, object]] = (
        asyncio.get_running_loop().create_future()
    )

    async def delayed_response(
        domain: str,
        service: str,
        data: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, object]:
        services.calls.append((domain, service, dict(data)))
        return await pending_response

    services.calls.clear()
    services.async_call = delayed_response
    sensor._sync_control_lease()
    hass.active_delays()[-1].run()
    await asyncio.sleep(0)
    owner = sensor._control_lease_task
    assert owner is not None and not owner.done()
    for _publication in range(5):
        sensor._sync_control_lease()
    assert sensor._control_lease_task is owner
    assert len(services.calls) == 1
    assert hass.active_delays() == []

    # A non-owner completion callback cannot erase the actual in-flight task.
    unrelated = asyncio.create_task(asyncio.sleep(0))
    await unrelated
    sensor._control_lease_renewal_done(unrelated)
    assert sensor._control_lease_task is owner

    pending_response.set_result(
        {
            "schema_version": 1,
            "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
            "accepted": True,
            "reason": "renewed",
            "renew_nonce": "00000003",
        }
    )
    await owner
    await asyncio.sleep(0)
    assert sensor._control_lease_task is None
    assert lease.sequence == 1
    assert len(services.calls) == 1
    assert len(hass.active_delays()) == 1
    sensor._cancel_control_lease_callback()


async def test_snapshot_generation_retry_is_bounded_and_revalidated() -> None:
    _hass, _entry, _runtime, sensor, services = await _added_environment()
    block = EXECUTOR.EmsBlock(
        mode=EXECUTOR.EmsMode.GRID_CHARGE,
        self_use_soc_percent_4301=25.0,
        backup_soc_percent_4302=90.0,
        force_charge_soc_percent_4303=45.0,
        maximum_charge_power_percent_4304=5.0,
        force_discharge_soc_percent_4305=0.0,
        maximum_discharge_power_percent_4306=100.0,
    )
    lease = _arm_sensor_lease(sensor, transaction_id="tariff:generation", block=block)
    generations = iter(((11, lease.block), (12, lease.block)))
    sensor._control_lease_renewal_evidence = lambda: next(generations)
    lease.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
    responses = iter(
        (
            {
                "schema_version": 1,
                "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                "accepted": False,
                "reason": "snapshot_generation_advanced",
            },
            {
                "schema_version": 1,
                "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
                "accepted": True,
                "reason": "renewed",
                "renew_nonce": "00000003",
            },
        )
    )

    async def advancing_response(
        domain: str,
        service: str,
        data: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, object]:
        services.calls.append((domain, service, dict(data)))
        return next(responses)

    services.calls.clear()
    services.async_call = advancing_response
    await sensor._async_renew_control_lease()
    assert len(services.calls) == 2
    assert [call[2]["snapshot_generation"] for call in services.calls] == [11, 12]
    assert [call[2]["sequence"] for call in services.calls] == [1, 1]
    assert [call[2]["command_generation"] for call in services.calls] == [1, 1]
    assert lease.sequence == 1 and lease.pending_sequence is None
    assert sensor._control_lease_last_result["status"] == "accepted"

    # A real block mismatch cannot use the generation-only retry path.
    sensor._control_lease_client.invalidate()
    second = _arm_sensor_lease(sensor, transaction_id="tariff:generation", block=block)
    second.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
    evidence = iter(((21, second.block), None))
    sensor._control_lease_renewal_evidence = lambda: next(evidence)

    async def stale_then_stop(
        domain: str,
        service: str,
        data: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, object]:
        services.calls.append((domain, service, dict(data)))
        return {
            "schema_version": 1,
            "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
            "accepted": False,
            "reason": "snapshot_generation_advanced",
        }

    services.calls.clear()
    services.async_call = stale_then_stop
    await sensor._async_renew_control_lease()
    assert len(services.calls) == 1
    assert sensor._control_lease_client.handle is None
    assert sensor._control_lease_last_result["status"] == "rejected"
    assert sensor._control_lease_last_result["reason"] == "snapshot_generation_advanced"


async def test_late_ack_and_stale_callback_cannot_touch_newer_lease() -> None:
    hass, _entry, _runtime, sensor, services = await _added_environment()
    hass.async_create_task = lambda coro, _name: asyncio.create_task(coro)
    block = EXECUTOR.EmsBlock(
        mode=EXECUTOR.EmsMode.GRID_CHARGE,
        self_use_soc_percent_4301=25.0,
        backup_soc_percent_4302=90.0,
        force_charge_soc_percent_4303=45.0,
        maximum_charge_power_percent_4304=5.0,
        force_discharge_soc_percent_4305=0.0,
        maximum_discharge_power_percent_4306=100.0,
    )
    old = _arm_sensor_lease(sensor, transaction_id="tariff:old", block=block)
    sensor._control_lease_renewal_evidence = lambda: (11, old.block)
    old.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
    delayed: asyncio.Future[dict[str, object]] = (
        asyncio.get_running_loop().create_future()
    )

    async def delayed_response(
        domain: str,
        service: str,
        data: dict[str, Any],
        **_kwargs: Any,
    ) -> dict[str, object]:
        services.calls.append((domain, service, dict(data)))
        return await delayed

    services.calls.clear()
    services.async_call = delayed_response
    sensor._sync_control_lease()
    hass.active_delays()[-1].run()
    await asyncio.sleep(0)
    old_task = sensor._control_lease_task
    assert old_task is not None and len(services.calls) == 1

    sensor._control_lease_client.invalidate()
    newer = _arm_sensor_lease(sensor, transaction_id="tariff:new", block=block)
    sensor._control_lease_renewal_evidence = lambda: (12, newer.block)
    delayed.set_result(
        {
            "schema_version": 1,
            "protocol_version": 2, "soft_remaining_ms": 120000, "maximum_lease_seconds": 120, "renew_interval_seconds": 20,
            "accepted": True,
            "reason": "renewed",
            "renew_nonce": "00000009",
        }
    )
    await old_task
    await asyncio.sleep(0)
    assert sensor._control_lease_client.handle is newer
    assert newer.sequence == 0 and newer.pending_sequence is None
    assert sensor._control_lease_last_result["status"] == "ignored_stale_response"
    assert sensor._control_lease_error is None
    assert len(services.calls) == 1
    sensor._cancel_control_lease_callback()

    # A timer captured for the old identity may only reschedule the new one.
    newer.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
    sensor._sync_control_lease()
    stale_timer = hass.active_delays()[-1]
    sensor._control_lease_client.invalidate()
    newest = _arm_sensor_lease(sensor, transaction_id="tariff:newest", block=block)
    sensor._control_lease_renewal_evidence = lambda: (13, newest.block)
    stale_timer.run()
    assert sensor._control_lease_task is None
    assert len(services.calls) == 1
    assert sensor._control_lease_client.handle is newest
    assert len(hass.active_delays()) == 1
    sensor._cancel_control_lease_callback()


async def test_renew_timeout_and_unload_preserve_fail_closed_ownership() -> None:
    hass, _entry, _runtime, sensor, services = await _added_environment()
    hass.async_create_task = lambda coro, _name: asyncio.create_task(coro)
    block = EXECUTOR.EmsBlock(
        mode=EXECUTOR.EmsMode.GRID_CHARGE,
        self_use_soc_percent_4301=25.0,
        backup_soc_percent_4302=90.0,
        force_charge_soc_percent_4303=45.0,
        maximum_charge_power_percent_4304=5.0,
        force_discharge_soc_percent_4305=0.0,
        maximum_discharge_power_percent_4306=100.0,
    )
    lease = _arm_sensor_lease(sensor, transaction_id="tariff:timeout", block=block)
    sensor._control_lease_renewal_evidence = lambda: (11, lease.block)
    lease.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1

    async def timeout(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise TimeoutError("controlled timeout")

    services.async_call = timeout
    await sensor._async_renew_control_lease()
    assert sensor._control_lease_client.handle is lease
    assert lease.pending_sequence == 1 and lease.sequence == 0
    assert sensor._control_lease_last_result["status"] == "unavailable"

    lease.next_renew_monotonic = asyncio.get_running_loop().time() - 0.1
    never: asyncio.Future[dict[str, object]] = asyncio.get_running_loop().create_future()
    services.async_call = lambda *_args, **_kwargs: never
    sensor._sync_control_lease()
    hass.active_delays()[-1].run()
    await asyncio.sleep(0)
    task = sensor._control_lease_task
    assert task is not None and not task.done()
    sensor._cleanup_lifecycle()
    await asyncio.sleep(0)
    assert task.cancelled()
    assert sensor._control_lease_task is None
    assert sensor._control_lease_client.handle is None
    assert hass.active_delays() == []


async def test_confirmed_terminal_record_clears_only_its_correlated_lease() -> None:
    hass, _entry, _runtime, sensor, _services = await _added_environment()
    block = EXECUTOR.EmsBlock(
        mode=EXECUTOR.EmsMode.GRID_CHARGE,
        self_use_soc_percent_4301=25.0,
        backup_soc_percent_4302=90.0,
        force_charge_soc_percent_4303=45.0,
        maximum_charge_power_percent_4304=5.0,
        force_discharge_soc_percent_4305=0.0,
        maximum_discharge_power_percent_4306=100.0,
    )
    old = _arm_sensor_lease(sensor, transaction_id="tariff:old", block=block)
    assert old is not None
    sensor._control_lease_renewal_evidence = lambda: None
    sensor._sync_control_lease()
    waiting = hass.active_delays()[-1]
    sensor._publish_composite_state = lambda: None
    sensor._schedule_master_stop_continuation_callback = lambda: None
    sensor._controller = None
    old_terminal = SimpleNamespace(
        state=EXECUTOR.ActiveState.IDLE,
        transaction=None,
        last_transaction=SimpleNamespace(
            transaction_id="tariff:old",
            owner=EXECUTOR.ExecutionOwner.NONE,
            rollback_status=EXECUTOR.RollbackStatus.CONFIRMED,
        ),
    )
    sensor._executor_record_updated(old_terminal)
    assert sensor._control_lease_client.handle is None
    assert waiting.cancelled is True

    newer = _arm_sensor_lease(sensor, transaction_id="tariff:new", block=block)
    assert newer is not None
    sensor._executor_record_updated(old_terminal)
    assert sensor._control_lease_client.handle is newer
    pending_new = SimpleNamespace(
        state=EXECUTOR.ActiveState.IDLE,
        transaction=None,
        last_transaction=SimpleNamespace(
            transaction_id="tariff:new",
            owner=EXECUTOR.ExecutionOwner.NONE,
            rollback_status=EXECUTOR.RollbackStatus.PENDING,
        ),
    )
    sensor._executor_record_updated(pending_new)
    assert sensor._control_lease_client.handle is newer


async def test_retarget_rejection_is_known_before_forced_transport() -> None:
    _hass, _entry, _runtime, sensor, services = await _added_environment()
    block = EXECUTOR.EmsBlock(
        mode=EXECUTOR.EmsMode.GRID_CHARGE,
        self_use_soc_percent_4301=25.0,
        backup_soc_percent_4302=90.0,
        force_charge_soc_percent_4303=45.0,
        maximum_charge_power_percent_4304=5.0,
        force_discharge_soc_percent_4305=0.0,
        maximum_discharge_power_percent_4306=100.0,
    )
    _arm_sensor_lease(sensor, transaction_id="tariff:old", block=block)
    services.calls.clear()
    try:
        await sensor._async_dispatch_leased_ems(
            EXECUTOR.AtomicWrite(
                EXECUTOR.AtomicWriteFamily.EMS_COMPLETE_BLOCK,
                10,
                ems_block=block,
            ),
            block,
        )
    except EXECUTOR.AtomicWriteNotQueued as err:
        assert err.reason == "retarget_not_authorized"
    else:
        raise AssertionError("known pre-transport retarget was not classified")
    assert [call[1] for call in services.calls] == [
        "source_node_ems_supervisor_control_lease_challenge"
    ]


async def test_manual_proxy_authority_is_exact_safe_off_only() -> None:
    hass, entry, runtime, sensor, services = await _added_environment()
    helper_id = "input_select.hoymiles_ems_supervisor_mode"
    assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is True

    for unsafe_state in ("Active", "unknown", "unavailable"):
        hass.states.values[helper_id] = H.FakeState(unsafe_state)
        assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is False
    hass.states.values[helper_id] = H.FakeState("Off")

    original_frame = sensor._latest_active_frame
    assert original_frame is not None
    sensor._latest_active_frame = replace(
        original_frame,
        decision=replace(
            original_frame.decision,
            supervisor_mode=H.CORE.SupervisorMode.ACTIVE,
        ),
    )
    assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is False
    sensor._latest_active_frame = replace(
        original_frame,
        context=replace(original_frame.context, owner_conflict=True),
    )
    assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is False
    for legacy_owner in (
        H.CORE.OwnerKind.NONE,
        H.CORE.OwnerKind.MANUAL,
        H.CORE.OwnerKind.BALANCING,
        H.CORE.OwnerKind.RCE,
        H.CORE.OwnerKind.TARIFF,
        H.CORE.OwnerKind.RCM,
    ):
        sensor._latest_active_frame = replace(
            original_frame,
            context=replace(
                original_frame.context,
                owner_kind=legacy_owner,
                owner_conflict=False,
            ),
        )
        assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is True
    for unsafe_owner in (
        H.CORE.OwnerKind.FOREIGN,
        H.CORE.OwnerKind.UNKNOWN,
    ):
        sensor._latest_active_frame = replace(
            original_frame,
            context=replace(
                original_frame.context,
                owner_kind=unsafe_owner,
                owner_conflict=False,
            ),
        )
        assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is False
    sensor._latest_active_frame = original_frame

    controller = sensor._controller
    assert controller is not None
    original_record = controller.record
    sensor._controller = SimpleNamespace(
        record=replace(
            original_record,
            state=EXECUTOR.ActiveState.RESTORING,
        )
    )
    assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is False
    sensor._controller = controller
    sensor._master_stop_latched = True
    assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is False
    sensor._master_stop_latched = False

    guard = hass.data.pop(SENSOR._GUARD_KEY)
    assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is False
    hass.data[SENSOR._GUARD_KEY] = guard
    hass.data[H.DOMAIN]["entry-b"] = H.FakeRuntimeData(runtime.source_device, {})
    assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is False

    select_source = (
        H.COMPONENT / "select.py"
    ).read_text(encoding="utf-8")
    number_source = (
        H.COMPONENT / "number.py"
    ).read_text(encoding="utf-8")
    assert 'self._catalog.get("source_id") == "ems_mode_4300"' in select_source
    assert 'option != "off_grid"' not in select_source
    assert "manual_ems_proxy_write_allowed" not in select_source
    assert "await async_dispatch_manual_ems_proxy_write(" in select_source
    for source_id in (
        "battery_max_charge_power_306",
        "ems_complete_block_charge_rollback_command",
        "gcf_export_soft_limit_ratio_259",
        "self_used_soc_4301",
        "force_charge_soc_4303",
        "maximum_charge_power_4304",
        "force_discharge_soc_4305",
        "maximum_discharge_power_4306",
    ):
        assert source_id in number_source
    assert "manual_ems_proxy_write_allowed" not in number_source
    assert "await async_dispatch_manual_ems_proxy_write(" in number_source

    class FakeSelectEntity:
        pass

    class FakeNumberEntity:
        pass

    class FakeNumberMode(str, Enum):
        AUTO = "auto"

    H._module(
        "homeassistant.components.select",
        ATTR_OPTION="option",
        DOMAIN="select",
        SERVICE_SELECT_OPTION="select_option",
        SelectEntity=FakeSelectEntity,
    )
    H._module(
        "homeassistant.components.number",
        ATTR_VALUE="value",
        DOMAIN="number",
        SERVICE_SET_VALUE="set_value",
        NumberEntity=FakeNumberEntity,
        NumberMode=FakeNumberMode,
    )
    sys.modules["homeassistant.const"].ATTR_ENTITY_ID = "entity_id"
    select_module = H._load(
        "custom_components.hoymiles_hit_modbus.select_authority_under_test",
        H.COMPONENT / "select.py",
    )
    number_module = H._load(
        "custom_components.hoymiles_hit_modbus.number_authority_under_test",
        H.COMPONENT / "number.py",
    )
    error_type = sys.modules["homeassistant.exceptions"].HomeAssistantError

    def proxy_matched(source_id: str, translation_key: str) -> Any:
        return H.FakeMatchedEntity(
            catalog={
                "translation_key": translation_key,
                "source_id": source_id,
                "options": [
                    {"key": "self_use", "raw": "Self-Use"},
                    {"key": "off_grid", "raw": "Off-Grid"},
                ],
            },
            source=SimpleNamespace(entity_id=f"number.native_{source_id}"),
        )

    def make_proxy(proxy_type: type[Any], source_id: str, translation_key: str) -> Any:
        proxy = proxy_type(
            hass,
            entry,
            runtime,
            proxy_matched(source_id, translation_key),
        )
        proxy._source_entity_id = f"native.{source_id}"
        return proxy

    hass.data[H.DOMAIN].pop("entry-b")
    hass.states.values[helper_id] = H.FakeState("Active")
    services.calls.clear()
    mode_proxy = make_proxy(
        select_module.HoymilesSelect,
        "ems_mode_4300",
        "ems_mode",
    )
    try:
        await mode_proxy.async_select_option("self_use")
    except error_type:
        pass
    else:
        raise AssertionError("Active allowed the EMS mode proxy writer")
    assert services.calls == []
    try:
        await mode_proxy.async_select_option("off_grid")
    except error_type:
        pass
    else:
        raise AssertionError("Active allowed a racing Off-Grid proxy writer")
    assert services.calls == []

    services.calls.clear()
    controlled_numbers = (
        ("battery_max_charge_power_306", "battery_max_charge_power"),
        ("ems_complete_block_charge_rollback_command", "ems_complete_block_charge_rollback_command"),
        ("gcf_export_soft_limit_ratio_259", "maximum_export_power_limit"),
        ("self_used_soc_4301", "self_use_soc"),
        ("force_charge_soc_4303", "force_charge_soc"),
        ("maximum_charge_power_4304", "maximum_charge_power"),
        ("force_discharge_soc_4305", "force_discharge_soc"),
        ("maximum_discharge_power_4306", "maximum_discharge_power"),
    )
    for source_id, translation_key in controlled_numbers:
        proxy = make_proxy(number_module.HoymilesNumber, source_id, translation_key)
        try:
            await proxy.async_set_native_value(50.0)
        except error_type:
            pass
        else:
            raise AssertionError(f"Active allowed the {source_id} proxy writer")
    assert services.calls == []

    unrelated = make_proxy(
        number_module.HoymilesNumber,
        "replenish_power_310",
        "low_soc_grid_charge_power",
    )
    await unrelated.async_set_native_value(40.0)
    assert services.calls[-1][0:2] == ("number", "set_value")

    hass.states.values[helper_id] = H.FakeState("Off")
    services.calls.clear()
    safe_frame = sensor._latest_active_frame
    assert safe_frame is not None
    rcm_owned_frame = replace(
        safe_frame,
        context=replace(
            safe_frame.context,
            owner_kind=H.CORE.OwnerKind.RCM,
            owner_conflict=False,
        ),
    )
    sensor._latest_active_frame = rcm_owned_frame
    rcm_battery_proxy = make_proxy(
        number_module.HoymilesNumber,
        "battery_max_charge_power_306",
        "battery_max_charge_power",
    )
    await rcm_battery_proxy.async_set_native_value(60.0)
    assert [call[0:2] for call in services.calls] == [
        ("esphome", "source_node_ems_supervisor_write_battery_charge_limit")
    ]
    sensor._latest_active_frame = replace(
        rcm_owned_frame,
        execution=replace(
            rcm_owned_frame.execution,
            battery_charge_limit_percent=60.0,
            battery_charge_limit_generation=int(
                rcm_owned_frame.execution.battery_charge_limit_generation
            )
            + 1,
            battery_charge_limit_generation_at=H.NOW,
        ),
    )
    await sensor._controller.async_reconcile(sensor._latest_active_frame)
    assert SENSOR.manual_ems_proxy_write_allowed(hass, "entry-a") is True
    services.calls.clear()
    sensor._latest_active_frame = safe_frame
    sensor._latest_active_frame = replace(
        safe_frame,
        execution=replace(
            safe_frame.execution,
            force_charge_soc_percent=90.0,
            maximum_charge_power_percent=50.0,
            maximum_discharge_power_percent=100.0,
        ),
    )
    await mode_proxy.async_select_option("self_use")
    frame_after_self_use = sensor._latest_active_frame
    assert frame_after_self_use is not None
    sensor._latest_active_frame = replace(
        frame_after_self_use,
        execution=replace(
            frame_after_self_use.execution,
            physical_mode_code=0,
            full_block_generation=int(
                frame_after_self_use.execution.full_block_generation
            )
            + 1,
            full_block_generation_at=H.NOW,
        ),
    )
    await mode_proxy.async_select_option("off_grid")
    frame_after_off_grid = sensor._latest_active_frame
    assert frame_after_off_grid is not None
    sensor._latest_active_frame = replace(
        frame_after_off_grid,
        execution=replace(
            frame_after_off_grid.execution,
            physical_mode_code=3,
            full_block_generation=int(
                frame_after_off_grid.execution.full_block_generation
            )
            + 1,
            full_block_generation_at=H.NOW,
        ),
    )
    controlled = make_proxy(
        number_module.HoymilesNumber,
        "force_charge_soc_4303",
        "force_charge_soc",
    )
    await controlled.async_set_native_value(90.0)
    frame_after_4303 = sensor._latest_active_frame
    assert frame_after_4303 is not None
    sensor._latest_active_frame = replace(
        frame_after_4303,
        execution=replace(
            frame_after_4303.execution,
            force_charge_soc_percent=90.0,
            full_block_generation=int(
                frame_after_4303.execution.full_block_generation
            )
            + 1,
            full_block_generation_at=H.NOW,
        ),
    )
    gcf_proxy = make_proxy(
        number_module.HoymilesNumber,
        "gcf_export_soft_limit_ratio_259",
        "maximum_export_power_limit",
    )
    await gcf_proxy.async_set_native_value(37.0)
    frame_after_gcf = sensor._latest_active_frame
    assert frame_after_gcf is not None
    sensor._latest_active_frame = replace(
        frame_after_gcf,
        execution=replace(
            frame_after_gcf.execution,
            effective_export_limit_percent=37.0,
            gcf_generation=int(frame_after_gcf.execution.gcf_generation) + 1,
            gcf_generation_at=H.NOW,
            gcf_cohort_coherent=True,
        ),
    )
    battery_proxy = make_proxy(
        number_module.HoymilesNumber,
        "battery_max_charge_power_306",
        "battery_max_charge_power",
    )
    await battery_proxy.async_set_native_value(60.0)
    assert [call[0:2] for call in services.calls] == [
        ("esphome", "source_node_ems_supervisor_write_complete_block"),
        ("esphome", "source_node_ems_supervisor_write_complete_block"),
        ("esphome", "source_node_ems_supervisor_write_complete_block"),
        ("esphome", "source_node_ems_supervisor_write_gcf_export_limit"),
        ("esphome", "source_node_ems_supervisor_write_battery_charge_limit"),
    ]
    assert all(
        call[2]["snapshot_generation"] >= 1
        for call in services.calls
    )
    assert services.calls[0][2]["mode_code"] == 0
    assert services.calls[1][2]["mode_code"] == 3
    assert services.calls[2][2]["force_charge_soc"] == 90.0
    assert services.calls[3][2]["target_percent"] == 37.0
    assert services.calls[4][2]["target_percent"] == 60.0


async def test_frame_less_master_stop_and_safe_off_order() -> None:
    _hass, _entry, _runtime, sensor, services = await _added_environment()
    sensor._latest_active_frame = None
    sensor._available = False
    await SENSOR.async_request_supervisor_master_stop(sensor.hass)

    latch_key = f"{SENSOR._MASTER_STOP_STORAGE_KEY_PREFIX}.entry-a"
    stored = sensor.hass.storage[latch_key]
    assert stored["latched"] is True
    assert sensor._master_stop_latched is True
    assert not any(
        domain == "input_select" and service == "select_option"
        for domain, service, _data in services.calls
    )
    assert any(domain == "input_boolean" for domain, _service, _data in services.calls)

    services.calls.clear()
    sensor._available = True
    incomplete = SimpleNamespace(
        master_stop_result=SimpleNamespace(status=EXECUTOR.MasterStopStatus.IN_PROGRESS),
        owner=EXECUTOR.ExecutionOwner.MANUAL,
        transaction=object(),
    )
    sensor._controller = SimpleNamespace(record=incomplete)
    await sensor._async_finalize_master_stop_if_safe()
    assert services.calls == []
    assert sensor.hass.storage[latch_key]["latched"] is True

    complete = EXECUTOR.ExecutorRecord(
        state=EXECUTOR.ActiveState.IDLE,
        owner=EXECUTOR.ExecutionOwner.NONE,
        transaction=None,
        starts_allowed=False,
        automatic_policies_enabled=False,
        reason=EXECUTOR.ExecutionReason.MASTER_STOP_COMPLETE,
        master_stop_result=EXECUTOR.MasterStopResult(
            status=EXECUTOR.MasterStopStatus.COMPLETED,
            transaction_id="master-stop-test-0001",
            requested_at=H.NOW,
            completed_at=H.NOW,
            reason=EXECUTOR.ExecutionReason.MASTER_STOP_COMPLETE,
        ),
    )
    sensor._controller = SimpleNamespace(
        record=complete,
        recorder_attributes=lambda: {},
    )
    await sensor._async_finalize_master_stop_if_safe()
    assert services.calls[-1] == (
        "input_select",
        "select_option",
        {
            "entity_id": "input_select.hoymiles_ems_supervisor_mode",
            "option": "Off",
        },
    )
    assert all(domain != "input_select" for domain, _service, _data in services.calls[:-1])
    assert sensor.hass.storage[latch_key]["latched"] is False


async def test_multi_entry_latch_and_joined_unload() -> None:
    hass, _entry, runtime, sensor, services = await _added_environment()
    cancelled = asyncio.Event()

    async def pending_executor() -> None:
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    sensor._controller_task = asyncio.create_task(pending_executor())
    await asyncio.sleep(0)
    hass.data[H.DOMAIN]["entry-b"] = H.FakeRuntimeData(runtime.source_device, {})
    SENSOR.notify_supervisor_guard(hass)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    latch_key = f"{SENSOR._MASTER_STOP_STORAGE_KEY_PREFIX}.entry-a"
    assert hass.storage[latch_key]["latched"] is True
    assert sensor.available is False
    assert not any(domain == "esphome" for domain, _service, _data in services.calls)

    second = SENSOR.HoymilesSupervisorSensor(
        hass,
        H.FakeConfigEntry("entry-b"),
        hass.data[H.DOMAIN]["entry-b"],
    )
    second.entity_id = "sensor.second_supervisor"
    second._init_fake_lifecycle()
    await second.add_to_platform_finish()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    second_key = f"{SENSOR._MASTER_STOP_STORAGE_KEY_PREFIX}.entry-b"
    assert hass.storage[second_key]["latched"] is True

    await sensor.remove_from_platform()
    assert cancelled.is_set()
    assert sensor._controller_task is None
    assert sensor._safety_task is None
    assert sensor._removed is True
    await second.remove_from_platform()

    active_hass, _active_entry, _active_runtime, active, _active_services = (
        await _added_environment()
    )

    class ActiveController:
        def __init__(self) -> None:
            self.calls = 0
            self.record = SimpleNamespace(
                state=EXECUTOR.ActiveState.EXECUTING,
                owner=EXECUTOR.ExecutionOwner.TARIFF,
                transaction=SimpleNamespace(master_stop_requested=False),
                master_stop_result=SimpleNamespace(
                    status=EXECUTOR.MasterStopStatus.NOT_REQUESTED
                ),
            )

        def recorder_attributes(self) -> dict[str, Any]:
            return {}

        async def async_master_stop(self, _frame: Any) -> None:
            self.calls += 1

    active_controller = ActiveController()
    active._controller = active_controller
    assert active._latest_active_frame is not None
    await active.remove_from_platform()
    active_key = f"{SENSOR._MASTER_STOP_STORAGE_KEY_PREFIX}.entry-a"
    assert active_hass.storage[active_key]["latched"] is True
    assert active_controller.calls == 1


async def main_async() -> None:
    await test_manual_ems_full_block_lease_resolves_on_matching_new_generation()
    print("PASS MANUAL_EMS_MATCHING_READBACK_LEASE")
    await test_manual_ems_full_block_normalizes_float32_readback_noise()
    print("PASS MANUAL_EMS_FLOAT32_NORMALIZATION")
    await test_manual_ems_full_block_lease_waits_for_second_coherent_generation_after_mismatch()
    print("PASS MANUAL_EMS_MISMATCH_BARRIER_LEASE")
    await test_manual_ems_malformed_mode_is_never_command_or_ack_authority()
    print("PASS MANUAL_EMS_MALFORMED_MODE")
    await test_manual_gcf_259_lease_resolves_on_matching_new_generation()
    print("PASS MANUAL_GCF_259_READBACK_LEASE")
    await test_manual_battery_306_lease_resolves_on_matching_new_generation()
    print("PASS MANUAL_BATTERY_306_READBACK_LEASE")
    await test_source_bound_transport_and_second_guard()
    print("PASS SOURCE_BOUND_TRANSPORT")
    await test_forced_block_requires_correlated_transaction_after_challenge()
    print("PASS FORCED_BLOCK_LEASED_SERVICE")
    await test_due_lease_renewal_replaces_waiting_unauthorized_timer()
    print("PASS LEASE_DUE_RESCHEDULE")
    test_rce_lease_direction_gate_supports_single_and_parallel_full_block()
    print("PASS LEASE_RCE_SINGLE_PARALLEL_DIRECTION_GATE")
    await test_renewal_transport_sequences_and_expiry_recovery()
    print("PASS LEASE_RENEW_SEQUENCE_EXPIRY_RECOVERY")
    await test_renewal_is_single_flight_and_task_cleanup_is_identity_owned()
    print("PASS LEASE_RENEW_SINGLE_FLIGHT")
    await test_snapshot_generation_retry_is_bounded_and_revalidated()
    print("PASS LEASE_GENERATION_BOUNDED_RETRY")
    await test_late_ack_and_stale_callback_cannot_touch_newer_lease()
    print("PASS LEASE_LATE_ACK_STALE_CALLBACK")
    await test_renew_timeout_and_unload_preserve_fail_closed_ownership()
    print("PASS LEASE_TIMEOUT_UNLOAD")
    await test_confirmed_terminal_record_clears_only_its_correlated_lease()
    print("PASS LEASE_TERMINAL_CLEANUP")
    await test_retarget_rejection_is_known_before_forced_transport()
    print("PASS LEASE_PRETRANSPORT_RETARGET")
    await test_manual_proxy_authority_is_exact_safe_off_only()
    print("PASS MANUAL_PROXY_AUTHORITY")
    await test_frame_less_master_stop_and_safe_off_order()
    print("PASS MASTER_STOP_LATCH_ORDER")
    await test_multi_entry_latch_and_joined_unload()
    print("PASS CARDINALITY_UNLOAD")


if __name__ == "__main__":
    asyncio.run(main_async())
    print("Supervisor transport/lifecycle contract: PASS groups=20")
