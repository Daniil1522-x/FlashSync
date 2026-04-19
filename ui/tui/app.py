"""
ui/tui/app.py — Textual TUI для FlashSync.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.widgets import (
    Header, Footer, DataTable, Tree, Label,
    Button, Input, Static, ProgressBar, Log, Checkbox,
)
from textual import work, on
from textual.screen import ModalScreen
from rich.text import Text

from domain.models import (
    SyncAction, ActionType, SyncProfile, SyncReport, ProtectionLevel
)
from application.differ import DiffEngine, summarize_plan
from infrastructure.scanner import scan_directory, get_directory_stats
from infrastructure.storage import load_profiles, save_profiles, save_report, setup_logger


# ── Цвета Textual (только ansi_* или hex) ────────────────────────────────────
ACTION_STYLE = {
    ActionType.COPY_NEW:       "ansi_bright_green",
    ActionType.COPY_UPDATE:    "ansi_yellow",
    ActionType.DELETE:         "ansi_red",
    ActionType.SKIP_EQUAL:     "ansi_bright_black",
    ActionType.SKIP_PROTECTED: "ansi_cyan",
}
ACTION_ICON = {
    ActionType.COPY_NEW:       "+",
    ActionType.COPY_UPDATE:    "~",
    ActionType.DELETE:         "-",
    ActionType.SKIP_EQUAL:     "=",
    ActionType.SKIP_PROTECTED: "L",
}


# ── Диалог подтверждения ──────────────────────────────────────────────────────

class ConfirmDialog(ModalScreen):
    CSS = """
    ConfirmDialog { align: center middle; }
    #dlg {
        width: 64; height: 14;
        border: thick $primary;
        background: $surface;
        padding: 2 3;
    }
    #dlg Label { margin-bottom: 2; }
    #dlg Button { margin: 0 1; }
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
                    "ВТОРОЕ ПОДТВЕРЖДЕНИЕ!\n" + self.message
                )
                return
        self.dismiss(True)

    @on(Button.Pressed, "#no")
    def _no(self):
        self.dismiss(False)


# ── Экран настроек ────────────────────────────────────────────────────────────

class SettingsScreen(ModalScreen):
    CSS = """
    SettingsScreen { align: center middle; }
    #sc {
        width: 72; height: 20;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    Input { margin-bottom: 1; }
    """

    def __init__(self, profiles: dict, current: SyncProfile):
        super().__init__()
        self.profiles = profiles
        self.current = current

    def compose(self) -> ComposeResult:
        with Container(id="sc"):
            yield Label("Настройки профиля")
            yield Input(value=self.current.src, placeholder="Источник (src)", id="inp-src")
            yield Input(value=self.current.dst, placeholder="Приёмник (dst)", id="inp-dst")
            with Horizontal():
                yield Checkbox("SHA-256", value=self.current.use_hash, id="chk-hash")
                yield Checkbox("Удалять из dst", value=self.current.delete_mode, id="chk-del")
                yield Checkbox("Игн. скрытые", value=self.current.ignore_hidden, id="chk-hid")
            with Horizontal():
                yield Button("Сохранить", variant="success", id="btn-save")
                yield Button("Отмена", variant="default", id="btn-cancel")

    @on(Button.Pressed, "#btn-save")
    def _save(self):
        # Берём значение напрямую — без Path() обёртки, чтобы не дублировать диск
        src_val = self.query_one("#inp-src", Input).value.strip()
        dst_val = self.query_one("#inp-dst", Input).value.strip()

        # Нормализуем на случай если пользователь вставил с дублем
        from infrastructure.storage import _normalize_path
        self.current.src = _normalize_path(src_val)
        self.current.dst = _normalize_path(dst_val)
        self.current.use_hash = self.query_one("#chk-hash", Checkbox).value
        self.current.delete_mode = self.query_one("#chk-del", Checkbox).value
        self.current.ignore_hidden = self.query_one("#chk-hid", Checkbox).value
        save_profiles(self.profiles)
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self):
        self.dismiss(False)


# ── Панель дерева файлов ──────────────────────────────────────────────────────

