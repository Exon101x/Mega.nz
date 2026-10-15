"""Telegram event handlers: commands, text input flows and every button callback."""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
import uuid
from collections import Counter

from telethon import errors, events, utils
from telethon.tl import types

from accounts import mask_email, parse_accounts
from browse import BrowseSession
from mega_client import UNKNOWN_NAME, MegaError, Node, extract_links
from proxies import mask_proxy, parse_proxy_entries
from router import ACCOUNT_MODES, PROXY_MODES
from ui import screens as S
from ui import themes as T

log = logging.getLogger("handlers")
AWAIT_TTL = 600  # seconds a pending "send me X" prompt stays valid
MAX_CHECK = 300  # proxies per checker run


# =========================================================================== helpers
class Ctx:
    """Bundles what a callback handler needs, and makes sure the query is answered exactly once."""

    def __init__(self, app, event, user, parts):
        self.app, self.event, self.user, self.parts = app, event, user, parts
        self._answered = False

    @property
    def uid(self) -> int:
        return self.event.sender_id

    @property
    def chat_id(self) -> int:
        return self.event.chat_id

    @property
    def msg_id(self) -> int:
        return self.event.message_id

    async def ans(self, text=None, alert: bool = False) -> None:
        if self._answered:
            return
        self._answered = True
        try:
            await self.event.answer(text or None, alert=alert)
        except Exception:
            pass

    async def edit(self, screen) -> None:
        text, buttons = screen
        try:
            await self.event.edit(text, buttons=buttons, parse_mode="html", link_preview=False)
        except errors.MessageNotModifiedError:
            pass

    async def refresh_user(self) -> dict:
        self.user = await self.app.db.user(self.uid)
        return self.user


async def authorize(app, uid: int, name=None):
    u = await app.db.user(uid)
    if uid in app.cfg.owner_ids:
        if not u:
            return await app.db.ensure_user(uid, "owner", name)
        if u["role"] != "owner":
            await app.db.update_user(uid, role="owner")
            u = await app.db.user(uid)
        return u
    if u and u["role"] == "banned":
        return None
    if u:
        return u
    if app.cfg.public:
        return await app.db.ensure_user(uid, "user", name)
    return None


async def show(app, chat_id: int, msg_id, screen) -> None:
    """Edits the menu message in place (single-message UI); sends a new one if that fails."""
    text, buttons = screen
    try:
        if msg_id:
            await app.bot.edit_message(chat_id, msg_id, text, buttons=buttons, parse_mode="html", link_preview=False)
            return
    except errors.MessageNotModifiedError:
        return
    except Exception:
        pass
    await app.bot.send_message(chat_id, text, buttons=buttons, parse_mode="html", link_preview=False)


async def reply(event, screen):
    text, buttons = screen
    return await event.respond(text, buttons=buttons, parse_mode="html", link_preview=False)


def set_awaiting(app, uid: int, kind: str, msg_id=None, **extra) -> None:
    app.awaiting[uid] = {"kind": kind, "msg_id": msg_id, "t": time.time(), **extra}


async def incoming_text(event) -> str:
    """Message text plus the content of an attached small text file (for pasted lists)."""
    text = event.raw_text or ""
    doc = event.message.document
    if doc and doc.size < 2 * 1024 * 1024:
        name = ""
        for a in doc.attributes:
            name = getattr(a, "file_name", name) or name
        if name.lower().endswith((".txt", ".csv", ".list")):
            data = await event.message.download_media(file=bytes)
            if data:
                text += "\n" + data.decode("utf-8", "ignore")
    return text


# =========================================================================== registration
def register(app) -> None:
    bot = app.bot

    @bot.on(events.NewMessage(incoming=True))
    async def _on_message(event):
        try:
            await on_message(app, event)
        except Exception:
            log.exception("message handler failed")
            try:
                await event.respond("⚠ Something went wrong. Try /menu.")
            except Exception:
                pass

    @bot.on(events.CallbackQuery())
    async def _on_callback(event):
        try:
            await on_callback(app, event)
        except Exception:
            log.exception("callback handler failed")
            try:
                await event.answer("⚠ Something went wrong", alert=True)
            except Exception:
                pass


# =========================================================================== messages
async def on_message(app, event) -> None:
    if not event.is_private:
        return
    sender = await event.get_sender()
    user = await authorize(app, event.sender_id, utils.get_display_name(sender) if sender else None)
    if user is None:
        await event.respond("⛔ You are not authorised to use this bot.\nYour ID: <code>%d</code> — send it to the owner."
                            % event.sender_id, parse_mode="html")
        return
    text = (event.raw_text or "").strip()
    if text.startswith("/"):
        parts = text[1:].split(None, 1)
        cmd = parts[0].split("@")[0].lower() if parts else ""
        arg = parts[1] if len(parts) > 1 else ""
        if event.message.document:  # e.g. a .txt list sent with the caption /checkproxy
            more = (await incoming_text(event)).split(None, 1)
            arg = more[1] if len(more) > 1 else arg
        app.awaiting.pop(event.sender_id, None)
        fn = COMMANDS.get(cmd)
        if fn:
            await fn(app, event, user, arg)
        else:
            await event.respond("Unknown command. Try /menu or /help.")
        return

    aw = app.awaiting.get(event.sender_id)
    if aw and time.time() - aw["t"] > AWAIT_TTL:
        app.awaiting.pop(event.sender_id, None)
        aw = None
    if aw:
        app.awaiting.pop(event.sender_id, None)
        await handle_awaiting(app, event, user, aw)
        return

    body = await incoming_text(event)
    links = extract_links(body)
    if links:
        for link in links[:5]:
            await start_browse(app, event, user, link)
        if len(links) > 5:
            await event.respond("Only the first 5 links were opened — send the rest again afterwards.")
        return
    await reply(event, await S.main_menu(app, user))


