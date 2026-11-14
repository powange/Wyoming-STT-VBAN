"""Follow-up without wake word: the satellite drives the pipeline runs.

The test plays Home Assistant's side of the Wyoming connection.
Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import asyncio
import logging
import socket
import struct
import time
import unittest
import unittest.mock
from typing import Optional

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.asr import Transcript
from wyoming.client import AsyncTcpClient
from wyoming.error import Error
from wyoming.event import Event
from wyoming.info import Describe
from wyoming.pipeline import RunPipeline
from wyoming.satellite import RunSatellite
from wyoming.server import AsyncServer
from wyoming.tts import Synthesize
from wyoming.vad import VoiceStarted

from wyoming_vban.satellite import (
    LISTEN_END_NOTES,
    SEARCH_NOTES,
    THINKING_NOTES,
    Announcement,
    Cue,
    FollowupSettings,
    VbanSatelliteHandler,
    WaitingCues,
    make_satellite_info,
    notify_search,
    notify_vision,
)
from wyoming_vban.vban import VbanPacket


def _free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeReceiver:
    """The satellite's microphone feed: packets converted once, like MicFeed."""

    callback = None

    def __init__(self) -> None:
        from wyoming_vban.vban import WyomingConverter
        self.converter = WyomingConverter()

    def subscribe(self, callback) -> None:
        self.callback = callback

    def feed(self, packet) -> None:
        self.callback(packet, self.converter.convert(packet))

    def unsubscribe(self, callback) -> None:
        pass


class FakeVbanSender:
    """Records what is sent; 'plays' instantly."""

    def __init__(self) -> None:
        self.sent: list[int] = []
        self.cleared = 0

    def reset_resampler(self) -> None:
        pass

    def clear_pending(self) -> None:
        self.cleared += 1

    def send(self, pcm, sample_rate=None, width=None, channels=None) -> None:
        self.sent.append(len(pcm))
        self.sent_at = getattr(self, "sent_at", []) + [time.monotonic()]

    async def wait_sent(self, max_wait: float = 300.0) -> None:
        pass

    def pending_seconds(self) -> float:
        return 0.0


