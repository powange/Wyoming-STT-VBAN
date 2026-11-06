"""Wake word clips: what openWakeWord heard each time the wake word fired.

Each detection (a wake run, not a follow-up started at ASR) keeps the audio
sent to Home Assistant from 2.5 s before to 0.5 s after it, with what
followed (transcript, response). The user sorts them in the interface:
good wake word, false trigger, or ignored. The sorted clips are copied to
WakeForge's inbox (/share/wakeforge/inbox/<wake word>/positifs|negatifs),
listed in its manifest.json, for training.

The app never classifies by itself: it only suggests ("suggestion").
"""

import audioop
import collections
import hashlib
import json
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from .const import WYOMING_RATE, WYOMING_WIDTH
from .media_player import build_wav

_LOGGER = logging.getLogger(__name__)

BEFORE_S = 2.5  # audio kept before the detection
AFTER_S = 0.5  # and after it
STATUSES = ("non_classe", "bon", "faux", "ignore")
INBOX_FOLDERS = {"bon": "positifs", "faux": "negatifs"}
# Transcripts of a wake word that wasn't one (nothing said after it)
EMPTY_TRANSCRIPTS = {"", "...", "…", "*bip*", "bip", "[blank_audio]"}


def wake_word_folder(wake_word: str) -> str:
    """WakeForge project name: the model without its version suffix
    (dis_moi_jarvis-6 → dis_moi_jarvis)."""
    name = re.sub(r"-\d+$", "", wake_word or "").strip()
    return re.sub(r"[^\w-]+", "_", name) or "inconnu"


def suggestion(meta: dict[str, Any]) -> str:
    """probablement_faux when nothing was said after the wake word."""
    if meta.get("stt_error") == "stt-no-text-recognized":
        return "probablement_faux"
    transcript = meta.get("transcript")
    if transcript is not None and transcript.strip().lower() in EMPTY_TRANSCRIPTS:
        return "probablement_faux"
    return "a_verifier"


def _db(value: float) -> float:
    return round(20 * math.log10(max(value, 1) / 32768), 1)


