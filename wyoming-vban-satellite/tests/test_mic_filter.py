"""Microphone clean-up: DC offset, noise suppression, automatic gain.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements, including pyspeex-noise)
"""

import audioop
import math
import random
import struct
import unittest

from wyoming_vban.mic_filter import MicFilter

RATE = 16000


def noise(seconds: float, amplitude: float, seed: int = 1) -> bytes:
    rng = random.Random(seed)
    return struct.pack(
        f"<{int(RATE * seconds)}h",
        *(int(rng.gauss(0, amplitude)) for _ in range(int(RATE * seconds))),
    )


def tone(seconds: float, amplitude: float, freq: float = 440.0) -> bytes:
    n = int(RATE * seconds)
    return struct.pack(f"<{n}h", *(int(amplitude * math.sin(2 * math.pi * freq * i / RATE))
                                   for i in range(n)))


def mix(a: bytes, b: bytes) -> bytes:
    return audioop.add(a, b, 2)


def db(pcm: bytes) -> float:
    return 20 * math.log10(max(audioop.rms(pcm, 2), 1) / 32768)


def run(filter_: MicFilter, pcm: bytes, chunk: int = 512) -> bytes:
    """Feed like the satellite does: VBAN-sized chunks (not 10 ms multiples)."""
    return b"".join(filter_.process(pcm[i:i + chunk]) for i in range(0, len(pcm), chunk))


class MicFilterTest(unittest.TestCase):
    def test_dc_offset_removed(self) -> None:
        pcm = audioop.bias(noise(3, 200), 2, 5000)
        out = run(MicFilter(), pcm)
        self.assertEqual(len(out), len(pcm))  # off: no buffering
        last = out[-RATE * 2:]  # after the 1 s tracking
        self.assertLess(abs(audioop.avg(last, 2)), 100)

    def test_noise_suppression_reduces_steady_noise(self) -> None:
        pcm = noise(4, 300)  # about -40 dB of steady noise
        quiet = run(MicFilter("medium"), pcm)
        reduction = db(pcm[-RATE * 2:]) - db(quiet[-RATE * 2:])
        self.assertGreater(reduction, 8)  # learned, then attenuated

    def test_voice_is_kept(self) -> None:
        filter_ = MicFilter("medium")
        run(filter_, noise(3, 300))  # learns the background
        voice = mix(tone(1, 8000), noise(1, 300, seed=2))
        out = run(filter_, voice)
        self.assertLess(abs(db(voice[RATE:]) - db(out[RATE // 2:])), 3)

    def speech(self, seconds: int, amplitude: float) -> bytes:
        """Half a second of "voice" (a tone), half a second of background."""
        parts = []
        for i in range(seconds * 2):
            part = noise(0.5, 100, seed=i)
            if i % 2 == 0:
                part = mix(part, tone(0.5, amplitude))
            parts.append(part)
        return b"".join(parts)

    def voice_level(self, out: bytes) -> float:
        """Level of the last voice half-second."""
        half = RATE // 2 * 2
        return db(out[-2 * half:-half])

    def test_auto_gain_raises_a_quiet_voice(self) -> None:
        out = run(MicFilter(auto_gain=True), self.speech(8, 500))  # about -39 dB
        self.assertAlmostEqual(self.voice_level(out), -18, delta=3)

    def test_auto_gain_lowers_a_loud_voice(self) -> None:
        out = run(MicFilter(auto_gain=True), self.speech(4, 20000))  # about -4 dB
        self.assertAlmostEqual(self.voice_level(out), -16, delta=3)  # max -12 dB gain

    def test_auto_gain_does_not_boost_background(self) -> None:
        pcm = noise(4, 100)
        out = run(MicFilter(auto_gain=True), pcm)
        self.assertAlmostEqual(db(out[-RATE:]), db(pcm[-RATE:]), delta=1)

    def test_auto_gain_with_noise_suppression_is_not_silent(self) -> None:
        # pyspeex-noise's own automatic gain outputs silence: not used
        out = run(MicFilter("medium", auto_gain=True), self.speech(8, 500))
        self.assertGreater(self.voice_level(out), -25)

    def test_silent_output_is_bypassed(self) -> None:
        class Broken:  # e.g. pyspeex-noise's own automatic gain
            def process_10ms(self, frame):
                return bytes(len(frame))

        filter_ = MicFilter("medium")
        filter_._speex = Broken()
        pcm = self.speech(5, 3000)
        with self.assertLogs("wyoming_vban.mic_filter", "ERROR"):
            out = run(filter_, pcm)
        self.assertTrue(filter_.bypassed)
        self.assertGreater(db(out[-2 * RATE:]), -40)  # last second: the voice gets through

    def test_output_cut_in_10ms_frames(self) -> None:
        filter_ = MicFilter("light", auto_gain=True)
        pcm = noise(1, 300)
        out = run(filter_, pcm, chunk=700)  # odd chunk size
        self.assertEqual(len(out) % 320, 0)
        self.assertLess(len(pcm) - len(out), 320)  # at most 10 ms held

    def test_configure_live(self) -> None:
        filter_ = MicFilter()
        self.assertFalse(filter_.active)
        filter_.configure("strong", False)
        self.assertTrue(filter_.active)
        run(filter_, noise(1, 300))
        self.assertIsNotNone(filter_.noise_floor_db())
        self.assertIsNotNone(filter_.level_db())
        filter_.configure("off", False)
        self.assertFalse(filter_.active)
        self.assertIsNone(filter_.noise_floor_db())


if __name__ == "__main__":
    unittest.main()
