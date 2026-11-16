"""Thin wrapper around the `adb` command line tool."""

from __future__ import annotations

import shlex
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable


class AdbError(RuntimeError):
    pass


@dataclass(slots=True)
class RemoteFile:
    rel: str  # path relative to the scan root, e.g. "DCIM/Camera/IMG_1.jpg"
    size: int
    mtime: int


@dataclass(slots=True)
class Device:
    serial: str
    state: str
    description: str


class Adb:
    def __init__(self, serial: str | None = None, adb_path: str | None = None) -> None:
        path = adb_path or shutil.which("adb")
        if not path:
            raise AdbError("`adb` was not found in PATH. Install Android platform-tools.")
        self.path = path
        self.serial = serial

    # ------------------------------------------------------------------ basics
    def _cmd(self, *args: str) -> list[str]:
        base = [self.path]
        if self.serial:
            base += ["-s", self.serial]
        return base + list(args)

    def run(self, *args: str, timeout: float | None = 30) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            self._cmd(*args), capture_output=True, text=True, timeout=timeout,
            errors="replace",
        )

    def devices(self) -> list[Device]:
        out = subprocess.run(
            [self.path, "devices", "-l"], capture_output=True, text=True, timeout=30
        ).stdout
        devices = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2:
                devices.append(Device(parts[0], parts[1], " ".join(parts[2:])))
        return devices

    def state(self) -> str:
        """Return 'device' when the device is connected and usable."""
        try:
            res = self.run("get-state", timeout=5)
        except subprocess.TimeoutExpired:
            return "timeout"
        return res.stdout.strip() if res.returncode == 0 else "disconnected"

    def is_connected(self) -> bool:
        return self.state() == "device"

    def shell(self, command: str, timeout: float | None = 30) -> str:
        res = self.run("exec-out", command, timeout=timeout)
        if res.returncode != 0:
            raise AdbError(res.stderr.strip() or f"adb shell failed: {command}")
        return res.stdout

    def prop(self, name: str) -> str:
        try:
            return self.shell(f"getprop {shlex.quote(name)}", timeout=10).strip()
        except (AdbError, subprocess.TimeoutExpired):
            return ""

    # -------------------------------------------------------------------- scan
    def scan(
        self, root: str = "/sdcard/", on_progress: Callable[[int], None] | None = None
    ) -> list[RemoteFile]:
        """List every regular file under `root` with its size and mtime."""
        root = root.rstrip("/") + "/"
        cmd = (
            f"find {shlex.quote(root)} -type f -exec stat -c '%s|%Y|%n' {{}} + "
            "2>/dev/null"
        )
        proc = subprocess.Popen(
            self._cmd("exec-out", cmd), stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        files: list[RemoteFile] = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.decode("utf-8", "surrogateescape").rstrip("\r\n")
            parts = line.split("|", 2)
            if len(parts) != 3 or not parts[2].startswith(root):
                continue
            try:
                size, mtime = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            rel = parts[2][len(root):]
            if rel:
                files.append(RemoteFile(rel, size, mtime))
                if on_progress and len(files) % 250 == 0:
                    on_progress(len(files))
        proc.wait()
        if proc.returncode not in (0, 1) and not files:  # find returns 1 on perms
            raise AdbError("Failed to list files on device (is it still connected?)")
        if on_progress:
            on_progress(len(files))
        return files

    def stat(self, remote: str) -> tuple[int, int] | None:
        try:
            out = self.shell(f"stat -c '%s|%Y' {shlex.quote(remote)}", timeout=15)
            size, mtime = out.strip().split("|")
            return int(size), int(mtime)
        except Exception:
            return None

    def md5(self, remote: str) -> str | None:
        try:
            out = self.shell(f"md5sum {shlex.quote(remote)}", timeout=None)
            digest = out.split()[0]
            return digest if len(digest) == 32 else None
        except Exception:
            return None

    # -------------------------------------------------------------------- pull
    def md5_many(self, remotes: list[str]) -> dict[str, str]:
        """MD5 of several device files with a single shell round-trip."""
        if not remotes:
            return {}
        cmd = "md5sum " + " ".join(shlex.quote(r) for r in remotes) + " 2>/dev/null"
        try:
            out = self.shell(cmd, timeout=None)
        except Exception:
            out = ""
        result = {}
        for line in out.splitlines():
            digest, _, path = line.partition("  ")
            if len(digest) == 32 and path:
                result[path] = digest
        return result

    def stat_many(self, remotes: list[str]) -> dict[str, tuple[int, int]]:
        """path -> (size, mtime) for each of `remotes` that exists."""
        if not remotes:
            return {}
        cmd = ("stat -c '%s|%Y|%n' " + " ".join(shlex.quote(r) for r in remotes)
               + " 2>/dev/null")
        res = self.run("exec-out", cmd, timeout=60)
        out: dict[str, tuple[int, int]] = {}
        for line in res.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) == 3:
                try:
                    out[parts[2]] = (int(parts[0]), int(parts[1]))
                except ValueError:
                    pass
        return out

    def dir_size(self, remote_dir: str) -> int:
        """Total size of regular files directly inside `remote_dir` (0 on error)."""
        cmd = f"find {shlex.quote(remote_dir)} -type f -exec stat -c %s {{}} + 2>/dev/null"
        try:
            res = self.run("exec-out", cmd, timeout=10)
            return sum(int(x) for x in res.stdout.split() if x.isdigit())
        except Exception:
            return 0

    def push_process(self, locals_: list[str], remote: str) -> subprocess.Popen[bytes]:
        """`adb push` one or more local files into existing remote directory `remote`."""
        return subprocess.Popen(
            self._cmd("push", *locals_, remote),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def media_scan(self) -> bool:
        """Ask MediaProvider to rescan shared storage so restored media shows up."""
        try:
            self.shell("content call --uri content://media --method scan_volume "
                       "--arg external_primary", timeout=120)
            return True
        except Exception:
            return False

    def pull_process(self, remotes: list[str], local: str) -> subprocess.Popen[bytes]:
        """`adb pull` one file to `local`, or several files into directory `local`."""
        return subprocess.Popen(
            self._cmd("pull", *remotes, local),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

