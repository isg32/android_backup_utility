"""Textual TUI: device check -> (resume?) -> scan -> folder picker -> backup/restore."""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Callable

from rich.style import Style
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.containers import Center, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, Footer, Header, Label, LoadingIndicator, OptionList, ProgressBar,
    RichLog, Static, Tree,
)
from textual.widgets.option_list import Option
from textual.widgets.tree import TreeNode

from .adb import Adb, AdbError, RemoteFile
from .engine import BackupEngine, TransferEngine
from .model import (ALL, NONE, PARTIAL, FsNode, build_tree, find, human_size,
                    human_time, plural)
from .restore import RestoreEngine, scan_local
from .state import CONFLICT, FAILED, StateDB

ROOT = "/sdcard/"
ANDROID_DIR = "Android"


# =============================================================== check tree
class CheckTree(Tree[FsNode]):
    """Folder tree with tri-state checkboxes. ←/→ collapse/expand, space toggles."""

    BINDINGS = [
        Binding("right,l", "open", "Open", show=True),
        Binding("left,h", "close", "Close", show=True),
        Binding("space", "check", "Toggle", show=True),
        Binding("enter", "screen.start", "Start", show=True),
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
    ]

    ICON_NODE = "▸ "
    ICON_NODE_EXPANDED = "▾ "

    def __init__(self, root: FsNode,
                 annotate: Callable[[FsNode], Text | None] | None = None, **kw) -> None:
        super().__init__(root.name, data=root, **kw)
        self.annotate = annotate
        self.auto_expand = False
        self.guide_depth = 3
        self._loaded: set[int] = set()

    def on_mount(self) -> None:
        self.load_children(self.root)
        self.root.expand()

    # -- lazy children -------------------------------------------------------
    def load_children(self, node: TreeNode[FsNode]) -> None:
        if node.id in self._loaded or node.data is None:
            return
        self._loaded.add(node.id)
        for child in node.data.sorted_children():
            node.add("", data=child, allow_expand=child.is_dir and bool(child.children))

    @on(Tree.NodeExpanded)
    def _expanded(self, event: Tree.NodeExpanded[FsNode]) -> None:
        self.load_children(event.node)

    # -- actions -------------------------------------------------------------
    def action_open(self) -> None:
        node = self.cursor_node
        if node is None or not node.allow_expand:
            return
        if node.is_expanded:
            if node.children:
                self.move_cursor(node.children[0])
        else:
            self.load_children(node)
            node.expand()

    def action_close(self) -> None:
        node = self.cursor_node
        if node is None:
            return
        if node.is_expanded and node.allow_expand:
            node.collapse()
        elif node.parent is not None:
            self.move_cursor(node.parent)

    def action_check(self) -> None:
        node = self.cursor_node
        if node is None or node.data is None:
            return
        node.data.toggle()
        self.changed()

    def changed(self) -> None:
        self._updates += 1
        self.refresh()
        self.post_message(SelectionChanged())

    # -- rendering -----------------------------------------------------------
    def render_label(self, node: TreeNode[FsNode], base_style: Style, style: Style) -> Text:
        fs = node.data
        if fs is None:
            return Text(str(node.label))
        state = fs.state
        box = {
            ALL: ("■ ", "bold #50fa7b"),
            PARTIAL: ("◧ ", "bold #f1fa8c"),
            NONE: ("□ ", "#6272a4"),
        }[state]
        if node.allow_expand:
            arrow = self.ICON_NODE_EXPANDED if node.is_expanded else self.ICON_NODE
        else:
            arrow = "  "
        name_style = "bold #8be9fd" if fs.is_dir else "#f8f8f2"
        if state == NONE:
            name_style += " dim"
        text = Text.assemble(
            (arrow, "#bd93f9"),
            box,
            (fs.name + ("/" if fs.is_dir and node is not self.root else ""), name_style),
        )
        text.stylize(style)
        text.append(f"  {human_size(fs.size)}", "#ffb86c")
        if fs.is_dir:
            text.append(f"  {fs.count:,} files", "#6272a4")
            if state == PARTIAL:
                text.append(f"  ({human_size(fs.sel_size)} selected)", "#f1fa8c dim")
        if self.annotate:
            extra = self.annotate(fs)
            if extra:
                text.append_text(extra)
        if fs.rel == ANDROID_DIR:
            text.append("  app data — off by default", "italic #ff79c6")
        return text


