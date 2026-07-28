#!/usr/bin/env python3
"""
scripts/generate_test_data.py — Быстрый генератор тестовых данных для FlashSync Pro.

Создаёт пару папок SRC и DST с четырьмя категориями файлов:
  IDENTICAL  → ожидаемый результат: SKIP_EQUAL
  MODIFIED   → ожидаемый результат: COPY_UPDATE
  NEW_SRC    → ожидаемый результат: COPY_NEW
  DST_ONLY   → ожидаемый результат: DELETE (если включён delete_mode)

Ключевые отличия от прошлой версии:
  - Генерирует 2 буфера по 4 МБ ОДИН РАЗ → все файлы пишутся из них
    (в 20–50× быстрее, чем random.randbytes() на каждый файл)
  - Ошибки идут в stderr явно и не перетираются прогресс-баром
  - Windows-safe имена файлов (нет trailing dots, спецсимволов)
  - Флаг --clean для удаления созданных данных
  - Три размера: small (~50 МБ, 5–15 с), medium (~400 МБ), large (~4 ГБ)

Использование:
  uv run scripts/generate_test_data.py                    # small — быстрая проверка
  uv run scripts/generate_test_data.py --medium           # ~400 МБ
  uv run scripts/generate_test_data.py --large            # ~4 ГБ, стресс-тест
  uv run scripts/generate_test_data.py --clean            # удалить папки SRC/DST
  uv run scripts/generate_test_data.py --path D:\\Test    # своя папка
  uv run scripts/generate_test_data.py --verify           # после запуска проверить счётчики
"""
from __future__ import annotations
import argparse, os, random, shutil, sys, time
from pathlib import Path

# ── Буферы генерируются ОДИН РАЗ при загрузке ───────────────────────────────
# Все файлы записываются из этих двух блоков.
# BUF_A = «старый» контент, BUF_B = «новый» контент (другой хеш).
print("Подготовка буферов... ", end="", flush=True)
_t0 = time.perf_counter()
BUF_A = os.urandom(4 * 1024 * 1024)
BUF_B = os.urandom(4 * 1024 * 1024)
print(f"готово ({(time.perf_counter()-_t0)*1000:.0f} мс)")

# ── Пресеты ─────────────────────────────────────────────────────────────────
PRESETS: dict[str, dict] = {
    "small": {
        "identical": 50,   # → ~10 МБ
        "modified":  30,   # → ~18 МБ
        "new_src":   80,   # → ~16 МБ
        "dst_only":  30,   # → ~9 МБ
        "min_kb": 50, "max_kb": 400,
        "desc": "~50 МБ | 190 файлов | 5–15 сек",
    },
    "medium": {
        "identical": 400,
        "modified":  200,
        "new_src":   600,
        "dst_only":  200,
        "min_kb": 200, "max_kb": 1500,
        "desc": "~400 МБ | 1400 файлов | 1–3 мин",
    },
    "large": {
        "identical": 800,
        "modified":  400,
        "new_src":  1600,
        "dst_only":  400,
        "min_kb": 500, "max_kb": 3000,
        "desc": "~4 ГБ | 3200 файлов | стресс-тест",
    },
}

_EXTS: dict[str, list[str]] = {
    "photos":    [".jpg", ".png", ".heic", ".raw"],
    "documents": [".pdf", ".docx", ".xlsx", ".txt"],
    "music":     [".mp3", ".flac", ".m4a"],
    "video":     [".mp4", ".mov", ".avi"],
    "archive":   [".zip", ".bak", ".tar"],
    "code":      [".py",  ".js",  ".json"],
}
_CATS = list(_EXTS.keys())

# ── Вспомогательные функции ─────────────────────────────────────────────────
def _fmt(n: float) -> str:
    for u in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} ПБ"


