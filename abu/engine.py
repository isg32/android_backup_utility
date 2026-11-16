"""Backup engine: pulls files one by one, verifies them, survives pauses and disconnects."""

from __future__ import annotations

import hashlib
import os
import queue
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .adb import Adb
from .state import DONE, FAILED, PENDING, Item, StateDB

PART_SUFFIX = ".abu-part"
CAN_SUSPEND = hasattr(signal, "SIGSTOP")
BATCH_FILE_MAX = 4 << 20    # files up to 4 MiB are pulled in batches
BATCH_MAX_FILES = 100
BATCH_MAX_BYTES = 64 << 20


@dataclass
class Stats:
    state: str = "idle"  # idle | running | paused | waiting | done | stopped
    total_files: int = 0
    total_bytes: int = 0
    done_files: int = 0
    done_bytes: int = 0
    failed_files: int = 0
    skipped_files: int = 0
    copied_files: int = 0
    current_rel: str = ""
    current_size: int = 0
    current_bytes: int = 0
    current_attempt: int = 0
    session_bytes: int = 0  # bytes actually transferred in this run
    started: float = field(default_factory=time.monotonic)
    _samples: deque = field(default_factory=lambda: deque(maxlen=60))

    def sample(self) -> None:
        self._samples.append((time.monotonic(), self.session_bytes + self.current_bytes))

    @property
    def speed(self) -> float:
        now = time.monotonic()
        pts = [p for p in self._samples if now - p[0] <= 6]
        if len(pts) < 2 or pts[-1][0] == pts[0][0]:
            return 0.0
        return max(0.0, (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]))

    @property
    def progress_bytes(self) -> int:
        return self.done_bytes + self.current_bytes

    @property
    def eta(self) -> float | None:
        spd = self.speed
        if spd <= 0:
            return None
        return (self.total_bytes - self.progress_bytes) / spd


