"""
scripts/generate_test_data.py — Генератор реалистичных тестовых данных.
Интегрирован с архитектурой FlashSync.

Запуск:
  python scripts/generate_test_data.py              # стандартный набор
  python scripts/generate_test_data.py --big        # большой набор
  python scripts/generate_test_data.py --small      # быстрый тест
  python scripts/generate_test_data.py --clean      # очистка
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path

# ─── Настройки путей ─────────────────────────────────────────────────────────
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

BASE = Path(__file__).parent.parent

if os.name == "nt":
    TEST_SRC = Path(r"E:\IT\Programming\2_tools__utilities__scripts\Scripts\Flash\TEST_SOURCE")
    TEST_DST = Path(r"E:\IT\Programming\2_tools__utilities__scripts\Scripts\Flash\TEST_DEST")
else:
    TEST_SRC = BASE / "test_data" / "SOURCE"
    TEST_DST = BASE / "test_data" / "DEST"

# ─── Данные для генерации ────────────────────────────────────────────────────
CATEGORIES = ["Фото", "Видео", "Документы", "Музыка", "Архивы", "Temp", "Old"]
KEYWORDS = ["important", "report", "photo", "video", "backup", "2025", "contract", "personal"]

EXT_MAP = {
    "Фото":     [".jpg", ".png", ".heic", ".raw"],
    "Видео":    [".mp4", ".mov", ".avi", ".mkv"],
    "Документы":[".pdf", ".docx", ".xlsx", ".txt", ".md"],
    "Музыка":   [".mp3", ".flac", ".wav"],
    "Архивы":   [".zip", ".rar", ".7z"],
    "Temp":     [".tmp", ".log", ".bak"],
    "Old":      [".old", ".backup"],
}

SIZE_PRESETS = [10, 50, 200, 1024, 5000]  # KB


def random_date(start_year=2023, end_year=2026) -> datetime:
    start = datetime(start_year, 1, 1)
    end   = datetime(end_year, 12, 31)
    return start + timedelta(days=random.randint(0, (end - start).days))


def write_random_file(path: Path, size_kb: int = 50) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(os.urandom(size_kb * 1024))
    ts = random_date().timestamp()
    os.utime(path, (ts, ts))


def random_filename(category: str) -> str:
    ext = random.choice(EXT_MAP[category])
    base = f"{category.lower()}_{random.randint(1000, 9999)}"
    if random.random() < 0.6:
        base += f"_{random.choice(KEYWORDS)}"
    if random.random() < 0.25:
        base += "_important"
    return base + ext


def generate(
    src_root: Path,
    dst_root: Path,
    total_folders: int = 20,
    files_per_folder: int = 12,
    max_depth: int = 4,
) -> None:
    """
    Реалистичный сценарий:
      ~50% файлов одинаковые в src и dst
      ~45% только в src (будут скопированы)
      ~5%  только в dst (лишние)
    """
    print(f"\n🚀 Генерация тестовых данных...")
    print(f"   SRC: {src_root}")
    print(f"   DST: {dst_root}")

    for d, name in [(src_root, "SOURCE"), (dst_root, "DEST")]:
        if d.exists():
            ans = input(f"Удалить существующую {name}? (y/n): ")
            if ans.lower() == "y":
                shutil.rmtree(d, ignore_errors=True)
                print(f"  🗑 {name} очищен")

    src_root.mkdir(parents=True, exist_ok=True)
    dst_root.mkdir(parents=True, exist_ok=True)

    created_src = 0
    created_dst = 0
    common = 0

    for i in range(total_folders):
        depth = random.randint(1, max_depth)
        src_cur = src_root
        dst_cur = dst_root

        for _ in range(depth):
            part = f"{random.choice(CATEGORIES)}_{random.randint(100, 999)}"
            if random.random() < 0.4:
                part += f"_{random.choice(KEYWORDS)}"
            src_cur = src_cur / part
            dst_cur = dst_cur / part
            src_cur.mkdir(parents=True, exist_ok=True)
            dst_cur.mkdir(parents=True, exist_ok=True)

        num_files = random.randint(6, files_per_folder)
        for _ in range(num_files):
            cat = random.choice(CATEGORIES)
            name = random_filename(cat)
            size = random.choice(SIZE_PRESETS)

            src_file = src_cur / name
            write_random_file(src_file, size)
            created_src += 1

            r = random.random()
            if r < 0.50:   # одинаковый файл в dst
                dst_file = dst_cur / name
                shutil.copy2(src_file, dst_file)
                created_dst += 1
                common += 1
            elif r < 0.95: # только в src (новый)
                pass
            else:          # только в dst (лишний)
                dst_file = dst_cur / name
                write_random_file(dst_file, size + random.randint(1, 10))
                created_dst += 1

        if created_src % 50 == 0 and created_src:
            print(f"  Обработано: {created_src} файлов...")

    print(f"\n✅ Готово!")
    print(f"   SOURCE: {created_src} файлов")
    print(f"   DEST:   {created_dst} файлов")
    print(f"   Общих: ~{common} ({int(common/created_src*100) if created_src else 0}%)")
    print(f"   Только в SRC: ~{created_src - common}")
    print(f"   Только в DST: ~{created_dst - common}")
    print(f"\n   Теперь запускайте: python main.py scan -s {src_root} -d {dst_root}")


def clean() -> None:
    for d in [TEST_SRC, TEST_DST]:
        if d.exists():
            shutil.rmtree(d)
            print(f"🗑 Удалено: {d}")
        else:
            print(f"⚠️  Не найдено: {d}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Генератор тестовых данных FlashSync")
    parser.add_argument("--src", default=str(TEST_SRC))
    parser.add_argument("--dst", default=str(TEST_DST))
    parser.add_argument("--small",  action="store_true", help="Маленький набор")
    parser.add_argument("--big",    action="store_true", help="Большой набор")
    parser.add_argument("--clean",  action="store_true", help="Очистить тестовые данные")
    args = parser.parse_args()

    if args.clean:
        clean()
    elif args.small:
        generate(Path(args.src), Path(args.dst), total_folders=5, files_per_folder=6, max_depth=2)
    elif args.big:
        generate(Path(args.src), Path(args.dst), total_folders=50, files_per_folder=25, max_depth=5)
    else:
        generate(Path(args.src), Path(args.dst))