def _write(path: Path, buf: bytes, size: int) -> None:
    """Записывает файл нужного размера, заполняя его данными из buf."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        written = 0
        while written < size:
            end = min(len(buf), size - written)
            f.write(buf[:end])
            written += end


def _mtime(path: Path, ts: float) -> None:
    try:
        os.utime(path, (ts, ts))
    except OSError:
        pass  # некоторые ФС ограничивают диапазон времён


def _bar(i: int, total: int, written: int, t0: float) -> None:
    """
    Прогресс-бар, который НЕ перетирает ошибки.
    Ошибки идут в sys.stderr, прогресс — в sys.stdout.
    """
    pct = 100 * i / total
    filled = int(30 * i / total)
    bar = "█" * filled + "·" * (30 - filled)
    elapsed = time.time() - t0
    speed = written / elapsed if elapsed > 0 else 0
    print(f"  [{bar}] {i:>4}/{total}  {pct:5.1f}%  {_fmt(speed)}/с",
          end="\r", flush=True)
    if i == total:
        print()


# ── Главная функция ──────────────────────────────────────────────────────────
def create_pair(src: Path, dst: Path, preset: dict, now: float) -> dict:
    src.mkdir(parents=True, exist_ok=True)
    dst.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    total_written = 0
    errors = []
    counters = {"identical": 0, "modified": 0, "new_src": 0, "dst_only": 0}

    def rand_size() -> int:
        return random.randint(preset["min_kb"] * 1024, preset["max_kb"] * 1024)

    def rand_ext(cat: str) -> str:
        return random.choice(_EXTS.get(cat, [".bin"]))

    # ── 1. ИДЕНТИЧНЫЕ ───────────────────────────────────────────────────────
    n = preset["identical"]
    print(f"\n→ Идентичные файлы ({n}) — ожидаем SKIP_EQUAL")
    for i in range(n):
        cat = _CATS[i % len(_CATS)]
        name = f"identical_{i:05d}{rand_ext(cat)}"
        size = rand_size()
        mtime = now - random.randint(86_400 * 365, 86_400 * 365 * 3)
        try:
            for folder in (src / cat, dst / cat):
                p = folder / name
                _write(p, BUF_A, size)
                _mtime(p, mtime)
            total_written += size * 2
            counters["identical"] += 1
        except Exception as e:
            errors.append(f"identical_{i}: {e}")
            print(f"\n  ⚠ ОШИБКА identical_{i}: {e}", file=sys.stderr)
        _bar(i + 1, n, total_written, t0)

    # ── 2. ИЗМЕНЁННЫЕ ───────────────────────────────────────────────────────
    n = preset["modified"]
    print(f"→ Изменённые файлы ({n}) — ожидаем COPY_UPDATE")
    for i in range(n):
        cat = _CATS[i % len(_CATS)]
        name = f"modified_{i:05d}{rand_ext(cat)}"
        old_size = rand_size()
        new_size = max(1024, int(old_size * random.uniform(0.7, 2.0)))
        old_mtime = now - random.randint(86_400 * 30, 86_400 * 365)
        new_mtime = now - random.randint(3_600, 86_400 * 7)
        try:
            p_dst = dst / cat / name
            _write(p_dst, BUF_A, old_size)   # старый контент в DST
            _mtime(p_dst, old_mtime)

            p_src = src / cat / name
            _write(p_src, BUF_B, new_size)   # новый контент (другой буфер) в SRC
            _mtime(p_src, new_mtime)

            total_written += old_size + new_size
            counters["modified"] += 1
        except Exception as e:
            errors.append(f"modified_{i}: {e}")
            print(f"\n  ⚠ ОШИБКА modified_{i}: {e}", file=sys.stderr)
        _bar(i + 1, n, total_written, t0)

    # ── 3. ТОЛЬКО В SRC ─────────────────────────────────────────────────────
    n = preset["new_src"]
    print(f"→ Только в SRC ({n}) — ожидаем COPY_NEW")
    for i in range(n):
        cat = _CATS[i % len(_CATS)]
        name = f"new_src_{i:05d}{rand_ext(cat)}"
        size = rand_size()
        mtime = now - random.randint(3_600, 86_400 * 30)
        try:
            p = src / cat / name
            _write(p, BUF_B, size)
            _mtime(p, mtime)
            total_written += size
            counters["new_src"] += 1
        except Exception as e:
            errors.append(f"new_src_{i}: {e}")
            print(f"\n  ⚠ ОШИБКА new_src_{i}: {e}", file=sys.stderr)
        _bar(i + 1, n, total_written, t0)

    # ── 4. ТОЛЬКО В DST ─────────────────────────────────────────────────────
    n = preset["dst_only"]
    print(f"→ Только в DST ({n}) — ожидаем DELETE (при delete_mode=True)")
    for i in range(n):
        cat = _CATS[i % len(_CATS)]
        name = f"dst_only_{i:05d}{rand_ext(cat)}"
        size = rand_size()
        mtime = now - random.randint(86_400 * 365, 86_400 * 365 * 5)
        try:
            p = dst / cat / name
            _write(p, BUF_A, size)
            _mtime(p, mtime)
            total_written += size
            counters["dst_only"] += 1
        except Exception as e:
            errors.append(f"dst_only_{i}: {e}")
            print(f"\n  ⚠ ОШИБКА dst_only_{i}: {e}", file=sys.stderr)
        _bar(i + 1, n, total_written, t0)

    elapsed = time.time() - t0
    return {
        "elapsed": elapsed,
        "written": total_written,
        "errors":  errors,
        "counters": counters,
    }


def verify_pair(src: Path, dst: Path) -> None:
    """Подсчитывает файлы по папкам и выводит ожидаемый план FlashSync."""
    print("\n── Проверка структуры ──────────────────────────────")
    src_files = {p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()}
    dst_files = {p.relative_to(dst).as_posix() for p in dst.rglob("*") if p.is_file()}

    only_src  = src_files - dst_files
    only_dst  = dst_files - src_files
    in_both   = src_files & dst_files

    identical = sum(1 for r in in_both if "identical_" in r)
    modified  = sum(1 for r in in_both if "modified_"  in r)

    print(f"  SRC файлов:          {len(src_files):>5}")
    print(f"  DST файлов:          {len(dst_files):>5}")
    print(f"  Только в SRC:        {len(only_src):>5}  → COPY_NEW")
    print(f"  Только в DST:        {len(only_dst):>5}  → DELETE")
    print(f"  В обеих / identical: {identical:>5}  → SKIP_EQUAL (ожидаем)")
    print(f"  В обеих / modified:  {modified:>5}  → COPY_UPDATE (ожидаем)")
    print()
    print("  Настройте FlashSync:")
    print(f"    SRC = {src}")
    print(f"    DST = {dst}")
    print("    delete_mode = True  (чтобы увидеть DELETE)")


# ── CLI ──────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Генератор тестовых данных для FlashSync Pro",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(
            f"  --{k:<8} {v['desc']}" for k, v in PRESETS.items()
        ),
    )
    size_grp = parser.add_mutually_exclusive_group()
    size_grp.add_argument("--small",  action="store_true", default=True,
                          help="~50 МБ, быстро (по умолчанию)")
    size_grp.add_argument("--medium", action="store_true",
                          help="~400 МБ")
    size_grp.add_argument("--large",  action="store_true",
                          help="~4 ГБ, стресс-тест")

    parser.add_argument("--path",   default="./TEST_DATA",
                        help="Корневая папка (по умолчанию: ./TEST_DATA)")
    parser.add_argument("--clean",  action="store_true",
                        help="Удалить TEST_DATA/SRC и TEST_DATA/DST")
    parser.add_argument("--verify", action="store_true",
                        help="После генерации проверить структуру файлов")
    parser.add_argument("--seed",   type=int, default=None,
                        help="Random seed для воспроизводимого результата")

    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        print(f"Seed: {args.seed}")

    root = Path(args.path)
    src  = root / "SRC"
    dst  = root / "DST"

    # ── Удаление ────────────────────────────────────────────────────────────
    if args.clean:
        for folder in (src, dst):
            if folder.exists():
                print(f"Удаляю {folder}...", end=" ", flush=True)
                shutil.rmtree(folder)
                print("OK")
            else:
                print(f"Уже отсутствует: {folder}")
        return

    # ── Проверка места ───────────────────────────────────────────────────────
    if args.large:
        preset_name = "large"
    elif args.medium:
        preset_name = "medium"
    else:
        preset_name = "small"

    preset = PRESETS[preset_name]
    print(f"\nПресет: {preset_name.upper()} — {preset['desc']}")
    print(f"Папка:  {root.resolve()}\n")

    try:
        total, used, free = shutil.disk_usage(str(root.parent))
    except OSError:
        total = used = free = 0

    if free > 0:
        free_mb = free / (1024 * 1024)
        print(f"Свободно: {_fmt(free)}")
        if free_mb < 100:
            print("⚠ Мало места!", file=sys.stderr)

    # ── Генерация ────────────────────────────────────────────────────────────
    now = time.time()
    result = create_pair(src, dst, preset, now)

    total_files = sum(result["counters"].values())
    spd = result["written"] / result["elapsed"] if result["elapsed"] > 0 else 0

    print(f"""
{'='*60}
{'✅ ГОТОВО' if not result['errors'] else f'⚠ ГОТОВО с {len(result["errors"])} ошибками'}
   Файлов:   {total_files:,}
   Данных:   {_fmt(result['written'])}
   Время:    {result['elapsed']:.1f} сек
   Скорость: {_fmt(spd)}/с

Структура (что FlashSync должен распознать):
   SKIP_EQUAL  — {result['counters']['identical']:>4} identical_NNNNN.*
   COPY_UPDATE — {result['counters']['modified']:>4} modified_NNNNN.*
   COPY_NEW    — {result['counters']['new_src']:>4} new_src_NNNNN.*
   DELETE      — {result['counters']['dst_only']:>4} dst_only_NNNNN.*
{'='*60}

Настройте FlashSync:
  SRC = {src}
  DST = {dst}
  delete_mode = True  (чтобы увидеть DELETE)
""")

    if result["errors"]:
        print(f"Ошибки ({len(result['errors'])}):", file=sys.stderr)
        for e in result["errors"][:10]:
            print(f"  {e}", file=sys.stderr)

    if args.verify:
        verify_pair(src, dst)


if __name__ == "__main__":
    main()
