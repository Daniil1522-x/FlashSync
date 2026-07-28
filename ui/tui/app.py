"""
ui/tui/app.py — FlashSync Pro, главное TUI-приложение.
"""
from __future__ import annotations
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import (
    Header, Footer, DataTable, Tree, Label,
    Button, Input, Checkbox, RichLog, ProgressBar, Select,
)
from textual import work, on
from textual.screen import ModalScreen
from rich.text import Text

from domain.models import SyncAction, ActionType, SyncProfile, SyncReport, ProtectionLevel, validate_sync_paths
from application.differ import DiffEngine, summarize_plan
from application.plan_overrides import PlanOverrides
from application.session import SessionManager, plan_stats_by_category, format_plan_stats
from infrastructure.scanner import scan_directory, get_directory_stats
from infrastructure.storage import (
    load_profiles, save_profiles, save_report,
    setup_logger, CONFIG_DIR, REPORTS_DIR, _normalize_path,
)
from infrastructure.crash_reporter import (
    setup_crash_reporter, crash_count_since, install_asyncio_handler,
)
from ui.tui.crash_screen import CrashReportsScreen
from ui.tui.folder_picker import FolderPickerScreen
from ui.tui.file_manager import FileManagerScreen
from ui.tui.mascot import MascotWidget
from ui.tui.splash import SplashScreen
from application.sync_engine import SyncEngine
from domain.version import __version__
import shutil
import subprocess
import os as os_module

# ── Словари действий ──────────────────────────────────────────────────────────

ACTION_LABEL = {
    ActionType.COPY_NEW:       "НОВЫЙ",
    ActionType.COPY_UPDATE:    "ИЗМЕНЁН",
    ActionType.DELETE:         "ТОЛЬКО В DST",
    ActionType.DELETE_PERM:    "УДАЛИТЬ",
    ActionType.SKIP_EQUAL:     "одинаковый",
    ActionType.SKIP_PROTECTED: "ЗАЩИЩЁН",
}
ACTION_STYLE = {
    ActionType.COPY_NEW:       "ansi_bright_green",
    ActionType.COPY_UPDATE:    "ansi_yellow",
    ActionType.DELETE:         "ansi_red",
    ActionType.DELETE_PERM:    "ansi_red",
    ActionType.SKIP_EQUAL:     "ansi_bright_black",
    ActionType.SKIP_PROTECTED: "ansi_cyan",
}
ACTION_WILL_DO = {
    ActionType.COPY_NEW:       "скопировать в dst",
    ActionType.COPY_UPDATE:    "обновить (старый → backup)",
    ActionType.DELETE:         "переместить в backup",
    ActionType.DELETE_PERM:    "удалить навсегда",
    ActionType.SKIP_EQUAL:     "ничего (одинаковые)",
    ActionType.SKIP_PROTECTED: "пропустить (защищён)",
}

FILTER_ALL = "all"
FILTER_OPTIONS = [
    ("Все изменения",     FILTER_ALL),
    ("Только новые",      "copy_new"),
    ("Только изменённые", "copy_update"),
    ("В backup",          "delete"),
    ("Защищённые",        "skip_protected"),
]


def _fmt_size(size: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


# ── Диалог подтверждения ────────────────────────────────────────────────────

class ConfirmDialog(ModalScreen):
    CSS = """
    ConfirmDialog { align: center middle; }
    #dlg { width: 70; min-height: 14; border: thick $primary;
           background: $surface; padding: 2 3; }
    #dlg Label { margin-bottom: 1; }
    #dlg Horizontal { height: 3; }
    #dlg Button { margin: 0 2; }
    """
    def __init__(self, message: str, double_confirm: bool = False):
        super().__init__()
        self.message = message
        self.double_confirm = double_confirm
        self._count = 0

    def compose(self) -> ComposeResult:
        with Container(id="dlg"):
            yield Label(self.message, id="msg")
            with Horizontal():
                yield Button("Подтвердить", variant="warning", id="yes")
                yield Button("Отмена", variant="default", id="no")

    @on(Button.Pressed, "#yes")
    def _yes(self):
        if self.double_confirm:
            self._count += 1
            if self._count < 2:
                self.query_one("#msg", Label).update(
                    "[bold red]ВТОРОЕ ПОДТВЕРЖДЕНИЕ![/]\n\n" + self.message)
                return
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self):
        self.dismiss(False)


# ── Настройки ────────────────────────────────────────────────────────────────

