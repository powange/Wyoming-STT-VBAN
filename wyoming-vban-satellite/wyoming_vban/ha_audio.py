"""Home Assistant's microphone entities of the Wyoming satellites, applied
by the app.

For each Wyoming satellite, HA creates "Mic volume", "Auto gain" and "Noise
suppression level" entities but doesn't pass them to the pipeline (only
ESPHome satellites get them). With microphone.follow_ha_entities, the app
reads them and applies them to its own clean-up (MicFilter).

The entities are found through the registries, never from their entity_id
(it can be renamed): the Wyoming device whose name is the satellite's name
(the name the app announces; a name given by the user is kept apart, in
name_by_user), then its entities by unique_id: {satellite_id}-auto_gain,
{satellite_id}-noise_suppression_level, {satellite_id}-volume_multiplier.
HA's APIs don't give the host and port of a Wyoming config entry.

Only HA → app: the app never writes these entities on its own (no loops),
except the calibration's "Apply", at the user's request.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from .config import AUTO_GAIN_TARGET_RANGE, MIC_VOLUME_RANGE
from .ha_ws import HaCommandError, HaStateWatcher, HaUnavailable
from .mic_filter import AutoGain

_LOGGER = logging.getLogger(__name__)

DOMAIN = "wyoming"
# unique_id suffix of each entity -> setting
ENTITY_KEYS = {
    "volume_multiplier": "volume",
    "auto_gain": "auto_gain",
    "noise_suppression_level": "noise_suppression",
}
# HA's levels -> the app's (the app has no level above strong)
HA_NOISE_LEVELS = {
    "off": "off", "low": "light", "medium": "medium", "high": "strong", "max": "strong",
}
HA_AUTO_GAIN_MAX = 31
# The app's levels -> HA's (calibration writing the select entity)
APP_NOISE_LEVELS = {"off": "off", "light": "low", "medium": "medium", "strong": "high"}


@dataclass
class HaMicSettings:
    """Last valid values of a satellite's HA entities (None: none yet)."""

    volume: Optional[float] = None
    noise_suppression: Optional[str] = None  # the app's level
    auto_gain: Optional[int] = None  # HA's value, 0-31 dB (0: off)
    synced_at: Optional[float] = None  # time.time() of the last valid value


def parse_state(setting: str, state: Any) -> Any:
    """The setting's value from an entity state; None if not valid
    (unknown, unavailable…): the last valid value is kept."""
    if state in (None, "unknown", "unavailable", ""):
        return None
    if setting == "noise_suppression":
        return HA_NOISE_LEVELS.get(str(state))
    try:
        value = float(state)
    except (TypeError, ValueError):
        return None
    if setting == "volume":
        return round(max(MIC_VOLUME_RANGE[0], min(MIC_VOLUME_RANGE[1], value)), 2)
    return int(max(0, min(HA_AUTO_GAIN_MAX, value)))


def auto_gain_settings(value: int) -> tuple[bool, float]:
    """HA's auto gain (0-31 dB) -> (on, target in dBFS): 0 is off, N aims
    at -N dBFS (bounded to -31..-3)."""
    if value <= 0:
        return False, AutoGain.TARGET_DB
    low, high = AUTO_GAIN_TARGET_RANGE
    return True, max(low, min(high, -float(value)))


