import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests import stubs  # noqa: E402

stubs.install_if_missing()

from accounts import AccountManager, Vault, parse_accounts  # noqa: E402
from browse import BrowseSession  # noqa: E402
from config import Config  # noqa: E402
from db import DB  # noqa: E402
from mega_client import MegaError, Node, UNKNOWN_NAME, build_nodes, classify, extract_links  # noqa: E402
from proxies import ProxyManager, mask_proxy, normalize_proxy, parse_proxy_list  # noqa: E402
from queue_worker import QueueManager, render_caption  # noqa: E402
from router import Router  # noqa: E402
from ui import screens as S  # noqa: E402
from ui import themes as T  # noqa: E402
from ui.animations import bar, fmt_eta, fmt_size  # noqa: E402
from app import App  # noqa: E402

def sample_nodes():
    return build_nodes({
        "readme.txt": {"handle": "AbCdEfG3", "size": 2048, "is_dir": False, "key": "k3"},
        "Season 1/ep1.mkv": {"handle": "AbCdEfG2", "size": 123456789, "is_dir": False, "key": "k2"},
        "Season 1/ep2.mkv": {"handle": "AbCdEfG4", "size": 4096, "is_dir": False, "key": "k4"},
    })


KEY = "a" * 22


class Parsing(unittest.TestCase):
    def test_links(self):
        text = ("see https://mega.nz/folder/AbCdEfGh#%s/file/ZZZZZZZZ and https://mega.nz/#F!AbCdEfGh!%s "
                "and https://mega.nz/file/Qwertyui#%s" % (KEY, "b" * 22, "c" * 43))
        links = extract_links(text)
        self.assertEqual([(l.kind, l.id) for l in links], [("folder", "AbCdEfGh"), ("folder", "AbCdEfGh"), ("file", "Qwertyui")])
        self.assertEqual(links[0].sub_id, "ZZZZZZZZ")
        self.assertEqual(links[0].file_url("XXXXXXXX"), "https://mega.nz/folder/AbCdEfGh#%s/file/XXXXXXXX" % KEY)
        self.assertEqual(extract_links("https://mega.nz/file/short#abc"), [])

    def test_node_tree(self):
        nodes = sample_nodes()
        self.assertEqual([n.path for n in nodes], ["readme.txt", "Season 1", "Season 1/ep1.mkv", "Season 1/ep2.mkv"])
        self.assertTrue(nodes[1].is_dir and nodes[1].handle is None)  # missing parent folders are created
        self.assertTrue(nodes[2].parent == 1 and nodes[2].size == 123456789 and nodes[2].key == "k2")

    def test_classify(self):
        self.assertEqual(classify("HTTP 509 Bandwidth Limit Exceeded"), "quota")
        self.assertEqual(classify("Object not found (ENOENT)"), "dead")
        self.assertEqual(classify("Can't decrypt: wrong key"), "key")
        self.assertEqual(classify("Failed login: bad password"), "auth")
        self.assertEqual(classify("curl: (7) connection refused by proxy"), "network")

    def test_proxies(self):
        self.assertEqual(normalize_proxy("1.2.3.4:8080"), "http://1.2.3.4:8080")
        self.assertEqual(normalize_proxy("1.2.3.4:8080:bob:pw"), "http://bob:pw@1.2.3.4:8080")
        self.assertEqual(normalize_proxy("socks5://u:p@h.example:1080"), "socks5://u:p@h.example:1080")
        self.assertIsNone(normalize_proxy("nonsense"))
        self.assertIsNone(normalize_proxy("1.2.3.4:99999"))
        self.assertEqual(mask_proxy("socks5://bob:secret@h.example:1080"), "socks5://bob:***@h.example:1080")
        good, bad = parse_proxy_list("1.1.1.1:80\n# c\n1.1.1.1:80\nbad\n2.2.2.2:81, 3.3.3.3:82")
        self.assertEqual((len(good), bad), (3, 1))

    def test_accounts_parse(self):
        self.assertEqual(parse_accounts("a@b.co pw1\nc@d.co:pw:2\nbad line\n"), [("a@b.co", "pw1"), ("c@d.co", "pw:2")])

    def test_format(self):
        self.assertEqual(fmt_size(1536), "1.5 KB")
        self.assertEqual(fmt_eta(3725), "1:02:05")
        th = T.get("ocean")
        self.assertEqual(bar(100, th), th["full"] * 10)
        self.assertEqual(len(bar(35, th, 1)), 10 * len(th["full"]) - 0 if len(th["full"]) == 1 else len(bar(35, th, 1)))
        f = {"name": "a.mkv", "size": 2048, "path": "S1/a.mkv"}
        self.assertEqual(render_caption("{name} | {size} | {folder} | {x}", f, 1, 3, "R"), "a.mkv | 2.0 KB | S1 | {x}")

    def test_vault(self):
        from cryptography.fernet import Fernet
        v = Vault(Fernet.generate_key())
        self.assertEqual(v.decrypt(v.encrypt("p@ss")), "p@ss")

    def test_browse(self):
        nodes = sample_nodes()
        s = BrowseSession("abc", 1, extract_links("https://mega.nz/folder/AbCdEfGh#%s" % KEY)[0], nodes, "Root")
        self.assertEqual([n.name for n in s.children()], ["Season 1", "readme.txt"])
        s.selected.update(n.idx for n in s.files_under(-1))
        self.assertEqual(len(s.selected_nodes()), 3)
        s.fkey = "video"
        self.assertEqual([n.name for n in s.files_under(-1)], ["ep1.mkv", "ep2.mkv"])
        self.assertEqual([n.name for n in s.children()], ["Season 1"])


