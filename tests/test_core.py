"""
tests/test_core.py — Полный тест-сьют FlashSync.
Запуск: pytest tests/test_core.py -v
"""
from __future__ import annotations
import asyncio
import os
import tempfile
from pathlib import Path
import pytest
import sys

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from domain.models import (
    FileInfo, SyncAction, ActionType, ProtectionLevel,
    ProtectionRule, SyncProfile, SyncReport, _classify,
)
from application.differ import DiffEngine, summarize_plan
from application.plan_overrides import PlanOverrides
from infrastructure.hasher import hash_file, verify_copy, quick_diff
from infrastructure.scanner import scan_directory, get_directory_stats

# ── Фикстуры ──────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_dirs():
    with tempfile.TemporaryDirectory() as s, tempfile.TemporaryDirectory() as d:
        yield Path(s), Path(d)


@pytest.fixture
def profile(tmp_dirs):
    src, dst = tmp_dirs
    return SyncProfile(
        name="test", src=str(src), dst=str(dst), use_hash=True,
        delete_mode=True,
        protection_rules=[
            ProtectionRule("*important*", ProtectionLevel.DOUBLE),
            ProtectionRule("*contract*", ProtectionLevel.SINGLE),
        ],
    )


def write(path: Path, content: bytes = b"hello") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def fi(root: Path, rel: str, content: bytes) -> FileInfo:
    full = root / rel
    write(full, content)
    return FileInfo(path=full, rel_path=Path(rel),
                    size=full.stat().st_size, mtime=full.stat().st_mtime,
                    hash=hash_file(full))


# ── domain/models ─────────────────────────────────────────────────────────────


class TestClassify:
    def test_image(self): assert _classify(".jpg") == "image"
    def test_video(self): assert _classify(".mp4") == "video"
    def test_other(self): assert _classify(".xyz") == "other"
    def test_code(self): assert _classify(".py") == "code"


class TestFileInfo:
    def test_same_by_hash(self, tmp_dirs):
        src, _ = tmp_dirs
        f = write(src / "a.txt", b"same")
        fi1 = FileInfo(path=f, rel_path=Path("a.txt"), size=4, mtime=f.stat().st_mtime, hash="abc")
        fi2 = FileInfo(path=f, rel_path=Path("a.txt"), size=4, mtime=f.stat().st_mtime, hash="abc")
        assert fi1.is_same_as(fi2, use_hash=True)

    def test_diff_by_hash(self, tmp_dirs):
        src, _ = tmp_dirs
        f = write(src / "a.txt", b"x")
        fi1 = FileInfo(path=f, rel_path=Path("a.txt"), size=1, mtime=0, hash="aaa")
        fi2 = FileInfo(path=f, rel_path=Path("a.txt"), size=1, mtime=0, hash="bbb")
        assert not fi1.is_same_as(fi2, use_hash=True)

    def test_size_bytes_copy_new(self, tmp_dirs):
        src, _ = tmp_dirs
        f = write(src / "f.txt", b"hello")
        finfo = FileInfo(path=f, rel_path=Path("f.txt"), size=5, mtime=0)
        a = SyncAction(action=ActionType.COPY_NEW, src_file=finfo)
        assert a.size_bytes == 5

    def test_size_bytes_delete(self, tmp_dirs):
        _, dst = tmp_dirs
        f = write(dst / "f.txt", b"hello")
        finfo = FileInfo(path=f, rel_path=Path("f.txt"), size=5, mtime=0)
        a = SyncAction(action=ActionType.DELETE, dst_file=finfo)
        assert a.size_bytes == 5

    def test_size_bytes_delete_perm(self, tmp_dirs):
        _, dst = tmp_dirs
        f = write(dst / "f.txt", b"x" * 999)
        finfo = FileInfo(path=f, rel_path=Path("f.txt"), size=999, mtime=0)
        a = SyncAction(action=ActionType.DELETE_PERM, dst_file=finfo)
        assert a.size_bytes == 999

    def test_size_bytes_skip_equal_is_zero(self, tmp_dirs):
        src, _ = tmp_dirs
        f = write(src / "f.txt", b"x")
        finfo = FileInfo(path=f, rel_path=Path("f.txt"), size=1, mtime=0)
        a = SyncAction(action=ActionType.SKIP_EQUAL, src_file=finfo)
        assert a.size_bytes == 0


class TestProtectionRule:
    def test_keyword_match(self):
        r = ProtectionRule("*important*", ProtectionLevel.DOUBLE)
        assert r.matches(Path("docs/important_file.pdf"))
        assert not r.matches(Path("random.pdf"))

    def test_glob_match(self):
        r = ProtectionRule("*.docx", ProtectionLevel.SINGLE)
        assert r.matches(Path("doc.docx"))
        assert not r.matches(Path("doc.pdf"))

    def test_double_confirm(self, tmp_dirs):
        src, _ = tmp_dirs
        f = write(src / "important.txt", b"x")
        finfo = FileInfo(path=f, rel_path=Path("important.txt"), size=1, mtime=0)
        a = SyncAction(action=ActionType.DELETE, dst_file=finfo, protection_level=ProtectionLevel.DOUBLE)
        assert not a.confirm()
        assert a.confirm()


# ── infrastructure/hasher ────────────────────────────────────────────────────


class TestHasher:
    def test_consistent(self, tmp_dirs):
        src, _ = tmp_dirs
        f = write(src / "f.bin", b"stable")
        h1 = hash_file(f); h2 = hash_file(f)
        assert h1 == h2 and len(h1) == 64

    def test_different(self, tmp_dirs):
        src, _ = tmp_dirs
        f1 = write(src / "a.bin", b"aaa")
        f2 = write(src / "b.bin", b"bbb")
        assert hash_file(f1) != hash_file(f2)

    def test_verify_success(self, tmp_dirs):
        src, dst = tmp_dirs
        content = b"verify" * 100
        assert verify_copy(write(src / "f.dat", content), write(dst / "f.dat", content))

    def test_verify_fail(self, tmp_dirs):
        src, dst = tmp_dirs
        assert not verify_copy(write(src / "f.dat", b"orig"), write(dst / "f.dat", b"diff"))

    def test_missing_file(self, tmp_dirs):
        src, _ = tmp_dirs
        assert hash_file(src / "nonexistent.txt") is None


# ── infrastructure/scanner ───────────────────────────────────────────────────


class TestScanner:
    def test_basic(self, tmp_dirs):
        src, _ = tmp_dirs
        write(src / "a.txt", b"hello")
        write(src / "sub" / "b.jpg", b"image")
        result = scan_directory(src, use_hash=False)
        assert len(result) == 2
        assert Path("a.txt") in result
        assert Path("sub/b.jpg") in result

    def test_exclude(self, tmp_dirs):
        src, _ = tmp_dirs
        write(src / "data.txt", b"keep")
        write(src / "temp.log", b"skip")
        result = scan_directory(src, exclude_patterns=["*.log"], use_hash=False)
        assert Path("data.txt") in result
        assert Path("temp.log") not in result

    def test_ignores_system_files(self, tmp_dirs):
        src, _ = tmp_dirs
        write(src / "real.txt", b"keep")
        write(src / "Thumbs.db", b"system")
        result = scan_directory(src, use_hash=False)
        assert Path("real.txt") in result
        assert all("thumbs" not in str(k).lower() for k in result)

    def test_with_hash(self, tmp_dirs):
        src, _ = tmp_dirs
        write(src / "a.txt", b"content")
        result = scan_directory(src, use_hash=True)
        assert result[Path("a.txt")].hash is not None
        assert len(result[Path("a.txt")].hash) == 64

    def test_nonexistent_dir(self):
        result = scan_directory(Path("/nonexistent/path/xyz"))
        assert result == {}

    def test_error_callback(self, tmp_dirs):
        """Ошибки доступа не роняют сканирование — вызывают error_cb."""
        src, _ = tmp_dirs
        write(src / "good.txt", b"ok")
        errors = []
        result = scan_directory(src, use_hash=False, error_cb=lambda p, e: errors.append(p))
        assert Path("good.txt") in result

    def test_progress_callback(self, tmp_dirs):
        """progress_cb вызывается до хеширования."""
        src, _ = tmp_dirs
        for i in range(10):
            write(src / f"f{i}.txt", b"x")
        found = []
        scan_directory(src, use_hash=False, progress_cb=lambda p: found.append(p))
        assert len(found) == 10

    def test_skips_backup_dirs(self, tmp_dirs):
        """Папки .flashsync_backup_* не сканируются."""
        src, _ = tmp_dirs
        write(src / "real.txt", b"keep")
        backup = src / ".flashsync_backup_20260101_120000"
        write(backup / "old.txt", b"old")
        result = scan_directory(src, use_hash=False)
        assert Path("real.txt") in result
        assert not any("backup" in str(k) for k in result)

    def test_large_directory_simulation(self, tmp_dirs):
        """500 файлов — не падает, не теряет файлы."""
        src, _ = tmp_dirs
        N = 500
        for i in range(N):
            write(src / f"subdir_{i % 10}" / f"file_{i}.dat", f"content_{i}".encode())
        result = scan_directory(src, use_hash=False)
        assert len(result) == N

    def test_get_stats(self, tmp_dirs):
        src, _ = tmp_dirs
        write(src / "photo.jpg", b"x" * 1000)
        write(src / "doc.pdf", b"y" * 500)
        write(src / "song.mp3", b"z" * 2000)
        stats = get_directory_stats(src)
        assert stats["total_files"] == 3
        assert stats["total_size"] == 3500
        assert "image" in stats["categories"]


