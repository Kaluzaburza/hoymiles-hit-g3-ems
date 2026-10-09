"""Bounded, date-valid PSE price cache; independent of HA and control leases."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
from math import isfinite
import re
from typing import Any
from zoneinfo import ZoneInfo

SCHEMA = 1
SOURCE = "pse_rce_daily_cache_v1"
WARSAW = ZoneInfo("Europe/Warsaw")
UTC = timezone.utc
_PERIOD = re.compile(r"^(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})$")


def utc_time(value: Any) -> datetime:
    """Decode an explicitly UTC API field; storage timestamps require a zone."""
    if not isinstance(value, str):
        raise ValueError("timestamp_missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _stored_time(value: Any, now: datetime) -> datetime:
    if not isinstance(value, str) or datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("storage_timestamp_zone_missing")
    parsed = utc_time(value)
    if parsed > now.astimezone(UTC) + timedelta(seconds=5):
        raise ValueError("future_timestamp")
    return parsed


def day_bounds(day: date) -> tuple[datetime, datetime]:
    return (
        datetime.combine(day, time.min, WARSAW).astimezone(UTC),
        datetime.combine(day + timedelta(days=1), time.min, WARSAW).astimezone(UTC),
    )


def validate_rows(rows: Any, day: date, now: datetime) -> list[dict[str, Any]]:
    """Accept exactly one complete quarter-hour Warsaw day (including DST)."""
    start, end = day_bounds(day)
    expected = int((end - start).total_seconds() // 900)
    if not isinstance(rows, list) or len(rows) != expected:
        raise ValueError("incomplete_day")
    by_end = {}
    for item in rows:
        if not isinstance(item, dict) or item.get("business_date") != day.isoformat():
            raise ValueError("wrong_business_date")
        raw_price = item.get("rce_pln")
        if isinstance(raw_price, bool) or not isinstance(raw_price, (str, float, int)):
            raise ValueError("invalid_price")
        price = float(raw_price)
        if not isfinite(price):
            raise ValueError("nonfinite_price")
        instant = utc_time(item.get("dtime_utc"))
        if not start < instant <= end or (instant - start).total_seconds() % 900:
            raise ValueError("invalid_period_end")
        if instant in by_end:
            raise ValueError("duplicate_period")
        period = item.get("period_utc")
        match = _PERIOD.fullmatch(period) if isinstance(period, str) else None
        if match is None:
            raise ValueError("invalid_utc_period")
        sh, sm, eh, em = map(int, match.groups())
        if any(m > 59 for m in (sm, em)) or sh > 23 or eh > 24 or (eh == 24 and em):
            raise ValueError("invalid_utc_clock")
        if (eh * 60 + em - sh * 60 - sm) % 1440 != 15 or (eh * 60 + em) % 1440 != instant.hour * 60 + instant.minute:
            raise ValueError("inconsistent_utc_period")
        row = {"business_date": day.isoformat(), "rce_pln": price,
               "dtime_utc": instant.isoformat(), "period_utc": period}
        if isinstance(item.get("period"), str):
            row["period"] = item["period"]
        for field in ("publication_ts", "publication_ts_utc"):
            value = item.get(field)
            if value is not None:
                if not isinstance(value, str):
                    raise ValueError("invalid_publication_timestamp")
                if field.endswith("_utc") and utc_time(value) > now.astimezone(UTC) + timedelta(seconds=5):
                    raise ValueError("future_publication")
                row[field] = value
        by_end[instant] = row
    return [by_end[start + timedelta(minutes=15 * i)] for i in range(1, expected + 1)]


def payload_hash(rows: list[dict[str, Any]]) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def public_entry(entry: dict[str, Any]) -> dict[str, Any]:
    """Keep the legacy row shape below HA's attribute size limit.

    Full per-period publication metadata stays in Store. The entity exposes
    its range/count plus both hashes, without repeating two dates 100 times.
    """
    rows = [{k: v for k, v in row.items() if not k.startswith("publication_ts")}
            for row in entry["value"]]
    publications = sorted({row["publication_ts_utc"] for row in entry["value"]
                           if row.get("publication_ts_utc")}, key=utc_time)
    return {**entry, "value": rows, "payload_sha256": payload_hash(rows),
            "cache_payload_sha256": entry["payload_sha256"],
            "publication_first_utc": publications[0] if publications else None,
            "publication_last_utc": publications[-1] if publications else None,
            "publication_version_count": len(publications)}


def validate_entry(entry: Any, day: date, now: datetime) -> dict[str, Any]:
    if not isinstance(entry, dict) or entry.get("source") != SOURCE or entry.get("schema_version") != SCHEMA:
        raise ValueError("invalid_cache_source")
    if entry.get("business_date") != day.isoformat():
        raise ValueError("invalid_cache_date")
    rows = validate_rows(entry.get("value"), day, now)
    if payload_hash(rows) != entry.get("payload_sha256"):
        raise ValueError("cache_checksum_mismatch")
    fetched = _stored_time(entry.get("fetched_at"), now)
    checked = _stored_time(entry.get("last_checked_at"), now)
    if checked < fetched or fetched >= day_bounds(day)[1]:
        raise ValueError("invalid_fetch_chronology")
    # A day-ahead publication cannot have been fetched before the prior local day.
    if fetched < day_bounds(day - timedelta(days=1))[0]:
        raise ValueError("implausible_day_ahead_fetch")
    return {**entry, "value": rows}


def cached_state_valid(state: Any, day: date, now: datetime) -> bool:
    """Validate the native publisher's dated contract, not its HA report age."""
    if state is None or state.state in {"unknown", "unavailable"}:
        return False
    today = now.astimezone(WARSAW).date()
    if day not in (today, today + timedelta(days=1)):
        return False
    try:
        entry = validate_entry(dict(state.attributes), day, now)
        return str(state.state) == str(len(entry["value"]))
    except (ValueError, TypeError, OverflowError, KeyError):
        return False