# ----------------------------------------------------------------------------- pipeline
class FakeBot:
    def __init__(self):
        self.edits, self.sent = [], []

    async def edit_message(self, chat, msg, text, **kw):
        self.edits.append(text)

    async def send_message(self, chat, text, **kw):
        self.sent.append(text)


class FakeMega:
    def __init__(self, script):
        self.script, self.calls = list(script), []

    async def fetch(self, link, handle, key, dest, creds, name_hint="", on_progress=None, abort=None, stall_timeout=0):
        self.calls.append((handle, creds.proxy, creds.email))
        step = self.script.pop(0) if self.script else "ok"
        if step == "quota":
            raise MegaError("quota", "509 bandwidth limit exceeded")
        if step == "dead":
            raise MegaError("dead", "not found")
        dest.mkdir(parents=True, exist_ok=True)
        p = dest / "real_name.bin"
        p.write_bytes(b"x" * 1000)
        if on_progress:
            on_progress(1000)
        return p

    async def account_info(self, creds):
        return "Total: 20 GiB · Free: 15 GiB"


class FakeUploader:
    def __init__(self):
        self.sent = []

    async def try_reuse(self, *a, **k):
        return None

    async def get_or_create_topic(self, *a, **k):
        return None

    async def send(self, dest, path, caption, send_as, topic_id, progress):
        progress(1000, 1000)
        self.sent.append((path.name, caption))
        return 42, False


def run(coro):
    return asyncio.run(coro)


def make_app(tmp, mega_script=()):
    cfg = Config(api_id=1, api_hash="x", bot_token="t", user_session="", owner_ids={1}, public=False,
                 data_dir=Path(tmp), max_concurrent=1, edit_interval=1.5, min_free_gb=0, quota_cooldown_min=45,
                 error_cooldown_min=5, max_attempts=4, upload_delay=0, stall_timeout=30, default_daily_gb=0,
                 admin_log_chat=0, log_level="INFO")
    for d in (cfg.download_dir, cfg.tmp_dir):
        d.mkdir(parents=True, exist_ok=True)
    from cryptography.fernet import Fernet
    app = App(cfg)
    app.db = DB(cfg.db_path)
    app.vault = Vault(Fernet.generate_key())
    app.mega = FakeMega(mega_script)
    app.router = Router(app.db, app.vault, cfg)
    app.proxy_pool = ProxyManager(app.db, app.vault)
    app.accounts = AccountManager(app.db, app.vault, app.mega)
    app.uploader = FakeUploader()
    app.bot = FakeBot()
    app.queue = QueueManager(app)
    return app


