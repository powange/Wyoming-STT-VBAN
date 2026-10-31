"""Microphone from a camera stream (RTSP, read by ffmpeg) and output: none.

Uses a fake ffmpeg (a Python script) instead of the real one.
Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import asyncio
import copy
import json
import os
import socket
import stat
import sys
import tempfile
import time
import unittest

from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.asr import Transcript
from wyoming.client import AsyncTcpClient
from wyoming.info import Describe
from wyoming.satellite import RunSatellite
from wyoming.server import AsyncServer
from wyoming.tts import Synthesize

from wyoming_vban.config import ConfigError, load_options, parse_options, validate
from wyoming_vban.rtsp_source import RtspSource, mask_url
from wyoming_vban.runtime import AppRuntime
from wyoming_vban.satellite import (
    FollowupSettings,
    VbanSatelliteHandler,
    make_satellite_info,
)

URL = "rtsp://admin:s3cr3t@192.168.0.17:554/cam/realmonitor?channel=1"

# Fake ffmpeg: logs each start, writes PCM, then behaves as FAKE_MODE says
FAKE_FFMPEG = f"""#!{sys.executable}
import os, sys, time
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(f"{{time.monotonic()}}\\n")
mode = os.environ.get("FAKE_MODE", "exit")
if mode == "error":
    sys.stderr.write("method DESCRIBE failed: 401 Unauthorized ({URL})\\n")
    sys.exit(1)
sys.stdout.buffer.write(b"\\x00\\x10" * 1600)  # 0.1 s of 16 kHz audio
sys.stdout.buffer.flush()
if mode == "stall":
    time.sleep(30)  # keeps running without sending audio
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeFfmpegTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.ffmpeg = os.path.join(self.dir.name, "ffmpeg")
        with open(self.ffmpeg, "w") as f:
            f.write(FAKE_FFMPEG)
        os.chmod(self.ffmpeg, os.stat(self.ffmpeg).st_mode | stat.S_IEXEC)
        self.log = os.path.join(self.dir.name, "starts.log")
        os.environ["FAKE_LOG"] = self.log
        os.environ["FAKE_MODE"] = "exit"

    async def asyncTearDown(self) -> None:
        self.dir.cleanup()

    def starts(self) -> list[float]:
        try:
            with open(self.log) as f:
                return [float(line) for line in f]
        except FileNotFoundError:
            return []

    def source(self, **kwargs) -> RtspSource:
        source = RtspSource(URL, ffmpeg=self.ffmpeg, **kwargs)
        source.BACKOFF_S = (0.3, 0.6, 1.0)
        source.STALL_S = 0.5
        return source


class RtspSourceTest(FakeFfmpegTest):
    async def test_packets_then_restart_after_backoff(self) -> None:
        source = self.source()
        packets = []
        source.subscribe(packets.append)
        with self.assertLogs("wyoming_vban.rtsp_source", "INFO") as logs:
            task = asyncio.create_task(source.run())
            await asyncio.sleep(1.2)
            source.stop()
            await asyncio.wait_for(task, 5)

        # 0.1 s per ffmpeg run, cut into 256-sample 16 kHz mono packets
        self.assertGreaterEqual(len(packets), 6)
        self.assertEqual(
            {(p.sample_rate, p.channels, p.samples_per_frame, len(p.payload))
             for p in packets},
            {(16000, 1, 256, 512)},
        )
        starts = self.starts()
        self.assertGreaterEqual(len(starts), 2)
        self.assertGreaterEqual(starts[1] - starts[0], 0.3)  # waited before restarting
        text = "\n".join(logs.output)
        self.assertIn("First RTSP audio from rtsp://***@192.168.0.17", text)
        self.assertIn("16000 Hz", text)
        self.assertIn("No RTSP audio received", text)
        self.assertIn("RTSP audio resumed", text)
        self.assertNotIn("s3cr3t", text)

    async def test_stalled_stream_is_restarted(self) -> None:
        os.environ["FAKE_MODE"] = "stall"
        source = self.source()
        task = asyncio.create_task(source.run())
        await asyncio.sleep(1.4)  # 0.5 s stall + 0.3 s wait + 0.5 s stall
        process = source._process
        source.stop()
        await asyncio.wait_for(task, 5)
        self.assertGreaterEqual(len(self.starts()), 2)
        self.assertIsNone(source._process)
        if process is not None:  # reaped: no zombie
            self.assertIsNotNone(process.returncode)

    async def test_ffmpeg_errors_logged_without_password(self) -> None:
        os.environ["FAKE_MODE"] = "error"
        source = self.source()
        with self.assertLogs("wyoming_vban.rtsp_source", "WARNING") as logs:
            task = asyncio.create_task(source.run())
            await asyncio.sleep(0.5)
            source.stop()
            await asyncio.wait_for(task, 5)
        text = "\n".join(logs.output)
        self.assertIn("401 Unauthorized", text)
        self.assertIn("rtsp://***@", text)
        self.assertNotIn("s3cr3t", text)

    def test_mask_url(self) -> None:
        self.assertEqual(mask_url(URL), "rtsp://***@192.168.0.17:554/cam/realmonitor?channel=1")
        self.assertEqual(mask_url("rtsp://192.168.0.201:8554/chambre"),
                         "rtsp://192.168.0.201:8554/chambre")


