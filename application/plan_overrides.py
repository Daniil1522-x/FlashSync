from __future__ import annotations
import json
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from domain.models import ActionType, ProtectionLevel, SyncAction

@dataclass
class Override:
    rel_path: str
    action: str
    protection_level: int
    reason: str
    src_hash: Optional[str] = None
    src_size: int = 0
    dst_hash: Optional[str] = None
    dst_size: int = 0

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
            dst_hash=action.dst_file.hash if action.dst_file else None,
            dst_size=action.dst_file.size if action.dst_file else 0,
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
                dst_hash=a.dst_file.hash if a.dst_file else None,
                dst_size=a.dst_file.size if a.dst_file else 0,
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
        import dataclasses
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
        if ov.src_hash and action.src_file and action.src_file.hash:
            if ov.src_hash != action.src_file.hash:
                return True
        elif ov.src_size > 0 and action.src_file:
            if ov.src_size != action.src_file.size:
                return True
        if ov.dst_hash and action.dst_file and action.dst_file.hash:
            if ov.dst_hash != action.dst_file.hash:
                return True
        elif ov.dst_size > 0 and action.dst_file:
            if ov.dst_size != action.dst_file.size:
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
        except Exception:
            pass

    def _load(self) -> None:
        try:
            if not self._path().exists():
                return
            with open(self._path(), encoding="utf-8") as f:
                for k, v in json.load(f).items():
                    self._overrides[k] = Override(**v)
        except Exception:
            self._overrides = {}