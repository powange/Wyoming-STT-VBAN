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
from wyoming.snd import Played

from .const import WYOMING_CHANNELS, WYOMING_RATE, WYOMING_WIDTH
from .media_player import MediaPlayerOutput, build_wav
from .vban import VbanPacket, VbanReceiver, VbanSender, resample_to_wyoming

_LOGGER = logging.getLogger(__name__)

# Media player playbacks outlive their handler (HA may reconnect while a
# response plays); keep strong references so they aren't garbage-collected.
_PLAYBACK_TASKS: "set[asyncio.Task]" = set()


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

    MAX_TTS_BUFFER_S = 300  # media_player mode: longest response buffered
    CAST_START_DELAY_S = 1.5  # time for Cast to fetch the WAV and start playing

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        satellite_info: Info,
        vban_receiver: VbanReceiver,
        vban_sender: Optional[VbanSender],
        media_output: Optional[MediaPlayerOutput] = None,
        vban_stream_name: str = "",
    ):
        super().__init__(reader, writer)
        self._info = satellite_info
        self._vban_receiver = vban_receiver
        # VBAN stream routed to this satellite ("" = any unclaimed stream)
        self._vban_stream_name = vban_stream_name
        self._vban_sender = vban_sender
        # When set, TTS goes to a HA media_player; VBAN is only a fallback.
        self._media_output = media_output
        self._tts_pcm = bytearray()
        self._tts_format = (WYOMING_RATE, WYOMING_WIDTH, WYOMING_CHANNELS)
        self._tts_overflow_logged = False
        self._playback_task: Optional[asyncio.Task] = None
        # Max ~500ms of buffering: at 16kHz VBAN packets (~16ms each), 32 slots
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)
        self._streaming = False
        self._streaming_task: Optional[asyncio.Task] = None
        self._subscribed = False
        self._first_packet_logged = False
        self._queue_full_logged = False  # rate-limit queue-full warnings
        self._server_ready = False  # True after RunSatellite received
        self._restart_task: Optional[asyncio.Task] = None

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
            self._subscribe_to_vban()
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
            start = AudioStart.from_event(event)
            _LOGGER.info(
                "Receiving TTS audio from server: %dHz, %d-bit, %dch",
                start.rate, start.width * 8, start.channels,
            )
            if self._media_output:
                # Buffer the whole response as-is (no resampling); it is
                # played as a WAV file once AudioStop arrives.
                self._tts_format = (start.rate, start.width, start.channels)
                self._tts_pcm = bytearray()
                self._tts_overflow_logged = False
                return True
            # Reset resampler state AND drop any leftover audio from a
            # previous stream so the new TTS starts cleanly.
            if self._vban_sender:
                self._vban_sender.reset_resampler()
                self._vban_sender.clear_pending()
            return True

        if AudioChunk.is_type(event.type):
            if self._media_output:
                self._buffer_tts_chunk(AudioChunk.from_event(event).audio)
                return True
            if self._vban_sender:
                try:
                    chunk = AudioChunk.from_event(event)
                    self._vban_sender.send(
                        chunk.audio,
                        sample_rate=chunk.rate,
                        width=chunk.width,
                        channels=chunk.channels,
                    )
                except (audioop.error, ValueError, KeyError) as err:
                    _LOGGER.warning(
                        "Failed to forward TTS audio chunk to VBAN: %s", err,
                    )
            return True

        if AudioStop.is_type(event.type):
            _LOGGER.debug("TTS audio finished")
            if self._media_output and self._tts_pcm:
                pcm, self._tts_pcm = bytes(self._tts_pcm), bytearray()
                # A newer response replaces one still waiting to report played
                if self._playback_task and not self._playback_task.done():
                    self._playback_task.cancel()
                # Run in the background so the event reader isn't blocked
                # while play_media runs and the response plays.
                self._playback_task = asyncio.create_task(
                    self._play_on_media_player(pcm, *self._tts_format)
                )
                _PLAYBACK_TASKS.add(self._playback_task)
                self._playback_task.add_done_callback(_PLAYBACK_TASKS.discard)
            return True

        if event.type == "ping":
            await self.write_event(Event(type="pong"))
            return True

        if event.type == "error":
            text = (event.data or {}).get("text", "<no text>")
            code = (event.data or {}).get("code", "<no code>")
            _LOGGER.error("Error from server: [%s] %s", code, text)
            # After an error (duplicate_wake_up_detected, stt-no-text-recognized,
            # wake_word_detection_aborted...), HA ends the pipeline and does NOT
            # restart it, even with restart_on_end=True. Audio chunks we keep
            # sending are rejected, leaving the satellite deaf. Re-send
            # RunPipeline ourselves so we go back to listening for the wake word.
            self._schedule_pipeline_restart()
            return True

        _LOGGER.debug("Unhandled event type: %s", event.type)
        return True

    def _buffer_tts_chunk(self, audio: bytes) -> None:
        """Append TTS PCM to the response buffer, capped in duration."""
        rate, width, channels = self._tts_format
        max_bytes = self.MAX_TTS_BUFFER_S * rate * width * channels
        if len(self._tts_pcm) + len(audio) > max_bytes:
            if not self._tts_overflow_logged:
                _LOGGER.warning(
                    "TTS response longer than %ds — truncating",
                    self.MAX_TTS_BUFFER_S,
                )
                self._tts_overflow_logged = True
            return
        self._tts_pcm.extend(audio)

    async def _play_on_media_player(
        self, pcm: bytes, rate: int, width: int, channels: int
    ) -> None:
        """Publish the response as a WAV, play it, then report played to HA.

        If play_media fails, the response goes to VBAN when configured.
        """
        assert self._media_output is not None
        duration = len(pcm) / (rate * width * channels)
        url = self._media_output.publish(build_wav(pcm, rate, width, channels))
        _LOGGER.info(
            "TTS published at %s (%.1fs WAV) for %s",
            url, duration, self._media_output.entity_id,
        )

        if not await self._media_output.play(url):
            if self._vban_sender is None:
                _LOGGER.error("No VBAN output configured — response dropped")
                return
            _LOGGER.warning("Falling back to VBAN output for this response")
            try:
                self._vban_sender.reset_resampler()
                self._vban_sender.clear_pending()
                self._vban_sender.send(
                    pcm, sample_rate=rate, width=width, channels=channels,
                )
            except (audioop.error, ValueError) as err:
                _LOGGER.warning("Failed to send TTS audio to VBAN: %s", err)
            return

        # Tell HA the response finished playing, like a satellite with its
        # own speaker would (entity goes back to idle, announcements stop
        # waiting). Cast needs a moment to fetch the file and start playing.
        await asyncio.sleep(duration + self.CAST_START_DELAY_S)
        if self.writer.is_closing():
            return
        try:
            await self.write_event(Played().event())
            _LOGGER.debug("Sent played")
        except (ConnectionError, OSError) as err:
            _LOGGER.debug("Could not send played: %s", err)

    def _subscribe_to_vban(self) -> None:
        """Subscribe this handler to VBAN audio packets."""
        if not self._subscribed:
            self._vban_receiver.subscribe(self._on_vban_audio, self._vban_stream_name)
            self._subscribed = True

    async def _send_pipeline_and_stream(self) -> None:
        """Send RunPipeline to HA, schedule audio streaming after a short delay.

        Must be called AFTER responding to Describe, so HA has the Info
        before it receives RunPipeline. Audio chunks must arrive after HA
        has processed RunPipeline (set _is_pipeline_running=True).

        The 300ms grace period is scheduled as a background task so we
        don't block the event reader while waiting.
        """
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

        # Mark streaming as pending and schedule startup in the background,
        # so handle_event can return immediately and keep reading HA events.
        self._streaming = True
        self._streaming_task = asyncio.create_task(
            self._delayed_stream_start(0.3)
        )

    async def _delayed_stream_start(self, delay: float) -> None:
        """Wait for HA to process RunPipeline, then run the audio stream loop."""
        try:
            await asyncio.sleep(delay)
            if self._streaming and self._is_running:
                await self._stream_audio()
        except asyncio.CancelledError:
            pass

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

    def _schedule_pipeline_restart(self, delay: float = 0.5) -> None:
        """Restart the wake→TTS pipeline in the background after an error."""
        if self._restart_task and not self._restart_task.done():
            return
        self._restart_task = asyncio.create_task(
            self._restart_pipeline_after_error(delay)
        )

    async def _restart_pipeline_after_error(self, delay: float) -> None:
        try:
            await self._stop_streaming()
            await asyncio.sleep(delay)
            if self._server_ready and self._is_running and not self._streaming:
                _LOGGER.info("Restarting pipeline after server error")
                await self._send_pipeline_and_stream()
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass

    async def _stream_audio(self) -> None:
        """Continuously read from the VBAN queue and send audio-chunk events.

        Buffers small VBAN packets into larger chunks (~1024 samples / 64ms)
        to match what openWakeWord and the Wyoming pipeline expect.
        """
        _LOGGER.info("Audio streaming started")
        await self.write_event(StreamingStarted().event())

        chunks_sent = 0
        timeouts = 0
        had_timeout = False
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
                    had_timeout = True
                    if timeouts == 1 or timeouts % 5 == 0:
                        _LOGGER.warning(
                            "No VBAN audio received for %d seconds (chunks sent so far: %d)",
                            timeouts * 2, chunks_sent,
                        )
                    continue

                # Reset queue-full flag once we've drained past the threshold
                if self._queue_full_logged and self._audio_queue.qsize() < 4:
                    self._queue_full_logged = False

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

                    # Log first chunk, and first chunk after any silence
                    if chunks_sent == 1:
                        _LOGGER.info(
                            "First audio chunk sent to Wyoming (%d bytes, %.1f dB)",
                            len(send_pcm), db,
                        )
                    elif had_timeout:
                        _LOGGER.info(
                            "VBAN audio resumed (chunk #%d, %.1f dB)",
                            chunks_sent, db,
                        )
                        had_timeout = False
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
            # Drop this new packet rather than the oldest — preserves
            # audio continuity (no discontinuity jump for wake word detection).
            # A full queue means the Wyoming writer is backlogged; log once
            # per overflow burst to help diagnose without log spam.
            if not self._queue_full_logged:
                _LOGGER.warning(
                    "Audio queue full — dropping incoming VBAN packets "
                    "(HA may be slow to consume)"
                )
                self._queue_full_logged = True

    async def disconnect(self) -> None:
        """Called when the client disconnects."""
        _LOGGER.info("Client disconnected")
        self._server_ready = False
        if self._restart_task and not self._restart_task.done():
            self._restart_task.cancel()
            try:
                await self._restart_task
            except asyncio.CancelledError:
                pass
        self._restart_task = None
        await self._stop_streaming()

        # Unsubscribe from VBAN audio (receiver keeps running for other handlers)
        if self._subscribed:
            self._vban_receiver.unsubscribe(self._on_vban_audio)
            self._subscribed = False


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
