"""Persistent job queue: download one file, upload it, delete it, next. Live animated status card."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
from datetime import date
from pathlib import Path
from typing import Optional

from telethon import errors

from mega_client import UNKNOWN_NAME, MegaError, extract_links
from ui.animations import Ema, fmt_size
from uploader import Aborted, UploadError

log = logging.getLogger("queue")


class FileFailed(Exception):
    pass


class JobCtl:
    """Runtime control flags for one running job."""

    def __init__(self):
        self.cancel = asyncio.Event()
        self.abort = asyncio.Event()  # stops the current transfer (cancel or skip)
        self.skip = False
        self.paused = False
        self.resume = asyncio.Event()
        self.resume.set()

    def do_cancel(self):
        self.cancel.set()
        self.abort.set()
        self.resume.set()

    def do_skip(self):
        self.skip = True
        self.abort.set()

    def do_pause(self):
        self.paused = True
        self.resume.clear()

    def do_resume(self):
        self.paused = False
        self.resume.set()


def render_caption(template: str, f: dict, index: int, total: int, root: str) -> str:
    """Fills {name} {size} {folder} {path} {ext} {index} {total} {root}; unknown fields are left as-is."""
    values = {
        "name": f["name"], "size": fmt_size(f["size"]), "path": f["path"],
        "folder": f["path"].rsplit("/", 1)[0] if "/" in f["path"] else "",
        "ext": Path(f["name"]).suffix.lstrip("."), "index": index, "total": total, "root": root,
    }
    out = re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template or "{name}")
    return out[:1024]


class JobView:
    """Everything the live status card needs; refreshed by a ticker task."""

    def __init__(self, app, job: dict, user: dict):
        self.app = app
        self.job_id, self.chat_id, self.msg_id = job["id"], job["chat_id"], job["msg_id"]
        opts = json.loads(job["options"] or "{}")
        self.title = opts.get("root_name") or "Mega download"
        self.theme = user["theme"]
        self.animations = bool(user["animations"])
        self.phase = "queued"
        self.file_name = ""
        self.file_idx = 0
        self.files_total = job["total_files"]
        self.files_done = self.files_failed = self.files_skipped = 0
        self.bytes_total = job["total_bytes"]
        self.bytes_done_files = 0
        self.cur_done = self.cur_total = 0
        self.route = self.dest = self.note = ""
        self.wait_until = 0.0
        self.frame = 0
        self.speed = Ema()
        self.interval = app.cfg.edit_interval if self.animations else max(app.cfg.edit_interval * 2, 6.0)
        self._last_b, self._last_t, self._last_edit = 0, time.monotonic(), 0.0

    def tick(self) -> None:
        now = time.monotonic()
        dt = now - self._last_t
        if dt > 0:
            delta = self.cur_done - self._last_b
            if delta >= 0 and self.phase in ("download", "upload"):
                self.speed.update(delta / dt)
        self._last_b, self._last_t = self.cur_done, now
        self.frame += 1

    async def refresh(self, force: bool = False, final: bool = False) -> None:
        now = time.monotonic()
        if not final and not force and now - self._last_edit < self.interval:
            return
        if not final and now - self._last_edit < 1.2:
            return
        from ui import screens  # local import avoids a cycle at start-up

        text, buttons = await screens.job_card(self.app, self)
        try:
            await self.app.bot.edit_message(self.chat_id, self.msg_id, text, buttons=buttons,
                                            parse_mode="html", link_preview=False)
        except errors.MessageNotModifiedError:
            pass
        except errors.FloodWaitError as e:
            self.interval = min(30.0, max(self.interval * 2, float(e.seconds) + 1))
        except Exception as e:
            log.debug("card edit failed: %s", e)
        self._last_edit = now


class QueueManager:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.cfg = app.cfg
        self.ctl: dict = {}
        self.views: dict = {}
        self.running: set = set()
        self.wake = asyncio.Event()
        self.claim_lock = asyncio.Lock()
        self.disk_slots = asyncio.Semaphore(self.cfg.max_files_on_disk)  # files on disk at once, across all jobs
        self.tasks: list = []

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        await self.db.execute("UPDATE jobs SET status='queued' WHERE status IN ('running','paused')")
        await self.db.execute("UPDATE job_files SET status='pending' WHERE status IN ('downloading','uploading')")
        self.tasks = [asyncio.create_task(self._worker(i)) for i in range(self.cfg.max_concurrent)]
        self.wake.set()

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()

    async def enqueue(self, job_id: int) -> None:
        self.wake.set()

    # -- controls ----------------------------------------------------------
    def get_ctl(self, job_id: int) -> JobCtl:
        return self.ctl.setdefault(job_id, JobCtl())

    async def cancel_job(self, job_id: int) -> None:
        if job_id in self.running:
            self.get_ctl(job_id).do_cancel()
        else:
            await self.db.execute("UPDATE jobs SET status='cancelled', finished=? WHERE id=? AND status IN ('queued','paused')",
                                  (time.time(), job_id))

    async def retry_failed(self, job_id: int) -> None:
        await self.db.execute("UPDATE job_files SET status='pending', error=NULL WHERE job_id=? AND status IN ('failed','skipped')", (job_id,))
        await self.db.execute("UPDATE jobs SET status='queued', finished=NULL WHERE id=?", (job_id,))
        self.ctl.pop(job_id, None)
        self.wake.set()

    async def queue_position(self, job_id: int) -> int:
        row = await self.db.one("SELECT COUNT(*) AS c FROM jobs WHERE status='queued' AND id<?", (job_id,))
        return (row["c"] if row else 0) + 1

    # -- worker loop -------------------------------------------------------
    async def _claim(self) -> Optional[int]:
        async with self.claim_lock:
            row = await self.db.one("SELECT id FROM jobs WHERE status='queued' ORDER BY priority DESC, id LIMIT 1")
            if not row:
                return None
            await self.db.execute("UPDATE jobs SET status='running' WHERE id=?", (row["id"],))
            self.running.add(row["id"])
            return row["id"]

    async def _worker(self, n: int) -> None:
        while True:
            job_id = await self._claim()
            if job_id is None:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), 5)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._run_job(job_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("job %s crashed", job_id)
                await self.db.execute("UPDATE jobs SET status='failed', finished=? WHERE id=?", (time.time(), job_id))
            finally:
                self.running.discard(job_id)
                self.views.pop(job_id, None)

    async def _ticker(self, view: JobView) -> None:
        while True:
            view.tick()
            await view.refresh(force=True)
            await asyncio.sleep(view.interval)

    # -- one job -----------------------------------------------------------
    async def _run_job(self, job_id: int) -> None:
        job = await self.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
        user = await self.db.user(job["user_id"])
        ctl = self.get_ctl(job_id)
        view = JobView(self.app, job, user)
        self.views[job_id] = view
        dest = await self.app.dest_for_job(job)
        view.dest = dest["name"]
        counts = await self.db.one(
            "SELECT SUM(status='done') d, SUM(status='failed') f, COALESCE(SUM(CASE WHEN status='done' THEN size END),0) b "
            "FROM job_files WHERE job_id=?", (job_id,))
        view.files_done, view.files_failed = counts["d"] or 0, 0
        view.bytes_done_files = counts["b"] or 0
        ticker = asyncio.create_task(self._ticker(view))
        try:
            files = await self.db.all("SELECT * FROM job_files WHERE job_id=? AND status='pending' ORDER BY seq", (job_id,))
            for f in files:
                if ctl.cancel.is_set():
                    break
                if ctl.paused:
                    view.phase = "paused"
                    await self.db.execute("UPDATE jobs SET status='paused' WHERE id=?", (job_id,))
                    await view.refresh(force=True)
                    await ctl.resume.wait()
                    await self.db.execute("UPDATE jobs SET status='running' WHERE id=? AND status='paused'", (job_id,))
                if ctl.cancel.is_set():
                    break
                await self._do_file(job, f, dest, user, view, ctl)
                await asyncio.sleep(self.cfg.upload_delay)
        finally:
            ticker.cancel()
        await self._finish(job, user, view, ctl)

    async def _do_file(self, job: dict, f: dict, dest: dict, user: dict, view: JobView, ctl: JobCtl) -> None:
        """One file at a time: download → upload → delete. With the default MAX_FILES_ON_DISK=1 a second job waits here."""
        if self.disk_slots.locked():
            view.note = "⏳ Waiting for another file to finish (keeps disk use low)"
            await view.refresh(force=True)
        async with self.disk_slots:
            if ctl.cancel.is_set():
                return
            await self._do_file_inner(job, f, dest, user, view, ctl)

    async def _do_file_inner(self, job: dict, f: dict, dest: dict, user: dict, view: JobView, ctl: JobCtl) -> None:
        db, app = self.db, self.app
        opts = json.loads(job["options"] or "{}")
        key = "%s:%s" % (job["root_id"], f["handle"])
        ctl.skip = False
        ctl.abort.clear()
        view.file_idx += 1
        view.file_name, view.cur_done, view.cur_total = f["name"], 0, f["size"]
        view.note, view.phase = "", "download"
        tmp = self.cfg.download_dir / ("j%d_f%d" % (job["id"], f["id"]))
        caption = render_caption(opts.get("caption") or user["caption"], f, f["seq"] + 1, view.files_total, view.title)
        topic_id = dest.get("topic_id") or None
        try:
            if opts.get("topic_per_folder") and dest.get("is_forum") and "/" in f["path"]:
                topic_id = await app.uploader.get_or_create_topic(dest, f["path"].split("/")[0]) or topic_id

            await db.execute("UPDATE job_files SET status='downloading' WHERE id=?", (f["id"],))
            sent_id = await app.uploader.try_reuse(key, dest, caption, topic_id)  # already uploaded before? just re-send
            size = f["size"]
            was_split = False
            if sent_id is None:
                need = f["size"] + int(self.cfg.min_free_gb * 1024 ** 3)
                self.cfg.download_dir.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(self.cfg.download_dir).free < need:
                    raise FileFailed("not enough free disk space")
                path = await self._download(job, f, tmp, view, ctl)
                size = path.stat().st_size
                if f["name"] == UNKNOWN_NAME:  # single-file link: now we know the real name
                    f["name"] = f["path"] = path.name
                    view.file_name = path.name
                    caption = render_caption(opts.get("caption") or user["caption"], f, f["seq"] + 1, view.files_total, view.title)
                    await db.execute("UPDATE job_files SET name=?, path=? WHERE id=?", (path.name, path.name, f["id"]))
                view.phase, view.cur_done, view.cur_total, view.route = "upload", 0, size, "→ " + dest["name"]
                await view.refresh(force=True)
                await db.execute("UPDATE job_files SET status='uploading', size=? WHERE id=?", (size, f["id"]))

                def progress(sent, total):
                    if ctl.abort.is_set():
                        raise Aborted()
                    view.cur_done, view.cur_total = sent, total

                sent_id, was_split = await app.uploader.send(dest, path, caption, opts.get("send_as", "auto"), topic_id, progress)
            if not was_split:
                await db.execute("INSERT OR IGNORE INTO uploaded(key,tg_chat,tg_msg,created) VALUES(?,?,?,?)",
                                 (key, dest["chat_id"], sent_id, time.time()))
            await db.execute("UPDATE job_files SET status='done', size=? WHERE id=?", (size, f["id"]))
            await db.execute(
                "INSERT INTO usage(user_id,day,bytes) VALUES(?,?,?) ON CONFLICT(user_id,day) DO UPDATE SET bytes=bytes+excluded.bytes",
                (job["user_id"], date.today().isoformat(), size))
            view.files_done += 1
            view.bytes_done_files += size
        except Aborted:
            status = "cancelled" if ctl.cancel.is_set() else "skipped"
            await db.execute("UPDATE job_files SET status=? WHERE id=?", (status, f["id"]))
            if status == "skipped":
                view.files_skipped += 1
        except (FileFailed, UploadError) as e:
            await self._fail(f, view, str(e))
            if isinstance(e, UploadError) and e.fatal:
                ctl.do_cancel()  # the destination itself is broken: stop the whole job
                view.note = "⛔ " + str(e)
        except Exception as e:
            log.exception("file %s failed", f["id"])
            await self._fail(f, view, "%s: %s" % (type(e).__name__, e))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            view.cur_done = view.cur_total = 0

    async def _fail(self, f: dict, view: JobView, message: str) -> None:
        view.files_failed += 1
        await self.db.execute("UPDATE job_files SET status='failed', error=? WHERE id=?", (message[:300], f["id"]))

    # -- download with route rotation ---------------------------------------
    async def _download(self, job: dict, f: dict, tmp: Path, view: JobView, ctl: JobCtl) -> Path:
        app = self.app
        links = extract_links(job["link"])
        if not links:
            raise FileFailed("stored link is invalid")
        link = links[0]
        anon_only = link.kind == "folder"  # Mega accounts cannot be used for files inside folder links
        tried, attempts, last = set(), 0, None
        while True:
            if ctl.abort.is_set():
                raise Aborted()
            r = await app.router.pick(exclude=tried, anon_only=anon_only)
            if r is None:
                eta = await app.router.next_ready_eta(anon_only)
                if eta is None:
                    raise FileFailed("no usable route%s" % (" (last error: %s)" % last if last else ""))
                await self._wait_for_route(view, ctl, eta, anon_only)
                tried.clear()
                continue
            if attempts >= self.cfg.max_attempts:
                raise FileFailed("gave up after %d attempts (%s)" % (attempts, last))
            attempts += 1
            view.phase, view.route, view.cur_done = "download", r.label, 0
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                def on_progress(n):
                    view.cur_done = n

                path = await app.mega.fetch(link, f["handle"], f.get("nkey"), tmp, r.creds, name_hint=f["name"],
                                            on_progress=on_progress, abort=ctl.abort, stall_timeout=self.cfg.stall_timeout)
                await app.router.report_ok(r, path.stat().st_size)
                return path
            except MegaError as e:
                if e.kind == "cancelled":
                    raise Aborted()
                last = "%s: %s" % (e.kind, str(e)[:100])
                if e.kind in ("dead", "key", "unsupported"):
                    raise FileFailed("link problem (%s)" % str(e)[:150])
                await app.router.report_fail(r, e.kind, str(e))
                tried.add(r.key)
                view.note = "↻ %s — trying another route" % e.kind

    async def _wait_for_route(self, view: JobView, ctl: JobCtl, eta: float, anon_only: bool = False) -> None:
        view.phase, view.wait_until = "wait", time.time() + eta
        until = view.wait_until
        while time.time() < until and not ctl.abort.is_set():
            if await self.app.router.candidates(anon_only):
                break
            await asyncio.sleep(3)
        view.phase, view.wait_until = "download", 0.0

    # -- wrap-up -----------------------------------------------------------
    async def _finish(self, job: dict, user: dict, view: JobView, ctl: JobCtl) -> None:
        row = await self.db.one(
            "SELECT COALESCE(SUM(status='done'),0) d, COALESCE(SUM(status='failed'),0) f FROM job_files WHERE job_id=?", (job["id"],))
        if ctl.cancel.is_set():
            status = "cancelled"
        elif row["f"] > 0:
            status = "partial" if row["d"] > 0 else "failed"
        else:
            status = "done"
        await self.db.execute("UPDATE jobs SET status=?, finished=? WHERE id=?", (status, time.time(), job["id"]))
        view.phase = status
        view.files_done, view.files_failed = row["d"], row["f"]
        await view.refresh(final=True)
        if user["notify"] and status != "cancelled":
            icon = {"done": "✅", "partial": "⚠️", "failed": "❌"}[status]
            text = "%s <b>Job #%d finished</b> — %d done, %d failed" % (icon, job["id"], row["d"], row["f"])
            try:
                await self.app.bot.send_message(job["chat_id"], text, parse_mode="html")
                if status == "done" and user["animations"]:
                    await self.app.bot.send_message(job["chat_id"], "🎉")  # a lone emoji plays Telegram's animation
            except Exception as e:
                log.debug("notify failed: %s", e)
        self.ctl.pop(job["id"], None)