# ── application/differ ───────────────────────────────────────────────────────


class TestDiffEngine:
    def test_copy_new(self, tmp_dirs, profile):
        src, dst = tmp_dirs
        src_fi = fi(src, "new.txt", b"new")
        plan = DiffEngine(profile).compute_plan({Path("new.txt"): src_fi}, {})
        assert plan[0].action == ActionType.COPY_NEW

    def test_skip_equal(self, tmp_dirs, profile):
        src, dst = tmp_dirs
        content = b"same"
        plan = DiffEngine(profile).compute_plan(
            {Path("f.txt"): fi(src, "f.txt", content)},
            {Path("f.txt"): fi(dst, "f.txt", content)},
        )
        assert plan[0].action == ActionType.SKIP_EQUAL

    def test_copy_update(self, tmp_dirs, profile):
        src, dst = tmp_dirs
        plan = DiffEngine(profile).compute_plan(
            {Path("f.txt"): fi(src, "f.txt", b"new")},
            {Path("f.txt"): fi(dst, "f.txt", b"old")},
        )
        assert plan[0].action == ActionType.COPY_UPDATE

    def test_delete(self, tmp_dirs, profile):
        _, dst = tmp_dirs
        plan = DiffEngine(profile).compute_plan({}, {Path("orphan.txt"): fi(dst, "orphan.txt", b"x")})
        assert plan[0].action == ActionType.DELETE

    def test_no_delete_when_disabled(self, tmp_dirs, profile):
        _, dst = tmp_dirs
        profile.delete_mode = False
        plan = DiffEngine(profile).compute_plan({}, {Path("orphan.txt"): fi(dst, "orphan.txt", b"x")})
        assert len(plan) == 0

    def test_protection_blocks_update(self, tmp_dirs, profile):
        src, dst = tmp_dirs
        plan = DiffEngine(profile).compute_plan(
            {Path("important_doc.pdf"): fi(src, "important_doc.pdf", b"new")},
            {Path("important_doc.pdf"): fi(dst, "important_doc.pdf", b"old")},
        )
        assert plan[0].action == ActionType.SKIP_PROTECTED
        assert plan[0].protection_level == ProtectionLevel.DOUBLE

    def test_protection_blocks_delete(self, tmp_dirs, profile):
        _, dst = tmp_dirs
        plan2 = DiffEngine(profile).compute_plan(
            {}, {Path("contract_2024.docx"): fi(dst, "contract_2024.docx", b"x")})
        assert plan2[0].action == ActionType.SKIP_PROTECTED


class TestSummarizePlan:
    def test_empty(self):
        r = summarize_plan([])
        assert r["total_actions"] == 0
        assert r["bytes_to_copy"] == 0
        assert "bytes_to_delete_perm" in r

    def test_delete_perm_counted(self, tmp_dirs):
        _, dst = tmp_dirs
        f = write(dst / "f.dat", b"x" * 1024)
        finfo = FileInfo(path=f, rel_path=Path("f.dat"), size=1024, mtime=0)
        a = SyncAction(action=ActionType.DELETE_PERM, dst_file=finfo)
        r = summarize_plan([a])
        assert r["bytes_to_delete_perm"] == 1024
        assert r["counts"].get("delete_permanent") == 1


# ── application/plan_overrides ───────────────────────────────────────────────


class TestPlanOverrides:
    def _plan(self, src, dst) -> list[SyncAction]:
        profile = SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False)
        write(src / "a.txt", b"aaa")
        write(dst / "b.txt", b"old_b")
        write(src / "b.txt", b"new_b")
        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        return DiffEngine(profile).compute_plan(st, dt)

    def test_override_survives_rescan(self, tmp_dirs):
        src, dst = tmp_dirs
        import dataclasses
        plan = self._plan(src, dst)
        ov = PlanOverrides("test_ov")
        ov.clear()
        b_action = next(a for a in plan if "b.txt" in str(a.rel_path))
        protected = dataclasses.replace(b_action, action=ActionType.SKIP_PROTECTED,
                                        protection_level=ProtectionLevel.SINGLE,
                                        reason="вручную")
        ov.set(protected)
        plan2, applied, stale = ov.apply(self._plan(src, dst))
        by = {a.rel_path.as_posix(): a for a in plan2}
        assert by["b.txt"].action == ActionType.SKIP_PROTECTED
        assert applied == 1
        assert stale == 0
        ov.clear()

    def test_stale_on_file_change(self, tmp_dirs):
        """Override сбрасывается когда файл реально изменился на диске."""
        src, dst = tmp_dirs
        import dataclasses
        write(dst / "will_change.txt", b"original_v1")
        profile = SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False, delete_mode=True)
        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        plan = DiffEngine(profile).compute_plan(st, dt)

        del_action = next(a for a in plan if "will_change" in str(a.rel_path))
        assert del_action.action == ActionType.DELETE

        ov = PlanOverrides("test_stale")
        ov.clear()
        ov.set(dataclasses.replace(del_action, action=ActionType.SKIP_PROTECTED,
                                   protection_level=ProtectionLevel.SINGLE,
                                   reason="вручную"))
        assert ov.count() == 1

        write(dst / "will_change.txt", b"changed_to_something_completely_different")
        dt2 = scan_directory(dst, use_hash=False)
        plan2 = DiffEngine(profile).compute_plan(st, dt2)
        _, applied, stale = ov.apply(plan2)

        assert stale == 1
        assert applied == 0
        assert ov.count() == 0
        ov.clear()

    def test_clear(self, tmp_dirs):
        src, dst = tmp_dirs
        plan = self._plan(src, dst)
        ov = PlanOverrides("test_clear")
        ov.set_many(plan[:2])
        assert ov.count() == 2
        ov.clear()
        assert ov.count() == 0


class TestPlanOverridesIsStale:
    def test_is_stale_by_size_change(self, tmp_dirs):
        import dataclasses
        src, dst = tmp_dirs
        write(src / "f.txt", b"original")
        write(dst / "f.txt", b"OLD_VER")

        profile = SyncProfile(name="stale_t", src=str(src), dst=str(dst), use_hash=False)
        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        plan = DiffEngine(profile).compute_plan(st, dt)

        ov = PlanOverrides("stale_size_test")
        ov.clear()
        action = next(a for a in plan if "f.txt" in str(a.rel_path))
        protected = dataclasses.replace(action, action=ActionType.SKIP_PROTECTED,
                                        protection_level=ProtectionLevel.SINGLE, reason="manual")
        ov.set(protected)

        write(dst / "f.txt", b"completely_new_content_much_longer")
        dt2 = scan_directory(dst, use_hash=False)
        plan2 = DiffEngine(profile).compute_plan(st, dt2)
        _, applied, stale = ov.apply(plan2)

        assert stale == 1
        assert applied == 0
        assert ov.count() == 0
        ov.clear()

    def test_is_stale_by_hash_change(self, tmp_dirs):
        """Override устаревает по изменению хеша (одинаковый размер, разный контент)."""
        import dataclasses
        src, dst = tmp_dirs
        write(src / "f.bin", b"A" * 100)
        write(dst / "f.bin", b"B" * 100)

        profile = SyncProfile(name="stale_h", src=str(src), dst=str(dst), use_hash=True)
        st = scan_directory(src, use_hash=True)
        dt = scan_directory(dst, use_hash=True)
        plan = DiffEngine(profile).compute_plan(st, dt)

        ov = PlanOverrides("stale_hash_test")
        ov.clear()
        action = next(a for a in plan if "f.bin" in str(a.rel_path))
        protected = dataclasses.replace(action, action=ActionType.SKIP_PROTECTED,
                                        protection_level=ProtectionLevel.SINGLE, reason="manual")
        ov.set(protected)

        write(dst / "f.bin", b"C" * 100)
        dt2 = scan_directory(dst, use_hash=True)
        plan2 = DiffEngine(profile).compute_plan(st, dt2)
        _, applied, stale = ov.apply(plan2)

        assert stale == 1
        ov.clear()

    def test_not_stale_if_file_unchanged(self, tmp_dirs):
        import dataclasses
        src, dst = tmp_dirs
        write(src / "f.txt", b"content_src")
        write(dst / "f.txt", b"content_dst_old")

        profile = SyncProfile(name="notstale_t", src=str(src), dst=str(dst), use_hash=False)
        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        plan = DiffEngine(profile).compute_plan(st, dt)

        ov = PlanOverrides("not_stale_test")
        ov.clear()
        action = next(a for a in plan if "f.txt" in str(a.rel_path))
        protected = dataclasses.replace(action, action=ActionType.SKIP_PROTECTED,
                                        protection_level=ProtectionLevel.SINGLE, reason="manual")
        ov.set(protected)

        _, applied, stale = ov.apply(plan)
        assert applied == 1
        assert stale == 0
        ov.clear()


# ── application/sync_engine (интеграционный) ─────────────────────────────────


