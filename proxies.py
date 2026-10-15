"""Proxy parsing, checking (reachability, latency, exit IP, IP-leak test), API import and pool management."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

log = logging.getLogger("proxies")

SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}
TEST_URL = "https://g.api.mega.co.nz/cs"
IP_URL = "https://api.ipify.org"
_IP_RE = re.compile(r"^\s*((?:\d{1,3}\.){3}\d{1,3}|[0-9a-fA-F:]{3,39})\s*$")


# --------------------------------------------------------------------------- parsing
def normalize_proxy(raw: str) -> Optional[str]:
    """Accepts ip:port, user:pass@ip:port, ip:port:user:pass or scheme://… and returns a URL (or None)."""
    raw = (raw or "").strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" not in raw:
        if "@" in raw:
            raw = "http://" + raw
        else:
            parts = raw.split(":")
            if len(parts) == 2:
                raw = "http://" + raw
            elif len(parts) == 4:
                host, port, user, pw = parts
                raw = "http://%s:%s@%s:%s" % (user, pw, host, port)
            else:
                return None
    try:
        u = urlsplit(raw)
        port = u.port
    except ValueError:
        return None
    if u.scheme.lower() not in SCHEMES or not u.hostname or not port or not (0 < port < 65536):
        return None
    return raw


def mask_proxy(url: str) -> str:
    """Hides the password when a proxy is shown in chat."""
    try:
        u = urlsplit(url)
    except ValueError:
        return "proxy"
    cred = ("%s:***@" % u.username) if u.username else ""
    port = ":%d" % u.port if u.port else ""
    return "%s://%s%s%s" % (u.scheme, cred, u.hostname or "?", port)


def mask_url(url: str) -> str:
    """Shows an API URL without its query string (it often carries an API key)."""
    try:
        u = urlsplit(url)
    except ValueError:
        return "api"
    path = u.path if len(u.path) <= 30 else u.path[:29] + "…"
    return "%s://%s%s%s" % (u.scheme, u.netloc.split("@")[-1], path, "?…" if u.query else "")


def proxy_key(url: str) -> tuple:
    """Identity of a proxy regardless of scheme: (host, port, user)."""
    try:
        u = urlsplit(url)
        return (u.hostname, u.port, u.username or "")
    except ValueError:
        return (url, 0, "")


@dataclass(frozen=True)
class Entry:
    url: str
    detect: bool = False  # True = no scheme was given, so find out whether it is http / socks5 / socks4


def parse_proxy_entries(text: str, default_scheme: str = "http") -> Tuple[List[Entry], int]:
    """default_scheme applies to lines without scheme: http | socks5 | socks4 | auto (detect by testing)."""
    entries, seen, bad = [], set(), 0
    for line in re.split(r"[\r\n;,]+", text or ""):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        explicit = "://" in line
        url = normalize_proxy(line)
        if not url:
            bad += 1
            continue
        detect = False
        if not explicit:
            if default_scheme == "auto":
                detect = True
            elif default_scheme in SCHEMES and default_scheme != "http":
                url = "%s://%s" % (default_scheme, url.split("://", 1)[1])
        if url not in seen:
            seen.add(url)
            entries.append(Entry(url, detect))
    return entries, bad


def parse_proxy_list(text: str) -> Tuple[List[str], int]:
    entries, bad = parse_proxy_entries(text, "http")
    return [e.url for e in entries], bad


def scheme_variants(url: str) -> List[str]:
    rest = url.split("://", 1)[1]
    return ["%s://%s" % (s, rest) for s in ("http", "socks5", "socks4")]


# --------------------------------------------------------------------------- API responses
HOST_KEYS = ("proxy_address", "ip", "host", "address", "hostname", "server", "proxy_ip")
PORT_KEYS = ("port", "proxy_port")
USER_KEYS = ("username", "user", "login", "proxy_username")
PASS_KEYS = ("password", "pass", "pwd", "proxy_password")
SCHEME_KEYS = ("protocol", "type", "scheme", "proxy_type", "protocols")
URL_KEYS = ("proxy", "url", "proxy_url", "uri")
LOOKS_RE = re.compile(r"^(?:[A-Za-z0-9]+://)?(?:[^\s@/]+@)?[A-Za-z0-9.\-]+:\d{2,5}(?::[^\s:]+:[^\s:]+)?$")
LOOSE_RE = re.compile(r"(?:(?:https?|socks4a?|socks5h?)://)?(?:[^\s@/:]+:[^\s@/]+@)?(?:\d{1,3}\.){3}\d{1,3}:\d{2,5}")


