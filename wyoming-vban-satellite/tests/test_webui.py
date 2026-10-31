"""Configuration UI: save through the Supervisor, apply without restart.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import asyncio
import copy
import os
import socket
import tempfile
import unittest

import aiohttp
from aiohttp import web

from wyoming_vban.config import parse_options, validate
from wyoming_vban.runtime import AppRuntime
from wyoming_vban.webui import SupervisorOptions, WebUI


def _free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _satellite(name: str, wyoming_port: int, vban_port: int) -> dict:
    return {
        "name": name, "wyoming_port": wyoming_port,
        "microphone": {"vban_port": vban_port, "stream_name": name},
        "vban_output": {}, "media_player": {}, "followup": {},
        "waiting_sounds": {"thinking_mode": "none", "search_mode": "none"},
        "camera_phrase": {"mode": "none"},
    }


class FakeSupervisor:
    """GET /addons/self/info and POST /addons/self/options."""

    def __init__(self, options: dict) -> None:
        self.options = copy.deepcopy(options)
        self.saved: list[dict] = []
        self.reject = ""  # message of the next refused save
        self.port = _free_port()

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/addons/self/info", self._info)
        app.router.add_post("/addons/self/options", self._set)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def stop(self) -> None:
        await self._runner.cleanup()

    async def _info(self, request: web.Request) -> web.Response:
        return web.json_response({"result": "ok", "data": {"options": self.options}})

    async def _set(self, request: web.Request) -> web.Response:
        if self.reject:
            message, self.reject = self.reject, ""
            return web.json_response({"result": "error", "message": message}, status=400)
        self.options = (await request.json())["options"]
        self.saved.append(copy.deepcopy(self.options))
        return web.json_response({"result": "ok", "data": {}})


class WebUITest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["SUPERVISOR_TOKEN"] = "token"
        self.port_a, self.port_b = _free_port(), _free_port()
        self.initial = {
            "satellites": [_satellite("Bureau", self.port_a, _free_port(socket.SOCK_DGRAM))],
            "publish_exchanges": False, "debug_mirror": [], "debug_logging": False,
        }
        self.supervisor = FakeSupervisor(self.initial)
        await self.supervisor.start()
        self.cache = tempfile.TemporaryDirectory()
        self.runtime = AppRuntime(self.cache.name)
        options = parse_options(copy.deepcopy(self.initial))
        validate(options.satellites)
        await self.runtime.start(options)
        self.ui_port = _free_port()
        self.ui = WebUI(
            self.runtime, self.ui_port,
            SupervisorOptions(f"http://127.0.0.1:{self.supervisor.port}"),
            allowed_ips=frozenset({"127.0.0.1"}),
        )
        await self.ui.start()
        self.http = aiohttp.ClientSession()
        await asyncio.sleep(0.3)

    async def asyncTearDown(self) -> None:
        await self.http.close()
        await self.ui.stop()
        await self.runtime.stop()
        await self.supervisor.stop()
        self.cache.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.ui_port}{path}"

    async def save(self, options: dict) -> tuple[int, dict]:
        async with self.http.post(self.url("/api/options"), json={"options": options}) as resp:
            return resp.status, await resp.json()

    async def can_connect(self, port: int) -> bool:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            return False
        writer.close()
        return True

    async def test_page_and_options(self) -> None:
        async with self.http.get(self.url("/")) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("VBAN Satellite", await resp.text())
        async with self.http.get(self.url("/api/options")) as resp:
            self.assertEqual((await resp.json())["options"], self.initial)

    async def test_save_applies_without_restart(self) -> None:
        new = copy.deepcopy(self.initial)
        new["satellites"][0]["wyoming_port"] = self.port_b
        status, body = await self.save(new)
        self.assertEqual((status, body), (200, {"ok": True, "done": ["satellite 'Bureau' restarted"]}))
        self.assertEqual(self.supervisor.options, new)
        self.assertTrue(await self.can_connect(self.port_b))
        self.assertFalse(await self.can_connect(self.port_a))
        self.assertFalse(self.runtime.fatal.is_set())

    async def test_invalid_config_is_not_saved(self) -> None:
        new = copy.deepcopy(self.initial)
        new["satellites"].append(copy.deepcopy(new["satellites"][0]))  # same port
        status, body = await self.save(new)
        self.assertEqual(status, 400)
        self.assertIn("unique wyoming_port", body["error"])
        self.assertEqual(self.supervisor.saved, [])

    async def test_supervisor_refusal_is_reported(self) -> None:
        self.supervisor.reject = "Missing option 'followup' in satellites"
        status, body = await self.save(copy.deepcopy(self.initial))
        self.assertEqual(status, 400)
        self.assertIn("Missing option", body["error"])
        self.assertTrue(await self.can_connect(self.port_a))

    async def test_failed_start_restores_previous(self) -> None:
        busy = socket.socket()
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("0.0.0.0", self.port_b))
        busy.listen()
        try:
            new = copy.deepcopy(self.initial)
            new["satellites"][0]["wyoming_port"] = self.port_b
            status, body = await self.save(new)
        finally:
            busy.close()
        self.assertEqual(status, 409)
        self.assertIn("previous configuration was restored", body["error"])
        self.assertEqual(self.supervisor.options, self.initial)  # saved back
        self.assertTrue(await self.can_connect(self.port_a))
        self.assertFalse(self.runtime.fatal.is_set())

    async def test_status_test_export_import(self) -> None:
        async with self.http.get(self.url("/api/status")) as resp:
            status = await resp.json()
        self.assertEqual([s["name"] for s in status["satellites"]], ["Bureau"])
        self.assertTrue(status["satellites"][0]["running"])
        self.assertFalse(status["satellites"][0]["ha_connected"])

        # Mode none: nothing to play; unknown signal refused
        async with self.http.post(self.url("/api/test"),
                                  json={"satellite": "Bureau", "signal": "thinking"}) as resp:
            self.assertEqual(await resp.json(), {"played": []})
        async with self.http.post(self.url("/api/test"),
                                  json={"satellite": "Bureau", "signal": "x"}) as resp:
            self.assertEqual(resp.status, 400)

        async with self.http.post(self.url("/api/export"), json={"options": self.initial}) as resp:
            text = await resp.text()
        self.assertIn("satellites:", text)
        async with self.http.post(self.url("/api/import"), json={"yaml": text}) as resp:
            self.assertEqual((await resp.json())["options"], self.initial)
        async with self.http.post(self.url("/api/import"), json={"yaml": "a: [1"}) as resp:
            self.assertEqual(resp.status, 400)
        async with self.http.post(self.url("/api/import"), json={"yaml": "satellites: 3"}) as resp:
            self.assertIn("satellites", (await resp.json())["error"])

    async def test_only_ingress(self) -> None:
        self.ui.allowed_ips = frozenset({"172.30.32.2"})
        async with self.http.get(self.url("/api/options")) as resp:
            self.assertEqual(resp.status, 403)


if __name__ == "__main__":
    unittest.main()
