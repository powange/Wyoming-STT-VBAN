"""Microphone calibration: analysis, proposal, live meter and the API.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp, pyspeex-noise)
"""

import asyncio
import copy
import io
import json
import math
import os
import random
import socket
import struct
import tempfile
import unittest
import wave

import aiohttp

from test_webui import FakeSupervisor, _free_port
from wyoming_vban.calibration import (
    LevelMeter,
    analyze_noise,
    analyze_voice,
    propose,
)
from wyoming_vban.config import parse_options, validate
from wyoming_vban.mic_filter import MicFilter
from wyoming_vban.runtime import AppRuntime
from wyoming_vban.vban import build_packet
from wyoming_vban.webui import SupervisorOptions, WebUI

RATE = 16000


def noise(seconds: float, amplitude: float, seed: int = 1) -> bytes:
    rng = random.Random(seed)
    n = int(RATE * seconds)
    return struct.pack(f"<{n}h", *(int(rng.gauss(0, amplitude)) for _ in range(n)))


def speech(seconds: float, amplitude: float, background: float = 100) -> bytes:
    """Alternating 0.25 s of "voice" (a tone) and 0.25 s of background."""
    out = bytearray()
    for i in range(int(seconds * 4)):
        part = list(struct.unpack("<4000h", noise(0.25, background, seed=i)))
        if i % 2 == 0:
            part = [max(-32768, min(32767, s + int(amplitude * math.sin(i * 0.1 + j / 4))))
                    for j, s in enumerate(part)]
        out.extend(struct.pack("<4000h", *part))
    return bytes(out)


class AnalysisTest(unittest.TestCase):
    def test_noise_and_voice(self) -> None:
        noise_stats = analyze_noise(noise(5, 100))
        self.assertAlmostEqual(noise_stats["rms_db"], -50.3, delta=1)
        voice = analyze_voice(speech(8, 3000), noise_stats["rms_db"])
        self.assertAlmostEqual(voice["rms_db"], -23.8, delta=1.5)  # spoken parts only
        self.assertAlmostEqual(voice["speech_s"], 4.0, delta=0.5)
        self.assertLess(voice["peak_db"], -18)
        self.assertEqual(voice["clip_pct"], 0)

    def test_normal_voice_gets_target_without_advice(self) -> None:
        proposal = propose({"rms_db": -60.0},
                           {"rms_db": -26.0, "peak_db": -12.0, "clip_pct": 0.0})
        self.assertAlmostEqual(proposal["gain_db"], 6.0, delta=0.1)
        self.assertAlmostEqual(proposal["volume"], 2.0, delta=0.01)
        self.assertAlmostEqual(proposal["voice_after_db"], -20.0, delta=0.1)
        self.assertEqual(proposal["snr_db"], 34.0)
        self.assertEqual(proposal["advice"], [])

    def test_peak_limits_the_gain(self) -> None:
        proposal = propose({"rms_db": -60.0},
                           {"rms_db": -30.0, "peak_db": -6.0, "clip_pct": 0.0})
        self.assertAlmostEqual(proposal["gain_db"], 3.0, delta=0.1)  # peak to -3 dBFS
        self.assertLessEqual(proposal["peak_after_db"], -3.0)

    def test_quiet_voice_advises_device_gain(self) -> None:
        proposal = propose({"rms_db": -70.0},
                           {"rms_db": -45.0, "peak_db": -35.0, "clip_pct": 0.0})
        self.assertTrue(any("montez le gain dans ESPHome" in a for a in proposal["advice"]))

    def test_over_max_volume(self) -> None:
        proposal = propose({"rms_db": -80.0},
                           {"rms_db": -48.0, "peak_db": -40.0, "clip_pct": 0.0})
        self.assertTrue(proposal["over_max"])
        self.assertEqual(proposal["volume"], 10.0)

    def test_clipping_advises_lower_device_gain(self) -> None:
        proposal = propose({"rms_db": -50.0},
                           {"rms_db": -8.0, "peak_db": 0.0, "clip_pct": 2.0})
        self.assertTrue(any("baissez le gain dans ESPHome" in a for a in proposal["advice"]))
        self.assertLess(proposal["gain_db"], 0)

    def test_noisy_after_gain_and_low_snr(self) -> None:
        proposal = propose({"rms_db": -40.0},
                           {"rms_db": -30.0, "peak_db": -15.0, "clip_pct": 0.0})
        advice = " ".join(proposal["advice"])
        self.assertIn("Écart voix/bruit faible", advice)
        self.assertIn("faux réveils", advice)

    def test_level_meter(self) -> None:
        meter = LevelMeter()
        self.assertIsNone(meter.stats())
        meter.add(struct.pack("<4h", 32767, -32768, 0, 0))
        stats = meter.stats()
        self.assertEqual(stats["clip_pct"], 50.0)
        self.assertAlmostEqual(stats["peak_db"], 0.0, delta=0.1)

    def test_mic_volume_in_filter(self) -> None:
        pcm = noise(1, 1000)
        out = MicFilter(volume=2.0).process(pcm)
        gain = 20 * math.log10(
            (sum(s * s for s in struct.unpack(f"<{len(out) // 2}h", out)) /
             sum(s * s for s in struct.unpack(f"<{len(pcm) // 2}h", pcm))) ** 0.5)
        self.assertAlmostEqual(gain, 6.0, delta=0.2)


