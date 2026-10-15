"""Tiny asyncio-friendly SQLite wrapper (standard library only)."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, role TEXT NOT NULL DEFAULT 'user', name TEXT,
  theme TEXT NOT NULL DEFAULT 'ocean', animations INTEGER NOT NULL DEFAULT 1,
  notify INTEGER NOT NULL DEFAULT 1, default_dest INTEGER,
  send_as TEXT NOT NULL DEFAULT 'auto',
  caption TEXT NOT NULL DEFAULT ('{name}' || char(10) || '💾 {size}'),
  daily_gb REAL NOT NULL DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS destinations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, name TEXT NOT NULL,
  chat_id INTEGER NOT NULL, topic_id INTEGER, via TEXT NOT NULL DEFAULT 'bot',
  topic_per_folder INTEGER NOT NULL DEFAULT 0, is_forum INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS proxies(
  id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL DEFAULT 'ok', fails INTEGER NOT NULL DEFAULT 0,
  cooldown_until REAL NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0,
  bytes_total INTEGER NOT NULL DEFAULT 0, latency REAL, last_error TEXT,
  exit_ip TEXT, checked_at REAL, source_id INTEGER);
CREATE TABLE IF NOT EXISTS proxy_sources(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, url TEXT NOT NULL, headers_enc BLOB,
  scheme TEXT NOT NULL DEFAULT 'auto', interval_min INTEGER NOT NULL DEFAULT 0,
  sync INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
  last_fetch REAL NOT NULL DEFAULT 0, last_count INTEGER NOT NULL DEFAULT 0, last_error TEXT);
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE, pw_enc BLOB NOT NULL,
  status TEXT NOT NULL DEFAULT 'ok', fails INTEGER NOT NULL DEFAULT 0,
  cooldown_until REAL NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0,
  bytes_total INTEGER NOT NULL DEFAULT 0, proxy_id INTEGER, last_error TEXT);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
  msg_id INTEGER, link TEXT NOT NULL, root_id TEXT, dest_id INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'queued', priority INTEGER NOT NULL DEFAULT 0,
  options TEXT NOT NULL DEFAULT '{}', total_files INTEGER NOT NULL DEFAULT 0,
  total_bytes INTEGER NOT NULL DEFAULT 0, created REAL, finished REAL);
CREATE TABLE IF NOT EXISTS job_files(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER NOT NULL, handle TEXT NOT NULL,
  path TEXT NOT NULL, name TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'pending', error TEXT, seq INTEGER NOT NULL DEFAULT 0, nkey TEXT);
CREATE INDEX IF NOT EXISTS idx_job_files_job ON job_files(job_id, status);
CREATE TABLE IF NOT EXISTS uploaded(
  key TEXT NOT NULL, tg_chat INTEGER NOT NULL, tg_msg INTEGER NOT NULL, created REAL,
  PRIMARY KEY(key, tg_chat, tg_msg));
CREATE TABLE IF NOT EXISTS topics(
  chat_id INTEGER NOT NULL, title TEXT NOT NULL, topic_id INTEGER NOT NULL,
  PRIMARY KEY(chat_id, title));
CREATE TABLE IF NOT EXISTS usage(
  user_id INTEGER NOT NULL, day TEXT NOT NULL, bytes INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(user_id, day));
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(SCHEMA)
            self._ensure_columns("proxies", {"exit_ip": "TEXT", "checked_at": "REAL", "source_id": "INTEGER"})
            self._ensure_columns("job_files", {"nkey": "TEXT"})

    def _ensure_columns(self, table: str, cols: dict) -> None:
        """Tiny migration helper: adds columns that older databases do not have yet."""
        have = {r["name"] for r in self._conn.execute("PRAGMA table_info(%s)" % table)}
        for name, decl in cols.items():
            if name not in have:
                self._conn.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))

    # -- sync helpers (run in a worker thread) --
    def _execute(self, sql: str, params: tuple) -> int:
        with self._lock:
            return self._conn.execute(sql, params).lastrowid

    def _all(self, sql: str, params: tuple) -> list:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _many(self, sql: str, rows: list) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # -- async API --
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        return await asyncio.to_thread(self._execute, sql, tuple(params))

    async def all(self, sql: str, params: Iterable[Any] = ()) -> list:
        return await asyncio.to_thread(self._all, sql, tuple(params))

    async def one(self, sql: str, params: Iterable[Any] = ()) -> Optional[dict]:
        rows = await self.all(sql, params)
        return rows[0] if rows else None

    async def executemany(self, sql: str, rows: Iterable[Iterable[Any]]) -> None:
        await asyncio.to_thread(self._many, sql, [tuple(r) for r in rows])

    # -- settings --
    async def get_setting(self, key: str, default: str = "") -> str:
        row = await self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row and row["value"] is not None else default

    async def set_setting(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # -- users --
    async def user(self, uid: int) -> Optional[dict]:
        return await self.one("SELECT * FROM users WHERE id=?", (uid,))

    async def ensure_user(self, uid: int, role: str = "user", name: Optional[str] = None) -> dict:
        await self.execute(
            "INSERT OR IGNORE INTO users(id, role, name, created) VALUES(?,?,?,?)",
            (uid, role, name, time.time()),
        )
        return await self.user(uid)

    async def update_user(self, uid: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join("%s=?" % k for k in fields)
        await self.execute("UPDATE users SET %s WHERE id=?" % cols, (*fields.values(), uid))
