"""The microphone mute on a media player: only the app's own playbacks,
never while a command is listened to, never stuck (14/11 Bureau deaf).

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
"""

import array
import asyncio
import math
import time
import unittest

from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.error import Error
from wyoming.tts import Synthesize
from wyoming.wake import Detection

import test_early_wake
import test_followup  # (not its classes by name: the loader would run their tests here)
from wyoming_vban.end_of_speech import EndOfSpeechSettings
from wyoming_vban.ha_ws import StateChange
from wyoming_vban.satellite import (
    Announcement,
    Cue,
    FollowupSettings,
    MicMute,
    VbanSatelliteHandler,
)

RATE = 16000
LOUD = test_early_wake.LOUD
WAKE_CONTEXT_S = test_early_wake.WAKE_CONTEXT_S


class NeverEndingOutput(test_followup.FakeMediaOutput):
    """play_media that never comes back (a speaker never reporting anything)."""

    async def play(self, url: str) -> bool:
        await asyncio.sleep(3600)
        return True


class MicMuteCasesTest(test_early_wake.EarlyWakeTest):
    followup = FollowupSettings(enabled=True, beep=False, timeout_s=1.0)

    def make_handler(self, reader, writer) -> VbanSatelliteHandler:
        self.media = getattr(self, "media", None) or test_followup.FakeCastOutput(late_s=0.0)
        self.media.end_margin_s = 0.3
        self.mute = getattr(self, "mute", None) or MicMute(tail_s=0.25, max_muted_s=30)
        return VbanSatelliteHandler(
            reader, writer, self.info, self.receiver, None, media_output=self.media,
            mic_mute=self.mute, followup=self.followup, exchange_publisher=self.publisher,
            satellite_name="Bureau", waiting_cues=self.waiting_cues,
            end_of_speech=EndOfSpeechSettings(enabled=True),
        )

    async def wake_word_heard(self, after: float, seconds: float = 5.0) -> bool:
        """A wake word server needing WAKE_CONTEXT_S of a wake run's audio:
        does it hear the loud audio said `after`?"""
        heard, deadline = 0, time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                event = await asyncio.wait_for(self.ha.read_event(), deadline - time.monotonic())
            except asyncio.TimeoutError:
                return False
            if event.type == "run-pipeline":
                heard = 0  # a new run: its own context
            elif event.type == "audio-chunk":
                samples = array.array("h", event.payload)
                if (time.monotonic() >= after and max(abs(s) for s in samples) > LOUD
                        and heard >= WAKE_CONTEXT_S * RATE):
                    return True
                heard += len(samples)
        return False

    async def test_media_player_waking_up_doesnt_cut_the_command(self) -> None:
        await self.next_run()
        speech_until = [-math.inf]  # quiet room until the command
        feed = asyncio.create_task(self.mic(
            lambda now: 8000 if speech_until[0] - 3.0 <= now < speech_until[0] else 30))
        await asyncio.sleep(0.5)
        try:
            await self.ha.write_event(Detection(name="dis_moi_jarvis").event())
            await self.ha.write_event(Transcribe().event())
            speech_until[0] = time.monotonic() + 3.0  # a 3 s command
            await asyncio.sleep(0.05)
            # The Nest woken by an automation at the wake word (off -> idle)
            self.media.state_watcher.changes.append(
                StateChange(time.monotonic(), "idle", {}))
            # …and even a mute left over (an announcement never finished)
            self.mute.start_response("announcement")
            quiet_during_speech, stop_at = 0, None
            deadline = time.monotonic() + 6
            while stop_at is None and time.monotonic() < deadline:
                event = await asyncio.wait_for(self.ha.read_event(), 6)
                now = time.monotonic()
                if event.type == "audio-chunk" and speech_until[0] - 2.7 < now < speech_until[0] - 0.1:
                    if max(abs(s) for s in array.array("h", event.payload)) < LOUD:
                        quiet_during_speech += 1
                elif event.type == "audio-stop":
                    stop_at = time.monotonic()
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        self.assertEqual(quiet_during_speech, 0)  # all of it sent, never silence
        self.assertIsNotNone(stop_at)
        self.assertGreater(stop_at, speech_until[0] + 0.45)  # end of speech after the command

    async def test_run_without_transcript_gives_the_real_mic(self) -> None:
        await self.next_run()
        keyword = [math.inf]
        feed = asyncio.create_task(self.mic(
            lambda now: 8000 if keyword[0] <= now <= keyword[0] + 0.8 else 30))
        try:
            await self.ha.write_event(Detection(name="dis_moi_jarvis").event())
            self.mute.start_response("announcement")  # left over, never ended
            with self.assertLogs("wyoming_vban.satellite", "INFO") as logs:
                await self.ha.write_event(Error(text="no text", code="stt-no-text-recognized").event())
                run = await self.next_run()
                self.assertEqual(run.start_stage, "wake")
                keyword[0] = time.monotonic() + 1.0  # said 1 s after the run
                self.assertTrue(await self.wake_word_heard(after=keyword[0]))
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        self.assertTrue(any("Wake word ready" in line and "(no response)" in line
                            for line in logs.output), logs.output)
        self.assertTrue(any("Mic restored (reason: run over without response" in line
                            for line in logs.output), logs.output)

    async def test_speaker_never_reporting_the_end(self) -> None:
        self.media.play = NeverEndingOutput.play.__get__(self.media)  # never comes back
        await self.next_run()
        feed = asyncio.create_task(self.mic(lambda now: 8000))
        try:
            with self.assertLogs("wyoming_vban.satellite", "WARNING") as logs:
                received = await self.response("Il est 10 h 27.")  # 1.5 s
                published = None
                while True:
                    event = await asyncio.wait_for(self.ha.read_event(), 8)
                    if published is None and self.media.published:
                        published = self.media.published[0]
                    if event.type == "audio-chunk" and max(
                            abs(s) for s in array.array("h", event.payload)) > LOUD:
                        break
                back = time.monotonic()
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        self.assertGreater(back - received, 1.5)  # muted while it (should) play
        self.assertLessEqual(back - (published or received), 1.5 + 0.3 + 3.0 + 0.2)
        self.assertTrue(any("Mic mute timeout, real microphone restored" in line
                            for line in logs.output))

    async def test_announcement_then_response_on_a_speaker_staying_playing(self) -> None:
        self.media.state_watcher.late_s = 14.0  # "playing" 14 s after each sound
        await self.next_run()
        keyword = [math.inf]
        feed = asyncio.create_task(self.mic(
            lambda now: 8000 if keyword[0] <= now <= keyword[0] + 0.8 else 30))
        try:
            await self.response("Il est 12 h 19.")
            self.assertEqual((await self.next_run()).start_stage, "wake")
            await asyncio.sleep(2.5)  # played (the speaker still shows playing)
            # A new question: "Je réfléchis..." then its response
            await self.ha.write_event(Detection(name="dis_moi_jarvis").event())
            await self.ha.write_event(Transcribe().event())
            await self.ha.write_event(Transcript(text="Combien font 8 x 9 ?").event())
            self.handlers[-1]._announce("thinking", Announcement(
                sound=Cue(bytes(int(RATE * 0.8) * 2), RATE)))
            await asyncio.sleep(0.3)
            await self.response("Ça fait 72.")
            while not len(self.media.state_watcher.changes) >= 6:
                await asyncio.sleep(0.01)
            end = self.media.state_watcher.changes[-2].time + self.RESPONSE_S
            keyword[0] = end + 1.0
            self.assertTrue(await self.wake_word_heard(after=keyword[0], seconds=8))
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)

    async def stream_response(self, text: str) -> float:
        """Streaming TTS (an LLM): the audio first, its text after it."""
        await self.ha.write_event(Transcript(text="Question").event())
        await self.ha.write_event(AudioStart(rate=22050, width=2, channels=1).event())
        await self.ha.write_event(AudioChunk(
            rate=22050, width=2, channels=1, audio=bytes(int(22050 * self.RESPONSE_S) * 2),
        ).event())
        await self.ha.write_event(AudioStop().event())
        await asyncio.sleep(0.4)
        await self.ha.write_event(Synthesize(text=text).event())
        return time.monotonic()

    async def test_streaming_response_waits_for_its_text(self) -> None:
        await self.next_run()
        texted = await self.stream_response("Il est midi.")
        self.assertIsNotNone(await self.next_run())
        # Not before HA's run is over (just after the text), still while it plays
        self.assertGreaterEqual(time.monotonic() - texted,
                                VbanSatelliteHandler.EARLY_WAKE_AFTER_TEXT_S - 0.05)
        self.assertEqual(self.publisher.events[-1][1]["response"], "Il est midi.")

    async def test_streaming_question_keeps_the_follow_up(self) -> None:
        await self.next_run()
        await self.stream_response("Quelle pièce ?")
        run = await self.next_run(timeout=6)
        self.assertEqual(run.start_stage, "asr")


# Only the tests written here
for _name in dir(test_early_wake.EarlyWakeTest):
    if _name.startswith("test_") and _name not in MicMuteCasesTest.__dict__:
        setattr(MicMuteCasesTest, _name, None)


if __name__ == "__main__":
    unittest.main()
