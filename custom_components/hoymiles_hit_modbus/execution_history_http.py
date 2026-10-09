"""Authenticated, bounded Recorder projection for the 48-hour EMS history."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from functools import partial
import json
import time

from aiohttp import web
from sqlalchemy import and_, case, func, or_, select

from homeassistant.components.http.view import HomeAssistantView
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import RecorderRuns, StateAttributes, States
from homeassistant.components.recorder.util import session_scope
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN
from .bounded_history import recorder_query_budget
from .execution_history import (
    HISTORY_SECONDS, MAX_DECISION_ROWS, MAX_ROWS, active_intervals, decision_intervals,
    energy_summary, execution_intervals, measurement_bins,
    RECORDED_EXECUTION_ATTRIBUTE, recorded_supervisor_attributes,
    RECORDED_STOP_ATTRIBUTE, recorded_stop_decisions, timestamp,
)

_ROLES = {
    "soc": "overview_battery_soc",
    "pv": "overview_pv_total_power",
    "battery": "overview_battery_power",
    "grid": "overview_grid_total_active_power",
    "supervisor": "ems_supervisor",
}

_DECISION_FIELDS = (
    "execution_phase", "selected_policy", "selected_action", "owner", "reason",
    "lifecycle_reason", "selection_reason", "execution_blocked_reason", "transaction_id",
    "transaction_evidence_scope",
    "transaction_evidence.transaction_id", "transaction_evidence.command_sent_at",
    "transaction_evidence.readback_result", "transaction_evidence.physical_verification.status",
    "transaction_evidence.physical_verification.observed_at",
    "transaction_evidence.physical_verification.transaction_id",
    "transaction_evidence.physical_verification.action",
    "candidate_summaries[0].policy_id", "candidate_summaries[0].reason_code",
    "candidate_summaries[1].policy_id", "candidate_summaries[1].reason_code",
    "candidate_summaries[2].policy_id", "candidate_summaries[2].reason_code",
    "rejected_reasons",
)


def _supervisor_history_fields(fields, dialect):
    """Project old/new rows in SQL without accepting an unknown compact format."""
    path = "$." + RECORDED_EXECUTION_ATTRIBUTE
    compact = func.json_extract(fields, path)
    version = func.json_extract(fields, path + ".schema_version")
    if dialect == "sqlite":
        kind = func.json_type(fields, path)
        version_kind = func.json_type(fields, path + ".schema_version")
        object_kind, integer_kind = "object", "integer"
    else:  # MySQL and MariaDB expose the type of the extracted JSON value.
        kind = func.json_type(compact)
        version_kind = func.json_type(version)
        object_kind, integer_kind = "OBJECT", "INTEGER"
    source = case(
        (and_(kind == object_kind, version_kind == integer_kind, version.in_((1, 2, 3))), compact),
        (kind.is_not(None), "{}"),
        else_=fields,
    )
    return func.json_extract(source, *("$." + key for key in _DECISION_FIELDS))


def _decode_projection(raw, role, projected):
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    if not projected:
        if not isinstance(value, dict):
            return {}
        return recorded_supervisor_attributes(value) if role == "supervisor" else value
    if not isinstance(value, list) or len(value) != len(_DECISION_FIELDS):
        return {}
    attrs = dict(zip(_DECISION_FIELDS[:10], value[:10]))
    attrs["transaction_evidence"] = {
        "transaction_id": value[10], "command_sent_at": value[11], "readback_result": value[12],
        "physical_verification": dict(zip(("status", "observed_at", "transaction_id", "action"), value[13:17])),
    }
    attrs["candidate_summaries"] = [{"policy_id": value[i], "reason_code": value[i + 1]} for i in (17, 19, 21)]
    attrs["rejected_reasons"] = value[23]
    return attrs


def _read_history(hass, sources: dict[str, str], start: float, end: float) -> dict:
    """Run only on Recorder's executor, with SQL row and wall-clock budgets."""
    deadline = time.monotonic() + 45
    result = {"start": start, "end": end, "sources": sources,
              "series": {}, "events": [], "executions": [], "balancing": [],
              "missing": [], "interval_seconds": 300}
    measurements = {}
    with session_scope(hass=hass, read_only=True) as session, recorder_query_budget(session, deadline):
        recorder = get_instance(hass)
        recorded_runs = list(session.execute(select(RecorderRuns).where(
            RecorderRuns.start < datetime.fromtimestamp(end, timezone.utc),
            or_(RecorderRuns.end.is_(None), RecorderRuns.end > datetime.fromtimestamp(start, timezone.utc)),
        ).order_by(RecorderRuns.start).limit(513)).scalars())
        if len(recorded_runs) > 512:
            raise ValueError("history_run_limit")
        runs = []
        for run in recorded_runs:
            run_start = run.start.replace(tzinfo=timezone.utc).timestamp()
            run_end = run.end.replace(tzinfo=timezone.utc).timestamp() if run.end else end
            if run.closed_incorrect:
                # A crash's synthetic run end may be the next startup. Stop at
                # the last stored row rather than bridge that unknown outage.
                last = session.execute(select(States.last_updated_ts).where(
                    States.last_updated_ts >= run_start, States.last_updated_ts < run_end,
                ).order_by(States.last_updated_ts.desc()).limit(1)).scalar()
                run_end = last if last is not None else run_start
            runs.append((run_start, run_end))
        for role, entity_id in sources.items():
            metadata = recorder.states_meta_manager.get(entity_id, session, False)
            limit = MAX_DECISION_ROWS if role == "supervisor" else MAX_ROWS
            projected = session.get_bind().dialect.name in {"sqlite", "mysql", "mariadb"}
            # Extract once inside the database. Passing entire transaction and
            # candidate blobs for every telemetry-triggered state is expensive.
            fields = StateAttributes.shared_attrs
            if projected and role == "supervisor":
                fields = _supervisor_history_fields(fields, session.get_bind().dialect.name)
            def state_rows():
                if metadata is None:
                    return
                base = (
                    select(States.last_updated_ts, States.state, fields)
                    .outerjoin(StateAttributes, States.attributes_id == StateAttributes.attributes_id)
                    .where(States.metadata_id == metadata)
                )
                query = (base.where(States.last_updated_ts >= start, States.last_updated_ts < end)
                    .order_by(States.last_updated_ts, States.state_id)
                    .limit(limit + 1)
                    .execution_options(yield_per=128)
                )
                previous = session.execute(base.where(States.last_updated_ts < start)
                    .order_by(States.last_updated_ts.desc(), States.state_id.desc()).limit(1)).first()
                if previous is not None and role != "supervisor":
                    yield previous[0], previous[1], _decode_projection(previous[2], role, False)
                pending = None
                previous_raw = None
                previous_state = None
                previous_attrs = None
                emitted_at = start - 300
                for index, (observed, state, raw) in enumerate(session.execute(query)):
                    if index >= limit:
                        raise ValueError("history_row_limit")
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"history_timeout:{role}:{index}")
                    if (role == "supervisor" and pending is not None
                            and raw == previous_raw and state == previous_state
                            and observed - pending[0] <= 300 and observed - emitted_at < 120
                            and not any(pending[0] < run_start <= observed for run_start, _ in runs)):
                        pending = (observed, state, previous_attrs)
                        continue
                    if pending is not None and pending[0] > emitted_at:
                        yield pending
                    attrs = _decode_projection(raw, role, projected and role == "supervisor")
                    # Project immediately; do not retain large optimizer/transaction blobs.
                    if role != "supervisor":
                        attrs = {"unit_of_measurement": attrs.get("unit_of_measurement")}
                    else:
                        attrs = {k: attrs.get(k) for k in (
                            "execution_phase", "selected_policy", "selected_action", "owner",
                            "reason", "lifecycle_reason", "selection_reason", "execution_blocked_reason",
                            "candidate_summaries", "rejected_reasons", "transaction_id",
                            "transaction_evidence_scope", "transaction_evidence",
                        )}
                        tx = attrs.get("transaction_evidence")
                        attrs["transaction_evidence"] = {k: tx.get(k) for k in (
                            "transaction_id", "command_sent_at", "readback_result", "physical_verification",
                        )} if isinstance(tx, dict) else {}
                    pending = (observed, state, attrs)
                    previous_raw, previous_state, previous_attrs = raw, state, attrs
                    emitted_at = observed
                    yield pending
                if pending is not None and pending[0] > emitted_at:
                    yield pending
            if role == "supervisor":
                decisions = list(state_rows())
                result["events"] = decision_intervals(decisions, start, end, runs)
                result["executions"] = execution_intervals(decisions, start, end, runs)
                if not result["events"]:
                    result["missing"].append(role)
            elif role == "balance":
                result["balancing"] = active_intervals(list(state_rows()), start, end, runs)
            else:
                measurements[role] = list(state_rows())
                result["series"][role] = measurement_bins(measurements[role], role, start, end, runs)
                if not any(point["value"] is not None for point in result["series"][role]):
                    result["missing"].append(role)
    result["summary"] = energy_summary(measurements, result["executions"], start, end, runs)
    return result


