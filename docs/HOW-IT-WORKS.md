# How abu works

A technical tour for anyone reading or changing the code.

## Module map

```
abu/
├── __main__.py   CLI (argparse) → starts AbuApp
├── adb.py        Wrapper around the `adb` binary: devices, scan, stat, md5, pull
├── model.py      In-memory folder tree (FsNode) with tri-state selection + size helpers
├── state.py      SQLite store of per-file status (the source of truth for resume)
├── engine.py     BackupEngine thread: batching, pull, verify, pause, reconnect, retry
├── app.py        Textual UI: screens, check-tree widget, modals, wiring
└── app.tcss      Styles for the UI
```

## Flow

```
start
  │
  ├─ find device ──(several)──► DevicePicker modal
  │
  ├─ open <out>/.abu/state.db
  │     └─ unfinished selected files? ──► ResumeModal
  │                                         ├─ Resume ─────────────────────┐
  │                                         └─ New selection ─┐            │
  ├─ scan /sdcard  (one `find … -exec stat` on the device)    │            │
  ├─ SelectScreen  (CheckTree)  ◄─────────────────────────────┘            │
  │     └─ enter → db.plan(selected files)                                 │
  └─ BackupScreen ── starts BackupEngine thread ◄──────────────────────────┘
```

## Scanning

A single command lists every file with its size and modified time:

```sh
find /sdcard/ -type f -exec stat -c '%s|%Y|%n' {} + 2>/dev/null
```

The output is streamed and split into at most 3 parts on `|`, so file names that contain `|` still work. On a phone with ~19k files this takes about 5 seconds. The list is turned into an `FsNode` tree:

- every folder stores `size` and `count`, the totals for everything inside it;
- every folder also stores `sel_size` and `sel_count`, the totals for what is selected inside it;
- ticking a node updates everything below it, then adds the size and count difference to each parent folder. Summary updates are therefore cheap, and a folder's mark (`■ ◧ □`) is simply `sel_count` compared with `count`.

The Textual `Tree` widget only builds the items for a folder when that folder is first opened, so even the 12k files in `Android/` cost nothing until you look at them.

## Saved state (`.abu/state.db`)

```sql
files(rel PRIMARY KEY, size, mtime, status, selected, attempts, error, updated)
meta(key PRIMARY KEY, value)
```

- `status` is one of `pending`, `done` or `failed`.
- `selected` marks the files in the current backup.
- `plan()` runs in a single transaction. It clears `selected`, then inserts or updates each selected file.
  A file keeps `done` only if its **size and mtime are unchanged**. Anything else becomes `pending`.
  This is how running abu again into an old backup only copies new or changed files.
- The database uses WAL mode and is updated after every file. Each update is its own small transaction, so a crash loses at most the file that was being copied.

## The engine

`BackupEngine` is a `threading.Thread`. The UI never calls into it directly. Instead it:

- reads `engine.stats` (a plain dataclass) every 200 ms to draw the bars;
- empties `engine.events` (a `queue.Queue`) into the log;
- calls `pause()`, `resume()` and `stop()`, which just set `threading.Event`s.

### Startup

1. Load all selected rows.
2. Rows marked `done` whose local file still exists with the right size are counted as done straight away. Rows marked `done` whose local file is missing go back to `pending`.
3. The remaining files are sorted by folder and grouped into jobs (next section).

### Jobs: single files and batches

Starting `adb pull` costs roughly 50–100 ms per file. For thousands of thumbnails that start-up time is most of the work. So:

- files **≤ 4 MiB** in the **same folder** are grouped, up to 100 files or 64 MiB per batch,
  and copied with one `adb pull f1 f2 … .abu/staging/` command;
- larger files are copied one at a time to `<name>.abu-part`.

Measured on a real phone: 2,423 files (420 MB) in 43 s with batches, against about 10 files/s one at a time.

After a batch, **each file is checked separately**. Files with the right size, and the right MD5 if `--verify-hash` is on, are moved into place. Every other file goes through the single-file path, with its retries and disconnect handling. A batch that fails halfway therefore never loses files that already arrived and never trusts files that didn't.

### Single-file loop

```
loop until done / failed / stopped:
    wait while paused
    adb pull remote  →  name.abu-part         (progress = size of .abu-part)
    check:  size == expected?                 (if not, stat the phone file again: did it change?)
            md5(phone) == md5(local)?         (with --verify-hash)
    ok   → rename .abu-part → name, set mtime, mark done
    fail → delete .abu-part
           phone disconnected?  → WAITING FOR DEVICE, check every 1 s, retry (no attempt used)
           "No such file"?      → mark failed (deleted on the phone)
           otherwise            → attempts += 1; mark failed once attempts reach the limit
```

The connection is only checked **after a failure**, never before each file. A healthy run therefore starts no extra `adb` processes.

### Pause

The engine sends `SIGSTOP` to the `adb pull` process and `SIGCONT` when you resume. The adb server and the phone just wait while the process is frozen, so the copy carries on from the same byte. If the phone disconnects while paused, the copy fails after you resume and goes through the normal reconnect path. On platforms without `SIGSTOP` (Windows), pausing ends the current copy and starts that file again after you resume.

### Stop

`stop()` wakes a paused process, ends the running `adb pull`, deletes the partial `.abu-part` file and leaves that file `pending`. Files in the current batch that had already arrived complete are kept, unless MD5 checking is on.

## Why there is no partial-file resume within a file

`adb pull` can't start partway through a file. A file interrupted by a disconnect is copied again from the start. The temporary `.abu-part` name guarantees that a truncated file is never mistaken for a finished one. Pausing (`SIGSTOP`) doesn't interrupt the copy, so it never costs anything.

## Testing without a cable

The UI can be driven headlessly with Textual's `App.run_test()` pilot. The screenshots in `docs/screenshots/` were taken that way against a real phone. Monkeypatching `adb.is_connected` and `engine._pull` to report failures gave the **WAITING FOR DEVICE** state without unplugging anything.
