"""Admin-only, bounded YAML preparation. Never writes config or calls ESP."""
import asyncio
from functools import partial
import json

from aiohttp import web
from homeassistant.components.http.view import HomeAssistantView
from homeassistant.exceptions import Unauthorized

from .const import DOMAIN
from .esphome_upgrade import prepare_upgrade

MAX_BODY_BYTES = 1048576


class HoymilesEsphomeUpgradeView(HomeAssistantView):
    url = f"/api/{DOMAIN}/prepare-esphome-upgrade"
    name = f"api:{DOMAIN}:prepare_esphome_upgrade"
    requires_auth = True

    def __init__(self):
        super().__init__()
        self._lock = asyncio.Lock()

    async def post(self, request):
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            raise Unauthorized()
        if self._lock.locked():
            raise web.HTTPTooManyRequests(text="preparation_busy")
        async with self._lock:
            try:
                async with asyncio.timeout(10):
                    body = bytearray()
                    async for chunk in request.content.iter_chunked(16384):
                        body.extend(chunk)
                        if len(body) > MAX_BODY_BYTES:
                            raise web.HTTPRequestEntityTooLarge(max_size=MAX_BODY_BYTES, actual_size=len(body))
                    data = json.loads(body)
                    if not isinstance(data, dict) or set(data) - {"source", "transport", "local_packages"}:
                        raise ValueError()
                    result = await request.app["hass"].async_add_executor_job(partial(
                        prepare_upgrade, data.get("source"),
                        transport=data.get("transport", "unknown"), local_packages=data.get("local_packages"),
                    ))
            except (ValueError, UnicodeError, RecursionError):
                raise web.HTTPBadRequest(text="invalid_request") from None
            except TimeoutError:
                raise web.HTTPRequestTimeout(text="preparation_timeout") from None
        return web.json_response(result, headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