# ------------------------------------------------------------------------- commands
async def cmd_start(app, event, user, arg):
    if user["animations"]:
        msg = await event.respond("🚀")
        try:
            for frame in ("🚀 <b>M</b>", "🚀 <b>MEGA</b> ⟶", "🚀 <b>MEGA → Tele</b>"):
                await asyncio.sleep(0.35)
                await msg.edit(frame, parse_mode="html")
            text, buttons = await S.main_menu(app, user)
            await msg.edit(text, buttons=buttons, parse_mode="html", link_preview=False)
            return
        except Exception:
            pass
    await reply(event, await S.main_menu(app, user))


async def cmd_menu(app, event, user, arg):
    await reply(event, await S.main_menu(app, user))


async def cmd_help(app, event, user, arg):
    await reply(event, await S.help_screen(app, user))


async def cmd_queue(app, event, user, arg):
    await reply(event, await S.queue_screen(app, user))


async def cmd_stats(app, event, user, arg):
    await reply(event, await S.stats_screen(app, user))


async def cmd_theme(app, event, user, arg):
    await reply(event, await S.theme_screen(app, user))


async def cmd_cancel(app, event, user, arg):
    await event.respond("✖ Cancelled.")


async def cmd_id(app, event, user, arg):
    await event.respond("Your Telegram ID: <code>%d</code>" % event.sender_id, parse_mode="html")


def _admin_only(fn):
    async def wrapper(app, event, user, arg):
        if not S.is_admin(user):
            await event.respond("⛔ Admins only.")
            return
        await fn(app, event, user, arg)
    return wrapper


@_admin_only
async def cmd_addproxy(app, event, user, arg):
    if arg.strip():
        await event.respond(await add_proxies(app, arg))
        await reply(event, await S.proxies_screen(app, user))
    else:
        m = await reply(event, await S.prompt_screen(
            app, user, "Send proxies, one per line (<code>ip:port</code>, <code>user:pass@ip:port</code>, "
                       "<code>socks5://…</code>) or upload a .txt file.", "p:cancel"))
        set_awaiting(app, user["id"], "proxy", m.id)


@_admin_only
async def cmd_checkproxy(app, event, user, arg):
    if arg.strip():
        await run_check(app, event, user, arg)
    else:
        m = await reply(event, await S.prompt_screen(app, user, CHECK_PROMPT, "p:cancel"))
        set_awaiting(app, user["id"], "pcheck", m.id)


@_admin_only
async def cmd_addproxyapi(app, event, user, arg):
    if arg.strip():
        await create_source(app, event, user, arg, None)
    else:
        m = await reply(event, await S.prompt_screen(app, user, SOURCE_PROMPT, "p:cancel"))
        set_awaiting(app, user["id"], "psrc", m.id)


@_admin_only
async def cmd_addaccount(app, event, user, arg):
    if arg.strip():
        await add_accounts(app, event, arg)
        await reply(event, await S.accounts_screen(app, user))
    else:
        m = await reply(event, await S.prompt_screen(
            app, user, "Send accounts, one per line: <code>email password</code>.\nYour message is deleted right after.", "a:cancel"))
        set_awaiting(app, user["id"], "account", m.id)


@_admin_only
async def cmd_allow(app, event, user, arg):
    m = re.match(r"\s*(\d+)", arg)
    if not m:
        await event.respond("Usage: /allow <user id>")
        return
    uid = int(m.group(1))
    await app.db.ensure_user(uid, "user")
    await app.db.update_user(uid, role="admin" if uid in app.cfg.owner_ids else "user")
    await event.respond("✅ %d is allowed." % uid)


@_admin_only
async def cmd_deny(app, event, user, arg):
    m = re.match(r"\s*(\d+)", arg)
    if not m or int(m.group(1)) in app.cfg.owner_ids:
        await event.respond("Usage: /deny <user id> (owners cannot be removed)")
        return
    await app.db.ensure_user(int(m.group(1)), "banned")
    await app.db.update_user(int(m.group(1)), role="banned")
    await event.respond("🚫 %s is blocked." % m.group(1))


async def cmd_admin(app, event, user, arg):
    if user["role"] != "owner":
        await event.respond("⛔ Owner only.")
        return
    m = re.match(r"\s*(\d+)", arg)
    if not m:
        await event.respond("Usage: /admin <user id>")
        return
    await app.db.ensure_user(int(m.group(1)), "admin")
    await app.db.update_user(int(m.group(1)), role="admin")
    await event.respond("✅ %s is now an admin." % m.group(1))


@_admin_only
async def cmd_users(app, event, user, arg):
    rows = await app.db.all("SELECT id, role, name FROM users ORDER BY role, id")
    lines = ["👥 <b>Users</b>"] + ["• <code>%d</code> %s — %s" % (r["id"], S.esc(r["name"] or "?"), r["role"]) for r in rows]
    await event.respond("\n".join(lines), parse_mode="html")