class SettingsScreen(ModalScreen):
    CSS = """
    SettingsScreen { align: center middle; }
    #sc { width: 90%; height: 85%; border: thick $primary;
          background: $surface; padding: 0; }
    #sc-inner { padding: 1 2; }
    .hint { color: $text-muted; margin-bottom: 1; }
    .pick-row { height: 3; layout: horizontal; margin-bottom: 1; }
    .pick-row Input { width: 1fr; }
    .pick-row Button { width: 14; margin-left: 1; }
    Checkbox { margin-bottom: 1; }
    .row { height: auto; margin-top: 1; layout: horizontal; }
    """
    def __init__(self, profiles: dict, current: SyncProfile):
        super().__init__()
        self.profiles = profiles
        self.current = current

    def compose(self) -> ComposeResult:
        with Container(id="sc"):
            with ScrollableContainer(id="sc-inner"):
                yield Label("[bold]Настройки профиля[/]")
                yield Label("")
                yield Label("Источник (флешка, папка):", classes="hint")
                with Horizontal(classes="pick-row"):
                    yield Input(value=self.current.src, placeholder="E:\\FLASH", id="inp-src")
                    yield Button("Выбрать...", id="btn-pick-src", variant="default")
                yield Label("Приёмник (папка на ПК):", classes="hint")
                with Horizontal(classes="pick-row"):
                    yield Input(value=self.current.dst, placeholder="E:\\Backup", id="inp-dst")
                    yield Button("Выбрать...", id="btn-pick-dst", variant="default")
                yield Label("")
                yield Label("[bold]Параметры:[/]", classes="hint")
                yield Checkbox("SHA-256: сравнивать по содержимому (точно, медленнее)",
                               value=self.current.use_hash, id="chk-hash")
                yield Checkbox("Удалять из dst: файлы которых нет в src → backup",
                               value=self.current.delete_mode, id="chk-del")
                yield Checkbox("Игнорировать скрытые файлы (.DS_Store, Thumbs.db)",
                               value=self.current.ignore_hidden, id="chk-hid")
                yield Label("")
                with Horizontal(classes="row"):
                    yield Button("Сохранить", variant="success", id="btn-save")
                    yield Button("Отмена", variant="default", id="btn-cancel")

    @on(Button.Pressed, "#btn-pick-src")
    def _pick_src(self):
        self._open_picker("inp-src", "Выберите источник")

    @on(Button.Pressed, "#btn-pick-dst")
    def _pick_dst(self):
        self._open_picker("inp-dst", "Выберите приёмник")

    def _open_picker(self, inp_id: str, title: str):
        cur = self.query_one(f"#{inp_id}", Input).value
        self.app.push_screen(FolderPickerScreen(title, cur),
                             lambda p: self._set_inp(inp_id, p))

    def _set_inp(self, inp_id: str, path) -> None:
        if path:
            self.query_one(f"#{inp_id}", Input).value = str(path)

    @on(Button.Pressed, "#btn-save")
    def _save(self):
        self.current.src = _normalize_path(self.query_one("#inp-src", Input).value.strip())
        self.current.dst = _normalize_path(self.query_one("#inp-dst", Input).value.strip())
        self.current.use_hash      = self.query_one("#chk-hash", Checkbox).value
        self.current.delete_mode   = self.query_one("#chk-del",  Checkbox).value
        self.current.ignore_hidden = self.query_one("#chk-hid",  Checkbox).value
        save_profiles(self.profiles)
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self):
        self.dismiss(False)


# ── Профили ──────────────────────────────────────────────────────────────────

class ProfilesScreen(ModalScreen):
    CSS = """
    ProfilesScreen { align: center middle; }
    #ps { width: 72; height: auto; border: thick $primary;
          background: $surface; padding: 1 2; }
    #ps DataTable { height: 10; }
    #ps Button { margin: 0 1; }
    #ps Input { margin-bottom: 1; width: 100%; }
    #ps .row { layout: horizontal; height: 3; margin-top: 1; }
    """
    def __init__(self, profiles: dict, current_name: str):
        super().__init__()
        self._profiles = profiles
        self._current = current_name

    def compose(self) -> ComposeResult:
        with Container(id="ps"):
            yield Label("[bold]Профили синхронизации[/]")
            yield Label("[dim]Enter / двойной клик = выбрать[/]")
            yield DataTable(id="prof-table", cursor_type="row")
            yield Label("")
            yield Label("Имя нового профиля:")
            yield Input(placeholder="Например: Работа или Фото", id="new-name")
            with Horizontal(classes="row"):
                yield Button("Создать",  id="btn-new",   variant="success")
                yield Button("Выбрать",  id="btn-pick",  variant="primary")
                yield Button("Удалить",  id="btn-del",   variant="warning")
                yield Button("Закрыть",  id="btn-close", variant="default")

    def on_mount(self) -> None:
        t = self.query_one("#prof-table", DataTable)
        t.add_columns(" ", "Имя", "Источник", "Приёмник")
        self._refresh()

    def _refresh(self) -> None:
        t = self.query_one("#prof-table", DataTable)
        t.clear()
        for name, p in self._profiles.items():
            mark = "★" if name == self._current else " "
            src_s = ("..." + p.src[-27:]) if len(p.src) > 30 else p.src
            dst_s = ("..." + p.dst[-27:]) if len(p.dst) > 30 else p.dst
            t.add_row(mark, name, src_s, dst_s, key=name)

    @on(DataTable.RowSelected, "#prof-table")
    def _row_sel(self, event) -> None:
        if event.cursor_row >= 0:
            row = self.query_one("#prof-table", DataTable).get_row_at(event.cursor_row)
            self._current = row[1]
            self.dismiss(self._current)

    @on(Button.Pressed, "#btn-new")
    def _new(self) -> None:
        name = self.query_one("#new-name", Input).value.strip()
        if not name:
            self.notify("Введите имя", severity="warning"); return
        if name in self._profiles:
            self.notify("Уже существует", severity="warning"); return
        self._profiles[name] = SyncProfile(name=name, src="", dst="")
        save_profiles(self._profiles)
        self._current = name
        self._refresh()

    @on(Button.Pressed, "#btn-pick")
    def _pick(self) -> None:
        t = self.query_one("#prof-table", DataTable)
        if t.cursor_row >= 0:
            self._current = t.get_row_at(t.cursor_row)[1]
            self.dismiss(self._current)

    @on(Button.Pressed, "#btn-del")
    def _del(self) -> None:
        t = self.query_one("#prof-table", DataTable)
        if t.cursor_row >= 0:
            name = t.get_row_at(t.cursor_row)[1]
            if name == "default":
                self.notify("Нельзя удалить 'default'", severity="warning"); return
            del self._profiles[name]
            save_profiles(self._profiles)
            if self._current == name:
                self._current = "default"
            self._refresh()

    @on(Button.Pressed, "#btn-close")
    def _close(self):
        self.dismiss(self._current)


# ── История ──────────────────────────────────────────────────────────────────

