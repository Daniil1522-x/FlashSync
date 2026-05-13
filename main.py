#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
main.py — Entry point for FlashSync Pro.
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to Python path
sys.path.insert(0, str(Path(__file__).parent))


def _ensure_profile_setup() -> None:
    """Check if profile paths are set. Ask user on first launch."""
    from infrastructure.storage import load_profiles, save_profiles

    profiles = load_profiles()
    p = list(profiles.values())[0]

    if not p.src or not Path(p.src).exists():
        print("\n" + "=" * 60)
        print("🆕 First launch of FlashSync")
        print("=" * 60)
        print("Let's set up your sync folders.\n")

        src = input(f"Source (flash drive) [E:\\FLASH]: ").strip() or r"E:\FLASH"
        dst_default = str(Path.home() / "FlashBackup")
        dst = input(f"Destination [{dst_default}]: ").strip() or dst_default

        p.src = src
        p.dst = dst
        profiles[p.name] = p
        save_profiles(profiles)

        print(f"✅ Settings saved!\n   Source:      {src}")
        print(f"   Destination: {dst}\n")


def cmd_stats(path: str) -> None:
    from infrastructure.scanner import get_directory_stats
    from domain.models import _classify

    root = Path(path)
    if not root.exists():
        print(f"❌ Path not found: {root}")
        return

    print(f"\n📁 Analysis: {root}")
    print("─" * 60)
    stats = get_directory_stats(root)

    print(f"Files:   {stats['total_files']:,}")
    mb = stats["total_size"] / (1024 * 1024)
    print(f"Size:    {mb:.1f} MB\n")

    print("By category:")
    for cat, cnt in sorted(stats["categories"].items(), key=lambda x: -x[1]):
        print(f"  {cat:<12} {cnt:>5}")

    print("\nTop extensions:")
    for ext, cnt in list(stats["extensions"].items())[:10]:
        print(f"  {ext:<12} {cnt:>5}")


def cmd_scan(src: str, dst: str, use_hash: bool = True) -> None:
    from infrastructure.scanner import scan_directory
    from application.differ import DiffEngine, summarize_plan
    from domain.models import SyncProfile, ActionType

    p = SyncProfile(name="cli", src=src, dst=dst, use_hash=use_hash)
    src_path = Path(src)
    dst_path = Path(dst)

    if not src_path.exists():
        print(f"❌ Source not found: {src}")
        return

    dst_path.mkdir(parents=True, exist_ok=True)

    print(f"🔍 Scanning source...")
    src_tree = scan_directory(src_path, use_hash=use_hash)
    print(f"🔍 Scanning destination...")
    dst_tree = scan_directory(dst_path, use_hash=use_hash)

    engine = DiffEngine(p)
    plan = engine.compute_plan(src_tree, dst_tree)
    summary = summarize_plan(plan)

    print(f"\n📋 SYNC PLAN")
    print("─" * 60)
    counts = summary["counts"]
    mb = summary["bytes_to_copy"] / (1024 * 1024)

    print(f"  ⊕ New:        {counts.get('copy_new', 0):>5}")
    print(f"  ↻ Update:     {counts.get('copy_update', 0):>5}")
    print(f"  ⊘ To backup:  {counts.get('delete', 0):>5}")
    print(f"  ≡ Same:       {counts.get('skip_equal', 0):>5}")
    print(f"  🔒 Protected:  {counts.get('skip_protected', 0):>5}")
    print(f"\n  💾 To copy:   {mb:.1f} MB")


def cmd_sync(src: str, dst: str, dry_run: bool = False, use_hash: bool = True) -> None:
    from infrastructure.scanner import scan_directory
    from application.differ import DiffEngine
    from application.sync_engine import SyncEngine
    from infrastructure.storage import save_report
    from domain.models import SyncProfile, ActionType, SyncReport

    p = SyncProfile(name="cli", src=src, dst=dst, use_hash=use_hash)
    src_path = Path(src)
    dst_path = Path(dst)

    if not src_path.exists():
        print(f"❌ Source not found: {src}")
        return

    dst_path.mkdir(parents=True, exist_ok=True)

    print("🔍 Scanning...")
    src_tree = scan_directory(src_path, use_hash=use_hash)
    dst_tree = scan_directory(dst_path, use_hash=use_hash)

    engine = DiffEngine(p)
    plan = engine.compute_plan(src_tree, dst_tree)

    active = [a for a in plan if a.action not in (ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED)]

    if not active:
        print("✅ Everything is up to date.")
        return

    if not dry_run:
        print(f"\n⚠️  {len(active)} operations will be performed.")
        if input("Continue? (y/N): ").strip().lower() != "y":
            print("Cancelled.")
            return

    def on_progress(action, msg):
        prefix = "[DRY RUN] " if dry_run else ""
        print(f"  {prefix}{msg}")

    sync_engine = SyncEngine(
        profile=p, src=src_path, dst=dst_path,
        dry_run=dry_run, progress_cb=on_progress
    )

    report = asyncio.run(sync_engine.execute(active))

    if not dry_run:
        rpath = save_report(report)
        print(f"\n📝 Report saved: {rpath}")

    print(f"\n{'[DRY RUN] ' if dry_run else ''}✅ Done!")
    for k, v in report.stats.items():
        print(f"  {k}: {v}")