COMMANDS = {
    "start": cmd_start, "menu": cmd_menu, "help": cmd_help, "queue": cmd_queue, "stats": cmd_stats,
    "theme": cmd_theme, "cancel": cmd_cancel, "id": cmd_id, "addproxy": cmd_addproxy, "checkproxy": cmd_checkproxy, "addproxyapi": cmd_addproxyapi,
    "addaccount": cmd_addaccount, "allow": cmd_allow, "deny": cmd_deny, "admin": cmd_admin, "users": cmd_users,
}


# ------------------------------------------------------------------------- awaited input
CHECK_PROMPT = ("Send the proxies to check (one per line, or upload a .txt file). "
                "Nothing is added until you confirm.\n"
                "Formats: <code>ip:port</code> · <code>user:pass@ip:port</code> · <code>socks5://…</code>. "
                "Without a scheme I try http, socks5 and socks4.")
SOURCE_PROMPT = ("Send the API / list URL on the first line. If it needs a key, add header lines below it:\n"
                 "<code>https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&amp;page_size=100\n"
                 "Authorization: Token YOUR_KEY</code>\n"
                 "Plain lists (one <code>ip:port</code> per line) and JSON both work. Your message is deleted afterwards.")


async def add_proxies(app, text: str) -> str:
    new, dupes, bad = await app.proxy_pool.add_many(text)
    if new:
        asyncio.create_task(app.proxy_pool.verify(new))
    return ("✅ %d proxies added (checking them in the background, scheme auto-detected) "
            "· ♻ %d duplicates · ⚠ %d invalid lines" % (len(new), dupes, bad))


def format_check_result(results, bad: int, capped: bool) -> str:
    working = sorted((r for r in results if r.ok), key=lambda r: r.latency or 99)
    failed = [r for r in results if not r.ok]
    lines = ["🩺 <b>Proxy check</b>",
             "✅ <b>%d</b> working · ❌ <b>%d</b> failed%s" % (len(working), len(failed), (" · ⚠ %d invalid lines" % bad) if bad else "")]
    if capped:
        lines.append("<i>Only the first %d proxies were checked.</i>" % MAX_CHECK)
    if working:
        lines += ["", "<b>Fastest</b>"]
        for r in working[:15]:
            ip = (" → %s" % r.exit_ip) if r.exit_ip else ""
            lines.append("✅ <code>%s</code> · %.2fs%s" % (S.esc(mask_proxy(r.url)), r.latency or 0, ip))
        if len(working) > 15:
            lines.append("… and %d more" % (len(working) - 15))
    if failed:
        lines += ["", "<b>Why they failed</b>"]
        for msg, n in Counter((r.error or "failed")[:40] for r in failed).most_common(3):
            lines.append("❌ %d× %s" % (n, S.esc(msg)))
    return "\n".join(lines)


async def run_check(app, event, user, text: str, msg_id=None) -> None:
    """Standalone proxy checker: tests a pasted list (reachability, latency, exit IP, IP-leak) without adding it."""
    entries, bad = parse_proxy_entries(text, "auto")
    if not entries:
        await event.respond("⚠ I found no valid proxies in that message.")
        return
    capped = len(entries) > MAX_CHECK
    entries = entries[:MAX_CHECK]
    status = await event.respond("🩺 Checking %d proxies…" % len(entries))
    state = {"done": 0, "ok": 0, "t": 0.0}

    async def on_result(res) -> None:
        state["done"] += 1
        state["ok"] += 1 if res.ok else 0
        now = time.monotonic()
        if now - state["t"] > 2.0 and state["done"] < len(entries):
            state["t"] = now
            try:
                await status.edit("🩺 Checking… %d/%d  ✅ %d  ❌ %d" % (state["done"], len(entries), state["ok"], state["done"] - state["ok"]))
            except Exception:
                pass

    results = await app.proxy_pool.check_entries(entries, 10, on_result)
    app.check_results[user["id"]] = {"t": time.time(), "results": results}
    working = sum(1 for r in results if r.ok)
    rows = []
    if working:
        rows.append([S.B("➕ Add working (%d)" % working, "p:cadd"), S.B("📄 Working list (.txt)", "p:cfile")])
    rows.append([S.B("🌐 Proxies", "m:p"), S.B("🏠 Menu", "m:home")])
    await status.edit(format_check_result(results, bad, capped), buttons=rows, parse_mode="html")
    if msg_id:
        await show(app, event.chat_id, msg_id, await S.proxies_screen(app, user))


def parse_source_input(text: str):
    """First http(s) line = URL; other `Name: value` lines = request headers (e.g. Authorization)."""
    url, headers = None, {}
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if re.match(r"https?://", line, re.I):
            if url is None:
                url = line
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            if re.fullmatch(r"[A-Za-z0-9\-]{2,40}", k.strip()) and v.strip():
                headers[k.strip()] = v.strip()
    return url, headers


async def create_source(app, event, user, text: str, msg_id) -> None:
    url, headers = parse_source_input(text)
    try:
        await event.delete()  # the message may contain an API key
    except Exception:
        pass
    if not url:
        await event.respond("⚠ I need an http(s) URL on the first line. Send it again, or /cancel.")
        set_awaiting(app, user["id"], "psrc", msg_id)
        return
    status = await event.respond("⬇ Fetching proxies from the API…")
    sid = await app.proxy_pool.add_source(url, headers)
    res = await app.proxy_pool.refresh_source(sid)
    if "error" in res:
        await app.proxy_pool.delete_source(sid, True)
        await status.edit("🔴 Could not import: %s\nNothing was saved — check the URL / API key and try again." % S.esc(res["error"]),
                          parse_mode="html")
        await show(app, event.chat_id, msg_id, await S.proxies_screen(app, user))
        return
    await status.edit("✅ Imported <b>%d</b> proxies (%d new, %d already known). Checking them in the background…"
                      % (res["fetched"], res["added"], res["dupes"]), parse_mode="html")
    await show(app, event.chat_id, msg_id, await S.source_detail(app, user, sid, "Tip: set ⏱ Auto to refresh this list on a schedule."))