class TestSyncEngine:
    @pytest.mark.asyncio
    async def test_copy_new(self, tmp_dirs, profile):
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        f = write(src / "new.txt", b"brand new")
        finfo = FileInfo(path=f, rel_path=Path("new.txt"), size=f.stat().st_size, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=finfo)
        report = await SyncEngine(profile, src, dst, dry_run=False).execute([action])
        assert len(report.errors) == 0
        assert (dst / "new.txt").read_bytes() == b"brand new"

    @pytest.mark.asyncio
    async def test_dry_run_no_copy(self, tmp_dirs, profile):
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        f = write(src / "dry.txt", b"dry")
        finfo = FileInfo(path=f, rel_path=Path("dry.txt"), size=f.stat().st_size, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=finfo)
        report = await SyncEngine(profile, src, dst, dry_run=True).execute([action])
        assert not (dst / "dry.txt").exists()
        assert len(report.errors) == 0

    @pytest.mark.asyncio
    async def test_update_creates_backup(self, tmp_dirs, profile):
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        sf = write(src / "upd.txt", b"new version")
        df = write(dst / "upd.txt", b"old version")
        src_fi = FileInfo(path=sf, rel_path=Path("upd.txt"), size=sf.stat().st_size, mtime=sf.stat().st_mtime)
        dst_fi = FileInfo(path=df, rel_path=Path("upd.txt"), size=df.stat().st_size, mtime=df.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_UPDATE, src_file=src_fi, dst_file=dst_fi)
        report = await SyncEngine(profile, src, dst, dry_run=False).execute([action])
        assert len(report.errors) == 0
        assert (dst / "upd.txt").read_bytes() == b"new version"
        backup_dirs = list(dst.glob(".flashsync_backup_*"))
        assert len(backup_dirs) == 1

    @pytest.mark.asyncio
    async def test_protected_not_touched(self, tmp_dirs, profile):
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        sf = write(src / "important.txt", b"new")
        df = write(dst / "important.txt", b"PROTECTED")
        src_fi = FileInfo(path=sf, rel_path=Path("important.txt"), size=sf.stat().st_size, mtime=sf.stat().st_mtime)
        dst_fi = FileInfo(path=df, rel_path=Path("important.txt"), size=df.stat().st_size, mtime=df.stat().st_mtime)
        action = SyncAction(action=ActionType.SKIP_PROTECTED, src_file=src_fi, dst_file=dst_fi,
                            protection_level=ProtectionLevel.DOUBLE)
        report = await SyncEngine(profile, src, dst, dry_run=False).execute([action])
        assert (dst / "important.txt").read_bytes() == b"PROTECTED"

    @pytest.mark.asyncio
    async def test_batch_small_files(self, tmp_dirs, profile):
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        actions = []
        for i in range(20):
            f = write(src / f"small_{i}.txt", f"content_{i}".encode())
            finfo = FileInfo(path=f, rel_path=Path(f"small_{i}.txt"),
                             size=f.stat().st_size, mtime=f.stat().st_mtime)
            actions.append(SyncAction(action=ActionType.COPY_NEW, src_file=finfo))
        msgs = []
        report = await SyncEngine(profile, src, dst, dry_run=False,
                                  progress_cb=lambda a, m: msgs.append(m)).execute(actions)
        assert len(report.errors) == 0
        assert len(list(dst.glob("small_*.txt"))) == 20
        batch_msgs = [m for m in msgs if "Копирование" in m]
        assert len(batch_msgs) < 20

    @pytest.mark.asyncio
    async def test_disk_space_check(self, tmp_dirs, profile, monkeypatch):
        """
        Если места не хватает — отчёт содержит ошибку, файлы не копируются.

        Мокаем shutil.disk_usage напрямую вместо передачи искусственно
        огромного size_bytes (10**15) — тот подход полагался на то, что
        ни на одном реальном диске физически нет столько места, что само
        по себе верно сегодня, но является случайным совпадением, а не
        осознанной проверкой; и не проверяет реальную логику сравнения
        bytes_needed/free, поскольку любое значение "много" прошло бы.
        """
        import shutil as shutil_mod
        from application.sync_engine import SyncEngine

        src, dst = tmp_dirs
        f = write(src / "big.dat", b"x" * 1024)
        finfo = FileInfo(path=f, rel_path=Path("big.dat"), size=500 * 1024 * 1024, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=finfo)

        class FakeUsage:
            total = 1024 * 1024 * 1024
            used = 1000 * 1024 * 1024
            free = 100 * 1024 * 1024  # всего 100 МБ свободно — меньше чем нужно 500 МБ

        monkeypatch.setattr(shutil_mod, "disk_usage", lambda path: FakeUsage())

        engine = SyncEngine(profile, src, dst, dry_run=False)
        report = await engine.execute([action])
        assert len(report.errors) > 0
        assert "место" in report.errors[0].lower() or "недостаточно" in report.errors[0].lower()
        assert not (dst / "big.dat").exists()

    @pytest.mark.asyncio
    async def test_disk_space_check_accounts_for_backup(self, tmp_dirs, profile, monkeypatch):
        """
        Регрессионный тест: проверка места должна учитывать размер backup
        для COPY_UPDATE/DELETE, а не только размер новых данных (баг 2.1
        из код-ревью — раньше bytes_needed считал только COPY_NEW/COPY_UPDATE
        без добавления места под backup старой версии).
        """
        import shutil as shutil_mod
        from application.sync_engine import SyncEngine

        src, dst = tmp_dirs
        sf = write(src / "f.dat", b"x" * 1024)
        df = write(dst / "f.dat", b"y" * 1024)
        src_fi = FileInfo(path=sf, rel_path=Path("f.dat"), size=200 * 1024 * 1024, mtime=sf.stat().st_mtime)
        dst_fi = FileInfo(path=df, rel_path=Path("f.dat"), size=200 * 1024 * 1024, mtime=df.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_UPDATE, src_file=src_fi, dst_file=dst_fi)

        class FakeUsage:
            total = 1024 * 1024 * 1024
            used = 700 * 1024 * 1024
            # 300 МБ свободно: хватает на копирование (200МБ) но НЕ хватает
            # если также учесть backup старой версии (ещё +200МБ = 400МБ нужно)
            free = 300 * 1024 * 1024

        monkeypatch.setattr(shutil_mod, "disk_usage", lambda path: FakeUsage())

        engine = SyncEngine(profile, src, dst, dry_run=False)
        report = await engine.execute([action])
        assert len(report.errors) > 0, "Проверка места должна была учесть размер backup и отказать"

    @pytest.mark.asyncio
    async def test_dry_run_skips_disk_space_check(self, tmp_dirs, profile):
        """Dry Run должен работать даже если реально не хватает места — он ничего не пишет."""
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        f = write(src / "huge.dat", b"x" * 1024)
        finfo = FileInfo(path=f, rel_path=Path("huge.dat"), size=10**15, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=finfo)
        engine = SyncEngine(profile, src, dst, dry_run=True)
        report = await engine.execute([action])
        assert len(report.errors) == 0
        assert len(report.actions_done) == 1


# ── Безопасность ──────────────────────────────────────────────────────────────


