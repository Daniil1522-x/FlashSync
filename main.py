#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — Точка входа FlashSync Pro.

Режимы запуска:
  python main.py            → TUI (Textual)
  python main.py --cli      → Простое CLI меню
  python main.py scan       → CLI: только сканирование
  python main.py sync       → CLI: синхронизация
  python main.py audit      → CLI: аудит (afqk)
  python main.py stats PATH → Статистика директории
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Добавляем корень проекта в path
sys.path.insert(0, str(Path(__file__).parent))


def cmd_stats(path: str) -> None:
    from infrastructure.scanner import get_directory_stats, scan_tree_structure
    from domain.models import _classify

    root = Path(path)
    if not root.exists():
        print(f"❌ Путь не найден: {root}")
        return

    print(f"\n📁 Анализ: {root}")
    print("─" * 60)
    stats = get_directory_stats(root)
    print(f"Файлов:   {stats['total_files']:,}")
    mb = stats["total_size"] / (1024 * 1024)
    print(f"Размер:   {mb:.1f} МБ")
    print()
    print("По категориям:")
    for cat, cnt in sorted(stats["categories"].items(), key=lambda x: -x[1]):
        print(f"  {cat:<12} {cnt:>5}")
    print()
    print("Расширения (топ-10):")
    for ext, cnt in list(stats["extensions"].items())[:10]:
        print(f"  {ext:<12} {cnt:>5}")


def cmd_scan(src: str, dst: str, use_hash: bool = True) -> None:
    from infrastructure.scanner import scan_directory
    from application.differ import DiffEngine, summarize_plan
    from infrastructure.storage import load_profiles
    from domain.models import SyncProfile, ActionType

    p = SyncProfile(name="cli", src=src, dst=dst, use_hash=use_hash)
    src_path = Path(src)
    dst_path = Path(dst)

    if not src_path.exists():
        print(f"❌ Источник не найден: {src}")
        return

    dst_path.mkdir(parents=True, exist_ok=True)

    print(f"🔍 Сканирование {src_path}...")
    src_tree = scan_directory(src_path, use_hash=use_hash)
    print(f"🔍 Сканирование {dst_path}...")
    dst_tree = scan_directory(dst_path, use_hash=use_hash)

    engine = DiffEngine(p)
    plan = engine.compute_plan(src_tree, dst_tree)
    summary = summarize_plan(plan)

    print(f"\n📋 ПЛАН СИНХРОНИЗАЦИИ")
    print("─" * 60)
    counts = summary["counts"]
    mb = summary["bytes_to_copy"] / (1024 * 1024)
    print(f"  ⊕  Новых:    {counts.get('copy_new', 0):>5}")
    print(f"  ↻  Обновить: {counts.get('copy_update', 0):>5}")
    print(f"  ⊘  Удалить:  {counts.get('delete', 0):>5}")
    print(f"  ≡  Равных:   {counts.get('skip_equal', 0):>5}")
    print(f"  🔒 Защищено: {counts.get('skip_protected', 0):>5}")
    print(f"\n  💾 К копированию: {mb:.1f} МБ")

    # Детали изменений
    changes = [a for a in plan if a.action not in (
        __import__('domain.models', fromlist=['ActionType']).ActionType.SKIP_EQUAL,
    )]
    if changes[:20]:
        print("\nИзменения (первые 20):")
        for a in changes[:20]:
            icons = {
                "copy_new": "⊕", "copy_update": "↻",
                "delete": "⊘", "skip_protected": "🔒",
            }
            icon = icons.get(a.action.value, "?")
            print(f"  {icon} {a.rel_path}")


def cmd_sync(src: str, dst: str, dry_run: bool = False, use_hash: bool = True) -> None:
    from infrastructure.scanner import scan_directory
    from application.differ import DiffEngine
    from application.sync_engine import SyncEngine
    from infrastructure.storage import load_profiles, save_report
    from domain.models import SyncProfile, ActionType, SyncReport

    p = SyncProfile(name="cli", src=src, dst=dst, use_hash=use_hash)
    src_path = Path(src)
    dst_path = Path(dst)

    if not src_path.exists():
        print(f"❌ Источник не найден: {src}")
        return

    dst_path.mkdir(parents=True, exist_ok=True)

    print(f"🔍 Сканирование...")
    src_tree = scan_directory(src_path, use_hash=use_hash)
    dst_tree = scan_directory(dst_path, use_hash=use_hash)

    engine = DiffEngine(p)
    plan = engine.compute_plan(src_tree, dst_tree)

    active = [a for a in plan if a.action not in (
        ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED
    )]

    if not active:
        print("✅ Всё актуально. Изменений нет.")
        return

    if not dry_run:
        print(f"\n⚠️  Будет выполнено {len(active)} операций.")
        confirm = input("Продолжить? (y/N): ")
        if confirm.lower() != "y":
            print("Отменено.")
            return

    def on_progress(action, msg):
        prefix = "[DRY] " if dry_run else ""
        print(f"  {prefix}{msg}")

    sync_engine = SyncEngine(
        profile=p, src=src_path, dst=dst_path,
        dry_run=dry_run, progress_cb=on_progress
    )

    report = asyncio.run(sync_engine.execute(active))

    if not dry_run:
        rpath = save_report(report)
        print(f"\n📝 Отчёт: {rpath}")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}✅ Готово!")
    for k, v in report.stats.items():
        print(f"  {k}: {v}")
    if report.errors:
        print(f"\n⚠️  Ошибок: {len(report.errors)}")
        for e in report.errors[:5]:
            print(f"  {e}")


