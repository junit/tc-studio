"""统一的本地持久化入口。

当前实现提供 SQLite 适配器。业务模块只应依赖本模块的数据库连接和路径
解析，不应各自创建独立的 SQLite 文件。未来迁移 PostgreSQL 时，替换本模块
的适配器即可，业务层不需要感知连接驱动。
"""

from __future__ import annotations

import os
import json
import hashlib
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STORAGE_DIR = PROJECT_ROOT / "storage"
DEFAULT_DATABASE_PATH = DEFAULT_STORAGE_DIR / "tim.sqlite3"
DATABASE_URL_ENV = "TIM_DATABASE_URL"
SQLITE_PATH_ENV = "TIM_SQLITE_PATH"
SQLITE_BUSY_TIMEOUT_MS = 5_000
SQLITE_JOURNAL_MODE_RETRIES = 20
SQLITE_JOURNAL_MODE_RETRY_DELAY_SECONDS = 0.1
BATCH_TASK_LEASE_SECONDS = 60
SYNC_JOB_LEASE_SECONDS = 60
MAX_TASK_EVENTS = 2_000
BATCH_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
SYNC_TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "UNKNOWN"})

_initialization_lock = threading.Lock()
_initialized_paths: set[str] = set()


class PersistenceConfigurationError(ValueError):
    """持久化配置无法安全解析。"""


