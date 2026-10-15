import asyncio
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import stubs  # noqa: E402

stubs.install_if_missing()

import proxies as P  # noqa: E402
from db import DB  # noqa: E402
from tests.test_core import make_app, run  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        port = self.server.server_address[1]
        path = urlsplit(self.path).path  # a proxy receives absolute URIs
        if path == "/plain":
            body, ctype = "# list\n1.1.1.1:80 US 120ms\nsocks5://u:p@2.2.2.2:1080\n3.3.3.3:81:bob:pw\nnot a proxy\n", "text/plain"
        elif path == "/json1":
            body = json.dumps({"count": 3, "next": "http://127.0.0.1:%d/json2" % port, "results": [
                {"proxy_address": "10.0.0.1", "port": 8000, "username": "a b", "password": "p@ss", "valid": True},
                {"proxy_address": "10.0.0.2", "port": 8001, "username": "x", "password": "y", "valid": False}]})
            ctype = "application/json"
        elif path == "/json2":
            body = json.dumps({"next": None, "results": [
                {"ip": "10.0.0.3", "port": "8002", "protocol": "socks5", "anonymity": "elite"}]})
            ctype = "application/json"
        elif path == "/auth":
            if self.headers.get("Authorization") != "Token secret":
                self.send_response(401)
                self.end_headers()
                return
            body, ctype = json.dumps({"data": ["4.4.4.4:3128", "5.5.5.5:3128"]}), "application/json"
        elif path == "/ipecho":
            body, ctype = "203.0.113.7", "text/plain"
        else:
            self.send_response(404)
            self.end_headers()
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class Server:
    def __enter__(self):
        self.srv = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()

    def url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)


class Extraction(unittest.TestCase):
    def test_plain_text_with_noise(self):
        raws, nxt = P.extract_raw("# list\n1.1.1.1:80 US 120ms\nsocks5://u:p@2.2.2.2:1080\n3.3.3.3:81:bob:pw\nnot a proxy\n")
        self.assertEqual(raws, ["1.1.1.1:80", "socks5://u:p@2.2.2.2:1080", "3.3.3.3:81:bob:pw"])
        self.assertIsNone(nxt)

    def test_json_variants(self):
        data = {"next": "https://x/y", "results": [
            {"proxy_address": "10.0.0.1", "port": 8000, "username": "a b", "password": "p@ss"},
            {"proxy_address": "10.0.0.2", "port": 8001, "valid": False},
            {"ip": "10.0.0.3", "port": "8002", "protocol": "socks5", "anonymity": "elite"},
            {"proxy": "10.0.0.4:90"}], "meta": {"country": "US"}}
        raws, nxt = P.extract_raw(json.dumps(data))
        self.assertEqual(len(raws), 3)  # the valid:false entry and the metadata are ignored
        self.assertEqual(raws[0], "a%20b:p%40ss@10.0.0.1:8000")  # credentials are URL-encoded
        self.assertEqual(raws[1:], ["socks5://10.0.0.3:8002", "10.0.0.4:90"])
        self.assertEqual(nxt, "https://x/y")
        raws2, _ = P.extract_raw(json.dumps({"proxies": "1.1.1.1:80\n2.2.2.2:81"}))
        self.assertEqual(raws2, ["1.1.1.1:80", "2.2.2.2:81"])

    def test_entries_and_schemes(self):
        e, bad = P.parse_proxy_entries("1.1.1.1:80\nsocks5://2.2.2.2:1\nbad", "auto")
        self.assertEqual([(x.url, x.detect) for x in e], [("http://1.1.1.1:80", True), ("socks5://2.2.2.2:1", False)])
        self.assertEqual(bad, 1)
        e2, _ = P.parse_proxy_entries("1.1.1.1:80", "socks5")
        self.assertEqual([(x.url, x.detect) for x in e2], [("socks5://1.1.1.1:80", False)])
        self.assertEqual(P.scheme_variants("http://u:p@h:1"), ["http://u:p@h:1", "socks5://u:p@h:1", "socks4://u:p@h:1"])
        self.assertEqual(P.mask_url("https://api.example.com/v2/list/?key=SECRET"), "https://api.example.com/v2/list/?…")


class Fetching(unittest.TestCase):
    def test_fetch_plain_json_pagination_and_auth(self):
        async def go():
            with Server() as s:
                ents, bad = await P.fetch_proxies(s.url("/plain"), {}, "auto")
                self.assertEqual([e.url for e in ents], ["http://1.1.1.1:80", "socks5://u:p@2.2.2.2:1080", "http://bob:pw@3.3.3.3:81"])
                ents, _ = await P.fetch_proxies(s.url("/json1"), {}, "http")
                urls = [e.url for e in ents]
                self.assertEqual(len(urls), 2)  # the valid:false entry is skipped, page 2 is followed
                self.assertTrue(urls[0].endswith("@10.0.0.1:8000") and urls[1] == "socks5://10.0.0.3:8002")
                with self.assertRaises(P.SourceError) as cm:
                    await P.fetch_proxies(s.url("/auth"), {}, "auto")
                self.assertIn("unauthorised", str(cm.exception))
                ents, _ = await P.fetch_proxies(s.url("/auth"), {"Authorization": "Token secret"}, "auto")
                self.assertEqual(len(ents), 2)
                with self.assertRaises(P.SourceError):
                    await P.fetch_proxies(s.url("/missing"), {}, "auto")
                with self.assertRaises(P.SourceError):
                    await P.fetch_proxies("file:///etc/passwd", {}, "auto")
        run(go())


