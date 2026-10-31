"""Phrase synthesized by HA's TTS API and cached on disk.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import asyncio
import io
import os
import socket
import tempfile
import unittest
import wave
from pathlib import Path

from aiohttp import web

from wyoming_vban.tts_phrase import TtsPhrase, remove_stale_phrases


def _free_tcp_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wav(seconds: float, rate: int = 22050) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setframerate(rate)
        wav_file.setsampwidth(2)
        wav_file.setnchannels(1)
        wav_file.writeframes(b"\x01\x00" * int(rate * seconds))
    return buf.getvalue()


class FakeTtsApi:
    """POST /api/tts_get_url + GET /api/tts_proxy/<file>."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.auth: list[str] = []
        self.fail = 0  # number of requests to fail first
        self.port = _free_tcp_port()

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/api/tts_get_url", self._get_url)
        app.router.add_get("/api/tts_proxy/{name}", self._proxy)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", self.port).start()

    async def stop(self) -> None:
        await self._runner.cleanup()

    async def _get_url(self, request: web.Request) -> web.Response:
        self.requests.append(await request.json())
        self.auth.append(request.headers.get("Authorization", ""))
        if self.fail:
            self.fail -= 1
            return web.Response(status=500, text="engine not ready")
        return web.json_response({"url": "x", "path": "/api/tts_proxy/abc.wav"})

    async def _proxy(self, request: web.Request) -> web.Response:
        return web.Response(body=_wav(1.2), content_type="audio/wav")


class TtsPhraseTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        os.environ["SUPERVISOR_TOKEN"] = "token"
        self.api = FakeTtsApi()
        await self.api.start()
        self.dir = tempfile.TemporaryDirectory()

    async def asyncTearDown(self) -> None:
        await self.api.stop()
        self.dir.cleanup()

    def phrase(self, text: str = "Attends, je regarde.") -> TtsPhrase:
        return TtsPhrase(
            text, "tts.piper", "fr_FR-siwis-medium", cache_dir=self.dir.name,
            base_url=f"http://127.0.0.1:{self.api.port}",
        )

    async def test_synthesized_once_then_cached(self) -> None:
        phrase = self.phrase()
        await asyncio.wait_for(phrase.run(), 5)
        self.assertAlmostEqual(phrase.cue.duration, 1.2, places=2)
        self.assertEqual(self.api.auth, ["Bearer token"])
        self.assertEqual(self.api.requests[0]["engine_id"], "tts.piper")
        self.assertEqual(self.api.requests[0]["message"], "Attends, je regarde.")
        self.assertEqual(self.api.requests[0]["language"], "fr_FR")
        self.assertEqual(self.api.requests[0]["options"]["voice"], "fr_FR-siwis-medium")
        self.assertEqual(self.api.requests[0]["options"]["preferred_format"], "wav")
        self.assertTrue(phrase.cache_path.exists())

        # Next start: from the cache, no request
        cached = self.phrase()
        await asyncio.wait_for(cached.run(), 5)
        self.assertAlmostEqual(cached.cue.duration, 1.2, places=2)
        self.assertEqual(len(self.api.requests), 1)

    async def test_retries_until_ha_answers(self) -> None:
        self.api.fail = 2
        phrase = self.phrase()
        phrase.RETRY_S = 0.05
        await asyncio.wait_for(phrase.run(), 5)
        self.assertIsNotNone(phrase.cue)
        self.assertEqual(len(self.api.requests), 3)

    async def test_changed_phrase_removes_old_cache(self) -> None:
        old = self.phrase("Je regarde.")
        await asyncio.wait_for(old.run(), 5)
        new = self.phrase()
        remove_stale_phrases([new])
        self.assertFalse(old.cache_path.exists())
        self.assertEqual(old.cache_path.parent, Path(self.dir.name))


if __name__ == "__main__":
    unittest.main()
