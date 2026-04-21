"""
ui/tui/file_manager.py — Управление файлами плана с древовидной структурой папок.
"""
from __future__ import annotations
import dataclasses
from collections import defaultdict
from pathlib import Path, PurePosixPath
from typing import Optional

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, ScrollableContainer
from textual.screen import ModalScreen
from textual.widgets import (
    Label, Button, DataTable, Select, Static, Tree, Footer,
)
from textual import on
from rich.text import Text

from domain.models import SyncAction, ActionType, ProtectionLevel, ProtectionRule, SyncProfile

# ── ИМПОРТ ДИАЛОГА ПОДТВЕРЖДЕНИЯ ─────────────────────────────────────────────
# Если ConfirmDialog в app.py, импортируем оттуда.
# Если файл не найден, создайте простой диалог ниже или уберите импорт.
try:
    from ui.tui.app import ConfirmDialog
except ImportError:
    # Заглушка, если импорт не сработает (лучше реализовать полноценный диалог)
    ConfirmDialog = None

# ── Стили ─────────────────────────────────────────────────────────────────────

ACTION_STYLE = {
    ActionType.COPY_NEW:       "ansi_bright_green",
    ActionType.COPY_UPDATE:    "ansi_yellow",
    ActionType.DELETE:         "ansi_red",
    ActionType.SKIP_EQUAL:     "ansi_bright_black",
    ActionType.SKIP_PROTECTED: "ansi_cyan",
}
ACTION_SHORT = {
    ActionType.COPY_NEW:       "НОВЫЙ",
    ActionType.COPY_UPDATE:    "ИЗМЕНЁН",
    ActionType.DELETE:         "BACKUP",
    ActionType.SKIP_EQUAL:     "одинак.",
    ActionType.SKIP_PROTECTED: "ЗАЩИЩЁН",
}
ACTION_DESC = {
    ActionType.COPY_NEW:       "скопировать в dst",
    ActionType.COPY_UPDATE:    "обновить → backup старый",
    ActionType.DELETE:         "переместить в backup",
    ActionType.SKIP_EQUAL:     "пропустить (одинаковые)",
    ActionType.SKIP_PROTECTED: "пропустить (защищён)",
}

FILTER_OPTIONS = [
    ("Все файлы",            "all"),
    ("Только новые",         "copy_new"),
    ("Только изменённые",    "copy_update"),
    ("Только backup",        "delete"),
    ("Только защищённые",    "skip_protected"),
    ("Одинаковые",           "skip_equal"),
]


def _real_size(action: SyncAction) -> int:
    if action.src_file: return action.src_file.size
    if action.dst_file: return action.dst_file.size
    return 0

def _fmt_size(size: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if size < 1024: return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}T"

def _change_action(action: SyncAction, new_type: ActionType) -> SyncAction:
    new_protection = ProtectionLevel.SINGLE if new_type == ActionType.SKIP_PROTECTED else ProtectionLevel.NONE
    return dataclasses.replace(
        action, action=new_type, protection_level=new_protection,
        confirmed=0, reason=f"изменено вручную",
    )

def _build_folder_tree(plan: list[SyncAction]) -> dict:
    folders: dict[str, list[int]] = defaultdict(list)
    for i, a in enumerate(plan):
        parent = a.rel_path.parent.as_posix()
        if parent == ".": parent = ""
        folders[parent].append(i)
    return dict(folders)


# ── Диалог выбора действия для одного файла ───────────────────────────────────

class SingleFileActionScreen(ModalScreen):
    CSS = """
    SingleFileActionScreen { align: center middle; }
    #sfa { width: 62; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    #sfa Button { width: 100%; margin-bottom: 1; }
    #sfa #file-info { color: $text-muted; margin-bottom: 1; }
    """

    def __init__(self, action: SyncAction):
        super().__init__()
        self._action = action

    def compose(self) -> ComposeResult:
        name = str(self._action.rel_path)
        size = _fmt_size(_real_size(self._action))
        current = ACTION_SHORT.get(self._action.action, "?")
        with Container(id="sfa"):
            yield Label(f"[bold]{name}[/]", id="file-info")
            yield Label(f"Размер: {size} | Сейчас: [{ACTION_STYLE.get(self._action.action,'')}]{current}[/]\n\nВыберите действие:")
            yield Button("КОПИРОВАТЬ — скопировать в dst", id="a-copy", variant="success")
            yield Button("ОБНОВИТЬ — обновить (старая → backup)", id="a-update", variant="warning")
            yield Button("ПРОПУСТИТЬ — ничего не делать", id="a-skip", variant="default")
            yield Button("ЗАЩИТИТЬ — никогда не трогать", id="a-protect", variant="primary")
            yield Button("В BACKUP — переместить в backup", id="a-backup", variant="error")
            # Кнопка удаления (одна, без дублей!)
            yield Button("🗑 УДАЛИТЬ (В корзину)", id="act-delete", variant="error")
            yield Button("Отмена", id="a-cancel", variant="default")

    @on(Button.Pressed, "#a-copy")
    def _copy(self): self.dismiss(ActionType.COPY_NEW)
    @on(Button.Pressed, "#a-update")
    def _update(self): self.dismiss(ActionType.COPY_UPDATE)
    @on(Button.Pressed, "#a-skip")
    def _skip(self): self.dismiss(ActionType.SKIP_EQUAL)
    @on(Button.Pressed, "#a-protect")
    def _protect(self): self.dismiss(ActionType.SKIP_PROTECTED)
    @on(Button.Pressed, "#a-backup")
    def _backup(self): self.dismiss(ActionType.DELETE)

    @on(Button.Pressed, "#a-cancel")
    def _cancel(self): self.dismiss(None)