async def add_accounts(app, event, text: str) -> None:
    accounts = parse_accounts(text)
    if not accounts:
        await event.respond("⚠ I found no valid <code>email password</code> lines.", parse_mode="html")
        return
    status = await event.respond("🔐 Checking %d account(s)…" % len(accounts))
    lines = []
    for email, pw in accounts:
        ok, info = await app.accounts.add(email, pw)
        lines.append("%s %s — %s" % ("✅" if ok else "❌", mask_email(email), S.esc(info[:80])))
    await status.edit("\n".join(lines), parse_mode="html")


async def handle_awaiting(app, event, user, aw) -> None:
    kind, uid, chat_id = aw["kind"], user["id"], event.chat_id
    text = await incoming_text(event)
    if kind == "proxy":
        await show_result(app, event, await add_proxies(app, text))
        await show(app, chat_id, aw["msg_id"], await S.proxies_screen(app, user))
    elif kind == "pcheck":
        await run_check(app, event, user, text, aw["msg_id"])
    elif kind == "psrc":
        await create_source(app, event, user, text, aw["msg_id"])
    elif kind == "account":
        try:
            await event.delete()  # never leave passwords in the chat
        except Exception:
            pass
        await add_accounts(app, event, text)
        await show(app, chat_id, aw["msg_id"], await S.accounts_screen(app, user))
    elif kind == "caption":
        template = text.strip()[:300]
        if template:
            await app.db.update_user(uid, caption=template)
        user = await app.db.user(uid)
        await show(app, chat_id, aw["msg_id"], await S.settings_screen(app, user))
    elif kind == "bfilter":
        s = app.sessions.get(aw["sid"])
        if s:
            s.name_filter = text.strip()[:60]
            s.page = 0
            await show(app, chat_id, aw["msg_id"], await S.browser(app, user, s))
    elif kind == "dest":
        await handle_dest_input(app, event, user, aw, text)


async def show_result(app, event, text: str) -> None:
    await event.respond(text)


# ------------------------------------------------------------------------- destination input
async def handle_dest_input(app, event, user, aw, text: str) -> None:
    chat_id = event.chat_id
    try:
        ent, hint = await resolve_destination(app, event, text)
    except ValueError as e:
        await event.respond("⚠ %s" % e)
        set_awaiting(app, user["id"], "dest", aw["msg_id"])
        return
    chat_peer = utils.get_peer_id(ent)
    title = getattr(ent, "title", None) or "chat"
    is_forum = bool(getattr(ent, "forum", False))
    warn = ""
    try:
        perms = await app.bot.get_permissions(ent, "me")
        if getattr(ent, "broadcast", False) and not (perms.is_admin or perms.is_creator):
            warn = "⚠ The bot is not an admin there, so posting will probably fail."
    except Exception:
        warn = "⚠ I could not verify the bot's rights there — make sure it may post."
    topics = []
    if is_forum:
        try:
            topics = await app.uploader.list_topics(app.bot, await app.bot.get_input_entity(ent))
        except Exception:
            topics = []
    if is_forum and hint and any(t[0] == hint for t in topics):
        name = next(t[1] for t in topics if t[0] == hint)
        await save_destination(app, user, chat_peer, title, hint, name, is_forum)
        await show(app, chat_id, aw["msg_id"], await S.dests_screen(app, await app.db.user(user["id"])))
        if warn:
            await event.respond(warn)
        return
    if is_forum:
        app.pending_dest[user["id"]] = {"chat_id": chat_peer, "title": title, "topics": dict(topics), "msg_id": aw["msg_id"]}
        btns = [[S.B("🏠 No topic (General)", "d:tp:0")]]
        btns += [[S.B("🧵 " + S.short(n, 30), "d:tp:%d" % i)] for i, n in topics[:30]]
        btns.append([S.B("✖ Cancel", "m:d")])
        text_out = "%s\n<b>%s</b> has topics — where should files go?%s" % (
            S.head(T.get(user["theme"]), "Pick a topic"), S.esc(title), ("\n" + warn) if warn else "")
        await show(app, chat_id, aw["msg_id"], (text_out, btns))
        return
    await save_destination(app, user, chat_peer, title, None, "", False)
    await show(app, chat_id, aw["msg_id"], await S.dests_screen(app, await app.db.user(user["id"])))
    if warn:
        await event.respond(warn)


