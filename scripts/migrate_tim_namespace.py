"""Copy the legacy TIM SQLite database into the TC Studio namespace.

The source database is never deleted. The command refuses to overwrite an
existing canonical database unless ``--force`` is supplied, and it requires
``--execute`` so a dry run cannot accidentally create a second SQLite fact
store.
"""

from __future__ import annotations

import argparse
from contextlib import suppress
import os
from pathlib import Path
import sqlite3
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.namespace import (  # noqa: E402
    CANONICAL_DATABASE_FILENAME,
    LEGACY_DATABASE_FILENAME,
)


def integrity_check(path: Path) -> str:
    connection = sqlite3.connect(str(path))
    try:
        return str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()


def migrate_database(
    source: Path,
    target: Path,
    *,
    force: bool = False,
) -> Path:
    """Create a verified canonical copy while retaining the legacy source."""

    source = source.expanduser().resolve()
    target = target.expanduser().resolve()
    if source == target:
        raise ValueError("源数据库和目标数据库不能是同一个路径")
    if not source.is_file():
        raise FileNotFoundError(f"旧 SQLite 主库不存在: {source}")
    if target.exists() and not force:
        raise FileExistsError(
            f"新 SQLite 主库已存在: {target}；如确认覆盖请传入 --force"
        )
    if integrity_check(source).lower() != "ok":
        raise RuntimeError(f"旧 SQLite 主库完整性校验失败: {source}")

    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target.parent, 0o700)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.migration.tmp")
    temporary.unlink(missing_ok=True)

    replaced = False
    try:
        source_connection = sqlite3.connect(str(source))
        try:
            destination = sqlite3.connect(str(temporary))
            try:
                source_connection.backup(destination)
                destination.execute("PRAGMA journal_mode = DELETE")
                destination.commit()
            finally:
                destination.close()
        finally:
            source_connection.close()
        if integrity_check(temporary).lower() != "ok":
            raise RuntimeError("迁移后的 SQLite 主库完整性校验失败")
        os.chmod(temporary, 0o600)
        os.replace(temporary, target)
        replaced = True
        for suffix in ("-wal", "-shm"):
            Path(f"{target}{suffix}").unlink(missing_ok=True)
        os.chmod(target, 0o600)
    except Exception:
        if not replaced:
            with suppress(FileNotFoundError):
                temporary.unlink()
        raise
    return target


def _default_source() -> Path:
    return PROJECT_ROOT / "storage" / LEGACY_DATABASE_FILENAME


def _default_target() -> Path:
    return PROJECT_ROOT / "storage" / CANONICAL_DATABASE_FILENAME


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=_default_source())
    parser.add_argument("--target", type=Path, default=_default_target())
    parser.add_argument(
        "--execute",
        action="store_true",
        help="执行复制；未指定时只检查路径并输出 dry-run 计划",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="允许覆盖已存在的新数据库；旧数据库仍保留",
    )
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    target = args.target.expanduser().resolve()
    print(f"source={source}")
    print(f"target={target}")
    print(f"source_exists={source.is_file()}")
    print(f"target_exists={target.exists()}")

    if not args.execute:
        print("mode=dry-run")
        return 0 if source.is_file() else 1

    migrated = migrate_database(source, target, force=args.force)
    print(f"migrated={migrated}")
    print(f"integrity={integrity_check(migrated)}")
    print(f"legacy_preserved={source.exists()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
