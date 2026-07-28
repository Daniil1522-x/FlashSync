"""
infrastructure/scanner.py — Сканирование директорий с поддержкой больших объёмов.

Ключевые решения для 18+ GB:
  1. Генератор вместо списка — не грузит все FileInfo в RAM сразу
  2. Хеширование опционально и постепенное — не блокирует UI
  3. Все исключения перехватываются — программа не падает от permission error
  4. progress_cb вызывается ДО хеширования — UI отзывчив всегда
"""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Callable, Iterator, Optional

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
    pat = pattern.lower()
    posix = rel_path.as_posix().lower()
    if "/" not in pat:
        return fnmatch.fnmatch(rel_path.name.lower(), pat)
    return fnmatch.fnmatch(posix, pat)


def scan_directory(
    root: Path,
    include_patterns: Optional[list[str]] = None,
    exclude_patterns: Optional[list[str]] = None,
    use_hash: bool = False,
    hash_algo: HashAlgo = HashAlgo.SHA256,
    ignore_hidden: bool = True,
    progress_cb: Optional[Callable[[Path], None]] = None,
    error_cb: Optional[Callable[[Path, Exception], None]] = None,
) -> dict[Path, FileInfo]:
    """
    Сканирует директорию. Возвращает {rel_path: FileInfo}.
    error_cb(path, exc) — вызывается при ошибке доступа к файлу.
    """
    if not root.exists() or not root.is_dir():
        return {}

    result: dict[Path, FileInfo] = {}

    for rel_path, full_path, stat in _walk_files(
        root, include_patterns, exclude_patterns, ignore_hidden, error_cb
    ):
        if progress_cb:
            try:
                progress_cb(full_path)
            except Exception:
                pass

        file_hash: Optional[str] = None
        if use_hash:
            try:
                file_hash = hash_file(full_path, hash_algo.value)
                if file_hash is None and error_cb:
                    # Хеширование не удалось (файл заблокирован/временная ошибка чтения),
                    # но файл НЕ исключаем из результата — иначе DiffEngine не увидит
                    # его вообще ни в src, ни в dst, и он молча не попадёт в план.
                    # Вместо этого включаем его с hash=None: FileInfo.is_same_as
                    # автоматически откатится на сравнение по размеру+дате.
                    error_cb(full_path, OSError(
                        f"не удалось вычислить хеш — сравнение по размеру+дате: {full_path.name}"
                    ))
            except Exception as e:
                if error_cb:
                    error_cb(full_path, e)
                file_hash = None

        try:
            result[rel_path] = FileInfo(
                path=full_path,
                rel_path=rel_path,
                size=stat.st_size,
                mtime=stat.st_mtime,
                hash=file_hash,
            )
        except Exception:
            continue

    return result


def _walk_files(
    root: Path,
    include_patterns: Optional[list[str]],
    exclude_patterns: Optional[list[str]],
    ignore_hidden: bool,
    error_cb: Optional[Callable],
) -> Iterator[tuple[Path, Path, os.stat_result]]:
    try:
        walk_iter = os.walk(root, onerror=lambda e: _handle_walk_error(e, error_cb))
    except Exception:
        return

    for dirpath, dirnames, filenames in walk_iter:
        if ignore_hidden:
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        dirnames[:] = [d for d in dirnames if not d.startswith(".flashsync_backup")]

        for filename in filenames:
            full_path = Path(dirpath) / filename
            try:
                rel_path = full_path.relative_to(root)
            except ValueError:
                continue

            if _should_ignore(full_path, ignore_hidden):
                continue
            if include_patterns and not any(_match_pattern(rel_path, p) for p in include_patterns):
                continue
            if exclude_patterns and any(_match_pattern(rel_path, p) for p in exclude_patterns):
                continue

            # Симлинки сознательно НЕ разыменовываем. Источник в этом приложении
            # часто — непроверенная флешка; символическая ссылка внутри неё может
            # указывать на произвольный путь на ПК пользователя (например на
            # ~/.ssh/id_rsa, замаскированный под "photo.jpg"). full_path.stat()
            # и shutil.copy2 по умолчанию следуют по симлинку и работают с ЦЕЛЬЮ,
            # а не с самой ссылкой — значит контент произвольного файла с ПК мог бы
            # попасть в backup-папку и затем, например, уйти в облако или другому
            # человеку при последующей передаче этой папки. Поэтому пропускаем
            # симлинки целиком и явно предупреждаем — это безопаснее чем угадывать,
            # legitimate он или нет.
            if full_path.is_symlink():
                if error_cb:
                    error_cb(full_path, OSError(
                        f"символическая ссылка пропущена из соображений безопасности: {full_path.name}"
                    ))
                continue

            try:
                stat = full_path.stat()
            except (OSError, PermissionError) as e:
                if error_cb:
                    error_cb(full_path, e)
                continue

            yield rel_path, full_path, stat


def _handle_walk_error(exc: OSError, error_cb: Optional[Callable]) -> None:
    if error_cb:
        try:
            error_cb(Path(exc.filename or "?"), exc)
        except Exception:
            pass


def get_directory_stats(root: Path) -> dict:
    """Быстрая статистика без FileInfo — только счётчики."""
    total_files = 0
    total_size = 0
    extensions: dict[str, int] = {}
    categories: dict[str, int] = {}

    def _walk_err(e):
        pass

    try:
        for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_err):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and not d.startswith(".flashsync")]
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
    except Exception:
        pass

    return {
        "total_files": total_files,
        "total_size": total_size,
        "extensions": dict(sorted(extensions.items(), key=lambda x: -x[1])[:20]),
        "categories": categories,
    }
