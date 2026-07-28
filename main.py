#!/usr/bin/env python3
"""
FlashSync Pro — точка входа.

Запуск: python main.py
"""
from __future__ import annotations
import sys
from pathlib import Path

# Гарантируем что корень проекта в sys.path независимо от того,
# из какой директории запущен скрипт
PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Импорты внутренних модулей — после настройки sys.path, но на уровне модуля
# (не внутри main()), т.к. circular import им не грозит и это упрощает
# статический анализ (mypy/pylint увидят отсутствующие зависимости сразу).
# Импорт ui.tui.app оставлен внутри main()/try — это единственный реально
# "тяжёлый" импорт (тащит Textual), и если ОН упадёт, мы хотим поймать
# это через тот же except Exception что и runtime-ошибки, а не получить
# некрасивый traceback до того как logger вообще настроен.
from infrastructure.crash_reporter import setup_crash_reporter
from infrastructure.storage import setup_logger, CONFIG_DIR
from domain.version import __version__


def main() -> int:
    # Crash reporter устанавливается максимально рано — до запуска TUI,
    # чтобы перехватить даже ошибки на старте приложения
    setup_crash_reporter(app_version=__version__)
    logger = setup_logger()
    logger.info("=" * 50)
    logger.info(f"Запуск FlashSync Pro v{__version__}")

    try:
        from ui.tui.app import FlashSyncApp
        app = FlashSyncApp()
        app.run()
    except KeyboardInterrupt:
        logger.info("Прервано пользователем (Ctrl+C)")
        return 0
    except Exception:
        logger.exception("Необработанная ошибка при запуске")
        print(f"\nКритическая ошибка. Подробности в: {CONFIG_DIR / 'crashes'}", file=sys.stderr)
        return 1
    finally:
        logger.info("FlashSync Pro завершён")

    return 0


if __name__ == "__main__":
    sys.exit(main())
