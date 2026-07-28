#!/usr/bin/env python3
"""
scripts/build_exe.py — Сборка FlashSync Pro в standalone .exe через PyInstaller.

Использование:
    pip install pyinstaller
    python scripts/build_exe.py

Результат: dist/FlashSync.exe (Windows) или dist/FlashSync (Linux/macOS)
"""
from __future__ import annotations
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller не установлен. Установите: pip install pyinstaller", file=sys.stderr)
        return 1

    main_py = PROJECT_ROOT / "main.py"
    if not main_py.exists():
        print(f"Не найден {main_py}", file=sys.stderr)
        return 1

    # Чистим предыдущие сборки
    for d in ("build", "dist"):
        p = PROJECT_ROOT / d
        if p.exists():
            shutil.rmtree(p)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name=FlashSync",
        "--onefile",
        "--console",
        "--clean",
        "--noconfirm",
        # Textual использует динамические импорты CSS/виджетов — добавляем явно
        "--hidden-import=textual",
        "--hidden-import=textual.widgets",
        "--hidden-import=rich",
        str(main_py),
    ]

    print("Запуск PyInstaller:")
    print(" ".join(cmd))
    result = subprocess.run(cmd, cwd=str(PROJECT_ROOT))

    if result.returncode == 0:
        dist_dir = PROJECT_ROOT / "dist"
        print(f"\nГотово! Исполняемый файл в: {dist_dir}")
    else:
        print("\nСборка завершилась с ошибкой.", file=sys.stderr)

    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