class FakePublisher:
    """Records fired HA events."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.events: list[tuple[str, dict]] = []

    async def fire_event(self, event_type: str, event_data: dict) -> bool:
        if not self.connected:
            return False
        self.events.append((event_type, event_data))
        return True


class FollowupTest(unittest.IsolatedAsyncioTestCase):
    followup = FollowupSettings(enabled=True, beep=False, timeout_s=0.5)
    waiting_cues = WaitingCues()  # no waiting sounds

    async def asyncSetUp(self) -> None:
        self.sender = FakeVbanSender()
        self.receiver = FakeReceiver()
        self.publisher = FakePublisher()
        port = self.port = _free_tcp_port()
        self.server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")
        self.handlers: list[VbanSatelliteHandler] = []

        self.info = make_satellite_info("Bureau")

        def factory(reader, writer):
            handler = self.make_handler(reader, writer)
            self.handlers.append(handler)
            return handler

        await self.server.start(factory)
        self.ha = AsyncTcpClient("127.0.0.1", port)
        await self.ha.connect()
        # HA's connection sequence
        await self.ha.write_event(Describe().event())
        await self.ha.write_event(RunSatellite().event())
        await self.ha.write_event(Describe().event())

    def make_handler(self, reader, writer) -> VbanSatelliteHandler:
        return VbanSatelliteHandler(
            reader, writer, self.info, self.receiver, self.sender,
            followup=self.followup, exchange_publisher=self.publisher,
            satellite_name="Bureau", waiting_cues=self.waiting_cues,
        )

    async def asyncTearDown(self) -> None:
        await self.ha.disconnect()
        await self.server.stop()

    async def next_event(self, event_type: str, timeout: float = 3.0) -> Optional[Event]:
        """Next event of a type from the satellite (None on timeout)."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (remaining := deadline - loop.time()) > 0:
            try:
                event = await asyncio.wait_for(self.ha.read_event(), remaining)
            except asyncio.TimeoutError:
                return None
            if event is not None and event.type == event_type:
                return event
        return None

    async def connection_closed(self, timeout: float) -> bool:
        """The satellite closed the connection (HA would reconnect)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while (left := deadline - asyncio.get_running_loop().time()) > 0:
            try:
                event = await asyncio.wait_for(self.ha.read_event(), left)
            except asyncio.TimeoutError:
                return False
            except (ConnectionError, OSError):
                return True
            if event is None:
                return True
        return False

    async def reconnect(self) -> None:
        """HA connecting again, like after a reset."""
        await self.ha.disconnect()
        self.ha = AsyncTcpClient("127.0.0.1", self.port)
        await self.ha.connect()
        await self.ha.write_event(Describe().event())
        await self.ha.write_event(RunSatellite().event())
        await self.ha.write_event(Describe().event())

    async def next_run(self, timeout: float = 3.0) -> Optional[RunPipeline]:
        event = await self.next_event("run-pipeline", timeout)
        return RunPipeline.from_event(event) if event else None

    async def respond(self, text: str, transcript: str = "...") -> None:
        """A run's end as HA sends it: transcript, synthesize, TTS audio."""
        await self.ha.write_event(Transcript(text=transcript).event())
        await self.ha.write_event(Synthesize(text=text).event())
        await self.tts_audio()

    async def tts_audio(self) -> None:
        await self.ha.write_event(AudioStart(rate=22050, width=2, channels=1).event())
        await self.ha.write_event(
            AudioChunk(rate=22050, width=2, channels=1, audio=bytes(4410)).event()
        )
        await self.ha.write_event(AudioStop().event())

    async def test_question_then_answer_without_wake_word(self) -> None:
        run = await self.next_run()
        self.assertEqual((run.start_stage, run.restart_on_end), ("wake", False))

        await self.respond("Quelle lumière souhaitez-vous éteindre ?")
        run = await self.next_run()
        self.assertEqual((run.start_stage, run.end_stage), ("asr", "tts"))
        self.assertFalse(run.restart_on_end)

        await self.ha.write_event(VoiceStarted().event())
        await self.respond("Éteint.")
        run = await self.next_run()
        self.assertEqual(run.start_stage, "wake")

    async def test_statement_goes_back_to_wake_word(self) -> None:
        await self.next_run()
        await self.respond("Il est 9 heures.")
        run = await self.next_run()
        self.assertEqual(run.start_stage, "wake")

    async def test_no_speech_stops_and_returns_to_wake_word(self) -> None:
        await self.next_run()
        await self.respond("Quelle pièce ?")
        self.assertEqual((await self.next_run()).start_stage, "asr")

        # No voice-started: the satellite ends the ASR run with audio-stop
        self.assertIsNotNone(await self.next_event("audio-stop", timeout=2))
        await self.ha.write_event(
            Error(text="No text recognized", code="stt-no-text-recognized").event()
        )
        run = await self.next_run()
        self.assertEqual(run.start_stage, "wake")

    async def test_follow_up_turned_off_live(self) -> None:
        await self.next_run()
        self.followup.enabled = False
        try:
            self.handlers[-1].followup_changed()
            await self.respond("Quelle lumière ?")  # a question, but follow-up is off
            run = await self.next_run()
            self.assertEqual((run.start_stage, run.restart_on_end), ("wake", True))
        finally:
            self.followup.enabled = True  # shared by the class's tests

    async def test_empty_response_ends_the_run_from_the_ha_state(self) -> None:
        await self.next_run()
        await self.ha.write_event(Transcript(text="Allume").event())
        await asyncio.sleep(0.7)
        self.handlers[-1].on_assist_state("idle")  # HA: run over, no TTS
        run = await self.next_run(timeout=2)  # not after 60 s
        self.assertEqual(run.start_stage, "wake")

    async def test_question_with_closing_quote(self) -> None:
        await self.next_run()
        await self.respond("Vous voulez dire « laquelle ?»")
        self.assertEqual((await self.next_run()).start_stage, "asr")

    async def test_announcement_text_does_not_decide_the_follow_up(self) -> None:
        await self.next_run()
        await self.ha.write_event(Synthesize(text="Tu es là ?").event())  # no transcript
        await self.respond("Il est 9 heures.")
        self.assertEqual((await self.next_run()).start_stage, "wake")

    async def test_ha_idle_doesnt_close_the_follow_up(self) -> None:
        # HA forces "idle" ~2 s after a response should have played (its TTS
        # timeout), even during the follow-up: the listening goes on
        self.enterContext(unittest.mock.patch.object(self.followup, "timeout_s", 5.0))
        await self.next_run()
        await self.respond("Tu confirmes ?")
        self.assertEqual((await self.next_run()).start_stage, "asr")
        handler = self.handlers[-1]
        handler.on_assist_state("listening")
        with self.assertLogs("wyoming_vban.satellite", "INFO") as logs:
            handler.on_assist_state("idle")
            self.assertIsNone(await self.next_run(timeout=0.3))  # no wake run
        self.assertTrue(any("listening goes on" in line for line in logs.output))
        # The answer is still taken
        await self.ha.write_event(VoiceStarted().event())
        await self.respond("C'est fait.", transcript="Je confirme")
        self.assertEqual((await self.next_run()).start_stage, "wake")
        self.assertEqual(self.publisher.events[-1][1]["transcript"], "Je confirme")

    async def test_ha_error_ends_the_follow_up_run(self) -> None:
        await self.next_run()
        await self.respond("Quelle pièce ?")
        self.assertEqual((await self.next_run()).start_stage, "asr")
        with self.assertLogs("wyoming_vban.satellite", "INFO") as logs:
            await self.ha.write_event(Error(text="stream failed", code="stt-stream-failed").event())
            run = await self.next_run(timeout=1.0)
        self.assertEqual(run.start_stage, "wake")
        self.assertTrue(any("Home Assistant ended the run while listening (error" in line
                            for line in logs.output))
        self.assertTrue(any("reason: previous run over: error stt-stream-failed" in line
                            for line in logs.output))

    async def test_follow_up_right_after_ha_reconnects(self) -> None:
        await self.next_run()
        await self.ha.disconnect()  # e.g. the app restarted, HA reconnects
        await asyncio.sleep(0.2)
        self.ha = AsyncTcpClient("127.0.0.1", self.port)
        await self.ha.connect()
        for event in (Describe(), RunSatellite(), Describe()):
            await self.ha.write_event(event.event())
        run = await self.next_run()
        self.assertEqual((run.start_stage, run.restart_on_end), ("wake", False))
        await self.respond("Quelle lumière ?")
        self.assertEqual((await self.next_run()).start_stage, "asr")
        self.assertIsNone(await self.next_run(timeout=self.followup.timeout_s * 0.8))

    async def test_logs_from_ha_calls_carry_the_satellite(self) -> None:
        from wyoming_vban.satellite import SATELLITE_NAME
        names = []

        class Capture(logging.Filter):
            def filter(self, record):
                names.append(SATELLITE_NAME.get())
                return True

        self.enterContext(unittest.mock.patch.object(self.followup, "timeout_s", 5.0))
        await self.next_run()
        await self.respond("Tu confirmes ?")
        await self.next_run()
        capture = Capture()
        logger = logging.getLogger("wyoming_vban.satellite")
        level = logger.level
        logger.setLevel(logging.INFO)
        logger.addFilter(capture)
        try:
            self.handlers[-1].on_assist_state("idle")  # called from the HA WebSocket
        finally:
            logger.removeFilter(capture)
            logger.setLevel(level)
        self.assertEqual(names, ["Bureau"])

    async def test_no_run_for_5_s_listens_again(self) -> None:
        await self.next_run()
        handler = self.handlers[-1]
        handler._mic_paused = True  # a run ended and nothing started the next one
        with unittest.mock.patch.multiple(
            VbanSatelliteHandler, IDLE_GUARD_S=1.0, IDLE_GUARD_CHECK_S=0.2,
        ), self.assertLogs("wyoming_vban.satellite", "WARNING") as logs:
            run = await self.next_run(timeout=3.0)
        self.assertIsNotNone(run)
        self.assertEqual(run.start_stage, "wake")
        self.assertTrue(any("No run for" in line for line in logs.output))

    async def test_speech_then_nothing_from_ha_ends_the_run(self) -> None:
        # The user speaks, then HA never answers: a long watchdog (not the
        # no-speech one) gives up and resets the connection (HA's run may
        # still go on: a new run on this connection would be left deaf)
        self.enterContext(unittest.mock.patch.object(
            VbanSatelliteHandler, "RESPONSE_TIMEOUT_S", 0.8))
        await self.next_run()
        await self.respond("Quelle pièce ?")
        self.assertEqual((await self.next_run()).start_stage, "asr")
        with self.assertLogs("wyoming_vban.satellite", "WARNING") as logs:
            await self.ha.write_event(VoiceStarted().event())
            closed = await self.connection_closed(timeout=2.0)
        self.assertTrue(closed)
        self.assertTrue(any("no transcript" in line for line in logs.output))
        self.assertTrue(any("Satellite reset after follow-up: speech without transcript" in line
                            for line in logs.output))

    async def test_at_most_three_follow_ups(self) -> None:
        await self.next_run()
        stages = []
        for _ in range(4):
            await self.respond("Encore une question ?")
            run = await self.next_run()
            stages.append(run.start_stage)
            await self.ha.write_event(VoiceStarted().event())
        self.assertEqual(stages, ["asr", "asr", "asr", "wake"])

    async def test_error_restarts_wake_run(self) -> None:
        await self.next_run()
        await self.ha.write_event(
            Error(text="dup", code="duplicate_wake_up_detected").event()
        )
        run = await self.next_run()
        self.assertEqual(run.start_stage, "wake")

    async def test_announcement_does_not_start_a_run(self) -> None:
        await self.next_run()
        # TTS audio without a transcript: an announcement during the wake run
        await self.tts_audio()
        self.assertIsNone(await self.next_run(timeout=1.0))
        self.assertEqual(self.publisher.events, [])  # nor an exchange event

    async def test_exchanges_published(self) -> None:
        await self.next_run()
        await self.respond("Quelle lumière ?", transcript="Éteins la lumière")
        await self.next_run()
        await self.ha.write_event(VoiceStarted().event())
        await self.respond("Éteint.", transcript="Celle du bureau")
        await self.next_run()
        self.assertEqual(self.publisher.events, [
            ("jarvis_echange", {
                "satellite": "Bureau", "transcript": "Éteins la lumière",
                "response": "Quelle lumière ?", "followup": False,
            }),
            ("jarvis_echange", {
                "satellite": "Bureau", "transcript": "Celle du bureau",
                "response": "Éteint.", "followup": self.followup.enabled,
            }),
        ])

    async def test_exchange_dropped_when_not_connected(self) -> None:
        self.publisher.connected = False
        await self.next_run()
        await self.respond("Il est 9 heures.", transcript="Quelle heure est-il")
        run = await self.next_run()
        self.assertEqual(run.start_stage, "wake")
        self.assertEqual(self.publisher.events, [])

    async def test_question_reply_is_played_before_next_run(self) -> None:
        await self.next_run()
        await self.respond("Quelle lumière ?")
        await self.next_run()
        self.assertEqual(len(self.sender.sent), 1)  # the response went to VBAN


