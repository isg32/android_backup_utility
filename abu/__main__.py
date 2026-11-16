from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__

EPILOG = """\
commands:
  abu [backup] [OUTPUT_DIR]   back up the device's /sdcard (default command)
  abu restore BACKUP_DIR      push a backup folder back onto the device

Run `abu backup -h` or `abu restore -h` for each command's options.
"""


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-s", "--serial", help="device serial (when several are connected)")
    p.add_argument("--verify-hash", action="store_true",
                   help="also compare MD5 checksums (slower, strongest check)")
    p.add_argument("--retries", type=int, default=3,
                   help="attempts per file before marking it failed (default: 3)")
    p.add_argument("--include-android", action="store_true",
                   help="pre-select the Android/ folder (excluded by default)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--resume", dest="resume", action="store_true", default=None,
                   help="resume an unfinished run without asking")
    g.add_argument("--new", dest="resume", action="store_false",
                   help="ignore unfinished work and pick folders again")
    p.add_argument("-V", "--version", action="version", version=f"abu {__version__}")


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = "backup"
    if argv and argv[0] in ("backup", "restore"):
        mode = argv.pop(0)

    if mode == "restore":
        p = argparse.ArgumentParser(
            prog="abu restore",
            description="Restore files from an abu backup folder onto the device.",
        )
        p.add_argument("backup_dir", type=Path, help="backup folder created by `abu backup`")
        p.add_argument("--to", default=None, metavar="REMOTE_DIR",
                       help="device folder to restore into (default: /sdcard/)")
        p.add_argument("--overwrite", action="store_true",
                       help="replace device files that differ from the backup "
                            "(default: leave them untouched)")
    else:
        p = argparse.ArgumentParser(
            prog="abu",
            description="Back up an Android device's /sdcard over adb with a resumable TUI.",
            epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter,
        )
        p.add_argument("output", nargs="?", type=Path,
                       help="backup folder (default: ./backups/<model>_<serial>). "
                            "Re-use the same folder to resume or update a backup.")
    _common(p)
    a = p.parse_args(argv)

    from .app import AbuApp

    if mode == "restore":
        app = AbuApp(a.backup_dir, a.serial, a.verify_hash, max(1, a.retries),
                     a.include_android, a.resume, mode="restore",
                     remote_root=a.to, overwrite=a.overwrite)
    else:
        app = AbuApp(a.output, a.serial, a.verify_hash, max(1, a.retries),
                     a.include_android, a.resume)
    app.run()


if __name__ == "__main__":
    main()
