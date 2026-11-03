"""
批处理任务调度器与 SSE 事件流推送
"""

from __future__ import annotations

import json
import os
import queue
import random
import re
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from .cloud_client import UniversalCloudClient
from .config import load_config
from .registry import registry
from .agent.risk import is_high_risk_api

STORAGE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "storage"))
HISTORY_PATH = os.path.join(STORAGE_DIR, "history.json")
_history_lock = threading.Lock()
MAX_CACHED_TASKS = 50
MAX_EVENTS_PER_TASK = 2000
# Rate limiting is transient: retry a single item three times after its
# initial request, with bounded exponential backoff and jitter.
MAX_RATE_LIMIT_RETRIES = 3
RATE_LIMIT_BACKOFF_BASE_SEC = 0.2
RATE_LIMIT_BACKOFF_JITTER_SEC = 0.1
RATE_LIMIT_ERROR_CODES = frozenset({"90001", "429", "requestlimitexceeded"})


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


def load_history() -> list[dict]:

    with _history_lock:
        if os.path.exists(HISTORY_PATH):
            try:
                with open(HISTORY_PATH, encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                print(f"[WARN] Failed to load history: {e}")
        return []


def save_history_item(batch_record: dict):
    with _history_lock:
        try:
            os.makedirs(STORAGE_DIR, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(f"无法创建批处理存储目录: {STORAGE_DIR}") from exc
        history = []
        if os.path.exists(HISTORY_PATH):
            try:
                with open(HISTORY_PATH, encoding="utf-8") as f:
                    history = json.load(f)
            except Exception:
                history = []
        history.insert(0, batch_record)
        # 最多保留 50 次批处理历史
        history = history[:50]
        _atomic_dump_json(HISTORY_PATH, history, indent=2)


def delete_history_item(task_id: str) -> bool:
    with _history_lock:
        if not os.path.exists(HISTORY_PATH):
            return False
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                history = json.load(f)
        except Exception:
            return False
        new_history = [h for h in history if h.get("task_id") != task_id]
        if len(new_history) != len(history):
            _atomic_dump_json(HISTORY_PATH, new_history, indent=2)
            return True
        return False


def clear_history() -> bool:
    with _history_lock:
        _atomic_dump_json(HISTORY_PATH, [], indent=2)
        return True


class BatchTask:
    def __init__(self, task_id: str, api_id: str, items: list[dict], delay_sec: float = 0.05, dry_run: bool = False, config_snapshot: dict | None = None, owner_id: str | None = None):

        self.task_id = task_id
        self.api_id = api_id
        self.items = items
        self.delay_sec = delay_sec
        self.dry_run = dry_run
        self.config_snapshot = config_snapshot
        self.owner_id = owner_id
        self.total = len(items)
        self.current = 0
        self.success = 0
        self.failed = 0
        self.status = "PENDING"  # PENDING, RUNNING, PAUSED, CANCELLED, COMPLETED, FAILED
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
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # set means running, clear means paused

    def register_subscriber(self) -> tuple:
        """注册新订阅者并获取全量历史事件快照，支持无感重连防事件撕裂"""
        q = queue.Queue(maxsize=MAX_EVENTS_PER_TASK)
        with self._lock:
            self._subscribers.append(q)
            history = list(self.events)
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
        if callback:
            callback()

    def pause(self):
        with self._lock:
            if self.status in ("PENDING", "CANCELLED", "COMPLETED", "FAILED"):
                return
            self._pause_event.clear()
            self.status = "PAUSED"

    def resume(self):
        with self._lock:
            if self.status != "PAUSED":
                return
            self._pause_event.set()
            self.status = "RUNNING"


class TaskRunner:
    def __init__(self):
        self.tasks: dict[str, BatchTask] = {}
        self._lock = threading.Lock()

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
        allow_high_risk: bool = False,
    ) -> BatchTask:

        api_meta = registry.get_api(api_id)
        if not dry_run and api_meta and is_high_risk_api(api_id, api_meta) and not allow_high_risk:
            raise ValueError("高危批处理接口必须经过显式确认")

        cfg = load_config()
        if delay_sec is None:
            delay_sec = float(cfg.get("delay_sec", 0.05))
        task_id = str(uuid.uuid4())[:8]
        # 快照当前激活配置，确保任务执行期间不受全局环境切换影响，彻底做到凭证隔离
        config_snapshot = dict(cfg)
        task = BatchTask(task_id, api_id, items, delay_sec=float(delay_sec), dry_run=dry_run, config_snapshot=config_snapshot, owner_id=owner_id)

        task._finalize_callback = lambda task=task: self._finalize_task(task, None, "任务在启动前被取消")
        with self._lock:
            # 淘汰超出缓存上限的陈旧任务，控制内存
            if len(self.tasks) >= MAX_CACHED_TASKS:
                completed_keys = [k for k, t in self.tasks.items() if t.status in ("COMPLETED", "CANCELLED", "FAILED")]
                for k in completed_keys[: len(self.tasks) - MAX_CACHED_TASKS + 1]:
                    self.tasks.pop(k, None)
                if len(self.tasks) >= MAX_CACHED_TASKS:
                    raise ValueError("批处理任务缓存已满，请等待现有任务结束")
            self.tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> BatchTask | None:

        return self.tasks.get(task_id)

    def start_task(self, task_id: str) -> bool:
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")

        with task._lock:
            if task._started or task.status != "PENDING" or task._finalized:
                return False
            task._started = True
        threading.Thread(target=self._worker, args=(task,), daemon=True).start()
        return True

    def _finalize_task(self, task: BatchTask, api_meta: Any | None, error_message: str | None = None):
        with task._lock:
            if task._finalized:
                return
            task._finalized = True
            task.end_time = task.end_time or time.time()
            if task.status == "CANCELLED":
                final_status = "CANCELLED"
            elif error_message:
                task.status = "FAILED"
                final_status = "FAILED"
            elif task.status == "FAILED":
                final_status = "FAILED"
            else:
                task.status = "COMPLETED"
                final_status = "COMPLETED"
            end_time = task.end_time
            start_time = task.start_time or end_time
            summary = {
                "task_id": task.task_id,
                "api_id": task.api_id,
                "owner_id": task.owner_id,
                "api_name": api_meta.name if api_meta else task.api_id,
                "service_type": getattr(api_meta, "service_type", "im") if api_meta else "im",
                "total": task.total,
                "success": task.success,
                "failed": task.failed,
                "duration_sec": round(end_time - start_time, 2),
                "status": final_status,
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(start_time)),
            }
        if error_message and final_status != "CANCELLED":
            task.emit("error", {"message": error_message, "status": final_status})
        else:
            task.emit("complete", summary)
        try:
            save_history_item({**summary, "results": task.results})
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
        if cancelled_before_start:
            self._finalize_task(task, None)
            return
        if not api_meta:
            self._finalize_task(task, None, f"API {task.api_id} 未注册")
            return

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
                if task._stop_event.is_set():
                    break
                while not task._pause_event.wait(timeout=0.1):
                    if task._stop_event.is_set():
                        break
                if task._stop_event.is_set():
                    break
                task.current = idx

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
        except Exception as global_err:
            self._finalize_task(task, api_meta, f"批处理工作线程异常中断: {global_err!s}")
            return
        self._finalize_task(task, api_meta)


task_runner = TaskRunner()
