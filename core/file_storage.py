"""原子文件发布与跨进程写锁。

SQLite 保存结构化业务事实；官方目录、文档详情和公共规范属于可重建的
文件缓存。它们仍然需要一个明确的发布协议，避免另一个进程在写文件时读到
半个 JSON。这个模块只负责小型 JSON 快照，不负责把大文件读入数据库。
"""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import uuid
from typing import Any, Iterator

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows falls back to the thread lock
    fcntl = None


_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _thread_locks_guard:
        lock = _thread_locks.get(key)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[key] = lock
        return lock


@contextmanager
def file_write_lock(path: str | os.PathLike[str]) -> Iterator[None]:
    """Serialize writes to one target across threads and POSIX processes."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    thread_lock = _lock_for(target)
    with thread_lock:
        lock_path = target.with_name(f".{target.name}.lock")
        with open(lock_path, "a+", encoding="utf-8") as lock_file:
            try:
                os.chmod(lock_path, 0o600)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _fsync_directory(directory: Path) -> None:
    """Persist the directory entry after ``os.replace`` where supported."""

    try:
        directory_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        os.close(directory_fd)


def atomic_write_json(
    path: str | os.PathLike[str],
    value: Any,
    *,
    mode: int = 0o600,
    indent: int = 2,
    default: Any | None = None,
) -> None:
    """Serialize and publish one complete JSON document atomically.

    The target is replaced only after JSON serialization, flush and ``fsync``
    succeed. A sibling lock file prevents two local processes from publishing
    competing snapshots at the same time.
    """

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with file_write_lock(target):
        temporary = target.with_name(
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            fd = os.open(
                str(temporary),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                mode,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(
                    value,
                    stream,
                    ensure_ascii=False,
                    indent=indent,
                    default=default,
                )
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, target)
            try:
                os.chmod(target, mode)
            except OSError:
                pass
            _fsync_directory(target.parent)
        except Exception:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise
