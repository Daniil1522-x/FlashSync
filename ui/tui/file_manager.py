"""
ui/tui/file_manager.py — Экран ручного управления файлами плана.

Пользователь видит каждый файл из плана и может:
  - изменить действие (копировать / пропустить / защитить)
  - отметить несколько файлов и применить действие ко всем
  - отфильтровать по типу действия
  - сохранить правила защиты в профиль навсегда
"""
from __future__ import annotations
from pathlib import Path
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen
from textual.widgets import (
    Label, Button, DataTable, Select, Static,
    Checkbox, Input, Footer,
)
from textual import on
from rich.text import Text

from domain.models import SyncAction, ActionType, ProtectionLevel, ProtectionRule, SyncProfile


# Действия которые пользователь может назначить вручную
MANUAL_ACTIONS = {
    "copy":    (ActionType.COPY_NEW,       "КОПИРОВАТЬ",   "ansi_bright_green"),
    "update":  (ActionType.COPY_UPDATE,    "ОБНОВИТЬ",     "ansi_yellow"),
    "skip":    (ActionType.SKIP_EQUAL,     "ПРОПУСТИТЬ",   "ansi_bright_black"),
    "protect": (ActionType.SKIP_PROTECTED, "ЗАЩИТИТЬ",     "ansi_cyan"),
    "backup":  (ActionType.DELETE,         "В BACKUP",     "ansi_red"),
}

# Метки фильтров
FILTER_OPTIONS = [
    ("all",       "Все файлы"),
    ("copy_new",  "Только новые"),
    ("copy_update","Только изменённые"),
    ("delete",    "Только в backup"),
    ("skip_protected", "Только защищённые"),
]