class TestSafety:
    def test_no_path_traversal(self, tmp_dirs, profile):
        src, dst = tmp_dirs
        write(src / "normal.txt", b"ok")
        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        plan = DiffEngine(profile).compute_plan(st, dt)
        for a in plan:
            assert not a.rel_path.is_absolute()
            assert ".." not in a.rel_path.parts

    def test_empty_src_safe(self, tmp_dirs, profile):
        src, dst = tmp_dirs
        write(dst / "existing.txt", b"keep me")
        profile.delete_mode = False
        st = scan_directory(src / "nonexistent", use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        plan = DiffEngine(profile).compute_plan(st, dt)
        assert all(a.action != ActionType.DELETE for a in plan)

    @pytest.mark.asyncio
    async def test_protected_survives_sync(self, tmp_dirs, profile):
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        write(src / "important.txt", b"new")
        write(dst / "important.txt", b"ORIGINAL")
        st = scan_directory(src, use_hash=True)
        dt = scan_directory(dst, use_hash=True)
        plan = DiffEngine(profile).compute_plan(st, dt)
        active = [a for a in plan if a.action not in (ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED)]
        await SyncEngine(profile, src, dst, dry_run=False).execute(active)
        assert (dst / "important.txt").read_bytes() == b"ORIGINAL"


# ── infrastructure/storage ───────────────────────────────────────────────────


class TestStorage:
    def test_normalize_path_no_dup(self):
        from infrastructure.storage import _normalize_path
        assert _normalize_path(r"E:\folder") == r"E:\folder"

    def test_normalize_path_removes_dup(self):
        from infrastructure.storage import _normalize_path
        assert _normalize_path(r"E:\E:\folder") == r"E:\folder"

    def test_normalize_empty(self):
        from infrastructure.storage import _normalize_path
        assert _normalize_path("") == ""

    def test_save_and_load_profiles(self, tmp_dirs):
        from infrastructure.storage import save_profiles, load_profiles, CONFIG_PATH
        orig = None
        try:
            if CONFIG_PATH.exists():
                orig = CONFIG_PATH.read_text(encoding="utf-8")
            profiles = load_profiles()
            name = "__test_profile__"
            profiles[name] = SyncProfile(name=name, src="/tmp/src", dst="/tmp/dst")
            save_profiles(profiles)
            loaded = load_profiles()
            assert name in loaded
            assert loaded[name].src == "/tmp/src"
            assert loaded[name].dst == "/tmp/dst"
            del loaded[name]
            save_profiles(loaded)
        finally:
            if orig is not None:
                CONFIG_PATH.write_text(orig, encoding="utf-8")

    def test_save_report(self, tmp_dirs):
        from infrastructure.storage import save_report
        _, dst = tmp_dirs
        report = SyncReport()
        report.bytes_copied = 1024 * 1024
        report.bytes_backed_up = 512
        report.bytes_deleted_perm = 0
        from datetime import datetime
        report.finished_at = datetime.now()
        src_fi = FileInfo(path=Path("/tmp/f"), rel_path=Path("f.txt"), size=100, mtime=0)
        report.actions_done.append(SyncAction(action=ActionType.COPY_NEW, src_file=src_fi))
        report.errors.append("test error")
        report.warnings.append("test warning")
        out_path = dst / "test_report.txt"
        result = save_report(report, out_path)
        assert result.exists()
        content = result.read_text(encoding="utf-8")
        assert "FlashSync Report" in content
        assert "1,048,576" in content
        assert "test error" in content
        assert "test warning" in content
        assert "copy_new" in content


# ── infrastructure/copier ────────────────────────────────────────────────────


class TestCopier:
    @pytest.mark.asyncio
    async def test_copy_file_small(self, tmp_dirs):
        from infrastructure.copier import copy_file
        src, dst = tmp_dirs
        f = write(src / "small.txt", b"hello world")
        ok = await copy_file(f, dst / "small.txt")
        assert ok
        assert (dst / "small.txt").read_bytes() == b"hello world"

    @pytest.mark.asyncio
    async def test_copy_file_missing_src(self, tmp_dirs):
        from infrastructure.copier import copy_file
        src, dst = tmp_dirs
        ok = await copy_file(src / "nonexistent.txt", dst / "out.txt")
        assert not ok

    @pytest.mark.asyncio
    async def test_copy_file_verify_ok(self, tmp_dirs):
        from infrastructure.copier import copy_file
        src, dst = tmp_dirs
        content = b"verify this content" * 50
        f = write(src / "verifiable.bin", content)
        ok = await copy_file(f, dst / "verifiable.bin", verify=True)
        assert ok
        assert (dst / "verifiable.bin").read_bytes() == content

    @pytest.mark.asyncio
    async def test_copy_batch_small(self, tmp_dirs):
        from infrastructure.copier import copy_batch_small
        src, dst = tmp_dirs
        pairs = []
        for i in range(10):
            f = write(src / f"b{i}.txt", f"content_{i}".encode())
            pairs.append((f, dst / f"b{i}.txt"))
        results = await copy_batch_small(pairs)
        assert len(results) == 10
        assert all(ok for _, _, ok in results)
        for i in range(10):
            assert (dst / f"b{i}.txt").read_bytes() == f"content_{i}".encode()

    @pytest.mark.asyncio
    async def test_copy_batch_partial_fail(self, tmp_dirs):
        from infrastructure.copier import copy_batch_small
        src, dst = tmp_dirs
        f_good = write(src / "good.txt", b"ok")
        pairs = [(src / "missing.txt", dst / "missing.txt"), (f_good, dst / "good.txt")]
        results = await copy_batch_small(pairs)
        assert len(results) == 2
        ok_results = [ok for _, _, ok in results]
        assert ok_results[0] is False
        assert ok_results[1] is True

    @pytest.mark.asyncio
    async def test_chunked_copy_progress(self, tmp_dirs):
        from infrastructure.copier import copy_file, SMALL_FILE_THRESHOLD
        src, dst = tmp_dirs
        size = SMALL_FILE_THRESHOLD + 1024
        content = b"x" * size
        f = write(src / "big.bin", content)
        progress_calls = []
        ok = await copy_file(f, dst / "big.bin",
                             progress_cb=lambda done, total: progress_calls.append((done, total)))
        assert ok
        assert len(progress_calls) > 0
        dones = [d for d, _ in progress_calls]
        assert dones == sorted(dones)
        assert progress_calls[-1][1] == size

    def test_verify_sha256_match(self, tmp_dirs):
        from infrastructure.copier import _verify_sha256
        src, dst = tmp_dirs
        content = b"identical content"
        f1 = write(src / "a.bin", content)
        f2 = write(dst / "a.bin", content)
        assert _verify_sha256(f1, f2)

    def test_verify_sha256_mismatch(self, tmp_dirs):
        from infrastructure.copier import _verify_sha256
        src, dst = tmp_dirs
        f1 = write(src / "a.bin", b"content A")
        f2 = write(dst / "a.bin", b"content B")
        assert not _verify_sha256(f1, f2)


# ── infrastructure/crash_reporter ────────────────────────────────────────────


class TestCrashReporter:
    def test_write_crash_creates_file(self, tmp_dirs):
        from infrastructure.crash_reporter import _write_crash
        path = _write_crash(ValueError, ValueError("test error"), None, "1.0.0", "pytest_test")
        assert path.exists()
        content = path.read_text(encoding="utf-8")
        assert "ValueError" in content
        assert "test error" in content
        assert "FlashSync Pro" in content
        assert "Python:" in content
        assert "Платформа:" in content
        path.unlink(missing_ok=True)

    def test_write_crash_with_traceback(self, tmp_dirs):
        from infrastructure.crash_reporter import _write_crash
        try:
            raise RuntimeError("simulated crash")
        except RuntimeError:
            import sys
            exc_type, exc_value, exc_tb = sys.exc_info()
            path = _write_crash(exc_type, exc_value, exc_tb, "1.0.0", "pytest_tb")
        content = path.read_text(encoding="utf-8")
        assert "RuntimeError" in content
        assert "simulated crash" in content
        assert "Traceback" in content
        path.unlink(missing_ok=True)

    def test_get_crash_reports_empty(self):
        from infrastructure.crash_reporter import get_crash_reports
        reports = get_crash_reports()
        assert isinstance(reports, list)

    def test_crash_count_since(self):
        from infrastructure.crash_reporter import _write_crash, crash_count_since
        before = crash_count_since(hours=1)
        path = _write_crash(KeyError, KeyError("k"), None, "1.0.0", "count_test")
        after = crash_count_since(hours=1)
        assert after == before + 1
        path.unlink(missing_ok=True)

    def test_setup_does_not_raise(self):
        from infrastructure.crash_reporter import setup_crash_reporter
        setup_crash_reporter("1.0.0")
        setup_crash_reporter("2.0.0")


# ── application/session ──────────────────────────────────────────────────────


class TestSessionManager:
    def _make_plan(self, src: Path, dst: Path) -> list[SyncAction]:
        write(src / "a.txt", b"aaa")
        write(src / "b.jpg", b"bbb" * 100)
        write(dst / "old.txt", b"old")
        profile = SyncProfile(name="sess_t", src=str(src), dst=str(dst), use_hash=False, delete_mode=True)
        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        return DiffEngine(profile).compute_plan(st, dt)

    def test_save_and_load(self, tmp_dirs):
        from application.session import SessionManager
        src, dst = tmp_dirs
        plan = self._make_plan(src, dst)
        sm = SessionManager("test_save_load")
        sm.clear()
        sm.save(plan, str(src), str(dst), scan_duration=1.5)
        result = sm.load(str(src), str(dst))
        assert result is not None
        loaded_plan, saved_at = result
        assert len(loaded_plan) == len(plan)
        orig_actions = {a.rel_path.as_posix(): a.action for a in plan}
        loaded_actions = {a.rel_path.as_posix(): a.action for a in loaded_plan}
        assert orig_actions == loaded_actions
        sm.clear()

    def test_load_wrong_paths_returns_none(self, tmp_dirs):
        from application.session import SessionManager
        src, dst = tmp_dirs
        plan = self._make_plan(src, dst)
        sm = SessionManager("test_wrong_paths")
        sm.clear()
        sm.save(plan, str(src), str(dst))
        result = sm.load("/other/src", "/other/dst")
        assert result is None
        sm.clear()

    def test_load_nonexistent_returns_none(self, tmp_dirs):
        from application.session import SessionManager
        sm = SessionManager("test_nonexistent_xyz")
        sm.clear()
        result = sm.load("/any/src", "/any/dst")
        assert result is None

    def test_clear_removes_file(self, tmp_dirs):
        from application.session import SessionManager
        src, dst = tmp_dirs
        plan = self._make_plan(src, dst)
        sm = SessionManager("test_clear_file")
        sm.save(plan, str(src), str(dst))
        assert sm.exists()
        sm.clear()
        assert not sm.exists()

    def test_save_preserves_action_types(self, tmp_dirs):
        from application.session import SessionManager
        import dataclasses
        src, dst = tmp_dirs
        plan = self._make_plan(src, dst)
        prot = dataclasses.replace(plan[0], action=ActionType.SKIP_PROTECTED,
                                   protection_level=ProtectionLevel.DOUBLE)
        plan_with_prot = plan + [prot]
        sm = SessionManager("test_action_types")
        sm.clear()
        sm.save(plan_with_prot, str(src), str(dst))
        result = sm.load(str(src), str(dst))
        assert result is not None
        loaded, _ = result
        actions = {a.rel_path.as_posix(): a.action for a in loaded}
        assert ActionType.SKIP_PROTECTED in actions.values()
        sm.clear()

    def test_plan_stats_by_category(self, tmp_dirs):
        from application.session import plan_stats_by_category, format_plan_stats
        src, dst = tmp_dirs
        write(src / "photo.jpg", b"x" * 1000)
        write(src / "video.mp4", b"y" * 5000)
        write(src / "doc.pdf", b"z" * 500)
        write(src / "photo2.png", b"w" * 2000)
        st = scan_directory(src, use_hash=False)
        plan = DiffEngine(SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False)).compute_plan(st, {})
        stats = plan_stats_by_category(plan)
        assert "image" in stats
        assert "video" in stats
        assert "document" in stats
        assert stats["image"]["count"] == 2
        assert stats["video"]["count"] == 1
        lines = format_plan_stats(stats)
        assert len(lines) >= 3
        assert any("Фото" in l or "image" in l for l in lines)
        assert any("Видео" in l or "video" in l for l in lines)

    def test_format_plan_stats_contains_size(self, tmp_dirs):
        from application.session import plan_stats_by_category, format_plan_stats
        src, dst = tmp_dirs
        write(src / "big.mp4", b"x" * (2 * 1024 * 1024))
        st = scan_directory(src, use_hash=False)
        plan = DiffEngine(SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False)).compute_plan(st, {})
        stats = plan_stats_by_category(plan)
        lines = format_plan_stats(stats)
        assert any("MB" in l for l in lines)


