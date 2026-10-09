"""Sequential, privacy-safe analysis of many diagnostic archives."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import heapq
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .archive import (
    DEFAULT_LIMITS,
    ArchiveLimits,
    ArchiveReadError,
    discover_archives,
    load_diagnostic_archive,
)
from .extractors import extract_archive_evidence, merge_events, prefer_control_event
from .history import analyze_control_history
from .models import (
    ANALYSIS_SCHEMA_VERSION,
    ANALYZER_VERSION,
    RULE_SET_VERSION,
    SUPPORTED_REPORT_SCHEMA_VERSIONS,
    ArchiveStatus,
    Controller,
    ControllerObservation,
    Finding,
    LoadedDiagnosticArchive,
    Severity,
)
from .rules import evaluate_archive, evaluate_longitudinal


LIMITATIONS = (
    {
        "code": "SNAPSHOT_BIASED_SAMPLE",
        "message": (
            "Statystyki opisują odsetek dostarczonych paczek, nie częstość awarii "
            "w całej populacji instalacji."
        ),
    },
    {
        "code": "CONTROL_HISTORY_CAPABILITY_DEPENDENT",
        "message": (
            "Historia 24 h zawiera ograniczone atrybuty zapisane przez Recorder "
            "i rozpoznany schemat zdarzeń. Pola wyłączone z Recordera oraz "
            "szybka telemetria mogą być niedostępne. Ostatnie odnowienie lease "
            "i ramki STOP nie dowodzą każdego odnowienia ani FC03 każdego Slave."
        ),
    },
    {
        "code": "PHYSICAL_RESPONSE_CAPABILITY_DEPENDENT",
        "message": (
            "Ocena odpowiedzi fizycznej wymaga jawnego sensora odpowiedzi "
            "agregatowej v1.5.6; starsze paczki pozostają nieocenialne."
        ),
    },
)

LOG_PATTERNS: Mapping[str, tuple[re.Pattern[str], ...]] = {
    "optimizer_exception": (
        re.compile(r"cannot calculate .*?(?:rce|rcm|tariff)", re.I),
        re.compile(r"optimizer[_ ]error", re.I),
    ),
    "modbus_communication": (
        re.compile(r"(?:modbus|esphome).*(?:timeout|disconnect|socket|error)", re.I),
        re.compile(r"SocketClosedAPIError", re.I),
    ),
    "readback_failure": (
        re.compile(r"readback.*(?:fail|timeout|mismatch|unverified)", re.I),
        re.compile(r"cannot verify", re.I),
    ),
    "rollback_failure": (
        re.compile(r"rollback.*(?:fail|timeout|error)", re.I),
        re.compile(r"failed.*rollback", re.I),
    ),
    "recorder_history": (
        re.compile(r"cannot rebuild .*?history", re.I),
        re.compile(r"recorder.*(?:timeout|locked|database)", re.I),
    ),
    "asset_failure": (
        re.compile(r"failed to install optional hoymiles", re.I),
        re.compile(r"frontend.*(?:404|failed|error)", re.I),
    ),
}

LOG_NON_FAILURE_PATTERNS = (
    re.compile(
        r"\brollback\b.*\b(?:completed|finished|succeeded|successful)\b"
        r".*\bwithout\s+(?:an?\s+)?(?:error|failure|issue)s?\b",
        re.I,
    ),
    re.compile(
        r"\b(?:errors?|failures?|timeouts?)\s+count\s+"
        r"(?:is|=|:)\s*0(?:\.0+)?(?![\d.])",
        re.I,
    ),
    re.compile(
        r"\boptimizer[_ ]error\b\s+(?:is\s+)?"
        r"(?:not\s+present|absent|none|false|0)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:rollback|readback)\b.*\b(?:completed|verified)\s+"
        r"successfully\b",
        re.I,
    ),
)

LOG_LONG_LEVEL_PATTERN = re.compile(
    r"^\s*(?:(?:\d{4}-\d{2}-\d{2})[T ][0-9:.+\-Z]+\s+)?"
    r"(?:\[[^\]\r\n]{1,80}\]\s*)*"
    r"(?P<level>critical|error|warning|warn|info|debug|trace)\b",
    re.I,
)
LOG_BRACKET_LEVEL_PATTERN = re.compile(
    r"^\s*(?:\[[^\]\r\n]{1,80}\]\s*)*?"
    r"\[(?P<level>critical|error|warning|warn|info|debug|trace)\]",
    re.I,
)
LOG_SHORT_LEVEL_PATTERN = re.compile(
    r"^\s*(?:\[[^\]\r\n]{1,80}\])*?"
    r"\[(?P<level>[ewidv])\]",
    re.I,
)

UUID_TEXT_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    r"(?![0-9a-f])"
)
NON_ACTIONABLE_LOG_LEVELS = frozenset({"info", "debug", "trace", "i", "d", "v"})

SEVERITY_RANK = {
    Severity.INFO.value: 0,
    Severity.WARNING.value: 1,
    Severity.ERROR.value: 2,
    Severity.CRITICAL.value: 3,
}

SUMMARY_NUMERIC_FIELDS: Mapping[Controller, tuple[str, ...]] = {
    Controller.RCE: (
        "planned_export_kwh",
        "planned_revenue_pln",
        "maximum_export_power_kw",
        "bms_discharge_power_limit_kw",
        "ending_battery_soc",
        "data_quality_score",
        "forecast_accuracy_factor",
        "forecast_factor_used",
    ),
    Controller.RCEM: (
        "maximum_voltage_v",
        "voltage_risk_score_percent",
        "history_days",
        "history_samples",
        "headroom_shortfall_kwh",
        "planned_grid_discharge_kwh",
        "recommended_charge_limit_percent",
        "recommended_export_limit_percent",
    ),
    Controller.TARIFF: (
        "planned_grid_import_kwh",
        "planned_stored_energy_kwh",
        "planned_cost_pln",
        "automation_savings_pln",
        "estimated_savings_pln",
        "target_soc_percent",
        "ending_battery_soc_percent",
        "planning_horizon_hours",
        "charge_power_feedback_applied_factor",
        "forecast_factor_used",
    ),
}

# Histories overlap heavily between consecutive bundles.  Keep one bounded,
# deterministic set instead of accumulating every per-archive copy in RAM.
MAX_RETAINED_CONTROL_EVENTS = 100_000
MAX_RETAINED_CONTROL_EVENT_BYTES = 64 * 1024 * 1024


def classify_relevant_logs(text: str | None) -> dict[str, int]:
    """Return category counts without copying raw diagnostic log lines."""
    counts = {category: 0 for category in LOG_PATTERNS}
    if not text:
        return counts
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.casefold().startswith("no relevant"):
            continue
        level_match = next(
            (
                match
                for pattern in (
                    LOG_LONG_LEVEL_PATTERN,
                    LOG_BRACKET_LEVEL_PATTERN,
                    LOG_SHORT_LEVEL_PATTERN,
                )
                if (match := pattern.search(stripped)) is not None
            ),
            None,
        )
        if (
            level_match is not None
            and level_match.group("level").casefold()
            in NON_ACTIONABLE_LOG_LEVELS
        ):
            continue
        if any(pattern.search(stripped) for pattern in LOG_NON_FAILURE_PATTERNS):
            continue
        for category, patterns in LOG_PATTERNS.items():
            if any(pattern.search(stripped) for pattern in patterns):
                counts[category] += 1
    return counts


def _semantic_digest(
    observations: Sequence[ControllerObservation],
    events: Sequence[Mapping[str, Any]],
    *,
    archive: LoadedDiagnosticArchive,
    log_counts: Mapping[str, int],
    extraction_issues: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "observations": [
            {
                key: value
                for key, value in observation.as_dict().items()
                if key != "archive_key"
            }
            for observation in observations
        ],
        "events": [
            {
                key: value
                for key, value in event.items()
                if key not in {"archive_key"}
            }
            for event in events
        ],
        # Same-timestamp bundles are equivalent only when their diagnostic
        # capabilities, coverage and normalized logs agree as well.  Ignoring
        # these fields could discard the only archive carrying an error log or
        # a broken second config entry.
        "report_contracts": [
            {
                "report_schema_version": report.report_schema_version,
                "integration_version": report.integration_version,
                "catalog_coverage": report.catalog_coverage,
                "catalog_translation_keys": sorted(
                    str(row.get("translation_key"))
                    for row in report.catalog_entities
                    if isinstance(row, Mapping)
                    and row.get("translation_key") is not None
                ),
            }
            for report in archive.reports
        ],
        "archive_warnings": sorted(archive.warnings),
        "log_counts": dict(sorted(log_counts.items())),
        "extraction_issues": list(extraction_issues),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _parse_aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _history_capture_windows(
    archive: LoadedDiagnosticArchive,
) -> tuple[tuple[datetime, datetime], ...]:
    """Return distinct Recorder coverage windows from all config entries."""

    windows: set[tuple[datetime, datetime]] = set()
    for report in archive.reports:
        history = report.control_history
        if not isinstance(history, Mapping) or history.get("available") is False:
            continue
        start = _parse_aware_timestamp(history.get("start"))
        end = _parse_aware_timestamp(history.get("end"))
        if start is not None and end is not None and end > start:
            windows.add((start, end))
    return tuple(sorted(windows))


def _redact_uuid_strings(value: Any) -> Any:
    """Remove canonical UUID text from privacy-default analyzer outputs."""

    if isinstance(value, str):
        return UUID_TEXT_PATTERN.sub("[UUID_REDACTED]", value)
    if isinstance(value, list):
        return [_redact_uuid_strings(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_uuid_strings(item) for item in value)
    if isinstance(value, Mapping):
        return {
            _redact_uuid_strings(key): _redact_uuid_strings(child)
            for key, child in value.items()
        }
    return value


def _control_event_identity(
    event: Mapping[str, Any],
) -> tuple[str, str, str, str]:
    """Return the same stable identity used by the final event merger."""

    state = event.get("state", "")
    try:
        state_key = json.dumps(
            state,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError):
        state_key = str(state)
    return (
        str(event.get("installation_key", "")),
        str(event.get("entity_id", "")),
        str(event.get("last_updated") or event.get("last_changed") or ""),
        state_key,
    )


def _control_event_priority(
    identity: tuple[str, str, str, str],
) -> tuple[str, str, str, str]:
    """Prefer the latest events while retaining deterministic tie breakers."""

    installation_key, entity_id, timestamp, state = identity
    return timestamp, installation_key, entity_id, state


def _control_event_estimated_bytes(event: Mapping[str, Any]) -> int:
    """Conservatively bound retained event memory using serialized size."""

    try:
        serialized = json.dumps(
            event,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    except (TypeError, ValueError):
        serialized = str(event).encode("utf-8", errors="replace")
    # Retention stores both structured objects and a stable identity string.
    # Deliberately overestimate common Python-container overhead.
    return len(serialized) * 16 + 1024


def _retain_control_events(
    events: Iterable[Mapping[str, Any]],
    retained: dict[tuple[str, str, str, str], Mapping[str, Any]],
    retained_sizes: dict[tuple[str, str, str, str], int],
    priority_heap: list[
        tuple[tuple[str, str, str, str], tuple[str, str, str, str]]
    ],
    counters: dict[str, int],
) -> None:
    """Incrementally deduplicate events under one global memory bound."""

    for event in events:
        counters["candidates"] += 1
        identity = _control_event_identity(event)
        current = retained.get(identity)
        if current is not None:
            if prefer_control_event(event, current):
                estimated_bytes = _control_event_estimated_bytes(event)
                replacement_bytes = (
                    counters["retained_estimated_bytes"] - retained_sizes[identity] + estimated_bytes
                )
                if replacement_bytes > MAX_RETAINED_CONTROL_EVENT_BYTES:
                    counters["drop_operations"] += 1
                    counters["oversized_events"] += int(estimated_bytes > MAX_RETAINED_CONTROL_EVENT_BYTES)
                    continue
                retained[identity] = event
                retained_sizes[identity] = estimated_bytes
                counters["retained_estimated_bytes"] = replacement_bytes
            continue

        estimated_bytes = _control_event_estimated_bytes(event)
        if estimated_bytes > MAX_RETAINED_CONTROL_EVENT_BYTES:
            counters["drop_operations"] += 1
            counters["oversized_events"] += 1
            continue

        priority = _control_event_priority(identity)
        heap_item = (priority, identity)
        if (
            len(retained) < MAX_RETAINED_CONTROL_EVENTS
            and counters["retained_estimated_bytes"] + estimated_bytes
            <= MAX_RETAINED_CONTROL_EVENT_BYTES
        ):
            retained[identity] = event
            retained_sizes[identity] = estimated_bytes
            counters["retained_estimated_bytes"] += estimated_bytes
            heapq.heappush(priority_heap, heap_item)
            continue

        # Preview the oldest victims.  If the candidate cannot displace only
        # older events, restore the heap and leave the retained set unchanged.
        victims: list[
            tuple[tuple[str, str, str, str], tuple[str, str, str, str]]
        ] = []
        projected_count = len(retained)
        projected_bytes = counters["retained_estimated_bytes"]
        while (
            projected_count >= MAX_RETAINED_CONTROL_EVENTS
            or projected_bytes + estimated_bytes
            > MAX_RETAINED_CONTROL_EVENT_BYTES
        ):
            if not priority_heap or heap_item <= priority_heap[0]:
                for victim in victims:
                    heapq.heappush(priority_heap, victim)
                counters["drop_operations"] += 1
                break
            victim = heapq.heappop(priority_heap)
            victims.append(victim)
            evicted_identity = victim[1]
            projected_count -= 1
            projected_bytes -= retained_sizes[evicted_identity]
        else:
            for _, evicted_identity in victims:
                del retained[evicted_identity]
                del retained_sizes[evicted_identity]
            counters["retained_estimated_bytes"] = projected_bytes
            counters["drop_operations"] += len(victims)
            counters["evictions"] += len(victims)
            retained[identity] = event
            retained_sizes[identity] = estimated_bytes
            counters["retained_estimated_bytes"] += estimated_bytes
            heapq.heappush(priority_heap, heap_item)


def _warning_findings(
    archive: LoadedDiagnosticArchive,
) -> list[Finding]:
    findings: list[Finding] = []
    for warning in archive.warnings:
        if warning == "INSTALLATION_ID_MISMATCH":
            severity = Severity.ERROR
            code = "ARCHIVE_IDENTITY_MISMATCH"
            message = "Identyfikator instalacji różni się między plikami paczki."
        elif warning == "INSTALLATION_ID_INVALID":
            severity = Severity.ERROR
            code = "ARCHIVE_IDENTITY_INVALID"
            message = "Paczka zawiera niepoprawny anonimowy identyfikator instalacji."
        elif warning == "INSTALLATION_ID_MISSING":
            severity = Severity.WARNING
            code = "ARCHIVE_IDENTITY_MISSING"
            message = "Starsza paczka nie ma ID i nie może być łączona longitudinalnie."
        elif warning == "REPORT_SCHEMA_UNSUPPORTED":
            severity = Severity.ERROR
            code = "ARCHIVE_SCHEMA_UNSUPPORTED"
            message = "Część raportów używa nieobsługiwanego schematu."
        elif warning == "REPORT_COUNT_MISMATCH":
            severity = Severity.WARNING
            code = "ARCHIVE_REPORT_COUNT_MISMATCH"
            message = "Deklarowana liczba raportów różni się od zawartości ZIP."
        else:
            severity = Severity.INFO
            code = f"ARCHIVE_{warning}"
            message = f"Ostrzeżenie parsera paczki: {warning}."
        findings.append(
            Finding(
                code=code,
                severity=severity,
                message=message,
                installation_key=archive.metadata.installation_key,
                archive_key=archive.metadata.archive_key,
                controller=Controller.SYSTEM,
                observed_at=archive.metadata.generated_at,
                evidence={"assessment": "confirmed", "warning": warning},
            )
        )
    return findings


def analyze_inputs(
    inputs: Iterable[str | Path],
    *,
    recursive: bool = True,
    limits: ArchiveLimits = DEFAULT_LIMITS,
    include_source_paths: bool = False,
    include_anonymous_id: bool = False,
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Analyze archives sequentially and return a bounded normalized report."""
    paths = discover_archives(inputs, recursive=recursive, limits=limits)
    analysis_time = generated_at or datetime.now(timezone.utc)
    if analysis_time.tzinfo is None:
        raise ValueError("generated_at must be timezone-aware")

    packages: list[dict[str, Any]] = []
    source_map: list[dict[str, str]] = []
    observations: list[ControllerObservation] = []
    finding_occurrences: list[Finding] = []
    retained_events: dict[
        tuple[str, str, str, str], Mapping[str, Any]
    ] = {}
    retained_event_sizes: dict[tuple[str, str, str, str], int] = {}
    retained_event_heap: list[
        tuple[tuple[str, str, str, str], tuple[str, str, str, str]]
    ] = []
    control_event_counters = {
        "candidates": 0,
        "drop_operations": 0,
        "evictions": 0,
        "oversized_events": 0,
        "retained_estimated_bytes": 0,
    }
    log_clusters: list[dict[str, Any]] = []
    seen_hashes: dict[str, str] = {}
    seen_semantic: dict[tuple[str, str], dict[str, str]] = {}
    capture_windows_by_installation: dict[
        str, set[tuple[datetime, datetime]]
    ] = defaultdict(set)

    for source_index, path in enumerate(paths, start=1):
        input_key = f"input-{source_index:04d}"
        try:
            loaded = load_diagnostic_archive(path, limits=limits)
        except ArchiveReadError as err:
            if include_source_paths:
                source_map.append(
                    {"input_key": input_key, "source_path": str(path)}
                )
            packages.append(
                {
                    "input_key": input_key,
                    "archive_key": None,
                    "installation_key": None,
                    "status": ArchiveStatus.REJECTED.value,
                    "error_code": err.code,
                    "error_message": err.message,
                }
            )
            finding_occurrences.append(
                Finding(
                    code="ARCHIVE_CORRUPT",
                    severity=Severity.ERROR,
                    message="Paczka została odrzucona jako uszkodzona lub niebezpieczna.",
                    controller=Controller.SYSTEM,
                    evidence={
                        "assessment": "confirmed",
                        "error_code": err.code,
                        "input_key": input_key,
                    },
                    recommendation="Wygeneruj paczkę ponownie; nie rozpakowuj jej ręcznie.",
                )
            )
            continue

        metadata = loaded.metadata
        input_key = metadata.archive_key
        if include_source_paths:
            source_map.append(
                {"input_key": input_key, "source_path": str(path)}
            )
        if metadata.content_sha256 in seen_hashes:
            packages.append(
                _package_row(
                    loaded,
                    input_key,
                    ArchiveStatus.DUPLICATE,
                    duplicate_of=seen_hashes[metadata.content_sha256],
                    include_anonymous_id=include_anonymous_id,
                )
            )
            continue
        seen_hashes[metadata.content_sha256] = metadata.archive_key

        supported_reports = tuple(
            report
            for report in loaded.reports
            if report.report_schema_version in SUPPORTED_REPORT_SCHEMA_VERSIONS
        )
        analysis_archive = (
            loaded
            if len(supported_reports) == len(loaded.reports)
            else replace(loaded, reports=supported_reports)
        )
        warning_findings = _warning_findings(loaded)
        if not supported_reports:
            package_row = _package_row(
                loaded,
                input_key,
                ArchiveStatus.REJECTED,
                include_anonymous_id=include_anonymous_id,
            )
            package_row.update(
                {
                    "error_code": "NO_SUPPORTED_REPORTS",
                    "error_message": (
                        "Archive contains no supported diagnostic report schema"
                    ),
                }
            )
            packages.append(package_row)
            finding_occurrences.extend(warning_findings)
            continue

        extracted, context, archive_events, extraction_issues = (
            extract_archive_evidence(analysis_archive)
        )
        log_counts = classify_relevant_logs(loaded.relevant_log_text)
        semantic_key = (
            metadata.installation_key,
            metadata.generated_at.isoformat() if metadata.generated_at else "",
        )
        semantic_hash = _semantic_digest(
            extracted,
            archive_events,
            archive=analysis_archive,
            log_counts=log_counts,
            extraction_issues=extraction_issues,
        )
        prior_variants = seen_semantic.setdefault(semantic_key, {})
        duplicate_of = prior_variants.get(semantic_hash)
        if duplicate_of is not None:
            packages.append(
                _package_row(
                    loaded,
                    input_key,
                    ArchiveStatus.DUPLICATE,
                    duplicate_of=duplicate_of,
                    include_anonymous_id=include_anonymous_id,
                )
            )
            continue
        if prior_variants:
            finding_occurrences.append(
                Finding(
                    code="ARCHIVE_DUPLICATE_TIMESTAMP_CONFLICT",
                    severity=Severity.ERROR,
                    message="Dwie paczki mają ten sam czas instalacji, ale różną treść.",
                    installation_key=metadata.installation_key,
                    archive_key=metadata.archive_key,
                    controller=Controller.SYSTEM,
                    observed_at=metadata.generated_at,
                    evidence={
                        "assessment": "confirmed",
                        "other_archive_key": sorted(prior_variants.values())[0],
                    },
                    recommendation="Sprawdź zegar HA i kolejność eksportów.",
                )
            )
        prior_variants[semantic_hash] = metadata.archive_key

        capture_windows_by_installation[metadata.installation_key].update(
            _history_capture_windows(analysis_archive)
        )

        if any(log_counts.values()):
            log_clusters.append(
                {
                    "archive_key": metadata.archive_key,
                    "installation_key": metadata.installation_key,
                    **log_counts,
                }
            )
        archive_findings = [
            *warning_findings,
            *evaluate_archive(
                analysis_archive,
                extracted,
                context,
                extraction_issues,
                log_counts,
            ),
        ]
        observations.extend(extracted)
        _retain_control_events(
            archive_events,
            retained_events,
            retained_event_sizes,
            retained_event_heap,
            control_event_counters,
        )
        finding_occurrences.extend(archive_findings)
        status = (
            ArchiveStatus.PARTIAL
            if loaded.warnings or len(supported_reports) != len(loaded.reports)
            else ArchiveStatus.ACCEPTED
        )
        packages.append(
            _package_row(
                loaded,
                input_key,
                status,
                include_anonymous_id=include_anonymous_id,
            )
        )

    finding_occurrences.extend(evaluate_longitudinal(observations))
    merged_events = merge_events(retained_events.values())
    control_events_truncated = control_event_counters["drop_operations"] > 0
    control_history_metrics, control_history_findings = analyze_control_history(
        merged_events,
        {
            installation_key: tuple(sorted(windows))
            for installation_key, windows in capture_windows_by_installation.items()
        },
        input_truncated=control_events_truncated,
    )
    finding_occurrences.extend(control_history_findings)
    if control_events_truncated:
        finding_occurrences.append(
            Finding(
                code="ANALYZER_CONTROL_EVENTS_TRUNCATED",
                severity=Severity.WARNING,
                message=(
                    "Timeline sterowania przekroczył globalny limit analizatora; "
                    "zachowano deterministycznie najnowsze zdarzenia."
                ),
                controller=Controller.SYSTEM,
                evidence={
                    "assessment": "confirmed",
                    "retention_limit": MAX_RETAINED_CONTROL_EVENTS,
                    "retention_bytes_limit": (
                        MAX_RETAINED_CONTROL_EVENT_BYTES
                    ),
                    "candidate_events": control_event_counters["candidates"],
                    "retained_unique_events": len(merged_events),
                    "drop_operations": control_event_counters[
                        "drop_operations"
                    ],
                    "estimated_retained_bytes": control_event_counters[
                        "retained_estimated_bytes"
                    ],
                },
                recommendation=(
                    "Ogranicz zakres wejściowy albo analizuj krótsze serie, jeśli "
                    "potrzebny jest pełny surowy timeline."
                ),
            )
        )
    observations.sort(key=_observation_sort_key)
    packages.sort(key=lambda item: (str(item.get("archive_key")), item["input_key"]))
    log_clusters.sort(key=lambda item: str(item["archive_key"]))
    aggregated_findings = _aggregate_findings(finding_occurrences)
    installations = _installation_summaries(
        packages,
        observations,
        aggregated_findings,
        control_history_metrics,
    )
    cohort = _cohort_summary(packages, installations, aggregated_findings, observations)
    rejected = sum(item["status"] == ArchiveStatus.REJECTED.value for item in packages)
    duplicates = sum(item["status"] == ArchiveStatus.DUPLICATE.value for item in packages)
    limitations = list(LIMITATIONS)
    if control_events_truncated:
        limitations.append(
            {
                "code": "CONTROL_EVENT_RETENTION_LIMIT_REACHED",
                "message": (
                    "Timeline został ograniczony przez budżet "
                    f"{MAX_RETAINED_CONTROL_EVENTS} zdarzeń / "
                    f"{MAX_RETAINED_CONTROL_EVENT_BYTES} estymowanych bajtów; "
                    "zachowano najnowszy możliwy podzbiór."
                ),
            }
        )
    result = {
        "analysis_schema_version": ANALYSIS_SCHEMA_VERSION,
        "rule_set_version": RULE_SET_VERSION,
        "analyzer_version": ANALYZER_VERSION,
        "generated_at": analysis_time.astimezone(timezone.utc).isoformat(),
        "offline_analysis": True,
        "privacy": {
            "full_anonymous_id_included": include_anonymous_id,
            "source_paths_included": include_source_paths,
            "raw_logs_included": False,
        },
        "limitations": limitations,
        "totals": {
            "discovered_archives": len(paths),
            "accepted_or_partial_archives": len(packages) - rejected - duplicates,
            "rejected_archives": rejected,
            "duplicate_archives": duplicates,
            "installations": len(installations),
            "controller_observations": len(observations),
            "deduplicated_control_events": len(merged_events),
            "control_history_metric_rows": len(control_history_metrics),
            "control_event_candidates": control_event_counters["candidates"],
            "control_event_drop_operations": control_event_counters[
                "drop_operations"
            ],
            "control_events_truncated": control_events_truncated,
            "finding_occurrences": len(finding_occurrences),
            "finding_groups": len(aggregated_findings),
        },
        "cohort": cohort,
        "data_limits": {
            "control_event_retention_limit": MAX_RETAINED_CONTROL_EVENTS,
            "control_event_retention_bytes_limit": (
                MAX_RETAINED_CONTROL_EVENT_BYTES
            ),
            "control_event_candidates": control_event_counters["candidates"],
            "retained_unique_control_events": len(merged_events),
            "retained_control_event_estimated_bytes": control_event_counters[
                "retained_estimated_bytes"
            ],
            "control_event_drop_operations": control_event_counters[
                "drop_operations"
            ],
            "control_event_evictions": control_event_counters["evictions"],
            "oversized_control_events_dropped": control_event_counters[
                "oversized_events"
            ],
            "control_events_truncated": control_events_truncated,
            "retention_policy": "latest_by_event_timestamp_with_stable_ties",
        },
        "packages": packages,
        "installations": installations,
        "findings": aggregated_findings,
        "finding_occurrences": [
            finding.as_dict()
            for finding in sorted(finding_occurrences, key=_finding_sort_key)
        ],
        "observations": [observation.as_dict() for observation in observations],
        "control_events": list(merged_events),
        "control_history_metrics": list(control_history_metrics),
        "log_clusters": log_clusters,
        **({"source_map": source_map} if include_source_paths else {}),
    }
    redacted = _redact_uuid_strings(result)
    if include_anonymous_id:
        # The opt-in exposes only the validated installation identity already
        # placed on package rows.  Every unrelated UUID in planner evidence,
        # paths or diagnostics remains redacted.
        raw_packages = result.get("packages", [])
        safe_packages = redacted.get("packages", [])
        if isinstance(raw_packages, list) and isinstance(safe_packages, list):
            for raw_row, safe_row in zip(raw_packages, safe_packages, strict=True):
                if not isinstance(raw_row, Mapping) or not isinstance(
                    safe_row, dict
                ):
                    continue
                anonymous_id = raw_row.get("anonymous_installation_id")
                if isinstance(anonymous_id, str):
                    safe_row["anonymous_installation_id"] = anonymous_id
    return redacted