class CurlAndChecker(unittest.TestCase):
    def test_real_curl_through_fake_proxy(self):
        async def go():
            with Server() as s:  # the "proxy" simply answers every request
                code, secs, body, err = await P._curl(s.url("/ipecho"), "http://127.0.0.1:%d" % s.port, 10)
                self.assertEqual((code, body.strip()), ("200", "203.0.113.7"))
                self.assertGreaterEqual(secs, 0)
                code, _s, _b, err = await P._curl("http://127.0.0.1:9/", "http://127.0.0.1:1", 5)
                self.assertEqual(code, "000")
                self.assertTrue(err)
        run(go())

    def test_leak_detection_and_results(self):
        async def go():
            calls = {P.TEST_URL: ("200", 0.25, "", ""), P.IP_URL: ("200", 0.1, "9.9.9.9", "")}

            async def fake_curl(target, proxy=None, timeout=20):
                return calls[target]

            with mock.patch.object(P, "_curl", fake_curl):
                ok = await P.test_proxy("http://h:1", own_ip="1.2.3.4")
                self.assertTrue(ok.ok and ok.exit_ip == "9.9.9.9" and ok.latency == 0.25)
                leak = await P.test_proxy("http://h:1", own_ip="9.9.9.9")
                self.assertFalse(leak.ok)
                self.assertIn("does not hide", leak.error)
                calls[P.TEST_URL] = ("000", 0.0, "", "Connection refused")
                bad = await P.test_proxy("http://h:1")
                self.assertFalse(bad.ok)
                self.assertIn("refused", bad.error)
        run(go())


