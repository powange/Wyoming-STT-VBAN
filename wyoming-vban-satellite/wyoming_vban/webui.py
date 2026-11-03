"""Configuration web UI, shown in Home Assistant through Ingress.

Options are saved through the Supervisor API (so the app's Configuration
tab shows the same options and the Supervisor validates them against the
schema), then applied without restarting the app.
"""

import asyncio
import copy
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import aiohttp
import yaml
from aiohttp import web

from . import calibration
from .calibration import CalibrationSession
from .config import ConfigError, parse_options, validate
from .media_player import build_wav
from .mic_filter import MicFilter
from .runtime import AppRuntime, SatelliteRunner

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

    async def info(self) -> dict[str, Any]:
        """This app's info: options, version…"""
        return await self._call("GET", "/addons/self/info")

    async def get(self) -> dict[str, Any]:
        return (await self.info())["options"]

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
        self._sessions: dict[str, CalibrationSession] = {}  # by satellite

    async def start(self) -> None:
        app = web.Application(middlewares=[self._only_ingress])
        app.router.add_get("/", self._index)
        app.router.add_get("/api/options", self._get_options)
        app.router.add_post("/api/options", self._save_options)
        app.router.add_get("/api/status", self._status)
        app.router.add_post("/api/test", self._test)
        app.router.add_post("/api/export", self._export)
        app.router.add_post("/api/import", self._import)
        app.router.add_get("/api/meter", self._meter_stream)
        app.router.add_get("/api/meter/once", self._meter_once)
        app.router.add_post("/api/calibration/record", self._calibration_record)
        app.router.add_get("/api/calibration/audio", self._calibration_audio)
        app.router.add_post("/api/calibration/apply", self._calibration_apply)
        app.router.add_delete("/api/calibration", self._calibration_clear)
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
        # Never reuse a cached page: after an update, the new interface
        # must show at once
        return web.FileResponse(
            WEB_DIR / "index.html", headers={"Cache-Control": "no-cache, no-store"},
        )

    async def _get_options(self, request: web.Request) -> web.Response:
        try:
            info = await self.supervisor.info()
        except (SupervisorError, aiohttp.ClientError, OSError) as err:
            return web.json_response({"error": f"Supervisor: {err}"}, status=502)
        return web.json_response({"options": info["options"], "version": info.get("version")})

    async def _save_options(self, request: web.Request) -> web.Response:
        """Validate, save through the Supervisor, then apply."""
        try:
            options = (await request.json())["options"]
        except (ValueError, KeyError, TypeError) as err:
            return web.json_response({"error": f"Invalid request: {err}"}, status=400)
        return await self._store_and_apply(options)

    async def _store_and_apply(self, options: dict[str, Any]) -> web.Response:
        """Validate, save through the Supervisor, then apply."""
        try:
            parsed = parse_options(copy.deepcopy(options))
            validate(parsed.satellites, strict=True)
        except (ValueError, KeyError, TypeError) as err:
            return web.json_response({"error": f"Invalid options: {err}"}, status=400)
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

    # Calibration

    def _runner_of(self, request: web.Request, name: Optional[str] = None) -> SatelliteRunner:
        name = name if name is not None else request.query.get("satellite", "")
        runner = self.runtime.runners.get(name)
        if runner is None:
            raise web.HTTPBadRequest(text="Unknown satellite")
        return runner

    @staticmethod
    def _meter_data(runner: SatelliteRunner) -> dict[str, Any]:
        return {
            "before": runner.meter_in.stats(),
            "after": runner.meter_out.stats(),
            "volume": runner.mic_filter.volume,
        }

    async def _meter_once(self, request: web.Request) -> web.Response:
        return web.json_response(self._meter_data(self._runner_of(request)))

    async def _meter_stream(self, request: web.Request) -> web.StreamResponse:
        """Live levels about 10 times a second (Server-Sent Events)."""
        runner = self._runner_of(request)
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await response.prepare(request)
        try:
            while True:
                data = json.dumps(self._meter_data(runner))
                await response.write(f"data: {data}\n\n".encode())
                await asyncio.sleep(0.1)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return response

    def _session(self, name: str, create: bool = False) -> Optional[CalibrationSession]:
        """The satellite's calibration (expired ones are deleted)."""
        for key in [k for k, v in self._sessions.items() if v.expired]:
            del self._sessions[key]
        if create:
            self._sessions[name] = CalibrationSession()
        return self._sessions.get(name)

    async def _calibration_record(self, request: web.Request) -> web.Response:
        """Record and analyze a step: "noise" (silence) then "voice"."""
        try:
            body = await request.json()
            step = body["step"]
            name = body["satellite"]
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "Invalid request"}, status=400)
        runner = self._runner_of(request, name)
        if step not in ("noise", "voice"):
            return web.json_response({"error": "Unknown step"}, status=400)
        session = self._session(name, create=(step == "noise"))
        if step == "voice" and (session is None or session.noise is None):
            return web.json_response({"error": "Mesurez d'abord le bruit de fond"}, status=400)
        assert session is not None
        seconds = 5.0 if step == "noise" else 8.0
        pcm = await runner.record(seconds)
        if len(pcm) < calibration.FRAME_BYTES * 10:
            return web.json_response(
                {"error": "Aucun audio reçu de ce micro pendant la mesure"}, status=409,
            )
        if step == "noise":
            session.noise_pcm = pcm
            session.noise = calibration.analyze_noise(pcm)
            return web.json_response({"noise": session.noise})

        session.voice_pcm = pcm
        session.voice = calibration.analyze_voice(pcm, session.noise["rms_db"])
        session.proposal = calibration.propose(session.noise, session.voice)
        # What HA would get with the proposed volume and the current clean-up:
        # the filter learns the noise first, then processes the voice
        current = runner.mic_filter
        preview = MicFilter(current.noise_suppression, current.auto_gain,
                            session.proposal["volume"])
        preview.process(session.noise_pcm)
        session.voice_after_pcm = preview.process(pcm)
        return web.json_response({
            "noise": session.noise, "voice": session.voice, "proposal": session.proposal,
        })

    async def _calibration_audio(self, request: web.Request) -> web.Response:
        """The voice recording, before or after the (proposed) processing."""
        name = request.query.get("satellite", "")
        session = self._session(name)
        which = request.query.get("which")
        pcm = None
        if session is not None:
            pcm = session.voice_pcm if which == "before" else session.voice_after_pcm
        if not pcm:
            raise web.HTTPNotFound(text="No recording")
        return web.Response(
            body=build_wav(pcm, 16000, 2, 1), content_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    async def _calibration_apply(self, request: web.Request) -> web.Response:
        """Write the microphone volume of a satellite (saved and applied
        like any change), then delete the recordings."""
        try:
            body = await request.json()
            name = body["satellite"]
            volume = round(float(body["volume"]), 2)
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "Invalid request"}, status=400)
        try:
            options = await self.supervisor.get()
        except (SupervisorError, aiohttp.ClientError, OSError) as err:
            return web.json_response({"error": f"Supervisor: {err}"}, status=502)
        satellite = next(
            (s for s in options.get("satellites", []) if s.get("name") == name), None,
        )
        if satellite is None:
            return web.json_response({"error": "Unknown satellite"}, status=400)
        satellite.setdefault("microphone", {})["volume"] = volume
        response = await self._store_and_apply(options)
        if response.status == 200:
            self._sessions.pop(name, None)
        return response

    async def _calibration_clear(self, request: web.Request) -> web.Response:
        self._sessions.pop(request.query.get("satellite", ""), None)
        return web.json_response({"ok": True})

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