class FileManagerScreen(ModalScreen):
    """
    Экран ручного управления файлами плана синхронизации.
    Возвращает изменённый plan: list[SyncAction].
    """

    CSS = """
    FileManagerScreen { align: center middle; }

    #fm-root {
        width: 95%;
        height: 90%;
        border: thick $primary;
        background: $surface;
        padding: 0;
    }

    #fm-header {
        height: 3;
        background: $surface-darken-2;
        padding: 0 1;
        align: left middle;
        layout: horizontal;
    }
    #fm-header Label { margin: 0 1; }
    #fm-header Button { margin: 0 1; min-width: 14; }
    #fm-header Select { width: 24; margin: 0 1; }

    #fm-hint {
        height: 2;
        background: $surface-darken-1;
        color: $text-muted;
        padding: 0 1;
    }

    #fm-table { height: 1fr; }

    #fm-action-bar {
        height: 4;
        background: $surface-darken-2;
        padding: 1;
        layout: horizontal;
        align: left middle;
    }
    #fm-action-bar Button { margin: 0 1; min-width: 16; }
    #fm-action-bar Label { margin: 0 2; color: $text-muted; }

    #fm-footer {
        height: 3;
        background: $surface-darken-2;
        padding: 0 1;
        layout: horizontal;
        align: left middle;
    }
    #fm-footer Button { margin: 0 1; }
    #fm-footer Label { width: 1fr; color: $text-muted; }
    """

    BINDINGS = [
        Binding("space", "toggle_select", "Выбрать/снять", show=True),
        Binding("a",     "select_all",    "Выбрать все",   show=True),
        Binding("escape","cancel",        "Отмена",        show=True),
        Binding("enter", "apply_changes", "Применить",     show=True),
    ]

    def __init__(self, plan: list[SyncAction], profile: SyncProfile):
        super().__init__()
        # Работаем с копией плана чтобы не менять оригинал до подтверждения
        self._plan: list[SyncAction] = list(plan)
        self._profile = profile
        # Индекс строки таблицы → индекс в _plan
        self._row_to_plan: list[int] = []
        # Выбранные строки (для batch-операций)
        self._selected: set[int] = set()  # индексы в _plan
        # Текущий фильтр
        self._filter = "all"

    def compose(self) -> ComposeResult:
        with Container(id="fm-root"):
            # Заголовок с фильтром
            with Horizontal(id="fm-header"):
                yield Label("[bold]Управление файлами[/]")
                yield Label("Фильтр:")
                yield Select(
                    [(label, key) for key, label in FILTER_OPTIONS],
                    value="all",
                    id="filter-select",
                    allow_blank=False,
                )
                yield Button("Выбрать все", id="btn-sel-all", variant="default")
                yield Button("Снять всё",   id="btn-sel-none", variant="default")

            # Подсказка
            yield Label(
                "  Пробел = выбрать строку  |  Затем нажмите действие внизу  |  "
                "Двойной клик = изменить одну строку",
                id="fm-hint"
            )

            # Таблица файлов
            yield DataTable(id="fm-table", cursor_type="row")

            # Панель действий (применяется к выбранным)
            with Horizontal(id="fm-action-bar"):
                yield Label("Применить к выбранным:")
                yield Button("КОПИРОВАТЬ",   id="act-copy",    variant="success")
                yield Button("ПРОПУСТИТЬ",   id="act-skip",    variant="default")
                yield Button("ЗАЩИТИТЬ",     id="act-protect", variant="primary")
                yield Button("В BACKUP",     id="act-backup",  variant="warning")
                yield Label("", id="sel-count")

            # Нижние кнопки
            with Horizontal(id="fm-footer"):
                yield Button("Применить изменения", id="btn-apply", variant="success")
                yield Button("Сохранить защиту в профиль", id="btn-save-rules", variant="primary")
                yield Button("Отмена", id="btn-cancel", variant="default")
                yield Label("", id="footer-hint")

    def on_mount(self) -> None:
        t = self.query_one("#fm-table", DataTable)
        t.add_columns(" ", "Статус", "Действие", "Файл", "Размер", "Категория")
        self._rebuild_table()

    def _rebuild_table(self) -> None:
        t = self.query_one("#fm-table", DataTable)
        t.clear()
        self._row_to_plan = []

        for i, action in enumerate(self._plan):
            # Применяем фильтр
            if self._filter != "all" and action.action.value != self._filter:
                continue

            selected = "✓" if i in self._selected else " "
            style = self._action_style(action.action)
            label = self._action_label(action.action)

            rel = str(action.rel_path)
            if len(rel) > 55:
                rel = "..." + rel[-52:]

            # Категория файла
            cat = ""
            fi = action.src_file or action.dst_file
            if fi:
                cat = fi.category

            t.add_row(
                Text(selected, style="bold ansi_bright_green" if i in self._selected else ""),
                Text(label, style=style),
                Text(self._action_description(action.action), style=style),
                Text(rel),
                Text(_fmt_size(action.size_bytes)),
                Text(cat),
            )
            self._row_to_plan.append(i)

        self._update_sel_count()

    def _action_style(self, action: ActionType) -> str:
        styles = {
            ActionType.COPY_NEW:       "ansi_bright_green",
            ActionType.COPY_UPDATE:    "ansi_yellow",
            ActionType.DELETE:         "ansi_red",
            ActionType.SKIP_EQUAL:     "ansi_bright_black",
            ActionType.SKIP_PROTECTED: "ansi_cyan",
        }
        return styles.get(action, "")

    def _action_label(self, action: ActionType) -> str:
        labels = {
            ActionType.COPY_NEW:       "НОВЫЙ",
            ActionType.COPY_UPDATE:    "ИЗМЕНЁН",
            ActionType.DELETE:         "ТОЛЬКО В DST",
            ActionType.SKIP_EQUAL:     "одинаковый",
            ActionType.SKIP_PROTECTED: "ЗАЩИЩЁН",
        }
        return labels.get(action, action.value)

    def _action_description(self, action: ActionType) -> str:
        desc = {
            ActionType.COPY_NEW:       "скопировать в dst",
            ActionType.COPY_UPDATE:    "обновить (старый → backup)",
            ActionType.DELETE:         "переместить в backup",
            ActionType.SKIP_EQUAL:     "ничего не делать",
            ActionType.SKIP_PROTECTED: "пропустить (защищён)",
        }
        return desc.get(action, "")

    def _update_sel_count(self) -> None:
        n = len(self._selected)
        try:
            self.query_one("#sel-count", Label).update(
                f"[bold]{n}[/] выбрано" if n > 0 else "ничего не выбрано"
            )
        except Exception:
            pass

    # ── Выбор строк ───────────────────────────────────────────────────────────

    def action_toggle_select(self) -> None:
        t = self.query_one("#fm-table", DataTable)
        row_idx = t.cursor_row
        if row_idx < len(self._row_to_plan):
            plan_idx = self._row_to_plan[row_idx]
            if plan_idx in self._selected:
                self._selected.discard(plan_idx)
            else:
                self._selected.add(plan_idx)
            self._rebuild_table()
            # Возвращаем курсор на ту же строку
            try:
                t.move_cursor(row=row_idx)
            except Exception:
                pass

    def action_select_all(self) -> None:
        self._selected = set(self._row_to_plan)
        self._rebuild_table()

    @on(Button.Pressed, "#btn-sel-all")
    def _sel_all(self): self.action_select_all()

    @on(Button.Pressed, "#btn-sel-none")
    def _sel_none(self):
        self._selected.clear()
        self._rebuild_table()

    # ── Фильтр ────────────────────────────────────────────────────────────────

    @on(Select.Changed, "#filter-select")
    def _on_filter(self, event: Select.Changed) -> None:
        self._filter = str(event.value)
        self._selected.clear()
        self._rebuild_table()

    # ── Применение действий к выбранным ──────────────────────────────────────

    @on(Button.Pressed, "#act-copy")
    def _act_copy(self): self._apply_to_selected(ActionType.COPY_NEW)

    @on(Button.Pressed, "#act-skip")
    def _act_skip(self): self._apply_to_selected(ActionType.SKIP_EQUAL)

    @on(Button.Pressed, "#act-protect")
    def _act_protect(self): self._apply_to_selected(ActionType.SKIP_PROTECTED)

    @on(Button.Pressed, "#act-backup")
    def _act_backup(self): self._apply_to_selected(ActionType.DELETE)

    def _apply_to_selected(self, new_action: ActionType) -> None:
        if not self._selected:
            self.notify("Сначала выберите файлы (пробел или 'Выбрать все')", severity="warning")
            return
        for idx in self._selected:
            self._plan[idx] = _change_action(self._plan[idx], new_action)
        self._selected.clear()
        self._rebuild_table()
        self.notify(f"Применено к файлам: {self._action_label(new_action)}", severity="information")

    # ── Двойной клик — меняем одну строку ────────────────────────────────────

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        row_idx = event.cursor_row
        if row_idx >= len(self._row_to_plan):
            return
        plan_idx = self._row_to_plan[row_idx]
        self.push_screen(
            SingleFileActionScreen(self._plan[plan_idx]),
            lambda result: self._on_single_changed(plan_idx, result)
        )

    def _on_single_changed(self, plan_idx: int, new_action: Optional[ActionType]) -> None:
        if new_action is not None:
            self._plan[plan_idx] = _change_action(self._plan[plan_idx], new_action)
            self._rebuild_table()

    # ── Сохранить правила защиты в профиль ───────────────────────────────────

    @on(Button.Pressed, "#btn-save-rules")
    def _save_rules(self) -> None:
        """Находит все PROTECTED файлы и добавляет их имена как правила защиты в профиль."""
        protected = [
            a for a in self._plan
            if a.action == ActionType.SKIP_PROTECTED
        ]
        if not protected:
            self.notify("Нет защищённых файлов для сохранения", severity="warning")
            return

        added = 0
        existing = {r.pattern for r in self._profile.protection_rules}
        for a in protected:
            # Используем имя файла как паттерн
            name = a.rel_path.name
            pattern = f"*{name}*"
            if pattern not in existing:
                self._profile.protection_rules.append(
                    ProtectionRule(
                        pattern=pattern,
                        level=ProtectionLevel.SINGLE,
                        description=f"добавлен вручную"
                    )
                )
                existing.add(pattern)
                added += 1

        if added:
            from infrastructure.storage import load_profiles, save_profiles
            profiles = load_profiles()
            profiles[self._profile.name] = self._profile
            save_profiles(profiles)
            self.notify(
                f"Сохранено {added} правил защиты в профиль.\n"
                f"Эти файлы будут автоматически защищены при следующем сканировании.",
                severity="information",
                timeout=6,
            )
        else:
            self.notify("Все правила уже есть в профиле", severity="information")

    # ── Применить / отмена ────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-apply")
    def action_apply_changes(self) -> None:
        self.dismiss(self._plan)

    @on(Button.Pressed, "#btn-cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Экран изменения действия для одного файла ─────────────────────────────────

class SingleFileActionScreen(ModalScreen):
    """Маленький диалог выбора действия для одного файла."""

    CSS = """
    SingleFileActionScreen { align: center middle; }
    #sfa {
        width: 60; height: auto;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #sfa Button { width: 100%; margin-bottom: 1; }
    #sfa Label { margin-bottom: 1; }
    """

    def __init__(self, action: SyncAction):
        super().__init__()
        self._action = action

    def compose(self) -> ComposeResult:
        fi = self._action.src_file or self._action.dst_file
        name = str(self._action.rel_path)
        size = _fmt_size(self._action.size_bytes)
        with Container(id="sfa"):
            yield Label(f"[bold]{name}[/]\n[dim]{size}[/]\n\nВыберите действие:")
            yield Button("КОПИРОВАТЬ — скопировать в dst",           id="a-copy",    variant="success")
            yield Button("ОБНОВИТЬ — обновить (старая версия → backup)", id="a-update", variant="warning")
            yield Button("ПРОПУСТИТЬ — ничего не делать",            id="a-skip",    variant="default")
            yield Button("ЗАЩИТИТЬ — никогда не трогать",            id="a-protect", variant="primary")
            yield Button("В BACKUP — переместить в backup",          id="a-backup",  variant="error")
            yield Button("Отмена",                                    id="a-cancel",  variant="default")

    @on(Button.Pressed, "#a-copy")
    def _copy(self):    self.dismiss(ActionType.COPY_NEW)
    @on(Button.Pressed, "#a-update")
    def _update(self):  self.dismiss(ActionType.COPY_UPDATE)
    @on(Button.Pressed, "#a-skip")
    def _skip(self):    self.dismiss(ActionType.SKIP_EQUAL)
    @on(Button.Pressed, "#a-protect")
    def _protect(self): self.dismiss(ActionType.SKIP_PROTECTED)
    @on(Button.Pressed, "#a-backup")
    def _backup(self):  self.dismiss(ActionType.DELETE)
    @on(Button.Pressed, "#a-cancel")
    def _cancel(self):  self.dismiss(None)


# ── Вспомогательные функции ───────────────────────────────────────────────────

def _change_action(action: SyncAction, new_type: ActionType) -> SyncAction:
    """Создаёт копию SyncAction с изменённым типом действия."""
    import dataclasses
    new_protection = (
        ProtectionLevel.SINGLE
        if new_type == ActionType.SKIP_PROTECTED
        else ProtectionLevel.NONE
    )
    return dataclasses.replace(
        action,
        action=new_type,
        protection_level=new_protection,
        confirmed=0,
        reason=f"изменено вручную → {new_type.value}",
    )


def _fmt_size(size: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}T"
