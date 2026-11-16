"""Persistent per-file transfer state (SQLite), used for verification and resume."""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .adb import RemoteFile

PENDING, DONE, FAILED, CONFLICT = "pending", "done", "failed", "conflict"

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    rel      TEXT PRIMARY KEY,
    size     INTEGER NOT NULL,
    mtime    INTEGER NOT NULL,
    status   TEXT NOT NULL DEFAULT 'pending',
    selected INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 0,
    error    TEXT,
    updated  REAL
);
CREATE INDEX IF NOT EXISTS files_sel ON files(selected, status);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


@dataclass(slots=True)
class Item:
    rel: str
    size: int
    mtime: int
    status: str
    attempts: int


class StateDB:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ------------------------------------------------------------------ meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    # ------------------------------------------------------------------ plan
    def plan(self, selected: Iterable[RemoteFile]) -> None:
        """Record a new selection. Files already backed up (same size+mtime) stay done."""
        rows = [(f.rel, f.size, f.mtime) for f in selected]
        with self._lock:
            c = self.conn
            c.execute("BEGIN")
            try:
                c.execute("UPDATE files SET selected=0")
                c.executemany(
                    """
                    INSERT INTO files(rel,size,mtime,status,selected,attempts)
                    VALUES(?,?,?,'pending',1,0)
                    ON CONFLICT(rel) DO UPDATE SET
                        selected=1,
                        status=CASE WHEN files.status='done' AND files.size=excluded.size
                                     AND files.mtime=excluded.mtime
                                    THEN 'done' ELSE 'pending' END,
                        attempts=CASE WHEN files.status='done' AND files.size=excluded.size
                                       AND files.mtime=excluded.mtime
                                      THEN files.attempts ELSE 0 END,
                        error=NULL,
                        size=excluded.size,
                        mtime=excluded.mtime
                    """,
                    rows,
                )
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        self.set_meta("planned_at", str(time.time()))

    def selected_rels(self) -> set[str]:
        with self._lock:
            return {r[0] for r in self.conn.execute("SELECT rel FROM files WHERE selected=1")}

    def selected_items(self) -> list[Item]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT rel,size,mtime,status,attempts FROM files "
                "WHERE selected=1 ORDER BY rel"
            ).fetchall()
        return [Item(*r) for r in rows]

    def reset_failed(self) -> int:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE files SET status='pending', attempts=0, error=NULL "
                "WHERE selected=1 AND status='failed'"
            )
            return cur.rowcount

    def summary(self) -> dict[str, tuple[int, int]]:
        """status -> (count, bytes) for selected files."""
        with self._lock:
            rows = self.conn.execute(
                "SELECT status, COUNT(*), COALESCE(SUM(size),0) FROM files "
                "WHERE selected=1 GROUP BY status"
            ).fetchall()
        return {s: (n, b) for s, n, b in rows}

    def listing(self, status: str) -> list[tuple[str, str]]:
        """(rel, error) for selected files with the given status."""
        with self._lock:
            return self.conn.execute(
                "SELECT rel, COALESCE(error,'') FROM files "
                "WHERE selected=1 AND status=? ORDER BY rel", (status,)
            ).fetchall()

    # ---------------------------------------------------------------- update
    def mark(self, rel: str, status: str, *, error: str | None = None,
             attempts: int | None = None, size: int | None = None,
             mtime: int | None = None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE files SET status=?, error=?, updated=?, "
                "attempts=COALESCE(?, attempts), size=COALESCE(?, size), "
                "mtime=COALESCE(?, mtime) WHERE rel=?",
                (status, error, time.time(), attempts, size, mtime, rel),
            )