def _package_row(
    archive: LoadedDiagnosticArchive,
    input_key: str,
    status: ArchiveStatus,
    *,
    duplicate_of: str | None = None,
    include_anonymous_id: bool = False,
) -> dict[str, Any]:
    metadata = archive.metadata
    row: dict[str, Any] = {
        "input_key": input_key,
        "archive_key": metadata.archive_key,
        "content_sha256": metadata.content_sha256,
        "installation_key": metadata.installation_key,
        "installation_id_schema_version": metadata.installation_id_schema_version,
        "generated_at": _iso(metadata.generated_at),
        "home_assistant_version": metadata.home_assistant_version,
        "report_schema_versions": list(metadata.report_schema_versions),
        "integration_versions": sorted(
            {
                report.integration_version
                for report in archive.reports
                if report.integration_version is not None
            }
        ),
        "report_count": metadata.actual_report_count,
        "source_size_bytes": metadata.source_size_bytes,
        "status": status.value,
        "warnings": list(metadata_value for metadata_value in archive.warnings),
    }
    if duplicate_of is not None:
        row["duplicate_of"] = duplicate_of
    if include_anonymous_id:
        row["anonymous_installation_id"] = metadata.anonymous_installation_id
    return row


def _aggregate_findings(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Finding]] = defaultdict(list)
    for finding in findings:
        key = (
            finding.installation_key or "unlinked",
            finding.controller.value if finding.controller else "system",
            finding.code,
        )
        groups[key].append(finding)
    result: list[dict[str, Any]] = []
    for key in sorted(groups):
        items = sorted(groups[key], key=_finding_sort_key)
        strongest = max(items, key=lambda item: SEVERITY_RANK[item.severity.value])
        times = [item.observed_at for item in items if item.observed_at is not None]
        archives = sorted({item.archive_key for item in items if item.archive_key})
        result.append(
            {
                "installation_key": key[0],
                "controller": key[1],
                "rule_id": key[2],
                "severity": strongest.severity.value,
                "confidence": strongest.confidence.value,
                "message": strongest.message,
                "recommendation": strongest.recommendation,
                "occurrence_count": sum(item.occurrences for item in items),
                "affected_archive_count": len(archives),
                "first_seen": _iso(min(times)) if times else None,
                "last_seen": _iso(max(times)) if times else None,
                "sample_evidence": strongest.evidence,
            }
        )
    return sorted(
        result,
        key=lambda item: (
            -SEVERITY_RANK[item["severity"]],
            item["installation_key"],
            item["controller"],
            item["rule_id"],
        ),
    )


