"""Satellite configuration: loading from the app options and validation."""

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from .const import DEFAULT_VBAN_PORT

_LOGGER = logging.getLogger(__name__)

DEFAULT_TTS_HTTP_PORT = 10800  # + satellite index
DEFAULT_VISION_PHRASE = "Attends, j'observe."
DEFAULT_THINKING_TEXT = "Je réfléchis."
DEFAULT_SEARCH_TEXT = "Je cherche sur internet."
DEFAULT_TTS_ENGINE = "tts.piper"
DEFAULT_TTS_VOICE = "fr_FR-siwis-medium"

# Announcement modes (waiting_sounds.*_mode, camera_phrase.mode)
ANNOUNCEMENT_MODES = ("none", "sound", "phrase", "sound_then_phrase")
LISTEN_END_MODES = ("none", "sound")


# App options (3.0 format): satellite fields grouped in blocks, mapped to
# the flat SatelliteConfig fields. "output" stays at the satellite level.
OPTION_BLOCKS: dict[str, dict[str, str]] = {
    "microphone": {
        "vban_port": "vban_receive_port",
        "vban_mode": "vban_receive_mode",
        "multicast_group": "vban_receive_multicast_group",
        "stream_name": "vban_receive_stream_name",
    },
    "vban_output": {
        "enabled": "tts_vban_enabled",
        "mode": "tts_vban_mode",
        "address": "tts_vban_address",
        "port": "tts_vban_port",
        "stream_name": "tts_vban_stream_name",
        "volume": "tts_vban_volume",
        "batch_packets": "tts_vban_batch_packets",
    },
    "media_player": {
        "entity": "tts_media_player",
        "http_port": "tts_http_port",
        "http_host": "tts_http_host",
        "mic_mute_tail_ms": "mic_mute_tail_ms",
    },
    "followup": {
        "enabled": "followup_enabled",
        "beep": "followup_beep",
        "timeout_s": "followup_timeout_s",
    },
    "waiting_sounds": {
        "listen_end_mode": "listen_end_mode",
        "listen_end_file": "listen_end_sound",
        "thinking_mode": "thinking_mode",
        "thinking_text": "thinking_text",
        "search_mode": "search_mode",
        "search_text": "search_text",
        # Before 3.2 (still read: they set the mode when it isn't set)
        "thinking": "thinking_sound_enabled",
        "thinking_delay_ms": "thinking_delay_ms",
        "thinking_file": "thinking_sound",
        "search": "search_sound_enabled",
        "search_file": "search_sound",
    },
    "camera_phrase": {
        "mode": "vision_mode",
        "text": "vision_phrase",
        "file": "vision_sound",
        # Before 3.2 (still read: sets the mode when it isn't set)
        "enabled": "vision_phrase_enabled",
        "tts_engine": "vision_tts_engine",
        "tts_voice": "vision_tts_voice",
    },
}
OPTION_RENAMES = {"output": "tts_output"}


def flatten_satellite_options(options: dict[str, Any]) -> dict[str, Any]:
    """Satellite options in the block format → flat field names.

    Flat (pre-3.0) names are accepted too, e.g. on the command line.
    """
    flat: dict[str, Any] = {}
    for key, value in options.items():
        if key in OPTION_BLOCKS and isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if sub_key in OPTION_BLOCKS[key]:
                    flat[OPTION_BLOCKS[key][sub_key]] = sub_value
                else:
                    _LOGGER.warning("Unknown option %s.%s ignored", key, sub_key)
        else:
            flat[OPTION_RENAMES.get(key, key)] = value
    return flat


class ConfigError(Exception):
    """Invalid satellite configuration."""


