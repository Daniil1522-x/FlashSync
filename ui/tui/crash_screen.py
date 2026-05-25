"""
ui/tui/crash_screen.py — Экран просмотра crash-репортов в TUI.
"""
from __future__ import annotations
from pathlib import Path

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, Button, DataTable, RichLog
from textual import on

from infrastructure.crash_reporter import get_crash_reports, CRASH_DIR


class CrashReportsScreen(ModalScreen):
    CSS = """
    CrashReportsScreen { align: center middle; }
    #cr-root {
        width: 88%; height: 88%;
        border: thick $error;
        background: $surface; padding: 0;
        layout: vertical;
    }
    #cr-header {
        height: 2; background: $error; color: $background;
        padding: 0 1; align: left middle;
    }
    #cr-body { height: 1fr; layout: horizontal; }
    #cr-list { width: 36%; border-right: solid $primary; }
    #cr-list DataTable { height: 1fr; }
    #cr-detail { width: 1fr; }
    #cr-detail RichLog { height: 1fr; }
    #cr-footer {
        height: 3; layout: horizontal;
        background: $surface-darken-2; padding: 0 1; align: left middle;
    }
    #cr-footer Button { margin: 0 1; }
    #cr-footer Label { width: 1fr; color: $text-muted; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="cr-root"):
            yield Label(
                " 🔴  Crash-репорты FlashSync — история падений",
                id="cr-header"
            )
            with Horizontal(id="cr-body"):
                with Vertical(id="cr-list"):
                    yield Label("[dim] Выберите репорт [/]")
                    yield DataTable(id="cr-table", cursor_type="row")
                with Vertical(id="cr-detail"):
                    yield Label("[dim] Содержимое репорта [/]")
                    yield RichLog(id="cr-log", markup=False)
            with Horizontal(id="cr-footer"):
                yield Button("Открыть папку", id="btn-folder", variant="default")
                yield Button("Удалить выбранный", id="btn-del-one", variant="warning")
                yield Button("Удалить все", id="btn-del-all", variant="error")
                yield Button("Закрыть", id="btn-close", variant="default")
                yield Label("", id="cr-status")

    def on_mount(self) -> None:
        t = self.query_one("#cr-table", DataTable)
        t.add_columns("Дата", "Источник", "Размер")
        self._load_list()

    def _load_list(self) -> None:
        t = self.query_one("#cr-table", DataTable)
        t.clear()
        self._reports = get_crash_reports()
        if not self._reports:
            self.query_one("#cr-status", Label).update("Crash-репортов нет — всё чисто")
            return
        for p in self._reports:
            try:
                stat = p.stat()
                # Дата из имени: crash_20260418_213055_main_thread.txt
                parts = p.stem.split("_")
                date_str = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:8]} {parts[2][:2]}:{parts[2][2:4]}" if len(parts) > 2 else p.stem
                source = "_".join(parts[3:]) if len(parts) > 3 else "unknown"
                size = f"{stat.st_size:,} B"
                t.add_row(date_str, source, size, key=str(p))
            except Exception:
                t.add_row("?", p.name, "?", key=str(p))
        self.query_one("#cr-status", Label).update(
            f"Найдено {len(self._reports)} репортов"
        )

    @on(DataTable.RowSelected, "#cr-table")
    def _show_report(self, event: DataTable.RowSelected) -> None:
        if event.cursor_row >= len(self._reports):
            return
        path = self._reports[event.cursor_row]
        log = self.query_one("#cr-log", RichLog)
        log.clear()
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
            for line in content.splitlines():
                log.write(line)
        except Exception as e:
            log.write(f"Ошибка чтения: {e}")

    @on(Button.Pressed, "#btn-folder")
    def _open_folder(self) -> None:
        try:
            import subprocess, os
            if os.name == "nt":
                subprocess.Popen(["explorer", str(CRASH_DIR)])
            else:
                subprocess.Popen(["xdg-open", str(CRASH_DIR)])
        except Exception as e:
            self.notify(f"Не удалось открыть: {e}", severity="error")

    @on(Button.Pressed, "#btn-del-one")
    def _del_one(self) -> None:
        t = self.query_one("#cr-table", DataTable)
        if t.cursor_row < len(self._reports):
            path = self._reports[t.cursor_row]
            try:
                path.unlink()
                self.notify("Репорт удалён", severity="information")
                self._load_list()
                self.query_one("#cr-log", RichLog).clear()
            except Exception as e:
                self.notify(f"Ошибка: {e}", severity="error")

    @on(Button.Pressed, "#btn-del-all")
    def _del_all(self) -> None:
        deleted = 0
        for p in list(self._reports):
            try:
                p.unlink()
                deleted += 1
            except Exception:
                pass
        self._load_list()
        self.query_one("#cr-log", RichLog).clear()
        self.notify(f"Удалено {deleted} репортов", severity="information")

    @on(Button.Pressed, "#btn-close")
    def _close(self) -> None:
        self.dismiss(None)