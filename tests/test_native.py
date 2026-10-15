"""End-to-end tests of the native Mega client against a fake Mega API + CDN served on localhost.
The fake encrypts with the documented scheme, so these check internal consistency and the plumbing
(curl transport, streaming decrypt, errors, cancel, stalls) — not compatibility with the live service."""
import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import stubs  # noqa: E402

stubs.install_if_missing()

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes  # noqa: E402

import mega_api as M  # noqa: E402
from mega_client import Creds, MegaLink, MegaTools, extract_links  # noqa: E402
from tests.test_core import make_app, run  # noqa: E402

ROOT = "ROOTHNDL"
MASTER = os.urandom(16)
OTHER_MASTER = os.urandom(16)


def pad16(b):
    return b + b"\0" * (-len(b) % 16)


def enc_attrs(name, aes_key):
    plain = pad16(b"MEGA" + json.dumps({"n": name}).encode())
    e = Cipher(algorithms.AES(aes_key), modes.CBC(b"\0" * 16)).encryptor()
    return M.b64e(e.update(plain) + e.finalize())


def enc_key(master, node_key):
    e = Cipher(algorithms.AES(master), modes.ECB()).encryptor()
    return M.b64e(e.update(node_key) + e.finalize())


def ctr_encrypt(content, node_key):
    e = Cipher(algorithms.AES(M.fold_key(node_key)), modes.CTR(node_key[16:24] + b"\0" * 8)).encryptor()
    return e.update(content) + e.finalize()


class FakeMega:
    """Holds a folder tree and the files' plaintext."""

    def __init__(self):
        self.nodes, self.files = [], {}
        self._folder("ROOTHNDL", "", "Shared stuff", None)
        self._folder("SEASON01", ROOT, "Season 1", None)
        self.big = os.urandom(3 * 1024 * 1024 + 123)  # > 1 MiB so chunked streaming is exercised
        self._file("EPISODE1", "SEASON01", "ep1.mkv", self.big)
        self._file("README01", ROOT, "readme.txt", b"hello mega")
        self._file("DUPNAME1", ROOT, "a.txt", b"first")
        self._file("DUPNAME2", ROOT, "a.txt", b"second")
        self._file("SHORT001", ROOT, "short.bin", os.urandom(5000))
        self._file("SLOW0001", ROOT, "slow.bin", os.urandom(5000))
        self._file("CDN50900", ROOT, "cdn509.bin", os.urandom(5000))
        # a node encrypted with a different master key (must be skipped, not crash)
        k = os.urandom(32)
        self.nodes.append({"h": "FOREIGN1", "p": ROOT, "t": 0, "s": 3, "a": enc_attrs("x", M.fold_key(k)),
                           "k": ROOT + ":" + enc_key(OTHER_MASTER, k)})
        # a stand-alone public file link
        self.file_key = os.urandom(32)
        self.file_content = os.urandom(70000)
        self.file_id = "FILELINK"
        self.files[self.file_id] = (self.file_key, self.file_content, "single.bin")

    def _folder(self, h, parent, name, _):
        k = os.urandom(16)
        self.nodes.append({"h": h, "p": parent, "t": 1, "a": enc_attrs(name, k), "k": ROOT + ":" + enc_key(MASTER, k)})

    def _file(self, h, parent, name, content):
        k = os.urandom(32)
        self.files[h] = (k, content, name)
        self.nodes.append({"h": h, "p": parent, "t": 0, "s": len(content), "a": enc_attrs(name, M.fold_key(k)),
                           "k": ROOT + ":" + enc_key(MASTER, k)})


