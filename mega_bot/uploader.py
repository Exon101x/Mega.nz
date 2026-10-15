"""Uploads files to Telegram (chats, channels, groups, forum topics), with splitting and thumbnails."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from telethon import errors
from telethon.tl import functions, types

from browse import AUDIO, IMAGE, VIDEO

log = logging.getLogger("uploader")

MiB = 1024 * 1024
BOT_LIMIT = 1950 * MiB       # Telegram allows 2000 MiB over MTProto for bots / normal accounts
PREMIUM_LIMIT = 3900 * MiB   # 4000 MiB for Premium user accounts


class Aborted(Exception):
    """Raised when the user cancels or skips the current transfer."""


class UploadError(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def write_part(path: Path, index: int, part_size: int) -> Path:
    """Writes part number `index` (1-based) of a file next to it (name.ext.001, .002 …; join with `cat` / 7-Zip)."""
    part = path.with_name("%s.%03d" % (path.name, index))
    written, left = 0, part_size
    with open(path, "rb") as src, open(part, "wb") as dst:
        src.seek((index - 1) * part_size)
        while left > 0:
            buf = src.read(min(8 * MiB, left))
            if not buf:
                break
            dst.write(buf)
            written += len(buf)
            left -= len(buf)
    return part


async def probe(path: Path) -> dict:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=width,height:format=duration", "-of", "json", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 60)
        data = json.loads(out or b"{}")
        st = (data.get("streams") or [{}])[0]
        return {"width": int(st.get("width") or 0), "height": int(st.get("height") or 0),
                "duration": float((data.get("format") or {}).get("duration") or 0)}
    except Exception:
        return {}


async def make_thumb(path: Path, duration: float) -> Optional[Path]:
    out = path.with_name(path.name + ".thumb.jpg")
    at = max(1.0, min(duration * 0.1, 30.0)) if duration else 3.0
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-loglevel", "error", "-ss", "%.1f" % at, "-i", str(path),
            "-frames:v", "1", "-vf", "scale=320:-2", str(out),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), 60)
    except Exception:
        return None
    return out if out.is_file() and out.stat().st_size > 0 else None


class Uploader:
    def __init__(self, app):
        self.app = app

    # -- helpers -----------------------------------------------------------
    def _client(self, dest: dict):
        if dest.get("via") == "user" and self.app.user_client is not None:
            return self.app.user_client
        return self.app.bot

    def split_limit(self, dest: dict) -> int:
        if dest.get("via") == "user" and self.app.user_client is not None and self.app.user_premium:
            return PREMIUM_LIMIT
        return BOT_LIMIT

    async def _entity(self, dest: dict):
        return await self._client(dest).get_input_entity(dest["chat_id"])

    async def _action_loop(self, client, entity, topic_id, video: bool) -> None:
        action = types.SendMessageUploadVideoAction(progress=0) if video else types.SendMessageUploadDocumentAction(progress=0)
        try:
            while True:
                await client(functions.messages.SetTypingRequest(peer=entity, top_msg_id=topic_id, action=action))
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            raise
        except Exception:
            return

    # -- topics ------------------------------------------------------------
    async def list_topics(self, dest_or_client, entity) -> List[Tuple[int, str]]:
        res = await dest_or_client(functions.messages.GetForumTopicsRequest(
            peer=entity, offset_date=None, offset_id=0, offset_topic=0, limit=50))
        return [(t.id, t.title) for t in res.topics if hasattr(t, "title")]

    async def get_or_create_topic(self, dest: dict, title: str) -> Optional[int]:
        title = title[:120]
        row = await self.app.db.one("SELECT topic_id FROM topics WHERE chat_id=? AND title=?", (dest["chat_id"], title))
        if row:
            return row["topic_id"]
        try:
            client = self._client(dest)
            res = await client(functions.messages.CreateForumTopicRequest(peer=await self._entity(dest), title=title))
            tid = None
            for u in getattr(res, "updates", []):
                msg = getattr(u, "message", None)
                if msg is not None and getattr(msg, "id", None):
                    tid = msg.id
                    break
            if tid:
                await self.app.db.execute(
                    "INSERT OR REPLACE INTO topics(chat_id,title,topic_id) VALUES(?,?,?)", (dest["chat_id"], title, tid))
            return tid
        except Exception as e:
            log.warning("could not create topic %r: %s", title, e)
            return None

    # -- re-use of earlier uploads -----------------------------------------
    async def try_reuse(self, key: str, dest: dict, caption: str, topic_id: Optional[int]) -> Optional[int]:
        rows = await self.app.db.all("SELECT tg_chat, tg_msg FROM uploaded WHERE key=? ORDER BY created DESC LIMIT 3", (key,))
        if not rows:
            return None
        client = self._client(dest)
        for r in rows:
            try:
                src = await client.get_messages(r["tg_chat"], ids=r["tg_msg"])
                if src and src.media:
                    sent = await client.send_file(await self._entity(dest), src.media, caption=caption[:1024], reply_to=topic_id)
                    return sent.id
            except Exception as e:
                log.debug("reuse failed: %s", e)
        return None

    # -- sending -----------------------------------------------------------
    async def send(self, dest: dict, path: Path, caption: str, send_as: str, topic_id: Optional[int],
                   progress: Callable[[int, int], None]) -> Tuple[int, bool]:
        """Uploads one file (splitting if needed). Returns (first message id, was_split)."""
        client = self._client(dest)
        try:
            entity = await self._entity(dest)
        except Exception as e:
            raise UploadError("cannot resolve the destination (%s). Re-add it with a forwarded message." % type(e).__name__, True)
        total = path.stat().st_size
        limit = self.split_limit(dest)
        video = path.suffix.lower() in VIDEO and send_as != "document"
        action = asyncio.create_task(self._action_loop(client, entity, topic_id, video))
        try:
            if total > limit:
                # cut and send ONE part at a time, so the disk only ever holds the original + a single part
                n_parts = -(-total // limit)
                if shutil.disk_usage(path.parent).free < limit + 64 * MiB:
                    raise UploadError("not enough free disk space to split this file into parts")
                first, base = None, 0
                for i in range(1, n_parts + 1):
                    part = await asyncio.to_thread(write_part, path, i, limit)
                    size = part.stat().st_size

                    def cb(sent, _t, base=base):
                        progress(base + sent, total)

                    try:
                        msg = await self._send_one(client, entity, part, "%s\n📦 Part %d/%d" % (caption, i, n_parts),
                                                   "document", topic_id, cb)
                    finally:
                        part.unlink(missing_ok=True)
                    first = first or msg.id
                    base += size
                path.unlink(missing_ok=True)
                return first, True
            msg = await self._send_one(client, entity, path, caption, send_as, topic_id, progress)
            return msg.id, False
        finally:
            action.cancel()

    async def _send_one(self, client, entity, path: Path, caption: str, send_as: str, topic_id, progress):
        kw = {"caption": caption[:1024], "reply_to": topic_id, "progress_callback": progress}
        ext, size = path.suffix.lower(), path.stat().st_size
        if send_as == "document":
            kw["force_document"] = True
        elif ext in VIDEO:
            info = await probe(path)
            kw.update(force_document=False, supports_streaming=True)
            if info.get("width") and info.get("height"):
                kw["attributes"] = [types.DocumentAttributeVideo(
                    duration=int(info.get("duration") or 0), w=info["width"], h=info["height"], supports_streaming=True)]
            thumb = await make_thumb(path, info.get("duration") or 0)
            if thumb:
                kw["thumb"] = str(thumb)
        elif ext in IMAGE and size <= 10 * MiB:
            kw["force_document"] = False
        else:
            kw["force_document"] = ext not in AUDIO
        last = None
        for attempt in range(3):
            try:
                return await client.send_file(entity, str(path), **kw)
            except (errors.ChatWriteForbiddenError, errors.ChatAdminRequiredError, errors.UserBannedInChannelError,
                    errors.ChannelPrivateError, errors.PeerIdInvalidError) as e:
                raise UploadError("cannot post to the destination (%s). Check the bot's admin rights." % type(e).__name__, True)
            except (ConnectionError, TimeoutError, asyncio.TimeoutError, OSError) as e:
                last = e
                await asyncio.sleep(3 * (attempt + 1))
        raise UploadError("upload failed after retries: %s" % last)