def looks_like_proxy(s: str) -> bool:
    return bool(LOOKS_RE.match(s))


def _first(d: dict, keys):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", [], {}):
            return v
    return None


def _walk(obj, out: List[str]) -> None:
    if isinstance(obj, dict):
        if any(obj.get(k) is False for k in ("valid", "alive", "working", "is_alive")):
            return  # providers flag dead entries; skip them
        host, port = _first(obj, HOST_KEYS), _first(obj, PORT_KEYS)
        if isinstance(host, str) and isinstance(port, (int, str)) and str(port).isdigit():
            scheme = _first(obj, SCHEME_KEYS)
            if isinstance(scheme, list):
                scheme = scheme[0] if scheme else None
            scheme = str(scheme).lower() if scheme else ""
            if scheme not in SCHEMES:
                scheme = ""  # e.g. "elite" / "anonymous" describe anonymity, not the protocol
            user, pw = _first(obj, USER_KEYS), _first(obj, PASS_KEYS)
            auth = "%s:%s@" % (quote(str(user), safe=""), quote(str(pw), safe="")) if user and pw else ""
            out.append("%s%s%s:%s" % (scheme + "://" if scheme else "", auth, host, port))
            return
        url = _first(obj, URL_KEYS)
        if isinstance(url, str) and looks_like_proxy(url.strip()):
            out.append(url.strip())
            return
        for v in obj.values():
            if isinstance(v, (dict, list, str)):
                _walk(v, out)
    elif isinstance(obj, list):
        for x in obj:
            _walk(x, out)
    elif isinstance(obj, str):
        for line in obj.splitlines():
            if looks_like_proxy(line.strip()):
                out.append(line.strip())


