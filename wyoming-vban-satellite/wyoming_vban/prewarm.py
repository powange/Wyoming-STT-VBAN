"""Prewarming the conversation agent's cache with each satellite's context.

Home Assistant builds a different system prompt for each device asking
(its area, timers support…), placed before the exposed entities: the
conversation.process action, without a device, warms a prompt the
satellites never use. The first question of a satellite after a quiet time
(midnight, a restart of HA, a change of exposed entities) then reads the
whole prompt again: up to 46 s with Ollama, instead of 3 to 4 s.

On the HA event jarvis_prechauffage (data: satellites, optional list; all
by default), and after prewarm_idle_minutes without a question, each
satellite's prompt is warmed in turn (Ollama answers one request at a
time) with the WebSocket command conversation/process: the agent of the
satellite's pipeline, its device_id, a new conversation. Nothing is played,
nothing is published (no jarvis_echange), the satellite's state doesn't
change. A satellite listening, thinking or answering is warmed DEFER_S later.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from .ha_audio import DOMAIN
from .ha_ws import HaCommandError, HaStateWatcher, HaUnavailable

if TYPE_CHECKING:
    from .runtime import SatelliteRunner

_LOGGER = logging.getLogger(__name__)

EVENT_TYPE = "jarvis_prechauffage"
TEXT = "Préchauffage, réponds seulement OK."
LANGUAGE = "fr"
PREFERRED = "preferred"  # pipeline select: the preferred pipeline
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_conversation_id() -> str:
    """A fresh ULID: HA starts a new conversation (the satellite's own one
    keeps its history)."""
    value = (int(time.time() * 1000) << 80) | int.from_bytes(os.urandom(10), "big")
    return "".join(_CROCKFORD[(value >> (5 * i)) & 31] for i in reversed(range(26)))


class PrewarmError(Exception):
    """The satellite's device or conversation agent can't be found."""


class Prewarmer:
    DEFER_S = 30.0  # a busy satellite is warmed this much later
    TIMEOUT_S = 90.0  # per satellite
    IDLE_CHECK_S = 60.0

    def __init__(
        self, watcher: Callable[[], Optional[HaStateWatcher]],
        runners: Callable[[], dict[str, SatelliteRunner]],
        idle_minutes: Callable[[], int],
    ) -> None:
        self._watcher = watcher
        self._runners = runners
        self._idle_minutes = idle_minutes
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._pending: set[str] = set()  # queued or deferred
        self._task: Optional[asyncio.Task] = None
        self._idle_task: Optional[asyncio.Task] = None
        self._deferred: dict[str, asyncio.TimerHandle] = {}
        # Last question (or prewarm) of each satellite, for the idle prewarm
        self.last_activity: dict[str, float] = {}

    # Lifecycle

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._work())
        if self._idle_task is None:
            self._idle_task = asyncio.create_task(self._watch_idle())

    async def stop(self) -> None:
        for handle in self._deferred.values():
            handle.cancel()
        self._deferred.clear()
        for task in (self._task, self._idle_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(*(t for t in (self._task, self._idle_task) if t),
                             return_exceptions=True)
        self._task = self._idle_task = None
        self._queue = asyncio.Queue()
        self._pending.clear()

    # Requests

    def on_event(self, data: dict[str, Any]) -> None:
        """HA event jarvis_prechauffage."""
        names = data.get("satellites")
        known = list(self._runners())
        if names is None:
            names = known
        elif isinstance(names, str):
            names = [names]
        elif not isinstance(names, list):
            _LOGGER.warning("%s: 'satellites' must be a list: %r", EVENT_TYPE, names)
            return
        for name in names:
            if name not in known:
                _LOGGER.warning("%s: unknown satellite '%s'", EVENT_TYPE, name)
        self.request([n for n in names if n in known])

    def request(self, names: list[str]) -> None:
        """Warm these satellites, in turn (each one once)."""
        for name in names:
            if name in self._pending:
                continue
            self._pending.add(name)
            self._queue.put_nowait(name)

    def question(self, name: str) -> None:
        """The satellite asked something: its cache is warm."""
        self.last_activity[name] = time.monotonic()

    # Work

    async def _work(self) -> None:
        while True:
            name = await self._queue.get()
            try:
                await self._prewarm(name)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - the next ones still go
                _LOGGER.exception("Prewarm %s failed", name)
                self._pending.discard(name)

    async def _prewarm(self, name: str) -> None:
        runner = self._runners().get(name)
        if runner is None:
            self._pending.discard(name)
            return
        if runner.in_exchange():
            _LOGGER.info("Prewarm %s: satellite busy, again in %.0f s", name, self.DEFER_S)
            self._deferred[name] = asyncio.get_running_loop().call_later(
                self.DEFER_S, self._requeue, name)
            return
        self._pending.discard(name)
        watcher = self._watcher()
        if watcher is None or not watcher.connected:
            _LOGGER.warning("Prewarm %s: Home Assistant not connected", name)
            return
        start = time.monotonic()
        try:
            device_id, agent_id = await self._target(watcher, name)
            await watcher.call({
                "type": "conversation/process", "text": TEXT, "language": LANGUAGE,
                "agent_id": agent_id, "device_id": device_id,
                "conversation_id": new_conversation_id(),
            }, timeout=self.TIMEOUT_S)
        except (HaUnavailable, HaCommandError, PrewarmError) as err:
            _LOGGER.warning("Prewarm %s failed after %.1f s: %s",
                            name, time.monotonic() - start, err)
            return
        self.last_activity[name] = time.monotonic()
        # Under ~6 s: the cache was good; 20 s and more: the prompt was read again
        _LOGGER.info("Prewarm %s: %.1f s", name, time.monotonic() - start)

    def _requeue(self, name: str) -> None:
        self._deferred.pop(name, None)
        self._pending.discard(name)
        self.request([name])

    async def _target(self, watcher: HaStateWatcher, name: str) -> tuple[str, str]:
        """(device_id, conversation agent) of the satellite, from HA's
        registries and its pipeline select (never from entity_ids, which
        can be renamed)."""
        devices = await watcher.call({"type": "config/device_registry/list"}) or []
        entities = await watcher.call({"type": "config/entity_registry/list"}) or []
        by_unique_id = {e.get("unique_id"): e for e in entities if e.get("platform") == DOMAIN}
        for device in devices:
            if device.get("name") != name:
                continue
            for identifier in device.get("identifiers") or []:
                if len(identifier) != 2 or identifier[0] != DOMAIN:
                    continue
                select = by_unique_id.get(f"{identifier[1]}-pipeline")
                if select is not None:
                    agent = await self._agent(watcher, select["entity_id"])
                    return device["id"], agent
        raise PrewarmError(f"no Wyoming device named '{name}' in Home Assistant")

    @staticmethod
    async def _agent(watcher: HaStateWatcher, select_entity: str) -> str:
        chosen = await watcher.state_of(select_entity)
        listing = await watcher.call({"type": "assist_pipeline/pipeline/list"}) or {}
        pipelines = listing.get("pipelines") or []
        if chosen in (None, PREFERRED, "unknown", "unavailable"):
            wanted = listing.get("preferred_pipeline")
            pipeline = next((p for p in pipelines if p.get("id") == wanted), None)
        else:
            pipeline = next((p for p in pipelines if p.get("name") == chosen), None)
        if pipeline is None or not pipeline.get("conversation_engine"):
            raise PrewarmError(f"pipeline '{chosen}' not found")
        return pipeline["conversation_engine"]

    async def _watch_idle(self) -> None:
        """prewarm_idle_minutes without a question: warm the satellite again."""
        while True:
            await asyncio.sleep(self.IDLE_CHECK_S)
            minutes = self._idle_minutes()
            if minutes <= 0:
                continue
            now = time.monotonic()
            for name in list(self._runners()):
                last = self.last_activity.setdefault(name, now)
                if now - last >= minutes * 60:
                    self.last_activity[name] = now  # (once per idle period)
                    _LOGGER.debug("Prewarm %s: %d min without a question", name, minutes)
                    self.request([name])
