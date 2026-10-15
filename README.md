# Mega → Telegram bot

Paste a public **mega.nz** link (file or folder) → browse folders → tick one file or everything → the bot downloads
and uploads them **one by one** to your chat, a channel, a group or a **forum topic**. Animated, colourful, button-driven.

## Features
- **Public links, no login and no megatools needed** (new `/file/…#key`, `/folder/…#key`, `/folder/…/file/…` and legacy `#!` / `#F!` formats); built-in client lists folders, reads names/sizes and downloads + decrypts single files
- **Folder browser** with pagination, ☑ checkboxes, select-all-in-folder, filters (video / images / audio / archives / docs / name search), review screen
- **One at a time**: download → upload → delete temp file (low disk use); files over Telegram's limit are auto-split (`.001`, `.002`…)
- **Destinations**: this chat, channels, groups, individual **topics**, optional *topic per Mega subfolder*; saved with a default per user
- **Proxy pool** (http / https / socks4 / socks5): add from chat or .txt file, round-robin / sticky / direct-first, automatic cooldown + failover on Mega's bandwidth limit
- **Proxy checker**: test any list *before* adding it (reachability, latency, exit IP, IP-leak test, http/socks5/socks4 auto-detection), then add only the working ones; the pool is re-checked automatically
- **Proxy via API**: import from a provider's API or a list URL (text or JSON, paged, with API-key headers), optional auto-refresh that adds new proxies and drops ones the provider removed
- **Mega accounts** (optional, needs megatools): encrypted at rest, used as a fallback for **single-file links** when the anonymous quota is used up; files inside folder links are always fetched anonymously
- **Queue**: persists across restarts, pause / resume / skip / cancel / retry failed, duplicate-skip (re-sends an already uploaded file without downloading again)
- **UI**: single-message menus, 5 colour themes, animated progress bars + spinner, 🎉 on completion, `/animations`-style toggle for slow hosts
- **Multi-user**: owners / admins / allow-list, optional daily GB limit

