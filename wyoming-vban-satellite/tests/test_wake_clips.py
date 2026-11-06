"""Wake word clips: capture around a detection, sorting, WakeForge inbox.

Run from wyoming-vban-satellite/:  python3 -m unittest discover tests
(needs the app requirements)
"""

import array
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from wyoming.wake import Detection

from test_followup import FollowupTest
from wyoming_vban.satellite import FollowupSettings, VbanSatelliteHandler
from wyoming_vban.wake_clips import ClipRecorder, ClipStore, suggest, suggestion, wake_word_folder

RATE = 16000
CHUNK = 1024  # samples, like the satellite's chunks to HA


def chunk(value: int) -> bytes:
    return array.array("h", [value] * CHUNK).tobytes()


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.inbox = root / "inbox"
        self.store = ClipStore(str(root / "clips"), str(self.inbox), 30, 2000)

    def tearDown(self) -> None:
        self.tmp.cleanup()


class RecorderTest(StoreCase):
    def test_clip_covers_before_and_after_across_a_mic_gap(self) -> None:
        recorder = ClipRecorder(self.store, "Salon")
        t = 1000.0
        # 2 s of audio "1", then the mic is cut 2.4 s (Atom playing), then "2"
        for _ in range(int(2 * RATE / CHUNK)):
            recorder.sent(chunk(1), t)
            t += CHUNK / RATE
        t += 2.4
        for _ in range(int(2 * RATE / CHUNK)):
            recorder.sent(chunk(2), t)
            t += CHUNK / RATE
        # Detection 0.6 s after the mic came back
        detection = recorder.sample_at(t - 2 + 0.6)
        clip = array.array("h", recorder.extract(detection))
        self.assertEqual(len(clip), int(3.0 * RATE))
        # Counted in samples sent: no hole where the mic was cut
        before = clip[: int(2.5 * RATE)]
        self.assertGreater(before.count(1), RATE)  # the audio before the gap…
        self.assertGreater(before.count(2), RATE // 2)  # …and after it
        self.assertEqual(before.count(0), 0)
        self.assertEqual(set(clip[int(2.5 * RATE):]), {2})  # the 0.5 s after

    def test_start_of_stream_is_padded(self) -> None:
        recorder = ClipRecorder(self.store, "Salon")
        for i in range(10):
            recorder.sent(chunk(5), 1000.0 + i * CHUNK / RATE)
        clip = recorder.extract(CHUNK * 2)
        self.assertEqual(len(clip), int(3.0 * RATE) * 2)
        self.assertIn(5, array.array("h", clip))

    def test_latency_learnt_from_timestamps(self) -> None:
        recorder = ClipRecorder(self.store, "Salon")
        for i in range(30):
            recorder.sent(chunk(1), 1000.0 + i * CHUNK / RATE)
        sample = 20 * CHUNK
        for _ in range(20):
            recorder.learn_latency(sample, recorder.time_of(sample) + 0.8)
        self.assertAlmostEqual(recorder.latency_s, 0.8, delta=0.05)


class StoreTest(StoreCase):
    def add(self, satellite="Salon", **meta) -> str:
        return self.store.add(satellite, "dis_moi_jarvis-6", chunk(1000) * 47, **meta)

    def test_suggestion(self) -> None:
        self.assertEqual(suggestion({"transcript": ""}), "probablement_faux")
        self.assertEqual(suggestion({"transcript": "*bip*"}), "probablement_faux")
        self.assertEqual(suggestion({"transcript": "..."}), "probablement_faux")
        self.assertEqual(suggestion({"stt_error": "stt-no-text-recognized"}),
                         "probablement_faux")
        self.assertEqual(suggest({"transcript": "allume la lumière", "response": "C'est fait."}),
                         ("probablement_bon", "phrase normale"))
        self.assertEqual(suggest({"transcript": "et après il m'a dit qu'il viendrait demain "
                                                "soir pour manger avec nous à la maison si "
                                                "tout va bien", "response": "D'accord."})[1],
                         "conversation captée")
        self.assertEqual(suggest({"transcript": "le chien", "response": "Désolé, je n'ai pas compris."}),
                         ("probablement_faux", "conversation captée"))
        self.assertEqual(suggest({"transcript": "*bip*"})[1], "bip capté")
        self.assertEqual(suggestion({"transcript": "allume la lumière"}), "a_verifier")  # no response
        self.assertEqual(suggestion({"transcript": None}), "a_verifier")
        self.assertEqual(suggest({"transcript": "allume", "response": "Ok.", "reported_false": True}),
                         ("probablement_faux", "signalé faux depuis Home Assistant"))

    def test_unvalidated_clips_never_reach_the_inbox(self) -> None:
        good = self.add(transcript="allume la lumière", response="C'est fait.")
        false = self.add(transcript="")
        self.assertEqual(self.store.list()[0]["suggestion"], "probablement_faux")
        self.assertEqual({c["id"]: c["suggestion"] for c in self.store.list()}[good],
                         "probablement_bon")
        self.assertEqual(list(self.inbox.rglob("*.wav")), [])  # neither one
        self.assertFalse((self.inbox / "dis_moi_jarvis" / "manifest.json").exists())
        del false

    def test_review_queue_order(self) -> None:
        older = self.add(transcript="allume", response="Ok.")
        newer = self.add(transcript="allume", response="Ok.")
        false = self.add(transcript="")
        sorted_one = self.add(transcript="allume", response="Ok.")
        self.store.classify(sorted_one, "bon")
        self.assertEqual([c["id"] for c in self.store.review_queue()], [false, older, newer])

    def test_undo_puts_it_back_in_the_queue_and_out_of_the_inbox(self) -> None:
        clip_id = self.add(transcript="allume", response="Ok.")
        self.store.classify(clip_id, "bon")
        self.assertEqual(len(list(self.inbox.rglob("*.wav"))), 1)
        self.assertEqual(self.store.review_queue(), [])
        self.store.classify(clip_id, "non_classe")  # "Annuler le dernier choix"
        self.assertEqual([c["id"] for c in self.store.review_queue()], [clip_id])
        self.assertEqual(list(self.inbox.rglob("*.wav")), [])
        manifest = json.loads((self.inbox / "dis_moi_jarvis" / "manifest.json").read_text())
        self.assertEqual(manifest["clips"], [])

    def test_folder_without_version(self) -> None:
        self.assertEqual(wake_word_folder("dis_moi_jarvis-6"), "dis_moi_jarvis")
        self.assertEqual(wake_word_folder(""), "inconnu")

    def test_reclassify_moves_the_file_and_updates_the_manifest(self) -> None:
        clip_id = self.add(transcript="allume")
        folder = self.inbox / "dis_moi_jarvis"
        self.store.classify(clip_id, "faux")
        self.assertEqual(len(list((folder / "negatifs").glob("*.wav"))), 1)
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual([c["status"] for c in manifest["clips"]], ["faux"])

        self.store.classify(clip_id, "bon")
        self.assertEqual(list((folder / "negatifs").glob("*.wav")), [])
        positives = list((folder / "positifs").glob("Salon_*.wav"))
        self.assertEqual(len(positives), 1)
        self.assertTrue(positives[0].with_suffix(".json").exists())
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(len(manifest["clips"]), 1)
        entry = manifest["clips"][0]
        self.assertEqual((entry["status"], entry["satellite"], entry["transcript"]),
                         ("bon", "Salon", "allume"))
        self.assertTrue(entry["path"].startswith("positifs/"))
        self.assertEqual(len(entry["sha256"]), 64)

        self.store.classify(clip_id, "ignore")  # out of the inbox
        self.assertEqual(list(folder.glob("*/*.wav")), [])
        self.assertEqual(json.loads((folder / "manifest.json").read_text())["clips"], [])

    def test_retention_removes_unsorted_first(self) -> None:
        self.store.max_clips = 3
        old_sorted = self.add()
        self.store.classify(old_sorted, "faux")
        old_unsorted = self.add()
        newer = [self.add() for _ in range(2)]  # 4 clips: one too many
        ids = {c["id"] for c in self.store.list()}
        self.assertNotIn(old_unsorted, ids)
        self.assertIn(old_sorted, ids)
        self.assertTrue(set(newer) <= ids)

    def test_retention_days(self) -> None:
        old = self.add()
        kept = self.add()
        self.store.classify(kept, "bon")
        for clip_id in (old, kept):
            self.store._clips[clip_id]["time"] = time.time() - 40 * 86400
        self.store.purge()
        ids = {c["id"] for c in self.store.list()}
        self.assertEqual(ids, {kept})  # sorted clips are training data

    def test_reloaded_from_disk(self) -> None:
        clip_id = self.add(transcript="bonjour")
        again = ClipStore(str(self.store.root), str(self.inbox))
        self.assertEqual(again.list()[0]["id"], clip_id)
        self.assertEqual(again.counts()[time.strftime("%Y-%m-%d")]["Salon"]["non_classe"], 1)


class CaptureTest(FollowupTest):
    """Through the satellite: wake run detection → clip; follow-up → none."""

    followup = FollowupSettings(enabled=True, beep=False, timeout_s=0.5)

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ClipStore(f"{self.tmp.name}/clips", f"{self.tmp.name}/inbox")
        self.recorder = ClipRecorder(self.store, "Bureau")
        await super().asyncSetUp()

    async def asyncTearDown(self) -> None:
        await super().asyncTearDown()
        self.tmp.cleanup()

    def make_handler(self, reader, writer):
        return VbanSatelliteHandler(
            reader, writer, self.info, self.receiver, self.sender, followup=self.followup,
            exchange_publisher=self.publisher, satellite_name="Bureau",
            waiting_cues=self.waiting_cues, clip_recorder=lambda: self.recorder,
        )

    async def feed_mic(self, seconds: float) -> None:
        from wyoming_vban.vban import VbanPacket
        for i in range(int(seconds / 0.016)):
            self.receiver.callback(VbanPacket(
                sample_rate=16000, samples_per_frame=256, channels=1, data_format=1,
                codec=0, stream_name="AtomBureau", frame_counter=i,
                payload=b"\x00\x10" * 256))
            await asyncio.sleep(0.016)

    async def test_clip_on_wake_run_not_on_follow_up(self) -> None:
        await self.next_run()  # wake run
        mic = asyncio.create_task(self.feed_mic(6.0))
        await asyncio.sleep(3.0)
        await self.ha.write_event(Detection(name="dis_moi_jarvis-6", timestamp=2500).event())
        await asyncio.sleep(1.0)
        clips = self.store.list()
        self.assertEqual(len(clips), 1)
        self.assertEqual((clips[0]["wake_word"], clips[0]["detection_source"]),
                         ("dis_moi_jarvis-6", "timestamp"))
        # What followed completes the clip
        await self.respond("Quelle lumière ?", transcript="Éteins la lumière")
        await asyncio.sleep(0.2)
        clip = self.store.list()[0]
        self.assertEqual((clip["transcript"], clip["response"]),
                         ("Éteins la lumière", "Quelle lumière ?"))
        # The follow-up run (start=asr) has no wake word: no clip
        self.assertEqual((await self.next_run()).start_stage, "asr")
        await self.ha.write_event(Detection(name="dis_moi_jarvis-6").event())
        await asyncio.sleep(1.0)
        mic.cancel()
        await asyncio.gather(mic, return_exceptions=True)
        self.assertEqual(len(self.store.list()), 1)

    async def test_no_text_suggests_false(self) -> None:
        await self.next_run()
        mic = asyncio.create_task(self.feed_mic(4.0))
        await asyncio.sleep(1.5)
        await self.ha.write_event(Detection(name="dis_moi_jarvis-6").event())
        await asyncio.sleep(0.8)
        from wyoming.error import Error
        await self.ha.write_event(
            Error(text="No text recognized", code="stt-no-text-recognized").event())
        await asyncio.sleep(0.2)
        mic.cancel()
        await asyncio.gather(mic, return_exceptions=True)
        clip = self.store.list()[0]
        self.assertEqual((clip["stt_error"], clip["suggestion"]),
                         ("stt-no-text-recognized", "probablement_faux"))


if __name__ == "__main__":
    unittest.main()


class ApiTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        import copy
        import os
        import socket

        import aiohttp

        from test_webui import FakeSupervisor, _free_port
        from wyoming_vban.config import parse_options, validate
        from wyoming_vban.runtime import AppRuntime
        from wyoming_vban.webui import SupervisorOptions, WebUI

        os.environ["SUPERVISOR_TOKEN"] = "token"
        self.tmp = tempfile.TemporaryDirectory()
        options = {
            "satellites": [{
                "name": "Salon", "wyoming_port": _free_port(), "output": "none",
                "microphone": {"vban_port": _free_port(socket.SOCK_DGRAM)},
                "vban_output": {}, "media_player": {}, "followup": {"enabled": False},
                "waiting_sounds": {}, "camera_phrase": {},
            }],
            "publish_exchanges": False, "debug_mirror": [], "wake_clips": {"enabled": True},
        }
        self.supervisor = FakeSupervisor(options)
        await self.supervisor.start()
        self.runtime = AppRuntime(self.tmp.name, f"{self.tmp.name}/clips", f"{self.tmp.name}/inbox")
        parsed = parse_options(copy.deepcopy(options))
        validate(parsed.satellites)
        await self.runtime.start(parsed)
        self.port = _free_port()
        self.ui = WebUI(self.runtime, self.port,
                        SupervisorOptions(f"http://127.0.0.1:{self.supervisor.port}"),
                        allowed_ips=frozenset({"127.0.0.1"}))
        await self.ui.start()
        self.http = aiohttp.ClientSession()

    async def asyncTearDown(self) -> None:
        await self.http.close()
        await self.ui.stop()
        await self.runtime.stop()
        await self.supervisor.stop()
        self.tmp.cleanup()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    async def test_list_classify_and_report(self) -> None:
        self.assertIsNotNone(self.runtime.runners["Salon"].clip_recorder)
        store = self.runtime.clip_store
        first = store.add("Salon", "dis_moi_jarvis-6", chunk(500) * 47)
        second = store.add("Salon", "dis_moi_jarvis-6", chunk(500) * 47)
        async with self.http.get(self.url("/api/wake_clips?status=non_classe")) as resp:
            body = await resp.json()
        self.assertEqual([c["id"] for c in body["clips"]], [second, first])
        async with self.http.get(self.url(f"/api/wake_clips/audio?id={first}")) as resp:
            self.assertEqual((resp.status, (await resp.read())[:4]), (200, b"RIFF"))
        async with self.http.post(self.url("/api/wake_clips/classify"),
                                  json={"id": first, "status": "bon"}) as resp:
            self.assertEqual(resp.status, 200)
        async with self.http.post(self.url("/api/wake_clips/classify"),
                                  json={"id": first, "status": "peut-être"}) as resp:
            self.assertEqual(resp.status, 400)
        # "Ce n'était pas pour toi" from HA: flagged for the review, not sorted
        self.runtime._on_false_wake({"satellite": "Salon"})
        clip = next(c for c in store.list() if c["id"] == second)
        self.assertEqual((clip["status"], clip["suggestion"]), ("non_classe", "probablement_faux"))
        async with self.http.get(self.url("/api/wake_clips/review")) as resp:
            body = await resp.json()
        self.assertEqual(([c["id"] for c in body["queue"]], body["to_review"], body["done_today"]),
                         ([second], 1, 1))

    async def test_review_notification_over_20(self) -> None:
        sent = []

        async def notify(count: int) -> None:
            sent.append(count)

        self.runtime._notify_review = notify
        self.runtime.options.wake_clips_notify = True
        store = self.runtime.clip_store
        ids = [store.add("Salon", "dis_moi_jarvis-6", chunk(500)) for _ in range(21)]
        await asyncio.sleep(0.1)
        self.assertEqual(sent, [21])  # only once it exceeds 20
        store.classify(ids[0], "faux")
        await asyncio.sleep(0.1)
        self.assertEqual(sent, [21, 0])  # back to 20: removed
