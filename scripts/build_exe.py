"""
scripts/build_exe.py — Сборка standalone .exe через PyInstaller.

Запуск (Windows PowerShell):
  pip install pyinstaller
  python scripts/build_exe.py

Результат: dist/flashsync.exe  (или dist/flashsync на Linux/macOS)
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC  = ROOT / "flashsync.spec"


PYINSTALLER_ARGS = [
    sys.executable, "-m", "PyInstaller",
    "--onefile",
    "--name", "flashsync",
    "--console",
    # Дополнительные данные
    "--add-data", f"{ROOT / 'domain'}{os.pathsep}domain",
    "--add-data", f"{ROOT / 'application'}{os.pathsep}application",
    "--add-data", f"{ROOT / 'infrastructure'}{os.pathsep}infrastructure",
    "--add-data", f"{ROOT / 'ui'}{os.pathsep}ui",
    # Скрытые импорты Textual
    "--hidden-import", "textual",
    "--hidden-import", "textual.app",
    "--hidden-import", "textual.widgets",
    "--hidden-import", "aiofiles",
    # Точка входа
    str(ROOT / "main.py"),
]


def build():
    print("⚙️  Запуск PyInstaller...")
    print(f"   Корень проекта: {ROOT}")
    result = subprocess.run(PYINSTALLER_ARGS, cwd=ROOT)
    if result.returncode == 0:
        exe_name = "flashsync.exe" if os.name == "nt" else "flashsync"
        exe_path = DIST / exe_name
        size_mb = exe_path.stat().st_size / (1024 * 1024) if exe_path.exists() else 0
        print(f"\n✅ Сборка завершена!")
        print(f"   Файл: {exe_path}")
        print(f"   Размер: {size_mb:.1f} МБ")
        print(f"\nЗапуск:")
        print(f"   {exe_path}               → TUI")
        print(f"   {exe_path} --cli         → CLI меню")
        print(f"   {exe_path} scan -s X -d Y → Сканирование")
        print(f"   {exe_path} sync -s X -d Y → Синхронизация")
        print(f"   {exe_path} audit -s X -d Y → Аудит")
    else:
        print("❌ Ошибка сборки. Убедитесь что установлен PyInstaller:")
        print("   pip install pyinstaller")


if __name__ == "__main__":
    build()