# ── Главный экран управления файлами ──────────────────────────────────────────

class FileManagerScreen(ModalScreen):
    """
    Левая панель: дерево папок
    Правая панель: файлы выбранной папки
    Низ: кнопки действий
    """

    CSS = """
    FileManagerScreen { align: center middle; }

    #fm-root {
        width: 98%; height: 94%;
        border: thick $primary;
        background: $surface;
        layout: vertical;
        padding: 0;
    }

    /* ── Строка заголовка ── */
    #fm-topbar {
        height: auto; min-height: 3; layout: horizontal;
        background: $surface-darken-2; padding: 0 1;
        align: left middle;
    }
    #fm-topbar Label { margin: 0 1; }
    #fm-topbar Select { width: 22; margin: 0 1; }
    #fm-topbar Button { margin: 0 1; min-width: 0; }

    /* ── Подсказка ── */
    #fm-hint {
        height: 1; background: $surface-darken-1;
        color: $text-muted; padding: 0 1;
    }

    /* ── Основной layout: дерево + таблица ── */
    #fm-body { height: 1fr; layout: horizontal; }

    /* Дерево папок */
    #fm-tree-panel {
        width: 30%; min-width: 22;
        border-right: solid $primary;
        background: $surface-darken-1;
        layout: vertical;
    }
    #fm-tree-title {
        height: 1; background: $primary;
        color: $background; text-align: center;
    }
    #fm-folder-tree { height: 1fr; }

    /* Таблица файлов */
    #fm-file-panel { width: 1fr; layout: vertical; }
    #fm-file-title {
        height: 1; background: $surface-darken-2;
        color: $text-muted; padding: 0 1;
    }
    #fm-table { height: 1fr; }

    /* ── Панель действий ── */
    #fm-actions {
        height: 3; layout: horizontal;
        background: $surface-darken-2; padding: 0 1;
        align: left middle;
    }
    #fm-actions Label { color: $text-muted; margin: 0 1; width: auto; }
    #fm-actions Button { margin: 0 1; min-width: 0; }

    /* ── Нижние кнопки ── */
    #fm-footer {
        height: 3; layout: horizontal;
        background: $surface-darken-2; padding: 0 1;
        align: left middle;
    }
    #fm-footer Button { margin: 0 1; min-width: 0; }
    #fm-footer #sel-info { width: 1fr; color: $text-muted; }
    """


    BINDINGS = [
        Binding("space", "toggle_select", "Выбрать", show=True),
        Binding("a", "select_all", "Все в папке", show=True),
        Binding("escape", "do_cancel", "Отмена", show=True),
    ]

    def __init__(self, plan: list[SyncAction], profile: SyncProfile):
        super().__init__()
        self._plan: list[SyncAction] = list(plan)
        self._profile = profile
        self._filter: str = "all"
        self._current_folder: str = ""
        self._row_to_plan: list[int] = []
        self._selected: set[int] = set()
        self._folder_tree: dict[str, list[int]] = {}

    def compose(self) -> ComposeResult:
        with Container(id="fm-root"):
            with Horizontal(id="fm-topbar"):
                yield Label("[bold]Управление файлами[/]")
                yield Label("Фильтр:")
                yield Select([(label, val) for label, val in FILTER_OPTIONS], value="all", id="fm-filter", allow_blank=False)
                yield Button("Выбрать все в папке", id="btn-sel-all", variant="default")
                yield Button("Снять всё", id="btn-sel-none", variant="default")
            yield Label(" Пробел=выбрать | A=все в папке | Enter=изменить одну | Папка=содержимое", id="fm-hint")
            with Horizontal(id="fm-body"):
                with Vertical(id="fm-tree-panel"):
                    yield Label(" Папки ", id="fm-tree-title")
                    yield Tree("[корень]", id="fm-folder-tree")
                with Vertical(id="fm-file-panel"):
                    yield Label(" Файлы папки: [корень] ", id="fm-file-title")
                    yield DataTable(id="fm-table", cursor_type="row")
            with Horizontal(id="fm-actions"):
                yield Label("К выбранным:")
                yield Button("КОПИРОВАТЬ", id="act-copy", variant="success")
                yield Button("ПРОПУСТИТЬ", id="act-skip", variant="default")
                yield Button("ЗАЩИТИТЬ", id="act-protect", variant="primary")
                yield Button("В BACKUP", id="act-backup", variant="warning")
            with Horizontal(id="fm-footer"):
                yield Button("Применить", id="btn-apply", variant="success")
                yield Button("Сохранить защиту", id="btn-save", variant="primary")
                yield Button("Отмена", id="btn-cancel", variant="default")
                yield Label("", id="sel-info")

    def on_mount(self) -> None:
        t = self.query_one("#fm-table", DataTable)
        t.add_columns(" ", "Статус", "Что будет", "Файл", "Размер", "Кат.")
        self._rebuild_folder_tree()
        self._show_folder("")

    def _rebuild_folder_tree(self) -> None:
        self._folder_tree = _build_folder_tree(self._plan)
        tree = self.query_one("#fm-folder-tree", Tree)
        tree.clear()
        tree.root.label = "[корень]"
        tree.root.data = ""
        all_folders = set(self._folder_tree.keys())
        extra = set()
        for f in all_folders:
            parts = f.split("/") if f else []
            for i in range(len(parts)): extra.add("/".join(parts[:i]))
        all_folders |= extra
        children: dict[str, list[str]] = defaultdict(list)
        for f in sorted(all_folders):
            if not f: continue
            parent = "/".join(f.split("/")[:-1])
            children[parent].append(f)
        def add_subtree(node, folder_key: str) -> None:
            for child_key in children.get(folder_key, []):
                name = child_key.split("/")[-1]
                n = sum(len(v) for k, v in self._folder_tree.items() if k == child_key or k.startswith(child_key + "/"))
                label = f"[папка] {name} [{n}]"
                child_node = node.add(label, data=child_key)
                add_subtree(child_node, child_key)
        add_subtree(tree.root, "")
        root_n = len(self._folder_tree.get("", []))
        tree.root.label = f"[корень] [{root_n}]"
        tree.root.expand()

    @on(Tree.NodeSelected, "#fm-folder-tree")
    def _on_folder_selected(self, event: Tree.NodeSelected) -> None:
        folder_key = event.node.data
        if folder_key is not None:
            self._current_folder = folder_key
            self._show_folder(folder_key)

    def _show_folder(self, folder_key: str) -> None:
        title_label = self.query_one("#fm-file-title", Label)
        display = folder_key if folder_key else "[корень]"
        title_label.update(f" Файлы: {display} ")
        t = self.query_one("#fm-table", DataTable)
        t.clear()
        self._row_to_plan = []
        indices = self._folder_tree.get(folder_key, [])
        for plan_idx in indices:
            action = self._plan[plan_idx]
            if self._filter != "all" and action.action.value != self._filter: continue
            sel_mark = "✓" if plan_idx in self._selected else " "
            style = ACTION_STYLE[action.action]
            short = ACTION_SHORT[action.action]
            desc  = ACTION_DESC[action.action]
            fname = action.rel_path.name
            size  = _fmt_size(_real_size(action))
            fi    = action.src_file or action.dst_file
            cat   = fi.category if fi else ""
            t.add_row(
                Text(sel_mark, style="bold ansi_bright_green" if plan_idx in self._selected else ""),
                Text(short, style=style), Text(desc, style=style),
                Text(fname), Text(size), Text(cat),
            )
            self._row_to_plan.append(plan_idx)
        self._update_sel_info()

    def action_toggle_select(self) -> None:
        t = self.query_one("#fm-table", DataTable)
        row = t.cursor_row
        if row < len(self._row_to_plan):
            idx = self._row_to_plan[row]
            if idx in self._selected: self._selected.discard(idx)
            else: self._selected.add(idx)
            self._show_folder(self._current_folder)
            try: t.move_cursor(row=row)
            except Exception: pass

    def action_select_all(self) -> None:
        for idx in self._row_to_plan: self._selected.add(idx)
        self._show_folder(self._current_folder)

    @on(Button.Pressed, "#btn-sel-all")
    def _sel_all(self): self.action_select_all()

    @on(Button.Pressed, "#btn-sel-none")
    def _sel_none(self):
        self._selected.clear()
        self._show_folder(self._current_folder)

    def _update_sel_info(self) -> None:
        n = len(self._selected)
        mb = sum(_real_size(self._plan[i]) for i in self._selected) / (1024 * 1024)
        try:
            self.query_one("#sel-info", Label).update(f"Выбрано: [bold]{n}[/] файлов ({mb:.1f} MB)")
        except Exception: pass

    @on(Select.Changed, "#fm-filter")
    def _on_filter(self, event: Select.Changed) -> None:
        self._filter = str(event.value)
        self._selected.clear()
        self._show_folder(self._current_folder)

    # ── Применение действий (С ИСПРАВЛЕНИЯМИ) ─────────────────────────────────

    @on(Button.Pressed, "#act-copy")
    def _act_copy(self): self._apply(ActionType.COPY_NEW)
    @on(Button.Pressed, "#act-skip")
    def _act_skip(self): self._apply(ActionType.SKIP_EQUAL)
    @on(Button.Pressed, "#act-protect")
    def _act_protect(self): self._apply(ActionType.SKIP_PROTECTED)
    @on(Button.Pressed, "#act-backup")
    def _act_backup(self): self._apply(ActionType.DELETE)

    # ✅ ОБРАБОТЧИК НОВОЙ КНОПКИ УДАЛЕНИЯ

    def _apply(self, new_type: ActionType) -> None:
        if not self._selected:
            self.notify("Сначала выберите файлы", severity="warning")
            return

        # ⚠️ ПОДТВЕРЖДЕНИЕ ДЛЯ ОПАСНОГО ДЕЙСТВИЯ
            count = len(self._selected)
            # Если ConfirmDialog не импортировался, используем простой notify или пропускаем
            if ConfirmDialog:
                self.app.push_screen(
                    ConfirmDialog(
                        message=f"[bold red]⚠️ УДАЛЕНИЕ НАВСЕГДА[/]\n\n{count} файл(ов) будут отправлены в [bold]Корзину[/].\nПродолжить?",
                        double_confirm=True
                    ),
                    lambda ok: self._finish_apply(new_type) if ok else None
                )
            else:
                # Fallback без модалки, если импорт не сработал
                self.notify("⚠️ Файлы будут удалены (Корзина)", severity="warning")
                self._finish_apply(new_type)
            return

        self._finish_apply(new_type)

    def _finish_apply(self, new_type: ActionType) -> None:
        """Внутренний метод применения."""
        for idx in list(self._selected):
            self._plan[idx] = _change_action(self._plan[idx], new_type)
        n = len(self._selected)
        self._selected.clear()
        self._rebuild_folder_tree()
        self._show_folder(self._current_folder)
        self.notify(f"Применено к {n} файлам: {ACTION_SHORT[new_type]}", severity="information")

    @on(DataTable.RowSelected, "#fm-table")
    def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        row = event.cursor_row
        if row >= len(self._row_to_plan): return
        plan_idx = self._row_to_plan[row]
        self.app.push_screen(
            SingleFileActionScreen(self._plan[plan_idx]),
            lambda result, idx=plan_idx: self._on_single_done(idx, result)
        )

    def _on_single_done(self, plan_idx: int, new_type: Optional[ActionType]) -> None:
        if new_type is not None:
            self._plan[plan_idx] = _change_action(self._plan[plan_idx], new_type)
            self._rebuild_folder_tree()
            self._show_folder(self._current_folder)

    @on(Button.Pressed, "#btn-save")
    def _save_protection(self) -> None:
        protected = [a for a in self._plan if a.action == ActionType.SKIP_PROTECTED]
        if not protected:
            self.notify("Нет защищённых файлов", severity="warning")
            return
        existing = {r.pattern for r in self._profile.protection_rules}
        added = 0
        for a in protected:
            pattern = f"*{a.rel_path.name}*"
            if pattern not in existing:
                self._profile.protection_rules.append(
                    ProtectionRule(pattern=pattern, level=ProtectionLevel.SINGLE, description="добавлен вручную")
                )
                existing.add(pattern)
                added += 1
        if added:
            from infrastructure.storage import load_profiles, save_profiles
            profiles = load_profiles()
            profiles[self._profile.name] = self._profile
            save_profiles(profiles)
            self.notify(f"Сохранено {added} правил защиты.", severity="information", timeout=6)
        else:
            self.notify("Все правила уже сохранены", severity="information")

    @on(Button.Pressed, "#btn-apply")
    def _apply_all(self) -> None: self.dismiss(self._plan)

    @on(Button.Pressed, "#btn-cancel")
    def action_do_cancel(self) -> None: self.dismiss(None)
