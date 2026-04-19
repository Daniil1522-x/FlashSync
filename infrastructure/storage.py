"""
infrastructure/storage.py — Хранение профилей синхронизации и логов.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from domain.models import SyncProfile, ProtectionRule, ProtectionLevel, HashAlgo, SyncReport


CONFIG_PATH = Path.home() / ".flashsync" / "config.json"
LOG_PATH    = Path.home() / ".flashsync" / "flashsync.log"


# ─────────────────────────────────────────────
# Логирование
# ─────────────────────────────────────────────

def setup_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("flashsync")
    logger.setLevel(logging.DEBUG)
    if not logger.handlers:
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        ))
        logger.addHandler(fh)
    return logger


# ─────────────────────────────────────────────
# Профили
# ─────────────────────────────────────────────

_DEFAULT_PROTECTION_RULES = [
    {"pattern": "*important*", "level": 2, "description": "important files"},
    {"pattern": "*contract*",  "level": 2, "description": "contracts"},
    {"pattern": "*personal*",  "level": 2, "description": "personal data"},
    {"pattern": "*backup*",    "level": 1, "description": "backup files"},
    {"pattern": "*final*",     "level": 1, "description": "final versions"},
]

_DEFAULT_EXCLUDE_PATTERNS = [
    "thumbs.db", ".ds_store", "desktop.ini",
    "*.tmp", "*.log", "*.bak",
]


def _rule_from_dict(d: dict) -> ProtectionRule:
    return ProtectionRule(
        pattern=d["pattern"],
        level=ProtectionLevel(d.get("level", 1)),
        description=d.get("description", ""),
    )


def _rule_to_dict(r: ProtectionRule) -> dict:
    return {"pattern": r.pattern, "level": r.level.value, "description": r.description}


def _profile_from_dict(d: dict) -> SyncProfile:
    return SyncProfile(
        name=d.get("name", "default"),
        src=d.get("src", ""),
        dst=d.get("dst", ""),
        include_patterns=d.get("include_patterns", []),
        exclude_patterns=d.get("exclude_patterns", list(_DEFAULT_EXCLUDE_PATTERNS)),
        protection_rules=[_rule_from_dict(r) for r in d.get("protection_rules", _DEFAULT_PROTECTION_RULES)],
        use_hash=d.get("use_hash", True),
        hash_algo=HashAlgo(d.get("hash_algo", "sha256")),
        delete_mode=d.get("delete_mode", False),
        backup_limit_mb=d.get("backup_limit_mb", 500),
        max_workers=d.get("max_workers", 6),
        ignore_hidden=d.get("ignore_hidden", True),
    )


def _profile_to_dict(p: SyncProfile) -> dict:
    return {
        "name": p.name,
        "src": p.src,
        "dst": p.dst,
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
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    raw = {name: _profile_to_dict(p) for name, p in profiles.items()}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)


def _make_default_profile() -> SyncProfile:
    import os
    if os.name == "nt":
        src = r"E:\IT\Programming\2_tools__utilities__scripts\Scripts\Flash\TEST_SOURCE"
        dst = r"E:\IT\Backup\Flash"
    else:
        src = "/media/flash"
        dst = str(Path.home() / "Backup" / "Flash")
    return _profile_from_dict({
        "name": "default",
        "src": src,
        "dst": dst,
    })


# ─────────────────────────────────────────────
# Сохранение отчётов
# ─────────────────────────────────────────────

def save_report(report: SyncReport, out_path: Optional[Path] = None) -> Path:
    if out_path is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out_path = Path.home() / ".flashsync" / "reports" / f"report_{ts}.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    from domain.models import ActionType
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(f"FlashSync Report — {report.started_at:%Y-%m-%d %H:%M:%S}\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Длительность: {report.duration_seconds:.1f}с\n")
        f.write(f"Скопировано байт: {report.bytes_copied:,}\n")
        f.write(f"В backup:        {report.bytes_backed_up:,}\n\n")
        f.write("Действия:\n")
        for action, count in report.stats.items():
            f.write(f"  {action}: {count}\n")
        if report.errors:
            f.write("\nОшибки:\n")
            for e in report.errors:
                f.write(f"  {e}\n")
    return out_path