class PoolAndSources(unittest.TestCase):
    def fakes(self, working_scheme="http", dead_hosts=()):
        async def fake_test(url, timeout=20, own_ip=""):
            host = P.proxy_key(url)[0]
            ok = url.startswith(working_scheme + "://") and host not in dead_hosts
            return P.ProbeResult(url, ok, 0.3 if ok else None, "9.9.9.9" if ok else "", "" if ok else "refused")

        async def fake_ip():
            return "1.2.3.4"

        return mock.patch.object(P, "test_proxy", fake_test), mock.patch.object(P, "get_own_ip", fake_ip)

    def test_autodetect_scheme(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp)
                a, b = self.fakes("socks5")
                with a, b:
                    new, dupes, bad = await app.proxy_pool.add_many("1.1.1.1:80\n2.2.2.2:81\n1.1.1.1:80")
                    self.assertEqual((len(new), dupes, bad), (2, 0, 0))  # duplicates inside one paste are merged
                    await app.proxy_pool.verify(new)
                    rows = await app.proxy_pool.list()
                    self.assertTrue(all(r["url"].startswith("socks5://") and r["status"] == "ok" and r["exit_ip"] == "9.9.9.9" for r in rows))
        run(go())

    def test_checker_does_not_touch_pool_and_add_working(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp)
                a, b = self.fakes("http", dead_hosts={"3.3.3.3"})
                with a, b:
                    entries, _ = P.parse_proxy_entries("1.1.1.1:80\n2.2.2.2:81\n3.3.3.3:82", "auto")
                    seen = []

                    async def cb(res):
                        seen.append(res.ok)

                    results = await app.proxy_pool.check_entries(entries, 3, cb)
                    self.assertEqual(sorted(seen), [False, True, True])
                    self.assertEqual(await app.proxy_pool.list(), [])
                    added, dupes = await app.proxy_pool.add_checked(results)
                    self.assertEqual((added, dupes), (2, 0))
                    rows = await app.proxy_pool.list()
                    self.assertTrue(all(r["status"] == "ok" and r["latency"] == 0.3 for r in rows))
        run(go())

    def test_source_refresh_sync_and_secret_headers(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp, Server() as s:
                app = make_app(tmp)
                a, b = self.fakes("socks5")
                with a, b:
                    sid = await app.proxy_pool.add_source(s.url("/auth"), {"Authorization": "Token secret"})
                    src = await app.proxy_pool.source(sid)
                    self.assertNotIn(b"secret", bytes(src["headers_enc"]))  # stored encrypted
                    res = await app.proxy_pool.refresh_source(sid, wait=True)
                    self.assertEqual((res["fetched"], res["added"], res["removed"]), (2, 2, 0))
                    rows = await app.proxy_pool.list()
                    self.assertTrue(all(r["source_id"] == sid and r["url"].startswith("socks5://") for r in rows))
                    # the provider now returns a different list -> old ones disappear, new ones appear
                    srv_list = ["4.4.4.4:3128", "6.6.6.6:3128"]

                    async def fake_fetch(url, headers, scheme, **k):
                        return P.parse_proxy_entries("\n".join(srv_list), scheme)[0], 0

                    with mock.patch.object(P, "fetch_proxies", fake_fetch):
                        res = await app.proxy_pool.refresh_source(sid, wait=True)
                    self.assertEqual((res["added"], res["removed"], res["dupes"]), (1, 1, 1))
                    hosts = sorted(P.proxy_key(r["url"])[0] for r in await app.proxy_pool.list())
                    self.assertEqual(hosts, ["4.4.4.4", "6.6.6.6"])
                    # a failing fetch must never wipe the pool
                    res = await app.proxy_pool.refresh_source(sid, wait=True)
                    self.assertEqual(res.get("fetched"), 2)
                    await app.db.execute("UPDATE proxy_sources SET url=? WHERE id=?", (s.url("/missing"), sid))
                    res = await app.proxy_pool.refresh_source(sid, wait=True)
                    self.assertIn("error", res)
                    self.assertEqual(len(await app.proxy_pool.list()), 2)
                    await app.proxy_pool.delete_source(sid, delete_proxies=True)
                    self.assertEqual(await app.proxy_pool.list(), [])
        run(go())

    def test_db_migration_adds_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "old.db"
            con = sqlite3.connect(path)
            con.execute("CREATE TABLE proxies(id INTEGER PRIMARY KEY AUTOINCREMENT, url TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'ok', "
                        "fails INTEGER NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0, "
                        "bytes_total INTEGER NOT NULL DEFAULT 0, latency REAL, last_error TEXT)")
            con.execute("INSERT INTO proxies(url) VALUES('http://1.1.1.1:80')")
            con.commit()
            con.close()
            db = DB(path)
            rows = run(db.all("SELECT url, exit_ip, checked_at, source_id FROM proxies"))
            self.assertEqual(rows, [{"url": "http://1.1.1.1:80", "exit_ip": None, "checked_at": None, "source_id": None}])


class UiAndHelpers(unittest.TestCase):
    def test_source_screens_hide_secrets(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                from ui import screens as S
                app = make_app(tmp)
                user = await app.db.ensure_user(1, "owner", "T")
                sid = await app.proxy_pool.add_source("https://api.example.com/v2/list/?key=SECRETKEY", {"Authorization": "Token TOPSECRET"})
                await app.proxy_pool.add_many("1.1.1.1:80")
                await app.db.execute("UPDATE proxies SET exit_ip='9.9.9.9', checked_at=?, latency=0.4, source_id=?", (__import__("time").time(), sid))
                for coro in (S.proxies_screen(app, user), S.proxy_detail(app, user, 1), S.sources_screen(app, user),
                             S.source_detail(app, user, sid, "note"), S.source_delete_screen(app, user, sid),
                             S.settings_screen(app, user)):
                    text, buttons = await coro
                    self.assertTrue(text and len(text) < 4000)
                    for row in buttons:
                        for b in row:
                            self.assertLessEqual(len(b[1]), 64)
                    self.assertNotIn("SECRETKEY", text)
                    self.assertNotIn("TOPSECRET", text)
                text, _ = await S.source_detail(app, user, sid)
                self.assertIn("Authorization", text)  # header *names* are shown, values are not
                self.assertIn("9.9.9.9", (await S.proxy_detail(app, user, 1))[0])
        run(go())

    def test_parse_source_input_and_check_format(self):
        import handlers as H
        url, headers = H.parse_source_input("https://api.example.com/list?x=1\nAuthorization: Token abc\nX-Api-Key: k:v\nrandom text")
        self.assertEqual(url, "https://api.example.com/list?x=1")
        self.assertEqual(headers, {"Authorization": "Token abc", "X-Api-Key": "k:v"})
        self.assertEqual(H.parse_source_input("no url here")[0], None)
        results = [P.ProbeResult("http://1.1.1.1:80", True, 0.31, "9.9.9.9"),
                   P.ProbeResult("socks5://u:p@2.2.2.2:1", True, 0.12, ""),
                   P.ProbeResult("http://3.3.3.3:80", False, None, "", "Connection refused"),
                   P.ProbeResult("http://4.4.4.4:80", False, None, "", "Connection refused")]
        text = H.format_check_result(results, 1, False)
        self.assertIn("<b>2</b> working", text)
        self.assertIn("2× Connection refused", text)
        self.assertLess(text.index("2.2.2.2"), text.index("1.1.1.1"))  # fastest first
        self.assertNotIn(":p@", text)  # passwords masked


if __name__ == "__main__":
    unittest.main()