class FollowupBeepTest(FollowupTest):
    followup = FollowupSettings(enabled=True, beep=True, timeout_s=0.5)
    GUARD = VbanSatelliteHandler.BEEP_SECONDS + VbanSatelliteHandler.VBAN_PLAYBACK_LATENCY_S

    async def test_question_reply_is_played_before_next_run(self) -> None:
        await self.next_run()
        await self.respond("Quelle lumière ?")
        await self.next_run()
        await asyncio.sleep(0.1)
        # The response, then the 150 ms beep (16 kHz, 16-bit), sent with the run
        self.assertEqual(self.sender.sent, [4410, int(16000 * 0.15) * 2])

    async def _mic(self, seconds: float, delay: float = 0.0) -> None:
        """Loud microphone audio, in real time (after delay)."""
        await asyncio.sleep(delay)
        for i in range(int(seconds / 0.016)):
            self.receiver.feed(VbanPacket(
                sample_rate=16000, samples_per_frame=256, channels=1,
                data_format=1, codec=0, stream_name="AtomBureau",
                frame_counter=i, payload=b"\x00\x40" * 256,
            ))
            await asyncio.sleep(0.016)

    async def _asr_run(self, seconds: float, mic=None) -> tuple[float, list, int]:
        """Question answered, then the follow-up's ASR run. Returns (time
        from the response to the ASR run, [(time from the run, loud chunk or
        "audio-stop")], loud chunks sent before the run). mic() starts with
        the response."""
        await self.next_run()
        await self.respond("Quelle lumière ?")
        t_resp = time.monotonic()
        feed = asyncio.create_task(mic()) if mic else None
        early_loud = 0
        while True:  # until the ASR run: nothing of the mic may reach HA
            event = await asyncio.wait_for(self.ha.read_event(), 8)
            # (chunks of the wake run still queued are read right away)
            late = time.monotonic() - t_resp > 0.2
            if event.type == "audio-chunk" and any(event.payload) and late:
                early_loud += 1
            if event.type == "run-pipeline":
                self.assertEqual(RunPipeline.from_event(event).start_stage, "asr")
                break
        t_run = time.monotonic()
        events = []
        while (left := t_run + seconds - time.monotonic()) > 0:
            try:
                event = await asyncio.wait_for(self.ha.read_event(), left)
            except asyncio.TimeoutError:
                break
            if event.type == "audio-chunk":
                events.append((time.monotonic() - t_run, any(event.payload)))
            elif event.type == "audio-stop":
                events.append((time.monotonic() - t_run, "audio-stop"))
        if feed:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        return t_run - t_resp, events, early_loud

    def _stop_time(self, events) -> float:
        stops = [t for t, kind in events if kind == "audio-stop"]
        self.assertTrue(stops)
        return stops[0]

    async def test_error_while_waiting_for_the_beep(self) -> None:
        await self.next_run()
        await self.respond("Quelle pièce ?")
        # Response played (0.4 s), then beep + 0.4 s + 0.25 s: no run open
        await asyncio.sleep(0.6)
        with self.assertLogs("wyoming_vban.satellite", "INFO") as logs:
            await self.ha.write_event(Error(text="late", code="stt-no-text-recognized").event())
            run = await self.next_run()
        self.assertEqual(run.start_stage, "asr")
        self.assertTrue(any("while no run is open: ignored" in line for line in logs.output))
        handler = self.handlers[-1]
        self.assertFalse(handler._followup_timed_out)
        # Speaking now isn't cut at timeout_s
        await self.ha.write_event(VoiceStarted().event())
        self.assertIsNone(await self.next_event("audio-stop", timeout=self.followup.timeout_s + 0.4))

    async def test_mic_cut_then_back_2_4_s_after_the_response(self) -> None:
        # The mic flows, then the satellite cuts it while playing (like an Atom)
        await self._mic(0.3)
        waited, events, early = await self._asr_run(
            2.0, lambda: self._mic(3.0, delay=2.4))
        # HA gets the ASR run only once the mic is back (+ tail): no beep, no
        # end of the response before it
        self.assertGreaterEqual(waited, 2.4 + self.followup.mic_tail_s - 0.1)
        self.assertEqual(early, 0)
        self.assertTrue(any(kind is True for _, kind in events))
        # The no-speech timeout counts from the run, for HA and for us
        self.assertGreaterEqual(self._stop_time(events), self.followup.timeout_s - 0.1)

    async def test_mic_never_cut(self) -> None:
        mic = asyncio.create_task(self._mic(6.0))  # flows all along
        try:
            waited, events, early = await self._asr_run(1.0)
        finally:
            mic.cancel()
            await asyncio.gather(mic, return_exceptions=True)
        # Response played (VBAN latency), beep + latency + tail, then the run
        opened = (VbanSatelliteHandler.VBAN_PLAYBACK_LATENCY_S
                  + VbanSatelliteHandler.BEEP_LATENCY_VBAN_S + self.followup.mic_tail_s)
        self.assertGreaterEqual(waited, opened - 0.1)
        self.assertLess(waited, opened + 0.5)
        self.assertEqual(early, 0)
        loud = [t for t, kind in events if kind is True]
        self.assertTrue(loud)
        self.assertLess(min(loud), 0.5)  # the opening's audio, held during HA's grace

    async def test_mic_never_back_opens_at_the_limit(self) -> None:
        await self._mic(0.3)
        with unittest.mock.patch.object(VbanSatelliteHandler, "FOLLOWUP_OPEN_MAX_S", 1.5):
            with self.assertLogs("wyoming_vban.satellite", "WARNING") as logs:
                waited, events, _ = await self._asr_run(1.5)
        self.assertTrue(any("listening anyway" in line for line in logs.output))
        self.assertGreaterEqual(waited, 1.5)
        self.assertGreaterEqual(self._stop_time(events), self.followup.timeout_s - 0.1)