def extract_raw(body: str) -> Tuple[List[str], Optional[str]]:
    """Pulls proxy strings out of a plain-text list or a JSON document. Returns (raw proxies, next page URL)."""
    s = (body or "").lstrip()
    if s[:1] in ("{", "["):
        try:
            data = json.loads(s)
        except ValueError:
            data = None
        if data is not None:
            out: List[str] = []
            _walk(data, out)
            nxt = data.get("next") if isinstance(data, dict) and isinstance(data.get("next"), str) else None
            return out, nxt
    out = []
    for line in (body or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if looks_like_proxy(line):
            out.append(line)
        else:
            m = LOOSE_RE.search(line)  # e.g. "1.2.3.4:8080 US 120ms"
            if m:
                out.append(m.group(0))
    return out, None


class SourceError(Exception):
    pass


def _http_get(url: str, headers: Dict[str, str], timeout: int = 30, max_bytes: int = 4 * 1024 * 1024) -> str:
    if not re.match(r"https?://", url, re.I):
        raise SourceError("only http(s) URLs are allowed")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (mega-bot proxy import)", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(max_bytes + 1)
            charset = r.headers.get_content_charset() or "utf-8"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise SourceError("unauthorised (HTTP %d) — check the API key / Authorization header" % e.code)
        if e.code == 429:
            raise SourceError("rate limited by the API (HTTP 429) — try again later")
        raise SourceError("HTTP %d from the API" % e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise SourceError("cannot reach the API (%s)" % (getattr(e, "reason", None) or e))
    if len(data) > max_bytes:
        raise SourceError("response is larger than %d MB" % (max_bytes // 1024 // 1024))
    return data.decode(charset, "replace")


async def fetch_proxies(url: str, headers: Dict[str, str], default_scheme: str = "auto",
                        max_items: int = 500, max_pages: int = 20) -> Tuple[List[Entry], int]:
    """Downloads a proxy list from an API/URL (text or JSON, follows `next` pages on the same host)."""
    entries: List[Entry] = []
    seen, bad, pages, page_url = set(), 0, 0, url
    host = urlsplit(url).netloc
    while page_url and pages < max_pages and len(entries) < max_items:
        body = await asyncio.to_thread(_http_get, page_url, headers)
        pages += 1
        raws, nxt = extract_raw(body)
        ents, b = parse_proxy_entries("\n".join(raws), default_scheme)
        bad += b
        for e in ents:
            if e.url not in seen:
                seen.add(e.url)
                entries.append(e)
        page_url = nxt if nxt and nxt != page_url and urlsplit(nxt).netloc == host else None
    return entries[:max_items], bad


# --------------------------------------------------------------------------- checking
@dataclass
class ProbeResult:
    url: str
    ok: bool
    latency: Optional[float] = None
    exit_ip: str = ""
    error: str = ""


async def _curl(target: str, proxy: Optional[str] = None, timeout: int = 20) -> Tuple[str, float, str, str]:
    """Returns (http_code, seconds, body, error). The proxy (with credentials) goes through stdin, never argv."""
    cfg = ""
    if proxy:
        cfg = 'proxy = "%s"\n' % proxy.replace("\\", "\\\\").replace('"', '\\"')
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-K", "-", "-s", "-S", "--max-time", str(timeout), "-w", "\n%{http_code} %{time_total}", target,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return "000", 0.0, "", "curl is not installed"
    try:
        out, err = await asyncio.wait_for(proc.communicate(cfg.encode()), timeout + 10)
    except asyncio.TimeoutError:
        proc.kill()
        return "000", 0.0, "", "timed out"
    body, _, last = out.decode("utf-8", "replace").rpartition("\n")
    parts = last.split()
    code = parts[0] if len(parts) == 2 else "000"
    try:
        secs = float(parts[1]) if len(parts) == 2 else 0.0
    except ValueError:
        secs = 0.0
    return code, secs, body, err.decode("utf-8", "replace").strip()[:160]


_own_ip = {"ip": "", "t": 0.0}


async def get_own_ip() -> str:
    """This machine's public IP (cached 10 min); '' when it cannot be determined."""
    if _own_ip["ip"] and time.time() - _own_ip["t"] < 600:
        return _own_ip["ip"]
    code, _t, body, _e = await _curl(IP_URL, None, 15)
    ip = body.strip() if code == "200" and _IP_RE.match(body) else ""
    if ip:
        _own_ip.update(ip=ip, t=time.time())
    return ip


async def test_proxy(url: str, timeout: int = 20, own_ip: str = "") -> ProbeResult:
    """Checks that the proxy reaches the Mega API, measures latency, learns the exit IP and rejects
    proxies that do not hide this machine's IP (transparent, or ignored by the client)."""
    (code, secs, _b, err), (ip_code, _s, ip_body, _e) = await asyncio.gather(
        _curl(TEST_URL, url, timeout), _curl(IP_URL, url, timeout))
    ok = len(code) == 3 and code[0] in "234"
    exit_ip = ip_body.strip() if ip_code == "200" and _IP_RE.match(ip_body) else ""
    if ok and own_ip and exit_ip and exit_ip == own_ip:
        return ProbeResult(url, False, None, exit_ip, "does not hide your IP (transparent or ignored)")
    if ok:
        return ProbeResult(url, True, secs, exit_ip, "")
    return ProbeResult(url, False, None, exit_ip, err or "no response (HTTP %s)" % code)


async def probe_entry(entry: Entry, own_ip: str = "") -> ProbeResult:
    """Tests one entry; when its scheme is unknown, tries http, socks5 and socks4 in turn."""
    last = None
    for url in (scheme_variants(entry.url) if entry.detect else [entry.url]):
        last = await test_proxy(url, own_ip=own_ip)
        if last.ok:
            return last
    return last


def proxy_state(p: dict, now: Optional[float] = None) -> str:
    """ok | cooldown | dead"""
    now = now or time.time()
    if p["status"] == "dead":
        return "dead"
    return "cooldown" if p["cooldown_until"] > now else "ok"


# --------------------------------------------------------------------------- pool manager
class ProxyManager:
    def __init__(self, db, vault=None):
        self.db, self.vault = db, vault

    async def list(self) -> list:
        return await self.db.all("SELECT * FROM proxies ORDER BY id")

    async def get(self, pid: int) -> Optional[dict]:
        return await self.db.one("SELECT * FROM proxies WHERE id=?", (pid,))

    # -- adding ------------------------------------------------------------
    async def add_entries(self, entries: List[Entry], source_id: Optional[int] = None, results: Optional[dict] = None):
        """Inserts new proxies. Returns ([(id, detect)], duplicates). `results` maps url → ProbeResult
        (already-checked proxies are stored with their status)."""
        have = {proxy_key(p["url"]) for p in await self.list()}
        new, dupes = [], 0
        for e in entries:
            k = proxy_key(e.url)
            if k in have:
                dupes += 1
                continue
            have.add(k)
            pid = await self.db.execute("INSERT INTO proxies(url, source_id) VALUES(?,?)", (e.url, source_id))
            res = (results or {}).get(e.url)
            if res is not None:
                await self._store(pid, res)
            new.append((pid, e.detect and res is None))
        return new, dupes

    async def add_many(self, text: str):
        """Adds proxies from pasted text; lines without a scheme are auto-detected later by verify().
        Returns ([(id, detect)], duplicates, invalid lines)."""
        entries, bad = parse_proxy_entries(text, "auto")
        new, dupes = await self.add_entries(entries)
        return new, dupes, bad

    async def add_checked(self, results: List[ProbeResult]) -> Tuple[int, int]:
        good = [r for r in results if r.ok]
        new, dupes = await self.add_entries([Entry(r.url) for r in good], None, {r.url: r for r in good})
        return len(new), dupes

    async def remove(self, pid: int) -> None:
        await self.db.execute("UPDATE accounts SET proxy_id=NULL WHERE proxy_id=?", (pid,))
        await self.db.execute("DELETE FROM proxies WHERE id=?", (pid,))

    async def reset(self, pid: int) -> None:
        await self.db.execute("UPDATE proxies SET status='ok', fails=0, cooldown_until=0, last_error=NULL WHERE id=?", (pid,))

    # -- checking ----------------------------------------------------------
    async def _store(self, pid: int, res: ProbeResult) -> None:
        if res.ok:
            await self.db.execute(
                "UPDATE proxies SET status='ok', fails=0, latency=?, exit_ip=?, checked_at=?, last_error=NULL WHERE id=?",
                (res.latency, res.exit_ip or None, time.time(), pid))
        else:
            await self.db.execute(
                "UPDATE proxies SET status='dead', latency=NULL, exit_ip=?, checked_at=?, last_error=? WHERE id=?",
                (res.exit_ip or None, time.time(), res.error[:160], pid))

    async def test(self, pid: int) -> Optional[dict]:
        p = await self.get(pid)
        if not p:
            return None
        await self._store(pid, await test_proxy(p["url"], own_ip=await get_own_ip()))
        return await self.get(pid)

    async def autodetect(self, pid: int) -> Optional[dict]:
        """Tests http / socks5 / socks4 variants and keeps the one that works."""
        p = await self.get(pid)
        if not p:
            return None
        res = await probe_entry(Entry(p["url"], True), await get_own_ip())
        if res.ok and res.url != p["url"]:
            clash = await self.db.one("SELECT id FROM proxies WHERE url=? AND id!=?", (res.url, pid))
            if clash:
                await self.db.execute("DELETE FROM proxies WHERE id=?", (pid,))
                return None
            await self.db.execute("UPDATE proxies SET url=? WHERE id=?", (res.url, pid))
        await self._store(pid, res)
        return await self.get(pid)

    async def verify(self, new: List[Tuple[int, bool]], parallel: int = 5) -> None:
        """Background check of freshly added proxies (new = [(id, needs scheme detection)])."""
        sem = asyncio.Semaphore(parallel)

        async def one(pid: int, detect: bool):
            async with sem:
                await (self.autodetect(pid) if detect else self.test(pid))

        await asyncio.gather(*(one(i, d) for i, d in new), return_exceptions=True)

    async def test_many(self, ids: List[int], parallel: int = 5) -> None:
        await self.verify([(i, False) for i in ids], parallel)

    async def test_all(self) -> None:
        await self.test_many([p["id"] for p in await self.list()])

    async def check_entries(self, entries: List[Entry], concurrency: int = 10,
                            on_result: Optional[Callable[[ProbeResult], Awaitable]] = None) -> List[ProbeResult]:
        """Standalone checker: tests entries without touching the pool."""
        own = await get_own_ip()
        sem = asyncio.Semaphore(concurrency)

        async def one(e: Entry) -> ProbeResult:
            async with sem:
                res = await probe_entry(e, own)
            if on_result:
                await on_result(res)
            return res

        return list(await asyncio.gather(*(one(e) for e in entries)))

    # -- API sources -------------------------------------------------------
    async def sources(self) -> list:
        return await self.db.all("SELECT * FROM proxy_sources ORDER BY id")

    async def source(self, sid: int) -> Optional[dict]:
        return await self.db.one("SELECT * FROM proxy_sources WHERE id=?", (sid,))

    def headers_of(self, src: dict) -> Dict[str, str]:
        if not src.get("headers_enc") or self.vault is None:
            return {}
        return json.loads(self.vault.decrypt(src["headers_enc"]))

    async def add_source(self, url: str, headers: Dict[str, str], scheme: str = "auto", interval_min: int = 0) -> int:
        enc = self.vault.encrypt(json.dumps(headers)) if headers and self.vault else None
        name = (urlsplit(url).hostname or "api")[:40]
        return await self.db.execute(
            "INSERT INTO proxy_sources(name, url, headers_enc, scheme, interval_min) VALUES(?,?,?,?,?)",
            (name, url, enc, scheme, interval_min))

    async def delete_source(self, sid: int, delete_proxies: bool = False) -> None:
        if delete_proxies:
            for row in await self.db.all("SELECT id FROM proxies WHERE source_id=?", (sid,)):
                await self.remove(row["id"])
        else:
            await self.db.execute("UPDATE proxies SET source_id=NULL WHERE source_id=?", (sid,))
        await self.db.execute("DELETE FROM proxy_sources WHERE id=?", (sid,))

    async def refresh_source(self, sid: int, wait: bool = False) -> dict:
        """Fetches the API, adds new proxies, (optionally) removes ones that disappeared, verifies the new ones."""
        src = await self.source(sid)
        if not src:
            return {"error": "source not found"}
        try:
            entries, bad = await fetch_proxies(src["url"], self.headers_of(src), src["scheme"])
            if not entries:
                raise SourceError("the response contained no proxies")
        except SourceError as e:
            await self.db.execute("UPDATE proxy_sources SET last_fetch=?, last_error=? WHERE id=?", (time.time(), str(e)[:200], sid))
            return {"error": str(e)}
        removed = 0
        if src["sync"]:
            fresh = {proxy_key(e.url) for e in entries}
            for row in await self.db.all("SELECT id, url FROM proxies WHERE source_id=?", (sid,)):
                if proxy_key(row["url"]) not in fresh:
                    await self.remove(row["id"])
                    removed += 1
        new, dupes = await self.add_entries(entries, sid)
        await self.db.execute("UPDATE proxy_sources SET last_fetch=?, last_count=?, last_error=NULL WHERE id=?",
                              (time.time(), len(entries), sid))
        task = self.verify(new)
        if wait:
            await task
        else:
            asyncio.create_task(task)
        return {"fetched": len(entries), "added": len(new), "dupes": dupes, "removed": removed, "bad": bad}

    # -- background maintenance ----------------------------------------------
    async def scheduler(self) -> None:
        """Refreshes API sources that are due and periodically re-checks the whole pool."""
        while True:
            await asyncio.sleep(30)
            try:
                now = time.time()
                for s in await self.sources():
                    if s["enabled"] and s["interval_min"] > 0 and s["last_fetch"] + s["interval_min"] * 60 <= now:
                        log.info("refreshing proxy source #%d", s["id"])
                        await self.refresh_source(s["id"], wait=True)
                every = int(await self.db.get_setting("proxy_check_min", "60") or 0)
                last = float(await self.db.get_setting("proxy_last_check", "0") or 0)
                if every > 0 and now - last >= every * 60 and await self.list():
                    await self.db.set_setting("proxy_last_check", now)
                    await self.test_all()
                    if await self.db.get_setting("proxy_autoremove", "0") == "1":
                        for p in await self.db.all("SELECT id FROM proxies WHERE status='dead'"):
                            await self.remove(p["id"])
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("proxy scheduler failed")
