"""Every screen of the bot. Each function returns (html_text, buttons) ready for edit/send."""
from __future__ import annotations

import html
import json
import shutil
import time

from accounts import account_state, mask_email
from browse import FILTERS, file_icon
from proxies import mask_proxy, mask_url, proxy_state
from router import ACCOUNT_MODES, PROXY_MODES
from ui import themes as T
from ui.animations import bar, fmt_eta, fmt_size, fmt_speed, short, spinner
from ui.keyboards import B, footer, grid, pager

esc = html.escape
STATE_ICON = {"ok": "🟢", "cooldown": "🟠", "dead": "🔴", "bad": "🔴"}
JOB_ICON = {"queued": "🟡", "running": "🟢", "paused": "⏸", "done": "✅", "partial": "⚠️", "failed": "❌", "cancelled": "🚫"}
SEND_AS = {"auto": "Auto (video / photo / file)", "document": "Always as file"}


def head(th: dict, title: str) -> str:
    return "%s <b>%s</b>\n%s" % (th["icon"], esc(title), T.DIVIDER)


def is_admin(user: dict) -> bool:
    return user["role"] in ("owner", "admin")


def onoff(v) -> str:
    return "ON ✅" if v else "OFF ❌"


def mode_label(modes, key: str) -> str:
    return dict(modes).get(key, key)


def effective_options(user: dict, s, dest: dict) -> dict:
    """Session overrides on top of the user's defaults."""
    return {
        "send_as": s.options.get("send_as", user["send_as"]),
        "topic_per_folder": int(s.options.get("topic_per_folder", dest.get("topic_per_folder", 0))),
        "caption": user["caption"],
    }


# --------------------------------------------------------------------------- menu
async def main_menu(app, user):
    th, db = T.get(user["theme"]), app.db
    now, admin = time.time(), is_admin(user)
    where, args = ("", ()) if admin else (" AND user_id=?", (user["id"],))
    running = (await db.one("SELECT COUNT(*) c FROM jobs WHERE status IN ('running','paused')" + where, args))["c"]
    waiting = (await db.one("SELECT COUNT(*) c FROM jobs WHERE status='queued'" + where, args))["c"]
    px = await db.all("SELECT status, cooldown_until FROM proxies")
    ac = await db.all("SELECT status, cooldown_until FROM accounts")
    px_ok = sum(1 for p in px if p["status"] != "dead" and p["cooldown_until"] <= now)
    ac_ok = sum(1 for a in ac if a["status"] != "bad" and a["cooldown_until"] <= now)
    dest = await app.get_dest(user)
    text = "\n".join([
        head(th, "MEGA → Telegram"),
        "🟢 Queue: <b>%d</b> running · <b>%d</b> waiting" % (running, waiting),
        "🌐 Proxies: %d/%d ready" % (px_ok, len(px)),
        "👤 Accounts: %d/%d ready" % (ac_ok, len(ac)),
        "📡 Destination: <b>%s</b>" % esc(dest["name"]),
        "🎨 Theme: %s" % th["label"],
        "",
        "Send me a <b>mega.nz link</b> to begin 👇",
    ])
    rows = [[B("🔗 New download", "m:new"), B("📋 Queue", "m:q")],
            [B("📡 Destinations", "m:d"), B("📊 Stats", "m:s")]]
    if admin:
        rows.append([B("🌐 Proxies", "m:p"), B("👤 Accounts", "m:a")])
    rows += [[B("🎨 Theme", "m:th"), B("⚙ Settings", "m:set")], [B("❓ Help", "m:help")]]
    return text, rows


async def new_download_screen(app, user):
    th = T.get(user["theme"])
    text = "\n".join([
        head(th, "New download"),
        "Paste a public <b>mega.nz</b> file or folder link (several links or a .txt file also work).",
        "",
        "• Folder links open a browser: pick folders, tick files or take everything.",
        "• Files are downloaded and uploaded <b>one by one</b>.",
    ])
    return text, footer()


