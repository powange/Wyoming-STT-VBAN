"""Wyoming satellite server that bridges VBAN audio to the HA voice pipeline."""

import asyncio
import audioop
import collections
import contextvars
import functools
import logging
import math
import re
import struct
import time
import wave
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Optional

from wyoming.asr import Transcript
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
from wyoming.wake import Detection

from .const import WYOMING_CHANNELS, WYOMING_RATE, WYOMING_WIDTH
from .ha_ws import HaStateWatcher
from .media_player import MediaPlayerOutput, build_wav
from .mic_filter import level_db
from .vban import VbanPacket, VbanReceiver, VbanSender

if TYPE_CHECKING:
    from .mic_filter import MicFilter
    from .wake_clips import ClipRecorder
    from .mirror import SatelliteMirrors
    from .tts_phrase import TtsPhrase

_LOGGER = logging.getLogger(__name__)

# Name of the satellite owning the current task, prefixed to log lines
# (all satellites run in one process). Tasks inherit it from the task
# that created them.
SATELLITE_NAME: contextvars.ContextVar[str] = contextvars.ContextVar(
    "satellite_name", default=""
)


def _as_satellite(method):
    """For the handler methods called from elsewhere (the HA WebSocket, a
    configuration change): their logs, and the tasks they start, carry the
    satellite's name (otherwise a filter on it hides them)."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        token = SATELLITE_NAME.set(self._satellite_name or SATELLITE_NAME.get())
        try:
            return method(self, *args, **kwargs)
        finally:
            SATELLITE_NAME.reset(token)

    return wrapper

def _update_clip(store, clip_id: str, fields: dict) -> None:
    try:
        store.update(clip_id, **fields)
    except OSError as err:
        _LOGGER.error("Wake word clip not updated: %s", err)


# Media player playbacks outlive their handler (HA may reconnect while a
# response plays); keep strong references so they aren't garbage-collected.
_PLAYBACK_TASKS: "set[asyncio.Task]" = set()


def _spawn(coro) -> asyncio.Task:
    """A background task kept until it ends (the loop only keeps a weak
    reference: an unreferenced task can be collected mid-way)."""
    task = asyncio.create_task(coro)
    _PLAYBACK_TASKS.add(task)
    task.add_done_callback(_PLAYBACK_TASKS.discard)
    return task


EXCHANGE_EVENT_TYPE = "jarvis_echange"  # HA event fired for each exchange

FALLBACK_WARNING_INTERVAL_S = 60.0
_last_fallback_warning: dict[str, float] = {}  # by speaker


def _warn_timing_fallback(reason: str, entity_id: str = "") -> None:
    """Warn (at most once a minute per speaker) that playback end is estimated."""
    now = time.monotonic()
    if now - _last_fallback_warning.get(entity_id, 0.0) >= FALLBACK_WARNING_INTERVAL_S:
        _LOGGER.warning(
            "%s — estimating the end of playback from the WAV duration", reason,
        )
        _last_fallback_warning[entity_id] = now


@dataclass
class FollowupSettings:
    """Listening without the wake word after a question (per satellite)."""

    enabled: bool = True
    beep: bool = True
    timeout_s: float = 8.0
    # Speaker playback latency of the beep (None: the output's default)
    beep_latency_s: Optional[float] = None
    # Margin after the microphone stream comes back (media_player.mic_mute_tail_ms)
    mic_tail_s: float = 0.25
    # Where the beep plays: "media_player" (with the responses) or "vban"
    beep_output: str = "media_player"


def make_tones(rate: int, notes: list[tuple[float, float]], volume: float) -> bytes:
    """16-bit mono sine notes [(Hz, seconds)], each with 10 ms fades (no clicks)."""
    samples = []
    fade = max(1, int(rate * 0.01))
    for freq, seconds in notes:
        count = int(rate * seconds)
        for i in range(count):
            gain = min(1.0, i / fade, (count - 1 - i) / fade)
            samples.append(
                int(volume * 32767 * gain * math.sin(2 * math.pi * freq * i / rate))
            )
    return struct.pack(f"<{len(samples)}h", *samples)


@functools.lru_cache(maxsize=4)
def make_beep(rate: int, seconds: float = 0.15) -> bytes:
    """Follow-up beep: one 880 Hz note (computed once per rate)."""
    return make_tones(rate, [(880.0, seconds)], 0.3)


@functools.lru_cache(maxsize=2)
def _beep_wav(rate: int, seconds: float) -> bytes:
    return build_wav(make_beep(rate, seconds), rate, 2, 1)


@dataclass
class Cue:
    """A short sound played on the satellite's output."""

    pcm: bytes
    rate: int
    width: int = 2
    channels: int = 1

    @property
    def duration(self) -> float:
        return len(self.pcm) / (self.rate * self.width * self.channels)

    @classmethod
    def tones(cls, notes: list[tuple[float, float]], volume: float) -> "Cue":
        return cls(make_tones(22050, notes, volume), 22050)

    @classmethod
    def from_wav(cls, path: str) -> "Cue":
        with wave.open(path, "rb") as wav_file:
            return cls(
                wav_file.readframes(wav_file.getnframes()),
                wav_file.getframerate(),
                wav_file.getsampwidth(),
                wav_file.getnchannels(),
            )


# Built-in announcement sounds, distinct from the follow-up beep (880 Hz)
THINKING_NOTES = [(660.0, 0.12), (520.0, 0.12)]  # two soft falling notes
SEARCH_NOTES = [(520.0, 0.08), (660.0, 0.08), (880.0, 0.08)]  # three rising
LISTEN_END_NOTES = [(1200.0, 0.06)]  # short high blip: request recorded


@dataclass
class Announcement:
    """One waiting signal: a sound, a spoken phrase, or both (in that order)."""

    sound: Optional[Cue] = None
    phrase: Optional["TtsPhrase"] = None  # its cue is None until synthesized
    # Played instead of a phrase not (yet) synthesized, unless the sound is
    # already part of the announcement
    fallback: Optional[Cue] = None

    def cues(self) -> list[tuple[Cue, str]]:
        items: list[tuple[Cue, str]] = []
        if self.sound is not None:
            items.append((self.sound, "sound"))
        if self.phrase is not None:
            if self.phrase.cue is not None:
                items.append((self.phrase.cue, f"'{self.phrase.text}'"))
            elif self.sound is None and self.fallback is not None:
                items.append((self.fallback, "sound (phrase not synthesized)"))
        return items


@dataclass
class WaitingCues:
    """Announcements while the response is being prepared (per satellite).

    None = signal disabled.
    """

    listen_end: Optional[Announcement] = None  # on the transcript
    thinking: Optional[Announcement] = None  # no response after the delay
    thinking_delay_s: float = 1.0
    search: Optional[Announcement] = None  # jarvis_recherche HA event
    vision: Optional[Announcement] = None  # jarvis_vision HA event


# Handlers between transcript and response: told about internet searches
_THINKING_HANDLERS: "set[VbanSatelliteHandler]" = set()


def notify_search(data: dict) -> None:
    """jarvis_recherche HA event: play the search sound where a response is
    being prepared (the event doesn't say which satellite asked)."""
    for handler in list(_THINKING_HANDLERS):
        handler.on_search()


