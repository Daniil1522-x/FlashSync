from __future__ import annotations
import os, shutil
from pathlib import Path

def _fmt(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"

def get_removable_drives() -> list[dict]:
    drives = []
    if os.name == "nt":
        try:
            import ctypes
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            for i in range(26):
                if bitmask & (1 << i):
                    letter = chr(65 + i)
                    path = f"{letter}:\\"
                    try:
                        drive_type = ctypes.windll.kernel32.GetDriveTypeW(path)
                        if drive_type in (2, 3):
                            usage = shutil.disk_usage(path)
                            label_buf = ctypes.create_unicode_buffer(256)
                            ctypes.windll.kernel32.GetVolumeInformationW(path, label_buf, 256, None, None, None, None, 0)
                            label = label_buf.value or "Без имени"
                            kind = "Съёмный" if drive_type == 2 else "Диск"
                            drives.append({"path": path, "label": label, "total": usage.total,
                                           "free": usage.free, "removable": drive_type == 2,
                                           "display": f"{kind} {letter}: [{label}]  {_fmt(usage.total)}  свободно {_fmt(usage.free)}"})
                    except Exception:
                        pass
        except Exception:
            pass
    else:
        try:
            import psutil
            for part in psutil.disk_partitions(all=False):
                try:
                    usage = shutil.disk_usage(part.mountpoint)
                    removable = part.mountpoint.startswith(("/media/", "/mnt/"))
                    drives.append({"path": part.mountpoint, "label": Path(part.mountpoint).name or part.device,
                                   "total": usage.total, "free": usage.free, "removable": removable,
                                   "display": f"{'Съёмный' if removable else 'Диск'}: {part.mountpoint}  {_fmt(usage.total)}  свободно {_fmt(usage.free)}"})
                except Exception:
                    pass
        except ImportError:
            pass
    drives.sort(key=lambda d: (not d["removable"], d["path"]))
    return drives

def get_removable_only() -> list[dict]:
    return [d for d in get_removable_drives() if d["removable"]]