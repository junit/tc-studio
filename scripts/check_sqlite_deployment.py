"""Fail-closed deployment checks for the supported single-host SQLite mode."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.persistence import database_path, open_database  # noqa: E402


NETWORK_FS_TYPES = {
    "nfs",
    "nfs4",
    "cifs",
    "smbfs",
    "sshfs",
    "davfs",
    "fuse.s3fs",
    "s3fs",
    "gcsfuse",
    "afpfs",
}


def _mounts() -> list[tuple[Path, str]]:
    commands = [["mount", "-p"], ["mount"]]
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode != 0:
            continue
        mounts: list[tuple[Path, str]] = []
        for line in completed.stdout.splitlines():
            parts = line.split()
            if " on " in line:
                # Linux: "source on /mount type ext4 (options)"
                # macOS: "source on /mount (apfs, options)"
                try:
                    mount_description = line.split(" on ", 1)[1]
                    if " type " in mount_description:
                        mountpoint_text, type_description = mount_description.split(
                            " type ", 1
                        )
                        fstype = type_description.split("(", 1)[0].strip()
                    elif " (" in mount_description:
                        mountpoint_text, options = mount_description.split(
                            " (", 1
                        )
                        fstype = options.rstrip(")").split(",", 1)[0].strip()
                    else:
                        continue
                    mountpoint = Path(mountpoint_text.strip())
                    if fstype:
                        mounts.append((mountpoint, fstype.lower()))
                except (IndexError, ValueError):
                    continue
            elif len(parts) >= 3 and parts[1].startswith("/"):
                # BSD/macOS "mount -p" output commonly uses:
                # source mountpoint fstype options
                mountpoint = Path(parts[1])
                fstype = parts[2].lower()
                mounts.append((mountpoint, fstype))
        if mounts:
            return mounts
    return []


def filesystem_type(path: Path) -> str | None:
    resolved = path.resolve()
    try:
        completed = subprocess.run(
            ["findmnt", "-T", str(resolved), "-n", "-o", "FSTYPE"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        completed = None
    if completed is not None and completed.returncode == 0:
        fstype = completed.stdout.strip().splitlines()
        if fstype and fstype[0]:
            return fstype[0].lower()

    candidates = [
        (mountpoint, fstype)
        for mountpoint, fstype in _mounts()
        if str(resolved) == str(mountpoint)
        or str(resolved).startswith(str(mountpoint).rstrip("/") + "/")
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda item: len(str(item[0])))[1]


def check(
    database: str | None,
    blob_root: Path | None,
    results_root: Path | None = None,
    backup_root: Path | None = None,
    min_free_bytes: int = 0,
    data_root: Path | None = None,
    storage_root: Path | None = None,
) -> dict[str, Any]:
    path = Path(database_path(database)).resolve()
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(parent, 0o700)
    connection = open_database(str(path))
    try:
        journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0]).lower()
        synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
        foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
    finally:
        connection.close()

    checks: dict[str, Any] = {
        "database": str(path),
        "platform": platform.platform(),
        "journal_mode": journal_mode,
        "synchronous": synchronous,
        "foreign_keys": foreign_keys == 1,
        "integrity": integrity,
        "database_mode": oct(path.stat().st_mode & 0o777) if path.exists() else None,
        "parent_mode": oct(parent.stat().st_mode & 0o777),
        "free_bytes": shutil.disk_usage(parent).free,
        "min_free_bytes": max(0, int(min_free_bytes)),
        "database_filesystem": filesystem_type(path),
        "blob_filesystem": None,
        "results_filesystem": None,
        "data_filesystem": None,
        "storage_filesystem": None,
        "same_device": None,
        "same_device_results": None,
        "same_device_data": None,
        "same_device_storage": None,
        "network_filesystem": False,
        "sidecar_modes": {},
        "backup_modes": {},
        "passed": True,
        "errors": [],
    }
    if blob_root is not None:
        blob_root = blob_root.expanduser().resolve()
        blob_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(blob_root, 0o700)
        checks["blob_root"] = str(blob_root)
        checks["blob_filesystem"] = filesystem_type(blob_root)
        checks["same_device"] = path.stat().st_dev == blob_root.stat().st_dev

    if results_root is not None:
        results_root = results_root.expanduser().resolve()
        results_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(results_root, 0o700)
        checks["results_root"] = str(results_root)
        checks["results_filesystem"] = filesystem_type(results_root)
        checks["same_device_results"] = path.stat().st_dev == results_root.stat().st_dev

    for name, root in (("data", data_root), ("storage", storage_root)):
        if root is None:
            continue
        root = root.expanduser().resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        checks[f"{name}_root"] = str(root)
        checks[f"{name}_filesystem"] = filesystem_type(root)
        checks[f"same_device_{name}"] = path.stat().st_dev == root.stat().st_dev

    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            checks["sidecar_modes"][suffix] = oct(
                sidecar.stat().st_mode & 0o777
            )

    if backup_root is not None:
        backup_root = backup_root.expanduser().resolve()
        backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(backup_root, 0o700)
        checks["backup_root"] = str(backup_root)
        for backup in sorted(backup_root.glob("*.sqlite3")):
            checks["backup_modes"][str(backup)] = oct(
                backup.stat().st_mode & 0o777
            )

    fs_types = {
        checks["database_filesystem"],
        checks["blob_filesystem"],
        checks["results_filesystem"],
        checks["data_filesystem"],
        checks["storage_filesystem"],
    }
    checks["network_filesystem"] = bool(fs_types & NETWORK_FS_TYPES)
    errors: list[str] = checks["errors"]
    expected_filesystem_keys = ["database_filesystem"]
    if blob_root is not None:
        expected_filesystem_keys.append("blob_filesystem")
    if results_root is not None:
        expected_filesystem_keys.append("results_filesystem")
    if data_root is not None:
        expected_filesystem_keys.append("data_filesystem")
    if storage_root is not None:
        expected_filesystem_keys.append("storage_filesystem")
    unknown_filesystems = [
        key for key in expected_filesystem_keys if checks.get(key) is None
    ]
    checks["filesystem_types_known"] = not unknown_filesystems
    if unknown_filesystems:
        errors.append(
            "无法识别 SQLite/运行期目录的文件系统类型，拒绝放行: "
            + ", ".join(unknown_filesystems)
        )
    if journal_mode != "wal":
        errors.append("SQLite journal_mode 必须为 WAL")
    if synchronous != 2:
        errors.append("SQLite synchronous 必须为 FULL (2)")
    if not checks["foreign_keys"]:
        errors.append("SQLite foreign_keys 必须启用")
    if integrity.lower() != "ok":
        errors.append("SQLite integrity_check 未通过")
    if (path.stat().st_mode & 0o077) != 0:
        errors.append("数据库文件权限必须收紧到 0600 或更严格")
    if (parent.stat().st_mode & 0o077) != 0:
        errors.append("数据库父目录权限必须收紧到 0700 或更严格")
    if checks["free_bytes"] < checks["min_free_bytes"]:
        errors.append(
            "数据库所在文件系统剩余空间低于发布预留阈值: "
            f"{checks['free_bytes']} < {checks['min_free_bytes']}"
        )
    for suffix, mode in checks["sidecar_modes"].items():
        if int(mode, 8) & 0o077:
            errors.append(f"SQLite {suffix} 文件权限必须收紧到 0600 或更严格")
    if results_root is not None:
        if (results_root.stat().st_mode & 0o077) != 0:
            errors.append("结果目录权限必须收紧到 0700 或更严格")
    if backup_root is not None:
        if (backup_root.stat().st_mode & 0o077) != 0:
            errors.append("备份目录权限必须收紧到 0700 或更严格")
        for backup, mode in checks["backup_modes"].items():
            if int(mode, 8) & 0o077:
                errors.append(f"备份文件权限必须收紧到 0600 或更严格: {backup}")
    if checks["network_filesystem"]:
        errors.append("SQLite/Blob 禁止放在 NFS、SMB、对象存储挂载或其他网络文件系统")
    if blob_root is not None and not checks["same_device"]:
        errors.append("SQLite 与 Blob 根目录必须位于同一台主机可管理的本地存储边界")
    if results_root is not None and not checks["same_device_results"]:
        errors.append("SQLite 与结果目录必须位于同一台主机可管理的本地存储边界")
    if data_root is not None:
        data_path = Path(str(checks["data_root"]))
        if (data_path.stat().st_mode & 0o077) != 0:
            errors.append("BOT/RAG 数据目录权限必须收紧到 0700 或更严格")
        if not checks["same_device_data"]:
            errors.append("SQLite 与 BOT/RAG 数据目录必须位于同一台主机可管理的本地存储边界")
    if storage_root is not None:
        storage_path = Path(str(checks["storage_root"]))
        if (storage_path.stat().st_mode & 0o077) != 0:
            errors.append("运行期存储目录权限必须收紧到 0700 或更严格")
        if not checks["same_device_storage"]:
            errors.append("SQLite 与运行期存储目录必须位于同一台主机可管理的本地存储边界")
    checks["passed"] = not errors
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default=None)
    parser.add_argument("--blob-root", type=Path, default=None)
    parser.add_argument("--results-root", type=Path, default=None)
    parser.add_argument("--backup-root", type=Path, default=None)
    parser.add_argument(
        "--min-free-bytes",
        type=int,
        default=0,
        help="文件系统至少需要保留的可用字节数；大于 0 时不足即失败",
    )
    parser.add_argument("--data-root", type=Path, default=None)
    parser.add_argument("--storage-root", type=Path, default=None)
    args = parser.parse_args()
    if args.min_free_bytes < 0:
        raise SystemExit("--min-free-bytes 不能小于 0")
    result = check(
        args.database,
        args.blob_root,
        args.results_root,
        args.backup_root,
        args.min_free_bytes,
        args.data_root,
        args.storage_root,
    )
    import json

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
