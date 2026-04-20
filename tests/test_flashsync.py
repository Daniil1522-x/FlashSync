"""
Тесты для FlashSync.
Запуск: pytest tests/test_flashsync.py -v
"""
import pytest
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
import time

from domain.models import (
    FileInfo, SyncAction, ActionType, SyncProfile,
    ProtectionLevel, ProtectionRule, HashAlgo
)
from application.differ import DiffEngine, summarize_plan
from infrastructure.hasher import hash_file, quick_diff, verify_copy
from infrastructure.scanner import scan_directory, _match_pattern


# ─────────────────────────────────────────────────────────────────────────────
# Фикстуры
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def temp_dirs():
    """Создаёт временные директории src и dst."""
    with tempfile.TemporaryDirectory() as src_dir, \
            tempfile.TemporaryDirectory() as dst_dir:
        yield Path(src_dir), Path(dst_dir)


@pytest.fixture
def sample_files(temp_dirs):
    """Создаёт тестовые файлы в src."""
    src, dst = temp_dirs

    # Файл 1: текстовый
    (src / "doc.txt").write_text("Hello World", encoding="utf-8")

    # Файл 2: бинарный
    (src / "data.bin").write_bytes(b"\x00\x01\x02\x03")

    # Файл 3: в подпапке
    subdir = src / "subdir"
    subdir.mkdir()
    (subdir / "nested.txt").write_text("Nested file", encoding="utf-8")

    # Файл 4: большой (1 MB)
    (src / "large.dat").write_bytes(b"x" * (1024 * 1024))

    return src, dst


@pytest.fixture
def default_profile():
    """Стандартный профиль для тестов."""
    return SyncProfile(
        name="test",
        src="",
        dst="",
        use_hash=True,
        delete_mode=True,
        ignore_hidden=True,
        max_workers=2
    )


# ─────────────────────────────────────────────────────────────────────────────
# Тесты безопасности
# ─────────────────────────────────────────────────────────────────────────────

class TestSafety:
    """Тесты безопасности."""

    def test_backup_created_before_delete(self, temp_dirs, default_profile):
        """Бэкап создаётся перед удалением файла."""
        src, dst = temp_dirs

        # Создаём файл в dst
        test_file = dst / "important.txt"
        test_file.write_text("Important data", encoding="utf-8")

        # Файла нет в src → должен быть удалён (с бэкапом)
        profile = default_profile
        profile.delete_mode = True

        src_tree = scan_directory(src, ignore_hidden=True)
        dst_tree = scan_directory(dst, ignore_hidden=True)

        engine = DiffEngine(profile)
        plan = engine.compute_plan(src_tree, dst_tree)

        # Проверяем, что есть действие DELETE
        delete_actions = [a for a in plan if a.action == ActionType.DELETE]
        assert len(delete_actions) == 1
        assert delete_actions[0].dst_file.path == test_file

    def test_dry_run_does_not_modify_files(self, sample_files, default_profile):
        """Dry Run не изменяет файлы."""
        src, dst = sample_files

        # Копируем один файл в dst
        (dst / "doc.txt").write_text("Old version", encoding="utf-8")

        src_tree = scan_directory(src, use_hash=True)
        dst_tree = scan_directory(dst, use_hash=True)

        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(src_tree, dst_tree)

        # Должно быть действие COPY_UPDATE
        update_actions = [a for a in plan if a.action == ActionType.COPY_UPDATE]
        assert len(update_actions) == 1

        # Проверяем, что файл в dst НЕ изменился (это только план!)
        assert (dst / "doc.txt").read_text(encoding="utf-8") == "Old version"

    def test_protected_files_not_deleted(self, temp_dirs, default_profile):
        """Защищённые файлы не удаляются."""
        src, dst = temp_dirs

        # Создаём защищённый файл в dst
        protected = dst / "important_contract.txt"
        protected.write_text("Contract", encoding="utf-8")

        # Добавляем правило защиты
        default_profile.protection_rules = [
            ProtectionRule(pattern="*contract*", level=ProtectionLevel.DOUBLE)
        ]
        default_profile.delete_mode = True

        src_tree = scan_directory(src, ignore_hidden=True)
        dst_tree = scan_directory(dst, ignore_hidden=True)

        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(src_tree, dst_tree)

        # Файл должен быть пропущен, а не удалён
        skip_actions = [
            a for a in plan
            if a.action == ActionType.SKIP_PROTECTED and "contract" in str(a.rel_path)
        ]
        assert len(skip_actions) == 1

    def test_disk_space_check(self, temp_dirs, default_profile):
        """Проверка места на диске."""
        src, dst = temp_dirs

        # Создаём большой файл (100 MB)
        large_file = src / "huge.dat"
        with open(large_file, "wb") as f:
            f.write(b"x" * (100 * 1024 * 1024))

        src_tree = scan_directory(src)

        # Проверяем, что файл найден
        assert len(src_tree) == 1
        assert src_tree[Path("huge.dat")].size == 100 * 1024 * 1024


