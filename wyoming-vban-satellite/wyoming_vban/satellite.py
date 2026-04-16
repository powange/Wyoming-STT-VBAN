"""Wyoming satellite server that bridges VBAN audio to the HA voice pipeline."""

import asyncio
import logging
from typing import Optional

from wyoming.audio import AudioChunk, AudioFormat, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, Satellite
from wyoming.pipeline import RunPipeline
from wyoming.satellite import (
    PauseSatellite,
    RunSatellite,
    StreamingStarted,
    StreamingStopped,
)
from wyoming.server import AsyncEventHandler

from .const import WYOMING_CHANNELS, WYOMING_RATE, WYOMING_WIDTH
from .vban import VbanPacket, VbanReceiver, VbanSender, resample_to_wyoming

_LOGGER = logging.getLogger(__name__)


class VbanSatelliteHandler(AsyncEventHandler):
    """Handles a single Wyoming client connection (Home Assistant server).

    Responds to describe, streams VBAN audio as audio-chunk events,
    and forwards TTS audio-chunk events back to VBAN sender.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        satellite_info: Info,
        vban_receiver: VbanReceiver,
        vban_sender: Optional[VbanSender],
    ):
        super().__init__(reader, writer)
        self._info = satellite_info
        self._vban_receiver = vban_receiver
        self._vban_sender = vban_sender
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=100)
        self._streaming = False
        self._streaming_task: Optional[asyncio.Task] = None
        self._receiver_started = False
        self._first_packet_logged = False

    async def handle_event(self, event: Event) -> bool:
        """Handle an incoming Wyoming event from the HA server."""

        if Describe.is_type(event.type):
            await self.write_event(self._info.event())
            _LOGGER.debug("Sent satellite info")
            return True

        if RunSatellite.is_type(event.type):
            _LOGGER.info("Server ready — starting VBAN audio streaming")
            await self._start_streaming()
            return True

        if PauseSatellite.is_type(event.type):
            _LOGGER.info("Server paused — stopping audio streaming")
            await self._stop_streaming()
            return True

        if RunPipeline.is_type(event.type):
            pipeline = RunPipeline.from_event(event)
            _LOGGER.debug(
                "Pipeline requested: start=%s end=%s restart=%s",
                pipeline.start_stage, pipeline.end_stage, pipeline.restart_on_end,
            )
            if not self._streaming:
                await self._start_streaming()
            return True

        if AudioStart.is_type(event.type):
            _LOGGER.debug("Receiving TTS audio from server")
            return True

        if AudioChunk.is_type(event.type):
            if self._vban_sender:
                chunk = AudioChunk.from_event(event)
                self._vban_sender.send(chunk.audio)
            return True

        if AudioStop.is_type(event.type):
            _LOGGER.debug("TTS audio finished")
            return True

        if event.type == "ping":
            await self.write_event(Event(type="pong"))
            return True

        _LOGGER.debug("Unhandled event type: %s", event.type)
        return True

    async def _start_streaming(self) -> None:
        """Start the VBAN receiver and audio streaming loop."""
        if self._streaming:
            return

        self._streaming = True

        # Start VBAN receiver once (persists across pause/resume)
        if not self._receiver_started:
            self._receiver_started = True
            asyncio.create_task(
                self._vban_receiver.start(self._on_vban_audio)
            )

        # Start streaming loop
        self._streaming_task = asyncio.create_task(self._stream_audio())

    async def _stop_streaming(self) -> None:
        """Stop the audio streaming loop (VBAN receiver stays alive)."""
        if not self._streaming:
            return

        self._streaming = False

        if self._streaming_task:
            self._streaming_task.cancel()
            try:
                await self._streaming_task
            except asyncio.CancelledError:
                pass
            self._streaming_task = None

        # Drain the audio queue
        while not self._audio_queue.empty():
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def _stream_audio(self) -> None:
        """Continuously read from the VBAN queue and send audio-chunk events."""
        _LOGGER.info("Audio streaming started")
        await self.write_event(StreamingStarted().event())

        chunks_sent = 0
        timeouts = 0

        try:
            while self._streaming and self._is_running:
                try:
                    pcm = await asyncio.wait_for(self._audio_queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    timeouts += 1
                    if timeouts == 1 or timeouts % 5 == 0:
                        _LOGGER.warning(
                            "No VBAN audio received for %d seconds (chunks sent so far: %d)",
                            timeouts * 2, chunks_sent,
                        )
                    continue

                timeouts = 0
                chunks_sent += 1

                if chunks_sent == 1:
                    _LOGGER.info("First audio chunk sent to Wyoming (%d bytes)", len(pcm))
                elif chunks_sent % 500 == 0:
                    _LOGGER.debug("Audio chunks sent: %d", chunks_sent)

                chunk = AudioChunk(
                    rate=WYOMING_RATE,
                    width=WYOMING_WIDTH,
                    channels=WYOMING_CHANNELS,
                    audio=pcm,
                )
                await self.write_event(chunk.event())
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            self._streaming = False
            try:
                await self.write_event(StreamingStopped().event())
            except (ConnectionError, OSError):
                pass
            _LOGGER.info("Audio streaming stopped (total chunks sent: %d)", chunks_sent)

    def _on_vban_audio(self, packet: VbanPacket) -> None:
        """Callback from VBAN receiver — enqueue resampled audio."""
        if not self._first_packet_logged:
            _LOGGER.info(
                "First VBAN packet from stream '%s': %dHz, %dch, format=%d",
                packet.stream_name,
                packet.sample_rate,
                packet.channels,
                packet.data_format,
            )
            self._first_packet_logged = True

        if not self._streaming:
            return

        pcm = resample_to_wyoming(packet.payload, packet)

        try:
            self._audio_queue.put_nowait(pcm)
        except asyncio.QueueFull:
            try:
                self._audio_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._audio_queue.put_nowait(pcm)

    async def disconnect(self) -> None:
        """Called when the client disconnects."""
        _LOGGER.info("Client disconnected")
        await self._stop_streaming()

        self._vban_receiver.stop()


def make_satellite_info(name: str, has_tts_output: bool) -> Info:
    """Build the Wyoming Info descriptor for this satellite.

    Only declare 'satellite' — no mic/snd programs.
    This matches the official wyoming-satellite behavior and ensures
    HA creates an assist_satellite entity (not assist_microphone).
    """
    return Info(
        satellite=Satellite(
            name=name,
            attribution=Attribution(name="", url=""),
            installed=True,
            description=name,
            version=None,
        ),
    )