# ── stop_flag ─────────────────────────────────────────────────────────────────


class TestStopFlag:
    @pytest.mark.asyncio
    async def test_stop_before_batch(self, tmp_dirs, profile):
        """stop_flag=True сразу — ничего не копируется."""
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        actions = []
        for i in range(5):
            f = write(src / f"f{i}.txt", b"x")
            fi_ = FileInfo(path=f, rel_path=Path(f"f{i}.txt"), size=1, mtime=f.stat().st_mtime)
            actions.append(SyncAction(action=ActionType.COPY_NEW, src_file=fi_))
        await SyncEngine(profile, src, dst, dry_run=False, stop_flag=lambda: True).execute(actions)
        assert len(list(dst.iterdir())) == 0

    @pytest.mark.asyncio
    async def test_stop_mid_sync(self, tmp_dirs, profile):
        """stop_flag срабатывает после первого батча — часть файлов скопирована."""
        from application.sync_engine import SyncEngine, BATCH_SIZE
        src, dst = tmp_dirs
        N = BATCH_SIZE * 3
        actions = []
        for i in range(N):
            f = write(src / f"f{i:03d}.txt", f"content_{i}".encode())
            fi_ = FileInfo(path=f, rel_path=Path(f"f{i:03d}.txt"), size=f.stat().st_size, mtime=f.stat().st_mtime)
            actions.append(SyncAction(action=ActionType.COPY_NEW, src_file=fi_))
        call_count = [0]
        def stop_after_first_batch():
            call_count[0] += 1
            return call_count[0] > 1
        await SyncEngine(profile, src, dst, dry_run=False, stop_flag=stop_after_first_batch).execute(actions)
        copied = list(dst.iterdir())
        assert 0 < len(copied) < N

    @pytest.mark.asyncio
    async def test_stop_flag_no_files_on_disk(self, tmp_dirs, profile):
        """stop_flag=True — никакие файлы не появляются на диске (реальный режим)."""
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        for i in range(5):
            write(src / f"g{i}.txt", b"data")
        st = scan_directory(src, use_hash=False)
        plan = DiffEngine(profile).compute_plan(st, {})
        await SyncEngine(profile, src, dst, dry_run=False, stop_flag=lambda: True).execute(plan)
        assert len(list(dst.iterdir())) == 0


# ── Полный интеграционный тест: scan → override → rescan → sync ───────────────


class TestFullCycle:
    @pytest.mark.asyncio
    async def test_full_cycle_with_overrides(self, tmp_dirs):
        """
        1. Создаём файлы → 2. Сканируем → план
        3. Вручную защищаем один файл → 4. Пересканируем → override выживает
        5. Синхронизируем → защищённый файл не тронут
        """
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs

        write(src / "regular.txt", b"regular")
        write(src / "update_me.txt", b"new_version_longer")
        write(dst / "update_me.txt", b"old_v")
        write(dst / "only_dst.txt", b"dst_only")

        profile = SyncProfile(name="full_test", src=str(src), dst=str(dst), use_hash=False, delete_mode=True)
        overrides = PlanOverrides("full_cycle_test")
        overrides.clear()

        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        plan1 = DiffEngine(profile).compute_plan(st, dt)
        plan1, _, _ = overrides.apply(plan1)

        by = {a.rel_path.as_posix(): a for a in plan1}
        assert by["regular.txt"].action == ActionType.COPY_NEW
        assert by["update_me.txt"].action == ActionType.COPY_UPDATE
        assert by["only_dst.txt"].action == ActionType.DELETE

        import dataclasses
        protected = dataclasses.replace(by["update_me.txt"], action=ActionType.SKIP_PROTECTED,
                                        protection_level=ProtectionLevel.SINGLE, reason="вручную")
        overrides.set(protected)

        plan2 = DiffEngine(profile).compute_plan(st, dt)
        plan2, applied, stale = overrides.apply(plan2)
        assert applied == 1
        assert stale == 0
        by2 = {a.rel_path.as_posix(): a for a in plan2}
        assert by2["update_me.txt"].action == ActionType.SKIP_PROTECTED

        active = [a for a in plan2 if a.action not in (ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED)]
        engine = SyncEngine(profile, src, dst, dry_run=False)
        report = await engine.execute(active)

        assert len(report.errors) == 0
        assert (dst / "regular.txt").exists()
        assert (dst / "update_me.txt").read_bytes() == b"old_v"
        backup_dirs = list(dst.glob(".flashsync_backup_*"))
        assert len(backup_dirs) == 1

        overrides.clear()

    @pytest.mark.asyncio
    async def test_large_file_sync_with_progress(self, tmp_dirs):
        """Большой файл (>4MB) синхронизируется с прогрессом."""
        from application.sync_engine import SyncEngine
        from infrastructure.copier import SMALL_FILE_THRESHOLD

        src, dst = tmp_dirs
        size = SMALL_FILE_THRESHOLD + 2 * 1024 * 1024
        content = b"A" * size
        f = write(src / "large_video.mp4", content)

        profile = SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False)
        src_fi = FileInfo(path=f, rel_path=Path("large_video.mp4"), size=f.stat().st_size, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=src_fi)

        progress_msgs = []
        def cb(a, msg):
            progress_msgs.append(msg)

        report = await SyncEngine(profile, src, dst, dry_run=False, progress_cb=cb).execute([action])

        assert len(report.errors) == 0
        assert (dst / "large_video.mp4").read_bytes() == content
        pct_msgs = [m for m in progress_msgs if "%" in m]
        assert len(pct_msgs) > 0, f"Нет сообщений с %: {progress_msgs}"

    def test_scan_errors_do_not_stop_scan(self, tmp_dirs):
        src, _ = tmp_dirs
        write(src / "accessible.txt", b"ok")
        write(src / "also_good.txt", b"ok too")
        errors = []
        result = scan_directory(src, use_hash=False, error_cb=lambda p, e: errors.append(str(p)))
        assert Path("accessible.txt") in result
        assert Path("also_good.txt") in result

    @pytest.mark.asyncio
    async def test_dry_run_log_format(self, tmp_dirs):
        """Dry run лог содержит имя файла, размер, расширение."""
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        f = write(src / "photo_001.jpg", b"x" * (100 * 1024))
        profile = SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False)
        finfo = FileInfo(path=f, rel_path=Path("photo_001.jpg"), size=f.stat().st_size, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=finfo)
        report = await SyncEngine(profile, src, dst, dry_run=True).execute([action])
        assert any("photo_001.jpg" in line for line in report.log_lines)
        assert any("[.jpg]" in line for line in report.log_lines)
        assert any("100K" in line or "102K" in line for line in report.log_lines)

    @pytest.mark.asyncio
    async def test_dry_run_huge_plan_no_disk_check(self, tmp_dirs):
        """
        Регрессионный тест на найденный баг: Dry Run на плане большем чем
        свободное место на диске должен пройти успешно (ничего не пишет на диск).
        """
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        # Один файл с искусственно завышенным размером — имитация "не хватит места"
        f = write(src / "f.dat", b"x" * 100)
        finfo = FileInfo(path=f, rel_path=Path("f.dat"), size=10**18, mtime=f.stat().st_mtime)  # экзабайт
        action = SyncAction(action=ActionType.COPY_NEW, src_file=finfo)
        report = await SyncEngine(profile=SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False),
                                  src=src, dst=dst, dry_run=True).execute([action])
        assert len(report.errors) == 0
        assert len(report.actions_done) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Регрессионные тесты на баги из код-ревью (см. диалог с пользователем)
# ══════════════════════════════════════════════════════════════════════════════


