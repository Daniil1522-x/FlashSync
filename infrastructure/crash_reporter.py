"""
infrastructure/crash_reporter.py — Перехват и логирование всех падений.

Что делает:
  1. Ловит ВСЕ необработанные исключения (sys.excepthook)
  2. Ловит исключения в asyncio задачах
  3. Ловит исключения в потоках (threading.excepthook)
  4. Пишет детальный crash-файл с трейсбеком, памятью, версией Python
  5. НЕ даёт программе молча закрыться — сохраняет диагностику

Использование:
    from infrastructure.crash_reporter import setup_crash_reporter
    setup_crash_reporter()
"""
from __future__ import annotations

import asyncio
import os
import platform
import sys
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

CRASH_DIR = Path.home() / ".flashsync" / "crashes"

_ASYNCIO_HANDLER: list[Optional[callable]] = [None]


def setup_crash_reporter(
    app_version: str = "0.0.0-unknown",  # передавайте domain.version.__version__ явно
    on_crash: Optional[callable] = None,
) -> None:
    """Устанавливает глобальные обработчики падений."""
    CRASH_DIR.mkdir(parents=True, exist_ok=True)

    original_excepthook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb):
        if issubclass(exc_type, KeyboardInterrupt):
            original_excepthook(exc_type, exc_value, exc_tb)
            return
        crash_path = _write_crash(exc_type, exc_value, exc_tb, app_version, "main_thread")
        _print_crash_info(crash_path)
        if on_crash:
            try:
                on_crash(crash_path)
            except Exception:
                pass

    sys.excepthook = _excepthook

    def _thread_excepthook(args):
        if args.exc_type is None or issubclass(args.exc_type, SystemExit):
            return
        crash_path = _write_crash(
            args.exc_type, args.exc_value, args.exc_traceback,
            app_version, f"thread:{getattr(args.thread, 'name', 'unknown')}",
        )
        _print_crash_info(crash_path)
        if on_crash:
            try:
                on_crash(crash_path)
            except Exception:
                pass

    threading.excepthook = _thread_excepthook

    def _asyncio_exception_handler(loop, context):
        exc = context.get("exception")
        if exc is None:
            _log_asyncio_message(context.get("message", "Unknown asyncio error"), app_version)
            return
        tb = exc.__traceback__
        crash_path = _write_crash(type(exc), exc, tb, app_version, "asyncio_task")
        _print_crash_info(crash_path)
        if on_crash:
            try:
                on_crash(crash_path)
            except Exception:
                pass

    try:
        loop = asyncio.get_event_loop()
        loop.set_exception_handler(_asyncio_exception_handler)
    except RuntimeError:
        pass

    _ASYNCIO_HANDLER[0] = _asyncio_exception_handler


def install_asyncio_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Вызвать для нового event loop (например в worker-потоке)."""
    if _ASYNCIO_HANDLER[0]:
        loop.set_exception_handler(_ASYNCIO_HANDLER[0])


def _write_crash(exc_type, exc_value, exc_tb, app_version: str, source: str) -> Path:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    crash_path = CRASH_DIR / f"crash_{ts}_{source[:20].replace(':', '_')}.txt"

    lines = [
        "=" * 70,
        "FlashSync Pro — Crash Report",
        "=" * 70,
        f"Время:      {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Версия:     {app_version}",
        f"Источник:   {source}",
        f"Python:     {sys.version}",
        f"Платформа:  {platform.platform()}",
        f"ОС:         {os.name} / {platform.system()} {platform.release()}",
        "",
        "─" * 70,
        f"Исключение: {exc_type.__name__ if exc_type else 'Unknown'}",
        f"Сообщение:  {exc_value}",
        "",
        "─" * 70,
        "Traceback (полный):",
        "",
    ]

    try:
        lines.extend(traceback.format_exception(exc_type, exc_value, exc_tb))
    except Exception as e:
        lines.append(f"[Не удалось получить traceback: {e}]")

    lines += ["", "─" * 70, "Контекст среды:", f"  cwd: {os.getcwd()}"]

    try:
        import psutil
        mem = psutil.Process().memory_info()
        lines += [
            f"  RAM использовано: {mem.rss / (1024**2):.1f} MB",
            f"  RAM виртуальное:  {mem.vms / (1024**2):.1f} MB",
        ]
    except ImportError:
        pass

    lines += ["", "=" * 70]

    try:
        crash_path.parent.mkdir(parents=True, exist_ok=True)
        crash_path.write_text("\n".join(lines), encoding="utf-8")
    except Exception:
        import tempfile
        crash_path = Path(tempfile.gettempdir()) / f"flashsync_crash_{ts}.txt"
        try:
            crash_path.write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass

    return crash_path


def _log_asyncio_message(message: str, app_version: str) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = CRASH_DIR / f"asyncio_warn_{ts}.txt"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            f"FlashSync asyncio warning — {datetime.now():%Y-%m-%d %H:%M:%S}\n"
            f"Version: {app_version}\n\n{message}\n",
            encoding="utf-8",
        )
    except Exception:
        pass


def _print_crash_info(crash_path: Path) -> None:
    print(f"\n{'='*60}", file=sys.stderr)
    print("FlashSync: КРИТИЧЕСКАЯ ОШИБКА", file=sys.stderr)
    print("Crash-репорт сохранён:", file=sys.stderr)
    print(f"  {crash_path}", file=sys.stderr)
    print("Отправьте этот файл разработчику для диагностики.", file=sys.stderr)
    print(f"{'='*60}\n", file=sys.stderr)


def get_crash_reports() -> list[Path]:
    if not CRASH_DIR.exists():
        return []
    return sorted(CRASH_DIR.glob("crash_*.txt"), reverse=True)


def get_latest_crash() -> Optional[Path]:
    reports = get_crash_reports()
    return reports[0] if reports else None


def crash_count_since(hours: int = 24) -> int:
    from datetime import timedelta
    cutoff = datetime.now() - timedelta(hours=hours)
    count = 0
    for p in get_crash_reports():
        try:
            if datetime.fromtimestamp(p.stat().st_mtime) > cutoff:
                count += 1
        except OSError:
            pass
    return count
