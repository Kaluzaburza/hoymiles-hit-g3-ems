"""Authenticated HTTP download for Hoymiles support diagnostics."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from aiohttp import web

from homeassistant.components.http.view import HomeAssistantView
from homeassistant.const import __version__ as HOME_ASSISTANT_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import Unauthorized

from .const import DOMAIN
from .diagnostic_bundle import MAX_REPORTS, build_support_archive
from .diagnostics import async_get_config_entry_diagnostics
from .installation_identity import async_get_or_create_installation_identity


SUPPORT_BUNDLE_URL = f"/api/{DOMAIN}/support-bundle"
REPORT_COLLECTION_TIMEOUT_SECONDS = 15
TOTAL_COLLECTION_TIMEOUT_SECONDS = 18


def _report_omission(
    *,
    report_index: int,
    reason: str,
    error_type: str,
    omitted_report_count_at_least: int = 1,
) -> dict[str, object]:
    """Return a safe marker for evidence omitted during collection."""

    return {
        "diagnostic_omission": True,
        "omission_schema_version": 1,
        "scope": "report_collection",
        "omission_reason": reason,
        "report_index": report_index,
        "omitted_report_count_at_least": omitted_report_count_at_least,
        "error_type": error_type,
    }


class HoymilesSupportBundleView(HomeAssistantView):
    """Generate a browser-downloadable diagnostic ZIP for administrators."""

    url = SUPPORT_BUNDLE_URL
    name = f"api:{DOMAIN}:support_bundle"
    requires_auth = True

    def __init__(self) -> None:
        super().__init__()
        self._collection_lock = asyncio.Lock()

    async def get(self, request: web.Request) -> web.Response:
        """Return a fresh ZIP without persisting it in /config."""
        user = request.get("hass_user")
        if user is None or not user.is_admin:
            raise Unauthorized()

        hass: HomeAssistant = request.app["hass"]
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise web.HTTPNotFound(text="Hoymiles integration is not configured")

        if self._collection_lock.locked():
            raise web.HTTPTooManyRequests(
                text="A diagnostic export is already running; retry shortly",
                headers={"Cache-Control": "no-store", "Retry-After": "5"},
            )

        async with self._collection_lock:
            installation_identity = (
                await async_get_or_create_installation_identity(hass)
            )
            # Build at most one report beyond the export limit.  Collection is
            # sequential and time-bounded so one export cannot fan out into
            # concurrent Recorder work.
            reports: list[dict[str, object]] = []
            loop = asyncio.get_running_loop()
            deadline = loop.time() + TOTAL_COLLECTION_TIMEOUT_SECONDS
            selected_entries = entries[: MAX_REPORTS + 1]
            for report_index, entry in enumerate(selected_entries):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    reports.append(
                        _report_omission(
                            report_index=report_index,
                            reason="total_collection_timeout",
                            error_type="TimeoutError",
                            omitted_report_count_at_least=(
                                len(selected_entries) - report_index
                            ),
                        )
                    )
                    break
                try:
                    report = await asyncio.wait_for(
                        async_get_config_entry_diagnostics(hass, entry),
                        timeout=min(REPORT_COLLECTION_TIMEOUT_SECONDS, remaining),
                    )
                except TimeoutError:
                    reports.append(
                        _report_omission(
                            report_index=report_index,
                            reason="report_collection_timeout",
                            error_type="TimeoutError",
                            omitted_report_count_at_least=(
                                len(selected_entries) - report_index
                            ),
                        )
                    )
                    break
                except Exception as err:  # noqa: BLE001 - return partial ZIP
                    reports.append(
                        _report_omission(
                            report_index=report_index,
                            reason="report_collection_error",
                            error_type=type(err).__name__,
                        )
                    )
                else:
                    reports.append(report)
            now = datetime.now(timezone.utc)
            build_archive = partial(
                build_support_archive,
                reports,
                log_path=Path(hass.config.path("home-assistant.log")),
                generated_at=now.isoformat(),
                home_assistant_version=HOME_ASSISTANT_VERSION,
                **installation_identity.as_dict(),
            )
            archive = await hass.async_add_executor_job(build_archive)
        filename = f"hoymiles_diagnostics_{now:%Y%m%dT%H%M%SZ}.zip"
        return web.Response(
            body=archive,
            content_type="application/zip",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "X-Content-Type-Options": "nosniff",
            },
        )
