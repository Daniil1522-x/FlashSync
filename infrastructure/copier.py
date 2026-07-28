"""
infrastructure/copier.py — Эффективное копирование файлов.

Три режима:
  - Маленькие файлы (<4 MB): shutil.copy2 через to_thread
  - Большие файлы (>=4 MB): чанковое копирование с прогрессом по байтам
  - Верификация: SHA-256 после копирования (опционально)
"""
from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import Callable, Optional

SMALL_FILE_THRESHOLD = 4 * 1024 * 1024   # 4 MB
CHUNK_SIZE = 1024 * 1024                  # 1 MB


async def copy_file(
    src: Path,
    dst: Path,
    verify: bool = False,
    progress_cb: Optional[Callable[[int, int], None]] = None,
) -> bool:
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = src.stat().st_size
    except OSError:
        return False

    if size < SMALL_FILE_THRESHOLD or progress_cb is None:
        try:
            await asyncio.to_thread(_copy_small, src, dst)
        except Exception:
            return False
    else:
        try:
            await asyncio.to_thread(_copy_chunked, src, dst, size, progress_cb)
        except Exception:
            return False

    if verify:
        verified = await asyncio.to_thread(_verify_sha256, src, dst)
        if not verified:
            # Файл скопировался, но содержимое не совпало с источником —
            # оставлять битый файл на диске опаснее чем отсутствие файла:
            # при следующей синхронизации без хеша (use_hash=False) битый
            # файл может пройти проверку по размеру+дате и остаться навсегда.
            try:
                await asyncio.to_thread(dst.unlink, missing_ok=True)
            except OSError:
                pass
        return verified
    return True


def _copy_small(src: Path, dst: Path) -> None:
    shutil.copy2(str(src), str(dst))


def _copy_chunked(src: Path, dst: Path, total: int, progress_cb: Callable[[int, int], None]) -> None:
    done = 0
    with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
        while True:
            chunk = fsrc.read(CHUNK_SIZE)
            if not chunk:
                break
            fdst.write(chunk)
            done += len(chunk)
            progress_cb(done, total)
    try:
        shutil.copystat(str(src), str(dst))
    except OSError:
        pass


def _verify_sha256(src: Path, dst: Path) -> bool:
    try:
        h_src = _hash(src)
        h_dst = _hash(dst)
        return h_src == h_dst and h_src is not None
    except Exception:
        return False


def _hash(path: Path) -> Optional[str]:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(CHUNK_SIZE), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


async def copy_batch_small(pairs: list[tuple[Path, Path]]) -> list[tuple[Path, Path, bool]]:
    """Копирует пакет маленьких файлов в одном потоке — меньше overhead на создание потоков."""
    return await asyncio.to_thread(_batch_worker, pairs)


def _batch_worker(pairs: list[tuple[Path, Path]]) -> list[tuple[Path, Path, bool]]:
    results = []
    for src, dst in pairs:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            results.append((src, dst, True))
        except Exception:
            results.append((src, dst, False))
    return results
