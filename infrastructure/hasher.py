"""
infrastructure/hasher.py — Утилиты хеширования и верификации.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Optional


def hash_file(path: Path, algo: str = "sha256", block: int = 65536) -> str:
    h = hashlib.new(algo)
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(block), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def verify_copy(src: Path, dst: Path) -> bool:
    """Проверяет, что копия идентична оригиналу (по SHA-256)."""
    if not dst.exists():
        return False
    return hash_file(src) == hash_file(dst)


def quick_diff(src: Path, dst: Path) -> bool:
    """Быстрое сравнение по размеру и mtime (без хеша)."""
    try:
        ss = src.stat()
        ds = dst.stat()
        return ss.st_size == ds.st_size and abs(ss.st_mtime - ds.st_mtime) < 2.0
    except OSError:
        return False