FAKE = FakeMega()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def _send(self, data, status=200, ctype="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client (curl) was killed on purpose by an abort / stall test

    def do_POST(self):
        port = self.server.server_address[1]
        qs = parse_qs(urlsplit(self.path).query)
        folder = (qs.get("n") or [""])[0]
        cmds = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        cmd = cmds[0]
        if cmd["a"] == "f":
            self._send(json.dumps([{"f": FAKE.nodes}] if folder == ROOT else [-9]).encode())
            return
        h = cmd.get("n") or cmd.get("p")
        if h == "OVERQUOTA":
            self._send(b"[-17]")
        elif h not in FAKE.files or (cmd.get("n") and folder != ROOT):
            self._send(b"[-9]")
        else:
            key, content, name = FAKE.files[h]
            res = {"g": "http://127.0.0.1:%d/data/%s" % (port, h), "s": len(content), "at": enc_attrs(name, M.fold_key(key))}
            self._send(json.dumps([res]).encode())

    def do_GET(self):
        h = urlsplit(self.path).path.rsplit("/", 1)[-1]
        key, content, _ = FAKE.files[h]
        data = ctr_encrypt(content, key)
        if h == "CDN50900":
            self._send(b"Bandwidth Limit Exceeded", 509, "text/plain")
        elif h == "SHORT001":
            self._send(data[:2000], 200, "application/octet-stream")  # claims 5000 bytes in the API, delivers 2000
        elif h == "SLOW0001":
            time.sleep(4)
            self._send(data, 200, "application/octet-stream")
        else:
            self._send(data, 200, "application/octet-stream")


class Server:
    def __enter__(self):
        class S(ThreadingHTTPServer):
            daemon_threads = True

        self.srv = S(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.old = M.API_URL
        M.API_URL = "http://127.0.0.1:%d/cs" % self.srv.server_address[1]
        return self

    def __exit__(self, *a):
        M.API_URL = self.old
        self.srv.shutdown()


def folder_link(key=None):
    return MegaLink("folder", ROOT, key or M.b64e(MASTER))


class Tree(unittest.TestCase):
    def test_crypto_helpers(self):
        k = os.urandom(32)
        self.assertEqual(len(M.fold_key(k)), 16)
        self.assertEqual(M.b64d(M.b64e(b"\x00\xff\xfe")), b"\x00\xff\xfe")
        self.assertEqual(M.safe_name("a/b\\c\x00.mkv"), "a_b_c_.mkv")
        self.assertEqual(M.safe_name("..", "fallback"), "fallback")

    def test_list_folder(self):
        async def go():
            with Server():
                root, nodes = await M.list_folder(folder_link())
                self.assertEqual(root, "Shared stuff")
                paths = [n.path for n in nodes]
                self.assertEqual(paths, sorted(paths, key=lambda r: [c.casefold() for c in r.split("/")]))
                self.assertIn("Season 1/ep1.mkv", paths)
                self.assertIn("a.txt", paths)
                self.assertIn("a.txt", paths)
                self.assertIn("a (2).txt", paths)
                self.assertEqual(sum(1 for n in nodes if n.name.startswith("a")), 2)  # duplicate names both survive
                self.assertNotIn("x", [n.name for n in nodes])  # the node with a foreign key is skipped
                ep = next(n for n in nodes if n.path == "Season 1/ep1.mkv")
                self.assertEqual((ep.handle, ep.size, ep.is_dir, ep.parent >= 0), ("EPISODE1", len(FAKE.big), False, True))
                self.assertEqual(M.b64d(ep.key), FAKE.files["EPISODE1"][0])
        run(go())

    def test_wrong_key_and_missing_folder(self):
        async def go():
            with Server():
                with self.assertRaises(M.MegaError) as cm:
                    await M.list_folder(folder_link(M.b64e(os.urandom(16))))
                self.assertEqual(cm.exception.kind, "key")
                with self.assertRaises(M.MegaError) as cm:
                    await M.list_folder(MegaLink("folder", "NOSUCH00", M.b64e(MASTER)))
                self.assertEqual(cm.exception.kind, "dead")
        run(go())


class Downloads(unittest.TestCase):
    def test_folder_file_roundtrip_with_progress(self):
        async def go():
            with Server(), tempfile.TemporaryDirectory() as tmp:
                _, nodes = await M.list_folder(folder_link())
                ep = next(n for n in nodes if n.path == "Season 1/ep1.mkv")
                seen = []
                path = await M.download(folder_link(), ep.handle, ep.key, Path(tmp), on_progress=seen.append)
                self.assertEqual(path.name, "ep1.mkv")
                self.assertEqual(path.read_bytes(), FAKE.big)
                self.assertEqual(seen[-1], len(FAKE.big))
                self.assertEqual(seen, sorted(seen))
                self.assertGreater(len(seen), 1)  # arrived in several chunks
        run(go())

    def test_key_is_looked_up_when_missing(self):
        async def go():
            with Server(), tempfile.TemporaryDirectory() as tmp:
                path = await M.download(folder_link(), "README01", None, Path(tmp))
                self.assertEqual(path.read_bytes(), b"hello mega")
        run(go())

    def test_file_link_info_and_download(self):
        async def go():
            with Server(), tempfile.TemporaryDirectory() as tmp:
                link = MegaLink("file", FAKE.file_id, M.b64e(FAKE.file_key))
                self.assertEqual(await M.file_info(link), ("single.bin", len(FAKE.file_content)))
                path = await M.download(link, FAKE.file_id, None, Path(tmp))
                self.assertEqual(path.read_bytes(), FAKE.file_content)
                with self.assertRaises(M.MegaError) as cm:  # right id, wrong key: the name cannot be decrypted
                    await M.file_info(MegaLink("file", FAKE.file_id, M.b64e(os.urandom(32))))
                self.assertEqual(cm.exception.kind, "key")
        run(go())

    def test_errors_map_to_kinds(self):
        async def go():
            with Server(), tempfile.TemporaryDirectory() as tmp:
                link, d = folder_link(), Path(tmp)
                k = M.b64e(os.urandom(32))
                for handle, kind in (("OVERQUOTA", "quota"), ("NOSUCH00", "dead"), ("CDN50900", "quota"), ("SHORT001", "network")):
                    key = M.b64e(FAKE.files[handle][0]) if handle in FAKE.files else k
                    with self.assertRaises(M.MegaError) as cm:
                        await M.download(link, handle, key, d)
                    self.assertEqual(cm.exception.kind, kind, handle)
                    self.assertEqual(list(d.iterdir()), [], "partial file must be removed (%s)" % handle)
        run(go())

    def test_abort_and_stall(self):
        async def go():
            with Server(), tempfile.TemporaryDirectory() as tmp:
                link, d = folder_link(), Path(tmp)
                key = M.b64e(FAKE.files["SLOW0001"][0])
                abort = asyncio.Event()
                asyncio.get_running_loop().call_later(0.7, abort.set)
                with self.assertRaises(M.MegaError) as cm:
                    await M.download(link, "SLOW0001", key, d, abort=abort, stall_timeout=30)
                self.assertEqual(cm.exception.kind, "cancelled")
                with self.assertRaises(M.MegaError) as cm:
                    await M.download(link, "SLOW0001", key, d, stall_timeout=1)
                self.assertEqual(cm.exception.kind, "network")
                self.assertIn("stalled", str(cm.exception))
        run(go())


class FrontEnd(unittest.TestCase):
    def test_fetch_routes_and_account_rule(self):
        async def go():
            with Server(), tempfile.TemporaryDirectory() as tmp:
                tools = MegaTools(Path(tmp) / "t")
                path = await tools.fetch(folder_link(), "README01", None, Path(tmp) / "d", Creds())
                self.assertEqual(path.read_bytes(), b"hello mega")
                with self.assertRaises(M.MegaError) as cm:  # accounts cannot fetch files inside folder links
                    await tools.fetch(folder_link(), "README01", None, Path(tmp) / "d2", Creds("a@b.co", "pw"))
                self.assertEqual(cm.exception.kind, "unsupported")
                root, nodes = await tools.list_folder(folder_link(), Creds())
                self.assertEqual(root, "Shared stuff")
        run(go())

    def test_router_anon_only(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp)
                await app.accounts.add("me@example.com", "pw")
                self.assertEqual({k[0] for k in await app.router.candidates()}, {None, 1})
                self.assertEqual({k[0] for k in await app.router.candidates(anon_only=True)}, {None})
                await app.db.execute("UPDATE accounts SET cooldown_until=?", (time.time() + 999,))
                self.assertIsNone(await app.router.next_ready_eta(anon_only=True))  # nothing anonymous is cooling down
                self.assertGreater(await app.router.next_ready_eta(), 900)          # but the account is, for other jobs
                await app.db.set_setting("direct_cd", time.time() + 50)
                self.assertTrue(40 < await app.router.next_ready_eta(anon_only=True) < 60)
        run(go())


if __name__ == "__main__":
    unittest.main()
