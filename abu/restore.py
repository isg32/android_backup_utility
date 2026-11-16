"""Restore engine: pushes files from a backup folder back onto the device.

Every push lands in a hidden staging folder on the device first
(`<root>/.abu-staging/`, marked `.nomedia`). abu checks its size there (and its MD5
with --verify-hash), then moves it into place with `mv`. A half-pushed file
therefore never appears in the gallery or overwrites a good copy.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path

from .adb import Adb, AdbError, RemoteFile
from .model import plural
from .engine import PART_SUFFIX, TransferEngine, _md5
from .state import CONFLICT, DONE, PENDING, Item, StateDB

STAGING = ".abu-staging"


def scan_local(src: Path) -> list[RemoteFile]:
    """List files in a backup folder (skipping abu's own bookkeeping)."""
    files: list[RemoteFile] = []
    src = src.resolve()
    for dirpath, dirnames, filenames in os.walk(src):
        if dirpath == str(src):
            dirnames[:] = [d for d in dirnames if d != ".abu"]
        for name in filenames:
            if name.endswith(PART_SUFFIX):
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            rel = os.path.relpath(full, src).replace(os.sep, "/")
            files.append(RemoteFile(rel, st.st_size, int(st.st_mtime)))
    return files


class RestoreEngine(TransferEngine):
    def __init__(self, adb: Adb, db: StateDB, src_dir: Path, root: str = "/sdcard/",
                 remote_files: dict[str, tuple[int, int]] | None = None,
                 overwrite: bool = False, verify_hash: bool = False,
                 retries: int = 3) -> None:
        super().__init__(adb, db, verify_hash, retries)
        self.src_dir = src_dir.resolve()
        self.root = root.rstrip("/") + "/"
        self.staging = self.root + STAGING + "/"
        self.remote_files = remote_files
        self.overwrite = overwrite

    # ------------------------------------------------------------------ main
    def run(self) -> None:
        s = self.stats
        try:
            items = self.db.selected_items()
            s.total_files = len(items)
            s.total_bytes = sum(i.size for i in items)
            remote = self.remote_files
            if remote is None:
                s.state = "scanning"
                self.log("info", f"Checking what is already on the device under {self.root}…")
                remote = {f.rel: (f.size, f.mtime) for f in self.adb.scan(self.root)}

            todo: list[Item] = []
            for it in items:
                on_device = remote.get(it.rel)
                if on_device and on_device[0] == it.size:
                    if it.status != DONE:
                        self.db.mark(it.rel, DONE, attempts=it.attempts)
                    self._count_done(it, copied=False)
                    continue
                if not (self.src_dir / it.rel).is_file():
                    self._fail(it, it.attempts, "missing from the backup folder")
                    continue
                if on_device and not self.overwrite:
                    self.db.mark(it.rel, CONFLICT,
                                 error=f"exists on device with a different size "
                                       f"({on_device[0]} vs {it.size} bytes)")
                    s.conflict_files += 1
                    s.done_bytes += it.size
                    continue
                if it.status != PENDING:
                    self.db.mark(it.rel, PENDING, attempts=it.attempts)
                todo.append(it)

            if s.skipped_files:
                self.log("info", f"{plural(s.skipped_files, 'file')} already on the device — skipping.")
            if s.conflict_files:
                self.log("warn", f"{plural(s.conflict_files, 'file')} differ from the device copy "
                                 "and will be left alone (use --overwrite to replace).")
            s.state = "running"
            for job in self.group_jobs(todo):
                if self._stop.is_set():
                    break
                self._process(job)
            s.current_rel, s.current_bytes, s.current_size = "", 0, 0
            self._cleanup_staging()
            if self._stop.is_set():
                s.state = "stopped"
                return
            if s.copied_files:
                s.state = "scanning"
                self.log("info", "Asking Android to index the restored media…")
                if self.adb.media_scan():
                    self.log("ok", "Media scan requested — photos and music will appear in apps shortly.")
            s.state = "done"
        except Exception as e:  # pragma: no cover - surfaced in UI
            self.log("error", f"Engine crashed: {e!r}")
            s.state = "stopped"

    # --------------------------------------------------------------- helpers
    def _sh(self, cmd: str, timeout: float = 60) -> bool:
        try:
            self.adb.shell(cmd, timeout=timeout)
            return True
        except (AdbError, Exception):
            return False

    def _cleanup_staging(self) -> None:
        self._sh(f"rm -rf {shlex.quote(self.staging)}")

    # --------------------------------------------------------------- process
    def _process(self, items: list[Item]) -> None:
        """Push a batch (or a single file); on partial failure, retry per file."""
        s = self.stats
        attempts = max(i.attempts for i in items)
        parent = os.path.dirname(items[0].rel)
        final_dir = self.root + (parent + "/" if parent else "")
        stg = self.staging

        while not self._stop.is_set():
            self._wait_while_paused()
            if self._stop.is_set():
                break
            s.state = "running"
            s.current_rel = (items[0].rel if len(items) == 1
                             else f"{parent or '.'}/  [{len(items)} files]")
            s.current_size = sum(i.size for i in items)
            s.current_bytes = 0
            s.current_attempt = attempts + 1

            q = shlex.quote
            prepared = self._sh(f"rm -rf {q(stg)} && mkdir -p {q(stg)} && touch {q(stg + '.nomedia')}")
            if prepared:
                locals_ = [str(self.src_dir / i.rel) for i in items]
                ok, err = self._run_adb(
                    self.adb.push_process(locals_, stg),
                    lambda: self.adb.dir_size(stg), measure_every=5,
                )
            else:
                ok, err = False, "could not prepare staging folder on device"
            if self._stop.is_set():
                break
            if err == "paused":
                continue

            good = self._verify_and_commit(items, final_dir) if prepared else []
            s.current_bytes = 0
            for it in good:
                self.db.mark(it.rel, DONE, attempts=attempts + 1)
                self._count_done(it, copied=True)
            leftovers = [i for i in items if i not in good]
            if not leftovers:
                return

            if not self.adb.is_connected():
                self.log("warn", f"Transfer interrupted: {s.current_rel}")
                self._wait_for_device()
                items = leftovers
                continue
            if len(leftovers) > 1 or len(items) > 1:
                # Batch partly failed: give each remaining file its own retries.
                for it in leftovers:
                    if self._stop.is_set():
                        break
                    self._process([it])
                return
            it = leftovers[0]
            if ok:
                err = "size or checksum mismatch after push"
            attempts += 1
            if attempts >= self.retries:
                self._fail(it, attempts, err)
                return
            self.log("warn", f"Retry {attempts}/{self.retries - 1}: {it.rel} ({err})")
            items = leftovers

        for it in items:  # stopped: leave for the next run
            self.db.mark(it.rel, PENDING, attempts=attempts)

    def _verify_and_commit(self, items: list[Item], final_dir: str) -> list[Item]:
        """Check staged files, move good ones into place, return those that made it."""
        stg = self.staging
        staged = {stg + os.path.basename(i.rel): i for i in items}
        sizes = self.adb.stat_many(list(staged))
        good = [i for p, i in staged.items() if sizes.get(p, (-1,))[0] == i.size]
        if good and self.verify_hash:
            self.stats.state = "verifying"
            hashes = self.adb.md5_many([stg + os.path.basename(i.rel) for i in good])
            good = [i for i in good
                    if hashes.get(stg + os.path.basename(i.rel)) == _md5(self.src_dir / i.rel)]
            self.stats.state = "running"
        if not good:
            return []
        q = shlex.quote
        names = " ".join(q(stg + os.path.basename(i.rel)) for i in good)
        self._sh(f"mkdir -p {q(final_dir)} && mv -f {names} {q(final_dir)}", timeout=120)
        # Confirm they really landed (mv can fail per file).
        final = self.adb.stat_many([self.root + i.rel for i in good])
        return [i for i in good if final.get(self.root + i.rel, (-1,))[0] == i.size]
