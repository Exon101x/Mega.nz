"""Native client for Mega's public-link API: list folders, read file info, download + decrypt single files.
No megatools needed. Network I/O goes through curl (so http / https / socks proxies work); crypto uses `cryptography`.

Protocol notes (public links):
  * every request is   POST https://g.api.mega.co.nz/cs?id=<n>[&n=<folder id>]   with a JSON list of commands
  * folder listing     {"a":"f","c":1,"r":1}            (the folder id goes in the `n` query parameter)
  * file download URL  {"a":"g","g":1,"p":<file id>}    (file link)   or   {"a":"g","g":1,"n":<node handle>} (inside a folder)
  * keys: a folder link carries a 16-byte master key; each node's key is stored AES-ECB-encrypted with it.
    A file key is 32 bytes: AES key = first half XOR second half, CTR nonce = bytes 16..24.
  * names live in the node attributes: AES-CBC (zero IV) of  'MEGA{"n":"name"}'.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

log = logging.getLogger("mega_api")
API_URL = "https://g.api.mega.co.nz/cs"


# --------------------------------------------------------------------------- errors
class MegaError(Exception):
    """kind: quota | dead | key | auth | network | unsupported | cancelled | other"""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


def classify(text: str) -> str:
    t = (text or "").lower()
    if re.search(r"509|bandwidth|quota|limit exceeded|over ?quota|too many (requests|connections)|rate ?limit", t):
        return "quota"
    if re.search(r"decrypt|invalid (key|link|url)|bad (key|link)|wrong key|ekey", t):
        return "key"
    if re.search(r"enoent|not found|removed|taken down|terms of service|blocked|expired|ebloc|eexpired|\b404\b|\b410\b", t):
        return "dead"
    if re.search(r"login|credential|password|esid|mfa|two.?factor|2fa|unauthori", t):
        return "auth"
    if re.search(r"proxy|connect|timed? ?out|timeout|resolve|network|ssl|certificate|curl|reset|refused|unreachable|dns", t):
        return "network"
    return "other"


def api_error(code: int) -> MegaError:
    if code == -17:
        return MegaError("quota", "transfer quota exceeded (EOVERQUOTA)")
    if code in (-4, -6):
        return MegaError("quota", "rate limited by Mega (%d)" % code)
    if code in (-8, -9, -11, -16):
        names = {-8: "link expired", -9: "link not found or removed", -11: "access denied", -16: "link blocked / taken down"}
        return MegaError("dead", names[code])
    if code in (-14, -15, -2):
        return MegaError("key", "invalid link or decryption key (%d)" % code)
    if code in (-3, -18):
        return MegaError("network", "Mega is temporarily unavailable (%d)" % code)
    return MegaError("other", "Mega API error %d" % code)


# --------------------------------------------------------------------------- crypto helpers
def b64d(s: str) -> bytes:
    s = (s or "").replace("-", "+").replace("_", "/").replace(",", "")
    return base64.b64decode(s + "=" * (-len(s) % 4))


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _ecb_decrypt(key: bytes, data: bytes) -> bytes:
    d = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return d.update(data) + d.finalize()


def _cbc_decrypt(key: bytes, data: bytes) -> bytes:
    data = data[: len(data) // 16 * 16]
    d = Cipher(algorithms.AES(key), modes.CBC(b"\0" * 16)).decryptor()
    return d.update(data) + d.finalize()


def fold_key(node_key: bytes) -> bytes:
    """AES key for a node: files carry 32 bytes (XOR the halves), folders 16."""
    if len(node_key) >= 32:
        return bytes(a ^ b for a, b in zip(node_key[:16], node_key[16:32]))
    return node_key[:16]


def decrypt_attrs(attr_b64: str, aes_key: bytes) -> Optional[dict]:
    try:
        plain = _cbc_decrypt(aes_key, b64d(attr_b64))
    except Exception:
        return None
    if not plain.startswith(b"MEGA"):
        return None
    try:
        return json.loads(plain[4:].rstrip(b"\0").decode("utf-8", "replace"))
    except ValueError:
        return None


def safe_name(name: str, fallback: str = "mega_file") -> str:
    name = re.sub(r"[\x00-\x1f/\\]", "_", name or "").strip().strip(".")
    return name[:200] or fallback


# --------------------------------------------------------------------------- tree
@dataclass
class Node:
    idx: int
    handle: Optional[str]
    name: str
    path: str
    size: int
    is_dir: bool
    parent: int  # idx of parent node, -1 for the root
    key: Optional[str] = None  # base64url node key (files inside folder links); needed to decrypt the download


def build_nodes(entries: Dict[str, dict]) -> List[Node]:
    """entries: relative path -> {handle, size, is_dir, key}. Adds missing parent folders, sorts, assigns indexes."""
    entries = dict(entries)
    for rel in list(entries):
        parts = rel.split("/")
        for i in range(1, len(parts)):
            anc = "/".join(parts[:i])
            if anc not in entries:
                entries[anc] = {"handle": None, "size": 0, "is_dir": True, "key": None}
            else:
                entries[anc]["is_dir"] = True
    order = sorted(entries, key=lambda r: [c.casefold() for c in r.split("/")])
    index = {rel: i for i, rel in enumerate(order)}
    nodes = []
    for i, rel in enumerate(order):
        e = entries[rel]
        parent = index.get(rel.rsplit("/", 1)[0], -1) if "/" in rel else -1
        nodes.append(Node(i, e.get("handle"), rel.rsplit("/", 1)[-1], rel, e.get("size", 0), e.get("is_dir", False), parent, e.get("key")))
    return nodes


def decrypt_node(n: dict, master: bytes) -> Optional[Tuple[bytes, str]]:
    """Returns (node key, name) for an API node, or None when it cannot be decrypted with this master key."""
    for part in str(n.get("k") or "").split("/"):
        if ":" not in part:
            continue
        try:
            raw = b64d(part.split(":", 1)[1])
            if not raw or len(raw) % 16:
                continue
            node_key = _ecb_decrypt(master, raw)
        except Exception:
            continue
        attrs = decrypt_attrs(n.get("a") or "", fold_key(node_key))
        if attrs and isinstance(attrs.get("n"), str):
            return node_key, attrs["n"]
    return None


def build_tree(raw: List[dict], master: bytes, root_id: str) -> Tuple[str, List[Node]]:
    info: Dict[str, dict] = {}
    for n in raw:
        if n.get("t") not in (0, 1) or not n.get("h"):
            continue
        got = decrypt_node(n, master)
        if got is None:
            log.debug("skipping undecryptable node %s", n.get("h"))
            continue
        info[n["h"]] = {"name": safe_name(got[1]), "key": got[0], "parent": n.get("p") or "",
                        "is_dir": n["t"] == 1, "size": int(n.get("s") or 0)}
    root_name = info.get(root_id, {}).get("name", "")
    entries: Dict[str, dict] = {}
    for h, i in info.items():
        if h == root_id:
            continue
        parts, cur, hops = [i["name"]], i["parent"], 0
        while cur and cur != root_id and cur in info and hops < 64:
            parts.append(info[cur]["name"])
            cur, hops = info[cur]["parent"], hops + 1
        rel = base = "/".join(reversed(parts))
        stem, ext = os.path.splitext(base)
        k = 1
        while rel in entries:  # Mega allows two files with the same name in one folder
            k += 1
            rel = "%s (%d)%s" % (stem, k, ext)
        entries[rel] = {"handle": h, "size": i["size"], "is_dir": i["is_dir"],
                        "key": None if i["is_dir"] else b64e(i["key"])}
    return root_name, build_nodes(entries)


# --------------------------------------------------------------------------- transport (curl)
def _proxy_cfg(proxy: Optional[str]) -> bytes:
    if not proxy:
        return b""
    return ('proxy = "%s"\n' % proxy.replace("\\", "\\\\").replace('"', '\\"')).encode()


async def _curl_post(url: str, body: str, proxy: Optional[str], timeout: int) -> Tuple[str, str, str]:
    """Returns (http_code, body, error). Proxy credentials travel via stdin, never argv."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-K", "-", "-s", "-S", "--max-time", str(timeout), "-X", "POST",
            "-H", "Content-Type: application/json", "--data-binary", body, "-w", "\n%{http_code}", url,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise MegaError("other", "curl is not installed (sudo apt install curl)")
    try:
        out, err = await asyncio.wait_for(proc.communicate(_proxy_cfg(proxy)), timeout + 10)
    except asyncio.TimeoutError:
        proc.kill()
        raise MegaError("network", "timed out talking to the Mega API")
    text, _, code = out.decode("utf-8", "replace").rpartition("\n")
    return code.strip(), text, err.decode("utf-8", "replace").strip()[:160]