class BackupEngine(threading.Thread):
    def __init__(self, adb: Adb, db: StateDB, out_dir: Path, root: str = "/sdcard/",
                 verify_hash: bool = False, retries: int = 3) -> None:
        super().__init__(daemon=True, name="abu-engine")
        self.adb = adb
        self.db = db
        self.out_dir = out_dir
        self.root = root.rstrip("/") + "/"
        self.verify_hash = verify_hash
        self.retries = retries
        self.stats = Stats()
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._proc: subprocess.Popen | None = None

    # -------------------------------------------------------------- controls
    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def pause(self) -> None:
        self._pause.set()

    def resume(self) -> None:
        self._pause.clear()

    def stop(self) -> None:
        self._stop.set()
        self._pause.clear()

    def log(self, level: str, msg: str) -> None:
        self.events.put((level, msg))

    # ------------------------------------------------------------------ main
    def run(self) -> None:
        s = self.stats
        try:
            items = self.db.selected_items()
            s.total_files = len(items)
            s.total_bytes = sum(i.size for i in items)
            todo: list[Item] = []
            for it in items:
                if it.status == DONE and self._local_ok(it):
                    s.done_files += 1
                    s.done_bytes += it.size
                else:
                    if it.status == DONE:  # local copy vanished/changed: redo
                        self.db.mark(it.rel, PENDING)
                    todo.append(it)
            if s.done_files:
                self.log("info", f"{s.done_files:,} files already backed up — skipping them.")
            s.state = "running"
            s.started = time.monotonic()
            for job in self._jobs(todo):
                if self._stop.is_set():
                    break
                if len(job) == 1:
                    self._process(job[0])
                else:
                    self._process_batch(job)
            s.current_rel, s.current_bytes, s.current_size = "", 0, 0
            s.state = "stopped" if self._stop.is_set() else "done"
        except Exception as e:  # pragma: no cover - surfaced in UI
            self.log("error", f"Engine crashed: {e!r}")
            s.state = "stopped"

    # ------------------------------------------------------------- batching
    def _jobs(self, todo: list[Item]):
        """Yield single large files, or batches of small files sharing a folder.

        One `adb pull` per small file is dominated by process start-up and
        connection overhead; pulling many at once is several times faster.
        """
        todo = sorted(todo, key=lambda i: (os.path.dirname(i.rel), i.rel))
        batch: list[Item] = []
        batch_bytes = 0
        for it in todo:
            if self._already_present(it):
                continue
            parent = os.path.dirname(it.rel)
            small = it.size <= BATCH_FILE_MAX
            if batch and (
                not small
                or os.path.dirname(batch[0].rel) != parent
                or len(batch) >= BATCH_MAX_FILES
                or batch_bytes + it.size > BATCH_MAX_BYTES
            ):
                yield batch
                batch, batch_bytes = [], 0
            if small:
                batch.append(it)
                batch_bytes += it.size
            else:
                yield [it]
        if batch:
            yield batch

    def _already_present(self, it: Item) -> bool:
        """Identical file (size + mtime) already on disk, e.g. from an older backup."""
        try:
            st = self.local_path(it.rel).stat()
        except (OSError, ValueError):
            return False
        if st.st_size == it.size and int(st.st_mtime) == it.mtime:
            self.db.mark(it.rel, DONE, attempts=it.attempts)
            self.stats.done_files += 1
            self.stats.done_bytes += it.size
            self.stats.skipped_files += 1
            return True
        return False

    def _finalize(self, it: Item, src: Path, attempts: int) -> None:
        local = self.local_path(it.rel)
        local.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, local)
        try:
            os.utime(local, (it.mtime, it.mtime))
        except OSError:
            pass
        self.db.mark(it.rel, DONE, attempts=attempts, size=it.size)
        s = self.stats
        s.done_files += 1
        s.done_bytes += it.size
        s.session_bytes += it.size
        s.copied_files += 1

    def _process_batch(self, items: list[Item]) -> None:
        s = self.stats
        staging = self.out_dir / ".abu" / "staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        parent = os.path.dirname(items[0].rel)
        s.current_rel = f"{parent or '.'}/  [{len(items)} files]"
        s.current_size = sum(i.size for i in items)
        s.current_bytes = 0
        s.current_attempt = 1
        remotes = [self.root + i.rel for i in items]

        self._wait_while_paused()
        if self._stop.is_set():
            return
        s.state = "running"
        ok, _err = self._pull(remotes, staging, lambda: _dir_size(staging))

        # Keep every file that fully arrived (and matches its checksum);
        # anything else goes through the single-file path with retries.
        hashes: dict[str, str] = {}
        if self.verify_hash and not self._stop.is_set():
            s.state = "verifying"
            hashes = self.adb.md5_many(remotes)
            s.state = "running"
        s.current_bytes = 0
        leftovers: list[Item] = []
        for it, remote in zip(items, remotes):
            staged = staging / os.path.basename(it.rel)
            try:
                good = staged.stat().st_size == it.size
            except OSError:
                good = False
            if good and self.verify_hash:
                good = (not self._stop.is_set()) and hashes.get(remote) == _md5(staged)
            if good:
                self._finalize(it, staged, it.attempts + 1)
            else:
                leftovers.append(it)
        shutil.rmtree(staging, ignore_errors=True)
        if not ok and not self._stop.is_set() and not self.adb.is_connected():
            self._wait_for_device()
        for it in leftovers:
            if self._stop.is_set():
                break
            self._process(it)

    # --------------------------------------------------------------- helpers
    def local_path(self, rel: str) -> Path:
        p = (self.out_dir / rel).resolve()
        if not p.is_relative_to(self.out_dir.resolve()):
            raise ValueError(f"unsafe path: {rel}")
        return p

    def _local_ok(self, it: Item) -> bool:
        try:
            st = self.local_path(it.rel).stat()
        except (OSError, ValueError):
            return False
        return st.st_size == it.size

    def _wait_while_paused(self) -> None:
        if not self._pause.is_set():
            return
        prev = self.stats.state
        self.stats.state = "paused"
        while self._pause.is_set() and not self._stop.is_set():
            self.stats.sample()
            time.sleep(0.1)
        self.stats.state = prev if prev != "paused" else "running"

    def _wait_for_device(self) -> None:
        self.stats.state = "waiting"
        self.log("warn", "Device disconnected — waiting for it to come back…")
        while not self._stop.is_set():
            self._wait_while_paused()
            if self.adb.is_connected():
                self.log("ok", "Device reconnected — resuming.")
                break
            time.sleep(1)
        self.stats.state = "running"

    # --------------------------------------------------------------- process
    def _process(self, it: Item) -> None:
        s = self.stats
        local = self.local_path(it.rel)
        part = local.with_name(local.name + PART_SUFFIX)
        local.parent.mkdir(parents=True, exist_ok=True)
        s.current_rel, s.current_size, s.current_bytes = it.rel, it.size, 0
        attempts = it.attempts
        remote = self.root + it.rel

        while not self._stop.is_set():
            self._wait_while_paused()
            if self._stop.is_set():
                break
            s.state = "running"
            s.current_attempt = attempts + 1
            _unlink(part)
            ok, err = self._pull([remote], part, lambda: _file_size(part))
            if self._stop.is_set():
                break
            if ok:
                ok, err = self._verify(it, remote, part)
            if ok:
                self._finalize(it, part, attempts + 1)
                s.current_bytes = 0
                return

            _unlink(part)
            s.current_bytes = 0
            if err == "paused":
                continue
            if not self.adb.is_connected():
                # Disconnect: not the file's fault, don't burn an attempt.
                self.log("warn", f"Transfer interrupted: {it.rel}")
                self._wait_for_device()
                continue
            if "does not exist" in err or "No such file" in err:
                self._fail(it, attempts + 1, "file no longer exists on device")
                return
            attempts += 1
            if attempts >= self.retries:
                self._fail(it, attempts, err)
                return
            self.log("warn", f"Retry {attempts}/{self.retries - 1}: {it.rel} ({err})")
            time.sleep(1)

        # stopped mid-file: leave it pending for next run
        _unlink(part)
        self.db.mark(it.rel, PENDING, attempts=attempts)

    def _fail(self, it: Item, attempts: int, err: str) -> None:
        self.db.mark(it.rel, FAILED, error=err[:500], attempts=attempts)
        self.stats.failed_files += 1
        self.stats.done_bytes += it.size  # count it as "processed" for the bar
        self.stats.current_bytes = 0
        self.log("error", f"FAILED: {it.rel} — {err}")

    def _pull(self, remotes: list[str], dest: Path,
              measure: Callable[[], int]) -> tuple[bool, str]:
        s = self.stats
        proc = self.adb.pull_process(remotes, str(dest))
        self._proc = proc
        suspended = False
        try:
            while proc.poll() is None:
                if self._stop.is_set():
                    if suspended:
                        _signal(proc, signal.SIGCONT)
                    proc.terminate()
                    try:
                        proc.wait(3)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    return False, "stopped"
                if self._pause.is_set():
                    if CAN_SUSPEND and not suspended:
                        _signal(proc, signal.SIGSTOP)
                        suspended = True
                    elif not CAN_SUSPEND:  # no SIGSTOP: abort and restart the file
                        proc.terminate()
                        proc.wait()
                        self._wait_while_paused()
                        return False, "paused"
                    s.state = "paused"
                elif suspended:
                    _signal(proc, signal.SIGCONT)
                    suspended = False
                    s.state = "running"
                s.current_bytes = measure()
                s.sample()
                time.sleep(0.1)
            err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "replace")
            err = " ".join(err.split())[-300:]
            return proc.returncode == 0, err or f"adb exited with {proc.returncode}"
        finally:
            self._proc = None
            if proc.stderr:
                proc.stderr.close()

    def _verify(self, it: Item, remote: str, part: Path) -> tuple[bool, str]:
        try:
            got = part.stat().st_size
        except OSError:
            return False, "pulled file missing"
        if got != it.size:
            # File may have changed on device since the scan; re-check.
            now = self.adb.stat(remote)
            if now and now[0] == got:
                self.log("info", f"File changed on device since scan: {it.rel}")
                self.stats.total_bytes += got - it.size
                it.size, it.mtime = now
            else:
                return False, f"size mismatch (expected {it.size}, got {got})"
        if self.verify_hash:
            self.stats.state = "verifying"
            remote_md5 = self.adb.md5(remote)
            local_md5 = _md5(part)
            self.stats.state = "running"
            if not remote_md5:
                return False, "could not compute checksum on device"
            if remote_md5 != local_md5:
                return False, "checksum mismatch"
        return True, ""


def _md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _file_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _dir_size(p: Path) -> int:
    try:
        return sum(e.stat().st_size for e in os.scandir(p) if e.is_file())
    except OSError:
        return 0


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def _signal(proc: subprocess.Popen, sig: int) -> None:
    try:
        proc.send_signal(sig)
    except (ProcessLookupError, OSError):
        pass