class FakeMediaOutput:
    """A Cast speaker without state over HA: timings from the WAV length."""

    entity_id = "media_player.nest"
    state_watcher = None
    end_margin_s = 0.6

    def __init__(self) -> None:
        self.published: list[float] = []  # time each WAV was published

    def publish(self, wav: bytes) -> str:
        self.published.append(time.monotonic())
        return f"http://127.0.0.1/tts/{len(self.published)}.wav"

    async def play(self, url: str) -> bool:
        return True


class FollowupMediaPlayerTest(FollowupTest):
    """Follow-up on a media player with 1.5 s of playback latency."""

    followup = FollowupSettings(enabled=True, beep=True, timeout_s=1.0, beep_latency_s=1.5)

    async def next_run(self, timeout: float = 8.0):
        # (each playback waits for its length plus 1.5 s)
        return await super().next_run(timeout)

    def make_handler(self, reader, writer) -> VbanSatelliteHandler:
        self.media = getattr(self, "media", None) or FakeMediaOutput()
        return VbanSatelliteHandler(
            reader, writer, self.info, self.receiver, None, media_output=self.media,
            followup=self.followup, exchange_publisher=self.publisher,
            satellite_name="Bureau", waiting_cues=self.waiting_cues,
        )

    async def test_playback_error_still_ends_the_run(self) -> None:
        await self.next_run()

        def broken(wav: bytes) -> str:
            raise RuntimeError("disque plein")

        self.media.publish = broken
        with self.assertLogs("wyoming_vban.satellite", "ERROR"):
            await self.respond("Il est 9 heures.")
            # At once, not after the idle guard (5 s)
            run = await self.next_run(timeout=3)
        self.assertEqual(run.start_stage, "wake")

    async def test_asr_run_only_after_the_beep(self) -> None:
        await self.next_run()
        await self.respond("Quelle lumière ?")
        run = await self.next_run(timeout=8)
        t_run = time.monotonic()
        self.assertEqual(run.start_stage, "asr")
        # Response then beep published; the run only once the beep has played
        self.assertEqual(len(self.media.published), 2)
        beep_at = self.media.published[1]
        self.assertGreaterEqual(
            t_run - beep_at,
            VbanSatelliteHandler.BEEP_SECONDS + 1.5 + self.followup.mic_tail_s - 0.1,
        )
        # The user has the whole timeout_s to answer, counted from the run
        self.assertIsNotNone(await self.next_event("audio-stop", timeout=3))
        self.assertGreaterEqual(time.monotonic() - t_run, self.followup.timeout_s - 0.1)

    # (the base class's VBAN-output tests don't apply here)
    async def test_question_reply_is_played_before_next_run(self) -> None:
        pass

    async def test_announcement_does_not_start_a_run(self) -> None:
        pass


