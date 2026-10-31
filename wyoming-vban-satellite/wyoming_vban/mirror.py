"""Debug copies of a satellite's audio streams, sent as VBAN.

Lets you listen (e.g. with VBAN Receptor or VoiceMeeter) to:
- mic_received: the microphone packets as received from the device;
- mic_sent: the audio actually sent to Home Assistant (16 kHz, with the
  silences of mutes, beep guards and waiting sounds);
- tts: the responses sent by Home Assistant.

These are copies: the satellite keeps working normally.
"""

import audioop
import ipaddress
import logging
import socket
import time
from typing import Optional

from .config import DebugMirror
from .const import WYOMING_RATE
from .vban import VbanPacket, VbanSender, build_packet

_LOGGER = logging.getLogger(__name__)

TTS_MIRROR_RATE = 22050  # Piper's rate: no resampling of the responses


def _mode(address: str) -> str:
    try:
        return "multicast" if ipaddress.ip_address(address).is_multicast else "unicast"
    except ValueError:
        return "unicast"  # host name


class RawMirror:
    """Re-sends received VBAN packets under another stream name."""

    ERROR_LOG_INTERVAL_S = 5.0

    def __init__(self, address: str, port: int, stream_name: str):
        self.address = address
        self.port = port
        self.stream_name = stream_name
        self._socket: Optional[socket.socket] = None
        self._counter = 0
        self._last_error_log = 0.0

    def open(self) -> None:
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        if _mode(self.address) == "multicast":
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 32)

    def on_packet(self, packet: VbanPacket) -> None:
        if self._socket is None:
            return
        data = build_packet(
            payload=packet.payload,
            stream_name=self.stream_name,
            sample_rate=packet.sample_rate,
            channels=packet.channels,
            samples_per_frame=packet.samples_per_frame,
            frame_counter=self._counter,
            data_format=packet.data_format,
        )
        self._counter = (self._counter + 1) & 0xFFFFFFFF
        try:
            self._socket.sendto(data, (self.address, self.port))
        except OSError as err:
            now = time.monotonic()
            if now - self._last_error_log >= self.ERROR_LOG_INTERVAL_S:
                _LOGGER.warning("Debug mirror %s send error: %s", self.stream_name, err)
                self._last_error_log = now

    async def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


class SatelliteMirrors:
    """The debug mirrors of one satellite."""

    def __init__(self, mirrors: list[DebugMirror]):
        self.mic_received: list[RawMirror] = []
        self._mic_sent: list[VbanSender] = []
        self._tts: list[VbanSender] = []
        self._build(mirrors)

    async def reconfigure(self, mirrors: list[DebugMirror]) -> None:
        """Replace the mirrors (the satellite keeps running)."""
        await self.close()
        self.mic_received, self._mic_sent, self._tts = [], [], []
        self._build(mirrors)
        self.open()

    def _build(self, mirrors: list[DebugMirror]) -> None:
        for mirror in mirrors:
            name = mirror.vban_stream_name()
            if mirror.stream == "mic_received":
                self.mic_received.append(RawMirror(mirror.address, mirror.port, name))
            else:
                rate = WYOMING_RATE if mirror.stream == "mic_sent" else TTS_MIRROR_RATE
                sender = VbanSender(
                    address=mirror.address, port=mirror.port,
                    mode=_mode(mirror.address), stream_name=name, sample_rate=rate,
                )
                (self._mic_sent if mirror.stream == "mic_sent" else self._tts).append(sender)
            _LOGGER.info(
                "Debug mirror: %s of '%s' → %s:%d (VBAN stream %s)",
                mirror.stream, mirror.satellite, mirror.address, mirror.port, name,
            )

    def open(self) -> None:
        for mirror in self.mic_received:
            mirror.open()
        for sender in self._mic_sent + self._tts:
            sender.open()

    async def close(self) -> None:
        for mirror in self.mic_received:
            await mirror.close()
        for sender in self._mic_sent + self._tts:
            await sender.close()

    def mic_sent(self, pcm: bytes) -> None:
        """Audio chunk sent to HA (16 kHz, 16-bit mono)."""
        for sender in self._mic_sent:
            sender.send(pcm)

    def tts_start(self) -> None:
        for sender in self._tts:
            sender.reset_resampler()
            sender.clear_pending()

    def tts(self, pcm: bytes, rate: int, width: int, channels: int) -> None:
        """Response audio chunk from HA."""
        for sender in self._tts:
            try:
                sender.send(pcm, sample_rate=rate, width=width, channels=channels)
            except (audioop.error, ValueError) as err:
                _LOGGER.debug("Debug mirror TTS chunk not sent: %s", err)