class CalibrationApiTest(unittest.IsolatedAsyncioTestCase):
    """The whole flow through the UI API, with a fake VBAN microphone."""

    async def asyncSetUp(self) -> None:
        os.environ["SUPERVISOR_TOKEN"] = "token"
        self.mic_port = _free_port(socket.SOCK_DGRAM)
        self.options = {
            "satellites": [{
                "name": "Bureau", "wyoming_port": _free_port(), "output": "none",
                "microphone": {"vban_port": self.mic_port, "stream_name": "AtomBureau"},
                "vban_output": {}, "media_player": {}, "followup": {"enabled": False},
                "waiting_sounds": {"thinking_mode": "none", "search_mode": "none"},
                "camera_phrase": {"mode": "none"},
            }],
            "publish_exchanges": False, "debug_mirror": [],
        }
        self.supervisor = FakeSupervisor(self.options)
        await self.supervisor.start()
        self.cache = tempfile.TemporaryDirectory()
        self.runtime = AppRuntime(self.cache.name)
        parsed = parse_options(copy.deepcopy(self.options))
        validate(parsed.satellites)
        await self.runtime.start(parsed)
        self.ui_port = _free_port()
        self.ui = WebUI(self.runtime, self.ui_port,
                        SupervisorOptions(f"http://127.0.0.1:{self.supervisor.port}"),
                        allowed_ips=frozenset({"127.0.0.1"}))
        await self.ui.start()
        self.http = aiohttp.ClientSession()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.counter = 0
        await asyncio.sleep(0.3)

    async def asyncTearDown(self) -> None:
        self.sock.close()
        await self.http.close()
        await self.ui.stop()
        await self.runtime.stop()
        await self.supervisor.stop()
        self.cache.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.ui_port}{path}"

    async def mic(self, pcm: bytes) -> None:
        """Send audio as VBAN packets, in real time."""
        for i in range(0, len(pcm) - 511, 512):
            self.sock.sendto(build_packet(pcm[i:i + 512], "AtomBureau", 16000, 1, 256,
                                          self.counter), ("127.0.0.1", self.mic_port))
            self.counter += 1
            await asyncio.sleep(0.016)

    async def record(self, step: str, pcm: bytes) -> tuple[int, dict]:
        request = asyncio.create_task(self.http.post(
            self.url("/api/calibration/record"), json={"satellite": "Bureau", "step": step}))
        await self.mic(pcm)
        resp = await request
        return resp.status, await resp.json()

    async def test_full_calibration(self) -> None:
        # Live meter while audio comes in
        feeding = asyncio.create_task(self.mic(noise(0.5, 100)))
        await asyncio.sleep(0.3)
        async with self.http.get(self.url("/api/meter/once?satellite=Bureau")) as resp:
            meter = await resp.json()
        await feeding
        self.assertAlmostEqual(meter["before"]["rms_db"], -50, delta=3)
        self.assertEqual(meter["volume"], 1.0)

        status, body = await self.record("noise", noise(5.3, 100))
        self.assertEqual(status, 200)
        self.assertAlmostEqual(body["noise"]["rms_db"], -50, delta=2)

        status, body = await self.record("voice", speech(8.3, 1500))
        self.assertEqual(status, 200)
        proposal = body["proposal"]
        self.assertAlmostEqual(proposal["voice_after_db"], -20, delta=1.5)
        self.assertGreater(proposal["volume"], 1.5)

        for which in ("before", "after"):
            async with self.http.get(
                self.url(f"/api/calibration/audio?satellite=Bureau&which={which}")
            ) as resp:
                self.assertEqual(resp.status, 200)
                with wave.open(io.BytesIO(await resp.read())) as wav:
                    self.assertEqual((wav.getframerate(), wav.getnchannels()), (16000, 1))
                    self.assertGreater(wav.getnframes(), 16000 * 7)

        async with self.http.post(self.url("/api/calibration/apply"), json={
            "satellite": "Bureau", "volume": proposal["volume"],
        }) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(self.supervisor.options["satellites"][0]["microphone"]["volume"],
                         proposal["volume"])
        self.assertEqual(self.runtime.runners["Bureau"].mic_filter.volume, proposal["volume"])
        # Recordings deleted once applied
        async with self.http.get(
            self.url("/api/calibration/audio?satellite=Bureau&which=before")
        ) as resp:
            self.assertEqual(resp.status, 404)

    async def test_voice_needs_noise_first_and_clear(self) -> None:
        status, body = await self.record("voice", speech(1, 1500))
        self.assertEqual(status, 400)
        status, _ = await self.record("noise", noise(5.3, 100))
        self.assertEqual(status, 200)
        async with self.http.delete(self.url("/api/calibration?satellite=Bureau")) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(self.ui._sessions, {})

    async def test_no_audio(self) -> None:
        async with self.http.post(self.url("/api/calibration/record"),
                                  json={"satellite": "Bureau", "step": "noise"}) as resp:
            self.assertEqual(resp.status, 409)

    async def test_meter_stream(self) -> None:
        feeding = asyncio.create_task(self.mic(noise(1, 100)))
        async with self.http.get(self.url("/api/meter?satellite=Bureau")) as resp:
            self.assertEqual(resp.headers["Content-Type"], "text/event-stream")
            events = []
            async for line in resp.content:
                if line.startswith(b"data: "):
                    events.append(json.loads(line[6:]))
                if len(events) == 3:
                    break
        feeding.cancel()
        self.assertEqual(len(events), 3)
        self.assertIn("before", events[0])


if __name__ == "__main__":
    unittest.main()
