"""Applying a new configuration without restarting what doesn't need it.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import asyncio
import copy
import socket
import tempfile
import unittest

from wyoming_vban.config import parse_options, validate
from wyoming_vban.runtime import AppRuntime
from wyoming_vban.vban import parse_packet


def _free_port(kind: int = socket.SOCK_STREAM) -> int:
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ApplyTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        # Speaker of the Salon satellite: counts the VBAN packets it gets
        self.speaker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.speaker.bind(("127.0.0.1", 0))
        self.speaker.setblocking(False)
        mic_port = _free_port(socket.SOCK_DGRAM)
        self.options = {
            "satellites": [
                {"name": "Bureau", "wyoming_port": _free_port(),
                 "microphone": {"vban_port": mic_port, "stream_name": "AtomBureau"},
                 "vban_output": {}, "media_player": {}, "followup": {},
                 "waiting_sounds": {"thinking_mode": "none", "search_mode": "none"},
                 "camera_phrase": {"mode": "none"}},
                {"name": "Salon", "wyoming_port": _free_port(),
                 "microphone": {"vban_port": mic_port, "stream_name": "AtomSalon"},
                 "vban_output": {"enabled": True, "address": "127.0.0.1",
                                 "port": self.speaker.getsockname()[1]},
                 "media_player": {}, "followup": {},
                 "waiting_sounds": {"thinking_mode": "sound", "search_mode": "none"},
                 "camera_phrase": {"mode": "none"}},
            ],
            "publish_exchanges": False, "debug_mirror": [],
        }
        self.cache = tempfile.TemporaryDirectory()
        self.runtime = AppRuntime(self.cache.name)
        await self.runtime.start(self.parse(self.options))
        await asyncio.sleep(0.3)

    async def asyncTearDown(self) -> None:
        await self.runtime.stop()
        self.speaker.close()
        self.cache.cleanup()

    @staticmethod
    def parse(options: dict):
        parsed = parse_options(copy.deepcopy(options))
        validate(parsed.satellites)
        return parsed

    async def apply(self, change) -> list[str]:
        new = copy.deepcopy(self.options)
        change(new)
        error, done = await self.runtime.apply(self.parse(new))
        self.assertIsNone(error)
        self.options = new
        return done

    def tasks(self) -> dict:
        return {name: r.task for name, r in self.runtime.runners.items()}

    async def test_live_settings_keep_the_satellite_running(self) -> None:
        before = self.tasks()
        handler_cues = self.runtime.runners["Salon"].cues

        def change(o):
            salon = o["satellites"][1]
            salon["vban_output"]["volume"] = 0.4
            salon["followup"]["timeout_s"] = 12
            salon["waiting_sounds"]["thinking_delay_ms"] = 2500
            salon["waiting_sounds"]["listen_end_mode"] = "sound"
        done = await self.apply(change)

        self.assertEqual(done, ["satellite 'Salon' updated without restart"])
        self.assertEqual(self.tasks(), before)  # nothing restarted
        salon = self.runtime.runners["Salon"]
        self.assertEqual(salon.vban_sender.volume, 0.4)
        self.assertEqual(salon.followup.timeout_s, 12)
        self.assertIs(salon.cues, handler_cues)  # same object, new content
        self.assertEqual(salon.cues.thinking_delay_s, 2.5)
        self.assertIsNotNone(salon.cues.listen_end)

    async def test_restart_field_restarts_only_that_satellite(self) -> None:
        before = self.tasks()
        new_port = _free_port()
        done = await self.apply(
            lambda o: o["satellites"][0].__setitem__("wyoming_port", new_port)
        )
        self.assertEqual(done, ["satellite 'Bureau' restarted"])
        after = self.tasks()
        self.assertIsNot(after["Bureau"], before["Bureau"])
        self.assertIs(after["Salon"], before["Salon"])
        _, writer = await asyncio.open_connection("127.0.0.1", new_port)
        writer.close()

    async def test_add_and_remove_satellites(self) -> None:
        def add(o):
            o["satellites"].append({
                "name": "Cuisine", "wyoming_port": _free_port(),
                "microphone": {"vban_port": _free_port(socket.SOCK_DGRAM)},
                "vban_output": {}, "media_player": {}, "followup": {},
                "waiting_sounds": {}, "camera_phrase": {},
            })
        self.assertEqual(await self.apply(add), ["satellite 'Cuisine' added"])
        self.assertEqual(sorted(self.runtime.runners), ["Bureau", "Cuisine", "Salon"])
        self.assertEqual(len(self.runtime._receivers), 2)

        done = await self.apply(lambda o: o["satellites"].pop())
        self.assertEqual(done, ["satellite 'Cuisine' removed"])
        self.assertEqual(len(self.runtime._receivers), 1)  # its port is closed

    async def test_receiver_change_restarts_its_satellites(self) -> None:
        before = self.tasks()
        new_port = _free_port(socket.SOCK_DGRAM)
        done = await self.apply(
            lambda o: [s["microphone"].__setitem__("vban_port", new_port)
                       for s in o["satellites"]]
        )
        self.assertEqual(sorted(done), ["satellite 'Bureau' restarted",
                                        "satellite 'Salon' restarted"])
        self.assertEqual(list(self.runtime._receivers), [new_port])
        self.assertIsNot(self.tasks()["Salon"], before["Salon"])

    async def test_test_signal_plays_on_the_speaker(self) -> None:
        played = await self.runtime.runners["Salon"].test("thinking")
        self.assertEqual(played, ["sound"])
        await asyncio.sleep(0.2)
        packets = []
        while True:
            try:
                packets.append(parse_packet(self.speaker.recv(2048)))
            except BlockingIOError:
                break
        self.assertTrue(packets)
        self.assertEqual(await self.runtime.runners["Bureau"].test("thinking"), [])

    async def test_failed_change_restores_previous(self) -> None:
        busy = socket.socket()
        busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        try:
            new = copy.deepcopy(self.options)
            new["satellites"][0]["wyoming_port"] = busy.getsockname()[1]
            error, _ = await self.runtime.apply(self.parse(new))
        finally:
            busy.close()
        self.assertIn("satellite 'Bureau' stopped", error)
        self.assertEqual(sorted(self.runtime.runners), ["Bureau", "Salon"])
        self.assertEqual(
            self.runtime.options.satellites[0].wyoming_port,
            self.options["satellites"][0]["wyoming_port"],
        )
        self.assertFalse(self.runtime.fatal.is_set())

    async def test_status(self) -> None:
        status = self.runtime.status()
        self.assertEqual([s["name"] for s in status["satellites"]], ["Bureau", "Salon"])
        self.assertIsNone(status["ha_websocket"])  # no HA connection needed here
        self.assertEqual(status["exchanges"], [])


if __name__ == "__main__":
    unittest.main()
