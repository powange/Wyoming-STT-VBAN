"""HA microphone entities applied by the app (microphone.follow_ha_entities).

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp, pyspeex-noise)
"""

import asyncio
import audioop
import math
import os
import socket
import tempfile
import unittest

import aiohttp
from aiohttp import WSMsgType, web

from test_calibration import speech
from test_webui import FakeSupervisor, _free_port
from wyoming_vban.calibration import run_filter
from wyoming_vban.config import parse_options, validate
from wyoming_vban.ha_audio import auto_gain_settings, parse_state, resolve_entities
from wyoming_vban.mic_filter import MicFilter
from wyoming_vban.runtime import AppRuntime
from wyoming_vban.webui import SupervisorOptions, WebUI

# Entities renamed by the user: found by unique_id, not by entity_id
VOLUME = "number.micro_du_bureau_renomme"
GAIN = "number.gain_bureau"
NOISE = "select.bruit_bureau"
DEVICES = [
    {"id": "d1", "name": "Bureau", "name_by_user": "Mon bureau",
     "identifiers": [["wyoming", "entry-bureau"]]},
    {"id": "d2", "name": "Salon", "name_by_user": None,
     "identifiers": [["wyoming", "entry-salon"]]},
    {"id": "d3", "name": "Bureau", "name_by_user": None,  # not Wyoming
     "identifiers": [["esphome", "aa:bb"]]},
]
ENTITIES = [
    {"entity_id": VOLUME, "platform": "wyoming", "unique_id": "entry-bureau-volume_multiplier"},
    {"entity_id": GAIN, "platform": "wyoming", "unique_id": "entry-bureau-auto_gain"},
    {"entity_id": NOISE, "platform": "wyoming",
     "unique_id": "entry-bureau-noise_suppression_level"},
    {"entity_id": "select.bureau_pipeline", "platform": "wyoming",
     "unique_id": "entry-bureau-pipeline"},
    {"entity_id": "number.salon_volume", "platform": "wyoming",
     "unique_id": "entry-salon-volume_multiplier"},
    {"entity_id": "number.esphome_thing", "platform": "esphome",
     "unique_id": "entry-bureau-auto_gain"},
]


class MappingTest(unittest.TestCase):
    def test_volume(self) -> None:
        self.assertEqual(parse_state("volume", "1.0"), 1.0)
        self.assertEqual(parse_state("volume", "2.5"), 2.5)
        self.assertEqual(parse_state("volume", "0.1"), 0.1)
        self.assertEqual(parse_state("volume", "10.0"), 10.0)
        self.assertEqual(parse_state("volume", "0.01"), 0.1)  # bounded

    def test_noise_suppression(self) -> None:
        expected = {"off": "off", "low": "light", "medium": "medium",
                    "high": "strong", "max": "strong"}
        for ha, app in expected.items():
            self.assertEqual(parse_state("noise_suppression", ha), app)
        self.assertIsNone(parse_state("noise_suppression", "loud"))

    def test_auto_gain(self) -> None:
        self.assertEqual(parse_state("auto_gain", "0"), 0)
        self.assertEqual(parse_state("auto_gain", "31.0"), 31)
        self.assertEqual(auto_gain_settings(0), (False, -18.0))
        self.assertEqual(auto_gain_settings(1), (True, -3.0))  # bounded to -3
        self.assertEqual(auto_gain_settings(18), (True, -18.0))
        self.assertEqual(auto_gain_settings(31), (True, -31.0))

    def test_unknown_and_unavailable_ignored(self) -> None:
        for setting in ("volume", "auto_gain", "noise_suppression"):
            for state in ("unknown", "unavailable", None, ""):
                self.assertIsNone(parse_state(setting, state))

    def test_resolution_by_unique_id(self) -> None:
        found = resolve_entities(["Bureau", "Salon", "Cuisine"], DEVICES, ENTITIES)
        self.assertEqual(found["Bureau"],
                         {"volume": VOLUME, "auto_gain": GAIN, "noise_suppression": NOISE})
        self.assertEqual(found["Salon"], {"volume": "number.salon_volume"})
        self.assertNotIn("Cuisine", found)


class AutoGainTargetTest(unittest.TestCase):
    def test_target_is_followed(self) -> None:
        # About -39 dBFS of voice: brought to the target asked, not to -18
        # (within the +20 dB the auto gain can add)
        for target in (-24.0, -28.0):
            out = run_filter(MicFilter(auto_gain=True, auto_gain_target_db=target),
                             speech(10, 500))
            last_voice = out[-32000:-24000]  # a voiced quarter of a second
            level = 20 * math.log10(max(audioop.rms(last_voice, 2), 1) / 32768)
            self.assertAlmostEqual(level, target, delta=3)


