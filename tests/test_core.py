"""
tests/test_core.py — Полный тест-сьют FlashSync.
Запуск: pytest tests/test_core.py -v
"""
from __future__ import annotations
import asyncio, os, tempfile, time
from pathlib import Path
import pytest
import sys
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from domain.models import (FileInfo, SyncAction, ActionType, ProtectionLevel,
                            ProtectionRule, SyncProfile, SyncReport, _classify)
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
    return SyncProfile(name="test", src=str(src), dst=str(dst), use_hash=True,
                       delete_mode=True, protection_rules=[
                           ProtectionRule("*important*", ProtectionLevel.DOUBLE),
                           ProtectionRule("*contract*",  ProtectionLevel.SINGLE),
                       ])

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
    def test_code(self):  assert _classify(".py")  == "code"

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
        assert verify_copy(write(src/"f.dat", content), write(dst/"f.dat", content))

    def test_verify_fail(self, tmp_dirs):
        src, dst = tmp_dirs
        assert not verify_copy(write(src/"f.dat", b"orig"), write(dst/"f.dat", b"diff"))

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
        result = scan_directory(src, use_hash=False,
                                error_cb=lambda p, e: errors.append(p))
        assert Path("good.txt") in result  # хорошие файлы сканируются

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
        write(src / "doc.pdf",   b"y" * 500)
        write(src / "song.mp3",  b"z" * 2000)
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
        plan = DiffEngine(profile).compute_plan(
            {}, {Path("personal_data.txt"): fi(dst, "personal_data.txt", b"x")})
        # personal не в правилах profile — проверяем contract
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
        b_action = next(a for a in plan if "b.txt" in str(a.rel_path))
        protected = dataclasses.replace(b_action, action=ActionType.SKIP_PROTECTED,
                                        protection_level=ProtectionLevel.SINGLE,
                                        reason="вручную")
        ov.set(protected)
        # Повторно строим план
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
        # Создаём файл только в dst (будет DELETE)
        write(dst / "will_change.txt", b"original_v1")
        profile = SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False, delete_mode=True)
        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        plan = DiffEngine(profile).compute_plan(st, dt)

        del_action = next(a for a in plan if "will_change" in str(a.rel_path))
        assert del_action.action == ActionType.DELETE

        ov = PlanOverrides("test_stale")
        ov.clear()  # сброс на случай остатков от предыдущего теста
        # Защищаем этот файл вручную
        ov.set(dataclasses.replace(del_action, action=ActionType.SKIP_PROTECTED,
                                   protection_level=ProtectionLevel.SINGLE,
                                   reason="вручную"))
        assert ov.count() == 1

        # Файл изменился на диске (другой размер)
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
        import dataclasses
        plan = self._plan(src, dst)
        ov = PlanOverrides("test_clear")
        ov.set_many(plan[:2])
        assert ov.count() == 2
        ov.clear()
        assert ov.count() == 0

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
        """Защищённые файлы не копируются движком (они SKIP_PROTECTED)."""
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        sf = write(src / "important.txt", b"new")
        df = write(dst / "important.txt", b"PROTECTED")
        src_fi = FileInfo(path=sf, rel_path=Path("important.txt"), size=sf.stat().st_size, mtime=sf.stat().st_mtime)
        dst_fi = FileInfo(path=df, rel_path=Path("important.txt"), size=df.stat().st_size, mtime=df.stat().st_mtime)
        action = SyncAction(action=ActionType.SKIP_PROTECTED, src_file=src_fi, dst_file=dst_fi,
                            protection_level=ProtectionLevel.DOUBLE)
        report = await SyncEngine(profile, src, dst, dry_run=False).execute([action])
        # SKIP_PROTECTED — не выполняется
        assert (dst / "important.txt").read_bytes() == b"PROTECTED"

    @pytest.mark.asyncio
    async def test_batch_small_files(self, tmp_dirs, profile):
        """20 маленьких файлов копируются батчами эффективно."""
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
        # Батчей должно быть меньше чем файлов
        batch_msgs = [m for m in msgs if "Копирование" in m]
        assert len(batch_msgs) < 20

    @pytest.mark.asyncio
    async def test_disk_space_check(self, tmp_dirs, profile):
        """Если нет места — отчёт содержит ошибку, файлы не трогаются."""
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        f = write(src / "big.dat", b"x" * 1024)
        finfo = FileInfo(path=f, rel_path=Path("big.dat"), size=10**15, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=finfo)
        engine = SyncEngine(profile, src, dst, dry_run=False)
        report = await engine.execute([action])
        assert len(report.errors) > 0
        assert "место" in report.errors[0].lower() or "недостаточно" in report.errors[0].lower()

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
        # С delete_mode=False — ничего не удаляется
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


