"""Bounded, transport-injected client of Pstryk's public hourly net chart.

Uses the public getDailyPrices read action, without an API key. The caller owns
an aiohttp-compatible session dedicated to public requests (no cookies/default
Authorization). No HA entity or writer is registered by this module. The action
identifier is discovered from same-origin scripts, not executed or persisted.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from decimal import Decimal
import json
import re

try:
    from .pstryk_prices import PriceSnapshot, WARSAW, parse_prices, request_window, utc, _scope
except ImportError:  # Standalone deterministic tests.
    from pstryk_prices import PriceSnapshot, WARSAW, parse_prices, request_window, utc, _scope

ORIGIN = "https://www.pstryk.pl"
ENDPOINT = ORIGIN + "/ceny"
MAX_RESPONSE_BYTES = 128 * 1024
MAX_PAGE_BYTES = 512 * 1024
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_DISCOVERY_BYTES = 4 * 1024 * 1024
MAX_SCRIPT_ASSETS = 32
REQUEST_TIMEOUT_SECONDS = 15
FETCH_TIMEOUT_SECONDS = 45
_ASSET = re.compile(r'/_next/static/chunks/[A-Za-z0-9_-]+\.js')
_ACTION = re.compile(r'createServerReference\)\("([a-f0-9]{40,64})"[^;]{0,200}"getDailyPrices"\)')


class PstrykAPIError(Exception):
    """Only a stable, non-sensitive reason crosses the adapter boundary."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def decode_public_response(raw: bytes) -> dict:
    """Decode only the JSON result referenced by the public action envelope.

    Unpublished-day null is empty coverage, never yesterday's price. The RSC stream
    is data: no JavaScript evaluation, guessed price extraction or gross fallback.
    """
    if not isinstance(raw, bytes) or len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response_size_invalid")
    records = {}
    for line in raw.decode("utf-8").splitlines():
        if not line:
            continue
        key, separator, value = line.partition(":")
        if not separator or not re.fullmatch(r"[a-f0-9]+", key) or key in records:
            raise ValueError("response_envelope_invalid")
        records[key] = value
    envelope = json.loads(records.get("0", "null"))
    target = envelope.get("a") if isinstance(envelope, dict) else None
    if not isinstance(target, str) or not re.fullmatch(r"\$@[a-f0-9]+", target):
        raise ValueError("response_reference_invalid")
    payload = json.loads(records.get(target[2:], ""), parse_float=Decimal)
    if payload is None:
        return {"frames": []}
    if not isinstance(payload, dict) or not isinstance(payload.get("frames"), list):
        raise ValueError("response_payload_invalid")
    # The public chart also represents an unpublished day as 24 zero frames
    # with a null net average. That metadata is missing coverage, not free
    # electricity. A published average of zero keeps genuine zero/negative hours.
    if "priceNetAvg" in payload and payload["priceNetAvg"] is None:
        return {"frames": []}
    return payload