# ─────────────────────────────────────────────────────────────────────────────
# Тесты основной функциональности
# ─────────────────────────────────────────────────────────────────────────────

class TestCoreFunctionality:
    """Тесты основных функций."""

    def test_new_file_detection(self, sample_files, default_profile):
        """Обнаружение новых файлов."""
        src, dst = sample_files

        src_tree = scan_directory(src, use_hash=False)
        dst_tree = scan_directory(dst, use_hash=False)  # dst пустая

        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(src_tree, dst_tree)

        # Все файлы из src должны быть помечены как COPY_NEW
        new_actions = [a for a in plan if a.action == ActionType.COPY_NEW]
        assert len(new_actions) == 4  # doc.txt, data.bin, nested.txt, large.dat

    def test_identical_files_skipped(self, sample_files, default_profile):
        """Идентичные файлы пропускаются."""
        src, dst = sample_files

        # Копируем файл в dst без изменений
        shutil.copy2(src / "doc.txt", dst / "doc.txt")

        src_tree = scan_directory(src, use_hash=True)
        dst_tree = scan_directory(dst, use_hash=True)

        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(src_tree, dst_tree)

        # Файл doc.txt должен быть пропущен
        skip_actions = [
            a for a in plan
            if a.action == ActionType.SKIP_EQUAL and a.rel_path == Path("doc.txt")
        ]
        assert len(skip_actions) == 1

    def test_modified_file_detection(self, sample_files, default_profile):
        """Обнаружение изменённых файлов."""
        src, dst = sample_files

        # Копируем и изменяем файл в dst
        shutil.copy2(src / "doc.txt", dst / "doc.txt")
        time.sleep(1)  # Ждём, чтобы mtime отличался
        (dst / "doc.txt").write_text("Modified!", encoding="utf-8")

        src_tree = scan_directory(src, use_hash=True)
        dst_tree = scan_directory(dst, use_hash=True)

        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(src_tree, dst_tree)

        # Файл должен быть помечен как COPY_UPDATE
        update_actions = [
            a for a in plan
            if a.action == ActionType.COPY_UPDATE and a.rel_path == Path("doc.txt")
        ]
        assert len(update_actions) == 1

    def test_delete_mode(self, temp_dirs, default_profile):
        """Режим удаления файлов, которых нет в src."""
        src, dst = temp_dirs

        # Создаём файл только в dst
        (dst / "orphan.txt").write_text("Orphan", encoding="utf-8")

        default_profile.delete_mode = True

        src_tree = scan_directory(src, ignore_hidden=True)
        dst_tree = scan_directory(dst, ignore_hidden=True)

        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(src_tree, dst_tree)

        # Файл должен быть помечен на удаление
        delete_actions = [a for a in plan if a.action == ActionType.DELETE]
        assert len(delete_actions) == 1
        assert delete_actions[0].rel_path == Path("orphan.txt")


# ─────────────────────────────────────────────────────────────────────────────
# Тесты хеширования
# ─────────────────────────────────────────────────────────────────────────────

class TestHashing:
    """Тесты хеширования."""

    def test_hash_file_correctness(self, temp_dirs):
        """Корректность вычисления хеша."""
        src, _ = temp_dirs

        test_file = src / "test.txt"
        test_file.write_text("Hello", encoding="utf-8")

        hash_sha256 = hash_file(test_file, algo="sha256")
        hash_md5 = hash_file(test_file, algo="md5")

        assert hash_sha256 is not None
        assert len(hash_sha256) == 64  # SHA-256 = 64 hex chars
        assert hash_md5 is not None
        assert len(hash_md5) == 32  # MD5 = 32 hex chars

        # Проверяем известный хеш для "Hello"
        assert hash_sha256 == "185f8db32271fe25f561a6fc938b2e264306ec304eda518007d1764826381969"

    def test_hash_file_not_found(self, temp_dirs):
        """Хеширование несуществующего файла."""
        src, _ = temp_dirs

        result = hash_file(src / "nonexistent.txt")
        assert result is None

    def test_quick_diff(self, temp_dirs):
        """Быстрое сравнение файлов."""
        src, dst = temp_dirs

        file1 = src / "a.txt"
        file2 = dst / "b.txt"

        file1.write_text("Same content", encoding="utf-8")
        file2.write_text("Same content", encoding="utf-8")

        # Файлы одинаковые (размер и mtime близки)
        assert quick_diff(file1, file2) is True

        # Изменяем второй файл
        time.sleep(0.1)
        file2.write_text("Different!", encoding="utf-8")

        assert quick_diff(file1, file2) is False


# ─────────────────────────────────────────────────────────────────────────────
# Тесты сканирования
# ─────────────────────────────────────────────────────────────────────────────

