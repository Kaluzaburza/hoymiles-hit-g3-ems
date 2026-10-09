"""Build downloadable, privacy-filtered Hoymiles support archives."""

from __future__ import annotations

from collections.abc import Iterable
from io import BytesIO
import json
from pathlib import Path
import re
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from .diagnostic_redaction import sanitize_diagnostic_value


MAX_LOG_BYTES = 2_000_000
MAX_LOG_LINES = 2_500
MAX_REPORTS = 32
MAX_REPORT_BYTES = 12 * 1024 * 1024
MAX_DIAGNOSTICS_MEMBER_BYTES = 20 * 1024 * 1024
MAX_LOG_MEMBER_BYTES = 2 * 1024 * 1024
MAX_ENVIRONMENT_MEMBER_BYTES = 64 * 1024
MAX_README_MEMBER_BYTES = 16 * 1024
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 23 * 1024 * 1024
MAX_ARCHIVE_BYTES = 24 * 1024 * 1024
OMISSION_SCHEMA_VERSION = 1
OMISSION_MARKER_RESERVE_BYTES = 1024
LOG_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:hoymiles|esphome|modbus|rcem|rce|rcm|"
    r"tariff|taryf|ems|SocketClosedAPIError)(?![A-Za-z0-9])",
    re.IGNORECASE,
)


def _omission_marker(
    scope: str,
    reason: str,
    **details: Any,
) -> dict[str, Any]:
    """Return a small machine-readable marker for atomically omitted data."""
    return {
        "diagnostic_omission": True,
        "omission_schema_version": OMISSION_SCHEMA_VERSION,
        "scope": scope,
        "omission_reason": reason,
        **details,
    }


def _json_bytes(value: Any, *, indent: int | None = None) -> bytes:
    """Serialize a JSON member once and measure its actual UTF-8 size."""
    return (
        json.dumps(value, ensure_ascii=False, indent=indent) + "\n"
    ).encode("utf-8")


def _report_entry_bytes(value: dict[str, Any]) -> bytes:
    """Encode one report for insertion into a readable JSON array."""
    encoded = json.dumps(value, ensure_ascii=False, indent=2)
    return "\n".join(f"  {line}" for line in encoded.splitlines()).encode("utf-8")


def _report_member_bytes(entries: list[bytes]) -> bytes:
    """Compose already-bounded report entries without reserializing them."""
    if not entries:
        return b"[]\n"
    return b"[\n" + b",\n".join(entries) + b"\n]\n"


def _projected_report_member_size(
    entries_size: int,
    entry_count: int,
    candidate_size: int,
) -> int:
    """Return the exact member size after appending one encoded entry."""
    separators = 2 * entry_count
    return 5 + entries_size + candidate_size + separators


def _bounded_log_output(lines: list[str]) -> str:
    """Keep the newest complete sanitized lines inside the log-member budget."""
    encoded_lines = [f"{line}\n".encode("utf-8") for line in lines]
    retained_newest_first: list[bytes] = []
    retained_bytes = 0

    for encoded_line in reversed(encoded_lines):
        if retained_bytes + len(encoded_line) > MAX_LOG_MEMBER_BYTES:
            break
        retained_newest_first.append(encoded_line)
        retained_bytes += len(encoded_line)

    omitted_count = len(encoded_lines) - len(retained_newest_first)
    marker = b""
    if omitted_count:
        marker = (
            "[OMITTED: "
            f"{omitted_count} older relevant log lines exceeded the "
            "log member size limit]\n"
        ).encode("utf-8")
        while (
            retained_newest_first
            and retained_bytes + len(marker) > MAX_LOG_MEMBER_BYTES
        ):
            retained_bytes -= len(retained_newest_first.pop())
            omitted_count += 1
            marker = (
                "[OMITTED: "
                f"{omitted_count} older relevant log lines exceeded the "
                "log member size limit]\n"
            ).encode("utf-8")

    output = marker + b"".join(reversed(retained_newest_first))
    if len(output) > MAX_LOG_MEMBER_BYTES:
        return "[OMITTED: relevant logs exceeded the log member size limit]\n"
    return output.decode("utf-8")


