"""
ui/tui/folder_picker.py — Выбор папки с навигацией.
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional
from textual.app import ComposeResult
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Input, Select
from textual.containers import Container, Horizontal, Vertical
from textual import on
import os


class FolderPickerScreen(ModalScreen):
    """Диалог выбора папки с полной навигацией."""

    CSS = """
    FolderPickerScreen { align: center middle; }
    #fp { 
        width: 90; 
        height: 20; 
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #fp Label { margin-bottom: 1; }
    #fp Input { margin-bottom: 1; width: 100%; }
    #fp .row { layout: horizontal; height: 3; margin-top: 1; }
    #fp Button { margin: 0 1; min-width: 15; }
    #fp .drive-list { height: 8; }
    #fp .drive-btn { width: 100%; text-align: left; }
    """

    def __init__(self, title: str = "Выберите папку", current_path: str = ""):
        super().__init__()
        self._title = title
        self._current = Path(current_path).resolve() if current_path else Path(".").resolve()

    def compose(self) -> ComposeResult:
        with Container(id="fp"):
            yield Label(f"[bold]{self._title}[/]")
            yield Label(f"[dim]Текущий путь:[/]", classes="hint")
            yield Input(value=str(self._current), id="path-input", placeholder="Введите путь или выберите ниже")

            # Кнопки быстрых действий
            with Horizontal(classes="row"):
                yield Button("⬆️ Наверх", id="btn-up", variant="default")
                yield Button("💾 Диски", id="btn-drives", variant="default")
                yield Button("📁 Домой", id="btn-home", variant="default")

            # Кнопки подтверждения
            with Horizontal(classes="row"):
                yield Button("✅ Выбрать этот путь", id="btn-select", variant="success")
                yield Button("❌ Отмена", id="btn-cancel", variant="default")

    @on(Button.Pressed, "#btn-up")
    def _go_up(self) -> None:
        """Перейти в родительскую директорию."""
        if self._current.parent and self._current.parent != self._current:
            self._current = self._current.parent
            self.query_one("#path-input", Input).value = str(self._current)
            self.notify(f"Папка: {self._current}", severity="information", timeout=2)

    @on(Button.Pressed, "#btn-home")
    def _go_home(self) -> None:
        """Перейти в домашнюю директорию."""
        self._current = Path.home()
        self.query_one("#path-input", Input).value = str(self._current)
        self.notify(f"Домашняя папка: {self._current}", severity="information", timeout=2)

    @on(Button.Pressed, "#btn-drives")
    def _show_drives(self) -> None:
        """Показать список дисков (Windows) или корневых разделов."""
        self.push_screen(DriveListScreen(), self._on_drive_selected)

    def _on_drive_selected(self, drive_path: Optional[str]) -> None:
        if drive_path:
            self._current = Path(drive_path)
            self.query_one("#path-input", Input).value = str(self._current)

    @on(Input.Changed, "#path-input")
    def _on_path_changed(self, event: Input.Changed) -> None:
        """Обновить текущий путь при ручном вводе."""
        try:
            new_path = Path(event.value).resolve()
            if new_path.exists() and new_path.is_dir():
                self._current = new_path
        except Exception:
            pass  # Игнорируем неверные пути

    @on(Button.Pressed, "#btn-select")
    def _select(self) -> None:
        """Подтвердить выбор текущего пути."""
        path_str = self.query_one("#path-input", Input).value.strip()
        try:
            path = Path(path_str).resolve()
            if path.exists() and path.is_dir():
                self.dismiss(str(path))
            else:
                self.notify(f"Папка не существует: {path}", severity="error")
        except Exception as e:
            self.notify(f"Ошибка пути: {e}", severity="error")

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)


class DriveListScreen(ModalScreen):
    """Список дисков для выбора."""

    CSS = """
    DriveListScreen { align: center middle; }
    #dl { 
        width: 70; 
        height: auto; 
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #dl Label { margin-bottom: 1; }
    #dl Button { width: 100%; margin-bottom: 1; text-align: left; }
    """

    def compose(self) -> ComposeResult:
        with Container(id="dl"):
            yield Label("[bold]Выберите диск:[/]")
            yield Label("")

            drives = self._get_drives()
            if not drives:
                yield Label("[dim]Диски не найдены[/]")

            for drive in drives:
                variant = "success" if drive.get("removable") else "default"
                yield Button(
                    f"{'💾' if drive.get('removable') else '🖴'} {drive['path']}  "
                    f"[{drive.get('label', 'Без имени')}]  "
                    f"Свободно: {drive.get('free', 'N/A')}",
                    id=f"drive-{drive['path']}",
                    variant=variant
                )

            yield Label("")
            yield Button("❌ Отмена", id="dl-cancel", variant="default")

    def _get_drives(self) -> list:
        """Получить список доступных дисков."""
        drives = []

        if os.name == "nt":
            # Windows
            import string
            import ctypes

            try:
                bitmask = ctypes.windll.kernel32.GetLogicalDrives()
                for letter in string.ascii_uppercase:
                    if bitmask & 1:
                        path = f"{letter}:\\"
                        try:
                            import shutil
                            usage = shutil.disk_usage(path)
                            free_gb = usage.free // (1024**3)

                            # Получить метку тома
                            label_buf = ctypes.create_unicode_buffer(256)
                            ctypes.windll.kernel32.GetVolumeInformationW(
                                path, label_buf, 256, None, None, None, None, 0
                            )
                            label = label_buf.value or "Без имени"

                            # Тип диска
                            drive_type = ctypes.windll.kernel32.GetDriveTypeW(path)
                            removable = (drive_type == 2)  # DRIVE_REMOVABLE

                            drives.append({
                                "path": path,
                                "label": label,
                                "free": f"{free_gb} GB",
                                "removable": removable
                            })
                        except Exception:
                            pass
                    bitmask >>= 1
            except Exception:
                # Fallback: простой перебор
                for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
                    path = f"{letter}:\\"
                    if Path(path).exists():
                        drives.append({"path": path, "label": "Диск", "free": "N/A", "removable": False})
        else:
            # Linux/macOS
            common_paths = ["/", "/home", "/media", "/mnt", "/Volumes"]
            for mp in common_paths:
                if Path(mp).exists():
                    drives.append({"path": mp, "label": mp, "free": "N/A", "removable": False})

        return drives

    @on(Button.Pressed)
    def _on_button(self, event: Button.Pressed) -> None:
        btn_id = event.button.id or ""
        if btn_id == "dl-cancel":
            self.dismiss(None)
        elif btn_id.startswith("drive-"):
            drive_path = btn_id.replace("drive-", "")
            self.dismiss(drive_path)