"""Chooses which (account, proxy) pair each Mega request goes through, and handles cooldowns."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Tuple

from accounts import mask_email
from mega_client import Creds
from proxies import mask_proxy

PROXY_MODES = [("direct_first", "Direct first"), ("rr", "Round-robin"), ("sticky", "Sticky"), ("direct", "Direct only")]
ACCOUNT_MODES = [("anon_first", "Anonymous first"), ("anon", "Anonymous only"), ("accounts", "Accounts only")]


@dataclass
class Resolved:
    key: Tuple[Optional[int], Optional[int]]  # (account_id, proxy_id)
    creds: Creds
    label: str
    account_id: Optional[int]
    proxy_id: Optional[int]


class Router:
    def __init__(self, db, vault, cfg):
        self.db, self.vault, self.cfg = db, vault, cfg

    # -- candidate selection -------------------------------------------------
    async def candidates(self, anon_only: bool = False) -> List[Tuple[Optional[int], Optional[int]]]:
        """anon_only: skip account routes (accounts cannot be used for files inside folder links)."""
        now = time.time()
        pmode = await self.db.get_setting("proxy_mode", "direct_first")
        amode = await self.db.get_setting("account_mode", "anon_first")
        sticky = int(await self.db.get_setting("sticky_proxy", "0") or 0)
        direct_ok = float(await self.db.get_setting("direct_cd", "0") or 0) <= now
        proxies = await self.db.all("SELECT * FROM proxies")
        healthy = sorted((p for p in proxies if p["status"] != "dead" and p["cooldown_until"] <= now),
                         key=lambda p: p["last_used"])
        ids = [p["id"] for p in healthy]
        direct = [None] if direct_ok else []

        if pmode == "direct" or not proxies:
            base = direct
        elif pmode == "direct_first":
            base = direct + ids
        elif pmode == "sticky":
            base = ([sticky] if sticky in ids else []) + [i for i in ids if i != sticky]
        else:  # rr
            base = ids

        accounts = await self.db.all("SELECT * FROM accounts WHERE status!='bad' ORDER BY last_used")
        usable = [a for a in accounts if a["cooldown_until"] <= now]
        acc_routes = []
        for a in usable:
            if a["proxy_id"] and a["proxy_id"] in ids:
                acc_routes.append((a["id"], a["proxy_id"]))
            elif base:
                acc_routes.append((a["id"], base[0]))
            elif pmode == "direct" or not proxies:
                # the direct-IP cooldown tracks *anonymous* quota; a logged-in account has its own allowance
                acc_routes.append((a["id"], None))
        anon_routes = [(None, pid) for pid in base]

        if anon_only:
            return anon_routes
        if amode == "accounts" and accounts:
            return acc_routes
        if amode == "anon":
            return anon_routes
        return anon_routes + acc_routes

    async def pick(self, exclude: Iterable = (), anon_only: bool = False) -> Optional[Resolved]:
        excluded = set(exclude)
        for key in await self.candidates(anon_only):
            if key not in excluded:
                return await self._resolve(*key)
        return None

    async def _resolve(self, account_id: Optional[int], proxy_id: Optional[int]) -> Resolved:
        now = time.time()
        creds, parts = Creds(), []
        if account_id:
            a = await self.db.one("SELECT * FROM accounts WHERE id=?", (account_id,))
            creds.email, creds.password = a["email"], self.vault.decrypt(a["pw_enc"])
            parts.append("👤 " + mask_email(a["email"]))
            await self.db.execute("UPDATE accounts SET last_used=? WHERE id=?", (now, account_id))
        else:
            parts.append("👤 anonymous")
        if proxy_id:
            p = await self.db.one("SELECT * FROM proxies WHERE id=?", (proxy_id,))
            creds.proxy = p["url"]
            parts.append("🌐 #%d %s" % (proxy_id, mask_proxy(p["url"]).split("://")[-1].split("@")[-1]))
            await self.db.execute("UPDATE proxies SET last_used=? WHERE id=?", (now, proxy_id))
        else:
            parts.append("🌐 direct")
        return Resolved((account_id, proxy_id), creds, " · ".join(parts), account_id, proxy_id)

    # -- feedback ------------------------------------------------------------
    async def report_ok(self, r: Resolved, nbytes: int = 0) -> None:
        if r.account_id:
            await self.db.execute(
                "UPDATE accounts SET fails=0, last_error=NULL, bytes_total=bytes_total+? WHERE id=?", (nbytes, r.account_id))
        if r.proxy_id:
            await self.db.execute(
                "UPDATE proxies SET fails=0, last_error=NULL, bytes_total=bytes_total+? WHERE id=?", (nbytes, r.proxy_id))
            if await self.db.get_setting("proxy_mode") == "sticky":
                await self.db.set_setting("sticky_proxy", r.proxy_id)

    async def report_fail(self, r: Resolved, kind: str, message: str = "") -> None:
        now = time.time()
        quota_cd = now + self.cfg.quota_cooldown_min * 60
        error_cd = now + self.cfg.error_cooldown_min * 60
        message = (message or kind)[:200]
        if kind == "quota":
            if r.account_id:
                await self.db.execute("UPDATE accounts SET cooldown_until=?, last_error=? WHERE id=?", (quota_cd, message, r.account_id))
            elif r.proxy_id:
                await self.db.execute("UPDATE proxies SET cooldown_until=?, last_error=? WHERE id=?", (quota_cd, message, r.proxy_id))
            else:
                await self.db.set_setting("direct_cd", quota_cd)
        elif kind == "auth" and r.account_id:
            await self.db.execute("UPDATE accounts SET status='bad', last_error=? WHERE id=?", (message, r.account_id))
        elif r.proxy_id:
            await self.db.execute(
                "UPDATE proxies SET fails=fails+1, cooldown_until=?, last_error=?, "
                "status=CASE WHEN fails+1>=5 THEN 'dead' ELSE status END WHERE id=?", (error_cd, message, r.proxy_id))

    async def next_ready_eta(self, anon_only: bool = False) -> Optional[float]:
        """Seconds until the soonest cooling-down route is usable again (None when nothing is cooling down)."""
        now = time.time()
        times = [float(await self.db.get_setting("direct_cd", "0") or 0)]
        if not anon_only:
            times += [a["cooldown_until"] for a in await self.db.all("SELECT cooldown_until FROM accounts WHERE status!='bad'")]
        times += [p["cooldown_until"] for p in await self.db.all("SELECT cooldown_until FROM proxies WHERE status!='dead'")]
        waits = [t - now for t in times if t > now]
        return min(waits) if waits else None
