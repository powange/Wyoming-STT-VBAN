"""Two VBAN streams on one UDP port are routed to the right satellite.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
"""

import asyncio
import json
import os
import socket
import tempfile
import unittest

from wyoming_vban.config import ConfigError, load_options, validate
from wyoming_vban.vban import VbanReceiver, build_packet


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _packet(stream_name: str, counter: int) -> bytes:
    return build_packet(
        payload=b"\x00\x00" * 256,
        stream_name=stream_name,
        sample_rate=16000,
        channels=1,
        samples_per_frame=256,
        frame_counter=counter,
    )


class SharedPortRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.port = _free_udp_port()
        self.receiver = VbanReceiver(port=self.port)
        self.task = asyncio.create_task(self.receiver.run())
        self.sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        await asyncio.sleep(0.05)  # let the receiver bind

    async def asyncTearDown(self) -> None:
        self.sender.close()
        self.receiver.stop()
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)

    async def _send(self, *streams: str, count: int = 20) -> None:
        for i in range(count):
            for stream in streams:
                self.sender.sendto(_packet(stream, i), ("127.0.0.1", self.port))
        await asyncio.sleep(0.2)

    async def test_two_streams_one_port(self) -> None:
        bureau, salon = [], []
        self.receiver.subscribe(lambda p: bureau.append(p.stream_name), "AtomBureau")
        self.receiver.subscribe(lambda p: salon.append(p.stream_name), "AtomSalon")

        await self._send("AtomBureau", "AtomSalon")

        # Every packet reaches its satellite, none reaches the other one
        self.assertEqual(bureau, ["AtomBureau"] * 20)
        self.assertEqual(salon, ["AtomSalon"] * 20)

    async def test_catch_all_gets_only_unclaimed_streams(self) -> None:
        named, catch_all = [], []
        self.receiver.subscribe(lambda p: named.append(p.stream_name), "AtomBureau")
        self.receiver.subscribe(lambda p: catch_all.append(p.stream_name), "")

        await self._send("AtomBureau", "Other")

        self.assertEqual(named, ["AtomBureau"] * 20)
        self.assertEqual(catch_all, ["Other"] * 20)

    async def test_unsubscribe(self) -> None:
        bureau = []

        def callback(packet) -> None:
            bureau.append(packet.stream_name)

        self.receiver.subscribe(callback, "AtomBureau")
        self.receiver.unsubscribe(callback)

        await self._send("AtomBureau", count=5)

        self.assertEqual(bureau, [])


class ConfigTest(unittest.TestCase):
    def _load(self, satellites: list) -> list:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"satellites": satellites, "debug_logging": False}, f)
        try:
            return load_options(f.name)[0]
        finally:
            os.unlink(f.name)

    def test_missing_and_null_options_use_defaults(self) -> None:
        sats = self._load([
            {"name": "Bureau", "wyoming_port": 10700, "tts_vban_batch_packets": None},
            {"name": "Salon", "wyoming_port": 10701},
        ])
        self.assertEqual(sats[0].tts_vban_batch_packets, 1)
        self.assertEqual(sats[0].tts_output, "vban")
        self.assertEqual([s.tts_http_port for s in sats], [10800, 10801])
        validate(sats)

    def test_shared_port_requires_same_mode(self) -> None:
        sats = self._load([
            {"name": "Bureau", "wyoming_port": 10700, "vban_receive_port": 6980},
            {
                "name": "Salon", "wyoming_port": 10701, "vban_receive_port": 6980,
                "vban_receive_mode": "multicast",
                "vban_receive_multicast_group": "239.0.0.1",
            },
        ])
        with self.assertRaises(ConfigError):
            validate(sats)

    def test_multicast_group_optional_in_unicast(self) -> None:
        sats = self._load([
            {"name": "Bureau", "wyoming_port": 10700},  # group absent
            {"name": "Salon", "wyoming_port": 10701,
             "vban_receive_multicast_group": None},
            # Leftover group in unicast: ignored (no shared-port conflict)
            {"name": "Cuisine", "wyoming_port": 10702,
             "vban_receive_multicast_group": "239.0.0.1"},
        ])
        validate(sats)
        self.assertEqual([s.vban_receive_multicast_group for s in sats], ["", "", ""])

    def test_multicast_without_group_is_rejected(self) -> None:
        for group in (None, "", "  "):
            entry = {"name": "Bureau", "vban_receive_mode": "multicast"}
            if group is not None:
                entry["vban_receive_multicast_group"] = group
            with self.assertRaisesRegex(ConfigError, "multicast group"):
                validate(self._load([entry]))

    def test_duplicate_wyoming_port(self) -> None:
        sats = self._load([
            {"name": "Bureau", "wyoming_port": 10700},
            {"name": "Salon", "wyoming_port": 10700},
        ])
        with self.assertRaises(ConfigError):
            validate(sats)


if __name__ == "__main__":
    unittest.main()