## Setup
1. Create a bot with [@BotFather](https://t.me/BotFather) → `BOT_TOKEN`.
2. Get `API_ID` / `API_HASH` at <https://my.telegram.org> → *API development tools*.
3. `cp .env.example .env` and fill in `API_ID`, `API_HASH`, `BOT_TOKEN`, `OWNER_IDS` (your numeric Telegram id).

### Docker (works on any VPS / hosting that runs containers)
```bash
docker compose up -d --build
docker compose logs -f
```

### Without Docker (Linux / macOS / WSL)
```bash
sudo apt install ffmpeg curl python3-venv                  # (+ megatools only if you want Mega accounts)
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt && pip install cryptg      # cryptg = much faster uploads
python main.py
```

### megatools (optional)
The bot talks to Mega's public-link API **itself** (listing folders, reading names and sizes, downloading and decrypting single files),
using only `curl` and Python — **megatools is not required**. It is only used for the optional Mega-account feature, and only for
single-file links. If you want that: `sudo apt install megatools` (any version), or point `MEGATOOLS_BIN` at a binary installed elsewhere
(the project lives at https://xff.cz/megatools/ — `megatools dl|ls|df` subcommands and the classic `megadl` / `megals` / `megadf` names are both supported).

## Using it
- `/start` → menu. Send a link → browse → **Review & start**.
- **Destinations → Add**: make the bot *admin* of the channel/group, then forward any message from it (or send `@username` / a `t.me` link; a topic link selects that topic). Forum groups show a topic picker. 🧪 sends a self-deleting test message.
- **Proxies / Accounts** (admins): add via buttons or `/addproxy <list>` / `/addaccount email password` (your message is deleted). Formats: `ip:port`, `user:pass@ip:port`, `ip:port:user:pass`, `socks5://user:pass@ip:port`.
- **Proxy checker** (`🌐 Proxies → 🩺 Check a list`, or `/checkproxy <list>`, or send a .txt with that caption): tests every proxy against the
  Mega API and reports ✅/❌, latency and the exit IP. A proxy whose exit IP equals this machine's IP is rejected ("does not hide your IP"), which also
  catches a client that silently ignores the proxy. Lines without a scheme are tried as http → socks5 → socks4. Nothing is added until you press
  **➕ Add working**; **📄 Working list** sends the working ones back as a .txt (fastest first). Up to 300 proxies per run.
- **Proxy via API** (`🌐 Proxies → 🔗 Add via API`, or `/addproxyapi`): send the URL on the first line and optional header lines below it, e.g.
  ```
  https://proxy.webshare.io/api/v2/proxy/list/?mode=direct&page_size=100
  Authorization: Token YOUR_KEY
  ```
  Works with providers that return JSON (`proxy_address`/`ip`/`host` + `port`, optional `username`/`password`/`protocol`; entries flagged `valid: false`
  are skipped; `next` pages on the same host are followed) and with plain-text list URLs. Header values are stored encrypted and never shown;
  the URL's query string is hidden in chats; your message is deleted after reading. Each source (`📡 API sources`) has: **⬇ Fetch now**,
  **🔤 Scheme** (auto / http / socks5 / socks4 for entries without one), **⏱ Auto** refresh (off / 15 / 30 min / 1 h / 6 h) and
  **♻ Sync removals** (drop proxies the provider no longer lists; an empty or failed fetch never wipes the pool).
  The exact JSON field names differ between providers — if a provider's list is not recognised, send me a sample and I'll add its format.
- **Automatic health checks** (⚙ Settings, admins): re-test the whole pool every 15 / 30 / 60 / 180 min (default 60) and optionally auto-remove dead proxies.
  Dead proxies that start working again are revived automatically.
- Modes (⚙ Settings): *Proxy* – Direct first · Round-robin · Sticky · Direct only. *Accounts* – Anonymous first · Anonymous only · Accounts only.
- Admin commands: `/allow <id>`, `/deny <id>`, `/admin <id>` (owner), `/users`.

## Disk usage (by design: one file at a time)
- A file is **downloaded → uploaded → deleted** before the next one starts. The next download never begins while the previous file is still being uploaded, so normal use needs about **one file's size** of free disk.
- `MAX_FILES_ON_DISK=1` (default) is a hard cap across **all** jobs: even with `MAX_CONCURRENT=2` a second job waits for the first file to finish (its card shows "Waiting for another file"). Raise it only if you have plenty of disk.
- Files above Telegram's limit are cut into parts **one part at a time** (cut → upload → delete the part), so the disk holds the file plus at most one ≤ 1.9 GB part. If there is not enough room for that, the file fails with a clear message instead of filling the disk.
- Before each download the bot checks that the file's size plus `MIN_FREE_GB` (default 1 GB) is free; otherwise that file is skipped as failed ("not enough free disk space") and the job continues.
- Temp files are removed after every file, on cancel/failure, and at start-up (leftovers from a crash). 📊 Stats shows free disk for admins.

## Good to know
- **Upload limits**: bot over MTProto ≈ 2 GB per file. For 4 GB (Premium) run `python gen_session.py`, put the string in `USER_SESSION`, then switch a destination to *Upload via: user*.
- **Mega quota** is per IP (anonymous) or per account. On a bandwidth-limit error the route rests for `QUOTA_COOLDOWN_MIN`, the next route is tried, and if none is left the job waits (shown on the card) and resumes automatically.
- **Pause** takes effect after the current file; **Skip** aborts the current file immediately.
- If a download is interrupted (crash/restart) that file starts again from the beginning.
- Rotating proxies works around Mega's per-IP limits, which Mega's terms of service discourage — use responsibly and only for content you may download.
- Free public proxies are usually slow or already blocked; rotating residential proxies work far better.

## Layout
```
main.py · app.py · config.py · db.py          start-up, wiring, settings, SQLite
mega_api.py · mega_client.py                  built-in Mega public-link client (list / info / download+decrypt); link parsing; optional megatools wrapper
router.py · proxies.py · accounts.py          route selection, proxy pool + checker + API import, encrypted account pool
queue_worker.py · uploader.py                 job queue + live card, Telegram uploads/topics/splitting
browse.py · handlers.py                       folder browser state, commands + button callbacks
ui/ (themes, animations, keyboards, screens)  all colours, animation and screens
tests/                                        python -m unittest discover -s tests -t .   (runs without Telegram/Mega)
```
