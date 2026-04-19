"""
ui/tui/app.py — Полноценный Textual TUI для FlashSync.

Панели:
  - Левая: дерево файлов источника
  - Центр: план синхронизации (таблица)
  - Правая: статистика + лог
  - Нижняя: статус + прогресс
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
    Button, Input, Static, ProgressBar, Log,
    TabbedContent, TabPane, Checkbox, Select,
)
from textual.reactive import reactive
from textual import work, on
from textual.screen import ModalScreen
from textual.color import Color
from rich.text import Text
from rich.table import Table

from domain.models import (
    SyncAction, ActionType, SyncProfile, SyncReport, ProtectionLevel
)
from application.differ import DiffEngine, summarize_plan
from infrastructure.scanner import scan_directory, get_directory_stats
from infrastructure.storage import load_profiles, save_profiles, save_report, setup_logger


# ─── Цвета по типу действия ──────────────────────────────────────────────────
ACTION_COLORS = {
    ActionType.COPY_NEW:      "ansi_bright_green",
    ActionType.COPY_UPDATE:   "ansi_yellow",
    ActionType.DELETE:        "ansi_red",
    ActionType.SKIP_EQUAL:    "ansi_dim",
    ActionType.SKIP_PROTECTED:"ansi_cyan",
}

ACTION_ICONS = {
    ActionType.COPY_NEW:      "⊕",
    ActionType.COPY_UPDATE:   "↻",
    ActionType.DELETE:        "⊘",
    ActionType.SKIP_EQUAL:    "≡",
    ActionType.SKIP_PROTECTED:"🔒",
}


# ─── Диалог подтверждения ─────────────────────────────────────────────────────

class ConfirmDialog(ModalScreen):
    CSS = """
    ConfirmDialog {
        align: center middle;
    }
    #dialog {
        width: 68;
        height: 16;
        border: thick $primary;
        background: $surface;
        padding: 2 3;
    }
    """

    def __init__(self, message: str, double_confirm: bool = False):
        super().__init__()
        self.message = message
        self.double_confirm = double_confirm
        self.confirm_count = 0

    def compose(self) -> ComposeResult:
        with Container(id="dialog"):
            yield Label(self.message, id="question")
            with Horizontal():
                yield Button("Подтвердить", variant="warning", id="confirm")
                yield Button("Отмена", variant="default", id="cancel")

    @on(Button.Pressed, "#confirm")
    def handle_confirm(self):
        if self.double_confirm:
            self.confirm_count += 1
            if self.confirm_count < 2:
                self.query_one("#question", Label).update(
                    "⚠️  ДВОЙНОЕ ПОДТВЕРЖДЕНИЕ!\n\n" + self.message
                )
                return
        self.dismiss(True)

    @on(Button.Pressed, "#cancel")
    def handle_cancel(self):
        self.dismiss(False)


# ─── Панели (оставил почти как у тебя) ──────────────────────────────────────

class FileTreePanel(Container):
    DEFAULT_CSS = """
    FileTreePanel {
        width: 28;
        border: solid $accent;
        background: $surface-darken-1;
    }
    FileTreePanel #tree-title {
        background: $accent;
        color: $background;
        text-align: center;
        height: 1;
    }
    """

    def __init__(self, title: str = "Источник", **kwargs):
        super().__init__(**kwargs)
        self._title = title

    def compose(self) -> ComposeResult:
        yield Label(f" {self._title} ", id="tree-title")
        yield Tree("📁 ...", id="file-tree")

    # ... (остальной код FileTreePanel без изменений)


class StatsPanel(Container):
    DEFAULT_CSS = """
    StatsPanel {
        width: 32;
        border: solid $primary;
        background: $surface-darken-1;
        padding: 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Label("📊 Статистика", id="stats-title")
        yield Static("", id="stats-content")
        yield Label("─" * 30)
        yield Log(id="sync-log", max_lines=100)

    def update_stats(self, stats: dict) -> None:
        total = stats.get("total_files", 0)
        size_mb = stats.get("total_size", 0) / (1024 * 1024)
        lines = [
            f"📁 Файлов: {total:,}",
            f"💾 Размер: {size_mb:.1f} МБ",
            "─" * 28,
        ]
        for cat, cnt in sorted(stats.get("categories", {}).items(), key=lambda x: -x[1])[:6]:
            lines.append(f"  {cat}: {cnt}")
        self.query_one("#stats-content", Static).update("\n".join(lines))

    def log(self, msg: str) -> None:
        self.query_one("#sync-log", Log).write_line(msg)


# ─── Главное приложение ───────────────────────────────────────────────────────

class FlashSyncApp(App):
    TITLE = "⚡ FlashSync Pro"

    CSS = """
    Screen {
        background: $background;
    }
    #main-layout {
        layout: horizontal;
        height: 1fr;
    }
    #center-panel {
        width: 1fr;
        border: solid $primary;
        background: $surface;
    }
    #plan-table {
        height: 1fr;
    }
    #toolbar {
        layout: horizontal;
        height: 3;
        background: $surface-darken-2;
        padding: 0 1;
        align: left middle;
    }
    #toolbar Button {
        margin: 0 1;
        min-width: 14;
    }
    #status-bar {
        height: 3;
        background: $surface-darken-2;
        padding: 0 1;
        layout: horizontal;
        align: left middle;
    }
    #progress-bar {
        width: 30;
        margin: 0 2;
    }
    #status-label {
        width: 1fr;
        color: $text-muted;
    }

    .action-copy-new       { color: ansi_bright_green; }
    .action-copy-update    { color: ansi_yellow; }
    .action-delete         { color: ansi_red; }
    .action-skip-equal     { color: ansi_bright_black; }
    .action-skip-protected { color: ansi_cyan; }
    """

    # ... (весь остальной код класса FlashSyncApp без изменений)

    # Только заменил старые цвета на ansi_*

    # В _update_table можно добавить классы для строк:
    # table.add_row(..., classes=f"action-{action.action.value.replace('_', '-')}")

    BINDINGS = [
        Binding("ctrl+s", "scan",     "Сканировать",   show=True),
        Binding("ctrl+r", "run_sync", "Синхронизировать", show=True),
        Binding("ctrl+d", "dry_run",  "Dry Run",       show=True),
        Binding("ctrl+p", "settings", "Настройки",     show=True),
        Binding("ctrl+q", "quit",     "Выход",         show=True),
        Binding("f5",     "scan",     "Обновить"),
    ]

    _actions: reactive[list[SyncAction]] = reactive([], layout=True)
    _status: reactive[str] = reactive("Готов")

    def __init__(self):
        super().__init__()
        self.profiles = load_profiles()
        self.current_profile = list(self.profiles.values())[0]
        self.logger = setup_logger()
        self._plan: list[SyncAction] = []
        self._report: Optional[SyncReport] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            # Панель инструментов
            with Horizontal(id="toolbar"):
                yield Button("⚡ Сканировать", id="btn-scan", variant="primary")
                yield Button("▶ Синхронизировать", id="btn-sync", variant="success")
                yield Button("👁 Dry Run", id="btn-dry", variant="warning")
                yield Button("🔄 ↔ Обратная", id="btn-reverse", variant="default")
                yield Button("⚙ Профили", id="btn-settings", variant="default")
                yield Label("  ", id="spacer")
                yield Label(f"Профиль: {self.current_profile.name}", id="profile-label")

            # Основной layout
            with Horizontal(id="main-layout"):
                yield FileTreePanel("📁 Источник", id="src-tree")
                with Vertical(id="center-panel"):
                    yield DataTable(id="plan-table", cursor_type="row")
                with StatsPanel(id="stats-panel"):
                    pass

        # Статус-бар
        with Horizontal(id="status-bar"):
            yield Label("●", id="status-indicator")
            yield Label("Готов к работе", id="status-label")
            yield ProgressBar(id="progress-bar", show_percentage=False)
            yield Label("", id="bytes-label")

        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#plan-table", DataTable)
        table.add_columns("", "Тип", "Файл", "Размер", "Причина")
        self._refresh_profile_label()

    def _refresh_profile_label(self) -> None:
        p = self.current_profile
        label = self.query_one("#profile-label", Label)
        label.update(
            f"[bold]{p.name}[/bold]  "
            f"[dim]{p.src}[/dim] → [dim]{p.dst}[/dim]  "
            f"{'[red]DELETE[/red]' if p.delete_mode else ''}"
            f"{'[yellow]HASH[/yellow]' if p.use_hash else ''}"
        )

    # ─── Кнопки ──────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-scan")
    def action_scan(self) -> None:
        self._do_scan()

    @on(Button.Pressed, "#btn-sync")
    def action_run_sync(self) -> None:
        if not self._plan:
            self.notify("Сначала выполните сканирование!", severity="warning")
            return
        self._do_sync(dry_run=False)

    @on(Button.Pressed, "#btn-dry")
    def action_dry_run(self) -> None:
        if not self._plan:
            self._do_scan(then_dry_run=True)
        else:
            self._do_sync(dry_run=True)

    @on(Button.Pressed, "#btn-reverse")
    def action_reverse(self) -> None:
        p = self.current_profile
        p.src, p.dst = p.dst, p.src
        self._refresh_profile_label()
        self.notify("Направление синхронизации изменено ↔", severity="information")

    @on(Button.Pressed, "#btn-settings")
    def action_settings(self) -> None:
        self.push_screen(SettingsScreen(self.profiles, self.current_profile))

    # ─── Сканирование ────────────────────────────────────────────────────────

    @work(thread=True)
    def _do_scan(self, then_dry_run: bool = False) -> None:
        self.call_from_thread(self._set_status, "🔍 Сканирование...")
        p = self.current_profile
        src = Path(p.src)
        dst = Path(p.dst)

        if not src.exists():
            self.call_from_thread(self.notify, f"Источник не найден: {src}", severity="error")
            self.call_from_thread(self._set_status, "❌ Ошибка: источник не найден")
            return

        dst.mkdir(parents=True, exist_ok=True)

        self.call_from_thread(self._set_status, "🔍 Сканирование источника...")
        src_tree = scan_directory(
            src,
            include_patterns=p.include_patterns or None,
            exclude_patterns=p.exclude_patterns,
            use_hash=p.use_hash,
            ignore_hidden=p.ignore_hidden,
        )

        self.call_from_thread(self._set_status, "🔍 Сканирование приёмника...")
        dst_tree = scan_directory(
            dst,
            include_patterns=p.include_patterns or None,
            exclude_patterns=p.exclude_patterns,
            use_hash=p.use_hash,
            ignore_hidden=p.ignore_hidden,
        )

        self.call_from_thread(self._set_status, "⚙ Построение плана...")
        engine = DiffEngine(p)
        plan = engine.compute_plan(src_tree, dst_tree)
        self._plan = plan

        summary = summarize_plan(plan)
        self.call_from_thread(self._update_table, plan)

        # Статистика источника
        stats = get_directory_stats(src)
        self.call_from_thread(self.query_one("#stats-panel", StatsPanel).update_stats, stats)

        # Дерево
        self.call_from_thread(self.query_one("#src-tree", FileTreePanel).load_path, src)

        counts = summary["counts"]
        mb = summary["bytes_to_copy"] / (1024 * 1024)
        status = (
            f"✅ Готово: "
            f"[green]+{counts.get('copy_new', 0)}[/] "
            f"[yellow]↻{counts.get('copy_update', 0)}[/] "
            f"[red]⊘{counts.get('delete', 0)}[/] "
            f"[dim]≡{counts.get('skip_equal', 0)}[/] "
            f"— {mb:.1f} МБ к копированию"
        )
        self.call_from_thread(self._set_status, status)
        self.call_from_thread(
            self.query_one("#stats-panel", StatsPanel).log,
            f"Сканирование завершено: {len(src_tree)} файлов в src, {len(dst_tree)} в dst"
        )
        self.logger.info(f"Scan: src={len(src_tree)}, dst={len(dst_tree)}, plan={len(plan)}")

        if then_dry_run:
            self.call_from_thread(self._do_sync, True)

    def _update_table(self, plan: list[SyncAction]) -> None:
        table = self.query_one("#plan-table", DataTable)
        table.clear()
        for action in plan:
            if action.action == ActionType.SKIP_EQUAL:
                continue  # не показываем одинаковые
            icon = ACTION_ICONS[action.action]
            color = ACTION_COLORS[action.action]
            rel = str(action.rel_path)
            size = _fmt_size(action.size_bytes)
            row = [
                Text(icon),
                Text(action.action.value, style=color),
                Text(rel[-60:] if len(rel) > 60 else rel),
                Text(size),
                Text(action.reason[:40]),
            ]
            table.add_row(*row)

    # ─── Синхронизация ───────────────────────────────────────────────────────

    def _do_sync(self, dry_run: bool = False) -> None:
        if not self._plan:
            self.notify("Нет плана синхронизации. Сначала сканируйте.", severity="warning")
            return

        # Фильтруем только активные действия
        active = [a for a in self._plan if a.action not in (
            ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED
        )]

        if not active:
            self.notify("Нет изменений для синхронизации.", severity="information")
            return

        # Проверяем защищённые
        needs_double = any(
            a.protection_level == ProtectionLevel.DOUBLE
            for a in active if a.action in (ActionType.COPY_UPDATE, ActionType.DELETE)
        )

        msg = f"{'[DRY RUN] ' if dry_run else ''}Выполнить {len(active)} операций?"
        self.push_screen(
            ConfirmDialog(msg, double_confirm=needs_double),
            lambda result: self._on_sync_confirmed(result, active, dry_run)
        )

    def _on_sync_confirmed(self, confirmed: bool, actions: list[SyncAction], dry_run: bool) -> None:
        if confirmed:
            self._run_sync_worker(actions, dry_run)

    @work(thread=True)
    def _run_sync_worker(self, actions: list[SyncAction], dry_run: bool) -> None:
        from application.sync_engine import SyncEngine
        from domain.models import SyncReport
        import asyncio

        p = self.current_profile
        src = Path(p.src)
        dst = Path(p.dst)

        report = SyncReport()
        done_count = 0
        total = len(actions)

        def on_progress(action: SyncAction, msg: str) -> None:
            nonlocal done_count
            done_count += 1
            self.call_from_thread(self._set_status, f"{'[DRY] ' if dry_run else ''}{msg}")
            self.call_from_thread(
                self.query_one("#stats-panel", StatsPanel).log, msg
            )

        engine = SyncEngine(
            profile=p,
            src=src,
            dst=dst,
            dry_run=dry_run,
            progress_cb=on_progress,
        )

        try:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(engine.execute(actions, report))
            loop.close()
        except Exception as e:
            self.call_from_thread(self.notify, f"Ошибка: {e}", severity="error")
            return

        # Сохраняем отчёт
        if not dry_run:
            try:
                report_path = save_report(report)
                self.logger.info(f"Report saved: {report_path}")
            except Exception:
                pass

        prefix = "[DRY RUN] " if dry_run else ""
        stats = report.stats
        summary = (
            f"{prefix}✅ Готово: "
            f"[green]+{stats.get('copy_new', 0)}[/green] "
            f"[yellow]↻{stats.get('copy_update', 0)}[/yellow] "
            f"[red]⊘{stats.get('delete', 0)}[/red]"
        )
        self.call_from_thread(self._set_status, summary)
        if report.errors:
            self.call_from_thread(
                self.notify,
                f"Завершено с {len(report.errors)} ошибками",
                severity="warning"
            )
        else:
            self.call_from_thread(self.notify, f"{prefix}Синхронизация завершена!", severity="information")

    # ─── Утилиты ─────────────────────────────────────────────────────────────

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#status-label", Label).update(msg)
        except Exception:
            pass


# ─── Экран настроек ───────────────────────────────────────────────────────────

class SettingsScreen(ModalScreen):
    CSS = """
    SettingsScreen {
        align: center middle;
    }
    #settings-container {
        width: 70;
        height: 30;
        border: thick $background 80%;
        background: $surface;
        padding: 1 2;
    }
    Input { margin: 0 0 1 0; }
    Button { margin: 1 1 0 0; }
    """

    def __init__(self, profiles: dict, current: SyncProfile):
        super().__init__()
        self.profiles = profiles
        self.current = current

    def compose(self) -> ComposeResult:
        with Container(id="settings-container"):
            yield Label("⚙ Настройки профиля", id="settings-title")
            yield Input(value=self.current.src, placeholder="Источник (src)", id="inp-src")
            yield Input(value=self.current.dst, placeholder="Приёмник (dst)", id="inp-dst")
            with Horizontal():
                yield Checkbox("Хеш-сравнение (SHA-256)", value=self.current.use_hash, id="chk-hash")
                yield Checkbox("Удалять из dst", value=self.current.delete_mode, id="chk-delete")
                yield Checkbox("Игнорировать скрытые", value=self.current.ignore_hidden, id="chk-hidden")
            with Horizontal():
                yield Button("Сохранить", variant="success", id="btn-save")
                yield Button("Отмена", variant="default", id="btn-cancel")

    @on(Button.Pressed, "#btn-save")
    def save(self) -> None:
        self.current.src = self.query_one("#inp-src", Input).value
        self.current.dst = self.query_one("#inp-dst", Input).value
        self.current.use_hash = self.query_one("#chk-hash", Checkbox).value
        self.current.delete_mode = self.query_one("#chk-delete", Checkbox).value
        self.current.ignore_hidden = self.query_one("#chk-hidden", Checkbox).value
        save_profiles(self.profiles)
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel")
    def cancel(self) -> None:
        self.dismiss(False)


# ─── Вспомогательные функции ──────────────────────────────────────────────────

def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size}B"
    elif size < 1024 * 1024:
        return f"{size // 1024}K"
    elif size < 1024 * 1024 * 1024:
        return f"{size // (1024 * 1024)}M"
    else:
        return f"{size / (1024 ** 3):.1f}G"


def run():
    app = FlashSyncApp()
    app.run()


if __name__ == "__main__":
    run()