# ══════════════════════════════════════════════════════════════════════════════
# Дополнительные тесты — покрытие ранее непокрытых модулей
# ══════════════════════════════════════════════════════════════════════════════

# ── infrastructure/storage ────────────────────────────────────────────────────

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
        import json, os
        # Используем временный config файл
        import tempfile
        orig = None
        try:
            # Читаем текущий если есть
            if CONFIG_PATH.exists():
                orig = CONFIG_PATH.read_text(encoding="utf-8")

            profiles = load_profiles()
            name = "__test_profile__"
            from domain.models import SyncProfile
            profiles[name] = SyncProfile(name=name, src="/tmp/src", dst="/tmp/dst")
            save_profiles(profiles)

            loaded = load_profiles()
            assert name in loaded
            assert loaded[name].src == "/tmp/src"
            assert loaded[name].dst == "/tmp/dst"

            # Чистим
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

        # Добавляем actions для stats
        import dataclasses
        src_fi = FileInfo(path=Path("/tmp/f"), rel_path=Path("f.txt"), size=100, mtime=0)
        report.actions_done.append(
            SyncAction(action=ActionType.COPY_NEW, src_file=src_fi)
        )
        report.errors.append("test error")
        report.warnings.append("test warning")

        out_path = dst / "test_report.txt"
        result = save_report(report, out_path)
        assert result.exists()
        content = result.read_text(encoding="utf-8")
        assert "FlashSync Report" in content
        assert "1,048,576" in content  # bytes_copied
        assert "test error" in content
        assert "test warning" in content
        assert "copy_new" in content


# ── infrastructure/copier ─────────────────────────────────────────────────────

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
        """Если один файл в батче недоступен — остальные копируются."""
        from infrastructure.copier import copy_batch_small
        src, dst = tmp_dirs
        f_good = write(src / "good.txt", b"ok")
        pairs = [
            (src / "missing.txt", dst / "missing.txt"),  # не существует
            (f_good, dst / "good.txt"),
        ]
        results = await copy_batch_small(pairs)
        assert len(results) == 2
        ok_results = [ok for _, _, ok in results]
        assert ok_results[0] is False   # missing — ошибка
        assert ok_results[1] is True    # good — ок

    @pytest.mark.asyncio
    async def test_chunked_copy_progress(self, tmp_dirs):
        """Чанковое копирование вызывает progress_cb с нарастающим счётчиком."""
        from infrastructure.copier import copy_file, SMALL_FILE_THRESHOLD
        src, dst = tmp_dirs
        # Файл больше порога чтобы использовался chunked путь
        size = SMALL_FILE_THRESHOLD + 1024
        content = b"x" * size
        f = write(src / "big.bin", content)
        progress_calls = []
        ok = await copy_file(f, dst / "big.bin",
                             progress_cb=lambda done, total: progress_calls.append((done, total)))
        assert ok
        assert len(progress_calls) > 0
        # Прогресс монотонно возрастает
        dones = [d for d, _ in progress_calls]
        assert dones == sorted(dones)
        # Последнее значение = полный размер
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
        from infrastructure.crash_reporter import _write_crash, CRASH_DIR
        path = _write_crash(
            ValueError, ValueError("test error"), None,
            "1.0.0", "pytest_test"
        )
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
        from infrastructure.crash_reporter import get_crash_reports, CRASH_DIR
        # Просто не падает
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
        # Повторный вызов не должен падать
        setup_crash_reporter("1.0.0")
        setup_crash_reporter("2.0.0")


# ── Полный интеграционный тест: scan → override → rescan → sync ───────────────