async def resolve_destination(app, event, text: str):
    """Returns (entity, topic hint). Accepts a forwarded message, @username, t.me link or numeric ID."""
    msg, hint, ent = event.message, None, None
    if msg.forward:
        try:
            ent = await msg.forward.get_chat()
        except Exception:
            ent = None
        if ent is None:
            raise ValueError("I cannot see where that message came from (the channel may hide it). Send @username or a t.me link instead.")
    else:
        t = text.strip()
        m = re.search(r"t\.me/c/(\d+)(?:/(\d+))?(?:/(\d+))?", t)
        if m:
            target = int("-100" + m.group(1))
            hint = int(m.group(2)) if m.group(2) else None
        else:
            m = re.search(r"(?:t\.me/|@)([A-Za-z][A-Za-z0-9_]{3,})(?:/(\d+))?(?:/(\d+))?", t)
            if m:
                target = m.group(1)
                hint = int(m.group(2)) if m.group(2) else None
            elif re.fullmatch(r"-?\d{5,}", t):
                target = int(t)
            else:
                raise ValueError("Send a message forwarded from the target, its @username, or a t.me link. "
                                 "(Invite links like t.me/+xxxx cannot be resolved by a bot.)")
        try:
            ent = await app.bot.get_entity(target)
        except Exception:
            raise ValueError("I cannot see that chat yet. Add the bot as admin there, then forward any message from it here.")
    if not isinstance(ent, (types.Channel, types.Chat)):
        raise ValueError("Please pick a channel or group (not a person).")
    return ent, hint


async def save_destination(app, user, chat_id: int, title: str, topic_id, topic_title: str, is_forum: bool) -> int:
    name = title + ((" › " + topic_title) if topic_id and topic_title else "")
    did = await app.db.execute(
        "INSERT INTO destinations(user_id,name,chat_id,topic_id,is_forum) VALUES(?,?,?,?,?)",
        (user["id"], name[:80], chat_id, topic_id or None, 1 if is_forum else 0))
    if not user["default_dest"]:
        await app.db.update_user(user["id"], default_dest=did)
    return did


# ------------------------------------------------------------------------- opening a link
async def with_cleanup(app) -> None:
    now = time.time()
    for sid in [k for k, v in app.sessions.items() if now - v.created > 3 * 3600]:
        app.sessions.pop(sid, None)
    for uid in [k for k, v in app.check_results.items() if now - v["t"] > 3600]:
        app.check_results.pop(uid, None)
    while len(app.sessions) > 100:
        app.sessions.pop(next(iter(app.sessions)))


async def start_browse(app, event, user, link) -> None:
    await with_cleanup(app)
    status = await event.respond("🔍 Reading link…")
    try:
        if link.kind == "file":
            name, size = UNKNOWN_NAME, 0
            try:
                name, size = await app.with_route(lambda creds: app.mega.file_info(link, creds))
            except MegaError as e:
                if e.kind in ("dead", "key"):
                    raise  # a transient problem only costs us the file name; a dead link is reported now
            nodes, title = [Node(0, link.id, name, name, size, False, -1)], "Single file"
        else:
            title, nodes = await app.with_route(lambda creds: app.mega.list_folder(link, creds))
            title = title or "Mega folder"
    except MegaError as e:
        msg = {"dead": "This link is dead, removed or empty.", "key": "The link's decryption key looks wrong or incomplete.",
               "quota": "Mega's limit is reached for every available route. Try later or add proxies."
               }.get(e.kind, "Could not read the link: %s" % str(e)[:150])
        await status.edit("🔴 " + S.esc(msg), buttons=[[S.B("🏠 Menu", "m:home")]], parse_mode="html")
        return
    sid = uuid.uuid4().hex[:8]
    s = BrowseSession(sid, user["id"], link, nodes, title)
    if link.sub_kind == "file" and link.sub_id:
        for n in nodes:
            if n.handle == link.sub_id and not n.is_dir:
                s.selected.add(n.idx)
    if link.kind == "file":
        s.selected.add(0)
    app.sessions[sid] = s
    text, buttons = await (S.summary(app, user, s) if link.kind == "file" else S.browser(app, user, s))
    await status.edit(text, buttons=buttons, parse_mode="html", link_preview=False)


# =========================================================================== callbacks
async def on_callback(app, event) -> None:
    data = (event.data or b"").decode()
    if data == "noop":
        await event.answer()
        return
    user = await authorize(app, event.sender_id)
    if user is None:
        await event.answer("Not authorised", alert=True)
        return
    parts = data.split(":")
    c = Ctx(app, event, user, parts)
    fn = CALLBACKS.get(parts[0])
    if fn:
        await fn(c)
    await c.ans()


async def need_admin(c: Ctx) -> bool:
    if S.is_admin(c.user):
        return True
    await c.ans("⛔ Admins only", alert=True)
    return False


# ------------------------------------------------------------------------- menu & settings
async def cb_menu(c: Ctx) -> None:
    app, user, act = c.app, c.user, c.parts[1]
    app.awaiting.pop(c.uid, None)
    if act == "home":
        await c.edit(await S.main_menu(app, user))
    elif act == "help":
        await c.edit(await S.help_screen(app, user))
    elif act == "new":
        await c.edit(await S.new_download_screen(app, user))
    elif act == "q":
        await c.edit(await S.queue_screen(app, user))
    elif act == "d":
        await c.edit(await S.dests_screen(app, user))
    elif act == "s":
        await c.edit(await S.stats_screen(app, user))
    elif act == "set":
        await c.edit(await S.settings_screen(app, user))
    elif act == "th":
        await c.edit(await S.theme_screen(app, user))
    elif act == "p" and await need_admin(c):
        await c.edit(await S.proxies_screen(app, user))
    elif act == "a" and await need_admin(c):
        await c.edit(await S.accounts_screen(app, user))


async def cb_theme(c: Ctx) -> None:
    if c.parts[1] in T.THEMES:
        await c.app.db.update_user(c.uid, theme=c.parts[1])
        await c.refresh_user()
    await c.edit(await S.theme_screen(c.app, c.user))


def _next(options: list, current):
    return options[(options.index(current) + 1) % len(options)] if current in options else options[0]


