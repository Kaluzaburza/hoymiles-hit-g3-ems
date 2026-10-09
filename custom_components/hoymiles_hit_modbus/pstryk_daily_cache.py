"""Complete Warsaw-day Pstryk prices, retained like the native RCE cache.

The dated publication remains valid for its delivery day, including after a
restart or failed correction request. Network failures never grant missing
coverage. Only today's and tomorrow's verified net prices are retained.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json

try:
    from .pstryk_prices import (
        CacheWrite, CONTRACT, HOUR, MAX_CACHE_BYTES, PRICE_BASIS, PriceCache,
        PriceSnapshot, PriceView, WARSAW, _json, _scope, _stamp, parse_prices, utc,
    )
except ImportError:  # Standalone deterministic tests.
    from pstryk_prices import (
        CacheWrite, CONTRACT, HOUR, MAX_CACHE_BYTES, PRICE_BASIS, PriceCache,
        PriceSnapshot, PriceView, WARSAW, _json, _scope, _stamp, parse_prices, utc,
    )


def day_bounds(day: date) -> tuple[datetime, datetime]:
    return tuple(utc(datetime.combine(d, time.min, WARSAW))
                 for d in (day, day + timedelta(days=1)))


def daily_snapshot_valid(snapshot: PriceSnapshot, day: date, now: datetime) -> bool:
    """Date-valid optimizer input; its real fetch age is not reset to zero."""
    try:
        today = utc(now).astimezone(WARSAW).date()
        return (isinstance(snapshot, PriceSnapshot)
                and day in (today, today + timedelta(days=1))
                and day_bounds(today - timedelta(days=1))[0] <= snapshot.fetched_at <= utc(now)
                and snapshot.covers(*day_bounds(day)))
    except (ValueError, TypeError, AttributeError):
        return False


class DailyPriceCache:
    """Two-phase Store handoff, complete days and bounded missing-day retries."""

    def __init__(self, source_scope: str):
        self.source_scope = _scope(source_scope)
        self.days: dict[str, PriceSnapshot] = {}
        self.revision_checked_on: str | None = None
        self.retry_at: dict[str, datetime] = {}
        self.attempts: dict[str, int] = {}
        self.last_error: str | None = None
        self._saved_key: str | None = None
        self._cached = False

    def prune(self, now: datetime) -> None:
        today = utc(now).astimezone(WARSAW).date()
        keep = {today.isoformat(), (today + timedelta(days=1)).isoformat()}
        self.days = {key: value for key, value in self.days.items() if key in keep}
        self.retry_at = {key: value for key, value in self.retry_at.items() if key in keep}
        self.attempts = {key: value for key, value in self.attempts.items() if key in keep}
        if self.revision_checked_on != today.isoformat():
            self.revision_checked_on = None

    def _validate_day(self, value: PriceSnapshot, now: datetime) -> None:
        start, end = day_bounds(value.window_start.astimezone(WARSAW).date())
        if (value.source_scope != self.source_scope
                or (value.window_start, value.window_end) != (start, end)
                or value.fetched_at > utc(now)
                or value.fetched_at < day_bounds(start.astimezone(WARSAW).date() - timedelta(days=1))[0]
                or value.fetched_at >= end
                or len(value.hours) != int((end-start)/HOUR)
                or not value.covers(start, end)):
            raise ValueError('incomplete_or_invalid_day')

    def get(self, day: date, now: datetime) -> PriceSnapshot | None:
        today = utc(now).astimezone(WARSAW).date()
        if day not in (today, today + timedelta(days=1)):
            return None
        value = self.days.get(day.isoformat())
        if value is not None:
            try:
                self._validate_day(value, now)
            except ValueError:
                return None
        return value

    def accept(self, snapshot: PriceSnapshot, *, now: datetime) -> None:
        """Accept whole days atomically; partial/newly unpublished data is rejected."""
        if not isinstance(snapshot, PriceSnapshot) or snapshot.source_scope != self.source_scope:
            raise ValueError('snapshot_scope_mismatch')
        first = snapshot.window_start.astimezone(WARSAW).date()
        last = snapshot.window_end.astimezone(WARSAW).date()
        if (not 1 <= (last-first).days <= 2 or snapshot.window_start != day_bounds(first)[0]
                or snapshot.window_end != day_bounds(last)[0]):
            raise ValueError('request_window_invalid')
        today = utc(now).astimezone(WARSAW).date()
        clean = {}
        for offset in range((last-first).days):
            day = first + timedelta(days=offset)
            start, end = day_bounds(day)
            if day not in (today, today + timedelta(days=1)):
                raise ValueError('date_outside_working_set')
            value = parse_prices({'frames': [h.as_row() for h in snapshot.hours if start <= h.start < end]},
                source_scope=self.source_scope, fetched_at=snapshot.fetched_at, window_start=start, window_end=end)
            self._validate_day(value, now)
            previous = self.days.get(day.isoformat())
            if previous and value.fetched_at < previous.fetched_at:
                raise ValueError('snapshot_older')
            # A correction check does not rejuvenate an identical publication.
            clean[day.isoformat()] = previous if previous and previous.revision == value.revision else value
        self.prune(now)
        self.days.update(clean)
        for key in clean:
            self.retry_at.pop(key, None)
            self.attempts.pop(key, None)
        if today.isoformat() in clean:
            self.revision_checked_on = today.isoformat()
        self.last_error, self._cached = None, False

    def due_days(self, now: datetime) -> tuple[date, ...]:
        today = utc(now).astimezone(WARSAW).date()
        result = []
        for day in (today, today + timedelta(days=1)):
            cached = self.get(day, now)
            if cached:
                if day == today and self.revision_checked_on != day.isoformat():
                    result.append(day)
            elif utc(now) >= self.retry_at.get(day.isoformat(), utc(now)):
                result.append(day)
        return tuple(result)

    def before_fetch(self, day: date, now: datetime) -> None:
        # Persist before await, so an interrupted fetch/restart cannot poll-loop.
        key = day.isoformat()
        if self.get(day, now) and day == utc(now).astimezone(WARSAW).date():
            self.revision_checked_on = key
        count = min(self.attempts.get(key, 0) + 1, 3)
        self.attempts[key] = count
        self.retry_at[key] = utc(now) + timedelta(minutes=15 * 2 ** (count-1))

    def failed(self, reason: str) -> None:
        self.last_error, self._cached = reason, True

    def view(self, *, now: datetime) -> PriceView:
        today = utc(now).astimezone(WARSAW).date()
        values = [self.get(day, now) for day in (today, today + timedelta(days=1))]
        values = [value for value in values if value is not None]
        if not values:
            return PriceView(None, 'unavailable', self.last_error or 'not_loaded')
        snapshot = parse_prices({'frames': [h.as_row() for value in values for h in value.hours]},
            source_scope=self.source_scope, fetched_at=min(value.fetched_at for value in values),
            window_start=day_bounds(today)[0], window_end=day_bounds(today + timedelta(days=1))[1])
        return PriceView(snapshot, 'cached' if self._cached else 'official', 'ok')

    def storage_candidate(self) -> CacheWrite | None:
        payload = {'schema': 3, 'contract': CONTRACT, 'basis': PRICE_BASIS, 'scope': self.source_scope,
            'revision_checked_on': self.revision_checked_on,
            'retry_at': {k: v.isoformat() for k,v in self.retry_at.items()}, 'attempts': self.attempts,
            'days': {key: {'fetched_at': value.fetched_at.isoformat(), 'revision': value.revision,
                          'hours': [h.as_row() for h in value.hours]} for key,value in self.days.items()}}
        encoded = _json(payload)
        if len(encoded.encode()) > MAX_CACHE_BYTES:
            raise ValueError('cache_size_limit')
        return None if encoded == self._saved_key else CacheWrite(encoded, encoded)

    def ack_storage(self, write: CacheWrite) -> None:
        self._saved_key = write.key

    @classmethod
    def restore(cls, payload_json: str, *, source_scope: str, now: datetime):
        if not isinstance(payload_json, str) or len(payload_json.encode()) > MAX_CACHE_BYTES:
            raise ValueError('cache_size_limit')
        data = json.loads(payload_json)
        if not isinstance(data, dict):
            raise ValueError('cache_schema_invalid')
        result = cls(source_scope)
        if data.get('schema') == 2:
            # Validate legacy checksum/scope/net basis first; migrate only complete
            # date-valid days. Old transport failure/TTL does not revoke a publication.
            legacy = PriceCache.restore(payload_json, source_scope=source_scope)
            value = legacy._snapshot
            if value:
                today = utc(now).astimezone(WARSAW).date()
                for day in (today, today + timedelta(days=1)):
                    start, end = day_bounds(day)
                    single = parse_prices({'frames': [h.as_row() for h in value.hours if start <= h.start < end]},
                        source_scope=source_scope, fetched_at=value.fetched_at, window_start=start, window_end=end)
                    try:
                        result.accept(single, now=now)
                    except ValueError:
                        continue
                # A migrated day still gets the normal single correction check.
                result.revision_checked_on = None
        else:
            if (type(data.get('schema')) is not int or data.get('schema') != 3 or data.get('contract') != CONTRACT
                    or data.get('basis') != PRICE_BASIS or data.get('scope') != source_scope
                    or not isinstance(data.get('days'), dict) or len(data['days']) > 2
                    or not isinstance(data.get('retry_at', {}), dict)
                    or not isinstance(data.get('attempts', {}), dict)):
                raise ValueError('cache_schema_invalid')
            for key, record in data['days'].items():
                if not isinstance(record, dict):
                    raise ValueError('cache_record_invalid')
                start, end = day_bounds(date.fromisoformat(key))
                value = parse_prices({'frames': record['hours']}, source_scope=source_scope,
                    fetched_at=_stamp(record['fetched_at']), window_start=start, window_end=end)
                result._validate_day(value, now)
                if value.revision != record['revision']:
                    raise ValueError('cache_revision_mismatch')
                result.days[key] = value
            result.revision_checked_on = data.get('revision_checked_on')
            for key, stamp in data.get('retry_at', {}).items():
                if len(result.retry_at) >= 2 or _stamp(stamp) > utc(now) + timedelta(hours=1):
                    raise ValueError('cache_retry_invalid')
                count = data.get('attempts', {}).get(key)
                if type(count) is not int or not 1 <= count <= 3:
                    raise ValueError('cache_retry_invalid')
                result.retry_at[key], result.attempts[key] = _stamp(stamp), count
        result.prune(now)
        result._cached = True
        # Leave migration/pruning pending for the real serialized Store ACK.
        if result.storage_candidate().payload_json == payload_json:
            result.ack_storage(result.storage_candidate())
        return result