async def help_screen(app, user):
    th = T.get(user["theme"])
    text = "\n".join([
        head(th, "Help"),
        "<b>Basics</b>",
        "Send a mega.nz link → browse → ☑ select → review → 🟢 start.",
        "",
        "<b>Commands</b>",
        "/menu – main menu · /queue – jobs · /stats – statistics",
        "/theme – colours · /cancel – cancel a pending input",
        "/addproxy &lt;list&gt; · /checkproxy &lt;list&gt; · /addproxyapi &lt;url&gt; (admins)",
        "/addaccount email password (admins)",
        "/allow &lt;id&gt; · /deny &lt;id&gt; · /users (admins)",
        "",
        "<b>Caption fields</b>",
        "{name} {size} {folder} {path} {ext} {index} {total} {root}",
    ])
    return text, footer()


async def prompt_screen(app, user, text: str, back: str = "m:home"):
    th = T.get(user["theme"])
    return "%s\n%s" % (head(th, "Input needed"), text), [[B("✖ Cancel", back)]]


async def confirm_screen(app, user, text: str, yes: str, no: str):
    th = T.get(user["theme"])
    return "%s\n%s" % (head(th, "Are you sure?"), text), [[B("✅ Yes", yes), B("✖ No", no)]]


async def error_screen(app, user, text: str, back: str = "m:home"):
    th = T.get(user["theme"])
    return "%s\n🔴 %s" % (head(th, "Problem"), text), footer(back if back != "m:home" else "")


# --------------------------------------------------------------------------- browser
async def browser(app, user, s):
    th = T.get(user["theme"])
    items = s.page_items()
    sel_n = len(s.selected_nodes())
    flt = FILTERS[s.fkey][0] + (' · "%s"' % esc(s.name_filter) if s.name_filter else "")
    lines = [head(th, short(s.title, 40)),
             "📍 %s" % esc(s.breadcrumb()),
             "☑ Selected: <b>%d</b> files · %s" % (sel_n, fmt_size(s.selected_size())),
             "🔍 Filter: %s" % flt]
    if not items:
        lines.append("\n<i>Nothing to show here.</i>")
    rows = []
    for n in items:
        if n.is_dir:
            rows.append([B("📁 %s · %s" % (short(n.name, 24), s.dir_summary(n.idx)), "b:open:%s:%d" % (s.sid, n.idx))])
        else:
            mark = "☑" if n.idx in s.selected else "☐"
            size = (" · " + fmt_size(n.size)) if n.size else ""
            rows.append([B("%s %s %s%s" % (mark, file_icon(n.name), short(n.name, 26), size), "b:tog:%s:%d" % (s.sid, n.idx))])
    rows += pager("b:pg:%s" % s.sid, s.page, s.pages())
    rows.append([B("☑ Select all here", "b:all:%s" % s.sid), B("🧹 Clear", "b:clr:%s" % s.sid)])
    nav = [B("🔍 Filter", "b:flt:%s" % s.sid)]
    if s.cwd != -1:
        nav.insert(0, B("⬆ Up", "b:up:%s" % s.sid))
    rows.append(nav)
    rows.append([B("📊 Review & start (%d)" % sel_n, "b:sum:%s" % s.sid)])
    rows.append([B("🏠 Menu", "m:home"), B("❌ Close", "b:cancel:%s" % s.sid)])
    return "\n".join(lines), rows


async def filter_screen(app, user, s):
    th = T.get(user["theme"])
    rows = [[B(("✅ " if k == s.fkey else "") + label, "b:fx:%s:%s" % (s.sid, k))] for k, (label, _e) in FILTERS.items()]
    rows = grid([r[0] for r in rows], 2)
    rows.append([B("🔎 Search by name", "b:fn:%s" % s.sid)])
    rows.append([B("⬅ Back to files", "b:back:%s" % s.sid)])
    return "%s\nChoose which files are shown and selected by <b>Select all</b>." % head(th, "Filter"), rows