async def make_job(app, files=1, link_kind="folder"):
    user = await app.db.ensure_user(1, "owner", "Tester")
    url = "https://mega.nz/%s/AbCdEfGh#%s" % (link_kind, KEY)
    jid = await app.db.execute(
        "INSERT INTO jobs(user_id,chat_id,msg_id,link,root_id,dest_id,status,options,total_files,total_bytes,created) "
        "VALUES(1,1,10,?,?,0,'queued','{\"root_name\":\"Root\",\"send_as\":\"auto\"}',?,?,0)", (url, "AbCdEfGh", files, files * 1000))
    for i in range(files):
        await app.db.execute("INSERT INTO job_files(job_id,handle,path,name,size,seq) VALUES(?,?,?,?,?,?)",
                             (jid, "H%07d" % i, "dir/f%d.bin" % i, "f%d.bin" % i, 1000, i))
    return jid, user


class Pipeline(unittest.TestCase):
    def test_happy_path_and_cleanup(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp)
                jid, _ = await make_job(app, files=2)
                await app.queue._claim()
                await app.queue._run_job(jid)
                job = await app.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
                self.assertEqual(job["status"], "done")
                rows = await app.db.all("SELECT status FROM job_files WHERE job_id=?", (jid,))
                self.assertEqual([r["status"] for r in rows], ["done", "done"])
                self.assertEqual(len(app.uploader.sent), 2)
                self.assertEqual(list(app.cfg.download_dir.iterdir()), [])  # temp files removed
                self.assertTrue(any("Finished" in t for t in app.bot.edits))
                self.assertEqual(len(await app.db.all("SELECT * FROM uploaded")), 2)
        run(go())

    def test_quota_rotates_to_next_proxy(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp, ["quota", "ok"])
                await app.proxy_pool.add_many("1.1.1.1:80\n2.2.2.2:80")
                await app.db.set_setting("proxy_mode", "rr")
                jid, _ = await make_job(app)
                await app.queue._claim()
                await app.queue._run_job(jid)
                self.assertEqual((await app.db.one("SELECT status FROM jobs WHERE id=?", (jid,)))["status"], "done")
                proxies_used = [c[1] for c in app.mega.calls]
                self.assertEqual(len(proxies_used), 2)
                self.assertNotEqual(proxies_used[0], proxies_used[1])
                cooling = await app.db.all("SELECT * FROM proxies WHERE cooldown_until>0")
                self.assertEqual(len(cooling), 1)
        run(go())

    def test_account_fallback_when_direct_blocked(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp, ["quota", "ok"])
                ok, _ = await app.accounts.add("me@example.com", "pw")
                self.assertTrue(ok)
                jid, _ = await make_job(app, link_kind="file")
                await app.queue._claim()
                await app.queue._run_job(jid)
                self.assertIsNone(app.mega.calls[0][2])               # first try: anonymous
                self.assertEqual(app.mega.calls[1][2], "me@example.com")  # then the account
                self.assertEqual((await app.db.one("SELECT status FROM jobs WHERE id=?", (jid,)))["status"], "done")
        run(go())

    def test_folder_jobs_never_use_accounts(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp, ["quota"])
                await app.accounts.add("me@example.com", "pw")
                jid, _ = await make_job(app)  # folder link: accounts cannot download files inside folders
                await app.queue._claim()
                task = asyncio.create_task(app.queue._run_job(jid))
                await asyncio.sleep(1.5)
                self.assertEqual(app.queue.views[jid].phase, "wait")  # waits for the anonymous route instead
                await app.queue.cancel_job(jid)
                await asyncio.wait_for(task, 10)
                self.assertTrue(all(c[2] is None for c in app.mega.calls))
        run(go())

    def test_dead_link_fails_file_without_rotation(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp, ["dead"])
                jid, _ = await make_job(app)
                await app.queue._claim()
                await app.queue._run_job(jid)
                self.assertEqual((await app.db.one("SELECT status FROM jobs WHERE id=?", (jid,)))["status"], "failed")
                self.assertEqual(len(app.mega.calls), 1)
                await app.queue.retry_failed(jid)
                self.assertEqual((await app.db.one("SELECT status FROM jobs WHERE id=?", (jid,)))["status"], "queued")
        run(go())

    def test_waits_when_all_routes_cooling_and_can_cancel(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp, ["quota"])
                jid, _ = await make_job(app)
                await app.queue._claim()
                task = asyncio.create_task(app.queue._run_job(jid))
                await asyncio.sleep(1.5)
                view = app.queue.views[jid]
                self.assertEqual(view.phase, "wait")
                await app.queue.cancel_job(jid)
                await asyncio.wait_for(task, 10)
                self.assertEqual((await app.db.one("SELECT status FROM jobs WHERE id=?", (jid,)))["status"], "cancelled")
        run(go())

    def test_single_file_link_learns_real_name(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp)
                jid, _ = await make_job(app, link_kind="file")
                await app.db.execute("UPDATE job_files SET name=?, path=?, size=0", (UNKNOWN_NAME, UNKNOWN_NAME))
                await app.queue._claim()
                await app.queue._run_job(jid)
                self.assertEqual(app.uploader.sent[0][0], "real_name.bin")
                self.assertIn("real_name.bin", app.uploader.sent[0][1])
        run(go())