class PstrykClient:
    def __init__(self, session, *, source_scope: str):
        # The HA adapter must own an anonymous session with a disabled cookie jar.
        headers = getattr(session, "headers", {})
        jar = getattr(session, "cookie_jar", None)
        if (getattr(session, "auth", None) or getattr(session, "_default_auth", None)
                or (jar is not None and type(jar).__name__ != "DummyCookieJar")
                or any(key.lower() in {"authorization", "cookie", "proxy-authorization"}
                       for key in headers)):
            raise ValueError("public_session_must_be_anonymous")
        self._session = session
        self._scope = _scope(source_scope)
        self._action_id: str | None = None
        self._task: asyncio.Task | None = None
        self._window: tuple[datetime, datetime] | None = None
        self._closed = False

    async def fetch(self, *, now: datetime) -> PriceSnapshot:
        stamp = utc(now)
        start, end = request_window(stamp)
        return await self.fetch_window(start=start, end=end, received_at=stamp)

    async def fetch_window(self, *, start: datetime, end: datetime,
                           received_at: datetime) -> PriceSnapshot:
        """Read one or two complete Warsaw days without backdating receipt."""
        stamp = utc(received_at)
        window = (utc(start), utc(end))
        local_start, local_end = (value.astimezone(WARSAW) for value in window)
        if (local_start.time() != time.min or local_end.time() != time.min
                or not 1 <= (local_end.date() - local_start.date()).days <= 2):
            raise ValueError("request_window_invalid")
        if self._closed:
            raise PstrykAPIError("client_closed")
        while self._task is not None and not self._task.done() and self._window != window:
            try:
                await asyncio.shield(self._task)
            except PstrykAPIError:
                pass
            if self._closed:
                raise PstrykAPIError("client_closed")
        if self._task is None or self._task.done() or self._window != window:
            self._window = window
            self._task = asyncio.create_task(self._fetch(stamp, window))
            self._task.add_done_callback(self._observe_completion)
        return await asyncio.shield(self._task)

    @staticmethod
    def _observe_completion(task):
        if not task.cancelled():
            task.exception()

    async def close(self) -> None:
        self._closed = True
        task = self._task
        if task is not None and not task.done():
            task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        self._task = None
        self._action_id = None

    async def _read(self, method: str, url: str, *, limit: int, **kwargs) -> bytes:
        async with self._session.request(
            method, url, timeout=REQUEST_TIMEOUT_SECONDS, allow_redirects=False, **kwargs,
        ) as response:
            if response.status in (401, 403):
                raise PstrykAPIError("source_unavailable")
            if response.status == 429:
                raise PstrykAPIError("rate_limited")
            if 500 <= response.status < 600:
                raise PstrykAPIError("server_error")
            if response.status != 200:
                raise PstrykAPIError("invalid_response")
            data = bytearray()
            while chunk := await response.content.read(min(16384, limit + 1 - len(data))):
                data.extend(chunk)
                if len(data) > limit:
                    raise PstrykAPIError("invalid_response")
            return bytes(data)

    async def _discover_action(self) -> str:
        page = await self._read("GET", ENDPOINT, limit=MAX_PAGE_BYTES)
        paths = list(dict.fromkeys(re.findall(r'<script[^>]+src="([^"]+)"', page.decode("utf-8"))))
        # Read literal same-origin assets only; no JS execution or foreign hosts.
        paths = [path for path in paths if _ASSET.fullmatch(path)]
        if not paths or len(paths) > MAX_SCRIPT_ASSETS:
            raise PstrykAPIError("invalid_response")
        remaining = MAX_DISCOVERY_BYTES - len(page)
        for path in reversed(paths):
            if remaining <= 0:
                raise PstrykAPIError("invalid_response")
            script = await self._read("GET", ORIGIN + path, limit=min(MAX_SCRIPT_BYTES, remaining))
            remaining -= len(script)
            matches = set(_ACTION.findall(script.decode("utf-8")))
            if len(matches) > 1:
                raise PstrykAPIError("invalid_response")
            if matches:
                return matches.pop()
        raise PstrykAPIError("invalid_response")

    async def _fetch(self, stamp: datetime, window: tuple[datetime, datetime]) -> PriceSnapshot:
        start, end = window
        try:
            async with asyncio.timeout(FETCH_TIMEOUT_SECONDS):
                if self._action_id is None:
                    self._action_id = await self._discover_action()
                day = start.astimezone(WARSAW).date()
                last = end.astimezone(WARSAW).date()
                frames = []
                while day < last:
                    raw = await self._read(
                        "POST", ENDPOINT, limit=MAX_RESPONSE_BYTES,
                        headers={"Next-Action": self._action_id, "Accept": "text/x-component",
                                 "Content-Type": "text/plain;charset=UTF-8", "Origin": ORIGIN},
                        data=json.dumps([day.isoformat()]),
                    )
                    left = utc(datetime.combine(day, time.min, tzinfo=WARSAW))
                    right = utc(datetime.combine(day + timedelta(days=1), time.min, tzinfo=WARSAW))
                    daily = parse_prices(decode_public_response(raw), source_scope=self._scope,
                                         fetched_at=stamp, window_start=left, window_end=right)
                    frames.extend(row.as_row() for row in daily.hours)
                    day += timedelta(days=1)
                return parse_prices({"frames": frames}, source_scope=self._scope, fetched_at=stamp,
                                    window_start=start, window_end=end)
        except PstrykAPIError as error:
            # Rediscover a changed build at the next scheduled fetch, not in a loop.
            if error.reason in {"invalid_response", "server_error"}:
                self._action_id = None
            raise
        except (ValueError, TypeError, OverflowError, RecursionError):
            self._action_id = None
            raise PstrykAPIError("invalid_response") from None
        except TimeoutError:
            raise PstrykAPIError("timeout") from None
        except asyncio.CancelledError:
            raise
        except Exception:
            raise PstrykAPIError("network_error") from None
