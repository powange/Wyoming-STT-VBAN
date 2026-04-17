"""Wyoming satellite server that bridges VBAN audio to the HA voice pipeline."""

import asyncio
import audioop
import logging
import math
from typing import Optional

from wyoming.audio import AudioChunk, AudioFormat, AudioStart, AudioStop
from wyoming.event import Event
from wyoming.info import Attribution, Describe, Info, Satellite, SndProgram
from wyoming.pipeline import PipelineStage, RunPipeline
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

    Protocol flow (critical ordering):
      1. HA sends Describe → we respond with Info
      2. HA sends RunSatellite → we mark ourselves ready
      3. HA sends Describe again (inside _run_pipeline_loop) → we respond with Info,
         then send RunPipeline, then start streaming audio after a brief delay

    Audio chunks MUST arrive after HA has processed RunPipeline, otherwise
    they are silently dropped (HA only forwards them when _is_pipeline_running=True).
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
        self._server_ready = False  # True after RunSatellite received

    async def run(self) -> None:
        """Run the handler, suppressing connection errors from the base class.

        The wyoming library's AsyncEventHandler.run() doesn't catch
        connection errors from async_read_event, so they propagate as
        unretrieved task exceptions. HA disconnects between pipeline
        cycles, which is normal.
        """
        try:
            await super().run()
        except (ConnectionError, OSError) as err:
            _LOGGER.debug("Connection closed by client: %s", err)

    async def handle_event(self, event: Event) -> bool:
        """Handle an incoming Wyoming event from the HA server."""

        if Describe.is_type(event.type):
            await self.write_event(self._info.event())
            _LOGGER.debug("Sent satellite info")

            # If the server already sent RunSatellite, this is the second
            # Describe (inside _run_pipeline_loop). Now we can start the pipeline.
            if self._server_ready and not self._streaming:
                await self._send_pipeline_and_stream()

            return True

        if RunSatellite.is_type(event.type):
            _LOGGER.info("Server ready")
            self._server_ready = True
            # Don't start streaming yet — wait for the next Describe
            # which HA sends inside _run_pipeline_loop
            self._ensure_vban_receiver()
            return True

        if PauseSatellite.is_type(event.type):
            _LOGGER.info("Server paused — stopping audio streaming")
            self._server_ready = False
            await self._stop_streaming()
            return True

        if RunPipeline.is_type(event.type):
            pipeline = RunPipeline.from_event(event)
            _LOGGER.debug(
                "Pipeline requested by server: start=%s end=%s restart=%s",
                pipeline.start_stage, pipeline.end_stage, pipeline.restart_on_end,
            )
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

    def _ensure_vban_receiver(self) -> None:
        """Start the VBAN receiver if not already running."""
        if not self._receiver_started:
            self._receiver_started = True
            asyncio.create_task(
                self._vban_receiver.start(self._on_vban_audio)
            )
            _LOGGER.info("VBAN receiver started")

    async def _send_pipeline_and_stream(self) -> None:
        """Send RunPipeline to HA, wait for it to be processed, then stream audio.

        This must be called AFTER responding to Describe, so HA has the Info
        before it receives RunPipeline. Audio chunks must arrive after HA
        has processed RunPipeline (set _is_pipeline_running=True).
        """
        # Tell HA to start the voice pipeline with wake word detection.
        # Always use TTS as end_stage — HA's pipeline crashes with KeyError
        # on 'tts_output' if end_stage is HANDLE because RUN_START event
        # data doesn't include tts_output. TTS audio is simply ignored
        # if no VBAN sender is configured.
        run_pipeline = RunPipeline(
            start_stage=PipelineStage.WAKE,
            end_stage=PipelineStage.TTS,
            restart_on_end=True,
        )
        await self.write_event(run_pipeline.event())
        _LOGGER.info(
            "Sent run-pipeline: start=%s end=%s restart=True",
            PipelineStage.WAKE.value, PipelineStage.TTS.value,
        )

        # Give HA time to process RunPipeline before we send audio
        await asyncio.sleep(0.3)

        # Now start the audio streaming task
        self._streaming = True
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
        """Continuously read from the VBAN queue and send audio-chunk events.

        Buffers small VBAN packets into larger chunks (~1024 samples / 64ms)
        to match what openWakeWord and the Wyoming pipeline expect.
        """
        _LOGGER.info("Audio streaming started")
        await self.write_event(StreamingStarted().event())

        chunks_sent = 0
        timeouts = 0
        peak_db = -100.0
        # Buffer to accumulate small VBAN packets into larger Wyoming chunks
        # Target: 1024 samples = 2048 bytes at 16-bit mono (64ms at 16kHz)
        target_bytes = 1024 * WYOMING_WIDTH * WYOMING_CHANNELS
        audio_buffer = bytearray()

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
                audio_buffer.extend(pcm)

                # Send when we have enough data
                while len(audio_buffer) >= target_bytes:
                    send_pcm = bytes(audio_buffer[:target_bytes])
                    del audio_buffer[:target_bytes]

                    chunks_sent += 1

                    # Track peak audio level over reporting window
                    rms = audioop.rms(send_pcm, WYOMING_WIDTH)
                    db = 20 * math.log10(max(rms, 1) / 32768)
                    if db > peak_db:
                        peak_db = db

                    # Log every 50 chunks (~3s) with peak level
                    if chunks_sent == 1:
                        _LOGGER.info(
                            "First audio chunk sent to Wyoming (%d bytes, %.1f dB)",
                            len(send_pcm), db,
                        )
                    elif chunks_sent % 50 == 0:
                        _LOGGER.debug(
                            "Audio chunks: %d (peak: %.1f dB, current: %.1f dB)",
                            chunks_sent, peak_db, db,
                        )
                        peak_db = -100.0

                    chunk = AudioChunk(
                        rate=WYOMING_RATE,
                        width=WYOMING_WIDTH,
                        channels=WYOMING_CHANNELS,
                        audio=send_pcm,
                    )
                    await self.write_event(chunk.event())
        except (ConnectionError, asyncio.CancelledError, OSError):
            pass
        finally:
            self._streaming = False
            try:
                if self._is_running:
                    await self.write_event(StreamingStopped().event())
            except (ConnectionError, OSError, asyncio.CancelledError):
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
        self._server_ready = False
        await self._stop_streaming()

        self._vban_receiver.stop()


def make_satellite_info(name: str) -> Info:
    """Build the Wyoming Info descriptor for this satellite.

    Always declare snd so HA knows the satellite can receive TTS audio.
    This allows HA to show the "Media player for TTS" option in the
    satellite config. If no VBAN speaker is configured, TTS audio is
    received and silently discarded.
    """
    return Info(
        snd=[
            SndProgram(
                name="snd",
                attribution=Attribution(name="", url=""),
                installed=True,
                description="Audio output",
                version=None,
                snd_format=AudioFormat(
                    rate=WYOMING_RATE,
                    width=WYOMING_WIDTH,
                    channels=WYOMING_CHANNELS,
                ),
            )
        ],
        satellite=Satellite(
            name=name,
            attribution=Attribution(name="", url=""),
            installed=True,
            description=name,
            version=None,
        ),
    )
