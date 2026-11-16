from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__


def main() -> None:
    p = argparse.ArgumentParser(
        prog="abu",
        description="Back up an Android device's /sdcard over adb with a resumable TUI.",
    )
    p.add_argument("output", nargs="?", type=Path,
                   help="backup folder (default: ./backups/<model>_<serial>). "
                        "Re-use the same folder to resume or update a backup.")
    p.add_argument("-s", "--serial", help="device serial (when several are connected)")
    p.add_argument("--verify-hash", action="store_true",
                   help="also compare MD5 checksums (slower, strongest check)")
    p.add_argument("--retries", type=int, default=3,
                   help="attempts per file before marking it failed (default: 3)")
    p.add_argument("--include-android", action="store_true",
                   help="pre-select the Android/ folder (excluded by default)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--resume", dest="resume", action="store_true", default=None,
                   help="resume an unfinished backup without asking")
    g.add_argument("--new", dest="resume", action="store_false",
                   help="ignore unfinished work and pick folders again")
    p.add_argument("-V", "--version", action="version", version=f"abu {__version__}")
    a = p.parse_args()

    from .app import AbuApp

    AbuApp(a.output, a.serial, a.verify_hash, max(1, a.retries),
           a.include_android, a.resume).run()


if __name__ == "__main__":
    main()
