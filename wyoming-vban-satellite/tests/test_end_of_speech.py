"""End of speech detected by the app (microphone.end_of_speech).

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
"""

import array
import asyncio
import logging
import math
import random
import time
import unittest
from typing import Optional

from wyoming.asr import Transcribe

import test_followup  # (not its class by name: the loader would run its tests here)
from wyoming_vban.end_of_speech import EndOfSpeechDetector, EndOfSpeechSettings
from wyoming_vban.satellite import FollowupSettings, VbanSatelliteHandler
from wyoming_vban.vban import VbanPacket

RATE = 16000
PACKET = 256  # samples per VBAN packet (16 ms)


def noise(seconds: float, seed: int = 1) -> bytes:
    """Room noise, about -60 dBFS."""
    rnd = random.Random(seed)
    return array.array("h", (rnd.randint(-55, 55) for _ in range(int(RATE * seconds)))).tobytes()


def speech(seconds: float) -> bytes:
    """A voice-like tone, about -15 dBFS."""
    return array.array("h", (int(7000 * math.sin(2 * math.pi * 220 * i / RATE))
                             for i in range(int(RATE * seconds)))).tobytes()


def feed_all(detector: EndOfSpeechDetector, pcm: bytes, step: int = PACKET * 2) -> Optional[int]:
    """Byte position of the end of speech in pcm (None if not reached)."""
    for start in range(0, len(pcm), step):
        cut = detector.feed(pcm[start:start + step])
        if cut is not None:
            return start + cut
    return None


class DetectorTest(unittest.TestCase):
    def detector(self, **settings) -> EndOfSpeechDetector:
        detector = EndOfSpeechDetector()
        feed_all(detector, noise(1.0))  # background learnt before the STT
        detector.arm(EndOfSpeechSettings(enabled=True, **settings))
        return detector

    def test_stop_500_to_550_ms_after_the_speech(self) -> None:
        detector = self.detector()
        lead = noise(0.2) + speech(1.5)
        end = feed_all(detector, lead + noise(2.0, seed=2))
        self.assertIsNotNone(end)
        after_ms = (end - len(lead)) / 2 / RATE * 1000
        self.assertGreaterEqual(after_ms, 500)
        self.assertLessEqual(after_ms, 550)
        self.assertAlmostEqual(detector.speech_s, 1.5, delta=0.05)

    def test_pause_in_a_sentence_doesnt_end_it(self) -> None:
        detector = self.detector()
        first = speech(0.8) + noise(0.3, seed=3)
        self.assertIsNone(feed_all(detector, first))
        end = feed_all(detector, speech(0.8) + noise(1.0, seed=4))
        self.assertIsNotNone(end)
        self.assertGreater(end, len(speech(0.8)))  # after the second part

    def test_no_speech_no_stop(self) -> None:
        detector = self.detector()
        self.assertIsNone(feed_all(detector, noise(5.0, seed=5)))

    def test_short_noise_isnt_speech(self) -> None:
        detector = self.detector()
        self.assertIsNone(feed_all(detector, speech(0.2) + noise(3.0, seed=6)))  # a click, a cough

    def test_minimum_listening(self) -> None:
        detector = self.detector(min_listen_ms=2000)
        end = feed_all(detector, speech(0.4) + noise(3.0, seed=7))
        self.assertGreaterEqual(end / 2 / RATE, 2.0 - 0.03)

    def test_silence_setting(self) -> None:
        detector = self.detector(silence_ms=1000)
        end = feed_all(detector, speech(1.0) + noise(3.0, seed=8))
        self.assertAlmostEqual((end / 2 / RATE - 1.0) * 1000, 1000, delta=40)

    def test_not_armed_only_learns(self) -> None:
        detector = EndOfSpeechDetector()
        self.assertIsNone(feed_all(detector, noise(0.5) + speech(1.0) + noise(2.0)))
        self.assertLess(detector.floor_db, -50)


