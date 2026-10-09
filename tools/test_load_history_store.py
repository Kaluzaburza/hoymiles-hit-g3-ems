"""Pure persistence/merge contract for qualified LOAD history."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "custom_components.hoymiles_hit_modbus"
for name, path in (
    ("custom_components", ROOT / "custom_components"),
    (PACKAGE, ROOT / "custom_components" / "hoymiles_hit_modbus"),
):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules.setdefault(name, module)


def load(name: str):
    path = ROOT / "custom_components" / "hoymiles_hit_modbus" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{PACKAGE}.{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


HISTORY = load("rce_history")
STORE = load("load_history_store")


def empty():
    return HISTORY.LoadHistorySummary(None, 0, {}, None, 0, {})


def main() -> None:
    generated = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
    profile = tuple([0.5] * 48)
    accepted = HISTORY.LoadHistorySummary(
        24.0, 1, {"2026-09-19": 24.0}, 8.0, 1, {"2026-09-19": 8.0},
        profile_kwh_by_date={"2026-09-19": profile},
        daily_quality_by_date={"2026-09-19": "complete"},
        profile_quality_by_date={"2026-09-19": "complete"},
        night_quality_by_date={"2026-09-19": "complete"},
        availability_diagnostics_by_date={
            "2026-09-19": {
                "phase-l1": {
                    "unknown_count": 1,
                    "unavailable_count": 0,
                    "event_count": 1,
                    "first_at": "2026-09-19T12:00:00+02:00",
                    "last_at": "2026-09-19T12:00:00+02:00",
                    "max_bracket_seconds": 600.0,
                    "max_numeric_bracket_seconds": 600.0,
                    "max_episode_seconds": 60.0,
                    "total_unavailable_seconds": 60.0,
                    "episode_count": 1,
                    "first_recovery_at": "2026-09-19T12:01:00+02:00",
                    "dense_quality_reason": "complete",
                    "dense_coverage_ratio": 0.998264,
                    "total_gap_seconds": 60.0,
                    "decision": "accepted_short_availability_gap",
                    "qualifier_version": STORE.QUALIFIER_VERSION,
                }
            }
        },
        daily_coverage_ratio=0.95,
        profile_coverage_ratio=0.9,
    )
    identity = STORE.source_identity(
        entry_id="entry-a",
        entry_unique_id="device-a",
        source_device_id="source-a",
        resolved_source_device_id="source-a-resolved",
        timezone="Europe/Warsaw",
    )
    payload = STORE.encode_cache(accepted, identity=identity, generated_at=generated)
    restored, restored_at = STORE.decode_cache(
        payload, expected_identity=identity, empty=empty()
    )
    assert restored_at == generated
    assert restored.daily_energy_kwh == accepted.daily_energy_kwh
    assert restored.profile_kwh_by_date == accepted.profile_kwh_by_date
    assert restored.daily_coverage_ratio == 0.95
    assert restored.profile_coverage_ratio == 0.9
    assert restored.availability_diagnostics_by_date == (
        accepted.availability_diagnostics_by_date
    )

    retained = STORE.merge_history(restored, empty())
    assert retained.daily_energy_kwh == restored.daily_energy_kwh
    assert retained.night_energy_kwh == restored.night_energy_kwh
    assert retained.profile_kwh_by_date == restored.profile_kwh_by_date

    rejected_night = HISTORY.LoadHistorySummary(
        None,
        0,
        {},
        None,
        0,
        {},
        night_quality_by_date={
            "2026-09-20": (
                "missing_or_reset:"
                "sensor.hoymiles_hit_load_energy_use_l1n_today"
            )
        },
    )
    rejected_merged = STORE.merge_history(retained, rejected_night)
    assert rejected_merged.night_history_days == retained.night_history_days
    assert rejected_merged.night_energy_kwh == retained.night_energy_kwh
    assert rejected_merged.night_quality_by_date == {
        "2026-09-19": "complete",
        "2026-09-20": (
            "missing_or_reset:"
            "sensor.hoymiles_hit_load_energy_use_l1n_today"
        ),
    }
    rejected_payload = STORE.encode_cache(
        rejected_merged,
        identity=identity,
        generated_at=generated,
    )
    rejected_restored, rejected_restored_at = STORE.decode_cache(
        rejected_payload,
        expected_identity=identity,
        empty=empty(),
    )
    assert rejected_restored_at == generated
    assert rejected_restored.night_history_days == retained.night_history_days
    assert rejected_restored.night_quality_by_date == (
        rejected_merged.night_quality_by_date
    )
    assert STORE.merge_history(
        rejected_restored, empty()
    ).night_quality_by_date == rejected_merged.night_quality_by_date

    diagnostics = STORE.qualification_diagnostics(rejected_restored)
    assert diagnostics == {
        "recorder_load_qualification_date": "2026-09-19",
        "recorder_load_qualification_reason": "complete",
        "recorder_night_qualification_date": "2026-09-20",
        "recorder_night_qualification_reason": (
            "missing_or_reset:"
            "sensor.hoymiles_hit_load_energy_use_l1n_today"
        ),
        "recorder_load_qualifier_version": STORE.QUALIFIER_VERSION,
        "recorder_load_cache_migrated_from": None,
        "recorder_load_availability_date": "2026-09-19",
        "recorder_load_availability_decision": (
            "accepted_short_availability_gap"
        ),
    }
    assert STORE.qualification_diagnostics(empty()) == {
        "recorder_load_qualification_date": None,
        "recorder_load_qualification_reason": "unknown",
        "recorder_night_qualification_date": None,
        "recorder_night_qualification_reason": "unknown",
        "recorder_load_qualifier_version": STORE.QUALIFIER_VERSION,
        "recorder_load_cache_migrated_from": None,
        "recorder_load_availability_date": None,
        "recorder_load_availability_decision": None,
    }

    # v3 accepted records are a strict subset of v4: v3 rejected every
    # nonnumeric row. Migrate only that direction, preserving freshness and
    # accepted timestamps. Rejected dates remain diagnostics, never energy.
    legacy_identity = dict(identity)
    legacy_identity["qualifier_version"] = "load_history_v3_delayed_midnight"
    legacy_payload = STORE.encode_cache(
        accepted, identity=legacy_identity, generated_at=generated
    )
    legacy_payload.pop("availability_diagnostics_by_date", None)
    migrated, migrated_at = STORE.decode_cache(
        legacy_payload, expected_identity=identity, empty=empty()
    )
    assert migrated.daily_energy_kwh == accepted.daily_energy_kwh
    assert migrated.profile_kwh_by_date == accepted.profile_kwh_by_date
    assert migrated_at == generated
    assert migrated.cache_migrated_from_qualifier_version == "load_history_v3_delayed_midnight"
    assert STORE.encode_cache(migrated, identity=identity, generated_at=migrated_at)[
        "cache_migrated_from_qualifier_version"
    ] == "load_history_v3_delayed_midnight"

    v4_identity = dict(identity)
    v4_identity["qualifier_version"] = "load_history_v4_bounded_availability"
    v4_payload = STORE.encode_cache(
        accepted, identity=v4_identity, generated_at=generated
    )
    v4_migrated, v4_at = STORE.decode_cache(
        v4_payload, expected_identity=identity, empty=empty()
    )
    assert v4_migrated.daily_energy_kwh == accepted.daily_energy_kwh
    assert v4_migrated.profile_kwh_by_date == accepted.profile_kwh_by_date
    assert v4_at == generated
    assert v4_migrated.cache_migrated_from_qualifier_version == "load_history_v4_bounded_availability"

    legacy_rejected = dict(
        legacy_payload,
        daily_energy_kwh={},
        profile_kwh_by_date={},
        daily_quality_by_date={"2026-09-18": "partial_invalid_value"},
    )
    rejected_migration, rejected_migration_at = STORE.decode_cache(
        legacy_rejected, expected_identity=identity, empty=empty()
    )
    assert rejected_migration.daily_history_days == 0
    assert rejected_migration.daily_energy_kwh == {}
    assert rejected_migration_at == generated

    try:
        STORE.decode_cache(payload, expected_identity=legacy_identity, empty=empty())
    except ValueError:
        pass
    else:
        raise AssertionError("new cache was accepted by the rollback identity")

    newer = HISTORY.LoadHistorySummary(
        30.0, 1, {"2026-09-20": 30.0}, None, 0, {},
        daily_quality_by_date={"2026-09-20": "complete"},
    )
    merged = STORE.merge_history(retained, newer)
    assert merged.daily_energy_kwh == {
        "2026-09-19": 24.0,
        "2026-09-20": 30.0,
    }
    assert merged.profile_kwh_by_date == {"2026-09-19": profile}

    bounded = STORE.merge_history(
        empty(),
        HISTORY.LoadHistorySummary(
            1.0,
            3,
            {"2026-09-18": 1.0, "2026-09-19": 1.0, "2026-09-20": 1.0},
            None,
            0,
            {},
            daily_quality_by_date={
                "2026-09-18": "complete",
                "2026-09-19": "complete",
                "2026-09-20": "complete",
            },
            phase_quality_by_date={
                "2026-09-18": {"l1": "complete"},
                "2026-09-19": {"l1": "complete"},
                "2026-09-20": {"l1": "complete"},
            },
        ),
        limit=2,
    )
    assert tuple(bounded.daily_energy_kwh) == ("2026-09-19", "2026-09-20")
    assert tuple(bounded.daily_quality_by_date or {}) == ("2026-09-19", "2026-09-20")
    assert tuple(bounded.phase_quality_by_date or {}) == ("2026-09-19", "2026-09-20")

    foreign = STORE.source_identity(
        entry_id="entry-b",
        entry_unique_id="device-b",
        source_device_id="source-b",
        resolved_source_device_id="source-b-resolved",
        timezone="Europe/Warsaw",
    )
    try:
        STORE.decode_cache(payload, expected_identity=foreign, empty=empty())
    except ValueError:
        pass
    else:
        raise AssertionError("foreign cache identity was accepted")

    corrupt = dict(payload, daily_energy_kwh={"2026-09-19": -1})
    try:
        STORE.decode_cache(corrupt, expected_identity=identity, empty=empty())
    except ValueError:
        pass
    else:
        raise AssertionError("corrupt cache was accepted")

    corrupt_quality = dict(payload, daily_quality_by_date={"2026-09-19": 7})
    try:
        STORE.decode_cache(corrupt_quality, expected_identity=identity, empty=empty())
    except ValueError:
        pass
    else:
        raise AssertionError("corrupt cache quality was accepted")

    corrupt_profile = dict(
        payload, profile_kwh_by_date={"2026-09-19": [1.0] * 48}
    )
    try:
        STORE.decode_cache(corrupt_profile, expected_identity=identity, empty=empty())
    except ValueError:
        pass
    else:
        raise AssertionError("profile inconsistent with daily energy was accepted")

    print("LOAD history Store: PASS (restore/merge/bounds/identity/corruption/freshness)")


if __name__ == "__main__":
    main()
