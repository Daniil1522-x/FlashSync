"""
application/sync_engine.py
"""
from __future__ import annotations
import asyncio
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable

try:
    from send2trash import send2trash
    USE_TRASH = True
except ImportError:
    USE_TRASH = False

from domain.models import SyncAction, ActionType, SyncReport, SyncProfile


class SyncEngine:
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
        self._report_lock: Optional[asyncio.Lock] = None

    def _get_backup_dir(self) -> Path:
        if self._backup_dir is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._backup_dir = self.dst / f".flashsync_backup_{ts}"
            if not self.dry_run:
                self._backup_dir.mkdir(parents=True, exist_ok=True)
        return self._backup_dir

    def _check_disk_space(self, bytes_needed: int) -> tuple[bool, int]:
        try:
            usage = shutil.disk_usage(self.dst)
            return usage.free >= bytes_needed, usage.free
        except OSError:
            return True, -1

    async def _backup_file(self, file_path: Path, rel_path: Path) -> bool:
        """Копирует файл в backup. Неблокирующий."""
        if not file_path.exists():
            return True
        backup_dir = self._get_backup_dir()
        dest = backup_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(shutil.copy2, str(file_path), str(dest))
            return True
        except (OSError, shutil.Error):
            return False

    async def _copy_file(self, src: Path, dst: Path) -> bool:
        """Копирует файл. Неблокирующий."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(shutil.copy2, str(src), str(dst))
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

        # Lock для потокобезопасного обновления отчёта
        self._report_lock = asyncio.Lock()

        bytes_needed = sum(
            a.size_bytes for a in actions
            if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE)
        )

        ok, free = self._check_disk_space(bytes_needed)
        if not ok and free != -1:
            msg = (f"Недостаточно места: нужно {bytes_needed // (1024**2)} МБ, "
                   f"свободно {free // (1024**2)} МБ")
            report.errors.append(msg)
            report.add_log(f"[ОШИБКА] {msg}")
            report.finished_at = datetime.now()
            return report

        semaphore = asyncio.Semaphore(self.profile.max_workers)

        async def _process(action: SyncAction) -> None:
            async with semaphore:
                await self._execute_single(action, report)

        active = [a for a in actions
                  if a.action not in (ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED)]

        if active:
            await asyncio.gather(*[_process(a) for a in active], return_exceptions=True)

        report.finished_at = datetime.now()
        return report

    async def _execute_single(self, action: SyncAction, report: SyncReport) -> None:
        rel = action.rel_path
        prefix = "[DRY RUN] " if self.dry_run else ""

        try:
            if action.action == ActionType.COPY_NEW:
                msg = f"{prefix}КОПИРОВАТЬ  {rel}"
                self._emit(action, msg)
                report.add_log(f"  + {msg}")
                if not self.dry_run:
                    ok = await self._copy_file(self.src / rel, self.dst / rel)
                    async with self._report_lock:
                        if ok:
                            report.actions_done.append(action)
                            report.bytes_copied += action.size_bytes
                        else:
                            report.errors.append(f"Ошибка копирования: {rel}")
                            report.add_log(f"  [ОШИБКА] Не удалось скопировать: {rel}")
                else:
                    async with self._report_lock:
                        report.actions_done.append(action)
                        report.bytes_copied += action.size_bytes

            elif action.action == ActionType.COPY_UPDATE:
                msg = f"{prefix}ОБНОВИТЬ    {rel}  (старый → backup)"
                self._emit(action, msg)
                report.add_log(f"  ~ {msg}")
                if not self.dry_run:
                    dst_path = self.dst / rel
                    backed = await self._backup_file(dst_path, rel)
                    ok = await self._copy_file(self.src / rel, dst_path)
                    async with self._report_lock:
                        if ok:
                            report.actions_done.append(action)
                            report.bytes_copied += action.size_bytes
                            if not backed:
                                report.warnings.append(f"Backup не удался, файл обновлён: {rel}")
                        else:
                            report.errors.append(f"Ошибка обновления: {rel}")
                            report.add_log(f"  [ОШИБКА] Не удалось обновить: {rel}")
                else:
                    async with self._report_lock:
                        report.actions_done.append(action)
                        report.bytes_copied += action.size_bytes

            elif action.action == ActionType.DELETE:
                msg = f"{prefix}В BACKUP    {rel}  (файла нет в источнике)"
                self._emit(action, msg)
                report.add_log(f"  - {msg}")
                if not self.dry_run:
                    dst_path = self.dst / rel
                    backed = await self._backup_file(dst_path, rel)
                    async with self._report_lock:
                        if backed:
                            report.actions_done.append(action)
                            report.bytes_backed_up += action.size_bytes
                            try:
                                await asyncio.to_thread(dst_path.unlink, missing_ok=True)
                            except OSError as e:
                                report.warnings.append(f"Не удалось удалить после backup {rel}: {e}")
                        else:
                            report.errors.append(f"Ошибка backup: {rel}")
                            report.add_log(f"  [ОШИБКА] Не удалось переместить в backup: {rel}")
                else:
                    async with self._report_lock:
                        report.actions_done.append(action)
                        report.bytes_backed_up += action.size_bytes

            elif action.action == ActionType.DELETE_PERM:
                msg = f"{prefix}УДАЛЕНИЕ    {rel}  (в корзину/безвозвратно)"
                self._emit(action, msg)
                report.add_log(f"  🗑 {msg}")
                if not self.dry_run:
                    dst_path = self.dst / rel
                    try:
                        if USE_TRASH:
                            await asyncio.to_thread(send2trash, str(dst_path))
                        else:
                            if dst_path.is_dir():
                                await asyncio.to_thread(shutil.rmtree, dst_path)
                            else:
                                await asyncio.to_thread(dst_path.unlink, missing_ok=True)

                        async with self._report_lock:
                            report.actions_done.append(action)
                            report.bytes_deleted_perm += action.size_bytes
                    except Exception as e:
                        async with self._report_lock:
                            report.errors.append(f"Ошибка удаления {rel}: {e}")
                            report.add_log(f"  [ОШИБКА] Не удалось удалить: {rel}")
                else:
                    async with self._report_lock:
                        report.actions_done.append(action)
                        report.bytes_deleted_perm += action.size_bytes

        except Exception as e:
            async with self._report_lock:
                report.errors.append(f"{rel}: {e}")

    def _emit(self, action: SyncAction, msg: str) -> None:
        if self.progress_cb:
            self.progress_cb(action, msg)