def database_path(database_url: str | None = None) -> str:
    """Resolve the current database URL to a local SQLite path.

    SQLite is the only adapter enabled in the current phase. ``sqlite:///`` URLs
    are accepted so the eventual PostgreSQL switch can keep the same setting
    name. Relative paths are resolved against the repository root.
    """
    raw = (
        database_url
        if database_url is not None
        else os.getenv(DATABASE_URL_ENV) or os.getenv(SQLITE_PATH_ENV)
    )
    if raw is None or not str(raw).strip():
        return str(DEFAULT_DATABASE_PATH)

    value = str(raw).strip()
    if value.startswith("sqlite:///"):
        value = value[len("sqlite:///") :]
    elif "://" in value:
        raise PersistenceConfigurationError(
            "当前阶段只支持 SQLite；多主机部署请切换 PostgreSQL 适配器"
        )

    path = Path(value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return str(path.resolve())


def _secure_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        # Windows and some mounted filesystems do not support chmod. The
        # deployment check must reject insecure mounts where mode enforcement
        # is required; this adapter remains portable for local development.
        pass


def _secure_file(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _connection(path: str) -> sqlite3.Connection:
    target = Path(path)
    _secure_directory(target.parent)
    connection = sqlite3.connect(
        str(target),
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000,
        isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    # WAL lets readers continue while a writer commits. It is appropriate for
    # the supported single-host local-disk deployment; PostgreSQL is required
    # when the database file must be shared across hosts.
    journal_mode_error: sqlite3.OperationalError | None = None
    for attempt in range(SQLITE_JOURNAL_MODE_RETRIES):
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            journal_mode_error = None
            break
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" not in message or attempt == SQLITE_JOURNAL_MODE_RETRIES - 1:
                raise
            journal_mode_error = exc
            time.sleep(SQLITE_JOURNAL_MODE_RETRY_DELAY_SECONDS)
    if journal_mode_error is not None:
        raise journal_mode_error
    # Task reservations, idempotency rows and audit facts must survive a
    # process or host power loss as far as SQLite can guarantee on the local
    # filesystem.  NORMAL is faster but can lose the newest WAL commits after
    # an OS crash, which can reopen a side-effecting idempotency key.
    connection.execute("PRAGMA synchronous = FULL")
    return connection


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    operation_kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    task_id TEXT NOT NULL,
    api_id TEXT NOT NULL DEFAULT '',
    total INTEGER NOT NULL DEFAULT 0,
    delay_sec REAL NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 0,
    state TEXT NOT NULL DEFAULT 'RESERVED',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    lease_until REAL,
    worker_id TEXT,
    last_error TEXT,
    result_location TEXT,
    PRIMARY KEY (
        issuer,
        tenant_id,
        owner_subject,
        operation_kind,
        idempotency_key
    )
);

CREATE INDEX IF NOT EXISTS idx_idempotency_created_at
    ON idempotency_keys(created_at);

CREATE INDEX IF NOT EXISTS idx_idempotency_task_id
    ON idempotency_keys(task_id);

CREATE TABLE IF NOT EXISTS batch_tasks (
    task_id TEXT PRIMARY KEY,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    api_id TEXT NOT NULL,
    status TEXT NOT NULL,
    current_index INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0,
    success_count INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    dry_run INTEGER NOT NULL DEFAULT 0,
    request_hash TEXT,
    items_json TEXT,
    environment_id TEXT,
    worker_id TEXT,
    lease_until REAL,
    last_heartbeat REAL,
    attempt INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    result_location TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batch_tasks_owner_created
    ON batch_tasks(owner_subject, created_at);

CREATE INDEX IF NOT EXISTS idx_batch_tasks_status_lease
    ON batch_tasks(status, lease_until);

CREATE INDEX IF NOT EXISTS idx_batch_tasks_updated_at
    ON batch_tasks(updated_at);

CREATE TABLE IF NOT EXISTS task_events (
    task_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, sequence),
    FOREIGN KEY (task_id) REFERENCES batch_tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS batch_task_items (
    task_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    status TEXT NOT NULL,
    error_code TEXT,
    result_sha256 TEXT NOT NULL,
    byte_offset INTEGER NOT NULL,
    byte_length INTEGER NOT NULL,
    result_uri TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (task_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_batch_task_items_status
    ON batch_task_items(task_id, status, sequence);

CREATE TABLE IF NOT EXISTS sync_jobs (
    task_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    idempotency_key TEXT,
    worker_id TEXT,
    lease_until REAL,
    last_heartbeat REAL,
    attempt INTEGER NOT NULL DEFAULT 0,
    progress_json TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    error_message TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_kind_status
    ON sync_jobs(kind, status, created_at);

CREATE INDEX IF NOT EXISTS idx_sync_jobs_idempotency
    ON sync_jobs(kind, owner_subject, idempotency_key);

CREATE TABLE IF NOT EXISTS batch_task_tombstones (
    task_id TEXT PRIMARY KEY,
    deleted_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    setting_key TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    value_json TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (setting_key, issuer, tenant_id)
);

CREATE INDEX IF NOT EXISTS idx_app_settings_scope
    ON app_settings(issuer, tenant_id, updated_at);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id TEXT PRIMARY KEY,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    actor_subject TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT,
    outcome TEXT NOT NULL,
    metadata_json TEXT,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_scope_created
    ON audit_events(tenant_id, actor_subject, created_at);

CREATE TABLE IF NOT EXISTS orchestration_events (
    event_id TEXT PRIMARY KEY,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    caller_bot_id TEXT NOT NULL DEFAULT '',
    target_bot_id TEXT NOT NULL DEFAULT '',
    record_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orchestration_events_scope_created
    ON orchestration_events(issuer, tenant_id, owner_subject, created_at);

CREATE INDEX IF NOT EXISTS idx_orchestration_events_edge
    ON orchestration_events(caller_bot_id, target_bot_id, created_at);

CREATE TABLE IF NOT EXISTS bots (
    bot_id TEXT PRIMARY KEY,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT,
    visibility TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_memories (
    bot_id TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    memory_key TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (
        bot_id, issuer, tenant_id, owner_subject, memory_key
    ),
    FOREIGN KEY (bot_id) REFERENCES bots(bot_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bot_session_summaries (
    bot_id TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    summary_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (bot_id, issuer, tenant_id, owner_subject),
    FOREIGN KEY (bot_id) REFERENCES bots(bot_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bot_skills (
    skill_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    version TEXT,
    definition_json TEXT NOT NULL,
    code_uri TEXT,
    code_sha256 TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (bot_id, issuer, tenant_id, owner_subject, name),
    FOREIGN KEY (bot_id) REFERENCES bots(bot_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversation_sessions (
    session_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (bot_id) REFERENCES bots(bot_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversation_messages (
    message_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, sequence),
    FOREIGN KEY (session_id) REFERENCES conversation_sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bot_memories_scope
    ON bot_memories(bot_id, issuer, tenant_id, owner_subject, updated_at);

CREATE INDEX IF NOT EXISTS idx_bot_skills_scope
    ON bot_skills(bot_id, issuer, tenant_id, owner_subject, updated_at);

CREATE INDEX IF NOT EXISTS idx_conversation_messages_session
    ON conversation_messages(session_id, sequence);

CREATE TABLE IF NOT EXISTS bot_files (
    file_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    blob_uri TEXT NOT NULL,
    original_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    content_type TEXT,
    size_bytes INTEGER NOT NULL DEFAULT 0,
    sha256 TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (bot_id) REFERENCES bots(bot_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bot_file_reservations (
    reservation_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (bot_id) REFERENCES bots(bot_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_bot_file_reservations_scope
    ON bot_file_reservations(bot_id, issuer, tenant_id, owner_subject, created_at);

CREATE TABLE IF NOT EXISTS rag_documents (
    document_id TEXT PRIMARY KEY,
    bot_id TEXT NOT NULL,
    issuer TEXT NOT NULL DEFAULT '',
    tenant_id TEXT NOT NULL DEFAULT '',
    owner_subject TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    blob_uri TEXT,
    status TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (bot_id) REFERENCES bots(bot_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS rag_chunks (
    document_id TEXT NOT NULL,
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (document_id, chunk_index),
    FOREIGN KEY (document_id) REFERENCES rag_documents(document_id) ON DELETE CASCADE
);
"""


def _ensure_schema_columns(connection: sqlite3.Connection) -> None:
    """Add columns introduced after the first local SQLite release.

    ``CREATE TABLE IF NOT EXISTS`` does not evolve an existing database.  The
    local deployment must therefore perform small, idempotent migrations before
    the application starts using the new task recovery fields.
    """
    required_columns = {
        "idempotency_keys": {
            "last_heartbeat": "REAL",
        },
        "batch_tasks": {
            "items_json": "TEXT",
            "environment_id": "TEXT",
            "last_heartbeat": "REAL",
            "attempt": "INTEGER NOT NULL DEFAULT 0",
        },
        "sync_jobs": {
            "worker_id": "TEXT",
            "lease_until": "REAL",
            "last_heartbeat": "REAL",
            "attempt": "INTEGER NOT NULL DEFAULT 0",
            "progress_json": "TEXT",
            "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        },
        "bots": {
            "issuer": "TEXT NOT NULL DEFAULT ''",
            "tenant_id": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table_name, columns in required_columns.items():
        existing = {
            str(row[1])
            for row in connection.execute(f"PRAGMA table_info({table_name})")
        }
        for column_name, column_definition in columns.items():
            if column_name not in existing:
                try:
                    connection.execute(
                        f"ALTER TABLE {table_name} ADD COLUMN "
                        f"{column_name} {column_definition}"
                    )
                except sqlite3.OperationalError as exc:
                    # Two independent processes may observe the same missing
                    # column before either ALTER TABLE commits.  The process
                    # that loses that race can safely continue when SQLite
                    # reports that the column now exists.
                    if "duplicate column name" not in str(exc).lower():
                        raise


def _json_load_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_load_value(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


_AUDIT_SENSITIVE_KEY_PARTS = (
    "secret",
    "password",
    "api_key",
    "usersig",
    "authorization",
    "private_key",
    "access_key",
)
_AUDIT_SENSITIVE_TOKEN_KEYS = frozenset(
    {
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "bearer_token",
    }
)


def _sanitize_audit_metadata(value: Any, *, depth: int = 0) -> Any:
    """Keep audit metadata useful without turning it into a secret sink."""
    if depth > 3:
        return "[truncated]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in list(value.items())[:50]:
            key = str(raw_key)[:128]
            lowered = key.lower().replace("-", "_")
            if (
                any(part in lowered for part in _AUDIT_SENSITIVE_KEY_PARTS)
                or lowered in _AUDIT_SENSITIVE_TOKEN_KEYS
                or lowered.endswith("_token")
            ):
                result[key] = "[redacted]"
            else:
                result[key] = _sanitize_audit_metadata(raw_value, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple, set)):
        return [
            _sanitize_audit_metadata(item, depth=depth + 1)
            for item in list(value)[:50]
        ]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:512]


class SqliteAppSettingsStore:
    """事务化的小型应用设置存储。

    这里保存的是非机密、需要被多个进程共同看到的配置事实。密钥、Token
    和私钥仍然必须留在环境变量、受限文件或外部 Secret Store 中。
    """

    def __init__(self, path: str | None = None):
        self.path = database_path(path)

    @staticmethod
    def _scope(
        issuer: str | None,
        tenant_id: str | None,
    ) -> tuple[str, str]:
        return str(issuer or ""), str(tenant_id or "")

    def get(
        self,
        setting_key: str,
        *,
        issuer: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any] | None:
        issuer_value, tenant_value = self._scope(issuer, tenant_id)
        connection = open_database(self.path)
        try:
            row = connection.execute(
                """
                SELECT setting_key, issuer, tenant_id, value_json, version,
                       updated_at, updated_by
                FROM app_settings
                WHERE setting_key = ? AND issuer = ? AND tenant_id = ?
                """,
                (str(setting_key), issuer_value, tenant_value),
            ).fetchone()
            if row is None:
                return None
            return {
                "setting_key": str(row["setting_key"]),
                "issuer": str(row["issuer"] or ""),
                "tenant_id": str(row["tenant_id"] or ""),
                "value": _json_load_value(row["value_json"]),
                "version": int(row["version"] or 1),
                "updated_at": float(row["updated_at"]),
                "updated_by": str(row["updated_by"] or ""),
            }
        finally:
            connection.close()

    def set(
        self,
        setting_key: str,
        value: Any,
        *,
        issuer: str | None = None,
        tenant_id: str | None = None,
        updated_by: str | None = None,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        issuer_value, tenant_value = self._scope(issuer, tenant_id)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("应用设置不能超过 256 KiB")
        now = time.time()
        connection = open_database(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT version
                FROM app_settings
                WHERE setting_key = ? AND issuer = ? AND tenant_id = ?
                """,
                (str(setting_key), issuer_value, tenant_value),
            ).fetchone()
            current_version = int(existing["version"]) if existing is not None else 0
            if expected_version is not None and current_version != int(expected_version):
                raise ValueError(
                    f"应用设置版本冲突: expected={expected_version}, actual={current_version}"
                )
            next_version = current_version + 1
            connection.execute(
                """
                INSERT INTO app_settings (
                    setting_key, issuer, tenant_id, value_json, version,
                    updated_at, updated_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(setting_key, issuer, tenant_id)
                DO UPDATE SET
                    value_json = excluded.value_json,
                    version = excluded.version,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by
                """,
                (
                    str(setting_key),
                    issuer_value,
                    tenant_value,
                    encoded,
                    next_version,
                    now,
                    str(updated_by or ""),
                ),
            )
            connection.commit()
            return {
                "setting_key": str(setting_key),
                "issuer": issuer_value,
                "tenant_id": tenant_value,
                "value": value,
                "version": next_version,
                "updated_at": now,
                "updated_by": str(updated_by or ""),
            }
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()


class SqliteAuditStore:
    """Append-only audit event adapter shared by API and worker boundaries."""

    def __init__(self, path: str | None = None):
        self.path = database_path(path)

    def append(
        self,
        *,
        action: str,
        resource_type: str,
        resource_id: str | None = None,
        outcome: str = "SUCCESS",
        actor_subject: str | None = None,
        issuer: str | None = None,
        tenant_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        event_id = uuid.uuid4().hex
        payload = _sanitize_audit_metadata(metadata or {})
        metadata_json = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        connection = open_database(self.path)
        try:
            connection.execute(
                """
                INSERT INTO audit_events (
                    event_id, issuer, tenant_id, actor_subject, action,
                    resource_type, resource_id, outcome, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    str(issuer or ""),
                    str(tenant_id or ""),
                    str(actor_subject or ""),
                    str(action)[:128],
                    str(resource_type)[:128],
                    None if resource_id is None else str(resource_id)[:256],
                    str(outcome)[:64],
                    metadata_json[:16_384],
                    time.time(),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return event_id

    def list_recent(
        self,
        *,
        issuer: str | None = None,
        tenant_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        connection = open_database(self.path)
        try:
            rows = connection.execute(
                """
                SELECT * FROM audit_events
                WHERE issuer = ? AND tenant_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (
                    str(issuer or ""),
                    str(tenant_id or ""),
                    max(1, min(int(limit), 1000)),
                ),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()


class SqliteOrchestrationStore:
    """SQLite-first store for multi-agent orchestration history.

    The legacy ``orchestrations.json`` file is accepted as a one-time import
    source. New events are inserted transactionally and retained as the latest
    bounded window, so two local processes cannot lose each other's updates.
    """

    MAX_EVENTS = 500
    MAX_RECORD_BYTES = 64 * 1024

    def __init__(self, path: str | None = None):
        self.path = database_path(path)

    @classmethod
    def _clean_record(
        cls,
        record: Mapping[str, Any],
        *,
        owner_subject: str,
    ) -> tuple[dict[str, Any], str]:
        cleaned = _sanitize_audit_metadata(dict(record))
        if not isinstance(cleaned, dict):
            cleaned = {}
        owner = str(owner_subject or cleaned.get("owner_id") or "")
        if owner and not cleaned.get("owner_id"):
            cleaned["owner_id"] = owner
        encoded = json.dumps(
            cleaned,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        if len(encoded.encode("utf-8")) > cls.MAX_RECORD_BYTES:
            cleaned = {
                "caller_bot_id": str(cleaned.get("caller_bot_id") or ""),
                "caller_bot_name": str(cleaned.get("caller_bot_name") or ""),
                "target_bot_id": str(cleaned.get("target_bot_id") or ""),
                "target_bot_name": str(cleaned.get("target_bot_name") or ""),
                "task": str(cleaned.get("task") or "")[:512],
                "status": str(cleaned.get("status") or "")[:64],
                "result_preview": str(cleaned.get("result_preview") or "")[:512],
                "elapsed_seconds": cleaned.get("elapsed_seconds", 0),
                "timestamp": str(cleaned.get("timestamp") or "")[:64],
                "owner_id": owner,
                "_truncated": True,
            }
        return cleaned, owner

    @staticmethod
    def _decode_record(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        try:
            parsed = json.loads(str(value or ""))
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _insert_unlocked(
        self,
        connection: sqlite3.Connection,
        record: Mapping[str, Any],
        *,
        issuer: str,
        tenant_id: str,
        owner_subject: str,
        created_at: float | None = None,
    ) -> str:
        cleaned, owner = self._clean_record(
            record,
            owner_subject=owner_subject,
        )
        event_id = uuid.uuid4().hex
        encoded = json.dumps(
            cleaned,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        caller_bot_id = str(cleaned.get("caller_bot_id") or "")[:256]
        target_bot_id = str(cleaned.get("target_bot_id") or "")[:256]
        connection.execute(
            """
            INSERT INTO orchestration_events (
                event_id, issuer, tenant_id, owner_subject,
                caller_bot_id, target_bot_id, record_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                str(issuer or ""),
                str(tenant_id or ""),
                owner,
                caller_bot_id,
                target_bot_id,
                encoded,
                float(created_at if created_at is not None else time.time()),
            ),
        )
        return event_id

    def _prune_unlocked(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            DELETE FROM orchestration_events
            WHERE rowid NOT IN (
                SELECT rowid
                FROM orchestration_events
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
            )
            """,
            (self.MAX_EVENTS,),
        )

    def append(
        self,
        record: Mapping[str, Any],
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> str:
        connection = open_database(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            event_id = self._insert_unlocked(
                connection,
                record,
                issuer=issuer,
                tenant_id=tenant_id,
                owner_subject=owner_subject,
            )
            self._prune_unlocked(connection)
            connection.commit()
            return event_id
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def import_legacy_file(self, path: str) -> int:
        """Import one legacy JSON snapshot only when the table is still empty."""
        legacy_path = Path(path)
        if not legacy_path.is_file():
            return 0
        connection = open_database(self.path)
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT COUNT(*) FROM orchestration_events"
            ).fetchone()
            if existing is not None and int(existing[0] or 0) > 0:
                connection.commit()
                return 0
            try:
                with legacy_path.open("r", encoding="utf-8") as stream:
                    loaded = json.load(stream)
            except (OSError, TypeError, ValueError):
                connection.rollback()
                return 0
            records = [
                item for item in loaded
                if isinstance(item, Mapping)
            ] if isinstance(loaded, list) else []
            for record in reversed(records[-self.MAX_EVENTS:]):
                self._insert_unlocked(
                    connection,
                    record,
                    issuer=str(record.get("issuer") or ""),
                    tenant_id=str(record.get("tenant_id") or ""),
                    owner_subject=str(record.get("owner_id") or ""),
                )
            self._prune_unlocked(connection)
            connection.commit()
            return len(records)
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def list_recent(
        self,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str | None = None,
        is_admin: bool = False,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        if not is_admin and not owner_subject:
            return []
        connection = open_database(self.path)
        try:
            bounded_limit = max(1, min(int(limit), self.MAX_EVENTS))
            if is_admin:
                rows = connection.execute(
                    """
                    SELECT record_json
                    FROM orchestration_events
                    WHERE issuer = ? AND tenant_id = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (str(issuer or ""), str(tenant_id or ""), bounded_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT record_json
                    FROM orchestration_events
                    WHERE issuer = ? AND tenant_id = ?
                      AND owner_subject = ?
                    ORDER BY created_at DESC, rowid DESC
                    LIMIT ?
                    """,
                    (
                        str(issuer or ""),
                        str(tenant_id or ""),
                        str(owner_subject or ""),
                        bounded_limit,
                    ),
                ).fetchall()
            return [self._decode_record(row["record_json"]) for row in rows]
        finally:
            connection.close()


class SqliteMetadataMigrationStore:
    """Additive mirror used by the JSON-to-SQLite migration command.

    Runtime BOT and RAG reads remain file-backed until the migration verifier
    reports a clean dual-read comparison.  Keeping this adapter separate makes
    the cutover reversible and prevents a partial import from changing user
    behavior.
    """

    def __init__(self, path: str | None = None):
        self.path = database_path(path)

    def upsert_bot(self, record: Mapping[str, Any]) -> None:
        connection = open_database(self.path)
        try:
            now = str(record.get("updated_at") or datetime.now(timezone.utc).isoformat())
            created = str(record.get("created_at") or now)
            definition = _sanitize_audit_metadata(record.get("definition") or {})
            connection.execute(
                """
                INSERT INTO bots (
                    bot_id, issuer, tenant_id, owner_subject, visibility,
                    definition_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    issuer = excluded.issuer,
                    tenant_id = excluded.tenant_id,
                    owner_subject = excluded.owner_subject,
                    visibility = excluded.visibility,
                    definition_json = excluded.definition_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(record["bot_id"]),
                    str(record.get("issuer") or ""),
                    str(record.get("tenant_id") or ""),
                    record.get("owner_subject"),
                    str(record.get("visibility") or "private"),
                    json.dumps(
                        definition,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    created,
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def replace_rag_document(
        self,
        *,
        bot_id: str,
        document: Mapping[str, Any],
        chunks: list[Mapping[str, Any]],
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
        blob_uri: str | None = None,
    ) -> int:
        connection = open_database(self.path)
        try:
            now = str(
                document.get("updated_at")
                or document.get("created_at")
                or datetime.now(timezone.utc).isoformat()
            )
            created = str(document.get("created_at") or now)
            document_id = str(document["id"])
            connection.execute(
                """
                INSERT INTO bots (
                    bot_id, issuer, tenant_id, owner_subject, visibility,
                    definition_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id) DO NOTHING
                """,
                (
                    bot_id,
                    issuer,
                    tenant_id,
                    owner_subject or None,
                    "private",
                    "{}",
                    created,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO rag_documents (
                    document_id, bot_id, issuer, tenant_id, owner_subject,
                    name, blob_uri, status, version, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    bot_id = excluded.bot_id,
                    issuer = excluded.issuer,
                    tenant_id = excluded.tenant_id,
                    owner_subject = excluded.owner_subject,
                    name = excluded.name,
                    blob_uri = excluded.blob_uri,
                    status = excluded.status,
                    version = excluded.version,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    document_id,
                    bot_id,
                    issuer,
                    tenant_id,
                    owner_subject,
                    str(document.get("name") or document_id),
                    blob_uri,
                    "ENABLED" if document.get("enabled", True) else "DISABLED",
                    int(document.get("version") or 1),
                    json.dumps(
                        dict(document),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    created,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM rag_chunks WHERE document_id = ?",
                (document_id,),
            )
            used_chunk_indexes: set[int] = set()
            next_chunk_index = 0
            index_collisions = 0
            for index, chunk in enumerate(chunks):
                raw_chunk_index = chunk.get("index", index)
                try:
                    requested_chunk_index = int(raw_chunk_index)
                except (TypeError, ValueError):
                    requested_chunk_index = index
                chunk_index = requested_chunk_index
                if chunk_index < 0 or chunk_index in used_chunk_indexes:
                    index_collisions += 1
                    chunk_index = max(next_chunk_index, 0)
                    while chunk_index in used_chunk_indexes:
                        chunk_index += 1
                used_chunk_indexes.add(chunk_index)
                next_chunk_index = max(next_chunk_index, chunk_index + 1)
                metadata = {
                    key: value
                    for key, value in chunk.items()
                    if key not in {"content", "index"}
                }
                if chunk_index != requested_chunk_index:
                    metadata["source_chunk_index"] = raw_chunk_index
                connection.execute(
                    """
                    INSERT INTO rag_chunks (
                        document_id, chunk_index, content, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        chunk_index,
                        str(chunk.get("content") or ""),
                        json.dumps(
                            metadata,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ),
                        now,
                    ),
                )
            connection.commit()
            return index_collisions
        finally:
            connection.close()

    def upsert_memory(
        self,
        *,
        bot_id: str,
        memory: Mapping[str, Any],
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> None:
        connection = open_database(self.path)
        try:
            now = str(
                memory.get("updated_at")
                or memory.get("created_at")
                or datetime.now(timezone.utc).isoformat()
            )
            connection.execute(
                """
                INSERT INTO bot_memories (
                    bot_id, issuer, tenant_id, owner_subject, memory_key,
                    content, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, issuer, tenant_id, owner_subject, memory_key)
                DO UPDATE SET
                    content = excluded.content,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    bot_id,
                    issuer,
                    tenant_id,
                    owner_subject,
                    str(memory.get("key") or memory.get("name") or ""),
                    str(memory.get("content") or ""),
                    json.dumps(
                        dict(memory),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    str(memory.get("created_at") or now),
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def upsert_summary(
        self,
        *,
        bot_id: str,
        summary: Mapping[str, Any],
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> None:
        connection = open_database(self.path)
        try:
            now = str(
                summary.get("updated_at")
                or summary.get("created_at")
                or datetime.now(timezone.utc).isoformat()
            )
            connection.execute(
                """
                INSERT INTO bot_session_summaries (
                    bot_id, issuer, tenant_id, owner_subject,
                    summary_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, issuer, tenant_id, owner_subject)
                DO UPDATE SET
                    summary_json = excluded.summary_json,
                    updated_at = excluded.updated_at
                """,
                (
                    bot_id,
                    issuer,
                    tenant_id,
                    owner_subject,
                    json.dumps(
                        dict(summary),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def upsert_skill(
        self,
        *,
        bot_id: str,
        skill: Mapping[str, Any],
        code_uri: str | None = None,
        code_sha256: str | None = None,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> None:
        connection = open_database(self.path)
        try:
            now = str(
                skill.get("updated_at")
                or skill.get("created_at")
                or datetime.now(timezone.utc).isoformat()
            )
            skill_id = hashlib.sha256(
                f"{bot_id}\0{issuer}\0{tenant_id}\0{owner_subject}\0"
                f"{skill.get('name') or ''}".encode("utf-8")
            ).hexdigest()
            connection.execute(
                """
                INSERT INTO bot_skills (
                    skill_id, bot_id, issuer, tenant_id, owner_subject,
                    name, version, definition_json, code_uri, code_sha256,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, issuer, tenant_id, owner_subject, name)
                DO UPDATE SET
                    version = excluded.version,
                    definition_json = excluded.definition_json,
                    code_uri = excluded.code_uri,
                    code_sha256 = excluded.code_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    skill_id,
                    bot_id,
                    issuer,
                    tenant_id,
                    owner_subject,
                    str(skill.get("name") or ""),
                    str(skill.get("version") or ""),
                    json.dumps(
                        dict(skill),
                        ensure_ascii=False,
                        separators=(",", ":"),
                        default=str,
                    ),
                    code_uri,
                    code_sha256,
                    str(skill.get("created_at") or now),
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def upsert_file(self, record: Mapping[str, Any]) -> None:
        """Import a local Blob reference without reading the Blob into SQLite."""
        SqliteBotMetadataStore(self.path).upsert_file(record)

    def replace_conversation(
        self,
        *,
        session_id: str,
        bot_id: str,
        messages: list[Mapping[str, Any]],
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> None:
        connection = open_database(self.path)
        try:
            now = datetime.now(timezone.utc).isoformat()
            connection.execute(
                """
                INSERT INTO conversation_sessions (
                    session_id, bot_id, issuer, tenant_id, owner_subject,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    bot_id,
                    issuer,
                    tenant_id,
                    owner_subject,
                    now,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM conversation_messages WHERE session_id = ?",
                (session_id,),
            )
            for sequence, message in enumerate(messages):
                message_id = str(
                    message.get("id")
                    or hashlib.sha256(
                        f"{session_id}:{sequence}".encode("utf-8")
                    ).hexdigest()[:32]
                )
                created_at = str(message.get("timestamp") or now)
                metadata = {
                    key: value
                    for key, value in message.items()
                    if key not in {"id", "role", "content", "timestamp"}
                }
                connection.execute(
                    """
                    INSERT INTO conversation_messages (
                        message_id, session_id, sequence, role, content,
                        metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        session_id,
                        sequence,
                        str(message.get("role") or "assistant"),
                        str(message.get("content") or ""),
                        json.dumps(
                            metadata,
                            ensure_ascii=False,
                            separators=(",", ":"),
                            default=str,
                        ),
                        created_at,
                    ),
                )
            connection.commit()
        finally:
            connection.close()

    def counts(self) -> dict[str, int]:
        connection = open_database(self.path)
        try:
            return {
                "bots": int(connection.execute("SELECT COUNT(*) FROM bots").fetchone()[0]),
                "rag_documents": int(
                    connection.execute("SELECT COUNT(*) FROM rag_documents").fetchone()[0]
                ),
                "rag_chunks": int(
                    connection.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0]
                ),
                "bot_memories": int(
                    connection.execute("SELECT COUNT(*) FROM bot_memories").fetchone()[0]
                ),
                "bot_skills": int(
                    connection.execute("SELECT COUNT(*) FROM bot_skills").fetchone()[0]
                ),
                "bot_files": int(
                    connection.execute("SELECT COUNT(*) FROM bot_files").fetchone()[0]
                ),
                "conversation_messages": int(
                    connection.execute(
                        "SELECT COUNT(*) FROM conversation_messages"
                    ).fetchone()[0]
                ),
            }
        finally:
            connection.close()

    def ids(self) -> dict[str, set[str]]:
        connection = open_database(self.path)
        try:
            return {
                "bots": {
                    str(row[0])
                    for row in connection.execute("SELECT bot_id FROM bots")
                },
                "rag_documents": {
                    str(row[0])
                    for row in connection.execute("SELECT document_id FROM rag_documents")
                },
            }
        finally:
            connection.close()


class SqliteBotMetadataStore:
    """SQLite-first runtime adapter for BOT, conversation and RAG metadata.

    The adapter deliberately keeps large bodies in the local file layer:
    conversation metadata, RAG chunks, ownership facts, status fields and
    integrity references live in SQLite, while uploaded documents and skill
    source code remain addressed through ``blob_uri``/``code_uri``.
    """

    def __init__(self, path: str | None = None):
        self.path = database_path(path)

    def _open(self) -> sqlite3.Connection:
        return open_database(self.path)

    @staticmethod
    def _scope(
        issuer: str | None = None,
        tenant_id: str | None = None,
        owner_subject: str | None = None,
    ) -> tuple[str, str, str]:
        return (
            str(issuer or ""),
            str(tenant_id or ""),
            str(owner_subject or ""),
        )

    @staticmethod
    def session_id(
        bot_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> str:
        identity = "\0".join(
            (str(bot_id), str(issuer), str(tenant_id), str(owner_subject))
        )
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()

    @staticmethod
    def _encode(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    @staticmethod
    def _decode_message(row: sqlite3.Row) -> dict[str, Any]:
        message = {
            "id": str(row["message_id"]),
            "role": str(row["role"]),
            "content": str(row["content"]),
            "timestamp": str(row["created_at"]),
        }
        metadata = _json_load_object(row["metadata_json"])
        message["metadata"] = metadata
        return message

    @staticmethod
    def _decode_document(
        row: sqlite3.Row,
        *,
        chunks_count: int | None = None,
    ) -> dict[str, Any]:
        metadata = _json_load_object(row["metadata_json"])
        document = dict(metadata)
        document["id"] = str(row["document_id"])
        document["name"] = str(row["name"])
        document["enabled"] = str(row["status"]) == "ENABLED"
        document["version"] = int(row["version"] or 1)
        document["created_at"] = str(row["created_at"])
        document["updated_at"] = str(row["updated_at"])
        if row["blob_uri"]:
            document["blob_uri"] = str(row["blob_uri"])
        if chunks_count is not None:
            document["chunks_count"] = int(chunks_count)
        return document

    @staticmethod
    def _decode_chunk(row: sqlite3.Row) -> dict[str, Any]:
        metadata = _json_load_object(row["metadata_json"])
        tokens = metadata.get("tokens")
        if not isinstance(tokens, list):
            tokens = []
        content = str(row["content"])
        title = str(metadata.get("title") or row["name"])
        return {
            "chunk_id": str(metadata.get("chunk_id") or f"{row['document_id']}_c{row['chunk_index']}"),
            "doc_id": str(row["document_id"]),
            "doc_name": str(metadata.get("doc_name") or row["name"]),
            "index": int(row["chunk_index"]),
            "title": title,
            "content": content,
            "tokens": tokens,
            "token_count": int(metadata.get("token_count") or len(tokens)),
        }

    # ------------------------------------------------------------------
    # BOT definitions
    # ------------------------------------------------------------------
    def get_bot(self, bot_id: str) -> dict[str, Any] | None:
        connection = self._open()
        try:
            row = connection.execute(
                "SELECT * FROM bots WHERE bot_id = ?",
                (str(bot_id),),
            ).fetchone()
            if row is None:
                return None
            definition = _json_load_object(row["definition_json"])
            definition["id"] = str(row["bot_id"])
            definition["visibility"] = str(row["visibility"])
            definition["owner_id"] = row["owner_subject"] or None
            definition["issuer"] = str(row["issuer"] or "")
            definition["tenant_id"] = str(row["tenant_id"] or "")
            definition["created_at"] = str(row["created_at"])
            definition["updated_at"] = str(row["updated_at"])
            return definition
        finally:
            connection.close()

    def list_bots(self) -> list[dict[str, Any]]:
        connection = self._open()
        try:
            rows = connection.execute(
                "SELECT * FROM bots ORDER BY updated_at DESC, bot_id ASC"
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                definition = _json_load_object(row["definition_json"])
                definition["id"] = str(row["bot_id"])
                definition["visibility"] = str(row["visibility"])
                definition["owner_id"] = row["owner_subject"] or None
                definition["issuer"] = str(row["issuer"] or "")
                definition["tenant_id"] = str(row["tenant_id"] or "")
                definition["created_at"] = str(row["created_at"])
                definition["updated_at"] = str(row["updated_at"])
                result.append(definition)
            return result
        finally:
            connection.close()

    def upsert_bot(self, record: Mapping[str, Any]) -> None:
        now = str(record.get("updated_at") or datetime.now(timezone.utc).isoformat())
        created = str(record.get("created_at") or now)
        definition = _sanitize_audit_metadata(record.get("definition") or {})
        owner_subject = record.get("owner_subject")
        connection = self._open()
        try:
            connection.execute(
                """
                INSERT INTO bots (
                    bot_id, issuer, tenant_id, owner_subject, visibility,
                    definition_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id) DO UPDATE SET
                    issuer = excluded.issuer,
                    tenant_id = excluded.tenant_id,
                    owner_subject = excluded.owner_subject,
                    visibility = excluded.visibility,
                    definition_json = excluded.definition_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(record["bot_id"]),
                    str(record.get("issuer") or ""),
                    str(record.get("tenant_id") or ""),
                    owner_subject,
                    str(record.get("visibility") or "private"),
                    self._encode(definition),
                    created,
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def insert_bot(self, record: Mapping[str, Any]) -> bool:
        """Insert a new BOT without allowing a concurrent creator to overwrite it."""
        now = str(record.get("updated_at") or datetime.now(timezone.utc).isoformat())
        created = str(record.get("created_at") or now)
        definition = _sanitize_audit_metadata(record.get("definition") or {})
        owner_subject = record.get("owner_subject")
        connection = self._open()
        try:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO bots (
                    bot_id, issuer, tenant_id, owner_subject, visibility,
                    definition_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(record["bot_id"]),
                    str(record.get("issuer") or ""),
                    str(record.get("tenant_id") or ""),
                    owner_subject,
                    str(record.get("visibility") or "private"),
                    self._encode(definition),
                    created,
                    now,
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def delete_bot(self, bot_id: str) -> bool:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM bots WHERE bot_id = ?",
                (str(bot_id),),
            )
            connection.commit()
            return cursor.rowcount > 0
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Long-term memory and running summaries
    # ------------------------------------------------------------------
    def list_memories(
        self,
        bot_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> list[dict[str, Any]]:
        connection = self._open()
        try:
            rows = connection.execute(
                """
                SELECT memory_key, content, metadata_json, created_at, updated_at
                FROM bot_memories
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ?
                ORDER BY updated_at DESC, memory_key ASC
                """,
                (str(bot_id), issuer, tenant_id, owner_subject),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                metadata = _json_load_object(row["metadata_json"])
                if metadata.get("_deleted") is True:
                    continue
                metadata["key"] = str(row["memory_key"])
                metadata["content"] = str(row["content"])
                metadata["created_at"] = str(row["created_at"])
                metadata["updated_at"] = str(row["updated_at"])
                result.append(metadata)
            return result
        finally:
            connection.close()

    def has_memory_scope(
        self,
        bot_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> bool:
        """Return whether SQLite has ever owned this scope.

        A deleted memory is represented by a tombstone.  Callers need this
        distinction so an old JSON snapshot is not imported again merely
        because the visible memory list is empty.
        """
        connection = self._open()
        try:
            row = connection.execute(
                """
                SELECT 1
                FROM bot_memories
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ?
                LIMIT 1
                """,
                (str(bot_id), issuer, tenant_id, owner_subject),
            ).fetchone()
            return row is not None
        finally:
            connection.close()

    def upsert_memory(
        self,
        *,
        bot_id: str,
        memory: Mapping[str, Any],
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> None:
        now = str(
            memory.get("updated_at")
            or memory.get("created_at")
            or datetime.now(timezone.utc).isoformat()
        )
        key = str(memory.get("key") or memory.get("name") or "")
        if not key:
            raise ValueError("memory key cannot be empty")
        content = str(memory.get("content") or "")
        connection = self._open()
        try:
            connection.execute(
                """
                INSERT INTO bot_memories (
                    bot_id, issuer, tenant_id, owner_subject, memory_key,
                    content, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, issuer, tenant_id, owner_subject, memory_key)
                DO UPDATE SET
                    content = excluded.content,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                    key,
                    content,
                    self._encode(dict(memory)),
                    str(memory.get("created_at") or now),
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def delete_memory(
        self,
        bot_id: str,
        key: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> bool:
        connection = self._open()
        try:
            now = datetime.now(timezone.utc).isoformat()
            cursor = connection.execute(
                """
                UPDATE bot_memories
                SET content = '', metadata_json = ?, updated_at = ?
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ? AND memory_key = ?
                """,
                (
                    self._encode({"_deleted": True, "key": str(key)}),
                    now,
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                    str(key),
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def get_summary(
        self,
        bot_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> dict[str, Any] | None:
        connection = self._open()
        try:
            row = connection.execute(
                """
                SELECT summary_json
                FROM bot_session_summaries
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ?
                """,
                (str(bot_id), issuer, tenant_id, owner_subject),
            ).fetchone()
            if row is None:
                return None
            return _json_load_object(row["summary_json"])
        finally:
            connection.close()

    def upsert_summary(
        self,
        *,
        bot_id: str,
        summary: Mapping[str, Any],
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> None:
        now = str(
            summary.get("updated_at")
            or datetime.now(timezone.utc).isoformat()
        )
        connection = self._open()
        try:
            connection.execute(
                """
                INSERT INTO bot_session_summaries (
                    bot_id, issuer, tenant_id, owner_subject,
                    summary_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, issuer, tenant_id, owner_subject)
                DO UPDATE SET
                    summary_json = excluded.summary_json,
                    updated_at = excluded.updated_at
                """,
                (
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                    self._encode(dict(summary)),
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def clear_summary(
        self,
        bot_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> None:
        # Keep a tombstone row so a legacy JSON snapshot cannot resurrect
        # cleared state during the compatibility window.
        self.upsert_summary(
            bot_id=bot_id,
            summary={"summary": "", "_cleared": True},
            issuer=issuer,
            tenant_id=tenant_id,
            owner_subject=owner_subject,
        )

    # ------------------------------------------------------------------
    # Skill metadata; source code stays in the Blob/file layer.
    # ------------------------------------------------------------------
    def list_skills(
        self,
        bot_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> list[dict[str, Any]]:
        connection = self._open()
        try:
            rows = connection.execute(
                """
                SELECT *
                FROM bot_skills
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ?
                ORDER BY name ASC
                """,
                (str(bot_id), issuer, tenant_id, owner_subject),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                metadata = _json_load_object(row["definition_json"])
                metadata.update(
                    {
                        "name": str(row["name"]),
                        "version": row["version"] or "",
                        "code_uri": row["code_uri"],
                        "code_sha256": row["code_sha256"],
                        "created_at": str(row["created_at"]),
                        "updated_at": str(row["updated_at"]),
                    }
                )
                result.append(metadata)
            return result
        finally:
            connection.close()

    def get_skill(
        self,
        bot_id: str,
        name: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> dict[str, Any] | None:
        connection = self._open()
        try:
            row = connection.execute(
                """
                SELECT *
                FROM bot_skills
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ? AND name = ?
                """,
                (str(bot_id), issuer, tenant_id, owner_subject, str(name)),
            ).fetchone()
            if row is None:
                return None
            metadata = _json_load_object(row["definition_json"])
            metadata.update(
                {
                    "name": str(row["name"]),
                    "version": row["version"] or "",
                    "code_uri": row["code_uri"],
                    "code_sha256": row["code_sha256"],
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"]),
                }
            )
            return metadata
        finally:
            connection.close()

    def upsert_skill(
        self,
        *,
        bot_id: str,
        skill: Mapping[str, Any],
        code_uri: str | None,
        code_sha256: str | None,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> None:
        name = str(skill.get("name") or "")
        if not name:
            raise ValueError("skill name cannot be empty")
        now = str(
            skill.get("updated_at")
            or skill.get("created_at")
            or datetime.now(timezone.utc).isoformat()
        )
        skill_id = hashlib.sha256(
            f"{bot_id}\0{issuer}\0{tenant_id}\0{owner_subject}\0{name}".encode(
                "utf-8"
            )
        ).hexdigest()
        connection = self._open()
        try:
            connection.execute(
                """
                INSERT INTO bot_skills (
                    skill_id, bot_id, issuer, tenant_id, owner_subject,
                    name, version, definition_json, code_uri, code_sha256,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(bot_id, issuer, tenant_id, owner_subject, name)
                DO UPDATE SET
                    version = excluded.version,
                    definition_json = excluded.definition_json,
                    code_uri = excluded.code_uri,
                    code_sha256 = excluded.code_sha256,
                    updated_at = excluded.updated_at
                """,
                (
                    skill_id,
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                    name,
                    str(skill.get("version") or ""),
                    self._encode(dict(skill)),
                    code_uri,
                    code_sha256,
                    str(skill.get("created_at") or now),
                    now,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def delete_skill(
        self,
        bot_id: str,
        name: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> bool:
        connection = self._open()
        try:
            cursor = connection.execute(
                """
                DELETE FROM bot_skills
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ? AND name = ?
                """,
                (str(bot_id), issuer, tenant_id, owner_subject, str(name)),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Conversation history
    # ------------------------------------------------------------------
    def _ensure_session(
        self,
        connection: sqlite3.Connection,
        *,
        session_id: str,
        bot_id: str,
        issuer: str,
        tenant_id: str,
        owner_subject: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO conversation_sessions (
                session_id, bot_id, issuer, tenant_id, owner_subject,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (
                session_id,
                str(bot_id),
                issuer,
                tenant_id,
                owner_subject,
                now,
                now,
            ),
        )

    def append_message(
        self,
        *,
        bot_id: str,
        message: Mapping[str, Any],
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> int:
        session_id = self.session_id(
            bot_id,
            issuer=issuer,
            tenant_id=tenant_id,
            owner_subject=owner_subject,
        )
        now = str(message.get("timestamp") or datetime.now(timezone.utc).isoformat())
        message_id = str(message.get("id") or uuid.uuid4().hex)
        metadata = dict(message.get("metadata") or {})
        metadata.update(
            {
                key: value
                for key, value in message.items()
                if key not in {"id", "role", "content", "timestamp", "metadata"}
            }
        )
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_session(
                connection,
                session_id=session_id,
                bot_id=bot_id,
                issuer=issuer,
                tenant_id=tenant_id,
                owner_subject=owner_subject,
                now=now,
            )
            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), -1) + 1
                FROM conversation_messages
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            sequence = int(row[0])
            connection.execute(
                """
                INSERT INTO conversation_messages (
                    message_id, session_id, sequence, role, content,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    sequence,
                    str(message.get("role") or "assistant"),
                    str(message.get("content") or ""),
                    self._encode(metadata),
                    now,
                ),
            )
            connection.commit()
            return sequence
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def replace_conversation(
        self,
        *,
        session_id: str,
        bot_id: str,
        messages: list[Mapping[str, Any]],
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._ensure_session(
                connection,
                session_id=session_id,
                bot_id=bot_id,
                issuer=issuer,
                tenant_id=tenant_id,
                owner_subject=owner_subject,
                now=now,
            )
            connection.execute(
                "DELETE FROM conversation_messages WHERE session_id = ?",
                (session_id,),
            )
            for sequence, message in enumerate(messages):
                message_id = str(
                    message.get("id")
                    or hashlib.sha256(
                        f"{session_id}:{sequence}".encode("utf-8")
                    ).hexdigest()[:32]
                )
                created_at = str(message.get("timestamp") or now)
                metadata = dict(message.get("metadata") or {})
                metadata.update(
                    {
                        key: value
                        for key, value in message.items()
                        if key not in {"id", "role", "content", "timestamp", "metadata"}
                    }
                )
                connection.execute(
                    """
                    INSERT INTO conversation_messages (
                        message_id, session_id, sequence, role, content,
                        metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        session_id,
                        sequence,
                        str(message.get("role") or "assistant"),
                        str(message.get("content") or ""),
                        self._encode(metadata),
                        created_at,
                    ),
                )
            connection.commit()
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def read_conversation(
        self,
        *,
        bot_id: str,
        limit: int,
        before_id: str | None = None,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> dict[str, Any]:
        session_id = self.session_id(
            bot_id,
            issuer=issuer,
            tenant_id=tenant_id,
            owner_subject=owner_subject,
        )
        page_limit = max(1, min(int(limit), 5000))
        connection = self._open()
        try:
            session_row = connection.execute(
                """
                SELECT 1
                FROM conversation_sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            total_row = connection.execute(
                "SELECT COUNT(*) FROM conversation_messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
            total = int(total_row[0] if total_row is not None else 0)
            if before_id:
                target = connection.execute(
                    """
                    SELECT sequence FROM conversation_messages
                    WHERE session_id = ? AND message_id = ?
                    """,
                    (session_id, str(before_id)),
                ).fetchone()
                if target is None:
                    return {"history": [], "total": total, "has_more": False}
                rows = connection.execute(
                    """
                    SELECT m.*, s.bot_id, s.issuer, s.tenant_id, s.owner_subject
                    FROM conversation_messages m
                    JOIN conversation_sessions s ON s.session_id = m.session_id
                    WHERE m.session_id = ? AND m.sequence < ?
                    ORDER BY m.sequence DESC
                    LIMIT ?
                    """,
                    (session_id, int(target["sequence"]), page_limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT m.*, s.bot_id, s.issuer, s.tenant_id, s.owner_subject
                    FROM conversation_messages m
                    JOIN conversation_sessions s ON s.session_id = m.session_id
                    WHERE m.session_id = ?
                    ORDER BY m.sequence DESC
                    LIMIT ?
                    """,
                    (session_id, page_limit),
                ).fetchall()
            messages = [self._decode_message(row) for row in reversed(rows)]
            has_more = bool(rows and int(rows[-1]["sequence"]) > 0)
            return {
                "history": messages,
                "total": total,
                "has_more": has_more,
                "storage_exists": session_row is not None,
            }
        finally:
            connection.close()

    def pop_last_assistant(
        self,
        *,
        bot_id: str,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> dict[str, Any] | None:
        session_id = self.session_id(
            bot_id,
            issuer=issuer,
            tenant_id=tenant_id,
            owner_subject=owner_subject,
        )
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM conversation_messages
                WHERE session_id = ?
                ORDER BY sequence DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None or str(row["role"]) != "assistant":
                connection.rollback()
                return None
            connection.execute(
                "DELETE FROM conversation_messages WHERE message_id = ?",
                (str(row["message_id"]),),
            )
            connection.commit()
            return self._decode_message(row)
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def truncate_conversation(
        self,
        *,
        bot_id: str,
        message_id: str,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> bool:
        session_id = self.session_id(
            bot_id,
            issuer=issuer,
            tenant_id=tenant_id,
            owner_subject=owner_subject,
        )
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT sequence
                FROM conversation_messages
                WHERE session_id = ? AND message_id = ?
                """,
                (session_id, str(message_id)),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            connection.execute(
                """
                DELETE FROM conversation_messages
                WHERE session_id = ? AND sequence > ?
                """,
                (session_id, int(row["sequence"])),
            )
            connection.commit()
            return True
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def clear_conversation(
        self,
        *,
        bot_id: str,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> None:
        session_id = self.session_id(
            bot_id,
            issuer=issuer,
            tenant_id=tenant_id,
            owner_subject=owner_subject,
        )
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM conversation_messages WHERE session_id = ?",
                (session_id,),
            )
            connection.execute(
                """
                UPDATE conversation_sessions
                SET updated_at = ?
                WHERE session_id = ?
                """,
                (datetime.now(timezone.utc).isoformat(), session_id),
            )
            connection.commit()
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # File metadata and Blob references
    # ------------------------------------------------------------------
    def reserve_file_slot(
        self,
        *,
        reservation_id: str,
        bot_id: str,
        max_files: int,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> bool:
        """Atomically reserve one file-count slot across local processes."""
        maximum = max(1, int(max_files))
        now = time.time()
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # A crashed upload must not consume capacity forever.
            connection.execute(
                "DELETE FROM bot_file_reservations WHERE created_at < ?",
                (now - 60 * 60,),
            )
            row = connection.execute(
                """
                SELECT
                    (SELECT COUNT(*)
                     FROM bot_files
                     WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                       AND owner_subject = ? AND deleted_at IS NULL)
                    +
                    (SELECT COUNT(*)
                     FROM bot_file_reservations
                     WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                       AND owner_subject = ?)
                """,
                (
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                ),
            ).fetchone()
            current = int(row[0] if row is not None else 0)
            if current >= maximum:
                connection.rollback()
                return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO bot_file_reservations (
                    reservation_id, bot_id, issuer, tenant_id,
                    owner_subject, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(reservation_id),
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                    now,
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def release_file_slot(
        self,
        reservation_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> bool:
        connection = self._open()
        try:
            cursor = connection.execute(
                """
                DELETE FROM bot_file_reservations
                WHERE reservation_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ?
                """,
                (str(reservation_id), issuer, tenant_id, owner_subject),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def upsert_file(self, record: Mapping[str, Any]) -> None:
        now = str(record.get("created_at") or datetime.now(timezone.utc).isoformat())
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO bot_files (
                    file_id, bot_id, issuer, tenant_id, owner_subject,
                    blob_uri, original_name, stored_name, content_type,
                    size_bytes, sha256, deleted_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                ON CONFLICT(file_id) DO UPDATE SET
                    blob_uri = excluded.blob_uri,
                    original_name = excluded.original_name,
                    stored_name = excluded.stored_name,
                    content_type = excluded.content_type,
                    size_bytes = excluded.size_bytes,
                    sha256 = excluded.sha256,
                    deleted_at = NULL
                """,
                (
                    str(record["file_id"]),
                    str(record["bot_id"]),
                    str(record.get("issuer") or ""),
                    str(record.get("tenant_id") or ""),
                    str(record.get("owner_subject") or ""),
                    str(record["blob_uri"]),
                    str(record.get("original_name") or ""),
                    str(record["stored_name"]),
                    record.get("content_type"),
                    int(record.get("size_bytes") or 0),
                    record.get("sha256"),
                    now,
                ),
            )
            connection.execute(
                """
                DELETE FROM bot_file_reservations
                WHERE reservation_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ?
                """,
                (
                    str(record["file_id"]),
                    str(record.get("issuer") or ""),
                    str(record.get("tenant_id") or ""),
                    str(record.get("owner_subject") or ""),
                ),
            )
            connection.commit()
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def list_files(
        self,
        bot_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> list[dict[str, Any]]:
        connection = self._open()
        try:
            rows = connection.execute(
                """
                SELECT *
                FROM bot_files
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ? AND deleted_at IS NULL
                ORDER BY created_at DESC, stored_name ASC
                """,
                (str(bot_id), issuer, tenant_id, owner_subject),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def find_file(
        self,
        bot_id: str,
        filename: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> dict[str, Any] | None:
        connection = self._open()
        try:
            row = connection.execute(
                """
                SELECT *
                FROM bot_files
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ? AND deleted_at IS NULL
                  AND (stored_name = ? OR original_name = ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                    str(filename),
                    str(filename),
                ),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def delete_file(
        self,
        bot_id: str,
        filename: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> bool:
        connection = self._open()
        try:
            cursor = connection.execute(
                """
                DELETE FROM bot_files
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ? AND (stored_name = ? OR original_name = ?)
                """,
                (
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                    str(filename),
                    str(filename),
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def delete_files_for_scope(
        self,
        bot_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> int:
        connection = self._open()
        try:
            cursor = connection.execute(
                """
                DELETE FROM bot_files
                WHERE bot_id = ? AND issuer = ? AND tenant_id = ?
                  AND owner_subject = ?
                """,
                (str(bot_id), issuer, tenant_id, owner_subject),
            )
            connection.commit()
            return int(cursor.rowcount)
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # RAG documents and chunks
    # ------------------------------------------------------------------
    def list_rag_documents(
        self,
        bot_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> list[dict[str, Any]]:
        connection = self._open()
        try:
            rows = connection.execute(
                """
                SELECT d.*, COUNT(c.chunk_index) AS chunks_count
                FROM rag_documents d
                LEFT JOIN rag_chunks c ON c.document_id = d.document_id
                WHERE d.bot_id = ? AND d.issuer = ? AND d.tenant_id = ?
                  AND d.owner_subject = ?
                GROUP BY d.document_id
                ORDER BY d.created_at DESC, d.document_id DESC
                """,
                (str(bot_id), issuer, tenant_id, owner_subject),
            ).fetchall()
            return [
                self._decode_document(row, chunks_count=int(row["chunks_count"]))
                for row in rows
            ]
        finally:
            connection.close()

    def get_rag_document(
        self,
        bot_id: str,
        document_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> dict[str, Any] | None:
        connection = self._open()
        try:
            row = connection.execute(
                """
                SELECT d.*, COUNT(c.chunk_index) AS chunks_count
                FROM rag_documents d
                LEFT JOIN rag_chunks c ON c.document_id = d.document_id
                WHERE d.document_id = ? AND d.bot_id = ?
                  AND d.issuer = ? AND d.tenant_id = ?
                  AND d.owner_subject = ?
                GROUP BY d.document_id
                """,
                (
                    str(document_id),
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                ),
            ).fetchone()
            return (
                self._decode_document(row, chunks_count=int(row["chunks_count"]))
                if row is not None
                else None
            )
        finally:
            connection.close()

    def list_rag_chunks(
        self,
        bot_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> list[dict[str, Any]]:
        connection = self._open()
        try:
            rows = connection.execute(
                """
                SELECT c.*, d.name
                FROM rag_chunks c
                JOIN rag_documents d ON d.document_id = c.document_id
                WHERE d.bot_id = ? AND d.issuer = ? AND d.tenant_id = ?
                  AND d.owner_subject = ?
                ORDER BY c.document_id ASC, c.chunk_index ASC
                """,
                (str(bot_id), issuer, tenant_id, owner_subject),
            ).fetchall()
            return [self._decode_chunk(row) for row in rows]
        finally:
            connection.close()

    def replace_rag_document(
        self,
        *,
        bot_id: str,
        document: Mapping[str, Any],
        chunks: list[Mapping[str, Any]],
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
        blob_uri: str | None = None,
    ) -> int:
        now = str(
            document.get("updated_at")
            or document.get("created_at")
            or datetime.now(timezone.utc).isoformat()
        )
        created = str(document.get("created_at") or now)
        document_id = str(document["id"])
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Standalone RagManager instances used by migration/tests may not
            # have a BotManager row yet; create a minimal owner-scoped parent.
            connection.execute(
                """
                INSERT INTO bots (
                    bot_id, issuer, tenant_id, owner_subject, visibility,
                    definition_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'private', '{}', ?, ?)
                ON CONFLICT(bot_id) DO NOTHING
                """,
                (
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject or None,
                    created,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO rag_documents (
                    document_id, bot_id, issuer, tenant_id, owner_subject,
                    name, blob_uri, status, version, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    bot_id = excluded.bot_id,
                    issuer = excluded.issuer,
                    tenant_id = excluded.tenant_id,
                    owner_subject = excluded.owner_subject,
                    name = excluded.name,
                    blob_uri = excluded.blob_uri,
                    status = excluded.status,
                    version = excluded.version,
                    metadata_json = excluded.metadata_json,
                    updated_at = excluded.updated_at
                """,
                (
                    document_id,
                    str(bot_id),
                    issuer,
                    tenant_id,
                    owner_subject,
                    str(document.get("name") or document_id),
                    blob_uri,
                    "ENABLED" if document.get("enabled", True) else "DISABLED",
                    int(document.get("version") or 1),
                    self._encode(dict(document)),
                    created,
                    now,
                ),
            )
            connection.execute(
                "DELETE FROM rag_chunks WHERE document_id = ?",
                (document_id,),
            )
            used_indexes: set[int] = set()
            next_index = 0
            collisions = 0
            for position, chunk in enumerate(chunks):
                raw_index = chunk.get("index", position)
                try:
                    requested_index = int(raw_index)
                except (TypeError, ValueError):
                    requested_index = position
                chunk_index = requested_index
                if chunk_index < 0 or chunk_index in used_indexes:
                    collisions += 1
                    chunk_index = max(0, next_index)
                    while chunk_index in used_indexes:
                        chunk_index += 1
                used_indexes.add(chunk_index)
                next_index = max(next_index, chunk_index + 1)
                metadata = {
                    key: value
                    for key, value in chunk.items()
                    if key not in {"content", "index"}
                }
                if chunk_index != requested_index:
                    metadata["source_chunk_index"] = raw_index
                connection.execute(
                    """
                    INSERT INTO rag_chunks (
                        document_id, chunk_index, content, metadata_json, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        chunk_index,
                        str(chunk.get("content") or ""),
                        self._encode(metadata),
                        now,
                    ),
                )
            connection.commit()
            return collisions
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def toggle_rag_document(
        self,
        bot_id: str,
        document_id: str,
        enabled: bool | None,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> bool:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT metadata_json, status
                FROM rag_documents
                WHERE document_id = ? AND bot_id = ? AND issuer = ?
                  AND tenant_id = ? AND owner_subject = ?
                """,
                (str(document_id), str(bot_id), issuer, tenant_id, owner_subject),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            current = str(row["status"]) == "ENABLED"
            next_enabled = (not current) if enabled is None else bool(enabled)
            metadata = _json_load_object(row["metadata_json"])
            metadata["enabled"] = next_enabled
            connection.execute(
                """
                UPDATE rag_documents
                SET status = ?, metadata_json = ?, updated_at = ?
                WHERE document_id = ?
                """,
                (
                    "ENABLED" if next_enabled else "DISABLED",
                    self._encode(metadata),
                    datetime.now(timezone.utc).isoformat(),
                    str(document_id),
                ),
            )
            connection.commit()
            return True
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def delete_rag_document(
        self,
        bot_id: str,
        document_id: str,
        *,
        issuer: str = "",
        tenant_id: str = "",
        owner_subject: str = "",
    ) -> dict[str, Any] | None:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT d.*, COUNT(c.chunk_index) AS chunks_count
                FROM rag_documents d
                LEFT JOIN rag_chunks c ON c.document_id = d.document_id
                WHERE d.document_id = ? AND d.bot_id = ? AND d.issuer = ?
                  AND d.tenant_id = ? AND d.owner_subject = ?
                GROUP BY d.document_id
                """,
                (str(document_id), str(bot_id), issuer, tenant_id, owner_subject),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            document = self._decode_document(row, chunks_count=int(row["chunks_count"]))
            connection.execute(
                "DELETE FROM rag_documents WHERE document_id = ?",
                (str(document_id),),
            )
            connection.commit()
            return document
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()


class SqliteTaskStore:
    """Small persistence adapter for task facts and durable task events.

    The runner still owns the in-process worker object.  This adapter owns the
    cross-process facts that must survive a worker restart: task metadata,
    leases, control state, idempotency state, and replayable events.
    """

    _BATCH_TASK_FIELDS = frozenset(
        {
            "status",
            "current_index",
            "total",
            "success_count",
            "failed_count",
            "worker_id",
            "lease_until",
            "last_heartbeat",
            "attempt",
            "error_message",
            "result_location",
            "started_at",
            "finished_at",
            "updated_at",
        }
    )

    def __init__(self, path: str | None = None):
        self.path = database_path(path)

    def _open(self) -> sqlite3.Connection:
        return open_database(self.path)

    def create_batch_task(self, record: Mapping[str, Any]) -> bool:
        """Insert a durable task reservation.

        ``False`` means another process already inserted the same task id.  The
        task id is generated by the caller and remains the stable public handle.
        """
        now = float(record.get("created_at") or time.time())
        connection = self._open()
        try:
            tombstone = connection.execute(
                """
                SELECT 1
                FROM batch_task_tombstones
                WHERE task_id = ?
                """,
                (str(record["task_id"]),),
            ).fetchone()
            if tombstone is not None:
                return False
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO batch_tasks (
                    task_id, issuer, tenant_id, owner_subject, api_id, status,
                    current_index, total, success_count, failed_count, dry_run,
                    request_hash, items_json, environment_id, worker_id,
                    lease_until, last_heartbeat, attempt, error_message,
                    result_location, created_at, started_at, finished_at,
                    updated_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?
                )
                """,
                (
                    str(record["task_id"]),
                    str(record.get("issuer") or ""),
                    str(record.get("tenant_id") or ""),
                    str(record.get("owner_subject") or ""),
                    str(record["api_id"]),
                    str(record.get("status") or "RESERVED"),
                    int(record.get("current_index") or 0),
                    int(record.get("total") or 0),
                    int(record.get("success_count") or 0),
                    int(record.get("failed_count") or 0),
                    int(bool(record.get("dry_run"))),
                    record.get("request_hash"),
                    record.get("items_json"),
                    record.get("environment_id"),
                    record.get("worker_id"),
                    record.get("lease_until"),
                    record.get("last_heartbeat"),
                    int(record.get("attempt") or 0),
                    record.get("error_message"),
                    record.get("result_location"),
                    now,
                    record.get("started_at"),
                    record.get("finished_at"),
                    float(record.get("updated_at") or now),
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def is_batch_task_tombstoned(self, task_id: str) -> bool:
        connection = self._open()
        try:
            return (
                connection.execute(
                    """
                    SELECT 1
                    FROM batch_task_tombstones
                    WHERE task_id = ?
                    """,
                    (str(task_id),),
                ).fetchone()
                is not None
            )
        finally:
            connection.close()

    def get_batch_task(
        self,
        task_id: str,
        *,
        reconcile_expired: bool = True,
    ) -> dict[str, Any] | None:
        connection = self._open()
        try:
            if reconcile_expired:
                self._reconcile_expired_locked(connection, task_id, time.time())
                connection.commit()
            row = connection.execute(
                "SELECT * FROM batch_tasks WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def _reconcile_expired_locked(
        self,
        connection: sqlite3.Connection,
        task_id: str,
        now: float,
    ) -> bool:
        row = connection.execute(
            """
            SELECT status, lease_until
            FROM batch_tasks
            WHERE task_id = ?
            """,
            (str(task_id),),
        ).fetchone()
        if row is None:
            return False
        lease_until = row["lease_until"]
        if (
            row["status"] != "RUNNING"
            or lease_until is None
            or float(lease_until) > now
        ):
            return False
        connection.execute(
            """
            UPDATE batch_tasks
            SET status = 'UNKNOWN',
                worker_id = NULL,
                lease_until = NULL,
                error_message = COALESCE(
                    error_message,
                    'worker lease expired; execution outcome unknown'
                ),
                finished_at = COALESCE(finished_at, ?),
                updated_at = ?
            WHERE task_id = ? AND status = 'RUNNING'
            """,
            (now, now, str(task_id)),
        )
        connection.execute(
            """
            UPDATE idempotency_keys
            SET state = 'UNKNOWN',
                worker_id = NULL,
                lease_until = NULL,
                last_error = COALESCE(
                    last_error,
                    'worker lease expired; execution outcome unknown'
                ),
                finished_at = COALESCE(finished_at, ?),
                updated_at = ?
            WHERE task_id = ? AND state = 'RUNNING'
            """,
            (now, now, str(task_id)),
        )
        return True

    def create_or_repair_batch_task(
        self,
        record: Mapping[str, Any],
    ) -> bool:
        """Create a task row for a legacy idempotency record if it is missing."""
        return self.create_batch_task(record)

    def update_batch_task(
        self,
        task_id: str,
        *,
        expected_worker_id: str | None = None,
        **fields: Any,
    ) -> bool:
        unknown = set(fields) - self._BATCH_TASK_FIELDS
        if unknown:
            raise ValueError(f"不允许更新任务字段: {', '.join(sorted(unknown))}")
        if not fields:
            return False
        fields.setdefault("updated_at", time.time())
        assignments = ", ".join(f"{name} = ?" for name in fields)
        values = [fields[name] for name in fields]
        values.append(str(task_id))
        predicate = "task_id = ?"
        if expected_worker_id is not None:
            predicate += " AND worker_id = ?"
            values.append(expected_worker_id)
        connection = self._open()
        try:
            cursor = connection.execute(
                f"UPDATE batch_tasks SET {assignments} WHERE {predicate}",
                tuple(values),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def claim_batch_task(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: int = BATCH_TASK_LEASE_SECONDS,
    ) -> bool:
        """Atomically claim a reserved or stale task for one worker."""
        now = time.time()
        lease_until = now + max(1, int(lease_seconds))
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._reconcile_expired_locked(connection, task_id, now)
            row = connection.execute(
                """
                SELECT status, worker_id, lease_until, attempt
                FROM batch_tasks
                WHERE task_id = ?
                """,
                (str(task_id),),
            ).fetchone()
            if row is None:
                connection.rollback()
                return False
            status = str(row["status"])
            current_worker = row["worker_id"]
            current_lease = row["lease_until"]
            # UNKNOWN means an external call may have completed before the
            # worker died.  It must be reviewed/reconciled before any retry;
            # automatic re-execution would turn a timeout into a duplicate
            # side effect.
            claimable = status in {"RESERVED", "PENDING"}
            if status == "RUNNING":
                claimable = (
                    current_worker == worker_id
                    or current_lease is None
                    or float(current_lease) <= now
                )
            if not claimable or status in {
                "COMPLETED",
                "FAILED",
                "CANCELLED",
            }:
                connection.rollback()
                return False
            connection.execute(
                """
                UPDATE batch_tasks
                SET status = 'RUNNING',
                    worker_id = ?,
                    lease_until = ?,
                    last_heartbeat = ?,
                    attempt = ?,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE task_id = ?
                """,
                (
                    worker_id,
                    lease_until,
                    now,
                    int(row["attempt"] or 0) + 1,
                    now,
                    now,
                    str(task_id),
                ),
            )
            connection.execute(
                """
                UPDATE idempotency_keys
                SET state = 'RUNNING',
                    worker_id = ?,
                    lease_until = ?,
                    last_heartbeat = ?,
                    updated_at = ?
                WHERE task_id = ?
                  AND state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
                """,
                (worker_id, lease_until, now, now, str(task_id)),
            )
            connection.commit()
            return True
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def heartbeat_batch_task(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: int = BATCH_TASK_LEASE_SECONDS,
    ) -> bool:
        now = time.time()
        lease_until = now + max(1, int(lease_seconds))
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE batch_tasks
                SET lease_until = ?, last_heartbeat = ?, updated_at = ?
                WHERE task_id = ? AND worker_id = ?
                  AND status IN ('RUNNING', 'PAUSED')
                """,
                (lease_until, now, now, str(task_id), worker_id),
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    UPDATE idempotency_keys
                    SET lease_until = ?, last_heartbeat = ?, updated_at = ?
                    WHERE task_id = ? AND worker_id = ?
                    """,
                    (lease_until, now, now, str(task_id), worker_id),
                )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def set_task_terminal(
        self,
        task_id: str,
        *,
        status: str,
        worker_id: str | None,
        current_index: int,
        success_count: int,
        failed_count: int,
        error_message: str | None = None,
        result_location: str | None = None,
        started_at: float | None = None,
        finished_at: float | None = None,
    ) -> bool:
        if status not in {"COMPLETED", "FAILED", "CANCELLED", "UNKNOWN"}:
            raise ValueError(f"非法任务终态: {status}")
        now = time.time()
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            where = "task_id = ?"
            where_values: list[Any] = [str(task_id)]
            if worker_id is not None:
                # A worker may only publish a terminal state while it still
                # owns the active lease. An old worker must not turn a
                # lease-expired UNKNOWN task into COMPLETED.
                where += " AND worker_id = ? AND status IN ('RUNNING', 'PAUSED')"
                where_values.append(worker_id)
            else:
                # This path is used for a task cancelled before its worker
                # thread starts, while the row is still RESERVED/PENDING.
                where += " AND status IN ('RESERVED', 'PENDING')"
            cursor = connection.execute(
                f"""
                UPDATE batch_tasks
                SET status = ?, current_index = ?, success_count = ?,
                    failed_count = ?, error_message = ?, result_location = ?,
                    finished_at = COALESCE(finished_at, ?),
                    started_at = COALESCE(started_at, ?),
                    worker_id = NULL, lease_until = NULL,
                    last_heartbeat = ?, updated_at = ?
                WHERE {where}
                """,
                (
                    status,
                    int(current_index),
                    int(success_count),
                    int(failed_count),
                    error_message,
                    result_location,
                    finished_at or now,
                    started_at or finished_at or now,
                    now,
                    now,
                    *where_values,
                ),
            )
            if cursor.rowcount:
                # Update the idempotency ledger only when this worker won the
                # task-row transition. A stale worker must not rewrite the
                # ledger after another process cancelled or quarantined it.
                connection.execute(
                    """
                    UPDATE idempotency_keys
                    SET state = ?, last_error = ?, result_location = ?,
                        worker_id = NULL, lease_until = NULL,
                        updated_at = ?, finished_at = COALESCE(finished_at, ?)
                    WHERE task_id = ?
                      AND state NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
                    """,
                    (
                        status,
                        error_message,
                        result_location,
                        now,
                        finished_at or now,
                        str(task_id),
                    ),
                )
            connection.commit()
            return cursor.rowcount > 0
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def request_control(self, task_id: str, action: str) -> dict[str, Any] | None:
        """Persist pause/resume/cancel so a different worker can observe it."""
        now = time.time()
        transitions = {
            "pause": (
                "PAUSED",
                "status IN ('RUNNING')",
            ),
            "resume": (
                "RUNNING",
                "status = 'PAUSED'",
            ),
            "stop": (
                "CANCELLED",
                "status IN ('RESERVED', 'PENDING', 'RUNNING', 'PAUSED', 'UNKNOWN', 'EXPIRED')",
            ),
        }
        if action not in transitions:
            raise ValueError(f"未知任务控制动作: {action}")
        status, predicate = transitions[action]
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if action == "stop":
                cursor = connection.execute(
                    f"""
                    UPDATE batch_tasks
                    SET status = ?, worker_id = NULL, lease_until = NULL,
                        finished_at = COALESCE(finished_at, ?),
                        updated_at = ?
                    WHERE task_id = ? AND {predicate}
                    """,
                    (status, now, now, str(task_id)),
                )
            else:
                cursor = connection.execute(
                    f"""
                    UPDATE batch_tasks
                    SET status = ?, updated_at = ?
                    WHERE task_id = ? AND {predicate}
                    """,
                    (status, now, str(task_id)),
                )
            if cursor.rowcount:
                if action == "stop":
                    connection.execute(
                        """
                        UPDATE idempotency_keys
                        SET state = 'CANCELLED',
                            worker_id = NULL,
                            lease_until = NULL,
                            finished_at = COALESCE(finished_at, ?),
                            updated_at = ?
                        WHERE task_id = ?
                        """,
                        (now, now, str(task_id)),
                    )
                else:
                    connection.execute(
                        """
                        UPDATE idempotency_keys
                        SET state = ?, updated_at = ?
                        WHERE task_id = ?
                        """,
                        (status, now, str(task_id)),
                    )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM batch_tasks WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            return dict(row) if row is not None else None
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def append_task_event(
        self,
        task_id: str,
        event_type: str,
        data: Mapping[str, Any],
        *,
        timestamp: float | None = None,
    ) -> int:
        now = float(timestamp or time.time())
        payload = json.dumps(
            {"data": dict(data), "timestamp": now},
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT COALESCE(MAX(sequence), -1) + 1
                FROM task_events
                WHERE task_id = ?
                """,
                (str(task_id),),
            ).fetchone()
            sequence = int(row[0])
            connection.execute(
                """
                INSERT INTO task_events
                    (task_id, sequence, event_type, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(task_id), sequence, str(event_type), payload, now),
            )
            minimum_sequence = sequence - MAX_TASK_EVENTS + 1
            if minimum_sequence > 0:
                connection.execute(
                    """
                    DELETE FROM task_events
                    WHERE task_id = ? AND sequence < ?
                    """,
                    (str(task_id), minimum_sequence),
                )
            connection.commit()
            return sequence
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def list_task_events(
        self,
        task_id: str,
        *,
        after_sequence: int = -1,
        limit: int = MAX_TASK_EVENTS,
    ) -> list[tuple[int, dict[str, Any]]]:
        connection = self._open()
        try:
            rows = connection.execute(
                """
                SELECT sequence, event_type, payload_json
                FROM task_events
                WHERE task_id = ? AND sequence > ?
                ORDER BY sequence ASC
                LIMIT ?
                """,
                (str(task_id), int(after_sequence), max(1, int(limit))),
            ).fetchall()
        finally:
            connection.close()
        events: list[tuple[int, dict[str, Any]]] = []
        for row in rows:
            payload = _json_load_object(row["payload_json"])
            event = {
                "type": row["event_type"],
                "data": payload.get("data", {}),
                "timestamp": payload.get("timestamp", row["sequence"]),
            }
            events.append((int(row["sequence"]), event))
        return events

    def append_batch_task_item(
        self,
        task_id: str,
        *,
        sequence: int,
        status: str,
        error_code: Any,
        result_sha256: str,
        byte_offset: int,
        byte_length: int,
        result_uri: str,
        created_at: float | None = None,
    ) -> bool:
        """Record the small, queryable index for one JSONL result row.

        The result body remains in the Blob/file layer.  This table stores only
        stable pagination and integrity metadata, so a future PostgreSQL
        adapter can keep the same contract without moving large response
        payloads into the relational database.
        """
        connection = self._open()
        try:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO batch_task_items (
                    task_id, sequence, status, error_code, result_sha256,
                    byte_offset, byte_length, result_uri, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(task_id),
                    int(sequence),
                    str(status or "UNKNOWN"),
                    None if error_code is None else str(error_code),
                    str(result_sha256),
                    int(byte_offset),
                    int(byte_length),
                    str(result_uri),
                    float(created_at or time.time()),
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def count_batch_task_items(
        self,
        task_id: str,
        *,
        result_uri: str | None = None,
    ) -> int:
        connection = self._open()
        try:
            if result_uri is None:
                row = connection.execute(
                    "SELECT COUNT(*) FROM batch_task_items WHERE task_id = ?",
                    (str(task_id),),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT COUNT(*) FROM batch_task_items
                    WHERE task_id = ? AND result_uri = ?
                    """,
                    (str(task_id), str(result_uri)),
                ).fetchone()
            return int(row[0] if row is not None else 0)
        finally:
            connection.close()

    def list_batch_task_items(
        self,
        task_id: str,
        *,
        offset: int = 0,
        limit: int = 1000,
    ) -> list[dict[str, Any]]:
        connection = self._open()
        try:
            rows = connection.execute(
                """
                SELECT task_id, sequence, status, error_code, result_sha256,
                       byte_offset, byte_length, result_uri, created_at
                FROM batch_task_items
                WHERE task_id = ?
                ORDER BY sequence ASC
                LIMIT ? OFFSET ?
                """,
                (str(task_id), max(1, int(limit)), max(0, int(offset))),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def list_batch_tasks(
        self,
        *,
        owner_subject: str | None = None,
        include_all: bool = False,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        connection = self._open()
        try:
            if include_all:
                rows = connection.execute(
                    """
                    SELECT * FROM batch_tasks
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (max(1, int(limit)),),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM batch_tasks
                    WHERE owner_subject = ?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (str(owner_subject or ""), max(1, int(limit))),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def delete_batch_task(self, task_id: str, *, allow_active: bool = False) -> bool:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            predicate = "task_id = ?"
            values: tuple[Any, ...] = (str(task_id),)
            if not allow_active:
                predicate += (
                    " AND status IN ('COMPLETED', 'FAILED', 'CANCELLED')"
                )
            cursor = connection.execute(
                f"DELETE FROM batch_tasks WHERE {predicate}",
                values,
            )
            if cursor.rowcount:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO batch_task_tombstones(task_id, deleted_at)
                    VALUES (?, ?)
                    """,
                    (str(task_id), time.time()),
                )
                connection.execute(
                    "DELETE FROM batch_task_items WHERE task_id = ?",
                    (str(task_id),),
                )
            connection.commit()
            return cursor.rowcount > 0
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def delete_all_batch_tasks(self) -> int:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT OR REPLACE INTO batch_task_tombstones(task_id, deleted_at)
                SELECT task_id, ?
                FROM batch_tasks
                WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED')
                """,
                (time.time(),),
            )
            cursor = connection.execute(
                """
                DELETE FROM batch_tasks
                WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED')
                """
            )
            connection.execute(
                """
                DELETE FROM batch_task_items
                WHERE task_id NOT IN (SELECT task_id FROM batch_tasks)
                """
            )
            connection.commit()
            return int(cursor.rowcount)
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()


class SqliteSyncJobStore:
    """Durable status/idempotency adapter for catalog synchronization jobs."""

    def __init__(self, path: str | None = None):
        self.path = database_path(path)

    def _open(self) -> sqlite3.Connection:
        return open_database(self.path)

    @staticmethod
    def _scope_values(
        issuer: str | None,
        tenant_id: str | None,
        owner_subject: str | None,
    ) -> tuple[str, str, str]:
        return (
            str(issuer or ""),
            str(tenant_id or ""),
            str(owner_subject or ""),
        )

    @staticmethod
    def _reconcile_expired_locked(
        connection: sqlite3.Connection,
        *,
        task_id: str | None = None,
        kind: str | None = None,
        now: float,
    ) -> int:
        predicates = [
            "status = 'RUNNING'",
            "lease_until IS NOT NULL",
            "lease_until <= ?",
        ]
        values: list[Any] = [now]
        if task_id is not None:
            predicates.append("task_id = ?")
            values.append(str(task_id))
        if kind is not None:
            predicates.append("kind = ?")
            values.append(str(kind))
        cursor = connection.execute(
            f"""
            UPDATE sync_jobs
            SET status = 'UNKNOWN',
                worker_id = NULL,
                lease_until = NULL,
                error_message = COALESCE(
                    error_message,
                    'worker lease expired; synchronization outcome unknown'
                ),
                finished_at = COALESCE(finished_at, ?),
                updated_at = ?
            WHERE {' AND '.join(predicates)}
            """,
            [now, now, *values],
        )
        return int(cursor.rowcount)

    def reserve(
        self,
        *,
        task_id: str,
        kind: str,
        issuer: str | None = None,
        tenant_id: str | None = None,
        owner_subject: str | None,
        idempotency_key: str | None,
        is_admin: bool,
    ) -> dict[str, Any]:
        issuer_value, tenant_value, owner = self._scope_values(
            issuer,
            tenant_id,
            owner_subject,
        )
        now = time.time()
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._reconcile_expired_locked(connection, kind=kind, now=now)
            if idempotency_key:
                replay = connection.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE kind = ? AND issuer = ? AND tenant_id = ?
                      AND owner_subject = ?
                      AND idempotency_key = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (
                        str(kind),
                        issuer_value,
                        tenant_value,
                        owner,
                        str(idempotency_key),
                    ),
                ).fetchone()
                if replay is not None:
                    pending_recovery_ready = (
                        str(replay["status"]) == "PENDING"
                        and now - float(replay["created_at"] or now)
                        >= SYNC_JOB_LEASE_SECONDS
                    )
                    connection.commit()
                    return {
                        "status": (
                            "recoverable"
                            if pending_recovery_ready
                            else "idempotent_replay"
                        ),
                        "task_id": replay["task_id"],
                        "row": dict(replay),
                    }

            active = connection.execute(
                """
                SELECT * FROM sync_jobs
                WHERE kind = ?
                  AND status NOT IN (
                      'COMPLETED', 'FAILED', 'CANCELLED', 'UNKNOWN'
                  )
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(kind),),
            ).fetchone()
            if active is not None:
                connection.commit()
                same_scope = (
                    str(active["issuer"] or "") == issuer_value
                    and str(active["tenant_id"] or "") == tenant_value
                )
                if (
                    not same_scope
                    or (not is_admin and str(active["owner_subject"] or "") != owner)
                ):
                    return {
                        "status": "already_running",
                        "task_id": None,
                        "row": {
                            "kind": str(kind),
                            "status": str(active["status"]),
                        },
                    }
                return {
                    "status": "already_running",
                    "task_id": active["task_id"],
                    "row": dict(active),
                }

            connection.execute(
                """
                INSERT INTO sync_jobs (
                    task_id, kind, issuer, tenant_id, owner_subject, status,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'PENDING', ?, ?, ?)
                """,
                (
                    str(task_id),
                    str(kind),
                    issuer_value,
                    tenant_value,
                    owner,
                    idempotency_key,
                    now,
                    now,
                ),
            )
            connection.commit()
            return {
                "status": "started",
                "task_id": str(task_id),
                "row": {
                    "task_id": str(task_id),
                    "kind": str(kind),
                    "issuer": issuer_value,
                    "tenant_id": tenant_value,
                    "owner_subject": owner,
                    "status": "PENDING",
                    "created_at": now,
                    "updated_at": now,
                },
            }
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def get(self, task_id: str) -> dict[str, Any] | None:
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._reconcile_expired_locked(
                connection,
                task_id=str(task_id),
                now=time.time(),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM sync_jobs WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def latest(
        self,
        kind: str,
        *,
        issuer: str | None = None,
        tenant_id: str | None = None,
        owner_subject: str | None = None,
        include_all: bool = False,
    ) -> dict[str, Any] | None:
        connection = self._open()
        try:
            now = time.time()
            connection.execute("BEGIN IMMEDIATE")
            self._reconcile_expired_locked(
                connection,
                kind=str(kind),
                now=now,
            )
            connection.commit()
            issuer_value, tenant_value, owner = self._scope_values(
                issuer,
                tenant_id,
                owner_subject,
            )
            if include_all:
                row = connection.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE kind = ? AND issuer = ? AND tenant_id = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (str(kind), issuer_value, tenant_value),
                ).fetchone()
            else:
                row = connection.execute(
                    """
                    SELECT * FROM sync_jobs
                    WHERE kind = ? AND issuer = ? AND tenant_id = ?
                      AND owner_subject = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (str(kind), issuer_value, tenant_value, owner),
                ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def claim(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: int = SYNC_JOB_LEASE_SECONDS,
    ) -> bool:
        now = time.time()
        lease_until = now + max(1, int(lease_seconds))
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._reconcile_expired_locked(
                connection,
                task_id=str(task_id),
                now=now,
            )
            row = connection.execute(
                """
                SELECT status, worker_id, attempt, cancel_requested
                FROM sync_jobs
                WHERE task_id = ?
                """,
                (str(task_id),),
            ).fetchone()
            if row is None or str(row["status"]) != "PENDING":
                connection.rollback()
                return False
            if bool(int(row["cancel_requested"] or 0)):
                connection.execute(
                    """
                    UPDATE sync_jobs
                    SET status = 'CANCELLED',
                        worker_id = NULL,
                        lease_until = NULL,
                        cancel_requested = 1,
                        finished_at = COALESCE(finished_at, ?),
                        updated_at = ?
                    WHERE task_id = ? AND status = 'PENDING'
                    """,
                    (now, now, str(task_id)),
                )
                connection.commit()
                return False
            cursor = connection.execute(
                """
                UPDATE sync_jobs
                SET status = 'RUNNING',
                    worker_id = ?,
                    lease_until = ?,
                    last_heartbeat = ?,
                    attempt = ?,
                    started_at = COALESCE(started_at, ?),
                    updated_at = ?
                WHERE task_id = ? AND status = 'PENDING'
                """,
                (
                    str(worker_id),
                    lease_until,
                    now,
                    int(row["attempt"] or 0) + 1,
                    now,
                    now,
                    str(task_id),
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def heartbeat(
        self,
        task_id: str,
        worker_id: str,
        *,
        lease_seconds: int = SYNC_JOB_LEASE_SECONDS,
    ) -> bool:
        now = time.time()
        lease_until = now + max(1, int(lease_seconds))
        connection = self._open()
        try:
            cursor = connection.execute(
                """
                UPDATE sync_jobs
                SET lease_until = ?, last_heartbeat = ?, updated_at = ?
                WHERE task_id = ? AND worker_id = ? AND status = 'RUNNING'
                """,
                (lease_until, now, now, str(task_id), str(worker_id)),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def update_progress(
        self,
        task_id: str,
        progress: Mapping[str, Any],
        *,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Persist bounded non-terminal progress visible to every process."""

        encoded = json.dumps(
            _sanitize_audit_metadata(progress),
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )
        if len(encoded.encode("utf-8")) > 64 * 1024:
            raise ValueError("同步任务进度不能超过 64 KiB")
        now = time.time()
        connection = self._open()
        try:
            predicate = (
                "task_id = ? AND status NOT IN "
                "('COMPLETED', 'FAILED', 'CANCELLED', 'UNKNOWN')"
            )
            values: list[Any] = [encoded, now, str(task_id)]
            if expected_worker_id is not None:
                predicate += " AND (worker_id = ? OR worker_id IS NULL)"
                values.append(str(expected_worker_id))
            cursor = connection.execute(
                f"""
                UPDATE sync_jobs
                SET progress_json = ?, updated_at = ?
                WHERE {predicate}
                """,
                values,
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def request_cancel(self, task_id: str) -> dict[str, Any] | None:
        """Set a durable cancellation flag without racing terminal publish."""

        now = time.time()
        connection = self._open()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE sync_jobs
                SET status = CASE
                        WHEN status = 'PENDING' THEN 'CANCELLED'
                        ELSE status
                    END,
                    cancel_requested = 1,
                    worker_id = CASE
                        WHEN status = 'PENDING' THEN NULL
                        ELSE worker_id
                    END,
                    lease_until = CASE
                        WHEN status = 'PENDING' THEN NULL
                        ELSE lease_until
                    END,
                    finished_at = CASE
                        WHEN status = 'PENDING' THEN COALESCE(finished_at, ?)
                        ELSE finished_at
                    END,
                    updated_at = ?
                WHERE task_id = ?
                  AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'UNKNOWN')
                """,
                (now, now, str(task_id)),
            )
            connection.commit()
            row = connection.execute(
                "SELECT * FROM sync_jobs WHERE task_id = ?",
                (str(task_id),),
            ).fetchone()
            return dict(row) if row is not None else None
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()

    def is_cancel_requested(self, task_id: str) -> bool:
        connection = self._open()
        try:
            row = connection.execute(
                """
                SELECT cancel_requested
                FROM sync_jobs
                WHERE task_id = ?
                """,
                (str(task_id),),
            ).fetchone()
            return bool(row and int(row["cancel_requested"] or 0))
        finally:
            connection.close()

    def set_terminal(
        self,
        task_id: str,
        *,
        worker_id: str,
        status: str,
        result_json: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        if status not in {"COMPLETED", "FAILED", "CANCELLED"}:
            raise ValueError(f"非法同步任务终态: {status}")
        now = time.time()
        connection = self._open()
        try:
            cursor = connection.execute(
                """
                UPDATE sync_jobs
                SET status = ?, result_json = ?, error_message = ?,
                    worker_id = NULL, lease_until = NULL,
                    cancel_requested = CASE
                        WHEN ? = 'CANCELLED' THEN 1
                        ELSE cancel_requested
                    END,
                    last_heartbeat = ?, finished_at = COALESCE(finished_at, ?),
                    updated_at = ?
                WHERE task_id = ? AND worker_id = ? AND status = 'RUNNING'
                """,
                (
                    str(status),
                    result_json,
                    error_message,
                    str(status),
                    now,
                    now,
                    now,
                    str(task_id),
                    str(worker_id),
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def mark_unknown(
        self,
        task_id: str,
        *,
        worker_id: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        """Quarantine a running job without allowing a stale writer to win."""
        now = time.time()
        connection = self._open()
        try:
            predicate = "task_id = ? AND status = 'RUNNING'"
            values: list[Any] = [str(task_id)]
            if worker_id is not None:
                predicate += " AND worker_id = ?"
                values.append(str(worker_id))
            cursor = connection.execute(
                f"""
                UPDATE sync_jobs
                SET status = 'UNKNOWN',
                    worker_id = NULL,
                    lease_until = NULL,
                    error_message = COALESCE(?, error_message,
                        'synchronization outcome unknown'),
                    finished_at = COALESCE(finished_at, ?),
                    updated_at = ?
                WHERE {predicate}
                """,
                [error_message, now, now, *values],
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()

    def update(
        self,
        task_id: str,
        *,
        status: str,
        result_json: str | None = None,
        error_message: str | None = None,
        expected_worker_id: str | None = None,
    ) -> bool:
        """Compatibility updater for callers outside SyncTaskManager.

        New workers must use claim/heartbeat/set_terminal so a stale worker
        cannot publish over an expired or remotely cancelled row.
        """
        if status in {"COMPLETED", "FAILED", "CANCELLED"} and expected_worker_id:
            return self.set_terminal(
                task_id,
                worker_id=expected_worker_id,
                status=status,
                result_json=result_json,
                error_message=error_message,
            )
        now = time.time()
        connection = self._open()
        try:
            cursor = connection.execute(
                """
                UPDATE sync_jobs
                SET status = ?, result_json = ?, error_message = ?,
                    started_at = CASE
                        WHEN ? = 'RUNNING' THEN COALESCE(started_at, ?)
                        ELSE started_at
                    END,
                    finished_at = CASE
                        WHEN ? IN ('COMPLETED', 'FAILED', 'CANCELLED')
                        THEN COALESCE(finished_at, ?)
                        ELSE finished_at
                    END,
                    updated_at = ?
                WHERE task_id = ?
                  AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED', 'UNKNOWN')
                  AND (? IS NULL OR worker_id = ?)
                """,
                (
                    str(status),
                    result_json,
                    error_message,
                    str(status),
                    now,
                    str(status),
                    now,
                    now,
                    str(task_id),
                    expected_worker_id,
                    expected_worker_id,
                ),
            )
            connection.commit()
            return cursor.rowcount > 0
        finally:
            connection.close()


def _legacy_idempotency_path(target_path: str) -> Path:
    return Path(target_path).parent / "batch_idempotency.sqlite3"


def _migrate_legacy_idempotency(connection: sqlite3.Connection, target_path: str) -> int:
    """Import rows from the former standalone database without deleting it."""
    legacy_path = _legacy_idempotency_path(target_path)
    if not legacy_path.exists() or legacy_path.resolve() == Path(target_path).resolve():
        return 0

    legacy = sqlite3.connect(str(legacy_path), timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    try:
        table = legacy.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'batch_idempotency'"
        ).fetchone()
        if table is None:
            return 0
        rows = legacy.execute(
            """
            SELECT owner_id, idempotency_key, request_hash, task_id, api_id,
                   total, delay_sec, dry_run, created_at
            FROM batch_idempotency
            """
        ).fetchall()
    finally:
        legacy.close()

    imported = 0
    for row in rows:
        created_at = float(row[8])
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO idempotency_keys (
                issuer, tenant_id, owner_subject, operation_kind,
                idempotency_key, request_hash, task_id, api_id, total,
                delay_sec, dry_run, state, created_at, updated_at
            ) VALUES ('', '', ?, 'batch', ?, ?, ?, ?, ?, ?, ?, 'RESERVED', ?, ?)
            """,
            (
                str(row[0] or ""),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4] or ""),
                int(row[5] or 0),
                float(row[6] or 0),
                int(bool(row[7])),
                created_at,
                created_at,
            ),
        )
        imported += int(cursor.rowcount > 0)
    return imported


def initialize_database(path: str | None = None) -> str:
    """Create the schema once and import the old idempotency ledger."""
    target_path = database_path(path)
    with _initialization_lock:
        if target_path in _initialized_paths:
            return target_path
        connection = _connection(target_path)
        try:
            connection.executescript(SCHEMA_SQL)
            _ensure_schema_columns(connection)
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (1, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (2, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (3, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (4, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (5, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO schema_migrations(version, applied_at)
                VALUES (?, ?)
                """,
                (6, datetime.now(timezone.utc).isoformat()),
            )
            connection.execute("BEGIN IMMEDIATE")
            imported_legacy = _migrate_legacy_idempotency(connection, target_path)
            if imported_legacy:
                connection.execute(
                    """
                    INSERT INTO audit_events (
                        event_id, issuer, tenant_id, actor_subject, action,
                        resource_type, resource_id, outcome, metadata_json,
                        created_at
                    ) VALUES (?, '', '', '', 'legacy.migration',
                              'idempotency_ledger', ?, 'SUCCESS', ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        str(Path(target_path).name),
                        json.dumps(
                            {"imported_rows": imported_legacy},
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        time.time(),
                    ),
                )
            connection.commit()
            _secure_file(Path(target_path))
            _secure_file(Path(f"{target_path}-wal"))
            _secure_file(Path(f"{target_path}-shm"))
        except Exception:
            with _safe_rollback(connection):
                pass
            raise
        finally:
            connection.close()
        _initialized_paths.add(target_path)
    return target_path


@contextmanager
def _safe_rollback(connection: sqlite3.Connection) -> Iterator[None]:
    try:
        yield
    finally:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass


def open_database(path: str | None = None) -> sqlite3.Connection:
    """Return a configured connection to the unified SQLite database."""
    target_path = initialize_database(path)
    connection = _connection(target_path)
    _secure_file(Path(target_path))
    _secure_file(Path(f"{target_path}-wal"))
    _secure_file(Path(f"{target_path}-shm"))
    return connection
