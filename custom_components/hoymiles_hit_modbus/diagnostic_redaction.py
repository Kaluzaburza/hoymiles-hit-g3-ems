"""Privacy helpers shared by Hoymiles diagnostic reports."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
import hashlib
import ipaddress
import math
import re
from typing import Any
from uuid import RFC_4122, UUID


REDACTED = "[REDACTED]"
# ZIP reports wrap supervisor -> STOP frame -> frozen inputs -> lease/readback.
# Keep that evidence intact while retaining the existing byte/item budgets.
MAX_DEPTH = 12
MAX_ITEMS = 500
MAX_STRING_LENGTH = 8_000
MAX_STRING_SCAN_LENGTH = 32_000

_SENSITIVE_KEY_PARTS = (
    "access_key",
    "api_key",
    "authorization",
    "bssid",
    "client_id",
    "cookie",
    "credential",
    "device_id",
    "email",
    "entry_id",
    "friendly_name",
    "host",
    "host_name",
    "hostname",
    "latitude",
    "location",
    "longitude",
    "mac",
    "name_by_user",
    "password",
    "refresh_token",
    "serial",
    "secret",
    "ssid",
    "token",
    "url",
    "user_id",
    "user_name",
    "username",
    "wifi",
)
_URL_RE = re.compile(
    r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_HOSTNAME_RE = re.compile(
    r"\b(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"(?:[a-z]{2,63}|local)\b",
    re.IGNORECASE,
)
_IPV4_RE = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")
_MAC_RE = re.compile(
    r"\b(?:(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}|"
    r"(?:[0-9a-f]{4}\.){2}[0-9a-f]{4})\b",
    re.IGNORECASE,
)
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
_OPAQUE_RE = re.compile(r"\b(?:[A-Za-z0-9+/_-]{40,}={0,2})\b")
_AUTH_VALUE_RE = re.compile(
    r"\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=:%-]+",
    re.IGNORECASE,
)
_IPV6_CANDIDATE_RE = re.compile(
    r"(?<![0-9A-Fa-f:.])(?:[0-9A-Fa-f:.]*:[0-9A-Fa-f:.]+)(?![0-9A-Fa-f:.])"
)
_SECRET_VALUE_RE = re.compile(
    r"(?<![A-Za-z0-9_])[\"']?"
    r"(?P<label>password|passphrase|token|refresh[_ -]?token|secret|"
    r"client[_ -]?secret|api[_ -]?key|access[_ -]?key|authorization|"
    r"cookie|credential|ssid|wifi|serial(?:[_ -]?number)?|"
    r"device[_ -]?id|entry[_ -]?id|user[_ -]?id|username|email|"
    r"friendly[_ -]?name|name[_ -]?by[_ -]?user|host(?:name)?|"
    r"latitude|longitude|location|mac|url)"
    r"[\"']?\s*[:=]\s*"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\r\n,;}\]]+)",
    re.IGNORECASE,
)
_IDENTIFIER_PHRASE_RE = re.compile(
    r"\b(?P<label>serial(?:\s+number)?|device\s+id|entry\s+id|user\s+id)"
    r"\s+(?:is\s+)?[A-Za-z0-9._:+/-]+",
    re.IGNORECASE,
)
_DYNAMIC_IDENTIFIER_KEY_RE = re.compile(
    r"^(?:serial(?:[_ -]?number)?|device[_ -]?id|entry[_ -]?id|user[_ -]?id)"
    r"[:=_ -]+(?!number$).+$",
    re.IGNORECASE,
)

_ANONYMOUS_INSTALLATION_ID_KEY = "anonymous_installation_id"
_CORRELATION_ID_KEYS = frozenset(
    {"transaction_id", "transaction_ids", "active_transaction_id",
     "lease_id", "range_id", "logical_range_id", "event_id",
     "input_revision", "captured_input_revision", "pending_input_revision",
     "plan_revision", "profile_revision", "source_revision", "load_model_revision",
     "joint_plan_revision", "joint_profile_revision"}
)


def _correlation_id(value: str) -> str:
    """Keep distinct opaque control IDs linkable without exporting their value."""
    sanitized = _sanitize_text(value)
    if (
        len(value) <= 512
        and re.fullmatch(r"[A-Za-z0-9_.:-]+", value)
        and sanitized != value
    ):
        return "diag-id-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
    return sanitized

# These exact keys are public, schema-owned energy metrics.  The generic
# ``authorization`` key rule must continue to redact credentials, so only
# finite numeric telemetry (and the one fixed projection label) may bypass it.
_SAFE_AUTHORIZATION_NUMERIC_KEYS = frozenset(
    {
        "authorization_final_soc_percent",
        "authorization_grid_export_kwh",
        "authorization_reserve_violation_count",
        "authorization_soc_end_percent",
        "authorization_soc_start_percent",
    }
)
_SAFE_AUTHORIZATION_PROJECTION_KEY = "authorization_projection"
_SAFE_AUTHORIZATION_PROJECTION_VALUE = "conservative_fail_closed"


def _safe_anonymous_installation_id(value: Any) -> str | None:
    """Allow only the deliberately exported canonical UUID v4."""
    if not isinstance(value, str):
        return None
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError):
        return None
    if (
        parsed.version != 4
        or parsed.variant != RFC_4122
        or str(parsed) != value
    ):
        return None
    return value


def _key_is_sensitive(key: str) -> bool:
    """Return whether a mapping key identifies private installation data."""
    acronym_boundaries = re.sub(
        r"(?<=[A-Z])(?=[A-Z][a-z])",
        "_",
        key,
    )
    with_word_boundaries = re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])",
        "_",
        acronym_boundaries,
    )
    normalized = re.sub(
        r"[^a-z0-9]+",
        "_",
        with_word_boundaries.casefold(),
    ).strip("_")
    normalized_with_boundaries = f"_{normalized}_"
    return (
        normalized == "key"
        or normalized.endswith("_key")
        or any(
            f"_{part}_" in normalized_with_boundaries
            for part in _SENSITIVE_KEY_PARTS
        )
    )


def _redact_ipv6_candidates(value: str) -> str:
    """Mask syntactically valid IPv6 addresses without eating timestamps."""

    def replace(match: re.Match[str]) -> str:
        candidate = match.group(0)
        try:
            parsed = ipaddress.ip_address(candidate)
        except ValueError:
            return candidate
        return "[REDACTED_IP]" if parsed.version == 6 else candidate

    return _IPV6_CANDIDATE_RE.sub(replace, value)


def _sanitize_text(value: str) -> str:
    """Mask common secrets and network/user identifiers in free-form text."""
    input_truncated = len(value) > MAX_STRING_SCAN_LENGTH
    sanitized = _AUTH_VALUE_RE.sub(
        "[REDACTED_AUTH]",
        value[:MAX_STRING_SCAN_LENGTH],
    )
    sanitized = _SECRET_VALUE_RE.sub(
        lambda match: f"{match.group('label')}={REDACTED}",
        sanitized,
    )
    sanitized = _IDENTIFIER_PHRASE_RE.sub(
        lambda match: f"{match.group('label')}={REDACTED}",
        sanitized,
    )
    sanitized = _URL_RE.sub("[REDACTED_URL]", sanitized)
    sanitized = _EMAIL_RE.sub("[REDACTED_EMAIL]", sanitized)
    sanitized = _HOSTNAME_RE.sub("[REDACTED_HOST]", sanitized)
    sanitized = _redact_ipv6_candidates(sanitized)
    sanitized = _IPV4_RE.sub("[REDACTED_IP]", sanitized)
    sanitized = _MAC_RE.sub("[REDACTED_MAC]", sanitized)
    sanitized = _UUID_RE.sub("[REDACTED_ID]", sanitized)
    sanitized = _OPAQUE_RE.sub(REDACTED, sanitized)
    if len(sanitized) > MAX_STRING_LENGTH or input_truncated:
        return f"{sanitized[:MAX_STRING_LENGTH]}...[TRUNCATED]"
    return sanitized


def sanitize_diagnostic_value(
    value: Any,
    *,
    key_hint: str = "",
    allow_anonymous_installation_id: bool = False,
    _depth: int = 0,
) -> Any:
    """Return a JSON-safe value with secrets and personal data masked."""
    if key_hint == _ANONYMOUS_INSTALLATION_ID_KEY:
        if allow_anonymous_installation_id and _depth == 1:
            return _safe_anonymous_installation_id(value) or REDACTED
        return REDACTED
    if key_hint in _SAFE_AUTHORIZATION_NUMERIC_KEYS:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return REDACTED
        return value if math.isfinite(float(value)) else REDACTED
    if key_hint == _SAFE_AUTHORIZATION_PROJECTION_KEY:
        return (
            value
            if value == _SAFE_AUTHORIZATION_PROJECTION_VALUE
            else REDACTED
        )
    if _key_is_sensitive(key_hint):
        return REDACTED
    if _depth >= MAX_DEPTH:
        return "[MAX_DEPTH_REACHED]"

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        if key_hint in _CORRELATION_ID_KEYS:
            return _correlation_id(value)
        return _sanitize_text(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        sanitized: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_ITEMS:
                sanitized["_truncated"] = True
                break
            text_key = str(key)
            sanitized_key = _sanitize_text(text_key)
            if (
                sanitized_key == text_key
                and _DYNAMIC_IDENTIFIER_KEY_RE.fullmatch(text_key)
            ):
                sanitized_key = "[REDACTED_KEY]"
            if sanitized_key in sanitized:
                sanitized_key = f"{sanitized_key}#{index + 1}"
            sanitized[sanitized_key] = sanitize_diagnostic_value(
                item,
                key_hint=text_key,
                allow_anonymous_installation_id=(
                    allow_anonymous_installation_id
                ),
                _depth=_depth + 1,
            )
        return sanitized
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        sanitized_items = [
            sanitize_diagnostic_value(
                item,
                key_hint=key_hint if key_hint in _CORRELATION_ID_KEYS else "",
                allow_anonymous_installation_id=(
                    allow_anonymous_installation_id
                ),
                _depth=_depth + 1,
            )
            for item in value[:MAX_ITEMS]
        ]
        if len(value) > MAX_ITEMS:
            sanitized_items.append("[TRUNCATED]")
        return sanitized_items

    return _sanitize_text(str(value))
