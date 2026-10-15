"""Mega account pool. Passwords are stored encrypted (Fernet) and never shown back."""
from __future__ import annotations

import re
import time
from typing import List, Optional, Tuple

from mega_client import Creds, MegaError


class Vault:
    def __init__(self, key: bytes):
        from cryptography.fernet import Fernet

        self._f = Fernet(key)

    def encrypt(self, text: str) -> bytes:
        return self._f.encrypt(text.encode())

    def decrypt(self, blob: bytes) -> str:
        return self._f.decrypt(bytes(blob)).decode()


def parse_accounts(text: str) -> List[Tuple[str, str]]:
    """One account per line: `email password` or `email:password`."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1) if re.search(r"\s", line) else line.split(":", 1)
        if len(parts) == 2 and "@" in parts[0] and parts[1].strip():
            out.append((parts[0].strip(), parts[1].strip()))
    return out


def mask_email(email: str) -> str:
    name, _, domain = email.partition("@")
    return "%s***@%s" % (name[:2], domain)


def account_state(a: dict, now: Optional[float] = None) -> str:
    """ok | cooldown | bad"""
    now = now or time.time()
    if a["status"] == "bad":
        return "bad"
    return "cooldown" if a["cooldown_until"] > now else "ok"


class AccountManager:
    def __init__(self, db, vault: Vault, mega):
        self.db, self.vault, self.mega = db, vault, mega

    async def list(self) -> list:
        return await self.db.all("SELECT * FROM accounts ORDER BY id")

    async def get(self, aid: int) -> Optional[dict]:
        return await self.db.one("SELECT * FROM accounts WHERE id=?", (aid,))

    async def add(self, email: str, password: str) -> Tuple[bool, str]:
        if await self.db.one("SELECT id FROM accounts WHERE email=?", (email,)):
            return False, "already added"
        try:
            info = await self.mega.account_info(Creds(email=email, password=password))
        except MegaError as e:
            return False, "login failed (%s)" % (str(e)[:80] or e.kind)
        await self.db.execute("INSERT INTO accounts(email, pw_enc) VALUES(?,?)", (email, self.vault.encrypt(password)))
        return True, info or "ok"

    async def remove(self, aid: int) -> None:
        await self.db.execute("DELETE FROM accounts WHERE id=?", (aid,))

    async def reset(self, aid: int) -> None:
        await self.db.execute("UPDATE accounts SET status='ok', fails=0, cooldown_until=0, last_error=NULL WHERE id=?", (aid,))

    async def info(self, aid: int) -> str:
        a = await self.get(aid)
        if not a:
            return "account not found"
        proxy = None
        if a["proxy_id"]:
            row = await self.db.one("SELECT url FROM proxies WHERE id=?", (a["proxy_id"],))
            proxy = row["url"] if row else None
        try:
            return await self.mega.account_info(Creds(a["email"], self.vault.decrypt(a["pw_enc"]), proxy))
        except MegaError as e:
            return "error: %s" % (str(e)[:120] or e.kind)

    async def cycle_proxy(self, aid: int) -> None:
        """Binds the account to the next proxy in the pool (or none) so each account keeps its own IP."""
        a = await self.get(aid)
        ids = [r["id"] for r in await self.db.all("SELECT id FROM proxies ORDER BY id")]
        options = [None] + ids
        cur = a["proxy_id"] if a["proxy_id"] in ids else None
        nxt = options[(options.index(cur) + 1) % len(options)]
        await self.db.execute("UPDATE accounts SET proxy_id=? WHERE id=?", (nxt, aid))