def _relevant_log_lines(log_path: Path) -> str:
    """Return the relevant tail of a Core log without loading an unbounded file."""
    if not log_path.is_file():
        return "Home Assistant log file is not available in /config.\n"
    try:
        with log_path.open("rb") as log_file:
            log_file.seek(0, 2)
            size = log_file.tell()
            log_file.seek(max(size - MAX_LOG_BYTES, 0))
            raw = log_file.read(MAX_LOG_BYTES)
    except OSError as err:
        return f"Cannot read Home Assistant log ({type(err).__name__}).\n"

    text = raw.decode("utf-8", errors="replace")
    if size > MAX_LOG_BYTES:
        # The first line may start in the middle after seeking into a large log.
        text = text.partition("\n")[2]
    relevant = [line for line in text.splitlines() if LOG_PATTERN.search(line)]
    sanitized = [
        str(sanitize_diagnostic_value(line))
        for line in relevant[-MAX_LOG_LINES:]
    ]
    if not sanitized:
        return "No relevant Hoymiles/ESPHome/Modbus log lines were found.\n"
    return _bounded_log_output(sanitized)


def _bounded_reports(
    reports: Iterable[dict[str, Any]],
    *,
    safe_identity: dict[str, Any],
) -> tuple[bytes, dict[str, Any]]:
    """Serialize a bounded report prefix with atomic omission markers."""
    entries: list[bytes] = []
    entries_size = 0
    consumed_count = 0
    exported_count = 0
    omitted_count = 0
    collection_complete = True
    content_complete = True
    incomplete_history_report_count = 0
    iterator = iter(reports)

    def append_marker(
        marker: dict[str, Any],
        *,
        terminal: bool,
    ) -> bool:
        nonlocal entries_size
        encoded_marker = _report_entry_bytes(marker)
        projected = _projected_report_member_size(
            entries_size,
            len(entries),
            len(encoded_marker),
        )
        if not terminal:
            projected += 2 + OMISSION_MARKER_RESERVE_BYTES
        if projected > MAX_DIAGNOSTICS_MEMBER_BYTES:
            return False
        entries.append(encoded_marker)
        entries_size += len(encoded_marker)
        return True

    for report_index in range(MAX_REPORTS):
        try:
            report = next(iterator)
        except StopIteration:
            break
        consumed_count += 1

        try:
            safe_report = sanitize_diagnostic_value(report)
            if not isinstance(safe_report, dict):
                raise TypeError("diagnostic report is not a mapping")
            safe_report.update(safe_identity)
            if safe_report.get("diagnostic_omission") is True:
                collection_complete = False
            control_history = safe_report.get("control_history")
            if (
                isinstance(control_history, dict)
                and control_history.get("complete") is False
            ):
                content_complete = False
                incomplete_history_report_count += 1
            encoded_report = _report_entry_bytes(safe_report)
        except Exception as err:  # A bad entry must not expose adjacent reports.
            omitted_count += 1
            collection_complete = False
            marker = {
                **_omission_marker(
                    "report",
                    "report_serialization_error",
                    report_index=report_index,
                    error_type=type(err).__name__,
                ),
                **safe_identity,
            }
            if not append_marker(marker, terminal=False):
                append_marker(
                    {
                        **_omission_marker(
                            "reports",
                            "diagnostics_member_size_limit",
                            first_omitted_report_index=report_index,
                            omitted_report_count_at_least=1,
                        ),
                        **safe_identity,
                    },
                    terminal=True,
                )
                break
            continue

        if len(encoded_report) > MAX_REPORT_BYTES:
            omitted_count += 1
            collection_complete = False
            marker = {
                **_omission_marker(
                    "report",
                    "report_size_limit",
                    report_index=report_index,
                    measured_bytes=len(encoded_report),
                    max_bytes=MAX_REPORT_BYTES,
                ),
                **safe_identity,
            }
            if not append_marker(marker, terminal=False):
                append_marker(
                    {
                        **_omission_marker(
                            "reports",
                            "diagnostics_member_size_limit",
                            first_omitted_report_index=report_index,
                            omitted_report_count_at_least=1,
                        ),
                        **safe_identity,
                    },
                    terminal=True,
                )
                break
            continue

        overflow_marker = {
            **_omission_marker(
                "reports",
                "diagnostics_member_size_limit",
                first_omitted_report_index=report_index,
                omitted_report_count_at_least=1,
            ),
            **safe_identity,
        }
        overflow_marker_size = len(_report_entry_bytes(overflow_marker))
        projected = _projected_report_member_size(
            entries_size,
            len(entries),
            len(encoded_report),
        )
        # Always reserve enough room to report a later collection/count limit.
        projected_with_marker = projected + 2 + max(
            overflow_marker_size,
            OMISSION_MARKER_RESERVE_BYTES,
        )
        if projected_with_marker > MAX_DIAGNOSTICS_MEMBER_BYTES:
            omitted_count += 1
            collection_complete = False
            append_marker(overflow_marker, terminal=True)
            break

        entries.append(encoded_report)
        entries_size += len(encoded_report)
        exported_count += 1
    else:
        try:
            next(iterator)
        except StopIteration:
            pass
        else:
            consumed_count += 1
            omitted_count += 1
            collection_complete = False
            append_marker(
                {
                    **_omission_marker(
                        "reports",
                        "report_count_limit",
                        first_omitted_report_index=MAX_REPORTS,
                        omitted_report_count_at_least=1,
                        max_reports=MAX_REPORTS,
                    ),
                    **safe_identity,
                },
                terminal=True,
            )

    return _report_member_bytes(entries), {
        "report_count": consumed_count,
        "exported_report_count": exported_count,
        "omitted_report_count_at_least": omitted_count,
        "report_collection_complete": collection_complete,
        "report_content_complete": content_complete,
        "incomplete_history_report_count": incomplete_history_report_count,
    }