class TestFullCycle:
    @pytest.mark.asyncio
    async def test_full_cycle_with_overrides(self, tmp_dirs):
        """
        Полный цикл:
        1. Создаём файлы
        2. Сканируем → план
        3. Вручную защищаем один файл
        4. Пересканируем → override выживает
        5. Синхронизируем → защищённый файл не тронут
        """
        from application.sync_engine import SyncEngine

        src, dst = tmp_dirs

        # Файлы
        write(src / "regular.txt",   b"regular")
        write(src / "update_me.txt", b"new_version_longer")
        write(dst / "update_me.txt", b"old_v")
        write(dst / "only_dst.txt",  b"dst_only")

        profile = SyncProfile(
            name="full_test", src=str(src), dst=str(dst),
            use_hash=False, delete_mode=True,
        )
        overrides = PlanOverrides("full_cycle_test")
        overrides.clear()

        # Первое сканирование
        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        plan1 = DiffEngine(profile).compute_plan(st, dt)
        plan1, _, _ = overrides.apply(plan1)

        by = {a.rel_path.as_posix(): a for a in plan1}
        assert by["regular.txt"].action   == ActionType.COPY_NEW
        assert by["update_me.txt"].action == ActionType.COPY_UPDATE
        assert by["only_dst.txt"].action  == ActionType.DELETE

        # Пользователь защищает update_me.txt
        import dataclasses
        protected = dataclasses.replace(
            by["update_me.txt"],
            action=ActionType.SKIP_PROTECTED,
            protection_level=ProtectionLevel.SINGLE,
            reason="вручную"
        )
        overrides.set(protected)

        # Второе сканирование — файлы не изменились
        plan2 = DiffEngine(profile).compute_plan(st, dt)
        plan2, applied, stale = overrides.apply(plan2)
        assert applied == 1
        assert stale   == 0
        by2 = {a.rel_path.as_posix(): a for a in plan2}
        assert by2["update_me.txt"].action == ActionType.SKIP_PROTECTED

        # Синхронизация
        active = [a for a in plan2
                  if a.action not in (ActionType.SKIP_EQUAL, ActionType.SKIP_PROTECTED)]
        engine = SyncEngine(profile, src, dst, dry_run=False)
        report = await engine.execute(active)

        assert len(report.errors) == 0
        assert (dst / "regular.txt").exists()
        # update_me.txt НЕ обновлён
        assert (dst / "update_me.txt").read_bytes() == b"old_v"
        # only_dst.txt убран в backup
        backup_dirs = list(dst.glob(".flashsync_backup_*"))
        assert len(backup_dirs) == 1

        overrides.clear()

    @pytest.mark.asyncio
    async def test_large_file_sync_with_progress(self, tmp_dirs):
        """Большой файл (>4MB) синхронизируется с прогрессом."""
        from application.sync_engine import SyncEngine
        from infrastructure.copier import SMALL_FILE_THRESHOLD

        src, dst = tmp_dirs
        size = SMALL_FILE_THRESHOLD + 2 * 1024 * 1024  # 6 MB
        content = b"A" * size
        f = write(src / "large_video.mp4", content)

        profile = SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False)
        src_fi = FileInfo(path=f, rel_path=Path("large_video.mp4"),
                          size=f.stat().st_size, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=src_fi)

        progress_msgs = []
        def cb(a, msg):
            progress_msgs.append(msg)

        report = await SyncEngine(profile, src, dst, dry_run=False, progress_cb=cb).execute([action])

        assert len(report.errors) == 0
        assert (dst / "large_video.mp4").read_bytes() == content
        # Для большого файла должны быть прогресс-сообщения с %
        pct_msgs = [m for m in progress_msgs if "%" in m]
        assert len(pct_msgs) > 0, f"Нет сообщений с %: {progress_msgs}"

    def test_scan_errors_do_not_stop_scan(self, tmp_dirs):
        """Ошибки доступа к файлам не останавливают сканирование."""
        src, _ = tmp_dirs
        write(src / "accessible.txt", b"ok")
        write(src / "also_good.txt",  b"ok too")

        errors = []
        result = scan_directory(
            src, use_hash=False,
            error_cb=lambda p, e: errors.append(str(p))
        )
        # Хорошие файлы всё равно просканированы
        assert Path("accessible.txt") in result
        assert Path("also_good.txt") in result

    @pytest.mark.asyncio
    async def test_dry_run_log_format(self, tmp_dirs):
        """Dry run лог содержит имя файла, размер, расширение."""
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        f = write(src / "photo_001.jpg", b"x" * (100 * 1024))
        profile = SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False)
        finfo = FileInfo(path=f, rel_path=Path("photo_001.jpg"),
                         size=f.stat().st_size, mtime=f.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_NEW, src_file=finfo)
        report = await SyncEngine(profile, src, dst, dry_run=True).execute([action])
        assert any("photo_001.jpg" in line for line in report.log_lines)
        assert any("[.jpg]" in line for line in report.log_lines)
        assert any("100K" in line or "102K" in line for line in report.log_lines)


