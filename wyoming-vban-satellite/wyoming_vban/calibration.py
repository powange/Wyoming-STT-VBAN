"""Microphone calibration: live levels, noise/voice analysis, then
proposals for the volume, the noise suppression level and the auto gain.

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

from .config import MIC_VOLUME_RANGE, NOISE_SUPPRESSION_LEVELS
from .const import WYOMING_RATE, WYOMING_WIDTH
from .mic_filter import AutoGain, MicFilter

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
# Noise suppression: frames this close to the noise count as quiet passages
QUIET_MARGIN_DB = 3.0
QUIET_GUARD_FRAMES = 3  # and this far from speech (noise suppression delays the audio)
QUIET_ENOUGH_DB = -55.0  # background under this: no need for more suppression
SNR_GAIN_DB = 6.0  # a level gaining this much on voice/noise is worth it
SNR_MIN_GAIN_DB = 3.0  # less than this on every level: off
VOICE_LOSS_DB = 3.0  # a level lowering the voice this much eats it
# Auto gain: near / far voice gap above this needs it
AUTO_GAIN_GAP_DB = 10.0
AUTO_GAIN_TARGET_BOUNDS = (-24.0, -14.0)  # target proposed: the near take, bounded
FAR_TOO_QUIET_DB = -45.0  # far voice under this even with the auto gain


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


def remove_dc(pcm: bytes) -> bytes:
    """The recording without its DC offset (a constant shift, common on
    ESP32 microphones, that isn't heard but would count as noise)."""
    if not pcm:
        return pcm
    mean = audioop.avg(pcm, WYOMING_WIDTH)
    if abs(mean) < 2:
        return pcm
    # audioop.add saturates (bias would wrap around near full scale)
    return audioop.add(pcm, audioop.bias(bytes(len(pcm)), WYOMING_WIDTH, -mean), WYOMING_WIDTH)


def analyze_noise(pcm: bytes) -> dict[str, float]:
    """Background noise: mean RMS of the recording."""
    return {"rms_db": round(_db(audioop.rms(remove_dc(pcm), WYOMING_WIDTH)), 1)}


def analyze_voice(pcm: bytes, noise_db: float) -> dict[str, float]:
    """Voice: RMS of the spoken frames (louder than the noise), peak, clipping
    (on the audio as received)."""
    clipped = _clipped(pcm)
    pcm = remove_dc(pcm)
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
        "clip_pct": round(100 * clipped / samples, 2),
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


def _frames(pcm: bytes) -> list[bytes]:
    return [pcm[i:i + FRAME_BYTES] for i in range(0, len(pcm) - FRAME_BYTES + 1, FRAME_BYTES)]


def _rms_db(pcm: bytes) -> Optional[float]:
    return round(_db(audioop.rms(pcm, WYOMING_WIDTH)), 1) if pcm else None


def frame_classes(raw: bytes, noise_db: float) -> tuple[list[int], list[int]]:
    """Indexes of the spoken frames and of the quiet frames of a take, from
    the received audio (the same frames are then measured after processing)."""
    levels = [_db(audioop.rms(f, WYOMING_WIDTH)) for f in _frames(remove_dc(raw))]
    speech = [i for i, db in enumerate(levels) if db >= noise_db + SPEECH_MARGIN_DB]
    if not speech:  # nothing above the noise: the loudest third
        loudest = sorted(range(len(levels)), key=lambda i: levels[i])
        speech = sorted(loudest[-max(1, len(levels) // 3):])
    loud = [db >= noise_db + QUIET_MARGIN_DB for db in levels]
    quiet = [
        i for i in range(len(levels))
        if not any(loud[max(0, i - QUIET_GUARD_FRAMES):i + QUIET_GUARD_FRAMES + 1])
    ]
    return speech, quiet


def _level_at(pcm: bytes, indexes: list[int]) -> Optional[float]:
    frames = _frames(pcm)
    return _rms_db(b"".join(frames[i] for i in indexes if i < len(frames)))


def run_filter(mic_filter: MicFilter, pcm: bytes) -> bytes:
    """Feed a recording in chunks like the live stream (the auto gain
    follows the level chunk by chunk)."""
    chunk = 1024  # 32 ms, about a VBAN packet pair
    return b"".join(mic_filter.process(pcm[i:i + chunk]) for i in range(0, len(pcm), chunk))


def process_take(
    noise_pcm: bytes, raw: bytes, noise_suppression: str, volume: float,
    auto_gain: bool = False, auto_gain_target_db: float = AutoGain.TARGET_DB,
) -> bytes:
    """A take processed offline like the satellite would, on a new filter:
    it learns the background noise first, then (auto gain) the take once,
    like a gain settled by earlier sentences."""
    mic_filter = MicFilter(noise_suppression, auto_gain, volume, auto_gain_target_db)
    run_filter(mic_filter, noise_pcm)
    if auto_gain:
        run_filter(mic_filter, raw)
    return run_filter(mic_filter, raw)


def compare_noise_levels(
    noise_pcm: bytes, voice_pcm: bytes, noise_db: float, volume: float,
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    """The voice take through each noise suppression level, with the volume:
    background left (quiet passages), voice level, voice/noise ratio.
    Returns (one row per level, processed voice per level)."""
    speech, quiet = frame_classes(voice_pcm, noise_db)
    rows, previews = [], {}
    for level in NOISE_SUPPRESSION_LEVELS:
        mic_filter = MicFilter(level, False, volume)
        noise_out = run_filter(mic_filter, noise_pcm)
        voice_out = run_filter(mic_filter, voice_pcm)
        # Quiet passages: the noise take once learned (its second half), and
        # the pauses of the voice take
        frames = _frames(voice_out)
        quiet_pcm = noise_out[len(noise_out) // 2:] + b"".join(
            frames[i] for i in quiet if i < len(frames)
        )
        background = _rms_db(quiet_pcm)
        voice = _level_at(voice_out, speech)
        rows.append({
            "level": level, "noise_db": background, "voice_db": voice,
            "snr_db": round(voice - background, 1)
            if voice is not None and background is not None else None,
        })
        previews[level] = voice_out
    off_voice = rows[0]["voice_db"]
    for row in rows:
        loss = (off_voice - row["voice_db"]) if None not in (off_voice, row["voice_db"]) else 0
        row["voice_loss_db"] = round(loss, 1)
        row["eats_voice"] = loss > VOICE_LOSS_DB
    return rows, previews


def choose_noise_level(rows: list[dict[str, Any]]) -> str:
    """The weakest level bringing the background under QUIET_ENOUGH_DB or
    gaining SNR_GAIN_DB on voice/noise; off if none gains SNR_MIN_GAIN_DB.
    Levels eating the voice are left out."""
    off = rows[0]
    usable = [r for r in rows if r["snr_db"] is not None and not r["eats_voice"]]
    if off["snr_db"] is None or not usable:
        return "off"
    for row in usable:
        if row["noise_db"] < QUIET_ENOUGH_DB or row["snr_db"] - off["snr_db"] >= SNR_GAIN_DB:
            return row["level"]
    best = max(usable, key=lambda r: r["snr_db"])
    return best["level"] if best["snr_db"] - off["snr_db"] > SNR_MIN_GAIN_DB else "off"


def noise_advice(rows: list[dict[str, Any]]) -> list[str]:
    names = {"light": "Légère", "medium": "Moyenne", "strong": "Forte"}
    eaten = [names[r["level"]] for r in rows if r["eats_voice"]]
    if not eaten:
        return []
    return [
        f"{', '.join(eaten)} : baisse la voix de plus de {VOICE_LOSS_DB:.0f} dB, la "
        "réduction de bruit grignote la voix (niveau écarté de la recommandation)."
    ]


def take_level(noise_pcm: bytes, raw: bytes, noise_db: float, noise_suppression: str,
               volume: float) -> Optional[float]:
    """Voice level of a take after the volume and the noise suppression."""
    speech, _ = frame_classes(raw, noise_db)
    return _level_at(process_take(noise_pcm, raw, noise_suppression, volume), speech)


def autogain_decision(near_db: float, far_db: float) -> dict[str, Any]:
    """Auto gain on when the near and far voices differ by more than
    AUTO_GAIN_GAP_DB, aiming at the near voice (bounded)."""
    gap = round(near_db - far_db, 1)
    low, high = AUTO_GAIN_TARGET_BOUNDS
    target = round(max(low, min(high, near_db)))
    far_max = round(far_db + AutoGain.MAX_GAIN_DB, 1)
    advice = []
    if far_max < FAR_TOO_QUIET_DB:
        advice.append(
            f"De loin, la voix resterait à {far_max:.0f} dBFS même avec le gain "
            f"automatique (+{AutoGain.MAX_GAIN_DB:.0f} dB au plus) : le niveau de base "
            "est trop bas, montez le gain dans ESPHome (mic_gain du codec ou gain du "
            "vban_sender), puis recommencez le calibrage."
        )
    return {
        "near_db": near_db, "far_db": far_db, "gap_db": gap,
        "auto_gain": gap > AUTO_GAIN_GAP_DB, "target_db": target,
        "far_max_db": far_max, "advice": advice,
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
    # Noise suppression: the voice take through each level
    denoise: Optional[dict[str, Any]] = None
    denoise_pcm: dict[str, bytes] = field(default_factory=dict)
    # Auto gain: near and far takes, and before / after the auto gain
    takes: dict[str, bytes] = field(default_factory=dict)  # "near", "far"
    take_levels: dict[str, Optional[float]] = field(default_factory=dict)
    autogain: Optional[dict[str, Any]] = None
    autogain_pcm: dict[str, bytes] = field(default_factory=dict)  # "near-after"…
    wake: Optional[dict[str, Any]] = None  # wake word test result (no audio kept)

    def audio(self, which: str) -> bytes:
        """A recording to listen to: before, after, denoise-<level>,
        near-before, near-after, far-before, far-after."""
        if which == "before":
            return self.voice_pcm
        if which == "after":
            return self.voice_after_pcm
        if which.startswith("denoise-"):
            return self.denoise_pcm.get(which[len("denoise-"):], b"")
        return self.autogain_pcm.get(which, b"")

    def state(self) -> dict[str, Any]:
        """What the page shows, to pick the calibration up again (reload)."""
        return {
            "noise": self.noise,
            "result": {"noise": self.noise, "voice": self.voice, "proposal": self.proposal}
            if self.proposal is not None else None,
            "denoise": self.denoise,
            "takes": dict(self.take_levels),
            "autogain": self.autogain,
            "wake": self.wake,
        }

    def touch(self) -> None:
        """Used: the expiry counts from the last action."""
        self.started = time.monotonic()

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.started > self.EXPIRY_S
