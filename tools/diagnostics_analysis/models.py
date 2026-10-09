"""Stable, privacy-aware data models for offline diagnostics analysis.

The analyzer deliberately keeps raw diagnostic payloads separate from its
normalized observations.  Output writers should serialize observations and
findings, not the ``raw`` report or ``relevant_log_text`` fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
import math
from pathlib import Path
from typing import Any


ANALYZER_VERSION = "1.1.0"
# v1.5.6 adds rule outcomes and rows using the existing finding/history
# mappings; no serialized model shape changed.
ANALYSIS_SCHEMA_VERSION = 1
RULE_SET_VERSION = 2
SUPPORTED_REPORT_SCHEMA_VERSIONS = frozenset({1})
SUPPORTED_INSTALLATION_ID_SCHEMA_VERSIONS = frozenset({1})


class Controller(str, Enum):
    """Controller families represented in normalized observations."""

    SYSTEM = "system"
    RCE = "rce"
    RCEM = "rcem"
    TARIFF = "tariff"


class Severity(str, Enum):
    """Stable finding severity levels."""

    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


class Confidence(str, Enum):
    """How directly the available bundle evidence supports a finding."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ArchiveStatus(str, Enum):
    """Processing outcome for one archive."""

    ACCEPTED = "accepted"
    PARTIAL = "partial"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"


class ValueStatus(str, Enum):
    """Explicit availability of a normalized value.

    This prevents missing, unavailable or redacted telemetry from being
    accidentally coerced to ``False`` or contractual numeric zero.
    """

    PRESENT = "present"
    MISSING = "missing"
    UNKNOWN = "unknown"
    UNAVAILABLE = "unavailable"
    REDACTED = "redacted"
    INVALID = "invalid"


def to_json_value(value: Any) -> Any:
    """Convert analyzer models to JSON-compatible values.

    Dataclass fields carrying ``metadata={"serialize": False}`` are internal
    and intentionally omitted.  The helper never stringifies unknown object
    types because doing so could leak an arbitrary diagnostic representation.
    """

    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("Analyzer models cannot serialize non-finite numbers")
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: to_json_value(getattr(value, item.name))
            for item in fields(value)
            if item.metadata.get("serialize", True)
        }
    if isinstance(value, Mapping):
        return {str(key): to_json_value(child) for key, child in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_json_value(child) for child in value]
    if isinstance(value, (set, frozenset)):
        return sorted(
            (to_json_value(child) for child in value),
            key=repr,
        )
    raise TypeError(f"Unsupported analyzer value type: {type(value).__name__}")


