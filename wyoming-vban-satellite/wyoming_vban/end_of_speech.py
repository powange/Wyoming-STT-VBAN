"""End of speech detected by the app during the STT stage.

Home Assistant's own VAD ends the speech about 1.7 s after the last word on
a Wyoming satellite (its "end of speech detection" setting doesn't seem to
apply to them). With this on, the satellite follows the speech on the audio
it sends to HA and sends audio-stop itself: at least MIN_SPEECH_S of
speech, then silence_ms of silence, never before min_listen_ms of listening.
With no speech at all nothing is sent: HA ends the run itself
(stt-no-text-recognized). HA's VAD stays the safety net.

A light VAD: 30 ms frames are speech when their level is SPEECH_MARGIN_DB
above the background noise. The background follows the quietest frames:
it drops at once to a quieter frame, rises slowly (FLOOR_RISE_DB_PER_S, so
a long sentence barely moves it while a lasting new noise is learnt).
"""

from __future__ import annotations

import audioop
import math
from dataclasses import dataclass
from typing import Optional

from .const import WYOMING_RATE, WYOMING_WIDTH


@dataclass
class EndOfSpeechSettings:
    """Per satellite (microphone.end_of_speech*); changed live."""

    enabled: bool = False
    silence_ms: int = 500
    min_listen_ms: int = 1000


class EndOfSpeechDetector:
    FRAME_S = 0.03
    FRAME_BYTES = int(WYOMING_RATE * FRAME_S) * WYOMING_WIDTH
    SPEECH_MARGIN_DB = 10.0
    MIN_SPEECH_S = 0.3
    FLOOR_RISE_DB_PER_S = 1.0
    MIN_FLOOR_DB = -70.0  # digital silence: a whisper of noise isn't speech

    def __init__(self) -> None:
        self.floor_db: Optional[float] = None  # background noise (dBFS)
        self.armed = False
        self._pending = bytearray()  # < one frame, for the next call
        self._silence_ms = 500
        self._min_listen_s = 1.0
        self.speech_s = 0.0
        self.silence_s = 0.0
        self.listened_s = 0.0

    @property
    def threshold_db(self) -> Optional[float]:
        """Level above which a frame is speech."""
        if self.floor_db is None:
            return None
        return max(self.floor_db, self.MIN_FLOOR_DB) + self.SPEECH_MARGIN_DB

    def arm(self, settings: EndOfSpeechSettings) -> None:
        """The STT stage starts: follow this utterance."""
        self.armed = True
        self._silence_ms = settings.silence_ms
        self._min_listen_s = settings.min_listen_ms / 1000
        self.speech_s = self.silence_s = self.listened_s = 0.0

    def disarm(self) -> None:
        self.armed = False

    def feed(self, pcm: bytes) -> Optional[int]:
        """Audio sent to HA (16 kHz mono 16-bit), never substituted silence.

        Returns, once the end of speech is reached, the number of bytes of
        pcm up to it (the audio after it isn't needed); then disarmed.
        Always learns the background noise."""
        start = len(self._pending)  # bytes of the first frame from before
        self._pending.extend(pcm)
        offset = -start
        while len(self._pending) >= self.FRAME_BYTES:
            frame = bytes(self._pending[:self.FRAME_BYTES])
            del self._pending[:self.FRAME_BYTES]
            offset += self.FRAME_BYTES
            if self._frame(frame):
                self._pending.clear()
                return max(0, min(len(pcm), offset))
        return None

    def _frame(self, frame: bytes) -> bool:
        level = 20 * math.log10(max(audioop.rms(frame, WYOMING_WIDTH), 1) / 32768)
        threshold = self.threshold_db
        speech = threshold is not None and level > threshold
        # Background: down at once, up slowly
        if self.floor_db is None or level < self.floor_db:
            self.floor_db = level
        else:
            self.floor_db += min(level - self.floor_db, self.FLOOR_RISE_DB_PER_S * self.FRAME_S)
        if not self.armed:
            return False
        self.listened_s += self.FRAME_S
        if speech:
            self.speech_s += self.FRAME_S
            self.silence_s = 0.0
            return False
        if self.speech_s > 0:
            self.silence_s += self.FRAME_S
        if (self.speech_s >= self.MIN_SPEECH_S
                and self.silence_s * 1000 >= self._silence_ms - 1e-6
                and self.listened_s >= self._min_listen_s - 1e-6):
            self.armed = False
            return True
        return False
