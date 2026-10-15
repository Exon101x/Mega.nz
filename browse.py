"""State for the interactive folder browser (one session per link the user opens)."""
from __future__ import annotations

import os
import time
from typing import Dict, List, Optional

from mega_client import MegaLink, Node

VIDEO = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".ts", ".flv", ".wmv", ".mpg", ".mpeg"}
IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff", ".heic"}
AUDIO = {".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
ARCHIVE = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"}
DOCS = {".pdf", ".epub", ".mobi", ".doc", ".docx", ".txt", ".xls", ".xlsx", ".ppt", ".pptx", ".csv"}

FILTERS = {
    "all": ("📄 All files", None),
    "video": ("🎬 Video", VIDEO),
    "image": ("🖼 Images", IMAGE),
    "audio": ("🎵 Audio", AUDIO),
    "archive": ("📦 Archives", ARCHIVE),
    "doc": ("📑 Documents", DOCS),
}


def file_icon(name: str) -> str:
    ext = os.path.splitext(name)[1].lower()
    for icon, exts in (("🎬", VIDEO), ("🖼", IMAGE), ("🎵", AUDIO), ("📦", ARCHIVE), ("📑", DOCS)):
        if ext in exts:
            return icon
    return "📄"


class BrowseSession:
    PER_PAGE = 8

    def __init__(self, sid: str, uid: int, link: MegaLink, nodes: List[Node], title: str):
        self.sid, self.uid, self.link, self.nodes, self.title = sid, uid, link, nodes, title
        self.cwd = -1
        self.page = 0
        self.selected: set = set()
        self.fkey = "all"
        self.name_filter = ""
        self.options: Dict[str, object] = {}
        self.dest_id: Optional[int] = None
        self.created = time.time()
        self.children_of: Dict[int, List[int]] = {}
        for n in nodes:
            self.children_of.setdefault(n.parent, []).append(n.idx)

    # -- filtering ---------------------------------------------------------
    def match(self, n: Node) -> bool:
        exts = FILTERS[self.fkey][1]
        if exts is not None and os.path.splitext(n.name)[1].lower() not in exts:
            return False
        return not self.name_filter or self.name_filter.lower() in n.name.lower()

    @property
    def filtered(self) -> bool:
        return self.fkey != "all" or bool(self.name_filter)

    def files_under(self, idx: int) -> List[Node]:
        out, stack = [], [idx]
        while stack:
            p = stack.pop()
            for c in self.children_of.get(p, []):
                n = self.nodes[c]
                if n.is_dir:
                    stack.append(c)
                elif self.match(n):
                    out.append(n)
        return sorted(out, key=lambda n: n.idx)

    def children(self) -> List[Node]:
        """Visible entries in the current folder: folders first, then files."""
        items = [self.nodes[c] for c in self.children_of.get(self.cwd, [])]
        dirs = [n for n in items if n.is_dir and (not self.filtered or self.files_under(n.idx))]
        files = [n for n in items if not n.is_dir and self.match(n)]
        return dirs + files

    def pages(self) -> int:
        return max(1, -(-len(self.children()) // self.PER_PAGE))

    def page_items(self) -> List[Node]:
        self.page = max(0, min(self.page, self.pages() - 1))
        return self.children()[self.page * self.PER_PAGE:(self.page + 1) * self.PER_PAGE]

    # -- navigation / selection -------------------------------------------
    def breadcrumb(self) -> str:
        parts, i = [], self.cwd
        while i != -1:
            parts.append(self.nodes[i].name)
            i = self.nodes[i].parent
        return " › ".join([self.title] + parts[::-1])

    def go_up(self) -> None:
        if self.cwd != -1:
            self.cwd = self.nodes[self.cwd].parent
            self.page = 0

    def selected_nodes(self) -> List[Node]:
        return [self.nodes[i] for i in sorted(self.selected) if not self.nodes[i].is_dir]

    def selected_size(self) -> int:
        return sum(n.size for n in self.selected_nodes())

    def dir_summary(self, idx: int) -> str:
        files = [n for n in self.files_under(idx)]
        return "%d" % len(files)
