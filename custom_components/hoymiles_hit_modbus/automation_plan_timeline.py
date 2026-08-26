"""Bounded observation-only contract for Aurora automation timelines.

The structures in this module are deliberately independent from Home
Assistant.  Optimizers may return an immutable trace made from calculations
they already perform, while the HA adapter validates and publishes a bounded
native-attribute projection.  Nothing in this module grants execution
authority or feeds a value back into an optimizer.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
import re
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = 1
DISPLAY_TIMEZONE = "Europe/Warsaw"
SLOT_MINUTES = 30
RCM_SLOT_MINUTES = 15
MAX_POINTS = 192
MAX_SERIALIZED_BYTES = 262_144
MAX_SOURCES = 16
PLAN_REVISION_SCOPE = "runtime"
ACTIVE_SCOPE = "publication_snapshot"

KINDS = frozenset({"actual", "forecast_plan"})
QUALITIES = frozenset(
    {"complete", "partial", "degraded", "stale", "pending", "unavailable"}
)
ACTIONS = frozenset(
    {
        "idle",
        "export",
        "battery_charge",
        "grid_support",
        "grid_support_and_charge",
        "absorb_pv",
        "limit_export",
        "monitor",
        "hold",
        "release_export",
        "restore",
        "grid_discharge_preparation",
        "preserve_headroom",
        "blocked",
        "unavailable",
    }
)
POLICY_IDS = frozenset({"rce", "tariff", "rcm"})
SENSOR_STATES = frozenset({"current", "pending", "unavailable"})
BOUNDED_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "policy_id",
        "config_entry_id",
        "generated_at",
        "horizon_start",
        "horizon_end",
        "actual_until",
        "forecast_from",
        "timezone",
        "slot_minutes",
        "point_count",
        "input_revision",
        "plan_revision",
        "plan_revision_scope",
        "result_current",
        "recalculation_pending",
        "quality",
        "blocker_code",
        "active_scope",
        "active_observed_at",
        "plan_entity_id",
        "current_actual",
        "sources",
        "points",
    }
)
PENDING_FIELD = "pending_input_revision"

COMMON_POINT_FIELDS = frozenset(
    {
        "start",
        "end",
        "kind",
        "pv_kw",
        "load_kw",
        "battery_kw",
        "grid_kw",
        "grid_import_kw",
        "grid_export_kw",
        "soc_percent",
        "baseline_soc_percent",
        "protected_soc_floor_percent",
        "action_code",
        "active",
        "selected",
        "quality",
        "policy",
    }
)
OPTIONAL_POINT_FIELDS = frozenset({"target_soc_percent", "required_headroom_kwh"})
CURRENT_ACTUAL_FIELDS = frozenset(
    {
        "observed_at",
        "pv_kw",
        "load_kw",
        "battery_kw",
        "grid_kw",
        "soc_percent",
        "quality",
        "source_ages_seconds",
    }
)
CURRENT_ACTUAL_AGE_FIELDS = frozenset({"pv", "load", "battery", "grid", "soc"})
SOURCE_FIELDS = frozenset({"role", "entity_id"})
RCE_POLICY_FIELDS = frozenset(
    {
        "sell_price_pln_kwh",
        "planned_export_kwh",
        "target_discharge_kw",
        "target_tolerance_kw",
        "expected_revenue_pln",
    }
)
TARIFF_POLICY_FIELDS = frozenset(
    {
        "buy_price_pln_kwh",
        "tariff_zone",
        "planned_import_kwh",
        "planned_charge_kw",
        "expected_cost_pln",
        "expected_saving_pln",
    }
)
RCM_POLICY_FIELDS = frozenset(
    {
        "voltage_risk_code",
        "planned_export_limit_percent",
        "planned_pre_discharge_kw",
        "headroom_shortfall_kwh",
        "control_mode",
    }
)


class TimelineValidationError(ValueError):
    """Raised when a timeline would violate the frozen public contract."""


@dataclass(frozen=True, slots=True)
class RCEPolicyPoint:
    """Exact RCE policy values retained for one native optimizer interval."""

    sell_price_pln_kwh: float | None
    planned_export_kwh: float
    target_discharge_kw: float
    target_tolerance_kw: float
    expected_revenue_pln: float | None


@dataclass(frozen=True, slots=True)
class TariffPolicyPoint:
    """Exact tariff policy values retained for one native optimizer interval."""

    buy_price_pln_kwh: float
    tariff_zone: str
    planned_import_kwh: float
    planned_charge_kw: float
    expected_cost_pln: float
    expected_saving_pln: float | None


@dataclass(frozen=True, slots=True)
class RCMPolicyPoint:
    """Bounded RCEm policy context without any future-voltage projection."""

    voltage_risk_code: str
    planned_export_limit_percent: float | None
    planned_pre_discharge_kw: float | None
    headroom_shortfall_kwh: float | None
    control_mode: str


@dataclass(frozen=True, slots=True)
class TimelineTracePoint:
    """One immutable optimizer-output trace point, expressed as slot energy."""

    start: datetime
    end: datetime
    pv_kwh: float | None
    load_kwh: float | None
    battery_delta_kwh: float | None
    grid_import_kwh: float | None
    grid_export_kwh: float | None
    soc_percent: float | None
    baseline_soc_percent: float | None
    protected_soc_floor_percent: float | None
    action_code: str
    selected: bool
    quality: str
    policy: RCEPolicyPoint | TariffPolicyPoint | RCMPolicyPoint
    target_soc_percent: float | None = None
    required_headroom_kwh: float | None = None


@dataclass(frozen=True, slots=True)
class OptimizerTimelineTrace:
    """Immutable output-only trace returned with an existing optimizer result."""

    policy_id: str
    points: tuple[TimelineTracePoint, ...]
    quality: str = "complete"
    blocker_code: str | None = None


@dataclass(frozen=True, slots=True)
class CurrentActualSnapshot:
    """One bounded, age-qualified physical snapshot captured at publication."""

    observed_at: datetime | None
    pv_kw: float | None
    load_kw: float | None
    battery_kw: float | None
    grid_kw: float | None
    soc_percent: float | None
    quality: str
    source_ages_seconds: Mapping[str, float | None]


def _number(value: Any, *, nullable: bool = False, name: str = "number") -> None:
    if value is None and nullable:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TimelineValidationError(f"{name} must be a JSON number")
    if not isfinite(float(value)):
        raise TimelineValidationError(f"{name} must be finite")


def _bounded_number(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    nullable: bool = False,
    name: str,
) -> None:
    _number(value, nullable=nullable, name=name)
    if value is None:
        return
    numeric = float(value)
    if minimum is not None and numeric < minimum:
        raise TimelineValidationError(f"{name} is below its minimum")
    if maximum is not None and numeric > maximum:
        raise TimelineValidationError(f"{name} is above its maximum")


def utc_iso(value: datetime) -> str:
    """Serialize one aware timestamp as an explicit UTC ISO-8601 value."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise TimelineValidationError("timeline timestamps must be timezone-aware")
    try:
        converted = value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError) as err:
        raise TimelineValidationError("timeline timestamp is invalid") from err
    return converted.isoformat().replace("+00:00", "Z")


