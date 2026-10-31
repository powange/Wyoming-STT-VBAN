"""media_player state watcher against a fake HA WebSocket API.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import asyncio
import os
import socket
import time
import unittest

from aiohttp import WSMsgType, web

from wyoming_vban.ha_ws import HaStateWatcher

ENTITY = "media_player.nestmini7039"


def _free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeHomeAssistant:
    """Minimal HA WebSocket API: auth, subscribe_trigger, state events."""

    def __init__(self, token: str = "token"):
        self.token = token
        self.sockets: list[web.WebSocketResponse] = []
        self.subscriptions: list[dict] = []
        self.received: list[dict] = []  # messages after the subscription
        self.subscribed = asyncio.Event()
        self.port = _free_tcp_port()
        self._runner: web.AppRunner

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/websocket", self._handle)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def stop(self) -> None:
        await self._runner.cleanup()

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}/websocket"

    async def _handle(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await ws.send_json({"type": "auth_required"})
        auth = await ws.receive_json()
        if auth.get("access_token") != self.token:
            await ws.send_json({"type": "auth_invalid", "message": "bad token"})
            await ws.close()
            return ws
        await ws.send_json({"type": "auth_ok"})
        self.sockets.append(ws)
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            data = msg.json()
            if data.get("type", "").startswith("subscribe_"):
                self.subscriptions.append(data)
                await ws.send_json({"id": data["id"], "type": "result", "success": True})
                self.subscribed.set()
            else:
                self.received.append(data)
        return ws

    def _subscription_id(self, msg_type: str, event_type: str = "") -> int:
        return next(
            s["id"] for s in reversed(self.subscriptions)
            if s["type"] == msg_type and s.get("event_type", "") == event_type
        )

    async def fire_ha_event(self, event_type: str, data: dict) -> None:
        """Send a HA bus event to the subscribers of its type."""
        sub_id = self._subscription_id("subscribe_events", event_type)
        for ws in self.sockets:
            await ws.send_json({
                "id": sub_id, "type": "event",
                "event": {"event_type": event_type, "data": data},
            })

    async def set_state(self, state: str, media_content_id: str = "") -> None:
        attributes = {"media_content_id": media_content_id} if media_content_id else {}
        for ws in self.sockets:
            await ws.send_json({
                "id": self._subscription_id("subscribe_trigger"),
                "type": "event",
                "event": {"variables": {"trigger": {
                    "platform": "state",
                    "entity_id": ENTITY,
                    "to_state": {
                        "entity_id": ENTITY, "state": state, "attributes": attributes,
                    },
                }}},
            })

    async def drop_connections(self) -> None:
        for ws in self.sockets:
            await ws.close()
        self.sockets.clear()
        self.subscribed.clear()


class HaStateWatcherTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["SUPERVISOR_TOKEN"] = "token"
        self.ha = FakeHomeAssistant()
        await self.ha.start()
        self.watcher = HaStateWatcher([ENTITY], url=self.ha.url)
        self.watcher.BACKOFF_MIN_S = 0.05
        self.task = asyncio.create_task(self.watcher.run())
        await asyncio.wait_for(self.ha.subscribed.wait(), 5)
        await asyncio.sleep(0.05)

    async def asyncTearDown(self) -> None:
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)
        await self.ha.stop()

    async def test_subscribes_to_tts_media_players_only(self) -> None:
        self.assertTrue(self.watcher.connected)
        self.assertEqual(self.ha.subscriptions[0]["type"], "subscribe_trigger")
        self.assertEqual(
            self.ha.subscriptions[0]["trigger"],
            {"platform": "state", "entity_id": [ENTITY]},
        )

    async def test_wait_for_playing_then_end(self) -> None:
        since = time.monotonic()
        url = "http://host:10800/tts/abc.wav"

        async def cast() -> None:
            await asyncio.sleep(0.1)
            await self.ha.set_state("playing", url)
            await asyncio.sleep(0.3)
            await self.ha.set_state("idle")

        cast_task = asyncio.create_task(cast())
        started = await self.watcher.wait_for(
            ENTITY, lambda c: c.state == "playing", since, timeout=2,
        )
        self.assertIsNotNone(started)
        self.assertAlmostEqual(started.time - since, 0.1, delta=0.08)

        finished = await self.watcher.wait_for(
            ENTITY, lambda c: c.state != "playing", started.time, timeout=2,
        )
        self.assertIsNotNone(finished)
        self.assertEqual(finished.state, "idle")
        self.assertAlmostEqual(finished.time - started.time, 0.3, delta=0.08)
        await cast_task

    async def test_change_before_since_is_ignored(self) -> None:
        await self.ha.set_state("playing", "old")
        await asyncio.sleep(0.05)
        result = await self.watcher.wait_for(
            ENTITY, lambda c: c.state == "playing", time.monotonic(), timeout=0.2,
        )
        self.assertIsNone(result)

    async def test_fire_event(self) -> None:
        data = {"satellite": "Bureau", "transcript": "a", "response": "b",
                "followup": False}
        self.assertTrue(await self.watcher.fire_event("jarvis_echange", data))
        await asyncio.sleep(0.1)
        msg = self.ha.received[-1]
        self.assertEqual(msg["type"], "fire_event")
        self.assertEqual(msg["event_type"], "jarvis_echange")
        self.assertEqual(msg["event_data"], data)
        self.assertGreater(msg["id"], self.ha.subscriptions[0]["id"])

    async def test_fire_event_not_connected(self) -> None:
        await self.ha.drop_connections()
        await asyncio.sleep(0.05)
        self.assertFalse(await self.watcher.fire_event("jarvis_echange", {}))

    async def test_custom_event_listener(self) -> None:
        # Listeners are registered before run(): use a second watcher
        received = []
        watcher = HaStateWatcher([], url=self.ha.url)
        watcher.add_event_listener("jarvis_recherche", received.append)
        task = asyncio.create_task(watcher.run())
        await asyncio.sleep(0.2)
        try:
            self.assertTrue(watcher.connected)
            await self.ha.fire_ha_event("jarvis_recherche", {"question": "actualités"})
            await asyncio.sleep(0.1)
            self.assertEqual(received, [{"question": "actualités"}])
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_disconnect_returns_none_and_reconnects(self) -> None:
        waiter = asyncio.create_task(self.watcher.wait_for(
            ENTITY, lambda c: c.state == "playing", time.monotonic(), timeout=5,
        ))
        await asyncio.sleep(0.05)
        await self.ha.drop_connections()
        self.assertIsNone(await asyncio.wait_for(waiter, 2))

        await asyncio.wait_for(self.ha.subscribed.wait(), 5)
        await asyncio.sleep(0.05)
        self.assertTrue(self.watcher.connected)


if __name__ == "__main__":
    unittest.main()
