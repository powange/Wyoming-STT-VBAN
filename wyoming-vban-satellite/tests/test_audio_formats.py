"""VBAN formats, stream names, credential masking and small config rules.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements)
"""

import array
import audioop
import dataclasses
import math
import struct
import tempfile
import unittest
from pathlib import Path

from wyoming_vban.config import SatelliteConfig, vban_name
from wyoming_vban.const import (
    VBAN_DATATYPE_FLOAT32,
    VBAN_DATATYPE_INT16,
    VBAN_DATATYPE_UINT8,
)
from wyoming_vban.rtsp_source import mask_url
from wyoming_vban.runtime import needs_restart
from wyoming_vban.tts_phrase import TtsPhrase, remove_stale_phrases
from wyoming_vban.vban import VbanPacket, WyomingConverter, build_packet, to_mono16


def packet(payload: bytes, rate=16000, channels=1, data_format=VBAN_DATATYPE_INT16) -> VbanPacket:
    return VbanPacket(sample_rate=rate, samples_per_frame=len(payload) // 2 or 1,
                      channels=channels, data_format=data_format, codec=0,
                      stream_name="Mic", frame_counter=0, payload=payload)


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

    def test_more_channels_keep_the_first(self) -> None:
        four = struct.pack("<8h", 1, 2, 3, 4, 5, 6, 7, 8)
        self.assertEqual(list(array.array("h", to_mono16(four, VBAN_DATATYPE_INT16, 4))), [1, 5])


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


if __name__ == "__main__":
    unittest.main()
