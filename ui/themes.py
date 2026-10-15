"""Colour themes: each one swaps emoji sets, progress-bar glyphs and spinner frames."""
from __future__ import annotations

DIVIDER = "━━━━━━━━━━━━━━━━━━"

THEMES = {
    "ocean": {"label": "🌊 Ocean", "icon": "🌊", "accent": "🔵", "full": "🟦", "empty": "⬜",
              "head": ["🔹", "🟦"], "spin": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"},
    "forest": {"label": "🌲 Forest", "icon": "🌲", "accent": "🟢", "full": "🟩", "empty": "⬜",
               "head": ["🍀", "🟩"], "spin": "⠁⠂⠄⡀⢀⠠⠐⠈"},
    "sunset": {"label": "🌅 Sunset", "icon": "🌅", "accent": "🟠", "full": "🟧", "empty": "⬜",
               "head": ["🔸", "🟨"], "spin": "◐◓◑◒"},
    "neon": {"label": "⚡ Neon", "icon": "⚡", "accent": "🟣", "full": "🟪", "empty": "⬛",
             "head": ["💜", "🟪"], "spin": "▖▘▝▗"},
    "mono": {"label": "⚪ Mono", "icon": "▪️", "accent": "⚪", "full": "▰", "empty": "▱",
             "head": ["▰", "▱"], "spin": "|/-\\"},
}


def get(name: str) -> dict:
    return THEMES.get(name or "", THEMES["ocean"])