class HistoryScreen(ModalScreen):
    CSS = """
    HistoryScreen { align: center middle; }
    #hs { width: 80%; height: 80%; border: thick $primary;
          background: $surface; padding: 1 2; }
    #hs DataTable { height: 1fr; }
    #hs Button { margin: 0 1; margin-top: 1; }
    """
    def compose(self) -> ComposeResult:
        with Container(id="hs"):
            yield Label("[bold]История синхронизаций[/]")
            yield Label(f"[dim]{REPORTS_DIR}[/]")
            yield DataTable(id="hist-table", cursor_type="row")
            with Horizontal():
                yield Button("Открыть", id="btn-open",  variant="primary")
                yield Button("Удалить", id="btn-del",   variant="warning")
                yield Button("Закрыть", id="btn-close", variant="default")

    def on_mount(self) -> None:
        t = self.query_one("#hist-table", DataTable)
        t.add_columns("Дата", "Размер", "Файл")
        self._load()

    def _load(self) -> None:
        t = self.query_one("#hist-table", DataTable)
        t.clear()
        if not REPORTS_DIR.exists():
            return
        for r in sorted(REPORTS_DIR.glob("report_*.txt"), reverse=True)[:50]:
            try:
                stat = r.stat()
                ts = r.stem.replace("report_", "")
                ds = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}" if len(ts) >= 15 else ts
                t.add_row(ds, f"{stat.st_size:,}B", r.name, key=str(r))
            except OSError:
                pass

    @on(Button.Pressed, "#btn-open")
    def _open(self) -> None:
        t = self.query_one("#hist-table", DataTable)
        if t.cursor_row >= 0:
            row = t.get_row_at(t.cursor_row)
            try:
                cmd = "notepad.exe" if os_module.name == "nt" else "xdg-open"
                subprocess.Popen([cmd, str(REPORTS_DIR / row[2])])
            except Exception as e:
                self.notify(f"Ошибка: {e}", severity="error")

    @on(Button.Pressed, "#btn-del")
    def _del(self) -> None:
        t = self.query_one("#hist-table", DataTable)
        if t.cursor_row >= 0:
            try:
                (REPORTS_DIR / t.get_row_at(t.cursor_row)[2]).unlink()
                self._load()
            except Exception as e:
                self.notify(f"Ошибка: {e}", severity="error")

    @on(Button.Pressed, "#btn-close")
    def _close(self):
        self.dismiss(None)


# ── Очистка backup ───────────────────────────────────────────────────────────

class BackupCleanupScreen(ModalScreen):
    CSS = """
    BackupCleanupScreen { align: center middle; }
    #bc { width: 72; height: auto; border: thick $primary;
          background: $surface; padding: 1 2; }
    #bc DataTable { height: 12; }
    #bc .row { layout: horizontal; height: 3; margin-top: 1; }
    #bc Button { margin: 0 1; }
    """
    def __init__(self, dst: str):
        super().__init__()
        self._dst = Path(dst)

    def compose(self) -> ComposeResult:
        with Container(id="bc"):
            yield Label("[bold]Очистка папок backup[/]")
            yield Label(f"[dim]{self._dst}[/]")
            yield DataTable(id="bc-table", cursor_type="row")
            with Horizontal(classes="row"):
                yield Button("Удалить выбранную", id="btn-del-one", variant="warning")
                yield Button("Удалить все",       id="btn-del-all", variant="error")
                yield Button("Закрыть",           id="btn-close",   variant="default")

    def on_mount(self) -> None:
        t = self.query_one("#bc-table", DataTable)
        t.add_columns("Папка", "Размер", "Файлов")
        self._load()

    def _load(self) -> None:
        t = self.query_one("#bc-table", DataTable)
        t.clear()
        if not self._dst.exists():
            return
        for d in sorted(self._dst.glob(".flashsync_backup_*")):
            if d.is_dir():
                files = [f for f in d.rglob("*") if f.is_file()]
                size = sum(f.stat().st_size for f in files)
                t.add_row(d.name, f"{size//(1024*1024)}MB", str(len(files)), key=str(d))

    @on(Button.Pressed, "#btn-del-one")
    def _del_one(self) -> None:
        t = self.query_one("#bc-table", DataTable)
        if t.cursor_row >= 0:
            try:
                shutil.rmtree(self._dst / t.get_row_at(t.cursor_row)[0])
                self._load()
                self.notify("Backup удалён", severity="information")
            except Exception as e:
                self.notify(f"Ошибка: {e}", severity="error")

    @on(Button.Pressed, "#btn-del-all")
    def _del_all(self) -> None:
        deleted = 0
        for d in list(self._dst.glob(".flashsync_backup_*")):
            try:
                shutil.rmtree(d); deleted += 1
            except Exception:
                pass
        self._load()
        self.notify(f"Удалено {deleted} папок", severity="information")

    @on(Button.Pressed, "#btn-close")
    def _close(self):
        self.dismiss(None)


# ── Dry Run лог ──────────────────────────────────────────────────────────────

class DryRunLogScreen(ModalScreen):
    CSS = """
    DryRunLogScreen { align: center middle; }
    #logbox { width: 90%; height: 80%; border: thick $primary;
              background: $surface; padding: 1 2; }
    #logbox RichLog { height: 1fr; }
    #logbox Button { margin-top: 1; }
    """
    def __init__(self, lines: list[str], summary: str):
        super().__init__()
        self.lines = lines
        self.summary = summary

    def compose(self) -> ComposeResult:
        with Container(id="logbox"):
            yield Label(f"[bold]Dry Run — что будет выполнено[/]\n{self.summary}")
            yield RichLog(id="rl", markup=True)
            yield Button("Закрыть", variant="default", id="close")

    def on_mount(self):
        rl = self.query_one("#rl", RichLog)
        for line in self.lines:
            rl.write(line)

    @on(Button.Pressed, "#close")
    def _close(self):
        self.dismiss(None)


# ── Дерево источника ─────────────────────────────────────────────────────────

class FileTreePanel(Container):
    DEFAULT_CSS = """
    FileTreePanel { width: 22%; min-width: 18; border: solid $accent;
                    background: $surface-darken-1; }
    FileTreePanel #ftitle { background: $accent; color: $background;
                            text-align: center; height: 1; }
    FileTreePanel Tree { height: 1fr; }
    """
    def compose(self) -> ComposeResult:
        yield Label(" Содержимое источника ", id="ftitle")
        yield Tree("...", id="ftree")

    def load_path(self, path: Path) -> None:
        tree = self.query_one("#ftree", Tree)
        tree.clear()
        tree.root.label = path.name
        tree.root.data = path
        self._add(tree.root, path, 0)
        tree.root.expand()

    def _add(self, node, path: Path, depth: int) -> None:
        if depth > 2:
            return
        try:
            entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except (PermissionError, OSError):
            return
        for e in entries[:50]:
            if e.name.startswith("."):
                continue
            if e.is_dir():
                self._add(node.add(f"[папка] {e.name}", data=e), e, depth + 1)
            else:
                node.add_leaf(f"  {e.name}", data=e)