def _read_stop_history(hass, sources: dict[str, str], start: float, end: float) -> dict:
    """Opt-in, bounded STOP diagnostic read; never part of the control loop."""
    deadline = time.monotonic() + 45
    result = {"start": start, "end": end, "schema_version": 1,
              "events": [], "status": "unavailable", "invalid_payloads": 0,
              "missing_rows": 0, "truncated": False,
              "capture_contract": "existing_rolling_eight_per_publication"}
    entity_id = sources.get("supervisor")
    if entity_id is None:
        return result
    seen_payloads, seen_frames = set(), set()
    total_bytes = 0
    output_bytes = 0
    transaction_frames = {}
    result["conflicting_transactions"] = 0
    valid_payloads = 0
    with session_scope(hass=hass, read_only=True) as session, recorder_query_budget(session, deadline):
        recorder = get_instance(hass)
        metadata = recorder.states_meta_manager.get(entity_id, session, False)
        if metadata is None:
            return result
        if session.get_bind().dialect.name not in {"sqlite", "mysql", "mariadb"}:
            result["status"] = "unsupported_backend"
            return result
        # Read JSON paths, not whole optimizer blobs. Row/time/output budgets
        # are separate; partial evidence is explicitly labelled.
        fields = StateAttributes.shared_attrs
        query = (select(States.last_updated_ts,
                        func.json_extract(fields, "$." + RECORDED_STOP_ATTRIBUTE),
                        func.json_extract(fields, "$.recent_stop_decisions"))
                 .outerjoin(StateAttributes, States.attributes_id == StateAttributes.attributes_id)
                 .where(States.metadata_id == metadata,
                        States.last_updated_ts >= start, States.last_updated_ts < end)
                 .order_by(States.last_updated_ts.desc(), States.state_id.desc())
                 .limit(MAX_DECISION_ROWS + 1).execution_options(yield_per=128))
        for index, (observed, compact, legacy) in enumerate(session.execute(query)):
            if index >= MAX_DECISION_ROWS or time.monotonic() > deadline:
                result["truncated"] = True
                break
            if compact is None and legacy is None:
                result["missing_rows"] += 1
                continue
            raw, key = (compact, RECORDED_STOP_ATTRIBUTE) if compact is not None else (legacy, "recent_stop_decisions")
            identity = (compact, legacy)
            if identity in seen_payloads:
                continue
            payload_bytes = sum(len(value.encode("utf-8")) for value in (compact, legacy) if value is not None)
            if len(seen_payloads) >= 512 or total_bytes + payload_bytes > 2_000_000:
                result["truncated"] = True
                break
            seen_payloads.add(identity)
            total_bytes += payload_bytes
            try:
                frames = recorded_stop_decisions({key: json.loads(raw)})
            except (TypeError, ValueError):
                frames = None
            if frames is None:
                result["invalid_payloads"] += 1
                # A valid raw copy is still real evidence when compact proof
                # is corrupt. Preserve it and keep the partial/corrupt marker.
                if compact is not None and legacy is not None:
                    try:
                        frames = recorded_stop_decisions({"recent_stop_decisions": json.loads(legacy)})
                    except (TypeError, ValueError):
                        frames = None
                if frames is None:
                    continue
            elif compact is not None and legacy is not None:
                try:
                    raw_frames = recorded_stop_decisions({"recent_stop_decisions": json.loads(legacy)})
                except (TypeError, ValueError):
                    raw_frames = None
                if raw_frames != frames:
                    result["invalid_payloads"] += 1
                    if raw_frames is not None:
                        frames = frames + raw_frames
            valid_payloads += 1
            for frame in frames:
                at = timestamp(frame.get("at"))
                if at is None:
                    result["invalid_payloads"] += 1
                    continue
                if not start <= at < end:
                    continue
                # Include complete contents in the identity: conflicting proof
                # for one transaction must not be silently discarded.
                frame_id = json.dumps(frame, sort_keys=True, separators=(",", ":"))
                if frame_id in seen_frames:
                    continue
                frame_bytes = len(frame_id.encode("utf-8"))
                if len(seen_frames) >= 128 or output_bytes + frame_bytes > 1_000_000:
                    result["truncated"] = True
                    break
                tx = frame["transaction_id"]
                if tx in transaction_frames and transaction_frames[tx] != frame_id:
                    result["conflicting_transactions"] += 1
                transaction_frames[tx] = frame_id
                output_bytes += frame_bytes
                seen_frames.add(frame_id)
                result["events"].append(frame)
            if result["truncated"]:
                break
    result["events"].sort(key=lambda frame: (timestamp(frame["at"]), frame["transaction_id"]))
    if (result["truncated"] or result["invalid_payloads"] or result["missing_rows"]
            or result["conflicting_transactions"]):
        result["status"] = "partial"
    elif valid_payloads:
        result["status"] = "available"
    return result