# ══════════════════════════════════════════════════════════════════════════════
# Тесты session.py и новых возможностей sync_engine
# ══════════════════════════════════════════════════════════════════════════════

class TestSessionManager:
    def _make_plan(self, src: Path, dst: Path) -> list[SyncAction]:
        write(src / "a.txt", b"aaa")
        write(src / "b.jpg", b"bbb" * 100)
        write(dst / "old.txt", b"old")
        profile = SyncProfile(name="sess_t", src=str(src), dst=str(dst),
                              use_hash=False, delete_mode=True)
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
        # Действия совпадают
        orig_actions  = {a.rel_path.as_posix(): a.action for a in plan}
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
        # Запрашиваем с другими путями — должен вернуть None
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
        """Все типы ActionType сериализуются и десериализуются корректно."""
        from application.session import SessionManager
        import dataclasses
        src, dst = tmp_dirs
        plan = self._make_plan(src, dst)
        # Добавляем SKIP_PROTECTED вручную
        fi = FileInfo(path=src/"a.txt", rel_path=Path("a.txt"), size=3, mtime=0)
        prot = dataclasses.replace(
            plan[0], action=ActionType.SKIP_PROTECTED,
            protection_level=ProtectionLevel.DOUBLE
        )
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
        write(src / "photo.jpg",  b"x" * 1000)
        write(src / "video.mp4",  b"y" * 5000)
        write(src / "doc.pdf",    b"z" * 500)
        write(src / "photo2.png", b"w" * 2000)

        st = scan_directory(src, use_hash=False)
        plan = DiffEngine(
            SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False)
        ).compute_plan(st, {})

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
        write(src / "big.mp4", b"x" * (2 * 1024 * 1024))  # 2 MB
        st = scan_directory(src, use_hash=False)
        plan = DiffEngine(
            SyncProfile(name="t", src=str(src), dst=str(dst), use_hash=False)
        ).compute_plan(st, {})
        stats = plan_stats_by_category(plan)
        lines = format_plan_stats(stats)
        # Должен быть размер в MB
        assert any("MB" in l for l in lines)


class TestStopFlag:
    @pytest.mark.asyncio
    async def test_stop_before_batch(self, tmp_dirs, profile):
        """stop_flag=True сразу — ничего не копируется."""
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        actions = []
        for i in range(5):
            f = write(src / f"f{i}.txt", b"x")
            fi = FileInfo(path=f, rel_path=Path(f"f{i}.txt"),
                          size=1, mtime=f.stat().st_mtime)
            actions.append(SyncAction(action=ActionType.COPY_NEW, src_file=fi))

        report = await SyncEngine(
            profile, src, dst, dry_run=False,
            stop_flag=lambda: True
        ).execute(actions)

        assert len(list(dst.iterdir())) == 0  # ничего не скопировано

    @pytest.mark.asyncio
    async def test_stop_mid_sync(self, tmp_dirs, profile):
        """stop_flag срабатывает после N батчей — часть файлов скопирована."""
        from application.sync_engine import SyncEngine, BATCH_SIZE
        src, dst = tmp_dirs
        N = BATCH_SIZE * 3  # 3 батча
        actions = []
        for i in range(N):
            f = write(src / f"f{i:03d}.txt", f"content_{i}".encode())
            fi = FileInfo(path=f, rel_path=Path(f"f{i:03d}.txt"),
                          size=f.stat().st_size, mtime=f.stat().st_mtime)
            actions.append(SyncAction(action=ActionType.COPY_NEW, src_file=fi))

        # Останавливаем после первого батча
        call_count = [0]
        def stop_after_first_batch():
            call_count[0] += 1
            return call_count[0] > 1  # True начиная со второго батча

        report = await SyncEngine(
            profile, src, dst, dry_run=False,
            stop_flag=stop_after_first_batch
        ).execute(actions)

        # Скопирован только первый батч
        copied = list(dst.iterdir())
        assert 0 < len(copied) < N  # больше 0, но не все

    @pytest.mark.asyncio
    async def test_stop_flag_no_files_on_disk(self, tmp_dirs, profile):
        """stop_flag=True — никакие файлы не появляются на диске."""
        from application.sync_engine import SyncEngine
        src, dst = tmp_dirs
        for i in range(5):
            f = write(src / f"g{i}.txt", b"data")
            fi = FileInfo(path=f, rel_path=Path(f"g{i}.txt"),
                          size=4, mtime=f.stat().st_mtime)

        # Реальная синхронизация с stop_flag=True
        st = scan_directory(src, use_hash=False)
        from application.differ import DiffEngine
        plan = DiffEngine(profile).compute_plan(st, {})
        report = await SyncEngine(
            profile, src, dst, dry_run=False,
            stop_flag=lambda: True
        ).execute(plan)

        # dst должен быть пустым
        assert len(list(dst.iterdir())) == 0


