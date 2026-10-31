"""Configuration web UI, shown in Home Assistant through Ingress.

Options are saved through the Supervisor API (so the app's Configuration
tab shows the same options and the Supervisor validates them against the
schema), then applied without restarting the app.
"""

import copy
import logging
import os
from pathlib import Path
from typing import Any, Optional

import aiohttp
import yaml
from aiohttp import web

from .config import ConfigError, parse_options, validate
from .runtime import AppRuntime

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor"
# Ingress requests come from the Supervisor. The app uses the host network,
# so the UI port is also reachable from the LAN: refuse anything else.
INGRESS_PROXY_IPS = frozenset({"172.30.32.2"})
WEB_DIR = Path(__file__).parent / "web"
TEST_SIGNALS = ("listen_end", "thinking", "search", "vision", "followup_beep")


class SupervisorError(Exception):
    """The Supervisor API refused a request (message shown to the user)."""


class SupervisorOptions:
    """Read and write this app's options through the Supervisor API."""

    def __init__(self, base_url: str = SUPERVISOR_URL):
        self.base_url = base_url
        self._token = os.environ.get("SUPERVISOR_TOKEN", "")

    async def _call(self, method: str, path: str, json: Any = None) -> Any:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                method, f"{self.base_url}{path}", json=json,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                body = await resp.json(content_type=None)
        if resp.status != 200 or body.get("result") != "ok":
            raise SupervisorError(body.get("message") or f"HTTP {resp.status}")
        return body.get("data")

    async def get(self) -> dict[str, Any]:
        return (await self._call("GET", "/addons/self/info"))["options"]

    async def set(self, options: dict[str, Any]) -> None:
        await self._call("POST", "/addons/self/options", {"options": options})


class WebUI:
    """aiohttp server for the configuration UI."""

    def __init__(
        self,
        runtime: AppRuntime,
        port: int,
        supervisor: Optional[SupervisorOptions] = None,
        allowed_ips: frozenset[str] = INGRESS_PROXY_IPS,
    ):
        self.runtime = runtime
        self.port = port
        self.supervisor = supervisor or SupervisorOptions()
        self.allowed_ips = allowed_ips
        self._runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        app = web.Application(middlewares=[self._only_ingress])
        app.router.add_get("/", self._index)
        app.router.add_get("/api/options", self._get_options)
        app.router.add_post("/api/options", self._save_options)
        app.router.add_get("/api/status", self._status)
        app.router.add_post("/api/test", self._test)
        app.router.add_post("/api/export", self._export)
        app.router.add_post("/api/import", self._import)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port).start()
        _LOGGER.info("Configuration UI listening on port %d (Ingress)", self.port)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    @web.middleware
    async def _only_ingress(self, request: web.Request, handler):
        if self.allowed_ips and request.remote not in self.allowed_ips:
            raise web.HTTPForbidden(text="Open this page from Home Assistant")
        return await handler(request)

    async def _index(self, request: web.Request) -> web.Response:
        return web.FileResponse(WEB_DIR / "index.html")

    async def _get_options(self, request: web.Request) -> web.Response:
        try:
            options = await self.supervisor.get()
        except (SupervisorError, aiohttp.ClientError, OSError) as err:
            return web.json_response({"error": f"Supervisor: {err}"}, status=502)
        return web.json_response({"options": options})

    async def _save_options(self, request: web.Request) -> web.Response:
        """Validate, save through the Supervisor, then apply."""
        try:
            options = (await request.json())["options"]
            parsed = parse_options(copy.deepcopy(options))
            validate(parsed.satellites, strict=True)
        except (ValueError, KeyError, TypeError) as err:
            return web.json_response({"error": f"Invalid request: {err}"}, status=400)
        except ConfigError as err:
            return web.json_response({"error": str(err)}, status=400)

        try:
            previous = await self.supervisor.get()
            await self.supervisor.set(options)
        except (SupervisorError, aiohttp.ClientError, OSError) as err:
            # e.g. the Supervisor's own schema validation
            return web.json_response({"error": f"Supervisor: {err}"}, status=400)

        _LOGGER.info("Configuration saved from the UI")
        error, done = await self.runtime.apply(parsed)
        if error is None:
            return web.json_response({"ok": True, "done": done})

        # The new configuration didn't start: keep the saved options in line
        # with what runs (the previous configuration, restored)
        try:
            await self.supervisor.set(previous)
        except (SupervisorError, aiohttp.ClientError, OSError) as err:
            _LOGGER.error("Could not restore the previous options: %s", err)
        return web.json_response(
            {"error": f"{error}. The previous configuration was restored."},
            status=409,
        )

    async def _status(self, request: web.Request) -> web.Response:
        return web.json_response(self.runtime.status())

    async def _test(self, request: web.Request) -> web.Response:
        """Play an announcement of a running satellite."""
        try:
            body = await request.json()
            runner = self.runtime.runners[body["satellite"]]
            signal = body["signal"]
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "Unknown satellite"}, status=400)
        if signal not in TEST_SIGNALS:
            return web.json_response({"error": "Unknown signal"}, status=400)
        played = await runner.test(signal)
        return web.json_response({"played": played})

    async def _export(self, request: web.Request) -> web.Response:
        """Options (from the editor) as a YAML file."""
        try:
            options = (await request.json())["options"]
        except (ValueError, KeyError, TypeError) as err:
            return web.json_response({"error": f"Invalid request: {err}"}, status=400)
        text = yaml.safe_dump(options, allow_unicode=True, sort_keys=False)
        return web.Response(
            text=text, content_type="application/yaml",
            headers={"Content-Disposition": 'attachment; filename="vban-satellite.yaml"'},
        )

    async def _import(self, request: web.Request) -> web.Response:
        """YAML → options for the editor (checked, not saved)."""
        try:
            options = yaml.safe_load((await request.json())["yaml"])
            if not isinstance(options, dict) or not isinstance(
                options.get("satellites"), list
            ):
                raise ValueError("a 'satellites' list is expected")
            parsed = parse_options(copy.deepcopy(options))
            validate(parsed.satellites, strict=True)
        except (yaml.YAMLError, ValueError, KeyError, TypeError) as err:
            return web.json_response({"error": f"Invalid YAML: {err}"}, status=400)
        except ConfigError as err:
            return web.json_response({"error": str(err)}, status=400)
        return web.json_response({"options": options})
