"""TTS output to a Home Assistant media_player (e.g. Google Cast).

The TTS response is buffered, wrapped as a WAV file, served by a small
HTTP server on the host's LAN IP, then played with the media_player.play_media
service through the Supervisor API.
"""

import asyncio
import io
import logging
import os
import socket
import uuid
import wave
from collections import OrderedDict
from typing import Optional

import aiohttp
from aiohttp import web

_LOGGER = logging.getLogger(__name__)

SUPERVISOR_API = "http://supervisor/core/api"


def detect_local_ip() -> str:
    """Return the host's LAN IP (the source address used to reach the internet).

    A UDP "connect" sends no packet; it only selects the outgoing interface.
    The app runs with host_network, so this is the host's IP.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError as err:
        _LOGGER.warning("Could not detect LAN IP (%s), using 127.0.0.1", err)
        return "127.0.0.1"
    finally:
        sock.close()


def build_wav(pcm: bytes, rate: int, width: int, channels: int) -> bytes:
    """Wrap raw PCM in a WAV container (in memory)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav_file:
        wav_file.setframerate(rate)
        wav_file.setsampwidth(width)
        wav_file.setnchannels(channels)
        wav_file.writeframes(pcm)
    return buf.getvalue()


class MediaPlayerOutput:
    """Serves TTS WAV files over HTTP and plays them on a HA media_player."""

    KEEP_FILES = 5  # most recent WAVs kept available (older ones are purged)
    PLAY_MEDIA_TIMEOUT_S = 5.0

    def __init__(self, entity_id: str, http_host: str, http_port: int):
        self.entity_id = entity_id
        self.http_host = http_host
        self.http_port = http_port
        self._files: "OrderedDict[str, bytes]" = OrderedDict()
        self._runner: Optional[web.AppRunner] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._token = os.environ.get("SUPERVISOR_TOKEN", "")

    async def start(self) -> None:
        """Start the HTTP server serving /tts/<name>.wav."""
        if not self._token:
            _LOGGER.error(
                "SUPERVISOR_TOKEN not set — play_media calls will fail "
                "(is homeassistant_api enabled?)"
            )

        app = web.Application()
        app.router.add_get("/tts/{name}", self._handle_get)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, host="0.0.0.0", port=self.http_port)
        await site.start()
        self._session = aiohttp.ClientSession()
        _LOGGER.info(
            "TTS HTTP server listening on http://%s:%d/tts/",
            self.http_host, self.http_port,
        )

    async def stop(self) -> None:
        """Stop the HTTP server and the API client session."""
        if self._session is not None:
            await self._session.close()
            self._session = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _handle_get(self, request: web.Request) -> web.Response:
        data = self._files.get(request.match_info["name"])
        if data is None:
            raise web.HTTPNotFound()
        _LOGGER.debug("Serving %s to %s", request.path, request.remote)
        return web.Response(body=data, content_type="audio/wav")

    def publish(self, wav: bytes) -> str:
        """Make a WAV available under a unique name and return its URL."""
        name = f"{uuid.uuid4().hex}.wav"
        self._files[name] = wav
        while len(self._files) > self.KEEP_FILES:
            self._files.popitem(last=False)
        return f"http://{self.http_host}:{self.http_port}/tts/{name}"

    async def play(self, url: str) -> bool:
        """Call media_player.play_media for url. Returns True on HTTP 200."""
        if self._session is None:
            return False

        try:
            async with self._session.post(
                f"{SUPERVISOR_API}/services/media_player/play_media",
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "entity_id": self.entity_id,
                    "media_content_id": url,
                    "media_content_type": "music",
                },
                timeout=aiohttp.ClientTimeout(total=self.PLAY_MEDIA_TIMEOUT_S),
            ) as resp:
                _LOGGER.info(
                    "play_media on %s returned HTTP %d", self.entity_id, resp.status,
                )
                if resp.status != 200:
                    _LOGGER.error(
                        "play_media failed: %s", (await resp.text())[:200],
                    )
                    return False
                return True
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error(
                "play_media on %s failed: %s", self.entity_id, err or type(err).__name__,
            )
            return False
