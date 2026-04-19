"""
infrastructure/storage.py — Хранение профилей и отчётов.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from datetime import datetime
from typing import Optional

from domain.models import SyncProfile, ProtectionRule, ProtectionLevel, HashAlgo, SyncReport

CONFIG_DIR  = Path.home() / ".flashsync"
CONFIG_PATH = CONFIG_DIR / "config.json"
LOG_PATH    = CONFIG_DIR / "flashsync.log"
REPORTS_DIR = CONFIG_DIR / "reports"


def setup_logger() -> logging.Logger:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("flashsync")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(fh)
    return logger


def _rule_from_dict(d: dict) -> ProtectionRule:
    return ProtectionRule(
        pattern=d["pattern"],
        level=ProtectionLevel(d.get("level", 1)),
        description=d.get("description", ""),
    )


def _rule_to_dict(r: ProtectionRule) -> dict:
    return {"pattern": r.pattern, "level": r.level.value, "description": r.description}


def _profile_from_dict(d: dict) -> SyncProfile:
    try:
        hash_algo = HashAlgo(d.get("hash_algo", "sha256"))
    except ValueError:
        hash_algo = HashAlgo.SHA256

    # ИСПРАВЛЕНИЕ: нормализуем пути — убираем дублирование буквы диска
    src = _normalize_path(d.get("src", ""))
    dst = _normalize_path(d.get("dst", ""))

    return SyncProfile(
        name=d.get("name", "default"),
        src=src,
        dst=dst,
        include_patterns=d.get("include_patterns", []),
        exclude_patterns=d.get("exclude_patterns", [
            "*.tmp", "*.log", "*.bak", "thumbs.db", ".ds_store", "desktop.ini"
        ]),
        protection_rules=[_rule_from_dict(r) for r in d.get("protection_rules", _DEFAULT_RULES)],
        use_hash=d.get("use_hash", True),
        hash_algo=hash_algo,
        delete_mode=d.get("delete_mode", False),
        backup_limit_mb=d.get("backup_limit_mb", 500),
        max_workers=d.get("max_workers", 4),
        ignore_hidden=d.get("ignore_hidden", True),
    )


def _normalize_path(path_str: str) -> str:
    """Убирает дублирование буквы диска Windows: E:\E:\... → E:\..."""
    if not path_str:
        return path_str
    # Паттерн E:\E:\ → убираем дубль
    import re
    fixed = re.sub(r'^([A-Za-z]:\\)[A-Za-z]:\\', r'\1', path_str)
    return fixed


def _profile_to_dict(p: SyncProfile) -> dict:
    return {
        "name": p.name,
        "src": str(p.src),
        "dst": str(p.dst),
        "include_patterns": p.include_patterns,
        "exclude_patterns": p.exclude_patterns,
        "protection_rules": [_rule_to_dict(r) for r in p.protection_rules],
        "use_hash": p.use_hash,
        "hash_algo": p.hash_algo.value,
        "delete_mode": p.delete_mode,
        "backup_limit_mb": p.backup_limit_mb,
        "max_workers": p.max_workers,
        "ignore_hidden": p.ignore_hidden,
    }


_DEFAULT_RULES = [
    {"pattern": "*important*", "level": 2, "description": "важные файлы"},
    {"pattern": "*contract*",  "level": 2, "description": "договоры"},
    {"pattern": "*personal*",  "level": 2, "description": "личные данные"},
    {"pattern": "*backup*",    "level": 1, "description": "резервные копии"},
    {"pattern": "*final*",     "level": 1, "description": "финальные версии"},
]


def load_profiles() -> dict[str, SyncProfile]:
    if not CONFIG_PATH.exists():
        default = _make_default_profile()
        save_profiles({"default": default})
        return {"default": default}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        return {name: _profile_from_dict(d) for name, d in raw.items()}
    except Exception:
        return {"default": _make_default_profile()}


def save_profiles(profiles: dict[str, SyncProfile]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    raw = {name: _profile_to_dict(p) for name, p in profiles.items()}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def _make_default_profile() -> SyncProfile:
    if os.name == "nt":
        src = r"E:\FLASH"
        dst = r"E:\IT\Backup\Flash"
    else:
        src = str(Path.home() / "flash_source")
        dst = str(Path.home() / "flash_backup")
    return _profile_from_dict({"name": "default", "src": src, "dst": dst})


def save_report(report: SyncReport, out_path: Optional[Path] = None) -> Path:
    if out_path is None:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = REPORTS_DIR / f"report_{ts}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"FlashSync Report — {report.started_at:%Y-%m-%d %H:%M:%S}\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Длительность: {report.duration_seconds:.1f}с\n")
        f.write(f"Скопировано: {report.bytes_copied:,} байт\n")
        f.write(f"В backup:    {report.bytes_backed_up:,} байт\n\n")
        f.write("Статистика:\n")
        for k, v in report.stats.items():
            f.write(f"  {k}: {v}\n")
        if report.errors:
            f.write(f"\nОшибки ({len(report.errors)}):\n")
            for e in report.errors:
                f.write(f"  {e}\n")
        if report.warnings:
            f.write(f"\nПредупреждения:\n")
            for w in report.warnings:
                f.write(f"  {w}\n")
    return out_path
