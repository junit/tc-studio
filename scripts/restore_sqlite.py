"""Restore a TIM SQLite backup into a new or explicitly approved database path."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.persistence import database_path  # noqa: E402


def integrity_check(path: Path) -> str:
    connection = sqlite3.connect(str(path))
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def restore(backup_path: Path, database: str | None, *, force: bool) -> Path:
    source_path = backup_path.expanduser().resolve()
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    if integrity_check(source_path).lower() != "ok":
        raise RuntimeError("源备份完整性校验失败，拒绝恢复")

    target = Path(database_path(database)).resolve()
    if target.exists() and not force:
        raise FileExistsError(
            f"目标数据库已存在：{target}；如确认覆盖，请传入 --force"
        )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.restore.tmp")
    temporary.unlink(missing_ok=True)
    source = sqlite3.connect(str(source_path))
    try:
        destination = sqlite3.connect(str(temporary))
        try:
            source.backup(destination)
            destination.commit()
        finally:
            destination.close()
    finally:
        source.close()
    if integrity_check(temporary).lower() != "ok":
        temporary.unlink(missing_ok=True)
        raise RuntimeError("恢复后的 SQLite 完整性校验失败")
    os.chmod(temporary, 0o600)
    os.replace(temporary, target)
    for suffix in ("-wal", "-shm"):
        Path(f"{target}{suffix}").unlink(missing_ok=True)
    os.chmod(target, 0o600)
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("--database", default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    target = restore(args.backup, args.database, force=args.force)
    print(f"restored={target}")
    print(f"integrity={integrity_check(target)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
