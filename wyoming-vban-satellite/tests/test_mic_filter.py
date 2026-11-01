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
        filter_.configure("off", False)
        self.assertFalse(filter_.active)
        self.assertIsNone(filter_.noise_floor_db())


if __name__ == "__main__":
    unittest.main()