def _cycle(modes, current: str) -> str:
    keys = [k for k, _ in modes]
    return keys[(keys.index(current) + 1) % len(keys)] if current in keys else keys[0]


async def cb_settings(c: Ctx) -> None:
    app, db, uid, act = c.app, c.app.db, c.uid, c.parts[1]
    u = c.user
    if act == "anim":
        await db.update_user(uid, animations=0 if u["animations"] else 1)
    elif act == "sendas":
        await db.update_user(uid, send_as="document" if u["send_as"] == "auto" else "auto")
    elif act == "notify":
        await db.update_user(uid, notify=0 if u["notify"] else 1)
    elif act == "cap":
        set_awaiting(app, uid, "caption", c.msg_id)
        await c.edit(await S.prompt_screen(
            app, u, "Send the new caption template.\nFields: <code>{name} {size} {folder} {path} {ext} {index} {total} {root}</code>",
            "m:set"))
        return
    elif act == "pmode" and await need_admin(c):
        await db.set_setting("proxy_mode", _cycle(PROXY_MODES, await db.get_setting("proxy_mode", "direct_first")))
    elif act == "pchk" and await need_admin(c):
        cur = int(await db.get_setting("proxy_check_min", "60") or 0)
        await db.set_setting("proxy_check_min", _next(S.CHECK_INTERVALS, cur))
    elif act == "prm" and await need_admin(c):
        await db.set_setting("proxy_autoremove", "0" if await db.get_setting("proxy_autoremove", "0") == "1" else "1")
    elif act == "amode" and await need_admin(c):
        await db.set_setting("account_mode", _cycle(ACCOUNT_MODES, await db.get_setting("account_mode", "anon_first")))
    await c.refresh_user()
    await c.edit(await S.settings_screen(app, c.user))


# ------------------------------------------------------------------------- browser
async def cb_browse(c: Ctx) -> None:
    app, user, act = c.app, c.user, c.parts[1]
    sid = c.parts[2] if len(c.parts) > 2 else ""
    s = app.sessions.get(sid)
    if act == "cancel":
        app.sessions.pop(sid, None)
        await c.edit(await S.main_menu(app, user))
        return
    if s is None or s.uid != c.uid:
        await c.ans("This session expired — send the link again.", alert=True)
        return
    arg = c.parts[3] if len(c.parts) > 3 else ""
    if act == "open":
        s.cwd, s.page = int(arg), 0
    elif act == "tog":
        i = int(arg)
        s.selected.symmetric_difference_update({i})
    elif act == "pg":
        s.page = int(arg)
    elif act == "up":
        s.go_up()
    elif act == "all":
        s.selected.update(n.idx for n in s.files_under(s.cwd))
    elif act == "clr":
        s.selected.clear()
    elif act == "flt":
        await c.edit(await S.filter_screen(app, user, s))
        return
    elif act == "fx":
        if arg in S.FILTERS:
            s.fkey, s.page = arg, 0
    elif act == "fn":
        set_awaiting(app, c.uid, "bfilter", c.msg_id, sid=sid)
        await c.edit(await S.prompt_screen(app, user, "Send part of a file name to filter by (e.g. <code>S01E</code>).", "b:back:%s" % sid))
        return
    elif act == "sum":
        await c.edit(await S.summary(app, user, s))
        return
    elif act == "opt":
        await c.edit(await S.options_screen(app, user, s))
        return
    elif act == "ot":
        dest = await app.get_dest(user, s.dest_id)
        o = S.effective_options(user, s, dest)
        if arg == "sendas":
            s.options["send_as"] = "document" if o["send_as"] == "auto" else "auto"
        elif arg == "tpf":
            s.options["topic_per_folder"] = 0 if o["topic_per_folder"] else 1
        await c.edit(await S.options_screen(app, user, s))
        return
    elif act == "dest":
        await c.edit(await S.dest_pick(app, user, s))
        return
    elif act == "setd":
        s.dest_id = int(arg)
        s.options.pop("topic_per_folder", None)
        await c.edit(await S.summary(app, user, s))
        return
    elif act == "go":
        await start_job(c, s)
        return
    elif act == "back":
        pass
    await c.edit(await S.browser(app, user, s))


