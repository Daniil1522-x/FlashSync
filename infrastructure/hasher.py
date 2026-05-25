from __future__ import annotations
import asyncio
import hashlib
from pathlib import Path
from typing import Optional

MTIME_TOLERANCE = 2.0

def hash_file(path: Path, algo: str = "sha256", block_size: int = 1024 * 1024) -> Optional[str]:
    if algo not in hashlib.algorithms_available:
        raise ValueError(f"Unknown algo: {algo}")
    h = hashlib.new(algo)
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(block_size), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None

async def hash_file_async(path: Path, algo: str = "sha256") -> Optional[str]:
    return await asyncio.to_thread(hash_file, path, algo)

def verify_copy(src: Path, dst: Path, algo: str = "sha256") -> bool:
    if not src.exists() or not dst.exists():
        return False
    try:
        if src.stat().st_size != dst.stat().st_size:
            return False
    except OSError:
        return False
    h_src = hash_file(src, algo)
    h_dst = hash_file(dst, algo)
    if h_src is None or h_dst is None:
        return False
    return h_src == h_dst

def quick_diff(src: Path, dst: Path) -> bool:
    try:
        ss = src.stat()
        ds = dst.stat()
        return ss.st_size == ds.st_size and abs(ss.st_mtime - ds.st_mtime) < MTIME_TOLERANCE
    except OSError:
        return False