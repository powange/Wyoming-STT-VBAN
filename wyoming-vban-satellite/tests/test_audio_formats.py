"""VBAN formats, stream names, credential masking and small config rules.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements)
"""

import array
import asyncio
import audioop
import dataclasses
import math
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

import aiohttp

from wyoming_vban.config import ConfigError, SatelliteConfig, validate, vban_name
from wyoming_vban.const import (
    VBAN_DATATYPE_FLOAT32,
    VBAN_DATATYPE_INT16,
    VBAN_DATATYPE_UINT8,
)
from wyoming_vban.media_player import MediaPlayerOutput
from wyoming_vban.rtsp_source import mask_url
from wyoming_vban.runtime import needs_restart
from wyoming_vban.tts_phrase import TtsPhrase, remove_stale_phrases
from wyoming_vban.vban import VbanPacket, WyomingConverter, build_packet, to_mono16


def packet(payload: bytes, rate=16000, channels=1, data_format=VBAN_DATATYPE_INT16) -> VbanPacket:
    return VbanPacket(sample_rate=rate, samples_per_frame=len(payload) // 2 or 1,
                      channels=channels, data_format=data_format, codec=0,
                      stream_name="Mic", frame_counter=0, payload=payload)


def _free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class ConversionTest(unittest.TestCase):
    def test_resampling_is_continuous(self) -> None:
        # 1 s of 48 kHz tone in 256-sample packets: 16000 samples, no clicks
        n = 48000
        tone = struct.pack(f"<{n}h", *(int(8000 * math.sin(2 * math.pi * 440 * i / n * 1))
                                       for i in range(n)))
        converter = WyomingConverter()
        out = b"".join(converter.convert(packet(tone[i:i + 512], rate=48000))
                       for i in range(0, len(tone), 512))
        self.assertAlmostEqual(len(out) // 2, 16000, delta=2)
        whole, _ = audioop.ratecv(tone, 2, 1, 48000, 16000, None)
        diff = max(abs(a - b) for a, b in zip(array.array("h", out), array.array("h", whole)))
        self.assertLess(diff, 50)

    def test_unsigned_8_bit(self) -> None:
        silence = bytes([128]) * 256
        self.assertEqual(audioop.max(to_mono16(silence, VBAN_DATATYPE_UINT8, 1), 2), 0)

    def test_float32(self) -> None:
        pcm = to_mono16(array.array("f", [0.5, -0.5, 1.5]).tobytes(), VBAN_DATATYPE_FLOAT32, 1)
        self.assertEqual(list(array.array("h", pcm)), [16383, -16383, 32767])

    def test_stereo_is_averaged_not_summed(self) -> None:
        stereo = struct.pack("<4h", 30000, 30000, -1000, 3000)
        self.assertEqual(list(array.array("h", to_mono16(stereo, VBAN_DATATYPE_INT16, 2))),
                         [30000, 1000])

    def test_float_stereo_averaged_nan_silent(self) -> None:
        stereo = array.array("f", [0.5, 0.0, float("nan"), 0.5, float("inf"), 1.0]).tobytes()
        self.assertEqual(list(array.array("h", to_mono16(stereo, VBAN_DATATYPE_FLOAT32, 2))),
                         [8191, 0, 32767])

    def test_12_and_10_bit_formats_refused(self) -> None:
        from wyoming_vban.vban import parse_packet
        good = build_packet(b"\0" * 4, "Mic", 16000, 1, 2, 0)
        self.assertIsNotNone(parse_packet(good))
        odd = bytearray(good)
        odd[7] = (odd[7] & 0xF0) | 6
        with self.assertLogs("wyoming_vban.vban", "WARNING"):
            self.assertIsNone(parse_packet(bytes(odd)))

    def test_more_channels_keep_the_first(self) -> None:
        four = struct.pack("<8h", 1, 2, 3, 4, 5, 6, 7, 8)
        self.assertEqual(list(array.array("h", to_mono16(four, VBAN_DATATYPE_INT16, 4))), [1, 5])


class SenderTest(unittest.IsolatedAsyncioTestCase):
    def test_host_name_resolved_once(self) -> None:
        from unittest import mock
        from wyoming_vban.vban import UdpTarget
        target = UdpTarget()
        target.RETRY_S = 0.0  # (a send error resolves again at most every RETRY_S)
        with mock.patch("socket.gethostbyname", return_value="192.0.2.7") as resolve:
            for _ in range(50):
                self.assertEqual(target.get("enceinte.local", 6980), ("192.0.2.7", 6980))
            target.invalidate()  # a send error: resolved again, once
            target.get("enceinte.local", 6980)
            target.get("enceinte.local", 6980)
            target.get("autre.local", 6980)  # a new address
        self.assertEqual(resolve.call_count, 3)

    async def test_tail_sent_without_waiting(self) -> None:
        from wyoming_vban.vban import VbanSender
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as listener:
            listener.bind(("127.0.0.1", 0))
            sender = VbanSender("127.0.0.1", listener.getsockname()[1])
            sender.open()
            try:
                sender.send(b"\1\0" * 100)  # under a packet, under the pre-buffer
                start = time.monotonic()
                await sender.wait_sent()
                self.assertLess(time.monotonic() - start, 0.3)
                self.assertEqual(sender.pending_seconds(), 0)
            finally:
                await sender.close()


class StreamNameTest(unittest.TestCase):
    def test_names_made_ascii(self) -> None:
        with self.assertLogs("wyoming_vban.config", "WARNING"):
            self.assertEqual(vban_name("Entrée", "x", "Salon"), "Entre")
        with self.assertLogs("wyoming_vban.config", "WARNING"):
            self.assertEqual(vban_name("SalonMicrophone01", "x", "Salon"), "SalonMicrophone0")
        self.assertEqual(vban_name("AtomBureau", "x", "Bureau"), "AtomBureau")

    def test_build_packet_never_fails_on_a_name(self) -> None:
        self.assertEqual(len(build_packet(b"\0" * 4, "Entrée", 16000, 1, 2, 0)), 28 + 4)
        with self.assertRaises(ValueError):
            build_packet(b"\0" * 4, "Mic", 12345, 1, 2, 0)

    def test_options_cleaned(self) -> None:
        sat = SatelliteConfig(name=" Salon ", tts_media_player="media_player.nest ",
                              tts_vban_address=" 192.168.1.5", vban_receive_stream_name="Micé")
        with self.assertLogs("wyoming_vban.config", "WARNING"):
            sat.normalize()
        self.assertEqual((sat.name, sat.tts_media_player, sat.tts_vban_address,
                          sat.vban_receive_stream_name),
                         ("Salon", "media_player.nest", "192.168.1.5", "Mic"))


class MaskTest(unittest.TestCase):
    def test_passwords_with_at_and_slash(self) -> None:
        self.assertEqual(mask_url("rtsp://admin:p@ss@cam/x"), "rtsp://***@cam/x")
        self.assertEqual(mask_url("rtsp://admin:ab/cd@cam:554/x"), "rtsp://***@cam:554/x")
        self.assertEqual(mask_url("rtsp://cam/x"), "rtsp://cam/x")


class RestartTest(unittest.TestCase):
    def test_http_port_only_counts_for_media_player(self) -> None:
        old = SatelliteConfig(name="Salon", tts_output="vban", tts_http_port=10802)
        new = dataclasses.replace(old, tts_http_port=10801)
        self.assertFalse(needs_restart(old, new))
        old.tts_output = new.tts_output = "media_player"
        self.assertTrue(needs_restart(old, new))


class PhraseCacheTest(unittest.TestCase):
    def test_last_phrase_removed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            stale = Path(folder) / f"{TtsPhrase.CACHE_PREFIX}old.wav"
            stale.write_bytes(b"x")
            remove_stale_phrases([], folder)
            self.assertFalse(stale.exists())


class OptionChecksTest(unittest.TestCase):
    def test_multicast_group_checked(self) -> None:
        sat = SatelliteConfig(name="Salon", vban_receive_mode="multicast",
                              vban_receive_multicast_group="192.168.1.5", tts_output="none")
        with self.assertRaisesRegex(ConfigError, "multicast"):
            validate([sat], strict=True)
        sat.vban_receive_multicast_group = "239.0.0.1"
        validate([sat], strict=True)

    def test_options_without_their_output_are_said(self) -> None:
        sat = SatelliteConfig(name="Salon", tts_output="vban", tts_vban_address="192.0.2.1",
                              tts_keep_awake=True, tts_end_margin_ms=900)
        with self.assertLogs("wyoming_vban.config", "WARNING") as logs:
            validate([sat], strict=True)
        self.assertTrue(any("keep_awake" in line for line in logs.output))
        self.assertTrue(any("end_margin_ms" in line for line in logs.output))
        sat = SatelliteConfig(name="Salon", tts_output="media_player",
                              tts_media_player="media_player.x", followup_enabled=True,
                              followup_beep_output="vban")
        with self.assertLogs("wyoming_vban.config", "WARNING") as logs:
            validate([sat], strict=True)
        self.assertTrue(any("beep_output" in line for line in logs.output))


class FilterBypassTest(unittest.TestCase):
    def test_same_settings_try_the_clean_up_again(self) -> None:
        from wyoming_vban.mic_filter import MicFilter
        mic_filter = MicFilter("medium", False, 2.0)
        mic_filter.bypassed = True
        mic_filter.configure("medium", False, 2.0)
        self.assertFalse(mic_filter.bypassed)


class MulticastInterfaceTest(unittest.TestCase):
    def sat(self, interface: str) -> SatelliteConfig:
        return SatelliteConfig(name="Salon", vban_receive_mode="multicast",
                               vban_receive_multicast_group="239.0.0.1",
                               vban_receive_multicast_interface=interface, tts_output="none")

    def test_address_checked(self) -> None:
        validate([self.sat("192.168.1.10")], strict=True)
        with self.assertRaises(ConfigError):
            validate([self.sat("eth0")], strict=True)

    def test_unicast_ignores_it(self) -> None:
        sat = self.sat("eth0")
        sat.vban_receive_mode = "unicast"
        validate([sat], strict=True)
        self.assertEqual(sat.vban_receive_multicast_interface, "")


class PortConflictTest(unittest.TestCase):
    def test_http_port_on_a_wyoming_port(self) -> None:
        a = SatelliteConfig(name="A", wyoming_port=10700, tts_output="none")
        b = SatelliteConfig(name="B", wyoming_port=10701, tts_output="media_player",
                            tts_media_player="media_player.x", tts_http_port=10700)
        with self.assertRaisesRegex(ConfigError, "Port 10700"):
            validate([a, b], strict=True)

    def test_interface_port(self) -> None:
        with self.assertRaisesRegex(ConfigError, "configuration interface"):
            validate([SatelliteConfig(name="A", wyoming_port=8743, tts_output="none")],
                     strict=True)


class RangeTest(unittest.IsolatedAsyncioTestCase):
    async def test_partial_wav(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        output = MediaPlayerOutput("media_player.x", "127.0.0.1", port)
        await output.start()
        try:
            url = output.publish(bytes(range(256)) * 4)
            async with aiohttp.ClientSession() as http:
                async with http.get(url, headers={"Range": "bytes=10-19"}) as resp:
                    self.assertEqual(resp.status, 206)
                    self.assertEqual(resp.headers["Content-Range"], "bytes 10-19/1024")
                    self.assertEqual(await resp.read(), bytes(range(10, 20)))
                async with http.get(url) as resp:
                    self.assertEqual((resp.status, len(await resp.read())), (200, 1024))
                    self.assertEqual(resp.headers["Accept-Ranges"], "bytes")
                # Beeps and silences published meanwhile don't remove it
                for _ in range(10):
                    output.publish(b"x")
                async with http.get(url) as resp:
                    self.assertEqual(resp.status, 200)
        finally:
            await output.stop()

    async def test_port_in_use_leaves_nothing_started(self) -> None:
        with socket.socket() as sock:
            sock.bind(("0.0.0.0", 0))
            sock.listen()
            output = MediaPlayerOutput("media_player.x", "127.0.0.1", sock.getsockname()[1])
            with self.assertRaises(OSError):
                await output.start()
            self.assertIsNone(output._runner)


class ReceiverRetryTest(unittest.IsolatedAsyncioTestCase):
    async def test_multicast_interface_missing_doesnt_stop_the_receiver(self) -> None:
        from wyoming_vban.vban import VbanReceiver
        receiver = VbanReceiver(port=_free_udp_port(), mode="multicast",
                                multicast_group="239.0.0.1", multicast_interface="192.0.2.123")
        receiver.RETRY_S = 0.1
        with self.assertLogs("wyoming_vban.vban", "ERROR") as logs:
            task = asyncio.create_task(receiver.run())
            await asyncio.sleep(0.5)
        self.assertFalse(task.done())  # still trying, the app goes on
        self.assertTrue(any("can't open its socket" in line for line in logs.output))
        receiver.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class KeepAwakeTest(unittest.IsolatedAsyncioTestCase):
    async def keep_awake(self, state: str, platform: str) -> list[str]:
        """What the keep awake loop plays in one round."""
        import types
        from wyoming_vban.runtime import SatelliteRunner

        played: list[str] = []

        class Output:
            entity_id = "media_player.salon"

            def publish(self, wav: bytes) -> str:
                return "http://x/silence.wav"

            async def play(self, url: str) -> bool:
                played.append(url)
                return True

        class Watcher:
            async def state_of(self, entity_id):
                return state

            async def call(self, command):
                return {"entity_id": command["entity_id"], "platform": platform}

        runner = types.SimpleNamespace(
            media_output=Output(), cfg=types.SimpleNamespace(tts_keep_awake=True),
            watcher=Watcher(), handlers=[], mic_mute=None, _platforms={},
            KEEP_AWAKE_EVERY_S=0.01, SILENCE_WAV=b"",
        )
        runner._is_cast = lambda entity_id: SatelliteRunner._is_cast(runner, entity_id)
        task = asyncio.create_task(SatelliteRunner._keep_cast_awake(runner))
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return played

    async def test_off_only_for_cast(self) -> None:
        self.assertTrue(await self.keep_awake("off", "cast"))  # its app closed
        self.assertFalse(await self.keep_awake("off", "samsungtv"))  # would turn it on
        self.assertTrue(await self.keep_awake("idle", "samsungtv"))
        self.assertFalse(await self.keep_awake("playing", "cast"))

    async def test_history_forgotten_on_reconnection(self) -> None:
        from wyoming_vban.ha_ws import HaStateWatcher
        watcher = HaStateWatcher(["media_player.salon"], url="http://127.0.0.1:9/ws")
        watcher.BACKOFF_MIN_S = 0.01
        watcher._on_state_trigger({"variables": {"trigger": {
            "to_state": {"entity_id": "media_player.salon", "state": "idle"}}}})
        task = asyncio.create_task(watcher.run())  # connection refused
        await asyncio.sleep(0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(len(watcher._history["media_player.salon"]), 0)


class CommandLineTest(unittest.TestCase):
    def test_every_option_has_a_value(self) -> None:
        import sys
        from wyoming_vban.__main__ import _config_from_args, parse_args
        argv = sys.argv
        sys.argv = ["wyoming_vban", "--name", "X"]
        try:
            cfg = _config_from_args(parse_args())
        finally:
            sys.argv = argv
        self.assertEqual((cfg.name, cfg.wake_clips), ("X", True))


if __name__ == "__main__":
    unittest.main()