async def api_call(commands: List[dict], *, node: str = "", proxy: Optional[str] = None, timeout: int = 60) -> list:
    url = "%s?id=%d%s" % (API_URL, random.randrange(10 ** 9), ("&n=" + node) if node else "")
    code, text, err = await _curl_post(url, json.dumps(commands), proxy, timeout)
    if code != "200":
        raise MegaError(classify(err) if err else "network", err or "HTTP %s from the Mega API" % code)
    try:
        data = json.loads(text)
    except ValueError:
        raise MegaError("network", "unreadable response from the Mega API")
    if isinstance(data, int):
        raise api_error(data)
    return data


def _first(data: list):
    res = data[0] if data else None
    if isinstance(res, int):
        raise api_error(res)
    if isinstance(res, dict) and isinstance(res.get("e"), int) and res["e"] < 0:
        raise api_error(res["e"])
    if not isinstance(res, dict):
        raise MegaError("network", "unexpected response from the Mega API")
    return res


# --------------------------------------------------------------------------- public operations
async def list_folder(link, proxy: Optional[str] = None) -> Tuple[str, List[Node]]:
    """Lists a public folder link. Returns (root folder name, nodes)."""
    res = _first(await api_call([{"a": "f", "c": 1, "r": 1}], node=link.id, proxy=proxy, timeout=120))
    raw = res.get("f") or []
    if not raw:
        raise MegaError("dead", "the folder is empty or could not be read")
    root, nodes = build_tree(raw, b64d(link.key), link.id)
    if not nodes:
        raise MegaError("key", "could not decrypt the folder — is the link's key complete?")
    return root, nodes


