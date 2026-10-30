"""Wyoming satellite server that bridges VBAN audio to the HA voice pipeline."""

import asyncio
import audioop
import collections
import logging
import math
import struct
import time
from dataclasses import dataclass
from typing import Callable, Optional

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
from wyoming.tts import Synthesize

from .const import WYOMING_CHANNELS, WYOMING_RATE, WYOMING_WIDTH
from .media_player import MediaPlayerOutput, build_wav
from .vban import VbanPacket, VbanReceiver, VbanSender, resample_to_wyoming

_LOGGER = logging.getLogger(__name__)

# Media player playbacks outlive their handler (HA may reconnect while a
# response plays); keep strong references so they aren't garbage-collected.
_PLAYBACK_TASKS: "set[asyncio.Task]" = set()

FALLBACK_WARNING_INTERVAL_S = 60.0
_last_fallback_warning = 0.0


def _warn_timing_fallback(reason: str) -> None:
    """Warn (at most once a minute) that playback end is estimated."""
    global _last_fallback_warning
    now = time.monotonic()
    if now - _last_fallback_warning >= FALLBACK_WARNING_INTERVAL_S:
        _LOGGER.warning(
            "%s — estimating the end of playback from the WAV duration", reason,
        )
        _last_fallback_warning = now


@dataclass
class FollowupSettings:
    """Listening without the wake word after a question (per satellite)."""

    enabled: bool = True
    beep: bool = True
    timeout_s: float = 8.0


def make_beep(rate: int, seconds: float = 0.15, freq: float = 880.0) -> bytes:
    """Short 16-bit mono sine beep with 10 ms fades (no clicks)."""
    count = int(rate * seconds)
    fade = max(1, int(rate * 0.01))
    samples = []
    for i in range(count):
        gain = min(1.0, i / fade, (count - 1 - i) / fade)
        samples.append(int(0.3 * 32767 * gain * math.sin(2 * math.pi * freq * i / rate)))
    return struct.pack(f"<{count}h", *samples)


