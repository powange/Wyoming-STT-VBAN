"""Satellite configuration: loading from the app options and validation."""

import json
import logging
from dataclasses import dataclass
from typing import Any

from .const import DEFAULT_VBAN_PORT

_LOGGER = logging.getLogger(__name__)

DEFAULT_TTS_HTTP_PORT = 10800  # + satellite index


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
    thinking_sound_enabled: bool = True
    search_sound_enabled: bool = True
    thinking_delay_ms: int = 1000
    thinking_sound: str = ""  # WAV file replacing the built-in sound
    search_sound: str = ""

    @classmethod
    def from_options(cls, options: dict[str, Any], index: int) -> "SatelliteConfig":
        """Build from one `satellites` entry; missing/null values use defaults."""
        values = {
            key: value
            for key, value in options.items()
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


def load_options(path: str) -> tuple[list[SatelliteConfig], bool, bool]:
    """Read the app options file.

    Returns (satellites, debug_logging, publish_exchanges).
    """
    with open(path, encoding="utf-8") as options_file:
        options = json.load(options_file)

    satellites = [
        SatelliteConfig.from_options(entry, index)
        for index, entry in enumerate(options.get("satellites") or [])
    ]
    publish = options.get("publish_exchanges")
    return (
        satellites,
        bool(options.get("debug_logging")),
        True if publish is None else bool(publish),
    )


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
                f"Satellite '{sat.name}': vban_receive_mode is multicast but "
                "vban_receive_multicast_group is empty. Set the multicast group "
                "address (e.g. 239.0.0.1), or use unicast mode."
            )

        if sat.tts_vban_enabled and not sat.tts_vban_address:
            raise ConfigError(
                f"Satellite '{sat.name}': TTS VBAN enabled but no tts_vban_address"
            )

        if sat.tts_output == "media_player":
            if not sat.tts_media_player.startswith("media_player."):
                raise ConfigError(
                    f"Satellite '{sat.name}': tts_output is media_player but "
                    f"tts_media_player is not a media_player entity_id: "
                    f"'{sat.tts_media_player}'"
                )
            if sat.tts_http_port in http_ports:
                raise ConfigError(
                    f"Satellite '{sat.name}' uses TTS HTTP port {sat.tts_http_port} "
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
                f"{port} but use different vban_receive_mode / multicast group. "
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
                    "vban_receive_stream_name: they will all receive every "
                    "unclaimed stream on this port. Set vban_receive_stream_name "
                    "to route each stream to one satellite.",
                    ", ".join(owners), port,
                )
        if len(names.get("", [])) == 1:
            _LOGGER.info(
                "VBAN port %d: %s has no vban_receive_stream_name and receives "
                "every stream not claimed by another satellite on this port",
                port, names[""][0],
            )
