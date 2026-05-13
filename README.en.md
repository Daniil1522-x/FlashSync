# ⚡ FlashSync Pro

A professional file synchronizer between a USB flash drive and your computer.
Runs in the terminal with a polished TUI interface built on [Textual](https://textual.textualize.io/).

---

## Features

- Compares two folders by **SHA-256** hash or by **size + date**
- Shows a full change plan **before doing anything**
- Copies new and updated files
- **Never deletes files permanently** — old versions are moved to a dated backup folder
- Protects important files from accidental modification (by filename pattern)
- **Dry Run mode** — simulate a sync without touching anything
- Saves reports to `~/.flashsync/reports/`

---

## Quick Start

### Install dependencies

```
pip install textual aiofiles tqdm
```

or via uv:

```
uv add textual aiofiles tqdm
```

### Launch TUI (recommended)

```
python main.py
```

### Launch CLI menu (without Textual)

```
python main.py --cli
```

### Direct terminal commands

```
python main.py scan  -s E:\FLASH -d E:\Backup        # show plan
python main.py sync  -s E:\FLASH -d E:\Backup        # synchronize
python main.py sync  -s E:\FLASH -d E:\Backup --dry-run   # simulate
python main.py audit -s E:\FLASH -d E:\Backup        # hash audit
python main.py stats E:\FLASH                        # folder statistics
```

---

## Build .exe (run without Python)

```
pip install pyinstaller
python scripts/build_exe.py
```

The ready binary will appear in `dist/flashsync.exe`.

---

## TUI Layout

```
┌─────────────────────────────────────────────────────┐
│ [Scan]  [Sync]  [Dry Run]  [Settings]               │
│ Source  │       CHANGE PLAN          │  Statistics  │
│ (tree)  │ + new  ~ update  - backup  │  + log       │
│         │                            │              │
└──────────────────── status ──────────────────────────┘
```

### Buttons

| Button | Hotkey | Description |
|--------|--------|-------------|
| **Scan** | Ctrl+S | Reads both folders and builds the change list. Nothing is modified. |
| **Sync** | Ctrl+R | Executes the plan: copies new files, updates changed ones, old versions go to backup. |
| **Dry Run** | Ctrl+D | Shows what **would** be done, but touches nothing. Safe to run anytime. |
| **Swap src↔dst** | — | Swaps source and destination for reverse synchronization. |
| **Settings** | Ctrl+P | Change paths, toggle hash comparison and delete mode. |
| **Quit** | Ctrl+Q | Exit the application. |

### Change plan symbols

| Symbol | Color | Action |
|--------|-------|--------|
| `+` | green | New file — will be copied to dst |
| `~` | yellow | File changed — will be updated (old → backup) |
| `-` | red | File only in dst — will be moved to backup |
| `=` | gray | Files are identical — no action |
| `L` | cyan | File is protected — will not be touched |

### What is Dry Run?

**Dry Run** is a simulation mode. The program shows what it *would* do but does not actually copy or delete anything. Use it before the first real sync to verify the plan looks correct.

---

## Profile Settings

The profile is stored in `~/.flashsync/config.json` (Linux/macOS) or `C:\Users\<name>\.flashsync\config.json` (Windows).

| Parameter | Description |
|-----------|-------------|
| **Source** | Path to the flash drive or source folder |
| **Destination** | Path where files will be copied |
| **SHA-256** | Compare files by content (slower, more accurate). Disabled = compare by size and date. |
| **Delete from dst** | If a file exists in dst but not in src — move it to backup |
| **Ignore hidden** | Skip `.DS_Store`, `Thumbs.db`, etc. |

---

## File Protection

Files matching certain name patterns are protected automatically:

| Pattern | Level | Behavior |
|---------|-------|----------|
| `*important*` | DOUBLE | Requires 2 confirmations to modify |
| `*contract*`  | DOUBLE | Requires 2 confirmations |
| `*personal*`  | DOUBLE | Requires 2 confirmations |
| `*backup*`    | SINGLE | Requires 1 confirmation |
| `*final*`     | SINGLE | Requires 1 confirmation |

Rules can be edited in `~/.flashsync/config.json`.

---

## Backup

FlashSync **never permanently deletes files**.
Before updating or removing a file, it is first copied to a dated backup folder:

```
dst/.flashsync_backup_20250419_143022/
```

Backup folders are never synchronized (they start with `.`).

---

## Project Structure

```
FlashSync/
├── domain/          # Entities: FileInfo, SyncAction, SyncProfile
├── application/     # Logic: DiffEngine, SyncEngine
├── infrastructure/  # Filesystem: scanner, hasher, storage
├── ui/tui/          # Textual TUI interface
├── tests/           # 31 tests (pytest)
├── scripts/
│   ├── build_exe.py            # Build .exe
│   └── generate_test_data.py   # Generate test data
├── main.py          # Entry point
└── requirements.txt
```

---

## Running Tests

```
pytest tests/ -v
```

---

## Roadmap

- [ ] Background file monitoring (`watchdog`)
- [ ] Cleanup of old backup folders
- [ ] Multiple profiles in TUI
- [ ] Progress bar for large file transfers
- [ ] Export plan to CSV/HTML

---

## Where FlashSync stores its files

**Windows:**
```
C:\Users\<YourName>\.flashsync\
    config.json      — sync profiles
    flashsync.log    — operation log
    reports\         — sync reports
```

**Linux/macOS:**
```
~/.flashsync/
    config.json
    flashsync.log
    reports/
```
