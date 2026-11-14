"""Conversation agent prewarmed with each satellite's context.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
"""

import asyncio
import copy
import os
import socket
import tempfile
import time
import unittest

from aiohttp import WSMsgType, web

from test_webui import _free_port
from wyoming_vban.config import parse_options, validate
from wyoming_vban.prewarm import EVENT_TYPE, TEXT, new_conversation_id
from wyoming_vban.runtime import AppRuntime

DEVICES = [
    {"id": "dev-bureau", "name": "Bureau", "identifiers": [["wyoming", "sat-b"]]},
    {"id": "dev-salon", "name": "Salon", "identifiers": [["wyoming", "sat-s"]]},
    {"id": "dev-autre", "name": "Bureau", "identifiers": [["esphome", "x"]]},
]
ENTITIES = [
    {"entity_id": "select.bureau_assistant", "unique_id": "sat-b-pipeline", "platform": "wyoming"},
    {"entity_id": "select.salon_assistant", "unique_id": "sat-s-pipeline", "platform": "wyoming"},
]
STATES = {"select.bureau_assistant": "preferred", "select.salon_assistant": "Salon local"}
PIPELINES = {
    "pipelines": [
        {"id": "p1", "name": "Jarvis", "conversation_engine": "conversation.ollama"},
        {"id": "p2", "name": "Salon local", "conversation_engine": "conversation.home_assistant"},
    ],
    "preferred_pipeline": "p1",
}


class FakeHomeAssistant:
    """HA WebSocket: registries, pipelines, conversation/process (slow)."""

    def __init__(self) -> None:
        self.port = _free_port()
        self.processed: list[dict] = []  # conversation/process, with start and end
        self.other: list[dict] = []  # services called, events fired
        self.event_subs: dict[str, int] = {}
        self.sockets: list[web.WebSocketResponse] = []

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

    async def fire(self, data: dict) -> None:
        for ws in self.sockets:
            await ws.send_json({"id": self.event_subs[EVENT_TYPE], "type": "event",
                                "event": {"event_type": EVENT_TYPE, "data": data}})

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
            asyncio.create_task(self._answer(ws, data))
        return ws

    async def _answer(self, ws: web.WebSocketResponse, data: dict) -> None:
        kind, result = data["type"], None
        if kind == "subscribe_events":
            self.event_subs[data["event_type"]] = data["id"]
        elif kind == "config/device_registry/list":
            result = DEVICES
        elif kind == "config/entity_registry/list":
            result = ENTITIES
        elif kind == "assist_pipeline/pipeline/list":
            result = PIPELINES
        elif kind == "conversation/process":
            entry = dict(data, start=time.monotonic())
            self.processed.append(entry)
            await asyncio.sleep(0.3)  # the agent thinking
            entry["end"] = time.monotonic()
            result = {"response": {"speech": {"plain": {"speech": "OK"}}}}
        elif kind in ("call_service", "fire_event"):
            self.other.append(data)
        await ws.send_json({"id": data["id"], "type": "result", "success": True,
                            "result": result})
        if kind == "subscribe_entities":
            await ws.send_json({"id": data["id"], "type": "event", "event": {"a": {
                e: {"s": STATES[e], "a": {}} for e in data["entity_ids"] if e in STATES}}})


def satellite(name: str) -> dict:
    return {
        "name": name, "wyoming_port": _free_port(), "output": "none",
        "microphone": {"vban_port": _free_port(socket.SOCK_DGRAM)},
        "vban_output": {}, "media_player": {}, "followup": {"enabled": False},
        "waiting_sounds": {}, "camera_phrase": {},
    }


class PrewarmTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["SUPERVISOR_TOKEN"] = "token"
        self.ha = FakeHomeAssistant()
        await self.ha.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = AppRuntime(self.tmp.name)
        self.runtime.ha_ws_url = self.ha.url
        options = {"satellites": [satellite("Bureau"), satellite("Salon")],
                   "publish_exchanges": True, "debug_mirror": []}
        parsed = parse_options(copy.deepcopy(options))
        validate(parsed.satellites)
        await self.runtime.start(parsed)
        for _ in range(100):  # connected and subscribed to the event
            if EVENT_TYPE in self.ha.event_subs and self.runtime._watcher.connected:
                break
            await asyncio.sleep(0.05)

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()
        await self.ha.stop()
        self.tmp.cleanup()

    async def wait_processed(self, count: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len([p for p in self.ha.processed if "end" in p]) >= count:
                return
            await asyncio.sleep(0.05)
        self.fail(f"{len(self.ha.processed)} prewarm(s) instead of {count}")

    async def test_each_satellite_in_turn_with_its_context(self) -> None:
        with self.assertLogs("wyoming_vban.prewarm", "INFO") as logs:
            await self.ha.fire({})
            await self.wait_processed(2)
            await asyncio.sleep(0.1)
        bureau, salon = self.ha.processed
        self.assertEqual((bureau["device_id"], bureau["agent_id"]),
                         ("dev-bureau", "conversation.ollama"))  # preferred pipeline
        self.assertEqual((salon["device_id"], salon["agent_id"]),
                         ("dev-salon", "conversation.home_assistant"))
        for entry in (bureau, salon):
            self.assertEqual((entry["text"], entry["language"]), (TEXT, "fr"))
        self.assertNotEqual(bureau["conversation_id"], salon["conversation_id"])
        self.assertGreaterEqual(salon["start"], bureau["end"])  # never in parallel
        self.assertEqual(self.ha.other, [])  # no jarvis_echange, no play_media
        self.assertTrue(any("Prewarm Bureau: 0.3 s" in line for line in logs.output), logs.output)

    async def test_only_the_satellites_asked(self) -> None:
        await self.ha.fire({"satellites": ["Salon"]})
        await self.wait_processed(1)
        await asyncio.sleep(0.5)
        self.assertEqual([p["device_id"] for p in self.ha.processed], ["dev-salon"])

    async def test_busy_satellite_is_postponed(self) -> None:
        prewarmer = self.runtime.prewarmer
        prewarmer.DEFER_S = 0.4
        busy = [True]
        self.runtime.runners["Bureau"].in_exchange = lambda: busy[0]
        with self.assertLogs("wyoming_vban.prewarm", "INFO") as logs:
            await self.ha.fire({})
            await self.wait_processed(1)
            self.assertEqual(self.ha.processed[0]["device_id"], "dev-salon")
            busy[0] = False
            await self.wait_processed(2)
        self.assertEqual(self.ha.processed[1]["device_id"], "dev-bureau")
        self.assertTrue(any("Prewarm Bureau: satellite busy" in line for line in logs.output))

    async def test_after_inactivity(self) -> None:
        prewarmer = self.runtime.prewarmer
        self.runtime.options.prewarm_idle_minutes = 1
        prewarmer.last_activity = {"Bureau": time.monotonic() - 61, "Salon": time.monotonic()}
        prewarmer.IDLE_CHECK_S = 0.1
        await prewarmer.stop()  # (again with the short check)
        prewarmer.start()
        await self.wait_processed(1)
        await asyncio.sleep(0.4)
        self.assertEqual([p["device_id"] for p in self.ha.processed], ["dev-bureau"])

    def test_conversation_ids_are_new_ulids(self) -> None:
        ids = {new_conversation_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)
        self.assertTrue(all(len(i) == 26 for i in ids))


if __name__ == "__main__":
    unittest.main()
