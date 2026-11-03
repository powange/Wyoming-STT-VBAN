"""Microphone calibration: live levels, noise/voice analysis, volume proposal.

Everything works on copies of the audio (16 kHz mono 16-bit): the stream
sent to Home Assistant is never interrupted. Recordings stay in memory.
"""

import array
import audioop
import collections
import math
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from .config import MIC_VOLUME_RANGE
from .const import WYOMING_RATE, WYOMING_WIDTH

FULL_SCALE = 32768
CLIP_THRESHOLD = 32700  # |sample| at or above: clipped
FRAME_BYTES = int(WYOMING_RATE * 0.03) * WYOMING_WIDTH  # 30 ms analysis frames
TARGET_RMS_DB = -20.0  # voice level aimed at
MAX_PEAK_DB = -3.0  # voice peaks must stay under this
SPEECH_MARGIN_DB = 6.0  # frames this much above the noise count as speech
QUIET_VOICE_DB = -40.0  # under this, the device gain is too low
CLIP_WARN_PCT = 0.1  # clipped samples (%) before the app gain: too loud
LOW_SNR_DB = 15.0
NOISY_AFTER_GAIN_DB = -45.0  # background this loud after gain: false wakes


def _db(value: float) -> float:
    return 20 * math.log10(max(value, 1) / FULL_SCALE)


def _clipped(pcm: bytes) -> int:
    """Number of samples at full scale (positive or negative)."""
    samples = array.array("h", pcm[: len(pcm) // WYOMING_WIDTH * WYOMING_WIDTH])
    return sum(1 for s in samples if s >= CLIP_THRESHOLD or s <= -CLIP_THRESHOLD)


class LevelMeter:
    """Levels of an audio stream over the last moments (live vu-meter)."""

    HISTORY_S = 2.0

    def __init__(self) -> None:
        # (time, sum of squares, samples, peak, clipped samples) per chunk
        self._chunks: collections.deque[tuple[float, float, int, int, int]] = (
            collections.deque()
        )

    def add(self, pcm: bytes) -> None:
        if len(pcm) < WYOMING_WIDTH:
            return
        samples = len(pcm) // WYOMING_WIDTH
        rms = audioop.rms(pcm, WYOMING_WIDTH)
        peak = audioop.max(pcm, WYOMING_WIDTH)
        clipped = _clipped(pcm) if peak >= CLIP_THRESHOLD else 0
        now = time.monotonic()
        self._chunks.append((now, rms * rms * samples, samples, peak, clipped))
        while self._chunks and now - self._chunks[0][0] > self.HISTORY_S:
            self._chunks.popleft()

    def stats(self, window_s: float = 0.1) -> Optional[dict[str, float]]:
        """RMS and peak (dBFS) and clipped samples (%) over the window."""
        now = time.monotonic()
        recent = [c for c in self._chunks if now - c[0] <= window_s]
        if not recent:
            return None
        samples = sum(c[2] for c in recent)
        return {
            "rms_db": round(_db(math.sqrt(sum(c[1] for c in recent) / samples)), 1),
            "peak_db": round(_db(max(c[3] for c in recent)), 1),
            "clip_pct": round(100 * sum(c[4] for c in recent) / samples, 2),
        }


def analyze_noise(pcm: bytes) -> dict[str, float]:
    """Background noise: mean RMS of the recording."""
    return {"rms_db": round(_db(audioop.rms(pcm, WYOMING_WIDTH)), 1)}


def analyze_voice(pcm: bytes, noise_db: float) -> dict[str, float]:
    """Voice: RMS of the spoken frames (louder than the noise), peak, clipping."""
    frames = [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]
    levels = [(_db(audioop.rms(f, WYOMING_WIDTH)), f) for f in frames]
    speech = [f for db, f in levels if db >= noise_db + SPEECH_MARGIN_DB]
    if not speech:  # nothing above the noise: the loudest third
        speech = [f for _, f in sorted(levels, key=lambda x: x[0])[-max(1, len(levels) // 3):]]
    voiced = b"".join(speech)
    samples = max(1, len(pcm) // WYOMING_WIDTH)
    return {
        "rms_db": round(_db(audioop.rms(voiced, WYOMING_WIDTH)), 1),
        "peak_db": round(_db(audioop.max(pcm, WYOMING_WIDTH)), 1),
        "clip_pct": round(100 * _clipped(pcm) / samples, 2),
        "speech_s": round(len(voiced) / WYOMING_WIDTH / WYOMING_RATE, 1),
    }


def propose(noise: dict[str, float], voice: dict[str, float]) -> dict[str, Any]:
    """Microphone volume for a voice at TARGET_RMS_DB with peaks under
    MAX_PEAK_DB, and plain-language advice on the device gain."""
    wanted_db = TARGET_RMS_DB - voice["rms_db"]
    gain_db = min(wanted_db, MAX_PEAK_DB - voice["peak_db"])
    low, high = MIC_VOLUME_RANGE
    volume = 10 ** (gain_db / 20)
    over_max = volume > high
    volume = round(min(high, max(low, volume)), 2)
    applied_db = 20 * math.log10(volume)
    advice = []
    if voice["clip_pct"] >= CLIP_WARN_PCT or voice["peak_db"] >= -0.5:
        advice.append(
            "La voix sature déjà à l'arrivée (avant le gain de l'app) : baissez le "
            "gain dans ESPHome (mic_gain du codec ou gain du vban_sender), puis "
            "recommencez le calibrage."
        )
    if voice["rms_db"] < QUIET_VOICE_DB or over_max:
        advice.append(
            "La voix arrive trop faible : montez le gain dans ESPHome (mic_gain du "
            "codec ou gain du vban_sender), puis recommencez le calibrage. Le "
            "volume du micro de l'app est limité à 10."
        )
    snr_db = round(voice["rms_db"] - noise["rms_db"], 1)
    if snr_db < LOW_SNR_DB:
        advice.append(
            f"Écart voix/bruit faible ({snr_db:.0f} dB) : le gain n'y change rien. "
            "Rapprochez le micro de l'endroit où vous parlez ou éloignez-le des "
            "sources de bruit, ou essayez la réduction de bruit."
        )
    noise_after = round(noise["rms_db"] + applied_db, 1)
    if noise_after > NOISY_AFTER_GAIN_DB:
        advice.append(
            f"Après ce gain, le bruit de fond arriverait à {noise_after:.0f} dB : "
            "risque de faux réveils. Préférez monter la voix à la source (gain "
            "ESPHome, micro plus proche) ou activez la réduction de bruit."
        )
    return {
        "volume": volume,
        "gain_db": round(applied_db, 1),
        "snr_db": snr_db,
        "voice_after_db": round(voice["rms_db"] + applied_db, 1),
        "peak_after_db": round(min(0.0, voice["peak_db"] + applied_db), 1),
        "noise_after_db": noise_after,
        "over_max": over_max,
        "advice": advice,
    }


@dataclass
class CalibrationSession:
    """One satellite's calibration recordings (in memory, short-lived)."""

    EXPIRY_S = 15 * 60

    started: float = field(default_factory=time.monotonic)
    noise_pcm: bytes = b""
    voice_pcm: bytes = b""
    voice_after_pcm: bytes = b""  # voice with the proposed volume + clean-up
    noise: Optional[dict[str, float]] = None
    voice: Optional[dict[str, float]] = None
    proposal: Optional[dict[str, Any]] = None

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.started > self.EXPIRY_S