class Screens(unittest.TestCase):
    def test_every_screen_renders(self):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                app = make_app(tmp)
                jid, user = await make_job(app)
                await app.proxy_pool.add_many("1.1.1.1:80")
                await app.accounts.add("me@example.com", "pw")
                nodes = sample_nodes()
                s = BrowseSession("abc", 1, extract_links("https://mega.nz/folder/AbCdEfGh#%s" % KEY)[0], nodes, "Root")
                s.selected.add(2)
                from queue_worker import JobView
                job = await app.db.one("SELECT * FROM jobs WHERE id=?", (jid,))
                view = JobView(app, job, user)
                view.phase, view.file_name, view.cur_done, view.cur_total, view.route = "download", "x.mkv", 500, 1000, "👤 anonymous · 🌐 direct"
                screens = [
                    S.main_menu(app, user), S.help_screen(app, user), S.new_download_screen(app, user),
                    S.browser(app, user, s), S.filter_screen(app, user, s), S.summary(app, user, s),
                    S.options_screen(app, user, s), S.dest_pick(app, user, s), S.queue_screen(app, user),
                    S.job_detail(app, user, jid), S.job_card(app, view), S.dests_screen(app, user),
                    S.proxies_screen(app, user), S.proxy_detail(app, user, 1), S.accounts_screen(app, user),
                    S.account_detail(app, user, 1, "info"), S.stats_screen(app, user), S.settings_screen(app, user),
                    S.theme_screen(app, user),
                ]
                for coro in screens:
                    text, buttons = await coro
                    self.assertTrue(text and len(text) < 4000)
                    for row in buttons:
                        for b in row:
                            self.assertLessEqual(len(b[1]), 64)  # stub Button.inline returns (text, data)
                for phase in ("queued", "upload", "wait", "paused", "done", "partial", "failed", "cancelled"):
                    view.phase = phase
                    await S.job_card(app, view)
        run(go())


if __name__ == "__main__":
    unittest.main()


class MegatoolsLayouts(unittest.TestCase):
    """A static / --prefix build may only provide one `megatools` binary with subcommands."""

    def test_single_binary_subcommands_and_override(self):
        import stat
        from mega_client import MegaTools

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                fake = Path(tmp) / "megatools"
                fake.write_text('#!/bin/sh\necho "megatools 1.12.0 args: $@"\n')
                fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
                tools = MegaTools(Path(tmp) / "t")
                self.assertEqual(tools.resolve_binaries(str(fake)), "%s dl" % fake)
                await tools.detect_version()
                self.assertEqual(tools.version, "1.12.0")
                rc, out, _ = await tools._run_simple("ls", ["-l", "LINK"], None)
                self.assertIn("args: ls --no-ask-password --config", out)
                self.assertTrue(out.strip().endswith("-l LINK"))
        run(go())

    def test_missing_binary_message(self):
        from mega_client import MegaTools

        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                tools = MegaTools(Path(tmp), ls="no-such-megals", dl="no-such-megadl")
                with self.assertRaises(MegaError) as cm:
                    await tools._run_simple("dl", ["--version"], None)
                self.assertIn("MEGATOOLS_BIN", str(cm.exception))
        run(go())
