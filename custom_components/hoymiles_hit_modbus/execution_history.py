"""Read-only presentation of recorded measurements and Supervisor evidence.

Nothing in this module reconstructs a plan or grants execution authority.
Intervals end at the last recorded observation; missing evidence stays missing.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from bisect import bisect_right
from datetime import datetime
import math
from copy import deepcopy
import base64
import binascii
import hashlib
import json
import zlib
from functools import lru_cache
from typing import Any

HISTORY_SECONDS = 48 * 60 * 60
BIN_SECONDS = 5 * 60
MAX_ROWS = 50_000
MAX_DECISION_ROWS = 250_000
MAX_EVENTS = 4_000

# Live attributes remain available to the UI/controller. Recorder retains this
# projection instead of copying complete candidates and transactions on every tick.
RECORDED_EXECUTION_ATTRIBUTE = "recorded_execution"
SUPERVISOR_UNRECORDED_ATTRIBUTES = frozenset({
    "candidate_summaries", "transaction_evidence", "execution_context_evidence",
    "tariff_decision",
    "execution_health", "control_lease", "control_lease_renewal",
    "arbitration_revision", "execution_last_valid_read_at",
    "profile_effects_not_applied",
    "rce_export_evidence",
})
_RECORDED_DECISION_KEYS = (
    "execution_phase", "selected_policy", "selected_action", "owner", "reason",
    "lifecycle_reason", "selection_reason", "execution_blocked_reason",
    "transaction_id", "transaction_evidence_scope", "rejected_reasons",
)

# Decision fields are already preserved in recorded_execution v1.
# Exclude the duplicate historical copy, preserving the live entity.
SUPERVISOR_UNRECORDED_ATTRIBUTES |= frozenset(_RECORDED_DECISION_KEYS)


def supervisor_recorder_projection(
    attributes: Mapping[str, Any],
    *,
    previous: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Keep decision proof and anchor its input provenance at transitions."""
    result = {key: attributes.get(key) for key in _RECORDED_DECISION_KEYS}
    result["schema_version"] = 3
    # Keep status transitions without recording a new timestamp/power payload
    # on every tick. Exact flow cohorts remain live and frozen in STOP frames.
    export_proof = attributes.get("rce_export_evidence")
    if isinstance(export_proof, Mapping):
        result["rce_export_evidence"] = {key: export_proof.get(key) for key in (
            "status", "reason", "control_authority",
        )}
    candidates = attributes.get("candidate_summaries")
    result["candidate_summaries"] = [
        {key: candidate.get(key) for key in (
            "policy_id", "reason_code", "start_eligible", "continuation_eligible"
        )}
        for candidate in candidates[:3] if isinstance(candidate, Mapping)
    ] if isinstance(candidates, (list, tuple)) else []
    transaction = attributes.get("transaction_evidence")
    transaction = transaction if isinstance(transaction, Mapping) else {}
    proof = transaction.get("physical_verification")
    proof = proof if isinstance(proof, Mapping) else {}
    result["transaction_evidence"] = {
        **{key: transaction.get(key) for key in (
            "transaction_id", "command_sent_at", "readback_result", "reason",
            "rollback_status", "rollback_result",
        )},
        "physical_verification": {key: proof.get(key) for key in (
            "status", "observed_at", "transaction_id", "action", "evidence",
        )},
    }
    tariff_decision = attributes.get("tariff_decision")
    if isinstance(tariff_decision, Mapping):
        projected_tariff = dict(tariff_decision)
        current_frame = projected_tariff.pop("source_frame", None)
        previous_tariff = previous.get("tariff_decision") if isinstance(previous, Mapping) else None
        previous_semantics = dict(previous_tariff) if isinstance(previous_tariff, Mapping) else None
        if previous_semantics is not None:
            previous_frame = previous_semantics.pop("source_frame", None)
            previous_other = {key: value for key, value in previous.items() if key != "tariff_decision"}
            current_other = {key: value for key, value in result.items() if key != "tariff_decision"}
            if previous_other == current_other and previous_semantics == projected_tariff:
                current_frame = previous_frame
        projected_tariff["source_frame"] = (
            dict(current_frame) if isinstance(current_frame, Mapping) else None
        )
        result["tariff_decision"] = projected_tariff
    else:
        result["tariff_decision"] = None
    return result


