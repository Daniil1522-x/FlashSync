"""
infrastructure/drives.py — Автоопределение съёмных дисков.
"""
from __future__ import annotations
import os
import shutil
from pathlib import Path
from typing import Optional


def _fmt_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def get_removable_drives() -> list[dict]:
    """
    Возвращает список съёмных дисков.
    Каждый элемент: {"path": "E:\\", "label": "...", "total": int, "free": int, "display": "..."}
    """
    drives = []

    if os.name == "nt":
        # Windows — перебираем буквы дисков
        try:
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if bitmask & (1 << i):
                    letter = chr(65 + i)
                    path = f"{letter}:\\"
                    try:
                        drive_type = ctypes.windll.kernel32.GetDriveTypeW(path)
                        # 2 = DRIVE_REMOVABLE (флешка, SD-карта)
                        # 3 = DRIVE_FIXED (HDD/SSD) — тоже показываем для ручного выбора
                        if drive_type in (2, 3):
                            usage = shutil.disk_usage(path)
                            # Получаем метку тома
                            label_buf = ctypes.create_unicode_buffer(256)
                            ctypes.windll.kernel32.GetVolumeInformationW(
                                path, label_buf, 256,
                                None, None, None, None, 0
                            )
                            label = label_buf.value or "Без имени"
                            kind = "Съёмный" if drive_type == 2 else "Диск"
                            drives.append({
                                "path": path,
                                "label": label,
                                "total": usage.total,
                                "free": usage.free,
                                "removable": drive_type == 2,
                                "display": f"{kind} {letter}: [{label}]  {_fmt_size(usage.total)}  свободно {_fmt_size(usage.free)}",
                            })
                    except (OSError, Exception):
                        pass
        except Exception:
            pass
    else:
        # Linux/macOS — через /proc/mounts или psutil
        try:
            import psutil
            for part in psutil.disk_partitions(all=False):
                try:
                    usage = shutil.disk_usage(part.mountpoint)
                    removable = any(x in part.opts for x in ("removable",)) or \
                                part.mountpoint.startswith("/media/") or \
                                part.mountpoint.startswith("/mnt/")
                    drives.append({
                        "path": part.mountpoint,
                        "label": Path(part.mountpoint).name or part.device,
                        "total": usage.total,
                        "free": usage.free,
                        "removable": removable,
                        "display": f"{'Съёмный' if removable else 'Диск'}: {part.mountpoint}  {_fmt_size(usage.total)}  свободно {_fmt_size(usage.free)}",
                    })
                except (OSError, Exception):
                    pass
        except ImportError:
            # Без psutil — читаем /proc/mounts
            try:
                with open("/proc/mounts") as f:
                    for line in f:
                        parts = line.split()
                        if len(parts) >= 2:
                            mp = parts[1]
                            if mp.startswith(("/media/", "/mnt/")):
                                try:
                                    usage = shutil.disk_usage(mp)
                                    drives.append({
                                        "path": mp,
                                        "label": Path(mp).name,
                                        "total": usage.total,
                                        "free": usage.free,
                                        "removable": True,
                                        "display": f"Съёмный: {mp}  {_fmt_size(usage.total)}  свободно {_fmt_size(usage.free)}",
                                    })
                                except OSError:
                                    pass
            except Exception:
                pass

    # Съёмные — первыми
    drives.sort(key=lambda d: (not d["removable"], d["path"]))
    return drives


def get_removable_only() -> list[dict]:
    return [d for d in get_removable_drives() if d["removable"]]