def cmd_audit(src: str, dst: str, output: str | None = None) -> None:
    """SHA-256 аудит двух директорий (аналог afqk.py)."""
    from infrastructure.scanner import scan_directory
    from infrastructure.storage import setup_logger
    from datetime import datetime

    src_path = Path(src)
    dst_path = Path(dst)

    print(f"🔍 Хеширование {src_path}...")
    src_tree = scan_directory(src_path, use_hash=True)
    print(f"🔍 Хеширование {dst_path}...")
    dst_tree = scan_directory(dst_path, use_hash=True)

    src_set = set(src_tree.keys())
    dst_set = set(dst_tree.keys())

    only_src = sorted(src_set - dst_set)
    only_dst = sorted(dst_set - src_set)
    common = src_set & dst_set
    diff_content = [r for r in common
                    if src_tree[r].hash != dst_tree[r].hash]

    if output is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = f"audit_report_{ts}.txt"

    out = Path(output)
    with out.open("w", encoding="utf-8") as f:
        f.write(f"FlashSync Audit — {datetime.now():%Y-%m-%d %H:%M:%S}\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Источник: {src_path}\n")
        f.write(f"Приёмник: {dst_path}\n\n")
        f.write(f"Файлов в источнике: {len(src_tree)}\n")
        f.write(f"Файлов в приёмнике: {len(dst_tree)}\n\n")

        if only_src:
            f.write(f"Только в источнике ({len(only_src)}):\n")
            for p in only_src:
                f.write(f"  {p}\n")
            f.write("\n")

        if only_dst:
            f.write(f"Только в приёмнике ({len(only_dst)}):\n")
            for p in only_dst:
                f.write(f"  {p}\n")
            f.write("\n")

        if diff_content:
            f.write(f"Разный контент ({len(diff_content)}):\n")
            for p in diff_content:
                f.write(f"  {p}  src={src_tree[p].hash[:16]}…  dst={dst_tree[p].hash[:16]}…\n")
            f.write("\n")

        if not only_src and not only_dst and not diff_content:
            f.write("✅ Каталоги идентичны!\n")

    print(f"\n📝 Аудит-отчёт: {out}")
    print(f"  Только в src: {len(only_src)}")
    print(f"  Только в dst: {len(only_dst)}")
    print(f"  Разный контент: {len(diff_content)}")


def launch_tui() -> None:
    try:
        from ui.tui.app import run
        run()
    except ImportError as e:
        print(f"⚠️  Textual не установлен: {e}")
        print("Установите: pip install textual")
        print("Запуск CLI режима...\n")
        cli_menu()


def cli_menu() -> None:
    from infrastructure.storage import load_profiles, save_profiles

    profiles = load_profiles()
    p = list(profiles.values())[0]

    while True:
        print("\n" + "=" * 70)
        print("  ⚡ FlashSync Pro — CLI")
        print("=" * 70)
        print(f"  Профиль: {p.name}")
        print(f"  Источник: {p.src}")
        print(f"  Приёмник: {p.dst}")
        print("-" * 70)
        print("  1. Сканировать и показать план")
        print("  2. Синхронизировать")
        print("  3. Dry Run (только симуляция)")
        print("  4. Аудит (SHA-256 сравнение)")
        print("  5. Статистика директории")
        print("  6. Изменить пути")
        print("  0. Выход")
        print("=" * 70)

        choice = input("\nДействие: ").strip()

        if choice == "0":
            break
        elif choice == "1":
            cmd_scan(p.src, p.dst, use_hash=p.use_hash)
        elif choice == "2":
            cmd_sync(p.src, p.dst, dry_run=False, use_hash=p.use_hash)
        elif choice == "3":
            cmd_sync(p.src, p.dst, dry_run=True, use_hash=p.use_hash)
        elif choice == "4":
            cmd_audit(p.src, p.dst)
        elif choice == "5":
            path = input("Путь к директории: ").strip()
            cmd_stats(path)
        elif choice == "6":
            p.src = input(f"Новый источник [{p.src}]: ").strip() or p.src
            p.dst = input(f"Новый приёмник [{p.dst}]: ").strip() or p.dst
            profiles[p.name] = p
            save_profiles(profiles)
        else:
            print("Неизвестная команда.")

        input("\nEnter для продолжения...")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="flashsync",
        description="⚡ FlashSync Pro — профессиональный синхронизатор файлов"
    )
    parser.add_argument("command", nargs="?",
                        choices=["scan", "sync", "audit", "stats", "tui"],
                        help="Команда (по умолчанию: tui)")
    parser.add_argument("-s", "--src", help="Источник")
    parser.add_argument("-d", "--dst", help="Приёмник")
    parser.add_argument("-o", "--output", help="Файл отчёта")
    parser.add_argument("--dry-run", action="store_true", help="Симуляция без изменений")
    parser.add_argument("--no-hash", action="store_true", help="Без SHA-256 (быстрее)")
    parser.add_argument("--cli", action="store_true", help="CLI меню вместо TUI")
    parser.add_argument("path", nargs="?", help="Путь для команды stats")

    args = parser.parse_args()

    if args.cli:
        cli_menu()
        return

    cmd = args.command or "tui"

    if cmd == "tui":
        launch_tui()
    elif cmd == "stats":
        path = args.path or args.src or "."
        cmd_stats(path)
    elif cmd == "scan":
        if not args.src or not args.dst:
            parser.error("scan требует -s SRC -d DST")
        cmd_scan(args.src, args.dst, use_hash=not args.no_hash)
    elif cmd == "sync":
        if not args.src or not args.dst:
            parser.error("sync требует -s SRC -d DST")
        cmd_sync(args.src, args.dst, dry_run=args.dry_run, use_hash=not args.no_hash)
    elif cmd == "audit":
        if not args.src or not args.dst:
            parser.error("audit требует -s SRC -d DST")
        cmd_audit(args.src, args.dst, output=args.output)


if __name__ == "__main__":
    main()