class MicMute:
    """Mutes the microphone while a response plays on the media_player.

    HA restarts wake word detection as soon as the pipeline run ends, which
    is before the response has played on the media_player: the speaker's
    voice would reach the microphone and trigger false wake-ups. (VBAN
    output doesn't need this: the receiver is half-duplex.)

    Shared by all connections of one satellite, so a reconnect from HA
    while a response plays keeps the microphone muted.
    """

    def __init__(self, tail_s: float, max_muted_s: float):
        self.tail_s = tail_s  # extra mute after playback (echo, Cast end)
        self._max_muted_s = max_muted_s
        self._muted = False  # a response is being received or played
        self._muted_since = 0.0
        self._generation = 0  # increments with each response

    def start_response(self) -> int:
        """Mute when HA starts sending a response; returns its id."""
        self._generation += 1
        self._muted = True
        self._muted_since = time.monotonic()
        return self._generation

    def end_response(self, generation: int) -> None:
        """Response done (played plus tail, failed or dropped): unmute.

        Ignored if a newer response has started since: it owns the mute.
        """
        if generation == self._generation:
            self._muted = False

    def is_muted(self) -> bool:
        if self._muted and time.monotonic() - self._muted_since > self._max_muted_s:
            # Safety net: never stay muted if a response never completes
            _LOGGER.warning(
                "Microphone muted for over %ds — unmuting", self._max_muted_s,
            )
            self._muted = False
        return self._muted


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
    # Estimated time for Cast to fetch the WAV and start playing, used only
    # when the media_player state can't be watched
    CAST_START_DELAY_S = 1.5
    PLAY_START_TIMEOUT_S = 5.0  # max wait for the media_player to be playing
    PLAY_END_MARGIN_S = 5.0  # max wait for the end: WAV duration + this

    # Follow-up (satellite-driven runs)
    MAX_FOLLOWUPS = 3  # consecutive follow-ups before requiring the wake word
    STREAM_GRACE_S = 0.3  # let HA process RunPipeline before sending audio
    ERROR_RESTART_DELAY_S = 0.5  # let HA end the failed run first
    # A transcript with no TTS response nor error within this time means HA
    # skipped TTS (empty response): the run is over.
    RESPONSE_TIMEOUT_S = 60.0
    # Run end not seen this long after stopping a follow-up: assume it ended
    FOLLOWUP_END_TIMEOUT_S = 10.0
    # VBAN receiver (e.g. Atom) buffering after the last packet is sent
    VBAN_PLAYBACK_LATENCY_S = 0.4
    # Follow-up beep: mic audio replaced by silence this long after the beep
    # is started, so it isn't captured. media_player: from play_media's
    # HTTP 200 (Cast starts in 0.07-0.2 s, beep 0.15 s); VBAN: beep + Atom
    # prebuffer, from sending.
    BEEP_GUARD_S = 0.45
    BEEP_SECONDS = 0.15
    # Mic audio kept during the guard, sent first if the user is already
    # speaking when it ends (answer started right after the beep)
    PREROLL_S = 0.6
    PREROLL_VOICE_DB = -45.0  # level of the last 128 ms counted as speech

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        satellite_info: Info,
        vban_receiver: VbanReceiver,
        vban_sender: Optional[VbanSender],
        media_output: Optional[MediaPlayerOutput] = None,
        vban_stream_name: str = "",
        mic_mute: Optional[MicMute] = None,
        followup: Optional[FollowupSettings] = None,
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
        # media_player mode: mic muted while a response plays (shared state)
        self._mic_mute = mic_mute
        self._tts_generation = 0  # MicMute id of the response being received
        self._tts_receiving = False  # between AudioStart and AudioStop
        # Max ~500ms of buffering: at 16kHz VBAN packets (~16ms each), 32 slots
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=32)
        self._streaming = False
        self._streaming_task: Optional[asyncio.Task] = None
        self._subscribed = False
        self._first_packet_logged = False
        self._queue_full_logged = False  # rate-limit queue-full warnings
        self._server_ready = False  # True after RunSatellite received
        self._restart_task: Optional[asyncio.Task] = None

        # Follow-up: when enabled, the satellite drives the pipeline runs
        # itself (restart_on_end=False) so it can start a run at the ASR
        # stage after a question. HA's restart_on_end always restarts at
        # the wake stage, and a run started while another is active gets
        # its audio rejected (the cancelled run's run-end resets HA's
        # "pipeline running" flag), so there must never be two runs.
        self._followup = followup or FollowupSettings(enabled=False)
        self._driven = self._followup.enabled
        self._run_seq = 0  # increments with each RunPipeline we send
        self._run_ended_seq = 0  # last run whose end was handled
        self._awaiting_response = False  # transcript received in this run
        self._response_audio = False  # incoming TTS is this run's response
        self._expect_followup = False  # the response is a question
        self._followup_count = 0  # consecutive follow-ups
        self._followup_active = False  # current run is a follow-up (ASR)
        self._followup_timed_out = False
        self._next_run_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        # Mic audio is not sent between runs (HA would reject it), nor
        # before the grace period after RunPipeline.
        self._mic_paused = False
        self._mic_resume_at = 0.0
        # Follow-up beep guard: mic replaced by silence until this time
        self._mic_guard_until = 0.0
        self._preroll: collections.deque[bytes] = collections.deque(
            maxlen=max(1, int(self.PREROLL_S * WYOMING_RATE / 1024))
        )

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
            self._cancel_followup_tasks()
            await self._stop_streaming()
            return True

        if event.type == "transcript":
            # STT done: a TTS response (or an error) normally follows. TTS
            # audio from now on is this run's response, not an announcement.
            self._awaiting_response = True
            if self._driven:
                self._set_watchdog(self.RESPONSE_TIMEOUT_S, self._on_response_timeout)
            return True

        if event.type == "voice-started":
            # Speech in a follow-up run (once listening): no more timeout
            if (
                self._followup_active
                and not self._followup_timed_out
                and time.monotonic() >= self._mic_guard_until
            ):
                self._cancel_watchdog()
            return True

        if Synthesize.is_type(event.type):
            text = Synthesize.from_event(event).text or ""
            if self._driven:
                self._expect_followup = text.strip().endswith("?")
                _LOGGER.debug(
                    "Response text%s: %s",
                    " (question)" if self._expect_followup else "", text,
                )
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
            # After a transcript, this is the run's response; otherwise an
            # announcement (it doesn't end a run).
            self._response_audio = self._awaiting_response
            self._awaiting_response = False
            if self._response_audio and self._driven:
                self._cancel_watchdog()
                self._mic_paused = True  # the run is over: HA rejects mic audio
            if self._media_output:
                # Buffer the whole response as-is (no resampling); it is
                # played as a WAV file once AudioStop arrives.
                self._tts_format = (start.rate, start.width, start.channels)
                self._tts_pcm = bytearray()
                self._tts_overflow_logged = False
                self._tts_receiving = True
                # Mute the mic right away, before play_media
                if self._mic_mute:
                    self._tts_generation = self._mic_mute.start_response()
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
            self._tts_receiving = False
            # Run to end once the response has played (follow-up mode)
            seq = self._run_seq if (self._response_audio and self._driven) else None
            self._response_audio = False
            if self._media_output and not self._tts_pcm:
                # Nothing to play: unmute now
                if self._mic_mute:
                    self._mic_mute.end_response(self._tts_generation)
                if seq is not None:
                    self._run_ended(seq)
            elif self._media_output:
                pcm, self._tts_pcm = bytes(self._tts_pcm), bytearray()
                # A newer response replaces one still waiting to report played
                if self._playback_task and not self._playback_task.done():
                    self._playback_task.cancel()
                # Run in the background so the event reader isn't blocked
                # while play_media runs and the response plays.
                self._playback_task = asyncio.create_task(
                    self._play_on_media_player(
                        pcm, *self._tts_format, self._tts_generation,
                        on_played=(
                            (lambda: self._run_ended(seq)) if seq is not None else None
                        ),
                    )
                )
                _PLAYBACK_TASKS.add(self._playback_task)
                self._playback_task.add_done_callback(_PLAYBACK_TASKS.discard)
            elif seq is not None:
                if self._vban_sender:
                    task = asyncio.create_task(self._run_ended_after_vban(seq))
                    _PLAYBACK_TASKS.add(task)
                    task.add_done_callback(_PLAYBACK_TASKS.discard)
                else:
                    self._run_ended(seq)
            return True

        if event.type == "ping":
            await self.write_event(Event(type="pong"))
            return True

        if event.type == "error":
            text = (event.data or {}).get("text", "<no text>")
            code = (event.data or {}).get("code", "<no code>")
            _LOGGER.error("Error from server: [%s] %s", code, text)
            if self._driven:
                # The run is over (no response). Start the next one once HA
                # has finished ending it; an error never leads to a follow-up.
                self._awaiting_response = False
                self._expect_followup = False
                self._run_ended(self._run_seq, delay=self.ERROR_RESTART_DELAY_S)
                return True
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
        self, pcm: bytes, rate: int, width: int, channels: int, generation: int,
        on_played: Optional[Callable[[], None]] = None,
    ) -> None:
        """Play the response on the media_player, keeping the mic muted.

        The mic stays muted until the response has played plus the tail; it
        is unmuted on failure or cancellation unless a newer response has
        started (which then owns the mute: no gap between the two).
        """
        try:
            await self._play_response(pcm, rate, width, channels, generation)
        finally:
            if self._mic_mute:
                self._mic_mute.end_response(generation)
        # Not reached when cancelled by a newer response (which reports itself)
        if on_played:
            on_played()

    async def _play_response(
        self, pcm: bytes, rate: int, width: int, channels: int, generation: int,
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

        requested_at = time.monotonic()
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
            await self._wait_vban_played()
            return

        await self._wait_playback_end(url, duration, requested_at)

        # Margin for echo / the very end of the Cast output
        if self._mic_mute:
            await asyncio.sleep(self._mic_mute.tail_s)
            self._mic_mute.end_response(generation)

        # Tell HA the response finished playing, like a satellite with its
        # own speaker would (entity goes back to idle, announcements stop
        # waiting).
        if self.writer.is_closing():
            return
        try:
            await self.write_event(Played().event())
            _LOGGER.debug("Sent played")
        except (ConnectionError, OSError) as err:
            _LOGGER.debug("Could not send played: %s", err)

    async def _wait_playback_end(
        self, url: str, duration: float, requested_at: float
    ) -> None:
        """Return when the media_player has finished playing url.

        Follows the media_player state over the HA WebSocket: wait for it to
        be playing our file, then to stop playing it. Without the WebSocket,
        or if playback is never seen starting, estimate the end from the WAV
        duration and CAST_START_DELAY_S (counted from the play_media call).
        """
        assert self._media_output is not None
        watcher = self._media_output.state_watcher
        entity_id = self._media_output.entity_id

        file_name = url.rsplit("/", 1)[-1]

        def is_ours(change) -> bool:
            # media_content_id is our URL on Cast; accept a missing one
            return file_name in str(change.attributes.get("media_content_id", url))

        started = None
        if watcher is None or not watcher.connected:
            _warn_timing_fallback("HA WebSocket not connected")
        else:
            started = await watcher.wait_for(
                entity_id,
                lambda c: c.state == "playing" and is_ours(c),
                since=requested_at,
                timeout=self.PLAY_START_TIMEOUT_S,
            )
            if started is None:
                _warn_timing_fallback(
                    f"{entity_id} not seen playing within "
                    f"{self.PLAY_START_TIMEOUT_S:.0f}s"
                )

        if started is None:
            estimated_end = requested_at + duration + self.CAST_START_DELAY_S
            await asyncio.sleep(max(0.0, estimated_end - time.monotonic()))
            return

        _LOGGER.debug("Cast playing after %.2fs", started.time - requested_at)
        finished = await watcher.wait_for(
            entity_id,
            lambda c: c.state not in ("playing", "buffering") or not is_ours(c),
            since=started.time,
            timeout=duration + self.PLAY_END_MARGIN_S,
        )
        if finished is None:
            # Timed out, or WebSocket lost: fall back to the WAV duration
            _LOGGER.debug("Cast end not seen, using the WAV duration")
            await asyncio.sleep(max(0.0, started.time + duration - time.monotonic()))
            return

        _LOGGER.debug(
            "Cast finished after %.2fs (WAV %.2fs)",
            finished.time - started.time, duration,
        )

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
        await self._send_run_pipeline(PipelineStage.WAKE)

        # Mark streaming as pending and schedule startup in the background,
        # so handle_event can return immediately and keep reading HA events.
        self._streaming = True
        self._streaming_task = asyncio.create_task(
            self._delayed_stream_start(0.3)
        )

    async def _send_run_pipeline(self, start_stage: PipelineStage) -> None:
        """Send RunPipeline (start_stage → TTS) and reset per-run state.

        Without follow-up, HA restarts the wake run itself (restart_on_end).
        """
        restart = not self._driven
        await self.write_event(
            RunPipeline(
                start_stage=start_stage,
                end_stage=PipelineStage.TTS,
                restart_on_end=restart,
            ).event()
        )
        self._run_seq += 1
        self._awaiting_response = False
        self._mic_paused = False
        self._mic_resume_at = time.monotonic() + self.STREAM_GRACE_S
        _LOGGER.info(
            "Sent run-pipeline: start=%s end=%s restart=%s",
            start_stage.value, PipelineStage.TTS.value, restart,
        )

    # ------------------------------------------------------------------
    # Follow-up: satellite-driven runs

    def _set_watchdog(self, delay: float, callback: Callable[[], None]) -> None:
        """(Re)arm the single per-run watchdog."""
        self._cancel_watchdog()

        async def fire() -> None:
            await asyncio.sleep(delay)
            callback()

        self._watchdog_task = asyncio.create_task(fire())

    def _cancel_watchdog(self) -> None:
        if self._watchdog_task and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        self._watchdog_task = None

    def _on_response_timeout(self) -> None:
        _LOGGER.info("No response after transcript (empty response?): run over")
        self._run_ended(self._run_seq)

    def _on_followup_timeout(self) -> None:
        """No speech in time after a question: stop listening."""
        _LOGGER.info("Follow-up: no speech, back to wake word")
        self._followup_timed_out = True
        self._mic_paused = True
        # Ends the ASR run (normally with stt-no-text-recognized)
        task = asyncio.create_task(self._write_quietly(AudioStop().event()))
        _PLAYBACK_TASKS.add(task)
        task.add_done_callback(_PLAYBACK_TASKS.discard)
        seq = self._run_seq
        self._set_watchdog(
            self.FOLLOWUP_END_TIMEOUT_S, lambda: self._run_ended(seq),
        )

    async def _write_quietly(self, event: Event) -> None:
        try:
            await self.write_event(event)
        except (ConnectionError, OSError):
            pass

    def _run_ended(self, seq: int, delay: float = 0.0) -> None:
        """The run seq is over (response played, error or timeout).

        Starts the next run: a follow-up if the response was a question,
        otherwise wake word listening. Ignored if that run's end was
        already handled or a newer run has started.
        """
        if not self._driven or seq != self._run_seq or seq == self._run_ended_seq:
            return
        self._run_ended_seq = seq
        self._cancel_watchdog()
        self._mic_paused = True
        if self._next_run_task and not self._next_run_task.done():
            self._next_run_task.cancel()
        self._next_run_task = asyncio.create_task(self._start_next_run(delay))

    async def _start_next_run(self, delay: float) -> None:
        try:
            await asyncio.sleep(delay)
            if not (self._server_ready and self._is_running) or self.writer.is_closing():
                return

            if self._followup_active:
                if not self._followup_timed_out:
                    _LOGGER.info("Follow-up: done")
                self._followup_active = False
                self._followup_timed_out = False

            expect, self._expect_followup = self._expect_followup, False
            if expect and self._followup_count < self.MAX_FOLLOWUPS:
                self._followup_count += 1
                _LOGGER.info(
                    "Follow-up: response is a question → listening without wake word"
                )
                # Start the ASR run right away, together with the beep, so HA
                # is ready as soon as the user answers. The mic is silenced
                # during the beep guard, then the no-speech timeout starts.
                self._followup_active = True
                self._preroll.clear()
                # Silenced until the beep has started (play_media may be slow)
                self._mic_guard_until = time.monotonic() + 3600
                await self._send_run_pipeline(PipelineStage.ASR)
                guard = await self._start_beep() if self._followup.beep else 0.0
                guard = max(guard, self.STREAM_GRACE_S)
                self._mic_guard_until = time.monotonic() + guard
                self._set_watchdog(guard, self._on_followup_listening)
                return

            if expect:
                _LOGGER.info(
                    "Follow-up: %d in a row, back to wake word", self._followup_count,
                )
            self._followup_count = 0
            await self._send_run_pipeline(PipelineStage.WAKE)
        except (ConnectionError, OSError):
            pass

    async def _start_beep(self) -> float:
        """Start the "I'm listening" beep on the satellite's output.

        Doesn't wait for it to play (a Cast device may not go back to idle
        after such a short sound). Returns how long the mic must stay
        silenced from now (0 if no beep could be played).
        """
        guard = 0.0
        if self._media_output:
            rate = 22050
            beep = make_beep(rate, self.BEEP_SECONDS)
            url = self._media_output.publish(build_wav(beep, rate, 2, 1))
            if await self._media_output.play(url):
                guard = self.BEEP_GUARD_S
        if not guard and self._vban_sender:
            self._vban_sender.reset_resampler()
            self._vban_sender.clear_pending()
            self._vban_sender.send(
                make_beep(WYOMING_RATE, self.BEEP_SECONDS), sample_rate=WYOMING_RATE,
            )
            guard = self.BEEP_SECONDS + self.VBAN_PLAYBACK_LATENCY_S
        if guard:
            _LOGGER.info("Follow-up: beep sent, mic opens in %.2fs", guard)
        return guard

    def _on_followup_listening(self) -> None:
        """Beep guard over: the user's answer now reaches HA."""
        _LOGGER.info("Follow-up: listening")
        self._set_watchdog(self._followup.timeout_s, self._on_followup_timeout)

    async def _wait_vban_played(self) -> None:
        """Return once the VBAN output has been sent and played."""
        assert self._vban_sender is not None
        await self._vban_sender.wait_sent()
        await asyncio.sleep(self.VBAN_PLAYBACK_LATENCY_S)

    async def _run_ended_after_vban(self, seq: int) -> None:
        await self._wait_vban_played()
        self._run_ended(seq)

    def _cancel_followup_tasks(self) -> None:
        self._cancel_watchdog()
        if self._next_run_task and not self._next_run_task.done():
            self._next_run_task.cancel()
        self._next_run_task = None
        self._followup_active = False
        self._followup_timed_out = False
        self._mic_guard_until = 0.0
        self._preroll.clear()
        self._expect_followup = False
        self._followup_count = 0
        self._awaiting_response = False
        self._response_audio = False

    # ------------------------------------------------------------------

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
        mic_muted = False

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

                    # Between runs: don't send
                    if self._mic_paused:
                        continue

                    # Follow-up beep guard: keep the real audio (pre-roll)
                    now = time.monotonic()
                    in_guard = now < self._mic_guard_until
                    if in_guard:
                        self._preroll.append(send_pcm)

                    # Right after RunPipeline: let HA process it first
                    if now < self._mic_resume_at:
                        continue

                    # During the guard, HA gets silence instead of the mic
                    if in_guard:
                        send_pcm = bytes(len(send_pcm))
                    elif self._preroll:
                        await self._flush_preroll()

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

                    # While a response plays on the media_player, send silence
                    # instead of the mic. Keep sending chunks: HA's wake word
                    # stage would otherwise starve and time out.
                    if self._mic_mute:
                        muted = self._mic_mute.is_muted()
                        if muted != mic_muted:
                            _LOGGER.debug(
                                "mic muted (TTS on media_player)" if muted
                                else "mic unmuted"
                            )
                            mic_muted = muted
                        if muted:
                            send_pcm = bytes(len(send_pcm))

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

    async def _flush_preroll(self) -> None:
        """After the beep guard: send the kept audio if speech has started."""
        chunks = list(self._preroll)
        self._preroll.clear()
        tail = b"".join(chunks[-2:])
        rms = audioop.rms(tail, WYOMING_WIDTH)
        db = 20 * math.log10(max(rms, 1) / 32768)
        if db < self.PREROLL_VOICE_DB:
            return
        _LOGGER.debug(
            "Follow-up: speech during the beep guard (%.1f dB), sending %d ms",
            db, len(chunks) * 64,
        )
        for audio in chunks:
            await self.write_event(
                AudioChunk(
                    rate=WYOMING_RATE,
                    width=WYOMING_WIDTH,
                    channels=WYOMING_CHANNELS,
                    audio=audio,
                ).event()
            )

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
        # A response cut off mid-transfer will never play: unmute
        if self._tts_receiving and self._mic_mute:
            self._mic_mute.end_response(self._tts_generation)
        self._tts_receiving = False
        self._cancel_followup_tasks()
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
