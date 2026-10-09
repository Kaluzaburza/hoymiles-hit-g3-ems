"""One-time defaults for Home Assistant EMS user settings.

YAML ``initial`` values take precedence over ``RestoreEntity`` and therefore
reset user settings on every helper reload.  This module replaces that behavior
with a versioned, write-ahead ledger.  A field is eligible only when Home
Assistant has no prior restore-state record for it, the live entity is owned by
YAML, and its current-session state still has pristine provenance.

The module never infers initialization from a value such as ``0``, ``off`` or
midnight: each of those may be an intentional user value.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Set
from dataclasses import dataclass, replace
from datetime import datetime, time, timezone
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import stat
from typing import Any


DEFAULT_SEED_VERSION = 2
_MIN_SUPPORTED_SEED_VERSION = 1
DEFAULT_SEED_STORAGE_KEY = "hoymiles_hit_modbus.ems_initial_defaults"
DEFAULT_SEED_RULESET_VERSION = 2
_SEED_LOCK_DATA_KEY = "_hoymiles_hit_modbus_initial_default_seed_lock"
_MAX_HELPER_STORE_BYTES = 2 * 1024 * 1024
_MAX_RESTORE_STORE_BYTES = 64 * 1024 * 1024
_HELPER_STORE_DOMAINS = frozenset(
    {"input_boolean", "input_datetime", "input_number"}
)

_UNAVAILABLE = frozenset({"unknown", "unavailable", "none"})
_TERMINAL_STATUSES = frozenset(
    {
        "seeded",
        "preserved_restored",
        "preserved_changed",
        "preserved_non_yaml",
        "preserved_invalid",
    }
)
_ERROR_STATUSES = frozenset({"write_failed", "verification_failed"})
_RETRY_STATUSES = frozenset({"seed_pending", *_ERROR_STATUSES})


@dataclass(frozen=True, slots=True)
class DefaultSeedSpec:
    """One frozen first-install default."""

    key: str
    target_entity_id: str
    kind: str
    default_value: str | float
    fallback_value: str | float
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    introduced_version: int = 1


def _boolean(key: str, object_id: str, default: str) -> DefaultSeedSpec:
    return DefaultSeedSpec(key, f"input_boolean.{object_id}", "boolean", default, "off")


def _time(key: str, object_id: str, default: str) -> DefaultSeedSpec:
    return DefaultSeedSpec(key, f"input_datetime.{object_id}", "time", default, "00:00:00")


def _number(
    key: str,
    object_id: str,
    default: float,
    minimum: float,
    maximum: float,
    step: float,
    introduced_version: int = 1,
) -> DefaultSeedSpec:
    return DefaultSeedSpec(
        key,
        f"input_number.{object_id}",
        "number",
        default,
        minimum,
        minimum,
        maximum,
        step,
        introduced_version,
    )


DEFAULT_SEED_SPECS = (
    _boolean("ev_load_filter_enabled", "hoymiles_ev_load_filter_enabled", "off"),
    # Zero means unconfigured; only the user supplies the charging power.
    _number("ev_charge_power", "hoymiles_ev_charge_power", 0, 0, 50, .1),
    _boolean("pv_charge_delay_enabled", "hoymiles_pv_charge_delay_enabled", "off"),
    _boolean("rce_advanced_view", "hoymiles_rce_advanced_view", "off"),
    _boolean("tariff_advanced_view", "hoymiles_tariff_advanced_view", "off"),
    _boolean("rcm_advanced_view", "hoymiles_rcm_advanced_view", "off"),
    _boolean("tariff_weekend_low_price", "hoymiles_tariff_weekend_low_price", "on"),
    _boolean(
        "tariff_polish_holidays_low_price",
        "hoymiles_tariff_polish_holidays_low_price",
        "on",
    ),
    _time("tariff_cheap_1_start", "hoymiles_tariff_cheap_1_start", "22:00:00"),
    _time("tariff_cheap_1_end", "hoymiles_tariff_cheap_1_end", "06:00:00"),
    _time("tariff_cheap_2_start", "hoymiles_tariff_cheap_2_start", "13:00:00"),
    _time("tariff_cheap_2_end", "hoymiles_tariff_cheap_2_end", "15:00:00"),
    _time("tariff_medium_start", "hoymiles_tariff_medium_start", "07:00:00"),
    _time("tariff_medium_end", "hoymiles_tariff_medium_end", "13:00:00"),
    _number("rcm_charge_efficiency", "hoymiles_rcm_charge_efficiency", 95, 80, 100, 1),
    _number("rcm_export_cap_percent", "hoymiles_rcm_export_cap_percent", 50, 0, 100, 1),
    _number(
        "battery_balancing_interval_days",
        "hoymiles_battery_balancing_interval_days",
        30,
        1,
        180,
        1,
    ),
    _number(
        "battery_balancing_hold_hours",
        "hoymiles_battery_balancing_hold_hours",
        2,
        0.5,
        12,
        0.5,
    ),
    _number("rce_export_efficiency", "hoymiles_rce_export_efficiency", 95, 80, 100, 1),
    _number("tariff_g11_price", "hoymiles_tariff_g11_price", 0.852, 0, 5, 0.001),
    _number("tariff_low_price", "hoymiles_tariff_low_price", 0.617, 0, 5, 0.001),
    _number("tariff_medium_price", "hoymiles_tariff_medium_price", 0.820, 0, 5, 0.001),
    _number("tariff_peak_price", "hoymiles_tariff_peak_price", 1.028, 0, 5, 0.001),
    _number(
        "tariff_charge_efficiency",
        "hoymiles_tariff_charge_efficiency",
        95,
        80,
        100,
        1,
    ),
    _number(
        "tariff_discharge_efficiency",
        "hoymiles_tariff_discharge_efficiency",
        95,
        80,
        100,
        1,
    ),
    _number("tariff_minimum_saving", "hoymiles_tariff_minimum_saving", 1, 0, 2, 0.001),
    _number("tariff_maximum_soc", "hoymiles_tariff_maximum_soc", 100, 50, 100, 1),
    # I2 v2 defaults. Five replace previous YAML initials and the RCE margin
    # is a formerly unseeded setting. The shared fallback is populated later
    # by the existing copy-once legacy-to-neutral migration.
    _number(
        "tariff_requested_charge_power",
        "hoymiles_tariff_requested_charge_power",
        50,
        1,
        100,
        1,
        2,
    ),
    _number(
        "rce_requested_discharge_power",
        "hoymiles_rce_requested_discharge_power",
        50,
        0,
        100,
        1,
        2,
    ),
    _number("rce_soc_safety_margin", "hoymiles_rce_soc_safety_margin", 5, 0, 90, 1, 2),
    # Retained entity id for migration compatibility; the value is now a
    # percentage of protected-period energy demand, not SOC percentage points.
    _number(
        "tariff_soc_safety_margin",
        "hoymiles_tariff_soc_safety_margin",
        5,
        0,
        100,
        1,
        2,
    ),
    _number("rcm_soc_safety_margin", "hoymiles_rcm_soc_safety_margin", 5, 0, 20, 1, 2),
    _number(
        "fallback_daily_home_load",
        "hoymiles_rce_fallback_daily_load",
        20,
        1,
        200,
        0.5,
        2,
    ),
)
_SPEC_BY_TARGET = {spec.target_entity_id: spec for spec in DEFAULT_SEED_SPECS}


def _ruleset_fingerprint() -> str:
    payload = [
        {
            "key": spec.key,
            "target": spec.target_entity_id,
            "kind": spec.kind,
            "default": spec.default_value,
            "fallback": spec.fallback_value,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "step": spec.step,
            "introduced": spec.introduced_version,
        }
        for spec in DEFAULT_SEED_SPECS
    ]
    return sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


DEFAULT_SEED_RULESET_FINGERPRINT = _ruleset_fingerprint()


@dataclass(frozen=True, slots=True)
class FreshInstallEvidence:
    """Fail-closed pre-install proof captured during HA bootstrap."""

    eligible: bool
    reason: str
    scheduler_path: Path
    shared_package_path: Path


@dataclass(frozen=True, slots=True)
class DefaultSeedAuthorization:
    """Durable authorization for one exact freshly installed ruleset."""

    ruleset_version: int
    ruleset_fingerprint: str
    package_version: str
    scheduler_sha256: str
    shared_package_sha256: str
    authorized_at: str

    @property
    def valid(self) -> bool:
        return (
            self.ruleset_version == DEFAULT_SEED_RULESET_VERSION
            and self.ruleset_fingerprint == DEFAULT_SEED_RULESET_FINGERPRINT
            and len(self.scheduler_sha256) == 64
            and len(self.shared_package_sha256) == 64
            and all(
                char in "0123456789abcdef"
                for char in self.scheduler_sha256 + self.shared_package_sha256
            )
            and bool(self.package_version)
            and bool(self.authorized_at)
        )


@dataclass(frozen=True, slots=True)
class HelperObservation:
    """Current helper value with ownership and current-session provenance."""

    value: Any
    yaml_owned: bool
    pristine: bool
    shape_valid: bool = True
    context_id: str | None = None
    last_updated: datetime | None = None


@dataclass(frozen=True, slots=True)
class DefaultSeedRecord:
    """Durable decision for one user setting."""

    key: str
    target_entity_id: str
    status: str = "not_run"
    expected_value: str | float | None = None
    baseline_value: str | float | None = None
    baseline_context_id: str | None = None
    baseline_last_updated: str | None = None
    attempts: int = 0
    updated_at: str | None = None
    error: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES


@dataclass(frozen=True, slots=True)
class DefaultSeedLedger:
    """Versioned, bounded first-install ledger."""

    version: int
    fields: tuple[DefaultSeedRecord, ...]
    authorization: DefaultSeedAuthorization | None = None
    pending_authorization: DefaultSeedAuthorization | None = None

    @classmethod
    def empty(cls) -> "DefaultSeedLedger":
        return cls(
            DEFAULT_SEED_VERSION,
            tuple(
                DefaultSeedRecord(spec.key, spec.target_entity_id)
                for spec in DEFAULT_SEED_SPECS
            ),
            None,
            None,
        )

    def field(self, target_entity_id: str) -> DefaultSeedRecord:
        return next(
            field for field in self.fields if field.target_entity_id == target_entity_id
        )

    def replace_field(self, record: DefaultSeedRecord) -> "DefaultSeedLedger":
        return replace(
            self,
            fields=tuple(
                record if field.target_entity_id == record.target_entity_id else field
                for field in self.fields
            ),
        )

    @property
    def result(self) -> str:
        if self.authorization is None or not self.authorization.valid:
            return "not_authorized"
        if any(field.status in _ERROR_STATUSES for field in self.fields):
            return "pending_with_errors"
        return "complete" if all(field.terminal for field in self.fields) else "pending"

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "result": self.result,
            "authorization": (
                {
                    "ruleset_version": self.authorization.ruleset_version,
                    "ruleset_fingerprint": self.authorization.ruleset_fingerprint,
                    "package_version": self.authorization.package_version,
                    "scheduler_sha256": self.authorization.scheduler_sha256,
                    "shared_package_sha256": self.authorization.shared_package_sha256,
                    "authorized_at": self.authorization.authorized_at,
                }
                if self.authorization is not None
                else None
            ),
            "pending_authorization": (
                {
                    "ruleset_version": self.pending_authorization.ruleset_version,
                    "ruleset_fingerprint": self.pending_authorization.ruleset_fingerprint,
                    "package_version": self.pending_authorization.package_version,
                    "scheduler_sha256": self.pending_authorization.scheduler_sha256,
                    "shared_package_sha256": self.pending_authorization.shared_package_sha256,
                    "authorized_at": self.pending_authorization.authorized_at,
                }
                if self.pending_authorization is not None
                else None
            ),
            "fields": [
                {
                    "key": field.key,
                    "target_entity_id": field.target_entity_id,
                    "status": field.status,
                    "expected_value": field.expected_value,
                    "baseline_value": field.baseline_value,
                    "baseline_context_id": field.baseline_context_id,
                    "baseline_last_updated": field.baseline_last_updated,
                    "attempts": field.attempts,
                    "updated_at": field.updated_at,
                    "error": field.error,
                }
                for field in self.fields
            ],
        }


@dataclass(frozen=True, slots=True)
class DefaultSeedWrite:
    """One authorized helper service call."""

    target_entity_id: str
    value: str | float
    baseline_value: str | float
    baseline_context_id: str | None
    baseline_last_updated: datetime | None


@dataclass(frozen=True, slots=True)
class DefaultSeedPlan:
    """Updated write-ahead ledger and its bounded writes."""

    ledger: DefaultSeedLedger
    writes: tuple[DefaultSeedWrite, ...]


class UnsupportedDefaultSeedVersion(ValueError):
    """A newer ledger must fail closed instead of replaying defaults."""


def _timestamp(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("default seed timestamp must be timezone-aware")
    return now.astimezone(timezone.utc).isoformat()


def _normalized_value(
    spec: DefaultSeedSpec,
    value: Any,
) -> str | float | None:
    if value is None:
        return None
    if spec.kind == "boolean":
        if isinstance(value, bool):
            return "on" if value else "off"
        normalized = str(value).strip().casefold()
        return normalized if normalized in {"on", "off"} else None
    if spec.kind == "time":
        normalized = str(value).strip()
        try:
            parsed = time.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return None
        return parsed.isoformat()
    if spec.kind == "number":
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if isfinite(parsed) else None
    return None


def default_seed_ledger_from_dict(raw: Any) -> DefaultSeedLedger:
    """Decode and upgrade a supported ledger without replaying decisions."""

    if raw is None:
        return DefaultSeedLedger.empty()
    if not isinstance(raw, Mapping):
        raise ValueError("invalid default seed ledger")
    raw_version = raw.get("version")
    if type(raw_version) is not int:
        raise ValueError("default seed ledger lacks a version")
    if raw_version > DEFAULT_SEED_VERSION:
        raise UnsupportedDefaultSeedVersion("default seed ledger is newer than code")
    if raw_version < _MIN_SUPPORTED_SEED_VERSION:
        raise ValueError("unsupported default seed ledger version")
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, list):
        raise ValueError("default seed ledger fields are invalid")
    def decode_authorization(value: Any) -> DefaultSeedAuthorization | None:
        if not isinstance(value, Mapping):
            return None
        candidate = DefaultSeedAuthorization(
            ruleset_version=(
                value.get("ruleset_version")
                if type(value.get("ruleset_version")) is int
                else -1
            ),
            ruleset_fingerprint=str(
                value.get("ruleset_fingerprint", "")
            )[:128],
            package_version=str(value.get("package_version", ""))[:64],
            scheduler_sha256=str(value.get("scheduler_sha256", ""))[:64],
            shared_package_sha256=str(
                value.get("shared_package_sha256", "")
            )[:64],
            authorized_at=str(value.get("authorized_at", ""))[:64],
        )
        return candidate if candidate.valid else None

    authorization = decode_authorization(raw.get("authorization"))
    pending_authorization = decode_authorization(raw.get("pending_authorization"))
    by_target = {
        str(item.get("target_entity_id")): item
        for item in raw_fields
        if isinstance(item, Mapping)
    }
    records: list[DefaultSeedRecord] = []
    for spec in DEFAULT_SEED_SPECS:
        item = by_target.get(spec.target_entity_id)
        if spec.introduced_version > raw_version:
            records.append(DefaultSeedRecord(spec.key, spec.target_entity_id))
            continue
        if item is None:
            records.append(
                DefaultSeedRecord(
                    spec.key,
                    spec.target_entity_id,
                    status="preserved_invalid",
                    error="ledger_field_missing",
                )
            )
            continue
        status = str(item.get("status", "not_run"))[:64]
        if status not in {
            "not_run",
            "target_missing",
            "target_unavailable",
            *_TERMINAL_STATUSES,
            *_RETRY_STATUSES,
        }:
            status = "preserved_invalid"
        attempts = item.get("attempts", 0)
        if type(attempts) is not int or not 0 <= attempts <= 1000:
            attempts = 0
        record = DefaultSeedRecord(
                key=spec.key,
                target_entity_id=spec.target_entity_id,
                status=status,
                expected_value=_normalized_value(spec, item.get("expected_value")),
                baseline_value=_normalized_value(spec, item.get("baseline_value")),
                baseline_context_id=(
                    str(item.get("baseline_context_id"))[:64]
                    if item.get("baseline_context_id") is not None
                    else None
                ),
                baseline_last_updated=(
                    str(item.get("baseline_last_updated"))[:64]
                    if item.get("baseline_last_updated") is not None
                    else None
                ),
                attempts=attempts,
                updated_at=(
                    str(item.get("updated_at"))[:64]
                    if item.get("updated_at") is not None
                    else None
                ),
                error=(
                    str(item.get("error"))[:128]
                    if item.get("error") is not None
                    else None
                ),
            )
        if status in _RETRY_STATUSES and (
            record.expected_value is None
            or record.baseline_value is None
            or record.baseline_context_id is None
            or record.baseline_last_updated is None
        ):
            record = replace(
                record,
                status="preserved_invalid",
                error="retry_evidence_incomplete",
            )
        records.append(record)
    return DefaultSeedLedger(
        DEFAULT_SEED_VERSION,
        tuple(records),
        authorization,
        pending_authorization,
    )


def _storage_unreadable_default_seed_ledger(now: datetime) -> DefaultSeedLedger:
    """Return a durable no-authorization tombstone for corrupt storage."""

    updated_at = _timestamp(now)
    return DefaultSeedLedger(
        DEFAULT_SEED_VERSION,
        tuple(
            DefaultSeedRecord(
                spec.key,
                spec.target_entity_id,
                status="preserved_invalid",
                updated_at=updated_at,
                error="storage_unreadable",
            )
            for spec in DEFAULT_SEED_SPECS
        ),
        None,
        None,
    )


async def _async_load_default_seed_ledger(
    store: Any,
    *,
    now: datetime,
) -> DefaultSeedLedger:
    """Load a seed ledger, persisting no-replay state after quarantine."""

    load_with_presence = getattr(store, "async_load_with_presence", None)
    if callable(load_with_presence):
        storage_present, raw = await load_with_presence()
    else:
        storage_present = False
        raw = await store.async_load()
    if storage_present and raw is None:
        ledger = _storage_unreadable_default_seed_ledger(now)
        await store.async_save(ledger.as_dict())
        return ledger
    return default_seed_ledger_from_dict(raw)


def plan_default_seeds(
    ledger: DefaultSeedLedger,
    observations: Mapping[str, HelperObservation],
    *,
    restored_entity_ids: Set[str],
    now: datetime,
) -> DefaultSeedPlan:
    """Authorize defaults only from explicit HA restore/current provenance."""

    if ledger.authorization is None or not ledger.authorization.valid:
        return DefaultSeedPlan(ledger, ())
    updated = ledger
    writes: list[DefaultSeedWrite] = []
    updated_at = _timestamp(now)
    for spec in DEFAULT_SEED_SPECS:
        record = updated.field(spec.target_entity_id)
        if record.terminal:
            continue
        observation = observations.get(spec.target_entity_id)
        if observation is None:
            updated = updated.replace_field(
                replace(record, status="target_missing", updated_at=updated_at)
            )
            continue
        raw = observation.value
        if str(raw).strip().casefold() in _UNAVAILABLE:
            updated = updated.replace_field(
                replace(record, status="target_unavailable", updated_at=updated_at)
            )
            continue
        current = _normalized_value(spec, raw)
        if current is None:
            updated = updated.replace_field(
                replace(record, status="preserved_invalid", updated_at=updated_at)
            )
            continue

        if record.status in _RETRY_STATUSES and record.expected_value is not None:
            if current == record.expected_value:
                updated = updated.replace_field(
                    replace(record, status="seeded", updated_at=updated_at, error=None)
                )
                continue
            if spec.target_entity_id in restored_entity_ids:
                updated = updated.replace_field(
                    replace(
                        record,
                        status="preserved_restored",
                        updated_at=updated_at,
                        error=None,
                    )
                )
                continue
            if (
                record.baseline_value is not None
                and current == record.baseline_value
                and observation.yaml_owned
                and observation.pristine
                and observation.shape_valid
                and observation.context_id == record.baseline_context_id
                and (
                    observation.last_updated.isoformat()
                    if observation.last_updated is not None
                    else None
                )
                == record.baseline_last_updated
            ):
                pending = replace(
                    record,
                    status="seed_pending",
                    attempts=min(record.attempts + 1, 1000),
                    updated_at=updated_at,
                    error=None,
                )
                updated = updated.replace_field(pending)
                writes.append(
                    DefaultSeedWrite(
                        spec.target_entity_id,
                        record.expected_value,
                        current,
                        observation.context_id,
                        observation.last_updated,
                    )
                )
                continue
            status = (
                "preserved_restored"
                if spec.target_entity_id in restored_entity_ids
                else "preserved_changed"
            )
            updated = updated.replace_field(
                replace(record, status=status, updated_at=updated_at, error=None)
            )
            continue

        if spec.target_entity_id in restored_entity_ids:
            updated = updated.replace_field(
                replace(record, status="preserved_restored", updated_at=updated_at)
            )
            continue
        if not observation.yaml_owned:
            updated = updated.replace_field(
                replace(record, status="preserved_non_yaml", updated_at=updated_at)
            )
            continue
        if not observation.pristine or not observation.shape_valid:
            updated = updated.replace_field(
                replace(record, status="preserved_changed", updated_at=updated_at)
            )
            continue
        fallback = _normalized_value(spec, spec.fallback_value)
        if fallback is None or current != fallback:
            updated = updated.replace_field(
                replace(record, status="preserved_changed", updated_at=updated_at)
            )
            continue

        expected = _normalized_value(spec, spec.default_value)
        assert expected is not None
        if current == expected:
            updated = updated.replace_field(
                replace(
                    record,
                    status="seeded",
                    expected_value=expected,
                    baseline_value=current,
                    updated_at=updated_at,
                )
            )
            continue
        pending = replace(
            record,
            status="seed_pending",
            expected_value=expected,
            baseline_value=current,
            baseline_context_id=observation.context_id,
            baseline_last_updated=(
                observation.last_updated.isoformat()
                if observation.last_updated is not None
                else None
            ),
            attempts=min(record.attempts + 1, 1000),
            updated_at=updated_at,
            error=None,
        )
        updated = updated.replace_field(pending)
        writes.append(
            DefaultSeedWrite(
                spec.target_entity_id,
                expected,
                current,
                observation.context_id,
                observation.last_updated,
            )
        )
    return DefaultSeedPlan(updated, tuple(writes))


def complete_default_seed_write(
    ledger: DefaultSeedLedger,
    *,
    target_entity_id: str,
    observed_value: Any,
    now: datetime,
) -> DefaultSeedLedger:
    """Commit a seed only after matching helper readback."""

    spec = _SPEC_BY_TARGET[target_entity_id]
    record = ledger.field(target_entity_id)
    status = (
        "seeded"
        if record.expected_value is not None
        and _normalized_value(spec, observed_value) == record.expected_value
        else "verification_failed"
    )
    return ledger.replace_field(
        replace(
            record,
            status=status,
            updated_at=_timestamp(now),
            error=None if status == "seeded" else "readback_mismatch",
        )
    )


def fail_default_seed_write(
    ledger: DefaultSeedLedger,
    *,
    target_entity_id: str,
    error: str,
    now: datetime,
) -> DefaultSeedLedger:
    """Keep an authorized seed retryable after a service failure."""

    record = ledger.field(target_entity_id)
    return ledger.replace_field(
        replace(
            record,
            status="write_failed",
            updated_at=_timestamp(now),
            error=str(error)[:128],
        )
    )


def blocked_shared_migration_targets(
    ledger: DefaultSeedLedger | None,
) -> frozenset[str]:
    """Hold only the fresh fallback copy while its source seed is pending."""

    shared_fallback = frozenset(
        {"input_number.hoymiles_ems_fallback_daily_home_load"}
    )
    if ledger is None:
        return shared_fallback
    fallback = ledger.field("input_number.hoymiles_rce_fallback_daily_load")
    if fallback.status == "preserved_invalid":
        return shared_fallback
    if ledger.authorization is None or not ledger.authorization.valid:
        return frozenset()
    if fallback.terminal:
        return frozenset()
    return shared_fallback


def trusted_shared_migration_source_values(
    ledger: DefaultSeedLedger | None,
) -> dict[str, str | float]:
    """Expose only exact same-lifecycle seeds consumed by shared migration."""

    if (
        ledger is None
        or ledger.authorization is None
        or not ledger.authorization.valid
    ):
        return {}
    trusted: dict[str, str | float] = {}
    for entity_id in (
        "input_number.hoymiles_rce_fallback_daily_load",
        "input_number.hoymiles_tariff_charge_efficiency",
        "input_number.hoymiles_tariff_discharge_efficiency",
    ):
        record = ledger.field(entity_id)
        if record.status == "seeded" and record.expected_value is not None:
            trusted[entity_id] = record.expected_value
    return trusted


def _regular_file_sha256(path: Path) -> str | None:
    """Return a hash only for a bounded, ordinary, single-link file."""

    try:
        identity = path.lstat()
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(identity.st_mode)
        or identity.st_nlink != 1
        or identity.st_size > _MAX_HELPER_STORE_BYTES
    ):
        return None
    digest = sha256()
    try:
        with path.open("rb") as source:
            while chunk := source.read(64 * 1024):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def _helper_storage_absence(config_path: Path) -> tuple[bool, str]:
    """Prove that no UI helper with a seeded identity exists on disk."""

    targets_by_domain: dict[str, set[str]] = {
        domain: set() for domain in _HELPER_STORE_DOMAINS
    }
    for spec in DEFAULT_SEED_SPECS:
        domain, object_id = spec.target_entity_id.split(".", 1)
        targets_by_domain[domain].add(object_id)
    for domain, object_ids in targets_by_domain.items():
        path = config_path / ".storage" / domain
        try:
            identity = path.lstat()
        except FileNotFoundError:
            continue
        if (
            not stat.S_ISREG(identity.st_mode)
            or identity.st_nlink != 1
            or identity.st_size > _MAX_HELPER_STORE_BYTES
        ):
            return False, f"{domain}_store_unverifiable"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False, f"{domain}_store_unverifiable"
        if (
            not isinstance(raw, Mapping)
            or raw.get("version") != 1
            or raw.get("key") != domain
            or type(raw.get("minor_version")) is not int
            or raw.get("minor_version") not in {1, 2}
        ):
            return False, f"{domain}_store_schema_unverifiable"
        data = raw.get("data")
        items = data.get("items") if isinstance(data, Mapping) else None
        if not isinstance(items, list):
            return False, f"{domain}_store_items_unverifiable"
        for item in items:
            if not isinstance(item, Mapping) or not isinstance(item.get("id"), str):
                return False, f"{domain}_store_item_unverifiable"
            if item["id"] in object_ids:
                return False, f"{domain}_store_collision"
    return True, "absent"


def _restore_storage_absence(config_path: Path) -> tuple[bool, str]:
    """Reject corrupt restore storage and any durable target membership."""

    path = config_path / ".storage" / "core.restore_state"
    try:
        identity = path.lstat()
    except FileNotFoundError:
        return True, "absent"
    if (
        not stat.S_ISREG(identity.st_mode)
        or identity.st_nlink != 1
        or identity.st_size > _MAX_RESTORE_STORE_BYTES
    ):
        return False, "restore_store_unverifiable"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False, "restore_store_unverifiable"
    if (
        not isinstance(raw, Mapping)
        or raw.get("version") != 1
        or raw.get("key") != "core.restore_state"
        or type(raw.get("minor_version")) is not int
        or raw.get("minor_version") != 1
        or not isinstance(raw.get("data"), list)
    ):
        return False, "restore_store_schema_unverifiable"
    target_ids = {spec.target_entity_id for spec in DEFAULT_SEED_SPECS}
    for item in raw["data"]:
        state = item.get("state") if isinstance(item, Mapping) else None
        entity_id = state.get("entity_id") if isinstance(state, Mapping) else None
        if not isinstance(entity_id, str):
            return False, "restore_store_item_unverifiable"
        if entity_id in target_ids:
            return False, "restore_store_state_exists"
    return True, "absent"


def _disk_persistence_absence(config_path: Path) -> tuple[bool, str]:
    helper_absent, reason = _helper_storage_absence(config_path)
    if not helper_absent:
        return False, reason
    return _restore_storage_absence(config_path)


def _package_paths_absent(scheduler_path: Path, shared_path: Path) -> bool:
    for path in (scheduler_path, shared_path):
        try:
            os.lstat(path)
        except FileNotFoundError:
            continue
        return False
    return True


async def _async_helper_absence(hass: Any, config_path: Path) -> tuple[bool, str]:
    """Combine live, registry, restore, and storage proof for every seed ID."""

    for spec in DEFAULT_SEED_SPECS:
        if hass.states.get(spec.target_entity_id) is not None:
            return False, "live_state_exists"
    try:
        from homeassistant.helpers import entity_registry as er  # noqa: PLC0415
        from homeassistant.helpers.restore_state import async_get  # noqa: PLC0415

        registry = er.async_get(hass)
        for spec in DEFAULT_SEED_SPECS:
            if registry.async_get(spec.target_entity_id) is not None:
                return False, "entity_registry_entry_exists"
        restore_states = async_get(hass).last_states
    except Exception:  # noqa: BLE001 - unavailable evidence must fail closed
        return False, "runtime_evidence_unverifiable"
    if not isinstance(restore_states, Mapping):
        return False, "restore_state_unverifiable"
    if any(spec.target_entity_id in restore_states for spec in DEFAULT_SEED_SPECS):
        return False, "restore_state_exists"
    try:
        return await hass.async_add_executor_job(_disk_persistence_absence, config_path)
    except Exception:  # noqa: BLE001 - unavailable evidence must fail closed
        return False, "helper_storage_unverifiable"


async def async_capture_fresh_install_evidence(hass: Any) -> FreshInstallEvidence:
    """Capture a pre-install proof; it never infers freshness from values."""

    config_path = Path(hass.config.config_dir)
    scheduler_path = config_path / "packages" / "hoymiles_ems_scheduler.yaml"
    shared_path = config_path / "packages" / "hoymiles_ems_shared_inputs.yaml"
    if bool(getattr(hass, "is_running", True)):
        return FreshInstallEvidence(
            False, "not_bootstrap", scheduler_path, shared_path
        )
    if bool(getattr(hass, "safe_mode", False)) or bool(
        getattr(hass.config, "safe_mode", False)
    ):
        return FreshInstallEvidence(
            False, "recovery_mode", scheduler_path, shared_path
        )
    try:
        packages_absent = await hass.async_add_executor_job(
            _package_paths_absent, scheduler_path, shared_path
        )
    except Exception:  # noqa: BLE001 - unavailable evidence must fail closed
        packages_absent = False
    if not packages_absent:
        return FreshInstallEvidence(
            False, "managed_package_exists", scheduler_path, shared_path
        )
    helpers_absent, reason = await _async_helper_absence(hass, config_path)
    return FreshInstallEvidence(
        helpers_absent,
        reason,
        scheduler_path,
        shared_path,
    )


async def async_stage_fresh_install_authorization(
    hass: Any,
    evidence: FreshInstallEvidence,
    *,
    scheduler_source: Path,
    shared_package_source: Path,
    package_version: str,
    now: datetime,
) -> bool:
    """Durably attest absence before package copy without granting writes."""

    if not evidence.eligible or bool(getattr(hass, "is_running", True)):
        return False
    scheduler_hash, shared_hash = await hass.async_add_executor_job(
        lambda: (
            _regular_file_sha256(scheduler_source),
            _regular_file_sha256(shared_package_source),
        )
    )
    if scheduler_hash is None or shared_hash is None:
        return False
    candidate = DefaultSeedAuthorization(
        DEFAULT_SEED_RULESET_VERSION,
        DEFAULT_SEED_RULESET_FINGERPRINT,
        str(package_version),
        scheduler_hash,
        shared_hash,
        _timestamp(now),
    )
    store = EMSInitialDefaultSeedStore(hass)
    ledger = await _async_load_default_seed_ledger(store, now=now)
    if ledger.authorization is not None:
        return ledger.authorization.valid
    if ledger.pending_authorization is not None:
        pending = ledger.pending_authorization
        matches_candidate = (
            pending.valid
            and pending.ruleset_version == candidate.ruleset_version
            and pending.ruleset_fingerprint == candidate.ruleset_fingerprint
            and pending.package_version == candidate.package_version
            and pending.scheduler_sha256 == candidate.scheduler_sha256
            and pending.shared_package_sha256 == candidate.shared_package_sha256
        )
        if matches_candidate:
            return True
        # A process may stop after staging but before copying either package.
        # Full fresh-install evidence on the next bootstrap is authority to
        # replace only that ungranted stale token with the current exact
        # sources. No field may already have entered the seed lifecycle.
        if any(field.status != "not_run" for field in ledger.fields):
            return False
        await store.async_save(
            replace(ledger, pending_authorization=candidate).as_dict()
        )
        return True
    if any(field.status != "not_run" for field in ledger.fields):
        return False
    await store.async_save(
        replace(ledger, pending_authorization=candidate).as_dict()
    )
    return True


async def async_authorize_fresh_install(
    hass: Any,
    evidence: FreshInstallEvidence,
    *,
    written_paths: Set[Path],
    scheduler_source: Path,
    shared_package_source: Path,
    package_version: str,
    now: datetime,
) -> bool:
    """Promote a pre-copy attestation only after exact package hashes exist."""

    if bool(getattr(hass, "is_running", True)):
        return False
    hashes = await hass.async_add_executor_job(
        lambda: (
            _regular_file_sha256(scheduler_source),
            _regular_file_sha256(evidence.scheduler_path),
            _regular_file_sha256(shared_package_source),
            _regular_file_sha256(evidence.shared_package_path),
        )
    )
    (
        scheduler_source_hash,
        scheduler_destination_hash,
        shared_source_hash,
        shared_destination_hash,
    ) = hashes
    if (
        scheduler_source_hash is None
        or shared_source_hash is None
        or scheduler_source_hash != scheduler_destination_hash
        or shared_source_hash != shared_destination_hash
    ):
        return False
    store = EMSInitialDefaultSeedStore(hass)
    ledger = await _async_load_default_seed_ledger(store, now=now)
    if ledger.authorization is not None:
        authorization = ledger.authorization
        return (
            authorization.valid
            and authorization.package_version == str(package_version)
            and authorization.scheduler_sha256 == scheduler_source_hash
            and authorization.shared_package_sha256 == shared_source_hash
        )
    if any(field.status != "not_run" for field in ledger.fields):
        return False
    authorization = ledger.pending_authorization
    if authorization is None:
        # Backward-compatible direct authorization is allowed only in the
        # original, fully evidenced same-boot path.
        if not evidence.eligible or not {
            evidence.scheduler_path,
            evidence.shared_package_path,
        }.issubset(set(written_paths)):
            return False
        config_path = Path(hass.config.config_dir)
        helpers_absent, _reason = await _async_helper_absence(hass, config_path)
        if not helpers_absent:
            return False
        authorization = DefaultSeedAuthorization(
            DEFAULT_SEED_RULESET_VERSION,
            DEFAULT_SEED_RULESET_FINGERPRINT,
            str(package_version),
            scheduler_source_hash,
            shared_source_hash,
            _timestamp(now),
        )
    if (
        not authorization.valid
        or authorization.package_version != str(package_version)
        or authorization.scheduler_sha256 != scheduler_source_hash
        or authorization.shared_package_sha256 != shared_source_hash
    ):
        return False
    await store.async_save(
        replace(
            ledger,
            authorization=authorization,
            pending_authorization=None,
        ).as_dict()
    )
    return True


class EMSInitialDefaultSeeder:
    """HA lifecycle adapter for the one-time defaults ledger."""

    def __init__(self, hass: Any, store: Any) -> None:
        self.hass = hass
        self._store = store

    @classmethod
    def for_home_assistant(cls, hass: Any) -> "EMSInitialDefaultSeeder":
        return cls(hass, EMSInitialDefaultSeedStore(hass))

    def _observation(self, spec: DefaultSeedSpec) -> HelperObservation | None:
        state = self.hass.states.get(spec.target_entity_id)
        if state is None:
            return None
        context = getattr(state, "context", None)
        context_raw_id = getattr(context, "id", None)
        context_id = str(context_raw_id) if context_raw_id is not None else None
        last_reported = getattr(state, "last_reported", None)
        last_updated = getattr(state, "last_updated", None)
        attributes = state.attributes
        shape_valid = True
        if spec.kind == "number":
            try:
                shape_valid = (
                    attributes.get("initial") is None
                    and float(attributes.get("min")) == spec.minimum
                    and float(attributes.get("max")) == spec.maximum
                    and float(attributes.get("step")) == spec.step
                )
            except (TypeError, ValueError):
                shape_valid = False
        elif spec.kind == "time":
            shape_valid = (
                attributes.get("has_date") is False
                and attributes.get("has_time") is True
            )
        pristine = (
            context_id is not None
            and getattr(context, "user_id", None) is None
            and getattr(context, "parent_id", None) is None
            and isinstance(last_reported, datetime)
            and isinstance(last_updated, datetime)
            and last_reported == last_updated
        )
        return HelperObservation(
            value=state.state,
            yaml_owned=attributes.get("editable") is False,
            pristine=pristine,
            shape_valid=shape_valid,
            context_id=context_id,
            last_updated=last_updated if isinstance(last_updated, datetime) else None,
        )

    def _observations(self) -> dict[str, HelperObservation]:
        result: dict[str, HelperObservation] = {}
        for spec in DEFAULT_SEED_SPECS:
            observation = self._observation(spec)
            if observation is not None:
                result[spec.target_entity_id] = observation
        return result

    def _restored_entity_ids(self) -> frozenset[str]:
        # HA 2026.8.2 keeps this cache after RestoreEntity registration and also
        # populates it when a package reload removes/re-adds a helper.
        from homeassistant.helpers.restore_state import async_get  # noqa: PLC0415

        return frozenset(async_get(self.hass).last_states)

    async def _async_authorization_current(
        self, authorization: DefaultSeedAuthorization
    ) -> bool:
        from .const import (  # noqa: PLC0415
            EMS_PACKAGE_VERSION,
            EMS_PACKAGE_VERSION_ENTITY,
        )

        if (
            bool(getattr(self.hass, "is_running", True))
            or authorization.package_version != EMS_PACKAGE_VERSION
        ):
            return False
        marker = self.hass.states.get(EMS_PACKAGE_VERSION_ENTITY)
        if marker is None or marker.state != EMS_PACKAGE_VERSION:
            return False
        config_path = Path(self.hass.config.config_dir)
        scheduler_path = config_path / "packages" / "hoymiles_ems_scheduler.yaml"
        shared_path = config_path / "packages" / "hoymiles_ems_shared_inputs.yaml"
        scheduler_hash, shared_hash = await self.hass.async_add_executor_job(
            lambda: (
                _regular_file_sha256(scheduler_path),
                _regular_file_sha256(shared_path),
            )
        )
        return (
            scheduler_hash == authorization.scheduler_sha256
            and shared_hash == authorization.shared_package_sha256
        )

    def _install_lock(self) -> asyncio.Lock:
        lock = self.hass.data.get(_SEED_LOCK_DATA_KEY)
        if lock is None:
            lock = asyncio.Lock()
            self.hass.data[_SEED_LOCK_DATA_KEY] = lock
        if not isinstance(lock, asyncio.Lock):
            raise RuntimeError("invalid EMS initial-default seed lock")
        return lock

    async def async_run_once(
        self,
        *,
        now: datetime,
        restored_entity_ids: Set[str] | None = None,
    ) -> DefaultSeedLedger:
        """Run one bounded seed pass, persisting authorization before writes."""

        async with self._install_lock():
            ledger = await _async_load_default_seed_ledger(
                self._store,
                now=now,
            )
            authorization = ledger.authorization
            if (
                authorization is None
                or not authorization.valid
                or not await self._async_authorization_current(authorization)
            ):
                return ledger
            restore_ids = (
                restored_entity_ids
                if restored_entity_ids is not None
                else self._restored_entity_ids()
            )
            plan = plan_default_seeds(
                ledger,
                self._observations(),
                restored_entity_ids=restore_ids,
                now=now,
            )
            loaded_ledger = ledger
            ledger = plan.ledger
            if ledger.as_dict() != loaded_ledger.as_dict():
                # Persist seed_pending before the first service call. A crash
                # can only verify/retry this exact authorized baseline.
                await self._store.async_save(ledger.as_dict())
            from homeassistant.core import Context  # noqa: PLC0415

            for write in plan.writes:
                spec = _SPEC_BY_TARGET[write.target_entity_id]
                current = self._observation(spec)
                if (
                    current is None
                    or _normalized_value(spec, current.value) != write.baseline_value
                    or current.context_id != write.baseline_context_id
                    or current.last_updated != write.baseline_last_updated
                    or not current.yaml_owned
                    or not current.pristine
                    or not current.shape_valid
                    or write.target_entity_id in self._restored_entity_ids()
                ):
                    record = ledger.field(write.target_entity_id)
                    ledger = ledger.replace_field(
                        replace(
                            record,
                            status="preserved_changed",
                            updated_at=_timestamp(now),
                            error="changed_before_write",
                        )
                    )
                    await self._store.async_save(ledger.as_dict())
                    continue
                domain = write.target_entity_id.split(".", 1)[0]
                if domain == "input_boolean":
                    service = "turn_on" if write.value == "on" else "turn_off"
                    data = {"entity_id": write.target_entity_id}
                elif domain == "input_datetime":
                    service = "set_datetime"
                    data = {"entity_id": write.target_entity_id, "time": write.value}
                elif domain == "input_number":
                    service = "set_value"
                    data = {"entity_id": write.target_entity_id, "value": write.value}
                else:
                    ledger = fail_default_seed_write(
                        ledger,
                        target_entity_id=write.target_entity_id,
                        error="unsupported_target_domain",
                        now=now,
                    )
                    await self._store.async_save(ledger.as_dict())
                    continue
                operation_context = Context()
                try:
                    await self.hass.services.async_call(
                        domain,
                        service,
                        data,
                        blocking=True,
                        context=operation_context,
                    )
                except Exception as err:  # noqa: BLE001 - retry remains bounded
                    ledger = fail_default_seed_write(
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
                        and getattr(target_context, "id", None) == operation_context.id
                    )
                    ledger = complete_default_seed_write(
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


class EMSInitialDefaultSeedStore:
    """Real HA Store with a no-replay v1 to v2 migrator."""

    def __new__(cls, hass: Any) -> Any:
        from homeassistant.helpers.storage import Store  # noqa: PLC0415

        class _Store(Store):
            async def async_load_with_presence(self) -> tuple[bool, Any]:
                """Distinguish proven absence from a quarantined corrupt key."""

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
                if old_major_version == DEFAULT_SEED_VERSION:
                    if not isinstance(old_data, Mapping):
                        raise ValueError("invalid default seed ledger")
                    return dict(old_data)
                if old_major_version != 1:
                    raise NotImplementedError
                if not isinstance(old_data, Mapping):
                    raise ValueError("invalid v1 default seed ledger")
                raw = dict(old_data)
                raw["version"] = 1
                return default_seed_ledger_from_dict(raw).as_dict()

        return _Store(
            hass,
            DEFAULT_SEED_VERSION,
            DEFAULT_SEED_STORAGE_KEY,
            atomic_writes=True,
        )


__all__ = (
    "DEFAULT_SEED_RULESET_FINGERPRINT",
    "DEFAULT_SEED_RULESET_VERSION",
    "DEFAULT_SEED_SPECS",
    "DEFAULT_SEED_STORAGE_KEY",
    "DEFAULT_SEED_VERSION",
    "DefaultSeedAuthorization",
    "DefaultSeedLedger",
    "DefaultSeedPlan",
    "DefaultSeedRecord",
    "DefaultSeedWrite",
    "EMSInitialDefaultSeedStore",
    "EMSInitialDefaultSeeder",
    "FreshInstallEvidence",
    "HelperObservation",
    "UnsupportedDefaultSeedVersion",
    "async_authorize_fresh_install",
    "async_capture_fresh_install_evidence",
    "async_stage_fresh_install_authorization",
    "blocked_shared_migration_targets",
    "complete_default_seed_write",
    "default_seed_ledger_from_dict",
    "fail_default_seed_write",
    "plan_default_seeds",
    "trusted_shared_migration_source_values",
)