async def summary(app, user, s):
    th = T.get(user["theme"])
    dest = await app.get_dest(user, s.dest_id)
    o = effective_options(user, s, dest)
    nodes = s.selected_nodes()
    lines = [head(th, "Review"),
             "📦 Files: <b>%d</b> · %s" % (len(nodes), fmt_size(sum(n.size for n in nodes)) if any(n.size for n in nodes) else "size unknown"),
             "📡 Destination: <b>%s</b>" % esc(dest["name"]),
             "📤 Send as: %s" % SEND_AS[o["send_as"]]]
    if dest.get("is_forum"):
        lines.append("🧵 Topic per folder: %s" % onoff(o["topic_per_folder"]))
    lines.append("📝 Caption: <code>%s</code>" % esc(short(o["caption"].replace("\n", " ⏎ "), 60)))
    rows = []
    if nodes:
        rows.append([B("🟢 Start download (%d)" % len(nodes), "b:go:%s" % s.sid)])
    else:
        lines.append("\n⚠ Nothing selected yet — go back and tick some files.")
    rows.append([B("📡 Destination", "b:dest:%s" % s.sid), B("⚙ Options", "b:opt:%s" % s.sid)])
    rows.append([B("⬅ Files", "b:back:%s" % s.sid), B("❌ Cancel", "b:cancel:%s" % s.sid)])
    return "\n".join(lines), rows


async def options_screen(app, user, s):
    th = T.get(user["theme"])
    dest = await app.get_dest(user, s.dest_id)
    o = effective_options(user, s, dest)
    rows = [[B("📤 Send as: %s" % ("Auto" if o["send_as"] == "auto" else "File"), "b:ot:%s:sendas" % s.sid)]]
    if dest.get("is_forum"):
        rows.append([B("🧵 Topic per folder: %s" % onoff(o["topic_per_folder"]), "b:ot:%s:tpf" % s.sid)])
    rows.append([B("⬅ Back to review", "b:sum:%s" % s.sid)])
    text = "%s\nOptions apply to this download only. Change the defaults in ⚙ Settings." % head(th, "Options")
    return text, rows


async def dest_pick(app, user, s):
    th = T.get(user["theme"])
    cur = (await app.get_dest(user, s.dest_id))["id"]
    rows = [[B(("✅ " if cur == 0 else "") + "💬 This chat", "b:setd:%s:0" % s.sid)]]
    for d in await app.db.all("SELECT * FROM destinations WHERE user_id=? ORDER BY id", (user["id"],)):
        rows.append([B(("✅ " if cur == d["id"] else "") + short(d["name"], 34), "b:setd:%s:%d" % (s.sid, d["id"]))])
    rows.append([B("⬅ Back to review", "b:sum:%s" % s.sid)])
    return "%s\nWhere should the files be sent?" % head(th, "Destination"), rows


# --------------------------------------------------------------------------- queue / jobs
def _job_name(job: dict) -> str:
    try:
        return json.loads(job["options"] or "{}").get("root_name") or "download"
    except ValueError:
        return "download"


async def _job_counts(db, job_id: int) -> dict:
    return await db.one(
        "SELECT COUNT(*) total, COALESCE(SUM(status='done'),0) done, COALESCE(SUM(status='failed'),0) failed, "
        "COALESCE(SUM(status='skipped'),0) skipped FROM job_files WHERE job_id=?", (job_id,))


async def queue_screen(app, user):
    th = T.get(user["theme"])
    where, args = ("", ()) if is_admin(user) else ("WHERE user_id=?", (user["id"],))
    jobs = await app.db.all(
        "SELECT * FROM jobs %s ORDER BY CASE WHEN status IN ('running','paused','queued') THEN 0 ELSE 1 END, id DESC LIMIT 12" % where, args)
    rows = []
    for j in jobs:
        c = await _job_counts(app.db, j["id"])
        rows.append([B("%s #%d %s · %d/%d" % (JOB_ICON.get(j["status"], "•"), j["id"], short(_job_name(j), 18), c["done"], c["total"]),
                       "j:view:%d" % j["id"])])
    rows.append([B("🔄 Refresh", "m:q")])
    rows += footer()
    text = head(th, "Queue") + ("" if jobs else "\n<i>No jobs yet — send a mega.nz link.</i>")
    return text, rows