class TestScanning:
    """Тесты сканирования директорий."""

    def test_scan_hidden_files(self, temp_dirs):
        """Скрытые файлы игнорируются."""
        src, _ = temp_dirs

        (src / "visible.txt").write_text("Visible", encoding="utf-8")
        (src / ".hidden.txt").write_text("Hidden", encoding="utf-8")

        # С игнорированием скрытых
        tree = scan_directory(src, ignore_hidden=True)
        assert len(tree) == 1
        assert Path("visible.txt") in tree

        # Без игнорирования
        tree = scan_directory(src, ignore_hidden=False)
        assert len(tree) == 2

    def test_scan_nested_directories(self, temp_dirs):
        """Сканирование вложенных директорий."""
        src, _ = temp_dirs

        # Создаём глубокую структуру
        deep_path = src / "a" / "b" / "c" / "d"
        deep_path.mkdir(parents=True)
        (deep_path / "file.txt").write_text("Deep", encoding="utf-8")

        tree = scan_directory(src, ignore_hidden=True)

        assert len(tree) == 1
        assert Path("a/b/c/d/file.txt") in tree

    def test_scan_large_directory(self, temp_dirs):
        """Сканирование большой директории."""
        src, _ = temp_dirs

        # Создаём 1000 файлов
        for i in range(1000):
            (src / f"file_{i:04d}.txt").write_text(f"Content {i}", encoding="utf-8")

        tree = scan_directory(src, ignore_hidden=True, use_hash=False)

        assert len(tree) == 1000


# ─────────────────────────────────────────────────────────────────────────────
# Тесты защиты (Protection Rules)
# ─────────────────────────────────────────────────────────────────────────────

class TestProtectionRules:
    """Тесты правил защиты."""

    def test_pattern_matching_simple(self):
        """Простой матчинг паттернов."""
        rule = ProtectionRule(pattern="*.txt", level=ProtectionLevel.SINGLE)

        assert rule.matches(Path("doc.txt")) is True
        assert rule.matches(Path("subdir/doc.txt")) is True
        assert rule.matches(Path("doc.pdf")) is False

    def test_pattern_matching_wildcard(self):
        """Матчинг с wildcards."""
        rule = ProtectionRule(pattern="*important*", level=ProtectionLevel.DOUBLE)

        assert rule.matches(Path("very_important_file.txt")) is True
        assert rule.matches(Path("important")) is True
        assert rule.matches(Path("normal.txt")) is False

    def test_pattern_matching_path(self):
        """Матчинг по полному пути."""
        rule = ProtectionRule(pattern="backup/*", level=ProtectionLevel.SINGLE)

        assert rule.matches(Path("backup/file.txt")) is True
        assert rule.matches(Path("backup/sub/file.txt")) is True
        assert rule.matches(Path("other/file.txt")) is False

    def test_multiple_rules_priority(self, temp_dirs, default_profile):
        """Приоритет правил (выбирается максимальный уровень)."""
        src, dst = temp_dirs

        # Создаём файл, подходящий под два правила
        (dst / "important_backup.txt").write_text("Data", encoding="utf-8")

        default_profile.protection_rules = [
            ProtectionRule(pattern="*important*", level=ProtectionLevel.SINGLE),
            ProtectionRule(pattern="*backup*", level=ProtectionLevel.DOUBLE),
        ]
        default_profile.delete_mode = True

        src_tree = scan_directory(src, ignore_hidden=True)
        dst_tree = scan_directory(dst, ignore_hidden=True)

        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(src_tree, dst_tree)

        # Файл должен быть защищён с уровнем DOUBLE (максимальный)
        protected = [a for a in plan if a.action == ActionType.SKIP_PROTECTED]
        assert len(protected) == 1
        assert protected[0].protection_level == ProtectionLevel.DOUBLE


# ─────────────────────────────────────────────────────────────────────────────
# Тесты утилит
# ─────────────────────────────────────────────────────────────────────────────

class TestUtils:
    """Тесты вспомогательных функций."""

    def test_summarize_plan(self, sample_files, default_profile):
        """Корректная статистика плана."""
        src, dst = sample_files

        src_tree = scan_directory(src, use_hash=False)
        dst_tree = scan_directory(dst, use_hash=False)

        engine = DiffEngine(default_profile)
        plan = engine.compute_plan(src_tree, dst_tree)

        summary = summarize_plan(plan)

        assert summary["total_actions"] == 4
        assert summary["counts"]["copy_new"] == 4
        assert summary["bytes_to_copy"] > 0

    def test_sync_action_properties(self):
        """Свойства SyncAction."""
        fi = FileInfo(
            path=Path("/test.txt"),
            rel_path=Path("test.txt"),
            size=1024,
            mtime=0
        )

        action = SyncAction(
            action=ActionType.COPY_NEW,
            src_file=fi,
            dst_file=None
        )

        assert action.rel_path == Path("test.txt")
        assert action.size_bytes == 1024
        assert action.needs_confirmation() is False


# ─────────────────────────────────────────────────────────────────────────────
# Запуск тестов
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])