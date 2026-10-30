"""Follow-up without wake word: the satellite drives the pipeline runs.

The test plays Home Assistant's side of the Wyoming connection.
Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import asyncio
import socket
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


def _free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeReceiver:
    def subscribe(self, callback, stream_name="") -> None:
        pass

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


class FollowupTest(unittest.IsolatedAsyncioTestCase):
    followup = FollowupSettings(enabled=True, beep=False, timeout_s=0.5)

    async def asyncSetUp(self) -> None:
        self.sender = FakeVbanSender()
        port = _free_tcp_port()
        self.server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")
        await self.server.start(
            lambda reader, writer: VbanSatelliteHandler(
                reader, writer, make_satellite_info("Bureau"), FakeReceiver(),
                self.sender, followup=self.followup,
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

    async def respond(self, text: str) -> None:
        """A run's end as HA sends it: transcript, synthesize, TTS audio."""
        await self.ha.write_event(Transcript(text="...").event())
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

    async def test_question_reply_is_played_before_next_run(self) -> None:
        await self.next_run()
        await self.respond("Quelle lumière ?")
        await self.next_run()
        self.assertEqual(len(self.sender.sent), 1)  # the response went to VBAN


class FollowupBeepTest(FollowupTest):
    followup = FollowupSettings(enabled=True, beep=True, timeout_s=0.5)

    async def test_question_reply_is_played_before_next_run(self) -> None:
        await self.next_run()
        await self.respond("Quelle lumière ?")
        await self.next_run()
        # The response, then the 150 ms beep (16 kHz, 16-bit), before the run
        self.assertEqual(self.sender.sent, [4410, int(16000 * 0.15) * 2])


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


if __name__ == "__main__":
    unittest.main()
