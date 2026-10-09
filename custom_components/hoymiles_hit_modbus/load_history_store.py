"""Versioned, identity-bound persistence for qualified LOAD history."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from math import isfinite
from typing import Any, Mapping

try:
    from .rce_history import LOAD_HISTORY_QUALIFIER_VERSION, LoadHistorySummary
except ImportError:  # pragma: no cover - standalone contract harness
    from rce_history import (  # type: ignore[no-redef]
        LOAD_HISTORY_QUALIFIER_VERSION,
        LoadHistorySummary,
    )

STORE_SCHEMA_VERSION = 1
QUALIFIER_VERSION = LOAD_HISTORY_QUALIFIER_VERSION
LEGACY_MIGRATABLE_QUALIFIER_VERSIONS = frozenset(
    {"load_history_v3_delayed_midnight", "load_history_v4_bounded_availability",
     "load_history_v5_sparse_phase_episodes"}
)


def source_identity(
    *,
    entry_id: str,
    entry_unique_id: str | None,
    source_device_id: str | None,
    resolved_source_device_id: str | None,
    timezone: str,
) -> dict[str, Any]:
    return {
        "entry_id": entry_id,
        "entry_unique_id": entry_unique_id,
        "source_device_id": source_device_id,
        "resolved_source_device_id": resolved_source_device_id,
        "timezone": timezone,
        "qualifier_version": QUALIFIER_VERSION,
        "measurement_contract": "three_phase_daily_kwh_plus_dense_aggregate_v1",
        "phase_entities": [
            "sensor.hoymiles_hit_load_energy_use_l1n_today",
            "sensor.hoymiles_hit_load_energy_use_l2n_today",
            "sensor.hoymiles_hit_load_energy_use_l3n_today",
        ],
        "profile_entity": "sensor.hoymiles_actual_load_energy_today",
    }


def _finite_map(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError("LOAD cache map is malformed")
    result: dict[str, float] = {}
    for key, raw in value.items():
        if not isinstance(key, str):
            raise ValueError("LOAD cache date is malformed")
        date.fromisoformat(key)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("LOAD cache energy is malformed")
        numeric = float(raw)
        if not isfinite(numeric) or numeric < 0.0:
            raise ValueError("LOAD cache energy is invalid")
        result[key] = numeric
    return result


def _profiles(value: Any) -> dict[str, tuple[float, ...]]:
    if not isinstance(value, Mapping):
        raise ValueError("LOAD cache profiles are malformed")
    result: dict[str, tuple[float, ...]] = {}
    for key, raw in value.items():
        date.fromisoformat(key)
        if not isinstance(raw, list) or len(raw) != 48:
            raise ValueError("LOAD cache profile shape is invalid")
        row = tuple(float(item) for item in raw)
        if any(not isfinite(item) or item < 0.0 for item in row):
            raise ValueError("LOAD cache profile value is invalid")
        result[key] = row
    return result


def _ratio(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("LOAD cache coverage ratio is malformed")
    numeric = float(value)
    if not isfinite(numeric) or not 0.0 <= numeric <= 1.0:
        raise ValueError("LOAD cache coverage ratio is invalid")
    return numeric


def _quality_map(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("LOAD cache quality map is malformed")
    result: dict[str, str] = {}
    for key, reason in value.items():
        if not isinstance(key, str) or not isinstance(reason, str):
            raise ValueError("LOAD cache quality entry is malformed")
        date.fromisoformat(key)
        result[key] = reason
    return result


def _phase_quality_map(value: Any) -> dict[str, dict[str, str]]:
    if not isinstance(value, Mapping):
        raise ValueError("LOAD cache phase quality map is malformed")
    result: dict[str, dict[str, str]] = {}
    for key, phase_reasons in value.items():
        if not isinstance(key, str) or not isinstance(phase_reasons, Mapping):
            raise ValueError("LOAD cache phase quality entry is malformed")
        date.fromisoformat(key)
        row: dict[str, str] = {}
        for phase, reason in phase_reasons.items():
            if not isinstance(phase, str) or not isinstance(reason, str):
                raise ValueError("LOAD cache phase quality reason is malformed")
            row[phase] = reason
        result[key] = row
    return result


def _phase_diagnostics_map(
    value: Any,
) -> dict[str, dict[str, dict[str, Any]]]:
    if not isinstance(value, Mapping):
        raise ValueError("LOAD cache phase diagnostics are malformed")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for key, entities in value.items():
        if not isinstance(key, str) or not isinstance(entities, Mapping):
            raise ValueError("LOAD cache phase diagnostics entry is malformed")
        date.fromisoformat(key)
        entity_rows: dict[str, dict[str, Any]] = {}
        for entity_id, details in entities.items():
            if not isinstance(entity_id, str) or not isinstance(details, Mapping):
                raise ValueError("LOAD cache phase diagnostic row is malformed")
            row = dict(details)
            if not isinstance(row.get("reason"), str):
                raise ValueError("LOAD cache phase diagnostic reason is malformed")
            if any(
                item is not None and not isinstance(item, (str, int, float, bool))
                for item in row.values()
            ):
                raise ValueError("LOAD cache phase diagnostic value is malformed")
            entity_rows[entity_id] = row
        result[key] = entity_rows
    return result


def _availability_diagnostics_map(
    value: Any,
) -> dict[str, dict[str, dict[str, Any]]]:
    if not isinstance(value, Mapping):
        raise ValueError("LOAD cache availability diagnostics are malformed")
    result: dict[str, dict[str, dict[str, Any]]] = {}
    for key, entities in value.items():
        if not isinstance(key, str) or not isinstance(entities, Mapping):
            raise ValueError("LOAD cache availability entry is malformed")
        date.fromisoformat(key)
        entity_rows: dict[str, dict[str, Any]] = {}
        for entity_id, details in entities.items():
            if not isinstance(entity_id, str) or not isinstance(details, Mapping):
                raise ValueError("LOAD cache availability row is malformed")
            row = dict(details)
            if not isinstance(row.get("decision"), str):
                raise ValueError("LOAD cache availability decision is malformed")
            if any(
                item is not None and not isinstance(item, (str, int, float, bool))
                for item in row.values()
            ):
                raise ValueError("LOAD cache availability value is malformed")
            entity_rows[entity_id] = row
        result[key] = entity_rows
    return result


def _bounded_map(value: Mapping[str, Any], limit: int) -> dict[str, Any]:
    return dict(sorted(value.items())[-limit:])


def _latest_quality(value: Mapping[str, str] | None) -> tuple[str | None, str]:
    if not value:
        return None, "unknown"
    key = sorted(value)[-1]
    return key, value[key]


def qualification_diagnostics(summary: LoadHistorySummary) -> dict[str, str | None]:
    """Return bounded qualification facts independently from model freshness."""

    daily_date, daily_reason = _latest_quality(summary.daily_quality_by_date)
    night_date, night_reason = _latest_quality(summary.night_quality_by_date)
    latest_availability_date: str | None = None
    latest_availability_decision: str | None = None
    if summary.availability_diagnostics_by_date:
        latest_availability_date = sorted(
            summary.availability_diagnostics_by_date
        )[-1]
        rows = summary.availability_diagnostics_by_date[
            latest_availability_date
        ]
        decisions = sorted(
            {
                str(details.get("decision"))
                for details in rows.values()
                if details.get("decision") is not None
            }
        )
        latest_availability_decision = ",".join(decisions) or None
    return {
        "recorder_load_qualification_date": daily_date,
        "recorder_load_qualification_reason": daily_reason,
        "recorder_night_qualification_date": night_date,
        "recorder_night_qualification_reason": night_reason,
        "recorder_load_qualifier_version": QUALIFIER_VERSION,
        "recorder_load_cache_migrated_from": summary.cache_migrated_from_qualifier_version,
        "recorder_load_availability_date": latest_availability_date,
        "recorder_load_availability_decision": latest_availability_decision,
    }


def _average_profiles(
    profiles: Mapping[str, tuple[float, ...]], *, weekend: bool | None = None
) -> tuple[float, ...]:
    selected = [
        row for key, row in sorted(profiles.items())
        if weekend is None or (date.fromisoformat(key).weekday() >= 5) is weekend
    ]
    if not selected:
        return ()
    return tuple(round(sum(row[index] for row in selected) / len(selected), 4)
                 for index in range(48))


def merge_history(
    previous: LoadHistorySummary,
    incoming: LoadHistorySummary,
    *,
    limit: int = 31,
) -> LoadHistorySummary:
    """Idempotently add accepted dates; partial/empty input never erases."""

    if limit < 1:
        raise ValueError("LOAD history limit must be positive")

    daily = {**previous.daily_energy_kwh, **incoming.daily_energy_kwh}
    nights = {**previous.night_energy_kwh, **incoming.night_energy_kwh}
    profiles = {
        **(previous.profile_kwh_by_date or {}),
        **(incoming.profile_kwh_by_date or {}),
    }
    daily = _bounded_map(daily, limit)
    nights = _bounded_map(nights, limit)
    profiles = _bounded_map(profiles, limit)
    daily_quality = dict(previous.daily_quality_by_date or {})
    for key, reason in (incoming.daily_quality_by_date or {}).items():
        if key not in previous.daily_energy_kwh:
            daily_quality[key] = reason
    for key in incoming.daily_energy_kwh:
        if key in (incoming.daily_quality_by_date or {}):
            daily_quality[key] = incoming.daily_quality_by_date[key]  # type: ignore[index]
    profile_quality = dict(previous.profile_quality_by_date or {})
    for key, reason in (incoming.profile_quality_by_date or {}).items():
        if key not in (previous.profile_kwh_by_date or {}):
            profile_quality[key] = reason
    for key in (incoming.profile_kwh_by_date or {}):
        if key in (incoming.profile_quality_by_date or {}):
            profile_quality[key] = incoming.profile_quality_by_date[key]  # type: ignore[index]
    phase_quality = {
        **(previous.phase_quality_by_date or {}),
        **(incoming.phase_quality_by_date or {}),
    }
    phase_diagnostics = {
        **(previous.phase_diagnostics_by_date or {}),
        **(incoming.phase_diagnostics_by_date or {}),
    }
    availability_diagnostics = {
        **(previous.availability_diagnostics_by_date or {}),
        **(incoming.availability_diagnostics_by_date or {}),
    }
    night_quality = dict(previous.night_quality_by_date or {})
    for key, reason in (incoming.night_quality_by_date or {}).items():
        if key not in previous.night_energy_kwh:
            night_quality[key] = reason
    for key in incoming.night_energy_kwh:
        if key in (incoming.night_quality_by_date or {}):
            night_quality[key] = incoming.night_quality_by_date[key]  # type: ignore[index]
    daily_quality = _bounded_map(daily_quality, limit)
    profile_quality = _bounded_map(profile_quality, limit)
    phase_quality = _bounded_map(phase_quality, limit)
    phase_diagnostics = _bounded_map(phase_diagnostics, limit)
    availability_diagnostics = _bounded_map(
        availability_diagnostics, limit
    )
    night_quality = _bounded_map(night_quality, limit)
    weekdays = [key for key in profiles if date.fromisoformat(key).weekday() < 5]
    weekends = [key for key in profiles if date.fromisoformat(key).weekday() >= 5]
    return replace(
        incoming,
        average_daily_kwh=(round(sum(daily.values()) / len(daily), 3) if daily else None),
        daily_history_days=len(daily),
        daily_energy_kwh=daily,
        average_night_kwh=(round(sum(nights.values()) / len(nights), 3) if nights else None),
        night_history_days=len(nights),
        night_energy_kwh=nights,
        average_profile_kwh=_average_profiles(profiles),
        weekday_profile_kwh=_average_profiles(profiles, weekend=False),
        weekend_profile_kwh=_average_profiles(profiles, weekend=True),
        weekday_profile_days=len(weekdays),
        weekend_profile_days=len(weekends),
        profile_history_days=len(profiles),
        daily_quality_by_date=daily_quality,
        profile_quality_by_date=profile_quality,
        phase_quality_by_date=phase_quality,
        phase_diagnostics_by_date=phase_diagnostics,
        availability_diagnostics_by_date=availability_diagnostics,
        night_quality_by_date=night_quality,
        profile_kwh_by_date=profiles,
        daily_coverage_ratio=(
            incoming.daily_coverage_ratio
            if incoming.daily_energy_kwh
            else previous.daily_coverage_ratio
        ),
        profile_coverage_ratio=(
            incoming.profile_coverage_ratio
            if incoming.profile_kwh_by_date
            else previous.profile_coverage_ratio
        ),
        cache_migrated_from_qualifier_version=(
            incoming.cache_migrated_from_qualifier_version
            or previous.cache_migrated_from_qualifier_version
        ),
    )


def history_in_window(
    summary: LoadHistorySummary, *, as_of: date, days: int = 28
) -> LoadHistorySummary:
    """Project accepted history onto a calendar window without changing cache.

    Rebuild aggregates from the retained dates. Invalid/missing days remain in
    diagnostic maps and are never invented to bridge an availability gap.
    """
    def retained(values: Mapping[str, Any] | None) -> dict[str, Any]:
        return {
            key: value for key, value in (values or {}).items()
            if 1 <= (as_of - date.fromisoformat(key)).days <= days
        }

    daily = retained(summary.daily_energy_kwh)
    profiles = retained(summary.profile_kwh_by_date)
    projected = replace(
        summary,
        daily_energy_kwh=daily,
        night_energy_kwh=retained(summary.night_energy_kwh),
        profile_kwh_by_date=profiles,
        daily_coverage_ratio=summary.daily_coverage_ratio if daily else 0.0,
        profile_coverage_ratio=summary.profile_coverage_ratio if profiles else 0.0,
    )
    empty = LoadHistorySummary(None, 0, {}, None, 0, {})
    return merge_history(empty, projected, limit=days)


def encode_cache(
    summary: LoadHistorySummary,
    *,
    identity: Mapping[str, Any],
    generated_at: datetime | None,
) -> dict[str, Any]:
    return {
        "schema_version": STORE_SCHEMA_VERSION,
        "identity": dict(identity),
        "generated_at": generated_at.isoformat() if generated_at else None,
        "daily_energy_kwh": summary.daily_energy_kwh,
        "night_energy_kwh": summary.night_energy_kwh,
        "profile_kwh_by_date": {
            key: list(value) for key, value in (summary.profile_kwh_by_date or {}).items()
        },
        "daily_quality_by_date": summary.daily_quality_by_date or {},
        "profile_quality_by_date": summary.profile_quality_by_date or {},
        "phase_quality_by_date": summary.phase_quality_by_date or {},
        "phase_diagnostics_by_date": summary.phase_diagnostics_by_date or {},
        "availability_diagnostics_by_date": (
            summary.availability_diagnostics_by_date or {}
        ),
        "night_quality_by_date": summary.night_quality_by_date or {},
        "daily_coverage_ratio": summary.daily_coverage_ratio,
        "profile_coverage_ratio": summary.profile_coverage_ratio,
        "cache_migrated_from_qualifier_version": (
            summary.cache_migrated_from_qualifier_version
        ),
    }


def decode_cache(
    raw: Any,
    *,
    expected_identity: Mapping[str, Any],
    empty: LoadHistorySummary,
) -> tuple[LoadHistorySummary, datetime | None]:
    if not isinstance(raw, Mapping) or raw.get("schema_version") != STORE_SCHEMA_VERSION:
        raise ValueError("LOAD cache schema is unsupported")
    raw_identity = raw.get("identity")
    expected = dict(expected_identity)
    identity_matches = raw_identity == expected
    migrated_from: str | None = None
    if isinstance(raw_identity, Mapping) and not identity_matches:
        legacy = dict(raw_identity)
        legacy_version = legacy.pop("qualifier_version", None)
        current = dict(expected)
        current_version = current.pop("qualifier_version", None)
        identity_matches = (
            legacy_version in LEGACY_MIGRATABLE_QUALIFIER_VERSIONS
            and current_version == QUALIFIER_VERSION
            and legacy == current
        )
        if identity_matches:
            migrated_from = legacy_version
    if not identity_matches:
        raise ValueError("LOAD cache identity does not match this installation")
    generated_raw = raw.get("generated_at")
    generated = datetime.fromisoformat(generated_raw) if isinstance(generated_raw, str) else None
    if generated is not None and (generated.tzinfo is None or generated.utcoffset() is None):
        raise ValueError("LOAD cache freshness timestamp is invalid")
    prior_migration = raw.get("cache_migrated_from_qualifier_version")
    if prior_migration is not None and (
        not isinstance(prior_migration, str)
        or prior_migration not in LEGACY_MIGRATABLE_QUALIFIER_VERSIONS
    ):
        raise ValueError("LOAD cache migration provenance is invalid")
    daily = _finite_map(raw.get("daily_energy_kwh", {}))
    nights = _finite_map(raw.get("night_energy_kwh", {}))
    profiles = _profiles(raw.get("profile_kwh_by_date", {}))
    for key, profile in profiles.items():
        daily_total = daily.get(key)
        if daily_total is None or abs(sum(profile) - daily_total) > max(
            0.05, daily_total * 0.01
        ):
            raise ValueError("LOAD cache profile does not match its daily energy")
    restored = replace(
        empty,
        daily_energy_kwh=daily,
        night_energy_kwh=nights,
        profile_kwh_by_date=profiles,
        daily_quality_by_date=_quality_map(raw.get("daily_quality_by_date", {})),
        profile_quality_by_date=_quality_map(raw.get("profile_quality_by_date", {})),
        phase_quality_by_date=_phase_quality_map(raw.get("phase_quality_by_date", {})),
        phase_diagnostics_by_date=_phase_diagnostics_map(
            raw.get("phase_diagnostics_by_date", {})
        ),
        availability_diagnostics_by_date=_availability_diagnostics_map(
            raw.get("availability_diagnostics_by_date", {})
        ),
        night_quality_by_date=_quality_map(raw.get("night_quality_by_date", {})),
        daily_coverage_ratio=_ratio(raw.get("daily_coverage_ratio", 0.0)),
        profile_coverage_ratio=_ratio(raw.get("profile_coverage_ratio", 0.0)),
        cache_migrated_from_qualifier_version=(
            migrated_from or prior_migration
        ),
    )
    return merge_history(empty, restored), generated
