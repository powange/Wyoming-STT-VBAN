"""A transcript too slow: the satellite resets its connection (14/11, 14:55:
Whisper took 17 s, the wake run sent meanwhile was left deaf by HA).

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
"""

import array
import asyncio
import math
import time
import unittest

from wyoming.asr import Transcribe, Transcript
from wyoming.tts import Synthesize
from wyoming.wake import Detection

import test_early_wake
import test_followup  # (not its class by name: the loader would run its tests here)
from wyoming_vban.end_of_speech import EndOfSpeechSettings
from wyoming_vban.satellite import FollowupSettings, VbanSatelliteHandler

RATE = 16000
LOUD = test_early_wake.LOUD


class SlowTranscriptTest(test_followup.FollowupTest):
    followup = FollowupSettings(enabled=True, beep=False, timeout_s=5.0, stt_timeout_s=1.0)

    def make_handler(self, reader, writer) -> VbanSatelliteHandler:
        return VbanSatelliteHandler(
            reader, writer, self.info, self.receiver, self.sender,
            followup=self.followup, exchange_publisher=self.publisher,
            satellite_name="Bureau", waiting_cues=self.waiting_cues,
            end_of_speech=EndOfSpeechSettings(enabled=True),
        )

    mic = test_early_wake.EarlyWakeTest.mic

    async def test_reset_then_the_wake_word_works(self) -> None:
        await self.next_run()
        loud = [math.inf, -math.inf]  # (from, until)
        feed = asyncio.create_task(self.mic(
            lambda now: 8000 if loud[0] <= now < loud[1] else 30))
        try:
            await asyncio.sleep(0.5)  # the room's background
            await self.ha.write_event(Detection(name="dis_moi_jarvis").event())
            await self.ha.write_event(Transcribe().event())
            loud[:] = [time.monotonic(), time.monotonic() + 1.2]  # the command
            with self.assertLogs("wyoming_vban.satellite", "INFO") as logs:
                self.assertIsNotNone(await self.next_event("audio-stop", timeout=3))
                stopped = time.monotonic()
                # HA's STT takes 20 s: the satellite gives up at stt_timeout_s
                self.assertTrue(await self.connection_closed(timeout=3))
                self.assertGreaterEqual(time.monotonic() - stopped,
                                        self.followup.stt_timeout_s - 0.1)
            self.assertTrue(any("Satellite reset after end of speech: no transcript" in line
                                for line in logs.output), logs.output)
            old = self.handlers[-1]
            # HA reconnects (about 3 s later in HA) and starts afresh
            await self.reconnect()
            run = await self.next_run()
            self.assertEqual(run.start_stage, "wake")
            self.assertIsNot(self.handlers[-1], old)
            keyword_at = time.monotonic() + 2.0  # "Dis-moi Jarvis" 2 s after
            loud[:] = [keyword_at, keyword_at + 0.8]
            heard, detected = 0, False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not detected:
                event = await asyncio.wait_for(self.ha.read_event(), 5)
                if event.type != "audio-chunk":
                    continue
                samples = array.array("h", event.payload)
                if max(abs(s) for s in samples) > LOUD and heard >= 1.3 * RATE:
                    detected = True
                heard += len(samples)
            self.assertTrue(detected)
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)

    async def test_late_transcript_goes_nowhere(self) -> None:
        await self.next_run()
        loud = [math.inf, -math.inf]
        feed = asyncio.create_task(self.mic(
            lambda now: 8000 if loud[0] <= now < loud[1] else 30))
        try:
            await asyncio.sleep(0.5)
            await self.ha.write_event(Detection(name="dis_moi_jarvis").event())
            await self.ha.write_event(Transcribe().event())
            loud[:] = [time.monotonic(), time.monotonic() + 1.0]
            self.assertTrue(await self.connection_closed(timeout=5))
            old_ha = self.ha
            await self.reconnect()
            self.assertEqual((await self.next_run()).start_stage, "wake")
            # The transcript arriving 17 s later, and its response: HA's run
            # was cancelled with the old connection; whatever comes on the
            # new one isn't this run's (no transcript was awaited there)
            for event in (Synthesize(text="Il fait 12 degrés.").event(),):
                await self.ha.write_event(event)
            self.assertIsNot(self.ha, old_ha)
            await asyncio.sleep(0.3)
            handler = self.handlers[-1]
            self.assertFalse(handler.in_exchange)
            self.assertEqual(self.publisher.events, [])  # not published as an exchange
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)

    async def test_transcript_in_time_no_reset(self) -> None:
        await self.next_run()
        loud = [math.inf, -math.inf]
        feed = asyncio.create_task(self.mic(
            lambda now: 8000 if loud[0] <= now < loud[1] else 30))
        try:
            await asyncio.sleep(0.5)
            await self.ha.write_event(Detection(name="dis_moi_jarvis").event())
            await self.ha.write_event(Transcribe().event())
            loud[:] = [time.monotonic(), time.monotonic() + 1.0]
            self.assertIsNotNone(await self.next_event("audio-stop", timeout=3))
            await self.ha.write_event(Transcript(text="Quelle heure est-il ?").event())
            await self.respond("Il est 15 h.", transcript="Quelle heure est-il ?")
            run = await self.next_run()
            self.assertEqual(run.start_stage, "wake")
            self.assertFalse(await self.connection_closed(timeout=1.5))
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)


class DeafWatchdogTest(unittest.TestCase):
    def runner(self):
        import collections
        import types
        from wyoming_vban.runtime import SatelliteRunner
        runner = SatelliteRunner.__new__(SatelliteRunner)
        runner.cfg = types.SimpleNamespace(name="Bureau")
        runner._levels = collections.deque()
        runner.last_detection_at = time.monotonic() - 3600
        runner._speech_bursts, runner._burst_on, runner._deaf_warned = 0, False, False
        runner._quiet_since, runner._floor_db, runner._floor_at = -math.inf, None, -math.inf
        return runner

    def hear(self, runner, pattern: list[tuple[float, float]]) -> None:
        """(seconds, level dB) in turn, as 16 ms packets."""
        now = time.monotonic() - sum(s for s, _ in pattern)
        for seconds, level in pattern:
            for _ in range(int(seconds / 0.016)):
                runner._levels.append((now, level, 0.0))
                runner._track_speech(now, level)
                now += 0.016

    def test_warns_once_speech_heard_without_wake_word(self) -> None:
        runner = self.runner()
        self.hear(runner, [(3, -60), (1, -25), (2, -60), (1, -25), (2, -60), (1, -25), (2, -60)])
        with self.assertLogs("wyoming_vban.runtime", "WARNING") as logs:
            runner.check_deaf(30)
        self.assertTrue(any("may be deaf" in line for line in logs.output))
        with self.assertNoLogs("wyoming_vban.runtime", "WARNING"):
            runner.check_deaf(30)  # once

    def test_quiet_room_or_recent_wake_word(self) -> None:
        runner = self.runner()
        self.hear(runner, [(10, -60)])
        with self.assertNoLogs("wyoming_vban.runtime", "WARNING"):
            runner.check_deaf(30)  # nobody spoke
        self.hear(runner, [(1, -25), (2, -60)] * 4)
        runner.note_detection()
        with self.assertNoLogs("wyoming_vban.runtime", "WARNING"):
            runner.check_deaf(30)  # it heard its wake word


# Only the tests written here
for _name in dir(test_followup.FollowupTest):
    if _name.startswith("test_") and _name not in SlowTranscriptTest.__dict__:
        setattr(SlowTranscriptTest, _name, None)


if __name__ == "__main__":
    unittest.main()