async def start_job(c: Ctx, s: BrowseSession) -> None:
    app, user, db = c.app, c.user, c.app.db
    nodes = s.selected_nodes()
    if not nodes:
        await c.ans("Select at least one file first.", alert=True)
        return
    dest = await app.get_dest(user, s.dest_id)
    o = S.effective_options(user, s, dest)
    total = sum(n.size for n in nodes)
    limit = user["daily_gb"] or app.cfg.default_daily_gb
    if limit and not S.is_admin(user):
        used = (await db.one("SELECT COALESCE(SUM(bytes),0) b FROM usage WHERE user_id=? AND day=date('now','localtime')", (user["id"],)))["b"]
        if used + total > limit * 1024 ** 3:
            await c.ans("Daily limit of %.1f GB would be exceeded." % limit, alert=True)
            return
    opts = {"send_as": o["send_as"], "topic_per_folder": o["topic_per_folder"], "caption": o["caption"], "root_name": s.title}
    jid = await db.execute(
        "INSERT INTO jobs(user_id,chat_id,msg_id,link,root_id,dest_id,status,options,total_files,total_bytes,created) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], c.chat_id, c.msg_id, s.link.url, s.link.id, dest["id"], "queued", json.dumps(opts), len(nodes), total, time.time()))
    await db.executemany(
        "INSERT INTO job_files(job_id,handle,path,name,size,seq,nkey) VALUES(?,?,?,?,?,?,?)",
        [(jid, n.handle or s.link.id, n.path, n.name, n.size, i, n.key) for i, n in enumerate(nodes)])
    app.sessions.pop(s.sid, None)
    await app.queue.enqueue(jid)
    await c.edit(await S.job_detail(app, user, jid))


# ------------------------------------------------------------------------- destinations
async def cb_dest(c: Ctx) -> None:
    app, db, user, act = c.app, c.app.db, c.user, c.parts[1]
    arg = int(c.parts[2]) if len(c.parts) > 2 and c.parts[2].lstrip("-").isdigit() else 0
    if act == "add":
        set_awaiting(app, c.uid, "dest", c.msg_id)
        await c.edit(await S.prompt_screen(
            app, user, "Add the bot as <b>admin</b> to the channel/group first. Then <b>forward any message</b> from it here, "
                       "or send its <code>@username</code> / a <code>t.me</code> link (a topic link selects that topic).", "m:d"))
        return
    if act == "tp":
        pend = app.pending_dest.pop(c.uid, None)
        if not pend:
            await c.ans("Selection expired — add the destination again.", alert=True)
            return
        topic = arg or None
        await save_destination(app, user, pend["chat_id"], pend["title"], topic, pend["topics"].get(arg, ""), True)
        await c.refresh_user()
        await c.edit(await S.dests_screen(app, c.user))
        return
    if act == "view":
        await c.edit(await S.dest_detail(app, user, arg))
        return
    d = await db.one("SELECT * FROM destinations WHERE id=? AND user_id=?", (arg, user["id"]))
    if not d:
        await c.edit(await S.dests_screen(app, user))
        return
    if act == "def":
        await db.update_user(c.uid, default_dest=arg)
    elif act == "tpf":
        await db.execute("UPDATE destinations SET topic_per_folder=1-topic_per_folder WHERE id=?", (arg,))
    elif act == "via":
        await db.execute("UPDATE destinations SET via=CASE via WHEN 'user' THEN 'bot' ELSE 'user' END WHERE id=?", (arg,))
    elif act == "test":
        try:
            client = app.uploader._client(d)
            msg = await client.send_message(await app.uploader._entity(d), "✅ Test message from the Mega bot", reply_to=d["topic_id"])
            asyncio.get_running_loop().call_later(6, lambda: asyncio.create_task(msg.delete()))
            await c.ans("✅ Test message sent (it deletes itself)", alert=True)
        except Exception as e:
            await c.ans("❌ Cannot post there: %s" % type(e).__name__, alert=True)
        return
    elif act == "del":
        await c.edit(await S.confirm_screen(app, user, "Delete <b>%s</b>?" % S.esc(d["name"]), "d:delc:%d" % arg, "d:view:%d" % arg))
        return
    elif act == "delc":
        await db.execute("DELETE FROM destinations WHERE id=?", (arg,))
        if user["default_dest"] == arg:
            await db.update_user(c.uid, default_dest=None)
        await c.refresh_user()
        await c.edit(await S.dests_screen(app, c.user))
        return
    await c.refresh_user()
    await c.edit(await S.dest_detail(app, c.user, arg))


# ------------------------------------------------------------------------- proxies
async def cb_proxy(c: Ctx) -> None:
    if not await need_admin(c):
        return
    app, db, user, act = c.app, c.app.db, c.user, c.parts[1]
    arg = c.parts[2] if len(c.parts) > 2 else ""
    pid = int(arg) if arg.isdigit() else 0
    pool = app.proxy_pool
    if act == "cancel":
        app.awaiting.pop(c.uid, None)
        await c.edit(await S.proxies_screen(app, user))
    elif act == "add":
        set_awaiting(app, c.uid, "proxy", c.msg_id)
        await c.edit(await S.prompt_screen(
            app, user, "Send proxies, one per line:\n<code>ip:port</code>\n<code>user:pass@ip:port</code>\n"
                       "<code>socks5://user:pass@ip:port</code>\nor upload a .txt file. Without a scheme I detect http / socks5 / socks4.",
            "p:cancel"))
    elif act == "chk":
        set_awaiting(app, c.uid, "pcheck", c.msg_id)
        await c.edit(await S.prompt_screen(app, user, CHECK_PROMPT, "p:cancel"))
    elif act == "cadd":
        data = app.check_results.get(c.uid)
        if not data:
            await c.ans("These results expired — run the check again.", alert=True)
            return
        added, dupes = await pool.add_checked(data["results"])
        await c.ans("✅ %d added · %d were already in the pool" % (added, dupes), alert=True)
    elif act == "cfile":
        data = app.check_results.get(c.uid)
        working = sorted((r for r in (data or {}).get("results", []) if r.ok), key=lambda r: r.latency or 99)
        if not working:
            await c.ans("Nothing to export.", alert=True)
            return
        bio = io.BytesIO("\n".join(r.url for r in working).encode())
        bio.name = "working_proxies.txt"
        await c.ans()
        await app.bot.send_file(c.chat_id, bio, caption="✅ %d working proxies (fastest first)" % len(working))
    elif act == "mode":
        await db.set_setting("proxy_mode", _cycle(PROXY_MODES, await db.get_setting("proxy_mode", "direct_first")))
        await c.edit(await S.proxies_screen(app, user))
    elif act == "view":
        await c.edit(await S.proxy_detail(app, user, pid))
    elif act == "test":
        await c.ans("🧪 Testing…")
        if arg == "all":
            await pool.test_all()
            await c.edit(await S.proxies_screen(app, user))
        else:
            await pool.test(pid)
            await c.edit(await S.proxy_detail(app, user, pid))
    elif act == "rst":
        await pool.reset(pid)
        await c.edit(await S.proxy_detail(app, user, pid))
    elif act == "del":
        await c.edit(await S.confirm_screen(app, user, "Remove proxy #%d?" % pid, "p:delc:%d" % pid, "p:view:%d" % pid))
    elif act == "delc":
        await pool.remove(pid)
        await c.edit(await S.proxies_screen(app, user))
    # ---- API sources
    elif act == "src":
        await c.edit(await S.sources_screen(app, user))
    elif act == "sadd":
        set_awaiting(app, c.uid, "psrc", c.msg_id)
        await c.edit(await S.prompt_screen(app, user, SOURCE_PROMPT, "p:cancel"))
    elif act == "sv":
        await c.edit(await S.source_detail(app, user, pid))
    elif act == "sf":
        await c.ans("⬇ Fetching…")
        res = await pool.refresh_source(pid)
        note = ("⚠ " + res["error"]) if "error" in res else (
            "✅ %d fetched · %d new · %d removed · checking in the background" % (res["fetched"], res["added"], res["removed"]))
        await c.edit(await S.source_detail(app, user, pid, note))
    elif act in ("ss", "si", "sy"):
        src = await pool.source(pid)
        if src:
            if act == "ss":
                await db.execute("UPDATE proxy_sources SET scheme=? WHERE id=?", (_next(S.SOURCE_SCHEMES, src["scheme"]), pid))
            elif act == "si":
                await db.execute("UPDATE proxy_sources SET interval_min=? WHERE id=?", (_next(S.SOURCE_INTERVALS, src["interval_min"]), pid))
            else:
                await db.execute("UPDATE proxy_sources SET sync=1-sync WHERE id=?", (pid,))
        await c.edit(await S.source_detail(app, user, pid))
    elif act == "sd":
        await c.edit(await S.source_delete_screen(app, user, pid))
    elif act == "sdc":
        await pool.delete_source(pid, delete_proxies=(c.parts[3] == "1" if len(c.parts) > 3 else False))
        await c.edit(await S.sources_screen(app, user))


# ------------------------------------------------------------------------- accounts
async def cb_account(c: Ctx) -> None:
    if not await need_admin(c):
        return
    app, db, user, act = c.app, c.app.db, c.user, c.parts[1]
    arg = c.parts[2] if len(c.parts) > 2 else ""
    aid = int(arg) if arg.isdigit() else 0
    if act == "cancel":
        app.awaiting.pop(c.uid, None)
        await c.edit(await S.accounts_screen(app, user))
    elif act == "add":
        set_awaiting(app, c.uid, "account", c.msg_id)
        await c.edit(await S.prompt_screen(
            app, user, "Send accounts, one per line:\n<code>email password</code>\nYour message is deleted right after.", "a:cancel"))
    elif act == "mode":
        await db.set_setting("account_mode", _cycle(ACCOUNT_MODES, await db.get_setting("account_mode", "anon_first")))
        await c.edit(await S.accounts_screen(app, user))
    elif act == "view":
        await c.edit(await S.account_detail(app, user, aid))
    elif act == "info":
        await c.ans("Checking…")
        await c.edit(await S.account_detail(app, user, aid, await app.accounts.info(aid)))
    elif act == "rst":
        await app.accounts.reset(aid)
        await c.edit(await S.account_detail(app, user, aid))
    elif act == "px":
        await app.accounts.cycle_proxy(aid)
        await c.edit(await S.account_detail(app, user, aid))
    elif act == "del":
        await c.edit(await S.confirm_screen(app, user, "Remove account #%d?" % aid, "a:delc:%d" % aid, "a:view:%d" % aid))
    elif act == "delc":
        await app.accounts.remove(aid)
        await c.edit(await S.accounts_screen(app, user))


# ------------------------------------------------------------------------- jobs
async def cb_job(c: Ctx) -> None:
    app, db, user, act = c.app, c.app.db, c.user, c.parts[1]
    jid = int(c.parts[2])
    job = await db.one("SELECT * FROM jobs WHERE id=?", (jid,))
    if not job or (job["user_id"] != c.uid and not S.is_admin(user)):
        await c.ans("Job not found.", alert=True)
        return
    q = app.queue
    running = jid in q.running
    if act == "pause" and running:
        q.get_ctl(jid).do_pause()
        await c.ans("Will pause after the current file.")
    elif act == "resume":
        q.get_ctl(jid).do_resume()
        await c.ans("Resuming…")
    elif act == "skip" and running:
        q.get_ctl(jid).do_skip()
        await c.ans("Skipping the current file…")
    elif act == "cancel":
        await q.cancel_job(jid)
        await c.ans("Cancelled.")
        if not running:
            await c.edit(await S.job_detail(app, user, jid))
    elif act == "retry":
        await q.retry_failed(jid)
        await c.ans("Queued again.")
        await c.edit(await S.job_detail(app, user, jid))
    elif act == "view":
        await c.edit(await S.job_detail(app, user, jid))


CALLBACKS = {"m": cb_menu, "th": cb_theme, "set": cb_settings, "b": cb_browse, "d": cb_dest,
             "p": cb_proxy, "a": cb_account, "j": cb_job}
