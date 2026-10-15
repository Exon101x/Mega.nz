"""Button builders. Callback data is kept short (Telegram allows 64 bytes)."""
from __future__ import annotations

from telethon import Button


def B(text: str, data: str):
    raw = data.encode()
    if len(raw) > 64:
        raise ValueError("callback data too long: %s" % data)
    return Button.inline(text, raw)


def grid(buttons: list, cols: int = 2) -> list:
    return [buttons[i:i + cols] for i in range(0, len(buttons), cols)]


def pager(prefix: str, page: int, pages: int) -> list:
    """Returns a list with one row: « ‹ 2/5 › »  (empty when there is a single page)."""
    if pages <= 1:
        return []
    row = []
    if page > 0:
        row += [B("«", "%s:0" % prefix), B("‹", "%s:%d" % (prefix, page - 1))]
    row.append(B("%d/%d" % (page + 1, pages), "noop"))
    if page < pages - 1:
        row += [B("›", "%s:%d" % (prefix, page + 1)), B("»", "%s:%d" % (prefix, pages - 1))]
    return [row]


def footer(back: str = "", home: bool = True) -> list:
    row = []
    if back:
        row.append(B("⬅ Back", back))
    if home:
        row.append(B("🏠 Menu", "m:home"))
    return [row] if row else []
