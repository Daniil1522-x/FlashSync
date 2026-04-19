"""
tests/test_core.py — Тесты ядра FlashSync.
Запуск: pytest tests/ -v
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

# ─── Настройка sys.path для импортов ────────────────────────────────────────
import sys
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from domain.models import (
    FileInfo, SyncAction, ActionType, ProtectionLevel, ProtectionRule,
    SyncProfile, SyncReport, _classify
)
from application.differ import DiffEngine, summarize_plan
from infrastructure.hasher import hash_file, verify_copy, quick_diff
from infrastructure.scanner import scan_directory, get_directory_stats


# ─────────────────────────────────────────────────────────────────────────────
# Фикстуры
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_dirs():
    """Создаёт пару временных директорий src/dst."""
    with tempfile.TemporaryDirectory() as src_dir:
        with tempfile.TemporaryDirectory() as dst_dir:
            yield Path(src_dir), Path(dst_dir)


@pytest.fixture
def default_profile(tmp_dirs) -> SyncProfile:
    src, dst = tmp_dirs
    return SyncProfile(
        name="test",
        src=str(src),
        dst=str(dst),
        use_hash=True,
        delete_mode=True,
        protection_rules=[
            ProtectionRule("*important*", ProtectionLevel.DOUBLE),
            ProtectionRule("*contract*", ProtectionLevel.SINGLE),
        ]
    )


def write_file(path: Path, content: bytes = b"hello world") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Tests: domain/models
# ─────────────────────────────────────────────────────────────────────────────

class TestFileInfo:

    def test_classify_image(self):
        assert _classify(".jpg") == "image"
        assert _classify(".png") == "image"
        assert _classify(".heic") == "image"

    def test_classify_video(self):
        assert _classify(".mp4") == "video"

    def test_classify_other(self):
        assert _classify(".xyz") == "other"

    def test_file_info_category(self, tmp_dirs):
        src, _ = tmp_dirs
        f = write_file(src / "photo.jpg")
        fi = FileInfo(
            path=f, rel_path=Path("photo.jpg"),
            size=f.stat().st_size, mtime=f.stat().st_mtime
        )
        assert fi.category == "image"
        assert fi.extension == ".jpg"

    def test_is_same_as_by_hash(self, tmp_dirs):
        src, dst = tmp_dirs
        content = b"test content 123"
        f1 = write_file(src / "a.txt", content)
        f2 = write_file(dst / "a.txt", content)
        fi1 = FileInfo(path=f1, rel_path=Path("a.txt"),
                       size=f1.stat().st_size, mtime=f1.stat().st_mtime,
                       hash="abc123")
        fi2 = FileInfo(path=f2, rel_path=Path("a.txt"),
                       size=f2.stat().st_size, mtime=f2.stat().st_mtime,
                       hash="abc123")
        assert fi1.is_same_as(fi2, use_hash=True)

    def test_is_different_by_hash(self, tmp_dirs):
        src, dst = tmp_dirs
        f1 = write_file(src / "a.txt", b"content1")
        f2 = write_file(dst / "a.txt", b"content2")
        fi1 = FileInfo(path=f1, rel_path=Path("a.txt"),
                       size=f1.stat().st_size, mtime=f1.stat().st_mtime,
                       hash="hash1")
        fi2 = FileInfo(path=f2, rel_path=Path("a.txt"),
                       size=f2.stat().st_size, mtime=f2.stat().st_mtime,
                       hash="hash2")
        assert not fi1.is_same_as(fi2, use_hash=True)


class TestProtectionRule:

    def test_keyword_match(self):
        rule = ProtectionRule("*important*", ProtectionLevel.DOUBLE)
        assert rule.matches(Path("docs/important_file.pdf"))
        assert rule.matches(Path("important.docx"))
        assert not rule.matches(Path("random_file.pdf"))

    def test_glob_match(self):
        rule = ProtectionRule("*.docx", ProtectionLevel.SINGLE)
        assert rule.matches(Path("document.docx"))
        assert not rule.matches(Path("document.pdf"))


class TestSyncAction:

    def test_needs_confirmation(self, tmp_dirs):
        src, _ = tmp_dirs
        f = write_file(src / "test.txt")
        fi = FileInfo(path=f, rel_path=Path("test.txt"),
                      size=f.stat().st_size, mtime=f.stat().st_mtime)
        a = SyncAction(
            action=ActionType.COPY_NEW,
            src_file=fi, dst_file=None,
            protection_level=ProtectionLevel.NONE
        )
        assert not a.needs_confirmation()

    def test_double_confirm_requires_two(self, tmp_dirs):
        src, _ = tmp_dirs
        f = write_file(src / "important.txt")
        fi = FileInfo(path=f, rel_path=Path("important.txt"),
                      size=f.stat().st_size, mtime=f.stat().st_mtime)
        a = SyncAction(
            action=ActionType.DELETE,
            src_file=None, dst_file=fi,
            protection_level=ProtectionLevel.DOUBLE
        )
        assert not a.confirm()   # первое — недостаточно
        assert a.confirm()       # второе — ОК


# ─────────────────────────────────────────────────────────────────────────────
# Tests: infrastructure/hasher
# ─────────────────────────────────────────────────────────────────────────────

class TestHasher:

    def test_hash_file_consistent(self, tmp_dirs):
        src, _ = tmp_dirs
        f = write_file(src / "file.bin", b"stable content")
        h1 = hash_file(f)
        h2 = hash_file(f)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_hash_different_content(self, tmp_dirs):
        src, _ = tmp_dirs
        f1 = write_file(src / "a.bin", b"content a")
        f2 = write_file(src / "b.bin", b"content b")
        assert hash_file(f1) != hash_file(f2)

    def test_verify_copy_success(self, tmp_dirs):
        src, dst = tmp_dirs
        content = b"verify me " * 100
        f_src = write_file(src / "file.dat", content)
        f_dst = write_file(dst / "file.dat", content)
        assert verify_copy(f_src, f_dst)

    def test_verify_copy_fail(self, tmp_dirs):
        src, dst = tmp_dirs
        f_src = write_file(src / "file.dat", b"original")
        f_dst = write_file(dst / "file.dat", b"modified")
        assert not verify_copy(f_src, f_dst)

    def test_quick_diff_same_size(self, tmp_dirs):
        src, dst = tmp_dirs
        content = b"same size"
        f1 = write_file(src / "f.txt", content)
        # Установим одинаковое mtime
        mtime = f1.stat().st_mtime
        f2 = write_file(dst / "f.txt", content)
        os.utime(f2, (mtime, mtime))
        assert quick_diff(f1, f2)


# ─────────────────────────────────────────────────────────────────────────────
# Tests: infrastructure/scanner
# ─────────────────────────────────────────────────────────────────────────────

class TestScanner:

    def test_scan_basic(self, tmp_dirs):
        src, _ = tmp_dirs
        write_file(src / "a.txt", b"hello")
        write_file(src / "sub" / "b.jpg", b"image")
        result = scan_directory(src, use_hash=False)
        assert len(result) == 2
        assert Path("a.txt") in result
        assert Path("sub/b.jpg") in result

    def test_scan_with_exclude(self, tmp_dirs):
        src, _ = tmp_dirs
        write_file(src / "data.txt", b"keep")
        write_file(src / "temp.log", b"ignore")
        result = scan_directory(src, exclude_patterns=["*.log"], use_hash=False)
        assert len(result) == 1
        assert Path("data.txt") in result

    def test_scan_ignores_system_files(self, tmp_dirs):
        src, _ = tmp_dirs
        write_file(src / "real.txt", b"keep")
        write_file(src / "Thumbs.db", b"system")
        result = scan_directory(src, use_hash=False)
        # Thumbs.db должен быть проигнорирован
        assert Path("real.txt") in result
        for k in result:
            assert "thumbs" not in str(k).lower()

    def test_scan_with_hash(self, tmp_dirs):
        src, _ = tmp_dirs
        write_file(src / "a.txt", b"content")
        result = scan_directory(src, use_hash=True)
        fi = result[Path("a.txt")]
        assert fi.hash is not None
        assert len(fi.hash) == 64

    def test_get_directory_stats(self, tmp_dirs):
        src, _ = tmp_dirs
        write_file(src / "photo.jpg", b"x" * 1000)
        write_file(src / "doc.pdf", b"y" * 500)
        write_file(src / "song.mp3", b"z" * 2000)
        stats = get_directory_stats(src)
        assert stats["total_files"] == 3
        assert stats["total_size"] == 3500
        assert "image" in stats["categories"]
        assert "audio" in stats["categories"]


# ─────────────────────────────────────────────────────────────────────────────
# Tests: application/differ
# ─────────────────────────────────────────────────────────────────────────────

class TestDiffEngine:

    def _make_fi(self, root: Path, rel: str, content: bytes) -> FileInfo:
        full = root / rel
        write_file(full, content)
        return FileInfo(
            path=full, rel_path=Path(rel),
            size=full.stat().st_size, mtime=full.stat().st_mtime,
            hash=hash_file(full),
        )

    def test_copy_new(self, tmp_dirs, default_profile):
        src, dst = tmp_dirs
        src_fi = self._make_fi(src, "new_file.txt", b"new content")
        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(
            {Path("new_file.txt"): src_fi},
            {}
        )
        assert len(plan) == 1
        assert plan[0].action == ActionType.COPY_NEW

    def test_skip_equal(self, tmp_dirs, default_profile):
        src, dst = tmp_dirs
        content = b"same content"
        src_fi = self._make_fi(src, "file.txt", content)
        dst_fi = self._make_fi(dst, "file.txt", content)
        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(
            {Path("file.txt"): src_fi},
            {Path("file.txt"): dst_fi}
        )
        assert len(plan) == 1
        assert plan[0].action == ActionType.SKIP_EQUAL

    def test_copy_update(self, tmp_dirs, default_profile):
        src, dst = tmp_dirs
        src_fi = self._make_fi(src, "file.txt", b"new version")
        dst_fi = self._make_fi(dst, "file.txt", b"old version")
        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(
            {Path("file.txt"): src_fi},
            {Path("file.txt"): dst_fi}
        )
        assert len(plan) == 1
        assert plan[0].action == ActionType.COPY_UPDATE

    def test_delete_when_delete_mode(self, tmp_dirs, default_profile):
        src, dst = tmp_dirs
        dst_fi = self._make_fi(dst, "only_in_dst.txt", b"orphan")
        default_profile.delete_mode = True
        engine = DiffEngine(default_profile)
        plan = engine.compute_plan({}, {Path("only_in_dst.txt"): dst_fi})
        assert len(plan) == 1
        assert plan[0].action == ActionType.DELETE

    def test_no_delete_when_delete_mode_off(self, tmp_dirs, default_profile):
        src, dst = tmp_dirs
        dst_fi = self._make_fi(dst, "only_in_dst.txt", b"orphan")
        default_profile.delete_mode = False
        engine = DiffEngine(default_profile)
        plan = engine.compute_plan({}, {Path("only_in_dst.txt"): dst_fi})
        assert len(plan) == 0

    def test_protection_blocks_update(self, tmp_dirs, default_profile):
        src, dst = tmp_dirs
        src_fi = self._make_fi(src, "important_contract.pdf", b"new")
        dst_fi = self._make_fi(dst, "important_contract.pdf", b"old")
        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(
            {Path("important_contract.pdf"): src_fi},
            {Path("important_contract.pdf"): dst_fi}
        )
        assert len(plan) == 1
        assert plan[0].action == ActionType.SKIP_PROTECTED
        assert plan[0].protection_level == ProtectionLevel.DOUBLE


class TestSummarizePlan:

    def test_empty_plan(self):
        result = summarize_plan([])
        assert result["total_actions"] == 0
        assert result["bytes_to_copy"] == 0

    def test_summary_counts(self, tmp_dirs, default_profile):
        src, dst = tmp_dirs
        engine = DiffEngine(default_profile)

        # Создаём минимальный набор файлов
        def fi(root, rel, content):
            full = root / rel
            write_file(full, content)
            return FileInfo(path=full, rel_path=Path(rel),
                           size=len(content), mtime=full.stat().st_mtime,
                           hash=hash_file(full))

        src_tree = {
            Path("new.txt"): fi(src, "new.txt", b"a"),
            Path("updated.txt"): fi(src, "updated.txt", b"new"),
        }
        dst_tree = {
            Path("updated.txt"): fi(dst, "updated.txt", b"old"),
        }

        plan = engine.compute_plan(src_tree, dst_tree)
        summary = summarize_plan(plan)
        assert summary["counts"].get("copy_new", 0) >= 1
        assert summary["counts"].get("copy_update", 0) >= 1


# ─────────────────────────────────────────────────────────────────────────────
# Tests: sync_engine (интеграционный)
# ─────────────────────────────────────────────────────────────────────────────

class TestSyncEngine:

    @pytest.mark.asyncio
    async def test_copy_new_file(self, tmp_dirs, default_profile):
        import asyncio
        from application.sync_engine import SyncEngine

        src, dst = tmp_dirs
        content = b"brand new file content"
        src_file = write_file(src / "newfile.txt", content)

        src_fi = FileInfo(
            path=src_file, rel_path=Path("newfile.txt"),
            size=src_file.stat().st_size, mtime=src_file.stat().st_mtime
        )
        action = SyncAction(
            action=ActionType.COPY_NEW,
            src_file=src_fi,
            dst_file=None,
        )

        engine = SyncEngine(default_profile, src, dst, dry_run=False)
        report = await engine.execute([action])

        assert len(report.errors) == 0
        assert (dst / "newfile.txt").exists()
        assert (dst / "newfile.txt").read_bytes() == content

    @pytest.mark.asyncio
    async def test_dry_run_does_not_copy(self, tmp_dirs, default_profile):
        from application.sync_engine import SyncEngine

        src, dst = tmp_dirs
        src_file = write_file(src / "dryfile.txt", b"dry")
        src_fi = FileInfo(
            path=src_file, rel_path=Path("dryfile.txt"),
            size=src_file.stat().st_size, mtime=src_file.stat().st_mtime
        )
        action = SyncAction(action=ActionType.COPY_NEW, src_file=src_fi, dst_file=None)

        engine = SyncEngine(default_profile, src, dst, dry_run=True)
        report = await engine.execute([action])

        # Dry run — файл НЕ должен появиться
        assert not (dst / "dryfile.txt").exists()
        assert len(report.errors) == 0
        assert len(report.actions_done) == 1

    @pytest.mark.asyncio
    async def test_update_creates_backup(self, tmp_dirs, default_profile):
        from application.sync_engine import SyncEngine

        src, dst = tmp_dirs
        src_file = write_file(src / "updated.txt", b"new version")
        dst_file = write_file(dst / "updated.txt", b"old version")

        src_fi = FileInfo(path=src_file, rel_path=Path("updated.txt"),
                          size=src_file.stat().st_size, mtime=src_file.stat().st_mtime)
        dst_fi = FileInfo(path=dst_file, rel_path=Path("updated.txt"),
                          size=dst_file.stat().st_size, mtime=dst_file.stat().st_mtime)
        action = SyncAction(action=ActionType.COPY_UPDATE, src_file=src_fi, dst_file=dst_fi)

        engine = SyncEngine(default_profile, src, dst, dry_run=False)
        report = await engine.execute([action])

        assert len(report.errors) == 0
        assert (dst / "updated.txt").read_bytes() == b"new version"

        # Проверяем backup
        backup_dirs = list(dst.glob(".flashsync_backup_*"))
        assert len(backup_dirs) == 1
        backed_up = list(backup_dirs[0].rglob("updated.txt"))
        assert len(backed_up) == 1
        assert backed_up[0].read_bytes() == b"old version"