class RtspSatelliteTest(FakeFfmpegTest):
    """A camera satellite in the runtime: its audio reaches HA."""

    async def test_camera_audio_reaches_wyoming(self) -> None:
        # 0.1 s of audio per ffmpeg run, run again every 0.2 s
        RtspSource.FFMPEG = self.ffmpeg
        RtspSource.BACKOFF_S = (0.2,)
        port = _free_port()
        options = parse_options({"satellites": [{
            "name": "Chambre", "wyoming_port": port, "output": "none",
            "microphone": {"source": "rtsp", "rtsp_url": URL},
            "vban_output": {}, "media_player": {}, "followup": {"enabled": False},
            "waiting_sounds": {"thinking_mode": "none", "search_mode": "none"},
            "camera_phrase": {"mode": "none"},
        }], "publish_exchanges": False})
        validate(options.satellites)
        runtime = AppRuntime(self.dir.name)
        try:
            await runtime.start(options)
            self.assertEqual(runtime._receivers, {})  # no VBAN socket
            client = AsyncTcpClient("127.0.0.1", port)
            await asyncio.sleep(0.3)
            await client.connect()
            for event in (Describe(), RunSatellite(), Describe()):
                await client.write_event(event.event())
            chunks = 0
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not chunks:
                try:
                    event = await asyncio.wait_for(client.read_event(), 1)
                except asyncio.TimeoutError:
                    continue
                chunks += event.type == "audio-chunk"
            await client.disconnect()
            self.assertGreater(chunks, 0)
            status = runtime.status()["satellites"][0]
            self.assertIsNotNone(status["last_packet_s"])
        finally:
            await runtime.stop()
            RtspSource.FFMPEG = "ffmpeg"
            RtspSource.BACKOFF_S = (1.0, 2.0, 5.0, 10.0, 30.0)


class ConfigTest(unittest.TestCase):
    def load(self, satellites: list) -> list:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"satellites": satellites}, f)
        try:
            return load_options(f.name).satellites
        finally:
            os.unlink(f.name)

    def test_vban_satellite_without_source(self) -> None:
        sats = self.load([{"name": "Bureau", "wyoming_port": 10700,
                           "microphone": {"vban_port": 6981}}])
        validate(sats)
        self.assertEqual((sats[0].mic_source, sats[0].vban_receive_port), ("vban", 6981))

    def test_rtsp_satellite_without_vban_port(self) -> None:
        sats = self.load([
            {"name": "Bureau", "wyoming_port": 10700,
             "microphone": {"vban_port": 6980, "vban_mode": "unicast"}},
            {"name": "Chambre", "wyoming_port": 10702, "output": "none",
             "microphone": {"source": "rtsp", "rtsp_url": "rtsp://192.168.0.201:8554/chambre"}},
        ])
        validate(sats)  # no VBAN port conflict with Bureau (default 6980 ignored)
        self.assertEqual(sats[1].mic_source, "rtsp")
        self.assertEqual(sats[1].rtsp_transport, "tcp")

    def test_missing_rtsp_url_skips_only_that_satellite(self) -> None:
        sats = self.load([
            {"name": "Bureau", "wyoming_port": 10700, "microphone": {"vban_port": 6980}},
            {"name": "Chambre", "wyoming_port": 10702, "microphone": {"source": "rtsp"}},
        ])
        with self.assertLogs("wyoming_vban.config", "ERROR") as logs:
            validate(sats)
        self.assertEqual([s.name for s in sats], ["Bureau"])
        self.assertIn("rtsp_url is empty", logs.output[0])

        strict = self.load([
            {"name": "Chambre", "wyoming_port": 10702, "microphone": {"source": "rtsp"}},
        ])
        with self.assertRaisesRegex(ConfigError, "rtsp_url"):
            validate(strict, strict=True)


class OutputNoneTest(unittest.IsolatedAsyncioTestCase):
    """output: none — the run ends normally, the response isn't played."""

    async def test_response_not_played_and_back_to_wake_word(self) -> None:
        port = _free_port()
        server = AsyncServer.from_uri(f"tcp://127.0.0.1:{port}")

        class Receiver:
            def subscribe(self, callback, stream_name=""):
                pass

            def unsubscribe(self, callback):
                pass

        await server.start(lambda r, w: VbanSatelliteHandler(
            r, w, make_satellite_info("Chambre"), Receiver(), None,
            followup=FollowupSettings(enabled=True, beep=False),
        ))
        ha = AsyncTcpClient("127.0.0.1", port)
        await ha.connect()
        try:
            for event in (Describe(), RunSatellite(), Describe()):
                await ha.write_event(event.event())

            async def next_run():
                while True:
                    event = await asyncio.wait_for(ha.read_event(), 3)
                    if event.type == "run-pipeline":
                        return event.data["start_stage"]

            self.assertEqual(await next_run(), "wake")
            with self.assertLogs("wyoming_vban.satellite", "INFO") as logs:
                await ha.write_event(Transcript(text="allume la lumière").event())
                await ha.write_event(Synthesize(text="Allumé.").event())
                await ha.write_event(AudioStart(rate=22050, width=2, channels=1).event())
                await ha.write_event(
                    AudioChunk(rate=22050, width=2, channels=1, audio=bytes(4410)).event()
                )
                await ha.write_event(AudioStop().event())
                self.assertEqual(await next_run(), "wake")
            self.assertIn("Response not played (output: none)", "\n".join(logs.output))
        finally:
            await ha.disconnect()
            await server.stop()


if __name__ == "__main__":
    unittest.main()
