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
            return load_options(f.name).satellites
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

    def test_block_format(self) -> None:
        sats = self._load([{
            "name": "Bureau", "wyoming_port": 10700, "output": "media_player",
            "microphone": {"vban_port": 6980, "stream_name": "AtomBureau"},
            "vban_output": {"enabled": True, "address": "192.168.1.50", "volume": 0.8},
            "media_player": {"entity": "media_player.nestmini7039"},
            "followup": {"timeout_s": 10},
            "waiting_sounds": {"search": False, "thinking_file": "/share/a.wav"},
            "camera_phrase": {"text": "Je regarde."},
        }])
        validate(sats)
        sat = sats[0]
        self.assertEqual(sat.tts_output, "media_player")
        self.assertEqual(sat.vban_receive_stream_name, "AtomBureau")
        self.assertEqual(sat.vban_receive_mode, "unicast")  # default
        self.assertTrue(sat.tts_vban_enabled)
        self.assertEqual(sat.tts_vban_address, "192.168.1.50")
        self.assertEqual(sat.tts_vban_volume, 0.8)
        self.assertEqual(sat.tts_media_player, "media_player.nestmini7039")
        self.assertEqual(sat.tts_http_port, 10800)  # default: 10800 + index
        self.assertEqual(sat.followup_timeout_s, 10)
        self.assertTrue(sat.followup_enabled)
        self.assertFalse(sat.search_sound_enabled)
        self.assertEqual(sat.thinking_sound, "/share/a.wav")
        self.assertEqual(sat.vision_phrase, "Je regarde.")

    def test_empty_blocks_use_defaults(self) -> None:
        sats = self._load([{
            "name": "Salon", "wyoming_port": 10701,
            "microphone": {"vban_port": 6980}, "vban_output": {},
            "media_player": {}, "followup": {}, "waiting_sounds": {},
            "camera_phrase": {},
        }])
        validate(sats)
        sat = sats[0]
        self.assertEqual(sat.tts_output, "vban")
        self.assertFalse(sat.tts_vban_enabled)
        self.assertTrue(sat.followup_enabled and sat.followup_beep)
        self.assertEqual(sat.thinking_delay_ms, 1000)
        self.assertEqual(sat.vision_phrase, "Attends, j'observe.")

    def test_announcement_defaults(self) -> None:
        sats = self._load([{"name": "Bureau", "wyoming_port": 10700,
                            "waiting_sounds": {}, "camera_phrase": {}}])
        validate(sats)
        sat = sats[0]
        self.assertEqual(
            (sat.listen_end_mode, sat.thinking_mode, sat.search_mode, sat.vision_mode),
            ("none", "phrase", "sound_then_phrase", "phrase"),
        )
        self.assertEqual(
            (sat.thinking_text, sat.search_text, sat.vision_phrase),
            ("Je réfléchis.", "Je cherche sur internet.", "Attends, j'observe."),
        )

    def test_announcement_migration_from_3_1(self) -> None:
        sats = self._load([{
            "name": "Bureau", "wyoming_port": 10700,
            "waiting_sounds": {"thinking": True, "thinking_delay_ms": 1500, "search": False},
            "camera_phrase": {"enabled": False, "text": "Je regarde."},
        }])
        with self.assertLogs("wyoming_vban.config", "WARNING") as logs:
            validate(sats)
        sat = sats[0]
        self.assertEqual((sat.thinking_mode, sat.search_mode, sat.vision_mode),
                         ("sound", "none", "none"))
        self.assertEqual(sat.thinking_delay_ms, 1500)
        self.assertEqual(sat.vision_phrase, "Je regarde.")
        self.assertIn("thinking_mode", logs.output[0])

    def test_explicit_mode_wins_over_old_switch(self) -> None:
        sats = self._load([{
            "name": "Bureau", "wyoming_port": 10700,
            "waiting_sounds": {"thinking": True, "thinking_mode": "none",
                               "listen_end_mode": "sound"},
            "camera_phrase": {"mode": "sound_then_phrase", "file": "/share/o.wav"},
        }])
        validate(sats)
        sat = sats[0]
        self.assertEqual((sat.thinking_mode, sat.listen_end_mode, sat.vision_mode),
                         ("none", "sound", "sound_then_phrase"))
        self.assertEqual(sat.vision_sound, "/share/o.wav")

    def test_unknown_mode_uses_default(self) -> None:
        sats = self._load([{"name": "Bureau", "wyoming_port": 10700,
                            "waiting_sounds": {"search_mode": "loud"}}])
        with self.assertLogs("wyoming_vban.config", "WARNING"):
            validate(sats)
        self.assertEqual(sats[0].search_mode, "sound_then_phrase")

    def test_duplicate_wyoming_port(self) -> None:
        sats = self._load([
            {"name": "Bureau", "wyoming_port": 10700},
            {"name": "Salon", "wyoming_port": 10700},
        ])
        with self.assertRaises(ConfigError):
            validate(sats)


if __name__ == "__main__":
    unittest.main()