@dataclass
class SatelliteConfig:
    """Settings for one satellite (one entry of the `satellites` option)."""

    name: str
    wyoming_port: int = 10700
    vban_receive_port: int = DEFAULT_VBAN_PORT
    vban_receive_mode: str = "unicast"
    vban_receive_multicast_group: str = ""
    vban_receive_stream_name: str = ""
    tts_vban_enabled: bool = False
    tts_vban_mode: str = "unicast"
    tts_vban_address: str = ""
    tts_vban_port: int = DEFAULT_VBAN_PORT
    tts_vban_stream_name: str = "TTS1"
    tts_vban_volume: float = 1.0
    tts_vban_batch_packets: int = 1
    tts_output: str = "vban"
    tts_media_player: str = ""
    tts_http_port: int = DEFAULT_TTS_HTTP_PORT
    tts_http_host: str = ""
    mic_mute_tail_ms: int = 250
    followup_enabled: bool = True
    followup_beep: bool = True
    followup_timeout_s: int = 8
    # Announcements while the response is prepared. Modes: ANNOUNCEMENT_MODES
    # ("" = not set: default, or from the pre-3.2 booleans below)
    listen_end_mode: str = "none"
    listen_end_sound: str = ""  # WAV replacing the built-in blip
    thinking_mode: str = ""
    thinking_text: str = DEFAULT_THINKING_TEXT
    search_mode: str = ""
    search_text: str = DEFAULT_SEARCH_TEXT
    vision_mode: str = ""
    vision_sound: str = ""  # no built-in sound
    # Pre-3.2 on/off switches (None = not set)
    thinking_sound_enabled: Optional[bool] = None
    search_sound_enabled: Optional[bool] = None
    thinking_delay_ms: int = 1000
    thinking_sound: str = ""  # WAV file replacing the built-in sound
    search_sound: str = ""
    vision_phrase_enabled: Optional[bool] = None
    vision_phrase: str = DEFAULT_VISION_PHRASE
    # Per-satellite TTS engine/voice for the phrases ("" = app options
    # announcements_tts_engine / announcements_tts_voice)
    vision_tts_engine: str = ""
    vision_tts_voice: str = ""

    @classmethod
    def from_options(cls, options: dict[str, Any], index: int) -> "SatelliteConfig":
        """Build from one `satellites` entry; missing/null values use defaults."""
        values = {
            key: value
            for key, value in flatten_satellite_options(options).items()
            if key in cls.__dataclass_fields__ and value is not None
        }
        values.setdefault("name", f"VBAN Satellite {index + 1}")
        values.setdefault("tts_http_port", DEFAULT_TTS_HTTP_PORT + index)
        return cls(**values)

    def normalize(self) -> None:
        """Clean up options and clamp numeric ones to their valid range."""
        # The multicast group is only used in multicast mode (and optional
        # in the schema): ignore a leftover value in unicast mode.
        self.vban_receive_multicast_group = (
            self.vban_receive_multicast_group.strip()
            if self.vban_receive_mode == "multicast" else ""
        )

        volume = max(0.0, min(2.0, self.tts_vban_volume))
        if volume != self.tts_vban_volume:
            _LOGGER.warning(
                "[%s] tts_vban_volume %.2f clamped to %.2f (valid range 0.0-2.0)",
                self.name, self.tts_vban_volume, volume,
            )
            self.tts_vban_volume = volume

        batch = max(1, min(8, self.tts_vban_batch_packets))
        if batch != self.tts_vban_batch_packets:
            _LOGGER.warning(
                "[%s] tts_vban_batch_packets %d clamped to %d (valid range 1-8)",
                self.name, self.tts_vban_batch_packets, batch,
            )
            self.tts_vban_batch_packets = batch

        tail = max(0, min(5000, self.mic_mute_tail_ms))
        if tail != self.mic_mute_tail_ms:
            _LOGGER.warning(
                "[%s] mic_mute_tail_ms %d clamped to %d (valid range 0-5000)",
                self.name, self.mic_mute_tail_ms, tail,
            )
            self.mic_mute_tail_ms = tail

        timeout = max(3, min(30, self.followup_timeout_s))
        if timeout != self.followup_timeout_s:
            _LOGGER.warning(
                "[%s] followup_timeout_s %d clamped to %d (valid range 3-30)",
                self.name, self.followup_timeout_s, timeout,
            )
            self.followup_timeout_s = timeout

        delay = max(300, min(5000, self.thinking_delay_ms))
        if delay != self.thinking_delay_ms:
            _LOGGER.warning(
                "[%s] thinking_delay_ms %d clamped to %d (valid range 300-5000)",
                self.name, self.thinking_delay_ms, delay,
            )
            self.thinking_delay_ms = delay
        self.thinking_sound = self.thinking_sound.strip()
        self.search_sound = self.search_sound.strip()
        self.vision_phrase = self.vision_phrase.strip() or DEFAULT_VISION_PHRASE
        self.thinking_text = self.thinking_text.strip() or DEFAULT_THINKING_TEXT
        self.search_text = self.search_text.strip() or DEFAULT_SEARCH_TEXT
        self.vision_tts_engine = self.vision_tts_engine.strip()
        self.vision_tts_voice = self.vision_tts_voice.strip()
        self.listen_end_sound = self.listen_end_sound.strip()
        self.vision_sound = self.vision_sound.strip()
        self._resolve_announcement_modes()

    def _resolve_announcement_modes(self) -> None:
        """Set each announcement mode: explicit, from the pre-3.2 switches
        (on = built-in sound, kept as in 3.1), or the default."""
        legacy = []

        def resolve(mode: str, switch: Optional[bool], on: str, default: str,
                    old_name: str, new_name: str) -> str:
            mode = mode.strip()
            if not mode and switch is not None:
                legacy.append(f"{old_name} → {new_name}: {on if switch else 'none'}")
                return on if switch else "none"
            if mode and mode not in ANNOUNCEMENT_MODES:
                _LOGGER.warning(
                    "[%s] %s '%s' unknown (%s): using %s",
                    self.name, new_name, mode, ", ".join(ANNOUNCEMENT_MODES), default,
                )
                return default
            return mode or default

        self.thinking_mode = resolve(
            self.thinking_mode, self.thinking_sound_enabled, "sound", "phrase",
            "waiting_sounds.thinking", "waiting_sounds.thinking_mode",
        )
        self.search_mode = resolve(
            self.search_mode, self.search_sound_enabled, "sound", "sound_then_phrase",
            "waiting_sounds.search", "waiting_sounds.search_mode",
        )
        self.vision_mode = resolve(
            self.vision_mode, self.vision_phrase_enabled, "phrase", "phrase",
            "camera_phrase.enabled", "camera_phrase.mode",
        )
        mode = self.listen_end_mode.strip() or "none"
        if mode not in LISTEN_END_MODES:
            _LOGGER.warning(
                "[%s] waiting_sounds.listen_end_mode '%s' unknown (none, sound): "
                "using none", self.name, mode,
            )
            mode = "none"
        self.listen_end_mode = mode
        if legacy:
            _LOGGER.warning(
                "[%s] Deprecated announcement options, converted: %s. Since 3.2 "
                "each signal has a mode (none, sound, phrase, sound_then_phrase): "
                "remove these options to get the new defaults (spoken phrases), "
                "or set the modes. See DOCS, Announcements.",
                self.name, "; ".join(legacy),
            )


