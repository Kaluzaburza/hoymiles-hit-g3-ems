"""Deterministic JSON, CSV, Markdown and HTML outputs for batch analysis."""

from __future__ import annotations

import csv
from hashlib import sha256
from html import escape
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping, Sequence


OUTPUT_FILENAMES = (
    "summary.json",
    "report.md",
    "report.html",
    "packages.csv",
    "installations.csv",
    "findings.csv",
    "rce_observations.csv",
    "rcem_observations.csv",
    "tariff_observations.csv",
    "control_events.csv",
    "control_runs.csv",
    "log_clusters.csv",
)

# ``source_map.csv`` is deliberately opt-in, but it is still an analyzer-owned
# artifact.  Keeping it in the managed set ensures that a later ``--force`` run
# without source paths removes the privacy-sensitive stale copy.
OPTIONAL_OUTPUT_FILENAMES = ("source_map.csv",)
KNOWN_OUTPUT_FILENAMES = OUTPUT_FILENAMES + OPTIONAL_OUTPUT_FILENAMES
LEGACY_TEMP_FILENAMES = tuple(f".{name}.tmp" for name in KNOWN_OUTPUT_FILENAMES)
MANAGED_ARTIFACT_FILENAMES = KNOWN_OUTPUT_FILENAMES + LEGACY_TEMP_FILENAMES
OUTPUT_LOCK_DIRECTORY = ".hoymiles-diagnostics-analysis.lock"
STAGING_DIRECTORY_PREFIX = ".hoymiles-diagnostics-analysis-stage-"

SEVERITY_ORDER = {"critical": 0, "error": 1, "warning": 2, "info": 3}


def _safe_csv_value(value: Any) -> str | int | float:
    if value is None:
        return ""
    if type(value) is bool:
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    else:
        text = str(value)
    text = text.replace("\x00", "").replace("\r", " ").replace("\n", " ")
    if text.startswith(("=", "+", "-", "@", "\t")):
        text = "'" + text
    return text


def _csv_bytes(rows: Sequence[Mapping[str, Any]], columns: Sequence[str]) -> bytes:
    output = StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(columns), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: _safe_csv_value(row.get(column)) for column in columns})
    return output.getvalue().encode("utf-8-sig")


def _flatten_observation(observation: Mapping[str, Any]) -> dict[str, Any]:
    row = {
        key: value
        for key, value in observation.items()
        if key not in {"metrics", "flags", "freshness", "ages_seconds", "details", "coverage"}
    }
    for group in ("metrics", "flags", "freshness", "ages_seconds", "coverage"):
        values = observation.get(group)
        if isinstance(values, Mapping):
            for key, value in values.items():
                row[f"{group}.{key}"] = value
    details = observation.get("details")
    if isinstance(details, Mapping):
        for key in (
            "source_confidence",
            "missing_entities_count",
            "planned_slots_count",
            "risk_windows_count",
            "data_quality_issues",
        ):
            if key in details:
                row[f"details.{key}"] = details[key]
    return row


