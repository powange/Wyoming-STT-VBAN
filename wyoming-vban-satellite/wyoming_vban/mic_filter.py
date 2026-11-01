"""Microphone clean-up before the audio is sent to Home Assistant.

16 kHz mono 16-bit in and out:
- DC offset removal (always): a constant shift, common on ESP32
  microphones, that isn't heard;
- optional noise suppression (Speex): learns the background noise during
  the quiet moments of the last seconds and attenuates it, frequency band by
  frequency band (works on steady noise: computer fan, fridge, hiss);
- optional automatic gain: brings the voice to a steady level.
"""

import audioop
import collections
import logging
import math
import time
from typing import Optional

from pyspeex_noise import AudioProcessor

from .const import WYOMING_RATE, WYOMING_WIDTH

_LOGGER = logging.getLogger(__name__)

# Maximum attenuation of the noise, in dB (0 = off)
NOISE_LEVELS = {"off": 0, "light": -6, "medium": -15, "strong": -25}
AUTO_GAIN_LEVEL = 4000  # target speech level (16-bit RMS, about -18 dBFS)
FRAME_BYTES = WYOMING_RATE // 100 * WYOMING_WIDTH  # Speex works on 10 ms
DC_TIME_CONSTANT_S = 1.0
MAX_SAMPLE = (1 << (8 * WYOMING_WIDTH - 1)) - 1


class MicFilter:
    """Cleans a satellite's microphone audio; settings can change live."""

    STATS_WINDOW_S = 10.0

    def __init__(self, noise_suppression: str = "off", auto_gain: bool = False):
        self.noise_suppression = "off"
        self.auto_gain = False
        self._processor: Optional[AudioProcessor] = None
        self._pending = bytearray()  # < 10 ms waiting for the next call
        self._dc: Optional[float] = None  # tracked mean of the signal
        # (time, level dB) of the cleaned audio, for the status panel
        self._levels: collections.deque[tuple[float, float]] = collections.deque()
        self.configure(noise_suppression, auto_gain)

    @property
    def active(self) -> bool:
        """Noise suppression or automatic gain is on."""
        return self._processor is not None

    def configure(self, noise_suppression: str, auto_gain: bool) -> None:
        if (noise_suppression, auto_gain) == (self.noise_suppression, self.auto_gain):
            return
        self.noise_suppression = noise_suppression
        self.auto_gain = auto_gain
        level = NOISE_LEVELS.get(noise_suppression, 0)
        if level or auto_gain:
            self._processor = AudioProcessor(AUTO_GAIN_LEVEL if auto_gain else 0, level)
        else:
            self._processor = None
        self._pending.clear()
        self._levels.clear()

    def process(self, pcm: bytes) -> bytes:
        """Clean a chunk. With Speex on, up to 10 ms is held for the next
        call (the output length may differ by a frame)."""
        if not pcm:
            return pcm
        pcm = self._remove_dc(pcm)
        if self._processor is None:
            return pcm
        self._pending.extend(pcm)
        out = bytearray()
        while len(self._pending) >= FRAME_BYTES:
            frame = bytes(self._pending[:FRAME_BYTES])
            del self._pending[:FRAME_BYTES]
            out.extend(self._processor.process_10ms(frame))
        if out:
            rms = audioop.rms(bytes(out), WYOMING_WIDTH)
            now = time.monotonic()
            self._levels.append((now, 20 * math.log10(max(rms, 1) / (MAX_SAMPLE + 1))))
            while self._levels and now - self._levels[0][0] > self.STATS_WINDOW_S:
                self._levels.popleft()
        return bytes(out)

    def _remove_dc(self, pcm: bytes) -> bytes:
        """Subtract the (slowly tracked) mean of the signal."""
        mean = audioop.avg(pcm, WYOMING_WIDTH)
        if self._dc is None:  # start from the first chunk, then track drifts
            self._dc = float(mean)
        samples = len(pcm) // WYOMING_WIDTH
        weight = min(1.0, samples / (WYOMING_RATE * DC_TIME_CONSTANT_S))
        self._dc += (mean - self._dc) * weight
        offset = round(self._dc)
        if abs(offset) < 2:
            return pcm
        # audioop.bias wraps around on overflow: skip the (rare) chunks that
        # would overflow rather than distort them
        low, high = audioop.minmax(pcm, WYOMING_WIDTH)
        if low - offset < -MAX_SAMPLE - 1 or high - offset > MAX_SAMPLE:
            return pcm
        return audioop.bias(pcm, WYOMING_WIDTH, -offset)

    def noise_floor_db(self) -> Optional[float]:
        """Background noise of the cleaned audio over the last seconds."""
        if not self._levels or time.monotonic() - self._levels[-1][0] > 2:
            return None
        levels = sorted(db for _, db in self._levels)
        return round(levels[len(levels) // 10], 1)