class FakeCastWatcher:
    """A Nest Mini's state over HA: "playing" 0.1 s after play_media, then
    "idle" once the WAV has played, plus late_s (Cast reporting it late)."""

    connected = True

    def __init__(self, late_s: float) -> None:
        self.late_s = late_s
        self.changes: list = []

    def played(self, url: str, duration: float) -> None:
        from wyoming_vban.ha_ws import StateChange
        start = time.monotonic() + 0.1
        self.changes.append(StateChange(start, "playing", {"media_content_id": url}))
        self.changes.append(StateChange(start + duration + self.late_s, "idle",
                                        {"media_content_id": url}))

    async def wait_for(self, entity_id, predicate, since, timeout):
        deadline = time.monotonic() + timeout
        while True:
            now = time.monotonic()
            for change in self.changes:
                if since <= change.time <= now and predicate(change):
                    return change
            if now >= deadline:
                return None
            await asyncio.sleep(0.02)


class FakeCastOutput(FakeMediaOutput):
    def __init__(self, late_s: float) -> None:
        super().__init__()
        self.state_watcher = FakeCastWatcher(late_s)
        self._durations: dict[str, float] = {}

    def publish(self, wav: bytes) -> str:
        url = super().publish(wav)
        rate = struct.unpack("<I", wav[24:28])[0]
        self._durations[url] = (len(wav) - 44) / (rate * 2)
        return url

    async def play(self, url: str) -> bool:
        self.state_watcher.played(url, self._durations[url])
        return True