def peaks(pcm: bytes, count: int = 120) -> list[float]:
    """Waveform for the interface: peak of each slice, 0 to 1."""
    if not pcm:
        return []
    step = max(WYOMING_WIDTH, len(pcm) // count // WYOMING_WIDTH * WYOMING_WIDTH)
    return [round(audioop.max(pcm[i:i + step], WYOMING_WIDTH) / 32768, 3)
            for i in range(0, len(pcm) - WYOMING_WIDTH + 1, step)][:count]


class ClipStore:
    """Clips in the app's data folder; sorted ones mirrored to the inbox."""

    def __init__(
        self, root: str = "/data/wake_clips", inbox: str = "/share/wakeforge/inbox",
        retention_days: int = 30, max_clips: int = 2000,
    ):
        self.root = Path(root)
        self.inbox = Path(inbox)
        self.retention_days = retention_days
        self.max_clips = max_clips
        self._clips: dict[str, dict[str, Any]] = {}  # id -> metadata
        self._load()

    # Storage

    def _load(self) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for path in self.root.glob("*.json"):
                try:
                    meta = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if (self.root / f"{meta.get('id')}.wav").exists():
                    self._clips[meta["id"]] = meta
        except OSError as err:
            _LOGGER.error("Wake word clips folder %s unusable: %s", self.root, err)

    def _write_meta(self, meta: dict[str, Any]) -> None:
        path = self.root / f"{meta['id']}.json"
        partial = path.with_suffix(".part")
        partial.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(partial, path)

    def wav_path(self, clip_id: str) -> Path:
        return self.root / f"{clip_id}.wav"

    # Clips

    def add(self, satellite: str, wake_word: str, pcm: bytes, **meta: Any) -> str:
        """A new clip (16 kHz mono 16-bit); returns its id."""
        now = time.time()
        clip_id = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-" + uuid.uuid4().hex[:6]
        info = {
            "id": clip_id, "satellite": satellite, "time": now,
            "wake_word": wake_word or "", "status": "non_classe",
            "rms_db": _db(audioop.rms(pcm, WYOMING_WIDTH)) if pcm else None,
            "peak_db": _db(audioop.max(pcm, WYOMING_WIDTH)) if pcm else None,
            "duration_s": round(len(pcm) / WYOMING_WIDTH / WYOMING_RATE, 2),
            "transcript": None, "response": None, "stt_error": None,
            "peaks": peaks(pcm),
        } | meta
        info["suggestion"] = suggestion(info)
        self.root.mkdir(parents=True, exist_ok=True)
        self.wav_path(clip_id).write_bytes(build_wav(pcm, WYOMING_RATE, WYOMING_WIDTH, 1))
        self._write_meta(info)
        self._clips[clip_id] = info
        self.purge()
        return clip_id

    def update(self, clip_id: str, **fields: Any) -> None:
        """What followed the detection (transcript, response, STT error)."""
        info = self._clips.get(clip_id)
        if info is None:
            return
        info.update(fields)
        info["suggestion"] = suggestion(info)
        self._write_meta(info)
        if info["status"] in INBOX_FOLDERS:
            self._write_manifest(wake_word_folder(info["wake_word"]))

    def classify(self, clip_id: str, status: str) -> dict[str, Any]:
        """Sort a clip; the inbox follows (moved when changed, removed when
        ignored or unsorted). KeyError / ValueError if unknown."""
        if status not in STATUSES:
            raise ValueError(status)
        info = self._clips[clip_id]
        info["status"] = status
        info["classified_at"] = time.time() if status != "non_classe" else None
        self._write_meta(info)
        self._sync_inbox(info)
        return info

    def last(self, satellite: str) -> Optional[dict[str, Any]]:
        clips = [c for c in self._clips.values() if c["satellite"] == satellite]
        return max(clips, key=lambda c: c["time"]) if clips else None

    def list(self, satellite: str = "", status: str = "") -> list[dict[str, Any]]:
        clips = [
            c for c in self._clips.values()
            if (not satellite or c["satellite"] == satellite)
            and (not status or c["status"] == status)
        ]
        return sorted(clips, key=lambda c: c["time"], reverse=True)

    def counts(self) -> dict[str, dict[str, dict[str, int]]]:
        """day -> satellite -> {bon, faux, non_classe, ignore}."""
        days: dict[str, dict[str, dict[str, int]]] = {}
        for clip in self._clips.values():
            day = time.strftime("%Y-%m-%d", time.localtime(clip["time"]))
            room = days.setdefault(day, {}).setdefault(
                clip["satellite"], {s: 0 for s in STATUSES})
            room[clip["status"]] += 1
        return days

    # Retention

    def purge(self) -> None:
        """Older than retention_days: unsorted and ignored clips go (sorted
        ones are training data). Over max_clips: unsorted first, then
        ignored, then the oldest sorted ones."""
        limit = time.time() - self.retention_days * 86400
        for clip in list(self._clips.values()):
            if clip["time"] < limit and clip["status"] in ("non_classe", "ignore"):
                self._delete(clip)
        excess = len(self._clips) - self.max_clips
        if excess <= 0:
            return
        order = {"non_classe": 0, "ignore": 1, "faux": 2, "bon": 2}
        for clip in sorted(self._clips.values(), key=lambda c: (order[c["status"]], c["time"]))[:excess]:
            self._delete(clip)

    def _delete(self, clip: dict[str, Any]) -> None:
        self._clips.pop(clip["id"], None)
        for path in (self.wav_path(clip["id"]), self.root / f"{clip['id']}.json"):
            path.unlink(missing_ok=True)
        if clip["status"] in INBOX_FOLDERS:
            clip["status"] = "ignore"  # removed from the inbox too
            self._sync_inbox(clip)

    # WakeForge inbox

    def _inbox_name(self, info: dict[str, Any]) -> str:
        satellite = re.sub(r"[^\w-]+", "_", info["satellite"]).strip("_") or "satellite"
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(info["time"]))
        return f"{satellite}_{stamp}_{info['id'][-6:]}"

    def _sync_inbox(self, info: dict[str, Any]) -> None:
        folder = self.inbox / wake_word_folder(info["wake_word"])
        name = self._inbox_name(info)
        try:
            for status, sub in INBOX_FOLDERS.items():
                if status == info["status"]:
                    continue
                for ext in (".wav", ".json"):
                    (folder / sub / f"{name}{ext}").unlink(missing_ok=True)
            if info["status"] in INBOX_FOLDERS:
                target = folder / INBOX_FOLDERS[info["status"]]
                target.mkdir(parents=True, exist_ok=True)
                wav = self.wav_path(info["id"]).read_bytes()
                (target / f"{name}.wav").write_bytes(wav)
                (target / f"{name}.json").write_text(json.dumps(
                    self._public(info), ensure_ascii=False, indent=1), encoding="utf-8")
            self._write_manifest(wake_word_folder(info["wake_word"]))
        except OSError as err:
            _LOGGER.error("WakeForge inbox %s not updated: %s", folder, err)

    @staticmethod
    def _public(info: dict[str, Any]) -> dict[str, Any]:
        """Clip metadata for WakeForge (no waveform)."""
        return {k: v for k, v in info.items() if k != "peaks"} | {
            "date": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(info["time"])),
            "sample_rate": WYOMING_RATE,
        }

    def _write_manifest(self, wake_word: str) -> None:
        folder = self.inbox / wake_word
        entries = []
        for info in sorted(self._clips.values(), key=lambda c: c["time"]):
            if info["status"] not in INBOX_FOLDERS or wake_word_folder(info["wake_word"]) != wake_word:
                continue
            path = f"{INBOX_FOLDERS[info['status']]}/{self._inbox_name(info)}.wav"
            try:
                digest = hashlib.sha256((folder / path).read_bytes()).hexdigest()
            except OSError:
                continue
            entries.append({
                "path": path, "status": info["status"], "sha256": digest,
                "satellite": info["satellite"],
                "date": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(info["time"])),
                "transcript": info.get("transcript"),
            })
        folder.mkdir(parents=True, exist_ok=True)
        partial = folder / "manifest.part"
        partial.write_text(json.dumps({"wake_word": wake_word, "clips": entries},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(partial, folder / "manifest.json")


class ClipRecorder:
    """Audio sent to HA by one satellite (ring buffer) and its clips.

    Positions are counted in samples sent, so a gap in the microphone stream
    (Atom muted while it plays) doesn't shorten a clip: openWakeWord heard a
    continuous stream.
    """

    KEEP_S = 6.0
    DEFAULT_LATENCY_S = 0.3  # detection received after the audio was sent
    LATENCY_RANGE = (0.0, 2.0)

    def __init__(self, store: ClipStore, satellite: str):
        self.store = store
        self.satellite = satellite
        # (first sample index, time sent, audio)
        self._chunks: collections.deque[tuple[int, float, bytes]] = collections.deque()
        self.samples_sent = 0
        self.latency_s = self.DEFAULT_LATENCY_S

    def sent(self, pcm: bytes, at: Optional[float] = None) -> None:
        """A chunk sent to HA."""
        at = time.monotonic() if at is None else at
        self._chunks.append((self.samples_sent, at, pcm))
        self.samples_sent += len(pcm) // WYOMING_WIDTH
        keep = int(self.KEEP_S * WYOMING_RATE)
        while self._chunks and self.samples_sent - self._chunks[0][0] > keep:
            self._chunks.popleft()

    def sample_at(self, moment: float) -> int:
        """Index of the sample sent at that moment (monotonic time)."""
        index = self._chunks[0][0] if self._chunks else self.samples_sent
        for first, at, pcm in self._chunks:
            if at > moment:
                break
            index = first + len(pcm) // WYOMING_WIDTH
        return index

    def time_of(self, sample: int) -> Optional[float]:
        for first, at, pcm in self._chunks:
            if first <= sample < first + len(pcm) // WYOMING_WIDTH:
                return at
        return None

    def learn_latency(self, sample: int, received_at: float) -> None:
        """A detection placed by its timestamp: how late it came."""
        sent_at = self.time_of(sample)
        if sent_at is None:
            return
        low, high = self.LATENCY_RANGE
        latency = max(low, min(high, received_at - sent_at))
        self.latency_s += (latency - self.latency_s) * 0.3

    def extract(self, sample: int) -> bytes:
        """BEFORE_S + AFTER_S of sent audio around a sample (silence where
        nothing was kept)."""
        start = sample - int(BEFORE_S * WYOMING_RATE)
        end = sample + int(AFTER_S * WYOMING_RATE)
        out = bytearray((end - start) * WYOMING_WIDTH)
        for first, _, pcm in self._chunks:
            count = len(pcm) // WYOMING_WIDTH
            lo, hi = max(start, first), min(end, first + count)
            if lo < hi:
                out[(lo - start) * WYOMING_WIDTH:(hi - start) * WYOMING_WIDTH] = \
                    pcm[(lo - first) * WYOMING_WIDTH:(hi - first) * WYOMING_WIDTH]
        return bytes(out)
