"""后台管理全局目录同步任务。

目录同步会访问腾讯云文档站点并改写本地 catalog/registry，不能在 FastAPI
请求线程中等待。这个管理器沿用批处理 TaskRunner 的 daemon thread 模式，
但任务按同步类型单飞：同一类型运行中再次提交只返回现有任务，避免并发写
同一个 catalog 文件和重复热重载注册表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from typing import Any

from .persistence import (
    SYNC_JOB_LEASE_SECONDS,
    SYNC_TERMINAL_STATUSES,
    SqliteAuditStore,
    SqliteSyncJobStore,
)

TERMINAL_STATUSES = SYNC_TERMINAL_STATUSES
MAX_JOBS = 64
MAX_IDEMPOTENCY_KEYS = 256
MAX_ERROR_LENGTH = 2_000


class SyncTaskCancelled(Exception):
    """Signal a cooperative cancellation after in-flight work has drained."""

    def __init__(self, result: Any = None):
        super().__init__("同步任务已取消")
        self.result = result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SyncJob:
    task_id: str
    kind: str
    owner_id: str | None
    issuer: str = ""
    tenant_id: str = ""
    idempotency_key: str | None = None
    status: str = "PENDING"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    result: Any = None
    progress: dict[str, Any] = field(default_factory=dict)
    cancel_requested: bool = False
    error: str | None = None
    worker_id: str | None = None
    last_heartbeat: float | None = None
    attempt: int = 0

    def snapshot(self) -> dict[str, Any]:
        snapshot = {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "is_running": self.status not in TERMINAL_STATUSES,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
            "attempt": self.attempt,
            "cancel_requested": self.cancel_requested,
        }
        snapshot["progress"] = dict(self.progress)
        return snapshot


class SyncTaskManager:
    """目录同步任务注册表，支持内存模式和 SQLite 状态持久化。"""

    def __init__(
        self,
        *,
        database_path: str | None = None,
        persistent: bool = False,
    ) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, SyncJob] = {}
        self._latest_by_kind: dict[str, str] = {}
        self._idempotency: dict[tuple[str, str, str, str, str], str] = {}
        self.worker_id = f"{os.getpid()}-{uuid.uuid4().hex[:12]}"
        self._store = (
            SqliteSyncJobStore(database_path)
            if persistent
            else None
        )

    @staticmethod
    def _normalize_key(value: str | None) -> str | None:
        if value is None:
            return None
        key = str(value).strip()
        if not key:
            return None
        # Header values are deliberately bounded so a caller cannot grow the
        # in-memory idempotency index without limit.
        return key[:256]

    @staticmethod
    def _normalize_scope(value: str | None) -> str:
        return str(value or "").strip()[:512]

    def _scope_matches(
        self,
        row: dict[str, Any],
        *,
        issuer: str,
        tenant_id: str,
    ) -> bool:
        return (
            str(row.get("issuer") or "") == issuer
            and str(row.get("tenant_id") or "") == tenant_id
        )

    def _prune_locked(self) -> None:
        if len(self._jobs) <= MAX_JOBS:
            return
        removable = [
            job for job in self._jobs.values()
            if job.status in TERMINAL_STATUSES and self._latest_by_kind.get(job.kind) != job.task_id
        ]
        removable.sort(key=lambda item: item.finished_at or item.created_at)
        for job in removable[: max(0, len(self._jobs) - MAX_JOBS)]:
            self._jobs.pop(job.task_id, None)
            if job.idempotency_key:
                self._idempotency.pop(
                    (
                        job.kind,
                        job.issuer,
                        job.tenant_id,
                        job.owner_id or "",
                        job.idempotency_key,
                    ),
                    None,
                )

        if len(self._idempotency) > MAX_IDEMPOTENCY_KEYS:
            # Remove keys whose jobs were pruned first. If all keys are still
            # live, retain the most recent bounded subset by job creation time.
            ordered = sorted(
                self._idempotency.items(),
                key=lambda item: self._jobs.get(item[1], SyncJob("", "", None)).created_at,
            )
            for key, task_id in ordered[: len(self._idempotency) - MAX_IDEMPOTENCY_KEYS]:
                self._idempotency.pop(key, None)
                if task_id in self._jobs:
                    self._jobs[task_id].idempotency_key = None

    def get_status(
        self,
        kind: str,
        task_id: str | None = None,
        *,
        owner_id: str | None = None,
        issuer: str | None = None,
        tenant_id: str | None = None,
        is_admin: bool = False,
    ) -> dict[str, Any]:
        issuer_scope = self._normalize_scope(issuer)
        tenant_scope = self._normalize_scope(tenant_id)
        if self._store is not None:
            row = (
                self._store.get(task_id)
                if task_id
                else self._store.latest(
                    kind,
                    issuer=issuer_scope,
                    tenant_id=tenant_scope,
                    owner_subject=owner_id,
                    include_all=is_admin,
                )
            )
            if row is None or row.get("kind") != kind:
                return self._idle_snapshot(kind)
            if not self._scope_matches(
                row,
                issuer=issuer_scope,
                tenant_id=tenant_scope,
            ):
                raise PermissionError("无权访问该同步任务")
            row_owner = str(row.get("owner_subject") or "")
            if owner_id is not None and not is_admin and row_owner != owner_id:
                raise PermissionError("无权访问该同步任务")
            return self._snapshot_row(row)

        with self._lock:
            selected_id = task_id or self._latest_by_kind.get(kind)
            job = self._jobs.get(selected_id) if selected_id else None
            if job is None or job.kind != kind:
                return {
                    "task_id": None,
                    "kind": kind,
                    "status": "IDLE",
                    "is_running": False,
                    "created_at": None,
                    "started_at": None,
                    "finished_at": None,
                    "result": None,
                    "error": None,
                }
            if (
                job.issuer != issuer_scope
                or job.tenant_id != tenant_scope
            ):
                raise PermissionError("无权访问该同步任务")
            if task_id and owner_id is not None and not is_admin and job.owner_id != owner_id:
                raise PermissionError("无权访问该同步任务")
            if not task_id and owner_id is not None and not is_admin and job.owner_id != owner_id:
                # A status request without task_id is a convenience lookup for
                # the caller's latest task. Never disclose another operator's
                # task id, result, or failure details through that shortcut.
                return {
                    "task_id": None,
                    "kind": kind,
                    "status": "IDLE",
                    "is_running": False,
                    "created_at": None,
                    "started_at": None,
                    "finished_at": None,
                    "result": None,
                    "error": None,
                }
            return job.snapshot()

    @staticmethod
    def _idle_snapshot(kind: str) -> dict[str, Any]:
        return {
            "task_id": None,
            "kind": kind,
            "status": "IDLE",
            "is_running": False,
            "created_at": None,
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "attempt": 0,
        }

    @staticmethod
    def _snapshot_row(row: dict[str, Any]) -> dict[str, Any]:
        def format_time(value: Any) -> Any:
            if value is None or isinstance(value, str):
                return value
            try:
                return datetime.fromtimestamp(
                    float(value),
                    timezone.utc,
                ).isoformat()
            except (TypeError, ValueError, OSError, OverflowError):
                return None

        result = None
        raw_result = row.get("result_json")
        if raw_result:
            try:
                result = json.loads(raw_result)
            except (TypeError, ValueError):
                result = None
        status = str(row.get("status") or "PENDING")
        progress = SyncTaskManager._progress_from_row(row)
        return {
            "task_id": row.get("task_id"),
            "kind": row.get("kind"),
            "status": status,
            "is_running": status not in TERMINAL_STATUSES,
            "created_at": format_time(row.get("created_at")),
            "started_at": format_time(row.get("started_at")),
            "finished_at": format_time(row.get("finished_at")),
            "result": result,
            "error": row.get("error_message"),
            "attempt": int(row.get("attempt") or 0),
            "cancel_requested": bool(int(row.get("cancel_requested") or 0)),
            "progress": progress,
        }

    def start(
        self,
        kind: str,
        operation: Callable[[], Any],
        *,
        operation_factory: Callable[[str], Callable[[], Any]] | None = None,
        initial_progress: Mapping[str, Any] | None = None,
        owner_id: str | None = None,
        issuer: str | None = None,
        tenant_id: str | None = None,
        is_admin: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Start one sync operation and return a stable task handle.

        ``kind`` is the single-flight boundary. An optional idempotency key is
        scoped to the authenticated owner and remains associated with the
        completed result, allowing safe client retries after a lost response.
        """
        normalized_key = self._normalize_key(idempotency_key)
        issuer_scope = self._normalize_scope(issuer)
        tenant_scope = self._normalize_scope(tenant_id)
        owner_scope = owner_id or ""
        if self._store is not None:
            task_id = uuid.uuid4().hex[:12]
            reservation = self._store.reserve(
                task_id=task_id,
                kind=kind,
                issuer=issuer_scope,
                tenant_id=tenant_scope,
                owner_subject=owner_id,
                idempotency_key=normalized_key,
                is_admin=is_admin,
            )
            row = reservation.get("row") or {}
            if reservation["status"] == "recoverable":
                with self._lock:
                    existing_job = self._jobs.get(str(reservation["task_id"]))
                if existing_job is not None:
                    return {
                        "status": "already_running",
                        "task_id": existing_job.task_id,
                        "sync_status": existing_job.snapshot(),
                    }
                job = self._job_from_row(
                    row,
                    fallback_key=normalized_key,
                )
                with self._lock:
                    self._jobs[job.task_id] = job
                    self._latest_by_kind[kind] = job.task_id
                    if normalized_key:
                        self._idempotency[
                            (
                                kind,
                                issuer_scope,
                                tenant_scope,
                                owner_scope,
                                normalized_key,
                            )
                        ] = job.task_id
                    self._prune_locked()
                if initial_progress is not None:
                    self.update_progress(job.task_id, initial_progress)
                actual_operation = (
                    operation_factory(job.task_id)
                    if operation_factory is not None
                    else operation
                )
                self._start_thread(job, actual_operation)
                return {
                    "status": "recovered",
                    "task_id": job.task_id,
                    "sync_status": self.get_status(
                        kind,
                        job.task_id,
                        owner_id=owner_id,
                        issuer=issuer_scope,
                        tenant_id=tenant_scope,
                        is_admin=is_admin,
                    ),
                }
            if reservation["status"] != "started":
                return {
                    "status": reservation["status"],
                    "task_id": reservation.get("task_id"),
                    "sync_status": self._snapshot_row(row)
                    if row
                    else self._idle_snapshot(kind),
                }
            job = SyncJob(
                task_id=str(reservation["task_id"]),
                kind=kind,
                owner_id=owner_id,
                issuer=issuer_scope,
                tenant_id=tenant_scope,
                idempotency_key=normalized_key,
            )
            with self._lock:
                self._jobs[job.task_id] = job
                self._latest_by_kind[kind] = job.task_id
                if normalized_key:
                    self._idempotency[
                        (
                            kind,
                            issuer_scope,
                            tenant_scope,
                            owner_scope,
                            normalized_key,
                        )
                    ] = job.task_id
                self._prune_locked()
            if initial_progress is not None:
                self.update_progress(job.task_id, initial_progress)
            actual_operation = (
                operation_factory(job.task_id)
                if operation_factory is not None
                else operation
            )
            self._start_thread(job, actual_operation)
            return {
                "status": "started",
                "task_id": job.task_id,
                "sync_status": self.get_status(
                    kind,
                    job.task_id,
                    owner_id=owner_id,
                    issuer=issuer_scope,
                    tenant_id=tenant_scope,
                    is_admin=is_admin,
                ),
            }

        with self._lock:
            if normalized_key:
                existing_id = self._idempotency.get(
                    (
                        kind,
                        issuer_scope,
                        tenant_scope,
                        owner_scope,
                        normalized_key,
                    )
                )
                existing = self._jobs.get(existing_id) if existing_id else None
                if existing is not None:
                    return {
                        "status": "idempotent_replay",
                        "task_id": existing.task_id,
                        "sync_status": existing.snapshot(),
                    }

            active_id = self._latest_by_kind.get(kind)
            active = self._jobs.get(active_id) if active_id else None
            if active is not None and active.status not in TERMINAL_STATUSES:
                if not is_admin and active.owner_id != owner_id:
                    return {
                        "status": "already_running",
                        "task_id": None,
                        "sync_status": {
                            "task_id": None,
                            "kind": kind,
                            "status": active.status,
                            "is_running": True,
                        },
                    }
                if normalized_key:
                    # A retry can arrive with a newly generated key while the
                    # same caller's single-flight task is still running. Bind
                    # it to that task so a lost response cannot trigger a
                    # second sync after the first one finishes.
                    self._idempotency[
                        (
                            kind,
                            issuer_scope,
                            tenant_scope,
                            owner_scope,
                            normalized_key,
                        )
                    ] = active.task_id
                return {
                    "status": "already_running",
                    "task_id": active.task_id,
                    "sync_status": active.snapshot(),
                }

            task_id = uuid.uuid4().hex[:12]
            job = SyncJob(
                task_id=task_id,
                kind=kind,
                owner_id=owner_id,
                issuer=issuer_scope,
                tenant_id=tenant_scope,
                idempotency_key=normalized_key,
            )
            self._jobs[task_id] = job
            self._latest_by_kind[kind] = task_id
            if normalized_key:
                self._idempotency[
                    (
                        kind,
                        issuer_scope,
                        tenant_scope,
                        owner_scope,
                        normalized_key,
                    )
                ] = task_id
            self._prune_locked()

        if initial_progress is not None:
            with self._lock:
                job.progress = dict(initial_progress)
        actual_operation = (
            operation_factory(job.task_id)
            if operation_factory is not None
            else operation
        )
        self._start_thread(job, actual_operation)
        return {
            "status": "started",
            "task_id": task_id,
            "sync_status": self.get_status(
                kind,
                task_id,
                owner_id=owner_id,
                issuer=issuer_scope,
                tenant_id=tenant_scope,
                is_admin=is_admin,
            ),
        }

    def update_progress(
        self,
        task_id: str,
        progress: Mapping[str, Any],
    ) -> bool:
        """Publish bounded progress through the durable store."""

        if self._store is None:
            with self._lock:
                job = self._jobs.get(str(task_id))
                if job is None or job.status in TERMINAL_STATUSES:
                    return False
                job.progress = dict(progress)
                return True
        with self._lock:
            job = self._jobs.get(str(task_id))
            worker_id = job.worker_id if job is not None else None
        written = self._store.update_progress(
            str(task_id),
            progress,
            expected_worker_id=worker_id,
        )
        if written and job is not None:
            with self._lock:
                job.progress = dict(progress)
        return written

    def is_cancel_requested(self, task_id: str) -> bool:
        if self._store is None:
            with self._lock:
                job = self._jobs.get(str(task_id))
                return bool(job and getattr(job, "cancel_requested", False))
        return self._store.is_cancel_requested(str(task_id))

    def cancel(
        self,
        kind: str,
        *,
        task_id: str | None = None,
        owner_id: str | None = None,
        issuer: str | None = None,
        tenant_id: str | None = None,
        is_admin: bool = False,
    ) -> dict[str, Any]:
        """Request cooperative cancellation using the shared task row."""

        status = self.get_status(
            kind,
            task_id,
            owner_id=owner_id,
            issuer=issuer,
            tenant_id=tenant_id,
            is_admin=is_admin,
        )
        selected_id = status.get("task_id")
        if not selected_id or status.get("status") in TERMINAL_STATUSES:
            return {
                "status": "not_running",
                "message": "当前没有运行中的同步任务",
                "sync_status": status,
            }
        if self._store is not None:
            row = self._store.request_cancel(str(selected_id))
            if row is None:
                return {
                    "status": "not_found",
                    "message": "同步任务不存在",
                    "sync_status": self._idle_snapshot(kind),
                }
            with self._lock:
                job = self._jobs.get(str(selected_id))
                if job is not None:
                    job.cancel_requested = True
                    job.progress = self._progress_from_row(row)
            return {
                "status": "cancelling",
                "message": "已发送取消同步请求",
                "task_id": str(selected_id),
                "sync_status": self._snapshot_row(row),
            }
        with self._lock:
            job = self._jobs.get(str(selected_id))
            if job is None or job.status in TERMINAL_STATUSES:
                return {
                    "status": "not_running",
                    "message": "当前没有运行中的同步任务",
                    "sync_status": self._idle_snapshot(kind),
                }
            job.cancel_requested = True
            return {
                "status": "cancelling",
                "message": "已发送取消同步请求",
                "task_id": job.task_id,
                "sync_status": job.snapshot(),
            }

    @staticmethod
    def _progress_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
        raw_progress = row.get("progress_json")
        if not raw_progress:
            return {}
        try:
            parsed = json.loads(raw_progress)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _job_from_row(
        row: dict[str, Any],
        *,
        fallback_key: str | None = None,
    ) -> SyncJob:
        def as_iso(value: Any) -> str | None:
            if value is None or isinstance(value, str):
                return value
            try:
                return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
            except (TypeError, ValueError, OSError, OverflowError):
                return None

        result = None
        if row.get("result_json"):
            try:
                result = json.loads(row["result_json"])
            except (TypeError, ValueError):
                result = None
        status = str(row.get("status") or "PENDING")
        return SyncJob(
            task_id=str(row.get("task_id") or ""),
            kind=str(row.get("kind") or ""),
            owner_id=row.get("owner_subject") or None,
            issuer=str(row.get("issuer") or ""),
            tenant_id=str(row.get("tenant_id") or ""),
            idempotency_key=row.get("idempotency_key") or fallback_key,
            status=status,
            created_at=as_iso(row.get("created_at")) or _now(),
            started_at=as_iso(row.get("started_at")),
            finished_at=as_iso(row.get("finished_at")),
            result=result,
            progress=SyncTaskManager._progress_from_row(row),
            cancel_requested=bool(int(row.get("cancel_requested") or 0)),
            error=row.get("error_message"),
            worker_id=row.get("worker_id"),
            last_heartbeat=row.get("last_heartbeat"),
            attempt=int(row.get("attempt") or 0),
        )

    def _start_thread(
        self,
        job: SyncJob,
        operation: Callable[[], Any],
    ) -> None:
        thread = threading.Thread(
            target=self._run,
            args=(job, operation),
            name=f"sync-{job.kind}-{job.task_id}",
            daemon=True,
        )
        thread.start()

    def _audit_job(
        self,
        job: SyncJob,
        *,
        action: str,
        outcome: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self._store is None:
            return
        try:
            SqliteAuditStore(self._store.path).append(
                action=action,
                resource_type="sync_job",
                resource_id=job.task_id,
                outcome=outcome,
                actor_subject=job.owner_id,
                issuer=job.issuer,
                tenant_id=job.tenant_id,
                metadata=metadata or {},
            )
        except Exception as exc:  # noqa: BLE001 - keep worker outcome durable
            print(f"[WARN] sync audit event write failed: {exc}")

    def _run(self, job: SyncJob, operation: Callable[[], Any]) -> None:
        heartbeat_stop = threading.Event()
        heartbeat_lost = threading.Event()
        if self._store is not None:
            if not self._store.claim(
                job.task_id,
                self.worker_id,
                lease_seconds=SYNC_JOB_LEASE_SECONDS,
            ):
                row = self._store.get(job.task_id)
                if row is not None:
                    refreshed = self._job_from_row(row)
                    with self._lock:
                        job.status = refreshed.status
                        job.started_at = refreshed.started_at
                        job.finished_at = refreshed.finished_at
                        job.result = refreshed.result
                        job.error = refreshed.error
                        job.progress = refreshed.progress
                        job.cancel_requested = refreshed.cancel_requested
                        job.attempt = refreshed.attempt
                if job.status == "CANCELLED":
                    self._audit_job(
                        job,
                        action="sync.cancelled",
                        outcome="CANCELLED",
                        metadata={"reason": "cancelled_before_claim"},
                    )
                else:
                    self._audit_job(
                        job,
                        action="sync.unknown",
                        outcome="UNKNOWN",
                        metadata={"reason": "claim_failed"},
                    )
                return
            with self._lock:
                job.status = "RUNNING"
                job.started_at = job.started_at or _now()
                job.worker_id = self.worker_id
                job.attempt += 1
                job.last_heartbeat = time.time()

            def heartbeat_loop() -> None:
                interval = max(1.0, SYNC_JOB_LEASE_SECONDS / 3)
                while not heartbeat_stop.wait(interval):
                    if self._store and not self._store.heartbeat(
                        job.task_id,
                        self.worker_id,
                        lease_seconds=SYNC_JOB_LEASE_SECONDS,
                    ):
                        heartbeat_lost.set()
                        with self._lock:
                            job.status = "UNKNOWN"
                            job.error = (
                                "同步任务租约丢失，外部同步结果未知，需要人工核对"
                            )
                        if self._store:
                            self._store.mark_unknown(
                                job.task_id,
                                worker_id=self.worker_id,
                                error_message=job.error,
                            )
                        return

            threading.Thread(
                target=heartbeat_loop,
                name=f"sync-lease-{job.kind}-{job.task_id}",
                daemon=True,
            ).start()
        else:
            with self._lock:
                job.status = "RUNNING"
                job.started_at = _now()
        try:
            result = operation()
        except SyncTaskCancelled as exc:
            result = exc.result
            result_json = None
            try:
                result_json = json.dumps(
                    result,
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError):
                result_json = None
            written = True
            if self._store is not None and not heartbeat_lost.is_set():
                written = self._store.set_terminal(
                    job.task_id,
                    worker_id=self.worker_id,
                    status="CANCELLED",
                    result_json=result_json,
                )
                if not written:
                    heartbeat_lost.set()
            if heartbeat_lost.is_set() and self._store is not None:
                self._store.mark_unknown(
                    job.task_id,
                    worker_id=self.worker_id,
                    error_message="同步任务执行权已丢失，结果未知，需要人工核对",
                )
            with self._lock:
                job.status = "UNKNOWN" if not written else "CANCELLED"
                job.finished_at = job.finished_at or _now()
                job.result = None if not written else result
                job.error = (
                    "同步任务执行结果未知，需要人工核对"
                    if not written
                    else None
                )
                job.cancel_requested = True
            self._audit_job(
                job,
                action="sync.unknown" if not written else "sync.cancelled",
                outcome="UNKNOWN" if not written else "CANCELLED",
                metadata={"reason": "cooperative_cancel"},
            )
            heartbeat_stop.set()
            return
        except Exception as exc:  # noqa: BLE001 - publish a safe task error
            message = str(exc).strip() or exc.__class__.__name__
            if self._store is not None and not heartbeat_lost.is_set():
                written = self._store.set_terminal(
                    job.task_id,
                    worker_id=self.worker_id,
                    status="FAILED",
                    error_message=message[:MAX_ERROR_LENGTH],
                )
                if not written:
                    heartbeat_lost.set()
            if heartbeat_lost.is_set() and self._store is not None:
                self._store.mark_unknown(
                    job.task_id,
                    worker_id=self.worker_id,
                    error_message=(
                        "同步任务执行结果未知，需要人工核对"
                    ),
                )
            with self._lock:
                job.status = "UNKNOWN" if heartbeat_lost.is_set() else "FAILED"
                job.error = (
                    "同步任务执行结果未知，需要人工核对"
                    if heartbeat_lost.is_set()
                    else message[:MAX_ERROR_LENGTH]
                )
                job.finished_at = job.finished_at or _now()
                job.result = None
            self._audit_job(
                job,
                action="sync.unknown" if heartbeat_lost.is_set() else "sync.failed",
                outcome="UNKNOWN" if heartbeat_lost.is_set() else "FAILED",
                metadata={"error": job.error},
            )
            heartbeat_stop.set()
            return
        finally:
            heartbeat_stop.set()

        result_json = None
        try:
            result_json = json.dumps(
                result,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
        except (TypeError, ValueError):
            result_json = None

        terminal_status = "COMPLETED"
        if self._store is not None and self._store.is_cancel_requested(job.task_id):
            terminal_status = "CANCELLED"

        if self._store is not None:
            written = False
            try:
                written = self._store.set_terminal(
                    job.task_id,
                    worker_id=self.worker_id,
                    status=terminal_status,
                    result_json=result_json,
                )
            except Exception:
                written = False
            if not written:
                heartbeat_lost.set()
                self._store.mark_unknown(
                    job.task_id,
                    worker_id=self.worker_id,
                    error_message="同步任务执行权已丢失，结果未知，需要人工核对",
                )
                row = self._store.get(job.task_id)
                if row is not None:
                    refreshed = self._job_from_row(row)
                    with self._lock:
                        job.status = refreshed.status
                        job.finished_at = refreshed.finished_at
                        job.error = refreshed.error
                        job.result = refreshed.result
                        job.attempt = refreshed.attempt
                self._audit_job(
                    job,
                    action="sync.unknown",
                    outcome="UNKNOWN",
                    metadata={"reason": "terminal_write_lost"},
                )
                return

        with self._lock:
            job.status = terminal_status
            job.finished_at = _now()
            job.result = result
            job.error = None
            job.cancel_requested = terminal_status == "CANCELLED"
        self._audit_job(
            job,
            action="sync.cancelled" if terminal_status == "CANCELLED" else "sync.completed",
            outcome="CANCELLED" if terminal_status == "CANCELLED" else "SUCCESS",
            metadata={"attempt": job.attempt},
        )


sync_task_manager = SyncTaskManager(persistent=True)