async def file_info(link, proxy: Optional[str] = None) -> Tuple[str, int]:
    """Name and size of a public file link."""
    res = _first(await api_call([{"a": "g", "p": link.id}], proxy=proxy))
    attrs = decrypt_attrs(res.get("at") or "", fold_key(b64d(link.key)))
    if not attrs or not isinstance(attrs.get("n"), str):
        raise MegaError("key", "could not decrypt the file name — is the link's key complete?")
    return safe_name(attrs["n"]), int(res.get("s") or 0)


async def _kill(proc) -> None:
    try:
        proc.kill()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except Exception:
        pass


async def download(link, handle: str, key_b64: Optional[str], dest: Path, proxy: Optional[str] = None, *,
                   name_hint: str = "", on_progress: Optional[Callable[[int], None]] = None,
                   abort: Optional[asyncio.Event] = None, stall_timeout: int = 150) -> Path:
    """Downloads one file (file link, or a file inside a folder link), decrypting while it streams to disk."""
    if link.kind == "file":
        command, node, key = {"a": "g", "g": 1, "p": link.id}, "", b64d(link.key)
    else:
        if not key_b64:  # e.g. a job created before keys were stored: look the node up again
            _root, nodes = await list_folder(link, proxy)
            key_b64 = next((n.key for n in nodes if n.handle == handle), None)
            if not key_b64:
                raise MegaError("dead", "that file is no longer in the folder")
        command, node, key = {"a": "g", "g": 1, "n": handle}, link.id, b64d(key_b64)
    res = _first(await api_call([command], node=node, proxy=proxy))
    url = res.get("g")
    if not url:
        if res.get("tl"):
            raise MegaError("quota", "transfer quota exceeded, retry in %s s" % res.get("tl"))
        raise MegaError("network", "Mega returned no download URL")
    size = int(res.get("s") or 0)
    aes_key = fold_key(key)
    nonce = key[16:24] + b"\0" * 8
    attrs = decrypt_attrs(res.get("at") or "", aes_key)
    name = safe_name((attrs or {}).get("n") or name_hint)
    dest.mkdir(parents=True, exist_ok=True)
    out_path = dest / name
    await _stream_decrypt(url, out_path, aes_key, nonce, size, proxy, on_progress, abort, stall_timeout)
    return out_path


async def _stream_decrypt(url: str, out_path: Path, aes_key: bytes, nonce: bytes, size: int, proxy: Optional[str],
                          on_progress, abort, stall_timeout: int) -> None:
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-K", "-", "-s", "-S", "--fail", "-L", "--connect-timeout", "30", url,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        raise MegaError("other", "curl is not installed (sudo apt install curl)")
    proc.stdin.write(_proxy_cfg(proxy))
    await proc.stdin.drain()
    proc.stdin.close()
    decryptor = Cipher(algorithms.AES(aes_key), modes.CTR(nonce)).decryptor()
    abort_wait = asyncio.ensure_future(abort.wait()) if abort else None
    written = 0
    try:
        with open(out_path, "wb") as fh:
            while True:
                read = asyncio.ensure_future(proc.stdout.read(1 << 20))
                waiting = {read} | ({abort_wait} if abort_wait else set())
                done, _ = await asyncio.wait(waiting, timeout=stall_timeout, return_when=asyncio.FIRST_COMPLETED)
                if read not in done:
                    read.cancel()
                    await _kill(proc)
                    if abort_wait is not None and abort_wait in done:
                        raise MegaError("cancelled", "aborted")
                    raise MegaError("network", "download stalled for %ds" % stall_timeout)
                chunk = read.result()
                if not chunk:
                    break
                plain = decryptor.update(chunk)
                await asyncio.to_thread(fh.write, plain)
                written += len(plain)
                if on_progress:
                    on_progress(written)
        rc = await proc.wait()
        err = (await proc.stderr.read()).decode("utf-8", "replace").strip()
        if rc != 0:
            raise MegaError(classify(err), err[-200:] or "download failed (curl exit %d)" % rc)
        if size and written != size:
            raise MegaError("network", "download incomplete (%d of %d bytes)" % (written, size))
    except BaseException:
        if proc.returncode is None:
            await _kill(proc)
        out_path.unlink(missing_ok=True)
        raise
    finally:
        if abort_wait:
            abort_wait.cancel()