def recorded_supervisor_attributes(attributes: Mapping[str, Any]) -> Mapping[str, Any]:
    """Read both legacy rows and the compact format; unknown formats prove nothing."""
    if RECORDED_EXECUTION_ATTRIBUTE not in attributes:
        return attributes
    recorded = attributes[RECORDED_EXECUTION_ATTRIBUTE]
    if (not isinstance(recorded, Mapping)
            or type(recorded.get("schema_version")) is not int
            or recorded["schema_version"] not in {1, 2, 3}):
        return {}
    return recorded


# Additive reader retained by the scoped rollback. The writer lives only in
# SupervisorSensor; rolling capture, publication cadence and authority do not
# change. This is compression, not a new event journal or Recorder commit ACK.
RECORDED_STOP_ATTRIBUTE = "recorded_stop_decisions"
MAX_STOP_DECODE_BYTES = 262144


@lru_cache(maxsize=8)
def _encode_stop_bytes(raw: bytes) -> str:
    return base64.b64encode(zlib.compress(raw, level=6)).decode("ascii")


def stop_recorder_projection(frames: list) -> dict:
    """Losslessly encode the current buffer; raw fallback preserves failures."""
    # JSON is also the existing HA state boundary. Copy the raw fallback so a
    # later controller update cannot mutate an already published snapshot.
    raw = json.dumps(frames, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    fallback = {"schema_version": 1, "encoding": "json", "frames": deepcopy(frames)}
    if len(raw) > MAX_STOP_DECODE_BYTES:
        return fallback
    try:
        result = {"schema_version": 1, "encoding": "zlib-base64-json",
                  "raw_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                  "data": _encode_stop_bytes(raw)}
    except (zlib.error, ValueError):
        return fallback
    return result if len(json.dumps(result)) < len(json.dumps(fallback)) else fallback


def recorded_stop_decisions(attributes: Mapping[str, Any]) -> list | None:
    """Decode only stored evidence. None means unavailable/corrupt, not empty."""
    if RECORDED_STOP_ATTRIBUTE not in attributes:
        frames = attributes.get("recent_stop_decisions")
    else:
        payload = attributes[RECORDED_STOP_ATTRIBUTE]
        if (not isinstance(payload, Mapping)
                or type(payload.get("schema_version")) is not int
                or payload["schema_version"] != 1):
            return None
        if payload.get("encoding") == "json":
            frames = payload.get("frames")
        elif payload.get("encoding") == "zlib-base64-json":
            size, data = payload.get("raw_bytes"), payload.get("data")
            if (type(size) is not int or not 0 <= size <= MAX_STOP_DECODE_BYTES
                    or not isinstance(data, str) or len(data) > MAX_STOP_DECODE_BYTES * 2):
                return None
            try:
                compressed = base64.b64decode(data, validate=True)
                decoder = zlib.decompressobj()
                raw = decoder.decompress(compressed, size + 1)
                if (len(raw) != size or not decoder.eof or decoder.unused_data
                        or decoder.unconsumed_tail
                        or hashlib.sha256(raw).hexdigest() != payload.get("sha256")):
                    return None
                frames = json.loads(raw)
            except (ValueError, TypeError, zlib.error, binascii.Error, UnicodeError, RecursionError):
                return None
        else:
            return None
    if not isinstance(frames, list) or len(frames) > 8:
        return None
    if any(not isinstance(frame, dict) or not isinstance(frame.get("transaction_id"), str)
           or not frame["transaction_id"] for frame in frames):
        return None
    return frames


def number(value: Any) -> float | None:
    """Accept finite recorded numbers without turning missing data into zero."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def timestamp(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.timestamp() if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def measurement_value(role: str, state: Any, attrs: Mapping[str, Any]) -> float | None:
    value = number(state)
    if value is None:
        return None
    unit = attrs.get("unit_of_measurement")
    if role == "soc":
        return value if unit == "%" and 0 <= value <= 100 else None
    if unit not in {"W", "kW"}:
        return None
    value *= 0.001 if unit == "W" else 1.0
    if role in {"pv", "load"} and value < 0:
        return None
    # Physical Overview signs: +discharge / +export. Public: +charge / +import.
    return -value if role in {"battery", "grid"} else value


def measurement_bins(
    rows: Iterable[tuple[float, Any, Mapping[str, Any]]],
    role: str,
    start: float,
    end: float,
    runs: list[tuple[float, float]],
) -> list[dict[str, Any]]:
    """Time-weighted recorded states; SOC is the last recorded level per bin.

    Recorder stores changes, not every unchanged device report. A recorded
    state persists until its next change, but never across a Recorder restart.
    No live value fills history; unknown/unavailable states remain gaps.
    """
    count = math.ceil((end - start) / BIN_SECONDS)
    sums = [0.0] * count
    coverage = [0.0] * count
    last = [None] * count
    positive = [0.0] * count
    negative = [0.0] * count
    observed_at = [None] * count
    ordered = list(rows)
    for index, (observed, state, attrs) in enumerate(ordered):
        value = measurement_value(role, state, attrs)
        if value is None:
            continue
        next_time = ordered[index + 1][0] if index + 1 < len(ordered) else end
        for run_start, run_end in runs:
            # An old value cannot supply a newly started Recorder session.
            if observed < run_start:
                continue
            left = max(start, observed, run_start)
            right = min(end, next_time, run_end)
            while left < right:
                bucket = min(int((left - start) / BIN_SECONDS), count - 1)
                boundary = min(right, start + (bucket + 1) * BIN_SECONDS)
                weight = boundary - left
                sums[bucket] += value * weight
                positive[bucket] += max(0, value) * weight / 3600
                negative[bucket] += max(0, -value) * weight / 3600
                coverage[bucket] += weight
                last[bucket] = value
                observed_at[bucket] = observed
                left = boundary
    return [{
            "time": start + index * BIN_SECONDS,
            "value": round(last[index] if role == "soc" else sums[index] / coverage[index], 4)
            if coverage[index] >= min(BIN_SECONDS, end - (start + index * BIN_SECONDS)) * 0.99 else None,
            "coverage_seconds": round(coverage[index], 2),
            "observed_at": observed_at[index],
            "positive_kwh": round(positive[index], 6) if role != "soc" and coverage[index] >= BIN_SECONDS * 0.99 else None,
            "negative_kwh": round(negative[index], 6) if role != "soc" and coverage[index] >= BIN_SECONDS * 0.99 else None,
        } for index in range(count)]


def execution_intervals(rows, start, end, runs):
    """Join consecutive confirmations of the same action, ignoring UI reasons.

    Reasons may change on each heartbeat. Neither those changes nor a previous
    transaction are permission to bridge an unconfirmed observation or outage.
    """
    result = []
    previous = None
    for observed, state, attrs in rows:
        run = next((i for i, (left, right) in enumerate(runs) if left <= observed < right), None)
        if not start <= observed < end or run is None:
            previous = None
            continue
        evidence = decision_evidence(state, attrs, observed)
        key = (run, evidence["policy"], evidence["action"], evidence["transaction_id"])
        if not evidence["confirmed"]:
            previous = None
            continue
        if previous == key and observed - result[-1]["end"] <= BIN_SECONDS:
            result[-1]["end"] = observed
        else:
            result.append({"start": observed, "end": observed, "policy": evidence["policy"],
                           "action": evidence["action"], "transaction_id": evidence["transaction_id"]})
        previous = key
    return result


def active_intervals(rows, start, end, runs):
    """Recorded balancing flag only; this is not Supervisor confirmation."""
    result = []
    for index, (observed, state, _attrs) in enumerate(rows):
        if state != "on":
            continue
        until = rows[index + 1][0] if index + 1 < len(rows) else end
        for run_start, run_end in runs:
            left, right = max(start, observed, run_start), min(end, until, run_end)
            if observed >= run_start and right > left:
                result.append({"start": left, "end": right, "policy": "balance", "action": "battery_balancing"})
    return result


def energy_summary(rows_by_role, executions, start, end, runs):
    """Integrate recorded powers only inside confirmed execution intervals.

    Split at every recorded change, confirmation boundary and Recorder run.
    Net powers do not uniquely identify grid-to-battery/home flow with PV on;
    return physical flow bounds instead of assuming a PV dispatch order. These
    observational estimates never feed the Supervisor accounting ledger.
    """
    roles = ("pv", "load", "battery", "grid")
    rows = {role: list(rows_by_role.get(role, ())) for role in roles}
    times = {role: [row[0] for row in rows[role]] for role in roles}
    actions = {
        "rce_export": {("rce", "rce_export")},
        "rcm_discharge": {("rcm", "rcm_pre_discharge")},
        "tariff_charge": {("tariff", "tariff_battery_charge"), ("tariff", "tariff_grid_support_and_charge")},
        "grid_home": {("tariff", action) for action in (
            "tariff_battery_charge", "tariff_grid_support", "tariff_grid_support_and_charge")},
    }
    result = {key: {"minimum_kwh": None, "maximum_kwh": None, "covered_seconds": 0.0,
                    "execution_seconds": 0.0, "observations": 0} for key in actions}
    boundaries = sorted({start, end, *(time for points in times.values() for time in points if start < time < end),
                         *(time for run in runs for time in run if start < time < end)})
    for event in executions:
        keys = [key for key, accepted in actions.items() if (event["policy"], event["action"]) in accepted]
        left, right = max(start, event["start"]), min(end, event["end"])
        for key in keys:
            result[key]["observations"] += 1
            result[key]["execution_seconds"] += max(0, right - left)
        if not keys or right <= left:
            continue
        cuts = [left, *boundaries[bisect_right(boundaries, left):bisect_right(boundaries, right - 1e-6)], right]
        for a, b in zip(cuts, cuts[1:]):
            run = next(((lo, hi) for lo, hi in runs if lo <= a and b <= hi), None)
            if run is None:
                continue
            values = {}
            for role in roles:
                index = bisect_right(times[role], a) - 1
                row = rows[role][index] if index >= 0 else None
                values[role] = measurement_value(role, row[1], row[2]) if row and row[0] >= run[0] else None
            pv, load, battery, grid = (values[role] for role in roles)
            for key in keys:
                low = high = None
                if key == "rce_export" and grid is not None:
                    low = high = max(0, -grid)
                elif key == "rcm_discharge" and battery is not None:
                    low = high = max(0, -battery)
                elif key == "tariff_charge" and all(v is not None for v in (grid, battery, pv)):
                    low, high = max(0, battery - pv), min(max(0, battery), max(0, grid))
                elif key == "grid_home" and all(v is not None for v in (grid, battery, pv, load)):
                    low, high = max(0, load - pv - max(0, -battery)), min(load, max(0, grid))
                if low is None or high < low:
                    continue
                metric = result[key]
                metric["minimum_kwh"] = (metric["minimum_kwh"] or 0) + low * (b - a) / 3600
                metric["maximum_kwh"] = (metric["maximum_kwh"] or 0) + high * (b - a) / 3600
                metric["covered_seconds"] += b - a
    return {key: {field: round(value, 6) if isinstance(value, float) else value
                  for field, value in metric.items()} for key, metric in result.items()}


def decision_evidence(state: str, attrs: Mapping[str, Any], observed: float) -> dict[str, Any]:
    """Project historical attributes; a previous transaction cannot be active."""
    attrs = recorded_supervisor_attributes(attrs)
    tx = attrs.get("transaction_evidence")
    tx = tx if isinstance(tx, Mapping) else {}
    active = attrs.get("transaction_evidence_scope") == "active"
    proof = tx.get("physical_verification")
    proof = proof if isinstance(proof, Mapping) else {}
    readback = tx.get("readback_result") if active else None
    physical = proof.get("status") if active else None
    sent_at = timestamp(tx.get("command_sent_at")) if active else None
    proof_at = timestamp(proof.get("observed_at")) if active else None
    tx_id = attrs.get("transaction_id")
    phase = attrs.get("execution_phase") or state
    confirmed = (
        active and phase == "executing" and bool(tx_id)
        and attrs.get("selected_policy") in {"rce", "tariff", "rcm"}
        and attrs.get("owner") == attrs.get("selected_policy")
        and isinstance(attrs.get("selected_action"), str)
        and tx_id == tx.get("transaction_id")
        and sent_at is not None and sent_at <= observed
        and readback == "confirmed" and physical == "confirmed"
        and proof.get("transaction_id") == tx_id
        and proof.get("action") == attrs.get("selected_action")
        and proof_at is not None and sent_at <= proof_at <= observed
    )
    reasons = [attrs.get("selection_reason"), attrs.get("execution_blocked_reason")]
    candidates = attrs.get("candidate_summaries") or []
    if isinstance(candidates, (list, tuple)):
        for candidate in candidates[:3]:
            if isinstance(candidate, Mapping) and candidate.get("policy_id") == attrs.get("selected_policy"):
                reasons.append(candidate.get("reason_code"))
    rejected = attrs.get("rejected_reasons")
    rejected = rejected if isinstance(rejected, (list, tuple)) else []
    return {
        "state": state,
        "phase": phase,
        "policy": attrs.get("selected_policy"),
        "action": attrs.get("selected_action"),
        "owner": attrs.get("owner"),
        "reason": attrs.get("lifecycle_reason") or attrs.get("reason"),
        "decision_reasons": list(dict.fromkeys(str(item)[:160] for item in reasons if item)),
        "rejections": [{"policy": str(item.get("policy_id", ""))[:40],
                        "reason": str(item.get("reason", ""))[:160]}
                       for item in rejected[:3] if isinstance(item, Mapping)],
        "transaction_id": tx_id if active else None,
        "readback": readback,
        "physical": physical,
        "confirmed": confirmed,
    }


def decision_intervals(
    rows: Iterable[tuple[float, str, Mapping[str, Any]]], start: float, end: float,
    runs: list[tuple[float, float]] | None = None,
) -> list[dict[str, Any]]:
    """Keep every semantic transition, including attribute-only changes.

    A five-minute observation gap splits the interval. Never stretch an old
    execution across a restart/outage, or to the present without new evidence.
    """
    result: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    previous_run = None
    interval_runs = [(start, end)] if runs is None else runs
    for observed, state, attrs in rows:
        if not start <= observed <= end:
            continue
        evidence = decision_evidence(state, attrs, observed)
        run_id = next((i for i, (left, right) in enumerate(interval_runs)
                       if left <= observed < right), None)
        if run_id is None:
            continue
        if (result and evidence == previous
                and run_id == previous_run
                and observed - result[-1]["end"] <= BIN_SECONDS):
            result[-1]["end"] = observed
        else:
            if len(result) >= MAX_EVENTS:
                raise ValueError("history_event_limit")
            result.append({"start": observed, "end": observed, **evidence})
        previous = evidence
        previous_run = run_id
    return result
