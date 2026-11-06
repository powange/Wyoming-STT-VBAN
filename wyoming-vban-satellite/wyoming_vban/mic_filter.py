"""Microphone clean-up before the audio is sent to Home Assistant.

16 kHz mono 16-bit in and out:
- DC offset removal (always): a constant shift, common on ESP32
  microphones, that isn't heard;
- microphone volume: a fixed gain (set by hand or by the calibration);
- optional noise suppression (Speex): learns the background noise during
  the quiet moments of the last seconds and attenuates it, frequency band by
  frequency band (works on steady noise: computer fan, fridge, hiss);
- optional automatic gain: brings the voice to a steady level (-18 dBFS,
  or the level set in Home Assistant). Our own: pyspeex-noise 2.0.1's
  automatic gain outputs silence.
"""

import audioop
import collections
import logging
import math
import time
from typing import Any, Optional

from pyspeex_noise import AudioProcessor

from .const import WYOMING_RATE, WYOMING_WIDTH

_LOGGER = logging.getLogger(__name__)

# Maximum attenuation of the noise, in dB (0 = off)
NOISE_LEVELS = {"off": 0, "light": -6, "medium": -15, "strong": -25}
FRAME_BYTES = WYOMING_RATE // 100 * WYOMING_WIDTH  # Speex works on 10 ms
DC_TIME_CONSTANT_S = 1.0
MAX_SAMPLE = (1 << (8 * WYOMING_WIDTH - 1)) - 1


def _db(pcm: bytes) -> float:
    return 20 * math.log10(max(audioop.rms(pcm, WYOMING_WIDTH), 1) / (MAX_SAMPLE + 1))


class AutoGain:
    """Brings speech to target_db; doesn't boost the background noise.

    The gain only follows the voice: chunks louder than the background noise
    by SPEECH_MARGIN_DB. It rises slowly and drops fast (no clipping).
    """

    TARGET_DB = -18.0
    MIN_GAIN_DB = -12.0
    MAX_GAIN_DB = 20.0
    SPEECH_MARGIN_DB = 10.0
    UP_DB_PER_S = 6.0
    DOWN_DB_PER_S = 40.0
    FLOOR_RISE_DB_PER_S = 3.0  # background estimate follows a rising noise

    def __init__(self, target_db: float = TARGET_DB) -> None:
        self.target_db = target_db
        self.gain_db = 0.0
        self._floor_db: Optional[float] = None

    def process(self, pcm: bytes) -> bytes:
        seconds = len(pcm) / WYOMING_WIDTH / WYOMING_RATE
        level = _db(pcm)
        if self._floor_db is None or level < self._floor_db:
            self._floor_db = level
        else:
            self._floor_db += self.FLOOR_RISE_DB_PER_S * seconds
        if level > self._floor_db + self.SPEECH_MARGIN_DB:
            target = min(self.MAX_GAIN_DB, max(self.MIN_GAIN_DB, self.target_db - level))
            if target > self.gain_db:
                self.gain_db = min(target, self.gain_db + self.UP_DB_PER_S * seconds)
            else:
                self.gain_db = max(target, self.gain_db - self.DOWN_DB_PER_S * seconds)
        if abs(self.gain_db) < 0.1:
            return pcm
        # audioop.mul clips at full scale instead of wrapping around
        return audioop.mul(pcm, WYOMING_WIDTH, 10 ** (self.gain_db / 20))


