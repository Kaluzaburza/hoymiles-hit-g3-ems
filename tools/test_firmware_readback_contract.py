#!/usr/bin/env python3
"""Source-contract checks for physical FC03 actuator acknowledgements."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def platform_block(source: str, component_id: str) -> str:
    # Require the platform-level four-space indentation. Action references may
    # legitimately reuse the same component id at a deeper indentation.
    marker = f"\n    id: {component_id}\n"
    start = source.index(marker)
    block_start = source.rfind("  - platform:", 0, start)
    assert block_start >= 0, component_id
    block_end = source.find("\n  - platform:", start)
    return source[block_start:] if block_end < 0 else source[block_start:block_end]


def script_block(source: str, component_id: str) -> str:
    marker = f"  - id: {component_id}\n"
    start = source.index(marker)
    block_end = source.find("\napi:\n", start)
    assert block_end >= 0, component_id
    return source[start:block_end]


def renew_decision(
    *,
    state_confirmed: bool = True,
    authorization_current: bool = True,
    nonce_current: bool = True,
    duplicate_response: bool = False,
    before_soft_expiry: bool = True,
    before_hard_deadline: bool = True,
    identity_matches: bool = True,
    sequence_valid: bool = True,
    requested_matches: bool = True,
    physical_matches: bool = True,
    readback_fresh: bool = True,
    generation_acceptable: bool = True,
) -> str:
    """Executable truth table for the ordered native renewal branches."""

    if not state_confirmed:
        return "lease_not_confirmed"
    if not authorization_current:
        return "authorization_lost"
    if not nonce_current and not duplicate_response:
        return "stale_or_invalid_nonce"
    if not before_soft_expiry or not before_hard_deadline:
        return "lease_expired"
    if not identity_matches or not sequence_valid:
        return "stale_or_invalid_sequence"
    if not requested_matches or not physical_matches or not readback_fresh:
        return "physical_authority_lost"
    if not generation_acceptable:
        return "snapshot_generation_advanced"
    return "renewed"


def renew_generation_acceptable(current: float, requested: float) -> bool:
    """Independent oracle for an in-flight FC03 rollover during renewal."""

    if not 1.0 <= requested <= 16_000_000.0:
        return False
    return (
        abs(current - requested) <= 0.01
        or (
            current > requested
            and not (current >= 15_999_994.0 and requested <= 6.0)
        )
        or (requested >= 15_999_994.0 and current <= 6.0)
    )


def main() -> None:
    core = (ROOT / "packages" / "core.yaml").read_text(encoding="utf-8")
    modbus_connection = (ROOT / "packages" / "modbus_connection.yaml").read_text(
        encoding="utf-8"
    )
    settings = (ROOT / "packages" / "settings.yaml").read_text(encoding="utf-8")
    parallel = (ROOT / "packages" / "parallel_network.yaml").read_text(
        encoding="utf-8"
    )
    overview = (ROOT / "packages" / "overview.yaml").read_text(encoding="utf-8")
    battery = (ROOT / "packages" / "battery.yaml").read_text(encoding="utf-8")
    pv = (ROOT / "packages" / "pv.yaml").read_text(encoding="utf-8")
    meters = (ROOT / "packages" / "meters.yaml").read_text(encoding="utf-8")
    load = (ROOT / "packages" / "backup_load.yaml").read_text(encoding="utf-8")
    catalog = json.loads(
        (ROOT / "custom_components" / "hoymiles_hit_modbus" / "entity_catalog.json")
        .read_text(encoding="utf-8")
    )

    readbacks = {
        "ems_mode_raw_4300": (4300, "U_WORD"),
        "self_used_soc_readback_4301": (4301, "U_WORD"),
        "backup_soc_raw_4302": (4302, "U_WORD"),
        "force_charge_soc_readback_4303": (4303, "U_WORD"),
        "maximum_charge_power_readback_4304": (4304, "U_WORD"),
        "force_discharge_soc_readback_4305": (4305, "U_WORD"),
        "maximum_discharge_power_readback_4306": (4306, "U_WORD"),
        "gcf_enable_readback_258": (258, "U_WORD"),
        "gcf_export_soft_limit_ratio_readback_259": (259, "S_WORD"),
        "battery_max_charge_power_readback_306": (306, "U_WORD"),
    }
    for component_id, (address, value_type) in readbacks.items():
        block = platform_block(settings, component_id)
        assert "platform: modbus_controller" in block, component_id
        assert "register_type: holding" in block, component_id
        assert f"address: {address}" in block, component_id
        assert f"value_type: {value_type}" in block, component_id
        assert "force_update: true" in block, component_id
        assert "internal: true" not in block, component_id

    generations = {
        "ems_control_readback_generation": "maximum_discharge_power_readback_4306",
        "gcf_control_readback_generation": "gcf_export_soft_limit_ratio_readback_259",
        "battery_charge_power_readback_generation": (
            "battery_max_charge_power_readback_306"
        ),
    }
    for generation_id, poll_sensor_id in generations.items():
        generation_block = platform_block(settings, generation_id)
        poll_block = platform_block(settings, poll_sensor_id)
        assert "platform: template" in generation_block, generation_id
        assert "update_interval: never" in generation_block, generation_id
        assert "force_update: true" in generation_block, generation_id
        publish = f"id({generation_id}).publish_state(generation);"
        assert settings.count(publish) == 1, generation_id
        assert publish in poll_block, generation_id

    mode_block = platform_block(settings, "ems_mode_4300")
    tariff_rollback_block = platform_block(
        settings, "ems_complete_block_charge_rollback_command"
    )
    ems_writer = script_block(settings, "ems_write_complete_block_4300_4306")
    assert "update_interval: never" in mode_block
    assert "optimistic:" not in mode_block
    assert ".publish_state(x)" not in mode_block
    assert "id(ems_write_complete_block_4300_4306)->execute(" in mode_block
    assert "create_write_multiple_command" not in mode_block
    assert "send_raw" not in mode_block
    assert "platform: template" in tariff_rollback_block
    assert "set_action:" in tariff_rollback_block
    assert "min_value: 0" in tariff_rollback_block
    assert "max_value: 101100" in tariff_rollback_block
    assert "initial_value: 0" in tariff_rollback_block
    assert "optimistic: false" in tariff_rollback_block
    assert "restore_value: false" in tariff_rollback_block
    assert "lambda:" not in tariff_rollback_block.split("set_action:", 1)[0]
    assert "update_interval:" not in tariff_rollback_block
    assert "x < 10010.0f || x > 101100.0f" in tariff_rollback_block
    assert "const int32_t force_charge_soc = payload / 1001;" in (
        tariff_rollback_block
    )
    assert "const int32_t maximum_charge_power_raw = payload % 1001;" in (
        tariff_rollback_block
    )
    assert "physical_mode == 3 ? 3.0f : 0.0f" in tariff_rollback_block
    assert "std::fabs(physical_mode_raw - static_cast<float>(physical_mode))" in (
        tariff_rollback_block
    )
    assert "create_write_multiple_command" not in tariff_rollback_block
    assert "send_raw" not in tariff_rollback_block

    # Every EMS edit is composed from the complete physical 4300-4306
    # snapshot. A Master gets the same FC16 frame on the system broadcast
    # address 0; a single inverter keeps the addressed controller path.
    assert "mode: queued" in ems_writer
    assert "snapshot_generation: float" in ems_writer
    assert "id(ems_verified_hardware_readback_supported).state" in ems_writer
    assert ems_writer.index("ems_verified_hardware_readback_supported") < ems_writer.index(
        "send_raw(payload)"
    )
    assert "static_cast<uint32_t>(millis() - ems_snapshot_ms) > 15000U" in ems_writer
    assert "if (machine_type == 1)" in ems_writer
    assert "0x00, 0x10, 0x10, 0xCC, 0x00, 0x07, 0x0E" in ems_writer
    assert "id(modbus_1).send_raw(payload);" in ems_writer
    assert "create_write_multiple_command" in ems_writer
    assert "controller, 4300, values.size(), values" in ems_writer
    assert ".publish_state(" not in ems_writer
    supervisor_api = settings.split("\napi:\n", 1)[1].split("\nsensor:\n", 1)[0]
    assert supervisor_api.count(
        "\n    - action: ems_supervisor_write_complete_block\n"
    ) == 1
    for field in (
        "mode_code: int",
        "self_use_soc: float",
        "backup_soc: float",
        "force_charge_soc: float",
        "maximum_charge_power: float",
        "force_discharge_soc: float",
        "maximum_discharge_power: float",
        "snapshot_generation: float",
    ):
        assert field in supervisor_api, field
    assert "id: ems_write_complete_block_4300_4306" in supervisor_api
    complete_action_start = supervisor_api.index(
        "    - action: ems_supervisor_write_complete_block"
    )
    complete_action_end = supervisor_api.index(
        "    - action: ems_supervisor_control_lease_challenge",
        complete_action_start,
    )
    complete_action = supervisor_api[complete_action_start:complete_action_end]
    assert "supports_response: optional" in complete_action
    assert 'lambda: "return return_response;"' in complete_action
    assert 'auto result = std::make_shared<std::string>("unknown");' in complete_action
    assert 'root["schema_version"] = 1;' in complete_action
    assert 'root["accepted"] = true;' in complete_action
    assert 'root["accepted"] = false;' in complete_action
    assert 'root["accepted"] = nullptr;' in complete_action
    assert 'root["reason"] = *result;' in complete_action
    assert "snapshot_generation, false, result" in complete_action

    # Forced modes use one durable ESP-side lease and still enter the same
    # full-block writer.  API connectivity alone is never a renewal source.
    assert "action: ems_supervisor_control_lease_challenge" in supervisor_api
    assert "action: ems_supervisor_write_complete_block_leased" in supervisor_api
    assert "action: ems_supervisor_renew_control_lease" in supervisor_api
    assert 'root["protocol_version"] = 2;' in supervisor_api
    assert "lease_store_failed" in supervisor_api
    assert "stale_or_invalid_nonce" in supervisor_api
    assert "authorization_current" in supervisor_api
    assert "snapshot_generation, true, dispatch" in supervisor_api
    assert "api.connected" not in supervisor_api
    assert "if ((requested_mode == 4 || requested_mode == 5) && !lease_context)" in ems_writer
    assert 'report_dispatch("control_lease_required")' in ems_writer
    assert "interval: 1s" in settings
    assert "expired_wait_readback_after_reboot" in settings
    assert "global_preferences->sync()" in settings
    assert "std::array<uint32_t, 28>" in settings
    assert "record[27] == crc32" in settings
    assert "retarget_not_authorized" in supervisor_api
    assert "command_generation > static_cast<int>(previous_record[5])" in supervisor_api
    assert "record[20U + index] = retarget ? physical[index] : 0U" in supervisor_api
    assert "active_owned && record[20] != 0U" in settings
    assert "active_matches || predecessor_matches" in settings
    assert "std::min<uint32_t>(120000U, hard_remaining)" in settings
    assert "id(ems_control_lease_expiry_ms) = now_ms + remaining;" in settings
    assert "authorization_seconds" in settings
    assert "authorization_expired" in settings
    assert settings.count("id(ems_control_lease_nonce_issued_ms)) <= 60000U;") == 1
    assert settings.count("id(ems_control_lease_nonce_issued_ms)) <= 120000U;") == 1
    assert "duplicate_response" in supervisor_api
    assert "id(ems_control_lease_previous_nonce) = supplied_nonce;" in supervisor_api
    assert "id(ems_control_lease_last_sequence) + 1U" in supervisor_api
    renew_action = supervisor_api.split(
        "    - action: ems_supervisor_renew_control_lease", 1
    )[1].split("    - action: ems_supervisor_write_gcf_export_limit", 1)[0]
    assert "const bool readback_fresh =" in renew_action
    assert "const bool generation_same_or_newer =" in renew_action
    assert "const bool generation_acceptable =" in renew_action
    assert "current_generation > snapshot_generation" in renew_action
    assert "snapshot_generation >= 15999994.0f" in renew_action
    assert "current_generation <= 6.0f" in renew_action
    assert renew_generation_acceptable(400.0, 400.0)
    assert renew_generation_acceptable(401.0, 400.0)
    assert renew_generation_acceptable(2.0, 15_999_998.0)
    assert not renew_generation_acceptable(400.0, 401.0)
    assert not renew_generation_acceptable(15_999_998.0, 2.0)
    assert not renew_generation_acceptable(400.0, 0.0)
    assert not renew_generation_acceptable(400.0, 16_000_001.0)
    assert 'reason = "snapshot_generation_advanced";' in renew_action
    assert renew_action.index('reason = "physical_authority_lost";') < (
        renew_action.index('reason = "snapshot_generation_advanced";')
    )
    assert renew_decision(
        generation_acceptable=False
    ) == "snapshot_generation_advanced"
    assert renew_decision(
        generation_acceptable=False,
        physical_matches=False,
    ) == "physical_authority_lost"
    assert renew_decision(
        generation_acceptable=False,
        requested_matches=False,
    ) == "physical_authority_lost"
    assert renew_decision(
        generation_acceptable=False,
        readback_fresh=False,
    ) == "physical_authority_lost"
    assert renew_decision(generation_acceptable=True) == "renewed"
    duplicate_branch = supervisor_api.split("else if (duplicate_response)", 1)[1].split(
        "} else {", 1
    )[0]
    assert "ems_control_lease_expiry_ms" not in duplicate_branch
    assert 'root["queued"]' not in complete_action
    assert complete_action.count("api.respond:") == 2
    assert "dispatch_result: std::shared_ptr<std::string>" in ems_writer
    refusal_reasons = (
        "previous_write_pending",
        "hardware_not_ready",
        "generation_unavailable",
        "stale_snapshot_generation",
        "mode_unavailable",
        "invalid_block",
        "stale_snapshot",
        "topology_unavailable",
        "topology_invalid",
        "topology_nonintegral",
        "slave_topology",
        "topology_unsupported",
        "machine_count_unavailable",
        "machine_count_invalid",
        "machine_count_nonintegral",
        "machine_count_out_of_range",
    )
    for reason in refusal_reasons:
        assert ems_writer.count(f'report_dispatch("{reason}");') == 1, reason
    assert ems_writer.count('report_dispatch("accepted");') == 2
    addressed_dispatch = ems_writer.index("controller->queue_command(command);")
    assert ems_writer.index('report_dispatch("accepted");', addressed_dispatch) > (
        addressed_dispatch
    )
    broadcast_dispatch = ems_writer.index("id(modbus_1).send_raw(payload);")
    assert ems_writer.index('report_dispatch("accepted");', broadcast_dispatch) > (
        broadcast_dispatch
    )
    assert supervisor_api.count(
        "action: ems_supervisor_write_gcf_export_limit"
    ) == 1
    assert supervisor_api.count(
        "action: ems_supervisor_write_battery_charge_limit"
    ) == 1
    assert "id(gcf_control_readback_generation).state -" in supervisor_api
    assert (
        "id(battery_charge_power_readback_generation).state -" in supervisor_api
    )
    assert supervisor_api.count(
        "id(direct_register_verified_readback_supported).state < 0.5f"
    ) == 2
    assert supervisor_api.count(
        "std::fabs(machine_count - 1.0f) < 0.01f"
    ) == 2
    assert "id: gcf_export_soft_limit_ratio_259" in supervisor_api
    assert "id: battery_max_charge_power_306" in supervisor_api
    ems_last_poll = platform_block(settings, "maximum_discharge_power_readback_4306")
    assert "id(ems_control_last_readback_ms) = millis();" in ems_last_poll
    assert "id(ems_control_last_readback_ms) = millis();" not in platform_block(
        settings, "ems_mode_raw_4300"
    )
    for mirror_id in (
        "self_used_soc_readback_4301",
        "backup_soc_raw_4302",
        "force_charge_soc_readback_4303",
        "maximum_charge_power_readback_4304",
        "force_discharge_soc_readback_4305",
        "maximum_discharge_power_readback_4306",
    ):
        assert f"id({mirror_id}).state" in mode_block, mirror_id
    for range_marker in (
        "valid_range(self_use_soc, 10.0f, 100.0f)",
        "valid_range(backup_soc, 60.0f, 100.0f)",
        "valid_range(force_charge_soc, 10.0f, 100.0f)",
        "valid_range(maximum_charge_power, 0.0f, 100.0f)",
        "valid_range(force_discharge_soc, 0.0f, 100.0f)",
        "valid_range(maximum_discharge_power, 0.0f, 100.0f)",
    ):
        assert range_marker in ems_writer, range_marker
    assert "std::isfinite(value)" in ems_writer
    assert "encode(id(self_used_soc_4301).state" not in mode_block
    assert "encode(id(force_charge_soc_4303).state" not in mode_block
    assert "encode(id(maximum_charge_power_4304).state" not in mode_block
    assert "encode(id(force_discharge_soc_4305).state" not in mode_block
    assert "encode(id(maximum_discharge_power_4306).state" not in mode_block

    # Every caller freezes the FC03 generation together with its complete
    # physical snapshot. The writer rejects a second mutation while the first
    # block is pending, and a queued stale tuple cannot become valid merely
    # because the first acknowledgement arrived in the meantime.
    call_marker = "id(ems_write_complete_block_4300_4306)->execute("
    actuator_calls = {
        "ems_mode_4300": (
            mode_block,
            (
                "mode",
                "id(self_used_soc_readback_4301).state",
                "id(backup_soc_raw_4302).state",
                "id(force_charge_soc_readback_4303).state",
                "id(maximum_charge_power_readback_4304).state",
                "id(force_discharge_soc_readback_4305).state",
                "id(maximum_discharge_power_readback_4306).state",
                "id(ems_control_readback_generation).state",
            ),
        ),
        "self_used_soc_4301": (
            platform_block(settings, "self_used_soc_4301"),
            (
                "id(ems_mode_raw_4300).state",
                "safe",
                "id(backup_soc_raw_4302).state",
                "id(force_charge_soc_readback_4303).state",
                "id(maximum_charge_power_readback_4304).state",
                "id(force_discharge_soc_readback_4305).state",
                "id(maximum_discharge_power_readback_4306).state",
                "id(ems_control_readback_generation).state",
            ),
        ),
        "force_charge_soc_4303": (
            platform_block(settings, "force_charge_soc_4303"),
            (
                "id(ems_mode_raw_4300).state",
                "id(self_used_soc_readback_4301).state",
                "id(backup_soc_raw_4302).state",
                "safe",
                "id(maximum_charge_power_readback_4304).state",
                "id(force_discharge_soc_readback_4305).state",
                "id(maximum_discharge_power_readback_4306).state",
                "id(ems_control_readback_generation).state",
            ),
        ),
        "maximum_charge_power_4304": (
            platform_block(settings, "maximum_charge_power_4304"),
            (
                "id(ems_mode_raw_4300).state",
                "id(self_used_soc_readback_4301).state",
                "id(backup_soc_raw_4302).state",
                "id(force_charge_soc_readback_4303).state",
                "safe",
                "id(force_discharge_soc_readback_4305).state",
                "id(maximum_discharge_power_readback_4306).state",
                "id(ems_control_readback_generation).state",
            ),
        ),
        "force_discharge_soc_4305": (
            platform_block(settings, "force_discharge_soc_4305"),
            (
                "id(ems_mode_raw_4300).state",
                "id(self_used_soc_readback_4301).state",
                "id(backup_soc_raw_4302).state",
                "id(force_charge_soc_readback_4303).state",
                "id(maximum_charge_power_readback_4304).state",
                "safe",
                "id(maximum_discharge_power_readback_4306).state",
                "id(ems_control_readback_generation).state",
            ),
        ),
        "maximum_discharge_power_4306": (
            platform_block(settings, "maximum_discharge_power_4306"),
            (
                "id(ems_mode_raw_4300).state",
                "id(self_used_soc_readback_4301).state",
                "id(backup_soc_raw_4302).state",
                "id(force_charge_soc_readback_4303).state",
                "id(maximum_charge_power_readback_4304).state",
                "id(force_discharge_soc_readback_4305).state",
                "safe",
                "id(ems_control_readback_generation).state",
            ),
        ),
        "ems_complete_block_charge_rollback_command": (
            tariff_rollback_block,
            (
                "rollback_mode",
                "id(self_used_soc_readback_4301).state",
                "id(backup_soc_raw_4302).state",
                "rollback_force_charge_soc",
                "rollback_maximum_charge_power",
                "id(force_discharge_soc_readback_4305).state",
                "id(maximum_discharge_power_readback_4306).state",
                "id(ems_control_readback_generation).state",
            ),
        ),
    }
    # Three additional invocations belong to the legacy API response, the
    # leased API response, and the local expiry restore path.
    assert settings.count(call_marker) == len(actuator_calls) + 3
    for component_id, (block, arguments) in actuator_calls.items():
        assert block.count(call_marker) == 1, component_id
        call_start = block.index(call_marker)
        call_end = block.index(");", call_start) + 2
        actual_call = re.sub(r"\s+", "", block[call_start:call_end])
        expected_call = call_marker + ",".join((*arguments, "false", "nullptr")) + ");"
        assert actual_call == expected_call, component_id
        if component_id not in {
            "ems_mode_4300",
            "ems_complete_block_charge_rollback_command",
        }:
            assert block[call_end:].lstrip().startswith("return {};"), component_id

    for marker in (
        "if (id(ems_control_write_pending))",
        "snapshot_generation - physical_generation",
        "auto arm_physical_ack_barrier = [&]()",
        "id(ems_control_write_generation_before) = physical_generation;",
        "id(ems_control_write_pending) = true;",
    ):
        assert marker in ems_writer, marker
    assert ems_writer.index("if (id(ems_control_write_pending))") < ems_writer.index(
        "id(modbus_1).send_raw(payload);"
    )
    assert ems_writer.index("if (id(ems_control_write_pending))") < ems_writer.index(
        "controller->queue_command(command);"
    )
    for register in range(4300, 4307):
        marker = f"id(ems_control_write_expected_{register}) = values[{register - 4300}];"
        assert marker in ems_writer, marker
    assert ems_writer.count("arm_physical_ack_barrier();") == 2
    assert ems_writer.index("arm_physical_ack_barrier();") < ems_writer.index(
        "id(modbus_1).send_raw(payload);"
    )
    assert ems_writer.rindex("arm_physical_ack_barrier();") < ems_writer.index(
        "controller->queue_command(command);"
    )

    # Only the final sensor in the grouped physical FC03 response may release
    # the barrier. Exact full-block ACK releases it immediately; a failed write
    # needs a second newer complete FC03 before a deliberate retry is allowed.
    assert settings.count("id(ems_control_write_pending) = false;") == 2
    assert ems_last_poll.count("id(ems_control_write_pending) = false;") == 2
    assert "generation > generation_before" in ems_last_poll
    assert "generation > mismatch_generation" in ems_last_poll
    for register in range(4300, 4307):
        assert f"id(ems_control_write_expected_{register})" in ems_last_poll, register
    assert ems_last_poll.index("const bool matches = complete") < ems_last_poll.index(
        "id(ems_control_write_pending) = false;"
    )
    assert "bariera zwolniona dopiero po kolejnej kompletnej generacji FC03" in (
        ems_last_poll
    )

    class ManualWriteBarrier:
        """Small model of the physical generation/exact-block interlock."""

        def __init__(self, generation: int, physical: tuple[int, ...]) -> None:
            self.generation = generation
            self.physical = physical
            self.pending: tuple[int, tuple[int, ...]] | None = None
            self.mismatch_generation: int | None = None
            self.writes: list[tuple[int, ...]] = []

        @staticmethod
        def newer(generation: int, baseline: int) -> bool:
            return generation > baseline or (baseline >= 16_000_000 and generation == 1)

        def request(
            self,
            desired: tuple[int, ...] | None,
            snapshot_generation: int | None,
            *,
            fresh: bool = True,
        ) -> bool:
            if (
                self.pending is not None
                or desired is None
                or len(desired) != 7
                or snapshot_generation is None
                or snapshot_generation != self.generation
                or not fresh
            ):
                return False
            self.pending = (self.generation, desired)
            self.mismatch_generation = None
            self.writes.append(desired)
            return True

        def physical_poll(
            self, generation: int, physical: tuple[int, ...] | None
        ) -> None:
            self.generation = generation
            if physical is not None:
                self.physical = physical
            if self.pending is None or physical is None:
                return
            baseline, expected = self.pending
            if not self.newer(generation, baseline):
                return
            if physical == expected:
                self.pending = None
                self.mismatch_generation = None
            elif self.mismatch_generation is None:
                self.mismatch_generation = generation
            elif self.newer(generation, self.mismatch_generation):
                self.pending = None
                self.mismatch_generation = None

    initial = (0, 20, 60, 50, 1000, 20, 1000)
    first = initial[:6] + (300,)
    stale_second = initial[:5] + (50, initial[6])

    # N+1 MATCH: the ordinary single edit obtains a normal fast full-block ACK.
    fast_ack = ManualWriteBarrier(10, initial)
    assert fast_ack.request(first, 10), "An ordinary manual mutation must still work"
    fast_ack.physical_poll(11, first)
    assert fast_ack.pending is None

    # Sequential edits after ACK preserve the sibling value confirmed above.
    final = first[:5] + (50, first[6])
    assert fast_ack.request(final, 11), "The next edit is accepted after physical ACK"
    fast_ack.physical_poll(12, final)
    assert fast_ack.pending is None
    assert fast_ack.physical[5] == 50 and fast_ack.physical[6] == 300

    # N+1 mismatch, N+2 match: the first poll may predate FC16. A late exact
    # confirmation remains a valid ACK and the original request is not lost.
    late_ack = ManualWriteBarrier(20, initial)
    assert late_ack.request(first, 20)
    late_ack.physical_poll(21, initial)
    assert late_ack.pending is not None
    late_ack.physical_poll(22, first)
    assert late_ack.pending is None
    assert late_ack.writes == [first]

    # N+1 mismatch, N+2 mismatch: abandon/rebase to authoritative physical
    # state. No automatic retry or second FC16 is emitted by the barrier.
    abandoned = ManualWriteBarrier(30, initial)
    assert abandoned.request(first, 30)
    abandoned.physical_poll(31, initial)
    recovered_physical = initial[:4] + (800,) + initial[5:]
    abandoned.physical_poll(32, recovered_physical)
    assert abandoned.pending is None
    assert abandoned.physical == recovered_physical
    assert abandoned.writes == [first], "Recovery must never auto-retry the write"

    # No N+1: matching values or elapsed time at the same generation are not
    # acknowledgement, so the barrier remains closed indefinitely.
    stalled = ManualWriteBarrier(40, initial)
    assert stalled.request(first, 40)
    stalled.physical_poll(40, first)
    assert stalled.pending is not None
    assert stalled.writes == [first]

    # Rapid second edit while pending is rejected and no stale full block can
    # enter either Modbus transport queue.
    rapid = ManualWriteBarrier(50, initial)
    assert rapid.request(first, 50)
    assert not rapid.request(stale_second, 50)
    assert rapid.writes == [first], "The stale sibling block must never reach FC16"

    # Shared-writer regression: an automatic writer is blocked by a pending
    # manual transaction, then works normally after physical recovery. This is
    # only serialization; it does not alter ownership or retry automatically.
    shared = ManualWriteBarrier(60, initial)
    assert shared.request(first, 60)
    automatic_start = (4,) + initial[1:]
    assert not shared.request(automatic_start, 60)
    assert shared.writes == [first]
    shared.physical_poll(61, initial)
    shared.physical_poll(62, recovered_physical)
    assert shared.pending is None
    automatic_retry = (4,) + recovered_physical[1:]
    assert shared.request(automatic_retry, 62)
    assert shared.writes == [first, automatic_retry]
    shared.physical_poll(63, automatic_retry)
    assert shared.pending is None

    # The tariff rollback composes its one desired block from the latest
    # physical tuple plus explicit saved 4303/4304 values. A no-op 4304 does
    # not cause a separate transaction.
    tariff_one_physical = (0, 20, 60, 93, 500, 20, 1000)
    tariff_one_desired = (0, 20, 60, 98, 500, 20, 1000)
    tariff_one = ManualWriteBarrier(70, tariff_one_physical)
    assert tariff_one.request(tariff_one_desired, 70)
    assert tariff_one.writes == [tariff_one_desired]
    tariff_one.physical_poll(71, tariff_one_desired)
    assert tariff_one.pending is None

    # Mode, 4303 and 4304 may all need restoration, but they still form one
    # FC16 desired tuple and one physical full-block acknowledgement.
    tariff_many_physical = (4, 20, 60, 93, 600, 20, 1000)
    tariff_many_desired = (0, 20, 60, 98, 500, 20, 1000)
    tariff_many = ManualWriteBarrier(80, tariff_many_physical)
    assert tariff_many.request(tariff_many_desired, 80)
    assert tariff_many.writes == [tariff_many_desired]
    tariff_many.physical_poll(81, tariff_many_desired)
    assert tariff_many.pending is None

    # A logical no-op emits no FC16, but it still needs a newer complete FC03
    # cohort before ownership may be released. Matching cached values, a
    # missing block, or any sibling mismatch are not acknowledgement.
    def tariff_no_op_ack(
        baseline_generation: int,
        generation: int,
        expected: tuple[int, ...],
        physical: tuple[int, ...] | None,
    ) -> bool:
        return (
            ManualWriteBarrier.newer(generation, baseline_generation)
            and physical is not None
            and physical == expected
        )

    tariff_no_op = (0, 20, 60, 98, 500, 20, 1000)
    assert not tariff_no_op_ack(90, 90, tariff_no_op, tariff_no_op)
    assert not tariff_no_op_ack(90, 91, tariff_no_op, None)
    assert not tariff_no_op_ack(
        90, 91, tariff_no_op, tariff_no_op[:6] + (900,)
    )
    assert tariff_no_op_ack(90, 91, tariff_no_op, tariff_no_op)

    assert not fast_ack.request(final, 11), "A stale captured generation must fail closed"
    assert not fast_ack.request(None, 12), "An incomplete snapshot must fail closed"
    assert not fast_ack.request(final, None), "A missing generation must fail closed"
    assert not fast_ack.request(final, 12, fresh=False), "A stale snapshot must fail closed"

    # A deliberate retry after failed ACK is rebuilt from the recovered block.
    retry = recovered_physical[:6] + (300,)
    assert abandoned.request(retry, 32)
    assert abandoned.writes[-1][4] == 800, "Retry must preserve the recovered snapshot"

    wrapped = ManualWriteBarrier(16_000_000, initial)
    assert wrapped.request(first, 16_000_000)
    wrapped.physical_poll(1, first)
    assert wrapped.pending is None, "Generation wrap still requires an exact physical block"

    ems_actuators = (
        "self_used_soc_4301",
        "force_charge_soc_4303",
        "maximum_charge_power_4304",
        "force_discharge_soc_4305",
        "maximum_discharge_power_4306",
    )
    for component_id in ems_actuators:
        block = platform_block(settings, component_id)
        assert "write_lambda:" in block, component_id
        assert "id(ems_verified_hardware_readback_supported).has_state()" in block, component_id
        assert "id(ems_verified_hardware_readback_supported).state" in block, component_id
        assert ".state < 0.5f" in block, component_id
        assert "id(ems_write_complete_block_4300_4306)->execute(" in block, component_id
        assert "return {};" in block, component_id
        assert "readback_generation).publish_state" not in block, component_id

    direct_actuators = (
        "gcf_enable_258",
        "gcf_export_soft_limit_ratio_259",
        "battery_max_charge_power_306",
    )
    for component_id in direct_actuators:
        block = platform_block(settings, component_id)
        assert "write_lambda:" in block, component_id
        assert (
            "id(direct_register_verified_readback_supported).has_state()" in block
        ), component_id
        assert "id(direct_register_verified_readback_supported).state" in block, component_id
        assert ".state < 0.5f" in block, component_id
        assert "ems_write_complete_block_4300_4306" not in block, component_id

    # A ModbusNumber/ModbusSelect command echo is not a physical acknowledgement.
    number_source = settings.split("\nnumber:\n", 1)[1]
    assert "readback_generation).publish_state" not in number_source

    for component_id, address in (
        ("machines_type_6048", 6048),
        ("number_of_machines_master_and_slave_6049", 6049),
    ):
        block = platform_block(parallel, component_id)
        assert "modbus_controller_id: ${modbus_settings_controller_id}" in block
        assert f"address: {address}" in block
        assert "skip_updates:" not in block
        assert "force_update: true" in block

    topology_generation = platform_block(
        parallel, "parallel_topology_readback_generation"
    )
    topology_first = platform_block(parallel, "machines_type_6048")
    topology_poll = platform_block(parallel, "number_of_machines_master_and_slave_6049")
    assert "update_interval: never" in topology_generation
    assert "force_update: true" in topology_generation
    assert "id(parallel_topology_last_readback_ms) = millis();" in topology_poll
    assert (
        "id(parallel_topology_readback_generation).publish_state(generation);"
        in topology_poll
    )
    assert "parallel_topology_last_readback_ms" not in topology_first
    assert "parallel_topology_readback_generation).publish_state" not in topology_first

    capability = platform_block(parallel, "ems_verified_hardware_readback_supported")
    assert 'name: "EMS Verified Hardware Readback Supported"' in capability
    assert "accuracy_decimals: 0" in capability
    assert "update_interval: 1s" in capability
    assert "static_cast<uint32_t>(millis() - last_readback) > 60000U" in capability
    assert "if (machine_type == 0) return 1.0f;" in capability
    assert "machine_type != 1" in capability
    assert "return count >= 2 && count <= 10 ? 1.0f : 0.0f;" in capability
    assert "communication_address_" not in capability

    direct_capability = platform_block(
        parallel, "direct_register_verified_readback_supported"
    )
    assert 'name: "Direct Register Verified Readback Supported"' in direct_capability
    assert "static_cast<uint32_t>(millis() - last_readback) > 60000U" in direct_capability
    assert "return machine_type == 0 ? 1.0f : 0.0f;" in direct_capability
    assert "\nbinary_sensor:\n" not in parallel
    assert "Gotowe - broadcast EMS, odczyt Mastera" in parallel

    # The broadcast itself has no Modbus response. HA must therefore certify
    # completion only from the later physical Master FC03 generation.
    assert settings.count("send_raw(payload)") == 1
    assert settings.count("0x00, 0x10, 0x10, 0xCC") == 1

    # Operational overview timestamps must prove a physical FC03 response.
    # A periodic template may keep publishing its cached value after Modbus
    # goes silent and defeats every HA-side max-age safety gate.
    overview_sources = {
        "pv_total_power_30001": (
            (overview, "pv_total_power_master_30001", "!= 1"),
            (pv, "pv_total_power_8528", "== 1"),
        ),
        "grid_total_active_power_30011": (
            (overview, "grid_total_active_power_master_30011", "!= 1"),
            (meters, "grid_total_active_power_1814", "== 1"),
        ),
        "load_active_power_30015": (
            (overview, "load_active_power_master_30015", "!= 1"),
            (load, "load_power_total_8553", "== 1"),
        ),
        "battery_soc_30020": (
            (overview, "battery_soc_master_30020", "!= 1"),
            (battery, "battery_soc_1909", "== 1"),
        ),
    }
    public_poll_entities = tuple(overview_sources)
    for public_id, physical_sources in overview_sources.items():
        public_block = platform_block(overview, public_id)
        assert "platform: template" in public_block, public_id
        assert "update_interval: never" in public_block, public_id
        assert "force_update: true" in public_block, public_id
        assert "lambda:" not in public_block, public_id
        publish = f"id({public_id}).publish_state(x);"
        for source, source_id, topology_gate in physical_sources:
            source_block = platform_block(source, source_id)
            assert "platform: modbus_controller" in source_block, source_id
            assert "force_update: true" in source_block, source_id
            assert publish in source_block, source_id
            for other_public_id in public_poll_entities:
                if other_public_id != public_id:
                    assert (
                        f"id({other_public_id}).publish_state(x);"
                        not in source_block
                    ), f"{source_id} must not publish {other_public_id}"
            assert "parallel_topology_last_readback_ms" in source_block, source_id
            assert topology_gate in source_block, source_id

    soc_source = platform_block(battery, "battery_soc_1909")
    assert "modbus_controller_id: ${modbus_fast_controller_id}" in soc_source

    # PV hold needs a new independent physical report even when BMS power
    # remains exactly zero. No template heartbeat, extra entity or lost history.
    bms_power = platform_block(battery, "battery_power_1914")
    assert "platform: modbus_controller" in bms_power
    assert "modbus_controller_id: ${modbus_fast_controller_id}" in bms_power
    for required in ("register_type: read", "address: 1914", "value_type: S_DWORD",
                     "force_update: true", "state_class: measurement"):
        assert required in bms_power, required
    for forbidden in ("lambda:", "heartbeat:", "skip_updates:", "internal: true"):
        assert forbidden not in bms_power, forbidden

    # Capacity is stable, but every successful FC03 cycle must still publish a
    # physical report.  This prevents an unchanged 4102 setting from looking
    # stale to HA and keeps it off the slow diagnostic controller.
    capacity_source = platform_block(battery, "battery_capacity_4102")
    assert (
        "modbus_controller_id: ${modbus_settings_controller_id}"
        in capacity_source
    )
    assert "register_type: holding" in capacity_source
    assert "address: 4102" in capacity_source
    assert "force_update: true" in capacity_source
    assert "skip_updates:" not in capacity_source
    assert "settings_update_interval: 20s" in core
    settings_controller_start = modbus_connection.index(
        "  - id: ${modbus_settings_controller_id}\n"
    )
    settings_controller_end = modbus_connection.find(
        "\n  - id:", settings_controller_start + 1
    )
    settings_controller = (
        modbus_connection[settings_controller_start:]
        if settings_controller_end < 0
        else modbus_connection[settings_controller_start:settings_controller_end]
    )
    assert "update_interval: ${settings_update_interval}" in settings_controller

    # Unchanged BMS limits are still successful physical FC03 samples.  They
    # must reach HA as reports so the signed-age fail-closed contract does not
    # confuse a stable limit with a dead Modbus source.
    for component_id in (
        "battery_voltage_1911",
        "max_charge_current_1916",
        "max_discharge_current_1917",
    ):
        block = platform_block(battery, component_id)
        assert "platform: modbus_controller" in block, component_id
        assert "force_update: true" in block, component_id

    for public_id, source_id in (
        ("inv_active_power_30007", "inv_active_power_master_30007"),
        ("battery_power_30009", "battery_power_master_30009"),
    ):
        public_block = platform_block(overview, public_id)
        assert "update_interval: never" in public_block, public_id
        assert "lambda:" not in public_block, public_id
        assert f"id({public_id}).publish_state(x);" in platform_block(
            overview, source_id
        )
        other_public_id = (
            "battery_power_30009"
            if public_id == "inv_active_power_30007"
            else "inv_active_power_30007"
        )
        assert f"id({other_public_id}).publish_state(x);" not in platform_block(
            overview, source_id
        )
    assert "id(inv_active_power_30007).publish_state(inverter_power);" in load
    assert "id(battery_power_30009).publish_state(" in load
    aggregate_generation = platform_block(
        overview, "parallel_aggregate_power_readback_generation"
    )
    assert 'name: "Parallel Aggregate Power Readback Generation"' in (
        aggregate_generation
    )
    assert "platform: template" in aggregate_generation
    assert "update_interval: never" in aggregate_generation
    assert "force_update: true" in aggregate_generation
    aggregate_load = platform_block(load, "load_power_total_8553")
    aggregate_publish = "generation_sensor->publish_state(generation);"
    all_firmware = overview + pv + meters + load
    assert all_firmware.count(aggregate_publish) == 1
    assert aggregate_publish in aggregate_load
    assert aggregate_load.index("id(battery_power_30009).publish_state(") < (
        aggregate_load.index(aggregate_publish)
    )
    for marker in (
        "std::lround(id(machines_type_6048).state) == 1",
        "overview_master_grid_last_readback_ms",
        "overview_master_pv_last_readback_ms",
        "static_cast<uint32_t>(now_ms - grid_ms) <= 30000U",
        "static_cast<uint32_t>(now_ms - pv_ms) <= 30000U",
        "id(grid_total_active_power_1814).has_state()",
        "id(pv_total_power_8528).has_state()",
        "overview_parallel_aggregate_committed_grid_readback_ms",
        "overview_parallel_aggregate_committed_pv_readback_ms",
        "grid_ms != committed_grid_ms",
        "pv_ms != committed_pv_ms",
        "auto *generation_sensor =",
        "id(parallel_aggregate_power_readback_generation);",
        "generation_sensor->has_state()",
        "generation_sensor->state < 16000000.0f",
    ):
        assert marker in aggregate_load, marker
    # A local write, timer or partial source callback cannot forge this marker.
    assert "parallel_aggregate_power_readback_generation" not in settings
    for source, component_id in (
        (pv, "pv_total_power_8528"),
        (meters, "grid_total_active_power_1814"),
    ):
        assert "parallel_aggregate_power_readback_generation" not in platform_block(
            source, component_id
        )
    for timestamp in (
        "overview_master_pv_last_readback_ms",
        "overview_master_grid_last_readback_ms",
        "overview_parallel_aggregate_committed_grid_readback_ms",
        "overview_parallel_aggregate_committed_pv_readback_ms",
    ):
        assert overview.count(f"id: {timestamp}") == 1, timestamp
        assert all_firmware.count(f"id({timestamp})") >= 1, timestamp
    assert "overview_master_load_last_readback_ms" not in overview + load

    committed_grid_position = aggregate_load.index(
        "id(overview_parallel_aggregate_committed_grid_readback_ms) ="
    )
    committed_pv_position = aggregate_load.index(
        "id(overview_parallel_aggregate_committed_pv_readback_ms) ="
    )
    publish_position = aggregate_load.index(aggregate_publish)
    assert committed_grid_position < publish_position
    assert committed_pv_position < publish_position

    def committed_generations(events: list[tuple[str, int]]) -> int:
        """Model the GRID/PV pair consumed by each Master LOAD callback."""

        grid_ms = 0
        pv_ms = 0
        committed_grid_ms = 0
        committed_pv_ms = 0
        generation = 0
        for event, timestamp_ms in events:
            if event == "grid":
                grid_ms = timestamp_ms
            elif event == "pv":
                pv_ms = timestamp_ms
            elif event == "load":
                if (
                    grid_ms != 0
                    and pv_ms != 0
                    and grid_ms != committed_grid_ms
                    and pv_ms != committed_pv_ms
                ):
                    committed_grid_ms = grid_ms
                    committed_pv_ms = pv_ms
                    generation += 1
            else:
                raise AssertionError(event)
        return generation

    assert committed_generations(
        [("grid", 10), ("pv", 11), ("load", 12), ("load", 13), ("load", 14)]
    ) == 1, "Three LOAD callbacks cannot reuse one GRID/PV reply pair"
    assert committed_generations(
        [
            ("grid", 10),
            ("pv", 11),
            ("load", 12),
            ("grid", 20),
            ("load", 21),
            ("pv", 22),
            ("load", 23),
            ("pv", 30),
            ("load", 31),
            ("grid", 32),
            ("load", 33),
        ]
    ) == 3, "Every committed generation requires both a new GRID and a new PV"

    catalog_names = {record["source_name"] for record in catalog}
    for source_name in (
        "EMS Control Readback Generation",
        "GCF Control Readback Generation",
        "Battery Charge Power Readback Generation",
        "EMS Mode Readback Code",
        "EMS Self-Use SOC Readback",
        "EMS Backup SOC Readback",
        "EMS Force Charge SOC Readback",
        "EMS Maximum Charge Power Readback",
        "EMS Force Discharge SOC Readback",
        "EMS Maximum Discharge Power Readback",
        "GCF Enable Readback Code",
        "GCF Maximum Export Power Readback",
        "Battery Max Charge Power Readback",
        "Parallel Topology Readback Generation",
        "EMS Verified Hardware Readback Supported",
        "Direct Register Verified Readback Supported",
    ):
        assert source_name in catalog_names, source_name
    capability_records = [
        record
        for record in catalog
        if record.get("source_name") == "EMS Verified Hardware Readback Supported"
    ]
    assert len(capability_records) == 1
    assert capability_records[0]["domain"] == "sensor"
    assert capability_records[0]["source_component"] == "sensor"
    direct_capability_records = [
        record
        for record in catalog
        if record.get("source_name")
        == "Direct Register Verified Readback Supported"
    ]
    assert len(direct_capability_records) == 1
    assert direct_capability_records[0]["domain"] == "sensor"
    assert direct_capability_records[0]["source_component"] == "sensor"

    print("Firmware FC03 readback contract: PASS")


if __name__ == "__main__":
    main()