MIRROR_STREAMS = ("mic_received", "mic_sent", "tts")


@dataclass
class DebugMirror:
    """Copy of one satellite audio stream sent as VBAN for debugging."""

    satellite: str
    stream: str  # one of MIRROR_STREAMS
    address: str
    port: int = DEFAULT_VBAN_PORT
    stream_name: str = ""  # VBAN stream name (default: <satellite>-<stream>)

    def vban_stream_name(self) -> str:
        """VBAN stream names are 16 ASCII chars max: last word of the
        satellite name + short stream suffix, e.g. "Bureau-tx"."""
        if self.stream_name:
            return self.stream_name[:16]
        suffix = {"mic_received": "rx", "mic_sent": "tx", "tts": "tts"}[self.stream]
        word = (self.satellite.split() or ["sat"])[-1]
        word = "".join(c for c in word if c.isascii() and c.isalnum()) or "sat"
        return f"{word[:16 - len(suffix) - 1]}-{suffix}"


@dataclass
class AppOptions:
    satellites: list[SatelliteConfig]
    debug_logging: bool = False
    publish_exchanges: bool = True
    debug_mirrors: list[DebugMirror] = field(default_factory=list)
    announcements_tts_engine: str = DEFAULT_TTS_ENGINE
    announcements_tts_voice: str = DEFAULT_TTS_VOICE


def load_options(path: str) -> AppOptions:
    """Read the app options file."""
    with open(path, encoding="utf-8") as options_file:
        options = json.load(options_file)

    satellites = [
        SatelliteConfig.from_options(entry, index)
        for index, entry in enumerate(options.get("satellites") or [])
    ]
    publish = options.get("publish_exchanges")
    mirrors = [
        DebugMirror(**{
            key: value for key, value in entry.items()
            if key in DebugMirror.__dataclass_fields__ and value is not None
        })
        for entry in options.get("debug_mirror") or []
    ]
    return AppOptions(
        satellites=satellites,
        debug_logging=bool(options.get("debug_logging")),
        publish_exchanges=True if publish is None else bool(publish),
        debug_mirrors=mirrors,
        announcements_tts_engine=(
            (options.get("announcements_tts_engine") or "").strip() or DEFAULT_TTS_ENGINE
        ),
        announcements_tts_voice=(
            (options.get("announcements_tts_voice") or "").strip() or DEFAULT_TTS_VOICE
        ),
    )