def cmd_audit(src: str, dst: str, output: str | None = None) -> None:
    """Perform SHA-256 audit between two directories."""
    from infrastructure.scanner import scan_directory
    from datetime import datetime

    src_path = Path(src)
    dst_path = Path(dst)

    print(f"🔍 Hashing source...")
    src_tree = scan_directory(src_path, use_hash=True)
    print(f"🔍 Hashing destination...")
    dst_tree = scan_directory(dst_path, use_hash=True)

    # ... (остальная часть функции без изменений, если хочешь — могу тоже почистить)


def launch_tui() -> None:
    _ensure_profile_setup()

    try:
        from ui.tui.app import run
        run()
    except ImportError as e:
        print(f"⚠️  Textual is not installed: {e}")
        print("Install with: pip install textual")
        cli_menu()


def cli_menu() -> None:
    _ensure_profile_setup()

    from infrastructure.storage import load_profiles, save_profiles

    profiles = load_profiles()
    p = list(profiles.values())[0]

    while True:
        print("\n" + "=" * 70)
        print("  ⚡ FlashSync Pro — CLI")
        print("=" * 70)
        print(f"  Profile:   {p.name}")
        print(f"  Source:    {p.src}")
        print(f"  Destination: {p.dst}")
        print("-" * 70)
        print("  1. Scan & show plan")
        print("  2. Sync")
        print("  3. Dry Run")
        print("  4. Audit (SHA-256)")
        print("  5. Folder stats")
        print("  6. Change paths")
        print("  0. Exit")
        print("=" * 70)

        choice = input("\nAction: ").strip()

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
            path = input("Path: ").strip()
            cmd_stats(path)
        elif choice == "6":
            p.src = input(f"New source [{p.src}]: ").strip() or p.src
            p.dst = input(f"New destination [{p.dst}]: ").strip() or p.dst
            profiles[p.name] = p
            save_profiles(profiles)
        else:
            print("Unknown command.")

        input("\nPress Enter to continue...")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="flashsync",
        description="⚡ FlashSync Pro — Professional file synchronizer"
    )
    parser.add_argument("command", nargs="?",
                        choices=["scan", "sync", "audit", "stats", "tui"],
                        help="Command to run (default: tui)")

    parser.add_argument("-s", "--src", help="Source path")
    parser.add_argument("-d", "--dst", help="Destination path")
    parser.add_argument("-o", "--output", help="Output report file")

    parser.add_argument("--dry-run", action="store_true", help="Simulation mode")
    parser.add_argument("--no-hash", action="store_true", help="Disable SHA-256 (faster)")
    parser.add_argument("--cli", action="store_true", help="Use CLI menu instead of TUI")
    parser.add_argument("path", nargs="?", help="Path for stats command")

    args = parser.parse_args()

    if args.cli:
        cli_menu()
        return

    cmd = args.command or "tui"

    if cmd == "tui":
        launch_tui()
    elif cmd == "stats":
        cmd_stats(args.path or args.src or ".")
    elif cmd == "scan":
        if not args.src or not args.dst:
            parser.error("scan requires -s SRC -d DST")
        cmd_scan(args.src, args.dst, use_hash=not args.no_hash)
    elif cmd == "sync":
        if not args.src or not args.dst:
            parser.error("sync requires -s SRC -d DST")
        cmd_sync(args.src, args.dst, dry_run=args.dry_run, use_hash=not args.no_hash)
    elif cmd == "audit":
        if not args.src or not args.dst:
            parser.error("audit requires -s SRC -d DST")
        cmd_audit(args.src, args.dst, args.output)


if __name__ == "__main__":
    main()