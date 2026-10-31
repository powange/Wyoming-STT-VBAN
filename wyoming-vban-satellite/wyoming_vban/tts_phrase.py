"""Spoken phrases synthesized by Home Assistant's TTS, cached on disk.

A phrase (e.g. "Attends, je regarde.") is synthesized once with
POST /api/tts_get_url (through the Supervisor proxy) and kept as a WAV in
/data, so it plays immediately when needed. Changing the text, engine or
voice gives a new cache file.
"""

import asyncio
import hashlib
import io
import logging
import os
import wave
from pathlib import Path
from typing import Optional

import aiohttp

from .satellite import Cue

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_CORE_URL = "http://supervisor/core"


class TtsPhrase:
    """A phrase synthesized in the background; `cue` is None until ready."""

    RETRY_S = 30.0  # HA may not be up yet when the app starts
    RETRY_MAX_S = 600.0  # retries slow down up to this
    REQUEST_TIMEOUT_S = 30.0
    CACHE_PREFIX = "tts_phrase_"

    def __init__(
        self,
        text: str,
        engine: str,
        voice: str,
        cache_dir: str = "/data",
        base_url: str = SUPERVISOR_CORE_URL,
    ):
        self.text = text
        self.engine = engine
        self.voice = voice
        self.base_url = base_url
        self.cue: Optional[Cue] = None
        key = hashlib.sha1(f"{engine}|{voice}|{text}".encode()).hexdigest()[:16]
        # e.g. /data/annonces/tts_phrase_<hash of text + engine + voice>.wav
        self.cache_path = Path(cache_dir) / f"{self.CACHE_PREFIX}{key}.wav"
        self._token = os.environ.get("SUPERVISOR_TOKEN", "")

    def load_cached(self) -> bool:
        """Use the cached WAV if present. Returns True if loaded."""
        try:
            self.cue = Cue.from_wav(str(self.cache_path))
        except (OSError, EOFError, wave.Error):
            return False
        _LOGGER.info("Phrase '%s' loaded from cache (%.2fs)", self.text, self.cue.duration)
        return True

    async def run(self) -> None:
        """Synthesize the phrase (retrying) unless it is already cached."""
        if self.load_cached():
            return
        retry = self.RETRY_S
        failures = 0
        async with aiohttp.ClientSession() as session:
            while self.cue is None:
                try:
                    wav = await self._synthesize(session)
                    self.cue = _cue_from_wav_bytes(wav)
                    _LOGGER.info(
                        "Phrase '%s' synthesized with %s / %s (%.2fs)",
                        self.text, self.engine, self.voice, self.cue.duration,
                    )
                    self._save(wav)
                    return
                except (aiohttp.ClientError, asyncio.TimeoutError, OSError,
                        EOFError, wave.Error, ValueError) as err:
                    failures += 1
                    # Clear error once (e.g. wrong engine/voice), then quieter
                    (_LOGGER.error if failures == 1 else _LOGGER.warning)(
                        "Cannot synthesize phrase '%s' with %s / %s (%s): the "
                        "built-in sound is played instead. Retrying in %.0fs",
                        self.text, self.engine, self.voice,
                        err or type(err).__name__, retry,
                    )
                await asyncio.sleep(retry)
                retry = min(retry * 2, self.RETRY_MAX_S)

    async def _synthesize(self, session: aiohttp.ClientSession) -> bytes:
        headers = {"Authorization": f"Bearer {self._token}"}
        timeout = aiohttp.ClientTimeout(total=self.REQUEST_TIMEOUT_S)
        request = {
            "engine_id": self.engine,
            "message": self.text,
            "options": {
                "voice": self.voice,
                # Ask for 16-bit mono WAV (converted by HA if needed)
                "preferred_format": "wav",
                "preferred_sample_rate": 22050,
                "preferred_sample_channels": 1,
                "preferred_sample_bytes": 2,
            },
        }
        # Piper voices are named <language>-<name>-<quality>
        if "_" in (language := self.voice.split("-", 1)[0]):
            request["language"] = language
        async with session.post(
            f"{self.base_url}/api/tts_get_url", json=request, headers=headers,
            timeout=timeout,
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"tts_get_url HTTP {resp.status}: {(await resp.text())[:200]}")
            path = (await resp.json())["path"]
        async with session.get(
            f"{self.base_url}{path}", headers=headers, timeout=timeout,
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"{path} HTTP {resp.status}")
            return await resp.read()

    def _save(self, wav: bytes) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_bytes(wav)
        except OSError as err:
            _LOGGER.debug("Phrase not cached (%s): kept in memory only", err)


def remove_stale_phrases(phrases: list[TtsPhrase]) -> None:
    """Delete cached phrases no longer configured (text/engine/voice changed)."""
    keep = {phrase.cache_path for phrase in phrases}
    for cache_dir in {phrase.cache_path.parent for phrase in phrases}:
        try:
            for path in cache_dir.glob(f"{TtsPhrase.CACHE_PREFIX}*.wav"):
                if path not in keep:
                    path.unlink(missing_ok=True)
                    _LOGGER.debug("Removed stale phrase cache %s", path)
        except OSError:
            pass


def remove_legacy_cache(data_dir: str = "/data") -> None:
    """Remove phrases cached directly in /data before 3.2 (now /data/annonces)."""
    try:
        for path in Path(data_dir).glob(f"{TtsPhrase.CACHE_PREFIX}*.wav"):
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _cue_from_wav_bytes(wav: bytes) -> Cue:
    with wave.open(io.BytesIO(wav), "rb") as wav_file:
        return Cue(
            wav_file.readframes(wav_file.getnframes()),
            wav_file.getframerate(),
            wav_file.getsampwidth(),
            wav_file.getnchannels(),
        )