class FileTreePanel(Container):
    DEFAULT_CSS = """
    FileTreePanel { width: 28; border: solid $accent; background: $surface-darken-1; }
    FileTreePanel #ftitle { background: $accent; color: $background; text-align: center; height: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label(" Источник ", id="ftitle")
        yield Tree("...", id="ftree")

    def load_path(self, path: Path) -> None:
        tree = self.query_one("#ftree", Tree)
        tree.clear()
        tree.root.label = f"[{path.name}]"
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
        for e in entries[:40]:
            if e.name.startswith("."):
                continue
            if e.is_dir():
                child = node.add(f"D {e.name}", data=e)
                self._add(child, e, depth + 1)
            else:
                node.add_leaf(f"  {e.name}", data=e)


# ── Панель статистики ─────────────────────────────────────────────────────────

class StatsPanel(Container):
    DEFAULT_CSS = """
    StatsPanel { width: 32; border: solid $primary; background: $surface-darken-1; padding: 1; }
    """

    def compose(self) -> ComposeResult:
        yield Label("Статистика", id="stitle")
        yield Static("", id="scontent")
        yield Label("─" * 28)
        yield Log(id="slog", max_lines=80)

    def update_stats(self, stats: dict) -> None:
        mb = stats.get("total_size", 0) / (1024 * 1024)
        lines = [
            f"Файлов: {stats.get('total_files', 0):,}",
            f"Размер: {mb:.1f} MB",
            "─" * 26,
        ]
        for cat, cnt in sorted(stats.get("categories", {}).items(), key=lambda x: -x[1])[:6]:
            lines.append(f"  {cat:<10} {cnt:>4}")
        self.query_one("#scontent", Static).update("\n".join(lines))

    def log(self, msg: str) -> None:
        self.query_one("#slog", Log).write_line(msg)


# ── Главное приложение ────────────────────────────────────────────────────────

class FlashSyncApp(App):
    TITLE = "FlashSync Pro"

    CSS = """
    Screen { background: $background; }
    #toolbar {
        layout: horizontal;
        height: 3;
        background: $surface-darken-2;
        padding: 0 1;
        align: left middle;
    }
    #toolbar Button { margin: 0 1; min-width: 14; }
    #main-layout { layout: horizontal; height: 1fr; }
    #center { width: 1fr; border: solid $primary; background: $surface; }
    #plan-table { height: 1fr; }
    #statusbar {
        height: 3;
        background: $surface-darken-2;
        padding: 0 1;
        layout: horizontal;
        align: left middle;
    }
    #statusbar #slabel { width: 1fr; color: $text-muted; }
    """

    BINDINGS = [
        Binding("ctrl+s", "scan",     "Сканировать",      show=True),
        Binding("ctrl+r", "run_sync", "Синхронизировать", show=True),
        Binding("ctrl+d", "dry_run",  "Dry Run",          show=True),
        Binding("ctrl+p", "settings", "Настройки",        show=True),
        Binding("ctrl+q", "quit",     "Выход",            show=True),
    ]

    def __init__(self):
        super().__init__()
        self.profiles = load_profiles()
        self.current_profile: SyncProfile = list(self.profiles.values())[0]
        self.logger = setup_logger()
        self._plan: list[SyncAction] = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            with Horizontal(id="toolbar"):
                yield Button("Сканировать", id="btn-scan", variant="primary")
                yield Button("Синхронизировать", id="btn-sync", variant="success")
                yield Button("Dry Run", id="btn-dry", variant="warning")
                yield Button("Обратить src<->dst", id="btn-rev", variant="default")
                yield Button("Настройки", id="btn-cfg", variant="default")
                yield Label("", id="profile-lbl")
            with Horizontal(id="main-layout"):
                yield FileTreePanel(id="src-tree")
                with Vertical(id="center"):
                    yield DataTable(id="plan-table", cursor_type="row")
                yield StatsPanel(id="stats")
        with Horizontal(id="statusbar"):
            yield Label("Готов", id="slabel")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#plan-table", DataTable)
        t.add_columns(" ", "Действие", "Файл", "Размер", "Причина")
        self._update_profile_label()

    def _update_profile_label(self) -> None:
        p = self.current_profile
        self.query_one("#profile-lbl", Label).update(
            f"  [{p.name}]  {p.src} -> {p.dst}"
        )

    # ── Кнопки ───────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-scan")
    def action_scan(self): self._do_scan()

    @on(Button.Pressed, "#btn-sync")
    def action_run_sync(self): self._start_sync(dry_run=False)

    @on(Button.Pressed, "#btn-dry")
    def action_dry_run(self): self._start_sync(dry_run=True)

    @on(Button.Pressed, "#btn-rev")
    def _reverse(self):
        p = self.current_profile
        p.src, p.dst = p.dst, p.src
        self._update_profile_label()
        self.notify("src <-> dst поменяны местами")

    @on(Button.Pressed, "#btn-cfg")
    def action_settings(self):
        self.push_screen(
            SettingsScreen(self.profiles, self.current_profile),
            self._on_settings_closed
        )

    def _on_settings_closed(self, saved: bool) -> None:
        if saved:
            self._update_profile_label()
            self.notify("Профиль сохранён")

    # ── Сканирование ─────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def _do_scan(self) -> None:
        self.call_from_thread(self._set_status, "Сканирование...")
        p = self.current_profile
        src = Path(p.src)
        dst = Path(p.dst)

        if not src.exists():
            self.call_from_thread(self.notify, f"Источник не найден: {src}", severity="error")
            self.call_from_thread(self._set_status, "Ошибка: источник не найден")
            return

        try:
            dst.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.call_from_thread(self.notify, f"Ошибка dst: {e}", severity="error")
            self.call_from_thread(self._set_status, f"Ошибка dst: {e}")
            return

        self.call_from_thread(self._set_status, "Сканирование источника...")
        src_tree = scan_directory(
            src,
            include_patterns=p.include_patterns or None,
            exclude_patterns=p.exclude_patterns,
            use_hash=p.use_hash,
            ignore_hidden=p.ignore_hidden,
        )

        self.call_from_thread(self._set_status, "Сканирование приёмника...")
        dst_tree = scan_directory(
            dst,
            include_patterns=p.include_patterns or None,
            exclude_patterns=p.exclude_patterns,
            use_hash=p.use_hash,
            ignore_hidden=p.ignore_hidden,
        )

        self.call_from_thread(self._set_status, "Построение плана...")
        engine = DiffEngine(p)
        self._plan = engine.compute_plan(src_tree, dst_tree)

        summary = summarize_plan(self._plan)
        self.call_from_thread(self._fill_table, self._plan)

        try:
            stats = get_directory_stats(src)
            self.call_from_thread(self.query_one("#stats", StatsPanel).update_stats, stats)
        except Exception:
            pass

        try:
            self.call_from_thread(self.query_one("#src-tree", FileTreePanel).load_path, src)
        except Exception:
            pass

        c = summary["counts"]
        mb = summary["bytes_to_copy"] / (1024 * 1024)
        msg = (
            f"Готово | "
            f"+{c.get('copy_new', 0)} новых  "
            f"~{c.get('copy_update', 0)} обновить  "
            f"-{c.get('delete', 0)} удалить  "
            f"={c.get('skip_equal', 0)} одинаковых  "
            f"| {mb:.1f} MB"
        )
        self.call_from_thread(self._set_status, msg)
        self.call_from_thread(
            self.query_one("#stats", StatsPanel).log,
            f"src:{len(src_tree)} dst:{len(dst_tree)} план:{len(self._plan)}"
        )
        self.logger.info(f"Scan done: src={len(src_tree)} dst={len(dst_tree)}")

    def _fill_table(self, plan: list[SyncAction]) -> None:
        t = self.query_one("#plan-table", DataTable)
        t.clear()
        for a in plan:
            if a.action == ActionType.SKIP_EQUAL:
                continue
            icon = ACTION_ICON[a.action]
            style = ACTION_STYLE[a.action]
            rel = str(a.rel_path)
            if len(rel) > 55:
                rel = "..." + rel[-52:]
            t.add_row(
                Text(icon, style=style),
                Text(a.action.value, style=style),
                Text(rel),
                Text(_fmt_size(a.size_bytes)),
                Text(a.reason[:38]),
            )

    # ── Синхронизация ─────────────────────────────────────────────────────────

    def _start_sync(self, dry_run: bool) -> None:
        if not self._plan:
            self.notify("Сначала нажмите Сканировать", severity="warning")
            return

        active = [a for a in self._plan if a.action not in (
            ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED
        )]

        if not active:
            self.notify("Нет изменений для синхронизации", severity="information")
            return

        needs_double = any(
            a.protection_level == ProtectionLevel.DOUBLE and
            a.action in (ActionType.COPY_UPDATE, ActionType.DELETE)
            for a in active
        )

        prefix = "[DRY RUN] " if dry_run else ""
        msg = f"{prefix}Выполнить {len(active)} операций?"
        self.push_screen(
            ConfirmDialog(msg, double_confirm=needs_double),
            lambda ok: self._run_sync_worker(ok, active, dry_run)
        )

    @work(thread=True, exclusive=True)
    def _run_sync_worker(self, confirmed: bool, actions: list[SyncAction], dry_run: bool) -> None:
        if not confirmed:
            return

        from application.sync_engine import SyncEngine

        p = self.current_profile

        def on_progress(action: SyncAction, msg: str) -> None:
            self.call_from_thread(self._set_status, msg)
            self.call_from_thread(self.query_one("#stats", StatsPanel).log, msg)

        engine = SyncEngine(
            profile=p,
            src=Path(p.src),
            dst=Path(p.dst),
            dry_run=dry_run,
            progress_cb=on_progress,
        )

        report = SyncReport()
        try:
            # Создаём новый event loop в этом потоке (Textual worker — отдельный поток)
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(engine.execute(actions, report))
            finally:
                loop.close()
        except Exception as e:
            self.call_from_thread(self.notify, f"Ошибка: {e}", severity="error")
            return

        if not dry_run:
            try:
                save_report(report)
            except Exception:
                pass

        prefix = "[DRY RUN] " if dry_run else ""
        s = report.stats
        msg = (
            f"{prefix}Готово: "
            f"+{s.get('copy_new', 0)} ~{s.get('copy_update', 0)} -{s.get('delete', 0)}"
        )
        self.call_from_thread(self._set_status, msg)

        if report.errors:
            self.call_from_thread(
                self.notify, f"Завершено с {len(report.errors)} ошибками", severity="warning"
            )
        else:
            self.call_from_thread(
                self.notify, f"{prefix}Синхронизация завершена!", severity="information"
            )

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#slabel", Label).update(msg)
        except Exception:
            pass


# ── Утилиты ───────────────────────────────────────────────────────────────────

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
