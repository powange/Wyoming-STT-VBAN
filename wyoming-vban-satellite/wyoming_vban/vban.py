"""VBAN protocol: packet parsing, building, unicast/multicast receive and send."""

import asyncio
import audioop
import logging
import socket
import struct
from dataclasses import dataclass
from typing import Callable, Optional

from .const import (
    DEFAULT_VBAN_PORT,
    VBAN_CODEC_PCM,
    VBAN_DATATYPE_INT16,
    VBAN_DATATYPE_WIDTH,
    VBAN_HEADER_SIZE,
    VBAN_MAGIC,
    VBAN_PROTOCOL_AUDIO,
    VBAN_SAMPLE_RATES,
    WYOMING_CHANNELS,
    WYOMING_RATE,
    WYOMING_WIDTH,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class VbanPacket:
    """Parsed VBAN packet."""

    sample_rate: int
    samples_per_frame: int
    channels: int
    data_format: int
    codec: int
    stream_name: str
    frame_counter: int
    payload: bytes

    @property
    def sample_width(self) -> int:
        return VBAN_DATATYPE_WIDTH.get(self.data_format, 2)


def parse_packet(data: bytes) -> Optional[VbanPacket]:
    """Parse raw UDP data into a VbanPacket. Returns None if invalid."""
    if len(data) < VBAN_HEADER_SIZE:
        return None

    if data[:4] != VBAN_MAGIC:
        return None

    sr_sub = data[4]
    sample_rate_index = sr_sub & 0x1F
    sub_protocol = sr_sub & 0xE0

    if sub_protocol != VBAN_PROTOCOL_AUDIO:
        return None

    if sample_rate_index >= len(VBAN_SAMPLE_RATES):
        return None

    samples_per_frame = data[5] + 1
    channels = data[6] + 1
    data_format_codec = data[7]
    data_format = data_format_codec & 0x07
    codec = data_format_codec & 0xF0

    if codec != VBAN_CODEC_PCM:
        _LOGGER.warning("Non-PCM VBAN codec (0x%02x) not supported", codec)
        return None

    stream_name_raw = data[8:24]
    stream_name = stream_name_raw.split(b"\x00", 1)[0].decode("ascii", errors="replace")

    frame_counter = struct.unpack_from("<I", data, 24)[0]
    payload = data[VBAN_HEADER_SIZE:]

    return VbanPacket(
        sample_rate=VBAN_SAMPLE_RATES[sample_rate_index],
        samples_per_frame=samples_per_frame,
        channels=channels,
        data_format=data_format,
        codec=codec,
        stream_name=stream_name,
        frame_counter=frame_counter,
        payload=payload,
    )


def build_packet(
    payload: bytes,
    stream_name: str,
    sample_rate: int,
    channels: int,
    samples_per_frame: int,
    frame_counter: int,
    data_format: int = VBAN_DATATYPE_INT16,
) -> bytes:
    """Build a raw VBAN packet from audio payload."""
    try:
        sr_index = VBAN_SAMPLE_RATES.index(sample_rate)
    except ValueError:
        sr_index = VBAN_SAMPLE_RATES.index(16000)

    sr_sub = (sr_index & 0x1F) | VBAN_PROTOCOL_AUDIO
    data_format_codec = (data_format & 0x07) | VBAN_CODEC_PCM

    name_bytes = stream_name.encode("ascii")[:16].ljust(16, b"\x00")
    counter_bytes = struct.pack("<I", frame_counter & 0xFFFFFFFF)

    header = (
        VBAN_MAGIC
        + bytes([sr_sub, samples_per_frame - 1, channels - 1, data_format_codec])
        + name_bytes
        + counter_bytes
    )

    return header + payload


def resample_to_wyoming(
    audio: bytes, packet: VbanPacket
) -> bytes:
    """Convert VBAN audio to Wyoming format (16kHz, 16-bit, mono)."""
    pcm = audio
    width = packet.sample_width
    channels = packet.channels
    rate = packet.sample_rate

    # Convert to mono if needed
    if channels > 1:
        pcm = audioop.tomono(pcm, width, 1.0, 1.0)

    # Convert sample width to 16-bit if needed
    if width != WYOMING_WIDTH:
        pcm = audioop.lin2lin(pcm, width, WYOMING_WIDTH)

    # Resample to 16kHz if needed
    if rate != WYOMING_RATE:
        pcm, _ = audioop.ratecv(
            pcm, WYOMING_WIDTH, 1, rate, WYOMING_RATE, None
        )

    return pcm


def resample_from_wyoming(
    audio: bytes,
    target_rate: int,
    target_channels: int,
    target_width: int = WYOMING_WIDTH,
) -> bytes:
    """Convert Wyoming audio (16kHz, 16-bit, mono) to VBAN output format."""
    pcm = audio

    # Resample from 16kHz
    if target_rate != WYOMING_RATE:
        pcm, _ = audioop.ratecv(
            pcm, WYOMING_WIDTH, 1, WYOMING_RATE, target_rate, None
        )

    # Convert sample width
    if target_width != WYOMING_WIDTH:
        pcm = audioop.lin2lin(pcm, WYOMING_WIDTH, target_width)

    # Convert to multi-channel if needed
    if target_channels > 1:
        pcm = audioop.tostereo(pcm, target_width, 1.0, 1.0)

    return pcm


class VbanReceiver:
    """Async VBAN packet receiver supporting unicast, broadcast, and multicast."""

    def __init__(
        self,
        port: int = DEFAULT_VBAN_PORT,
        mode: str = "unicast",
        multicast_group: str = "",
        stream_name_filter: str = "",
    ):
        self.port = port
        self.mode = mode
        self.multicast_group = multicast_group
        self.stream_name_filter = stream_name_filter
        self._socket: Optional[socket.socket] = None
        self._running = False
        self._on_audio: Optional[Callable[[VbanPacket], None]] = None

    def _create_socket(self) -> socket.socket:
        """Create and configure the UDP socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        if hasattr(socket, "SO_REUSEPORT"):
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass

        sock.bind(("", self.port))

        if self.mode == "multicast" and self.multicast_group:
            group = socket.inet_aton(self.multicast_group)
            mreq = group + socket.inet_aton("0.0.0.0")
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            _LOGGER.info(
                "Joined multicast group %s on port %d",
                self.multicast_group,
                self.port,
            )
        else:
            _LOGGER.info(
                "Listening for unicast/broadcast VBAN on port %d", self.port
            )

        sock.setblocking(False)
        return sock

    async def start(
        self, on_audio: Callable[[VbanPacket], None]
    ) -> None:
        """Start receiving VBAN packets."""
        self._on_audio = on_audio
        self._socket = self._create_socket()
        self._running = True

        loop = asyncio.get_running_loop()
        _LOGGER.info("VBAN receiver started (mode=%s)", self.mode)

        while self._running:
            try:
                data = await loop.sock_recv(self._socket, 2048)
            except OSError as err:
                if self._running:
                    _LOGGER.error("VBAN receive error: %s", err)
                break

            packet = parse_packet(data)
            if packet is None:
                continue

            # Filter by stream name if configured
            if self.stream_name_filter and packet.stream_name != self.stream_name_filter:
                continue

            if self._on_audio:
                self._on_audio(packet)

    def stop(self) -> None:
        """Stop receiving VBAN packets."""
        self._running = False
        if self._socket:
            # Leave multicast group before closing
            if self.mode == "multicast" and self.multicast_group:
                try:
                    group = socket.inet_aton(self.multicast_group)
                    mreq = group + socket.inet_aton("0.0.0.0")
                    self._socket.setsockopt(
                        socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq
                    )
                except OSError:
                    pass
            self._socket.close()
            self._socket = None
        _LOGGER.info("VBAN receiver stopped")


class VbanSender:
    """Sends audio as VBAN packets over UDP (unicast or multicast)."""

    def __init__(
        self,
        address: str,
        port: int = DEFAULT_VBAN_PORT,
        mode: str = "unicast",
        stream_name: str = "TTS1",
        sample_rate: int = WYOMING_RATE,
        channels: int = WYOMING_CHANNELS,
    ):
        self.address = address
        self.port = port
        self.mode = mode
        self.stream_name = stream_name
        self.sample_rate = sample_rate
        self.channels = channels
        self._socket: Optional[socket.socket] = None
        self._frame_counter = 0

    def open(self) -> None:
        """Create the send socket."""
        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

        if self.mode == "multicast":
            ttl = struct.pack("b", 32)
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
            _LOGGER.info(
                "VBAN sender: multicast to %s:%d stream=%s",
                self.address, self.port, self.stream_name,
            )
        else:
            _LOGGER.info(
                "VBAN sender: unicast to %s:%d stream=%s",
                self.address, self.port, self.stream_name,
            )

    def send(self, pcm_audio: bytes) -> None:
        """Send PCM audio as VBAN packets."""
        if not self._socket:
            return

        # Split into chunks that fit VBAN packets (max 256 samples per frame)
        bytes_per_sample = WYOMING_WIDTH * self.channels
        max_samples = 256
        max_payload = max_samples * bytes_per_sample

        offset = 0
        while offset < len(pcm_audio):
            chunk = pcm_audio[offset : offset + max_payload]
            samples_in_chunk = len(chunk) // bytes_per_sample

            if samples_in_chunk == 0:
                break

            packet = build_packet(
                payload=chunk,
                stream_name=self.stream_name,
                sample_rate=self.sample_rate,
                channels=self.channels,
                samples_per_frame=samples_in_chunk,
                frame_counter=self._frame_counter,
            )

            try:
                self._socket.sendto(packet, (self.address, self.port))
            except OSError as err:
                _LOGGER.error("VBAN send error: %s", err)
                return

            self._frame_counter = (self._frame_counter + 1) & 0xFFFFFFFF
            offset += len(chunk)

    def close(self) -> None:
        """Close the send socket."""
        if self._socket:
            self._socket.close()
            self._socket = None
        _LOGGER.info("VBAN sender closed")