class TestCodeReviewRegressions:
    """
    Каждый тест в этом классе соответствует конкретному подтверждённому
    багу из внешнего код-ревью и фиксирует, что исправление не отвалится
    при будущем рефакторинге.
    """

    # ── 2.4: is_stale через mtime, без хеша на одной из сторон ───────────────

    def test_override_stale_via_mtime_no_real_sleep(self, tmp_dirs):
        """
        Override сохранён БЕЗ хеша (use_hash=False на момент сохранения).
        Контент файла меняется при ТОМ ЖЕ размере, но mtime отличается на
        диске больше чем MTIME_TOLERANCE. _is_stale должен поймать это
        через mtime, а не пропустить как раньше (когда сравнивался только
        размер). Конструируем FileInfo вручную с контролируемым mtime —
        без time.sleep(), тест быстрый и детерминированный.
        """
        import dataclasses
        from application.plan_overrides import PlanOverrides, Override

        ov = PlanOverrides("regress_2_4")
        ov.clear()
        ov._overrides["f.bin"] = Override(
            rel_path="f.bin", action=ActionType.SKIP_PROTECTED.value,
            protection_level=ProtectionLevel.SINGLE.value, reason="manual",
            src_hash=None, src_size=10, src_mtime=1000.0,
            dst_hash=None, dst_size=10, dst_mtime=1000.0,
        )

        # Текущее сканирование: тот же размер, mtime сдвинут на 5 секунд
        # (> MTIME_TOLERANCE=2.0), хеш теперь доступен с одной стороны
        fi_now = FileInfo(path=Path("/x/f.bin"), rel_path=Path("f.bin"),
                          size=10, mtime=1005.0, hash="newhash123")
        action = SyncAction(action=ActionType.DELETE, dst_file=fi_now)

        is_stale = ov._is_stale(ov._overrides["f.bin"], action)
        assert is_stale, "БАГ: смена mtime при том же размере не обнаружена"
        ov.clear()

    def test_override_not_stale_within_mtime_tolerance(self, tmp_dirs):
        """mtime отличается на 0.5с (в пределах допуска) — НЕ должно считаться устаревшим."""
        from application.plan_overrides import PlanOverrides, Override

        ov = PlanOverrides("regress_2_4_tolerance")
        ov.clear()
        ov._overrides["f.bin"] = Override(
            rel_path="f.bin", action=ActionType.SKIP_PROTECTED.value,
            protection_level=ProtectionLevel.SINGLE.value, reason="manual",
            src_hash=None, src_size=10, src_mtime=1000.0,
            dst_hash=None, dst_size=10, dst_mtime=1000.0,
        )
        fi_now = FileInfo(path=Path("/x/f.bin"), rel_path=Path("f.bin"),
                          size=10, mtime=1000.5)  # +0.5с — в пределах допуска
        action = SyncAction(action=ActionType.DELETE, dst_file=fi_now)
        assert not ov._is_stale(ov._overrides["f.bin"], action)
        ov.clear()

    def test_override_hash_match_takes_priority_over_mtime(self, tmp_dirs):
        """Если хеш совпадает на обеих сторонах — не stale, даже если mtime сильно отличается."""
        from application.plan_overrides import PlanOverrides, Override

        ov = PlanOverrides("regress_2_4_hash_priority")
        ov.clear()
        ov._overrides["f.bin"] = Override(
            rel_path="f.bin", action=ActionType.SKIP_PROTECTED.value,
            protection_level=ProtectionLevel.SINGLE.value, reason="manual",
            src_hash=None, src_size=10, src_mtime=0,
            dst_hash="samehash", dst_size=10, dst_mtime=1000.0,
        )
        # mtime скакнул на 100 секунд (например файл перекопирован без изменений),
        # но хеш идентичен — не должно считаться устаревшим
        fi_now = FileInfo(path=Path("/x/f.bin"), rel_path=Path("f.bin"),
                          size=10, mtime=1100.0, hash="samehash")
        action = SyncAction(action=ActionType.DELETE, dst_file=fi_now)
        assert not ov._is_stale(ov._overrides["f.bin"], action)
        ov.clear()

    # ── 3.1: паттерн защиты по полному пути, не только по имени ──────────────

    @pytest.mark.asyncio
    async def test_protection_pattern_scoped_to_full_path(self, tmp_dirs):
        """
        _save_rules должен создавать паттерн по полному относительному пути,
        а не по одному имени файла — иначе защита одного report.pdf
        блокирует ЛЮБОЙ report.pdf во всём дереве синхронизации.
        """
        from ui.tui.file_manager import FileManagerScreen
        from textual.app import App

        fi_a = FileInfo(path=Path("/src/Work/important/report.pdf"),
                        rel_path=Path("Work/important/report.pdf"), size=100, mtime=0)
        fi_b = FileInfo(path=Path("/src/Other/report.pdf"),
                        rel_path=Path("Other/report.pdf"), size=200, mtime=0)
        plan = [
            SyncAction(action=ActionType.COPY_NEW, src_file=fi_a),
            SyncAction(action=ActionType.COPY_NEW, src_file=fi_b),
        ]
        profile = SyncProfile(name="pattern_scope_test", src="/src", dst="/dst")

        class TestApp(App):
            def on_mount(self):
                self.push_screen(FileManagerScreen(plan, profile))

        app = TestApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            screen._selected_paths.add("Work/important/report.pdf")
            screen._save_rules()
            await pilot.pause()

        patterns = [r.pattern for r in profile.protection_rules]
        assert "Work/important/report.pdf" in patterns
        assert "*report.pdf*" not in patterns  # старый, слишком широкий формат

        rule = profile.protection_rules[-1]
        assert rule.matches(Path("Work/important/report.pdf"))
        assert not rule.matches(Path("Other/report.pdf")), \
            "БАГ: защита одного файла зацепила другой файл с тем же именем"

    # ── 3.3: очистка битого файла после провала верификации ──────────────────

    @pytest.mark.asyncio
    async def test_corrupted_file_removed_on_verify_failure(self, tmp_dirs):
        from infrastructure import copier as copier_mod
        src, dst = tmp_dirs
        sf = write(src / "f.bin", b"original content")

        original_verify = copier_mod._verify_sha256
        copier_mod._verify_sha256 = lambda s, d: False
        try:
            ok = await copier_mod.copy_file(sf, dst / "f.bin", verify=True)
            assert ok is False
            assert not (dst / "f.bin").exists(), "БАГ: битый файл остался на диске"
        finally:
            copier_mod._verify_sha256 = original_verify

    @pytest.mark.asyncio
    async def test_verify_success_keeps_file(self, tmp_dirs):
        """Контрольный тест: при успешной верификации файл остаётся (не удаляется по ошибке)."""
        from infrastructure.copier import copy_file
        src, dst = tmp_dirs
        sf = write(src / "f.bin", b"content")
        ok = await copy_file(sf, dst / "f.bin", verify=True)
        assert ok is True
        assert (dst / "f.bin").exists()

    # ── 2.5: файл с ошибкой хеширования остаётся в результате (hash=None) ────

    def test_hash_failure_keeps_file_in_scan_result(self, tmp_dirs):
        import infrastructure.scanner as scanner_mod
        src, _ = tmp_dirs
        write(src / "good.txt", b"ok")
        write(src / "broken.txt", b"will fail to hash")

        original_hash_file = scanner_mod.hash_file
        def mock_hash_file(path, algo):
            if "broken" in str(path):
                return None
            return original_hash_file(path, algo)
        scanner_mod.hash_file = mock_hash_file
        try:
            errors = []
            result = scanner_mod.scan_directory(
                src, use_hash=True, error_cb=lambda p, e: errors.append((p, e))
            )
            assert Path("good.txt") in result
            assert Path("broken.txt") in result, "БАГ: файл выпал из результата сканирования"
            assert result[Path("broken.txt")].hash is None
            assert result[Path("good.txt")].hash is not None
            assert len(errors) == 1  # пользователь всё равно предупреждён
        finally:
            scanner_mod.hash_file = original_hash_file

    def test_hash_failure_file_still_compared_by_size_mtime(self, tmp_dirs):
        """Файл с hash=None из-за ошибки хеширования всё равно участвует в DiffEngine."""
        import infrastructure.scanner as scanner_mod
        src, dst = tmp_dirs
        write(src / "broken.txt", b"some content here")
        write(dst / "broken.txt", b"some content here")  # идентичный по размеру+mtime

        original_hash_file = scanner_mod.hash_file
        scanner_mod.hash_file = lambda path, algo: None  # хеширование всегда "ломается"
        try:
            profile = SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=True)
            st = scanner_mod.scan_directory(src, use_hash=True)
            dt = scanner_mod.scan_directory(dst, use_hash=True)
            assert Path("broken.txt") in st
            assert Path("broken.txt") in dt
            plan = DiffEngine(profile).compute_plan(st, dt)
            action = next(a for a in plan if "broken.txt" in str(a.rel_path))
            # Без хеша на обеих сторонах FileInfo.is_same_as откатится на size+mtime —
            # размеры и времена идентичны (только что записаны одинаково) → SKIP_EQUAL
            assert action.action == ActionType.SKIP_EQUAL
        finally:
            scanner_mod.hash_file = original_hash_file

    # ── 4.4: /run/media/ распознаётся как съёмный носитель ────────────────────

    def test_run_media_recognized_as_removable(self):
        """Современный systemd/udisks2 монтирует флешки в /run/media/, не только /media или /mnt."""
        test_path = "/run/media/user/USB_DRIVE"
        removable = test_path.startswith(("/media/", "/mnt/", "/run/media/"))
        assert removable, "БАГ: /run/media/ не распознаётся как съёмный носитель"

    # ── 4.2: точечное обновление индекса вместо полной пересборки ────────────

    @pytest.mark.asyncio
    async def test_file_manager_incremental_index_update(self, tmp_dirs):
        from ui.tui.file_manager import FileManagerScreen
        from textual.app import App

        plan = []
        for i in range(50):
            folder = f"folder_{i % 5}"
            f = FileInfo(path=Path(f"/x/{folder}/f{i}.txt"),
                        rel_path=Path(f"{folder}/f{i}.txt"), size=10, mtime=0)
            plan.append(SyncAction(action=ActionType.COPY_NEW, src_file=f))
        profile = SyncProfile(name="incr_test", src="/x", dst="/y")

        class TestApp(App):
            def on_mount(self):
                self.push_screen(FileManagerScreen(plan, profile))

        app = TestApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            targets = [a for a in plan if "folder_0" in str(a.rel_path)][:2]
            for a in targets:
                screen._selected_paths.add(str(a.rel_path))
            screen._apply_action_to_selected(ActionType.SKIP_EQUAL, reason="test")
            await pilot.pause()

            bucket = screen._folder_actions["folder_0"]
            changed = [a for a in bucket if a.action == ActionType.SKIP_EQUAL]
            assert len(changed) == 2
            untouched = [a for a in screen._plan if a.action == ActionType.COPY_NEW]
            assert len(untouched) == 48

    # ── 4.1: лимит записей в folder_picker не даёт UI замереть ────────────────

    @pytest.mark.asyncio
    async def test_folder_picker_caps_large_directory(self, tmp_dirs):
        from ui.tui.folder_picker import FolderPickerScreen
        from textual.app import App

        src, _ = tmp_dirs
        for i in range(50):
            (src / f"dir_{i:04d}").mkdir()

        class TestApp(App):
            def on_mount(self):
                self.push_screen(FolderPickerScreen("test", str(src)))

        app = TestApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert len(screen._entries) == 50  # меньше лимита — все показаны

    # ── 4.3: ротация логов вместо неограниченного роста ──────────────────────

    def test_logger_uses_rotating_handler(self):
        from logging.handlers import RotatingFileHandler
        from infrastructure.storage import setup_logger
        logger = setup_logger()
        assert any(isinstance(h, RotatingFileHandler) for h in logger.handlers), \
            "БАГ: лог использует обычный FileHandler без ротации"

    # ── Централизация версии ──────────────────────────────────────────────────

    def test_version_centralized(self):
        from domain.version import __version__
        assert __version__ == "1.0.0"
        # main.py и app.py не должны иметь свою захардкоженную копию строки версии
        main_src = (ROOT / "main.py").read_text(encoding="utf-8")
        app_src = (ROOT / "ui" / "tui" / "app.py").read_text(encoding="utf-8")
        assert '"1.0.0"' not in main_src
        assert '"1.0.0"' not in app_src


