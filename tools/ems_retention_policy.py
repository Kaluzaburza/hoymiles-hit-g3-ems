"""Validate a disabled, explicit EMS Recorder retention proposal.

This module has no Home Assistant service client and cannot purge data. Its
output is a reviewable service-call proposal, never an execution instruction.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
import re
from typing import Any


_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_ALLOWED_DAYS = frozenset({2, 7, 35})
_MINIMUM_DAYS = {
    "sensor.hoymiles_actual_load_energy_today": 35,
    "sensor.hoymiles_hit_load_energy_use_l1n_today": 35,
    "sensor.hoymiles_hit_load_energy_use_l2n_today": 35,
    "sensor.hoymiles_hit_load_energy_use_l3n_today": 35,
    "sensor.hoymiles_hit_pv_total_energy_today": 35,
    "binary_sensor.hoymiles_ems_export_allowed": 35,
    "sensor.hoymiles_ems_package_version": 35,
    "sensor.hoymiles_night_protected_load_energy_total": 7,
    "sensor.hoymiles_hit_grid_voltage_l1": 7,
    "sensor.hoymiles_hit_grid_voltage_l2": 7,
    "sensor.hoymiles_hit_grid_voltage_l3": 7,
    "sensor.hoymiles_hit_ems_supervisor": 7,
    "sensor.hoymiles_hit_ems_supervisor_canonical_plan": 7,
    "sensor.hoymiles_hit_rce_optimized_plan": 7,
    "sensor.hoymiles_hit_rcm_voltage_plan": 7,
    "sensor.hoymiles_battery_balancing_transaction": 7,
    "input_boolean.hoymiles_battery_balancing_active": 7,
}


def validate_disabled_policy(policy: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Fail closed on implicit scope, missing consent, or shortened dependencies."""
    if policy.get("enabled") is not False:
        raise ValueError("retention policy must remain disabled before approval")
    if policy.get("schema_version") != 1:
        raise ValueError("unknown retention policy schema")
    if set(policy) != {"schema_version", "enabled", "entities"}:
        raise ValueError("unexpected retention policy keys")
    rows = policy.get("entities")
    if not isinstance(rows, list) or not rows or len(rows) > 128:
        raise ValueError("retention policy requires a bounded entity list")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for raw in rows:
        if not isinstance(raw, dict) or set(raw) != {
            "entity_id", "keep_days", "role", "consumer", "external_use_reviewed"
        }:
            raise ValueError("retention row must have exact fields")
        entity_id = raw["entity_id"]
        days = raw["keep_days"]
        if type(entity_id) is not str or not _ENTITY_ID.fullmatch(entity_id):
            raise ValueError("retention needs one exact entity_id, never a glob/domain")
        if entity_id in seen:
            raise ValueError("duplicate retention entity")
        seen.add(entity_id)
        if type(days) is not int or days not in _ALLOWED_DAYS:
            raise ValueError("keep_days must be explicit and supported")
        minimum = _MINIMUM_DAYS.get(entity_id)
        if minimum is None:
            raise ValueError("unclassified entity is retained, not purged")
        if days < minimum:
            raise ValueError("retention would cut a known reader window")
        if not all(type(raw[key]) is str and raw[key].strip()
                   for key in ("role", "consumer")):
            raise ValueError("retention role and consumer are required")
        if raw["external_use_reviewed"] is not True:
            raise ValueError("external/raw-history uses need host review")
        result.append(dict(raw))
    return tuple(sorted(result, key=lambda row: row["entity_id"]))


def proposed_actions(
    policy: Mapping[str, Any], *, at_utc: datetime
) -> tuple[dict[str, Any], ...]:
    """Return exact, non-executing action data with explicit rolling cutoffs."""
    if at_utc.tzinfo is None or at_utc.utcoffset() is None:
        raise ValueError("plan time must be timezone-aware")
    at_utc = at_utc.astimezone(timezone.utc)
    rows = validate_disabled_policy(policy)
    actions: list[dict[str, Any]] = []
    for days in sorted({row["keep_days"] for row in rows}):
        actions.append({
            "action": "recorder.purge_entities",
            "data": {
                "entity_id": [row["entity_id"] for row in rows
                              if row["keep_days"] == days],
                "keep_days": days,
            },
            "rolling_cutoff_utc": (
                at_utc - timedelta(days=days)
            ).isoformat().replace("+00:00", "Z"),
            "execution_enabled": False,
        })
    return tuple(actions)