def _installation_summaries(
    packages: Sequence[Mapping[str, Any]],
    observations: Sequence[ControllerObservation],
    findings: Sequence[Mapping[str, Any]],
    control_history_metrics: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    package_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    observation_groups: dict[str, list[ControllerObservation]] = defaultdict(list)
    finding_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    response_metric_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for package in packages:
        key = package.get("installation_key")
        if isinstance(key, str) and package.get("status") not in {
            ArchiveStatus.REJECTED.value,
            ArchiveStatus.DUPLICATE.value,
        }:
            package_groups[key].append(package)
    for observation in observations:
        observation_groups[observation.installation_key].append(observation)
    for finding in findings:
        key = finding.get("installation_key")
        if isinstance(key, str):
            finding_groups[key].append(finding)
    for metric in control_history_metrics:
        key = metric.get("installation_key")
        if (
            isinstance(key, str)
            and metric.get("family") == "parallel_aggregate_response"
        ):
            response_metric_groups[key].append(metric)

    result: list[dict[str, Any]] = []
    for installation_key in sorted(package_groups):
        installation_packages = package_groups[installation_key]
        installation_observations = observation_groups.get(installation_key, [])
        installation_findings = finding_groups.get(installation_key, [])
        response_metrics = response_metric_groups.get(installation_key, [])
        response_finding_codes = {
            str(finding.get("rule_id")) for finding in installation_findings
        }
        if any(
            code.endswith("PARALLEL_AGGREGATE_RESPONSE_STALE")
            for code in response_finding_codes
        ):
            physical_response_verdict = "aggregate_response_stale"
        elif any(
            "PARALLEL_AGGREGATE_RESPONSE_PENDING_TIMEOUT" in code
            for code in response_finding_codes
        ):
            physical_response_verdict = "aggregate_response_pending_timeout"
        elif any(
            code.endswith("PARALLEL_AGGREGATE_RESPONSE_NOT_CONFIRMED")
            for code in response_finding_codes
        ):
            physical_response_verdict = "aggregate_response_not_confirmed"
        elif any(
            code.endswith("PARALLEL_AGGREGATE_RESPONSE_NOT_EVALUABLE")
            for code in response_finding_codes
        ):
            physical_response_verdict = "aggregate_response_not_evaluable"
        elif any(
            code.endswith("PARALLEL_AGGREGATE_RESPONSE_CONFIRMED")
            for code in response_finding_codes
        ):
            physical_response_verdict = "aggregate_response_confirmed"
        elif response_metrics:
            physical_response_verdict = "aggregate_response_pending"
        else:
            physical_response_verdict = (
                "not_evaluable_from_single_capture"
                if len(installation_packages) < 2
                else "requires_time_aligned_active_samples"
            )
        capture_times = [
            _parse_iso(package.get("generated_at"))
            for package in installation_packages
        ]
        capture_times = [item for item in capture_times if item is not None]
        severity_counts = Counter(
            str(finding.get("severity")) for finding in installation_findings
        )
        controller_summary: dict[str, Any] = {}
        for controller in (Controller.RCE, Controller.RCEM, Controller.TARIFF):
            items = [
                item
                for item in installation_observations
                if item.controller is controller
            ]
            controller_summary[controller.value] = _controller_summary(
                controller,
                items,
                len(installation_packages),
            )
        confidence = (
            "high"
            if len(installation_packages) >= 3
            else "medium"
            if len(installation_packages) == 2
            else "low"
        )
        result.append(
            {
                "installation_key": installation_key,
                "package_count": len(installation_packages),
                "first_capture": _iso(min(capture_times)) if capture_times else None,
                "last_capture": _iso(max(capture_times)) if capture_times else None,
                "integration_versions": sorted(
                    {
                        str(version)
                        for package in installation_packages
                        for version in package.get("integration_versions", [])
                    }
                ),
                "longitudinal_confidence": confidence,
                "coverage": {
                    "planner_history_attributes_available": False,
                    "fast_physical_telemetry_history_available": bool(
                        response_metrics
                    ),
                    "physical_response_verdict": physical_response_verdict,
                },
                "severity_counts": dict(sorted(severity_counts.items())),
                "controller_summary": controller_summary,
            }
        )
    return result


def _controller_summary(
    controller: Controller,
    observations: Sequence[ControllerObservation],
    package_count: int,
) -> dict[str, Any]:
    status_counts = Counter(
        observation.status_code or "unknown" for observation in observations
    )
    numeric: dict[str, Any] = {}
    for field in SUMMARY_NUMERIC_FIELDS[controller]:
        values = [
            float(observation.metrics[field])
            for observation in observations
            if field in observation.metrics
            and isinstance(observation.metrics[field], (int, float))
            and not isinstance(observation.metrics[field], bool)
            and math_isfinite(observation.metrics[field])
        ]
        if values:
            numeric[field] = {
                "minimum": min(values),
                "mean": round(sum(values) / len(values), 6),
                "maximum": max(values),
                "latest": values[-1],
                "sample_count": len(values),
            }
    return {
        "observation_count": len(observations),
        "capture_coverage_percent": (
            round(len(observations) / package_count * 100.0, 1)
            if package_count
            else 0.0
        ),
        "status_counts": dict(sorted(status_counts.items())),
        "result_current_false_count": sum(
            item.result_current is False for item in observations
        ),
        "active_count": sum(item.active is True for item in observations),
        "numeric_metrics": numeric,
    }


def _cohort_summary(
    packages: Sequence[Mapping[str, Any]],
    installations: Sequence[Mapping[str, Any]],
    findings: Sequence[Mapping[str, Any]],
    observations: Sequence[ControllerObservation],
) -> dict[str, Any]:
    analyzed_packages = {
        str(package["archive_key"])
        for package in packages
        if package.get("archive_key")
        and package.get("status")
        in {ArchiveStatus.ACCEPTED.value, ArchiveStatus.PARTIAL.value}
    }
    installation_keys = {
        str(installation["installation_key"]) for installation in installations
    }
    rule_package_counts: Counter[str] = Counter()
    rule_installations: dict[str, set[str]] = defaultdict(set)
    for finding in findings:
        rule_id = str(finding["rule_id"])
        installation = str(finding["installation_key"])
        rule_installations[rule_id].add(installation)
    rule_prevalence = []
    for finding in findings:
        rule_id = str(finding["rule_id"])
        rule_package_counts[rule_id] += int(
            finding.get("affected_archive_count", 0)
        )
    for rule_id in sorted(set(rule_installations) | set(rule_package_counts)):
        package_hits = min(rule_package_counts[rule_id], len(analyzed_packages))
        installation_hits = len(rule_installations[rule_id] & installation_keys)
        rule_prevalence.append(
            {
                "rule_id": rule_id,
                "affected_package_count": package_hits,
                "package_weighted_percent": (
                    round(package_hits / len(analyzed_packages) * 100.0, 2)
                    if analyzed_packages
                    else 0.0
                ),
                "affected_installation_count": installation_hits,
                "installation_weighted_percent": (
                    round(installation_hits / len(installation_keys) * 100.0, 2)
                    if installation_keys
                    else 0.0
                ),
            }
        )
    status_counts: dict[str, Counter[str]] = {
        controller.value: Counter() for controller in Controller if controller is not Controller.SYSTEM
    }
    for observation in observations:
        status_counts[observation.controller.value][
            observation.status_code or "unknown"
        ] += 1
    return {
        "weighting_note": (
            "Package-weighted and installation-weighted rates are reported "
            "separately because incident bundles are not a random sample."
        ),
        "rule_prevalence": rule_prevalence,
        "controller_status_counts": {
            key: dict(sorted(value.items()))
            for key, value in status_counts.items()
        },
    }


def _observation_sort_key(observation: ControllerObservation) -> tuple[str, str, str, str]:
    return (
        observation.installation_key,
        _iso(observation.observed_at) or "",
        observation.controller.value,
        observation.archive_key,
    )


def _finding_sort_key(finding: Finding) -> tuple[str, str, str, str]:
    return (
        _iso(finding.observed_at) or "",
        finding.installation_key or "",
        finding.controller.value if finding.controller else "system",
        finding.code,
    )


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def math_isfinite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False