class SelectionChanged(Message):
    pass


# ================================================================== modals
class ResumeModal(ModalScreen[str]):
    def __init__(self, summary: str) -> None:
        super().__init__()
        self.summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(f"↻  Unfinished {self.app.mode} found", id="dialog-title")
            yield Static(self.summary, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                yield Button("Resume", variant="success", id="resume")
                yield Button("New selection", variant="primary", id="new")
                yield Button("Quit", variant="error", id="quit")

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id or "quit")


class DevicePicker(ModalScreen[str]):
    def __init__(self, devices: list[tuple[str, str]]) -> None:
        super().__init__()
        self.devices = devices

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label("Select a device", id="dialog-title")
            yield OptionList(*[Option(f"{s}  {d}", id=s) for s, d in self.devices])

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)


class Confirm(ModalScreen[bool]):
    BINDINGS = [Binding("y", "yes", "Yes"), Binding("n,escape", "no", "No")]

    def __init__(self, title: str, body: str, yes: str, no: str,
                 yes_variant: str = "error") -> None:
        super().__init__()
        self.t, self.body, self.yes, self.no, self.yes_variant = title, body, yes, no, yes_variant

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog"):
            yield Label(self.t, id="dialog-title")
            yield Static(self.body, id="dialog-body")
            with Horizontal(id="dialog-buttons"):
                yield Button(self.yes, variant=self.yes_variant, id="yes")  # type: ignore[arg-type]
                yield Button(self.no, variant="primary", id="no")

    def action_yes(self) -> None:
        self.dismiss(True)

    def action_no(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


# ================================================================== screens
class MessageScreen(Screen):
    BINDINGS = [Binding("q,escape", "app.quit", "Quit")]

    def __init__(self, title: str, body: str, loading: bool = False) -> None:
        super().__init__()
        self.title_text, self.body_text, self.loading = title, body, loading

    def compose(self) -> ComposeResult:
        yield Header()
        with Center(id="msg-wrap"):
            with Vertical(id="msg"):
                yield Label(self.title_text, id="msg-title")
                if self.loading:
                    yield LoadingIndicator()
                yield Static(self.body_text, id="msg-body")
        yield Footer()

    def set_body(self, text: str) -> None:
        self.query_one("#msg-body", Static).update(text)


class SelectScreen(Screen):
    BINDINGS = [
        Binding("a", "toggle_all", "All/None"),
        Binding("A", "toggle_android", "Android dir"),
        Binding("o", "toggle_overwrite", "Overwrite"),
        Binding("s", "start", "Start"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, root: FsNode,
                 annotate: Callable[[FsNode], Text | None] | None = None) -> None:
        super().__init__()
        self.fs_root = root
        self.annotate = annotate

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action == "toggle_overwrite":
            return self.app.mode == "restore"
        return True

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="sel-summary")
        yield CheckTree(self.fs_root, self.annotate, id="tree")
        yield Footer()

    def on_mount(self) -> None:
        self.update_summary()
        self.query_one(CheckTree).focus()

    @on(SelectionChanged)
    def update_summary(self) -> None:
        r, app = self.fs_root, self.app
        head = (f" [b #50fa7b]{human_size(r.sel_size)}[/] selected in "
                f"[b]{r.sel_count:,}[/] files   ")
        if app.mode == "backup":
            text = (head + f"[dim]of {human_size(r.size)} / {r.count:,} files on device[/]"
                    f"   →  [#8be9fd]{app.out_dir}[/]")
        else:
            mode = ("[b #ff5555]overwrite[/]" if app.overwrite else "[b #8be9fd]keep device copy[/]")
            text = (head + f"[dim]of {human_size(r.size)} / {r.count:,} files in backup[/]"
                    f"   →  [#8be9fd]device:{app.remote_root}[/]\n"
                    f" Files that differ on the device: {mode} [dim](press o to change)[/]")
        self.query_one("#sel-summary", Static).update(text)

    def action_toggle_overwrite(self) -> None:
        self.app.overwrite = not self.app.overwrite
        self.update_summary()

    def action_toggle_all(self) -> None:
        self.fs_root.toggle()
        self.query_one(CheckTree).changed()

    def action_toggle_android(self) -> None:
        node = self.fs_root.children.get(ANDROID_DIR)
        if node:
            node.toggle()
            self.query_one(CheckTree).changed()

    def action_start(self) -> None:
        if self.fs_root.sel_count == 0:
            self.notify("Nothing selected.", severity="warning")
            return
        self.app.start_from_selection()


class BackupScreen(Screen):
    BINDINGS = [
        Binding("p,space", "pause", "Pause/Resume"),
        Binding("r", "retry", "Retry failed"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self, engine: TransferEngine) -> None:
        super().__init__()
        self.engine = engine
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="bk"):
            with Horizontal(id="bk-top"):
                yield Static(id="bk-state")
                yield Static(id="bk-dest")
            yield Label("Overall", classes="bk-label")
            yield ProgressBar(id="bar-total", show_eta=False)
            yield Static(id="bk-stats")
            yield Label("Current file", classes="bk-label")
            yield ProgressBar(id="bar-file", show_eta=False)
            yield Static(id="bk-file")
            yield RichLog(id="bk-log", markup=True, wrap=True, max_lines=2000)
        yield Footer()

    def on_mount(self) -> None:
        app = self.app
        if app.mode == "backup":
            route = f"[dim]{app.device_label}  →[/]  [#8be9fd]{app.out_dir}[/]"
        else:
            route = (f"[#8be9fd]{app.out_dir}[/]  [dim]→  {app.device_label}:[/]"
                     f"[#8be9fd]{app.remote_root}[/]")
        self.query_one("#bk-dest", Static).update(route)
        self.set_interval(0.2, self.refresh_stats)
        if not self.engine.is_alive():
            self.engine.start()

    def refresh_stats(self) -> None:
        e, s = self.engine, self.engine.stats
        log = self.query_one(RichLog)
        colors = {"info": "#8be9fd", "ok": "#50fa7b", "warn": "#f1fa8c", "error": "#ff5555"}
        while not e.events.empty():
            level, msg = e.events.get_nowait()
            log.write(f"[dim]{time.strftime('%H:%M:%S')}[/] [{colors.get(level, 'white')}]{msg}[/]")

        badge = {
            "idle": ("PREPARING", "#6272a4"),
            "scanning": ("◌ CHECKING DEVICE", "#8be9fd"),
            "running": ("● RUNNING", "#50fa7b"),
            "verifying": ("● VERIFYING", "#8be9fd"),
            "paused": ("❚❚ PAUSED", "#f1fa8c"),
            "waiting": ("⚠ WAITING FOR DEVICE", "#ff5555"),
            "done": ("✔ COMPLETE", "#50fa7b"),
            "stopped": ("■ STOPPED", "#ff5555"),
        }.get(s.state, (s.state.upper(), "white"))
        if e.paused and s.state not in ("done", "stopped"):
            badge = ("❚❚ PAUSED", "#f1fa8c")
        self.query_one("#bk-state", Static).update(f"[b reverse {badge[1]}] {badge[0]} [/]")

        self.query_one("#bar-total", ProgressBar).update(
            total=max(s.total_bytes, 1), progress=min(s.progress_bytes, s.total_bytes)
        )
        processed = s.done_files + s.failed_files + s.conflict_files
        stats = (
            f"[b]{processed:,}[/]/{s.total_files:,} files   "
            f"[#ffb86c]{human_size(s.progress_bytes)}[/] / {human_size(s.total_bytes)}   "
            f"[#8be9fd]{human_size(s.speed)}/s[/]   ETA [b]{human_time(s.eta)}[/]"
        )
        extras = []
        if s.copied_files:
            extras.append(f"[#50fa7b]✔ {s.copied_files:,} copied[/]")
        if s.skipped_files or (s.done_files - s.copied_files):
            where = "already present" if self.app.mode == "backup" else "already on device"
            extras.append(f"[#6272a4]↷ {s.done_files - s.copied_files:,} {where}[/]")
        if s.conflict_files:
            extras.append(f"[#f1fa8c]≠ {s.conflict_files:,} differ on device (kept)[/]")
        if s.failed_files:
            extras.append(f"[#ff5555]✘ {s.failed_files:,} failed[/]")
        if extras:
            stats += "\n" + "   ".join(extras)
        self.query_one("#bk-stats", Static).update(stats)

        self.query_one("#bar-file", ProgressBar).update(
            total=max(s.current_size, 1), progress=min(s.current_bytes, max(s.current_size, 1))
        )
        if s.current_rel:
            attempt = f"  [#f1fa8c](attempt {s.current_attempt})[/]" if s.current_attempt > 1 else ""
            self.query_one("#bk-file", Static).update(
                Text.assemble(
                    (s.current_rel, "#f8f8f2"),
                    (f"  {human_size(s.current_bytes)} / {human_size(s.current_size)}", "#ffb86c"),
                ).markup + attempt
            )
        else:
            self.query_one("#bk-file", Static).update("[dim]—[/]")

        if s.state in ("done", "stopped") and not self._finished:
            self._finished = True
            self.on_finished()

    def on_finished(self) -> None:
        log = self.query_one(RichLog)
        s = self.engine.stats
        app = self.app
        restore = app.mode == "restore"
        failed = app.db.listing(FAILED)
        report = app.out_dir / ".abu" / ("restore-failed.txt" if restore else "failed.txt")
        if failed:
            report.write_text("".join(f"{r}\t{err}\n" for r, err in failed))
        conflicts = app.db.listing(CONFLICT) if restore else []
        conflict_report = app.out_dir / ".abu" / "restore-conflicts.txt"
        if conflicts:
            conflict_report.write_text("".join(f"{r}\t{err}\n" for r, err in conflicts))
        if s.state == "done":
            elapsed = human_time(time.monotonic() - s.started)
            log.write(
                f"\n[b #50fa7b]{'Restore' if restore else 'Backup'} finished[/] in {elapsed}: "
                f"{s.copied_files:,} copied, {s.done_files - s.copied_files:,} "
                f"{'already on device' if restore else 'already present'}, "
                f"[#ff5555]{s.failed_files:,} failed[/]."
            )
            if conflicts:
                log.write(f"[#f1fa8c]{plural(len(conflicts), 'file')} differ on the device and were left "
                          f"untouched — see {conflict_report}. Re-run with --overwrite to replace them.[/]")
            if failed:
                log.write(f"[#f1fa8c]Failed files listed in {report}. Press [b]r[/] to retry them.[/]")
            else:
                log.write("[#50fa7b]Every file verified. Press [b]q[/] to exit.[/]")
            self.app.bell()

    def action_pause(self) -> None:
        if self.engine.stats.state in ("done", "stopped"):
            return
        if self.engine.paused:
            self.engine.resume()
            self.notify("Resumed")
        else:
            self.engine.pause()
            self.notify("Paused — press p to resume", severity="warning")

    def action_retry(self) -> None:
        if self.engine.is_alive():
            self.notify("Retry is available once the current run finishes.", severity="warning")
            return
        n = self.app.db.reset_failed()
        if not n:
            self.notify("No failed files to retry.")
            return
        self.app.run_engine()

    def action_quit(self) -> None:
        if not self.engine.is_alive():
            self.app.exit()
            return

        def done(yes: bool | None) -> None:
            if yes:
                self.app.stop_engine_and_exit()

        what = "restore" if self.app.mode == "restore" else "backup"
        self.app.push_screen(Confirm(
            f"Stop the {what}?",
            "Progress is saved per file. Run the same command again to resume\n"
            "exactly where you left off.",
            "Stop & quit", "Keep going"), done)


# ===================================================================== app
class AbuApp(App):
    TITLE = "Android Backup Utility"
    CSS_PATH = "app.tcss"
    BINDINGS = [Binding("ctrl+c", "quit_safely", "Quit", show=False, priority=True)]

    def __init__(self, out_dir: Path | None, serial: str | None, verify_hash: bool,
                 retries: int, include_android: bool, auto_resume: bool | None,
                 mode: str = "backup", remote_root: str | None = None,
                 overwrite: bool = False) -> None:
        super().__init__()
        self.mode = mode  # "backup" | "restore"
        self.remote_root_arg = remote_root
        self.remote_root = ROOT
        self.overwrite = overwrite
        self.remote_files: dict[str, tuple[int, int]] | None = None
        self.out_dir_arg = out_dir
        self.serial = serial
        self.verify_hash = verify_hash
        self.retries = retries
        self.include_android = include_android
        self.auto_resume = auto_resume
        self.adb: Adb | None = None
        self.db: StateDB | None = None
        self.out_dir: Path = Path(".")
        self.device_label = ""
        self.files: list[RemoteFile] = []
        self.fs_root: FsNode | None = None
        self.engine: TransferEngine | None = None

    # ------------------------------------------------------------- startup
    def on_mount(self) -> None:
        self.push_screen(MessageScreen("Looking for devices…", "", loading=True))
        self.find_device()

    @work(thread=True, exclusive=True)
    def find_device(self) -> None:
        try:
            adb = Adb(self.serial)
            devices = [d for d in adb.devices() if d.state == "device"]
        except AdbError as e:
            self.call_from_thread(self.fatal, "adb not available", str(e))
            return
        if self.serial:
            if not any(d.serial == self.serial for d in devices):
                self.call_from_thread(
                    self.fatal, "Device not found",
                    f"No authorised device with serial {self.serial}.",
                )
                return
        elif not devices:
            self.call_from_thread(
                self.fatal, "No device connected",
                "Connect your phone via USB, enable USB debugging and accept the\n"
                "authorisation prompt on the phone, then run again.\n\n"
                "[dim]`adb devices` must list it as 'device'.[/]",
            )
            return
        elif len(devices) > 1:
            choices = [(d.serial, d.description) for d in devices]
            self.call_from_thread(
                self.push_screen, DevicePicker(choices), self._device_picked
            )
            return
        else:
            self.serial = devices[0].serial
        self.call_from_thread(self._device_picked, self.serial)

    def _device_picked(self, serial: str | None) -> None:
        if not serial:
            self.exit()
            return
        self.serial = serial
        self.setup_device()

    @work(thread=True, exclusive=True)
    def setup_device(self) -> None:
        adb = Adb(self.serial)
        self.adb = adb
        model = adb.prop("ro.product.model") or "android"
        brand = adb.prop("ro.product.brand")
        self.device_label = f"{brand} {model}".strip() + f" ({self.serial})"
        if self.out_dir_arg:
            out = self.out_dir_arg
        else:
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{model}_{self.serial}")
            out = Path.cwd() / "backups" / safe
        self.out_dir = out.expanduser().resolve()
        if self.mode == "restore":
            if not self.out_dir.is_dir():
                self.call_from_thread(self.fatal, "Backup folder not found", str(self.out_dir))
                return
            self.db = StateDB(self.out_dir / ".abu" / "restore.db")
            root = self.remote_root_arg or self.db.get_meta("remote_root") or ROOT
            self.remote_root = "/" + root.strip("/") + "/"
            self.sub_title = f"Restore → {self.device_label}"
        else:
            self.out_dir.mkdir(parents=True, exist_ok=True)
            self.db = StateDB(self.out_dir / ".abu" / "state.db")
            self.sub_title = self.device_label
        self.db.set_meta("serial", self.serial or "")

        summary = self.db.summary()
        pending = summary.get("pending", (0, 0))
        failed = summary.get("failed", (0, 0))
        done = summary.get("done", (0, 0))
        if (pending[0] or failed[0]) and self.auto_resume is not False:
            if self.auto_resume:
                self.call_from_thread(self._resume_choice, "resume")
                return
            text = (
                f"[#50fa7b]{done[0]:,} files ({human_size(done[1])}) already "
                f"{'restored' if self.mode == 'restore' else 'backed up'}[/]\n"
                f"[#f1fa8c]{pending[0]:,} files ({human_size(pending[1])}) remaining[/]\n"
                + (f"[#ff5555]{failed[0]:,} files failed[/]\n" if failed[0] else "")
                + f"\n[dim]{self.out_dir}[/]"
            )
            self.call_from_thread(self.push_screen, ResumeModal(text), self._resume_choice)
        else:
            self.call_from_thread(self.start_scan)

    def _resume_choice(self, choice: str | None) -> None:
        if choice == "resume":
            assert self.db
            self.db.reset_failed()
            self.run_engine()
        elif choice == "new":
            self.start_scan()
        else:
            self.exit()

    def start_scan(self) -> None:
        if self.mode == "restore":
            title, body = "Reading backup…", f"Comparing {self.out_dir} with the device"
        else:
            title, body = "Scanning /sdcard…", "Listing files on the device"
        self.switch_screen(MessageScreen(title, body, loading=True))
        self.scan()

    @work(thread=True, exclusive=True)
    def scan(self) -> None:
        assert self.adb and self.db
        def progress(n: int) -> None:
            self.call_from_thread(self._scan_progress, n)
        annotate = None
        try:
            if self.mode == "restore":
                self.files = scan_local(self.out_dir)
                if not self.files:
                    self.call_from_thread(self.fatal, "Backup folder is empty", str(self.out_dir))
                    return
                remote = self.adb.scan(self.remote_root, progress)
                self.remote_files = {f.rel: (f.size, f.mtime) for f in remote}
                annotate = restore_annotator(self.files, self.remote_files)
                root = build_tree(self.files, "backup")
            else:
                self.files = self.adb.scan(ROOT, progress)
                root = build_tree(self.files)
        except AdbError as e:
            self.call_from_thread(self.fatal, "Scan failed", str(e))
            return
        previous = self.db.selected_rels()
        if previous:
            for rel in previous:
                node = find(root, rel)
                if node is not None:
                    node.set_selected(True)
        else:
            root.set_selected(True)
            android = root.children.get(ANDROID_DIR)
            if android and not self.include_android:
                android.set_selected(False)
        self.fs_root = root
        self.call_from_thread(self.switch_screen, SelectScreen(root, annotate))

    def _scan_progress(self, n: int) -> None:
        scr = self.screen
        if isinstance(scr, MessageScreen):
            where = "on the device" if self.mode == "restore" else ""
            scr.set_body(f"Found [b]{n:,}[/] files {where}…")

    # ------------------------------------------------------------- transfer
    def start_from_selection(self) -> None:
        assert self.fs_root and self.db
        by_rel = {f.rel: f for f in self.files}
        selected = [by_rel[n.rel] for n in self.fs_root.iter_selected()]
        if self.mode == "backup":
            self.db.plan(selected)
            self.run_engine()
            return

        remote = self.remote_files or {}
        same = [f for f in selected if remote.get(f.rel, (-1,))[0] == f.size]
        differ = [f for f in selected if f.rel in remote and remote[f.rel][0] != f.size]
        new_bytes = sum(f.size for f in selected) - sum(f.size for f in same)
        if not self.overwrite:
            new_bytes -= sum(f.size for f in differ)
        body = (
            f"Push [b]{human_size(new_bytes)}[/] to [#8be9fd]{self.remote_root}[/] on\n"
            f"{self.device_label}.\n\n"
            f"[#50fa7b]{len(selected) - len(same) - len(differ):,} new files[/]\n"
            f"[#6272a4]{len(same):,} already on the device (skipped)[/]\n"
            + (f"[#f1fa8c]{len(differ):,} differ on the device → "
               + ("[b #ff5555]will be OVERWRITTEN[/][/]" if self.overwrite else "kept as they are[/]")
               if differ else "")
        )

        def go(yes: bool | None) -> None:
            if yes:
                self.db.plan(selected)
                self.db.set_meta("remote_root", self.remote_root)
                self.run_engine()

        self.push_screen(Confirm("Start restore?", body, "Restore", "Cancel",
                                 yes_variant="success"), go)

    def run_engine(self) -> None:
        assert self.adb and self.db
        if self.mode == "restore":
            self.engine = RestoreEngine(
                self.adb, self.db, self.out_dir, self.remote_root,
                remote_files=self.remote_files, overwrite=self.overwrite,
                verify_hash=self.verify_hash, retries=self.retries,
            )
            self.remote_files = None  # stale after this run; rescan on retry
        else:
            self.engine = BackupEngine(
                self.adb, self.db, self.out_dir, ROOT,
                verify_hash=self.verify_hash, retries=self.retries,
            )
        self.switch_screen(BackupScreen(self.engine))

    def stop_engine_and_exit(self) -> None:
        if self.engine and self.engine.is_alive():
            self.engine.stop()
            self.engine.join(timeout=5)
        self.exit()

    def action_quit_safely(self) -> None:
        if isinstance(self.screen, BackupScreen):
            self.screen.action_quit()
        else:
            self.stop_engine_and_exit()

    def fatal(self, title: str, body: str) -> None:
        self.switch_screen(MessageScreen(f"✘ {title}", body + "\n\n[dim]Press q to quit.[/]"))

    def on_unmount(self) -> None:
        if self.engine and self.engine.is_alive():
            self.engine.stop()
            self.engine.join(timeout=5)
        if self.db:
            self.db.close()


def restore_annotator(files: list[RemoteFile],
                      remote: dict[str, tuple[int, int]]) -> Callable[[FsNode], Text | None]:
    """Label each tree row with how it compares to what is on the device."""
    status: dict[str, str] = {}
    dirs: dict[str, list[int]] = {}  # rel -> [new, same, differ]
    for f in files:
        r = remote.get(f.rel)
        st = "new" if r is None else ("same" if r[0] == f.size else "differ")
        status[f.rel] = st
        idx = {"new": 0, "same": 1, "differ": 2}[st]
        parts = f.rel.split("/")
        for i in range(len(parts)):
            dirs.setdefault("/".join(parts[:i]), [0, 0, 0])[idx] += 1

    def annotate(fs: FsNode) -> Text | None:
        if not fs.is_dir:
            st = status.get(fs.rel)
            if st == "same":
                return Text("  ✓ on device", "#6272a4")
            if st == "differ":
                return Text("  ≠ differs on device", "#f1fa8c")
            return Text("  new", "#50fa7b")
        new, same, differ = dirs.get(fs.rel, [0, 0, 0])
        t = Text()
        if new:
            t.append(f"  {new:,} new", "#50fa7b")
        if differ:
            t.append(f"  {differ:,} differ", "#f1fa8c")
        if same and not new and not differ:
            t.append("  ✓ all on device", "#6272a4")
        elif same:
            t.append(f"  {same:,} on device", "#6272a4")
        return t

    return annotate
