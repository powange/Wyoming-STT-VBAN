"""Microphone level statistics shown in the status panel.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import math
import random
import struct
import unittest

from wyoming_vban.config import SatelliteConfig
from wyoming_vban.runtime import ExchangePublisher, SatelliteRunner
from wyoming_vban.satellite import WaitingCues
from wyoming_vban.vban import VbanPacket


class FakeReceiver:
    def subscribe(self, callback, stream_name=""):
        pass

    def unsubscribe(self, callback):
        pass


def packet(amplitude: float, dc: int = 0, noise: bool = False) -> VbanPacket:
    """256 samples: a sine (or white noise) of this peak amplitude, plus DC."""
    rng = random.Random(1)
    samples = [
        int(dc + amplitude * (rng.uniform(-1, 1) if noise else math.sin(i / 3)))
        for i in range(256)
    ]
    return VbanPacket(
        sample_rate=16000, samples_per_frame=256, channels=1, data_format=1,
        codec=0, stream_name="x", frame_counter=0,
        payload=struct.pack("<256h", *samples),
    )


class LevelStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.runner = SatelliteRunner(
            SatelliteConfig(name="Salon"), FakeReceiver(), None,
            ExchangePublisher(False, None), WaitingCues(), [],
        )

    def test_dc_offset_is_not_counted_as_noise(self) -> None:
        # Quiet noise (about -54 dB) on top of a large DC offset (about -10 dB)
        for _ in range(100):
            self.runner._on_packet(packet(100, dc=10000, noise=True))
        status = self.runner.status()
        self.assertLess(status["noise_floor_db"], -50)
        self.assertLess(status["mic_level_db"], -50)
        self.assertAlmostEqual(status["dc_offset_db"], -10.3, delta=0.5)

    def test_noise_floor_and_voice_peak(self) -> None:
        for _ in range(90):  # background
            self.runner._on_packet(packet(300, noise=True))
        for _ in range(10):  # a voice, much louder
            self.runner._on_packet(packet(16000))
        status = self.runner.status()
        self.assertAlmostEqual(status["noise_floor_db"], -45.0, delta=2)
        self.assertAlmostEqual(status["peak_db"], -9.2, delta=1)
        self.assertLess(status["dc_offset_db"], -50)  # no real offset (sine residue only)


if __name__ == "__main__":
    unittest.main()