# ── Правая панель ────────────────────────────────────────────────────────────

class InfoPanel(Container):
    DEFAULT_CSS = """
    InfoPanel { width: 28%; min-width: 28; border: solid $primary;
                background: $surface-darken-1; padding: 0; }
    InfoPanel #stats-label { padding: 0 1; height: auto; }
    InfoPanel #divider { color: $text-muted; padding: 0 1; height: 1; }
    InfoPanel RichLog { height: 1fr; padding: 0 1; }
    """
    def compose(self) -> ComposeResult:
        yield Label("", id="stats-label")
        yield Label("── Лог операций ──", id="divider")
        yield RichLog(id="log-view", markup=True, max_lines=500)

    def update_stats(self, stats: dict, profile: SyncProfile,
                     plan: Optional[list] = None) -> None:
        mb = stats.get("total_size", 0) / (1024 * 1024)
        lines = [
            f"[bold]Источник:[/] {Path(profile.src).name}",
            f"Файлов:  {stats.get('total_files', 0):,}",
            f"Размер:  {mb:.1f} MB",
            "─" * 24,
        ]
        for cat, cnt in sorted(stats.get("categories", {}).items(), key=lambda x: -x[1])[:5]:
            lines.append(f"  {cat:<10} {cnt:>4}")
        if plan:
            cat_stats = plan_stats_by_category(plan)
            if cat_stats:
                lines += ["─" * 24, "В плане (изменения):"]
                lines += format_plan_stats(cat_stats)
        lines += ["─" * 24, f"[dim]Логи: {CONFIG_DIR}[/]"]
        self.query_one("#stats-label", Label).update("\n".join(lines))

    def log(self, msg: str, style: str = "") -> None:
        rl = self.query_one("#log-view", RichLog)
        rl.write(f"[{style}]{msg}[/]" if style else msg)

    def clear_log(self) -> None:
        self.query_one("#log-view", RichLog).clear()


# ── Главное приложение ────────────────────────────────────────────────────────

