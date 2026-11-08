"""Create a verified, permission-restricted backup of TC Studio's SQLite database."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.persistence import database_path, open_database  # noqa: E402


def integrity_check(path: Path) -> str:
    connection = sqlite3.connect(str(path))
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def create_backup(database: str | None, output: Path | None) -> Path:
    source_path = Path(database_path(database)).resolve()
    source = open_database(str(source_path))
    try:
        if output is None:
            backup_dir = source_path.parent / "backups"
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            output = backup_dir / f"tc-studio-{stamp}.sqlite3"
        target = output.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(target.parent, 0o700)
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.tmp"
        )
        if temporary.exists():
            temporary.unlink()
        backup = sqlite3.connect(str(temporary))
        try:
            source.backup(backup)
            backup.execute("PRAGMA journal_mode = DELETE")
            backup.commit()
        finally:
            backup.close()
        os.chmod(temporary, 0o600)
        if integrity_check(temporary).lower() != "ok":
            temporary.unlink(missing_ok=True)
            raise RuntimeError("SQLite 备份完整性校验失败")
        os.replace(temporary, target)
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
        os.chmod(target, 0o600)
        return target
    finally:
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=None)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    target = create_backup(args.database, args.output)
    print(f"backup={target}")
    print(f"integrity={integrity_check(target)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
