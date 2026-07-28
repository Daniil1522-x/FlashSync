"""
ui/tui/file_manager.py — Экран ручного управления планом синхронизации.

Структура: дерево папок слева (с количеством файлов), список файлов
выбранной папки справа. Множественный выбор (Space / A = вся папка).
Кнопки применяют действие к выбранным файлам.

Кнопка "🗑 УДАЛИТЬ" (DELETE_PERM) убрана из UI — слишком опасно для
синхронизатора флешка↔ПК (см. обсуждение). ActionType.DELETE_PERM и
обработка в sync_engine.py остаются в коде на случай будущего использования,
но пользователю явно эта возможность не предлагается.
"""
from __future__ import annotations
from collections import defaultdict
from pathlib import Path
from typing import Optional
import dataclasses

from textual.app import ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Label, Button, DataTable, Tree
from textual import on
from rich.text import Text

from domain.models import SyncAction, ActionType, ProtectionLevel, SyncProfile, ProtectionRule
from infrastructure.storage import load_profiles, save_profiles

ACTION_LABEL = {
    ActionType.COPY_NEW:       "НОВЫЙ",
    ActionType.COPY_UPDATE:    "ИЗМЕНЁН",
    ActionType.DELETE:         "В BACKUP",
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


def _fmt_size(size: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


class FileManagerScreen(ModalScreen):
    """
    Возвращает изменённый план (list[SyncAction]) через dismiss(),
    либо None если пользователь отменил.
    """

    CSS = """
    FileManagerScreen { align: center middle; }
    #fm-root {
        width: 94%; height: 92%;
        border: thick $primary; background: $surface;
        padding: 0; layout: vertical;
    }
    #fm-header { height: 2; background: $primary; color: $background; padding: 0 1; align: left middle; }
    #fm-body { height: 1fr; layout: horizontal; }
    #fm-tree-panel { width: 30%; border-right: solid $accent; }
    #fm-tree-panel Tree { height: 1fr; }
    #fm-table-panel { width: 1fr; }
    #fm-table-panel DataTable { height: 1fr; }
    #fm-selected-label { height: 1; padding: 0 1; color: $text-muted; }
    #fm-actions {
        height: 3; layout: horizontal;
        background: $surface-darken-2; padding: 0 1; align: left middle;
    }
    #fm-actions Button { margin-right: 1; }
    #fm-footer {
        height: 3; layout: horizontal;
        background: $surface-darken-1; padding: 0 1; align: left middle;
    }
    #fm-footer Button { margin-right: 1; }
    #fm-footer Label { width: 1fr; color: $text-muted; }
    """

    BINDINGS = [
        ("space", "toggle_select", "Выбрать"),
        ("a", "select_all_folder", "Вся папка"),
    ]

    def __init__(self, plan: list[SyncAction], profile: SyncProfile):
        super().__init__()
        self._original_plan = plan
        self._plan: list[SyncAction] = list(plan)
        self._profile = profile
        self._selected_paths: set[str] = set()
        self._current_folder: Optional[str] = None
        self._folder_actions: dict[str, list[SyncAction]] = defaultdict(list)
        self._build_folder_index()

    # ── Индексация по папкам ────────────────────────────────────────────────

    def _build_folder_index(self) -> None:
        self._folder_actions.clear()
        for a in self._plan:
            if a.action == ActionType.SKIP_EQUAL:
                continue
            rel = a.rel_path
            folder = str(rel.parent) if rel.parent != Path(".") else "(корень)"
            self._folder_actions[folder].append(a)

    def compose(self) -> ComposeResult:
        with Container(id="fm-root"):
            yield Label(" Управление файлами синхронизации ", id="fm-header")
            with Horizontal(id="fm-body"):
                with Vertical(id="fm-tree-panel"):
                    yield Tree("Папки", id="fm-tree")
                with Vertical(id="fm-table-panel"):
                    yield DataTable(id="fm-table", cursor_type="row")
                    yield Label("Выбрано: 0", id="fm-selected-label")
            with Horizontal(id="fm-actions"):
                yield Button("Копировать",      id="act-copy",     variant="success")
                yield Button("Пропустить",       id="act-skip",     variant="default")
                yield Button("Защитить",         id="act-protect",  variant="primary")
                yield Button("В BACKUP",         id="act-backup",   variant="warning")
                yield Button("Сохранить защиту в профиль", id="act-save-rules", variant="default")
            with Horizontal(id="fm-footer"):
                yield Button("Применить", id="btn-apply",  variant="success")
                yield Button("Отмена",    id="btn-cancel", variant="default")
                yield Label("", id="fm-footer-status")

    def on_mount(self) -> None:
        t = self.query_one("#fm-table", DataTable)
        t.add_columns(" ", "Действие", "Файл", "Размер")
        self._fill_tree()

    # ── Дерево папок ─────────────────────────────────────────────────────────

    def _fill_tree(self) -> None:
        tree = self.query_one("#fm-tree", Tree)
        tree.clear()
        tree.root.expand()
        for folder in sorted(self._folder_actions.keys()):
            n = len(self._folder_actions[folder])
            tree.root.add_leaf(f"{folder}  [{n}]", data=folder)

    @on(Tree.NodeSelected, "#fm-tree")
    def _on_folder_selected(self, event: Tree.NodeSelected) -> None:
        folder = event.node.data
        if folder is None:
            return
        self._current_folder = folder
        self._fill_table(folder)

    def _fill_table(self, folder: str) -> None:
        t = self.query_one("#fm-table", DataTable)
        t.clear()
        actions = self._folder_actions.get(folder, [])
        for a in actions:
            rel = str(a.rel_path)
            mark = "✓" if rel in self._selected_paths else " "
            style = ACTION_STYLE.get(a.action, "")
            t.add_row(
                mark,
                Text(ACTION_LABEL.get(a.action, a.action.value), style=style),
                a.rel_path.name,
                _fmt_size(a.size_bytes),
                key=rel,
            )
        self._update_selected_label()

    def _update_selected_label(self) -> None:
        self.query_one("#fm-selected-label", Label).update(f"Выбрано: {len(self._selected_paths)}")

    # ── Выбор файлов ─────────────────────────────────────────────────────────

    def action_toggle_select(self) -> None:
        t = self.query_one("#fm-table", DataTable)
        if t.cursor_row < 0 or not self._current_folder:
            return
        actions = self._folder_actions.get(self._current_folder, [])
        if t.cursor_row >= len(actions):
            return
        rel = str(actions[t.cursor_row].rel_path)
        if rel in self._selected_paths:
            self._selected_paths.discard(rel)
        else:
            self._selected_paths.add(rel)
        self._fill_table(self._current_folder)

    def action_select_all_folder(self) -> None:
        if not self._current_folder:
            return
        for a in self._folder_actions.get(self._current_folder, []):
            self._selected_paths.add(str(a.rel_path))
        self._fill_table(self._current_folder)

    # ── Применение действий к выбранным ─────────────────────────────────────

    def _apply_action_to_selected(self, new_action: ActionType,
                                  protection: ProtectionLevel = ProtectionLevel.NONE,
                                  reason: str = "вручную") -> None:
        """
        Применяет действие к выбранным файлам.

        Раньше делало два полных прохода по всему плану: один здесь чтобы
        построить new_plan, и ещё один внутри _build_folder_index(). На
        планах в десятки тысяч файлов это давало заметную задержку UI на
        каждый клик по кнопке действия. Теперь — один проход: rel_path
        (а значит и папка) не меняется при замене action/protection_level,
        так что можно обновить SyncAction прямо по месту в индексе без
        пересборки всей структуры с нуля.
        """
        if not self._selected_paths:
            self.notify("Сначала выберите файлы (Space)", severity="warning")
            return
        count = 0
        new_plan = []
        for a in self._plan:
            rel = str(a.rel_path)
            if rel in self._selected_paths:
                new_a = dataclasses.replace(
                    a, action=new_action, protection_level=protection, reason=reason, confirmed=0
                )
                new_plan.append(new_a)
                count += 1

                # Обновляем индекс по месту — папка та же, меняем только сам элемент
                folder = str(a.rel_path.parent) if a.rel_path.parent != Path(".") else "(корень)"
                bucket = self._folder_actions.get(folder)
                if bucket is not None:
                    # Поиск по identity (is), а не по значению (==) — нам нужен
                    # именно ЭТОТ объект, а не любой "равный по содержимому";
                    # для dataclass с вложенным FileInfo это и быстрее, и точнее
                    for idx, existing in enumerate(bucket):
                        if existing is a:
                            bucket[idx] = new_a
                            break
            else:
                new_plan.append(a)
        self._plan = new_plan
        if self._current_folder:
            self._fill_table(self._current_folder)
        self._fill_tree()
        self.query_one("#fm-footer-status", Label).update(f"Изменено {count} файлов")

    @on(Button.Pressed, "#act-copy")
    def _act_copy(self) -> None:
        self._apply_action_to_selected(ActionType.COPY_NEW, reason="вручную: копировать")

    @on(Button.Pressed, "#act-skip")
    def _act_skip(self) -> None:
        self._apply_action_to_selected(ActionType.SKIP_EQUAL, reason="вручную: пропустить")

    @on(Button.Pressed, "#act-protect")
    def _act_protect(self) -> None:
        self._apply_action_to_selected(
            ActionType.SKIP_PROTECTED, protection=ProtectionLevel.SINGLE,
            reason="вручную: защищён"
        )

    @on(Button.Pressed, "#act-backup")
    def _act_backup(self) -> None:
        self._apply_action_to_selected(ActionType.DELETE, reason="вручную: в backup")

    @on(Button.Pressed, "#act-save-rules")
    def _save_rules(self) -> None:
        """
        Сохраняет выбранные файлы как правила защиты в профиль.

        Паттерн строится по ПОЛНОМУ относительному пути файла, а не только
        по имени. Раньше использовался только rel.name ("*report.pdf*"),
        из-за чего правило защищало report.pdf в ЛЮБОЙ папке всего дерева
        синхронизации, а не только выбранный конкретный файл — это могло
        неожиданно заблокировать синхронизацию совершенно других файлов
        с тем же именем.
        """
        if not self._selected_paths:
            self.notify("Сначала выберите файлы", severity="warning")
            return
        added = 0
        existing_patterns = {r.pattern for r in self._profile.protection_rules}
        for rel in self._selected_paths:
            # Полный путь от корня синхронизации, нормализованный к posix-виду —
            # защищает именно этот файл в этом месте, а не файл с таким же именем где угодно
            pattern = Path(rel).as_posix()
            if pattern not in existing_patterns:
                self._profile.protection_rules.append(
                    ProtectionRule(pattern=pattern, level=ProtectionLevel.SINGLE,
                                   description="добавлено вручную из Управления файлами")
                )
                existing_patterns.add(pattern)
                added += 1
        try:
            profiles = load_profiles()
            profiles[self._profile.name] = self._profile
            save_profiles(profiles)
            self.notify(f"Добавлено {added} правил защиты в профиль '{self._profile.name}'",
                       severity="information")
        except Exception as e:
            self.notify(f"Ошибка сохранения: {e}", severity="error")

    # ── Закрытие ─────────────────────────────────────────────────────────────

    @on(Button.Pressed, "#btn-apply")
    def _apply(self) -> None:
        self.dismiss(self._plan)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self) -> None:
        self.dismiss(None)