def _zip_members(members: dict[str, bytes]) -> bytes:
    """Return one in-memory ZIP containing the supplied bounded members."""
    archive_buffer = BytesIO()
    with ZipFile(archive_buffer, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return archive_buffer.getvalue()


def _global_omission_members(
    *,
    readme: bytes,
    metadata: dict[str, Any],
    safe_identity: dict[str, Any],
    reason: str,
) -> dict[str, bytes]:
    """Return the fixed archive shape with all large evidence omitted."""
    global_marker = {
        **_omission_marker("archive", reason),
        **safe_identity,
    }
    fallback_metadata = {
        **metadata,
        "archive_complete": False,
        "archive_omission_reason": reason,
    }
    return {
        "README.txt": readme,
        "environment.json": _json_bytes(fallback_metadata, indent=2),
        "hoymiles_diagnostics.json": _json_bytes([global_marker], indent=2),
        "home_assistant_relevant_logs.txt": (
            f"[OMITTED: {reason}]\n".encode("utf-8")
        ),
    }


def build_support_archive(
    reports: Iterable[dict[str, Any]],
    *,
    log_path: Path,
    generated_at: str,
    home_assistant_version: str,
    anonymous_installation_id: str,
    installation_id_schema_version: int,
) -> bytes:
    """Return a ZIP with diagnostic JSON and redacted relevant Core logs."""
    safe_identity = sanitize_diagnostic_value(
        {
            "anonymous_installation_id": anonymous_installation_id,
            "installation_id_schema_version": installation_id_schema_version,
        },
        allow_anonymous_installation_id=True,
    )
    diagnostics_member, report_summary = _bounded_reports(
        reports,
        safe_identity=safe_identity,
    )
    metadata: dict[str, Any] = {
        "generated_at": generated_at,
        "home_assistant_version": home_assistant_version,
        **report_summary,
        "anonymous_installation_id": anonymous_installation_id,
        "installation_id_schema_version": installation_id_schema_version,
        "archive_complete": bool(
            report_summary["report_collection_complete"]
            and report_summary["report_content_complete"]
        ),
        "limits": {
            "max_reports": MAX_REPORTS,
            "max_report_bytes": MAX_REPORT_BYTES,
            "max_diagnostics_member_bytes": MAX_DIAGNOSTICS_MEMBER_BYTES,
            "max_log_source_bytes": MAX_LOG_BYTES,
            "max_log_lines": MAX_LOG_LINES,
            "max_log_member_bytes": MAX_LOG_MEMBER_BYTES,
            "max_archive_uncompressed_bytes": MAX_ARCHIVE_UNCOMPRESSED_BYTES,
            "max_archive_bytes": MAX_ARCHIVE_BYTES,
        },
    }
    readme = (
        "EMS for Hoymiles HIT-(5–20)L-G3 diagnostic archive\n\n"
        "Attach this ZIP together with the exact local date/time of the fault "
        "and a short description of the expected behaviour. Send the complete "
        "report to info@kaluzaaa.com.\n"
        "The archive contains the current integration state, 24 hours of "
        "significant control history and relevant Home Assistant Core logs.\n"
        "ESPHome device runtime logs are not exposed to Home Assistant Core; "
        "for low-level UART/Modbus faults attach an ESPHome log excerpt too.\n\n"
        "Paczka diagnostyczna EMS for Hoymiles HIT-(5–20)L-G3\n\n"
        "Dołącz ZIP wraz z dokładną lokalną datą i godziną błędu oraz opisem "
        "oczekiwanego zachowania i wyślij całość na info@kaluzaaa.com. "
        "Paczka zawiera bieżący stan integracji, "
        "24 godziny istotnych zmian sterowania i powiązane logi HA Core.\n"
        "Logi pracy urządzenia ESPHome nie są udostępniane procesowi HA Core; "
        "przy błędach UART/Modbus dołącz również fragment logu ESPHome.\n\n"
        "Device, account and configuration identifiers are automatically "
        "masked. A random anonymous installation ID is intentionally kept "
        "only to correlate support archives from this Home Assistant over "
        "time. "
        "Review the files before posting them publicly.\n"
        "Identyfikatory urządzeń, kont i konfiguracji są automatycznie "
        "maskowane. Losowy anonimowy identyfikator instalacji pozostaje "
        "wyłącznie do łączenia kolejnych paczek wsparcia z tego Home "
        "Assistanta. "
        "Przejrzyj pliki przed ich publicznym udostępnieniem.\n"
    )

    readme_member = readme.encode("utf-8")
    readme_complete = True
    if len(readme_member) > MAX_README_MEMBER_BYTES:
        readme_member = b"[OMITTED: README exceeded its member size limit]\n"
        readme_complete = False
    log_member = _relevant_log_lines(log_path).encode("utf-8")
    if len(log_member) > MAX_LOG_MEMBER_BYTES:
        log_member = b"[OMITTED: logs exceeded their member size limit]\n"
    log_complete = not log_member.startswith(
        (
            b"[OMITTED:",
            b"Cannot read Home Assistant log",
            b"Home Assistant log file is not available",
        )
    )
    metadata["readme_complete"] = readme_complete
    metadata["log_collection_complete"] = log_complete
    metadata["archive_complete"] = bool(
        metadata["archive_complete"] and readme_complete and log_complete
    )
    safe_metadata = sanitize_diagnostic_value(
        metadata,
        allow_anonymous_installation_id=True,
    )
    environment_member = _json_bytes(safe_metadata, indent=2)
    if len(environment_member) > MAX_ENVIRONMENT_MEMBER_BYTES:
        environment_member = _json_bytes(
            {
                **_omission_marker("environment", "environment_member_size_limit"),
                **safe_identity,
                "archive_complete": False,
            },
            indent=2,
        )

    members = {
        "README.txt": readme_member,
        "environment.json": environment_member,
        "hoymiles_diagnostics.json": diagnostics_member,
        "home_assistant_relevant_logs.txt": log_member,
    }
    if sum(len(payload) for payload in members.values()) > (
        MAX_ARCHIVE_UNCOMPRESSED_BYTES
    ):
        members = _global_omission_members(
            readme=readme_member,
            metadata=safe_metadata,
            safe_identity=safe_identity,
            reason="archive_uncompressed_size_limit",
        )

    archive_bytes = _zip_members(members)
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        members = _global_omission_members(
            readme=readme_member,
            metadata=safe_metadata,
            safe_identity=safe_identity,
            reason="archive_compressed_size_limit",
        )
        archive_bytes = _zip_members(members)
    if len(archive_bytes) > MAX_ARCHIVE_BYTES:
        raise ValueError("Diagnostic ZIP size limit is smaller than its omission archive")
    return archive_bytes
