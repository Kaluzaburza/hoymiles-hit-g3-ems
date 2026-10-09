"""Strict, versioned persistence codec for Supervisor Active executor records.

The persistence boundary is deliberately independent from Home Assistant and
transport I/O.  It accepts explicitly supported closed JSON-compatible schemas,
writes every current field explicitly, and rejects unknown, missing, malformed,
non-finite, or unbounded input.  Datetimes are persisted as canonical UTC RFC
3339 strings.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from enum import Enum
import json
from math import isfinite
import re
from typing import Any, Final, TypeVar

if __package__:
    from .supervisor_executor import (
        ActiveState,
        ActuatorIntent,
        AtomicWriteFamily,
        CommandSet,
        EmsBlock,
        EmsMode,
        ExecutionAction,
        ExecutionOwner,
        ExecutionReason,
        ExecutorRecord,
        ExpectedReadback,
        MasterStopResult,
        MasterStopStatus,
        PhysicalVerification,
        RollbackStatus,
        SettingsSnapshot,
        SupervisorExecutor,
        TransactionRecord,
        VerificationStatus,
        _retarget_mode,
    )
else:  # Direct import used by the repository's dependency-free tool tests.
    from supervisor_executor import (  # type: ignore[no-redef]
        ActiveState,
        ActuatorIntent,
        AtomicWriteFamily,
        CommandSet,
        EmsBlock,
        EmsMode,
        ExecutionAction,
        ExecutionOwner,
        ExecutionReason,
        ExecutorRecord,
        ExpectedReadback,
        MasterStopResult,
        MasterStopStatus,
        PhysicalVerification,
        RollbackStatus,
        SettingsSnapshot,
        SupervisorExecutor,
        TransactionRecord,
        VerificationStatus,
        _retarget_mode,
    )


SCHEMA_VERSION: Final = 4
MAX_RECORD_JSON_BYTES: Final = 64 * 1024
MAX_EXECUTOR_REVISION: Final = 2**63 - 1

_UTC_DATETIME = re.compile(
    r"^(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T"
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\."
    r"(?P<microsecond>\d{6})Z$"
)
_TRANSACTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,95}$")

_TOP_KEYS = frozenset({"schema_version", "record"})
_RECORD_KEYS = frozenset(
    {
        "state",
        "owner",
        "transaction",
        "last_transaction",
        "starts_allowed",
        "automatic_policies_enabled",
        "reason",
        "master_stop_result",
        "revision",
    }
)
_TRANSACTION_KEYS_V1 = frozenset(
    {
        "transaction_id",
        "state",
        "intent",
        "owner",
        "started_at",
        "deadline",
        "reason",
        "command_snapshot",
        "prewrite_snapshot",
        "expected_readback",
        "command_sent_at",
        "readback_result",
        "physical_verification",
        "rollback_status",
        "rollback_result",
        "restore_command",
        "restore_snapshot",
        "restore_expected_readback",
        "restore_sent_at",
        "master_stop_requested",
        "off_grid_preserved",
    }
)
_TRANSACTION_KEYS_V2 = _TRANSACTION_KEYS_V1 | frozenset({"restore_attempts"})
_TRANSACTION_KEYS_V3 = _TRANSACTION_KEYS_V2 | frozenset({
    "restore_not_queued_count", "restore_not_queued_first_at",
})
_TRANSACTION_KEYS_V4 = _TRANSACTION_KEYS_V3 | frozenset({
    "lease_identity", "terminal_epoch", "interruption_reason",
})
_LEASE_IDENTITY_KEYS = frozenset({"session_id", "lease_id", "command_generation"})
_INTENT_KEYS = frozenset(
    {
        "policy",
        "action",
        "command",
        "candidate_revision",
        "deadline",
        "physical_verification_required",
    }
)
_COMMAND_KEYS = frozenset(
    {
        "ems_block",
        "export_limit_percent_259",
        "battery_max_charge_power_percent_306",
    }
)
_EMS_BLOCK_KEYS = frozenset(
    {
        "mode",
        "self_use_soc_percent_4301",
        "backup_soc_percent_4302",
        "force_charge_soc_percent_4303",
        "maximum_charge_power_percent_4304",
        "force_discharge_soc_percent_4305",
        "maximum_discharge_power_percent_4306",
    }
)
_SNAPSHOT_KEYS = frozenset(
    {
        "ems_block",
        "ems_generation",
        "ems_observed_at",
        "ems_coherent",
        "gcf_enabled_258",
        "export_limit_percent_259",
        "gcf_generation",
        "gcf_observed_at",
        "gcf_coherent",
        "battery_max_charge_power_percent_306",
        "battery_generation",
        "battery_observed_at",
        "battery_coherent",
    }
)
_EXPECTED_KEYS = frozenset(
    {
        "ems_block",
        "gcf_enabled_258",
        "export_limit_percent_259",
        "battery_max_charge_power_percent_306",
        "base_ems_generation",
        "base_gcf_generation",
        "base_battery_generation",
        "written_families",
    }
)
_PHYSICAL_KEYS = frozenset(
    {"transaction_id", "action", "status", "observed_at", "evidence"}
)
_MASTER_STOP_KEYS = frozenset(
    {
        "status",
        "transaction_id",
        "requested_at",
        "completed_at",
        "off_grid_preserved",
        "reason",
    }
)

_EnumT = TypeVar("_EnumT", bound=Enum)


def _strict_object(value: object, keys: frozenset[str], path: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{path} must be an object")
    result = value
    actual = frozenset(result)
    if actual != keys:
        missing = [key for key in keys if key not in actual]
        extra = [key for key in actual if key not in keys]
        raise ValueError(f"{path} has missing keys {missing!r} or extra keys {extra!r}")
    if any(type(key) is not str for key in result):
        raise ValueError(f"{path} contains a non-string key")
    return result


def _strict_bool(value: object, path: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{path} must be boolean")
    return value


def _strict_int(value: object, path: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{path} must be an integer in [{minimum}, {maximum}]")
    return value


def _strict_number(value: object, path: str) -> float:
    if type(value) not in {int, float}:
        raise ValueError(f"{path} must be a finite JSON number")
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{path} must be a finite JSON number")
    return result


def _strict_string(value: object, path: str, maximum_length: int) -> str:
    if type(value) is not str or len(value) > maximum_length:
        raise ValueError(f"{path} must be a bounded string")
    return value


def _decode_enum(
    enum_type: type[_EnumT],
    value: object,
    path: str,
    *,
    integer_value: bool = False,
) -> _EnumT:
    if integer_value:
        if type(value) is not int:
            raise ValueError(f"{path} must be an integer enum value")
    elif type(value) is not str:
        raise ValueError(f"{path} must be a string enum value")
    try:
        return enum_type(value)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{path} has an unknown enum value") from err


def _encode_enum(value: object, enum_type: type[_EnumT], path: str) -> str | int:
    if not isinstance(value, enum_type):
        raise ValueError(f"{path} is not {enum_type.__name__}")
    encoded = value.value
    if type(encoded) not in {str, int}:
        raise ValueError(f"{path} has an unsupported enum representation")
    return encoded


def _encode_datetime(value: object, path: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{path} must be timezone-aware")
    utc = value.astimezone(timezone.utc)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}T"
        f"{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}."
        f"{utc.microsecond:06d}Z"
    )


def _decode_datetime(value: object, path: str) -> datetime:
    text = _strict_string(value, path, 27)
    match = _UTC_DATETIME.fullmatch(text)
    if match is None:
        raise ValueError(f"{path} must be canonical UTC with six fractional digits and Z")
    parts = {name: int(raw) for name, raw in match.groupdict().items()}
    try:
        return datetime(tzinfo=timezone.utc, **parts)
    except ValueError as err:
        raise ValueError(f"{path} is not a valid UTC datetime") from err


def _optional_datetime(value: object, path: str) -> datetime | None:
    return None if value is None else _decode_datetime(value, path)


def _json_size(value: object) -> int:
    try:
        rendered = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return len(rendered.encode("utf-8"))
    except (OverflowError, TypeError, UnicodeError, ValueError) as err:
        raise ValueError("executor record is not strict JSON") from err


def _require_bounded_json(value: object) -> None:
    size = _json_size(value)
    if size > MAX_RECORD_JSON_BYTES:
        raise ValueError(
            f"executor record JSON exceeds {MAX_RECORD_JSON_BYTES} bytes"
        )


def _encode_ems_block(value: EmsBlock) -> dict[str, object]:
    if type(value) is not EmsBlock:
        raise ValueError("ems_block has an invalid type")
    return {
        "mode": _encode_enum(value.mode, EmsMode, "ems_block.mode"),
        "self_use_soc_percent_4301": _strict_number(
            value.self_use_soc_percent_4301, "ems_block.4301"
        ),
        "backup_soc_percent_4302": _strict_number(
            value.backup_soc_percent_4302, "ems_block.4302"
        ),
        "force_charge_soc_percent_4303": _strict_number(
            value.force_charge_soc_percent_4303, "ems_block.4303"
        ),
        "maximum_charge_power_percent_4304": _strict_number(
            value.maximum_charge_power_percent_4304, "ems_block.4304"
        ),
        "force_discharge_soc_percent_4305": _strict_number(
            value.force_discharge_soc_percent_4305, "ems_block.4305"
        ),
        "maximum_discharge_power_percent_4306": _strict_number(
            value.maximum_discharge_power_percent_4306, "ems_block.4306"
        ),
    }


def _decode_ems_block(value: object, path: str) -> EmsBlock:
    data = _strict_object(value, _EMS_BLOCK_KEYS, path)
    try:
        return EmsBlock(
            mode=_decode_enum(EmsMode, data["mode"], f"{path}.mode", integer_value=True),
            self_use_soc_percent_4301=_strict_number(
                data["self_use_soc_percent_4301"], f"{path}.self_use_soc_percent_4301"
            ),
            backup_soc_percent_4302=_strict_number(
                data["backup_soc_percent_4302"], f"{path}.backup_soc_percent_4302"
            ),
            force_charge_soc_percent_4303=_strict_number(
                data["force_charge_soc_percent_4303"], f"{path}.force_charge_soc_percent_4303"
            ),
            maximum_charge_power_percent_4304=_strict_number(
                data["maximum_charge_power_percent_4304"],
                f"{path}.maximum_charge_power_percent_4304",
            ),
            force_discharge_soc_percent_4305=_strict_number(
                data["force_discharge_soc_percent_4305"],
                f"{path}.force_discharge_soc_percent_4305",
            ),
            maximum_discharge_power_percent_4306=_strict_number(
                data["maximum_discharge_power_percent_4306"],
                f"{path}.maximum_discharge_power_percent_4306",
            ),
        )
    except ValueError as err:
        raise ValueError(f"{path} is invalid: {err}") from err


def _encode_command(value: CommandSet) -> dict[str, object]:
    if type(value) is not CommandSet:
        raise ValueError("command has an invalid type")
    return {
        "ems_block": None if value.ems_block is None else _encode_ems_block(value.ems_block),
        "export_limit_percent_259": (
            None
            if value.export_limit_percent_259 is None
            else _strict_number(value.export_limit_percent_259, "command.259")
        ),
        "battery_max_charge_power_percent_306": (
            None
            if value.battery_max_charge_power_percent_306 is None
            else _strict_number(value.battery_max_charge_power_percent_306, "command.306")
        ),
    }


def _decode_command(value: object, path: str) -> CommandSet:
    data = _strict_object(value, _COMMAND_KEYS, path)
    try:
        return CommandSet(
            ems_block=(
                None
                if data["ems_block"] is None
                else _decode_ems_block(data["ems_block"], f"{path}.ems_block")
            ),
            export_limit_percent_259=(
                None
                if data["export_limit_percent_259"] is None
                else _strict_number(
                    data["export_limit_percent_259"], f"{path}.export_limit_percent_259"
                )
            ),
            battery_max_charge_power_percent_306=(
                None
                if data["battery_max_charge_power_percent_306"] is None
                else _strict_number(
                    data["battery_max_charge_power_percent_306"],
                    f"{path}.battery_max_charge_power_percent_306",
                )
            ),
        )
    except ValueError as err:
        raise ValueError(f"{path} is invalid: {err}") from err


def _encode_snapshot(value: SettingsSnapshot) -> dict[str, object]:
    if type(value) is not SettingsSnapshot:
        raise ValueError("settings snapshot has an invalid type")
    return {
        "ems_block": _encode_ems_block(value.ems_block),
        "ems_generation": _strict_int(value.ems_generation, "snapshot.ems_generation", 1, 16_000_000),
        "ems_observed_at": _encode_datetime(value.ems_observed_at, "snapshot.ems_observed_at"),
        "ems_coherent": _strict_bool(value.ems_coherent, "snapshot.ems_coherent"),
        "gcf_enabled_258": (
            None
            if value.gcf_enabled_258 is None
            else _strict_bool(value.gcf_enabled_258, "snapshot.gcf_enabled_258")
        ),
        "export_limit_percent_259": (
            None
            if value.export_limit_percent_259 is None
            else _strict_number(
                value.export_limit_percent_259, "snapshot.export_limit_percent_259"
            )
        ),
        "gcf_generation": (
            None
            if value.gcf_generation is None
            else _strict_int(
                value.gcf_generation, "snapshot.gcf_generation", 1, 16_000_000
            )
        ),
        "gcf_observed_at": (
            None
            if value.gcf_observed_at is None
            else _encode_datetime(value.gcf_observed_at, "snapshot.gcf_observed_at")
        ),
        "gcf_coherent": _strict_bool(value.gcf_coherent, "snapshot.gcf_coherent"),
        "battery_max_charge_power_percent_306": (
            None
            if value.battery_max_charge_power_percent_306 is None
            else _strict_number(
                value.battery_max_charge_power_percent_306,
                "snapshot.battery_max_charge_power_percent_306",
            )
        ),
        "battery_generation": (
            None
            if value.battery_generation is None
            else _strict_int(
                value.battery_generation,
                "snapshot.battery_generation",
                1,
                16_000_000,
            )
        ),
        "battery_observed_at": (
            None
            if value.battery_observed_at is None
            else _encode_datetime(
                value.battery_observed_at, "snapshot.battery_observed_at"
            )
        ),
        "battery_coherent": _strict_bool(value.battery_coherent, "snapshot.battery_coherent"),
    }


def _decode_snapshot(value: object, path: str) -> SettingsSnapshot:
    data = _strict_object(value, _SNAPSHOT_KEYS, path)
    try:
        return SettingsSnapshot(
            ems_block=_decode_ems_block(data["ems_block"], f"{path}.ems_block"),
            ems_generation=_strict_int(data["ems_generation"], f"{path}.ems_generation", 1, 16_000_000),
            ems_observed_at=_decode_datetime(data["ems_observed_at"], f"{path}.ems_observed_at"),
            ems_coherent=_strict_bool(data["ems_coherent"], f"{path}.ems_coherent"),
            gcf_enabled_258=(
                None
                if data["gcf_enabled_258"] is None
                else _strict_bool(
                    data["gcf_enabled_258"], f"{path}.gcf_enabled_258"
                )
            ),
            export_limit_percent_259=(
                None
                if data["export_limit_percent_259"] is None
                else _strict_number(
                    data["export_limit_percent_259"],
                    f"{path}.export_limit_percent_259",
                )
            ),
            gcf_generation=(
                None
                if data["gcf_generation"] is None
                else _strict_int(
                    data["gcf_generation"],
                    f"{path}.gcf_generation",
                    1,
                    16_000_000,
                )
            ),
            gcf_observed_at=(
                None
                if data["gcf_observed_at"] is None
                else _decode_datetime(
                    data["gcf_observed_at"], f"{path}.gcf_observed_at"
                )
            ),
            gcf_coherent=_strict_bool(data["gcf_coherent"], f"{path}.gcf_coherent"),
            battery_max_charge_power_percent_306=(
                None
                if data["battery_max_charge_power_percent_306"] is None
                else _strict_number(
                    data["battery_max_charge_power_percent_306"],
                    f"{path}.battery_max_charge_power_percent_306",
                )
            ),
            battery_generation=(
                None
                if data["battery_generation"] is None
                else _strict_int(
                    data["battery_generation"],
                    f"{path}.battery_generation",
                    1,
                    16_000_000,
                )
            ),
            battery_observed_at=(
                None
                if data["battery_observed_at"] is None
                else _decode_datetime(
                    data["battery_observed_at"], f"{path}.battery_observed_at"
                )
            ),
            battery_coherent=_strict_bool(data["battery_coherent"], f"{path}.battery_coherent"),
        )
    except ValueError as err:
        raise ValueError(f"{path} is invalid: {err}") from err


def _encode_expected(value: ExpectedReadback) -> dict[str, object]:
    if type(value) is not ExpectedReadback:
        raise ValueError("expected readback has an invalid type")
    families = sorted(
        (_encode_enum(item, AtomicWriteFamily, "expected.written_families") for item in value.written_families),
        key=str,
    )
    return {
        "ems_block": _encode_ems_block(value.ems_block),
        "gcf_enabled_258": (
            None
            if value.gcf_enabled_258 is None
            else _strict_bool(value.gcf_enabled_258, "expected.gcf_enabled_258")
        ),
        "export_limit_percent_259": (
            None
            if value.export_limit_percent_259 is None
            else _strict_number(
                value.export_limit_percent_259, "expected.export_limit_percent_259"
            )
        ),
        "battery_max_charge_power_percent_306": (
            None
            if value.battery_max_charge_power_percent_306 is None
            else _strict_number(
                value.battery_max_charge_power_percent_306,
                "expected.battery_max_charge_power_percent_306",
            )
        ),
        "base_ems_generation": _strict_int(
            value.base_ems_generation, "expected.base_ems_generation", 1, 16_000_000
        ),
        "base_gcf_generation": (
            None
            if value.base_gcf_generation is None
            else _strict_int(
                value.base_gcf_generation,
                "expected.base_gcf_generation",
                1,
                16_000_000,
            )
        ),
        "base_battery_generation": (
            None
            if value.base_battery_generation is None
            else _strict_int(
                value.base_battery_generation,
                "expected.base_battery_generation",
                1,
                16_000_000,
            )
        ),
        "written_families": families,
    }


def _decode_expected(value: object, path: str) -> ExpectedReadback:
    data = _strict_object(value, _EXPECTED_KEYS, path)
    raw_families = data["written_families"]
    if type(raw_families) is not list or not 1 <= len(raw_families) <= 3:
        raise ValueError(f"{path}.written_families must contain one to three values")
    families = tuple(
        _decode_enum(AtomicWriteFamily, item, f"{path}.written_families[{index}]")
        for index, item in enumerate(raw_families)
    )
    if len(frozenset(families)) != len(families):
        raise ValueError(f"{path}.written_families contains duplicates")
    if list(raw_families) != sorted(raw_families):
        raise ValueError(f"{path}.written_families is not canonical")
    try:
        return ExpectedReadback(
            ems_block=_decode_ems_block(data["ems_block"], f"{path}.ems_block"),
            gcf_enabled_258=(
                None
                if data["gcf_enabled_258"] is None
                else _strict_bool(
                    data["gcf_enabled_258"], f"{path}.gcf_enabled_258"
                )
            ),
            export_limit_percent_259=(
                None
                if data["export_limit_percent_259"] is None
                else _strict_number(
                    data["export_limit_percent_259"],
                    f"{path}.export_limit_percent_259",
                )
            ),
            battery_max_charge_power_percent_306=(
                None
                if data["battery_max_charge_power_percent_306"] is None
                else _strict_number(
                    data["battery_max_charge_power_percent_306"],
                    f"{path}.battery_max_charge_power_percent_306",
                )
            ),
            base_ems_generation=_strict_int(
                data["base_ems_generation"], f"{path}.base_ems_generation", 1, 16_000_000
            ),
            base_gcf_generation=(
                None
                if data["base_gcf_generation"] is None
                else _strict_int(
                    data["base_gcf_generation"],
                    f"{path}.base_gcf_generation",
                    1,
                    16_000_000,
                )
            ),
            base_battery_generation=(
                None
                if data["base_battery_generation"] is None
                else _strict_int(
                    data["base_battery_generation"],
                    f"{path}.base_battery_generation",
                    1,
                    16_000_000,
                )
            ),
            written_families=frozenset(families),
        )
    except ValueError as err:
        raise ValueError(f"{path} is invalid: {err}") from err


def _encode_intent(value: ActuatorIntent) -> dict[str, object]:
    if type(value) is not ActuatorIntent:
        raise ValueError("actuator intent has an invalid type")
    return {
        "policy": _encode_enum(value.policy, ExecutionOwner, "intent.policy"),
        "action": _encode_enum(value.action, ExecutionAction, "intent.action"),
        "command": _encode_command(value.command),
        "candidate_revision": _strict_string(
            value.candidate_revision, "intent.candidate_revision", 128
        ),
        "deadline": _encode_datetime(value.deadline, "intent.deadline"),
        "physical_verification_required": _strict_bool(
            value.physical_verification_required,
            "intent.physical_verification_required",
        ),
    }


def _decode_intent(value: object, path: str) -> ActuatorIntent:
    data = _strict_object(value, _INTENT_KEYS, path)
    try:
        return ActuatorIntent(
            policy=_decode_enum(ExecutionOwner, data["policy"], f"{path}.policy"),
            action=_decode_enum(ExecutionAction, data["action"], f"{path}.action"),
            command=_decode_command(data["command"], f"{path}.command"),
            candidate_revision=_strict_string(
                data["candidate_revision"], f"{path}.candidate_revision", 128
            ),
            deadline=_decode_datetime(data["deadline"], f"{path}.deadline"),
            physical_verification_required=_strict_bool(
                data["physical_verification_required"],
                f"{path}.physical_verification_required",
            ),
        )
    except ValueError as err:
        raise ValueError(f"{path} is invalid: {err}") from err


def _encode_physical(value: PhysicalVerification) -> dict[str, object]:
    if type(value) is not PhysicalVerification:
        raise ValueError("physical verification has an invalid type")
    if type(value.evidence) is not tuple or len(value.evidence) > 16:
        raise ValueError("physical verification evidence is invalid")
    evidence = [
        _strict_string(item, f"physical.evidence[{index}]", 160)
        for index, item in enumerate(value.evidence)
    ]
    return {
        "transaction_id": _encode_transaction_id(value.transaction_id, "physical.transaction_id"),
        "action": _encode_enum(value.action, ExecutionAction, "physical.action"),
        "status": _encode_enum(value.status, VerificationStatus, "physical.status"),
        "observed_at": _encode_datetime(value.observed_at, "physical.observed_at"),
        "evidence": evidence,
    }


def _decode_physical(value: object, path: str) -> PhysicalVerification:
    data = _strict_object(value, _PHYSICAL_KEYS, path)
    raw_evidence = data["evidence"]
    if type(raw_evidence) is not list or len(raw_evidence) > 16:
        raise ValueError(f"{path}.evidence must be a list with at most 16 entries")
    evidence = tuple(
        _strict_string(item, f"{path}.evidence[{index}]", 160)
        for index, item in enumerate(raw_evidence)
    )
    try:
        return PhysicalVerification(
            transaction_id=_decode_transaction_id(
                data["transaction_id"], f"{path}.transaction_id"
            ),
            action=_decode_enum(ExecutionAction, data["action"], f"{path}.action"),
            status=_decode_enum(VerificationStatus, data["status"], f"{path}.status"),
            observed_at=_decode_datetime(data["observed_at"], f"{path}.observed_at"),
            evidence=evidence,
        )
    except ValueError as err:
        raise ValueError(f"{path} is invalid: {err}") from err


def _encode_transaction_id(value: object, path: str) -> str:
    text = _strict_string(value, path, 96)
    if _TRANSACTION_ID.fullmatch(text) is None:
        raise ValueError(f"{path} is malformed")
    return text


def _decode_transaction_id(value: object, path: str) -> str:
    return _encode_transaction_id(value, path)


def _encode_master_stop(value: MasterStopResult) -> dict[str, object]:
    if type(value) is not MasterStopResult:
        raise ValueError("master_stop_result has an invalid type")
    return {
        "status": _encode_enum(value.status, MasterStopStatus, "master_stop.status"),
        "transaction_id": (
            None
            if value.transaction_id is None
            else _encode_transaction_id(value.transaction_id, "master_stop.transaction_id")
        ),
        "requested_at": (
            None
            if value.requested_at is None
            else _encode_datetime(value.requested_at, "master_stop.requested_at")
        ),
        "completed_at": (
            None
            if value.completed_at is None
            else _encode_datetime(value.completed_at, "master_stop.completed_at")
        ),
        "off_grid_preserved": _strict_bool(
            value.off_grid_preserved, "master_stop.off_grid_preserved"
        ),
        "reason": _encode_enum(value.reason, ExecutionReason, "master_stop.reason"),
    }


def _decode_master_stop(value: object, path: str) -> MasterStopResult:
    data = _strict_object(value, _MASTER_STOP_KEYS, path)
    status = _decode_enum(MasterStopStatus, data["status"], f"{path}.status")
    transaction_id = (
        None
        if data["transaction_id"] is None
        else _decode_transaction_id(data["transaction_id"], f"{path}.transaction_id")
    )
    requested_at = _optional_datetime(data["requested_at"], f"{path}.requested_at")
    completed_at = _optional_datetime(data["completed_at"], f"{path}.completed_at")
    if status is MasterStopStatus.NOT_REQUESTED:
        if transaction_id is not None or requested_at is not None or completed_at is not None:
            raise ValueError(f"{path} has evidence for a non-requested MASTER STOP")
    elif transaction_id is None or requested_at is None:
        raise ValueError(f"{path} lacks MASTER STOP request identity")
    if completed_at is not None and requested_at is not None and completed_at < requested_at:
        raise ValueError(f"{path}.completed_at precedes requested_at")
    return MasterStopResult(
        status=status,
        transaction_id=transaction_id,
        requested_at=requested_at,
        completed_at=completed_at,
        off_grid_preserved=_strict_bool(
            data["off_grid_preserved"], f"{path}.off_grid_preserved"
        ),
        reason=_decode_enum(ExecutionReason, data["reason"], f"{path}.reason"),
    )


def _encode_transaction(
    value: TransactionRecord,
    *,
    schema_version: int,
) -> dict[str, object]:
    if type(value) is not TransactionRecord:
        raise ValueError("transaction record has an invalid type")
    if type(value.master_stop_requested) is not bool or type(value.off_grid_preserved) is not bool:
        raise ValueError("transaction flags must be boolean")
    encoded = {
        "transaction_id": _encode_transaction_id(value.transaction_id, "transaction.transaction_id"),
        "state": _encode_enum(
            _persisted_transaction_state(value),
            ActiveState,
            "transaction.state",
        ),
        "intent": _encode_intent(value.intent),
        "owner": _encode_enum(value.owner, ExecutionOwner, "transaction.owner"),
        "started_at": _encode_datetime(value.started_at, "transaction.started_at"),
        "deadline": _encode_datetime(value.deadline, "transaction.deadline"),
        "reason": _encode_enum(value.reason, ExecutionReason, "transaction.reason"),
        "command_snapshot": (
            None if value.command_snapshot is None else _encode_snapshot(value.command_snapshot)
        ),
        "prewrite_snapshot": (
            None if value.prewrite_snapshot is None else _encode_snapshot(value.prewrite_snapshot)
        ),
        "expected_readback": (
            None if value.expected_readback is None else _encode_expected(value.expected_readback)
        ),
        "command_sent_at": (
            None
            if value.command_sent_at is None
            else _encode_datetime(value.command_sent_at, "transaction.command_sent_at")
        ),
        "readback_result": _encode_enum(
            value.readback_result, VerificationStatus, "transaction.readback_result"
        ),
        "physical_verification": (
            None
            if value.physical_verification is None
            else _encode_physical(value.physical_verification)
        ),
        "rollback_status": _encode_enum(
            value.rollback_status, RollbackStatus, "transaction.rollback_status"
        ),
        "rollback_result": _encode_enum(
            value.rollback_result, VerificationStatus, "transaction.rollback_result"
        ),
        "restore_command": (
            None if value.restore_command is None else _encode_command(value.restore_command)
        ),
        "restore_snapshot": (
            None if value.restore_snapshot is None else _encode_snapshot(value.restore_snapshot)
        ),
        "restore_expected_readback": (
            None
            if value.restore_expected_readback is None
            else _encode_expected(value.restore_expected_readback)
        ),
        "restore_sent_at": (
            None
            if value.restore_sent_at is None
            else _encode_datetime(value.restore_sent_at, "transaction.restore_sent_at")
        ),
        "master_stop_requested": value.master_stop_requested,
        "off_grid_preserved": value.off_grid_preserved,
    }
    if schema_version in {2, 3, 4}:
        encoded["restore_attempts"] = _strict_int(
            value.restore_attempts,
            "transaction.restore_attempts",
            0,
            2,
        )
    if schema_version in {3, 4}:
        encoded["restore_not_queued_count"] = _strict_int(
            value.restore_not_queued_count,
            "transaction.restore_not_queued_count",
            0,
            2,
        )
        encoded["restore_not_queued_first_at"] = (
            None if value.restore_not_queued_first_at is None
            else _encode_datetime(
                value.restore_not_queued_first_at,
                "transaction.restore_not_queued_first_at",
            )
        )
    elif schema_version not in {1, 2}:
        raise ValueError("unsupported executor record schema version")
    if schema_version == 4:
        identity = value.lease_identity
        encoded["lease_identity"] = (
            None if identity is None else {
                "session_id": _strict_string(identity[0], "transaction.lease_session_id", 128),
                "lease_id": _strict_string(identity[1], "transaction.lease_id", 128),
                "command_generation": _strict_int(
                    identity[2], "transaction.lease_command_generation", 1, 2**31 - 1
                ),
            }
        )
        encoded["terminal_epoch"] = (
            None if value.terminal_epoch is None else _strict_int(
                value.terminal_epoch, "transaction.terminal_epoch", 1, 2**32 - 1
            )
        )
        encoded["interruption_reason"] = (
            None if value.interruption_reason is None else _encode_enum(
                value.interruption_reason, ExecutionReason, "transaction.interruption_reason"
            )
        )
    return encoded


def _persisted_transaction_state(value: TransactionRecord) -> ActiveState:
    """Encode RETARGETING with a state understood by the previous executor."""

    if value.state is not ActiveState.RETARGETING:
        return value.state
    return (
        ActiveState.STARTING
        if value.command_sent_at is None
        else ActiveState.WAITING_READBACK
    )


def _decode_transaction(
    value: object,
    path: str,
    *,
    schema_version: int,
) -> TransactionRecord:
    if schema_version == 1:
        transaction_keys = _TRANSACTION_KEYS_V1
    elif schema_version == 2:
        transaction_keys = _TRANSACTION_KEYS_V2
    elif schema_version == 3:
        transaction_keys = _TRANSACTION_KEYS_V3
    elif schema_version == 4:
        transaction_keys = _TRANSACTION_KEYS_V4
    else:
        raise ValueError("unsupported executor record schema version")
    data = _strict_object(value, transaction_keys, path)
    transaction_id = _decode_transaction_id(
        data["transaction_id"], f"{path}.transaction_id"
    )
    intent = _decode_intent(data["intent"], f"{path}.intent")
    started_at = _decode_datetime(data["started_at"], f"{path}.started_at")
    deadline = _decode_datetime(data["deadline"], f"{path}.deadline")
    if deadline <= started_at:
        raise ValueError(f"{path}.deadline must follow started_at")
    physical = (
        None
        if data["physical_verification"] is None
        else _decode_physical(data["physical_verification"], f"{path}.physical_verification")
    )
    if physical is not None and (
        physical.transaction_id != transaction_id or physical.action is not intent.action
    ):
        raise ValueError(f"{path}.physical_verification belongs to another transaction")
    expected = (
        None
        if data["expected_readback"] is None
        else _decode_expected(data["expected_readback"], f"{path}.expected_readback")
    )
    if expected is not None and expected.written_families != intent.command.families:
        raise ValueError(f"{path}.expected_readback does not match the command families")
    rollback_status = _decode_enum(
        RollbackStatus, data["rollback_status"], f"{path}.rollback_status"
    )
    restore_sent_at = _optional_datetime(
        data["restore_sent_at"], f"{path}.restore_sent_at"
    )
    if schema_version == 1:
        # Schema v1 predated bounded retries and persisted prepared writes.
        # Preparation, dispatch, or failure evidence therefore proves exactly
        # one restore attempt, never two.
        restore_attempts = int(
            data["restore_command"] is not None
            or restore_sent_at is not None
            or rollback_status is RollbackStatus.FAILED
        )
    else:
        restore_attempts = _strict_int(
            data["restore_attempts"], f"{path}.restore_attempts", 0, 2
        )
    restore_not_queued_count = (
        _strict_int(
            data["restore_not_queued_count"],
            f"{path}.restore_not_queued_count",
            0,
            2,
        ) if schema_version in {3, 4} else 0
    )
    restore_not_queued_first_at = (
        _optional_datetime(
            data["restore_not_queued_first_at"],
            f"{path}.restore_not_queued_first_at",
        ) if schema_version in {3, 4} else None
    )
    identity = None
    terminal_epoch = None
    interruption_reason = None
    if schema_version == 4:
        raw_identity = data["lease_identity"]
        if raw_identity is not None:
            decoded_identity = _strict_object(
                raw_identity, _LEASE_IDENTITY_KEYS, f"{path}.lease_identity"
            )
            session_id = _strict_string(
                decoded_identity["session_id"], f"{path}.lease_identity.session_id", 128
            )
            lease_id = _strict_string(
                decoded_identity["lease_id"], f"{path}.lease_identity.lease_id", 128
            )
            if not session_id or not lease_id:
                raise ValueError(f"{path}.lease_identity is empty")
            identity = (
                session_id, lease_id,
                _strict_int(
                    decoded_identity["command_generation"],
                    f"{path}.lease_identity.command_generation", 1, 2**31 - 1,
                ),
            )
        terminal_epoch = (
            None if data["terminal_epoch"] is None else _strict_int(
                data["terminal_epoch"], f"{path}.terminal_epoch", 1, 2**32 - 1
            )
        )
        if terminal_epoch is not None and identity is None:
            raise ValueError(f"{path}.terminal_epoch has no lease identity")
        interruption_reason = (
            None if data["interruption_reason"] is None else _decode_enum(
                ExecutionReason, data["interruption_reason"],
                f"{path}.interruption_reason",
            )
        )
    return TransactionRecord(
        transaction_id=transaction_id,
        state=_decode_enum(ActiveState, data["state"], f"{path}.state"),
        intent=intent,
        owner=_decode_enum(ExecutionOwner, data["owner"], f"{path}.owner"),
        started_at=started_at,
        deadline=deadline,
        reason=_decode_enum(ExecutionReason, data["reason"], f"{path}.reason"),
        command_snapshot=(
            None
            if data["command_snapshot"] is None
            else _decode_snapshot(data["command_snapshot"], f"{path}.command_snapshot")
        ),
        prewrite_snapshot=(
            None
            if data["prewrite_snapshot"] is None
            else _decode_snapshot(data["prewrite_snapshot"], f"{path}.prewrite_snapshot")
        ),
        expected_readback=expected,
        command_sent_at=_optional_datetime(data["command_sent_at"], f"{path}.command_sent_at"),
        readback_result=_decode_enum(
            VerificationStatus, data["readback_result"], f"{path}.readback_result"
        ),
        physical_verification=physical,
        rollback_status=rollback_status,
        rollback_result=_decode_enum(
            VerificationStatus, data["rollback_result"], f"{path}.rollback_result"
        ),
        restore_command=(
            None
            if data["restore_command"] is None
            else _decode_command(data["restore_command"], f"{path}.restore_command")
        ),
        restore_snapshot=(
            None
            if data["restore_snapshot"] is None
            else _decode_snapshot(data["restore_snapshot"], f"{path}.restore_snapshot")
        ),
        restore_expected_readback=(
            None
            if data["restore_expected_readback"] is None
            else _decode_expected(
                data["restore_expected_readback"], f"{path}.restore_expected_readback"
            )
        ),
        restore_sent_at=restore_sent_at,
        restore_attempts=restore_attempts,
        restore_not_queued_count=restore_not_queued_count,
        restore_not_queued_first_at=restore_not_queued_first_at,
        lease_identity=identity,
        terminal_epoch=terminal_epoch,
        interruption_reason=interruption_reason,
        master_stop_requested=_strict_bool(
            data["master_stop_requested"], f"{path}.master_stop_requested"
        ),
        off_grid_preserved=_strict_bool(
            data["off_grid_preserved"], f"{path}.off_grid_preserved"
        ),
    )


def _encode_record(
    value: ExecutorRecord,
    *,
    schema_version: int,
) -> dict[str, object]:
    if type(value) is not ExecutorRecord:
        raise ValueError("record must be ExecutorRecord")
    persisted_state = value.state
    if value.state is ActiveState.RETARGETING:
        transaction = value.transaction
        if transaction is None:
            raise ValueError("retargeting record has no transaction")
        persisted_state = _persisted_transaction_state(transaction)
    return {
        "state": _encode_enum(persisted_state, ActiveState, "record.state"),
        "owner": _encode_enum(value.owner, ExecutionOwner, "record.owner"),
        "transaction": (
            None
            if value.transaction is None
            else _encode_transaction(value.transaction, schema_version=schema_version)
        ),
        "last_transaction": (
            None
            if value.last_transaction is None
            else _encode_transaction(
                value.last_transaction,
                schema_version=schema_version,
            )
        ),
        "starts_allowed": _strict_bool(value.starts_allowed, "record.starts_allowed"),
        "automatic_policies_enabled": _strict_bool(
            value.automatic_policies_enabled, "record.automatic_policies_enabled"
        ),
        "reason": _encode_enum(value.reason, ExecutionReason, "record.reason"),
        "master_stop_result": _encode_master_stop(value.master_stop_result),
        "revision": _strict_int(
            value.revision, "record.revision", 0, MAX_EXECUTOR_REVISION
        ),
    }


def _decode_record(
    value: object,
    path: str,
    *,
    schema_version: int,
) -> ExecutorRecord:
    data = _strict_object(value, _RECORD_KEYS, path)
    record = ExecutorRecord(
        state=_decode_enum(ActiveState, data["state"], f"{path}.state"),
        owner=_decode_enum(ExecutionOwner, data["owner"], f"{path}.owner"),
        transaction=(
            None
            if data["transaction"] is None
            else _decode_transaction(
                data["transaction"],
                f"{path}.transaction",
                schema_version=schema_version,
            )
        ),
        last_transaction=(
            None
            if data["last_transaction"] is None
            else _decode_transaction(
                data["last_transaction"],
                f"{path}.last_transaction",
                schema_version=schema_version,
            )
        ),
        starts_allowed=_strict_bool(data["starts_allowed"], f"{path}.starts_allowed"),
        automatic_policies_enabled=_strict_bool(
            data["automatic_policies_enabled"],
            f"{path}.automatic_policies_enabled",
        ),
        reason=_decode_enum(ExecutionReason, data["reason"], f"{path}.reason"),
        master_stop_result=_decode_master_stop(
            data["master_stop_result"], f"{path}.master_stop_result"
        ),
        revision=_strict_int(
            data["revision"], f"{path}.revision", 0, MAX_EXECUTOR_REVISION
        ),
    )
    if schema_version >= 2:
        record = _lift_retarget_alias(record)
    try:
        SupervisorExecutor(record)
    except (TypeError, ValueError) as err:
        raise ValueError(f"{path} violates executor invariants: {err}") from err
    return record


def _lift_retarget_alias(record: ExecutorRecord) -> ExecutorRecord:
    """Lift only the strong persisted marker for a supported same-run retarget."""

    transaction = record.transaction
    if (
        transaction is None
        or record.state not in {
            ActiveState.STARTING,
            ActiveState.WAITING_READBACK,
        }
        or transaction.state is not record.state
        or record.owner is not transaction.owner
        or transaction.owner is not transaction.intent.policy
        or _retarget_mode(transaction.intent) is None
        or transaction.intent.command.families
        != frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
        or transaction.intent.command.ems_block is None
        or transaction.command_snapshot is None
        or transaction.prewrite_snapshot is None
        or transaction.expected_readback is None
        or transaction.prewrite_snapshot.ems_block.mode is not _retarget_mode(transaction.intent)
        or transaction.prewrite_snapshot.ems_generation
        == transaction.command_snapshot.ems_generation
        or transaction.expected_readback.base_ems_generation
        != transaction.prewrite_snapshot.ems_generation
        or transaction.expected_readback.ems_block
        != transaction.intent.command.ems_block
        or transaction.expected_readback.written_families
        != frozenset({AtomicWriteFamily.EMS_COMPLETE_BLOCK})
        # The controller persists both the sent/pending phase and the narrow
        # FC03-matched/physical-proof-pending phase before EXECUTING.
        or transaction.readback_result
        not in {VerificationStatus.PENDING, VerificationStatus.CONFIRMED}
        or transaction.rollback_status is not RollbackStatus.PENDING
        or transaction.rollback_result is not VerificationStatus.PENDING
        or transaction.restore_command is not None
        or transaction.restore_snapshot is not None
        or transaction.restore_expected_readback is not None
        or transaction.restore_sent_at is not None
        or transaction.restore_attempts != 0
        or transaction.master_stop_requested
    ):
        return record
    if record.state is ActiveState.STARTING:
        if (
            transaction.command_sent_at is not None
            or transaction.reason is not ExecutionReason.COMMAND_READY
            or transaction.readback_result is not VerificationStatus.PENDING
            or transaction.physical_verification is not None
        ):
            return record
    else:
        if transaction.command_sent_at is None:
            return record
        verification = transaction.physical_verification
        if transaction.readback_result is VerificationStatus.PENDING:
            phase_exact = bool(
                verification is None
                and transaction.reason
                in {
                    ExecutionReason.WAITING_READBACK,
                    ExecutionReason.READBACK_PENDING,
                }
            )
        elif verification is None:
            phase_exact = transaction.reason is ExecutionReason.WAITING_PHYSICAL
        else:
            phase_exact = bool(
                verification.status is VerificationStatus.PENDING
                and verification.transaction_id == transaction.transaction_id
                and verification.action is transaction.intent.action
                and verification.observed_at > transaction.command_sent_at
                and transaction.reason is ExecutionReason.PHYSICAL_PENDING
            )
        if not phase_exact:
            return record
    retarget = replace(transaction, state=ActiveState.RETARGETING)
    return replace(record, state=ActiveState.RETARGETING, transaction=retarget)


def record_to_dict(record: ExecutorRecord) -> dict[str, object]:
    """Return the canonical, bounded current-schema dictionary for ``record``."""

    payload: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "record": _encode_record(record, schema_version=SCHEMA_VERSION),
    }
    _require_bounded_json(payload)
    # Re-read our own representation so constructor and executor invariants are
    # applied equally to outbound and inbound persistence.
    decoded = _decode_record(
        payload["record"],
        "record",
        schema_version=SCHEMA_VERSION,
    )
    if decoded != record:
        raise ValueError(
            f"record is not exactly representable by schema v{SCHEMA_VERSION}"
        )
    return payload


def record_from_dict(payload: object) -> ExecutorRecord:
    """Decode a supported canonical schema, failing closed on divergence."""

    _require_bounded_json(payload)
    data = _strict_object(payload, _TOP_KEYS, "root")
    version = _strict_int(
        data["schema_version"], "root.schema_version", 1, SCHEMA_VERSION
    )
    if version not in {1, 2, 3, SCHEMA_VERSION}:
        raise ValueError("unsupported executor record schema version")
    record = _decode_record(
        data["record"],
        "root.record",
        schema_version=version,
    )
    canonical = {
        "schema_version": version,
        "record": _encode_record(record, schema_version=version),
    }
    if canonical != data:
        raise ValueError(f"executor record is not canonical schema v{version}")
    return record


__all__ = (
    "MAX_EXECUTOR_REVISION",
    "MAX_RECORD_JSON_BYTES",
    "SCHEMA_VERSION",
    "record_from_dict",
    "record_to_dict",
)