class HoymilesExecutionHistoryView(HomeAssistantView):
    """On-demand display data only; no subscriptions or control services."""

    url = f"/api/{DOMAIN}/execution-history"
    name = f"api:{DOMAIN}:execution_history"
    requires_auth = True

    def __init__(self) -> None:
        self._pending = None
        self._cache = None

    async def get(self, request: web.Request) -> web.Response:
        hass = request.app["hass"]
        registry = er.async_get(hass)
        supervisor = registry.async_get(request.query.get("entity_id", ""))
        if (supervisor is None or supervisor.platform != DOMAIN
                or supervisor.unique_id != f"{supervisor.config_entry_id}_ems_supervisor"):
            raise web.HTTPBadRequest(text="unknown_supervisor")
        entry_id = supervisor.config_entry_id
        sources = {}
        for role, suffix in _ROLES.items():
            candidates = [item.entity_id for item in registry.entities.values()
                          if item.platform == DOMAIN and item.config_entry_id == entry_id
                          and item.unique_id == f"{entry_id}_{suffix}"]
            if len(candidates) == 1:
                sources[role] = candidates[0]
        # This installation-wide managed LOAD template is shared by the planner.
        # Fail closed on multiple entries rather than mix installations.
        if len(hass.config_entries.async_entries(DOMAIN)) == 1:
            sources["load"] = "sensor.hoymiles_actual_load_power"
            sources["balance"] = "input_boolean.hoymiles_battery_balancing_active"
        user = request.get("hass_user")
        if user is None or any(not user.permissions.check_entity(eid, "read")
                               for eid in sources.values()):
            raise web.HTTPForbidden()
        stop_history = request.query.get("stop_history") == "1"
        key = (tuple(sorted(sources.items())), stop_history)
        if self._cache and self._cache[0] == key and time.monotonic() < self._cache[1]:
            return self.json(self._cache[2])
        if self._pending is not None and not self._pending.done():
            raise web.HTTPServiceUnavailable(text="history_busy")
        end = time.time()
        self._pending = asyncio.ensure_future(get_instance(hass).async_add_executor_job(
            partial(_read_stop_history if stop_history else _read_history,
                    hass, sources, end - HISTORY_SECONDS, end)
        ))
        # Shield the worker: timeouts must not allow overlapping database reads.
        self._pending.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        try:
            payload = await asyncio.wait_for(asyncio.shield(self._pending), timeout=47)
        except TimeoutError as err:
            raise web.HTTPServiceUnavailable(text="history_timeout") from err
        except ValueError as err:
            raise web.HTTPServiceUnavailable(text="history_unavailable") from err
        if not stop_history:
            payload["missing"] = sorted(set(payload["missing"]) | (set(_ROLES) | {"load", "balance"}) - set(sources))
        self._cache = (key, time.monotonic() + 60, payload)
        return self.json(payload)
