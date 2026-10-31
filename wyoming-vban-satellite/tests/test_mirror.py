"""Debug copies of a satellite's audio streams (debug_mirror option).

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import asyncio
import json
import os
import socket
import tempfile
import unittest

from wyoming_vban.config import (
    DebugMirror,
    SatelliteConfig,
    load_options,
    validate_mirrors,
)
from wyoming_vban.mirror import SatelliteMirrors
from wyoming_vban.vban import VbanPacket, parse_packet


class DebugMirrorConfigTest(unittest.TestCase):
    def test_default_stream_names(self) -> None:
        names = [
            DebugMirror("VBAN Satellite Bureau", stream, "127.0.0.1").vban_stream_name()
            for stream in ("mic_received", "mic_sent", "tts")
        ]
        self.assertEqual(names, ["Bureau-rx", "Bureau-tx", "Bureau-tts"])
        long = DebugMirror("Satellite Chambreparentale", "tts", "x").vban_stream_name()
        self.assertLessEqual(len(long), 16)
        self.assertTrue(long.endswith("-tts"))
        custom = DebugMirror("Bureau", "tts", "x", stream_name="MonFlux")
        self.assertEqual(custom.vban_stream_name(), "MonFlux")

    def test_load_and_validate(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({
                "satellites": [{"name": "Bureau", "wyoming_port": 10700}],
                "debug_mirror": [
                    {"satellite": "Bureau", "stream": "mic_sent",
                     "address": "192.168.0.42", "port": 6990},
                    {"satellite": "Cuisine", "stream": "tts", "address": "x"},
                    {"satellite": "Bureau", "stream": "nope", "address": "x"},
                ],
            }, f)
        try:
            options = load_options(f.name)
        finally:
            os.unlink(f.name)
        self.assertEqual(len(options.debug_mirrors), 3)
        with self.assertLogs("wyoming_vban.config", "WARNING") as logs:
            valid = validate_mirrors(options.debug_mirrors, options.satellites)
        self.assertEqual(len(logs.output), 2)  # unknown satellite, unknown stream
        self.assertEqual(
            [(m.satellite, m.stream, m.port) for m in valid], [("Bureau", "mic_sent", 6990)],
        )

    def test_no_debug_mirror_option(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"satellites": [{"name": "Bureau"}]}, f)
        try:
            self.assertEqual(load_options(f.name).debug_mirrors, [])
        finally:
            os.unlink(f.name)
        self.assertEqual(validate_mirrors([], [SatelliteConfig(name="Bureau")]), [])


class SatelliteMirrorsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.setblocking(False)
        port = self.listener.getsockname()[1]
        self.mirrors = SatelliteMirrors([
            DebugMirror("VBAN Satellite Bureau", stream, "127.0.0.1", port)
            for stream in ("mic_received", "mic_sent", "tts")
        ])
        self.mirrors.open()

    async def asyncTearDown(self) -> None:
        await self.mirrors.close()
        self.listener.close()

    async def received(self, wait: float = 0.5) -> list[VbanPacket]:
        await asyncio.sleep(wait)
        packets = []
        while True:
            try:
                packets.append(parse_packet(self.listener.recv(2048)))
            except BlockingIOError:
                return packets

    async def test_mic_received_is_resent_under_its_own_name(self) -> None:
        packet = VbanPacket(
            sample_rate=48000, samples_per_frame=256, channels=1, data_format=1,
            codec=0, stream_name="AtomBureau", frame_counter=7,
            payload=bytes(range(256)) * 2,
        )
        self.mirrors.mic_received[0].on_packet(packet)
        (copy,) = await self.received(0.1)
        self.assertEqual(copy.stream_name, "Bureau-rx")
        self.assertEqual(copy.sample_rate, 48000)
        self.assertEqual(copy.payload, packet.payload)

    async def test_mic_sent_and_tts(self) -> None:
        self.mirrors.mic_sent(b"\x01\x00" * 1024)  # 64 ms at 16 kHz
        self.mirrors.tts_start()
        self.mirrors.tts(b"\x02\x00" * 2205, rate=22050, width=2, channels=1)  # 100 ms
        packets = await self.received(1.0)
        by_name: dict[str, list[VbanPacket]] = {}
        for packet in packets:
            by_name.setdefault(packet.stream_name, []).append(packet)
        self.assertEqual(sorted(by_name), ["Bureau-tts", "Bureau-tx"])
        self.assertEqual({p.sample_rate for p in by_name["Bureau-tx"]}, {16000})
        self.assertEqual(sum(len(p.payload) for p in by_name["Bureau-tx"]), 2048)
        self.assertEqual({p.sample_rate for p in by_name["Bureau-tts"]}, {22050})


if __name__ == "__main__":
    unittest.main()