class MicFilter:
    """Cleans a satellite's microphone audio; settings can change live."""

    STATS_WINDOW_S = 10.0
    # Safety net: cleaned audio silent while the microphone isn't, for this
    # long → clean-up bypassed (HA must never be left with silence)
    SILENT_OUTPUT_S = 3.0
    SILENT_DB = -85.0

    def __init__(
        self, noise_suppression: str = "off", auto_gain: bool = False, volume: float = 1.0,
        auto_gain_target_db: float = AutoGain.TARGET_DB,
    ):
        self.noise_suppression = "off"
        self.auto_gain = False
        self.volume = 1.0
        self.auto_gain_target_db = AutoGain.TARGET_DB
        self._speex: Optional[AudioProcessor] = None
        self._agc: Optional[AutoGain] = None
        self._pending = bytearray()  # < 10 ms waiting for the next call
        self._silent_s = 0.0  # cleaned audio silent while the input isn't
        self.bypassed = False
        self._dc: Optional[float] = None  # tracked mean of the signal
        # (time, level dB) of the cleaned audio, for the status panel
        self._levels: collections.deque[tuple[float, float]] = collections.deque()
        # Optional live meter of what is sent to HA (calibration)
        self.output_meter: Optional[Any] = None
        self.configure(noise_suppression, auto_gain, volume, auto_gain_target_db)

    @property
    def active(self) -> bool:
        """Volume, noise suppression or automatic gain changes the audio."""
        return self._speex is not None or self._agc is not None or self.volume != 1.0

    def configure(
        self, noise_suppression: str, auto_gain: bool, volume: float = 1.0,
        auto_gain_target_db: float = AutoGain.TARGET_DB,
    ) -> None:
        if (noise_suppression, auto_gain, volume, auto_gain_target_db) == (
            self.noise_suppression, self.auto_gain, self.volume, self.auto_gain_target_db
        ):
            return
        self.noise_suppression = noise_suppression
        self.auto_gain = auto_gain
        self.volume = volume
        self.auto_gain_target_db = auto_gain_target_db
        level = NOISE_LEVELS.get(noise_suppression, 0)
        # Speex for noise suppression only (its automatic gain is broken)
        self._speex = AudioProcessor(0, level) if level else None
        self._agc = AutoGain(auto_gain_target_db) if auto_gain else None
        self._pending.clear()
        self._levels.clear()
        self._silent_s = 0.0
        self.bypassed = False

    def process(self, pcm: bytes) -> bytes:
        """Clean a chunk. With noise suppression on, up to 10 ms is held for
        the next call (the output length may differ by a frame)."""
        out = self._process(pcm)
        if self.output_meter is not None and out:
            self.output_meter.add(out)
        return out

    def _process(self, pcm: bytes) -> bytes:
        if not pcm:
            return pcm
        pcm = self._remove_dc(pcm)
        if not self.active or self.bypassed:
            return pcm
        raw = pcm
        if self.volume != 1.0:
            # audioop.mul clips at full scale instead of wrapping around
            pcm = audioop.mul(pcm, WYOMING_WIDTH, self.volume)
        if self._speex is not None:
            self._pending.extend(pcm)
            out = bytearray()
            while len(self._pending) >= FRAME_BYTES:
                frame = bytes(self._pending[:FRAME_BYTES])
                del self._pending[:FRAME_BYTES]
                out.extend(self._speex.process_10ms(frame))
            pcm = bytes(out)
        if self._agc is not None and pcm:
            pcm = self._agc.process(pcm)
        if pcm and self._output_stuck_silent(raw, pcm):
            return raw
        if pcm:
            now = time.monotonic()
            self._levels.append((now, _db(pcm)))
            while self._levels and now - self._levels[0][0] > self.STATS_WINDOW_S:
                self._levels.popleft()
        return pcm

    def _output_stuck_silent(self, raw: bytes, cleaned: bytes) -> bool:
        """Bypass the clean-up if it has been silencing a live microphone."""
        if _db(cleaned) < self.SILENT_DB and _db(raw) > self.SILENT_DB + 20:
            self._silent_s += len(raw) / WYOMING_WIDTH / WYOMING_RATE
        else:
            self._silent_s = 0.0
        if self._silent_s < self.SILENT_OUTPUT_S:
            return False
        self.bypassed = True
        _LOGGER.error(
            "Microphone clean-up (noise suppression %s, auto gain %s) outputs "
            "silence: bypassed, raw audio sent to Home Assistant",
            self.noise_suppression, "on" if self.auto_gain else "off",
        )
        return True

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

    def level_db(self) -> Optional[float]:
        """Level of the cleaned audio now (peak over the last second)."""
        now = time.monotonic()
        recent = [db for t, db in self._levels if now - t <= 1.0]
        return round(max(recent), 1) if recent else None

    def noise_floor_db(self) -> Optional[float]:
        """Background noise of the cleaned audio over the last seconds."""
        if not self._levels or time.monotonic() - self._levels[-1][0] > 2:
            return None
        levels = sorted(db for _, db in self._levels)
        return round(levels[len(levels) // 10], 1)
