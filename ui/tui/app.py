"""
ui/tui/app.py — FlashSync TUI
"""
from __future__ import annotations
import asyncio
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.widgets import (
    Header, Footer, DataTable, Tree, Label,
    Button, Input, Static, Log, Checkbox, RichLog,
    ProgressBar, Select,
)
from textual import work, on
from textual.screen import ModalScreen
from rich.text import Text

from domain.models import SyncAction, ActionType, SyncProfile, SyncReport, ProtectionLevel
from ui.tui.file_manager import FileManagerScreen
from application.differ import DiffEngine, summarize_plan
from infrastructure.scanner import scan_directory, get_directory_stats
from infrastructure.storage import (
    load_profiles, save_profiles, save_report,
    setup_logger, CONFIG_DIR, REPORTS_DIR,
)
from infrastructure.drives import get_removable_drives
from ui.tui.folder_picker import FolderPickerScreen


ACTION_LABEL = {
    ActionType.COPY_NEW:       "НОВЫЙ",
    ActionType.COPY_UPDATE:    "ИЗМЕНЁН",
    ActionType.DELETE:         "ТОЛЬКО В DST",
    ActionType.SKIP_EQUAL:     "одинаковый",
    ActionType.SKIP_PROTECTED: "ЗАЩИЩЁН",
}
ACTION_STYLE = {
    ActionType.COPY_NEW:       "ansi_bright_green",
    ActionType.COPY_UPDATE:    "ansi_yellow",
    ActionType.DELETE:         "ansi_red",
    ActionType.SKIP_EQUAL:     "ansi_bright_black",
    ActionType.SKIP_PROTECTED: "ansi_cyan",
}
ACTION_WILL_DO = {
    ActionType.COPY_NEW:       "скопировать в dst",
    ActionType.COPY_UPDATE:    "обновить (старый → backup)",
    ActionType.DELETE:         "переместить в backup",
    ActionType.SKIP_EQUAL:     "ничего (одинаковые)",
    ActionType.SKIP_PROTECTED: "пропустить (защищён)",
}


# ── Диалог подтверждения ──────────────────────────────────────────────────────

class ConfirmDialog(ModalScreen):
    CSS = """
    ConfirmDialog { align: center middle; }
    #dlg { width: 70; min-height: 16; border: thick $primary;
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
    def _no(self): self.dismiss(False)


# ── Экран настроек ────────────────────────────────────────────────────────────

class SettingsScreen(ModalScreen):
    CSS = """
    SettingsScreen { align: center middle; }
    #sc { width: 90%; height: 85%; border: thick $primary;
          background: $surface; padding: 0; }
    #sc-inner { padding: 1 2; }
    .hint { color: $text-muted; margin-bottom: 1; }
    Input { margin-bottom: 1; width: 100%; }
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
                yield Label("Источник — откуда копировать (флешка, папка):", classes="hint")
                with Horizontal(classes="row"):
                    yield Input(value=self.current.src, placeholder="E:\\FLASH", id="inp-src")
                    yield Button("Выбрать...", id="btn-pick-src", variant="default")
                yield Label("Приёмник — куда копировать (папка на ПК):", classes="hint")
                with Horizontal(classes="row"):
                    yield Input(value=self.current.dst, placeholder="E:\\Backup\\Flash", id="inp-dst")
                    yield Button("Выбрать...", id="btn-pick-dst", variant="default")
                yield Label("")
                yield Label("[bold]Параметры сравнения:[/]", classes="hint")
                yield Checkbox(
                    "SHA-256: сравнивать файлы по содержимому (точно, но медленнее). "
                    "Выкл = по размеру и дате (быстро)",
                    value=self.current.use_hash, id="chk-hash")
                yield Checkbox(
                    "Удалять из dst: файлы которых нет в src → переместить в backup",
                    value=self.current.delete_mode, id="chk-del")
                yield Checkbox(
                    "Игнорировать скрытые: .DS_Store, Thumbs.db и файлы с точки",
                    value=self.current.ignore_hidden, id="chk-hid")
                yield Label("")
                with Horizontal(classes="row"):
                    yield Button("Сохранить", variant="success", id="btn-save")
                    yield Button("Отмена", variant="default", id="btn-cancel")

    @on(Button.Pressed, "#btn-pick-src")
    def _pick_src(self):
        from ui.tui.folder_picker import FolderPickerScreen
        cur = self.query_one("#inp-src", Input).value
        self.app.push_screen(FolderPickerScreen("Выберите источник", cur),
                             lambda p: self._set_inp("inp-src", p))

    @on(Button.Pressed, "#btn-pick-dst")
    def _pick_dst(self):
        from ui.tui.folder_picker import FolderPickerScreen
        cur = self.query_one("#inp-dst", Input).value
        self.app.push_screen(FolderPickerScreen("Выберите приёмник", cur),
                             lambda p: self._set_inp("inp-dst", p))

    def _set_inp(self, inp_id: str, path) -> None:
        if path:
            self.query_one(f"#{inp_id}", Input).value = str(path)

    @on(Button.Pressed, "#btn-save")
    def _save(self):
        from infrastructure.storage import _normalize_path
        self.current.src = _normalize_path(self.query_one("#inp-src", Input).value.strip())
        self.current.dst = _normalize_path(self.query_one("#inp-dst", Input).value.strip())
        self.current.use_hash      = self.query_one("#chk-hash", Checkbox).value
        self.current.delete_mode   = self.query_one("#chk-del",  Checkbox).value
        self.current.ignore_hidden = self.query_one("#chk-hid",  Checkbox).value
        save_profiles(self.profiles)
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self): self.dismiss(False)


