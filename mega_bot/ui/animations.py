"""Formatting helpers: sizes, speeds, ETA, animated progress bars and spinners."""
from __future__ import annotations


def fmt_size(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "0 B"


def fmt_speed(bps) -> str:
    return (fmt_size(bps) + "/s") if bps and bps > 0 else "—"


def fmt_eta(sec) -> str:
    if sec is None or sec <= 0 or sec == float("inf"):
        return "—"
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%02d:%02d" % (m, s))


def bar(pct: float, th: dict, frame: int = 0, width: int = 10) -> str:
    """Progress bar whose leading cell pulses between two glyphs on every frame."""
    pct = max(0.0, min(100.0, float(pct)))
    filled = int(pct / 100.0 * width)
    out = th["full"] * filled
    if filled < width:
        heads = th["head"]
        out += heads[frame % len(heads)] + th["empty"] * (width - filled - 1)
    return out


def spinner(th: dict, frame: int) -> str:
    return th["spin"][frame % len(th["spin"])]


def short(text: str, n: int = 28) -> str:
    text = text or ""
    return text if len(text) <= n else text[: n - 1] + "…"


class Ema:
    """Exponential moving average, used to smooth the transfer speed."""

    def __init__(self, alpha: float = 0.3):
        self.alpha = alpha
        self.value = 0.0

    def update(self, x: float) -> float:
        self.value = x if self.value == 0 else self.alpha * x + (1 - self.alpha) * self.value
        return self.value