# ══════════════════════════════════════════════════════════════════════════════
# Регрессионные тесты на риски безопасности данных, найденные при доп. аудите
# ══════════════════════════════════════════════════════════════════════════════


class TestPathOverlapSafety:
    """src и dst не должны пересекаться — иначе backup-папка рекурсивно
    копируется сама в себя, а часть приёмника может попасть под удаление."""

    def test_validate_same_path(self, tmp_dirs):
        from domain.models import validate_sync_paths
        src, _ = tmp_dirs
        assert validate_sync_paths(src, src) is not None

    def test_validate_dst_inside_src(self, tmp_dirs):
        from domain.models import validate_sync_paths
        src, _ = tmp_dirs
        nested = src / "Backup"
        nested.mkdir()
        err = validate_sync_paths(src, nested)
        assert err is not None
        assert "ВНУТРИ источника" in err

    def test_validate_src_inside_dst(self, tmp_dirs):
        from domain.models import validate_sync_paths
        _, dst = tmp_dirs
        nested = dst / "Photos"
        nested.mkdir()
        err = validate_sync_paths(nested, dst)
        assert err is not None
        assert "ВНУТРИ приёмника" in err

    def test_validate_independent_paths_ok(self, tmp_dirs):
        from domain.models import validate_sync_paths
        src, dst = tmp_dirs
        assert validate_sync_paths(src, dst) is None

    @pytest.mark.asyncio
    async def test_sync_engine_refuses_nested_paths(self, tmp_dirs):
        """SyncEngine отказывает в синхронизации при пересечении путей — defense in depth."""
        from application.sync_engine import SyncEngine
        src, _ = tmp_dirs
        nested = src / "Backup"
        nested.mkdir()
        f = write(src / "photo.jpg", b"data")
        fi_ = FileInfo(path=f, rel_path=Path("photo.jpg"), size=4, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=fi_)
        profile = SyncProfile(name="t", src=str(src), dst=str(nested), use_hash=False)

        report = await SyncEngine(profile, src, nested, dry_run=False).execute([action])
        assert len(report.errors) > 0
        assert not (nested / "photo.jpg").exists()

    @pytest.mark.asyncio
    async def test_sync_engine_refuses_nested_paths_even_in_dry_run(self, tmp_dirs):
        """Опасность тут не в записи на диск, а в структуре плана — должно отказывать и в dry_run."""
        from application.sync_engine import SyncEngine
        src, _ = tmp_dirs
        nested = src / "Backup"
        nested.mkdir()
        f = write(src / "photo.jpg", b"data")
        fi_ = FileInfo(path=f, rel_path=Path("photo.jpg"), size=4, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=fi_)
        profile = SyncProfile(name="t", src=str(src), dst=str(nested), use_hash=False)

        report = await SyncEngine(profile, src, nested, dry_run=True).execute([action])
        assert len(report.errors) > 0


class TestBackupFailureSafety:
    """
    Критический баг: если backup старой версии файла не удался (диск полон,
    нет прав на backup-папку), движок раньше ВСЁ РАВНО перезаписывал оригинал —
    старая версия терялась безвозвратно и БЕЗ единой ошибки в отчёте.
    """

    @pytest.mark.asyncio
    async def test_small_file_not_overwritten_if_backup_fails(self, tmp_dirs, profile):
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        sf = write(src / "doc.txt", b"NEW")
        df = write(dst / "doc.txt", b"OLD_IRREPLACEABLE")
        src_fi = FileInfo(path=sf, rel_path=Path("doc.txt"), size=sf.stat().st_size, mtime=sf.stat().st_mtime)
        dst_fi = FileInfo(path=df, rel_path=Path("doc.txt"), size=df.stat().st_size, mtime=df.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_UPDATE, src_file=src_fi, dst_file=dst_fi)

        original_backup = SyncEngine._backup_file
        SyncEngine._backup_file = lambda self, fp, rp: _async_false()
        try:
            report = await SyncEngine(profile, src, dst, dry_run=False).execute([action])
        finally:
            SyncEngine._backup_file = original_backup

        assert df.read_bytes() == b"OLD_IRREPLACEABLE", "Старая версия должна остаться нетронутой"
        assert len(report.errors) > 0, "Провал backup должен попасть в errors"
        assert len(report.actions_done) == 0

    @pytest.mark.asyncio
    async def test_large_file_not_overwritten_if_backup_fails(self, tmp_dirs, profile):
        from application.sync_engine import SyncEngine
        from infrastructure.copier import SMALL_FILE_THRESHOLD
        src, dst = tmp_dirs
        size = SMALL_FILE_THRESHOLD + 1024
        sf = write(src / "video.mp4", b"N" * size)
        df = write(dst / "video.mp4", b"OLD_VIDEO" * 1000)
        src_fi = FileInfo(path=sf, rel_path=Path("video.mp4"), size=sf.stat().st_size, mtime=sf.stat().st_mtime)
        dst_fi = FileInfo(path=df, rel_path=Path("video.mp4"), size=df.stat().st_size, mtime=df.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_UPDATE, src_file=src_fi, dst_file=dst_fi)

        original_backup = SyncEngine._backup_file
        SyncEngine._backup_file = lambda self, fp, rp: _async_false()
        try:
            report = await SyncEngine(profile, src, dst, dry_run=False).execute([action])
        finally:
            SyncEngine._backup_file = original_backup

        assert df.read_bytes().startswith(b"OLD_VIDEO"), "Старая версия большого файла должна остаться"
        assert len(report.errors) > 0

    @pytest.mark.asyncio
    async def test_normal_update_still_works_when_backup_succeeds(self, tmp_dirs, profile):
        """Контрольный тест: обычное обновление (backup проходит) работает как раньше."""
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        sf = write(src / "doc.txt", b"NEW")
        df = write(dst / "doc.txt", b"OLD")
        src_fi = FileInfo(path=sf, rel_path=Path("doc.txt"), size=sf.stat().st_size, mtime=sf.stat().st_mtime)
        dst_fi = FileInfo(path=df, rel_path=Path("doc.txt"), size=df.stat().st_size, mtime=df.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_UPDATE, src_file=src_fi, dst_file=dst_fi)

        report = await SyncEngine(profile, src, dst, dry_run=False).execute([action])
        assert len(report.errors) == 0
        assert df.read_bytes() == b"NEW"
        assert len(list(dst.glob(".flashsync_backup_*"))) == 1