async def job_detail(app, user, job_id: int):
    th = T.get(user["theme"])
    j = await app.db.one("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not j or (j["user_id"] != user["id"] and not is_admin(user)):
        return await error_screen(app, user, "Job not found.", "m:q")
    c = await _job_counts(app.db, job_id)
    st = j["status"]
    lines = [head(th, "Job #%d · %s" % (job_id, short(_job_name(j), 28))),
             "%s Status: <b>%s</b>" % (JOB_ICON.get(st, "•"), st),
             "📚 Files: %d/%d ✅ · %d ❌ · %d skipped" % (c["done"], c["total"], c["failed"], c["skipped"]),
             "📦 Total: %s" % fmt_size(j["total_bytes"])]
    if st == "queued":
        lines.append("🕒 Position in queue: %d" % await app.queue.queue_position(job_id))
    rows = []
    if st in ("queued", "running", "paused"):
        ctl = []
        if st == "paused":
            ctl.append(B("▶ Resume", "j:resume:%d" % job_id))
        elif st == "running":
            ctl += [B("⏸ Pause", "j:pause:%d" % job_id), B("⏭ Skip", "j:skip:%d" % job_id)]
        ctl.append(B("❌ Cancel", "j:cancel:%d" % job_id))
        rows.append(ctl)
    if st in ("partial", "failed", "cancelled") and (c["failed"] or c["skipped"]):
        rows.append([B("🔁 Retry failed", "j:retry:%d" % job_id)])
    rows.append([B("⬅ Queue", "m:q"), B("🏠 Menu", "m:home")])
    return "\n".join(lines), rows


async def job_card(app, v):
    """Live, animated card edited in place while a job runs."""
    th = T.get(v.theme)
    phase = v.phase
    title = {
        "queued": "🟡 Queued",
        "download": "%s ⬇ Downloading" % spinner(th, v.frame),
        "upload": "%s ⬆ Uploading" % spinner(th, v.frame),
        "wait": "⏳ Waiting for a free route",
        "paused": "⏸ Paused",
        "done": "✅ Finished",
        "partial": "⚠️ Finished with errors",
        "failed": "❌ Failed",
        "cancelled": "🚫 Cancelled",
    }.get(phase, phase)
    lines = [head(th, "Job #%d · %s" % (v.job_id, short(v.title, 26))), title]
    if phase in ("download", "upload"):
        pct = (v.cur_done / v.cur_total * 100.0) if v.cur_total else 0.0
        lines.append("%s <code>%s</code>" % (file_icon(v.file_name), esc(short(v.file_name, 38))))
        lines.append("%s %d%%" % (bar(pct, th, v.frame), int(pct)))
        speed = v.speed.value
        eta = ((v.cur_total - v.cur_done) / speed) if speed > 0 and v.cur_total else None
        size = "%s / %s" % (fmt_size(v.cur_done), fmt_size(v.cur_total)) if v.cur_total else fmt_size(v.cur_done)
        lines.append("⚡ %s · ⏱ %s · 📦 %s" % (fmt_speed(speed), fmt_eta(eta), size))
    elif phase == "wait":
        lines.append("🕒 Retrying in <b>%s</b> (quota / cooldown)" % fmt_eta(max(0, v.wait_until - time.time())))
    if v.files_total > 1 and v.bytes_total:
        overall = (v.bytes_done_files + v.cur_done) / v.bytes_total * 100.0
        lines.append("Σ %s %d%%" % (bar(overall, th, v.frame), int(min(overall, 100))))
    tail = "📚 Files: %d/%d ✅" % (v.files_done, v.files_total)
    if v.files_failed:
        tail += " · %d ❌" % v.files_failed
    if v.files_skipped:
        tail += " · %d ⏭" % v.files_skipped
    lines.append(tail)
    if v.route and phase in ("download", "upload", "wait"):
        lines.append("🧭 %s" % esc(v.route))
    if v.dest:
        lines.append("📡 %s" % esc(short(v.dest, 40)))
    if v.note:
        lines.append(esc(v.note))
    ctl = v.app.queue.get_ctl(v.job_id)
    if phase in ("download", "upload", "wait"):
        pause = B("⏸ Pause", "j:pause:%d" % v.job_id)
        rows = [[pause, B("⏭ Skip", "j:skip:%d" % v.job_id), B("❌ Cancel", "j:cancel:%d" % v.job_id)]]
        if ctl.paused:
            lines.append("⏸ Pausing after this file…")
    elif phase == "paused":
        rows = [[B("▶ Resume", "j:resume:%d" % v.job_id), B("❌ Cancel", "j:cancel:%d" % v.job_id)]]
    elif phase == "queued":
        rows = [[B("❌ Cancel", "j:cancel:%d" % v.job_id)]]
    elif phase in ("partial", "failed"):
        rows = [[B("🔁 Retry failed", "j:retry:%d" % v.job_id)], [B("📋 Queue", "m:q"), B("🏠 Menu", "m:home")]]
    else:
        rows = [[B("📋 Queue", "m:q"), B("🏠 Menu", "m:home")]]
    return "\n".join(lines), rows


# --------------------------------------------------------------------------- destinations
async def dests_screen(app, user):
    th = T.get(user["theme"])
    dests = await app.db.all("SELECT * FROM destinations WHERE user_id=? ORDER BY id", (user["id"],))
    rows = [[B(("⭐ " if user["default_dest"] == d["id"] else "📡 ") + short(d["name"], 34), "d:view:%d" % d["id"])] for d in dests]
    rows.append([B("➕ Add destination", "d:add")])
    rows += footer()
    text = "\n".join([head(th, "Destinations"),
                      "Default: <b>%s</b>" % esc((await app.get_dest(user))["name"]),
                      "Add the bot as <b>admin</b> to your channel/group first.", "Without one, files go to this chat."])
    return text, rows


async def dest_detail(app, user, did: int):
    th = T.get(user["theme"])
    d = await app.db.one("SELECT * FROM destinations WHERE id=? AND user_id=?", (did, user["id"]))
    if not d:
        return await error_screen(app, user, "Destination not found.", "m:d")
    lines = [head(th, short(d["name"], 34)),
             "🆔 Chat: <code>%s</code>" % d["chat_id"],
             "🧵 Topic: %s" % (d["topic_id"] or "none (main chat)"),
             "📤 Uploads via: %s" % ("your user account" if d["via"] == "user" else "the bot")]
    rows = []
    if user["default_dest"] != did:
        rows.append([B("⭐ Set as default", "d:def:%d" % did)])
    rows.append([B("🧪 Send test message", "d:test:%d" % did)])
    if d["is_forum"]:
        rows.append([B("🧵 Topic per folder: %s" % onoff(d["topic_per_folder"]), "d:tpf:%d" % did)])
    if app.user_client is not None:
        rows.append([B("👤 Upload via: %s" % ("user" if d["via"] == "user" else "bot"), "d:via:%d" % did)])
    rows.append([B("🗑 Delete", "d:del:%d" % did)])
    rows.append([B("⬅ Back", "m:d"), B("🏠 Menu", "m:home")])
    return "\n".join(lines), rows


# --------------------------------------------------------------------------- proxies
SOURCE_INTERVALS = [0, 15, 30, 60, 360]       # minutes between automatic API refreshes (0 = manual)
SOURCE_SCHEMES = ["auto", "http", "socks5", "socks4"]
CHECK_INTERVALS = [0, 15, 30, 60, 180]        # minutes between automatic pool health checks (0 = off)


def _host(url: str) -> str:
    return mask_proxy(url).split("://")[-1].split("@")[-1]


def _ago(ts) -> str:
    if not ts:
        return "never"
    sec = int(time.time() - ts)
    if sec < 90:
        return "just now"
    if sec < 5400:
        return "%d min ago" % (sec // 60)
    if sec < 172800:
        return "%d h ago" % (sec // 3600)
    return "%d d ago" % (sec // 86400)


def _every(minutes) -> str:
    minutes = int(minutes or 0)
    if not minutes:
        return "off"
    return ("%d min" % minutes) if minutes < 60 else ("%d h" % (minutes // 60))


async def proxies_screen(app, user):
    th = T.get(user["theme"])
    mode = await app.db.get_setting("proxy_mode", "direct_first")
    check = await app.db.get_setting("proxy_check_min", "60")
    proxies = await app.proxy_pool.list()
    sources = await app.proxy_pool.sources()
    now = time.time()
    ready = sum(1 for p in proxies if proxy_state(p, now) == "ok")
    lines = [head(th, "Proxies"),
             "Mode: <b>%s</b> · %d in pool · %d ready" % (mode_label(PROXY_MODES, mode), len(proxies), ready),
             "🩺 Auto-check: %s · 🔗 API sources: %d" % (_every(check), len(sources))]
    btns = []
    for p in proxies[:40]:
        lat = (" · %.1fs" % p["latency"]) if p["latency"] else ""
        btns.append(B("%s #%d %s%s" % (STATE_ICON[proxy_state(p, now)], p["id"], short(_host(p["url"]), 16), lat), "p:view:%d" % p["id"]))
    if not proxies:
        lines.append("<i>No proxies yet. Without any, downloads use this machine's IP.</i>")
    elif len(proxies) > 40:
        lines.append("<i>Showing the first 40 of %d.</i>" % len(proxies))
    rows = grid(btns, 2)
    rows.append([B("➕ Add", "p:add"), B("🔗 Add via API", "p:sadd")])
    rows.append([B("🩺 Check a list", "p:chk"), B("🧪 Test all", "p:test:all")])
    rows.append([B("📡 API sources (%d)" % len(sources), "p:src"), B("🔁 Mode: %s" % mode_label(PROXY_MODES, mode), "p:mode")])
    rows += footer()
    return "\n".join(lines), rows


async def proxy_detail(app, user, pid: int):
    th = T.get(user["theme"])
    p = await app.proxy_pool.get(pid)
    if not p:
        return await error_screen(app, user, "Proxy not found.", "m:p")
    st, now = proxy_state(p), time.time()
    lines = [head(th, "Proxy #%d" % pid), "🔗 <code>%s</code>" % esc(mask_proxy(p["url"])),
             "%s Status: <b>%s</b>" % (STATE_ICON[st], st)]
    if st == "cooldown":
        lines.append("⏳ Back in %s" % fmt_eta(p["cooldown_until"] - now))
    if p["latency"]:
        lines.append("⚡ Latency: %d ms" % int(p["latency"] * 1000))
    if p.get("exit_ip"):
        lines.append("🌍 Exit IP: <code>%s</code>" % esc(p["exit_ip"]))
    lines.append("🩺 Checked: %s" % _ago(p.get("checked_at")))
    if p.get("source_id"):
        lines.append("📡 From API source #%d" % p["source_id"])
    lines.append("📦 Downloaded: %s" % fmt_size(p["bytes_total"]))
    if p["last_error"]:
        lines.append("⚠ Last error: %s" % esc(short(p["last_error"], 80)))
    rows = [[B("🧪 Test", "p:test:%d" % pid), B("♻ Reset", "p:rst:%d" % pid)],
            [B("🗑 Remove", "p:del:%d" % pid)], [B("⬅ Back", "m:p"), B("🏠 Menu", "m:home")]]
    return "\n".join(lines), rows


async def sources_screen(app, user):
    th = T.get(user["theme"])
    sources = await app.proxy_pool.sources()
    lines = [head(th, "Proxy API sources"),
             "Import proxies from a provider's API or a list URL (text or JSON).",
             "Each source can refresh itself on a schedule."]
    rows = []
    for src in sources:
        n = (await app.db.one("SELECT COUNT(*) c FROM proxies WHERE source_id=?", (src["id"],)))["c"]
        icon = "🔴" if src["last_error"] else "🟢"
        rows.append([B("%s %s · %d proxies · ⏱ %s" % (icon, short(src["name"], 18), n, _every(src["interval_min"])), "p:sv:%d" % src["id"])])
    if not sources:
        lines.append("<i>No API sources yet.</i>")
    rows.append([B("➕ Add API source", "p:sadd")])
    rows.append([B("⬅ Proxies", "m:p"), B("🏠 Menu", "m:home")])
    return "\n".join(lines), rows


async def source_detail(app, user, sid: int, note: str = ""):
    th = T.get(user["theme"])
    src = await app.proxy_pool.source(sid)
    if not src:
        return await error_screen(app, user, "API source not found.", "p:src")
    row = await app.db.one("SELECT COUNT(*) c, COALESCE(SUM(status!='dead'),0) ok FROM proxies WHERE source_id=?", (sid,))
    names = ", ".join(app.proxy_pool.headers_of(src).keys()) or "none"
    lines = [head(th, "API source #%d" % sid),
             "🔗 <code>%s</code>" % esc(mask_url(src["url"])),
             "🔑 Headers: %s (values hidden)" % esc(names),
             "📥 Last fetch: %s · %d proxies" % (_ago(src["last_fetch"]), src["last_count"]),
             "🌐 In pool: %d (%d alive)" % (row["c"], row["ok"])]
    if src["last_error"]:
        lines.append("🔴 Error: %s" % esc(short(src["last_error"], 100)))
    if note:
        lines.append(esc(note))
    rows = [[B("⬇ Fetch now", "p:sf:%d" % sid)],
            [B("🔤 Scheme: %s" % src["scheme"], "p:ss:%d" % sid), B("⏱ Auto: %s" % _every(src["interval_min"]), "p:si:%d" % sid)],
            [B("♻ Sync removals: %s" % onoff(src["sync"]), "p:sy:%d" % sid)],
            [B("🗑 Delete source", "p:sd:%d" % sid)],
            [B("⬅ Sources", "p:src"), B("🏠 Menu", "m:home")]]
    return "\n".join(lines), rows


async def source_delete_screen(app, user, sid: int):
    th = T.get(user["theme"])
    rows = [[B("🗑 Source + its proxies", "p:sdc:%d:1" % sid)],
            [B("🗑 Source only (keep proxies)", "p:sdc:%d:0" % sid)],
            [B("✖ Cancel", "p:sv:%d" % sid)]]
    return "%s\nDelete API source #%d?" % (head(th, "Are you sure?"), sid), rows


# --------------------------------------------------------------------------- accounts
async def accounts_screen(app, user):
    th = T.get(user["theme"])
    mode = await app.db.get_setting("account_mode", "anon_first")
    accounts = await app.accounts.list()
    now = time.time()
    lines = [head(th, "Mega accounts"), "Mode: <b>%s</b> · %d saved" % (mode_label(ACCOUNT_MODES, mode), len(accounts)),
             "Passwords are stored encrypted.",
             "ℹ Accounts are only used for <b>single-file links</b> (through megatools). "
             "Files inside folder links are always fetched anonymously."]
    btns = [B("%s #%d %s" % (STATE_ICON[account_state(a, now)], a["id"], mask_email(a["email"])), "a:view:%d" % a["id"]) for a in accounts[:40]]
    if not accounts:
        lines.append("<i>No accounts yet. Public links work without one.</i>")
    rows = grid(btns, 1)
    rows.append([B("➕ Add account", "a:add")])
    rows.append([B("🔁 Mode: %s" % mode_label(ACCOUNT_MODES, mode), "a:mode")])
    rows += footer()
    return "\n".join(lines), rows


async def account_detail(app, user, aid: int, info: str = ""):
    th = T.get(user["theme"])
    a = await app.accounts.get(aid)
    if not a:
        return await error_screen(app, user, "Account not found.", "m:a")
    st, now = account_state(a), time.time()
    lines = [head(th, "Account #%d" % aid), "✉ %s" % esc(mask_email(a["email"])), "%s Status: <b>%s</b>" % (STATE_ICON[st], st)]
    if st == "cooldown":
        lines.append("⏳ Quota cooldown: %s left" % fmt_eta(a["cooldown_until"] - now))
    lines.append("📦 Downloaded: %s" % fmt_size(a["bytes_total"]))
    lines.append("🌐 Bound proxy: %s" % (("#%d" % a["proxy_id"]) if a["proxy_id"] else "auto"))
    if a["last_error"]:
        lines.append("⚠ Last error: %s" % esc(short(a["last_error"], 80)))
    if info:
        lines.append("ℹ %s" % esc(short(info, 160)))
    rows = [[B("ℹ Storage info", "a:info:%d" % aid), B("♻ Reset", "a:rst:%d" % aid)],
            [B("🌐 Next proxy", "a:px:%d" % aid)], [B("🗑 Remove", "a:del:%d" % aid)],
            [B("⬅ Back", "m:a"), B("🏠 Menu", "m:home")]]
    return "\n".join(lines), rows


# --------------------------------------------------------------------------- misc
async def stats_screen(app, user):
    th, db = T.get(user["theme"]), app.db
    where, args = ("", ()) if is_admin(user) else (" WHERE user_id=?", (user["id"],))
    jobs = await db.one("SELECT COUNT(*) c, COALESCE(SUM(status='done'),0) d FROM jobs" + where, args)
    files = await db.one(
        "SELECT COUNT(*) c, COALESCE(SUM(size),0) b FROM job_files WHERE status='done'"
        + ("" if is_admin(user) else " AND job_id IN (SELECT id FROM jobs WHERE user_id=?)"), args)
    today = await db.one("SELECT COALESCE(SUM(bytes),0) b FROM usage WHERE day=date('now','localtime')" + (" AND user_id=?" if args else ""), args)
    up = int(time.time() - app.started)
    lines = [head(th, "Stats"),
             "📋 Jobs: %d (%d finished)" % (jobs["c"], jobs["d"]),
             "📄 Files sent: %d · %s" % (files["c"], fmt_size(files["b"])),
             "📅 Today: %s" % fmt_size(today["b"])]
    if is_admin(user):
        usage = shutil.disk_usage(app.cfg.download_dir)
        lines.append("💽 Disk free: %s" % fmt_size(usage.free))
        lines.append("⏱ Uptime: %s" % fmt_eta(up))
        mv = getattr(app.mega, "version", "")
        lines.append("🧰 megatools: %s (optional — only for Mega accounts)" % (mv or "not installed"))
        for p in (await app.proxy_pool.list())[:8]:
            lines.append("🌐 #%d %s — %s" % (p["id"], short(_host(p["url"]), 18), fmt_size(p["bytes_total"])))
        for a in (await app.accounts.list())[:8]:
            lines.append("👤 #%d %s — %s" % (a["id"], mask_email(a["email"]), fmt_size(a["bytes_total"])))
    return "\n".join(lines), footer()


async def settings_screen(app, user):
    th = T.get(user["theme"])
    rows = [[B("🎞 Animations: %s" % onoff(user["animations"]), "set:anim")],
            [B("📤 Send as: %s" % ("Auto" if user["send_as"] == "auto" else "File"), "set:sendas")],
            [B("🔔 Notify when done: %s" % onoff(user["notify"]), "set:notify")],
            [B("📝 Caption template", "set:cap")],
            [B("🎨 Theme", "m:th")]]
    if is_admin(user):
        pm = await app.db.get_setting("proxy_mode", "direct_first")
        am = await app.db.get_setting("account_mode", "anon_first")
        pc = await app.db.get_setting("proxy_check_min", "60")
        pr = await app.db.get_setting("proxy_autoremove", "0") == "1"
        rows.append([B("🌐 Proxy mode: %s" % mode_label(PROXY_MODES, pm), "set:pmode")])
        rows.append([B("🩺 Proxy auto-check: %s" % _every(pc), "set:pchk")])
        rows.append([B("🧹 Auto-remove dead proxies: %s" % onoff(pr), "set:prm")])
        rows.append([B("👤 Account mode: %s" % mode_label(ACCOUNT_MODES, am), "set:amode")])
    rows += footer()
    text = "\n".join([head(th, "Settings"), "📝 Caption: <code>%s</code>" % esc(short(user["caption"].replace("\n", " ⏎ "), 60)),
                      "Animations off = fewer message edits (good for slow hosts)."])
    return text, rows


async def theme_screen(app, user):
    th = T.get(user["theme"])
    btns = [B(("✅ " if k == user["theme"] else "") + v["label"], "th:%s" % k) for k, v in T.THEMES.items()]
    rows = grid(btns, 2) + footer()
    demo = "%s 62%%" % bar(62, th, 0)
    return "%s\nPreview: %s" % (head(th, "Theme"), demo), rows
