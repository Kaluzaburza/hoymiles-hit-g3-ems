"""Copy-once migration adapter for neutral EMS configuration helpers.

The integration lifecycle invokes this adapter once per config-entry setup.
Missing or unavailable target helpers remain pending and cause no write; a
later reload retries the same durable plan.  The ledger makes the one-way
legacy-to-shared contract deterministic, upgradeable, and restart-safe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Set
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import json
from math import isfinite
from pathlib import Path
import re
from typing import Any


MIGRATION_VERSION = 2
_MIN_SUPPORTED_MIGRATION_VERSION = 1
MIGRATION_STORAGE_KEY = "hoymiles_hit_modbus.ems_shared_input_migration"
_MIGRATION_LOCK_DATA_KEY = "_hoymiles_hit_modbus_shared_input_migration_lock"

_ENTITY_ID_RE = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
_RATED_POWER_RE = re.compile(
    r"(5|8|10|12|15|20)(?:\s*kW)?",
    re.IGNORECASE,
)
_AUTOMATIC_OPTIONS = frozenset({"auto", "automatic", "automatycznie"})
_UNAVAILABLE = frozenset({"unknown", "unavailable", "none", "niedostępne"})

_TERMINAL_STATUSES = frozenset(
    {
        "copied_legacy",
        "preserved_new",
        "preserved_changed",
        "preserved_invalid_new",
        "storage_unreadable",
        "legacy_not_restored",
        "no_legacy_value",
        "invalid_legacy",
    }
)
_ERROR_STATUSES = frozenset(
    {
        "preserved_invalid_new",
        "storage_unreadable",
        "invalid_legacy",
        "write_failed",
    }
)


@dataclass(frozen=True, slots=True)
class MigrationFieldSpec:
    """One frozen one-way source-to-target migration mapping."""

    key: str
    target_entity_id: str
    legacy_entity_id: str
    kind: str
    introduced_version: int = 1


MIGRATION_FIELD_SPECS = (
    MigrationFieldSpec(
        "forecast_today",
        "input_text.hoymiles_ems_pv_forecast_today_entity",
        "input_text.hoymiles_solcast_forecast_today_entity",
        "entity_id",
    ),
    MigrationFieldSpec(
        "forecast_tomorrow",
        "input_text.hoymiles_ems_pv_forecast_tomorrow_entity",
        "input_text.hoymiles_solcast_forecast_tomorrow_entity",
        "entity_id",
    ),
    MigrationFieldSpec(
        "forecast_day3",
        "input_text.hoymiles_ems_pv_forecast_day_3_entity",
        "input_text.hoymiles_solcast_forecast_day_3_entity",
        "entity_id",
    ),
    MigrationFieldSpec(
        "fallback_daily_home_load",
        "input_number.hoymiles_ems_fallback_daily_home_load",
        "input_number.hoymiles_rce_fallback_daily_load",
        "positive_number",
    ),
    MigrationFieldSpec(
        "inverter_rated_power_each",
        "input_select.hoymiles_ems_inverter_rated_power_each",
        "input_select.hoymiles_rce_inverter_rated_power",
        "rated_power",
    ),
    MigrationFieldSpec(
        "pv_to_battery_efficiency",
        "input_number.hoymiles_ems_pv_to_battery_efficiency",
        "input_number.hoymiles_tariff_charge_efficiency",
        "efficiency_percent",
        2,
    ),
    MigrationFieldSpec(
        "battery_to_home_efficiency",
        "input_number.hoymiles_ems_battery_to_home_efficiency",
        "input_number.hoymiles_tariff_discharge_efficiency",
        "efficiency_percent",
        2,
    ),
)
_SPEC_BY_TARGET = {spec.target_entity_id: spec for spec in MIGRATION_FIELD_SPECS}


@dataclass(frozen=True, slots=True)
class MigrationFieldRecord:
    """Durable state for one copy-once field."""

    key: str
    target_entity_id: str
    legacy_entity_id: str
    status: str = "not_run"
    expected_value: str | float | None = None
    attempts: int = 0
    updated_at: str | None = None
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class MigrationLedger:
    """Versioned, bounded migration ledger."""

    version: int
    fields: tuple[MigrationFieldRecord, ...]

    @classmethod
    def empty(cls) -> "MigrationLedger":
        return cls(
            version=MIGRATION_VERSION,
            fields=tuple(
                MigrationFieldRecord(
                    key=spec.key,
                    target_entity_id=spec.target_entity_id,
                    legacy_entity_id=spec.legacy_entity_id,
                )
                for spec in MIGRATION_FIELD_SPECS
            ),
        )

    def field(self, target_entity_id: str) -> MigrationFieldRecord:
        return next(
            item for item in self.fields if item.target_entity_id == target_entity_id
        )

    def replace_field(self, record: MigrationFieldRecord) -> "MigrationLedger":
        return replace(
            self,
            fields=tuple(
                record if item.target_entity_id == record.target_entity_id else item
                for item in self.fields
            ),
        )

    @property
    def result(self) -> str:
        if any(field.status in _ERROR_STATUSES for field in self.fields):
            return "completed_with_errors" if all(
                field.terminal for field in self.fields
            ) else "pending_with_errors"
        if all(field.terminal for field in self.fields):
            return "complete"
        return "pending"

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "result": self.result,
            "fields": [
                {
                    "key": field.key,
                    "target_entity_id": field.target_entity_id,
                    "legacy_entity_id": field.legacy_entity_id,
                    "status": field.status,
                    "expected_value": field.expected_value,
                    "attempts": field.attempts,
                    "updated_at": field.updated_at,
                    "error": field.error,
                }
                for field in self.fields
            ],
        }

    def as_public_dict(self) -> dict[str, Any]:
        seeded_values = {
            field.key: field.expected_value
            for field in self.fields
            if field.status == "copied_legacy"
            and field.expected_value is not None
        }
        return {
            "version": self.version,
            "result": self.result,
            "fields": {
                field.key: field.status
                for field in self.fields
            },
            "seeded_values": seeded_values,
            "provenance": {
                field.key: (
                    "migration_seed_from_legacy"
                    if field.status == "copied_legacy"
                    else "neutral_helper"
                    if field.status
                    in {"preserved_new", "preserved_changed", "preserved_invalid_new"}
                    else "unavailable"
                )
                for field in self.fields
            },
        }


@dataclass(frozen=True, slots=True)
class MigrationWrite:
    """One planned HA helper service call."""

    target_entity_id: str
    value: str | float
    baseline_value: Any = None
    baseline_context_id: str | None = None
    baseline_last_updated: datetime | None = None
    source_entity_id: str | None = None
    source_value: Any = None
    source_context_id: str | None = None
    source_last_updated: datetime | None = None


@dataclass(frozen=True, slots=True)
class MigrationObservation:
    """One target snapshot used to reject stale migration writes."""

    value: Any
    yaml_owned: bool
    context_id: str | None
    last_updated: datetime | None


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """Updated pre-write ledger and at most one write per field."""

    ledger: MigrationLedger
    writes: tuple[MigrationWrite, ...]


class UnsupportedMigrationVersion(ValueError):
    """A newer durable ledger must never re-arm copy-once migration."""


def migration_ledger_from_dict(raw: Any) -> MigrationLedger:
    """Decode a supported ledger; only an absent store starts clean."""

    if raw is None:
        return MigrationLedger.empty()
    if not isinstance(raw, Mapping):
        raise ValueError("invalid shared EMS migration ledger")
    raw_version = raw.get("version")
    if type(raw_version) is not int:
        raise ValueError("shared EMS migration ledger lacks a version")
    if raw_version > MIGRATION_VERSION:
        raise UnsupportedMigrationVersion(
            "shared EMS migration ledger is newer than code"
        )
    if raw_version < _MIN_SUPPORTED_MIGRATION_VERSION:
        raise ValueError("unsupported shared EMS migration ledger version")
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list):
        raise ValueError("shared EMS migration ledger fields are invalid")
    expected_targets = {
        spec.target_entity_id
        for spec in MIGRATION_FIELD_SPECS
        if spec.introduced_version <= raw_version
    }
    raw_targets = [
        item.get("target_entity_id")
        for item in raw_fields
        if isinstance(item, Mapping)
    ]
    if (
        len(raw_targets) != len(raw_fields)
        or len(set(raw_targets)) != len(raw_targets)
        or set(raw_targets) != expected_targets
    ):
        raise ValueError("shared EMS migration ledger field set is invalid")
    by_target = {
        str(item.get("target_entity_id")): item
        for item in raw_fields
        if isinstance(item, Mapping)
    }
    records: list[MigrationFieldRecord] = []
    for spec in MIGRATION_FIELD_SPECS:
        item = by_target.get(spec.target_entity_id)
        if item is None:
            if spec.introduced_version <= raw_version:
                raise ValueError(
                    "shared EMS migration ledger required field is missing"
                )
            records.append(
                MigrationFieldRecord(
                    key=spec.key,
                    target_entity_id=spec.target_entity_id,
                    legacy_entity_id=spec.legacy_entity_id,
                )
            )
            continue
        attempts = item.get("attempts", 0)
        attempts = attempts if type(attempts) is int and 0 <= attempts <= 1000 else 0
        status = str(item.get("status", "not_run"))[:64]
        expected = _validated_value(spec, item.get("expected_value"))
        error = (
            str(item.get("error"))[:128]
            if item.get("error") is not None
            else None
        )
        allowed_statuses = {
            "not_run",
            "target_missing",
            "target_unavailable",
            "legacy_unavailable",
            "copy_pending",
            "verification_failed",
            "write_failed",
            *_TERMINAL_STATUSES,
        }
        if status not in allowed_statuses or (
            status
            in {"copy_pending", "verification_failed", "write_failed", "copied_legacy"}
            and expected is None
        ):
            status = "preserved_invalid_new"
            expected = None
            error = "ledger_record_invalid"
        records.append(
            MigrationFieldRecord(
                key=spec.key,
                target_entity_id=spec.target_entity_id,
                legacy_entity_id=spec.legacy_entity_id,
                status=status,
                expected_value=expected,
                attempts=attempts,
                updated_at=(
                    str(item.get("updated_at"))[:64]
                    if item.get("updated_at") is not None
                    else None
                ),
                error=error,
            )
        )
    # A v1 ledger is retained field-for-field while the two v2 efficiency
    # records are appended as not_run. Existing copy-once decisions are never
    # replayed merely because the schema gained new fields.
    return MigrationLedger(MIGRATION_VERSION, tuple(records))


def _timestamp(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("migration timestamp must be timezone-aware")
    return now.astimezone(timezone.utc).isoformat()


def _storage_unreadable_ledger(now: datetime) -> MigrationLedger:
    """Create a durable tombstone that can never authorize a migration write."""

    updated_at = _timestamp(now)
    return MigrationLedger(
        version=MIGRATION_VERSION,
        fields=tuple(
            MigrationFieldRecord(
                key=spec.key,
                target_entity_id=spec.target_entity_id,
                legacy_entity_id=spec.legacy_entity_id,
                status="storage_unreadable",
                updated_at=updated_at,
                error="durable_ledger_unreadable",
            )
            for spec in MIGRATION_FIELD_SPECS
        ),
    )


def _raw_state(states: Mapping[str, Any], entity_id: str) -> tuple[bool, Any]:
    return entity_id in states, states.get(entity_id)


def _is_unavailable(value: Any) -> bool:
    return value is None or str(value).strip().casefold() in _UNAVAILABLE


def _target_is_unset(spec: MigrationFieldSpec, value: Any) -> bool:
    if spec.kind == "entity_id":
        return (
            value is not None
            and str(value).strip().casefold() in {"", "unknown"}
        )
    if spec.kind == "positive_number":
        try:
            return float(value) == 0.0
        except (TypeError, ValueError):
            return False
    if spec.kind == "efficiency_percent":
        try:
            return float(value) == 0.0
        except (TypeError, ValueError):
            return False
    if spec.kind == "rated_power":
        return str(value or "").strip().casefold() in _AUTOMATIC_OPTIONS
    return False


def _validated_value(spec: MigrationFieldSpec, value: Any) -> str | float | None:
    if value is None:
        return None
    if spec.kind == "entity_id":
        normalized = str(value).strip().casefold()
        return normalized if _ENTITY_ID_RE.fullmatch(normalized) else None
    if spec.kind == "positive_number":
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if isfinite(parsed) and 0.0 < parsed <= 200.0 else None
    if spec.kind == "efficiency_percent":
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if isfinite(parsed) and 0.0 < parsed <= 100.0 else None
    if spec.kind == "rated_power":
        match = _RATED_POWER_RE.fullmatch(str(value).strip())
        return f"{match.group(1)} kW" if match is not None else None
    return None


def _values_equal(spec: MigrationFieldSpec, left: Any, right: Any) -> bool:
    return (
        _validated_value(spec, left) is not None
        and _validated_value(spec, left) == _validated_value(spec, right)
    )


def plan_copy_once(
    ledger: MigrationLedger,
    states: Mapping[str, Any],
    *,
    now: datetime,
    blocked_target_entity_ids: Set[str] = frozenset(),
    restored_entity_ids: Set[str] | None = None,
    trusted_seeded_source_values: Mapping[str, Any] | None = None,
) -> MigrationPlan:
    """Plan one idempotent pass without changing HA or the supplied ledger."""

    updated = ledger
    writes: list[MigrationWrite] = []
    updated_at = _timestamp(now)
    for spec in MIGRATION_FIELD_SPECS:
        record = updated.field(spec.target_entity_id)
        if record.terminal:
            continue
        if spec.target_entity_id in blocked_target_entity_ids:
            # The first-install default ledger has not yet committed the
            # corresponding legacy source. Keep this migration retryable and
            # keep the legacy listener window open instead of copying an HA
            # framework fallback as if it were a user value.
            updated = updated.replace_field(
                replace(
                    record,
                    status="target_unavailable",
                    updated_at=updated_at,
                    error="default_seed_pending",
                )
            )
            continue

        target_exists, target_raw = _raw_state(states, spec.target_entity_id)
        if not target_exists:
            updated = updated.replace_field(
                replace(record, status="target_missing", updated_at=updated_at)
            )
            continue
        if restored_entity_ids is not None and spec.target_entity_id in restored_entity_ids:
            valid_target = _validated_value(spec, target_raw)
            updated = updated.replace_field(
                replace(
                    record,
                    status=(
                        "preserved_new"
                        if valid_target is not None
                        else "preserved_invalid_new"
                    ),
                    expected_value=None,
                    updated_at=updated_at,
                    error="restored_target",
                )
            )
            continue
        target_unset = _target_is_unset(spec, target_raw)
        if _is_unavailable(target_raw) and not target_unset:
            updated = updated.replace_field(
                replace(record, status="target_unavailable", updated_at=updated_at)
            )
            continue

        if record.status in {"copy_pending", "write_failed", "verification_failed"} and record.expected_value is not None:
            if _values_equal(spec, target_raw, record.expected_value):
                updated = updated.replace_field(
                    replace(
                        record,
                        status="copied_legacy",
                        updated_at=updated_at,
                        error=None,
                    )
                )
                continue
            if not target_unset:
                updated = updated.replace_field(
                    replace(
                        record,
                        status="preserved_new",
                        expected_value=None,
                        updated_at=updated_at,
                        error=None,
                    )
                )
                continue
            legacy_exists, legacy_raw = _raw_state(states, spec.legacy_entity_id)
            if not legacy_exists or _is_unavailable(legacy_raw):
                updated = updated.replace_field(
                    replace(
                        record,
                        status="legacy_unavailable",
                        updated_at=updated_at,
                        error="authorized_legacy_source_unavailable",
                    )
                )
                continue
            seeded_source = (
                trusted_seeded_source_values or {}
            ).get(spec.legacy_entity_id)
            source_seeded_here = (
                seeded_source is not None
                and _values_equal(spec, legacy_raw, seeded_source)
            )
            if restored_entity_ids is not None and not (
                spec.legacy_entity_id in restored_entity_ids or source_seeded_here
            ):
                updated = updated.replace_field(
                    replace(
                        record,
                        status="legacy_not_restored",
                        expected_value=None,
                        updated_at=updated_at,
                        error="legacy_source_has_no_restore_provenance",
                    )
                )
                continue
            if not _values_equal(spec, legacy_raw, record.expected_value):
                updated = updated.replace_field(
                    replace(
                        record,
                        status="preserved_changed",
                        expected_value=None,
                        updated_at=updated_at,
                        error="legacy_changed_after_authorization",
                    )
                )
                continue
            pending = replace(
                record,
                status="copy_pending",
                attempts=min(record.attempts + 1, 1000),
                updated_at=updated_at,
                error=None,
            )
            updated = updated.replace_field(pending)
            writes.append(
                MigrationWrite(
                    spec.target_entity_id,
                    record.expected_value,
                    baseline_value=target_raw,
                    source_entity_id=spec.legacy_entity_id,
                    source_value=legacy_raw,
                )
            )
            continue

        if not target_unset:
            valid_target = _validated_value(spec, target_raw)
            status = "preserved_new" if valid_target is not None else "preserved_invalid_new"
            updated = updated.replace_field(
                replace(record, status=status, updated_at=updated_at)
            )
            continue

        legacy_exists, legacy_raw = _raw_state(states, spec.legacy_entity_id)
        if not legacy_exists or _is_unavailable(legacy_raw):
            updated = updated.replace_field(
                replace(record, status="legacy_unavailable", updated_at=updated_at)
            )
            continue
        seeded_source = (
            trusted_seeded_source_values or {}
        ).get(spec.legacy_entity_id)
        source_seeded_here = (
            seeded_source is not None
            and _values_equal(spec, legacy_raw, seeded_source)
        )
        if restored_entity_ids is not None and not (
            spec.legacy_entity_id in restored_entity_ids or source_seeded_here
        ):
            updated = updated.replace_field(
                replace(
                    record,
                    status="legacy_not_restored",
                    updated_at=updated_at,
                    error="legacy_source_has_no_restore_provenance",
                )
            )
            continue
        if spec.kind == "entity_id" and str(legacy_raw).strip() == "":
            updated = updated.replace_field(
                replace(record, status="no_legacy_value", updated_at=updated_at)
            )
            continue
        legacy_value = _validated_value(spec, legacy_raw)
        if legacy_value is None:
            updated = updated.replace_field(
                replace(record, status="invalid_legacy", updated_at=updated_at)
            )
            continue

        pending = replace(
            record,
            status="copy_pending",
            expected_value=legacy_value,
            attempts=min(record.attempts + 1, 1000),
            updated_at=updated_at,
            error=None,
        )
        updated = updated.replace_field(pending)
        writes.append(
            MigrationWrite(
                spec.target_entity_id,
                legacy_value,
                baseline_value=target_raw,
                source_entity_id=spec.legacy_entity_id,
                source_value=legacy_raw,
            )
        )
    return MigrationPlan(updated, tuple(writes))


def complete_migration_write(
    ledger: MigrationLedger,
    *,
    target_entity_id: str,
    observed_value: Any,
    now: datetime,
) -> MigrationLedger:
    """Commit a pending copy only after exact target readback."""

    spec = _SPEC_BY_TARGET[target_entity_id]
    record = ledger.field(target_entity_id)
    if record.status != "copy_pending" or record.expected_value is None:
        return ledger
    if _values_equal(spec, observed_value, record.expected_value):
        status = "copied_legacy"
        error = None
    else:
        status = "verification_failed"
        error = "target_readback_mismatch"
    return ledger.replace_field(
        replace(
            record,
            status=status,
            updated_at=_timestamp(now),
            error=error,
        )
    )


def fail_migration_write(
    ledger: MigrationLedger,
    *,
    target_entity_id: str,
    error: str,
    now: datetime,
) -> MigrationLedger:
    """Record a bounded retryable service failure."""

    record = ledger.field(target_entity_id)
    if record.status != "copy_pending":
        return ledger
    return ledger.replace_field(
        replace(
            record,
            status="write_failed",
            updated_at=_timestamp(now),
            error=str(error)[:128],
        )
    )


class EMSSharedInputMigrationCoordinator:
    """Lifecycle-owned HA adapter for one-way, copy-once helper seeding."""

    def __init__(self, hass: Any, store: Any) -> None:
        self.hass = hass
        self._store = store

    @classmethod
    def for_home_assistant(cls, hass: Any) -> "EMSSharedInputMigrationCoordinator":
        """Construct the version-aware HA Store lazily for pure-test isolation."""

        return cls(hass, EMSSharedInputMigrationStore(hass))

    def _observations(self) -> dict[str, MigrationObservation]:
        entity_ids = {
            entity_id
            for spec in MIGRATION_FIELD_SPECS
            for entity_id in (spec.target_entity_id, spec.legacy_entity_id)
        }
        result: dict[str, MigrationObservation] = {}
        for entity_id in entity_ids:
            state = self.hass.states.get(entity_id)
            if state is not None:
                context = getattr(state, "context", None)
                context_raw_id = getattr(context, "id", None)
                last_updated = getattr(state, "last_updated", None)
                attributes = getattr(state, "attributes", {})
                result[entity_id] = MigrationObservation(
                    value=state.state,
                    yaml_owned=(
                        isinstance(attributes, Mapping)
                        and attributes.get("editable") is False
                    ),
                    context_id=(
                        str(context_raw_id)
                        if context_raw_id is not None
                        else None
                    ),
                    last_updated=(
                        last_updated
                        if isinstance(last_updated, datetime)
                        else None
                    ),
                )
        return result

    def _restored_entity_ids(self) -> frozenset[str]:
        from homeassistant.helpers.restore_state import async_get  # noqa: PLC0415

        return frozenset(async_get(self.hass).last_states)

    def _install_lock(self) -> asyncio.Lock:
        lock = self.hass.data.get(_MIGRATION_LOCK_DATA_KEY)
        if lock is None:
            lock = asyncio.Lock()
            self.hass.data[_MIGRATION_LOCK_DATA_KEY] = lock
        if not isinstance(lock, asyncio.Lock):
            raise RuntimeError("invalid shared EMS migration lock")
        return lock

    async def async_run_copy_once(
        self,
        *,
        now: datetime,
        blocked_target_entity_ids: Set[str] = frozenset(),
        restored_entity_ids: Set[str] | None = None,
        trusted_seeded_source_values: Mapping[str, Any] | None = None,
    ) -> MigrationLedger:
        """Run one bounded lifecycle pass with durable readback verification."""

        async with self._install_lock():
            load_with_presence = getattr(
                self._store,
                "async_load_with_presence",
                None,
            )
            if callable(load_with_presence):
                storage_present, raw_ledger = await load_with_presence()
            else:
                # Pure-test/in-memory stores explicitly use ``None`` to mean
                # that no durable ledger has ever existed.
                storage_present = False
                raw_ledger = await self._store.async_load()
            if storage_present and raw_ledger is None:
                # Store may quarantine a corrupt file. Persist a new primary
                # tombstone before returning so a second config entry or the
                # next restart cannot mistake the quarantined key for a first
                # run and replay legacy values.
                ledger = _storage_unreadable_ledger(now)
                await self._store.async_save(ledger.as_dict())
                return ledger
            ledger = migration_ledger_from_dict(raw_ledger)
            # Copy-once is bootstrap-only. A runtime reload waits for the next
            # restart instead of racing a user who is editing the helpers.
            if bool(getattr(self.hass, "is_running", True)):
                return ledger
            restore_ids = (
                restored_entity_ids
                if restored_entity_ids is not None
                else self._restored_entity_ids()
            )
            observations = self._observations()
            plan = plan_copy_once(
                ledger,
                {
                    entity_id: observation.value
                    for entity_id, observation in observations.items()
                },
                now=now,
                blocked_target_entity_ids=blocked_target_entity_ids,
                restored_entity_ids=restore_ids,
                trusted_seeded_source_values=trusted_seeded_source_values,
            )
            ledger = plan.ledger
            writes = tuple(
                replace(
                    write,
                    baseline_context_id=observations[
                        write.target_entity_id
                    ].context_id,
                    baseline_last_updated=observations[
                        write.target_entity_id
                    ].last_updated,
                    source_entity_id=spec.legacy_entity_id,
                    source_value=observations[spec.legacy_entity_id].value,
                    source_context_id=observations[spec.legacy_entity_id].context_id,
                    source_last_updated=observations[
                        spec.legacy_entity_id
                    ].last_updated,
                )
                for write in plan.writes
                for spec in (_SPEC_BY_TARGET[write.target_entity_id],)
            )
            # Persist copy_pending before the first service call. A crash can
            # then verify or safely retry the same authorized baseline.
            await self._store.async_save(ledger.as_dict())
            for write in writes:
                spec = _SPEC_BY_TARGET[write.target_entity_id]
                current = self._observations().get(write.target_entity_id)
                current_source = self._observations().get(spec.legacy_entity_id)
                seeded_source = (
                    trusted_seeded_source_values or {}
                ).get(spec.legacy_entity_id)
                source_seeded_here = (
                    current_source is not None
                    and seeded_source is not None
                    and _values_equal(spec, current_source.value, seeded_source)
                )
                if (
                    current is None
                    or current.value != write.baseline_value
                    or current.context_id != write.baseline_context_id
                    or current.last_updated != write.baseline_last_updated
                    or not current.yaml_owned
                    or current.context_id is None
                    or current.last_updated is None
                    or write.target_entity_id in restore_ids
                    or not _target_is_unset(spec, current.value)
                    or current_source is None
                    or current_source.value != write.source_value
                    or current_source.context_id != write.source_context_id
                    or current_source.last_updated != write.source_last_updated
                    or current_source.context_id is None
                    or current_source.last_updated is None
                    or not (
                        spec.legacy_entity_id in restore_ids
                        or source_seeded_here
                    )
                ):
                    record = ledger.field(write.target_entity_id)
                    ledger = ledger.replace_field(
                        replace(
                            record,
                            status="preserved_changed",
                            expected_value=None,
                            updated_at=_timestamp(now),
                            error=(
                                "legacy_changed_before_write"
                                if current_source is None
                                or current_source.value != write.source_value
                                or current_source.context_id
                                != write.source_context_id
                                or current_source.last_updated
                                != write.source_last_updated
                                else "changed_before_write"
                            ),
                        )
                    )
                    await self._store.async_save(ledger.as_dict())
                    continue
                domain = write.target_entity_id.split(".", 1)[0]
                if domain in {"input_text", "input_number"}:
                    service = "set_value"
                    data = {
                        "entity_id": write.target_entity_id,
                        "value": write.value,
                    }
                elif domain == "input_select":
                    service = "select_option"
                    data = {
                        "entity_id": write.target_entity_id,
                        "option": write.value,
                    }
                else:
                    ledger = fail_migration_write(
                        ledger,
                        target_entity_id=write.target_entity_id,
                        error="unsupported_target_domain",
                        now=now,
                    )
                    await self._store.async_save(ledger.as_dict())
                    continue
                from homeassistant.core import Context  # noqa: PLC0415

                operation_context = Context()
                try:
                    await self.hass.services.async_call(
                        domain,
                        service,
                        data,
                        blocking=True,
                        context=operation_context,
                    )
                except Exception as err:  # noqa: BLE001 - retry is bounded
                    ledger = fail_migration_write(
                        ledger,
                        target_entity_id=write.target_entity_id,
                        error=type(err).__name__,
                        now=now,
                    )
                else:
                    target = self.hass.states.get(write.target_entity_id)
                    target_context = getattr(target, "context", None)
                    context_matches = (
                        target_context is not None
                        and getattr(target_context, "id", None)
                        == operation_context.id
                    )
                    ledger = complete_migration_write(
                        ledger,
                        target_entity_id=write.target_entity_id,
                        observed_value=(
                            target.state
                            if target is not None and context_matches
                            else None
                        ),
                        now=now,
                    )
                await self._store.async_save(ledger.as_dict())
            return ledger


class EMSSharedInputMigrationStore:
    """Factory facade for the real HA Store with an explicit v1→v2 migrator."""

    def __new__(cls, hass: Any) -> Any:
        from homeassistant.helpers.storage import Store  # noqa: PLC0415

        class _Store(Store):
            async def async_load_with_presence(self) -> tuple[bool, Any]:
                """Load while distinguishing a missing file from corruption.

                Home Assistant's Store intentionally returns ``None`` both
                for a missing key and for some unreadable/corrupt envelopes.
                The copy-once migration may treat only proven absence as an
                empty ledger, so sample durable presence on both sides of the
                load.  A corrupt file may be quarantined during ``async_load``;
                the pre-load observation still keeps the migration fail-closed.
                """

                storage_path = Path(self.path)

                def _present() -> bool:
                    try:
                        storage_path.lstat()
                    except FileNotFoundError:
                        return False
                    return True

                def _quarantine_after_windows_store_failure() -> bool:
                    """Finish HA's corrupt-file quarantine with a portable name."""

                    try:
                        json.loads(storage_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        suffix = datetime.now(timezone.utc).strftime(
                            "%Y%m%dT%H%M%S%fZ"
                        )
                        storage_path.rename(
                            storage_path.with_name(
                                f"{storage_path.name}.corrupt.{suffix}"
                            )
                        )
                        return True
                    return False

                present_before = await self.hass.async_add_executor_job(_present)
                try:
                    loaded = await self.async_load()
                except OSError:
                    # HA 2026.8.2 builds its quarantine filename from ISO-8601.
                    # On Windows the ':' characters make that rename fail after
                    # JSON corruption has already been positively identified.
                    quarantined = await self.hass.async_add_executor_job(
                        _quarantine_after_windows_store_failure
                    )
                    if not quarantined:
                        raise
                    loaded = None
                present_after = await self.hass.async_add_executor_job(_present)
                return present_before or present_after, loaded

            async def _async_migrate_func(
                self,
                old_major_version: int,
                old_minor_version: int,
                old_data: Any,
            ) -> dict[str, Any]:
                del old_minor_version
                if old_major_version == MIGRATION_VERSION:
                    if not isinstance(old_data, Mapping):
                        raise ValueError("invalid shared EMS migration ledger")
                    return dict(old_data)
                if old_major_version != 1:
                    raise NotImplementedError
                if not isinstance(old_data, Mapping):
                    raise ValueError("invalid v1 shared EMS migration ledger")
                raw = dict(old_data)
                raw["version"] = 1
                return migration_ledger_from_dict(raw).as_dict()

        return _Store(
            hass,
            MIGRATION_VERSION,
            MIGRATION_STORAGE_KEY,
            atomic_writes=True,
        )
