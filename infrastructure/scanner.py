"""
infrastructure/scanner.py — Сканирование директорий.
Собирает FileInfo для каждого файла, опционально вычисляет хеш.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterator, Callable, Optional

from domain.models import FileInfo, HashAlgo

# Системные файлы для игнорирования
_SYSTEM_FILES = {
    "thumbs.db", ".ds_store", "desktop.ini",
    ".spotlight-v100", ".trashes", "$recycle.bin",
    ".fseventsd", ".documentrevisions-v100",
}


def _should_ignore(path: Path, ignore_hidden: bool) -> bool:
    name = path.name.lower()
    if name in _SYSTEM_FILES:
        return True
    if ignore_hidden and name.startswith("."):
        return True
    return False


def compute_hash(path: Path, algo: HashAlgo = HashAlgo.SHA256,
                 block_size: int = 65536,
                 progress_cb: Optional[Callable[[int], None]] = None) -> str:
    """Вычисляет хеш файла. progress_cb вызывается с количеством прочитанных байт."""
    h = hashlib.new(algo.value)
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(block_size), b""):
                h.update(chunk)
                if progress_cb:
                    progress_cb(len(chunk))
    except (PermissionError, OSError):
        return ""
    return h.hexdigest()


def scan_directory(
    root: Path,
    include_patterns: list[str] | None = None,
    exclude_patterns: list[str] | None = None,
    use_hash: bool = False,
    hash_algo: HashAlgo = HashAlgo.SHA256,
    ignore_hidden: bool = True,
    progress_cb: Optional[Callable[[Path], None]] = None,
) -> dict[Path, FileInfo]:
    """
    Сканирует директорию и возвращает {rel_path: FileInfo}.
    """
    result: dict[Path, FileInfo] = {}

    for dirpath, dirnames, filenames in os.walk(root):
        # Фильтр скрытых директорий
        if ignore_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]

        for filename in filenames:
            full_path = Path(dirpath) / filename
            rel_path = full_path.relative_to(root)

            if _should_ignore(full_path, ignore_hidden):
                continue

            # Фильтр include
            if include_patterns:
                name = filename.lower()
                if not any(_glob_match(name, p) for p in include_patterns):
                    continue

            # Фильтр exclude
            if exclude_patterns:
                rel_str = str(rel_path).lower()
                if any(_glob_match(rel_str, p) for p in exclude_patterns):
                    continue

            try:
                stat = full_path.stat()
            except OSError:
                continue

            file_hash = None
            if use_hash:
                file_hash = compute_hash(full_path, hash_algo)

            fi = FileInfo(
                path=full_path,
                rel_path=rel_path,
                size=stat.st_size,
                mtime=stat.st_mtime,
                hash=file_hash,
            )

            result[rel_path] = fi

            if progress_cb:
                progress_cb(full_path)

    return result


def _glob_match(name: str, pattern: str) -> bool:
    """Простое glob-совпадение: * любое количество символов, ? один символ."""
    import fnmatch
    return fnmatch.fnmatch(name, pattern.lower())


def scan_tree_structure(root: Path, ignore_hidden: bool = True) -> dict:
    """
    Возвращает дерево структуры директории для отображения.
    {
      "name": "root",
      "path": Path,
      "is_dir": True,
      "children": [...],
      "file_count": int,
      "total_size": int,
      "extensions": {".jpg": 5, ...}
    }
    """
    def _walk(path: Path) -> dict:
        node = {
            "name": path.name or str(path),
            "path": path,
            "is_dir": path.is_dir(),
            "children": [],
            "file_count": 0,
            "total_size": 0,
            "extensions": {},
        }
        if path.is_dir():
            try:
                entries = sorted(path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
            except PermissionError:
                return node
            for entry in entries:
                if _should_ignore(entry, ignore_hidden):
                    continue
                child = _walk(entry)
                node["children"].append(child)
                node["file_count"] += child["file_count"] if child["is_dir"] else 1
                node["total_size"] += child["total_size"]
                for ext, cnt in child["extensions"].items():
                    node["extensions"][ext] = node["extensions"].get(ext, 0) + cnt
        else:
            try:
                node["total_size"] = path.stat().st_size
                ext = path.suffix.lower()
                node["extensions"][ext] = 1
            except OSError:
                pass
        return node

    return _walk(root)


def get_directory_stats(root: Path) -> dict:
    """Быстрая статистика по директории без построения дерева."""
    total_files = 0
    total_size = 0
    extensions: dict[str, int] = {}
    categories: dict[str, int] = {}

    from domain.models import _classify
    for dirpath, _, filenames in os.walk(root):
        for fn in filenames:
            fp = Path(dirpath) / fn
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
