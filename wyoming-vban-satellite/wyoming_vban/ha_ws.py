"""Home Assistant WebSocket API connection, shared by the whole app.

One connection (via the Supervisor proxy), reconnecting with a backoff:
- subscribed to state changes of the media_players used for TTS output,
  so satellites can wait for the real start and end of playback;
- used to fire events on the HA event bus (voice exchanges).
"""

import asyncio
import collections
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_WS_URL = "ws://supervisor/core/websocket"

@dataclass
class StateChange:
    """One state change of an entity, as seen by this app."""

    time: float  # time.monotonic() when received
    state: str
    attributes: dict[str, Any]


class HaStateWatcher:
    """Shared WebSocket connection: media_player states and fire_event."""

    BACKOFF_MIN_S = 1.0
    BACKOFF_MAX_S = 30.0
    HISTORY_LEN = 20  # state changes kept per entity

    def __init__(self, entity_ids: list[str], url: str = SUPERVISOR_WS_URL):
        self.entity_ids = sorted(set(entity_ids))
        self.url = url
        self._token = os.environ.get("SUPERVISOR_TOKEN", "")
        self._connected = False
        self._history: dict[str, collections.deque[StateChange]] = {
            entity_id: collections.deque(maxlen=self.HISTORY_LEN)
            for entity_id in self.entity_ids
        }
        self._changed = asyncio.Condition()
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._next_id = 1  # message id, per connection
        self._send_lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._connected

    async def run(self) -> None:
        """Connect, subscribe and receive state changes forever (reconnecting)."""
        backoff = self.BACKOFF_MIN_S
        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    async with session.ws_connect(self.url, heartbeat=30) as ws:
                        await self._authenticate(ws)
                        self._next_id = 1
                        if self.entity_ids:
                            await self._subscribe(ws)
                        self._ws = ws
                        self._connected = True
                        backoff = self.BACKOFF_MIN_S
                        _LOGGER.info(
                            "HA WebSocket connected%s",
                            f", watching {', '.join(self.entity_ids)}"
                            if self.entity_ids else "",
                        )
                        await self._receive(ws)
                    _LOGGER.warning("HA WebSocket closed")
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as err:
                    _LOGGER.warning("HA WebSocket error: %s", err or type(err).__name__)
                finally:
                    self._ws = None
                    if self._connected:
                        self._connected = False
                        await self._notify()

                _LOGGER.info("Reconnecting to HA WebSocket in %.0fs", backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.BACKOFF_MAX_S)

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        msg = await ws.receive_json(timeout=10)
        if msg.get("type") != "auth_required":
            raise ValueError(f"unexpected message {msg.get('type')}")
        await ws.send_json({"type": "auth", "access_token": self._token})
        msg = await ws.receive_json(timeout=10)
        if msg.get("type") != "auth_ok":
            raise ValueError(f"authentication failed: {msg.get('message', msg)}")

    async def _subscribe(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # A state trigger with only entity_id also fires on attribute
        # changes, e.g. a new media_content_id while already playing.
        await ws.send_json({
            "id": self._take_id(),
            "type": "subscribe_trigger",
            "trigger": {"platform": "state", "entity_id": self.entity_ids},
        })
        msg = await ws.receive_json(timeout=10)
        if not msg.get("success"):
            raise ValueError(f"subscribe_trigger failed: {msg.get('error', msg)}")

    def _take_id(self) -> int:
        msg_id = self._next_id
        self._next_id += 1
        return msg_id

    async def fire_event(self, event_type: str, event_data: dict[str, Any]) -> bool:
        """Fire an event on the HA event bus. False if not connected.

        Fire-and-forget: HA's result message is not awaited.
        """
        ws = self._ws
        if ws is None or not self._connected:
            return False
        try:
            async with self._send_lock:
                await ws.send_json({
                    "id": self._take_id(),
                    "type": "fire_event",
                    "event_type": event_type,
                    "event_data": event_data,
                })
        except (aiohttp.ClientError, ConnectionError, RuntimeError) as err:
            _LOGGER.debug("fire_event %s failed: %s", event_type, err)
            return False
        return True

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            msg = message.json()
            if msg.get("type") == "result" and not msg.get("success"):
                _LOGGER.warning("HA WebSocket command failed: %s", msg.get("error", msg))
            if msg.get("type") != "event":
                continue
            trigger = msg.get("event", {}).get("variables", {}).get("trigger", {})
            to_state = trigger.get("to_state") or {}
            entity_id = to_state.get("entity_id") or trigger.get("entity_id")
            if entity_id not in self._history:
                continue
            change = StateChange(
                time=time.monotonic(),
                state=to_state.get("state", "unavailable"),
                attributes=to_state.get("attributes") or {},
            )
            self._history[entity_id].append(change)
            _LOGGER.debug("%s -> %s", entity_id, change.state)
            await self._notify()

    async def _notify(self) -> None:
        async with self._changed:
            self._changed.notify_all()

    async def wait_for(
        self,
        entity_id: str,
        predicate: Callable[[StateChange], bool],
        since: float,
        timeout: float,
    ) -> Optional[StateChange]:
        """Wait for a state change received after `since` matching predicate.

        Returns None on timeout or if the connection is lost meanwhile.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout

        def match() -> Optional[StateChange]:
            for change in self._history.get(entity_id, ()):
                if change.time >= since and predicate(change):
                    return change
            return None

        async with self._changed:
            while True:
                if (found := match()) is not None:
                    return found
                if not self._connected:
                    return None
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._changed.wait(), remaining)
                except asyncio.TimeoutError:
                    return match()
