"""Bounded public Pstryk net prices, independent of execution authority.

No HA entity or device writer is registered here. Prices are the provider's
public priceNet PLN/kWh, used unchanged for both BUY and SELL by explicit policy.
No account, gross price or fee is part of this source. Receipt time determines
freshness, not semantic revision. Cache storage is an explicit two-phase handoff:
the caller acknowledges a candidate only after a successful serialized save.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from zoneinfo import ZoneInfo

UTC = timezone.utc
WARSAW = ZoneInfo("Europe/Warsaw")
HOUR = timedelta(hours=1)
HALF_HOUR = timedelta(minutes=30)
PRICE_UNIT = "PLN/kWh"
PRICE_BASIS = "public_net_same_buy_sell"
CONTRACT = "pstryk_public_net_hour_v1"
MAX_FRAMES = 50
MAX_CACHE_BYTES = 32 * 1024
DEFAULT_CACHE_AGE = timedelta(hours=2)
_TEMPORARY_FAILURES = frozenset({"timeout", "network_error", "rate_limited", "server_error"})
_BLOCKING_FAILURES = frozenset({"source_unavailable", "invalid_response"})


def utc(value: datetime) -> datetime:
    """Reject naive timestamps; comparisons always use real UTC instants."""
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp_timezone_missing")
    return value.astimezone(UTC)


def _stamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp_invalid")
    try:
        return utc(datetime.fromisoformat(value))
    except (ValueError, OverflowError):
        raise ValueError("timestamp_invalid") from None


def _range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start, end = utc(start), utc(end)
    if end <= start:
        raise ValueError("interval_invalid")
    return start, end


def _scope(value: object) -> str:
    # A local entry/profile epoch. The public source has no account or token.
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError("source_scope_invalid")
    return value


def _price(value: object) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("price_invalid")
    text = str(value).strip().replace(",", ".")
    if not text or len(text) > 64:
        raise ValueError("price_invalid")
    try:
        result = Decimal(text)
        numeric = float(result)
        if not result.is_finite() or not math.isfinite(numeric) or (result and numeric == 0):
            raise ValueError("price_invalid")
    except (InvalidOperation, ValueError, OverflowError):
        raise ValueError("price_invalid") from None
    return result


def _price_text(value: Decimal | None) -> str | None:
    if value is None:
        return None
    if not value:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def request_window(now: datetime) -> tuple[datetime, datetime]:
    """Two Warsaw calendar days, i.e. 47/48/49 UTC hours around DST."""
    day = utc(now).astimezone(WARSAW).date()
    start = datetime.combine(day, time.min, tzinfo=WARSAW)
    end = datetime.combine(day + timedelta(days=2), time.min, tzinfo=WARSAW)
    return utc(start), utc(end)


@dataclass(frozen=True, slots=True)
class PriceHour:
    start: datetime
    end: datetime
    net: Decimal | None

    @property
    def buy(self) -> Decimal | None:
        return self.net

    @property
    def sell(self) -> Decimal | None:
        return self.net

    def as_row(self) -> dict[str, object]:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(),
                "priceNet": _price_text(self.net)}


@dataclass(frozen=True, slots=True)
class PriceSlice:
    start: datetime
    end: datetime
    source_start: datetime
    net: Decimal

    @property
    def buy(self) -> Decimal:
        return self.net

    @property
    def sell(self) -> Decimal:
        return self.net

    @property
    def hours(self) -> float:
        return (self.end - self.start).total_seconds() / 3600


@dataclass(frozen=True, slots=True)
class PriceSnapshot:
    source_scope: str
    fetched_at: datetime
    window_start: datetime
    window_end: datetime
    hours: tuple[PriceHour, ...]
    revision: str

    def at(self, when: datetime) -> PriceHour | None:
        stamp = utc(when)
        return next((row for row in self.hours if row.start <= stamp < row.end), None)

    def covers(self, start: datetime, end: datetime, *, directions=("buy", "sell")) -> bool:
        cursor, end = _range(start, end)
        if not directions or any(item not in {"buy", "sell"} for item in directions):
            raise ValueError("price_direction_invalid")
        for row in self.hours:
            if row.end <= cursor:
                continue
            if row.start > cursor or any(getattr(row, item) is None for item in directions):
                return False
            cursor = min(end, row.end)
            if cursor == end:
                return True
        return False

    def slices(self, start: datetime, end: datetime) -> tuple[PriceSlice, ...]:
        """Price internal half-hours without adding time or filling gaps."""
        cursor, end = _range(start, end)
        if not self.covers(cursor, end):
            raise ValueError("price_coverage_missing")
        result = []
        while cursor < end:
            row = self.at(cursor)
            assert row is not None and row.buy is not None and row.sell is not None
            next_half = cursor.replace(minute=(cursor.minute // 30) * 30, second=0, microsecond=0) + HALF_HOUR
            stop = min(end, row.end, next_half)
            result.append(PriceSlice(cursor, stop, row.start, row.net))
            cursor = stop
        return tuple(result)


def parse_prices(payload: object, *, source_scope: str, fetched_at: datetime,
                 window_start: datetime, window_end: datetime,
                 unit: str = PRICE_UNIT) -> PriceSnapshot:
    """Validate public chart frames; priceNet is the only economic input.

    Malformed structure/value rejects the response. Absent/null prices are kept
    as missing and coverage fails for both directions. An empty successful
    response is empty, never permission to reuse a previous publication.
    """
    source_scope = _scope(source_scope)
    fetched_at = utc(fetched_at)
    start, end = _range(window_start, window_end)
    if unit != PRICE_UNIT:
        raise ValueError("price_unit_invalid")
    if end - start > MAX_FRAMES * HOUR or any(
        x.minute or x.second or x.microsecond for x in (start, end)
    ):
        raise ValueError("request_window_invalid")
    if not isinstance(payload, Mapping) or not isinstance(payload.get("frames"), list):
        raise ValueError("frames_invalid")
    if len(payload["frames"]) > MAX_FRAMES:
        raise ValueError("frames_limit")
    indexed = {}
    for frame in payload["frames"]:
        if not isinstance(frame, Mapping):
            raise ValueError("frame_invalid")
        left, right = _stamp(frame.get("start")), _stamp(frame.get("end"))
        if (right - left != HOUR or left.minute or left.second or left.microsecond
                or left < start or right > end):
            raise ValueError("hour_interval_invalid")
        row = PriceHour(left, right, _price(frame.get("priceNet")))
        if left in indexed and indexed[left] != row:
            raise ValueError("duplicate_conflict")
        indexed[left] = row
    rows = tuple(indexed[key] for key in sorted(indexed))
    semantic = {"contract": CONTRACT, "scope": source_scope, "unit": unit,
                "basis": PRICE_BASIS, "start": start.isoformat(), "end": end.isoformat(),
                "hours": [row.as_row() for row in rows]}
    revision = hashlib.sha256(_json(semantic).encode()).hexdigest()
    return PriceSnapshot(source_scope, fetched_at, start, end, rows, revision)


@dataclass(frozen=True, slots=True)
class PriceView:
    snapshot: PriceSnapshot | None
    quality: str
    reason: str


@dataclass(frozen=True, slots=True)
class CacheWrite:
    key: str
    payload_json: str


class PriceCache:
    """One bounded snapshot, with conservative restart and explicit save ACK.

    Receipt checkpoints are coalesced within a UTC hour. Content changes and
    hard invalidation require a new save. The HA adapter must serialize writes;
    this pure component does not start workers or write files itself.
    """
    def __init__(self, source_scope: str, *, max_age: timedelta = DEFAULT_CACHE_AGE):
        self.source_scope = _scope(source_scope)
        if not isinstance(max_age, timedelta) or not timedelta(0) < max_age <= DEFAULT_CACHE_AGE:
            raise ValueError("cache_age_invalid")
        self.max_age = max_age
        self._snapshot: PriceSnapshot | None = None
        self._blocking: str | None = None
        self._cached = False
        self._saved_key: str | None = None

    def accept(self, snapshot: PriceSnapshot, *, now: datetime) -> None:
        now = utc(now)
        if not isinstance(snapshot, PriceSnapshot) or snapshot.source_scope != self.source_scope:
            raise ValueError("snapshot_scope_mismatch")
        if snapshot.fetched_at > now:
            raise ValueError("snapshot_future")
        if self._snapshot and snapshot.fetched_at < self._snapshot.fetched_at:
            raise ValueError("snapshot_older")
        self._snapshot, self._blocking, self._cached = snapshot, None, False

    def failed(self, reason: str) -> None:
        if reason in _BLOCKING_FAILURES:
            self._blocking = reason
        elif reason not in _TEMPORARY_FAILURES:
            raise ValueError("failure_reason_invalid")
        self._cached = True

    def view(self, *, now: datetime) -> PriceView:
        now = utc(now)
        if self._blocking:
            return PriceView(None, "unavailable", self._blocking)
        value = self._snapshot
        if value is None:
            return PriceView(None, "unavailable", "not_loaded")
        age = now - value.fetched_at
        if age < timedelta(0):
            return PriceView(None, "unavailable", "clock_rollback")
        if age > self.max_age:
            return PriceView(None, "unavailable", "cache_expired")
        return PriceView(value, "cached" if self._cached else "official", "ok")

    def storage_candidate(self) -> CacheWrite | None:
        value = self._snapshot
        if value is None and self._blocking is None:
            return None
        payload = {"schema": 2, "contract": CONTRACT, "basis": PRICE_BASIS,
                   "scope": self.source_scope, "blocking": self._blocking,
                   "snapshot": None if value is None else {
                       "fetched_at": value.fetched_at.isoformat(),
                       "window_start": value.window_start.isoformat(),
                       "window_end": value.window_end.isoformat(),
                       "revision": value.revision,
                       "hours": [row.as_row() for row in value.hours],
                   }}
        key = _json((value.revision if value else None, self._blocking,
                     value.fetched_at.replace(minute=0, second=0, microsecond=0).isoformat() if value else None))
        if key == self._saved_key:
            return None
        encoded = _json(payload)
        if len(encoded.encode()) > MAX_CACHE_BYTES:
            raise ValueError("cache_size_limit")
        return CacheWrite(key, encoded)

    def ack_storage(self, write: CacheWrite) -> None:
        # ACK of an earlier write must not mark a newer revision as persisted.
        self._saved_key = write.key

    @classmethod
    def restore(cls, payload_json: str, *, source_scope: str,
                max_age: timedelta = DEFAULT_CACHE_AGE) -> PriceCache:
        if not isinstance(payload_json, str) or len(payload_json.encode()) > MAX_CACHE_BYTES:
            raise ValueError("cache_size_limit")
        try:
            data = json.loads(payload_json)
            if (not isinstance(data, dict) or type(data.get("schema")) is not int
                    or data["schema"] != 2 or data.get("contract") != CONTRACT
                    or data.get("basis") != PRICE_BASIS):
                raise ValueError("cache_schema_invalid")
            if data.get("scope") != source_scope:
                raise ValueError("cache_scope_mismatch")
            result = cls(source_scope, max_age=max_age)
            record = data.get("snapshot")
            if record is not None:
                rows = record["hours"]
                if not isinstance(rows, list) or len(rows) > MAX_FRAMES:
                    raise ValueError("cache_frames_invalid")
                response = {"frames": rows}
                result._snapshot = parse_prices(
                    response, source_scope=source_scope, fetched_at=_stamp(record["fetched_at"]),
                    window_start=_stamp(record["window_start"]), window_end=_stamp(record["window_end"]),
                )
                if result._snapshot.revision != record["revision"]:
                    raise ValueError("cache_revision_mismatch")
            blocking = data.get("blocking")
            if blocking is not None and blocking not in _BLOCKING_FAILURES:
                raise ValueError("cache_status_invalid")
            result._blocking, result._cached = blocking, True
            pending = result.storage_candidate()
            if pending is not None:
                result.ack_storage(pending)
            return result
        except (KeyError, TypeError, ValueError, RecursionError):
            raise ValueError("cache_invalid") from None


def price_projection(view: PriceView, *, now: datetime) -> dict[str, object]:
    """Stable historical price summary, not a command or complete live cache."""
    stamp = utc(now)
    row = view.snapshot.at(stamp) if view.snapshot is not None else None
    reason, quality = view.reason, view.quality
    if view.snapshot is not None and row is None:
        reason, quality = "current_hour_missing", "unavailable"
    elif row is not None and (row.buy is None or row.sell is None):
        reason, quality = "net_price_missing", "unavailable"
    return {
        "schema": 2, "source": "pstryk", "unit": PRICE_UNIT, "basis": PRICE_BASIS,
        "revision": view.snapshot.revision if view.snapshot else None,
        "quality": quality, "reason": reason,
        "hour_start": row.start.isoformat() if row else None,
        "hour_end": row.end.isoformat() if row else None,
        "net_pln_kwh": float(row.net) if row and row.net is not None else None,
    }
