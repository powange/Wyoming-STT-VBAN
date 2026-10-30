"""Follow-up without wake word: the satellite drives the pipeline runs.

The test plays Home Assistant's side of the Wyoming connection.
Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import asyncio
import socket
import time
import unittest
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
    FollowupSettings,
    VbanSatelliteHandler,
    make_satellite_info,
)
from wyoming_vban.vban import VbanPacket


def _free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeReceiver:
    callback = None

    def subscribe(self, callback, stream_name="") -> None:
        self.callback = callback

    def unsubscribe(self, callback) -> None:
        pass


class FakeVbanSender:
    """Records what is sent; 'plays' instantly."""

    def __init__(self) -> None:
        self.sent: list[int] = []

    def reset_resampler(self) -> None:
        pass

    def clear_pending(self) -> None:
        pass

    def send(self, pcm, sample_rate=None, width=None, channels=None) -> None:
        self.sent.append(len(pcm))

    async def wait_sent(self, max_wait: float = 300.0) -> None:
        pass


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

    async def asyncSetUp(self) -> None:
        self.sender = FakeVbanSender()
        self.receiver = FakeReceiver()
        self.publisher = FakePublisher()
        port = _free_tcp_port()
        self.server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")
        await self.server.start(
            lambda reader, writer: VbanSatelliteHandler(
                reader, writer, make_satellite_info("Bureau"), self.receiver,
                self.sender, followup=self.followup,
                exchange_publisher=self.publisher, satellite_name="Bureau",
            )
        )
        self.ha = AsyncTcpClient("127.0.0.1", port)
        await self.ha.connect()
        # HA's connection sequence
        await self.ha.write_event(Describe().event())
        await self.ha.write_event(RunSatellite().event())
        await self.ha.write_event(Describe().event())

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

    async def _mic(self, seconds: float) -> None:
        """Loud microphone audio, in real time."""
        for i in range(int(seconds / 0.016)):
            self.receiver.callback(VbanPacket(
                sample_rate=16000, samples_per_frame=256, channels=1,
                data_format=1, codec=0, stream_name="AtomBureau",
                frame_counter=i, payload=b"\x00\x40" * 256,
            ))
            await asyncio.sleep(0.016)

    async def _chunks_after_run(self, seconds: float) -> tuple[float, list]:
        """(time of the ASR run, [(time, loud?) of audio chunks / audio-stop])."""
        await self.next_run()
        await self.respond("Quelle lumière ?")
        run = await self.next_run()
        self.assertEqual(run.start_stage, "asr")
        t_run = time.monotonic()
        mic = asyncio.create_task(self._mic(seconds))
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
        await mic
        return t_run, events

    async def test_mic_silent_during_beep_guard_then_preroll(self) -> None:
        _, events = await self._chunks_after_run(1.5)
        chunks = [(t, loud) for t, loud in events if loud != "audio-stop"]
        # Only silence during the guard
        self.assertTrue(all(not loud for t, loud in chunks if t < self.GUARD - 0.05))
        # Then the kept audio arrives at once, ahead of real time
        loud = [t for t, is_loud in chunks if is_loud]
        self.assertTrue(loud)
        first = loud[0]
        self.assertGreaterEqual(first, self.GUARD - 0.05)
        burst = [t for t in loud if t < first + 0.1]
        self.assertGreaterEqual(len(burst), 8)

    async def test_no_speech_timeout_starts_after_guard(self) -> None:
        # The mic is silent here: no voice-started, so the timeout fires
        await self.next_run()
        await self.respond("Quelle pièce ?")
        await self.next_run()
        t_run = time.monotonic()
        self.assertIsNotNone(await self.next_event("audio-stop", timeout=3))
        self.assertGreaterEqual(
            time.monotonic() - t_run, self.GUARD + self.followup.timeout_s - 0.05,
        )


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


if __name__ == "__main__":
    unittest.main()
