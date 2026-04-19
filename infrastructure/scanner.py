"""
infrastructure/scanner.py
"""
from __future__ import annotations
import fnmatch
import os
from pathlib import Path
from typing import Callable, Optional

from domain.models import FileInfo, HashAlgo, _classify
from infrastructure.hasher import hash_file

_SYSTEM_FILES = {
    "thumbs.db", ".ds_store", "desktop.ini", ".spotlight-v100",
    ".trashes", "$recycle.bin", ".fseventsd",
}


def _should_ignore(path: Path, ignore_hidden: bool = True) -> bool:
    name = path.name.lower()
    if name in _SYSTEM_FILES:
        return True
    if ignore_hidden and name.startswith("."):
        return True
    return False


def _match_pattern(rel_path: Path, pattern: str) -> bool:
    """
    Кроссплатформенный glob-матчинг.
    *.jpg  — матчит имя файла в любой папке
    temp/* — матчит полный относительный путь (через as_posix)
    """
    pat = pattern.lower()
    posix = rel_path.as_posix().lower()  # всегда прямые слеши, даже на Windows
    if "/" not in pat:
        # паттерн без слеша → сравниваем только с именем файла
        return fnmatch.fnmatch(rel_path.name.lower(), pat)
    # паттерн со слешем → сравниваем с полным относительным путём
    return fnmatch.fnmatch(posix, pat)


def scan_directory(
    root: Path,
    include_patterns: Optional[list[str]] = None,
    exclude_patterns: Optional[list[str]] = None,
    use_hash: bool = False,
    hash_algo: HashAlgo = HashAlgo.SHA256,
    ignore_hidden: bool = True,
    progress_cb: Optional[Callable[[Path], None]] = None,
) -> dict[Path, FileInfo]:
    """Сканирует директорию и возвращает {rel_path: FileInfo}."""
    if not root.exists():
        return {}
    if not root.is_dir():
        return {}

    result: dict[Path, FileInfo] = {}

    for dirpath, dirnames, filenames in os.walk(root):
        if ignore_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for filename in filenames:
            full_path = Path(dirpath) / filename
            rel_path = full_path.relative_to(root)

            if _should_ignore(full_path, ignore_hidden):
                continue
            if include_patterns and not any(_match_pattern(rel_path, p) for p in include_patterns):
                continue
            if exclude_patterns and any(_match_pattern(rel_path, p) for p in exclude_patterns):
                continue

            try:
                stat = full_path.stat()
            except OSError:
                continue

            file_hash: Optional[str] = None
            if use_hash:
                file_hash = hash_file(full_path, hash_algo.value)
                # None = ошибка чтения, пропускаем файл
                if file_hash is None:
                    continue

            result[rel_path] = FileInfo(
                path=full_path,
                rel_path=rel_path,
                size=stat.st_size,
                mtime=stat.st_mtime,
                hash=file_hash,
            )

            if progress_cb:
                progress_cb(full_path)

    return result


def get_directory_stats(root: Path) -> dict:
    """Быстрая статистика без хранения FileInfo."""
    total_files = 0
    total_size = 0
    extensions: dict[str, int] = {}
    categories: dict[str, int] = {}

    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            fp = Path(dirpath) / fn
            if _should_ignore(fp):
                continue
            try:
                sz = fp.stat().st_size
            except OSError:
                continue
            total_files += 1
            total_size += sz
            ext = fp.suffix.lower()
            extensions[ext] = extensions.get(ext, 0) + 1
            cat = _classify(ext)
            categories[cat] = categories.get(cat, 0) + 1

    return {
        "total_files": total_files,
        "total_size": total_size,
        "extensions": dict(sorted(extensions.items(), key=lambda x: -x[1])[:20]),
        "categories": categories,
    }
