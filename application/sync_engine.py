from __future__ import annotations
import asyncio, shutil
from datetime import datetime
from pathlib import Path
from typing import Optional, Callable
from domain.models import SyncAction, ActionType, SyncReport, SyncProfile
from infrastructure.copier import copy_file, copy_batch_small, SMALL_FILE_THRESHOLD

try:
    from send2trash import send2trash
    USE_TRASH = True
except ImportError:
    USE_TRASH = False

BATCH_SIZE = 16

class SyncEngine:
    def __init__(self, profile: SyncProfile, src: Path, dst: Path,
                 dry_run: bool = False,
                 progress_cb: Optional[Callable[[SyncAction, str], None]] = None,
                 stop_flag: Optional[Callable[[], bool]] = None):
        self.profile = profile
        self.src = src
        self.dst = dst
        self.dry_run = dry_run
        self.progress_cb = progress_cb
        self.stop_flag = stop_flag or (lambda: False)
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
        if not file_path.exists():
            return True
        backup_dir = self._get_backup_dir()
        dest = backup_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            await asyncio.to_thread(shutil.copy2, str(file_path), str(dest))
            return True
        except Exception:
            return False

    async def execute(self, actions: list[SyncAction], report: Optional[SyncReport] = None) -> SyncReport:
        if report is None:
            report = SyncReport()
        self._report_lock = asyncio.Lock()

        bytes_needed = sum(a.size_bytes for a in actions if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE))
        ok, free = self._check_disk_space(bytes_needed)
        if not ok and free != -1:
            msg = f"Недостаточно места: нужно {bytes_needed//(1024**2)} МБ, свободно {free//(1024**2)} МБ"
            report.errors.append(msg)
            report.add_log(f"[ОШИБКА] {msg}")
            report.finished_at = datetime.now()
            return report

        active = [a for a in actions if a.action not in (ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED)]
        if not active:
            report.finished_at = datetime.now()
            return report

        small_copy = [a for a in active if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE) and a.size_bytes < SMALL_FILE_THRESHOLD]
        large_copy = [a for a in active if a.action in (ActionType.COPY_NEW, ActionType.COPY_UPDATE) and a.size_bytes >= SMALL_FILE_THRESHOLD]
        other      = [a for a in active if a.action not in (ActionType.COPY_NEW, ActionType.COPY_UPDATE)]

        await self._execute_small_batch(small_copy, report)

        sem = asyncio.Semaphore(self.profile.max_workers)
        for action in large_copy:
            if self.stop_flag(): break
            await self._execute_large(action, report, sem)
        for action in other:
            if self.stop_flag(): break
            await self._execute_other(action, report)

        report.finished_at = datetime.now()
        return report

    async def _execute_small_batch(self, actions: list[SyncAction], report: SyncReport) -> None:
        if not actions:
            return
        if self.dry_run:
            for a in actions:
                verb = "КОПИРОВАТЬ" if a.action == ActionType.COPY_NEW else "ОБНОВИТЬ"
                self._emit(a, f"[DRY] {verb}  {a.rel_path}")
                report.add_log(
                    f"  [DRY] {verb:<10} {str(a.rel_path):<55} {_fmt(a.size_bytes):>7}"
                    f"  [{a.rel_path.suffix.lower() or 'нет расш.'}]"
                )
            async with self._report_lock:
                report.actions_done.extend(actions)
                report.bytes_copied += sum(a.size_bytes for a in actions)
            return

        for a in actions:
            if a.action == ActionType.COPY_UPDATE:
                await self._backup_file(self.dst / a.rel_path, a.rel_path)

        for i in range(0, len(actions), BATCH_SIZE):
            if self.stop_flag(): break
            batch = actions[i:i + BATCH_SIZE]
            self._emit(batch[0], f"Копирование {len(batch)} файлов... {batch[0].rel_path.name}")
            pairs = [(self.src / a.rel_path, self.dst / a.rel_path) for a in batch]
            results = await copy_batch_small(pairs)
            async with self._report_lock:
                for a, (_, _, ok) in zip(batch, results):
                    verb = "КОПИРОВАТЬ" if a.action == ActionType.COPY_NEW else "ОБНОВИТЬ"
                    if ok:
                        report.actions_done.append(a)
                        report.bytes_copied += a.size_bytes
                        report.add_log(
                            f"  ✓ {verb:<10} {str(a.rel_path):<55} {_fmt(a.size_bytes):>7}"
                            f"  [{a.rel_path.suffix.lower() or 'нет расш.'}]"
                        )
                    else:
                        report.errors.append(f"Ошибка копирования: {a.rel_path}")
                        report.add_log(
                            f"  ✗ {verb:<10} {str(a.rel_path):<55} — ОШИБКА"
                        )

    async def _execute_large(self, action: SyncAction, report: SyncReport, sem: asyncio.Semaphore) -> None:
        async with sem:
            rel = action.rel_path
            size = action.size_bytes
            prefix = "[DRY] " if self.dry_run else ""
            verb = "КОПИРОВАТЬ" if action.action == ActionType.COPY_NEW else "ОБНОВИТЬ"
            self._emit(action, f"{prefix}{verb}  {rel}  ({_fmt(size)})")
            report.add_log(
                f"  → {prefix}{verb:<10} {str(rel):<55} {_fmt(size):>7}"
                f"  [{rel.suffix.lower() or 'нет расш.'}]  (большой файл)"
            )
            if self.dry_run:
                async with self._report_lock:
                    report.actions_done.append(action)
                    report.bytes_copied += size
                return
            if action.action == ActionType.COPY_UPDATE:
                await self._backup_file(self.dst / rel, rel)
            _last = [0]
            def _prog(done: int, total: int) -> None:
                if done - _last[0] >= 5 * 1024 * 1024:
                    _last[0] = done
                    pct = int(done / total * 100) if total else 0
                    self._emit(action, f"{verb}  {rel.name}  {_fmt(done)}/{_fmt(total)}  {pct}%")
            ok = await copy_file(self.src / rel, self.dst / rel, verify=self.profile.use_hash, progress_cb=_prog)
            async with self._report_lock:
                if ok:
                    report.actions_done.append(action)
                    report.bytes_copied += size
                    report.add_log(
                        f"  ✓ {verb:<10} {str(rel):<55} {_fmt(size):>7}  верифицирован"
                    )
                else:
                    report.errors.append(f"Ошибка копирования: {rel}")
                    report.add_log(
                        f"  ✗ {verb:<10} {str(rel):<55} — ОШИБКА КОПИРОВАНИЯ"
                    )

    async def _execute_other(self, action: SyncAction, report: SyncReport) -> None:
        rel = action.rel_path
        prefix = "[DRY] " if self.dry_run else ""
        try:
            if action.action == ActionType.DELETE:
                self._emit(action, f"{prefix}В BACKUP  {rel}")
                report.add_log(
                    f"  ⊘ {'[DRY] ' if self.dry_run else ''}В BACKUP  "
                    f"{str(rel):<55} {_fmt(action.size_bytes):>7}"
                    f"  [{rel.suffix.lower() or 'нет расш.'}]"
                )
                if not self.dry_run:
                    backed = await self._backup_file(self.dst / rel, rel)
                    async with self._report_lock:
                        if backed:
                            report.actions_done.append(action)
                            report.bytes_backed_up += action.size_bytes
                            try:
                                await asyncio.to_thread((self.dst / rel).unlink, missing_ok=True)
                            except OSError as e:
                                report.warnings.append(f"Удалить после backup {rel}: {e}")
                        else:
                            report.errors.append(f"Ошибка backup: {rel}")
                else:
                    async with self._report_lock:
                        report.actions_done.append(action)
                        report.bytes_backed_up += action.size_bytes
            elif action.action == ActionType.DELETE_PERM:
                self._emit(action, f"{prefix}УДАЛЕНИЕ  {rel}")
                report.add_log(
                    f"  🗑 {'[DRY] ' if self.dry_run else ''}УДАЛИТЬ   "
                    f"{str(rel):<55} {_fmt(action.size_bytes):>7}"
                )
                if not self.dry_run:
                    dst_path = self.dst / rel
                    try:
                        if USE_TRASH:
                            await asyncio.to_thread(send2trash, str(dst_path))
                        else:
                            await asyncio.to_thread(dst_path.unlink, missing_ok=True)
                        async with self._report_lock:
                            report.actions_done.append(action)
                            report.bytes_deleted_perm += action.size_bytes
                    except Exception as e:
                        async with self._report_lock:
                            report.errors.append(f"Ошибка удаления {rel}: {e}")
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

def _fmt(size: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if size < 1024:
            return f"{size:.0f}{unit}"
        size /= 1024
    return f"{size:.1f}T"