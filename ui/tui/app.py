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
)
from textual import work, on
from textual.screen import ModalScreen
from rich.text import Text

from domain.models import SyncAction, ActionType, SyncProfile, SyncReport, ProtectionLevel
from ui.tui.file_manager import FileManagerScreen
from application.differ import DiffEngine, summarize_plan
from infrastructure.scanner import scan_directory, get_directory_stats
from infrastructure.storage import load_profiles, save_profiles, save_report, setup_logger, CONFIG_DIR

# ── Отображение действий ──────────────────────────────────────────────────────

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
# Что БУДЕТ сделано — для понятного объяснения в таблице
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
    #dlg {
        width: 70; min-height: 16;
        border: thick $primary;
        background: $surface;
        padding: 2 3;
    }
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
                    "[bold red]ВТОРОЕ ПОДТВЕРЖДЕНИЕ ТРЕБУЕТСЯ![/]\n\n" + self.message
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
        width: 90%;           /* было: 80 */
        min-height: 35;       /* было: 30 */
        max-height: 90%;      /* ← новое: не выше 90% экрана */
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
    #sc Label.hint { 
        color: $text-muted; 
        margin-bottom: 1;
        text-wrap: wrap;      /* ← новое: перенос длинных подсказок */
    }
    Input { 
        margin-bottom: 1;
        width: 100%;
    }
    #sc .row { 
        height: auto; 
        margin-bottom: 1; 
    }
    Checkbox {
        text-wrap: wrap;      /* ← новое: перенос текста чекбоксов */
        margin-bottom: 1;
    }
    """
    def __init__(self, profiles: dict, current: SyncProfile):
        super().__init__()
        self.profiles = profiles
        self.current = current

    def compose(self) -> ComposeResult:
        with Container(id="sc"):
            yield Label("[bold]Настройки профиля[/]")
            yield Label("")
            yield Label("Источник (откуда копировать):", classes="hint")
            yield Input(value=self.current.src, placeholder="E:\\FLASH", id="inp-src")
            yield Label("Приёмник (куда копировать):", classes="hint")
            yield Input(value=self.current.dst, placeholder="E:\\Backup\\Flash", id="inp-dst")
            yield Label("")
            yield Label("[bold]Параметры:[/]", classes="hint")
            yield Checkbox(
                "SHA-256: сравнивать по содержимому (медленнее, но точно). "
                "Выкл = только по размеру и дате",
                value=self.current.use_hash, id="chk-hash"
            )
            yield Checkbox(
                "Удалять из приёмника: файлы, которых нет в источнике → в backup",
                value=self.current.delete_mode, id="chk-del"
            )
            yield Checkbox(
                "Игнорировать скрытые: .DS_Store, Thumbs.db, файлы с точки",
                value=self.current.ignore_hidden, id="chk-hid"
            )
            yield Label("")
            with Horizontal(classes="row"):
                yield Button("Сохранить", variant="success", id="btn-save")
                yield Button("Отмена", variant="default", id="btn-cancel")

    @on(Button.Pressed, "#btn-save")
    def _save(self):
        from infrastructure.storage import _normalize_path
        self.current.src = _normalize_path(self.query_one("#inp-src", Input).value.strip())
        self.current.dst = _normalize_path(self.query_one("#inp-dst", Input).value.strip())
        self.current.use_hash     = self.query_one("#chk-hash", Checkbox).value
        self.current.delete_mode  = self.query_one("#chk-del",  Checkbox).value
        self.current.ignore_hidden= self.query_one("#chk-hid",  Checkbox).value
        save_profiles(self.profiles)
        self.dismiss(True)

    @on(Button.Pressed, "#btn-cancel")
    def _cancel(self):
        self.dismiss(False)


# ── Экран лога Dry Run ────────────────────────────────────────────────────────

class DryRunLogScreen(ModalScreen):
    CSS = """
    DryRunLogScreen { align: center middle; }
    #logbox {
        width: 90%; height: 80%;
        border: thick $primary;
        background: $surface;
        padding: 1 2;
    }
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


# ── Панель дерева файлов ──────────────────────────────────────────────────────

