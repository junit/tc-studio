"""
批处理任务调度器与 SSE 事件流推送
"""

from __future__ import annotations

import json
import hashlib
import os
import queue
import random
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from .cloud_client import UniversalCloudClient
from .config import load_config
from .persistence import (
    BATCH_TASK_LEASE_SECONDS,
    BATCH_TERMINAL_STATUSES,
    SqliteTaskStore,
    database_path,
    open_database,
)
from .registry import registry
from .agent.risk import is_high_risk_api

_DEFAULT_STORAGE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "storage")
)
STORAGE_DIR = os.path.abspath(
    os.environ.get("TIM_STORAGE_DIR") or _DEFAULT_STORAGE_DIR
)
HISTORY_PATH = os.path.abspath(
    os.environ.get("TIM_HISTORY_PATH") or os.path.join(STORAGE_DIR, "history.json")
)
RESULTS_DIR = os.path.abspath(
    os.environ.get("TIM_RESULTS_DIR") or os.path.join(STORAGE_DIR, "batch_results")
)
# Keep the old constant name as a compatibility seam for tests and downstream
# callers. The default now points to the unified SQLite database; the legacy
# standalone file is imported by core.persistence on first open.
IDEMPOTENCY_DB_PATH = database_path()
_history_lock = threading.Lock()
MAX_CACHED_TASKS = 50
MAX_EVENTS_PER_TASK = 2000
MAX_ITEMS_PER_TASK = 100_000
MAX_INLINE_HISTORY_RESULTS = 1_000
MAX_IDEMPOTENCY_KEY_LENGTH = 256
IDEMPOTENCY_RETENTION_SECONDS = 24 * 60 * 60
IDEMPOTENCY_GC_INTERVAL_SECONDS = 24 * 60 * 60
IDEMPOTENCY_EXPIRABLE_STATES = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED"}
)
ORPHAN_RESULTS_MAX_AGE_SECONDS = 30 * 24 * 60 * 60
ORPHAN_RESULTS_GC_INTERVAL_SECONDS = 24 * 60 * 60
MAX_TASK_INPUT_BYTES = 32 * 1024 * 1024
# Rate limiting is transient: retry a single item three times after its
# initial request, with bounded exponential backoff and jitter.
MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_BASE_SEC = 0.2
RATE_LIMIT_BACKOFF_JITTER_SEC = 0.1
RATE_LIMIT_ERROR_CODES = frozenset({"90001", "429", "requestlimitexceeded"})


class IdempotencyConflict(ValueError):
    """The same idempotency key was reused for a different request."""


class InvalidIdempotencyKey(ValueError):
    """The request supplied an invalid Idempotency-Key value."""


class ResultPersistenceError(RuntimeError):
    """A result row could not be durably written and indexed."""


def _normalize_idempotency_key(value: str | None) -> str | None:
    if value is None:
        return None
    key = str(value).strip()
    if not key:
        return None
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise InvalidIdempotencyKey(
            f"Idempotency-Key 不能超过 {MAX_IDEMPOTENCY_KEY_LENGTH} 个字符"
        )
    return key