class CastEndTest(FollowupTest):
    """The end of a response on a Cast speaker reporting it late or on time."""

    followup = FollowupSettings(enabled=True, beep=True, timeout_s=1.0)

    async def next_run(self, timeout: float = 8.0):
        return await super().next_run(timeout)

    def make_handler(self, reader, writer) -> VbanSatelliteHandler:
        self.media = getattr(self, "media", None) or FakeCastOutput(late_s=0.0)
        return VbanSatelliteHandler(
            reader, writer, self.info, self.receiver, None, media_output=self.media,
            followup=self.followup, exchange_publisher=self.publisher,
            satellite_name="Bureau", waiting_cues=self.waiting_cues,
        )

    async def beep_delay(self) -> float:
        """From the response's "playing" to the beep's play_media."""
        await self.next_run()
        await self.respond("Quelle lumière ?")  # 0.1 s of audio
        self.assertIsNotNone(await self.next_run(timeout=8))
        playing = self.media.state_watcher.changes[0].time
        return self.media.published[1] - playing

    async def test_nest_reporting_the_end_on_time(self) -> None:
        # Unchanged: the beep follows the reported end (0.1 s), before the estimate
        self.assertLess(await self.beep_delay(), 0.1 + 0.6)

    async def test_nest_reporting_the_end_5_s_late(self) -> None:
        self.media.state_watcher.late_s = 5.0
        delay = await self.beep_delay()
        # At the estimated end ("playing" + 0.1 s + 0.6 s), not 5 s later
        self.assertGreaterEqual(delay, 0.1 + 0.6 - 0.05)
        self.assertLess(delay, 1.5)

    async def test_follow_up_stays_open_while_the_nest_shows_playing(self) -> None:
        # The beep shown "playing" for 30 s, and HA's TTS timeout forcing
        # "idle" during the follow-up (15:02 on 12/11)
        self.media.state_watcher.late_s = 30.0
        await self.next_run()
        await self.respond("Tu confirmes ?")
        self.assertEqual((await self.next_run()).start_stage, "asr")
        handler = self.handlers[-1]
        handler.on_assist_state("listening")
        handler.on_assist_state("idle")
        self.assertIsNone(await self.next_run(timeout=self.followup.timeout_s * 0.8))
        await self.ha.write_event(VoiceStarted().event())
        await self.respond("C'est fait.", transcript="Je confirme")
        await self.next_run()  # the next run, once the answer's response has played
        self.assertEqual(self.publisher.events[-1][1]["transcript"], "Je confirme")

    async def test_late_end_is_logged(self) -> None:
        self.media.state_watcher.late_s = 2.5
        await self.next_run()
        with self.assertLogs("wyoming_vban.satellite", "INFO") as logs:
            await self.respond("Il est 9 heures.")  # no beep after it
            run = await self.next_run()
            self.assertEqual(run.start_stage, "wake")
            await asyncio.sleep(2.5)
        self.assertTrue(any("Media player reported end 1.8 s after the estimated end" in line
                            or "Media player reported end 1.9 s after the estimated end" in line
                            for line in logs.output), logs.output)

    # (the base class's VBAN-output tests don't apply here)
    async def test_question_reply_is_played_before_next_run(self) -> None:
        pass

    async def test_announcement_does_not_start_a_run(self) -> None:
        pass


class BeepOnVbanTest(FollowupTest):
    """followup.beep_output: vban, with the responses on a media player."""

    followup = FollowupSettings(enabled=True, beep=True, timeout_s=1.0, beep_output="vban")

    def make_handler(self, reader, writer) -> VbanSatelliteHandler:
        self.media = getattr(self, "media", None) or FakeMediaOutput()
        return VbanSatelliteHandler(
            reader, writer, self.info, self.receiver, self.sender, media_output=self.media,
            followup=self.followup, exchange_publisher=self.publisher,
            satellite_name="Bureau", waiting_cues=self.waiting_cues,
        )

    async def next_run(self, timeout: float = 8.0):
        return await super().next_run(timeout)

    async def test_beep_on_vban_then_listening(self) -> None:
        await self.next_run()
        await self.respond("Quelle lumière ?")
        run = await self.next_run()
        t_run = time.monotonic()
        self.assertEqual(run.start_stage, "asr")
        self.assertEqual(len(self.media.published), 1)  # the response only
        self.assertEqual(self.sender.sent, [int(16000 * 0.15) * 2])  # the beep, on VBAN
        # Listening once the VBAN beep has played (+ latency + tail)
        self.assertGreaterEqual(
            t_run - self.sender.sent_at[0],
            VbanSatelliteHandler.BEEP_LATENCY_VBAN_S + self.followup.mic_tail_s - 0.1,
        )

    async def test_question_reply_is_played_before_next_run(self) -> None:
        pass

    async def test_announcement_does_not_start_a_run(self) -> None:
        pass


THINKING = Cue.tones(THINKING_NOTES, 0.15)
SEARCH = Cue.tones(SEARCH_NOTES, 0.2)
BLIP = Cue.tones(LISTEN_END_NOTES, 0.12)


