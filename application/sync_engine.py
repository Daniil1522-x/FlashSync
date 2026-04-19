"""
application/sync_engine.py — Исполнитель плана синхронизации.
Реализует безопасное копирование, backup, логирование.
"""
from __future__ import annotations

import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

from domain.models import (
    SyncAction, ActionType, SyncReport, SyncProfile, ProtectionLevel
)
from infrastructure.hasher import verify_copy


CHUNK_SIZE = 1024 * 1024  # 1 МБ


class SyncEngine:
    """
    Выполняет список SyncAction.
    Поддерживает dry_run, backup, прогресс-коллбек.
    """

    def __init__(
        self,
        profile: SyncProfile,
        src: Path,
        dst: Path,
        dry_run: bool = False,
        progress_cb: Optional[Callable[[SyncAction, str], None]] = None,
    ):
        self.profile = profile
        self.src = src
        self.dst = dst
        self.dry_run = dry_run
        self.progress_cb = progress_cb
        self._backup_dir: Optional[Path] = None

    def _get_backup_dir(self) -> Path:
        if not self._backup_dir:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._backup_dir = self.dst / f".flashsync_backup_{ts}"
            if not self.dry_run:
                self._backup_dir.mkdir(parents=True, exist_ok=True)
        return self._backup_dir

    def _check_disk_space(self, bytes_needed: int) -> tuple[bool, int]:
        """Проверяет наличие свободного места на dst."""
        try:
            usage = shutil.disk_usage(self.dst)
            return usage.free >= bytes_needed, usage.free
        except OSError:
            return True, -1

    def _backup_file(self, file_path: Path, rel_path: Path) -> bool:
        """Перемещает файл в backup. Возвращает True при успехе."""
        backup_dir = self._get_backup_dir()
        dest = backup_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(file_path), str(dest))
            return True
        except (OSError, shutil.Error) as e:
            return False

    def _copy_file(self, src: Path, dst: Path) -> bool:
        """Копирует файл по чанкам. Возвращает True при успехе."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(str(src), str(dst))
            return True
        except (OSError, shutil.Error):
            return False

    async def execute(
        self,
        actions: list[SyncAction],
        report: Optional[SyncReport] = None,
    ) -> SyncReport:
        if report is None:
            report = SyncReport()

        # Считаем сколько байт нужно скопировать
        bytes_needed = sum(
            a.size_bytes for a in actions
            if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE)
        )

        ok, free = self._check_disk_space(bytes_needed)
        if not ok:
            report.errors.append(
                f"Недостаточно места: нужно {bytes_needed // (1024**2)} МБ, "
                f"свободно {free // (1024**2)} МБ"
            )
            report.finished_at = datetime.now()
            return report

        semaphore = asyncio.Semaphore(self.profile.max_workers)

        async def _process_action(action: SyncAction) -> None:
            async with semaphore:
                await self._execute_single(action, report)

        tasks = [
            _process_action(a) for a in actions
            if a.action not in (ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED)
        ]

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        report.finished_at = datetime.now()
        return report

    async def _execute_single(self, action: SyncAction, report: SyncReport) -> None:
        rel = action.rel_path

        try:
            if action.action == ActionType.COPY_NEW:
                src_path = self.src / rel
                dst_path = self.dst / rel
                self._emit(action, f"→ Копирование: {rel}")
                if not self.dry_run:
                    ok = self._copy_file(src_path, dst_path)
                    if ok:
                        report.actions_done.append(action)
                        report.bytes_copied += action.size_bytes
                    else:
                        report.errors.append(f"Ошибка копирования: {rel}")
                else:
                    report.actions_done.append(action)

            elif action.action == ActionType.COPY_UPDATE:
                src_path = self.src / rel
                dst_path = self.dst / rel
                self._emit(action, f"↻ Обновление: {rel}")
                if not self.dry_run:
                    # Сначала backup старого файла
                    self._backup_file(dst_path, rel)
                    ok = self._copy_file(src_path, dst_path)
                    if ok:
                        report.actions_done.append(action)
                        report.bytes_copied += action.size_bytes
                    else:
                        report.errors.append(f"Ошибка обновления: {rel}")
                else:
                    report.actions_done.append(action)

            elif action.action == ActionType.DELETE:
                dst_path = self.dst / rel
                self._emit(action, f"⊘ Удаление (→ backup): {rel}")
                if not self.dry_run:
                    ok = self._backup_file(dst_path, rel)
                    if ok:
                        report.actions_done.append(action)
                        report.bytes_backed_up += action.size_bytes
                    else:
                        report.errors.append(f"Ошибка backup: {rel}")
                else:
                    report.actions_done.append(action)

        except Exception as e:
            report.errors.append(f"{rel}: {e}")

    def _emit(self, action: SyncAction, msg: str) -> None:
        if self.progress_cb:
            self.progress_cb(action, msg)
