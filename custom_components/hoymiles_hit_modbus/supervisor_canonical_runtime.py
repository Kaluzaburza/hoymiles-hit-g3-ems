"""Build the one post-arbitration, output-only Supervisor SOC projection.

The three optimizer timelines remain independent diagnostic plans.  This
adapter never joins their published SOC endpoints.  For every common atomic
interval it runs the shared Supervisor arbiter, selects exactly one energy
trace, derives stored battery energy from that trace, and then lets the pure
canonical ledger rebuild one continuous SOC chain.

Nothing in this module acquires an owner or grants execution authority.
Future-slot eligibility is deliberately ``unverified`` because current
physical readback cannot authorize a later write.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any, Mapping

if __package__:
    from .automation_plan_timeline import parse_utc_iso, validate_payload
    from .baseline_energy_timeline import (
        BASELINE_POINT_KEYS,
        BASELINE_PAYLOAD_KEYS,
        BASELINE_TIMELINE_KIND,
        BASELINE_TIMELINE_SCHEMA_VERSION,
    )
    from .ems_supervisor import (
        ActuatorScope,
        EconomicValueStatus,
        ExecutionContext,
        ExportState,
        NeedClass,
        OwnerKind as ArbiterOwnerKind,
        PhysicalMode,
        PolicyCandidate as ArbiterPolicyCandidate,
        PolicyId as ArbiterPolicyId,
        PriorityClass,
        ReasonCode,
        RequestedAction as ArbiterRequestedAction,
        SCHEMA_VERSION as SUPERVISOR_SCHEMA_VERSION,
        SupervisorMode,
        arbitrate_supervisor,
        apply_minimum_grid_power,
        planned_grid_power_kw,
        planned_battery_grid_power_kw,
    )
    from .supervisor_active_bridge import settings_from_execution_source
    from .supervisor_runtime import build_rcm_candidate
    from .supervisor_canonical_ledger import (
        BatteryEnergyFlows,
        CanonicalExecutionLedger,
        CanonicalSlotInput,
        CommandExpectation,
        CommandTarget,
        ExpectationValue,
        OwnerKind,
        PlannedEnergy,
        PolicyCandidate,
        PolicyId,
        ReadbackExpectation,
        RequestedAction,
        StartEligibility,
        build_canonical_execution_ledger,
    )
else:  # Dependency-free repository tests import modules directly.
    from automation_plan_timeline import parse_utc_iso, validate_payload  # type: ignore[no-redef]
    from baseline_energy_timeline import (  # type: ignore[no-redef]
        BASELINE_POINT_KEYS,
        BASELINE_PAYLOAD_KEYS,
        BASELINE_TIMELINE_KIND,
        BASELINE_TIMELINE_SCHEMA_VERSION,
    )
    from ems_supervisor import (  # type: ignore[no-redef]
        ActuatorScope,
        EconomicValueStatus,
        ExecutionContext,
        ExportState,
        NeedClass,
        OwnerKind as ArbiterOwnerKind,
        PhysicalMode,
        PolicyCandidate as ArbiterPolicyCandidate,
        PolicyId as ArbiterPolicyId,
        PriorityClass,
        ReasonCode,
        RequestedAction as ArbiterRequestedAction,
        SCHEMA_VERSION as SUPERVISOR_SCHEMA_VERSION,
        SupervisorMode,
        arbitrate_supervisor,
        apply_minimum_grid_power,
        planned_grid_power_kw,
        planned_battery_grid_power_kw,
    )
    from supervisor_active_bridge import settings_from_execution_source  # type: ignore[no-redef]
    from supervisor_runtime import build_rcm_candidate  # type: ignore[no-redef]
    from supervisor_canonical_ledger import (  # type: ignore[no-redef]
        BatteryEnergyFlows,
        CanonicalExecutionLedger,
        CanonicalSlotInput,
        CommandExpectation,
        CommandTarget,
        ExpectationValue,
        OwnerKind,
        PlannedEnergy,
        PolicyCandidate,
        PolicyId,
        ReadbackExpectation,
        RequestedAction,
        StartEligibility,
        build_canonical_execution_ledger,
    )


POLICY_ORDER = ("rce", "tariff", "rcm")
MAX_HORIZON = timedelta(hours=48)
# Match each full-plan cadence plus the existing bounded 30-second scheduler
# jitter/cache grace.  The 15-second RCEm live loop must not masquerade as a
# fresh 48-hour forecast commit.
TIMELINE_MAX_AGE_SECONDS = {"rce": 150.0, "tariff": 150.0, "rcm": 630.0}
EXPECTED_TIMELINE_MAX_AGE_SECONDS = 330.0
MAX_FUTURE_SKEW_SECONDS = 5.0
ENERGY_EPSILON_KWH = 1e-7
MAX_EXPECTED_TIMELINE_POINTS = 96


class CanonicalRuntimeError(ValueError):
    """A source set cannot produce a truthful canonical projection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc(value: datetime, *, code: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalRuntimeError(code)
    return value.astimezone(timezone.utc)


def _number(
    value: Any,
    *,
    code: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CanonicalRuntimeError(code)
    result = float(value)
    if not math.isfinite(result):
        raise CanonicalRuntimeError(code)
    if minimum is not None and result < minimum:
        raise CanonicalRuntimeError(code)
    if maximum is not None and result > maximum:
        raise CanonicalRuntimeError(code)
    return 0.0 if result == 0.0 else result


def _optional_number(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float | None:
    try:
        return _number(
            value,
            code="invalid_candidate_target",
            minimum=minimum,
            maximum=maximum,
        )
    except CanonicalRuntimeError:
        return None


def _sha(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=lambda item: item.value,
        ).encode("utf-8")
    ).hexdigest()


def _point_times(point: Mapping[str, Any]) -> tuple[datetime, datetime]:
    try:
        start = parse_utc_iso(point["start"], name="point.start")
        end = parse_utc_iso(point["end"], name="point.end")
    except (KeyError, TypeError, ValueError) as err:
        raise CanonicalRuntimeError("invalid_timeline_interval") from err
    if start is None or end is None or end <= start:
        raise CanonicalRuntimeError("invalid_timeline_interval")
    return start, end


def canonical_timeline_dependencies(frame: Any) -> tuple[str, ...]:
    """Exclude RCEm only when explicit controls and ownership prove it is off.

    This applies solely to the output-only chart. Unknown controls, an active
    RCEm latch, or ambiguous ownership keep its normal freshness dependency.
    Never relabel an old RCEm trace as current or borrow its energy/reserve.
    """
    source = getattr(frame, "rcm", None)
    context = getattr(frame, "context", None)
    template = next((c for c in getattr(frame, "candidates", ())
                     if getattr(c, "policy_id", None) is ArbiterPolicyId.RCM), None)
    known_owners = {ArbiterOwnerKind.NONE, ArbiterOwnerKind.RCE, ArbiterOwnerKind.TARIFF}
    disabled = (
        source is not None and context is not None and template is not None
        and all(getattr(source, key, None) is False for key in (
            "allowed_by_user", "enabled", "export_control_enabled",
            "pre_discharge_enabled", "absorb_active", "export_active",
            "pre_discharge_active"))
        and template.allowed_by_user is False and template.enabled is False
        and template.active_latched is False and template.local_hard_stop is False
        and context.owner_kind in known_owners
        and context.transaction_owner_kind in known_owners
        and context.owner_conflict is False
    )
    return ("rce", "tariff") if disabled else POLICY_ORDER


def _validated_timelines(
    timelines: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime,
    required: tuple[str, ...] = POLICY_ORDER,
) -> dict[str, Mapping[str, Any]]:
    if (not isinstance(timelines, Mapping)
            or not set(required) <= set(timelines) <= set(POLICY_ORDER)):
        raise CanonicalRuntimeError("timeline_set_incomplete")
    output: dict[str, Mapping[str, Any]] = {}
    config_entry_id: str | None = None
    for policy_id in required:
        payload = timelines[policy_id]
        if not isinstance(payload, Mapping):
            raise CanonicalRuntimeError("invalid_timeline")
        try:
            validate_payload(payload, expected_state="current")
        except (TypeError, ValueError) as err:
            raise CanonicalRuntimeError(f"{policy_id}_timeline_invalid") from err
        if payload.get("policy_id") != policy_id:
            raise CanonicalRuntimeError("timeline_policy_mismatch")
        current_entry = payload.get("config_entry_id")
        if config_entry_id is None:
            config_entry_id = current_entry
        elif current_entry != config_entry_id:
            raise CanonicalRuntimeError("timeline_entry_mismatch")
        generated = parse_utc_iso(payload["generated_at"], name="generated_at")
        assert generated is not None
        age = (now - generated).total_seconds()
        if age < -MAX_FUTURE_SKEW_SECONDS:
            raise CanonicalRuntimeError(f"{policy_id}_timeline_future")
        if age > TIMELINE_MAX_AGE_SECONDS[policy_id]:
            raise CanonicalRuntimeError(f"{policy_id}_timeline_stale")
        output[policy_id] = payload
    return output


def _covering_point(
    payload: Mapping[str, Any],
    start: datetime,
    end: datetime,
) -> tuple[int, Mapping[str, Any]] | None:
    for index, point in enumerate(payload["points"]):
        point_start, point_end = _point_times(point)
        if point_start <= start and end <= point_end:
            return index, point
    return None


def _uncovered_point() -> Mapping[str, Any]:
    """Represent missing policy-local coverage without inventing zero data."""

    return {
        "selected": False,
        "action_code": "unavailable",
        "quality": "unavailable",
    }


def _common_boundaries(
    timelines: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime,
) -> tuple[datetime, ...]:
    all_boundaries: set[datetime] = set()
    for payload in timelines.values():
        points = payload["points"]
        for point in points:
            point_start, point_end = _point_times(point)
            all_boundaries.update((point_start, point_end))
    # Tariff is the existing explicit energy backbone used whenever the
    # arbiter selects no policy action.  Keep its truthful, continuous horizon
    # instead of intersecting it with shorter policy-local RCE/RCEm windows.
    # Missing non-backbone coverage is represented below as unavailable and
    # contributes neither invented energy nor execution authority.
    tariff_points = timelines["tariff"]["points"]
    tariff_start, _ = _point_times(tariff_points[0])
    _, tariff_end = _point_times(tariff_points[-1])
    horizon_start = max(tariff_start, now)
    horizon_end = min(tariff_end, horizon_start + MAX_HORIZON)
    if horizon_end <= horizon_start:
        raise CanonicalRuntimeError("timeline_horizon_empty")
    bounded = sorted(
        {horizon_start, horizon_end}
        | {
            boundary
            for boundary in all_boundaries
            if horizon_start < boundary < horizon_end
        }
    )
    # The pure ledger is deliberately bounded to 192 atomic intervals.  A
    # partial first interval can otherwise create one harmless 193rd edge.
    if len(bounded) > 193:
        bounded = bounded[:193]
    if len(bounded) < 2:
        raise CanonicalRuntimeError("timeline_horizon_empty")
    return tuple(bounded)


def _stored_power_by_point(
    payload: Mapping[str, Any],
    *,
    capacity_kwh: float,
    soc_field: str = "soc_percent",
) -> tuple[float, ...]:
    actual = payload.get("current_actual")
    if not isinstance(actual, Mapping):
        raise CanonicalRuntimeError("timeline_actual_missing")
    previous_soc = _number(
        actual.get("soc_percent"),
        code="timeline_actual_soc_missing",
        minimum=0.0,
        maximum=100.0,
    )
    result: list[float] = []
    for point in payload["points"]:
        start, end = _point_times(point)
        duration_hours = (end - start).total_seconds() / 3600.0
        soc_end = _number(
            point.get(soc_field),
            code="timeline_soc_missing",
            minimum=0.0,
            maximum=100.0,
        )
        stored_delta = capacity_kwh * (soc_end - previous_soc) / 100.0
        result.append(stored_delta / duration_hours)
        previous_soc = soc_end
    return tuple(result)


def _bounded_projection_delta(
    *,
    stored_energy_kwh: float,
    desired_delta_kwh: float,
    capacity_kwh: float,
    protected_reserve_percent: float,
    maximum_soc_percent: float = 100.0,
) -> float:
    """Bound an observation-only delta without creating reserve energy."""

    reserve_kwh = (
        capacity_kwh
        * min(max(protected_reserve_percent, 0.0), 100.0)
        / 100.0
    )
    maximum_kwh = (
        capacity_kwh
        * min(max(maximum_soc_percent, 0.0), 100.0)
        / 100.0
    )
    minimum_energy = min(stored_energy_kwh, reserve_kwh)
    # A physical reading above a newly lowered maximum is not corrected by
    # inventing a discharge; it merely blocks any further charging.
    maximum_energy = max(stored_energy_kwh, maximum_kwh)
    return min(
        max(desired_delta_kwh, minimum_energy - stored_energy_kwh),
        maximum_energy - stored_energy_kwh,
    )


def _bounded_observation_mapping(value: Any) -> bool:
    """Validate one already-bounded public provenance/source mapping."""

    if not isinstance(value, Mapping) or len(value) > 16:
        return False
    for key, item in value.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            return False
        if item is not None and (
            not isinstance(item, str) or not item or len(item) > 256
        ):
            return False
    return True


def _optional_observation_number(
    value: Any,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> bool:
    if value is None:
        return True
    try:
        _number(
            value,
            code="expected_timeline_number_invalid",
            minimum=minimum,
            maximum=maximum,
        )
    except CanonicalRuntimeError:
        return False
    return True


def _validated_expected_timeline(
    payload: Mapping[str, Any] | None,
    *,
    now: datetime,
    config_entry_id: Any,
) -> Mapping[str, Any] | None:
    """Return a trustworthy output-only baseline, otherwise no observation.

    Failure is deliberately represented as ``None``.  This source can enrich
    the cyan/P50 display path, but malformed or stale observation data must
    never invalidate the already-built authorization ledger.
    """

    if not isinstance(payload, Mapping) or set(payload) != BASELINE_PAYLOAD_KEYS:
        return None
    try:
        if (
            payload.get("schema_version") != BASELINE_TIMELINE_SCHEMA_VERSION
            or payload.get("timeline_kind") != BASELINE_TIMELINE_KIND
            or payload.get("output_only") is not True
            or payload.get("authority") is not False
            or payload.get("supervisor_candidate") is not False
            or payload.get("config_entry_id") != config_entry_id
            or payload.get("battery_sign_convention") != "positive_charge"
            or payload.get("interval_minutes") != 30
        ):
            return None
        state = payload.get("state")
        quality = payload.get("quality")
        if state not in {"current", "partial", "unavailable"} or quality != state:
            return None
        if payload.get("current") is not (state != "unavailable"):
            return None
        blocker = payload.get("blocker_code")
        if blocker is not None and (
            not isinstance(blocker, str) or not blocker or len(blocker) > 256
        ):
            return None
        if not isinstance(payload.get("zero_export_confirmed"), bool):
            return None
        revision = payload.get("shared_inputs_revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            return None
        if not _bounded_observation_mapping(payload.get("source")) or not (
            _bounded_observation_mapping(payload.get("provenance"))
        ):
            return None
        generated = parse_utc_iso(payload.get("generated_at"), name="generated_at")
        if generated is None:
            return None
        age = (now - generated).total_seconds()
        if age < -MAX_FUTURE_SKEW_SECONDS or (
            age > EXPECTED_TIMELINE_MAX_AGE_SECONDS
        ):
            return None

        points = payload.get("points")
        point_count = payload.get("point_count")
        if (
            not isinstance(points, list)
            or isinstance(point_count, bool)
            or not isinstance(point_count, int)
            or point_count != len(points)
            or point_count > MAX_EXPECTED_TIMELINE_POINTS
        ):
            return None
        for field in (
            "current_actual_soc_percent",
            "effective_reserve_soc_percent",
            "effective_maximum_soc_percent",
        ):
            if not _optional_observation_number(
                payload.get(field), minimum=0.0, maximum=100.0
            ):
                return None
        for field, minimum, maximum in (
            ("pv_to_battery_efficiency", 0.01, 1.0),
            ("battery_to_home_efficiency", 0.01, 1.0),
            ("maximum_charge_power_kw", 0.0, 1_000.0),
            ("maximum_discharge_power_kw", 0.0, 1_000.0),
            ("system_ac_power_kw", 0.0, 1_000.0),
        ):
            if not _optional_observation_number(
                payload.get(field), minimum=minimum, maximum=maximum
            ):
                return None

        previous_end: datetime | None = None
        first_start: datetime | None = None
        for point in points:
            if not isinstance(point, Mapping) or set(point) != BASELINE_POINT_KEYS:
                return None
            point_start, point_end = _point_times(point)
            if (point_end - point_start).total_seconds() > 3600.0:
                return None
            if previous_end is not None and point_start != previous_end:
                return None
            if first_start is None:
                first_start = point_start
                if abs((first_start - generated).total_seconds()) > (
                    MAX_FUTURE_SKEW_SECONDS
                ):
                    return None
            previous_end = point_end
            point_quality = point.get("quality")
            if point_quality not in {"current", "partial", "unavailable"}:
                return None
            point_blocker = point.get("blocker_code")
            if point_blocker is not None and (
                not isinstance(point_blocker, str)
                or not point_blocker
                or len(point_blocker) > 256
            ):
                return None
            if not _bounded_observation_mapping(point.get("source")) or not (
                _bounded_observation_mapping(point.get("provenance"))
            ):
                return None
            numeric_fields = (
                ("pv_kw", 0.0, 1_000.0),
                ("load_kw", 0.0, 1_000.0),
                ("battery_kw", -1_000.0, 1_000.0),
                ("grid_import_kw", 0.0, 1_000.0),
                ("grid_export_kw", 0.0, 1_000.0),
                ("soc_start_percent", 0.0, 100.0),
                ("soc_end_percent", 0.0, 100.0),
                ("reserve_soc_percent", 0.0, 100.0),
            )
            for field, minimum, maximum in numeric_fields:
                if not _optional_observation_number(
                    point.get(field), minimum=minimum, maximum=maximum
                ):
                    return None
            if point_quality == "current" and (
                point_blocker is not None
                or any(point.get(field) is None for field, _, _ in numeric_fields)
            ):
                return None
            if (
                point_quality == "partial"
                and point_blocker == "hardware_soc_bounds_partial"
                and any(
                    point.get(field) is None
                    for field in (
                        "pv_kw",
                        "load_kw",
                        "soc_start_percent",
                        "soc_end_percent",
                    )
                )
            ):
                return None
        if (
            first_start is not None
            and previous_end is not None
            and previous_end - first_start > MAX_HORIZON
        ):
            return None
    except (CanonicalRuntimeError, TypeError, ValueError):
        return None
    return payload


def _provider_expected_point(
    payload: Mapping[str, Any] | None,
    *,
    start: datetime,
    end: datetime,
) -> tuple[Mapping[str, Any], str, bool] | None:
    """Return one complete provider-P50 point for cursor-based simulation."""

    if payload is None:
        return None
    covered = _covering_point(payload, start, end)
    if covered is None:
        return None
    _, point = covered
    point_quality = point.get("quality")
    blocker_code = point.get("blocker_code")
    if point_quality == "current" and blocker_code is None:
        expected_source = "provider_p50"
        partial_bounds = False
    elif (
        point_quality == "partial"
        and blocker_code == "hardware_soc_bounds_partial"
    ):
        # Missing a narrower hardware maximum is explicitly represented by
        # the mathematical 100% SOC ceiling in the neutral model.  This is
        # useful observation data, but it remains visibly partial and never
        # gains authority.
        expected_source = "provider_p50_partial_bounds"
        partial_bounds = True
    else:
        return None
    return point, expected_source, partial_bounds


def _expected_self_use_parameters(
    payload: Mapping[str, Any] | None,
) -> tuple[float, float, float, float, float, float, float] | None:
    """Read the exact neutral-model parameters published with raw P50.

    Older or partial payloads without this complete output-only contract are
    observation fallbacks.  Values are never inferred from already limited
    point flows because that would make a later action-created headroom look
    like a zero charge-power limit.
    """

    if payload is None:
        return None
    try:
        reserve = _number(
            payload.get("effective_reserve_soc_percent"),
            code="expected_timeline_reserve_invalid",
            minimum=0.0,
            maximum=100.0,
        )
        maximum = _number(
            payload.get("effective_maximum_soc_percent"),
            code="expected_timeline_maximum_invalid",
            minimum=0.0,
            maximum=100.0,
        )
        charge_efficiency = _number(
            payload.get("pv_to_battery_efficiency"),
            code="expected_timeline_charge_efficiency_invalid",
            minimum=0.01,
            maximum=1.0,
        )
        discharge_efficiency = _number(
            payload.get("battery_to_home_efficiency"),
            code="expected_timeline_discharge_efficiency_invalid",
            minimum=0.01,
            maximum=1.0,
        )
        maximum_charge_power = _number(
            payload.get("maximum_charge_power_kw"),
            code="expected_timeline_charge_limit_invalid",
            minimum=0.0,
            maximum=1_000.0,
        )
        maximum_discharge_power = _number(
            payload.get("maximum_discharge_power_kw"),
            code="expected_timeline_discharge_limit_invalid",
            minimum=0.0,
            maximum=1_000.0,
        )
        system_ac_power = _number(
            payload.get("system_ac_power_kw"),
            code="expected_timeline_system_ac_limit_invalid",
            minimum=0.0,
            maximum=1_000.0,
        )
    except CanonicalRuntimeError:
        return None
    if reserve > maximum:
        return None
    return (
        reserve,
        maximum,
        charge_efficiency,
        discharge_efficiency,
        maximum_charge_power,
        maximum_discharge_power,
        system_ac_power,
    )


def _neutral_self_use_delta(
    point: Mapping[str, Any],
    *,
    stored_energy_kwh: float,
    capacity_kwh: float,
    duration_hours: float,
    reserve_soc_percent: float,
    maximum_soc_percent: float,
    charge_efficiency: float,
    discharge_efficiency: float,
    maximum_charge_power_kw: float,
    maximum_discharge_power_kw: float,
    system_ac_power_kw: float,
    controlled_export_kwh: float = 0.0,
) -> tuple[float, float, float, float]:
    """Simulate one raw PV/LOAD interval from the current expected cursor.

    The returned tuple is stored-energy delta, potential PV, LOAD, and PV
    charge input.  This is intentionally the same directional math as the
    neutral baseline builder, including its shared installation-wide AC
    bridge.  It starts from the cursor after every prior selected EMS action
    rather than replaying a precomputed SOC difference.
    """

    pv_kw = _number(
        point.get("pv_kw"),
        code="canonical_expected_pv_missing",
        minimum=0.0,
    )
    load_kw = _number(
        point.get("load_kw"),
        code="canonical_expected_load_missing",
        minimum=0.0,
    )
    potential_pv = pv_kw * duration_hours
    expected_load = load_kw * duration_hours
    system_energy_kwh = system_ac_power_kw * duration_hours
    pv_to_load_kwh = min(potential_pv, expected_load, system_energy_kwh)
    remaining_pv_kwh = max(potential_pv - pv_to_load_kwh, 0.0)
    remaining_load_kwh = max(expected_load - pv_to_load_kwh, 0.0)
    remaining_bridge_kwh = max(system_energy_kwh - pv_to_load_kwh, 0.0)
    if remaining_pv_kwh > ENERGY_EPSILON_KWH:
        bridge_charge_input_kwh = max(
            remaining_bridge_kwh - max(controlled_export_kwh, 0.0),
            0.0,
        )
        maximum_energy = capacity_kwh * maximum_soc_percent / 100.0
        headroom_kwh = max(maximum_energy - stored_energy_kwh, 0.0)
        pv_charge_input = min(
            remaining_pv_kwh,
            bridge_charge_input_kwh,
            maximum_charge_power_kw * duration_hours / charge_efficiency,
            headroom_kwh / charge_efficiency,
        )
        stored_delta = pv_charge_input * charge_efficiency
    elif remaining_load_kwh > ENERGY_EPSILON_KWH:
        reserve_kwh = capacity_kwh * reserve_soc_percent / 100.0
        usable_stored_kwh = max(stored_energy_kwh - reserve_kwh, 0.0)
        delivered_kwh = min(
            remaining_load_kwh,
            remaining_bridge_kwh,
            maximum_discharge_power_kw * duration_hours * discharge_efficiency,
            usable_stored_kwh * discharge_efficiency,
        )
        stored_delta = -delivered_kwh / discharge_efficiency
        pv_charge_input = 0.0
    else:
        stored_delta = 0.0
        pv_charge_input = 0.0
    return stored_delta, potential_pv, expected_load, pv_charge_input


def _rcm_action_segment_seconds(
    point: Mapping[str, Any],
    policy: Mapping[str, Any],
    *,
    start: datetime,
    end: datetime,
) -> tuple[float, float, float, float]:
    """Return natural-before, active, natural-after, and full action seconds."""

    point_start, point_end = _point_times(point)
    point_seconds = (point_end - point_start).total_seconds()
    offset_start = _number(
        policy.get("action_start_offset_seconds"),
        code="canonical_expected_rcm_interval_missing",
        minimum=0.0,
        maximum=point_seconds,
    )
    offset_end = _number(
        policy.get("action_end_offset_seconds"),
        code="canonical_expected_rcm_interval_missing",
        minimum=offset_start,
        maximum=point_seconds,
    )
    action_window_seconds = offset_end - offset_start
    if action_window_seconds <= 0.0:
        raise CanonicalRuntimeError("canonical_expected_rcm_interval_empty")
    action_start = point_start + timedelta(seconds=offset_start)
    action_end = point_start + timedelta(seconds=offset_end)
    overlap_start = max(start, action_start)
    overlap_end = min(end, action_end)
    action_seconds = max((overlap_end - overlap_start).total_seconds(), 0.0)
    slot_seconds = (end - start).total_seconds()
    if action_seconds <= 0.0:
        return (
            slot_seconds if end <= action_start else 0.0,
            0.0,
            slot_seconds if start >= action_end else 0.0,
            action_window_seconds,
        )
    return (
        max((overlap_start - start).total_seconds(), 0.0),
        action_seconds,
        max((end - overlap_end).total_seconds(), 0.0),
        action_window_seconds,
    )


def augment_canonical_projection_payload(
    payload: dict[str, Any],
    *,
    frame: Any,
    timelines: Mapping[str, Mapping[str, Any]],
    expected_timeline: Mapping[str, Any] | None,
    usable_capacity_kwh: float,
    freshness_now: datetime | None = None,
) -> dict[str, Any]:
    """Attach a provider-P50 display path beside conservative authorization.

    The canonical ledger remains the sole, fail-closed authorization
    projection.  This sidecar starts from the same fresh physical SOC, uses
    the neutral output-only baseline timeline when each point is current, and
    replays only the action delta selected by the Supervisor.  A missing,
    malformed, stale, partial, or uncovered expected point falls back to a
    policy-local authorization baseline.  It has no owner, command, or
    execution fields and therefore cannot grant control authority.
    """

    if not isinstance(payload, dict) or not isinstance(payload.get("slots"), list):
        raise CanonicalRuntimeError("canonical_projection_payload_invalid")
    capacity = _number(
        usable_capacity_kwh,
        code="battery_capacity_invalid",
        minimum=0.001,
        maximum=10_000.0,
    )
    freshness_reference = (
        _utc(frame.now, code="active_frame_time_invalid")
        if freshness_now is None
        else _utc(
            freshness_now,
            code="canonical_freshness_time_invalid",
        )
    )
    normalized = _validated_timelines(
        timelines,
        now=freshness_reference,
        required=canonical_timeline_dependencies(frame),
    )
    normalized_expected = _validated_expected_timeline(
        expected_timeline,
        now=freshness_reference,
        config_entry_id=normalized["tariff"].get("config_entry_id"),
    )
    expected_model = _expected_self_use_parameters(normalized_expected)
    selected_powers = {
        policy_id: _stored_power_by_point(source, capacity_kwh=capacity)
        for policy_id, source in normalized.items()
    }
    baseline_powers = {
        policy_id: _stored_power_by_point(
            source,
            capacity_kwh=capacity,
            soc_field="baseline_soc_percent",
        )
        for policy_id, source in normalized.items()
    }
    expected_cursor = capacity * _number(
        payload.get("initial_soc_percent"),
        code="canonical_projection_initial_soc_invalid",
        minimum=0.0,
        maximum=100.0,
    ) / 100.0
    export_state = frame.context.export_state
    if type(export_state) is not ExportState:
        raise CanonicalRuntimeError("canonical_energy_disposition_unverified")

    projection_quality = "complete"
    expected_min_soc = expected_cursor / capacity * 100.0
    expected_export_total: float | None = 0.0
    expected_curtailment_total: float | None = 0.0
    authorization_export_total = 0.0
    provider_slot_count = 0
    for slot in payload["slots"]:
        if not isinstance(slot, dict):
            raise CanonicalRuntimeError("canonical_projection_slot_invalid")
        try:
            start = parse_utc_iso(slot["starts_at"], name="slot.starts_at")
            end = parse_utc_iso(slot["ends_at"], name="slot.ends_at")
        except (KeyError, TypeError, ValueError) as err:
            raise CanonicalRuntimeError("canonical_projection_slot_invalid") from err
        if start is None or end is None or end <= start:
            raise CanonicalRuntimeError("canonical_projection_slot_invalid")
        duration_hours = (end - start).total_seconds() / 3600.0

        provider_point = (
            _provider_expected_point(
                normalized_expected,
                start=start,
                end=end,
            )
            if expected_model is not None
            else None
        )
        if provider_point is not None:
            (
                expected_point,
                expected_source,
                provider_partial_bounds,
            ) = provider_point
            (
                neutral_reserve_percent,
                expected_maximum_percent,
                charge_efficiency,
                discharge_efficiency,
                maximum_charge_power,
                maximum_discharge_power,
                system_ac_power,
            ) = expected_model
            (
                base_delta,
                potential_pv,
                expected_load,
                pv_charge_input,
            ) = _neutral_self_use_delta(
                expected_point,
                stored_energy_kwh=expected_cursor,
                capacity_kwh=capacity,
                duration_hours=duration_hours,
                reserve_soc_percent=neutral_reserve_percent,
                maximum_soc_percent=expected_maximum_percent,
                charge_efficiency=charge_efficiency,
                discharge_efficiency=discharge_efficiency,
                maximum_charge_power_kw=maximum_charge_power,
                maximum_discharge_power_kw=maximum_discharge_power,
                system_ac_power_kw=system_ac_power,
            )
            provider_slot_count += 1
            if provider_partial_bounds:
                projection_quality = "partial"
        else:
            # Observation failure never removes the current authorization
            # ledger.  Prefer the RCE baseline where it genuinely covers the
            # slot, otherwise use the continuous tariff authorization
            # backbone and label the provenance explicitly.
            expected_covered = _covering_point(normalized["rce"], start, end)
            expected_policy = "rce"
            if expected_covered is None:
                expected_covered = _covering_point(
                    normalized["tariff"], start, end
                )
                expected_policy = "tariff"
            if expected_covered is None:
                raise CanonicalRuntimeError("canonical_expected_coverage_gap")
            expected_index, expected_point = expected_covered
            base_delta = (
                baseline_powers[expected_policy][expected_index]
                * duration_hours
            )
            expected_source = (
                "rce_calibrated_fallback"
                if expected_policy == "rce"
                else "tariff_conservative_fallback"
            )
            expected_maximum_percent = 100.0
            maximum_charge_power = None
            system_ac_power = None
            projection_quality = "partial"
        protected = slot.get("protected_reserve")
        if not isinstance(protected, Mapping):
            raise CanonicalRuntimeError("protected_reserve_missing")
        protected_percent = _number(
            protected.get("percent"),
            code="protected_reserve_missing",
            minimum=0.0,
            maximum=100.0,
        )
        bounded_base_delta = _bounded_projection_delta(
            stored_energy_kwh=expected_cursor,
            desired_delta_kwh=base_delta,
            capacity_kwh=capacity,
            protected_reserve_percent=(
                neutral_reserve_percent
                if provider_point is not None
                else protected_percent
            ),
            maximum_soc_percent=expected_maximum_percent,
        )
        neutral_cursor = expected_cursor + bounded_base_delta

        if provider_point is None:
            potential_pv = _number(
                expected_point.get("pv_kw"),
                code="canonical_expected_pv_missing",
                minimum=0.0,
            ) * duration_hours
            expected_load = _number(
                expected_point.get("load_kw"),
                code="canonical_expected_load_missing",
                minimum=0.0,
            ) * duration_hours
            surplus = max(potential_pv - expected_load, 0.0)
            inferred_efficiency = 0.95
            if surplus > ENERGY_EPSILON_KWH and base_delta > ENERGY_EPSILON_KWH:
                inferred_efficiency = min(max(base_delta / surplus, 0.01), 1.0)
            pv_charge_input = min(
                surplus,
                max(bounded_base_delta, 0.0) / inferred_efficiency,
            )

        selected_policy = slot.get("selected_policy")
        selected_action = slot.get("selected_action")
        action_delta = 0.0
        selected_point: Mapping[str, Any] | None = None
        selected_scale = 0.0
        direct_action_export_kwh: float | None = None
        rcm_expected_export_kwh: float | None = None
        rcm_expected_curtailment_kwh: float | None = None
        if selected_action != "none":
            if selected_policy not in POLICY_ORDER:
                raise CanonicalRuntimeError("canonical_projection_policy_invalid")
            selected_covered = _covering_point(
                normalized[selected_policy], start, end
            )
            if selected_covered is None:
                raise CanonicalRuntimeError("canonical_expected_action_coverage_gap")
            selected_index, selected_point = selected_covered
            action_delta = (
                selected_powers[selected_policy][selected_index]
                - baseline_powers[selected_policy][selected_index]
            ) * duration_hours
            point_start, point_end = _point_times(selected_point)
            selected_scale = duration_hours / (
                (point_end - point_start).total_seconds() / 3600.0
            )
            if (
                provider_point is None
                and selected_action in {"rce_export", "rcm_pre_discharge"}
            ):
                # A discharge action cannot create stored energy.  The
                # selected optimizer trace may rise relative to its saturated
                # baseline because the action first created PV headroom; raw
                # PV was already applied by the neutral cursor simulation.
                action_delta = min(action_delta, 0.0)
            elif provider_point is None and selected_action == "rcm_absorb_pv":
                action_delta = max(action_delta, 0.0)

        if selected_action == "pv_charge_hold":
            # Holding PV charge applies to the entire expected surplus, not
            # just the smaller amount in the conservative authorization trace.
            # Keep household deficit consumption truthful if PV falls short.
            action_delta = 0.0
            if export_state is ExportState.VERIFIED_ALLOWED:
                bounded_base_delta = min(bounded_base_delta, 0.0)
                neutral_cursor = expected_cursor + bounded_base_delta
                pv_charge_input = 0.0

        projection_floor_percent = protected_percent
        action_maximum_percent = expected_maximum_percent
        if selected_action == "rcm_pre_discharge":
            if selected_point is None:
                raise CanonicalRuntimeError(
                    "canonical_expected_rcm_action_missing"
                )
            projection_floor_percent = max(
                projection_floor_percent,
                _number(
                    selected_point.get("target_soc_percent"),
                    code="canonical_expected_rcm_target_missing",
                    minimum=0.0,
                    maximum=100.0,
                ),
            )
        elif selected_action in {
            "tariff_battery_charge",
            "tariff_grid_support",
            "tariff_grid_support_and_charge",
        }:
            if selected_point is None:
                raise CanonicalRuntimeError(
                    "canonical_expected_tariff_action_missing"
                )
            policy = selected_point.get("policy")
            if not isinstance(policy, Mapping):
                raise CanonicalRuntimeError(
                    "canonical_expected_tariff_policy_missing"
                )
            target_percent = _number(
                selected_point.get("target_soc_percent"),
                code="canonical_expected_tariff_target_missing",
                minimum=0.0,
                maximum=100.0,
            )
            planned_direct_load = (
                _number(
                    policy.get("direct_load_kwh"),
                    code="canonical_expected_tariff_support_missing",
                    minimum=0.0,
                )
                * selected_scale
                if selected_action in {
                    "tariff_grid_support",
                    "tariff_grid_support_and_charge",
                }
                else 0.0
            )
            if provider_point is not None and planned_direct_load > 0.0:
                # Grid support serves an explicit AC share of the household
                # deficit before the battery.  Re-running Self-Use with that
                # smaller deficit preserves the discharge-efficiency, power,
                # reserve, and partial-slot semantics exactly; adding energy
                # after a floor-limited discharge would manufacture SOC.
                supported_load = min(
                    planned_direct_load,
                    max(expected_load - potential_pv, 0.0),
                )
                supported_point = dict(expected_point)
                supported_point["load_kw"] = max(
                    expected_load - supported_load,
                    0.0,
                ) / duration_hours
                (
                    base_delta,
                    _supported_pv,
                    _supported_load,
                    pv_charge_input,
                ) = _neutral_self_use_delta(
                    supported_point,
                    stored_energy_kwh=expected_cursor,
                    capacity_kwh=capacity,
                    duration_hours=duration_hours,
                    reserve_soc_percent=neutral_reserve_percent,
                    maximum_soc_percent=expected_maximum_percent,
                    charge_efficiency=charge_efficiency,
                    discharge_efficiency=discharge_efficiency,
                    maximum_charge_power_kw=maximum_charge_power,
                    maximum_discharge_power_kw=maximum_discharge_power,
                    system_ac_power_kw=system_ac_power,
                )
                bounded_base_delta = base_delta
                neutral_cursor = expected_cursor + bounded_base_delta
                support_delta = 0.0
            else:
                support_efficiency = (
                    expected_model[3] if expected_model is not None else 1.0
                )
                support_delta = min(
                    max(-bounded_base_delta, 0.0),
                    planned_direct_load / support_efficiency,
                )
            cursor_after_support = neutral_cursor + support_delta
            stored_charge = (
                _number(
                    policy.get("stored_energy_kwh"),
                    code="canonical_expected_tariff_charge_missing",
                    minimum=0.0,
                )
                * selected_scale
                if selected_action in {
                    "tariff_battery_charge",
                    "tariff_grid_support_and_charge",
                }
                else 0.0
            )
            if maximum_charge_power is not None:
                available_charge_power = max(
                    maximum_charge_power * duration_hours
                    - max(bounded_base_delta, 0.0),
                    0.0,
                )
                stored_charge = min(stored_charge, available_charge_power)
            charge_to_target = max(
                capacity * target_percent / 100.0 - cursor_after_support,
                0.0,
            )
            action_delta = support_delta + min(stored_charge, charge_to_target)
        action_applied_before_neutral = False
        if (
            provider_point is not None
            and selected_action == "rce_export"
            and export_state is ExportState.VERIFIED_ALLOWED
        ):
            if selected_point is None:
                raise CanonicalRuntimeError(
                    "canonical_expected_discharge_action_missing"
                )
            policy = selected_point.get("policy")
            if not isinstance(policy, Mapping):
                raise CanonicalRuntimeError(
                    "canonical_expected_discharge_policy_missing"
                )
            planned_export = _number(
                policy.get("planned_export_kwh"),
                code="canonical_expected_rce_export_missing",
                minimum=0.0,
            ) * selected_scale
            requested_withdrawal = _number(
                policy.get("planned_battery_withdrawal_kwh"),
                code="canonical_expected_rce_withdrawal_missing",
                minimum=0.0,
            ) * selected_scale
            natural_withdrawal = max(-bounded_base_delta, 0.0)
            surplus_slot = potential_pv >= expected_load
            available_discharge = max(
                maximum_discharge_power * duration_hours
                - (0.0 if surplus_slot else natural_withdrawal),
                0.0,
            )
            action_delta = -min(requested_withdrawal, available_discharge)
            if surplus_slot:
                # RCE/RCEm exports first and the same-slot raw PV can then
                # refill the newly-created headroom.  Applying the neutral
                # precomputed delta first would either lose or duplicate that
                # PV depending on the optimizer's independent SOC cursor.
                applied_action_delta = _bounded_projection_delta(
                    stored_energy_kwh=expected_cursor,
                    desired_delta_kwh=action_delta,
                    capacity_kwh=capacity,
                    protected_reserve_percent=projection_floor_percent,
                    maximum_soc_percent=action_maximum_percent,
                )
                action_cursor = expected_cursor + applied_action_delta
                expected_min_soc = min(
                    expected_min_soc,
                    action_cursor / capacity * 100.0,
                )
                direct_action_export_kwh = planned_export * (
                    min(
                        max(applied_action_delta / action_delta, 0.0),
                        1.0,
                    )
                    if action_delta < -ENERGY_EPSILON_KWH
                    else 0.0
                )
                (
                    base_delta,
                    potential_pv,
                    expected_load,
                    pv_charge_input,
                ) = _neutral_self_use_delta(
                    expected_point,
                    stored_energy_kwh=action_cursor,
                    capacity_kwh=capacity,
                    duration_hours=duration_hours,
                    reserve_soc_percent=neutral_reserve_percent,
                    maximum_soc_percent=expected_maximum_percent,
                    charge_efficiency=charge_efficiency,
                    discharge_efficiency=discharge_efficiency,
                    maximum_charge_power_kw=maximum_charge_power,
                    maximum_discharge_power_kw=maximum_discharge_power,
                    system_ac_power_kw=system_ac_power,
                    controlled_export_kwh=direct_action_export_kwh,
                )
                bounded_base_delta = base_delta
                neutral_cursor = action_cursor + bounded_base_delta
                action_applied_before_neutral = True
            if not action_applied_before_neutral:
                applied_for_export = _bounded_projection_delta(
                    stored_energy_kwh=neutral_cursor,
                    desired_delta_kwh=action_delta,
                    capacity_kwh=capacity,
                    protected_reserve_percent=projection_floor_percent,
                    maximum_soc_percent=action_maximum_percent,
                )
                direct_action_export_kwh = planned_export * (
                    min(max(applied_for_export / action_delta, 0.0), 1.0)
                    if action_delta < -ENERGY_EPSILON_KWH
                    else 0.0
                )
        elif (
            provider_point is not None
            and selected_action in {"rcm_absorb_pv", "rcm_limit_export"}
        ):
            if selected_point is None:
                raise CanonicalRuntimeError(
                    "canonical_expected_rcm_action_missing"
                )
            policy = selected_point.get("policy")
            if not isinstance(policy, Mapping):
                raise CanonicalRuntimeError(
                    "canonical_expected_rcm_policy_missing"
                )
            (
                before_seconds,
                action_seconds,
                after_seconds,
                _action_window_seconds,
            ) = _rcm_action_segment_seconds(
                selected_point,
                policy,
                start=start,
                end=end,
            )
            recommended_charge_power: float | None = None
            if selected_action == "rcm_absorb_pv":
                recommended_charge_power = _number(
                    policy.get("recommended_charge_power_kw"),
                    code="canonical_expected_rcm_charge_limit_missing",
                    minimum=0.0,
                    maximum=1_000.0,
                )
            export_limit_power: float | None = None
            if selected_action == "rcm_limit_export":
                export_limit_power = _number(
                    policy.get("planned_export_limit_kw"),
                    code="canonical_expected_rcm_export_limit_missing",
                    minimum=0.0,
                    maximum=1_000.0,
                )

            cursor = expected_cursor
            base_delta = 0.0
            pv_charge_input = 0.0
            outside_residual = 0.0
            active_residual = 0.0
            net_power = potential_pv / duration_hours - expected_load / duration_hours
            for segment, segment_seconds in (
                ("before", before_seconds),
                ("active", action_seconds),
                ("after", after_seconds),
            ):
                if segment_seconds <= 0.0:
                    continue
                segment_hours = segment_seconds / 3600.0
                segment_charge_limit = maximum_charge_power
                if segment == "active" and recommended_charge_power is not None:
                    # The RCEm recommendation is the AC charge-input cap used
                    # by its model; the neutral helper accepts a stored/DC cap.
                    segment_charge_limit = min(
                        segment_charge_limit,
                        recommended_charge_power * charge_efficiency,
                    )
                segment_delta, _, _, segment_pv_charge = _neutral_self_use_delta(
                    expected_point,
                    stored_energy_kwh=cursor,
                    capacity_kwh=capacity,
                    duration_hours=segment_hours,
                    reserve_soc_percent=neutral_reserve_percent,
                    maximum_soc_percent=expected_maximum_percent,
                    charge_efficiency=charge_efficiency,
                    discharge_efficiency=discharge_efficiency,
                    maximum_charge_power_kw=segment_charge_limit,
                    maximum_discharge_power_kw=maximum_discharge_power,
                    system_ac_power_kw=system_ac_power,
                )
                cursor += segment_delta
                base_delta += segment_delta
                pv_charge_input += segment_pv_charge
                segment_residual = max(
                    max(net_power, 0.0) * segment_hours
                    - segment_pv_charge,
                    0.0,
                )
                if segment == "active":
                    active_residual += segment_residual
                else:
                    outside_residual += segment_residual
            bounded_base_delta = base_delta
            neutral_cursor = cursor
            action_delta = 0.0
            action_applied_before_neutral = True
            if export_limit_power is not None:
                allowed_active_export = min(
                    active_residual,
                    export_limit_power * action_seconds / 3600.0,
                )
                rcm_expected_export_kwh = (
                    outside_residual + allowed_active_export
                )
                rcm_expected_curtailment_kwh = max(
                    active_residual - allowed_active_export,
                    0.0,
                )
        elif (
            provider_point is not None
            and selected_action == "rcm_pre_discharge"
            and export_state is ExportState.VERIFIED_ALLOWED
        ):
            if selected_point is None:
                raise CanonicalRuntimeError(
                    "canonical_expected_rcm_action_missing"
                )
            policy = selected_point.get("policy")
            if not isinstance(policy, Mapping):
                raise CanonicalRuntimeError(
                    "canonical_expected_rcm_policy_missing"
                )
            (
                before_seconds,
                action_seconds,
                after_seconds,
                action_window_seconds,
            ) = _rcm_action_segment_seconds(
                selected_point,
                policy,
                start=start,
                end=end,
            )
            cursor = expected_cursor
            pv_charge_input = 0.0
            base_delta = 0.0
            if before_seconds > 0.0:
                before_delta, _, _, before_pv_charge = _neutral_self_use_delta(
                    expected_point,
                    stored_energy_kwh=cursor,
                    capacity_kwh=capacity,
                    duration_hours=before_seconds / 3600.0,
                    reserve_soc_percent=neutral_reserve_percent,
                    maximum_soc_percent=expected_maximum_percent,
                    charge_efficiency=charge_efficiency,
                    discharge_efficiency=discharge_efficiency,
                    maximum_charge_power_kw=maximum_charge_power,
                    maximum_discharge_power_kw=maximum_discharge_power,
                    system_ac_power_kw=system_ac_power,
                )
                cursor += before_delta
                base_delta += before_delta
                pv_charge_input += before_pv_charge
            planned_stored = _number(
                policy.get("planned_pre_discharge_stored_kwh"),
                code="canonical_expected_rcm_withdrawal_missing",
                minimum=0.0,
            )
            planned_ac = _number(
                policy.get("planned_pre_discharge_kwh"),
                code="canonical_expected_rcm_export_missing",
                minimum=0.0,
            )
            action_fraction = action_seconds / action_window_seconds
            requested_withdrawal = min(
                planned_stored * action_fraction,
                maximum_discharge_power * action_seconds / 3600.0,
            )
            action_delta = -requested_withdrawal
            applied_action_delta = _bounded_projection_delta(
                stored_energy_kwh=cursor,
                desired_delta_kwh=action_delta,
                capacity_kwh=capacity,
                protected_reserve_percent=projection_floor_percent,
                maximum_soc_percent=action_maximum_percent,
            )
            cursor += applied_action_delta
            expected_min_soc = min(
                expected_min_soc,
                cursor / capacity * 100.0,
            )
            applied_ac = planned_ac * action_fraction * (
                min(max(applied_action_delta / action_delta, 0.0), 1.0)
                if action_delta < -ENERGY_EPSILON_KWH
                else 0.0
            )
            if after_seconds > 0.0:
                after_delta, _, _, after_pv_charge = _neutral_self_use_delta(
                    expected_point,
                    stored_energy_kwh=cursor,
                    capacity_kwh=capacity,
                    duration_hours=after_seconds / 3600.0,
                    reserve_soc_percent=neutral_reserve_percent,
                    maximum_soc_percent=expected_maximum_percent,
                    charge_efficiency=charge_efficiency,
                    discharge_efficiency=discharge_efficiency,
                    maximum_charge_power_kw=maximum_charge_power,
                    maximum_discharge_power_kw=maximum_discharge_power,
                    system_ac_power_kw=system_ac_power,
                )
                cursor += after_delta
                base_delta += after_delta
                pv_charge_input += after_pv_charge
            bounded_base_delta = base_delta
            neutral_cursor = cursor
            outside_hours = (before_seconds + after_seconds) / 3600.0
            outside_surplus = max(
                (
                    potential_pv / duration_hours
                    - expected_load / duration_hours
                )
                * outside_hours,
                0.0,
            )
            outside_export = max(outside_surplus - pv_charge_input, 0.0)
            action_net = (
                potential_pv / duration_hours
                - expected_load / duration_hours
            ) * (action_seconds / 3600.0)
            rcm_expected_export_kwh = (
                outside_export + max(action_net + applied_ac, 0.0)
            )
            action_applied_before_neutral = True
        elif (
            provider_point is not None
            and selected_action in {"rce_export", "rcm_pre_discharge"}
        ):
            # A selected export cannot be replayed without verified export
            # permission.  Keep the raw neutral observation, but never turn
            # it into display-only execution authority.
            action_delta = 0.0
        if not action_applied_before_neutral:
            applied_action_delta = _bounded_projection_delta(
                stored_energy_kwh=neutral_cursor,
                desired_delta_kwh=action_delta,
                capacity_kwh=capacity,
                protected_reserve_percent=projection_floor_percent,
                maximum_soc_percent=action_maximum_percent,
            )
        expected_start_soc = expected_cursor / capacity * 100.0
        expected_cursor = (
            neutral_cursor
            if action_applied_before_neutral
            else min(
                capacity,
                max(0.0, neutral_cursor + applied_action_delta),
            )
        )
        expected_end_soc = expected_cursor / capacity * 100.0
        expected_min_soc = min(expected_min_soc, expected_end_soc)

        bridge_natural_export: float | None = None
        bridge_curtailment: float | None = None
        if provider_point is not None:
            # LOAD, controlled battery export, PV charging, and residual PV
            # export all share the same installation-wide AC bridge.  The
            # controlled share is the export actually possible from the
            # expected cursor after reserve/power clamps, never the raw plan.
            system_energy_kwh = system_ac_power * duration_hours
            pv_to_load_kwh = min(
                potential_pv,
                expected_load,
                system_energy_kwh,
            )
            residual_surplus = max(
                potential_pv - pv_to_load_kwh - pv_charge_input,
                0.0,
            )
            controlled_export_kwh = direct_action_export_kwh or 0.0
            natural_export_headroom_kwh = max(
                system_energy_kwh
                - pv_to_load_kwh
                - pv_charge_input
                - controlled_export_kwh,
                0.0,
            )
            bridge_natural_export = min(
                residual_surplus,
                natural_export_headroom_kwh,
            )
            bridge_curtailment = max(
                residual_surplus - bridge_natural_export,
                0.0,
            )
        else:
            surplus = max(potential_pv - expected_load, 0.0)
            residual_surplus = max(surplus - pv_charge_input, 0.0)

        expected_export: float | None
        usable_pv: float | None
        curtailed_pv: float | None
        if export_state is ExportState.VERIFIED_ALLOWED:
            if rcm_expected_curtailment_kwh is not None:
                curtailed_pv = rcm_expected_curtailment_kwh
            elif bridge_curtailment is not None:
                curtailed_pv = bridge_curtailment
            else:
                curtailed_pv = 0.0
            usable_pv = potential_pv - curtailed_pv
            if rcm_expected_export_kwh is not None:
                expected_export = rcm_expected_export_kwh
            elif bridge_natural_export is not None:
                expected_export = bridge_natural_export
            else:
                expected_export = residual_surplus
        elif export_state in {
            ExportState.CONFIRMED_ZERO_EXPORT,
            ExportState.PROHIBITED,
        }:
            usable_pv = potential_pv - residual_surplus
            curtailed_pv = residual_surplus
            expected_export = 0.0
        elif (
            bridge_natural_export is not None
            and bridge_natural_export <= ENERGY_EPSILON_KWH
        ):
            usable_pv = potential_pv - residual_surplus
            curtailed_pv = residual_surplus
            expected_export = 0.0
        elif residual_surplus > ENERGY_EPSILON_KWH:
            usable_pv = None
            curtailed_pv = None
            expected_export = None
            projection_quality = "unverified_export_disposition"
        else:
            usable_pv = potential_pv
            curtailed_pv = 0.0
            expected_export = 0.0

        if direct_action_export_kwh is not None:
            if expected_export is not None:
                expected_export += direct_action_export_kwh
        elif selected_point is not None and selected_policy == "rce":
            policy = selected_point.get("policy")
            if not isinstance(policy, Mapping):
                raise CanonicalRuntimeError("canonical_expected_rce_policy_missing")
            planned_export = _number(
                policy.get("planned_export_kwh"),
                code="canonical_expected_rce_export_missing",
                minimum=0.0,
            ) * selected_scale
            action_ratio = (
                min(
                    max(applied_action_delta / action_delta, 0.0),
                    1.0,
                )
                if action_delta < -ENERGY_EPSILON_KWH
                else 0.0
            )
            if expected_export is not None:
                expected_export += planned_export * action_ratio

        equation = slot.get("soc_equation")
        planned = slot.get("planned")
        if not isinstance(equation, dict) or not isinstance(planned, dict):
            raise CanonicalRuntimeError("canonical_projection_slot_invalid")
        authorization_start = _number(
            equation.get("soc_start_percent"),
            code="canonical_projection_authorization_soc_invalid",
            minimum=0.0,
            maximum=100.0,
        )
        authorization_end = _number(
            equation.get("soc_end_percent"),
            code="canonical_projection_authorization_soc_invalid",
            minimum=0.0,
            maximum=100.0,
        )
        equation.update(
            {
                "authorization_soc_start_percent": authorization_start,
                "authorization_soc_end_percent": authorization_end,
                "expected_soc_start_percent": expected_start_soc,
                "expected_soc_end_percent": expected_end_soc,
                "expected_source": expected_source,
            }
        )
        planned.update(
            {
                "potential_pv_kwh": potential_pv,
                "usable_pv_kwh": usable_pv,
                "pv_curtailed_kwh": curtailed_pv,
                "expected_load_kwh": expected_load,
                "expected_grid_export_kwh": expected_export,
                "authorization_grid_export_kwh": max(
                    -_number(
                        planned.get("grid_kwh_import_positive"),
                        code="canonical_projection_grid_invalid",
                    ),
                    0.0,
                ),
            }
        )
        authorization_export_total += planned["authorization_grid_export_kwh"]
        if expected_export_total is not None:
            expected_export_total = (
                expected_export_total + expected_export
                if expected_export is not None
                else None
            )
        if expected_curtailment_total is not None:
            expected_curtailment_total = (
                expected_curtailment_total + curtailed_pv
                if curtailed_pv is not None
                else None
            )

    audit = payload.get("audit")
    if not isinstance(audit, Mapping):
        raise CanonicalRuntimeError("canonical_projection_audit_missing")
    slot_count = len(payload["slots"])
    expected_projection = (
        "provider_p50_observation_only"
        if slot_count > 0 and provider_slot_count == slot_count
        else "mixed_observation_only"
        if provider_slot_count > 0
        else "authorization_fallback_observation_only"
    )
    payload.update(
        {
            "projection_contract": "dual_soc_v1",
            "authorization_projection": "conservative_fail_closed",
            "expected_projection": expected_projection,
            "expected_projection_quality": projection_quality,
            "authorization_final_soc_percent": payload.get("final_soc_percent"),
            "expected_final_soc_percent": expected_cursor / capacity * 100.0,
            "expected_min_soc_percent": expected_min_soc,
            "expected_grid_export_kwh": expected_export_total,
            "expected_pv_curtailed_kwh": expected_curtailment_total,
            "authorization_grid_export_kwh": authorization_export_total,
            "authorization_reserve_violation_count": audit.get(
                "reserve_violation_count"
            ),
        }
    )
    return payload


def _template_candidates(frame: Any) -> dict[str, ArbiterPolicyCandidate]:
    result = {candidate.policy_id.value: candidate for candidate in frame.candidates}
    if set(result) != set(POLICY_ORDER):
        raise CanonicalRuntimeError("runtime_candidate_set_incomplete")
    return result


def _current_rcm_is_idle(
    frame: Any, template: ArbiterPolicyCandidate, *, now: datetime,
) -> bool:
    """Attest the live RCEm state when its forecast starts only tomorrow.

    Reuse the runtime's source/freshness contract at publication time. This
    resolves only the current display conflict; missing forecast energy stays
    unavailable and cannot become an energy backbone or execution authority.
    """
    source = getattr(frame, "rcm", None)
    if source is None:
        return False
    fresh = build_rcm_candidate(source, now=now)
    return all(
        candidate.available is True
        and candidate.result_current is True
        and candidate.recalculation_pending is False
        and candidate.requested_action is ArbiterRequestedAction.NONE
        and candidate.reason_code is ReasonCode.NO_ACTION
        and candidate.blocked_reason is None
        and candidate.active_latched is False
        and candidate.local_hard_stop is False
        for candidate in (template, fresh)
    )


def _action_for_point(policy_id: str, point: Mapping[str, Any]) -> ArbiterRequestedAction:
    if point.get("selected") is not True:
        return ArbiterRequestedAction.NONE
    action_code = point.get("action_code")
    mapping = {
        ("rce", "export"): ArbiterRequestedAction.RCE_EXPORT,
        ("rce", "pv_charge_hold"): ArbiterRequestedAction.PV_CHARGE_HOLD,
        ("tariff", "battery_charge"): ArbiterRequestedAction.TARIFF_BATTERY_CHARGE,
        ("tariff", "grid_support"): ArbiterRequestedAction.TARIFF_GRID_SUPPORT,
        ("tariff", "grid_support_and_charge"): (
            ArbiterRequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE
        ),
        ("rcm", "absorb_pv"): ArbiterRequestedAction.RCM_ABSORB_PV,
        ("rcm", "limit_export"): ArbiterRequestedAction.RCM_LIMIT_EXPORT,
        ("rcm", "grid_discharge_preparation"): (
            ArbiterRequestedAction.RCM_PRE_DISCHARGE
        ),
    }
    return mapping.get((policy_id, action_code), ArbiterRequestedAction.NONE)


def _action_shape(
    policy_id: str,
    action: ArbiterRequestedAction,
    point: Mapping[str, Any],
    template: ArbiterPolicyCandidate,
    *,
    current_interval: bool,
) -> tuple[PriorityClass, NeedClass, ReasonCode, ActuatorScope, bool]:
    if action is ArbiterRequestedAction.NONE:
        return (
            PriorityClass.NONE,
            NeedClass.NONE,
            ReasonCode.NO_ACTION,
            ActuatorScope.NONE,
            True,
        )
    if policy_id == "rce":
        return (
            PriorityClass.ECONOMIC,
            NeedClass.OPTIONAL,
            ReasonCode.ECONOMIC_CANDIDATE,
            ActuatorScope.EMS_BLOCK_4300_4306,
            True,
        )
    if policy_id == "tariff":
        policy = point.get("policy")
        need_code = policy.get("need_class") if isinstance(policy, Mapping) else None
        required = need_code in {"required_energy", "mixed"}
        economic = need_code in {"economic", "mixed"}
        shape_valid = bool(
            (required or economic)
            and not (
                required
                and action is ArbiterRequestedAction.TARIFF_GRID_SUPPORT
            )
        )
        return (
            PriorityClass.REQUIRED_ENERGY if required else PriorityClass.ECONOMIC,
            NeedClass.MANDATORY if required else NeedClass.OPTIONAL,
            ReasonCode.REQUIRED_ENERGY_RESTORE if required else ReasonCode.ECONOMIC_CANDIDATE,
            ActuatorScope.EMS_BLOCK_4300_4306,
            shape_valid,
        )
    if current_interval and template.requested_action is action:
        priority = template.priority_class
        need = template.need_class
        reason = template.reason_code
    else:
        priority = PriorityClass.PREVENTIVE_GRID
        need = NeedClass.PREVENTIVE
        reason = ReasonCode.PREVENTIVE_VOLTAGE_ACTION
    scope = (
        ActuatorScope.DIRECT_306
        if action is ArbiterRequestedAction.RCM_ABSORB_PV
        else ActuatorScope.DIRECT_259
        if action is ArbiterRequestedAction.RCM_LIMIT_EXPORT
        else ActuatorScope.EMS_BLOCK_4300_4306
    )
    return priority, need, reason, scope, True


def _ems_values(block: Any, **changes: float | int) -> tuple[ExpectationValue, ...]:
    values: dict[str, float | int] = {
        "backup_soc": block.backup_soc_percent_4302,
        "force_charge_soc": block.force_charge_soc_percent_4303,
        "force_discharge_soc": block.force_discharge_soc_percent_4305,
        "maximum_charge_power": block.maximum_charge_power_percent_4304,
        "maximum_discharge_power": block.maximum_discharge_power_percent_4306,
        "mode_code": int(block.mode),
        "self_use_soc": block.self_use_soc_percent_4301,
    }
    values.update(changes)
    return tuple(ExpectationValue(name=name, value=value) for name, value in sorted(values.items()))


def _pv_hold_target(
    frame: Any, *, current_interval: bool, projected_soc_percent: float,
) -> float:
    """Preserve a current latch; future preview uses that interval's SOC."""
    if (current_interval and frame.rce.pv_charge_hold
            and frame.rce.latched_minimum_soc_percent is not None):
        return _number(frame.rce.latched_minimum_soc_percent,
            code="pv_delay_soc_target_invalid", minimum=1.0, maximum=100.0)
    if __package__:
        from .pv_charge_delay import hold_soc_target
    else:
        from pv_charge_delay import hold_soc_target
    try:
        return float(hold_soc_target(projected_soc_percent))
    except ValueError as err:
        # No headroom rejects this candidate, not every unrelated plan slot.
        raise CanonicalRuntimeError("pv_delay_soc_headroom_missing") from err


def _command_for_action(
    action: ArbiterRequestedAction,
    point: Mapping[str, Any],
    frame: Any,
    settings: Any,
    *,
    pv_hold_target_percent: float | None = None,
) -> tuple[CommandTarget, int | None, tuple[ExpectationValue, ...]]:
    if action is ArbiterRequestedAction.NONE:
        return CommandTarget.NONE, None, ()
    policy = point.get("policy")
    if not isinstance(policy, Mapping):
        raise CanonicalRuntimeError("candidate_policy_missing")
    block = settings.ems_block
    if action is ArbiterRequestedAction.PV_CHARGE_HOLD:
        target = _number(pv_hold_target_percent,
            code="pv_delay_soc_target_invalid", minimum=1.0, maximum=100.0)
        return (CommandTarget.EMS_BLOCK_4300_4306, settings.ems_generation,
            _ems_values(block, mode_code=5, force_discharge_soc=target,
                        maximum_discharge_power=1.))
    if action is ArbiterRequestedAction.RCE_EXPORT:
        floor = _number(
            point.get("protected_soc_floor_percent"),
            code="rce_command_target_missing",
            minimum=0.0,
            maximum=100.0,
        )
        power = _number(
            policy.get("command_discharge_power_percent"),
            code="rce_command_power_missing",
            minimum=0.000001,
            maximum=100.0,
        )
        return (
            CommandTarget.EMS_BLOCK_4300_4306,
            settings.ems_generation,
            _ems_values(
                block,
                mode_code=5,
                force_discharge_soc=floor,
                maximum_discharge_power=power,
            ),
        )
    if action in {
        ArbiterRequestedAction.TARIFF_BATTERY_CHARGE,
        ArbiterRequestedAction.TARIFF_GRID_SUPPORT,
        ArbiterRequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
    }:
        target = _number(
            point.get("target_soc_percent"),
            code="tariff_command_target_missing",
            minimum=10.0,
            maximum=100.0,
        )
        power = _number(
            frame.tariff.command_charge_power_percent,
            code="tariff_command_power_missing",
            minimum=0.0,
            maximum=100.0,
        )
        if policy.get("tariff_zone") == "pstryk":
            # Pstryk chooses a separate executable command for each interval.
            # The current interval can be idle (0%) while a future BUY is
            # selected. Its projection must retain that future command.
            system = _number(frame.rce.system_power_kw,
                code="tariff_system_power_missing", minimum=0.000001)
            requested = _number(policy.get("planned_charge_kw"),
                code="tariff_command_power_missing", minimum=0.000001)
            raw = requested / system * 100.0
            power = _number(round(raw), code="tariff_command_power_missing",
                minimum=1.0, maximum=100.0)
            if abs(raw-power)>1e-6:
                raise CanonicalRuntimeError("tariff_command_power_unquantized")
        return (
            CommandTarget.EMS_BLOCK_4300_4306,
            settings.ems_generation,
            _ems_values(
                block,
                mode_code=4,
                force_charge_soc=target,
                maximum_charge_power=power,
            ),
        )
    if action is ArbiterRequestedAction.RCM_ABSORB_PV:
        target = _number(
            frame.rcm.recommended_charge_limit_percent,
            code="rcm_charge_limit_missing",
            minimum=10.0,
            maximum=100.0,
        )
        return (
            CommandTarget.BATTERY_CHARGE_LIMIT_306,
            settings.battery_generation,
            (ExpectationValue("battery_charge_limit_percent", target),),
        )
    if action is ArbiterRequestedAction.RCM_LIMIT_EXPORT:
        target = _number(
            policy.get("planned_export_limit_percent"),
            code="rcm_export_limit_missing",
            minimum=0.0,
            maximum=100.0,
        )
        if target > 0.05:
            raise CanonicalRuntimeError("rcm_positive_limit_unverified")
        return (
            CommandTarget.GCF_EXPORT_LIMIT_259,
            settings.gcf_generation,
            (ExpectationValue("export_limit_percent", target),),
        )
    target = _number(
        point.get("target_soc_percent"),
        code="rcm_pre_discharge_target_missing",
        minimum=0.0,
        maximum=100.0,
    )
    power = _number(
        frame.rcm.pre_discharge_power_percent,
        code="rcm_pre_discharge_power_missing",
        minimum=0.000001,
        maximum=100.0,
    )
    return (
        CommandTarget.EMS_BLOCK_4300_4306,
        settings.ems_generation,
        _ems_values(
            block,
            mode_code=5,
            force_discharge_soc=target,
            maximum_discharge_power=power,
        ),
    )


def _candidate_requested_values(
    action: ArbiterRequestedAction,
    point: Mapping[str, Any],
    frame: Any,
    *,
    duration_hours: float,
    pv_hold_target_percent: float | None = None,
) -> tuple[PhysicalMode | None, float | None, float | None, float | None, float | None]:
    policy = point.get("policy")
    if not isinstance(policy, Mapping):
        return None, None, None, None, None
    floor = _optional_number(
        point.get("protected_soc_floor_percent"), minimum=0.0, maximum=100.0
    )
    if action is ArbiterRequestedAction.PV_CHARGE_HOLD:
        return (PhysicalMode.GRID_DISCHARGE, frame.rce.system_power_kw*.01,
                0., None, pv_hold_target_percent)
    if action is ArbiterRequestedAction.RCE_EXPORT:
        return (
            PhysicalMode.GRID_DISCHARGE,
            _optional_number(policy.get("target_discharge_kw"), minimum=0.0),
            _optional_number(policy.get("planned_export_kwh"), minimum=0.0),
            None,
            floor,
        )
    if action in {
        ArbiterRequestedAction.TARIFF_BATTERY_CHARGE,
        ArbiterRequestedAction.TARIFF_GRID_SUPPORT,
        ArbiterRequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE,
    }:
        return (
            PhysicalMode.GRID_CHARGE,
            _optional_number(policy.get("planned_charge_kw")
                if policy.get("tariff_zone") == "pstryk"
                else frame.tariff.requested_charge_power_kw, minimum=0.0),
            _optional_number(policy.get("planned_import_kwh"), minimum=0.0),
            _optional_number(point.get("target_soc_percent"), minimum=0.0, maximum=100.0),
            floor,
        )
    if action is ArbiterRequestedAction.RCM_ABSORB_PV:
        return (
            PhysicalMode.SELF_USE,
            _optional_number(frame.rcm.recommended_charge_power_kw, minimum=0.0),
            None,
            _optional_number(point.get("target_soc_percent"), minimum=0.0, maximum=100.0),
            floor,
        )
    if action is ArbiterRequestedAction.RCM_LIMIT_EXPORT:
        return None, None, None, None, floor
    if action is ArbiterRequestedAction.RCM_PRE_DISCHARGE:
        power = _optional_number(policy.get("planned_pre_discharge_kw"), minimum=0.0)
        return (
            PhysicalMode.GRID_DISCHARGE,
            power,
            power * duration_hours if power is not None else None,
            _optional_number(point.get("target_soc_percent"), minimum=0.0, maximum=100.0),
            floor,
        )
    return None, None, None, None, floor


def _build_candidate(
    *,
    policy_id: str,
    point: Mapping[str, Any],
    timeline: Mapping[str, Any],
    template: ArbiterPolicyCandidate,
    frame: Any,
    settings: Any,
    start: datetime,
    end: datetime,
    current_interval: bool,
    interval_coverage_verified: bool,
    projected_soc_percent: float,
) -> ArbiterPolicyCandidate:
    action = _action_for_point(policy_id, point)
    priority, need, reason, scope, shape_valid = _action_shape(
        policy_id,
        action,
        point,
        template,
        current_interval=current_interval,
    )
    command_valid = True
    duration_hours = (end - start).total_seconds() / 3600.0
    try:
        pv_hold_target = (
            _pv_hold_target(frame, current_interval=current_interval,
                projected_soc_percent=projected_soc_percent)
            if action is ArbiterRequestedAction.PV_CHARGE_HOLD else None
        )
        target, generation, values = _command_for_action(action, point, frame,
            settings, pv_hold_target_percent=pv_hold_target)
        requested_mode, requested_power, requested_energy, target_soc, protected = (
            _candidate_requested_values(action, point, frame,
                duration_hours=duration_hours, pv_hold_target_percent=pv_hold_target)
        )
    except CanonicalRuntimeError:
        target, generation, values = CommandTarget.NONE, None, ()
        requested_mode, requested_power, requested_energy, target_soc, protected = (
            None, None, None, None, None
        )
        command_valid = False
    point_complete = point.get("quality") == "complete"
    available = bool(
        shape_valid
        and command_valid
        and point_complete
        and interval_coverage_verified
    )
    if action is ArbiterRequestedAction.NONE:
        available = bool(
            point.get("quality") in {"complete", "partial"}
            and interval_coverage_verified
        )
    fingerprint = None
    if action is not ArbiterRequestedAction.NONE:
        fingerprint = _sha(
            {
                "policy": policy_id,
                "action": action.value,
                "slot_start": start.isoformat(),
                "slot_end": end.isoformat(),
                "target": target.value,
                "source_generation": generation,
                "values": {item.name: item.value for item in values},
            }
        )
    revision_digest = _sha(
        {
            "input_revision": timeline["input_revision"],
            "plan_revision": timeline["plan_revision"],
            "policy": policy_id,
            "action": action.value,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "fingerprint": fingerprint,
        }
    )
    candidate = ArbiterPolicyCandidate(
        schema_version=SUPERVISOR_SCHEMA_VERSION,
        policy_id=ArbiterPolicyId(policy_id),
        observed_at=start,
        allowed_by_user=template.allowed_by_user,
        enabled=template.enabled,
        available=available,
        result_current=True,
        recalculation_pending=False,
        input_revision=int(timeline["input_revision"]),
        candidate_revision=int(revision_digest[:16], 16),
        start_eligible=bool(action is not ArbiterRequestedAction.NONE and available),
        continuation_eligible=False,
        active_latched=False,
        local_hard_stop=False,
        requested_action=action,
        actuator_scope=scope,
        priority_class=priority,
        need_class=need,
        reason_code=reason,
        blocked_reason=(
            None
            if available
            else ReasonCode.ACTUATOR_UNAVAILABLE
            if action is not ArbiterRequestedAction.NONE
            else None
        ),
        valid_from=start,
        valid_until=end,
        desired_actuator_fingerprint=fingerprint,
        economic_value_status=EconomicValueStatus.UNAVAILABLE,
        requested_mode=requested_mode,
        requested_power_kw=requested_power,
        requested_energy_kwh=requested_energy,
        target_soc_percent=target_soc,
        protected_soc_floor_percent=protected,
        economic_contract_id=None,
        economic_basis_fingerprint=None,
        expected_marginal_net_benefit_pln=None,
        urgency=None,
        severity=None,
    )
    # Policy energy belongs to the native optimizer interval, not each smaller
    # segment on the shared canonical grid (nor the time remaining until end).
    native_seconds = 0.0
    if available and requested_energy is not None:
        native_start, native_end = _point_times(point)
        native_seconds = (native_end - native_start).total_seconds()
    minimum_power = planned_grid_power_kw(requested_energy, native_seconds)
    if action in {ArbiterRequestedAction.TARIFF_BATTERY_CHARGE,
                  ArbiterRequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE}:
        minimum_power = planned_battery_grid_power_kw(
            requested_power, point.get("policy", {}).get("direct_load_kwh"), native_seconds)
    return apply_minimum_grid_power(candidate, minimum_power)


def _planning_context(frame: Any, *, observed_at: datetime) -> ExecutionContext:
    external_owner = frame.context.owner_kind in {
        ArbiterOwnerKind.MANUAL,
        ArbiterOwnerKind.BALANCING,
        ArbiterOwnerKind.FOREIGN,
        ArbiterOwnerKind.UNKNOWN,
    }
    return replace(
        frame.context,
        observed_at=observed_at,
        owner_kind=(frame.context.owner_kind if external_owner else ArbiterOwnerKind.NONE),
        transaction_pending=False,
        transaction_owner_kind=ArbiterOwnerKind.NONE,
    )


def _ledger_policy(policy: ArbiterPolicyId | None) -> PolicyId:
    return PolicyId.NONE if policy is None else PolicyId(policy.value)


def _ledger_action(action: ArbiterRequestedAction) -> RequestedAction:
    return RequestedAction(action.value)


def _ledger_owner(
    selected: ArbiterPolicyId | None,
    context: ExecutionContext,
) -> OwnerKind:
    if context.owner_kind in {
        ArbiterOwnerKind.MANUAL,
        ArbiterOwnerKind.BALANCING,
        ArbiterOwnerKind.FOREIGN,
        ArbiterOwnerKind.UNKNOWN,
    }:
        return OwnerKind(context.owner_kind.value)
    return OwnerKind.NONE if selected is None else OwnerKind(selected.value)


def _projection_reasons(
    *,
    selected: ArbiterPolicyId | None,
    decision: Any,
    summary: Any,
    raw: ArbiterPolicyCandidate,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if summary.rejection_reason is not None:
        reasons.append(summary.rejection_reason.value)
    if selected is not raw.policy_id:
        if decision.supervisor_mode is SupervisorMode.OFF:
            reasons.append("supervisor_off")
        elif summary.rejection_reason is None:
            reasons.append(
                "lower_priority" if selected is not None else decision.selection_reason.value
            )
    if raw.requested_action is ArbiterRequestedAction.NONE and not reasons:
        reasons.append("no_action")
    return tuple(sorted(set(reasons)))


def _start_verdict(
    *,
    frame_now: datetime,
    start: datetime,
    end: datetime,
    raw: ArbiterPolicyCandidate,
    summary: Any,
    selected: bool,
    decision: Any,
) -> StartEligibility:
    if raw.requested_action is ArbiterRequestedAction.NONE:
        return StartEligibility.BLOCKED
    if not raw.available:
        return StartEligibility.UNVERIFIED
    if summary.rejection_reason is not None:
        return StartEligibility.BLOCKED
    current = start <= frame_now < end
    if not current:
        return StartEligibility.UNVERIFIED
    if selected and decision.execution_blocked_reason is not None:
        return StartEligibility.BLOCKED
    return StartEligibility.ELIGIBLE


def _candidate_projection(
    *,
    frame_now: datetime,
    start: datetime,
    end: datetime,
    raw: ArbiterPolicyCandidate,
    summary: Any,
    decision: Any,
) -> PolicyCandidate:
    selected = decision.selected_policy is raw.policy_id
    reasons = _projection_reasons(
        selected=decision.selected_policy,
        decision=decision,
        summary=summary,
        raw=raw,
    )
    verdict = _start_verdict(
        frame_now=frame_now,
        start=start,
        end=end,
        raw=raw,
        summary=summary,
        selected=selected,
        decision=decision,
    )
    return PolicyCandidate(
        policy_id=PolicyId(raw.policy_id.value),
        requested_action=RequestedAction(raw.requested_action.value),
        eligible=bool(
            raw.requested_action is not ArbiterRequestedAction.NONE
            and raw.available
            and summary.rejection_reason is None
        ),
        start_eligibility=verdict,
        input_revision=raw.input_revision,
        candidate_revision=raw.candidate_revision,
        rejected_reasons=reasons,
    )


def _energy_for_segment(
    point: Mapping[str, Any],
    *,
    stored_power_kw: float,
    duration_hours: float,
) -> tuple[PlannedEnergy, BatteryEnergyFlows]:
    pv = _number(point.get("pv_kw"), code="canonical_pv_missing", minimum=0.0) * duration_hours
    load = _number(point.get("load_kw"), code="canonical_load_missing", minimum=0.0) * duration_hours
    grid = _number(point.get("grid_kw"), code="canonical_grid_missing") * duration_hours
    stored = stored_power_kw * duration_hours
    losses = pv + grid - load - stored
    if losses < -ENERGY_EPSILON_KWH:
        raise CanonicalRuntimeError("canonical_negative_losses")
    losses = max(losses, 0.0)
    # Remove only sub-micro-Wh source rounding so the pure ledger sees an exact
    # system equation while preserving every material timeline value.
    grid = load + stored + losses - pv
    bus_to_battery = stored + losses
    if bus_to_battery >= 0.0:
        pv_surplus = max(pv - load, 0.0)
        pv_to_battery = min(pv_surplus, bus_to_battery)
        grid_to_battery = bus_to_battery - pv_to_battery
        battery_to_load = 0.0
        battery_to_grid = 0.0
    else:
        battery_output = -bus_to_battery
        load_deficit = max(load - pv, 0.0)
        battery_to_load = min(load_deficit, battery_output)
        battery_to_grid = battery_output - battery_to_load
        pv_to_battery = 0.0
        grid_to_battery = 0.0
    return (
        PlannedEnergy(
            pv_kwh=pv,
            load_kwh=load,
            battery_kwh=stored,
            grid_kwh=grid,
        ),
        BatteryEnergyFlows(
            pv_to_battery_kwh=pv_to_battery,
            grid_to_battery_kwh=grid_to_battery,
            battery_to_load_kwh=battery_to_load,
            battery_to_grid_kwh=battery_to_grid,
            losses_kwh=losses,
        ),
    )


def _limit_segment_to_physical_soc(
    planned: PlannedEnergy,
    flows: BatteryEnergyFlows,
    *,
    stored_energy_kwh: float,
    capacity_kwh: float,
    export_state: ExportState,
    grid_import_verified: bool,
    protected_reserve_kwh: float = 0.0,
) -> tuple[PlannedEnergy, BatteryEnergyFlows, float]:
    """Bound one source delta to physical battery energy without inventing power.

    Timeline power is applied only for the canonical interval that really
    remains.  A late start or a switch between independent source traces can
    therefore leave a small SOC offset.  If a later, otherwise valid source
    interval reaches 0 or 100%, scale only the unavailable battery energy and
    its conversion loss, then route the bounded energy through PV before grid
    on charge and load before grid on discharge.  A verified PV surplus is
    exported, an exact no-export surplus is curtailed, and an unverified
    disposition stays closed.  The pure ledger still receives an exact, fully
    balanced physical trajectory.
    """

    desired = planned.battery_kwh
    reserve = min(max(protected_reserve_kwh, 0.0), capacity_kwh)
    # A projection never manufactures energy to repair an already-low SOC.
    # It does, however, stop every further discharge at the greater of the
    # current stored energy and the verified policy/physical reserve.
    minimum_allowed_energy = min(stored_energy_kwh, reserve)
    minimum = minimum_allowed_energy - stored_energy_kwh
    maximum = capacity_kwh - stored_energy_kwh
    bounded = min(max(desired, minimum), maximum)
    scale = bounded / desired if desired != 0.0 else 1.0
    if not math.isfinite(scale) or scale < 0.0 or scale > 1.0:
        raise CanonicalRuntimeError("canonical_soc_cursor_invalid")

    if type(export_state) is not ExportState or type(grid_import_verified) is not bool:
        raise CanonicalRuntimeError("canonical_energy_disposition_unverified")
    limited_loss = flows.losses_kwh * scale

    def routed(
        pv_kwh: float,
    ) -> tuple[PlannedEnergy, BatteryEnergyFlows]:
        grid_kwh = (
            planned.load_kwh + bounded + limited_loss - pv_kwh
        )
        bus_to_battery = bounded + limited_loss
        if bus_to_battery >= 0.0:
            pv_surplus = max(pv_kwh - planned.load_kwh, 0.0)
            pv_to_battery = min(pv_surplus, bus_to_battery)
            grid_to_battery = bus_to_battery - pv_to_battery
            battery_to_load = 0.0
            battery_to_grid = 0.0
        else:
            battery_output = -bus_to_battery
            load_deficit = max(planned.load_kwh - pv_kwh, 0.0)
            battery_to_load = min(load_deficit, battery_output)
            battery_to_grid = battery_output - battery_to_load
            pv_to_battery = 0.0
            grid_to_battery = 0.0
        return (
            PlannedEnergy(
                pv_kwh=pv_kwh,
                load_kwh=planned.load_kwh,
                battery_kwh=bounded,
                grid_kwh=grid_kwh,
            ),
            BatteryEnergyFlows(
                pv_to_battery_kwh=pv_to_battery,
                grid_to_battery_kwh=grid_to_battery,
                battery_to_load_kwh=battery_to_load,
                battery_to_grid_kwh=battery_to_grid,
                losses_kwh=limited_loss,
            ),
        )

    limited_plan, limited_flows = routed(planned.pv_kwh)
    if limited_plan.grid_kwh < -ENERGY_EPSILON_KWH:
        if export_state is ExportState.VERIFIED_ALLOWED:
            pass
        elif export_state in {
            ExportState.CONFIRMED_ZERO_EXPORT,
            ExportState.PROHIBITED,
        }:
            # Curtail only the PV surplus needed to reach zero grid exchange.
            # This is also valid while discharging: after curtailment the
            # battery may feed the newly exposed load deficit.  If removing
            # all PV would still leave export, that residual necessarily comes
            # from the battery and the adapter must stay closed.
            curtailed_pv = planned.pv_kwh + limited_plan.grid_kwh
            if curtailed_pv < -ENERGY_EPSILON_KWH:
                raise CanonicalRuntimeError("canonical_export_prohibited")
            limited_plan, limited_flows = routed(max(curtailed_pv, 0.0))
            if limited_plan.grid_kwh < -ENERGY_EPSILON_KWH:
                raise CanonicalRuntimeError("canonical_export_prohibited")
        else:
            raise CanonicalRuntimeError(
                "canonical_export_disposition_unverified"
            )
    if (
        limited_plan.grid_kwh > ENERGY_EPSILON_KWH
        and not grid_import_verified
    ):
        raise CanonicalRuntimeError("canonical_grid_import_unavailable")
    next_energy = min(capacity_kwh, max(0.0, stored_energy_kwh + bounded))
    return limited_plan, limited_flows, next_energy


def _expectations(
    action: ArbiterRequestedAction,
    point: Mapping[str, Any],
    frame: Any,
    settings: Any,
    *,
    pv_hold_target_percent: float | None = None,
) -> tuple[CommandExpectation, ReadbackExpectation]:
    # Local import avoids a second alias in the already crowded public type set.
    if __package__:
        from .supervisor_canonical_ledger import PhysicalExpectation
    else:
        from supervisor_canonical_ledger import PhysicalExpectation

    target, generation, values = _command_for_action(action, point, frame,
        settings, pv_hold_target_percent=pv_hold_target_percent)
    physical = {
        ArbiterRequestedAction.NONE: PhysicalExpectation.NONE,
        ArbiterRequestedAction.RCE_EXPORT: PhysicalExpectation.GRID_EXPORT_AND_BATTERY_DISCHARGE,
        ArbiterRequestedAction.PV_CHARGE_HOLD: PhysicalExpectation.PV_EXPORT_AND_HOUSE_SELF_CONSUMPTION,
        ArbiterRequestedAction.TARIFF_BATTERY_CHARGE: PhysicalExpectation.GRID_IMPORT_AND_BATTERY_CHARGE,
        ArbiterRequestedAction.TARIFF_GRID_SUPPORT: PhysicalExpectation.GRID_IMPORT_AND_NO_BATTERY_DISCHARGE,
        ArbiterRequestedAction.TARIFF_GRID_SUPPORT_AND_CHARGE: PhysicalExpectation.GRID_IMPORT_AND_BATTERY_CHARGE,
        ArbiterRequestedAction.RCM_ABSORB_PV: PhysicalExpectation.PV_SURPLUS_AND_BATTERY_CHARGE,
        ArbiterRequestedAction.RCM_LIMIT_EXPORT: PhysicalExpectation.EXPORT_NOT_ABOVE_SNAPSHOT,
        ArbiterRequestedAction.RCM_PRE_DISCHARGE: PhysicalExpectation.GRID_EXPORT_AND_BATTERY_DISCHARGE,
    }[action]
    return (
        CommandExpectation(target, generation, values),
        ReadbackExpectation(
            target,
            action is not ArbiterRequestedAction.NONE,
            values,
            physical,
        ),
    )


def build_supervisor_canonical_ledger(
    *,
    frame: Any,
    timelines: Mapping[str, Mapping[str, Any]],
    usable_capacity_kwh: float,
    freshness_now: datetime | None = None,
) -> CanonicalExecutionLedger:
    """Build one bounded continuous trajectory from the required current timelines."""

    if frame is None or not all(
        hasattr(frame, name)
        for name in (
            "now",
            "decision",
            "candidates",
            "context",
            "rce",
            "tariff",
            "rcm",
            "execution",
        )
    ):
        raise CanonicalRuntimeError("active_frame_missing")
    frame_now = _utc(frame.now, code="active_frame_time_invalid")
    freshness_reference = (
        frame_now
        if freshness_now is None
        else _utc(freshness_now, code="canonical_freshness_time_invalid")
    )
    capacity = _number(
        usable_capacity_kwh,
        code="battery_capacity_invalid",
        minimum=0.001,
        maximum=10_000.0,
    )
    if frame.context.critical_bms_ready is not True:
        raise CanonicalRuntimeError("battery_soc_unverified")
    initial_soc = _number(
        frame.execution.battery_soc_percent,
        code="battery_soc_unverified",
        minimum=0.0,
        maximum=100.0,
    )
    observed_soc = _utc(
        frame.execution.battery_soc_observed_at,
        code="battery_soc_unverified",
    )
    soc_age = (freshness_reference - observed_soc).total_seconds()
    if soc_age < -MAX_FUTURE_SKEW_SECONDS or soc_age > 120.0:
        raise CanonicalRuntimeError("battery_soc_unverified")

    normalized = _validated_timelines(timelines, now=freshness_reference,
                                     required=canonical_timeline_dependencies(frame))
    boundaries = _common_boundaries(normalized, now=frame_now)
    templates = _template_candidates(frame)
    settings = settings_from_execution_source(frame.execution)
    stored_powers = {
        policy_id: _stored_power_by_point(payload, capacity_kwh=capacity)
        for policy_id, payload in normalized.items()
    }

    slots: list[CanonicalSlotInput] = []
    arbitration_revisions: list[str] = []
    stored_energy_cursor = capacity * initial_soc / 100.0
    for slot_index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        points: dict[str, Mapping[str, Any]] = {}
        point_indices: dict[str, int] = {}
        raw_candidates: list[ArbiterPolicyCandidate] = []
        current_interval = start <= frame_now < end
        projected_soc_percent = stored_energy_cursor / capacity * 100.0
        for policy_id in POLICY_ORDER:
            covered = (_covering_point(normalized[policy_id], start, end)
                       if policy_id in normalized else None)
            if covered is None:
                point_index = None
                point = _uncovered_point()
            else:
                point_index, point = covered
            points[policy_id] = point
            if point_index is not None:
                point_indices[policy_id] = point_index
        interval_coverage_verified = bool(
            not current_interval
            or len(point_indices) == len(POLICY_ORDER)
            or (
                set(point_indices) == {"rce", "tariff"}
                and ("rcm" not in normalized or _current_rcm_is_idle(
                    frame, templates["rcm"], now=freshness_reference))
            )
        )
        for policy_id in POLICY_ORDER:
            if policy_id not in normalized:
                # Preserve the disabled runtime candidate and its uncertainty.
                # It contributes no points, energy or invented freshness.
                raw_candidates.append(templates[policy_id])
                continue
            raw_candidates.append(
                _build_candidate(
                    policy_id=policy_id,
                    point=points[policy_id],
                    timeline=normalized[policy_id],
                    template=templates[policy_id],
                    frame=frame,
                    settings=settings,
                    start=start,
                    end=end,
                    current_interval=current_interval,
                    interval_coverage_verified=interval_coverage_verified,
                    projected_soc_percent=projected_soc_percent,
                )
            )
        context = _planning_context(frame, observed_at=start)
        if not current_interval:
            # The output-only future arbiter must see the SOC reached by all
            # preceding canonical intervals, not today's live SOC repeated
            # across the horizon. Keep observed hardware/direction gates;
            # future selection remains UNVERIFIED and cannot authorize a write.
            context = replace(
                context, battery_soc_percent=projected_soc_percent
            )
        decision = arbitrate_supervisor(
            mode=frame.decision.supervisor_mode,
            profile=frame.decision.profile,
            context=context,
            candidates=tuple(raw_candidates),
            now=start,
        )
        if len(decision.candidate_summaries) != 3:
            raise CanonicalRuntimeError("slot_arbitration_invalid")
        arbitration_revisions.append(decision.arbitration_revision)
        raw_by_policy = {candidate.policy_id: candidate for candidate in raw_candidates}
        projected = tuple(
            _candidate_projection(
                frame_now=frame_now,
                start=start,
                end=end,
                raw=raw_by_policy[summary.policy_id],
                summary=summary,
                decision=decision,
            )
            for summary in decision.candidate_summaries
        )
        selected_raw = (
            raw_by_policy[decision.selected_policy]
            if decision.selected_policy is not None
            else None
        )
        selected_action = (
            selected_raw.requested_action
            if selected_raw is not None
            else ArbiterRequestedAction.NONE
        )
        selected_policy = _ledger_policy(decision.selected_policy)
        selected_point_policy = (
            decision.selected_policy.value
            if decision.selected_policy is not None
            else next(
                (
                    policy_id
                    for policy_id in ("tariff", "rce", "rcm")
                    if policy_id in point_indices
                    and points[policy_id].get("quality") == "complete"
                    and _action_for_point(policy_id, points[policy_id])
                    is ArbiterRequestedAction.NONE
                ),
                None,
            )
        )
        if selected_point_policy is None:
            raise CanonicalRuntimeError("canonical_energy_backbone_unavailable")
        if selected_point_policy not in point_indices:
            raise CanonicalRuntimeError("timeline_coverage_gap")
        selected_point = points[selected_point_policy]
        duration_hours = (end - start).total_seconds() / 3600.0
        planned, flows = _energy_for_segment(
            selected_point,
            stored_power_kw=stored_powers[selected_point_policy][
                point_indices[selected_point_policy]
            ],
            duration_hours=duration_hours,
        )
        physical_floor = _optional_number(
            frame.context.physical_protected_soc_floor_percent,
            minimum=0.0,
            maximum=100.0,
        )
        reserve_values = tuple(
            value
            for value in (
                physical_floor,
                *(
                    _optional_number(
                        point.get("protected_soc_floor_percent"),
                        minimum=0.0,
                        maximum=100.0,
                    )
                    for point in points.values()
                )
            )
            if value is not None
        )
        if not reserve_values:
            raise CanonicalRuntimeError("protected_reserve_missing")
        protected_reserve_percent = max(reserve_values)
        projection_floor_percent = protected_reserve_percent
        if selected_action is ArbiterRequestedAction.RCM_PRE_DISCHARGE:
            projection_floor_percent = max(
                projection_floor_percent,
                _number(
                    selected_point.get("target_soc_percent"),
                    code="rcm_pre_discharge_target_missing",
                    minimum=0.0,
                    maximum=100.0,
                ),
            )
        planned, flows, stored_energy_cursor = _limit_segment_to_physical_soc(
            planned,
            flows,
            stored_energy_kwh=stored_energy_cursor,
            capacity_kwh=capacity,
            protected_reserve_kwh=capacity * projection_floor_percent / 100.0,
            export_state=frame.context.export_state,
            grid_import_verified=bool(
                frame.context.physical_mode_fresh is True
                and frame.context.physical_mode
                in {
                    PhysicalMode.SELF_USE,
                    PhysicalMode.GRID_CHARGE,
                    PhysicalMode.GRID_DISCHARGE,
                }
            ),
        )
        command, readback = _expectations(
            selected_action,
            selected_point,
            frame,
            settings,
            pv_hold_target_percent=(selected_raw.protected_soc_floor_percent
                if selected_raw is not None
                and selected_action is ArbiterRequestedAction.PV_CHARGE_HOLD else None),
        )
        selected_projection = next(
            (
                candidate
                for candidate in projected
                if candidate.policy_id is selected_policy
            ),
            None,
        )
        slot_verdict = (
            selected_projection.start_eligibility
            if selected_projection is not None
            else StartEligibility.NOT_APPLICABLE
        )
        slots.append(
            CanonicalSlotInput(
                slot_id=f"slot-{slot_index:03d}",
                starts_at=start,
                ends_at=end,
                candidates=projected,
                selected_policy=selected_policy,
                selected_action=_ledger_action(selected_action),
                start_eligibility=slot_verdict,
                owner=_ledger_owner(decision.selected_policy, context),
                planned=planned,
                flows=flows,
                protected_reserve_percent=protected_reserve_percent,
                command_expectation=command,
                readback_expectation=readback,
            )
        )

    combined_revision = _sha(
        {
            "frame_arbitration_revision": frame.decision.arbitration_revision,
            "timeline_revisions": {
                policy_id: {
                    "input_revision": payload["input_revision"],
                    "plan_revision": payload["plan_revision"],
                }
                for policy_id, payload in normalized.items()
            },
            "slot_arbitration_revisions": arbitration_revisions,
        }
    )
    try:
        return build_canonical_execution_ledger(
            built_at=frame_now,
            arbitration_revision=combined_revision,
            usable_capacity_kwh=capacity,
            initial_soc_percent=initial_soc,
            slots=slots,
        )
    except ValueError as err:
        code = (
            "canonical_soc_out_of_bounds"
            if str(err) == "canonical SOC would leave the physical 0..100 range"
            else "canonical_ledger_invalid"
        )
        raise CanonicalRuntimeError(code) from err


__all__ = (
    "canonical_timeline_dependencies",
    "CanonicalRuntimeError",
    "augment_canonical_projection_payload",
    "build_supervisor_canonical_ledger",
)