def notify_vision(data: dict) -> None:
    """jarvis_vision HA event: say the vision phrase where a response is
    being prepared (the event doesn't say which satellite asked)."""
    for handler in list(_THINKING_HANDLERS):
        handler.on_vision()


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
    # Follow-up: the listening opens once the beep has really played (and
    # the response before it): the mic audio received until then is
    # replaced by silence, the no-speech timeout starts at the opening.
    BEEP_SECONDS = 0.15
    # Playback latency of the speaker by default (followup.beep_latency_ms):
    # VBAN after the last packet is sent (Atom buffer); Cast from play_media
    # when its state can't be followed
    BEEP_LATENCY_VBAN_S = VBAN_PLAYBACK_LATENCY_S
    BEEP_LATENCY_MEDIA_PLAYER_S = 1.5
    # Some satellites (Atom) send no microphone audio while they play: the
    # stream counts as cut after this gap, and the listening waits for it
    MIC_GAP_S = 0.3
    FOLLOWUP_OPEN_MAX_S = 4.0  # from the beep: listen anyway after this
    # Errors HA reports in normal use (no speech after a question, a second
    # wake word…): logged at INFO, not as failures
    ROUTINE_ERRORS = {
        "stt-no-text-recognized", "duplicate_wake_up_detected",
        "wake_word_detection_aborted", "stt-stream-failed",
    }
    # "?" at the end, maybe followed by quotes, brackets, emoji or spaces
    QUESTION_END = re.compile(r"\?[\W_]*$")
    # Waiting sound on a media_player: Cast start delay (0.07-0.2 s) margin,
    # for the mic silence and before the response can follow
    CUE_CAST_START_S = 0.3
    # A response waits for a short sound (beep, chime) to end, but cuts a
    # longer announcement (a spoken phrase) instead of waiting seconds
    CUE_FINISH_MAX_S = 1.0

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        satellite_info: Info,
        vban_receiver: VbanReceiver,
        vban_sender: Optional[VbanSender],
        media_output: Optional[MediaPlayerOutput] = None,
        mic_mute: Optional[MicMute] = None,
        followup: Optional[FollowupSettings] = None,
        exchange_publisher: Optional[HaStateWatcher] = None,
        satellite_name: str = "",
        mic_label: str = "VBAN",
        mic_filter: Optional["MicFilter"] = None,
        waiting_cues: Optional[WaitingCues] = None,
        debug_mirrors: Optional["SatelliteMirrors"] = None,
        clip_recorder: Optional[Callable[[], Optional["ClipRecorder"]]] = None,
    ):
        super().__init__(reader, writer)
        self._info = satellite_info
        self._vban_receiver = vban_receiver
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
        # (time received, audio)
        self._audio_queue: asyncio.Queue[tuple[float, bytes]] = asyncio.Queue(maxsize=32)
        self._streaming = False
        self._streaming_task: Optional[asyncio.Task] = None
        self._subscribed = False
        self._first_packet_logged = False
        self._queue_full_logged = False  # rate-limit queue-full warnings
        self._server_ready = False  # True after RunSatellite received
        self._restart_task: Optional[asyncio.Task] = None

        # Publishes each exchange (transcript + response) as a HA event
        self._exchange_publisher = exchange_publisher
        self._satellite_name = satellite_name
        self._mic_label = mic_label  # microphone source in logs (VBAN, RTSP)
        # Microphone clean-up (DC offset, noise suppression, auto gain);
        # shared by the satellite's connections
        self._mic_filter = mic_filter
        self._last_transcript = ""
        self._last_transcript_at = 0.0
        # A response cut by a newer TTS stream hands its run end over to it
        self._playback_seq: Optional[int] = None
        self._was_paused = False  # resumed by RunSatellite (maybe no Describe)
        self._resume_task: Optional[asyncio.Task] = None
        self._switch_task: Optional[asyncio.Task] = None  # follow-up turned on
        self._beep_output_warned = False
        self._transcript_expiry: Optional[asyncio.Task] = None

        # Debug copies of the audio sent to HA and of the responses
        self._mirrors = debug_mirrors

        # Wake word clips: the satellite's recorder (None when off), the
        # first sample of the current wake run, the clip being completed
        self._clip_recorder = clip_recorder or (lambda: None)
        self._wake_run = True  # the current run listens for the wake word
        self._wake_sample0: Optional[int] = None
        self._wake_sample0_pending = False
        self._clip: Optional[dict] = None  # {"id": ..., "meta": {...}}

        # Waiting sounds between transcript and response
        self._cues = waiting_cues or WaitingCues()
        self._thinking = False
        self._thinking_timer: Optional[asyncio.Task] = None
        self._search_announced = False  # at most one per run
        self._vision_announced = False  # at most one per run
        # Announcements to play, in order: (signal, cue, label)
        self._cue_queue: collections.deque[tuple[str, Cue, str]] = collections.deque()
        self._cue_task: Optional[asyncio.Task] = None  # plays the queue
        self._current_cue: Optional[Cue] = None
        self._mic_silence_until = 0.0  # mic replaced by silence (sound playing)

        # Follow-up: when enabled, the satellite drives the pipeline runs
        # itself (restart_on_end=False) so it can start a run at the ASR
        # stage after a question. HA's restart_on_end always restarts at
        # the wake stage, and a run started while another is active gets
        # its audio rejected (the cancelled run's run-end resets HA's
        # "pipeline running" flag), so there must never be two runs.
        self._followup = followup or FollowupSettings(enabled=False)
        self._driven = self._followup.enabled
        # HA forces its satellite state to idle about 2 s after a response
        # should have finished (its TTS timeout): until then, "idle" isn't
        # a run end
        self._ha_tts_idle_until = 0.0
        self._tts_stream_bytes = 0
        self._run_seq = 0  # increments with each RunPipeline we send
        self._next_run_reason = ""  # why the last run ended (logged with the next)
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
        # Follow-up: audio of the grace period after RunPipeline, sent first
        self._hold_grace_audio = False
        self._grace_audio: list[bytes] = []
        self._idle_guard_task: Optional[asyncio.Task] = None
        self._vban_end_task: Optional[asyncio.Task] = None
        self._vban_end_seq: Optional[int] = None
        # Follow-up: mic audio received before this time is replaced by
        # silence (until the listening really opens)
        self._mic_guard_until = 0.0
        # Microphone stream: last packet, and a packet after a gap
        self._last_mic_at = 0.0
        self._mic_back = asyncio.Event()

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

    @property
    def ha_ready(self) -> bool:
        """HA has started the satellite on this connection."""
        return self._server_ready

    @property
    def busy(self) -> bool:
        """An exchange is under way (response awaited or playing, follow-up)."""
        return self._followup_active or self._work_pending()

    def _work_pending(self) -> bool:
        """Something under way will end or start a run: a response awaited,
        received or playing, a sound, a pending task or timeout. The one
        definition (three lists of their own had drifted apart)."""
        pending = (
            self._next_run_task, self._playback_task, self._vban_end_task, self._restart_task,
            self._watchdog_task,  # an armed watchdog will end the run
        )
        return bool(
            self._awaiting_response or self._tts_receiving or self._cue_playing()
            or any(t is not None and not t.done() for t in pending)
            or (self._vban_sender is not None and self._vban_sender.pending_seconds() > 0)
        )

    @property
    def is_streaming(self) -> bool:
        """Microphone audio is being sent to HA."""
        return self._streaming

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
            if self._idle_guard_task is None or self._idle_guard_task.done():
                self._idle_guard_task = asyncio.create_task(self._idle_guard())
            if self._was_paused:
                # Resumed (unmuted): if no Describe follows, start anyway
                self._was_paused = False
                if self._resume_task and not self._resume_task.done():
                    self._resume_task.cancel()
                self._resume_task = asyncio.create_task(self._start_if_still_idle())
            return True

        if PauseSatellite.is_type(event.type):
            _LOGGER.info("Server paused — stopping audio streaming")
            self._server_ready = False
            self._was_paused = True
            self._cancel_followup_tasks()
            self._stop_thinking()
            await self._stop_streaming()
            return True

        if event.type == "transcript":
            # STT done: a TTS response (or an error) normally follows. TTS
            # audio from now on is this run's response, not an announcement.
            self._awaiting_response = True
            self._last_transcript = Transcript.from_event(event).text or ""
            self._clip_note(transcript=self._last_transcript)
            self._last_transcript_at = time.monotonic()
            self._start_thinking()
            self._expire_transcript_later()
            if self._driven:
                _LOGGER.debug("Transcript received, waiting for the response")
                self._set_watchdog(self.RESPONSE_TIMEOUT_S, self._on_response_timeout)
            return True

        if event.type == "detection":
            if not self._driven:
                # A new wake word: whatever was awaited from the last run is over
                self._forget_transcript()
            self._capture_wake_clip(Detection.from_event(event))
            return True

        if event.type == "voice-started":
            # Speech in a follow-up run: no more no-speech timeout, but a
            # long one still ends a run HA would end without telling us
            # (otherwise nothing brings the satellite back: deaf)
            if self._followup_active and not self._followup_timed_out and not self._mic_paused:
                self._set_watchdog(self.RESPONSE_TIMEOUT_S, self._on_speech_without_end)
            return True

        if Synthesize.is_type(event.type):
            text = Synthesize.from_event(event).text or ""
            if not self._awaiting_response:
                # An announcement, not this run's response
                _LOGGER.debug("Announcement text: %s", text)
                return True
            self._stop_thinking()
            self._clip_note(close=True, response=text)
            self._publish_exchange(text)
            if self._driven:
                self._expect_followup = bool(self.QUESTION_END.search(text.strip()))
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
            self._tts_stream_bytes = 0
            self._tts_format = (start.rate, start.width, start.channels)
            _LOGGER.info(
                "Receiving TTS audio from server: %dHz, %d-bit, %dch",
                start.rate, start.width * 8, start.channels,
            )
            if self._mirrors:
                self._mirrors.tts_start()
            # After a transcript, this is the run's response; otherwise an
            # announcement (it doesn't end a run).
            self._response_audio = self._awaiting_response
            self._awaiting_response = False
            if self._response_audio:
                self._stop_thinking()
            if self._response_audio and self._driven:
                self._cancel_watchdog()
                self._mic_paused = True  # the run is over: HA rejects mic audio
            if self._media_output:
                # Buffer the whole response as-is (no resampling); it is
                # played as a WAV file once AudioStop arrives.
                self._tts_pcm = bytearray()
                self._tts_overflow_logged = False
                self._tts_receiving = True
                # Mute the mic right away, before play_media
                if self._mic_mute:
                    self._tts_generation = self._mic_mute.start_response()
                return True
            # Reset resampler state AND drop any leftover audio from a
            # previous stream so the new TTS starts cleanly — unless a
            # waiting sound is playing: the response then follows it.
            if self._vban_sender:
                self._vban_sender.reset_resampler()
                if self._response_audio:
                    self._interrupt_long_cue()
                if not self._cue_playing():
                    self._vban_sender.clear_pending()
            return True

        if AudioChunk.is_type(event.type):
            chunk = AudioChunk.from_event(event)  # parsed once
            self._tts_stream_bytes += len(chunk.audio)
            if self._mirrors:
                self._mirrors.tts(chunk.audio, chunk.rate, chunk.width, chunk.channels)
            if self._media_output:
                self._buffer_tts_chunk(chunk.audio)
                return True
            if self._vban_sender:
                try:
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
            rate, width, channels = self._tts_format
            duration = self._tts_stream_bytes / max(1, rate * width * channels)
            # HA's TTS timeout: about duration + 2 s after the audio was sent
            self._ha_tts_idle_until = time.monotonic() + duration + 3.0
            if self._response_audio and not self._has_output():
                _LOGGER.info("Response not played (output: none)")
            self._tts_receiving = False
            # Run to end once the response has played (follow-up mode)
            seq = self._run_seq if (self._response_audio and self._driven) else None
            self._response_audio = False
            if self._media_output and not self._tts_pcm:
                # Nothing to play: unmute now
                if self._mic_mute:
                    self._mic_mute.end_response(self._tts_generation)
                if seq is not None:
                    self._run_ended(seq, reason="response empty")
            elif self._media_output:
                pcm, self._tts_pcm = bytes(self._tts_pcm), bytearray()
                # A newer response replaces one still waiting to report
                # played: it reports the cancelled one's run end too
                if self._playback_task and not self._playback_task.done():
                    self._playback_task.cancel()
                    if seq is None:
                        seq = self._playback_seq
                self._playback_seq = seq
                # Run in the background so the event reader isn't blocked
                # while play_media runs and the response plays.
                self._playback_task = _spawn(
                    self._play_on_media_player(
                        pcm, *self._tts_format, self._tts_generation,
                        on_played=(
                            (lambda: self._run_ended(seq, reason="response played"))
                            if seq is not None else None
                        ),
                    )
                )
            elif self._vban_sender:
                # A newer response replaces one still waiting to report
                # played: it ends the replaced one's run too
                if self._vban_end_task and not self._vban_end_task.done():
                    self._vban_end_task.cancel()
                    if seq is None:
                        seq = self._vban_end_seq
                self._vban_end_seq = seq
                self._vban_end_task = _spawn(self._run_ended_after_vban(seq))
            elif seq is not None:
                self._run_ended(seq, reason="response not played (no output)")
            return True

        if event.type == "ping":
            await self.write_event(Event(type="pong"))
            return True

        if event.type == "error":
            text = (event.data or {}).get("text", "<no text>")
            code = (event.data or {}).get("code", "<no code>")
            (_LOGGER.info if code in self.ROUTINE_ERRORS else _LOGGER.error)(
                "Error from server: [%s] %s", code, text,
            )
            self._clip_note(close=True, stt_error=code)
            self._last_transcript = ""
            self._stop_thinking()
            if self._driven:
                if self._run_ended_seq == self._run_seq:
                    # No run open (e.g. waiting for the beep): the end of an
                    # earlier one, not of the follow-up about to open
                    _LOGGER.info("Error [%s] while no run is open: ignored", code)
                    return True
                # The run is over (no response). Start the next one once HA
                # has finished ending it; an error never leads to a follow-up.
                if (self._followup_active and not self._awaiting_response
                        and not self._followup_timed_out):
                    _LOGGER.info(
                        "Follow-up: Home Assistant ended the run while listening (error %s)",
                        code,
                    )
                    self._followup_timed_out = True
                self._awaiting_response = False
                self._expect_followup = False
                self._run_ended(self._run_seq, delay=self.ERROR_RESTART_DELAY_S,
                                reason=f"error {code}")
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

    # ------------------------------------------------------------------
    # Waiting sounds

    def _expire_transcript_later(self) -> None:
        """No response nor error within RESPONSE_TIMEOUT_S (HA skipped the
        TTS): forget the transcript, so it isn't paired with a later
        response, and stop the waiting announcements."""
        if self._transcript_expiry and not self._transcript_expiry.done():
            self._transcript_expiry.cancel()

        async def expire() -> None:
            await asyncio.sleep(self.RESPONSE_TIMEOUT_S)
            if self._awaiting_response:
                _LOGGER.info("No response %.0fs after the transcript", self.RESPONSE_TIMEOUT_S)
                self._forget_transcript()

        self._transcript_expiry = asyncio.create_task(expire())

    def _forget_transcript(self) -> None:
        if not self._driven:  # driven runs end through their watchdog
            self._awaiting_response = False
        self._last_transcript = ""
        self._stop_thinking()

    async def _start_if_still_idle(self) -> None:
        """After a resume: HA normally sends Describe, which starts the
        pipeline; start it if that didn't happen."""
        try:
            await asyncio.sleep(1.0)
            if self._server_ready and self._is_running and not self._streaming:
                _LOGGER.info("Resumed without Describe: starting the pipeline")
                await self._send_pipeline_and_stream("resumed without Describe")
        except (ConnectionError, OSError, asyncio.CancelledError):
            pass

    def _has_output(self) -> bool:
        return self._media_output is not None or self._vban_sender is not None

    def _start_thinking(self) -> None:
        """Transcript received: the response is being prepared."""
        self._stop_thinking()
        cues = self._cues
        if not self._has_output() or not (
            cues.listen_end or cues.thinking or cues.search or cues.vision
        ):
            return
        self._thinking = True
        self._search_announced = False
        self._vision_announced = False
        _THINKING_HANDLERS.add(self)
        if cues.listen_end:
            self._announce("listen_end", cues.listen_end)
        if cues.thinking:
            self._thinking_timer = asyncio.create_task(self._thinking_after_delay())

    def _stop_thinking(self) -> None:
        """Response arriving, error or run over: no more announcements.

        The one playing finishes (the response follows it); the pending
        ones are dropped.
        """
        self._thinking = False
        _THINKING_HANDLERS.discard(self)
        self._cancel_thinking_timer()
        self._cue_queue.clear()

    def _cancel_thinking_timer(self) -> None:
        if self._thinking_timer and not self._thinking_timer.done():
            self._thinking_timer.cancel()
        self._thinking_timer = None

    async def _thinking_after_delay(self) -> None:
        await asyncio.sleep(self._cues.thinking_delay_s)
        if self._thinking and self._cues.thinking:
            self._announce("thinking", self._cues.thinking)

    @_as_satellite
    def on_search(self) -> None:
        """jarvis_recherche: the response needs an internet search."""
        if self._thinking and self._cues.search and not self._search_announced:
            self._search_announced = True
            self._replace_thinking()
            self._announce("search", self._cues.search)

    @_as_satellite
    def on_vision(self) -> None:
        """jarvis_vision: the response needs a camera look."""
        if self._thinking and self._cues.vision and not self._vision_announced:
            self._vision_announced = True
            self._replace_thinking()
            self._announce("vision", self._cues.vision)

    def _replace_thinking(self) -> None:
        """A more precise announcement replaces the thinking one, unless it
        has started (it then finishes first)."""
        self._cancel_thinking_timer()
        self._cue_queue = collections.deque(
            item for item in self._cue_queue if item[0] != "thinking"
        )

    def _announce(self, signal: str, announcement: Announcement) -> None:
        """Queue an announcement after the ones already queued or playing."""
        for cue, label in announcement.cues():
            self._cue_queue.append((signal, cue, label))
        if self._cue_queue and not self._cue_playing():
            self._cue_task = _spawn(self._play_cue_queue())

    def _cue_playing(self) -> bool:
        return self._cue_task is not None and not self._cue_task.done()

    async def _play_cue_queue(self) -> None:
        while self._cue_queue:
            signal, cue, label = self._cue_queue.popleft()
            _LOGGER.info("Announcement %s: %s (%.2fs)", signal, label, cue.duration)
            self._current_cue = cue
            try:
                await self._play_cue(cue)
            finally:
                self._current_cue = None

    async def _play_cue(self, cue: Cue) -> None:
        """Play one cue; returns once it has played (the next may follow)."""
        if self._media_output:
            url = self._media_output.publish(
                build_wav(cue.pcm, cue.rate, cue.width, cue.channels)
            )
            if await self._media_output.play(url):
                playing = cue.duration + self.CUE_CAST_START_S
                self._mic_silence_until = time.monotonic() + playing
                await asyncio.sleep(playing)
                return
        if self._vban_sender:
            try:
                self._vban_sender.send(
                    cue.pcm, sample_rate=cue.rate, width=cue.width,
                    channels=cue.channels,
                )
            except (audioop.error, ValueError) as err:
                _LOGGER.warning("Failed to send announcement to VBAN: %s", err)
                return
            self._mic_silence_until = (
                time.monotonic() + cue.duration + self.VBAN_PLAYBACK_LATENCY_S
            )
            # Until it has left the send buffer (a response then queues after)
            await asyncio.sleep(cue.duration + 0.1)

    def _interrupt_long_cue(self) -> bool:
        """The response is here: a spoken announcement still playing is cut
        (a short sound is left to finish). True if one was cut."""
        cue = self._current_cue
        if not self._cue_playing() or cue is None or cue.duration <= self.CUE_FINISH_MAX_S:
            return False
        assert self._cue_task is not None
        self._cue_task.cancel()
        self._cue_queue.clear()
        self._mic_silence_until = 0.0
        if self._vban_sender:
            self._vban_sender.clear_pending()
        _LOGGER.debug("Announcement cut by the response")
        return True

    async def _wait_cue(self) -> None:
        if self._interrupt_long_cue():
            return  # the response replaces it on the media_player
        if self._cue_playing():
            assert self._cue_task is not None
            await asyncio.gather(self._cue_task, return_exceptions=True)

    # ------------------------------------------------------------------

    def _publish_exchange(self, response: str) -> None:
        """Fire a HA event with the user's request and the response.

        Only for a response following a transcript in the same run (not for
        announcements). Dropped if the HA WebSocket isn't connected.
        """
        transcript, self._last_transcript = self._last_transcript, ""
        if not transcript or self._exchange_publisher is None:
            return
        data = {
            "satellite": self._satellite_name,
            "transcript": transcript,
            "response": response,
            "followup": self._followup_active,
        }

        async def publish() -> None:
            assert self._exchange_publisher is not None
            if await self._exchange_publisher.fire_event(EXCHANGE_EVENT_TYPE, data):
                _LOGGER.debug("Published exchange: %s", data)
            else:
                _LOGGER.debug("HA WebSocket not connected, exchange not published")

        _spawn(publish())

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
        except Exception:  # noqa: BLE001 — the run must still end
            _LOGGER.exception("Response not played")
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
        await self._wait_cue()  # let a waiting sound finish (≤ 0.4 s)
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
            await self._send_played()
            return

        await self._wait_playback_end(url, duration, requested_at)

        # Margin for echo / the very end of the Cast output
        if self._mic_mute:
            await asyncio.sleep(self._mic_mute.tail_s)
            self._mic_mute.end_response(generation)

        await self._send_played()

    async def _send_played(self) -> None:
        """Tell HA the response finished playing, like a satellite with its
        own speaker would (entity goes back to idle, announcements stop
        waiting)."""
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
            _warn_timing_fallback("HA WebSocket not connected", entity_id)
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
                    f"{self.PLAY_START_TIMEOUT_S:.0f}s",
                    entity_id,
                )

        if started is None:
            estimated_end = requested_at + duration + self.CAST_START_DELAY_S
            await asyncio.sleep(max(0.0, estimated_end - time.monotonic()))
            return

        _LOGGER.debug("Cast playing after %.2fs", started.time - requested_at)
        # Over at the first of: the end reported, or "playing" + length +
        # margin (Cast can report the end seconds late, e.g. after reopening
        # its app: 9.4 s of "playing" for 4.1 s of audio)
        estimated_end = started.time + duration + self._media_output.end_margin_s

        def ended(c) -> bool:
            return c.state not in ("playing", "buffering") or not is_ours(c)

        finished = await watcher.wait_for(
            entity_id, ended, since=started.time,
            timeout=max(0.0, estimated_end - time.monotonic()),
        )
        if finished is None:
            # Not reported yet (or WebSocket lost): the estimated end
            await asyncio.sleep(max(0.0, estimated_end - time.monotonic()))
            # (seen only if nothing else is played meanwhile, e.g. no beep)
            self._watch_late_end(
                watcher, entity_id,
                lambda c: c.state not in ("playing", "buffering") and is_ours(c),
                started.time, estimated_end,
            )
            return

        _LOGGER.debug(
            "Cast finished after %.2fs (WAV %.2fs)",
            finished.time - started.time, duration,
        )

    def _watch_late_end(self, watcher, entity_id, ended, since: float, estimated_end: float) -> None:
        """Log how late the media player reports the end, to tune
        media_player.end_margin_ms."""

        async def watch() -> None:
            finished = await watcher.wait_for(entity_id, ended, since=since, timeout=60.0)
            if finished is not None and finished.time - estimated_end > 1.0:
                _LOGGER.info(
                    "Media player reported end %.1f s after the estimated end",
                    finished.time - estimated_end,
                )

        _spawn(watch())

    def _subscribe_to_vban(self) -> None:
        """Subscribe this handler to VBAN audio packets."""
        if not self._subscribed:
            self._vban_receiver.subscribe(self._on_vban_audio)
            self._subscribed = True

    async def _send_pipeline_and_stream(self, reason: str = "Home Assistant ready") -> None:
        """Send RunPipeline to HA, schedule audio streaming after a short delay.

        Must be called AFTER responding to Describe, so HA has the Info
        before it receives RunPipeline. Audio chunks must arrive after HA
        has processed RunPipeline (set _is_pipeline_running=True).

        The 300ms grace period is scheduled as a background task so we
        don't block the event reader while waiting.
        """
        # Marked first: a Describe or an error restart arriving while
        # RunPipeline is being written must not start a second run
        self._streaming = True
        try:
            await self._send_run_pipeline(PipelineStage.WAKE, reason)
        except BaseException:
            self._streaming = False
            raise

        # Schedule startup in the background, so handle_event can return
        # immediately and keep reading HA events.
        self._streaming_task = asyncio.create_task(
            self._delayed_stream_start(self.STREAM_GRACE_S)
        )

    async def _send_run_pipeline(self, start_stage: PipelineStage, reason: str = "") -> None:
        """Send RunPipeline (start_stage → TTS) and reset per-run state.

        Without follow-up, HA restarts the wake run itself (restart_on_end).
        """
        if self._driven and not self._followup.enabled and start_stage == PipelineStage.WAKE:
            # Follow-up turned off: from this wake run on, HA restarts the runs
            self._driven = False
            _LOGGER.info("Follow-up off: Home Assistant restarts the runs again")
        restart = not self._driven
        # The new run's state is set before the write: events read while it
        # is being sent belong to it, not to the previous run
        previous_seq = self._run_seq
        self._run_seq += 1
        self._clip = None  # a new exchange: the previous clip is complete
        # A wake run's detection timestamp counts from its first audio chunk
        self._wake_run = start_stage == PipelineStage.WAKE
        self._wake_sample0 = None
        self._wake_sample0_pending = self._wake_run
        self._awaiting_response = False
        self._last_transcript = ""
        self._stop_thinking()
        self._mic_paused = False
        self._mic_resume_at = time.monotonic() + self.STREAM_GRACE_S
        self._hold_grace_audio = start_stage == PipelineStage.ASR
        self._grace_audio = []
        if start_stage == PipelineStage.WAKE:  # no beep guard outside follow-ups
            self._mic_guard_until = 0.0
        try:
            await self.write_event(
                RunPipeline(
                    start_stage=start_stage,
                    end_stage=PipelineStage.TTS,
                    restart_on_end=restart,
                ).event()
            )
        except BaseException:
            self._run_seq = previous_seq  # not sent: no new run
            raise
        _LOGGER.info(
            "Sent run-pipeline: start=%s end=%s restart=%s (reason: %s)",
            start_stage.value, PipelineStage.TTS.value, restart, reason or "?",
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

    # Wake word clips

    def _capture_wake_clip(self, detection: Detection) -> None:
        """HA detected the wake word: keep what openWakeWord heard."""
        recorder = self._clip_recorder()
        if recorder is None or not self._wake_run or self._followup_active:
            return  # off, or a follow-up (started at ASR: no wake word)
        received = time.monotonic()
        sample, source = None, "latence"
        # The timestamp (ms of the run's audio) places it exactly, when this
        # satellite started the run (follow-up mode: first chunk known)
        if self._driven and detection.timestamp is not None and self._wake_sample0 is not None:
            candidate = self._wake_sample0 + int(detection.timestamp * WYOMING_RATE / 1000)
            if 0 <= recorder.samples_sent - candidate <= 3 * WYOMING_RATE:
                sample, source = candidate, "timestamp"
                recorder.learn_latency(candidate, received)
        if sample is None:
            sample = recorder.sample_at(received - recorder.latency_s)
        clip = {"id": None, "meta": {}}
        self._clip = clip
        _spawn(self._save_wake_clip(
            recorder, clip, sample, detection.name or "", source))

    async def _save_wake_clip(
        self, recorder: "ClipRecorder", clip: dict, sample: int, wake_word: str, source: str,
    ) -> None:
        from .wake_clips import AFTER_S
        # The 0.5 s after it are sent first (2 s at most: mic cut…)
        end = sample + int(AFTER_S * WYOMING_RATE)
        for _ in range(40):
            if recorder.samples_sent >= end:
                break
            await asyncio.sleep(0.05)
        try:
            meta = dict(clip["meta"])
            clip["id"] = await asyncio.to_thread(
                recorder.store.add, recorder.satellite, wake_word,
                recorder.extract(sample), detection_source=source, **meta,
            )
            if clip["meta"] != meta:  # completed while it was being written
                recorder.store.update(clip["id"], **clip["meta"])
            _LOGGER.debug("Wake word clip %s kept (%s)", clip["id"], source)
        except OSError as err:
            _LOGGER.error("Wake word clip not saved: %s", err)

    def _clip_note(self, close: bool = False, **fields) -> None:
        """What followed the detection, for its clip (until the response or
        an error: a later follow-up run is another exchange)."""
        clip = self._clip
        if clip is None:
            return
        clip["meta"].update(fields)
        recorder = self._clip_recorder()
        if clip["id"] is not None and recorder is not None:
            # Files written in a thread: not in the loop that streams the mic
            _spawn(asyncio.to_thread(
                _update_clip, recorder.store, clip["id"], fields))
        if close:
            self._clip = None

    @_as_satellite
    def followup_changed(self) -> None:
        """followup.enabled changed (applied without restarting the satellite).

        Off: the next wake run is left to HA (see _send_run_pipeline). On: HA
        restarts the runs itself and a run sent while one is active loses its
        audio, so this connection is closed once idle; HA reconnects and the
        new connection drives the runs.
        """
        if not self._followup.enabled or self._driven:
            return
        if self._switch_task and not self._switch_task.done():
            return
        self._switch_task = asyncio.create_task(self._reconnect_when_idle())

    async def _reconnect_when_idle(self) -> None:
        for _ in range(240):  # up to 2 minutes for an exchange to end
            if not self._work_pending():
                break
            await asyncio.sleep(0.5)
        if self._followup.enabled and not self._driven and self._is_running:
            _LOGGER.info("Follow-up on: reconnecting to Home Assistant to drive the runs")
            self.writer.close()

    @_as_satellite
    def on_assist_state(self, state: str) -> None:
        """State of this satellite's assist_satellite entity in HA (follow-up
        mode): back to idle after a transcript with no response coming
        means HA ended the run (empty response) — no need to wait for
        RESPONSE_TIMEOUT_S.

        Never used to close an open follow-up: HA also forces "idle" by
        itself about 2 s after a response should have finished playing
        (its TTS timeout), whatever run is going on then. Taken for the end
        of the follow-up, it made the app start a wake run that cancelled
        the listening (3.12.0). A follow-up ends with the end of speech, an
        error from HA, or timeout_s.
        """
        if not self._driven:
            return
        if self._followup_active and not self._mic_paused and not self._awaiting_response:
            if state == "idle":
                _LOGGER.info(
                    "Follow-up: Home Assistant shows idle while listening "
                    "(its TTS timeout?): the listening goes on",
                )
            return
        if not (self._awaiting_response and state == "idle"):
            return
        now = time.monotonic()
        if now - self._last_transcript_at < 0.5:
            return  # the state before the transcript, arriving late
        if now < self._ha_tts_idle_until:
            # Maybe HA's TTS timeout for the previous response: the
            # RESPONSE_TIMEOUT_S watchdog still ends a run with no response
            _LOGGER.debug("Idle within Home Assistant's TTS timeout window: not a run end")
            return
        _LOGGER.info("Home Assistant ended the run without a response")
        self._cancel_watchdog()
        self._awaiting_response = False
        self._last_transcript = ""
        self._stop_thinking()
        self._run_ended(self._run_seq, reason="Home Assistant ended it without a response")

    # Safety net: never left without listening

    IDLE_GUARD_S = 5.0  # no run (and nothing pending) this long: listen again
    IDLE_GUARD_CHECK_S = 1.0

    def _without_run(self) -> bool:
        """Connected, not muted, yet no run listens and nothing is under way
        that will start one."""
        if not (self._server_ready and self._is_running) or self.writer.is_closing():
            return False
        if not self._streaming:
            return not (self._restart_task and not self._restart_task.done())
        if not self._driven:
            return False  # HA restarts its runs itself
        if not self._mic_paused:
            return False  # a run is open
        return not self._work_pending()

    async def _idle_guard(self) -> None:
        since: Optional[float] = None
        try:
            while self._is_running:
                await asyncio.sleep(self.IDLE_GUARD_CHECK_S)
                if not self._without_run():
                    since = None
                    continue
                now = time.monotonic()
                since = since or now
                if now - since < self.IDLE_GUARD_S:
                    continue
                since = None
                _LOGGER.warning(
                    "No run for %.0f s while connected: listening for the wake word again",
                    self.IDLE_GUARD_S,
                )
                await self._listen_again()
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - the safety net must say it fell
            _LOGGER.exception("Listening safety net stopped")

    async def _listen_again(self) -> None:
        try:
            if not self._streaming:
                await self._send_pipeline_and_stream("safety net: not streaming")
                return
            self._cancel_followup_tasks()
            self._followup_count = 0
            await self._send_run_pipeline(PipelineStage.WAKE, "safety net: no run")
        except (ConnectionError, OSError) as err:
            _LOGGER.debug("Listening again failed: %s", err)

    def _on_speech_without_end(self) -> None:
        _LOGGER.warning(
            "Follow-up: no transcript %.0f s after speech started: run over",
            self.RESPONSE_TIMEOUT_S,
        )
        self._run_ended(self._run_seq, reason="follow-up: speech without transcript")

    def _on_response_timeout(self) -> None:
        _LOGGER.info("No response after transcript (empty response?): run over")
        self._run_ended(self._run_seq, reason="no response in time")

    def _on_followup_timeout(self) -> None:
        """No speech in time after a question: stop listening."""
        _LOGGER.info("Follow-up: no speech, back to wake word")
        self._followup_timed_out = True
        self._mic_paused = True
        # Ends the ASR run (normally with stt-no-text-recognized)
        _spawn(self._write_quietly(AudioStop().event()))
        seq = self._run_seq
        self._set_watchdog(
            self.FOLLOWUP_END_TIMEOUT_S, lambda: self._run_ended(seq, reason="follow-up: no speech"),
        )

    async def _write_quietly(self, event: Event) -> None:
        try:
            await self.write_event(event)
        except (ConnectionError, OSError):
            pass

    def _run_ended(self, seq: int, delay: float = 0.0, reason: str = "run over") -> None:
        """The run seq is over (response played, error or timeout).

        Starts the next run: a follow-up if the response was a question,
        otherwise wake word listening. Ignored if that run's end was
        already handled or a newer run has started.
        """
        if not self._driven or seq != self._run_seq or seq == self._run_ended_seq:
            return
        self._run_ended_seq = seq
        self._next_run_reason = reason
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
            # (follow-up turned off meanwhile: back to the wake word)
            expect = expect and self._followup.enabled
            if expect and self._followup_count < self.MAX_FOLLOWUPS:
                self._followup_count += 1
                _LOGGER.info(
                    "Follow-up: response is a question → listening without wake word"
                )
                # The ASR run starts when the listening really opens (beep
                # played, mic back): a run started with the beep got only
                # silence and HA ended it before the user could answer.
                # Until then no run is open: the mic audio isn't sent.
                self._followup_active = True
                self._followup_timed_out = False
                await self._open_after_beep()
                return

            if expect:
                _LOGGER.info(
                    "Follow-up: %d in a row, back to wake word", self._followup_count,
                )
            self._followup_count = 0
            await self._send_run_pipeline(
                PipelineStage.WAKE, f"previous run over: {self._next_run_reason}")
        except (ConnectionError, OSError):
            pass
        except Exception:  # noqa: BLE001 — never stay without a run
            _LOGGER.exception("Next run not started: back to the wake word")
            self._followup_active = False
            self._followup_timed_out = False
            self._followup_count = 0
            try:
                await self._send_run_pipeline(PipelineStage.WAKE, "after an internal error")
            except Exception:  # noqa: BLE001 — the idle guard tries again
                _LOGGER.exception("Wake run not started")

    async def _open_after_beep(self) -> None:
        """Play the beep, then open the listening once it has really played:
        its end (VBAN sent + latency, or the media_player's state), then the
        microphone stream back if the satellite cut it while playing, plus
        mic_tail_s. At most FOLLOWUP_OPEN_MAX_S after the beep."""
        start = time.monotonic()
        deadline = start + self.FOLLOWUP_OPEN_MAX_S
        played = await self._play_beep() if self._followup.beep else None
        capped = ""  # why the listening opened at the limit
        try:
            if played is not None:
                await asyncio.wait_for(played, max(0.0, deadline - time.monotonic()))
            if await self._wait_mic_back(deadline) is None:
                capped = "no microphone audio came back"
            else:
                tail = min(self._followup.mic_tail_s, max(0.0, deadline - time.monotonic()))
                await asyncio.sleep(tail)
        except asyncio.TimeoutError:
            capped = "beep end not seen"
        if capped:
            _LOGGER.warning(
                "Follow-up: %s within %.1fs of the beep, listening anyway",
                capped, self.FOLLOWUP_OPEN_MAX_S,
            )
        self._mic_guard_until = time.monotonic()  # what came before is dropped
        # Now HA listens too: its no-speech end and ours start together
        await self._send_run_pipeline(PipelineStage.ASR, "follow-up: listening opens")
        _LOGGER.info(
            "Follow-up: %s, mic back after %.2f s, listening for %g s",
            "beep played" if played is not None else "no beep",
            time.monotonic() - start, self._followup.timeout_s,
        )
        self._on_followup_listening()

    async def _play_beep(self):
        """Start the "I'm listening" beep on the satellite's output; returns
        an awaitable ending when it has played (None if none could play)."""
        latency = self._followup.beep_latency_s
        on_vban = self._followup.beep_output == "vban"
        if on_vban and self._vban_sender is None:
            if not self._beep_output_warned:
                _LOGGER.warning("followup.beep_output is vban but the VBAN output is off: "
                                "beep on the media player")
                self._beep_output_warned = True
            on_vban = False
        if self._media_output and not on_vban:
            rate = 22050
            url = self._media_output.publish(_beep_wav(rate, self.BEEP_SECONDS))
            requested_at = time.monotonic()
            if await self._media_output.play(url):
                if latency is None:
                    latency = self.BEEP_LATENCY_MEDIA_PLAYER_S
                return self._wait_beep_on_media_player(url, requested_at, latency)
        if self._vban_sender:
            self._vban_sender.reset_resampler()
            self._vban_sender.clear_pending()
            self._vban_sender.send(
                make_beep(WYOMING_RATE, self.BEEP_SECONDS), sample_rate=WYOMING_RATE,
            )
            if latency is None:
                latency = self.BEEP_LATENCY_VBAN_S
            return self._wait_beep_on_vban(latency)
        return None

    async def _wait_beep_on_vban(self, latency: float) -> None:
        assert self._vban_sender is not None
        await self._vban_sender.wait_sent()  # paced: sent ≈ played, but buffered
        await asyncio.sleep(latency)

    async def _wait_beep_on_media_player(
        self, url: str, requested_at: float, latency: float,
    ) -> None:
        """The media_player playing the beep, then not playing it any more;
        without its state, the beep's length plus latency from play_media."""
        assert self._media_output is not None
        watcher = self._media_output.state_watcher
        entity_id = self._media_output.entity_id
        file_name = url.rsplit("/", 1)[-1]
        estimated_end = requested_at + self.BEEP_SECONDS + latency

        def is_ours(change) -> bool:
            return file_name in str(change.attributes.get("media_content_id", url))

        if watcher is None or not watcher.connected:
            await asyncio.sleep(max(0.0, estimated_end - time.monotonic()))
            return
        started = await watcher.wait_for(
            entity_id, lambda c: c.state == "playing" and is_ours(c),
            since=requested_at, timeout=self.FOLLOWUP_OPEN_MAX_S,
        )
        if started is None:
            await asyncio.sleep(max(0.0, estimated_end - time.monotonic()))
            return
        # A Cast device may not report (in time) the end of such a short sound
        beep_end = started.time + self.BEEP_SECONDS + self._media_output.end_margin_s
        finished = await watcher.wait_for(
            entity_id, lambda c: c.state not in ("playing", "buffering") or not is_ours(c),
            since=started.time, timeout=max(0.0, beep_end - time.monotonic()),
        )
        if finished is None:
            await asyncio.sleep(max(0.0, beep_end - time.monotonic()))

    async def _wait_mic_back(self, deadline: float) -> Optional[bool]:
        """False if the microphone stream is flowing (or never started: nothing
        to wait for), True once it came back after a cut, None if it didn't by
        the deadline."""
        if not self._last_mic_at or time.monotonic() - self._last_mic_at <= self.MIC_GAP_S:
            return False
        self._mic_back.clear()
        try:
            await asyncio.wait_for(self._mic_back.wait(), max(0.0, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            return None
        return True

    def _on_followup_listening(self) -> None:
        """Listening open: the user's answer now reaches HA."""
        _LOGGER.debug("Follow-up: listening")
        self._set_watchdog(self._followup.timeout_s, self._on_followup_timeout)

    async def _wait_vban_played(self) -> None:
        """Return once the VBAN output has been sent and played."""
        assert self._vban_sender is not None
        await self._vban_sender.wait_sent()
        await asyncio.sleep(self.VBAN_PLAYBACK_LATENCY_S)

    async def _run_ended_after_vban(self, seq: Optional[int]) -> None:
        """VBAN output: once the audio has played, report played (and end
        the run, follow-up mode)."""
        await self._wait_vban_played()
        await self._send_played()
        if seq is not None:
            self._run_ended(seq, reason="response played (VBAN)")

    def _cancel_followup_tasks(self) -> None:
        self._cancel_watchdog()
        if self._next_run_task and not self._next_run_task.done():
            self._next_run_task.cancel()
        self._next_run_task = None
        self._followup_active = False
        self._followup_timed_out = False
        self._mic_guard_until = 0.0
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
            await asyncio.gather(self._streaming_task, return_exceptions=True)
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
            # A new wake run: a pending follow-up (or its watchdog) would
            # start a second one
            self._cancel_followup_tasks()
            await asyncio.sleep(delay)
            if self._server_ready and self._is_running and not self._streaming:
                _LOGGER.info("Restarting pipeline after server error")
                await self._send_pipeline_and_stream("after a server error")
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
        filter_error_logged = False
        silence = bytes(target_bytes)  # sent instead of the mic when needed
        debug = _LOGGER.isEnabledFor(logging.DEBUG)

        try:
            while self._streaming and self._is_running:
                try:
                    items = [await asyncio.wait_for(self._audio_queue.get(), timeout=2.0)]
                    # Take what else is waiting in one go (fewer wake-ups)
                    while not self._audio_queue.empty():
                        items.append(self._audio_queue.get_nowait())
                    # Oldest first: if the first one isn't early, none is
                    guard = self._mic_guard_until
                    pre_open = [t < guard for t, _ in items] if items[0][0] < guard else None
                    pcm = b"".join(audio for _, audio in items)
                except asyncio.TimeoutError:
                    timeouts += 1
                    had_timeout = True
                    if timeouts == 1 or timeouts % 5 == 0:
                        _LOGGER.warning(
                            "No %s audio received for %d seconds (chunks sent so far: %d)",
                            self._mic_label, timeouts * 2, chunks_sent,
                        )
                    continue

                # Reset queue-full flag once we've drained past the threshold
                if self._queue_full_logged and self._audio_queue.qsize() < 4:
                    self._queue_full_logged = False

                timeouts = 0
                if self._mic_filter is not None:
                    try:
                        pcm = self._mic_filter.process(pcm)
                    except Exception:  # noqa: BLE001 - raw audio rather than none
                        if not filter_error_logged:
                            _LOGGER.exception("Microphone clean-up failed: raw audio sent")
                            filter_error_logged = True
                if pre_open is not None and any(pre_open):
                    # Received before the follow-up listening opened (beep,
                    # end of the response): HA gets silence instead. Still
                    # processed above, so the clean-up keeps its state.
                    if all(pre_open):
                        pcm = bytes(len(pcm))
                    else:
                        cut = sum(len(a) for (_, a), early in zip(items, pre_open) if early)
                        cut = min(cut, len(pcm)) // WYOMING_WIDTH * WYOMING_WIDTH
                        pcm = bytes(cut) + pcm[cut:]
                    audio_buffer[:] = bytes(len(audio_buffer))  # older, kept for chunking
                audio_buffer.extend(pcm)
                recorder = self._clip_recorder()  # once per batch, not per chunk

                # Send when we have enough data
                while len(audio_buffer) >= target_bytes:
                    with memoryview(audio_buffer) as view:  # one copy
                        send_pcm = view[:target_bytes].tobytes()
                    del audio_buffer[:target_bytes]

                    # Between runs: don't send
                    if self._mic_paused:
                        continue

                    # Right after RunPipeline: let HA process it first. A
                    # follow-up keeps that audio (the answer may start right
                    # away) and sends it at the head of the run.
                    now = time.monotonic()
                    if now < self._mic_resume_at:
                        if self._hold_grace_audio:
                            self._grace_audio.append(send_pcm)
                        continue
                    if self._grace_audio:
                        held, self._grace_audio = self._grace_audio, []
                        for audio in held:
                            await self.write_event(AudioChunk(
                                rate=WYOMING_RATE, width=WYOMING_WIDTH,
                                channels=WYOMING_CHANNELS, audio=audio,
                            ).event())
                            if self._mirrors:
                                self._mirrors.mic_sent(audio)

                    # A waiting sound is playing: don't capture it
                    if now < self._mic_silence_until:
                        send_pcm = silence

                    chunks_sent += 1

                    # Level: only computed for the logs that show it
                    db = 0.0
                    if debug or chunks_sent == 1 or had_timeout:
                        db = level_db(send_pcm)
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
                            "%s audio resumed (chunk #%d, %.1f dB)",
                            self._mic_label, chunks_sent, db,
                        )
                        had_timeout = False
                    elif debug and chunks_sent % 50 == 0:
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
                            send_pcm = silence

                    chunk = AudioChunk(
                        rate=WYOMING_RATE,
                        width=WYOMING_WIDTH,
                        channels=WYOMING_CHANNELS,
                        audio=send_pcm,
                    )
                    await self.write_event(chunk.event())
                    if self._mirrors:
                        self._mirrors.mic_sent(send_pcm)
                    if recorder is not None:
                        if self._wake_sample0_pending:
                            self._wake_sample0 = recorder.samples_sent
                            self._wake_sample0_pending = False
                        recorder.sent(send_pcm)
        except (ConnectionError, asyncio.CancelledError, OSError):
            pass
        except Exception:  # noqa: BLE001 - never stay deaf on a live connection
            _LOGGER.exception("Audio streaming error: restarting the pipeline")
            if self._server_ready and self._is_running:
                self._schedule_pipeline_restart(1.0)
        finally:
            self._streaming = False
            try:
                if self._is_running:
                    await self.write_event(StreamingStopped().event())
            except (ConnectionError, OSError, asyncio.CancelledError):
                pass
            _LOGGER.info("Audio streaming stopped (total chunks sent: %d)", chunks_sent)

    def _on_vban_audio(self, packet: VbanPacket, pcm: bytes) -> None:
        """Microphone audio — enqueue it. pcm: the packet converted to
        16 kHz mono 16-bit (once per packet, by the satellite's MicFeed)."""
        now = time.monotonic()
        if now - self._last_mic_at > self.MIC_GAP_S:
            self._mic_back.set()  # the stream (re)starts
        self._last_mic_at = now
        if not self._first_packet_logged:
            # Called from the receiver: the log carries the satellite's name
            token = SATELLITE_NAME.set(self._satellite_name or SATELLITE_NAME.get())
            try:
                _LOGGER.info(
                    "First %s packet from stream '%s': %dHz, %dch, format=%d",
                    self._mic_label, packet.stream_name,
                    packet.sample_rate,
                    packet.channels,
                    packet.data_format,
                )
            finally:
                SATELLITE_NAME.reset(token)
            self._first_packet_logged = True

        if not self._streaming:
            return

        try:
            self._audio_queue.put_nowait((now, pcm))
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
        self._stop_thinking()
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
        for task in (self._resume_task, self._transcript_expiry, self._switch_task,
                     self._idle_guard_task):
            if task and not task.done():
                task.cancel()
        # HA closed its side: close ours (the socket would stay half open)
        self._is_running = False
        self.writer.close()


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
