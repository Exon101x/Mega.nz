"""The bot must never hold more than one file (plus, when splitting, one part) on disk at a time."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import stubs  # noqa: E402

stubs.install_if_missing()

import uploader as U  # noqa: E402
from queue_worker import QueueManager  # noqa: E402
from tests.test_core import FakeUploader, make_app, make_job, run  # noqa: E402


class Tracker:
    def __init__(self):
        self.active = self.peak = 0

    def enter(self):
        self.active += 1
        self.peak = max(self.peak, self.active)

    def leave(self):
        self.active -= 1


class OneFileAtATime(unittest.TestCase):
    def _run_two_jobs(self, slots):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp)
                app.cfg.max_concurrent = 2          # two jobs may run at the same time...
                app.cfg.max_files_on_disk = slots   # ...but only `slots` files may be on disk
                app.queue = QueueManager(app)
                track = Tracker()

                async def fetch(link, handle, key, dest, creds, name_hint="", on_progress=None, abort=None, stall_timeout=0):
                    track.enter()
                    await asyncio.sleep(0.15)
                    dest.mkdir(parents=True, exist_ok=True)
                    p = dest / "f.bin"
                    p.write_bytes(b"x" * 100)
                    return p

                class Up(FakeUploader):
                    async def send(self, *a, **k):
                        await asyncio.sleep(0.15)
                        res = await super().send(*a, **k)
                        track.leave()  # the file is uploaded and about to be deleted
                        return res

                app.mega.fetch = fetch
                app.uploader = Up()
                j1, _ = await make_job(app, files=2)
                j2, _ = await make_job(app, files=2)
                await app.queue._claim()
                await app.queue._claim()
                await asyncio.gather(app.queue._run_job(j1), app.queue._run_job(j2))
                for j in (j1, j2):
                    self.assertEqual((await app.db.one("SELECT status FROM jobs WHERE id=?", (j,)))["status"], "done")
                self.assertEqual(list(app.cfg.download_dir.iterdir()), [])
                return track.peak
        return run(go())

    def test_default_is_strictly_one_file_even_with_two_workers(self):
        self.assertEqual(self._run_two_jobs(1), 1)

    def test_cap_can_be_raised_deliberately(self):
        self.assertEqual(self._run_two_jobs(2), 2)


class LazySplit(unittest.TestCase):
    def test_parts_are_cut_and_sent_one_at_a_time(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                folder = Path(tmp)
                original = folder / "movie.bin"
                data = os.urandom(2500)
                original.write_bytes(data)
                sent, joined = [], bytearray()

                class Client:
                    async def get_input_entity(self, chat_id):
                        return "entity"

                    async def __call__(self, request):
                        return None

                    async def send_file(self, entity, path, **kw):
                        p = Path(path)
                        sent.append((p.name, p.stat().st_size, sorted(x.name for x in folder.iterdir()), kw["caption"]))
                        joined.extend(p.read_bytes())
                        kw["progress_callback"](p.stat().st_size, p.stat().st_size)
                        return SimpleNamespace(id=100 + len(sent))

                app = SimpleNamespace(bot=Client(), user_client=None, user_premium=False, db=None)
                up = U.Uploader(app)
                progress = []
                with mock.patch.object(U, "BOT_LIMIT", 1000):
                    first, was_split = await up.send({"chat_id": 1, "via": "bot"}, original, "cap", "auto", None,
                                                     lambda sent_, total: progress.append((sent_, total)))
                self.assertEqual((first, was_split), (101, True))
                self.assertEqual([(n, s) for n, s, _, _ in sent],
                                 [("movie.bin.001", 1000), ("movie.bin.002", 1000), ("movie.bin.003", 500)])
                for name, _size, listing, _cap in sent:  # only the original + the part being sent exist
                    self.assertEqual(listing, sorted(["movie.bin", name]))
                self.assertIn("Part 1/3", sent[0][3])
                self.assertEqual(bytes(joined), data)         # the parts rebuild the original exactly
                self.assertEqual(progress[-1], (2500, 2500))
                self.assertEqual(list(folder.iterdir()), [])  # original and parts are gone afterwards
        run(go())

    def test_refuses_to_split_when_disk_is_nearly_full(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                original = Path(tmp) / "movie.bin"
                original.write_bytes(os.urandom(2500))

                class Client:
                    async def get_input_entity(self, chat_id):
                        return "entity"

                    async def __call__(self, request):
                        return None

                app = SimpleNamespace(bot=Client(), user_client=None, user_premium=False, db=None)
                with mock.patch.object(U, "BOT_LIMIT", 1000), \
                        mock.patch.object(U.shutil, "disk_usage", return_value=SimpleNamespace(free=10)):
                    with self.assertRaises(U.UploadError) as cm:
                        await U.Uploader(app).send({"chat_id": 1, "via": "bot"}, original, "c", "auto", None, lambda a, b: None)
                self.assertIn("disk space", str(cm.exception))
                self.assertEqual([p.name for p in Path(tmp).iterdir()], ["movie.bin"])  # nothing half-written is left
        run(go())


if __name__ == "__main__":
    unittest.main()
