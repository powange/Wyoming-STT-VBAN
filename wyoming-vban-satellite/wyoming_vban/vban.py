"""VBAN protocol: packet parsing, building, unicast/multicast receive and send."""

import array
import asyncio
import audioop
import logging
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional

from .const import (
    DEFAULT_VBAN_PORT,
    VBAN_CODEC_PCM,
    VBAN_DATATYPE_FLOAT32,
    VBAN_DATATYPE_FLOAT64,
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

# Rate-limit non-PCM codec warnings to avoid log spam
_non_pcm_warned_codecs: set = set()


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
        # Warn only once per unique codec to avoid log spam (would fire
        # up to 62 times/second on a continuous non-PCM stream)
        if codec not in _non_pcm_warned_codecs:
            _non_pcm_warned_codecs.add(codec)
            _LOGGER.warning(
                "Non-PCM VBAN codec (0x%02x) not supported — ignoring packets",
                codec,
            )
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
    """Build a raw VBAN packet from audio payload. The sample rate must be
    one of VBAN's (ValueError otherwise: the payload would play at the
    wrong speed)."""
    sr_index = VBAN_SAMPLE_RATES.index(sample_rate)

    sr_sub = (sr_index & 0x1F) | VBAN_PROTOCOL_AUDIO
    data_format_codec = (data_format & 0x07) | VBAN_CODEC_PCM

    # Names are made ASCII when the options are read; never fail here
    name_bytes = stream_name.encode("ascii", "replace")[:16].ljust(16, b"\x00")
    counter_bytes = struct.pack("<I", frame_counter & 0xFFFFFFFF)

    header = (
        VBAN_MAGIC
        + bytes([sr_sub, samples_per_frame - 1, channels - 1, data_format_codec])
        + name_bytes
        + counter_bytes
    )

    return header + payload


def to_mono16(audio: bytes, data_format: int, channels: int) -> bytes:
    """VBAN samples -> mono 16-bit at the same rate.

    8-bit VBAN is unsigned; float formats are scaled from ±1.0; stereo is
    averaged (a sum would clip); beyond two channels, the first one is kept.
    """
    width = VBAN_DATATYPE_WIDTH.get(data_format, 2)
    channels = max(1, channels)
    frame = width * channels
    audio = audio[: len(audio) // frame * frame]
    if not audio:
        return b""
    if data_format in (VBAN_DATATYPE_FLOAT32, VBAN_DATATYPE_FLOAT64):
        floats = array.array("f" if data_format == VBAN_DATATYPE_FLOAT32 else "d", audio)
        if channels > 1:
            floats = floats[::channels]
        return array.array(
            "h", (max(-32768, min(32767, int(s * 32767))) for s in floats)
        ).tobytes()
    pcm = audio
    if data_format == VBAN_DATATYPE_UINT8:
        pcm = audioop.bias(pcm, 1, -128)
    if channels == 2:
        pcm = audioop.tomono(pcm, width, 0.5, 0.5)
    elif channels > 2:
        pcm = b"".join(pcm[i:i + width] for i in range(0, len(pcm), frame))
    if width != WYOMING_WIDTH:
        pcm = audioop.lin2lin(pcm, width, WYOMING_WIDTH)
    return pcm


class WyomingConverter:
    """VBAN packets -> Wyoming audio (16 kHz mono 16-bit), one stream.

    The resampler state is kept from packet to packet: resampling each
    packet on its own clicks at every packet and adds samples. It starts
    again when the stream's format changes.
    """

    def __init__(self) -> None:
        self._format: Optional[tuple[int, int, int]] = None
        self._state = None

    def convert(self, packet: VbanPacket) -> bytes:
        stream_format = (packet.sample_rate, packet.channels, packet.data_format)
        if stream_format != self._format:
            self._format = stream_format
            self._state = None
        pcm = to_mono16(packet.payload, packet.data_format, packet.channels)
        if packet.sample_rate != WYOMING_RATE and pcm:
            pcm, self._state = audioop.ratecv(
                pcm, WYOMING_WIDTH, 1, packet.sample_rate, WYOMING_RATE, self._state
            )
        return pcm


def resample_to_wyoming(audio: bytes, packet: VbanPacket) -> bytes:
    """Convert one packet's audio to Wyoming format (16kHz, 16-bit, mono),
    without state: for a stream, use a WyomingConverter."""
    pcm = to_mono16(audio, packet.data_format, packet.channels)
    if packet.sample_rate != WYOMING_RATE and pcm:
        pcm, _ = audioop.ratecv(pcm, WYOMING_WIDTH, 1, packet.sample_rate, WYOMING_RATE, None)
    return pcm


class VbanReceiver:
    """Async VBAN packet receiver with pub/sub model, one per UDP port.

    Designed to run persistently at application startup. All satellites
    receiving on the same port share this receiver: with several sockets
    bound to one port (SO_REUSEADDR/SO_REUSEPORT), Linux spreads unicast
    datagrams across them instead of duplicating them, so a satellite
    would get (and drop) another satellite's stream.

    Each subscriber gives the stream name it wants. A packet goes to the
    subscribers of its exact stream name; if there are none, it goes to
    the catch-all subscribers (empty stream name).
    """

    def __init__(
        self,
        port: int = DEFAULT_VBAN_PORT,
        mode: str = "unicast",
        multicast_group: str = "",
    ):
        self.port = port
        self.mode = mode
        self.multicast_group = multicast_group
        self._socket: Optional[socket.socket] = None
        self._running = False
        # stream name ("" = catch-all) -> callbacks
        self._subscribers: dict[str, list[Callable[[VbanPacket], None]]] = {}
        self._last_subscriber_error_log = 0.0
        self._unrouted_logged: set[str] = set()  # log each unrouted stream once

    def subscribe(
        self, callback: Callable[[VbanPacket], None], stream_name: str = ""
    ) -> None:
        """Register a callback for one stream name ("" = any unclaimed stream)."""
        callbacks = self._subscribers.setdefault(stream_name, [])
        if callback not in callbacks:
            callbacks.append(callback)
            _LOGGER.debug(
                "Port %d: subscriber added for stream '%s'",
                self.port, stream_name or "<any>",
            )

    def unsubscribe(self, callback: Callable[[VbanPacket], None]) -> None:
        """Remove a callback (whatever stream it subscribed to)."""
        for stream_name, callbacks in list(self._subscribers.items()):
            if callback in callbacks:
                callbacks.remove(callback)
                if not callbacks:
                    del self._subscribers[stream_name]
                _LOGGER.debug(
                    "Port %d: subscriber removed for stream '%s'",
                    self.port, stream_name or "<any>",
                )

    def _targets(self, stream_name: str) -> list[Callable[[VbanPacket], None]]:
        """Subscribers that should receive a packet of this stream."""
        callbacks = self._subscribers.get(stream_name) or self._subscribers.get("")
        if not callbacks:
            if self._subscribers and stream_name not in self._unrouted_logged:
                _LOGGER.info(
                    "Port %d: ignoring VBAN stream '%s' (no satellite for it)",
                    self.port, stream_name,
                )
                self._unrouted_logged.add(stream_name)
            return []
        return list(callbacks)

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

    RETRY_S = 5.0  # after a socket error, the socket is created again

    async def run(self) -> None:
        """Run the receiver loop. Should be started once at application startup."""
        self._socket = self._create_socket()
        self._running = True

        loop = asyncio.get_running_loop()
        _LOGGER.info("VBAN receiver started (port=%d mode=%s)", self.port, self.mode)

        try:
            while self._running:
                try:
                    data = await loop.sock_recv(self._socket, 2048)
                except OSError as err:
                    if not self._running:
                        break
                    # E.g. the network interface went down: try again
                    _LOGGER.error(
                        "VBAN receive error on port %d: %s (retrying in %.0fs)",
                        self.port, err, self.RETRY_S,
                    )
                    self._close_socket()
                    await asyncio.sleep(self.RETRY_S)
                    self._socket = self._create_socket()
                    continue

                packet = parse_packet(data)
                if packet is None:
                    continue

                for callback in self._targets(packet.stream_name):
                    try:
                        callback(packet)
                    except Exception as err:
                        # Rate-limit to avoid log spam if a callback fails on every packet
                        now = time.monotonic()
                        if now - self._last_subscriber_error_log >= 5.0:
                            _LOGGER.error("Subscriber callback error: %s", err)
                            self._last_subscriber_error_log = now
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
        """Stop the receiver. The socket is closed by run() as it ends (the
        task is cancelled), not under a pending read."""
        self._running = False


class VbanSender:
    """Sends audio as VBAN packets over UDP, paced at the audio rate.

    VBAN is a real-time protocol: packets must arrive at the rate the
    receiver is playing them. A background task drains a PCM buffer
    and emits packets in batches spaced at the audio rate.

    batch_packets (default 1) sets how many packets are sent per wake-up.
    One packet every 16ms keeps the stream smooth for receivers with a
    tiny UDP queue (ESP32/lwIP drops packets from bursts). Pre-buffering
    (PREBUFFER_MS of audio before first emit) absorbs arrival jitter from HA.
    """

    SAMPLES_PER_PACKET = 256
    DEFAULT_BATCH_PACKETS = 1  # 1 packet × 16ms at 16kHz
    PREBUFFER_MS = 50  # wait for this much audio before starting to drain
    # Hard cap on pending buffer size. Must be >= longest expected TTS response
    # since HA sends the full TTS as a burst over localhost TCP (fills the
    # buffer in milliseconds while drain emits at real-time). 60s = ~1.9 MB
    # at 16kHz mono 16-bit, covers long assistant responses.
    MAX_PENDING_MS = 60_000
    SEND_ERROR_LOG_INTERVAL_S = 5.0  # max frequency for send-error log spam
    OVERFLOW_RESET_RATIO = 4  # reset overflow flag when pending drops below max/N

    def __init__(
        self,
        address: str,
        port: int = DEFAULT_VBAN_PORT,
        mode: str = "unicast",
        stream_name: str = "TTS1",
        sample_rate: int = WYOMING_RATE,
        channels: int = WYOMING_CHANNELS,
        volume: float = 1.0,
        batch_packets: int = DEFAULT_BATCH_PACKETS,
    ):
        self.address = address
        self.port = port
        self.mode = mode
        self.stream_name = stream_name
        if sample_rate not in VBAN_SAMPLE_RATES:
            _LOGGER.error("VBAN sender: %d Hz isn't a VBAN rate, sending at %d Hz",
                          sample_rate, WYOMING_RATE)
            sample_rate = WYOMING_RATE
        self.sample_rate = sample_rate
        self.channels = channels
        self.volume = volume
        self.batch_packets = max(1, batch_packets)
        self._socket: Optional[socket.socket] = None
        self._frame_counter = 0
        self._ratecv_state = None
        self._pending: bytearray = bytearray()
        self._data_ready: Optional[asyncio.Event] = None
        self._drain_task: Optional[asyncio.Task] = None
        self._pending_overflow_logged = False
        self._last_send_error_log = 0.0
        self._max_pending_bytes = int(
            (self.MAX_PENDING_MS / 1000) * sample_rate * WYOMING_WIDTH * channels
        )

    def reset_resampler(self) -> None:
        """Reset the resampler state between TTS streams."""
        self._ratecv_state = None

    def clear_pending(self) -> None:
        """Discard buffered audio waiting to be sent.

        Called when a new TTS stream starts (AudioStart) to prevent
        leftover audio from a previous stream leaking into the new one.
        """
        self._pending.clear()

    def pending_seconds(self) -> float:
        """Duration of the audio still waiting to be sent."""
        return len(self._pending) / (self.sample_rate * WYOMING_WIDTH * self.channels)

    async def wait_sent(self, max_wait: float = 300.0) -> None:
        """Return once the buffered audio has been sent (or max_wait)."""
        packet_seconds = self.SAMPLES_PER_PACKET / self.sample_rate
        deadline = time.monotonic() + max_wait
        while self.pending_seconds() >= packet_seconds and time.monotonic() < deadline:
            await asyncio.sleep(0.05)

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
        self._drain_task.add_done_callback(self._drain_done)

    @staticmethod
    def _drain_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            _LOGGER.error("VBAN sender stopped: %s", task.exception(),
                          exc_info=task.exception())

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
            pcm = audioop.tomono(pcm, WYOMING_WIDTH, 0.5, 0.5)
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

        # Apply volume multiplier. audioop.mul clips at the 16-bit range
        # automatically, so factor > 1.0 gracefully saturates loud samples
        # rather than wrapping around.
        if self.volume != 1.0:
            pcm = audioop.mul(pcm, WYOMING_WIDTH, self.volume)

        # Cap pending buffer to prevent unbounded memory growth if the
        # drain task falls behind (e.g. network issue, huge TTS response).
        # Once full, drop everything until it has drained a good part: a
        # later small chunk slipping in would leave a hole in the speech.
        if self._pending_overflow_logged:
            if len(self._pending) >= self._max_pending_bytes // self.OVERFLOW_RESET_RATIO:
                return
            self._pending_overflow_logged = False
        if len(self._pending) + len(pcm) > self._max_pending_bytes:
            _LOGGER.warning(
                "TTS send buffer full (%d ms) — dropping audio",
                self.MAX_PENDING_MS,
            )
            self._pending_overflow_logged = True
            return

        self._pending.extend(pcm)

        # Notify the drain task that data is available
        if self._data_ready is not None:
            self._data_ready.set()

    def _send_packet(self, packet_bytes: int) -> bool:
        """Emit one VBAN packet from the pending buffer. Returns True on success."""
        if self._socket is None or len(self._pending) < packet_bytes:
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
            # Rate-limit send-error logs to avoid spam on persistent failures
            # (e.g. destination unreachable during an entire TTS response).
            now = time.monotonic()
            if now - self._last_send_error_log >= self.SEND_ERROR_LOG_INTERVAL_S:
                _LOGGER.error("VBAN send error: %s", err)
                self._last_send_error_log = now
            return False

        self._frame_counter = (self._frame_counter + 1) & 0xFFFFFFFF
        return True

    async def _drain_loop(self) -> None:
        """Emit VBAN packets in batches, paced at the audio rate.

        1. Wait (event-driven) until enough audio is buffered to start (pre-buffer)
        2. Emit batch_packets packets every batch_duration
        3. Continue until buffer empties, then go back to step 1

        Batches are never sent back-to-back to catch up after a late
        wake-up: the schedule restarts from the late batch instead.
        """
        bytes_per_sample = WYOMING_WIDTH * self.channels
        packet_bytes = self.SAMPLES_PER_PACKET * bytes_per_sample
        packet_duration = self.SAMPLES_PER_PACKET / self.sample_rate
        batch_duration = self.batch_packets * packet_duration
        prebuffer_bytes = int(
            (self.PREBUFFER_MS / 1000) * self.sample_rate * bytes_per_sample
        )

        loop = asyncio.get_running_loop()

        while self._socket is not None:
            # Phase 1: wait for enough audio (pre-buffer) or any data after timeout
            while len(self._pending) < prebuffer_bytes:
                # Clear first, then re-check buffer length before waiting.
                # This avoids a race where send() fires between the length
                # check and the clear (we'd wait 500ms for nothing).
                self._data_ready.clear()
                if len(self._pending) >= prebuffer_bytes:
                    break
                try:
                    await asyncio.wait_for(self._data_ready.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    # No new data for 500ms — if we have any data, flush it,
                    # the last partial packet padded with silence
                    if self._pending:
                        if len(self._pending) % packet_bytes:
                            self._pending.extend(
                                bytes(packet_bytes - len(self._pending) % packet_bytes)
                            )
                        break
                if self._socket is None:
                    return

            # Phase 2: drain at real-time rate until buffer runs low.
            # next_send is the time the next batch is due.
            next_send = loop.time()
            while len(self._pending) >= packet_bytes:
                delay = next_send - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                if self._socket is None:
                    return

                sent_at = loop.time()
                for _ in range(self.batch_packets):
                    if not self._send_packet(packet_bytes):
                        break

                # Small lateness (sleep overshoot) is absorbed by keeping the
                # schedule. If this batch went out more than half an interval
                # late, restart the schedule from now rather than catching up:
                # catching up would emit batches back-to-back, which a small
                # receiver queue drops. Batches are thus always >= half an
                # interval apart.
                if sent_at - next_send > batch_duration / 2:
                    next_send = sent_at
                next_send += batch_duration

    async def close(self) -> None:
        """Close the send socket and stop the drain task."""
        # Close socket first so the drain loop sees _socket is None and exits
        if self._socket:
            self._socket.close()
            self._socket = None

        # Wake up the drain task if it's waiting on _data_ready
        if self._data_ready is not None:
            self._data_ready.set()

        # Cancel and await the drain task
        if self._drain_task:
            self._drain_task.cancel()
            try:
                await self._drain_task
            except asyncio.CancelledError:
                pass
            self._drain_task = None

        _LOGGER.info("VBAN sender closed")
