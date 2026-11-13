"""Wake word listening started while a response plays on a media player.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
"""

import array
import asyncio
import math
import time
import unittest

from wyoming.asr import Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.tts import Synthesize

import test_followup  # (not its class by name: the loader would run its tests here)
from wyoming_vban.satellite import FollowupSettings, MicMute, VbanSatelliteHandler
from wyoming_vban.vban import VbanPacket

RATE = 16000
PACKET = 256
LOUD = 3000  # a sample above this is the microphone, not the silence sent instead
WAKE_CONTEXT_S = 1.3  # audio openWakeWord needs before it can detect


def tone(seconds: float, amplitude: int) -> bytes:
    return array.array("h", (int(amplitude * math.sin(2 * math.pi * 300 * i / RATE))
                             for i in range(int(RATE * seconds)))).tobytes()


class EarlyWakeTest(test_followup.FollowupTest):
    followup = FollowupSettings(enabled=True, beep=False, timeout_s=1.0)
    RESPONSE_S = 1.5

    def make_handler(self, reader, writer) -> VbanSatelliteHandler:
        self.media = getattr(self, "media", None) or test_followup.FakeCastOutput(late_s=5.0)
        self.media.end_margin_s = 0.3
        self.mute = MicMute(tail_s=0.25, max_muted_s=60)
        return VbanSatelliteHandler(
            reader, writer, self.info, self.receiver, None, media_output=self.media,
            mic_mute=self.mute, followup=self.followup, exchange_publisher=self.publisher,
            satellite_name="Bureau", waiting_cues=self.waiting_cues,
        )

    async def mic(self, amplitude_at) -> None:
        """Microphone in real time; amplitude_at(now) gives its loudness."""
        n = 0
        start = time.monotonic()
        while True:
            amplitude = amplitude_at(time.monotonic())
            self.receiver.feed(VbanPacket(
                sample_rate=RATE, samples_per_frame=PACKET, channels=1, data_format=1,
                codec=0, stream_name="AtomBureau", frame_counter=n,
                payload=tone(PACKET / RATE, amplitude),
            ))
            n += 1
            await asyncio.sleep(max(0.0, start + n * PACKET / RATE - time.monotonic()))

    async def response(self, text: str) -> float:
        """HA's end of run with a RESPONSE_S response; returns when its
        TTS was fully received."""
        await self.ha.write_event(Transcript(text="Quelle heure est-il ?").event())
        await self.ha.write_event(Synthesize(text=text).event())
        await self.ha.write_event(AudioStart(rate=22050, width=2, channels=1).event())
        await self.ha.write_event(AudioChunk(
            rate=22050, width=2, channels=1, audio=bytes(int(22050 * self.RESPONSE_S) * 2),
        ).event())
        await self.ha.write_event(AudioStop().event())
        return time.monotonic()

    def sound_end(self) -> float:
        """When the speaker finished the response ("playing" + its length)."""
        return self.media.state_watcher.changes[0].time + self.RESPONSE_S

    async def test_wake_run_at_the_response_and_mic_back_at_its_end(self) -> None:
        await self.next_run()
        feed = asyncio.create_task(self.mic(lambda now: 8000))  # someone talks all along
        try:
            with self.assertLogs("wyoming_vban.satellite", "INFO") as logs:
                received = await self.response("Il est 10 h 27.")
                run = await self.next_run()
                t_run = time.monotonic()
                self.assertEqual(run.start_stage, "wake")
                self.assertLess(t_run - received, 0.1)
                # Silence while the response plays, the real mic after
                while True:
                    event = await asyncio.wait_for(self.ha.read_event(), 5)
                    if event.type == "audio-chunk" and max(
                            abs(s) for s in array.array("h", event.payload)) > LOUD:
                        break
                t_back = time.monotonic()
                await asyncio.sleep(0.05)
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        end = self.sound_end()
        self.assertGreater(t_back, end)
        self.assertLessEqual(t_back - end, 0.3 + 0.25 + 0.1)  # end_margin + tail (+ a chunk)
        self.assertTrue(any("Wake word ready 0.5 s after the end of the response" in line
                            or "Wake word ready 0.6 s after the end of the response" in line
                            for line in logs.output), logs.output)

    async def test_wake_word_right_after_the_response(self) -> None:
        await self.next_run()
        keyword_at = [math.inf]

        def loudness(now: float) -> int:
            return 8000 if keyword_at[0] <= now <= keyword_at[0] + 0.8 else 30

        feed = asyncio.create_task(self.mic(loudness))
        try:
            await self.response("Il est 10 h 27.")
            run = await self.next_run()
            self.assertEqual(run.start_stage, "wake")
            while not self.media.state_watcher.changes:
                await asyncio.sleep(0.01)
            keyword_at[0] = self.sound_end() + 0.5  # "Dis-moi Jarvis" 0.5 s after the sound
            # A wake word server needing WAKE_CONTEXT_S of the run's audio
            heard = 0
            detected = False
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not detected:
                event = await asyncio.wait_for(self.ha.read_event(), 5)
                if event.type != "audio-chunk":
                    continue
                samples = array.array("h", event.payload)
                if max(abs(s) for s in samples) > LOUD and heard >= WAKE_CONTEXT_S * RATE:
                    detected = True
                heard += len(samples)
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        self.assertTrue(detected)

    async def test_question_keeps_the_follow_up(self) -> None:
        await self.next_run()
        await self.response("Quelle lumière ?")
        run = await self.next_run(timeout=6)
        self.assertEqual(run.start_stage, "asr")  # no wake run in between
        self.assertFalse(self.handlers[-1]._early_wake)


# Only the tests written here (not FollowupTest's again)
for _name in dir(test_followup.FollowupTest):
    if _name.startswith("test_") and _name not in EarlyWakeTest.__dict__:
        setattr(EarlyWakeTest, _name, None)


if __name__ == "__main__":
    unittest.main()