class FileTreePanel(Container):
    DEFAULT_CSS = """
    FileTreePanel {
        width: 25%;
        min-width: 20;
        border: solid $accent;
        background: $surface-darken-1;
    }
    FileTreePanel #ftitle {
        background: $accent; color: $background;
        text-align: center; height: 1;
    }
    FileTreePanel Tree { height: 1fr; }
    """
    def compose(self) -> ComposeResult:
        yield Label(" Содержимое источника ", id="ftitle")
        yield Tree("...", id="ftree")

    def load_path(self, path: Path) -> None:
        tree = self.query_one("#ftree", Tree)
        tree.clear()
        tree.root.label = f"{path.name}"
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
    InfoPanel {
        width: 25%;
        min-width: 22;
        border: solid $primary;
        background: $surface-darken-1;
        padding: 0;
    }
    InfoPanel #stats-label { padding: 0 1; height: auto; }
    InfoPanel #divider { color: $text-muted; padding: 0 1; height: 1; }
    InfoPanel RichLog { height: 1fr; padding: 0 1; }
    """
    def compose(self) -> ComposeResult:
        yield Label("", id="stats-label")
        yield Label("── Лог операций ──", id="divider")
        yield RichLog(id="log-view", markup=True, max_lines=200)

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
        lines.append(f"─" * 22)
        lines.append(f"[dim]Лог: {CONFIG_DIR}[/]")
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
    layout: horizontal;
    height: auto;        /* было: height: 3 */
    min-height: 3;
    background: $surface-darken-2;
    padding: 0 1;
    align: left middle;
    }

    #toolbar Button {
        margin: 0 1;
        min-width: 0; /* ✅ Разрешаем кнопкам сжиматься */
    }
    #direction-lbl {
    color: $text-muted;
    margin: 0 2;
    width: auto;         /* было: 1fr */
    text-wrap: wrap;     /* Разрешаем перенос */
    overflow-x: hidden;
    text-overflow: ellipsis; /* ✅ Ставит ... если путь слишком длинный */
    }

    #main-layout { layout: horizontal; height: 1fr; }

    #center {
        width: 1fr;
        border: solid $primary;
        background: $surface;
    }

    #legend {
        height: 2;
        background: $surface-darken-1;
        padding: 0 1;
        color: $text-muted;
    }

    #plan-table { height: 1fr; }

    #statusbar {
        height: 2;
        background: $surface-darken-2;
        padding: 0 1;
        align: left middle;
    }
    #statusbar #slabel { width: 1fr; }
    """

    BINDINGS = [
        Binding("ctrl+s", "scan",     "Сканировать",      show=True),
        Binding("ctrl+r", "run_sync", "Синхронизировать", show=True),
        Binding("ctrl+d", "dry_run",  "Пред_запуск(без.изм)", show=True),
        Binding("ctrl+f", "file_mgr", "Файлы",            show=True),
        Binding("ctrl+p", "settings", "Настройки",        show=True),
        Binding("ctrl+q", "quit",     "Выход",            show=True),
    ]

    def __init__(self):
        super().__init__()
        self.profiles = load_profiles()
        self.current_profile: SyncProfile = list(self.profiles.values())[0]
        self.logger = setup_logger()
        self._plan: list[SyncAction] = []
        self._last_dry_run_report: Optional[SyncReport] = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical():
            # ── Toolbar ──
            with Horizontal(id="toolbar"):
                yield Button("Сканировать [^S]",      id="btn-scan", variant="primary")
                yield Button("Синхронизировать [^R]", id="btn-sync", variant="success")
                yield Button("Dry Run [^D]",          id="btn-dry",  variant="warning")
                yield Button("Управление файлами [^F]", id="btn-fm", variant="default")
                yield Button("Обратить src<->dst",    id="btn-rev",  variant="default")
                yield Button("Настройки [^P]",        id="btn-cfg",  variant="default")
                yield Label("", id="direction-lbl")

            # ── Основной layout ──
            with Horizontal(id="main-layout"):
                yield FileTreePanel(id="src-tree")
                with Vertical(id="center"):
                    # Легенда прямо в интерфейсе
                    yield Label(
                        "[ansi_bright_green]● НОВЫЙ[/]  "
                        "[ansi_yellow]● ИЗМЕНЕН.Backup[/]  "
                        "[ansi_red]● ПРИЕМНИК→Backup[/]  "
                        "[ansi_cyan]● ЗАЩИЩЁН[/]  "
                        "[ansi_bright_black]● ОДИНАКОВЫЙ[/]",
                        id="legend"
                    )
                    yield DataTable(id="plan-table", cursor_type="row")
                yield InfoPanel(id="info")

        with Horizontal(id="statusbar"):
            yield Label("Готов. Нажмите Сканировать (Ctrl+S)", id="slabel")
        yield Footer()

    def on_mount(self) -> None:
        t = self.query_one("#plan-table", DataTable)
        t.add_columns("Действие", "Что будет сделано", "Файл", "Размер")
        self._update_direction_label()

    def _update_direction_label(self) -> None:
        p = self.current_profile
        src = Path(p.src).name or p.src
        dst = Path(p.dst).name or p.dst
        # Убрали пробелы и двоеточия — экономим 6-8 символов
        self.query_one("#direction-lbl", Label).update(
            f"[bold]ОТКУДА[/]{src}→[bold]КУДА[/]{dst}"
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
        self._update_direction_label()
        self.notify(
            f"Направление изменено!\n"
            f"Теперь копируем:\n{p.src}\n→ {p.dst}",
            severity="information",
            timeout=5,
        )

    @on(Button.Pressed, "#btn-fm")
    def action_file_mgr(self):
        if not self._plan:
            self.notify("Сначала нажмите Сканировать (Ctrl+S)", severity="warning")
            return
        self.push_screen(
            FileManagerScreen(self._plan, self.current_profile),
            self._on_file_mgr_closed
        )

    def _on_file_mgr_closed(self, new_plan) -> None:
        if new_plan is not None:
            self._plan = new_plan
            # ✅ ПРЯМОЙ вызов — мы уже в главном потоке
            self._fill_table(self._plan)
            self.notify("Изменения применены к плану", severity="information")

    @on(Button.Pressed, "#btn-cfg")
    def action_settings(self):
        self.push_screen(
            SettingsScreen(self.profiles, self.current_profile),
            self._on_settings_saved
        )

    def _on_settings_saved(self, saved: bool) -> None:
        if saved:
            self._update_direction_label()
            self.notify("Профиль сохранён", severity="information")

    # ── Сканирование ─────────────────────────────────────────────────────────

    @work(thread=True, exclusive=True)
    def _do_scan(self) -> None:
        self.call_from_thread(self._set_status, "Сканирование...")
        self.call_from_thread(self.query_one("#info", InfoPanel).clear_log)

        p = self.current_profile
        src = Path(p.src)
        dst = Path(p.dst)

        if not src.exists():
            self.call_from_thread(self.notify, f"Источник не найден:\n{src}", severity="error")
            self.call_from_thread(self._set_status, f"Ошибка: источник не найден — {src}")
            return

        try:
            dst.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.call_from_thread(self.notify, f"Ошибка создания dst:\n{e}", severity="error")
            self.call_from_thread(self._set_status, f"Ошибка dst: {e}")
            return

        self.call_from_thread(self._set_status, f"Сканирование источника: {src}")
        src_tree = scan_directory(
            src,
            include_patterns=p.include_patterns or None,
            exclude_patterns=p.exclude_patterns,
            use_hash=p.use_hash,
            ignore_hidden=p.ignore_hidden,
        )

        self.call_from_thread(self._set_status, f"Сканирование приёмника: {dst}")
        dst_tree = scan_directory(
            dst,
            include_patterns=p.include_patterns or None,
            exclude_patterns=p.exclude_patterns,
            use_hash=p.use_hash,
            ignore_hidden=p.ignore_hidden,
        )

        self.call_from_thread(self._set_status, "Построение плана изменений...")
        engine = DiffEngine(p)
        self._plan = engine.compute_plan(src_tree, dst_tree)

        summary = summarize_plan(self._plan)
        self.call_from_thread(self._fill_table, self._plan)

        try:
            stats = get_directory_stats(src)
            self.call_from_thread(
                self.query_one("#info", InfoPanel).update_stats, stats, p
            )
        except Exception:
            pass

        try:
            self.call_from_thread(
                self.query_one("#src-tree", FileTreePanel).load_path, src
            )
        except Exception:
            pass

        c = summary["counts"]
        mb = summary["bytes_to_copy"] / (1024 * 1024)
        method = "SHA-256" if p.use_hash else "размер+дата"
        status = (
            f"Сканирование завершено [{method}] | "
            f"Новых: {c.get('copy_new', 0)}  "
            f"Изменённых: {c.get('copy_update', 0)}  "
            f"Только в dst: {c.get('delete', 0)}  "
            f"Одинаковых: {c.get('skip_equal', 0)}  "
            f"Защищённых: {c.get('skip_protected', 0)}  "
            f"| К копированию: {mb:.1f} MB"
        )
        self.call_from_thread(self._set_status, status)

        # Лог
        info = self.query_one("#info", InfoPanel)
        self.call_from_thread(info.log, f"Сканирование: src={len(src_tree)} файлов, dst={len(dst_tree)} файлов")
        self.call_from_thread(info.log, f"Новых: {c.get('copy_new',0)}  Изменённых: {c.get('copy_update',0)}  В backup: {c.get('delete',0)}")
        self.logger.info(f"Scan: src={len(src_tree)} dst={len(dst_tree)} plan={len(self._plan)}")

    def _fill_table(self, plan: list[SyncAction]) -> None:
        t = self.query_one("#plan-table", DataTable)
        t.clear()
        for a in plan:
            if a.action == ActionType.SKIP_EQUAL:
                continue
            style  = ACTION_STYLE[a.action]
            label  = ACTION_LABEL[a.action]
            will   = ACTION_WILL_DO[a.action]
            rel    = str(a.rel_path)
            if len(rel) > 60:
                rel = "..." + rel[-57:]
            t.add_row(
                Text(label, style=style),
                Text(will,  style=style),
                Text(rel),
                Text(_fmt_size(a.size_bytes)),
            )

    # ── Синхронизация ─────────────────────────────────────────────────────────

    def _start_sync(self, dry_run: bool) -> None:
        if not self._plan:
            self.notify("Сначала нажмите Сканировать (Ctrl+S)", severity="warning")
            return

        active = [a for a in self._plan if a.action not in (
            ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED
        )]

        if not active:
            self.notify("Нет изменений — всё актуально.", severity="information")
            return

        needs_double = any(
            a.protection_level == ProtectionLevel.DOUBLE and
            a.action in (ActionType.COPY_UPDATE, ActionType.DELETE)
            for a in active
        )

        p = self.current_profile
        n_copy = sum(1 for a in active if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE))
        n_del  = sum(1 for a in active if a.action == ActionType.DELETE)
        mb     = sum(a.size_bytes for a in active if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE)) / (1024*1024)

        if dry_run:
            msg = (
                f"[bold]DRY RUN — симуляция без изменений[/]\n\n"
                f"Источник: {p.src}\n"
                f"Приёмник: {p.dst}\n\n"
                f"Будет скопировано/обновлено: {n_copy} файлов ({mb:.1f} MB)\n"
                f"Будет перемещено в backup:   {n_del} файлов\n\n"
                f"Запустить симуляцию?"
            )
        else:
            msg = (
                f"[bold]Подтвердите синхронизацию[/]\n\n"
                f"Источник: {p.src}\n"
                f"Приёмник: {p.dst}\n\n"
                f"Скопировать/обновить: {n_copy} файлов ({mb:.1f} MB)\n"
                f"Переместить в backup: {n_del} файлов\n\n"
                f"Продолжить?"
            )

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
        info = self.query_one("#info", InfoPanel)

        def on_progress(action: SyncAction, msg: str) -> None:
            style = ACTION_STYLE.get(action.action, "")
            self.call_from_thread(self._set_status, msg[:100])
            self.call_from_thread(info.log, msg, style)

        engine = SyncEngine(
            profile=p,
            src=Path(p.src),
            dst=Path(p.dst),
            dry_run=dry_run,
            progress_cb=on_progress,
        )

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

        if dry_run:
            # Показываем подробный лог dry run в отдельном окне
            self._last_dry_run_report = report
            s = report.stats
            mb_c = report.bytes_copied / (1024*1024)
            mb_b = report.bytes_backed_up / (1024*1024)
            summary = (
                f"Скопировать: {s.get('copy_new',0)} новых + {s.get('copy_update',0)} обновлений "
                f"({mb_c:.1f} MB)  |  В backup: {s.get('delete',0)} файлов ({mb_b:.1f} MB)"
            )
            self.call_from_thread(
                self.push_screen,
                DryRunLogScreen(report.log_lines, summary),
                lambda _: None
            )
            self.call_from_thread(self._set_status, f"[DRY RUN] {summary}")
        else:
            try:
                rpath = save_report(report)
                self.call_from_thread(info.log, f"Отчёт сохранён: {rpath}")
            except Exception:
                pass

            s = report.stats
            msg = (
                f"Готово: скопировано {s.get('copy_new',0)} новых, "
                f"обновлено {s.get('copy_update',0)}, "
                f"в backup {s.get('delete',0)}"
            )
            self.call_from_thread(self._set_status, msg)

            if report.errors:
                self.call_from_thread(
                    self.notify,
                    f"Завершено с {len(report.errors)} ошибками. Смотрите лог.",
                    severity="warning"
                )
            else:
                self.call_from_thread(self.notify, "Синхронизация завершена!", severity="information")

    def _set_status(self, msg: str) -> None:
        try:
            self.query_one("#slabel", Label).update(msg)
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
