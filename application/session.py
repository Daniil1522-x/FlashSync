"""
application/session.py — Сохранение и восстановление сессии.

Хранит последнее сканирование (план + деревья) чтобы при перезапуске
не нужно было сканировать заново. Также хранит активные фильтры и
статистику по типам файлов в плане.

Файл: ~/.flashsync/session_<profile>.json
"""
from __future__ import annotations

import json
import dataclasses
from datetime import datetime
from pathlib import Path
from typing import Optional

from domain.models import (
    SyncAction, ActionType, ProtectionLevel,
    FileInfo, SyncProfile, HashAlgo,
)


SESSION_VERSION = 2  # инкремент при изменении формата


class SessionManager:
    def __init__(self, profile_name: str):
        self._profile_name = profile_name

    def _path(self) -> Path:
        from infrastructure.storage import CONFIG_DIR
        return CONFIG_DIR / f"session_{self._profile_name}.json"

    # ── Сохранение ────────────────────────────────────────────────────────────

    def save(
        self,
        plan: list[SyncAction],
        src_path: str,
        dst_path: str,
        scan_duration: float = 0.0,
    ) -> None:
        """Сохраняет план сканирования на диск."""
        try:
            data = {
                "version": SESSION_VERSION,
                "saved_at": datetime.now().isoformat(),
                "profile": self._profile_name,
                "src_path": src_path,
                "dst_path": dst_path,
                "scan_duration": scan_duration,
                "plan": [self._action_to_dict(a) for a in plan],
            }
            p = self._path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
        except Exception:
            pass  # сессия не критична — молча игнорируем

    # ── Загрузка ──────────────────────────────────────────────────────────────

    def load(self, src_path: str, dst_path: str) -> Optional[tuple[list[SyncAction], str]]:
        """
        Загружает сохранённую сессию.
        Возвращает (plan, saved_at_str) или None если сессии нет
        или пути изменились.
        """
        try:
            p = self._path()
            if not p.exists():
                return None
            with open(p, encoding="utf-8") as f:
                data = json.load(f)

            if data.get("version") != SESSION_VERSION:
                return None
            if data.get("src_path") != src_path:
                return None
            if data.get("dst_path") != dst_path:
                return None

            plan = [self._action_from_dict(d) for d in data.get("plan", [])]
            plan = [a for a in plan if a is not None]

            saved_at = data.get("saved_at", "")
            try:
                dt = datetime.fromisoformat(saved_at)
                saved_at = dt.strftime("%d.%m.%Y %H:%M")
            except Exception:
                pass

            return plan, saved_at
        except Exception:
            return None

    def clear(self) -> None:
        try:
            self._path().unlink(missing_ok=True)
        except Exception:
            pass

    def exists(self) -> bool:
        return self._path().exists()

    # ── Сериализация SyncAction ───────────────────────────────────────────────

    def _fi_to_dict(self, fi: Optional[FileInfo]) -> Optional[dict]:
        if fi is None:
            return None
        return {
            "path": str(fi.path),
            "rel_path": fi.rel_path.as_posix(),
            "size": fi.size,
            "mtime": fi.mtime,
            "hash": fi.hash,
            "extension": fi.extension,
            "category": fi.category,
        }

    def _fi_from_dict(self, d: Optional[dict]) -> Optional[FileInfo]:
        if d is None:
            return None
        try:
            return FileInfo(
                path=Path(d["path"]),
                rel_path=Path(d["rel_path"]),
                size=d["size"],
                mtime=d["mtime"],
                hash=d.get("hash"),
                extension=d.get("extension", ""),
                category=d.get("category", "other"),
            )
        except Exception:
            return None

    def _action_to_dict(self, a: SyncAction) -> dict:
        return {
            "action": a.action.value,
            "src_file": self._fi_to_dict(a.src_file),
            "dst_file": self._fi_to_dict(a.dst_file),
            "reason": a.reason,
            "protection_level": a.protection_level.value,
            "confirmed": a.confirmed,
        }

    def _action_from_dict(self, d: dict) -> Optional[SyncAction]:
        try:
            return SyncAction(
                action=ActionType(d["action"]),
                src_file=self._fi_from_dict(d.get("src_file")),
                dst_file=self._fi_from_dict(d.get("dst_file")),
                reason=d.get("reason", ""),
                protection_level=ProtectionLevel(d.get("protection_level", 0)),
                confirmed=d.get("confirmed", 0),
            )
        except Exception:
            return None


# ── Статистика плана по типам файлов ─────────────────────────────────────────

def plan_stats_by_category(plan: list[SyncAction]) -> dict[str, dict]:
    """
    Возвращает статистику плана по категориям файлов.
    {
      "image": {"count": 120, "bytes": 524288000, "actions": {"copy_new": 80, "copy_update": 40}},
      "video": {...},
      ...
    }
    """
    result: dict[str, dict] = {}

    for action in plan:
        if action.action == ActionType.SKIP_EQUAL:
            continue
        fi = action.src_file or action.dst_file
        cat = fi.category if fi else "other"

        if cat not in result:
            result[cat] = {"count": 0, "bytes": 0, "actions": {}}

        result[cat]["count"] += 1
        result[cat]["bytes"] += action.size_bytes
        key = action.action.value
        result[cat]["actions"][key] = result[cat]["actions"].get(key, 0) + 1

    return dict(sorted(result.items(), key=lambda x: -x[1]["bytes"]))


def format_plan_stats(stats: dict[str, dict]) -> list[str]:
    """Форматирует статистику в строки для отображения."""
    lines = []
    cat_names = {
        "image": "Фото", "video": "Видео", "audio": "Аудио",
        "document": "Документы", "archive": "Архивы",
        "code": "Код", "temp": "Временные", "other": "Прочее",
    }
    for cat, info in stats.items():
        name = cat_names.get(cat, cat)
        mb = info["bytes"] / (1024 * 1024)
        actions = info["actions"]
        detail = []
        if actions.get("copy_new"):
            detail.append(f"+{actions['copy_new']} нов.")
        if actions.get("copy_update"):
            detail.append(f"↻{actions['copy_update']} изм.")
        if actions.get("delete"):
            detail.append(f"⊘{actions['delete']} bkp")
        detail_str = "  ".join(detail)
        lines.append(f"  {name:<12} {info['count']:>4}  {mb:>6.1f}MB  {detail_str}")
    return lines