def batch_request_fingerprint(
    api_id: str,
    items: list[dict],
    delay_sec: float,
    dry_run: bool,
    allow_high_risk: bool,
    *,
    approved_high_risk_paths: list[str] | None = None,
) -> str:
    """Create a stable fingerprint for an idempotent batch submission."""
    payload = {
        "api_id": api_id,
        "items": items,
        "delay_sec": float(delay_sec),
        "dry_run": bool(dry_run),
        "allow_high_risk": bool(allow_high_risk),
        "approved_high_risk_paths": sorted(
            str(path).strip().lower()
            for path in (approved_high_risk_paths or [])
            if str(path).strip()
        ),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _open_idempotency_db() -> sqlite3.Connection:
    return open_database(IDEMPOTENCY_DB_PATH)


def cleanup_expired_idempotency_records(now: float | None = None) -> int:
    """Delete only terminal, safely reusable idempotency records.

    ``UNKNOWN`` is deliberately retained indefinitely: it means an external
    side effect may have happened and the result was lost.  Expiring that row
    would allow the same key to create a second side effect after a restart.
    """
    cutoff = (time.time() if now is None else float(now)) - IDEMPOTENCY_RETENTION_SECONDS
    connection = _open_idempotency_db()
    try:
        placeholders = ", ".join("?" for _ in IDEMPOTENCY_EXPIRABLE_STATES)
        cursor = connection.execute(
            f"""
            DELETE FROM idempotency_keys
            WHERE operation_kind = 'batch'
              AND state IN ({placeholders})
              AND created_at < ?
            """,
            (*sorted(IDEMPOTENCY_EXPIRABLE_STATES), cutoff),
        )
        connection.commit()
        return int(cursor.rowcount)
    finally:
        connection.close()


def _reserve_idempotency(
    owner_id: str | None,
    issuer: str | None,
    tenant_id: str | None,
    idempotency_key: str,
    request_hash: str,
    task_id: str,
    api_id: str,
    total: int,
    delay_sec: float,
    dry_run: bool,
    items_json: str,
    environment_id: str | None,
) -> tuple[bool, dict[str, Any] | None]:
    """Atomically reserve a key and create its durable task reservation."""
    owner_scope = owner_id or ""
    issuer_scope = issuer or ""
    tenant_scope = tenant_id or ""
    now = time.time()
    connection = _open_idempotency_db()
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT request_hash, task_id, api_id, total, delay_sec, dry_run,
                   created_at, state, lease_until, worker_id
            FROM idempotency_keys
            WHERE issuer = ? AND tenant_id = ? AND owner_subject = ?
              AND operation_kind = 'batch' AND idempotency_key = ?
            """,
            (issuer_scope, tenant_scope, owner_scope, idempotency_key),
        ).fetchone()
        if row is not None:
            existing = {
                "request_hash": row[0],
                "task_id": row[1],
                "api_id": row[2],
                "total": row[3],
                "delay_sec": row[4],
                "dry_run": bool(row[5]),
                "created_at": row[6],
                "state": row[7],
                "lease_until": row[8],
                "worker_id": row[9],
            }
            existing_state = str(existing["state"] or "").upper()
            tombstoned = connection.execute(
                """
                SELECT 1
                FROM batch_task_tombstones
                WHERE task_id = ?
                """,
                (str(existing["task_id"]),),
            ).fetchone() is not None
            if (
                now - float(existing["created_at"]) > IDEMPOTENCY_RETENTION_SECONDS
                and existing_state in IDEMPOTENCY_EXPIRABLE_STATES
            ):
                connection.execute(
                    """
                    DELETE FROM idempotency_keys
                    WHERE issuer = ? AND tenant_id = ? AND owner_subject = ?
                      AND operation_kind = 'batch' AND idempotency_key = ?
                    """,
                    (issuer_scope, tenant_scope, owner_scope, idempotency_key),
                )
            elif tombstoned:
                existing["state"] = "DELETED"
                connection.commit()
                return False, existing
            elif existing["request_hash"] != request_hash:
                raise IdempotencyConflict(
                    "同一 Idempotency-Key 已用于不同的批处理请求"
                )
            else:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO batch_tasks (
                        task_id, issuer, tenant_id, owner_subject, api_id,
                        status, current_index, total, success_count,
                        failed_count, dry_run, request_hash, items_json,
                        environment_id, created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, 0, ?, 0, 0, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        str(existing["task_id"]),
                        issuer_scope,
                        tenant_scope,
                        owner_scope,
                        str(existing["api_id"]),
                        str(existing["state"] or "RESERVED"),
                        int(existing["total"] or 0),
                        int(bool(existing["dry_run"])),
                        str(existing["request_hash"]),
                        items_json,
                        environment_id,
                        float(existing["created_at"]),
                        now,
                    ),
                )
                connection.commit()
                return False, existing

        connection.execute(
            """
            INSERT INTO idempotency_keys
                (
                    issuer, tenant_id, owner_subject, operation_kind,
                    idempotency_key, request_hash, task_id, api_id, total,
                    delay_sec, dry_run, state, created_at, updated_at
                )
            VALUES (?, ?, ?, 'batch', ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?)
            """,
            (
                issuer_scope,
                tenant_scope,
                owner_scope,
                idempotency_key,
                request_hash,
                task_id,
                api_id,
                total,
                float(delay_sec),
                int(bool(dry_run)),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO batch_tasks (
                task_id, issuer, tenant_id, owner_subject, api_id, status,
                current_index, total, success_count, failed_count, dry_run,
                request_hash, items_json, environment_id, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, 'RESERVED', 0, ?, 0, 0, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                task_id,
                issuer_scope,
                tenant_scope,
                owner_scope,
                api_id,
                int(total),
                int(bool(dry_run)),
                request_hash,
                items_json,
                environment_id,
                now,
                now,
            ),
        )
        connection.commit()
        return True, None
    except Exception:
        with suppress(Exception):
            connection.rollback()
        raise
    finally:
        connection.close()


def _release_idempotency(
    owner_id: str | None,
    issuer: str | None,
    tenant_id: str | None,
    idempotency_key: str,
    task_id: str,
) -> None:
    connection = _open_idempotency_db()
    try:
        connection.execute(
            """
            DELETE FROM idempotency_keys
            WHERE issuer = ? AND tenant_id = ? AND owner_subject = ?
              AND operation_kind = 'batch' AND idempotency_key = ? AND task_id = ?
            """,
            (issuer or "", tenant_id or "", owner_id or "", idempotency_key, task_id),
        )
        connection.commit()
    finally:
        connection.close()


def _idempotency_task_exists(task_id: str) -> bool:
    connection = _open_idempotency_db()
    try:
        row = connection.execute(
            "SELECT 1 FROM idempotency_keys WHERE operation_kind = 'batch' AND task_id = ? LIMIT 1",
            (task_id,),
        ).fetchone()
        return row is not None
    finally:
        connection.close()


def _is_rate_limited_response(response: Any) -> bool:
    """Return whether a normalized Tencent response represents rate limiting.

    IM uses numeric ``ErrorCode: 90001`` while HTTP-level throttling is
    normalized by the clients to ``ErrorCode: 429``.  Keep the message check
    as a compatibility fallback for clients that preserve only HTTP text.
    """
    if not isinstance(response, dict):
        return False
    code = response.get("ErrorCode")
    if str(code).strip().lower() in RATE_LIMIT_ERROR_CODES:
        return True
    info = str(response.get("ErrorInfo", ""))
    return bool(re.search(r"(?:^|\b)HTTP\s*429(?:\b|$)", info, re.IGNORECASE))


def _atomic_dump_json(filepath: str, data: Any, indent: int = 2) -> None:
    """原子化写入 JSON 文件，避免多线程并发导致历史文件截断损坏"""
    try:
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with suppress(OSError):
            os.chmod(os.path.dirname(filepath), 0o700)
    except OSError as exc:
        raise RuntimeError(f"无法创建历史目录: {filepath}") from exc
    tmp_path = f"{filepath}.{os.getpid()}.{threading.get_ident()}.{int(time.time() * 1000)}.tmp"
    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, 0o600)
        os.replace(tmp_path, filepath)
    except Exception:
        with suppress(OSError):
            os.remove(tmp_path)
        raise


def _load_legacy_history_unlocked() -> list[dict]:
    if not os.path.exists(HISTORY_PATH):
        return []
    try:
        with open(HISTORY_PATH, encoding="utf-8") as stream:
            loaded = json.load(stream)
        return loaded if isinstance(loaded, list) else []
    except Exception as exc:
        print(f"[WARN] Failed to load legacy history: {exc}")
        return []


def _normalise_history_status(value: Any) -> str:
    status = str(value or "COMPLETED").upper()
    if status not in {
        "RESERVED",
        "RUNNING",
        "PAUSED",
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "UNKNOWN",
        "EXPIRED",
    }:
        return "COMPLETED"
    return status


def _history_summary_from_row(
    row: dict[str, Any],
    legacy_item: dict[str, Any] | None = None,
) -> dict[str, Any]:
    legacy_item = legacy_item if isinstance(legacy_item, dict) else {}
    created = float(row.get("created_at") or time.time())
    started = row.get("started_at")
    finished = row.get("finished_at")
    success_count = int(row.get("success_count") or 0)
    failed_count = int(row.get("failed_count") or 0)
    summary = {
        "task_id": str(row.get("task_id") or ""),
        "api_id": row.get("api_id"),
        "issuer": row.get("issuer") or "",
        "tenant_id": row.get("tenant_id") or "",
        "owner_id": row.get("owner_subject") or None,
        "total": int(row.get("total") or 0),
        "success": success_count,
        "failed": failed_count,
        "status": row.get("status"),
        "created_at": time.strftime(
            "%Y-%m-%d %H:%M:%S",
            time.localtime(created),
        ),
        "result_count": success_count + failed_count,
    }
    for field in ("api_name", "service_type"):
        if field in legacy_item:
            summary[field] = legacy_item[field]
    if row.get("result_location"):
        summary["results_path"] = row["result_location"]
    elif isinstance(legacy_item.get("results"), list):
        # Old inline results are a Blob compatibility path. SQLite still owns
        # task status, owner, counters and deletion semantics.
        summary["results"] = legacy_item["results"]
        summary["result_count"] = len(legacy_item["results"])
    if started is not None and finished is not None:
        summary["duration_sec"] = round(
            max(0.0, float(finished) - float(started)),
            2,
        )
    return summary


def _rewrite_legacy_history_unlocked(retained: list[dict]) -> None:
    if not os.path.exists(HISTORY_PATH):
        return
    _atomic_dump_json(HISTORY_PATH, retained, indent=2)


def load_history() -> list[dict]:
    """Return task history from SQLite, importing legacy JSON only once per row.

    ``storage/history.json`` is now a migration/compatibility snapshot. It can
    contribute an old row that is absent from SQLite, but it can never override
    a durable row and a tombstoned task can never be resurrected from it.
    """

    with _history_lock:
        legacy_history = _load_legacy_history_unlocked()

    store = SqliteTaskStore(IDEMPOTENCY_DB_PATH)
    legacy_by_id = {
        str(item.get("task_id")): item
        for item in legacy_history
        if isinstance(item, dict) and item.get("task_id")
    }

    # Compatibility import is additive and idempotent. A deleted task has a
    # tombstone in SQLite, so a stale legacy snapshot cannot recreate it.
    for item in legacy_history:
        if not isinstance(item, dict) or not item.get("task_id"):
            continue
        task_id = str(item["task_id"])
        if store.get_batch_task(task_id, reconcile_expired=False) is not None:
            continue
        if store.is_batch_task_tombstoned(task_id):
            continue
        store.create_batch_task(
            {
                "task_id": task_id,
                "issuer": item.get("issuer") or "",
                "tenant_id": item.get("tenant_id") or "",
                "owner_subject": item.get("owner_id") or "",
                "api_id": str(item.get("api_id") or ""),
                "status": _normalise_history_status(item.get("status")),
                "total": int(item.get("total") or 0),
                "success_count": int(item.get("success") or 0),
                "failed_count": int(item.get("failed") or 0),
                "result_location": item.get("results_path"),
            }
        )

    rows = store.list_batch_tasks(include_all=True, limit=MAX_CACHED_TASKS)
    return [
        _history_summary_from_row(row, legacy_by_id.get(str(row.get("task_id"))))
        for row in rows
        if row.get("task_id")
    ]


def save_history_item(batch_record: dict):
    """Ensure the durable task row exists without writing the legacy JSON file."""

    status = _normalise_history_status(batch_record.get("status"))
    store = SqliteTaskStore(IDEMPOTENCY_DB_PATH)
    store.create_batch_task(
        {
            "task_id": str(batch_record["task_id"]),
            "issuer": batch_record.get("issuer") or "",
            "tenant_id": batch_record.get("tenant_id") or "",
            "owner_subject": batch_record.get("owner_id") or "",
            "api_id": str(batch_record.get("api_id") or ""),
            "status": status,
            "total": int(batch_record.get("total") or 0),
            "success_count": int(batch_record.get("success") or 0),
            "failed_count": int(batch_record.get("failed") or 0),
            "dry_run": bool(batch_record.get("dry_run")),
            "result_location": batch_record.get("results_path"),
        }
    )
    with suppress(Exception):
        cleanup_orphan_task_results()


def delete_history_item(task_id: str) -> bool:
    store = SqliteTaskStore(IDEMPOTENCY_DB_PATH)
    # Import a legacy-only row before deciding whether it exists.
    load_history()
    durable_record = store.get_batch_task(
        task_id,
        reconcile_expired=False,
    )
    if durable_record is None:
        return False
    if str(durable_record.get("status")) not in BATCH_TERMINAL_STATUSES:
        return False
    if not store.delete_batch_task(task_id):
        return False

    with suppress(OSError):
        os.remove(task_results_path(task_id))
    with _history_lock:
        legacy_history = _load_legacy_history_unlocked()
        retained = [
            item
            for item in legacy_history
            if not isinstance(item, dict) or str(item.get("task_id")) != str(task_id)
        ]
        with suppress(Exception):
            _rewrite_legacy_history_unlocked(retained)
    return True


def clear_history() -> bool:
    store = SqliteTaskStore(IDEMPOTENCY_DB_PATH)
    try:
        # This also imports any legacy-only records before the terminal set is
        # calculated, while SQLite remains authoritative for the final result.
        load_history()
        durable_rows = store.list_batch_tasks(
            include_all=True,
            limit=100_000,
        )
        terminal_ids = {
            str(row.get("task_id"))
            for row in durable_rows
            if row.get("task_id")
            and str(row.get("status") or "").upper() in BATCH_TERMINAL_STATUSES
        }
        store.delete_all_batch_tasks()
    except Exception as exc:
        print(f"[WARN] Failed to clear terminal batch history: {exc}")
        return False

    with _history_lock:
        legacy_history = _load_legacy_history_unlocked()
        retained_history = [
            item
            for item in legacy_history
            if not (
                isinstance(item, dict)
                and str(item.get("task_id")) in terminal_ids
            )
        ]
        with suppress(Exception):
            _rewrite_legacy_history_unlocked(retained_history)

    for task_id in terminal_ids:
        with suppress(OSError):
            os.remove(task_results_path(task_id))
    with suppress(OSError):
        os.rmdir(RESULTS_DIR)
    return True

def task_results_path(task_id: str) -> str:
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", str(task_id))
    return os.path.join(RESULTS_DIR, f"task_{safe_id}.jsonl")


def cleanup_orphan_task_results(max_age_seconds: int = ORPHAN_RESULTS_MAX_AGE_SECONDS) -> int:
    """Remove stale JSONL result files no longer referenced by history.

    A crash can leave a task result file behind before its summary reaches
    ``history.json``.  Only files older than the retention window are eligible,
    so an in-flight or recently completed task remains recoverable.
    """
    if not os.path.isdir(RESULTS_DIR):
        return 0
    try:
        valid_ids = {
            str(record.get("task_id"))
            for record in load_history()
            if isinstance(record, dict) and record.get("task_id")
        }
        with _history_lock:
            valid_ids.update(
                str(record.get("task_id"))
                for record in _load_legacy_history_unlocked()
                if isinstance(record, dict) and record.get("task_id")
            )
    except Exception:
        valid_ids = set()
    cutoff = time.time() - max(0, int(max_age_seconds))
    removed = 0
    try:
        names = os.listdir(RESULTS_DIR)
    except OSError:
        return 0
    for name in names:
        if not name.startswith("task_") or not name.endswith(".jsonl"):
            continue
        task_id = name[5:-6]
        if task_id in valid_ids:
            continue
        path = os.path.join(RESULTS_DIR, name)
        try:
            if not os.path.isfile(path) or os.path.getmtime(path) >= cutoff:
                continue
            os.remove(path)
            removed += 1
        except OSError:
            continue
    return removed


def append_task_result(task_id: str, result: dict[str, Any]) -> str:
    """Append one batch result and publish its durable index.

    The JSONL file is the Blob layer and ``batch_task_items`` is the database
    index.  They cannot share one atomic transaction, so the safe ordering is
    file ``fsync`` first, then the index insert.  If the index insert fails,
    truncate the just-written bytes and fail the task instead of silently
    continuing with an unindexed result.
    """
    os.makedirs(RESULTS_DIR, mode=0o700, exist_ok=True)
    with suppress(OSError):
        os.chmod(RESULTS_DIR, 0o700)
    path = task_results_path(task_id)
    encoded = (
        json.dumps(
            result,
            ensure_ascii=False,
            default=str,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    store = SqliteTaskStore(IDEMPOTENCY_DB_PATH)
    try:
        sequence = store.count_batch_task_items(task_id, result_uri=path)
    except Exception as exc:
        raise ResultPersistenceError("读取批处理结果索引失败") from exc

    def rollback_file(offset: int) -> None:
        try:
            with open(path, "r+b") as rollback_handle:
                rollback_handle.truncate(offset)
                rollback_handle.flush()
                os.fsync(rollback_handle.fileno())
        except OSError as rollback_error:
            raise ResultPersistenceError(
                "批处理结果索引写入失败，且结果文件回滚失败"
            ) from rollback_error

    try:
        with open(path, "ab") as handle:
            byte_offset = handle.tell()
            try:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            except Exception:
                rollback_file(byte_offset)
                raise
    except ResultPersistenceError:
        raise
    except Exception as exc:
        raise ResultPersistenceError("写入批处理结果文件失败") from exc

    with suppress(OSError):
        os.chmod(path, 0o600)

    try:
        indexed = store.append_batch_task_item(
            task_id,
            sequence=sequence,
            status=str(result.get("_status") or "UNKNOWN"),
            error_code=result.get("_err_code"),
            result_sha256=hashlib.sha256(encoded).hexdigest(),
            byte_offset=byte_offset,
            byte_length=len(encoded),
            result_uri=path,
        )
    except Exception as exc:
        rollback_file(byte_offset)
        raise ResultPersistenceError("写入批处理结果索引失败") from exc
    if not indexed:
        rollback_file(byte_offset)
        raise ResultPersistenceError("批处理结果索引未写入，拒绝继续执行")

    return path


def _result_consistency_result(
    *,
    file_exists: bool,
    indexed_count: int,
    file_count: int,
    consistent: bool,
    error: str | None = None,
) -> dict[str, Any]:
    result = {
        "file_exists": file_exists,
        "indexed_count": indexed_count,
        "file_count": file_count,
        "consistent": consistent,
    }
    if error:
        result["error"] = error
    return result


def task_result_consistency(task_id: str) -> dict[str, Any]:
    """Verify the JSONL body against every SQLite result-index row.

    A count-only check misses same-length tampering and stale trailing bytes.
    The index already stores offsets, lengths and SHA-256 values, so validate
    those fields before an export or history response is allowed to read the
    file.
    """
    path = task_results_path(task_id)
    file_exists = os.path.isfile(path)
    store = SqliteTaskStore(IDEMPOTENCY_DB_PATH)
    try:
        indexed_count = store.count_batch_task_items(
            task_id,
            result_uri=path,
        )
        indexed_rows = store.list_batch_task_items(
            task_id,
            limit=max(1, indexed_count),
        )
        task_record = store.get_batch_task(
            task_id,
            reconcile_expired=False,
        )
    except Exception:
        return _result_consistency_result(
            file_exists=file_exists,
            indexed_count=0,
            file_count=0,
            consistent=False,
            error="结果数据库索引不可读",
        )

    current_task_has_results = bool(
        task_record
        and (
            task_record.get("result_location")
            or int(task_record.get("current_index") or 0) > 0
            or (
                int(task_record.get("success_count") or 0)
                + int(task_record.get("failed_count") or 0)
            )
            > 0
        )
    )

    if not file_exists:
        return _result_consistency_result(
            file_exists=False,
            indexed_count=indexed_count,
            file_count=0,
            consistent=indexed_count == 0 and not current_task_has_results,
            error=(
                "结果文件缺失"
                if indexed_count or current_task_has_results
                else None
            ),
        )

    if indexed_count != len(indexed_rows):
        return _result_consistency_result(
            file_exists=True,
            indexed_count=indexed_count,
            file_count=len(indexed_rows),
            consistent=False,
            error="结果数据库索引读取不完整",
        )

    file_count = 0
    try:
        if indexed_count == 0:
            # Legacy history rows may predate the SQLite result index.  Keep
            # those readable, but a current durable task that already reports
            # progress must fail closed when its index is empty.
            malformed = False
            with open(path, "rb") as handle:
                for raw_line in handle:
                    if not raw_line.strip():
                        continue
                    file_count += 1
                    try:
                        decoded = json.loads(raw_line.decode("utf-8"))
                    except (UnicodeDecodeError, TypeError, ValueError):
                        malformed = True
                        continue
                    if not isinstance(decoded, dict):
                        malformed = True
            consistent = not malformed and not current_task_has_results
            return _result_consistency_result(
                file_exists=True,
                indexed_count=0,
                file_count=file_count,
                consistent=consistent,
                error=(
                    "结果文件存在但数据库索引缺失"
                    if current_task_has_results
                    else "结果文件包含无法解析的行"
                    if malformed
                    else None
                ),
            )

        expected_offset = 0
        with open(path, "rb") as handle:
            for row in indexed_rows:
                byte_offset = int(row["byte_offset"])
                byte_length = int(row["byte_length"])
                if byte_offset != expected_offset or byte_length <= 0:
                    return _result_consistency_result(
                        file_exists=True,
                        indexed_count=indexed_count,
                        file_count=file_count,
                        consistent=False,
                        error="结果索引偏移或长度不连续",
                    )
                handle.seek(byte_offset)
                encoded = handle.read(byte_length)
                if len(encoded) != byte_length:
                    return _result_consistency_result(
                        file_exists=True,
                        indexed_count=indexed_count,
                        file_count=file_count,
                        consistent=False,
                        error="结果文件被截断",
                    )
                if hashlib.sha256(encoded).hexdigest() != str(row["result_sha256"]):
                    return _result_consistency_result(
                        file_exists=True,
                        indexed_count=indexed_count,
                        file_count=file_count,
                        consistent=False,
                        error="结果文件哈希与数据库索引不一致",
                    )
                if not encoded.endswith(b"\n"):
                    return _result_consistency_result(
                        file_exists=True,
                        indexed_count=indexed_count,
                        file_count=file_count,
                        consistent=False,
                        error="结果文件存在未完成的 JSONL 行",
                    )
                try:
                    decoded = json.loads(encoded.decode("utf-8"))
                except (UnicodeDecodeError, TypeError, ValueError):
                    return _result_consistency_result(
                        file_exists=True,
                        indexed_count=indexed_count,
                        file_count=file_count,
                        consistent=False,
                        error="结果文件存在无法解析的 JSONL 行",
                    )
                if not isinstance(decoded, dict):
                    return _result_consistency_result(
                        file_exists=True,
                        indexed_count=indexed_count,
                        file_count=file_count,
                        consistent=False,
                        error="结果文件行不是 JSON 对象",
                    )
                file_count += 1
                expected_offset += byte_length
            trailing = handle.read()
            if trailing:
                return _result_consistency_result(
                    file_exists=True,
                    indexed_count=indexed_count,
                    file_count=file_count,
                    consistent=False,
                    error="结果文件存在未建立索引的尾部数据",
                )
    except (OSError, UnicodeError) as exc:
        return _result_consistency_result(
            file_exists=True,
            indexed_count=indexed_count,
            file_count=file_count,
            consistent=False,
            error=f"结果文件不可读: {type(exc).__name__}",
        )

    return _result_consistency_result(
        file_exists=True,
        indexed_count=indexed_count,
        file_count=file_count,
        consistent=True,
    )


def iter_task_results(task_id: str, offset: int = 0, limit: int | None = None):
    path = task_results_path(task_id)
    if not os.path.isfile(path):
        return iter(())

    def _iterator():
        seen = 0
        emitted = 0
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                if seen < offset:
                    seen += 1
                    continue
                if limit is not None and emitted >= limit:
                    break
                seen += 1
                try:
                    item = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if isinstance(item, dict):
                    yield item
                    emitted += 1

    return _iterator()


def count_task_results(task_id: str) -> int:
    path = task_results_path(task_id)
    if not os.path.isfile(path):
        return 0
    indexed_count = 0
    with suppress(Exception):
        indexed_count = SqliteTaskStore(IDEMPOTENCY_DB_PATH).count_batch_task_items(
            task_id,
            result_uri=path,
        )
    if indexed_count:
        return indexed_count
    with open(path, "r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


class BatchTask:
    def __init__(
        self,
        task_id: str,
        api_id: str,
        items: list[dict],
        delay_sec: float = 0.05,
        dry_run: bool = False,
        config_snapshot: dict | None = None,
        owner_id: str | None = None,
        idempotent_replay: bool = False,
        replay_only: bool = False,
        database_path_override: str | None = None,
        environment_id: str | None = None,
        issuer: str | None = None,
        tenant_id: str | None = None,
        persistent: bool = True,
    ):

        self.task_id = task_id
        self.api_id = api_id
        self.items = items
        self.delay_sec = delay_sec
        self.dry_run = dry_run
        self.config_snapshot = config_snapshot
        self.owner_id = owner_id
        self.issuer = str(issuer or "")
        self.tenant_id = str(tenant_id or "")
        self.idempotent_replay = idempotent_replay
        self.replay_only = replay_only
        self.environment_id = environment_id
        self.persistent = persistent
        self._store = (
            SqliteTaskStore(database_path_override)
            if persistent
            else None
        )
        self.total = len(items)
        self.current = 0
        self.success = 0
        self.failed = 0
        self.status = "PENDING"
        self.results = []
        self.events: list[dict] = []
        self.event_queue = queue.Queue(maxsize=MAX_EVENTS_PER_TASK)  # 兼容向后单队列引用
        self._subscribers: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.start_time: float | None = None
        self.end_time: float | None = None
        self._started = False
        self._finalized = False
        self._finalize_callback: Callable[[], None] | None = None
        self._local_execution = False
        self.worker_id: str | None = None
        self.last_heartbeat: float | None = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # set means running, clear means paused
        self._lease_stop_event = threading.Event()

    def _persist_event(self, event_type: str, data: dict, timestamp: float) -> None:
        if self._store is None:
            return
        with suppress(Exception):
            self._store.append_task_event(
                self.task_id,
                event_type,
                data,
                timestamp=timestamp,
            )

    def _persist_progress(self) -> None:
        if self._store is None:
            return
        with suppress(Exception):
            self._store.update_batch_task(
                self.task_id,
                current_index=self.current,
                success_count=self.success,
                failed_count=self.failed,
                last_heartbeat=self.last_heartbeat,
                expected_worker_id=self.worker_id,
            )

    def refresh_from_store(self) -> dict[str, Any] | None:
        if self._store is None:
            return None
        record = self._store.get_batch_task(self.task_id)
        if record is None:
            return None
        with self._lock:
            persisted_status = str(record.get("status") or self.status)
            # The lease loop also sets _stop_event when it quarantines a task
            # as UNKNOWN. Treating every stop event as an explicit cancel
            # would turn an indeterminate external side effect into a false
            # CANCELLED/COMPLETED result.
            local_stop_requested = self.status == "CANCELLED" or (
                self._stop_event.is_set()
                and self.status not in {"UNKNOWN", "EXPIRED"}
            )
            if local_stop_requested and persisted_status in {
                "RESERVED",
                "PENDING",
                "RUNNING",
                "PAUSED",
                "UNKNOWN",
                "EXPIRED",
            }:
                self.status = "CANCELLED"
            else:
                self.status = persisted_status
            self.current = int(record.get("current_index") or self.current)
            self.success = int(record.get("success_count") or self.success)
            self.failed = int(record.get("failed_count") or self.failed)
            self.worker_id = record.get("worker_id")
            self.last_heartbeat = record.get("last_heartbeat")
            if record.get("started_at") is not None:
                self.start_time = float(record["started_at"])
            if record.get("finished_at") is not None:
                self.end_time = float(record["finished_at"])
            if self.status in {"CANCELLED", "UNKNOWN", "EXPIRED"}:
                self._stop_event.set()
                self._pause_event.set()
            elif self.status == "PAUSED":
                self._pause_event.clear()
            elif self.status == "RUNNING":
                self._pause_event.set()
        return record

    def heartbeat(self) -> bool:
        if self._store is None or not self.worker_id:
            return False
        result = self._store.heartbeat_batch_task(
            self.task_id,
            self.worker_id,
            lease_seconds=BATCH_TASK_LEASE_SECONDS,
        )
        if result:
            self.last_heartbeat = time.time()
        return result

    def register_subscriber(self) -> tuple:
        """注册新订阅者并获取全量历史事件快照，支持无感重连防事件撕裂"""
        q = queue.Queue(maxsize=MAX_EVENTS_PER_TASK)
        with self._lock:
            self._subscribers.append(q)
            history = list(self.events)
        if self._store is not None:
            with suppress(Exception):
                persisted = [
                    event
                    for _sequence, event in self._store.list_task_events(self.task_id)
                ]
                if persisted:
                    history = persisted
        return q, history

    def unregister_subscriber(self, q: queue.Queue):
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def emit(self, event_type: str, data: dict):
        evt = {"type": event_type, "data": data, "timestamp": time.time()}
        with self._lock:
            self.events.append(evt)
            if len(self.events) > MAX_EVENTS_PER_TASK:
                del self.events[:-MAX_EVENTS_PER_TASK]
            subs = list(self._subscribers)
        self._persist_event(event_type, data, float(evt["timestamp"]))
        # 兼容老版单队列
        with suppress(queue.Full):
            self.event_queue.put_nowait(evt)
        for sub in subs:
            with suppress(queue.Full):
                sub.put_nowait(evt)

    def stop(self):
        callback = None
        with self._lock:
            if self._finalized:
                return
            self._stop_event.set()
            self.status = "CANCELLED"
            self._pause_event.set()
            if not self._started:
                callback = self._finalize_callback
        if self._store is not None:
            with suppress(Exception):
                self._store.request_control(self.task_id, "stop")
        if callback:
            callback()

    def pause(self):
        with self._lock:
            if self.status in ("PENDING", "CANCELLED", "COMPLETED", "FAILED"):
                return
            self._pause_event.clear()
            self.status = "PAUSED"
        if self._store is not None:
            with suppress(Exception):
                self._store.request_control(self.task_id, "pause")

    def resume(self):
        with self._lock:
            if self.status != "PAUSED":
                return
            self._pause_event.set()
            self.status = "RUNNING"
        if self._store is not None:
            with suppress(Exception):
                self._store.request_control(self.task_id, "resume")


class TaskRunner:
    def __init__(self):
        self.tasks: dict[str, BatchTask] = {}
        self._lock = threading.Lock()
        self._last_orphan_gc = 0.0
        self._last_idempotency_gc = 0.0
        self.worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"

    @staticmethod
    def _serialize_task_items(items: list[dict]) -> str:
        encoded = json.dumps(
            items,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        if len(encoded.encode("utf-8")) > MAX_TASK_INPUT_BYTES:
            raise ValueError(
                f"批处理请求体超过 {MAX_TASK_INPUT_BYTES // (1024 * 1024)} MiB 安全上限"
            )
        return encoded

    @staticmethod
    def _decode_task_items(items_json: str | None) -> list[dict]:
        if not items_json:
            return []
        try:
            decoded = json.loads(items_json)
        except (TypeError, ValueError):
            return []
        if not isinstance(decoded, list):
            return []
        return [item for item in decoded if isinstance(item, dict)]

    def _store(self) -> SqliteTaskStore:
        # Resolve the constant at call time so focused tests and embedded
        # deployments can select an isolated SQLite file before creating a
        # runner.
        return SqliteTaskStore(IDEMPOTENCY_DB_PATH)

    def _cache_task(self, task: BatchTask) -> None:
        with self._lock:
            if task.task_id in self.tasks:
                self.tasks[task.task_id] = task
                return
            if len(self.tasks) >= MAX_CACHED_TASKS:
                completed_keys = [
                    key
                    for key, cached in self.tasks.items()
                    if cached.status in (
                        "COMPLETED",
                        "CANCELLED",
                        "FAILED",
                        "UNKNOWN",
                    )
                ]
                for key in completed_keys[: len(self.tasks) - MAX_CACHED_TASKS + 1]:
                    self.tasks.pop(key, None)
                if len(self.tasks) >= MAX_CACHED_TASKS:
                    raise ValueError("批处理任务缓存已满，请等待现有任务结束")
            self.tasks[task.task_id] = task

    def _hydrate_task(
        self,
        record: dict[str, Any],
        *,
        fallback_items: list[dict] | None = None,
        idempotent_replay: bool = False,
        replay_only: bool | None = None,
    ) -> BatchTask:
        status = str(record.get("status") or "UNKNOWN")
        items = self._decode_task_items(record.get("items_json"))
        if not items and fallback_items is not None:
            items = list(fallback_items)
        config_snapshot = load_config()
        task = BatchTask(
            task_id=str(record["task_id"]),
            api_id=str(record["api_id"]),
            items=items,
            delay_sec=float(config_snapshot.get("delay_sec", 0.05)),
            dry_run=bool(record.get("dry_run")),
            config_snapshot=dict(config_snapshot),
            owner_id=record.get("owner_subject") or None,
            issuer=record.get("issuer") or "",
            tenant_id=record.get("tenant_id") or "",
            idempotent_replay=idempotent_replay,
            replay_only=(
                status
                in {
                    "RUNNING",
                    "PAUSED",
                    "COMPLETED",
                    "FAILED",
                    "CANCELLED",
                    "UNKNOWN",
                }
                if replay_only is None
                else replay_only
            ),
            database_path_override=IDEMPOTENCY_DB_PATH,
            environment_id=record.get("environment_id"),
        )
        task.status = status
        task.current = int(record.get("current_index") or 0)
        task.total = int(record.get("total") or len(items))
        task.success = int(record.get("success_count") or 0)
        task.failed = int(record.get("failed_count") or 0)
        task.worker_id = record.get("worker_id")
        task.last_heartbeat = record.get("last_heartbeat")
        task.start_time = record.get("started_at")
        task.end_time = record.get("finished_at")
        task._started = status in {
            "RUNNING",
            "PAUSED",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "UNKNOWN",
        }
        task._finalized = status in {
            "COMPLETED",
            "FAILED",
            "CANCELLED",
            "UNKNOWN",
        }
        if status == "PAUSED":
            task._pause_event.clear()
        if status == "CANCELLED":
            task._stop_event.set()
        return task

    def _maybe_cleanup_orphan_results(self) -> None:
        """Run bounded orphan-result GC at most once per day per runner.

        Module import performs an initial sweep; this periodic hook covers
        long-lived processes where stale files can accumulate after a crash.
        Cleanup is best-effort and never blocks task creation on an error.
        """
        now = time.time()
        if now - self._last_orphan_gc < ORPHAN_RESULTS_GC_INTERVAL_SECONDS:
            return
        self._last_orphan_gc = now
        with suppress(Exception):
            cleanup_orphan_task_results()

    def _maybe_cleanup_expired_idempotency(self) -> None:
        now = time.time()
        if now - self._last_idempotency_gc < IDEMPOTENCY_GC_INTERVAL_SECONDS:
            return
        self._last_idempotency_gc = now
        with suppress(Exception):
            cleanup_expired_idempotency_records(now)

    @staticmethod
    def _invoke_with_rate_limit_retry(client: UniversalCloudClient, api_meta: Any, payload: dict, stop_event: threading.Event | None = None) -> dict:
        """Invoke one item, retrying transient Tencent rate-limit responses.

        ``MAX_RATE_LIMIT_RETRIES`` counts retries after the initial request.
        A task cancellation interrupts the backoff wait, while the last
        response is still returned so the item is accounted for exactly once.
        """
        response = client.invoke_api(api_meta, payload)
        for retry_index in range(MAX_RATE_LIMIT_RETRIES):
            if not _is_rate_limited_response(response):
                break
            delay = (
                RATE_LIMIT_BACKOFF_BASE_SEC * (2 ** retry_index)
                + random.uniform(0, RATE_LIMIT_BACKOFF_JITTER_SEC)
            )
            if stop_event is not None:
                if stop_event.wait(delay):
                    break
            else:
                time.sleep(delay)
            response = client.invoke_api(api_meta, payload)
        return response

    def create_task(
        self,
        api_id: str,
        items: list[dict],
        delay_sec: float | None = None,
        dry_run: bool = False,
        owner_id: str | None = None,
        issuer: str | None = None,
        tenant_id: str | None = None,
        allow_high_risk: bool = False,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
    ) -> BatchTask:

        if len(items) > MAX_ITEMS_PER_TASK:
            raise ValueError(f"批处理任务行数超过 {MAX_ITEMS_PER_TASK} 条安全上限")

        self._maybe_cleanup_orphan_results()
        self._maybe_cleanup_expired_idempotency()
        api_meta = registry.get_api(api_id)
        if not dry_run and api_meta and is_high_risk_api(api_id, api_meta) and not allow_high_risk:
            raise ValueError("高危批处理接口必须经过显式确认")

        cfg = load_config()
        if delay_sec is None:
            delay_sec = float(cfg.get("delay_sec", 0.05))
        from .config import validate_delay_sec

        delay_sec = validate_delay_sec(delay_sec)
        normalized_key = _normalize_idempotency_key(idempotency_key)
        items_json = self._serialize_task_items(items)
        request_hash = request_fingerprint or batch_request_fingerprint(
            api_id,
            items,
            delay_sec,
            dry_run,
            allow_high_risk,
        )
        task_id = uuid.uuid4().hex[:12]

        if normalized_key:
            created, existing = _reserve_idempotency(
                owner_id,
                issuer,
                tenant_id,
                normalized_key,
                request_hash,
                task_id,
                api_id,
                len(items),
                delay_sec,
                dry_run,
                items_json,
                str(cfg.get("active_env_id") or ""),
            )
            if not created and existing is not None:
                # Only reuse an object already owned by this runner.  Loading
                # a durable row here would turn a replay into a local worker
                # candidate before the caller has decided whether recovery of
                # a RESERVED task is allowed.
                local_task = self.tasks.get(str(existing["task_id"]))
                if local_task is not None:
                    local_task.idempotent_replay = True
                    return local_task
                record = self._store().get_batch_task(str(existing["task_id"]))
                if record is None:
                    record = {
                        "task_id": str(existing["task_id"]),
                        "api_id": str(existing["api_id"]),
                        "issuer": issuer or "",
                        "tenant_id": tenant_id or "",
                        "owner_subject": owner_id or "",
                        "status": str(existing.get("state") or "RESERVED"),
                        "total": int(existing["total"]),
                        "dry_run": int(bool(existing["dry_run"])),
                        "request_hash": existing["request_hash"],
                        "items_json": items_json,
                        "environment_id": str(cfg.get("active_env_id") or ""),
                        "created_at": float(existing["created_at"]),
                        "updated_at": time.time(),
                    }
                    self._store().create_or_repair_batch_task(record)
                task = self._hydrate_task(
                    record,
                    fallback_items=items,
                    idempotent_replay=True,
                    replay_only=True,
                )
                task.delay_sec = float(existing["delay_sec"])
                return task

        # 快照当前激活配置，确保任务执行期间不受全局环境切换影响，彻底做到凭证隔离
        config_snapshot = dict(cfg)
        task = BatchTask(
            task_id,
            api_id,
            items,
            delay_sec=delay_sec,
            dry_run=dry_run,
            config_snapshot=config_snapshot,
            owner_id=owner_id,
            issuer=issuer,
            tenant_id=tenant_id,
            database_path_override=IDEMPOTENCY_DB_PATH,
            environment_id=str(cfg.get("active_env_id") or ""),
        )

        task._finalize_callback = lambda task=task: self._finalize_task(task, None, "任务在启动前被取消")
        try:
            if not normalized_key:
                created = self._store().create_batch_task(
                    {
                        "task_id": task_id,
                        "issuer": issuer or "",
                        "tenant_id": tenant_id or "",
                        "owner_subject": owner_id or "",
                        "api_id": api_id,
                        "status": "RESERVED",
                        "total": len(items),
                        "dry_run": dry_run,
                        "request_hash": request_hash,
                        "items_json": items_json,
                        "environment_id": str(cfg.get("active_env_id") or ""),
                    }
                )
                if not created:
                    raise ValueError("批处理任务 ID 已存在，请重试")
            self._cache_task(task)
        except Exception:
            if normalized_key:
                with suppress(Exception):
                    _release_idempotency(
                        owner_id,
                        issuer,
                        tenant_id,
                        normalized_key,
                        task_id,
                    )
            with suppress(Exception):
                self._store().delete_batch_task(task_id, allow_active=True)
            raise
        return task

    def get_task(self, task_id: str) -> BatchTask | None:
        record = self._store().get_batch_task(task_id)
        if record is None:
            local = self.tasks.get(task_id)
            if local is not None and not getattr(local, "persistent", True):
                return local
            # SQLite is authoritative for persistent tasks. A local object can
            # outlive a deletion request in the same process, so evict it
            # before returning the externally visible 404.
            self.tasks.pop(task_id, None)
            return None
        local = self.tasks.get(task_id)
        if local is not None:
            return local
        task = self._hydrate_task(record)
        with suppress(ValueError):
            self._cache_task(task)
        return task

    def get_task_record(self, task_id: str) -> dict[str, Any] | None:
        return self._store().get_batch_task(task_id)

    def list_task_events(
        self,
        task_id: str,
        *,
        after_sequence: int = -1,
        limit: int = MAX_EVENTS_PER_TASK,
    ) -> list[tuple[int, dict[str, Any]]]:
        return self._store().list_task_events(
            task_id,
            after_sequence=after_sequence,
            limit=limit,
        )

    def control_task(self, task_id: str, action: str) -> dict[str, Any] | None:
        """Apply a durable control command and wake a local worker if present."""
        record = self._store().request_control(task_id, action)
        local = self.tasks.get(task_id)
        if local is None or record is None:
            return record
        expected_status = {
            "stop": "CANCELLED",
            "pause": "PAUSED",
            "resume": "RUNNING",
        }[action]
        # request_control can be a no-op for a terminal task. Do not mutate a
        # local cache unless the durable row actually reached the requested
        # state.
        if str(record.get("status")) == expected_status:
            with local._lock:
                if action == "stop":
                    local._stop_event.set()
                    local._pause_event.set()
                    local.status = "CANCELLED"
                elif action == "pause":
                    local._pause_event.clear()
                    local.status = "PAUSED"
                elif action == "resume":
                    local._pause_event.set()
                    local.status = "RUNNING"
        return record

    def start_task(self, task_id: str, *, recover_reserved: bool = False) -> bool:
        if self._store().is_batch_task_tombstoned(task_id):
            self.tasks.pop(task_id, None)
            return False
        task = self.tasks.get(task_id)
        if task is None and recover_reserved:
            task = self.get_task(task_id)
        if not task:
            if _idempotency_task_exists(task_id):
                # Legacy rows created before ``batch_tasks`` existed have no
                # input payload to recover.  They are safe to replay as an
                # already-reserved handle, but this path never executes them.
                return True
            raise ValueError(f"Task {task_id} not found")

        if task.replay_only:
            if task.status == "RESERVED" and recover_reserved:
                pass
            elif task.status in {
                "RESERVED",
                "RUNNING",
                "PAUSED",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
            }:
                return True
            else:
                return False
        if task.status in {"COMPLETED", "FAILED", "CANCELLED"}:
            return True
        with task._lock:
            if task._started or task._finalized:
                return False
            if task.status not in {"PENDING", "RESERVED"}:
                return False
            task._started = True
        if task._store is not None:
            claimed = task._store.claim_batch_task(
                task.task_id,
                self.worker_id,
                lease_seconds=BATCH_TASK_LEASE_SECONDS,
            )
            if not claimed:
                refreshed = task.refresh_from_store() or {}
                return str(refreshed.get("status")) in {
                    "RUNNING",
                    "PAUSED",
                    "COMPLETED",
                    "FAILED",
                    "CANCELLED",
                }
            task.worker_id = self.worker_id
            task._local_execution = True
            task.status = "RUNNING"
        threading.Thread(target=self._worker, args=(task,), daemon=True).start()
        return True

    def _finalize_task(self, task: BatchTask, api_meta: Any | None, error_message: str | None = None):
        if task._store is not None:
            with suppress(Exception):
                task.refresh_from_store()
        with task._lock:
            if task._finalized:
                return
            task._finalized = True
            finalization_time = task.end_time or time.time()
            if task.status == "CANCELLED":
                final_status = "CANCELLED"
            elif task.status in {"UNKNOWN", "EXPIRED"}:
                # UNKNOWN means an external request may have completed before
                # the worker lost its lease. It is a manual-reconciliation
                # state and must never be silently promoted to COMPLETED.
                task.status = "UNKNOWN"
                final_status = "UNKNOWN"
                error_message = error_message or (
                    "任务租约已过期，外部请求结果未知，需要人工核对后再决定是否重试"
                )
            elif error_message:
                task.status = "FAILED"
                final_status = "FAILED"
            elif task.status == "FAILED":
                final_status = "FAILED"
                error_message = "批处理任务执行失败"
            else:
                task.status = "COMPLETED"
                final_status = "COMPLETED"
            worker_id = task.worker_id
            current_index = task.current
            success_count = task.success
            failed_count = task.failed
            started_at = task.start_time
        result_path = task_results_path(task.task_id)
        persisted_result_path = result_path if os.path.isfile(result_path) else None
        skip_local_history = False
        if task._store is not None:
            terminal_written = False
            try:
                terminal_written = task._store.set_task_terminal(
                    task.task_id,
                    status=final_status,
                    worker_id=worker_id,
                    current_index=current_index,
                    success_count=success_count,
                    failed_count=failed_count,
                    error_message=error_message
                    if final_status == "FAILED"
                    else None,
                    result_location=persisted_result_path,
                    started_at=started_at,
                    finished_at=finalization_time,
                )
            except Exception as exc:
                print(f"[WARN] Failed to persist batch terminal state: {exc}")
            if not terminal_written:
                # A remote stop, lease expiry, or another terminal writer may
                # have won the race after the local refresh above. Re-read the
                # durable row and align the local event/history with it rather
                # than publishing a stale COMPLETED result.
                with suppress(Exception):
                    durable = task._store.get_batch_task(
                        task.task_id,
                        reconcile_expired=False,
                    )
                    if durable is not None:
                        durable_status = str(durable.get("status") or "")
                        if durable_status in BATCH_TERMINAL_STATUSES or durable_status == "UNKNOWN":
                            final_status = durable_status
                            with task._lock:
                                task.status = durable_status
                            if durable.get("finished_at") is not None:
                                finalization_time = float(durable["finished_at"])
                            current_index = int(
                                durable.get("current_index") or current_index
                            )
                            success_count = int(
                                durable.get("success_count") or success_count
                            )
                            failed_count = int(
                                durable.get("failed_count") or failed_count
                            )
                            if final_status in {"FAILED", "UNKNOWN"}:
                                error_message = error_message or (
                                    durable.get("error_message")
                                    or (
                                        "任务执行结果未知，需要人工核对"
                                        if final_status == "UNKNOWN"
                                        else "批处理任务执行失败"
                                    )
                                )
                        else:
                            # The row is still active but this worker no
                            # longer owns a valid terminal transition. Do not
                            # write a contradictory local history record.
                            skip_local_history = True
                            final_status = "UNKNOWN"
                            error_message = (
                                error_message
                                or "任务执行权已丢失，执行结果未知，需要人工核对"
                            )
                            with task._lock:
                                task.status = "UNKNOWN"
        if skip_local_history:
            with task._lock:
                task.end_time = finalization_time
            return

        end_time = finalization_time
        start_time = started_at or end_time
        summary = {
            "task_id": task.task_id,
            "api_id": task.api_id,
            "issuer": task.issuer,
            "tenant_id": task.tenant_id,
            "owner_id": task.owner_id,
            "api_name": api_meta.name if api_meta else task.api_id,
            "service_type": getattr(api_meta, "service_type", "im") if api_meta else "im",
            "total": task.total,
            "success": success_count,
            "failed": failed_count,
            "duration_sec": round(end_time - start_time, 2),
            "status": final_status,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)),
        }
        if (error_message and final_status != "CANCELLED") or final_status == "UNKNOWN":
            task.emit("error", {"message": error_message, "status": final_status})
        else:
            task.emit("complete", summary)
        with task._lock:
            task.end_time = finalization_time
        try:
            result_count = success_count + failed_count
            record = {**summary, "result_count": result_count}
            if persisted_result_path:
                record["results_path"] = persisted_result_path
            if result_count <= MAX_INLINE_HISTORY_RESULTS:
                record["results"] = task.results
            save_history_item(record)
        except Exception as exc:
            print(f"[WARN] Failed to save batch history: {exc}")

    def _worker(self, task: BatchTask):
        with task._lock:
            if task._finalized:
                return
            if task._stop_event.is_set() or task.status == "CANCELLED":
                cancelled_before_start = True
                api_meta = None
            else:
                cancelled_before_start = False
                task.status = "RUNNING"
                task.start_time = time.time()
                api_meta = registry.get_api(task.api_id)
                task._local_execution = True
                task.heartbeat()
        if cancelled_before_start:
            self._finalize_task(task, None)
            return
        if not api_meta:
            self._finalize_task(task, None, f"API {task.api_id} 未注册")
            return

        lease_thread = threading.Thread(
            target=self._lease_heartbeat_loop,
            args=(task,),
            name=f"batch-lease-{task.task_id}",
            daemon=True,
        )
        lease_thread.start()
        try:
            # 使用该任务创建时固化的独立配置快照实例化统一云客户端
            client = UniversalCloudClient(config=task.config_snapshot)
            task.emit("start", {
                "task_id": task.task_id,
                "api_id": task.api_id,
                "api_name": api_meta.name,
                "total": task.total,
            })

            for idx, row in enumerate(task.items, start=1):
                task.refresh_from_store()
                if task._stop_event.is_set():
                    break
                while not task._pause_event.wait(timeout=0.1):
                    task.refresh_from_store()
                    task.heartbeat()
                    if task._stop_event.is_set():
                        break
                if task._stop_event.is_set():
                    break
                task.current = idx
                task.heartbeat()

                try:
                    payload = api_meta.build_payload(row)
                    if task.dry_run:
                        action_status = "OK"
                        err_code = 0
                        err_info = "【预览演练】参数结构构建成功 (未发起真实网络请求)"
                        cost_ms = 0
                    else:
                        resp = self._invoke_with_rate_limit_retry(
                            client,
                            api_meta,
                            payload,
                            stop_event=task._stop_event,
                        )
                        action_status = resp.get("ActionStatus", "FAIL")
                        err_code = resp.get("ErrorCode", -1)
                        err_info = resp.get("ErrorInfo", "")
                        cost_ms = resp.get("_cost_ms", 0)
                except Exception as item_err:
                    action_status = "FAIL"
                    err_code = -999
                    err_info = f"参数转换或执行异常: {item_err!s}"
                    cost_ms = 0

                is_ok = action_status == "OK" and err_code == 0
                if is_ok:
                    task.success += 1
                else:
                    task.failed += 1

                result_item = {
                    **row,
                    "_status": action_status,
                    "_err_code": err_code,
                    "_err_info": err_info,
                    "_cost_ms": cost_ms,
                }
                task.results.append(result_item)
                append_task_result(task.task_id, result_item)
                persisted = True
                if persisted and len(task.results) > MAX_INLINE_HISTORY_RESULTS:
                    del task.results[:-MAX_INLINE_HISTORY_RESULTS]
                task.last_heartbeat = time.time()
                task._persist_progress()
                task.emit("progress", {
                    "index": idx,
                    "total": task.total,
                    "current": idx,
                    "success": task.success,
                    "failed": task.failed,
                    "percent": round(idx / task.total * 100, 1),
                    "is_ok": is_ok,
                    "err_code": err_code,
                    "err_info": err_info,
                    "cost_ms": cost_ms,
                    "item": result_item,
                })

                if task.delay_sec > 0 and task._stop_event.wait(task.delay_sec):
                    break
            task.refresh_from_store()
        except Exception as global_err:
            self._finalize_task(task, api_meta, f"批处理工作线程异常中断: {global_err!s}")
            return
        finally:
            task._lease_stop_event.set()
        self._finalize_task(task, api_meta)

    def _lease_heartbeat_loop(self, task: BatchTask) -> None:
        interval = max(1.0, BATCH_TASK_LEASE_SECONDS / 3)
        while not task._lease_stop_event.wait(interval):
            if task._stop_event.is_set():
                return
            if not task.heartbeat():
                # A different process may have cancelled or taken over the
                # lease.  The current worker must stop at the next safe
                # polling point instead of publishing a second terminal state.
                task.status = "UNKNOWN"
                task._stop_event.set()
                task._pause_event.set()
                return


cleanup_orphan_task_results()
task_runner = TaskRunner()
