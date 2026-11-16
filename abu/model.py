"""In-memory folder tree built from the device file listing, with tri-state selection."""

from __future__ import annotations

from typing import Iterable, Iterator

from .adb import RemoteFile

NONE, PARTIAL, ALL = 0, 1, 2


class FsNode:
    __slots__ = (
        "name", "rel", "is_dir", "size", "mtime", "count",
        "children", "parent", "sel_size", "sel_count",
    )

    def __init__(self, name: str, rel: str, is_dir: bool, parent: "FsNode | None" = None):
        self.name = name
        self.rel = rel
        self.is_dir = is_dir
        self.parent = parent
        self.size = 0
        self.mtime = 0
        self.count = 0  # number of files below (1 for a file)
        self.children: dict[str, FsNode] = {}
        self.sel_size = 0
        self.sel_count = 0

    # ---------------------------------------------------------------- queries
    @property
    def state(self) -> int:
        if self.sel_count == 0:
            return NONE
        return ALL if self.sel_count == self.count else PARTIAL

    def sorted_children(self) -> list["FsNode"]:
        return sorted(
            self.children.values(),
            key=lambda n: (not n.is_dir, n.name.startswith("."), n.name.lower()),
        )

    def iter_files(self) -> Iterator["FsNode"]:
        if not self.is_dir:
            yield self
            return
        stack = [self]
        while stack:
            node = stack.pop()
            for child in node.children.values():
                if child.is_dir:
                    stack.append(child)
                else:
                    yield child

    def iter_selected(self) -> Iterator["FsNode"]:
        for f in self.iter_files():
            if f.sel_count:
                yield f

    # -------------------------------------------------------------- mutation
    def set_selected(self, selected: bool) -> None:
        """Select/deselect this node and everything below it, updating ancestors."""
        old_size, old_count = self.sel_size, self.sel_count
        self._apply(selected)
        d_size, d_count = self.sel_size - old_size, self.sel_count - old_count
        p = self.parent
        while p is not None:
            p.sel_size += d_size
            p.sel_count += d_count
            p = p.parent

    def toggle(self) -> None:
        self.set_selected(self.state != ALL)

    def _apply(self, selected: bool) -> None:
        if not self.is_dir:
            self.sel_count = 1 if selected else 0
            self.sel_size = self.size if selected else 0
            return
        self.sel_size = self.sel_count = 0
        for child in self.children.values():
            child._apply(selected)
            self.sel_size += child.sel_size
            self.sel_count += child.sel_count


def build_tree(files: Iterable[RemoteFile], root_name: str = "/sdcard") -> FsNode:
    root = FsNode(root_name, "", True)
    for f in files:
        parts = f.rel.split("/")
        node = root
        node.size += f.size
        node.count += 1
        for i, part in enumerate(parts[:-1]):
            child = node.children.get(part)
            if child is None:
                child = FsNode(part, "/".join(parts[: i + 1]), True, node)
                node.children[part] = child
            child.size += f.size
            child.count += 1
            node = child
        leaf = FsNode(parts[-1], f.rel, False, node)
        leaf.size, leaf.mtime, leaf.count = f.size, f.mtime, 1
        node.children[parts[-1]] = leaf
    return root


def find(root: FsNode, rel: str) -> FsNode | None:
    node = root
    for part in rel.split("/"):
        node = node.children.get(part)  # type: ignore[assignment]
        if node is None:
            return None
    return node


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_time(seconds: float | None) -> str:
    if seconds is None or seconds != seconds or seconds == float("inf"):
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m" if h else f"{m:02d}:{s:02d}"


def plural(n: int, word: str) -> str:
    return f"{n:,} {word}" + ("" if n == 1 else "s")
