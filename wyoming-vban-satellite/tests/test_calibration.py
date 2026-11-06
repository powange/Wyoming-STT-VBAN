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
from typing import Optional

import aiohttp

from wyoming.event import Event
from wyoming.info import Attribution, Info, WakeModel, WakeProgram
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.wake import Detect, Detection, NotDetected

from test_webui import FakeSupervisor, _free_port
from wyoming_vban import wake_test
from wyoming_vban.calibration import (
    LevelMeter,
    analyze_noise,
    analyze_voice,
    autogain_decision,
    choose_noise_level,
    compare_noise_levels,
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


class NoiseSuppressionChoiceTest(unittest.TestCase):
    def choose(self, background: float, voice: float) -> str:
        noise_pcm = noise(5, background, seed=7)
        voice_pcm = speech(8, voice, background) if voice else noise(8, background, seed=9)
        rows, previews = compare_noise_levels(
            noise_pcm, voice_pcm, analyze_noise(noise_pcm)["rms_db"], 1.0)
        self.assertEqual([r["level"] for r in rows], ["off", "light", "medium", "strong"])
        self.assertEqual(set(previews), {"off", "light", "medium", "strong"})
        return choose_noise_level(rows)

    def test_quiet_room_needs_none(self) -> None:
        self.assertEqual(self.choose(20, 3000), "off")  # background about -65 dBFS

    def test_noise_only_needs_none(self) -> None:
        self.assertEqual(self.choose(300, 0), "off")  # nothing to gain on a voice

    def test_steady_noise_and_voice(self) -> None:
        # Fan about -40 dBFS: the weakest level gaining 6 dB on voice/noise
        self.assertEqual(self.choose(300, 3000), "medium")  # voice/noise about 17 dB
        self.assertEqual(self.choose(300, 600), "medium")  # about 5 dB

    def row(self, level, noise_db, voice_db, eats=False):
        return {"level": level, "noise_db": noise_db, "voice_db": voice_db,
                "snr_db": voice_db - noise_db, "eats_voice": eats}

    def test_rules(self) -> None:
        off = self.row("off", -45, -20)
        # Under -55 dBFS: the weakest level doing it
        self.assertEqual(choose_noise_level([
            off, self.row("light", -50, -20), self.row("medium", -56, -20),
            self.row("strong", -60, -20)]), "medium")
        # +4 dB at best: that level; +2 dB at best: off
        self.assertEqual(choose_noise_level([
            off, self.row("light", -47, -20), self.row("medium", -49, -20)]), "medium")
        self.assertEqual(choose_noise_level([off, self.row("light", -47, -20)]), "off")
        # A level eating the voice isn't recommended
        self.assertEqual(choose_noise_level([
            off, self.row("light", -47, -20), self.row("medium", -60, -25, eats=True)]), "off")
        # Already quiet: off
        self.assertEqual(choose_noise_level([self.row("off", -60, -20)]), "off")


class AutoGainDecisionTest(unittest.TestCase):
    def test_small_gap_off(self) -> None:
        decision = autogain_decision(-20.0, -26.0)
        self.assertFalse(decision["auto_gain"])
        self.assertEqual(decision["gap_db"], 6.0)
        self.assertEqual(decision["advice"], [])

    def test_large_gap_on_with_bounded_target(self) -> None:
        decision = autogain_decision(-18.0, -32.0)
        self.assertTrue(decision["auto_gain"])
        self.assertEqual(decision["target_db"], -18)
        self.assertEqual(autogain_decision(-8.0, -22.0)["target_db"], -14)
        self.assertEqual(autogain_decision(-30.0, -44.0)["target_db"], -24)

    def test_far_too_quiet(self) -> None:
        decision = autogain_decision(-50.0, -68.0)
        self.assertTrue(any("montez le gain dans ESPHome" in a for a in decision["advice"]))


class FakeOpenWakeWord:
    """Wyoming wake word server: one detection per audio chunk, up to
    `detections`, then nothing (like a wake word said that many times)."""

    MODELS = ["dis_moi_jarvis-6", "ok_nabu"]

    def __init__(self) -> None:
        self.detections = 0
        self.port = _free_port()
        self.names: list = []
        self.server = AsyncServer.from_uri(f"tcp://127.0.0.1:{self.port}")
        self.task: asyncio.Task

    async def start(self) -> None:
        fake = self

        class Handler(AsyncEventHandler):
            sent = 0

            async def handle_event(self, event: Event) -> bool:
                if event.type == "describe":
                    await self.write_event(Info(wake=[WakeProgram(
                        name="openwakeword", description="", installed=True, version="1",
                        attribution=Attribution(name="", url=""),
                        models=[WakeModel(name=m, description=m, installed=True,
                                          attribution=Attribution(name="", url=""),
                                          languages=[], version="1", phrase=m)
                                for m in fake.MODELS])]).event())
                elif Detect.is_type(event.type):
                    fake.names.append(Detect.from_event(event).names)
                elif event.type == "audio-chunk" and self.sent < fake.detections:
                    self.sent += 1
                    await self.write_event(
                        Detection(name="dis_moi_jarvis-6", timestamp=self.sent).event())
                elif event.type == "audio-stop" and not self.sent:
                    await self.write_event(NotDetected().event())
                return True

        self.task = asyncio.create_task(self.server.run(Handler))
        await asyncio.sleep(0.1)

    async def stop(self) -> None:
        await self.server.stop()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class WakeWordTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.oww = FakeOpenWakeWord()
        await self.oww.start()

    async def asyncTearDown(self) -> None:
        await self.oww.stop()

    async def test_counts_detections(self) -> None:
        pcm = noise(3, 300)
        for count in range(6):
            self.oww.detections = count
            found = await wake_test.count_detections(
                pcm, "127.0.0.1", self.oww.port, "dis_moi_jarvis-6", pace=0)
            self.assertEqual(len(found), count)
        self.assertEqual(self.oww.names[-1], ["dis_moi_jarvis-6"])

    async def test_models(self) -> None:
        self.assertEqual(await wake_test.list_models("127.0.0.1", self.oww.port),
                         ["dis_moi_jarvis-6", "ok_nabu"])
        await wake_test.check_model("127.0.0.1", self.oww.port, "dis_moi_jarvis-6")
        with self.assertRaises(wake_test.WakeTestError):
            await wake_test.check_model("127.0.0.1", self.oww.port, "hey_jarvis")

    async def test_unreachable(self) -> None:
        with self.assertRaises(wake_test.WakeTestError):
            await wake_test.list_models("127.0.0.1", _free_port())
        with self.assertRaises(wake_test.WakeTestError):
            await wake_test.count_detections(b"\0" * 3200, "127.0.0.1", _free_port(),
                                             "dis_moi_jarvis-6", pace=0)


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

    async def record(self, step: str, pcm: bytes, extra: Optional[dict] = None) -> tuple[int, dict]:
        request = asyncio.create_task(self.http.post(
            self.url("/api/calibration/record"),
            json={"satellite": "Bureau", "step": step, **(extra or {})}))
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
        # The preview is deleted once applied; the voice take serves step A
        self.assertEqual(await self.audio_status("after"), 404)
        self.assertEqual(await self.audio_status("before"), 200)

        # Step A: the voice through each noise suppression level
        async with self.http.post(self.url("/api/calibration/denoise"),
                                  json={"satellite": "Bureau"}) as resp:
            self.assertEqual(resp.status, 200)
            denoise = await resp.json()
        self.assertEqual(len(denoise["rows"]), 4)
        self.assertIn(denoise["recommended"], ("off", "light", "medium", "strong"))
        self.assertEqual(await self.audio_status("denoise-strong"), 200)

        # Step B: near and far takes
        status, body = await self.record("near", speech(6.3, 6000))
        self.assertEqual(status, 200, body)
        status, body = await self.record("far", speech(6.3, 800))
        self.assertEqual(status, 200, body)
        async with self.http.post(self.url("/api/calibration/autogain"), json={
            "satellite": "Bureau", "noise_suppression": "off",
        }) as resp:
            self.assertEqual(resp.status, 200)
            autogain = await resp.json()
        self.assertGreater(autogain["gap_db"], 10)
        self.assertTrue(autogain["auto_gain"])
        self.assertTrue(-24 <= autogain["target_db"] <= -14)
        for which in ("near-before", "near-after", "far-before", "far-after"):
            self.assertEqual(await self.audio_status(which), 200)

        # Step C: wake word test, current and proposed settings side by side
        oww = FakeOpenWakeWord()
        await oww.start()
        self.ui.STEP_SECONDS = dict(self.ui.STEP_SECONDS, wake=2.0)
        self.ui.WAKE_PACE = 0
        try:
            oww.detections = 3
            status, wake = await self.record("wake", speech(2.3, 3000), {
                "host": "127.0.0.1", "port": oww.port, "model": "dis_moi_jarvis-6",
                "proposed": {"noise_suppression": "medium"},
            })
            self.assertEqual(status, 200, wake)
            self.assertEqual((wake["current"]["detections"], wake["proposed"]["detections"],
                              wake["expected"]), (3, 3, 5))
            self.assertEqual(wake["proposed"]["noise_suppression"], "medium")
            self.assertTrue(wake["proposed"]["auto_gain"])
            # Unknown model: refused before recording
            status, body = await self.record("wake", b"", {
                "host": "127.0.0.1", "port": oww.port, "model": "hey_jarvis"})
            self.assertEqual(status, 502)
            self.assertIn("hey_jarvis", body["error"])
        finally:
            await oww.stop()

        # Apply all: in the app's options, then everything is deleted
        async with self.http.post(self.url("/api/calibration/apply"), json={
            "satellite": "Bureau", "volume": proposal["volume"], "noise_suppression": "light",
            "auto_gain": True, "auto_gain_target_db": autogain["target_db"], "finish": True,
        }) as resp:
            self.assertEqual(resp.status, 200, await resp.text())
        mic = self.supervisor.options["satellites"][0]["microphone"]
        self.assertEqual((mic["noise_suppression"], mic["auto_gain"], mic["auto_gain_target_db"]),
                         ("light", True, autogain["target_db"]))
        mic_filter = self.runtime.runners["Bureau"].mic_filter
        self.assertEqual((mic_filter.noise_suppression, mic_filter.auto_gain,
                          mic_filter.auto_gain_target_db),
                         ("light", True, float(autogain["target_db"])))
        self.assertEqual(self.ui._sessions, {})

    async def audio_status(self, which: str) -> int:
        async with self.http.get(
            self.url(f"/api/calibration/audio?satellite=Bureau&which={which}")
        ) as resp:
            return resp.status

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