class TestPlanOverridesIsStale:
    def test_is_stale_by_size_change(self, tmp_dirs):
        """Override устаревает если изменился размер файла."""
        import dataclasses
        from application.plan_overrides import PlanOverrides
        src, dst = tmp_dirs
        write(src / "f.txt", b"original")
        write(dst / "f.txt", b"OLD_VER")

        profile = SyncProfile(name="stale_t", src=str(src), dst=str(dst),
                              use_hash=False)
        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        plan = DiffEngine(profile).compute_plan(st, dt)

        ov = PlanOverrides("stale_size_test")
        ov.clear()
        action = next(a for a in plan if "f.txt" in str(a.rel_path))
        protected = dataclasses.replace(action, action=ActionType.SKIP_PROTECTED,
                                        protection_level=ProtectionLevel.SINGLE,
                                        reason="manual")
        ov.set(protected)

        # Меняем файл — другой размер
        write(dst / "f.txt", b"completely_new_content_much_longer")
        dt2 = scan_directory(dst, use_hash=False)
        plan2 = DiffEngine(profile).compute_plan(st, dt2)
        _, applied, stale = ov.apply(plan2)

        assert stale == 1
        assert applied == 0
        assert ov.count() == 0
        ov.clear()

    def test_is_stale_by_hash_change(self, tmp_dirs):
        """Override устаревает если изменился хеш файла (при use_hash=True)."""
        import dataclasses
        from application.plan_overrides import PlanOverrides
        src, dst = tmp_dirs
        write(src / "f.bin", b"A" * 100)
        write(dst / "f.bin", b"B" * 100)  # одинаковый размер, разный контент

        profile = SyncProfile(name="stale_h", src=str(src), dst=str(dst),
                              use_hash=True)
        st = scan_directory(src, use_hash=True)
        dt = scan_directory(dst, use_hash=True)
        plan = DiffEngine(profile).compute_plan(st, dt)

        ov = PlanOverrides("stale_hash_test")
        ov.clear()
        action = next(a for a in plan if "f.bin" in str(a.rel_path))
        protected = dataclasses.replace(action, action=ActionType.SKIP_PROTECTED,
                                        protection_level=ProtectionLevel.SINGLE,
                                        reason="manual")
        ov.set(protected)

        # Меняем контент dst (тот же размер!)
        write(dst / "f.bin", b"C" * 100)
        dt2 = scan_directory(dst, use_hash=True)
        plan2 = DiffEngine(profile).compute_plan(st, dt2)
        _, applied, stale = ov.apply(plan2)

        assert stale == 1  # хеш изменился → устарел
        ov.clear()

    def test_not_stale_if_file_unchanged(self, tmp_dirs):
        """Override не устаревает если файл не изменился."""
        import dataclasses
        from application.plan_overrides import PlanOverrides
        src, dst = tmp_dirs
        write(src / "f.txt", b"content_src")
        write(dst / "f.txt", b"content_dst_old")

        profile = SyncProfile(name="notstale_t", src=str(src), dst=str(dst),
                              use_hash=False)
        st = scan_directory(src, use_hash=False)
        dt = scan_directory(dst, use_hash=False)
        plan = DiffEngine(profile).compute_plan(st, dt)

        ov = PlanOverrides("not_stale_test")
        ov.clear()
        action = next(a for a in plan if "f.txt" in str(a.rel_path))
        protected = dataclasses.replace(action, action=ActionType.SKIP_PROTECTED,
                                        protection_level=ProtectionLevel.SINGLE,
                                        reason="manual")
        ov.set(protected)

        # НЕ меняем файлы
        _, applied, stale = ov.apply(plan)
        assert applied == 1
        assert stale == 0
        ov.clear()