def validate_mirrors(
    mirrors: list[DebugMirror], satellites: list[SatelliteConfig]
) -> list[DebugMirror]:
    """Keep the valid debug mirrors (a wrong one only logs a warning)."""
    names = {sat.name for sat in satellites}
    valid = []
    for mirror in mirrors:
        if mirror.satellite not in names:
            _LOGGER.warning(
                "debug_mirror: unknown satellite '%s' (satellites: %s), ignored",
                mirror.satellite, ", ".join(sorted(names)),
            )
        elif mirror.stream not in MIRROR_STREAMS:
            _LOGGER.warning(
                "debug_mirror: unknown stream '%s' (%s), ignored",
                mirror.stream, ", ".join(MIRROR_STREAMS),
            )
        elif not mirror.address.strip():
            _LOGGER.warning("debug_mirror: no address for %s, ignored", mirror.satellite)
        else:
            mirror.address = mirror.address.strip()
            valid.append(mirror)
    return valid


def validate(satellites: list[SatelliteConfig]) -> None:
    """Check per-satellite settings and conflicts between satellites.

    Raises ConfigError on a fatal problem; logs warnings for ambiguous
    stream routing on a shared VBAN port.
    """
    if not satellites:
        raise ConfigError("No satellite configured")

    wyoming_ports: dict[int, str] = {}
    http_ports: dict[int, str] = {}
    # VBAN receive port -> satellites listening on it
    by_vban_port: dict[int, list[SatelliteConfig]] = {}

    for sat in satellites:
        sat.normalize()

        if sat.wyoming_port in wyoming_ports:
            raise ConfigError(
                f"Satellite '{sat.name}' uses Wyoming port {sat.wyoming_port} which "
                f"is already taken by '{wyoming_ports[sat.wyoming_port]}'. Each "
                "satellite must have a unique wyoming_port."
            )
        wyoming_ports[sat.wyoming_port] = sat.name

        if sat.vban_receive_mode == "multicast" and not sat.vban_receive_multicast_group:
            raise ConfigError(
                f"Satellite '{sat.name}': microphone.vban_mode is multicast but "
                "microphone.multicast_group is empty. Set the multicast group "
                "address (e.g. 239.0.0.1), or use unicast mode."
            )

        if sat.tts_vban_enabled and not sat.tts_vban_address:
            raise ConfigError(
                f"Satellite '{sat.name}': vban_output is enabled but vban_output.address "
                "is empty"
            )

        if sat.tts_output == "media_player":
            if not sat.tts_media_player.startswith("media_player."):
                raise ConfigError(
                    f"Satellite '{sat.name}': output is media_player but "
                    f"media_player.entity is not a media_player entity_id: "
                    f"'{sat.tts_media_player}'"
                )
            if sat.tts_http_port in http_ports:
                raise ConfigError(
                    f"Satellite '{sat.name}' uses media_player.http_port {sat.tts_http_port} "
                    f"which is already taken by '{http_ports[sat.tts_http_port]}'"
                )
            http_ports[sat.tts_http_port] = sat.name

        by_vban_port.setdefault(sat.vban_receive_port, []).append(sat)

    for port, sats in by_vban_port.items():
        if len(sats) < 2:
            continue

        # One socket per port: all its satellites must agree on how to open it
        modes = {(s.vban_receive_mode, s.vban_receive_multicast_group) for s in sats}
        if len(modes) > 1:
            raise ConfigError(
                f"Satellites {', '.join(s.name for s in sats)} share VBAN port "
                f"{port} but use different microphone.vban_mode / multicast_group. "
                "Satellites on the same port must use the same mode and group."
            )

        names: dict[str, list[str]] = {}
        for sat in sats:
            names.setdefault(sat.vban_receive_stream_name, []).append(sat.name)
        for stream, owners in names.items():
            if len(owners) < 2:
                continue
            if stream:
                _LOGGER.warning(
                    "Satellites %s share VBAN port %d with the same stream name "
                    "'%s': they will all receive the same audio",
                    ", ".join(owners), port, stream,
                )
            else:
                _LOGGER.warning(
                    "Satellites %s share VBAN port %d without "
                    "microphone.stream_name: they will all receive every "
                    "unclaimed stream on this port. Set microphone.stream_name "
                    "to route each stream to one satellite.",
                    ", ".join(owners), port,
                )
        if len(names.get("", [])) == 1:
            _LOGGER.info(
                "VBAN port %d: %s has no microphone.stream_name and receives "
                "every stream not claimed by another satellite on this port",
                port, names[""][0],
            )