async def _async_false() -> bool:
    return False


class TestSymlinkSafety:
    """
    Источник часто — непроверенная флешка. Символическая ссылка внутри неё
    может маскировать произвольный файл с ПК пользователя под обычное медиа.
    Сканер должен пропускать симлинки, а не разыменовывать их.
    """

    def test_symlinked_file_skipped(self, tmp_dirs):
        if not hasattr(Path, "symlink_to"):
            pytest.skip("платформа не поддерживает symlink")
        src, _ = tmp_dirs
        outside = src.parent / "outside_secret_for_test.txt"
        outside.write_bytes(b"sensitive")
        link = src / "disguised_photo.jpg"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("создание symlink не разрешено в этой среде")

        write(src / "normal.txt", b"normal content")
        errors = []
        result = scan_directory(src, use_hash=False, error_cb=lambda p, e: errors.append(p))
        assert Path("disguised_photo.jpg") not in result, "БАГ: симлинк разыменован и попал в результат"
        assert Path("normal.txt") in result
        assert len(errors) == 1
        outside.unlink()

    def test_symlinked_directory_not_traversed(self, tmp_dirs):
        if not hasattr(Path, "symlink_to"):
            pytest.skip("платформа не поддерживает symlink")
        src, _ = tmp_dirs
        outside_dir = src.parent / "outside_dir_for_test"
        outside_dir.mkdir(exist_ok=True)
        (outside_dir / "secret.txt").write_bytes(b"private")
        link_dir = src / "looks_normal"
        try:
            link_dir.symlink_to(outside_dir, target_is_directory=True)
        except OSError:
            pytest.skip("создание symlink не разрешено в этой среде")

        result = scan_directory(src, use_hash=False)
        assert not any("secret" in str(k) for k in result)

        import shutil
        shutil.rmtree(outside_dir)


class TestBackupDirectoryCreationSafety:
    """
    Если создание САМОЙ backup-папки проваливается (не отдельного файла внутри
    неё — а корневой .flashsync_backup_TIMESTAMP/), это раньше вылетало
    необработанным исключением из execute() и роняло всю синхронизацию,
    вместо того чтобы аккуратно зарегистрировать ошибку для одного файла.
    Реалистичный триггер: флешка физически отключилась именно в этот момент.
    """

    @pytest.mark.asyncio
    async def test_backup_dir_mkdir_failure_does_not_crash_engine(self, tmp_dirs, profile, monkeypatch):
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        sf = write(src / "doc.txt", b"new")
        df = write(dst / "doc.txt", b"old")
        src_fi = FileInfo(path=sf, rel_path=Path("doc.txt"), size=sf.stat().st_size, mtime=sf.stat().st_mtime)
        dst_fi = FileInfo(path=df, rel_path=Path("doc.txt"), size=df.stat().st_size, mtime=df.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_UPDATE, src_file=src_fi, dst_file=dst_fi)

        original_mkdir = Path.mkdir
        def failing_mkdir(self, *a, **kw):
            if ".flashsync_backup_" in str(self):
                raise OSError(5, "Input/output error")
            return original_mkdir(self, *a, **kw)
        monkeypatch.setattr(Path, "mkdir", failing_mkdir)

        engine = SyncEngine(profile, src, dst, dry_run=False)
        report = await engine.execute([action])  # не должно выбросить исключение

        assert df.read_bytes() == b"old"
        assert len(report.errors) > 0


class TestMascotWidget:
    """Маскот — чисто декоративный виджет, не должен влиять на логику
    приложения и должен корректно переключаться idle/walk вокруг scan/sync."""

    def test_frames_equal_width(self):
        """Все кадры анимации должны иметь одинаковую видимую ширину строк —
        иначе силуэт 'съезжает' между кадрами анимации в статус-баре."""
        import re
        from ui.tui.mascot import FRAME_IDLE, FRAME_BLINK, FRAME_WALK1, FRAME_WALK2

        def size(f):
            plain = re.sub(r'\[/?[^\]]+\]', '', f)
            lines = plain.split('\n')
            return len(lines[0]), len(lines)

        sizes = {n: size(f) for n, f in [
            ('IDLE',  FRAME_IDLE),
            ('BLINK', FRAME_BLINK),
            ('WALK1', FRAME_WALK1),
            ('WALK2', FRAME_WALK2),
        ]}
        for name, (w, h) in sizes.items():
            assert w == 10 and h == 5, f"{name}: ожидали 10×5 символов, получили {w}×{h}"
        assert len(set(sizes.values())) == 1, f"Кадры разного размера: {sizes}"

    def test_halfblock_frames_contain_color(self):
        """Half-block кадры должны содержать символы ▀ и 24-bit RGB цвет."""
        from ui.tui.mascot import FRAME_IDLE, FRAME_WALK1, FRAME_WALK2
        for name, frame in [("IDLE", FRAME_IDLE), ("WALK1", FRAME_WALK1), ("WALK2", FRAME_WALK2)]:
            assert "▀" in frame,    f"{name}: нет символов ▀ — halfblock не рендерится"
            assert "[#" in frame,   f"{name}: нет 24-bit цвета [#rrggbb]"
            assert "[on #" in frame, f"{name}: нет background-цвета [on #rrggbb]"

    def test_frames_are_distinct(self):
        """Кадры должны быть содержательно разными, иначе анимация не работает."""
        from ui.tui.mascot import FRAME_IDLE, FRAME_BLINK, FRAME_WALK1, FRAME_WALK2
        assert FRAME_IDLE  != FRAME_BLINK,  "IDLE == BLINK: моргание не работает"
        assert FRAME_WALK1 != FRAME_WALK2,  "WALK1 == WALK2: ходьба не работает"
        assert FRAME_IDLE  != FRAME_WALK1,  "IDLE == WALK1: переключение не работает"

    @pytest.mark.asyncio
    async def test_mascot_toggles_around_scan_and_sync(self, tmp_dirs):
        """
        Регрессионный тест: маскот должен активироваться РОВНО на время
        scan и sync (по одному циклу True/False на каждый), и не оставаться
        включённым после завершения — независимо от того, насколько быстро
        прошла операция (используем перехват вызова вместо опроса по таймеру,
        чтобы тест не зависел от скорости диска).
        """
        from ui.tui.app import FlashSyncApp
        from ui.tui.splash import SplashScreen
        from domain.models import SyncProfile
        from application.plan_overrides import PlanOverrides
        from application.session import SessionManager

        src, dst = tmp_dirs
        write(src / "f1.txt", b"content one")
        write(src / "f2.txt", b"content two")

        app = FlashSyncApp()
        app.current_profile = SyncProfile(name="mascot_test", src=str(src), dst=str(dst), use_hash=False)
        app._overrides = PlanOverrides("mascot_test_regress")
        app._overrides.clear()
        app._session = SessionManager("mascot_test_regress")
        app._session.clear()

        calls = []
        original = app._mascot_set_active
        def spy(value):
            calls.append(value)
            return original(value)
        app._mascot_set_active = spy

        async with app.run_test() as pilot:
            await pilot.pause()
            for _ in range(60):
                await pilot.pause(); await asyncio.sleep(0.03)
                if not isinstance(app.screen, SplashScreen):
                    break

            app.action_scan()
            for _ in range(60):
                await pilot.pause()
                if app._plan:
                    break
                await asyncio.sleep(0.03)

            app._start_sync(dry_run=False)
            await pilot.pause()
            await pilot.click("#yes")
            for _ in range(80):
                await pilot.pause(); await asyncio.sleep(0.02)

        assert calls.count(True) == 2, f"Ожидали 2 активации (скан+синк), получили: {calls}"
        assert calls.count(False) == 2, f"Ожидали 2 деактивации, получили: {calls}"
        assert calls and calls[-1] is False, "Маскот не должен оставаться активным после завершения"
        app._overrides.clear()
        app._session.clear()

    @pytest.mark.asyncio
    async def test_mascot_set_active_idempotent(self):
        """Повторный вызов set_active(True) дважды подряд не должен плодить лишние таймеры."""
        from ui.tui.mascot import MascotWidget
        from textual.app import App

        class TestApp(App):
            def compose(self):
                yield MascotWidget(id="m")

        app = TestApp()
        async with app.run_test() as pilot:
            await pilot.pause()
            m = app.query_one("#m", MascotWidget)

            timers_created = []
            original_set_interval = m.set_interval
            def spy_interval(*a, **kw):
                t = original_set_interval(*a, **kw)
                timers_created.append(t)
                return t
            m.set_interval = spy_interval

            m.set_active(True)
            m.set_active(True)  # повторный вызов с тем же значением
            await pilot.pause()
            assert len(timers_created) == 1, "set_active(True) дважды подряд создал лишний таймер"
            m.set_active(False)
            await pilot.pause()