def resolve_entities(
    names: list[str], devices: list[dict[str, Any]], entities: list[dict[str, Any]],
) -> dict[str, dict[str, str]]:
    """Satellite name -> {setting: entity_id}, from the device and entity
    registries (config/device_registry/list, config/entity_registry/list)."""
    satellite_ids: dict[str, list[str]] = {}  # name -> Wyoming satellite ids
    for device in devices:
        if device.get("name") not in names:
            continue
        for identifier in device.get("identifiers") or []:
            if len(identifier) == 2 and identifier[0] == DOMAIN:
                satellite_ids.setdefault(device["name"], []).append(identifier[1])
    by_unique_id = {
        e.get("unique_id"): e["entity_id"] for e in entities
        if e.get("platform") == DOMAIN and e.get("entity_id")
    }
    found: dict[str, dict[str, str]] = {}
    for name, ids in satellite_ids.items():
        candidates = [
            {setting: by_unique_id[f"{sid}-{suffix}"]
             for suffix, setting in ENTITY_KEYS.items() if f"{sid}-{suffix}" in by_unique_id}
            for sid in ids
        ]
        candidates = [c for c in candidates if c]
        if len(candidates) > 1:
            _LOGGER.warning(
                "Several Wyoming devices are named '%s' in Home Assistant: using "
                "the first one's entities (%s)", name, ", ".join(candidates[0].values()),
            )
        if candidates:
            found[name] = candidates[0]
    return found


