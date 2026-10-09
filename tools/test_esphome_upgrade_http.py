"""Real HA/aiohttp boundary: auth, bounds and no side effects."""
import asyncio
import importlib
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest

from aiohttp import web
from homeassistant.exceptions import Unauthorized

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = "upgrade_test_component"
package = ModuleType(PACKAGE)
package.__path__ = [str(ROOT / "custom_components/hoymiles_hit_modbus")]
sys.modules[PACKAGE] = package
http = importlib.import_module(f"{PACKAGE}.esphome_upgrade_http")


class Body:
    def __init__(self, data):
        self.data = data
        self.was_read = False

    async def iter_chunked(self, size):
        self.was_read = True
        for offset in range(0, len(self.data), size):
            yield self.data[offset:offset + size]


class Request(dict):
    def __init__(self, data=b"{}", *, admin=True):
        super().__init__(hass_user=SimpleNamespace(is_admin=admin))
        self.content = Body(data)
        async def executor(job):
            return await asyncio.to_thread(job)
        self.app = {"hass": SimpleNamespace(async_add_executor_job=executor)}


class HttpTest(unittest.IsolatedAsyncioTestCase):
    async def test_auth_before_body_or_work(self):
        view = http.HoymilesEsphomeUpgradeView()
        self.assertTrue(view.requires_auth)
        for request in (Request(admin=False), Request()):
            if request["hass_user"].is_admin:
                request.pop("hass_user")
            with self.assertRaises(Unauthorized):
                await view.post(request)
            self.assertFalse(request.content.was_read)

    async def test_ready_only_prepares_with_no_store(self):
        source = (ROOT / "hoymiles-inverter.yaml").read_text(encoding="utf-8")
        response = await http.HoymilesEsphomeUpgradeView().post(Request(json.dumps({"source": source, "transport": "encrypted"}).encode()))
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        result = json.loads(response.body)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["backup"], source)
        self.assertFalse(result["uploaded"])

    async def test_malformed_and_oversize(self):
        for body in (b'{"source":"PRIVATE_SENTINEL"', b'[]', b'{"path":"/config/secrets.yaml"}', b'\xff'):
            with self.assertRaises(web.HTTPBadRequest) as context:
                await http.HoymilesEsphomeUpgradeView().post(Request(body))
            self.assertNotIn("PRIVATE_SENTINEL", str(context.exception))
        with self.assertRaises(web.HTTPRequestEntityTooLarge):
            await http.HoymilesEsphomeUpgradeView().post(Request(b"x" * (http.MAX_BODY_BYTES + 1)))

    async def test_second_request_rejected_while_preparing(self):
        view = http.HoymilesEsphomeUpgradeView()
        await view._lock.acquire()
        request = Request()
        try:
            with self.assertRaises(web.HTTPTooManyRequests):
                await view.post(request)
            self.assertFalse(request.content.was_read)
        finally:
            view._lock.release()

    async def test_timeout_is_bounded_and_unlocks(self):
        request = Request()
        async def stalled(size):
            raise TimeoutError()
            yield b""  # Preserve the asynchronous iterator interface.
        request.content.iter_chunked = stalled
        view = http.HoymilesEsphomeUpgradeView()
        with self.assertRaises(web.HTTPRequestTimeout):
            await view.post(request)
        self.assertFalse(view._lock.locked())


if __name__ == "__main__":
    unittest.main(verbosity=2)
