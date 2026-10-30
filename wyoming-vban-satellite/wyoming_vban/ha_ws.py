"""Watch media_player states through the Home Assistant WebSocket API.

One connection for the whole app (via the Supervisor proxy), subscribed to
state changes of the media_players used for TTS output, reconnecting with
a backoff. Lets satellites wait for the real start and end of playback.
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
    """Shared WebSocket subscription to media_player state changes."""

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
                        await self._subscribe(ws)
                        self._connected = True
                        backoff = self.BACKOFF_MIN_S
                        _LOGGER.info(
                            "HA WebSocket connected, watching %s",
                            ", ".join(self.entity_ids),
                        )
                        await self._receive(ws)
                    _LOGGER.warning("HA WebSocket closed")
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as err:
                    _LOGGER.warning("HA WebSocket error: %s", err or type(err).__name__)
                finally:
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
            "id": 1,
            "type": "subscribe_trigger",
            "trigger": {"platform": "state", "entity_id": self.entity_ids},
        })
        msg = await ws.receive_json(timeout=10)
        if not msg.get("success"):
            raise ValueError(f"subscribe_trigger failed: {msg.get('error', msg)}")

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            msg = message.json()
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
