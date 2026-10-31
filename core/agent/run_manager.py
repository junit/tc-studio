# ruff: noqa
"""
Agent 异步执行与流式会话恢复管理器 (AgentRunManager)
支持：
1. 后台解耦执行（断开连接/刷新页面不会终止后台任务）
2. 运行期状态全量快照与持久事件日志 (State Snapshot & Event Buffer)
3. 刷新页面后的无感断线重连与实时流恢复 (Catch-up Replay & Live Resumption)
4. 手动停止与资源清理
"""

import asyncio
import json
import threading
import time
from typing import Any, Dict, List, Optional

from .constants import MAX_CACHED_RUNS, RUN_TTL_SECONDS

MAX_EVENTS_PER_RUN = 2000


class AgentRunSubscriber:
    """线程安全的异步事件队列订阅者"""
    def __init__(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        self.queue = queue
        self.loop = loop

    def _enqueue(self, event: dict):
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # A slow client must not be able to grow memory without bound.
            pass

    def put_event(self, event_name: str, data: Dict[str, Any]):
        evt = {"event": event_name, "data": data, "timestamp": time.time()}
        try:
            self.loop.call_soon_threadsafe(self._enqueue, evt)
        except Exception:
            pass


class AgentRun:
    """单个智能体执行任务实体"""
    def __init__(
        self,
        run_id: str,
        bot_id: str,
        user_message: str,
        client_msg_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        skip_user_append: bool = False
    ):
        self.run_id = run_id
        self.bot_id = bot_id
        self.user_message = user_message
        self.attachments = attachments or []
        self.skip_user_append = skip_user_append
        now_ts = int(time.time() * 1000)
        self.user_msg_id = f"usr-{now_ts}"
        self.assistant_msg_id = client_msg_id or f"ast-{now_ts}"
        self.status = "running"  # running, done, error, stopped
        self.stream_status = "thinking"  # thinking, executing, answering, done, error
        self.start_time = time.time()
        self.end_time: Optional[float] = None

        self.thought = ""
        self.code_blocks: List[Dict[str, Any]] = []
        self.execution_logs: List[Dict[str, Any]] = []
        self.content = ""
        self.error: Optional[str] = None
        self.current_step = 1

        self.events: List[Dict[str, Any]] = []
        self.subscribers: List[AgentRunSubscriber] = []
        self._stop_requested = False
        self.pending_approval: Optional[Dict[str, Any]] = None
        self._approval_event: Optional[threading.Event] = None
        self._approval_decision: Optional[str] = None
        self._lock = threading.Lock()

    def request_stop(self):
        """请求中断执行"""
        with self._lock:
            if self.status != "running":
                return
            self._stop_requested = True
            self.status = "stopped"
            self.stream_status = "done"
            self.end_time = time.time()
            approval_event = self._approval_event
            self._approval_decision = "rejected"
        if approval_event:
            approval_event.set()
        self.dispatch_event("done", {"status": "stopped"})

    def request_approval(self, approval_data: Dict[str, Any]) -> str:
        """挂起当前执行线程，向订阅者分发 approval_required 事件，等待用户授权或拒绝"""
        with self._lock:
            self.pending_approval = approval_data
            self._approval_decision = None
            self._approval_event = threading.Event()
            self.stream_status = "waiting_approval"

        # 分发 approval_required 事件给前端
        self.dispatch_event("approval_required", approval_data)

        # 阻塞等待用户决策（最长等待 300 秒 = 5 分钟）
        timeout_sec = approval_data.get("timeout_seconds", 300)
        signaled = self._approval_event.wait(timeout=timeout_sec)

        with self._lock:
            decision = self._approval_decision
            self.pending_approval = None
            self._approval_event = None
            if not signaled:
                decision = "timeout"
            elif not decision:
                decision = "rejected"
            self.stream_status = "executing"

        self.dispatch_event("approval_resolved", {
            "approval_id": approval_data.get("approval_id"),
            "decision": decision
        })
        return decision

    def resolve_approval(self, approval_id: str, decision: str) -> bool:
        """接收用户在前端提交的决策 (approve 或 reject) 并唤醒工作线程"""
        with self._lock:
            if not self.pending_approval or self.pending_approval.get("approval_id") != approval_id:
                return False
            normalized_decision = decision.lower().strip()
            if normalized_decision not in {"approve", "reject"}:
                return False
            self._approval_decision = normalized_decision
            if self._approval_event:
                self._approval_event.set()
            return True

    @property
    def is_stopped(self) -> bool:
        return self._stop_requested

    def register_subscriber(self, queue: asyncio.Queue, loop: asyncio.AbstractEventLoop) -> tuple:
        """线程安全注册订阅者并原子获取当前快照，确保无事件漏发与重复"""
        with self._lock:
            sub = AgentRunSubscriber(queue, loop)
            self.subscribers.append(sub)
            snapshot = self._to_dict_unlocked()
        return sub, snapshot

    def unregister_subscriber(self, sub: AgentRunSubscriber):
        with self._lock:
            if sub in self.subscribers:
                self.subscribers.remove(sub)

    def dispatch_event(self, event_name: str, data: Dict[str, Any]):
        """分发事件给所有活跃订阅者并同步更新内存状态"""
        evt = {"event": event_name, "data": data, "timestamp": time.time()}
        with self._lock:
            self.events.append(evt)
            if len(self.events) > MAX_EVENTS_PER_RUN:
                del self.events[:-MAX_EVENTS_PER_RUN]
            self._update_internal_state(event_name, data)
            current_subs = list(self.subscribers)

        for sub in current_subs:
            sub.put_event(event_name, data)

    def _update_internal_state(self, event_name: str, data: Dict[str, Any]):
        if event_name == "thought_chunk":
            delta = data.get("delta", "")
            self.thought += delta
            self.stream_status = "thinking"
        elif event_name == "thought":
            c = data.get("content", "").strip()
            if c:
                self.thought = (self.thought + "\n\n" if self.thought else "") + c
        elif event_name == "code_start":
            self.stream_status = "executing"
            step = data.get("step", 1)
            self.current_step = step
            item = {"step": step, "code": data.get("code", ""), "status": "running"}
            idx = next((i for i, b in enumerate(self.code_blocks) if b.get("step") == step), -1)
            if idx >= 0:
                self.code_blocks[idx] = item
            else:
                self.code_blocks.append(item)
        elif event_name == "execution_result":
            step = data.get("step", 1)
            log_item = {"step": step, "output": data.get("output", ""), "status": "success"}
            idx = next((i for i, l in enumerate(self.execution_logs) if l.get("step") == step), -1)
            if idx >= 0:
                self.execution_logs[idx] = log_item
            else:
                self.execution_logs.append(log_item)
            for b in self.code_blocks:
                if b.get("step") == step and b.get("status") == "running":
                    b["status"] = "success"
        elif event_name == "answer_start":
            self.stream_status = "answering"
            for b in self.code_blocks:
                if b.get("status") == "running":
                    b["status"] = "success"
        elif event_name == "chunk":
            self.stream_status = "answering"
            self.content += data.get("delta", "")
        elif event_name == "message":
            self.stream_status = "answering"
            self.content = data.get("content", "") or self.content
        elif event_name == "done":
            terminal_status = data.get("status")
            if terminal_status == "stopped":
                self.status = "stopped"
                self.stream_status = "done"
            elif self.status == "error" or terminal_status == "error":
                self.status = "error"
                self.stream_status = "error"
            else:
                self.status = "done"
                self.stream_status = "done"
            self.end_time = time.time()
            if data.get("attachments"):
                self.attachments = data.get("attachments")
            elif data.get("files"):
                self.attachments = data.get("files")
            for b in self.code_blocks:
                if b.get("status") == "running":
                    b["status"] = "failed" if self.status in ("error", "stopped") else "success"
        elif event_name == "error":
            self.status = "error"
            self.stream_status = "error"
            self.end_time = time.time()
            self.error = data.get("error", "未知错误")

    def _to_dict_unlocked(self) -> Dict[str, Any]:
        elapsed = (self.end_time or time.time()) - self.start_time
        return {
            "run_id": self.run_id,
            "bot_id": self.bot_id,
            "user_msg_id": self.user_msg_id,
            "assistant_msg_id": self.assistant_msg_id,
            "user_message": self.user_message,
            "attachments": self.attachments,
            "status": self.status,
            "stream_status": self.stream_status,
            "start_time": self.start_time,
            "elapsed_seconds": round(elapsed, 1),
            "thought": self.thought,
            "code_blocks": [dict(b) for b in self.code_blocks],
            "execution_logs": [dict(l) for l in self.execution_logs],
            "content": self.content,
            "current_step": self.current_step,
            "pending_approval": self.pending_approval,
            "error": self.error
        }

    def to_dict(self) -> Dict[str, Any]:
        """导出当前快照（包含思维链、代码块、执行结果与文本）"""
        with self._lock:
            return self._to_dict_unlocked()


class AgentRunManager:
    """全局会话运行管理器（具备防内存泄漏的自动淘汰机制）"""
    def __init__(self, max_cached_runs: int = MAX_CACHED_RUNS, run_ttl_seconds: int = RUN_TTL_SECONDS):
        self.runs: Dict[str, AgentRun] = {}
        self.active_bot_runs: Dict[str, str] = {}  # bot_id -> active run_id
        self.max_cached_runs = max_cached_runs
        self.run_ttl_seconds = run_ttl_seconds
        self._lock = threading.Lock()

    def _evict_stale_runs_unlocked(self):
        """淘汰过期或超出缓存上限的历史已完成任务，防止内存泄漏"""
        now = time.time()
        active_run_ids = set(self.active_bot_runs.values())

        # 1. 淘汰已完成且已超过 TTL (默认 1 小时) 的 Run
        to_delete = []
        for run_id, run in self.runs.items():
            if run_id in active_run_ids or run.status == "running":
                continue
            if run.end_time and (now - run.end_time) > self.run_ttl_seconds:
                to_delete.append(run_id)

        for rid in to_delete:
            self.runs.pop(rid, None)

        # 2. 如果非活跃完成任务总数仍然超出上限，按创建时间淘汰最老的非活跃 Run
        if len(self.runs) > self.max_cached_runs:
            finished_runs = [
                (rid, r.start_time)
                for rid, r in self.runs.items()
                if rid not in active_run_ids and r.status != "running"
            ]
            finished_runs.sort(key=lambda x: x[1])
            excess_count = len(self.runs) - self.max_cached_runs
            for rid, _ in finished_runs[:excess_count]:
                self.runs.pop(rid, None)

    def get_active_run(self, bot_id: str) -> Optional[AgentRun]:
        with self._lock:
            run_id = self.active_bot_runs.get(bot_id)
            if not run_id:
                return None
            run = self.runs.get(run_id)
            if run and run.status == "running":
                return run
            return None

    def list_active_runs(self) -> Dict[str, Dict[str, Any]]:
        """获取所有当前正在运行中的智能体任务快照简述"""
        result = {}
        with self._lock:
            for bot_id, run_id in list(self.active_bot_runs.items()):
                run = self.runs.get(run_id)
                if run and run.status == "running":
                    elapsed = (run.end_time or time.time()) - run.start_time
                    result[bot_id] = {
                        "run_id": run.run_id,
                        "bot_id": run.bot_id,
                        "status": run.status,
                        "stream_status": run.stream_status,
                        "start_time": run.start_time,
                        "elapsed_seconds": round(elapsed, 1),
                        "current_step": run.current_step,
                        "user_message": run.user_message[:60] if run.user_message else "",
                    }
        return result

    def resolve_approval(self, bot_id: str, approval_id: str, decision: str) -> bool:
        """为指定智能体当前的挂起操作提交决策"""
        run = self.get_active_run(bot_id)
        if run:
            return run.resolve_approval(approval_id, decision)
        return False

    def stop_run(self, bot_id: str) -> bool:
        """停止指定智能体的当前任务"""
        run = self.get_active_run(bot_id)
        if run:
            run.request_stop()
            with self._lock:
                if self.active_bot_runs.get(bot_id) == run.run_id:
                    self.active_bot_runs.pop(bot_id, None)
            return True
        return False

    def start_run(
        self,
        bot_id: str,
        user_message: str,
        engine,
        bot_manager,
        client_msg_id: Optional[str] = None,
        attachments: Optional[List[Dict[str, Any]]] = None,
        skip_user_append: bool = False
    ) -> AgentRun:
        """启动新的后台执行任务（若已有运行中的任务先中止旧任务）"""
        with self._lock:
            self._evict_stale_runs_unlocked()
            old_run_id = self.active_bot_runs.get(bot_id)
            if old_run_id and old_run_id in self.runs:
                old_run = self.runs[old_run_id]
                if old_run.status == "running":
                    old_run.request_stop()

            run_id = f"run_{bot_id}_{int(time.time() * 1000)}"
            run = AgentRun(
                run_id,
                bot_id,
                user_message,
                client_msg_id=client_msg_id,
                attachments=attachments,
                skip_user_append=skip_user_append
            )
            self.runs[run_id] = run
            self.active_bot_runs[bot_id] = run_id

        # 启动后台独立工作线程
        worker = threading.Thread(
            target=self._execute_run_worker,
            args=(run, engine, bot_manager),
            daemon=True,
            name=f"Worker-{run_id}"
        )
        worker.start()
        return run

    def _execute_run_worker(self, run: AgentRun, engine, bot_manager):
        """后台独立工作线程：执行 agent_engine 并持续分发事件"""
        try:
            stream_gen = engine.run_agent_stream(
                run.bot_id,
                run.user_message,
                attachments=run.attachments,
                skip_user_append=run.skip_user_append,
                stop_checker=lambda: run.is_stopped,
                approval_requester=run.request_approval
            )
            for event_obj in stream_gen:
                if run.is_stopped:
                    break
                evt_name = event_obj.get("event", "message")
                data = event_obj.get("data", {})
                run.dispatch_event(evt_name, data)
                if evt_name in ("done", "error"):
                    break
        except Exception as e:
            if not run.is_stopped:
                run.dispatch_event("error", {"error": str(e)})
                run.dispatch_event("done", {"status": "error"})
        finally:
            if run.status == "running":
                run.status = "done"
                run.stream_status = "done"
                run.dispatch_event("done", {"status": "done"})
            with self._lock:
                if self.active_bot_runs.get(run.bot_id) == run.run_id:
                    self.active_bot_runs.pop(run.bot_id, None)
                self._evict_stale_runs_unlocked()


# 单例实例
agent_run_manager = AgentRunManager()