class RCEPriceCache:
    """Two-day working set. Never preserve expired rows as current prices."""

    def __init__(self) -> None:
        self.days: dict[str, dict[str, Any]] = {}
        self.revision_checked_on: str | None = None

    def prune(self, now: datetime) -> bool:
        today = now.astimezone(WARSAW).date()
        keep = {today.isoformat(), (today + timedelta(days=1)).isoformat()}
        old = set(self.days)
        self.days = {key: value for key, value in self.days.items() if key in keep}
        return old != set(self.days)

    def load(self, payload: Any, now: datetime) -> None:
        self.days = {}
        self.revision_checked_on = None
        if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
            return
        days = payload.get("days")
        if not isinstance(days, dict):
            return
        today = now.astimezone(WARSAW).date()
        for day in (today, today + timedelta(days=1)):
            try:
                self.days[day.isoformat()] = validate_entry(days.get(day.isoformat()), day, now)
            except (ValueError, TypeError, OverflowError, KeyError):
                continue
        if payload.get("revision_checked_on") == today.isoformat():
            self.revision_checked_on = today.isoformat()

    def dump(self) -> dict[str, Any]:
        return deepcopy({"schema_version": SCHEMA, "days": self.days,
                         "revision_checked_on": self.revision_checked_on})

    def get(self, day: date, now: datetime) -> dict[str, Any] | None:
        today = now.astimezone(WARSAW).date()
        if day not in (today, today + timedelta(days=1)):
            return None
        entry = self.days.get(day.isoformat())
        if entry is None:
            return None
        try:
            return deepcopy(validate_entry(entry, day, now))
        except (ValueError, TypeError, OverflowError, KeyError):
            return None

    def accept(self, day: date, rows: Any, now: datetime) -> bool:
        self.prune(now)
        today = now.astimezone(WARSAW).date()
        if day not in (today, today + timedelta(days=1)):
            raise ValueError("date_outside_working_set")
        clean = validate_rows(rows, day, now)
        previous = self.days.get(day.isoformat())
        if previous:
            for old, new in zip(previous["value"], clean, strict=True):
                old_pub, new_pub = old.get("publication_ts_utc"), new.get("publication_ts_utc")
                if old_pub and (not new_pub or utc_time(new_pub) < utc_time(old_pub)):
                    raise ValueError("older_publication")
            if utc_time(previous["last_checked_at"]) > now.astimezone(UTC):
                raise ValueError("older_response")
        digest = payload_hash(clean)
        changed = previous is None or digest != previous["payload_sha256"]
        entry = {"schema_version": SCHEMA, "source": SOURCE, "business_date": day.isoformat(),
                 "value": clean, "payload_sha256": digest,
                 "fetched_at": now.astimezone(UTC).isoformat() if changed else previous["fetched_at"],
                 "last_checked_at": now.astimezone(UTC).isoformat()}
        self.days[day.isoformat()] = validate_entry(entry, day, now)
        return changed
