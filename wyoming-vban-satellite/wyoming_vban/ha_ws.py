"""Home Assistant WebSocket API connection, shared by the whole app.

One connection (via the Supervisor proxy), reconnecting with a backoff:
- subscribed to state changes of the media_players used for TTS output,
  so satellites can wait for the real start and end of playback;
- subscribed to custom HA events (e.g. jarvis_recherche);
- used to fire events on the HA event bus (voice exchanges);
- commands with their result and subscriptions made while connected
  (microphone entities of the satellites, see ha_audio).
"""

import asyncio
import collections
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

import aiohttp

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_WS_URL = "ws://supervisor/core/websocket"


class HaUnavailable(Exception):
    """Not connected to HA, connection lost or no answer."""


class HaCommandError(Exception):
    """HA answered a command with an error."""


@dataclass
class StateChange:
    """One state change of an entity, as seen by this app."""

    time: float  # time.monotonic() when received
    state: str
    attributes: dict[str, Any]


class HaStateWatcher:
    """Shared WebSocket connection: media_player states, HA events, fire_event."""

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
        # HA event type -> callbacks(event data), subscribed on each connection
        self._event_listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._event_sub_ids: dict[int, str] = {}  # subscription id -> event type
        self._resubscribe = False  # reconnect at once (entities changed)
        self._results: dict[int, asyncio.Future] = {}  # command id -> its result
        # Subscriptions made with subscribe(): id -> callback(event), per connection
        self._subscriptions: dict[int, Callable[[dict[str, Any]], None]] = {}
        # Called (in a task) on each connection, e.g. to subscribe again
        self._connect_listeners: list[Callable[[], Awaitable[None]]] = []
        self._listener_tasks: set[asyncio.Task] = set()

    async def set_entity_ids(self, entity_ids: list[str]) -> None:
        """Watch other media_players: reconnects to subscribe to them."""
        entity_ids = sorted(set(entity_ids))
        if entity_ids == self.entity_ids:
            return
        self.entity_ids = entity_ids
        self._history = {
            entity_id: self._history.get(entity_id)
            or collections.deque(maxlen=self.HISTORY_LEN)
            for entity_id in entity_ids
        }
        if self._ws is not None:
            self._resubscribe = True
            await self._ws.close()

    def add_event_listener(
        self, event_type: str, callback: Callable[[dict[str, Any]], None]
    ) -> None:
        """Call callback(data) for each HA event of this type (before run())."""
        self._event_listeners.setdefault(event_type, []).append(callback)

    def add_connect_listener(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Run callback() on each connection (and now, if connected)."""
        self._connect_listeners.append(callback)
        if self._connected:
            self._spawn(callback())

    def remove_connect_listener(self, callback: Callable[[], Awaitable[None]]) -> None:
        if callback in self._connect_listeners:
            self._connect_listeners.remove(callback)

    def _spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self._listener_tasks.add(task)
        task.add_done_callback(self._listener_done)

    def _listener_done(self, task: asyncio.Task) -> None:
        self._listener_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("Error in HA WebSocket connect listener",
                          exc_info=task.exception())

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
                        self._event_sub_ids = {}
                        if self.entity_ids:
                            await self._subscribe(ws)
                        for event_type in self._event_listeners:
                            await self._subscribe_event(ws, event_type)
                        self._ws = ws
                        self._connected = True
                        backoff = self.BACKOFF_MIN_S
                        _LOGGER.info(
                            "HA WebSocket connected%s",
                            f", watching {', '.join(self.entity_ids)}"
                            if self.entity_ids else "",
                        )
                        for callback in self._connect_listeners:
                            self._spawn(callback())
                        await self._receive(ws)
                    if not self._resubscribe:
                        _LOGGER.warning("HA WebSocket closed")
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError) as err:
                    _LOGGER.warning("HA WebSocket error: %s", err or type(err).__name__)
                finally:
                    self._ws = None
                    self._subscriptions = {}
                    for future in self._results.values():
                        if not future.done():
                            future.set_exception(HaUnavailable("connection lost"))
                    self._results = {}
                    if self._connected:
                        self._connected = False
                        await self._notify()
                if self._resubscribe:  # entities changed: reconnect at once
                    self._resubscribe = False
                    continue

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

    async def _subscribe_event(
        self, ws: aiohttp.ClientWebSocketResponse, event_type: str
    ) -> None:
        msg_id = self._take_id()
        await ws.send_json(
            {"id": msg_id, "type": "subscribe_events", "event_type": event_type}
        )
        msg = await ws.receive_json(timeout=10)
        if not msg.get("success"):
            raise ValueError(
                f"subscribe_events {event_type} failed: {msg.get('error', msg)}"
            )
        self._event_sub_ids[msg_id] = event_type

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

    async def call(self, command: dict[str, Any], timeout: float = 10.0) -> Any:
        """Send a command and return its result.

        Raises HaUnavailable (not connected, lost, no answer) or HaCommandError.
        """
        return (await self._command(command, timeout))[1]

    async def subscribe(
        self, command: dict[str, Any], on_event: Callable[[dict[str, Any]], None],
    ) -> int:
        """Subscribe (subscribe_trigger, subscribe_events…): on_event(event)
        for each of its events, until unsubscribe() or the connection is
        lost. Returns the subscription id."""
        return (await self._command(command, 10.0, on_event))[0]

    async def _command(
        self, command: dict[str, Any], timeout: float,
        on_event: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> tuple[int, Any]:
        ws = self._ws
        if ws is None or not self._connected:
            raise HaUnavailable("not connected to Home Assistant")
        future = asyncio.get_running_loop().create_future()
        async with self._send_lock:
            msg_id = self._take_id()
            self._results[msg_id] = future
            if on_event is not None:  # events may come right after the result
                self._subscriptions[msg_id] = on_event
            try:
                await ws.send_json({**command, "id": msg_id})
            except (aiohttp.ClientError, ConnectionError, RuntimeError) as err:
                self._results.pop(msg_id, None)
                self._subscriptions.pop(msg_id, None)
                raise HaUnavailable(str(err) or type(err).__name__) from err
        try:
            return msg_id, await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._subscriptions.pop(msg_id, None)
            raise HaUnavailable(f"no answer to {command.get('type')}") from None
        except HaCommandError:
            self._subscriptions.pop(msg_id, None)
            raise
        finally:
            self._results.pop(msg_id, None)

    async def unsubscribe(self, sub_id: int) -> None:
        if self._subscriptions.pop(sub_id, None) is None:
            return  # lost with the connection
        try:
            await self.call({"type": "unsubscribe_events", "subscription": sub_id})
        except (HaUnavailable, HaCommandError) as err:
            _LOGGER.debug("unsubscribe %d: %s", sub_id, err)

    async def _receive(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for message in ws:
            if message.type != aiohttp.WSMsgType.TEXT:
                break
            msg = message.json()
            if msg.get("type") == "result":
                future = self._results.get(msg.get("id"))
                if future is not None and not future.done():
                    if msg.get("success"):
                        future.set_result(msg.get("result"))
                    else:
                        error = msg.get("error") or {}
                        future.set_exception(HaCommandError(error.get("message") or str(error)))
                elif not msg.get("success"):
                    _LOGGER.warning("HA WebSocket command failed: %s", msg.get("error", msg))
            if msg.get("type") != "event":
                continue
            if (on_event := self._subscriptions.get(msg.get("id"))) is not None:
                try:
                    on_event(msg.get("event") or {})
                except Exception:  # a listener bug must not drop the connection
                    _LOGGER.exception("Error in HA subscription listener")
                continue
            if (event_type := self._event_sub_ids.get(msg.get("id"))) is not None:
                data = msg.get("event", {}).get("data") or {}
                _LOGGER.debug("HA event %s: %s", event_type, data)
                for callback in self._event_listeners.get(event_type, []):
                    try:
                        callback(data)
                    except Exception:  # a listener bug must not drop the connection
                        _LOGGER.exception("Error in %s listener", event_type)
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
