"""Microphone from the audio of a camera stream (RTSP), read with ffmpeg.

Same interface as VbanReceiver (subscribe / unsubscribe / run / stop): the
audio is cut into VbanPacket objects (16 kHz mono 16-bit, 256 samples), so
everything downstream (Wyoming, mic mute, levels, debug copies) is the same
as for a VBAN microphone.
"""

import asyncio
import audioop
import collections
import logging
import math
import re
import time
from typing import Callable, Optional

from .const import VBAN_DATATYPE_INT16, WYOMING_RATE, WYOMING_WIDTH
from .vban import VbanPacket

_LOGGER = logging.getLogger(__name__)

SAMPLES_PER_PACKET = 256
PACKET_BYTES = SAMPLES_PER_PACKET * WYOMING_WIDTH


def mask_url(text: str) -> str:
    """Hide credentials in URLs (also inside a message), e.g.
    rtsp://user:pass@host → rtsp://***@host."""
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/@\s]*@", r"\1***@", text)


class RtspSource:
    """Reads a camera stream's audio with an ffmpeg subprocess.

    ffmpeg is restarted when it stops or sends no audio for STALL_S, after
    1, 2, 5, 10 then 30 s (back to 1 s once audio comes back).
    """

    FFMPEG = "ffmpeg"  # executable (tests use a fake one)
    STALL_S = 5.0
    BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)
    STOP_TIMEOUT_S = 3.0

    def __init__(
        self,
        url: str,
        transport: str = "tcp",
        name: str = "rtsp",
        ffmpeg: Optional[str] = None,
    ):
        self.url = url
        self.transport = transport
        self.name = name  # stream name of the packets (shown in logs)
        self.ffmpeg = ffmpeg or self.FFMPEG
        self._subscribers: list[Callable[[VbanPacket], None]] = []
        self._process: Optional[asyncio.subprocess.Process] = None
        self._running = False
        self._counter = 0
        self._buffer = bytearray()
        self.last_audio_at = 0.0  # time.monotonic()

    # Same interface as VbanReceiver

    def subscribe(
        self, callback: Callable[[VbanPacket], None], stream_name: str = ""
    ) -> None:
        if callback not in self._subscribers:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[VbanPacket], None]) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def command(self) -> list[str]:
        return [
            self.ffmpeg, "-nostdin", "-loglevel", "error",
            "-rtsp_transport", self.transport,
            "-fflags", "nobuffer", "-flags", "low_delay",
            "-i", self.url,
            "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(WYOMING_RATE),
            "-f", "s16le", "pipe:1",
        ]

    async def run(self) -> None:
        """Read the stream until stop() (or cancellation), reconnecting."""
        self._running = True
        url = mask_url(self.url)
        attempt = 0
        first_audio = True
        outage = False  # audio was lost (log when it comes back)
        lost_since = time.monotonic()  # last audio, or start
        _LOGGER.info("RTSP microphone: %s (%s)", url, self.transport)
        try:
            while self._running:
                got_audio = False
                errors: collections.deque[str] = collections.deque(maxlen=5)
                try:
                    self._process = await asyncio.create_subprocess_exec(
                        *self.command(),
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError as err:
                    _LOGGER.error("Cannot start ffmpeg (%s): %s", self.ffmpeg, err)
                    self._process = None
                else:
                    stderr_task = asyncio.create_task(self._read_stderr(errors))
                    try:
                        while self._running:
                            try:
                                data = await asyncio.wait_for(
                                    self._process.stdout.read(4096), self.STALL_S
                                )
                            except asyncio.TimeoutError:
                                break  # no audio for STALL_S
                            if not data:
                                break  # ffmpeg stopped
                            now = time.monotonic()
                            if first_audio:
                                first_audio = False
                                _LOGGER.info(
                                    "First RTSP audio from %s: %d Hz, 1ch, %.1f dB",
                                    url, WYOMING_RATE, _level_db(data),
                                )
                            elif outage:
                                _LOGGER.info(
                                    "RTSP audio resumed after %.0f seconds", now - lost_since,
                                )
                            outage = False
                            got_audio = True
                            attempt = 0
                            self.last_audio_at = now
                            self._deliver(data)
                    finally:
                        await self._stop_process()
                        stderr_task.cancel()
                        await asyncio.gather(stderr_task, return_exceptions=True)

                if not self._running:
                    break
                now = time.monotonic()
                if got_audio:
                    lost_since = self.last_audio_at
                outage = True
                delay = self.BACKOFF_S[min(attempt, len(self.BACKOFF_S) - 1)]
                attempt += 1
                _LOGGER.warning(
                    "No RTSP audio received for %.0f seconds%s, reconnecting in %.0f s",
                    now - lost_since,
                    f" (ffmpeg: {errors[-1]})" if errors else "",
                    delay,
                )
                await asyncio.sleep(delay)
        finally:
            self._running = False
            await self._stop_process()

    def stop(self) -> None:
        """Stop reading (run() returns; ffmpeg is terminated)."""
        self._running = False
        if self._process is not None and self._process.returncode is None:
            self._process.terminate()

    # Internals

    def _deliver(self, data: bytes) -> None:
        """Cut the PCM into 256-sample packets for the subscribers."""
        self._buffer.extend(data)
        while len(self._buffer) >= PACKET_BYTES:
            payload = bytes(self._buffer[:PACKET_BYTES])
            del self._buffer[:PACKET_BYTES]
            packet = VbanPacket(
                sample_rate=WYOMING_RATE, samples_per_frame=SAMPLES_PER_PACKET,
                channels=1, data_format=VBAN_DATATYPE_INT16, codec=0,
                stream_name=self.name, frame_counter=self._counter, payload=payload,
            )
            self._counter = (self._counter + 1) & 0xFFFFFFFF
            for callback in list(self._subscribers):
                try:
                    callback(packet)
                except Exception as err:  # one subscriber must not stop the stream
                    _LOGGER.error("Subscriber callback error: %s", err)

    async def _read_stderr(self, errors: collections.deque[str]) -> None:
        """Keep ffmpeg's last error lines (and never let its pipe fill up)."""
        assert self._process is not None and self._process.stderr is not None
        async for line in self._process.stderr:
            text = mask_url(line.decode(errors="replace").strip())
            if text:
                errors.append(text)
                _LOGGER.debug("ffmpeg: %s", text)

    async def _stop_process(self) -> None:
        """Terminate ffmpeg and reap it (no zombie)."""
        process, self._process = self._process, None
        self._buffer.clear()
        if process is None:
            return
        if process.returncode is None:
            try:
                process.terminate()
                await asyncio.wait_for(process.wait(), self.STOP_TIMEOUT_S)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
            except ProcessLookupError:
                pass
        else:
            await process.wait()


def _level_db(pcm: bytes) -> float:
    rms = audioop.rms(pcm[: len(pcm) // 2 * 2], WYOMING_WIDTH) if len(pcm) >= 2 else 0
    return 20 * math.log10(max(rms, 1) / 32768)
