"""Application container: wires the database, Mega tools, routing, uploader, queue and Telegram clients."""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from typing import Awaitable, Callable, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl import functions, types

import config
import handlers
from accounts import AccountManager, Vault
from db import DB
from mega_client import MegaError, MegaTools
from proxies import ProxyManager
from queue_worker import QueueManager
from router import Router
from uploader import Uploader

log = logging.getLogger("app")


class App:
    def __init__(self, cfg: config.Config):
        self.cfg = cfg
        self.started = time.time()
        self.sessions: dict = {}      # browse sessions by id
        self.awaiting: dict = {}      # pending "send me X" prompts by user id
        self.pending_dest: dict = {}  # destination awaiting topic choice, by user id
        self.check_results: dict = {}  # last proxy-checker run per user
        self.bot: Optional[TelegramClient] = None
        self.user_client: Optional[TelegramClient] = None
        self.user_premium = False

    async def start(self) -> None:
        cfg = self.cfg
        self.db = DB(cfg.db_path)
        self.vault = Vault(config.secret_key(cfg))
        self.mega = MegaTools(cfg.tmp_dir)
        log.info("megatools command: %s", self.mega.resolve_binaries(cfg.megatools_bin))
        version = await self.mega.detect_version()
        log.info("megatools: %s (optional, only used for Mega accounts)", version or "not installed")
        log.info("disk policy: at most %d file(s) on disk at once, keep >= %.1f GB free", cfg.max_files_on_disk, cfg.min_free_gb)
        self.router = Router(self.db, self.vault, cfg)
        self.proxy_pool = ProxyManager(self.db, self.vault)
        self.accounts = AccountManager(self.db, self.vault, self.mega)
        self.uploader = Uploader(self)
        self.queue = QueueManager(self)

        shutil.rmtree(cfg.download_dir, ignore_errors=True)  # leftovers from a crash; interrupted files restart
        cfg.download_dir.mkdir(parents=True, exist_ok=True)
        for f in cfg.tmp_dir.glob("megarc_*"):
            f.unlink(missing_ok=True)

        self.bot = TelegramClient(str(cfg.data_dir / "bot"), cfg.api_id, cfg.api_hash, flood_sleep_threshold=300)
        await self.bot.start(bot_token=cfg.bot_token)
        if cfg.user_session:
            await self._start_user_client()
        handlers.register(self)
        await self._set_commands()
        await self.queue.start()
        self.proxy_task = asyncio.create_task(self.proxy_pool.scheduler())  # API refresh + periodic health checks

    async def _start_user_client(self) -> None:
        cfg = self.cfg
        client = TelegramClient(StringSession(cfg.user_session), cfg.api_id, cfg.api_hash, flood_sleep_threshold=300)
        await client.connect()
        if not await client.is_user_authorized():
            log.warning("USER_SESSION is not authorised — ignoring it")
            await client.disconnect()
            return
        me = await client.get_me()
        self.user_premium = bool(getattr(me, "premium", False))
        try:
            await client.get_dialogs(limit=200)  # fills the entity cache so chat ids resolve
        except Exception as e:
            log.debug("get_dialogs failed: %s", e)
        self.user_client = client
        log.info("User account connected (premium=%s)", self.user_premium)

    async def _set_commands(self) -> None:
        cmds = [("start", "Open the menu"), ("menu", "Main menu"), ("queue", "Show jobs"), ("stats", "Statistics"),
                ("theme", "Change colours"), ("help", "Help"), ("cancel", "Cancel pending input")]
        try:
            await self.bot(functions.bots.SetBotCommandsRequest(
                scope=types.BotCommandScopeDefault(), lang_code="",
                commands=[types.BotCommand(command=c, description=d) for c, d in cmds]))
        except Exception as e:
            log.debug("could not set commands: %s", e)

    # -- helpers used by handlers / queue ------------------------------------
    async def with_route(self, fn: Callable[..., Awaitable], attempts: int = 4, anon_only: bool = True):
        """Runs a Mega call through the router, rotating routes on quota/network errors."""
        tried, last = set(), None
        for _ in range(attempts):
            r = await self.router.pick(exclude=tried, anon_only=anon_only)
            if r is None:
                break
            try:
                res = await fn(r.creds)
                await self.router.report_ok(r, 0)
                return res
            except MegaError as e:
                last = e
                if e.kind in ("dead", "key", "unsupported"):
                    raise
                await self.router.report_fail(r, e.kind, str(e))
                tried.add(r.key)
        raise last or MegaError("quota", "no route available")

    async def get_dest(self, user: dict, dest_id: Optional[int] = None) -> dict:
        """dest_id None → the user's default; 0 or unknown → the user's own chat."""
        if dest_id is None:
            dest_id = user.get("default_dest")
        if dest_id:
            d = await self.db.one("SELECT * FROM destinations WHERE id=? AND user_id=?", (dest_id, user["id"]))
            if d:
                return d
        return {"id": 0, "user_id": user["id"], "name": "💬 This chat", "chat_id": user["id"],
                "topic_id": None, "via": "bot", "topic_per_folder": 0, "is_forum": 0}

    async def dest_for_job(self, job: dict) -> dict:
        return await self.get_dest({"id": job["user_id"], "default_dest": None}, job["dest_id"])