def _observation_columns(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    preferred = [
        "installation_key",
        "archive_key",
        "observed_at",
        "controller",
        "entity_id",
        "state",
        "status_code",
        "action",
        "result_current",
        "planned",
        "enabled",
        "active",
        "owner_code",
        "suppression_reason",
        "continue_reason",
        "block_reason",
    ]
    remaining = sorted({key for row in rows for key in row} - set(preferred))
    return [key for key in preferred if any(key in row for row in rows)] + remaining


def build_output_payloads(summary: Mapping[str, Any]) -> dict[str, bytes]:
    """Return every report file in memory before atomically publishing it."""
    payloads: dict[str, bytes] = {
        "summary.json": (
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8"),
        "report.md": _markdown_report(summary).encode("utf-8"),
        "report.html": _html_report(summary).encode("utf-8"),
    }

    packages = _mapping_rows(summary.get("packages"))
    payloads["packages.csv"] = _csv_bytes(
        packages,
        (
            "archive_key",
            "installation_key",
            "status",
            "generated_at",
            "home_assistant_version",
            "integration_versions",
            "report_schema_versions",
            "report_count",
            "source_size_bytes",
            "warnings",
            "duplicate_of",
            "error_code",
            "error_message",
        ),
    )

    installations = []
    for item in _mapping_rows(summary.get("installations")):
        row = dict(item)
        row["severity_counts"] = item.get("severity_counts")
        for controller, controller_summary in (
            item.get("controller_summary", {}).items()
            if isinstance(item.get("controller_summary"), Mapping)
            else []
        ):
            if isinstance(controller_summary, Mapping):
                row[f"{controller}.observation_count"] = controller_summary.get(
                    "observation_count"
                )
                row[f"{controller}.capture_coverage_percent"] = controller_summary.get(
                    "capture_coverage_percent"
                )
                row[f"{controller}.status_counts"] = controller_summary.get(
                    "status_counts"
                )
        installations.append(row)
    payloads["installations.csv"] = _csv_bytes(
        installations,
        (
            "installation_key",
            "package_count",
            "first_capture",
            "last_capture",
            "integration_versions",
            "longitudinal_confidence",
            "severity_counts",
            "rce.observation_count",
            "rce.capture_coverage_percent",
            "rce.status_counts",
            "rcem.observation_count",
            "rcem.capture_coverage_percent",
            "rcem.status_counts",
            "tariff.observation_count",
            "tariff.capture_coverage_percent",
            "tariff.status_counts",
        ),
    )

    findings = _mapping_rows(summary.get("findings"))
    payloads["findings.csv"] = _csv_bytes(
        findings,
        (
            "severity",
            "rule_id",
            "controller",
            "installation_key",
            "confidence",
            "occurrence_count",
            "affected_archive_count",
            "first_seen",
            "last_seen",
            "message",
            "recommendation",
            "sample_evidence",
        ),
    )

    observations = [_flatten_observation(row) for row in _mapping_rows(summary.get("observations"))]
    for controller in ("rce", "rcem", "tariff"):
        rows = [row for row in observations if row.get("controller") == controller]
        payloads[f"{controller}_observations.csv"] = _csv_bytes(
            rows,
            _observation_columns(rows),
        )

    events = _mapping_rows(summary.get("control_events"))
    payloads["control_events.csv"] = _csv_bytes(
        events,
        (
            "installation_key",
            "entity_id",
            "state",
            "last_changed",
            "last_updated",
            "archive_key",
            "event_schema_version",
            "attribute_profile",
            "attributes_available",
            "attributes",
        ),
    )
    control_runs = _mapping_rows(summary.get("control_history_metrics"))
    payloads["control_runs.csv"] = _csv_bytes(
        control_runs,
        (
            "installation_key",
            "controller",
            "family",
            "entity_id",
            "event_count",
            "starts",
            "stops",
            "active_minutes",
            "longest_active_minutes",
            "short_runs",
            "short_run_threshold_seconds",
            "transitions",
            "max_toggles_per_hour",
            "open",
            "first_observed_at",
            "last_observed_at",
            "capture_end",
            "ambiguous_event_count",
            "evidence_truncated",
        ),
    )
    logs = _mapping_rows(summary.get("log_clusters"))
    payloads["log_clusters.csv"] = _csv_bytes(
        logs,
        (
            "installation_key",
            "archive_key",
            "optimizer_exception",
            "modbus_communication",
            "readback_failure",
            "rollback_failure",
            "recorder_history",
            "asset_failure",
        ),
    )
    if "source_map" in summary:
        payloads["source_map.csv"] = _csv_bytes(
            _mapping_rows(summary.get("source_map")),
            ("input_key", "source_path"),
        )
    return payloads


def write_analysis_outputs(
    summary: Mapping[str, Any],
    output_directory: str | Path,
    *,
    force: bool = False,
) -> tuple[Mapping[str, Any], ...]:
    """Transactionally publish the analyzer-owned output set.

    Payloads are fully materialized and fsynced in a private staging directory
    before any current output is moved.  A caught publishing failure rolls the
    complete known set back; unrelated files in the destination are never
    touched.
    """
    destination = Path(output_directory)
    destination.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink() or not destination.is_dir():
        raise OSError(
            f"Output destination must be a real directory: {destination}"
        )
    payloads = build_output_payloads(summary)
    unknown_payloads = set(payloads) - set(KNOWN_OUTPUT_FILENAMES)
    if unknown_payloads:
        raise ValueError(
            "Refusing to publish unknown output files: "
            + ", ".join(sorted(unknown_payloads))
        )

    lock_directory = destination / OUTPUT_LOCK_DIRECTORY
    try:
        lock_directory.mkdir()
    except FileExistsError as err:
        raise FileExistsError(
            f"Another output transaction is active: {lock_directory}"
        ) from err

    staging_root: Path | None = None
    transaction_succeeded = False
    preserve_staging = False
    try:
        # Inspect collisions only after acquiring the cooperative lock.  This
        # prevents a second analyzer process from making the preflight result
        # stale between the check and publication.
        managed_existing = [
            name
            for name in MANAGED_ARTIFACT_FILENAMES
            if _path_exists(destination / name)
        ]
        for name in managed_existing:
            _require_regular_output(destination / name)
        if managed_existing and not force:
            raise FileExistsError(
                "Output files already exist: "
                + ", ".join(sorted(managed_existing))
            )

        staging_root = Path(
            tempfile.mkdtemp(prefix=STAGING_DIRECTORY_PREFIX, dir=destination)
        )
        staged_new = staging_root / "new"
        staged_backup = staging_root / "backup"
        staged_new.mkdir()
        staged_backup.mkdir()

        manifest: list[Mapping[str, Any]] = []
        for name in sorted(payloads):
            data = payloads[name]
            _write_staged_file(staged_new / name, data)
            manifest.append(
                {
                    "name": name,
                    "size_bytes": len(data),
                    "sha256": sha256(data).hexdigest(),
                }
            )

        backed_up: list[str] = []
        published: list[str] = []
        try:
            # Back up the entire analyzer-owned set, including an opt-in
            # source map and fixed-name temporary files from older releases.
            for name in sorted(managed_existing):
                (destination / name).replace(staged_backup / name)
                backed_up.append(name)
            for name in sorted(payloads):
                (staged_new / name).replace(destination / name)
                published.append(name)
        except BaseException as publish_error:
            rollback_errors: list[str] = []
            for name in reversed(published):
                try:
                    _remove_regular_file(destination / name)
                except OSError as err:
                    rollback_errors.append(f"remove {name}: {err}")
            for name in reversed(backed_up):
                backup = staged_backup / name
                try:
                    if _path_exists(destination / name):
                        _remove_regular_file(destination / name)
                    backup.replace(destination / name)
                except OSError as err:
                    rollback_errors.append(f"restore {name}: {err}")
            if rollback_errors:
                preserve_staging = True
                raise OSError(
                    "Output transaction failed and rollback was incomplete: "
                    + "; ".join(rollback_errors)
                    + f". Recovery files retained in {staging_root}"
                ) from publish_error
            raise

        transaction_succeeded = True
        return tuple(manifest)
    finally:
        cleanup_errors: list[str] = []
        if staging_root is not None and not preserve_staging:
            try:
                _cleanup_staging_directory(staging_root)
            except OSError as err:
                cleanup_errors.append(str(err))
        try:
            lock_directory.rmdir()
        except OSError as err:
            cleanup_errors.append(str(err))
        if cleanup_errors and transaction_succeeded:
            raise OSError(
                "Outputs were published, but transaction cleanup failed: "
                + "; ".join(cleanup_errors)
            )


def _path_exists(path: Path) -> bool:
    """Return true for regular paths and broken symlinks."""

    return path.exists() or path.is_symlink()


def _require_regular_output(path: Path) -> None:
    """Reject symlinks and non-files before starting an output transaction."""

    if path.is_symlink() or not path.is_file():
        raise FileExistsError(
            f"Analyzer output target is not a regular file: {path}"
        )


def _write_staged_file(path: Path, data: bytes) -> None:
    """Write one new staging file durably without following an old path."""

    with path.open("xb") as output:
        output.write(data)
        output.flush()
        os.fsync(output.fileno())


def _remove_regular_file(path: Path) -> None:
    if not _path_exists(path):
        return
    if path.is_symlink() or not path.is_file():
        raise OSError(f"Refusing to remove non-regular output path: {path}")
    path.unlink()


def _cleanup_staging_directory(staging_root: Path) -> None:
    """Remove only files created or backed up by this transaction."""

    if not staging_root.exists():
        return
    for directory_name in ("new", "backup"):
        directory = staging_root / directory_name
        if not directory.exists():
            continue
        for child in directory.iterdir():
            _remove_regular_file(child)
        directory.rmdir()
    staging_root.rmdir()


def _mapping_rows(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _md(value: Any) -> str:
    return str(_safe_csv_value(value)).replace("|", "\\|")


def _markdown_report(summary: Mapping[str, Any]) -> str:
    totals = summary.get("totals") if isinstance(summary.get("totals"), Mapping) else {}
    findings = sorted(
        _mapping_rows(summary.get("findings")),
        key=lambda item: (
            SEVERITY_ORDER.get(str(item.get("severity")), 99),
            str(item.get("installation_key")),
            str(item.get("rule_id")),
        ),
    )
    installations = _mapping_rows(summary.get("installations"))
    control_runs = _mapping_rows(summary.get("control_history_metrics"))
    lines = [
        "# Hoymiles diagnostics — batch analysis",
        "",
        f"Generated: `{_md(summary.get('generated_at'))}`",
        "",
        "## Overview",
        "",
        f"- Archives discovered: **{totals.get('discovered_archives', 0)}**",
        f"- Accepted/partial: **{totals.get('accepted_or_partial_archives', 0)}**",
        f"- Rejected: **{totals.get('rejected_archives', 0)}**",
        f"- Duplicate: **{totals.get('duplicate_archives', 0)}**",
        f"- Installations: **{totals.get('installations', 0)}**",
        f"- Finding groups: **{totals.get('finding_groups', 0)}**",
        "",
        "## Findings",
        "",
        "| Severity | Rule | Installation | Controller | Occurrences | Message |",
        "|---|---|---|---|---:|---|",
    ]
    for item in findings:
        lines.append(
            "| "
            + " | ".join(
                _md(item.get(key))
                for key in (
                    "severity",
                    "rule_id",
                    "installation_key",
                    "controller",
                    "occurrence_count",
                    "message",
                )
            )
            + " |"
        )
    if not findings:
        lines.append("| info | NO_CONFIRMED_FINDINGS | — | system | 0 | No confirmed issue in supplied snapshots. |")
    lines.extend(
        [
            "",
            "## Installations",
            "",
            "| Installation | Packages | First | Last | Confidence | Severity counts |",
            "|---|---:|---|---|---|---|",
        ]
    )
    for item in installations:
        lines.append(
            "| "
            + " | ".join(
                _md(item.get(key))
                for key in (
                    "installation_key",
                    "package_count",
                    "first_capture",
                    "last_capture",
                    "longitudinal_confidence",
                    "severity_counts",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Control-run history",
            "",
            "| Installation | Controller | Helper | Starts | Active min | Longest min | Short runs | Max toggles/h | Open |",
            "|---|---|---|---:|---:|---:|---:|---:|---|",
        ]
    )
    for item in control_runs:
        lines.append(
            "| "
            + " | ".join(
                _md(item.get(key))
                for key in (
                    "installation_key",
                    "controller",
                    "entity_id",
                    "starts",
                    "active_minutes",
                    "longest_active_minutes",
                    "short_runs",
                    "max_toggles_per_hour",
                    "open",
                )
            )
            + " |"
        )
    if not control_runs:
        lines.append("| — | system | — | 0 | 0 | 0 | 0 | 0 | — |")
    lines.extend(
        [
            "",
            "## Interpretation limits",
            "",
        ]
    )
    for limitation in _mapping_rows(summary.get("limitations")):
        lines.append(f"- **{_md(limitation.get('code'))}:** {_md(limitation.get('message'))}")
    lines.extend(
        [
            "",
            "The report is offline and does not include raw logs, source paths or the full anonymous UUID by default.",
            "",
        ]
    )
    return "\n".join(lines)


def _html_report(summary: Mapping[str, Any]) -> str:
    totals = summary.get("totals") if isinstance(summary.get("totals"), Mapping) else {}
    findings = _mapping_rows(summary.get("findings"))
    installations = _mapping_rows(summary.get("installations"))
    control_runs = _mapping_rows(summary.get("control_history_metrics"))
    finding_rows = "".join(
        "<tr class='sev-{severity}'><td>{severity}</td><td><code>{rule}</code></td>"
        "<td>{installation}</td><td>{controller}</td><td>{count}</td>"
        "<td>{message}</td><td>{recommendation}</td></tr>".format(
            severity=escape(str(item.get("severity", ""))),
            rule=escape(str(item.get("rule_id", ""))),
            installation=escape(str(item.get("installation_key", ""))),
            controller=escape(str(item.get("controller", ""))),
            count=escape(str(item.get("occurrence_count", ""))),
            message=escape(str(item.get("message", ""))),
            recommendation=escape(str(item.get("recommendation", ""))),
        )
        for item in findings
    ) or "<tr><td colspan='7'>No confirmed findings in supplied snapshots.</td></tr>"
    installation_rows = "".join(
        "<tr><td><code>{key}</code></td><td>{count}</td><td>{first}</td>"
        "<td>{last}</td><td>{confidence}</td><td>{severity}</td></tr>".format(
            key=escape(str(item.get("installation_key", ""))),
            count=escape(str(item.get("package_count", ""))),
            first=escape(str(item.get("first_capture", ""))),
            last=escape(str(item.get("last_capture", ""))),
            confidence=escape(str(item.get("longitudinal_confidence", ""))),
            severity=escape(json.dumps(item.get("severity_counts", {}), sort_keys=True)),
        )
        for item in installations
    )
    control_run_rows = "".join(
        "<tr><td><code>{installation}</code></td><td>{controller}</td>"
        "<td><code>{helper}</code></td><td>{starts}</td><td>{active}</td>"
        "<td>{longest}</td><td>{short}</td><td>{toggles}</td><td>{open}</td></tr>".format(
            installation=escape(str(item.get("installation_key", ""))),
            controller=escape(str(item.get("controller", ""))),
            helper=escape(str(item.get("entity_id", ""))),
            starts=escape(str(item.get("starts", ""))),
            active=escape(str(item.get("active_minutes", ""))),
            longest=escape(str(item.get("longest_active_minutes", ""))),
            short=escape(str(item.get("short_runs", ""))),
            toggles=escape(str(item.get("max_toggles_per_hour", ""))),
            open=escape(str(item.get("open", ""))),
        )
        for item in control_runs
    ) or "<tr><td colspan='9'>No evaluable control-run history.</td></tr>"
    cards = "".join(
        f"<div class='card'><span>{escape(label)}</span><strong>{escape(str(totals.get(key, 0)))}</strong></div>"
        for key, label in (
            ("discovered_archives", "Archives"),
            ("installations", "Installations"),
            ("finding_groups", "Finding groups"),
            ("rejected_archives", "Rejected"),
            ("duplicate_archives", "Duplicates"),
        )
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hoymiles diagnostics analysis</title>
<style>
:root{{--bg:#0b1220;--panel:#111c2e;--line:#26364f;--text:#e7eefb;--muted:#9fb0ca;--critical:#ff6b6b;--error:#ff9f43;--warning:#ffd166;--info:#66d9ef}}
body{{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,sans-serif}}main{{max-width:1500px;margin:auto;padding:24px}}
h1,h2{{letter-spacing:.01em}}.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}}.card{{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;display:flex;flex-direction:column}}.card span{{color:var(--muted)}}.card strong{{font-size:25px}}
section{{margin-top:24px;background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:16px;overflow:auto}}input{{width:min(520px,100%);padding:10px;border-radius:8px;border:1px solid var(--line);background:#09101c;color:var(--text);margin-bottom:12px}}
table{{width:100%;border-collapse:collapse;min-width:850px}}th,td{{text-align:left;padding:9px;border-bottom:1px solid var(--line);vertical-align:top}}th{{position:sticky;top:0;background:var(--panel)}}code{{color:#9cdcfe}}.sev-critical td:first-child{{color:var(--critical);font-weight:700}}.sev-error td:first-child{{color:var(--error);font-weight:700}}.sev-warning td:first-child{{color:var(--warning)}}.sev-info td:first-child{{color:var(--info)}}.muted{{color:var(--muted)}}
</style></head><body><main>
<h1>Hoymiles diagnostics — batch analysis</h1><p class="muted">Offline report generated {escape(str(summary.get('generated_at', '')))}. Full UUID, source paths and raw logs are excluded by default.</p>
<div class="cards">{cards}</div>
<section><h2>Findings</h2><input type="search" placeholder="Filter findings…" data-filter="findings"><table id="findings"><thead><tr><th>Severity</th><th>Rule</th><th>Installation</th><th>Controller</th><th>Count</th><th>Message</th><th>Recommendation</th></tr></thead><tbody>{finding_rows}</tbody></table></section>
<section><h2>Installations</h2><input type="search" placeholder="Filter installations…" data-filter="installations"><table id="installations"><thead><tr><th>Installation</th><th>Packages</th><th>First</th><th>Last</th><th>Confidence</th><th>Severity counts</th></tr></thead><tbody>{installation_rows}</tbody></table></section>
<section><h2>Control-run history</h2><input type="search" placeholder="Filter control runs…" data-filter="control-runs"><table id="control-runs"><thead><tr><th>Installation</th><th>Controller</th><th>Helper</th><th>Starts</th><th>Active min</th><th>Longest min</th><th>Short runs</th><th>Max toggles/h</th><th>Open</th></tr></thead><tbody>{control_run_rows}</tbody></table></section>
<section><h2>Evidence limits</h2><ul>{''.join(f'<li><b>{escape(str(item.get("code", "")))}</b>: {escape(str(item.get("message", "")))}</li>' for item in _mapping_rows(summary.get("limitations")))}</ul></section>
</main><script>document.querySelectorAll('[data-filter]').forEach(i=>i.addEventListener('input',()=>{{const q=i.value.toLowerCase();document.querySelectorAll('#'+i.dataset.filter+' tbody tr').forEach(r=>r.hidden=!r.textContent.toLowerCase().includes(q));}}));</script></body></html>"""
