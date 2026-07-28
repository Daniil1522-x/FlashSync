"""
ui/tui/folder_picker.py — Экран выбора папки.

ВАЖНО про копирование/вставку пути:
Textual Input поддерживает стандартные системные сочетания клавиш
(Ctrl+C/Ctrl+V на Linux/Windows, Cmd+C/Cmd+V на macOS) НАТИВНО на уровне
терминала — сам виджет Input не должен их перехватывать или блокировать.
Если paste не работал, типичная причина — родительский экран ловил
те же клавиши через BINDINGS и съедал событие до того как оно дошло
до Input. Здесь у ModalScreen нет конфликтующих BINDINGS, а сам Input
оставлен в "чистом" режиме без обработчиков on_key, которые могли бы
перехватывать ввод раньше виджета.
"""
from __future__ import annotations
import os
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, Button, Input, DataTable
from textual import on

from infrastructure.drives import get_removable_drives


class FolderPickerScreen(ModalScreen):
    """
    Экран навигации по файловой системе.
    Возвращает выбранный путь (str) через dismiss(), либо None при отмене.
    """

    CSS = """
    FolderPickerScreen { align: center middle; }
    #fp-root {
        width: 86%; height: 86%;
        border: thick $primary;
        background: $surface; padding: 1 2;
        layout: vertical;
    }
    #fp-title { height: 1; margin-bottom: 1; }
    #fp-path-row { height: 3; layout: horizontal; margin-bottom: 1; }
    #fp-path-row Input { width: 1fr; }
    #fp-nav-row { height: 3; layout: horizontal; margin-bottom: 1; }
    #fp-nav-row Button { margin-right: 1; }
    #fp-table { height: 1fr; border: solid $accent; margin-bottom: 1; }
    #fp-footer { height: 3; layout: horizontal; }
    #fp-footer Button { margin-right: 1; }
    #fp-error { color: $error; height: 1; }
    """

    def __init__(self, title: str, current_path: str = ""):
        super().__init__()
        self._title = title
        # Стартовая точка: текущий путь если валиден, иначе домашняя папка
        start = Path(current_path) if current_path else Path.home()
        if not start.exists() or not start.is_dir():
            start = start.parent if start.parent.exists() else Path.home()
        self._current_dir = start
        self._entries: list[Path] = []

    def compose(self) -> ComposeResult:
        with Container(id="fp-root"):
            yield Label(f"[bold]{self._title}[/]", id="fp-title")

            with Horizontal(id="fp-path-row"):
                # Обычный Input — поддерживает копирование/вставку через
                # стандартные сочетания терминала (Ctrl+C/V, выделение мышью)
                yield Input(value=str(self._current_dir), id="fp-path-input",
                           placeholder="Введите путь вручную или используйте навигацию ниже")
                yield Button("Перейти", id="btn-goto", variant="primary")

            with Horizontal(id="fp-nav-row"):
                yield Button("↑ Наверх", id="btn-up", variant="default")
                yield Button("💾 Диски", id="btn-drives", variant="default")
                yield Button("📁 Домой", id="btn-home", variant="default")

            yield DataTable(id="fp-table", cursor_type="row")
            yield Label("", id="fp-error")

            with Horizontal(id="fp-footer"):
                yield Button("✅ Выбрать этот путь", id="btn-select", variant="success")
                yield Button("❌ Отмена", id="btn-cancel", variant="error")

    def on_mount(self) -> None:
        t = self.query_one("#fp-table", DataTable)
        t.add_columns(" ", "Имя", "Тип")
        self._refresh_listing()

    # ── Навигация ────────────────────────────────────────────────────────────

    # Не даём UI замереть на папках с десятками/сотнями тысяч записей
    # (node_modules, корень диска, Windows\WinSxS и т.п.) — без лимита
    # sorted(iterdir()) в главном потоке мог морозить интерфейс на минуты.
    MAX_ENTRIES = 2000

    def _refresh_listing(self) -> None:
        t = self.query_one("#fp-table", DataTable)
        t.clear()
        self._entries = []
        err_lbl = self.query_one("#fp-error", Label)
        err_lbl.update("")

        try:
            # os.scandir дешевле чем Path.iterdir для больших папок —
            # is_dir() из DirEntry часто берётся из кэша readdir без лишнего stat()
            import os
            raw_entries = []
            with os.scandir(self._current_dir) as it:
                for entry in it:
                    if entry.name.startswith("."):
                        continue
                    try:
                        if not entry.is_dir():
                            continue  # выбираем только папки — не тратим время на файлы
                    except OSError:
                        continue
                    raw_entries.append(entry.name)
                    if len(raw_entries) >= self.MAX_ENTRIES:
                        break
            entries = sorted(self._current_dir / name for name in raw_entries)
            truncated = len(raw_entries) >= self.MAX_ENTRIES
        except PermissionError:
            err_lbl.update(f"⚠ Нет доступа: {self._current_dir}")
            return
        except OSError as e:
            err_lbl.update(f"⚠ Ошибка: {e}")
            return

        for p in entries:
            self._entries.append(p)
            t.add_row("📁", p.name, "папка", key=str(p))

        if truncated:
            err_lbl.update(
                f"Показаны первые {self.MAX_ENTRIES} папок (их больше) — "
                f"введите точный путь вручную если нужной нет в списке"
            )

        self.query_one("#fp-path-input", Input).value = str(self._current_dir)

    def _go_to(self, path: Path) -> None:
        if not path.exists():
            self.query_one("#fp-error", Label).update(f"⚠ Путь не существует: {path}")
            return
        if not path.is_dir():
            self.query_one("#fp-error", Label).update(f"⚠ Это не папка: {path}")
            return
        self._current_dir = path
        self._refresh_listing()

    # ── Обработчики ──────────────────────────────────────────────────────────

    @on(DataTable.RowSelected, "#fp-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self._entries):
            self._go_to(self._entries[event.cursor_row])

    @on(Button.Pressed, "#btn-up")
    def _go_up(self) -> None:
        parent = self._current_dir.parent
        if parent != self._current_dir:
            self._go_to(parent)

    @on(Button.Pressed, "#btn-home")
    def _go_home(self) -> None:
        self._go_to(Path.home())

    @on(Button.Pressed, "#btn-drives")
    def _show_drives(self) -> None:
        drives = get_removable_drives()
        if not drives:
            self.query_one("#fp-error", Label).update("Диски не найдены")
            return
        t = self.query_one("#fp-table", DataTable)
        t.clear()
        self._entries = []
        for d in drives:
            self._entries.append(Path(d["path"]))
            icon = "💾" if d["removable"] else "🖴"
            t.add_row(icon, d["display"], "диск", key=d["path"])

    @on(Button.Pressed, "#btn-goto")
    def _goto_typed_path(self) -> None:
        """Переход по пути, введённому (или вставленному) в поле Input."""
        typed = self.query_one("#fp-path-input", Input).value.strip()
        if typed:
            self._go_to(Path(typed))

    @on(Input.Submitted, "#fp-path-input")
    def _on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter в поле пути — переходит туда же, что и кнопка Перейти."""
        typed = event.value.strip()
        if typed:
            self._go_to(Path(typed))

    @on(Button.Pressed, "#btn-select")
    def _select(self) -> None:
        # Берём актуальное значение поля — если пользователь вставил/напечатал путь
        # вручную и не нажал "Перейти", выбираем именно его, а не self._current_dir
        typed = self.query_one("#fp-path-input", Input).value.strip()
        result = typed if typed else str(self._current_dir)
        self.dismiss(result)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)
