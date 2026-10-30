"""Microphone mute while a response plays on the media_player.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements: wyoming, aiohttp)
"""

import time
import unittest

from wyoming_vban.satellite import MicMute


class MicMuteTest(unittest.TestCase):
    def test_muted_from_start_until_response_ends(self) -> None:
        mute = MicMute(tail_s=0.25, max_muted_s=300)
        self.assertFalse(mute.is_muted())

        gen = mute.start_response()
        self.assertTrue(mute.is_muted())

        mute.end_response(gen)  # played + tail, or play_media failed
        self.assertFalse(mute.is_muted())

    def test_older_response_does_not_unmute_newer_one(self) -> None:
        mute = MicMute(tail_s=0.0, max_muted_s=300)
        old = mute.start_response()
        mute.start_response()  # new response arrives, old task cancelled
        mute.end_response(old)
        self.assertTrue(mute.is_muted())

    def test_safety_unmute(self) -> None:
        mute = MicMute(tail_s=0.0, max_muted_s=0.05)
        mute.start_response()  # AudioStop never comes
        time.sleep(0.1)
        self.assertFalse(mute.is_muted())


if __name__ == "__main__":
    unittest.main()