class FakePhrase:
    """A synthesized phrase (TtsPhrase stand-in); distinct lengths."""

    def __init__(self, text: str, seconds: float, synthesized: bool = True):
        self.text = text
        self.cue = Cue(bytes(2 * int(22050 * seconds)), 22050) if synthesized else None


THINK_PHRASE = FakePhrase("Je réfléchis.", 0.3)
SEARCH_PHRASE = FakePhrase("Je cherche sur internet.", 0.32)
VISION_PHRASE = FakePhrase("Attends, j'observe.", 0.34)


def size(item) -> int:
    return len(item.cue.pcm if isinstance(item, FakePhrase) else item.pcm)


class AnnouncementTest(unittest.IsolatedAsyncioTestCase):
    """Announcements between transcript and response (VBAN output)."""

    async def start(self, cues: WaitingCues) -> None:
        self.sender = FakeVbanSender()
        port = _free_tcp_port()
        self.server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")
        await self.server.start(
            lambda reader, writer: VbanSatelliteHandler(
                reader, writer, make_satellite_info("Bureau"), FakeReceiver(),
                self.sender, followup=FollowupSettings(enabled=True, beep=False),
                waiting_cues=cues,
            )
        )
        self.ha = AsyncTcpClient("127.0.0.1", port)
        await self.ha.connect()
        for event in (Describe(), RunSatellite(), Describe()):
            await self.ha.write_event(event.event())
        await asyncio.sleep(0.2)

    async def asyncTearDown(self) -> None:
        await self.ha.disconnect()
        await self.server.stop()

    async def transcript(self) -> None:
        await self.ha.write_event(Transcript(text="Raconte une blague").event())
        await asyncio.sleep(0.05)

    async def response(self) -> None:
        await self.ha.write_event(Synthesize(text="Voici une blague.").event())
        await self.ha.write_event(AudioStart(rate=22050, width=2, channels=1).event())
        await asyncio.sleep(0.05)

    def assertSent(self, *items) -> None:
        self.assertEqual(self.sender.sent, [size(item) for item in items])

    # Modes

    async def test_none_plays_nothing(self) -> None:
        await self.start(WaitingCues(thinking_delay_s=0.2))
        await self.transcript()
        await asyncio.sleep(0.3)
        notify_search({"question": "x"})
        notify_vision({"question": "x", "piece": "bureau"})
        await asyncio.sleep(0.3)
        self.assertSent()

    async def test_listen_end_blip_right_away(self) -> None:
        await self.start(WaitingCues(listen_end=Announcement(sound=BLIP)))
        await self.transcript()
        self.assertSent(BLIP)

    async def test_fast_response_plays_nothing(self) -> None:
        await self.start(WaitingCues(
            thinking=Announcement(phrase=THINK_PHRASE), thinking_delay_s=0.3,
        ))
        await self.transcript()
        await asyncio.sleep(0.1)
        await self.response()
        await asyncio.sleep(0.4)
        self.assertSent()

    async def test_thinking_modes(self) -> None:
        for announcement, expected in (
            (Announcement(sound=THINKING), [THINKING]),
            (Announcement(phrase=THINK_PHRASE), [THINK_PHRASE]),
            (Announcement(sound=THINKING, phrase=THINK_PHRASE), [THINKING, THINK_PHRASE]),
        ):
            with self.subTest(expected=len(expected)):
                await self.start(WaitingCues(thinking=announcement, thinking_delay_s=0.2))
                await self.transcript()
                await asyncio.sleep(0.9)
                self.assertSent(*expected)
                await self.asyncTearDown()

    async def test_phrase_not_synthesized_falls_back_to_sound(self) -> None:
        await self.start(WaitingCues(
            thinking=Announcement(
                phrase=FakePhrase("Je réfléchis.", 0.3, synthesized=False),
                fallback=THINKING,
            ),
            thinking_delay_s=0.2,
        ))
        await self.transcript()
        await asyncio.sleep(0.5)
        self.assertSent(THINKING)

    # Priorities

    async def test_vision_before_thinking_delay_replaces_it(self) -> None:
        await self.start(WaitingCues(
            thinking=Announcement(phrase=THINK_PHRASE), thinking_delay_s=0.3,
            vision=Announcement(phrase=VISION_PHRASE),
        ))
        await self.transcript()
        notify_vision({"question": "assis ou debout ?", "piece": "bureau"})
        await asyncio.sleep(0.9)
        self.assertSent(VISION_PHRASE)

    async def test_thinking_playing_then_vision(self) -> None:
        await self.start(WaitingCues(
            thinking=Announcement(phrase=THINK_PHRASE), thinking_delay_s=0.2,
            vision=Announcement(phrase=VISION_PHRASE),
        ))
        await self.transcript()
        await asyncio.sleep(0.25)  # thinking phrase playing
        notify_vision({"question": "x", "piece": "bureau"})
        notify_vision({"question": "x", "piece": "bureau"})  # at most once
        await asyncio.sleep(0.9)
        self.assertSent(THINK_PHRASE, VISION_PHRASE)

    async def test_vision_drops_thinking_phrase_not_started(self) -> None:
        await self.start(WaitingCues(
            thinking=Announcement(sound=THINKING, phrase=THINK_PHRASE),
            thinking_delay_s=0.2, vision=Announcement(phrase=VISION_PHRASE),
        ))
        await self.transcript()
        await asyncio.sleep(0.25)  # thinking sound playing, phrase queued
        notify_vision({"question": "x", "piece": "bureau"})
        await asyncio.sleep(0.9)
        self.assertSent(THINKING, VISION_PHRASE)

    async def test_thinking_then_search_sound_then_phrase(self) -> None:
        await self.start(WaitingCues(
            thinking=Announcement(phrase=THINK_PHRASE), thinking_delay_s=0.2,
            search=Announcement(sound=SEARCH, phrase=SEARCH_PHRASE),
        ))
        await self.transcript()
        await asyncio.sleep(0.25)
        notify_search({"question": "actualités"})
        notify_search({"question": "actualités"})  # at most once
        await asyncio.sleep(1.2)
        self.assertSent(THINK_PHRASE, SEARCH, SEARCH_PHRASE)

    async def test_search_ignored_when_not_thinking(self) -> None:
        await self.start(WaitingCues(search=Announcement(sound=SEARCH)))
        notify_search({"question": "actualités"})
        await asyncio.sleep(0.1)
        self.assertSent()

    async def test_response_lets_announcement_finish_drops_pending(self) -> None:
        await self.start(WaitingCues(
            thinking=Announcement(sound=THINKING, phrase=THINK_PHRASE),
            thinking_delay_s=0.2,
        ))
        await self.transcript()
        await asyncio.sleep(0.25)  # thinking sound playing, phrase queued
        cleared = self.sender.cleared
        await self.response()
        await asyncio.sleep(0.6)
        # The sound isn't cut (VBAN buffer not cleared), the phrase is dropped
        self.assertEqual(self.sender.cleared, cleared)
        self.assertSent(THINKING)


    async def test_response_cuts_a_long_spoken_announcement(self) -> None:
        long_phrase = FakePhrase("Je cherche sur internet, un instant s'il te plaît.", 3.0)
        await self.start(WaitingCues(
            thinking=Announcement(phrase=long_phrase), thinking_delay_s=0.2,
        ))
        await self.transcript()
        await asyncio.sleep(0.4)  # the 3 s phrase is playing
        cleared = self.sender.cleared
        await self.response()
        await asyncio.sleep(0.2)
        # Cut (VBAN buffer cleared) instead of making the response wait 3 s
        self.assertGreater(self.sender.cleared, cleared)