class HaAudioSync:
    """Follows the microphone entities of the satellites that want it.

    Values are kept when HA is unreachable or an entity is unknown /
    unavailable; on_change(name, settings) is called when they change.
    """

    RESOLVE_DELAY_S = 1.0  # registry events come in bursts
    RETRY_MIN_S = 5.0  # HA connected but not answering: tried again later
    RETRY_MAX_S = 60.0

    def __init__(
        self, watcher: HaStateWatcher, on_change: Callable[[str, HaMicSettings], None],
    ):
        self.watcher = watcher
        self.on_change = on_change
        self.names: list[str] = []  # satellites following their HA entities
        self.settings: dict[str, HaMicSettings] = {}
        self.entities: dict[str, dict[str, str]] = {}  # name -> setting -> entity_id
        self._owners: dict[str, tuple[str, str]] = {}  # entity_id -> (name, setting)
        self._state_sub: Optional[int] = None
        self._resolve_task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()
        self._missing_logged: dict[str, tuple[str, ...]] = {}
        self._retry_s = self.RETRY_MIN_S
        watcher.add_connect_listener(self._on_connect)

    def close(self) -> None:
        self.watcher.remove_connect_listener(self._on_connect)
        if self._resolve_task is not None:
            self._resolve_task.cancel()

    def set_satellites(self, names: list[str]) -> None:
        """Satellites following their HA entities (e.g. after a change)."""
        names = sorted(set(names))
        if names == self.names:
            return
        self.names = names
        self.settings = {n: s for n, s in self.settings.items() if n in names}
        if self.watcher.connected:
            self._schedule_resolve(0)

    def status(self, name: str) -> dict[str, Any]:
        settings = self.settings.get(name)
        return {
            "entities": self.entities.get(name, {}),
            "synced_at": settings.synced_at if settings else None,
        }

    async def write(self, name: str, values: dict[str, Any]) -> list[str]:
        """Write a satellite's entities (calibration): volume (0.1-10),
        noise_suppression (the app's level), auto_gain (HA's 0-31). Their
        new states come back through the subscription. Returns the
        entity_ids written."""
        found = self.entities.get(name, {})
        labels = {"volume": "Volume du micro", "noise_suppression":
                  "Niveau de suppression du bruit", "auto_gain": "Gain automatique"}
        missing = [labels[s] for s in values if s not in found]
        if missing:
            raise HaCommandError(
                f"entité(s) {', '.join(missing)} de {name} introuvable(s) dans Home Assistant"
            )
        written = []
        for setting, value in values.items():
            entity_id = found[setting]
            if setting == "noise_suppression":
                command = {"domain": "select", "service": "select_option",
                           "service_data": {"option": APP_NOISE_LEVELS[value]}}
            else:
                command = {"domain": "number", "service": "set_value",
                           "service_data": {"value": value}}
            await self.watcher.call(
                {"type": "call_service", "target": {"entity_id": entity_id}, **command}
            )
            written.append(entity_id)
        return written

    # Connection and registries

    async def _on_connect(self) -> None:
        self._state_sub = None  # subscriptions are lost with the connection
        try:
            for event_type in ("entity_registry_updated", "device_registry_updated"):
                await self.watcher.subscribe(
                    {"type": "subscribe_events", "event_type": event_type},
                    lambda _event: self._schedule_resolve(self.RESOLVE_DELAY_S),
                )
        except (HaUnavailable, HaCommandError) as err:
            _LOGGER.warning("Home Assistant microphone entities: %s", err)
            return
        await self._resolve()

    def _schedule_resolve(self, delay: float, retry: bool = False) -> None:
        # The running resolution schedules its own retry (it's still "running")
        if not retry and self._resolve_task is not None and not self._resolve_task.done():
            return

        async def later() -> None:
            await asyncio.sleep(delay)
            await self._resolve()

        self._resolve_task = asyncio.create_task(later())

    async def _resolve(self) -> None:
        """Find the entities, subscribe to their states and read them."""
        async with self._lock:
            try:
                await self._resolve_locked()
            except (HaUnavailable, HaCommandError) as err:
                # Values kept; tried again later (or on the next connection)
                _LOGGER.warning(
                    "Home Assistant microphone entities unreachable (%s): keeping "
                    "the last values, retrying in %.0fs", err, self._retry_s,
                )
                if self.watcher.connected:
                    self._schedule_resolve(self._retry_s, retry=True)
                self._retry_s = min(self._retry_s * 2, self.RETRY_MAX_S)
            else:
                self._retry_s = self.RETRY_MIN_S

    async def _resolve_locked(self) -> None:
        if self._state_sub is not None:
            await self.watcher.unsubscribe(self._state_sub)
            self._state_sub = None
        if not self.names:
            self.entities, self._owners = {}, {}
            return
        devices = await self.watcher.call({"type": "config/device_registry/list"})
        entities = await self.watcher.call({"type": "config/entity_registry/list"})
        self.entities = resolve_entities(self.names, devices or [], entities or [])
        self._owners = {
            entity_id: (name, setting)
            for name, found in self.entities.items() for setting, entity_id in found.items()
        }
        for name in self.names:
            missing = tuple(s for s in ENTITY_KEYS.values() if s not in self.entities.get(name, {}))
            if missing and self._missing_logged.get(name) != missing:
                _LOGGER.warning(
                    "[%s] Home Assistant entities not found (%s): the app's settings "
                    "are used instead. Is the satellite added in the Wyoming "
                    "integration under this name?", name, ", ".join(missing),
                )
            self._missing_logged[name] = missing
        if not self._owners:
            return
        # Subscribe first: no change is missed between reading and subscribing
        self._state_sub = await self.watcher.subscribe(
            {"type": "subscribe_trigger",
             "trigger": {"platform": "state", "entity_id": sorted(self._owners)}},
            self._on_trigger,
        )
        states = await self.watcher.call({"type": "get_states"})
        changed = set()
        for state in states or []:
            if (name := self._update(state.get("entity_id"), state.get("state"))) is not None:
                changed.add(name)
        for name in sorted(changed):
            self.on_change(name, self.settings[name])

    def _on_trigger(self, event: dict[str, Any]) -> None:
        trigger = (event.get("variables") or {}).get("trigger") or {}
        to_state = trigger.get("to_state") or {}
        entity_id = to_state.get("entity_id") or trigger.get("entity_id")
        if (name := self._update(entity_id, to_state.get("state"))) is not None:
            self.on_change(name, self.settings[name])

    def _update(self, entity_id: Optional[str], state: Any) -> Optional[str]:
        """Store a valid value; returns the satellite's name if stored."""
        if entity_id not in self._owners:
            return None
        name, setting = self._owners[entity_id]
        value = parse_state(setting, state)
        if value is None:
            _LOGGER.debug("%s is %s: last value kept", entity_id, state)
            return None
        settings = self.settings.setdefault(name, HaMicSettings())
        setattr(settings, setting, value)
        settings.synced_at = time.time()
        return name