def parse_utc_iso(value: Any, *, nullable: bool = False, name: str) -> datetime | None:
    """Parse and require an aware timestamp whose serialized offset is UTC."""

    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise TimelineValidationError(f"{name} must be a UTC ISO timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as err:
        raise TimelineValidationError(f"{name} is malformed") from err
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise TimelineValidationError(f"{name} must use UTC")
    return parsed.astimezone(timezone.utc)


def validate_point_count(count: int) -> None:
    """Enforce the hard bounded point limit."""

    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= MAX_POINTS:
        raise TimelineValidationError(f"point_count must be between 0 and {MAX_POINTS}")


def validate_serialized_byte_count(byte_count: int) -> None:
    """Enforce the hard compact UTF-8 payload limit."""

    if (
        isinstance(byte_count, bool)
        or not isinstance(byte_count, int)
        or byte_count < 0
        or byte_count > MAX_SERIALIZED_BYTES
    ):
        raise TimelineValidationError(
            f"serialized payload exceeds {MAX_SERIALIZED_BYTES} bytes"
        )


def compact_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return deterministic compact JSON bytes, rejecting NaN and Infinity."""

    try:
        serialized = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as err:
        raise TimelineValidationError("payload is not finite JSON") from err
    validate_serialized_byte_count(len(serialized))
    return serialized


def _policy_dict(
    policy: RCEPolicyPoint | TariffPolicyPoint | RCMPolicyPoint,
) -> dict[str, Any]:
    if isinstance(policy, RCEPolicyPoint):
        return {
            "sell_price_pln_kwh": policy.sell_price_pln_kwh,
            "planned_export_kwh": policy.planned_export_kwh,
            "target_discharge_kw": policy.target_discharge_kw,
            "target_tolerance_kw": policy.target_tolerance_kw,
            "expected_revenue_pln": policy.expected_revenue_pln,
        }
    if isinstance(policy, TariffPolicyPoint):
        return {
            "buy_price_pln_kwh": policy.buy_price_pln_kwh,
            "tariff_zone": policy.tariff_zone,
            "planned_import_kwh": policy.planned_import_kwh,
            "planned_charge_kw": policy.planned_charge_kw,
            "expected_cost_pln": policy.expected_cost_pln,
            "expected_saving_pln": policy.expected_saving_pln,
        }
    if isinstance(policy, RCMPolicyPoint):
        return {
            "voltage_risk_code": policy.voltage_risk_code,
            "planned_export_limit_percent": (
                policy.planned_export_limit_percent
            ),
            "planned_pre_discharge_kw": policy.planned_pre_discharge_kw,
            "headroom_shortfall_kwh": policy.headroom_shortfall_kwh,
            "control_mode": policy.control_mode,
        }
    raise TimelineValidationError("wrong policy object")


def _snapshot_dict(snapshot: CurrentActualSnapshot) -> dict[str, Any]:
    return {
        "observed_at": (
            utc_iso(snapshot.observed_at) if snapshot.observed_at is not None else None
        ),
        "pv_kw": snapshot.pv_kw,
        "load_kw": snapshot.load_kw,
        "battery_kw": snapshot.battery_kw,
        "grid_kw": snapshot.grid_kw,
        "soc_percent": snapshot.soc_percent,
        "quality": snapshot.quality,
        "source_ages_seconds": {
            key: snapshot.source_ages_seconds.get(key)
            for key in ("pv", "load", "battery", "grid", "soc")
        },
    }


def unavailable_snapshot() -> CurrentActualSnapshot:
    """Return the bounded null snapshot used before physical data are usable."""

    return CurrentActualSnapshot(
        observed_at=None,
        pv_kw=None,
        load_kw=None,
        battery_kw=None,
        grid_kw=None,
        soc_percent=None,
        quality="unavailable",
        source_ages_seconds={
            "pv": None,
            "load": None,
            "battery": None,
            "grid": None,
            "soc": None,
        },
    )


def _trace_point_dict(
    point: TimelineTracePoint,
    *,
    generated_at: datetime,
    physical_active: bool | None,
) -> dict[str, Any]:
    start = point.start.astimezone(timezone.utc)
    end = point.end.astimezone(timezone.utc)
    duration_hours = (end - start).total_seconds() / 3600.0
    if duration_hours <= 0.0:
        raise TimelineValidationError("point interval must be positive")
    grid_import_kw = (
        point.grid_import_kwh / duration_hours
        if point.grid_import_kwh is not None
        else None
    )
    grid_export_kw = (
        point.grid_export_kwh / duration_hours
        if point.grid_export_kwh is not None
        else None
    )
    grid_kw = (
        grid_import_kw - grid_export_kw
        if grid_import_kw is not None and grid_export_kw is not None
        else None
    )
    observation_interval = (
        point.selected
        and start <= generated_at.astimezone(timezone.utc) < end
    )
    active = physical_active if observation_interval else False
    result: dict[str, Any] = {
        "start": utc_iso(start),
        "end": utc_iso(end),
        "kind": "forecast_plan",
        "pv_kw": (
            point.pv_kwh / duration_hours if point.pv_kwh is not None else None
        ),
        "load_kw": (
            point.load_kwh / duration_hours
            if point.load_kwh is not None
            else None
        ),
        "battery_kw": (
            point.battery_delta_kwh / duration_hours
            if point.battery_delta_kwh is not None
            else None
        ),
        "grid_kw": grid_kw,
        "grid_import_kw": grid_import_kw,
        "grid_export_kw": grid_export_kw,
        "soc_percent": point.soc_percent,
        "baseline_soc_percent": point.baseline_soc_percent,
        "protected_soc_floor_percent": point.protected_soc_floor_percent,
        "action_code": point.action_code,
        "active": active,
        "selected": point.selected,
        "quality": point.quality,
        "policy": _policy_dict(point.policy),
    }
    if point.target_soc_percent is not None:
        result["target_soc_percent"] = point.target_soc_percent
    if point.required_headroom_kwh is not None:
        result["required_headroom_kwh"] = point.required_headroom_kwh
    return result


def slot_minutes_for_policy(policy_id: str) -> int:
    """Return the frozen native resolution for one supported policy."""

    if policy_id not in POLICY_IDS:
        raise TimelineValidationError("unknown policy_id")
    return RCM_SLOT_MINUTES if policy_id == "rcm" else SLOT_MINUTES


def build_current_payload(
    trace: OptimizerTimelineTrace,
    *,
    config_entry_id: str,
    generated_at: datetime,
    input_revision: int,
    plan_revision: int,
    plan_entity_id: str,
    current_actual: CurrentActualSnapshot,
    sources: Sequence[Mapping[str, str]],
    physical_active: bool | None,
    active_observed_at: datetime | None = None,
) -> dict[str, Any]:
    """Build and validate one complete in-memory current publication."""

    if trace.policy_id not in POLICY_IDS:
        raise TimelineValidationError("unknown policy_id")
    points = [
        _trace_point_dict(
            point,
            generated_at=generated_at,
            physical_active=physical_active,
        )
        for point in trace.points
    ]
    horizon_start = points[0]["start"] if points else None
    horizon_end = points[-1]["end"] if points else None
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "policy_id": trace.policy_id,
        "config_entry_id": config_entry_id,
        "generated_at": utc_iso(generated_at),
        "horizon_start": horizon_start,
        "horizon_end": horizon_end,
        "actual_until": None,
        "forecast_from": horizon_start,
        "timezone": DISPLAY_TIMEZONE,
        "slot_minutes": slot_minutes_for_policy(trace.policy_id),
        "point_count": len(points),
        "input_revision": input_revision,
        "plan_revision": plan_revision,
        "plan_revision_scope": PLAN_REVISION_SCOPE,
        "result_current": True,
        "recalculation_pending": False,
        "quality": trace.quality,
        "blocker_code": trace.blocker_code,
        "active_scope": ACTIVE_SCOPE,
        "active_observed_at": (
            utc_iso(active_observed_at or generated_at)
            if physical_active is not None
            else None
        ),
        "plan_entity_id": plan_entity_id,
        "current_actual": _snapshot_dict(current_actual),
        "sources": [dict(source) for source in sources],
        "points": points,
    }
    validate_payload(payload, expected_state="current")
    return payload


def build_unavailable_payload(
    *,
    policy_id: str,
    config_entry_id: str,
    generated_at: datetime,
    input_revision: int,
    plan_entity_id: str | None,
    blocker_code: str,
    plan_revision: int = 0,
) -> dict[str, Any]:
    """Build a bounded unavailable transition with no retained plan points."""

    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "policy_id": policy_id,
        "config_entry_id": config_entry_id,
        "generated_at": utc_iso(generated_at),
        "horizon_start": None,
        "horizon_end": None,
        "actual_until": None,
        "forecast_from": None,
        "timezone": DISPLAY_TIMEZONE,
        "slot_minutes": slot_minutes_for_policy(policy_id),
        "point_count": 0,
        "input_revision": input_revision,
        "plan_revision": plan_revision,
        "plan_revision_scope": PLAN_REVISION_SCOPE,
        "result_current": False,
        "recalculation_pending": False,
        "quality": "unavailable",
        "blocker_code": blocker_code,
        "active_scope": ACTIVE_SCOPE,
        "active_observed_at": None,
        "plan_entity_id": plan_entity_id,
        "current_actual": _snapshot_dict(unavailable_snapshot()),
        "sources": [],
        "points": [],
    }
    validate_payload(payload, expected_state="unavailable")
    return payload


def build_pending_payload(
    last_complete: Mapping[str, Any] | None,
    *,
    policy_id: str,
    config_entry_id: str,
    generated_at: datetime,
    pending_input_revision: int,
    plan_entity_id: str | None,
    plan_revision: int | None = None,
) -> dict[str, Any]:
    """Retain an old complete series without mixing it with new input data."""

    if last_complete is None:
        payload = build_unavailable_payload(
            policy_id=policy_id,
            config_entry_id=config_entry_id,
            generated_at=generated_at,
            input_revision=0,
            plan_entity_id=plan_entity_id,
            blocker_code="awaiting_first_calculation",
        )
        payload["quality"] = "pending"
        payload["blocker_code"] = "recalculation_pending"
    else:
        payload = deepcopy(dict(last_complete))
        if payload.get("policy_id") != policy_id or payload.get("config_entry_id") != config_entry_id:
            raise TimelineValidationError("pending transition cannot cross entries")
        payload["generated_at"] = utc_iso(generated_at)
        payload["quality"] = "pending"
        payload["blocker_code"] = "recalculation_pending"
    if plan_revision is not None:
        payload["plan_revision"] = plan_revision
    payload["result_current"] = False
    payload["recalculation_pending"] = True
    payload[PENDING_FIELD] = pending_input_revision
    validate_payload(payload, expected_state="pending")
    return payload


def _canonical_fingerprint_value(value: Any, *, field: str | None = None) -> Any:
    """Canonicalize exact JSON semantics without changing the public payload."""

    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return 0 if value == 0 else value
    if isinstance(value, float):
        numeric = value
        if not isfinite(numeric):
            raise TimelineValidationError("fingerprint number must be finite")
        if numeric == 0.0:
            return 0
        if numeric.is_integer():
            return int(numeric)
        return numeric
    if isinstance(value, Mapping):
        return {
            key: _canonical_fingerprint_value(item, field=key)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        items = [
            _canonical_fingerprint_value(item, field=None)
            for item in value
        ]
        if field == "sources":
            return sorted(
                items,
                key=lambda item: (
                    item.get("role", "") if isinstance(item, dict) else "",
                    item.get("entity_id", "") if isinstance(item, dict) else "",
                ),
            )
        return items
    raise TimelineValidationError("fingerprint payload is not finite JSON")


def semantic_fingerprint(payload: Mapping[str, Any]) -> str:
    """Hash semantic plan content while ignoring presentation-only age churn."""

    candidate = deepcopy(dict(payload))
    candidate.pop("generated_at", None)
    candidate.pop("plan_revision", None)
    candidate.pop(PENDING_FIELD, None)
    candidate.pop("active_observed_at", None)
    actual = candidate.get("current_actual")
    if isinstance(actual, dict):
        actual.pop("observed_at", None)
        actual.pop("source_ages_seconds", None)
    canonical = _canonical_fingerprint_value(candidate)
    return hashlib.sha256(compact_json_bytes(canonical)).hexdigest()


def next_plan_revision(
    current_revision: int,
    previous_fingerprint: str | None,
    candidate_fingerprint: str,
) -> int:
    """Return a runtime-monotonic revision for one complete semantic plan."""

    if (
        isinstance(current_revision, bool)
        or not isinstance(current_revision, int)
        or current_revision < 0
    ):
        raise TimelineValidationError("current plan revision is invalid")
    if not isinstance(candidate_fingerprint, str) or not candidate_fingerprint:
        raise TimelineValidationError("candidate fingerprint is invalid")
    if current_revision == 0 or previous_fingerprint is None:
        return max(current_revision, 0) + 1
    return (
        current_revision
        if previous_fingerprint == candidate_fingerprint
        else current_revision + 1
    )


def validate_payload(payload: Mapping[str, Any], *, expected_state: str) -> None:
    """Validate exact schema, intervals, signs, policy fields and hard limits."""

    if expected_state not in SENSOR_STATES:
        raise TimelineValidationError("unknown sensor state")
    expected_fields = set(TOP_LEVEL_FIELDS)
    if expected_state == "pending":
        expected_fields.add(PENDING_FIELD)
    if set(payload) != expected_fields:
        raise TimelineValidationError("top-level schema fields differ")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise TimelineValidationError("schema_version differs")
    if payload["policy_id"] not in POLICY_IDS:
        raise TimelineValidationError("unknown policy_id")
    if not isinstance(payload["config_entry_id"], str) or not payload["config_entry_id"]:
        raise TimelineValidationError("config_entry_id is required")
    parse_utc_iso(payload["generated_at"], name="generated_at")
    if (
        payload["timezone"] != DISPLAY_TIMEZONE
        or payload["slot_minutes"]
        != slot_minutes_for_policy(payload["policy_id"])
    ):
        raise TimelineValidationError("timezone or slot resolution differs")
    if payload["plan_revision_scope"] != PLAN_REVISION_SCOPE:
        raise TimelineValidationError("plan_revision_scope differs")
    if payload["active_scope"] != ACTIVE_SCOPE:
        raise TimelineValidationError("active_scope differs")
    parse_utc_iso(
        payload["active_observed_at"],
        nullable=True,
        name="active_observed_at",
    )
    for name in ("input_revision", "plan_revision"):
        value = payload[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise TimelineValidationError(f"{name} must be a non-negative integer")
    if not isinstance(payload["result_current"], bool) or not isinstance(
        payload["recalculation_pending"], bool
    ):
        raise TimelineValidationError("revision flags must be boolean")
    if payload["quality"] not in QUALITIES:
        raise TimelineValidationError("unknown quality")
    blocker = payload["blocker_code"]
    if blocker is not None and (
        not isinstance(blocker, str) or BOUNDED_CODE_PATTERN.fullmatch(blocker) is None
    ):
        raise TimelineValidationError("blocker_code must be null or a bounded code")
    plan_entity_id = payload["plan_entity_id"]
    if plan_entity_id is not None and (
        not isinstance(plan_entity_id, str) or not plan_entity_id.startswith("sensor.")
    ):
        raise TimelineValidationError("plan_entity_id is invalid")

    points = payload["points"]
    if not isinstance(points, list):
        raise TimelineValidationError("points must be a list of objects")
    validate_point_count(len(points))
    if payload["point_count"] != len(points):
        raise TimelineValidationError("point_count differs from points")

    if expected_state == "current":
        if not payload["result_current"] or payload["recalculation_pending"]:
            raise TimelineValidationError("current flags differ")
        if payload["quality"] not in {"complete", "partial", "degraded", "stale"}:
            raise TimelineValidationError("current quality differs")
        if not points or payload["plan_revision"] < 1:
            raise TimelineValidationError("current plan must contain a revision and points")
    elif expected_state == "pending":
        if payload["result_current"] or not payload["recalculation_pending"]:
            raise TimelineValidationError("pending flags differ")
        if payload["quality"] != "pending":
            raise TimelineValidationError("pending quality differs")
        pending_revision = payload[PENDING_FIELD]
        if isinstance(pending_revision, bool) or not isinstance(pending_revision, int) or pending_revision < 0:
            raise TimelineValidationError("pending_input_revision is invalid")
    else:
        if payload["result_current"] or payload["recalculation_pending"]:
            raise TimelineValidationError("unavailable flags differ")
        if (
            payload["quality"] != "unavailable"
            or blocker is None
            or points
            or payload["active_observed_at"] is not None
        ):
            raise TimelineValidationError("unavailable payload differs")

    sources = payload["sources"]
    if not isinstance(sources, list) or len(sources) > MAX_SOURCES:
        raise TimelineValidationError("sources exceed the bounded list")
    for source in sources:
        if not isinstance(source, dict) or set(source) != SOURCE_FIELDS:
            raise TimelineValidationError("source record fields differ")
        if not all(isinstance(source[key], str) and source[key] for key in SOURCE_FIELDS):
            raise TimelineValidationError("source record values must be strings")

    _validate_current_actual(payload["current_actual"])
    _validate_points(payload["policy_id"], points)

    horizon_start = parse_utc_iso(
        payload["horizon_start"], nullable=True, name="horizon_start"
    )
    horizon_end = parse_utc_iso(
        payload["horizon_end"], nullable=True, name="horizon_end"
    )
    parse_utc_iso(payload["actual_until"], nullable=True, name="actual_until")
    forecast_from = parse_utc_iso(
        payload["forecast_from"], nullable=True, name="forecast_from"
    )
    if points:
        if horizon_start is None or horizon_end is None or forecast_from is None:
            raise TimelineValidationError("point horizon is missing")
        if horizon_start >= horizon_end:
            raise TimelineValidationError("horizon interval is invalid")
        if horizon_start != parse_utc_iso(points[0]["start"], name="point.start"):
            raise TimelineValidationError("horizon_start differs")
        if horizon_end != parse_utc_iso(points[-1]["end"], name="point.end"):
            raise TimelineValidationError("horizon_end differs")
    elif any(value is not None for value in (horizon_start, horizon_end, forecast_from)):
        raise TimelineValidationError("empty points require an empty horizon")
    compact_json_bytes(payload)


def _validate_current_actual(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != CURRENT_ACTUAL_FIELDS:
        raise TimelineValidationError("current_actual fields differ")
    parse_utc_iso(value["observed_at"], nullable=True, name="current_actual.observed_at")
    for key in ("pv_kw", "load_kw", "battery_kw", "grid_kw", "soc_percent"):
        _number(value[key], nullable=True, name=f"current_actual.{key}")
    for key in ("pv_kw", "load_kw"):
        if value[key] is not None and value[key] < 0:
            raise TimelineValidationError(f"current_actual.{key} must be non-negative")
    if value["soc_percent"] is not None and not 0 <= value["soc_percent"] <= 100:
        raise TimelineValidationError("current_actual.soc_percent is out of range")
    if value["quality"] not in QUALITIES:
        raise TimelineValidationError("current_actual quality differs")
    ages = value["source_ages_seconds"]
    if not isinstance(ages, dict) or set(ages) != CURRENT_ACTUAL_AGE_FIELDS:
        raise TimelineValidationError("source age fields differ")
    for key, age in ages.items():
        _number(age, nullable=True, name=f"source_ages_seconds.{key}")


def _validate_points(policy_id: str, points: list[Any]) -> None:
    previous_end: datetime | None = None
    seen: set[tuple[datetime, datetime]] = set()
    for index, point in enumerate(points):
        if not isinstance(point, dict):
            raise TimelineValidationError("point must be an object")
        fields = set(point)
        if not COMMON_POINT_FIELDS <= fields or fields - COMMON_POINT_FIELDS - OPTIONAL_POINT_FIELDS:
            raise TimelineValidationError("point fields differ")
        if policy_id != "rcm" and "required_headroom_kwh" in point:
            raise TimelineValidationError("AP-1 policies cannot expose required headroom")
        if policy_id == "rce" and "target_soc_percent" in point:
            raise TimelineValidationError("RCE cannot expose target SOC")
        start = parse_utc_iso(point["start"], name=f"points[{index}].start")
        end = parse_utc_iso(point["end"], name=f"points[{index}].end")
        assert start is not None and end is not None
        duration_seconds = (end - start).total_seconds()
        native_minutes = slot_minutes_for_policy(policy_id)
        native_seconds = native_minutes * 60
        partial_first = bool(
            policy_id in {"rce", "tariff"}
            and index == 0
            and 0 < duration_seconds < native_seconds
        )
        if duration_seconds != native_seconds and not partial_first:
            raise TimelineValidationError("point is not one native policy slot")
        if partial_first and (
            end.second != 0
            or end.microsecond != 0
            or end.minute % native_minutes != 0
        ):
            raise TimelineValidationError(
                "partial first point does not end on the native UTC grid"
            )
        if previous_end is not None and start != previous_end:
            raise TimelineValidationError("timeline contains a gap or overlap")
        identity = (start, end)
        if identity in seen:
            raise TimelineValidationError("duplicate point interval")
        seen.add(identity)
        previous_end = end
        if point["kind"] not in KINDS or point["quality"] not in QUALITIES:
            raise TimelineValidationError("point kind or quality differs")
        if point["action_code"] not in ACTIONS:
            raise TimelineValidationError("unknown action")
        if point["active"] is not None and not isinstance(point["active"], bool):
            raise TimelineValidationError("point active flag must be boolean or null")
        if not isinstance(point["selected"], bool):
            raise TimelineValidationError("point flags must be boolean")
        for key in (
            "pv_kw",
            "load_kw",
            "battery_kw",
            "grid_kw",
            "grid_import_kw",
            "grid_export_kw",
            "soc_percent",
            "baseline_soc_percent",
            "protected_soc_floor_percent",
        ):
            _number(point[key], nullable=True, name=f"point.{key}")
        for key in ("pv_kw", "load_kw", "grid_import_kw", "grid_export_kw"):
            if point[key] is not None and point[key] < 0:
                raise TimelineValidationError(f"point.{key} must be non-negative")
        for key in ("soc_percent", "baseline_soc_percent", "protected_soc_floor_percent"):
            if point[key] is not None and not 0 <= point[key] <= 100:
                raise TimelineValidationError(f"point.{key} is out of range")
        if "target_soc_percent" in point:
            _bounded_number(
                point["target_soc_percent"],
                minimum=0.0,
                maximum=100.0,
                nullable=True,
                name="point.target_soc_percent",
            )
        if "required_headroom_kwh" in point:
            _bounded_number(
                point["required_headroom_kwh"],
                minimum=0.0,
                nullable=True,
                name="point.required_headroom_kwh",
            )
        _validate_grid_signs(point)
        _validate_power_balance(point)
        _validate_policy(policy_id, point["policy"])


def _validate_grid_signs(point: Mapping[str, Any]) -> None:
    grid = point["grid_kw"]
    imported = point["grid_import_kw"]
    exported = point["grid_export_kw"]
    if grid is None:
        if imported is not None or exported is not None:
            raise TimelineValidationError("missing grid balance requires null split values")
        return
    if imported is None or exported is None:
        raise TimelineValidationError("finite grid balance requires split values")
    if imported > 0 and exported > 0:
        raise TimelineValidationError("grid import and export cannot both be positive")
    tolerance = 1e-9
    if abs(imported - max(grid, 0.0)) > tolerance or abs(exported - max(-grid, 0.0)) > tolerance:
        raise TimelineValidationError("grid split is inconsistent with canonical sign")


def _validate_power_balance(point: Mapping[str, Any]) -> None:
    values = tuple(point[key] for key in ("pv_kw", "load_kw", "battery_kw", "grid_kw"))
    if any(value is None for value in values):
        return
    pv, load, battery, grid = (float(value) for value in values)
    if abs(grid - (load + battery - pv)) > 1e-8:
        raise TimelineValidationError("power balance differs from canonical signs")


def _validate_policy(policy_id: str, policy: Any) -> None:
    if not isinstance(policy, dict):
        raise TimelineValidationError("policy must be an object")
    expected = (
        RCE_POLICY_FIELDS
        if policy_id == "rce"
        else TARIFF_POLICY_FIELDS
        if policy_id == "tariff"
        else RCM_POLICY_FIELDS
    )
    if set(policy) != expected:
        raise TimelineValidationError("wrong policy object")
    text_fields = (
        {"tariff_zone"}
        if policy_id == "tariff"
        else {"voltage_risk_code", "control_mode"}
        if policy_id == "rcm"
        else set()
    )
    numeric_fields = expected - text_fields
    for key in numeric_fields:
        _number(policy[key], nullable=True, name=f"policy.{key}")
    if policy_id == "tariff" and (
        not isinstance(policy["tariff_zone"], str)
        or BOUNDED_CODE_PATTERN.fullmatch(policy["tariff_zone"]) is None
    ):
        raise TimelineValidationError("tariff_zone must be a bounded code")
    if policy_id == "rcm":
        for key in text_fields:
            value = policy[key]
            if (
                not isinstance(value, str)
                or BOUNDED_CODE_PATTERN.fullmatch(value) is None
            ):
                raise TimelineValidationError(f"{key} must be a bounded code")
        for key in (
            "planned_export_limit_percent",
            "planned_pre_discharge_kw",
            "headroom_shortfall_kwh",
        ):
            if policy[key] is not None and policy[key] < 0:
                raise TimelineValidationError(f"policy.{key} must be non-negative")
        if (
            policy["planned_export_limit_percent"] is not None
            and policy["planned_export_limit_percent"] > 100
        ):
            raise TimelineValidationError(
                "policy.planned_export_limit_percent is above its maximum"
            )
    for key in (
        "planned_export_kwh",
        "target_discharge_kw",
        "target_tolerance_kw",
        "planned_import_kwh",
        "planned_charge_kw",
        "expected_cost_pln",
    ):
        if key in policy and policy[key] is not None and policy[key] < 0:
            raise TimelineValidationError(f"policy.{key} must be non-negative")
