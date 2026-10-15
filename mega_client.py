"""mega.nz link parsing and the downloader front-end.

Anonymous routes (with or without a proxy) use the built-in API client in mega_api.py — megatools is NOT needed.
Routes that carry a Mega account use the optional megatools program (single-file links only)."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

import mega_api
from mega_api import MegaError, Node, b64d, build_nodes, classify  # noqa: F401  (re-exported)

log = logging.getLogger("mega")

UNKNOWN_NAME = "mega file"  # placeholder name for a single-file link whose real name could not be read

_HOST = r"mega\.(?:nz|io|co\.nz)"
NEW_RE = re.compile(
    r"https?://(?:www\.)?%s/(file|folder)/([A-Za-z0-9_-]{8})#([A-Za-z0-9_-]{16,})"
    r"(?:/(file|folder)/([A-Za-z0-9_-]{8}))?" % _HOST
)
OLD_RE = re.compile(r"https?://(?:www\.)?%s/#(F?)!([A-Za-z0-9_-]{8})!([A-Za-z0-9_-]{16,})" % _HOST)


# --------------------------------------------------------------------------- links
@dataclass(frozen=True)
class MegaLink:
    kind: str  # "file" | "folder"
    id: str
    key: str
    sub_kind: Optional[str] = None
    sub_id: Optional[str] = None

    @property
    def url(self) -> str:
        return "https://mega.nz/%s/%s#%s" % (self.kind, self.id, self.key)

    def file_url(self, handle: str) -> str:
        """URL of one file inside a folder link."""
        return "%s/file/%s" % (self.url, handle)


def extract_links(text: str) -> List[MegaLink]:
    """Finds every mega.nz link (new and legacy formats) in a piece of text."""
    found: List[MegaLink] = []
    seen = set()
    hits = []
    for m in NEW_RE.finditer(text or ""):
        hits.append((m.start(), MegaLink(m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))))
    for m in OLD_RE.finditer(text or ""):
        hits.append((m.start(), MegaLink("folder" if m.group(1) else "file", m.group(2), m.group(3))))
    for _, link in sorted(hits, key=lambda h: h[0]):
        k = (link.kind, link.id, link.key)
        if k not in seen:
            seen.add(k)
            found.append(link)
    return found


@dataclass
class Creds:
    email: Optional[str] = None
    password: Optional[str] = None
    proxy: Optional[str] = None


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(str(path)):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _esc(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


# --------------------------------------------------------------------------- front-end
class MegaTools:
    def __init__(self, tmp_dir: Path, ls: str = "megals", dl: str = "megadl", df: str = "megadf"):
        self.tmp_dir = tmp_dir
        # command line for each megatools program; either the classic separate programs or `megatools <subcommand>`
        self.cmds = {"ls": [ls], "dl": [dl], "df": [df]}
        self.version = ""  # megatools version, if installed (only used for Mega-account routes)

    # -- native client (anonymous routes) ------------------------------------
    async def list_folder(self, link: MegaLink, creds: Optional[Creds]):
        """Returns (root folder name, nodes)."""
        return await mega_api.list_folder(link, creds.proxy if creds else None)

    async def file_info(self, link: MegaLink, creds: Optional[Creds]):
        """Returns (name, size) of a public file link."""
        return await mega_api.file_info(link, creds.proxy if creds else None)

    async def fetch(self, link: MegaLink, handle: str, key: Optional[str], dest: Path, creds: Optional[Creds], *,
                    name_hint: str = "", on_progress: Optional[Callable[[int], None]] = None,
                    abort: Optional[asyncio.Event] = None, stall_timeout: int = 150) -> Path:
        """Downloads one file of a public link into `dest` and returns its path."""
        if creds and creds.email:  # account route → megatools
            if link.kind != "file":
                raise MegaError("unsupported", "Mega accounts can only be used for single-file links")
            return await self.download(link.url, dest, creds, on_progress=on_progress, abort=abort, stall_timeout=stall_timeout)
        return await mega_api.download(link, handle, key, dest, creds.proxy if creds else None, name_hint=name_hint,
                                       on_progress=on_progress, abort=abort, stall_timeout=stall_timeout)

    # -- megatools (optional: Mega accounts) ----------------------------------
    def resolve_binaries(self, override: str = "") -> str:
        """Picks how to call megatools. `override` = path to the single multi-call `megatools` binary
        (static builds and --prefix installs have it, and not always the old megadl/megals names)."""
        if override:
            path = shutil.which(override) or override
            self.cmds = {k: [path, k] for k in ("ls", "dl", "df")}
        elif not (shutil.which(self.cmds["ls"][0]) and shutil.which(self.cmds["dl"][0])):
            single = shutil.which("megatools")
            if single:
                self.cmds = {k: [single, k] for k in ("ls", "dl", "df")}
        return " ".join(self.cmds["dl"])

    async def detect_version(self) -> str:
        try:
            rc, out, err = await self._run_simple("dl", ["--version"], None, timeout=20)
        except MegaError:
            return ""
        m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", out + err)
        if m:
            self.version = ".".join(g for g in m.groups() if g is not None)
        return self.version

    def _write_config(self, creds: Optional[Creds]) -> Path:
        """Per-call megarc, so passwords and proxy credentials never appear on the command line."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix="megarc_", dir=str(self.tmp_dir))
        lines = []
        if creds and creds.email:
            lines += ["[Login]", "Username = %s" % _esc(creds.email), "Password = %s" % _esc(creds.password or "")]
        if creds and creds.proxy:
            lines += ["[Network]", "Proxy = %s" % _esc(creds.proxy)]
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
        os.chmod(name, 0o600)
        return Path(name)

    async def _spawn(self, tool: str, args: List[str], creds: Optional[Creds]):
        cfg = self._write_config(creds)
        env = os.environ.copy()
        if creds and creds.proxy:  # libcurl also honours these; belt and braces
            for k in ("all_proxy", "ALL_PROXY", "https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY"):
                env[k] = creds.proxy
        cmd = self.cmds[tool] + ["--no-ask-password", "--config", str(cfg)] + args
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, env=env)
        except FileNotFoundError:
            cfg.unlink(missing_ok=True)
            raise MegaError("other", "megatools is not installed (%s not found) — it is only needed for Mega accounts; "
                                     "see README or set MEGATOOLS_BIN" % self.cmds[tool][0])
        return proc, cfg

    async def _run_simple(self, tool: str, args: List[str], creds: Optional[Creds], timeout: int = 180):
        proc, cfg = await self._spawn(tool, args, creds)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise MegaError("network", "timed out after %ds" % timeout)
        finally:
            cfg.unlink(missing_ok=True)
        return proc.returncode, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")

    async def account_info(self, creds: Creds) -> str:
        rc, out, err = await self._run_simple("df", ["--human"], creds, timeout=90)
        if rc != 0:
            msg = (err or out).strip()[-300:]
            raise MegaError(classify(msg), msg or "login failed")
        return " · ".join(x.strip() for x in out.splitlines() if x.strip())

    async def download(self, url: str, dest: Path, creds: Optional[Creds], *,
                       on_progress: Optional[Callable[[int], None]] = None,
                       abort: Optional[asyncio.Event] = None, stall_timeout: int = 150) -> Path:
        """Downloads one file link with megatools (account routes). Progress is measured from the growing file."""
        dest.mkdir(parents=True, exist_ok=True)
        proc, cfg = await self._spawn("dl", ["--no-progress", "--path", str(dest), url], creds)
        comm = asyncio.create_task(proc.communicate())
        abort_wait = asyncio.create_task(abort.wait()) if abort else None
        last_size, last_change = 0, time.monotonic()
        try:
            while True:
                waiting = {comm} | ({abort_wait} if abort_wait else set())
                done, _ = await asyncio.wait(waiting, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
                size = dir_size(dest)
                now = time.monotonic()
                if size != last_size:
                    last_size, last_change = size, now
                if on_progress:
                    on_progress(size)
                if comm in done:
                    break
                if abort_wait is not None and abort_wait in done:
                    await self._stop(proc, comm)
                    raise MegaError("cancelled", "aborted")
                if now - last_change > stall_timeout:
                    await self._stop(proc, comm)
                    raise MegaError("network", "download stalled for %ds" % stall_timeout)
            out, err = comm.result()
        finally:
            cfg.unlink(missing_ok=True)
            if abort_wait:
                abort_wait.cancel()
            if not comm.done():
                comm.cancel()
        text = (err.decode("utf-8", "replace") + "\n" + out.decode("utf-8", "replace")).strip()
        if proc.returncode != 0:
            raise MegaError(classify(text), text[-300:] or "megadl failed")
        files = [p for p in dest.rglob("*") if p.is_file() and not p.name.startswith(".megatmp")]
        if not files:
            raise MegaError("other", "megadl finished but produced no file")
        return max(files, key=lambda p: p.stat().st_size)

    @staticmethod
    async def _stop(proc, comm) -> None:
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(comm, 5)
        except Exception:
            pass
