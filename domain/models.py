"""
domain/models.py — Доменные сущности FlashSync.
Чистые dataclass без зависимостей от инфраструктуры.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path
from datetime import datetime
from typing import Optional


# ─────────────────────────────────────────────
# Статусы и действия
# ─────────────────────────────────────────────

class ActionType(Enum):
    COPY_NEW      = "copy_new"       # файл только в src → скопировать
    COPY_UPDATE   = "copy_update"    # файл изменился → заменить (старый → backup)
    DELETE        = "delete"         # файл только в dst → в backup
    SKIP_EQUAL    = "skip_equal"     # файлы идентичны → ничего
    SKIP_PROTECTED= "skip_protected" # защищённый → пропустить


class HashAlgo(Enum):
    SHA256 = "sha256"
    MD5    = "md5"
    XXHASH = "xxhash"  # быстрый, если установлен


class ProtectionLevel(Enum):
    NONE    = 0   # без защиты
    SINGLE  = 1   # требует одного подтверждения
    DOUBLE  = 2   # требует двух подтверждений (особо важные)


# ─────────────────────────────────────────────
# Файловый объект
# ─────────────────────────────────────────────

@dataclass
class FileInfo:
    """Снимок файла на момент сканирования."""
    path: Path                  # абсолютный путь
    rel_path: Path              # относительный от корня (src или dst)
    size: int                   # байты
    mtime: float                # unix timestamp
    hash: Optional[str] = None  # SHA-256 (lazy)
    extension: str = ""
    category: str = "other"

    def __post_init__(self):
        self.extension = self.path.suffix.lower()
        self.category = _classify(self.extension)

    @property
    def mtime_dt(self) -> datetime:
        return datetime.fromtimestamp(self.mtime)

    def is_same_as(self, other: "FileInfo", use_hash: bool = True) -> bool:
        if use_hash and self.hash and other.hash:
            return self.hash == other.hash
        return self.size == other.size and abs(self.mtime - other.mtime) < 2.0


# ─────────────────────────────────────────────
# Действие синхронизации
# ─────────────────────────────────────────────

@dataclass
class SyncAction:
    action: ActionType
    src_file: Optional[FileInfo]  # None для DELETE
    dst_file: Optional[FileInfo]  # None для COPY_NEW
    reason: str = ""
    protection_level: ProtectionLevel = ProtectionLevel.NONE
    confirmed: int = 0            # счётчик подтверждений

    @property
    def rel_path(self) -> Path:
        f = self.src_file or self.dst_file
        return f.rel_path  # type: ignore

    @property
    def size_bytes(self) -> int:
        if self.src_file:
            return self.src_file.size
        return 0

    def needs_confirmation(self) -> bool:
        return self.protection_level != ProtectionLevel.NONE

    def confirm(self) -> bool:
        """Добавляет подтверждение. Возвращает True если достаточно."""
        self.confirmed += 1
        return self.confirmed >= self.protection_level.value


# ─────────────────────────────────────────────
# Результат синхронизации
# ─────────────────────────────────────────────

@dataclass
class SyncReport:
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None
    actions_planned: list[SyncAction] = field(default_factory=list)
    actions_done: list[SyncAction] = field(default_factory=list)
    actions_skipped: list[SyncAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    bytes_copied: int = 0
    bytes_backed_up: int = 0

    @property
    def duration_seconds(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return 0.0

    @property
    def stats(self) -> dict:
        by_type: dict[str, int] = {}
        for a in self.actions_done:
            key = a.action.value
            by_type[key] = by_type.get(key, 0) + 1
        return by_type


# ─────────────────────────────────────────────
# Правило защиты
# ─────────────────────────────────────────────

@dataclass
class ProtectionRule:
    pattern: str                             # glob или keyword
    level: ProtectionLevel = ProtectionLevel.SINGLE
    description: str = ""

    def matches(self, rel_path: Path) -> bool:
        name = str(rel_path).lower()
        pat = self.pattern.lower()
        if "*" in pat or "?" in pat:
            return rel_path.match(pat)
        return pat in name


# ─────────────────────────────────────────────
# Профиль синхронизации
# ─────────────────────────────────────────────

@dataclass
class SyncProfile:
    name: str
    src: str
    dst: str
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    protection_rules: list[ProtectionRule] = field(default_factory=list)
    use_hash: bool = True
    hash_algo: HashAlgo = HashAlgo.SHA256
    delete_mode: bool = False         # перемещать в backup файлы, которых нет в src
    backup_limit_mb: int = 500        # лимит папки backup
    max_workers: int = 6
    ignore_hidden: bool = True        # игнорировать .DS_Store, Thumbs.db и т.п.


# ─────────────────────────────────────────────
# Вспомогательная классификация файлов
# ─────────────────────────────────────────────

_EXT_MAP = {
    "image":    {".jpg", ".jpeg", ".png", ".heic", ".raw", ".gif", ".bmp", ".webp", ".tiff"},
    "video":    {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v"},
    "audio":    {".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a"},
    "document": {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".md", ".rtf"},
    "archive":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "code":     {".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".cs", ".go", ".rs"},
    "temp":     {".tmp", ".log", ".bak", ".old", ".backup", ".cache"},
}

def _classify(ext: str) -> str:
    for cat, exts in _EXT_MAP.items():
        if ext in exts:
            return cat
    return "other"