class FakeHomeAssistant:
    """HA WebSocket API: registries, states, triggers, services."""

    def __init__(self) -> None:
        self.states = {VOLUME: "2.5", GAIN: "24", NOISE: "high"}
        self.services: list[dict] = []
        self.sockets: list[web.WebSocketResponse] = []
        self.trigger_ids: list[int] = []
        self.port = _free_port()
        self._runner: web.AppRunner

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/websocket", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def stop(self) -> None:
        for ws in self.sockets:
            await ws.close()
        await self._runner.cleanup()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/websocket"

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        await ws.receive_json()
        await ws.send_json({"type": "auth_ok"})
        self.sockets.append(ws)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            data = msg.json()
            result = None
            if data["type"] == "config/device_registry/list":
                result = DEVICES
            elif data["type"] == "config/entity_registry/list":
                result = ENTITIES
            elif data["type"] == "get_states":
                result = [{"entity_id": e, "state": s} for e, s in self.states.items()]
            elif data["type"] == "subscribe_entities":
                self.trigger_ids.append(data["id"])
                self.subscribed_entities = data["entity_ids"]
            elif data["type"] == "call_service":
                self.services.append(data)
            await ws.send_json({"id": data["id"], "type": "result", "success": True,
                                "result": result})
            if data["type"] == "subscribe_entities":  # current states first
                await ws.send_json({"id": data["id"], "type": "event", "event": {"a": {
                    e: {"s": s, "a": {}} for e, s in self.states.items()
                    if e in data["entity_ids"]}}})
            if data["type"] == "call_service":  # the entity changes, like in HA
                value = data["service_data"].get("value", data["service_data"].get("option"))
                await self.set_state(data["target"]["entity_id"], str(value))
        return ws

    async def set_state(self, entity_id: str, state: str) -> None:
        self.states[entity_id] = state
        for ws in self.sockets:
            if self.trigger_ids and not ws.closed:
                await ws.send_json({"id": self.trigger_ids[-1], "type": "event", "event": {
                    "c": {entity_id: {"+": {"s": state}}}}})


def satellite_options(port: int, mic_port: int, follow: bool) -> dict:
    return {
        "satellites": [{
            "name": "Bureau", "wyoming_port": port, "output": "none",
            "microphone": {"vban_port": mic_port, "stream_name": "AtomBureau",
                           "volume": 1.5, "follow_ha_entities": follow},
            "vban_output": {}, "media_player": {}, "followup": {"enabled": False},
            "waiting_sounds": {"thinking_mode": "none", "search_mode": "none"},
            "camera_phrase": {"mode": "none"},
        }],
        "publish_exchanges": False, "debug_mirror": [],
    }


class FollowHaEntitiesTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["SUPERVISOR_TOKEN"] = "token"
        self.ha = FakeHomeAssistant()
        await self.ha.start()
        self.cache = tempfile.TemporaryDirectory()
        self.runtime = AppRuntime(self.cache.name)
        self.runtime.ha_ws_url = self.ha.url
        self.port, self.mic_port = _free_port(), _free_port(socket.SOCK_DGRAM)

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()
        await self.ha.stop()
        self.cache.cleanup()

    def parsed(self, follow: bool):
        parsed = parse_options(satellite_options(self.port, self.mic_port, follow))
        validate(parsed.satellites)
        return parsed

    @property
    def filter(self) -> MicFilter:
        return self.runtime.runners["Bureau"].mic_filter

    async def wait_for(self, predicate, timeout: float = 5.0) -> None:
        for _ in range(int(timeout / 0.05)):
            if predicate():
                return
            await asyncio.sleep(0.05)
        self.fail("condition not reached")

    async def test_follow_on_and_off_live(self) -> None:
        await self.runtime.start(self.parsed(False))
        self.assertEqual(
            (self.filter.volume, self.filter.auto_gain, self.filter.noise_suppression),
            (1.5, False, "off"),
        )
        with self.assertLogs("wyoming_vban.runtime", "INFO") as logs:
            error, _ = await self.runtime.apply(self.parsed(True))
            self.assertIsNone(error)
            await self.wait_for(lambda: self.filter.volume == 2.5)
        self.assertTrue(any("HA → microphone: volume 2.50, auto gain -24 dBFS, "
                            "noise suppression strong" in line for line in logs.output))
        self.assertEqual((self.filter.auto_gain, self.filter.auto_gain_target_db,
                          self.filter.noise_suppression), (True, -24.0, "strong"))
        status = self.runtime.status()["satellites"][0]
        self.assertEqual(status["mic_settings"]["source"], "home_assistant")
        self.assertEqual(status["ha_mic"]["entities"]["volume"], VOLUME)
        self.assertIsNotNone(status["ha_mic"]["synced_at"])

        # A change in HA, applied without restart
        runner = self.runtime.runners["Bureau"]
        await self.ha.set_state(VOLUME, "3.0")
        await self.ha.set_state(GAIN, "0")
        await self.wait_for(lambda: self.filter.volume == 3.0 and not self.filter.auto_gain)
        self.assertIs(self.runtime.runners["Bureau"], runner)

        # Back to the app's settings
        error, _ = await self.runtime.apply(self.parsed(False))
        self.assertIsNone(error)
        self.assertEqual((self.filter.volume, self.filter.noise_suppression), (1.5, "off"))
        self.assertEqual(self.runtime.status()["satellites"][0]["mic_settings"]["source"], "app")
        self.assertIs(self.runtime.runners["Bureau"], runner)

    async def test_unknown_and_unreachable_keep_the_last_value(self) -> None:
        await self.runtime.start(self.parsed(True))
        await self.wait_for(lambda: self.filter.volume == 2.5)
        await self.ha.set_state(VOLUME, "unavailable")
        await self.ha.set_state(NOISE, "unknown")
        await self.ha.set_state(GAIN, "20")  # comes after: the others were handled
        await self.wait_for(lambda: self.filter.auto_gain_target_db == -20.0)
        self.assertEqual((self.filter.volume, self.filter.noise_suppression), (2.5, "strong"))
        # HA gone: the values stay, the audio goes on
        await self.ha.stop()
        await asyncio.sleep(0.3)
        self.assertEqual(self.filter.volume, 2.5)
        self.assertTrue(self.runtime.runners["Bureau"].task is not None
                        and not self.runtime.runners["Bureau"].task.done())

    async def test_entities_not_found_uses_the_app_settings(self) -> None:
        self.ha.states = {}
        global DEVICES
        devices = DEVICES
        DEVICES = []  # no such device in HA
        try:
            with self.assertLogs("wyoming_vban.ha_audio", "WARNING") as logs:
                await self.runtime.start(self.parsed(True))
                await self.wait_for(lambda: any("not found" in l for l in logs.output))
        finally:
            DEVICES = devices
        self.assertEqual(self.filter.volume, 1.5)

    async def test_calibration_writes_the_ha_entities(self) -> None:
        await self.runtime.start(self.parsed(True))
        await self.wait_for(lambda: self.filter.volume == 2.5)
        supervisor = FakeSupervisor(satellite_options(self.port, self.mic_port, True))
        await supervisor.start()
        ui_port = _free_port()
        ui = WebUI(self.runtime, ui_port, SupervisorOptions(f"http://127.0.0.1:{supervisor.port}"),
                   allowed_ips=frozenset({"127.0.0.1"}))
        await ui.start()
        try:
            async with aiohttp.ClientSession() as http:
                async with http.post(f"http://127.0.0.1:{ui_port}/api/calibration/apply", json={
                    "satellite": "Bureau", "volume": 4.2, "noise_suppression": "light",
                    "auto_gain": True, "auto_gain_target_db": -16, "finish": True,
                }) as resp:
                    self.assertEqual(resp.status, 200, await resp.text())
        finally:
            await ui.stop()
            await supervisor.stop()
        calls = {c["target"]["entity_id"]: (c["domain"], c["service"], c["service_data"])
                 for c in self.ha.services}
        self.assertEqual(calls[VOLUME], ("number", "set_value", {"value": 4.2}))
        self.assertEqual(calls[NOISE], ("select", "select_option", {"option": "low"}))
        self.assertEqual(calls[GAIN], ("number", "set_value", {"value": 16}))
        # The app's options are untouched; the values come back from HA
        self.assertEqual(supervisor.options["satellites"][0]["microphone"]["volume"], 1.5)
        await self.wait_for(lambda: (self.filter.volume, self.filter.noise_suppression,
                                     self.filter.auto_gain_target_db) == (4.2, "light", -16.0))


if __name__ == "__main__":
    unittest.main()
