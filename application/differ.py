"""
application/differ.py — Движок сравнения двух каталогов.
Принимает два словаря {rel_path: FileInfo} и генерирует список SyncAction.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from domain.models import (
    FileInfo, SyncAction, ActionType,
    ProtectionLevel, ProtectionRule, SyncProfile,
)


class DiffEngine:
    """
    Сравнивает src_tree и dst_tree, возвращает план синхронизации.
    """

    def __init__(self, profile: SyncProfile):
        self.profile = profile
        self._protection_rules = profile.protection_rules

    def compute_plan(
        self,
        src_tree: dict[Path, FileInfo],
        dst_tree: dict[Path, FileInfo],
    ) -> list[SyncAction]:
        actions: list[SyncAction] = []

        src_keys = set(src_tree.keys())
        dst_keys = set(dst_tree.keys())

        # ── 1. Файлы в src ──────────────────────────────────────────────
        for rel in sorted(src_keys):
            src_f = src_tree[rel]
            protection = self._get_protection(rel)

            if rel not in dst_keys:
                # Нового файла нет в dst → скопировать
                actions.append(SyncAction(
                    action=ActionType.COPY_NEW,
                    src_file=src_f,
                    dst_file=None,
                    reason="only in source",
                    protection_level=ProtectionLevel.NONE,
                ))
            else:
                dst_f = dst_tree[rel]
                if src_f.is_same_as(dst_f, use_hash=self.profile.use_hash):
                    # Идентичны → пропустить
                    actions.append(SyncAction(
                        action=ActionType.SKIP_EQUAL,
                        src_file=src_f,
                        dst_file=dst_f,
                        reason="files are identical",
                    ))
                else:
                    # Файл изменился
                    if protection != ProtectionLevel.NONE:
                        actions.append(SyncAction(
                            action=ActionType.SKIP_PROTECTED,
                            src_file=src_f,
                            dst_file=dst_f,
                            reason=f"protected ({protection.name})",
                            protection_level=protection,
                        ))
                    else:
                        actions.append(SyncAction(
                            action=ActionType.COPY_UPDATE,
                            src_file=src_f,
                            dst_file=dst_f,
                            reason=f"src newer/different (src={src_f.size}b, dst={dst_f.size}b)",
                        ))

        # ── 2. Файлы только в dst ────────────────────────────────────────
        if self.profile.delete_mode:
            for rel in sorted(dst_keys - src_keys):
                dst_f = dst_tree[rel]
                protection = self._get_protection(rel)
                if protection == ProtectionLevel.DOUBLE:
                    actions.append(SyncAction(
                        action=ActionType.SKIP_PROTECTED,
                        src_file=None,
                        dst_file=dst_f,
                        reason="double-protected, cannot delete",
                        protection_level=protection,
                    ))
                elif protection == ProtectionLevel.SINGLE:
                    actions.append(SyncAction(
                        action=ActionType.DELETE,
                        src_file=None,
                        dst_file=dst_f,
                        reason="only in dest (will move to backup)",
                        protection_level=ProtectionLevel.SINGLE,
                    ))
                else:
                    actions.append(SyncAction(
                        action=ActionType.DELETE,
                        src_file=None,
                        dst_file=dst_f,
                        reason="only in dest (will move to backup)",
                    ))

        return actions

    def _get_protection(self, rel_path: Path) -> ProtectionLevel:
        level = ProtectionLevel.NONE
        for rule in self._protection_rules:
            if rule.matches(rel_path):
                if rule.level.value > level.value:
                    level = rule.level
        return level


def summarize_plan(actions: list[SyncAction]) -> dict:
    """Возвращает краткую статистику по плану."""
    counts: dict[str, int] = {}
    bytes_to_copy = 0
    bytes_to_delete = 0
    for a in actions:
        key = a.action.value
        counts[key] = counts.get(key, 0) + 1
        if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE):
            bytes_to_copy += a.size_bytes
        elif a.action == ActionType.DELETE:
            bytes_to_delete += a.size_bytes
    return {
        "counts": counts,
        "bytes_to_copy": bytes_to_copy,
        "bytes_to_delete": bytes_to_delete,
        "total_actions": len(actions),
    }
