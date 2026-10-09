"""Authenticated, bounded archive reads. No Recorder query and no control."""
from datetime import date
import sqlite3

from aiohttp import web
from homeassistant.components.http.view import HomeAssistantView
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN


class HoymilesProfitView(HomeAssistantView):
    url = f"/api/{DOMAIN}/profits"
    name = f"api:{DOMAIN}:profits"
    requires_auth = True

    async def get(self, request):
        hass = request.app["hass"]
        row = er.async_get(hass).async_get(request.query.get("entity_id", ""))
        if (row is None or row.platform != DOMAIN
                or row.unique_id != f"{row.config_entry_id}_ems_supervisor"):
            raise web.HTTPBadRequest(text="unknown_supervisor")
        runtime = hass.data.get(DOMAIN, {}).get(row.config_entry_id)
        manager = getattr(runtime, "profits", None)
        if manager is None or not manager.ready:
            raise web.HTTPServiceUnavailable(text="archive_unavailable")
        user = request.get("hass_user")
        if user is None or any(not user.permissions.check_entity(entity, "read")
                               for entity in (row.entity_id, *manager.sources.values())):
            raise web.HTTPForbidden()
        period = request.query.get("period", "day")
        selected = request.query.get("date", "")
        try:
            day = date.fromisoformat(selected)
            offset = int(request.query.get("tariff_offset", "0"))
            if not 2000 <= day.year <= 2100 or period not in {"day", "week", "month", "year"}:
                raise ValueError("invalid_period")
            if not 0 <= offset <= 100000:
                raise ValueError("invalid_offset")
            return self.json(await manager.read(period, selected, offset))
        except ValueError as err:
            raise web.HTTPBadRequest(text="invalid_period_or_archive") from err
        except (sqlite3.Error, OSError) as err:
            raise web.HTTPServiceUnavailable(text="archive_read_failed") from err
