"""
application/plan_overrides.py — Хранилище ручных переопределений плана.

Пользователь может вручную изменить действие для файла в FileManager.
Эти изменения должны ПЕРЕЖИВАТЬ повторное сканирование.

Логика применения:
  - Если файл изменился на диске (другой хеш/размер) — переопределение СБРАСЫВАЕТСЯ
  - Если файл тот же — переопределение ПРИМЕНЯЕТСЯ поверх свежего плана

Персистентность: ~/.flashsync/overrides_<profile>.json
"""
from __future__ import annotations

import json
import logging
import dataclasses
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

from domain.models import ActionType, ProtectionLevel, SyncAction

_logger = logging.getLogger("flashsync")


@dataclass
class Override:
    rel_path: str
    action: str
    protection_level: int
    reason: str
    src_hash: Optional[str] = None
    src_size: int = 0
    src_mtime: float = 0.0
    dst_hash: Optional[str] = None
    dst_size: int = 0
    dst_mtime: float = 0.0


class PlanOverrides:
    def __init__(self, profile_name: str):
        self._profile_name = profile_name
        self._overrides: dict[str, Override] = {}
        self._load()

    def set(self, action: SyncAction) -> None:
        rel = action.rel_path.as_posix()
        self._overrides[rel] = Override(
            rel_path=rel, action=action.action.value,
            protection_level=action.protection_level.value, reason=action.reason,
            src_hash=action.src_file.hash if action.src_file else None,
            src_size=action.src_file.size if action.src_file else 0,
            src_mtime=action.src_file.mtime if action.src_file else 0.0,
            dst_hash=action.dst_file.hash if action.dst_file else None,
            dst_size=action.dst_file.size if action.dst_file else 0,
            dst_mtime=action.dst_file.mtime if action.dst_file else 0.0,
        )
        self._save()

    def set_many(self, actions: list[SyncAction]) -> None:
        for a in actions:
            rel = a.rel_path.as_posix()
            self._overrides[rel] = Override(
                rel_path=rel, action=a.action.value,
                protection_level=a.protection_level.value, reason=a.reason,
                src_hash=a.src_file.hash if a.src_file else None,
                src_size=a.src_file.size if a.src_file else 0,
                src_mtime=a.src_file.mtime if a.src_file else 0.0,
                dst_hash=a.dst_file.hash if a.dst_file else None,
                dst_size=a.dst_file.size if a.dst_file else 0,
                dst_mtime=a.dst_file.mtime if a.dst_file else 0.0,
            )
        self._save()

    def remove(self, rel_path_posix: str) -> None:
        self._overrides.pop(rel_path_posix, None)
        self._save()

    def clear(self) -> None:
        self._overrides.clear()
        self._save()

    def count(self) -> int:
        return len(self._overrides)

    def apply(self, plan: list[SyncAction]) -> tuple[list[SyncAction], int, int]:
        """
        Применяет переопределения к свежему плану.
        Возвращает (новый_план, applied_count, stale_count).
        """
        applied = stale = 0
        result = []

        for action in plan:
            rel = action.rel_path.as_posix()
            ov = self._overrides.get(rel)
            if ov is None:
                result.append(action)
                continue

            if self._is_stale(ov, action):
                stale += 1
                self._overrides.pop(rel, None)
                result.append(action)
                continue

            try:
                new_action = ActionType(ov.action)
                new_prot = ProtectionLevel(ov.protection_level)
            except ValueError:
                result.append(action)
                continue

            result.append(dataclasses.replace(
                action, action=new_action, protection_level=new_prot,
                reason=ov.reason, confirmed=0,
            ))
            applied += 1

        if stale > 0:
            self._save()

        return result, applied, stale

    def _is_stale(self, ov: Override, action: SyncAction) -> bool:
        """
        Override устарел если файл реально изменился на диске.

        Если хеш есть на обеих сторонах (тогда и тогда только) — сравниваем
        по хешу, это надёжно. Иначе сравниваем и по размеру, и по mtime:
        прежняя версия проверяла в этом случае только размер, из-за чего
        override переживал смену содержимого файла того же размера, если
        он был сохранён без хеша (use_hash=False), а пересканирован с
        хешем включённым позже — staleness не обнаруживался. mtime почти
        всегда меняется при правке файла даже без изменения размера.
        """
        MTIME_TOLERANCE = 2.0

        if ov.src_hash and action.src_file and action.src_file.hash:
            if ov.src_hash != action.src_file.hash:
                return True
        elif action.src_file:
            if ov.src_size > 0 and ov.src_size != action.src_file.size:
                return True
            if ov.src_mtime > 0 and abs(ov.src_mtime - action.src_file.mtime) > MTIME_TOLERANCE:
                return True

        if ov.dst_hash and action.dst_file and action.dst_file.hash:
            if ov.dst_hash != action.dst_file.hash:
                return True
        elif action.dst_file:
            if ov.dst_size > 0 and ov.dst_size != action.dst_file.size:
                return True
            if ov.dst_mtime > 0 and abs(ov.dst_mtime - action.dst_file.mtime) > MTIME_TOLERANCE:
                return True

        return False

    def _path(self) -> Path:
        from infrastructure.storage import CONFIG_DIR
        return CONFIG_DIR / f"overrides_{self._profile_name}.json"

    def _save(self) -> None:
        try:
            self._path().parent.mkdir(parents=True, exist_ok=True)
            with open(self._path(), "w", encoding="utf-8") as f:
                json.dump({k: asdict(v) for k, v in self._overrides.items()}, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _logger.warning(f"Не удалось сохранить ручные изменения для профиля {self._profile_name}: {e}")

    def _load(self) -> None:
        try:
            if not self._path().exists():
                return
            with open(self._path(), encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    self._overrides[k] = Override(**v)
        except Exception as e:
            _logger.warning(f"Не удалось загрузить ручные изменения для профиля {self._profile_name}: {e}")
            self._overrides = {}
