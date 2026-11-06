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

from . import calibration, wake_test
from .calibration import CalibrationSession
from .config import ConfigError, parse_options, validate
from .ha_ws import HaCommandError, HaUnavailable
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
                try:
                    body = await resp.json(content_type=None)
                except ValueError:
                    body = None
        if not isinstance(body, dict):
            raise SupervisorError(f"unexpected answer (HTTP {resp.status})")
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
        self._purge_task: Optional[asyncio.Task] = None
        self._sessions: dict[str, CalibrationSession] = {}  # by satellite
        # One calibration request at a time per satellite (another tab…)
        self._busy: set[str] = set()
        # Read, write and apply the options one change at a time
        self._options_lock = asyncio.Lock()

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
        app.router.add_post("/api/calibration/denoise", self._calibration_denoise)
        app.router.add_post("/api/calibration/autogain", self._calibration_autogain)
        app.router.add_get("/api/calibration/wake_models", self._wake_models)
        app.router.add_delete("/api/calibration", self._calibration_clear)
        app.router.add_get("/api/calibration", self._calibration_state)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", self.port).start()
        _LOGGER.info("Configuration UI listening on port %d (Ingress)", self.port)
        self._purge_task = asyncio.create_task(self._purge_sessions())

    async def _purge_sessions(self) -> None:
        """Expired calibrations are deleted even if the page isn't used again."""
        while True:
            await asyncio.sleep(60)
            for name in [n for n, s in self._sessions.items() if s.expired]:
                del self._sessions[name]

    async def stop(self) -> None:
        if self._purge_task is not None:
            self._purge_task.cancel()
            await asyncio.gather(self._purge_task, return_exceptions=True)
            self._purge_task = None
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
            return web.json_response({"error": f"Requête invalide : {err}"}, status=400)
        if not isinstance(options, dict):
            return web.json_response({"error": "Requête invalide : options attendues"}, status=400)
        async with self._options_lock:
            return await self._store_and_apply(options)

    async def _store_and_apply(self, options: dict[str, Any]) -> web.Response:
        """Validate, save through the Supervisor, then apply (the caller
        holds _options_lock)."""
        try:
            parsed = parse_options(copy.deepcopy(options))
            validate(parsed.satellites, strict=True)
        except (ValueError, KeyError, TypeError, AttributeError) as err:
            return web.json_response({"error": f"Options invalides : {err}"}, status=400)
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
            {"error": f"{error}. La configuration précédente a été rétablie."},
            status=409,
        )

    # Calibration

    def _runner_of(self, request: web.Request, name: Optional[str] = None) -> SatelliteRunner:
        name = name if name is not None else request.query.get("satellite", "")
        runner = self.runtime.runners.get(name) if isinstance(name, str) else None
        if runner is None:
            raise web.HTTPNotFound(
                text=json.dumps({"error": f"Satellite « {name} » pas démarré"}),
                content_type="application/json",
            )
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
        name = request.query.get("satellite", "")
        runner = self._runner_of(request)
        response = web.StreamResponse(headers={
            "Content-Type": "text/event-stream", "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await response.prepare(request)
        try:
            while True:
                # The satellite may have been restarted (new runner) or removed
                runner = self.runtime.runners.get(name)
                if runner is None:
                    break
                data = json.dumps(self._meter_data(runner))
                await response.write(f"data: {data}\n\n".encode())
                await asyncio.sleep(0.1)
        except ConnectionResetError:
            pass
        return response

    def _session(self, name: str, create: bool = False) -> Optional[CalibrationSession]:
        """The satellite's calibration (expired ones are deleted)."""
        for key in [k for k, v in self._sessions.items() if v.expired]:
            del self._sessions[key]
        if create:
            self._sessions[name] = CalibrationSession()
        session = self._sessions.get(name)
        if session is not None:
            session.touch()
        return session

    async def _calibration_record(self, request: web.Request) -> web.Response:
        """Record and analyze a step: "noise" (silence), "voice" (the
        sentence), "near" / "far" (auto gain), "wake" (wake word test)."""
        body = await self._json(request)
        step, name = body.get("step"), body.get("satellite")
        if not isinstance(step, str) or not isinstance(name, str):
            return web.json_response({"error": "Requête invalide"}, status=400)
        runner = self._runner_of(request, name)
        if step not in self.STEP_SECONDS:
            return web.json_response({"error": "Étape inconnue"}, status=400)
        session = self._session(name)
        if step != "noise" and (session is None or session.noise is None):
            return web.json_response(
                {"error": "Mesurez d'abord le bruit de fond", "expired": True}, status=400)
        if step in ("near", "far", "wake") and session.proposal is None:
            return web.json_response({"error": "Faites d'abord la mesure de la voix"}, status=400)
        if name in self._busy:
            return web.json_response(
                {"error": "Un calibrage est déjà en cours sur ce satellite"}, status=409)
        try:
            wake_target = self._wake_target(body) if step == "wake" else None
            proposed = body.get("proposed") or {}
            if not isinstance(proposed, dict):
                raise ValueError("proposed")
        except (ValueError, TypeError):
            return web.json_response({"error": "Requête invalide"}, status=400)
        self._busy.add(name)
        try:
            if step == "wake":
                return await self._wake_step(runner, session, body, wake_target)
            pcm = await runner.record(self.STEP_SECONDS[step])
            if len(pcm) < calibration.FRAME_BYTES * 10:
                return web.json_response(
                    {"error": "Aucun audio reçu de ce micro pendant la mesure"}, status=409,
                )
            if step == "noise":
                # A new calibration, only once the measure worked
                session = self._session(name, create=True)
                session.noise_pcm = pcm
                session.noise = calibration.analyze_noise(pcm)
                return web.json_response({"noise": session.noise})
            if self._sessions.get(name) is not session:
                return web.json_response(
                    {"error": "Calibrage effacé pendant la mesure", "expired": True}, status=409)
            return await self._record_step(runner, session, step, pcm, body)
        finally:
            self._busy.discard(name)

    async def _record_step(
        self, runner: SatelliteRunner, session: CalibrationSession, step: str,
        pcm: bytes, body: dict[str, Any],
    ) -> web.Response:
        if step == "voice":
            return web.json_response(await asyncio.to_thread(self._voice_step, runner, session, pcm))
        session.takes[step] = pcm
        level = await asyncio.to_thread(
            calibration.take_level, session.noise_pcm, pcm, session.noise["rms_db"],
            self._noise_choice(session, body), session.proposal["volume"],
        )
        session.take_levels[step] = level
        return web.json_response({"take": step, "voice_db": level})

    # Recording lengths (s) of the steps
    STEP_SECONDS = {"noise": 5.0, "voice": 8.0, "near": 6.0, "far": 6.0, "wake": 20.0}
    WAKE_REPEATS = 5  # times the wake word is said in the test

    @staticmethod
    def _voice_step(
        runner: SatelliteRunner, session: CalibrationSession, pcm: bytes,
    ) -> dict[str, Any]:
        session.voice_pcm = pcm
        session.voice = calibration.analyze_voice(pcm, session.noise["rms_db"])
        session.proposal = calibration.propose(session.noise, session.voice)
        # What HA would get with the proposed volume and the current clean-up
        noise, auto_gain, _, target = runner.mic_settings_applied()
        session.voice_after_pcm = calibration.process_take(
            session.noise_pcm, pcm, noise, session.proposal["volume"], auto_gain, target,
        )
        # Later steps start again from this voice
        session.denoise, session.denoise_pcm = None, {}
        session.takes, session.autogain, session.autogain_pcm = {}, None, {}
        return {"noise": session.noise, "voice": session.voice, "proposal": session.proposal}

    @staticmethod
    def _noise_choice(session: CalibrationSession, body: dict[str, Any]) -> str:
        """The noise suppression level picked in step A (or recommended)."""
        level = body.get("noise_suppression")
        if level in calibration.NOISE_SUPPRESSION_LEVELS:
            return level
        return session.denoise["recommended"] if session.denoise else "off"

    async def _calibration_denoise(self, request: web.Request) -> web.Response:
        """Step A: the voice take through each noise suppression level."""
        name = str((await self._json(request)).get("satellite", ""))
        session = self._session(name)
        if session is None or session.proposal is None:
            return web.json_response({"error": "Faites d'abord la mesure de la voix"}, status=400)

        def analyze() -> dict[str, Any]:
            rows, previews = calibration.compare_noise_levels(
                session.noise_pcm, session.voice_pcm, session.noise["rms_db"],
                session.proposal["volume"],
            )
            session.denoise_pcm = previews
            session.denoise = {
                "rows": rows, "recommended": calibration.choose_noise_level(rows),
                "advice": calibration.noise_advice(rows),
            }
            return session.denoise

        return web.json_response(await asyncio.to_thread(analyze))

    async def _calibration_autogain(self, request: web.Request) -> web.Response:
        """Step B: near / far voice levels -> auto gain on or off."""
        body = await self._json(request)
        session = self._session(str(body.get("satellite", "")))
        if session is None or not {"near", "far"} <= set(session.takes):
            return web.json_response({"error": "Enregistrez d'abord les deux prises"}, status=400)
        noise = self._noise_choice(session, body)

        def analyze() -> dict[str, Any]:
            volume, noise_db = session.proposal["volume"], session.noise["rms_db"]
            levels = {
                take: calibration.take_level(session.noise_pcm, raw, noise_db, noise, volume)
                for take, raw in session.takes.items()
            }
            if None in levels.values():
                raise ValueError("no voice")
            decision = calibration.autogain_decision(levels["near"], levels["far"])
            decision["noise_suppression"] = noise
            session.autogain = decision
            session.autogain_pcm = {}
            for take, raw in session.takes.items():
                session.autogain_pcm[f"{take}-before"] = calibration.process_take(
                    session.noise_pcm, raw, noise, volume)
                session.autogain_pcm[f"{take}-after"] = calibration.process_take(
                    session.noise_pcm, raw, noise, volume, True, decision["target_db"])
            return decision

        try:
            return web.json_response(await asyncio.to_thread(analyze))
        except ValueError:
            return web.json_response({"error": "Aucune voix dans les prises"}, status=409)

    @staticmethod
    def _wake_target(body: dict[str, Any]) -> tuple[str, int, str]:
        """(host, port, model); ValueError / TypeError if invalid."""
        port = int(body.get("port") or wake_test.DEFAULT_PORT)
        if not 0 < port < 65536:
            raise ValueError("port")
        return (
            str(body.get("host") or wake_test.DEFAULT_HOST),
            port,
            str(body.get("model") or wake_test.DEFAULT_MODEL),
        )

    async def _wake_step(
        self, runner: SatelliteRunner, session: CalibrationSession,
        body: dict[str, Any], wake_target: Optional[tuple[str, int, str]],
    ) -> web.Response:
        """Step C: while the wake words are said, the audio is processed
        with the current and the proposed settings and sent, as it comes, to
        openWakeWord over two connections (nothing stored)."""
        assert wake_target is not None
        host, port, model = wake_target
        current = dict(zip(("noise_suppression", "auto_gain", "volume", "auto_gain_target_db"),
                           runner.mic_settings_applied()))
        proposed = self._proposed_settings(session, body.get("proposed") or {}, current)
        try:
            # The page checked the model before the countdown; checked again
            # (openWakeWord falls back to another model for an unknown name)
            await wake_test.check_model(host, port, model)
        except wake_test.WakeTestError as err:
            return web.json_response({"error": str(err)}, status=502)
        filters = await asyncio.to_thread(self._warm_filters, session, (current, proposed))
        queues: list[asyncio.Queue] = [asyncio.Queue() for _ in filters]
        tasks = [asyncio.create_task(wake_test.count_detections_live(q, host, port, model))
                 for q in queues]

        def tap(pcm: bytes) -> None:
            for mic_filter, queue in zip(filters, queues):
                if out := mic_filter.process(pcm):
                    queue.put_nowait(out)

        try:
            raw = await runner.record(self.STEP_SECONDS["wake"], tap)
        finally:
            for queue in queues:
                queue.put_nowait(None)
        try:
            counts = await asyncio.gather(*tasks)
        except wake_test.WakeTestError as err:
            return web.json_response({"error": str(err)}, status=502)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if len(raw) < calibration.FRAME_BYTES * 10:
            return web.json_response(
                {"error": "Aucun audio reçu de ce micro pendant la mesure"}, status=409,
            )
        result = {
            "model": model, "expected": self.WAKE_REPEATS,
            "current": current | {"detections": len(counts[0])},
            "proposed": proposed | {"detections": len(counts[1])},
        }
        session.wake = result
        return web.json_response(result)

    @staticmethod
    def _warm_filters(
        session: CalibrationSession, settings: tuple[dict[str, Any], ...],
    ) -> list[MicFilter]:
        """One filter per settings, having learnt the background noise and
        (auto gain) settled on the voice take, like during use."""
        filters = []
        for s in settings:
            mic_filter = MicFilter(s["noise_suppression"], s["auto_gain"], s["volume"],
                                   s["auto_gain_target_db"])
            calibration.run_filter(mic_filter, session.noise_pcm)
            if s["auto_gain"] and session.voice_pcm:
                calibration.run_filter(mic_filter, session.voice_pcm)
            filters.append(mic_filter)
        return filters

    @staticmethod
    def _proposed_settings(
        session: CalibrationSession, chosen: dict[str, Any], current: dict[str, Any],
    ) -> dict[str, Any]:
        """The settings proposed so far (choices of the page first)."""
        settings = dict(current)
        if session.proposal is not None:
            settings["volume"] = session.proposal["volume"]
        if session.denoise is not None:
            settings["noise_suppression"] = session.denoise["recommended"]
        if session.autogain is not None:
            settings["auto_gain"] = session.autogain["auto_gain"]
            if session.autogain["auto_gain"]:
                settings["auto_gain_target_db"] = float(session.autogain["target_db"])
        for key in settings:
            value = chosen.get(key)
            if value is None:
                continue
            try:
                if key == "auto_gain":
                    if isinstance(value, bool):
                        settings[key] = value
                elif key == "noise_suppression":
                    settings[key] = str(value)
                else:
                    settings[key] = float(value)
            except (TypeError, ValueError):
                pass  # keep the proposal
        if settings["noise_suppression"] not in calibration.NOISE_SUPPRESSION_LEVELS:
            settings["noise_suppression"] = current["noise_suppression"]
        return settings

    async def _wake_models(self, request: web.Request) -> web.Response:
        try:
            host, port, _ = self._wake_target(dict(request.query))
        except (ValueError, TypeError):
            return web.json_response({"error": "Requête invalide"}, status=400)
        try:
            models = await wake_test.list_models(host, port)
        except wake_test.WakeTestError as err:
            return web.json_response({"error": str(err)}, status=502)
        return web.json_response({"models": models, "default": wake_test.DEFAULT_MODEL})

    @staticmethod
    async def _json(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text=json.dumps({"error": "Requête invalide"}),
                                     content_type="application/json")
        return body

    async def _calibration_audio(self, request: web.Request) -> web.Response:
        """A recording: the voice before / after the proposed volume, through
        a noise suppression level, or a near / far take before / after the
        auto gain."""
        session = self._session(request.query.get("satellite", ""))
        pcm = session.audio(request.query.get("which", "")) if session is not None else b""
        if not pcm:
            raise web.HTTPNotFound(text="No recording")
        return web.Response(
            body=build_wav(pcm, 16000, 2, 1), content_type="audio/wav",
            headers={"Cache-Control": "no-store"},
        )

    # Settings the calibration applies -> microphone option
    APPLY_KEYS = {
        "volume": "volume", "noise_suppression": "noise_suppression",
        "auto_gain": "auto_gain", "auto_gain_target_db": "auto_gain_target_db",
    }

    async def _calibration_apply(self, request: web.Request) -> web.Response:
        """Write microphone settings of a satellite: any of volume,
        noise_suppression, auto_gain (with auto_gain_target_db). Saved and
        applied like any change; "finish" ends the calibration.

        If the satellite follows its HA entities, they are written instead:
        the app gets them back like any change made in HA. The recordings
        no step needs any more are deleted."""
        body = await self._json(request)
        name = str(body.get("satellite", ""))
        try:
            values = self._apply_values(body)
        except (ValueError, TypeError):
            return web.json_response({"error": "Requête invalide"}, status=400)
        if not values:
            return web.json_response({"error": "Rien à appliquer"}, status=400)
        async with self._options_lock:
            return await self._apply_locked(name, values, body)

    async def _apply_locked(
        self, name: str, values: dict[str, Any], body: dict[str, Any],
    ) -> web.Response:
        runner = self.runtime.runners.get(name)
        if runner is not None and runner.cfg.mic_follow_ha:
            ha_values = {k: v for k, v in values.items() if k in ("volume", "noise_suppression")}
            if "auto_gain" in values:  # HA: 0 = off, N = aim at -N dBFS
                target = values.get("auto_gain_target_db", runner.mic_settings_applied()[3])
                ha_values["auto_gain"] = round(-target) if values["auto_gain"] else 0
            try:
                written = await self.runtime.write_ha_mic(name, ha_values)
            except (HaUnavailable, HaCommandError) as err:
                return web.json_response({"error": f"Home Assistant : {err}"}, status=502)
            self._forget(name, values, body.get("finish"))
            return web.json_response({"ok": True, "entities": written})
        try:
            options = await self.supervisor.get()
        except (SupervisorError, aiohttp.ClientError, OSError) as err:
            return web.json_response({"error": f"Supervisor: {err}"}, status=502)
        satellite = next(
            (s for s in options.get("satellites", []) if s.get("name") == name), None,
        )
        if satellite is None:
            return web.json_response({"error": f"Satellite « {name} » inconnu"}, status=400)
        microphone = satellite.setdefault("microphone", {})
        for key, value in values.items():
            microphone[self.APPLY_KEYS[key]] = value
        response = await self._store_and_apply(options)
        if response.status == 200:
            self._forget(name, values, body.get("finish"))
        return response

    @staticmethod
    def _apply_values(body: dict[str, Any]) -> dict[str, Any]:
        values: dict[str, Any] = {}
        if body.get("volume") is not None:
            values["volume"] = round(float(body["volume"]), 2)
        if body.get("noise_suppression") is not None:
            if body["noise_suppression"] not in calibration.NOISE_SUPPRESSION_LEVELS:
                raise ValueError("noise_suppression")
            values["noise_suppression"] = body["noise_suppression"]
        if body.get("auto_gain") is not None:
            values["auto_gain"] = bool(body["auto_gain"])
            if values["auto_gain"] and body.get("auto_gain_target_db") is not None:
                values["auto_gain_target_db"] = int(round(float(body["auto_gain_target_db"])))
        return values

    def _forget(self, name: str, applied: dict[str, Any], finish: Any) -> None:
        """Delete the recordings of the steps applied; all of them at the end."""
        session = self._sessions.get(name)
        if session is None:
            return
        if finish:
            del self._sessions[name]
            return
        if "volume" in applied:
            session.voice_after_pcm = b""
        if "noise_suppression" in applied:  # the voice take only served step A
            session.voice_pcm = b""
            session.denoise_pcm = {}
        if "auto_gain" in applied:
            session.takes = {}
            session.take_levels = {}
            session.autogain_pcm = {}

    async def _calibration_state(self, request: web.Request) -> web.Response:
        """The satellite's calibration in progress (or none)."""
        session = self._session(request.query.get("satellite", ""))
        return web.json_response(session.state() if session is not None else None)

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
            return web.json_response({"error": "Satellite pas démarré"}, status=400)
        if signal not in TEST_SIGNALS:
            return web.json_response({"error": "Annonce inconnue"}, status=400)
        played = await runner.test(signal)
        return web.json_response({"played": played})

    async def _export(self, request: web.Request) -> web.Response:
        """Options (from the editor) as a YAML file."""
        try:
            options = (await request.json())["options"]
        except (ValueError, KeyError, TypeError) as err:
            return web.json_response({"error": f"Requête invalide : {err}"}, status=400)
        text = yaml.safe_dump(options, allow_unicode=True, sort_keys=False)
        return web.Response(
            text=text, content_type="application/yaml",
            headers={"Content-Disposition": 'attachment; filename="vban-satellite.yaml"'},
        )

    async def _import(self, request: web.Request) -> web.Response:
        """YAML → options for the editor (checked, not saved)."""
        try:
            text = (await request.json())["yaml"]
            if not isinstance(text, str):
                raise ValueError("a YAML text is expected")
            options = yaml.safe_load(text)
            if not isinstance(options, dict) or not isinstance(
                options.get("satellites"), list
            ):
                raise ValueError("a 'satellites' list is expected")
            parsed = parse_options(copy.deepcopy(options))
            validate(parsed.satellites, strict=True)
        except (yaml.YAMLError, ValueError, KeyError, TypeError, AttributeError) as err:
            return web.json_response({"error": f"YAML invalide : {err}"}, status=400)
        except ConfigError as err:
            return web.json_response({"error": str(err)}, status=400)
        return web.json_response({"options": options})