class EndOfSpeechRunTest(test_followup.FollowupTest):
    """A satellite with the end of speech on, driving its runs."""

    eos = EndOfSpeechSettings(enabled=True)
    followup = FollowupSettings(enabled=True, beep=False, timeout_s=5.0)

    def make_handler(self, reader, writer) -> VbanSatelliteHandler:
        return VbanSatelliteHandler(
            reader, writer, self.info, self.receiver, self.sender,
            followup=self.followup, exchange_publisher=self.publisher,
            satellite_name="Bureau", waiting_cues=self.waiting_cues,
            end_of_speech=self.eos,
        )

    async def mic(self, pcm: bytes) -> None:
        """Microphone audio as VBAN packets, in real time."""
        start = time.monotonic()
        for n, i in enumerate(range(0, len(pcm), PACKET * 2)):
            self.receiver.feed(VbanPacket(
                sample_rate=RATE, samples_per_frame=PACKET, channels=1, data_format=1,
                codec=0, stream_name="AtomBureau", frame_counter=n,
                payload=pcm[i:i + PACKET * 2],
            ))
            await asyncio.sleep(max(0.0, start + (n + 1) * PACKET / RATE - time.monotonic()))

    async def listen(self, pcm: bytes, seconds: float) -> tuple[bytes, bool]:
        """STT starts, the mic sends pcm: (audio HA got, audio-stop sent)."""
        await self.next_run()
        await self.ha.write_event(Transcribe().event())
        feed = asyncio.create_task(self.mic(pcm))
        audio = bytearray()
        stopped = False
        deadline = time.monotonic() + seconds
        try:
            while (left := deadline - time.monotonic()) > 0:
                try:
                    event = await asyncio.wait_for(self.ha.read_event(), left)
                except asyncio.TimeoutError:
                    break
                if event.type == "audio-chunk":
                    audio.extend(event.payload)
                elif event.type == "audio-stop":
                    stopped = True
                    break
        finally:
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        return bytes(audio), stopped

    @staticmethod
    def silence_at_end_ms(audio: bytes) -> float:
        samples = array.array("h", audio)
        last = max(i for i, s in enumerate(samples) if abs(s) > 1000)
        return (len(samples) - 1 - last) / RATE * 1000

    async def test_sentence_then_silence(self) -> None:
        pcm = noise(0.6) + speech(1.5) + noise(2.5, seed=2)
        with self.assertLogs("wyoming_vban.satellite", "INFO") as logs:
            audio, stopped = await self.listen(pcm, 4.5)
        self.assertTrue(stopped)
        self.assertGreaterEqual(self.silence_at_end_ms(audio), 500)
        self.assertLessEqual(self.silence_at_end_ms(audio), 550)
        self.assertTrue(any("End of speech: 1.5 s of speech, audio-stop after" in line
                            for line in logs.output))

    async def test_pause_in_the_middle(self) -> None:
        pcm = noise(0.6) + speech(0.8) + noise(0.3, seed=3) + speech(0.8) + noise(2.0, seed=4)
        audio, stopped = await self.listen(pcm, 4.5)
        self.assertTrue(stopped)
        # Stopped after the second part, not in the pause
        loud = sum(1 for s in array.array("h", audio) if abs(s) > 1000)
        self.assertGreater(loud, RATE * 1.4)
        self.assertGreaterEqual(self.silence_at_end_ms(audio), 500)

    async def test_no_speech_left_to_ha(self) -> None:
        _, stopped = await self.listen(noise(3.0), 3.0)
        self.assertFalse(stopped)


class EndOfSpeechOffTest(EndOfSpeechRunTest):
    eos = EndOfSpeechSettings(enabled=False)

    async def test_sentence_then_silence(self) -> None:
        _, stopped = await self.listen(noise(0.6) + speech(1.0) + noise(2.0, seed=2), 3.5)
        self.assertFalse(stopped)  # HA's VAD decides

    async def test_pause_in_the_middle(self) -> None:
        pass


class EndOfSpeechFollowupTest(EndOfSpeechRunTest):
    """The beep of a follow-up isn't speech."""

    eos = EndOfSpeechSettings(enabled=True, min_listen_ms=0)
    followup = FollowupSettings(enabled=True, beep=True, timeout_s=8.0)

    async def test_beep_isnt_speech(self) -> None:
        await self.next_run()
        loud = asyncio.create_task(self.mic(speech(5.0)))  # the beep heard by the mic
        await self.respond("Quelle lumière ?")
        run = await self.next_run(timeout=5)
        self.assertEqual(run.start_stage, "asr")
        await self.ha.write_event(Transcribe().event())
        await asyncio.sleep(0.1)  # its tail, right at the opening
        loud.cancel()
        await asyncio.gather(loud, return_exceptions=True)
        feed = asyncio.create_task(self.mic(noise(2.0)))
        lines: list[str] = []
        handler = logging.Handler()
        handler.emit = lambda record: lines.append(record.getMessage())
        log = logging.getLogger("wyoming_vban.satellite")
        log.addHandler(handler)
        try:
            stop = await self.next_event("audio-stop", timeout=2.0)
        finally:
            log.removeHandler(handler)
            feed.cancel()
            await asyncio.gather(feed, return_exceptions=True)
        self.assertIsNone(stop)  # (the follow-up's own timeout is 8 s)
        self.assertFalse(any("End of speech" in line for line in lines))


# Only the tests written here (not FollowupTest's again)
for _case in (EndOfSpeechRunTest, EndOfSpeechOffTest, EndOfSpeechFollowupTest):
    for _name in dir(test_followup.FollowupTest):
        if _name.startswith("test_") and _name not in _case.__dict__:
            setattr(_case, _name, None)
for _name in ("test_sentence_then_silence", "test_pause_in_the_middle",
              "test_no_speech_left_to_ha"):
    if _name not in EndOfSpeechFollowupTest.__dict__:
        setattr(EndOfSpeechFollowupTest, _name, None)


if __name__ == "__main__":
    unittest.main()