class FlashSyncApp(App):
    TITLE = "FlashSync Pro"

    CSS = """
    Screen { background: $background; }
    #toolbar {
        layout: horizontal; height: auto; min-height: 3;
        background: $surface-darken-2; padding: 0 1; align: left middle;
    }
    #toolbar Button { margin: 0 0; min-width: 0; }
    #direction-lbl { color: $text-muted; margin: 0 1; }

    #path-status { height: 1; background: $surface-darken-1; padding: 0 1; }
    #path-status Label { margin: 0 2; }

    #filter-row {
        height: 3; layout: horizontal;
        background: $surface-darken-1; padding: 0 1; align: left middle;
    }
    #filter-row Label { margin: 0 1; color: $text-muted; }
    #filter-row Select { width: 22; }
    #filter-row #filter-stats { width: 1fr; color: $text-muted; margin: 0 2; }

    #main-layout { layout: horizontal; height: 1fr; }
    #center { width: 1fr; border: solid $primary; background: $surface; }
    #legend { height: 2; background: $surface-darken-1; padding: 0 1; }
    #plan-table { height: 1fr; }

    #progress-row {
        height: 2; background: $surface-darken-2;
        layout: horizontal; padding: 0 1; align: left middle;
    }
    #progress-row ProgressBar { width: 1fr; }
    #progress-row #prog-label { width: 32; color: $text-muted; }

    #statusbar {
        height: 6; background: $surface-darken-2;
        padding: 0 1; align: left middle; layout: horizontal;
    }
    #statusbar #slabel { width: 1fr; }
    #statusbar #ov-count-lbl { color: ansi_cyan; margin: 0 1; }
    #statusbar #crash-badge  { color: ansi_red;  margin: 0 1; }
    #statusbar MascotWidget  { width: 10; height: 5; margin: 0 1 0 2; }

    #btn-stop { display: none; }
    #btn-stop.visible { display: block; }
    """

    BINDINGS = [
        Binding("ctrl+s", "scan",       "Сканировать",   show=True),
        Binding("ctrl+r", "run_sync",   "Синхр-ть",      show=True),
        Binding("ctrl+d", "dry_run",    "Dry Run",       show=True),
        Binding("ctrl+f", "file_mgr",   "Файлы",         show=True),
        Binding("ctrl+p", "settings",   "Настройки",     show=True),
        Binding("ctrl+q", "quit",       "Выход",         show=True),
        Binding("escape", "cancel_sync","Стоп",          show=False),
    ]

    def __init__(self):
        super().__init__()
        setup_crash_reporter(__version__, on_crash=self._on_crash)
        self.profiles = load_profiles()
        self.current_profile: SyncProfile = list(self.profiles.values())[0]
        self.logger = setup_logger()
        self._plan: list[SyncAction] = []
        self._filtered_plan: list[SyncAction] = []
        self._filter = FILTER_ALL
        self._overrides = PlanOverrides(self.current_profile.name)
        self._session = SessionManager(self.current_profile.name)
        self._scan_errors: list[str] = []
        self._stop_sync = False
        self._syncing = False

    # ── Compose ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            with Horizontal(id="toolbar"):
                yield Button("Сканировать",   id="btn-scan",     variant="primary")
                yield Button("Синхр-ть",      id="btn-sync",     variant="success")
                yield Button("Dry Run",       id="btn-dry",      variant="warning")
                yield Button("■ Стоп",        id="btn-stop",     variant="error")
                yield Button("Файлы",         id="btn-fm",       variant="default")
                yield Button("Сбросить",      id="btn-reset-ov", variant="default")
                yield Button("↔ Обратить",    id="btn-rev",      variant="default")
                yield Button("Профили",       id="btn-prof",     variant="default")
                yield Button("История",       id="btn-hist",     variant="default")
                yield Button("Backup",        id="btn-bkp",      variant="default")
                yield Button("Настройки",     id="btn-cfg",      variant="default")
                yield Button("Ошибки",        id="btn-crashes",  variant="error")
                yield Label("", id="direction-lbl")

            with Horizontal(id="path-status"):
                yield Label("", id="src-status-lbl")
                yield Label("", id="dst-status-lbl")

            with Horizontal(id="filter-row"):
                yield Label("Фильтр:")
                yield Select(
                    [(label, val) for label, val in FILTER_OPTIONS],
                    value=FILTER_ALL, id="plan-filter", allow_blank=False,
                )
                yield Label("", id="filter-stats")

            with Horizontal(id="main-layout"):
                yield FileTreePanel(id="src-tree")
                with Vertical(id="center"):
                    yield Label(
                        "[ansi_bright_green]● НОВЫЙ[/]  "
                        "[ansi_yellow]● ИЗМЕНЁН[/]  "
                        "[ansi_red]● ТОЛЬКО В DST[/]  "
                        "[ansi_cyan]● ЗАЩИЩЁН[/]",
                        id="legend"
                    )
                    yield DataTable(id="plan-table", cursor_type="row")
                yield InfoPanel(id="info")

        with Horizontal(id="progress-row"):
            yield ProgressBar(id="prog-bar", total=100, show_percentage=True)
            yield Label("", id="prog-label")
        with Horizontal(id="statusbar"):
            yield Label("Готов. Ctrl+S = сканировать", id="slabel")
            yield Label("", id="ov-count-lbl")
            yield Label("", id="crash-badge")
            yield MascotWidget(id="mascot")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#plan-table", DataTable)
        t.add_columns("Действие", "Что будет сделано", "Файл", "Размер")
        self._update_direction_label()
        self._update_crash_badge()
        self._check_path_status()
        self._try_restore_session()
        self.push_screen(SplashScreen())

    # ── Восстановление сессии ───────────────────────────────────────────────

    def _try_restore_session(self) -> None:
        p = self.current_profile
        result = self._session.load(p.src, p.dst)
        if result is None:
            return
        plan, saved_at = result
        if not plan:
            return
        self._plan, applied, stale = self._overrides.apply(plan)
        self._apply_filter()
        self._update_ov_label()
        msg = f"Восстановлена сессия от {saved_at}"
        if applied:
            msg += f"  ({applied} ручных изм. сохранены)"
        self._set_status(msg)
        self.notify(msg, severity="information", timeout=6)

    # ── Вспомогательные ─────────────────────────────────────────────────────

    def _update_direction_label(self) -> None:
        p = self.current_profile
        src = p.src[-32:] if len(p.src) > 32 else p.src
        dst = p.dst[-32:] if len(p.dst) > 32 else p.dst
        self.query_one("#direction-lbl", Label).update(f"[bold]{p.name}[/]  {src} → {dst}")

    def _check_path_status(self) -> None:
        p = self.current_profile
        src_ok = Path(p.src).exists() if p.src else False
        dst_ok = Path(p.dst).exists() if p.dst else False
        src_lbl = self.query_one("#src-status-lbl", Label)
        dst_lbl = self.query_one("#dst-status-lbl", Label)
        src_lbl.update("[ansi_bright_green]● SRC[/]" if src_ok else "[ansi_red]✗ SRC не найден[/]")
        dst_lbl.update("[ansi_bright_green]● DST[/]" if dst_ok else "[ansi_yellow]○ DST будет создан[/]")

    def _update_ov_label(self) -> None:
        try:
            n = self._overrides.count()
            self.query_one("#ov-count-lbl", Label).update(
                f"[ansi_cyan]● {n} ручных изм.[/]" if n > 0 else ""
            )
        except Exception:
            pass

    def _update_crash_badge(self) -> None:
        try:
            n = crash_count_since(hours=24)
            self.query_one("#crash-badge", Label).update(
                f"[ansi_red]⚠ {n} краш[/]" if n > 0 else ""
            )
        except Exception:
            pass

    def _on_crash(self, crash_path: Path) -> None:
        try:
            self.call_from_thread(self._update_crash_badge)
        except Exception:
            pass

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#slabel", Label).update(msg)
        except Exception:
            pass

    def _set_progress(self, done: int, total: int, label: str) -> None:
        try:
            self.query_one("#prog-bar", ProgressBar).update(progress=done, total=max(total, 1))
            self.query_one("#prog-label", Label).update(label)
        except Exception:
            pass

    def _show_stop_btn(self, visible: bool) -> None:
        try:
            btn = self.query_one("#btn-stop", Button)
            if visible:
                btn.add_class("visible")
            else:
                btn.remove_class("visible")
        except Exception:
            pass

    def _mascot_set_active(self, value: bool) -> None:
        """Включает/выключает анимацию ходьбы маскота. Чисто декоративно —
        обёрнуто в try/except, чтобы сбой здесь никогда не повлиял на скан/синк."""
        try:
            self.query_one("#mascot", MascotWidget).set_active(value)
        except Exception:
            pass

    # ── Фильтр плана ─────────────────────────────────────────────────────────

    @on(Select.Changed, "#plan-filter")
    def _on_filter_changed(self, event: Select.Changed) -> None:
        self._filter = str(event.value)
        self._apply_filter()

    def _apply_filter(self) -> None:
        if self._filter == FILTER_ALL:
            self._filtered_plan = [a for a in self._plan if a.action != ActionType.SKIP_EQUAL]
        else:
            self._filtered_plan = [a for a in self._plan if a.action.value == self._filter]
        self._fill_table(self._filtered_plan)
        self._update_filter_stats()

    def _update_filter_stats(self) -> None:
        try:
            total_changes = sum(1 for a in self._plan if a.action != ActionType.SKIP_EQUAL)
            shown = len(self._filtered_plan)
            lbl = self.query_one("#filter-stats", Label)
            if self._filter == FILTER_ALL:
                lbl.update(f"Показано {total_changes} изменений")
            else:
                lbl.update(f"Показано {shown} из {total_changes}")
        except Exception:
            pass

    # ── Кнопки ───────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-scan")
    def action_scan(self):
        self._do_scan()

    @on(Button.Pressed, "#btn-sync")
    def action_run_sync(self):
        self._start_sync(dry_run=False)

    @on(Button.Pressed, "#btn-dry")
    def action_dry_run(self):
        self._start_sync(dry_run=True)

    @on(Button.Pressed, "#btn-stop")
    def action_cancel_sync(self):
        self._stop_sync = True
        self.notify("Остановка после текущего файла...", severity="warning")

    @on(Button.Pressed, "#btn-fm")
    def action_file_mgr(self):
        if not self._plan:
            self.notify("Сначала нажмите Сканировать", severity="warning"); return
        self.push_screen(FileManagerScreen(self._plan, self.current_profile),
                         self._on_file_mgr_closed)

    @on(Button.Pressed, "#btn-reset-ov")
    def _reset_overrides(self):
        n = self._overrides.count()
        if n == 0:
            self.notify("Нет сохранённых изменений", severity="information"); return
        self.push_screen(
            ConfirmDialog(f"Сбросить {n} ручных изменений?\n\nПлан будет пересчитан при следующем сканировании."),
            lambda ok: self._on_reset_confirmed(ok)
        )

    def _on_reset_confirmed(self, ok: bool) -> None:
        if ok:
            n = self._overrides.count()
            self._overrides.clear()
            self._update_ov_label()
            self.notify(f"Сброшено {n} изменений", severity="warning")

    @on(Button.Pressed, "#btn-rev")
    def _reverse(self):
        p = self.current_profile
        p.src, p.dst = p.dst, p.src
        save_profiles(self.profiles)
        self._update_direction_label()
        self._check_path_status()
        self._session = SessionManager(p.name)
        self.notify(f"Направление изменено:\n{p.src} → {p.dst}", severity="information", timeout=4)

    @on(Button.Pressed, "#btn-prof")
    def _profiles(self):
        self.push_screen(ProfilesScreen(self.profiles, self.current_profile.name),
                         self._on_profile_picked)

    @on(Button.Pressed, "#btn-hist")
    def action_history(self):
        self.push_screen(HistoryScreen(), lambda _: None)

    @on(Button.Pressed, "#btn-bkp")
    def action_cleanup(self):
        self.push_screen(BackupCleanupScreen(self.current_profile.dst), lambda _: None)

    @on(Button.Pressed, "#btn-cfg")
    def action_settings(self):
        self.push_screen(SettingsScreen(self.profiles, self.current_profile),
                         self._on_settings_saved)

    @on(Button.Pressed, "#btn-crashes")
    def _show_crashes(self):
        self.push_screen(CrashReportsScreen(), lambda _: self._update_crash_badge())

    # ── Коллбеки экранов ─────────────────────────────────────────────────────

    def _on_file_mgr_closed(self, new_plan) -> None:
        if new_plan is not None:
            old_by = {a.rel_path.as_posix(): a for a in self._plan}
            changed = [a for a in new_plan
                       if a.rel_path.as_posix() in old_by
                       and a.action != old_by[a.rel_path.as_posix()].action]
            if changed:
                self._overrides.set_many(changed)
            self._plan = new_plan
            self._apply_filter()
            self._update_ov_label()
            p = self.current_profile
            self._session.save(self._plan, p.src, p.dst)
            self.notify(f"Изменения применены ({len(changed)} файлов). Сохранены.",
                       severity="information", timeout=4)

    def _on_profile_picked(self, name: Optional[str]) -> None:
        if name and name in self.profiles:
            self.current_profile = self.profiles[name]
            self._overrides = PlanOverrides(name)
            self._session = SessionManager(name)
            self._plan = []
            self._filtered_plan = []
            self._fill_table([])
            self._update_direction_label()
            self._check_path_status()
            self._try_restore_session()
            self.notify(f"Профиль: {name}", severity="information")

    def _on_settings_saved(self, saved: bool) -> None:
        if saved:
            self._update_direction_label()
            self._check_path_status()
            self._session = SessionManager(self.current_profile.name)
            self._plan = []
            self._fill_table([])
            self.notify("Профиль сохранён. Нажмите Сканировать.", severity="information")

    # ── Сканирование ─────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def _do_scan(self) -> None:
        # Маскот шагает, пока идёт реальная работа — try/finally гарантирует,
        # что он остановится при ЛЮБОМ из множества early-return путей внутри
        # _do_scan_impl, не трогая саму логику сканирования.
        self.call_from_thread(self._mascot_set_active, True)
        try:
            self._do_scan_impl()
        finally:
            self.call_from_thread(self._mascot_set_active, False)

    def _do_scan_impl(self) -> None:
        self._scan_errors = []
        self.call_from_thread(self._set_status, "Сканирование...")
        self.call_from_thread(self.query_one("#info", InfoPanel).clear_log)
        self.call_from_thread(self._set_progress, 0, 100, "")

        p = self.current_profile
        src = Path(p.src)
        dst = Path(p.dst)
        scan_start = datetime.now()

        if not src.exists():
            self.call_from_thread(self.notify, f"Источник не найден: {src}", severity="error")
            self.call_from_thread(self._set_status, "Ошибка: источник не найден")
            return

        # Проверяем что src и dst не пересекаются ДО создания dst и сканирования —
        # иначе при создании dst.mkdir() мы рискуем создать вложенную папку прямо
        # внутри src, и следующий же scan_directory(src) увидит её как часть источника.
        path_error = validate_sync_paths(src, dst)
        if path_error is None and not dst.exists():
            # dst ещё не существует — проверяем пересечение и для будущего пути,
            # т.к. resolve() требует существования на некоторых платформах для relative_to
            try:
                if dst.resolve().is_relative_to(src.resolve()):
                    path_error = (f"Приёмник ({dst}) будет создан ВНУТРИ источника ({src}). "
                                 f"Выберите другой путь приёмника.")
            except (OSError, ValueError):
                pass
        if path_error:
            self.call_from_thread(self.notify, path_error, severity="error", timeout=8)
            self.call_from_thread(self._set_status, "Ошибка: src и dst пересекаются")
            return

        try:
            dst.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.call_from_thread(self.notify, f"Ошибка dst: {e}", severity="error")
            return

        def _err_cb(path: Path, exc: Exception) -> None:
            self._scan_errors.append(str(path))
            self.call_from_thread(self.query_one("#info", InfoPanel).log,
                                  f"⚠ Пропущен: {path.name} ({type(exc).__name__})", "ansi_red")

        _src_count = [0]
        def _src_cb(path: Path):
            _src_count[0] += 1
            if _src_count[0] % 20 == 0:
                self.call_from_thread(self._set_status,
                    f"Сканирование src: {_src_count[0]} файлов... {path.name}")

        self.call_from_thread(self._set_progress, 10, 100, f"Сканирование {src.name}...")
        try:
            src_tree = scan_directory(
                src, include_patterns=p.include_patterns or None,
                exclude_patterns=p.exclude_patterns,
                use_hash=p.use_hash, ignore_hidden=p.ignore_hidden,
                progress_cb=_src_cb, error_cb=_err_cb,
            )
        except Exception as e:
            self.call_from_thread(self.notify, f"Ошибка сканирования src: {e}", severity="error")
            self.logger.exception("scan src failed")
            return

        _dst_count = [0]
        def _dst_cb(path: Path):
            _dst_count[0] += 1
            if _dst_count[0] % 20 == 0:
                self.call_from_thread(self._set_status,
                    f"Сканирование dst: {_dst_count[0]} файлов... {path.name}")

        self.call_from_thread(self._set_progress, 40, 100, f"Сканирование {dst.name}...")
        try:
            dst_tree = scan_directory(
                dst, include_patterns=p.include_patterns or None,
                exclude_patterns=p.exclude_patterns,
                use_hash=p.use_hash, ignore_hidden=p.ignore_hidden,
                progress_cb=_dst_cb, error_cb=_err_cb,
            )
        except Exception as e:
            self.call_from_thread(self.notify, f"Ошибка сканирования dst: {e}", severity="error")
            self.logger.exception("scan dst failed")
            return

        self.call_from_thread(self._set_progress, 70, 100, "Построение плана...")
        try:
            raw_plan = DiffEngine(p).compute_plan(src_tree, dst_tree)
            self._plan, applied, stale = self._overrides.apply(raw_plan)
        except Exception as e:
            self.call_from_thread(self.notify, f"Ошибка плана: {e}", severity="error")
            self.logger.exception("compute_plan failed")
            return

        scan_duration = (datetime.now() - scan_start).total_seconds()

        try:
            self._session.save(self._plan, p.src, p.dst, scan_duration)
        except Exception:
            pass

        if applied > 0 or stale > 0:
            parts = []
            if applied: parts.append(f"восстановлено {applied} ручных изменений")
            if stale:   parts.append(f"сброшено {stale} устаревших")
            self.call_from_thread(self.notify, "Ручные изменения: " + ", ".join(parts),
                                  severity="information", timeout=5)

        summary = summarize_plan(self._plan)
        self.call_from_thread(self._apply_filter)
        self.call_from_thread(self._update_ov_label)

        try:
            stats = get_directory_stats(src)
            self.call_from_thread(self.query_one("#info", InfoPanel).update_stats, stats, p, self._plan)
        except Exception:
            pass
        try:
            self.call_from_thread(self.query_one("#src-tree", FileTreePanel).load_path, src)
        except Exception:
            pass

        c = summary["counts"]
        mb = summary["bytes_to_copy"] / (1024 * 1024)
        method = "SHA-256" if p.use_hash else "размер+дата"
        del_perm = c.get("delete_permanent", 0)

        status = (
            f"[{method}]  "
            f"Новых: {c.get('copy_new',0)}  "
            f"Изменённых: {c.get('copy_update',0)}  "
            f"В backup: {c.get('delete',0)}  "
            + (f"Удалить: {del_perm}  " if del_perm else "")
            + f"Одинаковых: {c.get('skip_equal',0)}  "
            f"Защищённых: {c.get('skip_protected',0)}  "
            f"| {mb:.1f} MB  [{scan_duration:.1f}с]"
        )
        if self._scan_errors:
            status += f"  [ansi_red]⚠ {len(self._scan_errors)} пропущено[/]"

        self.call_from_thread(self._set_status, status)
        self.call_from_thread(self._set_progress, 100, 100, "Готово")

        info = self.query_one("#info", InfoPanel)
        now_str = datetime.now().strftime("%H:%M:%S")
        self.call_from_thread(info.log, f"─── Сканирование {now_str} ───")
        self.call_from_thread(info.log, f"  SRC  {src.name:<28} {len(src_tree):>5} файлов")
        self.call_from_thread(info.log, f"  DST  {dst.name:<28} {len(dst_tree):>5} файлов")
        for atype, label, style in [
            (ActionType.COPY_NEW,       "Новых",      "ansi_bright_green"),
            (ActionType.COPY_UPDATE,    "Изменённых", "ansi_yellow"),
            (ActionType.DELETE,         "В backup",   "ansi_red"),
            (ActionType.SKIP_PROTECTED, "Защищённых", "ansi_cyan"),
        ]:
            n = c.get(atype.value, 0)
            if n:
                self.call_from_thread(info.log, f"  {label:<14} {n:>5}", style)
        self.call_from_thread(info.log, f"  Одинаковых     {c.get('skip_equal',0):>5}", "ansi_bright_black")
        self.call_from_thread(info.log, f"  К копированию  {mb:>5.1f} MB")
        if self._scan_errors:
            self.call_from_thread(info.log,
                f"  ⚠ Пропущено    {len(self._scan_errors):>5} (нет доступа)", "ansi_red")
            self.call_from_thread(self.notify,
                f"Пропущено {len(self._scan_errors)} файлов (нет доступа).\nСмотрите лог.",
                severity="warning", timeout=6)

        self.logger.info(
            f"Scan OK: src={len(src_tree)} dst={len(dst_tree)} "
            f"errors={len(self._scan_errors)} t={scan_duration:.1f}s"
        )

    # ── Таблица ──────────────────────────────────────────────────────────────

    def _fill_table(self, plan: list[SyncAction]) -> None:
        t = self.query_one("#plan-table", DataTable)
        t.clear()
        for a in plan:
            style = ACTION_STYLE.get(a.action, "")
            rel = str(a.rel_path)
            if len(rel) > 58:
                rel = "..." + rel[-55:]
            t.add_row(
                Text(ACTION_LABEL.get(a.action, a.action.value), style=style),
                Text(ACTION_WILL_DO.get(a.action, ""), style=style),
                Text(rel),
                Text(_fmt_size(a.size_bytes)),
            )

    # ── Синхронизация ────────────────────────────────────────────────────────

    def _start_sync(self, dry_run: bool) -> None:
        if not self._plan:
            self.notify("Сначала нажмите Сканировать", severity="warning"); return
        active = [a for a in self._plan if a.action not in (ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED)]
        if not active:
            self.notify("Нет изменений — всё актуально.", severity="information"); return

        needs_double = any(
            a.protection_level == ProtectionLevel.DOUBLE
            and a.action in (ActionType.COPY_UPDATE, ActionType.DELETE)
            for a in active
        )
        p = self.current_profile
        n_copy = sum(1 for a in active if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE))
        n_del = sum(1 for a in active if a.action == ActionType.DELETE)
        mb = sum(a.size_bytes for a in active
                 if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE)) / (1024*1024)

        prefix = "[bold]DRY RUN — симуляция[/]\n\n" if dry_run else "[bold]Подтвердите синхронизацию[/]\n\n"
        msg = (f"{prefix}Источник: {p.src}\nПриёмник: {p.dst}\n\n"
               f"Скопировать/обновить: {n_copy} файлов ({mb:.1f} MB)\n"
               f"Переместить в backup: {n_del} файлов\n\n"
               f"{'Запустить симуляцию?' if dry_run else 'Продолжить?'}")
        self.push_screen(ConfirmDialog(msg, double_confirm=needs_double),
                         lambda ok: self._run_sync_worker(ok, active, dry_run))

    @work(thread=True, exclusive=True)
    def _run_sync_worker(self, confirmed: bool, actions: list[SyncAction], dry_run: bool) -> None:
        if not confirmed:
            return

        self._stop_sync = False
        self._syncing = True
        self.call_from_thread(self._show_stop_btn, True)
        self.call_from_thread(self._mascot_set_active, True)

        p = self.current_profile
        info = self.query_one("#info", InfoPanel)
        total = len(actions)
        done = 0
        self.call_from_thread(self._set_progress, 0, total, f"0 / {total}")

        def on_progress(action: SyncAction, msg: str) -> None:
            nonlocal done
            done += 1
            style = ACTION_STYLE.get(action.action, "")
            self.call_from_thread(self._set_status, msg[:120])
            if msg.startswith(("  ✓", "  ✗", "  ⊘", "  🗑", "  [DRY]", "  →")):
                self.call_from_thread(info.log, msg, style)
            self.call_from_thread(self._set_progress, done, total,
                                  f"{done}/{total}  {_fmt_size(action.size_bytes)}")

        engine = SyncEngine(
            profile=p, src=Path(p.src), dst=Path(p.dst),
            dry_run=dry_run, progress_cb=on_progress,
            stop_flag=lambda: self._stop_sync,
        )
        report = SyncReport()
        try:
            loop = asyncio.new_event_loop()
            install_asyncio_handler(loop)
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(engine.execute(actions, report))
            finally:
                loop.close()
        except Exception as e:
            self.call_from_thread(self.notify, f"Критическая ошибка: {e}", severity="error")
            self.logger.exception("sync failed")
        finally:
            self._syncing = False
            self.call_from_thread(self._show_stop_btn, False)
            self.call_from_thread(self._mascot_set_active, False)

        self.call_from_thread(self._set_progress, total, total, "Готово")

        if self._stop_sync:
            self.call_from_thread(self.notify,
                f"Синхронизация остановлена. Выполнено {done}/{total}.", severity="warning")
            self._stop_sync = False
            return

        if dry_run:
            s = report.stats
            mb_c = report.bytes_copied / (1024*1024)
            mb_b = report.bytes_backed_up / (1024*1024)
            summary = (
                f"Скопировать: {s.get('copy_new',0)} нов. + {s.get('copy_update',0)} изм. "
                f"({mb_c:.1f} MB)  |  В backup: {s.get('delete',0)} ({mb_b:.1f} MB)"
            )
            self.call_from_thread(self.push_screen,
                DryRunLogScreen(report.log_lines, summary), lambda _: None)
            self.call_from_thread(self._set_status, f"[DRY RUN] {summary}")
        else:
            try:
                rpath = save_report(report)
                self.call_from_thread(info.log, f"  Отчёт → {rpath.name}")
            except Exception:
                pass

            s = report.stats
            now_s = datetime.now().strftime("%H:%M:%S")
            mb_d = report.bytes_copied / (1024*1024)
            self.call_from_thread(info.log, f"─── Синхронизация завершена {now_s} ───")
            self.call_from_thread(info.log,
                f"  Скопировано  {s.get('copy_new',0):>4} нов.  {s.get('copy_update',0):>4} изм.",
                "ansi_bright_green")
            self.call_from_thread(info.log,
                f"  В backup     {s.get('delete',0):>4}  |  {mb_d:.1f} MB  за {report.duration_seconds:.1f}с")
            if report.errors:
                self.call_from_thread(info.log, f"  ✗ Ошибок     {len(report.errors):>4}", "ansi_red")
                for err in report.errors[:5]:
                    self.call_from_thread(info.log, f"    {err}", "ansi_red")

            parts = [f"+{s.get('copy_new',0)} нов.", f"↻{s.get('copy_update',0)} изм.",
                     f"⊘{s.get('delete',0)} backup"]
            if s.get("delete_permanent", 0):
                parts.append(f"🗑{s.get('delete_permanent',0)} удал.")
            self.call_from_thread(self._set_status, "Готово: " + "  ".join(parts))

            if report.errors:
                self.call_from_thread(self.notify, f"Завершено с {len(report.errors)} ошибками.", severity="warning")
            else:
                self.call_from_thread(self.notify, "Синхронизация завершена!", severity="information")

            self._session.clear()


def run():
    FlashSyncApp().run()


if __name__ == "__main__":
    run()