class SerializableModel:
    """Mixin exposing a consistent dictionary representation."""

    __slots__ = ()

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible mapping without internal raw fields."""

        converted = to_json_value(self)
        if not isinstance(converted, dict):  # pragma: no cover - model invariant
            raise TypeError("Analyzer model did not serialize to a mapping")
        return converted


@dataclass(frozen=True, slots=True)
class NormalizedValue(SerializableModel):
    """A scalar together with explicit availability and optional source."""

    value: str | int | float | bool | None = None
    status: ValueStatus = ValueStatus.UNKNOWN
    source: str | None = None


@dataclass(frozen=True, slots=True)
class DiagnosticReport(SerializableModel):
    """One config-entry report normalized from the bundle JSON."""

    report_index: int
    report_schema_version: int | None
    generated_at: datetime | None
    integration_version: str | None
    managed_state_snapshot: Mapping[str, Any] = field(
        metadata={"serialize": False}
    )
    control_history: Mapping[str, Any] = field(metadata={"serialize": False})
    catalog_entities: tuple[Mapping[str, Any], ...] = field(
        metadata={"serialize": False}
    )
    catalog_coverage: Mapping[str, Any] = field(
        metadata={"serialize": False}
    )
    raw: Mapping[str, Any] = field(
        repr=False,
        compare=False,
        metadata={"serialize": False},
    )


@dataclass(frozen=True, slots=True)
class ArchiveMetadata(SerializableModel):
    """Validated metadata for one diagnostic ZIP."""

    archive_key: str
    content_sha256: str
    source_size_bytes: int
    generated_at: datetime | None
    home_assistant_version: str | None
    declared_report_count: int | None
    actual_report_count: int
    anonymous_installation_id: str | None = field(
        metadata={"serialize": False}
    )
    installation_id_schema_version: int | None
    installation_key: str
    report_schema_versions: tuple[int, ...]
    extra_members: tuple[str, ...] = field(metadata={"serialize": False})


@dataclass(frozen=True, slots=True)
class LoadedDiagnosticArchive(SerializableModel):
    """Validated archive contents ready for bounded offline extraction."""

    metadata: ArchiveMetadata
    reports: tuple[DiagnosticReport, ...] = field(
        metadata={"serialize": False}
    )
    environment: Mapping[str, Any] = field(metadata={"serialize": False})
    relevant_log_text: str | None = field(
        default=None,
        repr=False,
        compare=False,
        metadata={"serialize": False},
    )
    warnings: tuple[str, ...] = ()

    @property
    def canonical_snapshot(self) -> Mapping[str, Any]:
        """Return one complete global snapshot without config-entry repeats."""

        if not self.reports:
            return {}
        return max(
            self.reports,
            key=lambda report: len(report.managed_state_snapshot),
        ).managed_state_snapshot

    @property
    def canonical_history(self) -> Mapping[str, Any]:
        """Return one complete global history without config-entry repeats."""

        if not self.reports:
            return {}

        def history_size(report: DiagnosticReport) -> int:
            entities = report.control_history.get("entities")
            return len(entities) if isinstance(entities, Mapping) else 0

        return max(self.reports, key=history_size).control_history


@dataclass(frozen=True, slots=True)
class ControllerObservation(SerializableModel):
    """Normalized point-in-time evidence for one controller."""

    archive_key: str
    installation_key: str
    report_index: int
    controller: Controller
    observed_at: datetime | None
    entity_id: str | None
    state: str | None
    last_changed: datetime | None
    last_updated: datetime | None
    status_code: str | None = None
    action: str | None = None
    result_current: bool | None = None
    planned: bool | None = None
    enabled: bool | None = None
    active: bool | None = None
    owner_code: str | None = None
    suppression_reason: str | None = None
    continue_reason: str | None = None
    block_reason: str | None = None
    freshness: Mapping[str, bool | None] = field(default_factory=dict)
    ages_seconds: Mapping[str, float | None] = field(default_factory=dict)
    metrics: Mapping[str, Any] = field(default_factory=dict)
    flags: Mapping[str, bool | None] = field(default_factory=dict)
    coverage: Mapping[str, ValueStatus] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Finding(SerializableModel):
    """One deterministic diagnostic conclusion."""

    code: str
    severity: Severity
    message: str
    confidence: Confidence = Confidence.MEDIUM
    installation_key: str | None = None
    archive_key: str | None = None
    controller: Controller | None = None
    observed_at: datetime | None = None
    evidence: Mapping[str, Any] = field(default_factory=dict)
    recommendation: str | None = None
    occurrences: int = 1


@dataclass(frozen=True, slots=True)
class ArchiveAnalysis(SerializableModel):
    """Normalized observations and findings derived from one archive."""

    package: LoadedDiagnosticArchive = field(
        metadata={"serialize": False},
    )
    observations: tuple[ControllerObservation, ...] = ()
    findings: tuple[Finding, ...] = ()
    status: ArchiveStatus = ArchiveStatus.ACCEPTED

    @property
    def archive_key(self) -> str:
        """Return the source archive key without serializing raw payloads."""

        return self.package.metadata.archive_key

    @property
    def installation_key(self) -> str:
        """Return the privacy-safe installation grouping key."""

        return self.package.metadata.installation_key
