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
    VBAN_DATATYPE_INT32,
    VBAN_DATATYPE_UINT8,
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
    """Async VBAN packet receiver with pub/sub model.

    Designed to run persistently at application startup. Multiple
    subscribers (handlers) can subscribe/unsubscribe to audio events
    without affecting the underlying UDP socket.
    """

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
        self._subscribers: list[Callable[[VbanPacket], None]] = []

    def subscribe(self, callback: Callable[[VbanPacket], None]) -> None:
        """Register a callback to receive VBAN packets."""
        if callback not in self._subscribers:
            self._subscribers.append(callback)
            _LOGGER.debug("Subscriber added (total: %d)", len(self._subscribers))

    def unsubscribe(self, callback: Callable[[VbanPacket], None]) -> None:
        """Remove a callback."""
        if callback in self._subscribers:
            self._subscribers.remove(callback)
            _LOGGER.debug("Subscriber removed (total: %d)", len(self._subscribers))

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

    async def run(self) -> None:
        """Run the receiver loop. Should be started once at application startup."""
        self._socket = self._create_socket()
        self._running = True

        loop = asyncio.get_running_loop()
        _LOGGER.info("VBAN receiver started (mode=%s)", self.mode)

        try:
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

                # Broadcast to all subscribers
                for callback in list(self._subscribers):
                    try:
                        callback(packet)
                    except Exception as err:
                        _LOGGER.error("Subscriber callback error: %s", err)
        finally:
            self._close_socket()
            _LOGGER.info("VBAN receiver stopped")

    def _close_socket(self) -> None:
        """Close the socket and leave multicast group if applicable."""
        if self._socket:
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

    def stop(self) -> None:
        """Stop the receiver (shuts down the socket)."""
        self._running = False
        self._close_socket()


class VbanSender:
    """Sends audio as VBAN packets over UDP, paced at the audio rate.

    VBAN is a real-time protocol: packets must arrive at the rate the
    receiver is playing them. A background task drains a PCM buffer
    and emits packets in batches spaced at the audio rate.

    Batching (BATCH_PACKETS packets per wake-up) reduces timer jitter
    impact. Pre-buffering (PREBUFFER_MS of audio before first emit)
    absorbs arrival jitter from HA.
    """

    SAMPLES_PER_PACKET = 256
    BATCH_PACKETS = 4  # 4 packets × 16ms = 64ms batch at 16kHz
    PREBUFFER_MS = 100  # wait for this much audio before starting to drain

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
        self._ratecv_state = None
        self._pending: bytearray = bytearray()
        self._data_ready: Optional[asyncio.Event] = None
        self._drain_task: Optional[asyncio.Task] = None

    def reset_resampler(self) -> None:
        """Reset the resampler state between TTS streams."""
        self._ratecv_state = None

    def open(self) -> None:
        """Create the send socket and start the drain task."""
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

        self._data_ready = asyncio.Event()
        self._drain_task = asyncio.create_task(self._drain_loop())

    def send(
        self,
        pcm_audio: bytes,
        sample_rate: Optional[int] = None,
        width: Optional[int] = None,
        channels: Optional[int] = None,
    ) -> None:
        """Queue PCM audio for paced transmission.

        Converts/resamples to the sender's target format, then appends
        to an internal buffer. The drain task emits packets at the
        proper playback rate.
        """
        if not self._socket:
            return

        src_rate = sample_rate if sample_rate is not None else self.sample_rate
        src_channels = channels if channels is not None else self.channels
        src_width = width if width is not None else WYOMING_WIDTH

        pcm = pcm_audio

        if src_width != WYOMING_WIDTH:
            pcm = audioop.lin2lin(pcm, src_width, WYOMING_WIDTH)

        if src_channels > 1 and self.channels == 1:
            pcm = audioop.tomono(pcm, WYOMING_WIDTH, 1.0, 1.0)
            src_channels = 1

        if src_rate != self.sample_rate:
            pcm, self._ratecv_state = audioop.ratecv(
                pcm,
                WYOMING_WIDTH,
                src_channels,
                src_rate,
                self.sample_rate,
                self._ratecv_state,
            )

        if src_channels == 1 and self.channels > 1:
            pcm = audioop.tostereo(pcm, WYOMING_WIDTH, 1.0, 1.0)

        self._pending.extend(pcm)

        # Notify the drain task that data is available
        if self._data_ready is not None:
            self._data_ready.set()

    def _send_packet(self, packet_bytes: int) -> bool:
        """Emit one VBAN packet from the pending buffer. Returns True on success."""
        if len(self._pending) < packet_bytes:
            return False

        chunk = bytes(self._pending[:packet_bytes])
        del self._pending[:packet_bytes]

        packet = build_packet(
            payload=chunk,
            stream_name=self.stream_name,
            sample_rate=self.sample_rate,
            channels=self.channels,
            samples_per_frame=self.SAMPLES_PER_PACKET,
            frame_counter=self._frame_counter,
            data_format=VBAN_DATATYPE_INT16,
        )

        try:
            self._socket.sendto(packet, (self.address, self.port))
        except OSError as err:
            _LOGGER.error("VBAN send error: %s", err)
            return False

        self._frame_counter = (self._frame_counter + 1) & 0xFFFFFFFF
        return True

    async def _drain_loop(self) -> None:
        """Emit VBAN packets in batches, paced at the audio rate.

        1. Wait (event-driven) until enough audio is buffered to start (pre-buffer)
        2. Emit BATCH_PACKETS packets, then sleep batch_duration
        3. Continue until buffer empties, then go back to step 1
        """
        bytes_per_sample = WYOMING_WIDTH * self.channels
        packet_bytes = self.SAMPLES_PER_PACKET * bytes_per_sample
        packet_duration = self.SAMPLES_PER_PACKET / self.sample_rate
        batch_duration = self.BATCH_PACKETS * packet_duration
        prebuffer_bytes = int(
            (self.PREBUFFER_MS / 1000) * self.sample_rate * bytes_per_sample
        )

        loop = asyncio.get_running_loop()

        while self._socket is not None:
            # Phase 1: wait for enough audio (pre-buffer) or any data after timeout
            while len(self._pending) < prebuffer_bytes:
                self._data_ready.clear()
                try:
                    await asyncio.wait_for(self._data_ready.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    # No new data for 500ms — if we have any data, flush it
                    if len(self._pending) >= packet_bytes:
                        break
                if self._socket is None:
                    return

            # Phase 2: drain at real-time rate until buffer runs low
            next_send = loop.time() + batch_duration
            while len(self._pending) >= packet_bytes:
                # Emit a batch of packets
                for _ in range(self.BATCH_PACKETS):
                    if not self._send_packet(packet_bytes):
                        break

                # Pace: sleep until next batch time
                now = loop.time()
                delay = next_send - now
                if delay > 0:
                    await asyncio.sleep(delay)
                    next_send += batch_duration
                else:
                    # Running behind — resync without oversleeping
                    next_send = now + batch_duration

    def close(self) -> None:
        """Close the send socket and stop the drain task."""
        if self._drain_task:
            self._drain_task.cancel()
            self._drain_task = None
        if self._socket:
            self._socket.close()
            self._socket = None
        _LOGGER.info("VBAN sender closed")
