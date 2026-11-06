"""后台管理全局目录同步任务。

目录同步会访问腾讯云文档站点并改写本地 catalog/registry，不能在 FastAPI
请求线程中等待。这个管理器沿用批处理 TaskRunner 的 daemon thread 模式，
但任务按同步类型单飞：同一类型运行中再次提交只返回现有任务，避免并发写
同一个 catalog 文件和重复热重载注册表。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
import uuid
from collections.abc import Callable
from typing import Any


TERMINAL_STATUSES = frozenset({"COMPLETED", "FAILED"})
MAX_JOBS = 64
MAX_IDEMPOTENCY_KEYS = 256
MAX_ERROR_LENGTH = 2_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class SyncJob:
    task_id: str
    kind: str
    owner_id: str | None
    idempotency_key: str | None = None
    status: str = "PENDING"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    result: Any = None
    error: str | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "kind": self.kind,
            "status": self.status,
            "is_running": self.status not in TERMINAL_STATUSES,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "result": self.result,
            "error": self.error,
        }


class SyncTaskManager:
    """单进程目录同步任务注册表。

    任务是内存状态，和现有 ``TaskRunner`` 一样面向当前进程；同步结果本身
    仍由原同步函数持久化到 catalog/storage。状态查询因此不会再次触发网络。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: dict[str, SyncJob] = {}
        self._latest_by_kind: dict[str, str] = {}
        self._idempotency: dict[tuple[str, str, str], str] = {}

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
                self._idempotency.pop((job.kind, job.owner_id or "", job.idempotency_key), None)

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
        is_admin: bool = False,
    ) -> dict[str, Any]:
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

    def start(
        self,
        kind: str,
        operation: Callable[[], Any],
        *,
        owner_id: str | None = None,
        is_admin: bool = False,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Start one sync operation and return a stable task handle.

        ``kind`` is the single-flight boundary. An optional idempotency key is
        scoped to the authenticated owner and remains associated with the
        completed result, allowing safe client retries after a lost response.
        """
        normalized_key = self._normalize_key(idempotency_key)
        owner_scope = owner_id or ""
        with self._lock:
            if normalized_key:
                existing_id = self._idempotency.get((kind, owner_scope, normalized_key))
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
                    self._idempotency[(kind, owner_scope, normalized_key)] = active.task_id
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
                idempotency_key=normalized_key,
            )
            self._jobs[task_id] = job
            self._latest_by_kind[kind] = task_id
            if normalized_key:
                self._idempotency[(kind, owner_scope, normalized_key)] = task_id
            self._prune_locked()

        thread = threading.Thread(
            target=self._run,
            args=(job, operation),
            name=f"sync-{kind}-{task_id}",
            daemon=True,
        )
        thread.start()
        return {
            "status": "started",
            "task_id": task_id,
            "sync_status": self.get_status(kind, task_id, owner_id=owner_id, is_admin=is_admin),
        }

    def _run(self, job: SyncJob, operation: Callable[[], Any]) -> None:
        with self._lock:
            # The task may only be observed as PENDING for a short interval;
            # transition under the same lock used by status readers.
            job.status = "RUNNING"
            job.started_at = _now()
        try:
            result = operation()
        except Exception as exc:  # noqa: BLE001 - publish a safe task error
            message = str(exc).strip() or exc.__class__.__name__
            with self._lock:
                job.status = "FAILED"
                job.error = message[:MAX_ERROR_LENGTH]
                job.finished_at = _now()
                job.result = None
            return
        with self._lock:
            job.status = "COMPLETED"
            job.finished_at = _now()
            job.result = result
            job.error = None


sync_task_manager = SyncTaskManager()