class FollowupDisabledTest(FollowupTest):
    followup = FollowupSettings(enabled=False)

    async def test_question_then_answer_without_wake_word(self) -> None:
        # HA restarts the wake run itself; the satellite sends nothing more
        run = await self.next_run()
        self.assertEqual((run.start_stage, run.restart_on_end), ("wake", True))
        await self.respond("Quelle lumière ?")
        self.assertIsNone(await self.next_run(timeout=1.0))

    async def test_statement_goes_back_to_wake_word(self) -> None:
        pass

    async def test_no_speech_stops_and_returns_to_wake_word(self) -> None:
        pass

    async def test_at_most_three_follow_ups(self) -> None:
        pass

    async def test_announcement_does_not_start_a_run(self) -> None:
        pass

    async def test_question_reply_is_played_before_next_run(self) -> None:
        pass

    async def test_exchanges_published(self) -> None:
        # HA restarts the runs itself: just check the exchange is published
        await self.next_run()
        await self.respond("Allumé.", transcript="Allume la lumière")
        await asyncio.sleep(0.2)
        self.assertEqual(self.publisher.events, [("jarvis_echange", {
            "satellite": "Bureau", "transcript": "Allume la lumière",
            "response": "Allumé.", "followup": False,
        })])

    async def test_exchange_dropped_when_not_connected(self) -> None:
        pass

    async def test_empty_response_ends_the_run_from_the_ha_state(self) -> None:
        pass

    async def test_follow_up_turned_off_live(self) -> None:
        pass

    async def test_follow_up_turned_on_live_reconnects(self) -> None:
        await self.next_run()
        self.followup.enabled = True
        try:
            self.handlers[-1].followup_changed()  # idle: closed at once
            await asyncio.sleep(1.0)
            self.assertFalse(self.handlers[-1]._is_running)
        finally:
            self.followup.enabled = False

    async def test_question_with_closing_quote(self) -> None:
        pass

    async def test_announcement_text_does_not_decide_the_follow_up(self) -> None:
        pass

    async def test_ha_idle_doesnt_close_the_follow_up(self) -> None:
        pass

    async def test_speech_then_nothing_from_ha_ends_the_run(self) -> None:
        pass

    async def test_ha_error_ends_the_follow_up_run(self) -> None:
        pass

    async def test_follow_up_right_after_ha_reconnects(self) -> None:
        pass

    async def test_logs_from_ha_calls_carry_the_satellite(self) -> None:
        pass

    async def test_no_run_for_5_s_listens_again(self) -> None:
        pass


if __name__ == "__main__":
    unittest.main()
