"""
domain/models.py
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional


class ActionType(Enum):
    COPY_NEW       = "copy_new"
    COPY_UPDATE    = "copy_update"
    DELETE         = "delete"
    SKIP_EQUAL     = "skip_equal"
    SKIP_PROTECTED = "skip_protected"


class HashAlgo(Enum):
    SHA256 = "sha256"
    MD5    = "md5"


class ProtectionLevel(Enum):
    NONE   = 0
    SINGLE = 1
    DOUBLE = 2


@dataclass
class FileInfo:
    path: Path
    rel_path: Path
    size: int
    mtime: float
    hash: Optional[str] = None
    extension: str = ""
    category: str = "other"

    def __post_init__(self):
        if not self.extension:
            self.extension = self.path.suffix.lower()
        if not self.category or self.category == "other":
            self.category = _classify(self.extension)

    @property
    def mtime_dt(self) -> datetime:
        return datetime.fromtimestamp(self.mtime)

    def is_same_as(self, other: "FileInfo", use_hash: bool = True) -> bool:
        if use_hash and self.hash and other.hash:
            return self.hash == other.hash
        return self.size == other.size and abs(self.mtime - other.mtime) < 2.0


@dataclass
class SyncAction:
    action: ActionType
    src_file: Optional[FileInfo] = None
    dst_file: Optional[FileInfo] = None
    reason: str = ""
    protection_level: ProtectionLevel = ProtectionLevel.NONE
    confirmed: int = 0

    @property
    def rel_path(self) -> Path:
        f = self.src_file or self.dst_file
        assert f is not None
        return f.rel_path

    @property
    def size_bytes(self) -> int:
        if self.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE):
            return self.src_file.size if self.src_file else 0
        if self.action == ActionType.DELETE:
            return self.dst_file.size if self.dst_file else 0
        return 0

    def needs_confirmation(self) -> bool:
        return self.protection_level != ProtectionLevel.NONE

    def confirm(self) -> bool:
        self.confirmed += 1
        return self.confirmed >= self.protection_level.value


@dataclass
class SyncReport:
    started_at: datetime = field(default_factory=datetime.now)
    finished_at: Optional[datetime] = None
    actions_planned: list[SyncAction] = field(default_factory=list)
    actions_done: list[SyncAction] = field(default_factory=list)
    actions_skipped: list[SyncAction] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    bytes_copied: int = 0
    bytes_backed_up: int = 0

    @property
    def duration_seconds(self) -> float:
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return 0.0

    @property
    def stats(self) -> dict[str, int]:
        counter: dict[str, int] = {}
        for a in self.actions_done:
            key = a.action.value
            counter[key] = counter.get(key, 0) + 1
        return counter


@dataclass
class ProtectionRule:
    pattern: str
    level: ProtectionLevel = ProtectionLevel.SINGLE
    description: str = ""

    def matches(self, rel_path: Path) -> bool:
        import fnmatch
        name = str(rel_path).lower()
        pat = self.pattern.lower()
        if "*" in pat or "?" in pat or "[" in pat:
            return fnmatch.fnmatch(name, pat)
        return pat in name


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
    delete_mode: bool = False
    backup_limit_mb: int = 500
    max_workers: int = 4
    ignore_hidden: bool = True


_EXT_MAP = {
    "image":    {".jpg", ".jpeg", ".png", ".heic", ".raw", ".gif", ".bmp", ".webp", ".tiff"},
    "video":    {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".m4v"},
    "audio":    {".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a"},
    "document": {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".txt", ".md", ".rtf"},
    "archive":  {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2"},
    "code":     {".py", ".js", ".ts", ".java", ".cpp", ".c", ".h", ".cs", ".go", ".rs"},
    "temp":     {".tmp", ".log", ".bak", ".old", ".cache", ".swp"},
}

def _classify(ext: str) -> str:
    ext = ext.lower()
    for cat, exts in _EXT_MAP.items():
        if ext in exts:
            return cat
    return "other"