# ── Экран выбора диска ────────────────────────────────────────────────────────

class DrivePickerScreen(ModalScreen):
    """Показывает список дисков и позволяет выбрать src или dst."""
    CSS = """
    DrivePickerScreen { align: center middle; }
    #dp { width: 72; height: auto; border: thick $primary;
          background: $surface; padding: 1 2; }
    #dp Label { margin-bottom: 1; }
    #dp Button { width: 100%; margin-bottom: 1; }
    #dp .drive-btn { text-align: left; }
    #dp .removable { color: ansi_bright_green; }
    """

    def __init__(self, title: str = "Выберите диск"):
        super().__init__()
        self._title = title
        self._drives = get_removable_drives()

    def compose(self) -> ComposeResult:
        with Container(id="dp"):
            yield Label(f"[bold]{self._title}[/]")
            yield Label("")
            if not self._drives:
                yield Label("[dim]Диски не обнаружены[/]")
            for i, d in enumerate(self._drives):
                flag = "[ansi_bright_green]● Съёмный[/]  " if d["removable"] else "[dim]● Диск[/]      "
                yield Button(
                    f"{flag}{d['path']}  [{d['label']}]  {_fmt_size(d['total'])}  "
                    f"свободно {_fmt_size(d['free'])}",
                    id=f"drive-{i}",
                    variant="success" if d["removable"] else "default",
                )
            yield Label("")
            yield Button("Отмена", id="dp-cancel", variant="default")

    @on(Button.Pressed)
    def _pick(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "dp-cancel":
            self.dismiss(None)
            return
        if btn_id.startswith("drive-"):
            idx = int(btn_id.split("-")[1])
            self.dismiss(self._drives[idx]["path"])


# ── Экран истории синхронизаций ───────────────────────────────────────────────

class HistoryScreen(ModalScreen):
    CSS = """
    HistoryScreen { align: center middle; }
    #hs { width: 80%; height: 80%; border: thick $primary;
          background: $surface; padding: 1 2; }
    #hs DataTable { height: 1fr; }
    #hs Button { margin-top: 1; }
    #hs Label { margin-bottom: 1; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="hs"):
            yield Label("[bold]История синхронизаций[/]")
            yield Label(f"[dim]{REPORTS_DIR}[/]")
            yield DataTable(id="hist-table", cursor_type="row")
            with Horizontal():
                yield Button("Открыть отчёт", id="btn-open", variant="primary")
                yield Button("Удалить", id="btn-del", variant="warning")
                yield Button("Закрыть", id="btn-close", variant="default")

    def on_mount(self) -> None:
        t = self.query_one("#hist-table", DataTable)
        t.add_columns("Дата", "Размер", "Файл")
        self._load_reports()

    def _load_reports(self) -> None:
        t = self.query_one("#hist-table", DataTable)
        t.clear()
        if not REPORTS_DIR.exists():
            return
        reports = sorted(REPORTS_DIR.glob("report_*.txt"), reverse=True)
        for r in reports[:50]:
            try:
                stat = r.stat()
                ts = r.stem.replace("report_", "")
                # Форматируем: 20260419_143022 → 2026-04-19 14:30
                if len(ts) == 15:
                    date_str = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]} {ts[9:11]}:{ts[11:13]}"
                else:
                    date_str = ts
                t.add_row(date_str, _fmt_size(stat.st_size), r.name, key=str(r))
            except OSError:
                pass

    @on(Button.Pressed, "#btn-open")
    def _open(self) -> None:
        t = self.query_one("#hist-table", DataTable)
        if t.cursor_row >= 0:
            row = t.get_row_at(t.cursor_row)
            path = REPORTS_DIR / row[2]
            if path.exists():
                try:
                    import subprocess, os
                    if os.name == "nt":
                        subprocess.Popen(["notepad.exe", str(path)])
                    else:
                        subprocess.Popen(["xdg-open", str(path)])
                except Exception as e:
                    self.notify(f"Не удалось открыть: {e}", severity="error")

    @on(Button.Pressed, "#btn-del")
    def _delete(self) -> None:
        t = self.query_one("#hist-table", DataTable)
        if t.cursor_row >= 0:
            row = t.get_row_at(t.cursor_row)
            path = REPORTS_DIR / row[2]
            try:
                path.unlink()
                self._load_reports()
                self.notify("Отчёт удалён", severity="information")
            except Exception as e:
                self.notify(f"Ошибка: {e}", severity="error")

    @on(Button.Pressed, "#btn-close")
    def _close(self): self.dismiss(None)


# ── Экран управления профилями ────────────────────────────────────────────────

class ProfilesScreen(ModalScreen):
    CSS = """
    ProfilesScreen { align: center middle; }
    #ps { width: 72; height: auto; border: thick $primary;
          background: $surface; padding: 1 2; }
    #ps DataTable { height: 12; }
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
            yield Label("[dim]Двойной клик = выбрать профиль[/]")
            yield DataTable(id="prof-table", cursor_type="row")
            yield Label("")
            yield Label("Имя нового профиля:")
            yield Input(placeholder="Например: Работа или Фото", id="new-name")
            with Horizontal(classes="row"):
                yield Button("Создать",   id="btn-new",    variant="success")
                yield Button("Выбрать",   id="btn-pick",   variant="primary")
                yield Button("Удалить",   id="btn-del",    variant="warning")
                yield Button("Закрыть",   id="btn-close",  variant="default")

    def on_mount(self) -> None:
        t = self.query_one("#prof-table", DataTable)
        t.add_columns(" ", "Имя", "Источник", "Приёмник")
        self._refresh()

    def _refresh(self) -> None:
        t = self.query_one("#prof-table", DataTable)
        t.clear()
        for name, p in self._profiles.items():
            mark = "★" if name == self._current else " "
            src_show = ("..." + p.src[-27:]) if len(p.src) > 30 else p.src
            dst_show = ("..." + p.dst[-27:]) if len(p.dst) > 30 else p.dst
            t.add_row(mark, name, src_show, dst_show, key=name)

    @on(Button.Pressed, "#btn-new")
    def _new(self) -> None:
        name = self.query_one("#new-name", Input).value.strip()
        if not name:
            self.notify("Введите имя профиля", severity="warning")
            return
        if name in self._profiles:
            self.notify("Профиль уже существует", severity="warning")
            return
        from domain.models import SyncProfile
        self._profiles[name] = SyncProfile(name=name, src="", dst="")
        save_profiles(self._profiles)
        self._current = name
        self._refresh()
        self.notify(f"Профиль '{name}' создан", severity="information")

    @on(Button.Pressed, "#btn-pick")
    def _pick(self) -> None:
        t = self.query_one("#prof-table", DataTable)
        if t.cursor_row >= 0:
            row = t.get_row_at(t.cursor_row)
            self._current = row[1]
            self.dismiss(self._current)

    @on(Button.Pressed, "#btn-del")
    def _del(self) -> None:
        t = self.query_one("#prof-table", DataTable)
        if t.cursor_row >= 0:
            row = t.get_row_at(t.cursor_row)
            name = row[1]
            if name == "default":
                self.notify("Нельзя удалить профиль 'default'", severity="warning")
                return
            del self._profiles[name]
            save_profiles(self._profiles)
            if self._current == name:
                self._current = "default"
            self._refresh()

    @on(DataTable.RowSelected, "#prof-table")
    def _row_selected(self, event) -> None:
        if event.cursor_row >= 0:
            row = self.query_one("#prof-table", DataTable).get_row_at(event.cursor_row)
            self._current = row[1]
            self.dismiss(self._current)

    @on(Button.Pressed, "#btn-close")
    def _close(self): self.dismiss(self._current)


# ── Экран очистки backup ──────────────────────────────────────────────────────

class BackupCleanupScreen(ModalScreen):
    CSS = """
    BackupCleanupScreen { align: center middle; }
    #bc { width: 72; height: auto; border: thick $primary;
          background: $surface; padding: 1 2; }
    #bc Label { margin-bottom: 1; }
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
        t.add_columns("Папка backup", "Размер", "Файлов")
        self._load()

    def _load(self) -> None:
        t = self.query_one("#bc-table", DataTable)
        t.clear()
        if not self._dst.exists():
            return
        for d in sorted(self._dst.glob(".flashsync_backup_*")):
            if d.is_dir():
                files = list(d.rglob("*"))
                n_files = sum(1 for f in files if f.is_file())
                size = sum(f.stat().st_size for f in files if f.is_file())
                t.add_row(d.name, _fmt_size(size), str(n_files), key=str(d))

    @on(Button.Pressed, "#btn-del-one")
    def _del_one(self) -> None:
        import shutil
        t = self.query_one("#bc-table", DataTable)
        if t.cursor_row >= 0:
            row = t.get_row_at(t.cursor_row)
            path = self._dst / row[0]
            try:
                shutil.rmtree(path)
                self._load()
                self.notify("Backup удалён", severity="information")
            except Exception as e:
                self.notify(f"Ошибка: {e}", severity="error")

    @on(Button.Pressed, "#btn-del-all")
    def _del_all(self) -> None:
        import shutil
        deleted = 0
        for d in list(self._dst.glob(".flashsync_backup_*")):
            try:
                shutil.rmtree(d)
                deleted += 1
            except Exception:
                pass
        self._load()
        self.notify(f"Удалено {deleted} папок backup", severity="information")

    @on(Button.Pressed, "#btn-close")
    def _close(self): self.dismiss(None)


# ── Экран лога Dry Run ────────────────────────────────────────────────────────

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
    def _close(self): self.dismiss(None)


# ── Панель дерева файлов ──────────────────────────────────────────────────────

class FileTreePanel(Container):
    DEFAULT_CSS = """
    FileTreePanel { width: 25%; min-width: 20; border: solid $accent;
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
                child = node.add(f"[папка] {e.name}", data=e)
                self._add(child, e, depth + 1)
            else:
                node.add_leaf(f"  {e.name}", data=e)


# ── Правая панель: статистика + лог ──────────────────────────────────────────

class InfoPanel(Container):
    DEFAULT_CSS = """
    InfoPanel { width: 25%; min-width: 22; border: solid $primary;
                background: $surface-darken-1; padding: 0; }
    InfoPanel #stats-label { padding: 0 1; height: auto; }
    InfoPanel #divider { color: $text-muted; padding: 0 1; height: 1; }
    InfoPanel RichLog { height: 1fr; padding: 0 1; }
    """
    def compose(self) -> ComposeResult:
        yield Label("", id="stats-label")
        yield Label("── Лог операций ──", id="divider")
        yield RichLog(id="log-view", markup=True, max_lines=300)

    def update_stats(self, stats: dict, profile: SyncProfile) -> None:
        mb = stats.get("total_size", 0) / (1024 * 1024)
        lines = [
            f"[bold]Источник:[/] {Path(profile.src).name}",
            f"Файлов:  {stats.get('total_files', 0):,}",
            f"Размер:  {mb:.1f} MB",
            "─" * 22,
        ]
        for cat, cnt in sorted(stats.get("categories", {}).items(), key=lambda x: -x[1])[:5]:
            lines.append(f"  {cat:<10} {cnt:>4}")
        lines.append("─" * 22)
        lines.append(f"[dim]Логи: {CONFIG_DIR}[/]")
        self.query_one("#stats-label", Label).update("\n".join(lines))

    def log(self, msg: str, style: str = "") -> None:
        rl = self.query_one("#log-view", RichLog)
        if style:
            rl.write(f"[{style}]{msg}[/]")
        else:
            rl.write(msg)

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
    #direction-lbl { color: $text-muted; margin: 0 2; }
    #main-layout { layout: horizontal; height: 1fr; }
    #center { width: 1fr; border: solid $primary; background: $surface; }
    #legend { height: 2; background: $surface-darken-1; padding: 0 1; color: $text-muted; }
    #plan-table { height: 1fr; }
    #progress-row {
        height: 2; background: $surface-darken-2;
        layout: horizontal; padding: 0 1; align: left middle;
    }
    #progress-row ProgressBar { width: 1fr; }
    #progress-row #prog-label { width: 30; color: $text-muted; }
    #statusbar { height: 2; background: $surface-darken-2; padding: 0 1; align: left middle; }
    #statusbar #slabel { width: 1fr; }
    """

    BINDINGS = [
        Binding("ctrl+s", "scan",      "Сканировать",      show=True),
        Binding("ctrl+r", "run_sync",  "Синхронизировать", show=True),
        Binding("ctrl+d", "dry_run",   "Dry Run",          show=True),
        Binding("ctrl+f", "file_mgr",  "Файлы",            show=True),
        Binding("ctrl+h", "history",   "История",          show=True),
        Binding("ctrl+b", "cleanup",   "Backup",           show=True),
        Binding("ctrl+p", "settings",  "Настройки",        show=True),
        Binding("ctrl+q", "quit",      "Выход",            show=True),
    ]

    def __init__(self):
        super().__init__()
        self.profiles = load_profiles()
        self.current_profile: SyncProfile = list(self.profiles.values())[0]
        self.logger = setup_logger()
        self._plan: list[SyncAction] = []
        self._total_actions = 0
        self._done_actions = 0

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            with Horizontal(id="toolbar"):
                yield Button("Сканировать",  id="btn-scan", variant="primary")
                yield Button("Синхр-ть",     id="btn-sync", variant="success")
                yield Button("Dry Run",      id="btn-dry",  variant="warning")
                yield Button("Файлы",     id="btn-fm",   variant="default")
                yield Button("↔ Обратить",  id="btn-rev",  variant="default")
                yield Button("Профили",   id="btn-prof", variant="default")
                yield Button("История",   id="btn-hist", variant="default")
                yield Button("Backup",    id="btn-bkp",  variant="default")
                yield Button("Настройки",    id="btn-cfg",  variant="default")
                yield Label("", id="direction-lbl")
            with Horizontal(id="main-layout"):
                yield FileTreePanel(id="src-tree")
                with Vertical(id="center"):
                    yield Label(
                        "[ansi_bright_green]● НОВЫЙ=скопировать[/]  "
                        "[ansi_yellow]● ИЗМЕНЁН=обновить+backup[/]  "
                        "[ansi_red]● ТОЛЬКО В DST=в backup[/]  "
                        "[ansi_cyan]● ЗАЩИЩЁН=не трогать[/]",
                        id="legend"
                    )
                    yield DataTable(id="plan-table", cursor_type="row")
                yield InfoPanel(id="info")
        with Horizontal(id="progress-row"):
            yield ProgressBar(id="prog-bar", total=100, show_percentage=True)
            yield Label("", id="prog-label")
        with Horizontal(id="statusbar"):
            yield Label("Готов. Нажмите Сканировать (Ctrl+S)", id="slabel")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#plan-table", DataTable)
        t.add_columns("Действие", "Что будет сделано", "Файл", "Размер")
        self._update_direction_label()
        # Прячем прогресс-бар до начала операции
        self.query_one("#prog-bar", ProgressBar).update(progress=0, total=100)

    def _update_direction_label(self) -> None:
        p = self.current_profile
        src = p.src[-35:] if len(p.src) > 35 else p.src
        dst = p.dst[-35:] if len(p.dst) > 35 else p.dst
        self.query_one("#direction-lbl", Label).update(
            f"[bold]{p.name}[/]  {src} → {dst}"
        )

    # ── Кнопки ───────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-scan")
    def action_scan(self): self._do_scan()

    @on(Button.Pressed, "#btn-sync")
    def action_run_sync(self): self._start_sync(dry_run=False)

    @on(Button.Pressed, "#btn-dry")
    def action_dry_run(self): self._start_sync(dry_run=True)

    @on(Button.Pressed, "#btn-fm")
    def action_file_mgr(self):
        if not self._plan:
            self.notify("Сначала нажмите Сканировать", severity="warning")
            return
        self.push_screen(FileManagerScreen(self._plan, self.current_profile),
                         self._on_file_mgr_closed)

    @on(Button.Pressed, "#btn-rev")
    def _reverse(self):
        p = self.current_profile
        p.src, p.dst = p.dst, p.src
        self._update_direction_label()
        self.notify(f"Направление изменено:\n{p.src} → {p.dst}", severity="information", timeout=5)

    @on(Button.Pressed, "#btn-drive")
    def _pick_drive(self):
        # Открываем диалог выбора папки вместо выбора диска
        self.push_screen(
            FolderPickerScreen("Выберите папку-источник", self.current_profile.src),
            self._on_folder_picked
        )

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

    # ── Коллбеки экранов ─────────────────────────────────────────────────────

    def _on_file_mgr_closed(self, new_plan) -> None:
        if new_plan is not None:
            self._plan = new_plan
            self._fill_table(self._plan)
            self.notify("Изменения применены к плану", severity="information")

    def _on_drive_picked(self, path: Optional[str]) -> None:
        if path:
            self.current_profile.src = path.rstrip("\\/")
            save_profiles(self.profiles)
            self._update_direction_label()
            self.notify(f"Источник установлен: {path}", severity="information")

    def _on_folder_picked(self, path: Optional[str]) -> None:
        """Callback после выбора папки в FolderPickerScreen."""
        if path:
            self.current_profile.src = path
            save_profiles(self.profiles)
            self._update_direction_label()
            self.notify(f"Источник: {path}", severity="information")

    def _on_profile_picked(self, name: Optional[str]) -> None:
        if name and name in self.profiles:
            self.current_profile = self.profiles[name]
            self._update_direction_label()
            self.notify(f"Профиль: {name}", severity="information")

    def _on_settings_saved(self, saved: bool) -> None:
        if saved:
            self._update_direction_label()
            self.notify("Профиль сохранён", severity="information")

    # ── Сканирование ─────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def _do_scan(self) -> None:
        self.call_from_thread(self._set_status, "Сканирование...")
        self.call_from_thread(self.query_one("#info", InfoPanel).clear_log)
        self.call_from_thread(self._set_progress, 0, 100, "")

        p = self.current_profile
        src = Path(p.src)
        dst = Path(p.dst)

        if not src.exists():
            self.call_from_thread(self.notify, f"Источник не найден: {src}", severity="error")
            self.call_from_thread(self._set_status, f"Ошибка: источник не найден")
            return

        try:
            dst.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.call_from_thread(self.notify, f"Ошибка dst: {e}", severity="error")
            return

        self.call_from_thread(self._set_progress, 10, 100, "Сканирование источника...")
        _src_count = [0]
        def _src_cb(path):
            _src_count[0] += 1
            if _src_count[0] % 20 == 0:
                self.call_from_thread(
                    self._set_status,
                    f"Сканирование источника: {_src_count[0]} файлов... {path.name}"
                )
        src_tree = scan_directory(src, include_patterns=p.include_patterns or None,
                                  exclude_patterns=p.exclude_patterns,
                                  use_hash=p.use_hash, ignore_hidden=p.ignore_hidden,
                                  progress_cb=_src_cb)

        self.call_from_thread(self._set_progress, 40, 100, "Сканирование приёмника...")
        _dst_count = [0]
        def _dst_cb(path):
            _dst_count[0] += 1
            if _dst_count[0] % 20 == 0:
                self.call_from_thread(
                    self._set_status,
                    f"Сканирование приёмника: {_dst_count[0]} файлов... {path.name}"
                )
        dst_tree = scan_directory(dst, include_patterns=p.include_patterns or None,
                                  exclude_patterns=p.exclude_patterns,
                                  use_hash=p.use_hash, ignore_hidden=p.ignore_hidden,
                                  progress_cb=_dst_cb)

        self.call_from_thread(self._set_progress, 70, 100, "Построение плана...")
        engine = DiffEngine(p)
        self._plan = engine.compute_plan(src_tree, dst_tree)

        summary = summarize_plan(self._plan)
        self.call_from_thread(self._fill_table, self._plan)

        try:
            stats = get_directory_stats(src)
            self.call_from_thread(self.query_one("#info", InfoPanel).update_stats, stats, p)
        except Exception:
            pass
        try:
            self.call_from_thread(self.query_one("#src-tree", FileTreePanel).load_path, src)
        except Exception:
            pass

        c = summary["counts"]
        mb = summary["bytes_to_copy"] / (1024 * 1024)
        method = "SHA-256" if p.use_hash else "размер+дата"
        status = (
            f"[{method}]  "
            f"Новых: {c.get('copy_new', 0)}  "
            f"Изменённых: {c.get('copy_update', 0)}  "
            f"В backup: {c.get('delete', 0)}  "
            f"Одинаковых: {c.get('skip_equal', 0)}  "
            f"Защищённых: {c.get('skip_protected', 0)}  "
            f"| {mb:.1f} MB к копированию"
        )
        self.call_from_thread(self._set_status, status)
        self.call_from_thread(self._set_progress, 100, 100, "Готово")

        info = self.query_one("#info", InfoPanel)
        self.call_from_thread(info.log, f"src: {len(src_tree)} файлов  dst: {len(dst_tree)} файлов")
        self.call_from_thread(info.log,
            f"Новых: {c.get('copy_new',0)}  "
            f"Изменённых: {c.get('copy_update',0)}  "
            f"В backup: {c.get('delete',0)}")
        self.logger.info(f"Scan: src={len(src_tree)} dst={len(dst_tree)}")

    def _fill_table(self, plan: list[SyncAction]) -> None:
        t = self.query_one("#plan-table", DataTable)
        t.clear()
        for a in plan:
            if a.action == ActionType.SKIP_EQUAL:
                continue
            style = ACTION_STYLE[a.action]
            rel = str(a.rel_path)
            if len(rel) > 60:
                rel = "..." + rel[-57:]
            t.add_row(
                Text(ACTION_LABEL[a.action], style=style),
                Text(ACTION_WILL_DO[a.action], style=style),
                Text(rel),
                Text(_fmt_size(a.size_bytes)),
            )

    # ── Синхронизация ─────────────────────────────────────────────────────────

    def _start_sync(self, dry_run: bool) -> None:
        if not self._plan:
            self.notify("Сначала нажмите Сканировать", severity="warning")
            return
        active = [a for a in self._plan if a.action not in (
            ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED)]
        if not active:
            self.notify("Нет изменений — всё актуально.", severity="information")
            return

        needs_double = any(
            a.protection_level == ProtectionLevel.DOUBLE and
            a.action in (ActionType.COPY_UPDATE, ActionType.DELETE)
            for a in active)

        p = self.current_profile
        n_copy = sum(1 for a in active if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE))
        n_del  = sum(1 for a in active if a.action == ActionType.DELETE)
        mb = sum(a.size_bytes for a in active
                 if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE)) / (1024 * 1024)

        prefix = "[bold]DRY RUN — симуляция[/]\n\n" if dry_run else "[bold]Подтвердите синхронизацию[/]\n\n"
        msg = (
            f"{prefix}"
            f"Источник: {p.src}\n"
            f"Приёмник: {p.dst}\n\n"
            f"Скопировать/обновить: {n_copy} файлов ({mb:.1f} MB)\n"
            f"Переместить в backup: {n_del} файлов\n\n"
            f"{'Запустить симуляцию?' if dry_run else 'Продолжить?'}"
        )
        self.push_screen(ConfirmDialog(msg, double_confirm=needs_double),
                         lambda ok: self._run_sync_worker(ok, active, dry_run))

    @work(thread=True, exclusive=True)
    def _run_sync_worker(self, confirmed: bool, actions: list[SyncAction], dry_run: bool) -> None:
        if not confirmed:
            return

        from application.sync_engine import SyncEngine

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
            self.call_from_thread(info.log, msg, style)
            self.call_from_thread(
                self._set_progress, done, total,
                f"{done}/{total}  {_fmt_size(action.size_bytes)}"
            )

        engine = SyncEngine(profile=p, src=Path(p.src), dst=Path(p.dst),
                            dry_run=dry_run, progress_cb=on_progress)
        report = SyncReport()
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(engine.execute(actions, report))
            finally:
                loop.close()
        except Exception as e:
            self.call_from_thread(self.notify, f"Ошибка: {e}", severity="error")
            return

        self.call_from_thread(self._set_progress, total, total, "Готово")

        if dry_run:
            s = report.stats
            mb_c = report.bytes_copied / (1024 * 1024)
            mb_b = report.bytes_backed_up / (1024 * 1024)
            summary = (
                f"Скопировать: {s.get('copy_new',0)} новых + {s.get('copy_update',0)} обновлений "
                f"({mb_c:.1f} MB)  |  В backup: {s.get('delete',0)} файлов ({mb_b:.1f} MB)"
            )
            self.call_from_thread(self.push_screen,
                                  DryRunLogScreen(report.log_lines, summary),
                                  lambda _: None)
            self.call_from_thread(self._set_status, f"[DRY RUN] {summary}")
        else:
            try:
                rpath = save_report(report)
                self.call_from_thread(info.log, f"Отчёт: {rpath}")
            except Exception:
                pass
            s = report.stats
            msg = (
                f"Готово: +{s.get('copy_new',0)} новых  "
                f"↻{s.get('copy_update',0)} обновлено  "
                f"⊘{s.get('delete',0)} в backup"
            )
            self.call_from_thread(self._set_status, msg)
            if report.errors:
                self.call_from_thread(
                    self.notify, f"Завершено с {len(report.errors)} ошибками", severity="warning")
            else:
                self.call_from_thread(self.notify, "Синхронизация завершена!", severity="information")

    # ── Утилиты ───────────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#slabel", Label).update(msg)
        except Exception:
            pass

    def _set_progress(self, done: int, total: int, label: str) -> None:
        try:
            pb = self.query_one("#prog-bar", ProgressBar)
            pb.update(progress=done, total=max(total, 1))
            self.query_one("#prog-label", Label).update(label)
        except Exception:
            pass


def _fmt_size(size: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def run():
    FlashSyncApp().run()


if __name__ == "__main__":
    run()